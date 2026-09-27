"""
密码找回（忘记密码）。

与 verify.py 同源，但**风险更高**（拿到链接即可改口令 → 等同于账号接管），
因此比注册验证更严：

    - 令牌高熵随机，库中只存 SHA256 摘要（库泄露也无法直接使用）
    - 一次性消费：`UPDATE ... WHERE used=0` 抢占，以 rowcount 判定归属
      （先 SELECT 再 UPDATE 是两个事务，并发点击会双花）
    - 有效期**默认 2 小时**（注册验证的 24 小时太长，不适合改口令场景）
    - 每人同时只保留 1 个有效令牌，重新申请即作废旧的
    - 消费成功 = 令牌已烧掉，与「口令是否改成功」解耦：
      宁可让用户重新申请，也不允许同一链接用第二次
    - 发信与消费都留痕（email_send_log.purpose='reset' / admin_logs）

★ 连接一律走 ClosingConnection。
  `sqlite3.Connection` 的上下文管理器**只 commit 不 close**，
  原生 `with conn:` 实测每次调用泄 4 个 fd（verify.py 曾踩此坑，
  1200 fd / 300 次调用），ulimit 1024 下几百次请求即耗尽 → 进程级 DoS。
"""
from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import time

from . import dbutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "users.db")

# 令牌明文长度（hex 字符数）
TOKEN_BYTES = 32

# 申请冷却：同一账号多久内只能申请一次（秒）
COOLDOWN_SECONDS = 60

_SCHEMA = """
CREATE TABLE IF NOT EXISTS password_resets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT NOT NULL UNIQUE,
    username   TEXT NOT NULL,
    email      TEXT NOT NULL,
    created    REAL NOT NULL,
    expires    REAL NOT NULL,
    used       INTEGER NOT NULL DEFAULT 0,
    used_at    REAL,
    ip         TEXT
);
CREATE INDEX IF NOT EXISTS idx_pr_user ON password_resets(username);
CREATE INDEX IF NOT EXISTS idx_pr_hash ON password_resets(token_hash);
CREATE INDEX IF NOT EXISTS idx_pr_created ON password_resets(created DESC);
"""


def _db():
    """连接工厂收口：`with _db() as c:` = 提交 + 关闭（见 dbutil 的说明）。"""
    c = sqlite3.connect(DB, timeout=15, factory=dbutil.ClosingConnection)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init():
    with _db() as c:
        c.executescript(_SCHEMA)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# --------------------------------------------------------------------------- #
def issue(username: str, email: str, ttl_hours: float = 2, ip: str = "") -> str:
    """签发找回令牌，返回明文令牌（仅此一次可见）。旧令牌立即作废。"""
    init()
    token = secrets.token_hex(TOKEN_BYTES)
    now = time.time()
    with _db() as c:
        c.execute("UPDATE password_resets SET used=1, used_at=? "
                  "WHERE username=? AND used=0", (now, username))
        c.execute("INSERT INTO password_resets"
                  "(token_hash,username,email,created,expires,used,ip) "
                  "VALUES(?,?,?,?,?,0,?)",
                  (_hash(token), username, email, now, now + ttl_hours * 3600, ip))
    return token


def check(token: str) -> tuple[bool, str, str]:
    """返回 (是否有效, 用户名, 原因)。**不消费**令牌。"""
    if not token or len(token) < 16:
        return False, "", "链接格式不正确"
    init()
    with _db() as c:
        row = c.execute("SELECT * FROM password_resets WHERE token_hash=?",
                        (_hash(token),)).fetchone()
    if not row:
        return False, "", "重置链接无效"
    if row["used"]:
        return False, "", "该重置链接已被使用"
    if row["expires"] < time.time():
        return False, row["username"], "重置链接已过期，请重新申请"
    return True, row["username"], ""


def consume(token: str) -> tuple[bool, str, str]:
    """校验并**原子消费**令牌。成功即令牌作废，返回 (True, 用户名, "")。

    并发点两下同一链接时，只有第一方能拿到 rowcount=1，
    另一方得到明确提示而不是「两边都改了口令」。
    """
    ok, username, why = check(token)
    if not ok:
        return False, username, why
    with _db() as c:
        cur = c.execute("UPDATE password_resets SET used=1, used_at=? "
                        "WHERE token_hash=? AND used=0", (time.time(), _hash(token)))
        if not cur.rowcount:
            return False, username, "该重置链接已被使用，请重新申请"
    return True, username, ""


def invalidate(username: str):
    """作废该用户所有未用令牌（改密成功后、封禁、删除时调用）。"""
    init()
    with _db() as c:
        c.execute("UPDATE password_resets SET used=1, used_at=? "
                  "WHERE username=? AND used=0", (time.time(), username))


def pending(username: str) -> dict | None:
    init()
    with _db() as c:
        row = c.execute("SELECT * FROM password_resets WHERE username=? "
                        "AND used=0 ORDER BY id DESC LIMIT 1", (username,)).fetchone()
    if not row:
        return None
    return {
        "email": row["email"],
        "created": row["created"],
        "expires": row["expires"],
        "expired": row["expires"] < time.time(),
        "remaining_hours": max(0.0, (row["expires"] - time.time()) / 3600),
    }


def seconds_since_last(username: str) -> float | None:
    """距上次申请过了多少秒（用于冷却判定）。从未申请过返回 None。"""
    init()
    with _db() as c:
        row = c.execute("SELECT created FROM password_resets WHERE username=? "
                        "ORDER BY id DESC LIMIT 1", (username,)).fetchone()
    return None if not row else max(0.0, time.time() - float(row["created"]))


def cooldown_left(username: str) -> int:
    """冷却剩余秒数，0 表示可以申请。"""
    age = seconds_since_last(username)
    if age is None:
        return 0
    return max(0, int(COOLDOWN_SECONDS - age))


def build_link(base_url: str, token: str) -> str:
    base = (base_url or "").rstrip("/")
    return f"{base}/reset/?token={token}"


def stats() -> dict:
    init()
    now = time.time()
    with _db() as c:
        total = c.execute("SELECT COUNT(*) FROM password_resets").fetchone()[0]
        used = c.execute("SELECT COUNT(*) FROM password_resets WHERE used=1").fetchone()[0]
        alive = c.execute("SELECT COUNT(*) FROM password_resets WHERE used=0 AND expires>?",
                          (now,)).fetchone()[0]
        expired = c.execute("SELECT COUNT(*) FROM password_resets WHERE used=0 AND expires<=?",
                            (now,)).fetchone()[0]
        last24 = c.execute("SELECT COUNT(*) FROM password_resets WHERE created>?",
                           (now - 86400,)).fetchone()[0]
    return {"total": total, "used": used, "active": alive,
            "expired": expired, "last_24h": last24}


def cleanup(days: int = 30) -> int:
    """清理历史令牌（已用/过期且超过保留期），返回删除行数。"""
    init()
    cutoff = time.time() - days * 86400
    with _db() as c:
        return c.execute("DELETE FROM password_resets WHERE created < ?", (cutoff,)).rowcount
