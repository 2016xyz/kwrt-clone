#!/usr/bin/env python3
"""
抓取 openwrt.ai 对外公开元数据，落地为本地离线数据集（供克隆站直接服务）。
使用 curl 子进程（HTTP/2 + 浏览器指纹，绕过 Cloudflare 对 urllib 的拦截），低并发 + 指数退避。
已存在的文件默认跳过（--force 覆盖）。
"""
import json, os, subprocess, sys, time, concurrent.futures as cf

BASE = "https://openwrt.ai"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
LANGS = ["zh-cn", "en", "ca", "es", "de", "fr", "it", "no", "pl", "tr", "ko"]
FORCE = "--force" in sys.argv


def curl(path, retries=8):
    """返回 (bytes|None, status)。单连接、节流、指数退避，规避 Cloudflare 瞬时限流。"""
    url = BASE + path
    for i in range(retries):
        p = subprocess.run(
            ["curl", "-s", "-L", "--max-time", "45", "--http2", "-A", UA,
             "-H", "Referer: " + BASE + "/", "-H", "Accept: */*",
             "-w", "\n%{http_code}", url],
            capture_output=True)
        if p.returncode == 0 and p.stdout:
            body = p.stdout
            code = body.rsplit(b"\n", 1)[-1].decode(errors="ignore").strip()
            data = body[: body.rfind(b"\n")]
            if code == "200" and data:
                time.sleep(0.35)          # 主动节流
                return data, 200
            if code == "404":
                time.sleep(0.35)
                return None, 404
        time.sleep(min(20, 1.0 * (1.6 ** i)))
    return None, 0


def save(path, data):
    dst = os.path.join(OUT, path)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, dst)
    return len(data)


def fetch_one(rel):
    """rel 形如 json/v1/... 或 langs/...；返回 (rel, status, bytes)"""
    dst = os.path.join(OUT, rel)
    if os.path.exists(dst) and not FORCE:
        return (rel, 304, os.path.getsize(dst))
    data, code = curl("/" + rel)
    if code == 200:
        return (rel, 200, save(rel, data))
    return (rel, code, 0)


def log(*a):
    print(*a, flush=True)


def main():
    os.makedirs(OUT, exist_ok=True)

    # 1) 语言
    for l in LANGS:
        rel, code, n = fetch_one(f"langs/{l}.json")
        log(f"[lang] {l} {code} {n}b")

    # 2) 总览
    rel, code, n = fetch_one("json/v1/overview.json")
    if code not in (200, 304):
        log("FATAL: overview.json unavailable ->", code)
        return
    ov = json.load(open(os.path.join(OUT, "json/v1/overview.json")))
    fetch_one("json/v1/device_box.json")
    log("[overview] branches:", list(ov.get("branches", {}).keys()))

    jobs = []          # (rel)
    for v in ov.get("latest", []):
        b = ov["branches"][v]
        path, ppath = b["path"], b.get("path_packages", "packages-" + v)
        log(f"\n=== {v} ({path}) ===")

        rel, code, n = fetch_one(f"json/v1/{path}/overview.json")
        if code not in (200, 304):
            log("  overview FAIL", code)
            continue
        d = json.load(open(os.path.join(OUT, f"json/v1/{path}/overview.json")))
        profs = d.get("profiles", [])
        log(f"  profiles: {len(profs)}")

        # 设备详情
        devs = [(f"json/v1/{path}/targets/{p['target']}/{p['id']}.json") for p in profs]
        ok = err = 0
        arches = set()
        with cf.ThreadPoolExecutor(max_workers=4) as ex:
            for rel, code, n in ex.map(fetch_one, devs):
                if code in (200, 304):
                    ok += 1
                else:
                    err += 1
                    if err <= 5:
                        log("   dev FAIL", rel, code)
        log(f"  devices: ok={ok} err={err}")

        # 收集架构 + targets -> 需要 index.json / 包索引
        targets = set()
        for dirpath, _, files in os.walk(os.path.join(OUT, f"json/v1/{path}/targets")):
            for f in files:
                if f.endswith(".json") and f != "index.json":
                    try:
                        dd = json.load(open(os.path.join(dirpath, f)))
                        targets.add(dd["target"])
                        if dd.get("arch_packages"):
                            arches.add(dd["arch_packages"])
                    except Exception:
                        pass

        jobs += [(f"json/v1/{path}/targets/{t}/index.json") for t in sorted(targets)]
        jobs += [(f"json/v1/releases/{ppath}/{a}-index.json") for a in sorted(arches)]
        log(f"  targets={len(targets)} arches={sorted(arches)}")

    log(f"\n=== fetching {len(jobs)} index files (4 workers) ===")
    ok = err = 0
    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        for rel, code, n in ex.map(fetch_one, jobs):
            if code in (200, 304):
                ok += 1
            else:
                err += 1
                log("  index FAIL", rel, code)
    log(f"indexes: ok={ok} err={err}")

    total = size = 0
    for r, _, fs in os.walk(OUT):
        for f in fs:
            total += 1
            size += os.path.getsize(os.path.join(r, f))
    log(f"\nDONE files={total} size={size/1048576:.1f}MB -> {OUT}")


if __name__ == "__main__":
    main()
