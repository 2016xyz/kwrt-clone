#!/usr/bin/env python3
"""「干净部署」验证 —— 只按 requirements.txt 装，能不能真的跑起来。

## 为什么需要这个

线上出过一次 500：

    ModuleNotFoundError: No module named 'PIL'   ← app/pay.py:385 qr_png_bytes

根因不是「少装了一个包」，而是**代码用的依赖没有写进 requirements.txt**
（注释还明确写着「不需要 Pillow」，把错误坐实了）。开发机上恰好装着 Pillow，
所以永远发现不了；只有按 requirements 装出来的干净环境才炸。

静态扫描 import 只能防一半：`python-multipart`、`jinja2` 这类**不被代码按名导入**
却是 FastAPI 运行时必需的包，静态扫描看不见。
所以这里做的是**决定性验证**：建一个全新的 venv，只装 requirements.txt，
然后把 app/ 下每个模块都 import 一遍，再把关键功能真跑一次。

## 断言

D-1  静态：app/ 里每个第三方顶层模块都在 requirements.txt 中声明
D-2  **干净 venv 可导入 app/ 下全部模块**（决定性）
D-3  干净 venv 里 qr_png_bytes() / qr_svg_data_uri() 都能跑（线上 500 的那两个）
D-4  干净 venv 里确实没有 Pillow —— 证明上面两条不是靠「碰巧装了」通过的
D-5  负向对照：静态检测器对「构造出的未声明依赖」必须报错

## 用法
    python3 tools/verify_declared_deps.py            # 用当前解释器
    KWRT_PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple python3 tools/verify_declared_deps.py
"""
from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REC: list[tuple[str, str, bool, str]] = []
INDEX = os.environ.get("KWRT_PIP_INDEX",
                       "https://pypi.tuna.tsinghua.edu.cn/simple")


def rec(cid: str, title: str, ok: bool, note: str = "") -> None:
    REC.append((cid, title, bool(ok), note))
    print(f"  {cid:<6} {title:<56} {'✓ 通过' if ok else '✗ 失败'}  {note}")


# --------------------------------------------------------------------------- #
def declared(requirements: Path) -> set[str]:
    """从 requirements.txt 抽出声明的顶层包名（去掉版本约束与注释）。"""
    out = set()
    for ln in requirements.read_text(encoding="utf-8").splitlines():
        ln = ln.split("#", 1)[0].strip()
        if not ln or ln.startswith("-"):
            continue
        name = re.split(r"[<>=!~\[; ]", ln, maxsplit=1)[0].strip().lower()
        if name:
            out.add(name)
    return out


def imported_top_level(root: Path) -> dict[str, set[str]]:
    """扫描第三方顶层 import（跳过标准库与包内相对导入）。"""
    stdlib = set(sys.stdlib_module_names) | {"app", "tools", "__future__"}
    third: dict[str, set[str]] = {}
    for p in sorted(root.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for n in ast.walk(tree):
            mods: list[str] = []
            if isinstance(n, ast.Import):
                mods = [a.name.split(".")[0] for a in n.names]
            elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
                mods = [n.module.split(".")[0]]
            for m in mods:
                if m in stdlib or not m:
                    continue
                third.setdefault(m, set()).add(
                    f"{p.relative_to(ROOT)}:{getattr(n, 'lineno', '?')}")
    return third


# --------------------------------------------------------------------------- #
CLEAN_PROBE = r'''
import importlib, pathlib, sys, traceback
sys.path.insert(0, ".")
bad = []
for p in sorted(pathlib.Path("app").rglob("*.py")):
    if p.name == "__init__.py":
        continue
    mod = ".".join(p.with_suffix("").parts)
    try:
        importlib.import_module(mod)
    except Exception as e:
        bad.append(f"{mod}: {type(e).__name__}: {e}")
print("IMPORT_FAILS=%d" % len(bad))
for b in bad:
    print("  " + b)

import app.pay as pay
try:
    png = pay.qr_png_bytes("https://qr.alipay.com/probe", size=320)
    print("QR_PNG_BYTES=%d" % len(png))
except Exception as e:
    print("QR_PNG_FAIL=%s: %s" % (type(e).__name__, e))
try:
    uri = pay.qr_svg_data_uri("https://qr.alipay.com/probe")
    print("QR_SVG_LEN=%d" % len(uri))
except Exception as e:
    print("QR_SVG_FAIL=%s: %s" % (type(e).__name__, e))

try:
    import PIL  # noqa: F401
    print("HAS_PIL=1")
except ImportError:
    print("HAS_PIL=0")
'''


def main() -> int:
    print("=" * 110)
    print(" 干净部署验证 —— 只装 requirements.txt，app/ 全量导入 + 关键功能实跑")
    print("=" * 110)

    req = ROOT / "requirements.txt"
    dec = declared(req)
    imports = imported_top_level(ROOT / "app")

    # ---------- D-1 静态声明检查 ----------
    # 名字归一：requirements 用短横（python-multipart），import 用下划线（multipart）
    norm = {n.replace("-", "_") for n in dec}
    missing = [m for m in imports if m.replace("-", "_") not in norm]
    rec("D-1", "app/ 里每个第三方 import 都在 requirements.txt 中声明",
        not missing,
        f"{len(imports)} 个第三方模块全部已声明" if not missing
        else f"未声明：{missing}")

    # ---------- D-5 负向对照 ----------
    fake = dict(imports)
    fake["totally_undeclared_pkg"] = {"app/x.py:1"}
    fake_missing = [m for m in fake if m.replace("-", "_") not in norm]
    rec("D-5", "负向对照：静态检测器对未声明依赖必须报错",
        "totally_undeclared_pkg" in fake_missing,
        f"命中 {fake_missing[:3]}")

    # ---------- D-2/D-3/D-4 干净 venv ----------
    tmp = Path(tempfile.mkdtemp(prefix="kwrt-clean-"))
    venv = tmp / "venv"
    try:
        print(f"  ·  建干净 venv 并只装 requirements.txt（索引 {INDEX}）…")
        r = subprocess.run([sys.executable, "-m", "venv", str(venv)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            rec("D-2", "干净 venv 可导入 app/ 全部模块", False,
                f"建 venv 失败：{r.stderr[-120:]}")
            rec("D-3", "干净环境里二维码生成可用", False, "见 D-2")
            rec("D-4", "干净环境确实没有 Pillow", False, "见 D-2")
        else:
            py = venv / "bin" / "python"
            rr = subprocess.run(
                [str(py), "-m", "pip", "install", "-q", "--no-cache-dir",
                 "--disable-pip-version-check", "-i", INDEX, "-r", str(req)],
                capture_output=True, text=True, timeout=900)
            if rr.returncode != 0:
                rec("D-2", "干净 venv 可导入 app/ 全部模块", False,
                    f"pip install 失败：{rr.stderr[-160:]}")
                rec("D-3", "干净环境里二维码生成可用", False, "见 D-2")
                rec("D-4", "干净环境确实没有 Pillow", False, "见 D-2")
            else:
                pf = tmp / "_probe.py"
                pf.write_text(CLEAN_PROBE, encoding="utf-8")
                # 干净 venv 里没有 app/，把仓库根作为 cwd + sys.path
                pr = subprocess.run([str(py), str(pf)], cwd=ROOT,
                                    capture_output=True, text=True, timeout=300)
                out = pr.stdout

                def grab(k: str) -> str:
                    m = re.search(rf"^{k}=(\S+)$", out, re.M)
                    return m.group(1) if m else ""

                nfail = grab("IMPORT_FAILS")
                rec("D-2", "干净 venv 可导入 app/ 全部模块",
                    nfail == "0", f"失败 {nfail} 个" if nfail else out[-160:] or "输出为空")

                png_n = grab("QR_PNG_BYTES")
                svg_n = grab("QR_SVG_LEN")
                png_fail = grab("QR_PNG_FAIL")
                svg_fail = grab("QR_SVG_FAIL")
                rec("D-3", "干净环境里二维码生成可用（线上 500 的那两个函数）",
                    bool(png_n) and bool(svg_n),
                    f"PNG {png_n or png_fail} 字节 / SVG {svg_n or svg_fail} 字符")

                rec("D-4", "干净环境确实没有 Pillow（证明 D-2/D-3 不是碰巧）",
                    grab("HAS_PIL") == "0",
                    "无 Pillow" if grab("HAS_PIL") == "0" else "有 Pillow —— 前提不成立")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("=" * 110)
    bad = [r for r in REC if not r[2]]
    print(f" 合计 {len(REC)} 项，" + ("全部通过 ✓" if not bad else f"失败 ✗ {[b[0] for b in bad]}"))
    print("=" * 110)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
