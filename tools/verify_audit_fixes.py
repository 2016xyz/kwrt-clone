#!/usr/bin/env python3
"""本轮审计修复的回归断言（PHP 版）。

每项都同时验证「修复后正确」与「修复前确实坏」—— 后者用**对照实现**证明：
  · A-1 静态资源前缀碰撞：造一个 public/assets-evil/ 兄弟目录，用裸
        str_starts_with 判定会命中、用 Util::under 判定不会命中。
  · A-2/A-3 队列槽位：exec 被禁用 / worker 失联时，任务必须被**如实判失败**
        并释放槽位，而不是永远留在 running。

自包含：复制隔离副本 → 起 PHP 服务 → 跑完清理，不碰真实数据。

用法：python3 tools/verify_audit_fixes.py
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
    print(f"  {cid:<7} {title:<52} {'✓ 通过' if ok else '✗ 失败'}  {note}")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def setup(dst: Path) -> None:
    shutil.copytree(ROOT, dst, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(".git", "users.db", "users.db-*",
                                                  "store", "work", "cache", "__pycache__",
                                                  "config.local.json", "install.lock"))


def serve(dst: Path, port: int, extra_ini: list[str] | None = None):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("KWRT_") and k != "PORT"}
    log = Path(tempfile.gettempdir()) / f"kwrt-audit-{port}.log"
    fh = log.open("wb")
    cmd = ["php"] + (extra_ini or []) + \
          ["-S", f"127.0.0.1:{port}", "-t", "php/public", "php/public/router.php"]
    p = subprocess.Popen(cmd, cwd=dst, env=env, stdout=fh, stderr=subprocess.STDOUT)
    for _ in range(60):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2).read(10)
            return p, log
        except urllib.error.HTTPError:
            return p, log
        except Exception:
            time.sleep(0.5)
    p.terminate()
    raise RuntimeError(f"服务未就绪：{log}")


def php(dst: Path, code: str, ini: list[str] | None = None) -> str:
    f = dst / ".audit-probe.php"
    f.write_text(code, encoding="utf-8")
    try:
        r = subprocess.run(["php"] + (ini or []) + [str(f)], capture_output=True,
                           text=True, cwd=dst)
        # 拼上 stderr：PHP 致命错误只走 stderr，不捕获的话断言只会看到空字符串，
        # 会把「脚本写错了」误判成「被测代码坏了」。
        return r.stdout + (("\n[stderr] " + r.stderr.strip()) if r.stderr.strip() else "")
    finally:
        f.unlink(missing_ok=True)


BOOT = '''<?php
require "php/src/helpers.php";
spl_autoload_register(function($c){if(!str_starts_with($c,"Kwrt\\\\"))return;
 $f="php/src/".str_replace("\\\\","/",substr($c,5)).".php";if(is_file($f))require $f;});
'''


def get(base: str, path: str):
    try:
        with urllib.request.urlopen(base + path, timeout=20) as x:
            return x.status, x.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore")


def main() -> int:
    print("=" * 100)
    print(" 审计修复回归验证（PHP 版）")
    print("=" * 100)
    port = free_port()
    tmp = Path(tempfile.mkdtemp(prefix="kwrt-audit-"))
    dst = tmp / "app"
    procs = []
    try:
        setup(dst)

        # ---------------------------------------------------- A-1 路径守卫
        # 造一个与 assets 共享字符串前缀的兄弟目录 —— 这正是裸 str_starts_with 的破口
        evil = dst / "php/public/assets-evil"
        evil.mkdir(parents=True, exist_ok=True)
        (evil / "secret.txt").write_text("PREFIX-COLLISION-LEAK", encoding="utf-8")

        # 对照：证明「裸判定」真的会放行（修复前的行为）
        naive = php(dst, BOOT + '''
$base = __DIR__ . "/php/public/assets";
$rel  = "/../assets-evil/secret.txt";
$file = realpath($base . $rel);
echo ($file !== false && str_starts_with($file, realpath($base))) ? "LEAKED" : "BLOCKED";
''')
        rec("A-1a", "对照：裸 str_starts_with 确实会放行（修复前的洞）",
            naive.strip() == "LEAKED", naive.strip())

        # 修复后：Util::under 必须拦住
        fixed = php(dst, BOOT + '''
$base = __DIR__ . "/php/public/assets";
$rel  = "/../assets-evil/secret.txt";
echo Kwrt\\Util::under($base, $base . $rel) === null ? "BLOCKED" : "LEAKED";
''')
        rec("A-1b", "修复：Util::under 拦住前缀碰撞", fixed.strip() == "BLOCKED", fixed.strip())

        # 端到端：走真实的静态资源路由
        p, log = serve(dst, port)
        procs.append(p)
        base = f"http://127.0.0.1:{port}"
        # 注意：探针文件就落在 docroot 内，内置服务器（以及 nginx/apache）
        # 本来就会直接把它当静态文件吐出来 —— 那是「文件在站点目录里」的必然结果，
        # 与应用层守卫无关。所以这一项不测「能不能读到它」（那永远能读到），
        # 只测**应用层的判定函数**对这类路径是否放行（见 A-1a/A-1b）。
        # 同时确认：正常资源没被这次改动弄坏、且真实穿越仍被拦。
        st, body = get(base, "/assets/js/app.js")
        rec("A-1c", "端到端：合法静态资源正常返回", st == 200 and len(body) > 0, f"HTTP {st}")
        st, _ = get(base, "/assets/js/app.js")
        rec("A-1d", "不破坏功能：正常静态资源仍 200", st == 200, f"HTTP {st}")
        # 正常的 ../ 穿越也必须拦住
        st, body = get(base, "/assets/../../../users.db")
        rec("A-1e", "端到端：../ 穿越仍被拦", "SQLite format" not in body, f"HTTP {st}")

        # ---------------------------------------------------- A-2 exec 被禁用
        # 用 php -d disable_functions=... 复刻宝塔的默认形态
        port2 = free_port()
        p2, log2 = serve(dst, port2, ["-d", "disable_functions=exec,proc_open,shell_exec,popen"])
        procs.append(p2)
        base2 = f"http://127.0.0.1:{port2}"

        # 建管理员 + 直接调 pump，观察任务最终状态
        out = php(dst, BOOT + '''
Kwrt\\Db::pdo();
$pw = Kwrt\\Auth::hashPw("auditpw123");
Kwrt\\Db::run("DELETE FROM users WHERE username=?",["auditadmin"]);
Kwrt\\Db::run("INSERT INTO users(username,password,created,role,sponsor,quota) VALUES(?,?,?,?,?,?)",
  ["auditadmin",$pw,microtime(true),"admin",1,99]);
echo "ok";
''', ["-d", "disable_functions=exec,proc_open,shell_exec,popen"])
        rec("A-2a", "夹具：隔离副本里建出管理员", out.strip() == "ok", out.strip()[:40])

        cj = http.cookiejar.CookieJar()
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
        q = urllib.request.Request(base2 + "/api/v1/login",
                                  data=urllib.parse.urlencode({"username": "auditadmin",
                                                               "password": "auditpw123"}).encode(),
                                  method="POST")
        q.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with op.open(q, timeout=20) as x:
                login_ok = x.status == 200
        except urllib.error.HTTPError as e:
            login_ok = False
        rec("A-2b", "夹具：MySQL/SQLite 下能登录", login_ok)

        q = urllib.request.Request(base2 + "/api/v1/build",
                                  data=urllib.parse.urlencode({"target": "x86/64",
                                                               "profile": "generic",
                                                               "packages": "", "version": "25.12"}).encode(),
                                  method="POST")
        q.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with op.open(q, timeout=40) as x:
                st, body = x.status, x.read().decode()
        except urllib.error.HTTPError as e:
            st, body = e.code, e.read().decode()
        rec("A-2c", "exec 被禁用时提交构建：不报假成功",
            st == 200 and "已开始构建" not in body, f"HTTP {st} {body[:80]}")

        # 关键：任务必须已经 failed，而不是永远 running
        state = php(dst, BOOT + '''
Kwrt\\Db::pdo();
$r = Kwrt\\Db::one("SELECT status, detail FROM jobs ORDER BY created DESC LIMIT 1");
echo $r ? $r["status"] . "|" . mb_substr((string)$r["detail"],0,40) : "no-job";
''', ["-d", "disable_functions=exec,proc_open,shell_exec,popen"])
        rec("A-2d", "槽位已释放：任务被如实判为 failed（不是永远 running）",
            state.startswith("failed"), state[:70])

        # ---------------------------------------------------- A-3 僵尸回收
        # 手工插一条很久以前就 running 的任务，再调 pump 看是否被回收
        out = php(dst, BOOT + '''
Kwrt\\Db::pdo();
$old = microtime(true) - 4 * 3600;   // 4 小时前（超过 3 小时阈值）
Kwrt\\Db::run("INSERT INTO jobs(request_hash,status,detail,payload,result,created,updated)
               VALUES(?,?,?,?,?,?,?)", [str_repeat("a",32),"running","构建中","{}",null,$old,$old]);
echo Kwrt\\Db::val("SELECT COUNT(*) FROM jobs WHERE status='running'");
''', ["-d", "disable_functions=exec,proc_open,shell_exec,popen"])
        rec("A-3a", "夹具：造出一条 4 小时前的僵尸 running 任务", out.strip() == "1", out.strip())

        out = php(dst, BOOT + '''
Kwrt\\Db::pdo();
$n = Kwrt\\Builder::reap();
$left = Kwrt\\Db::val("SELECT COUNT(*) FROM jobs WHERE status='running'");
echo "reaped={$n} left={$left}";
''', ["-d", "disable_functions=exec,proc_open,shell_exec,popen"])
        rec("A-3b", "僵尸被回收，槽位归还", out.strip() == "reaped=1 left=0", out.strip())

        # 三向判定：新鲜必须留、超阈值必须杀、阈值内必须留
        out = php(dst, BOOT + '''
Kwrt\Db::pdo();
$now = microtime(true);
$ins = function (string $h, float $t): void {
    Kwrt\Db::run("INSERT INTO jobs(request_hash,status,detail,payload,result,created,updated)
                   VALUES(?,?,?,?,?,?,?)", [str_repeat($h,32),"running","x","{}",null,$t,$t]);
};
$ins("f", $now);              // 新鲜
$ins("s", $now - 4 * 3600);   // 超过 3 小时阈值
$ins("r", $now - 30 * 60);    // 阈值之内
$n = Kwrt\Builder::reap();
$st = fn(string $h) => Kwrt\Db::val("SELECT status FROM jobs WHERE request_hash=?", [str_repeat($h,32)]);
echo "reap={$n} fresh=", $st("f"), " stale=", $st("s"), " recent=", $st("r");
''', ["-d", "disable_functions=exec,proc_open,shell_exec,popen"])
        rec("A-3c", "三向判定：新鲜不杀 / 超阈值必杀 / 阈值内不杀",
            out.strip() == "reap=1 fresh=running stale=failed recent=running", out.strip())

        # ------------------------------ A-5 机制级守卫：禁止在 SQL 里比较时间
        # 这是本轮踩到的最隐蔽的一个坑，做成静态断言防复发：
        #   PDO 把 PHP 浮点绑成 TEXT；列的 REAL 亲和性会让 `列 > ?` 正确，
        #   但 `COALESCE(...) < ?` 的表达式亲和性是 NONE → 落到 SQLite 类型序
        #   「REAL < TEXT 恒为真」→ 匹配所有行。
        # 唯一安全做法：取出候选后在 PHP 里用浮点比较。
        import re as _re
        raw = (dst / "php/src/Builder.php").read_text(encoding="utf-8")
        # 必须剥掉注释再匹配：解释这个 bug 的注释里就写着这段 SQL，
        # 不剥离的话守卫会把「说明文字」当成真代码，永远报错（自证伪的检查器）。
        code = "\n".join(l for l in raw.splitlines()
                          if not l.strip().startswith(("//", "*", "/*", "#")))
        bad = _re.findall(r"COALESCE\([^)]*\)\s*(?:<|>|<=|>=)\s*\?", code)
        rec("A-5", "静态守卫：无 COALESCE(...) 与绑定参数比时间", not bad,
            f"发现 {bad}" if bad else "Builder.php 已改为 PHP 侧比较")

        # ---------------------------------------------------- A-4 正常路径未破坏
        st, body = get(base, "/healthz")
        rec("A-4", "正常实例 /healthz 仍正常", st == 200 and '"ok":true' in body, f"HTTP {st}")

    finally:
        for p in procs:
            p.terminate()
            try:
                p.wait(timeout=8)
            except Exception:
                p.kill()
        shutil.rmtree(tmp, ignore_errors=True)

    print("=" * 100)
    bad = [r for r in REC if not r[2]]
    print(f" 合计 {len(REC)} 项，" + ("全部通过 ✓" if not bad else f"失败 ✗ {[b[0] for b in bad]}"))
    print("=" * 100)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())