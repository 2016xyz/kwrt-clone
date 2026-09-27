#!/usr/bin/env python3
"""把测试期间改过的 GitHub 设置还原（verify_remote_backend.py 的清理钩子）。"""
import sqlite3, sys, pathlib
DB = pathlib.Path(__file__).resolve().parent.parent / "users.db"
WANT = {"gh.repo": "own/repo", "gh.enabled": "0", "gh.queues": "[]"}
c = sqlite3.connect(DB)
for k, v in WANT.items():
    c.execute("UPDATE settings SET value=? WHERE key=?", (v, k))
c.commit()
print("restored:", ", ".join(WANT))
