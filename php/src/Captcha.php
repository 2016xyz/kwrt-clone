<?php
declare(strict_types=1);

namespace Kwrt;

/**
 * 图形验证码 —— 与 Python 版 (app/captcha.py) 完全互通。
 *
 * ★ 互通的含义：Python 签发的验证码，PHP 也能校验；反之亦然。
 *   为此三件事必须**逐字节一致**，任何一处不同都会表现为「验证码明明对却总说不对」：
 *     1. 同一张表        captchas
 *     2. 同一个盐        app_secrets 表的 captcha_salt（安装级，非进程内随机）
 *     3. 同一个哈希算法  sha256( "{salt}|{cid}|{CODE大写}" ) 的 hex
 *   盐必须持久化在库里而不是进程内随机 —— 否则多进程/多机部署时
 *   用户在 A 进程拿到图、校验落到 B 进程，就会随机失败。
 *
 * 其余设计同 Python：一次性使用、有有效期、按 IP 限流、不依赖图形库（纯 SVG）。
 */
final class Captcha
{
    /** 去混淆字符集（去掉 0 O 1 I L），与 Python 侧一致 */
    private const ALPHABET = '23456789ABCDEFGHJKMNPQRSTUVWXYZ';

    /** 单 IP 每分钟最多签发张数 */
    private const ISSUE_PER_MIN = 30;

    /** 记录保留秒数，过期即清理 */
    private const KEEP_SECONDS = 3600;

    public static function enabled(): bool
    {
        return self::truthy(Settings::get('security.captcha_enabled', false));
    }

    public static function enabledForRegister(): bool
    {
        return self::truthy(Settings::get('security.captcha_on_register', false));
    }

    /**
     * 找回密码的**申请**接口是否要求验证码。
     *
     * 默认**开**：该接口会真实发信，没有验证码就是现成的邮件轰炸放大器
     * （遍历用户名即可持续触发）。schema 的默认值也是 true，这里保持一致。
     */
    public static function enabledForReset(): bool
    {
        $v = Settings::get('security.captcha_on_reset', null);
        return $v === null ? true : self::truthy($v);
    }

    private static function truthy(mixed $v): bool
    {
        if (is_bool($v)) {
            return $v;
        }
        if ($v === null) {
            return false;
        }
        return in_array(strtolower(trim((string) $v)), ['1', 'true', 'yes', 'on'], true);
    }

    public static function length(): int
    {
        $n = (int) (Settings::get('security.captcha_length', 4) ?: 4);
        return max(3, min(6, $n));
    }

    public static function ttlSeconds(): int
    {
        $m = (int) (Settings::get('security.captcha_ttl_min', 5) ?: 5);
        return max(1, min(30, $m)) * 60;
    }

    // ----------------------------------------------------------------------- #

    private static function ensureTable(): void
    {
        // DDL 统一取自 Schema 真源（MySQL 版由生成器派生），避免这里手写一份后漏掉方言差异
        Db::ensureTable('captchas');
    }

    /** 安装级盐：库里存一份，所有进程/两版共用。 */
    private static function salt(): string
    {
        static $cached = null;
        if ($cached !== null) {
            return $cached;
        }
        $env = getenv('KWRT_CAPTCHA_SALT');
        if ($env !== false && $env !== '') {
            return $cached = $env;
        }
        Db::ensureTable('app_secrets');
        $row = Db::one("SELECT `value` FROM app_secrets WHERE name='captcha_salt'");
        if ($row && (string) ($row['value'] ?? '') !== '') {
            return $cached = (string) $row['value'];
        }
        $salt = bin2hex(random_bytes(32));
        Db::upsert('app_secrets',
            ['name' => 'captcha_salt', 'value' => $salt, 'created' => microtime(true)], ['name']);
        return $cached = $salt;
    }

    private static function hash(string $cid, string $code, string $salt): string
    {
        // 与 Python 的 hashlib.sha256(f"{salt}|{cid}|{code.upper()}".encode()).hexdigest() 同式
        return hash('sha256', $salt . '|' . $cid . '|' . strtoupper($code));
    }

    private static function gc(): void
    {
        Db::run('DELETE FROM captchas WHERE created < ?', [microtime(true) - self::KEEP_SECONDS]);
    }

    // ----------------------------------------------------------------------- #

    /**
     * 签发一张验证码。
     *
     * @return array{id:string,svg:string,ttl:int,length:int}
     * @throws \RuntimeException 该 IP 签发过于频繁
     */
    public static function issue(string $ip = ''): array
    {
        self::ensureTable();
        self::gc();

        $n = self::length();
        $chars = self::ALPHABET;
        $code = '';
        for ($i = 0; $i < $n; $i++) {
            $code .= $chars[random_int(0, strlen($chars) - 1)];
        }
        $cid = rtrim(strtr(base64_encode(random_bytes(18)), '+/', '-_'), '=');
        $now = microtime(true);

        $row = Db::one('SELECT COUNT(*) AS n FROM captchas WHERE ip=? AND created>?',
            [$ip, $now - 60]);
        if ($row && (int) $row['n'] >= self::ISSUE_PER_MIN) {
            throw new \RuntimeException('验证码请求过于频繁，请稍后再试');
        }

        Db::run('INSERT INTO captchas(id, answer_hash, created, used, ip) VALUES(?,?,?,0,?)',
            [$cid, self::hash($cid, $code, self::salt()), $now, $ip]);

        return ['id' => $cid, 'svg' => self::svg($code), 'ttl' => self::ttlSeconds(),
                'length' => $n];
    }

    /**
     * 校验并**立即作废**该验证码。
     *
     * @return array{0:bool,1:string}
     */
    public static function verify(string $cid, string $code): array
    {
        $cid = trim($cid);
        $code = trim($code);
        if ($cid === '' || $code === '') {
            return [false, '请填写验证码'];
        }
        if (mb_strlen($code) > 16) {
            return [false, '验证码不正确'];
        }

        self::ensureTable();
        $row = Db::one('SELECT * FROM captchas WHERE id=?', [$cid]);
        if (!$row) {
            return [false, '验证码已失效，请刷新后重试'];
        }
        // ★ 原子认领：作废必须**带条件**并检查受影响行数。
        //   原实现是无条件 `UPDATE ... SET used=1 WHERE id=?`，却拿**更新前**读到的
        //   $row['used'] 判断 —— 两个并发请求会同时读到 used=0、同时通过，
        //   同一张验证码被用两次（验证码可重放）。
        //   条件更新下只有一方能拿到 rowCount()===1，另一方拿到 0。
        //   这与 Builder::pump() 抢槽、Pay::settle() 认领订单是同一种写法。
        $n = Db::run('UPDATE captchas SET used=1 WHERE id=? AND used=0', [$cid]);
        if ($n !== 1) {
            return [false, '验证码已使用，请刷新后重试'];
        }
        if (microtime(true) - (float) $row['created'] > self::ttlSeconds()) {
            return [false, '验证码已过期，请刷新后重试'];
        }
        if (!hash_equals((string) $row['answer_hash'], self::hash($cid, $code, self::salt()))) {
            return [false, '验证码不正确'];
        }
        return [true, ''];
    }

    // ----------------------------------------------------------------------- #

    /** 渲染成 SVG（无第三方图形库依赖）。 */
    private static function svg(string $code): string
    {
        $w = 132;
        $h = 44;
        $p = [];
        $p[] = '<svg xmlns="http://www.w3.org/2000/svg" width="' . $w . '" height="' . $h
            . '" viewBox="0 0 ' . $w . ' ' . $h . '" role="img" aria-label="验证码">';
        $p[] = '<rect width="' . $w . '" height="' . $h . '" fill="#f3f4f6"/>';

        for ($i = 0; $i < 5; $i++) {
            $p[] = '<line x1="' . random_int(0, $w) . '" y1="' . random_int(0, $h)
                . '" x2="' . random_int(0, $w) . '" y2="' . random_int(0, $h)
                . '" stroke="#9ca3af" stroke-width="1" opacity="0.6"/>';
        }
        for ($i = 0; $i < 70; $i++) {
            $p[] = '<circle cx="' . random_int(0, $w) . '" cy="' . random_int(0, $h)
                . '" r="1" fill="#6b7280" opacity="0.5"/>';
        }

        $chars = preg_split('//u', $code, -1, PREG_SPLIT_NO_EMPTY) ?: [];
        $step = $w / (count($chars) + 1);
        $colors = ['#111827', '#1f2937', '#374151', '#2563eb', '#0f766e'];
        foreach ($chars as $i => $ch) {
            $x = $step * ($i + 1);
            $y = $h / 2 + random_int(-5, 5);
            $deg = random_int(-28, 28);
            $color = $colors[random_int(0, count($colors) - 1)];
            $p[] = '<text x="' . round($x, 1) . '" y="' . round($y, 1)
                . '" font-family="monospace,DejaVu Sans Mono" font-size="24" font-weight="700"'
                . ' fill="' . $color . '" text-anchor="middle" dominant-baseline="middle"'
                . ' transform="rotate(' . $deg . ' ' . round($x, 1) . ' ' . round($y, 1) . ')">'
                . htmlspecialchars($ch, ENT_QUOTES) . '</text>';
        }
        $p[] = '</svg>';
        return implode('', $p);
    }
}
