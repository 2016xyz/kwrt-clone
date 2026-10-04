<?php
/**
 * Qr.php 自测：
 *   1. 导出 8 个强制掩码下的矩阵（供与 python-qrcode 逐格比对）
 *   2. 为每个用例生成 PNG，供真实解码器验证可扫性
 */
$cases = json_decode(file_get_contents('/tmp/qr_tests/cases.json'), true);
require '/tmp/qr_tests/Qr.php';

$out = [];
foreach ($cases as $i => $text) {
    $per = [];
    for ($mask = 0; $mask < 8; $mask++) {
        $rows = [];
        foreach (\Kwrt\Qr::matrix($text, $mask) as $row) {
            $rows[] = implode('', array_map(static fn($v) => $v ? '1' : '0', $row));
        }
        $per[] = implode("\n", $rows);
    }
    $auto = \Kwrt\Qr::matrix($text);
    $autoRows = [];
    foreach ($auto as $row) {
        $autoRows[] = implode('', array_map(static fn($v) => $v ? '1' : '0', $row));
    }
    $png = \Kwrt\Qr::png($text);
    file_put_contents("/tmp/qr_tests/php_{$i}.png", $png);
    $out[] = ['len' => strlen($text), 'size' => count($auto),
              'auto' => implode("\n", $autoRows), 'masks' => $per,
              'png_bytes' => strlen($png)];
}
file_put_contents('/tmp/qr_tests/php_out.json', json_encode($out));
echo "PHP 侧完成：" . count($cases) . " 个用例\n";
foreach ($out as $i => $o) {
    $len = $o['len'];
    $size = $o['size'];
    $bytes = $o['png_bytes'];
    echo "  用例{$i}: {$len} 字节 → 矩阵 {$size}x{$size}, PNG {$bytes} 字节\n";
}
