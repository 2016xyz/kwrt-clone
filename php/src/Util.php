<?php
/**
 * 通用工具：构建入参清洗、路径安全、哈希 ID。
 *
 * ★ params 清洗是**根控制**，不是辅助函数。Python 版曾因 target 零校验
 *   导致命令注入（target 直接进 shell 与 make 变量），修法是把字符白名单
 *   做成唯一出口。PHP 侧照同样口径实现，两版共用一套规则。
 */
declare(strict_types=1);

namespace Kwrt;

final class Util
{
    public const TARGET_RE  = '/^[A-Za-z0-9][A-Za-z0-9_.\-]*\/[A-Za-z0-9][A-Za-z0-9_.\-]*(\/[A-Za-z0-9][A-Za-z0-9_.\-]*)*$/';
    public const PROFILE_RE = '/^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$/';
    public const PKG_RE     = '/^[A-Za-z0-9][A-Za-z0-9._+\-]{0,79}$/';
    public const VERSION_RE = '/^[A-Za-z0-9][A-Za-z0-9.\-]{0,15}$/';
    public const HASH_RE    = '/^[A-Za-z0-9_\-]{1,64}$/';

    /** 构建入参校验：不合法即抛，绝不「尽量洗一洗继续」。 */
    public static function checkTarget(string $v): string
    {
        $v = trim($v);
        if ($v === '' || strlen($v) > 80 || !preg_match(self::TARGET_RE, $v)) {
            throw new \InvalidArgumentException('编译目标格式不正确');
        }
        return $v;
    }

    public static function checkProfile(string $v): string
    {
        $v = trim($v);
        if ($v === '' || !preg_match(self::PROFILE_RE, $v)) {
            throw new \InvalidArgumentException('型号标识格式不正确');
        }
        return $v;
    }

    public static function checkVersion(string $v): string
    {
        $v = trim($v);
        if ($v === '' || !preg_match(self::VERSION_RE, $v)) {
            throw new \InvalidArgumentException('固件版本格式不正确');
        }
        return $v;
    }

    /** 软件包名：支持前缀 `-` 表示移除。 */
    public static function checkPkg(string $v): string
    {
        $v = trim($v);
        $neg = str_starts_with($v, '-');
        $name = $neg ? substr($v, 1) : $v;
        if (!preg_match(self::PKG_RE, $name)) {
            throw new \InvalidArgumentException("软件包名不合法: {$name}");
        }
        return $neg ? '-' . $name : $name;
    }

    public static function packages(array $list, int $max = 400): array
    {
        $out = [];
        foreach ($list as $x) {
            if (!is_scalar($x)) {
                continue;
            }
            $n = self::checkPkg((string) $x);
            if (!in_array($n, $out, true)) {
                $out[] = $n;
            }
            if (count($out) >= $max) {
                break;
            }
        }
        return $out;
    }

    /** 请求 ID：内容寻址（同样的配置 → 同一个 hash，天然去重）。 */
    public static function requestHash(array $payload): string
    {
        ksort($payload);
        return substr(hash('sha256', json_encode($payload, JSON_UNESCAPED_UNICODE)), 0, 32);
    }

    /**
     * 路径归属校验：目标必须真的在 base 之内。
     * 用 realpath + 分隔符判断，绝不裸 startswith（'a/b' 与 'a/bc' 会被误判为同类）。
     */
    public static function under(string $base, string $path): ?string
    {
        $rb = realpath($base);
        if ($rb === false) {
            return null;
        }
        $rp = realpath($path);
        if ($rp === false) {
            // 还不存在时（准备创建），退化为规范化比较
            $rp = $rb . DIRECTORY_SEPARATOR . ltrim(str_replace('\\', '/', $path), '/');
        }
        $rb = rtrim($rb, DIRECTORY_SEPARATOR);
        if ($rp === $rb) {
            return null;
        }
        if (!str_starts_with($rp, $rb . DIRECTORY_SEPARATOR)) {
            return null;
        }
        return $rp;
    }

    /** 文件名清洗：只留纯文件名，拒路径分隔符与点目录。 */
    public static function safeFilename(string $fn): string
    {
        $fn = trim($fn);
        if ($fn === '' || $fn === '.' || $fn === '..') {
            return '';
        }
        if (str_contains($fn, '/') || str_contains($fn, '\\') || str_contains($fn, '\0')) {
            return '';
        }
        // 与 Python 版白名单一致：真实 OpenWrt 包名含 ~（git 哈希分隔符）
        if (!preg_match('/^[A-Za-z0-9][A-Za-z0-9._+~\-]{0,180}$/', $fn)) {
            return '';
        }
        return $fn;
    }

    /** 目录体积与文件数。 */
    public static function dirStat(string $dir): array
    {
        $bytes = 0;
        $files = 0;
        if (!is_dir($dir)) {
            return ['bytes' => 0, 'files' => 0, 'mtime' => 0];
        }
        $it = new \RecursiveIteratorIterator(
            new \RecursiveDirectoryIterator($dir, \FilesystemIterator::SKIP_DOTS),
            \RecursiveIteratorIterator::SELF_FIRST
        );
        $mtime = 0;
        foreach ($it as $f) {
            if ($f->isFile()) {
                $bytes += $f->getSize();
                $files++;
                $mtime = max($mtime, $f->getMTime());
            }
        }
        return ['bytes' => $bytes, 'files' => $files, 'mtime' => $mtime];
    }

    public static function rrmdir(string $dir): bool
    {
        if (!is_dir($dir)) {
            return false;
        }
        $it = new \RecursiveIteratorIterator(
            new \RecursiveDirectoryIterator($dir, \FilesystemIterator::SKIP_DOTS),
            \RecursiveIteratorIterator::CHILD_FIRST
        );
        foreach ($it as $f) {
            $f->isDir() ? @rmdir($f->getPathname()) : @unlink($f->getPathname());
        }
        return @rmdir($dir);
    }

    public static function clientUa(): string
    {
        return mb_substr((string) ($_SERVER['HTTP_USER_AGENT'] ?? ''), 0, 300);
    }
}
