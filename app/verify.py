"""
注册邮箱验证。

设计：
    - 令牌为高熵随机串，库中只存 SHA256 摘要（库泄露也无法直接使用）
    - 一次性消费：验证成功后立刻置 used，防止链接被重放
    - 带有效期，可重发（重发使旧令牌失效）
    - 用户表 email_verified 标记；未验证账号不能登录（若站点开启强制验证）
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import os
import sqlite3
import time

from . import dbutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "users.db")

# 令牌明文长度（hex 字符数）
TOKEN_BYTES = 32

_SCHEMA = """
CREATE TABLE IF NOT EXISTS email_verifications (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT NOT NULL UNIQUE,
    username  TEXT NOT NULL,
    email     TEXT NOT NULL,
    created   REAL NOT NULL,
    expires   REAL NOT NULL,
    used      INTEGER NOT NULL DEFAULT 0,
    used_at   REAL,
    ip        TEXT
);
CREATE INDEX IF NOT EXISTS idx_ev_user ON email_verifications(username);
CREATE INDEX IF NOT EXISTS idx_ev_hash ON email_verifications(token_hash);

CREATE TABLE IF NOT EXISTS email_send_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    purpose  TEXT NOT NULL,
    to_addr  TEXT NOT NULL,
    username TEXT,
    ok       INTEGER NOT NULL,
    detail   TEXT,
    created  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_esl_created ON email_send_log(created DESC);
"""


def _db():
    """连接工厂收口：`with _db() as c:` = 提交 + 关闭。

    ★ 原来这里返回的是原生 sqlite3.Connection —— 它的上下文管理器
      **只 commit/rollback，并不 close**，连接要等对象被回收才释放；
      而连接处在循环引用里（connection ⇄ 游标/语句缓存），只能靠分代 GC。
      实测（关掉 GC 模拟负载窗口）：300 次调用泄漏 **1200 个 fd**（≈4/次）。
      默认 ulimit -n 1024 下约 256 次请求即耗尽 fd，此后整个进程
      连数据库都打不开 —— 属于**进程级拒绝服务**。
      统一改用 dbutil.ClosingConnection，语义不变（仍是 `with ... as c:`）。
    """
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
def issue(username: str, email: str, ttl_hours: float = 24, ip: str = "") -> str:
    """签发验证令牌，返回明文令牌（仅此一次可见）。旧令牌立即作废。"""
    init()
    token = secrets.token_hex(TOKEN_BYTES)
    now = time.time()
    with _db() as c:
        c.execute("UPDATE email_verifications SET used=1, used_at=? "
                  "WHERE username=? AND used=0", (now, username))
        c.execute("INSERT INTO email_verifications"
                  "(token_hash,username,email,created,expires,used,ip) "
                  "VALUES(?,?,?,?,?,0,?)",
                  (_hash(token), username, email, now, now + ttl_hours * 3600, ip))
    return token


def check(token: str) -> tuple[bool, str, str]:
    """返回 (是否有效, 用户名, 原因)。不消费令牌。"""
    if not token or len(token) < 16:
        return False, "", "令牌格式不正确"
    init()
    with _db() as c:
        row = c.execute("SELECT * FROM email_verifications WHERE token_hash=?",
                        (_hash(token),)).fetchone()
    if not row:
        return False, "", "验证链接无效"
    if row["used"]:
        return False, "", "该验证链接已被使用"
    if row["expires"] < time.time():
        return False, row["username"], "验证链接已过期，请重新发送"
    return True, row["username"], ""


def consume(token: str) -> tuple[bool, str, str]:
    """校验并消费令牌，同时把用户置为已验证。

    原子性：原来「先 check（SELECT）→ 再 UPDATE used=1」是两个事务，
    并发点击同一链接时两侧都能通过 check，令牌实际被消费两次。
    这里改成 `UPDATE ... WHERE used=0` 抢占，以 rowcount 判定这次是否由我消费。
    """
    ok, username, why = check(token)
    if not ok:
        return False, username, why
    now = time.time()
    with _db() as c:
        cur = c.execute("UPDATE email_verifications SET used=1, used_at=? "
                        "WHERE token_hash=? AND used=0", (now, _hash(token)))
        if not cur.rowcount:
            return False, username, "验证链接已被使用，请重新发送"
        c.execute("UPDATE users SET email_verified=1 WHERE username=?", (username,))
    return True, username, ""


def invalidate(username: str):
    """使用户所有未用令牌作废（改邮箱、封禁、删除时调用）。"""
    init()
    with _db() as c:
        c.execute("UPDATE email_verifications SET used=1, used_at=? "
                  "WHERE username=? AND used=0", (time.time(), username))


def pending(username: str) -> dict | None:
    init()
    with _db() as c:
        row = c.execute("SELECT * FROM email_verifications WHERE username=? "
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


def mark_verified(username: str, verified: bool = True):
    init()
    with _db() as c:
        c.execute("UPDATE users SET email_verified=? WHERE username=?",
                  (1 if verified else 0, username))


def is_verified(username: str) -> bool:
    with _db() as c:
        row = c.execute("SELECT email_verified FROM users WHERE username=?",
                        (username,)).fetchone()
    return bool(row and row["email_verified"])


# --------------------------------------------------------------------------- #
def log_send(purpose: str, to_addr: str, username: str, ok: bool, detail: str = ""):
    init()
    with _db() as c:
        c.execute("INSERT INTO email_send_log(purpose,to_addr,username,ok,detail,created) "
                  "VALUES(?,?,?,?,?,?)",
                  (purpose, to_addr, username, 1 if ok else 0, detail[:300], time.time()))


def send_log(limit: int = 50) -> list[dict]:
    init()
    with _db() as c:
        rows = c.execute("SELECT * FROM email_send_log ORDER BY id DESC LIMIT ?",
                         (limit,)).fetchall()
    return [dict(r) for r in rows]


def stats() -> dict:
    init()
    now = time.time()
    with _db() as c:
        total = c.execute("SELECT COUNT(*) FROM email_verifications").fetchone()[0]
        used = c.execute("SELECT COUNT(*) FROM email_verifications WHERE used=1").fetchone()[0]
        alive = c.execute("SELECT COUNT(*) FROM email_verifications WHERE used=0 AND expires>?",
                          (now,)).fetchone()[0]
        expired = c.execute("SELECT COUNT(*) FROM email_verifications WHERE used=0 AND expires<=?",
                            (now,)).fetchone()[0]
        sent = c.execute("SELECT COUNT(*) FROM email_send_log WHERE ok=1").fetchone()[0]
        failed = c.execute("SELECT COUNT(*) FROM email_send_log WHERE ok=0").fetchone()[0]
        unverified = c.execute("SELECT COUNT(*) FROM users WHERE email_verified=0").fetchone()[0]
    return {"total": total, "used": used, "active": alive, "expired": expired,
            "sent_ok": sent, "sent_failed": failed, "unverified_users": unverified}


def cleanup(days: int = 30) -> int:
    init()
    cutoff = time.time() - days * 86400
    with _db() as c:
        n = c.execute("DELETE FROM email_verifications WHERE created < ?", (cutoff,)).rowcount
    return n


def build_link(base_url: str, token: str) -> str:
    base = (base_url or "").rstrip("/")
    return f"{base}/verify/?token={token}"
