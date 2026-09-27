#!/usr/bin/env python3
"""精确定位窄屏下的横向溢出元素。"""
import asyncio
import os
import sys

from playwright.async_api import async_playwright

BASE = "http://127.0.0.1:8443"
JS = """() => {
  const vw = window.innerWidth;
  const rows = [];
  // 判断是否被某个会裁剪的祖先容器包住（被裁剪的元素不会撑宽文档）
  const clipped = (el) => {
    let p = el.parentElement;
    while (p && p !== document.documentElement) {
      const ox = getComputedStyle(p).overflowX;
      if (ox === 'hidden' || ox === 'auto' || ox === 'scroll') return p;
      p = p.parentElement;
    }
    return null;
  };
  document.querySelectorAll('*').forEach(el => {
    const b = el.getBoundingClientRect();
    if (b.width > 0 && b.right > vw + 1 && !clipped(el)) {
      rows.push({
        sel: el.tagName.toLowerCase() + (el.className ? '.' + String(el.className).trim().split(/\\s+/).join('.') : ''),
        left: Math.round(b.left), right: Math.round(b.right),
        w: Math.round(b.width),
        text: (el.textContent || '').trim().slice(0, 40),
        overflowX: getComputedStyle(el).overflowX,
        parent: el.parentElement ? el.parentElement.tagName.toLowerCase() + '.' + String(el.parentElement.className || '').trim().split(/\\s+/)[0] : ''
      });
    }
  });
  // 只保留最外层的几个（避免层层嵌套刷屏）
  return { vw, scrollW: document.documentElement.scrollWidth, rows: rows.slice(0, 24) };
}"""


async def main():
    url = sys.argv[1] if len(sys.argv) > 1 else "/?target=x86/64&id=generic"
    width = int(sys.argv[2]) if len(sys.argv) > 2 else 375
    login = len(sys.argv) > 3 and sys.argv[3] == "login"

    async with async_playwright() as p:
        exe = next((c for c in ("/usr/bin/chromium-browser", "/usr/bin/chromium")
                    if os.path.exists(c)), None)
        b = await p.chromium.launch(executable_path=exe, args=["--no-sandbox"])
        ctx = await b.new_context(viewport={"width": width, "height": 900})
        pg = await ctx.new_page()
        await pg.goto(BASE + "/login/", wait_until="domcontentloaded")
        if login:
            await pg.evaluate("""async () => { await fetch('/api/v1/login', {method:'POST',
              headers:{'Content-Type':'application/x-www-form-urlencoded'},
              body:'username=admin&password=admin123'}); }""")
        await pg.goto(BASE + url, wait_until="networkidle")
        await pg.wait_for_timeout(2000)
        d = await pg.evaluate(JS)
        print(f"视口 {d['vw']}px  文档宽 {d['scrollW']}px  "
              f"{'⚠ 横向溢出' if d['scrollW'] > d['vw'] + 1 else '✓ 无溢出'}")
        print()
        for r in d['rows']:
            print(f"  {r['sel'][:56]:<56} x={r['left']:>4}→{r['right']:>5} w={r['w']:>4} "
                  f"overflowX={r['overflowX']:<8} 父={r['parent'][:24]}")
            if r['text']:
                print(f"      文本: {r['text']!r}")
        await b.close()


if __name__ == "__main__":
    asyncio.run(main())
