<?php
/**
 * PHP 模板渲染（替代原来的 Vue 单页 + 静态 HTML）。
 *
 * 设计取舍：
 *   · 模板就是普通 .php 文件，不需要编译步骤、不依赖 composer —— 直接部署
 *   · 变量默认 **显式转义**：模板里必须写 `<?= e($x) ?>`；要输出原始 HTML 才写 `raw()`
 *     与邮件模板同一条纪律：默认安全，放行要显式
 *   · 布局用「先渲染内容、再套外层」的两段式，避免输出缓冲的黑魔法
 */
declare(strict_types=1);

namespace Kwrt;

final class View
{
    private static string $dir = '';
    private static array $shared = [];

    public static function init(string $dir): void
    {
        self::$dir = rtrim($dir, '/');
    }

    public static function share(string $k, mixed $v): void
    {
        self::$shared[$k] = $v;
    }

    public static function shared(): array
    {
        return self::$shared;
    }

    /** 渲染模板文件，返回 HTML 字符串。 */
    public static function template(string $name, array $data = []): string
    {
        // 模板名白名单：只允许字母数字/_/-/. 分段 —— 先校验再碰文件系统
        if (!preg_match('#^[a-zA-Z0-9_\-/]+$#', $name)) {
            throw new \RuntimeException("模板名非法: {$name}");
        }
        $file = self::$dir . '/' . $name . '.php';
        if (!is_file($file)) {
            throw new \RuntimeException("模板不存在: {$name}");
        }
        $vars = array_merge(self::$shared, $data);
        extract($vars, EXTR_SKIP);
        ob_start();
        try {
            include $file;
        } catch (\Throwable $e) {
            ob_end_clean();
            throw $e;
        }
        return (string) ob_get_clean();
    }

    /** 渲染整页：内容模板 + 布局。 */
    public static function page(string $name, array $data = [], string $layout = 'layout'): string
    {
        $data['__view_name'] = $name;
        $content = self::template($name, $data);
        return self::template($layout, array_merge($data, ['content' => $content]));
    }

    /** 直接把整页输出出去。 */
    public static function send(string $name, array $data = [], string $layout = 'layout'): void
    {
        echo self::page($name, $data, $layout);
    }

    /** 局部片段（AJAX 返回 HTML 片段时用）。 */
    public static function partial(string $name, array $data = []): string
    {
        return self::template('partials/' . $name, $data);
    }

    public static function exists(string $name): bool
    {
        return is_file(self::$dir . '/' . $name . '.php');
    }
}
