<?php
/**
 * 站点设置中心。
 *
 * 行为与 Python 版 app/sitesettings.py 对齐：
 *   · schema 驱动（见 SettingsSchema.php，由 Python 侧生成）
 *   · 存储为 settings 表的 key/value 文本，按类型编解码
 *   · 写入前一律走 validate()，非法值拒绝并给出可读原因
 *   · 密文类型（password）在审计日志里绝不出现
 *
 * PHP 侧的额外收益：读取结果在**单请求内**缓存。
 * Python 版 SS.get() 无缓存（每次读库，改设置立即生效）；PHP 是「每请求一进程」
 * 模型，单请求内缓存既保持「改完立即生效」的语义（下一个请求即新值），
 * 又把一次页面渲染里的几十次 get() 收敛成一次查询。
 */
declare(strict_types=1);

namespace Kwrt;

final class Settings
{
    private static ?array $cache = null;
    /** 密钥类键名：审计日志与前端回显都必须屏蔽。 */
    private const SECRET_HINTS = ['key', 'secret', 'token', 'password', 'passwd', 'pwd', 'private'];

    private static function byKey(): array
    {
        static $m = null;
        return $m ??= SettingsSchema::byKey();
    }

    /** 全量读取（含默认值）。 */
    public static function load(): array
    {
        if (self::$cache !== null) {
            return self::$cache;
        }
        $out = [];
        foreach (SettingsSchema::SCHEMA as $s) {
            $out[$s['k']] = $s['d'];
        }
        try {
            foreach (Db::all('SELECT `key`, `value` FROM settings') as $r) {
                if (!array_key_exists($r['key'], $out)) {
                    continue;   // 库里可能有已废弃的旧键，忽略
                }
                $out[$r['key']] = self::decode($r['value'], self::byKey()[$r['key']]['t'] ?? 'text',
                                               $out[$r['key']]);
            }
        } catch (\Throwable $e) {
            // ★ 这里只能吞「表还没建好」这一种情况。
            //   原实现是 catch (\Throwable) 无条件忽略 —— 结果一个 MySQL 方言错误
            //   （`key` 是保留字、没加反引号导致的 42000 语法错）被**静默吞掉**，
            //   表现为「后台改了设置不生效、读出来永远是默认值」，
            //   而库里其实已经写对了。宽泛的 catch 就是这样把真错误藏起来的。
            $m = $e->getMessage();
            $notReady = stripos($m, "doesn't exist") !== false
                     || stripos($m, 'no such table') !== false
                     || stripos($m, 'Base table or view not found') !== false;
            if (!$notReady) {
                throw $e;
            }
        }
        return self::$cache = $out;
    }

    public static function get(string $key, mixed $default = null): mixed
    {
        if (func_num_args() >= 2) {
            $all = self::load();
            if (array_key_exists($key, $all) && $all[$key] !== null && $all[$key] !== '') {
                return $all[$key];
            }
            return $default;
        }
        return self::load()[$key] ?? null;
    }

    public static function bool(string $key, bool $default = false): bool
    {
        $v = self::load()[$key] ?? null;
        return $v === null ? $default : (bool) $v;
    }

    public static function int(string $key, int $default = 0): int
    {
        $v = self::load()[$key] ?? null;
        return is_numeric($v) ? (int) $v : $default;
    }

    public static function has(string $key): bool
    {
        return array_key_exists($key, self::byKey());
    }

    public static function isSecret(string $key): bool
    {
        foreach (self::SECRET_HINTS as $h) {
            if (stripos($key, $h) !== false) {
                return true;
            }
        }
        return false;
    }

    /** 首次启动写入默认值（幂等：已存在的键不动）。 */
    public static function bootstrap(?\PDO $pdo = null): void
    {
        $pdo ??= Db::pdo();
        $now = microtime(true);
        // 方言差异走助手：SQLite INSERT OR IGNORE / MySQL INSERT IGNORE。
        // 不用预处理语句，因为表名要带前缀而占位符不能用于表名。
        foreach (SettingsSchema::SCHEMA as $s) {
            Db::insertIgnore('settings', [
                'key' => $s['k'], 'value' => self::encode($s['d'], $s['t']), 'updated' => $now,
            ]);
        }
        // 软件包目录不在 schema 里（由后台「插件目录」单独维护），
        // 但首次启动要有初值，否则新装的站没有预设插件。用随仓库分发的默认清单补齐。
        $has = $pdo->prepare('SELECT 1 FROM `settings` WHERE `key`=?');
        $has->execute(['build.pkg_catalog']);
        if ($has->fetchColumn() === false) {
            $f = Config::path('data', 'pkg_catalog.default.json');
            if (is_file($f)) {
                $j = json_decode((string) file_get_contents($f), true);
                if (is_array($j)) {
                    Db::insertIgnore('settings', [
                        'key' => 'build.pkg_catalog',
                        'value' => json_encode($j, JSON_UNESCAPED_UNICODE),
                        'updated' => $now,
                    ]);
                }
            }
        }
        self::$cache = null;
    }

    // ------------------------------------------------------------- 编解码

    /** 值 → 存储文本。 */
    public static function encode(mixed $v, string $t): string
    {
        return match ($t) {
            'bool' => $v ? '1' : '0',
            'number' => self::numToText($v),
            'json' => json_encode($v, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES) ?: '[]',
            default => (string) ($v ?? ''),
        };
    }

    /** 存储文本 → 值。无法解析时回落到默认值（绝不抛异常）。 */
    public static function decode(mixed $raw, string $t, mixed $default = null): mixed
    {
        if ($raw === null) {
            return $default;
        }
        return match ($t) {
            'bool' => in_array((string) $raw, ['1', 'true', 'True', 'on', 'yes'], true),
            'number' => self::numFromText($raw, $default),
            'json' => (static function ($s, $d) {
                if (!is_string($s) || $s === '') {
                    return $d;
                }
                $j = json_decode($s, true);
                return $j === null && trim($s) !== 'null' ? $d : $j;
            })($raw, $default),
            default => (string) $raw,
        };
    }

    /**
     * 数字 → 文本。非有限值（inf/nan）退化为 "0"。
     *
     * Python 版这里踩过真实的坑：`int(float("inf"))` 抛 OverflowError，
     * 而 except 里只写了 (TypeError, ValueError)，整条读取路径 500。
     * PHP 侧虽然 `(int) INF` 不会抛（PHP 会给出 0），但显式判有限性更清楚。
     */
    private static function numToText(mixed $v): string
    {
        if ($v === null || $v === '') {
            return '0';
        }
        $f = is_numeric($v) ? (float) $v : 0.0;
        if (!is_finite($f)) {
            return '0';
        }
        return (string) ((int) $f === $f ? (int) $f : $f);
    }

    /** 文本 → 数字。非法/非有限 → 默认值。 */
    private static function numFromText(mixed $raw, mixed $default): mixed
    {
        if (!is_numeric($raw)) {
            return $default;
        }
        $f = (float) $raw;
        if (!is_finite($f)) {       // "1e400" → INF
            return $default;
        }
        return (int) $f === $f ? (int) $f : $f;
    }

    // ------------------------------------------------------------- 校验

    /* ------------------------------------------------------------------ *
     * 广告位（key=ads）
     * 取值约束与 app/sitesettings.py 的 _ADS_* 常量保持一致 —— 两版共用
     * 同一份设置，校验口径不一致会出现「Python 能存、PHP 存不进」的怪象。
     * ------------------------------------------------------------------ */
    private const ADS_MODES = ['popup', 'marquee'];
    private const ADS_FREQ  = ['session', 'always', 'once'];
    private const ADS_POS   = ['top', 'bottom'];
    private const ADS_TEXT_MAX = ['title' => 200, 'content' => 4000, 'image' => 500,
                                  'link' => 500, 'link_text' => 60, 'bg' => 32,
                                  'color' => 32, 'start' => 32, 'end' => 32];

    private static function asBool(mixed $x, bool $dflt = true): bool
    {
        if ($x === null) {
            return $dflt;
        }
        if (is_bool($x)) {
            return $x;
        }
        return !in_array(strtolower(trim((string) $x)), ['false', '0', 'no', 'off', ''], true);
    }

    private static function clampInt(mixed $x, int $dflt, int $lo, int $hi): int
    {
        $n = is_numeric($x) ? (int) (float) $x : $dflt;
        return max($lo, min($hi, $n));
    }

    /** 校验广告列表。返回 '' 表示通过，否则为错误文案。 */
    private static function validateAds(mixed $v): string
    {
        if (!is_array($v) || !array_is_list($v)) {
            return '广告列表必须是 JSON 数组';
        }
        if (count($v) > 20) {
            return '广告最多 20 条';
        }
        $i = 0;
        foreach ($v as $it) {
            $i++;
            $tag = "第 {$i} 条广告";
            if (!is_array($it)) {
                return "{$tag}不是对象";
            }
            if (!in_array(strtolower(trim((string) ($it['mode'] ?? 'popup'))), self::ADS_MODES, true)) {
                return "{$tag}的 mode 只能是 popup(弹出) 或 marquee(滚动)";
            }
            if (!in_array(strtolower(trim((string) ($it['frequency'] ?? 'session'))), self::ADS_FREQ, true)) {
                return "{$tag}的 frequency 只能是 session/always/once";
            }
            if (!in_array(strtolower(trim((string) ($it['position'] ?? 'top'))), self::ADS_POS, true)) {
                return "{$tag}的 position 只能是 top/bottom";
            }
            foreach (self::ADS_TEXT_MAX as $fld => $mx) {
                if (mb_strlen((string) ($it[$fld] ?? '')) > $mx) {
                    return "{$tag}的 {$fld} 长度不能超过 {$mx}";
                }
            }
            $dly = $it['delay'] ?? 0;
            if ($dly !== null && $dly !== '' && !is_numeric($dly)) {
                return "{$tag}的 delay 需在 0–120 秒之间";
            }
            if (is_numeric($dly) && (float) $dly != self::clampInt($dly, 0, 0, 120)) {
                return "{$tag}的 delay 需在 0–120 秒之间";
            }
            $spd = $it['speed'] ?? 60;
            if ($spd !== null && $spd !== '' && !is_numeric($spd)) {
                return "{$tag}的 speed 需在 10–400 之间";
            }
            if (is_numeric($spd) && (float) $spd != self::clampInt($spd, 60, 10, 400)) {
                return "{$tag}的 speed 需在 10–400 之间";
            }
            foreach (['start', 'end'] as $fld) {
                $s = trim((string) ($it[$fld] ?? ''));
                if ($s !== '' && !preg_match('/^\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?$/', $s)) {
                    return "{$tag}的 {$fld} 需形如 2026-01-01 或 2026-01-01 08:30";
                }
            }
        }
        return '';
    }

    /** 归一化：补齐 id、裁剪字段、统一类型与默认值。只保留已知字段。 */
    private static function normalizeAds(mixed $v): array
    {
        $out = [];
        foreach ((array) $v as $it) {
            $it = (array) $it;
            $id = trim((string) ($it['id'] ?? ''));
            if ($id === '') {
                $id = bin2hex(random_bytes(6));
            }
            $freq = strtolower(trim((string) ($it['frequency'] ?? 'session')));
            if (!in_array($freq, self::ADS_FREQ, true)) {
                $freq = 'session';
            }
            $out[] = [
                'id' => mb_substr($id, 0, 32),
                'enabled' => self::asBool($it['enabled'] ?? true),
                'mode' => strtolower(trim((string) ($it['mode'] ?? 'popup'))) === 'marquee' ? 'marquee' : 'popup',
                'title' => (string) ($it['title'] ?? ''),
                'content' => (string) ($it['content'] ?? ''),
                'image' => (string) ($it['image'] ?? ''),
                'link' => (string) ($it['link'] ?? ''),
                'link_text' => (string) ($it['link_text'] ?? ''),
                'closable' => self::asBool($it['closable'] ?? true),
                'delay' => self::clampInt($it['delay'] ?? 0, 0, 0, 120),
                'frequency' => $freq,
                'speed' => self::clampInt($it['speed'] ?? 60, 60, 10, 400),
                'position' => strtolower(trim((string) ($it['position'] ?? 'top'))) === 'bottom' ? 'bottom' : 'top',
                'bg' => (string) ($it['bg'] ?? ''),
                'color' => (string) ($it['color'] ?? ''),
                'start' => (string) ($it['start'] ?? ''),
                'end' => (string) ($it['end'] ?? ''),
            ];
        }
        return $out;
    }

    /**
     * 校验单个设置项。
     * @return array{0:bool, 1:mixed, 2:string} [是否通过, 规范化后的值, 失败原因]
     */
    public static function validate(string $key, mixed $raw): array
    {
        $spec = self::byKey()[$key] ?? null;
        if ($spec === null) {
            return [false, null, '未知配置项'];
        }
        $t = $spec['t'];
        $label = $spec['label'] ?? $key;

        switch ($t) {
            case 'bool':
                $ok = in_array($raw, [true, 1, '1', 'true', 'on', 'yes', 'True'], true);
                $off = in_array($raw, [false, 0, '0', 'false', 'off', 'no', '', null, 'False'], true);
                if (!$ok && !$off) {
                    return [false, null, "{$label} 需为开关值"];
                }
                return [true, $ok, ''];

            case 'number': {
                if (is_string($raw)) {
                    $raw = trim($raw);
                }
                if ($raw === '' || !is_numeric($raw)) {
                    return [false, null, "{$label} 必须是数字"];
                }
                $f = (float) $raw;
                if (!is_finite($f)) {          // "1e400" / "inf" / "nan"
                    return [false, null, "{$label} 必须是有限数字"];
                }
                if (isset($spec['min']) && $f < (float) $spec['min']) {
                    return [false, null, "{$label} 不能小于 {$spec['min']}"];
                }
                if (isset($spec['max']) && $f > (float) $spec['max']) {
                    return [false, null, "{$label} 不能大于 {$spec['max']}"];
                }
                return [true, (int) $f === $f ? (int) $f : $f, ''];
            }

            case 'select': {
                $opts = $spec['opts'] ?? [];
                $v = is_scalar($raw) ? (string) $raw : '';
                if (!in_array($v, $opts, true)) {
                    return [false, null, "{$label} 取值非法（可选：" . implode(' / ', $opts) . '）'];
                }
                return [true, $v, ''];
            }

            case 'color': {
                $v = is_scalar($raw) ? trim((string) $raw) : '';
                if ($v === '') {
                    return [true, '', ''];
                }
                // 只接受 #RGB / #RRGGBB —— Python 版原先只校验长度，
                // '#";x"yy' 恰好 7 字符也能过，所以这里显式限定字符集。
                if (!preg_match('/^#(?:[0-9A-Fa-f]{3}|[0-9A-Fa-f]{6})$/', $v)) {
                    return [false, null, "{$label} 需为 #RGB 或 #RRGGBB（仅十六进制）"];
                }
                return [true, $v, ''];
            }

            case 'url': {
                $v = is_scalar($raw) ? trim((string) $raw) : '';
                if ($v === '') {
                    return [true, '', ''];
                }
                $p = parse_url($v);
                if (!is_array($p) || empty($p['scheme']) || empty($p['host'])
                    || !in_array(strtolower($p['scheme']), ['http', 'https'], true)) {
                    return [false, null, "{$label} 需为 http/https 开头的完整地址"];
                }
                return [true, $v, ''];
            }

            case 'email': {
                $v = is_scalar($raw) ? trim((string) $raw) : '';
                if ($v === '') {
                    return [true, '', ''];
                }
                if (!filter_var($v, FILTER_VALIDATE_EMAIL)) {
                    return [false, null, "{$label} 邮箱格式不正确"];
                }
                return [true, $v, ''];
            }

            case 'json': {
                $v = is_string($raw) ? trim($raw) : $raw;
                if (is_string($v)) {
                    if ($v === '') {
                        return [true, [], ''];
                    }
                    $j = json_decode($v, true);
                    if ($j === null && trim($v) !== 'null') {
                        return [false, null, "{$label} 不是合法 JSON"];
                    }
                    $v = $j;
                }
                // 广告位：逐条校验 + 归一化（补齐 id / 裁剪字段），与 Python 版一致
                if ($key === 'ads') {
                    $err = self::validateAds($v);
                    if ($err !== '') {
                        return [false, null, $err];
                    }
                    $v = self::normalizeAds($v);
                }
                return [true, $v, ''];
            }

            case 'textarea':
            case 'text':
            case 'password':
            default: {
                $v = is_scalar($raw) ? (string) $raw : '';
                // 统一按字符数（mb）截断判定，与前端 maxlength 语义一致
                $max = $spec['max'] ?? null;
                if ($max !== null && mb_strlen($v) > (int) $max) {
                    return [false, null, "{$label} 不能超过 {$max} 个字符"];
                }
                return [true, $v, ''];
            }
        }
    }

    /**
     * 写入一个设置项。返回 [成功, 原因]。
     * 校验不过一律不落库 —— 部分写入在 Python 版是被明确避免的。
     */
    public static function set(string $key, mixed $raw): array
    {
        [$ok, $val, $why] = self::validate($key, $raw);
        if (!$ok) {
            return [false, $why];
        }
        $t = self::byKey()[$key]['t'] ?? 'text';
        Db::upsert('settings',
            ['key' => $key, 'value' => self::encode($val, $t), 'updated' => microtime(true)],
            ['key']);
        self::$cache = null;    // 同请求内后续读取立即看到新值
        return [true, ''];
    }

    /**
     * 跨字段一致性校验 —— 单字段 validate() 天生看不到「两个值之间的关系」。
     *
     * 与 Python 版 app/sitesettings.py::cross_check 同规则，返回：
     *   ['blocked' => [key => 原因], 'warnings' => [key => 提示]]
     * blocked 的键**拒绝写入**（保留库里旧值），warnings 只提示、照常写入。
     *
     * @param array<string,mixed> $kv 本次要写入的键值对
     */
    public static function crossCheck(array $kv): array
    {
        $blocked = [];
        $warnings = [];

        $eff = static function (string $k) use ($kv): mixed {
            if (array_key_exists($k, $kv)) {
                [$ok, $val, ] = self::validate($k, $kv[$k]);
                return $ok ? $val : null;
            }
            return self::get($k);
        };

        // 金额区间 min <= max：写反了两个键一起拒 —— 只落一半的区间更危险
        if (array_key_exists('sponsor.min_amount', $kv) || array_key_exists('sponsor.max_amount', $kv)) {
            $lo = $eff('sponsor.min_amount');
            $hi = $eff('sponsor.max_amount');
            if ($lo !== null && $hi !== null && (int) $lo > (int) $hi) {
                $msg = "最低金额（{$lo}）不能大于最高金额（{$hi}）";
                $blocked['sponsor.min_amount'] = $msg;
                $blocked['sponsor.max_amount'] = $msg;
            }
        }

        // 来源选了动态生成，但内容为空 / 没有 {amount} —— 用户点击后要么失败、要么不带金额
        if (array_key_exists('sponsor.qr_kind', $kv) || array_key_exists('sponsor.qr_text', $kv)) {
            $kind = (string) ($eff('sponsor.qr_kind') ?: 'image');
            $txt = (string) ($eff('sponsor.qr_text') ?: '');
            if ($kind === 'text' && trim($txt) === '') {
                $warnings['sponsor.qr_text'] = '「收款码来源」选了 text，但「收款码内容 / 链接」为空 —— '
                    . '用户点击生成收款码时会失败，请填写内容或改回 image';
            } elseif ($kind === 'text' && strpos($txt, '{amount}') === false) {
                $warnings['sponsor.qr_text'] = '「收款码内容」里没有 {amount} 占位符 —— '
                    . '生成的收款码不会带上用户输入的金额';
            }
        }

        return ['blocked' => $blocked, 'warnings' => $warnings];
    }

    public static function setMany(array $kv): array
    {
        $errors = [];
        $norm = [];
        foreach ($kv as $k => $v) {
            [$ok, $val, $why] = self::validate((string) $k, $v);
            if (!$ok) {
                $errors[(string) $k] = $why;
                continue;
            }
            $norm[(string) $k] = [$val, self::byKey()[$k]['t'] ?? 'text'];
        }
        if ($errors) {
            return ['status' => 'error', 'errors' => $errors];
        }
        Db::tx(function () use ($norm) {
            $now = microtime(true);
            foreach ($norm as $k => [$val, $t]) {
                Db::upsert('settings',
                    ['key' => $k, 'value' => self::encode($val, $t), 'updated' => $now], ['key']);
            }
        });
        self::$cache = null;
        return ['status' => 'ok', 'errors' => []];
    }

    /** 供管理端渲染表单：按分组返回定义（含当前值，密钥类不回显明文）。 */
    /**
     * 对外（前台/控制台总览）可见的配置子集 —— **绝不含密钥类字段**。
     *
     * 与 Python 版 app/sitesettings.py 的 public_values() 取同一份键清单，
     * 保证两版对外暴露的配置形状一致。
     */
    public static function publicValues(): array
    {
        $keys = [
            'site_name', 'site_short', 'site_desc', 'logo_url', 'favicon_url',
            'primary_color', 'theme_default', 'hero_title', 'hero_subtitle',
            'devices_hint', 'customize_title', 'announcement', 'footer_text', 'show_help',
            'footer_moat_title', 'footer_moat_text',
            'packages_url', 'contact_email', 'contact_text', 'icp_text',
            'build_default_version', 'build_vip_hint', 'build_allow_anonymous',
            'download.require_login', 'download.link_ttl_hours',
            'sponsor.enabled', 'sponsor.currency', 'sponsor.note', 'sponsor.pay_qr',
            'sponsor.contact', 'sponsor.tiers', 'sponsor.auto_approve',
            // 动态收款码：前台要知道能不能自定义金额、区间多少、来源是图还是实时生成。
            // sponsor.qr_text（收款内容本身）属半敏感，**不下发**。
            'sponsor.custom_amount', 'sponsor.min_amount', 'sponsor.max_amount',
            'sponsor.per_day_price', 'sponsor.qr_kind',
            // 支付：只暴露「是否可用」与必要的前端参数，密钥类字段绝不出门
            'pay.alipay_enabled', 'pay.auto_activate', 'pay.poll_seconds',
            'pay.order_ttl_minutes',
            'builder.enabled', 'builder.concurrency',
        ];
        $out = [];
        foreach ($keys as $k) {
            // 双保险：即使将来有人把密钥键写进上面这份清单，这里也拦住
            if (self::isSecret($k)) {
                $out[$k] = '';
                continue;
            }
            $out[$k] = self::get($k);
        }
        return $out;
    }

    public static function groupsForUi(): array
    {
        $cur = self::load();
        $out = [];
        foreach (SettingsSchema::GROUPS as [$gk, $glabel, $ghint]) {
            $items = [];
            foreach (SettingsSchema::ofGroup($gk) as $s) {
                if (!empty($s['hidden'])) {
                    continue;
                }
                $v = $cur[$s['k']] ?? $s['d'];
                $secret = self::isSecret($s['k']);
                $items[] = [
                    'k'      => $s['k'],
                    'label'  => $s['label'],
                    't'      => $s['t'],
                    'hint'   => $s['hint'] ?? '',
                    'opts'   => $s['opts'] ?? [],
                    'max'    => $s['max'] ?? null,
                    'min'    => $s['min'] ?? null,
                    // 密钥类只回显「是否已设置」，绝不把明文送到前端
                    'value'  => $secret ? '' : (is_array($v) ? json_encode($v, JSON_UNESCAPED_UNICODE) : (string) $v),
                    'is_set' => $secret ? (string) $v !== '' : true,
                    'secret' => $secret,
                ];
            }
            $out[] = ['k' => $gk, 'label' => $glabel, 'hint' => $ghint, 'items' => $items];
        }
        return $out;
    }
}
