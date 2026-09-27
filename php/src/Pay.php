<?php
/**
 * 支付宝当面付（alipay.trade.precreate / query / refund）。
 *
 * 签名要点（Python 版踩过的坑，这里照抄正确做法）：
 *   · **请求签名**的参数串包含 sign_type；**通知验签**时 sign 与 sign_type 都要剔除。
 *     这两处不一致是最容易出 isv.invalid-signature 的地方。
 *   · 只有「验签通过且 trade_status 属于已支付集合」才置位；查询失败绝不谎报成功。
 *   · 结算走原子 claim（先改状态再发放），避免重复通知导致重复发放。
 */
declare(strict_types=1);

namespace Kwrt;

final class Pay
{
    /**
     * 「已收款」状态集合 —— 与 Python 版对齐（main.py 里到处写 `IN ('paid','paid_pending')`）。
     *   paid_pending：网关已收款但权益未发放（后台关了自动发放，等管理员确认）
     *   paid        ：权益已发放
     * 凡是统计收入 / 判断「这笔单已付款」的地方都必须带上 paid_pending，
     * 否则关了自动发放时后台收入统计会凭空少一截。
     */
    public const PAID_STATES = ['paid', 'paid_pending'];

    /** 供拼 SQL 用：'paid','paid_pending' */
    public static function paidStatesSql(): string
    {
        return "'" . implode("','", self::PAID_STATES) . "'";
    }

    private const PAID = ['TRADE_SUCCESS', 'TRADE_FINISHED'];
    private const GATEWAY = 'https://openapi.alipay.com/gateway.do';

    public static function enabled(): bool
    {
        return Settings::bool('pay.alipay_enabled', false)
            && self::appId() !== '' && self::privateKey() !== '';
    }

    /**
     * 密钥三项是否齐备（对应 Python 的 pay.is_configured()）。
     * Python 的判据是 app_id && private_key && alipay_public_key。
     */
    public static function configured(): bool
    {
        return self::appId() !== '' && self::privateKey() !== '' && self::publicKey() !== '';
    }

    /**
     * 对外宣称「支付可用」（对应 Python 的 pay.available()）。
     * = 开关已开（pay.alipay_enabled）且密钥齐备。
     */
    public static function available(): bool
    {
        return Settings::bool('pay.alipay_enabled', false) && self::configured();
    }

    private static function appId(): string
    {
        return trim((string) Settings::get('pay.alipay_app_id', ''));
    }

    private static function privateKey(): string
    {
        return (string) Settings::get('pay.alipay_private_key', '');
    }

    private static function publicKey(): string
    {
        return (string) Settings::get('pay.alipay_public_key', '');
    }

    private static function gateway(): string
    {
        // 允许切沙箱（Python 版同款开关）
        // ★ 键名是 pay.alipay_sandbox，不是 pay.sandbox（后者 schema 里不存在）。
        //   写错的后果很严重：Settings::bool 取默认 false →
        //   **即便管理员打开了沙箱模式，PHP 仍会连生产网关**，
        //   测试期就会产生真实订单。Python 侧一直用对。
        return Settings::bool('pay.alipay_sandbox', false)
            ? 'https://openapi-sandbox.dl.alipaydev.com/gateway.do'
            // 自定义网关地址（自建代理/专线）也要生效：Python 侧读这个键，
            // PHP 原先直接落到常量 → 后台改了没用，两版行为不一致。
            : (trim((string) Settings::get('pay.alipay_gateway', '')) ?: self::GATEWAY);
    }

    /** 补齐 PEM 头尾（库里存的是裸 base64 时）。 */
    private static function wrapPem(string $key, string $type): string
    {
        if (str_contains($key, '-----BEGIN')) {
            return $key;
        }
        $body = chunk_split(preg_replace('/\\s+/', '', $key) ?? '', 64, "\\n");
        return "-----BEGIN {$type}-----\\n{$body}-----END {$type}-----\\n";
    }

    // ---------------------------------------------------------------- 签名

    private static function signString(array $params, bool $dropSignType): string
    {
        unset($params['sign']);
        if ($dropSignType) {
            unset($params['sign_type']);
        }
        $params = array_filter($params, static fn($v) => $v !== '' && $v !== null);
        ksort($params);
        $parts = [];
        foreach ($params as $k => $v) {
            $parts[] = $k . '=' . $v;
        }
        return implode('&', $parts);
    }

    private static function sign(array $params): ?string
    {
        $key = openssl_pkey_get_private(self::wrapPem(self::privateKey(), 'RSA PRIVATE KEY'));
        if ($key === false) {
            return null;
        }
        $ok = openssl_sign(self::signString($params, false), $sig, $key, OPENSSL_ALGO_SHA256);
        return $ok ? base64_encode($sig) : null;
    }

    /** 验签：剔除 sign 与 sign_type。 */
    public static function verify(array $params): bool
    {
        $sig = (string) ($params['sign'] ?? '');
        if ($sig === '') {
            return false;
        }
        $pub = openssl_pkey_get_public(self::wrapPem(self::publicKey(), 'PUBLIC KEY'));
        if ($pub === false) {
            return false;
        }
        $plain = self::signString($params, true);
        return openssl_verify($plain, base64_decode($sig), $pub, OPENSSL_ALGO_SHA256) === 1;
    }

    // ---------------------------------------------------------------- 网关调用

    private static function call(string $method, array $biz): array
    {
        if (!self::enabled()) {
            return ['ok' => false, 'code' => '', 'detail' => '支付未配置'];
        }
        $params = [
            'app_id' => self::appId(),
            'method' => $method,
            'format' => 'JSON',
            'charset' => 'utf-8',
            'sign_type' => 'RSA2',
            'timestamp' => date('Y-m-d H:i:s'),
            'version' => '1.0',
            'notify_url' => self::notifyUrl(),
            'biz_content' => json_encode($biz, JSON_UNESCAPED_UNICODE),
        ];
        $sig = self::sign($params);
        if ($sig === null) {
            return ['ok' => false, 'code' => '', 'detail' => '私钥无效，无法签名'];
        }
        $params['sign'] = $sig;

        $ch = curl_init(self::gateway());
        curl_setopt_array($ch, [
            CURLOPT_POST => true,
            CURLOPT_POSTFIELDS => http_build_query($params),
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_TIMEOUT => 20,
            CURLOPT_CONNECTTIMEOUT => 8,
            CURLOPT_SSL_VERIFYPEER => true,
            CURLOPT_SSL_VERIFYHOST => 2,
        ]);
        $resp = curl_exec($ch);
        $err = curl_error($ch);
        $code = (int) curl_getinfo($ch, CURLINFO_HTTP_CODE);
        curl_close($ch);
        if ($resp === false || $code !== 200) {
            return ['ok' => false, 'code' => '', 'detail' => '网关请求失败: ' . ($err ?: "HTTP {$code}")];
        }
        $node = str_replace('.', '_', $method) . '_response';
        $j = json_decode((string) $resp, true);
        $body = $j[$node] ?? null;
        if (!is_array($body)) {
            return ['ok' => false, 'code' => '', 'detail' => '网关响应无法解析'];
        }
        return [
            'ok' => ($body['code'] ?? '') === '10000',
            'code' => (string) ($body['code'] ?? ''),
            'sub_code' => (string) ($body['sub_code'] ?? ''),
            'detail' => (string) ($body['sub_msg'] ?? $body['msg'] ?? ''),
            'body' => $body,
        ];
    }

    /** 异步通知地址：优先用设置里的显式配置，否则按站点地址推导。 */
    public static function notifyUrl(): string
    {
        $u = trim((string) Settings::get('pay.notify_url', ''));
        return $u !== '' ? $u : Net::url('/api/v1/alipay/notify');
    }

    // ---------------------------------------------------------------- 下单

    /** @return array{ok:bool,out_trade_no:string,qr_code:string,expires:float,detail:string} */
    public static function precreate(string $username, string $tierName, float $amount, int $days): array
    {
        // ★ 键名是 pay.alipay_subject_prefix（Python 侧同款），不是 pay.order_prefix。
        //   写错 → 永远用默认值，管理员设置的主题前缀不生效，两版订单标题也不一致。
        $prefix = (string) Settings::get('pay.alipay_subject_prefix', 'KWRT');
        $no = $prefix . date('YmdHis') . bin2hex(random_bytes(4));
        $ttlMin = max(1, (int) Settings::get('pay.order_ttl_minutes', 15));
        $expires = microtime(true) + $ttlMin * 60;

        $biz = [
            'out_trade_no' => $no,
            'total_amount' => number_format($amount, 2, '.', ''),
            'subject' => mb_substr((string) Settings::get('pay.alipay_subject_prefix', '赞助')
                . ' ' . $tierName, 0, 250),
        ];
        $r = self::call('alipay.trade.precreate', $biz);

        $qr = (string) ($r['body']['qr_code'] ?? '');
        Db::run('INSERT INTO pay_orders(out_trade_no, username, tier_name, amount, days, status, '
            . 'qr_code, created, expires, raw) VALUES(?,?,?,?,?,?,?,?,?,?)',
            [$no, $username, $tierName, $amount, $days, $r['ok'] ? 'created' : 'failed',
             $qr, microtime(true), $expires, json_encode($r, JSON_UNESCAPED_UNICODE)]);

        if (!$r['ok']) {
            return ['ok' => false, 'out_trade_no' => $no, 'qr_code' => '', 'expires' => $expires,
                    'detail' => $r['detail'] ?: '下单失败'];
        }
        return ['ok' => true, 'out_trade_no' => $no, 'qr_code' => $qr,
                'expires' => $expires, 'detail' => ''];
    }

    /** 主动查单并同步状态（查询失败不改状态、不谎报成功）。 */
    public static function syncOrder(string $outTradeNo): bool
    {
        $o = Db::one('SELECT * FROM pay_orders WHERE out_trade_no=?', [$outTradeNo]);
        if (!$o) {
            return false;
        }
        $r = self::call('alipay.trade.query', ['out_trade_no' => $outTradeNo]);
        Db::run('UPDATE pay_orders SET last_query=?, raw=? WHERE out_trade_no=?',
            [microtime(true), json_encode($r, JSON_UNESCAPED_UNICODE), $outTradeNo]);
        if (!$r['ok']) {
            return false;   // ACQ.TRADE_NOT_EXIST 等：如实返回失败
        }
        $b = $r['body'];
        $status = (string) ($b['trade_status'] ?? '');
        if (in_array($status, self::PAID, true)) {
            self::settle($outTradeNo, (string) ($b['trade_no'] ?? ''),
                (string) ($b['buyer_user_id'] ?? ''), $b);
            return true;
        }
        // 明确失败态才改，避免把已支付订单改坏
        if (in_array($status, ['TRADE_CLOSED'], true)) {
            Db::run("UPDATE pay_orders SET status='closed' WHERE out_trade_no=? "
            . "AND status NOT IN (" . self::paidStatesSql() . ")",
                [$outTradeNo]);
        }
        return false;
    }

    /**
     * 结算：原子 claim（只有把 status 从非 paid 改成 paid 的那一次生效），
     * 再发放赞助权益。重复通知不会重复发放。
     */
    public static function settle(string $outTradeNo, string $tradeNo, string $buyerId, array $raw): void
    {
        // ★ 与 Python 版对齐的两态模型：
        //     paid_pending = 网关已收款、**权益尚未发放**（后台关了自动发放）
        //     paid         = 权益已发放
        //   原实现只有 paid 一个状态，只好「先 grant() 再改状态」来兜住
        //   「已付款但没发放」的窗口 —— 结果后台「标记已付款」点两次就发放两次。
        //   Python 侧早已修好并留了注释（main.py:1959），这里补齐。
        $auto = Settings::bool('pay.auto_activate', true);
        $target = $auto ? 'paid' : 'paid_pending';

        $done = Db::tx(function () use ($outTradeNo, $tradeNo, $buyerId, $raw, $target) {
            $n = Db::run("UPDATE pay_orders SET status=?, paid_at=?, trade_no=?, buyer_id=?, raw=? "
                . "WHERE out_trade_no=? AND status NOT IN ('paid','paid_pending','refunded')",
                [$target, microtime(true), $tradeNo, $buyerId,
                 json_encode($raw, JSON_UNESCAPED_UNICODE), $outTradeNo]);
            if ($n !== 1) {
                return null;    // 别人已经结算过（或已退款），不重复发放
            }
            return Db::one('SELECT * FROM pay_orders WHERE out_trade_no=?', [$outTradeNo]);
        });
        if ($done === null) {
            return;
        }
        if (!$auto) {
            Auth::audit('pay_paid', $outTradeNo, ['note' => '已收款，等待管理员发放权益']);
            return;
        }
        self::grant((string) $done['username'], (int) $done['days'], (string) $done['tier_name'],
            (float) $done['amount']);
        Auth::audit('pay_settled', $outTradeNo, ['username' => $done['username']]);
    }

    /** 发放赞助权益（可叠加已有未过期时长）。 */
    public static function grant(string $username, int $days, string $tierName, float $amount): void
    {
        $u = Db::one('SELECT sponsor_until FROM users WHERE username=?', [$username]);
        if (!$u) {
            return;
        }
        $now = microtime(true);
        $cur = (float) ($u['sponsor_until'] ?? 0);
        $base = $cur > $now ? $cur : $now;
        Db::run('UPDATE users SET sponsor=1, sponsor_until=?, sponsor_tier=?, sponsor_amount=? '
            . 'WHERE username=?',
            [$base + $days * 86400, $tierName, $amount, $username]);
    }

    // ---------------------------------------------------------------- 通知

    /** @return array{code:int,body:string} */
    public static function handleNotify(array $data): array
    {
        if (!self::verify($data)) {
            return ['code' => 400, 'body' => 'signature invalid'];
        }
        $out = (string) ($data['out_trade_no'] ?? '');
        $status = (string) ($data['trade_status'] ?? '');
        if ($out === '' || !in_array($status, self::PAID, true)) {
            return ['code' => 200, 'body' => 'success'];   // 非成功态也回 success，避免支付宝重推
        }
        self::settle($out, (string) ($data['trade_no'] ?? ''), (string) ($data['buyer_id'] ?? ''), $data);
        return ['code' => 200, 'body' => 'success'];
    }

    // ---------------------------------------------------------------- 退款

    /** 真实调用退款接口；网关失败**绝不**标记为已完成。 */
    /**
     * 发起退款。
     *
     * ★ $outRequestNo 是**幂等键**，必须传。支付宝对同一个 out_request_no
     *   返回同一笔退款结果，因此「首次已退款但响应超时 → 重试」不会退第二次。
     *   Python 侧一直是传的（pay.py:342 的 out_request_no），PHP 侧原先漏了 ——
     *   而 PHP 的审批流程在网关失败时会把申请退回 pending 允许重试，
     *   两者相叠加就可能在**响应超时**这一真实场景下重复退款（真金白银）。
     */
    public static function refund(string $outTradeNo, float $amount, string $reason,
                                  ?string $outRequestNo = null): array
    {
        $biz = [
            'out_trade_no' => $outTradeNo,
            'refund_amount' => number_format($amount, 2, '.', ''),
            'refund_reason' => mb_substr($reason, 0, 250),
        ];
        if ($outRequestNo !== null && $outRequestNo !== '') {
            $biz['out_request_no'] = mb_substr($outRequestNo, 0, 64);
        }
        $r = self::call('alipay.trade.refund', $biz);
        return [
            'ok' => $r['ok'],
            'sub_code' => $r['sub_code'] ?? '',
            'detail' => $r['detail'] ?? '',
            'raw' => json_encode($r, JSON_UNESCAPED_UNICODE),
        ];
    }

    /** 离线退款：不调网关，直接撤销赞助权益（用于已被网关拒绝的场景）。 */
    public static function revokeSponsor(string $username, int $days): void
    {
        $u = Db::one('SELECT sponsor_until FROM users WHERE username=?', [$username]);
        if (!$u) {
            return;
        }
        $now = microtime(true);
        $cur = (float) ($u['sponsor_until'] ?? 0);
        $left = max(0.0, $cur - $now) - $days * 86400;
        if ($left <= 0) {
            Db::run('UPDATE users SET sponsor=0, sponsor_until=0, sponsor_tier=? WHERE username=?',
                ['', $username]);
        } else {
            Db::run('UPDATE users SET sponsor_until=?, sponsor_tier=? WHERE username=?',
                [$now + $left, '', $username]);
        }
    }
}
