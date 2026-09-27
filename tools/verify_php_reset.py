#!/usr/bin/env python3
"""PHP 版找回密码回归 —— 含**跨版本令牌互认**。

## 为什么必须测跨版本

PHP 版与 Python 版必须能操作**同一个 users.db**（这是本项目一贯的契约）。
找回密码尤其不能各写一套：如果 PHP 签发的令牌 Python 认不出来，
用户就会遇到「用 PHP 站申请、切到 Python 站说链接无效」这种最难查的故障。
所以本套件把两个版本同时跑起来、共用一个库，双向互认各测一遍。

## 断言

  PHPR-1  /reset/ 页面可访问
  PHPR-2  /api/v1/site 暴露 captcha.reset 与 nav.reset
  PHPR-3  默认要求验证码（security.captcha_on_reset 默认开）
  PHPR-4  带正确验证码后经**真实 SMTP** 发出邮件，正文含链接与令牌
  PHPR-5  reset_check 识别令牌
  PHPR-6  reset_confirm 改密成功；新密码可登录、旧密码失效
  PHPR-7  令牌一次性
  PHPR-8  改密后该账号**全部旧会话失效**
  PHPR-9  反枚举：不存在账号与存在账号响应一致
  PHPR-10 **Python 签发 -> PHP 消费**（跨版本互认 B 向）
  PHPR-11 **PHP 签发 -> Python 消费**（跨版本互认 A 向）
  PHPR-12 弱口令被拒
  PHPR-13 页面开关关闭后 /reset/ 被拦
  PHPR-14 与 Python 版对同一令牌的 check 结果一致（同构性直接断言）

## 用法
    python3 tools/verify_php_reset.py
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

PORT_PHP = int(os.environ.get("KWRT_PHPR_PHP_PORT", "8211"))
PORT_PY = int(os.environ.get("KWRT_PHPR_PY_PORT", "8212"))
REC: list[tuple[str, str, bool, str]] = []


def rec(cid: str, title: str, ok: bool, note: str = "") -> None:
    REC.append((cid, title, bool(ok), note))
    print(f"  {cid:<7} {title:<54} {'✓ 通过' if ok else '✗ 失败'}  {note}")


class Client:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.cj = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cj))

    def req(self, method, path, form=None, timeout=20):
        data = urllib.parse.urlencode(form).encode() if form is not None else None
        r = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            r.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with self.op.open(r, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    def get(self, p):
        return self.req("GET", p)

    def post(self, p, form):
        return self.req("POST", p, form or {})

    def jpost(self, p, form):
        st, b = self.post(p, form)
        try:
            return st, json.loads(b)
        except Exception:
            return st, {"_raw": b[:200]}


def wait_up(base: str, path="/healthz", timeout=40) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(base + path, timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.25)
    return False


def captcha(c: Client):
    st, b = c.get("/api/v1/captcha")
    try:
        d = json.loads(b)
    except Exception:
        return "", "", st
    ans = "".join(re.findall(r"<text[^>]*>([^<]+)</text>", d.get("svg", "")))
    return d.get("id", ""), ans, st


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
mkuser("phpr_user",  "PhpOld123",  "phpr_user@example.com",  "user")
mkuser("phpr_admin", "PhpAdmOld1", "phpr_admin@example.com", "admin")
# 跨版本互认需要独立账号：每人同时只保留一条有效令牌，
# 用同一个账号会让两个方向的测试互相作废（第一次踩到过）。
mkuser("phpr_x",     "PhpXOld123", "phpr_x@example.com",     "user")
SS.set_("site.domain", "")
SS.set_("site.trusted_hosts", "")
SS.set_("site.allow_any_host", True)
SS.set_("mail.enabled", True)
SS.set_("mail.host", "127.0.0.1")
SS.set_("mail.port", int(os.environ["SINK_PORT"]))
SS.set_("mail.encryption", "none")
SS.set_("mail.user", "")
SS.set_("mail.password", "")
SS.set_("mail.from_name", "Kwrt Test")
SS.set_("mail.from_addr", "noreply@example.com")
SS.set_("mail.reset_ttl_hours", 2)
SS.set_("security.captcha_on_reset", True)
SS.set_("page.reset_enabled", True)
print("SETUP OK")
'''


def main() -> int:
    print("=" * 112)
    print(" PHP 版找回密码回归（PHP + Python 同库并行，含跨版本令牌互认）")
    print("=" * 112)

    sink = Sink(("127.0.0.1", 0)).start()
    tmp = Path(tempfile.mkdtemp(prefix="kwrt-phpreset-"))
    php = pyproc = None
    try:
        for d in ("app", "php", "web"):
            shutil.copytree(ROOT / d, tmp / d,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for f in ("config.json", "VERSION"):
            if (ROOT / f).exists():
                shutil.copy2(ROOT / f, tmp / f)

        (tmp / "_setup.py").write_text(SETUP, encoding="utf-8")
        r = subprocess.run([sys.executable, "_setup.py"], cwd=tmp,
                           env=dict(os.environ, SINK_PORT=str(sink.port)),
                           capture_output=True, text=True)
        if "SETUP OK" not in r.stdout:
            print("  ! 初始化失败：", (r.stdout + r.stderr)[-800:])
            return 2
        (tmp / "_setup.py").unlink()

        php_base = f"http://127.0.0.1:{PORT_PHP}"
        py_base = f"http://127.0.0.1:{PORT_PY}"

        php = subprocess.Popen(
            ["php", "-S", f"127.0.0.1:{PORT_PHP}", "-t", "php/public",
             "php/public/router.php"],
            cwd=tmp, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            preexec_fn=os.setsid)
        pyproc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
             "--port", str(PORT_PY), "--log-level", "warning", "--forwarded-allow-ips", ""],
            cwd=tmp, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            preexec_fn=os.setsid)

        if not wait_up(php_base, "/"):
            print("  ! PHP 未起来：", (php.stdout.read() or "")[-700:] if php.stdout else "")
            return 2
        if not wait_up(py_base):
            print("  ! Python 未起来：", (pyproc.stdout.read() or "")[-700:] if pyproc.stdout else "")
            return 2

        c = Client(php_base)          # PHP 站点
        cp = Client(py_base)          # Python 站点

        # ---------------- PHPR-1 页面 ----------------
        # 注意：PHP 版是服务端渲染，页面里**不会有** reset_request 这种 JS 字面量，
        # 断言要落在真实的 DOM 标记上（表单 id / 文案），否则会以错误的原因失败。
        st, body = c.get("/reset/")
        rec("PHPR-1", "PHP /reset/ 页面可访问且含表单",
            st == 200 and "找回密码" in body and 'id="resetForm"' in body,
            f"HTTP {st}, {len(body)}B")

        # ---------------- PHPR-2 站点信息 ----------------
        st, b = c.get("/api/v1/site")
        try:
            d = json.loads(b)
        except Exception:
            d = {}
        rec("PHPR-2", "/api/v1/site 暴露 captcha.reset 与 nav.reset",
            st == 200 and isinstance(d.get("captcha", {}).get("reset"), bool)
            and isinstance(d.get("nav", {}).get("reset"), bool),
            f"captcha.reset={d.get('captcha', {}).get('reset')} nav.reset={d.get('nav', {}).get('reset')}")

        # ---------------- PHPR-3 验证码默认开 ----------------
        sink.clear()
        st, j = c.jpost("/api/v1/reset_request", {"account": "phpr_user"})
        rec("PHPR-3", "默认要求验证码（captcha_on_reset 默认开）",
            st == 400 and j.get("captcha") is True and sink.count == 0,
            f"HTTP {st}, 未发信={sink.count == 0}")

        # ---------------- PHPR-4 真实发信 ----------------
        cid, ans, stc = captcha(c)
        st, j = c.jpost("/api/v1/reset_request",
                        {"account": "phpr_user", "captcha_id": cid, "captcha_code": ans})
        msg = sink.wait(1, timeout=12)
        raw = msg["data"] if msg else ""
        import email as _email
        parsed = _email.message_from_string(raw)
        parts = []
        for p in parsed.walk():
            if p.get_content_maintype() == "multipart":
                continue
            try:
                parts.append(p.get_payload(decode=True).decode(
                    p.get_content_charset() or "utf-8", "replace"))
            except Exception:
                parts.append(str(p.get_payload()))
        plain = "\n".join(parts)
        m = re.search(r"/reset/\?token=([A-Za-z0-9]{32,})", plain)
        tok_php = m.group(1) if m else ""
        rec("PHPR-4", "带正确验证码后经真实 SMTP 发信，正文含令牌",
            st == 200 and msg is not None and bool(tok_php)
            and msg["to"] == ["phpr_user@example.com"],
            f"HTTP {st}, 收信 {sink.count} 封, token 长度 {len(tok_php)}")

        # ---------------- PHPR-5 reset_check ----------------
        st, j = c.jpost("/api/v1/reset_check", {"token": tok_php})
        rec("PHPR-5", "PHP reset_check 识别令牌",
            st == 200 and j.get("username") == "phpr_user",
            f"HTTP {st}, username={j.get('username')}")

        # ---------------- PHPR-14 两版 check 结果一致 ----------------
        st_py, j_py = cp.jpost("/api/v1/reset_check", {"token": tok_php})
        rec("PHPR-14", "两版对同一令牌的 check 结果一致（同构性直接断言）",
            st_py == 200 and j_py.get("username") == j.get("username"),
            f"PHP={j.get('username')} Python={j_py.get('username')}")

        # ---------------- PHPR-10 Python 签发 -> PHP 消费 ----------------
        # 方向 B：Python 侧申请（令牌存进同一个 users.db），PHP 侧消费。
        sink.clear()
        cid2, ans2, _sc = captcha(cp)
        st_pyreq, j_pyreq = cp.jpost("/api/v1/reset_request",
                                     {"account": "phpr_admin", "captcha_id": cid2,
                                      "captcha_code": ans2})
        msg2 = sink.wait(1, timeout=12)
        raw2 = msg2["data"] if msg2 else ""
        p2 = _email.message_from_string(raw2)
        txt2 = "\n".join(
            (q.get_payload(decode=True) or b"").decode(q.get_content_charset() or "utf-8", "replace")
            for q in p2.walk() if q.get_content_maintype() != "multipart")
        m2 = re.search(r"/reset/\?token=([A-Za-z0-9]{32,})", txt2)
        tok_py = m2.group(1) if m2 else ""
        st_chk, j_chk = c.jpost("/api/v1/reset_check", {"token": tok_py})
        # 不只「识别」，还要真的消费掉，并确认新口令在 Python 站生效 ——
        # 只 check 不 consume 会漏掉「消费时的方言差异」（例如 rowCount 语义）
        st_cons, j_cons = c.jpost("/api/v1/reset_confirm",
                                  {"token": tok_py, "password": "PyToPhp9876"})
        c_pylogin = Client(py_base)
        st_pl2, _ = c_pylogin.jpost("/api/v1/login",
                                    {"username": "phpr_admin", "password": "PyToPhp9876"})
        rec("PHPR-10", "Python 签发的令牌 PHP 能识别并消费，新口令在 Python 站可登录（互认 B 向）",
            bool(tok_py) and st_chk == 200 and j_chk.get("username") == "phpr_admin"
            and st_cons == 200 and st_pl2 == 200,
            f"PHP check {st_chk} -> {j_chk.get('username')}, PHP 改密 {st_cons}, Python 登录 {st_pl2}")

        # ---------------- PHPR-11 PHP 签发 -> Python 消费 ----------------
        # 方向 A：PHP 侧申请，Python 侧消费，再用 PHP 站登录验证口令真的变了。
        sink.clear()
        cid3, ans3, _ = captcha(c)
        st_phpreq, _j = c.jpost("/api/v1/reset_request",
                                {"account": "phpr_x", "captcha_id": cid3, "captcha_code": ans3})
        msg3 = sink.wait(1, timeout=12)
        raw3 = msg3["data"] if msg3 else ""
        p3 = _email.message_from_string(raw3)
        txt3 = "\n".join(
            (q.get_payload(decode=True) or b"").decode(q.get_content_charset() or "utf-8", "replace")
            for q in p3.walk() if q.get_content_maintype() != "multipart")
        m3 = re.search(r"/reset/\?token=([A-Za-z0-9]{32,})", txt3)
        tok_php2 = m3.group(1) if m3 else ""
        st_pyc, j_pyc = cp.jpost("/api/v1/reset_confirm",
                                 {"token": tok_php2, "password": "CrossNew12345"})
        c_cross = Client(php_base)
        st_cross, _ = c_cross.jpost("/api/v1/login",
                                    {"username": "phpr_x", "password": "CrossNew12345"})
        rec("PHPR-11", "PHP 签发的令牌 Python 能消费，且新口令在 PHP 站可登录（互认 A 向）",
            bool(tok_php2) and st_pyc == 200 and st_cross == 200,
            f"token 长度 {len(tok_php2)}, Python 改密 HTTP {st_pyc}, PHP 登录 HTTP {st_cross}")

        # ---------------- PHPR-6 PHP 消费 PHP 令牌改密 ----------------
        # 先把一个「改密前」的会话造出来，供 PHPR-8 断言「旧会话失效」
        c_old = Client(php_base)
        st_old, _ = c_old.jpost("/api/v1/login",
                                {"username": "phpr_user", "password": "PhpOld123"})
        st_old_u, _ = c_old.get("/api/v1/user")

        st, j = c.jpost("/api/v1/reset_confirm",
                        {"token": tok_php, "password": "PhpBrandNew9"})
        c_new = Client(php_base)
        st_l, _ = c_new.jpost("/api/v1/login",
                              {"username": "phpr_user", "password": "PhpBrandNew9"})
        c_oldpw = Client(php_base)
        st_o, _ = c_oldpw.jpost("/api/v1/login",
                                {"username": "phpr_user", "password": "PhpOld123"})
        rec("PHPR-6", "PHP reset_confirm 改密成功；新密码可登录、旧密码失效",
            st == 200 and st_l == 200 and st_o != 200,
            f"改密 HTTP {st} / 新密码 {st_l} / 旧密码 {st_o}")

        # ---------------- PHPR-8 旧会话失效 ----------------
        # ★ 两版对「未登录」的表达**不同**（既有差异，非本次引入）：
        #     Python: 200 + {"logged_in": false}
        #     PHP   : 401 + {"status":"error","detail":"未登录"}
        #   两者语义一致（会话已死），两版前端也都按各自方式处理（catch/判空）。
        #   这里按语义断言，而不是按某一版的字面量 —— 否则会因为
        #   「另一版没照抄这个细节」而误报失败。
        st_u, b_u = c_old.get("/api/v1/user")
        if st_u == 200:
            try:
                logged = json.loads(b_u).get("logged_in")
            except Exception:
                logged = None
            dead = logged is False
        else:
            dead = st_u == 401        # PHP 口径：401 = 未登录 = 会话已失效
        rec("PHPR-8", "改密后该账号全部旧会话立即失效",
            st_old == 200 and st_old_u == 200 and dead,
            f"改密前 {st_old_u} -> 改密后 HTTP {st_u}（PHP 口径 401=已失效）")

        # ---------------- PHPR-7 令牌一次性 ----------------
        st, j = c.jpost("/api/v1/reset_confirm",
                        {"token": tok_php, "password": "PhpAnother11"})
        rec("PHPR-7", "同一令牌第二次使用被拒（一次性）",
            st == 400 and "已被使用" in json.dumps(j, ensure_ascii=False),
            f"HTTP {st}, {j.get('detail')}")

        # ---------------- PHPR-9 反枚举 ----------------
        cid3, ans3, _ = captcha(c)
        st_a, ja = c.jpost("/api/v1/reset_request",
                           {"account": "no_such_zzz", "captcha_id": cid3, "captcha_code": ans3})
        cid4, ans4, _ = captcha(c)
        st_b, jb = c.jpost("/api/v1/reset_request",
                           {"account": "phpr_user", "captcha_id": cid4, "captcha_code": ans4})
        rec("PHPR-9", "反枚举：不存在账号与存在账号响应一致",
            st_a == st_b == 200 and ja.get("message") == jb.get("message"),
            f"不存在 HTTP {st_a} / 存在 HTTP {st_b}，文案一致="
            f"{ja.get('message') == jb.get('message')}")

        # ---------------- PHPR-12 弱口令 ----------------
        st, j = c.jpost("/api/v1/reset_confirm", {"token": "z" * 64, "password": "123"})
        rec("PHPR-12", "弱口令被拒",
            st == 400 and "6" in str(j.get("detail", "")), f"HTTP {st}, {j.get('detail')}")

        # ---------------- PHPR-13 页面开关 ----------------
        (tmp / "_sw.py").write_text(
            'import sys;sys.path.insert(0,".")\n'
            'from app import sitesettings as SS\n'
            'SS.set_("page.reset_enabled", False)\nprint("OK")\n', encoding="utf-8")
        subprocess.run([sys.executable, "_sw.py"], cwd=tmp, capture_output=True, text=True)
        (tmp / "_sw.py").unlink()
        time.sleep(0.2)
        st_page, _ = c.get("/reset/")
        st_site, b_site = c.get("/api/v1/site")
        try:
            nav_reset = json.loads(b_site).get("nav", {}).get("reset")
        except Exception:
            nav_reset = None
        rec("PHPR-13", "页面开关关闭后 PHP /reset/ 被拦、导航同步隐藏",
            st_page == 404 and nav_reset is False,
            f"页面 HTTP {st_page} / nav.reset={nav_reset}")

    finally:
        for p in (php, pyproc):
            if p:
                try:
                    os.killpg(os.getpgid(p.pid), 15)
                except Exception:
                    try:
                        p.terminate()
                    except Exception:
                        pass
        sink.stop()
        shutil.rmtree(tmp, ignore_errors=True)

    print("=" * 112)
    bad = [r for r in REC if not r[2]]
    print(f" 合计 {len(REC)} 项，" + ("全部通过 ✓" if not bad else f"失败 ✗ {[b[0] for b in bad]}"))
    print("=" * 112)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
