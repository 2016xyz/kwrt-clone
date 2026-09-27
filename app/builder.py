#!/usr/bin/env python3
"""
真实固件构建引擎 —— 基于 OpenWrt 官方 ImageBuilder。

流程：
  1. 选择与请求版本匹配的官方 ImageBuilder（首次自动下载并解压，带镜像回退）
  2. make image PROFILE=<profile> PACKAGES="..." FILES=<自定义文件目录>
     ROOTFS_PARTSIZE=<mb>  （x86 支持）
  3. 产物输出到 bin/targets/<target>/ → 复制到 store/<request_hash>/
  4. 计算 sha256、解析安装包清单、抓取 stderr/stdout

自定义 defaults：把前端拼好的 uci 脚本写成 /etc/uci-defaults/zz-asu-defaults，
打包进 FILES 目录，首次启动自动执行 —— 与线上站行为一致。
"""
import hashlib
import json
import os
import shutil
import subprocess
import threading
import tarfile
import zipfile
import time
import urllib.request
import gzip
from concurrent.futures import ThreadPoolExecutor
from queue import Queue

from . import releases
from . import params, prefetch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = json.load(open(os.path.join(ROOT, "config.json")))
BUILD = CFG["builder"]

WORK = os.path.join(ROOT, "work")
MAKE_BIN = shutil.which("make") or "/usr/bin/make"          # ImageBuilder 解压目录
STORE = os.path.join(ROOT, "store")        # 构建产物对外目录
CACHE = os.path.join(ROOT, "cache")        # 下载缓存

UA = "Kwrt-Builder/1.0 (+https://example.local)"


# --------------------------------------------------------------------------- #
# 下载工具
# --------------------------------------------------------------------------- #
def download(url, dst, retries=3):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=180) as r, open(dst, "wb") as f:
                shutil.copyfileobj(r, f, 1 << 20)
            if os.path.getsize(dst) > 0:
                return True
        except Exception as e:
            print(f"[builder] download fail {url}: {e}", flush=True)
            time.sleep(3 * (i + 1))
    return False


# 仅存在于第三方 feed（kiddin9，随 24.10 提供）的站点插件
# 勾选这些插件时，apk 后端（25.12）无法提供 → 自动路由到 opkg 后端（24.10）
THIRD_PARTY_ONLY = (
    "luci-app-passwall", "luci-app-passwall2", "luci-app-openclash", "luci-app-homeproxy",
    "luci-app-ssr-plus", "luci-app-nikki", "luci-app-momo", "luci-app-fchomo",
    "luci-app-nekobox", "luci-app-hijpass", "luci-app-v2raya", "luci-app-xray-status",
    "luci-app-daede", "luci-app-istorex", "luci-app-routerdog",
    # 站点默认主题 Argon 亦仅第三方 feed 提供
    "luci-theme-argon", "luci-app-argon-config",
)


def needs_third_party(packages):
    """请求中是否包含只有第三方 feed 才有的插件。"""
    return any(any(p == k or p.startswith(k + " ") for k in THIRD_PARTY_ONLY)
               for p in (packages or []))


def pick_version(target, want=None, packages=None):
    """把站点版本号解析为可真实构建的官方 release；
    若勾选了仅第三方源提供的插件而当前后端不支持，则回落到 opkg 后端（24.10）。"""
    r = releases.resolve(want)
    if r["backend"] == "apk" and needs_third_party(packages):
        fallback = releases.resolve("24.10")
        print(f"[builder] 勾选插件需第三方 feed，版本回落 {r['release']} -> {fallback['release']}",
              flush=True)
        return fallback
    return r


#: 上游 ImageBuilder 支持的 profile 名缓存：(release, target) → (抓取时间, 名字集合|None)
_IB_PROF_CACHE: dict = {}
_IB_PROF_TTL = 6 * 3600
_IB_PROF_LOCK = threading.Lock()


def imagebuilder_profiles(release, target):
    """上游官方 ImageBuilder 在该 release/target 下**真正支持**的 profile 名集合。

    ★ 为什么需要它：站点的设备库来自 kwrt 数据集，其中相当一部分 profile
      上游 ImageBuilder 根本不提供 —— 实测 997 台里 **107 台**（10.7%）编不出来，
      最重的 rockchip/armv8 是 87 台里 49 台，qualcommax/ipq60xx 是 29 台里 14 台
      （见 reports/19）。不提前拦，用户要等一整轮 CI 跑完才看到失败，
      而失败文案还是「请稍后重试或更换软件包组合」—— 重试永远不会成功。

    ★ **取不到时返回 None，调用方必须放行。**
      这项检查是「提前告知」，不是准入控制：镜像站抖动/超时都不该把正常构建拦掉。
      （同理做了负缓存，避免网络不通时每次都白等一轮超时。）
    """
    key = (release, target)
    now = time.time()
    with _IB_PROF_LOCK:
        hit = _IB_PROF_CACHE.get(key)
        if hit and now - hit[0] < _IB_PROF_TTL:
            return hit[1]

    for tpl in BUILD["imagebuilder_mirrors"]:
        url = tpl.replace("{version}", release).replace("{target}", target) + "profiles.json"
        try:
            rq = urllib.request.Request(url, headers={"User-Agent": "kwrt-imagebuilder-check"})
            with urllib.request.urlopen(rq, timeout=20) as r:
                d = json.loads(r.read())
            profs = set((d.get("profiles") or {}).keys())
            if profs:
                with _IB_PROF_LOCK:
                    _IB_PROF_CACHE[key] = (now, profs)
                return profs
        except Exception as e:                                     # noqa: BLE001
            print(f"[builder] profiles.json 取不到 {url}: {type(e).__name__}", flush=True)

    with _IB_PROF_LOCK:
        _IB_PROF_CACHE[key] = (now, None)
    return None


def ib_dir_name(version, target):
    return f"openwrt-imagebuilder-{version}-{target.replace('/', '-')}.Linux-x86_64"


def ensure_imagebuilder(version, target):
    """下载并解压 ImageBuilder，返回其根目录。"""
    name = ib_dir_name(version, target)
    root = os.path.join(WORK, name)
    # 24.10 的 build_dir 内含预置内核产物（bzImage / generic-kernel.bin / vmlinux），
    # 缺失会导致 "No rule to make target generic-kernel.bin"；用它判定完整性。
    ok = os.path.isfile(os.path.join(root, "Makefile"))
    if ok:
        kdir = os.path.join(root, "build_dir", "target-x86_64_musl", "linux-x86_64")
        if os.path.isdir(os.path.join(root, "build_dir")) and not os.path.exists(
                os.path.join(kdir, "generic-kernel.bin")):
            ok = False                      # build_dir 不完整 → 重新解压
    if ok:
        return root

    tarball = os.path.join(CACHE, name + ".tar.zst")
    if not os.path.exists(tarball) or os.path.getsize(tarball) < 1 << 20:
        ok = False
        for tpl in BUILD["imagebuilder_mirrors"]:
            url = tpl.replace("{version}", version).replace("{target}", target) + name + ".tar.zst"
            print(f"[builder] fetching ImageBuilder: {url}", flush=True)
            if download(url, tarball):
                ok = True
                break
        if not ok:
            raise RuntimeError(f"ImageBuilder 下载失败: {version} {target}")

    os.makedirs(WORK, exist_ok=True)
    # tar.zst 需要 zstd；python 3.12+ 支持 --zstd，低版本走 zstd|tar 管道。
    # 安全：**绝不**把 tarball 拼进 shell 字符串 —— 见下方 sh -c 写法。
    tmp = os.path.join(WORK, "_extract")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    p = subprocess.run(["tar", "--zstd", "-xf", tarball, "-C", tmp],
                       capture_output=True, text=True)
    if p.returncode != 0:
        # 管道必须交由 shell 执行，但路径不能进已解析的命令串。
        # 用 `sh -c '脚本' sh "$1" "$2"`：$0/$1 作为单独 argv 传入，
        # shell 只解释固定脚本，路径即使含空格/分号/$( ) 也只是普通参数。
        script = 'zstd -dc "$1" | tar -xf - -C "$2"'
        p2 = subprocess.run(["sh", "-c", script, "sh", tarball, tmp],
                            capture_output=True, text=True)
        if p2.returncode != 0:
            raise RuntimeError("解压 ImageBuilder 失败: " + (p.stderr or p2.stderr))
    inner = os.listdir(tmp)
    if not inner:
        raise RuntimeError("ImageBuilder 包为空")
    if os.path.isdir(root):
        shutil.rmtree(root, ignore_errors=True)      # 不完整则重建
    shutil.move(os.path.join(tmp, inner[0]), root)
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"[builder] ImageBuilder ready: {root}", flush=True)
    return root


# --------------------------------------------------------------------------- #
# 构建
# --------------------------------------------------------------------------- #
def _inside(base, path):
    """path 是否真的落在 base 之内（含 base 自身）。

    ⚠ 不能用 `p.startswith(base)`：`/a/b` 是 `/a/bc` 的字符串前缀却不是它的
    祖先目录。原实现正是这么写的，导致「符号链接指向 base 的兄弟目录
    （名字以 base 为前缀）」能绕过校验 —— 实测可把文件写到 dest 之外。
    这里先规范化为绝对真实路径，再用 commonpath 判定。
    """
    base = os.path.realpath(base)
    p = os.path.realpath(path)
    if p == base:
        return True
    try:
        return os.path.commonpath([base, p]) == base
    except ValueError:
        # 不同盘符/根（Windows）时 commonpath 抛错 → 判定为越界
        return False


def _safe_extract(archive, dest):
    """安全解压自定义文件包，杜绝路径穿越（Tar-Slip）。

    用户上传的归档会被解压进固件的 files/ 目录。若直接 tarfile.extractall()：
      · 条目名含 ../ 时可写到 dest 之外（甚至覆盖宿主文件）
      · 条目是符号链接时，后续条目可借它跳出 dest
    这里对每个条目做规范化校验，任何越界条目直接拒绝整包。

    支持 .tar.gz/.tgz/.tar；其余（.zip/.7z）尝试用系统工具，失败即放弃。
    """
    dest = os.path.realpath(dest)
    lower = archive.lower()

    if lower.endswith(".zip"):
        # 先**只读列目录**校验，再解压 —— 原实现先解压后校验，
        # 越界条目在被发现前已经落盘。改为 zipfile 逐条预检。
        with zipfile.ZipFile(archive) as z:
            for zi in z.infolist():
                nm = zi.filename or ""
                if nm.startswith("/") or ".." in nm.split("/"):
                    raise RuntimeError(f"归档包含越界路径: {nm}")
                # Unix 属性位：判断是否是符号链接（外部属性高 16 位）
                mode = (zi.external_attr >> 16) & 0o170000
                if mode == 0o120000:
                    raise RuntimeError(f"归档包含符号链接，已拒绝: {nm}")
            for zi in z.infolist():
                target = os.path.join(dest, zi.filename)
                if not _inside(dest, target):
                    raise RuntimeError(f"归档条目越界: {zi.filename}")
            z.extractall(dest)
        return

    if lower.endswith(".7z"):
        # 没有 stdlib 支持，只能交给系统工具；解压后校验（7z 未安装时会失败）
        tool = ["7z", "x", "-y", f"-o{dest}", archive]
        try:
            subprocess.run(tool, check=True, capture_output=True, timeout=300)
        except Exception as e:
            raise RuntimeError(f"解压 {os.path.basename(archive)} 失败: {e}")
        for root, _dirs, names in os.walk(dest):
            for n in names:
                if not _inside(dest, os.path.join(root, n)):
                    raise RuntimeError(f"归档包含越界路径: {n}")
        return

    with tarfile.open(archive, "r:*") as t:
        members = t.getmembers()
        for m in members:
            name = m.name or ""
            if name.startswith("/") or ".." in name.split("/"):
                raise RuntimeError(f"归档包含越界路径: {name}")
            if m.issym() or m.islnk():
                # 链接目标同样必须落在 dest 内（用 _inside 而非 startswith）
                target = os.path.normpath(
                    os.path.join(dest, os.path.dirname(name), m.linkname))
                if not _inside(dest, target):
                    raise RuntimeError(f"归档包含越界链接: {name} -> {m.linkname}")
            if m.isdev():
                raise RuntimeError(f"归档包含设备文件，已拒绝: {name}")
        # 兜底：所有条目的最终落点也必须落在 dest 内
        for m in members:
            if not _inside(dest, os.path.join(dest, m.name)):
                raise RuntimeError(f"归档条目越界: {m.name}")
        # 到这里所有条目都已校验通过
        t.extractall(dest)


def write_defaults(files_dir, script):
    """把 uci-defaults 脚本落盘到 FILES 目录。"""
    if not script:
        return
    d = os.path.join(files_dir, "etc", "uci-defaults")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "zz-asu-defaults"), "w") as f:
        f.write("#!/bin/sh\n")
        f.write(script.rstrip() + "\n")
    os.chmod(os.path.join(d, "zz-asu-defaults"), 0o755)


def parse_pkg_list(packages):
    """区分 安装 / 排除 / 覆盖清单(package_changes)。"""
    add, remove = [], []
    for p in packages:
        if not p:
            continue
        if p.startswith("-"):
            remove.append(p[1:])
        else:
            add.append(p)
    return add, remove


# 站点前端使用、但官方源不存在的包名 -> 官方实际包名
PKG_ALIAS = {
    "luci-app-xray-status": "luci-app-xray",
    "luci-app-daede": "daed",
    "luci-app-webadmin": None,          # 无对应包，直接丢弃
    "luci-app-istorex": "luci-app-istoreenhance",
    "luci-theme-openwrt": "luci-theme-openwrt-2020",
}

ARCH_BY_TARGET = {
    "x86/64": "x86_64",
    "x86/generic": "i386_pentium4",
    "rockchip/armv8": "aarch64_generic",
    "armsr/armv8": "aarch64_generic",
}


def arch_of(target):
    return ARCH_BY_TARGET.get(target, "x86_64")


def normalize_packages(add_remove, backend):
    """把前端包名映射/过滤为后端真实可安装的包名。"""
    add, remove = add_remove
    fixed = []
    for p in add:
        if p in PKG_ALIAS:
            q = PKG_ALIAS[p]
            if q is None:
                continue
            fixed.append(q)
        else:
            fixed.append(p)

    # opkg 后端：站点出厂包含 dnsmasq-full，与官方基础 dnsmasq 存在文件冲突
    # （passwall/openclash 等会拉入 dnsmasq-full）→ 强制替换
    if backend == "opkg":
        if "dnsmasq" not in remove:
            remove.append("dnsmasq")
        if "dnsmasq-full" not in fixed:
            fixed.append("dnsmasq-full")

    # 去重保序
    seen, out = set(), []
    for p in fixed:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out, remove


def ensure_core_ipks(ib):
    """
    24.10（opkg 后端）的 ImageBuilder 把 libc / kernel 的 ipk 放在 build_dir 内，
    而 Makefile 的 package_install 依赖 $(PACKAGE_DIR)/libc_*.ipk。
    ① 把它们固化到 packages/（即使清理 build_dir 省磁盘也不破坏构建）；
    ② 同时预生成空的 Packages/Packages.gz 并置新，避免上游 ipkg-make-index.sh
       在「仅有 kernel/libc」时以非零码退出导致 package_index 失败。
    """
    pkgdir = os.path.join(ib, "packages")
    if not os.path.isdir(pkgdir):
        return
    names = os.listdir(pkgdir)
    has_libc = any(f.startswith("libc_") and f.endswith(".ipk") for f in names)
    has_kern = any(f.startswith("kernel_") and f.endswith(".ipk") for f in names)
    if not (has_libc and has_kern):
        found = {}
        for root, _, files in os.walk(os.path.join(ib, "build_dir")):
            for f in files:
                if not f.endswith(".ipk"):
                    continue
                if f.startswith("libc_") and "libc" not in found:
                    found["libc"] = os.path.join(root, f)
                elif f.startswith("kernel_") and "kernel" not in found:
                    found["kernel"] = os.path.join(root, f)
        for src in found.values():
            shutil.copy2(src, os.path.join(pkgdir, os.path.basename(src)))
            print(f"[builder] 核心包已固化: {os.path.basename(src)}", flush=True)

    # 预生成索引（空）并置新，使 package_reload 跳过 package_index
    pkg_index = os.path.join(pkgdir, "Packages")
    gz = pkg_index + ".gz"
    if not os.path.exists(gz) or os.path.getsize(gz) == 0:
        with open(pkg_index, "wb") as f:
            f.write(b"")
        with gzip.open(gz, "wb") as f:
            f.write(b"")
    os.utime(gz, None)


def ensure_repositories(ib, branch, release, arch, backend):
    """
    接入第三方插件源（opkg 后端），并放宽签名校验。
    apk 后端的上游 feed 为 .ipk 格式，无法直接用于 apk 版 ImageBuilder，故跳过。
    """
    if backend == "opkg":
        conf = os.path.join(ib, "repositories.conf")
        feeds = releases.third_party_feeds(branch, arch, backend)
        if os.path.exists(conf) and feeds:
            with open(conf) as _f:
                txt = _f.read()
            for i, f in enumerate(feeds):
                line = f"src/gz kwrt_extra_{i} {f}\n"
                if f not in txt:
                    txt += line
            txt = txt.replace("option check_signature", "# option check_signature")
            open(conf, "w").write(txt)
            print(f"[builder] 已接入第三方源 {feeds}（签名校验已关闭）", flush=True)


def apply_filesystem(ib, filesystem):
    """
    只生成用户所选文件系统的镜像。
    ImageBuilder 默认同时产出 squashfs + ext4（各 ~1GB 未压缩），
    在磁盘受限环境下会直接写满；按请求收敛既省资源又不改变语义。
    """
    cfg = os.path.join(ib, ".config")
    if not os.path.exists(cfg):
        return
    want_ext4 = (filesystem or "squashfs") == "ext4"
    try:
        with open(cfg) as _f:
            lines = _f.read().splitlines()
        out = []
        for l in lines:
            if l.startswith("CONFIG_TARGET_ROOTFS_EXT4FS") or l.startswith("# CONFIG_TARGET_ROOTFS_EXT4FS"):
                out.append("CONFIG_TARGET_ROOTFS_EXT4FS=y" if want_ext4
                           else "# CONFIG_TARGET_ROOTFS_EXT4FS is not set")
            elif l.startswith("CONFIG_TARGET_ROOTFS_SQUASHFS") or l.startswith("# CONFIG_TARGET_ROOTFS_SQUASHFS"):
                out.append("# CONFIG_TARGET_ROOTFS_SQUASHFS is not set" if want_ext4
                           else "CONFIG_TARGET_ROOTFS_SQUASHFS=y")
            else:
                out.append(l)
        open(cfg, "w").write("\n".join(out) + "\n")
        print(f"[builder] 文件系统收敛: {'ext4' if want_ext4 else 'squashfs'}", flush=True)
    except Exception as e:
        print("[builder] apply_filesystem failed:", e, flush=True)


def check_disk(need_mb, on_progress=None):
    """构建前磁盘校验：镜像生成需要约 3x 根目录容量，磁盘不足则迅速给出可读错误。"""
    st = os.statvfs(ROOT)
    free_mb = st.f_bavail * st.f_frsize / 1048576
    if free_mb < need_mb:
        raise RuntimeError(
            f"构建所需磁盘空间不足：剩余 {free_mb:.0f}MB，约需 {need_mb}MB。"
            f"请清理 work/ 或 store/ 后重试（镜像生成峰值约为根目录容量的 3 倍）。")


def build(req, on_progress, request_hash=None, handle=None):
    """
    req: dict  -> target/profile/packages/defaults/filesystem/rootfs/efi/vmdk/more/settings/...
    request_hash: 由队列分配的任务 ID（产物目录名必须与之严格一致）
    handle: 任务 dict（由队列传入）。管理员取消时写入 cancelled=True 并登记 proc。
    返回 dict: {request_hash, files[], packages[], stdout, stderr, status}
    """
    if handle is None:
        handle = {}
    if handle.get("cancelled"):
        return {"status": "cancelled", "detail": "已被管理员取消", "stdout": "", "stderr": ""}

    # 纵深防御第二道：即便调用方（路由/重试/restore）漏了净化，这里也强制再来一次。
    # target/profile/packages 会进入 shell 与 make，绝不能相信上游。
    try:
        req = params.sanitize(req)
    except params.BuildParamError as e:
        return {"status": "failed", "detail": f"invalid build params: {e}",
                "stdout": "", "stderr": f"BuildParamError: {e}"}

    target = req["target"]              # 例如 x86/64
    profile = req["profile"]
    rinfo = releases.resolve(req.get("version") or req.get("branch"))
    packages = req.get("packages") or []
    if rinfo["backend"] == "apk" and needs_third_party(packages):
        rinfo = releases.resolve("24.10")          # 回落至具备第三方 feed 的后端
    version = rinfo["release"]
    branch, backend = rinfo["branch"], rinfo["backend"]
    add, remove = normalize_packages(parse_pkg_list(packages), backend)
    rootfs_mb = req.get("rootfs_size_mb") or 1004

    # 仅产出单一文件系统时，镜像生成峰值 ≈ 2x 根目录容量 + ImageBuilder 开销
    check_disk(int(rootfs_mb) * 2 + 600, on_progress)

    on_progress("started", "准备 ImageBuilder")
    ib = ensure_imagebuilder(version, target)
    ensure_core_ipks(ib)
    ensure_repositories(ib, branch, version, arch_of(target), backend)
    apply_filesystem(ib, req.get("filesystem"))

    # 第三方依赖预取（仅 opkg 后端）：把闭包下载到本地包仓，
    # 构建期不再访问上游 → 确定性 + 不受源站限流影响。
    # 依赖残缺的高版本包会在预取阶段自动降级，并关闭远程第三方源，
    # 使本地仓成为第三方包的唯一来源（版本完全可控）。
    if backend == "opkg":
        try:
            pf = prefetch.prefetch(ib, branch, arch_of(target), add, on_progress,
                                   release=version)
            ov = pf.get("overrides") or {}
            if ov:
                print(f"[builder] 依赖回退 {len(ov)} 个: "
                      + ", ".join(f"{k}={v}" for k, v in ov.items()), flush=True)
            prefetch.drop_third_party_feeds(ib)
        except Exception as e:
            print("[builder] prefetch failed:", e, flush=True)

    # 取消检查点（预取可能耗时较久）
    if handle.get("cancelled"):
        return {"status": "cancelled", "detail": "已被管理员取消", "stdout": "", "stderr": ""}

    # 关键：.profiles.mk 必须由 target-metadata.pl 重新生成（依赖 perl-FindBin）。
    # 缺失/陈旧会导致 "Profile does not exist"。
    pmk = os.path.join(ib, ".profiles.mk")
    try:
        if not os.path.exists(pmk) or os.path.getsize(pmk) < 32:
            if os.path.exists(pmk):
                os.remove(pmk)
            subprocess.run([MAKE_BIN, "-C", ib, "--", ".profiles.mk"], capture_output=True,
                           text=True, timeout=300)
    except Exception as e:
        print("[builder] regenerate .profiles.mk failed:", e, flush=True)

    # FILES 目录：uci-defaults + 自定义文件包
    files_dir = os.path.join(ib, "files")
    shutil.rmtree(files_dir, ignore_errors=True)
    os.makedirs(files_dir, exist_ok=True)
    write_defaults(files_dir, req.get("defaults") or "")

    # 自定义文件包解压覆盖
    fp = req.get("files_path") or ""
    if fp:
        # files_path 可能是以下任一形态（按序尝试）：
        #   _staged/<用户名>/<文件名>   —— 「先上传、后提交」的暂存件
        #   <request_hash>/<文件名>     —— 「先提交、后上传」的挂载件
        #   <文件名>                    —— 旧的平铺件（兼容历史构建记录）
        # 逐段过滤 .. 与绝对路径，确保拼出的路径不会跑出 uploads 目录。
        rel = "/".join(p for p in str(fp).split("/") if p and p not in (".", ".."))
        cands = []
        if rel:
            cands.append(os.path.join(STORE, "uploads", rel))
        base = os.path.basename(rel or fp)
        if request_hash:
            cands.append(os.path.join(STORE, "uploads", request_hash, base))
        cands.append(os.path.join(STORE, "uploads", base))
        up_root = os.path.realpath(os.path.join(STORE, "uploads"))
        src = ""
        for c in cands:
            rc = os.path.realpath(c)
            if rc.startswith(up_root + os.sep) and os.path.isfile(rc):
                src = rc
                break
        if src:
            try:
                _safe_extract(src, files_dir)
            except Exception as e:
                print("[builder] extract files_path failed:", e, flush=True)

    # 绝对路径 + -- 结束选项解析：避免 make 把后续的 VAR=value 当目标名，
    # 也避免 cwd 变化时误取当前目录的 Makefile。
    make = [MAKE_BIN, "-C", ib, "--", "image",
            f"PROFILE={profile}",
            "PACKAGES=" + " ".join(add + ["-" + x for x in remove]),
            f"FILES={files_dir}"]
    if rootfs_mb:
        make.append(f"ROOTFS_PARTSIZE={int(rootfs_mb)}")
    if req.get("diff_packages"):
        make.append("IGNORE_ERRORS=")

    env = dict(os.environ)
    env["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    env["FORCE_UNSAFE_CONFIGURE"] = "1"

    on_progress("started", f"imagebuilder 开始: PROFILE={profile} pkgs={len(add)}")
    t0 = time.time()
    # 用 Popen + 独立进程组启动，使管理员可从控制台真实终止构建。
    # handle 由队列传入（同一个 dict 对象），cancel() 写入 cancelled 或读取 proc。
    if handle is None:
        handle = {}
    proc = subprocess.Popen(make, cwd=ib, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, env=env, start_new_session=True)
    handle["proc"] = proc
    try:
        stdout, stderr = proc.communicate(timeout=7200)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
    dur = time.time() - t0
    stdout, stderr = stdout or "", stderr or ""
    print(f"[builder] make exit={proc.returncode} in {dur:.1f}s", flush=True)

    if proc.returncode != 0:
        # 管理员主动取消 → 不算构建失败
        if handle.get("cancelled"):
            return {"status": "cancelled", "stdout": stdout[-20000:], "stderr": stderr[-20000:],
                    "detail": "已被管理员取消"}
        return {"status": "failed", "stdout": stdout[-40000:], "stderr": stderr[-40000:],
                "detail": "tr-build-failed"}

    # 收集产物
    if not request_hash:
        request_hash = hashlib.md5(
            json.dumps(req, sort_keys=True, default=str).encode()).hexdigest()[:16]
    outdir = os.path.join(STORE, request_hash)
    shutil.rmtree(outdir, ignore_errors=True)
    os.makedirs(outdir, exist_ok=True)

    bin_root = os.path.join(ib, "bin", "targets", target)
    files = []
    for dirpath, _, names in os.walk(bin_root):
        for n in names:
            if not n.endswith((".img.gz", ".tar.gz", ".vmdk", ".img", ".itb", ".bin")):
                continue
            src = os.path.join(dirpath, n)
            shutil.copy2(src, os.path.join(outdir, n))
            files.append({
                "name": n,
                "type": guess_type(n),
                "size": os.path.getsize(src),
                "sha256": sha256(src),
            })

    # 安装包清单
    installed = sorted({os.path.splitext(f)[0] for f in _installed_ips(ib, target)})
    on_progress("done", f"构建完成，产出 {len(files)} 个文件，耗时 {dur:.0f}s")
    return {
        "status": "done",
        "request_hash": request_hash,
        "store_url": f"/store/{request_hash}",
        "files": files,
        "packages": installed,
        "stdout": stdout[-40000:],
        "stderr": stderr[-40000:],
        "target": target,
        "profile": profile,
        "version": version,
        "duration": round(dur, 1),
    }


def _installed_ips(ib, target):
    """从 ImageBuilder 的 packages 目录读已安装包名。"""
    out = []
    root = os.path.join(ib, "bin", "targets", target, "packages")
    if os.path.isdir(root):
        for n in os.listdir(root):
            if n.endswith(".ipk"):
                out.append(n)
    if not out:
        # 从 manifest 取
        for dirpath, _, names in os.walk(os.path.join(ib, "bin", "targets", target)):
            for n in names:
                if n.endswith(".manifest"):
                    try:
                        for line in open(os.path.join(dirpath, n)):
                            out.append(line.split(" - ")[0].strip() + ".ipk")
                    except Exception:
                        pass
    return out


def guess_type(name):
    n = name.lower()
    if "vmdk" in n:
        return "vmdk"
    if "rootfs" in n and n.endswith("tar.gz"):
        return "rootfs"
    if "efi" in n and "combined" in n:
        return "combined-efi"
    if "combined" in n:
        return "combined"
    if "sysupgrade" in n:
        return "sysupgrade"
    if "factory" in n:
        return "factory"
    if "initramfs" in n or "kernel" in n:
        return "kernel"
    return "other"


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# 队列（单并发；VIP 2 并发）
# --------------------------------------------------------------------------- #
class BuildQueue:
    def __init__(self, concurrency=None):
        self.concurrency = concurrency or BUILD.get("max_concurrent", 1)
        self.jobs = {}                 # hash -> dict
        self.q = Queue()
        self._lock = threading.RLock()  # 保护 jobs / q / concurrency
        self._started = 0
        # 构建执行器（由上层注入，用于切换本地 / GitHub 后端）
        # 签名: runner(req, on_progress, handle) -> result dict
        self.runner = None
        # 完成回调（注入后用于签发下载令牌 / 发邮件）
        # 签名: hook(job) -> None
        self.on_finished = None

    def submit(self, req, meta=None):
        jid = hashlib.md5((json.dumps(req, sort_keys=True, default=str) +
                           str(time.time())).encode()).hexdigest()[:16]
        job = {"request_hash": jid, "status": "queued", "req": req,
               "queue_position": len(self.jobs), "logs": [], "created": time.time()}
        # 归属信息（用于签发下载令牌与邮件通知）
        for k in ("username", "email"):
            if meta and meta.get(k):
                job[k] = meta[k]
        with self._lock:
            self.jobs[jid] = job
            self.q.put(jid)
        try:
            from . import jobs as jobstore
            jobstore.put(job)
        except Exception:
            pass
        self._pump()
        return job

    def restore(self):
        """重启后把数据库中未完成的任务重新排入队列（真实续跑）。"""
        try:
            from . import jobs as jobstore
            for rec in jobstore.pending():
                jid = rec["request_hash"]
                if jid in self.jobs:
                    continue
                self.jobs[jid] = {"request_hash": jid, "status": "queued", "req": rec["req"],
                                  "queue_position": len(self.jobs), "logs": [],
                                  "created": rec.get("created", time.time())}
                self.q.put(jid)
            self._pump()
        except Exception as e:
            print("[builder] restore queue failed:", e, flush=True)

    def _pump(self):
        """调度排队任务。

        并发安全：submit / _run 结尾 / 后台线程都会调用本方法，
        原实现无锁，「读 running → 起线程 → 再读」之间存在 TOCTOU，
        并发调用会一起越过 concurrency 上限（实测可超发构建）。
        """
        with self._lock:
            running = sum(1 for j in self.jobs.values()
                          if j["status"] in ("started", "running"))
            while running < self.concurrency and not self.q.empty():
                jid = self.q.get()
                job = self.jobs.get(jid)
                if not job or job["status"] != "queued":
                    continue
                job["status"] = "started"
                job["imagebuilder_status"] = "初始化"
                ThreadPoolExecutor(max_workers=1).submit(self._run, jid)
                running += 1
            # 刷新队列位置
            # ★ 这里原来写的是 job["queue_position"] = pos —— 循环变量明明是 j，
            #   却写到了 job，而 job 是上面 while 循环的残留变量。两种翻车：
            #     ① 队列空、或并发已满（while 一次都没进）时 job 从未绑定 →
            #        UnboundLocalError → 提交接口直接 **500**。
            #        实测：并发=1 且有构建在跑时，再提交一次必现。
            #        线上 builder.max_concurrent 正是 1，等于「别人在编时你别点」。
            #     ② 即使进了 while，排队位置也全写到刚启动的那个任务上，
            #        真正的排队任务拿不到位置。
            pos = 0
            for j in self.jobs.values():
                if j["status"] == "queued":
                    j["queue_position"] = pos
                    pos += 1

    def _run(self, jid):
        job = self.jobs[jid]

        def prog(status, msg):
            job["status"] = status if status != "started" else "running"
            if job.get("status") == "cancelled":
                return
            job["imagebuilder_status"] = msg
            job["logs"].append((time.time(), msg))

        try:
            if self.runner:
                res = self.runner(job["req"], prog, job)
            else:
                res = build(job["req"], prog, request_hash=jid, handle=job)
        except Exception as e:
            res = {"status": "failed", "stderr": f"{type(e).__name__}: {e}", "stdout": ""}
        # 已被管理员取消：保留 cancelled 状态，不被构建返回值覆盖
        if job.get("status") == "cancelled" or job.get("cancelled_by_admin"):
            job["status"] = "cancelled"
            job.setdefault("imagebuilder_status", "已被管理员取消")
        else:
            job.update(res)
            job["status"] = res.get("status", "failed")
        job["finished"] = time.time()
        job.pop("proc", None)
        try:
            from . import jobs as jobstore
            jobstore.put(job)
        except Exception as e:
            print("[builder] persist job failed:", e, flush=True)
        # 完成回调：签发带有效期的下载链接、发邮件通知等
        if self.on_finished and job["status"] in ("done", "failed"):
            try:
                self.on_finished(job)
            except Exception as e:
                print("[builder] on_finished failed:", e, flush=True)
        self._pump()

    def get(self, jid):
        return self.jobs.get(jid)

    def cancel(self, jid):
        """
        真实取消：
          - 排队中的任务：从队列逻辑上作废（不再启动）
          - 运行中的任务：终止正在执行的 make 进程，标记 cancelled
        返回是否发生了取消。
        """
        job = self.jobs.get(jid)
        if not job:
            return False
        if job.get("status") in ("done", "failed", "cancelled"):
            return False
        # 运行中：杀掉构建进程树
        proc = job.get("proc")
        job["cancelled"] = True
        if job.get("proc"):
            job["cancelled_by_admin"] = True
        if proc and proc.poll() is None:
            try:
                import signal
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                time.sleep(2)
                if proc.poll() is None:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception as e:
                print("[builder] cancel kill failed:", e, flush=True)
        job["status"] = "cancelled"
        job["imagebuilder_status"] = "已被管理员取消"
        job["finished"] = time.time()
        # 清理半成品产物目录
        d = os.path.join(STORE, jid)
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
        try:
            from . import jobs as jobstore
            jobstore.put(job)
        except Exception as e:
            # 原先静默 pass：任务终态没能落库时，构建在管理端会**永远显示 running**，
            # 且没有任何线索可查。至少要留下日志。
            print(f"[builder] 写入任务终态失败 {jid}: {type(e).__name__}: {e}", flush=True)
        return True


QUEUE = None

def get_queue():
    global QUEUE
    if QUEUE is None:
        QUEUE = BuildQueue()
        QUEUE.restore()          # 真实续跑：重启后重新排入未完成任务
    return QUEUE
