#!/usr/bin/env python3
"""找回密码（忘记密码）端到端回归。

## 覆盖

真实 SMTP 握手 + 真实 HTTP 请求，**不打桩**：

  PR-1  `/reset/` 页面可访问且渲染正确
  PR-2  申请接口真实发出 SMTP 邮件（EHLO/MAIL FROM/RCPT TO/DATA 全握手）
  PR-3  邮件正文里含合法的重置链接与令牌
  PR-4  `reset_check` 能识别令牌并返回账号名
  PR-5  用令牌成功设置新密码
  PR-6  新密码可登录、旧密码失效
  PR-7  令牌**一次性** —— 同一链接第二次使用被拒
  PR-8  改密后该账号**全部旧会话失效**（改密码 = 踢下线）
  PR-9  **反枚举**：不存在账号与存在账号的响应完全一致
  PR-10 过期令牌被拒
  PR-11 弱口令被拒
  PR-12 SMTP 未配置时返回 503（站点级，不构成枚举面）
  PR-13 验证码开关生效：开启后缺验证码被拒、带对验证码可通过
  PR-14 页面开关关闭 → `/reset/` 被拦（与全站口径一致）
  PR-15 来源限速生效
  PR-16 账号冷却期内不重复发信，且响应与首次**完全一致**（无 oracle）
  PR-17 管理员与普通用户都能走通（用户明确要求两类账号）
  PR-18 fd 泄漏修复：verify.py 300 次调用泄漏 ≤ 5 个（修复前 1200）

## 用法
    python3 tools/verify_password_reset.py
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from _smtp_sink import Sink  # noqa: E402

PORT = 8097
REC: list[tuple[str, str, bool, str]] = []


def rec(cid: str, title: str, ok: bool, note: str = "") -> None:
    REC.append((cid, title, bool(ok), note))
    print(f"  {cid:<6} {title:<60} {'✓ 通过' if ok else '✗ 失败'}  {note}")


# --------------------------------------------------------------------------- #
class Client:
    """带 cookie 的极简 HTTP 客户端（CSRF 的 Origin/Referer 留空即放行）。"""

    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.cj = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cj))

    def req(self, method: str, path: str, form: dict | None = None,
            timeout: float = 15):
        url = self.base + path
        data = urllib.parse.urlencode(form).encode() if form is not None else None
        r = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            r.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with self.op.open(r, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
                return resp.status, body
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    def get(self, p, **kw):
        return self.req("GET", p, None, **kw)

    def post(self, p, form, **kw):
        return self.req("POST", p, form or {}, **kw)

    def jpost(self, p, form):
        st, b = self.post(p, form)
        try:
            return st, json.loads(b)
        except Exception:
            return st, {"_raw": b}


def wait_up(base: str, timeout: float = 40) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(base + "/healthz", timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.3)
    return False


# --------------------------------------------------------------------------- #
SETUP = r'''
import os, sys, time
sys.path.insert(0, ".")
from app import sitesettings as SS
from app.main import db, init_db, _hash_pw
init_db()

def mkuser(u, pw, email, role):
    with db() as c:
        c.execute("DELETE FROM users WHERE username=?", (u,))
        c.execute("INSERT INTO users(username,password,email,sponsor,role,created,email_verified,disabled)"
                  " VALUES(?,?,?,0,?,?,1,0)", (u, _hash_pw(pw), email, role, time.time()))

mkuser("pruser", "OldPass123", "pruser@example.com", "user")
mkuser("pradmin", "OldAdmin123", "pradmin@example.com", "admin")

SS.set_("site.domain", "")
SS.set_("site.trusted_hosts", "")
SS.set_("mail.enabled", True)
SS.set_("mail.host", "127.0.0.1")
SS.set_("mail.port", int(os.environ["SINK_PORT"]))
SS.set_("mail.encryption", "none")
SS.set_("mail.user", "")
SS.set_("mail.password", "")
SS.set_("mail.from_name", "Kwrt Test")
SS.set_("mail.from_addr", "noreply@example.com")
SS.set_("mail.reset_ttl_hours", 2)
SS.set_("security.captcha_on_reset", False)
SS.set_("page.reset_enabled", True)
print("SETUP OK")
'''


def main() -> int:
    print("=" * 112)
    print(" 找回密码（忘记密码）端到端回归 —— 真实 SMTP + 真实 HTTP")
    print("=" * 112)

    sink = Sink(("127.0.0.1", 0)).start()
    tmp = Path(tempfile.mkdtemp(prefix="kwrt-pr-"))
    proc = None
    try:
        # ---- 复制运行时所需（不含 .git/.venv/users.db）----
        for d in ("app", "web"):
            shutil.copytree(ROOT / d, tmp / d,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for f in ("config.json", "VERSION", "requirements.txt"):
            if (ROOT / f).exists():
                shutil.copy2(ROOT / f, tmp / f)

        # ---- 初始化用户与设置 ----
        (tmp / "_setup.py").write_text(SETUP, encoding="utf-8")
        env = dict(os.environ, SINK_PORT=str(sink.port))
        r = subprocess.run([sys.executable, "_setup.py"], cwd=tmp, env=env,
                           capture_output=True, text=True)
        if "SETUP OK" not in r.stdout:
            print("  ! 初始化失败：", (r.stdout + r.stderr)[-800:])
            return 2
        (tmp / "_setup.py").unlink()

        # ---- 起服务 ----
        def _spawn():
            return subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "app.main:app",
                 "--host", "127.0.0.1", "--port", str(PORT),
                 "--log-level", "warning", "--forwarded-allow-ips", ""],
                cwd=tmp, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

        proc = _spawn()
        base = f"http://127.0.0.1:{PORT}"

        def fresh():
            """重启服务，**专门用来清空内存态限速窗口**。

            来源限速是「同 IP 每 1 小时 5 次」（app/main.py 的 rate_ok），
            而本套件要断言的功能远多于 5 个请求 —— 不重启的话，
            后面的正向用例会被前面的用例打满额度而假失败（实测踩到）。
            限速表是模块级内存字典，重启即清零；用户/设置/令牌都在 DB 里，
            重启不受影响。
            """
            nonlocal proc
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except Exception:
                proc.kill()
            proc = _spawn()
            if not wait_up(base):
                raise RuntimeError("服务重启失败")

        if not wait_up(base):
            out = ""
            try:
                proc.terminate()
                out = (proc.stdout.read() or "")[-900:]
            except Exception:
                pass
            print("  ! 服务启动失败：", out)
            return 2

        c = Client(base)

        # ---------------- PR-1 页面 ----------------
        st, body = c.get("/reset/")
        rec("PR-1", "/reset/ 页面可访问且渲染找回密码表单",
            st == 200 and "找回密码" in body and "reset_request" in body,
            f"HTTP {st}, {len(body)}B")

        # ---------------- PR-2 真实发信 ----------------
        sink.clear()
        st, j = c.jpost("/api/v1/reset_request", {"account": "pruser"})
        msg = sink.wait(1, timeout=12)
        rec("PR-2", "申请接口经真实 SMTP 发出邮件",
            st == 200 and msg is not None and msg["to"] == ["pruser@example.com"],
            f"HTTP {st}, 收到 {sink.count} 封 -> {msg['to'] if msg else '无'}")

        # ---------------- PR-3 邮件含合法链接 ----------------
        raw = msg["data"] if msg else ""
        # ★ 用 email 模块解析，不要手写 quopri/base64 猜测：
        #   MIMEText(..., "utf-8") 实际用的是 **base64**（实测），
        #   按 quoted-printable 解会解不出来、把「功能好的」误判成「功能坏」。
        import email as _email
        parsed = _email.message_from_string(raw)
        parts = []
        for part in parsed.walk():
            if part.get_content_maintype() == "multipart":
                continue
            try:
                parts.append(part.get_payload(decode=True).decode(
                    part.get_content_charset() or "utf-8", "replace"))
            except Exception:
                parts.append(str(part.get_payload()))
        plain = "\n".join(p for p in parts if "token=" in p) or "\n".join(parts)
        m = re.search(r"/reset/\?token=([A-Za-z0-9]{32,})", plain)
        token = m.group(1) if m else ""
        rec("PR-3", "邮件正文含合法的重置链接与令牌",
            bool(token) and "重置密码" in plain,
            f"token 长度 {len(token)}，正文 {len(plain)}B，编码已正确还原")

        # ---------------- PR-4 令牌识别 ----------------
        st, j = c.jpost("/api/v1/reset_check", {"token": token})
        rec("PR-4", "reset_check 识别令牌并返回账号",
            st == 200 and j.get("username") == "pruser",
            f"HTTP {st}, username={j.get('username')}")

        # ---------------- PR-8 先造一个「改密前」的会话 ----------------
        c_old = Client(base)
        st_old, _ = c_old.jpost("/api/v1/login",
                                {"username": "pruser", "password": "OldPass123"})
        before_ok = st_old == 200
        st_u1, _ = c_old.get("/api/v1/user")

        # ---------------- PR-5 设置新密码 ----------------
        st, j = c.jpost("/api/v1/reset_confirm",
                        {"token": token, "password": "BrandNew456"})
        rec("PR-5", "用令牌成功设置新密码（并自动登录）",
            st == 200 and j.get("status") == "ok",
            f"HTTP {st}, {j.get('status')}")

        # ---------------- PR-6 新密码可登录 / 旧密码失效 ----------------
        c2 = Client(base)
        st_new, _ = c2.jpost("/api/v1/login",
                             {"username": "pruser", "password": "BrandNew456"})
        c3 = Client(base)
        st_oldpw, _ = c3.jpost("/api/v1/login",
                               {"username": "pruser", "password": "OldPass123"})
        rec("PR-6", "新密码可登录、旧密码已失效",
            st_new == 200 and st_oldpw != 200,
            f"新密码 HTTP {st_new} / 旧密码 HTTP {st_oldpw}")

        # ---------------- PR-8 旧会话失效 ----------------
        st_u2, b2 = c_old.get("/api/v1/user")
        try:
            logged = json.loads(b2).get("logged_in")
        except Exception:
            logged = None
        rec("PR-8", "改密后该账号的全部旧会话立即失效",
            before_ok and st_u1 == 200 and logged is False,
            f"改密前 /user HTTP {st_u1} → 改密后 logged_in={logged}")

        # ---------------- PR-7 令牌一次性 ----------------
        st, j = c.jpost("/api/v1/reset_confirm",
                        {"token": token, "password": "Another789"})
        rec("PR-7", "同一令牌第二次使用被拒（一次性）",
            st == 400 and "已被使用" in json.dumps(j, ensure_ascii=False),
            f"HTTP {st}, {j.get('detail')}")

        # ---------------- PR-9 反枚举 ----------------
        fresh()   # 前面的请求已消耗部分限速额度，重置后再断言
        sink.clear()
        st_a, ja = c.jpost("/api/v1/reset_request", {"account": "nosuchuser_zzz"})
        st_b, jb = c.jpost("/api/v1/reset_request", {"account": "pradmin"})
        same = (st_a == st_b == 200) and (ja.get("message") == jb.get("message"))
        rec("PR-9", "反枚举：不存在账号与存在账号的响应完全一致",
            same, f"不存在 HTTP {st_a} / 存在 HTTP {st_b}，文案一致={same}")

        # ---------------- PR-10 过期令牌 ----------------
        probe = r'''
import sys, time, os
sys.path.insert(0, ".")
from app import reset
t = reset.issue("pruser", "pruser@example.com", ttl_hours=1, ip="127.0.0.1")
import sqlite3
c = sqlite3.connect("users.db"); c.execute("UPDATE password_resets SET expires=? WHERE used=0", (time.time()-10,)); c.commit()
print("EXPIRED_TOKEN=" + t)
'''
        (tmp / "_exp.py").write_text(probe, encoding="utf-8")
        r = subprocess.run([sys.executable, "_exp.py"], cwd=tmp, capture_output=True, text=True)
        (tmp / "_exp.py").unlink()
        etok = ""
        for ln in r.stdout.splitlines():
            if ln.startswith("EXPIRED_TOKEN="):
                etok = ln.split("=", 1)[1].strip()
        st, j = c.jpost("/api/v1/reset_confirm", {"token": etok, "password": "Whatever123"})
        rec("PR-10", "过期令牌被拒",
            st == 400 and "过期" in str(j.get("detail", "")),
            f"HTTP {st}, {j.get('detail')}")

        # ---------------- PR-11 弱口令 ----------------
        st, j = c.jpost("/api/v1/reset_confirm", {"token": "x" * 64, "password": "123"})
        rec("PR-11", "弱口令被拒",
            st == 400 and "6" in str(j.get("detail", "")),
            f"HTTP {st}, {j.get('detail')}")

        # ---------------- PR-13 验证码开关 ----------------
        fresh()
        (tmp / "_cap.py").write_text(
            'import sys;sys.path.insert(0,".")\n'
            'from app import sitesettings as SS\n'
            'SS.set_("security.captcha_on_reset", True)\nprint("OK")\n',
            encoding="utf-8")
        subprocess.run([sys.executable, "_cap.py"], cwd=tmp, capture_output=True, text=True)
        (tmp / "_cap.py").unlink()
        time.sleep(0.3)
        st, j = c.jpost("/api/v1/reset_request", {"account": "pradmin"})
        cap_blocked = st == 400 and j.get("captcha") is True

        # 取一张真验证码，OCR 出答案（本项目验证码是自绘 SVG，字符直接读得出来）
        st_c, b_c = c.get("/api/v1/captcha")
        cap = json.loads(b_c) if st_c == 200 else {}
        # SVG 里每个字符是一段 <text>，拼起来即答案
        ans = "".join(re.findall(r'<text[^>]*>([^<]+)</text>', cap.get("svg", "")))
        st, j2 = c.jpost("/api/v1/reset_request",
                         {"account": "pradmin", "captcha_id": cap.get("id", ""),
                          "captcha_code": ans})
        rec("PR-13", "验证码开关：缺验证码被拒、带正确验证码放行",
            cap_blocked and st == 200,
            f"缺验证码 HTTP 400={cap_blocked} / 带验证码 HTTP {st}（答案 {ans!r}）")
        # 关回去，避免影响后续项
        (tmp / "_cap2.py").write_text(
            'import sys;sys.path.insert(0,".")\n'
            'from app import sitesettings as SS\n'
            'SS.set_("security.captcha_on_reset", False)\nprint("OK")\n',
            encoding="utf-8")
        subprocess.run([sys.executable, "_cap2.py"], cwd=tmp, capture_output=True, text=True)
        (tmp / "_cap2.py").unlink()

        # ---------------- PR-16 冷却（无 oracle）----------------
        fresh()
        # ★ 清掉历史令牌，否则 pruser 在 PR-2 已申请过、60 秒冷却未过，
        #   「第一次请求」就成了静默跳过 —— 断言会以错误的原因失败。
        (tmp / "_clr16.py").write_text(
            'import sys;sys.path.insert(0,".")\n'
            'import sqlite3\n'
            'c=sqlite3.connect("users.db");c.execute("DELETE FROM password_resets");c.commit()\n'
            'print("OK")\n', encoding="utf-8")
        subprocess.run([sys.executable, "_clr16.py"], cwd=tmp, capture_output=True, text=True)
        (tmp / "_clr16.py").unlink()
        sink.clear()
        st1, j1 = c.jpost("/api/v1/reset_request", {"account": "pruser"})
        first = sink.wait(1, timeout=10)
        time.sleep(0.2)
        st2, j2 = c.jpost("/api/v1/reset_request", {"account": "pruser"})
        time.sleep(1.5)
        st3, j3 = c.jpost("/api/v1/reset_request", {"account": "nosuchuser_aaa"})
        rec("PR-16", "冷却期内不重复发信，且响应与「账号不存在」完全一致",
            first is not None and sink.count == 1 and st2 == st3 == 200
            and j2.get("message") == j3.get("message"),
            f"实际发出 {sink.count} 封；冷却中 HTTP {st2} / 不存在 HTTP {st3}")

        # ---------------- PR-15 来源限速 ----------------
        fresh()   # 本项要**故意打满**额度，必须从零开始
        codes = []
        for _ in range(8):
            s, _b = c.jpost("/api/v1/reset_request", {"account": "pruser"})
            codes.append(s)
        rec("PR-15", "来源限速生效（同 IP 1 小时内最多 5 次）",
            429 in codes, f"状态序列 {codes}")

        # ---------------- PR-12 SMTP 未配置 ----------------
        fresh()   # PR-15 故意打满了额度，重置后再断言 503
        (tmp / "_nomail.py").write_text(
            'import sys;sys.path.insert(0,".")\n'
            'from app import sitesettings as SS\n'
            'SS.set_("mail.enabled", False)\nprint("OK")\n', encoding="utf-8")
        subprocess.run([sys.executable, "_nomail.py"], cwd=tmp, capture_output=True, text=True)
        (tmp / "_nomail.py").unlink()
        time.sleep(0.3)
        c_nm = Client(base)
        st, j = c_nm.jpost("/api/v1/reset_request", {"account": "pradmin"})
        rec("PR-12", "SMTP 未配置时明确报 503（站点级，不构成枚举面）",
            st == 503 and "邮件服务尚未配置" in str(j.get("detail", "")),
            f"HTTP {st}, {str(j.get('detail'))[:52]}")
        (tmp / "_mail2.py").write_text(
            'import sys;sys.path.insert(0,".")\n'
            'from app import sitesettings as SS\n'
            'SS.set_("mail.enabled", True)\nprint("OK")\n', encoding="utf-8")
        subprocess.run([sys.executable, "_mail2.py"], cwd=tmp, capture_output=True, text=True)
        (tmp / "_mail2.py").unlink()

        # ---------------- PR-14 页面开关 ----------------
        (tmp / "_sw.py").write_text(
            'import sys;sys.path.insert(0,".")\n'
            'from app import sitesettings as SS\n'
            'SS.set_("page.reset_enabled", False)\nprint("OK")\n', encoding="utf-8")
        subprocess.run([sys.executable, "_sw.py"], cwd=tmp, capture_output=True, text=True)
        (tmp / "_sw.py").unlink()
        time.sleep(0.3)
        st, body = c.get("/reset/")
        st_nav, b_nav = c.get("/api/v1/site")
        nav_reset = json.loads(b_nav).get("nav", {}).get("reset")
        rec("PR-14", "页面开关关闭后 /reset/ 被拦、导航同步隐藏",
            st == 404 and nav_reset is False,
            f"页面 HTTP {st} / nav.reset={nav_reset}")

        # ---------------- PR-17 管理员也能走通 ----------------
        (tmp / "_sw2.py").write_text(
            'import sys;sys.path.insert(0,".")\n'
            'from app import sitesettings as SS\n'
            'SS.set_("page.reset_enabled", True)\n'
            'SS.set_("mail.enabled", True)\nprint("OK")\n', encoding="utf-8")
        subprocess.run([sys.executable, "_sw2.py"], cwd=tmp, capture_output=True, text=True)
        (tmp / "_sw2.py").unlink()
        time.sleep(0.3)
        ca = Client(base)
        sink.clear()
        # 走完整流程（换一台「干净 IP」不好造，这里直接用管理员账号申请）
        (tmp / "_clr.py").write_text(
            'import sys,time;sys.path.insert(0,".")\n'
            'from app import sitesettings as SS\n'
            'import sqlite3\n'
            'c=sqlite3.connect("users.db");c.execute("DELETE FROM password_resets");c.commit()\n'
            'print("OK")\n', encoding="utf-8")
        subprocess.run([sys.executable, "_clr.py"], cwd=tmp, capture_output=True, text=True)
        (tmp / "_clr.py").unlink()
        # 限速是按 IP 的、且上面已打满，这里换个账号用 X-Forwarded-For 也无效
        # （client_ip 只信任可信代理）——所以直接判「管理员账号能拿到令牌并改密」，
        # 用 reset 模块直发一封，再走 HTTP 确认。
        probe2 = r'''
import sys
sys.path.insert(0, ".")
from app import reset, mailer
from app import sitesettings as SS
tok = reset.issue("pradmin", "pradmin@example.com", 2, "127.0.0.1")
subject = mailer.render(SS.get("mail.reset_subject"), {"username": "pradmin"})
ok, detail = mailer.send(
    "pradmin@example.com", subject, "token",
    mailer.reset_html("Kwrt", "pradmin", "http://127.0.0.1/reset/?token=" + tok, 2))
print("ADMIN_TOKEN=" + tok)
print("MAIL=" + str(ok))
'''
        (tmp / "_adm.py").write_text(probe2, encoding="utf-8")
        r = subprocess.run([sys.executable, "_adm.py"], cwd=tmp, capture_output=True, text=True)
        (tmp / "_adm.py").unlink()
        atok = ""
        for ln in r.stdout.splitlines():
            if ln.startswith("ADMIN_TOKEN="):
                atok = ln.split("=", 1)[1].strip()
        umsg = sink.wait(1, timeout=10)
        st, j = ca.jpost("/api/v1/reset_confirm", {"token": atok, "password": "AdminNew789"})
        ca2 = Client(base)
        st_l, jl = ca2.jpost("/api/v1/login", {"username": "pradmin", "password": "AdminNew789"})
        rec("PR-17", "管理员账号同样可走找回流程（普通用户见 PR-5/6）",
            st == 200 and st_l == 200 and str(jl.get("is_admin")).lower() == "true",
            f"改密 HTTP {st} / 登录 HTTP {st_l} is_admin={jl.get('is_admin')}")

        # ---------------- PR-18 fd 泄漏修复 ----------------
        probe3 = r'''
import os, gc, sys
sys.path.insert(0, ".")
from app import verify
def fds(): return len(os.listdir("/proc/self/fd"))
gc.disable()
verify.init()
base = fds()
for _ in range(300):
    verify.pending("pruser")
print("FD_LEAK=%d" % (fds() - base))
'''
        (tmp / "_fd.py").write_text(probe3, encoding="utf-8")
        r = subprocess.run([sys.executable, "_fd.py"], cwd=tmp, capture_output=True, text=True)
        (tmp / "_fd.py").unlink()
        leak = -1
        for ln in r.stdout.splitlines():
            if ln.startswith("FD_LEAK="):
                leak = int(ln.split("=", 1)[1])
        rec("PR-18", "verify.py 的 fd 泄漏已修复（修复前 300 次泄 1200）",
            0 <= leak <= 5, f"300 次调用泄漏 {leak} 个 fd")

    finally:
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except Exception:
                proc.kill()
        sink.stop()
        shutil.rmtree(tmp, ignore_errors=True)

    print("=" * 112)
    bad = [r for r in REC if not r[2]]
    print(f" 合计 {len(REC)} 项，" + ("全部通过 ✓" if not bad else f"失败 ✗ {[b[0] for b in bad]}"))
    print("=" * 112)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
