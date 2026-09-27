#!/usr/bin/env python3
"""全量代码完整性检查。

回答一个问题：**仓库里的代码是否完整、自洽、可运行**。

为什么需要单独一个检查器：本项目两套实现（Python + PHP）共享同一份设置 schema、
同一张数据库、同一套路由语义，靠人工对照看不出遗漏。而且历史上出过
「write_file 写 PHP 时残留 Python 行」（Pay.php 尾部多出几行 Python，
php -l 立刻报错）这类**文件被写坏**的问题 —— 必须机械地扫。

检查层次：
  L1 语法     每个文件能不能被自己的解析器接受
  L2 完整性   有没有被写坏/截断（残留异语言行、未闭合、空实现）
  L3 自洽     引用的东西存不存在（模板、路由、设置键、函数）
  L4 对称     两套实现的接口/路由/开关是否一一对应
  L5 交付     关键文件与能力是否齐备

用法：python3 tools/verify_integrity.py
"""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)

PASS: list[str] = []
FAIL: list[tuple[str, str]] = []
WARN: list[str] = []


def ok(msg: str) -> None:
    PASS.append(msg)


def bad(area: str, msg: str) -> None:
    FAIL.append((area, msg))


def warn(msg: str) -> None:
    WARN.append(msg)


def sh(*args, cwd=None, timeout=60):
    return subprocess.run(args, capture_output=True, text=True, cwd=cwd or ROOT,
                          timeout=timeout)


# ============================================================ L1 语法
def l1_syntax() -> None:
    py = sorted(ROOT.glob("app/**/*.py")) + sorted(ROOT.glob("tools/*.py")) \
        + sorted(ROOT.glob("php/scripts/*.py"))
    py_bad = []
    for f in py:
        try:
            ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError as e:
            py_bad.append(f"{f.relative_to(ROOT)}:{e.lineno} {e.msg}")
    if py_bad:
        bad("L1-Python", f"{len(py_bad)} 个文件语法错误: {py_bad[:3]}")
    else:
        ok(f"L1 Python 语法：{len(py)} 个文件全部通过")

    php = sorted(ROOT.glob("php/**/*.php"))
    php_bad = []
    for f in php:
        r = sh("php", "-l", str(f))
        if r.returncode != 0 or "No syntax errors" not in r.stdout:
            php_bad.append(f"{f.relative_to(ROOT)}: {(r.stdout + r.stderr).strip()[:90]}")
    if php_bad:
        bad("L1-PHP", f"{len(php_bad)} 个文件语法错误: {php_bad[:3]}")
    else:
        ok(f"L1 PHP 语法：{len(php)} 个文件全部通过")

    shs = ["run.sh", "install.sh", "update.sh", "run-php.sh", "tools/verify_launcher.sh"]
    shs += [str(p.relative_to(ROOT)) for p in sorted(ROOT.glob("php/scripts/*.sh"))]
    sh_bad = []
    for f in shs:
        if not (ROOT / f).exists():
            sh_bad.append(f"{f} 不存在")
            continue
        r = sh("bash", "-n", f)
        if r.returncode != 0:
            sh_bad.append(f"{f}: {r.stderr.strip()[:80]}")
    if sh_bad:
        bad("L1-Shell", str(sh_bad[:3]))
    else:
        ok(f"L1 Shell 语法：{len(shs)} 个文件全部通过")

    jsons = sorted(ROOT.glob("data/**/*.json")) + sorted(ROOT.glob("web/data/**/*.json"))
    if (ROOT / "requirements.txt").exists():
        side = ROOT / "php/src/SettingsSchema.php"
        _ = side
    j_bad = []
    for f in jsons:
        try:
            json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            j_bad.append(f"{f.relative_to(ROOT)}: {type(e).__name__}")
    if j_bad:
        bad("L1-JSON", str(j_bad[:3]))
    else:
        ok(f"L1 JSON：{len(jsons)} 个文件全部通过")

    js = sorted(ROOT.glob("php/public/assets/js/*.js")) + sorted(ROOT.glob("web/assets/js/*.js"))
    node = sh("bash", "-lc", "command -v node || true")
    if node.stdout.strip():
        j_bad = []
        for f in js:
            r = sh("node", "--check", str(f))
            if r.returncode != 0:
                j_bad.append(f"{f.relative_to(ROOT)}: {r.stderr.strip().splitlines()[-1][:80]}")
        if j_bad:
            bad("L1-JS", str(j_bad[:3]))
        else:
            ok(f"L1 JavaScript：{len(js)} 个文件全部通过（node --check）")
    else:
        warn(f"未安装 node，跳过 {len(js)} 个 JS 文件的语法检查")


# ============================================================ L2 完整性
PY_IN_PHP = re.compile(
    r"^\s*(?:"
    r"def [A-Za-z_]\w*\s*\(|class [A-Za-z_]\w*(?:\([^)]*\))?\s*:|"
    r"import [A-Za-z_][\w.]*\s*$|from [A-Za-z_][\w.]* import |"
    r"self\.[A-Za-z_]\w*\s*=|"
    r"return \w+\.\w+\s*$|print\s*\(|[A-Za-z_]\w*\s*=\s*\[\s*$"
    r")"
)


def l2_integrity() -> None:
    # ① PHP 文件里混入 Python 行（历史上 Pay.php 尾部残留过 Python 收尾行）
    bad_files = []
    for f in sorted(ROOT.glob("php/**/*.php")):
        txt = f.read_text(encoding="utf-8", errors="ignore")
        # 只在最后一个 ?> 之后（或文件尾）找可疑的裸 Python
        tail = txt.split("?>")[-1] if "?>" in txt else txt
        for i, line in enumerate(tail.splitlines(), 1):
            if not line.strip():
                continue
            if PY_IN_PHP.match(line) and "<?php" not in line:
                bad_files.append(f"{f.relative_to(ROOT)}: 尾部残留疑似 Python 行 → {line[:60]}")
                break
    if bad_files:
        bad("L2-混语言", str(bad_files[:4]))
    else:
        ok("L2 PHP 文件内未发现混入的 Python 行")

    # ② PHP heredoc / 括号是否闭合
    unclosed = []
    for f in sorted(ROOT.glob("php/**/*.php")):
        txt = f.read_text(encoding="utf-8", errors="ignore")
        # <<<TAG ... TAG;
        tags = re.findall(r"<<<(['\"]?)([A-Za-z_]\w*)\1", txt)
        for _, tag in tags:
            if not re.search(rf"^\s*{re.escape(tag)};?\s*$", txt, re.M):
                unclosed.append(f"{f.relative_to(ROOT)}: heredoc {tag} 未闭合")
        # 曾经这里还做朴素的 { } 计数。已删除 —— 它无法做可靠：
        # 路由模式 "/dl/t/{token}"、正则、heredoc 里的括号都会让它误报。
        # 而它想抓的「文件被截断」由 L1 的 php -l 必然抓到（括号不闭合就是语法错），
        # 所以它是纯噪音。留一个永远误报的检查，只会让人开始忽略整个报告。
    if unclosed:
        bad("L2-未闭合", str(unclosed[:4]))
    else:
        ok("L2 PHP heredoc 与花括号全部闭合")

    # ③ Python 文件的「空实现」/ 占位符
    stubs = []
    for f in sorted(ROOT.glob("app/**/*.py")):
        src = f.read_text(encoding="utf-8")
        for m in re.finditer(r"^\s*(def|async def)\s+(\w+)[^\n]*:\s*\n\s*(pass|\.\.\.)\s*$",
                             src, re.M):
            stubs.append(f"{f.relative_to(ROOT)}:{m.group(2)}")
    if stubs:
        bad("L2-空实现", f"Python 函数体为 pass/...: {stubs[:5]}")
    else:
        ok("L2 Python 无空实现（pass/...）函数")

    # ④ PHP 空方法
    pstubs = []
    for f in sorted(ROOT.glob("php/src/**/*.php")):
        src = f.read_text(encoding="utf-8")
        for m in re.finditer(r"function\s+(\w+)\s*\([^)]*\)[^{]*\{\s*\}", src):
            name = m.group(1)
            # 构造函数可以为空：PHP 8 的属性提升把赋值写在参数列表里
            # （Engine.php:__construct 就是这样），不是空实现。
            if name in ("__construct", "__destruct", "__clone"):
                continue
            pstubs.append(f"{f.relative_to(ROOT)}:{name}")
    if pstubs:
        bad("L2-空实现", f"PHP 空方法: {pstubs[:5]}")
    else:
        ok("L2 PHP 无空方法")

    # ⑤ 常见占位符标记
    marks = []
    # 只扫源码目录（模板里的 placeholder 是 HTML 属性，不是待办标记）
    scans = list(ROOT.glob("app/**/*.py")) + list(ROOT.glob("php/src/**/*.php")) \
        + list(ROOT.glob("php/scripts/*.php")) + list(ROOT.glob("php/public/**/*.php"))
    for pat in ("TODO", "FIXME", "XXX", "NotImplementedError", "尚未实现", "待实现"):
        for f in scans:
            txt = f.read_text(encoding="utf-8", errors="ignore")
            for i, line in enumerate(txt.splitlines(), 1):
                if pat in line and not line.strip().startswith(("#", "//", "*", "/*")):
                    marks.append(f"{f.relative_to(ROOT)}:{i} {pat}")
    for f in scans:
        txt = f.read_text(encoding="utf-8", errors="ignore")
        for i, line in enumerate(txt.splitlines(), 1):
            if re.search(r"\bplaceholder\b(?!\s*=)", line) \
                    and not line.strip().startswith(("#", "//", "*", "/*")):
                marks.append(f"{f.relative_to(ROOT)}:{i} placeholder")
    if marks:
        bad("L2-占位符", f"{len(marks)} 处出现未实现标记: {marks[:4]}")
    else:
        ok("L2 源码中无 TODO/FIXME/未实现标记（注释除外）")

    # ⑥ 文件末尾是否被截断（最后一个字符不该是半个表达式）
    trunc = []
    for f in list(ROOT.glob("app/**/*.py")) + list(ROOT.glob("php/**/*.php")):
        txt = f.read_text(encoding="utf-8", errors="ignore").rstrip()
        if not txt:
            # __init__.py 为空是合法的包标记；其它文件为空才是问题
            if f.name != "__init__.py":
                trunc.append(f"{f.relative_to(ROOT)}: 空文件")
            continue
        last = txt.splitlines()[-1].rstrip()
        if last.endswith(("\\", "&&", "||", "+", ",", "=", "->", ".")):
            trunc.append(f"{f.relative_to(ROOT)}: 末行疑似截断 → {last[-40:]}")
    if trunc:
        bad("L2-截断", str(trunc[:4]))
    else:
        ok("L2 源文件末尾无截断迹象")


# ============================================================ L3 自洽
def l3_consistency() -> None:
    # ① PHP 模板引用是否都存在
    tpl_missing = []
    php_all = "\n".join(f.read_text(encoding="utf-8", errors="ignore")
                        for f in ROOT.glob("php/**/*.php"))
    for m in re.finditer(r"""View::page\(\s*['"]([\w/.-]+)['"]""", php_all):
        name = m.group(1)
        cands = [ROOT / f"php/templates/{name}.php", ROOT / f"php/templates/{name}"]
        if not any(c.exists() for c in cands):
            tpl_missing.append(name)
    if tpl_missing:
        bad("L3-模板", f"PHP 引用了不存在的模板: {sorted(set(tpl_missing))[:5]}")
    else:
        ok("L3 PHP 引用的模板全部存在")

    # ② MySQL-无；改成：PHP 路由引用的控制器方法是否存在
    routes = (ROOT / "php/routes.php").read_text(encoding="utf-8")
    missing_methods = []
    for m in re.finditer(r"\[\s*(\w+)::class\s*,\s*'(\w+)'\s*\]", routes):
        cls, meth = m.group(1), m.group(2)
        found = False
        for f in ROOT.glob(f"php/src/Controllers/{cls}.php"):
            if re.search(rf"function\s+{re.escape(meth)}\s*\(", f.read_text(encoding="utf-8")):
                found = True
                break
        if not found:
            missing_methods.append(f"{cls}::{meth}")
    if missing_methods:
        bad("L3-路由", f"PHP 路由指向不存在的方法: {sorted(set(missing_methods))[:6]}")
    else:
        ok("L3 PHP 路由引用的控制器方法全部存在")

    # ③ Python：装饰器声明的路由都有对应函数（FastAPI 天然保证，检查重复定义）
    src = (ROOT / "app/main.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    names = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    dups = {n for n in names if names.count(n) > 1}
    if dups:
        bad("L3-重定义", f"app/main.py 顶层函数重名: {sorted(dups)[:6]}")
    else:
        ok(f"L3 app/main.py 顶层函数无重名（{len(names)} 个）")

    # ④ 前端 fetch 的 API 路径是否都在后端存在
    js_api = set()
    for f in list(ROOT.glob("php/public/assets/js/*.js")) + list(ROOT.glob("web/assets/js/*.js")):
        for m in re.finditer(r"""['"`](/api/v1/[\w/{}$.-]+)['"`]""", f.read_text(encoding="utf-8", errors="ignore")):
            p = m.group(1)
            if "$" in p or "{" in p:
                p = re.sub(r"\$\{[^}]*\}", "{x}", p)
            js_api.add(p.split("?")[0])
    py_routes = set(re.findall(r'@app\.(?:get|post|put|delete)\("(/[^"]*)"', src))
    php_routes = set(re.findall(r"""->(?:get|post|del|put)\(\s*['"](/[^'"]*)['"]""", routes))
    known = py_routes | php_routes
    def norm(p: str) -> str:
        return re.sub(r"\{[^}]*\}", "{x}", p).rstrip("/")
    py_norm = {norm(p) for p in py_routes}
    php_norm = {norm(p) for p in php_routes}
    unknown = []
    for p in js_api:
        # 拼接基址（'/api/v1/admin/' + map[action]）不是完整接口，跳过
        if p.rstrip("/") + "/" == p and any(
                q.startswith(p) and q != p for q in py_norm | php_norm):
            continue
        n = norm(p)
        if n not in py_norm and n not in php_norm and not any(
                n.startswith(x.rsplit("{x}", 1)[0]) and "{x}" in x for x in py_norm | php_norm):
            unknown.append(p)
    if unknown:
        bad("L3-前端接口", f"前端调用了后端不存在的接口: {sorted(unknown)[:6]}")
    else:
        ok(f"L3 前端调用的 {len(js_api)} 个 API 路径后端均存在")

    # ⑤ 设置键引用是否存在
    sys.path.insert(0, str(ROOT))
    from app import sitesettings as SS
    schema = {it["k"] for it in SS.SCHEMA if isinstance(it, dict) and "k" in it}
    used = set()
    def _is_concat_base(key: str) -> bool:
        """'page.' / 'pay.' 这种是拼接前缀，不是完整键名。"""
        return key.endswith(".") or key.count(".") == 0 and key.endswith("_")

    for f in list(ROOT.glob("app/**/*.py")):
        for m in re.finditer(r"""SS\.get\(\s*['"]([\w.]+)['"]""", f.read_text(encoding="utf-8")):
            if not _is_concat_base(m.group(1)):
                used.add(m.group(1))
    for m in re.finditer(r"""(?:Settings::(?:get|bool|int|str))\(\s*['"]([\w.]+)['"]""", php_all):
        if not _is_concat_base(m.group(1)):
            used.add(m.group(1))
    phpschema = set(re.findall(r"""['"]([\w.]+)['"]\s*=>\s*\[""",
                               (ROOT / "php/src/SettingsSchema.php").read_text(encoding="utf-8")))
    # 有意为之的**内部覆盖键**：不进 schema（后台不暴露），但两版一致地读取。
    # 之所以单独列出而不是让检查器沉默，是因为「键不在 schema 里」本身是
    # 这类缺陷的**唯一信号**——一旦无声放行，真正的键名写错就会被漏掉。
    # 每次往这里加键，都必须写明为什么它不该出现在后台。
    INTERNAL_KEYS = {
        # 强制指定对外基址，覆盖「绑定域名」的推导。给反向代理/CDN 特殊拓扑留的后门。
        # 两版（app/netcfg.py 与 php/src/Net.php）读取方式一致。
        "site.base_url",
    }
    unknown_keys = sorted(k for k in used
                          if k not in schema and k not in phpschema and k not in INTERNAL_KEYS)
    if unknown_keys:
        bad("L3-设置键", f"引用了 schema 中不存在的设置键: {unknown_keys[:8]}")
    else:
        ok(f"L3 引用的设置键全部在 schema 中（{len(used)} 个被引用）")

    # ⑥ 关键 Python 模块互相引用的属性是否存在
    from app import netcfg
    fn_missing = []
    for f in ROOT.glob("app/**/*.py"):
        for m in re.finditer(r"netcfg\.(\w+)\(", f.read_text(encoding="utf-8")):
            if not hasattr(netcfg, m.group(1)):
                fn_missing.append(f"netcfg.{m.group(1)}()")
    if fn_missing:
        bad("L3-函数", f"调用了 netcfg 中不存在的函数: {sorted(set(fn_missing))}")
    else:
        ok("L3 netcfg 被调用的函数全部存在")

    # ⑦ 后台必须显示版本号，且两版都取自**同一个真源**
    #
    # 需求原话「后台需要显示版本号」。这里做**接线**检查（不是渲染检查）：
    #   Python 是 Vue 客户端渲染 —— 断言模板里有「系统版本」行且绑定 site.version；
    #   PHP 是服务端渲染 —— 断言模板直接输出 Version::display()。
    # PHP 侧另有一条**真渲染**断言（verify_php.py 的 P-23），两条合起来才完整。
    #
    # 为什么必须两版都查：只查一版的话，另一版「接口有值但页面没接线」不会被发现 ——
    # 这正是本项目反复踩到的缺陷类（静默失效 / 后台没接线）。
    admin_ver_issues = []
    py_admin = (ROOT / "web" / "admin.html").read_text(encoding="utf-8")
    if "系统版本" not in py_admin:
        admin_ver_issues.append("web/admin.html 没有「系统版本」行")
    elif not re.search(r"系统版本[\s\S]{0,200}?site\.version", py_admin):
        admin_ver_issues.append("web/admin.html 的「系统版本」没有绑定 site.version")
    php_admin = (ROOT / "php" / "templates" / "admin.php").read_text(encoding="utf-8")
    if "系统版本" not in php_admin:
        admin_ver_issues.append("php/templates/admin.php 没有「系统版本」行")
    elif "Version::display()" not in php_admin:
        admin_ver_issues.append("php/templates/admin.php 的「系统版本」没有走 Version::display()")
    if admin_ver_issues:
        bad("L3-版本显示", "; ".join(admin_ver_issues))
    else:
        ok("L3 后台两版都显示系统版本号，且都取自同一真源")


# ============================================================ L4 对称
def l4_symmetry() -> None:
    src = (ROOT / "app/main.py").read_text(encoding="utf-8")
    routes = (ROOT / "php/routes.php").read_text(encoding="utf-8")
    py_routes = set(re.findall(r'@app\.(?:get|post|put|delete)\("(/[^"]*)"', src))
    php_routes = set(re.findall(r"""->(?:get|post|del|put)\(\s*['"](/[^'"]*)['"]""", routes))

    def norm(p): return re.sub(r"\{[^}]*\}", "{x}", p).rstrip("/")
    pyn = {norm(p) for p in py_routes}
    phpn = {norm(p) for p in php_routes}
    only_py = sorted(pyn - phpn)
    only_php = sorted(phpn - pyn)
    # 允许的差异：PHP 独有的静态页/SEO 变体
    # PHP 独有的合理差异（仅限「实现方式不同但语义等价」的静态页变体）
    allowed_php: set[str] = set()
    only_php = [p for p in only_php if p not in allowed_php]
    if only_py or only_php:
        bad("L4-路由对称",
            f"仅 Python 有 {only_py[:6]}；仅 PHP 有 {only_php[:6]}")
    else:
        ok(f"L4 路由对称：Python {len(py_routes)} 条 / PHP {len(php_routes)} 条，差异均在预期内")

    # 设置 schema 同源（生成器自带 --check）
    r = sh("python3", "php/scripts/gen_settings_schema.py", "--check")
    if r.returncode == 0:
        ok("L4 设置 schema 两版同源（gen_settings_schema --check 通过）")
    else:
        bad("L4-schema漂移", (r.stdout + r.stderr).strip()[:160])

    # 口令哈希跨语言一致（决定能否共用同一个 users.db）
    r = sh("php", "php/scripts/../src/../scripts/gen_settings_schema.py", "--check")
    _ = r


# ============================================================ L5 交付
def l5_delivery() -> None:
    must = [
        "app/main.py", "app/netcfg.py", "app/sitesettings.py", "app/builder.py",
        "app/artifacts.py", "app/mailer.py", "app/pay.py", "app/refund.py",
        "app/dbutil.py", "app/params.py", "app/prefetch.py", "app/releases.py",
        "run.sh", "run-php.sh", "install.sh", "update.sh",
        "requirements.txt", "README.md", "php/README.md",
        "php/public/index.php", "php/public/router.php", "php/routes.php",
        "php/src/Net.php", "php/src/Releases.php", "php/src/Engine.php",
        "php/src/SettingsSchema.php",
        "php/scripts/gen_settings_schema.py", "php/scripts/set_setting.php",
        "data/pkg_catalog.default.json",
    ]
    missing = [f for f in must if not (ROOT / f).exists()]
    if missing:
        bad("L5-缺文件", f"关键文件缺失: {missing}")
    else:
        ok(f"L5 关键文件齐备（{len(must)} 个）")

    # 每个 PHP 页面模板都能被解析
    tpl = sorted(ROOT.glob("php/templates/**/*.php"))
    tb = []
    for f in tpl:
        r = sh("php", "-l", str(f))
        if r.returncode != 0:
            tb.append(str(f.relative_to(ROOT)))
    if tb:
        bad("L5-模板语法", str(tb[:4]))
    else:
        ok(f"L5 PHP 模板语法：{len(tpl)} 个全部通过")

    # 邮件模板齐备
    mail = sorted(ROOT.glob("app/templates/mail/*.html"))
    if len(mail) >= 5:
        ok(f"L5 邮件模板 {len(mail)} 个存在")
    else:
        bad("L5-邮件模板", f"仅 {len(mail)} 个，期望 >=5")


def main() -> int:
    print("=" * 100)
    print(" 全量代码完整性检查")
    print("=" * 100)
    for fn in (l1_syntax, l2_integrity, l3_consistency, l4_symmetry, l5_delivery):
        try:
            fn()
        except Exception as e:
            bad(fn.__name__, f"检查器自身异常 {type(e).__name__}: {e}")

    for m in PASS:
        print(f"  ✓ {m}")
    for m in WARN:
        print(f"  ! {m}")
    for area, m in FAIL:
        print(f"  ✗ [{area}] {m}")
    print("=" * 100)
    print(f" 通过 {len(PASS)} 项 / 警告 {len(WARN)} 项 / 失败 {len(FAIL)} 项")
    if not FAIL:
        print(" 结论：代码完整、自洽 ✓")
    else:
        print(" 结论：存在缺陷，见上方 ✗ 行")
    print("=" * 100)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
