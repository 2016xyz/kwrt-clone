"""第三方插件依赖预取。

把目标插件及其递归依赖从上游 feed 预先下载到 ImageBuilder 的本地包仓
（packages/ + dl/），使 opkg 无需在构建期访问外网：
  · 构建确定性 —— 不受上游限流影响
  · 构建速度 —— 重复构建不再下载数百 MB
"""
import gzip
import io
import os
import re
import shutil
import subprocess
import time
import urllib.request
import concurrent.futures as cf

UA = "Kwrt-Builder/1.0"
MAX_PKGS = 400
MAX_INDEX_BYTES = 64 * 1024 * 1024        # 单个索引响应上限（真实索引约 10MB）
MAX_INDEX_PLAIN = 256 * 1024 * 1024       # 解压后上限，防 gzip 炸弹

# 包文件名白名单：只允许安全字符，且不得含路径分隔符。
# 上游 Packages 索引里的 `Filename:` 字段是**远端可控**的 —— 实测把它写成
# "../../../../tmp/x.ipk" 时，`os.path.join(dldir, fn)` 会跳出下载目录并
# 在任意可写位置落文件（已复现）。索引一旦被投毒即等于任意文件写入。
_SAFE_FN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+~\-]{0,180}$")


def safe_filename(fn):
    """把索引里的 Filename 归一化为安全的纯文件名；不合法返回空串。"""
    fn = str(fn or "").strip()
    if "/" in fn or "\\" in fn or fn in (".", ".."):
        return ""
    if not _SAFE_FN_RE.match(fn):
        return ""
    if not fn.endswith((".ipk", ".apk")):      # 只接受包文件
        return ""
    return fn


def _get(url, timeout=60, retries=3, max_bytes=MAX_INDEX_BYTES):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read(max_bytes + 1)
                if len(data) > max_bytes:
                    raise ValueError(f"响应超过 {max_bytes} 字节上限")
                return data
        except urllib.error.HTTPError as e:
            # 4xx 是确定性错误（404/403 等），重试没有意义，直接放弃
            if 400 <= e.code < 500:
                raise RuntimeError(f"GET {url}: HTTP {e.code}")
            last = e
        except Exception as e:
            last = e
        time.sleep(1.5 * (2 ** i))       # 限流时退避重试
    raise RuntimeError(f"GET {url}: {last}")


def parse_index(raw):
    """解析 opkg Packages 索引为 {name: {version, filename, depends, provides, feed}}"""
    if raw[:2] == b"\x1f\x8b":
        # 限幅解压：gzip 炸弹（50KB → 50MB 实测可行）会把内存吃光。
        # gzip.decompress 本身无上限，所以手工按块读并累计判定。
        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as g:
            while True:
                chunk = g.read(1024 * 1024)
                if not chunk:
                    break
                buf.write(chunk)
                if buf.tell() > MAX_INDEX_PLAIN:
                    raise ValueError(f"索引解压后超过 {MAX_INDEX_PLAIN} 字节上限（疑似压缩炸弹）")
        raw = buf.getvalue()
    text = raw.decode("utf-8", "ignore")
    out = {}
    for block in text.split("\n\n"):
        if "Package:" not in block:
            continue
        rec = {}
        for line in block.splitlines():
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            rec[k.strip().lower()] = v.strip()
        name = rec.get("package")
        if not name:
            continue
        deps = []
        for d in re.split(r"[,\s]+", rec.get("depends", "")):
            d = d.strip()
            if not d:
                continue
            d = d.split("|")[0]              # 取首个可选依赖
            d = re.sub(r"\s*\(.*?\)\s*", "", d)
            if d:
                deps.append(d)
        provides = [p.strip() for p in re.split(r"[,\s]+", rec.get("provides", "")) if p.strip()]
        out[name] = {
            "name": name,
            "version": rec.get("version", ""),
            # 归一化：Filename 由远端索引提供，是**不可信输入**。
            # 不合法（含路径分隔符 / 越界 / 非常规字符）一律置空，
            # 下游会因 filename 为空而跳过该包，而不是把文件写到目录之外。
            "filename": safe_filename(rec.get("filename", "")),
            "depends": deps,
            "provides": provides,
        }
    return out


def load_feeds(branch, arch, base_url="https://dl.openwrt.ai"):
    """加载第三方 + 官方 feed 索引（合并为一个可解析空间）。"""
    feeds = [
        f"{base_url}/packages-{branch}/{arch}/kiddin9/Packages",
        f"{base_url}/packages-{branch}/{arch}/luci/Packages",
    ]
    index, urls = {}, {}
    for fu in feeds:
        try:
            idx = parse_index(_get(fu))
            for k, v in idx.items():
                v["url"] = fu.rsplit("/", 1)[0] + "/" + v["filename"]
                index.setdefault(k, v)
            print(f"[prefetch] feed ok: {fu} ({len(idx)} pkgs)", flush=True)
        except Exception as e:
            print(f"[prefetch] feed skip {fu}: {e}", flush=True)
    # provides -> 实际包名
    for k, v in list(index.items()):
        for p in v["provides"]:
            urls.setdefault(p, k)
    return index, urls


def load_repos_sources(ib, cache_ttl=86400):
    """
    按 ImageBuilder 实际配置的 repositories.conf 加载每个远程源，
    返回 [{base, index}] 列表（保持配置顺序）。
    """
    conf = os.path.join(ib, "repositories.conf")
    cache = os.path.join(os.path.dirname(ib), "feeds_cache")
    os.makedirs(cache, exist_ok=True)
    bases = []
    if os.path.exists(conf):
        with open(conf, encoding="utf-8", errors="replace") as fh:
            conf_lines = fh.read().splitlines()
        for line in conf_lines:
            line = line.strip()
            if line.startswith("src/gz"):
                parts = line.split()
                if len(parts) >= 3 and parts[2].startswith("http"):
                    bases.append(parts[2])
    out = []
    for base in bases:
        key = re.sub(r"[^A-Za-z0-9]+", "_", base)[-90:]
        cp = os.path.join(cache, f"{key}.gz")
        try:
            if os.path.exists(cp) and (time.time() - os.path.getmtime(cp)) < cache_ttl:
                with open(cp, "rb") as fh:
                    raw = fh.read()
            else:
                raw = _get(base + "/Packages.gz", timeout=120, retries=2)
                with open(cp, "wb") as fh:
                    fh.write(raw)
            idx = parse_index(raw)
        except Exception as e:
            print(f"[prefetch] 源索引跳过 {base}: {e}", flush=True)
            continue
        for k, v in idx.items():
            v["url"] = base + "/" + v["filename"]
            v["source"] = base
        out.append({"base": base, "index": idx})
        print(f"[prefetch] 源 {base.split('/')[-2:]} : {len(idx)} pkgs", flush=True)
    return out


def load_repos_index(ib, cache_ttl=86400):
    """全部已配置源的并集索引（向后兼容的便捷封装）。"""
    srcs = load_repos_sources(ib, cache_ttl)
    union, prov, _ = build_union(srcs)
    return union, prov


def ver_tuple(v):
    """OpenWrt 版本串归一化为可比较元组（数字段按数值比较）。"""
    parts = re.split(r"[.\-_~+]", v or "")
    out = []
    for p in parts:
        # Python 3.11+ 对 int(str) 有 4300 位上限，超长数字串会抛 ValueError
        # 把整条构建链路打断；截断到 18 位足够比较（版本号字段不会更长）。
        if p.isdigit() and len(p) <= 18:
            out.append((0, int(p), ""))
        elif p.isdigit():
            out.append((0, int(p[:18]), p))
        else:
            out.append((1, 0, p))
    return out


def build_union(sources):
    """
    合并全部源：每个包名取「版本最高」的记录（这正是 opkg 的选择规则），
    同时保留候选版本列表（降序），供依赖不可满足时降级。

    返回 (union_index, union_provides, candidates{name: [rec,...]降序})
    """
    candidates = {}
    for s in sources:
        for k, v in s["index"].items():
            candidates.setdefault(k, []).append(v)
    for k in candidates:
        candidates[k].sort(key=lambda r: ver_tuple(r.get("version", "")), reverse=True)
    union = {k: vs[0] for k, vs in candidates.items()}
    provides = {}
    for k, vs in candidates.items():
        for rec in vs:
            for p in rec.get("provides", []):
                provides.setdefault(p, k)
    return union, provides, candidates


def resolve_report(index, provides, wanted, max_pkgs=MAX_PKGS):
    """
    BFS 求闭包，同时报告无法满足的依赖名。
    返回 (need:set, missing:set)
    """
    need, queue, seen, missing = set(), list(wanted), set(), set()
    while queue:
        n = queue.pop(0)
        if n in seen or len(need) > max_pkgs:
            continue
        seen.add(n)
        real = n if n in index else provides.get(n)
        if not real or real not in index:
            # 基础库/内核等由镜像自带，不算缺失
            if n and n not in ("libc", "kernel", "libgcc"):
                missing.add(n)
            continue
        need.add(real)
        for d in index[real]["depends"]:
            if d not in seen:
                queue.append(d)
    return need, missing


def plan_overrides(packages, union, provides, candidates):
    """
    依赖可满足性回退（版本降级）。

    opkg 在多个源之间按「版本最高」选包。第三方源的部分包版本更高，
    但其私有依赖在整个源集合中都不存在（例如 luci-app-ddns 依赖
    ddns-scripts-aliyun）→ 安装必然失败。

    这里对每个请求包：先用「最高版本」求闭包；若出现全源缺失的依赖，
    则按版本降序尝试较低版本，找到第一个依赖可满足的版本并钉住它。

    返回 (overrides{name: version}, fallback_need:set)
    """
    overrides, fallback_need = {}, set()
    for p in packages:
        name = p.split("=")[0]
        if name not in candidates:
            continue
        _, miss = resolve_report(union, provides, [name])
        if not miss:
            continue                       # 最高版本方案自洽
        picked = None
        for rec in candidates[name]:
            if rec["version"] == union[name]["version"]:
                continue                   # 已试过最高版本
            trial = dict(union)
            trial[name] = rec
            need, miss2 = resolve_report(trial, provides, [name])
            if not miss2:
                picked, fallback_need = rec, need
                break
        if picked:
            overrides[name] = picked["version"]
            print(f"[prefetch] {name}: 高版本依赖不可满足 {sorted(miss)[:2]} → "
                  f"降级钉住 {picked['version']}", flush=True)
        else:
            print(f"[prefetch] {name}: 全部候选版本依赖均不可满足 "
                  f"{sorted(miss)[:3]}", flush=True)
    return overrides, fallback_need


def drop_third_party_feeds(ib):
    """
    预取完成后关闭远程第三方源。

    原因：预取已把第三方包的全部依赖闭包落到本地仓（packages/），
    远程源再参与只会带来两个问题 ——
      1) 源站限流导致构建随机失败；
      2) 同名包存在多个版本时 opkg 选「版本最高」的那个，
         而第三方源的部分高版本依赖残缺（如 luci-app-ddns 依赖
         全源都不存在的 ddns-scripts-aliyun），必然安装失败。
    关闭远程第三方源后，本地仓成为第三方包的唯一来源，版本完全可控。
    """
    conf = os.path.join(ib, "repositories.conf")
    if not os.path.exists(conf):
        return
    out, dropped = [], 0
    with open(conf, encoding="utf-8", errors="replace") as fh:
        conf_lines = fh.read().splitlines()
    for line in conf_lines:
        s = line.strip()
        if s.startswith("src/gz") and "kwrt_extra" in s:
            out.append("# " + line.rstrip("\n") + "   # 已由本地仓提供\n")
            dropped += 1
        else:
            out.append(line)
    if dropped:
        with open(conf, "w", encoding="utf-8") as fh:
            fh.write("".join(out))
        print(f"[prefetch] 已关闭远程第三方源 {dropped} 个（改用本地仓）", flush=True)


def prefetch(ib, branch, arch, packages, on_progress=None, base_url="https://dl.openwrt.ai",
             release=None):
    """
    下载 packages 的依赖闭包到 ImageBuilder 本地包仓。

    返回 dict {"count": n, "overrides": {name: version}}。
    overrides 是「必须钉住非第三方版本」的包 —— 调用方需把它们以
    name=version 形式写进 PACKAGES，否则 opkg 会选到依赖不可满足的高版本。
    """
    if not packages:
        return {"count": 0, "overrides": {}}
    pkgdir = os.path.join(ib, "packages")
    dldir = os.path.join(ib, "dl")
    os.makedirs(pkgdir, exist_ok=True)
    os.makedirs(dldir, exist_ok=True)

    tp, prov_tp = load_feeds(branch, arch, base_url)
    if not tp:
        return {"count": 0, "overrides": {}}

    # 全部已配置源（官方 + 第三方）合并；并按 opkg 规则（版本最高）选包
    srcs = load_repos_sources(ib)
    union, allprov, candidates = build_union(srcs)

    # --- 依赖可满足性校验：必要时降级钉住可满足版本 -------------------------
    overrides, fallback_need = plan_overrides(packages, union, allprov, candidates)

    # 钉住的包不按「最高版本」走，改从它的实际来源取
    need, _ = resolve_report(union, allprov, [p.split("=")[0] for p in packages])

    jobs, seen_fn = [], set()
    for n in sorted(need | fallback_need):
        rec = overrides.get(n) and next(
            (r for r in candidates.get(n, []) if r["version"] == overrides[n]), None)
        if not rec:
            rec = union.get(n)
        if not rec or not rec.get("filename"):
            continue
        fn = safe_filename(rec["filename"])
        if not fn:                      # 兜底：任何不经 parse_index 的记录也不会越界落盘
            print(f"[prefetch] 跳过非法文件名: {rec.get('filename')!r}", flush=True)
            continue
        if fn in seen_fn:
            continue
        seen_fn.add(fn)
        dst = os.path.join(dldir, fn)
        if os.path.exists(dst) and os.path.getsize(dst) > 0:
            continue
        tag = "fallback" if n in overrides else "union"
        jobs.append((n, rec["url"], dst, tag))

    if not jobs:
        return {"count": 0, "overrides": overrides}
    if on_progress:
        on_progress("started", f"预取依赖 {len(jobs)} 个包"
                    + (f"（含 {len(overrides)} 个版本回退）" if overrides else ""))
    print(f"[prefetch] 需下载 {len(jobs)} 个包（回退 {len(overrides)} 个）", flush=True)

    ok = 0

    def fetch(job):
        name, url, dst, tag = job
        try:
            data = _get(url, timeout=120, retries=4)
            tmp = dst + ".part"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, dst)
            return True
        except Exception as e:
            print(f"[prefetch] fail [{tag}] {url.split('/')[-1]}: {e}", flush=True)
            return False

    with cf.ThreadPoolExecutor(max_workers=3) as ex:      # 限并发，避免触发源站限流
        for r in ex.map(fetch, jobs):
            ok += 1 if r else 0

    # 钉住的包：清理本地仓中的同名不可用版本，只保留被选中的版本
    for name, ver in (overrides or {}).items():
        for d in (pkgdir, dldir):
            if not os.path.isdir(d):
                continue
            for fn in os.listdir(d):
                if fn.startswith(name + "_") and fn.endswith(".ipk"):
                    if not fn.startswith(name + "_" + ver):
                        try:
                            os.remove(os.path.join(d, fn))
                            print(f"[prefetch] 移除不可用版本: {fn}", flush=True)
                        except OSError:
                            pass

    # 把本地包纳入 package 仓索引（opkg 从 file:packages 安装）
    for fn in os.listdir(dldir):
        if fn.endswith(".ipk"):
            tgt = os.path.join(pkgdir, fn)
            if not os.path.exists(tgt):
                try:
                    shutil.copy2(os.path.join(dldir, fn), tgt)
                except Exception as e:
                    # 静默失败会让包缺席本地仓，构建期只报「找不到包」，无从定位
                    print(f"[prefetch] 复制到本地仓失败 {fn}: {type(e).__name__}: {e}",
                          flush=True)

    # 重建本地包仓索引，使新增/移除的 ipk 立即被 opkg 看到
    try:
        subprocess.run([shutil.which("make") or "/usr/bin/make", "package_index"],
                       cwd=ib, capture_output=True, text=True, timeout=300)
    except Exception as e:
        print("[prefetch] package_index failed:", e, flush=True)

    print(f"[prefetch] 完成 {ok}/{len(jobs)}", flush=True)
    return {"count": ok, "overrides": overrides}
