#!/usr/bin/env python3
"""赞助订单与退款入口的可见性回归（真实浏览器）。

## 为什么需要这个套件

用户报的原话：「赞助完成后没有显示订单 没有显示退款按钮」。

根因（两处叠加）：

  1. 「我的订单 / 申请退款」**只长在赞助弹窗内部**。弹窗要靠某个入口点开，
     入口没了，订单和退款就一起跟着看不见。
  2. 设备定制页那张入口卡片写的是
     `v-if="site['sponsor.enabled'] && !user.sponsor"` ——
     **赞助成功后 user.sponsor 变 true，卡片当场消失**。
     而它正是用户当时唯一的显眼入口（导航栏那个「赞助支持」小链接没人会去点）。

  合起来就是：付完钱 → 原地入口消失 → 订单和退款按钮都「没有显示」。

**接口一直是好的** —— `/api/v1/sponsor/orders` 正常返回 `count=8`、
`refund_enabled=true`、那笔 paid 订单 `refundable=true`。
坏的纯粹是「东西在不在用户看得见的地方」。

所以这个套件检查的是**可见性**，必须用真实浏览器 —— 静态 HTML 里查不到，
因为首页是客户端渲染的 Vue 应用（源码里只有模板，没有渲染结果）。

## 断言

  SU-1  「我的订单」区块**不打开任何弹窗**就可见        ← 主断言
  SU-2  已支付订单旁有「申请退款」按钮
  SU-3  已是赞助用户时，赞助入口卡片**仍然可见**（不再消失）
  SU-4  点「申请退款」能打开退款弹窗
  SU-5  负向对照：已取消/已退款的订单**没有**退款按钮（证明 SU-2 非空测）
  SU-6  结构性：订单区块**不在** `.modal-mask` 内（挡住「又搬回弹窗」的回归）
  SU-7  页面无 JS 运行时错误
  SU-8  页头有「我的订单」直达入口（对齐 PHP 版 layout.php）

## 用法

    python3.9 tools/verify_sponsor_ui.py

★ 必须用**装了 playwright 的解释器**跑（本机是 `/usr/bin/python3.9`，
  不是 `/usr/bin/python3`）；被测服务由套件自己用项目 `.venv` 起。
★ 必须传 `executable_path=/usr/bin/chromium-browser` —— 项目没装 playwright 自带的浏览器。
★ 临时目录必须把 **`data/`** 一起拷进去：设备列表来自 `/json/v1/...`，
  那是本地数据集目录。少了它设备选不中，SU-3 会因为「页面上没有设备」
  而假失败 —— 这个坑踩过一次。
"""
from __future__ import annotations

import hashlib
import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("KWRT_SUI_PORT", "8291"))
BASE = f"http://127.0.0.1:{PORT}"
CHROME = "/usr/bin/chromium-browser"
#: 被测用户：本地库里赞助中的普通用户（sponsor=1, role=user）—— 正是出问题的场景
SUBJECT, SUBJECT_PW = "sponsor", "sponsor123"

REC: list[tuple[str, str, bool, str]] = []


def rec(cid, title, ok, note=""):
    REC.append((cid, title, bool(ok), note))
    print(f"  {cid:<5} {title:<52} {'✓ 通过' if ok else '✗ 失败'}  {note}")


def main() -> int:                                              # noqa: C901
    print("=" * 112)
    print(" 赞助订单与退款入口的可见性回归（真实浏览器）")
    print("=" * 112)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  ! 当前解释器没有 playwright。本套件必须用系统 python3 运行：")
        print("      /usr/bin/python3 tools/verify_sponsor_ui.py")
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="kwrt-sui-"))
    srv = None
    try:
        for d in ("app", "web", "data"):
            shutil.copytree(HERE / d, tmp / d,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for f in ("config.json", "VERSION"):
            if (HERE / f).exists():
                shutil.copy2(HERE / f, tmp / f)
        # 复用仓库库（表结构齐全），只补造需要的订单
        shutil.copy2(HERE / "users.db", tmp / "users.db")
        tdb = tmp / "users.db"

        c = sqlite3.connect(tdb)
        c.execute("DELETE FROM pay_orders WHERE username=?", (SUBJECT,))
        c.execute("DELETE FROM refund_requests WHERE username=?", (SUBJECT,))
        c.execute("UPDATE users SET sponsor=1 WHERE username=?", (SUBJECT,))
        now = time.time()
        # ① 已支付 → 应显示「已支付」+「申请退款」
        c.execute("INSERT INTO pay_orders(out_trade_no,username,tier_name,amount,days,status,"
                  "created,paid_at) VALUES(?,?,?,?,?,?,?,?)",
                  ("SUI-PAID-0001", SUBJECT, "月付赞助", 4.99, 30, "paid", now - 600, now - 540))
        # ② 已取消 → 不得出现退款按钮（SU-5 的负向对照）
        c.execute("INSERT INTO pay_orders(out_trade_no,username,tier_name,amount,days,status,"
                  "created) VALUES(?,?,?,?,?,?,?)",
                  ("SUI-CANCEL-0002", SUBJECT, "季付赞助", 9.99, 90, "cancelled", now - 500))
        c.commit()
        c.close()

        srv = subprocess.Popen(
            [str(HERE / ".venv" / "bin" / "python"), "-m", "uvicorn", "app.main:app",
             "--host", "127.0.0.1", "--port", str(PORT), "--log-level", "warning",
             "--forwarded-allow-ips", ""],
            cwd=tmp, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            preexec_fn=os.setsid)
        up = False
        for _ in range(60):
            try:
                with urllib.request.urlopen(BASE + "/healthz", timeout=3) as r:
                    if r.status == 200:
                        up = True
                        break
            except Exception:                                    # noqa: BLE001
                time.sleep(0.5)
        if not up:
            print("  ! 服务未起来：")
            print("   ", (srv.stdout.read() or "")[-600:] if srv.stdout else "")
            print(f"    排查：ss -lntp | grep :{PORT}")
            return 2

        with sync_playwright() as p:
            exe = next((x for x in (CHROME, "/usr/bin/chromium") if os.path.exists(x)), None)
            b = p.chromium.launch(executable_path=exe, args=["--no-sandbox"])
            pg = b.new_page(viewport={"width": 1280, "height": 900})
            errs: list[str] = []
            pg.on("pageerror", lambda e: errs.append(str(e)[:140]))

            pg.goto(BASE + "/", wait_until="domcontentloaded")
            # 页面内登录（比走登录表单稳），再重载让 onMounted 拿到登录态
            ok_login = pg.evaluate(
                """async () => {
                     const r = await fetch('/api/v1/login', {
                       method:'POST', headers:{'Content-Type':'application/x-www-form-urlencoded'},
                       body: new URLSearchParams({username:'%s', password:'%s'})});
                     return r.status;
                   }""" % (SUBJECT, SUBJECT_PW))
            pg.reload(wait_until="networkidle")
            pg.wait_for_timeout(1500)

            def visible(sel):
                loc = pg.locator(sel)
                return loc.count() > 0 and loc.first.is_visible()

            # ---- SU-1 主断言：不打开任何弹窗，订单区块就该可见
            mask_open = pg.locator(".modal-mask").count() > 0
            orders_visible = False
            for sel in ("text=我的订单", "h2:has-text('我的订单')"):
                if visible(sel):
                    orders_visible = True
                    break
            rec("SU-1", "「我的订单」不打开弹窗就可见（登录且已赞助）",
                ok_login == 200 and orders_visible and not mask_open,
                f"登录={ok_login} 弹窗打开={mask_open} 订单区可见={orders_visible}")

            # ---- SU-2 已支付订单要有退款按钮
            refund_btns = pg.locator("button:has-text('申请退款')")
            n_refund = refund_btns.count()
            rec("SU-2", "已支付订单旁有「申请退款」按钮",
                n_refund >= 1, f"找到 {n_refund} 个")

            # ---- SU-3 已是赞助用户时入口卡片不再消失
            # ★ 这张卡片在「已选设备」那一屏里，所以要先真的选中设备。
            #   走 URL 查询串最稳 —— 热点按钮只是把词填进搜索框并展开下拉，
            #   还得再点一次下拉里的结果项才会 pickDevice()。第一版就卡在
            #   这里，把「设备没选中」误判成「卡片不见了」。
            pg.goto(BASE + "/?target=x86/64&id=generic", wait_until="networkidle")
            pg.wait_for_timeout(2500)
            dev_ok = pg.locator(".card", has_text="构建摘要").count() > 0
            card = pg.locator(".card", has_text="你已是赞助用户")
            entry_visible = card.count() > 0 and card.first.is_visible()
            rec("SU-3", "已赞助用户的赞助入口卡片仍然可见",
                dev_ok and entry_visible,
                f"设备已选中={dev_ok} 卡片可见={entry_visible}")

            # ---- SU-4 点「申请退款」能打开退款弹窗
            dialog_ok = False
            if n_refund >= 1:
                refund_btns.first.click()
                pg.wait_for_timeout(1200)
                dlg = pg.locator("h3:has-text('申请退款')")
                dialog_ok = dlg.count() > 0 and dlg.first.is_visible()
                pg.keyboard.press("Escape")
                pg.wait_for_timeout(400)
            rec("SU-4", "点「申请退款」能打开退款申请弹窗", dialog_ok, "")

            # ---- SU-5 负向对照：已取消订单不得有退款按钮
            n_orders = pg.locator(".order-item").count()
            cancel_refundable = pg.evaluate(
                """() => {
                     const items = Array.from(document.querySelectorAll('.order-item'));
                     const bad = items.filter(el =>
                       el.textContent.indexOf('SUI-CANCEL-0002') >= 0 &&
                       el.textContent.indexOf('申请退款') >= 0);
                     return bad.length;
                   }""")
            rec("SU-5", "负向对照：已取消订单没有退款按钮（证明 SU-2 非空测）",
                n_orders >= 2 and cancel_refundable == 0,
                f"订单条目 {n_orders} 个；已取消订单带退款按钮的 = {cancel_refundable}")

            # ---- SU-8 页头要有常驻的「我的订单」直达入口
            # 与 PHP 版 layout.php 的 <a href="/?orders=1">我的订单</a> 对齐。
            # 光有页面下方的区块还不够 —— 用户在别处（比如刚付完款）也得能一眼找到。
            nav = pg.locator("a.nav-link", has_text="我的订单")
            nav_ok = nav.count() > 0 and nav.first.get_attribute("href") == "#orders"
            rec("SU-8", "页头有「我的订单」直达入口（对齐 PHP 版）",
                nav_ok, f"找到 {nav.count()} 个，href={'#orders' if nav_ok else '不符'}")

            # ---- SU-6 结构性：订单区块不在弹窗里
            in_modal = pg.evaluate(
                """() => {
                     const m = document.querySelector('.modal-mask');
                     if (!m) return false;
                     return m.textContent.indexOf('我的订单') >= 0;
                   }""")
            rec("SU-6", "结构性：订单区块不在 .modal-mask 内（挡住搬回弹窗）",
                not in_modal, "不在弹窗内 ✓" if not in_modal else "又搬回弹窗里了")

            if errs:
                print("\n  JS 运行时错误：")
                for e in errs[:6]:
                    print("   ", e)
            b.close()

        rec("SU-7", "页面无 JS 运行时错误", not errs, f"{len(errs)} 个")
    finally:
        for p in (srv,):
            if p is not None:
                try:
                    os.killpg(os.getpgid(p.pid), 15)
                    p.wait(timeout=10)
                except Exception:                                # noqa: BLE001
                    pass
        shutil.rmtree(tmp, ignore_errors=True)

    print("-" * 112)
    npass = sum(1 for _, _, ok, _ in REC if ok)
    print(f" 合计 {len(REC)} 项，"
          f"{'全部通过 ✓' if npass == len(REC) else '失败 ✗ ' + str([c for c, _, ok, _ in REC if not ok])}")
    print("=" * 112)
    return 0 if npass == len(REC) else 1


if __name__ == "__main__":
    raise SystemExit(main())
