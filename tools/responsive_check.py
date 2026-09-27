#!/usr/bin/env python3
"""
响应式与页面健康实测：真实浏览器视口 375 / 768 / 1280 下逐页检查。

检查项：
  · 横向溢出（scrollWidth > viewport，且排除被滚动容器裁剪的元素）
  · 移动端汉堡菜单 / 桌面导航 的显隐切换
  · 表格在窄屏是否转为卡片布局
  · 管理台侧边导航在窄屏是否转为横向
  · 页面是否真的渲染出内容（防止空壳页面被误判为「通过」）
  · 验证页进入「验证成功」态（用离线构造的真实令牌）
并输出三档截图供人工核验。

用法：
    python3 tools/responsive_check.py            # 全量
    python3 tools/responsive_check.py home login # 只测指定页
"""
import asyncio
import hashlib
import os
import secrets
import sqlite3
import sys
import time

from playwright.async_api import async_playwright

BASE = "http://127.0.0.1:8443"
OUT = "/tmp/responsive"
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(HERE, "users.db")
ADMIN_USER, ADMIN_PASS = "admin", "admin123"

ALL_PAGES = [
    ("home",     "/"),
    ("device",   "/?target=x86/64&id=generic"),
    ("packages", "/packages/"),
    ("newpkg",   "/newpkg/"),
    ("fadian",   "/fadian/"),
    ("contact",  "/contact/"),
    ("login",    "/login/"),
    ("admin",    "/admin/"),
    ("verify",   "/verify/"),        # 令牌在运行时注入
]
WIDTHS = [375, 768, 1280]

PASS, FAIL, SKIP = [], [], []


def ok(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  [{extra}]" if extra else ""))


def skip(name, why):
    SKIP.append(f"{name}（{why}）")
    print(f"  ⊘ {name}  [跳过：{why}]")


def login_url():
    return "/login/"


# ---------------------------------------------------------------- 令牌构造
def make_vtoken():
    """
    造一个合法的邮箱验证令牌（返回 (username, token)）。

    直接写库：令牌哈希是无盐 SHA256（app/verify.py），可离线生成。
    不走注册流程 —— 那会顶掉 admin 会话，还要临时改注册验证开关。
    注意 users.password 是 NOT NULL，必须给一个占位值。
    """
    tok = secrets.token_hex(32)
    user = "rt" + str(int(time.time() * 1000))[-8:]
    now = time.time()
    with sqlite3.connect(DB, timeout=15) as c:
        c.execute("INSERT INTO users (username,password,email,role,email_verified,created) "
                  "VALUES (?,?,?,?,0,?)", (user, "x", f"{user}@example.com", "user", now))
        c.execute("INSERT INTO email_verifications "
                  "(token_hash,username,email,created,expires,used,ip) "
                  "VALUES (?,?,?,?,?,0,'127.0.0.1')",
                  (hashlib.sha256(tok.encode()).hexdigest(), user,
                   f"{user}@example.com", now, now + 86400))
    return user, tok


PROBE_JS = r"""() => {
  const d = document.documentElement;
  const q = s => document.querySelector(s);
  const vis = el => el ? getComputedStyle(el).display !== 'none' : null;

  // 被某个会裁剪/滚动的祖先包住的元素不会撑宽文档，判定溢出时应排除
  const clipped = el => {
    let p = el.parentElement;
    while (p && p !== d) {
      const ox = getComputedStyle(p).overflowX;
      if (ox === 'hidden' || ox === 'auto' || ox === 'scroll') return true;
      p = p.parentElement;
    }
    return false;
  };
  const wide = [];
  document.querySelectorAll('*').forEach(el => {
    const b = el.getBoundingClientRect();
    if (b.width > 0 && b.right > window.innerWidth + 1 && !clipped(el)) {
      wide.push(el.tagName.toLowerCase() + '.' + String(el.className || '').split(' ')[0]);
    }
  });

  return {
    scrollW: d.scrollWidth,
    innerW: window.innerWidth,
    fullText: (document.body.innerText || '').replace(/\s+/g, ' '),
    header: !!q('.site-header') || !!q('.admin-shell') || !!q('.admin-body'),
    burger: vis(q('.burger')),
    deskNav: vis(q('.nav-links.desktop')),
    tblHead: q('.table-responsive thead') ? vis(q('.table-responsive thead')) : null,
    adminDir: q('.admin-nav') ? getComputedStyle(q('.admin-nav')).flexDirection : null,
    emptyMsg: (q('.empty h4') || {}).textContent || null,
    bootVisible: !!q('.boot'),
    wide: [...new Set(wide)].slice(0, 6),
  };
}"""


async def probe(page, name, width):
    await page.set_viewport_size({"width": width, "height": 900})
    await page.goto(BASE + PAGES_URL[name], wait_until="networkidle")
    await page.wait_for_timeout(1800)
    m = await page.evaluate(PROBE_JS)

    tag = f"{name}@{width}"
    # verify 页成功态本身简短（一张结果卡），阈值按页面性质分别设定
    minlen = 60 if name == "verify" else 200
    ok(f"{tag} 有实际内容", len(m["fullText"]) > minlen, f"{len(m['fullText'])} 字符")
    ok(f"{tag} 无横向溢出", m["scrollW"] <= width + 2,
       f"scrollW={m['scrollW']}/vw={width}" + (f" 溢出={m['wide']}" if m["wide"] else ""))
    ok(f"{tag} 加载遮罩已消失", not m["bootVisible"])

    if name in ("home", "device", "login", "packages", "newpkg", "fadian", "contact"):
        if width == 375:
            ok(f"{tag} 显示汉堡菜单", m["burger"] is True)
        elif width == 1280:
            ok(f"{tag} 显示桌面导航", m["deskNav"] is True)

    if name in ("device", "packages"):
        if width == 375:
            ok(f"{tag} 表格转卡片（表头隐藏）", m["tblHead"] is False)
        elif width == 1280:
            ok(f"{tag} 表格正常（表头显示）", m["tblHead"] is True)

    if name == "admin":
        if width == 375:
            ok(f"{tag} 侧边导航转横向", m["adminDir"] == "row", str(m["adminDir"]))
        elif width == 1280:
            ok(f"{tag} 侧边导航为竖向", m["adminDir"] == "column", str(m["adminDir"]))

    if name == "verify":
        ok(f"{tag} 进入验证成功态", "邮箱验证成功" in m["fullText"],
           (m["fullText"][-40:] if "邮箱验证成功" not in m["fullText"] else "ok"))

    os.makedirs(OUT, exist_ok=True)
    await page.screenshot(path=f"{OUT}/{name}-{width}.png")
    return m


PAGES_URL = {}


async def main():
    want = set(sys.argv[1:])
    pages = [p for p in ALL_PAGES if not want or p[0] in want]

    print("=" * 78)
    print("响应式与页面健康实测（真实浏览器视口 375 / 768 / 1280）")
    print("=" * 78)

    async with async_playwright() as p:
        # 本机已装系统 Chromium，直接复用，无需再下载 Playwright 自带的 headless shell
        exe = next((c for c in ("/usr/bin/chromium-browser", "/usr/bin/chromium",
                                "/usr/bin/google-chrome") if os.path.exists(c)), None)
        browser = await p.chromium.launch(executable_path=exe,
                                          args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = await browser.new_context()
        page = await ctx.new_page()

        for name, url in pages:
            PAGES_URL[name] = url

        # 管理台需要登录
        if any(n in ("admin", "verify") for n, _ in pages):
            await page.goto(BASE + "/login/", wait_until="domcontentloaded")
            st = await page.evaluate("""async () => {
              const r = await fetch('/api/v1/login', {method:'POST',
                headers:{'Content-Type':'application/x-www-form-urlencoded'},
                body:'username=admin&password=admin123'});
              return (await r.json()).status;
            }""")
            print(f"\n  [会话] admin 登录: {st}")

        for name, _ in pages:
            print(f"\n[{name}]")
            for w in WIDTHS:
                if name == "verify":
                    # 令牌一次性消费，故每个宽度都换新令牌，否则第二档只能测到「已使用」
                    user, tok = make_vtoken()
                    PAGES_URL["verify"] = "/verify/?token=" + tok
                await probe(page, name, w)

        await browser.close()

    # 清理本测试创建的账号与令牌
    try:
        with sqlite3.connect(DB, timeout=15) as c:
            c.execute("DELETE FROM users WHERE username LIKE 'rt%' OR username LIKE 'vfy%' "
                      "OR username LIKE 'probe%'")
            c.execute("DELETE FROM email_verifications WHERE username LIKE 'rt%' "
                      "OR username LIKE 'vfy%'")
            c.execute("DELETE FROM sessions WHERE username NOT IN (SELECT username FROM users)")
        print("\n已清理测试账号与令牌。")
    except Exception as e:
        print(f"\n清理失败: {e}")

    print("\n" + "=" * 78)
    print(f"结果: 通过 {len(PASS)} 项，失败 {len(FAIL)} 项" +
          (f"，跳过 {len(SKIP)} 项" if SKIP else ""))
    for f in FAIL:
        print("  ✗", f)
    for s in SKIP:
        print("  ⊘", s)
    if not FAIL:
        print("全部通过 ✓")
    print(f"\n截图已保存至 {OUT}/")
    print("=" * 78)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
