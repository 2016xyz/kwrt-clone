<?php
/**
 * SQLite 访问层（PDO）。
 *
 * 与 Python 版共用同一个 users.db —— 表结构、字段名、类型完全一致，
 * 因此两个实现可以指向同一个库文件，也可以互相接管。
 *
 * 注意与 Python 版的对应关系：
 *   Python 的 `with db() as c:` 在原实现里只 commit 不 close，
 *   实测每调用一次泄漏一个 fd（线性 1:1，详见 reports/04）。
 *   PHP 侧用 PDO 长连接 + 显式事务包一层，天然不存在该问题：
 *   PDO 连接在请求结束时由 PHP 释放，不需要手工 close。
 */
declare(strict_types=1);

namespace Kwrt;

use PDO;
use PDOException;
use RuntimeException;

final class Db
{
    private static ?PDO $pdo = null;
    private static ?string $driver = null;

    /**
     * 丢弃缓存的连接与驱动。**仅供安装向导与测试使用** ——
     * 向导会在同一请求里先试连、再落盘、再正式连，必须能重来一次。
     */
    public static function reset(): void
    {
        self::$pdo = null;
        self::$driver = null;
    }

    /** 当前驱动：sqlite | mysql。 */
    public static function driver(): string
    {
        return self::$driver ??= Config::dbDriver();
    }

    /** 取得进程内共享的 PDO 连接（PHP-FPM 下每个 worker 各一份）。 */
    public static function pdo(): PDO
    {
        if (self::$pdo instanceof PDO) {
            return self::$pdo;
        }
        $fresh = self::driver() === 'sqlite' ? !file_exists(Config::dbPath()) : false;

        try {
            $pdo = self::driver() === 'mysql' ? self::connectMysql() : self::connectSqlite();
        } catch (PDOException $e) {
            throw new RuntimeException('数据库连接失败: ' . $e->getMessage(), 0, $e);
        }

        self::$pdo = $pdo;
        self::migrate($pdo);
        // 引导默认值：SQLite 看「文件是否新建」，MySQL 没有文件概念，
        // 看「settings 表是否为空」。用 $pdo 直接查，不能走 self::val()
        // ——那会回到 self::pdo()，而此时 self::$pdo 还没赋值，会无限递归。
        $needBootstrap = $fresh;
        if (!$needBootstrap) {
            try {
                $n = $pdo->query('SELECT COUNT(*) FROM `settings`')->fetchColumn();
                $needBootstrap = ((int) $n) === 0;
            } catch (PDOException) {
                $needBootstrap = true;   // 表刚建出来但查不到，按需要引导处理
            }
        }
        if ($needBootstrap) {
            Settings::bootstrap($pdo);
        }
        return self::$pdo;
    }

    private static function connectSqlite(): PDO
    {
        $path = Config::dbPath();
        $dir  = dirname($path);
        if (!is_dir($dir) && !@mkdir($dir, 0755, true) && !is_dir($dir)) {
            throw new RuntimeException("无法创建数据目录: {$dir}");
        }
        if (!is_dir($dir) || !is_writable($dir)) {
            throw new RuntimeException(
                "数据目录不可写: {$dir}\n" .
                "宝塔面板下请确认：站点目录属主为运行 PHP 的用户（常见 www），" .
                "且「防跨站攻击(open_basedir)」未把上级目录排除。");
        }
        $pdo = new PDO('sqlite:' . $path, null, null, [
            PDO::ATTR_ERRMODE            => PDO::ERRMODE_EXCEPTION,
            PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
            // 用真实预处理语句，杜绝 SQL 拼接（与 Python 版全部 ? 参数化对齐）
            PDO::ATTR_EMULATE_PREPARES   => false,
        ]);
        // WAL + 忙等：本站是「多请求并发读、少量写」的形态。
        // busy_timeout 必须设，否则并发构建写入时会直接抛 "database is locked"。
        $pdo->exec('PRAGMA journal_mode=WAL');
        $pdo->exec('PRAGMA busy_timeout=8000');
        $pdo->exec('PRAGMA foreign_keys=ON');
        $pdo->exec('PRAGMA synchronous=NORMAL');
        return $pdo;
    }

    /**
     * MySQL 连接。
     *
     * 关键点（宝塔/共享主机上最容易踩的几处）：
     *   · charset 必须显式 utf8mb4 —— 否则中文站点名/公告存进去变问号；
     *   · 用 `SET NAMES` 之外的 sql_mode 保持宽容：宝塔默认可能开
     *     STRICT_TRANS_TABLES，写入超长 TEXT 会直接报错而不是截断；
     *   · 不自动建库（需要 CREATE 权限，共享主机常不给）——
     *     库不存在时给出**可照抄的 SQL**，让人去面板里建。
     */
    private static function connectMysql(): PDO
    {
        $c = Config::mysql();
        $dsn = sprintf('mysql:host=%s;port=%d;dbname=%s;charset=%s',
            $c['host'], $c['port'], $c['dbname'], $c['charset']);
        try {
            $pdo = new PDO($dsn, $c['user'], $c['pass'], [
                PDO::ATTR_ERRMODE            => PDO::ERRMODE_EXCEPTION,
                PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
                PDO::ATTR_EMULATE_PREPARES   => false,
                PDO::ATTR_STRINGIFY_FETCHES  => false,
                PDO::MYSQL_ATTR_INIT_COMMAND => 'SET NAMES ' . $c['charset'],
            ]);
        } catch (PDOException $e) {
            $msg = $e->getMessage();
            if (stripos($msg, 'Unknown database') !== false) {
                throw new RuntimeException(
                    "MySQL 里没有数据库 `{$c['dbname']}`。请在宝塔面板「数据库」里新建一个，"
                    . "或手工执行：\n"
                    . "CREATE DATABASE `{$c['dbname']}` DEFAULT CHARACTER SET utf8mb4 "
                    . "COLLATE utf8mb4_unicode_ci;", 0, $e);
            }
            if (stripos($msg, 'Access denied') !== false) {
                throw new RuntimeException(
                    "MySQL 账号或口令不对（用户 {$c['user']}@{$c['host']}）。"
                    . "宝塔面板「数据库」页可重置口令；环境变量 KWRT_MYSQL_USER / KWRT_MYSQL_PASS 可覆盖。",
                    0, $e);
            }
            throw $e;
        }
        return $pdo;
    }

    /**
     * 幂等建表。
     *
     * DDL 取自 Schema 真源：SQLite 用 Schema::SQLITE（与 Python 版逐字对齐，
     * 保证两版能操作同一个 users.db），MySQL 用 Schema::MYSQL（由
     * php/scripts/gen_mysql_schema.py 从同一份真源派生）。
     */
    public static function migrate(PDO $pdo): void
    {
        $isMysql = self::driver() === 'mysql';
        $tables  = $isMysql ? Schema::MYSQL : Schema::SQLITE;
        $indexes = $isMysql ? Schema::INDEX_MYSQL : Schema::INDEX_SQLITE;

        foreach ($tables as $sql) {
            $pdo->exec($sql);
        }

        foreach ($indexes as $sql) {
            // MySQL 不支持 CREATE INDEX IF NOT EXISTS（SQLite 支持）。
            // 先试一次；重复建索引抛 1061，那正是幂等迁移的**预期**结果。
            try {
                $pdo->exec($sql);
            } catch (PDOException $e) {
                // MySQL 不支持 CREATE INDEX IF NOT EXISTS（SQLite 支持），
                // 重复建索引会抛 1061 Duplicate key name —— 这是幂等迁移的**预期**情况，
                // 不是错误。只吞这一种，其它照常抛。
                if (!$isMysql || !preg_match('/Duplicate key name|already exists/i', $e->getMessage())) {
                    throw $e;
                }
            }
        }
    }


    public static function ensureTable(string $table): void
    {
        $isMysql = self::driver() === 'mysql';
        $src = $isMysql ? Schema::MYSQL : Schema::SQLITE;
        if (!isset($src[$table])) {
            throw new RuntimeException("Schema 里没有表定义: {$table}");
        }
        self::pdo()->exec($src[$table]);
    }

    /**
     * UPSERT（存在即更新）。方言差异集中在这一处。
     *
     * SQLite: schema 里写的是 `ON CONFLICT(col) DO UPDATE SET ... excluded.x`
     * MySQL : 对应语法是 `ON DUPLICATE KEY UPDATE x=VALUES(x)`
     *
     * 之所以做成助手而不是让调用方自己拼：原先 8 处 `ON CONFLICT` 是内联 SQL，
     * 换成 MySQL 得逐处重写 —— 漏掉任何一处都是「SQLite 下正常、MySQL 下报错」，
     * 而且只在真正走到那条分支时才暴露。集中一处才验得完。
     *
     * @param string               $table   逻辑表名
     * @param array<string,mixed>  $data    列 => 值
     * @param string[]             $keyCols 冲突判定的键列（SQLite 需要，MySQL 靠唯一索引）
     */
    public static function upsert(string $table, array $data, array $keyCols = []): void
    {
        $cols = array_keys($data);
        $ph   = implode(',', array_fill(0, count($cols), '?'));
        $collist = implode(',', array_map(fn($c) => "`{$c}`", $cols));
        $args = array_values($data);

        if (self::driver() === 'mysql') {
            $upd = implode(',', array_map(fn($c) => "`{$c}`=VALUES(`{$c}`)", $cols));
            $sql = "INSERT INTO `{$table}` ({$collist}) VALUES ({$ph}) "
                 . "ON DUPLICATE KEY UPDATE {$upd}";
        } else {
            $conflict = $keyCols ? ' ON CONFLICT(' . implode(',', $keyCols) . ')' : '';
            $upd = implode(',', array_map(fn($c) => "`{$c}`=excluded.`{$c}`", $cols));
            $sql = "INSERT INTO `{$table}` ({$collist}) VALUES ({$ph})"
                 . "{$conflict} DO UPDATE SET {$upd}";
        }
        self::run($sql, $args);
    }

    /** INSERT，冲突则忽略（SQLite: INSERT OR IGNORE / MySQL: INSERT IGNORE）。 */
    public static function insertIgnore(string $table, array $data): void
    {
        $cols = array_keys($data);
        $ph   = implode(',', array_fill(0, count($cols), '?'));
        $collist = implode(',', array_map(fn($c) => "`{$c}`", $cols));
        $kw = self::driver() === 'mysql' ? 'INSERT IGNORE' : 'INSERT OR IGNORE';
        self::run("{$kw} INTO `{$table}` ({$collist}) VALUES ({$ph})",
                  array_values($data));
    }

    /** 取单行。 */
    public static function one(string $sql, array $args = []): ?array
    {
        $st = self::pdo()->prepare($sql);
        $st->execute($args);
        $row = $st->fetch();
        return $row === false ? null : $row;
    }

    /** 取多行。 */
    public static function all(string $sql, array $args = []): array
    {
        $st = self::pdo()->prepare($sql);
        $st->execute($args);
        return $st->fetchAll();
    }

    /** 取标量。 */
    public static function val(string $sql, array $args = [], mixed $default = null): mixed
    {
        $st = self::pdo()->prepare($sql);
        $st->execute($args);
        $v = $st->fetchColumn();
        return $v === false ? $default : $v;
    }

    /** 执行写语句，返回受影响行数。 */
    public static function run(string $sql, array $args = []): int
    {
        $st = self::pdo()->prepare($sql);
        $st->execute($args);
        return $st->rowCount();
    }

    /**
     * 事务包装。回调抛异常即回滚。
     * Python 版对「先声明后执行」的竞态点（结算/退款/管理员改单）用原子 claim，
     * PHP 侧同样收在这一个口子里，避免各处自己 BEGIN/COMMIT 漏掉回滚。
     */
    public static function tx(callable $fn): mixed
    {
        $pdo = self::pdo();
        // 已在事务里则直接执行（支持嵌套调用）
        if ($pdo->inTransaction()) {
            return $fn($pdo);
        }
        $pdo->beginTransaction();
        try {
            $out = $fn($pdo);
            $pdo->commit();
            return $out;
        } catch (\Throwable $e) {
            if ($pdo->inTransaction()) {
                $pdo->rollBack();
            }
            throw $e;
        }
    }
}
