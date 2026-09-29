#!/usr/bin/env python3
"""
Kwrt 在线定制站 —— 后端服务
真实实现：元数据 API、一次性校验、会话、账户/赞助、上传、构建队列、产物下载。
"""
import base64
import hashlib
import hmac
import html

from app import captcha
from app import version as _version
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid as uuidlib

from fastapi import FastAPI, Request, Response, UploadFile, File, Form, HTTPException
from fastapi.responses import (JSONResponse, FileResponse, HTMLResponse, RedirectResponse,
                              PlainTextResponse, StreamingResponse)
from fastapi.staticfiles import StaticFiles


class RevalidatingStaticFiles(StaticFiles):
    """
    自家资源的静态托管：强制每次都回源校验（ETag/Last-Modified → 304）。

    为什么不用长缓存：本站的 HTML/JS/CSS 会随版本更新而改动，若浏览器直接
    用本地缓存，用户升级后会拿到旧脚本，出现「服务端已修、页面仍旧」的
    幽灵问题。no-cache 不是「不缓存」，而是「每次校验」——内容未变仍是 304，
    开销可忽略，换来的是版本一致性。
    """

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        return resp

from . import netcfg
from . import (builder, jobs, releases, sitesettings as SS, mailer, dl, backends, verify,
               reset, pay, refund, pkgcatalog, params, artifacts, dbutil, update)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as _cf:
    CFG = json.load(_cf)
WEB = os.path.join(ROOT, "web")
DATA = os.path.join(ROOT, "data")
STORE = os.path.join(ROOT, "store")
# 官方预编译镜像的上游主机（硬编码，绝不由请求控制 —— 防 SSRF/开放代理）
UPSTREAM_DL = "https://dl.openwrt.ai"
DB = os.path.join(ROOT, "users.db")
SESSION_TTL = 86400 * 10
PBKDF2_ROUNDS = 240000
LOGIN_MAX_FAILS = 8          # 同一账号/IP 连续失败上限
LOGIN_LOCK_SECONDS = 300     # 锁定时长
RESERVED_NAMES = {"admin", "administrator", "root", "system", "support", "official",
                  "alipay", "kwrt", "xsp"}

app = FastAPI(title="Kwrt Clone")


# --------------------------------------------------------------------------- #
# 全局中间件：安全响应头 + 写操作 CSRF 防护
# --------------------------------------------------------------------------- #
# 允许「无 Origin 头」直接放行的场景：同源的表单/测试工具通常不带 Origin，
# 而现代浏览器对跨站 fetch 一定会带 Origin —— 因此这个组合能拦住真实的 CSRF，
# 又不会误伤 curl / 内部调用。
_SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


def _same_origin(request: Request) -> bool:
    """判断请求是否来自本站。

    注意：不能用 `Origin is None` 直接放行 —— 攻击者可用
    `fetch(url, {mode:'no-cors'})` 之外的表单提交带 Origin，也可能伪造。
    这里的判定规则：
      · 带 Origin / Referer 时，必须与请求 Host 同源；
      · 都没有时（curl、非浏览器客户端）放行，因为其不携带用户 cookie 攻击场景弱。
    """
    host = (request.headers.get("host") or "").split(",")[0].strip()
    if not host:
        return False
    for hdr in ("origin", "referer"):
        v = request.headers.get(hdr)
        if not v:
            continue
        try:
            m = re.match(r"^https?://([^/]+)", v, re.I)
            if not m:
                return False
            return m.group(1).lower() == host.lower()
        except Exception:
            return False
    return True


# ======================================================================
# PWA / SEO 路由（与 PHP 版 php/src/Controllers/PublicController.php 同源）
#
# 这些看似"附属品"，但设置里 entry.pwa_enabled 默认为 True —— 只加开关不加实现，
# 后台能改而前台无任何效果，正是本轮修掉的那类"假功能"。
# ======================================================================

@app.get("/manifest.webmanifest")
def pwa_manifest():
    """Web App Manifest。仅在后台开启 PWA 时对外提供。"""
    if not SS.get("entry.pwa_enabled"):
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    logo = str(SS.get("logo_url") or "")
    icons = []
    if logo:
        _icon = netcfg.asset(logo)
        # MIME 必须与真实扩展名一致：给 .svg 声明 image/png，
        # 浏览器会拒绝这个图标（PWA 装上后是白块），且不报任何错。
        _low = logo.lower().split("?")[0]
        _mime = ("image/svg+xml" if _low.endswith(".svg")
                 else "image/webp" if _low.endswith(".webp")
                 else "image/jpeg" if _low.endswith((".jpg", ".jpeg"))
                 else "image/png")
        icons = [{"src": _icon, "sizes": "192x192", "type": _mime},
                 {"src": _icon, "sizes": "512x512", "type": _mime}]
    return JSONResponse(
        {
            "name": str(SS.get("site_name") or "Kwrt"),
            "short_name": str(SS.get("entry.pwa_short_name") or SS.get("site_short") or "Kwrt"),
            "description": str(SS.get("site_desc") or ""),
            "start_url": "/?from=pwa",
            "scope": "/",
            "display": "standalone",
            "background_color": "#ffffff",
            "theme_color": str(SS.get("entry.pwa_theme_color") or "#2563eb"),
            "orientation": "portrait-primary",
            "icons": icons,
        },
        # 跟随版本号做短缓存：改了图标/名称能较快生效，又不至于每次回源
        headers={"Cache-Control": "public, max-age=300"},
        media_type="application/manifest+json",
    )


@app.get("/service-worker.js")
def pwa_service_worker():
    """离线兜底 + 静态资源缓存。

    两条纪律写死在脚本里：
      · /api/ 一律走网络、绝不缓存 —— 否则用户会拿到过期的构建状态；
      · SW 自身绝不长缓存 —— 否则新版永远发不出去。
    """
    if not SS.get("entry.pwa_enabled"):
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    ver = str(SS.get("site.asset_version") or "1")
    js = (
        "// Kwrt Service Worker v" + ver + "\n"
        "const CACHE = 'kwrt-" + ver + "';\n"
        "const SHELL = ['/', '/offline.html'];\n\n"
        "self.addEventListener('install', (e) => {\n"
        "  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL))"
        ".then(() => self.skipWaiting()));\n"
        "});\n\n"
        "self.addEventListener('activate', (e) => {\n"
        "  e.waitUntil(caches.keys()\n"
        "    .then(ks => Promise.all(ks.filter(k => k !== CACHE)"
        ".map(k => caches.delete(k))))\n"
        "    .then(() => self.clients.claim()));\n"
        "});\n\n"
        "self.addEventListener('fetch', (e) => {\n"
        "  const req = e.request;\n"
        "  if (req.method !== 'GET') return;\n"
        "  const url = new URL(req.url);\n"
        "  if (url.origin !== location.origin) return;\n"
        "  if (url.pathname.startsWith('/api/')) return;\n"
        "  if (/\\.(css|js|png|jpe?g|svg|webp|ico|woff2?)$/i.test(url.pathname)) {\n"
        "    e.respondWith(caches.match(req).then(hit => hit || fetch(req)"
        ".then(res => {\n"
        "      const cp = res.clone();\n"
        "      caches.open(CACHE).then(c => c.put(req, cp));\n"
        "      return res;\n"
        "    }).catch(() => hit)));\n"
        "    return;\n"
        "  }\n"
        "  e.respondWith(fetch(req).catch(() => caches.match('/offline.html')));\n"
        "});\n"
    )
    return Response(js, media_type="application/javascript",
                    headers={"Cache-Control": "no-cache, must-revalidate"})


@app.get("/offline.html")
def pwa_offline():
    """断网回落页。纯静态、不依赖任何外部资源。"""
    name = str(SS.get("site_short") or SS.get("site_name") or "Kwrt")
    html = (
        "<!DOCTYPE html><html lang=\"zh-Hans\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>离线 · " + name + "</title><style>"
        "body{margin:0;min-height:100vh;display:flex;align-items:center;"
        "justify-content:center;font:16px/1.7 system-ui,-apple-system,"
        "'Segoe UI',Roboto,'Helvetica Neue','PingFang SC','Microsoft YaHei',sans-serif;"
        "background:#f6f7fb;color:#14181f;text-align:center;padding:24px}"
        ".box{max-width:420px}h1{font-size:20px;margin:0 0 8px}"
        "p{color:#6b7280;margin:0 0 20px}a{display:inline-block;padding:10px 20px;"
        "border-radius:8px;background:#4a63ed;color:#fff;text-decoration:none}"
        "</style></head><body><div class=\"box\"><h1>当前处于离线状态</h1>"
        "<p>网页已缓存，联网后即可继续使用。</p>"
        "<a href=\"/\">重试</a></div></body></html>"
    )
    return HTMLResponse(html, headers={"Cache-Control": "public, max-age=86400"})


@app.get("/robots.txt")
def robots_txt():
    """被后台关掉的页面不该被搜索引擎收录。"""
    lines = ["User-agent: *"]
    for name, path in (("packages", "/packages/"), ("newpkg", "/newpkg/"),
                       ("fadian", "/fadian/"), ("contact", "/contact/")):
        if not netcfg.page_enabled(path):
            lines.append("Disallow: " + path)
    lines += ["Disallow: /admin/", "Disallow: /store/", "Disallow: /dl/",
              "Sitemap: " + netcfg.base_url(None) + "/sitemap.xml", ""]
    return Response("\n".join(lines), media_type="text/plain")


@app.get("/sitemap.xml")
def sitemap_xml():
    """只收录仍开启的公开页面。"""
    base = netcfg.base_url(None)
    urls = [p for p in ("/", "/packages/", "/newpkg/", "/fadian/", "/contact/")
            if netcfg.page_enabled(p)]
    body = ("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
            "<urlset xmlns=\"http://www.sitemaps.org/schemas/sitemap/0.9\">\n"
            + "".join("  <url><loc>" + base + u + "</loc></url>\n" for u in urls)
            + "</urlset>\n")
    return Response(body, media_type="application/xml")


def _serve_page(filename: str, request=None, with_entry: bool = False) -> HTMLResponse:
    """服务一个页面 HTML。

    做两件 FileResponse 做不到的事：
      1. **CDN 改写**：把页面里的 /assets/ 与 /static/ 前缀换成配好的 CDN 域名，
         并统一带上 ?v=版本号 —— 否则接了 CDN 用户还是从源站拉静态资源，
         CDN 等于白接。（改 asset_version 即让 CDN 上的旧文件失效）
      2. **入口注入**：按后台开关把「App 下载」「公众号」入口插进导航与页脚。
         只在公开页注入，后台页不注入。
    """
    path = os.path.join(WEB, filename)
    try:
        with open(path, encoding="utf-8") as f:
            html_text = f.read()
    except OSError:
        return HTMLResponse(_not_found_page(filename), status_code=404)

    # ---- 资源 URL 改写：**路径式**版本化（CDN 无关）----
    #
    # ★ 版本号必须放进**路径**，不能只放 ?v= 查询串。实测事故链：
    #   ① 本站 CDN 是外部的（EdgeOne），应用不知道它存在，原先 cdn_on 为假，
    #      资源 URL 是裸的 /assets/js/login.js；
    #   ② 该响应带 max-age=3600，宝塔反代的**全局** proxy_cache
    #      （/www/server/nginx/conf/proxy.conf 的 `proxy_cache cache_one;`）
    #      与 EdgeOne 都会照抄；
    #   ③ 更硬的一条：EdgeOne 的缓存键**忽略查询参数** —— 实测拿一个全新的
    #      ?v=xxx 请求它仍返回旧缓存（Age 非 0）。所以光加查询串穿不过 CDN。
    #
    #   路径式（/assets/_v/<ver>/js/login.js）对三处缓存同时有效：
    #   浏览器、nginx 的 proxy_cache、外部 CDN，都不需要额外配置。
    ver = netcfg.asset_version()
    cdn_base = netcfg.cdn_base() if netcfg.cdn_enabled() else ""
    for _pfx in ("assets", "static", "vendor"):
        for _attr in ("href", "src"):
            # (?<![:@]) 排除 Vue 的 :src / :href 绑定表达式 —— 那是运行期求值的
            # JS 片段，文本替换会破坏语义。已带 _v/ 的不重复插。
            html_text = re.sub(
                rf'(?<![:@]){_attr}="({re.escape(cdn_base)})/{_pfx}/(?!_v/)',
                lambda m, p=_pfx: f'{_attr}="{m.group(1)}/{p}/_v/{ver}/',
                html_text)

    # ---- PWA 注入：manifest 链接 + Service Worker 注册 ----
    # 缺了这两行，「添加到主屏幕」拿不到图标与名称、断网也没有兜底页。
    if bool(SS.get("entry.pwa_enabled")):
        head = ('<link rel="manifest" href="/manifest.webmanifest">'
                '<meta name="theme-color" content="'
                + str(SS.get("entry.pwa_theme_color") or "#2563eb") + '">')
        html_text = html_text.replace("</head>", head + "</head>", 1)
        html_text = html_text.replace("</body>", """<script>
if ('serviceWorker' in navigator) {
  window.addEventListener('load', function () {
    navigator.serviceWorker.register('/service-worker.js').catch(function () {});
  });
}
</script></body>""", 1)

    # ---- 自定义导航链接注入（后台 homepage_links）----
    # ★ 这个键一直存在于 schema（JSON 数组，如 [{"label":"文档","url":"/docs"}]）
    #   却从无代码使用 —— 管理员配了页头不显示，属于「后台能配、前台不见」的死键。
    #   与 nav 里那些**页面开关**不同：这里加的是管理员自定义的外链。
    try:
        raw = SS.get("homepage_links")
        if isinstance(raw, str) and raw.strip().startswith("["):
            raw = json.loads(raw)
        if isinstance(raw, list) and raw:
            extra = ""
            for item in raw:
                if not isinstance(item, dict):
                    continue
                label = str(item.get("label") or "").strip()
                href = str(item.get("url") or "").strip()
                # 字符白名单：只允许 http(s) 与站内相对路径，
                # 防 javascript: / data: 这类把 XSS 带进每一页页头的写法
                if not label or not href:
                    continue
                if not (href.startswith("http://") or href.startswith("https://")
                        or (href.startswith("/") and not href.startswith("//"))):
                    continue
                extra += (f'<a class="nav-link" href="{html.escape(href, quote=True)}">'
                          f'{html.escape(label)}</a>')
            if extra:
                # 注入到主导航最后一个 </nav> 之前。
                # 注意：这里必须用 html_text（不是 html_text）—— 函数内已有局部变量
                # `html_text`（页面正文），会把模块 `html_text` 遮蔽掉，
                # 于是 html.escape 报 'str' object has no attribute 'escape'，
                # 整个注入被 except 静默吞掉、后台配的链接一条都不显示。
                idx = html_text.rfind("</nav>")
                if idx > 0:
                    html_text = html_text[:idx] + extra + html_text[idx:]
    except Exception as exc:
        print("[serve] homepage_links 注入失败（已忽略）:", exc, flush=True)

    # ---- App / 公众号入口注入 ----
    if with_entry:
        app_on = bool(SS.get("entry.app_enabled"))
        wx_on = bool(SS.get("entry.wechat_enabled"))
        if app_on or wx_on:
            links = ""
            if app_on:
                links += '<a href="#app" class="entry-link" data-entry="app">App 下载</a>'
            if wx_on:
                links += '<a href="#wechat" class="entry-link" data-entry="wechat">公众号</a>'
            html_text = html_text.replace("</head>",
                                "<style>"
                                ".entry-link{margin-left:12px;font-size:14px;color:inherit}"
                                ".entry-modal{position:fixed;inset:0;z-index:9999;display:none;"
                                "align-items:center;justify-content:center;background:rgba(0,0,0,.5)}"
                                ".entry-modal.on{display:flex}"
                                ".entry-box{background:#fff;color:#14181f;border-radius:12px;"
                                "padding:22px;max-width:460px;width:92%;text-align:center}"
                                ".entry-box img{max-width:200px}"
                                "</style></head>", 1)
            html_text = html_text.replace("</body>", _entry_block() + _entry_script() + "</body>", 1)
            # 导航里插入入口（首页与各公开页都有 .head-act 结构）
            html_text = html_text.replace('<div class="head-act">',
                                f'<div class="head-act">{links}', 1)

    return HTMLResponse(html_text, headers=netcfg.asset_cache_headers(True))


def _entry_block() -> str:
    """App 下载 / 公众号 弹层（内容全部来自后台设置）。"""
    app_on = bool(SS.get("entry.app_enabled"))
    wx_on = bool(SS.get("entry.wechat_enabled"))
    out = ""
    if app_on:
        name = str(SS.get("entry.app_name") or "Kwrt 助手")
        desc = str(SS.get("entry.app_desc") or "")
        apk = str(SS.get("entry.app_android_url") or "")
        ios = str(SS.get("entry.app_ios_url") or "")
        qr = str(SS.get("entry.app_qrcode_url") or "")
        links = ""
        if apk:
            links += f'<a class="btn btn-primary" href="{apk}" rel="noopener">Android 下载 APK</a> '
        if ios:
            links += f'<a class="btn" href="{ios}" rel="noopener">iPhone / iPad 下载</a>'
        qrimg = (f'<img src="{qr}" alt="App 下载二维码">' if qr
                 else f"<div class='qr' data-qr='{apk or ios}'></div>")
        out += ('<div class="entry-modal" id="entry-app"><div class="entry-box">'
                f"<h3>{name}</h3><p style='color:#6b7280;font-size:13px'>{desc}</p>"
                f"{qrimg}<div style='margin-top:12px'>{links}</div>"
                "<p style='color:#6b7280;font-size:12px'>App 即本站在 WebView 中的封装，"
                "功能与网页版一致；也可直接在浏览器里「添加到主屏幕」。</p>"
                '<p><button class="btn" data-entry-close>关闭</button></p>'
                "</div></div>")
    if wx_on:
        nm = str(SS.get("entry.wechat_official_name") or "官方公众号")
        hint = str(SS.get("entry.wechat_follow_hint") or "")
        qr = str(SS.get("entry.wechat_qrcode_url") or "")
        # ★ entry.wechat_appid 一直存在于 schema（hint 写着「仅用于展示」）
        #   却从无代码使用 —— 后台填了前台看不见。这里按 hint 的原意展示出来：
        #   公众号 AppID 是用户能在微信里搜到该号的唯一标识，比二维码更耐用
        #   （二维码会过期，AppID 不会）。同样做 HTML 转义。
        aid = str(SS.get("entry.wechat_appid") or "").strip()
        aid_html = ""
        if aid:
            aid_html = ("<p style='color:#6b7280;font-size:13px'>公众号 ID："
                        f"<code>{html.escape(aid)}</code></p>")
        out += ('<div class="entry-modal" id="entry-wechat"><div class="entry-box">'
                f"<h3>{html.escape(nm)}</h3>"
                f"<p style='color:#6b7280;font-size:13px'>{html.escape(hint)}</p>"
                + (f'<img src="{html.escape(qr, quote=True)}" alt="公众号二维码">' if qr
                   else "<p style='color:#6b7280'>管理员尚未配置公众号二维码。</p>")
                + aid_html
                + '<p><button class="btn" data-entry-close>关闭</button></p></div></div>')
    return out


def _entry_script() -> str:
    return """<script>
(function(){
  document.addEventListener('click', function(e){
    var a = e.target.closest ? e.target.closest('[data-entry]') : null;
    if (a) { e.preventDefault();
      var m = document.getElementById('entry-' + a.getAttribute('data-entry'));
      if (m) m.classList.add('on'); return; }
    if (e.target.closest && e.target.closest('[data-entry-close]')) {
      document.querySelectorAll('.entry-modal').forEach(function(m){m.classList.remove('on');}); }
    if (e.target.classList && e.target.classList.contains('entry-modal')) {
      e.target.classList.remove('on'); }
  });
  document.addEventListener('keydown', function(e){
    if (e.key === 'Escape') document.querySelectorAll('.entry-modal')
      .forEach(function(m){m.classList.remove('on');});
  });
})();
</script>"""


def _not_found_page(path: str = "/") -> str:
    """极简 404（被页面开关拦下时返回，与 PHP 版 error404 模板同等作用）。"""
    safe = (str(path).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    return ("<!DOCTYPE html><html lang=\"zh-CN\"><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>404</title><body style=\"font:16px/1.6 -apple-system,sans-serif;"
            "text-align:center;padding:80px 16px;color:#14181f\">"
            "<h1 style=\"font-size:56px;margin:0 0 8px;color:#6b7280\">404</h1>"
            f"<p style=\"color:#6b7280\">页面不存在或已被移除。</p>"
            f"<p style=\"font-family:monospace;font-size:13px;color:#9aa4b2\">{safe}</p>"
            "<p><a href=\"/\" style=\"color:#2563eb\">返回首页</a></p></body></html>")


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    path = request.url.path

    # -1) 强制 HTTPS。
    #     ★ 这个开关 PHP 版一直有实现（php/public/index.php 的 3.0 段），
    #       Python 版却**只有设置项、没有代码** —— 后台打开后毫无效果，
    #       是同一份 schema 下最典型的一种静默失效。
    #     仅在「设置开启 且 当前确实是 http」时跳转，避免反代终止 TLS 的场景下死循环。
    #     判定依据用 netcfg 的对外 scheme（尊重 X-Forwarded-Proto），而不是 socket 上的明文事实。
    if bool(SS.get("site.force_https", False)) and netcfg.scheme(request) != "https":
        qs = request.url.query
        base = netcfg.base_url(request)
        if base.startswith("http://"):
            base = "https://" + base[len("http://"):]
        target = base.rstrip("/") + path + (f"?{qs}" if qs else "")
        return RedirectResponse(target, status_code=301)

    # 0) Host 白名单：不通过直接拒绝。
    #    注意回环地址永远放行 —— 否则「绑定域名」之后从本机也进不了后台，
    #    等于设了域名就把自己锁死且改不回来（PHP 版踩过同一个坑）。
    if not netcfg.host_allowed(request):
        return JSONResponse({"detail": "请求的 Host 不在允许列表内"}, status_code=400)

    # 0.1) 页面开关：一处判定，全站口径一致（判定表在 netcfg.PAGE_SWITCHES）
    if not netcfg.page_enabled(path):
        code, to, msg = netcfg.deny_response()
        if to is not None:
            return RedirectResponse(to, status_code=302)
        if msg is not None:
            return HTMLResponse(
                f"<!DOCTYPE html><html lang=zh-CN><meta charset=utf-8>"
                f"<title>暂时下线</title><body style='font:16px/1.6 sans-serif;"
                f"text-align:center;padding:80px 16px'>{msg}"
                f"<p><a href='/'>返回首页</a></p></body></html>",
                status_code=code)
        return HTMLResponse(_not_found_page(path), status_code=404)

    # 1) CSRF：所有会改状态的请求都校验同源
    if request.method not in _SAFE_METHODS:
        path = request.url.path
        # 支付宝回调是外站发起，靠验签保护，不走同源判定
        if not path.startswith("/api/v1/alipay/"):
            if not _same_origin(request):
                return JSONResponse({"detail": "跨站请求已被拒绝（CSRF 防护）"},
                                    status_code=403)

    resp = await call_next(request)

    # 2) 安全响应头。由 netcfg 统一给出，接了 CDN 会自动把 CDN 域名并入
    #    script/style/img-src —— 否则 CDN 上的 JS/CSS 会被浏览器拦掉。
    #    注意：script-src 必须保留 'unsafe-eval'（Vue 运行时编译器依赖
    #    new Function()，去掉会让整页空白，已实测）。
    for k, v in netcfg.security_headers(request).items():
        resp.headers.setdefault(k, v)

    # 3) 缓存头分流：**带 ?v= 的**静态资源长缓存，其余一律 no-cache。
    #
    #    ★ 判据是「这个 URL 是否版本化」，不是「是否配了 CDN」。
    #      实测事故：资源 URL 不带版本号时，宝塔反代的**全局** proxy_cache
    #      （/www/server/nginx/conf/proxy.conf 的 `proxy_cache cache_one;`，
    #      由 nginx.conf 直接 include）与 EdgeOne 会照抄 max-age，
    #      导致部署后的新 JS/CSS 最长一小时发不出去。
    #      带上版本号后 URL 变了，缓存自然失效；裸 URL 则强制回源。
    ctype = (resp.headers.get("content-type") or "").lower()
    is_static = any(ctype.startswith(t) for t in
                    ("text/css", "application/javascript", "image/", "font/",
                     "application/font")) or path.endswith(
                    (".css", ".js", ".png", ".jpg", ".jpeg", ".svg",
                     ".webp", ".ico", ".woff", ".woff2"))
    if path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    elif is_static:
        # ★ 这里必须**赋值覆盖**，不能用 setdefault：
        #   RevalidatingStaticFiles 给静态资源显式写了 no-cache,must-revalidate，
        #   用 setdefault 根本改不掉 —— 结果就是「接了 CDN 依然每次回源」，
        #   CDN 命中率永远为 0。实测踩到过。
        #
        # ★ 判据是「这个 URL 是否版本化」，不是「是否配了 CDN」。版本化有两种形态：
        #   · 路径式 /assets/_v/<ver>/...   ← 主用形态，能穿过忽略查询参数的 CDN
        #   · 查询式 ?v=<ver>               ← 兼容旧链接
        #   两者都算版本化，都可以长缓存。
        versioned = ("v=" in (request.url.query or "")) or bool(
            re.search(r"/(?:assets|static|vendor)/_v/[^/]+/", path))
        for k, v in netcfg.asset_cache_headers(False, versioned=versioned).items():
            resp.headers[k] = v
    else:
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp

# --------------------------------------------------------------------------- #
# 数据库：账户 / 会话 / 构建记录
# --------------------------------------------------------------------------- #
def db():
    # factory=ClosingConnection：让 `with db() as c:` 提交后真正**关闭**连接。
    # 原生 sqlite3.Connection 的 __exit__ 只 commit 不 close —— 实测每调用一次
    # 泄漏 1 个 fd（循环引用只能靠分代 GC 回收），默认 ulimit 下构成进程级 DoS。
    c = sqlite3.connect(DB, factory=dbutil.ClosingConnection)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            email TEXT,
            sponsor INTEGER DEFAULT 0,
            created REAL
        );
        CREATE TABLE IF NOT EXISTS sessions(
            token TEXT PRIMARY KEY,
            username TEXT,
            created REAL,
            expires REAL
        );
        CREATE TABLE IF NOT EXISTS builds(
            request_hash TEXT PRIMARY KEY,
            username TEXT,
            target TEXT,
            profile TEXT,
            packages TEXT,
            status TEXT,
            created REAL,
            payload TEXT
        );
        CREATE TABLE IF NOT EXISTS proposals(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT, url TEXT, note TEXT, created REAL
        );
        """)
        # --- 迁移：角色体系 ------------------------------------------------
        cols = {r[1] for r in c.execute("PRAGMA table_info(users)")}
        if "role" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'user'")
        if "disabled" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN disabled INTEGER DEFAULT 0")
        if "last_login" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN last_login REAL")
        if "quota" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN quota INTEGER DEFAULT 12")
        # --- 管理员操作审计 -------------------------------------------------
        c.execute("""
        CREATE TABLE IF NOT EXISTS admin_logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin TEXT, action TEXT, target TEXT, detail TEXT, ip TEXT, created REAL
        )""")
        # --- 工单/提议处理状态 ----------------------------------------------
        pcols = {r[1] for r in c.execute("PRAGMA table_info(proposals)")}
        if "status" not in pcols:
            c.execute("ALTER TABLE proposals ADD COLUMN status TEXT DEFAULT 'pending'")
        if "reply" not in pcols:
            c.execute("ALTER TABLE proposals ADD COLUMN reply TEXT DEFAULT ''")
        # 提交来源 IP：用于频率限制与「本人撤回」的归属判断
        if "ip" not in pcols:
            c.execute("ALTER TABLE proposals ADD COLUMN ip TEXT DEFAULT ''")
        # --- 站点公告（管理员可下发） ----------------------------------------
        c.execute("""
        CREATE TABLE IF NOT EXISTS settings(
            key TEXT PRIMARY KEY, value TEXT, updated REAL
        )""")
        # --- 封禁列表 --------------------------------------------------------
        c.execute("""
        CREATE TABLE IF NOT EXISTS bans(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT, value TEXT, reason TEXT, created REAL
        )""")


init_db()

# 退款申请表（幂等建表；与 init_db 分离以保持单文件可读）
with db() as _c:
    refund.init(_c)


def audit(admin, action, target="", detail="", ip=""):
    """写管理员操作审计日志（真实留痕）。"""
    with db() as c:
        c.execute("INSERT INTO admin_logs(admin,action,target,detail,ip,created) VALUES(?,?,?,?,?,?)",
                  (admin, action, target, detail, ip, time.time()))


def get_setting(key, default=None):
    """统一配置读取入口 —— 全部委托给 sitesettings（schema 驱动 + 类型化）。"""
    try:
        return SS.get(key, default)
    except Exception:
        with db() as c:
            r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default


def set_setting(key, value):
    """统一配置写入入口 —— 经 schema 校验后落库。"""
    try:
        return SS.set_(key, value)
    except Exception:
        with db() as c:
            c.execute("INSERT OR REPLACE INTO settings(key,value,updated) VALUES(?,?,?)",
                      (key, str(value), time.time()))
        return value


# 赞助到期检查：sponsor 列 0/1，sponsor_until 记录到期时间
with db() as c:
    ucols = {r[1] for r in c.execute("PRAGMA table_info(users)")}
    if "sponsor_until" not in ucols:
        c.execute("ALTER TABLE users ADD COLUMN sponsor_until REAL")
    if "sponsor_tier" not in ucols:
        c.execute("ALTER TABLE users ADD COLUMN sponsor_tier TEXT DEFAULT ''")
    if "sponsor_amount" not in ucols:
        c.execute("ALTER TABLE users ADD COLUMN sponsor_amount REAL DEFAULT 0")
    # 邮箱验证标记：默认 1（历史账号视为已验证），新注册按站点开关决定
    if "email_verified" not in ucols:
        c.execute("ALTER TABLE users ADD COLUMN email_verified INTEGER DEFAULT 1")

    # 支付宝当面付订单表
    c.executescript("""
    CREATE TABLE IF NOT EXISTS pay_orders (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        out_trade_no TEXT NOT NULL UNIQUE,
        username     TEXT NOT NULL,
        tier_name    TEXT NOT NULL,
        amount       REAL NOT NULL,
        days         INTEGER NOT NULL,
        status       TEXT NOT NULL DEFAULT 'created',
        qr_code      TEXT,
        trade_no     TEXT,
        buyer_id     TEXT,
        created      REAL NOT NULL,
        expires      REAL,
        paid_at      REAL,
        last_query   REAL,
        raw          TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_po_user   ON pay_orders(username, status);
    CREATE INDEX IF NOT EXISTS idx_po_status ON pay_orders(status, created DESC);
    """)

# 邮箱验证表
verify.init()

# 找回密码表（与邮箱验证同源，独立一张表 + 独立 TTL）
reset.init()

# 更新检查的缓存表（避免反复点「检查更新」把 GitHub 速率打满）
update.init()


# 初始管理员：只在「库里一个管理员都没有」时创建，且密码来自外部注入或随机生成。
# 原先这里硬编码 admin/admin123 并 INSERT OR IGNORE，等于每个部署都带一组已知口令。
def _ensure_admin():
    with db() as c:
        n = c.execute("SELECT COUNT(*) n FROM users WHERE role='admin'").fetchone()["n"]
        if n:
            return
        pw = os.environ.get("KWRT_ADMIN_PASSWORD") or secrets.token_urlsafe(12)
        c.execute("INSERT OR IGNORE INTO users(username,password,sponsor,role,created) "
                  "VALUES(?,?,?,?,?)",
                  ("admin", _hash_pw(pw), 1, "admin", time.time()))
        c.execute("UPDATE users SET role='admin', sponsor=1, disabled=0 WHERE username='admin'")
    if not os.environ.get("KWRT_ADMIN_PASSWORD"):
        print("[init] 已生成初始管理员 admin，随机密码：" + pw, flush=True)
        print("[init] 请立即登录后台修改密码，或设置 KWRT_ADMIN_PASSWORD 环境变量。", flush=True)


def _hash_pw(pw: str, salt: str = "") -> str:
    """带盐口令哈希：`pbkdf2_sha256$迭代次数$盐$哈希`。

    兼容历史数据：验证时若库里是无盐的 64 位 hex，则按 SHA256 比对，
    并在登录成功后**自动升级**为加盐格式（见 api_login）。
    """
    if not salt:
        salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${salt}${dk.hex()}"


def _verify_pw(pw: str, stored: str):
    """返回 (是否通过, 是否需要升级重哈希)。"""
    stored = stored or ""
    if stored.startswith("pbkdf2_sha256$"):
        try:
            _, rounds, salt, want = stored.split("$", 3)
            dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), int(rounds))
            return (hmac.compare_digest(dk.hex(), want), False)
        except Exception:
            return (False, False)
    # 历史无盐 SHA256
    legacy = hashlib.sha256(pw.encode()).hexdigest()
    if hmac.compare_digest(legacy, stored):
        return (True, True)
    return (False, False)


# 初始管理员兜底（定义顺序：_hash_pw/_verify_pw 必须先就位）
_ensure_admin()


# --------------------------------------------------------------------------- #
# 一次性校验值（与前端 GENERATE_NG_ONE_TIME_VERIF_VALUE 同构）
# --------------------------------------------------------------------------- #
def verify_ng_value(raw: str):
    """base64 -> xor 0x50 -> JSON{uuid}；返回 uuid 或 None"""
    if not raw:
        return None
    try:
        dec = base64.b64decode(raw).decode("utf-8", "ignore")
        txt = "".join(chr(ord(ch) ^ 80) for ch in dec)
        obj = json.loads(txt)
        u = obj.get("uuid", "")
        if re.fullmatch(r"[0-9a-fA-F-]{36}", u):
            return u
    except Exception:
        return None
    return None


def current_user(request: Request):
    tok = request.cookies.get(CFG["server"]["session_name"])
    if not tok:
        return None
    with db() as c:
        r = c.execute("SELECT username,expires FROM sessions WHERE token=?", (tok,)).fetchone()
        if not r or r["expires"] < time.time():
            return None
        u = c.execute("SELECT username,sponsor,email,role,disabled,quota,sponsor_until,sponsor_tier "
                      "FROM users WHERE username=?",
                      (r["username"],)).fetchone()
        if not u:
            return None
        if u["disabled"]:
            return None                      # 已封禁账户的会话立即失效
        # 赞助到期自动失效（到期时间由管理员设置）
        sponsor = bool(u["sponsor"])
        until = u["sponsor_until"]
        if sponsor and until and until < time.time():
            sponsor = False
        # 会话建立后被降权 / 提权，以数据库实时角色为准
        return {"username": u["username"], "sponsor": sponsor,
                "email": u["email"], "role": u["role"] or "user",
                # is_admin 与 /api/v1/login 的响应保持一致，便于前端统一判断
                "is_admin": (u["role"] or "user") == "admin",
                "sponsor_until": until, "sponsor_tier": u["sponsor_tier"] or "",
                "quota": u["quota"] if u["quota"] is not None else 12}


def is_admin(request: Request):
    """返回管理员 dict；非管理员返回 None。

    ⚠ 历史坑：若此处改成返回 True，则 `a = is_admin(request); a["username"]`
    会在管理员分支抛 TypeError。统一「返回 dict 或 None」，调用方一律判真值。
    """
    u = current_user(request)
    return u if (u and u.get("role") == "admin") else None


def require_admin(request: Request):
    """管理员鉴权：非管理员一律 403（前端路由与 API 双向校验）。"""
    u = is_admin(request)
    if not u:
        ip = client_ip(request) or "-"
        key = "admin:" + ip
        _login_fail(key)
        raise HTTPException(status_code=403, detail="需要管理员权限")
    _login_reset("admin:" + (client_ip(request) or "-"))
    return True


def require_admin_limited(request: Request):
    """同 require_admin，但对持续尝试的 IP 追加限速（后台接口防爆破）。"""
    ip = client_ip(request) or "-"
    if _login_locked("admin:" + ip):
        raise HTTPException(status_code=429, detail="尝试过于频繁，请稍后再试")
    return require_admin(request)


def client_ip(request: Request):
    """真实客户端 IP（CDN/反代感知）。

    ★ 这曾是一个**真实漏洞**：原实现直接取 `X-Forwarded-For` 的第一个值。
      该头由客户端完全可控，攻击者发 `X-Forwarded-For: 1.2.3.4` 即可任意
      切换身份 —— **封禁（bans 表按 IP）与限速（登录锁定/注册限速）同时失效**。

    现在只在「直连对端本身落在可信代理网段内」时才采信转发头，
    并右往左跳过可信代理取第一个不可信地址；否则一律以 REMOTE_ADDR 为准。
    """
    return netcfg.client_ip(request)


# 构建任务 ID 白名单：md5 前 16 位 hex（builder.submit 生成），兼容历史字符集
HASH_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def check_hash_id(v, *, allow_empty=False):
    """校验构建任务 ID。

    为什么必须做：多处用 `os.path.basename(request_hash)` 拼路径，
    而 basename("..") == ".."，会让 STORE/<id> 变成 STORE/..，
    进而列出/下载项目根目录下的文件（users.db 就在那里）。
    """
    v = str(v or "")
    if not v:
        if allow_empty:
            return ""
        raise HTTPException(400, "缺少任务 ID")
    if v == "uploads" or not HASH_ID_RE.match(v):
        raise HTTPException(400, "任务 ID 非法")
    return v


def safe_under(base: str, *parts: str):
    """在 base 目录下安全拼接路径：越界一律返回 None。

    为什么必须要它（真实漏洞）：
        /json/v1/{path:path} 直接把请求路径 os.path.join(DATA,"json","v1",path)，
        请求 "/json/v1/../../../config.json" 即可读到项目配置 —— 已实测复现。
        os.path.join 本身会"吃掉"用户传的绝对路径，且完全不处理 ".." 上跳。

    规则：拼接后取 realpath，必须仍在 realpath(base) 之内才算合法。
    """
    if base is None:
        return None
    root = os.path.realpath(base)
    rel = os.path.join(root, *[str(x) for x in parts])
    real = os.path.realpath(rel)
    if real == root or not real.startswith(root + os.sep):
        return None
    return real


# --- 登录失败限速（进程内） ------------------------------------------------- #
_LOGIN_FAILS = {}          # key -> (失败次数, 首次失败时间)
# ★ 容量上限。key 里含**请求方完全可控的用户名**，而 /api/v1/login 是未认证
# 接口 —— 原来这张表只增不减（过期项要等下次被查到才删），攻击者用海量随机
# 用户名登录即可让它无界增长直至 OOM。这里与 _RATE 用同一套「满了淘汰最旧」策略。
_LOGIN_FAILS_MAX = 20000


def _login_fails_prune(now: float):
    """满了先清已过锁定期的，仍满则按插入顺序淘汰最旧的。"""
    if len(_LOGIN_FAILS) < _LOGIN_FAILS_MAX:
        return
    for k in [k for k, (n, t0) in _LOGIN_FAILS.items()
              if not n or now - t0 > LOGIN_LOCK_SECONDS]:
        _LOGIN_FAILS.pop(k, None)
    while len(_LOGIN_FAILS) >= _LOGIN_FAILS_MAX:
        _LOGIN_FAILS.pop(next(iter(_LOGIN_FAILS)), None)


def _login_locked(key: str) -> bool:
    now = time.time()
    _login_fails_prune(now)
    n, t0 = _LOGIN_FAILS.get(key, (0, 0))
    if not n:
        return False
    if now - t0 > LOGIN_LOCK_SECONDS:
        _LOGIN_FAILS.pop(key, None)
        return False
    return n >= LOGIN_MAX_FAILS


def _login_fail(key: str):
    now = time.time()
    _login_fails_prune(now)
    n, t0 = _LOGIN_FAILS.get(key, (0, 0))
    if not n or now - t0 > LOGIN_LOCK_SECONDS:
        _LOGIN_FAILS[key] = (1, now)
    else:
        _LOGIN_FAILS[key] = (n + 1, t0)


def _login_reset(key: str):
    _LOGIN_FAILS.pop(key, None)


# --- 通用滑动窗口限速（进程内） --------------------------------------------- #
# 为什么需要：登录有锁定、提议有 10 分钟计数，但 /api/v1/register 与
# /api/v1/resend_verify 当时**完全没有来源限速** —— 实测可连续无限注册账号，
# 也可遍历用户名反复触发验证邮件（邮件轰炸）。这里给这两类入口补上限速。
_RATE = {}                       # bucket -> {key: [命中时间戳]}
_RATE_LOCK = threading.Lock()
_RATE_MAX_KEYS = 5000            # 防止字典无限增长


def rate_ok(bucket: str, key: str, limit: int, window: int):
    """滑动窗口限速，返回 (是否允许, 建议等待秒数)。"""
    now = time.time()
    with _RATE_LOCK:
        d = _RATE.setdefault(bucket, {})
        hits = [t for t in d.get(key, []) if now - t < window]
        if len(hits) >= limit:
            wait = int(window - (now - hits[0])) + 1
            d[key] = hits
            return False, max(1, wait)
        hits.append(now)
        d[key] = hits
        if len(d) > _RATE_MAX_KEYS:
            for k in list(d)[: _RATE_MAX_KEYS // 2]:
                d.pop(k, None)
        return True, 0


def is_banned(value):
    with db() as c:
        r = c.execute("SELECT 1 FROM bans WHERE value=? LIMIT 1", (value,)).fetchone()
    return bool(r)


def _cookie_secure(request=None) -> bool:
    """
    会话 Cookie 是否带 Secure 标记。

    默认按请求协议自动判断（HTTPS 或可信反代声明 X-Forwarded-Proto=https 就加）。
    纯内网 HTTP 部署时若强制 Secure，浏览器根本不回传 cookie、直接登录不上，
    所以允许用 KWRT_COOKIE_SECURE=0/1 显式覆盖。
    """
    ov = (os.environ.get("KWRT_COOKIE_SECURE") or "").strip().lower()
    if ov in ("0", "false", "no", "off"):
        return False
    if ov in ("1", "true", "yes", "on"):
        return True
    if request is None:
        return False
    try:
        if request.url.scheme == "https":
            return True
        proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
        return proto == "https"
    except Exception:
        return False


def make_session(response: Response, username, request=None):
    """建立会话。

    会话固定防护：调用方在「已存在会话」时先删除旧令牌（见 api_login），
    这里始终签发全新随机令牌，绝不复用请求里带来的旧值。

    ★ 补上 Secure：原来只有 httponly + samesite，明文 HTTP 链路上会话令牌
    会被中间人直接截获（本项目存在纯 HTTP 部署形态，所以做成「按协议自动 +
    环境变量可覆盖」，而不是无条件开启）。
    """
    tok = secrets.token_hex(32)
    with db() as c:
        c.execute("INSERT OR REPLACE INTO sessions(token,username,created,expires) VALUES(?,?,?,?)",
                  (tok, username, time.time(), time.time() + SESSION_TTL))
    response.set_cookie(CFG["server"]["session_name"], tok, max_age=SESSION_TTL,
                        httponly=True, samesite="lax",
                        secure=_cookie_secure(request), path="/")


# --------------------------------------------------------------------------- #
# 静态资源
# --------------------------------------------------------------------------- #
@app.get("/")
def index(request: Request):
    return _serve_page("index.html", request, with_entry=True)


@app.get("/index.css")
def css():
    """历史路径。真实样式表在 /assets/css/app.css。

    早期版本的桩页面引用过 /index.css，文件早已不存在 → 500。
    这里保留 301 重定向，避免任何遗留链接或浏览器缓存拿到 500。
    """
    return RedirectResponse("/assets/css/app.css", status_code=301)


@app.get("/index.js")
def js():
    """历史路径。真实脚本在 /assets/js/app.js。"""
    return RedirectResponse("/assets/js/app.js", status_code=301)


@app.get("/form-storage.js")
def fsjs():
    """历史路径。真实实现已并入 /assets/js/common.js。"""
    return RedirectResponse("/assets/js/common.js", status_code=301)


@app.get("/langs/{name}")
def langs(name: str):
    """语言包。name 只取基名并用 safe_under 收口（原先仅 basename，形同虚设）。"""
    bl = os.path.basename(name or "")
    p = safe_under(os.path.join(WEB, "langs"), bl)
    if p and os.path.isfile(p):
        return FileResponse(p, media_type="application/json")
    raise HTTPException(404)


# 元数据 API：直接从本地离线数据集服务
@app.get("/json/v1/{path:path}")
def jsonapi(path: str):
    """离线数据集读取。

    ⚠ 这里原先直接 os.path.join(DATA,"json","v1",path)，配合 Starlette 传入的
    未归一化路径，`/json/v1/../../../config.json` 可读出项目配置（已实测）。
    现在用 safe_under 强制约束在数据集目录内。
    """
    full = safe_under(os.path.join(DATA, "json", "v1"), path or "")
    if full and os.path.isfile(full) and full.endswith(".json"):
        return FileResponse(full, media_type="application/json")
    raise HTTPException(404)


# --------------------------------------------------------------------------- #
# 构建 API（严格对齐上游契约）
# --------------------------------------------------------------------------- #
@app.options("/api/v1/build")
def build_options(request: Request, response: Response):
    u = verify_ng_value(request.headers.get("Ng-One-Time-Verif-Value", ""))
    if not u:
        return JSONResponse({"detail": "invalid one-time verification"}, status_code=401)
    # 上游在 OPTIONS 阶段下发会话 cookie
    if not request.cookies.get(CFG["server"]["session_name"]):
        make_session(response, "")
    return Response(status_code=200, headers={"Allow": "POST, OPTIONS"})


@app.post("/api/v1/build")
async def build_post(request: Request, response: Response):
    raw = await request.body()
    if not verify_ng_value(request.headers.get("Ng-One-Time-Verif-Value", "")):
        return JSONResponse({"detail": "invalid one-time verification"}, status_code=401)
    try:
        req = json.loads(raw or b"{}")
    except Exception:
        return JSONResponse({"detail": "bad json", "stderr": "invalid payload"}, status_code=400)

    if not req.get("target") or not req.get("profile"):
        return JSONResponse({"detail": "bad profile", "stderr": "target/profile 必填"}, status_code=400)

    # 入参白名单：target/profile/packages/... 会流向 shell 与 make，
    # 这里必须净化（详见 app/params.py 的说明与已复现的注入面）。
    try:
        req = params.sanitize(req)
    except params.BuildParamError as e:
        return JSONResponse({"detail": str(e), "stderr": "invalid build params",
                             "error_code": "BAD_PARAM"}, status_code=400)
    except Exception as e:
        return JSONResponse({"detail": f"参数校验失败: {type(e).__name__}",
                             "stderr": "invalid build params"}, status_code=400)

    u = current_user(request)
    q = builder.get_queue()

    # 管理员可停用构建引擎（前台立即生效）
    if not BUILD_CFG()["enabled"] and not (u and u.get("role") == "admin"):
        return JSONResponse({"detail": "站点维护中：构建服务已由管理员暂停",
                             "stderr": "builder disabled"}, status_code=503)

    # VIP 通道 / 配额限制（配额可由管理员按用户或全局调整）
    limit = int(get_setting("default_quota", "12"))
    if u and u.get("quota") is not None:
        limit = int(u["quota"])
    n_app = [p for p in req["packages"] if p.startswith("luci-app-")]
    if not (u and u["sponsor"]) and len(n_app) > limit:
        return JSONResponse(
            {"detail": f"为缓解服务器压力, luci-app插件限定{limit}个, 更多软件包需赞助后解锁.",
             "stderr": "package quota exceeded"}, status_code=400)

    # 站点配置：是否允许匿名构建
    if not (u and u.get("role") == "admin"):
        if not SS.get("build_allow_anonymous") and not u:
            return JSONResponse({"detail": "本站已关闭匿名构建，请先登录",
                                 "stderr": "login required"}, status_code=401)

    # 默认分支（管理员可配置）
    if not req.get("version"):
        req["version"] = SS.get("build_default_version") or "25.12"

    # 设备 profile 预检 —— 提前拦掉上游 ImageBuilder 根本不提供的设备。
    #
    # ★ 背景：站点的设备库来自 kwrt 数据集，实测 997 台里有 107 台（10.7%）
    #   上游官方 ImageBuilder 编不出来，它们的 profile 在 profiles.json 里不存在。
    #   不提前拦的后果是：用户等一整轮 CI（几分钟到十几分钟）看到失败，
    #   而失败文案是「请稍后重试或更换软件包组合」—— 重试永远不会成功，
    #   这句兜底文案把人往错方向上带。实测事故见 reports/19。
    #
    # ★ 必须用 pick_version()（与真实构建同一条解析路径）：勾选仅第三方 feed
    #   提供的插件时，它会把版本回落到 24.10，profile 得在那个 release 里
    #   存在才算数。直接用 req["version"] 会在这种情形下误判。
    #
    # ★ 取不到 profiles.json 时**放行**（imagebuilder_profiles 返回 None）：
    #   这是提前告知，不是准入控制，镜像站抖动不该把正常构建拦掉。
    try:
        rel = builder.pick_version(req["target"], req["version"], req["packages"])["release"]
        supported = builder.imagebuilder_profiles(rel, req["target"])
        if supported is not None and req["profile"] not in supported:
            return JSONResponse({
                "detail": (f"设备 {req['profile']} 不在官方 ImageBuilder 的 "
                           f"{req['target']}（{rel}）里，本站无法在线编译它。"
                           f"该平台可在线编译的设备共 {len(supported)} 个，"
                           f"请更换设备，或直接下载该设备的官方预编译镜像。"),
                "stderr": "profile not in upstream ImageBuilder",
                "error_code": "PROFILE_UNSUPPORTED",
                "supported_profiles": sorted(supported),
            }, status_code=400)
    except Exception as e:                                        # noqa: BLE001
        print(f"[builder] profile 预检异常，放行本次构建: {type(e).__name__}: {e}", flush=True)

    job = q.submit(req, meta={"username": (u or {}).get("username", ""),
                              "email": (u or {}).get("email") or req.get("email") or ""})
    with db() as c:
        c.execute("INSERT OR REPLACE INTO builds(request_hash,username,target,profile,packages,status,created,payload) "
                  "VALUES(?,?,?,?,?,?,?,?)",
                  (job["request_hash"], (u or {}).get("username", ""), req.get("target"),
                   req.get("profile"), " ".join(req["packages"]), "queued",
                   time.time(), json.dumps(req)[:200000]))

    if not CFG["builder"]["enabled"]:
        return JSONResponse({"detail": "builder disabled"}, status_code=503)

    # 异步队列：立刻返回 202，前端轮询 /api/v1/build/<hash>
    return JSONResponse({
        "request_hash": job["request_hash"],
        "detail": "queued",
        "queue_position": job["queue_position"],
        "imagebuilder_status": "排队中",
    }, status_code=202)


@app.post("/api/v1/build/{hash_}/files")
async def build_attach_files(hash_: str, request: Request):
    """把已上传的自定义文件包绑定到指定构建任务。

    与 /api/v1/upload 分两步：先建构建拿到 hash，再上传文件，最后调这里绑定。
    只更新数据库里的 payload，不重新入队 —— 否则同一个定制会变成两个构建任务。
    仅在任务尚未开跑时允许绑定（队列一旦取走任务，payload 已定）。
    """
    u = current_user(request)
    rh = re.sub(r"[^A-Za-z0-9_-]", "", hash_)[:64]
    try:
        body = await request.json()
    except Exception:
        body = {}
    files_path = os.path.basename(str((body or {}).get("files_path") or ""))
    if not files_path:
        return JSONResponse({"detail": "缺少 files_path"}, status_code=400)

    with db() as c:
        row = c.execute("SELECT * FROM builds WHERE request_hash=?", (rh,)).fetchone()
    if not row:
        return JSONResponse({"detail": "构建任务不存在"}, status_code=404)
    owner = row["username"] or ""
    if owner != (u or {}).get("username", "") and not (u and u.get("role") == "admin"):
        return JSONResponse({"detail": "无权修改他人构建"}, status_code=403)

    j = builder.get_queue().jobs.get(rh)
    if j and j.get("status") not in ("queued", "pending"):
        return JSONResponse({"detail": f"任务已开始（{j.get('status')}），无法再附加文件"},
                            status_code=409)

    # 文件必须真实存在于该任务的目录下
    fp = os.path.join(UPLOADS, rh, files_path)
    if not os.path.isfile(fp):
        return JSONResponse({"detail": "上传文件不存在，请先上传"}, status_code=404)

    try:
        payload = json.loads(row["payload"] or "{}")
    except Exception:
        payload = {}
    payload["files_path"] = files_path
    with db() as c:
        c.execute("UPDATE builds SET payload=? WHERE request_hash=?",
                  (json.dumps(payload)[:200000], rh))
    if j:
        j.setdefault("req", {})["files_path"] = files_path   # 内存队列同步，避免重启前不生效
    audit((u or {}).get("username", ""), "build_attach_files", rh, files_path,
          client_ip(request))
    return {"status": "ok", "request_hash": rh, "files_path": files_path}


@app.get("/api/v1/build/{hash_}")
def build_status(hash_: str):
    q = builder.get_queue()
    j = q.get(hash_)
    if not j:
        with db() as c:
            r = c.execute("SELECT * FROM builds WHERE request_hash=?", (hash_,)).fetchone()
        if r:
            # 先从 jobs 表补充 stderr/stdout/detail
            js = jobs.get(hash_)
            if js:
                return {k: v for k, v in js.items() if k != "req"}
            return {"request_hash": hash_, "status": r["status"], "detail": "unknown"}
        # 回退到构建引擎的持久化任务表
        js = jobs.get(hash_)
        if js:
            return {k: v for k, v in js.items() if k != "req"}
        raise HTTPException(404)
    # 与主服务记录同步状态（供历史查询与列表页使用）
    with db() as c:
        c.execute("UPDATE builds SET status=? WHERE request_hash=?", (j["status"], hash_))
    out = {"request_hash": hash_, "status": j["status"]}
    for k in ("files", "packages", "stdout", "stderr", "detail", "store_url",
              "queue_position", "duration", "target", "profile", "version",
              "imagebuilder_status", "download_links", "link_ttl_hours",
              "links_expire_at", "mail_status", "external", "gh_run_url", "mirror"):
        if j.get(k) is not None:
            out[k] = j[k]
    # 未签发过链接的历史任务：按需补签（保证产物始终有可用链接）
    if j.get("status") == "done" and not j.get("download_links") and j.get("files"):
        try:
            ttl = int(SS.get("download.link_ttl_hours") or 72)
            links = []
            for f in j["files"]:
                if f.get("name"):
                    tok = dl.issue(hash_, f["name"], username=j.get("username", ""),
                                   ttl_hours=ttl)
                    links.append({"name": f["name"], "size": f.get("size") or 0,
                                  "url": _download_base() + "/dl/t/" + tok,
                                  "path": "/dl/t/" + tok})
            if links:
                j["download_links"] = links
                j["link_ttl_hours"] = ttl
                j["links_expire_at"] = time.time() + ttl * 3600
                out["download_links"] = links
                out["link_ttl_hours"] = ttl
        except Exception as e:
            print("[dl] 补签失败:", e, flush=True)
    return out


@app.get("/api/v1/user")
def api_user(request: Request):
    u = current_user(request)
    if not u:
        return {"logged_in": False}
    return {"logged_in": True, **u}


# --------------------------------------------------------------------------- #
# 账户
# --------------------------------------------------------------------------- #
@app.get("/login/", response_class=HTMLResponse)
def login_page(request: Request):
    return _serve_page("login.html", request, with_entry=True)


@app.post("/api/v1/login")
async def api_login(response: Response, request: Request, username: str = Form(...),
                    password: str = Form(...), captcha_id: str = Form(""),
                    captcha_code: str = Form("")):
    ip = client_ip(request) or "-"
    # 验证码：放在**任何密码比对之前** —— 否则撞库请求仍会消耗一次密码校验，
    # 验证码就只是多一步而不是真正的门。
    if captcha.enabled():
        ok, why = captcha.verify(captcha_id, captcha_code)
        if not ok:
            audit(username or "-", "login_captcha_failed", "", why, ip)
            return JSONResponse({"status": "error", "detail": why, "captcha": True},
                                status_code=400)
    # 失败锁定：账号与来源 IP 双维度计数，防在线暴力破解（原先无任何限速）
    if _login_locked(username) or _login_locked("ip:" + ip):
        audit(username or "-", "login_locked", "", "失败次数过多，已临时锁定", ip)
        return JSONResponse({"status": "error",
                             "detail": f"失败次数过多，请 {LOGIN_LOCK_SECONDS // 60} 分钟后再试"},
                            status_code=429)

    with db() as c:
        r = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    passed, need_upgrade = (False, False)
    if r:
        passed, need_upgrade = _verify_pw(password, r["password"])
    if not r or not passed:
        _login_fail(username)
        _login_fail("ip:" + ip)
        audit(username or "-", "login_failed", "", "密码错误", ip)
        return JSONResponse({"status": "error", "detail": "用户名或密码错误"}, status_code=403)
    # 登录成功清空计数
    _login_reset(username)
    _login_reset("ip:" + ip)
    # 老的无盐哈希在登录成功后自动升级为 PBKDF2
    if need_upgrade:
        with db() as c:
            c.execute("UPDATE users SET password=? WHERE username=?", (_hash_pw(password), username))
    if r["disabled"]:
        return JSONResponse({"status": "error", "detail": "该账户已被管理员停用"}, status_code=403)
    if is_banned(client_ip(request)) or is_banned(username):
        return JSONResponse({"status": "error", "detail": "该来源已被管理员封禁"}, status_code=403)
    # 邮箱未验证：密码校验通过后才提示，避免向陌生人泄露账号状态
    if "email_verified" in r.keys() and not r["email_verified"]:
        audit(username, "login_blocked_unverified", "", "邮箱未验证", client_ip(request))
        return JSONResponse({"status": "error", "detail": "邮箱尚未验证，请先完成邮箱验证",
                             "unverified": True, "email": r["email"] or ""},
                            status_code=403)
    with db() as c:
        c.execute("UPDATE users SET last_login=? WHERE username=?", (time.time(), username))
    # 会话固定防护：登录成功后作废旧会话，换发新令牌
    old_tok = request.cookies.get(CFG["server"]["session_name"])
    if old_tok:
        with db() as c:
            c.execute("DELETE FROM sessions WHERE token=?", (old_tok,))
    make_session(response, username, request)
    role = r["role"] or "user"
    if role == "admin":
        audit(username, "login", "", "管理员登录", client_ip(request))
    return {"status": "ok", "username": username, "sponsor": bool(r["sponsor"]),
            "role": role, "is_admin": role == "admin"}


@app.post("/api/v1/register")
async def api_register(response: Response, request: Request, username: str = Form(...),
                       password: str = Form(...), email: str = Form(""),
                       captcha_id: str = Form(""), captcha_code: str = Form("")):
    # 注册验证码：独立开关（security.captcha_on_register），
    # 与登录开关分开 —— 很多站点只想挡批量注册，不想给登录加步骤。
    if captcha.enabled_for_register():
        ok, why = captcha.verify(captcha_id, captcha_code)
        if not ok:
            audit(username or "-", "register_captcha_failed", "", why, client_ip(request) or "-")
            return JSONResponse({"status": "error", "detail": why, "captcha": True},
                                status_code=400)
    if len(username) < 3 or len(password) < 6:
        return JSONResponse({"status": "error", "detail": "用户名≥3位，密码≥6位"}, status_code=400)
    if not re.fullmatch(r"[A-Za-z0-9_.\-]{3,32}", username):
        return JSONResponse({"status": "error", "detail": "用户名仅允许字母数字._-（3-32位）"},
                            status_code=400)
    # 保留用户名：防止注册 admin 之类的名字，配合 role 逻辑造成提权或混淆
    if username.lower() in RESERVED_NAMES:
        return JSONResponse({"status": "error", "detail": "该用户名为系统保留，请换一个"},
                            status_code=400)
    if get_setting("registration_open", "1") == "0":
        return JSONResponse({"status": "error", "detail": "站点已由管理员关闭注册"}, status_code=403)
    if is_banned(client_ip(request)):
        return JSONResponse({"status": "error", "detail": "该来源已被管理员封禁"}, status_code=403)
    # 来源限速：同一 IP 每小时最多 5 次注册尝试（原先完全没有限速，
    # 实测可无限批量注册账号）
    ok_r, wait_r = rate_ok("register", client_ip(request) or "-", 5, 3600)
    if not ok_r:
        return JSONResponse({"status": "error",
                             "detail": f"注册过于频繁，请 {wait_r} 秒后再试"},
                            status_code=429)

    need_verify = bool(SS.get("mail.verify_register"))
    email = (email or "").strip()
    if need_verify:
        if not email or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            return JSONResponse({"status": "error", "detail": "本站已开启邮箱验证，请填写有效的邮箱地址"},
                                status_code=400)
        # 开启了验证却发不出信，是配置错误 —— 明确报错，不放行
        if not mailer.ready():
            return JSONResponse({"status": "error",
                                 "detail": f"站点开启了邮箱验证但当前无法发信（{mailer.why_not_ready()}），"
                                           "请联系管理员"},
                                status_code=503)

    with db() as c:
        dup = c.execute("SELECT email_verified FROM users WHERE username=?", (username,)).fetchone()
    if dup:
        if need_verify and not dup["email_verified"]:
            return JSONResponse({"status": "error", "detail": "该用户名已注册但未验证邮箱，"
                                                             "请重新发送验证邮件", "unverified": True},
                                status_code=409)
        return JSONResponse({"status": "error", "detail": "用户名已存在"}, status_code=409)

    try:
        with db() as c:
            # ★ 必须走 _hash_pw（PBKDF2-SHA256 + 每用户随机盐）。
            # 这里原来直接写 hashlib.sha256(password) —— 无盐、单轮，等于把
            # 口令以「可秒级爆破」的形式落库：彩虹表可直接查、GPU 每秒几十亿次、
            # 相同口令哈希完全相同（一撞一片）。实测库中注册用户正是 64 位无盐
            # hex，而 admin/改密路径已是 pbkdf2_sha256$240000$<盐>$<哈希>，
            # 属实现不一致造成的降级。登录时会自动升级旧哈希，但**从未登录过的
            # 用户会一直是无盐 SHA256**，一旦库被拖走即全部可破。
            c.execute("INSERT INTO users(username,password,email,sponsor,role,created,email_verified) "
                      "VALUES(?,?,?,0,'user',?,?)",
                      (username, _hash_pw(password), email,
                       time.time(), 0 if need_verify else 1))
    except sqlite3.IntegrityError:
        return JSONResponse({"status": "error", "detail": "用户名已存在"}, status_code=409)

    if not need_verify:
        make_session(response, username, request)
        return {"status": "ok", "username": username, "sponsor": False,
                "role": "user", "is_admin": False, "verified": True}

    ok, detail = _send_verify_mail(username, email, request)
    return {"status": "pending_verification", "username": username, "email": email,
            "mail_ok": ok, "detail": detail,
            "message": "验证邮件已发送，请查收并点击链接完成注册" if ok
                       else "账号已创建，但验证邮件发送失败，请在登录页重新发送"}


def _send_verify_mail(username: str, email: str, request=None) -> tuple[bool, str]:
    """签发令牌并发送验证邮件。返回 (是否成功, 说明)。"""
    hours = float(str(SS.get("mail.verify_ttl_hours") or 24))
    token = verify.issue(username, email, hours, client_ip(request) if request else "")
    base = _site_base_url(request)
    link = verify.build_link(base, token)
    ctx = {"username": username, "email": email, "link": link,
           "hours": f"{hours:g}", "site": SS.get("site_name") or "本站"}
    subject = mailer.render(SS.get("mail.verify_subject") or "[Kwrt] 请验证你的邮箱", ctx)
    body = mailer.render(SS.get("mail.verify_body") or "请点击链接验证：{link}", ctx)
    # 纯文本仍作为 multipart/alternative 的降级分支保留；HTML 走模板
    html = mailer.verify_html(ctx["site"], username, link, hours)
    ok, detail = mailer.send(email, subject, body, html)
    verify.log_send("verify", email, username, ok, detail)
    return ok, detail


HOST_RE = re.compile(r"^[A-Za-z0-9.-]+(?::[0-9]{1,5})?$")


def _site_base_url(request=None) -> str:
    """推导站点绝对地址，用于拼接验证/回调链接与支付宝 notify_url。

    ★ 原实现会采信请求头里的 Host / X-Forwarded-Host —— 两者都由客户端控制，
      于是**发给用户的邮件里会出现指向攻击者域名的链接**（钓鱼 → 接管），
      支付宝 notify_url 也能被劫持。仅校验「格式合法」挡不住这个。

    现在统一走 netcfg.base_url：
      优先级 = site.base_url → config.json 的 site.base_url → **绑定的域名**
               → 通过白名单校验的请求 Host → 监听地址兜底
      Host 兜底**之前必须**过 host_allowed()，否则仍是注入。
    """
    return netcfg.base_url(request)


@app.get("/verify/", response_class=HTMLResponse)
def page_verify(token: str = ""):
    return _serve_page("verify.html", None, with_entry=False)


@app.post("/api/v1/verify_email")
def api_verify_email(token: str = Form("")):
    ok, username, why = verify.consume(token)
    if not ok:
        return JSONResponse({"status": "error", "detail": why}, status_code=400)
    with db() as c:
        row = c.execute("SELECT sponsor,role FROM users WHERE username=?", (username,)).fetchone()
    return {"status": "ok", "username": username, "verified": True,
            "sponsor": bool(row["sponsor"]) if row else False,
            "is_admin": bool(row and row["role"] == "admin")}


@app.post("/api/v1/resend_verify")
def api_resend_verify(request: Request, username: str = Form("")):
    """重发验证邮件（不泄露「用户是否存在」之外的信息，但仍需防滥用）。"""
    username = (username or "").strip()
    if not username:
        return JSONResponse({"status": "error", "detail": "请填写用户名"}, status_code=400)
    if is_banned(client_ip(request)):
        return JSONResponse({"status": "error", "detail": "该来源已被管理员封禁"}, status_code=403)
    # 来源限速：同一 IP 10 分钟最多 5 次（原先只有 60 秒的「同账号」冷却，
    # 攻击者遍历用户名即可持续触发发信 = 邮件轰炸）
    ok_r, wait_r = rate_ok("resend", client_ip(request) or "-", 5, 600)
    if not ok_r:
        return JSONResponse({"status": "error",
                             "detail": f"请求过于频繁，请 {wait_r} 秒后再试"},
                            status_code=429)
    with db() as c:
        row = c.execute("SELECT email, email_verified FROM users WHERE username=?",
                        (username,)).fetchone()
    if not row:
        return JSONResponse({"status": "error", "detail": "用户名不存在"}, status_code=404)
    if row["email_verified"]:
        return {"status": "ok", "detail": "该账号已验证，可直接登录"}
    if not (row["email"] or "").strip():
        return JSONResponse({"status": "error", "detail": "该账号未登记邮箱，请联系管理员"},
                            status_code=400)
    if not mailer.ready():
        return JSONResponse({"status": "error",
                             "detail": f"当前无法发信（{mailer.why_not_ready()}），请联系管理员"},
                            status_code=503)
    # 冷却：60 秒内不重复发
    p = verify.pending(username)
    if p and (time.time() - p["created"]) < 60:
        wait = int(60 - (time.time() - p["created"]))
        return JSONResponse({"status": "error", "detail": f"发送过于频繁，请 {wait} 秒后再试"},
                            status_code=429)
    ok, detail = _send_verify_mail(username, row["email"], request)
    if not ok:
        return JSONResponse({"status": "error", "detail": f"发送失败：{detail}"}, status_code=502)
    return {"status": "ok", "detail": f"验证邮件已重新发送至 {row['email']}"}


# --------------------------------------------------------------------------- #
# 找回密码（忘记密码）
# --------------------------------------------------------------------------- #
# 统一的对外回复：**不区分**账号是否存在、是否登记邮箱、是否真的发出去了。
# 这是防用户名/邮箱枚举的关键 —— 任何差异化提示都等于给攻击者一个免费的用户清单接口。
RESET_REPLY = "如果该账号存在且已绑定邮箱，重置邮件已经发出，请查收（含垃圾邮件目录）。"


def _send_reset_mail(username: str, email: str, request=None) -> tuple[bool, str]:
    """签发找回令牌并发送重置邮件。返回 (是否成功, 说明)。"""
    hours = float(str(SS.get("mail.reset_ttl_hours") or 2))
    token = reset.issue(username, email, hours, client_ip(request) if request else "")
    base = _site_base_url(request)
    link = reset.build_link(base, token)
    ctx = {"username": username, "email": email, "link": link,
           "hours": f"{hours:g}", "site": SS.get("site_name") or "本站"}
    subject = mailer.render(SS.get("mail.reset_subject") or "[Kwrt] 重置你的登录密码", ctx)
    body = mailer.render(SS.get("mail.reset_body") or "请点击链接重置密码：{link}", ctx)
    html = mailer.reset_html(ctx["site"], username, link, hours)
    ok, detail = mailer.send(email, subject, body, html)
    verify.log_send("reset", email, username, ok, detail)
    return ok, detail


@app.get("/reset/", response_class=HTMLResponse)
def page_reset():
    return _serve_page("reset.html", None, with_entry=False)


@app.post("/api/v1/reset_request")
def api_reset_request(request: Request, account: str = Form(""),
                      captcha_id: str = Form(""), captcha_code: str = Form("")):
    """申请重置密码。

    ★ 反枚举：除了「站点级」的失败（验证码错、被限速、SMTP 没配）之外，
      一律返回同一句 RESET_REPLY。账号维度的差异（不存在 / 无邮箱 / 冷却中）
      **只用日志记录，不回显**。
    """
    ip = client_ip(request) or "-"
    account = (account or "").strip()

    # 1) 验证码 —— 放在任何查询与发信之前。
    #    这个接口会真实发信，没有验证码就是现成的邮件轰炸放大器。
    if captcha.enabled_for_reset():
        ok_c, why_c = captcha.verify(captcha_id, captcha_code)
        if not ok_c:
            audit(account or "-", "reset_captcha_failed", "", why_c, ip)
            return JSONResponse({"status": "error", "detail": why_c, "captcha": True},
                                status_code=400)

    if not account:
        return JSONResponse({"status": "error", "detail": "请填写用户名或邮箱"},
                            status_code=400)

    # 2) 来源封禁与限速（站点级，对所有账号一视同仁，不构成枚举面）
    if is_banned(ip):
        return JSONResponse({"status": "error", "detail": "该来源已被管理员封禁"},
                            status_code=403)
    ok_r, wait_r = rate_ok("reset", ip, 5, 3600)
    if not ok_r:
        return JSONResponse({"status": "error",
                             "detail": f"请求过于频繁，请 {wait_r} 秒后再试"},
                            status_code=429)

    # 3) SMTP 没配好时明确报错 —— 这是**站点级**状态，对任何账号都一样，
    #    不泄露账号是否存在；反过来若静默返回成功，用户会一直等一封永远不来的信。
    if not mailer.ready():
        return JSONResponse({"status": "error",
                             "detail": f"本站邮件服务尚未配置（{mailer.why_not_ready()}），"
                                       "请联系管理员重置密码"},
                            status_code=503)

    # 4) 查账号（用户名 或 邮箱，邮箱忽略大小写）
    with db() as c:
        row = c.execute("SELECT username,email,disabled FROM users "
                        "WHERE username=? OR (email<>'' AND LOWER(email)=LOWER(?)) LIMIT 1",
                        (account, account)).fetchone()
    if not row:
        audit(account, "reset_request_unknown", account, "账号不存在（对外统一回复）", ip)
        return {"status": "ok", "message": RESET_REPLY}
    username = row["username"]
    email = (row["email"] or "").strip()
    if row["disabled"]:
        audit(account, "reset_request_disabled", username, "账号已停用", ip)
        return {"status": "ok", "message": RESET_REPLY}
    if not email:
        audit(account, "reset_request_no_email", username, "未登记邮箱", ip)
        return {"status": "ok", "message": RESET_REPLY}

    # 5) 账号冷却：60 秒内不重复发。
    #    ★ 这里**不能**返回 429 —— 那会让「存在且刚申请过」与「不存在」可区分。
    #      静默跳过发送，对外仍是同一句话。
    left = reset.cooldown_left(username)
    if left > 0:
        audit(account, "reset_request_cooldown", username, f"冷却中，剩余 {left}s", ip)
        return {"status": "ok", "message": RESET_REPLY}

    ok, detail = _send_reset_mail(username, email, request)
    if not ok:
        # 发信失败对**该账号**是可观察的，但把它藏起来会让用户白等，
        # 且失败原因（SMTP 拒绝/网络不通）与账号是否存在无关，不构成枚举面。
        audit(account, "reset_send_failed", username, detail, ip)
        return JSONResponse({"status": "error", "detail": f"邮件发送失败：{detail}"},
                            status_code=502)
    audit(account, "reset_request_sent", username, f"已发送至 {email}", ip)
    return {"status": "ok", "message": RESET_REPLY}


@app.post("/api/v1/reset_check")
def api_reset_check(request: Request, token: str = Form("")):
    """校验重置令牌（不消费），用于让用户在填新密码前知道在为哪个账号操作。

    只暴露用户名，不暴露邮箱、角色等其它字段 —— 链接本身即凭据，
    拿到链接的人本来就能改密码，所以这里没有额外提权。
    """
    ok, username, why = reset.check(token)
    if not ok:
        return JSONResponse({"status": "error", "detail": why}, status_code=400)
    return {"status": "ok", "username": username,
            "ttl_hours": float(str(SS.get("mail.reset_ttl_hours") or 2))}


@app.post("/api/v1/reset_confirm")
def api_reset_confirm(response: Response, request: Request,
                      token: str = Form(""), password: str = Form("")):
    """用令牌设置新密码。

    顺序很关键：
      1. **先原子消费令牌** —— 并发提交同一个链接时只有一方能过，
         另一方拿到明确提示，而不是「两边都改成功」。
      2. 再改口令。
      3. **注销该账号全部会话** —— 改密码的语义就是「其它地方的登录全部失效」，
         否则被盗号者只要不刷新就能继续用旧会话。
      4. 作废该账号其余的找回令牌（旧邮件里的链接一并失效）。
      5. 清掉登录失败锁定计数（否则改完密码可能还被锁着）。
    """
    ip = client_ip(request) or "-"

    if len(password or "") < 6:
        return JSONResponse({"status": "error", "detail": "密码至少 6 位"}, status_code=400)

    # 令牌爆破/暴力尝试限速（令牌 256 bit，此处只是兜底）
    ok_r, wait_r = rate_ok("reset_confirm", ip, 20, 3600)
    if not ok_r:
        return JSONResponse({"status": "error",
                             "detail": f"尝试过于频繁，请 {wait_r} 秒后再试"},
                            status_code=429)

    ok, username, why = reset.consume(token)
    if not ok:
        audit("-", "reset_confirm_failed", "", why, ip)
        return JSONResponse({"status": "error", "detail": why}, status_code=400)

    with db() as c:
        cur = c.execute("UPDATE users SET password=? WHERE username=?",
                        (_hash_pw(password), username))
        if not cur.rowcount:
            audit(username, "reset_confirm_failed", username, "账号已不存在", ip)
            return JSONResponse({"status": "error", "detail": "账号不存在，请联系管理员"},
                                status_code=400)
        c.execute("DELETE FROM sessions WHERE username=?", (username,))

    reset.invalidate(username)          # 作废其余找回令牌
    verify.invalidate(username)         # 顺手作废未用的邮箱验证令牌
    _login_reset(username)              # 清失败计数
    _login_reset("ip:" + ip)
    audit(username, "password_reset", username, "通过邮件链接重置密码", ip)

    # 自动登录：用户刚证明了自己掌握邮箱，且口令是他自己刚设的
    make_session(response, username, request)
    with db() as c:
        row = c.execute("SELECT sponsor,role FROM users WHERE username=?",
                        (username,)).fetchone()
    return {"status": "ok", "username": username,
            "sponsor": bool(row["sponsor"]) if row else False,
            "is_admin": bool(row and row["role"] == "admin")}


@app.post("/api/v1/logout")
def api_logout(response: Response, request: Request):
    tok = request.cookies.get(CFG["server"]["session_name"])
    if tok:
        with db() as c:
            c.execute("DELETE FROM sessions WHERE token=?", (tok,))
    response.delete_cookie(CFG["server"]["session_name"], path="/")
    return {"status": "ok"}


@app.post("/api/v1/sponsor")
def api_sponsor(request: Request):
    """【已停用的自助置位入口】

    原实现允许任何登录用户直接把自己置为赞助态，等价于「免费解锁全部付费权益」。
    任何真实部署都不能保留这种端点 —— 现在一律拒绝，权益只能通过这些路径产生：
      · 支付宝回调 / 主动查单（服务端验签后置位）
      · 管理员在后台发放
      · /api/v1/sponsor/claim（受 sponsor.auto_approve 开关约束）
    """
    u = current_user(request)
    if not u:
        return JSONResponse({"detail": "login required"}, status_code=401)
    audit(u["username"], "sponsor_self_activate_blocked", u["username"],
          "自助置位接口已停用", client_ip(request))
    return JSONResponse(
        {"detail": "该入口已停用。请通过「在线支付」或联系管理员开通赞助权益。",
         "code": "SELF_ACTIVATE_DISABLED"}, status_code=403)


# --------------------------------------------------------------------------- #
# 自定义文件包上传
# --------------------------------------------------------------------------- #
UPLOADS = os.path.join(STORE, "uploads")
os.makedirs(UPLOADS, exist_ok=True)


@app.post("/api/v1/upload")
async def api_upload(request: Request, file: UploadFile = File(...),
                     request_hash: str = Form("")):
    """赞助用户上传自定义文件包（构建时解压覆盖到固件 files/ 目录）。

    两种用法：
      ① 不传 request_hash —— 「先上传、后提交」：文件落到该用户的暂存目录，
         返回可用的 files_path；随后提交构建时带上它即可。这是前端的默认路径，
         因为队列几乎立刻就取走任务，等提交完再上传必然赶不上。
      ② 传 request_hash —— 「先提交、后上传」：直接挂到该任务上，任务须仍在排队。

    安全要点：
      · 只接受白名单扩展名，且只取 basename（防路径穿越）
      · 限制单文件大小，防止磁盘被灌满
      · 任务模式下列/写的构建必须归属当前用户（管理员可代办）
    """
    u = current_user(request)
    if not (u and u["sponsor"]):
        return JSONResponse({"status": "error", "detail": "仅限赞助用户"}, status_code=403)

    name = os.path.basename(file.filename or "files.tar.gz")
    if not name.endswith((".zip", ".7z", ".tar.gz", ".tgz")):
        return JSONResponse({"status": "error",
                             "detail": "仅支持 .zip/.7z/.tar.gz/.tgz"}, status_code=400)

    rh = re.sub(r"[^A-Za-z0-9_-]", "", (request_hash or ""))[:64]
    if rh:
        # 模式 ②：挂到指定构建
        with db() as c:
            row = c.execute("SELECT username,status FROM builds WHERE request_hash=?",
                            (rh,)).fetchone()
        if not row:
            return JSONResponse({"status": "error", "detail": "构建任务不存在"}, status_code=404)
        owner = row["username"] or ""
        if owner != u["username"] and u.get("role") != "admin":
            return JSONResponse({"status": "error", "detail": "无权为他人构建上传文件"},
                                status_code=403)
        if row["status"] not in ("queued", "pending", ""):
            return JSONResponse({"status": "error",
                                 "detail": f"该构建已进入 {row['status']} 状态，无法再上传文件"},
                                status_code=409)
        rel = rh
    else:
        # 模式 ①：暂存到该用户自己的目录，待提交构建时引用
        safe_u = re.sub(r"[^A-Za-z0-9_.-]", "_", u["username"])[:40] or "anon"
        rel = os.path.join("_staged", safe_u)

    limit = 200 * 1024 * 1024
    dst_dir = os.path.join(UPLOADS, rel)
    os.makedirs(dst_dir, exist_ok=True)
    dst = os.path.join(dst_dir, name)

    written = 0
    try:
        with open(dst, "wb") as f:
            while True:
                chunk = await file.read(1 << 20)
                if not chunk:
                    break
                written += len(chunk)
                if written > limit:
                    f.close()
                    os.remove(dst)
                    return JSONResponse(
                        {"status": "error", "detail": "文件超过 200 MB 上限"},
                        status_code=413)
                f.write(chunk)
    except Exception as e:
        try:
            os.remove(dst)
        except OSError:
            pass
        return JSONResponse({"status": "error", "detail": f"写入失败: {e}"}, status_code=500)

    # 对外统一用 POSIX 分隔符表示 files_path
    files_path = rel.replace(os.sep, "/") + "/" + name
    audit(u["username"], "upload", name,
          f"build={rh or '-'} size={written}", client_ip(request))

    attached = None
    note = ""
    if rh:
        # 模式 ②：顺手绑定到内存队列与库里的 payload
        try:
            j = builder.get_queue().jobs.get(rh)
            if j and j.get("status") not in ("queued", "pending"):
                note = f"构建已进入 {j.get('status')} 状态，文件包未能附加到本次构建"
                attached = False
            else:
                with db() as c:
                    row2 = c.execute("SELECT payload FROM builds WHERE request_hash=?",
                                     (rh,)).fetchone()
                try:
                    pl = json.loads((row2["payload"] if row2 else "") or "{}")
                except Exception:
                    pl = {}
                pl["files_path"] = name
                with db() as c:
                    c.execute("UPDATE builds SET payload=? WHERE request_hash=?",
                              (json.dumps(pl)[:200000], rh))
                if j:
                    j.setdefault("req", {})["files_path"] = name
                attached = True
        except Exception as e:
            attached = False
            note = f"自动附加失败：{e}"

    return {"status": "ok", "name": name, "size": written,
            "request_hash": rh, "files_path": files_path,
            "attached": attached, "note": note}


# --------------------------------------------------------------------------- #
# 站点信息（前台公开）与下载令牌
# --------------------------------------------------------------------------- #
@app.get("/api/v1/captcha")
def api_captcha(request: Request):
    """签发一张图形验证码。

    前端拿到 SVG 直接内联渲染（无需再请求图片），配套 id 用于提交时校验。
    返回 SVG 而不是二进制图片：不引入图形库依赖，且能跟着页面的缓存策略一起管理。
    """
    if not (captcha.enabled() or captcha.enabled_for_register()
            or captcha.enabled_for_reset()):
        return JSONResponse({"status": "error", "detail": "验证码未启用"}, status_code=404)
    try:
        d = captcha.issue(client_ip(request) or "")
    except RuntimeError as e:
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=429)
    return JSONResponse({"status": "ok", "id": d["id"], "svg": d["svg"],
                         "ttl": d["ttl"], "length": d["length"]},
                        headers={"Cache-Control": "no-store"})


@app.get("/api/v1/site")
def api_site(request: Request):
    """前台启动时拉取的站点信息（含品牌、文案、链接、赞助套餐、构建设置）。"""
    d = SS.public_values()
    # 版本号：后台头部/页脚都从 /api/v1/site 取，一处定义两处显示
    d["version"] = _version.display()
    # 验证码要求：前端据此决定登录/注册表单里是否显示验证码输入框。
    # 只暴露真假，不暴露任何内部参数。
    d["captcha"] = {"login": captcha.enabled(), "register": captcha.enabled_for_register(),
                    "reset": captcha.enabled_for_reset()}
    d["sponsor_tiers"] = SS.get("sponsor.tiers") or []
    d["currency"] = SS.get("sponsor.currency") or "CNY"
    d["build"] = backends.info()
    # 在线支付可用性 + 邮箱验证开关（均为「能力开关」，不含任何密钥）
    d["pay"] = {
        "available": pay.available(),
        "enabled": bool(SS.get("pay.alipay_enabled")),
        "poll_seconds": int(str(SS.get("pay.poll_seconds") or 3)),
        "order_ttl_minutes": int(str(SS.get("pay.order_ttl_minutes") or 15)),
    }
    d["mail_ready"] = mailer.ready()
    d["verify_register"] = bool(SS.get("mail.verify_register"))
    d["versions"] = [{"branch": b, "release": releases.resolve(b)["release"],
                      "backend": releases.resolve(b)["backend"]} for b in ("25.12", "24.10")]
    # 当前对外基址：绑域名 / 接 CDN 后，验证邮件链接、下载链接、支付回调都基于它。
    # 放出来便于运维核对（不含任何敏感值）。
    d["base_url"] = netcfg.base_url(request)
    d["cdn"] = {"enabled": netcfg.cdn_enabled(), "base": netcfg.cdn_base()}
    # 导航可见性：由后台「页面与入口」的逐页开关决定。前端据此隐藏入口 ——
    # 页面本身已被中间件拦成 404，这里只是让 UI 一致，避免「点了才被告知下线」。
    d["nav"] = {
        "home": netcfg.page_enabled("/"),
        "packages": netcfg.page_enabled("/packages/"),
        "newpkg": netcfg.page_enabled("/newpkg/"),
        "fadian": netcfg.page_enabled("/fadian/"),
        "contact": netcfg.page_enabled("/contact/"),
        "login": netcfg.page_enabled("/login/"),
        "register": bool(SS.get("page.register_enabled", True)),
        "reset": netcfg.page_enabled("/reset/"),
        "download": netcfg.page_enabled("/firmware/"),
        "app": bool(SS.get("entry.app_enabled")),
        "wechat": bool(SS.get("entry.wechat_enabled")),
    }
    return d


@app.get("/api/v1/announcement")
def announcement():
    """前台公告（兼容旧接口）。"""
    return {"announcement": SS.get("announcement") or "",
            "registration_open": SS.get("registration_open", "1") in (True, "1", "true")}


@app.get("/dl/mirror")
def dl_mirror(version: str, target: str, id: str, name: str, request: Request):
    """
    官方预编译镜像的下载中转。

    为什么要中转而不是直链上游：
      · 单源同域，避免跨域与 Referer 策略差异；
      · 可在本站统一记日志、限流、后续加校验。

    安全要点（防开放代理 / SSRF）：
      · 只允许在上游主机（硬编码）取文件，主机名不来自请求；
      · 请求的三元组（version/target/id/name）必须能在本地离线数据集里
        找到对应的镜像条目才放行 —— 不能凭请求任意拼路径；
      · 文件名拒绝任何路径分隔符与上跳段。
    """
    # 1) 文件名净化：只允许基名，无分隔符、无上跳
    base = os.path.basename(name)
    if base != name or "/" in name or "\\" in name or name.startswith("."):
        return JSONResponse({"detail": "非法的文件名"}, status_code=400)

    # 2) 组件净化：版本/目标/机型 ID 只允许安全字符集
    safe = re.compile(r"^[A-Za-z0-9._/-]{1,64}$")
    if not all(safe.match(x or "") for x in (version, target, id)):
        return JSONResponse({"detail": "非法的路径参数"}, status_code=400)
    if ".." in version or ".." in target or ".." in id:
        return JSONResponse({"detail": "非法的路径参数"}, status_code=400)

    # 3) 必须命中本地数据集中的镜像条目
    meta_path = os.path.join(DATA, "json", "v1", "releases", version,
                             "targets", target, id + ".json")
    if not os.path.isfile(meta_path):
        return JSONResponse({"detail": "未知的设备或版本"}, status_code=404)
    try:
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
    except Exception:
        return JSONResponse({"detail": "设备元数据读取失败"}, status_code=500)
    allowed = {im.get("name") for im in (meta.get("images") or []) if im.get("name")}
    if base not in allowed:
        return JSONResponse({"detail": "该文件不属于此设备"}, status_code=404)

    # 4) 组装上游地址（主机硬编码，路径由已校验组件拼成）
    url = f"{UPSTREAM_DL}/releases/{version}/targets/{target}/{base}"
    hdr = {"User-Agent": "XSP-Kwrt/1.0"}
    rng = request.headers.get("range")
    if rng and re.match(r"^bytes=\d*-\d*$", rng.strip()):
        hdr["Range"] = rng.strip()          # 透传 Range，支持断点续传
    try:
        up = urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=30)
    except urllib.error.HTTPError as e:
        return JSONResponse({"detail": f"上游返回 {e.code}"},
                            status_code=(404 if e.code == 404 else 502))
    except Exception as e:
        return JSONResponse({"detail": f"上游不可达：{type(e).__name__}"}, status_code=502)

    def stream():
        try:
            while True:
                chunk = up.read(256 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            up.close()

    out = {
        "Content-Disposition": f'attachment; filename="{base}"',
        "Accept-Ranges": "bytes",
    }
    if up.headers.get("Content-Range"):
        out["Content-Range"] = up.headers["Content-Range"]
    return StreamingResponse(
        stream(),
        status_code=(206 if up.status == 206 else 200),
        media_type=up.headers.get("Content-Type") or "application/octet-stream",
        headers=out)


@app.get("/dl/t/{token}")
def download_by_token(token: str, request: Request):
    """
    限时下载链接入口：校验签名/有效期/次数后返回文件。
    这是邮件与 API 中下发的链接形式。
    """
    ip = client_ip(request)
    ok, msg, row = dl.verify(token, consume=False, ip=ip)
    if not ok:
        return HTMLResponse(
            f"""<!DOCTYPE html><html lang="zh-Hans"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>下载链接不可用 · {SS.get('site_name') or 'Kwrt'}</title>
<link rel="stylesheet" href="/assets/css/app.css"></head><body>
<div class="container container-xs" style="padding-top:12vh">
  <div class="card"><div class="card-bd stack text-center">
    <div style="font-size:2.4rem">⚠</div>
    <h2>下载链接不可用</h2>
    <p class="text-muted">{msg}</p>
    <p class="fs-sm text-subtle">链接有效期由站点管理员设置。过期后请回到站点，
       在构建记录中重新生成下载链接。</p>
    <a class="btn btn-primary btn-block" href="/">返回首页</a>
  </div></div>
</div></body></html>""", status_code=410)

    # 需登录的站点：校验会话归属
    if SS.get("download.require_login"):
        u = current_user(request)
        owner = row["username"] or ""
        if not u:
            return RedirectResponse(f"/login/?next=/dl/t/{token}", status_code=302)
        if owner and u["username"] != owner and u.get("role") != "admin":
            return HTMLResponse("<h3>403</h3><p>该下载链接不属于当前账号。</p>", status_code=403)

    path = dl.resolve_path(row["request_hash"], row["filename"])
    if not path:
        return HTMLResponse("<h3>404</h3><p>产物文件已不存在（可能已被清理）。</p>",
                            status_code=404)

    dl.verify(token, consume=True, ip=ip)      # 计次
    ext = SS.get("download.external_host") or ""
    if ext and SS.get("download.serve_local") is False:
        return RedirectResponse(ext.rstrip("/") + f"/{row['request_hash']}/{row['filename']}",
                                status_code=302)
    return FileResponse(path, media_type="application/octet-stream",
                        filename=os.path.basename(row["filename"]))


@app.get("/api/v1/downloads")
def api_my_downloads(request: Request):
    """当前用户的有效下载链接（登录可见）。"""
    u = current_user(request)
    if not u:
        return JSONResponse({"detail": "请先登录"}, status_code=401)
    rows = dl.list_for(username=u["username"], include_expired=False, limit=200)
    for r in rows:
        r.pop("token", None)                  # 不回传令牌原文（防越权复制）
    return {"count": len(rows), "downloads": rows}


# --------------------------------------------------------------------------- #
# 赞助（金额/套餐由管理员配置）
# --------------------------------------------------------------------------- #
@app.get("/api/v1/sponsor/tiers")
def sponsor_tiers():
    return {"enabled": SS.get("sponsor.enabled"), "currency": SS.get("sponsor.currency") or "CNY",
            "note": SS.get("sponsor.note") or "", "tiers": SS.get("sponsor.tiers") or [],
            "pay_qr": SS.get("sponsor.pay_qr") or ""}


@app.post("/api/v1/sponsor/claim")
def sponsor_claim(request: Request, tier: str = Form(""), amount: float = Form(0),
                  note: str = Form(""), email: str = Form("")):
    """
    用户声明已完成赞助。
    自助模式（sponsor.auto_approve=true）立即生效；否则记为待审核由管理员确认。
    """
    u = current_user(request)
    if not u:
        return JSONResponse({"detail": "请先登录"}, status_code=401)
    tiers = SS.get("sponsor.tiers") or []
    chosen = None
    if tier:
        for t in tiers:
            if str(t.get("name")) == tier:
                chosen = t
                break
    # 金额与天数只能来自管理员配置的套餐，绝不采信前端的 amount/days。
    # 原实现 unmatched 时用 float(amount)，开启 auto_approve 后前端可自报任意
    # 金额，赞助统计与 sponsor_amount 归集都会被污染。
    if not chosen:
        return JSONResponse({"detail": "套餐不存在，请从页面列出的套餐中选择"}, status_code=400)
    days = int(chosen.get("days", 30))
    amt = float(chosen.get("amount") or 0)
    # 安全默认：未显式开启自动通过时，一律走人工审核
    auto = bool(SS.get("sponsor.auto_approve", False)) and SS.get("sponsor.auto_approve") in (True, "1", "true")
    now = time.time()
    if auto:
        until = now + days * 86400
        with db() as c:
            c.execute("UPDATE users SET sponsor=1, sponsor_until=?, sponsor_tier=?, "
                      "sponsor_amount=? WHERE username=?",
                      (until, (chosen or {}).get("name", "自定义"), amt, u["username"]))
            if email:
                c.execute("UPDATE users SET email=? WHERE username=? AND (email IS NULL OR email='')",
                          (email, u["username"]))
        audit(u["username"], "sponsor_claim", u["username"],
              f"tier={tier} amount={amt} auto=true", client_ip(request))
        return {"status": "ok", "sponsor": True, "until": until,
                "detail": f"已激活赞助权益 {days} 天"}
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS sponsor_claims(
            id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT, tier TEXT,
            amount REAL, note TEXT, status TEXT, created REAL)""")
        c.execute("INSERT INTO sponsor_claims(username,tier,amount,note,status,created) "
                  "VALUES(?,?,?,?,?,?)", (u["username"], chosen.get("name") if chosen else "自定义",
                                          amt, note, "pending", now))
    audit(u["username"], "sponsor_claim", u["username"], f"tier={tier} pending", client_ip(request))
    return {"status": "pending", "detail": "已提交，管理员确认后生效"}


# --------------------------------------------------------------------------- #
# 在线支付（支付宝当面付）
# --------------------------------------------------------------------------- #
def _activate_sponsor(username: str, days: int, tier_name: str, amount: float) -> float:
    """
    置位赞助态。若当前赞助未到期，则在其基础上顺延（不吞掉剩余天数）。
    返回新的到期时间戳。
    """
    now = time.time()
    with db() as c:
        row = c.execute("SELECT sponsor, sponsor_until, sponsor_amount FROM users WHERE username=?",
                        (username,)).fetchone()
        cur_until = (row["sponsor_until"] or 0) if row else 0
        base = cur_until if (row and row["sponsor"] and cur_until > now) else now
        until = base + days * 86400
        c.execute("UPDATE users SET sponsor=1, sponsor_until=?, sponsor_tier=?, "
                  "sponsor_amount=COALESCE(sponsor_amount,0)+? WHERE username=?",
                  (until, tier_name, amount, username))
    return until


def _find_tier(name: str):
    tiers = SS.get("sponsor.tiers")
    if not isinstance(tiers, list):
        return None
    for t in tiers:
        if isinstance(t, dict) and (t.get("name") == name or str(t.get("name")) == str(name)):
            return t
    return None


def _out_trade_no() -> str:
    return f"XSP{int(time.time())}{secrets.token_hex(4)}"


def _pay_notify_url(request=None) -> str:
    explicit = str(SS.get("pay.notify_url") or "").strip()
    if explicit:
        return explicit
    return _site_base_url(request).rstrip("/") + "/api/v1/alipay/notify"


def _order_row(conn, out_trade_no):
    return conn.execute("SELECT * FROM pay_orders WHERE out_trade_no=?", (out_trade_no,)).fetchone()


def _settle(order, trade_status: str, trade_no: str = "", buyer_id: str = "",
            source: str = "query") -> dict:
    """
    依据支付宝返回的交易状态落库并置位赞助。幂等：已支付的订单不会重复发放。
    """
    o = dict(order)
    if o["status"] in ("paid", "paid_pending", "refunded"):
        return {"changed": False, "status": o["status"], "detail": "订单已处理过"}
    if trade_status not in pay.PAID_STATES:
        cooked = {"TRADE_CLOSED": "closed", "WAIT_BUYER_PAY": "waiting"}
        new = cooked.get(trade_status, "created")
        if new == "closed" and o["status"] != "closed":
            with db() as c:
                c.execute("UPDATE pay_orders SET status='closed', last_query=? WHERE out_trade_no=?",
                          (time.time(), o["out_trade_no"]))
        elif trade_status == "WAIT_BUYER_PAY":
            with db() as c:
                c.execute("UPDATE pay_orders SET status='waiting', last_query=? "
                          "WHERE out_trade_no=? AND status='created'",
                          (time.time(), o["out_trade_no"]))
        return {"changed": False, "status": new, "detail": f"交易状态 {trade_status}"}

    now = time.time()
    auto = bool(SS.get("pay.auto_activate"))
    target_status = "paid" if auto else "paid_pending"
    # 原子抢占：只有把订单从「未发放」推进到目标状态的那一次调用，才允许发放权益。
    # 支付宝的异步通知与本接口的主动查单会并发触发（TOCTOU），
    # 原实现先读后写且分支不覆盖 paid_pending → 同一笔订单可重复顺延赞助天数。
    with db() as c:
        cur = c.execute(
            "UPDATE pay_orders SET status=?, trade_no=?, buyer_id=?, paid_at=?, "
            "last_query=?, raw=? WHERE out_trade_no=? "
            "AND status NOT IN ('paid','paid_pending','refunded')",
            (target_status, trade_no, buyer_id, now, now,
             json.dumps({"trade_status": trade_status, "source": source}, ensure_ascii=False),
             o["out_trade_no"]))
        claimed = cur.rowcount
    if not claimed:
        return {"changed": False, "status": o["status"], "detail": "订单已处理过（并发）"}
    until = None
    if auto:
        until = _activate_sponsor(o["username"], int(o["days"]), o["tier_name"], float(o["amount"]))
    audit(o["username"], "pay_success", o["username"],
          f"out_trade_no={o['out_trade_no']} amount={o['amount']} tier={o['tier_name']} "
          f"source={source} auto={auto}", "")
    return {"changed": True,
            "status": target_status,
            "until": until,
            "detail": (f"支付成功，赞助权益 {o['days']} 天已生效" if auto
                       else "支付成功，等待管理员确认发放权益")}


@app.get("/api/v1/pay/info")
def pay_info():
    """前台可见的支付能力（不含任何密钥）。"""
    return {"available": pay.available(),
            "enabled": bool(SS.get("pay.alipay_enabled")),
            "configured": pay.is_configured(),
            "poll_seconds": int(str(SS.get("pay.poll_seconds") or 3)),
            "order_ttl_minutes": int(str(SS.get("pay.order_ttl_minutes") or 15)),
            "currency": SS.get("sponsor.currency") or "CNY"}


@app.post("/api/v1/sponsor/pay")
def sponsor_pay(request: Request, tier: str = Form(...)):
    """
    选定套餐下单：调 alipay.trade.precreate 取回二维码。
    仅返回二维码与订单号，绝不返回密钥或支付宝原始报文。
    """
    u = current_user(request)
    if not u:
        return JSONResponse({"detail": "请先登录"}, status_code=401)
    if not bool(SS.get("pay.alipay_enabled")):
        return JSONResponse({"detail": "站点未开启在线支付"}, status_code=400)
    if not pay.is_configured():
        return JSONResponse({"detail": "支付未配置（管理员需填写 APPID 与密钥）"}, status_code=503)

    t = _find_tier(tier)
    if not t:
        return JSONResponse({"detail": "套餐不存在"}, status_code=400)
    amount = float(t.get("amount") or 0)
    days = int(t.get("days") or 0)
    if amount <= 0 or days <= 0:
        return JSONResponse({"detail": "套餐金额或天数无效"}, status_code=400)

    # 复用同一用户未过期的待支付订单，避免重复下单
    ttl_min = int(str(SS.get("pay.order_ttl_minutes") or 15))
    now = time.time()
    with db() as c:
        old = c.execute("SELECT * FROM pay_orders WHERE username=? AND status IN ('created','waiting') "
                        "AND tier_name=? AND expires>? ORDER BY id DESC LIMIT 1",
                        (u["username"], t.get("name"), now)).fetchone()
    if old and old["qr_code"]:
        return {"status": "ok", "out_trade_no": old["out_trade_no"], "qr_code": old["qr_code"],
                "amount": old["amount"], "days": old["days"], "tier": old["tier_name"],
                "expires_in": max(0, int(old["expires"] - now)), "reused": True}

    otn = _out_trade_no()
    with db() as c:
        c.execute("INSERT INTO pay_orders(out_trade_no,username,tier_name,amount,days,status,"
                  "created,expires) VALUES(?,?,?,?,?,'created',?,?)",
                  (otn, u["username"], t.get("name"), amount, days, now, now + ttl_min * 60))

    r = pay.precreate(otn, f"{amount:.2f}", str(t.get("name")), _pay_notify_url(request))
    if not r.get("ok"):
        with db() as c:
            c.execute("UPDATE pay_orders SET status='failed', raw=? WHERE out_trade_no=?",
                      (json.dumps({"error": r.get("error")}, ensure_ascii=False), otn))
        audit(u["username"], "pay_create_failed", u["username"],
              f"out_trade_no={otn} err={r.get('error')}", client_ip(request))
        return JSONResponse({"detail": f"下单失败：{r.get('error')}"}, status_code=502)

    with db() as c:
        c.execute("UPDATE pay_orders SET qr_code=? WHERE out_trade_no=?",
                  (r["qr_code"], otn))
    audit(u["username"], "pay_create", u["username"],
          f"out_trade_no={otn} amount={amount} tier={t.get('name')} "
          f"verified={r.get('verified')}", client_ip(request))
    return {"status": "ok", "out_trade_no": otn, "qr_code": r["qr_code"],
            "amount": amount, "days": days, "tier": t.get("name"),
            "expires_in": ttl_min * 60, "reused": False,
            "sign_verified": bool(r.get("verified"))}


@app.get("/api/v1/sponsor/pay/{out_trade_no}/qr.png")
def sponsor_pay_qr(request: Request, out_trade_no: str):
    """
    渲染当前订单的支付宝二维码为 PNG。
    需要登录且订单归属本人 —— 二维码内含支付链接，不应被他人取用。
    """
    u = current_user(request)
    if not u:
        return JSONResponse({"detail": "请先登录"}, status_code=401)
    with db() as c:
        o = _order_row(c, out_trade_no)
    if not o:
        return JSONResponse({"detail": "订单不存在"}, status_code=404)
    d = dict(o)
    if d["username"] != u["username"] and not u.get("is_admin"):
        return JSONResponse({"detail": "无权查看该订单"}, status_code=403)
    if not d.get("qr_code"):
        return JSONResponse({"detail": "该订单没有二维码"}, status_code=404)
    png = pay.qr_png_bytes(d["qr_code"])
    return Response(content=png, media_type="image/png",
                    headers={"Cache-Control": "private, max-age=300"})


@app.get("/api/v1/sponsor/pay/{out_trade_no}")
def sponsor_pay_status(request: Request, out_trade_no: str):
    """前端轮询：服务端主动查单，不信任前端任何断言。"""
    u = current_user(request)
    if not u:
        return JSONResponse({"detail": "请先登录"}, status_code=401)
    with db() as c:
        o = _order_row(c, out_trade_no)
    if not o:
        return JSONResponse({"detail": "订单不存在"}, status_code=404)
    d = dict(o)
    if d["username"] != u["username"] and not u.get("is_admin"):
        return JSONResponse({"detail": "无权查看该订单"}, status_code=403)

    if d["status"] in ("paid", "paid_pending"):
        return {"status": "ok", "order_status": d["status"], "paid": True,
                "out_trade_no": out_trade_no, "amount": d["amount"], "days": d["days"]}

    now = time.time()
    if d["expires"] and now > d["expires"] and d["status"] in ("created", "waiting"):
        pay.close(out_trade_no)
        with db() as c:
            c.execute("UPDATE pay_orders SET status='expired', last_query=? WHERE out_trade_no=?",
                      (now, out_trade_no))
        return {"status": "ok", "order_status": "expired", "paid": False,
                "detail": "订单已超时，请重新下单"}

    q = pay.query(out_trade_no)
    if not q.get("ok"):
        # 查单失败不回虚假状态：明确告知前端仍在等待，可继续轮询
        return {"status": "ok", "order_status": d["status"], "paid": False,
                "query_ok": False, "detail": f"查单暂不可用：{q.get('error')}"}
    res = _settle(o, q.get("trade_status", ""), q.get("trade_no", ""),
                  q.get("buyer_id", ""), source="query")
    return {"status": "ok", "order_status": res["status"], "paid": res["changed"],
            "trade_status": q.get("trade_status", ""), "detail": res.get("detail", ""),
            "until": res.get("until"), "amount": d["amount"], "days": d["days"]}


@app.post("/api/v1/sponsor/pay/{out_trade_no}/cancel")
def sponsor_pay_cancel(request: Request, out_trade_no: str):
    u = current_user(request)
    if not u:
        return JSONResponse({"detail": "请先登录"}, status_code=401)
    with db() as c:
        o = _order_row(c, out_trade_no)
    if not o:
        return JSONResponse({"detail": "订单不存在"}, status_code=404)
    if dict(o)["username"] != u["username"] and not u.get("is_admin"):
        return JSONResponse({"detail": "无权操作该订单"}, status_code=403)
    if dict(o)["status"] == "paid":
        return JSONResponse({"detail": "订单已支付，无法取消"}, status_code=400)
    pay.close(out_trade_no)
    with db() as c:
        c.execute("UPDATE pay_orders SET status='cancelled', last_query=? WHERE out_trade_no=?",
                  (time.time(), out_trade_no))
    return {"status": "ok", "detail": "订单已取消"}


@app.post("/api/v1/alipay/notify", response_class=PlainTextResponse)
async def alipay_notify(request: Request):
    """
    支付宝异步通知。必须以纯文本 "success" 应答，否则支付宝会重复投递。
    验签不通过一律拒绝，绝不依据未验签的报文发放权益。
    """
    form = dict((await request.form()))
    flat = {k: str(v) for k, v in form.items()}
    if not flat:
        return PlainTextResponse("failure", status_code=400)
    if not pay.verify_notify(flat):
        audit("alipay", "notify_bad_sign", flat.get("out_trade_no", ""),
              "异步通知验签失败", client_ip(request))
        return PlainTextResponse("failure", status_code=400)

    otn = flat.get("out_trade_no", "")
    with db() as c:
        o = _order_row(c, otn)
    if not o:
        return PlainTextResponse("failure", status_code=404)

    # 金额核对：防止篡改通知金额
    try:
        if abs(float(flat.get("total_amount", 0)) - float(dict(o)["amount"])) > 0.001:
            audit("alipay", "notify_amount_mismatch", otn,
                  f"通知金额={flat.get('total_amount')} 订单金额={dict(o)['amount']}",
                  client_ip(request))
            return PlainTextResponse("failure", status_code=400)
    except (TypeError, ValueError):
        return PlainTextResponse("failure", status_code=400)

    _settle(o, flat.get("trade_status", ""), flat.get("trade_no", ""),
            flat.get("buyer_id", ""), source="notify")
    return PlainTextResponse("success")


@app.get("/api/v1/sponsor/orders")
def sponsor_orders(request: Request):
    u = current_user(request)
    if not u:
        return JSONResponse({"detail": "请先登录"}, status_code=401)
    with db() as c:
        rows = c.execute("SELECT out_trade_no,tier_name,amount,days,status,created,paid_at "
                         "FROM pay_orders WHERE username=? ORDER BY id DESC LIMIT 50",
                         (u["username"],)).fetchall()
        orders = [dict(r) for r in rows]
        # 标注每笔订单是否可申请退款，前端据此显示按钮
        refunds = refund.list_for_user(c, u["username"], limit=200)
    busy = {r["out_trade_no"] for r in refunds
            if r["status"] in ("pending", "approved")}
    refunded = {r["out_trade_no"] for r in refunds if r["status"] == "approved"}
    for o in orders:
        o["refundable"] = (o["status"] in ("paid", "paid_pending")
                           and o["out_trade_no"] not in busy
                           and o["out_trade_no"] not in refunded)
        o["refund_status"] = next(
            (r["status_text"] for r in refunds if r["out_trade_no"] == o["out_trade_no"]), "")
    return {"count": len(orders), "orders": orders,
            "refund_enabled": bool(SS.get("pay.refund_enabled", True))}


@app.get("/api/v1/sponsor/refunds")
def sponsor_refunds(request: Request):
    """我提交过的退款申请。"""
    u = current_user(request)
    if not u:
        return JSONResponse({"detail": "请先登录"}, status_code=401)
    with db() as c:
        refund.init(c)
        rows = refund.list_for_user(c, u["username"])
    return {"count": len(rows), "requests": rows}


@app.post("/api/v1/sponsor/refund")
def sponsor_refund_apply(request: Request, out_trade_no: str = Form(...),
                         reason: str = Form(...), detail: str = Form("")):
    """用户提交退款申请（必须填理由）。"""
    u = current_user(request)
    if not u:
        return JSONResponse({"detail": "请先登录"}, status_code=401)
    if not bool(SS.get("pay.refund_enabled", True)):
        return JSONResponse({"detail": "本站未开启退款通道"}, status_code=400)

    reason = (reason or "").strip()
    detail = (detail or "").strip()
    if len(reason) < refund.MIN_REASON:
        return JSONResponse({"detail": f"退款理由至少 {refund.MIN_REASON} 个字，请说明具体原因"},
                            status_code=400)
    if len(reason) > refund.MAX_REASON:
        return JSONResponse({"detail": f"退款理由不能超过 {refund.MAX_REASON} 个字"},
                            status_code=400)

    with db() as c:
        refund.init(c)
        ok, why = refund.can_apply(c, u["username"], out_trade_no)
        if not ok:
            return JSONResponse({"detail": why}, status_code=400)
        r = refund.create(c, u["username"], out_trade_no, reason, detail)
    audit(u["username"], "refund_apply", out_trade_no,
          f"amount={r.get('amount')} reason={reason[:80]}", client_ip(request))
    return {"status": "ok", "id": r.get("id"), "detail": "退款申请已提交，请等待管理员审核",
            "request": r}


# ---- 管理端 ----
@app.get("/api/v1/admin/pay/info")
def admin_pay_info(request: Request):
    if not require_admin(request):
        return JSONResponse({"detail": "需要管理员权限"}, status_code=403)
    info = pay.info()
    now = time.time()
    with db() as c:
        rows = c.execute("SELECT status, COUNT(*) n, COALESCE(SUM(amount),0) total "
                         "FROM pay_orders GROUP BY status").fetchall()
    by_status = {r["status"]: {"count": r["n"], "amount": round(r["total"], 2)} for r in rows}
    with db() as c:
        paid = c.execute("SELECT COUNT(*) n, COALESCE(SUM(amount),0) total FROM pay_orders "
                         "WHERE status IN ('paid','paid_pending')").fetchone()
        pending = c.execute("SELECT COUNT(*) FROM pay_orders WHERE status='paid_pending'").fetchone()[0]
    # 异步通知地址：必须带上 request 才能依据真实 Host / X-Forwarded-* 推导，
    # 否则会退回 127.0.0.1，管理员看到的就是错误地址。
    auto_url = _site_base_url(request).rstrip("/") + "/api/v1/alipay/notify"
    explicit = str(SS.get("pay.notify_url") or "").strip()
    return {"info": info, "by_status": by_status,
            "paid_count": paid["n"], "paid_amount": round(paid["total"], 2),
            "pending_confirm": pending,
            "notify_url": explicit or auto_url,
            "notify_url_auto": auto_url,
            "notify_url_explicit": explicit,
            "notify_url_is_custom": bool(explicit),
            "site_base": _site_base_url(request).rstrip("/")}


@app.post("/api/v1/admin/pay/test")
def admin_pay_test(request: Request):
    """用真实网关做一次签名/验签连通性测试（下单 0.01 元并立即关闭）。"""
    if not require_admin(request):
        return JSONResponse({"detail": "需要管理员权限"}, status_code=403)
    if not pay.is_configured():
        return JSONResponse({"status": "error",
                             "detail": "未配置：需填 APPID、应用私钥、支付宝公钥"}, status_code=400)
    otn = _out_trade_no()
    r = pay.precreate(otn, "0.01", "连通性测试", _pay_notify_url(request))
    if r.get("ok"):
        pay.close(otn)
        audit("admin", "pay_test", "", f"ok out_trade_no={otn} verified={r.get('verified')}",
              client_ip(request))
        return {"status": "ok", "detail": "下单成功，签名与验签均通过",
                "sign_verified": bool(r.get("verified")),
                "gateway": pay.cfg()["gateway"]}
    audit("admin", "pay_test", "", f"fail err={r.get('error')}", client_ip(request))
    return JSONResponse({"status": "error", "detail": r.get("error") or "下单失败"},
                        status_code=400)


@app.get("/api/v1/admin/pay/orders")
def admin_pay_orders(request: Request, status: str = "", limit: int = 100):
    if not require_admin(request):
        return JSONResponse({"detail": "需要管理员权限"}, status_code=403)
    q = ("SELECT out_trade_no,username,tier_name,amount,days,status,trade_no,created,paid_at "
         "FROM pay_orders")
    args = []
    if status:
        q += " WHERE status=?"
        args.append(status)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(min(max(int(limit), 1), 500))
    with db() as c:
        rows = c.execute(q, args).fetchall()
    return {"count": len(rows), "orders": [dict(r) for r in rows]}


# ---- 全局构建插件目录（软件包 / 分类 / 套件） ---- #
@app.get("/api/v1/packages/catalog")
def packages_catalog():
    """前台用的插件目录（构建定制页的预设清单）。"""
    with db() as c:
        d = pkgcatalog.get(c)
    return d


@app.get("/api/v1/admin/catalog")
def admin_catalog(request: Request):
    if not require_admin(request):
        return JSONResponse({"detail": "需要管理员权限"}, status_code=403)
    with db() as c:
        d = pkgcatalog.get(c)
    return {"catalog": d, "counts": {
        "presets": len(d["presets"]), "cats": len(d["cats"]), "suites": len(d["suites"])}}


@app.post("/api/v1/admin/catalog")
async def admin_catalog_edit(request: Request):
    """增删构建用的插件与软件包。

    body(JSON) 支持：
      {"action":"add_preset","name":"...","label":"...","cat":"...","desc":"..."}
      {"action":"remove_preset","name":"..."}
      {"action":"add_cat","key":"...","label":"..."}
      {"action":"remove_cat","key":"..."}
      {"action":"add_suite","key":"...","label":"...","pkgs":["a","b"]}
      {"action":"remove_suite","key":"..."}
      {"action":"reset"}
    """
    a = is_admin(request)
    if not a:
        return JSONResponse({"detail": "需要管理员权限"}, status_code=403)
    try:
        body = json.loads((await request.body()) or b"{}")
    except ValueError:
        return JSONResponse({"detail": "请求体不是合法 JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"detail": "请求体必须是 JSON 对象"}, status_code=400)

    act = str(body.get("action") or "").strip()
    with db() as c:
        pkgcatalog.get(c)          # 确保已初始化
        if act == "add_preset":
            r = pkgcatalog.add_preset(c, body.get("name", ""), body.get("label", ""),
                                      body.get("cat", ""), body.get("desc", ""))
        elif act == "remove_preset":
            r = pkgcatalog.remove_preset(c, body.get("name", ""))
        elif act == "add_cat":
            r = pkgcatalog.add_cat(c, body.get("key", ""), body.get("label", ""))
        elif act == "remove_cat":
            r = pkgcatalog.remove_cat(c, body.get("key", ""))
        elif act == "add_suite":
            r = pkgcatalog.add_suite(c, body.get("key", ""), body.get("label", ""),
                                     body.get("pkgs"))
        elif act == "remove_suite":
            r = pkgcatalog.remove_suite(c, body.get("key", ""))
        elif act == "reset":
            pkgcatalog.reset(c)
            r = {"ok": True, "catalog": pkgcatalog.get(c)}
        else:
            return JSONResponse({"detail": "未知 action"}, status_code=400)

    if not r.get("ok"):
        return JSONResponse({"detail": r.get("error") or "操作失败"}, status_code=400)
    audit(a["username"], "catalog_" + act, str(body.get("name") or body.get("key") or ""),
          json.dumps({k: v for k, v in body.items() if k != "action"},
                     ensure_ascii=False)[:300], client_ip(request))
    return {"status": "ok", "catalog": r["catalog"]}


@app.get("/api/v1/admin/refunds")
def admin_refunds(request: Request, status: str = ""):
    """退款申请列表（管理员审核用）。"""
    if not require_admin(request):
        return JSONResponse({"detail": "需要管理员权限"}, status_code=403)
    with db() as c:
        refund.init(c)
        rows = refund.list_all(c, status)
        st = refund.stats(c)
    return {"count": len(rows), "requests": rows, "stats": st,
            "pending": st.get("pending", {}).get("count", 0)}


@app.post("/api/v1/admin/refund")
async def admin_refund_review(request: Request, rid: int = Form(...),
                              action: str = Form(...), note: str = Form(""),
                              offline: str = Form("0")):
    """审核退款：approve 调真实网关退款并回收权益；reject 驳回。

    offline=1 时不调网关（用于已线下退款的场景），仅标记与回收权益。
    """
    a = is_admin(request)
    if not a:
        return JSONResponse({"detail": "需要管理员权限"}, status_code=403)
    with db() as c:
        refund.init(c)
        r = refund.get(c, rid)
        if not r:
            return JSONResponse({"detail": "申请不存在"}, status_code=404)

        if action == "reject":
            res = refund.reject(c, rid, a["username"], note)
            if not res["ok"]:
                return JSONResponse({"detail": res["error"]}, status_code=400)
            audit(a["username"], "refund_reject", r["out_trade_no"],
                  f"rid={rid} note={note[:80]}", client_ip(request))
            return {"status": "ok", "detail": "已驳回", "request": refund.get(c, rid)}
        if action != "approve":
            return JSONResponse({"detail": "action 必须为 approve / reject"}, status_code=400)

    # approve 走网关外呼，用独立连接，不占着上面的会话
    with db() as c2:
        refund.init(c2)
        res = refund.approve(c2, rid, a["username"], note,
                             do_gateway=(offline not in ("1", "true")))
        cur = refund.get(c2, rid)
    if not res["ok"]:
        audit(a["username"], "refund_approve_failed", r["out_trade_no"],
              f"rid={rid} err={res['error']}", client_ip(request))
        return JSONResponse({"detail": res["error"], "status": res.get("status", "failed"),
                             "request": cur}, status_code=502)
    audit(a["username"], "refund_approve", r["out_trade_no"],
          f"rid={rid} amount={res['refund_amount']}", client_ip(request))
    return {"status": "ok", "detail": f"已退款 {res['refund_amount']} 元，赞助权益已回收",
            "fund_change": res.get("fund_change"), "request": cur}


@app.post("/api/v1/admin/pay/order")
def admin_pay_order(request: Request, out_trade_no: str = Form(...), action: str = Form("confirm")):
    """管理员对已支付但未自动发放的订单做人工确认。"""
    if not require_admin(request):
        return JSONResponse({"detail": "需要管理员权限"}, status_code=403)
    with db() as c:
        o = _order_row(c, out_trade_no)
    if not o:
        return JSONResponse({"detail": "订单不存在"}, status_code=404)
    d = dict(o)
    if action == "confirm":
        # 只允许 paid_pending（自动发放关闭时）→ paid。
        # 原实现把 'paid' 也放进允许集合，管理员点两次就发放两次权益。
        if d["status"] != "paid_pending":
            return JSONResponse({"detail": f"订单状态为 {d['status']}，无法确认发放"
                                           "（仅「待确认」可人工发放）"}, status_code=400)
        with db() as c:
            cur = c.execute("UPDATE pay_orders SET status='paid' "
                            "WHERE out_trade_no=? AND status='paid_pending'", (out_trade_no,))
            claimed = cur.rowcount
        if not claimed:
            return JSONResponse({"detail": "订单已被其它操作处理，请刷新后查看"}, status_code=409)
        until = _activate_sponsor(d["username"], int(d["days"]), d["tier_name"], float(d["amount"]))
        audit("admin", "pay_confirm", d["username"],
              f"out_trade_no={out_trade_no} days={d['days']}", client_ip(request))
        return {"status": "ok", "detail": f"已为 {d['username']} 发放 {d['days']} 天赞助", "until": until}
    if action == "close":
        pay.close(out_trade_no)
        with db() as c:
            c.execute("UPDATE pay_orders SET status='closed' WHERE out_trade_no=?", (out_trade_no,))
        audit("admin", "pay_close", d["username"], f"out_trade_no={out_trade_no}", client_ip(request))
        return {"status": "ok", "detail": "订单已关闭"}
    return JSONResponse({"detail": "未知操作"}, status_code=400)


@app.get("/api/v1/admin/pay/verify_stats")
def admin_pay_verify_stats(request: Request):
    if not require_admin(request):
        return JSONResponse({"detail": "需要管理员权限"}, status_code=403)
    return {"email": verify.stats(), "email_send_log": verify.send_log(30)}


# ---- 注册邮箱验证：管理端 ----
@app.post("/api/v1/admin/user/verify")
def admin_user_verify(request: Request, username: str = Form(...), verified: int = Form(1)):
    """管理员手动置位/解除邮箱验证（用于线下核验后放行）。"""
    if not require_admin(request):
        return JSONResponse({"detail": "需要管理员权限"}, status_code=403)
    with db() as c:
        n = c.execute("UPDATE users SET email_verified=? WHERE username=?",
                      (1 if int(verified) else 0, username)).rowcount
    if not n:
        return JSONResponse({"detail": "用户不存在"}, status_code=404)
    if int(verified):
        verify.invalidate(username)
    audit("admin", "user_verify", username, f"verified={verified}", client_ip(request))
    return {"status": "ok", "detail": "已更新邮箱验证状态"}


@app.post("/api/v1/admin/user/reverify")
def admin_user_reverify(request: Request, username: str = Form(...)):
    """管理员代用户重发验证邮件。"""
    if not require_admin(request):
        return JSONResponse({"detail": "需要管理员权限"}, status_code=403)
    with db() as c:
        row = c.execute("SELECT email FROM users WHERE username=?", (username,)).fetchone()
    if not row:
        return JSONResponse({"detail": "用户不存在"}, status_code=404)
    if not (row["email"] or "").strip():
        return JSONResponse({"detail": "该用户未登记邮箱"}, status_code=400)
    if not mailer.ready():
        return JSONResponse({"detail": f"当前无法发信（{mailer.why_not_ready()}）"}, status_code=503)
    ok, detail = _send_verify_mail(username, row["email"], request)
    if not ok:
        return JSONResponse({"detail": f"发送失败：{detail}"}, status_code=502)
    return {"status": "ok", "detail": f"已重发至 {row['email']}"}



@app.get("/api/v1/admin/sponsor/claims")
def admin_sponsor_claims(request: Request, status: str = ""):
    require_admin(request)
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS sponsor_claims(
            id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT, tier TEXT,
            amount REAL, note TEXT, status TEXT, created REAL)""")
        sql, args = "SELECT * FROM sponsor_claims WHERE 1=1", []
        if status:
            sql += " AND status=?"; args.append(status)
        sql += " ORDER BY created DESC LIMIT 500"
        rows = [dict(r) for r in c.execute(sql, args)]
    return {"claims": rows, "stats": sponsor_stats()}


def sponsor_stats():
    with db() as c:
        tot = c.execute("SELECT COUNT(*) n FROM users WHERE sponsor=1").fetchone()["n"]
        act = c.execute("SELECT COUNT(*) n FROM users WHERE sponsor=1 AND "
                        "(sponsor_until IS NULL OR sponsor_until>?)", (time.time(),)).fetchone()["n"]
        amt = c.execute("SELECT COALESCE(SUM(sponsor_amount),0) s FROM users WHERE sponsor=1"
                        ).fetchone()["s"]
    return {"sponsors": tot, "active": act, "expired": tot - act, "total_amount": round(amt, 2),
            "currency": SS.get("sponsor.currency") or "CNY"}


@app.post("/api/v1/admin/sponsor/claim")
async def admin_sponsor_claim(request: Request, cid: int = Form(...), action: str = Form(...),
                              days: str = Form("")):
    a = is_admin(request)
    require_admin(request)
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS sponsor_claims(
            id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT, tier TEXT,
            amount REAL, note TEXT, status TEXT, created REAL)""")
        row = c.execute("SELECT * FROM sponsor_claims WHERE id=?", (cid,)).fetchone()
    if not row:
        raise HTTPException(404, "记录不存在")
    if action == "approve":
        tiers = SS.get("sponsor.tiers") or []
        d = None
        for t in tiers:
            if t.get("name") == row["tier"]:
                d = int(t.get("days", 30))
                break
        if days:
            try:
                d = int(days)
            except ValueError:
                raise HTTPException(400, "天数必须是整数")
        d = d or 30
        now = time.time()
        with db() as c:
            cur = c.execute("SELECT sponsor_until FROM users WHERE username=?",
                            (row["username"],)).fetchone()
            base = max(now, (cur["sponsor_until"] or 0) if cur else 0)
            c.execute("UPDATE users SET sponsor=1, sponsor_until=?, sponsor_tier=?, "
                      "sponsor_amount=COALESCE(sponsor_amount,0)+? WHERE username=?",
                      (base + d * 86400, row["tier"], row["amount"] or 0, row["username"]))
            c.execute("UPDATE sponsor_claims SET status='approved' WHERE id=?", (cid,))
        audit(a["username"], "sponsor_approve", row["username"],
              f"tier={row['tier']} days={d}", client_ip(request))
        return {"status": "ok", "days": d}
    if action == "reject":
        with db() as c:
            c.execute("UPDATE sponsor_claims SET status='rejected' WHERE id=?", (cid,))
        audit(a["username"], "sponsor_reject", row["username"], "", client_ip(request))
        return {"status": "ok"}
    raise HTTPException(400, "action 必须为 approve / reject")


@app.post("/api/v1/admin/user/sponsor")
async def admin_set_sponsor(request: Request, username: str = Form(...),
                            sponsor: str = Form("1"), days: str = Form("30"),
                            amount: str = Form("0"), clear: str = Form("0")):
    """管理员直接赋予 / 撤销 / 续期赞助权益。"""
    a = is_admin(request)
    require_admin(request)
    with db() as c:
        u = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if not u:
            raise HTTPException(404, "用户不存在")
    if clear in ("1", "true"):
        with db() as c:
            c.execute("UPDATE users SET sponsor=0, sponsor_until=NULL, sponsor_tier='' WHERE username=?",
                      (username,))
        audit(a["username"], "sponsor_clear", username, "", client_ip(request))
        return {"status": "ok", "sponsor": False}
    try:
        d = int(days)
    except ValueError:
        raise HTTPException(400, "天数必须是整数")
    try:
        amt = float(amount or 0)
    except ValueError:
        raise HTTPException(400, "金额必须是数字")
    now = time.time()
    with db() as c:
        cur = c.execute("SELECT sponsor_until FROM users WHERE username=?", (username,)).fetchone()
        base = max(now, (cur["sponsor_until"] or 0) if cur else 0)
        c.execute("UPDATE users SET sponsor=1, sponsor_until=?, sponsor_amount=COALESCE(sponsor_amount,0)+? "
                  "WHERE username=?", (base + d * 86400, amt, username))
    audit(a["username"], "sponsor_set", username, f"days={d} amount={amt}", client_ip(request))
    return {"status": "ok", "until": base + d * 86400}


# --- 站点配置（schema 驱动） ------------------------------------------------ #
@app.get("/api/v1/admin/site")
def admin_site_get(request: Request):
    require_admin(request)
    payload = SS.schema_payload()
    payload["backend_info"] = backends.info()
    payload["sponsor_stats"] = sponsor_stats()
    payload["download_stats"] = dl.stats()
    payload["mail_configured"] = mailer.ready()
    payload["mail_reason"] = mailer.why_not_ready()
    return payload


@app.post("/api/v1/admin/site")
async def admin_site_set(request: Request):
    """批量写入站点配置。body: {key: value, ...}"""
    a = is_admin(request)
    require_admin(request)
    raw = await request.body()
    try:
        payload = json.loads(raw or b"{}")
    except Exception:
        return JSONResponse({"detail": "bad json"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"detail": "payload 必须是对象"}, status_code=400)
    ok, errs = {}, {}
    for k, v in payload.items():
        try:
            ok[k] = SS.set_(k, v)
        except ValueError as e:
            errs[k] = str(e)
        except Exception as e:
            errs[k] = f"{type(e).__name__}: {e}"
    if ok:
        audit(a["username"], "site_settings", ",".join(list(ok.keys())[:12]),
              f"updated={len(ok)} failed={len(errs)}", client_ip(request))
    return {"status": "ok" if not errs else "partial", "updated": ok, "errors": errs}


@app.post("/api/v1/admin/mail/test")
async def admin_mail_test(request: Request, to: str = Form("")):
    a = is_admin(request)
    require_admin(request)
    target = (to or "").strip() or (SS.get("mail.from_addr") or "")
    if not target:
        return JSONResponse({"detail": "请填写测试收件地址"}, status_code=400)
    ok, msg = await __import__("asyncio").to_thread(mailer.test, target)
    # ★ 留痕到 email_send_log（purpose='test'）—— PHP 的 AdminController::mailTest
    #   本来就写，Python 侧漏了。少了这一笔，「发送测试邮件」的结果不会出现在
    #   后台「投递记录」里：管理员点完测试、翻到记录页看到空的，像是功能坏了。
    #   失败也要记（ok=0）—— 排查投递问题时失败记录比成功记录更有用。
    try:
        verify.log_send("test", str(target), str((a or {}).get("username") or ""), ok, msg)
    except Exception:                                           # noqa: BLE001
        pass  # 记日志失败不该把「测试邮件」本身的结果吞掉
    audit(a["username"], "mail_test", target, msg[:160], client_ip(request))
    return JSONResponse({"status": "ok" if ok else "error", "detail": msg, "to": target},
                        status_code=200 if ok else 400)


@app.get("/api/v1/admin/mail/log")
def admin_mail_log(request: Request, limit: int = 60):
    require_admin(request)
    return {"log": mailer.recent(limit), "configured": mailer.ready(),
            "reason": mailer.why_not_ready()}


@app.post("/api/v1/admin/github/test")
async def admin_gh_test(request: Request):
    a = is_admin(request)
    require_admin(request)
    be = backends._GH
    ok, res = await __import__("asyncio").to_thread(be.list_workflows)
    audit(a["username"], "github_test", SS.get("gh.repo") or "",
          ("ok" if ok else str(res))[:160], client_ip(request))
    return JSONResponse({"status": "ok" if ok else "error",
                         "detail": res if not ok else f"连接成功，共 {len(res)} 个 workflow",
                         "workflows": res if ok else []},
                        status_code=200 if ok else 400)


# --- 下载令牌管理 ----------------------------------------------------------- #
@app.get("/api/v1/admin/tokens")
def admin_tokens(request: Request, request_hash: str = "", username: str = "",
                 include_expired: int = 1, limit: int = 500):
    require_admin(request)
    # 管理端需要 token 原文才能执行吊销/续期，显式 include_token=True
    return {"tokens": dl.list_for(request_hash or None, username or None,
                                  bool(include_expired), limit, include_token=True),
            "stats": dl.stats()}


@app.post("/api/v1/admin/tokens")
async def admin_tokens_op(request: Request, action: str = Form(...), token: str = Form(""),
                          request_hash: str = Form(""), hours: str = Form(""),
                          username: str = Form("")):
    a = is_admin(request)
    require_admin(request)
    ip = client_ip(request)
    if action == "revoke":
        ok = dl.revoke(token)
        audit(a["username"], "token_revoke", (token or "")[:24], f"ok={ok}", ip)
        return {"status": "ok" if ok else "error", "detail": "已吊销" if ok else "令牌不存在"}
    if action == "extend":
        try:
            h = int(hours)
        except ValueError:
            raise HTTPException(400, "小时数必须是整数")
        ok = dl.extend(token, h)
        audit(a["username"], "token_extend", (token or "")[:24], f"+{h}h ok={ok}", ip)
        return {"status": "ok" if ok else "error", "detail": f"已续期 {h} 小时" if ok else "令牌不存在"}
    if action == "reissue":
        if not request_hash:
            raise HTTPException(400, "缺少 request_hash")
        hid = check_hash_id(request_hash)
        d = os.path.join(STORE, hid)
        if not os.path.isdir(d):
            raise HTTPException(404, "该构建的产物不存在")
        h = int(hours) if hours else int(SS.get("download.link_ttl_hours") or 72)
        made = {}
        for fn in sorted(os.listdir(d)):
            if os.path.isfile(os.path.join(d, fn)):
                made[fn] = dl.issue(request_hash, fn, username, ttl_hours=h)
        audit(a["username"], "token_reissue", request_hash, f"{len(made)} 个 {h}h", ip)
        return {"status": "ok", "count": len(made),
                "links": [{"name": n, "url": "/dl/t/" + t} for n, t in made.items()]}
    if action == "revoke_build":
        n = dl.revoke_build(request_hash)
        audit(a["username"], "token_revoke_build", request_hash, f"{n} 个", ip)
        return {"status": "ok", "revoked": n}
    if action == "revoke_user":
        n = dl.revoke_user(username)
        audit(a["username"], "token_revoke_user", username, f"{n} 个", ip)
        return {"status": "ok", "revoked": n}
    if action == "cleanup":
        n = dl.cleanup_expired(30)
        audit(a["username"], "token_cleanup", "", f"{n} 条", ip)
        return {"status": "ok", "cleaned": n}
    raise HTTPException(400, f"未知操作 {action}")


# --------------------------------------------------------------------------- #
# 管理员控制台 —— API
# 全部接口强制 require_admin；所有写操作写入 admin_logs 审计
# --------------------------------------------------------------------------- #
@app.get("/admin/", response_class=HTMLResponse)
def admin_page(request: Request):
    return _serve_page("admin.html", request, with_entry=False)


@app.get("/api/v1/admin/overview")
def admin_overview(request: Request):
    require_admin(request)
    q = builder.get_queue()
    with db() as c:
        n_users = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        n_sponsor = c.execute("SELECT COUNT(*) n FROM users WHERE sponsor=1").fetchone()["n"]
        n_admin = c.execute("SELECT COUNT(*) n FROM users WHERE role='admin'").fetchone()["n"]
        n_disabled = c.execute("SELECT COUNT(*) n FROM users WHERE disabled=1").fetchone()["n"]
        n_builds = c.execute("SELECT COUNT(*) n FROM builds").fetchone()["n"]
        n_done = c.execute("SELECT COUNT(*) n FROM builds WHERE status='done'").fetchone()["n"]
        n_fail = c.execute("SELECT COUNT(*) n FROM builds WHERE status='failed'").fetchone()["n"]
        n_prop = c.execute("SELECT COUNT(*) n FROM proposals WHERE COALESCE(status,'pending')='pending'").fetchone()["n"]
        n_bans = c.execute("SELECT COUNT(*) n FROM bans").fetchone()["n"]
    running = [j for j in q.jobs.values() if j.get("status") in ("started", "running")]
    queued = [j for j in q.jobs.values() if j.get("status") == "queued"]
    # 磁盘（构建健康的真实指标）
    st = os.statvfs(ROOT)
    disk_free = st.f_bavail * st.f_frsize / 1048576
    try:
        du = subprocess.run(["du", "-sm", os.path.join(ROOT, "store")],
                            capture_output=True, text=True, timeout=30)
        store_mb = int(du.stdout.split()[0]) if du.returncode == 0 else -1
    except Exception:
        store_mb = -1
    # 完整站点配置（供控制台各页直接读取；敏感项已掩码）
    conf = SS.all_values()
    conf.update({
        "builder_enabled": BUILD_CFG()["enabled"],
        "max_concurrent": BUILD_CFG()["max_concurrent"],
        "registration_open": bool(SS.get("registration_open", True)),
        "mail_enabled": mailer.ready(),
    })
    return {
        "users": {"total": n_users, "sponsor": n_sponsor, "admin": n_admin, "disabled": n_disabled},
        "builds": {"total": n_builds, "done": n_done, "failed": n_fail},
        "queue": {"running": len(running), "queued": len(queued),
                  "concurrency": q.concurrency, "total": len(q.jobs)},
        "proposals_pending": n_prop,
        "bans": n_bans,
        "disk": {"free_mb": round(disk_free), "store_mb": store_mb},
        "sponsor": sponsor_stats(),
        "download": dl.stats(),
        "backend": backends.info(),
        "config": conf,
        "versions": releases_info(),
        # 更新检查的静态信息（仓库/开关）。**不在这里发起网络请求** ——
        # 总览是后台打开就会调的接口，每次打一次 GitHub 会把速率打满。
        # 真正的检查只在管理员点「检查更新」时发生（走下方独立接口）。
        "update": {
            "enabled": update.enabled(),
            "repo": update.repo(),
            "configured": bool(update.repo()),
            "cache_minutes": update.cache_minutes(),
        },
    }


# --------------------------------------------------------------------------- #
# 检查更新（管理员）
# --------------------------------------------------------------------------- #
@app.get("/api/v1/admin/update/check")
def admin_update_check(request: Request, force: int = 0):
    """检查本程序是否有新版本。

    ★ 只读接口：只查询更新源并返回结果，**不落地任何文件、不重启服务**。
      真正的升级动作由管理员按返回的指引手工执行（见 docs/更新升级与完整性检查.md）——
      让一个网页按钮去覆盖正在运行的程序文件并重启自身，出问题时没人收得了场。

    ★ force=1 跳过缓存。缓存分钟数是给「反复点击」和「多人同时打开后台」准备的，
      否则很容易把 GitHub 未认证的 60 次/小时限额打满，连固件构建派发一起挂掉。
    """
    require_admin(request)
    return JSONResponse(update.check(force=bool(force)))


@app.post("/api/v1/admin/update/apply")
def admin_update_apply(request: Request):
    """一键更新：git pull + 装依赖 + 重启服务。仅管理员可用。"""
    require_admin(request)
    u = is_admin(request)
    result = update.apply_update()
    audit((u or {}).get("username", "admin"), "update_apply",
          detail=result.get("message", ""))
    return JSONResponse(result)


def BUILD_CFG():
    """运行时可改的构建参数（管理员在控制台调整后立即生效）。"""
    return {
        "max_concurrent": int(SS.get("builder.max_concurrent") or 1),
        "enabled": bool(SS.get("builder.enabled")),
    }


# --------------------------------------------------------------------------- #
# 构建完成回调 —— 签发限时下载链接 + 邮件通知
# --------------------------------------------------------------------------- #
def _download_base(request: Request = None):
    """下载链接的站点前缀（用于邮件中的绝对地址）。"""
    ext = (SS.get("download.external_host") or "").strip()
    if ext:
        return ext.rstrip("/")
    base = (CFG.get("site", {}).get("base_url") or "").strip()
    if base:
        return base.rstrip("/")
    return ""          # 空表示用相对路径


def on_build_finished(job):
    """
    队列完成回调：
      1. 为产物签发限时下载令牌（有效期由管理员配置）
      2. 若用户留有邮箱且开启邮件通知 → 真实发信
      3. 结果写入 job，供前端展示
    """
    jid = job.get("request_hash")
    req = job.get("req") or {}
    status = job.get("status")

    if status == "done":
        files = job.get("files") or []
        ttl = int(SS.get("download.link_ttl_hours") or 72)
        links = []
        local_n = ext_n = 0
        for f in files:
            name = f.get("name")
            if not name:
                continue
            # 产物已回传本站 → 签发限时令牌（有效期由管理员配置）
            if dl.resolve_path(jid, name):
                tok = dl.issue(jid, name, username=job.get("username", ""), ttl_hours=ttl)
                links.append({"name": name, "size": f.get("size") or 0,
                              "url": _download_base() + "/dl/t/" + tok,
                              "path": "/dl/t/" + tok, "expires_hours": ttl})
                local_n += 1
            else:
                # 产物仍在 GitHub 侧：直接给出 GitHub 产物地址（有效期由 GitHub 保留策略决定）
                links.append({"name": name, "size": f.get("size") or 0,
                              "url": f.get("url") or job.get("gh_run_url") or "",
                              "path": f.get("url") or "",
                              "external": True, "expires_hours": None,
                              "note": "托管于 GitHub，有效期由 GitHub 产物保留策略决定"})
                ext_n += 1
        job["download_links"] = links
        job["link_ttl_hours"] = ttl if local_n else None
        job["links_expire_at"] = time.time() + ttl * 3600 if local_n else None
        job["mirror"] = {"local": local_n, "external": ext_n}

        # 邮件通知（真实发送）
        if SS.get("download.email_on_ready"):
            to = job.get("email") or ""
            if not to and job.get("username"):
                with db() as c:
                    r = c.execute("SELECT email FROM users WHERE username=?",
                                  (job["username"],)).fetchone()
                to = (r["email"] if r else "") or ""
            if to:
                ok, msg = mailer.notify_build_done(
                    to, job.get("username", ""), req, files, links,
                    job.get("duration") or 0, ttl if local_n else 0, ok=True)
                job["mail_status"] = {"ok": ok, "to": to, "detail": msg}
                print(f"[notify] 邮件 -> {to}: {msg}", flush=True)
            else:
                job["mail_status"] = {"ok": False, "to": "",
                                      "detail": "用户未填写邮箱，跳过通知"}
    elif status == "failed" and SS.get("mail.notify_fail"):
        to = job.get("email") or ""
        if not to and job.get("username"):
            with db() as c:
                r = c.execute("SELECT email FROM users WHERE username=?",
                              (job["username"],)).fetchone()
            to = (r["email"] if r else "") or ""
        if to:
            ok, msg = mailer.notify_build_done(
                to, job.get("username", ""), req, [], [],
                job.get("duration") or 0, 0, ok=False,
                err=(job.get("stderr") or "")[-1200:])
            job["mail_status"] = {"ok": ok, "to": to, "detail": msg}


def _wire_queue():
    """把后端选择器与完成回调接到构建队列上。"""
    q = builder.get_queue()
    q.runner = lambda req, prog, handle: backends.get_backend().dispatch(req, prog, handle)
    q.on_finished = on_build_finished
    return q


@app.get("/api/v1/admin/build_backend")
def admin_build_backend(request: Request):
    require_admin(request)
    return backends.info()


def releases_info():
    out = []
    for b in ("25.12", "24.10"):
        r = releases.resolve(b)
        out.append({"branch": b, "release": r["release"], "backend": r["backend"]})
    return out


# --- 用户管理 -------------------------------------------------------------- #
@app.get("/api/v1/admin/users")
def admin_users(request: Request, q: str = "", role: str = "", status: str = ""):
    require_admin(request)
    sql, args = "SELECT id,username,email,sponsor,role,disabled,quota,created,last_login FROM users WHERE 1=1", []
    if q:
        sql += " AND username LIKE ?"; args.append(f"%{q}%")
    if role:
        sql += " AND role=?"; args.append(role)
    if status == "disabled":
        sql += " AND disabled=1"
    elif status == "active":
        sql += " AND disabled=0"
    sql += " ORDER BY id"
    with db() as c:
        rows = [dict(r) for r in c.execute(sql, args)]
        for r in rows:
            r["build_count"] = c.execute("SELECT COUNT(*) n FROM builds WHERE username=?",
                                         (r["username"],)).fetchone()["n"]
    return {"total": len(rows), "users": rows}


@app.post("/api/v1/admin/user")
async def admin_user_op(request: Request, username: str = Form(...), action: str = Form(...),
                        value: str = Form("")):
    a = is_admin(request)
    require_admin(request)
    with db() as c:
        u = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if not u:
        raise HTTPException(404, "用户不存在")
    ip = client_ip(request)

    if action == "set_role":
        if value not in ("user", "admin"):
            raise HTTPException(400, "role 只能为 user / admin")
        if username == a["username"] and value != "admin":
            return JSONResponse({"status": "error", "detail": "不能撤销自己的管理员权限"},
                                status_code=400)
        if username == "admin" and value != "admin":
            return JSONResponse({"status": "error", "detail": "内置管理员账号不可降权"},
                                status_code=400)
        with db() as c:
            c.execute("UPDATE users SET role=? WHERE username=?", (value, username))
    elif action == "set_sponsor":
        v = 1 if value in ("1", "true", "yes") else 0
        with db() as c:
            c.execute("UPDATE users SET sponsor=? WHERE username=?", (v, username))
    elif action == "set_quota":
        try:
            n = max(0, min(9999, int(value)))
        except ValueError:
            raise HTTPException(400, "配额必须为整数")
        with db() as c:
            c.execute("UPDATE users SET quota=? WHERE username=?", (n, username))
    elif action == "disable":
        if username == a["username"]:
            return JSONResponse({"status": "error", "detail": "不能停用自己的账号"},
                                status_code=400)
        if username == "admin":
            return JSONResponse({"status": "error", "detail": "内置管理员账号不可停用"},
                                status_code=400)
        with db() as c:
            c.execute("UPDATE users SET disabled=1 WHERE username=?", (username,))
            c.execute("DELETE FROM sessions WHERE username=?", (username,))  # 立即踢下线
    elif action == "enable":
        with db() as c:
            c.execute("UPDATE users SET disabled=0 WHERE username=?", (username,))
    elif action == "kick":
        with db() as c:
            n = c.execute("DELETE FROM sessions WHERE username=?", (username,)).rowcount
        audit(a["username"], "kick", username, f"清除 {n} 个会话", ip)
        return {"status": "ok", "kicked": n}
    elif action == "reset_password":
        if len(value) < 6:
            raise HTTPException(400, "新密码至少 6 位")
        with db() as c:
            # ★ 同注册路径：必须用 _hash_pw，不能写无盐 SHA256
            c.execute("UPDATE users SET password=? WHERE username=?",
                      (_hash_pw(value), username))
            c.execute("DELETE FROM sessions WHERE username=?", (username,))
    elif action == "delete":
        if username in ("admin", a["username"]):
            return JSONResponse({"status": "error", "detail": "不可删除该账号"}, status_code=400)
        with db() as c:
            c.execute("DELETE FROM users WHERE username=?", (username,))
            c.execute("DELETE FROM sessions WHERE username=?", (username,))
        audit(a["username"], "delete_user", username, "删除账户", ip)
        return {"status": "ok"}
    else:
        raise HTTPException(400, f"未知操作 {action}")

    audit(a["username"], action, username, f"value={value}"[:200], ip)
    return {"status": "ok"}


@app.post("/api/v1/admin/user/create")
async def admin_user_create(request: Request, username: str = Form(...),
                            password: str = Form(...), email: str = Form(""),
                            role: str = Form("user"), sponsor: str = Form("0")):
    a = is_admin(request)
    require_admin(request)
    if len(username) < 3 or len(password) < 6:
        return JSONResponse({"status": "error", "detail": "用户名≥3位，密码≥6位"}, status_code=400)
    if role not in ("user", "admin"):
        role = "user"
    try:
        with db() as c:
            # ★ 同注册路径：必须 _hash_pw（PBKDF2+盐），不能写无盐 SHA256
            c.execute("INSERT INTO users(username,password,email,sponsor,role,created) "
                      "VALUES(?,?,?,?,?,?)",
                      (username, _hash_pw(password), email,
                       1 if sponsor in ("1", "true") else 0, role, time.time()))
    except sqlite3.IntegrityError:
        return JSONResponse({"status": "error", "detail": "用户名已存在"}, status_code=409)
    audit(a["username"], "create_user", username, f"role={role} sponsor={sponsor}",
          client_ip(request))
    return {"status": "ok"}


# --- 构建管理 -------------------------------------------------------------- #
@app.get("/api/v1/admin/builds")
def admin_builds(request: Request, status: str = "", q: str = "", limit: int = 200):
    require_admin(request)
    sql = ("SELECT b.*, (SELECT COUNT(*) FROM builds) total FROM builds b WHERE 1=1")
    args = []
    if status:
        sql += " AND b.status=?"; args.append(status)
    if q:
        sql += " AND (b.username LIKE ? OR b.request_hash LIKE ? OR b.packages LIKE ?)"
        args += [f"%{q}%"] * 3
    sql += " ORDER BY b.created DESC LIMIT ?"; args.append(max(1, min(1000, limit)))
    q_ = builder.get_queue()
    with db() as c:
        rows = [dict(r) for r in c.execute(sql, args)]
        # 合并 jobs 表的 stderr/stdout/detail（构建失败时展示详细原因）
        hashes = [r["request_hash"] for r in rows]
        job_results = {}
        if hashes:
            placeholders = ",".join("?" * len(hashes))
            for jr in c.execute(
                f"SELECT request_hash, status, detail, result FROM jobs WHERE request_hash IN ({placeholders})",
                hashes
            ):
                jr = dict(jr)
                result = {}
                try:
                    result = json.loads(jr.get("result") or "{}")
                except Exception:
                    pass
                job_results[jr["request_hash"]] = {
                    "detail": jr.get("detail") or result.get("detail", ""),
                    "stderr": result.get("stderr", ""),
                    "stdout": result.get("stdout", ""),
                    "duration": result.get("duration"),
                }
    for r in rows:
        # 合入 jobs 表的详细字段
        jr = job_results.get(r["request_hash"])
        if jr:
            r.setdefault("detail", jr["detail"])
            r.setdefault("stderr", jr["stderr"])
            r.setdefault("stdout", jr["stdout"])
            if jr.get("duration") and not r.get("duration"):
                r["duration"] = jr["duration"]
        j = q_.get(r["request_hash"])
        if j:
            r["live_status"] = j.get("status")
            r["imagebuilder_status"] = j.get("imagebuilder_status")
            r["files"] = len(j.get("files") or [])
            # 运行中任务也能看到实时 stderr（部分输出）
            if not r.get("stderr"):
                r["stderr"] = j.get("stderr", "")
            if not r.get("stdout"):
                r["stdout"] = j.get("stdout", "")
        # 产物是否还在磁盘上：管理员单独删过产物后，记录仍在，
        # 但「产物」链接会指向已不存在的目录，需要据此隐藏。
        try:
            r["has_artifacts"] = artifacts._safe_dir(r["request_hash"]) is not None
        except Exception:
            r["has_artifacts"] = False
    return {"total": len(rows), "builds": rows}


# --- 本地构建物（产物盘）管理 ---------------------------------------------- #
@app.get("/api/v1/admin/artifacts")
def admin_artifacts(request: Request):
    """盘点本地构建物：每个构建占多大、谁是孤儿目录、上传目录占用。

    为什么需要单独一个只读端点：`/admin/builds` 只列得出「有记录的构建」，
    磁盘上真实的占用（尤其是 builds 行已不存在、目录还在的孤儿）看不到，
    管理员无法判断该删谁。
    """
    require_admin(request)
    sn = artifacts.scan()
    up = artifacts.uploads_scan()
    return {
        "items": sn["items"],
        "total_bytes": sn["total_bytes"],
        "total_files": sn["total_files"],
        "orphan_bytes": sn["orphan_bytes"],
        "orphan_count": sn["orphan_count"],
        "uploads": up,
        "store_bytes": sn["total_bytes"] + up["bytes"],
    }


@app.post("/api/v1/admin/artifact")
async def admin_artifact_op(request: Request, action: str = Form(...),
                            request_hash: str = Form("")):
    a = is_admin(request)
    require_admin(request)
    ip = client_ip(request)

    if action in ("delete", "delete_record"):
        hid = check_hash_id(request_hash)
        r = artifacts.delete(hid, drop_record=(action == "delete_record"))
        audit(a["username"], "delete_artifact", hid,
              f"释放 {r['freed']} 字节 / {r['files']} 个文件，"
              f"作废令牌 {r['revoked_tokens']} 个"
              f"{'，已删除构建记录' if r['record_dropped'] else '，保留构建记录'}",
              ip)
        return {"status": "ok", **r}

    if action == "delete_orphans":
        r = artifacts.delete_orphans()
        audit(a["username"], "delete_orphan_artifacts", "",
              f"清理 {r['removed']} 个孤儿产物目录，释放 {r['freed']} 字节", ip)
        return {"status": "ok", **r}

    raise HTTPException(400, f"未知操作 {action}")


@app.post("/api/v1/admin/build")
async def admin_build_op(request: Request, request_hash: str = Form(...), action: str = Form(...)):
    a = is_admin(request)
    require_admin(request)
    ip = client_ip(request)
    q = builder.get_queue()
    job = q.get(request_hash)
    with db() as c:
        row = c.execute("SELECT * FROM builds WHERE request_hash=?", (request_hash,)).fetchone()

    if action == "cancel":
        if not job:
            # 任务不在内存队列（服务重启后丢失），尝试从持久化表补救
            js = jobs.get(request_hash)
            db_status = (row["status"] if row else None) or (js.get("status") if js else None)
            if db_status in ("done", "failed", "cancelled"):
                return JSONResponse({"status": "error", "detail": "任务已结束，无法取消"}, status_code=400)
            # 直接把数据库状态标记为 cancelled（内存队列里已不存在，无需 kill 进程）
            with db() as c:
                c.execute("UPDATE builds SET status='cancelled' WHERE request_hash=?", (request_hash,))
            if js:
                jobs.put({**js, "status": "cancelled"})
            audit((a or {}).get("username", "admin"), "cancel_build", request_hash,
                  "cancelled=db_only(not_in_queue)", ip)
            return {"status": "ok", "cancelled": True, "note": "任务不在内存队列，已标记为已取消"}
        if job.get("status") in ("done", "failed", "cancelled"):
            return JSONResponse({"status": "error", "detail": "任务已结束，无法取消"}, status_code=400)
        ok = q.cancel(request_hash)
        with db() as c:
            c.execute("UPDATE builds SET status='cancelled' WHERE request_hash=?", (request_hash,))
        audit((a or {}).get("username", "admin"), "cancel_build", request_hash, f"cancelled={ok}", ip)
        return {"status": "ok", "cancelled": ok}

    if action == "retry":
        if not row:
            raise HTTPException(404, "任务记录不存在")
        if job and job.get("status") in ("queued", "started", "running"):
            return JSONResponse({"status": "error", "detail": "任务仍在队列中，无需重试"},
                                status_code=400)
        req = json.loads(row["payload"] or "{}")
        nj = q.submit(req)
        with db() as c:
            c.execute("INSERT OR REPLACE INTO builds(request_hash,username,target,profile,"
                      "packages,status,created,payload) VALUES(?,?,?,?,?,?,?,?)",
                      (nj["request_hash"], row["username"], row["target"], row["profile"],
                       row["packages"], "queued", time.time(), json.dumps(req)[:200000]))
        audit(a["username"], "retry_build", nj["request_hash"], f"from={request_hash}", ip)
        return {"status": "ok", "request_hash": nj["request_hash"]}

    if action == "delete":
        if job and job.get("status") in ("queued", "started", "running"):
            return JSONResponse({"status": "error", "detail": "任务进行中，请先取消"}, status_code=400)
        hid = check_hash_id(request_hash)
        d = os.path.join(STORE, hid)
        freed = 0
        if os.path.isdir(d):
            freed = sum(os.path.getsize(os.path.join(d, f))
                        for f in os.listdir(d) if os.path.isfile(os.path.join(d, f)))
            shutil.rmtree(d, ignore_errors=True)
        with db() as c:
            c.execute("DELETE FROM builds WHERE request_hash=?", (request_hash,))
        audit(a["username"], "delete_build", request_hash, f"释放 {freed} 字节", ip)
        return {"status": "ok", "freed": freed}

    raise HTTPException(400, f"未知操作 {action}")


# --- 队列控制 -------------------------------------------------------------- #
@app.post("/api/v1/admin/queue")
async def admin_queue_op(request: Request, action: str = Form(...), value: str = Form("")):
    a = is_admin(request)
    require_admin(request)
    ip = client_ip(request)
    q = builder.get_queue()
    if action == "set_concurrency":
        try:
            n = max(1, min(8, int(value)))
        except ValueError:
            raise HTTPException(400, "并发数必须为整数")
        q.concurrency = n
        set_setting("builder.max_concurrent", str(n))
        q._pump()
        audit(a["username"], "set_concurrency", "", f"concurrency={n}", ip)
        return {"status": "ok", "concurrency": n}
    if action == "clear_finished":
        gone = [k for k, v in list(q.jobs.items()) if v.get("status") in ("done", "failed", "cancelled")]
        for k in gone:
            q.jobs.pop(k, None)
        audit(a["username"], "clear_finished", "", f"清理 {len(gone)} 条内存任务", ip)
        return {"status": "ok", "cleared": len(gone)}
    if action == "purge_store":
        # 收口到 artifacts.purge_all：原实现只删目录与 builds 行，
        # 留下的 dl_tokens 会变成指向已删文件的悬空限时链接（点开 404）。
        r = artifacts.purge_all()
        audit(a["username"], "purge_store", "",
              f"清除全部产物 {r['removed']} 个，释放 {r['freed']} 字节，"
              f"并作废全部下载令牌", ip)
        return {"status": "ok", "freed": r["freed"], "removed": r["removed"]}
    if action == "toggle_builder":
        cur = BUILD_CFG()["enabled"]
        set_setting("builder.enabled", "false" if cur else "true")
        audit(a["username"], "toggle_builder", "", f"{cur} -> {not cur}", ip)
        return {"status": "ok", "enabled": not cur}
    raise HTTPException(400, f"未知操作 {action}")


# --- 工单 / 插件提议 -------------------------------------------------------- #
@app.get("/api/v1/admin/proposals")
def admin_proposals(request: Request, status: str = ""):
    require_admin(request)
    sql = "SELECT * FROM proposals WHERE 1=1"
    args = []
    if status:
        sql += " AND COALESCE(status,'pending')=?"; args.append(status)
    sql += " ORDER BY created DESC LIMIT 500"
    with db() as c:
        return {"proposals": [dict(r) for r in c.execute(sql, args)]}


@app.post("/api/v1/admin/proposal")
async def admin_proposal_op(request: Request, pid: int = Form(...), action: str = Form(...),
                            reply: str = Form("")):
    a = is_admin(request)
    require_admin(request)
    if action not in ("approve", "reject", "pending"):
        raise HTTPException(400, "action 必须为 approve / reject / pending")
    with db() as c:
        r = c.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone()
        if not r:
            raise HTTPException(404, "提议不存在")
        c.execute("UPDATE proposals SET status=?, reply=? WHERE id=?",
                  ({"approve": "approved", "reject": "rejected", "pending": "pending"}[action],
                   reply, pid))
    audit(a["username"], f"proposal_{action}", str(pid), f"reply={reply}"[:200], client_ip(request))
    return {"status": "ok"}


# --- 审计日志 -------------------------------------------------------------- #
@app.get("/api/v1/admin/logs")
def admin_logs(request: Request, limit: int = 300, q: str = ""):
    require_admin(request)
    sql, args = "SELECT * FROM admin_logs WHERE 1=1", []
    if q:
        sql += " AND (admin LIKE ? OR action LIKE ? OR target LIKE ? OR detail LIKE ?)"
        args += [f"%{q}%"] * 4
    sql += " ORDER BY id DESC LIMIT ?"; args.append(max(1, min(2000, limit)))
    with db() as c:
        return {"logs": [dict(r) for r in c.execute(sql, args)]}


@app.post("/api/v1/admin/logs/clear")
def admin_logs_clear(request: Request):
    a = is_admin(request)
    require_admin(request)
    with db() as c:
        n = c.execute("SELECT COUNT(*) n FROM admin_logs").fetchone()["n"]
        c.execute("DELETE FROM admin_logs")
    audit(a["username"], "clear_logs", "", f"清除 {n} 条审计日志", client_ip(request))
    return {"status": "ok", "cleared": n}


# --- 封禁 ------------------------------------------------------------------ #
@app.get("/api/v1/admin/bans")
def admin_bans(request: Request):
    require_admin(request)
    with db() as c:
        return {"bans": [dict(r) for r in c.execute("SELECT * FROM bans ORDER BY id DESC")]}


@app.post("/api/v1/admin/ban")
async def admin_ban_op(request: Request, action: str = Form(...), kind: str = Form("ip"),
                       value: str = Form(""), reason: str = Form("")):
    a = is_admin(request)
    require_admin(request)
    ip = client_ip(request)
    if action == "add":
        if not value.strip():
            raise HTTPException(400, "封禁对象不能为空")
        with db() as c:
            c.execute("INSERT INTO bans(kind,value,reason,created) VALUES(?,?,?,?)",
                      (kind, value.strip(), reason, time.time()))
            if kind == "user":
                c.execute("DELETE FROM sessions WHERE username=?", (value.strip(),))
        audit(a["username"], "ban_add", f"{kind}:{value}", reason, ip)
        return {"status": "ok"}
    if action == "remove":
        try:
            bid = int(value)
        except ValueError:
            raise HTTPException(400, "需要封禁记录 id")
        with db() as c:
            c.execute("DELETE FROM bans WHERE id=?", (bid,))
        audit(a["username"], "ban_remove", str(bid), "", ip)
        return {"status": "ok"}
    raise HTTPException(400, f"未知操作 {action}")


# --- 站点配置 -------------------------------------------------------------- #
@app.get("/api/v1/admin/settings")
def admin_settings(request: Request):
    """兼容旧前端的摘要接口；完整配置请用 /api/v1/admin/site。"""
    require_admin(request)
    return {
        "registration_open": bool(SS.get("registration_open", True)),
        "default_quota": int(SS.get("default_quota") or 12),
        "announcement": SS.get("announcement") or "",
        "sponsor_enabled": bool(SS.get("sponsor.enabled")),
        "builder_enabled": BUILD_CFG()["enabled"],
        "max_concurrent": BUILD_CFG()["max_concurrent"],
        "site_name": SS.get("site_name") or "",
        "build_backend": (SS.get("builder.backend") or "local"),
    }


@app.post("/api/v1/admin/settings")
async def admin_settings_set(request: Request, key: str = Form(...), value: str = Form("")):
    """写入单个配置项 —— 白名单由 sitesettings.SCHEMA 决定。"""
    a = is_admin(request)
    require_admin(request)
    if key not in SS.BY_KEY:
        raise HTTPException(400, f"不支持的配置项 {key}")
    try:
        v = SS.set_(key, value)
    except ValueError as e:
        raise HTTPException(400, str(e))
    # 密钥类配置绝不明文入审计日志（admin_logs.detail 管理员可读、
    # 且长期留库）：只记「已更新」，长度可用于判断是否为空。
    spec = SS.BY_KEY.get(key) or {}
    if spec.get("secret"):
        detail = f"secret updated (len={len(str(v or ''))})"
    else:
        detail = f"value={str(v)[:160]}"
    audit(a["username"], "set_setting", key, detail, client_ip(request))
    return {"status": "ok", "key": key, "value": v}


# --------------------------------------------------------------------------- #
# 产物 / 固件库
# --------------------------------------------------------------------------- #
@app.get("/dl/{path:path}")
def dl_proxy(path: str):
    """已编译固件直链：转发到上游固件库 dl.openwrt.ai（真实文件、真实可下载）。"""
    safe = "/".join(p for p in path.split("/") if p and p not in (".", ".."))
    return RedirectResponse("https://dl.openwrt.ai/" + safe, status_code=302)


def _may_read_store(request: Request, hash_: str):
    """判定当前请求是否有权读取某个构建的产物目录。

    U-8：`/store/{hash}/{name}` 原来是**完全无鉴权**的直链 —— 只要知道
    hash 就能下载任意构建产物（别人的固件/含自定义文件的包）。
    现在要求：管理员，或该构建的属主登录用户。

    `/dl/t/<token>`（限时签名令牌）不受影响 —— 那才是给匿名下载用的通道，
    构建完成后签发的链接走的都是它。
    返回 (ok, 用户名)；重建历史任务时 username 可能为空。
    """
    u = current_user(request)
    if u and (u.get("role") == "admin" or u.get("is_admin")):
        return True, "admin"
    if not u:
        return False, None
    with db() as c:
        r = c.execute("SELECT username FROM builds WHERE request_hash=?",
                      (hash_,)).fetchone()
    if r is None:
        # 老记录没有 builds 行：会话有效即放行（避免历史链接触死）
        return True, u["username"]
    owner = (r["username"] or "").strip()
    if not owner:
        return True, u["username"]              # 匿名构建留下的记录
    return owner == u["username"], u["username"]


@app.get("/store/{hash_}/")
def store_index(hash_: str, request: Request):
    """构建产物目录索引（真实列出该任务产出的文件）。"""
    try:
        hid = check_hash_id(hash_)
    except HTTPException:
        raise HTTPException(404)
    ok, who = _may_read_store(request, hid)
    if not ok:
        raise HTTPException(401 if who is None else 403,
                            "需要登录后访问自己的构建产物" if who is None
                            else "无权访问该构建的产物")
    base = os.path.join(STORE, hid)
    if not os.path.isdir(base):
        # 回退到构建记录，给出明确提示而不是空白 404
        with db() as c:
            r = c.execute("SELECT status FROM builds WHERE request_hash=?", (hash_,)).fetchone()
        if r:
            return JSONResponse({"request_hash": hash_, "status": r["status"], "files": []},
                                status_code=200)
        raise HTTPException(404)
    files = []
    for n in sorted(os.listdir(base)):
        # 过滤隐藏/中间文件：回传过程中的 `.x.zip`（半截包）与 `.x.zip.fp`
        # （身份指纹）都是内部状态，既不该出现在产物清单里，也不该可被下载。
        if n.startswith("."):
            continue
        p = os.path.join(base, n)
        if os.path.isfile(p):
            files.append({"name": n, "size": os.path.getsize(p)})
    return JSONResponse({"request_hash": hash_, "count": len(files), "files": files})


@app.get("/store/{hash_}/{name}")
def store_file(hash_: str, name: str, request: Request):
    try:
        hid = check_hash_id(hash_)
    except HTTPException:
        raise HTTPException(404)                   # 不暴露用户上传包 / 非法 ID
    ok, who = _may_read_store(request, hid)
    if not ok:
        raise HTTPException(401 if who is None else 403,
                            "需要登录后访问自己的构建产物" if who is None
                            else "无权访问该构建的产物")
    base = os.path.join(STORE, hid)
    bn = os.path.basename(name)
    # 隐藏/中间文件不可下载（半截的回传包、身份指纹等内部状态）
    if not bn or bn.startswith("."):
        raise HTTPException(404)
    p = os.path.join(base, bn)
    # 二次确认：解析后仍须落在该构建目录内
    if not os.path.realpath(p).startswith(os.path.realpath(base) + os.sep):
        raise HTTPException(404)
    if os.path.isfile(p):
        return FileResponse(p, media_type="application/octet-stream", filename=os.path.basename(name))
    raise HTTPException(404)


@app.get("/firmware/{target:path}", response_class=HTMLResponse)
def firmware_list(target: str = "", dev: str = ""):
    """历史版本 / 固件目录页（对齐上游 dl.openwrt.ai 目录索引）。

    路由用 {target:path} 是因为 target 本身含斜杠（如 x86/64）；
    原先的 /firmware/{target}/{dev}/ 永远匹配不到，恒 404。
    同时支持 /firmware/x86/64/generic/ 这种「target + 设备」的组合写法：
    若末段不在 target 映射里，则把它当作设备名剥离。
    """
    target = (target or "").strip("/")
    if not target:
        raise HTTPException(404, "缺少 target")

    # 规范化：把 target 里的斜杠还原成映射表里的形式，并识别可选的设备段
    known = set()
    try:
        with open(os.path.join(DATA, "json", "v1", "overview.json"), encoding="utf-8") as _f:
            ov = json.load(_f)
        for b in (ov.get("branches") or {}).values():
            known.update((b.get("targets") or {}).keys())
    except Exception as e:
        # 读不到就不做规范化（行为可接受），但静默会让「target 写法怪」类问题无从查起
        print(f"[firmware] overview.json 读取失败，跳过 target 规范化: "
              f"{type(e).__name__}: {e}", flush=True)

    if target not in known and "/" in target:
        head = target.rsplit("/", 1)[0]
        if head in known:
            dev = target.rsplit("/", 1)[1]
            target = head

    # 产物按 target 过滤：目录名形如 x86-64-generic-...，用 target 的横线形式匹配
    key = target.replace("/", "-")
    rows = []
    if os.path.isdir(STORE):
        for h in sorted(os.listdir(STORE)):
            d = os.path.join(STORE, h)
            if not os.path.isdir(d) or h == "uploads":
                continue
            for n in sorted(os.listdir(d)):
                # 隐藏/中间文件不入清单（半截回传包、.fp 身份指纹）
                if n.startswith("."):
                    continue
                fp = os.path.join(d, n)
                if not os.path.isfile(fp):
                    continue
                # 只列与该 target 相关的产物（原实现是 `... or True`，恒真，会把全站产物都列出来）
                if key and key not in n and key.replace("-", "_") not in n:
                    continue
                try:
                    sz = os.path.getsize(fp)
                except OSError:
                    continue
                mtime = os.path.getmtime(fp)
                rows.append((f"/store/{h}/{n}", n, sz, mtime, h))

    if not rows:
        return HTMLResponse(
            f"<h3>暂无 {target}{('/' + dev) if dev else ''} 的构建产物</h3>"
            f"<p>请在首页选择设备并构建。</p>")
    rows.sort(key=lambda r: r[3], reverse=True)
    li = "".join(
        f'<li><a href="{u}">{n}</a> <small>{s / 1048576:.1f} MB · '
        f'{time.strftime("%Y-%m-%d %H:%M", time.localtime(mt))}</small></li>'
        for u, n, s, mt, _h in rows)
    return HTMLResponse(
        f"<h3>{target}{('/' + dev) if dev else ''} 固件列表</h3>"
        f"<p>共 {len(rows)} 个文件</p><ul>{li}</ul>")


@app.get("/packages/", response_class=HTMLResponse)
def packages_page(request: Request):
    """自建软件库索引页。"""
    return _serve_page("packages.html", request, with_entry=True)


@app.get("/api/v1/packages/archs")
def api_package_archs():
    """软件库架构清单：扫描本地索引文件，返回架构名与包数量。

    纯读本地目录，不接受任何路径参数 —— 无目录穿越面。
    """
    idx = os.path.join(DATA, "json", "v1", "releases")
    out = []
    if not os.path.isdir(idx):
        return {"count": 0, "archs": [], "releases": []}
    for mfr in sorted(os.listdir(idx)):
        d = os.path.join(idx, mfr)
        if not os.path.isdir(d):
            continue
        for n in sorted(os.listdir(d)):
            if not n.endswith("-index.json"):
                continue
            arch = n[: -len("-index.json")]
            try:
                with open(os.path.join(d, n), encoding="utf-8") as _f:
                    data = json.load(_f)
                cnt = len(data)
            except Exception:
                cnt = 0
            out.append({
                "release": mfr.replace("packages-", ""),
                "src": mfr,
                "arch": arch,
                "count": cnt,
                "url": f"/json/v1/releases/{mfr}/{n}",
            })
    releases = sorted({r["release"] for r in out})
    return {"count": len(out), "archs": out, "releases": releases}


@app.get("/api/v1/packages/search")
def api_package_search(q: str = "", arch: str = "", src: str = "", limit: int = 50):
    """在指定架构的包索引里检索。

    arch/src 只用于拼装文件名，且经白名单校验（必须命中本地已存在的索引文件），
    因此不存在路径穿越。limit 上限 200，防大响应拖垮前端。
    """
    q = (q or "").strip().lower()
    limit = max(1, min(200, int(limit or 50)))
    base = os.path.join(DATA, "json", "v1", "releases")
    if not arch or not src:
        return {"count": 0, "total": 0, "packages": [], "detail": "需指定架构"}

    # 白名单：只允许本地真实存在的索引文件名
    safe = re.sub(r"[^A-Za-z0-9_.\-]", "", arch)
    safe_src = re.sub(r"[^A-Za-z0-9_.\-]", "", src)
    fn = f"{safe}-index.json"
    path = os.path.join(base, safe_src, fn)
    real = os.path.realpath(path)
    if not real.startswith(os.path.realpath(base) + os.sep) or not os.path.isfile(real):
        return {"count": 0, "total": 0, "packages": [], "detail": "架构不存在"}

    try:
        with open(real, encoding="utf-8") as _f:
            data = json.load(_f)
    except Exception as e:
        return {"count": 0, "total": 0, "packages": [], "detail": f"索引读取失败: {e}"}

    items = list(data.items()) if isinstance(data, dict) else []
    total = len(items)
    if q:
        items = [(k, v) for k, v in items if q in str(k).lower()]
    items.sort(key=lambda kv: str(kv[0]))
    pkgs = [{"name": k, "version": v} for k, v in items[:limit]]
    return {"count": len(pkgs), "total": len(items), "matched": total,
            "truncated": len(items) > limit, "packages": pkgs, "arch": safe, "src": safe_src}


@app.get("/newpkg/", response_class=HTMLResponse)
def newpkg_page(request: Request):
    """新插件收录提议页。"""
    return _serve_page("newpkg.html", request, with_entry=True)


@app.get("/api/v1/proposals")
def api_proposals_list(limit: int = 50):
    """公开的提议列表（只回传非敏感的展示字段，不含管理端回复原文以外的信息）。"""
    limit = max(1, min(200, int(limit or 50)))
    with db() as c:
        rows = c.execute(
            "SELECT id,name,url,note,created,status FROM proposals "
            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        cnt = c.execute("SELECT COUNT(*) FROM proposals").fetchone()[0]
        by = {r["status"] or "pending": r["n"] for r in c.execute(
            "SELECT COALESCE(status,'pending') status, COUNT(*) n FROM proposals GROUP BY 1")}
    return {"count": cnt, "proposals": [dict(r) for r in rows], "by_status": by}


@app.post("/api/v1/propose")
async def api_propose(request: Request, name: str = Form(...), url: str = Form(...),
                      note: str = Form("")):
    """提交插件收录提议。

    - 插件名与仓库地址做长度与格式校验（仓库须是 http(s) 链接）
    - 同一仓库地址不重复收录
    - 基础频率限制：同 IP 10 分钟内最多 5 次
    """
    name = (name or "").strip()[:80]
    url = (url or "").strip()[:300]
    note = (note or "").strip()[:500]
    if not name:
        return JSONResponse({"detail": "插件名不能为空"}, status_code=400)
    if not re.match(r"^https?://[^\s]+$", url):
        return JSONResponse({"detail": "仓库地址必须是 http(s) 链接"}, status_code=400)

    # 必须用 client_ip()：它认 X-Forwarded-For。
    # 原实现用 request.client.host，在反向代理后所有用户 IP 相同 ——
    # 既让频率限制失效，又让 /api/v1/propose/{id} 的「同 IP 可撤回」判定
    # 变成「任何人可撤回他人未审提议」。
    ip = client_ip(request) or "-"
    now = time.time()
    with db() as c:
        recent = c.execute("SELECT COUNT(*) FROM proposals WHERE created > ? AND ip = ?",
                           (now - 600, ip)).fetchone()[0]
        if recent >= 5:
            return JSONResponse({"detail": "提交过于频繁，请 10 分钟后再试"}, status_code=429)
        dup = c.execute("SELECT id FROM proposals WHERE url = ?", (url,)).fetchone()
        if dup:
            return JSONResponse({"detail": "该仓库已在收录列表中", "id": dup["id"]},
                                status_code=409)
        c.execute("INSERT INTO proposals(name,url,note,created,ip,status) "
                  "VALUES(?,?,?,?,?,'pending')", (name, url, note, now, ip))
        pid = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
    return {"status": "ok", "id": pid, "detail": "提议已提交，等待管理员审核"}


@app.delete("/api/v1/propose/{pid}")
def api_propose_delete(pid: int, request: Request):
    """提交者撤回自己的提议（仅未审核状态，且需管理员或同 IP）。"""
    u = current_user(request)
    ip = client_ip(request) or "-"
    with db() as c:
        row = c.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone()
        if not row:
            return JSONResponse({"detail": "提议不存在"}, status_code=404)
        if (row["status"] or "pending") != "pending":
            return JSONResponse({"detail": "该提议已审核，无法撤回"}, status_code=400)
        if not (u and u.get("role") == "admin") and (row["ip"] or "") != ip:
            return JSONResponse({"detail": "无权撤回他人提议"}, status_code=403)
        c.execute("DELETE FROM proposals WHERE id=?", (pid,))
    return {"status": "ok", "detail": "已撤回"}


@app.get("/contact/", response_class=HTMLResponse)
def contact_page(request: Request):
    return _serve_page("contact.html", request, with_entry=True)


@app.get("/fadian/", response_class=HTMLResponse)
def fadian_page(request: Request):
    """为爱发电：赞助权益说明。"""
    return _serve_page("fadian.html", request, with_entry=True)


@app.get("/healthz")
def healthz():
    q = builder.get_queue()
    return {"ok": True, "jobs": len(q.jobs),
            "builds_done": sum(1 for j in q.jobs.values() if j.get("status") == "done"),
            "builder_enabled": BUILD_CFG()["enabled"],
            "concurrency": q.concurrency,
            "backend": backends.info()["effective"],
            "mail": mailer.ready(),
            # 版本号：排障时第一件要问的事，别让人去翻文件
            "version": _version.VERSION}


# --------------------------------------------------------------------------- #
# 路径式版本化的静态资源
# --------------------------------------------------------------------------- #
# 为什么需要：版本号放在 ?v= 查询串里**穿不过 EdgeOne** —— 实测其缓存键忽略
#   查询参数（拿一个全新的 ?v= 请求，仍返回旧缓存、Age 非 0）。只有路径变了，
#   CDN 才认定换了资源。于是把版本号放进路径：
#
#       /assets/_v/<ver>/js/login.js   →   web/assets/js/login.js
#
#   这一层同时解决浏览器、宝塔反代的全局 proxy_cache、以及外部 CDN 三处缓存。
#   裸路径 /assets/js/login.js 仍然照常工作（只是不享受长缓存）。
_VERSION_SEG = "_v"


def _versioned_static(prefix: str, ver: str, rest: str):
    """把 /<prefix>/_v/<ver>/<rest> 映射回 web/<prefix>/<rest>。"""
    # 版本号白名单：只允许字母数字点下划线短横 —— 从入口就杜绝 ../ 之类
    if not re.fullmatch(r"[0-9A-Za-z._-]{1,40}", ver or ""):
        return Response(status_code=404)
    base = os.path.join(WEB, prefix)
    target = os.path.join(base, rest or "")
    # ★ 归属校验必须落在 realpath 上、且在**拼接之后**做。
    #   复用 builder._inside（就是为「startswith 不是祖先判定」这件事写的）。
    if not builder._inside(base, target):
        return Response(status_code=404)
    if not os.path.isfile(target):
        return Response(status_code=404)
    # 路径里已带版本 ⇒ 换资源必然换路径 ⇒ 长缓存绝对安全
    return FileResponse(target, headers=netcfg.asset_cache_headers(False, versioned=True))


def _mk_versioned_route(_prefix: str):
    async def _handler(ver: str, rest: str):
        return _versioned_static(_prefix, ver, rest)
    _handler.__name__ = f"versioned_{_prefix}"
    return _handler


# 必须注册在下面的 app.mount(...) **之前**：Starlette 按注册顺序匹配，
# 挂载是前缀匹配，放前面会把 /assets/_v/... 一起吞掉。
for _pfx in ("assets", "static", "vendor"):
    app.get(f"/{_pfx}/{_VERSION_SEG}/{{ver}}/{{rest:path}}")(_mk_versioned_route(_pfx))


app.mount("/static", RevalidatingStaticFiles(directory=os.path.join(WEB, "static")), name="static")

# 新版前端静态资源（设计系统 / Vue 组件 / 页面脚本）
app.mount("/assets", RevalidatingStaticFiles(directory=os.path.join(WEB, "assets")), name="assets")
# 本地化的第三方库（Vue 3 / jQuery），避免依赖公网 CDN
app.mount("/vendor", StaticFiles(directory=os.path.join(WEB, "vendor")), name="vendor")
# 前端数据文件（常用软件包标签等）
if os.path.isdir(os.path.join(WEB, "data")):
    app.mount("/data", RevalidatingStaticFiles(directory=os.path.join(WEB, "data")), name="webdata")


@app.on_event("startup")
def _startup_sync():
    """启动时把管理员保存的运行时配置应用到构建队列，并接上后端与完成回调。"""
    try:
        cfg = BUILD_CFG()
        q = _wire_queue()
        q.concurrency = max(1, min(8, int(cfg["max_concurrent"])))
        bi = backends.info()
        print(f"[admin] 队列并发 = {q.concurrency}, 构建引擎 = {cfg['enabled']}, "
              f"后端 = {bi['effective']}（配置：{bi['configured']}）", flush=True)
        print(f"[admin] 邮件通知 = {mailer.ready()}, "
              f"下载链接有效期 = {SS.get('download.link_ttl_hours')} 小时", flush=True)
    except Exception as e:
        print("[admin] startup sync failed:", e, flush=True)


def run():
    import uvicorn
    uvicorn.run(app, host=CFG["server"]["host"], port=CFG["server"]["port"])


if __name__ == "__main__":
    run()
