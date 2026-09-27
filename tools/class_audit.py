#!/usr/bin/env python3
"""检查 HTML 中使用的 class 是否都在 app.css 中有定义（避免样式缺失导致的隐形破版）。"""
import re
import sys
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSS = open(os.path.join(ROOT, 'web/assets/css/app.css'), encoding='utf-8').read()
# 提取 CSS 中定义过的类名
defined = set(re.findall(r'\.([a-zA-Z][a-zA-Z0-9_-]*)', CSS))

# 动态拼接的类名（JS 里生成），忽略
IGNORE = {
    'on', 'out', 'hide', 'over', 'is-loading', 'is-invalid', 'is-disabled',
    'grid-auto-sm', 'grid-auto-lg', 'badge-dot', 'badge-pulse', 'progress-lg',
    'card-pad-sm', 'card-pad-lg', 'card-flat', 'card-sunken', 'card-hover',
    'table-responsive', 'container-narrow', 'container-tight', 'modal-lg', 'modal-sm',
    'btn-icon', 'btn-block', 'btn-square', 'btn-xs', 'btn-sm', 'btn-lg', 'btn-xl',
    'num', 'actions', 'mono', 'tnum', 'req',
}

bad_total = 0
for name in ('index.html', 'login.html', 'admin.html', 'verify.html'):
    p = os.path.join(ROOT, 'web', name)
    if not os.path.isfile(p):
        continue
    s = open(p, encoding='utf-8').read()
    used = set()
    # 只匹配静态 class="，排除 :class="（Vue 绑定表达式）
    for m in re.finditer(r'(?<![:\w-])class="([^"]*)"', s):
        v = m.group(1)
        # 跳过 Vue 绑定里的表达式
        if '{{' in v or '$' in v:
            v = re.sub(r'\{\{[^}]*\}\}', ' ', v)
        for c in v.split():
            if re.match(r'^[a-zA-Z][a-zA-Z0-9_-]*$', c):
                used.add(c)
    missing = sorted(c for c in used if c not in defined and c not in IGNORE)
    print(f"  {name:<14} 使用 {len(used):>3} 个类，缺失定义 {len(missing)} 个"
          + (f"  → {missing}" if missing else "  ✓"))
    bad_total += len(missing)

print()
print("结果:", "全部有定义 ✓" if bad_total == 0 else f"共 {bad_total} 个类缺样式定义")
sys.exit(0 if bad_total == 0 else 1)
