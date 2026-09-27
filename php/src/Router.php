<?php
/**
 * 极简路由器。
 *
 * 为什么不引入框架：这个站要能丢进**任意共享主机**跑起来（这正是选 PHP 的理由）。
 * 依赖 composer / 框架会把这个优势抵消掉。这里只做必需的事：
 *   · 静态路径 + `{name}` 参数 + `{name:path}` 兜底段
 *   · 按最长匹配优先，避免 /a 抢掉 /a/b
 *   · 页面开关、封禁、CSRF、安全头统一在前置阶段处理（一处生效，不可能漏）
 */
declare(strict_types=1);

namespace Kwrt;

final class Router
{
    /** @var array<int, array{method:string, regex:string, keys:array<int,string>, handler:array|callable, path:string}> */
    private array $routes = [];

    /**
     * 注册路由。
     *
     * $handler 支持两种写法：
     *   · `[Controller::class, 'method']` —— 控制器方法**不必是 static**，
     *     分发时由路由器实例化。这是刻意选择：控制器里用 $this 更自然，
     *     而 PHP 的 `callable` 类型不接受「类名 + 非静态方法」这种数组形式
     *     （会被判定为不可调用），所以这里自己解析而不是靠类型系统。
     *   · 任意闭包
     */
    public function add(string $method, string $path, array|callable $handler): void
    {
        $keys = [];
        $regex = preg_replace_callback(
            '#\{([a-zA-Z_][a-zA-Z0-9_]*)(?::([a-z]+))?\}#',
            static function ($m) use (&$keys) {
                $keys[] = $m[1];
                // ★ $m[2] 是可选捕获组，路由里没写 `:path` 时**根本不存在**，
                //   直接比较会每注册一条路由就产生一条 "Undefined array key 2" 警告
                //   （实测日志被刷屏）。必须用 ?? 兜底。
                return ($m[2] ?? '') === 'path'
                    ? '(?P<' . $m[1] . '>.+)'
                    : '(?P<' . $m[1] . '>[^/]+)';
            },
            $path
        );
        $this->routes[] = [
            'method'  => strtoupper($method),
            'regex'   => '#^' . $regex . '$#',
            'keys'    => $keys,
            'handler' => $handler,
            'path'    => $path,
        ];
    }

    public function get(string $p, array|callable $h): void  { $this->add('GET', $p, $h); }
    public function post(string $p, array|callable $h): void { $this->add('POST', $p, $h); }
    public function del(string $p, array|callable $h): void  { $this->add('DELETE', $p, $h); }

    /** 找匹配的处理器；返回 [handler, params] 或 null。 */
    public function match(string $method, string $path): ?array
    {
        $path = Pages::normalize($path);
        $method = strtoupper($method);
        // 最长路径优先：把 path 字面量长度降序排（{...} 视为 0 长度做近似）
        $cands = [];
        foreach ($this->routes as $r) {
            if ($r['method'] !== $method && !($method === 'HEAD' && $r['method'] === 'GET')) {
                continue;
            }
            if (preg_match($r['regex'], $path, $m)) {
                $params = [];
                foreach ($r['keys'] as $k) {
                    $params[$k] = $m[$k] ?? '';
                }
                $cands[] = [$r, $params];
            }
        }
        if (!$cands) {
            return null;
        }
        // 优先级：**字面量字符多者优先**，字面量相同则通配更少者优先。
        //
        // ★ 不能直接按整个路径串的长度比 —— 实测踩到：
        //     /dl/t/{token}      (13 字符)
        //     /dl/{path:path}    (14 字符)
        //   后者「更长」于是把前者盖掉，/dl/t/<令牌> 全部走进了通配分支，
        //   被 302 到上游镜像站，下载链接集体失效（表现为 403/404）。
        // 按字面量长度比：/dl/t/ = 6 > /dl/ = 4，令牌路由正确优先。
        $score = static function (array $r): array {
            $literal = strlen(preg_replace('/\{[^}]*\}/', '', $r['path']) ?? '');
            $wild = substr_count($r['path'], ':path');
            return [$literal, -$wild];
        };
        usort($cands, static function ($a, $b) use ($score) {
            [$la, $wa] = $score($a[0]);
            [$lb, $wb] = $score($b[0]);
            return [$lb, $wb] <=> [$la, $wa];
        });
        return [self::resolve($cands[0][0]['handler']), $cands[0][1], $cands[0][0]['path']];
    }

    /**
     * 把 [类名, 方法] 解析为可调用闭包（顺手实例化控制器）。
     * 单例缓存实例，避免同一请求里重复 new。
     */
    private static function resolve(array|callable $h): callable
    {
        if (is_array($h) && count($h) === 2 && is_string($h[0])) {
            /** @var array<string, object> $inst */
            static $inst = [];
            $cls = $h[0];
            $inst[$cls] ??= new $cls();
            $obj = $inst[$cls];
            $method = $h[1];
            if (!method_exists($obj, $method)) {
                throw new \RuntimeException("控制器方法不存在: {$cls}::{$method}");
            }
            return static fn(array $params) => $obj->{$method}($params);
        }
        return $h;
    }

    /** 已允许的方法（用于 405）。 */
    public function allowedFor(string $path): array
    {
        $path = Pages::normalize($path);
        $out = [];
        foreach ($this->routes as $r) {
            if (preg_match($r['regex'], $path)) {
                $out[] = $r['method'];
            }
        }
        return array_values(array_unique($out));
    }
}
