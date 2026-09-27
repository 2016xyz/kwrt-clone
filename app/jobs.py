"""任务持久化：进程重启后仍可查询/续跑排队中的构建任务。"""
import json
import os
import sqlite3
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "users.db")


from contextlib import closing

# 任务状态机：终态一旦写入就不允许被非终态覆盖（U-4）。
# 允许的跃迁：queued → running/started/done/failed
#             running|started → done/failed
#             done|failed|cancelled → 仅允许同值或 force=True 覆盖
TERMINAL = ("done", "failed", "cancelled", "ok", "success", "error")
_ACTIVE = ("queued", "running", "started")
_RESULT_MAX = 800_000


def _trim_result(payload, limit=_RESULT_MAX):
    """把结果压到 limit 以内，且**始终保持合法 JSON**。

    为什么不能用切片截断：无论 `[-N:]` 还是 `[:N]`，切一刀都会得到非法 JSON
    （如 `{"files":[...` 缺右括号），`get()` 里 `json.loads` 抛错后会把**整个
    结果静默丢弃** —— 表现为「构建明明成功，接口却查不到产物」。
    这里改为优先裁剪最大的文本字段（stdout/stderr），并显式标记已裁剪。
    """
    d = dict(payload or {})
    text = json.dumps(d, ensure_ascii=False)
    if len(text) <= limit:
        return text
    d["_truncated"] = True
    # 先整体砍掉最占空间的两块日志，再从大到小继续削
    for key in ("stdout", "stderr"):
        if key in d and isinstance(d[key], str):
            d[key] = d[key][:_RESULT_MAX // 8]
        if len(json.dumps(d, ensure_ascii=False)) <= limit:
            return json.dumps(d, ensure_ascii=False)
    # 仍超限：缩短 files 里每项的字段 + 收缩 packages
    if isinstance(d.get("files"), list):
        slim = []
        for it in d["files"]:
            if isinstance(it, dict):
                slim.append({k: (str(v)[:300] if isinstance(v, str) else v)
                             for k, v in it.items() if k in
                             ("name", "size", "sha256", "path", "url", "external")})
        d["files"] = slim
    if isinstance(d.get("packages"), list):
        d["packages"] = [str(x)[:120] for x in d["packages"][:1000]]
    text = json.dumps(d, ensure_ascii=False)
    if len(text) <= limit:
        return text
    # 最后兜底：仍然合法的最小对象，至少保留定位信息
    return json.dumps({"_truncated": True,
                       "files": d.get("files", [])[:50],
                       "detail": str(d.get("detail", ""))[:500]}, ensure_ascii=False)


def conn():
    """打开连接。

    ⚠ 调用方必须用 `with closing(conn()) as c:` —— sqlite3 的 `with conn`
    只负责提交/回滚事务，**不会关闭连接**，原写法每次调用都泄漏一个 fd。
    """
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def init():
    con = conn()
    try:
        with con:
            con.execute("""CREATE TABLE IF NOT EXISTS jobs(
            request_hash TEXT PRIMARY KEY,
            status TEXT, detail TEXT, payload TEXT,
                result TEXT, created REAL, updated REAL)""")
    finally:
        con.close()


def put(job, force=False):
    """写入/更新任务。

    U-4：加状态机守卫。原实现用 `INSERT OR REPLACE` 无条件覆盖，
    任何一次迟到的 `put(status="queued")` 都能把已 `done` 的任务打回排队，
    已完成的构建被当成未完成重新跑（重复消耗算力，且结果被覆盖）。
    """
    init()
    new_status = job.get("status", "queued")
    con = conn()
    try:
        with con:                       # 事务：提交/回滚
            if not force:
                row = con.execute(
                    "SELECT status FROM jobs WHERE request_hash=?",
                    (job["request_hash"],)).fetchone()
                if row is not None:
                    old = row["status"] or ""
                    # 终态不可被非终态覆盖；同态或向终态收敛则放行
                    if old in TERMINAL and new_status not in TERMINAL:
                        print(f"[jobs] 拒绝状态回退 {job['request_hash'][:12]} "
                              f"{old} → {new_status}（终态不可被覆盖）", flush=True)
                        return False
                    # 顺带拦住 done → failed 这类同层翻覆（除非显式 force）
                    if old in ("done", "ok", "success") and new_status in ("failed", "error"):
                        print(f"[jobs] 拒绝终态翻覆 {job['request_hash'][:12]} "
                              f"{old} → {new_status}", flush=True)
                        return False
            con.execute("INSERT OR REPLACE INTO jobs(request_hash,status,detail,payload,result,created,updated) "
                        "VALUES(?,?,?,?,?,?,?)",
                      (job["request_hash"], new_status,
                       job.get("imagebuilder_status", ""),
                       json.dumps(job.get("req", {}), ensure_ascii=False),
                       _trim_result({k: v for k, v in job.items()
                                     if k in ("files", "packages", "stdout", "stderr", "detail",
                                              "store_url", "duration", "target", "profile", "version")}),
                       job.get("created", time.time()), time.time()))
    finally:
        con.close()
    return True


def get(hash_):
    init()
    con = conn()
    try:
        r = con.execute("SELECT * FROM jobs WHERE request_hash=?", (hash_,)).fetchone()
    finally:
        con.close()
    if not r:
        return None
    out = {"request_hash": r["request_hash"], "status": r["status"],
           "imagebuilder_status": r["detail"]}
    try:
        out["req"] = json.loads(r["payload"] or "{}")
    except Exception:
        out["req"] = {}
    try:
        out.update(json.loads(r["result"] or "{}"))
    except Exception:
        pass
    return out


def pending():
    """未完成的真实构建任务（用于重启后续跑）。
    超过 12 小时仍未完成的视为陈旧，标记失败，避免重启后重复排队。"""
    init()
    cutoff = time.time() - 43200
    con = conn()
    try:
        with con:
            con.execute("UPDATE jobs SET status='failed' WHERE status IN ('queued','running','started') "
                        "AND created < ?", (cutoff,))
            rows = con.execute("SELECT * FROM jobs WHERE status IN ('queued','running','started') "
                               "ORDER BY created").fetchall()
    finally:
        con.close()
    out = []
    for r in rows:
        try:
            req = json.loads(r["payload"] or "{}")
        except Exception:
            continue
        if req.get("target"):
            out.append({"request_hash": r["request_hash"], "req": req,
                        "status": r["status"], "created": r["created"]})
    return out
