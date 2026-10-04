<?php
declare(strict_types=1);

/**
 * Archive::safeExtract 自测（Tar-Slip / Zip-Slip 防护回归）。
 *
 * 背景：Engine::injectDefaults() 原来是裸 `tar -xzf <用户上传包> -C files`，
 * 对归档条目零校验 —— 赞助用户上传一个含 `../../` 或符号链接成员的 tar.gz
 * 即可写到 ImageBuilder 之外（构建机任意文件写入）。本脚本把「必须拦截」
 * 与「必须放行」两侧都钉死，防止回退。
 *
 * 用法：php php/scripts/archive_selftest.php
 */

$SRC = dirname(__DIR__) . '/src';
require $SRC . '/helpers.php';
spl_autoload_register(static function (string $c) use ($SRC): void {
    if (!str_starts_with($c, 'Kwrt\\')) {
        return;
    }
    $f = $SRC . '/' . str_replace('\\', '/', substr($c, 5)) . '.php';
    if (is_file($f)) {
        require $f;
    }
});

use Kwrt\Archive;

$pass = 0;
$fail = 0;
function ok(string $title, bool $cond, string $note = ''): void
{
    global $pass, $fail;
    if ($cond) {
        $pass++;
        echo "  ✓ $title" . ($note !== '' ? "  $note" : '') . "\n";
    } else {
        $fail++;
        echo "  ✗ $title" . ($note !== '' ? "  $note" : '') . "\n";
    }
}

$base = sys_get_temp_dir() . '/kwrt-arc-' . bin2hex(random_bytes(4));
$work = $base . '/work';
$dest = $base . '/dest';
@mkdir($work, 0755, true);
@mkdir($dest, 0755, true);

echo "=" . str_repeat('=', 80) . "\n";
echo "Archive::safeExtract 自测\n";
echo "=" . str_repeat('=', 80) . "\n";

// ---------------------------------------------------------------- 名字判据
echo "\n[1] badName 判据\n";
foreach ([
    ['../x', true], ['/etc/passwd', true], ['a/../b', true],
    ['C:\\win', true], ['a\\..\\b', true], ['', true],
    ['etc/config/net', false], ['good/file.txt', false], ['a/./b', false],
] as [$n, $want]) {
    $got = Archive::badName($n);
    ok(sprintf('badName(%-16s) = %s', var_export($n, true), $got ? 'true' : 'false'),
        $got === $want);
}

// ---------------------------------------------------------------- tar 用例
echo "\n[2] tar.gz 解压\n";
$run = static function (array $argv): array {
    $p = proc_open($argv, [0 => ['file', '/dev/null', 'r'], 1 => ['pipe', 'w'], 2 => ['pipe', 'w']], $pipes);
    $o = is_resource($p) ? (string) stream_get_contents($pipes[1]) : '';
    $e = is_resource($p) ? (string) stream_get_contents($pipes[2]) : '';
    $c = is_resource($p) ? proc_close($p) : -1;
    return [$c, $o, $e];
};

file_put_contents($work . '/evil.txt', 'pwned');
file_put_contents($work . '/good.txt', 'ok');

// (a) 合法包
[$c] = $run(['tar', '-czf', $base . '/ok.tar.gz', '-C', $work, 'good.txt']);
ok('构造合法 tar.gz', $c === 0);
$threw = '';
try {
    Archive::safeExtract($base . '/ok.tar.gz', $dest);
} catch (\Throwable $e) {
    $threw = $e->getMessage();
}
ok('合法 tar.gz 放行并落盘', $threw === '' && is_file($dest . '/good.txt'), $threw);

// (b) 含 ../ 穿越
[$c] = $run(['tar', '-czf', $base . '/slip.tar.gz',
    '--transform=s|^evil.txt|../../evil.txt|', '-C', $work, 'evil.txt']);
ok('构造含 ../ 的 tar.gz', $c === 0);
$threw = '';
try {
    Archive::safeExtract($base . '/slip.tar.gz', $base . '/dest2/');
} catch (\Throwable $e) {
    $threw = $e->getMessage();
}
ok('含 ../ 穿越 → 拒绝', $threw !== '' && str_contains($threw, '越界'),
    $threw !== '' ? $threw : '没有抛错！');

// (c) 绝对路径
[$c] = $run(['tar', '-czf', $base . '/abs.tar.gz',
    '--transform=s|^evil.txt|/tmp/kwrt_abs_evil.txt|', '-C', $work, 'evil.txt']);
$threw = '';
try {
    Archive::safeExtract($base . '/abs.tar.gz', $dest);
} catch (\Throwable $e) {
    $threw = $e->getMessage();
}
ok('含绝对路径 → 拒绝', $threw !== '', $threw !== '' ? $threw : '没有抛错！');
ok('绝对路径未被写出', !is_file('/tmp/kwrt_abs_evil.txt'));
@unlink('/tmp/kwrt_abs_evil.txt');

// (d) 符号链接指向外部
@symlink('/etc', $work . '/esclink');
[$c] = $run(['tar', '-czf', $base . '/sym.tar.gz', '-C', $work, 'esclink']);
$threw = '';
try {
    Archive::safeExtract($base . '/sym.tar.gz', $dest);
} catch (\Throwable $e) {
    $threw = $e->getMessage();
}
ok('含符号链接成员 → 拒绝', $threw !== '', $threw !== '' ? $threw : '没有抛错！');

// (e) tgz 扩展名同样受管
copy($base . '/slip.tar.gz', $base . '/slip.tgz');
$threw = '';
try {
    Archive::safeExtract($base . '/slip.tgz', $dest);
} catch (\Throwable $e) {
    $threw = $e->getMessage();
}
ok('tgz 后缀同样拦截穿越', $threw !== '');

// ---------------------------------------------------------------- zip 用例
echo "\n[3] zip 解压\n";
if (class_exists('ZipArchive') || class_exists('PharData')) {
    $z = $base . '/slip.zip';
    if (class_exists('ZipArchive')) {
        $zz = new ZipArchive();
        $zz->open($z, ZipArchive::CREATE | ZipArchive::OVERWRITE);
        $zz->addFromString('../../zip_evil.txt', 'x');
        $zz->addFromString('inner/keep.txt', 'y');
        $zz->close();
    } else {
        $ph = new PharData($z);
        $ph->addFromString('inner/keep.txt', 'y');
    }
    $d3 = $base . '/dest3';
    @mkdir($d3, 0755, true);
    $threw = '';
    try {
        Archive::safeExtract($z, $d3);
    } catch (\Throwable $e) {
        $threw = $e->getMessage();
    }
    // zip 路径的策略是「丢弃越界条目，其余照常解」—— 关键断言是没写出界
    $escaped = is_file($base . '/zip_evil.txt') || is_file($base . '/../zip_evil.txt')
        || is_file(sys_get_temp_dir() . '/zip_evil.txt');
    ok('zip 越界条目未写出', !$escaped, $threw !== '' ? "抛错: $threw" : '');
    ok('zip 合法条目正常解出', is_file($d3 . '/inner/keep.txt'));
} else {
    ok('跳过 zip（无 zip/phar 扩展）', true);
}

// ---------------------------------------------------------------- 7z
echo "\n[4] 7z\n";
file_put_contents($base . '/x.7z', "7z\xbc\xaf\x27\x1c" . str_repeat("\0", 32));
$threw = '';
try {
    Archive::safeExtract($base . '/x.7z', $dest);
} catch (\Throwable $e) {
    $threw = $e->getMessage();
}
if (Archive::sevenzipAvailable()) {
    ok('本机有 7z：坏包必须报错而不是静默', $threw !== '', $threw);
} else {
    ok('未装 7z：明确报错（非静默跳过）',
        $threw !== '' && str_contains($threw, '未安装 7z'), $threw);
}

// ---------------------------------------------------------------- 清理
foreach (['/ok.tar.gz', '/slip.tar.gz', '/slip.tgz', '/abs.tar.gz', '/sym.tar.gz',
          '/slip.zip', '/x.7z'] as $f) {
    @unlink($base . $f);
}
@unlink($work . '/esclink');
exec('rm -rf ' . escapeshellarg($base));

echo "=" . str_repeat('=', 80) . "\n";
echo "合计 " . ($pass + $fail) . " 项，全部通过 " . ($fail === 0 ? '✓' : "✗ 失败 $fail 项") . "\n";
exit($fail === 0 ? 0 : 1);
