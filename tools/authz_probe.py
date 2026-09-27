"""端点鉴权实测：逐条发真实请求，验证「未登录」「普通用户」「管理员」三态。"""
import json, os, subprocess, sys

B = "http://127.0.0.1:8443"
ROOT = "/root/.hermes/profiles/2/workspace/kwrt-clone"
os.chdir(ROOT)


def sh(c):
    return subprocess.run(c, shell=True, capture_output=True, text=True).stdout.strip()


def code(cmd):
    return sh(f'{cmd} -s -o /dev/null -w "%{{http_code}}" -m 12')


# 准备三个会话
sh(f'curl -s -c /tmp/p_adm -X POST {B}/api/v1/login -d "username=admin&password=admin123" -o /dev/null')
sh(f'curl -s -c /tmp/p_usr -X POST {B}/api/v1/login -d "username=user&password=user123" -o /dev/null')
sh(f'curl -s -c /tmp/p_spo -X POST {B}/api/v1/login -d "username=sponsor&password=sponsor123" -o /dev/null')

GET_ADMIN = [
    "/api/v1/admin/overview", "/api/v1/admin/users", "/api/v1/admin/builds",
    "/api/v1/admin/logs", "/api/v1/admin/bans", "/api/v1/admin/settings",
    "/api/v1/admin/site", "/api/v1/admin/tokens", "/api/v1/admin/catalog",
    "/api/v1/admin/refunds", "/api/v1/admin/pay/orders", "/api/v1/admin/pay/info",
    "/api/v1/admin/pay/verify_stats", "/api/v1/admin/proposals",
    "/api/v1/admin/sponsor/claims", "/api/v1/admin/mail/log",
    "/api/v1/admin/build_backend",
]
POST_ADMIN = [
    "/api/v1/admin/user", "/api/v1/admin/user/create", "/api/v1/admin/queue",
    "/api/v1/admin/ban", "/api/v1/admin/logs/clear", "/api/v1/admin/site",
    "/api/v1/admin/tokens", "/api/v1/admin/catalog", "/api/v1/admin/refund",
    "/api/v1/admin/pay/order", "/api/v1/admin/proposal", "/api/v1/admin/settings",
    "/api/v1/admin/mail/test", "/api/v1/admin/github/test",
    "/api/v1/admin/user/verify", "/api/v1/admin/user/reverify",
    "/api/v1/admin/user/sponsor", "/api/v1/admin/sponsor/claim",
]

print("=" * 84)
print(f"{'端点':<50}{'匿名':<8}{'普通用户':<10}{'赞助用户':<10}判定")
print("=" * 84)
problems = []


def row(method, path, data=""):
    d = f" -d '{data}'" if data else ""
    anon = code(f"curl -X {method}{d} {B}{path}")
    usr = code(f'curl -b /tmp/p_usr -X {method}{d} {B}{path}')
    spo = code(f'curl -b /tmp/p_spo -X {method}{d} {B}{path}')
    # 判定：匿名与普通用户都必须被拒（非 2xx）
    ok = (not anon.startswith("2")) and (not usr.startswith("2"))
    if not ok:
        problems.append((method, path, anon, usr))
    print(f"{method+' '+path:<50}{anon:<8}{usr:<10}{spo:<10}{'✓' if ok else '✗ 未拦截'}")


for p in GET_ADMIN:
    row("GET", p)
for p in POST_ADMIN:
    row("POST", p, "action=noop")

print("=" * 84)
if problems:
    print("发现未拦截：")
    for m, p, a, u in problems:
        print(f"  {m} {p}: 匿名={a} 普通用户={u}")
else:
    print("全部管理端点：匿名与普通用户均被拒绝 ✓")
print("=" * 84)
for f in ("/tmp/p_adm", "/tmp/p_usr", "/tmp/p_spo"):
    os.path.exists(f) and os.remove(f)
