#!/usr/bin/env python3
"""把 PHP 生成的二维码矩阵与参考实现 python-qrcode 逐格比对。

强制掩码 0–7：掩码选择策略（罚分规则）各家实现可能有细微差异，
但那不影响"编码/RS 纠错/交织/布点/格式信息"的正确性判定。
只要 8 个掩码下矩阵全等，就说明 PHP 编码器与参考实现逐比特一致。

另外用真实解码器（opencv 若可用）验证 PNG 真能被扫出来。
"""
import json
import sys

import qrcode
from qrcode.util import QRData, MODE_8BIT_BYTE

cases = json.load(open('/tmp/qr_tests/cases.json'))
php = json.load(open('/tmp/qr_tests/php_out.json'))

assert len(cases) == len(php), "用例数不一致"

total = 0
bad = 0
for i, text in enumerate(cases):
    ref = []
    for mask in range(8):
        q = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M,
                          box_size=1, border=0, mask_pattern=mask)
        q.add_data(QRData(text, mode=MODE_8BIT_BYTE))
        q.make(fit=True)
        m = q.modules
        ref.append("\n".join("".join("1" if v else "0" for v in row) for row in m))

    for mask in range(8):
        total += 1
        a, b = php[i]['masks'][mask], ref[mask]
        if a != b:
            bad += 1
            if bad <= 3:
                la, lb = a.split("\n"), b.split("\n")
                diff = [(r, c) for r in range(min(len(la), len(lb)))
                        for c in range(min(len(la[r]), len(lb[r])))
                        if la[r][c] != lb[r][c]]
                print(f"✗ 用例{i} (len={php[i]['len']}) 掩码{mask} 不一致："
                      f"PHP {len(la)}x{len(la[0])} vs REF {len(lb)}x{len(lb[0])}，"
                      f"差异格数={len(diff)}，示例={diff[:5]}")
print(f"\n掩码逐格比对：{total - bad}/{total} 一致")

# 自动选掩码的结果也应当是一个合法矩阵（尺寸对、与某个强制掩码结果相等）
auto_ok = 0
for i in range(len(cases)):
    auto = php[i]['auto']
    if auto in php[i]['masks']:
        auto_ok += 1
    else:
        print(f"✗ 用例{i} 自动掩码结果不等于任何一个强制掩码结果（罚分流程有副作用）")
print(f"自动选掩码：{auto_ok}/{len(cases)} 落在 8 个合法掩码结果内")

# 用真实解码器验证 PNG
try:
    import cv2
    import numpy as np
    img = cv2.imread('/tmp/qr_tests/php_qr.png', cv2.IMREAD_GRAYSCALE)
    det = cv2.QRCodeDetector()
    data, pts, _ = det.detectAndDecode(img)
    print(f"\nopencv 解码 PHP PNG → {data!r}")
    expect = 'https://qr.alipay.com/fkx12345?amount=50'
    print("解码结果匹配：" + ("是 ✓" if data == expect else f"否 ✗（期望 {expect!r}）"))
except ImportError:
    print("\n（本机无 opencv，跳过 PNG 实际解码校验）")

sys.exit(0 if bad == 0 else 1)
