"""域名绑定 + CDN / 反向代理适配（Python 版）。

对应 PHP 版 php/src/Net.php —— 两版必须同一套判定口径，否则同一份配置
在两个实现下行为不一致。

---------------------------------------------------------------------------
为什么必须存在（这里同时修掉了两个真实漏洞）
---------------------------------------------------------------------------
1) `client_ip()` 原先**直接取 X-Forwarded-For 的第一个值**：

       fwd = request.headers.get("x-forwarded-for")
       if fwd: return fwd.split(",")[0].strip()

   X-Forwarded-For 是**客户端可伪造**的头。攻击者只要发
   `X-Forwarded-For: 1.2.3.4`，就能任意切换身份 ——
   **封禁（bans 表按 IP）与限速（登录锁定/注册限速）同时失效**。

2) `_site_base_url()` 原先采信 `x-forwarded-host` / `host`：
   于是发给用户的**邮件里的链接**可以被指向攻击者域名（钓鱼 → 接管），
   支付宝 notify_url 也能被劫持。

两者的正确做法都是：**只信任来自「可信代理网段」的转发头**，
否则一律以直连对端（request.client.host）为准。
"""
from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import time

from . import sitesettings as SS

# 内容签名缓存：(时间戳, 值)。目录遍历不必每请求做一次。
_ASSET_SIG: dict = {"t": 0.0, "v": ""}
_ASSET_SIG_TTL = 30.0

# 默认可信代理网段：本机回环 + 私网。生产应在设置里按实际 CDN 覆盖。
DEFAULT_TRUSTED = [
    "127.0.0.0/8", "::1/128", "10.0.0.0/8",
    "172.16.0.0/12", "192.168.0.0/16", "fc00::/7",
]

# CDN 常用回源头（仅在直连对端可信时才读）
CDN_IP_HEADERS = (
    "cf-connecting-ip",     # Cloudflare
    "true-client-ip",       # Akamai / Cloudflare Enterprise
    "x-real-ip",            # Nginx 反代
    "x-client-ip",
    "fastly-client-ip",     # Fastly
    "x-azure-clientip",     # Azure Front Door
)

# 永远放行的 Host（防「绑了域名把自己锁在门外」）
LOOPBACK = {"127.0.0.1", "::1", "localhost", "ip6-localhost", "0.0.0.0"}

HOST_RE = re.compile(r"^([a-z0-9.\-]+|\[[0-9a-f:]+\])(?::\d{1,5})?$", re.I)
_SEP_RE = re.compile(r"[\s,]+")


# --------------------------------------------------------------------- 配置

def bound_domain() -> str:
    """绑定的域名；空表示未绑定。"""
    return str(SS.get("site.domain") or "").strip().lower()


def scheme_pref() -> str:
    return str(SS.get("site.scheme") or "auto").strip().lower()


def cdn_enabled() -> bool:
    return bool(SS.get("site.cdn_enabled"))


def cdn_base() -> str:
    return str(SS.get("site.cdn_base") or "").strip().rstrip("/")


def allow_any_host() -> bool:
    v = SS.get("site.allow_any_host")
    return True if v is None else bool(v)


def _host_list(raw) -> list[str]:
    if isinstance(raw, (list, tuple)):
        items = [str(x) for x in raw]
    else:
        items = _SEP_RE.split(str(raw or ""))
    out = []
    for h in items:
        h = str(h).strip().lower()
        h = re.sub(r"^[a-z]+://", "", h)
        h = h.split("/")[0].split(":")[0].strip("[]")
        if h and h != "*":
            out.append(h)
    return out


def trusted_hosts() -> list[str]:
    """可信 Host 白名单；绑定域名会自动并入。"""
    hosts = _host_list(SS.get("site.trusted_hosts"))
    bd = bound_domain()
    if bd and bd not in hosts:
        hosts.append(bd)
    return hosts


def trusted_proxies() -> list[str]:
    raw = str(SS.get("site.trusted_proxies") or "").strip()
    if not raw:
        return list(DEFAULT_TRUSTED)
    cidrs = [c for c in _SEP_RE.split(raw) if c.strip()]
    return cidrs or list(DEFAULT_TRUSTED)


# --------------------------------------------------------------------- IP

def _in_cidr(ip: str, cidr: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
        if "/" in cidr:
            return a in ipaddress.ip_network(cidr, strict=False)
        return a == ipaddress.ip_address(cidr)
    except ValueError:
        return False


def ip_in_any(ip: str, cidrs) -> bool:
    return any(_in_cidr(ip, c) for c in cidrs)


def peer_ip(request) -> str:
    """直连对端 IP（不可伪造）。"""
    return (request.client.host if request.client else "") or ""


def client_ip(request) -> str:
    """真实客户端 IP（CDN / 反代感知）。

    判定顺序：
      1. 直连对端不在可信网段 → **直接用 REMOTE_ADDR**，转发头一概不信
      2. 对端可信 → 先看 CDN 专用头，再退到 XFF「右往左找第一个不可信项」

    第 1 条是防伪造的关键：少了它，任何人发一个请求头就能换身份。
    """
    peer = peer_ip(request)
    if not peer or not ip_in_any(peer, trusted_proxies()):
        return peer

    for h in CDN_IP_HEADERS:
        v = (request.headers.get(h) or "").strip()
        if v:
            try:
                ipaddress.ip_address(v)
                return v
            except ValueError:
                pass

    xff = (request.headers.get("x-forwarded-for") or "").strip()
    if xff:
        trusted = trusted_proxies()
        for part in reversed([p.strip() for p in xff.split(",")]):
            try:
                ipaddress.ip_address(part)
            except ValueError:
                continue
            if not ip_in_any(part, trusted):
                return part
    return peer


# --------------------------------------------------------------------- URL

def scheme(request=None) -> str:
    pref = scheme_pref()
    if pref in ("http", "https"):
        return pref
    if request is not None:
        peer = peer_ip(request)
        if peer and ip_in_any(peer, trusted_proxies()):
            fp = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
            if fp in ("http", "https"):
                return fp
            if "https" in (request.headers.get("cf-visitor") or "").lower():
                return "https"
        if request.url.scheme == "https":
            return "https"
    return "http"


def host(request) -> str:
    h = (request.headers.get("host") or "").split(",")[0].strip().lower()
    return h if h and HOST_RE.match(h) else ""


def host_allowed(request) -> bool:
    """请求 Host 是否被允许（含回环永远放行，防自锁）。"""
    h = host(request)
    if not h:
        return False
    bare = h.split(":")[0].strip("[]")
    if bare in LOOPBACK:
        return True
    if bare in trusted_hosts():
        return True
    if ip_in_any(bare, trusted_proxies()):
        return True
    if bound_domain() or trusted_hosts():
        return False
    return allow_any_host()


def base_url(request=None) -> str:
    """站点对外基址（不带尾斜杠）—— 所有绝对 URL 的唯一来源。

    优先级：site.base_url → CFG.site.base_url → 绑定域名 → 白名单内 Host → 兜底。
    Host 兜底**之前必须**过 host_allowed()，否则就是 Host 头注入。
    """
    explicit = str(SS.get("site.base_url") or "").strip()
    if explicit:
        return explicit.rstrip("/")

    from .main import CFG  # 延迟导入，避免循环依赖
    cfg_base = str((CFG.get("site") or {}).get("base_url") or "").strip()
    if cfg_base:
        return cfg_base.rstrip("/")

    sch = scheme(request)
    bd = bound_domain()
    if bd:
        return f"{sch}://{bd}"

    if request is not None and host_allowed(request):
        h = host(request)
        if h:
            return f"{sch}://{h}"

    th = trusted_hosts()
    if th:
        return f"{sch}://{th[0]}"
    h = (CFG.get("server") or {}).get("host") or "127.0.0.1"
    p = (CFG.get("server") or {}).get("port") or 8443
    if h in ("0.0.0.0", "::"):
        h = "127.0.0.1"
    suffix = "" if int(p) in (80, 443) else f":{p}"
    return f"{sch}://{h}{suffix}"


def url(path: str = "/", request=None) -> str:
    return base_url(request).rstrip("/") + "/" + str(path).lstrip("/")


# --------------------------------------------------------------------- 静态资源

def asset_base(request=None) -> str:
    """静态资源基址：接了 CDN 就走 CDN，否则回源站。"""
    if cdn_enabled() and cdn_base():
        return cdn_base()
    return base_url(request)


def asset_signature() -> str:
    """对静态资源目录做内容签名：文件一改就变。

    为什么需要它：部署新 JS/CSS 后，URL 必须变，否则所有缓存层都会继续吐旧文件。
    实测事故：把「找回密码」入口写进 login.js 后，用户端一直看不到入口 ——
    因为 nginx 的**全局** proxy_cache（宝塔 /www/server/nginx/conf/proxy.conf 的
    `proxy_cache cache_one;`）按 URL 缓存了旧 login.js 一小时，EdgeOne 又叠一层。
    资源 URL 不带会变的版本号，就是「服务端已修、页面仍旧」的成因。

    取 (相对路径, 大小, mtime) 三元组的短哈希 —— 不读文件内容，
    但部署必然改变 mtime/size，所以够用且极便宜。
    结果缓存 _ASSET_SIG_TTL 秒，避免每请求遍历目录。
    """
    now = time.time()
    if _ASSET_SIG["v"] and now - _ASSET_SIG["t"] < _ASSET_SIG_TTL:
        return _ASSET_SIG["v"]
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    h = hashlib.sha1()
    for sub in ("assets", "static", "vendor", "langs", "data"):
        d = os.path.join(root, "web", sub)
        for dirpath, dirnames, filenames in os.walk(d):
            dirnames.sort()
            for fn in sorted(filenames):
                fp = os.path.join(dirpath, fn)
                try:
                    st = os.stat(fp)
                except OSError:
                    continue
                rel = os.path.relpath(fp, root)
                h.update(f"{rel}|{st.st_size}|{int(st.st_mtime)}\n".encode())
    v = h.hexdigest()[:10]
    _ASSET_SIG.update(t=now, v=v)
    return v


def asset_version() -> str:
    """静态资源版本号：优先用管理员显式配的，否则用内容签名。

    ★ 不能只靠 site.asset_version：管理员不填时它为空，旧实现回落到常量 "1"
      —— 部署新文件后 URL 纹丝不动，缓存层继续发旧资源。默认值必须是
      **内容派生**的，才能保证「改了文件就换了 URL」。
    """
    v = str(SS.get("site.asset_version") or "").strip()
    if v:
        return v
    return asset_signature()


_VERSIONABLE_PREFIXES = ("assets", "static", "vendor")


def asset(path: str, request=None) -> str:
    """资源 URL —— **路径式**版本化。

    ★ 版本号放在**路径**里（/assets/_v/<ver>/js/app.js），不放 ?v= 查询串。
      实测事故：EdgeOne（腾讯 CDN）的缓存键**忽略查询参数** —— 拿一个全新的
      ?v= 请求它仍然返回旧缓存（Age 非 0）。结果「部署了新 JS、用户端还是旧的」。
      只有**路径**变了，CDN 才认定是换了资源。

      路径式同时解决三处缓存：浏览器、宝塔反代的全局 proxy_cache、外部 CDN。
    """
    p = str(path).lstrip("/")
    head, _, tail = p.partition("/")
    if head in _VERSIONABLE_PREFIXES and tail:
        p = f"{head}/_v/{asset_version()}/{tail}"
    return f"{asset_base(request)}/{p}"


def asset_cache_headers(is_html: bool = False, versioned: bool = False) -> dict:
    """缓存头分流：**已版本化**的静态资源长缓存，其余一律不走中间缓存。

    ★ 判定依据从「是否接了 CDN」改成「这个 URL 是否带 ?v=」——
      因为真正的正确性条件是这个，而不是有没有配 CDN。

      事故背景：宝塔反代在 /www/server/nginx/conf/proxy.conf 里有**全局**
      `proxy_cache cache_one;`（nginx.conf 直接 include，所有 proxy_pass 生效），
      EdgeOne 又是一层。只要响应头里出现可缓存语义，这两层就照抄，
      于是部署后用户最长一小时拿到旧 JS/CSS。

      规则：
        · 带 ?v= 的 URL  → 长缓存 immutable（URL 变了就等于换了资源，安全）
        · 不带 ?v= 的 URL → no-cache, must-revalidate（让 nginx/CDN 不敢缓存）
      这样即使有人手敲一个裸 /assets/js/app.js，也不会被中间层锁住旧内容。
    """
    if is_html:
        return {"Cache-Control": "no-cache, must-revalidate"}
    if not versioned:
        return {"Cache-Control": "no-cache, must-revalidate"}
    return {
        "Cache-Control": "public, max-age=31536000, immutable",
        "CDN-Cache-Control": "public, max-age=31536000",
        "Cloudflare-CDN-Cache-Control": "public, max-age=31536000",
    }


def security_headers(request=None) -> dict:
    """安全响应头（含 CDN 时把 CDN 域名并入 CSP，否则 CDN 上的 JS/CSS 会被拦）。"""
    csp = (
        "default-src 'self'; img-src 'self' data: blob:; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
        "connect-src 'self'; frame-ancestors 'self'; base-uri 'self'; "
        "form-action 'self'"
    )
    if cdn_enabled() and cdn_base():
        cdn = cdn_base()
        csp = csp.replace("img-src 'self' data: blob:",
                          f"img-src 'self' data: blob: {cdn} https://qr.alipay.com")
        csp = csp.replace("style-src 'self'", f"style-src 'self' {cdn}")
        csp = csp.replace("script-src 'self'", f"script-src 'self' {cdn}")
    h = {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "SAMEORIGIN",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
        "Content-Security-Policy": csp,
    }
    if scheme(request) == "https":
        h["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return h


# --------------------------------------------------------------------- 页面开关

# 路径前缀 → 开关键（与 PHP 版 Pages::MAP 逐条对齐）
PAGE_SWITCHES = {
    "/packages/": "page.packages_enabled",
    "/newpkg/": "page.newpkg_enabled",
    "/fadian/": "page.fadian_enabled",
    "/contact/": "page.contact_enabled",
    "/login/": "page.login_enabled",
    "/verify/": "page.login_enabled",
    # 找回密码：独立开关（关掉登录页不影响找回，反之亦然）。
    # 与 /login/ 共用 page.login_enabled 是**故意的例外** —— 邮箱验证是注册流程的一环，
    # 登录页关掉就不该还能走进注册验证；而找回密码是独立功能，故单列。
    "/reset/": "page.reset_enabled",
    "/firmware/": "page.download_enabled",
    "/dl/": "page.download_enabled",
    "/store/": "page.download_enabled",
}

# 永远不受开关管辖
ALWAYS_ALLOW = ("/admin/", "/api/", "/json/", "/assets/", "/static/",
                "/langs/", "/healthz", "/favicon.ico", "/manifest.webmanifest",
                "/service-worker.js", "/offline.html", "/robots.txt")

# 不允许关闭（关掉会把管理员锁在门外）
MUST_STAY_ON = ("page.index_enabled",)


def switch_for(path: str) -> str | None:
    """路径 → 开关键；None 表示不受开关管辖。按最长前缀命中。"""
    p = "/" + str(path or "/").lstrip("/")
    for a in ALWAYS_ALLOW:
        if p.startswith(a):
            return None
    if p in ("/", ""):
        return "page.index_enabled"
    best, best_len = None, -1
    for prefix, key in PAGE_SWITCHES.items():
        if p.startswith(prefix) and len(prefix) > best_len:
            best, best_len = key, len(prefix)
    return best


def page_enabled(path: str) -> bool:
    key = switch_for(path)
    if key is None or key in MUST_STAY_ON:
        return True
    v = SS.get(key)
    return True if v is None else bool(v)


def deny_response() -> tuple[int, str | None, str | None]:
    """关闭时的响应：[状态码, 跳转目标, 提示文案]。"""
    mode = str(SS.get("page.disabled_behavior") or "404")
    if mode == "redirect":
        return 302, str(SS.get("page.disabled_redirect") or "/"), None
    if mode == "message":
        return 503, None, str(SS.get("page.disabled_message") or "该功能已暂时下线，请稍后再试。")
    return 404, None, None
