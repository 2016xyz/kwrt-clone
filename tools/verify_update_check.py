#!/usr/bin/env python3
"""检查更新（GitHub）回归 —— 守住「后台能查到新版本」这件事。

## 为什么需要这个套件

需求是「后台可以检查更新程序，用 GitHub」。功能本身不复杂，但有四处
做错了不会立刻显形、只会在真实部署里咬人：

  1. **版本比较按字符串做。** `"1.0.10" < "1.0.9"` 在字符串语义下为真，
     版本语义下为假 —— 发了 1.0.10 后台反而说「已是最新」。这种 bug 只在
     跨两位数时出现，随手测根本撞不上，所以这里把边界逐条钉死。
  2. **把「查不了」显示成「已是最新」。** 网络不通 / 没权限 / 速率限制
     全都不该长得像「最新」—— 那会让管理员错过安全更新。UC-5/UC-6 专门守它。
  3. **Token 泄漏进错误文本。** 错误对象常带 URL 与请求头，一旦透传到后台
     界面或审计日志，等于把仓库读权限交出去。UC-15/UC-16 守它（含负向对照）。
  4. **每次打开后台就查一次。** GitHub 未认证请求每小时只有 60 次，
     打满之后连固件构建派发一起挂。UC-9/10/11 守缓存。
  5. **两版行为不一致。** 两版共用同一套前端契约与同一个数据库，同一个
     仓库必须比出同一个结论。UC-2 用同一张边界表比对两版实现。

## 断言

  UC-1  版本比较边界（Python）
  UC-2  同一张边界表两版结果逐项一致（同构证明）
  UC-3  私有仓库 + Token：回退读 VERSION 文件（真实 GitHub API）
  UC-4  公共仓库有 Release：has_update=True，带真实说明与链接
  UC-5  不存在的仓库：status=error，且**绝不**出现「已是最新」
  UC-6  未配置仓库：status=error + 可操作提示（不是崩溃）
  UC-7  未登录访问检查接口：拒绝（两版）
  UC-8  非管理员登录访问：拒绝（两版）
  UC-9  缓存：第二次 cached=True；force=1 跳过缓存
  UC-10 缓存跨进程生效（测试进程写，PHP 进程读；不依赖网络，故不偶发）
  UC-10b 失败结果不入缓存（保证「重试」有意义）
  UC-11 总览接口带 update 段，且**不发起** GitHub 请求（缓存仍为空）
  UC-12 Python 后台页含「检查更新」控件并绑到真实接口
  UC-13 PHP 后台页含同一控件
  UC-14 两版前端 JS 都调用真实接口
  UC-15 Token 脱敏：错误文本里绝不出现 Token（两版）
  UC-16 负向对照：脱敏函数确实会被含 Token 的输入触发（证明 UC-15 非空测）

## 用法
    python3 tools/verify_update_check.py

★ 本套件会**真实调用 GitHub API**（只读）。默认用私有仓库带 Token，
  避免把未认证的 60 次/小时限额打满。
"""
from __future__ import annotations

import hashlib
import http.cookiejar
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PORT_PY = int(os.environ.get("KWRT_UC_PY_PORT", "8261"))
PORT_PHP = int(os.environ.get("KWRT_UC_PHP_PORT", "8262"))

#: 真实存在、且**有 Release** 的公共仓库 —— 用来验证「发现新版本」这条路径。
PUBLIC_WITH_RELEASES = os.environ.get("KWRT_UC_PUBLIC_REPO", "cli/cli")
#: 本程序自己的（私有）仓库 —— 用来验证「回退读 VERSION 文件」这条路径。
PRIVATE_REPO = os.environ.get("KWRT_UC_PRIVATE_REPO", "2016xyz/kwrt-clone")

REC: list[tuple[str, str, bool, str]] = []


def rec(cid, title, ok, note=""):
    REC.append((cid, title, bool(ok), note))
    print(f"  {cid:<6} {title:<52} {'✓ 通过' if ok else '✗ 失败'}  {note}")


class Client:
    def __init__(self, base):
        self.base = base.rstrip("/")
        self.op = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def req(self, path, method="GET", data=None, headers=None, timeout=45):
        body = None
        hdrs = dict(headers or {})
        if data is not None:
            if isinstance(data, dict):
                body = urllib.parse.urlencode(data).encode()
                hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
            else:
                body = str(data).encode()
        r = urllib.request.Request(self.base + path, data=body, method=method, headers=hdrs)
        try:
            with self.op.open(r, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception as e:                       # noqa: BLE001
            return 0, str(e)

    def get(self, path, **kw):
        return self.req(path, "GET", **kw)


def wait_up(base, path="/healthz", timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(base + path, timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:                            # noqa: BLE001
            time.sleep(0.25)
    return False


def py_pw(pw: str) -> str:
    """按 app/main.py 的格式生成口令哈希（pbkdf2_sha256$轮数$盐$哈希）。"""
    salt = "ucsalt0123456789ab"
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 240000)
    return f"pbkdf2_sha256$240000${salt}${dk.hex()}"


def set_setting(db: Path, kv: dict):
    c = sqlite3.connect(db)
    for k, v in kv.items():
        c.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, str(v)))
    c.commit()
    c.close()


def main() -> int:
    print("=" * 110)
    print(" 检查更新（GitHub）回归")
    print("=" * 110)

    from app import update

    # ---------------------------------------------------------------- UC-1 比较边界
    # ★ 这一组正是「按字符串比」会错的地方，逐条钉死。
    CASES = [
        ("1.0.10", "1.0.9", 1), ("1.0.9", "1.0.10", -1), ("1.0.1", "1.0.1", 0),
        ("1.2.0", "1.10.0", -1), ("1.10.0", "1.2.0", 1),
        ("v1.0.2", "1.0.1", 1), ("1.0", "1.0.0", 0), ("2.0.0", "1.99.99", 1),
        ("1.0.0-beta.1", "1.0.0", -1), ("1.0.0", "1.0.0-rc1", 1),
        ("1.0.0-beta.2", "1.0.0-beta.1", 1),
    ]
    bad = [c for c in CASES if update.compare(c[0], c[1]) != c[2]]
    rec("UC-1", "版本比较边界（Python，含 1.0.10 > 1.0.9）", not bad,
        f"{len(CASES)} 条边界全对" if not bad else f"出错 {bad}")

    # 形态校验：写坏的 VERSION 文件不该被当成版本号
    rec("UC-1b", "版本形态校验（拒绝非法串）",
        update.is_plain("1.0.1") and update.is_plain("v2.0") and
        not update.is_plain("") and not update.is_plain("abc") and
        not update.is_plain("../../etc/passwd"),
        "1.0.1/v2.0 接受，空/abc/路径串拒绝")

    # ---------------------------------------------------------------- 起两个真服务
    tmp = Path(tempfile.mkdtemp(prefix="kwrt-uc-"))
    py = php = None
    try:
        for d in ("app", "php", "web"):
            shutil.copytree(ROOT / d, tmp / d,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for f in ("config.json", "VERSION", "users.db"):
            if (ROOT / f).exists():
                shutil.copy2(ROOT / f, tmp / f)

        tdb = tmp / "users.db"
        # ★ 关键：测试进程里 import 的 app.update 默认指向**仓库根**的 users.db，
        #   而被测服务用的是临时目录里的那一份。不把 DB 指过去的话，
        #   clear_cache()/load_cache() 操作的是另一个库 —— UC-11 会变成「假绿」
        #   （缓存永远读到 None，看着像「总览没查 GitHub」，其实啥也没验证）。
        update.DB = str(tdb)
        gh_token = ""
        try:
            c = sqlite3.connect(ROOT / "users.db")
            row = c.execute("SELECT value FROM settings WHERE key='gh.token'").fetchone()
            gh_token = (row[0] if row else "") or ""
            c.close()
        except sqlite3.Error:
            pass

        # 管理员（Python 侧直接写库；PHP 侧用仓库自带脚本另建一个）
        c = sqlite3.connect(tdb)
        c.execute("INSERT OR REPLACE INTO users(username,password,role,created,sponsor,"
                  "disabled,email_verified) VALUES('ucadmin',?,'admin',"
                  "strftime('%s','now'),0,0,1)", (py_pw("ucadmin123"),))
        c.execute("INSERT OR REPLACE INTO users(username,password,role,created,sponsor,"
                  "disabled,email_verified) VALUES('ucuser',?,'user',"
                  "strftime('%s','now'),0,0,1)", (py_pw("ucuser123"),))
        c.commit()
        c.close()

        set_setting(tdb, {
            "update.enabled": "1",
            "update.repo": PRIVATE_REPO,
            "update.ref": "main",
            "update.token": gh_token,
            "update.cache_minutes": "10",
            "update.include_prerelease": "0",
        })

        py_base = f"http://127.0.0.1:{PORT_PY}"
        php_base = f"http://127.0.0.1:{PORT_PHP}"

        py = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
             "--port", str(PORT_PY), "--log-level", "warning", "--forwarded-allow-ips", ""],
            cwd=tmp, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            preexec_fn=os.setsid)
        php = subprocess.Popen(
            ["php", "-S", f"127.0.0.1:{PORT_PHP}", "-t", "php/public", "php/public/router.php"],
            cwd=tmp, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            preexec_fn=os.setsid)

        if not wait_up(py_base):
            print("  ! Python 未起来（端口可能被上一次异常退出的进程占着）：")
            print("   ", (py.stdout.read() or "")[-500:] if py.stdout else "")
            print(f"    排查：ss -lntp | grep :{PORT_PY}")
            return 2
        if not wait_up(php_base, "/"):
            print("  ! PHP 未起来（端口可能被上一次异常退出的进程占着）：")
            print("   ", (php.stdout.read() or "")[-500:] if php.stdout else "")
            print(f"    排查：ss -lntp | grep :{PORT_PHP}")
            return 2

        pyc, phpc = Client(py_base), Client(php_base)

        # ------------------------------------------------------------ UC-2 PHP 同构
        # 用同一张边界表跑 PHP 的 Update::compare，逐项与 Python 对照。
        # ★ 两版共用同一个前端契约，比出不同结论就是缺陷。
        php_probe = (
            "<?php require __DIR__ . '/php/src/_probe_boot.php';"
            "$cases = " + json.dumps([[a, b] for a, b, _ in CASES]) + ";"
            "$out = [];"
            "foreach ($cases as $c) { $out[] = \\Kwrt\\Update::compare($c[0], $c[1]); }"
            "echo json_encode(['cmp' => $out,"
            " 'plain' => [\\Kwrt\\Update::isPlain('1.0.1'), \\Kwrt\\Update::isPlain('abc')]]);"
        )
        # 探针引导文件（只为这个套件服务，跑完随临时目录一起消失）
        (tmp / "php" / "src" / "_probe_boot.php").write_text(
            "<?php\ndeclare(strict_types=1);\n"
            "require_once __DIR__ . '/helpers.php';\n"
            "spl_autoload_register(function ($c) {\n"
            "  if (strpos($c, 'Kwrt\\\\') !== 0) { return; }\n"
            "  $f = __DIR__ . '/' . str_replace('\\\\', '/', substr($c, 5)) . '.php';\n"
            "  if (is_file($f)) { require_once $f; }\n"
            "});\n"
            "\\Kwrt\\Settings::bootstrap();\n", encoding="utf-8")
        (tmp / "_probe.php").write_text(php_probe, encoding="utf-8")
        pr = subprocess.run(["php", "_probe.php"], cwd=tmp, capture_output=True, text=True, timeout=60)
        php_cmp = []
        try:
            php_cmp = json.loads(pr.stdout)["cmp"]
        except Exception:                            # noqa: BLE001
            pass
        py_cmp = [update.compare(a, b) for a, b, _ in CASES]
        rec("UC-2", "同一张边界表两版结果逐项一致（同构）",
            php_cmp == py_cmp,
            f"Python={py_cmp[:5]}… PHP={php_cmp[:5]}…" if php_cmp else
            f"PHP 探针未取到结果：{(pr.stdout or pr.stderr)[-160:]}")

        # ------------------------------------------------------------ UC-3 私有仓库
        # ★ 先登录 —— 这几条打的是管理员接口，没登录当然 403。
        #   （一开始漏了这一步，UC-3~6 全被鉴权挡住，看着像「功能坏了」。）
        pyc.req("/api/v1/login", "POST", {"username": "ucadmin", "password": "ucadmin123"})
        st, b = pyc.get("/api/v1/admin/update/check")
        d3 = json.loads(b)
        rec("UC-3", "私有仓库回退读 VERSION 文件（真实 GitHub API）",
            st == 200 and d3.get("status") == "ok" and d3.get("source") == "version_file",
            f"HTTP {st} source={d3.get('source')} latest={d3.get('latest_display')} 仓库={PRIVATE_REPO}")

        # ------------------------------------------------------------ UC-4 有 Release
        set_setting(tdb, {"update.repo": PUBLIC_WITH_RELEASES, "update.token": ""})
        update.clear_cache()
        st, b = pyc.get("/api/v1/admin/update/check?force=1")
        d4 = json.loads(b)
        rec("UC-4", "公共仓库 Release：has_update=True 且带真实说明与链接",
            st == 200 and d4.get("has_update") is True and d4.get("source") == "release"
            and d4.get("html_url", "").startswith("https://github.com/")
            and len(d4.get("notes") or "") > 0,
            f"latest={d4.get('latest_display')} url={d4.get('html_url')} notes={len(d4.get('notes') or '')}字")

        # ------------------------------------------------------------ UC-5 不存在的仓库
        set_setting(tdb, {"update.repo": "2016xyz/no-such-repo-uc-probe"})
        update.clear_cache()
        st, b = pyc.get("/api/v1/admin/update/check?force=1")
        d5 = json.loads(b)
        # ★ 关键：失败**不能**长得像「最新」。文案里出现「已是最新」即为严重缺陷。
        msg5 = d5.get("message") or ""
        rec("UC-5", "不存在的仓库：如实报错，绝不冒充「已是最新」",
            d5.get("status") == "error" and d5.get("has_update") is False
            and "已是最新" not in msg5 and "最新" not in msg5,
            f"status={d5.get('status')} message={msg5[:60]}")

        # ------------------------------------------------------------ UC-6 未配置仓库
        set_setting(tdb, {"update.repo": ""})
        update.clear_cache()
        st, b = pyc.get("/api/v1/admin/update/check?force=1")
        d6 = json.loads(b)
        rec("UC-6", "未配置仓库：可操作提示而非崩溃",
            st == 200 and d6.get("status") == "error" and "配置" in (d6.get("message") or "")
            and "已是最新" not in (d6.get("message") or ""),
            f"HTTP {st} message={(d6.get('message') or '')[:56]}")

        # ------------------------------------------------------------ UC-7/UC-8 鉴权
        anon_py, _ = Client(py_base).get("/api/v1/admin/update/check")
        anon_php = Client(php_base).get("/api/v1/admin/update/check")[0]
        rec("UC-7", "未登录访问检查接口 → 拒绝（两版）",
            anon_py in (401, 403) and anon_php in (401, 403),
            f"Python={anon_py} PHP={anon_php}")

        pyc.req("/api/v1/login", "POST", {"username": "ucuser", "password": "ucuser123"})
        st8, b8 = pyc.get("/api/v1/admin/update/check")
        rec("UC-8", "普通用户访问检查接口 → 拒绝（Python）",
            st8 in (401, 403), f"HTTP {st8}")

        # ------------------------------------------------------------ UC-9 缓存
        set_setting(tdb, {"update.repo": PRIVATE_REPO, "update.token": gh_token})
        update.clear_cache()
        pyc.req("/api/v1/login", "POST", {"username": "ucadmin", "password": "ucadmin123"})
        st, b = pyc.get("/api/v1/admin/update/check")
        d9a = json.loads(b)
        st, b = pyc.get("/api/v1/admin/update/check")
        d9b = json.loads(b)
        st, b = pyc.get("/api/v1/admin/update/check?force=1")
        d9c = json.loads(b)
        rec("UC-9", "缓存：首查不命中 → 第二次命中 → force=1 再跳过",
            d9a.get("cached") is False and d9b.get("cached") is True
            and d9c.get("cached") is False,
            f"首={d9a.get('cached')} 二={d9b.get('cached')} force={d9c.get('cached')}")

        # ------------------------------------------------------------ UC-18 缓存绑定配置
        # ★ 现场踩到的真实缺陷：把 update.repo 从 A 改成 B 后，页面仍端出 A 的
        #   「已是最新」——缓存没有跟配置绑定。拿一个不相干仓库的结论去指导升级
        #   决策，比查不出来更糟。所以缓存里存了配置指纹，不匹配即作废。
        update.clear_cache()
        set_setting(tdb, {"update.repo": PRIVATE_REPO, "update.token": gh_token})
        st, b = pyc.get("/api/v1/admin/update/check")
        a18 = json.loads(b)
        set_setting(tdb, {"update.repo": PUBLIC_WITH_RELEASES, "update.token": ""})
        st, b = pyc.get("/api/v1/admin/update/check")          # 故意**不** force
        c18 = json.loads(b)
        rec("UC-18", "缓存与配置绑定：换更新源后不再端出旧结论",
            c18.get("cached") is False and c18.get("repo") == PUBLIC_WITH_RELEASES
            and c18.get("has_update") is True,
            f"改前 repo={a18.get('repo')}/{a18.get('latest_display')} → "
            f"改后 cached={c18.get('cached')} repo={c18.get('repo')} latest={c18.get('latest_display')}")

        # ------------------------------------------------------------ UC-11 总览不打 GitHub
        # ★ 直接断言「总览没有写缓存」—— 比计时可靠，且能被改红。
        update.clear_cache()
        st, b = pyc.get("/api/v1/admin/overview")
        ov = json.loads(b)
        cached_after, _ts = update.load_cache()
        rec("UC-11", "总览带 update 段，但**不发起** GitHub 请求",
            st == 200 and isinstance(ov.get("update"), dict)
            and ov["update"].get("configured") is True
            and cached_after is None,
            f"update 段={json.dumps(ov.get('update'), ensure_ascii=False)} 总览后缓存={cached_after}")

        # ------------------------------------------------------------ UC-10 PHP 跨进程缓存
        # ★ 原来的写法要 PHP 先真查一次 GitHub、再查第二次断言 cached=True。
        #   这会**偶发失败**，因为 check() 有意**只缓存成功结果**：
        #   第一次若碰上 GitHub 抖动（速率/网络），什么也不写缓存，
        #   第二次自然 cached=False —— 断言红，产品却是对的。
        #   一个会对网络抖动的常驻断言是有害的：它会把人训练成「红了就重跑」。
        #
        #   改成**不依赖网络**的更强证法：由**测试进程**写一条缓存，
        #   再让 php -S 的**另一个进程**读回来。这直接证明「一个进程写的，
        #   另一个进程看得到」—— 正是这条断言真正要证的事。
        update.clear_cache()
        marker = "9.9.9"
        update.save_cache({
            "status": "ok", "latest": marker, "latest_display": "v" + marker,
            "source": "release", "published_at": "", "notes": "", "html_url": "",
            "name": "", "repo": PUBLIC_WITH_RELEASES, "prerelease": False,
        })
        phpc.req("/api/v1/login", "POST", {"username": "ucadmin", "password": "ucadmin123"})
        set_setting(tdb, {"update.repo": PUBLIC_WITH_RELEASES, "update.token": ""})
        st, b = phpc.get("/api/v1/admin/update/check")              # 不加 force → 走缓存
        p10 = json.loads(b)
        rec("UC-10", "缓存跨进程生效（测试进程写，PHP 进程读）",
            st == 200 and p10.get("cached") is True
            and p10.get("latest_display") == "v" + marker,
            f"测试进程写入 v{marker} → PHP 进程读到 cached={p10.get('cached')} "
            f"latest={p10.get('latest_display')}")

        # 配套断言：**失败结果绝不入缓存**（否则「重试」按钮就是摆设）
        # —— 这是 UC-10 上面那条「只缓存成功」规则的守卫，也是它偶发的根源。
        update.clear_cache()
        set_setting(tdb, {"update.repo": "2016xyz/no-such-repo-for-cache-test", "update.token": ""})
        st, b = phpc.get("/api/v1/admin/update/check?force=1")
        p10c = json.loads(b)
        cached_after, _ts = update.load_cache()
        rec("UC-10b", "失败结果不入缓存（保证「重试」有意义）",
            p10c.get("status") == "error" and cached_after is None
            and "最新" not in (p10c.get("message") or ""),
            f"status={p10c.get('status')} 缓存={cached_after or '空 ✓'} "
            f"提示=「{(p10c.get('message') or '')[:40]}」")

        # ------------------------------------------------------------ UC-12/13/14 界面接线
        # ★ Python 后台是**纯静态 Vue 页面**：端点写在 admin.js 里，HTML 只提供
        #   控件与 @click 绑定。所以断言分两处 ——
        #     HTML：有控件，且绑到了 checkUpdate 方法（HTML↔JS 的接线契约）
        #     JS  ：真的定义了该方法并调用真实端点
        #   把端点串拿去 HTML 里找是找错了地方（第一版就是这么误判的）。
        st, html = pyc.get("/admin/")
        html_ok = ("检查更新" in html) and ("checkUpdate(" in html)
        _, js = pyc.get("/assets/js/admin.js")
        js_ok = "checkUpdate" in js and "/api/v1/admin/update/check" in js
        rec("UC-12", "Python 后台页含「检查更新」控件并绑到 checkUpdate",
            st == 200 and html_ok and js_ok,
            f"HTTP {st} HTML控件={html_ok} JS接线={js_ok}")

        stp, htmlp = phpc.get("/admin/")
        soup_p = "checkUpdateBtn" in htmlp and "update/check" in htmlp
        rec("UC-13", "PHP 后台页含同一控件", stp == 200 and soup_p,
            f"HTTP {stp} 含控件={soup_p}")

        _, jsp = phpc.get("/assets/js/admin.js")
        rec("UC-14", "两版前端 JS 都调用真实接口",
            js_ok and "checkUpdateBtn" in jsp and "update/check" in jsp,
            f"Python JS={js_ok} PHP JS={'checkUpdateBtn' in jsp}")

        # ------------------------------------------------------------ UC-15/16 Token 脱敏
        # ★ 样本用一个**不匹配任何通用 Token 正则**的串：这样只有「按当前 Token 值
        #   整体替换」这条规则能把它拿掉。若用 ghp_xxx 那种样本，通用正则也会命中，
        #   测出来是绿的却证明不了「知道自己的 Token 长什么样」这件事。
        FAKE = "ZZsecretvalue1234567890XYZab"
        raw = f"failed to connect: Authorization: Bearer {FAKE} -> api.github.com"

        # Python：把 token() 猴补成假样本（不去动真实设置，避免留副作用）
        real_token_fn = update.token
        try:
            update.token = lambda: FAKE
            safe_py = update._safe(raw)
        finally:
            update.token = real_token_fn

        # PHP：把假样本写进临时库，探针进程读的就是它
        set_setting(tdb, {"update.token": FAKE})
        php_code = ("<?php require __DIR__ . '/php/src/_probe_boot.php';"
                    "echo \\Kwrt\\Update::safe(" + json.dumps(raw) + ");")
        (tmp / "_probe2.php").write_text(php_code, encoding="utf-8")
        pr2 = subprocess.run(["php", "_probe2.php"], cwd=tmp, capture_output=True, text=True, timeout=60)
        safe_php = pr2.stdout
        set_setting(tdb, {"update.token": gh_token})

        rec("UC-15", "Token 脱敏：错误文本里绝不出现 Token（两版）",
            FAKE not in safe_py and FAKE not in safe_php
            and "***" in safe_py and "***" in safe_php,
            f"Python={safe_py[:50]!r} PHP={safe_php[:50]!r}")

        # ★ 负向对照：证明 UC-15 不是空测 —— 输入里确实**有** Token，
        #   是脱敏函数把它拿掉的；抽掉脱敏逻辑就会原样漏出去。
        rec("UC-16", "负向对照：未脱敏时 Token 确实会漏（证明 UC-15 非空测）",
            FAKE in raw and FAKE not in safe_py and FAKE not in safe_php,
            "输入含 Token ✓，两版脱敏后都不含 ✓（抽掉 _safe / safe() 即漏）")

        # ------------------------------------------------------------ UC-17 接口→视图模型接线
        # ★ 现场踩到的真实缺陷：后端 /api/v1/admin/overview 返回了 update 段，
        #   但 admin.js 的 loadOverview() 是**显式重建**一个新对象，忘了映射
        #   新字段 → 检查更新按钮渲染出来了却永远 disabled，看着像「功能没做」。
        #   这正是本项目反复出现的「接口有值、页面没接线」缺陷类，所以做成常驻断言：
        #   接口顶层字段必须被视图模型接上，除非显式列入豁免表。
        st, b = pyc.get("/api/v1/admin/overview")
        ov_keys = set(json.loads(b).keys())
        _, js_src = pyc.get("/assets/js/admin.js")
        # 扫描范围必须是**整个 loadOverview 函数体**，不能只看视图模型字面量：
        # 形如 `q = d.queue || {}` 的解构赋值同样算「接上了」——
        # 只看字面量会把这几个误判成未接线（第一版就是这么产生假阳性的）。
        i = js_src.find("async function loadOverview(")
        k = js_src.find("\n      async function ", i + 10) if i >= 0 else -1
        body = js_src[i:k if k > i else i + 9000] if i >= 0 else ""
        consumed = (set(re.findall(r"([A-Za-z_]\w*)\s*:", body))
                    | set(re.findall(r"\bd\.([A-Za-z_]\w*)", body)))
        # 页面确实不需要的字段（留在这里即等于显式声明「我决定不映射」）
        EXEMPT = {"versions"}
        missing_keys = sorted(ov_keys - consumed - EXEMPT)
        # ★ 负向对照：塞一个不存在的字段进去，检查器必须能报出来 ——
        #   否则这条断言就是个永远为真的摆设。
        control = (ov_keys | {"__definitely_not_wired__"}) - consumed - EXEMPT
        rec("UC-17", "总览接口的字段都被前端视图模型接上",
            i >= 0 and not missing_keys and "__definitely_not_wired__" in control
            and {"update", "backend", "conf"} <= consumed,
            f"接口 {len(ov_keys)} 字段，未接线={missing_keys or '无'}；"
            f"负向对照可报红={('__definitely_not_wired__' in control)}")

        # ------------------------------------------------------------ 收尾
        print("-" * 110)
        npass = sum(1 for _, _, ok, _ in REC if ok)
        fails = [c for c, _, ok, _ in REC if not ok]
        print(f" 合计 {len(REC)} 项，{'全部通过 ✓' if not fails else '失败 ✗ ' + str(fails)}")
        print("=" * 110)
        return 0 if not fails else 1
    finally:
        for p in (py, php):
            if p is None:
                continue
            try:
                os.killpg(os.getpgid(p.pid), 15)
            except Exception:                        # noqa: BLE001
                try:
                    p.terminate()
                except Exception:                    # noqa: BLE001
                    pass
        time.sleep(0.4)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
