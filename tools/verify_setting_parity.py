#!/usr/bin/env python3
"""设置项「接线一致性」检查 —— 防止后台能改、某一版却根本不读的**静默失效**。

## 为什么要检查这个
后台暴露 119 个设置项。两版各自实现，很容易出现「声明了但没接线」：
管理员改了设置、界面也提示保存成功，功能却毫无变化 —— 这种**静默失效**
比报错更糟，因为没人会发现。

本轮就是靠这个思路查出来的真缺口：
  · `download.max_hits` —— PHP 侧调用方不传 $maxHits，签名默认 0 = **不限次数**，
    管理员设的「单链接最多下载 N 次」被静默忽略（安全相关）。已修。
  · `pay.alipay_gateway` —— PHP 直接落到硬编码常量，自定义网关地址改了没用。已修。
  · `builder.backend=github` —— PHP 原先只实现本地构建，设了会**静默本地构建**，
    而两版共用一份设置 → 同一设置两种行为。现已补齐远端派发（php/src/Github.php）。

原先登记的 12 项差额（mail.* 自定义模板、download.* 外链、gh.* 远端派发参数）
已在 1.0.3 全部接线，故 ACK 清单清空 —— 这是 S-4 的用处：修好了必须划掉，
否则清单会烂掉、变成「以为没人管」的借口。

## 判据（避免误报，这点踩过）
Python 侧有一批键是**整体下发**给前端的：`app/sitesettings.py::public_values()`
返回的 38 个键，前端拿到后按需使用，因此这些键在别的 .py 文件里不会字面出现。
所以 Python 侧「被使用」= 在 app/*.py 出现（排除 sitesettings.py）**或** 属于
public_values()。漏掉后半条会把 20 个前台文案键误判成缺口（实测踩过）。

## 剩余已确认缺口（ACK）
下列键 Python 生效、PHP 未接线，属**已知且接受**的差额（PHP 版不做远端派发、
不做外链下载代理、不做自定义邮件模板）。它们被显式登记而不是被忽略：
任何**新增**分歧会让本检查失败；ACK 里的项若已被修好也会失败（防止清单烂掉）。

用法：python3 tools/verify_setting_parity.py
"""
from __future__ import annotations

import os
import re
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: PHP 未接线但**已知且接受**的键 → 理由。新增分歧不在表内即判失败。
ACK_PHP_MISSING: dict[str, str] = {
    # 历史遗留键：币种的真源是 sponsor.currency（下拉）。pay.currency 是早期
    # 的自由文本键，两版都已不读；保留在 schema 里只为兼容旧配置，属已废弃。
    "pay.currency": "★已废弃：币种真源是 sponsor.currency；保留仅为兼容旧配置",
}

#: Python 未接线但**已知且接受**的键 → 理由。
ACK_PY_MISSING: dict[str, str] = {
    "entry.qrcode_api": "PHP 版可选加载第三方二维码接口；Python 版未接入，属已知差额",
    "pay.result_note":  "PHP 版支付结果页有自定义说明文案；Python 版未接入，属已知差额",
    "pay.currency":     "★已废弃：币种真源是 sponsor.currency；保留仅为兼容旧配置",
}


def strip_comments(src: str, lang: str) -> str:
    """剥掉注释再扫描。

    ★ 必须做这一步：代码里常常用**注释**提到某个键名来解释它（例如
    「币种键是 sponsor.currency，不是 pay.currency」），若不剥注释，
    检查器会把这类提及当成「已接线」，于是真正的未接线键被掩盖。
    实测踩过：pay.currency 只在注释里出现，却让 ACK 条目被判为「已失效」。
    """
    if lang == "php":
        src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
        src = re.sub(r"(?m)//.*$", "", src)
        src = re.sub(r"(?m)^\s*\*.*$", "", src)
    else:
        src = re.sub(r"(?m)#.*$", "", src)
        src = re.sub(r'(?s)"""[^"]*?"""', "", src)
        src = re.sub(r"(?s)\'\'\'[^\']*?\'\'\'", "", src)
    return src


def schema_keys() -> set[str]:
    src = (ROOT / "php/src/SettingsSchema.php").read_text(encoding="utf-8")
    return set(re.findall(r"\['k'\s*=>\s*'([^']+)'", src))


def public_keys() -> set[str]:
    """前端整体下发的那批键（必须真跑，不能靠猜）。"""
    code = ("import sys; sys.path.insert(0,'.');"
            "from app import sitesettings as SS;"
            "print('\\n'.join(sorted(SS.public_values())))")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       cwd=ROOT, env={**os.environ, "KWRT_DB": str(ROOT / "users.db")})
    if r.returncode != 0:
        raise RuntimeError("取 public_values 失败: " + r.stderr.strip()[-300:])
    return set(r.stdout.split())


def main() -> int:
    print("=" * 104)
    print(" 设置项接线一致性检查（防静默失效）")
    print("=" * 104)

    keys = schema_keys()
    pub = public_keys()
    php_src = "\n".join(strip_comments(f.read_text(encoding="utf-8"), "php")
                        for f in ROOT.glob("php/**/*.php") if f.name != "SettingsSchema.php")
    py_files = [f for f in ROOT.glob("app/*.py") if f.name != "sitesettings.py"]
    py_src = "\n".join(strip_comments(f.read_text(encoding="utf-8"), "py") for f in py_files)

    php_used = {k for k in keys if k in php_src}
    py_used = {k for k in keys if k in py_src} | (keys & pub)

    php_missing = sorted(keys - php_used)
    py_missing = sorted(keys - py_used)

    print(f"  schema={len(keys)}  PHP 接线={len(php_used)}  "
          f"Python 接线={len(py_used)}（含前台下发 {len(keys & pub)}）")

    new_php = [k for k in php_missing if k not in ACK_PHP_MISSING]
    stale_php = [k for k in ACK_PHP_MISSING if k not in php_missing]
    new_py = [k for k in py_missing if k not in ACK_PY_MISSING]
    stale_py = [k for k in ACK_PY_MISSING if k not in py_missing]

    def rec(cid, title, ok, note=""):
        print(f"  {cid:<6} {title:<50} {'✓ 通过' if ok else '✗ 失败'}  {note}")

    ghost = sorted((set(php_missing) & set(py_missing)) - set(ACK_PHP_MISSING))
    rec("S-1", "无未登记的「两版都不读」幽灵设置键", not ghost,
        "; ".join(ghost) if ghost else
        "仅剩 1 个已登记的废弃键（pay.currency）" if (set(php_missing) & set(py_missing)) else "0 个")
    rec("S-2", "无**新增**的 PHP 未接线设置项", not new_php,
        "; ".join(new_php) if new_php else f"已登记 {len(ACK_PHP_MISSING)} 项已知差额")
    rec("S-3", "无**新增**的 Python 未接线设置项", not new_py,
        "; ".join(new_py) if new_py else "0 个新增")
    rec("S-4", "ACK 清单无已失效条目（修好了就要从清单划掉）",
        not stale_php and not stale_py,
        "; ".join(stale_php + stale_py) if (stale_php or stale_py) else "清单与现状一致")

    if php_missing:
        print("\n  PHP 侧未接线（含已登记）：")
        for k in php_missing:
            tag = "已登记" if k in ACK_PHP_MISSING else "★新增"
            print(f"    [{tag}] {k}" + (f" — {ACK_PHP_MISSING[k]}" if k in ACK_PHP_MISSING else ""))

    print("=" * 104)
    bad = [new_php, new_py, stale_php, stale_py]
    badc = sum(1 for b in bad if b)
    if sorted((set(php_missing) & set(py_missing)) - set(ACK_PHP_MISSING)):
        badc += 1
    print(f" 合计 4 项，" + ("全部通过 ✓" if not badc else f"失败 ✗ {badc} 项"))
    print("=" * 104)
    return 1 if badc else 0


if __name__ == "__main__":
    sys.exit(main())