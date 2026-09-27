#!/usr/bin/env python3
"""开关实现对称性检查：两版共用同一份设置 schema，就不能只在一侧实现。

**为什么需要这个**：本项目里同一个设置 schema 由 `app/sitesettings.py` 生成给
Python 与 PHP 两版。于是「后台能改、前台无效果」成了一种反复出现的缺陷：

  · 域名绑定 / CDN / 页面开关  —— 先只做了 PHP
  · PWA / robots / sitemap     —— 又只做了 PHP

两次都是「假功能」：开关存在、默认还开着，但一侧根本没有实现。
靠人工复查很不可靠，所以这里做两件事：

  1. **行为检查**（强）：逐开关翻转，断言**两版都有可观察的效果**。
     PHP 用一个独立实例跑；Python 打当前服务。
  2. **引用检查**（弱，兜底）：schema 里的开关键至少要在两侧源码中出现。
     这条是 tripwire —— 用来抓「两侧都没实现」的漏网之鱼。

用法：
    BASE=http://127.0.0.1:8443 python3 tools/verify_switch_parity.py
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

BASE = os.environ.get("BASE", "http://127.0.0.1:8443")
PHP_PORT = int(os.environ.get("KWRT_PARITY_PHP_PORT", "8095"))
PHP_DB = "/tmp/kwrt_parity.db"

RESULTS: list[tuple] = []


def rec(item, desc, ok, ev=""):
    RESULTS.append((item, desc, bool(ok), ev))


def http(url, method="GET", data=None, cookie=None, timeout=15):
    h = {}
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        h["Content-Type"] = "application/json"
    if cookie:
        h["Cookie"] = cookie
    req = urllib.request.Request(url, data=body, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers
    except Exception as e:
        return None, str(e).encode(), {}


# ------------------------------------------------------------------ 待检开关
# (键, 可观察的表现路径, 期望「关」时的状态码)
PY_CHECKS = [
    ("page.packages_enabled", "/packages/"),
    ("page.newpkg_enabled", "/newpkg/"),
    ("page.fadian_enabled", "/fadian/"),
    ("page.contact_enabled", "/contact/"),
    ("page.login_enabled", "/login/"),
    ("entry.pwa_enabled", "/manifest.webmanifest"),
    ("entry.pwa_enabled", "/service-worker.js"),
    # 注意：/offline.html 刻意**不**受 PWA 开关管辖 —— 它是 Service Worker 的兜底页，
    # 两版实现一致地始终提供。把它写进「关了应 404」的列表会得出错误结论。
]

# PHP 侧与 Python 侧尽量同集：两侧覆盖面不同就会漏掉一侧的缺陷
# （entry.pwa_enabled 在 PHP 侧漏掉 gate，正是靠这条查出来的）
PHP_CHECKS = [
    ("page.packages_enabled", "/packages/"),
    ("page.newpkg_enabled", "/newpkg/"),
    ("page.fadian_enabled", "/fadian/"),
    ("page.contact_enabled", "/contact/"),
    ("entry.pwa_enabled", "/manifest.webmanifest"),
    ("entry.pwa_enabled", "/service-worker.js"),
]


def py_toggle(key, value):
    from app import sitesettings as SS
    SS.set_(key, value)


def py_case():
    from app import sitesettings as SS
    bad = []
    for key, path in PY_CHECKS:
        old = SS.get(key)
        py_toggle(key, True)
        on = http(BASE + path)[0]
        py_toggle(key, False)
        off = http(BASE + path)[0]
        py_toggle(key, old)
        back = http(BASE + path)[0]
        if not (on == 200 and off == 404 and back == 200):
            bad.append(f"{key}→{path}: 开{on} 关{off} 恢复{back}")
    rec("S-1", "Python：逐开关翻转都有可观察效果（开 200 / 关 404 / 可恢复）",
        not bad, f"{len(PY_CHECKS)} 项全部有效 ✓" if not bad else "; ".join(bad[:3]))


def php_case():
    """起一个独立 PHP 实例，逐开关翻转。"""
    env = dict(os.environ, KWRT_DB=PHP_DB)
    for f in (PHP_DB, PHP_DB + "-wal", PHP_DB + "-shm"):
        if os.path.exists(f):
            os.remove(f)
    proc = subprocess.Popen(
        ["php", "-S", f"127.0.0.1:{PHP_PORT}", "-t", "php/public", "php/public/router.php"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ready = False
        for _ in range(40):
            if proc.poll() is not None:
                rec("S-2", "PHP：逐开关翻转都有可观察效果", False, "PHP 实例启动失败")
                return
            if http(f"http://127.0.0.1:{PHP_PORT}/healthz")[0] == 200:
                ready = True
                break
            time.sleep(0.25)
        if not ready:
            rec("S-2", "PHP：逐开关翻转都有可观察效果", False, "实例未就绪")
            return

        def phpset(key, value):
            # 用独立脚本而不是 `php -r`：带命名空间的调用经多层反斜杠转义极易写错，
            # 且失败是静默的 —— 会让人误判成「这个开关没实现」。
            r = subprocess.run(
                ["php", "php/scripts/set_setting.php", key, "1" if value else ""],
                env=env, capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError(f"设置 {key} 失败: {r.stderr.strip()[:120]}")

        bad = []
        for key, path in PHP_CHECKS:
            url = f"http://127.0.0.1:{PHP_PORT}{path}"
            phpset(key, True)
            on = http(url)[0]
            phpset(key, False)
            off = http(url)[0]
            phpset(key, True)
            back = http(url)[0]
            if not (on == 200 and off == 404 and back == 200):
                bad.append(f"{key}→{path}: 开{on} 关{off} 恢复{back}")
        rec("S-2", "PHP：逐开关翻转都有可观察效果（开 200 / 关 404 / 可恢复）",
            not bad, f"{len(PHP_CHECKS)} 项全部有效 ✓" if not bad else "; ".join(bad[:3]))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        for f in (PHP_DB, PHP_DB + "-wal", PHP_DB + "-shm"):
            if os.path.exists(f):
                os.remove(f)


def ref_case():
    """兜底 tripwire：schema 里的开关键要在两侧源码中都出现。"""
    from app import sitesettings as SS
    from app import sitesettings as SS
    keys = {k for k in schema_keys() if k.endswith("_enabled")}

    app_src = ""
    for dp, _, fs in os.walk("app"):
        for f in fs:
            if f.endswith(".py"):
                app_src += open(os.path.join(dp, f), encoding="utf-8", errors="ignore").read()
    php_src = ""
    for dp, _, fs in os.walk("php"):
        for f in fs:
            if f.endswith(".php"):
                php_src += open(os.path.join(dp, f), encoding="utf-8", errors="ignore").read()

    miss_py = sorted(k for k in keys if k not in app_src)
    miss_php = sorted(k for k in keys if k not in php_src)
    rec("S-3", "schema 里每个 *_enabled 键在两版源码中都被引用",
        not miss_py and not miss_php,
        f"{len(keys)} 个键；Python 缺 {miss_py or '无'}；PHP 缺 {miss_php or '无'}")


def schema_keys() -> list[str]:
    """SCHEMA 是 list[dict]，项目的键名是短键 k（还有 g/t/d/label…）。"""
    from app import sitesettings as SS
    return [str(it["k"]) for it in SS.SCHEMA if isinstance(it, dict) and "k" in it]


def main() -> int:
    py_case()
    php_case()
    try:
        ref_case()
    except Exception as e:
        rec("S-3", "schema 里每个 *_enabled 键在两版源码中都被引用", False,
            f"{type(e).__name__}: {e}")

    print("=" * 96)
    print(f"{'项':<7}{'内容':<50}{'结果':<10}证据")
    print("=" * 96)
    for item, desc, ok, ev in RESULTS:
        print(f"{item:<7}{desc:<50}{'✓ 通过' if ok else '✗ 失败':<10}{ev}")
    print("=" * 96)
    bad = [r for r in RESULTS if not r[2]]
    print(f"合计 {len(RESULTS)} 项，" + ("全部通过 ✓" if not bad else f"失败 ✗ {[b[0] for b in bad]}"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
