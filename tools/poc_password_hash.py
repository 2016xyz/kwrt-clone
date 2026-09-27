#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PoC：口令存储强度 —— 注册 / 管理员建号 / 管理员重置 是否写入无盐 SHA-256？

背景（实测发现）：
  users.db 里注册产生的用户（tester01/test1）password 是 **64 位无盐 hex**，
  而 admin / sponsor / user 是 `pbkdf2_sha256$240000$<盐>$<哈希>`。
  也就是说 main.py 有三处写口令漏用了 _hash_pw()，写成 hashlib.sha256(pw)。

影响：
  · 无盐 → 彩虹表可直接查；单轮 SHA256 → GPU 每秒几十亿次，弱口令秒破
  · 相同口令 → 哈希完全相同，一处破解即一片沦陷
  · 登录时才自动升级，**从未登录过的用户会一直是弱哈希**

本脚本用真实 HTTP 接口走一遍，直接查库确认落库形态。不猜测。

用法：python3 tools/poc_password_hash.py
"""
import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f"  → {extra}" if extra and not cond else ""))


print("=" * 74)
print("PoC：口令存储强度（无盐 SHA-256 vs PBKDF2+盐）")
print("=" * 74)

# ── 1. 纯逻辑验证：_hash_pw / _verify_pw 的行为（不启服务，直接调）──────────
sys.path.insert(0, ROOT)
os.environ.setdefault("KWRT_ADMIN_PASSWORD", "Adm1n@Kwrt2026")

TMP = tempfile.mkdtemp(prefix="kwrt_pwhash_")
shutil.copy(os.path.join(ROOT, "users.db"), os.path.join(TMP, "users.db")) \
    if os.path.exists(os.path.join(ROOT, "users.db")) else None
shutil.copytree(os.path.join(ROOT, "app"), os.path.join(TMP, "app"), dirs_exist_ok=True)
shutil.copytree(os.path.join(ROOT, "web"), os.path.join(TMP, "web"), dirs_exist_ok=True)
for d in ("data", "store", "config.json"):
    src = os.path.join(ROOT, d)
    if os.path.isdir(src):
        shutil.copytree(src, os.path.join(TMP, d), dirs_exist_ok=True)
    elif os.path.isfile(src):
        shutil.copy(src, os.path.join(TMP, d))

import importlib  # noqa: E402
sys.path.insert(0, TMP)
os.chdir(TMP)
import app.main as M  # noqa: E402
importlib.reload(M)

print("\n── A. _hash_pw 输出形态 ──")
h = M._hash_pw("Password123")
check("★ 输出带 pbkdf2_sha256 前缀", h.startswith("pbkdf2_sha256$"), h[:40])
check("★ 含迭代次数", h.split("$")[1].isdigit(), h[:40])
check("★ 含盐（长度足够）", len(h.split("$")[2]) >= 16, h[:60])
check("★ 相同口令两次哈希不同（有盐）", M._hash_pw("Password123") != h)
check("★ 正确口令可校验通过", M._verify_pw("Password123", h)[0])
check("★ 错误口令被拒", not M._verify_pw("wrong", h)[0])
check("★ 已是强哈希时不再要求升级", M._verify_pw("Password123", h)[1] is False)

print("\n── B. 历史无盐哈希仍可登录，且标记需要升级 ──")
legacy = hashlib.sha256(b"OldPass123").hexdigest()
ok, need = M._verify_pw("OldPass123", legacy)
check("★ 历史无盐哈希仍能通过（不能把老用户锁死）", ok)
check("★ 且被标记为需要升级重哈希", need is True)

# ── 2. 真实 HTTP：注册 / 管理员建号 / 管理员重置 ────────────────────────────
print("\n── C. 真实接口落库形态（这是关键）──")
from fastapi.testclient import TestClient  # noqa: E402
c = TestClient(M.app)
DB = os.path.join(TMP, "users.db")


def stored(username):
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    r = con.execute("SELECT password FROM users WHERE username=?", (username,)).fetchone()
    con.close()
    return r[0] if r else None


def login(u, p):
    return c.post("/api/v1/login", data={"username": u, "password": p})


# 注册（KWRT 的注册走 /api/v1/register）
r = c.post("/api/v1/register", data={"username": "regprobe", "password": "RegPass123!",
                                     "email": "regprobe@example.com"})
check("注册接口被调用（非 5xx）", r.status_code < 500, f"{r.status_code} {r.text[:120]}")
sv = stored("regprobe")
if sv is None:
    # 注册可能需要邮箱验证流程，退一步：直接看已有注册用户的形态
    check("★ 注册路径落库为 PBKDF2+盐（而非无盐 SHA256）",
          False, "未创建成功，见上一条")
else:
    check("★ ★ 注册路径落库为 PBKDF2+盐（而非无盐 SHA256）",
          sv.startswith("pbkdf2_sha256$"),
          f"实际落库：len={len(sv)} {sv[:36]}")

# 管理员建号
login("admin", os.environ["KWRT_ADMIN_PASSWORD"])
r = c.post("/api/v1/admin/user/create",
           data={"username": "adminmade", "password": "MadePass123", "role": "user"})
check("管理员建号接口被调用", r.status_code < 500, f"{r.status_code} {r.text[:120]}")
sv = stored("adminmade")
if sv:
    check("★ ★ 管理员建号落库为 PBKDF2+盐",
          sv.startswith("pbkdf2_sha256$"), f"len={len(sv)} {sv[:36]}")

# 管理员重置密码
r = c.post("/api/v1/admin/user/op",
           data={"username": "regprobe" if stored("regprobe") else "adminmade",
                 "action": "reset_password", "value": "ResetPass123"})
check("管理员重置接口被调用", r.status_code < 500, f"{r.status_code} {r.text[:120]}")
for un in ("regprobe", "adminmade"):
    sv = stored(un)
    if sv:
        check(f"★ ★ 重置后 {un} 落库为 PBKDF2+盐",
              sv.startswith("pbkdf2_sha256$"), f"len={len(sv)} {sv[:36]}")

print("\n── D. 全库扫描：还有没有无盐哈希残留 ──")
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
bad = [(r[0], r[1]) for r in con.execute("SELECT username, length(password) FROM users")
       if not str(r[1]) or r[1] == 64]
con.close()
check("★ 本库新写入的口令无 64 位无盐哈希",
      all(u in ("tester01", "test1") for u, _ in bad) or not bad,
      f"仍有：{bad}（历史数据，登录时自动升级）")

print("\n" + "=" * 74)
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
print("=" * 74)
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  -", f)

os.chdir(ROOT)
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAIL else 0)
