#!/usr/bin/env python3
"""静态资源版本化回归 —— 守住「部署后必须立即生效」这件事。

## 为什么需要这个套件

现场事故：把「找回密码」入口写进 `login.js`、`login.html` 也部署上去了，
但用户端**一直看不到入口**。排查结论是三层缓存叠加：

  1. 资源 URL 不带版本号（`/assets/js/login.js`）
  2. 应用给它发 `Cache-Control: public, max-age=3600`
  3. 宝塔反代在 `/www/server/nginx/conf/proxy.conf` 里有**全局**
     `proxy_cache cache_one;`（被 nginx.conf 直接 include，对所有 proxy_pass 生效），
     EdgeOne 再叠一层

于是新文件最长一小时发不出去 —— 就是应用自己注释里警告的
「服务端已修、页面仍旧」的幽灵问题。

修法是：**资源 URL 永远带一个会随文件变化的版本号**，并让「是否长缓存」
取决于 URL 是否版本化，而不是取决于有没有配 CDN。

## 断言

  AV-1  版本号随资源文件内容/mtime 变化（Python 侧内容签名）
  AV-2  版本号随资源文件变化（PHP 侧内容签名）
  AV-3  显式配置 site.asset_version 时优先用它（两版）
  AV-4  **未配置 CDN 时** HTML 里的 /assets/ /static/ 引用也带 ?v=（Python）
  AV-5  PHP 渲染的 HTML 里 /assets/ 引用带 ?v=
  AV-6  带 ?v= 的资源 → 长缓存 immutable
  AV-7  不带 ?v= 的资源 → no-cache, must-revalidate（中间层不敢缓存）
  AV-8  HTML → no-cache
  AV-9  负向对照：版本号补写不得碰 Vue 的 :src / :href 绑定表达式

## 用法
    python3 tools/verify_asset_versioning.py
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import re
import shutil
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
PORT_PY = int(os.environ.get("KWRT_AV_PY_PORT", "8231"))
PORT_PHP = int(os.environ.get("KWRT_AV_PHP_PORT", "8232"))
REC: list[tuple[str, str, bool, str]] = []


def rec(cid, title, ok, note=""):
    REC.append((cid, title, bool(ok), note))
    print(f"  {cid:<6} {title:<52} {'✓ 通过' if ok else '✗ 失败'}  {note}")


class Client:
    def __init__(self, base):
        self.base = base.rstrip("/")
        self.op = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def get(self, path, timeout=20):
        # ★ 头名一律小写化：uvicorn 发的是小写 `cache-control`，
        #   PHP 内置 server 发的是 `Cache-Control` —— 不归一化会按实现不同而假失败。
        def norm(h):
            return {k.lower(): v for k, v in dict(h).items()}
        try:
            with self.op.open(self.base + path, timeout=timeout) as r:
                return r.status, r.read().decode("utf-8", "replace"), norm(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace"), norm(e.headers)
        except Exception as e:
            return 0, str(e), {}


def wait_up(base, path="/healthz", timeout=40):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(base + path, timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.25)
    return False


def main() -> int:
    print("=" * 110)
    print(" 静态资源版本化回归（部署必须立即生效）")
    print("=" * 110)

    from app import netcfg

    # ---------------------------------------------------------------- AV-1 / AV-3
    target = ROOT / "web" / "assets" / "js" / "login.js"
    st0 = target.stat()
    v0 = netcfg.asset_signature()
    # 改 mtime（模拟一次部署），并穿透 30 秒签名缓存
    os.utime(target, (st0.st_atime, st0.st_mtime + 7))
    netcfg._ASSET_SIG["t"] = 0.0
    v1 = netcfg.asset_signature()
    os.utime(target, (st0.st_atime, st0.st_mtime))          # 原样还原
    netcfg._ASSET_SIG["t"] = 0.0
    v2 = netcfg.asset_signature()
    rec("AV-1", "版本号随资源文件变化（Python 内容签名）",
        v0 != v1 and v2 == v0,
        f"原 {v0} → 改后 {v1} → 还原 {v2}")

    # 显式配置优先
    from app import sitesettings as SS
    old = SS.get("site.asset_version")
    try:
        SS.set_("site.asset_version", "EXPLICIT9")
        rec("AV-3", "显式配置 site.asset_version 时优先用它（Python）",
            netcfg.asset_version() == "EXPLICIT9", f"取到 {netcfg.asset_version()}")
    finally:
        SS.set_("site.asset_version", old or "")

    # ---------------------------------------------------- 起 Python / PHP 两个真服务
    tmp = Path(tempfile.mkdtemp(prefix="kwrt-av-"))
    py = php = None
    try:
        for d in ("app", "php", "web"):
            shutil.copytree(ROOT / d, tmp / d,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for f in ("config.json", "VERSION"):
            if (ROOT / f).exists():
                shutil.copy2(ROOT / f, tmp / f)

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

        c = Client(py_base)

        # ------------------------------------------------------------ AV-4 HTML 路径式版本化
        st, html, hdrs = c.get("/login/")
        refs = re.findall(r'(?<![:@])(?:href|src)="([^"]*?/(?:assets|static|vendor)/[^"]*)"', html)
        ver_seg = re.findall(r'/(?:assets|static|vendor)/_v/([0-9A-Za-z._-]+)/', html)
        versioned = [r for r in refs if "/_v/" in r]
        bare_refs = [r for r in refs if "/_v/" not in r]
        rec("AV-4", "HTML 里资源引用为**路径式**版本化（未配 CDN 时也生效）",
            st == 200 and bool(versioned) and not bare_refs and len(set(ver_seg)) <= 1,
            f"共 {len(refs)} 个引用，路径带版本 {len(versioned)} 个，裸 {bare_refs[:3]}，版本段={set(ver_seg)}")

        # ------------------------------------------------------------ AV-10 映射正确性
        # 版本化路径必须真的把**同一个文件**发出来（不是 404、不是别的文件）
        same = True
        detail = []
        for r in versioned[:3]:
            sb, bb, _ = c.get(r)
            sbare, bbare, _ = c.get(re.sub(r'/(assets|static|vendor)/_v/[^/]+/', r'/\1/', r))
            ok = sb == 200 and sbare == 200 and bb == bbare and len(bb) > 0
            same = same and ok
            detail.append(f"{r.split('/')[-1]}:{sb}/{sbare},{len(bb)}B,{'同' if bb == bbare else '异'}")
        rec("AV-10", "版本化路径映射到同一文件（字节一致）", same, "; ".join(detail[:3]))

        # ------------------------------------------------------------ AV-11 负向对照：穿越
        # 版本化路径是**新的文件转发入口**，必须证明它不能读出目录之外的东西。
        # 用真实存在的敏感文件做目标（users.db / config.json 都在仓库根）。
        trav = [
            "/assets/_v/x/../../users.db",
            "/assets/_v/x/..%2f..%2fusers.db",
            "/assets/_v/x/../../../etc/passwd",
            "/assets/_v/..%2f..%2fjs/login.js",      # 版本段里带 .. → 白名单应拦掉
        ]
        leaked = []
        for t in trav:
            stt, bt, _ = c.get(t)
            body = bt if isinstance(bt, str) else bt.decode("utf-8", "replace")
            if stt == 200 and ("SQLite format" in body or "root:" in body
                               or '"site_name"' in body):
                leaked.append(t)
        rec("AV-11", "负向对照：版本化路径不能目录穿越", not leaked,
            f"试了 {len(trav)} 种，泄漏 {leaked or '无'}")

        # ------------------------------------------------------------ AV-9 负向对照
        # Vue 绑定表达式里的 '/static/logo.svg' 不该被文本替换动到
        # （它由 site.logo_url 运行期求值，塞 ?v= 进去会改变语义）
        vue_refs = re.findall(r'[:@]\w+="[^"]*?/(?:assets|static)/[^"]*"', html)
        bad_vue = [r for r in vue_refs if "?v=" in r]
        rec("AV-9", "负向对照：版本号补写不碰 Vue 的 :src / :href 绑定",
            not bad_vue,
            f"Vue 绑定 {len(vue_refs)} 处，被误改 {len(bad_vue)} 处")

        # ------------------------------------------------------------ AV-6 / AV-7 / AV-8
        if versioned:
            u = versioned[0]
            s1, _b1, h1 = c.get(u)
            cc1 = h1.get("cache-control", "")
        else:
            s1, cc1 = 0, ""
        rec("AV-6", "路径带版本号的资源 → 长缓存 immutable",
            s1 == 200 and "immutable" in cc1 and "max-age=31536000" in cc1,
            f"HTTP {s1}, Cache-Control={cc1!r}")

        s2, _b2, h2 = c.get("/assets/js/login.js")
        cc2 = h2.get("cache-control", "")
        rec("AV-7", "不带 ?v= 的资源 → no-cache（中间层不敢缓存）",
            s2 == 200 and "no-cache" in cc2,
            f"HTTP {s2}, Cache-Control={cc2!r}")

        s3, _b3, h3 = c.get("/login/")
        cc3 = h3.get("cache-control", "")
        rec("AV-8", "HTML → no-cache",
            s3 == 200 and "no-cache" in cc3, f"Cache-Control={cc3!r}")

        # ------------------------------------------------------------ AV-5 PHP 路径式版本化
        cp = Client(php_base)
        stp, htmlp, _hp = cp.get("/login/")
        refsp = re.findall(r'(?:href|src)="([^"]*?/(?:assets|static|vendor)/[^"]*)"', htmlp)
        verp = [r for r in refsp if "/_v/" in r]
        barep = [r for r in refsp if "/_v/" not in r]
        rec("AV-5", "PHP 渲染的 HTML 里资源引用为路径式版本化",
            stp == 200 and bool(verp) and not barep,
            f"共 {len(refsp)} 个，路径带版本 {len(verp)} 个，裸 {barep[:3]}")

        # PHP 侧版本化路径也要真的能取到文件（不是 404）
        # ★ 注意：HTML 里的引用是**绝对 URL**（Net::assetBase() 给了完整基址），
        #   而 Client.get 会再拼一次 base —— 必须先剥掉，否则拼成
        #   http://hosthttp://host/... 抛异常，状态码 0，看着像「服务端坏了」。
        #   （踩过：一开始就是这么误判的。）
        bad_php = []
        for r in verp[:3]:
            rel = r[len(php_base):] if r.startswith(php_base) else r
            if not rel.startswith("/"):
                rel = "/" + rel
            s_, b_, _h_ = cp.get(rel)
            if s_ != 200 or len(b_) == 0:
                bad_php.append((rel, s_))
        rec("AV-12", "PHP 版本化路径可正常取到文件", not bad_php, f"异常 {bad_php or '无'}")

        # PHP 侧穿越负向对照
        leaked_php = []
        for t in ("/assets/_v/x/../../users.db", "/assets/_v/x/../../config.json"):
            s_, b_, _h_ = cp.get(t)
            if s_ == 200 and ("SQLite format" in b_ or '"site_name"' in b_):
                leaked_php.append(t)
        rec("AV-13", "负向对照：PHP 版本化路径不能目录穿越", not leaked_php,
            f"泄漏 {leaked_php or '无'}")

        # PHP 侧内容签名：直接调 PHP 函数对比两次结果
        php_code = (
            'require "php/src/helpers.php";'
            'spl_autoload_register(function($c){if(!str_starts_with($c,"Kwrt\\\\"))return;'
            '$f="php/src/".str_replace("\\\\","/",substr($c,5)).".php";if(is_file($f))require $f;});'
            'echo Kwrt\\Net::assetSignature();'
        )
        r1 = subprocess.run(["php", "-r", php_code], cwd=tmp, capture_output=True, text=True)
        sig1 = r1.stdout.strip()
        tgt = tmp / "web" / "assets" / "js" / "login.js"
        stt = tgt.stat()
        os.utime(tgt, (stt.st_atime, stt.st_mtime + 7))
        r2 = subprocess.run(["php", "-r", php_code], cwd=tmp, capture_output=True, text=True)
        sig2 = r2.stdout.strip()
        os.utime(tgt, (stt.st_atime, stt.st_mtime))
        rec("AV-2", "版本号随资源文件变化（PHP 内容签名）",
            bool(sig1) and len(sig1) == 10 and sig1 != sig2,
            f"{sig1} → {sig2}（err={r1.stderr[:60]!r}）")

    finally:
        for p in (py, php):
            if p:
                try:
                    os.killpg(os.getpgid(p.pid), 15)
                except Exception:
                    try:
                        p.terminate()
                    except Exception:
                        pass
        shutil.rmtree(tmp, ignore_errors=True)

    print("=" * 110)
    bad = [r for r in REC if not r[2]]
    print(f" 合计 {len(REC)} 项，" + ("全部通过 ✓" if not bad else f"失败 ✗ {[b[0] for b in bad]}"))
    print("=" * 110)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
