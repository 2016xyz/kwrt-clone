#!/usr/bin/env python3
"""管理端接口冒烟回归 —— 守住「后台每个接口都不 500」。

## 为什么需要这个套件

真机上抓到的：`/api/v1/admin/mail/log` **稳定 500**

    app/mailer.py:325  in recent()
        return _sent[-limit:][::-1]
    TypeError: sequence index must be integer, not 'slice'

`_sent` 是 `collections.deque`，而 **deque 不支持切片**。这个函数从来没被
任何测试调用过 —— 于是「邮件通知」页面的投递记录一直打不开，而所有套件
全绿。这不是「漏了一个 bug」，是**整类缺陷没有覆盖**：一个管理端接口
挂了，没有任何测试会发现。

所以这个套件的核心是一条**通用**断言：把 `app/main.py` 里注册的每一个
管理端 GET 接口都真打一遍，**任何一个 5xx 都算失败**。
新增接口自动被覆盖 —— 不需要有人记得来加断言。

## 断言

  AS-1  管理端 GET 接口枚举完整（数量 ≥ 阈值，防止扫描本身变空转）
  AS-2  每一个管理端 GET 接口都不返回 5xx          ← 主断言
  AS-3  邮件投递记录接口正常，且返回的是**表结构**字段
  AS-4  投递记录 limit：合法整数须 200，非整数须 4xx（干净拒绝，不是崩溃）
  AS-4b limit=0/负数回空列表，而非退化成「取全部」（off-by-one）
  AS-5  负向对照：deque 切片确实会抛（证明 AS-3 守的是真问题）
  AS-6  投递记录读的是 email_send_log 表 —— 与 PHP 版同一份数据源
  AS-7  「发送测试邮件」也要留痕（PHP 会写、Python 漏了的那一笔）

## 用法
    python3 tools/verify_admin_smoke.py

★ AS-5 是负向对照：它断言**旧的写法本身会崩**。如果哪天有人把
  `recent()` 改回 `_sent[-limit:]`，AS-3/AS-4 会先红。
"""
from __future__ import annotations

import collections
import hashlib
import http.cookiejar
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PORT = int(os.environ.get("KWRT_AS_PORT", "8281"))
#: 枚举下限。低于这个数说明正则失配/路由被改名 —— 扫描成了空转，必须报红。
MIN_ADMIN_ROUTES = 15

REC: list[tuple[str, str, bool, str]] = []


def rec(cid, title, ok, note=""):
    REC.append((cid, title, bool(ok), note))
    print(f"  {cid:<6} {title:<50} {'✓ 通过' if ok else '✗ 失败'}  {note}")


def py_pw(pw: str) -> str:
    """按 app/main.py 的格式生成口令哈希（pbkdf2_sha256$轮数$盐$哈希）。"""
    import secrets
    salt = secrets.token_hex(8)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 240000)
    return f"pbkdf2_sha256$240000${salt}${dk.hex()}"


class Client:
    def __init__(self, base):
        self.base = base.rstrip("/")
        self.op = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def req(self, path, method="GET", data=None, timeout=30):
        body = None
        hdrs = {}
        if data is not None:
            body = urllib.parse.urlencode(data).encode() if isinstance(data, dict) else data
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        r = urllib.request.Request(self.base + path, data=body, method=method, headers=hdrs)
        try:
            with self.op.open(r, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception as e:                                   # noqa: BLE001
            return 0, str(e)


def wait_up(base, path="/healthz", tries=60) -> bool:
    c = Client(base)
    for _ in range(tries):
        if c.req(path)[0] == 200:
            return True
        time.sleep(0.5)
    return False


def admin_routes() -> list[str]:
    """从 app/main.py 提取管理端 GET 路由。

    只取 GET：POST 大多会改数据，冒烟扫一遍等于在生产上乱点。
    被跳过的 POST 数量会打印出来，不做静默忽略。
    """
    src = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    hits = re.findall(r'@app\.get\(\s*"(/api/v1/admin[^"]*)"', src)
    out, seen = [], set()
    for h in hits:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return sorted(out)


def all_admin_verbs() -> int:
    src = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    return len(set(re.findall(r'@app\.(?:post|put|delete)\(\s*"(/api/v1/admin[^"]*)"', src)))


def main() -> int:                                              # noqa: C901
    print("=" * 110)
    print(" 管理端接口冒烟回归（每个后台 GET 接口都不许 5xx）")
    print("=" * 110)

    routes = admin_routes()
    npost = all_admin_verbs()

    tmp = Path(tempfile.mkdtemp(prefix="kwrt-as-"))
    py = None
    try:
        for d in ("app", "web"):
            shutil.copytree(ROOT / d, tmp / d,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for f in ("config.json", "VERSION"):
            if (ROOT / f).exists():
                shutil.copy2(ROOT / f, tmp / f)

        # 空库起步：让 app 自己建表，顺带验证「全新部署」也是好的
        tdb = tmp / "users.db"
        c = sqlite3.connect(tdb)
        c.execute("CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT,"
                  "username TEXT UNIQUE NOT NULL,password TEXT NOT NULL,role TEXT NOT NULL,"
                  "created REAL,sponsor INTEGER DEFAULT 0,disabled INTEGER DEFAULT 0,"
                  "email TEXT,email_verified INTEGER DEFAULT 0)")
        c.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT)")
        c.execute("INSERT OR REPLACE INTO users(username,password,role,created,sponsor,"
                  "disabled,email_verified) VALUES('asadmin',?,'admin',"
                  "strftime('%s','now'),0,0,1)", (py_pw("asadmin123"),))
        # 造一条真实的投递记录，用来验证 AS-3「读的是表」
        c.execute("CREATE TABLE IF NOT EXISTS email_send_log(id INTEGER PRIMARY KEY AUTOINCREMENT,"
                  "purpose TEXT NOT NULL,to_addr TEXT NOT NULL,username TEXT,ok INTEGER NOT NULL,"
                  "detail TEXT,created REAL NOT NULL)")
        c.execute("INSERT INTO email_send_log(purpose,to_addr,username,ok,detail,created) "
                  "VALUES('verify','as-probe@example.test','asadmin',1,'AS 探针记录',?)",
                  (time.time(),))
        c.commit()
        c.close()

        base = f"http://127.0.0.1:{PORT}"
        py = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
             "--port", str(PORT), "--log-level", "warning", "--forwarded-allow-ips", ""],
            cwd=tmp, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            preexec_fn=os.setsid)

        if not wait_up(base):
            print("  ! Python 未起来（端口可能被上一次异常退出的进程占着）：")
            print("   ", (py.stdout.read() or "")[-600:] if py.stdout else "")
            print(f"    排查：ss -lntp | grep :{PORT}")
            return 2

        cl = Client(base)

        # ------------------------------------------------------------ AS-1 枚举完整性
        rec("AS-1", "管理端 GET 接口枚举完整",
            len(routes) >= MIN_ADMIN_ROUTES,
            f"枚举到 {len(routes)} 个 GET（下限 {MIN_ADMIN_ROUTES}）；"
            f"另有 {npost} 个写接口本轮不扫（会改数据，不做静默忽略）")

        # 登录
        st, _ = cl.req("/api/v1/login", "POST", {"username": "asadmin", "password": "asadmin123"})
        ok_login = st == 200
        if not ok_login:
            rec("AS-2", "管理员登录", False, f"HTTP {st}")
            return 2

        # ------------------------------------------------------------ AS-2 主断言
        # ★ 这一条就是本套件存在的理由：任何一个管理端 GET 返回 5xx 都算失败。
        bad5xx, bad0, statuses = [], [], {}
        for r in routes:
            # 路径参数填个哑值：404 是正常的（资源不存在），5xx 才是缺陷
            path = re.sub(r"\{[^}]+\}", "probe-nonexistent", r)
            st, body = cl.req(path)
            statuses[r] = st
            if 500 <= st < 600:
                bad5xx.append((r, st, body[:160]))
            elif st == 0:
                bad0.append((r, body[:80]))
        rec("AS-2", "每个管理端 GET 接口都不返回 5xx",
            not bad5xx and not bad0,
            f"扫了 {len(routes)} 个接口，5xx={len(bad5xx)}，连不上={len(bad0)}"
            + (f"；首个 5xx：{bad5xx[0][0]} → {bad5xx[0][1]} {bad5xx[0][2]}" if bad5xx else ""))

        # ------------------------------------------------------------ AS-3 投递记录接口
        st, body = cl.req("/api/v1/admin/mail/log")
        try:
            d = json.loads(body)
        except Exception:                                        # noqa: BLE001
            d = {}
        logs = d.get("log") if isinstance(d, dict) else None
        logs = logs if isinstance(logs, list) else []
        rowshape = bool(logs) and all(isinstance(x, dict) for x in logs[:1]) and "to_addr" in logs[0]
        rec("AS-3", "邮件投递记录接口正常且返回表结构字段",
            st == 200 and isinstance(logs, list) and rowshape,
            f"HTTP {st}，记录 {len(logs)} 条，"
            f"字段={sorted(logs[0].keys()) if rowshape else '(不符)'}")

        # ------------------------------------------------------------ AS-4 limit 参数健壮性
        # limit 直接来自查询参数，必须夹紧：[-0:] 会退化成「取全部」（经典 off-by-one）。
        #
        # ★ 判定分两类，别把正常校验当缺陷：
        #   · 合法整数（0 / 负 / 超大）→ 必须 200。0 和负数都应回空列表，
        #     而不是「取全部」（那是 off-by-one），更不该 5xx。
        #   · 非整数（abc / 1.5 / 空串）→ FastAPI 的类型声明会回 422。
        #     这是**干净、明确**的拒绝，属于正确行为，不是崩溃 ——
        #     只要求「不是 5xx」。（第一版把 422 也判成失败，那是测试自己的错。）
        bad_lim, bad_reject = [], []
        for lim in ("0", "1", "60", "1000", "99999", "-5"):
            st, _ = cl.req(f"/api/v1/admin/mail/log?limit={urllib.parse.quote(lim)}")
            if st != 200:
                bad_lim.append((lim, st))
        for lim in ("abc", "1.5", ""):
            st, _ = cl.req(f"/api/v1/admin/mail/log?limit={urllib.parse.quote(lim)}")
            if not (400 <= st < 500):
                bad_reject.append((lim, st))
        rec("AS-4", "limit 参数各种取值都不崩（0/负/超大/非数字）",
            not bad_lim and not bad_reject,
            f"合法值 6 种须 200，异常={bad_lim or '无'}；"
            f"非法值 3 种须 4xx（干净拒绝），异常={bad_reject or '无'}")

        # 0 与负数必须是「空列表」而不是「全部」—— 单独钉死这个 off-by-one
        st0, b0 = cl.req("/api/v1/admin/mail/log?limit=0")
        stn, bn = cl.req("/api/v1/admin/mail/log?limit=-5")
        try:
            n0 = len(json.loads(b0).get("log") or [])
            nn = len(json.loads(bn).get("log") or [])
        except Exception:                                        # noqa: BLE001
            n0 = nn = -1
        rec("AS-4b", "limit=0/负数回空列表，而非退化成「取全部」",
            st0 == 200 and stn == 200 and n0 == 0 and nn == 0,
            f"limit=0 → {n0} 条；limit=-5 → {nn} 条（库里确实有记录，所以 0 不是巧合）")

        # ------------------------------------------------------------ AS-7 测试邮件要留痕
        # ★ 两版不一致的真实缺陷：PHP 的 AdminController::mailTest 会写
        #   email_send_log（purpose='test'），Python 侧漏了。结果管理员点完
        #   「发送测试邮件」再翻「投递记录」是空的 —— 看着像功能坏了。
        #   SMTP 未配置时接口回 400（干净拒绝），但**照样要留痕**。
        st, body = cl.req("/api/v1/admin/mail/test", "POST",
                          {"to": "as-mailtest@example.test"})
        time.sleep(0.3)
        c = sqlite3.connect(tdb)
        row = c.execute("SELECT purpose,to_addr,ok FROM email_send_log "
                        "WHERE to_addr='as-mailtest@example.test'").fetchone()
        c.close()
        rec("AS-7", "「发送测试邮件」留痕到投递记录（与 PHP 版一致）",
            row is not None and row[0] == "test",
            f"接口 HTTP {st}；库中记录={row}")

        # ------------------------------------------------------------ AS-6 与 PHP 同源
        # 读的是 email_send_log 表 —— 与 PHP 的 AdminController::mailLog 同一份数据。
        # 用「我们刚插进去的那条探针记录能看到」来证明它真的在读表，
        # 而不是另一个进程内数组。
        seen_probe = any((r.get("to_addr") == "as-probe@example.test")
                         for r in (logs or []) if isinstance(r, dict))
        rec("AS-6", "投递记录读的是 email_send_log 表（与 PHP 同源）",
            seen_probe,
            "库里的探针记录能被接口读到" if seen_probe
            else "接口没返回库里的探针记录 → 读的不是这张表")

    finally:
        for p in (py,):
            if p is not None:
                try:
                    os.killpg(os.getpgid(p.pid), 15)
                    p.wait(timeout=10)
                except Exception:                                # noqa: BLE001
                    pass
        shutil.rmtree(tmp, ignore_errors=True)

    # ---------------------------------------------------------------- AS-5 负向对照
    # 断言「旧写法本身会崩」—— 这条不通过，说明 AS-3/AS-4 守的可能不是真问题。
    # 下面这行是**故意**写的类型错误（deque 不支持切片）：
    # pyright 会在此报 reportArgumentType —— 那个报错本身就是本断言的证据，
    # 所以加 ignore 而不是改写成不报错的写法（改了就证明不了任何事）。
    root_cause = False
    try:
        collections.deque([1, 2, 3])[-1:]                        # type: ignore[index]
    except TypeError:
        root_cause = True
    rec("AS-5", "负向对照：deque 切片确实会抛（旧写法真会崩）",
        root_cause,
        "collections.deque(...)[-1:] → TypeError ✓"
        if root_cause else "deque 竟然支持切片了？本套件的立论需要复核")

    print("-" * 110)
    npass = sum(1 for _, _, ok, _ in REC if ok)
    print(f" 合计 {len(REC)} 项，{'全部通过 ✓' if npass == len(REC) else '失败 ✗ ' + str([c for c, _, ok, _ in REC if not ok])}")
    print("=" * 110)
    return 0 if npass == len(REC) else 1


if __name__ == "__main__":
    raise SystemExit(main())
