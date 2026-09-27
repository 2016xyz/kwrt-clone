"""目标 release 解析：把站点分支（25.12）映射到可真实构建的官方 release，
并声明每个 release 的包管理后端与可用第三方源。"""
import json
import re
import os
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 分支 -> (主版本, 官方 release, 包后端)
BRANCH_RELEASE = {
    "25.12": ("25.12", "25.12.5", "apk"),
    "24.10": ("24.10", "24.10.8", "opkg"),
    "23.05": ("23.05", "23.05.6", "opkg"),
}

# 第三方源（kiddin9 feed）随 release 提供；缺失则跳过
THIRD_PARTY = {
    "opkg": ["https://dl.openwrt.ai/packages-{branch}/{arch}/kiddin9"],
    "apk":  [],
}

_SUPPORTED = None


def supported_releases():
    """列出 downloads.openwrt.org 上真实存在的 release（可离线缓存）。"""
    global _SUPPORTED
    if _SUPPORTED is not None:
        return _SUPPORTED
    cache = os.path.join(ROOT, "data", "releases_cache.json")
    if os.path.exists(cache):
        try:
            with open(cache, encoding="utf-8") as f:
                _SUPPORTED = json.load(f)
            return _SUPPORTED
        except Exception:
            pass
    try:
        req = urllib.request.Request("https://downloads.openwrt.org/.versions.json",
                                     headers={"User-Agent": "Kwrt-Builder/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.load(r)
        _SUPPORTED = d.get("versions_list", [])
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        with open(cache, "w", encoding="utf-8") as f:
              json.dump(_SUPPORTED, f)
    except Exception:
        _SUPPORTED = list(BRANCH_RELEASE.values())[:1] and [v[1] for v in BRANCH_RELEASE.values()]
    return _SUPPORTED


def resolve(branch_or_version, prefer=None):
    """
    返回 dict(branch, release, backend)。
    branch_or_version 可为 '25.12'（分支）或 '25.12.5'（具体版本）。
    """
    v = (branch_or_version or "").strip()
    if v:
        for b, (major, rel, backend) in BRANCH_RELEASE.items():
            # 只接受「分支名」「发布号」以及本分支下的具体补丁版本
            # （如 25.12.5）。原用 v.startswith(major + ".") 会把
            # "25.120" 之类也误判为 25.12 分支，且请求 "25.12.9"（不存在）
            # 时静默回落到 25.12.5 —— 用户以为按指定版本构建，实际不是。
            if v == b or v == rel:
                return {"branch": b, "release": rel, "backend": backend}
            if re.fullmatch(re.escape(major) + r"\.[0-9]{1,3}", v):
                return {"branch": b, "release": v, "backend": backend}
    # 默认最新分支
    b = sorted(BRANCH_RELEASE.keys(), reverse=True)[0]
    major, rel, backend = BRANCH_RELEASE[b]
    return {"branch": b, "release": rel, "backend": backend}


def third_party_feeds(branch, arch, backend):
    out = []
    for tpl in THIRD_PARTY.get(backend, []):
        out.append(tpl.format(branch=branch, arch=arch))
    return out
