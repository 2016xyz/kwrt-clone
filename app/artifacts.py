"""本地构建物仓库的管理：盘点 / 按构建删除 / 清理孤儿目录。

背景
----
`store/<request_hash>/` 下是回传或本地编译出的固件。管理端原先只有
「按构建记录删除」和「清空全部」两个动作，缺三件东西：

1. **看不见**：不知道哪个构建占了多大，无法判断该删谁。
2. **孤儿目录**：`builds` 行已不存在（重启后内存任务清空、或历史清理过），
   但目录还躺在磁盘上 —— 既不在「构建」列表里（没有行），也不被
   `purge_store` 之外任何动作清掉，会一直占盘。
3. **悬空下载令牌**：产物删了，`dl_tokens` 里指向它的限时链接还在，
   点开是 404，且令牌列表看起来「有货」。

本模块把这三件事收口。所有路径都做 realpath 归属校验，且**绝不触碰
`store/uploads/`**（那是用户上传的文件，不属于「构建物」）。
"""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STORE = os.path.join(ROOT, "store")
DB = os.path.join(ROOT, "users.db")

# 与 dl.HASH_RE / main.HASH_ID_RE 保持一致的字符集
HASH_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
UPLOADS = "uploads"

_LOCK = threading.RLock()


def _conn():
    con = sqlite3.connect(DB, timeout=15)
    con.row_factory = sqlite3.Row
    return con


def _dir_stat(path: str):
    """递归统计目录的字节数与文件数。返回 (bytes, files)。"""
    total = 0
    files = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for fn in filenames:
            fp = os.path.join(dirpath, fn)
            try:
                if os.path.isfile(fp) and not os.path.islink(fp):
                    total += os.path.getsize(fp)
                    files += 1
            except OSError:
                # 文件在遍历途中被删/权限变化：跳过，不让盘点整体失败
                continue
    return total, files


def _safe_dir(name: str):
    """把 `name` 解析为 STORE 下一个安全子目录；不合法或越界返回 None。"""
    if not name or name == UPLOADS or not HASH_RE.match(name):
        return None
    real = os.path.realpath(os.path.join(STORE, name))
    root = os.path.realpath(STORE)
    if real == root or not real.startswith(root + os.sep):
        return None
    if not os.path.isdir(real):
        return None
    return real


def scan():
    """盘点本地构建物，并与 builds 表比对，标记孤儿目录。"""
    with _LOCK:
        if not os.path.isdir(STORE):
            return {"items": [], "total_bytes": 0, "total_files": 0,
                    "orphan_bytes": 0, "orphan_count": 0}
        try:
            names = sorted(os.listdir(STORE))
        except OSError:
            names = []

        rows = {}
        try:
            con = _conn()
            try:
                for r in con.execute(
                        "SELECT request_hash, username, target, profile, status, created "
                        "FROM builds"):
                    rows[r["request_hash"]] = dict(r)
            finally:
                con.close()
        except sqlite3.Error:
            rows = {}

        items = []
        for name in names:
            d = _safe_dir(name)
            if d is None:
                continue
            size, files = _dir_stat(d)
            try:
                mtime = os.path.getmtime(d)
            except OSError:
                mtime = 0.0
            rec = rows.get(name)
            items.append({
                "request_hash": name,
                "bytes": size,
                "files": files,
                "mtime": mtime,
                "orphan": rec is None,
                "username": (rec or {}).get("username") or "",
                "target": (rec or {}).get("target") or "",
                "profile": (rec or {}).get("profile") or "",
                "status": (rec or {}).get("status") or "",
                "created": (rec or {}).get("created") or 0.0,
            })
        # 大的排前面 —— 管理端最关心「谁占盘」
        items.sort(key=lambda x: (-x["bytes"], x["request_hash"]))

        total_bytes = sum(i["bytes"] for i in items)
        total_files = sum(i["files"] for i in items)
        orphan = [i for i in items if i["orphan"]]
        return {
            "items": items,
            "total_bytes": total_bytes,
            "total_files": total_files,
            "orphan_bytes": sum(i["bytes"] for i in orphan),
            "orphan_count": len(orphan),
        }


def uploads_scan():
    """盘点用户上传目录（只读展示，不参与「构建物」删除）。"""
    base = os.path.join(STORE, UPLOADS)
    if not os.path.isdir(base):
        return {"bytes": 0, "files": 0, "staged": 0}
    size, files = _dir_stat(base)
    staged = 0
    st = os.path.join(base, "_staged")
    if os.path.isdir(st):
        try:
            staged = len([x for x in os.listdir(st) if os.path.isdir(os.path.join(st, x))])
        except OSError:
            staged = 0
    return {"bytes": size, "files": files, "staged": staged}


def _revoke_tokens(request_hash: str):
    """产物删了，指向它的限时令牌必须一起作废（否则点开是 404 的悬空链接）。"""
    try:
        con = _conn()
        try:
            with con:
                n = con.execute(
                    "UPDATE dl_tokens SET revoked=1 WHERE request_hash=?",
                    (request_hash,)).rowcount
            return n
        finally:
            con.close()
    except sqlite3.Error:
        return 0


def delete(request_hash: str, *, drop_record: bool = False):
    """删除单个构建的本地产物。

    drop_record=False —— 只删磁盘产物 + 作废令牌，保留构建记录
                        （用户仍能在「我的构建」里看到「产物已清理」）。
    drop_record=True  —— 连构建记录一起删。
    """
    with _LOCK:
        d = _safe_dir(request_hash)
        freed = 0
        files = 0
        if d is not None:
            freed, files = _dir_stat(d)
            shutil.rmtree(d, ignore_errors=True)
            # 删完复核：rmtree 用 ignore_errors，失败要如实反映
            existed_after = os.path.isdir(d)
        else:
            existed_after = False

        revoked = _revoke_tokens(request_hash)
        dropped = False
        try:
            con = _conn()
            try:
                with con:
                    if drop_record:
                        con.execute("DELETE FROM builds WHERE request_hash=?",
                                    (request_hash,))
                        dropped = True
                    # 只删产物时不动记录：管理端列表会按磁盘实际存在与否
                    # 显示 has_artifacts，从而自动隐藏失效的「产物」链接，
                    # 不需要在库里另加状态字段（原先这里是 UPDATE status=status
                    # 的空写入，已移除）。
            finally:
                con.close()
        except sqlite3.Error:
            pass
        return {"request_hash": request_hash, "freed": freed, "files": files,
                "revoked_tokens": revoked, "removed": not existed_after,
                "record_dropped": dropped}


def delete_orphans():
    """清理所有「磁盘有目录、DB 无记录」的孤儿产物目录。"""
    with _LOCK:
        sn = scan()
        gone = []
        freed = 0
        for it in sn["items"]:
            if not it["orphan"]:
                continue
            d = _safe_dir(it["request_hash"])
            if d is None:
                continue
            shutil.rmtree(d, ignore_errors=True)
            if not os.path.isdir(d):
                gone.append(it["request_hash"])
                freed += it["bytes"]
                _revoke_tokens(it["request_hash"])
        return {"removed": len(gone), "freed": freed, "hashes": gone}


def purge_all(*, keep_uploads: bool = True):
    """清空全部构建产物（保留 uploads）。同时作废所有下载令牌。"""
    with _LOCK:
        freed = 0
        removed = 0
        if os.path.isdir(STORE):
            try:
                names = list(os.listdir(STORE))
            except OSError:
                names = []
            for name in names:
                if keep_uploads and name == UPLOADS:
                    continue
                d = _safe_dir(name)
                if d is None:
                    continue
                sz, _ = _dir_stat(d)
                shutil.rmtree(d, ignore_errors=True)
                if not os.path.isdir(d):
                    freed += sz
                    removed += 1
        try:
            con = _conn()
            try:
                with con:
                    con.execute("DELETE FROM builds")
                    con.execute("UPDATE dl_tokens SET revoked=1")
            finally:
                con.close()
        except sqlite3.Error:
            pass
        return {"removed": removed, "freed": freed}
