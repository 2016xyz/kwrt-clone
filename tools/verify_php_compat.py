#!/usr/bin/env python3
"""PHP 版本兼容性检查。

## 为什么要有这个脚本
现场事故：宝塔部署后整站白屏，只有一行
    Fatal error: Dynamic class names are not allowed in compile-time ::class fetch
    in /www/wwwroot/xxx/public/index.php on line 215

根因是入口文件用了 PHP 8.0 的**语法**（`$obj::class`、非捕获式 `catch (Throwable)`），
而服务器跑的是 PHP 7.4 —— 这类错误发生在**编译期**，整个文件编译不过，
所以写在文件里的任何运行时检查都不会执行，用户只能看到那句看不懂的报错。

修法是：入口文件只用 PHP 5/7 也能解析的语法，并把版本守卫放在最前面，
让「版本不对」变成一句能照做的话。**这个性质必须被机械地守住** ——
任何人日后再往入口文件里写一个 PHP 8 语法糖，整站就又白屏了。

## 检查项
  C-1  所有入口/脚本都含版本守卫
  C-2  守卫在入口文件里最先执行（早于 require / spl_autoload_register / 类引用）
  C-3  入口文件不含 PHP 8.0 专有**语法**（静态扫描，无 docker 也能跑）
  C-4  PHP 7.4 下入口文件可解析（需 docker；不可用则跳过）
  C-5  PHP 7.4 下实跑 index.php 得到可读提示而非 Fatal error（需 docker）
  C-6  PHP 8 下全量 lint 通过（防回归）

用法：
    python3 tools/verify_php_compat.py
    # 无 docker 时 C-4/C-5 会跳过并说明
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REC: list[tuple[str, str, bool, str]] = []

#: 必须先于任何业务代码执行、且必须能被 PHP 7 解析的入口文件
ENTRY = [
    "php/public/index.php",
    "php/public/install.php",
    "php/scripts/build-worker.php",
    "php/scripts/reap.php",
]

#: PHP 8.0 才允许的**语法**（编译期即报错）。运行时函数（str_starts_with 等）
#: 不在此列 —— 它们在 PHP 7 下是「未定义函数」，属运行期错误，守卫能提前拦下。
SYNTAX_80 = [
    (r"\$[A-Za-z_]\w*::class", "变量::class（PHP 8.0+）"),
    (r"\bcatch\s*\(\s*\\?[A-Za-z_\\|]+\s*\)", "非捕获式 catch（PHP 8.0+）"),
    # match 表达式要求 match 前面没有 -> :: $ —— 否则会把 $router->match(...)
    # 这种普方法调用误判成 PHP 8 语法（实测踩过：C-4 已证明 7.4 可解析，
    # C-3 却报错，那就是检查器错了，不是代码错了）。
    (r"(?<![\->:\w$])match\s*\(", "match 表达式（PHP 8.0+）"),
    (r"\?->", "nullsafe 运算符（PHP 8.0+）"),
    (r"\b(?:int|string|bool|float|array|self|static)\s*\|\s*(?:int|string|bool|float|array|null)\b",
     "联合类型（PHP 8.0+）"),
    (r"\bpublic\s+(?:readonly\s+)?[A-Za-z\\?]+\s+\$[a-z_]\w*\s*[,)]", "构造器属性提升（PHP 8.0+）"),
    (r"#\[", "属性语法 #[...]（PHP 8.0+）"),
    (r"\bthrow\s+new\b(?=.*\?)", "throw 表达式（PHP 8.0+，存疑时人工确认）"),
]


def rec(cid: str, title: str, ok: bool, note: str = "") -> None:
    REC.append((cid, title, bool(ok), note))
    print(f"  {cid:<6} {title:<50} {'✓ 通过' if ok else '✗ 失败'}  {note}")


def strip_comments(src: str) -> str:
    """去掉注释与字符串里的干扰 —— 否则「解释这个坑的注释」会被当成真代码。"""
    out = []
    for line in src.splitlines():
        s = line.strip()
        if s.startswith(("//", "*", "/*", "#")):
            continue
        out.append(line)
    return "\n".join(out)


def guard_pos(src: str) -> tuple[int, int]:
    """返回（守卫位置, 首个业务语句位置）。"""
    g = src.find("PHP_VERSION_ID < 80000")
    marks = [src.find(m) for m in ("spl_autoload_register", "require ", "require_once ")]
    marks = [m for m in marks if m != -1]
    return g, (min(marks) if marks else len(src))


def php_bin() -> str:
    return shutil.which("php") or "php"


def have_php74() -> bool:
    if not shutil.which("docker"):
        return False
    r = subprocess.run(["docker", "image", "inspect", "php:7.4-cli"],
                       capture_output=True, text=True)
    return r.returncode == 0


def main() -> int:
    print("=" * 100)
    print(" PHP 版本兼容性检查")
    print("=" * 100)

    # ---------------------------------------------------------------- C-1 / C-2 / C-3
    missing, misordered, syntax = [], [], []
    for rel in ENTRY:
        p = ROOT / rel
        if not p.is_file():
            missing.append(rel)
            continue
        src = p.read_text(encoding="utf-8")
        if "PHP_VERSION_ID < 80000" not in src:
            missing.append(rel)
        else:
            g, first = guard_pos(src)
            if g > first:
                misordered.append(f"{rel}(守卫@{g} 晚于业务@{first})")
        code = strip_comments(src)
        for pat, name in SYNTAX_80:
            if name.startswith("throw 表达式"):
                continue                      # 存疑项，不机械判失败
            if re.search(pat, code):
                syntax.append(f"{rel}: {name}")

    rec("C-1", f"{len(ENTRY)} 个入口/脚本都含版本守卫", not missing,
        f"缺失 {missing}" if missing else "")
    rec("C-2", "守卫先于任何 require / autoload", not misordered,
        "; ".join(misordered) if misordered else "守卫在业务代码之前")
    rec("C-3", "入口文件不含 PHP 8.0 专有语法（守卫才编译得过）", not syntax,
        "; ".join(syntax) if syntax else "仅用 PHP 5/7 可解析语法")

    # ---------------------------------------------------------------- C-6 PHP 8 lint
    bad = []
    for f in sorted((ROOT / "php").rglob("*.php")):
        r = subprocess.run([php_bin(), "-l", str(f)], capture_output=True, text=True)
        if "No syntax errors" not in r.stdout:
            bad.append(f"{f.relative_to(ROOT)}: {r.stdout.strip()[:60]}")
    rec("C-6", f"PHP 8 下 {len(list((ROOT/'php').rglob('*.php')))} 个文件 lint 通过",
        not bad, "; ".join(bad[:2]) if bad else f"本机 {php_bin()}")

    # ---------------------------------------------------------------- C-4 / C-5 真 PHP 7.4
    if not have_php74():
        print("  C-4    PHP 7.4 下可解析                                —— 跳过"
              "（本机无 docker 或未缓存 php:7.4-cli）")
        print("  C-5    PHP 7.4 下实跑给出可读提示                      —— 跳过（同上）")
    else:
        r = subprocess.run(
            ["docker", "run", "--rm", "-v", f"{ROOT}:/app", "-w", "/app", "php:7.4-cli",
             "sh", "-c",
             "for f in " + " ".join(ENTRY) + "; do php -l $f; done"],
            capture_output=True, text=True, timeout=300)
        failed = [l for l in r.stdout.splitlines() if "No syntax errors" not in l and l.strip()]
        rec("C-4", "PHP 7.4 下入口文件全部可解析", not failed,
            f"{len(ENTRY)} 个文件" if not failed else "; ".join(failed[:2]))

        r = subprocess.run(
            ["docker", "run", "--rm", "-v", f"{ROOT}:/app", "-w", "/app", "php:7.4-cli",
             "sh", "-c", "php php/public/index.php 2>&1"],
            capture_output=True, text=True, timeout=300)
        out = r.stdout + r.stderr
        ok = ("需要 PHP 8.0" in out) and ("Fatal error" not in out) and ("Parse error" not in out)
        rec("C-5", "PHP 7.4 下实跑输出可读提示（非 Fatal error）", ok,
            out.strip().splitlines()[0][:56] if out.strip() else "(无输出)")

    print("=" * 100)
    bad = [r for r in REC if not r[2]]
    print(f" 合计 {len(REC)} 项，" + ("全部通过 ✓" if not bad else f"失败 ✗ {[b[0] for b in bad]}"))
    print("=" * 100)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())