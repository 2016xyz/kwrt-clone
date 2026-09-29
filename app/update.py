"""检查程序自身更新（更新源：GitHub）。

为什么值得单独一个模块
----------------------
这件事看着简单（「取个版本号比大小」），但有四处容易做错，每一处都会
在真实部署里咬人：

1. **版本比较不能按字符串。** `"1.0.10" < "1.0.9"` 在字符串语义下为真，
   在版本语义下为假。用字符串比会导致「发了 1.0.10，后台反而说没有更新」，
   而且这种 bug 只在跨两位数时出现，测试时很难撞上。→ `parse_version()`。

2. **私有仓库要带 token，而 token 绝不能进错误信息。** 异常文本里常带 URL
   与请求头，一旦透传到后台界面或审计日志就泄漏了。→ `_safe()` 统一过一遍。

3. **必须缓存。** GitHub 未认证请求每小时 60 次；管理员反复点「检查更新」
   或后台每次打开都自动查，很容易打满限额，然后所有 GitHub 功能（包括
   固件构建派发）一起挂掉。→ 结果落库，默认缓存 10 分钟。

4. **不能假装成功。** 网络不通、仓库没权限、速率限制 —— 这三种都必须
   如实告诉管理员「查不了，原因是 X」，而不是回一个「已是最新」。
   把失败说成「最新」，会让管理员错过安全更新。→ `status` 二态分明。
"""
from __future__ import annotations

import base64
import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request

from . import dbutil, sitesettings as SS

API = "https://api.github.com"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "users.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS update_checks (
    id         INTEGER PRIMARY KEY,
    checked_at INTEGER NOT NULL,
    payload    TEXT    NOT NULL
);
"""

#: 版本号里允许出现的形态：可带前导 v，可带 -beta.1 之类的预发布后缀
_VERSION_RE = re.compile(r"^v?(\d+(?:\.\d+){0,3})(?:[-+](.*))?$")


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def enabled() -> bool:
    return bool(SS.get("update.enabled", True))


def repo() -> str:
    """更新源仓库。

    ★ 留空时**不**回落到 gh.repo：`gh.repo` 是「固件构建器」仓库，
      和「本程序自己的源码仓库」是两回事。回落到它会让更新检查去比对一个
      完全无关的仓库版本号，比出「有新版本」还更糟 —— 管理员会照着升级指令
      去更新一个不相干的仓库。所以留空就直接报「未配置」，说清楚。
    """
    return str(SS.get("update.repo") or "").strip()


def token() -> str:
    """更新源 Token。留空时回落到 gh.token（同一台机器上通常就是同一个）。"""
    t = str(SS.get("update.token") or "").strip()
    return t or str(SS.get("gh.token") or "").strip()


def include_prerelease() -> bool:
    return bool(SS.get("update.include_prerelease", False))


def ref() -> str:
    return str(SS.get("update.ref") or "main").strip() or "main"


def cache_minutes() -> int:
    try:
        n = int(SS.get("update.cache_minutes", 10))
    except (TypeError, ValueError):
        n = 10
    return max(1, min(1440, n))


# --------------------------------------------------------------------------- #
# 版本比较
# --------------------------------------------------------------------------- #
def _pre_key(s: str) -> tuple:
    """把预发布后缀解析成可比较的键（semver 规则）。

    ★ 只判断「有没有后缀」是不够的：`1.0.0-beta.2` 与 `1.0.0-beta.1` 会被判成
      相同，于是开了 update.include_prerelease 的管理员**永远看不到 beta.2**。
      所以这里逐段比较：
        · 纯数字段按**数值**比（`2` < `10`，字符串比会错）
        · 数字段排在字母段**之前**
        · 字母段按 ASCII 字典序
        · 前缀相同则段数少的更小
    """
    parts = []
    for seg in str(s).split("."):
        if seg.isdigit():
            parts.append((0, int(seg), ""))
        else:
            parts.append((1, 0, seg))
    return tuple(parts)


def parse_version(s: str) -> tuple:
    """把版本串解析成可比较的元组 (数字段, 是否正式版, 预发布键)。

    ★ 数字段逐段转成 **int** 再比 —— 这正是不能按字符串比的原因：
      "1.0.10" > "1.0.9" 只在整数语义下成立。
      第二项：1 = 正式版，0 = 预发布版（正式版**更新**，与 semver 一致）。
    """
    m = _VERSION_RE.match(str(s or "").strip())
    if not m:
        # 认不出来的一律当作 0.0.0 —— 但调用方会用 is_plain() 先挡一道，
        # 不会拿它去做「有更新」的判断。
        return ((), 1, ())
    nums = tuple(int(x) for x in m.group(1).split("."))
    pre = m.group(2)
    if pre:
        return (nums, 0, _pre_key(pre))
    return (nums, 1, ())


def is_plain(s: str) -> bool:
    """能否被当作版本号解析（防 VERSION 文件被写坏后污染判断）。"""
    return bool(_VERSION_RE.match(str(s or "").strip()))


def compare(a: str, b: str) -> int:
    """a 与 b 比较：1 = a 新，-1 = b 新，0 = 相同。"""
    na, fa, pa = parse_version(a)
    nb, fb, pb = parse_version(b)
    n = max(len(na), len(nb))
    na = na + (0,) * (n - len(na))
    nb = nb + (0,) * (n - len(nb))
    if na != nb:
        return 1 if na > nb else -1
    if fa != fb:
        return 1 if fa > fb else -1
    if pa != pb:
        return 1 if pa > pb else -1
    return 0


def current() -> str:
    from . import version as _v
    return _v.VERSION


# --------------------------------------------------------------------------- #
# 数据库 / 缓存
# --------------------------------------------------------------------------- #
def _db():
    """连接工厂收口：`with _db() as c:` = 提交 + 关闭。

    原生 sqlite3.Connection 的上下文管理器只 commit/rollback 不 close，
    连接靠分代 GC 回收 —— 高负载下 fd 会先涨起来（本项目在 verify.py 上
    实测过 300 次调用泄漏 1200 个 fd）。这里统一用 ClosingConnection。
    """
    c = sqlite3.connect(DB, timeout=15, factory=dbutil.ClosingConnection)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init():
    with _db() as c:
        c.executescript(_SCHEMA)


def load_cache():
    """读缓存的检查结果。返回 (payload_dict|None, checked_at|None)。"""
    try:
        init()
        with _db() as c:
            row = c.execute("SELECT checked_at, payload FROM update_checks WHERE id=1").fetchone()
    except sqlite3.Error:
        return None, None
    if not row:
        return None, None
    try:
        return json.loads(row["payload"]), float(row["checked_at"])
    except (ValueError, TypeError):
        # 缓存坏了不该让功能挂掉：当作没有缓存，重新去查
        return None, None


def save_cache(payload: dict):
    init()
    blob = json.dumps(payload, ensure_ascii=False)
    with _db() as c:
        c.execute("DELETE FROM update_checks WHERE id=1")
        c.execute("INSERT INTO update_checks(id, checked_at, payload) VALUES(1, ?, ?)",
                  (int(time.time()), blob))


def clear_cache():
    try:
        init()
        with _db() as c:
            c.execute("DELETE FROM update_checks WHERE id=1")
    except sqlite3.Error:
        pass


# --------------------------------------------------------------------------- #
# GitHub 取数
# --------------------------------------------------------------------------- #
def _safe(text) -> str:
    """把可能出现的 token 从任何对外文本里抹掉。

    ★ 异常对象常带 URL、请求头、代理地址；token 一旦进了后台界面或审计日志，
      就等于把仓库读权限交出去了。这里是唯一的出口，统一过一遍。
    """
    s = str(text or "")
    t = token()
    if t and len(t) >= 8:
        s = s.replace(t, "***")
    # 兜底：任何形似 GitHub token / Bearer 头的串一律打码
    s = re.sub(r"(gh[pousr]_[A-Za-z0-9]{10,}|github_pat_[A-Za-z0-9_]{10,})", "***", s)
    s = re.sub(r"(?i)(authorization\s*[:=]\s*)\S+", r"\1***", s)
    return s[:500]


def _headers() -> dict:
    h = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        # GitHub 强制要求 UA，缺了直接 403
        "User-Agent": "Kwrt-Update-Check/1.0",
    }
    t = token()
    if t:
        h["Authorization"] = f"Bearer {t}"
    return h


def _get(path: str, timeout: int = 20):
    """返回 (data, err)。err 非空即失败 —— 失败必须能被上层如实报告。"""
    if not repo():
        return None, "尚未配置更新源仓库（后台 → 站点设置 → update.repo）"
    if not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", repo()):
        return None, "更新源仓库格式不对，应为 owner/repo"
    url = f"{API}{path}"
    req = urllib.request.Request(url, headers=_headers(), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            # 速率限制即使成功也要看一眼：剩余 0 时下一次就失败了，提前告知
            if resp.headers.get("X-RateLimit-Remaining") == "0":
                return None, ("GitHub API 速率已用尽（未认证请求每小时 60 次）；"
                              "配一个 Token 可提到 5000 次/小时")
            try:
                return json.loads(raw), ""
            except ValueError:
                return None, "GitHub 返回的不是合法 JSON"
    except urllib.error.HTTPError as e:
        code = e.code
        if code == 404:
            return None, f"仓库或分支不存在，或 Token 无权访问（HTTP 404）。仓库={repo()}"
        if code in (401, 403):
            remain = e.headers.get("X-RateLimit-Remaining") if e.headers else None
            if remain == "0":
                return None, "GitHub API 速率已用尽；配一个 Token 可提到 5000 次/小时"
            return None, f"Token 无效或权限不足（HTTP {code}）"
        return None, f"GitHub 返回 HTTP {code}"
    except urllib.error.URLError as e:
        return None, f"连不上 GitHub：{_safe(e.reason)}"
    except Exception as e:                      # noqa: BLE001 —— 兜底也要说人话
        return None, f"请求失败：{_safe(type(e).__name__ + ': ' + str(e))}"


def _release_candidate(releases: list):
    """从 release 列表里挑出「最新且被允许」的那个。

    用列表接口而不是 /releases/latest：后者在没有 release 时 404，
    且**永远排除预发布版**，无法支持「包含预发布」这个开关。
    """
    allow_pre = include_prerelease()
    best = None
    for r in releases or []:
        if not isinstance(r, dict):
            continue
        if r.get("draft"):
            continue
        if r.get("prerelease") and not allow_pre:
            continue
        tag = str(r.get("tag_name") or "").strip()
        if not is_plain(tag):
            continue
        key = (parse_version(tag), str(r.get("published_at") or r.get("created_at") or ""))
        if best is None or key > best[0]:
            best = (key, r)
    return best[1] if best else None


def fetch_latest():
    """取远端最新版本。返回 (info_dict, err)。

    info = {"version","tag","published_at","notes","html_url","source"}
    顺序：先看 Releases（有发布说明与下载链接），没有再退回读分支上的 VERSION 文件。
    """
    data, err = _get(f"/repos/{repo()}/releases?per_page=30")
    if err:
        return None, err
    rel = _release_candidate(data if isinstance(data, list) else [])
    if rel:
        tag = str(rel.get("tag_name") or "").strip()
        return {
            "version": tag[1:] if tag[:1] in ("v", "V") else tag,
            "tag": tag,
            "published_at": str(rel.get("published_at") or ""),
            "notes": str(rel.get("body") or "")[:4000],
            "html_url": str(rel.get("html_url") or ""),
            "name": str(rel.get("name") or ""),
            "source": "release",
        }, ""

    # 退回：读指定分支上的 VERSION 文件
    data, err = _get(f"/repos/{repo()}/contents/VERSION?ref={urllib.parse.quote(ref())}")
    if err:
        return None, err
    if not isinstance(data, dict) or "content" not in data:
        return None, "无法从 GitHub 读取版本信息（既没有 Release，也没有 VERSION 文件）"
    try:
        text = base64.b64decode(data.get("content") or "").decode("utf-8", "replace").strip()
    except Exception:                            # noqa: BLE001
        return None, "VERSION 文件内容无法解码"
    if not is_plain(text):
        return None, f"VERSION 文件内容不是合法版本号：{_safe(text[:40])}"
    return {
        "version": text,
        "tag": "v" + text,
        "published_at": "",
        "notes": "",
        "html_url": str(data.get("html_url") or ""),
        "name": "",
        "source": "version_file",
    }, ""


# --------------------------------------------------------------------------- #
# 对外接口
# --------------------------------------------------------------------------- #
def check(force: bool = False) -> dict:
    """检查更新。返回的 dict 永远有 status 字段（ok / error），不会抛异常。

    ★ 失败时**绝不**返回「已是最新」—— 那会让管理员错过安全更新。
    """
    cur = current()
    out = {
        "status": "ok",
        "current": cur,
        "current_display": "v" + cur,
        "latest": "",
        "latest_display": "",
        "has_update": False,
        "source": "",
        "published_at": "",
        "notes": "",
        "html_url": "",
        "checked_at": 0,
        "cached": False,
        "message": "",
        "repo": repo(),
    }

    if not enabled():
        out["status"] = "error"
        out["message"] = "更新检查已在后台关闭（update.enabled）"
        return out

    if not force:
        payload, ts = load_cache()
        # ★ 缓存必须与**当前配置**绑定。管理员改了更新源仓库（或「包含预发布」
        #   开关）之后，旧仓库得出的结论对新配置毫无意义 —— 继续端出来就是
        #   拿一个不相干仓库的版本号误导升级决策。
        #   实测踩到过：把 update.repo 从 A 改成 B 后，页面仍显示 A 的「已是最新」。
        if (payload and ts and (time.time() - ts) < cache_minutes() * 60
                and payload.get("repo") == repo()
                and bool(payload.get("prerelease")) == include_prerelease()):
            payload = dict(payload)
            payload["cached"] = True
            payload["checked_at"] = int(ts)
            # 当前版本用**本地实时值**覆盖缓存里的旧值：缓存的是「远端最新」，
            # 不是「本地版本」。升级完不该还显示旧版本号。
            payload["current"] = cur
            payload["current_display"] = "v" + cur
            payload["has_update"] = bool(payload.get("latest")) and \
                compare(payload.get("latest", ""), cur) > 0
            payload["message"] = _msg(payload)
            return payload

    info, err = fetch_latest()
    if err or not info:
        out["status"] = "error"
        out["message"] = err or "未能从更新源取到版本信息"
        return out

    latest = info["version"]
    if not is_plain(latest):
        out["status"] = "error"
        out["message"] = f"远端版本号不合法：{_safe(latest[:40])}"
        return out

    out.update({
        "latest": latest,
        "latest_display": "v" + latest,
        "has_update": compare(latest, cur) > 0,
        "source": info["source"],
        "published_at": info["published_at"],
        "notes": info["notes"],
        "html_url": info["html_url"],
        "name": info.get("name", ""),
        "checked_at": int(time.time()),
    })
    out["message"] = _msg(out)

    # ★ 只缓存**成功**的结果：把一次网络故障缓存 10 分钟，会让管理员
    #   点「重试」也拿不到新结果，以为功能坏了。
    save_cache({k: out[k] for k in (
        "status", "latest", "latest_display", "source", "published_at",
        "notes", "html_url", "name", "repo")} | {"prerelease": include_prerelease()})
    return out


def _msg(r: dict) -> str:
    if r.get("has_update"):
        return f"发现新版本 {r.get('latest_display')}（当前 {r.get('current_display')}）"
    return f"已是最新版本 {r.get('current_display')}"


# --------------------------------------------------------------------------- #
# 一键更新（执行 git pull + 依赖安装 + 重启服务）
# --------------------------------------------------------------------------- #
import subprocess
import shutil


def _restart_service(steps: list) -> bool:
    """尝试重启 systemd 服务，返回是否成功。"""
    svc = "kwrt"
    if not shutil.which("systemctl"):
        steps.append({"step": "restart", "ok": False,
                       "stderr": "无 systemctl，请手动重启"})
        return False
    try:
        r = subprocess.run(
            ["systemctl", "restart", svc],
            capture_output=True, text=True, timeout=30)
        steps.append({"step": "restart", "ok": r.returncode == 0,
                       "stdout": r.stdout[-500:], "stderr": r.stderr[-500:]})
        return r.returncode == 0
    except Exception as e:
        steps.append({"step": "restart", "ok": False, "stderr": str(e)})
        return False


def apply_update() -> dict:
    """执行一键更新：git pull → pip install → 重启 systemd 服务。

    返回 dict(status, message, steps)，steps 记录每一步的输出。
    """
    steps = []
    root = ROOT

    # 0. 前置检查
    if not shutil.which("git"):
        return {"status": "error", "message": "服务器未安装 git", "steps": steps}
    if not os.path.isdir(os.path.join(root, ".git")):
        return {"status": "error", "message": "项目目录不是 git 仓库", "steps": steps}

    # 1. git pull
    try:
        r = subprocess.run(
            ["git", "pull", "--ff-only"],
            cwd=root, capture_output=True, text=True, timeout=120)
        steps.append({"step": "git pull", "ok": r.returncode == 0,
                       "stdout": r.stdout[-2000:], "stderr": r.stderr[-1000:]})
        if r.returncode != 0:
            return {"status": "error",
                    "message": f"git pull 失败：{r.stderr[-300:]}",
                    "steps": steps}
    except Exception as e:
        steps.append({"step": "git pull", "ok": False, "stderr": str(e)})
        return {"status": "error", "message": f"git pull 异常：{e}", "steps": steps}

    # 2. pip install（找 venv）
    vpy = None
    for cand in [os.path.join(root, ".venv", "bin", "python"),
                 os.path.join(root, "venv", "bin", "python")]:
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            vpy = cand
            break

    if vpy and os.path.isfile(os.path.join(root, "requirements.txt")):
        try:
            r = subprocess.run(
                [vpy, "-m", "pip", "install", "-q",
                 "-r", os.path.join(root, "requirements.txt"),
                 "-i", "https://pypi.tuna.tsinghua.edu.cn/simple"],
                cwd=root, capture_output=True, text=True, timeout=300)
            steps.append({"step": "pip install", "ok": r.returncode == 0,
                           "stdout": r.stdout[-1000:], "stderr": r.stderr[-1000:]})
        except Exception as e:
            steps.append({"step": "pip install", "ok": False, "stderr": str(e)})
    else:
        steps.append({"step": "pip install", "ok": True,
                       "stdout": "跳过（未找到 venv 或 requirements.txt）"})

    # 3. 重启服务
    restarted = _restart_service(steps)
    clear_cache()

    if restarted:
        return {"status": "ok",
                "message": "更新成功，服务正在重启",
                "steps": steps}
    return {"status": "ok",
            "message": "代码已更新，但未能自动重启服务（请手动重启）",
            "steps": steps}
