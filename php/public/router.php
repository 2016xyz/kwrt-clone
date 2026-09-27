<?php
/**
 * PHP 内置服务器（php -S）的路由垫片。
 *
 * 为什么需要它：`php -S host:port -t docroot` **不带垫片**时，
 * 内置服务器会先当成静态文件查找，找不到就直接 404 ——
 * index.php 根本没机会运行，于是 /manifest.webmanifest、/healthz
 * 这类「没有对应实体文件」的路径全部 404。
 *
 * 用法（README 的「PHP 版启动」一节与 run-php.sh 都用这个）：
 *     php -S 0.0.0.0:8080 -t php/public php/public/router.php
 *
 * Apache / Nginx 上不需要它，各自用 .htaccess / try_files 达到同样效果。
 */
declare(strict_types=1);

$path = parse_url((string) ($_SERVER['REQUEST_URI'] ?? '/'), PHP_URL_PATH) ?: '/';
$file = __DIR__ . $path;

// 真实存在的静态文件交给内置服务器直接吐（更快，也不走会话逻辑）
if ($path !== '/' && is_file($file)) {
    return false;
}

require __DIR__ . '/index.php';
