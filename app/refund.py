"""
赞助退款：用户提交申请（填理由）→ 管理员审核（通过/驳回）。

设计要点：

* 只有「已支付」的订单可申请退款，且一笔订单同时只能有一个进行中的申请。
* 退款走真实网关 alipay.trade.refund；网关失败时**绝不**把申请标成已退款，
  如实记下失败原因供管理员重试或转入线下退款。
* 审核通过即回收对应天数的赞助权益（按订单 days 回退，到期时间不早于当前时刻）。
"""
import json
import time

from . import pay


# --------------------------------------------------------------------------- #
# 表结构
# --------------------------------------------------------------------------- #
SCHEMA = """
CREATE TABLE IF NOT EXISTS refund_requests(
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    out_trade_no  TEXT NOT NULL,
    username      TEXT NOT NULL,
    amount        REAL NOT NULL,
    reason        TEXT NOT NULL,
    detail        TEXT,
    status        TEXT NOT NULL DEFAULT 'pending',
    created       REAL NOT NULL,
    reviewed_at   REAL,
    reviewer      TEXT,
    review_note   TEXT,
    trade_no      TEXT,
    refund_amount REAL,
    gateway_raw   TEXT
);
CREATE INDEX IF NOT EXISTS idx_refund_status ON refund_requests(status);
CREATE INDEX IF NOT EXISTS idx_refund_order  ON refund_requests(out_trade_no);
"""

STATUS_TEXT = {
    "pending": "待审核", "processing": "退款中", "approved": "已退款",
    "rejected": "已驳回", "failed": "退款失败",
}


def init(conn):
    """建表（幂等）。"""
    conn.executescript(SCHEMA)


# --------------------------------------------------------------------------- #
# 查询
# --------------------------------------------------------------------------- #
def list_for_user(conn, username: str, limit: int = 50):
    rows = conn.execute(
        "SELECT * FROM refund_requests WHERE username=? ORDER BY id DESC LIMIT ?",
        (username, limit)).fetchall()
    return [_view(r) for r in rows]


def list_all(conn, status: str = "", limit: int = 500):
    sql, args = "SELECT * FROM refund_requests WHERE 1=1", []
    if status:
        sql += " AND status=?"
        args.append(status)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    return [_view(r) for r in conn.execute(sql, args).fetchall()]


def stats(conn):
    out = {}
    for r in conn.execute("SELECT status, COUNT(*) n, COALESCE(SUM(amount),0) amt "
                          "FROM refund_requests GROUP BY status"):
        out[r["status"]] = {"count": r["n"], "amount": round(r["amt"], 2)}
    return out


def _view(row) -> dict:
    d = dict(row)
    d["status_text"] = STATUS_TEXT.get(d.get("status") or "", d.get("status") or "")
    return d


def get(conn, rid: int):
    r = conn.execute("SELECT * FROM refund_requests WHERE id=?", (rid,)).fetchone()
    return _view(r) if r else None


# --------------------------------------------------------------------------- #
# 用户提交
# --------------------------------------------------------------------------- #
MIN_REASON = 5
MAX_REASON = 500


def can_apply(conn, username: str, out_trade_no: str) -> tuple:
    """返回 (ok, 原因)。校验订单归属、可退状态、无重复申请。"""
    o = conn.execute("SELECT * FROM pay_orders WHERE out_trade_no=?",
                     (out_trade_no,)).fetchone()
    if not o:
        return False, "订单不存在"
    o = dict(o)
    if o["username"] != username:
        return False, "无权操作该订单"
    if o["status"] not in ("paid", "paid_pending"):
        return False, "该订单未支付或已退款，无法申请"
    # ★ 必须含 'processing'：网关退款在途（或进程崩溃后卡在 processing）时，
    #   若允许再提交，一旦首条随后变 failed，就会出现两条待审申请 →
    #   管理员都批准 → 同一笔订单退款两次（真实资金损失）。
    #   PHP 侧 (ApiController) 一直是三个状态，Python 漏了 processing —— 两版对齐。
    dup = conn.execute(
        "SELECT id FROM refund_requests WHERE out_trade_no=? AND status IN "
        "('pending','processing','approved')", (out_trade_no,)).fetchone()
    if dup:
        return False, "该订单已有退款申请，请勿重复提交"
    return True, ""


def create(conn, username: str, out_trade_no: str, reason: str,
           detail: str = "") -> dict:
    now = time.time()
    cur = conn.execute(
        "INSERT INTO refund_requests(out_trade_no,username,amount,reason,detail,"
        "status,created) VALUES(?,?,?,?,?,'pending',?)",
        (out_trade_no, username, 0.0, reason, detail, now))
    rid = cur.lastrowid
    # 金额取订单金额，避免前端伪造
    conn.execute("UPDATE refund_requests SET amount=(SELECT amount FROM pay_orders "
                 "WHERE out_trade_no=?) WHERE id=?", (out_trade_no, rid))
    conn.commit()
    return get(conn, rid) or {}


# --------------------------------------------------------------------------- #
# 管理员审核
# --------------------------------------------------------------------------- #
def approve(conn, rid: int, reviewer: str, note: str = "",
            do_gateway: bool = True) -> dict:
    """同意退款：调真实网关退款 + 回收赞助权益。

    网关退款失败时状态记为 failed，不回收权益，保留原始错误供排查。
    """
    r = get(conn, rid)
    if not r:
        return {"ok": False, "error": "申请不存在"}
    if r["status"] != "pending":
        return {"ok": False, "error": f"该申请已处理（{r['status_text']}）"}

    # 原子抢占：把 pending 推进到 processing，再发放退款。
    # 原实现「先读状态 → 调网关 → 再写状态」，两次点击/并发请求会调用网关两次，
    # 造成重复退款（真实资金损失）。
    cur = conn.execute("UPDATE refund_requests SET status='processing' "
                       "WHERE id=? AND status='pending'", (rid,))
    claimed = cur.rowcount
    conn.commit()   # 显式提交；**不能**用 `with conn:` —— 
    # ClosingConnection.__exit__ 会关闭连接，块内后续语句会炸（见 tools/verify_refund_safety.py）
    if not claimed:
        return {"ok": False, "error": "该申请正在处理或已被处理，请刷新后查看"}

    o = conn.execute("SELECT * FROM pay_orders WHERE out_trade_no=?",
                     (r["out_trade_no"],)).fetchone()
    if not o:
        conn.execute("UPDATE refund_requests SET status='pending' WHERE id=?", (rid,))
        conn.commit()   # 显式提交；**不能**用 `with conn:` —— 
        # ClosingConnection.__exit__ 会关闭连接，块内后续语句会炸（见 tools/verify_refund_safety.py）
        return {"ok": False, "error": "关联订单不存在"}
    o = dict(o)

    # ★ 调网关前复查订单**当前**状态：同一订单可能存在两条申请（在途时又提交），
    #   第二条会把已退款的订单再退一次。订单状态是最后一道闸。
    if o["status"] not in ("paid", "paid_pending"):
        conn.execute(
            "UPDATE refund_requests SET status='failed', reviewed_at=?, reviewer=?, "
            "review_note=? WHERE id=? AND status='processing'",
            (time.time(), reviewer,
             f"关联订单状态为 {o['status']}，不是可退款状态（可能已退过款）", rid))
        conn.commit()   # 显式提交；**不能**用 `with conn:` —— 
        # ClosingConnection.__exit__ 会关闭连接，块内后续语句会炸（见 tools/verify_refund_safety.py）
        return {"ok": False, "status": "failed",
                "error": f"关联订单状态为 {o['status']}，无法退款（该订单可能已经退过款）"}

    gw = {"ok": False, "error": "未调用网关（线下退款）"}
    if do_gateway:
        if not pay.is_configured():
            conn.execute("UPDATE refund_requests SET status='pending' WHERE id=?", (rid,))
            conn.commit()   # 显式提交；**不能**用 `with conn:` —— 
            # ClosingConnection.__exit__ 会关闭连接，块内后续语句会炸（见 tools/verify_refund_safety.py）
            return {"ok": False, "error": "未配置支付宝密钥，无法自动退款"}
        gw = pay.refund(r["out_trade_no"], f"{float(o['amount']):.2f}",
                        reason=r["reason"] or "用户申请退款",
                        out_request_no=f"RF{rid}")
        if not gw.get("ok"):
            conn.execute(
                "UPDATE refund_requests SET status='failed', reviewed_at=?, "
                "reviewer=?, review_note=?, gateway_raw=? WHERE id=?",
                (time.time(), reviewer, note,
                 json.dumps({"error": gw.get("error"),
                             "data": gw.get("data")}, ensure_ascii=False)[:4000], rid))
            conn.commit()   # 显式提交；**不能**用 `with conn:` —— 
            # ClosingConnection.__exit__ 会关闭连接，块内后续语句会炸（见 tools/verify_refund_safety.py）
            return {"ok": False, "error": gw.get("error") or "网关退款失败",
                    "status": "failed"}

    now = time.time()
    refund_amount = float(o["amount"])
    conn.execute(
        "UPDATE refund_requests SET status='approved', reviewed_at=?, reviewer=?, "
        "review_note=?, trade_no=?, refund_amount=?, gateway_raw=? WHERE id=?",
        (now, reviewer, note, o.get("trade_no") or "", refund_amount,
         json.dumps({"ok": gw.get("ok"), "error": gw.get("error"),
                     "fund_change": gw.get("fund_change")},
                    ensure_ascii=False)[:4000], rid))
    conn.execute("UPDATE pay_orders SET status='refunded' WHERE out_trade_no=?",
                 (r["out_trade_no"],))
    conn.commit()   # 显式提交；**不能**用 `with conn:` —— 
    # ClosingConnection.__exit__ 会关闭连接，块内后续语句会炸（见 tools/verify_refund_safety.py）
    _revoke_sponsor(conn, o["username"], int(o.get("days") or 0))
    return {"ok": True, "status": "approved", "refund_amount": refund_amount,
            "fund_change": gw.get("fund_change")}


def reject(conn, rid: int, reviewer: str, note: str = "") -> dict:
    r = get(conn, rid)
    if not r:
        return {"ok": False, "error": "申请不存在"}
    if r["status"] != "pending":
        return {"ok": False, "error": f"该申请已处理（{r['status_text']}）"}
    cur = conn.execute("UPDATE refund_requests SET status='rejected', reviewed_at=?, "
                       "reviewer=?, review_note=? WHERE id=? AND status='pending'",
                       (time.time(), reviewer, note, rid))
    if not cur.rowcount:
        return {"ok": False, "error": "该申请已被处理，请刷新后查看"}
    conn.commit()   # 显式提交；**不能**用 `with conn:` —— 
    # ClosingConnection.__exit__ 会关闭连接，块内后续语句会炸（见 tools/verify_refund_safety.py）
    return {"ok": True, "status": "rejected"}


def _revoke_sponsor(conn, username: str, days: int):
    """回收该订单对应的赞助天数；到期时间不早于当前时刻。"""
    row = conn.execute("SELECT sponsor, sponsor_until FROM users WHERE username=?",
                       (username,)).fetchone()
    if not row:
        return
    now = time.time()
    cur = row["sponsor_until"] or 0
    if cur <= now:
        return
    new_until = max(now, cur - days * 86400)
    if new_until <= now + 1:
        conn.execute("UPDATE users SET sponsor=0, sponsor_until=NULL, "
                     "sponsor_tier='' WHERE username=?", (username,))
    else:
        conn.execute("UPDATE users SET sponsor_until=? WHERE username=?",
                     (new_until, username))

    conn.commit()   # 显式提交；**不能**用 `with conn:` —— 
    # ClosingConnection.__exit__ 会关闭连接，块内后续语句会炸（见 tools/verify_refund_safety.py）