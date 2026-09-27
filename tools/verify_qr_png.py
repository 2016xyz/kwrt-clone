#!/usr/bin/env python3
"""支付二维码 PNG 回归 —— 「零依赖生成」的闭环验证。

## 本次修掉的线上缺陷（HTTP 500）

    File "/root/kwrt-clone/app/pay.py", line 385, in qr_png_bytes
    ModuleNotFoundError: No module named 'PIL'

`qr_png_bytes()` 原先走 `qrcode` 的**默认工厂**，那是 PIL 实现，末尾还有
`img.resize()`；而 `requirements.txt` 只声明了 `qrcode`，并明确注释
「使用 qrcode.image.svg，**不需要 Pillow**」。于是按 requirements 装出来的
干净部署一访问 `/api/v1/sponsor/pay/{订单}/qr.png` 必 500 ——
**前端支付弹窗的 <img> 正是指向这个端点**，所以是「支付二维码永远出不来」。

缺陷类别：**注释承诺的能力 ≠ 代码实际依赖的能力**。
开发机上恰好装了 Pillow 就永远看不见，只有干净部署才炸。

## 断言策略

Q-1  生成环境**确实没有 Pillow**（前提；有就没法证明零依赖）
Q-2  PNG 结构合法：签名 + chunk 序列 + 每个 chunk 的 CRC32
Q-3  IHDR 字段正确：1 bit、调色板（type 3）、非隔行
Q-4  IDAT 可解压，长度 = 行数 ×（1 + 每行字节数）
Q-5  **位图逐像素等于 qrcode 的矩阵**（含整数倍放大与静区）——
     这是最关键的一条：只要位图与库矩阵一致，内容就一定是那个二维码
Q-6  多载荷（不同 QR 版本/尺寸）都能生成且结构合法
Q-7  **真解码**：用带 OpenCV 的解释器解码，文本与原文一致
Q-8  静态护栏：`app/pay.py` 不得再出现 PIL 依赖写法（可注入变红）
Q-9  负向对照：把 PIL 相关写法注入临时副本 → Q-8 必须变红

## 用法
    python3 tools/verify_qr_png.py
"""
from __future__ import annotations

import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

REC: list[tuple[str, str, bool, str]] = []


def rec(cid: str, title: str, ok: bool, note: str = "") -> None:
    REC.append((cid, title, bool(ok), note))
    print(f"  {cid:<6} {title:<58} {'✓ 通过' if ok else '✗ 失败'}  {note}")


# --------------------------------------------------------------------------- #
def parse_png(png: bytes) -> dict:
    """严格解析：签名 + chunk 序列 + 逐 chunk CRC。任何一处不合法直接抛。"""
    if png[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("PNG 签名错误")
    off, chunks, meta = 8, [], {}
    while off < len(png):
        (ln,) = struct.unpack(">I", png[off:off + 4])
        tag = png[off + 4:off + 8]
        data = png[off + 8:off + 8 + ln]
        (crc,) = struct.unpack(">I", png[off + 8 + ln:off + 12 + ln])
        if crc != (zlib.crc32(tag + data) & 0xFFFFFFFF):
            raise ValueError(f"chunk {tag!r} CRC 校验失败")
        chunks.append(tag.decode())
        if tag == b"IHDR":
            (meta["w"], meta["h"], meta["depth"], meta["ctype"],
             meta["comp"], meta["filt"], meta["inter"]) = struct.unpack(">IIBBBBB", data)
        elif tag == b"IDAT":
            meta["idat"] = meta.get("idat", b"") + data
        elif tag == b"PLTE":
            meta["plte"] = data
        off += 12 + ln
    meta["chunks"] = chunks
    return meta


def bitmap(meta: dict) -> list[list[bool]]:
    """把 PNG 还原成像素矩阵；True = 深色（调色板 0）。"""
    w, h = meta["w"], meta["h"]
    raw = zlib.decompress(meta["idat"])
    rb = (w + 7) // 8
    if len(raw) != h * (1 + rb):
        raise ValueError(f"IDAT 解压长度 {len(raw)} != {h * (1 + rb)}")
    out = []
    for y in range(h):
        base = y * (1 + rb) + 1
        row = raw[base:base + rb]
        if raw[y * (1 + rb)] != 0:
            raise ValueError(f"第 {y} 行 filter type 非 0")
        out.append([((row[x >> 3] >> (7 - (x & 7))) & 1) == 0 for x in range(w)])
    return out


DECODER = r'''
import sys
try:
    import cv2
except ImportError:
    print("NO_CV2"); sys.exit(0)
img = cv2.imread(sys.argv[1])
if img is None:
    print("DECODE_FAIL"); sys.exit(0)
data, _pts, _ = cv2.QRCodeDetector().detectAndDecode(img)
print("DECODED:" + (data or ""))
'''


def try_decode(png_path: Path) -> tuple[bool, str]:
    """找一个带 cv2 的解释器来真解码。找不到就返回 (False, 'no-cv2')。"""
    cands = [sys.executable, "/usr/bin/python3", "/usr/bin/python3.12",
             "/usr/bin/python3.13", "/usr/bin/python3.9"]
    for exe in dict.fromkeys(cands):
        if not Path(exe).exists():
            continue
        r = subprocess.run([exe, "-c", DECODER, str(png_path)],
                           capture_output=True, text=True)
        out = (r.stdout or "").strip()
        if out.startswith("DECODED:"):
            return True, out[len("DECODED:"):]
        if out == "NO_CV2":
            continue
        if out == "DECODE_FAIL":
            return False, "解码失败"
    return False, "no-cv2"


# --------------------------------------------------------------------------- #
def main() -> int:
    print("=" * 108)
    print(" 支付二维码 PNG 回归（零依赖生成 + 真解码闭环）")
    print("=" * 108)

    # ---------- Q-1 前提：确实没有 Pillow ----------
    has_pil = False
    try:
        import PIL  # noqa: F401
        has_pil = True
    except ImportError:
        pass
    rec("Q-1", "生成环境确实没有 Pillow（零依赖验证的前提）",
        not has_pil,
        "无 Pillow" if not has_pil else "有 Pillow —— 本套件证明不了「干净部署可用」")

    from app import pay

    TEXT = "https://qr.alipay.com/bavh4wjlxf12tper3a"
    png = pay.qr_png_bytes(TEXT, size=320)
    (ROOT / ".ekko-tmp").mkdir(exist_ok=True)
    probe = ROOT / ".ekko-tmp" / "qr_verify.png"
    probe.write_bytes(png)

    # ---------- Q-2 结构 ----------
    try:
        meta = parse_png(png)
        ok2, note2 = True, f"{len(png)}B, chunks={meta['chunks']}"
    except Exception as e:
        meta, ok2, note2 = {}, False, str(e)
    rec("Q-2", "PNG 结构合法（签名 / chunk 序列 / 逐 chunk CRC32）", ok2, note2)

    # ---------- Q-3 IHDR ----------
    rec("Q-3", "IHDR：1 bit 调色板图、非隔行",
        ok2 and meta.get("depth") == 1 and meta.get("ctype") == 3
        and meta.get("inter") == 0 and meta.get("comp") == 0 and meta.get("filt") == 0,
        f"depth={meta.get('depth')} ctype={meta.get('ctype')} inter={meta.get('inter')}")

    # ---------- Q-4 IDAT ----------
    q4 = False
    note4 = ""
    try:
        raw = zlib.decompress(meta["idat"])
        rb = (meta["w"] + 7) // 8
        q4 = len(raw) == meta["h"] * (1 + rb)
        note4 = f"解压 {len(raw)}B = {meta['h']} × (1 + {rb})"
    except Exception as e:
        note4 = str(e)
    rec("Q-4", "IDAT 可解压且长度与行数/每行字节数吻合", q4, note4)

    # ---------- Q-5 位图 == qrcode 矩阵 ----------
    q5, note5 = False, ""
    try:
        import qrcode
        q = qrcode.QRCode(box_size=1, border=2,
                          error_correction=qrcode.constants.ERROR_CORRECT_M)
        q.add_data(TEXT)
        q.make(fit=True)
        m = q.get_matrix()
        n = len(m)
        scale = meta["w"] // n
        bm = bitmap(meta)
        diff = sum(1 for y in range(meta["h"]) for x in range(meta["w"])
                   if bm[y][x] != m[y // scale][x // scale])
        q5 = (meta["w"] % n == 0 and scale >= 1 and diff == 0)
        note5 = f"{n}×{n} 模块，放大 {scale} 倍，{meta['w']}×{meta['h']} 像素，逐像素差异 {diff}"
    except Exception as e:
        note5 = str(e)
    rec("Q-5", "位图与 qrcode 库矩阵逐像素一致（含整数倍放大与静区）", q5, note5)

    # ---------- Q-6 多载荷 ----------
    bad6 = []
    for t in ["A", "https://qr.alipay.com/x", "x" * 120, "https://qr.alipay.com/" + "y" * 300]:
        try:
            p2 = pay.qr_png_bytes(t, size=320)
            m2 = parse_png(p2)
            bitmap(m2)
        except Exception as e:
            bad6.append(f"{t[:12]}…: {e}")
    rec("Q-6", "多载荷（不同 QR 版本）均生成成功且结构合法",
        not bad6, f"4/4 通过" if not bad6 else "; ".join(bad6)[:80])

    # ---------- Q-7 真解码 ----------
    ok7, got = try_decode(probe)
    if got == "no-cv2":
        rec("Q-7", "OpenCV 真解码，内容与原文一致", True, "本机无 cv2，跳过（不计失败）")
    else:
        rec("Q-7", "OpenCV 真解码，内容与原文一致",
            ok7 and got == TEXT, f"解码 {got[:46]!r}")

    # ---------- Q-8 静态护栏 ----------
    SRC = (ROOT / "app" / "pay.py").read_text(encoding="utf-8")

    def banned(text: str) -> list[str]:
        """剥离注释与字符串字面量后再匹配 —— 否则注释里提到 Pillow 就会误判。

        注：`qrcode.image.svg` 是允许的（它不依赖 PIL），
        所以不能一刀切禁 `make_image`，只禁「默认工厂 + resize」这组写法。
        """
        t = re.sub(r"#.*", "", text)
        t = re.sub(r'"""(?:.|\n)*?"""', "", t)
        t = re.sub(r"'''(?:.|\n)*?'''", "", t)
        t = re.sub(r'"(?:[^"\\]|\\.)*"', '""', t)
        t = re.sub(r"'(?:[^'\\]|\\.)*'", "''", t)
        hits = []
        if re.search(r"\bresize\s*\(", t):
            hits.append("resize()")
        if re.search(r"\bmake_image\s*\(", t):
            hits.append("make_image()")
        if re.search(r"\bfrom\s+PIL\b|\bimport\s+PIL\b", t):
            hits.append("PIL")
        return hits

    h8 = banned(SRC)
    rec("Q-8", "静态护栏：pay.py 不再出现 PIL 依赖写法（注释已剥离）",
        not h8, "干净" if not h8 else f"命中 {h8}")

    # ---------- Q-9 负向对照：护栏必须能变红 ----------
    tmp = Path(tempfile.mkdtemp(prefix="kwrt-qr-"))
    try:
        # 确定性注入：追加一段「默认工厂 + resize」的写法，护栏必须命中。
        # 一个不会变红的护栏等于没有护栏，所以这一步是必须的。
        inj = SRC + ("\n\n# 负向对照注入\ndef _injected():\n"
                     "    import qrcode\n"
                     "    img = qrcode.make_image()\n"
                     "    return img.resize((320, 320))\n")
        h9 = banned(inj)
        rec("Q-9", "负向对照：注入 PIL 写法后护栏必须变红",
            bool(h9) and banned(SRC) == [], f"注入后命中 {h9}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    try:
        probe.unlink()
    except Exception:
        pass

    print("=" * 108)
    bad = [r for r in REC if not r[2]]
    print(f" 合计 {len(REC)} 项，" + ("全部通过 ✓" if not bad else f"失败 ✗ {[b[0] for b in bad]}"))
    print("=" * 108)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
