<?php
declare(strict_types=1);

namespace Kwrt;

/**
 * 密码找回（忘记密码）—— Python 版 app/reset.py 的 PHP 同构实现。
 *
 * 两版必须同构：它们共用同一个 `users.db`，任一版签发的令牌另一版必须能校验。
 * 因此这里逐条对齐 Python 的语义，而不是「另写一套差不多的」：
 *
 *   · 令牌高熵随机（32 字节 → 64 hex），库里**只存 SHA-256 摘要**
 *   · 一次性消费：`UPDATE ... WHERE used=0` 抢占，以 rowCount 判定归属
 *     （先 SELECT 再 UPDATE 是两个事务，并发点击会双花）
 *   · 有效期默认 2 小时，读 `mail.reset_ttl_hours`
 *   · 每人同时只保留 1 个有效令牌，重新申请即作废旧的
 *   · 消费成功 = 令牌已烧掉，与「口令是否改成功」解耦
 *
 * 为什么风险等级高于注册验证：拿到链接即可改口令 ≈ 账号接管。
 * 所以 TTL 更短、冷却更严、并且**绝不泄露账号是否存在**。
 */
final class Reset
{
    /** 令牌明文长度：32 字节随机数 → 64 个 hex 字符。 */
    public const TOKEN_BYTES = 32;

    /** 同一账号两次申请之间的冷却（秒）。 */
    public const COOLDOWN_SECONDS = 60;

    /** 令牌默认有效期（小时）；被 mail.reset_ttl_hours 覆盖。 */
    public const DEFAULT_TTL_HOURS = 2;

    /** 统一对外回复：账号不存在 / 未登记邮箱 / 冷却中，一律回这句。 */
    public const REPLY = '如果该账号存在且已绑定邮箱，重置邮件已经发出，请查收（含垃圾邮件目录）。';

    /** 建表（幂等）。索引由 Db::migrate() 在启动时按 Schema 一并建。 */
    public static function init(): void
    {
        Db::ensureTable('password_resets');
    }

    public static function ttlHours(): float
    {
        $v = Settings::get('mail.reset_ttl_hours', self::DEFAULT_TTL_HOURS);
        if (!is_numeric($v)) {
            return (float) self::DEFAULT_TTL_HOURS;
        }
        // 与 schema 的 min/max 呼应，避免一个手滑的 0 或 1e9 把功能变成砖
        return min(168.0, max(1.0, (float) $v));
    }

    private static function hash(string $token): string
    {
        return hash('sha256', $token);
    }

    /**
     * 签发找回令牌，返回**明文**令牌（仅此一次可见）。旧令牌立即作废。
     */
    public static function issue(string $username, string $email, ?float $ttlHours = null,
                                 string $ip = ''): string
    {
        self::init();
        $token = bin2hex(random_bytes(self::TOKEN_BYTES));
        $now = microtime(true);
        $ttl = ($ttlHours ?? self::ttlHours()) * 3600;

        Db::run('UPDATE password_resets SET used=1, used_at=? WHERE username=? AND used=0',
            [$now, $username]);
        Db::run('INSERT INTO password_resets'
            . '(token_hash, username, email, created, expires, used, ip) VALUES(?,?,?,?,?,0,?)',
            [self::hash($token), $username, $email, $now, $now + $ttl, $ip]);
        return $token;
    }

    /**
     * 校验令牌（**不消费**）。
     *
     * @return array{0:bool,1:string,2:string} [是否有效, 用户名, 原因]
     */
    public static function check(string $token): array
    {
        if ($token === '' || strlen($token) < 16) {
            return [false, '', '链接格式不正确'];
        }
        self::init();
        $row = Db::one('SELECT * FROM password_resets WHERE token_hash=?', [self::hash($token)]);
        if (!$row) {
            return [false, '', '重置链接无效'];
        }
        if ((int) $row['used'] === 1) {
            return [false, '', '该重置链接已被使用'];
        }
        if ((float) $row['expires'] < microtime(true)) {
            return [false, (string) $row['username'], '重置链接已过期，请重新申请'];
        }
        return [true, (string) $row['username'], ''];
    }

    /**
     * 校验并**原子消费**令牌。成功即令牌作废。
     *
     * @return array{0:bool,1:string,2:string}
     */
    public static function consume(string $token): array
    {
        [$ok, $username, $why] = self::check($token);
        if (!$ok) {
            return [false, $username, $why];
        }
        // ★ 原子认领：条件写在 UPDATE 里，靠 rowCount 定归属。
        //   并发点两下同一链接时只有一方得到 1，另一方拿到明确提示 —
        //   而不是「两边都改了口令」。
        $n = Db::run('UPDATE password_resets SET used=1, used_at=? WHERE token_hash=? AND used=0',
            [microtime(true), self::hash($token)]);
        if ($n !== 1) {
            return [false, $username, '该重置链接已被使用，请重新申请'];
        }
        return [true, $username, ''];
    }

    /** 作废该用户全部未用令牌（改密成功后、封禁/删除时调用）。 */
    public static function invalidate(string $username): void
    {
        self::init();
        Db::run('UPDATE password_resets SET used=1, used_at=? WHERE username=? AND used=0',
            [microtime(true), $username]);
    }

    /** 距上次申请过了多少秒；从未申请过返回 null。 */
    public static function secondsSinceLast(string $username): ?float
    {
        self::init();
        $v = Db::val('SELECT created FROM password_resets WHERE username=? ORDER BY id DESC LIMIT 1',
            [$username]);
        return $v === null ? null : max(0.0, microtime(true) - (float) $v);
    }

    /** 冷却剩余秒数；0 表示可以申请。 */
    public static function cooldownLeft(string $username): int
    {
        $age = self::secondsSinceLast($username);
        if ($age === null) {
            return 0;
        }
        return max(0, (int) (self::COOLDOWN_SECONDS - $age));
    }

    /** 拼重置链接（与 Python 版同为 `<base>/reset/?token=...`）。 */
    public static function buildLink(string $baseUrl, string $token): string
    {
        return rtrim($baseUrl, '/') . '/reset/?token=' . $token;
    }

    /** @return array{total:int,used:int,active:int,expired:int,last_24h:int} */
    public static function stats(): array
    {
        self::init();
        $now = microtime(true);
        return [
            'total'    => (int) Db::val('SELECT COUNT(*) FROM password_resets', [], 0),
            'used'     => (int) Db::val('SELECT COUNT(*) FROM password_resets WHERE used=1', [], 0),
            'active'   => (int) Db::val(
                'SELECT COUNT(*) FROM password_resets WHERE used=0 AND expires>?', [$now], 0),
            'expired'  => (int) Db::val(
                'SELECT COUNT(*) FROM password_resets WHERE used=0 AND expires<=?', [$now], 0),
            'last_24h' => (int) Db::val(
                'SELECT COUNT(*) FROM password_resets WHERE created>?', [$now - 86400], 0),
        ];
    }

    /** 清理超过保留期的历史令牌，返回删除行数。 */
    public static function cleanup(int $days = 30): int
    {
        self::init();
        return Db::run('DELETE FROM password_resets WHERE created < ?',
            [microtime(true) - $days * 86400]);
    }
}
