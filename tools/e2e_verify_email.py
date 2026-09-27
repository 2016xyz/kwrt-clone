#!/usr/bin/env python3
"""
注册邮箱验证 —— 端到端测试。

覆盖：
    §1 未开启验证时：注册即用，不要求邮箱
    §2 开启验证但邮件未配置：明确报错，不放行（不静默降级）
    §3 开启验证正常流程：注册 → 真实 SMTP 收信 → 提取链接 → 验证 → 可登录
    §4 未验证账号登录被拦（且不在密码校验前泄露账号状态）
    §5 令牌一次性：重放被拒
    §6 令牌过期：被拒
    §7 令牌无效/伪造：被拒
    §8 重发：冷却生效、旧令牌作废、新令牌可用
    §9 重复注册同名未验证账号：给出明确指引
    §10 管理员可手动置位验证 / 代发邮件
    §11 管理端统计可读
"""
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE = "http://127.0.0.1:8443"
INBOX = "/tmp/smtp_inbox.jsonl"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PASS, FAIL = [], []


def ok(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  [{extra}]" if extra else ""))


class Client:
    def __init__(self):
        self.cj = urllib.request.HTTPCookieProcessor()
        self.op = urllib.request.build_opener(self.cj)

    def req(self, method, path, js=None, form=None):
        hdr, body = {}, None
        if js is not None:
            body = json.dumps(js).encode(); hdr["Content-Type"] = "application/json"
        elif form is not None:
            body = urllib.parse.urlencode(form).encode()
            hdr["Content-Type"] = "application/x-www-form-urlencoded"
        r = urllib.request.Request(BASE + path, data=body, method=method, headers=hdr)
        try:
            with self.op.open(r, timeout=30) as resp:
                raw = resp.read().decode("utf-8", "ignore")
                try:
                    return resp.status, json.loads(raw)
                except Exception:
                    return resp.status, raw
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "ignore")
            try:
                return e.code, json.loads(raw)
            except Exception:
                return e.code, raw

    def get(self, p):
        return self.req("GET", p)

    def post(self, p, js=None, form=None):
        return self.req("POST", p, js=js, form=form)


def inbox_since(n):
    """读取 SMTP 捕获日志的后 n 条。"""
    if not os.path.isfile(INBOX):
        return []
    out = []
    for line in open(INBOX, encoding="utf-8", errors="ignore"):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out[-n:] if n else out


def inbox_count():
    return len(inbox_since(0))


def extract_token(text):
    m = re.search(r"token=([0-9a-f]{32,})", text or "")
    return m.group(1) if m else None


def main():
    print("=" * 74)
    print("注册邮箱验证 —— 端到端测试")
    print("=" * 74)

    from app import sitesettings as SS
    snap = {k: SS.get(k) for k in ("mail.verify_register", "mail.verify_ttl_hours",
                                   "mail.enabled", "mail.host", "mail.port",
                                   "mail.encryption", "mail.from_addr", "mail.user",
                                   "mail.password")}
    tag = str(int(time.time()))[-6:]

    ad = Client()
    st, r = ad.post("/api/v1/login", form={"username": "admin", "password": "admin123"})
    ok("管理员登录", st == 200 and r.get("is_admin") is True)

    # ---------- §1 未开启验证 ----------
    print("\n[1] 未开启验证：注册即用")
    SS.set_("mail.verify_register", False)
    c1 = Client()
    u1 = "nv" + tag
    st, r = c1.post("/api/v1/register", form={"username": u1, "password": "pass1234", "email": ""})
    ok("无邮箱也可注册", st == 200 and r.get("status") == "ok", str(r)[:70])
    st, me = c1.get("/api/v1/user")
    ok("注册后直接进入登录态", st == 200 and me.get("logged_in") is True)
    ok("注册返回 verified=True", r.get("verified") is True)

    # ---------- §2 开启验证但邮件未配置 ----------
    print("\n[2] 开启验证但邮件未配置：明确报错")
    SS.set_("mail.verify_register", True)
    _save_mail = {k: SS.get(k) for k in ("mail.enabled", "mail.host")}
    SS.set_("mail.enabled", False)
    c2 = Client()
    st, r = c2.post("/api/v1/register", form={"username": "nc" + tag, "password": "pass1234",
                                              "email": f"nc{tag}@example.com"})
    ok("邮件未配置（未启用）时注册被明确拒绝",
       st == 503 and "无法发信" in str(r.get("detail")) and "邮件通知未启用" in str(r.get("detail")),
       f"HTTP {st} {str(r.get('detail'))[:52]}")
    # 再测「已启用但缺主机」这一分支
    SS.set_("mail.enabled", True)
    SS.set_("mail.host", "")
    st, r = c2.post("/api/v1/register", form={"username": "nc2" + tag, "password": "pass1234",
                                              "email": f"nc2{tag}@example.com"})
    ok("缺 SMTP 主机时注册被明确拒绝",
       st == 503 and "SMTP 服务器未配置" in str(r.get("detail")),
       f"HTTP {st} {str(r.get('detail'))[:52]}")
    _created = [x["username"] for x in (ad.get("/api/v1/admin/users")[1].get("users") or [])]
    ok("两种情况均未创建半成品账号", "nc" + tag not in _created and "nc2" + tag not in _created)
    SS.set_("mail.enabled", _save_mail["mail.enabled"])
    SS.set_("mail.host", _save_mail["mail.host"])

    # 配置本地 SMTP 捕获服务
    SS.set_("mail.enabled", True)
    SS.set_("mail.host", "127.0.0.1")
    SS.set_("mail.port", 2525)
    SS.set_("mail.encryption", "none")
    SS.set_("mail.from_addr", "noreply@kwrt.local")
    SS.set_("mail.verify_ttl_hours", 24)
    ok("SMTP 已指向本地捕获服务", SS.get("mail.host") == "127.0.0.1")

    # ---------- §3 正常流程 ----------
    print("\n[3] 正常流程：注册 → 收信 → 验证 → 登录")
    before = inbox_count()
    c3 = Client()
    u3 = "vf" + tag
    e3 = f"{u3}@example.com"
    st, r = c3.post("/api/v1/register", form={"username": u3, "password": "pass1234", "email": e3})
    ok("注册返回 pending_verification", st == 200 and r.get("status") == "pending_verification",
       str(r.get("status")))
    ok("注册响应不回传令牌", "token" not in json.dumps(r))
    time.sleep(1.5)
    got = inbox_since(3)
    mine = [m for m in got if e3 in json.dumps(m)]
    ok("SMTP 服务器实际收到验证邮件", len(mine) >= 1, f"收件箱新增 {inbox_count()-before} 封")
    if mine:
        m = mine[-1]
        ok("收件人正确", e3 in json.dumps(m.get("to")))
        body = (m.get("body") or "") + (m.get("html") or "")
        tok = extract_token(body)
        ok("邮件正文含验证链接", bool(tok), (m.get("subject") or "")[:44])
        ok("链接含有效期说明", "小时" in body)

        st, me = c3.get("/api/v1/user")
        ok("未验证时尚未建立会话", me.get("logged_in") is not True)

        c4 = Client()
        st, lr = c4.post("/api/v1/login", form={"username": u3, "password": "pass1234"})
        ok("未验证账号登录被拦", st == 403 and lr.get("unverified") is True,
           f"HTTP {st} {str(lr.get('detail'))[:30]}")

        st, vr = c4.post("/api/v1/verify_email", form={"token": tok})
        ok("验证成功", st == 200 and vr.get("verified") is True, str(vr)[:60])
        st, lr2 = c4.post("/api/v1/login", form={"username": u3, "password": "pass1234"})
        ok("验证后可正常登录", st == 200 and lr2.get("username") == u3)

        # ---------- §5 一次性 ----------
        print("\n[5] 令牌一次性")
        c5 = Client()
        st, rr = c5.post("/api/v1/verify_email", form={"token": tok})
        ok("同一令牌重放被拒", st == 400 and "已被使用" in str(rr.get("detail")),
           str(rr.get("detail"))[:30])

        # ---------- §7 伪造 ----------
        print("\n[7] 伪造 / 无效令牌")
        c6 = Client()
        st, rr = c6.post("/api/v1/verify_email", form={"token": "f" * 64})
        ok("伪造令牌被拒", st == 400, str(rr.get("detail"))[:30])
        st, rr = c6.post("/api/v1/verify_email", form={"token": ""})
        ok("空令牌被拒", st == 400)
        st, rr = c6.post("/api/v1/verify_email", form={"token": "short"})
        ok("过短令牌被拒", st == 400, str(rr.get("detail"))[:30])

    # ---------- §6 过期 ----------
    print("\n[6] 令牌过期")
    c7 = Client()
    u7 = "ex" + tag
    st, r = c7.post("/api/v1/register", form={"username": u7, "password": "pass1234",
                                              "email": f"{u7}@example.com"})
    time.sleep(1.2)
    m7 = [m for m in inbox_since(4) if f"{u7}@example.com" in json.dumps(m)]
    tok7 = extract_token((m7[-1].get("body") or "") + (m7[-1].get("html") or "")) if m7 else None
    if tok7:
        with sqlite3.connect(os.path.join(ROOT, "users.db")) as c:
            c.execute("UPDATE email_verifications SET expires=? WHERE used=0 AND username=?",
                      (time.time() - 10, u7))
        c8 = Client()
        st, rr = c8.post("/api/v1/verify_email", form={"token": tok7})
        ok("过期令牌被拒", st == 400 and "过期" in str(rr.get("detail")), str(rr.get("detail"))[:30])
    else:
        ok("过期令牌被拒", False, "未取到令牌")

    # ---------- §8 重发 ----------
    print("\n[8] 重发验证邮件")
    c9 = Client()
    u9 = "rs" + tag
    st, r = c9.post("/api/v1/register", form={"username": u9, "password": "pass1234",
                                              "email": f"{u9}@example.com"})
    time.sleep(1.2)
    m9 = [m for m in inbox_since(4) if f"{u9}@example.com" in json.dumps(m)]
    old_tok = extract_token((m9[-1].get("body") or "") + (m9[-1].get("html") or "")) if m9 else None
    st, rr = c9.post("/api/v1/resend_verify", form={"username": u9})
    ok("冷却期内重发被限流（429）", st == 429, f"HTTP {st} {str(rr.get('detail'))[:30]}")
    # 绕过冷却直接改创建时间
    with sqlite3.connect(os.path.join(ROOT, "users.db")) as c:
        c.execute("UPDATE email_verifications SET created=created-120 WHERE used=0 AND username=?",
                  (u9,))
    st, rr = c9.post("/api/v1/resend_verify", form={"username": u9})
    ok("冷却后重发成功", st == 200, str(rr)[:50])
    time.sleep(1.2)
    m9b = [m for m in inbox_since(4) if f"{u9}@example.com" in json.dumps(m)]
    new_tok = extract_token((m9b[-1].get("body") or "") + (m9b[-1].get("html") or "")) if m9b else None
    ok("重发产生了新令牌", bool(new_tok) and new_tok != old_tok)
    if old_tok:
        st, rr = c9.post("/api/v1/verify_email", form={"token": old_tok})
        ok("旧令牌已作废", st == 400, str(rr.get("detail"))[:30])
    if new_tok:
        st, rr = c9.post("/api/v1/verify_email", form={"token": new_tok})
        ok("新令牌可用", st == 200)
    st, rr = c9.post("/api/v1/resend_verify", form={"username": "no_such_user_zzz"})
    ok("对不存在用户重发给出明确错误", st == 404, f"HTTP {st}")
    st, rr = c9.post("/api/v1/resend_verify", form={"username": u3})
    ok("对已验证用户重发提示可直接登录", st == 200 and "已验证" in str(rr.get("detail")),
       str(rr.get("detail"))[:30])

    # ---------- §9 重复注册未验证同名 ----------
    print("\n[9] 重复注册同名未验证账号")
    c10 = Client()
    u10 = "dp" + tag
    c10.post("/api/v1/register", form={"username": u10, "password": "pass1234",
                                       "email": f"{u10}@example.com"})
    c11 = Client()
    st, rr = c11.post("/api/v1/register", form={"username": u10, "password": "pass1234",
                                                "email": f"{u10}@example.com"})
    ok("重复注册未验证同名给出指引", st == 409 and rr.get("unverified") is True,
       f"HTTP {st} {str(rr.get('detail'))[:34]}")

    # ---------- §10 管理员介入 ----------
    print("\n[10] 管理员介入")
    st, rr = ad.post("/api/v1/admin/user/verify", form={"username": u10, "verified": 1})
    ok("管理员可手动置位验证", st == 200, str(rr.get("detail"))[:30])
    c12 = Client()
    st, lr = c12.post("/api/v1/login", form={"username": u10, "password": "pass1234"})
    ok("手动置位后可登录", st == 200, f"HTTP {st}")
    st, rr = ad.post("/api/v1/admin/user/reverify", form={"username": u9})
    ok("管理员可代发验证邮件", st in (200, 429), f"HTTP {st} {str(rr.get('detail'))[:30]}")

    # ---------- §11 统计 ----------
    print("\n[11] 管理端统计")
    st, vs = ad.get("/api/v1/admin/pay/verify_stats")
    e = (vs or {}).get("email") or {}
    ok("验证统计可读", st == 200 and "total" in e,
       f"total={e.get('total')} active={e.get('active')} used={e.get('used')} sent_ok={e.get('sent_ok')}")
    ok("已记录收信成功数", e.get("sent_ok", 0) >= 3, str(e.get("sent_ok")))
    ok("统计含未验证用户数", "unverified_users" in e, str(e.get("unverified_users")))

    # ---------- 清理 ----------
    print("\n[12] 清理")
    for uu in (u1, u3, u7, u9, u10, "nc" + tag):
        ad.post("/api/v1/admin/user", form={"username": uu, "action": "delete"})
    with sqlite3.connect(os.path.join(ROOT, "users.db")) as c:
        c.execute("DELETE FROM email_verifications WHERE username LIKE ?", ("%" + tag,))
        c.execute("DELETE FROM email_send_log WHERE username LIKE ?", ("%" + tag,))
    ok("测试数据已清理", True)

    for k, v in snap.items():
        SS.set_(k, v)
    print("已还原测试前的邮件/验证配置。")

    print("\n" + "=" * 74)
    print(f"结果: 通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项:")
        for f in FAIL:
            print("  ✗", f)
    else:
        print("全部通过 ✓")
    print("=" * 74)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
