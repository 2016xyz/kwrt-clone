<?php
/**
 * 限时下载令牌。
 *
 * 与 Python 版同设计：令牌本身是随机串，签名用 HMAC 防止伪造；
 * 令牌有 TTL、可选最大命中次数、可吊销。产物文件不直接暴露路径，
 * 一律经 /dl/t/{token} 校验后再把文件吐出去。
 */
declare(strict_types=1);

namespace Kwrt;

final class Download
{
    private const SECRET_KEY = 'download.hmac_secret';

    /** 取（或首次生成）HMAC 密钥，存库，不进版本控制。 */
    private static function secret(): string
    {
        $s = Db::val('SELECT `value` FROM settings WHERE `key`=?', [self::SECRET_KEY]);
        if (is_string($s) && strlen($s) >= 32) {
            return $s;
        }
        $s = bin2hex(random_bytes(32));
        Db::upsert('settings',
            ['key' => self::SECRET_KEY, 'value' => $s, 'updated' => microtime(true)], ['key']);
        return $s;
    }

    public static function ttlHours(): int
    {
        return max(1, (int) Settings::get('download.link_ttl_hours', 72));
    }

    /** 签发一个令牌：`<random>.<expires>.<sig>`。 */
    public static function issue(string $requestHash, string $username, string $filename,
                              ?int $ttlHours = null, ?int $maxHits = null): string
 {
     $ttl = $ttlHours ?? self::ttlHours();
     // ★ 必须读 download.max_hits。
     //   原先 $maxHits 默认 0，而两个调用方（Engine::128 / AdminController:425）
     //   都不传它 → 生成出来的下载链接**永远不限次数**，
     //   管理员在后台设的「单链接最多下载 N 次」被静默忽略
     //   （Python 侧是生效的）。这是安全相关设置，不能默默失效。
     $maxHits = $maxHits ?? max(0, (int) Settings::get('download.max_hits', 0));
        $now = microtime(true);
        $exp = $now + $ttl * 3600;
        $rand = bin2hex(random_bytes(16));
        $sig = substr(hash_hmac('sha256', $rand . '.' . (int) $exp, self::secret()), 0, 32);
        $token = $rand . '.' . (int) $exp . '.' . $sig;
        Db::run('INSERT INTO dl_tokens(token, request_hash, username, filename, created, expires, '
            . 'max_hits, hits, revoked) VALUES(?,?,?,?,?,?,?,0,0)',
            [$token, $requestHash, $username, $filename, $now, $exp, $maxHits]);
        return $token;
    }

    /** @return array{0:?array,1:string} [令牌行, 失败原因] */
    public static function verify(string $token, bool $consume = true): array
    {
        if ($token === '' || strlen($token) > 200) {
            return [null, '令牌格式不正确'];
        }
        $parts = explode('.', $token);
        if (count($parts) !== 3) {
            return [null, '令牌格式不正确'];
        }
        [$rand, $exp, $sig] = $parts;
        if (!ctype_digit($exp)) {
            return [null, '令牌格式不正确'];
        }
        // 先验签，再看库 —— 伪造的令牌连库都不用查
        $want = substr(hash_hmac('sha256', $rand . '.' . (int) $exp, self::secret()), 0, 32);
        if (!hash_equals($want, (string) $sig)) {
            return [null, '令牌签名无效'];
        }
        $row = Db::one('SELECT * FROM dl_tokens WHERE token=?', [$token]);
        if (!$row) {
            return [null, '下载链接不存在'];
        }
        if ((int) $row['revoked'] === 1) {
            return [null, '下载链接已被吊销'];
        }
        if ((float) $row['expires'] < microtime(true)) {
            return [null, '下载链接已过期'];
        }
        $max = (int) $row['max_hits'];
        if ($max > 0 && (int) $row['hits'] >= $max) {
            return [null, '下载次数已达上限'];
        }
        if ($consume) {
            Db::run('UPDATE dl_tokens SET hits=hits+1, last_hit=?, last_ip=? WHERE token=?',
                [microtime(true), Net::clientIp(), $token]);
        }
        return [$row, ''];
    }

    /** 全部吊销（清空产物时调用，避免留下指向已删文件的悬空链接）。 */
    public static function revokeAll(): int
    {
        return Db::run('UPDATE dl_tokens SET revoked=1 WHERE revoked=0');
    }

    public static function revokeBuild(string $requestHash): int
    {
        return Db::run('UPDATE dl_tokens SET revoked=1 WHERE request_hash=? AND revoked=0',
            [$requestHash]);
    }

    public static function revokeUser(string $username, bool $onlyUnrevoked = true): int
    {
        $sql = 'UPDATE dl_tokens SET revoked=1 WHERE username=?'
             . ($onlyUnrevoked ? ' AND revoked=0' : '');
        return Db::run($sql, [$username]);
    }

    public static function extend(string $token, int $hours): bool
    {
        $row = Db::one('SELECT * FROM dl_tokens WHERE token=?', [$token]);
        if (!$row) {
            return false;
        }
        // 续期只延长到期时间，**不解除吊销**（吊销是管理员的明确意图）
        Db::run('UPDATE dl_tokens SET expires=? WHERE token=?',
            [microtime(true) + $hours * 3600, $token]);
        return true;
    }

    public static function listFor(?string $username = null, string $requestHash = '',
                                   bool $onlyActive = false, int $limit = 200): array
    {
        $w = [];
        $a = [];
        if ($username !== null) { $w[] = 'username=?'; $a[] = $username; }
        if ($requestHash !== '') { $w[] = 'request_hash=?'; $a[] = $requestHash; }
        if ($onlyActive) { $w[] = 'revoked=0 AND expires>?'; $a[] = microtime(true); }
        $sql = 'SELECT * FROM dl_tokens' . ($w ? ' WHERE ' . implode(' AND ', $w) : '')
             . ' ORDER BY created DESC LIMIT ' . max(1, min(5000, $limit));
        return Db::all($sql, $a);
    }

    public static function stats(): array
    {
        $now = microtime(true);
        return [
            'total'   => (int) Db::val('SELECT COUNT(*) FROM dl_tokens', [], 0),
            'active'  => (int) Db::val('SELECT COUNT(*) FROM dl_tokens WHERE revoked=0 AND expires>?', [$now], 0),
            'expired' => (int) Db::val('SELECT COUNT(*) FROM dl_tokens WHERE revoked=0 AND expires<=?', [$now], 0),
            'revoked' => (int) Db::val('SELECT COUNT(*) FROM dl_tokens WHERE revoked=1', [], 0),
            'hits'    => (int) Db::val('SELECT COALESCE(SUM(hits),0) FROM dl_tokens', [], 0),
        ];
    }

    public static function cleanupExpired(int $days = 7): int
    {
        return Db::run('DELETE FROM dl_tokens WHERE expires < ?', [microtime(true) - $days * 86400]);
    }

    /** 令牌 → 磁盘路径（做归属校验，确保不越出 store/）。 */
    public static function resolvePath(string $requestHash, string $filename): ?string
    {
        if (!preg_match(Util::HASH_RE, $requestHash)) {
            return null;
        }
        $fn = Util::safeFilename($filename);
        if ($fn === '') {
            return null;
        }
        $dir = Config::path('store', $requestHash);
        return Util::under($dir, $dir . DIRECTORY_SEPARATOR . $fn);
    }
}
