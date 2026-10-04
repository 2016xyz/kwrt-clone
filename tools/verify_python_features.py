#!/usr/bin/env python3
"""Python 版「域名绑定 / CDN / 页面开关 / 源 IP 防伪造」逐项复验。

对应 PHP 版 tools/verify_php.py 的同类断言 —— 两版必须同一套口径。

★ 前置条件（否则第 P-1 组会「假失败」，说明见下）：
  服务必须用 `--forwarded-allow-ips=""` 启动（run.sh 默认已带）。
  uvicorn 的 proxy_headers 默认开启且默认信任 127.0.0.1，会在应用之前
  用 X-Forwarded-For 改写 request.client，从而架空应用层的可信代理判定。

用法：
    BASE=http://127.0.0.1:8443 python3 tools/verify_python_features.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
BASE = os.environ.get("BASE", "http://127.0.0.1:8443")
DB = os.environ.get("KWRT_DB", os.path.join(ROOT, "users.db"))

RESULTS = []


def rec(item, desc, ok, ev):
    RESULTS.append((item, desc, bool(ok), ev))


def http(path, method="GET", data=None, headers=None, cookie=None, timeout=20):
    h = dict(headers or {})
    body = None
    if data is not None:
        body = data.encode() if isinstance(data, str) else data
    if cookie:
        h["Cookie"] = cookie
    req = urllib.request.Request(BASE + path, data=body, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "ignore"), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore"), e.headers
    except Exception as e:
        return None, f"{type(e).__name__}: {e}", {}


def sid(hdrs):
    vals = hdrs.get_all("Set-Cookie") if hasattr(hdrs, "get_all") else []
    for v in vals or []:
        if "=" in v and not v.startswith("kwrt_csrf"):
            return v.split(";")[0]
    return ""


sys.path.insert(0, ROOT)
from app import sitesettings as SS  # noqa: E402


def setv(k, v):
    SS.set_(k, v)


def main() -> int:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row

    # ---------------------------------------------------------------- P-1 源 IP 防伪造
    # 场景：真实源 IP 已被封禁，攻击者能否用一个请求头绕过？
    con.execute("DELETE FROM bans WHERE value IN ('127.0.0.1','9.9.9.9')")
    con.execute("INSERT INTO bans(kind,value,reason,created) VALUES('ip','127.0.0.1','verify',0)")
    con.commit()

    def login(xff=None):
        r = urllib.request.Request(
            BASE + "/api/v1/login",
            data=b"username=user&password=user123", method="POST")
        r.add_header("Content-Type", "application/x-www-form-urlencoded")
        if xff:
            r.add_header("X-Forwarded-For", xff)
        try:
            with urllib.request.urlopen(r, timeout=15) as resp:
                return resp.status
        except urllib.error.HTTPError as e:
            return e.code
        except Exception:
            return None

    setv("site.trusted_proxies", "203.0.113.0/24")     # 对端不可信 → 转发头一律不信
    a = login()
    b = login("9.9.9.9")
    rec("P-1", "对端不可信时伪造 X-Forwarded-For 无法绕过封禁",
        a == 403 and b == 403,
        f"无伪造头={a} 伪造 XFF={b}（两者一致=伪造被无视）")
    setv("site.trusted_proxies", "127.0.0.0/8")        # 对端可信（本机反代）
    c = login("9.9.9.9")
    rec("P-1b", "对端在可信网段时采信转发头（反代场景的设计行为）",
        c == 200, f"伪造 XFF={c}（200=被采信，否则反代后真实 IP 全丢）")
    setv("site.trusted_proxies", "")
    con.execute("DELETE FROM bans WHERE value='127.0.0.1'")
    con.commit()

    # ---------------------------------------------------------------- P-2 页面开关
    setv("page.newpkg_enabled", False)
    off = http("/newpkg/")[0]
    api = http("/api/v1/proposals")[0]
    # Python 前台是 Vue SPA：导航由前端按 /api/v1/site 的 nav 字段渲染，
    # 所以「入口是否隐藏」要看 nav 载荷，不能看原始 HTML（那里永远有链接）。
    nav_off = None
    try:
        nav_off = json.loads(http("/api/v1/site")[1]).get("nav", {}).get("newpkg")
    except Exception:
        pass
    setv("page.newpkg_enabled", True)
    on = http("/newpkg/")[0]
    nav_on = None
    try:
        nav_on = json.loads(http("/api/v1/site")[1]).get("nav", {}).get("newpkg")
    except Exception:
        pass
    rec("P-2", "页面开关：关→404、导航载荷标记关闭、接口不受影响、可恢复",
        off == 404 and api == 200 and nav_off is False and nav_on is True and on == 200,
        f"关{off} 接口{api} nav.newpkg={nav_off}→{nav_on} 恢复{on}")

    # ---------------------------------------------------------------- P-3 域名绑定 / Host 注入
    def base_url_of(host=None):
        hdrs = {"Host": host} if host else None
        try:
            return json.loads(http("/api/v1/site", headers=hdrs)[1]).get("base_url")
        except Exception:
            return None

    setv("site.domain", "fw.verify.test")
    bound = base_url_of()
    forced = base_url_of("fw.verify.test")
    forged = http("/api/v1/site", headers={"Host": "evil.example"})[0]
    leaked = "evil.example" in http("/api/v1/site", headers={"Host": "evil.example"})[1]
    loop = http("/healthz")[0]
    setv("site.domain", "")
    # 前台页面用相对链接（/assets/...）是对的 —— 相对链接在任何域名下都能用，
    # 真正必须锁定的是「绝对 URL 的生成源」，也就是 base_url。
    rec("P-3", "绑定域名：绝对链接生成源改用绑定域名、伪造 Host 被拒且不泄漏、本机不被锁死",
        bound and bound.startswith("http://fw.verify.test") and forced
        and forced.startswith("http://fw.verify.test")
        and forged == 400 and not leaked and loop == 200,
        f"绑定后 base_url={bound} 伪造Host={forged} 泄漏={leaked} 本机={loop}")

    # ---------------------------------------------------------------- P-4 CDN
    setv("site.cdn_enabled", True)
    setv("site.cdn_base", "https://cdn.verify.test")
    setv("site.asset_version", "vtest")
    _, page2, _ = http("/")
    via_cdn = "cdn.verify.test/assets/css/app.css" in page2
    tagged = "?v=vtest" in page2
    _, _, hdr = http("/login/")
    html_cc = hdr.get("Cache-Control") or ""
    csp = hdr.get("Content-Security-Policy") or ""
    _, _, hdr2 = http("/assets/css/app.css")
    static_cc = hdr2.get("Cache-Control") or ""
    rec("P-4", "CDN：静态资源走 CDN+版本号、HTML 不缓存、静态长缓存、CSP 放行 CDN",
        via_cdn and tagged and "no-cache" in html_cc and "immutable" in static_cc
        and "cdn.verify.test" in csp,
        f"走CDN={via_cdn} 版本={tagged} HTML={html_cc[:22]} 静态={static_cc[:34]} CSP含CDN={'cdn.verify.test' in csp}")
    setv("site.cdn_enabled", False)
    setv("site.cdn_base", "")
    setv("site.asset_version", "")

    # ---------------------------------------------------------------- P-4b PWA / SEO
    #
    # 设置里 entry.pwa_enabled 默认为 True，而 Python 侧原先**没有任何实现** ——
    # 后台能改、前台无效果。这正是本轮修掉的那类「假功能」，用一条断言钉住。
    import json as _json
    ms, mb, mh = http("/manifest.webmanifest")
    ok_mime = "manifest" in (mh.get("Content-Type") or "")
    try:
        man = _json.loads(mb)
    except Exception:
        man = {}
    # 图标 MIME 必须与扩展名一致，否则浏览器静默拒绝该图标
    icon_ok = bool(man.get("icons")) and all(
        (i.get("type") == "image/svg+xml") == str(i.get("src", "")).split("?")[0].endswith(".svg")
        for i in man.get("icons", []))
    sw_s, sw_b, sw_h = http("/service-worker.js")
    off_s = http("/offline.html")[0]
    rb_s, rb_b, _ = http("/robots.txt")
    sm_s, sm_b, _ = http("/sitemap.xml")
    rec("P-4b", "PWA/SEO 路由齐备（manifest/SW/离线页/robots/sitemap）且 MIME 正确",
        ms == 200 and sw_s == 200 and off_s == 200 and rb_s == 200 and sm_s == 200
        and ok_mime and icon_ok and "application/javascript" in (sw_h.get("Content-Type") or "")
        and "/admin/" in rb_b and "<urlset" in sm_b,
        f"manifest={ms} SW={sw_s} 离线={off_s} robots={rb_s} sitemap={sm_s} "
        f"MIME={'✓' if ok_mime else '✗'} 图标类型匹配={'✓' if icon_ok else '✗'}")

    # 页面必须真的引用它们，否则「装了也是白装」
    pg = http("/")[1]
    # ★ 先把判定抽成变量再进 f-string：f-string 的表达式部分里不允许出现反斜杠
    #   （Python 3.12 才放宽），而本项目的运行环境是 Python 3.9 ——
    #   原先写成 f"{'有' if 'rel=\"manifest\"' in pg else '无'}" 会让整个文件
    #   在 3.9 下直接 SyntaxError，检查器自己先挂了。
    _has_manifest = 'rel="manifest"' in pg
    _has_sw = "serviceWorker.register" in pg
    rec("P-4c", "页面注入 manifest 链接与 Service Worker 注册",
        _has_manifest and _has_sw,
        f"manifest 链接={'有' if _has_manifest else '无'} "
        f"SW 注册={'有' if _has_sw else '无'}")

    # ---------------------------------------------------------------- P-5 无回归
    bad = []
    for u in ("/", "/packages/", "/newpkg/", "/fadian/", "/contact/", "/login/",
              "/healthz", "/api/v1/site", "/api/v1/packages/catalog"):
        st = http(u)[0]
        if st != 200:
            bad.append((u, st))
    rec("P-5", "常规页面与接口无回归", not bad, f"9 项，异常 {bad if bad else '无'}")

    # ---------------------------------------------------------------- P-6 敏感设置不外泄
    setv("pay.alipay_private_key", "VERIFY_SECRET_XYZ")
    _, pub, _ = http("/api/v1/site")
    rec("P-6", "公开接口不回显密钥类设置", "VERIFY_SECRET_XYZ" not in pub,
        f"命中={'是 ✗' if 'VERIFY_SECRET_XYZ' in pub else '否 ✓'}")

    con.close()

    print("=" * 92)
    print(f"{'项':<7}{'内容':<48}{'结果':<10}证据")
    print("=" * 92)
    for item, desc, ok, ev in RESULTS:
        print(f"{item:<7}{desc:<48}{'✓ 通过' if ok else '✗ 失败':<10}{ev}")
    print("=" * 92)
    bad = [r for r in RESULTS if not r[2]]
    print(f"合计 {len(RESULTS)} 项，" + ("全部通过 ✓" if not bad else f"失败 ✗ {[b[0] for b in bad]}"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
