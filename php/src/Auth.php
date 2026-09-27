<?php
/**
 * 鉴权与会话。
 *
 * ★ 口令格式必须与 Python 版**逐字节兼容**，否则两个实现不能共用一个 users.db：
 *       pbkdf2_sha256$240000$<hex盐>$<hex派生密钥>
 *   PHP:    hash_pbkdf2('sha256', $pw, $salt, 240000, 0)   // length=0 → 完整 hex
 *   Python: hashlib.pbkdf2_hmac('sha256', pw, salt, 240000).hex()
 *   两者同为 32 字节 digest 的 64 位小写 hex，可直接比对（见 AccountsCompatTest）。
 *
 * 历史数据兼容：库里可能还有无盐的 64 位 SHA256（早期实现遗留）。
 * 验证时按 SHA256 比对，**登录成功后自动升级**为加盐格式 —— 与 Python 版同策略。
 */
declare(strict_types=1);

namespace Kwrt;

final class Auth
{
    public const PBKDF2_ROUNDS = 240000;

    private const COOKIE = 'kwrt_sid';
    private const SESSION_TTL = 86400 * 14;
    private const CSRF_COOKIE = 'kwrt_csrf';

    // 登录失败锁定（与 Python 版同参数：8 次 / 300 秒，用户名与 IP 双维度）
    private const LOGIN_MAX_FAILS = 8;
    private const LOGIN_LOCK_SECONDS = 300;

    // ---------------------------------------------------------------- 口令

    public static function hashPw(string $pw, ?string $salt = null): string
    {
        $salt ??= bin2hex(random_bytes(16));
        $dk = hash_pbkdf2('sha256', $pw, $salt, self::PBKDF2_ROUNDS, 0, false);
        return 'pbkdf2_sha256$' . self::PBKDF2_ROUNDS . '$' . $salt . '$' . $dk;
    }

    /**
     * @return array{0:bool,1:bool} [是否通过, 是否需要升级重哈希]
     */
    public static function verifyPw(string $pw, string $stored): array
    {
        if ($stored === '') {
            return [false, false];
        }
        if (str_starts_with($stored, 'pbkdf2_sha256$')) {
            $parts = explode('$', $stored, 4);
            if (count($parts) !== 4) {
                return [false, false];
            }
            [, $rounds, $salt, $want] = $parts;
            if (!ctype_digit($rounds) || (int) $rounds < 1) {
                return [false, false];
            }
            $dk = hash_pbkdf2('sha256', $pw, $salt, (int) $rounds, 0, false);
            return [hash_equals($want, $dk), false];
        }
        // 历史无盐 SHA256
        $legacy = hash('sha256', $pw);
        if (hash_equals($stored, $legacy)) {
            return [true, true];
        }
        return [false, false];
    }

    // ---------------------------------------------------------------- 会话

    public static function currentUser(): ?array
    {
        static $cached = false;
        static $user = null;
        if ($cached) {
            return $user;
        }
        $cached = true;
        $tok = $_COOKIE[self::COOKIE] ?? '';
        if (!is_string($tok) || $tok === '' || !preg_match('/^[0-9a-f]{32,64}$/', $tok)) {
            return $user = null;
        }
        $s = Db::one('SELECT username, expires FROM sessions WHERE token=?', [$tok]);
        if (!$s) {
            return $user = null;
        }
        if ((float) $s['expires'] < microtime(true)) {
            Db::run('DELETE FROM sessions WHERE token=?', [$tok]);
            return $user = null;
        }
        $u = Db::one('SELECT * FROM users WHERE username=?', [$s['username']]);
        if (!$u || (int) ($u['disabled'] ?? 0) === 1) {
            return $user = null;
        }
        return $user = $u;
    }

    public static function login(array $user): void
    {
        $tok = bin2hex(random_bytes(32));
        $now = microtime(true);
        Db::run('INSERT INTO sessions(token, username, created, expires) VALUES(?,?,?,?)',
            [$tok, $user['username'], $now, $now + self::SESSION_TTL]);
        self::setCookie(self::COOKIE, $tok, time() + self::SESSION_TTL);
        Db::run('UPDATE users SET last_login=? WHERE username=?', [$now, $user['username']]);
        self::issueCsrf();
    }

    public static function logout(): void
    {
        $tok = $_COOKIE[self::COOKIE] ?? '';
        if (is_string($tok) && $tok !== '') {
            Db::run('DELETE FROM sessions WHERE token=?', [$tok]);
        }
        self::setCookie(self::COOKIE, '', time() - 3600);
        self::setCookie(self::CSRF_COOKIE, '', time() - 3600);
    }

    private static function setCookie(string $name, string $val, int $expires): void
    {
        $secure = Net::scheme() === 'https';
        $opts = [
            'expires'  => $expires,
            'path'     => '/',
            'httponly' => $name !== self::CSRF_COOKIE,   // CSRF 需要被前端 JS 读到
            'samesite' => 'Lax',
            'secure'   => $secure,
        ];
        if (PHP_VERSION_ID >= 70300) {
            setcookie($name, $val, $opts);
        } else {
            setcookie($name, $val, $expires, '/; samesite=Lax', '', $secure, $name !== self::CSRF_COOKIE);
        }
        $_COOKIE[$name] = $val;
    }

    // ---------------------------------------------------------------- 权限

    public static function isAdmin(?array $u = null): bool
    {
        $u ??= self::currentUser();
        return $u !== null && (string) ($u['role'] ?? 'user') === 'admin';
    }

    /** 是否处于有效赞助期（含过期判定）。 */
    public static function isSponsor(?array $u = null): bool
    {
        $u ??= self::currentUser();
        if ($u === null) {
            return false;
        }
        if ((string) ($u['role'] ?? '') === 'admin') {
            return true;
        }
        $until = (float) ($u['sponsor_until'] ?? 0);
        if ($until > 0) {
            return $until > microtime(true);
        }
        return (int) ($u['sponsor'] ?? 0) === 1;
    }

    // ---------------------------------------------------------------- CSRF

    public static function csrfToken(): string
    {
        $t = $_COOKIE[self::CSRF_COOKIE] ?? '';
        if (!is_string($t) || !preg_match('/^[0-9a-f]{32,64}$/', $t)) {
            $t = self::issueCsrf();
        }
        return $t;
    }

    private static function issueCsrf(): string
    {
        $t = bin2hex(random_bytes(32));
        self::setCookie(self::CSRF_COOKIE, $t, 0);   // 会话 cookie
        return $t;
    }

    /**
     * 校验跨站防护。
     *
     * 与 Python 版同思路：① 校验 Origin/Referer 是否同源；② 校验 CSRF token。
     * 支付回调（/api/v1/alipay/notify）**豁免** CSRF —— 它是服务器间调用，
     * 没有浏览器 Origin，安全性由**签名验证**保证，不能靠 CSRF。
     */
    public static function csrfOk(string $path): bool
    {
        if ($path === '/api/v1/alipay/notify') {
            return true;
        }
        $method = strtoupper($_SERVER['REQUEST_METHOD'] ?? 'GET');
        if (in_array($method, ['GET', 'HEAD', 'OPTIONS'], true)) {
            return true;
        }
        // Origin / Referer 同源检查
        $origin = $_SERVER['HTTP_ORIGIN'] ?? '';
        if ($origin === '') {
            $ref = (string) ($_SERVER['HTTP_REFERER'] ?? '');
            if ($ref !== '') {
                $origin = (parse_url($ref, PHP_URL_SCHEME) ?? '') . '://' . (parse_url($ref, PHP_URL_HOST) ?? '');
                $port = parse_url($ref, PHP_URL_PORT);
                if ($port) {
                    $origin .= ':' . $port;
                }
            }
        }
        if ($origin !== '') {
            $base = Net::baseUrl();
            $oHost = strtolower((string) parse_url($origin, PHP_URL_HOST));
            $bHost = strtolower((string) parse_url($base, PHP_URL_HOST));
            $sameScheme = parse_url($origin, PHP_URL_SCHEME) === parse_url($base, PHP_URL_SCHEME);
            if ($oHost === '' || $bHost === '' || $oHost !== $bHost || !$sameScheme) {
                return false;
            }
        }
        // token 校验：表单字段、或 X-CSRF-Token 头
        $sent = $_POST['_csrf'] ?? ($_SERVER['HTTP_X_CSRF_TOKEN'] ?? '');
        if (!is_string($sent) || $sent === '') {
            // 无 token 但同源：放宽（与 Python 版一致，同源已拦住浏览器跨站）
            return true;
        }
        return hash_equals(self::csrfToken(), $sent);
    }

    // ---------------------------------------------------------------- 限速

    /**
     * 限速表。PHP 是「每请求一进程」模型，进程内数组无法跨请求累计，
     * 所以这里**落到 SQLite**（与 Python 版用进程内 dict 不同，但对外行为一致，
     * 而且多 worker 下也正确 —— Python 版那份在多 worker 下是分片计数，见报告 §五）。
     */
    public static function rateOk(string $bucket, string $key, int $limit, int $window): array
    {
        $now = microtime(true);
        $id = $bucket . '|' . $key;
        return Db::tx(function () use ($id, $limit, $window, $now) {
            $row = Db::one('SELECT hits, window_start FROM rate_limits WHERE id=?', [$id]);
            if (!$row || $now - (float) $row['window_start'] > $window) {
                Db::upsert('rate_limits',
                    ['id' => $id, 'hits' => 1, 'window_start' => $now], ['id']);
                return [true, 0];
            }
            $hits = (int) $row['hits'];
            if ($hits >= $limit) {
                $wait = (int) ceil($window - ($now - (float) $row['window_start']));
                return [false, max(1, $wait)];
            }
            Db::run('UPDATE rate_limits SET hits=hits+1 WHERE id=?', [$id]);
            return [true, 0];
        });
    }

    public static function pruneRateLimits(): void
    {
        Db::run('DELETE FROM rate_limits WHERE window_start < ?', [microtime(true) - 7200]);
    }

    // ---------------------------------------------------------------- 封禁

    /** 当前请求者是否被封禁（按 IP 或用户名）。CDN 场景下用 Net::clientIp()。 */
    public static function banned(): ?array
    {
        $ip = Net::clientIp();
        $u = self::currentUser();
        $rows = Db::all('SELECT kind, `value`, reason FROM bans');
        foreach ($rows as $b) {
            $kind = (string) $b['kind'];
            $val = (string) $b['value'];
            if ($kind === 'ip' && $val !== '' && $val === $ip) {
                return $b;
            }
            if ($kind === 'user' && $u && $val !== '' && $val === $u['username']) {
                return $b;
            }
        }
        return null;
    }

    // ---------------------------------------------------------------- 登录锁定

    public static function loginLocked(string $username): int
    {
        $now = microtime(true);
        $ip = Net::clientIp();
        foreach ([['u', $username], ['i', $ip]] as [$k, $v]) {
            $r = Db::one('SELECT fails, until FROM login_fails WHERE id=?', [$k . '|' . $v]);
            if ($r && (float) $r['until'] > $now) {
                return (int) ceil((float) $r['until'] - $now);
            }
        }
        return 0;
    }

    public static function loginFail(string $username): void
    {
        $now = microtime(true);
        $ip = Net::clientIp();
        foreach ([['u', $username], ['i', $ip]] as [$k, $v]) {
            $id = $k . '|' . $v;
            Db::tx(function () use ($id, $now) {
                $r = Db::one('SELECT fails, until FROM login_fails WHERE id=?', [$id]);
                $fails = ($r && (float) $r['until'] > $now - self::LOGIN_LOCK_SECONDS)
                    ? (int) $r['fails'] + 1 : 1;
                $until = $fails >= self::LOGIN_MAX_FAILS ? $now + self::LOGIN_LOCK_SECONDS : 0.0;
                Db::upsert('login_fails',
                    ['id' => $id, 'fails' => $fails, 'until' => $until, 'updated' => $now], ['id']);
            });
        }
    }

    public static function loginReset(string $username): void
    {
        $ip = Net::clientIp();
        Db::run('DELETE FROM login_fails WHERE id=?', ['u|' . $username]);
        Db::run('DELETE FROM login_fails WHERE id=?', ['i|' . $ip]);
    }

    // ---------------------------------------------------------------- 管理员审计

    public static function audit(string $action, string $target = '', mixed $detail = ''): void
    {
        $u = self::currentUser();
        $d = is_scalar($detail) ? (string) $detail : json_encode($detail, JSON_UNESCAPED_UNICODE);
        // 审计日志绝不记录密钥类配置的值
        Db::run('INSERT INTO admin_logs(admin, action, target, detail, ip, created) VALUES(?,?,?,?,?,?)',
            [$u['username'] ?? '-', $action, $target, mb_substr($d ?? '', 0, 2000), Net::clientIp(), microtime(true)]);
    }
}
