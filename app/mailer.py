#!/usr/bin/env python3
"""
邮件通知 —— 真实 SMTP 发送。

支持 SSL(465) / STARTTLS(587) / 明文(25) 三种加密方式，
构建完成后向用户邮箱发送带下载链接的通知邮件。

所有配置取自 sitesettings，模板支持变量替换。
"""
import collections
import os
import re
import smtplib
import ssl
import threading
import time
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid

from . import sitesettings as SS

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _cfg():
    return {
        "enabled":  SS.get("mail.enabled"),
        "host":     (SS.get("mail.host") or "").strip(),
        "port":     int(SS.get("mail.port") or 465),
        "enc":      SS.get("mail.encryption") or "ssl",
        "user":     (SS.get("mail.user") or "").strip(),
        "password": SS.get("mail.password") or "",
        "from_name": SS.get("mail.from_name") or "Kwrt",
        "from_addr": (SS.get("mail.from_addr") or "").strip(),
        "subject":  SS.get("mail.subject_tpl") or "[Kwrt] 固件构建完成",
        "body":     SS.get("mail.body_tpl") or "",
        "notify_fail": SS.get("mail.notify_fail"),
    }


def is_configured():
    """SMTP 参数是否齐备（不含 enabled 判断，用于管理台展示配置完整度）。"""
    c = _cfg()
    return bool(c["host"] and c["port"])


def ready():
    """
    是否「真的能发出去」：已启用 且 参数齐备 且 有发件人地址。
    依赖发信的功能（注册验证、重发、构建通知）必须用这个判断，
    否则会出现「以为能发，实际发不出」——账号建了却收不到信，用户被卡死。
    """
    c = _cfg()
    return bool(c["enabled"] and c["host"] and c["port"] and c["from_addr"])


def why_not_ready():
    """不能发信时给出具体原因，便于前端/管理台展示。"""
    c = _cfg()
    if not c["enabled"]:
        return "邮件通知未启用"
    if not c["host"] or not c["port"]:
        return "SMTP 服务器未配置"
    if not c["from_addr"]:
        return "发件人地址未配置"
    return ""


def _e(v):
    """HTML 转义。用于邮件正文插值 —— 产物名/用户名/target 等字段若含
    < > & " 会破坏邮件结构（虽非浏览器 XSS，仍应转义）。"""
    import html as _html
    return _html.escape(str(v if v is not None else ""), quote=True)


def render(tpl, ctx):
    """模板变量替换；未知变量原样保留，避免误伤正文。"""
    if not tpl:
        return ""
    def sub(m):
        k = m.group(1)
        return str(ctx.get(k, m.group(0)))
    return re.sub(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", sub, tpl)


# --------------------------------------------------------------------------- #
# HTML 邮件模板
# --------------------------------------------------------------------------- #
# 模板放 app/templates/mail/*.html，外框与各正文分离，改版式不必动 Python。
_TPL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "mail")
_TPL_NAME_RE = re.compile(r"^[a-z0-9_]+\.html$")
_TPL_CACHE = {}
_TPL_LOCK = threading.Lock()


def _tpl(name):
    """读取模板（带缓存）。模板名限定 [a-z0-9_]+.html，杜绝路径穿越。"""
    if not _TPL_NAME_RE.match(name or ""):
        raise ValueError(f"非法邮件模板名: {name!r}")
    with _TPL_LOCK:
        hit = _TPL_CACHE.get(name)
    if hit is not None:
        return hit
    path = os.path.join(_TPL_DIR, name)
    with open(path, encoding="utf-8") as f:
        t = f.read()
    with _TPL_LOCK:
        _TPL_CACHE[name] = t
    return t


def render_html(name, ctx, raw=()):
    """HTML 模板渲染。

    安全默认：**所有变量一律 HTML 转义**。只有调用方在 `raw` 里显式列出的键
    才按原样插入 —— 这类键必须是调用方已逐字段转义过的整段 HTML（如产物表格行）。
    做成「默认转义、显式放行」而非反过来，是因为新增模板时最容易忘的就是转义。
    """
    raw = set(raw or ())

    def sub(m):
        k = m.group(1)
        v = ctx.get(k, "")
        return str(v) if k in raw else _e(v)

    return re.sub(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}", sub, _tpl(name))


def render_mail(name, ctx, raw=(), accent="#2563eb", title=None):
    """渲染完整邮件：正文片段 + 统一外框（layout.html）。"""
    body = render_html(name, ctx, raw)
    return render_html("layout.html", {
        **ctx,
        "content": body,
        "accent": accent,
        "title": title or (ctx.get("site") or "通知"),
        "site": ctx.get("site") or "本站",
    }, raw=("content",))


def verify_html(site, username, url, ttl_hours):
    """邮箱验证邮件 HTML。"""
    return render_mail("verify.html", {
        "site": site, "username": username or "朋友",
        "verify_url": url, "ttl": f"{float(ttl_hours):g}",
    }, title=f"{site} · 邮箱验证")


def reset_html(site, username, url, ttl_hours):
    """找回密码邮件 HTML。

    注意 accent 用琥珀色而非默认蓝：改口令是高影响操作，
    视觉上与「注册验证」区分开，降低用户误点。
    """
    return render_mail("reset.html", {
        "site": site, "username": username or "朋友",
        "reset_url": url, "ttl": f"{float(ttl_hours):g}",
    }, accent="#b45309", title=f"{site} · 重置密码")


def test_html(site, host, port, enc, from_addr):
    """SMTP 配置测试邮件 HTML。"""
    return render_mail("test.html", {
        "site": site, "host": f"{host}:{port}", "enc": enc,
        "from_addr": from_addr, "now": time.strftime("%Y-%m-%d %H:%M:%S"),
    }, title=f"{site} · SMTP 配置测试")


def _connect(c):
    """建立 SMTP 连接（真实握手）。"""
    if c["enc"] == "ssl":
        ctx = ssl.create_default_context()
        s = smtplib.SMTP_SSL(c["host"], c["port"], timeout=25, context=ctx)
    else:
        s = smtplib.SMTP(c["host"], c["port"], timeout=25)
        if c["enc"] == "starttls":
            s.ehlo()
            s.starttls(context=ssl.create_default_context())
        s.ehlo()
    if c["user"]:
        s.login(c["user"], c["password"])
    return s


def send(to_addr, subject, body, html=None):
    """
    真实发信。返回 (ok, message)。
    未配置或未启用时返回 (False, 原因)，调用方据此决定是否落库提示。
    """
    c = _cfg()
    if not to_addr:
        return False, "收件人为空"
    if not c["enabled"]:
        return False, "邮件通知未启用"
    if not c["host"]:
        return False, "SMTP 服务器未配置"
    from_addr = c["from_addr"] or c["user"]
    if not from_addr:
        return False, "发件人地址未配置"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header(c["from_name"], "utf-8")), from_addr))
    msg["To"] = to_addr
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=(from_addr.split("@")[-1] or "kwrt.local"))
    msg.attach(MIMEText(body or "", "plain", "utf-8"))
    if html:
        msg.attach(MIMEText(html, "html", "utf-8"))

    s = None
    try:
        # _connect 内部会把套接字交给临时变量后才 login；若 login 抛异常，
        # 原实现（_connect 在 try 外/内但不持有引用）会让套接字泄漏。
        # 这里先置 s=None，连接成功后立刻接管引用，finally 里兜底关闭。
        s = _connect(c)
        s.sendmail(from_addr, [to_addr], msg.as_string())
        print(f"[mail] 已发送 -> {to_addr} : {subject}", flush=True)
        return True, "已发送"
    except Exception as e:
        print(f"[mail] 发送失败 -> {to_addr}: {type(e).__name__}: {e}", flush=True)
        return False, f"{type(e).__name__}: {e}"
    finally:
        # 无论成功或异常都关闭套接字，避免连接泄漏
        if s is not None:
            try:
                s.quit()
            except Exception:
                try:
                    s.close()
                except Exception:
                    pass


def test(to_addr):
    """管理端「发送测试邮件」——用真实 SMTP 发一封。"""
    c = _cfg()
    site = SS.get("site_name") or "Kwrt"
    subject = f"[{site}] SMTP 配置测试"
    body = (f"这是一封来自 {site} 的测试邮件。\n\n"
            f"收到此邮件说明 SMTP 配置正确，构建完成通知可以正常投递。\n\n"
            f"服务器：{c['host']}:{c['port']}（{c['enc']}）\n"
            f"发件人：{c['from_addr'] or c['user']}\n"
            f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    html = test_html(site, c["host"], c["port"], c["enc"],
                     c["from_addr"] or c["user"])
    return send(to_addr, subject, body, html)


def notify_build_done(user_email, username, req, files, links, duration_sec, hours, ok=True,
                      err=""):
    """
    构建完成通知（真实发送）。
    links: [{name, url, size}]
    """
    if not user_email:
        return False, "用户未填写邮箱"
    c = _cfg()
    site = SS.get("site_name") or "Kwrt"
    target = req.get("target", "-")
    profile = req.get("profile", "-")
    version = req.get("version") or "-"

    if ok:
        lines = []
        for l in links:
            sz = l.get("size") or 0
            sz_txt = f"{sz / 1048576:.1f} MB" if sz else "-"
            lines.append(f"  · {l['name']}  ({sz_txt})\n    {l['url']}")
        ctx = {
            "username": username or "朋友",
            "site": site, "target": target, "profile": profile, "version": version,
            "count": len(links), "duration": f"{int(duration_sec)} 秒" if duration_sec else "-",
            "hours": hours, "links": "\n".join(lines) or "(无产物)",
        }
        subject = render(c["subject"], ctx)
        body = render(c["body"], ctx)
        html = _html_ok(site, ctx, links)
    else:
        if not c["notify_fail"]:
            return False, "构建失败通知已关闭"
        ctx = {"username": username or "朋友", "site": site, "target": target,
               "profile": profile, "version": version, "error": err or "未知错误"}
        subject = f"[{site}] 固件构建失败：{target}"
        body = (f"你好 {ctx['username']}：\n\n"
                f"你定制的 {target} / {profile} 固件构建失败。\n\n"
                f"错误信息：\n{ctx['error']}\n\n"
                f"可回到站点查看完整构建日志。\n")
        html = _html_fail(site, ctx)
    return send(user_email, subject, body, html)


def _html_ok(site, ctx, links):
    """构建成功邮件 HTML（版式见 app/templates/mail/build_ok.html）。"""
    rows = "".join(
        f'<tr>'
        f'<td style="padding:10px 14px;border-bottom:1px solid #e2e8f0">'
        f'<a href="{_e(l["url"])}" style="color:#2563eb;text-decoration:none;font-weight:600">'
        f'{_e(l["name"])}</a></td>'
        f'<td style="padding:10px 14px;border-bottom:1px solid #e2e8f0;color:#64748b;'
        f'text-align:right;white-space:nowrap">{(l.get("size") or 0) / 1048576:.1f} MB</td>'
        f'</tr>'
        for l in links)
    return render_mail("build_ok.html", {**ctx, "site": site, "rows": rows},
                       raw=("rows",))


def _html_fail(site, ctx):
    """构建失败邮件 HTML（版式见 app/templates/mail/build_fail.html）。"""
    return render_mail("build_fail.html", {**ctx, "site": site},
                       accent="#dc2626", title=f"{site} · 构建失败")


def recent(limit=50):
    """最近发信记录（新的在前），读 email_send_log 表。

    ★ 这里原来是读一个进程内的 deque：

          return _sent[-limit:][::-1]

      两个真实问题：
      (1) **deque 不支持切片** —— 这行会抛
          `TypeError: sequence index must be integer, not 'slice'`，
          于是 /api/v1/admin/mail/log 稳定 500（在真机上抓到的，
          本地套件没覆盖这个端点，所以一直没暴露）；
      (2) 内存日志**重启即失**，而 PHP 版读的是 email_send_log 表 ——
          同一个后台页面，两版看到的历史长度不一样。

      现在与 PHP 对齐：读同一张表（verify.log_send 已在写它），
      并**删掉那份重复的内存日志** —— 少一套机制就少一处不一致。
    """
    try:
        n = int(limit)
    except (TypeError, ValueError):
        n = 50
    # 夹紧上限：[-0:] 会退化成「取全部」，是经典的 off-by-one
    n = max(0, min(n, 500))
    if n <= 0:
        return []
    from . import verify
    return verify.send_log(n)


# 异步发送线程池：原实现每次调用都裸起一个 daemon 线程，无上限。
# 构建高峰期（或邮件服务器变慢时）会瞬间堆积大量线程 → 内存与 fd 暴涨。
# 改为固定上限的线程池 + 有界队列，队列满时降级为同步发送（宁可慢，不可崩）。
_MAIL_POOL = None
_MAIL_POOL_LOCK = threading.Lock()
_MAIL_MAX_WORKERS = 4


def _pool():
    global _MAIL_POOL
    if _MAIL_POOL is None:
        with _MAIL_POOL_LOCK:
            if _MAIL_POOL is None:
                import concurrent.futures as _cf
                _MAIL_POOL = _cf.ThreadPoolExecutor(
                    max_workers=_MAIL_MAX_WORKERS, thread_name_prefix="mail")
    return _MAIL_POOL


def async_send(fn, *a, **kw):
    """异步发送，避免阻塞构建队列回调。

    U-7：改用有界线程池。线程池本身用无界队列，但并发上限固定为
    _MAIL_MAX_WORKERS，超出的任务排队而非再开线程 —— 线程数不再随请求量增长。
    """
    try:
        return _pool().submit(fn, *a, **kw)
    except Exception as e:
        # 线程池不可用（极端情况）时降级为同步发送，保证邮件不丢
        print(f"[mail] 线程池提交失败，改为同步发送: {e}", flush=True)
        fn(*a, **kw)
        return None
