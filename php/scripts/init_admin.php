<?php
/**
 * 初始化首个管理员（幂等：已存在管理员则不动）。
 *
 * 用法：
 *     KWRT_ADMIN_PASSWORD='强密码' php php/scripts/init_admin.php
 *     php php/scripts/init_admin.php            # 未设环境变量则随机生成并打印一次
 */
declare(strict_types=1);

spl_autoload_register(static function (string $c): void {
    if (!str_starts_with($c, 'Kwrt\\')) {
        return;
    }
    $f = dirname(__DIR__) . '/src/' . str_replace('\\', '/', substr($c, 5)) . '.php';
    if (is_file($f)) {
        require $f;
    }
});
require dirname(__DIR__) . '/src/helpers.php';

Kwrt\Db::pdo();

$n = (int) Kwrt\Db::val("SELECT COUNT(*) FROM users WHERE role='admin'", [], 0);
if ($n > 0) {
    echo "已存在 {$n} 个管理员，不做改动（要重置请用 make_admin.php）。\n";
    exit(0);
}

$pw = (string) (getenv('KWRT_ADMIN_PASSWORD') ?: '');
$generated = false;
if ($pw === '') {
    $pw = bin2hex(random_bytes(8));
    $generated = true;
}
if (strlen($pw) < 8) {
    fwrite(STDERR, "密码至少 8 位\n");
    exit(1);
}

Kwrt\Db::run(
    'INSERT INTO users(username, password, email, created, role, sponsor, disabled, quota, email_verified) '
    . 'VALUES(?,?,?,?,?,1,0,50,1)',
    ['admin', Kwrt\Auth::hashPw($pw), '', microtime(true), 'admin']
);

$file = Kwrt\Config::path('data', 'INITIAL_ADMIN.txt');
@mkdir(dirname($file), 0755, true);
file_put_contents($file, "管理员账号 admin\n密码  : {$pw}\n生成时间: " . date('Y-m-d H:i:s') . "\n");
@chmod($file, 0600);

echo "已创建管理员 admin\n";
if ($generated) {
    echo "随机密码: {$pw}\n";
}
echo "已写入 {$file}（权限 600）—— 登录后请立即修改。\n";
