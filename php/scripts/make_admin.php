<?php
/**
 * 测试脚手架：创建/重置一个管理员账号（仅测试用，不要在生产跑）。
 *
 * 用法：KWRT_DB=/path/to/test.db php php/scripts/make_admin.php <用户名> <密码>
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

$user = $argv[1] ?? 'admin';
$pass = $argv[2] ?? 'AdminPass123';

Kwrt\Db::pdo();
Kwrt\Db::run('DELETE FROM users WHERE username=?', [$user]);
Kwrt\Db::run(
    'INSERT INTO users(username, password, email, created, role, sponsor, disabled, quota, email_verified) '
    . 'VALUES(?,?,?,?,?,1,0,50,1)',
    [$user, Kwrt\Auth::hashPw($pass), $user . '@example.com', microtime(true), 'admin']
);
echo "已创建管理员 {$user} / {$pass}\n";
