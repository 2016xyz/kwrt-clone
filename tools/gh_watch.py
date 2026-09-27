#!/usr/bin/env python3
"""跟踪 GitHub Actions run 直到完成，输出逐步结果与产物清单。"""
import json, os, re, sys, time, urllib.error, urllib.request

TOK = os.environ.get("GH_TOKEN") or ""
REPO = os.environ.get("GH_REPO", "2016xyz/kwrt-firmware-builder")
H = {"Authorization": f"Bearer {TOK}", "Accept": "application/vnd.github+json",
     "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "Kwrt-Builder/1.0"}
API = "https://api.github.com"


def get(path, raw=False):
    r = urllib.request.Request(API + path, headers=H)
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            d = resp.read()
            return resp.status, (d if raw else json.loads(d or b"{}"))
    except urllib.error.HTTPError as e:
        return e.code, (None if raw else {"err": e.read().decode("utf-8", "ignore")[:200]})


def logs_for(job_id):
    """GitHub 日志重定向到 Azure Blob，需手动跟随（且不能带认证头）。"""
    class NoRedir(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    op = urllib.request.build_opener(NoRedir)
    req = urllib.request.Request(f"{API}/repos/{REPO}/actions/jobs/{job_id}/logs", headers=H)
    try:
        op.open(req, timeout=60)
        return ""
    except urllib.error.HTTPError as e:
        loc = e.headers.get("Location")
        if not loc:
            return ""
        r2 = urllib.request.Request(loc, headers={"User-Agent": "curl/8"})
        with urllib.request.urlopen(r2, timeout=120) as resp:
            return resp.read().decode("utf-8", "ignore")


def main():
    rid = sys.argv[1] if len(sys.argv) > 1 else open("/tmp/ghrun.txt").read().strip()
    print(f"跟踪 run #{rid}\n", flush=True)
    last = None
    for i in range(180):
        st, b = get(f"/repos/{REPO}/actions/runs/{rid}")
        if st != 200:
            print(f"  [{i}] 查询失败 HTTP {st}"); time.sleep(15); continue
        s, c = b.get("status"), b.get("conclusion")
        if (s, c) != last:
            print(f"  [{i}] {s} / {c or '-'}   started={b.get('run_started_at','')}", flush=True)
            last = (s, c)
        if s == "completed":
            print(f"\n{'='*70}\n构建结束: {c}\n{'='*70}")
            st, jobs = get(f"/repos/{REPO}/actions/runs/{rid}/jobs")
            failed_step = None
            if st == 200:
                for j in jobs.get("jobs", []):
                    print(f"\njob: {j['name']}  → {j.get('conclusion')}  ({j.get('started_at')} ~ {j.get('completed_at')})")
                    for step in j.get("steps", []):
                        mk = {"success": "✓", "failure": "✗", "skipped": "-",
                              "cancelled": "x"}.get(step.get("conclusion"), "?")
                        print(f"   {mk} {step.get('name')}  [{step.get('conclusion')}]")
                        if step.get("conclusion") == "failure" and not failed_step:
                            failed_step = (j["id"], step.get("name"))
            if failed_step:
                print(f"\n{'='*70}\n失败步骤日志: {failed_step[1]}\n{'='*70}")
                txt = logs_for(failed_step[0])
                lines = [re.sub(r"\x1b\[[0-9;]*m", "", l) for l in txt.split("\n")]
                # 定位该步骤起始
                start = 0
                for i2, l in enumerate(lines):
                    if failed_step[1] in l:
                        start = i2
                print("\n".join(x[:200] for x in lines[max(0, start - 2):start + 60]))
            st, arts = get(f"/repos/{REPO}/actions/runs/{rid}/artifacts")
            if st == 200:
                print(f"\n{'='*70}\n产物\n{'='*70}")
                for a in arts.get("artifacts", []):
                    print(f"  {a['name']}  {a['size_in_bytes']/1048576:.1f} MB  "
                          f"expired={a['expired']}  id={a['id']}")
            return
        time.sleep(15)
    print("轮询超时")


if __name__ == "__main__":
    main()