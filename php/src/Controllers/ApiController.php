<?php
/**
 * 公开 API 控制器。
 *
 * 磁盘与安全相关的出口都在这里：软件包查询、构建提交与状态、下载令牌、
 * 产物读取、赞助与支付。每个入口都做归属校验，不做「先查再判」的两段式。
 */
declare(strict_types=1);

namespace Kwrt\Controllers;

use Kwrt\Auth;
use Kwrt\Builder;
use Kwrt\Catalog;
use Kwrt\Config;
use Kwrt\Db;
use Kwrt\Download;
use Kwrt\Net;
use Kwrt\Pages;
use Kwrt\Pay;
use Kwrt\Qr;
use Kwrt\Releases;
use Kwrt\Settings;
use Kwrt\Sponsor;
use Kwrt\Util;
use Kwrt\View;

final class ApiController
{
    // ---------------------------------------------------------------- 健康检查

    public function healthz(array $p): string
    {
        $q = Builder::queueStats();
        json_out([
            'ok' => true,
            'jobs' => $q['running'] + $q['queued'],
            'builds_done' => $q['done'],
            'builder_enabled' => $q['enabled'],
            'concurrency' => $q['max'],
            'backend' => $q['backend'],
            'mail' => \Kwrt\Mailer::enabled(),
            // 与 Python 版同字段名，且同源（都读仓库根 VERSION）
            'version' => \Kwrt\Version::raw(),
        ]);
    }

    // ---------------------------------------------------------------- 站点

    /** GET /api/v1/captcha —— 签发一张验证码（与 Python 版同表同盐，可互相校验）。 */
    public function captcha(array $p): string
    {
        // ★ 三个开关都要判。原先只判 enabled()/enabledForRegister()，
        //   于是「只开 security.captcha_on_reset」时这个端点返回 404，
        //   找回密码页取不到验证码 → **整个找回流程不可用**。
        //   （Python 版同一个位置也犯过同样的错，已一并修掉。）
        if (!\Kwrt\Captcha::enabled() && !\Kwrt\Captcha::enabledForRegister()
            && !\Kwrt\Captcha::enabledForReset()) {
            json_out(['status' => 'error', 'detail' => '验证码未启用'], 404);
        }
        try {
            $d = \Kwrt\Captcha::issue(\Kwrt\Net::clientIp());
        } catch (\RuntimeException $e) {
            json_out(['status' => 'error', 'detail' => $e->getMessage()], 429);
        }
        header('Cache-Control: no-store');
        json_out(['status' => 'ok', 'id' => $d['id'], 'svg' => $d['svg'],
                  'ttl' => $d['ttl'], 'length' => $d['length']]);
    }

    public function site(array $p): string
    {
        $s = Settings::load();
        $branches = [];
        $f = Config::path('data', 'json', 'v1', 'overview.json');
        if (is_file($f)) {
            $ov = json_decode((string) file_get_contents($f), true);
            foreach (($ov['branches'] ?? []) as $name => $b) {
                if (isset($b['enabled']) && !$b['enabled']) {
                    continue;
                }
                $branches[(string) $name] = true;
            }
        }
        json_out([
            'status' => 'ok',
            'site' => [
                'name' => $s['site_name'] ?? '',
                'short' => $s['site_short'] ?? '',
                'desc' => $s['site_desc'] ?? '',
                'logo' => Net::asset((string) ($s['logo_url'] ?? '')),
                'color' => $s['primary_color'] ?? '#2563eb',
                'theme' => $s['theme_default'] ?? 'auto',
            ],
            'branches' => array_keys($branches) ?: ['25.12'],
            'nav' => Pages::nav(),
            'version' => \Kwrt\Version::display(),
            // 前端据此决定登录/注册表单里是否显示验证码输入框（只暴露真假）
            'captcha' => [
                'login' => \Kwrt\Captcha::enabled(),
                'register' => \Kwrt\Captcha::enabledForRegister(),
                'reset' => \Kwrt\Captcha::enabledForReset(),
            ],
            'entry' => [
                'app' => (bool) ($s['entry.app_enabled'] ?? false),
                'wechat' => (bool) ($s['entry.wechat_enabled'] ?? false),
            ],
            'devices' => (new PublicController())->devices(),
            'sponsor' => ['enabled' => (bool) ($s['sponsor.enabled'] ?? true)],
            // 广告位：原始列表交由前端按 enabled / 时间窗 / 频率自行过滤渲染
            'ads' => is_array($s['ads'] ?? null) ? $s['ads'] : [],
        ]);
    }

    public function announcement(array $p): string
    {
        json_out(['status' => 'ok', 'announcement' => (string) Settings::get('announcement', '')]);
    }

    public function user(array $p): string
    {
        $u = Auth::currentUser();
        if (!$u) {
            json_out(['status' => 'error', 'detail' => '未登录'], 401);
        }
        json_out([
            'status' => 'ok',
            'username' => $u['username'],
            'email' => $u['email'] ?? '',
            'email_verified' => (int) ($u['email_verified'] ?? 1) === 1,
            'is_admin' => Auth::isAdmin($u),
            'sponsor' => Auth::isSponsor($u),
            'sponsor_until' => (float) ($u['sponsor_until'] ?? 0),
            'sponsor_tier' => (string) ($u['sponsor_tier'] ?? ''),
            'quota' => (int) ($u['quota'] ?? 0),
        ]);
    }

    public function sponsorTiers(array $p): string
    {
        $raw = Settings::get('sponsor.tiers', []);
        if (is_string($raw)) {
            $raw = json_decode($raw, true) ?: [];
        }
        // ★ 补齐 Python 版返回的 currency / note / pay_qr。
        //   原先只回 tiers+enabled，导致 PHP 的赞助页
        //   既不显示后台配置的「赞助说明」，也不显示「收款码图片」
        //   —— 后台能配、前台看不见，又是一类静默失效。
        json_out(['status' => 'ok',
                  'tiers' => array_values((array) $raw),
                  'enabled' => Settings::bool('sponsor.enabled', true),
                  'currency' => (string) Settings::get('sponsor.currency', 'CNY'),
                  'note' => (string) Settings::get('sponsor.note', ''),
                  'pay_qr' => (string) Settings::get('sponsor.pay_qr', ''),
                  // 动态收款码的前端参数（与 Python /api/v1/sponsor/tiers 对齐）：
                  // 前端据此决定是否画金额输入框、区间多少、按钮写"生成收款码"还是"扫码支付"。
                  'custom_amount' => Settings::bool('sponsor.custom_amount', true),
                  'min_amount' => Sponsor::amountRange()[0],
                  'max_amount' => Sponsor::amountRange()[1],
                  'qr_kind' => (string) (Settings::get('sponsor.qr_kind', 'image') ?: 'image'),
                  'pay_available' => Pay::available()]);
    }

    /**
     * 统一下单入口 —— 用户输入金额（或选套餐）点击后调用。
     * 与 Python /api/v1/sponsor/order 对齐：
     *   mode=alipay 有当面付 → 真实下单，url 指向该订单的二维码
     *   mode=manual 没有当面付 → url 指向服务端**实时生成**的收款码图片
     */
    public function sponsorOrder(array $p): string
    {
        $u = Auth::currentUser();
        if (!$u) {
            json_out(['status' => 'error', 'detail' => '请先登录'], 401);
        }
        if (!Settings::bool('sponsor.enabled', true)) {
            json_out(['status' => 'error', 'detail' => '本站未开启赞助'], 400);
        }
        $tier = trim((string) input('tier', ''));
        [$amount, $aerr] = Sponsor::parseAmount(input('amount', ''));
        if ($aerr !== '') {
            json_out(['status' => 'error', 'detail' => $aerr], 400);
        }
        [$choice, $err] = Sponsor::resolve($tier, $amount);
        if ($choice === null) {
            json_out(['status' => 'error', 'detail' => $err], 400);
        }
        $currency = (string) Settings::get('sponsor.currency', 'CNY');

        if (Pay::available()) {
            $out = Pay::precreate((string) $u['username'], (string) $choice['tier'],
                (float) $choice['amount'], (int) $choice['days']);
            if (!$out['ok']) {
                json_out(['status' => 'error', 'detail' => $out['detail']], 502);
            }
            json_out([
                'status' => 'ok',
                'mode' => 'alipay',
                'out_trade_no' => $out['out_trade_no'],
                'url' => Net::url('/api/v1/sponsor/pay/' . rawurlencode((string) $out['out_trade_no']) . '/qr.png'),
                'amount' => $choice['amount'],
                'days' => $choice['days'],
                'tier' => $choice['tier'],
                'currency' => $currency,
                'expires_in' => max(0, (int) $out['expires'] - time()),
            ]);
        }

        $problem = Sponsor::qrConfigProblem();
        if ($problem !== '') {
            json_out(['status' => 'error', 'detail' => $problem], 503);
        }
        json_out([
            'status' => 'ok',
            'mode' => 'manual',
            'url' => Net::url('/api/v1/sponsor/qr.png?amount=' . rawurlencode(Sponsor::num((float) $choice['amount']))),
            'qr_kind' => (string) (Settings::get('sponsor.qr_kind', 'image') ?: 'image'),
            'amount' => $choice['amount'],
            'days' => $choice['days'],
            'tier' => $choice['tier'],
            'currency' => $currency,
            'self_confirm' => Settings::bool('sponsor.custom_amount_auto', false) || $tier !== '',
        ]);
    }

    /**
     * ★「收款码图片地址」的服务端入口 —— 用户输入金额点击后，前端把它当图片加载。
     *   kind=image → 302 跳到站长填的固定图片地址；
     *   kind=text  → 把 {amount} 替换进站长填的内容，用 Qr 实时出 PNG。
     *
     * 这里**必须自己画图**而不是跳外部二维码接口：收款内容含站长的收款标识，
     * 交给第三方等于把收款信息送给别人。
     */
    public function sponsorQr(array $p): string
    {
        $u = Auth::currentUser();
        if (!$u) {
            http_response_code(401);
            header('Content-Type: application/json; charset=utf-8');
            echo json_encode(['status' => 'error', 'detail' => '请先登录'], JSON_UNESCAPED_UNICODE);
            exit;
        }
        [$amount, $aerr] = Sponsor::parseAmount(input('amount', ''));
        if ($aerr !== '') {
            http_response_code(400);
            header('Content-Type: application/json; charset=utf-8');
            echo json_encode(['status' => 'error', 'detail' => $aerr], JSON_UNESCAPED_UNICODE);
            exit;
        }
        if ($amount <= 0) {
            http_response_code(400);
            header('Content-Type: application/json; charset=utf-8');
            echo json_encode(['status' => 'error', 'detail' => '缺少金额参数'], JSON_UNESCAPED_UNICODE);
            exit;
        }
        [$lo, $hi] = Sponsor::amountRange();
        if ($amount < $lo || $amount > $hi) {
            http_response_code(400);
            header('Content-Type: application/json; charset=utf-8');
            echo json_encode(['status' => 'error', 'detail' => "金额需在 {$lo} ~ {$hi} 之间"],
                JSON_UNESCAPED_UNICODE);
            exit;
        }
        $amount = round($amount, 2);

        $kind = (string) (Settings::get('sponsor.qr_kind', 'image') ?: 'image');
        if ($kind === 'image') {
            $url = trim((string) Settings::get('sponsor.pay_qr', ''));
            if ($url === '') {
                http_response_code(404);
                header('Content-Type: application/json; charset=utf-8');
                echo json_encode(['status' => 'error', 'detail' => '站长尚未配置收款码图片地址'],
                    JSON_UNESCAPED_UNICODE);
                exit;
            }
            header('Location: ' . $url, true, 302);
            exit;
        }

        $tpl = trim((string) Settings::get('sponsor.qr_text', ''));
        if ($tpl === '') {
            http_response_code(404);
            header('Content-Type: application/json; charset=utf-8');
            echo json_encode(['status' => 'error', 'detail' => '站长尚未配置收款码内容 / 链接'],
                JSON_UNESCAPED_UNICODE);
            exit;
        }
        try {
            $png = Qr::png(Sponsor::renderQrText($tpl, $amount));
        } catch (\Throwable $ex) {
            http_response_code(500);
            header('Content-Type: application/json; charset=utf-8');
            echo json_encode(['status' => 'error', 'detail' => '收款码生成失败：' . $ex->getMessage()],
                JSON_UNESCAPED_UNICODE);
            exit;
        }
        header('Content-Type: image/png');
        header('Content-Length: ' . strlen($png));
        header('Cache-Control: private, max-age=300');
        echo $png;
        exit;
    }

    public function payInfo(array $p): string
    {
        // ★ 币种键是 sponsor.currency（下拉：CNY/USD/EUR/JPY/HKD），
        //   不是 pay.currency。原先读 pay.currency 的后果：管理员在下拉里
        //   选了 USD/EUR/JPY/HKD 也不生效，且两版显示**不同的币种**
        //   （Python 读 sponsor.currency）。pay.currency 只是另一个自由文本键。
        // ★ 同时补上 Python 版有而这里缺的字段 —— 两版响应形状必须一致，
        //   前端同一份代码要能吃两版的后端。
        json_out([
            'status' => 'ok',
            'available' => Pay::enabled(),
            'enabled' => Settings::bool('pay.alipay_enabled', false),
            'configured' => Pay::configured(),
            'refund_enabled' => Settings::bool('pay.refund_enabled', true),
            'poll_seconds' => (int) Settings::get('pay.poll_seconds', 3),
            'order_ttl_minutes' => (int) Settings::get('pay.order_ttl_minutes', 15),
            'currency' => (string) Settings::get('sponsor.currency', 'CNY'),
            'note' => (string) Settings::get('pay.result_note', ''),
        ]);
    }

    // ---------------------------------------------------------------- 软件包

    public function packagesCatalog(array $p): string
    {
        json_out([
            'status' => 'ok',
            'presets' => Catalog::presets(),
            'cats' => Catalog::cats(),
            'suites' => Catalog::suites(),
        ]);
    }

    public function packagesArchs(array $p): string
    {
        json_out(['status' => 'ok', 'archs' => (new PublicController())->archs()]);
    }

    /** 在离线索引里搜索软件包（服务端流式过滤，不把整个索引读进内存）。 */
    public function packagesSearch(array $p): string
    {
        $q = trim((string) ($_GET['q'] ?? ''));
        $arch = trim((string) ($_GET['arch'] ?? ''));
        $src = trim((string) ($_GET['src'] ?? 'packages-25.12'));
        $limit = max(1, min(200, (int) ($_GET['limit'] ?? 60)));

        // 白名单：这两个参数直接参与路径拼接
        if (!preg_match('/^[a-z0-9_.\\-]{1,60}$/', $src)) {
            json_out(['status' => 'error', 'detail' => '数据源标识非法'], 400);
        }
        if ($arch !== '' && !preg_match('/^[A-Za-z0-9_.\\-]{1,60}$/', $arch)) {
            json_out(['status' => 'error', 'detail' => '架构标识非法'], 400);
        }
        if (mb_strlen($q) > 80) {
            json_out(['status' => 'error', 'detail' => '关键字过长'], 400);
        }

        $dir = Config::path('data', 'json', 'v1', 'releases', $src);
        $files = [];
        if (is_dir($dir)) {
            foreach (scandir($dir) ?: [] as $f) {
                if (!str_ends_with($f, '-index.json')) {
                    continue;
                }
                $a = str_replace('-index.json', '', $f);
                if ($arch === '' || $a === $arch) {
                    $files[] = [$a, $dir . '/' . $f];
                }
            }
        }
        if (!$files) {
            json_out(['status' => 'ok', 'results' => [], 'total' => 0,
                      'hint' => '该数据源下没有索引文件']);
        }

        $needle = mb_strtolower($q);
        $out = [];
        $total = 0;
        foreach ($files as [$archName, $path]) {
            $raw = @file_get_contents($path);
            if ($raw === false) {
                continue;
            }
            $idx = json_decode($raw, true);
            if (!is_array($idx)) {
                continue;
            }
            foreach ($idx as $name => $rec) {
                $hit = $needle === ''
                    || str_contains(mb_strtolower((string) $name), $needle)
                    || str_contains(mb_strtolower((string) ($rec['description'] ?? '')), $needle);
                if (!$hit) {
                    continue;
                }
                $total++;
                if (count($out) < $limit) {
                    $out[] = [
                        'name' => (string) $name,
                        'version' => (string) ($rec['version'] ?? ''),
                        'arch' => (string) ($rec['arch'] ?? $archName),
                        'size' => (int) ($rec['size'] ?? 0),
                        'description' => (string) ($rec['description'] ?? ''),
                        'src' => (string) $src,
                    ];
                }
            }
            if (count($out) >= $limit && $needle !== '') {
                break;
            }
        }
        json_out(['status' => 'ok', 'results' => $out, 'total' => $total,
                  'src' => $src, 'arch' => $arch]);
    }

    // ---------------------------------------------------------------- 插件提议

    public function proposals(array $p): string
    {
        $rows = Db::all('SELECT id, name, url, note, status, reply, created FROM proposals '
            . "WHERE status != 'rejected' ORDER BY created DESC LIMIT 100");
        json_out(['status' => 'ok', 'proposals' => $rows]);
    }

    public function propose(array $p): string
    {
        $ip = Net::clientIp();
        [$ok, $wait] = Auth::rateOk('propose', $ip, 5, 600);
        if (!$ok) {
            json_out(['status' => 'error', 'detail' => "提交过于频繁，请 {$wait} 秒后再试"], 429);
        }
        $name = trim((string) input('name', ''));
        $url = trim((string) input('url', ''));
        $note = trim((string) input('note', ''));
        if ($name === '' || mb_strlen($name) > 80) {
            json_out(['status' => 'error', 'detail' => '请填写插件名称（不超过 80 字）'], 400);
        }
        if ($url !== '') {
            if (mb_strlen($url) > 300) {
                json_out(['status' => 'error', 'detail' => '链接过长'], 400);
            }
            $scheme = strtolower((string) (parse_url($url, PHP_URL_SCHEME) ?: ''));
            if (!in_array($scheme, ['http', 'https'], true)) {
                json_out(['status' => 'error', 'detail' => '链接需以 http/https 开头'], 400);
            }
        }
        if (mb_strlen($note) > 600) {
            json_out(['status' => 'error', 'detail' => '说明过长'], 400);
        }
        Db::run('INSERT INTO proposals(name, url, note, created, status, reply, ip) '
            . 'VALUES(?,?,?,?,?,?,?)',
            [$name, $url, $note, microtime(true), 'pending', '', $ip]);
        json_out(['status' => 'ok']);
    }

    public function proposeDelete(array $p): string
    {
        if (!Auth::isAdmin()) {
            json_out(['status' => 'error', 'detail' => '需要管理员权限'], 403);
        }
        $pid = (int) ($p['pid'] ?? 0);
        Db::run('DELETE FROM proposals WHERE id=?', [$pid]);
        Auth::audit('proposal_delete', (string) $pid);
        json_out(['status' => 'ok']);
    }

    // ---------------------------------------------------------------- 构建

    public function buildSubmit(array $p): string
    {
        $u = Auth::currentUser();
        if (!$u) {
            json_out(['status' => 'error', 'detail' => '请先登录'], 401);
        }
        $sponsor = Auth::isSponsor($u);

        try {
            $target = Util::checkTarget((string) input('target', ''));
            $profile = Util::checkProfile((string) input('profile', ''));
            $version = Util::checkVersion((string) input('version', '25.12'));
            $pkgs = Util::packages((array) (input('packages', []) ?: []));
            $extra = (string) input('extra', '');
            if ($extra !== '') {
                $pkgs = array_merge($pkgs, Util::packages(preg_split('/\\s+/', trim($extra)) ?: []));
            }
        } catch (\InvalidArgumentException $e) {
            json_out(['status' => 'error', 'error_code' => 'BAD_PARAM', 'detail' => $e->getMessage()], 400);
        }

        // ★ 界面下拉框给的是**分支号**（25.12），而镜像站只认**发布号**（25.12.5）：
        //   releases/25.12/…   → 404      releases/25.12.5/… → 200
        //   不解析就会卡在「无法下载 ImageBuilder」—— 真用户从界面构建必然失败。
        //   （勾了只有第三方源才有的插件时，会自动回落到 opkg 后端，见 Releases::resolveForBuild）
        $rel = Releases::resolveForBuild($version, $pkgs);
        $version = $rel['release'];
        $note = '';
        if (!empty($rel['fallback_from'])) {
            $note = '所选插件需第三方源，版本由 ' . $rel['fallback_from']
                . ' 回落至 ' . $rel['release'];
        }

        $fs = (string) input('filesystem', 'squashfs');
        if (!in_array($fs, ['squashfs', 'ext4'], true)) {
            json_out(['status' => 'error', 'detail' => '文件系统取值非法'], 400);
        }
        $rootfs = (int) input('rootfs_size_mb', 512);
        if ($rootfs < 128 || $rootfs > 4096) {
            json_out(['status' => 'error', 'detail' => '根分区容量需在 128–4096 MB'], 400);
        }
        if (!$sponsor && $rootfs > 512) {
            json_out(['status' => 'error', 'detail' => '自定义根分区容量为赞助用户功能'], 403);
        }

        // 配额
        $quota = (int) ($u['quota'] ?? 12);
        if ($quota > 0 && !$sponsor) {
            $used = (int) Db::val('SELECT COUNT(*) FROM builds WHERE username=? AND created>?',
                [$u['username'], strtotime('today 00:00')], 0);
            if ($used >= $quota) {
                json_out(['status' => 'error', 'detail' => "今日构建次数已达上限（{$quota} 次）"], 429);
            }
        }
        if (!Builder::enabled()) {
            json_out(['status' => 'error', 'detail' => '构建服务暂时关闭'], 503);
        }

        $filesPath = (string) input('files_path', '');
        $payload = [
            'target' => $target,
            'profile' => $profile,
            'version' => $version,
            'packages' => array_values(array_unique($pkgs)),
            'filesystem' => $fs,
            'rootfs_size_mb' => $rootfs,
            'hostname' => mb_substr((string) input('hostname', ''), 0, 63),
            'lan_ip' => mb_substr((string) input('lan_ip', ''), 0, 40),
            'ipv6' => (bool) input('ipv6', false),
            'dhcp' => (bool) input('dhcp', true),
            'eflasher' => (bool) input('eflasher', false),
            'usb_net' => (bool) input('usb_net', false),
            'usb_wireless' => (bool) input('usb_wireless', false),
            'wanlan' => (bool) input('wanlan', false),
            'files_path' => $filesPath,
            'note' => $note,          // 版本回落提示，随构建记录留存
        ];
        // 赞助专属项：非赞助用户即使伪造请求体也强制归零
        if (!$sponsor) {
            $payload['eflasher'] = false;
            $payload['usb_wireless'] = false;
            $payload['files_path'] = '';
        }

        $hash = Util::requestHash($payload);
        Db::upsert('builds', [
            'request_hash' => $hash, 'username' => $u['username'], 'target' => $target,
            'profile' => $profile, 'packages' => implode(' ', $payload['packages']),
            'status' => 'queued', 'created' => microtime(true),
            'payload' => json_encode($payload, JSON_UNESCAPED_UNICODE),
        ], ['request_hash']);
        Builder::enqueue($hash, $payload);
        [$ok, $why, $pos] = Builder::pump();
        Auth::audit('build_submit', $hash, ['target' => $target, 'profile' => $profile]);

        json_out(['status' => 'ok', 'request_hash' => $hash, 'queued' => !$ok,
                  'position' => $pos, 'detail' => $why,
                  // 有回落时一并告知，避免用户以为按所选版本构建
                  'version' => $version, 'note' => $note]);
    }

    public function buildStatus(array $p): string
    {
        $hash = (string) ($p['hash_'] ?? '');
        if (!preg_match(Util::HASH_RE, $hash)) {
            json_out(['status' => 'error', 'detail' => '请求标识非法'], 400);
        }
        $b = Db::one('SELECT * FROM builds WHERE request_hash=?', [$hash]);
        $j = Builder::job($hash);
        $status = $j['status'] ?? ($b['status'] ?? 'unknown');
        $detail = $j['detail'] ?? '';

        $links = [];
        if ($status === 'done') {
            $u = Auth::currentUser();
            // 只给已登录用户签发令牌；链接是能力型 URL，有效期有限
            if ($u) {
                foreach (Download::listFor((string) $u['username'], $hash, true) as $t) {
                    $links[] = [
                        'filename' => $t['filename'],
                        'url' => Net::url('/dl/t/' . $t['token']),
                        'expires' => (float) $t['expires'],
                    ];
                }
            }
        }
        $pos = $status === 'queued'
            ? (int) Db::val("SELECT COUNT(*) FROM jobs WHERE status='queued' AND created<?",
                [$b['created'] ?? microtime(true)], 0) + 1
            : 0;
        json_out([
            'status' => $status,
            'detail' => $detail,
            'position' => $pos,
            'target' => $b['target'] ?? '',
            'profile' => $b['profile'] ?? '',
            'download_links' => $links,
        ]);
    }

    public function buildAttachFiles(array $p): string
    {
        $u = Auth::currentUser();
        if (!$u) {
            json_out(['status' => 'error', 'detail' => '请先登录'], 401);
        }
        json_out(['status' => 'error', 'detail' => '请在提交构建时一并上传文件包'], 400);
    }

    /** 上传自定义文件包（仅赞助用户）。 */
    public function upload(array $p): string
    {
        $u = Auth::currentUser();
        if (!$u) {
            json_out(['status' => 'error', 'detail' => '请先登录'], 401);
        }
        if (!Auth::isSponsor($u)) {
            json_out(['status' => 'error', 'detail' => '上传为赞助用户功能'], 403);
        }
        if (empty($_FILES['file']) || !is_array($_FILES['file'])) {
            json_out(['status' => 'error', 'detail' => '没有收到文件'], 400);
        }
        $f = $_FILES['file'];
        if ((int) $f['error'] !== UPLOAD_ERR_OK) {
            json_out(['status' => 'error', 'detail' => '上传失败（错误码 ' . (int) $f['error'] . '）'], 400);
        }
        // 上传上限两版一致地硬编码为 200 MB（Python: app/main.py `limit = 200 * 1024 * 1024`）。
        // 不读 Settings —— schema 里没有这个键，读了会让人误以为后台可调，实际永远取默认值。
        $maxMb = 200;
        if ((int) $f['size'] > $maxMb * 1024 * 1024) {
            json_out(['status' => 'error', 'detail' => "文件超过 {$maxMb} MB 上限"], 400);
        }
        $name = Util::safeFilename((string) $f['name']);
        if ($name === '') {
            json_out(['status' => 'error', 'detail' => '文件名不合法'], 400);
        }
        if (!preg_match('/\\.(zip|7z|tar\\.gz|tgz)$/i', $name)) {
            json_out(['status' => 'error', 'detail' => '仅支持 .zip / .7z / .tar.gz / .tgz'], 400);
        }
        // 上传落盘位置：store/uploads/_staged/<user>/，绝不与产物目录混放
        $user = preg_replace('/[^A-Za-z0-9_.\\-]/', '_', (string) $u['username']) ?? 'u';
        $dir = Config::path('store', 'uploads', '_staged', $user);
        if (!is_dir($dir) && !@mkdir($dir, 0755, true) && !is_dir($dir)) {
            json_out(['status' => 'error', 'detail' => '无法创建上传目录'], 500);
        }
        $dst = $dir . DIRECTORY_SEPARATOR . $name;
        if (!is_uploaded_file((string) $f['tmp_name'])
            || !@move_uploaded_file((string) $f['tmp_name'], $dst)) {
            json_out(['status' => 'error', 'detail' => '保存上传文件失败'], 500);
        }
        json_out(['status' => 'ok', 'files_path' => '_staged/' . $user . '/' . $name,
                  'size' => (int) $f['size']]);
    }

    // ---------------------------------------------------------------- 下载

    public function downloads(array $p): string
    {
        $u = Auth::currentUser();
        if (!$u) {
            json_out(['status' => 'error', 'detail' => '请先登录'], 401);
        }
        // 只返回**当前用户自己的**令牌（SQL 按 username 过滤），并给出可直接
        // 点击的 url —— 与 Python 版 /api/v1/downloads 的响应结构保持一致
        // （downloads 数组，每行含 url / remaining_hours / size）。
        // 旧实现直接回传原始行：没有 url、没有 remaining_hours / size，
        // 前端按 d.token 拼链接、按 remaining_hours 渲染时只能得到
        // "/dl/t/undefined" 与「undefined 小时」。
        $now = microtime(true);
        $out = [];
        foreach (Download::listFor((string) $u['username'], '', true) as $t) {
            $exp  = (float) ($t['expires'] ?? 0);
            $rem  = max(0, (int) $exp - (int) $now);
            $path = Download::resolvePath((string) ($t['request_hash'] ?? ''),
                                          (string) ($t['filename'] ?? ''));
            $out[] = [
                'request_hash'    => (string) ($t['request_hash'] ?? ''),
                'username'        => (string) ($t['username'] ?? ''),
                'filename'        => (string) ($t['filename'] ?? ''),
                'created'         => (float) ($t['created'] ?? 0),
                'expires'         => $exp,
                'hits'            => (int) ($t['hits'] ?? 0),
                'max_hits'        => (int) ($t['max_hits'] ?? 0),
                'revoked'         => (int) ($t['revoked'] ?? 0),
                'expired'         => $exp <= $now,
                'remaining'       => $rem,
                'remaining_hours' => (int) (($rem + 3599) / 3600),
                'size'            => ($path !== null && is_file($path)) ? (int) filesize($path) : 0,
                'url'             => Net::url('/dl/t/' . (string) ($t['token'] ?? '')),
            ];
        }
        json_out(['status' => 'ok', 'count' => count($out), 'downloads' => $out,
                  'tokens' => $out, 'stats' => Download::stats()]);
    }

    /** 静态镜像中转：只允许跳到固定的官方镜像域名，杜绝开放重定向。 */
    public function dlMirror(array $p): string
    {
        $name = Util::safeFilename((string) ($_GET['name'] ?? ''));
        if ($name === '') {
            json_out(['status' => 'error', 'detail' => '文件名非法'], 400);
        }
        // 只允许跳转到内置白名单域名
        $host = 'https://dl.openwrt.ai/';
        header('Location: ' . $host . $name, true, 302);
        exit;
    }

    /** 令牌下载：验签 → 校验归属 → 吐文件。 */
    public function dlToken(array $p): string
    {
        $token = (string) ($p['token'] ?? '');
        [$row, $why] = Download::verify($token);
        if (!$row) {
            http_response_code(410);
            json_out(['status' => 'error', 'detail' => $why], 410);
        }
        $path = Download::resolvePath((string) $row['request_hash'], (string) $row['filename']);
        if ($path === null || !is_file($path)) {
            http_response_code(404);
            json_out(['status' => 'error', 'detail' => '文件不存在或已被清理'], 404);
        }
        // ★ 外链下载：download.external_host 配了域名、且 download.serve_local 关掉时，
        //   把下载请求交给 CDN/对象存储（本站不出流量）。
        //   与 Python 版 main.py:1805-1808 同语义：ext.rstrip('/') + /{hash}/{filename}。
        //   注意**先计次再跳转** —— 否则外链模式下下载次数统计会漏。
        $ext = trim((string) Settings::get('download.external_host', ''));
        if ($ext !== '' && !Settings::bool('download.serve_local', true)) {
            redirect(rtrim($ext, '/') . '/' . rawurlencode((string) $row['request_hash'])
                . '/' . rawurlencode((string) $row['filename']), 302);
        }
        $this->stream($path, (string) $row['filename']);
        return '';
    }

    /** 产物目录索引（属主或管理员）。 */
    public function storeIndex(array $p): string
    {
        $hash = (string) ($p['hash_'] ?? '');
        $dir = $this->authorizeStore($hash);
        $files = [];
        foreach (scandir($dir) ?: [] as $f) {
            // 隐藏文件与半成品一律不列出也不可下载
            if ($f === '.' || $f === '..' || str_starts_with($f, '.')) {
                continue;
            }
            $full = $dir . DIRECTORY_SEPARATOR . $f;
            if (!is_file($full)) {
                continue;
            }
            if (str_ends_with($f, '.fp') || str_ends_with($f, '.partial')) {
                continue;
            }
            $files[] = ['name' => $f, 'size' => filesize($full), 'mtime' => filemtime($full)];
        }
        usort($files, static fn($a, $b) => strcmp($a['name'], $b['name']));
        $u = Auth::currentUser();
        $links = [];
        foreach (Download::listFor((string) $u['username'], $hash, true) as $t) {
            $links[(string) $t['filename']] = Net::url('/dl/t/' . $t['token']);
        }
        return View::page('store', [
            'pageTitle' => '构建产物 ' . $hash,
            'hash' => $hash,
            'files' => $files,
            'links' => $links,
        ]);
    }

    public function storeFile(array $p): string
    {
        $hash = (string) ($p['hash_'] ?? '');
        $name = (string) ($p['name'] ?? '');
        $dir = $this->authorizeStore($hash);
        $fn = Util::safeFilename($name);
        if ($fn === '' || str_starts_with($fn, '.') || str_ends_with($fn, '.fp')) {
            http_response_code(404);
            json_out(['status' => 'error', 'detail' => '文件不存在'], 404);
        }
        $path = Util::under($dir, $dir . DIRECTORY_SEPARATOR . $fn);
        if ($path === null || !is_file($path)) {
            http_response_code(404);
            json_out(['status' => 'error', 'detail' => '文件不存在'], 404);
        }
        $this->stream($path, $fn);
        return '';
    }

    /** 归属校验：只有构建属主或管理员能读产物目录。 */
    private function authorizeStore(string $hash): string
    {
        if (!preg_match(Util::HASH_RE, $hash)) {
            $this->deny('请求标识非法');
        }
        $u = Auth::currentUser();
        $b = Db::one('SELECT username FROM builds WHERE request_hash=?', [$hash]);
        if (!$u || (!$b) || ((string) $b['username'] !== (string) $u['username'] && !Auth::isAdmin($u))) {
            $this->deny('无权访问该构建产物');
        }
        $dir = Builder::storeDir($hash);
        $real = realpath($dir);
        if ($real === false || !is_dir($real)) {
            $this->deny('产物不存在或已被清理', 404);
        }
        return $real;
    }

    private function deny(string $msg, int $code = 403): never
    {
        http_response_code($code);
        json_out(['status' => 'error', 'detail' => $msg], $code);
    }

    /** 带 Range 支持的流式输出。 */
    private function stream(string $path, string $name): void
    {
        $size = filesize($path);
        header('Content-Type: application/octet-stream');
        header('Content-Disposition: attachment; filename="' . rawurlencode($name) . '"');
        header('Accept-Ranges: bytes');
        header('X-Content-Type-Options: nosniff');
        $start = 0;
        $end = $size - 1;
        if (!empty($_SERVER['HTTP_RANGE'])
            && preg_match('/bytes=(\\d*)-(\\d*)/', (string) $_SERVER['HTTP_RANGE'], $m)) {
            if ($m[1] !== '') {
                $start = (int) $m[1];
            }
            if ($m[2] !== '') {
                $end = (int) $m[2];
            }
            if ($start > $end || $end >= $size) {
                http_response_code(416);
                header('Content-Range: bytes */' . $size);
                return;
            }
            http_response_code(206);
            header("Content-Range: bytes {$start}-{$end}/{$size}");
        }
        header('Content-Length: ' . ($end - $start + 1));
        $fp = fopen($path, 'rb');
        if (!$fp) {
            return;
        }
        fseek($fp, $start);
        $left = $end - $start + 1;
        while ($left > 0 && !feof($fp)) {
            $chunk = fread($fp, (int) min(262144, $left));
            if ($chunk === false || $chunk === '') {
                break;
            }
            echo $chunk;
            $left -= strlen($chunk);
        }
        fclose($fp);
    }

    /** /dl/{path} —— 官方镜像中转白名单跳转。 */
    public function dlPath(array $p): string
    {
        $rel = (string) ($p['path'] ?? '');
        $rel = ltrim($rel, '/');
        if (!preg_match('#^[A-Za-z0-9._/+\\-]{1,200}$#', $rel) || str_contains($rel, '..')) {
            http_response_code(404);
            json_out(['status' => 'error', 'detail' => '路径非法'], 404);
        }
        header('Location: https://dl.openwrt.ai/' . $rel, true, 302);
        exit;
    }

    // ---------------------------------------------------------------- 离线数据

    /** /json/v1/{path} —— 离线元数据。严格限定在 data/json/v1 之内。 */
    public function jsonData(array $p): string
    {
        $rel = (string) ($p['path'] ?? '');
        $base = Config::path('data', 'json', 'v1');
        $full = Util::under($base, $base . DIRECTORY_SEPARATOR . $rel);
        if ($full === null || !is_file($full)) {
            http_response_code(404);
            json_out(['status' => 'error', 'detail' => '数据不存在'], 404);
        }
        // 只允许 .json，杜绝把配置/数据库文件读出去
        if (!str_ends_with(strtolower($full), '.json')) {
            http_response_code(404);
            json_out(['status' => 'error', 'detail' => '数据不存在'], 404);
        }
        header('Content-Type: application/json; charset=utf-8');
        header('Content-Length: ' . filesize($full));
        readfile($full);
        return '';
    }

    public function lang(array $p): string
    {
        $name = (string) ($p['name'] ?? '');
        if (!preg_match('/^[a-z]{2}(-[a-z]{2})?\\.json$/', $name)) {
            http_response_code(404);
            json_out(['status' => 'error', 'detail' => '语言包不存在'], 404);
        }
        $f = Config::path('data', 'langs', $name);
        if (!is_file($f)) {
            http_response_code(404);
            json_out(['status' => 'error', 'detail' => '语言包不存在'], 404);
        }
        header('Content-Type: application/json; charset=utf-8');
        readfile($f);
        return '';
    }

    // ---------------------------------------------------------------- 赞助 / 支付

    /** 自助置位赞助曾被证明可自提权，这里直接关闭（与 Python 版同决定）。 */
    public function sponsorDisabled(array $p): string
    {
        json_out(['status' => 'error', 'error_code' => 'SELF_ACTIVATE_DISABLED',
                  'detail' => '自助开通已关闭，请通过支付或联系管理员'], 403);
    }

    public function sponsorClaim(array $p): string
    {
        $u = Auth::currentUser();
        if (!$u) {
            json_out(['status' => 'error', 'detail' => '请先登录'], 401);
        }
        $tierName = trim((string) input('tier', ''));
        [$reqAmount, $aerr] = Sponsor::parseAmount(input('amount', ''));
        if ($aerr !== '') {
            json_out(['status' => 'error', 'detail' => $aerr], 400);
        }
        // ★ 金额与天数的真值一律由 Sponsor::resolve() 决定：
        //     选套餐 → 用套餐里的 amount/days，前端传什么都不看；
        //     自定义金额 → amount 取用户的申报值（要的就是这个），
        //                  **days 由服务端按单价折算**，前端无权指定。
        [$choice, $err] = Sponsor::resolve($tierName, $reqAmount);
        if ($choice === null) {
            json_out(['status' => 'error', 'detail' => $err], 400);
        }
        $tierName = (string) $choice['tier'];
        $days = (int) $choice['days'];
        $amt = (float) $choice['amount'];
        $note = mb_substr(trim((string) input('note', '')), 0, 300);
        $email = trim((string) input('email', ''));

        // ★ 自助确认分两档（与 Python 对齐）：
        //     选套餐     → 看 sponsor.auto_approve（默认开）
        //     自定义金额 → 看 sponsor.custom_amount_auto（默认**关**）
        //   套餐金额是站长定的，风险有界；自定义金额是用户填的，一旦允许自助
        //   确认就等于让用户凭空给自己发任意时长。
        $isCustom = (bool) $choice['custom'];
        $auto = $isCustom
            ? Settings::bool('sponsor.custom_amount_auto', false)
            : (Settings::bool('sponsor.auto_approve', false)
                && in_array(Settings::get('sponsor.auto_approve'), [true, '1', 'true'], true));

        if ($auto) {
            // 与 Python 的 _activate_sponsor 一致：在**未过期的剩余时长**上顺延，
            // 并且累计 sponsor_amount（Pay::grant 与 Python 同语义）。
            Pay::grant((string) $u['username'], $days, $tierName, $amt);
            $after = Db::one('SELECT sponsor_until FROM users WHERE username=?', [$u['username']]);
            $until = (float) ($after['sponsor_until'] ?? 0);
            // 与 Python 一致：邮箱为空时才补写，绝不覆盖用户已有邮箱
            if ($email !== '') {
                Db::run("UPDATE users SET email=? WHERE username=? AND (email IS NULL OR email='')",
                    [$email, $u['username']]);
            }
            Auth::audit('sponsor_claim', (string) $u['username'],
                ['tier' => $tierName, 'amount' => $amt, 'days' => $days, 'auto' => true]);
            json_out(['status' => 'ok', 'sponsor' => true, 'until' => $until,
                      'detail' => '已激活赞助权益 ' . $days . ' 天']);
        }

        Db::run('INSERT INTO sponsor_claims(username, tier, amount, note, status, created) '
            . 'VALUES(?,?,?,?,?,?)',
            [$u['username'], $tierName, $amt, $note, 'pending', microtime(true)]);
        Auth::audit('sponsor_claim', (string) $u['username'],
            ['tier' => $tierName, 'amount' => $amt, 'days' => $days, 'pending' => true]);
        json_out(['status' => 'pending', 'days' => $days, 'amount' => $amt,
                  'detail' => '已提交，管理员确认后生效（将发放 ' . $days . ' 天）']);
    }

    public function sponsorPay(array $p): string
    {
        $u = Auth::currentUser();
        if (!$u) {
            json_out(['status' => 'error', 'detail' => '请先登录'], 401);
        }
        // ★ 门禁与 Python 完全对齐：Python 是「enabled 开关」+「is_configured 三项密钥」，
        //   原先这里只查 enabled()（缺公钥校验）→ 密钥不全时 PHP 会带着不完整配置去下单，
        //   到跳转支付宝那一步才失败，报错点离原因很远。
        if (!Pay::available()) {
            json_out(['status' => 'error', 'detail' => '在线支付未开启或未配置完整'], 503);
        }
        $tierName = trim((string) input('tier', ''));
        $tiers = Settings::get('sponsor.tiers', []);
        if (is_string($tiers)) {
            $tiers = json_decode($tiers, true) ?: [];
        }
        $found = null;
        foreach ((array) $tiers as $t) {
            if ((string) ($t['name'] ?? '') === $tierName) {
                $found = $t;
                break;
            }
        }
        if ($found === null) {
            json_out(['status' => 'error', 'detail' => '套餐不存在'], 400);
        }
        $out = Pay::precreate((string) $u['username'], $tierName,
            (float) ($found['amount'] ?? 0), (int) ($found['days'] ?? 30));
        if (!$out['ok']) {
            json_out(['status' => 'error', 'detail' => $out['detail']], 502);
        }
        json_out([
            'status' => 'ok',
            'out_trade_no' => $out['out_trade_no'],
            'qr_code' => $out['qr_code'],
            'amount' => (float) ($found['amount'] ?? 0),
            'expires' => $out['expires'],
        ]);
    }

    public function sponsorPayStatus(array $p): string
    {
        $u = Auth::currentUser();
        if (!$u) {
            json_out(['status' => 'error', 'detail' => '请先登录'], 401);
        }
        $no = (string) ($p['out_trade_no'] ?? '');
        $o = Db::one('SELECT * FROM pay_orders WHERE out_trade_no=? AND username=?',
            [$no, $u['username']]);
        if (!$o) {
            json_out(['status' => 'error', 'detail' => '订单不存在'], 404);
        }
        // 未支付时才去网关查（真实查询，失败不谎报成功）
        if (in_array((string) $o['status'], ['created', 'pending'], true)) {
            Pay::syncOrder($no);
            $o = Db::one('SELECT * FROM pay_orders WHERE out_trade_no=?', [$no]);
        }
        json_out([
            'status' => (string) $o['status'],
            'out_trade_no' => $no,
            'amount' => (float) $o['amount'],
            'paid_at' => (float) ($o['paid_at'] ?? 0),
        ]);
    }

    public function sponsorPayQr(array $p): string
    {
        $u = Auth::currentUser();
        if (!$u) {
            http_response_code(401);
            exit;
        }
        $no = (string) ($p['out_trade_no'] ?? '');
        $o = Db::one('SELECT qr_code FROM pay_orders WHERE out_trade_no=? AND username=?',
            [$no, $u['username']]);
        $qr = $o['qr_code'] ?? '';
        if (!$qr) {
            http_response_code(404);
            exit;
        }
        // 二维码是一串 URL，转到支付宝域名；不自己渲染图片（避免引第三方库）
        header('Location: ' . $qr, true, 302);
        exit;
    }

    public function sponsorPayCancel(array $p): string
    {
        $u = Auth::currentUser();
        if (!$u) {
            json_out(['status' => 'error', 'detail' => '请先登录'], 401);
        }
        $no = (string) ($p['out_trade_no'] ?? '');
        $n = Db::run("UPDATE pay_orders SET status='cancelled' "
            . "WHERE out_trade_no=? AND username=? AND status IN ('created','pending')",
            [$no, $u['username']]);
        json_out(['status' => $n ? 'ok' : 'error', 'detail' => $n ? '' : '订单无法取消']);
    }

    public function sponsorOrders(array $p): string
    {
        $u = Auth::currentUser();
        if (!$u) {
            json_out(['status' => 'error', 'detail' => '请先登录'], 401);
        }
        json_out(['status' => 'ok', 'orders' =>
            Db::all('SELECT * FROM pay_orders WHERE username=? ORDER BY created DESC LIMIT 100',
                [$u['username']])]);
    }

    public function sponsorRefunds(array $p): string
    {
        $u = Auth::currentUser();
        if (!$u) {
            json_out(['status' => 'error', 'detail' => '请先登录'], 401);
        }
        json_out(['status' => 'ok', 'refunds' =>
            Db::all('SELECT * FROM refund_requests WHERE username=? ORDER BY created DESC LIMIT 100',
                [$u['username']])]);
    }

    public function sponsorRefund(array $p): string
    {
        $u = Auth::currentUser();
        if (!$u) {
            json_out(['status' => 'error', 'detail' => '请先登录'], 401);
        }
        if (!Settings::bool('pay.refund_enabled', true)) {
            json_out(['status' => 'error', 'detail' => '退款申请已关闭'], 403);
        }
        $no = trim((string) input('out_trade_no', ''));
        $reason = trim((string) input('reason', ''));
        $detail = mb_substr(trim((string) input('detail', '')), 0, 1000);
        if (mb_strlen($reason) < 5) {
            json_out(['status' => 'error', 'detail' => '退款原因至少 5 个字'], 400);
        }
        if (mb_strlen($reason) > 500) {
            json_out(['status' => 'error', 'detail' => '退款原因不能超过 500 个字'], 400);
        }
        $o = Db::one('SELECT * FROM pay_orders WHERE out_trade_no=? AND username=?',
            [$no, $u['username']]);
        if (!$o) {
            json_out(['status' => 'error', 'detail' => '订单不存在'], 404);
        }
        // 与 Python 的 refund.py:107 对齐：已收款即可退 —— 包含 paid_pending
        // （网关已收款但权益未发放）与 paid。只认 'paid' 会让关了自动发放的
        // 站点无法给这类订单退款。
        if (!in_array((string) $o['status'], Pay::PAID_STATES, true)) {
            json_out(['status' => 'error', 'detail' => '只有已收款的订单可以申请退款'], 400);
        }
        $dup = Db::one("SELECT id FROM refund_requests WHERE out_trade_no=? AND status IN ('pending','processing','approved')",
            [$no]);
        if ($dup) {
            json_out(['status' => 'error', 'detail' => '该订单已有进行中的退款申请'], 409);
        }
        Db::run('INSERT INTO refund_requests(out_trade_no, username, amount, reason, detail, '
            . 'status, created) VALUES(?,?,?,?,?,?,?)',
            [$no, $u['username'], (float) $o['amount'], $reason, $detail, 'pending', microtime(true)]);
        Auth::audit('refund_request', $no, ['reason' => $reason]);
        json_out(['status' => 'ok']);
    }

    /** 支付宝异步通知：CSRF 豁免，但**必须验签**；验签不过一律拒绝。 */
    public function alipayNotify(array $p): string
    {
        $data = array_merge($_GET, $_POST);
        $out = Pay::handleNotify($data);
        http_response_code($out['code']);
        header('Content-Type: text/plain; charset=utf-8');
        return $out['body'];
    }
}
