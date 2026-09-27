<?php
/**
 * 构建 worker：由 Builder::pump() 用 nohup 起来的**独立进程**，负责真正跑 ImageBuilder。
 *
 * 为什么是独立进程：PHP 是「每请求一进程」，请求结束就退出，
 * 没法在请求里跑一个几分钟的编译。所以设计成两段：
 *   · 提交请求：只入队 + 抢槽（毫秒级返回）
 *   · 本脚本：在后台真正编译，把状态写回 jobs 表
 * 这样多 worker / 多机部署下队列也是正确的（比 Python 版的进程内队列更强）。
 *
 * 用法：php build-worker.php <request_hash>
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


if (PHP_SAPI !== 'cli') {
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

$hash = $argv[1] ?? '';
if (!preg_match(Kwrt\Util::HASH_RE, $hash)) {
    fwrite(STDERR, "request_hash 非法\n");
    exit(2);
}

$job = Kwrt\Builder::job($hash);
if (!$job) {
    fwrite(STDERR, "任务不存在: {$hash}\n");
    exit(3);
}
$payload = json_decode((string) $job['payload'], true) ?: [];

/** 写回进度。 */
$progress = static function (string $status, string $detail) use ($hash): void {
    Kwrt\Db::run('UPDATE jobs SET status=?, detail=?, updated=? WHERE request_hash=?',
        [$status, mb_substr($detail, 0, 900), microtime(true), $hash]);
};

/** 失败收尾：写失败状态 + 按 mail.notify_fail 决定是否发信。 */
$failWith = static function (string $err) use ($hash, $payload): void {
    Kwrt\Builder::fail($hash, $err);
    Kwrt\Notifier::buildFail($hash, $payload, $err);
};

$backend = strtolower((string) Kwrt\Settings::get('builder.backend', 'local'));

// ------------------------------------------------------------------ 远端后端
// 派发到 GitHub Actions：本站不出 CPU/磁盘，只负责轮询与产物回传。
if ($backend === 'github') {
    if (!is_file(dirname(__DIR__) . '/src/Github.php')) {
        $failWith('远端构建模块缺失（php/src/Github.php）');
        exit(5);
    }
    try {
        $res = Kwrt\Github::dispatch($hash, $payload, $progress);
    } catch (Throwable $e) {
        $failWith(get_class($e) . ': ' . $e->getMessage());
        exit(6);
    }
    if (($res['status'] ?? '') === 'done') {
        $info = Kwrt\Notifier::buildDone($hash, $payload, (array) ($res['files'] ?? []),
                                          (float) ($res['duration'] ?? 0));
        $n = count($info['links']);
        $progress('done', "构建完成，共 {$n} 个文件" . ($info['ttl_hours'] > 0
            ? "（下载链接 {$info['ttl_hours']} 小时内有效）" : '（产物托管于 GitHub）'));
        if (!empty($res['gh_run_url'])) {
            $progress('done', "构建完成，共 {$n} 个文件；GitHub run: " . $res['gh_run_url']);
        }
        exit(0);
    }
    $err = trim((string) ($res['stderr'] ?? ''));
    $failWith($err !== '' ? $err : 'GitHub 构建失败');
    exit(7);
}

// ------------------------------------------------------------------ 本地后端
// 磁盘检查要在真正开跑之前做 —— 构建工作目录约 2 GB（远端后端不需要本站磁盘）
$need = 2 * 1024 * 1024 * 1024 + 600 * 1024 * 1024;
$free = (int) (@disk_free_space(Kwrt\Config::root()) ?: 0);
if ($free < $need) {
    $failWith('磁盘空间不足：需要约 2.6 GB，当前可用 ' . bytes_h($free));
    exit(4);
}

$engine = dirname(__DIR__) . '/src/Engine.php';
if (!is_file($engine)) {
    $failWith('构建引擎缺失（php/src/Engine.php）');
    exit(5);
}

require $engine;
try {
    (new Kwrt\Engine($hash, $payload, $progress))->run();
} catch (Throwable $e) {
    $failWith(get_class($e) . ': ' . $e->getMessage());
    exit(6);
}
