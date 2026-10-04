"""
支付宝当面付（alipay.trade.precreate）集成。

流程：
    1. 服务端调 alipay.trade.precreate 下单，取回 qr_code
    2. 前端展示二维码，用户用支付宝扫码支付
    3. 两条并行的确认路径：
       a) 前端轮询 → 服务端调 alipay.trade.query 主动查单
       b) 支付宝异步回调 notify_url（用户关闭页面也能置位）
       两条路径都验签后才置位，互不信任前端。

签名要点（易错处，见下方注释）：
    - 待签名串：参数按 key 升序，剔除 sign 与空值，`k=v` 以 & 连接
    - 用商户私钥 RSA-SHA256 签名，base64 编码
    - 同步响应验签：必须对**响应节点的原始 JSON 子串**验签，
      不能重新 json.dumps(dict)——序列化差异会导致验签失败
    - 异步通知验签：用回调表单的原始值，同样剔除 sign / sign_type / 空值
"""
from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

GATEWAY_PROD = "https://openapi.alipay.com/gateway.do"
GATEWAY_SANDBOX = "https://openapi-sandbox.dl.alipaydev.com/gateway.do"

# 支付状态语义
PAID_STATES = ("TRADE_SUCCESS", "TRADE_FINISHED")
DEAD_STATES = ("TRADE_CLOSED",)


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def _settings():
    """延迟导入，避免与 main 的初始化顺序耦合。"""
    from . import sitesettings as SS
    return SS


def _s(v) -> str:
    """设置值可能是 str/int/bool/list，统一安全转成去空白的字符串。"""
    if v is None or isinstance(v, (list, dict)):
        return ""
    if isinstance(v, bool):
        return "1" if v else ""
    return str(v).strip()


def cfg():
    SS = _settings()
    sandbox = bool(SS.get("pay.alipay_sandbox"))
    gw = _s(SS.get("pay.alipay_gateway"))
    if not gw:
        gw = GATEWAY_SANDBOX if sandbox else GATEWAY_PROD
    return {
        "enabled": bool(SS.get("pay.alipay_enabled")),
        "app_id": _s(SS.get("pay.alipay_app_id")),
        "private_key": _s(SS.get("pay.alipay_private_key")),
        "alipay_public_key": _s(SS.get("pay.alipay_public_key")),
        "gateway": gw,
        "sandbox": sandbox,
        "subject_prefix": _s(SS.get("pay.alipay_subject_prefix")) or "赞助",
        "notify_url": _s(SS.get("pay.notify_url")),
    }


def is_configured() -> bool:
    c = cfg()
    return bool(c["app_id"] and c["private_key"] and c["alipay_public_key"])


def available() -> bool:
    c = cfg()
    return c["enabled"] and is_configured()


def info() -> dict:
    c = cfg()
    return {
        "enabled": c["enabled"],
        "configured": bool(c["app_id"] and c["private_key"] and c["alipay_public_key"]),
        "available": available(),
        "app_id": c["app_id"],
        "has_private_key": bool(c["private_key"]),
        "has_public_key": bool(c["alipay_public_key"]),
        "sandbox": c["sandbox"],
        "gateway": c["gateway"],
        "notify_url": c["notify_url"],
    }


# --------------------------------------------------------------------------- #
# 密钥装载（支付宝控制台给的是一行 base64，无 PEM 头）
# --------------------------------------------------------------------------- #
def _wrap_pem(key: str, kind: str) -> str:
    """kind: 'private' | 'public'。已含 PEM 头则原样返回。"""
    k = key.strip()
    if "-----BEGIN" in k:
        return k
    body = "".join(k.split())
    lines = [body[i:i + 64] for i in range(0, len(body), 64)]
    if kind == "private":
        head, tail = "-----BEGIN PRIVATE KEY-----", "-----END PRIVATE KEY-----"
    else:
        head, tail = "-----BEGIN PUBLIC KEY-----", "-----END PUBLIC KEY-----"
    return f"{head}\n" + "\n".join(lines) + f"\n{tail}\n"


def _load_private(key: str):
    pem = _wrap_pem(key, "private").encode()
    try:
        return serialization.load_pem_private_key(pem, password=None)
    except Exception:
        # 支付宝旧版给的是 PKCS#1（RSA PRIVATE KEY）
        body = "".join(key.split())
        lines = [body[i:i + 64] for i in range(0, len(body), 64)]
        pkcs1 = ("-----BEGIN RSA PRIVATE KEY-----\n" + "\n".join(lines)
                 + "\n-----END RSA PRIVATE KEY-----\n").encode()
        return serialization.load_pem_private_key(pkcs1, password=None)


def _load_public(key: str):
    pem = _wrap_pem(key, "public").encode()
    return serialization.load_pem_public_key(pem)


# --------------------------------------------------------------------------- #
# 签名与验签
# --------------------------------------------------------------------------- #
def _sign_string(params: dict, drop_sign_type: bool = False) -> str:
    """构造待签名字符串。

    两类场景的剔除规则不同，混用会直接导致 isv.invalid-signature：

    * 请求签名（商户 → 支付宝）：剔除 `sign`，**保留 `sign_type`**。
      网关实际用于验签的字符串就含 `sign_type=RSA2`——把请求打错时
      支付宝会在报错里回显该串，可直接比对。
    * 异步通知验签：按官方 rsaCheckV1 同时剔除 `sign` 与 `sign_type`。
    """
    drop = ("sign", "sign_type") if drop_sign_type else ("sign",)
    items = [(k, v) for k, v in params.items()
             if k not in drop and v not in (None, "")]
    items.sort(key=lambda kv: kv[0])
    return "&".join(f"{k}={v}" for k, v in items)


def sign(params: dict, private_key: str) -> str:
    """请求签名：待签名串保留 sign_type（与网关验签串逐字节一致）。"""
    data = _sign_string(params).encode("utf-8")
    key = _load_private(private_key)
    sig = key.sign(data, padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(sig).decode()


def verify(content: str, signature: str, public_key: str) -> bool:
    try:
        pub = _load_public(public_key)
        pub.verify(base64.b64decode(signature), content.encode("utf-8"),
                   padding.PKCS1v15(), hashes.SHA256())
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False
    except Exception as e:
        # 公钥格式错误、底层库异常等：不静默吞掉，留痕便于排查
        print(f"[pay] verify error: {type(e).__name__}: {e}", flush=True)
        return False


def build_params(method: str, biz: dict, notify_url: str = "") -> dict:
    c = cfg()
    p = {
        "app_id": c["app_id"],
        "method": method,
        "format": "JSON",
        "charset": "utf-8",
        "sign_type": "RSA2",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "version": "1.0",
        "biz_content": json.dumps(biz, ensure_ascii=False, separators=(",", ":")),
    }
    nu = notify_url or c["notify_url"]
    if nu:
        p["notify_url"] = nu
    p["sign"] = sign(p, c["private_key"])
    return p


def _extract_node(raw: str, method: str):
    """
    从支付宝同步响应里取出 **原始 JSON 子串** 与签名。

    易错点：验签必须用响应节点的原始字节子串。
    重新 json.dumps() 会因键顺序/空格/转义差异导致验签失败。
    """
    node = method.replace(".", "_") + "_response"
    key = f'"{node}"'
    i = raw.find(key)
    if i < 0:
        return None, None
    j = raw.find("{", i + len(key))
    if j < 0:
        return None, None
    depth, k, in_str, esc = 0, j, False, False
    while k < len(raw):
        ch = raw[k]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    break
        k += 1
    segment = raw[j:k + 1]
    sig = None
    m = raw.find('"sign"', k)
    if m >= 0:
        q1 = raw.find('"', m + 6)
        q2 = raw.find('"', q1 + 1)
        if q1 >= 0 and q2 > q1:
            sig = raw[q1 + 1:q2]
    return segment, sig


def call(method: str, biz: dict, notify_url: str = "", timeout: int = 20) -> dict:
    """
    真实调用支付宝网关。返回 {'ok':bool, 'data':dict, 'verified':bool, 'error':str}
    未配置时明确报错，绝不返回假成功。
    """
    if not is_configured():
        return {"ok": False, "error": "支付宝未配置（缺 app_id / 私钥 / 支付宝公钥）",
                "data": {}, "verified": False}
    c = cfg()
    params = build_params(method, biz, notify_url)
    body = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(
        c["gateway"], data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
                 "User-Agent": "XSP-Kwrt/1.0"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code}: {e.read().decode('utf-8','ignore')[:200]}",
                "data": {}, "verified": False}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "data": {}, "verified": False}

    segment, sig = _extract_node(raw, method)
    if segment is None:
        return {"ok": False, "error": f"响应中未找到 {method} 节点: {raw[:200]}",
                "data": {}, "verified": False}

    verified = bool(sig) and verify(segment, sig, c["alipay_public_key"])
    try:
        data = json.loads(segment)
    except Exception as e:
        return {"ok": False, "error": f"响应 JSON 解析失败: {e}", "data": {}, "verified": verified}

    code = str(data.get("code", ""))
    # 关键：必须「验签通过」且「业务码 10000」才算成功。
    # 原实现只看 code，等于信任未验签的响应体，中间人可伪造成功报文。
    ok = (code == "10000") and verified
    if code == "10000" and not verified:
        return {"ok": False, "data": data, "verified": False,
                "error": "网关响应验签未通过，已拒绝（疑似响应被篡改）"}
    return {
        "ok": ok,
        "data": data,
        "verified": verified,
        "error": "" if ok else f"{data.get('sub_code') or code}: {data.get('sub_msg') or data.get('msg')}",
    }


# --------------------------------------------------------------------------- #
# 业务封装
# --------------------------------------------------------------------------- #
def precreate(out_trade_no: str, amount: str, subject: str,
              notify_url: str = "") -> dict:
    """当面付下单，返回 {'ok', 'qr_code', 'error', 'verified', 'data'}"""
    c = cfg()
    biz = {
        "out_trade_no": out_trade_no,
        "total_amount": f"{float(amount):.2f}",
        "subject": f"{c['subject_prefix']}·{subject}"[:256],
        "timeout_express": "15m",
    }
    r = call("alipay.trade.precreate", biz, notify_url)
    if r["ok"]:
        r["qr_code"] = r["data"].get("qr_code", "")
        if not r["qr_code"]:
            r["ok"] = False
            r["error"] = "支付宝未返回 qr_code"
    return r


def query(out_trade_no: str) -> dict:
    r = call("alipay.trade.query", {"out_trade_no": out_trade_no})
    if r["ok"]:
        r["trade_status"] = r["data"].get("trade_status", "")
        r["trade_no"] = r["data"].get("trade_no", "")
        r["buyer_id"] = r["data"].get("buyer_user_id", "")
    return r


def close(out_trade_no: str) -> dict:
    return call("alipay.trade.close", {"out_trade_no": out_trade_no})


def refund(out_trade_no: str, amount: str, reason: str = "",
           out_request_no: str = "") -> dict:
    """退款 alipay.trade.refund。

    out_request_no 为部分退款时的请求号；不传则整笔全额退。
    返回 {'ok','refund_amount','error','data',...}
    """
    biz = {
        "out_trade_no": out_trade_no,
        "refund_amount": f"{float(amount):.2f}",
    }
    if reason:
        biz["refund_reason"] = str(reason)[:256]
    if out_request_no:
        biz["out_request_no"] = out_request_no
    r = call("alipay.trade.refund", biz)
    if r["ok"]:
        d = r.get("data") or {}
        r["refund_amount"] = d.get("refund_fee") or d.get("refund_amount") or biz["refund_amount"]
        r["fund_change"] = d.get("fund_change")
    return r


def verify_notify(form: dict) -> bool:
    """异步通知验签：按官方 rsaCheckV1，剔除 sign 与 sign_type 后再验。"""
    c = cfg()
    sig = form.get("sign", "")
    if not sig:
        return False
    return verify(_sign_string(dict(form), drop_sign_type=True), sig,
                  c["alipay_public_key"])


# --------------------------------------------------------------------------- #
# 二维码
# --------------------------------------------------------------------------- #
def qr_svg_data_uri(text: str) -> str:
    """把支付链接渲染成内联 SVG 二维码（无需外网、无额外请求）。"""
    import qrcode
    import qrcode.image.svg
    img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage,
                      box_size=10, border=2)
    import io
    buf = io.BytesIO()
    img.save(buf)
    b64 = base64.b64encode(buf.getvalue()).decode()
    return f"data:image/svg+xml;base64,{b64}"


_PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _pc_ihdr(w: int, h: int) -> bytes:
    """IHDR：宽高 + 位深 1 + 颜色类型 3（调色板）+ 压缩/过滤/隔行均为 0。"""
    import struct
    return struct.pack(">IIBBBBB", w, h, 1, 3, 0, 0, 0)


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    """拼一个 PNG chunk：长度 + 类型 + 数据 + CRC32(类型+数据)。"""
    import struct
    import zlib
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def qr_png_bytes(text: str, size: int = 320) -> bytes:
    """把文本渲染成 PNG 二维码。

    ★ **不依赖 Pillow。**

    历史缺陷（线上实测 500）：本函数原先用 `qrcode.make_image()` 的默认工厂，
    那是 **PIL 实现**，末尾还调了 `img.resize()` —— 于是它需要 Pillow。
    而 `requirements.txt` 只声明了 `qrcode`，并在注释里明确写着
    「使用 qrcode.image.svg，**不需要 Pillow**」。结果：任何按 requirements
    装出来的干净部署，一访问 `/api/v1/sponsor/pay/{订单}/qr.png` 就
    `ModuleNotFoundError: No module named 'PIL'` → **支付二维码永远出不来**。
    这是「注释承诺的能力」与「代码实际使用的能力」不一致的典型：
    单元测试里装了 Pillow 就全绿，线上必然挂。

    修法不是去装 Pillow，而是**去掉这个依赖**：PNG 是公开格式，
    1 bit 调色板图只要 zlib + struct 就能拼出来，几十行、零依赖、可断言。
    顺带修掉一个隐性缺陷：原先的 `resize()` 走的是重采样，
    会把 QR 模块的边缘抹糊（QR 必须用最近邻整数倍放大才好扫）。

    `size` 是目标边长（上限）：实际边长取**最接近的整数倍**，
    宁可少几个像素也不做非整数缩放 —— 模块错位比尺寸不整更致命。
    """
    import zlib
    import qrcode

    q = qrcode.QRCode(box_size=1, border=2,
                      error_correction=qrcode.constants.ERROR_CORRECT_M)
    q.add_data(text)
    q.make(fit=True)
    m = q.get_matrix()
    n = len(m)
    # ★ 每个模块至少 MIN_MODULE_PX 像素。密集码（版本 15 以上，如很长的收款链接）
    #   在 320px 目标下只能分到 3–4 px/模块，实测那正是扫码器最容易失败的临界带。
    #   宁可输出大一点的图，也不要给用户一张"看着清楚、就是扫不出来"的二维码。
    #   （PHP 版 php/src/Qr.php 用同一常量、同一策略。）
    scale = max(6, int(size) // n)
    dim = n * scale

    # 逐行拼原始扫描线：每行开头一个 filter type（0 = None），
    # 后面是 1 bit/像素、**高位在前**的调色板索引。
    raw = bytearray()
    rowbytes = (dim + 7) // 8
    for y in range(dim):
        src = m[y // scale]
        raw.append(0)
        line = bytearray(rowbytes)
        for x in range(dim):
            # 调色板 0 = 深色模块（#0f172a），1 = 浅色底（#ffffff）
            if not src[x // scale]:
                line[x >> 3] |= 0x80 >> (x & 7)
        raw += line

    ihdr = _pc_ihdr(dim, dim)
    plte = bytes((0x0F, 0x17, 0x2A, 0xFF, 0xFF, 0xFF))
    return (_PNG_SIG
            + _png_chunk(b"IHDR", ihdr)
            + _png_chunk(b"PLTE", plte)
            + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + _png_chunk(b"IEND", b""))
