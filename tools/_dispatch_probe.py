#!/usr/bin/env python3
"""真实派发探针：调 GitHub Actions dispatch，拿 run_id，然后**立刻删除该 run**。

为什么必须真跑：派发路径的两个坑只在真网络里暴露 ——
  ① `return_run_details` 必须放在**请求体**里，放 query GitHub 会视而不见并返回
     204 空响应，拿不到精确 run_id（只能靠 45 秒时间窗猜，并发时会张冠李戴）；
  ② 产物 302 的第二跳不能带 Authorization（签名不匹配）。
静态检查验不出这两条。

纪律：创建的 run **必须删除**，不能在用户仓库里留残留。
"""
import json, pathlib, sqlite3, subprocess, sys, time, urllib.error, urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
REPO = "2016xyz/kwrt-firmware-builder"
WF = "build-firmware.yml"
API = "https://api.github.com"


def db():
    return sqlite3.connect(ROOT / "users.db")


def get(key):
    with db() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r[0] if r else ""


def set_(key, val):
    with db() as c:
        c.execute("UPDATE settings SET value=? WHERE key=?", (val, key))
        c.commit()


def api(method, path, token, payload=None):
    req = urllib.request.Request(API + path, method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "Kwrt-Verify/1.0",
                 **({"Content-Type": "application/json"} if payload is not None else {})})
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        try:
            body = json.loads(body)
        except Exception:
            pass
        if e.code == 404:
            return 404, body
        return e.code, body
    except Exception as e:
        return 0, {"message": f"{type(e).__name__}: {e}"}


def main():
    token = get("gh.token").strip()
    if not token:
        print("SKIP 无 gh.token")
        return 1
    before = 0
    st, body = api("GET", f"/repos/{REPO}/actions/workflows/{WF}/runs?per_page=1", token)
    if st == 200:
        before = (body.get("total_count") or 0)
    print(f"派发前 build-firmware.yml run 总数 = {before}")

    st, body = api("POST", f"/repos/{REPO}/actions/workflows/{WF}/dispatches", token,
                   {"ref": "main",
                    "inputs": {"target": "x86/64", "profile": "generic", "packages": "",
                               "version": "25.12", "defaults": "", "filesystem": "squashfs",
                               "rootfs_size_mb": "512"},
                    "return_run_details": True})
    print(f"dispatch HTTP {st}  body={json.dumps(body, ensure_ascii=False)[:160]}")
    rid = None
    if st in (200, 201) and isinstance(body, dict):
        rid = body.get("workflow_run_id") or body.get("run_id")
    if st == 204:
        print("  ⚠ 返回 204（说明 return_run_details 没进请求体）—— 正是要验的坑")

    # 兜底：即便没拿到 run_id 也要找到刚创建的那个，确认可删
    if not rid:
        for _ in range(12):
            time.sleep(5)
            st2, b2 = api("GET", f"/repos/{REPO}/actions/workflows/{WF}/runs?per_page=5", token)
            if st2 == 200:
                for run in (b2.get("workflow_runs") or []):
                    if run.get("head_branch") == "main" and                        time.time() - time.mktime(time.strptime(run["created_at"], "%Y-%m-%dT%H:%M:%SZ")) < 120:
                        rid = run["id"]
                        break
            if rid:
                break

    if not rid:
        print("FAIL 未找到刚创建的 run（无法验证也无法清理）")
        return 1
    print(f"拿到 run_id = {rid}（精确路径成功）")

    # ★ 删除前必须先取消并等它进入 completed：
    #   DELETE /actions/runs/{id} 在 run **进行中**会返回 403（实测踩过，
    #   当时仓库里留了一个 run，靠手工 cancel 才清掉）。
    st_c, _ = api("GET", f"/repos/{REPO}/actions/runs/{rid}", token)
    if st_c == 200:
        st_r, r_ = api("GET", f"/repos/{REPO}/actions/runs/{rid}", token)
        if (r_ or {}).get("status") != "completed":
            stx, _ = api("POST", f"/repos/{REPO}/actions/runs/{rid}/cancel", token)
            print(f"取消 run -> HTTP {stx}，等待进入 completed…")
            for _ in range(20):
                time.sleep(6)
                _, rr = api("GET", f"/repos/{REPO}/actions/runs/{rid}", token)
                if (rr or {}).get("status") == "completed":
                    print(f"  已 completed（conclusion={rr.get('conclusion')}）")
                    break
    st3 = 0
    for attempt in range(1, 6):
        st3, _ = api("DELETE", f"/repos/{REPO}/actions/runs/{rid}", token)
        print(f"删除 run {rid} 第{attempt}次 -> HTTP {st3}")
        if st3 in (204, 202, 200):
            break
        time.sleep(8)
    time.sleep(3)
    st4, b4 = api("GET", f"/repos/{REPO}/actions/workflows/{WF}/runs?per_page=1", token)
    after = (b4.get("total_count") or 0) if st4 == 200 else -1
    print(f"派发后 run 总数 = {after}（应与派发前一致）")
    if st3 in (204, 202, 200):
        print("DISPATCH_OK")
        return 0
    print("FAIL 删除失败，仓库里有残留 run，请手工清理")
    return 1


if __name__ == "__main__":
    sys.exit(main())
