"""
全局构建插件目录（软件包 / 分类 / 一键套件）。

单一样本来来源是数据库里的 `build.pkg_catalog`，管理员可在后台增删改；
首次运行时从 web/assets/js/presets.js 解析出默认清单灌库，保证升级不丢内容。

对外只暴露目录本身（包名 + 中文说明），不涉及任何密钥。
"""
import json
import os
import re
import threading

# 预设文件（默认清单来源）
PRESETS_JS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "web", "assets", "js", "presets.js")

# 目录的读-改-写必须串行：add/remove 均为「get → 改 → save」三步，
# 并发请求会互相覆盖（实测丢更新）。单进程部署用进程内锁即可。
_LOCK = threading.RLock()
PKG_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}$")
MAX_SUITE_PKGS = 200

KEY = "build.pkg_catalog"

CAT_KEYS = ("proxy", "net", "dl", "share", "disk", "mgr", "ctrl", "media", "virt", "theme")


def _parse_defaults() -> dict:
    """从 presets.js 解析默认清单。

    只认固定的字面量形状 `{ c: 'x', n: 'y', l: 'z', d: '...' }`，
    解析失败则返回空结构（不抛错，避免站点起不来）。
    """
    cats, presets, suites = [], [], []
    try:
        with open(PRESETS_JS, encoding="utf-8") as _f:
            src = _f.read()
    except OSError:
        return {"cats": [], "presets": [], "suites": []}

    # U-6：先用 `window.KWRT_* = [ … ]` 切出各自数组区间，再在区间内做条目匹配。
    # 原实现把条目正则丢在**整个文件**上跑，三类条目的形态又很接近，
    # 一旦文件里出现额外区块（或注释里带样例），就会跨区块误匹配 ——
    # 实测 presets 会被数成 120 条（真实 119），套件也被多认一个（13 vs 12）。
    def _segment(var_name, next_var):
        start = src.find(var_name)
        if start < 0:
            return ""
        start = src.find("[", start)
        if start < 0:
            return ""
        end = src.find(next_var, start) if next_var else -1
        if end < 0:
            end = len(src)
        return src[start:end]

    seg_presets = _segment("window.KWRT_PRESETS", "window.KWRT_PRESET_CATS")
    seg_cats = _segment("window.KWRT_PRESET_CATS", "window.KWRT_SUITES")
    seg_suites = _segment("window.KWRT_SUITES", None)

    # 条目：{ c: 'proxy', n: 'luci-app-openclash', l: 'OpenClash', d: '...' }
    item_re = re.compile(
        r"\{\s*c:\s*'([^']*)'\s*,\s*n:\s*'([^']*)'\s*,\s*l:\s*'([^']*)'"
        r"(?:\s*,\s*d:\s*'([^']*)')?\s*\}")
    for m in item_re.finditer(seg_presets):
        presets.append({"c": m.group(1), "n": m.group(2), "l": m.group(3),
                        "d": m.group(4) or ""})

    cat_re = re.compile(r"\{\s*k:\s*'([^']*)'\s*,\s*l:\s*'([^']*)'\s*\}")
    cats = [{"k": m.group(1), "l": m.group(2)}
            for m in cat_re.finditer(seg_cats)]

    # 套件：{ k: 'openclash', l: 'OpenClash', pkgs: ['luci-app-openclash'] }
    suite_re = re.compile(
        r"\{\s*k:\s*'([^']*)'\s*,\s*l:\s*'([^']*)'\s*,\s*pkgs:\s*\[([^\]]*)\]\s*\}")
    for m in suite_re.finditer(seg_suites):
        pkgs = [p.strip().strip("'\"") for p in m.group(3).split(",") if p.strip()]
        suites.append({"k": m.group(1), "l": m.group(2), "pkgs": pkgs})

    return {"cats": cats, "presets": presets, "suites": suites}


DEFAULTS = _parse_defaults()


# --------------------------------------------------------------------------- #
# 读写
# --------------------------------------------------------------------------- #
def _normalize(d: dict) -> dict:
    """保证结构完整且条目合法（包名不能为空、不可重复）。"""
    out = {"cats": [], "presets": [], "suites": []}
    seen_cat, seen_pkg, seen_suite = set(), set(), set()

    for c in (d.get("cats") or []):
        k = str(c.get("k") or "").strip()
        l = str(c.get("l") or "").strip()
        if not k or not l or k in seen_cat:
            continue
        seen_cat.add(k)
        out["cats"].append({"k": k, "l": l})

    for p in (d.get("presets") or []):
        n = str(p.get("n") or "").strip()
        if not n or n in seen_pkg:
            continue
        seen_pkg.add(n)
        out["presets"].append({
            "c": str(p.get("c") or "").strip(),
            "n": n,
            "l": str(p.get("l") or n).strip(),
            "d": str(p.get("d") or "").strip()[:200],
        })

    for s in (d.get("suites") or []):
        k = str(s.get("k") or "").strip()
        if not k or k in seen_suite:
            continue
        pkgs = [str(x).strip() for x in (s.get("pkgs") or []) if str(x).strip()]
        if not pkgs:
            continue
        seen_suite.add(k)
        out["suites"].append({"k": k, "l": str(s.get("l") or k).strip(), "pkgs": pkgs})

    # 丢弃指向不存在分类的条目，归入第一个分类，避免界面出现孤儿分组
    valid = {c["k"] for c in out["cats"]} or {""}
    if "" not in valid:
        fallback = out["cats"][0]["k"]
        for p in out["presets"]:
            if p["c"] not in valid:
                p["c"] = fallback
    return out


def get(conn) -> dict:
    """取目录；未初始化时用默认清单灌库。"""
    row = conn.execute("SELECT value FROM settings WHERE key=?", (KEY,)).fetchone()
    if row and row["value"]:
        try:
            d = json.loads(row["value"])
            # 只要行存在且可解析就用它 —— 包括管理员刻意清空的空清单。
            # 原实现要求 presets 非空，导致清空后再读会重新灌默认值，
            # 管理员的删除操作等于无效。
            if isinstance(d, dict):
                return _normalize(d)
        except (ValueError, TypeError):
            pass
    d = _normalize(DEFAULTS)
    save(conn, d)
    return d


def save(conn, d: dict) -> dict:
    with _LOCK:
        d = _normalize(d)
        conn.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (KEY, json.dumps(d, ensure_ascii=False)))
        conn.commit()
        return d


def reset(conn) -> dict:
    with _LOCK:
        """恢复为内置默认清单。"""
        return save(conn, _normalize(DEFAULTS))


# --------------------------------------------------------------------------- #
# 增删
# --------------------------------------------------------------------------- #
def add_preset(conn, name: str, label: str = "", cat: str = "", desc: str = "") -> dict:
    with _LOCK:
        name = (name or "").strip()
        if not name:
            return {"ok": False, "error": "软件包名不能为空"}
        if not PKG_NAME_RE.match(name):
            return {"ok": False, "error": "软件包名含非法字符（只允许字母、数字、. _ + -）"}
        if len(name) > 120:
            return {"ok": False, "error": "软件包名过长"}
        d = get(conn)
        if any(p["n"] == name for p in d["presets"]):
            return {"ok": False, "error": f"软件包 {name} 已在清单中"}
        valid = {c["k"] for c in d["cats"]}
        if cat and cat not in valid:
            return {"ok": False, "error": f"分类 {cat} 不存在"}
        d["presets"].append({"c": cat or (d["cats"][0]["k"] if d["cats"] else ""),
                             "n": name, "l": (label or name).strip()[:80],
                             "d": (desc or "").strip()[:200]})
        save(conn, d)
        return {"ok": True, "catalog": _normalize(d)}


def remove_preset(conn, name: str) -> dict:
    with _LOCK:
        name = (name or "").strip()
        d = get(conn)
        before = len(d["presets"])
        d["presets"] = [p for p in d["presets"] if p["n"] != name]
        if len(d["presets"]) == before:
            return {"ok": False, "error": f"清单中没有 {name}"}
        # 同时把该包从各套件里摘掉；套件空则删除套件
        for s in d["suites"]:
            s["pkgs"] = [p for p in s["pkgs"] if p != name]
        d["suites"] = [s for s in d["suites"] if s["pkgs"]]
        save(conn, d)
        return {"ok": True, "catalog": _normalize(d)}


def add_cat(conn, key: str, label: str) -> dict:
    with _LOCK:
        key = (key or "").strip()
        label = (label or "").strip()
        if not key or not label:
            return {"ok": False, "error": "分类标识与名称都不能为空"}
        if not re.match(r"^[A-Za-z0-9_-]{1,32}$", key):
            return {"ok": False, "error": "分类标识只允许字母、数字、下划线、连字符"}
        d = get(conn)
        if any(c["k"] == key for c in d["cats"]):
            return {"ok": False, "error": f"分类 {key} 已存在"}
        d["cats"].append({"k": key, "l": label[:80]})
        save(conn, d)
        return {"ok": True, "catalog": _normalize(d)}


def remove_cat(conn, key: str) -> dict:
    with _LOCK:
        key = (key or "").strip()
        d = get(conn)
        if not any(c["k"] == key for c in d["cats"]):
            return {"ok": False, "error": f"分类 {key} 不存在"}
        n = sum(1 for p in d["presets"] if p["c"] == key)
        if n:
            return {"ok": False, "error": f"该分类下还有 {n} 个软件包，请先移除或改分类"}
        d["cats"] = [c for c in d["cats"] if c["k"] != key]
        save(conn, d)
        return {"ok": True, "catalog": _normalize(d)}


def add_suite(conn, key: str, label: str, pkgs) -> dict:
    with _LOCK:
        key = (key or "").strip()
        if not key:
            return {"ok": False, "error": "套件标识不能为空"}
        if not re.match(r"^[A-Za-z0-9_-]{1,32}$", key):
            return {"ok": False, "error": "套件标识只允许字母、数字、下划线、连字符"}
        if isinstance(pkgs, str):
            pkgs = [p.strip() for p in re.split(r"[,\s]+", pkgs) if p.strip()]
        pkgs = [str(p).strip() for p in (pkgs or []) if str(p).strip()]
        if not pkgs:
            return {"ok": False, "error": "套件至少需要一个软件包"}
        # 包名必须合法（原实现完全不校验，可写入任意字符 —— 会随构建请求
        # 进入 make 的 PACKAGES= 变量，属注入的前置条件）
        if len(pkgs) > MAX_SUITE_PKGS:
            return {"ok": False, "error": f"单个套件最多 {MAX_SUITE_PKGS} 个软件包"}
        for x in pkgs:
            if not PKG_NAME_RE.match(x):
                return {"ok": False, "error": f"套件中的软件包名非法：{x[:40]}"}
        d = get(conn)
        if any(s["k"] == key for s in d["suites"]):
            return {"ok": False, "error": f"套件 {key} 已存在"}
        d["suites"].append({"k": key, "l": (label or key).strip()[:80], "pkgs": pkgs})
        save(conn, d)
        return {"ok": True, "catalog": _normalize(d)}


def remove_suite(conn, key: str) -> dict:
    with _LOCK:
        key = (key or "").strip()
        d = get(conn)
        before = len(d["suites"])
        d["suites"] = [s for s in d["suites"] if s["k"] != key]
        if len(d["suites"]) == before:
            return {"ok": False, "error": f"套件 {key} 不存在"}
        save(conn, d)
        return {"ok": True, "catalog": _normalize(d)}
