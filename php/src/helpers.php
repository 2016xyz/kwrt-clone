<?php
/**
 * 全局辅助函数。刻意保持很短：模板里能用到的都在这。
 */
declare(strict_types=1);

use Kwrt\Auth;
use Kwrt\Net;
use Kwrt\Pages;
use Kwrt\Settings;

if (!function_exists('e')) {
    /** HTML 转义（模板里输出任何变量都必须过它）。 */
    function e(mixed $v): string
    {
        return htmlspecialchars((string) ($v ?? ''), ENT_QUOTES | ENT_SUBSTITUTE, 'UTF-8');
    }
}

if (!function_exists('raw')) {
    /** 显式放行：只在内容确实已经过安全处理时才用。 */
    function raw(mixed $v): string
    {
        return (string) ($v ?? '');
    }
}

if (!function_exists('attr')) {
    /** 输出到 HTML 属性里（在 e() 基础上额外处理换行）。 */
    function attr(mixed $v): string
    {
        return htmlspecialchars((string) ($v ?? ''), ENT_QUOTES | ENT_SUBSTITUTE, 'UTF-8');
    }
}

if (!function_exists('js')) {
    /** 安全地把值塞进 <script> 里的 JSON 字面量。 */
    function js(mixed $v): string
    {
        return json_encode(
            $v,
            JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES | JSON_HEX_TAG | JSON_HEX_AMP
                | JSON_HEX_APOS | JSON_HEX_QUOT
        ) ?: 'null';
    }
}

if (!function_exists('setting')) {
    function setting(string $k, mixed $d = null): mixed
    {
        return Settings::get($k, $d);
    }
}

if (!function_exists('t_bool')) {
    function t_bool(string $k, bool $d = true): bool
    {
        return Settings::bool($k, $d);
    }
}

if (!function_exists('base_url')) {
    function base_url(string $p = '/'): string
    {
        return Net::url($p);
    }
}

if (!function_exists('asset')) {
    function asset(string $p): string
    {
        return Net::asset($p);
    }
}

if (!function_exists('csrf')) {
    function csrf(): string
    {
        return Auth::csrfToken();
    }
}

if (!function_exists('csrf_field')) {
    function csrf_field(): string
    {
        return '<input type="hidden" name="_csrf" value="' . e(Auth::csrfToken()) . '">';
    }
}

if (!function_exists('current_user')) {
    function current_user(): ?array
    {
        return Auth::currentUser();
    }
}

if (!function_exists('is_admin')) {
    function is_admin(): bool
    {
        return Auth::isAdmin();
    }
}

if (!function_exists('is_sponsor')) {
    function is_sponsor(): bool
    {
        return Auth::isSponsor();
    }
}

if (!function_exists('nav_on')) {
    function nav_on(): array
    {
        return Pages::nav();
    }
}

if (!function_exists('bytes_h')) {
    /** 人类可读的字节数。 */
    function bytes_h(float|int $n): string
    {
        $n = (float) $n;
        $u = ['B', 'KB', 'MB', 'GB', 'TB'];
        $i = 0;
        while ($n >= 1024 && $i < count($u) - 1) {
            $n /= 1024;
            $i++;
        }
        return ($i === 0 ? (string) (int) $n : number_format($n, 1)) . ' ' . $u[$i];
    }
}

if (!function_exists('ts_h')) {
    /** 时间戳 → 本地时间字符串。 */
    function ts_h(float|int|null $t, string $fmt = 'Y/m/d H:i:s'): string
    {
        if (!$t) {
            return '—';
        }
        return date($fmt, (int) $t);
    }
}

if (!function_exists('ago_h')) {
    function ago_h(float|int|null $t): string
    {
        if (!$t) {
            return '—';
        }
        $d = time() - (int) $t;
        if ($d < 60) {
            return $d . ' 秒前';
        }
        if ($d < 3600) {
            return intdiv($d, 60) . ' 分钟前';
        }
        if ($d < 86400) {
            return intdiv($d, 3600) . ' 小时前';
        }
        return intdiv($d, 86400) . ' 天前';
    }
}

if (!function_exists('kwrt_header_set')) {
    /** 判断某个响应头是否已被设置（用于避免兜底覆盖控制器声明的类型）。 */
    function kwrt_header_set(string $name): bool
    {
        foreach (headers_list() as $h) {
            if (stripos($h, $name . ':') === 0) {
                return true;
            }
        }
        return false;
    }
}

if (!function_exists('json_out')) {
    /**
     * 统一 JSON 响应，**发出后立即结束请求**。
     *
     * 为什么要 exit：控制器方法普遍声明了 `: string` 返回类型（正常路径要
     * 返回渲染好的 HTML），而 JSON 分支是「直接吐响应」。若只是 echo 然后
     * 往下走，PHP 会因为「声明了 string 却没 return」抛 TypeError。
     * 让 json_out 自己终结请求，就把这个坑收在了**一个点**上，
     * 不用在几十处端点里逐个补 `return '';`（漏一个就 500）。
     */
    function json_out(mixed $data, int $status = 200): never
    {
        if (!headers_sent()) {
            http_response_code($status);
            header('Content-Type: application/json; charset=utf-8');
        }
        echo json_encode($data, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
        exit;
    }
}

if (!function_exists('input')) {
    /** 取请求入参：$_POST → $_GET → JSON body。 */
    function input(string $k, mixed $d = null): mixed
    {
        if (array_key_exists($k, $_POST)) {
            return $_POST[$k];
        }
        if (array_key_exists($k, $_GET)) {
            return $_GET[$k];
        }
        $v = json_body();
        return $v[$k] ?? $d;
    }
}

if (!function_exists('json_body')) {
    /** 惰性解析 JSON 请求体。 */
    function json_body(): array
    {
        static $body = null;
        if ($body !== null) {
            return $body;
        }
        $ct = $_SERVER['CONTENT_TYPE'] ?? '';
        if (stripos($ct, 'application/json') === false) {
            return $body = [];
        }
        $raw = file_get_contents('php://input');
        $j = json_decode((string) $raw, true);
        return $body = is_array($j) ? $j : [];
    }
}

if (!function_exists('is_post')) {
    function is_post(): bool
    {
        return strtoupper($_SERVER['REQUEST_METHOD'] ?? 'GET') === 'POST';
    }
}

if (!function_exists('redirect')) {
    function redirect(string $to, int $code = 302): never
    {
        header('Location: ' . $to, true, $code);
        exit;
    }
}
