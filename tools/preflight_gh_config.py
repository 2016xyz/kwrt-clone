#!/usr/bin/env python3
"""GitHub Actions 触发配置的**只读预检** —— 不派发、不消耗 Actions 分钟。

## 为什么需要它

`tools/verify_gh_backend.py` 会真实触发一次构建来验链路（几十秒 + 消耗
Actions 分钟）。多数时候你只想确认「参数填对了没」，不必真的跑一次。

这个脚本把派发会踩的坑**静态**排掉，一次 HTTP 请求都不派发：

  1. token 是否存在、是否有效、能否读到目标仓库
  2. owner/repo 形态是否为 `owner/repo`
  3. ref（分支）是否存在 —— 写错时派发报 422
  4. workflow 文件是否存在且 state=active
  5. **输入契约**：workflow 声明的 inputs ⊇ 程序实际派发的键
     （不一致时 dispatch 返回 204 看着完全正常，run 跑起来才失败）
  6. 产物名是否匹配 gh.artifact_pattern
     （不匹配的症状是「构建成功但没有产物」）
  7. build_default_version 是否落在 workflow 接受的取值里

## 用法

    # 用仓库里 users.db 的配置
    .venv/bin/python tools/preflight_gh_config.py

    # 指定数据库（例如线上库的副本）
    .venv/bin/python tools/preflight_gh_config.py --db /path/to/users.db
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OK: list[str] = []
WARN: list[str] = []
BAD: list[str] = []


def rec(ok, title, note=""):
    (OK if ok else BAD).append(title)
    print(f"  {'✓' if ok else '✗'} {title:<46} {note}")


def warn(title, note=""):
    WARN.append(title)
    print(f"  ! {title:<46} {note}")


def workflow_inputs(yml: str) -> set:
    """抽出 workflow_dispatch 声明的 inputs 名（靠缩进，不依赖 PyYAML）。

    ★ 必须只收**输入名那一层**的键。写成「比 inputs: 更深就收」的话，
      每个输入下面的 description / required / default 也会被当输入名 ——
      危害不是多收名字，而是契约检查被放宽：某个真缺的输入名若在别处
      作为普通字符串出现过，就会被误判成「已声明」而漏过。
    """
    lines = yml.splitlines()
    start = next((i for i, l in enumerate(lines)
                  if l.strip() == "workflow_dispatch:"), None)
    if start is None:
        return set()
    inputs_ind = name_ind = None
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
            break
        if name_ind is None:
            name_ind = ind
        if ind == name_ind:
            out.add(l.strip().split(":")[0])
    return out


def dispatched_inputs() -> set:
    """程序派发时实际发送的 inputs 键（从 app/backends.py 里抽）。"""
    src = (ROOT / "app/backends.py").read_text(encoding="utf-8")
    m = re.search(r"inputs = \{(.*?)\n        \}", src, re.S)
    return set(re.findall(r'"([A-Za-z_][A-Za-z0-9_]*)":', m.group(1))) if m else set()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "users.db"))
    a = ap.parse_args()

    if not Path(a.db).exists():
        print(f"  ! 数据库不存在：{a.db}")
        return 2

    c = sqlite3.connect(a.db)

    def G(k, d=""):
        r = c.execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone()
        return (r[0] if r else d) or d

    tok = G("gh.token").strip()
    repo = G("gh.repo").strip()
    wf = G("gh.workflow", "build-firmware.yml").strip()
    ref = G("gh.ref", "main").strip()
    pat = G("gh.artifact_pattern", "openwrt-*").strip()
    enabled = G("gh.enabled", "false")
    backend = G("builder.backend", "local")
    bdef = G("build_default_version", "25.12")
    c.close()

    print("=" * 104)
    print(" GitHub 触发配置 —— 只读预检（不派发）")
    print("=" * 104)
    print(f"  db                  = {a.db}")
    print(f"  gh.enabled          = {enabled}")
    print(f"  gh.repo             = {repo}")
    print(f"  gh.workflow         = {wf}")
    print(f"  gh.ref              = {ref}")
    print(f"  gh.token            = len={len(tok)} prefix={tok[:4] if tok else '(空)'}")
    print(f"  gh.artifact_pattern = {pat}")
    print(f"  builder.backend     = {backend}")
    print(f"  build_default_ver   = {bdef}")
    print()

    def api(path):
        r = urllib.request.Request(
            "https://api.github.com" + path,
            headers={"Authorization": "token " + tok, "User-Agent": "kwrt-preflight",
                     "Accept": "application/vnd.github+json"})
        try:
            with urllib.request.urlopen(r, timeout=30) as x:
                return x.status, json.loads(x.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")
        except Exception as e:                                    # noqa: BLE001
            return 0, {"err": repr(e)[:120]}

    rec(enabled.lower() == "true", "gh.enabled 已开启", f"值={enabled}")
    rec(backend == "github", "builder.backend = github", f"值={backend}")
    rec(repo.count("/") == 1, "gh.repo 形态为 owner/repo", repo or "(空)")
    rec(bool(tok), "gh.token 已填写", f"len={len(tok)}")
    if not (tok and repo.count("/") == 1):
        print("\n  ！基础项缺失，后续检查无法进行")
        return 1

    st, d = api("/user")
    rec(st == 200, "token 有效（GET /user）", f"HTTP {st} login={d.get('login')}")

    st, d = api(f"/repos/{repo}")
    rec(st == 200, "仓库可访问（owner/repo 正确）",
        f"HTTP {st} private={d.get('private')} default={d.get('default_branch')}")
    default_branch = d.get("default_branch") or "main"

    st, _ = api(f"/repos/{repo}/branches/{ref}")
    rec(st == 200, f"ref 存在（{ref}）", f"HTTP {st}")
    if st == 200 and ref != default_branch:
        warn("ref 与默认分支不同", f"ref={ref} default={default_branch}")

    st, d = api(f"/repos/{repo}/actions/workflows/{wf}")
    rec(st == 200 and d.get("state") == "active",
        f"workflow 存在且 active（{wf}）", f"HTTP {st} state={d.get('state')}")

    st, d2 = api(f"/repos/{repo}/contents/.github/workflows/{wf}")
    yml = base64.b64decode(d2["content"]).decode("utf-8") if st == 200 else ""

    if yml:
        declared, sent = workflow_inputs(yml), dispatched_inputs()
        missing = sorted(sent - declared)
        rec(bool(sent) and not missing, "输入契约一致（声明 ⊇ 派发）",
            f"派发 {sorted(sent)}" + (f" —— 缺 {missing}" if missing else ""))

        an = re.search(r"name:\s*(openwrt-[^\n]*)", yml)
        if an:
            rec(an.group(1).strip().startswith(pat.rstrip("*")),
                f"产物名匹配 gh.artifact_pattern（{pat}）", an.group(1).strip()[:64])
        else:
            warn("未能从 workflow 解析出产物名", "人工复核 upload-artifact 的 name")

        vl = re.search(r"version:\s*\n(?:.*\n)*?\s*default:\s*'([^']*)'", yml)
        if vl:
            vals = re.findall(r"\b\d{2}\.\d{2}(?:\.\d+)?\b", vl.group(0))
            rec(not vals or bdef in vals, "build_default_version 在 workflow 取值内",
                f"站点默认={bdef} workflow 示例={sorted(set(vals))}")
    else:
        warn("未取到 workflow 内容", "无法做契约与产物名检查")

    print("-" * 104)
    print(f" 通过 {len(OK)} 项" + (f" / 警告 {len(WARN)} 项" if WARN else "")
          + (f" / 失败 {len(BAD)} 项 → {BAD}" if BAD else " —— 配置可用 ✓"))
    print("=" * 104)
    return 0 if not BAD else 1


if __name__ == "__main__":
    raise SystemExit(main())
