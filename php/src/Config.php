<?php
/**
 * 站点引导配置（config.json）+ 路径解析。
 *
 * 与 Python 版的差异：Python 版用 `ROOT = 项目根目录（相对当前工作目录解析）`，
 * 因此 systemd 必须设 WorkingDirectory，否则链路静默失效（README 有记录）。
 * PHP 版改成**从 __DIR__ 反推绝对路径**，不再依赖 cwd —— 这是 PHP 侧的一个真实改进：
 * 部署在共享主机 / 任意 cwd 下都不会再出现「相对路径失效」。
 */
declare(strict_types=1);

namespace Kwrt;

final class Config
{
    private static ?array $cfg = null;
    private static ?string $root = null;

    /** 项目根目录（php/ 的上一级），绝对路径。 */
    public static function root(): string
    {
        if (self::$root === null) {
            // 入口（index.php / install.php）已用 php/public/_boot.php 探测过布局
            // 并定义 KWRT_ROOT；CLI 脚本不经入口，退化为「php/src 的上一级」。
            self::$root = defined('KWRT_ROOT') ? (string) KWRT_ROOT : dirname(__DIR__, 2);
        }
        return self::$root;
    }

    public static function path(string ...$parts): string
    {
        return self::root() . DIRECTORY_SEPARATOR . implode(DIRECTORY_SEPARATOR, $parts);
    }

    /** 数据库文件：优先环境变量 KWRT_DB，其次 <root>/users.db。 */
    public static function dbPath(): string
    {
        $env = getenv('KWRT_DB');
        if (is_string($env) && $env !== '') {
            return $env;
        }
        return self::path('users.db');
    }

    /**
     * 数据库驱动：sqlite（默认）| mysql。
     *
     * 优先级：环境变量 KWRT_DB_DRIVER > config.local.json 的 db.driver > sqlite。
     *
     * 为什么默认仍是 sqlite：Python 版只能读 SQLite，两版共用一个 users.db
     * 是本项目的一条既有契约（见 reports/05）。切到 MySQL 就等于**放弃与
     * Python 版共库**，必须由管理员显式选择，不能替他决定。
     */
    public static function dbDriver(): string
    {
        $env = getenv('KWRT_DB_DRIVER');
        if (is_string($env) && $env !== '') {
            $d = strtolower(trim($env));
            return $d === 'mysql' ? 'mysql' : 'sqlite';
        }
        $d = strtolower(trim((string) self::get('db.driver', 'sqlite')));
        return $d === 'mysql' ? 'mysql' : 'sqlite';
    }

    /**
     * MySQL 连接参数。
     *
     * 取值顺序：环境变量 > config.local.json 的 db.mysql.* > 默认。
     * 环境变量更适合宝塔/容器部署（不必把口令写进版本库里的文件）。
     */
    public static function mysql(): array
    {
        $c = self::get('db.mysql', []);
        $c = is_array($c) ? $c : [];
        $env = static function (string $k, string $def = ''): string {
            $v = getenv($k);
            return (is_string($v) && $v !== '') ? $v : $def;
        };
        return [
            'host'    => $env('KWRT_MYSQL_HOST',    (string) ($c['host'] ?? '127.0.0.1')),
            'port'    => (int) $env('KWRT_MYSQL_PORT', (string) ($c['port'] ?? '3306')),
            // 库名不能带反引号/分号 —— 它会被拼进 DSN 与 CREATE DATABASE 语句
            'dbname'  => preg_replace('/[^A-Za-z0-9_$]/', '', (string) $env('KWRT_MYSQL_DB', (string) ($c['dbname'] ?? 'kwrt'))) ?: 'kwrt',
            'user'    => $env('KWRT_MYSQL_USER',    (string) ($c['user'] ?? 'kwrt')),
            'pass'    => $env('KWRT_MYSQL_PASS',    (string) ($c['pass'] ?? '')),
            'charset' => 'utf8mb4',
        ];
    }

    /**
     * 清掉配置缓存。**仅供安装向导与测试使用** ——
     * 运行时改配置后必须让下一次读取重新落盘，否则向导写完
     * config.local.json 后，同一次请求里读到的还是旧值。
     */
    public static function reset(): void
    {
        self::$cfg = null;
    }

    /** config.json 内容（顶层结构 site / builder / sponsor / server）。 */
    public static function all(): array
    {
        if (self::$cfg !== null) {
            return self::$cfg;
        }
        $f = self::path('config.json');
        $c = [];
        if (is_file($f)) {
            $raw = file_get_contents($f);
            if ($raw !== false) {
                $j = json_decode($raw, true);
                if (is_array($j)) {
                    $c = $j;
                }
            }
        }
        // 允许用 config.local.json 覆盖（不进版本控制，用于部署差异）
        $lf = self::path('config.local.json');
        if (is_file($lf)) {
            $raw = file_get_contents($lf);
            if ($raw !== false) {
                $j = json_decode($raw, true);
                if (is_array($j)) {
                    $c = array_replace_recursive($c, $j);
                }
            }
        }
        self::$cfg = $c;
        return $c;
    }

    /** 点号取值：Config::get('site.name', '默认')。 */
    public static function get(string $dotted, mixed $default = null): mixed
    {
        $cur = self::all();
        foreach (explode('.', $dotted) as $k) {
            if (!is_array($cur) || !array_key_exists($k, $cur)) {
                return $default;
            }
            $cur = $cur[$k];
        }
        return $cur;
    }

    public static function env(string $name, mixed $default = null): mixed
    {
        $v = getenv($name);
        return ($v === false || $v === '') ? $default : $v;
    }
}
