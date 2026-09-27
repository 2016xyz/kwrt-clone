#!/usr/bin/env python3
"""GitHub Actions 构建后端的端到端联调（真实派发，非 mock）。

## 为什么需要它

「配置填好了」和「构建真的能跑」是两件事。填完之后有五种典型翻车方式，
只有**真派发一次**才能全部抓到：

  1. token 权限不够            → 派发返回 403/404
  2. repo / workflow / ref 写错 → 404
  3. **workflow 的 inputs 名与程序派发的键不一致** → 派发成功但 run 立刻失败
  4. 产物名不匹配 gh.artifact_pattern → 站点找不到固件，构建「成功但没产物」
  5. 私有仓库的产物下载需要 token → 回传失败，用户拿到死链

第 3 条尤其阴 —— dispatch 返回 204 看着完全正常，错误要等 run 跑完才暴露。

## 用哪个 workflow

仓库里有两个：

  · `build-firmware.yml`        真实构建，产出上百 MB 固件
  · `build-firmware-verify.yml` **专为联调设计**：输入契约完全一致，
                                但只产小体积产物，几十秒跑完

本脚本默认用后者。理由：本脚本验的是**站点与 GitHub 之间的链路**，
不是 ImageBuilder 能不能编出固件 —— 用真构建会把联调成本放大几百倍
（跨国下载上百 MB，且每次消耗大量 Actions 分钟）。

## 用法

    # 用仓库里的配置（users.db 里的 gh.*）
    .venv/bin/python tools/verify_gh_backend.py

    # 指定仓库 / token / workflow
    KWRT_GH_REPO=owner/repo KWRT_GH_TOKEN=ghp_xxx \
      .venv/bin/python tools/verify_gh_backend.py

    # 跑真实构建（慢，消耗 Actions 分钟）
    KWRT_GH_WORKFLOW=build-firmware.yml .venv/bin/python tools/verify_gh_backend.py

★ 会真实触发一次 GitHub Actions 运行并消耗少量 Actions 分钟。
★ 全程在**临时目录**里起一份独立实例，不改动仓库的 users.db，也不碰线上。
"""
from __future__ import annotations

import base64
import http.cookiejar
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("KWRT_GH_PORT", "8295"))
BASE = f"http://127.0.0.1:{PORT}"
ADMIN_USER, ADMIN_PASS = "admin", "admin123"
POLL_MAX = int(os.environ.get("KWRT_GH_POLL_MAX", "600"))     # 秒

REC: list[tuple[str, str, bool, str]] = []


def rec(cid, title, ok, note=""):
    REC.append((cid, title, bool(ok), note))
    print(f"  {cid:<5} {title:<50} {'✓ 通过' if ok else '✗ 失败'}  {note}")


def verif() -> str:
    """POST /api/v1/build 要求的一次性校验头（与 tools/e2e_test.py 同一算法）。"""
    j = json.dumps({"uuid": "12345678-1234-4000-8000-123456789012"})
    return base64.b64encode(bytes(ord(c) ^ 80 for c in j)).decode()


def gh_api(token: str, path: str):
    req = urllib.request.Request(
        "https://api.github.com" + path,
        headers={"Authorization": "token " + token, "User-Agent": "kwrt-gh-verify",
                 "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")
    except Exception as e:                                        # noqa: BLE001
        return 0, {"err": repr(e)[:160]}


# --------------------------------------------------------------------------- #
def workflow_inputs(yml: str) -> set:
    """从 workflow YAML 文本里抽出 workflow_dispatch 声明的 inputs 名。

    不依赖 PyYAML（这个项目刻意不引入无关依赖）：靠缩进定位。

    ★ 缩进必须严格——只收**输入名那一层**的键。第一版写成了
      「比 inputs: 更深就收」，于是把每个输入下面的 description / required /
      default 也都当成了输入名。危害不是多收几个名字，而是**契约检查被放宽**：
      某个输入名若只在别处出现过，就会被误判成「已声明」，
      于是「workflow 不认这个参数」这类问题从检查里漏过去。
    """
    lines = yml.splitlines()
    start = next((i for i, l in enumerate(lines)
                  if l.strip() == "workflow_dispatch:"), None)
    if start is None:
        return set()
    inputs_ind = None      # `inputs:` 自身的缩进
    name_ind = None        # 输入名的缩进（inputs 的子层）
    out: set[str] = set()
    for l in lines[start + 1:]:
        if not l.strip() or l.lstrip().startswith("#"):
            continue
        ind = len(l) - len(l.lstrip())
        if l.strip() == "inputs:":
            inputs_ind = ind
            continue
        if inputs_ind is None:
            continue
        if ind <= inputs_ind:
            break                       # 已离开 inputs 块
        if name_ind is None:
            name_ind = ind              # 第一个键的缩进即输入名缩进
        if ind == name_ind:
            out.add(l.strip().split(":")[0])
    return out


def dispatched_inputs() -> set:
    """从 app/backends.py 里抽出程序派发时实际发送的 inputs 键。"""
    src = (ROOT / "app/backends.py").read_text(encoding="utf-8")
    m = re.search(r"inputs = \{(.*?)\n        \}", src, re.S)
    if not m:
        return set()
    return set(re.findall(r'"([A-Za-z_][A-Za-z0-9_]*)":', m.group(1)))


# --------------------------------------------------------------------------- #
def main() -> int:                                                # noqa: C901
    print("=" * 112)
    print(" GitHub Actions 构建后端 —— 端到端联调（真实派发）")
    print("=" * 112)

    repo = os.environ.get("KWRT_GH_REPO", "").strip()
    token = os.environ.get("KWRT_GH_TOKEN", "").strip()
    workflow = os.environ.get("KWRT_GH_WORKFLOW", "build-firmware-verify.yml").strip()

    if not token:
        tdb = ROOT / "users.db"
        if not tdb.exists():
            print("  ! 仓库里没有 users.db，请用 KWRT_GH_TOKEN=... 指定 token")
            return 2
        c = sqlite3.connect(tdb)
        token = (c.execute("SELECT value FROM settings WHERE key='gh.token'").fetchone() or [""])[0]
        if not repo:
            repo = (c.execute("SELECT value FROM settings WHERE key='gh.repo'").fetchone() or [""])[0]
        c.close()
        token, repo = token.strip(), repo.strip()

    if not token or not repo:
        print(f"  ! 缺 token 或 repo（token={'有' if token else '无'} repo={repo or '空'}）")
        print("    用 KWRT_GH_REPO=owner/repo KWRT_GH_TOKEN=ghp_xxx 指定")
        return 2
    if "/" not in repo:
        print(f"  ! repo 形态不对：{repo!r} —— 应为 owner/repo")
        return 2

    print(f"\n  repo     : {repo}")
    print(f"  workflow : {workflow}")
    print(f"  token    : 前缀={token[:4]} 长度={len(token)}")

    # ---- G-0 token 与仓库可达性
    st, d = gh_api(token, "/user")
    who = d.get("login") if st == 200 else None
    rec("G-0", "token 有效且能读到身份",
        st == 200, f"HTTP {st} login={who or d}")

    st, d = gh_api(token, f"/repos/{repo}")
    rec("G-0b", "目标仓库可访问",
        st == 200, f"HTTP {st} private={d.get('private')} default={d.get('default_branch')}")
    default_branch = d.get("default_branch") or "main"

    st, d = gh_api(token, f"/repos/{repo}/actions/workflows/{workflow}")
    wf_found = st == 200
    rec("G-0c", f"workflow 存在且启用（{workflow}）",
        wf_found, f"HTTP {st}" + (f" state={d.get('state')}" if wf_found else ""))

    # ---- G-1 输入契约：workflow 声明的 inputs 必须覆盖程序派发的键
    #      这条是**静态**检查，不需要跑一次构建，却能抓住最阴的那类翻车。
    if wf_found:
        st2, d2 = gh_api(token, f"/repos/{repo}/contents/.github/workflows/{workflow}")
        yml = base64.b64decode(d2["content"]).decode("utf-8") if st2 == 200 else ""
    else:
        yml = ""
    if yml:
        declared, sent = workflow_inputs(yml), dispatched_inputs()
        missing = sorted(sent - declared)
        rec("G-1", "输入契约一致（workflow 声明的 inputs ⊇ 程序派发的键）",
            bool(sent) and not missing,
            f"程序发 {len(sent)} 个 / workflow 声明 {len(declared)} 个"
            + (f"；缺 {missing}" if missing else ""))

    # ---- 起一份临时实例（独立 DB，不碰仓库 users.db）
    tmp = Path(tempfile.mkdtemp(prefix="kwrt-ghv-"))
    srv = None
    try:
        for sub in ("app", "web", "data"):
            shutil.copytree(ROOT / sub, tmp / sub,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for f in ("config.json", "VERSION"):
            if (ROOT / f).exists():
                shutil.copy2(ROOT / f, tmp / f)
        shutil.copy2(ROOT / "users.db", tmp / "users.db")

        c = sqlite3.connect(tmp / "users.db")
        for k, v in (("gh.enabled", "true"), ("gh.repo", repo), ("gh.token", token),
                     ("gh.workflow", workflow), ("gh.ref", default_branch),
                     ("gh.queues", "[]"), ("gh.artifact_pattern", "openwrt-*"),
                     ("gh.mirror_artifacts", "true"), ("gh.mirror_timeout", "1800"),
                     ("builder.enabled", "true"), ("builder.backend", "github"),
                     ("builder.max_concurrent", "1")):
            c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (k, v))
        c.commit()
        c.close()

        srv = subprocess.Popen(
            [str(ROOT / ".venv" / "bin" / "python"), "-m", "uvicorn", "app.main:app",
             "--host", "127.0.0.1", "--port", str(PORT), "--log-level", "warning",
             "--forwarded-allow-ips", ""],
            cwd=tmp, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            preexec_fn=os.setsid)
        up = False
        for _ in range(80):
            try:
                with urllib.request.urlopen(BASE + "/healthz", timeout=3) as r:
                    if r.status == 200:
                        up = True
                        break
            except Exception:                                     # noqa: BLE001
                time.sleep(0.5)
        if not up:
            print("  ! 服务未起来：")
            print("   ", (srv.stdout.read() or "")[-700:] if srv.stdout else "")
            return 2

        cj = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

        def call(method, path, body=None, headers=None,
                 timeout=120) -> "tuple[int, dict]":
            h = dict(headers or {})
            data = None
            if body is not None:
                data = json.dumps(body).encode()
                h["Content-Type"] = "application/json"
            r = urllib.request.Request(BASE + path, data=data, headers=h, method=method)
            try:
                with opener.open(r, timeout=timeout) as resp:
                    return resp.status, json.loads(resp.read() or b"{}")
            except urllib.error.HTTPError as e:
                try:
                    return e.code, json.loads(e.read() or b"{}")
                except Exception:                                 # noqa: BLE001
                    return e.code, {}
            except Exception as e:                                # noqa: BLE001
                return 0, {"err": repr(e)[:160]}

        # 登录（管理员）—— 走表单编码，不能用上面的 json 分支
        frm = urllib.parse.urlencode({"username": ADMIN_USER,
                                      "password": ADMIN_PASS}).encode()
        rq = urllib.request.Request(BASE + "/api/v1/login", data=frm, method="POST",
                                    headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with opener.open(rq, timeout=30) as resp:
                st = resp.status
        except urllib.error.HTTPError as e:
            st = e.code
        rec("G-2", "管理员登录成功", st == 200, f"HTTP {st}")

        # ---- G-3 backend_info 报表
        st, info = call("GET", "/api/v1/admin/site")
        bi = (info or {}).get("backend_info") or {}
        rec("G-3", "后端报表：configured=github 且 github_available=true",
            bi.get("configured") == "github" and bi.get("github_available") is True
            and bi.get("effective") == "github",
            f"configured={bi.get('configured')} available={bi.get('github_available')} "
            f"effective={bi.get('effective')} queues={bi.get('queue_count')}")

        # ---- G-4 真实派发
        st, sub = call("POST", "/api/v1/build",
                       {"target": "x86/64", "profile": "generic", "packages": [],
                        "version": "25.12", "filesystem": "squashfs",
                        "rootfs_size_mb": 512},
                       headers={"Ng-One-Time-Verif-Value": verif()}, timeout=60)
        jid = (sub or {}).get("request_hash")
        rec("G-4", "构建任务已受理（POST /api/v1/build → 202）",
            st in (200, 202) and bool(jid), f"HTTP {st} hash={jid or sub}")

        # ---- G-5 轮询到终态
        job, waited, seen = {}, 0, []
        if jid:
            t0 = time.time()
            while time.time() - t0 < POLL_MAX:
                _, job = call("GET", f"/api/v1/build/{jid}", timeout=60)
                s = job.get("status")
                if not seen or seen[-1] != s:
                    seen.append(s)
                    print(f"        [{int(time.time()-t0):3d}s] status={s} "
                          f"detail={str(job.get('detail') or job.get('imagebuilder_status') or '')[:60]}")
                if s in ("done", "failed", "error", "cancelled"):
                    break
                time.sleep(5)
            waited = int(time.time() - t0)

        status = job.get("status")
        run_url = job.get("gh_run_url")
        rec("G-5", "构建跑到终态且成功（status=done）",
            status == "done",
            f"status={status} 用时={waited}s 状态链={'→'.join(str(x) for x in seen)}")
        rec("G-5b", "拿到 GitHub run 链接（证明真的派发到了 Actions）",
            bool(run_url), str(run_url or job.get("stderr") or job.get("detail") or "无")[:100])

        # ---- G-6 产物回传（私有仓库的产物要带 token 下载，最易翻车）
        files = job.get("files") or []
        rec("G-6", "产物已回传到本站（mirror）",
            bool(files), f"{len(files)} 个：" + ", ".join(str(f.get("name")) for f in files[:3]))

        # ---- G-7 下载链接真的能下
        links = job.get("download_links") or []
        one_ok, note = False, "无链接"
        if links:
            u = links[0].get("url") or ""
            try:
                with urllib.request.urlopen(u, timeout=120) as r:
                    n = len(r.read(4096))
                    one_ok = r.status == 200 and n > 0
                    note = f"HTTP {r.status} 首块 {n}B"
            except Exception as e:                                # noqa: BLE001
                note = repr(e)[:120]
        rec("G-7", "下载链接可访问且返回真实字节",
            one_ok, f"{note}（共 {len(links)} 条）")

        # ---- G-8 负向对照：关掉 gh.enabled 后必须回落 local
        call("POST", "/api/v1/admin/site", {"gh.enabled": False})
        _, info2 = call("GET", "/api/v1/admin/site")
        bi2 = (info2 or {}).get("backend_info") or {}
        rec("G-8", "负向对照：gh.enabled=false → 回落 local（开关真的生效）",
            bi2.get("github_available") is False and bi2.get("effective") == "local",
            f"available={bi2.get('github_available')} effective={bi2.get('effective')}")

    finally:
        if srv is not None:
            try:
                os.killpg(os.getpgid(srv.pid), 15)
                srv.wait(timeout=10)
            except Exception:                                     # noqa: BLE001
                pass
        shutil.rmtree(tmp, ignore_errors=True)

    print("-" * 112)
    npass = sum(1 for _, _, ok, _ in REC if ok)
    bad = [c for c, _, ok, _ in REC if not ok]
    print(f" 合计 {len(REC)} 项，{'全部通过 ✓' if not bad else '失败 ✗ ' + str(bad)}")
    print("=" * 112)
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
