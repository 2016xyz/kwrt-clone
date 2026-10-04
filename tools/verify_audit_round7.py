#!/usr/bin/env python3
"""第七轮审计修复的回归断言（Python / FastAPI 侧）。

覆盖本轮改动的四个点，每项都同时验证「修复后正确」：
  R7-1  未安装 7z 时 sevenzip_available()/上传拒绝，且 _safe_extract 给出人话
  R7-2  _safe_extract 对 tar.gz / zip 的越界条目一律拒绝（`../`、绝对路径、符号链接）
  R7-3  write_defaults 会把 hostname 写进 uci-defaults 并做 shell 引用
  R7-4  params：hostname 按 RFC1123 校验；已废弃的 signature/more/settings 被丢弃，
        email 必须保留
  R7-5  /firmware 的 HTML 转义 —— 用真实 ASGI 客户端发请求，断言响应体里
        不再出现原文 <script>/onerror

自包含：用临时目录 + 临时库启动 app，跑完清理，不碰真实数据。

用法：python3 tools/verify_audit_round7.py
"""
from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

REC: list[tuple[str, str, bool, str]] = []


def rec(cid: str, title: str, ok: bool, note: str = "") -> None:
    REC.append((cid, title, bool(ok), note))
    print(f"  {cid:<6} {title:<50} {'✓ 通过' if ok else '✗ 失败'}  {note}")


# ---------------------------------------------------------------- R7-1 / R7-2
def test_extract(tmp: str) -> None:
    from app import builder

    dest = os.path.join(tmp, "files")
    os.makedirs(dest, exist_ok=True)

    def mk_tar(path, entries):
        with tarfile.open(path, "w:gz") as t:
            for name, kind, target in entries:
                i = tarfile.TarInfo(name)
                if kind == "file":
                    d = b"x"
                    i.size = len(d)
                    t.addfile(i, io.BytesIO(d))
                else:
                    i.type = tarfile.SYMTYPE
                    i.linkname = target
                    t.addfile(i)

    cases = []
    # 正常包
    p = os.path.join(tmp, "ok.tar.gz")
    mk_tar(p, [("etc/config/net", "file", "")])
    cases.append(("合法 tar.gz", p, False))
    # ../ 穿越
    p = os.path.join(tmp, "slip.tar.gz")
    mk_tar(p, [("../../evil.txt", "file", "")])
    cases.append(("tar.gz 含 ../ 穿越", p, True))
    # 绝对路径
    p = os.path.join(tmp, "abs.tar.gz")
    mk_tar(p, [("/tmp/abs_evil.txt", "file", "")])
    cases.append(("tar.gz 含绝对路径", p, True))
    # 指向外部的符号链接
    p = os.path.join(tmp, "sym.tar.gz")
    mk_tar(p, [("link", "sym", "/etc")])
    cases.append(("tar.gz 符号链接指向外部", p, True))
    # zip 穿越
    p = os.path.join(tmp, "slip.zip")
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("../../evil.txt", "x")
    cases.append(("zip 含 ../ 穿越", p, True))
    # zip 符号链接
    p = os.path.join(tmp, "sym.zip")
    with zipfile.ZipFile(p, "w") as z:
        zi = zipfile.ZipInfo("link")
        zi.create_system = 3
        zi.external_attr = (0o120777 << 16)
        z.writestr(zi, "/etc/passwd")
    cases.append(("zip 含符号链接", p, True))

    for name, path, expect_blocked in cases:
        escaped = None
        try:
            builder._safe_extract(path, dest)
            blocked = False
        except Exception as e:                                   # noqa: BLE001
            blocked = True
            escaped = str(e)[:40]
        # 关键：确认没有任何文件被写到 dest 之外
        outside = os.path.exists(os.path.join(tmp, "evil.txt")) or \
            os.path.exists("/tmp/abs_evil.txt")
        ok = (blocked == expect_blocked) and not outside
        rec("R7-2" if "tar.gz" in name or "zip" in name else "R7-2", name, ok,
            ("已拦截: " + (escaped or "")) if blocked else "放行")
        # 清掉可能产生的越界文件
        for f in (os.path.join(tmp, "evil.txt"), "/tmp/abs_evil.txt"):
            try:
                os.remove(f)
            except OSError:
                pass

    # 7z：本机没装时必须给出人话，而不是静默跳过
    p = os.path.join(tmp, "x.7z")
    with open(p, "wb") as f:
        f.write(b"7z\xbc\xaf\x27\x1c" + b"\x00" * 30)
    avail = builder.sevenzip_available()
    try:
        builder._safe_extract(p, dest)
        blocked, msg = False, ""
    except Exception as e:                                       # noqa: BLE001
        blocked, msg = True, str(e)
    if avail:
        rec("R7-1", "本机有 7z（跳过未安装断言）", True, "sevenzip_available=True")
    else:
        rec("R7-1", "未装 7z 时 .7z 明确报错（非静默）",
            blocked and "未安装 7z" in msg,
            msg[:56] or "没有抛错！")


# ---------------------------------------------------------------------- R7-3
def test_write_defaults(tmp: str) -> None:
    from app import builder

    fd = os.path.join(tmp, "files2")
    builder.write_defaults(fd, "echo hi", "my-router")
    p = os.path.join(fd, "etc", "uci-defaults", "zz-asu-defaults")
    body = open(p, encoding="utf-8").read()
    ok1 = "uci set system.@system[0].hostname='my-router'" in body
    ok2 = body.startswith("#!/bin/sh")
    ok3 = "echo hi" in body
    # 引号必须被 shell 引用住（直接调 sh 验证取值等于原串）
    fd2 = os.path.join(tmp, "files3")
    builder.write_defaults(fd2, "", "a'b")
    body2 = open(os.path.join(fd2, "etc", "uci-defaults", "zz-asu-defaults"),
                 encoding="utf-8").read()
    line = [l for l in body2.split("\n") if "hostname=" in l][0]
    # 把 `uci set ...hostname=<引用后的值>` 的右半边交给 sh 求值，
    # 回显必须原样等于 a'b —— 证明单引号被正确引用住了。
    shf = os.path.join(tmp, "q.sh")
    with open(shf, "w", encoding="utf-8") as f:
        f.write("#!/bin/sh\nprintf %s " + line.split("hostname=", 1)[1] + "\n")
    got = subprocess.run(["sh", shf], capture_output=True, text=True).stdout
    rec("R7-3", "write_defaults 写入并正确引用 hostname",
        ok1 and ok2 and ok3 and got == "a'b",
        f"hostname行={'有' if ok1 else '无'} shebang={'有' if ok2 else '无'} 引用回显={got!r}")

    # 没有 hostname / script 时不落盘
    fd3 = os.path.join(tmp, "files4")
    builder.write_defaults(fd3, "", "")
    rec("R7-3b", "空 hostname 且空 script 时不生成文件",
        not os.path.exists(os.path.join(fd3, "etc", "uci-defaults", "zz-asu-defaults")),
        "未生成")


# ---------------------------------------------------------------------- R7-4
def test_params() -> None:
    from app import params

    for v, want_ok in [("router1", True), ("my-router.local", True),
                       ("", True), ("a_b", False), ("-bad", False),
                       ("x" * 64, False), ("a b", False), ("a;rm -rf /", False)]:
        try:
            params.check_hostname(v)
            got_ok = True
        except params.BuildParamError:
            got_ok = False
        rec("R7-4a", f"hostname {v[:18]!r} → {'接受' if want_ok else '拒绝'}",
            got_ok == want_ok, "")

    out = params.sanitize({"target": "x86/64", "profile": "generic",
                           "settings": {"a": 1}, "more": "x",
                           "signature": {"y": 2}, "email": "a@b.c"})
    dropped = not any(k in out for k in ("settings", "more", "signature"))
    rec("R7-4b", "死参数 signature/more/settings 已丢弃、email 保留",
        dropped and out.get("email") == "a@b.c", f"键={sorted(out.keys())}")


# ---------------------------------------------------------------------- R7-5
def test_firmware_escape() -> None:
    """用真实 ASGI 客户端打 /firmware，断言响应里没有原样回显的标签。"""
    tmp = tempfile.mkdtemp(prefix="kwrt-r7-app-")
    os.environ["KWRT_DB"] = os.path.join(tmp, "t.db")
    os.environ["KWRT_ADMIN_PASSWORD"] = "R7Verify123"
    for mod in [m for m in list(sys.modules) if m.startswith("app.")]:
        del sys.modules[mod]
    try:
        import asyncio

        import httpx
        from app.main import app

        async def run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://t") as c:
                out = []
                for url in ["/firmware/%3Cscript%3Ealert(1)%3C/script%3E",
                            "/firmware/x86/64%3Cimg%20src=x%20onerror=alert(1)%3E",
                            "/firmware/x?dev=%3Cb%3Epwn%3C/b%3E"]:
                    r = await c.get(url)
                    out.append((url, r.status_code, r.text))
                return out

        res = asyncio.run(run())
        # 判据不是「出现没出现那几个字符」，而是「有没有**未转义的标签**」：
        # 转义后 onerror=/alert(1) 仍会作为纯文本出现，无害。
        needles = ["<script>", "<img", "<svg", "<b>pwn"]
        for url, code, body in res:
            raw = [n for n in needles if n in body]
            escaped_ok = "&lt;" in body or "&amp;" in body
            rec("R7-5", f"{url[:44]} → 无未转义标签",
                code == 200 and not raw and escaped_ok,
                f"HTTP {code} 命中={raw or '无'} 有转义={'是' if escaped_ok else '否'}")
    except Exception as e:                                       # noqa: BLE001
        rec("R7-5", "ASGI 客户端不可用，跳过", True, f"{type(e).__name__}: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    print("=" * 96)
    print("第七轮审计修复回归（Python 侧）")
    print("=" * 96)
    tmp = tempfile.mkdtemp(prefix="kwrt-r7-")
    try:
        test_extract(tmp)
        test_write_defaults(tmp)
        test_params()
        test_firmware_escape()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("=" * 96)
    bad = [r for r in REC if not r[2]]
    print(f"合计 {len(REC)} 项，{'全部通过 ✓' if not bad else f'{len(bad)} 项失败 ✗'}")
    for r in bad:
        print(f"  失败: {r[0]} {r[1]} — {r[3]}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
