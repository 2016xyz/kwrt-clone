#!/usr/bin/env python3
"""部署布局兼容性验证 —— 覆盖宝塔上真实会出现的几种摆放方式。

为什么要有这个：入口原先硬编码 `dirname(__DIR__, 2)`，只在「仓库原样布局」下成立。
宝塔上更常见的做法是把 `php/public` 的内容直接放到站点根 —— 这时它会退到仓库根
的上一级，用户看到的是
    Fatal error: Failed opening required '/www/wwwroot/php/src/helpers.php'
一句 PHP 内部错误，既没说清布局要求也没说怎么改（真实现场）。

本脚本把 4 种真实布局各搭一遍真跑，并验证「完全找不到代码」时给的是人话而不是 fatal。

用法：python3 tools/verify_layout.py
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REC: list[tuple[str, str, bool, str]] = []
# work/ 里可能有上次真实构建留下的 ImageBuilder 树（含断链符号链接，copytree 会炸），
# cache/ 里有几十 MB 的 ImageBuilder 压缩包 —— 都不需要复制。
IGNORE = shutil.ignore_patterns(".git", "users.db", "users.db-*", "config.local.json",
                                "install.lock", "__pycache__", ".ekko-tmp",
                                "work", "cache", "store", "*.tar.zst")


def rec(cid, title, ok, note=""):
    REC.append((cid, title, bool(ok), note))
    print(f"  {cid:<6} {title:<54} {'✓ 通过' if ok else '✗ 失败'}  {note}")


def fetch(url, timeout=10):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore")
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


def app_of(base: Path) -> Path:
    """仓库根：build() 一律把代码摆在 <base>/app 下。"""
    return base / "app"


def build(layout: str, base: Path) -> Path:
    """按布局把代码摆好，返回 docroot。"""
    app = base / "app"
    if layout == "A 标准":
        # <R>/php/public + <R>/php/src，docroot=<R>/php/public
        shutil.copytree(ROOT, app, dirs_exist_ok=True, ignore=IGNORE)
        return app / "php/public"
    if layout == "B 扁平(src 并列)":
        # docroot=<R>/public，代码=<R>/src
        for sub in ("public", "src", "templates", "scripts"):
            shutil.copytree(ROOT / "php" / sub, app / sub, dirs_exist_ok=True, ignore=IGNORE)
        shutil.copy2(ROOT / "php/routes.php", app / "routes.php")
        for f in ("config.json", "VERSION"):
            shutil.copy2(ROOT / f, app / f)
        (app / "data").mkdir(exist_ok=True)
        return app / "public"
    if layout == "C 扁平(php 保留)":
        # docroot=<R>/public，代码=<R>/php/src
        shutil.copytree(ROOT / "php/public", app / "public", dirs_exist_ok=True, ignore=IGNORE)
        for sub in ("src", "templates", "scripts"):
            shutil.copytree(ROOT / "php" / sub, app / "php" / sub, dirs_exist_ok=True, ignore=IGNORE)
        shutil.copy2(ROOT / "php/routes.php", app / "php/routes.php")
        for f in ("config.json", "VERSION"):
            shutil.copy2(ROOT / f, app / f)
        (app / "data").mkdir(exist_ok=True)
        return app / "public"
    if layout == "D 全平铺":
        # docroot=<R>，代码=<R>/src
        shutil.copytree(ROOT / "php/public", app, dirs_exist_ok=True, ignore=IGNORE)
        for sub in ("src", "templates", "scripts"):
            shutil.copytree(ROOT / "php" / sub, app / sub, dirs_exist_ok=True, ignore=IGNORE)
        shutil.copy2(ROOT / "php/routes.php", app / "routes.php")
        for f in ("config.json", "VERSION"):
            shutil.copy2(ROOT / f, app / f)
        (app / "data").mkdir(exist_ok=True)
        return app
    if layout == "E 缺代码":
        shutil.copytree(ROOT / "php/public", app, dirs_exist_ok=True, ignore=IGNORE)
        return app          # 故意不放 src/
    raise ValueError(layout)


def serve(docroot: Path, port: int):
    p = subprocess.Popen(["php", "-S", f"127.0.0.1:{port}", "-t", str(docroot),
                          str(docroot / "router.php") if (docroot / "router.php").is_file() else ""],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         cwd=str(docroot))
    for _ in range(40):
        time.sleep(0.25)
        if fetch(f"http://127.0.0.1:{port}/healthz", timeout=2)[0] != 0:
            return p
    return p


def main() -> int:
    print("=" * 108)
    print(" 部署布局兼容性验证")
    print("=" * 108)
    tmp = Path(tempfile.mkdtemp(prefix="kwrt-layout-"))
    port = 8130
    try:
        for layout in ("A 标准", "B 扁平(src 并列)", "C 扁平(php 保留)", "D 全平铺"):
            base = tmp / re.sub(r"\W+", "_", layout)
            base.mkdir(parents=True, exist_ok=True)
            docroot = build(layout, base)
            expect_root = str(app_of(base))     # 真实仓库根，由 build() 的摆法决定
            p = serve(docroot, port)
            try:
                s1, b1 = fetch(f"http://127.0.0.1:{port}/healthz")
                s2, b2 = fetch(f"http://127.0.0.1:{port}/install.php")
                s3, b3 = fetch(f"http://127.0.0.1:{port}/")
                fatal = ("Fatal error" in b2) or ("Failed opening required" in b2)
                # ★ 判据必须落在**正文内容**上。
                #   只看状态码会骗人：router.php 跑通时连 _boot.php 都会回一个
                #   空正文的 200，于是「全绿」而应用其实没起来。实测踩过 ——
                #   第一版这里只断言 status==200，4 个布局全「通过」，全是假的。
                ok = (s1 == 200 and b1.strip() != "" and s2 == 200 and s3 == 200
                      and len(b3) > 200 and not fatal)
                # 用临时 PHP 文件探测（php -r 的多层反斜杠转义会静默出错）
                probe = docroot.parent / "_probe.php"
                # 只读常量：原先想调 Kwrt\Config::root()，但探针里没注册 autoloader，
                # 类找不到 → 静默空输出，root_ok 永远 false（假失败）。
                probe.write_text('<?php require __DIR__ . "/' + docroot.name + '/_boot.php";'
                                 'echo KWRT_ROOT . "|" . KWRT_SRC . "|" . KWRT_TPL;',
                                 encoding="utf-8")
                out = subprocess.run(["php", str(probe)], capture_output=True, text=True,
                                     cwd=str(docroot.parent))
                probe.unlink(missing_ok=True)
                parts = (out.stdout or "").split("|")
                # ★ 必须**完全相等**：第一版写的是 startswith(base)，
                #   于是「数据根多偏一层」也能通过 —— 断言太松和没断言一样。
                root_ok = len(parts) == 3 and parts[0] == expect_root
                rec(f"L-{len(REC)+1}", f"{layout}：页面正常渲染且 Config::root 落位正确",
                    ok and root_ok,
                    f"healthz={s1}({len(b1)}B) install={s2} home={len(b3)}B "
                    + ("root 落位正确" if root_ok else
                       f"root={parts[0] if parts else out.stderr[-60:]} 期望={expect_root}"))
            finally:
                p.kill()
                p.wait(timeout=10)
            port += 1

        # 缺代码：必须是**人话**，不是 PHP fatal
        base = tmp / "E"
        base.mkdir(parents=True, exist_ok=True)
        docroot = build("E 缺代码", base)
        p = serve(docroot, port)
        try:
            s, b = fetch(f"http://127.0.0.1:{port}/install.php")
            human = ("找不到项目代码目录" in b) and ("运行目录" in b) and ("php/src" in b)
            nofatal = ("Fatal error" not in b) and ("Failed opening required" not in b)
            rec(f"L-{len(REC)+1}", "缺代码时给可照做的诊断页（而不是 PHP fatal）",
                human and nofatal, f"HTTP {s} 正文{len(b)}B；含布局说明={'是' if human else '否'}")
            # _boot.php 直接访问应 404（必须在服务器还活着时测）
            s6, b6 = fetch(f"http://127.0.0.1:{port}/_boot.php")
            rec(f"L-{len(REC)+1}", "_boot.php 被直接访问时 404（不暴露内部结构）",
                s6 == 404 and "kwrt_locate" not in b6, f"HTTP {s6}")
        finally:
            p.kill()
            p.wait(timeout=10)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("=" * 108)
    bad = [r for r in REC if not r[2]]
    print(f" 合计 {len(REC)} 项，" + ("全部通过 ✓" if not bad else f"失败 ✗ {[b[0] for b in bad]}"))
    print("=" * 108)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
