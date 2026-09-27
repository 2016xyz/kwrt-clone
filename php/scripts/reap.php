<?php
/**
 * 回收「僵尸构建任务」：单独的命令行入口。
 *
 * 它解决什么
 * ----------
 * 构建 worker 是 Builder::spawnWorker() 用 nohup 起的独立进程。它可能
 * 压根没起来、起来后立刻崩、或被 OOM killer 干掉 —— 无论哪种，jobs.status
 * 都会永远留在 running，把并发槽位吃掉。槽位耗尽后队列永久不动，
 * 而用户看到的是「已排队，等待空闲槽」一直不变。
 *
 * 通常不需要跑它
 * --------------
 * Builder::pump() 在每次提交构建时都会先调用 Builder::reap()，
 * 所以「有僵尸 → 下次提交先清掉再抢槽」，用户不会被卡住。这是刻意设计：
 * 空闲时没人提交，也就没人需要槽位，不必白白轮询数据库。
 *
 * 这个脚本是给「想要一层额外保险」的场景用的 —— 例如希望僵尸在后台页面里
 * 尽快显示成失败，而不是等到下一次有人提交构建。配一条低频 cron 即可：
 *
 *     cd /www/wwwroot/kwrt && KWRT_DB_DRIVER=mysql KWRT_MYSQL_DB=kwrt \
 *       KWRT_MYSQL_USER=kwrt KWRT_MYSQL_PASS='口令' \
 *       php php/scripts/reap.php >/dev/null 2>&1
 *
 * cron 的分钟位写「步进 10」即可（每 10 分钟一次）；
 * 注意本文件注释里不能出现那三个字符的组合，它会提前闭合注释块 —— 这是踩过的坑。
 *
 * 注意 CLI 下必须带与 Web 一致的数据库变量，否则 CLI 连 SQLite、Web 连 MySQL，
 * 这个脚本就白跑了（它回收的是另一个库里的任务）。
 *
 * 阈值
 * ----
 * 默认 3 小时 = Engine 里 make 的上限 2 小时 + 1 小时余量。
 * 可用 KWRT_STALE_RUNNING_SECONDS 覆盖（慢机器放宽，例如 14400）。
 * 不要设得比最长构建时间还短 —— 那会把正常构建误杀。
 *
 * 退出码：0 成功 / 1 失败（连不上库等）。配合 cron 的 MAILTO 或监控即可发现。
 */
declare(strict_types=1);

/**
 * PHP 版本前置守卫（必须最先执行，且本身只能用 PHP 5/7 也能解析的语法写）。
 *
 * 为什么必须放在最前：本文件与 php/src 用到 PHP 8.0 的**语法** ——
 * `$obj::class`、非捕获式 `catch (Throwable)`、联合类型参数、构造器属性提升。
 * 这些是**编译期**错误：PHP 7.x 下整个文件根本编译不过，
 * 写在后面的任何检查都不会执行，用户只会看到
 *     Fatal error: Dynamic class names are not allowed in compile-time ::class fetch
 * 这种看不懂的提示（真实现场：宝塔部署后整站白屏，只有这一行）。
 * 所以守卫必须用 PHP 5/7 能解析的语法、放在文件最前面 —— 它唯一的工作
 * 就是把「版本不对」变成一句人能看懂、能照着做的话。
 */
if (PHP_VERSION_ID < 80000) {
    if (!headers_sent()) {
        header('Content-Type: text/plain; charset=utf-8', true, 500);
    }
    echo "Kwrt(OpenWrt) 固件站 · PHP 版启动失败\n"
       . "=====================================\n\n"
       . "原因：需要 PHP 8.0 或更高版本，当前是 " . PHP_VERSION . "。\n"
       . "（代码里用了 PHP 8.0 语法，低版本会在编译阶段直接失败，\n"
       . "  报错形如「Dynamic class names are not allowed in compile-time ::class fetch」。）\n\n"
       . "怎么修 ——\n"
       . "  宝塔面板：网站 → 你的站点 → 设置 → 「PHP 版本」选 8.0 / 8.1 / 8.2 → 保存\n"
       . "  可能还需要：软件商店 → 安装对应 PHP 版本，并装扩展 pdo_mysql(或 pdo_sqlite)、\n"
       . "              curl、openssl、mbstring、fileinfo、sockets\n"
       . "  完成后重载 PHP-FPM（PHP → 设置 → 重载配置）\n\n"
       . "  命令行确认：php -v    应显示 8.0 以上\n"
       . "  自检页面：  访问 /install.php 或 /healthz\n\n"
       . "注意：PHP 7.4 已于 2022-11 停止安全支持，不建议降级代码去适配它。\n";
    exit(1);
}


// 只允许命令行运行：这是个运维入口，绝不能让 Web 打到它。
if (PHP_SAPI !== 'cli') {
    http_response_code(404);
    exit("仅限命令行运行\n");
}

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

$quiet = in_array('--quiet', $argv, true) || in_array('-q', $argv, true);

try {
    Kwrt\Db::pdo();                       // 顺带把表建好（幂等）
    $n = Kwrt\Builder::reap();
    if (!$quiet) {
        printf("[reap] 驱动=%s  回收僵尸任务 %d 个  阈值=%d 秒\n",
            Kwrt\Db::driver(), $n, Kwrt\Builder::reapThreshold());
    }
    exit(0);
} catch (Throwable $e) {
    fwrite(STDERR, '[reap] 失败: ' . get_class($e) . ': ' . $e->getMessage() . "\n");
    exit(1);
}