<?php
/**
 * 域名绑定 + CDN / 反向代理适配。
 *
 * ---------------------------------------------------------------------------
 * 为什么这一层必须存在（不是「加个配置项」那么简单）
 * ---------------------------------------------------------------------------
 * 本站有两处**以源 IP 为安全依据**的逻辑：
 *   1. 封禁（bans 表，按 IP）
 *   2. 限速（登录失败锁定、注册/重发验证邮件限速）
 * 一旦前面挂了 CDN，REMOTE_ADDR 会变成 **CDN 节点 IP**，于是：
 *   · 封禁会把 CDN 节点整个封掉 → 全站用户一起 403
 *   · 限速会把「所有用户」当成同一个人 → 正常用户被误伤
 * 所以必须取回真实源 IP。
 *
 * 但「取回真实源 IP」本身就是个坑：X-Forwarded-For 是**客户端可伪造**的头。
 * 若不加判别地信任它，攻击者只要发 `X-Forwarded-For: 1.2.3.4`，就能
 * 任意切换身份，封禁与限速**同时失效** —— 比不处理更糟。
 *
 * 因此本层的铁律：
 *   **只有当直连对端（REMOTE_ADDR）本身位于「可信代理/CDN 网段」内时，
 *     才采信转发头；否则一律以 REMOTE_ADDR 为准。**
 *
 * ---------------------------------------------------------------------------
 * 域名绑定
 * ---------------------------------------------------------------------------
 * 绑域名后，所有「绝对 URL」都必须用绑定的域名生成，而不是拿请求头里的 Host 拼。
 * 否则：验证邮件链接、下载链接、支付宝 notify_url 会带上攻击者传入的 Host
 * （Host 头注入 → 邮件里的链接指向攻击者域名 → 账号接管）。
 * 本实现里 Host **只能用来兜底，且必须先过白名单**。
 */
declare(strict_types=1);

namespace Kwrt;

final class Net
{
    /** 默认可信代理网段（RFC1918 回环 + 本机）。生产建议用设置项覆盖。 */
    private const DEFAULT_TRUSTED = [
        '127.0.0.0/8', '::1/128', '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', 'fc00::/7',
    ];

    /** CDN 厂商常用回源头（仅当直连对端可信时才读）。 */
    private const CDN_IP_HEADERS = [
        'HTTP_CF_CONNECTING_IP',   // Cloudflare
        'HTTP_TRUE_CLIENT_IP',     // Akamai / Cloudflare Enterprise
        'HTTP_X_REAL_IP',          // Nginx 反代
        'HTTP_X_CLIENT_IP',
        'HTTP_FASTLY_CLIENT_IP',   // Fastly
        'HTTP_X_AZURE_CLIENTIP',   // Azure Front Door
    ];

    // ------------------------------------------------------------------ 配置

    /** 绑定的域名，如 `fw.example.com`；为空表示未绑定（用请求 Host 兜底）。 */
    public static function boundDomain(): string
    {
        return trim((string) Settings::get('site.domain', ''));
    }

    /** 声明的对外协议：auto / https / http。 */
    public static function schemePref(): string
    {
        return (string) Settings::get('site.scheme', 'auto');
    }

    public static function cdnEnabled(): bool
    {
        return (bool) Settings::get('site.cdn_enabled', false);
    }

    /** CDN 静态资源的对外基址，如 `https://cdn.example.com`。空表示不用 CDN 域名。 */
    public static function cdnBase(): string
    {
        return rtrim(trim((string) Settings::get('site.cdn_base', '')), '/');
    }

    /** 信任的 Host 白名单（逗号/换行分隔）。绑定域名会自动并入。 */
    public static function trustedHosts(): array
    {
        $raw = (string) Settings::get('site.trusted_hosts', '');
        $list = preg_split('/[\s,]+/', $raw, -1, PREG_SPLIT_NO_EMPTY) ?: [];
        $bd = self::boundDomain();
        if ($bd !== '') {
            $list[] = $bd;
        }
        // 允许配置里带协议前缀，统一收敛成纯 host
        $hosts = [];
        foreach ($list as $h) {
            $h = strtolower(trim($h));
            $h = preg_replace('#^[a-z]+://#', '', $h) ?? $h;
            $h = explode('/', $h)[0];
            $h = explode(':', $h)[0];
            if ($h !== '' && $h !== '*') {
                $hosts[$h] = true;
            }
        }
        return array_keys($hosts);
    }

    /** 是否允许「任意 Host」（默认放行，仅在未绑域名时生效）。 */
    public static function allowAnyHost(): bool
    {
        return (bool) Settings::get('site.allow_any_host', true);
    }

    /** 可信代理网段列表（CIDR）。设置项为空时用内置私网段。 */
    public static function trustedProxies(): array
    {
        $raw = trim((string) Settings::get('site.trusted_proxies', ''));
        if ($raw === '') {
            return self::DEFAULT_TRUSTED;
        }
        $out = [];
        foreach (preg_split('/[\s,]+/', $raw, -1, PREG_SPLIT_NO_EMPTY) ?: [] as $cidr) {
            $out[] = trim($cidr);
        }
        return $out ?: self::DEFAULT_TRUSTED;
    }

    // ------------------------------------------------------------------ 源 IP

    /** 请求的直连对端 IP（不可伪造）。 */
    public static function peerIp(): string
    {
        return (string) ($_SERVER['REMOTE_ADDR'] ?? '');
    }

    /**
     * 真实客户端 IP（CDN / 反代感知）。
     *
     * 判定顺序：
     *   1. 直连对端不在可信网段 → **直接用 REMOTE_ADDR**，转发头一概不信
     *   2. 对端可信 → 依次尝试 CDN 专用头，再退到 X-Forwarded-For 的最右侧
     *      非可信项（右往左扫，跳过可信代理，取第一个不可信的）
     *
     * 第 1 条是整个防伪造的关键：不加这条，任何人都能用一个请求头换身份，
     * 封禁和限速会同时失效。
     */
    public static function clientIp(): string
    {
        $peer = self::peerIp();
        if ($peer === '' || !self::ipInAnyList($peer, self::trustedProxies())) {
            return $peer;
        }
        // 对端可信：CDN 专用头优先（这些头由 CDN 覆写，不会被客户端保留）
        foreach (self::CDN_IP_HEADERS as $k) {
            $v = trim((string) ($_SERVER[$k] ?? ''));
            if ($v !== '' && filter_var($v, FILTER_VALIDATE_IP)) {
                return $v;
            }
        }
        // XFF：右往左找第一个「不可信」的地址
        $xff = (string) ($_SERVER['HTTP_X_FORWARDED_FOR'] ?? '');
        if ($xff !== '') {
            $parts = array_reverse(array_map('trim', explode(',', $xff)));
            $trusted = self::trustedProxies();
            foreach ($parts as $ip) {
                if (!filter_var($ip, FILTER_VALIDATE_IP)) {
                    continue;
                }
                if (!self::ipInAnyList($ip, $trusted)) {
                    return $ip;
                }
            }
        }
        return $peer;
    }

    /** 单个 IP 是否落在任一 CIDR / 单 IP 内。 */
    public static function ipInAnyList(string $ip, array $cidrs): bool
    {
        foreach ($cidrs as $c) {
            if (self::ipInCidr($ip, $c)) {
                return true;
            }
        }
        return false;
    }

    public static function ipInCidr(string $ip, string $cidr): bool
    {
        $cidr = trim($cidr);
        if ($cidr === '') {
            return false;
        }
        if (!str_contains($cidr, '/')) {
            return $ip === $cidr;
        }
        [$net, $bits] = explode('/', $cidr, 2);
        $bits = (int) $bits;
        $ipB = @inet_pton($ip);
        $netB = @inet_pton($net);
        if ($ipB === false || $netB === false || strlen($ipB) !== strlen($netB)) {
            return false;
        }
        $len = strlen($ipB) * 8;
        if ($bits < 0 || $bits > $len) {
            return false;
        }
        $wholeBytes = intdiv($bits, 8);
        $remBits = $bits % 8;
        if ($wholeBytes > 0 && substr($ipB, 0, $wholeBytes) !== substr($netB, 0, $wholeBytes)) {
            return false;
        }
        if ($remBits === 0) {
            return true;
        }
        $mask = 0xFF << (8 - $remBits) & 0xFF;
        return (ord($ipB[$wholeBytes]) & $mask) === (ord($netB[$wholeBytes]) & $mask);
    }

    // ------------------------------------------------------------------ URL

    /** 请求当前生效的协议。 */
    public static function scheme(): string
    {
        $pref = self::schemePref();
        if ($pref === 'https' || $pref === 'http') {
            return $pref;
        }
        // auto：只在直连对端可信时才看 X-Forwarded-Proto
        $peer = self::peerIp();
        if ($peer !== '' && self::ipInAnyList($peer, self::trustedProxies())) {
            $fp = strtolower(trim((string) ($_SERVER['HTTP_X_FORWARDED_PROTO'] ?? '')));
            if ($fp !== '') {
                $first = explode(',', $fp)[0];
                if ($first === 'https' || $first === 'http') {
                    return $first;
                }
            }
            // CDN 常在 CF-Visitor 里声明
            $vis = (string) ($_SERVER['HTTP_CF_VISITOR'] ?? '');
            if ($vis !== '' && str_contains(strtolower($vis), 'https')) {
                return 'https';
            }
        }
        return (!empty($_SERVER['HTTPS']) && $_SERVER['HTTPS'] !== 'off') ? 'https' : 'http';
    }

    /** 当前请求的 Host（已做格式校验）。 */
    public static function host(): string
    {
        $h = strtolower(trim((string) ($_SERVER['HTTP_HOST'] ?? '')));
        $h = explode(',', $h)[0];
        $h = trim($h);
        // 允许 host:port；host 部分必须像域名或 IP
        if (!preg_match('/^([a-z0-9.\-]+|\[[0-9a-f:]+\])(?::\d{1,5})?$/', $h)) {
            return '';
        }
        return $h;
    }

    /**
     * 本机回环地址：**永远允许**，与白名单无关。
     *
     * 为什么必须有这条：绑定域名之后，白名单里只剩该域名，
     * 于是从服务器本机（127.0.0.1 / localhost / 内网 IP）访问会被 400 拒绝 ——
     * 包括**管理后台本身**。结果是「设了域名 → 立刻把自己锁在门外，
     * 而且改不回来」。实测踩到过这个坑。
     * 放行回环不会削弱防护：外网请求的 Host 仍是攻击者可控、仍受白名单约束，
     * 而回环要能伪造 Host 意味着攻击者已经能连本机了。
     */
    private const LOOPBACK = ['127.0.0.1', '::1', 'localhost', 'ip6-localhost', '0.0.0.0'];

    /** 请求 Host 是否被允许。 */
    public static function hostAllowed(): bool
    {
        $h = self::host();
        if ($h === '') {
            return false;
        }
        $bare = explode(':', $h)[0];
        $bare = trim($bare, '[]');
        // ① 回环永远放行（防自锁）
        if (in_array($bare, self::LOOPBACK, true)) {
            return true;
        }
        $trusted = self::trustedHosts();
        if (in_array($bare, $trusted, true)) {
            return true;
        }
        // ② 白名单里的私网 IP 也放行（运维常用内网 IP 直连后台）
        if (filter_var($bare, FILTER_VALIDATE_IP) && self::ipInAnyList($bare, self::trustedProxies())) {
            return true;
        }
        // 绑定了域名或配了白名单 → 只认白名单
        if (self::boundDomain() !== '' || $trusted) {
            return false;
        }
        return self::allowAnyHost();
    }

    /**
     * 站点对外基址（**不带尾斜杠**），所有绝对 URL 的唯一来源。
     *
     * 优先级：设置项 site.base_url → 绑定的域名 → 白名单内的请求 Host → 兜底。
     * 注意 Host 兜底之前**必须**过 hostAllowed()，否则就是 Host 头注入。
     */
    public static function baseUrl(): string
    {
        static $cache = null;
        if ($cache !== null) {
            return $cache;
        }
        $explicit = trim((string) Settings::get('site.base_url', ''));
        if ($explicit !== '') {
            return $cache = rtrim($explicit, '/');
        }
        $scheme = self::scheme();
        $bd = self::boundDomain();
        if ($bd !== '') {
            return $cache = $scheme . '://' . $bd;
        }
        if (self::hostAllowed()) {
            $h = self::host();
            if ($h !== '') {
                return $cache = $scheme . '://' . $h;
            }
        }
        // 兜底：拿白名单第一项；再不行用本机地址（绝不回显未校验的 Host）
        $th = self::trustedHosts();
        if ($th) {
            return $cache = $scheme . '://' . $th[0];
        }
        return $cache = $scheme . '://127.0.0.1' . self::portSuffix();
    }

    private static function portSuffix(): string
    {
        $p = (int) ($_SERVER['SERVER_PORT'] ?? 0);
        return ($p > 0 && !in_array($p, [80, 443], true)) ? (':' . $p) : '';
    }

    /** 拼接绝对 URL。 */
    /**
     * 后台自定义页头导航链接（homepage_links）。
     *
     * ★ 这个键一直存在于 schema（JSON 数组，如 [{"label":"文档","url":"/docs"}]）
     *   却两版都无代码使用 —— 管理员配了页头不显示，属「后台能配、前台不见」的死键。
     *   与页面开关不同：这里加的是管理员自定义的外链。
     *
     * 字符白名单：只允许 http(s) 与站内相对路径，
     * 防 javascript: / data: 这类把 XSS 带进**每一页**页头的写法。
     *
     * @return array<int, array{label:string,url:string}>
     */
    public static function homepageLinks(): array
    {
        $raw = Settings::get('homepage_links', []);
        if (is_string($raw)) {
            $raw = json_decode($raw, true) ?: [];
        }
        $out = [];
        foreach ((array) $raw as $item) {
            if (!is_array($item)) {
                continue;
            }
            $label = trim((string) ($item['label'] ?? ''));
            $href = trim((string) ($item['url'] ?? ''));
            if ($label === '' || $href === '') {
                continue;
            }
            $okScheme = str_starts_with($href, 'http://') || str_starts_with($href, 'https://');
            $okRel = str_starts_with($href, '/') && !str_starts_with($href, '//');
            if (!$okScheme && !$okRel) {
                continue;
            }
            $out[] = ['label' => $label, 'url' => $href];
        }
        return $out;
    }

    public static function url(string $path = '/'): string
    {
        return self::baseUrl() . '/' . ltrim($path, '/');
    }

    // ------------------------------------------------------------------ 静态资源

    /**
     * 静态资源的对外基址。挂 CDN 时返回 CDN 域名，否则回源站。
     * 前端所有 <script>/<link> 都必须经这里，否则 CDN 等于没接。
     */
    public static function assetBase(): string
    {
        if (self::cdnEnabled() && self::cdnBase() !== '') {
            return self::cdnBase();
        }
        return self::baseUrl();
    }

    /**
     * 静态资源内容签名：文件一改就变。
     *
     * 为什么需要它：部署新 JS/CSS 后 URL 必须变，否则所有缓存层都会继续吐旧文件。
     * 实测事故：nginx 的**全局** proxy_cache（宝塔 /www/server/nginx/conf/proxy.conf
     * 的 `proxy_cache cache_one;`，被 nginx.conf 直接 include，对所有 proxy_pass 生效）
     * 按 URL 缓存了旧 login.js 一小时，EdgeOne 又叠一层 ——
     * 结果「找回密码」入口在用户端一直不显示。
     *
     * 取 (相对路径, 大小, mtime) 三元组的短哈希：不读文件内容，但部署必然改变
     * mtime/size，够用且极便宜。
     */
    public static function assetSignature(): string
    {
        static $cached = null;
        if ($cached !== null) {
            return $cached;
        }
        $root = Config::root();
        $h = hash_init('sha1');
        foreach (['assets', 'static', 'vendor', 'langs', 'data'] as $sub) {
            $dir = $root . '/web/' . $sub;
            if (!is_dir($dir)) {
                continue;
            }
            // 递归收集（显式排序，保证跨请求结果稳定）
            $files = [];
            $it = new \RecursiveIteratorIterator(
                new \RecursiveDirectoryIterator($dir, \FilesystemIterator::SKIP_DOTS));
            foreach ($it as $f) {
                if ($f->isFile()) {
                    $files[] = $f->getPathname();
                }
            }
            sort($files, SORT_STRING);
            foreach ($files as $fp) {
                $rel = ltrim(str_replace($root, '', $fp), '/');
                hash_update($h, $rel . '|' . (string) filesize($fp) . '|'
                    . (string) filemtime($fp) . "\n");
            }
        }
        return $cached = substr(hash_final($h), 0, 10);
    }

    /**
     * 静态资源版本号：管理员显式配了就用它，否则用**内容签名**。
     *
     * ★ 旧实现回落到一个从未被 define 过的常量 KWRT_BUILD，于是恒为 '1' ——
     *   部署新文件后 URL 纹丝不动，缓存层继续发旧资源。默认值必须内容派生。
     */
    public static function assetVersion(): string
    {
        $v = trim((string) Settings::get('site.asset_version', ''));
        if ($v !== '') {
            return $v;
        }
        return self::assetSignature();
    }

    /** 资源 URL —— **路径式**版本化（/assets/_v/<ver>/js/app.js）。 */
    public static function asset(string $path): string
    {
        $path = ltrim($path, '/');
        // ★ 版本号放**路径**不放 ?v=：实测 EdgeOne 的缓存键忽略查询参数
        //   （全新的 ?v= 仍命中旧缓存）。路径变了才是真的换资源。
        $parts = explode('/', $path, 2);
        if (count($parts) === 2 && in_array($parts[0], ['assets', 'static', 'vendor'], true)) {
            $path = $parts[0] . '/_v/' . self::assetVersion() . '/' . $parts[1];
        }
        return self::assetBase() . '/' . $path;
    }

    /**
     * 静态资源的缓存头。
     *
     * ★ 判据是「这个 URL 是否带 ?v=」，不是「是否配了 CDN」。
     *   实测事故：宝塔反代在 /www/server/nginx/conf/proxy.conf 里有**全局**
     *   `proxy_cache cache_one;`（nginx.conf 直接 include，对所有 proxy_pass 生效），
     *   EdgeOne 再叠一层。只要响应头带可缓存语义，这两层就照抄，
     *   于是部署后用户最长一小时拿到旧 JS/CSS。
     *
     *   带 ?v= 的 URL → 长缓存 immutable（URL 变了就等于换了资源）
     *   裸 URL        → no-cache（让中间层不敢缓存，避免被锁住旧内容）
     *   HTML          → no-cache（保证改设置立即生效）
     */
    public static function assetCacheHeaders(bool $isHtml = false, bool $versioned = false): array
    {
        if ($isHtml || !$versioned) {
            return ['Cache-Control' => 'no-cache, must-revalidate'];
        }
        return [
            'Cache-Control' => 'public, max-age=31536000, immutable',
            // CDN 侧也缓存一年；配合 ?v= 版本号做失效
            'CDN-Cache-Control'       => 'public, max-age=31536000',
            'Cloudflare-CDN-Cache-Control' => 'public, max-age=31536000',
        ];
    }

    /** 是否为「纯静态资源」路径（用于决定缓存策略）。 */
    public static function isStaticPath(string $path): bool
    {
        return (bool) preg_match('#\.(css|js|png|jpe?g|gif|svg|webp|ico|woff2?|ttf|map|json)$#i', $path);
    }

    /** 统一的安全响应头（与 Python 版逐条对齐）。 */
    public static function securityHeaders(): array
    {
        $h = [
            'X-Content-Type-Options' => 'nosniff',
            'X-Frame-Options'        => 'SAMEORIGIN',
            'Referrer-Policy'        => 'strict-origin-when-cross-origin',
            'Permissions-Policy'     => 'geolocation=(), microphone=(), camera=()',
            'Content-Security-Policy' =>
                "default-src 'self'; img-src 'self' data: blob:; "
                . "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
                . "connect-src 'self'; frame-ancestors 'self'; base-uri 'self'; form-action 'self'",
        ];
        // 挂 CDN 时把 CDN 域名、以及常见的支付宝二维码域名放进 CSP，否则图会被拦
        if (self::cdnEnabled() && self::cdnBase() !== '') {
            $cdn = self::cdnBase();
            $h['Content-Security-Policy'] = str_replace(
                "style-src 'self'",
                "style-src 'self' {$cdn}",
                str_replace("script-src 'self'", "script-src 'self' {$cdn}", $h['Content-Security-Policy'])
            );
            $h['Content-Security-Policy'] = str_replace(
                "img-src 'self' data: blob:",
                "img-src 'self' data: blob: {$cdn} https://qr.alipay.com",
                $h['Content-Security-Policy']
            );
        }
        if (self::scheme() === 'https') {
            $h['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains';
        }
        return $h;
    }
}
