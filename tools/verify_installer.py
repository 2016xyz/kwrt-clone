#!/usr/bin/env python3
"""安装向导验证（PHP 版）。

自包含：会自己复制一份**隔离的**仓库副本、起一个 PHP 内置服务器、
跑完所有断言后全部清理。**不会碰你的 users.db 或 config.local.json。**

覆盖：
  I-1..I-3   环境检查页与步骤推进
  I-4..I-8   SQLite 全流程安装 → 锁 → 自我锁定 → 能登录
  I-9..I-12  安全边界：跳步、CSRF、live_unlocked 拦截、install.allow 放行
  I-13..I-15 已完成站点拒绝重装、凭据文件权限、向导不泄露口令
  I-16..I-18 MySQL 路径（有可达 MySQL 时；没有则跳过并说明）

用法：
    python3 tools/verify_installer.py
    # 想连带验 MySQL：
    KWRT_TEST_MYSQL_HOST=127.0.0.1 KWRT_TEST_MYSQL_PORT=13306 \
    KWRT_TEST_MYSQL_USER=kwrt KWRT_TEST_MYSQL_PASS=xxx python3 tools/verify_installer.py
"""
from __future__ import annotations

import http.cookiejar
import json
import os
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
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REC: list[tuple[str, str, bool, str]] = []


def rec(cid: str, title: str, ok: bool, note: str = "") -> None:
    REC.append((cid, title, bool(ok), note))
    print(f"  {cid:<8} {title:<46} {'✓ 通过' if ok else '✗ 失败'}  {note}")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Wizard:
    """一次安装会话（独立 cookie jar = 独立 PHP session）。"""

    def __init__(self, base: str):
        self.base = base
        self.cj = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cj))

    def get(self, path="/install.php"):
        try:
            with self.op.open(self.base + path, timeout=30) as x:
                return x.status, x.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "ignore")

    def post(self, data: dict, path="/install.php"):
        q = urllib.request.Request(
            self.base + path, data=urllib.parse.urlencode(data).encode(), method="POST")
        q.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with self.op.open(q, timeout=90) as x:
                return x.status, x.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "ignore")

    @staticmethod
    def csrf(html: str) -> str:
        m = re.search(r'name="csrf" value="([^"]+)"', html)
        return m.group(1) if m else ""

    @staticmethod
    def heading(html: str) -> str:
        m = re.search(r"<h2[^>]*>(.*?)</h2>", html, re.S)
        return re.sub(r"\s+", " ", m.group(1)).strip() if m else "(无标题)"

    @staticmethod
    def errors(html: str) -> list[str]:
        return re.findall(r'<div class="err">(.*?)</div>', html, re.S)


def setup_copy(dst: Path) -> None:
    """复制仓库到隔离副本，去掉运行数据。"""
    shutil.copytree(
        ROOT, dst, dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(
            ".git", "users.db", "users.db-*", "store", "work", "cache",
            "config.local.json", "install.lock", "install.allow", "__pycache__"))


def start_server(dst: Path, port: int, env_extra: dict | None = None):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("KWRT_") and k not in ("PORT",)}
    env.update(env_extra or {})
    log = Path(tempfile.gettempdir()) / f"kwrt-inst-{port}.log"
    fh = log.open("wb")
    p = subprocess.Popen(
        ["php", "-S", f"127.0.0.1:{port}", "-t", "php/public", "php/public/router.php"],
        cwd=dst, env=env, stdout=fh, stderr=subprocess.STDOUT)
    for _ in range(60):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/install.php", timeout=2).read(10)
            return p, log
        except urllib.error.HTTPError:
            return p, log
        except Exception:
            time.sleep(0.5)
    p.terminate()
    raise RuntimeError(f"服务器未就绪，日志：{log}")


def main() -> int:
    print("=" * 96)
    print(" 安装向导验证（PHP 版）")
    print("=" * 96)

    port = free_port()
    tmp = Path(tempfile.mkdtemp(prefix="kwrt-wizard-"))
    dst = tmp / "app"
    proc = None
    try:
        setup_copy(dst)
        proc, log = start_server(dst, port)
        B = f"http://127.0.0.1:{port}"

        # ---------------------------------------------- SQLite 全流程
        w = Wizard(B)
        st, h = w.get()
        rec("I-1", "向导可打开且是环境检查步", st == 200 and "环境检查" in h, f"HTTP {st}")
        rec("I-2", "环境检查必需项全通过", 'class="bad">✗' not in h,
            "有必需项未通过" if 'class="bad">✗' in h else "")

        st, h = w.post({"csrf": w.csrf(h), "step": "1"})
        rec("I-3", "环境检查 → 数据库", "② 数据库" in h, w.heading(h))

        st, h = w.post({"csrf": w.csrf(h), "step": "2", "driver": "sqlite"})
        rec("I-4", "SQLite 建库 → 管理员步", "③ 管理员账户" in h, w.heading(h))

        st, h = w.post({"csrf": w.csrf(h), "step": "3", "admin_user": "wizadmin",
                        "admin_pass": "Wiz#Pass123", "admin_pass2": "Wiz#Pass123",
                        "admin_email": "w@example.com"})
        rec("I-5", "建管理员 → 站点设置步", "④ 站点设置" in h, w.heading(h))

        st, h = w.post({"csrf": w.csrf(h), "step": "4", "site_name": "向导装的站",
                        "domain": "", "trusted_hosts": "", "force_https": ""})
        rec("I-6", "站点设置 → 完成步", "⑤ 完成" in h, w.heading(h))

        st, h = w.post({"csrf": w.csrf(h), "step": "5"})
        rec("I-7", "完成安装并写锁", "安装完成" in h and (dst / "data/install.lock").is_file())
        lock = json.loads((dst / "data/install.lock").read_text()) if (dst / "data/install.lock").is_file() else {}
        rec("I-8", "锁内容记录了驱动/管理员/版本",
            lock.get("admin") == "wizadmin" and lock.get("driver") == "sqlite"
            and bool(lock.get("installed_at")), str(lock)[:70])

        # ---------------------------------------------- 安装后可用
        st, h = w.get("/healthz")
        rec("I-9", "安装后 /healthz 可用", st == 200 and '"ok":true' in h, f"HTTP {st}")

        w2 = Wizard(B)
        st, h = w2.post({"username": "wizadmin", "password": "Wiz#Pass123"}, "/api/v1/login")
        rec("I-10", "建出的管理员能登录", st == 200 and '"status":"ok"' in h, f"HTTP {st}")

        for p in ("/", "/admin/", "/packages/", "/api/v1/site"):
            st, _ = w2.get(p)
            if st != 200:
                rec("I-11", "安装后关键路由均为 200", False, f"{p} → {st}")
                break
        else:
            rec("I-11", "安装后关键路由均为 200", True)

        # ---------------------------------------------- 锁与安全边界
        st, h = w2.get()
        rec("I-12", "有锁时向导自我锁定", "向导已锁定" in h, w2.heading(h))

        # 删掉锁 → 应被 live_unlocked 拦住（库里有管理员）
        (dst / "data/install.lock").unlink()
        w3 = Wizard(B)
        st, h = w3.get()
        rec("I-13", "删锁后仍被拦（库里已有管理员）", "向导已停止" in h, w3.heading(h))

        # 放行标记 → 可以进
        (dst / "data/install.allow").write_text("")
        w4 = Wizard(B)
        st, h = w4.get()
        rec("I-14", "放 data/install.allow 后放行", "环境检查" in h, w4.heading(h))

        # 跳步：直接 POST step=3 应被拒
        w5 = Wizard(B)
        st, h = w5.get()
        st, h = w5.post({"csrf": w5.csrf(h), "step": "3", "admin_user": "hacker",
                         "admin_pass": "Hacker#123", "admin_pass2": "Hacker#123"})
        ok = "步骤顺序不对" in h or "环境检查" in h or "② 数据库" in h
        rec("I-15", "跳步 POST step=3 被拒", ok, w5.heading(h))

        # CSRF
        w6 = Wizard(B)
        w6.get()
        st, h = w6.post({"csrf": "deadbeef", "step": "1"})
        rec("I-16", "CSRF 错误被拒", "CSRF" in h or "已过期" in h)

        # ---------------------------------------------- 凭据与泄露
        adm = dst / "data/INITIAL_ADMIN.txt"
        ok = adm.is_file() and oct(adm.stat().st_mode)[-3:] == "600"
        rec("I-17", "INITIAL_ADMIN.txt 权限 0600", ok,
            oct(adm.stat().st_mode)[-3:] if adm.is_file() else "文件缺失")

        # 向导页面上不应回显口令明文
        w7 = Wizard(B)
        st, h = w7.get()
        rec("I-18", "向导页面不回显口令", "Wiz#Pass123" not in h)

        # ---------------------------------------------- MySQL 路径（可选）
        mh = os.environ.get("KWRT_TEST_MYSQL_HOST")
        if not mh:
            print("  I-19..   MySQL 路径                              —— 跳过（未设 KWRT_TEST_MYSQL_HOST）")
        else:
            for f in ("config.local.json", "data/install.lock", "data/install.allow",
                      "users.db", "data/INITIAL_ADMIN.txt"):
                pp = dst / f
                if pp.exists():
                    pp.unlink()
            mp = os.environ.get("KWRT_TEST_MYSQL_PORT", "3306")
            db = f"kwrt_vfy_{int(time.time()) % 100000}"
            # 建库要用**有建库权限的账号**（通常 root）。应用账号一般只在
            # 自己那个库上有权限，拿它 CREATE DATABASE 会 Access denied ——
            # 那样测出来的失败是环境问题，不是代码问题。
            au = os.environ.get("KWRT_TEST_MYSQL_ADMIN_USER", "root")
            ap = os.environ.get("KWRT_TEST_MYSQL_ADMIN_PASS", "rootpw")
            app_user = os.environ.get("KWRT_TEST_MYSQL_USER", "root")
            mk = subprocess.run(
                ["mysql", "-h", mh, "-P", mp, "-u", au, f"-p{ap}", "-e",
                 f"DROP DATABASE IF EXISTS {db}; CREATE DATABASE {db} "
                 "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci; "
                 f"GRANT ALL PRIVILEGES ON `{db}`.* TO '{app_user}'@'%'; FLUSH PRIVILEGES;"],
                capture_output=True, text=True)
            if mk.returncode != 0:
                print(f"  I-19..   MySQL 路径                              —— 跳过（建库失败："
                      f"{mk.stderr.strip()[:70]}）；可设 KWRT_TEST_MYSQL_ADMIN_USER/PASS")
                mh = None
            wm = Wizard(B)
            st, h = wm.get()
            st, h = wm.post({"csrf": wm.csrf(h), "step": "1"})
            st, h = wm.post({"csrf": wm.csrf(h), "step": "2", "driver": "mysql", "host": mh,
                             "port": mp, "dbname": db,
                             "user": os.environ.get("KWRT_TEST_MYSQL_USER", "root"),
                             "pass": os.environ.get("KWRT_TEST_MYSQL_PASS", ""), "autocreate": "1"})
            err = wm.errors(h)
            rec("I-19", "MySQL 建库建表 → 管理员步", "③ 管理员账户" in h,
                err[0][:90] if err else wm.heading(h))
            st, h = wm.post({"csrf": wm.csrf(h), "step": "3", "admin_user": "mysqladmin",
                             "admin_pass": "My#Pass1234", "admin_pass2": "My#Pass1234"})
            st, h = wm.post({"csrf": wm.csrf(h), "step": "4", "site_name": "MySQL站"})
            st, h = wm.post({"csrf": wm.csrf(h), "step": "5"})
            rec("I-20", "MySQL 安装完成并写锁", "安装完成" in h)
            wm2 = Wizard(B)
            st, h = wm2.post({"username": "mysqladmin", "password": "My#Pass1234"}, "/api/v1/login")
            rec("I-21", "MySQL 下管理员能登录", st == 200 and '"status":"ok"' in h, f"HTTP {st}")
            r = subprocess.run(
                ["mysql", "-h", mh, "-P", mp, "-u", os.environ.get("KWRT_TEST_MYSQL_USER", "root"),
                 f"-p{os.environ.get('KWRT_TEST_MYSQL_PASS', '')}", "-N", "-e", "SHOW TABLES", db],
                capture_output=True, text=True)
            # ★ 从 Schema 真源派生期望张数，不硬编码 —— 硬编码 18 会在
            #   每次往 schema 加表时变成假红灯（刚加 gh_run_claims 就踩了）。
            import re as _re
            _sch = (ROOT / "php/src/Schema.php").read_text(encoding="utf-8")
            _n = len(_re.findall(r"^\s*'\w+' => 'CREATE TABLE", _sch, _re.M)) // 2
            rec("I-22", f"MySQL 里建出与 schema 一致的表数（{_n}）", len(r.stdout.split()) == _n,
                f"{len(r.stdout.split())} 张")

    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except Exception:
                proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)

    print("=" * 96)
    bad = [r for r in REC if not r[2]]
    print(f" 合计 {len(REC)} 项，" + ("全部通过 ✓" if not bad else f"失败 ✗ {[b[0] for b in bad]}"))
    print("=" * 96)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())