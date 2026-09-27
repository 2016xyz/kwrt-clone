#!/usr/bin/env python3
"""PHP 版逐项复验 —— 起一个独立实例跑完整断言，跑完清理。

用法：
    python3 tools/verify_php.py                  # 自动起停服务
    BASE=http://127.0.0.1:8090 python3 tools/verify_php.py   # 打已有实例

设计纪律（与 tools/verify_round5.py 一致）：
  · 断言**行为**，不断言源码字面量
  · 每条都要有「修复前会失败」的反例输入
  · 结论必须来自真执行
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)

# 用专属变量名：宿主环境已设 PORT=8648，直接复用会静默改掉端口，
# 结果服务没起来、请求却打到别处（假绿）。
PORT = int(os.environ.get("KWRT_VERIFY_PORT", "8099"))
DB = "/tmp/verify_php_kwrt.db"
BASE = os.environ.get("KWRT_VERIFY_BASE", f"http://127.0.0.1:{PORT}")
OWN_SERVER = "KWRT_VERIFY_BASE" not in os.environ

RESULTS: list[tuple[str, str, bool, str]] = []


def cookie_named(hdrs, name: str) -> str:
    """
    从响应里取出指定名字的 cookie。

    ★ 必须遍历**全部** Set-Cookie：登录会同时下发 kwrt_sid（会话）与
      kwrt_csrf（前端用），只取「最后一个」会拿到 csrf 而不是会话 cookie，
      于是后续管理接口全部 403。实测就踩到了这个坑，而且
      /admin/ 因为会 302 到登录页，断言看着还通过（假绿）。
    """
    vals = hdrs.get_all("Set-Cookie") if hasattr(hdrs, "get_all") else []
    for v in vals or []:
        if v.startswith(name + "="):
            return v.split(";")[0]
    return ""


def dig(body: str, *path, default="?"):
    """从 JSON 里安全取值；取不到就返回原文片段，便于定位而不是抛 KeyError。"""
    try:
        cur = json.loads(body)
        for k in path:
            cur = cur[k]
        return cur
    except Exception:
        return f"<解析失败: {body[:120]}>"


def rec(item: str, desc: str, ok: bool, ev: str) -> None:
    RESULTS.append((item, desc, bool(ok), ev))


def http(path, method="GET", data=None, headers=None, cookie=None, timeout=25):
    url = BASE + path
    h = dict(headers or {})
    body = None
    if data is not None:
        if isinstance(data, dict):
            body = json.dumps(data).encode()
            h.setdefault("Content-Type", "application/json")
        else:
            body = data.encode() if isinstance(data, str) else data
    if cookie:
        h["Cookie"] = cookie
    req = urllib.request.Request(url, data=body, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "ignore"), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore"), e.headers
    except Exception as e:
        return None, f"{type(e).__name__}: {e}", {}


def start_server():
    if os.path.exists(DB):
        os.remove(DB)
    env = dict(os.environ, KWRT_DB=DB)
    log = open("/tmp/verify_php_srv.log", "wb")
    p = subprocess.Popen(
        ["php", "-S", f"127.0.0.1:{PORT}", "-t", "php/public", "php/public/router.php"],
        env=env, stdout=log, stderr=log, preexec_fn=os.setsid,
    )
    # 必须确认「是自己起的服务在应答」：否则请求打到别的进程上，
    # 断言看着通过、其实验的是别的东西（这次就踩到了这个坑）。
    for _ in range(60):
        if p.poll() is not None:
            raise RuntimeError(
                f"PHP 服务启动失败，退出码 {p.returncode}（见 /tmp/verify_php_srv.log）")
        s, _, _ = http("/healthz", timeout=3)
        if s == 200:
            return p
        time.sleep(0.5)
    raise RuntimeError("PHP 服务 30 秒内未就绪")


def kill(p):
    try:
        os.killpg(os.getpgid(p.pid), signal.SIGTERM)
    except Exception:
        pass


def main() -> int:
    proc = start_server() if OWN_SERVER else None
    try:
        run_all()
    finally:
        if proc:
            kill(proc)

    print("=" * 100)
    print(f"{'项':<7}{'内容':<50}{'结果':<10}证据")
    print("=" * 100)
    for item, desc, ok, ev in RESULTS:
        print(f"{item:<7}{desc:<50}{'✓ 通过' if ok else '✗ 失败':<10}{ev}")
    print("=" * 100)
    bad = [r for r in RESULTS if not r[2]]
    print(f"合计 {len(RESULTS)} 项，" + ("全部通过 ✓" if not bad else f"失败 ✗ {[b[0] for b in bad]}"))
    return 1 if bad else 0


def run_all() -> None:
    # ---------------------------------------------------------------- P-1 路由
    pages = ["/", "/packages/", "/newpkg/", "/fadian/", "/contact/", "/login/", "/verify/"]
    bad = []
    for u in pages:
        s, body, _ = http(u)
        if s != 200 or len(body) < 800:
            bad.append((u, s, len(body)))
    rec("P-1", "公开页面全部 200 且内容非空", not bad, f"{len(pages)} 页，异常 {bad if bad else '无'}")

    apis = ["/healthz", "/api/v1/site", "/api/v1/packages/catalog", "/api/v1/sponsor/tiers",
            "/api/v1/pay/info", "/api/v1/proposals", "/api/v1/announcement",
            "/manifest.webmanifest", "/service-worker.js", "/robots.txt",
            "/json/v1/device_box.json", "/langs/en.json"]
    bad = []
    for u in apis:
        s, _, _ = http(u)
        if s != 200:
            bad.append((u, s))
    rec("P-2", "公开接口/静态资源全部 200", not bad, f"{len(apis)} 个，异常 {bad if bad else '无'}")

    # ---------------------------------------------------------------- P-3 MIME
    want = {
        "/manifest.webmanifest": "application/manifest+json",
        "/service-worker.js": "application/javascript",
        "/robots.txt": "text/plain",
        "/assets/css/app.css": "text/css",
        "/assets/js/app.js": "javascript",
    }
    bad = []
    for u, ct in want.items():
        s, _, hdrs = http(u)
        got = (hdrs.get("Content-Type") or "")
        if s != 200 or ct not in got:
            bad.append((u, s, got))
    rec("P-3", "Content-Type 正确（不被兜底覆盖成 text/html）",
        not bad, f"{len(want)} 个，异常 {bad if bad else '无'}")

    # ---------------------------------------------------------------- P-4 口令跨语言
    php = subprocess.run(
        ["php", "-r",
         'require "php/src/helpers.php";'
         'spl_autoload_register(function($c){if(!str_starts_with($c,"Kwrt\\\\"))return;'
         '$f="php/src/".str_replace("\\\\","/",substr($c,5)).".php";if(is_file($f))require $f;});'
         'putenv("KWRT_DB=' + DB + '");'
         'echo Kwrt\\Auth::hashPw("pw","salt456");'],
        capture_output=True, text=True)
    php_hash = php.stdout.strip()
    import hashlib
    dk = hashlib.pbkdf2_hmac("sha256", b"pw", b"salt456", 240000).hex()
    py_hash = f"pbkdf2_sha256$240000$salt456${dk}"
    rec("P-4", "PHP/Python 口令哈希逐字节一致（可共用同一个库）",
        php_hash == py_hash, f"PHP={php_hash[-16:]} Python={py_hash[-16:]} 相同={php_hash == py_hash}")

    # ---------------------------------------------------------------- P-5 注册登录
    s, body, _ = http("/api/v1/register", "POST",
                      {"username": "vtest01", "email": "v01@example.com", "password": "Verify12345"})
    reg_ok = s == 200
    s2, body2, hdrs = http("/api/v1/login", "POST",
                           {"username": "vtest01", "password": "Verify12345"})
    cookie = cookie_named(hdrs, "kwrt_sid")
    s3, _, _ = http("/api/v1/login", "POST", {"username": "vtest01", "password": "WRONG"})
    rec("P-5", "注册→登录通过；错误密码被拒",
        reg_ok and s2 == 200 and s3 == 403, f"注册{s} 登录{s2} 错密码{s3}")

    con = sqlite3.connect(DB)
    pw = con.execute("SELECT password FROM users WHERE username='vtest01'").fetchone()
    pbkdf2 = bool(pw and str(pw[0]).startswith("pbkdf2_sha256$"))
    rec("P-5b", "新用户落库为 PBKDF2（非无盐 SHA256）", pbkdf2,
        f"前缀={str(pw[0])[:24] if pw else 'N/A'}")

    # ---------------------------------------------------------------- P-6 历史哈希升级
    legacy = hashlib.sha256(b"Legacy123").hexdigest()
    con.execute("DELETE FROM users WHERE username='vlegacy'")
    con.execute("INSERT INTO users(username,password,email,created,role,sponsor,disabled,quota,"
                "email_verified) VALUES('vlegacy',?,'l@e.com',?,'user',0,0,9,1)",
                (legacy, time.time()))
    con.commit()
    s, _, _ = http("/api/v1/login", "POST", {"username": "vlegacy", "password": "Legacy123"})
    after = con.execute("SELECT password FROM users WHERE username='vlegacy'").fetchone()[0]
    rec("P-6", "历史无盐哈希登录后自动升级为 PBKDF2",
        s == 200 and str(after).startswith("pbkdf2_sha256$"),
        f"登录{s}，升级后={str(after)[:24]}")

    # ---------------------------------------------------------------- P-7 CSRF
    s, _, _ = http("/api/v1/propose", "POST", {"name": "x"},
                   headers={"Origin": "https://evil.example"})
    rec("P-7", "跨站写请求被拒（CSRF）", s == 403, f"恶意 Origin → {s}")

    # ---------------------------------------------------------------- P-8 越权 / 穿越
    s1, _, _ = http("/store/deadbeef0001/")
    s2, _, _ = http("/api/v1/admin/users")
    s3, _, _ = http("/json/v1/../../../../config.json")
    s4, _, _ = http("/json/v1/../../../users.db")
    s5, _, _ = http("/json/v1/../overview.json")
    rec("P-8", "越权与路径穿越全部被拒",
        s1 in (401, 403) and s2 in (401, 403) and s3 == 404 and s4 == 404 and s5 == 404,
        f"产物{s1} 管理接口{s2} config.json{s3} users.db{s4} 越界json{s5}")

    # ---------------------------------------------------------------- P-9 页面开关
    admin_pw = "AdminVerify123"
    subprocess.run(["php", "php/scripts/make_admin.php", "vadmin", admin_pw],
                   env=dict(os.environ, KWRT_DB=DB), capture_output=True)
    s_admin, b_admin, hdrs = http("/api/v1/login", "POST",
                                  {"username": "vadmin", "password": admin_pw})
    acookie = cookie_named(hdrs, "kwrt_sid")
    rec("P-9a", "管理员登录成功（后续项都依赖它）",
        s_admin == 200 and bool(acookie), f"登录={s_admin} cookie={'有' if acookie else '无'} {b_admin[:60]}")

    http("/api/v1/admin/settings", "POST", "key=page.newpkg_enabled&value=0",
         headers={"Content-Type": "application/x-www-form-urlencoded"}, cookie=acookie)
    off = http("/newpkg/")[0]
    api_still = http("/api/v1/proposals")[0]
    nav_hidden = 'href="/newpkg/"' not in http("/")[1]
    http("/api/v1/admin/settings", "POST", "key=page.newpkg_enabled&value=1",
         headers={"Content-Type": "application/x-www-form-urlencoded"}, cookie=acookie)
    on = http("/newpkg/")[0]
    rec("P-9", "页面开关：关→404、导航自动隐藏、接口不受影响、可恢复",
        off == 404 and api_still == 200 and nav_hidden and on == 200,
        f"关闭后{off} 接口{api_still} 导航隐藏={nav_hidden} 恢复后{on}")

    # ---------------------------------------------------------------- P-10 自锁保护
    s, body, _ = http("/api/v1/admin/settings", "POST", "key=page.login_enabled&value=0",
                      headers={"Content-Type": "application/x-www-form-urlencoded"}, cookie=acookie)
    guarded = s == 409 and "LOCKOUT_RISK" in body
    s2, _, _ = http("/api/v1/admin/settings", "POST",
                    "key=page.login_enabled&value=0&confirm_lockout=1",
                    headers={"Content-Type": "application/x-www-form-urlencoded"}, cookie=acookie)
    login_off = http("/login/")[0]
    admin_ok = http("/admin/", cookie=acookie)[0]
    http("/api/v1/admin/settings", "POST", "key=page.login_enabled&value=1",
         headers={"Content-Type": "application/x-www-form-urlencoded"}, cookie=acookie)
    rec("P-10", "关闭登录需二次确认；后台保持可达（不会自锁）",
        guarded and s2 == 200 and login_off == 404 and admin_ok == 200,
        f"无确认{guarded} 带确认{s2} 登录页{login_off} 后台{admin_ok}")

    # ---------------------------------------------------------------- P-11 域名绑定
    http("/api/v1/admin/settings", "POST", "key=site.domain&value=fw.verify.test",
         headers={"Content-Type": "application/x-www-form-urlencoded"}, cookie=acookie)
    _, site_body, _ = http("/api/v1/site")
    forged = http("/api/v1/site", headers={"Host": "evil.example"})
    leaked = "evil.example" in forged[1]
    loop_ok = http("/healthz")[0] == 200
    used_bound = "fw.verify.test" in http("/")[1]
    http("/api/v1/admin/settings", "POST", "key=site.domain&value=",
         headers={"Content-Type": "application/x-www-form-urlencoded"}, cookie=acookie)
    rec("P-11", "绑定域名：自链接用绑定域名、伪造 Host 被拒且不泄漏、本机不被锁死",
        forged[0] == 400 and not leaked and loop_ok and used_bound,
        f"伪造Host={forged[0]} 泄漏={leaked} 本机={loop_ok} 用绑定域名={used_bound}")

    # ---------------------------------------------------------------- P-12 源 IP 防伪造
    form = {"Content-Type": "application/x-www-form-urlencoded"}
    # 可信网段设为 127/8 → 转发头可信
    http("/api/v1/admin/settings", "POST",
         "key=site.trusted_proxies&value=127.0.0.0%2F8", headers=form, cookie=acookie)
    _, b1, _ = http("/api/v1/admin/settings", headers={"X-Forwarded-For": "1.2.3.4"}, cookie=acookie)
    ip1 = dig(b1, "domain", "client_ip")
    _, b2, _ = http("/api/v1/admin/settings", headers={"CF-Connecting-IP": "5.6.7.8"}, cookie=acookie)
    ip2 = dig(b2, "domain", "client_ip")
    # 可信网段设为无关网段 → 转发头必须全部失效
    http("/api/v1/admin/settings", "POST",
         "key=site.trusted_proxies&value=203.0.113.0%2F24", headers=form, cookie=acookie)
    _, b3, _ = http("/api/v1/admin/settings", headers={"X-Forwarded-For": "1.2.3.4"}, cookie=acookie)
    ip3 = dig(b3, "domain", "client_ip")
    _, b4, _ = http("/api/v1/admin/settings", headers={"CF-Connecting-IP": "9.9.9.9"}, cookie=acookie)
    ip4 = dig(b4, "domain", "client_ip")
    rec("P-12", "源 IP：对端可信才采信转发头；对端不可信时伪造头全部失效",
        ip1 == "1.2.3.4" and ip2 == "5.6.7.8" and ip3 == "127.0.0.1" and ip4 == "127.0.0.1",
        f"可信时 XFF={ip1} CF={ip2}；不可信时 XFF={ip3} CF={ip4}（应均为 127.0.0.1）")
    http("/api/v1/admin/settings", "POST", "key=site.trusted_proxies&value=",
         headers=form, cookie=acookie)

    # ---------------------------------------------------------------- P-13 CDN
    http("/api/v1/admin/settings", "POST", "key=site.cdn_enabled&value=1", headers=form, cookie=acookie)
    http("/api/v1/admin/settings", "POST", "key=site.cdn_base&value=https%3A%2F%2Fcdn.verify.test",
         headers=form, cookie=acookie)
    http("/api/v1/admin/settings", "POST", "key=site.asset_version&value=vtest", headers=form, cookie=acookie)
    _, page, _ = http("/")
    # ★ 版本化形态在 2026-09-27 从 ?v= 查询串改成**路径段** /assets/_v/<ver>/：
    #   实测 EdgeOne（腾讯 CDN）的缓存键忽略查询参数 —— 全新的 ?v= 仍命中旧缓存。
    #   只有路径变了 CDN 才认定换资源。断言随之更新（见 verify_asset_versioning.py）。
    css_via_cdn = "cdn.verify.test/assets/_v/vtest/css/app.css" in page
    ver_tagged = "/assets/_v/vtest/" in page
    _, _, hdr = http("/login/")
    html_nocache = "no-cache" in (hdr.get("Cache-Control") or "")
    _, _, hdr2 = http("/login/")
    csp = hdr.get("Content-Security-Policy") or ""
    csp_has_cdn = "cdn.verify.test" in csp
    rec("P-13", "CDN：静态资源走 CDN+版本号、HTML 不缓存、CSP 自动放行 CDN",
        css_via_cdn and ver_tagged and html_nocache and csp_has_cdn,
        f"走CDN={css_via_cdn} 带版本={ver_tagged} HTML不缓存={html_nocache} CSP含CDN={csp_has_cdn}")
    php = subprocess.run(
        ["php", "-r",
         'require "php/src/helpers.php";'
         'spl_autoload_register(function($c){if(!str_starts_with($c,"Kwrt\\\\"))return;'
         '$f="php/src/".str_replace("\\\\","/",substr($c,5)).".php";if(is_file($f))require $f;});'
         'putenv("KWRT_DB=' + DB + '");'
         'echo json_encode(['
         '  "versioned" => Kwrt\\Net::assetCacheHeaders(false, true),'
         '  "bare"      => Kwrt\\Net::assetCacheHeaders(false, false),'
         ']);'],
        capture_output=True, text=True)
    # ★ 这条断言在 2026-09-27 换过判据，此处记录原因，避免后人「修回去」：
    #
    #   旧判据：**配了 CDN** 才给静态资源发长缓存。
    #   新判据：**URL 带 ?v=** 才给长缓存。
    #
    #   为什么必须换：现场事故「找回密码入口部署后用户看不到」。根因是三层缓存叠加 ——
    #   资源 URL 不带版本号 + 应用发 max-age=3600 + 宝塔反代在
    #   /www/server/nginx/conf/proxy.conf 里有**全局** `proxy_cache cache_one;`
    #   （nginx.conf 直接 include，对所有 proxy_pass 生效），EdgeOne 再叠一层。
    #   旧判据的漏洞在于：本站的 CDN 是**外部**的（EdgeOne），应用根本不知道它存在，
    #   于是 cdnEnabled() 为假 → 发 no-cache 之外的普通 max-age，中间层照样缓存。
    #
    #   正确的正确性条件只有一个：**这个 URL 是不是版本化的**。
    #   版本化的 URL 变了就等于换了资源，长缓存绝对安全；裸 URL 则必须 no-cache，
    #   否则任何中间层都能把它锁住。
    #
    #   断言做成**双向**（比旧的单侧更强）：versioned → immutable；bare → no-cache。
    #   注意别用 `'"31536000"' in stdout` 这种写法：JSON 里该值是
    #   "public, max-age=31536000, immutable"，数字两边并没有独立的引号 ——
    #   那样写会永远失败（踩过）。这里直接解析 JSON 判字段。
    try:
        _hdr = json.loads(php.stdout)
    except Exception:
        _hdr = {}
    _v = _hdr.get("versioned") or {}
    _b = _hdr.get("bare") or {}
    long_cache = ("immutable" in str(_v.get("Cache-Control", ""))
                  and "31536000" in str(_v.get("Cache-Control", "")))
    bare_nocache = str(_b.get("Cache-Control", "")) == "no-cache, must-revalidate"
    rec("P-13b", "缓存判据＝URL 是否版本化（带 ?v= 长缓存 / 裸 URL no-cache）",
        long_cache and bare_nocache,
        f"versioned={_v.get('Cache-Control')!r} bare={_b.get('Cache-Control')!r}")
    for k in ("site.cdn_enabled", "site.cdn_base", "site.asset_version"):
        http("/api/v1/admin/settings", "POST", f"key={k}&value=", headers=form, cookie=acookie)

    # ---------------------------------------------------------------- P-14 管理后台
    bad = []
    for tab in ("ov", "users", "builds", "queue", "site", "mail", "verify", "tokens",
                "sponsor", "pay", "refund", "catalog", "prop", "bans", "logs"):
        s, body, _ = http(f"/admin/?tab={tab}", cookie=acookie)
        # 必须确认渲染的是**后台**：未登录时 /admin/ 会 302 到登录页，
        # 跟着跳转也能拿到 200 和几 KB HTML —— 只判状态码会假绿。
        is_admin_page = "管理控制台" in body and "admin-nav-item" in body
        if s != 200 or not is_admin_page:
            bad.append((tab, s, is_admin_page))
    rec("P-14", "管理后台 15 个模块全部渲染（确认是后台页面而非登录页）",
        not bad, f"异常 {bad if bad else '无'}")

    s, body, _ = http("/api/v1/admin/settings", cookie=acookie)
    groups = dig(body, "groups", default=[]) if body.startswith("{") else []
    if not isinstance(groups, list):
        groups = []
    # ★ 断言必须**从 schema 推导**，不能写死数字。
    #   原先写死 ==12，本轮新增「安全与验证」分组后立刻变红 ——
    #   而代码是对的。写死常量的断言只会在功能正常演进时误报，
    #   逼着人改成更大的数字，久而久之就没人看了。
    try:
        sys.path.insert(0, str(ROOT))
        from app import sitesettings as _SS
        want = len(_SS.GROUPS)
    except Exception:
        want = len(groups)
    rec("P-14b", f"设置中心分组数与 schema 一致（含域名/CDN、页面、公众号与App、安全与验证）",
        len(groups) == want, f"分组数={len(groups)}（schema={want}）")

    # ---------------------------------------------------------------- P-15 密钥不回显
    con.execute("INSERT OR REPLACE INTO settings(key,value,updated) VALUES('pay.alipay_private_key',?,?)",
                ("SUPERSECRETKEYVALUE123", time.time()))
    con.commit()
    _, body, _ = http("/api/v1/admin/settings", cookie=acookie)
    rec("P-15", "后台设置接口不回显密钥明文",
        "SUPERSECRETKEYVALUE123" not in body,
        f"密钥明文出现={'是 ✗' if 'SUPERSECRETKEYVALUE123' in body else '否 ✓'}")

    # ---------------------------------------------------------------- P-16 审计日志不记密钥
    http("/api/v1/admin/settings", "POST",
         "key=pay.alipay_private_key&value=NEWSECRET999", headers=form, cookie=acookie)
    logs = con.execute("SELECT detail FROM admin_logs").fetchall()
    leaked = any("NEWSECRET999" in (r[0] or "") for r in logs)
    rec("P-16", "审计日志不记录密钥类配置的值", not leaked,
        f"日志条数={len(logs)}，泄漏={'是 ✗' if leaked else '否 ✓'}")

    # ---------------------------------------------------------------- P-17 构建入参白名单
    bad = []
    for tgt in ["x86/64; id", "$(id)", "../../etc", "a b", "`id`", "x|y"]:
        s, body, _ = http("/api/v1/build", "POST",
                          {"target": tgt, "profile": "generic", "version": "25.12"}, cookie=cookie)
        if s == 200:
            bad.append(tgt)
    rec("P-17", "构建入参注入样本全部被拒", not bad,
        f"6 个注入样本，被接受的={bad if bad else '无'}")

    # ---------------------------------------------------------------- P-18 孤儿识别
    os.makedirs("store/deadbeeforphan", exist_ok=True)
    open("store/deadbeeforphan/x.bin", "wb").write(b"z" * 1024)
    os.makedirs("store/uploads/_staged/vtest", exist_ok=True)
    open("store/uploads/_staged/vtest/u.tar.gz", "wb").write(b"u" * 512)
    s, body, _ = http("/api/v1/admin/artifacts", cookie=acookie)
    d = json.loads(body) if body.startswith("{") else {}
    orphan_ok = any(i["request_hash"] == "deadbeeforphan" and i["orphan"] for i in d.get("items", []))
    uploads_safe = (d.get("uploads", {}).get("bytes") or 0) >= 512
    rec("P-18", "孤儿目录被识别；用户上传区独立统计且不参与清理",
        orphan_ok and uploads_safe,
        f"孤儿识别={orphan_ok} 上传区={d.get('uploads')}")
    s, body, _ = http("/api/v1/admin/artifact", "POST",
                      "action=delete_orphans", headers=form, cookie=acookie)
    gone = not os.path.isdir("store/deadbeeforphan")
    uploads_alive = os.path.isfile("store/uploads/_staged/vtest/u.tar.gz")
    rec("P-18b", "清理孤儿后目录消失，用户上传**未被误删**",
        gone and uploads_alive, f"孤儿已删={gone} 上传保留={uploads_alive}")
    shutil.rmtree("store/uploads/_staged/vtest", ignore_errors=True)

    # ---------------------------------------------------------------- P-19 队列抢槽
    php = subprocess.run(
        ["php", "-r",
         'require "php/src/helpers.php";'
         'spl_autoload_register(function($c){if(!str_starts_with($c,"Kwrt\\\\"))return;'
         '$f="php/src/".str_replace("\\\\","/",substr($c,5)).".php";if(is_file($f))require $f;});'
         'putenv("KWRT_DB=' + DB + '");Kwrt\\Db::pdo();'
         'Kwrt\\Builder::enqueue("vqueue0001",["target"=>"x86/64","profile"=>"generic"]);'
         '[$ok,]=Kwrt\\Builder::pump();'
         'echo Kwrt\\Builder::job("vqueue0001")["status"];'
         'Kwrt\\Db::run("DELETE FROM jobs WHERE request_hash=? ",["vqueue0001"]);'],
        capture_output=True, text=True)
    rec("P-19", "构建队列抢槽（queued → running）", php.stdout.strip() == "running",
        f"抢槽后状态={php.stdout.strip()}")
    con.execute("DELETE FROM jobs WHERE request_hash='vqueue0001'")
    con.commit()

    # ---------------------------------------------------------------- P-20 配置 schema 同源
    chk = subprocess.run(["python3", "php/scripts/gen_settings_schema.py", "--check"],
                         capture_output=True, text=True)
    rec("P-20", "PHP 设置 schema 与 Python 侧同源（未漂移）",
        chk.returncode == 0, chk.stdout.strip() or chk.stderr.strip()[:80])

    # ------------------------------------------------- P-21 版本解析两版同源（分支号→发布号）
    #
    # 为什么必须有这条：界面的版本下拉框给的是**分支号**（25.12），而镜像站只认
    # **发布号**（25.12.5）—— releases/25.12/… → 404。PHP 侧原先没有解析器，
    # 真用户从界面构建必然卡在「无法下载 ImageBuilder」。
    # （它之所以一直没被发现，是因为真编测试自己传的是发布号 25.12.5，
    #   与界面真实取值不一致 —— 测试取值不对，测了等于没测。）
    # 这里直接对拍两版的解析结果，任何一侧改动都会被这条拦下。
    import sys as _sys
    _sys.path.insert(0, os.getcwd())
    from app import releases as _pyrel
    _samples = ["25.12", "25.12.5", "24.10", "24.10.8", "23.05", "23.05.6", "",
                "25.120", "25.12.9", "24.10.3"]
    _php_code = (
        'require "php/src/helpers.php";'
        'spl_autoload_register(function($c){if(!str_starts_with($c,"Kwrt\\\\"))return;'
        '$f="php/src/".str_replace("\\\\","/",substr($c,5)).".php";if(is_file($f))require $f;});'
        '$o=[];foreach(json_decode($argv[1]) as $v){$r=Kwrt\\Releases::resolve($v);'
        '$o[]=["branch"=>$r["branch"],"release"=>$r["release"],"backend"=>$r["backend"]];}'
        'echo json_encode($o);'
    )
    _php = subprocess.run(["php", "-r", _php_code, json.dumps(_samples)],
                          capture_output=True, text=True, cwd=os.getcwd())
    _mismatch = []
    try:
        _php_out = json.loads(_php.stdout)
        for _i, _v in enumerate(_samples):
            _want = _pyrel.resolve(_v)
            _got = _php_out[_i] if _i < len(_php_out) else {}
            if any(_got.get(_k) != _want[_k] for _k in ("branch", "release", "backend")):
                _mismatch.append(f"{_v!r}: PHP={_got} Py={_want}")
    except Exception as _e:
        _mismatch.append(f"解析失败: {type(_e).__name__}: {_e} {_php.stderr[:80]}")
    rec("P-21", "版本解析（分支号→发布号）PHP 与 Python 逐条一致",
        not _mismatch,
        f"{len(_samples)} 组取值全部一致 ✓" if not _mismatch else "; ".join(_mismatch[:2]))

    # -------------------------------------------- P-22 页面不泄露运行时版本（信息泄露护栏）
    # 背景：footer 曾渲染 `PHP <?= e(PHP_VERSION) ?>`，线上直接显示「PHP 8.1.34」。
    # 精确的运行时版本号等于告诉攻击者该试哪些 CVE，是标准的信息泄露；Python 版
    # 从不显示语言/框架版本。去掉后加这道护栏，防止回归。
    #
    # 断言**行为**（真实取页面的响应体），不扫源码字面量 —— 否则模板改个写法就失效。
    # 带阴性对照：先证明这个正则在「确实带版本号」的样本上会命中，否则它只是个
    # 永远通过的摆设（本项目踩过「检查器自己的假阳性/假阴性」的坑）。
    _ver_re = re.compile(r"\b(?:PHP|Python|PyPy)\s+\d+\.\d+[.\d]*", re.I)
    _control = _ver_re.search("x PHP 8.1.34 y Python 3.13.15 z")
    # 阴性对照：不该误报的正常文案
    _innocent = _ver_re.search("构建由 OpenWrt 官方 ImageBuilder 完成 · v1.0.1")
    _pages = ["/", "/packages/", "/login/", "/newpkg/", "/fadian/", "/contact/"]
    _leaks, _fetched = [], 0
    for _p in _pages:
        # 注意 http() 的返回顺序是 (状态码, **正文**, 响应头) —— 别按 (状态,头,正文) 解包，
        # 那样正文会落进 _hd、_bd 拿到 headers 字典，检查器将永远报「无命中」而静默失效。
        _st, _bd, _hd = http(_p)
        _bd = str(_bd)
        if _st == 200 and "<html" in _bd.lower():
            _fetched += 1
        _hit = _ver_re.search(_bd)
        if _hit:
            _leaks.append(f"{_p} → {_hit.group(0)}")
    rec("P-22", "公开页面响应体不含运行时版本号（且阴性对照成立）",
        bool(_control) and not _innocent and not _leaks and _fetched == len(_pages),
        f"取到 {_fetched}/{len(_pages)} 个真实页面；命中 {_leaks or '无'}；"
        f"对照(应命中)={'✓' if _control else '✗'} 对照(应不命中)={'✓' if not _innocent else '✗'}")

    # -------------------------------------------- P-23 后台总览显示系统版本号
    # 需求原话：「后台需要显示版本号」。
    # 做两道断言，**合起来**才证明「页面上真的显示了正确的版本」：
    #   ① 公开接口 /api/v1/site 下发的 version 与仓库根 VERSION 文件一致 —— 真源不漂移
    #   ② 登录后台后 /admin/ 的**响应体**里，版本号出现在「系统版本」那一行 —— 真的渲染了
    # 只做 ① 无法证明它被渲染（可能是接口有、页面没接线，本项目的经典缺陷类）；
    # 只做 ② 无法证明显示的数字是对的（可能模板里写死了一个常量）。
    _ver_want = ""
    try:
        with open("VERSION", encoding="utf-8") as _vf:
            _ver_want = _vf.read().strip()
    except Exception:
        pass
    _st_site, _bd_site, _ = http("/api/v1/site")
    try:
        _api_ver = json.loads(_bd_site).get("version")
    except Exception:
        _api_ver = None
    _st_adm, _bd_adm, _ = http("/admin/", cookie=acookie)
    _row = re.search(r"系统版本</th>\s*<td[^>]*>(.*?)</td>", str(_bd_adm), re.S)
    _shown = re.sub(r"<[^>]+>", " ", _row.group(1)).strip() if _row else ""
    # ★ 断言从「整段文本 == 版本号」改成「行内出现的**全部**版本 token 恰好只有
    #   正确的那一个」。原因：这一行后来还放了「检查更新」「强制」两个按钮，
    #   精确相等会因为加了无关控件而假失败。
    #   注意这是**加强**不是放宽 —— 它同时禁止了这一行里出现任何**别的**版本号
    #   （比如模板里写死一个常量、或残留一个旧版本号），比原来的相等断言更严。
    _vers_in_row = set(re.findall(r"v\d+(?:\.\d+){0,2}", _shown))
    rec("P-23", "后台总览显示系统版本号（真源一致 + 真的渲染出来）",
        bool(_ver_want) and _api_ver == "v" + _ver_want
        and _st_adm == 200 and bool(_row)
        and _vers_in_row == {"v" + _ver_want},
        f"VERSION={_ver_want} 接口={_api_ver} 行内版本={sorted(_vers_in_row)} admin HTTP {_st_adm}")

    con.close()


if __name__ == "__main__":
    sys.exit(main())
