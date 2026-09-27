#!/usr/bin/env python3
"""
下载令牌 —— 带有效期的产物下载链接。

设计：
  · 每次构建完成，为产物生成一次性/限时令牌（默认 TTL 由管理员配置）。
  · 令牌可设置最大下载次数、可吊销、可续期。
  · 支持外部下载域名（CDN / 对象存储）—— 令牌仍由本站签发与校验。
  · 令牌用 HMAC 签名，防止伪造（密钥持久化在 settings 表）。

校验路径：GET /dl/t/<token> → 校验签名/过期/次数 → 302 跳转到实际文件或直接流式返回。
"""
import hashlib
import hmac
import os
import re
import secrets
import threading
import sqlite3
import time

from . import dbutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "users.db")
STORE = os.path.join(ROOT, "store")

_SECRET_CACHE = None


def db():
    # 见 app/dbutil.py：with 退出时要「提交 + 关闭」，否则每次调用泄漏 1 个 fd
    c = sqlite3.connect(DB, timeout=20, factory=dbutil.ClosingConnection)
    c.row_factory = sqlite3.Row
    return c


def _init():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS settings(
            key TEXT PRIMARY KEY, value TEXT, updated REAL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS dl_tokens(
            token      TEXT PRIMARY KEY,
            request_hash TEXT,
            username   TEXT,
            filename   TEXT,
            created    REAL,
            expires    REAL,
            max_hits   INTEGER DEFAULT 0,
            hits       INTEGER DEFAULT 0,
            revoked    INTEGER DEFAULT 0,
            last_hit   REAL,
            last_ip    TEXT
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS ix_dltok_hash ON dl_tokens(request_hash)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_dltok_user ON dl_tokens(username)")


_init()


# --------------------------------------------------------------------------- #
# 签名密钥（持久化，重启不变，避免历史链接失效）
_SECRET_LOCK = threading.Lock()
# --------------------------------------------------------------------------- #
def secret():
    """取（或首次生成）下载令牌签名密钥。

    U-5：原来无锁 —— 两个并发首次调用会各自 `token_hex` 生成**不同的**密钥，
    后写者覆盖数据库，但先写者已把自己那份缓存返回；此后用它签发的令牌
    验签必然失败（表现为「令牌突然全部失效」）。
    这里加锁 + 双重检查，并用 INSERT OR IGNORE 保证只有第一个写生效，
    随后一律以库中值为准。
    """
    global _SECRET_CACHE
    if _SECRET_CACHE:
        return _SECRET_CACHE
    with _SECRET_LOCK:
        # 双重检查：等锁期间可能已被别的线程写入
        if _SECRET_CACHE:
            return _SECRET_CACHE
        with db() as c:
            r = c.execute("SELECT value FROM settings WHERE key='dl.secret'").fetchone()
            if r and r["value"]:
                _SECRET_CACHE = r["value"]
                return _SECRET_CACHE
        s = secrets.token_hex(32)
        with db() as c:
            # OR IGNORE：并发下只让第一个写入生效，避免互相覆盖
            c.execute("INSERT OR IGNORE INTO settings(key,value,updated) "
                      "VALUES('dl.secret',?,?)", (s, time.time()))
        # 无条件回读，确保返回的是库中权威值（可能与本次生成的 s 不同）
        with db() as c:
            r = c.execute("SELECT value FROM settings WHERE key='dl.secret'").fetchone()
            if r and r["value"]:
                _SECRET_CACHE = r["value"]
                return _SECRET_CACHE
        _SECRET_CACHE = s
        return s


def _sig(request_hash, filename, expires, nonce):
    msg = f"{request_hash}|{filename}|{int(expires)}|{nonce}".encode()
    return hmac.new(secret().encode(), msg, hashlib.sha256).hexdigest()[:32]


# --------------------------------------------------------------------------- #
# 签发 / 校验
# --------------------------------------------------------------------------- #
def issue(request_hash, filename, username="", ttl_hours=None, max_hits=None):
    """
    为单个产物签发限时下载令牌。返回 token 字符串（URL 安全）。
    """
    from . import dbutil, sitesettings as SS
    if ttl_hours is None:
        ttl_hours = int(SS.get("download.link_ttl_hours") or 72)
    if max_hits is None:
        max_hits = int(SS.get("download.max_hits") or 0)
    now = time.time()
    expires = now + max(1, int(ttl_hours)) * 3600
    nonce = secrets.token_hex(8)
    tok = f"{nonce}.{int(expires)}.{_sig(request_hash, filename, expires, nonce)}"
    with db() as c:
        c.execute("""INSERT OR REPLACE INTO dl_tokens
                     (token,request_hash,username,filename,created,expires,max_hits,hits,revoked)
                     VALUES(?,?,?,?,?,?,?,0,0)""",
                  (tok, request_hash, username, filename, now, expires, int(max_hits)))
    return tok


def issue_many(request_hash, filenames, username="", ttl_hours=None, max_hits=None):
    return {fn: issue(request_hash, fn, username, ttl_hours, max_hits) for fn in filenames}


def verify(token, consume=False, ip=""):
    """
    校验令牌。返回 (ok, info_or_error, row)。
    consume=True 时累加下载次数（用于实际下载）。
    """
    if not token or "." not in token:
        return False, "令牌格式错误", None
    parts = token.split(".")
    if len(parts) != 3:
        return False, "令牌格式错误", None
    nonce, exp_s, sig = parts
    try:
        expires = int(exp_s)
    except ValueError:
        return False, "令牌格式错误", None

    with db() as c:
        row = c.execute("SELECT * FROM dl_tokens WHERE token=?", (token,)).fetchone()
    if not row:
        return False, "令牌不存在", None

    # 签名校验（防篡改）
    if not hmac.compare_digest(_sig(row["request_hash"], row["filename"], expires, nonce), sig):
        return False, "令牌签名无效", row
    if row["revoked"]:
        return False, "令牌已被吊销", row
    if time.time() > row["expires"]:
        return False, "令牌已过期", row
    if row["max_hits"] and row["hits"] >= row["max_hits"]:
        return False, "令牌下载次数已用尽", row

    if consume:
        # 判断与自增必须原子：原实现先读 hits 再自增，并发请求会一起通过
        # 上限校验，实际下载次数可达 max_hits + N - 1。这里把条件写进 WHERE，
        # 用 rowcount 判定是否抢到本次配额。
        with db() as c:
            cur = c.execute(
                "UPDATE dl_tokens SET hits=hits+1, last_hit=?, last_ip=? "
                "WHERE token=? AND revoked=0 AND expires>? "
                "AND (max_hits=0 OR hits<max_hits)",
                (time.time(), ip or "", token, time.time()))
            if cur.rowcount == 0:
                return False, "令牌下载次数已用尽", row
    return True, "ok", row


def revoke(token):
    with db() as c:
        n = c.execute("UPDATE dl_tokens SET revoked=1 WHERE token=?", (token,)).rowcount
    return bool(n)


def revoke_build(request_hash):
    """吊销某次构建的全部分发令牌（删除产物/重新构建时调用）。"""
    with db() as c:
        n = c.execute("UPDATE dl_tokens SET revoked=1 WHERE request_hash=?", (request_hash,)).rowcount
    return n


def revoke_user(username):
    with db() as c:
        n = c.execute("UPDATE dl_tokens SET revoked=1 WHERE username=?", (username,)).rowcount
    return n


def extend(token, extra_hours):
    """续期。

    注意：原实现顺带 `revoked=0` —— 管理员刻意吊销的令牌会被一次「续期」
    静默复活。吊销是明确的安全决策，续期不应把它撤销，故只改 expires。
    """
    with db() as c:
        row = c.execute("SELECT expires FROM dl_tokens WHERE token=?", (token,)).fetchone()
        if not row:
            return False
        c.execute("UPDATE dl_tokens SET expires=? WHERE token=?",
                  (row["expires"] + int(extra_hours) * 3600, token))
    return True


def list_for(request_hash=None, username=None, include_expired=True, limit=500,
             include_token=False):
    sql, args = "SELECT * FROM dl_tokens WHERE 1=1", []
    if request_hash:
        sql += " AND request_hash=?"; args.append(request_hash)
    if username:
        sql += " AND username=?"; args.append(username)
    if not include_expired:
        sql += " AND expires>? AND revoked=0"; args.append(time.time())
    sql += " ORDER BY created DESC LIMIT ?"; args.append(max(1, min(5000, limit)))
    with db() as c:
        rows = [dict(r) for r in c.execute(sql, args)]
    now = time.time()
    for r in rows:
        r["expired"] = now > r["expires"]
        r["remaining"] = max(0, int(r["expires"] - now))
        # 令牌是敏感凭据：默认只给 url，并删除 token 原文，
        # 避免调用方以为"已脱敏"而实际仍把明文返回给终端用户。
        # 需要展示令牌的管理端显式传 include_token=True。
        if include_token:
            r["url"] = "/dl/t/" + r["token"]
        else:
            r.pop("token", None)
            r["url"] = r.get("url") or ""
    return rows


def stats():
    now = time.time()
    with db() as c:
        total = c.execute("SELECT COUNT(*) n FROM dl_tokens").fetchone()["n"]
        active = c.execute("SELECT COUNT(*) n FROM dl_tokens WHERE expires>? AND revoked=0",
                           (now,)).fetchone()["n"]
        expired = c.execute("SELECT COUNT(*) n FROM dl_tokens WHERE expires<=?",
                            (now,)).fetchone()["n"]
        revoked = c.execute("SELECT COUNT(*) n FROM dl_tokens WHERE revoked=1").fetchone()["n"]
        hits = c.execute("SELECT COALESCE(SUM(hits),0) n FROM dl_tokens").fetchone()["n"]
    return {"total": total, "active": active, "expired": expired,
            "revoked": revoked, "hits": hits}


def cleanup_expired(older_than_days=30):
    """清理过期的令牌记录（保留近期便于审计）。"""
    cutoff = time.time() - older_than_days * 86400
    with db() as c:
        n = c.execute("DELETE FROM dl_tokens WHERE expires < ?", (cutoff,)).rowcount
    return n


# 构建任务 ID 的实际形态：md5 前 16 位十六进制（见 builder.submit）
HASH_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def resolve_path(request_hash, filename):
    """令牌对应产物在本地的真实路径（防目录穿越）。

    ⚠ 原实现只做 `os.path.basename` + `!= "uploads"` 判定，但
    `os.path.basename("..")` 返回的正是 `".."`（非空、也不等于 uploads），
    于是 `resolve_path("..", "users.db")` 会得到 `STORE/../users.db` ——
    可借管理端 reissue 签发令牌后下载项目根目录下的 users.db
    （内含 users.password 与 dl.secret，等于泄露令牌签名密钥）。
    这里改为：任务 ID 必须匹配白名单字符集，且最终路径必须落在 STORE 之内。
    """
    h = str(request_hash or "")
    n = os.path.basename(str(filename or ""))
    if not HASH_RE.match(h) or h == "uploads":
        return None
    if not n or n in (".", ".."):
        return None
    base = os.path.realpath(STORE)
    p = os.path.realpath(os.path.join(base, h, n))
    if p != base and not p.startswith(base + os.sep):
        return None
    return p if os.path.isfile(p) else None
