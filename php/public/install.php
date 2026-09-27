<?php
/**
 * 安装向导（网页版）。
 *
 * 为什么要有它：原先 PHP 版装完代码还得SSH 进去跑 `php php/scripts/init_admin.php`
 * 才能登录，宝塔类面板用户并不总是有终端习惯；数据库配错时 index.php 只会
 * 抛一句「数据库初始化失败」，不告诉你哪一步错了。这个向导把
 * 「环境检查 → 数据库 → 管理员 → 站点设置」四步搬到浏览器里，
 * 每一步失败都给出**能照着做**的原因。
 *
 * 安全上的三条硬要求（都不是可选项）：
 *   ① 装完写 data/install.lock，向导自身从此拒绝再跑 ——
 *      否则任何人访问 /install.php 就能重建管理员，等于后门。
 *   ② 若库连通且**已存在管理员**，即使没有锁文件也一律拦截 ——
 *      光靠锁文件挡不住「删掉锁文件再来一次」这种接管手法。
 *      确有重装需求时，需在服务器上放 data/install.allow（证明有文件系统权限）。
 *   ③ 所有写操作都要 CSRF token；数据库口令在页面上永不回显。
 *
 * 本文件不依赖 index.php 的引导链 —— 数据库坏掉时它必须仍能打开，
 * 否则「装坏了就再也进不去向导」。
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


// 布局探测：不再写死 dirname(__DIR__, 2)。
// 写死的话，docroot 一旦不是 <R>/php/public（宝塔上很常见），就会去仓库根的
// 上一级找 php/src —— 用户只看到
//    Failed opening required '/www/wwwroot/php/src/helpers.php'
// 这种看不懂的 fatal。_boot.php 认得出常见几种布局，认不出来会给人话。
require __DIR__ . '/_boot.php';
$ROOT = KWRT_ROOT;

/**
 * 设置环境变量 —— 但**不能假设 putenv 可用**。
 *
 * 宝塔默认把 putenv 写进 disable_functions。被禁用的函数在 PHP 里等同
 * 「未定义」，直接调用会抛致命错误：
 *
 *     Uncaught Error: Call to undefined function putenv()
 *     in public/install.php on line 389
 *
 * 用户看到的就是一个没有任何信息的 500 —— 真实现场（op.2016xlx.cn，
 * PHP 8.1 + 宝塔，向导第 2 步选 MySQL 提交后必 500）。
 *
 * 关键认识：环境变量只是「让**本次请求**立刻用上新配置」的捷径。
 * 配置本身已经写进 config.local.json，而 Config::dbDriver() / Config::mysql()
 * 都设计成会回退去读那个文件。所以 putenv 不可用时**跳过即可** ——
 * 安装不该因为一个可选优化而中断。
 *
 * 注：函数被 disable_functions 禁用时，function_exists() 返回 false，
 * 所以这一个判断就够了。
 */
function kwrt_setenv(string $key, string $value): void
{
    if (function_exists('putenv')) {
        @putenv($key . '=' . $value);
    }
}

/**
 * 致命错误兜底：把「光秃秃的 500」变成能照着做的话。
 *
 * 为什么需要：向导自己在第 2 步选 MySQL 后建表失败时，本来会走 catch 把
 * 「建表失败：…」渲染出来；但如果**渲染那一页**又撞上致命错误（比如 MySQL
 * 拒绝 TEXT DEFAULT / TEXT 进索引，而错误页自身还要读设置、读模板），
 * 用户看到的就是一个没有任何信息的 500 —— 真实现场。
 * 有了这个兜底，任何未捕获的致命错误都会附上：错误内容、出错文件与行号、
 * 以及「把这一段贴给运维 / 提 issue」的指引。
 */
register_shutdown_function(static function (): void {
    $e = error_get_last();
    if (!$e || !in_array($e['type'], [E_ERROR, E_PARSE, E_CORE_ERROR, E_COMPILE_ERROR], true)) {
        return;
    }
    if (headers_sent()) {
        return;
    }
    http_response_code(500);
    header('Content-Type: text/plain; charset=utf-8');
    echo "Kwrt(OpenWrt) 固件站 · 安装向导遇到致命错误\n"
       . "=====================================\n\n"
       . "错误：" . $e['message'] . "\n"
       . "位置：" . $e['file'] . " 第 " . $e['line'] . " 行\n\n"
       . "怎么排查 ——\n"
       . "  · 如果是数据库相关（含 SQLSTATE / 1064 / 1101 / 1170 之类）：\n"
       . "    多数是 MySQL 版本差异。请把上面的完整错误行贴给运维或提 issue，\n"
       . "    并附上 MySQL 版本（面板里能看到）。\n"
       . "  · 如果是「找不到 …php」：上传的文件不全，或运行目录设错 ——\n"
       . "    访问 /install.php 会给出期望的目录结构。\n"
       . "  · 想看更详细的报错：站点设置里把 PHP 的 display_errors 打开，\n"
       . "    或查看 PHP 错误日志（宝塔：网站 → 设置 → 日志）。\n\n"
       . "数据库当前配置：驱动=" . (string) (getenv('KWRT_DB_DRIVER') ?: 'sqlite')
       . "，配置文件=" . (is_file($GLOBALS['CFG'] ?? '') ? ($GLOBALS['CFG'] ?? '') : '未写入') . "\n";
});

require KWRT_SRC . '/helpers.php';
spl_autoload_register(static function (string $class): void {
    if (!str_starts_with($class, 'Kwrt\\')) {
        return;
    }
    $f = KWRT_SRC . '/' . str_replace('\\', '/', substr($class, 5)) . '.php';
    if (is_file($f)) {
        require $f;
    }
});

@ini_set('display_errors', '0');
error_reporting(E_ALL & ~E_DEPRECATED);

$LOCK   = $ROOT . '/data/install.lock';
$ALLOW  = $ROOT . '/data/install.allow';
$CFG    = $ROOT . '/config.local.json';

if (session_status() !== PHP_SESSION_ACTIVE) {
    @session_start();
}
$S = &$_SESSION['kwrt_install'];
if (!is_array($S)) {
    $S = ['step' => 1, 'db' => [], 'site' => []];
}

// ------------------------------------------------------------------ 状态判定

/**
 * 站点是否已安装。
 *
 * 返回 fresh | installed | live_unlocked
 *   fresh         全新，可以装
 *   installed     有锁文件，向导拒绝运行
 *   live_unlocked 没锁文件但库里已有管理员 —— 疑似删锁重装，必须拦
 */
function kwrt_state(string $LOCK, string $ALLOW): array
{
    if (is_file($LOCK)) {
        $j = json_decode((string) @file_get_contents($LOCK), true);
        return ['installed', is_array($j) ? $j : []];
    }
    if (is_file($ALLOW)) {
        return ['fresh', ['forced' => true]];   // 管理员显式放行
    }
    try {
        \Kwrt\Db::pdo();
        $n = (int) \Kwrt\Db::val("SELECT COUNT(*) FROM users WHERE role='admin'");
        if ($n > 0) {
            return ['live_unlocked', ['admins' => $n]];
        }
    } catch (\Throwable $e) {
        // 库都连不上，那肯定是新装或配错了 —— 交给向导处理
        unset($e);
    }
    return ['fresh', []];
}

[$STATE, $STATE_INFO] = kwrt_state($LOCK, $ALLOW);

/**
 * 安装是否正在进行中。
 *
 * 为什么需要这个：live_unlocked 守卫（防「删掉锁文件重装接管」）是在第 3 步
 * 建出管理员之后就会成立的 —— 于是它会把**正在安装的自己**拦死，
 * 卡在第 3 步再也走不下去。会话里已有 step>=3 就是「本轮安装进行中」的凭据。
 */
$INSTALL_IN_FLIGHT = (int) ($S['step'] ?? 1) >= 3;

// ------------------------------------------------------------------ CSRF

if (empty($_SESSION['kwrt_install_csrf'])) {
    $_SESSION['kwrt_install_csrf'] = bin2hex(random_bytes(16));
}
$CSRF = (string) $_SESSION['kwrt_install_csrf'];
function csrf_ok(string $csrf): bool
{
    return $csrf !== '' && hash_equals((string) ($_SESSION['kwrt_install_csrf'] ?? ''), $csrf);
}

// ------------------------------------------------------------------ 环境检查

function env_checks(string $ROOT): array
{
    $out = [];
    $out[] = ['PHP 版本 ≥ 8.0', PHP_VERSION, version_compare(PHP_VERSION, '8.0.0', '>='),
              '当前 ' . PHP_VERSION, true];

    foreach (['pdo' => '必需', 'json' => '必需', 'mbstring' => '必需', 'curl' => '必需',
              'openssl' => '必需', 'zlib' => '必需', 'fileinfo' => '建议',
              'sockets' => '建议', 'pdo_sqlite' => '选 SQLite 时必需',
              'pdo_mysql' => '选 MySQL 时必需'] as $ext => $need) {
        $has = extension_loaded($ext);
        $required = $need === '必需';
        $out[] = ["扩展 {$ext}", $need, $has, $has ? '已加载' : '未加载', $required];
    }

    // 被禁用的函数：构建固件要 spawn 子进程
    $disabled = array_filter(array_map('trim', explode(',',
        (string) ini_get('disable_functions'))));
    $need = ['proc_open', 'exec', 'shell_exec'];
    $blocked = array_values(array_intersect($need, $disabled));
    $out[] = ['构建所需函数未被禁用', implode(' / ', $need), $blocked === [],
              $blocked ? '被禁用：' . implode(', ', $blocked) : '可用', false];

    foreach ([$ROOT => '仓库根目录', $ROOT . '/data' => 'data/',
              $ROOT . '/store' => 'store/', $ROOT . '/work' => 'work/',
              $ROOT . '/cache' => 'cache/'] as $dir => $label) {
        if (!is_dir($dir)) {
            @mkdir($dir, 0755, true);
        }
        $w = is_dir($dir) && is_writable($dir);
        $out[] = ["目录可写 {$label}", '必需', $w,
                  $w ? '可写' : '不可写（把属主改成运行 PHP 的用户，常见 www）', true];
    }

    $free = @disk_free_space($ROOT);
    $ok = $free !== false && $free > 3 * 1024 * 1024 * 1024;
    $out[] = ['磁盘余量 ≥ 3 GB', '构建功能需要', $ok,
              $free !== false ? round($free / 1073741824, 1) . ' GB 可用' : '未知', false];

    if (function_exists('opcache_get_status')) {
        $st = @opcache_get_status(false);
        $enabled = is_array($st) && !empty($st['opcache_enabled']);
        $out[] = ['OPcache', '建议', $enabled, $enabled ? '已启用' : '未启用（能跑，但慢）', false];
    }
    return $out;
}

/** 只保留「必需」项全过才放行。 */
function env_pass(array $rows): bool
{
    foreach ($rows as $r) {
        if ($r[4] && !$r[2]) {
            return false;
        }
    }
    return true;
}

// ------------------------------------------------------------------ 数据库

/**
 * 用**独立于 Config/Db 的裸 PDO** 试连。
 *
 * 为什么不直接用 Db::pdo()：那会读已经写好的配置，而此刻配置还没写。
 * 试连必须是「拿用户刚填的参数去连」，连不上不能留下任何副作用。
 */
function try_mysql(array $p): array
{
    $dsn = sprintf('mysql:host=%s;port=%d;dbname=%s;charset=utf8mb4',
        $p['host'], (int) $p['port'], $p['dbname']);
    try {
        $pdo = new PDO($dsn, $p['user'], $p['pass'], [
            PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
            PDO::ATTR_TIMEOUT => 6,
        ]);
        return [true, $pdo, ''];
    } catch (PDOException $e) {
        return [false, null, $e->getMessage()];
    }
}

/** 库不存在时尝试建库（需要 CREATE 权限；没有就返回可照抄的 SQL）。 */
function create_mysql_db(array $p): array
{
    try {
        $pdo = new PDO(sprintf('mysql:host=%s;port=%d;charset=utf8mb4', $p['host'], (int) $p['port']),
            $p['user'], $p['pass'], [PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION]);
        $pdo->exec("CREATE DATABASE IF NOT EXISTS `{$p['dbname']}` "
                 . 'DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci');
        return [true, ''];
    } catch (PDOException $e) {
        $msg = $e->getMessage();
        if (stripos($msg, 'denied') !== false || stripos($msg, 'privilege') !== false) {
            return [false, "当前数据库账号没有建库权限。请在面板里手工建库后重试：\n"
                         . "CREATE DATABASE `{$p['dbname']}` DEFAULT CHARACTER SET utf8mb4 "
                         . 'COLLATE utf8mb4_unicode_ci;'];
        }
        return [false, $msg];
    }
}

/** 把配置写进 config.local.json（已在 .gitignore 内，不会被提交）。 */
function write_local_config(string $CFG, array $db): array
{
    $cur = [];
    if (is_file($CFG)) {
        $j = json_decode((string) file_get_contents($CFG), true);
        if (is_array($j)) {
            $cur = $j;
        }
    }
    $cur['db'] = $db;
    $json = json_encode($cur, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);
    if (@file_put_contents($CFG, $json . "\n") === false) {
        return [false, "写不进去 {$CFG}：目录不可写"];
    }
    // 里面是数据库口令，收权限
    @chmod($CFG, 0600);
    return [true, ''];
}

// ------------------------------------------------------------------ 请求处理

$errors = [];
$done   = '';

if (($_SERVER['REQUEST_METHOD'] ?? 'GET') === 'POST') {
    $step = (int) ($_POST['step'] ?? 0);
    $csrf = (string) ($_POST['csrf'] ?? '');

    if (!csrf_ok($csrf)) {
        $errors[] = '页面已过期（CSRF 校验失败），请刷新后重试。';
    } elseif ($step > 1 && $step < 6 && $step !== (int) $S['step']) {
        // 跳步：比如直接 POST step=3 想绕过数据库选择。
        // 不做守卫的话，管理员会建到一个「默认 SQLite」上，向导状态就乱了。
        $errors[] = '步骤顺序不对，请从当前步骤继续。';
    } elseif ($step === 1) {
        // 环境检查只是展示，表单只负责推进
        $S['step'] = 2;
    } elseif ($step === 2 && (int) $S['step'] === 2) {
        // ---- 数据库
        $driver = ($_POST['driver'] ?? 'sqlite') === 'mysql' ? 'mysql' : 'sqlite';
        if ($driver === 'sqlite') {
            $S['db'] = ['driver' => 'sqlite'];
            try {
                \Kwrt\Config::reset();
                \Kwrt\Db::reset();
                kwrt_setenv('KWRT_DB_DRIVER', 'sqlite');
                \Kwrt\Db::pdo();                       // 建表 + 引导默认设置
                $S['step'] = 3;
            } catch (\Throwable $e) {
                $errors[] = 'SQLite 初始化失败：' . $e->getMessage();
            }
        } else {
            $p = [
                'host'   => trim((string) ($_POST['host'] ?? '127.0.0.1')) ?: '127.0.0.1',
                'port'   => (int) ($_POST['port'] ?? 3306) ?: 3306,
                'dbname' => preg_replace('/[^A-Za-z0-9_$]/', '', (string) ($_POST['dbname'] ?? '')),
                'user'   => trim((string) ($_POST['user'] ?? '')),
                'pass'   => (string) ($_POST['pass'] ?? ''),
            ];
            if ($p['dbname'] === '' || $p['user'] === '') {
                $errors[] = '库名与用户名不能为空。';
            } else {
                [$ok, $pdo, $why] = try_mysql($p);
                if (!$ok && stripos($why, 'Unknown database') !== false
                    && !empty($_POST['autocreate'])) {
                    [$cok, $cwhy] = create_mysql_db($p);
                    if (!$cok) {
                        $errors[] = $cwhy;
                    } else {
                        [$ok, $pdo, $why] = try_mysql($p);
                    }
                }
                if (!$ok && !$errors) {
                    // 三类失败要分开说 —— 修法完全不同，错归一类会让人白折腾：
                    //   Unknown database  → 库没有，去建库
                    //   Access denied ... to database → 库有了但这账号没被授权（新建库最容易踩）
                    //   其它 Access denied → 才是账号/口令不对
                    if (stripos($why, 'Unknown database') !== false) {
                        $errors[] = "库里没有 `{$p['dbname']}`。勾选「库不存在时自动创建」，"
                                  . "或到面板里手工建库（字符集选 utf8mb4）。";
                    } elseif (stripos($why, 'to database') !== false) {
                        $errors[] = "MySQL 账号 `{$p['user']}` 存在，但对库 `{$p['dbname']}` "
                                  . "没有授权（不是口令错）。\n"
                                  . "宝塔：数据库 → 找到该库 → 权限设置为「所有人/指定账号」；\n"
                                  . "或执行：GRANT ALL PRIVILEGES ON `{$p['dbname']}`.* "
                                  . "TO '{$p['user']}'@'%'; FLUSH PRIVILEGES;";
                    } elseif (stripos($why, 'Access denied') !== false) {
                        $errors[] = "MySQL 账号或口令不对（用户 {$p['user']}）。"
                                  . "宝塔面板「数据库」页可重置口令。";
                    } else {
                        $errors[] = '连接失败：' . $why;
                    }
                }
                if (!$errors) {
                    // 落盘 + 正式建表
                    [$wok, $wwhy] = write_local_config($CFG, [
                        'driver' => 'mysql',
                        'mysql'  => ['host' => $p['host'], 'port' => $p['port'],
                                     'dbname' => $p['dbname'], 'user' => $p['user'],
                                     'pass' => $p['pass']],
                    ]);
                    if (!$wok) {
                        $errors[] = $wwhy;
                    } else {
                        kwrt_setenv('KWRT_DB_DRIVER', 'mysql');
                        kwrt_setenv('KWRT_MYSQL_HOST', (string) $p['host']);
                        kwrt_setenv('KWRT_MYSQL_PORT', (string) $p['port']);
                        kwrt_setenv('KWRT_MYSQL_DB', (string) $p['dbname']);
                        kwrt_setenv('KWRT_MYSQL_USER', (string) $p['user']);
                        kwrt_setenv('KWRT_MYSQL_PASS', (string) $p['pass']);
                        try {
                            \Kwrt\Config::reset();
                            \Kwrt\Db::reset();
                            \Kwrt\Db::pdo();
                            $S['db'] = ['driver' => 'mysql', 'dbname' => $p['dbname']];
                            $S['step'] = 3;
                        } catch (\Throwable $e) {
                            $errors[] = '建表失败：' . $e->getMessage();
                        }
                    }
                }
            }
        }
    } elseif ($step === 3 && (int) $S['step'] === 3) {
        // ---- 管理员
        $u = trim((string) ($_POST['admin_user'] ?? ''));
        $p1 = (string) ($_POST['admin_pass'] ?? '');
        $p2 = (string) ($_POST['admin_pass2'] ?? '');
        $em = trim((string) ($_POST['admin_email'] ?? ''));
        if (!preg_match('/^[A-Za-z0-9_.-]{3,32}$/', $u)) {
            $errors[] = '用户名需为 3–32 位字母、数字、_ . -';
        } elseif (strlen($p1) < 8) {
            $errors[] = '口令至少 8 位。';
        } elseif ($p1 !== $p2) {
            $errors[] = '两次输入的口令不一致。';
        } elseif ($em !== '' && !filter_var($em, FILTER_VALIDATE_EMAIL)) {
            $errors[] = '邮箱格式不正确（可留空）。';
        } else {
            try {
                $exists = \Kwrt\Db::val('SELECT COUNT(*) FROM users WHERE username=?', [$u]);
                if ((int) $exists > 0) {
                    $errors[] = "用户名 {$u} 已存在，换一个。";
                } else {
                    $quota = (int) (\Kwrt\Settings::get('default_quota', 12) ?: 12);
                    \Kwrt\Db::run(
                        'INSERT INTO users(username, password, email, created, role, sponsor, quota) '
                        . 'VALUES(?,?,?,?,?,?,?)',
                        [$u, \Kwrt\Auth::hashPw($p1), $em, microtime(true), 'admin', 1, $quota]);
                    // 立刻写一份管理员凭据到 data/，与 init_admin.php 的行为一致
                    @file_put_contents($ROOT . '/data/INITIAL_ADMIN.txt',
                        "用户名: {$u}\n口令: {$p1}\n创建于: " . date('c') . "\n"
                        . "（登录后请立即修改口令，并删除本文件）\n");
                    @chmod($ROOT . '/data/INITIAL_ADMIN.txt', 0600);
                    $S['admin'] = $u;
                    $S['step'] = 4;
                }
            } catch (\Throwable $e) {
                $errors[] = '创建管理员失败：' . $e->getMessage();
            }
        }
    } elseif ($step === 4 && (int) $S['step'] === 4) {
        // ---- 站点设置（都可留空，用默认值）
        $name   = trim((string) ($_POST['site_name'] ?? ''));
        $domain = trim((string) ($_POST['domain'] ?? ''));
        $https  = !empty($_POST['force_https']);
        $hosts  = trim((string) ($_POST['trusted_hosts'] ?? ''));
        try {
            if ($name !== '') {
                \Kwrt\Settings::set('site_name', $name);
            }
            if ($domain !== '') {
                // 允许带协议前缀，存进去时去掉
                $domain = preg_replace('#^https?://#i', '', $domain);
                $domain = rtrim((string) preg_replace('#/.*$#', '', $domain), '/');
                \Kwrt\Settings::set('site.domain', $domain);
                // 绑了域名就把 Host 白名单同步上，否则绑完自己都进不去
                $list = $domain;
                if ($hosts !== '') {
                    $list .= ',' . $hosts;
                }
                \Kwrt\Settings::set('site.trusted_hosts', $list);
            } elseif ($hosts !== '') {
                \Kwrt\Settings::set('site.trusted_hosts', $hosts);
            }
            \Kwrt\Settings::set('site.force_https', $https ? '1' : '0');
            $S['step'] = 5;
        } catch (\Throwable $e) {
            $errors[] = '保存站点设置失败：' . $e->getMessage();
        }
    } elseif ($step === 5 && (int) $S['step'] === 5) {
        // ---- 收尾：写锁
        $info = [
            'installed_at' => date('c'),
            'driver'       => (string) ($S['db']['driver'] ?? 'sqlite'),
            'db'           => (string) ($S['db']['dbname'] ?? 'users.db'),
            'admin'        => (string) ($S['admin'] ?? ''),
            'version'      => trim((string) @file_get_contents($ROOT . '/VERSION')),
        ];
        if (!is_dir($ROOT . '/data')) {
            @mkdir($ROOT . '/data', 0755, true);
        }
        if (@file_put_contents($LOCK, json_encode($info, JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE)) === false) {
            $errors[] = "写不进去 {$LOCK}：data/ 目录不可写";
        } else {
            @chmod($LOCK, 0600);
            @unlink($ALLOW);              // 用掉放行标记
            $done = 'ok';
            $S['step'] = 6;
        }
    }
}

// ------------------------------------------------------------------ 渲染

$envRows = env_checks($ROOT);
$envOk   = env_pass($envRows);
$step    = (int) $S['step'];
if ($step === 5 && !$envOk) {
    $step = 1;                        // 环境不过就别往下走
}

function h($s): string
{
    return htmlspecialchars((string) $s, ENT_QUOTES, 'UTF-8');
}

$INSTALLED_BLOCK = $STATE === 'installed';
$LIVE_BLOCK      = $STATE === 'live_unlocked' && !$INSTALL_IN_FLIGHT;
$VER             = trim((string) @file_get_contents($ROOT . '/VERSION')) ?: '—';
?>
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>安装向导 · Kwrt(OpenWrt) 固件站</title>
<style>
  :root{--bg:#0f1115;--card:#171a21;--line:#262b36;--fg:#e6e8ee;--dim:#98a2b3;
        --acc:#3b82f6;--ok:#22c55e;--warn:#f59e0b;--bad:#ef4444}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);line-height:1.6;
       font:15px/1.6 -apple-system,"Segoe UI",Roboto,"Helvetica Neue","PingFang SC","Microsoft YaHei",sans-serif}
  .wrap{max-width:860px;margin:0 auto;padding:32px 20px 64px}
  h1{font-size:22px;margin:0 0 4px}
  .sub{color:var(--dim);font-size:13px;margin-bottom:22px}
  .steps{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:24px}
  .steps span{padding:5px 12px;border:1px solid var(--line);border-radius:999px;
              font-size:12.5px;color:var(--dim)}
  .steps span.on{border-color:var(--acc);color:#fff;background:#1d4ed825}
  .steps span.ok{border-color:var(--ok);color:var(--ok)}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:22px;margin-bottom:18px}
  .card h2{font-size:16px;margin:0 0 14px}
  table{width:100%;border-collapse:collapse;font-size:13.5px}
  td,th{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top}
  th{color:var(--dim);font-weight:500}
  .ok{color:var(--ok)} .bad{color:var(--bad)} .warn{color:var(--warn)} .dim{color:var(--dim)}
  label{display:block;margin:12px 0 5px;font-size:13px;color:var(--dim)}
  input[type=text],input[type=password],input[type=number],select{
    width:100%;padding:9px 11px;background:#0d1017;border:1px solid var(--line);
    border-radius:8px;color:var(--fg);font-size:14px}
  input:focus,select:focus{outline:none;border-color:var(--acc)}
  .row{display:flex;gap:12px;flex-wrap:wrap}
  .row>div{flex:1;min-width:180px}
  .btn{display:inline-block;padding:10px 22px;border-radius:8px;border:0;cursor:pointer;
       background:var(--acc);color:#fff;font-size:14.5px;font-weight:600}
  .btn.ghost{background:transparent;border:1px solid var(--line);color:var(--fg)}
  .btn[disabled]{opacity:.45;cursor:not-allowed}
  .err{background:#3b1113;border:1px solid #7f1d1d;color:#fecaca;padding:11px 14px;
       border-radius:8px;margin-bottom:14px;font-size:13.5px;white-space:pre-wrap}
  .note{background:#0d1017;border:1px solid var(--line);border-radius:8px;padding:12px 14px;
        font-size:13px;color:var(--dim);white-space:pre-wrap;margin-top:14px}
  code{background:#0d1017;padding:1px 6px;border-radius:5px;font-size:12.5px}
  .radios{display:flex;gap:10px;margin-top:8px}
  .radios label{margin:0;display:flex;gap:7px;align-items:center;cursor:pointer;
    border:1px solid var(--line);border-radius:8px;padding:10px 14px;flex:1;color:var(--fg)}
  .radios input{accent-color:var(--acc)}
  .mt{margin-top:16px}
</style>
</head>
<body>
<div class="wrap">
  <h1>安装向导</h1>
  <div class="sub">Kwrt(OpenWrt) 固件在线定制站 · PHP 版 · 当前代码版本 v<?= h($VER) ?></div>

<?php if ($INSTALLED_BLOCK): ?>
  <div class="card">
    <h2 class="bad">站点已安装，向导已锁定</h2>
    <p>检测到 <code>data/install.lock</code>。向导在此状态下拒绝运行 ——
       否则任何人访问本页面都能重建管理员。</p>
    <table>
      <tr><th>安装时间</th><td><?= h($STATE_INFO['installed_at'] ?? '—') ?></td></tr>
      <tr><th>数据库</th><td><?= h(($STATE_INFO['driver'] ?? '?') . ' / ' . ($STATE_INFO['db'] ?? '?')) ?></td></tr>
      <tr><th>管理员</th><td><?= h($STATE_INFO['admin'] ?? '—') ?></td></tr>
      <tr><th>安装时版本</th><td><?= h($STATE_INFO['version'] ?? '—') ?></td></tr>
    </table>
    <div class="note">请直接使用站点：<a href="/">首页</a> · <a href="/admin/">后台</a>
（口令在安装时写入的 data/INITIAL_ADMIN.txt 里，登录后请改掉并删除该文件）

确需重装：在服务器上删除 data/install.lock，并创建空文件 data/install.allow 后刷新本页。</div>
  </div>

<?php elseif ($LIVE_BLOCK): ?>
  <div class="card">
    <h2 class="bad">检测到已存在的站点，向导已停止</h2>
    <p>数据库里已有 <b><?= (int) ($STATE_INFO['admins'] ?? 0) ?></b> 个管理员账户，
       但缺少锁文件 <code>data/install.lock</code>。
       为避免「删掉锁文件重装 → 接管站点」，向导在此状态下拒绝运行。</p>
    <div class="note">这通常意味着：站点是老版本装的（那时还没有锁文件），
或者是有人删过 data/ 下的文件。

如果站点确实是你自己的，且确实要重新安装：
  1. 在服务器上创建空文件 <code>data/install.allow</code>
  2. 刷新本页
  3. 装完后该文件会被自动删除</div>
  </div>

<?php else: ?>

  <div class="steps">
    <?php
    $names = [1 => '环境检查', 2 => '数据库', 3 => '管理员', 4 => '站点设置', 5 => '完成'];
    foreach ($names as $i => $n) {
        $cls = $i === $step ? 'on' : ($i < $step ? 'ok' : '');
        echo '<span class="' . $cls . '">' . ($i < $step ? '✓ ' : $i . '. ') . h($n) . '</span>';
    }
    ?>
  </div>

  <?php foreach ($errors as $e): ?>
    <div class="err"><?= h($e) ?></div>
  <?php endforeach; ?>

  <?php if ($step === 1): ?>
    <div class="card">
      <h2>① 环境检查</h2>
      <table>
        <tr><th>检查项</th><th>要求</th><th>结果</th></tr>
        <?php foreach ($envRows as $r): ?>
          <tr>
            <td><?= h($r[0]) ?></td>
            <td class="dim"><?= h($r[1]) ?></td>
            <td>
              <span class="<?= $r[2] ? 'ok' : ($r[4] ? 'bad' : 'warn') ?>">
                <?= $r[2] ? '✓' : ($r[4] ? '✗' : '!') ?>
              </span>
              <?= h($r[3]) ?>
            </td>
          </tr>
        <?php endforeach; ?>
      </table>
      <?php if (!$envOk): ?>
        <div class="note">标 ✗ 的「必需」项没通过，先修好再继续。
打包安装（宝塔）常见原因：
  · 扩展缺失 → 面板里给对应 PHP 版本装扩展，然后重载 PHP-FPM
  · 目录不可写 → 把站点目录属主改成运行 PHP 的用户（常见 www）
  · 若开了「防跨站攻击(open_basedir)」，确认没有把站点目录的上级排除掉</div>
      <?php endif; ?>
      <form method="post" class="mt">
        <input type="hidden" name="csrf" value="<?= h($CSRF) ?>">
        <input type="hidden" name="step" value="1">
        <button class="btn" <?= $envOk ? '' : 'disabled' ?>>下一步：配置数据库</button>
        <?php if (!$envOk): ?>
          <a class="btn ghost" href="?">重新检查</a>
        <?php endif; ?>
      </form>
    </div>
  <?php endif; ?>

  <?php if ($step === 2): ?>
    <div class="card">
      <h2>② 数据库</h2>
      <form method="post">
        <input type="hidden" name="csrf" value="<?= h($CSRF) ?>">
        <input type="hidden" name="step" value="2">
        <div class="radios">
          <label><input type="radio" name="driver" value="sqlite" checked> SQLite（零配置）</label>
          <label><input type="radio" name="driver" value="mysql"> MySQL / MariaDB</label>
        </div>

        <div class="note" id="n-sqlite">数据文件将建在 <code><?= h($ROOT . '/users.db') ?></code>
（在站点目录之外，不会被当成静态文件下载）。
这个是 Python 版也支持的存储 —— 想两版共用同一份数据就选它。</div>

        <div id="f-mysql" style="display:none">
          <div class="row">
            <div><label>主机</label><input type="text" name="host" value="127.0.0.1"></div>
            <div><label>端口</label><input type="number" name="port" value="3306"></div>
          </div>
          <div class="row">
            <div><label>库名</label><input type="text" name="dbname" value="kwrt"></div>
          </div>
          <div class="row">
            <div><label>用户名</label><input type="text" name="user" value="kwrt"></div>
            <div><label>口令</label><input type="password" name="pass" value=""></div>
          </div>
          <label style="margin-top:14px">
            <input type="checkbox" name="autocreate" value="1" checked>
            库不存在时自动创建（需要该账号有 CREATE 权限；没有就先去面板建库）
          </label>
          <div class="note">注意：切到 MySQL 后<b>不再与 Python 版共库</b>（Python 只支持 SQLite）。
只跑 PHP 版的话 MySQL 更合适。

一个数据库对应一个站点（本版不支持表前缀）。要跑多个站点就建多个库 ——
给全站查询加前缀需要改动两百多处 SQL，做一半反而更危险。</div>
        </div>

        <button class="btn mt">测试连接并初始化</button>
      </form>
    </div>
    <script>
      var rs = document.querySelectorAll('input[name=driver]');
      function sync(){
        var v = document.querySelector('input[name=driver]:checked').value;
        document.getElementById('f-mysql').style.display  = v==='mysql'  ? '' : 'none';
        document.getElementById('n-sqlite').style.display = v==='sqlite' ? '' : 'none';
      }
      rs.forEach(function(r){ r.addEventListener('change', sync); });
      sync();
    </script>
  <?php endif; ?>

  <?php if ($step === 3): ?>
    <div class="card">
      <h2>③ 管理员账户</h2>
      <p class="dim">数据库已就绪（<?= h(($S['db']['driver'] ?? '') . ' / ' . ($S['db']['dbname'] ?? 'users.db')) ?>）。
         现在建第一个管理员，它是你进后台的唯一入口。</p>
      <form method="post">
        <input type="hidden" name="csrf" value="<?= h($CSRF) ?>">
        <input type="hidden" name="step" value="3">
        <div class="row">
          <div><label>用户名</label><input type="text" name="admin_user" value="admin" required></div>
          <div><label>邮箱（可留空）</label><input type="text" name="admin_email" value=""></div>
        </div>
        <div class="row">
          <div><label>口令（≥8 位）</label><input type="password" name="admin_pass" required></div>
          <div><label>再输一次</label><input type="password" name="admin_pass2" required></div>
        </div>
        <div class="note">创建后会同时在 <code>data/INITIAL_ADMIN.txt</code> 留一份凭据
（权限 0600，只是给你自己看的 —— 登录后请改口令并删掉它）。</div>
        <button class="btn mt">创建并继续</button>
      </form>
    </div>
  <?php endif; ?>

  <?php if ($step === 4): ?>
    <div class="card">
      <h2>④ 站点设置</h2>
      <p class="dim">都可跳过，之后在后台「设置中心」随时改。</p>
      <form method="post">
        <input type="hidden" name="csrf" value="<?= h($CSRF) ?>">
        <input type="hidden" name="step" value="4">
        <label>站点名称</label>
        <input type="text" name="site_name" value="Kwrt(OpenWrt) 固件在线定制">
        <label>绑定域名（可留空，之后在后台改）</label>
        <input type="text" name="domain" placeholder="fw.example.com">
        <label>额外允许的 Host（逗号分隔，可留空）</label>
        <input type="text" name="trusted_hosts" placeholder="127.0.0.1:8080,localhost">
        <label style="margin-top:14px">
          <input type="checkbox" name="force_https" value="1"> 强制跳转 HTTPS（配好证书后再开）
        </label>
        <div class="note">绑域名时会自动把它加进 Host 白名单，否则绑完自己都进不去。
本机 127.0.0.1 / localhost 始终放行，不会被锁在门外。</div>
        <button class="btn mt">保存并完成安装</button>
      </form>
    </div>
  <?php endif; ?>

  <?php if ($step === 5): ?>
    <div class="card">
      <h2>⑤ 完成</h2>
      <p>安装信息已确认，点下面的按钮写入锁文件并收尾。</p>
      <table>
        <tr><th>数据库</th><td><?= h(($S['db']['driver'] ?? 'sqlite') . ' / ' . ($S['db']['dbname'] ?? 'users.db')) ?></td></tr>
        <tr><th>管理员</th><td><?= h($S['admin'] ?? '—') ?></td></tr>
      </table>
      <form method="post" class="mt">
        <input type="hidden" name="csrf" value="<?= h($CSRF) ?>">
        <input type="hidden" name="step" value="5">
        <button class="btn">完成安装</button>
      </form>
    </div>
  <?php endif; ?>

  <?php if ($done === 'ok'): ?>
    <div class="card">
      <h2 class="ok">✓ 安装完成</h2>
      <p>现在可以进站了。建议按顺序做三件事：</p>
      <table>
        <tr><th>1</th><td>登录后台改掉初始口令：<a href="/admin/">/admin/</a>
                （凭据在 <code>data/INITIAL_ADMIN.txt</code>）</td></tr>
        <tr><th>2</th><td>删掉本文件 <code>php/public/install.php</code>，
                或保持 <code>data/install.lock</code> 存在 —— 有锁时向导会拒绝运行</td></tr>
        <tr><th>3</th><td>需要构建固件的话，到后台「构建设置」确认后端与 GitHub 队列</td></tr>
      </table>
      <div class="note">构建功能还需要服务器能访问 downloads.openwrt.org 与 dl.openwrt.ai，
且留出 ≥3 GB 临时空间。

宝塔面板部署的完整清单见仓库 docs/宝塔面板部署指南.md。</div>
      <p class="mt"><a class="btn" href="/">进入站点</a>
         <a class="btn ghost" href="/admin/">打开后台</a></p>
    </div>
  <?php endif; ?>

<?php endif; ?>

  <div class="sub" style="margin-top:26px">
    Kwrt(OpenWrt) 固件在线定制站 · PHP 版安装向导 ·
    <a href="/healthz" style="color:var(--dim)">/healthz</a>
  </div>
</div>
</body>
</html>