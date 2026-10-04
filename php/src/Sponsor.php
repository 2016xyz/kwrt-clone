<?php

declare(strict_types=1);

namespace Kwrt;

/**
 * 赞助金额 / 天数的**唯一真源**。
 *
 * 与 Python 版 app/main.py 里的 _amount_range / _per_day_price / _custom_days /
 * _resolve_sponsor_choice / _render_qr_text / _qr_config_problem 一一对应，
 * 两版必须保持同一套规则 —— 否则同一笔赞助在两边算出不同天数。
 *
 * 安全底线：天数**永远由服务端算**。套餐走套餐的 days，自定义金额走 customDays()。
 * 前端传来的 amount 是"用户申报付了多少钱"（自定义金额要的就是这个），但 days
 * 前端无权指定 —— 否则就是"付 1 元给 3650 天"。
 */
final class Sponsor
{
    /** 自定义金额折算天数的上限（10 年） */
    private const MAX_DAYS = 3650;

    /** @return array{0:int,1:int} [最低, 最高]，恒有 最低 <= 最高 */
    public static function amountRange(): array
    {
        $lo = (int) (Settings::get('sponsor.min_amount', 1) ?: 1);
        $hi = (int) (Settings::get('sponsor.max_amount', 99999) ?: 99999);
        $lo = max(1, $lo);
        $hi = max($lo, $hi);
        return [$lo, $hi];
    }

    /**
     * 每天单价。站长配了就用配的；填 0 则取套餐里最划算（amount/days 最小）
     * 的那个折算；两者都没有时按 1 兜底 —— 可预测，不会凭空送天数。
     */
    public static function perDayPrice(): float
    {
        $rate = (float) (Settings::get('sponsor.per_day_price', 0) ?: 0);
        if ($rate > 0) {
            return $rate;
        }
        $raw = Settings::get('sponsor.tiers', []);
        if (is_string($raw)) {
            $raw = json_decode($raw, true) ?: [];
        }
        $rates = [];
        foreach ((array) $raw as $t) {
            if (!is_array($t)) {
                continue;
            }
            $a = (float) ($t['amount'] ?? 0);
            $d = (int) ($t['days'] ?? 0);
            if ($a > 0 && $d > 0) {
                $rates[] = $a / $d;
            }
        }
        return $rates ? min($rates) : 1.0;
    }

    public static function customDays(float $amount): int
    {
        $rate = self::perDayPrice();
        $days = $rate > 0 ? (int) ($amount / $rate) : 0;
        return max(1, min($days, self::MAX_DAYS));
    }

    /**
     * 把前端传上来的金额解析成 float，返回 [值, 错误信息]。
     *
     * 与 Python 的 app/main.py::_parse_amount 同规则：空串视为 0，非数字给人话，
     * NaN/Inf 也算非法 —— `(float) "abc"` 在 PHP 里会静默变成 0.0，然后报出
     * "金额不能低于 1" 这种指错方向的信息。
     *
     * @return array{0:float,1:string}
     */
    public static function parseAmount(mixed $raw): array
    {
        $s = trim((string) ($raw ?? ''));
        if ($s === '') {
            return [0.0, ''];
        }
        if (!is_numeric($s)) {
            return [0.0, '金额必须是数字'];
        }
        $v = (float) $s;
        if (!is_finite($v)) {
            return [0.0, '金额必须是有限数字'];
        }
        return [$v, ''];
    }

    /**
     * 解析用户的选择。
     *
     * @param float $amount 用户申报的金额（仅在未选套餐时生效）
     * @return array{0:?array{amount:float,days:int,tier:string,custom:bool},1:string}
     *         [选择, 成功为空 / 失败为错误信息]
     */
    public static function resolve(string $tier, float $amount): array
    {
        if ($tier !== '') {
            $found = null;
            $raw = Settings::get('sponsor.tiers', []);
            if (is_string($raw)) {
                $raw = json_decode($raw, true) ?: [];
            }
            foreach ((array) $raw as $t) {
                if (is_array($t) && (string) ($t['name'] ?? '') === $tier) {
                    $found = $t;
                    break;
                }
            }
            if ($found === null) {
                return [null, '套餐不存在，请从页面列出的套餐中选择'];
            }
            $amt = (float) ($found['amount'] ?? 0);
            $days = (int) ($found['days'] ?? 0);
            if ($amt <= 0 || $days <= 0) {
                return [null, '该套餐的金额或天数无效，请联系站长'];
            }
            return [['amount' => $amt, 'days' => $days, 'tier' => $tier, 'custom' => false], ''];
        }

        if (!Settings::bool('sponsor.custom_amount', true)) {
            return [null, '本站未开放自定义金额，请从页面列出的套餐中选择'];
        }
        if (!is_finite($amount)) {
            return [null, '金额必须是有限数字'];
        }
        [$lo, $hi] = self::amountRange();
        if ($amount < $lo) {
            return [null, '金额不能低于 ' . $lo];
        }
        if ($amount > $hi) {
            return [null, '金额不能高于 ' . $hi];
        }
        $amt = round($amount, 2);
        return [['amount' => $amt, 'days' => self::customDays($amt),
                 'tier' => '自定义赞助 ' . self::num($amt), 'custom' => true], ''];
    }

    /** 把 {amount} / {amount2} 替换成服务端解析过的金额 */
    public static function renderQrText(string $tpl, float $amount): string
    {
        return str_replace(['{amount2}', '{amount}'],
            [number_format($amount, 2, '.', ''), self::num($amount)], $tpl);
    }

    /** 金额的紧凑写法：50 / 4.99（去掉多余的 0），与 Python 的 {amount:g} 对齐 */
    public static function num(float $v): string
    {
        $s = rtrim(rtrim(number_format($v, 2, '.', ''), '0'), '.');
        return $s === '' ? '0' : $s;
    }

    /** 收款码链路当前能不能出图；不能则返回一句人话 */
    public static function qrConfigProblem(): string
    {
        $kind = (string) (Settings::get('sponsor.qr_kind', 'image') ?: 'image');
        if ($kind === 'image') {
            if (trim((string) Settings::get('sponsor.pay_qr', '')) === '') {
                return '站长尚未配置收款码：请设置「收款码图片地址」，或把「收款码来源」改为 text 并填写内容';
            }
            return '';
        }
        if (trim((string) Settings::get('sponsor.qr_text', '')) === '') {
            return '站长尚未配置收款码：请填写「收款码内容 / 链接」，或把「收款码来源」改为 image 并填图片地址';
        }
        return '';
    }
}
