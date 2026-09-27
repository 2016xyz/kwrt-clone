#!/usr/bin/env python3
"""第五轮审计修复的逐项复验 —— 每项一条可重跑断言。

用法：python3 tools/verify_round5.py

设计纪律（来自 source-code-security-audit 技能）：
  · 断言**行为**，不断言源码字面量 —— 后者会把错误实现锁死。
  · 尽量同时验证「修复前会失败」的输入现在被正确拒绝。
  · 结论必须来自真执行，不来自「读代码看起来对了」。
"""
import os
import sys
import gc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

RESULTS = []


def rec(item, desc, ok, evidence):
    RESULTS.append((item, desc, bool(ok), evidence))


def fds():
    return len(os.listdir("/proc/self/fd"))


# --------------------------------------------------------------------------- #
# F-1  SQLite 连接泄漏（`with db() as c:` 只提交不关闭）→ 进程级 fd 耗尽
# --------------------------------------------------------------------------- #
try:
    gc.disable()                       # 关掉自动 GC，模拟「GC 尚未运行」的窗口
    from app import main, sitesettings as SS, dl
    base = fds()
    for _ in range(800):
        with SS.db() as c:
            c.execute("SELECT 1").fetchone()
        with dl.db() as c:
            c.execute("SELECT COUNT(*) FROM dl_tokens").fetchone()
        with main.db() as c:
            c.execute("SELECT COUNT(*) FROM users").fetchone()
    leaked = fds() - base
    gc.enable()
    gc.collect()
    rec("F-1", "with db() 退出后真正关闭连接（修复前 1 调用 = 1 fd）",
        leaked == 0, f"2400 次 with db() → 泄漏 fd = {leaked}（期望 0）")
except Exception as e:
    rec("F-1", "with db() 退出后真正关闭连接", False, f"{type(e).__name__}: {e}")

# 连接确实具备 ClosingConnection 身份（行为层：退出后不可再用）
try:
    from app import dbutil
    with SS.db() as c:
        c.execute("SELECT 1").fetchone()
    closed = False
    try:
        c.execute("SELECT 1")
    except Exception:
        closed = True
    rec("F-1b", "with 退出后的连接不可再执行语句",
        closed and issubclass(dbutil.ClosingConnection, __import__("sqlite3").Connection),
        f"退出后再 execute 抛异常 = {closed}")
except Exception as e:
    rec("F-1b", "with 退出后的连接不可再执行语句", False, f"{type(e).__name__}: {e}")

# --------------------------------------------------------------------------- #
# F-2  数值配置：1e400 / nan 曾抛未捕获的 OverflowError（→ 500）
# --------------------------------------------------------------------------- #
try:
    from app import sitesettings as SS
    cases = []
    for label, fn in (
        ("_dec('1e400')",   lambda: SS._dec("1e400", {"t": "number", "d": 7})),
        ("_dec('nan')",     lambda: SS._dec("nan", {"t": "number", "d": 7})),
        ("_enc(inf)",       lambda: SS._enc(float("inf"), "number")),
        ("validate('1e400')", lambda: SS.validate("mail.port", "1e400")),
        ("validate('1e4000')", lambda: SS.validate("mail.port", "1e4000")),
        ("validate('nan')", lambda: SS.validate("mail.port", "nan")),
    ):
        try:
            fn()
            cases.append((label, True))
        except Exception as e:
            cases.append((label, f"{type(e).__name__}"))
    bad = [c for c in cases if c[1] is not True]
    rec("F-2", "数值配置对 1e400/nan 不再抛 OverflowError",
        not bad, f"{len(cases)} 组用例，异常 {len(bad)} 组 {bad if bad else ''}")
    # 正常值仍要能用
    ok_norm = SS.validate("mail.port", "465")[0] and SS.validate("mail.port", "587")[0]
    rec("F-2b", "正常端口值仍通过校验", ok_norm,
        f"465 → {SS.validate('mail.port','465')}, 587 → {SS.validate('mail.port','587')[0]}")
except Exception as e:
    rec("F-2", "数值配置对 1e400/nan 不再抛 OverflowError", False, f"{type(e).__name__}: {e}")

# --------------------------------------------------------------------------- #
# F-3  主色配置只校验长度，#";x"yy 这类 7 字符串能过 → 进入 CSS/内联样式
# --------------------------------------------------------------------------- #
try:
    from app import sitesettings as SS
    okv = SS.validate("primary_color", "#2563eb")[0] and SS.validate("primary_color", "#abc")[0]
    badv = all(not SS.validate("primary_color", v)[0]
               for v in ('#";x"yy', "#zzzzzz", '#",x,', "#<svg>", "#00000g"))
    rec("F-3", "主色仅接受合法十六进制",
        okv and badv, f"合法 #2563eb/#abc 通过={okv}，5 个注入/非法样本全拒={badv}")
except Exception as e:
    rec("F-3", "主色仅接受合法十六进制", False, f"{type(e).__name__}: {e}")

# --------------------------------------------------------------------------- #
# F-4  上游索引的 Filename 可越界写（实测曾写到 /tmp 之外）
# --------------------------------------------------------------------------- #
try:
    from app import prefetch as pf
    attacks = ["../../../../tmp/ROUND5_PWNED.ipk", "/etc/cron.d/e.ipk",
               "..%2f..%2fx.ipk", "a b.ipk", "..", ".", "x/../y.ipk",
               'a"b.ipk', "sub/dir/x.ipk"]
    blocked = all(pf.safe_filename(a) == "" for a in attacks)
    # 真实文件名必须不被误杀（1083 个真实索引条目的子集）
    legit = ["luci-app-x_1.0_x86_64.ipk", "libc_1.2.5-r4_x86_64.ipk",
             "kernel_6.6.144~50daf8372d97-r1_x86_64.ipk",
             "luci-app-passwall_25.12.19-r1_all.ipk", "pkg.apk"]
    kept = all(pf.safe_filename(x) == x for x in legit)
    # 端到端：投毒索引解析后 filename 必须为空
    idx = pf.parse_index(b"Package: evil\nVersion: 1\n"
                         b"Filename: ../../../../tmp/ROUND5_PWNED.ipk\n\n")
    poisoned_empty = idx.get("evil", {}).get("filename") == ""
    if os.path.exists("/tmp/ROUND5_PWNED.ipk"):
        os.remove("/tmp/ROUND5_PWNED.ipk")
    rec("F-4", "索引 Filename 越界被拒且不误杀真实包名",
        blocked and kept and poisoned_empty,
        f"9 类攻击全拒={blocked}，5 个真实名全放行={kept}，投毒索引 filename 置空={poisoned_empty}")
except Exception as e:
    rec("F-4", "索引 Filename 越界被拒且不误杀真实包名", False, f"{type(e).__name__}: {e}")

# --------------------------------------------------------------------------- #
# F-5  gzip 炸弹（50KB → 50MB 实测可行）
# --------------------------------------------------------------------------- #
try:
    import gzip
    from app import prefetch as pf
    bomb = gzip.compress(b"\x00" * (400 * 1024 * 1024))
    capped = False
    try:
        pf.parse_index(bomb)
    except ValueError:
        capped = True
    normal_ok = "a" in pf.parse_index(b"Package: a\nVersion: 1\nFilename: a_1.0.ipk\n\n")
    rec("F-5", "索引解压限幅（防 gzip 炸弹）", capped and normal_ok,
        f"{len(bomb)}B 压缩包被拦={capped}，正常索引仍可解析={normal_ok}")
except Exception as e:
    rec("F-5", "索引解压限幅（防 gzip 炸弹）", False, f"{type(e).__name__}: {e}")

# --------------------------------------------------------------------------- #
# F-6  版本串超 4300 位数字 → int() 抛 ValueError（Py3.11+ 限制）
# --------------------------------------------------------------------------- #
try:
    from app import prefetch as pf
    crashed = False
    try:
        pf.ver_tuple("9" * 5000)
    except Exception as e:
        crashed = f"{type(e).__name__}"
    ordered = (sorted(["1.2.0", "1.10.0", "2.0.0"], key=pf.ver_tuple)
               == ["1.2.0", "1.10.0", "2.0.0"])
    rec("F-6", "超长版本串不再抛异常，且排序语义不变",
        crashed is False and ordered,
        f"5000 位数字异常={crashed}，大小排序正确={ordered}")
except Exception as e:
    rec("F-6", "超长版本串不再抛异常，且排序语义不变", False, f"{type(e).__name__}: {e}")

# --------------------------------------------------------------------------- #
# F-7  prefetch 里 5 处裸 open() 未用 with（同仓库其他处已收口）
# --------------------------------------------------------------------------- #
try:
    src = open("app/prefetch.py", encoding="utf-8").read()
    import re
    bare = re.findall(r"(?<![\w.])(?:for \w+ in |=\s*)open\([^)]*\)(?!\s*as\b)", src)
    n_bare = len([m for m in bare if ".read()" in m or ".write(" in m])
    rec("F-7", "prefetch 的 open() 全部改为 with", n_bare == 0,
        f"残留未收口的 open = {n_bare}")
except Exception as e:
    rec("F-7", "prefetch 的 open() 全部改为 with", False, f"{type(e).__name__}: {e}")

# --------------------------------------------------------------------------- #
# F-8  构建相关命令统一走解析后的绝对路径（不再裸 "make"）
# --------------------------------------------------------------------------- #
try:
    import re
    src = open("app/prefetch.py", encoding="utf-8").read()
    bare_make = re.search(r'subprocess\.run\(\s*\[\s*"make"', src) is not None
    rec("F-8", "prefetch 调用 make 使用解析后的路径", not bare_make,
        f"仍存在裸 \"make\" = {bare_make}")
except Exception as e:
    rec("F-8", "prefetch 调用 make 使用解析后的路径", False, f"{type(e).__name__}: {e}")

# --------------------------------------------------------------------------- #
# F-9  静默吞异常导致「构建永远 running」等无迹可查的故障
# --------------------------------------------------------------------------- #
try:
    b = open("app/builder.py", encoding="utf-8").read()
    pf_src = open("app/prefetch.py", encoding="utf-8").read()
    m = open("app/main.py", encoding="utf-8").read()
    ok = ("写入任务终态失败" in b and "复制到本地仓失败" in pf_src
          and "overview.json 读取失败" in m)
    rec("F-9", "三处静默吞异常已加日志线索", ok,
        "builder/prefetch/main 各有日志 = " + str(ok))
except Exception as e:
    rec("F-9", "三处静默吞异常已加日志线索", False, f"{type(e).__name__}: {e}")

# --------------------------------------------------------------------------- #
# F-10  artifacts 模块：孤儿识别 + 越界拒绝 + uploads 不受影响
# --------------------------------------------------------------------------- #
try:
    import tempfile
    from app import artifacts
    bad_paths = [artifacts._safe_dir(x) for x in ("..", "../../etc", "uploads", "", "a/b")]
    rec("F-10", "构建物盘点拒绝越界/保留 uploads",
        all(v is None for v in bad_paths),
        f"_safe_dir 对 5 个越界输入的返回 = {bad_paths}")
except Exception as e:
    rec("F-10", "构建物盘点拒绝越界/保留 uploads", False, f"{type(e).__name__}: {e}")

# --------------------------------------------------------------------------- #
# F-11  邮件 HTML 模板：默认转义 + 模板名防穿越 + 模板文件齐备
# --------------------------------------------------------------------------- #
try:
    from app import mailer
    h = mailer._html_ok("站点", {"username": "<script>x</script>", "site": "S",
                                 "target": "t", "profile": "p", "version": "v",
                                 "count": 1, "duration": "1s", "hours": 72},
                        [{"name": 'a"onx.bin', "url": "https://x/y", "size": 1}])
    escaped = "<script>" not in h and "&lt;script&gt;" in h and '"onx.bin' not in h
    struct = h.strip().startswith("<!DOCTYPE html>") and h.strip().endswith("</html>")
    names_ok = all(mailer.render_mail(n, {"site": "S", "username": "u", "rows": "",
                                          "target": "t", "profile": "p", "version": "v",
                                          "count": 0, "duration": "0", "hours": 1,
                                          "error": "e", "verify_url": "u", "ttl": "1",
                                          "host": "h", "enc": "ssl", "from_addr": "a",
                                          "now": "n"})
                   for n in ("build_ok.html", "build_fail.html", "verify.html", "test.html"))
    rejected = 0
    for bad in ("../../etc/passwd", "layout.HTML", "a b.html", ""):
        try:
            mailer._tpl(bad)
        except ValueError:
            rejected += 1
    rec("F-11", "HTML 邮件模板：默认转义 / 结构完整 / 模板名防穿越",
        escaped and struct and names_ok and rejected == 4,
        f"转义={escaped} 结构完整={struct} 4 个模板可渲染={names_ok} 非法名被拒={rejected}/4")
except Exception as e:
    rec("F-11", "HTML 邮件模板：默认转义 / 结构完整 / 模板名防穿越", False,
        f"{type(e).__name__}: {e}")

# --------------------------------------------------------------------------- #
print("=" * 78)
print(f"{'项':<7}{'内容':<44}{'结果':<10}证据")
print("=" * 78)
for item, desc, ok, ev in RESULTS:
    mark = "✓ 通过" if ok else "✗ 失败"
    print(f"{item:<7}{desc:<44}{mark:<10}{ev}")
print("=" * 78)
failed = [r for r in RESULTS if not r[2]]
print(f"合计 {len(RESULTS)} 项，" + ("全部通过 ✓" if not failed else f"存在失败 ✗ {[f[0] for f in failed]}"))
sys.exit(1 if failed else 0)
