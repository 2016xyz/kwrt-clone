#!/usr/bin/env python3
"""端到端测试：真实走一遍 元数据 -> 构建 -> 轮询 -> 下载 的完整链路。"""
import base64, json, os, sys, time, urllib.request, urllib.error, urllib.parse, http.cookiejar

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8443"


def verif():
    j = json.dumps({"uuid": "12345678-1234-4000-8000-123456789012"})
    return base64.b64encode(bytes(ord(c) ^ 80 for c in j)).decode()


def req(method, path, body=None, headers=None):
    h = {"Ng-One-Time-Verif-Value": verif()}
    h.update(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    r = urllib.request.Request(BASE + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=120) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def main():
    ok = True
    print("=" * 70)
    print("1) 元数据接口")
    st, _, b = req("GET", "/json/v1/overview.json")
    ov = json.loads(b)
    print(f"   overview.json  {st}  branches={list(ov['branches'])}")

    st, _, b = req("GET", "/json/v1/releases/25.12/overview.json")
    rel = json.loads(b)
    print(f"   releases overview  {st}  profiles={len(rel['profiles'])}")
    assert st == 200 and len(rel["profiles"]) > 0

    dev = None
    for p in rel["profiles"]:
        if p["id"] == "generic" and p["target"] == "x86/64":
            dev = p
    print(f"   目标设备: {dev['target']} / {dev['id']}")

    st, _, b = req("GET", f"/json/v1/releases/25.12/targets/{dev['target']}/{dev['id']}.json")
    dd = json.loads(b)
    print(f"   设备详情  {st}  arch={dd['arch_packages']} kernel={dd['linux_kernel']['version']} images={len(dd['images'])}")
    assert st == 200

    st, _, b = req("GET", "/json/v1/releases/25.12/targets/x86/64/index.json")
    idx = json.loads(b)
    print(f"   target index  {st}  packages={len(idx.get('packages', {}))}")

    st, _, b = req("GET", "/json/v1/releases/packages-25.12/x86_64-index.json")
    pk = json.loads(b)
    print(f"   架构包索引  {st}  packages={len(pk)}")

    print("=" * 70)
    print("2) 一次性校验（负例）")
    r = urllib.request.Request(BASE + "/api/v1/build", method="OPTIONS",
                               headers={"Ng-One-Time-Verif-Value": "bogus"})
    try:
        urllib.request.urlopen(r, timeout=30)
        print("   ✗ 伪造校验值竟然通过"); ok = False
    except urllib.error.HTTPError as e:
        print(f"   ✓ 伪造校验值被拒: HTTP {e.code}")

    print("=" * 70)
    print("3) 提交真实构建任务 (POST /api/v1/build)")
    VER = os.environ.get("E2E_VERSION", "")
    PKGS = os.environ.get("E2E_PACKAGES", "luci-app-aria2,luci-app-ddns,luci-app-uhttpd,luci-ssl").split(",")
    print(f"   version={VER or '(默认)'} packages={PKGS}")
    payload = {
        "target": "x86/64",
        "profile": "generic",
        "packages": PKGS,
        "defaults": ('uci -q set system.@system[0].hostname="Kwrt-E2E"\nuci commit system\n'
                     'uci -q set network.lan.ipaddr="10.9.9.1"\nuci commit network'),
        "filesystem": "squashfs",
        "rootfs_size_mb": 512,
        "version": VER,
        "settings": {"theme": "argon", "webserver": "uhttpd", "ipv6": True},
    }
    st, hdr, b = req("POST", "/api/v1/build", payload)
    print(f"   POST -> HTTP {st}  {b[:200].decode()}")
    assert st == 202, st
    job = json.loads(b)
    jid = job["request_hash"]

    print("=" * 70)
    print("4) 轮询任务状态")
    t0 = time.time()
    last = None
    while True:
        st, _, b = req("GET", f"/api/v1/build/{jid}")
        j = json.loads(b)
        cur = (j.get("status"), j.get("imagebuilder_status"))
        if cur != last:
            print(f"   [{time.time()-t0:6.1f}s] status={j.get('status')}  {j.get('imagebuilder_status','')}")
            last = cur
        if j.get("status") in ("done", "failed"):
            break
        if time.time() - t0 > 2400:
            print("   ✗ 超时"); ok = False; break
        time.sleep(6)

    if j.get("status") != "done":
        print("   ✗ 构建失败:", (j.get("stderr") or "")[-800:]); ok = False
    else:
        files = j.get("files", [])
        print(f"   ✓ 构建完成 {j.get('duration')}s，产出 {len(files)} 个固件")
        for f in files:
            print(f"      {f['name']:<58} {f['size']/1048576:7.1f}MB  {f['type']:<12} {f['sha256'][:16]}…")
        print(f"   packages 清单: {len(j.get('packages', []))} 个")
        assert len(files) > 0

        print("=" * 70)
        print("5) 下载产物并校验 sha256")
        # 优先挑 gz 镜像做头部取证
        cands = [f for f in files if f["name"].endswith(".img.gz")] or \
                sorted(files, key=lambda x: -x["size"])
        f0 = cands[0]
        url = f"{BASE}/store/{jid}/{f0['name']}"
        with urllib.request.urlopen(url, timeout=600) as r:
            data = r.read()
        import hashlib
        h = hashlib.sha256(data).hexdigest()
        print(f"   下载 {f0['name']}  {len(data)} bytes")
        print(f"   实测 sha256 = {h}")
        print(f"   记录 sha256 = {f0['sha256']}")
        if h == f0["sha256"]:
            print("   ✓ 哈希一致，产物可下载且完整")
        else:
            print("   ✗ 哈希不一致"); ok = False

        # 校验产物确实是 openwrt 镜像（容忍截断的部分解压）
        if f0["name"].endswith(".gz"):
            import zlib
            head = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(data[:1 << 20])[:512]
            print(f"   镜像头部: {head[:24]!r} (b'\\x00'* 填充 + 分区表特征)")
            if head[:2] in (b"\x00\x00", b"\xeb", b"MB"):
                print("   ✓ 头部为可引导磁盘镜像特征")
        st, _, b = req("GET", f"/store/{jid}/")
        print(f"   目录索引 HTTP {st}")

    print("=" * 70)
    print("6) 登录 / 赞助态 / 配额")
    import http.cookiejar
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    form = urllib.parse.urlencode({"username": "sponsor", "password": "sponsor123"}).encode()
    r = urllib.request.Request(BASE + "/api/v1/login", data=form,
                               headers={"Content-Type": "application/x-www-form-urlencoded"})
    with op.open(r, timeout=30) as resp:
        d = json.loads(resp.read())
    print(f"   登录 sponsor -> {d}")

    # 未登录配额限制（配额可由管理员配置，读取真实值再验证）
    lim = 12
    try:
        with urllib.request.urlopen(BASE + "/api/v1/announcement", timeout=15) as r:
            pass
    except Exception:
        pass
    try:
        with urllib.request.urlopen(BASE + "/healthz", timeout=15) as r:
            pass
    except Exception:
        pass
    adm = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    f = urllib.parse.urlencode({"username": "admin", "password": "admin123"}).encode()
    try:
        with adm.open(urllib.request.Request(BASE + "/api/v1/login", data=f, headers={
                "Content-Type": "application/x-www-form-urlencoded"}), timeout=30) as resp:
            if json.loads(resp.read()).get("is_admin"):
                with adm.open(BASE + "/api/v1/admin/settings", timeout=30) as resp:
                    lim = int(json.loads(resp.read()).get("default_quota", 12))
    except Exception as e:
        print(f"   (读取管理员配额失败，按默认 12 校验: {e})")

    many = ["luci-app-x%d" % i for i in range(lim + 3)]
    st, _, b = req("POST", "/api/v1/build", {"target": "x86/64", "profile": "generic", "packages": many})
    print(f"   未登录 {len(many)} 个 luci-app（限 {lim}）-> HTTP {st}  {b[:110].decode()}")
    if st != 400:
        print("   ✗ 配额未生效"); ok = False
    else:
        print("   ✓ 配额限制生效")

    st, _, b = req("POST", "/api/v1/build", {"target": "x86/64", "profile": "generic",
                                             "packages": ["luci-app-aria2"]})
    print(f"   登录 sp... (匿名) 1 个包 -> HTTP {st}")

    print("=" * 70)
    print("结果:", "全部通过 ✓" if ok else "存在失败 ✗")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
