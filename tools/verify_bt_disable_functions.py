"""复现「宝塔默认 disable_functions」下的安装向导 —— 必须能装完。

真实现场（op.2016xlx.cn，PHP 8.1 + 宝塔）：
    向导第 2 步选 MySQL 提交后 500
    Uncaught Error: Call to undefined function putenv()
    in public/install.php on line 389

宝塔默认把 putenv（以及 proc_open/exec/shell_exec/symlink 等）写进
disable_functions。被禁用的函数在 PHP 里等同「未定义」：function_exists()
返回 false，而**直接调用会抛致命错误**。向导原先无条件 putenv 来让本次请求
立刻用上新配置，于是必 500 —— 且没有兜底时页面空白，用户拿不到任何信息。

本脚本用 `php -S ... -d disable_functions=...` 起一个和宝塔同构的环境，
把向导从头走到尾，断言：
    DF-1  对照：putenv 确实不可用（否则这个用例没有意义）
    DF-2  SQLite 分支能装完（写锁 + 建管理员）
    DF-3  站点 /healthz 200 且 / 渲染正常
    DF-4  用同一套被禁函数环境走 MySQL 分支（需 KWRT_TEST_MYSQL_HOST）

用法：
    python3 tools/verify_bt_disable_functions.py
    KWRT_TEST_MYSQL_HOST=127.0.0.1 KWRT_TEST_MYSQL_PORT=3306 \
    KWRT_TEST_MYSQL_DB=kwrt_df KWRT_TEST_MYSQL_USER=... KWRT_TEST_MYSQL_PASS=... \
        python3 tools/verify_bt_disable_functions.py
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import pathlib
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
DISABLED = "putenv,proc_open,exec,shell_exec,symlink,popen"
RESULTS: list[tuple[str, str, bool, str]] = []


def rec(rid: str, desc: str, ok: bool, note: str = "") -> None:
    RESULTS.append((rid, desc, bool(ok), note))
    print(f"  {rid:<6} {desc:<46} {'✓ 通过' if ok else '✗ 失败'}  {note}", flush=True)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def build_tree() -> pathlib.Path:
    base = pathlib.Path(tempfile.mkdtemp(prefix="kwrt-df-"))
    app = base / "app"
    ign = shutil.ignore_patterns(".git", "users.db", "users.db-*", "config.local.json",
                                 "install.lock", "install.allow", "__pycache__",
                                 "work", "cache", "store", "*.tar.zst")
    for sub in ("public", "src", "templates", "scripts"):
        shutil.copytree(ROOT / "php" / sub, app / sub, dirs_exist_ok=True, ignore=ign)
    shutil.copy2(ROOT / "php/routes.php", app / "routes.php")
    for f in ("config.json", "VERSION"):
        shutil.copy2(ROOT / f, app / f)
    for d in ("data", "store", "work", "cache"):
        (app / d).mkdir(exist_ok=True)
    return app


class Wizard:
    def __init__(self, port: int) -> None:
        self.base = f"http://127.0.0.1:{port}"
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def get(self, path: str) -> tuple[int, str]:
        try:
            with self.op.open(self.base + path, timeout=30) as r:
                return r.status, r.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "ignore")
        except Exception as e:  # noqa: BLE001
            return 0, f"{type(e).__name__}: {e}"

    def post(self, path: str, data: dict) -> tuple[int, str]:
        body = urllib.parse.urlencode(data).encode()
        try:
            with self.op.open(self.base + path, body, timeout=60) as r:
                return r.status, r.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "ignore")
        except Exception as e:  # noqa: BLE001
            return 0, f"{type(e).__name__}: {e}"


def csrf_of(html: str) -> str:
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    return m.group(1) if m else ""


def serve(app: pathlib.Path, port: int) -> subprocess.Popen:
    return subprocess.Popen(
        ["php", "-d", f"disable_functions={DISABLED}", "-S", f"127.0.0.1:{port}",
         "-t", str(app / "public"), str(app / "public" / "router.php")],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=str(app / "public"))


def main() -> int:
    print("=" * 112)
    print(" 宝塔 disable_functions 下的安装向导验证（putenv 等被禁用）")
    print("=" * 112)
    app = build_tree()
    port = free_port()
    p = serve(app, port)
    time.sleep(2)
    try:
        w = Wizard(port)

        # ---- DF-1 对照：确认 putenv 真的不可用，否则本用例毫无意义 ----
        probe = subprocess.run(
            ["php", "-d", f"disable_functions={DISABLED}", "-r",
             'echo function_exists("putenv") ? "yes" : "no";'],
            capture_output=True, text=True)
        rec("DF-1", "对照：本环境里 putenv 确实不可用", probe.stdout.strip() == "no",
            f"function_exists(putenv)={probe.stdout.strip()}")

        # ---- DF-2 SQLite 分支走完 ----
        st, html = w.get("/install.php")
        csrf = csrf_of(html)
        rec("DF-2a", "向导首页可打开", st == 200 and csrf != "", f"HTTP {st}")
        w.post("/install.php", {"step": "1", "csrf": csrf})
        st, html = w.post("/install.php", {"step": "2", "csrf": csrf, "driver": "sqlite"})
        step3 = 'name="admin_user"' in html
        fatal = "Fatal error" in html or "undefined function" in html
        rec("DF-2b", "第 2 步（SQLite）不 500 且进入第 3 步", st == 200 and step3 and not fatal,
            f"HTTP {st} 进入step3={step3}")
        st, html = w.post("/install.php", {"step": "3", "csrf": csrf, "admin_user": "dfadmin",
                                           "admin_pass": "Df#Passw0rd", "admin_pass2": "Df#Passw0rd",
                                           "admin_email": ""})
        rec("DF-2c", "第 3 步建管理员", st == 200 and "undefined function" not in html, f"HTTP {st}")
        st, html = w.post("/install.php", {"step": "4", "csrf": csrf, "site_name": "DF站",
                                           "domain": "", "force_https": "0"})
        # 向导是 5 步：第 5 步才写 install.lock
        st, html = w.post("/install.php", {"step": "5", "csrf": csrf})
        lock = (app / "data" / "install.lock").is_file()
        rec("DF-2d", "第 5 步完成并写 install.lock", lock and st == 200, f"HTTP {st} lock={lock}")

        # ---- DF-3 站点可用 ----
        st_h, h = w.get("/healthz")
        st_i, body = w.get("/")
        rec("DF-3", "被禁函数环境下站点仍可用", st_h == 200 and '"ok":true' in h and st_i == 200,
            f"healthz={st_h} home={st_i} {len(body)}B")

        # ---- DF-4 MySQL 分支（可选） ----
        mh = os.environ.get("KWRT_TEST_MYSQL_HOST", "")
        if not mh:
            rec("DF-4", "MySQL 分支（未跑）", True, "未设 KWRT_TEST_MYSQL_HOST，跳过")
        else:
            app2 = build_tree()
            port2 = free_port()
            p2 = serve(app2, port2)
            time.sleep(2)
            try:
                w2 = Wizard(port2)
                _, html = w2.get("/install.php")
                csrf2 = csrf_of(html)
                w2.post("/install.php", {"step": "1", "csrf": csrf2})
                st, html = w2.post("/install.php", {
                    "step": "2", "csrf": csrf2, "driver": "mysql",
                    "host": mh, "port": os.environ.get("KWRT_TEST_MYSQL_PORT", "3306"),
                    "dbname": os.environ.get("KWRT_TEST_MYSQL_DB", "kwrt_df"),
                    "user": os.environ.get("KWRT_TEST_MYSQL_USER", "root"),
                    "pass": os.environ.get("KWRT_TEST_MYSQL_PASS", ""), "autocreate": "1"})
                ok = st == 200 and 'name="admin_user"' in html
                note = "HTTP %s" % st
                if not ok:
                    err = re.findall(r'class="[^"]*err[^"]*"[^>]*>(.*?)</', html, re.S)
                    note += " " + (re.sub("<[^>]+>", "", err[0]).strip()[:110] if err else html[:110])
                rec("DF-4", "第 2 步（MySQL）在被禁函数下不 500", ok, note)
            finally:
                p2.kill()
                p2.wait(timeout=10)
                shutil.rmtree(app2.parent, ignore_errors=True)
    finally:
        p.kill()
        out = (p.stdout.read() if p.stdout else "")
        p.wait(timeout=10)
        bad = [l for l in out.split("\n") if "Fatal error" in l or "Uncaught" in l]
        if bad:
            print("  PHP 端致命错误:\n    " + "\n    ".join(bad[:3]))
        shutil.rmtree(app.parent, ignore_errors=True)

    fails = [r[0] for r in RESULTS if not r[2]]
    print("=" * 112)
    print(f" 合计 {len(RESULTS)} 项，" + (f"失败 ✗ {fails}" if fails else "全部通过 ✓"))
    print("=" * 112)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
