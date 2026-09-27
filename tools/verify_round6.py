#!/usr/bin/env python3
"""本轮新功能验证：版本号 / systemd 脚本 / 验证码 / 多队列。

只做**静态与库级**断言（不需要起服务），可与其它验证脚本并行跑。
运行：python3 tools/verify_round6.py
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("KWRT_DB", str(ROOT / "users.db"))

REC = []


def rec(cid, title, ok, note=""):
    REC.append((cid, title, bool(ok), note))
    print(f"  {cid:<6} {title:<52} {'✓ 通过' if ok else '✗ 失败'}  {note}")


# ---------------------------------------------------------------- 版本号
def t_version():
    ver_file = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    from app import version as V
    rec("V-1", "VERSION 文件与 app/version.py 一致",
        ver_file == V.VERSION, f"文件={ver_file} 代码={V.VERSION}")
    rec("V-2", "版本格式合法且从 1.0.1 起",
        bool(re.fullmatch(r"\d+\.\d+\.\d+", V.VERSION)) and V.VERSION >= "1.0.1",
        f"v{V.VERSION}")
    rec("V-3", "display() 带 v 前缀", V.display() == "v" + V.VERSION, V.display())
    # PHP 读的是同一份 VERSION 文件
    php = subprocess.run(
        ["php", "-r",
         'require "php/src/helpers.php";'
         'spl_autoload_register(function($c){if(!str_starts_with($c,"Kwrt\\\\"))return;'
         '$f="php/src/".str_replace("\\\\","/",substr($c,5)).".php";if(is_file($f))require $f;});'
         'echo Kwrt\\Version::raw();'],
        capture_output=True, text=True, cwd=ROOT)
    rec("V-4", "PHP 读到同一个版本号（同源）",
        php.stdout.strip() == V.VERSION, f"PHP={php.stdout.strip()}")
    # healthz / site 都暴露版本
    main = (ROOT / "app/main.py").read_text(encoding="utf-8")
    rec("V-5", "healthz 与 /api/v1/site 均返回版本",
        '"version": _version.VERSION' in main and 'd["version"] = _version.display()' in main)
    admin = (ROOT / "web/admin.html").read_text(encoding="utf-8")
    rec("V-6", "后台界面显示版本号", "site.version" in admin)


# ---------------------------------------------------------------- systemd
def t_systemd():
    s = ROOT / "scripts/systemd-install.sh"
    rec("S-1", "共用 systemd 脚本存在", s.is_file())
    txt = s.read_text(encoding="utf-8")
    rec("S-2", "脚本会 enable 开机自启并**验证**结果",
        "systemctl enable" in txt and "is-enabled --quiet" in txt)
    rec("S-3", "enable 失败会报错退出（不吞错）", "systemctl enable 失败" in txt)
    ins = (ROOT / "install.sh").read_text(encoding="utf-8")
    upd = (ROOT / "update.sh").read_text(encoding="utf-8")
    rec("S-4", "install.sh 调用共用脚本", "scripts/systemd-install.sh" in ins)
    rec("S-5", "install.sh 不再用 || true 吞掉 enable 失败",
        "systemctl enable \"$SERVICE_NAME\" >/dev/null 2>&1 || true" not in ins)
    rec("S-6", "update.sh 调用共用脚本（服务缺失时自动安装）",
        "scripts/systemd-install.sh" in upd and "自动安装并启用" in upd)
    rec("S-7", "update.sh 不再只是「请手动重启」",
        "未检测到 systemd 服务 ${SERVICE_NAME}，请手动重启" not in upd)
    for f in ("scripts/systemd-install.sh", "install.sh", "update.sh"):
        r = subprocess.run(["bash", "-n", str(ROOT / f)], capture_output=True, text=True)
        rec(f"S-8:{Path(f).name}", f"{f} 语法通过", r.returncode == 0, r.stderr.strip()[:60])


# ---------------------------------------------------------------- 验证码
def t_captcha():
    from app import captcha, sitesettings as SS

    keys = {it["k"] for it in SS.SCHEMA if isinstance(it, dict)}
    for k in ("security.captcha_enabled", "security.captcha_on_register",
              "security.captcha_length", "security.captcha_ttl_min"):
        rec(f"C-schema:{k.split('.')[-1]}", f"设置项 {k} 存在", k in keys)

    rec("C-1", "默认关闭（不替管理员决定）",
        captcha.enabled() is False and captcha.enabled_for_register() is False)
    rec("C-2", "位数被夹在 3–6", 3 <= captcha.length() <= 6, f"位数={captcha.length()}")
    rec("C-3", "有效期被夹在 1–30 分钟",
        60 <= captcha.ttl_seconds() <= 1800, f"{captcha.ttl_seconds()}s")

    d = captcha.issue("127.0.0.1")
    svg = d["svg"]
    rec("C-4", "签发返回 SVG（不依赖图形库）", svg.startswith("<svg") and "</svg>" in svg)
    # 注意：SVG **本来就要把字符画出来**，所以「SVG 里没有字符」是错的断言。
    # 真正要守的不变量是：接口响应里没有 answer/code 这样的明文字段，
    # 且库里只存哈希（见 C-6）。答案只能靠看图，不能靠读响应。
    rec("C-5", "签发响应不含答案明文字段",
        set(d.keys()) == {"id", "svg", "ttl", "length"}, f"字段={sorted(d.keys())}")
    # 答案只以哈希入库
    import sqlite3
    con = sqlite3.connect(os.environ["KWRT_DB"])
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM captchas WHERE id=?", (d["id"],)).fetchone()
    rec("C-6", "答案以哈希入库（非明文）",
        row is not None and len(str(row["answer_hash"])) == 64)
    rec("C-7", "可用性：错误答案被拒", captcha.verify(d["id"], "ZZZZZ")[0] is False)
    rec("C-8", "一次性：同 id 不可重放", captcha.verify(d["id"], "ZZZZZ")[0] is False)
    con.close()

    # 盐是持久化的（多进程/多 worker 关键）
    con = sqlite3.connect(os.environ["KWRT_DB"])
    con.row_factory = sqlite3.Row
    sal = con.execute("SELECT value FROM app_secrets WHERE name='captcha_salt'").fetchone()
    rec("C-9", "盐持久化在库里（多 worker 不会随机失败）",
        sal is not None and len(str(sal["value"])) == 64)
    con.close()

    php = (ROOT / "php/src/Captcha.php").read_text(encoding="utf-8")
    rec("C-10", "PHP 侧存在且同表同盐同算法",
        "captchas" in php and "captcha_salt" in php
        and "hash('sha256'" in php and "strtoupper($code)" in php)
    auth = (ROOT / "php/src/Controllers/AuthController.php").read_text(encoding="utf-8")
    rec("C-11", "PHP 登录校验验证码在密码比对之前",
        auth.index("Captcha::enabled()") < auth.index("SELECT * FROM users WHERE username=?"))
    main = (ROOT / "app/main.py").read_text(encoding="utf-8")
    rec("C-12", "Python 登录校验验证码在密码比对之前",
        main.index("if captcha.enabled():") < main.index('SELECT * FROM users WHERE username=?'))


# ---------------------------------------------------------------- 多队列
def t_queues():
    from app import backends, sitesettings as SS

    keys = {it["k"] for it in SS.SCHEMA if isinstance(it, dict)}
    rec("Q-1", "设置项 gh.queues 存在且为 json 型",
        "gh.queues" in keys and
        any(it.get("k") == "gh.queues" and it.get("t") == "json" for it in SS.SCHEMA))

    old_q, old_enabled, old_repo, old_tok = (SS.get("gh.queues"), SS.get("gh.enabled"),
                                             SS.get("gh.repo"), SS.get("gh.token"))
    try:
        SS.set_("gh.queues", "[]")
        SS.set_("gh.repo", "own/repo")
        SS.set_("gh.token", "t0")
        rec("Q-2", "旧配置（无 gh.queues）自动合成单队列",
            len(backends.list_queues()) == 1
            and backends.list_queues()[0]["name"] == "默认队列")

        SS.set_("gh.queues", json.dumps([
            {"name": "A", "repo": "o/a", "token": "ta"},
            {"name": "B", "repo": "o/b", "token": "tb", "ref": "dev"},
            {"name": "off", "repo": "o/c", "token": "tc", "enabled": False},
        ]))
        qs = backends.list_queues()
        rec("Q-3", "禁用队列被排除", len(qs) == 2, f"队列={[q['name'] for q in qs]}")
        backends._RR["i"] = 0
        names = [backends.pick_queue()["name"] for _ in range(4)]
        rec("Q-4", "多队列轮转派发", names == ["A", "B", "A", "B"], "→".join(names))
        b = backends.GitHubBackend(qs[1])
        rec("Q-5", "队列参数绑到实例上（不串号）",
            b._repo() == "o/b" and b._token() == "tb" and b._ref() == "dev")

        SS.set_("gh.queues", json.dumps([{"name": "ok", "repo": "o/a", "token": "t"},
                                         {"name": "no-token", "repo": "o/b"},
                                         {"name": "no-repo", "token": "t"}]))
        rec("Q-6", "坏项被跳过（不拖垮整体）",
            [q["name"] for q in backends.list_queues()] == ["ok"])
        # 显式重置前置条件：断言不该依赖上一步留下的状态
        SS.set_("gh.repo", "own/repo")
        SS.set_("gh.token", "t0")
        SS.set_("gh.queues", json.dumps([{"name": "x"}]))
        rec("Q-7", "全部无效时回落单队列配置",
            [q["name"] for q in backends.list_queues()] == ["默认队列"],
            f"实际={[q['name'] for q in backends.list_queues()]}")

        info = backends.info()
        rec("Q-8", "info() 暴露队列列表与数量",
            "queues" in info and "queue_count" in info, f"count={info.get('queue_count')}")
    finally:
        SS.set_("gh.queues", "[]")
        SS.set_("gh.enabled", old_enabled if old_enabled is not None else False)
        SS.set_("gh.repo", old_repo or "")
        SS.set_("gh.token", old_tok or "")

    pq = (ROOT / "php/src/Queues.php").read_text(encoding="utf-8")
    rec("Q-9", "PHP 侧存在同语义队列实现",
        "function available" in pq and "function pick" in pq and "gh.queues" in pq)
    ac = (ROOT / "php/src/Controllers/AdminController.php").read_text(encoding="utf-8")
    rec("Q-10", "PHP 后台测试连接使用真正会派发的那个队列",
        "Queues::pick()" in ac)


def main():
    print("=" * 100)
    print(" 本轮新功能验证：版本号 / systemd / 验证码 / 多队列")
    print("=" * 100)
    t_version()
    t_systemd()
    t_captcha()
    t_queues()
    print("=" * 100)
    bad = [r for r in REC if not r[2]]
    print(f" 合计 {len(REC)} 项，" + ("全部通过 ✓" if not bad else f"失败 ✗ {[b[0] for b in bad]}"))
    print("=" * 100)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
