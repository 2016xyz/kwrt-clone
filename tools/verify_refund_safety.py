#!/usr/bin/env python3
"""退款安全回归。

## 本轮修掉的两个真缺陷（都是**线上 500**级）

1. `app/refund.py` 在 `with db() as c:` 得到的连接上又写了 `with conn:`
   —— 而 `ClosingConnection.__exit__` 会**关闭**连接（这是为修 fd 泄漏刻意做的）。
   于是退款审批在**生产调用方式下必然抛**
   `sqlite3.ProgrammingError: Cannot operate on a closed database`：
   `approve()` 的原子认领一提交就把连接关了，紧接着第 157 行查订单即炸；
   `_revoke_sponsor()` 更是在已关闭的连接上执行。
   → 赞助退款功能整体不可用。已改为显式 `conn.commit()`（提交但不关闭）。

2. `can_apply()` 的重复申请检查漏了 `processing`，与 PHP 侧
   (`ApiController` 用 'pending','processing','approved') 不一致。
   网关退款在途时仍可再提交；首条随后若变 failed，就会出现两条待审申请，
   管理员都批准即**同一订单退款两次**（真金白银）。

另：`approve()` 调网关前不复查订单状态 —— 已补闸门（订单不是可退状态则拒绝）。

## 用法
    python3 tools/verify_refund_safety.py
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REC: list[tuple[str, str, bool, str]] = []


def rec(cid: str, title: str, ok: bool, note: str = "") -> None:
    REC.append((cid, title, bool(ok), note))
    print(f"  {cid:<6} {title:<56} {'✓ 通过' if ok else '✗ 失败'}  {note}")


PROBE = '''
import sys, os, time
sys.path.insert(0, ".")
os.environ["KWRT_DB"] = os.path.join(os.getcwd(), "users.db")
from app.main import db, init_db
from app import refund
init_db()
now = time.time()

def mk(status, rstatus="pending"):
    with db() as c:
        refund.init(c)
        c.execute("DELETE FROM refund_requests")
        c.execute("DELETE FROM pay_orders")
        c.execute("INSERT OR REPLACE INTO users(username,password,created,sponsor,sponsor_until)"
                  " VALUES('u','x',?,1,?)", (now, now + 30 * 86400))
        c.execute("INSERT INTO pay_orders(out_trade_no,username,tier_name,amount,days,status,"
                  "created,expires) VALUES('P1','u','t',9.99,30,?,?,?)", (status, now, now + 900))
        c.execute("INSERT INTO refund_requests(out_trade_no,username,amount,reason,status,created)"
                  " VALUES('P1','u',9.99,'r',?,?)", (rstatus, now))
        return c.execute("SELECT id FROM refund_requests").fetchone()[0]

# RF-1 正常路径：原先必抛 Closed database
rid = mk("paid")
try:
    with db() as c:
        res = refund.approve(c, rid, "admin", "n", do_gateway=False)
    with db() as c:
        st = refund.get(c, rid)["status"]
    with db() as c:
        until = c.execute("SELECT sponsor_until FROM users WHERE username='u'").fetchone()[0]
    print("RF1 ok=%s st=%s expired=%s" % (res.get("ok"), st, until is None or until <= now + 1))
except Exception as e:
    print("RF1 EXC %s: %s" % (type(e).__name__, e))

# RF-2 已退款订单必须被拒（调网关前的闸门）
rid = mk("refunded")
try:
    with db() as c:
        res = refund.approve(c, rid, "admin", "n", do_gateway=False)
    with db() as c:
        st = refund.get(c, rid)["status"]
    print("RF2 ok=%s st=%s" % (res.get("ok"), st))
except Exception as e:
    print("RF2 EXC %s: %s" % (type(e).__name__, e))

# RF-3 processing 在途时不可再申请
mk("paid", rstatus="processing")
with db() as c:
    ok, why = refund.can_apply(c, "u", "P1")
print("RF3 can_apply=%s" % ok)
'''

#: app/ 下禁止在 db() 连接上再开 with 上下文（__exit__ 会关连接）
BAD_CTX = re.compile(r"^\s*with\s+(conn|c\d?|c)\s*:", re.M)


def main() -> int:
    print("=" * 108)
    print(" 退款安全回归")
    print("=" * 108)

    # ---------------------------------------------------------- 静态守卫
    offenders = []
    for f in sorted((ROOT / "app").glob("*.py")):
        src = f.read_text(encoding="utf-8")
        for m in BAD_CTX.finditer(src):
            line = src[: m.start()].count("\n") + 1
            offenders.append(f"{f.name}:{line}")
    rec("RF-4", "静态守卫：app/ 下无 `with conn:`（会关闭连接）", not offenders,
        "; ".join(offenders) if offenders else "已全部改为显式 commit()")

    a = (ROOT / "php/src/Pay.php").read_text(encoding="utf-8")
    b = (ROOT / "php/src/Controllers/AdminController.php").read_text(encoding="utf-8")
    rec("RF-5", "PHP：退款带幂等键 out_request_no（重试不退第二次）",
        "?string $outRequestNo" in a and "out_request_no" in a and "'RF' . $rid" in b,
        "静态守卫：行为需真网关才能验")

    py = (ROOT / "app/refund.py").read_text(encoding="utf-8")
    ph = (ROOT / "php/src/Controllers/ApiController.php").read_text(encoding="utf-8")
    rec("RF-6", "两版一致：重复退款申请检查都含 processing",
        "'pending','processing','approved'" in py and "'pending','processing','approved'" in ph,
        "同集合")

    # ---------------------------------------------------------- 行为断言
    tmp = Path(tempfile.mkdtemp(prefix="kwrt-refund-"))
    dst = tmp / "app"
    try:
        shutil.copytree(ROOT, dst, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns(".git", "users.db", "users.db-*", "store",
                                                      "work", "cache", "__pycache__",
                                                      "config.local.json", "install.lock"))
        (dst / "_p.py").write_text(PROBE, encoding="utf-8")
        r = subprocess.run([sys.executable, "_p.py"], capture_output=True, text=True, cwd=dst)
        out = (r.stdout + r.stderr).strip()
        (dst / "_p.py").unlink(missing_ok=True)

        rec("RF-1", "生产调用方式下正常退款成功（原先必抛 Closed database）",
            "RF1 ok=True st=approved expired=True" in out,
            "ok=True/st=approved/权益已回收" if "RF1 ok=True" in out else out.splitlines()[-1][:70])
        rec("RF-2", "已退款订单被拒并标 failed（调网关前的闸门）",
            "RF2 ok=False st=failed" in out,
            "订单状态闸门生效" if "RF2 ok=False" in out else out.splitlines()[-1][:70])
        rec("RF-3", "processing 在途时不可再申请退款",
            "RF3 can_apply=False" in out,
            "与 PHP 同集合" if "RF3 can_apply=False" in out else out.splitlines()[-1][:70])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("=" * 108)
    bad = [r for r in REC if not r[2]]
    print(f" 合计 {len(REC)} 项，" + ("全部通过 ✓" if not bad else f"失败 ✗ {[b[0] for b in bad]}"))
    print("=" * 108)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())