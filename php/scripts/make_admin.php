<?php
/**
 * 测试脚手架：创建/重置一个管理员账号。
 *
 * ★ 本脚本会 **DELETE 后重建** 指定用户 —— 它是一个"销毁性"脚本，不是"创建"脚本。
 *
 * 血的教训（真事）：有人（对，就是给我写这行注释的那个人）在服务器上顺手
 * `php php/scripts/make_admin.php` 不带参数跑了一下，本脚本默认参数是
 * `admin` / `AdminPass123`，且默认连的就是**生产库** —— 结果生产管理员账号被
 * 当场删除重建：口令变成 AdminPass123，email / quota / sponsor_until /
 * sponsor_tier / sponsor_amount 全部被覆盖成初始值，站主要用自己的密码登不进去了。
 *
 * 所以现在加了三道闸，任何一道不满足就直接退出，绝不落到 Db 上：
 *   1. 必须显式给出用户名与口令（不再有"默认管理员口令"这种危险默认值）；
 *   2. 必须显式设置环境变量 KWRT_DB 指向目标库 —— **不允许**默认落到生产库；
 *   3. 目标库路径里出现生产库特征（users.db 且非 test/tmp 目录）时，要求
 *      额外加上 --i-know-this-is-destructive 才放行。
 *
 * 用法：
 *   KWRT_DB=/path/to/test.db php php/scripts/make_admin.php <用户名> <密码>
 *   KWRT_DB=/tmp/x.db php php/scripts/make_admin.php tester pw123 --i-know-this-is-destructive
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

$args = array_values(array_filter($argv, static fn($a, $i) => $i > 0, ARRAY_FILTER_USE_BOTH));
$force = in_array('--i-know-this-is-destructive', $args, true);
$args = array_values(array_filter($args, static fn($a) => $a !== '--i-know-this-is-destructive'));

$user = (string) ($args[0] ?? '');
$pass = (string) ($args[1] ?? '');

// 闸 1：不允许"默认管理员口令"
if ($user === '' || $pass === '') {
    fwrite(STDERR, "拒绝执行：必须显式给出用户名与口令。\n"
        . "  KWRT_DB=/path/to/test.db php php/scripts/make_admin.php <用户名> <密码>\n");
    exit(2);
}

// 闸 2：不允许默认落到生产库
$dbEnv = getenv('KWRT_DB');
if ($dbEnv === false || trim((string) $dbEnv) === '') {
    fwrite(STDERR, "拒绝执行：未设置 KWRT_DB。\n"
        . "  本脚本会先 DELETE 再 INSERT 目标用户，绝不能对生产库隐式运行。\n"
        . "  请显式指定一个测试库，例如：\n"
        . "    KWRT_DB=/tmp/kwrt-test.db php php/scripts/make_admin.php tester pw123\n");
    exit(2);
}

// 闸 3：看着像生产库就再要一次确认
$looksProd = (bool) preg_match('#(^|/)users\.db$#', (string) $dbEnv)
    && !preg_match('#(^|/)(tmp|test|tests|fixtures)/#', (string) $dbEnv);
if ($looksProd && !$force) {
    fwrite(STDERR, "拒绝执行：KWRT_DB 看起来是**生产库**（{$dbEnv}）。\n"
        . "  这会删除并重建用户「{$user}」，口令、邮箱、赞助状态全部被重置。\n"
        . "  确实要这么做，请追加 --i-know-this-is-destructive。\n");
    exit(2);
}

Kwrt\Db::pdo();
Kwrt\Db::run('DELETE FROM users WHERE username=?', [$user]);
Kwrt\Db::run(
    'INSERT INTO users(username, password, email, created, role, sponsor, disabled, quota, email_verified) '
    . 'VALUES(?,?,?,?,?,1,0,50,1)',
    [$user, Kwrt\Auth::hashPw($pass), $user . '@example.com', microtime(true), 'admin']
);
echo "已创建管理员 {$user} / {$pass}（库：{$dbEnv}）\n";
