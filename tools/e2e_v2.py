#!/usr/bin/env python3
"""
新版功能综合 e2e —— 站点配置 / 赞助 / 双后端 / 邮件通知 / 限时下载链接
用法: python3 tools/e2e_v2.py
"""
import base64
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import uuid

B = os.environ.get("E2E_BASE", "http://127.0.0.1:8443")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PASS, FAIL = [], []


def ok(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  [{extra}]" if extra else ""), flush=True)


def client():
    cj = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))


def get(op, path, raw=False):
    try:
        r = op.open(B + path, timeout=30)
        b = r.read()
        return r.status, (b if raw else json.loads(b or b"{}"))
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            body = json.loads(body or b"{}")
        except Exception:
            # HTML / 文本响应保留全文，供断言检查关键字
            body = body.decode("utf-8", "ignore")
        return e.code, body


def post(op, path, data=None, js=None, headers=None):
    h = dict(headers or {})
    if js is not None:
        body = json.dumps(js).encode()
        h["Content-Type"] = "application/json"
    else:
        body = urllib.parse.urlencode(data or {}).encode()
    req = urllib.request.Request(B + path, data=body, headers=h)
    try:
        r = op.open(req, timeout=60)
        b = r.read()
        try:
            return r.status, json.loads(b or b"{}")
        except Exception:
            return r.status, b
    except urllib.error.HTTPError as e:
        b = e.read()
        try:
            return e.code, json.loads(b or b"{}")
        except Exception:
            return e.code, b.decode("utf-8", "ignore")


def verif():
    s = json.dumps({"uuid": str(uuid.uuid4())})
    return base64.b64encode("".join(chr(ord(c) ^ 80) for c in s).encode()).decode()


def main():
    print("=" * 72)
    print("新版功能综合 e2e")
    print("=" * 72)

    # ---------- 1. 站点信息 API ----------
    print("\n[1] 站点信息与安全边界")
    anon = client()
    st, site = get(anon, "/api/v1/site")
    ok("前台站点信息可读", st == 200 and "site_name" in site)
    ok("前台不泄露 SMTP 密码", "mail.password" not in site)
    ok("前台不泄露 GitHub Token", "gh.token" not in site)
    ok("前台不泄露 SMTP 主机", "mail.host" not in site)
    ok("前台含赞助套餐", len(site.get("sponsor_tiers") or []) >= 1,
       f"{len(site.get('sponsor_tiers') or [])} 个")
    ok("前台含构建设置", "build" in site)

    st, _ = get(anon, "/api/v1/admin/site")
    ok("匿名访问管理配置被拒", st == 403)

    # ---------- 2. 管理员登录 ----------
    print("\n[2] 管理员登录与配置 schema")
    ad = client()
    st, r = post(ad, "/api/v1/login", {"username": "admin", "password": "admin123"})
    ok("管理员登录", st == 200 and r.get("is_admin") is True)

    # 全局快照：本测试会改写站点配置，结束时逐项还原，避免污染真实配置
    sys.path.insert(0, ROOT)
    from app import sitesettings as _SS
    _snap = dict(_SS.all_values())
    print(f"    已快照 {len(_snap)} 项站点配置，结束时自动还原")

    st, cfg = get(ad, "/api/v1/admin/site")
    ok("配置 schema 可读", st == 200)
    n_fields = len(cfg.get("schema") or [])
    ok("配置项数量充足（≥50）", n_fields >= 50, f"{n_fields} 项")
    ok("分组完整（≥6）", len(cfg.get("groups") or []) >= 6, f"{len(cfg.get('groups'))} 组")
    ok("敏感项已掩码", cfg["values"].get("mail.password") in ("", "********"))

    # ---------- 3. 修改站点信息 → 前台生效 ----------
    print("\n[3] 管理员修改站点信息 → 前台立即生效")
    tag = str(int(time.time()))[-6:]
    st, r = post(ad, "/api/v1/admin/site", js={
        "hero_title": f"e2e-标题-{tag}",
        "site_short": "KwrtE2E",
        "footer_text": f"e2e-页脚-{tag}",
        "download.link_ttl_hours": 24,
        "sponsor.tiers": [
            {"name": "E2E 套餐A", "amount": 7, "days": 14, "perks": ["解锁定制"]},
            {"name": "E2E 套餐B", "amount": 77, "days": 200, "perks": ["解锁全部"]},
        ],
    })
    ok("批量写入配置成功", r.get("status") == "ok" and not r.get("errors"), str(r.get("errors")))
    st, site2 = get(anon, "/api/v1/site")
    ok("前台标题已更新", site2.get("hero_title") == f"e2e-标题-{tag}")
    ok("前台简称已更新", site2.get("site_short") == "KwrtE2E")
    ok("前台有效期已更新为 24h", site2.get("download.link_ttl_hours") == 24)
    ok("前台套餐已更新（含金额）",
       [t["amount"] for t in site2.get("sponsor_tiers", [])] == [7, 77])

    st, r = post(ad, "/api/v1/admin/site", js={"download.link_ttl_hours": 99999})
    ok("非法配置被拒（超范围）", r.get("status") == "partial" and "download.link_ttl_hours" in r.get("errors", {}))
    st, r = post(ad, "/api/v1/admin/site", js={"sponsor.tiers": [{"name": "", "amount": -1, "days": 0}]})
    ok("非法套餐被拒", bool(r.get("errors")))

    # ---------- 4. 赞助流程 ----------
    print("\n[4] 赞助流程（金额由管理员配置）")
    uu = "e2e_user_" + tag
    post(ad, "/api/v1/admin/user/create",
         {"username": uu, "password": "e2ePass123", "email": f"{uu}@example.com",
          "role": "user", "sponsor": 0})
    us = client()
    st, r = post(us, "/api/v1/login", {"username": uu, "password": "e2ePass123"})
    ok("新用户登录", st == 200 and r.get("sponsor") is False)

    # 关闭自助 → 走审核
    post(ad, "/api/v1/admin/site", js={"sponsor.auto_approve": False})
    st, r = post(us, "/api/v1/sponsor/claim", {"tier": "E2E 套餐A", "note": "e2e"})
    ok("赞助申请进入待审核", r.get("status") == "pending", str(r.get("detail"))[:40])

    st, cl = get(ad, "/api/v1/admin/sponsor/claims")
    pending = [c for c in cl.get("claims", []) if c["username"] == uu and c["status"] == "pending"]
    ok("管理端可见待审核申请", len(pending) == 1)
    if pending:
        st, r = post(ad, "/api/v1/admin/sponsor/claim", {"cid": pending[0]["id"], "action": "approve"})
        ok("审核通过", r.get("status") == "ok", f"天数 {r.get('days')}")
    st, mu = get(us, "/api/v1/user")
    ok("用户赞助态已激活", mu.get("sponsor") is True, f"到期 {mu.get('sponsor_until')}")
    ok("赞助到期时间已记录", bool(mu.get("sponsor_until")))

    st, r = post(ad, "/api/v1/admin/user/sponsor", {"username": uu, "clear": "1"})
    st, mu = get(us, "/api/v1/user")
    ok("管理员可撤销赞助", mu.get("sponsor") is False)

    # ---------- 5. 下载令牌 ----------
    print("\n[5] 限时下载链接")
    st, tk = get(ad, "/api/v1/admin/tokens?limit=200")
    ok("令牌列表可读", st == 200 and "stats" in tk)
    stats0 = tk.get("stats", {})

    # 造一个真实产物用于签发
    probe = os.path.join(ROOT, "store", "e2eprobe")
    os.makedirs(probe, exist_ok=True)
    with open(os.path.join(probe, "probe.bin"), "wb") as f:
        f.write(os.urandom(4096))

    sys.path.insert(0, ROOT)
    from app import dl
    t_ok = dl.issue("e2eprobe", "probe.bin", username=uu, ttl_hours=24)
    st, raw = get(anon, "/dl/t/" + t_ok, raw=True)
    ok("有效令牌可下载", st == 200 and len(raw) == 4096, f"{len(raw) if isinstance(raw, bytes) else '?'} 字节")

    t_exp = dl.issue("e2eprobe", "probe.bin", ttl_hours=24)
    with dl.db() as c:
        c.execute("UPDATE dl_tokens SET expires=? WHERE token=?", (time.time() - 5, t_exp))
    st, body = get(anon, "/dl/t/" + t_exp)
    ok("过期令牌返回 410", st == 410)
    ok("过期页面给出原因", "过期" in (body if isinstance(body, str) else str(body)))

    t_rev = dl.issue("e2eprobe", "probe.bin", ttl_hours=24)
    st, r = post(ad, "/api/v1/admin/tokens", {"action": "revoke", "token": t_rev})
    st, _ = get(anon, "/dl/t/" + t_rev)
    ok("吊销后不可下载", st == 410)

    t_ext = dl.issue("e2eprobe", "probe.bin", ttl_hours=1)
    st, r = post(ad, "/api/v1/admin/tokens", {"action": "extend", "token": t_ext, "hours": "100"})
    ok("续期操作成功", r.get("status") == "ok")
    o, m, row = dl.verify(t_ext)
    ok("续期后剩余时间增加", o and row["expires"] - time.time() > 99 * 3600)

    st, r = get(anon, "/dl/t/aaaa.9999999999.bbbb")
    ok("伪造令牌返回 410", st == 410)

    st, r = post(ad, "/api/v1/admin/tokens", {"action": "reissue", "request_hash": "e2eprobe", "hours": "12"})
    ok("重发链接成功", r.get("status") == "ok" and r.get("count", 0) >= 1, f"{r.get('count')} 个")

    st, r = post(ad, "/api/v1/admin/tokens", {"action": "revoke_build", "request_hash": "e2eprobe"})
    ok("按构建批量吊销", r.get("status") == "ok")

    # ---------- 6. 邮件通知 ----------
    print("\n[6] 邮件通知（真实 SMTP）")
    st, ml = get(ad, "/api/v1/admin/mail/log")
    ok("邮件日志可读", st == 200)

    st, r = post(ad, "/api/v1/admin/site", js={"mail.enabled": False})
    st, r = post(ad, "/api/v1/admin/mail/test", {"to": "x@example.com"})
    ok("未启用时给出明确原因", st == 400 and "未启用" in str(r.get("detail")))

    st, r = post(ad, "/api/v1/admin/site",
                 js={"mail.enabled": True, "mail.host": "", "mail.port": 465})
    st, r = post(ad, "/api/v1/admin/mail/test", {"to": "x@example.com"})
    ok("未配服务器时给出明确原因", st == 400 and "SMTP" in str(r.get("detail")))

    st, r = post(ad, "/api/v1/admin/site",
                 js={"mail.enabled": True, "mail.host": "127.0.0.1", "mail.port": 2525,
                     "mail.encryption": "none", "mail.from_addr": "build@kwrt.local"})
    ok("SMTP 配置写入", not r.get("errors"))
    st, r = post(ad, "/api/v1/admin/mail/test", {"to": "e2e@example.com"})
    ok("真实发信成功", r.get("status") == "ok", str(r.get("detail"))[:60])

    # ---------- 7. 构建后端 ----------
    print("\n[7] 构建后端（本地 / GitHub）")
    st, bi = get(ad, "/api/v1/admin/build_backend")
    ok("后端信息可读", st == 200 and "effective" in bi)
    post(ad, "/api/v1/admin/site", js={"builder.backend": "github"})
    st, bi = get(ad, "/api/v1/admin/build_backend")
    # 断言随前置条件自适应：
    #   未配置 GitHub → 必须安全回落本地；已配置 → 必须真正生效为 github
    if bi.get("github_available"):
        ok("选 github 且已配置时后端真正生效",
           bi.get("configured") == "github" and bi.get("effective") == "github",
           f"effective={bi.get('effective')}")
    else:
        ok("选 github 但未配置时安全回落本地",
           bi.get("configured") == "github" and bi.get("effective") == "local",
           f"effective={bi.get('effective')}")
    # 保存真实 GitHub 配置，测完必须还原（避免 e2e 破坏可用配置）
    # token 在前端接口是掩码，无法回读；直接读库取值用于还原
    sys.path.insert(0, ROOT)
    from app import sitesettings as _SS
    _saved_gh = {"repo": _SS.get("gh.repo") or "", "token": _SS.get("gh.token") or "",
                 "backend": _SS.get("builder.backend") or "local"}
    post(ad, "/api/v1/admin/site",
         js={"gh.repo": "no-such-owner-x/no-such-repo-x", "gh.token": "ghp_invalid000"})
    st, r = post(ad, "/api/v1/admin/github/test", {})
    ok("GitHub API 真实调用（返回鉴权错误）",
       st == 400 and ("401" in str(r.get("detail")) or "Bad credentials" in str(r.get("detail"))),
       str(r.get("detail"))[:60])
    # 逐项还原（含 token 原文）
    restore = {"gh.repo": _saved_gh["repo"], "builder.backend": _saved_gh["backend"]}
    if _saved_gh["token"]:
        restore["gh.token"] = _saved_gh["token"]
    post(ad, "/api/v1/admin/site", js=restore)
    st, bi2 = get(ad, "/api/v1/admin/build_backend")
    ok("GitHub 配置已完整还原",
       (_saved_gh["token"] == "" and not bi2.get("github_available"))
       or bi2.get("github_available") is True,
       f"repo={_saved_gh['repo'] or '(未配)'} github_available={bi2.get('github_available')}")

    # ---------- 8. 真实构建 + 令牌 + 邮件 全链路 ----------
    print("\n[8] 全链路：提交构建 → 签发限时链接 → 邮件通知")
    # 本步固定用本地后端：验证「站点受理 → 签发限时链接 → 邮件」这条链路本身。
    # GitHub 后端的派发/轮询/回传链路由 tools/gh_watch.py 与报告 04 单独验证，
    # 且其产物回传受跨国带宽限制（可达数十分钟），不适合放进常规回归。
    post(ad, "/api/v1/admin/site", js={"builder.backend": "local"})
    st, r = post(us, "/api/v1/build",
                 js={"target": "x86/64", "profile": "generic", "version": "25.12",
                     "packages": ["luci-app-ttyd"], "filesystem": "squashfs",
                     "rootfs_size_mb": 512, "efi": "all", "email": f"{uu}@example.com"},
                 headers={"Ng-One-Time-Verif-Value": verif()})
    if st != 202:
        ok("构建提交", False, f"HTTP {st} {str(r)[:120]}")
        return
    jid = r["request_hash"]
    ok("构建提交成功", st == 202, jid)

    final = None
    for i in range(120):
        st, j = get(anon, "/api/v1/build/" + jid)
        if j.get("status") in ("done", "failed", "cancelled"):
            final = j
            break
        time.sleep(8)
    if not final:
        ok("构建完成", False, "超时")
        return
    ok("构建成功", final["status"] == "done", f"{final.get('duration')}s")
    ok("产出多个文件", len(final.get("files") or []) >= 3, f"{len(final.get('files') or [])} 个")
    links = final.get("download_links") or []
    ok("已签发限时下载链接", len(links) >= 3, f"{len(links)} 个")
    ok("链接带有效期(24h)", final.get("link_ttl_hours") == 24, f"{final.get('link_ttl_hours')}h")
    ok("链接含到期时间戳", bool(final.get("links_expire_at")))

    if links:
        st, raw = get(anon, links[0]["path"], raw=True)
        ok("构建产物可经令牌下载", st == 200 and isinstance(raw, bytes) and len(raw) > 1000,
           f"{len(raw) if isinstance(raw, bytes) else '?'} 字节")

    ms = final.get("mail_status") or {}
    ok("构建完成邮件已发送", ms.get("ok") is True, f"{ms.get('to')} / {ms.get('detail')}")

    # 验证 SMTP 实际收到
    inbox = "/tmp/smtp_inbox.jsonl"
    if os.path.exists(inbox):
        import email.header
        rows = [json.loads(l) for l in open(inbox)]
        for r in rows:                      # 主题为 MIME 编码，需先解码再匹配
            sj = r.get("subject", "")
            if sj.startswith("=?"):
                r["subject_decoded"] = str(
                    email.header.make_header(email.header.decode_header(sj)))
            else:
                r["subject_decoded"] = sj
        hit = [r for r in rows if f"{uu}@example.com" in str(r.get("to"))
               and "构建完成" in r.get("subject_decoded", "")]
        ok("SMTP 服务器实际收到构建通知", len(hit) >= 1, f"共 {len(rows)} 封")
        if hit:
            body = hit[-1]["body"]
            ok("通知含下载链接", "/dl/t/" in body)
            ok("通知含有效期说明", "24 小时" in body or "小时内有效" in body)
            ok("通知含文件名", "openwrt-" in body)

    # ---------- 9. 清理 ----------
    print("\n[9] 清理测试数据")
    post(ad, "/api/v1/admin/tokens", {"action": "revoke_build", "request_hash": "e2eprobe"})
    st, r = post(ad, "/api/v1/admin/user", {"username": uu, "action": "delete"})
    ok("测试用户已删除", r.get("status") == "ok" or st == 200)
    st, r = post(ad, "/api/v1/admin/build", {"request_hash": jid, "action": "delete"})
    ok("测试构建已删除", st == 200)
    # 恢复测试前的后端设置
    post(ad, "/api/v1/admin/site", js={"builder.backend": _saved_gh["backend"]})
    st, bi3 = get(ad, "/api/v1/admin/build_backend")
    ok("后端配置已恢复", bi3.get("configured") == _saved_gh["backend"],
       f"configured={bi3.get('configured')} effective={bi3.get('effective')}")

    # 逐项还原站点配置（只写回真正被改动的项），杜绝测试污染真实配置
    _now = _SS.all_values()
    _changed = {k: v for k, v in _snap.items() if _now.get(k) != v}
    for k, v in _changed.items():
        _SS.set_(k, v)
    _after = _SS.all_values()
    _still = [k for k in _changed if _after.get(k) != _snap[k]]
    ok("站点配置已完整还原", not _still,
       f"还原 {len(_changed)} 项" + (f"，残留差异 {_still}" if _still else ""))

    print("\n" + "=" * 72)
    print(f"结果: 通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项:")
        for f in FAIL:
            print("  ✗", f)
        sys.exit(1)
    print("全部通过 ✓")
    print("=" * 72)


if __name__ == "__main__":
    main()