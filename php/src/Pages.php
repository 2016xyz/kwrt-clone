<?php
/**
 * 页面开关（后台「页面与入口」）。
 *
 * 设计要点：**开关的判定收在一个地方**，由路由层在分发之前统一调用。
 * 如果让每个控制器自己 `if (!Settings::bool(...))` 返回 404，就一定会漏 ——
 * 加页面的人不知道要加判断。这里用一张「路径 → 开关」的表，新增页面只在表里
 * 补一行，路由层自动生效。
 *
 * 另外把「哪些页面属于同一开关」也表达清楚：例如关闭「固件下载」应当同时
 * 关掉 /firmware/* 与 /dl/*，而不是只关一个入口。
 */
declare(strict_types=1);

namespace Kwrt;

final class Pages
{
    /**
     * 路径前缀 → 开关键。
     *
     * 匹配规则：按 **最长前缀** 命中（保证 /api/... 不会被 / 抢先）。
     * 未列出的路径默认放行（API 与静态资源不在此管辖）。
     */
    private const MAP = [
        '/'                 => 'page.index_enabled',
        '/packages/'        => 'page.packages_enabled',
        '/newpkg/'          => 'page.newpkg_enabled',
        '/fadian/'          => 'page.fadian_enabled',
        '/contact/'         => 'page.contact_enabled',
        '/login/'           => 'page.login_enabled',
        '/verify/'          => 'page.login_enabled',
        // 找回密码有意**独立**于 /login/ 开关：运营可能想关掉注册但仍允许老用户找回密码。
        '/reset/'           => 'page.reset_enabled',
        '/firmware/'        => 'page.download_enabled',
        '/dl/'              => 'page.download_enabled',
        '/store/'           => 'page.download_enabled',
    ];

    /** 这些前缀永远不受开关管辖（否则关掉页面会把 API 一起关掉，后台都进不去）。 */
    private const ALWAYS_ALLOW = [
        '/admin/',          // 后台必须永远可达，否则关错开关就自锁
        '/api/',            // 接口由各自的鉴权与配额管
        '/json/',
        '/static/',
        '/assets/',
        '/langs/',
        '/healthz',
        '/favicon.ico',
        '/manifest.webmanifest',
        '/service-worker.js',
        '/offline.html',
        '/robots.txt',
    ];

    /** 禁止被关闭的开关（关掉会把管理员锁在门外）。 */
    private const MUST_STAY_ON = [
        'page.index_enabled',
    ];

    public static function switchFor(string $path): ?string
    {
        $path = self::normalize($path);
        foreach (self::ALWAYS_ALLOW as $p) {
            if (str_starts_with($path, $p) || $path === rtrim($p, '/')) {
                return null;
            }
        }
        $best = null;
        $bestLen = -1;
        foreach (self::MAP as $prefix => $key) {
            $hit = $prefix === '/' ? true : str_starts_with($path, $prefix);
            if ($hit && strlen($prefix) > $bestLen) {
                $best = $key;
                $bestLen = strlen($prefix);
            }
        }
        return $best;
    }

    public static function enabled(string $path): bool
    {
        $key = self::switchFor($path);
        if ($key === null) {
            return true;
        }
        if (in_array($key, self::MUST_STAY_ON, true)) {
            return true;
        }
        return Settings::bool($key, true);
    }

    /**
     * 给一组开关键，判断是否全部开启（用于「能否注册」这类组合判定）。
     */
    public static function allOn(array $keys): bool
    {
        foreach ($keys as $k) {
            if (in_array($k, self::MUST_STAY_ON, true)) {
                continue;
            }
            if (!Settings::bool($k, true)) {
                return false;
            }
        }
        return true;
    }

    /**
     * 关闭时的响应方式（后台可选 404 / 跳转 / 提示）。
     * 返回 [状态码, 要跳转的地址|null, 提示文案|null]。
     */
    public static function denyResponse(): array
    {
        $mode = (string) Settings::get('page.disabled_behavior', '404');
        return match ($mode) {
            'redirect' => [$mode === 'redirect' ? 302 : 302, (string) Settings::get('page.disabled_redirect', '/'), null],
            'message'  => [503, null, (string) Settings::get('page.disabled_message', '该功能已暂时下线，请稍后再试。')],
            default    => [404, null, null],
        };
    }

    /** 归一化：去掉查询串、折叠重复斜杠、保证目录路径带尾斜杠。 */
    public static function normalize(string $path): string
    {
        $path = explode('?', $path, 2)[0];
        $path = preg_replace('#/+#', '/', $path) ?? $path;
        if ($path === '' ) {
            return '/';
        }
        return $path;
    }

    /** 供导航渲染：某个开关是否开启（前端据此隐藏入口）。 */
    public static function nav(): array
    {
        return [
            'home'     => Settings::bool('page.index_enabled', true),
            'packages' => Settings::bool('page.packages_enabled', true),
            'newpkg'   => Settings::bool('page.newpkg_enabled', true),
            'fadian'   => Settings::bool('page.fadian_enabled', true),
            'contact'  => Settings::bool('page.contact_enabled', true),
            'login'    => Settings::bool('page.login_enabled', true),
            'register' => Settings::bool('page.register_enabled', true),
            'reset'    => Settings::bool('page.reset_enabled', true),
            'download' => Settings::bool('page.download_enabled', true),
        ];
    }
}
