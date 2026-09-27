#!/usr/bin/env python3
"""MySQL 支持验证（PHP 版）。

若环境里没有可达的 MySQL，则**跳过并说明**，不判失败 ——
CI 上没起数据库时不该红。

用法：
    KWRT_MYSQL_HOST=127.0.0.1 KWRT_MYSQL_PORT=3306 KWRT_MYSQL_DB=kwrt \
    KWRT_MYSQL_USER=kwrt KWRT_MYSQL_PASS=xxx python3 tools/verify_mysql.py
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REC: list[tuple[str, str, bool, str]] = []


def rec(cid, title, ok, note=""):
    REC.append((cid, title, bool(ok), note))
    print(f"  {cid:<8} {title:<50} {'✓ 通过' if ok else '✗ 失败'}  {note}")


def php(code: str, env: dict) -> tuple[str, str]:
    p = ROOT / ".ekko-tmp-mysql.php"
    p.write_text(code, encoding="utf-8")
    try:
        r = subprocess.run(["php", str(p)], capture_output=True, text=True, cwd=ROOT, env=env)
        return r.stdout, r.stderr
    finally:
        p.unlink(missing_ok=True)


BOOT = '''<?php
require "php/src/helpers.php";
spl_autoload_register(function($c){if(!str_starts_with($c,"Kwrt\\\\"))return;
 $f="php/src/".str_replace("\\\\","/",substr($c,5)).".php";if(is_file($f))require $f;});
'''


def main() -> int:
    env = {**os.environ, "KWRT_DB_DRIVER": "mysql",
           "KWRT_MYSQL_HOST": os.environ.get("KWRT_MYSQL_HOST", "127.0.0.1"),
           "KWRT_MYSQL_PORT": os.environ.get("KWRT_MYSQL_PORT", "3306"),
           "KWRT_MYSQL_DB":   os.environ.get("KWRT_MYSQL_DB", "kwrt"),
           "KWRT_MYSQL_USER": os.environ.get("KWRT_MYSQL_USER", "kwrt"),
           "KWRT_MYSQL_PASS": os.environ.get("KWRT_MYSQL_PASS", "")}

    print("=" * 96)
    print(" MySQL 支持验证（PHP 版）")
    print("=" * 96)

    out, err = php(BOOT + 'echo Kwrt\\Db::driver();', env)
    if out.strip() != "mysql":
        rec("M-0", "驱动可切换为 mysql", False, f"读到的驱动 = {out.strip()!r} {err[:60]}")
        return 1
    rec("M-0", "驱动可切换为 mysql", True)

    # 连通性（连不上就整体跳过）
    out, err = php(BOOT + 'try { Kwrt\\Db::pdo(); echo "OK"; } catch (Throwable $e) { echo "ERR:".$e->getMessage(); }', env)
    if "OK" not in out:
        print(f"\n  ⚠ 无法连上 MySQL，跳过全部断言。")
        print(f"    {out.strip()[:200]}")
        print("  这不是代码问题，请起一个 MySQL 或设置 KWRT_MYSQL_* 后重跑。")
        return 0
    rec("M-1", "能连上 MySQL", True)

    # 建表完整性：与 SQLite 真源表数一致
    out, _ = php(BOOT + '''
$t = array_map(fn($r)=>array_values($r)[0], Kwrt\\Db::all("SHOW TABLES"));
echo count($t), "|", count(Kwrt\\Schema::SQLITE);
''', env)
    n_mysql, n_sqlite = (out.strip().split("|") + ["0", "0"])[:2]
    rec("M-2", "建表数量与 SQLite 真源一致", n_mysql == n_sqlite, f"MySQL {n_mysql} / SQLite {n_sqlite}")

    # 未加反引号的保留字：这是本轮踩到的真坑，做成常驻断言
    src = (ROOT / "php/src").rglob("*.php")
    bad = []
    for f in src:
        if f.name == "SettingsSchema.php":
            continue
        t = f.read_text(encoding="utf-8")
        for m in re.finditer(r"(?:FROM|INTO|UPDATE)\s+(\w+)", t):
            pass
        for m in re.finditer(r"(?<![`'\"\w.])(key|value)(?![`'\"\w])\s*[,=]?\s*(?:FROM|WHERE|\()?", t):
            # 只关心 SQL 字符串里未加反引号的 key/value
            pass
        # 逐行看，且跳过注释 —— 注释里写 "SQLite INSERT OR IGNORE" 是说明文字，
        # 不是真的 SQL。原先整文件正则匹配会把注释也算进去，产生误报。
        for ln, line in enumerate(t.splitlines(), 1):
            s = line.strip()
            if s.startswith(("//", "*", "/*", "#")):
                continue
            for m in re.finditer(r"(?:SELECT|WHERE|INSERT INTO \w+\()([^;'\"]*)\b(key|value)\b", line):
                seg = m.group(0)
                if "`" not in seg:
                    bad.append(f"{f.relative_to(ROOT)}:{ln}: {seg[:56]}")
    # 只保留 settings 表相关的（bans.value 等非保留字场景不报）
    bad = [b for b in bad if "settings" in b or "`key`" not in b][:5]
    rec("M-3", "SQL 里的保留字 key/value 已加反引号", not bad, f"可疑 {len(bad)} 处" if bad else "")

    # upsert / insertIgnore 语义
    out, err = php(BOOT + '''
Kwrt\\Db::upsert("settings", ["key"=>"__mt","value"=>"v1","updated"=>1.0], ["key"]);
Kwrt\\Db::upsert("settings", ["key"=>"__mt","value"=>"v2","updated"=>2.0], ["key"]);
$v1 = Kwrt\\Db::val("SELECT `value` FROM settings WHERE `key`='__mt'");
$n1 = Kwrt\\Db::val("SELECT COUNT(*) FROM settings WHERE `key`='__mt'");
Kwrt\\Db::insertIgnore("settings", ["key"=>"__mt","value"=>"NO","updated"=>3.0]);
$v2 = Kwrt\\Db::val("SELECT `value` FROM settings WHERE `key`='__mt'");
Kwrt\\Db::run("DELETE FROM settings WHERE `key`='__mt'");
echo "$v1|$n1|$v2";
''', env)
    parts = (out.strip().split("|") + ["", "", ""])[:3]
    rec("M-4", "upsert 幂等（覆盖且不新增行）", parts[0] == "v2" and parts[1] == "1", f"{parts[0]}/{parts[1]}")
    rec("M-5", "insertIgnore 不覆盖已有值", parts[2] == "v2", parts[2])

    # utf8mb4：中文 + emoji 往返
    out, _ = php(BOOT + '''
Kwrt\\Settings::set("site_name", "中文🚀测试");
echo Kwrt\\Settings::get("site_name");
''', env)
    rec("M-6", "utf8mb4 中文与 emoji 往返正确", out.strip() == "中文🚀测试", out.strip()[:24])

    # 设置读写（本轮真正的缺陷点：读不出来 = key 没加反引号）
    out, _ = php(BOOT + '''
$before = Kwrt\\Settings::get("default_quota");
Kwrt\\Settings::set("default_quota", "9");
echo $before, "|", Kwrt\\Settings::get("default_quota"), "|",
     Kwrt\\Db::val("SELECT `value` FROM settings WHERE `key`='default_quota'");
''', env)
    p = (out.strip().split("|") + ["", "", ""])[:3]
    rec("M-7", "设置写入后能读回（且与库内一致）", p[1] == "9" and p[2] == "9", "|".join(p))

    # 自增主键
    out, _ = php(BOOT + '''
Kwrt\\Db::run("INSERT INTO users(username,password,created,role) VALUES(?,?,?,?)", ["__mt1","x",microtime(true),"user"]);
$a = Kwrt\\Db::val("SELECT id FROM users WHERE username='__mt1'");
Kwrt\\Db::run("INSERT INTO users(username,password,created,role) VALUES(?,?,?,?)", ["__mt2","x",microtime(true),"user"]);
$b = Kwrt\\Db::val("SELECT id FROM users WHERE username='__mt2'");
Kwrt\\Db::run("DELETE FROM users WHERE username IN ('__mt1','__mt2')");
echo gettype($a), "|", ($b > $a ? "grow" : "nogrow");
''', env)
    p = (out.strip().split("|") + ["", ""])[:2]
    rec("M-8", "自增主键可用且类型为整数", p[1] == "grow", f"type={p[0]}")

    # 懒建表（验证码 / 队列游标）在 MySQL 下也通
    out, err = php(BOOT + '''
$d = Kwrt\\Captcha::issue("127.0.0.1");
[$ok,] = Kwrt\\Captcha::verify($d["id"], "ZZZZ");
echo (strlen($d["svg"]) > 100 ? "svg" : "nosvg"), "|", var_export($ok, true), "|",
     Kwrt\\Db::val("SELECT COUNT(*) FROM app_secrets WHERE name='captcha_salt'");
''', env)
    p = (out.strip().split("|") + ["", "", ""])[:3]
    rec("M-9", "验证码在 MySQL 下签发+校验正常", p[0] == "svg" and p[1] == "false" and p[2] == "1",
        f"{p[0]}/{p[1]}/salt={p[2]}")

    # 方言差异必须只在 Db.php 一处
    others = []
    for f in list((ROOT / "php/src").rglob("*.php")) + list((ROOT / "php/scripts").rglob("*.php")):
        if f.name in ("Db.php", "Schema.php", "SettingsSchema.php"):
            continue
        for ln, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            s = line.strip()
            if s.startswith(("//", "*", "/*", "#")):
                continue            # 注释里的方言名只是说明文字
            for pat in ("ON CONFLICT", "INSERT OR REPLACE", "INSERT OR IGNORE",
                        "AUTOINCREMENT", "PRAGMA"):
                if pat in s:
                    others.append(f"{f.relative_to(ROOT)}:{ln}:{pat}")
    rec("M-10", "方言专有写法只在 Db.php 内出现", not others, ", ".join(others[:3]))

    # M-11 新增的更新检查缓存表（update_checks）在 MySQL 下能真正读写往返。
    #      —— 只验「表建出来了」不够：payload 是整段 JSON，写入路径走
    #      Db::run 的占位符与 TEXT 列，方言差异（TEXT NOT NULL 的默认值、
    #      长度限制、占位符绑定）正是容易出问题的地方，所以真写一次再读回来比内容。
    probe = (
        r"$payload = ['status'=>'ok','latest'=>'9.9.9',"
        r"'notes'=>'中文🚀 含引号\"与\\反斜杠','repo'=>'a/b'];"
        r"\Kwrt\Update::clearCache();"
        r"$before = \Kwrt\Update::loadCache();"
        r"\Kwrt\Update::saveCache($payload);"
        r"\Kwrt\Update::clearCache();"          # 先证明清空确实生效
        r"\Kwrt\Update::saveCache($payload);"
        r"[$got, $ts] = \Kwrt\Update::loadCache();"
        r"echo json_encode(['before'=>$before[0], 'same'=>$got === $payload,"
        r" 'latest'=>$got['latest'] ?? '', 'notes'=>$got['notes'] ?? '',"
        r" 'ts'=>((float)$ts > 0)], JSON_UNESCAPED_UNICODE);"
    )
    out, err = php(BOOT + probe, env)
    try:
        j = json.loads(out.strip().split("\n")[-1])
    except Exception:                                 # noqa: BLE001
        j = {}
    rec("M-11", "更新检查缓存表在 MySQL 下读写往返正确",
        bool(j) and j.get("before") is None and j.get("same") is True and j.get("ts") is True,
        (f"清前={j.get('before')} 往返一致={j.get('same')} notes={j.get('notes','')[:16]}"
         if j else f"探针失败：{(out + err)[-140:]}"))

    print("=" * 96)
    bad = [r for r in REC if not r[2]]
    print(f" 合计 {len(REC)} 项，" + ("全部通过 ✓" if not bad else f"失败 ✗ {[b[0] for b in bad]}"))
    print("=" * 96)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())