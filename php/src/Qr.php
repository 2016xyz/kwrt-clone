<?php

declare(strict_types=1);

namespace Kwrt;

/**
 * 零依赖二维码 PNG 生成器。
 *
 * ★ 为什么不用第三方库 / 不跳转到外部服务：
 *   1. 收款码内容里含站长的收款标识（支付宝个人收款链接等），交给第三方二维码
 *      接口等于把收款信息送给别人，还可能被记录、被篡改 —— 必须本地生成。
 *   2. PHP 版原先的 sponsorPayQr 是直接 302 跳到支付宝返回的 qr_code 链接，
 *      那对"当面付"有效（支付宝自己托管二维码），但对**站长自己的收款码**无效：
 *      没有可跳转的目的地，只能自己画。
 *   3. 引 composer 包会给部署加一层依赖（Python 版就是因为 requirements 少写了
 *      Pillow 导致线上二维码 500），这里按同样的原则：只在标准库内实现。
 *
 * 实现范围：字节模式（UTF-8 安全的二进制）、纠错等级 M、版本 1–20（约 660 字节），
 * 覆盖收款链接这种量级绰绰有余。输出 1 bit 调色板 PNG，用 gzcompress 做 zlib
 * 流，自己拼 chunk + CRC32 —— 与 Python 版 app/pay.py::qr_png_bytes 同源同规格。
 */
final class Qr
{
    /** 每个模块的最小像素数 —— 低于这个值，扫码器在密集码上会开始失败 */
    public const MIN_MODULE_PX = 6;

    /**
     * 版本 → [每块纠错码字数, 组1块数, 组1数据码字数, 组2块数, 组2数据码字数]
     * 取自 ISO/IEC 18004 表 9，纠错等级 M。
     */
    private const EC_M = [
        1  => [10, 1, 16, 0, 0],
        2  => [16, 1, 28, 0, 0],
        3  => [26, 1, 44, 0, 0],
        4  => [18, 2, 32, 0, 0],
        5  => [24, 2, 43, 0, 0],
        6  => [16, 4, 27, 0, 0],
        7  => [18, 4, 31, 0, 0],
        8  => [22, 2, 38, 2, 39],
        9  => [22, 3, 36, 2, 37],
        10 => [26, 4, 43, 1, 44],
        11 => [30, 1, 50, 4, 51],
        12 => [22, 6, 36, 2, 37],
        13 => [22, 8, 37, 1, 38],
        14 => [24, 4, 40, 5, 41],
        15 => [24, 5, 41, 5, 42],
        16 => [28, 7, 45, 3, 46],
        17 => [28, 10, 46, 1, 47],
        18 => [26, 9, 43, 4, 44],
        19 => [26, 3, 44, 11, 45],
        20 => [26, 3, 41, 13, 42],
    ];

    /** 版本 → 校正图案中心坐标 */
    private const ALIGN = [
        1 => [], 2 => [6, 18], 3 => [6, 22], 4 => [6, 26], 5 => [6, 30], 6 => [6, 34],
        7 => [6, 22, 38], 8 => [6, 24, 42], 9 => [6, 26, 46], 10 => [6, 28, 50],
        11 => [6, 30, 54], 12 => [6, 32, 58], 13 => [6, 34, 62],
        14 => [6, 26, 46, 66], 15 => [6, 26, 48, 70], 16 => [6, 26, 50, 74],
        17 => [6, 30, 54, 78], 18 => [6, 30, 56, 82], 19 => [6, 30, 58, 86],
        20 => [6, 34, 62, 90],
    ];

    /** @var int[]|null GF(256) 指数表 */
    private static $exp = null;
    /** @var int[]|null GF(256) 对数表 */
    private static $log = null;

    // ------------------------------------------------------------------ //
    // 对外
    // ------------------------------------------------------------------ //

    /**
     * 生成 PNG 字节流。
     *
     * @param string $text 要编码的内容
     * @param int    $size 目标边长**下限**；实际取不小于它的整数倍模块大小
     * @return string PNG 二进制
     */
    public static function png(string $text, int $size = 320): string
    {
        $m = self::matrix($text);
        $n = count($m);
        // ★ 模块像素不小于 MIN_MODULE_PX。密集码（版本 15 以上）在 320px 的目标下
        //   只能分到 3–4 px/模块，实测正是扫码器最容易失败的临界带 —— 宁可把图
        //   放大一点，也不要交给用户一张"看着清楚、就是扫不出来"的收款码。
        $scale = max(self::MIN_MODULE_PX, intdiv(max($size, 1), $n));
        $border = 2;                       // 静默区（模块数），与 Python 版一致
        $dim = ($n + 2 * $border) * $scale;

        // 逐行拼 1 bit/像素的调色板索引位图：调色板 0 = 深色，1 = 浅色底。
        // 每行开头一个 filter type（0 = None），像素按**高位在前**打包。
        //
        // ★ 本行必须以「全浅色」为初值，再把深色模块的位**清掉**。
        //   反过来（初值 0、浅色置位）会让静默区——即矩阵之外、从未被写入的像素——
        //   全部落成调色板 0 的深色，二维码四周直接糊成一圈黑边就扫不出来了。
        //   这个坑很隐蔽：静默区在"模块矩阵"里根本不存在，逐格比对矩阵永远发现不了，
        //   只有把 PNG 真正解码才知道。（Python 版没这个问题，是因为它的矩阵由
        //   qrcode 库带着 border 一起给出，不存在"矩阵外"的像素。）
        $rowBytes = intdiv($dim + 7, 8);
        $tail = $dim & 7;                    // 末字节多余位的掩码：只保留高 $tail 位
        $tailMask = $tail ? (0xFF << (8 - $tail)) & 0xFF : 0;
        $raw = '';
        for ($y = 0; $y < $dim; $y++) {
            $my = intdiv($y, $scale) - $border;
            $line = str_repeat("\xFF", $rowBytes);   // 默认浅色
            if ($my >= 0 && $my < $n) {
                for ($x = 0; $x < $dim; $x++) {
                    $mx = intdiv($x, $scale) - $border;
                    if ($mx < 0 || $mx >= $n) {
                        continue;                        // 静默区：保持浅色
                    }
                    if ($m[$my][$mx]) {                  // 深色模块 → 索引 0（清位）
                        $i = $x >> 3;
                        $line[$i] = chr(ord($line[$i]) & ~(0x80 >> ($x & 7)));
                    }
                }
            }
            if ($tail) {
                $line[$rowBytes - 1] = chr(ord($line[$rowBytes - 1]) & $tailMask);
            }
            $raw .= "\x00" . $line;
        }

        $ihdr = pack('NNCCCCC', $dim, $dim, 1, 3, 0, 0, 0);          // 1 bit 调色板图
        $plte = "\x0f\x17\x2a\xff\xff\xff";                          // 0=#0f172a 1=#ffffff

        return "\x89PNG\r\n\x1a\n"
            . self::chunk('IHDR', $ihdr)
            . self::chunk('PLTE', $plte)
            . self::chunk('IDAT', gzcompress($raw, 9))
            . self::chunk('IEND', '');
    }

    /**
     * 生成模块矩阵（true = 深色）。含静默区之外的纯码图。
     * 单独暴露出来是为了能**与参考实现逐格比对**，而不是只靠"看着像"。
     *
     * @param int|null $forceMask 强制使用某个掩码（0–7）；null 则按罚分自动选优
     * @return array<int, array<int, bool>>
     */
    public static function matrix(string $text, ?int $forceMask = null): array
    {
        $version = self::pickVersion(strlen($text));
        $cfg = self::EC_M[$version];
        $n = $version * 4 + 17;

        $codewords = self::dataCodewords($text, $version, $cfg);

        // ---- 分块 + 纠错 + 交错 ----
        $blocks = [];
        $offset = 0;
        foreach ([[$cfg[1], $cfg[2]], [$cfg[3], $cfg[4]]] as [$count, $len]) {
            for ($i = 0; $i < $count; $i++) {
                $d = array_slice($codewords, $offset, $len);
                $offset += $len;
                $blocks[] = ['d' => $d, 'e' => self::rsRemainder($d, $cfg[0]), 'len' => $len];
            }
        }
        $final = [];
        $maxData = 0;
        foreach ($blocks as $b) {
            $maxData = max($maxData, $b['len']);
        }
        for ($i = 0; $i < $maxData; $i++) {
            foreach ($blocks as $b) {
                if ($i < $b['len']) {
                    $final[] = $b['d'][$i];
                }
            }
        }
        for ($i = 0; $i < $cfg[0]; $i++) {
            foreach ($blocks as $b) {
                $final[] = $b['e'][$i];
            }
        }

        // ---- 固定图案 ----
        $reserved = array_fill(0, $n, array_fill(0, $n, false));
        $base = array_fill(0, $n, array_fill(0, $n, false));

        $finder = static function (int $r, int $c) use (&$base, &$reserved, $n): void {
            for ($i = -1; $i <= 7; $i++) {
                for ($j = -1; $j <= 7; $j++) {
                    $y = $r + $i;
                    $x = $c + $j;
                    if ($y < 0 || $y >= $n || $x < 0 || $x >= $n) {
                        continue;
                    }
                    $on = ($i >= 0 && $i <= 6 && $j >= 0 && $j <= 6)
                        && (($i === 0 || $i === 6 || $j === 0 || $j === 6)
                            || ($i >= 2 && $i <= 4 && $j >= 2 && $j <= 4));
                    $base[$y][$x] = $on;
                    $reserved[$y][$x] = true;
                }
            }
        };
        $finder(0, 0);
        $finder(0, $n - 7);
        $finder($n - 7, 0);

        // ★ 校正图案必须在**定时图案之前**放置 —— 二者在 (6, x) / (x, 6) 上相交。
        //   反过来先铺定时线，会把校正图案的中心判成"已占用"而整块跳过，
        //   生成出来的码少了校正图案，扫码器在倾斜/畸变下就认不出来。
        $align = self::ALIGN[$version];
        foreach ($align as $r) {
            foreach ($align as $c) {
                if ($reserved[$r][$c]) {
                    continue;                                    // 与定位图案重叠
                }
                for ($i = -2; $i <= 2; $i++) {
                    for ($j = -2; $j <= 2; $j++) {
                        $base[$r + $i][$c + $j] = (max(abs($i), abs($j)) !== 1);
                        $reserved[$r + $i][$c + $j] = true;
                    }
                }
            }
        }

        for ($i = 8; $i < $n - 8; $i++) {                       // 定时图案（只填空位）
            if (!$reserved[6][$i]) {
                $base[6][$i] = ($i % 2 === 0);
                $reserved[6][$i] = true;
            }
            if (!$reserved[$i][6]) {
                $base[$i][6] = ($i % 2 === 0);
                $reserved[$i][6] = true;
            }
        }

        $base[$n - 8][8] = true;                                 // 固定深色模块
        $reserved[$n - 8][8] = true;
        for ($i = 0; $i <= 8; $i++) {                            // 格式信息区
            if ($i !== 6) {
                $reserved[8][$i] = true;
                $reserved[$i][8] = true;
            }
        }
        $reserved[8][6] = true;
        $reserved[6][8] = true;
        for ($i = 0; $i < 8; $i++) {
            $reserved[8][$n - 1 - $i] = true;
            $reserved[$n - 1 - $i][8] = true;
        }
        if ($version >= 7) {                                     // 版本信息区
            for ($i = 0; $i < 6; $i++) {
                for ($j = 0; $j < 3; $j++) {
                    $reserved[$n - 11 + $j][$i] = true;
                    $reserved[$i][$n - 11 + $j] = true;
                }
            }
        }

        // 功能图案占位图，此后不再变化 —— 数据填充与掩码都拿它做"是不是功能格"的判定
        $fn = $reserved;

        // ---- 数据位按之字形填入 ----
        $total = count($final) * 8;
        $bitAt = static function (int $k) use ($final): int {
            return ($final[$k >> 3] >> (7 - ($k & 7))) & 1;
        };
        $idx = 0;
        $up = true;
        for ($col = $n - 1; $col > 0; $col -= 2) {
            // 第 6 列是竖向定时图案，整列跳过；col<=6 时整体左移一格，
            // 使得成对的列依次是 (n-1,n-2)…(8,7),(5,4),(3,2),(1,0) —— 第 0 列不会被漏掉。
            $cc = $col <= 6 ? $col - 1 : $col;
            for ($k = 0; $k < $n; $k++) {
                $row = $up ? ($n - 1 - $k) : $k;
                foreach ([$cc, $cc - 1] as $c) {
                    if ($fn[$row][$c]) {
                        continue;
                    }
                    $base[$row][$c] = $idx < $total ? ($bitAt($idx) === 1) : false;
                    $idx++;
                }
            }
            $up = !$up;
        }

        // ---- 选掩码：逐个套用 + 写格式信息 + 算罚分 ----
        $best = null;
        $bestScore = PHP_INT_MAX;
        $masks = $forceMask === null ? range(0, 7) : [$forceMask];
        foreach ($masks as $mask) {
            $cand = $base;
            for ($r = 0; $r < $n; $r++) {
                for ($c = 0; $c < $n; $c++) {
                    if ($fn[$r][$c]) {
                        continue;                                // 功能图案不受掩码影响
                    }
                    if (self::maskHit($mask, $r, $c)) {
                        $cand[$r][$c] = !$cand[$r][$c];
                    }
                }
            }
            self::writeFormat($cand, $mask, $n);
            if ($version >= 7) {
                self::writeVersion($cand, $version, $n);
            }
            $score = self::penalty($cand, $n);
            if ($score < $bestScore) {
                $bestScore = $score;
                $best = $cand;
            }
        }

        return $best;
    }

    // ------------------------------------------------------------------ //
    // 内部：版本 / 数据码字
    // ------------------------------------------------------------------ //

    private static function pickVersion(int $len): int
    {
        foreach (self::EC_M as $v => $cfg) {
            $cap = $cfg[1] * $cfg[2] + $cfg[3] * $cfg[4];
            $ccBits = $v <= 9 ? 8 : 16;
            $needBits = 4 + $ccBits + 8 * $len;
            if (intdiv($needBits + 7, 8) <= $cap) {
                return $v;
            }
        }
        throw new \RuntimeException('收款码内容过长，无法生成二维码（请精简到 600 字节以内）');
    }

    private static function dataCodewords(string $text, int $version, array $cfg): array
    {
        $totalData = $cfg[1] * $cfg[2] + $cfg[3] * $cfg[4];
        $capBits = $totalData * 8;
        $bits = [];
        $push = static function (int $val, int $n) use (&$bits): void {
            for ($i = $n - 1; $i >= 0; $i--) {
                $bits[] = ($val >> $i) & 1;
            }
        };
        $push(0b0100, 4);                                        // 字节模式
        $push(strlen($text), $version <= 9 ? 8 : 16);            // 字符计数
        for ($i = 0, $l = strlen($text); $i < $l; $i++) {
            $push(ord($text[$i]), 8);
        }
        $term = min(4, $capBits - count($bits));                 // 结束符
        for ($i = 0; $i < $term; $i++) {
            $bits[] = 0;
        }
        while (count($bits) % 8 !== 0) {                         // 补齐字节
            $bits[] = 0;
        }
        $pads = [0xEC, 0x11];
        $pi = 0;
        while (count($bits) < $capBits) {                        // 填充码字
            $push($pads[$pi % 2], 8);
            $pi++;
        }
        $out = [];
        for ($i = 0; $i < count($bits); $i += 8) {
            $b = 0;
            for ($j = 0; $j < 8; $j++) {
                $b = ($b << 1) | $bits[$i + $j];
            }
            $out[] = $b;
        }
        return $out;
    }

    // ------------------------------------------------------------------ //
    // 内部：GF(256) / Reed-Solomon
    // ------------------------------------------------------------------ //

    private static function gfInit(): void
    {
        if (self::$exp !== null) {
            return;
        }
        $exp = array_fill(0, 512, 0);
        $log = array_fill(0, 256, 0);
        $x = 1;
        for ($i = 0; $i < 255; $i++) {
            $exp[$i] = $x;
            $log[$x] = $i;
            $x <<= 1;
            if ($x & 0x100) {
                $x ^= 0x11D;
            }
        }
        for ($i = 255; $i < 512; $i++) {
            $exp[$i] = $exp[$i - 255];
        }
        self::$exp = $exp;
        self::$log = $log;
    }

    private static function gfMul(int $a, int $b): int
    {
        if ($a === 0 || $b === 0) {
            return 0;
        }
        return self::$exp[self::$log[$a] + self::$log[$b]];
    }

    /** 生成多项式 ∏(x + α^i)，i = 0..ecLen-1；返回长度 ecLen+1，首项恒为 1 */
    private static function rsGen(int $ecLen): array
    {
        self::gfInit();
        $g = [1];
        for ($i = 0; $i < $ecLen; $i++) {
            $ng = array_fill(0, count($g) + 1, 0);
            foreach ($g as $j => $c) {
                $ng[$j] ^= $c;
                $ng[$j + 1] ^= self::gfMul($c, self::$exp[$i]);
            }
            $g = $ng;
        }
        return $g;
    }

    private static function rsRemainder(array $data, int $ecLen): array
    {
        $g = self::rsGen($ecLen);
        $rem = array_fill(0, $ecLen, 0);
        foreach ($data as $b) {
            $factor = $b ^ $rem[0];
            array_shift($rem);
            $rem[] = 0;
            if ($factor !== 0) {
                for ($i = 0; $i < $ecLen; $i++) {
                    $rem[$i] ^= self::gfMul($g[$i + 1], $factor);
                }
            }
        }
        return $rem;
    }

    // ------------------------------------------------------------------ //
    // 内部：掩码 / 格式 / 版本 / 罚分
    // ------------------------------------------------------------------ //

    private static function maskHit(int $mask, int $r, int $c): bool
    {
        switch ($mask) {
            case 0: return ($r + $c) % 2 === 0;
            case 1: return $r % 2 === 0;
            case 2: return $c % 3 === 0;
            case 3: return ($r + $c) % 3 === 0;
            case 4: return (intdiv($r, 2) + intdiv($c, 3)) % 2 === 0;
            case 5: return (($r * $c) % 2) + (($r * $c) % 3) === 0;
            case 6: return ((($r * $c) % 2) + (($r * $c) % 3)) % 2 === 0;
            case 7: return ((($r + $c) % 2) + (($r * $c) % 3)) % 2 === 0;
        }
        return false;
    }

    private static function bchFormat(int $mask): int
    {
        $d = $mask;                                              // 纠错等级 M = 00
        $v = $d << 10;
        for ($i = 14; $i >= 10; $i--) {
            if (($v >> $i) & 1) {
                $v ^= 0x537 << ($i - 10);
            }
        }
        return (($d << 10) | $v) ^ 0x5412;
    }

    private static function writeFormat(array &$m, int $mask, int $n): void
    {
        $f = self::bchFormat($mask);
        for ($i = 0; $i < 15; $i++) {
            $bit = (($f >> $i) & 1) === 1;
            // 竖排：第 8 列，先上（0..5）、再跳 6 走 7/8、最后下段
            if ($i < 6) {
                $m[$i][8] = $bit;
            } elseif ($i < 8) {
                $m[$i + 1][8] = $bit;
            } else {
                $m[$n - 15 + $i][8] = $bit;
            }
            // 横排：第 8 行，先右段（n-1..n-8）、再跳 6 走 7、最后左段
            if ($i < 8) {
                $m[8][$n - 1 - $i] = $bit;
            } elseif ($i === 8) {
                $m[8][7] = $bit;
            } else {
                $m[8][15 - $i - 1] = $bit;
            }
        }
        $m[$n - 8][8] = true;                                    // 固定深色模块
    }

    private static function writeVersion(array &$m, int $version, int $n): void
    {
        $v = $version << 12;
        for ($i = 17; $i >= 12; $i--) {
            if (($v >> $i) & 1) {
                $v ^= 0x1F25 << ($i - 12);
            }
        }
        $info = ($version << 12) | $v;
        for ($i = 0; $i < 18; $i++) {
            $bit = (($info >> $i) & 1) === 1;
            $m[intdiv($i, 3)][$n - 11 + ($i % 3)] = $bit;
            $m[$n - 11 + ($i % 3)][intdiv($i, 3)] = $bit;
        }
    }

    /** ISO/IEC 18004 罚分规则 N1–N4，取分最低的掩码 */
    private static function penalty(array $m, int $n): int
    {
        $score = 0;

        // N1：行/列上连续同色 >= 5 个 → 3 + (len - 5)
        for ($r = 0; $r < $n; $r++) {
            $run = 1;
            for ($c = 1; $c < $n; $c++) {
                if ($m[$r][$c] === $m[$r][$c - 1]) {
                    $run++;
                } else {
                    if ($run >= 5) {
                        $score += 3 + ($run - 5);
                    }
                    $run = 1;
                }
            }
            if ($run >= 5) {
                $score += 3 + ($run - 5);
            }
        }
        for ($c = 0; $c < $n; $c++) {
            $run = 1;
            for ($r = 1; $r < $n; $r++) {
                if ($m[$r][$c] === $m[$r - 1][$c]) {
                    $run++;
                } else {
                    if ($run >= 5) {
                        $score += 3 + ($run - 5);
                    }
                    $run = 1;
                }
            }
            if ($run >= 5) {
                $score += 3 + ($run - 5);
            }
        }

        // N2：2×2 同色块 → 3 分/块
        for ($r = 0; $r < $n - 1; $r++) {
            for ($c = 0; $c < $n - 1; $c++) {
                $v = $m[$r][$c];
                if ($v === $m[$r][$c + 1] && $v === $m[$r + 1][$c] && $v === $m[$r + 1][$c + 1]) {
                    $score += 3;
                }
            }
        }

        // N3：出现 1:1:3:1:1 伴随 4 个浅色模块 → 40 分/次
        $pat1 = [true, false, true, true, true, false, true, false, false, false, false];
        $pat2 = [false, false, false, false, true, false, true, true, true, false, true];
        $check = static function (array $line, int $n, array $pat1, array $pat2): int {
            $hit = 0;
            for ($i = 0; $i + 11 <= $n; $i++) {
                $ok1 = true;
                $ok2 = true;
                for ($j = 0; $j < 11; $j++) {
                    if ($line[$i + $j] !== $pat1[$j]) {
                        $ok1 = false;
                    }
                    if ($line[$i + $j] !== $pat2[$j]) {
                        $ok2 = false;
                    }
                }
                if ($ok1 || $ok2) {
                    $hit++;
                }
            }
            return $hit;
        };
        for ($r = 0; $r < $n; $r++) {
            $score += 40 * $check($m[$r], $n, $pat1, $pat2);
        }
        for ($c = 0; $c < $n; $c++) {
            $col = [];
            for ($r = 0; $r < $n; $r++) {
                $col[] = $m[$r][$c];
            }
            $score += 40 * $check($col, $n, $pat1, $pat2);
        }

        // N4：深色占比偏离 50% → 每 5% 扣 10 分
        $dark = 0;
        foreach ($m as $row) {
            foreach ($row as $v) {
                if ($v) {
                    $dark++;
                }
            }
        }
        $ratio = $dark * 100 / ($n * $n);
        $score += 10 * (int) (abs($ratio - 50) / 5);

        return $score;
    }

    private static function chunk(string $tag, string $data): string
    {
        return pack('N', strlen($data)) . $tag . $data
            . pack('N', crc32($tag . $data) & 0xFFFFFFFF);
    }
}
