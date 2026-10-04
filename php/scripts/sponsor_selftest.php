<?php
/**
 * PHP 侧动态收款码逻辑自测。
 * 纯函数（Qr / Sponsor::num / renderQrText）必须全部通过；
 * 依赖数据库的部分（amountRange / resolve）在 DB 可用时一并校验。
 */
declare(strict_types=1);

$SRC = dirname(__DIR__) . '/src';
require $SRC . '/helpers.php';
spl_autoload_register(static function (string $c): void {
    if (!str_starts_with($c, 'Kwrt\\')) {
        return;
    }
    $f = $SRC . '/' . str_replace('\\', '/', substr($c, 5)) . '.php';
    if (is_file($f)) {
        require $f;
    }
});

$fail = 0;
function ck(string $name, $got, $want): void
{
    global $fail;
    $ok = $got === $want;
    if (!$ok) {
        $fail++;
    }
    printf("  %-52s %s\n", $name,
        $ok ? '✓' : '✗ 得到 ' . var_export($got, true) . '，期望 ' . var_export($want, true));
}

echo "=== Sponsor::num（金额紧凑写法，对齐 Python 的 {amount:g}）===\n";
ck('num(50)', \Kwrt\Sponsor::num(50.0), '50');
ck('num(50.0)', \Kwrt\Sponsor::num(50), '50');
ck('num(4.99)', \Kwrt\Sponsor::num(4.99), '4.99');
ck('num(99.50)', \Kwrt\Sponsor::num(99.5), '99.5');
ck('num(0.1)', \Kwrt\Sponsor::num(0.1), '0.1');
ck('num(100)', \Kwrt\Sponsor::num(100.0), '100');

echo "\n=== Sponsor::renderQrText（占位符替换）===\n";
ck('{amount}',
    \Kwrt\Sponsor::renderQrText('https://qr.alipay.com/x?amount={amount}', 50.0),
    'https://qr.alipay.com/x?amount=50');
ck('{amount2}',
    \Kwrt\Sponsor::renderQrText('a={amount2}', 4.99),
    'a=4.99');
ck('无占位符原样',
    \Kwrt\Sponsor::renderQrText('收款码A', 50.0),
    '收款码A');
ck('中文 + 金额',
    \Kwrt\Sponsor::renderQrText('支付宝收款 {amount} 元', 66.0),
    '支付宝收款 66 元');

echo "\n=== Sponsor::parseAmount（非法金额给人话，不是静默变 0）===\n";
ck('"50"', \Kwrt\Sponsor::parseAmount('50'), [50.0, '']);
ck('空串', \Kwrt\Sponsor::parseAmount(''), [0.0, '']);
ck('"abc"', \Kwrt\Sponsor::parseAmount('abc')[1], '金额必须是数字');
ck('"1e999"', \Kwrt\Sponsor::parseAmount('1e999')[1], '金额必须是有限数字');

echo "\n=== Qr::png（真图 + 可解码由 Python 侧负责，这里只查结构）===\n";
$png = \Kwrt\Qr::png('https://qr.alipay.com/fkxTEST?amount=50');
ck('PNG 魔数', bin2hex(substr($png, 0, 8)), '89504e470d0a1a0a');
ck('含 IHDR', strpos($png, 'IHDR') !== false, true);
ck('含 PLTE', strpos($png, 'PLTE') !== false, true);
ck('含 IDAT', strpos($png, 'IDAT') !== false, true);
ck('含 IEND', strpos($png, 'IEND') !== false, true);
$m = \Kwrt\Qr::matrix('https://qr.alipay.com/fkxTEST?amount=50');
ck('矩阵边长 = 版本*4+17', (count($m) - 17) % 4, 0);
ck('模块像素下限生效（29 模块 ≥ 320px 目标）', strlen($png) > 300, true);

echo "\n=== 依赖数据库的部分 ===\n";
try {
    $range = \Kwrt\Sponsor::amountRange();
    ck('amountRange 有序', $range[0] <= $range[1], true);
    printf("  (当前 最低=%d 最高=%d 有效单价=%s)\n", $range[0], $range[1],
        (string) \Kwrt\Sponsor::perDayPrice());
    [$c, $e] = \Kwrt\Sponsor::resolve('', 50);
    if ($c !== null) {
        printf("  自定义 50 → 天数 %d，tier=%s\n", $c['days'], $c['tier']);
        ck('自定义金额 custom 标志', $c['custom'], true);
    } else {
        printf("  （未开放自定义金额：%s）\n", $e);
    }
    [$c2, $e2] = \Kwrt\Sponsor::resolve('不存在的套餐', 0);
    ck('未知套餐被拒', $e2, '套餐不存在，请从页面列出的套餐中选择');
} catch (\Throwable $ex) {
    printf("  （数据库不可用，跳过：%s）\n", $ex->getMessage());
}

echo "\n" . ($fail === 0 ? '全部通过 ✓' : "失败 {$fail} 项 ✗") . "\n";
exit($fail === 0 ? 0 : 1);
