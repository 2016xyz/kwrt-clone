<?php
/**
 * 前端控制器（唯一入口）。
 *
 * 请求处理顺序（顺序本身就是安全设计，不能随意调换）：
 *   1. 引导：autoload / 配置 / 数据库 / 模板目录
 *   2. 静态资源短路（不经过业务逻辑，避免给静态文件加会话开销）
 *   3. 安全前置：强制 HTTPS → 安全响应头 → Host 白名单 → 封禁 → 页面开关 → CSRF
 *   4. 路由分发
 *   5. 兜底 404 / 500（500 绝不回显堆栈）
 *
 * 部署方式两种都支持：
 *   A. 内置服务器：  php -S 0.0.0.0:8080 -t php/public
 *   B. Apache/Nginx：document_root 指到 php/public，其余请求交给本文件
 *      （php/public/.htaccess 已给出 Apache 的等价规则）
 */
declare(strict_types=1);


namespace Kwrt;

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


// ---------------------------------------------------------------- 1. 引导

define('KWRT_START', microtime(true));

// 布局探测（代码目录 / 数据根 / 模板目录）必须在 autoload 之前完成
require __DIR__ . '/_boot.php';

// 开发模式：KWRT_DEBUG=1 时回显错误详情；生产默认关闭
$KWRT_DEBUG = (bool) (getenv('KWRT_DEBUG') ?: false);
if ($KWRT_DEBUG) {
    ini_set('display_errors', '1');
    error_reporting(E_ALL);
} else {
    ini_set('display_errors', '0');
    error_reporting(E_ALL & ~E_DEPRECATED);
}

spl_autoload_register(static function (string $class): void {
    if (!str_starts_with($class, 'Kwrt\\')) {
        return;
    }
    $rel = str_replace('\\', '/', substr($class, 5));
    $f = KWRT_SRC . '/' . $rel . '.php';
    if (is_file($f)) {
        require $f;
    }
});

require KWRT_SRC . '/helpers.php';

// ★ 不再写死 dirname(__DIR__, 2)：宝塔上常见把 php/public 的内容直接放到站点根，
//   那样退两级会落到仓库根的上一级、加载不到代码（真实现场报过
//   「Failed opening required /www/wwwroot/php/src/helpers.php」）。
//   _boot.php 会探测实际布局，认不出来时给人话而不是 fatal。
$ROOT = KWRT_ROOT;
View::init(KWRT_TPL);

// ---------------------------------------------------------------- 请求上下文

$method = strtoupper($_SERVER['REQUEST_METHOD'] ?? 'GET');
$uri    = (string) ($_SERVER['REQUEST_URI'] ?? '/');
$path   = Pages::normalize(parse_url($uri, PHP_URL_PATH) ?: '/');

// ---------------------------------------------------------------- 2. 静态资源短路

$staticDir = __DIR__ . '/assets';
if (str_starts_with($path, '/assets/') || str_starts_with($path, '/static/')) {
    $base = str_starts_with($path, '/assets/')
        ? $staticDir
        : __DIR__ . '/static';
    $rel = substr($path, 8);
    // ★ 路径式版本化：/<prefix>/_v/<ver>/<rest> → /<prefix>/<rest>
    //
    //   为什么版本号放**路径**而不是 ?v= 查询串：实测 EdgeOne（腾讯 CDN）的
    //   缓存键**忽略查询参数** —— 拿一个全新的 ?v= 请求，它仍然返回旧缓存
    //   （Age 非 0）。只有路径变了，CDN 才认定是换了资源。
    //   这样浏览器、宝塔反代的全局 proxy_cache、外部 CDN 三处同时失效。
    //
    //   版本段用白名单校验（只允许字母数字点下划线短横）—— 从入口杜绝 ../。
    //   剩下部分交给 Util::under 收口（见下方注释）。
    $versioned = false;
    if (preg_match('#^_v/([0-9A-Za-z._-]{1,40})/(.+)$#', $rel, $m)) {
        $rel = $m[2];
        $versioned = true;
    } elseif (isset($_GET['v']) && (string) $_GET['v'] !== '') {
        $versioned = true;      // 兼容旧的 ?v= 形式
    }
    // ★ 必须用 Util::under 收口，不要裸 str_starts_with。
    //   裸判断没带分隔符：base=/…/public/assets 时，/…/public/assets-×/x
    //   是它的**字符串前缀**却不是子路径 —— 一旦 public/ 下出现名字以
    //   assets / static 开头的兄弟目录，这里就变成任意文件读取。
    //   同一个仓库里 Util::under（Util.php:90）就是为这件事写的，用起来。
    $file = Util::under($base, $base . '/' . $rel);
    if ($file !== null && is_file($file)) {
        foreach (Net::assetCacheHeaders(false, $versioned) as $k => $v) {
            header($k . ': ' . $v);
        }
        header('Content-Type: ' . (mime_content_type($file) ?: 'application/octet-stream'));
        header('Content-Length: ' . filesize($file));
        readfile($file);
        exit;
    }
    http_response_code(404);
    exit;
}

// 引导数据库（失败直接 500，但要说人话）
try {
    Db::pdo();
} catch (\Throwable $e) {
    http_response_code(500);
    header('Content-Type: text/plain; charset=utf-8');
    echo "数据库初始化失败：\n" . ($KWRT_DEBUG ? $e->getMessage() : '请检查 users.db 目录是否可写');
    exit;
}

// ---------------------------------------------------------------- 3. 安全前置

// 3.0 强制 HTTPS（仅在设置开启，且当前确实是 http 时）
if (Settings::bool('site.force_https', false) && Net::scheme() !== 'https') {
    $qs = $_SERVER['QUERY_STRING'] ?? '';
    $target = 'https://' . (Net::boundDomain() ?: Net::host()) . $path . ($qs !== '' ? '?' . $qs : '');
    header('Location: ' . $target, true, 301);
    exit;
}

// 3.1 安全响应头（挂在所有响应上，包括错误页）
foreach (Net::securityHeaders() as $k => $v) {
    header($k . ': ' . $v);
}
header('X-Powered-By: ');   // 不暴露实现

// 3.2 Host 白名单：不通过就拒绝，不把未校验的 Host 带进任何链接
if (!Net::hostAllowed()) {
    http_response_code(400);
    header('Content-Type: text/plain; charset=utf-8');
    echo "请求的 Host 不在允许列表内。\n";
    exit;
}

// 3.3 封禁（CDN 场景下用的是回源头，见 Net::clientIp）
if (str_starts_with($path, '/api/') || str_starts_with($path, '/admin/')) {
    if (($b = Auth::banned()) !== null) {
        json_out(['status' => 'error', 'detail' => '你已被禁止访问' . ($b['reason'] ? '：' . $b['reason'] : '')], 403);
        exit;
    }
}

// 3.4 页面开关：一处生效，全部页面口径一致
if (!Pages::enabled($path)) {
    [$code, $to, $msg] = Pages::denyResponse();
    if ($to !== null) {
        redirect($to, 302);
    }
    http_response_code($code);
    if ($msg !== null) {
        header('Content-Type: text/html; charset=utf-8');
        View::share('page_switch_msg', $msg);
        echo View::page('disabled', ['message' => $msg]);
    } else {
        header('Content-Type: text/html; charset=utf-8');
        echo View::page('error404', ['path' => $path]);
    }
    exit;
}

// 3.5 CSRF（浏览器跨站写操作）
if (!Auth::csrfOk($path)) {
    if (str_starts_with($path, '/api/')) {
        json_out(['status' => 'error', 'detail' => '跨站请求被拒绝'], 403);
    } else {
        http_response_code(403);
        echo View::page('error403', ['path' => $path]);
    }
    exit;
}

// 3.6 全局共享给模板（flash 走 cookie，不引入 PHP session —— 保持无状态、易水平扩展）
View::share('settings', Settings::load());
View::share('nav', Pages::nav());
View::share('user', Auth::currentUser());
View::share('flash', isset($_COOKIE['kwrt_flash']) ? (string) $_COOKIE['kwrt_flash'] : null);
View::share('current_path', $path);
if (isset($_COOKIE['kwrt_flash'])) {
    setcookie('kwrt_flash', '', ['expires' => time() - 3600, 'path' => '/']);
}

// ---------------------------------------------------------------- 4. 分发

$router = new Router();
// routes.php 返回一个「注册路由」的闭包；require 会执行文件并返回该闭包。
// ★ 与 src/ 同级（php/routes.php），不能写死 __DIR__/../routes.php ——
//   扁平部署（docroot 不是 <R>/php/public）时那条相对路径不存在，
//   整站会直接 500。这里探测两处，都没有就给人话。
$routesFile = dirname(KWRT_SRC) . '/routes.php';
if (!is_file($routesFile)) {
    $routesAlt = KWRT_ROOT . '/routes.php';
    if (is_file($routesAlt)) {
        $routesFile = $routesAlt;
    }
}
if (!is_file($routesFile)) {
    error_log('[kwrt] routes.php 未找到，已试: ' . dirname(KWRT_SRC) . '/routes.php, '
        . KWRT_ROOT . '/routes.php');
    http_response_code(500);
    header('Content-Type: text/plain; charset=utf-8');
    echo "Kwrt(OpenWrt) 固件站 · 启动失败\n"
       . "===============================\n\n"
       . "原因：找不到路由定义文件 routes.php。\n\n"
       . "它应该和 src/ 放在同一层，例如：\n"
       . "    " . dirname(KWRT_SRC) . "/routes.php\n"
       . "或：\n"
       . "    " . KWRT_ROOT . "/routes.php\n\n"
       . "上传仓库时漏了它（它不在 php/public 里，容易漏）。\n"
       . "把 php/routes.php 补传上去即可。\n";
    exit(1);
}
(require $routesFile)($router);

$hit = $router->match($method, $path);

if ($hit === null) {
    $allowed = $router->allowedFor($path);
    if ($allowed) {
        header('Allow: ' . implode(', ', $allowed));
        http_response_code(405);
        json_out(['status' => 'error', 'detail' => '方法不被允许'], 405);
        exit;
    }
    http_response_code(404);
    if (str_starts_with($path, '/api/')) {
        json_out(['status' => 'error', 'detail' => '接口不存在'], 404);
    } else {
        echo View::page('error404', ['path' => $path]);
    }
    exit;
}

[$handler, $params, $routePath] = $hit;

try {
    $out = $handler($params);
    if (is_string($out)) {
        // 只有控制器**没有**自己设 Content-Type 时才兜底为 HTML。
        // 否则 /manifest.webmanifest、/service-worker.js、/robots.txt 这些
        // 自己声明了类型的端点会被这里覆盖成 text/html ——
        // 浏览器会拒绝执行 MIME 不匹配的脚本（实测触发过
        // "unsupported MIME type ('text/html')"）。
        if (!headers_sent() && !kwrt_header_set('Content-Type')) {
            header('Content-Type: text/html; charset=utf-8');
        }
        // HTML 一律显式声明不缓存。
        // 为什么要显式：挂 CDN 时若不给 Cache-Control，CDN 会按自己的默认策略缓存
        // 页面 —— 于是「后台刚关掉的页面」或「刚改的站点文案」在所有边缘节点上
        // 仍然可见，而且极难排查（本地刷新是好的，因为本地没有 CDN）。
        if (!headers_sent() && !kwrt_header_set('Cache-Control')) {
            foreach (Net::assetCacheHeaders(true) as $k => $v) {
                header($k . ': ' . $v);
            }
        }
        echo $out;
    }
} catch (\Throwable $e) {
    error_log('[kwrt] ' . get_class($e) . ': ' . $e->getMessage() . ' @ ' . $e->getFile() . ':' . $e->getLine());
    if (str_starts_with($path, '/api/')) {
        json_out([
            'status' => 'error',
            'detail' => $KWRT_DEBUG ? $e->getMessage() : '服务器内部错误',
            'trace'  => $KWRT_DEBUG ? explode("\n", $e->getTraceAsString()) : null,
        ], 500);
    } else {
        http_response_code(500);
        echo View::page('error500', ['detail' => $KWRT_DEBUG ? $e->getMessage() : null]);
    }
}
