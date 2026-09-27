"""枚举全部路由 + 标注鉴权调用，找出「该鉴权却未鉴权」的端点。"""
import re, os

src = open("app/main.py", encoding="utf-8").read()
lines = src.split("\n")

routes = []
for i, ln in enumerate(lines):
    m = re.match(r'@app\.(get|post|put|patch|delete)\("([^"]+)"', ln)
    if not m:
        continue
    method, path = m.group(1).upper(), m.group(2)
    # 抓函数体（到下一个 @app 或 def 结束的粗略范围：后 60 行）
    body = "\n".join(lines[i:i + 70])
    # 截到下一个顶层 @app
    nxt = body.find("\n@app.")
    if nxt > 0:
        body = body[:nxt]
    has_require = "require_admin" in body
    has_isadmin = "is_admin(" in body
    has_current = "current_user(" in body
    has_guard = "_may_read_store" in body
    has_verif = "verif" in body.lower() or "one_time" in body.lower()
    routes.append({
        "method": method, "path": path,
        "admin": has_require, "is_admin": has_isadmin,
        "user": has_current, "store_guard": has_guard,
        "line": i + 1,
    })

print(f"总路由数: {len(routes)}\n")
print(f"{'方法':<7}{'路径':<52}{'鉴权'}")
print("-" * 90)
for r in routes:
    if r["admin"]:
        a = "ADMIN"
    elif r["store_guard"]:
        a = "属主/ADMIN"
    elif r["is_admin"] or r["user"]:
        a = "登录"
    else:
        a = "—— 公开 ——"
    print(f"{r['method']:<7}{r['path']:<52}{a}")

print("\n\n=== 公开端点清单（需逐个确认是否应公开）===")
for r in routes:
    if not (r["admin"] or r["is_admin"] or r["user"] or r["store_guard"]):
        print(f"  L{r['line']:<5} {r['method']:<6} {r['path']}")
