#!/usr/bin/env python3
"""远端构建后端 / 外链下载 / 自定义邮件模板 的回归。

对应 1.0.3 补齐的三块 PHP 功能（原先只有 Python 版有）：
  ① 远端 GitHub Actions 派发（php/src/Github.php）
  ② 外链下载代理（download.serve_local / download.external_host / email_on_ready）
  ③ 自定义邮件模板（mail.subject_tpl / body_tpl / verify_subject / verify_body /
     notify_fail / verify_ttl_hours）

判据分两层：
  · 离线层（G-1..G-9）—— 纯函数与渲染，不需要网络，每次都跑；
  · 在线层（G-L*）—— **真实**调 GitHub API。默认跑只读的「测试连接」；
    真实派发要显式加 `--dispatch`，且**用后立即删除该 run 并还原设置**。
    为什么坚持真跑：派发路径的坑（return_run_details 必须放 body、302 第二跳
    不能带 Authorization）都只在真网络里才暴露，静态检查验不出来。

用法：
    python3 tools/verify_remote_backend.py             # 离线 + 只读在线
    python3 tools/verify_remote_backend.py --dispatch  # 额外做真实派发（会创建并删除 run）
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REC: list[tuple[str, str, bool, str]] = []


def rec(cid: str, title: str, ok: bool, note: str = "") -> None:
    REC.append((cid, title, bool(ok), note))
    print(f"  {cid:<7} {title:<56} {'✓ 通过' if ok else '✗ 失败'}  {note}")


BOOT = '''<?php
require "php/src/helpers.php";
spl_autoload_register(function ($c) { if (!str_starts_with($c, "Kwrt\\\\")) return;
    $f = "php/src/" . str_replace("\\\\", "/", substr($c, 5)) . ".php"; if (is_file($f)) require $f; });
'''


def run_php(dst: Path, body: str, timeout: int = 300) -> str:
    f = dst / ".vrb.php"
    f.write_text(BOOT + body, encoding="utf-8")
    try:
        r = subprocess.run(["php", str(f)], capture_output=True, text=True, cwd=dst, timeout=timeout)
        return (r.stdout + (("\n[stderr] " + r.stderr.strip()) if r.stderr.strip() else "")).strip()
    finally:
        f.unlink(missing_ok=True)


def copy_repo(dst: Path) -> None:
    shutil.copytree(ROOT, dst, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(".git", "users.db", "users.db-*", "store",
                                                  "work", "cache", "__pycache__",
                                                  "config.local.json", "install.lock"))


def main() -> int:
    do_dispatch = "--dispatch" in sys.argv
    print("=" * 108)
    print(" 远端后端 / 外链下载 / 自定义邮件模板 回归")
    print("=" * 108)

    tmp = Path(tempfile.mkdtemp(prefix="kwrt-rb-"))
    dst = tmp / "app"
    try:
        copy_repo(dst)

        # ---------------------------------------------------------- ① 重定向白名单
        out = run_php(dst, '''
$cases = [
  "https://objects.githubusercontent.com/x?a=1"     => true,
  "https://foo.blob.core.windows.net/c/a.zip?sig=1" => true,
  "https://api.github.com/repos/a/b"                => true,
  "file:///etc/passwd"                              => false,
  "http://objects.githubusercontent.com/x"          => false,
  "https://evil.example.com/a.zip"                  => false,
  "https://github.com.evil.com/a"                   => false,
  "https://127.0.0.1/a"                             => false,
  ""                                                => false,
];
$bad = [];
foreach ($cases as $loc => $want) {
    [$ok, $why] = Kwrt\\Github::validateRedirect($loc);
    if ($ok !== $want) { $bad[] = $loc . "=>" . var_export($ok, true) . "(" . $why . ")"; }
}
echo $bad ? ("FAIL " . implode(" | ", $bad)) : "OK 8/8";
''')
        rec("G-1", "302 重定向白名单（放行 GitHub/Azure，拒绝 file:// 与内网）",
            out.strip() == "OK 8/8", out.strip()[:64])

        # ---------------------------------------------------------- ② zip 完整性
        z_ok = dst / "ok.zip"
        with zipfile.ZipFile(z_ok, "w") as z:
            z.writestr("a.txt", "hello" * 100)
        z_bad = dst / "bad.zip"
        z_bad.write_bytes(z_ok.read_bytes()[:60])          # 截断
        out = run_php(dst, '''
$ok  = Kwrt\\Github::zipComplete("ok.zip");
$bad = Kwrt\\Github::zipComplete("bad.zip");
echo ($ok === true && $bad === false) ? "OK" : ("FAIL ok=" . var_export($ok, true) . " bad=" . var_export($bad, true));
''')
        rec("G-2", "zip 完整性探测（Truncated 包不被当成成功产物）",
            out.strip() == "OK", out.strip()[:64])

        # ---------------------------------------------------------- ③ 产物名匹配
        out = run_php(dst, '''
Kwrt\\Settings::set("gh.artifact_pattern", "openwrt-*");
$a = Kwrt\\Github::artifactMatch("openwrt-x86-64-generic");
$b = Kwrt\\Github::artifactMatch("other-thing");
// 空值时**两版都回落到 openwrt-***（Python: `SS.get(...) or "openwrt-*"`），
// 所以空串下 "openwrt-foo" 仍匹配、"other" 仍不匹配 —— 与 Python 一致。
Kwrt\\Settings::set("gh.artifact_pattern", "");
$c = Kwrt\\Github::artifactMatch("openwrt-foo");
$d = Kwrt\\Github::artifactMatch("other");
Kwrt\\Settings::set("gh.artifact_pattern", "openwrt-*");
echo ($a === true && $b === false && $c === true && $d === false) ? "OK"
   : ("FAIL a=" . var_export($a, true) . " b=" . var_export($b, true)
      . " c=" . var_export($c, true) . " d=" . var_export($d, true));
''')
        rec("G-3", "gh.artifact_pattern 的 glob 过滤（空模式不过滤）",
            out.strip() == "OK", out.strip()[:64])

        # ---------------------------------------------------------- ④ 安全解压
        evil = dst / "evil.zip"
        with zipfile.ZipFile(evil, "w") as z:
            z.writestr("good.txt", "fine")
            z.writestr("../../escaped.txt", "pwned")       # 路径穿越
            z.writestr("/abs/abs.txt", "pwned")            # 绝对路径
        (dst / "unz").mkdir(exist_ok=True)
        out = run_php(dst, '''
try { $n = Kwrt\\Github::safeUnzip("evil.zip", "unz"); }
catch (Throwable $e) { echo "EXC " . $e->getMessage(); exit; }
$esc = is_file("escaped.txt") || is_file("../escaped.txt") || is_file("../../escaped.txt");
$abs = is_file("/abs/abs.txt");
$good = is_file("unz/good.txt");
echo ($good && !$esc && !$abs) ? "OK n=$n" : ("FAIL good=" . var_export($good, true)
   . " esc=" . var_export($esc, true) . " abs=" . var_export($abs, true));
''')
        rec("G-4", "安全解压：拒路径穿越与绝对路径，正常文件照收",
            out.startswith("OK "), out.strip()[:64])

        # ---------------------------------------------------------- ⑤ 外链基址
        out = run_php(dst, '''
Kwrt\\Settings::set("download.external_host", "https://cdn.example.com/");
$b1 = Kwrt\\Notifier::downloadBase();
Kwrt\\Settings::set("download.external_host", "");
$b2 = Kwrt\\Notifier::downloadBase();
Kwrt\\Settings::set("download.external_host", "");
echo ($b1 === "https://cdn.example.com" ? "OK1" : "F1:" . $b1) . "|" . ($b2 !== "" ? "OK2" : "F2");
''')
        rec("G-5", "download.external_host 决定下载链接基址（去掉尾部斜杠）",
            out.strip() == "OK1|OK2", out.strip()[:64])

        # ---------------------------------------------------------- ⑥ 自定义邮件模板
        out = run_php(dst, '''
Kwrt\\Settings::set("mail.verify_subject", "[{site}] 验证 {username}");
Kwrt\\Settings::set("mail.verify_body", "你好 {username}，点击 {link}（{hours}h）。未知{unknownvar}保留");
$s = Kwrt\\Mailer::tpl("mail.verify_subject", "DEF", ["site" => "Kwrt", "username" => "alice"]);
$b = Kwrt\\Mailer::tpl("mail.verify_body", "DEF",
     ["username" => "alice", "link" => "https://x/y", "hours" => "24"]);
echo $s . "||" . $b;
''')
        ok6 = ("[Kwrt] 验证 alice" in out) and ("点击 https://x/y（24h）" in out) and ("{unknownvar}" in out)
        rec("G-6", "mail.verify_subject/body 自定义且 {var} 替换、未知变量保留",
            ok6, ("未知变量已保留" if "{unknownvar}" in out else "未知变量被吞"))

        # ---------------------------------------------------------- ⑦ 构建完成邮件走自定义模板
        out = run_php(dst, '''
Kwrt\\Settings::set("mail.subject_tpl", "构建好了：{target} 共{count}个");
Kwrt\\Settings::set("mail.body_tpl", "给{username}：{target}/{profile} {count}个 {duration} 链接：{links} 有效期{hours}h");
$s = Kwrt\\Mailer::tpl("mail.subject_tpl", "D", ["target" => "x86/64", "count" => "3"]);
$b = Kwrt\\Mailer::tpl("mail.body_tpl", "D",
     ["username" => "u", "target" => "x86/64", "profile" => "generic", "count" => "3",
      "duration" => "12 秒", "links" => "L1", "hours" => "72"]);
echo $s . "||" . $b;
''')
        ok7 = "构建好了：x86/64 共3个" in out and "有效期72h" in out
        rec("G-7", "mail.subject_tpl/body_tpl 生效（构建完成通知）", ok7, out.strip()[:70])

        # ---------------------------------------------------------- ⑧ notify_fail 开关
        out = run_php(dst, '''
Kwrt\\Settings::set("mail.notify_fail", "0");
Kwrt\\Settings::set("mail.enabled", "1");
[$ok, $why] = Kwrt\\Mailer::sendBuildFail("a@b.c", "u", "boom");
echo "off=" . var_export($ok, true) . ":" . $why;
Kwrt\\Settings::set("mail.notify_fail", "1");
''')
        rec("G-8", "mail.notify_fail=false 时不发失败邮件（原先无论如何都发）",
            "off=false" in out and "关闭" in out, out.strip()[:64])

        # ---------------------------------------------------------- ⑨ 验证链接有效期
        out = run_php(dst, '''
Kwrt\\Settings::set("mail.verify_ttl_hours", "72");
$h = Kwrt\\Mailer::verifyTtlHours();
Kwrt\\Settings::set("mail.verify_ttl_hours", "24");
echo "h=" . $h;
''')
        rec("G-9", "mail.verify_ttl_hours 决定验证链接有效期（原先写死 24h）",
            out.strip() == "h=72", out.strip()[:48])

        # ---------------------------------------------------------- 在线层
        r = subprocess.run([sys.executable, "-c",
            "import sqlite3,sys;c=sqlite3.connect(sys.argv[1]);"
            "print(c.execute(\"SELECT value FROM settings WHERE key='gh.enabled'\").fetchone()[0])",
            str(ROOT / "users.db")], capture_output=True, text=True)
        gh_on = r.stdout.strip() == "1"
        out = run_php(dst, '''
Kwrt\\Settings::set("gh.enabled", "0");
$off = Kwrt\\Github::available();
Kwrt\\Settings::set("gh.enabled", "1");
Kwrt\\Settings::set("gh.repo", "");
$norepo = Kwrt\\Github::available();
echo "off=" . var_export($off, true) . " norepo=" . var_export($norepo, true);
''')
        rec("G-L1", "gh.enabled 总开关被尊重（关掉即便有 token 也不可用）",
            "off=false" in out and "norepo=false" in out, out.strip()[:64])

        if do_dispatch:
            # 真实派发：用真 token 调 GitHub，创建 run 后立刻删除。
            out = subprocess.run(
                [sys.executable, str(ROOT / "tools" / "_dispatch_probe.py")],
                capture_output=True, text=True, cwd=ROOT)
            txt = (out.stdout + out.stderr).strip()
            rec("G-L2", "真实 GitHub 派发（return_run_details 拿到精确 run_id）",
                "DISPATCH_OK" in txt, txt[-80:].replace("\n", " "))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        # 还原被测试改过的设置
        subprocess.run([sys.executable, str(ROOT / "tools" / "_restore_settings.py")],
                       capture_output=True, text=True, cwd=ROOT)

    print("=" * 108)
    bad = [r for r in REC if not r[2]]
    print(f" 合计 {len(REC)} 项，" + ("全部通过 ✓" if not bad else f"失败 ✗ {[b[0] for b in bad]}"))
    print("=" * 108)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())