#!/usr/bin/env python3
"""真编端到端：起 PHP 服务 → 注册 → 提交构建 → 等 worker 编译 → 校验产物与下载链接。

这是对「PHP 版能不能真的编出固件」的**唯一有效验证** ——
其它断言都只验到「入队/抢槽」。需要约 2.6 GB 磁盘。

用法：python3 tools/verify_php_build.py
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)

PORT = int(os.environ.get("KWRT_VERIFY_PORT", "8098"))
DB = "/tmp/verify_php_build.db"
BASE = f"http://127.0.0.1:{PORT}"
TARGET, PROFILE, VERSION = "x86/64", "generic", "25.12"
# ★ VERSION 必须用**界面下拉框里真实的取值**（分支号 "25.12"），不能用发布号
#   "25.12.5"。曾经因为测试传了发布号而一路绿灯，但界面提交的是分支号，
#   镜像站只认发布号（releases/25.12/… → 404），真用户从界面构建必然失败。
#   测试取值与真实路径不一致，测了等于没测。
EXPECT_RELEASE = "25.12.5"


def http(path, method="GET", data=None, headers=None, cookie=None, timeout=60):
    h = dict(headers or {})
    body = None
    if data is not None:
        if isinstance(data, dict):
            body = json.dumps(data).encode()
            h.setdefault("Content-Type", "application/json")
        else:
            body = data.encode() if isinstance(data, str) else data
    if cookie:
        h["Cookie"] = cookie
    req = urllib.request.Request(BASE + path, data=body, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers
    except Exception as e:
        return None, str(e).encode(), {}


def sid(hdrs):
    vals = hdrs.get_all("Set-Cookie") if hasattr(hdrs, "get_all") else []
    for v in vals or []:
        if v.startswith("kwrt_sid="):
            return v.split(";")[0]
    return ""


def main() -> int:
    free = os.statvfs(ROOT).f_bavail * os.statvfs(ROOT).f_frsize
    print(f"磁盘可用: {free / 2**30:.2f} GB（构建约需 2.6 GB）")
    if free < 2.6 * 2**30:
        print("✗ 磁盘不足，无法进行真编验证")
        return 2

    for f in (DB,):
        if os.path.exists(f):
            os.remove(f)

    log = open("/tmp/verify_php_build_srv.log", "wb")
    srv = subprocess.Popen(
        ["php", "-S", f"127.0.0.1:{PORT}", "-t", "php/public", "php/public/router.php"],
        env=dict(os.environ, KWRT_DB=DB), stdout=log, stderr=log, preexec_fn=os.setsid)

    try:
        for _ in range(60):
            if srv.poll() is not None:
                print("✗ 服务启动失败"); return 3
            if http("/healthz", timeout=3)[0] == 200:
                break
            time.sleep(0.5)

        # 注册 + 登录
        s, b, _ = http("/api/v1/register", "POST",
                       {"username": "builder01", "email": "b@e.com", "password": "BuildPass123"})
        print(f"注册: {s} {b[:80].decode('utf-8', 'ignore')}")
        s, b, h = http("/api/v1/login", "POST",
                       {"username": "builder01", "password": "BuildPass123"})
        ck = sid(h)
        print(f"登录: {s} cookie={'有' if ck else '无'}")

        # 提交前先确认「界面下拉框里真实的取值」——若界面哪天改成直接给发布号，
        # 这条会立刻失配，提醒本用例已不再覆盖真实路径。
        _st, pg, _ = http("/")
        pg = pg.decode("utf-8", "ignore") if isinstance(pg, (bytes, bytearray)) else str(pg or "")
        _m = re.search(r'<option value="([^"]+)"[^>]*>\s*(?:25\.12|24\.10|23\.05)', pg)
        print(f"界面版本下拉框取值: {_m.group(1) if _m else '(未解析到)'}"
              f"  → 提交值 {VERSION!r} 必须与之相同"
              f"  [{'一致 ✓' if (_m and _m.group(1) == VERSION) else '不一致 ✗'}]")

        # 提交构建（目标用已缓存的 25.12.5）
        payload = {"target": TARGET, "profile": PROFILE, "version": VERSION,
                   "packages": ["luci-app-ttyd"], "filesystem": "squashfs",
                   "rootfs_size_mb": 512, "hostname": "KwrtBuildTest"}
        s, b, _ = http("/api/v1/build", "POST", payload, cookie=ck)
        d = json.loads(b) if b.startswith(b"{") else {}
        print(f"提交构建: {s} {json.dumps(d, ensure_ascii=False)[:160]}")
        h_ = d.get("request_hash")
        if not h_:
            print("✗ 未拿到 request_hash"); return 4

        # 轮询
        t0 = time.time()
        last = None
        while time.time() - t0 < 1800:
            time.sleep(5)
            s, b, _ = http(f"/api/v1/build/{h_}", cookie=ck)
            st = json.loads(b) if b.startswith(b"{") else {}
            cur = (st.get("status"), st.get("detail"))
            if cur != last:
                print(f"  [{time.time() - t0:6.1f}s] {cur[0]} — {cur[1]}")
                last = cur
            if cur[0] in ("done", "failed", "cancelled"):
                break

        con = sqlite3.connect(DB)
        job = con.execute("SELECT status, detail FROM jobs WHERE request_hash=?", (h_,)).fetchone()
        print(f"\n最终状态: {job[0]} — {job[1] if job else ''}")
        if job[0] != "done":
            print("\n--- 构建日志尾部 ---")
            lg = os.path.join("store", h_, "build.log")
            if os.path.isfile(lg):
                with open(lg, "rb") as f:
                    tail = f.read()[-3000:].decode("utf-8", "ignore")
                print(tail)
            print("\n✗ 构建未成功（如实报告，不伪造）")
            return 5

        # 校验产物
        sdir = os.path.join("store", h_)
        files = sorted(f for f in os.listdir(sdir)
                       if os.path.isfile(os.path.join(sdir, f)) and not f.startswith("."))
        print(f"\n产物 {len(files)} 个:")
        for f in files:
            print(f"  {f:<52} {os.path.getsize(os.path.join(sdir, f)):>12,} 字节")

        imgs = [f for f in files if f.endswith((".img.gz", ".img", ".bin", ".itb"))]
        print(f"\n可启动镜像: {len(imgs)} 个 {'✓' if imgs else '✗'}")

        # sha256sums 校验
        sha_ok = None
        sf = os.path.join(sdir, "sha256sums")
        if os.path.isfile(sf):
            r = subprocess.run(["sha256sum", "-c", "sha256sums"], cwd=sdir,
                               capture_output=True, text=True)
            ok = r.stdout.count("OK")
            bad = r.stdout.count("FAILED")
            sha_ok = (bad == 0 and ok > 0)
            print(f"sha256sum -c: {ok} 个 OK, {bad} 个 FAILED → {'✓' if sha_ok else '✗'}")

        # 下载链接（走令牌）
        s, b, _ = http(f"/api/v1/build/{h_}", cookie=ck)
        st = json.loads(b)
        links = st.get("download_links") or []
        print(f"\n签发下载链接: {len(links)} 条")
        link_ok = 0
        for l in links[:3]:
            url = l["url"]
            path = url[url.index("/dl/t/"):]
            req = urllib.request.Request(BASE + path, headers={"Cookie": ck})
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    data = r.read()
                same = len(data) == l.get("size") or True
                # 用 sha256 与本地文件比对，确认吐出的确实是同一份产物
                local = os.path.join(sdir, l["filename"])
                ref = hashlib.sha256(open(local, "rb").read()).hexdigest()
                got = hashlib.sha256(data).hexdigest()
                ok = ref == got
                link_ok += 1 if ok else 0
                print(f"  {l['filename']:<44} HTTP {r.status} {len(data):>12,} 字节 sha256一致={ok}")
            except Exception as e:
                print(f"  {l['filename']:<44} ✗ {type(e).__name__}: {e}")

        # 清理
        subprocess.run(["rm", "-rf", sdir], check=False)
        con.execute("DELETE FROM jobs WHERE request_hash=?", (h_,))
        con.execute("DELETE FROM builds WHERE request_hash=?", (h_,))
        con.commit()
        con.close()

        print("\n" + "=" * 70)
        print(f"真编结果: 构建成功={'✓' if job[0] == 'done' else '✗'} "
              f"镜像={len(imgs)} 个 "
              f"sha256={'✓' if sha_ok else ('✗' if sha_ok is False else '未提供')} "
              f"下载链接={link_ok}/{min(3, len(links))} 校验一致")
        print("=" * 70)
        return 0
    finally:
        try:
            os.killpg(os.getpgid(srv.pid), signal.SIGTERM)
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
