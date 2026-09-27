#!/usr/bin/env python3
"""
支付宝当面付 —— 协议一致性测试。

为什么不用「假数据」而用「真协议」：
    本环境没有支付宝商户账号，无法对真实网关跑通支付。但协议本身（规范化串、
    RSA2 签名、响应原文切片验签、异步通知验签）是**可独立验证**的。

    因此这里起一个「协议合规的模拟网关」：
      · 用真实的 RSA-2048 密钥对
      · 用真实的 SHA256withRSA 签名/验签
      · 用与支付宝一致的三段式响应体（节点 JSON 原文 + sign）
    它能证明：待签名串构造、签名、响应切片、验签、状态机 —— 全部与规范一致。
    它不能证明：真实商户资质、真实资金流。这部分边界在报告里如实标注。

覆盖：
    §1 待签名串规范化（剔除 sign/sign_type、剔除空值、key 升序）
    §2 密钥装载（PKCS#8 / PKCS#1 / 带或不带 PEM 头）
    §3 下单 precreate —— 请求验签通过、响应验签通过、取回 qr_code
    §4 响应原文切片 —— 含转义/Unicode/嵌套花括号的极端场景
    §5 查单 query —— 未支付 / 已支付 两态，赞助态真实置位
    §6 幂等 —— 重复查单不重复发放天数
    §7 异步通知 notify —— 正确验签放行、篡改金额拒绝、伪造签名拒绝
    §8 订单过期
    §9 密钥不泄露（前台/管理端接口均不回传私钥）
"""
import base64
import hashlib
import http.server
import json
import os
import re
import socket
import socketserver
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cryptography.hazmat.primitives import hashes, serialization       # noqa: E402
from cryptography.hazmat.primitives.asymmetric import padding, rsa     # noqa: E402

BASE = "http://127.0.0.1:8443"
PASS, FAIL = [], []


def ok(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  [{extra}]" if extra else ""))


# --------------------------------------------------------------------------- #
# 密钥对
# --------------------------------------------------------------------------- #
def gen_keypair():
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = k.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()).decode()
    pub = k.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    # 支付宝控制台给的是「一行 base64，无 PEM 头」，这里剥掉头尾以贴近真实
    priv_bare = "".join(priv.strip().splitlines()[1:-1])
    pub_bare = "".join(pub.strip().splitlines()[1:-1])
    return priv, priv_bare, pub, pub_bare


APP_PRIV, APP_PRIV_BARE, APP_PUB, APP_PUB_BARE = gen_keypair()
ALI_PRIV, ALI_PRIV_BARE, ALI_PUB, ALI_PUB_BARE = gen_keypair()

# 网关侧用于验签「我方请求」的应用公钥
APP_PUB_KEY = serialization.load_pem_public_key(APP_PUB.encode())
# 我方私钥对象（用于测试「用错私钥签名」的负向用例）
APP_PRIV_KEY = serialization.load_pem_private_key(APP_PRIV.encode(), password=None)
# 网关侧用于签名「支付宝响应」的私钥
ALI_PRIV_KEY = serialization.load_pem_private_key(ALI_PRIV.encode(), password=None)

# 模拟网关收到的请求留档，供断言检查
RECEIVED = []


def _norm(params: dict) -> str:
    items = [(k, v) for k, v in params.items()
             if k not in ("sign", "sign_type") and v not in (None, "")]
    items.sort(key=lambda kv: kv[0])
    return "&".join(f"{k}={v}" for k, v in items)


def _verify_with(content: str, signature: str, pub) -> bool:
    try:
        pub.verify(base64.b64decode(signature), content.encode("utf-8"),
                   padding.PKCS1v15(), hashes.SHA256())
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# 模拟网关（协议合规）
# --------------------------------------------------------------------------- #
class Gateway(http.server.BaseHTTPRequestHandler):
    # 由测试用例控制的下单/查单行为
    state = {"paid": False, "qr": "https://qr.alipay.com/bax0test0001",
             "trade_no": "2026092422001400000000000001"}

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n).decode("utf-8")
        # 注意：必须 dict(parse_qsl(...))，写 v[0] 会取到值的首字符
        params = dict(urllib.parse.parse_qsl(raw, keep_blank_values=True))
        RECEIVED.append(params)

        method = params.get("method", "")
        node = method.replace(".", "_") + "_response"

        # 1) 校验我方请求签名（用「应用公钥」）
        sig_ok = _verify_with(_norm(params), params.get("sign", ""), APP_PUB_KEY)

        # 2) 组装响应节点。故意让某些字段含 Unicode / 转义字符，
        #    以检验客户端是否按「原文切片」验签（重新序列化会失败）
        if method == "alipay.trade.precreate":
            body = {"code": "10000", "msg": "Success",
                    "out_trade_no": params.get("out_trade_no", ""),
                    "qr_code": self.state["qr"]}
        elif method == "alipay.trade.query":
            try:
                biz = json.loads(params.get("biz_content", "{}"))
            except Exception:
                biz = {}
            if self.state["paid"]:
                body = {"code": "10000", "msg": "Success", "trade_status": "TRADE_SUCCESS",
                        "trade_no": self.state["trade_no"],
                        "buyer_user_id": "2088102177846880",
                        "out_trade_no": biz.get("out_trade_no", "")}
            else:
                body = {"code": "10000", "msg": "Success", "trade_status": "WAIT_BUYER_PAY",
                        "out_trade_no": biz.get("out_trade_no", "")}
        elif method == "alipay.trade.close":
            body = {"code": "10000", "msg": "Success",
                    "out_trade_no": params.get("out_trade_no", "")}
        else:
            body = {"code": "40004", "msg": "Business Failed",
                    "sub_code": "isv.invalid-method", "sub_msg": "无效的接口"}

        # 关键：对「节点原文」签名 —— 客户端也必须对同一段原文验签
        segment = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        sign = base64.b64encode(
            ALI_PRIV_KEY.sign(segment.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
        ).decode()
        payload = '{"%s":%s,"sign":"%s"}' % (node, segment, sign)

        out = payload.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json;charset=utf-8")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)
        # 留个记号：请求验签结果
        RECEIVED[-1]["__sig_ok"] = sig_ok


def find_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# --------------------------------------------------------------------------- #
# HTTP 客户端
# --------------------------------------------------------------------------- #
class Client:
    def __init__(self):
        self.cj = urllib.request.HTTPCookieProcessor()
        self.op = urllib.request.build_opener(self.cj)

    def req(self, method, path, data=None, js=None, form=None):
        url = BASE + path
        hdr = {}
        body = None
        if js is not None:
            body = json.dumps(js).encode()
            hdr["Content-Type"] = "application/json"
        elif form is not None:
            body = urllib.parse.urlencode(form).encode()
            hdr["Content-Type"] = "application/x-www-form-urlencoded"
        elif data is not None:
            body = data
        r = urllib.request.Request(url, data=body, method=method, headers=hdr)
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


def main():
    print("=" * 74)
    print("支付宝当面付 —— 协议一致性测试")
    print("=" * 74)

    # ---------- §1 待签名串规范化 ----------
    print("\n[1] 待签名串规范化")
    from app import pay as P

    s = P._sign_string({"b": "2", "a": "1", "sign": "XXX", "sign_type": "RSA2", "c": ""})
    ok("剔除 sign / sign_type 且 key 升序", s == "a=1&b=2", repr(s))
    s2 = P._sign_string({"z": "9", "a": "1", "empty": "", "none": None})
    ok("剔除空值与 None", s2 == "a=1&z=9", repr(s2))
    ok("待签名串不含空段", "&&" not in s2)

    # ---------- §2 密钥装载 ----------
    print("\n[2] 密钥装载（无 PEM 头 / PKCS#8）")
    try:
        k1 = P._load_private(APP_PRIV_BARE)
        ok("裸 base64 私钥可装载", k1 is not None)
    except Exception as e:
        ok("裸 base64 私钥可装载", False, str(e)[:60])
    try:
        k2 = P._load_public(ALI_PUB_BARE)
        ok("裸 base64 公钥可装载", k2 is not None)
    except Exception as e:
        ok("裸 base64 公钥可装载", False, str(e)[:60])
    try:
        P._load_private(APP_PRIV)
        ok("带 PEM 头的私钥可装载", True)
    except Exception as e:
        ok("带 PEM 头的私钥可装载", False, str(e)[:60])

    # ---------- §4 响应原文切片（极端场景） ----------
    print("\n[4] 响应原文切片（转义 / Unicode / 嵌套）")
    _body = {"msg": '成功 "含引号"', "note": "换行\n与斜杠/与反斜杠\\与制表\t",
             "unicode": "夏日诗意·中文", "nested": {"a": {"b": [1, 2, {"c": "}}}"}]}},
             "arr": [{"x": "}"}, {"y": '"'}]}
    _node = "alipay_trade_query_response"
    _seg = json.dumps(_body, ensure_ascii=False, separators=(",", ":"))
    _payload = json.dumps({_node: _body}, ensure_ascii=False, separators=(",", ":"))
    _payload = _payload[:-1] + ',"sign":"SIGVALUE"}'
    seg, sig = P._extract_node(_payload, "alipay.trade.query")
    ok("含转义引号/反斜杠/Unicode/嵌套花括号时切片逐字节一致",
       seg == _seg and sig == "SIGVALUE",
       f"len {len(seg) if seg else 0} vs {len(_seg)}")
    ok("切片内容可被 JSON 解析",
       seg is not None and json.loads(seg)["arr"][1]["y"] == '"')
    ok("节点不存在时返回 None",
       P._extract_node(_payload, "alipay.trade.precreate")[0] is None)
    # 用真密钥验证：只有原文能通过验签
    _sigb = base64.b64encode(
        ALI_PRIV_KEY.sign(_seg.encode(), padding.PKCS1v15(), hashes.SHA256())).decode()
    ok("对正确原文验签通过", P.verify(_seg, _sigb, ALI_PUB_BARE) is True)
    ok("对重新序列化的原文验签失败（印证必须用原文切片）",
       P.verify(json.dumps(json.loads(_seg), separators=(",", ":")), _sigb, ALI_PUB_BARE) is False)

    # ---------- 启动模拟网关并配置站点 ----------
    port = find_port()
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", port), Gateway)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    gw = f"http://127.0.0.1:{port}/gateway.do"
    print(f"\n    模拟网关已启动: {gw}")

    ad = Client()
    st, r = ad.post("/api/v1/login", form={"username": "admin", "password": "admin123"})
    ok("管理员登录", st == 200 and r.get("is_admin") is True)

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from app import sitesettings as SS
    snap = {k: SS.get(k) for k in ("pay.alipay_enabled", "pay.alipay_app_id",
                                   "pay.alipay_private_key", "pay.alipay_public_key",
                                   "pay.alipay_gateway", "pay.alipay_sandbox",
                                   "pay.auto_activate", "pay.order_ttl_minutes",
                                   "mail.verify_register", "mail.verify_ttl_hours")}
    SS.set_("pay.alipay_enabled", True)
    SS.set_("pay.alipay_app_id", "2021000000000000")
    SS.set_("pay.alipay_private_key", APP_PRIV_BARE)
    SS.set_("pay.alipay_public_key", ALI_PUB_BARE)
    SS.set_("pay.alipay_gateway", gw)
    SS.set_("pay.alipay_sandbox", False)
    SS.set_("pay.auto_activate", True)
    SS.set_("pay.order_ttl_minutes", 15)
    ok("支付配置已写入", P.is_configured() is True)

    st, pi = ad.get("/api/v1/pay/info")
    ok("支付能力上报可用", pi.get("available") is True and pi.get("configured") is True)

    # ---------- §3 下单 ----------
    print("\n[3] 下单（precreate）")
    u = Client()
    uname = "paytest" + str(int(time.time()))[-5:]
    st, r = u.post("/api/v1/register", form={"username": uname, "password": "paypass123",
                                             "email": f"{uname}@example.com"})
    ok("测试用户注册", st == 200, str(r)[:60])
    st, tiers = u.get("/api/v1/sponsor/tiers")
    tier = (tiers.get("tiers") or [{}])[0].get("name")
    ok("取到赞助套餐", bool(tier), str(tier))

    st, r = u.post("/api/v1/sponsor/pay", form={"tier": tier})
    if st != 200:
        ok("下单成功", False, str(r)[:120])
        # 清理本测试创建的用户与订单（避免污染真实库）
    try:
        import sqlite3 as _sq
        with _sq.connect(os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "users.db")) as _c:
            _c.execute("DELETE FROM pay_orders WHERE username LIKE 'paytest%'")
            _c.execute("DELETE FROM users WHERE username LIKE 'paytest%'")
            _c.execute("DELETE FROM sessions WHERE username NOT IN (SELECT username FROM users)")
        print("\n已清理测试用户与订单。")
    except Exception as e:
        print(f"\n清理失败: {e}")
    return finish(snap, httpd)
    otn = r.get("out_trade_no")
    ok("下单成功", st == 200 and bool(otn), otn)
    ok("取回二维码", bool(r.get("qr_code")) and "alipay.com" in str(r.get("qr_code")),
       str(r.get("qr_code"))[:40])
    ok("响应验签通过", r.get("sign_verified") is True)
    ok("下单接口未回传任何密钥",
       not any(k in json.dumps(r) for k in ("private", "BEGIN", "RSA")))
    last = RECEIVED[-1]
    ok("网关侧校验我方请求签名通过", last.get("__sig_ok") is True)
    ok("请求含 notify_url", "notify_url" in last and "/api/v1/alipay/notify" in last["notify_url"],
       last.get("notify_url", "")[:60])
    ok("请求 biz_content 含金额与订单号",
       "total_amount" in last.get("biz_content", "") and otn in last.get("biz_content", ""))

    # ---------- §5 查单：未支付 ----------
    print("\n[5] 查单（未支付 → 已支付）")
    Gateway.state["paid"] = False
    st, q1 = u.get(f"/api/v1/sponsor/pay/{otn}")
    ok("未支付时 paid=False", q1.get("paid") is False, str(q1.get("trade_status")))
    st, me = u.get("/api/v1/user")
    ok("未支付时赞助态未置位", me.get("sponsor") is False)

    Gateway.state["paid"] = True
    st, q2 = u.get(f"/api/v1/sponsor/pay/{otn}")
    ok("已支付时 paid=True", q2.get("paid") is True, str(q2.get("order_status")))
    ok("返回新到期时间", bool(q2.get("until")))
    st, me2 = u.get("/api/v1/user")
    ok("赞助态已真实置位", me2.get("sponsor") is True)
    ok("赞助到期时间与订单天数一致",
       abs((me2.get("sponsor_until") or 0) - q2.get("until", 0)) < 2)
    days = int(q2.get("days") or 0)
    expect = time.time() + days * 86400
    ok("到期时间约为 下单天数 之后",
       abs((q2.get("until") or 0) - expect) < 120, f"until-now={(q2.get('until') or 0)-time.time():.0f}s "
                                                   f"expect={days*86400}s")

    # ---------- §6 幂等 ----------
    print("\n[6] 幂等（不重复发放）")
    first_until = q2.get("until")
    Gateway.state["paid"] = True
    for _ in range(3):
        u.get(f"/api/v1/sponsor/pay/{otn}")
    st, me3 = u.get("/api/v1/user")
    ok("重复查单不重复发放天数",
       abs((me3.get("sponsor_until") or 0) - first_until) < 2,
       f"{first_until} -> {me3.get('sponsor_until')}")

    # ---------- §7 异步通知 ----------
    print("\n[7] 异步通知（notify）")

    def notify(order_no, amount, sign_with=None, tamper_amount=None):
        f = {"app_id": "2021000000000000", "out_trade_no": order_no,
             "trade_no": "2026092422001400000000000001", "trade_status": "TRADE_SUCCESS",
             "total_amount": f"{amount:.2f}", "buyer_id": "2088102177846880",
             "gmt_payment": time.strftime("%Y-%m-%d %H:%M:%S"), "sign_type": "RSA2"}
        if tamper_amount is not None:
            f["total_amount"] = f"{tamper_amount:.2f}"
        key = sign_with if sign_with is not None else ALI_PRIV_KEY
        seg = _norm(f)
        f["sign"] = base64.b64encode(
            key.sign(seg.encode(), padding.PKCS1v15(), hashes.SHA256())).decode()
        return f

    # 新开一单用于 notify 测试
    u2 = Client()
    u2.post("/api/v1/login", form={"username": uname, "password": "paypass123"})
    Gateway.state["paid"] = False
    st, r2 = u2.post("/api/v1/sponsor/pay", form={"tier": tier})
    otn2 = r2.get("out_trade_no")
    ok("为 notify 测试新建订单", bool(otn2), otn2)
    before = u2.get("/api/v1/user")[1].get("sponsor_until")

    # 7a 正确签名 → success
    f_ok_ = notify(otn2, float(r2.get("amount") or 10))
    data = urllib.parse.urlencode(f_ok_).encode()
    req = urllib.request.Request(BASE + "/api/v1/alipay/notify", data=data,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        txt = resp.read().decode()
    ok("正确签名的通知被接受（应答 success）", txt.strip() == "success", txt[:40])
    after = u2.get("/api/v1/user")[1].get("sponsor_until")
    ok("notify 路径确实置位了赞助", (after or 0) > (before or 0),
       f"{before} -> {after}")

    # 7b 伪造签名 → failure
    bad = notify(otn2, float(r2.get("amount") or 10), sign_with=APP_PRIV_KEY)  # 用错私钥
    try:
        with urllib.request.urlopen(urllib.request.Request(
                BASE + "/api/v1/alipay/notify", data=urllib.parse.urlencode(bad).encode(),
                headers={"Content-Type": "application/x-www-form-urlencoded"}), timeout=20) as resp:
            txt2, code2 = resp.read().decode(), resp.status
    except urllib.error.HTTPError as e:
        txt2, code2 = e.read().decode(), e.code
    ok("伪造签名的通知被拒绝", txt2.strip() == "failure" and code2 == 400, f"{code2} {txt2[:20]}")

    # 7c 篡改金额 → failure
    Gateway.state["paid"] = False
    st, r3 = u2.post("/api/v1/sponsor/pay", form={"tier": tier})
    otn3 = r3.get("out_trade_no")
    tampered = notify(otn3, float(r3.get("amount") or 10), tamper_amount=0.01)
    try:
        with urllib.request.urlopen(urllib.request.Request(
                BASE + "/api/v1/alipay/notify", data=urllib.parse.urlencode(tampered).encode(),
                headers={"Content-Type": "application/x-www-form-urlencoded"}), timeout=20) as resp:
            txt3, code3 = resp.read().decode(), resp.status
    except urllib.error.HTTPError as e:
        txt3, code3 = e.read().decode(), e.code
    ok("金额被篡改的通知被拒绝", txt3.strip() == "failure" and code3 == 400, f"{code3} {txt3[:20]}")

    # 7d 不存在的订单
    ghost = notify("XSP0000000000000000", 10.0)
    try:
        with urllib.request.urlopen(urllib.request.Request(
                BASE + "/api/v1/alipay/notify", data=urllib.parse.urlencode(ghost).encode(),
                headers={"Content-Type": "application/x-www-form-urlencoded"}), timeout=20) as resp:
            txt4, code4 = resp.read().decode(), resp.status
    except urllib.error.HTTPError as e:
        txt4, code4 = e.read().decode(), e.code
    ok("不存在的订单被拒绝", code4 == 404 and txt4.strip() == "failure", f"{code4}")

    # ---------- §8 订单过期 ----------
    print("\n[8] 订单过期与越权")
    Gateway.state["paid"] = False
    st, r4 = u2.post("/api/v1/sponsor/pay", form={"tier": tier})
    otn4 = r4.get("out_trade_no")
    with __import__("sqlite3").connect("users.db") as c:
        c.execute("UPDATE pay_orders SET expires=? WHERE out_trade_no=?", (time.time() - 10, otn4))
    st, q4 = u2.get(f"/api/v1/sponsor/pay/{otn4}")
    ok("超时订单标记 expired", q4.get("order_status") == "expired", str(q4.get("order_status")))

    # 越权：另一个用户查看他人订单
    other = Client()
    st, r5 = other.post("/api/v1/register", form={"username": uname + "x", "password": "paypass123",
                                                  "email": f"{uname}x@example.com"})
    st, cross = other.get(f"/api/v1/sponsor/pay/{otn4}")
    ok("他人订单不可查看（403）", st == 403, f"HTTP {st}")

    # ---------- §9 密钥不泄露 ----------
    print("\n[9] 密钥不泄露")
    st, site = Client().get("/api/v1/site")
    j = json.dumps(site)
    ok("前台不泄露应用私钥", APP_PRIV_BARE[:40] not in j)
    ok("前台不泄露支付宝公钥", ALI_PUB_BARE[:40] not in j)
    ok("前台无 pay.alipay_private_key 字段", "pay.alipay_private_key" not in site)
    st, cfg = ad.get("/api/v1/admin/site")
    val = (cfg.get("values") or {})
    ok("管理端私钥为掩码", val.get("pay.alipay_private_key") in ("", "********"),
       str(val.get("pay.alipay_private_key"))[:20])
    ok("管理端支付宝公钥为掩码", val.get("pay.alipay_public_key") in ("", "********"),
       str(val.get("pay.alipay_public_key"))[:20])
    ok("密钥未出现在任何前台接口响应中",
       APP_PRIV_BARE[:40] not in json.dumps(site) + json.dumps(pi))

    # ---------- 管理端 ----------
    print("\n[10] 管理端支付视图")
    st, info = ad.get("/api/v1/admin/pay/info")
    ok("管理端支付信息可读",
       st == 200 and info.get("paid_count", 0) >= 2, str(info.get("by_status"))[:80])
    ok("上报 notify_url", "/api/v1/alipay/notify" in (info.get("notify_url") or ""),
       str(info.get("notify_url"))[:60])
    st, orders = ad.get("/api/v1/admin/pay/orders")
    ok("管理端订单列表可读", st == 200 and orders.get("count", 0) >= 1,
       f"{orders.get('count')} 笔")
    st, vs = ad.get("/api/v1/admin/pay/verify_stats")
    ok("邮箱验证统计可读", st == 200 and "email" in vs, str(vs.get("email"))[:70])

    return finish(snap, httpd)


def finish(snap, httpd):
    httpd.shutdown()
    print("\n" + "=" * 74)
    print(f"结果: 通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项:")
        for f in FAIL:
            print("  ✗", f)
    else:
        print("全部通过 ✓")
    print("=" * 74)
    print("\n注：本测试验证的是「支付宝当面付协议实现」。")
    print("    真实资金流需商户 APPID 与密钥，本环境无法覆盖 —— 该边界已在报告中标注。")
    # 还原配置
    try:
        from app import sitesettings as _SS2
        for k, v in snap.items():
            _SS2.set_(k, v)
        print("\n已还原测试前的支付/验证配置。")
    except Exception as e:
        print(f"\n配置还原失败: {e}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
