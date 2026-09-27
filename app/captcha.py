"""图形验证码 —— 服务端生成、一次性使用、答案不以明文入库。

设计要点（都是为了「挡自动化」这个目的真的成立）：

1. **答案明文绝不入库**，只存 sha256(salt|code)。数据库被读走也拿不到答案。
2. **一次性**：校验成功或失败即标记 used，重放同一个 id 一律拒绝。
   （否则自动化可以取一张图、试无数次，验证码就白做了。）
3. **有效期**：超过 security.captcha_ttl_min 分钟即失效（默认 5 分钟）。
4. **按 IP 限流**：同一 IP 每分钟最多签发 N 张，防止拿验证码接口当资源放大器。
5. **不依赖 Pillow**：直接输出 SVG。第三方图形库在最小化安装上经常缺，
   缺了就会让「验证码」这个功能整块失效 —— 用纯字符串拼 SVG 最稳。
6. **字符集去混淆**：不含 0/O/1/I/l 这类易混字符，用户输错的概率显著下降。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import random
import secrets
import time

from app import sitesettings as SS

#: 去混淆字符集（去掉 0 O 1 I L）
ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"

#: 单 IP 每分钟最多签发张数
ISSUE_PER_MIN = 30

#: 记录保留时长（秒），过期即清理，避免表无限增长
KEEP_SECONDS = 3600


def enabled() -> bool:
    """登录是否要求验证码。"""
    return _truthy(SS.get("security.captcha_enabled"))


def enabled_for_register() -> bool:
    """注册是否要求验证码（独立开关）。"""
    return _truthy(SS.get("security.captcha_on_register"))


def enabled_for_reset() -> bool:
    """找回密码是否要求验证码（独立开关）。

    默认**开**：这个接口会真实发信，没有验证码就是现成的邮件轰炸放大器
    （遍历用户名即可持续触发）。
    """
    v = SS.get("security.captcha_on_reset")
    return True if v is None else _truthy(v)


def _truthy(v) -> bool:
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def length() -> int:
    try:
        n = int(float(str(SS.get("security.captcha_length") or 4)))
    except (TypeError, ValueError):
        n = 4
    return max(3, min(6, n))


def ttl_seconds() -> int:
    try:
        m = int(float(str(SS.get("security.captcha_ttl_min") or 5)))
    except (TypeError, ValueError):
        m = 5
    return max(1, min(30, m)) * 60


# --------------------------------------------------------------------------- #
# 存储
# --------------------------------------------------------------------------- #

def _ensure_table(c) -> None:
    c.execute("""CREATE TABLE IF NOT EXISTS captchas(
        id TEXT PRIMARY KEY,
        answer_hash TEXT NOT NULL,
        created REAL NOT NULL,
        used INTEGER NOT NULL DEFAULT 0,
        ip TEXT DEFAULT '')""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_captchas_created ON captchas(created)")


def _hash(cid: str, code: str, salt: str) -> str:
    """答案哈希。绑上 id 与安装级盐，避免彩虹表，也防止两题答案相同被看出规律。"""
    return hashlib.sha256(f"{salt}|{cid}|{code.upper()}".encode("utf-8")).hexdigest()


#: 盐缓存（进程内），真源在库里 —— 见 _salt()
_SALT: str | None = None


def _salt(conn) -> str:
    """取得**安装级**的验证码盐。

    ★ 这里必须是持久化的、所有进程共享的盐，不能是进程内随机数。
      原实现用进程内 `secrets.token_hex()`，后果：
        - `uvicorn --workers 4` 时每个 worker 盐不同 →
          用户在 A worker 拿到图、请求落到 B worker 校验 → **随机失败**，
          且表现为「验证码明明输对了却说不对」，极难排查；
        - 服务重启后所有在途验证码一并失效（这个反而是可接受的）。
      真源放在 users.db 的 app_secrets 表，多进程/重启都一致，
      也随数据库一起备份迁移。
    """
    global _SALT
    if _SALT:
        return _SALT
    env = os.environ.get("KWRT_CAPTCHA_SALT")
    if env:
        return env          # 显式覆盖优先（便于测试与多机共享）

    conn.execute("""CREATE TABLE IF NOT EXISTS app_secrets(
        name TEXT PRIMARY KEY, value TEXT NOT NULL, created REAL NOT NULL)""")
    row = conn.execute("SELECT value FROM app_secrets WHERE name='captcha_salt'").fetchone()
    if row and row["value"]:
        _SALT = str(row["value"])
    else:
        _SALT = secrets.token_hex(32)
        conn.execute("INSERT OR REPLACE INTO app_secrets(name, value, created) VALUES(?,?,?)",
                     ("captcha_salt", _SALT, time.time()))
    return _SALT


def _gc(c) -> None:
    """清理过期记录。顺带做，不单独起任务。"""
    c.execute("DELETE FROM captchas WHERE created < ?", (time.time() - KEEP_SECONDS,))


def issue(ip: str = "") -> dict:
    """签发一张验证码。

    :return: {"id":..., "svg":..., "ttl":..., "length":...}
    :raises RuntimeError: 该 IP 签发过于频繁
    """
    from app.main import db  # 延迟导入，避免循环依赖

    n = length()
    code = "".join(random.SystemRandom().choice(ALPHABET) for _ in range(n))
    cid = secrets.token_urlsafe(18)
    now = time.time()

    with db() as c:
        _ensure_table(c)
        _gc(c)
        salt = _salt(c)
        # 限流：同 IP 一分钟内的签发量
        row = c.execute("SELECT COUNT(*) n FROM captchas WHERE ip=? AND created>?",
                        (ip or "", now - 60)).fetchone()
        if row and int(row["n"]) >= ISSUE_PER_MIN:
            raise RuntimeError("验证码请求过于频繁，请稍后再试")
        c.execute("INSERT INTO captchas(id, answer_hash, created, used, ip) VALUES(?,?,?,0,?)",
                  (cid, _hash(cid, code, salt), now, ip or ""))

    return {"id": cid, "svg": _svg(code), "ttl": ttl_seconds(), "length": n}


def verify(cid: str, code: str) -> tuple[bool, str]:
    """校验并**立即作废**该验证码。

    :return: (是否通过, 失败原因)
    """
    from app.main import db

    cid = (cid or "").strip()
    code = (code or "").strip()
    if not cid or not code:
        return False, "请填写验证码"
    if len(code) > 16:
        return False, "验证码不正确"

    now = time.time()
    with db() as c:
        _ensure_table(c)
        row = c.execute("SELECT * FROM captchas WHERE id=?", (cid,)).fetchone()
        if not row:
            return False, "验证码已失效，请刷新后重试"
        # ★ 原子认领：作废必须**带条件**并检查受影响行数。
        #   原实现是无条件 UPDATE，却拿**更新前**读到的 row["used"] 判断 ——
        #   两个并发请求会同时读到 used=0、同时通过，同一张验证码被用两次。
        #   PHP 侧原先有同一处缺陷，一并修掉（两版必须一致）。
        cur = c.execute("UPDATE captchas SET used=1 WHERE id=? AND used=0", (cid,))
        if cur.rowcount != 1:
            return False, "验证码已使用，请刷新后重试"
        if now - float(row["created"]) > ttl_seconds():
            return False, "验证码已过期，请刷新后重试"
        if not hmac.compare_digest(str(row["answer_hash"]), _hash(cid, code, _salt(c))):
            return False, "验证码不正确"
    return True, ""


# --------------------------------------------------------------------------- #
# 图形
# --------------------------------------------------------------------------- #

def _svg(code: str) -> str:
    """把验证码渲染成 SVG。

    纯手写字符串，无第三方图形库依赖。加入噪点、干扰线、字符旋转与
    基线抖动 —— 目的是干扰简单 OCR，不是追求好看的验证码。
    """
    width, height = 132, 44
    rnd = random.SystemRandom()

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="验证码">',
        f'<rect width="{width}" height="{height}" fill="#f3f4f6"/>',
    ]
    # 干扰线
    for _ in range(5):
        x1, y1 = rnd.randint(0, width), rnd.randint(0, height)
        x2, y2 = rnd.randint(0, width), rnd.randint(0, height)
        parts.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
                     f'stroke="#9ca3af" stroke-width="1" opacity="0.6"/>')
    # 噪点
    for _ in range(70):
        parts.append(f'<circle cx="{rnd.randint(0, width)}" cy="{rnd.randint(0, height)}" '
                     f'r="1" fill="#6b7280" opacity="0.5"/>')
    # 字符：每个单独旋转 + 抖动基线
    step = width / (len(code) + 1)
    for i, ch in enumerate(code):
        x = step * (i + 1)
        y = height / 2 + rnd.randint(-5, 5)
        deg = rnd.randint(-28, 28)
        color = rnd.choice(["#111827", "#1f2937", "#374151", "#2563eb", "#0f766e"])
        parts.append(
            f'<text x="{x:.1f}" y="{y:.1f}" font-family="monospace,DejaVu Sans Mono" '
            f'font-size="24" font-weight="700" fill="{color}" '
            f'text-anchor="middle" dominant-baseline="middle" '
            f'transform="rotate({deg} {x:.1f} {y:.1f})">{ch}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)
