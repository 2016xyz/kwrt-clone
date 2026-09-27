<?php
declare(strict_types=1);

namespace Kwrt;

/**
 * 检查本程序自身更新（更新源：GitHub）—— Python 版 app/update.py 的同构实现。
 *
 * 两版必须同构到「同一个远端仓库、同一份设置、同一套比较与缓存语义」，
 * 否则同一个站点用哪一版跑，会得出不一样的「有没有新版本」——
 * 而这恰恰是管理员用来做升级决策的依据，不能有差异。
 *
 * 四处容易做错的地方（与 Python 侧逐条对应）：
 *
 *   1. **版本比较不能按字符串。** `"1.0.10" < "1.0.9"` 在字符串语义下为真，
 *      版本语义下为假。用字符串比会导致「发了 1.0.10 后台却说没有更新」，
 *      且只在跨两位数时出现，很难在测试里撞上。→ self::compare() 逐段转 int。
 *
 *   2. **私有仓库要带 Token，而 Token 绝不能进对外文本。** curl 的错误信息
 *      里可能带完整 URL 与请求头。→ self::safe() 统一抹一遍再返回。
 *
 *   3. **必须缓存。** GitHub 未认证请求每小时 60 次。反复点「检查更新」会把
 *      限额打满，然后连固件构建派发一起挂掉。→ update_checks 表 + 缓存分钟数。
 *
 *   4. **不能假装成功。** 网络不通 / 无权限 / 速率限制，都必须如实回报
 *      「查不了，原因是 X」。把失败说成「已是最新」，会让管理员错过安全更新。
 */
final class Update
{
    private const API = 'https://api.github.com';
    private const UA  = 'Kwrt-Update-Check/1.0';

    /** 版本号形态：可带前导 v，可带 -beta.1 之类后缀。 */
    private const VER_RE = '/^v?(\d+(?:\.\d+){0,3})(?:[-+](.*))?$/';

    // ----------------------------------------------------------------- 配置
    public static function enabled(): bool
    {
        return Settings::bool('update.enabled', true);
    }

    /**
     * 更新源仓库。
     *
     * ★ 留空时**不**回落到 gh.repo —— 那是「固件构建器」仓库，和本程序自己的
     *   源码仓库是两回事。回落到它会让更新检查去比对一个无关仓库的版本号，
     *   比出「有新版本」比查不出来更糟：管理员会照着指引去升级一个不相干的仓库。
     */
    public static function repo(): string
    {
        return trim((string) Settings::get('update.repo', ''));
    }

    /** 更新源 Token；留空回落到 gh.token（同一台机器上通常就是同一个）。 */
    public static function token(): string
    {
        $t = trim((string) Settings::get('update.token', ''));
        return $t !== '' ? $t : trim((string) Settings::get('gh.token', ''));
    }

    public static function includePrerelease(): bool
    {
        return Settings::bool('update.include_prerelease', false);
    }

    public static function ref(): string
    {
        $v = trim((string) Settings::get('update.ref', 'main'));
        return $v !== '' ? $v : 'main';
    }

    public static function cacheMinutes(): int
    {
        $v = Settings::get('update.cache_minutes', 10);
        $n = is_numeric($v) ? (int) $v : 10;
        return max(1, min(1440, $n));
    }

    public static function current(): string
    {
        return Version::raw();
    }

    // ----------------------------------------------------------------- 表
    public static function init(): void
    {
        Db::ensureTable('update_checks');
    }

    // ----------------------------------------------------------------- 版本比较
    /**
     * 把预发布后缀解析成可比较的键（semver 规则）。
     *
     * ★ 只判断「有没有后缀」是不够的：`1.0.0-beta.2` 与 `1.0.0-beta.1` 会被判成
     *   相同，于是开了 update.include_prerelease 的管理员**永远看不到 beta.2**。
     *   逐段比较：纯数字段按数值（`2` < `10`）、数字段排在字母段之前、
     *   字母段按字典序、前缀相同则段数少的更小。
     *
     * @return array<int,array{0:int,1:int,2:string}>
     */
    public static function preKey(string $s): array
    {
        $parts = [];
        foreach (explode('.', $s) as $seg) {
            if ($seg !== '' && ctype_digit($seg)) {
                $parts[] = [0, (int) $seg, ''];
            } else {
                $parts[] = [1, 0, $seg];
            }
        }
        return $parts;
    }

    /**
     * 把版本串解析成 [数字段, 是否正式版, 预发布键]。
     *
     * 逐段转 int 再比 —— 这正是不能按字符串比的原因。
     * 第二项：1 = 正式版，0 = 预发布版（正式版**更新**，与 semver 一致）。
     *
     * @return array{0:int[],1:int,2:array<int,array{0:int,1:int,2:string}>}
     */
    public static function parseVersion(string $s): array
    {
        if (!preg_match(self::VER_RE, trim($s), $m)) {
            return [[], 1, []];
        }
        $nums = array_map('intval', explode('.', $m[1]));
        $pre  = $m[2] ?? '';
        if ($pre !== '') {
            return [$nums, 0, self::preKey($pre)];
        }
        return [$nums, 1, []];
    }

    public static function isPlain(string $s): bool
    {
        return (bool) preg_match(self::VER_RE, trim($s));
    }

    /** $a 与 $b 比较：1 = $a 新，-1 = $b 新，0 = 相同。 */
    public static function compare(string $a, string $b): int
    {
        [$na, $fa, $pa] = self::parseVersion($a);
        [$nb, $fb, $pb] = self::parseVersion($b);
        $len = max(count($na), count($nb));
        for ($i = 0; $i < $len; $i++) {
            $x = $na[$i] ?? 0;
            $y = $nb[$i] ?? 0;
            if ($x !== $y) {
                return $x > $y ? 1 : -1;
            }
        }
        if ($fa !== $fb) {
            return $fa > $fb ? 1 : -1;
        }
        if ($pa !== $pb) {
            // PHP 的数组比较是按元素逐个比、长度作平局判定 —— 与 semver 的
            // 「前缀相同则段数少的更小」正好一致，可以直接用。
            return $pa > $pb ? 1 : -1;
        }
        return 0;
    }

    // ----------------------------------------------------------------- 缓存
    /** @return array{0:?array,1:?float} [结果, 检查时间] */
    public static function loadCache(): array
    {
        try {
            self::init();
            $row = Db::one('SELECT checked_at, payload FROM update_checks WHERE id=1');
        } catch (\Throwable $e) {
            return [null, null];
        }
        if (!$row) {
            return [null, null];
        }
        $j = json_decode((string) $row['payload'], true);
        if (!is_array($j)) {
            // 缓存坏了不该让功能挂掉：当作没有缓存，重新去查
            return [null, null];
        }
        return [$j, (float) $row['checked_at']];
    }

    public static function saveCache(array $payload): void
    {
        self::init();
        Db::run('DELETE FROM update_checks WHERE id=1');
        Db::run('INSERT INTO update_checks(id, checked_at, payload) VALUES(1, ?, ?)',
            [microtime(true), json_encode($payload, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES)]);
    }

    public static function clearCache(): void
    {
        try {
            self::init();
            Db::run('DELETE FROM update_checks WHERE id=1');
        } catch (\Throwable $e) {
            // 清缓存失败无所谓，下次照样会重查
        }
    }

    // ----------------------------------------------------------------- 取数
    /**
     * 把可能出现的 Token 从任何对外文本里抹掉。
     *
     * ★ curl 的错误对象常带 URL、请求头；Token 一旦进了后台界面或审计日志，
     *   就等于把仓库读权限交出去了。这是唯一出口，统一过一遍。
     */
    public static function safe(string $text): string
    {
        $t = self::token();
        if ($t !== '' && strlen($t) >= 8) {
            $text = str_replace($t, '***', $text);
        }
        $text = preg_replace('/gh[pousr]_[A-Za-z0-9]{10,}|github_pat_[A-Za-z0-9_]{10,}/', '***', $text) ?? $text;
        $text = preg_replace('/(?i)(authorization\s*[:=]\s*)\S+/', '${1}***', $text) ?? $text;
        return mb_substr($text, 0, 500);
    }

    /**
     * @return array{0:mixed,1:string} [数据, 错误文本]；错误非空即失败
     */
    private static function get(string $path, int $timeout = 20): array
    {
        $repo = self::repo();
        if ($repo === '') {
            return [null, '尚未配置更新源仓库（后台 → 站点设置 → 程序更新）'];
        }
        if (!preg_match('#^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$#', $repo)) {
            return [null, '更新源仓库格式不对，应为 owner/repo'];
        }
        if (!function_exists('curl_init')) {
            return [null, 'PHP 缺少 curl 扩展，无法访问 GitHub'];
        }

        $headers = [
            'Accept: application/vnd.github+json',
            'X-GitHub-Api-Version: 2022-11-28',
            'User-Agent: ' . self::UA,
        ];
        $tok = self::token();
        if ($tok !== '') {
            $headers[] = 'Authorization: Bearer ' . $tok;
        }

        $ch = curl_init(self::API . $path);
        curl_setopt_array($ch, [
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_TIMEOUT        => $timeout,
            CURLOPT_FOLLOWLOCATION => false,
            CURLOPT_HTTPHEADER     => $headers,
            CURLOPT_HEADER         => true,
        ]);
        $resp = curl_exec($ch);
        $err  = curl_error($ch);
        $code = (int) curl_getinfo($ch, CURLINFO_HTTP_CODE);
        $hlen = (int) curl_getinfo($ch, CURLINFO_HEADER_SIZE);
        curl_close($ch);

        if ($resp === false) {
            return [null, '连不上 GitHub：' . self::safe($err)];
        }
        $raw     = (string) $resp;
        $body    = substr($raw, $hlen);
        $rawHead = substr($raw, 0, $hlen);

        // 速率限制：即使这次成功，剩余 0 也意味着下一次必失败，提前告知
        if (preg_match('/^x-ratelimit-remaining:\s*(\d+)/mi', $rawHead, $rm)
            && (int) $rm[1] === 0) {
            return [null, 'GitHub API 速率已用尽（未认证请求每小时 60 次）；配一个 Token 可提到 5000 次/小时'];
        }

        if ($code === 404) {
            return [null, '仓库或分支不存在，或 Token 无权访问（HTTP 404）。仓库=' . $repo];
        }
        if ($code === 401 || $code === 403) {
            if (preg_match('/^x-ratelimit-remaining:\s*0/mi', $rawHead)) {
                return [null, 'GitHub API 速率已用尽；配一个 Token 可提到 5000 次/小时'];
            }
            return [null, 'Token 无效或权限不足（HTTP ' . $code . '）'];
        }
        if ($code < 200 || $code >= 300) {
            return [null, 'GitHub 返回 HTTP ' . $code];
        }

        $j = json_decode($body, true);
        if ($j === null) {
            return [null, 'GitHub 返回的不是合法 JSON'];
        }
        return [$j, ''];
    }

    /**
     * 从 release 列表里挑出「最新且被允许」的那个。
     *
     * 用列表接口而非 /releases/latest：后者在没有 release 时 404，且**永远排除
     * 预发布版**，无法支持「包含预发布」这个开关。
     */
    private static function pickRelease(array $releases): ?array
    {
        $allowPre = self::includePrerelease();
        $best = null;
        $bestKey = null;
        foreach ($releases as $r) {
            if (!is_array($r)) {
                continue;
            }
            if (!empty($r['draft'])) {
                continue;
            }
            if (!empty($r['prerelease']) && !$allowPre) {
                continue;
            }
            $tag = trim((string) ($r['tag_name'] ?? ''));
            if (!self::isPlain($tag)) {
                continue;
            }
            [$nums, $flag, $pkey] = self::parseVersion($tag);
            $key = [$nums, $flag, $pkey, (string) ($r['published_at'] ?? $r['created_at'] ?? '')];
            if ($bestKey === null || $key > $bestKey) {
                $bestKey = $key;
                $best = $r;
            }
        }
        return $best;
    }

    /**
     * 取远端最新版本。
     *
     * @return array{0:?array,1:string} info = version/tag/published_at/notes/html_url/source
     */
    public static function fetchLatest(): array
    {
        [$data, $err] = self::get('/repos/' . self::repo() . '/releases?per_page=30');
        if ($err !== '') {
            return [null, $err];
        }
        $rel = self::pickRelease(is_array($data) ? $data : []);
        if ($rel) {
            $tag = trim((string) ($rel['tag_name'] ?? ''));
            return [[
                'version'      => (strlen($tag) > 0 && ($tag[0] === 'v' || $tag[0] === 'V'))
                                    ? substr($tag, 1) : $tag,
                'tag'          => $tag,
                'published_at' => (string) ($rel['published_at'] ?? ''),
                'notes'        => mb_substr((string) ($rel['body'] ?? ''), 0, 4000),
                'html_url'     => (string) ($rel['html_url'] ?? ''),
                'name'         => (string) ($rel['name'] ?? ''),
                'source'       => 'release',
            ], ''];
        }

        // 退回：读指定分支上的 VERSION 文件
        [$data, $err] = self::get('/repos/' . self::repo() . '/contents/VERSION?ref='
                                  . rawurlencode(self::ref()));
        if ($err !== '') {
            return [null, $err];
        }
        if (!is_array($data) || !isset($data['content'])) {
            return [null, '无法从 GitHub 读取版本信息（既没有 Release，也没有 VERSION 文件）'];
        }
        $decoded = base64_decode((string) $data['content'], false);
        if ($decoded === false) {
            return [null, 'VERSION 文件内容无法解码'];
        }
        $text = trim($decoded);
        if (!self::isPlain($text)) {
            return [null, 'VERSION 文件内容不是合法版本号：' . self::safe(mb_substr($text, 0, 40))];
        }
        return [[
            'version'      => $text,
            'tag'          => 'v' . $text,
            'published_at' => '',
            'notes'        => '',
            'html_url'     => (string) ($data['html_url'] ?? ''),
            'name'         => '',
            'source'       => 'version_file',
        ], ''];
    }

    // ----------------------------------------------------------------- 对外
    /**
     * 检查更新。返回数组**永远**带 status（ok / error），不抛异常。
     *
     * ★ 失败时绝不返回「已是最新」—— 那会让管理员错过安全更新。
     */
    public static function check(bool $force = false): array
    {
        $cur = self::current();
        $out = [
            'status'          => 'ok',
            'current'         => $cur,
            'current_display' => 'v' . $cur,
            'latest'          => '',
            'latest_display'  => '',
            'has_update'      => false,
            'source'          => '',
            'published_at'    => '',
            'notes'           => '',
            'html_url'        => '',
            'checked_at'      => 0,
            'cached'          => false,
            'message'         => '',
            'repo'            => self::repo(),
        ];

        if (!self::enabled()) {
            $out['status']  = 'error';
            $out['message'] = '更新检查已在后台关闭（update.enabled）';
            return $out;
        }

        if (!$force) {
            [$payload, $ts] = self::loadCache();
            // ★ 缓存必须与**当前配置**绑定。管理员改了更新源仓库（或「包含预发布」
            //   开关）之后，旧仓库得出的结论对新配置毫无意义 —— 继续端出来就是
            //   拿一个不相干仓库的版本号误导升级决策。
            if (is_array($payload) && $ts !== null
                && (microtime(true) - $ts) < self::cacheMinutes() * 60
                && ($payload['repo'] ?? '') === self::repo()
                && (bool) ($payload['prerelease'] ?? false) === self::includePrerelease()) {
                $payload['cached']          = true;
                $payload['checked_at']      = (int) $ts;
                // 缓存的是「远端最新」，不是「本地版本」——当前版本用本地实时值覆盖，
                // 否则升级完还显示旧版本号。
                $payload['current']         = $cur;
                $payload['current_display'] = 'v' . $cur;
                $payload['has_update']      = !empty($payload['latest'])
                    && self::compare((string) $payload['latest'], $cur) > 0;
                $payload['message']         = self::message($payload);
                return $payload;
            }
        }

        [$info, $err] = self::fetchLatest();
        if ($err !== '' || !$info) {
            $out['status']  = 'error';
            $out['message'] = $err !== '' ? $err : '未能从更新源取到版本信息';
            return $out;
        }

        $latest = (string) $info['version'];
        if (!self::isPlain($latest)) {
            $out['status']  = 'error';
            $out['message'] = '远端版本号不合法：' . self::safe(mb_substr($latest, 0, 40));
            return $out;
        }

        $out['latest']          = $latest;
        $out['latest_display']  = 'v' . $latest;
        $out['has_update']      = self::compare($latest, $cur) > 0;
        $out['source']          = (string) $info['source'];
        $out['published_at']    = (string) $info['published_at'];
        $out['notes']           = (string) $info['notes'];
        $out['html_url']        = (string) $info['html_url'];
        $out['name']            = (string) ($info['name'] ?? '');
        $out['checked_at']      = time();
        $out['message']         = self::message($out);

        // ★ 只缓存**成功**的结果：把一次网络故障缓存 10 分钟，会让管理员
        //   点「强制」也拿不到新结果，以为功能坏了。
        self::saveCache([
            'status'       => $out['status'],
            'latest'       => $out['latest'],
            'latest_display' => $out['latest_display'],
            'source'       => $out['source'],
            'published_at' => $out['published_at'],
            'notes'        => $out['notes'],
            'html_url'     => $out['html_url'],
            'name'         => $out['name'],
            'repo'         => $out['repo'],
            // 配置指纹：仓库/预发布开关变了，这份缓存就作废（见上面的读取处）
            'prerelease'   => self::includePrerelease(),
        ]);
        return $out;
    }

    private static function message(array $r): string
    {
        if (!empty($r['has_update'])) {
            return '发现新版本 ' . ($r['latest_display'] ?? '') . '（当前 ' . ($r['current_display'] ?? '') . '）';
        }
        return '已是最新版本 ' . ($r['current_display'] ?? '');
    }
}
