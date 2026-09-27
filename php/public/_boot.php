<?php
/**
 * 入口布局引导 —— 定位「代码目录」与「数据根目录」。
 *
 * 为什么需要这个文件
 * ------------------
 * 原先每个入口都硬编码 `dirname(__DIR__, 2)`，即假设部署成**仓库原样布局**：
 *
 *     <R>/                          ← 数据根（users.db / data/ / store/ / cache/）
 *     ├── php/
 *     │   ├── public/install.php    ← docroot
 *     │   ├── src/                  ← 代码
 *     │   └── templates/
 *     └── config.json
 *
 * 这个假设在宝塔上很容易被打破 —— 常见做法是把 `php/public` 的内容直接放到
 * 站点根目录下（docroot = `<R>/public`）。此时 `dirname(__DIR__, 2)` 会退到
 * 仓库根的**上一级**，于是去找一个根本不存在的路径，用户看到的是：
 *
 *     Warning: require(/www/wwwroot/php/src/helpers.php): Failed to open stream
 *     Fatal error: Uncaught Error: Failed opening required ...
 *
 * ——一句 PHP 内部错误，既没说清布局要求，也没说怎么改。真实现场。
 *
 * 本文件做两件事：
 *   1. **探测**几种真实存在的布局，不再写死一种；
 *   2. 一个都认不出来时，输出**人能看懂、能照着做**的诊断页（期望什么布局、
 *      实际找到了什么、怎么改），而不是让 PHP 抛 fatal。
 *
 * 布局要求（本文件用 PHP 5/7 也能解析的语法写，理由同 PHP 版本守卫）：
 *   A 标准   docroot=<R>/php/public  代码=<R>/php/src   数据=<R>
 *   B 扁平A  docroot=<R>/public      代码=<R>/src       数据=<R>
 *   C 扁平B  docroot=<R>/public      代码=<R>/php/src   数据=<R>
 *   D 全平铺 docroot=<R>             代码=<R>/src       数据=<R>
 *
 * 用法：
 *     require __DIR__ . '/_boot.php';
 *     require KWRT_SRC . '/helpers.php';
 *     // 之后一律用 KWRT_SRC（代码）与 KWRT_ROOT（数据）
 */

// 本文件在 docroot 下，会被 Web 直接访问到。它不产出任何内容，
// 但没必要被直接请求 —— 直接访问就静默 404。
if (isset($_SERVER['SCRIPT_FILENAME'])
    && realpath((string) $_SERVER['SCRIPT_FILENAME']) === realpath(__FILE__)) {
    http_response_code(404);
    exit;
}

if (!defined('KWRT_BOOT_LOADED')) {
    define('KWRT_BOOT_LOADED', 1);

    /**
     * 在候选里找「代码目录」——判据是里面同时有 helpers.php 与 Config.php。
     *
     * 不用「目录存在」当判据：宝塔站点根目录下常有同名但无关的目录。
     */
    function kwrt_locate(): array
    {
        $here = __DIR__;
        $up1  = dirname($here);
        $up2  = dirname($here, 2);
        $up3  = dirname($here, 3);

        // 代码目录候选（判据：同时有 helpers.php 与 Config.php）。
        // 顺序即优先级，覆盖四种布局。
        $codeCands = [
            $up2 . '/php/src',    // A 标准：docroot=<R>/php/public
            $up1 . '/php/src',    // C 扁平：docroot=<R>/public，php/ 保留
            $up1 . '/src',        // B 扁平：docroot=<R>/public，src 并列
            $here . '/src',       // D 全平铺：docroot=<R>
            $up3 . '/php/src',    // 多套一层的情况
        ];

        foreach ($codeCands as $src) {
            if (!is_file($src . '/helpers.php') || !is_file($src . '/Config.php')) {
                continue;
            }
            return [$src, kwrt_data_root($src)];
        }
        return ['', ''];
    }

    /**
     * 数据根 = 从代码目录**向上**找第一个含 config.json / VERSION / data 的目录。
     *
     * 为什么不能靠「数几层」推导：`<R>/php/src` 与 `<R>/src` 这两种摆法下，
     * 「上一级」的含义完全不同 —— 前者要退两级才是 <R>，后者退一级就是 <R>。
     * 光看路径层级分不清，必须靠**内容标记**判断。
     * （本函数的第一版就是靠 $upN 硬推，结果标准布局下多退了一层，
     *   store/、users.db、data/ 全部偏位 —— 被 verify_php 的 P-2/P-18 抓到。）
     */
    function kwrt_data_root(string $src): string
    {
        $d = $src;
        for ($i = 0; $i < 4; $i++) {
            $d = dirname($d);
            if ($d === '' || $d === '/' || $d === '.') {
                break;
            }
            if (is_file($d . '/config.json') || is_file($d . '/VERSION') || is_dir($d . '/data')) {
                return $d;
            }
        }
        // 兜底：<...>/php/src → 退两级；其余 → 退一级
        return basename(dirname($src)) === 'php' ? dirname($src, 2) : dirname($src);
    }

    /** 模板目录：优先 <php>/templates，其次 <root>/templates。 */
    function kwrt_tpl(string $src, string $root): string
    {
        $a = dirname($src) . '/templates';
        if (is_dir($a)) {
            return $a;
        }
        $b = $root . '/templates';
        if (is_dir($b)) {
            return $b;
        }
        return $a;
    }

    /** 找不到布局时，输出可照做的诊断页并退出。 */
    function kwrt_boot_fail(string $here): void
    {
        $up1 = dirname($here);
        $up2 = dirname($here, 2);
        if (!headers_sent()) {
            header('Content-Type: text/plain; charset=utf-8', true, 500);
        }
        $probe = static function (string $p): string {
            return '    ' . $p . '  →  ' . (is_dir($p) ? '目录存在' : '不存在')
                 . (is_file($p . '/helpers.php') ? ' + helpers.php 在' : '');
        };
        echo "Kwrt(OpenWrt) 固件站 · PHP 版启动失败\n"
           . "=====================================\n\n"
           . "原因：找不到项目代码目录（php/src，需含 helpers.php 与 Config.php）。\n"
           . "      也就是说：站点上传的文件不完整，或者「运行目录」设错了。\n\n"
           . "我实际找过这些位置：\n"
           . $probe($up2 . '/php/src') . "\n"
           . $probe($up1 . '/src') . "\n"
           . $probe($up1 . '/php/src') . "\n"
           . $probe($here . '/src') . "\n\n"
           . "当前入口文件所在目录（docroot）：\n    " . $here . "\n"
           . "它的上一级：\n    " . $up1 . "\n\n"
           . "期望的目录结构（推荐、最省事）：\n"
           . "    /www/wwwroot/你的域名/            ← 整个仓库都传到这里\n"
           . "    ├── php/\n"
           . "    │   ├── public/     ← 宝塔「网站 → 设置 → 网站目录 → 运行目录」选 /php/public\n"
           . "    │   ├── src/\n"
           . "    │   ├── scripts/\n"
           . "    │   └── templates/\n"
           . "    ├── data/           ← 需要可写\n"
           . "    ├── store/  work/  cache/   ← 需要可写\n"
           . "    └── config.json\n\n"
           . "怎么修（二选一）——\n"
           . "  ① 推荐：把**完整仓库**（不是只有 php/public）上传到站点根目录，\n"
           . "     然后 宝塔 → 网站 → 你的站点 → 设置 → 网站目录 → 运行目录 选 /php/public。\n"
           . "     （只传 public 的话，src/、templates/、data/ 都会缺。）\n\n"
           . "  ② 已经只传了 public 的话：把 php/src、php/scripts、php/templates\n"
           . "     也传上来，与 public 并列放在同一层，再把 data/、store/、work/、cache/\n"
           . "     建在它们的上一级。本程序支持这种扁平布局。\n\n"
           . "检查清单：\n"
           . "  · src/ 里有没有 helpers.php 和 Config.php？\n"
           . "  · data/ 目录是否存在且可写（宝塔：网站 → 设置 → 目录权限）？\n"
           . "  · 运行目录是不是指到了 php/public（或 public）？\n\n"
           . "装好后访问 /install.php 走安装向导；自检 /healthz。\n";
        exit(1);
    }

    [$kwrtSrc, $kwrtRoot] = kwrt_locate();
    if ($kwrtSrc === '') {
        kwrt_boot_fail(__DIR__);
    }

    define('KWRT_SRC', $kwrtSrc);
    define('KWRT_ROOT', $kwrtRoot);
    define('KWRT_TPL', kwrt_tpl($kwrtSrc, $kwrtRoot));
}
