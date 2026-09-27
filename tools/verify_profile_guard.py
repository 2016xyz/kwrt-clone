#!/usr/bin/env python3
"""设备 profile 预检 + 构建失败原因可见性 —— 回归。

## 背景（实测事故，见 reports/19）

用户报「请稍后重试或更换软件包组合。构建失败」。真实原因是：

    Profile "jdcloud_re-cs-02" does not exist!
    make[1]: *** [Makefile:323: _check_profile] Error 1

站点的设备库来自 **kwrt 数据集**，而构建用的是**上游官方 ImageBuilder**。
两者对不上：实测 997 台设备里 **107 台（10.7%）**上游根本编不出来
（rockchip/armv8 87 台里 49 台，qualcommax/ipq60xx 29 台里 14 台）。

而且这句话还**误导**：它让人以为「重试」或「换软件包」有用，
但这两种做法永远不会成功 —— 换设备才有用。

顺带发现第二个缺陷：后端把失败原因写在 `stderr`，而前端模板读的是 `job.error`，
**`job.error` 在整个前端从未被赋值** → 真实原因被丢掉，用户永远只看到那句兜底文案。

## 断言

  PG-1  上游编不出的设备 → 提交被**提前拒绝**（400 / PROFILE_UNSUPPORTED）
  PG-2  上游能编的设备 → 正常受理（证明不是「一律拒绝」）
  PG-3  **失败放行**：profiles.json 取不到时必须放行（否则镜像站抖动就全站不能构建）
  PG-4  拒绝文案必须可行动，且**不再**出现「请稍后重试」这类误导话术
  PG-5  前端失败块必须引用 stderr（否则真实原因又被丢掉）
  PG-6  接线守卫：main.py 的 build_post 必须真的调用预检函数

## 怎么做到「不发 CI、不跑重构建」

临时实例的 `imagebuilder_mirrors` 指向**本套件自建的假镜像站**：
  · 25.12.5 的 profiles.json → 返回真实上游数据（预检正常工作）
  · 其它版本 → 404（用来制造「取不到」→ 验 PG-3 的失败放行）
  · ImageBuilder tarball 一律 404（万一有构建漏过去，也只会立刻失败）

`builder.backend` 固定为 `local` —— 全程不碰 GitHub，不消耗 Actions 分钟。

## 用法

    .venv/bin/python tools/verify_profile_guard.py
"""
from __future__ import annotations

import base64
import http.cookiejar
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ADMIN_USER, ADMIN_PASS = "admin", "admin123"
REC: list[tuple[str, str, bool, str]] = []

#: 被测目标：一个上游编不出的设备
BAD_TARGET, BAD_PROFILE = "qualcommax/ipq60xx", "jdcloud_re-cs-02"
#: 对照：一个上游能编的设备
GOOD_TARGET, GOOD_PROFILE = "x86/64", "generic"
RELEASE = "25.12.5"


def rec(cid, title, ok, note=""):
    REC.append((cid, title, bool(ok), note))
    print(f"  {cid:<5} {title:<50} {'✓ 通过' if ok else '✗ 失败'}  {note}")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def fetch_upstream(target: str) -> str | None:
    url = f"https://downloads.openwrt.org/releases/{RELEASE}/targets/{target}/profiles.json"
    try:
        with urllib.request.urlopen(url, timeout=45) as r:
            return r.read().decode("utf-8")
    except Exception as e:                                        # noqa: BLE001
        print(f"  ! 取不到 {url}: {repr(e)[:80]}")
        return None


def verif() -> str:
    j = json.dumps({"uuid": "12345678-1234-4000-8000-123456789012"})
    return base64.b64encode(bytes(ord(c) ^ 80 for c in j)).decode()


def make_mirror(routes: dict) -> tuple[ThreadingHTTPServer, int]:
    class H(BaseHTTPRequestHandler):
        def do_GET(self):                                          # noqa: N802
            body = routes.get(self.path)
            if body is None:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            b = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def log_message(self, format, *args):                      # noqa: A002
            pass

    port = free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


def main() -> int:                                                 # noqa: C901
    print("=" * 108)
    print(" 设备 profile 预检 —— 回归（假镜像站；不发 CI、不跑重构建）")
    print("=" * 108)

    prof_bad = fetch_upstream(BAD_TARGET)
    prof_good = fetch_upstream(GOOD_TARGET)
    if not prof_bad or not prof_good:
        print("  ! 取不到上游 profiles.json（需要外网）—— 无法进行")
        return 2

    routes = {f"/{RELEASE}/targets/{BAD_TARGET}/profiles.json": prof_bad,
              f"/{RELEASE}/targets/{GOOD_TARGET}/profiles.json": prof_good}
    mirror, mport = make_mirror(routes)
    # ★ 镜像模板必须带 {version}/{target} 占位符 —— 这正是 app 的约定
    #   （config.json 里三个真实镜像都长这样）。第一版我写成了裸地址
    #   http://127.0.0.1:PORT/，于是 program 拼出来的请求变成 /profiles.json
    #   → 404 → 预检失败放行，把「预检没生效」误判成了产品问题。
    MIRROR_TPL = f"http://127.0.0.1:{mport}/{{version}}/targets/{{target}}/"
    print(f"  假镜像站模板: {MIRROR_TPL}")
    print(f"    （只服务 {RELEASE} 的 profiles.json，其余 404）\n")

    app_port = free_port()
    base = f"http://127.0.0.1:{app_port}"
    tmp = Path(tempfile.mkdtemp(prefix="kwrt-pg-"))
    srv = None
    try:
        for sub in ("app", "web", "data"):
            shutil.copytree(ROOT / sub, tmp / sub,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copy2(ROOT / "VERSION", tmp / "VERSION")
        shutil.copy2(ROOT / "users.db", tmp / "users.db")

        cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        cfg["builder"]["imagebuilder_mirrors"] = [MIRROR_TPL]
        (tmp / "config.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")

        c = sqlite3.connect(tmp / "users.db")
        for k, v in (("builder.enabled", "true"), ("builder.backend", "local"),
                     ("builder.max_concurrent", "1"), ("gh.enabled", "false"),
                     ("build_allow_anonymous", "true"), ("build_default_version", "25.12")):
            c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (k, v))
        c.commit()
        c.close()

        srv = subprocess.Popen(
            [str(ROOT / ".venv" / "bin" / "python"), "-m", "uvicorn", "app.main:app",
             "--host", "127.0.0.1", "--port", str(app_port), "--log-level", "warning",
             "--forwarded-allow-ips", ""],
            cwd=tmp, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            preexec_fn=os.setsid)
        up = False
        for _ in range(80):
            try:
                with urllib.request.urlopen(base + "/healthz", timeout=3) as r:
                    if r.status == 200:
                        up = True
                        break
            except Exception:                                      # noqa: BLE001
                time.sleep(0.5)
        if not up:
            print("  ! 服务未起来：")
            print("   ", (srv.stdout.read() or "")[-600:] if srv.stdout else "")
            return 2

        cj = http.cookiejar.CookieJar()
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
        frm = urllib.parse.urlencode({"username": ADMIN_USER, "password": ADMIN_PASS}).encode()
        rq = urllib.request.Request(base + "/api/v1/login", data=frm, method="POST",
                                    headers={"Content-Type": "application/x-www-form-urlencoded"})
        with op.open(rq, timeout=30) as r:
            login_st = r.status

        def submit(target, profile, version):
            body = json.dumps({"target": target, "profile": profile, "packages": [],
                               "version": version, "filesystem": "squashfs",
                               "rootfs_size_mb": 512}).encode()
            r = urllib.request.Request(
                base + "/api/v1/build", data=body, method="POST",
                headers={"Content-Type": "application/json",
                         "Ng-One-Time-Verif-Value": verif()})
            try:
                with op.open(r, timeout=60) as resp:
                    return resp.status, json.loads(resp.read() or b"{}")
            except urllib.error.HTTPError as e:
                try:
                    return e.code, json.loads(e.read() or b"{}")
                except Exception:                                  # noqa: BLE001
                    return e.code, {}

        # ---------------- PG-1 编不出的设备必须被提前拒绝
        st, d = submit(BAD_TARGET, BAD_PROFILE, "25.12")
        detail = str(d.get("detail") or "")
        rec("PG-1", f"上游编不出的设备被提前拒绝（{BAD_PROFILE}）",
            st == 400 and d.get("error_code") == "PROFILE_UNSUPPORTED",
            f"HTTP {st} code={d.get('error_code')}")
        rec("PG-1b", "拒绝时给出可行动的原因（含平台名与可编数量）",
            ("ImageBuilder" in detail and BAD_TARGET.split("/")[0] in detail
             and "台" in detail),
            detail[:96])

        # ---------------- PG-4 文案不得再误导
        bad_words = [w for w in ("请稍后重试", "更换软件包组合", "稍后重试") if w in detail]
        rec("PG-4", "拒绝文案不再出现误导性话术",
            not bad_words, f"命中 {bad_words}" if bad_words else "无「请稍后重试」等字样")

        # ---------------- PG-2 可编的设备必须正常受理
        st2, d2 = submit(GOOD_TARGET, GOOD_PROFILE, "25.12")
        rec("PG-2", f"上游能编的设备正常受理（{GOOD_TARGET}/{GOOD_PROFILE}）",
            st2 in (200, 202) and bool(d2.get("request_hash")),
            f"HTTP {st2} hash={str(d2.get('request_hash'))[:16]}")

        # ---------------- PG-3 失败放行：profiles.json 取不到时必须放行
        #      用 24.10 让版本解析到 24.10.8 —— 假镜像站对该版本一律 404，
        #      于是 imagebuilder_profiles() 返回 None，预检应放行。
        st3, d3 = submit(BAD_TARGET, BAD_PROFILE, "24.10")
        rec("PG-3", "profiles.json 取不到时放行（不因镜像站抖动阻断构建）",
            st3 in (200, 202) and d3.get("error_code") != "PROFILE_UNSUPPORTED",
            f"HTTP {st3} code={d3.get('error_code')}（旧版本镜像不可达 → 放行）")

        # ---------------- PG-7 排队中再提交不得 500（_pump 的 UnboundLocalError）
        #      并发=1 且上一个任务还在跑时再提交，原来会因 job 未绑定直接 500。
        #      线上 builder.max_concurrent 正是 1 —— 等于「别人在编时你别点」。
        st7, d7 = submit(GOOD_TARGET, GOOD_PROFILE, "25.12")
        rec("PG-7", "并发已满时再提交不再 500（_pump 的 UnboundLocalError）",
            st7 != 500 and st7 in (200, 202),
            f"HTTP {st7}（期望 202；500 即回归）")

        # ---------------- PG-5 / PG-6 接线守卫（静态）
        idx = (ROOT / "web/index.html").read_text(encoding="utf-8")
        m = re.search(r"job\.status === 'failed'.*?</template>", idx, re.S)
        block = m.group(0) if m else ""
        # 注释里为了说明历史问题会引用旧文案，判断前先把 HTML 注释剥掉
        block_code = re.sub(r"<!--.*?-->", "", block, flags=re.S)
        rec("PG-5", "前端失败块引用 stderr（真实原因不会被丢掉）",
            "job.stderr" in block_code and "请稍后重试或更换软件包组合" not in block_code,
            "引用 job.stderr ✓" if "job.stderr" in block_code else "仍在读未赋值的 job.error")

        mainpy = (ROOT / "app/main.py").read_text(encoding="utf-8")
        bp = mainpy[mainpy.find("async def build_post"):]
        bp = bp[:bp.find("\n@app.")] if "\n@app." in bp else bp
        rec("PG-6", "build_post 真的调用了 profile 预检（接线守卫）",
            "imagebuilder_profiles(" in bp and "PROFILE_UNSUPPORTED" in bp,
            "已接线 ✓" if "imagebuilder_profiles(" in bp else "预检未接入提交路径")

    finally:
        try:
            mirror.shutdown()
        except Exception:                                          # noqa: BLE001
            pass
        if srv is not None:
            try:
                os.killpg(os.getpgid(srv.pid), 15)
                srv.wait(timeout=10)
            except Exception:                                      # noqa: BLE001
                pass
        shutil.rmtree(tmp, ignore_errors=True)

    print("-" * 108)
    bad = [c for c, _, ok, _ in REC if not ok]
    print(f" 合计 {len(REC)} 项，{'全部通过 ✓' if not bad else '失败 ✗ ' + str(bad)}")
    print("=" * 108)
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
