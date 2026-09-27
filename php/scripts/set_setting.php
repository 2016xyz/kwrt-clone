<?php
/**
 * 命令行设置一个站点设置项：php php/scripts/set_setting.php <key> <value>
 *
 * 为什么单独成一个文件：用 `php -r '...'` 传递带命名空间的调用需要多层反斜杠转义，
 * 极易写错且失败时是静默的（测试会得出「开关无效」的错误结论）。放进文件最省事。
 */
declare(strict_types=1);

require __DIR__ . '/../src/helpers.php';

spl_autoload_register(static function (string $c): void {
    if (!str_starts_with($c, 'Kwrt\\')) {
        return;
    }
    $f = __DIR__ . '/../src/' . str_replace('\\', '/', substr($c, 5)) . '.php';
    if (is_file($f)) {
        require $f;
    }
});

$key = $argv[1] ?? '';
$val = $argv[2] ?? '';
if ($key === '') {
    fwrite(STDERR, "用法: php set_setting.php <key> <value>\n");
    exit(2);
}
Kwrt\Settings::set($key, $val);
$now = Kwrt\Settings::get($key);
fwrite(STDOUT, $key . ' = ' . var_export($now, true) . "\n");
