#!/usr/bin/env python3
"""浏览器实测：管理台「本地构建物」面板是否真的渲染、按钮是否可用。"""
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)

from playwright.sync_api import sync_playwright

CHROME = "/usr/bin/chromium-browser"
BASE = "http://127.0.0.1:8443"
OUT = "/tmp/admin_art_probe.json"

result = {"errors": [], "console": []}

with sync_playwright() as p:
    b = p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
    pg = b.new_page(viewport={"width": 1440, "height": 1000})

    errs = []
    pg.on("console", lambda m: (errs.append(m.text) if m.type == "error" else None))
    pg.on("pageerror", lambda e: errs.append(f"pageerror: {e}"))

    # 登录
    pg.goto(f"{BASE}/login/", wait_until="networkidle")
    pg.fill("#lu", "admin")
    pg.fill("#lp", "admin123")
    pg.click("form.stack button.btn-primary.btn-block")
    pg.wait_for_timeout(2500)
    result["after_login_url"] = pg.url

    # 打开管理台，切到「队列与存储」
    pg.goto(f"{BASE}/admin/#queue", wait_until="networkidle")
    pg.wait_for_timeout(3000)

    result["vue_mounted"] = pg.evaluate(
        "() => !!(document.querySelector('#app') && document.querySelector('#app').children.length)")
    result["tab"] = pg.evaluate(
        "() => { const e=document.querySelector('.admin-nav-item.active'); return e? e.textContent.trim() : null; }")

    # 面板是否出现
    result["has_panel"] = pg.evaluate(
        "() => [...document.querySelectorAll('h4')].some(h => h.textContent.includes('本地构建物'))")
    result["kpi"] = pg.evaluate(
        """() => {
            const out = {};
            document.querySelectorAll('.kv').forEach(k => {
                const a = k.querySelector('.kv-k'), v = k.querySelector('.kv-v');
                if (a && v && /产物合计|孤儿|用户上传/.test(a.textContent)) out[a.textContent.trim()] = v.textContent.trim();
            });
            return out;
        }""")

    # 空态或表格
    result["empty_state"] = pg.evaluate(
        "() => [...document.querySelectorAll('.empty')].some(e => e.textContent.includes('本地没有构建产物'))")
    result["table_rows"] = pg.evaluate(
        """() => {
            const t = [...document.querySelectorAll('table')].find(x => x.previousElementSibling === null && /孤儿|属主/.test(x.textContent));
            return t ? t.querySelectorAll('tbody tr').length : 0;
        }""")

    # 刷新按钮可用（点一下不报错）
    try:
        pg.click("text=本地构建物 >> xpath=ancestor::div[contains(@class,'card')]//button[text()='刷新']", timeout=4000)
        pg.wait_for_timeout(1200)
        result["refresh_clickable"] = True
    except Exception as e:
        result["refresh_clickable"] = f"未点到：{type(e).__name__}"

    result["errors"] = errs
    pg.screenshot(path="/tmp/admin_art.png", full_page=True)

    b.close()

with open(OUT, "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, indent=2)
print(json.dumps(result, ensure_ascii=False, indent=2))
