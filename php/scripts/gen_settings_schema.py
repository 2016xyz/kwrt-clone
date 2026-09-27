#!/usr/bin/env python3
"""从 Python 版 app/sitesettings.py 生成 PHP 版 php/src/SettingsSchema.php。

为什么要生成而不是手抄：
  设置项有 100+ 条，两个实现各维护一份必然漂移 —— 加了一项忘了另一边，
  就会出现「管理端能改、前台不生效」这种最难查的 bug。
  这里让 Python 侧当**唯一事实来源**，PHP 侧由它生成。

用法：
    python3 php/scripts/gen_settings_schema.py            # 生成
    python3 php/scripts/gen_settings_schema.py --check     # 只校验是否已同步（CI 用）
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from app import sitesettings as SS  # noqa: E402

OUT = os.path.join(ROOT, "php", "src", "SettingsSchema.php")

# 需要透传到 PHP 的类型专属字段
EXTRA_KEYS = ("opts", "max", "min", "hint", "hidden", "step", "rows")


def php_str(s) -> str:
    return "'" + str(s).replace("\\", "\\\\").replace("'", "\\'") + "'"


def php_val(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (list, dict)):
        return php_str(json.dumps(v, ensure_ascii=False))
    if v is None:
        return "null"
    return php_str(v)


def build() -> str:
    groups = ",\n        ".join(
        "[" + ", ".join([php_str(g[0]), php_str(g[1]), php_str(g[2] if len(g) > 2 else "")]) + "]"
        for g in SS.GROUPS
    )

    rows = []
    for s in SS.SCHEMA:
        parts = [
            "'k' => " + php_str(s["k"]),
            "'g' => " + php_str(s.get("g", "")),
            "'t' => " + php_str(s.get("t", "text")),
            "'label' => " + php_str(s.get("label", s["k"])),
            "'d' => " + php_val(s.get("d")),
        ]
        for extra in EXTRA_KEYS:
            if extra in s:
                parts.append("'" + extra + "' => " + php_val(s[extra]))
        rows.append("        [" + ", ".join(parts) + "],")
    schema = "\n".join(rows)

    return f"""<?php
/**
 * 设置项定义（schema）。
 *
 * ★ 本文件由 php/scripts/gen_settings_schema.py 从 Python 版
 *   app/sitesettings.py 自动生成，**请勿手改**。
 *   要增删设置项，请改 Python 侧再重新生成，两版永远同源：
 *
 *       python3 php/scripts/gen_settings_schema.py
 *
 * 生成基准：{len(SS.SCHEMA)} 项设置 / {len(SS.GROUPS)} 个分组
 */
declare(strict_types=1);

namespace Kwrt;

final class SettingsSchema
{{
    /** 分组：[key, 标签, 说明]，顺序即管理端展示顺序。 */
    public const GROUPS = [
        {groups},
    ];

    /** 全部设置项：k=键 g=分组 t=类型 d=默认值 label=标签（+ 类型专属约束）。 */
    public const SCHEMA = [
{schema}
    ];

    /** 键 => 定义 的索引。 */
    public static function byKey(): array
    {{
        $m = [];
        foreach (self::SCHEMA as $s) {{
            $m[$s['k']] = $s;
        }}
        return $m;
    }}

    /** 某分组下的全部设置项。 */
    public static function ofGroup(string $g): array
    {{
        return array_values(array_filter(self::SCHEMA, static fn($s) => $s['g'] === $g));
    }}
}}
"""


def main() -> int:
    php = build()
    if "--check" in sys.argv:
        cur = open(OUT, encoding="utf-8").read() if os.path.exists(OUT) else ""
        if cur != php:
            print("✗ php/src/SettingsSchema.php 与 Python 版不同步，请重新生成", file=sys.stderr)
            return 1
        print("✓ PHP 设置 schema 与 Python 版同源")
        return 0
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(php)
    print(f"✓ 已生成 {OUT}（{len(php)} 字节，{len(SS.SCHEMA)} 项 / {len(SS.GROUPS)} 分组）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
