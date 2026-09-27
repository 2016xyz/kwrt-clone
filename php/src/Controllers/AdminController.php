<?php
/**
 * 管理后台控制器（15 个模块）。
 *
 * 与 Python 版模块一一对应：
 *   总览 / 用户 / 构建 / 队列存储 / 站点设置 / 邮件 / 邮箱验证 / 下载链接 /
 *   赞助 / 在线支付 / 退款审核 / 插件目录 / 插件提议 / 封禁 / 审计日志
 *
 * 后台页面同样服务端渲染（PHP 模板），写操作走 JSON 接口。
 * 所有写操作：先鉴权 → 校验入参 → 落库 → 写审计日志。
 */
declare(strict_types=1);

namespace Kwrt\Controllers;

use Kwrt\Artifacts;
use Kwrt\Auth;
use Kwrt\Builder;
use Kwrt\Catalog;
use Kwrt\Config;
use Kwrt\Db;
use Kwrt\Download;
use Kwrt\Update;
use Kwrt\Mailer;
use Kwrt\Net;
use Kwrt\Pay;
use Kwrt\Releases;
use Kwrt\Settings;
use Kwrt\SettingsSchema;
use Kwrt\Util;
use Kwrt\View;

final class AdminController
{
    /** 后台首页（服务端渲染，15 个模块一次给全）。 */
    public function page(array $p): string
    {
        $u = Auth::currentUser();
        if (!$u) {
            redirect('/login/?next=' . urlencode('/admin/'));
        }
        if (!Auth::isAdmin($u)) {
            http_response_code(403);
            return View::page('error403', ['path' => '/admin/']);
        }
        $tab = (string) ($_GET['tab'] ?? 'ov');
        $tabs = [
            'ov' => '总览', 'users' => '用户', 'builds' => '构建', 'queue' => '队列与存储',
            'site' => '站点设置', 'mail' => '邮件通知', 'verify' => '邮箱验证',
            'tokens' => '下载链接', 'sponsor' => '赞助', 'pay' => '在线支付',
            'refund' => '退款审核', 'catalog' => '插件目录', 'prop' => '插件提议',
            'bans' => '封禁', 'logs' => '审计日志',
        ];
        if (!isset($tabs[$tab])) {
            $tab = 'ov';
        }
        return View::page('admin', [
            'pageTitle' => '管理控制台',
            'tab' => $tab,
            'tabs' => $tabs,
            'overview' => $this->overviewData(),
            'groups' => Settings::groupsForUi(),
            'users' => $this->userRows(),
            'builds' => $this->buildRows(),
            'artifacts' => Artifacts::scan(),
            'logs' => Db::all('SELECT * FROM admin_logs ORDER BY created DESC LIMIT 200'),
            'bans' => Db::all('SELECT * FROM bans ORDER BY created DESC LIMIT 200'),
            'tokens' => Download::listFor(null, '', false, 200),
            'proposals' => Db::all('SELECT * FROM proposals ORDER BY created DESC LIMIT 200'),
            'refunds' => Db::all('SELECT * FROM refund_requests ORDER BY created DESC LIMIT 200'),
            'orders' => Db::all('SELECT * FROM pay_orders ORDER BY created DESC LIMIT 200'),
            'claims' => Db::all('SELECT * FROM sponsor_claims ORDER BY created DESC LIMIT 200'),
            'mailLog' => Db::all('SELECT * FROM email_send_log ORDER BY id DESC LIMIT 100'),
            'catalog' => Catalog::load(),
            'queue' => Builder::queueStats(),
            'dlStats' => Download::stats(),
            'payEnabled' => Pay::enabled(),
            'notifyUrl' => Pay::notifyUrl(),
        ], 'admin_layout');
    }

    // ---------------------------------------------------------------- 总览

    /**
     * GET /api/v1/admin/overview —— 控制台总览（API）。
     *
     * ★ 这个方法此前**根本不存在**：routes.php 指向 AdminController::overview，
     *   而类里只有私有的 overviewData()（给服务端渲染的 page() 用）。
     *   于是这个接口固定返回 500：
     *       Uncaught RuntimeException: 控制器方法不存在: ...AdminController::overview
     *   是 tools/verify_integrity.py 的「路由→方法」检查发现的。
     *
     *   返回结构刻意与 Python 版 app/main.py 的 admin_overview() **同构** ——
     *   两版共享同一套前端契约，形状不一致就等于接口不一致。
     */
    public function overview(array $p): string
    {
        $this->needAdmin();
        $q = Builder::queueStats();

        $users = [
            'total'    => (int) Db::val('SELECT COUNT(*) FROM users', [], 0),
            'sponsor'  => (int) Db::val('SELECT COUNT(*) FROM users WHERE sponsor=1', [], 0),
            'admin'    => (int) Db::val("SELECT COUNT(*) FROM users WHERE role='admin'", [], 0),
            'disabled' => (int) Db::val('SELECT COUNT(*) FROM users WHERE disabled=1', [], 0),
        ];
        $builds = [
            'total'  => (int) Db::val('SELECT COUNT(*) FROM builds', [], 0),
            'done'   => (int) Db::val("SELECT COUNT(*) FROM builds WHERE status='done'", [], 0),
            'failed' => (int) Db::val("SELECT COUNT(*) FROM builds WHERE status='failed'", [], 0),
        ];
        $jobsTotal = (int) Db::val('SELECT COUNT(*) FROM jobs', [], 0);

        // 敏感项必须掩码 —— 总览会把整份配置交给控制台各页
        $conf = Settings::publicValues();
        $conf['builder_enabled'] = $q['enabled'];
        $conf['max_concurrent'] = $q['max'];
        $conf['registration_open'] = Settings::bool('registration_open', true);
        $conf['mail_enabled'] = Mailer::enabled();

        json_out([
            'users'  => $users,
            'builds' => $builds,
            'queue'  => [
                'running'     => $q['running'],
                'queued'      => $q['queued'],
                'concurrency' => $q['max'],
                'total'       => $jobsTotal,
            ],
            'proposals_pending' => (int) Db::val(
                "SELECT COUNT(*) FROM proposals WHERE COALESCE(status,'pending')='pending'", [], 0),
            'bans'    => (int) Db::val('SELECT COUNT(*) FROM bans', [], 0),
            'disk'    => [
                'free_mb'  => (int) round($q['disk_free'] / 1048576),
                'store_mb' => (int) ($q['store_bytes'] / 1048576),
            ],
            'download' => Download::stats(),
            'backend'  => [
                'backend'      => $q['backend'],
                'enabled'      => $q['enabled'],
                'concurrency'  => $q['max'],
            ],
            'config'   => $conf,
            'versions' => Releases::branches(),
            // 更新检查的静态信息（仓库/开关）。**不在这里发起网络请求** ——
            // 总览是后台打开就会调的接口，每次打一次 GitHub 会把速率打满。
            // 真正的检查只在管理员点「检查更新」时发生（走 updateCheck）。
            'update' => [
                'enabled'      => Update::enabled(),
                'repo'         => Update::repo(),
                'configured'   => Update::repo() !== '',
                'cache_minutes' => Update::cacheMinutes(),
            ],
        ], 200);
    }

    /**
     * 检查本程序是否有新版本（管理员）。
     *
     * ★ 只读接口：只查询更新源并返回结果，**不落地任何文件、不重启服务**。
     *   真正的升级动作由管理员按返回的指引手工执行
     *   （见 docs/更新升级与完整性检查.md）—— 让一个网页按钮去覆盖正在运行的
     *   程序文件并重启自身，出问题时没人收得了场。
     *
     * ★ force=1 跳过缓存。缓存是给「反复点击」与「多人同时打开后台」准备的，
     *   否则很容易把 GitHub 未认证的 60 次/小时限额打满，连固件构建派发一起挂掉。
     */
    public function updateCheck(array $p): string
    {
        $this->needAdmin();
        $force = !empty($p['force']) || !empty($_GET['force']);
        json_out(Update::check($force));
    }

    private function overviewData(): array
    {
        $now = microtime(true);
        $q = Builder::queueStats();
        return [
            'users' => (int) Db::val('SELECT COUNT(*) FROM users', [], 0),
            'admins' => (int) Db::val("SELECT COUNT(*) FROM users WHERE role='admin'", [], 0),
            'sponsors' => (int) Db::val('SELECT COUNT(*) FROM users WHERE sponsor=1', [], 0),
            'builds' => (int) Db::val('SELECT COUNT(*) FROM builds', [], 0),
            'builds_today' => (int) Db::val('SELECT COUNT(*) FROM builds WHERE created>?',
                [strtotime('today 00:00')], 0),
            'jobs_running' => $q['running'],
            'jobs_queued' => $q['queued'],
            'concurrency' => $q['max'],
            'backend' => $q['backend'],
            'builder_enabled' => $q['enabled'],
            'disk_free' => $q['disk_free'],
            'store_bytes' => $q['store_bytes'],
            'orders' => (int) Db::val('SELECT COUNT(*) FROM pay_orders', [], 0),
            'paid' => (int) Db::val("SELECT COUNT(*) FROM pay_orders WHERE status='paid'", [], 0),
            'paid_amount' => (float) Db::val("SELECT COALESCE(SUM(amount),0) FROM pay_orders WHERE status='paid'", [], 0),
            'refunds_pending' => (int) Db::val("SELECT COUNT(*) FROM refund_requests WHERE status='pending'", [], 0),
            'tokens_active' => Download::stats()['active'],
            'proposals_pending' => (int) Db::val("SELECT COUNT(*) FROM proposals WHERE status='pending'", [], 0),
            'bans' => (int) Db::val('SELECT COUNT(*) FROM bans', [], 0),
            'mail' => Mailer::enabled(),
        ];
    }

    // ---------------------------------------------------------------- 数据装配

    private function userRows(): array
    {
        $rows = Db::all('SELECT username, email, role, sponsor, sponsor_until, sponsor_tier, '
            . 'disabled, quota, email_verified, created, last_login FROM users ORDER BY created DESC LIMIT 300');
        foreach ($rows as &$r) {
            $r['builds'] = (int) Db::val('SELECT COUNT(*) FROM builds WHERE username=?',
                [$r['username']], 0);
            $r['is_admin'] = (string) $r['role'] === 'admin';
            $r['sponsor_active'] = (float) ($r['sponsor_until'] ?? 0) > microtime(true)
                || (int) $r['sponsor'] === 1;
        }
        return $rows;
    }

    private function buildRows(): array
    {
        $rows = Db::all('SELECT * FROM builds ORDER BY created DESC LIMIT 200');
        foreach ($rows as &$r) {
            $j = Builder::job((string) $r['request_hash']);
            $r['live_status'] = $j['status'] ?? (string) $r['status'];
            $r['detail'] = $j['detail'] ?? '';
            $st = Artifacts::scan();
            $r['has_artifacts'] = isset($st['by_hash'][$r['request_hash']]);
            $r['size'] = $st['by_hash'][$r['request_hash']]['bytes'] ?? 0;
        }
        return $rows;
    }

    // ---------------------------------------------------------------- 鉴权包装

    private function needAdmin(): array
    {
        $u = Auth::currentUser();
        if (!$u || !Auth::isAdmin($u)) {
            json_out(['status' => 'error', 'detail' => '需要管理员权限'], 403);
        }
        return $u;
    }

    // ---------------------------------------------------------------- 用户

    public function users(array $p): string
    {
        $this->needAdmin();
        json_out(['status' => 'ok', 'users' => $this->userRows()]);
    }

    public function userOp(array $p): string
    {
        $admin = $this->needAdmin();
        $action = (string) input('action', '');
        $username = trim((string) input('username', ''));
        if ($username === '') {
            json_out(['status' => 'error', 'detail' => '缺少用户名'], 400);
        }
        $target = Db::one('SELECT * FROM users WHERE username=?', [$username]);
        if (!$target) {
            json_out(['status' => 'error', 'detail' => '用户不存在'], 404);
        }
        // 不允许管理员把自己降级/停用/删除 —— 否则一步自锁
        $isSelf = (string) $target['username'] === (string) $admin['username'];

        switch ($action) {
            case 'set_role':
                $role = (string) input('role', 'user');
                if (!in_array($role, ['user', 'admin'], true)) {
                    json_out(['status' => 'error', 'detail' => '角色取值非法'], 400);
                }
                if ($isSelf && $role !== 'admin') {
                    json_out(['status' => 'error', 'detail' => '不能取消自己的管理员权限'], 400);
                }
                Db::run('UPDATE users SET role=? WHERE username=?', [$role, $username]);
                break;
            case 'set_sponsor':
                $on = (bool) input('value', false);
                Db::run('UPDATE users SET sponsor=? WHERE username=?', [$on ? 1 : 0, $username]);
                break;
            case 'set_quota':
                $q = (int) input('quota', 12);
                if ($q < 0 || $q > 100000) {
                    json_out(['status' => 'error', 'detail' => '配额取值非法'], 400);
                }
                Db::run('UPDATE users SET quota=? WHERE username=?', [$q, $username]);
                break;
            case 'disable':
            case 'enable':
                if ($isSelf && $action === 'disable') {
                    json_out(['status' => 'error', 'detail' => '不能停用自己'], 400);
                }
                Db::run('UPDATE users SET disabled=? WHERE username=?',
                    [$action === 'disable' ? 1 : 0, $username]);
                if ($action === 'disable') {
                    Download::revokeUser($username);
                }
                break;
            case 'kick':
                $n = Db::run('DELETE FROM sessions WHERE username=?', [$username]);
                Auth::audit('user_kick', $username, ['sessions' => $n]);
                json_out(['status' => 'ok', 'kicked' => $n]);
            case 'reset_password':
                $pw = (string) input('password', '');
                if (mb_strlen($pw) < 8) {
                    json_out(['status' => 'error', 'detail' => '密码至少 8 位'], 400);
                }
                Db::run('UPDATE users SET password=? WHERE username=?',
                    [Auth::hashPw($pw), $username]);
                Db::run('DELETE FROM sessions WHERE username=?', [$username]);
                break;
            case 'delete':
                if ($isSelf) {
                    json_out(['status' => 'error', 'detail' => '不能删除自己'], 400);
                }
                Db::run('DELETE FROM users WHERE username=?', [$username]);
                Db::run('DELETE FROM sessions WHERE username=?', [$username]);
                Download::revokeUser($username);
                break;
            default:
                json_out(['status' => 'error', 'detail' => '未知操作'], 400);
        }
        Auth::audit('user_' . $action, $username);
        json_out(['status' => 'ok']);
    }

    public function userCreate(array $p): string
    {
        $this->needAdmin();
        $username = trim((string) input('username', ''));
        $password = (string) input('password', '');
        $email = trim((string) input('email', ''));
        $role = (string) input('role', 'user');
        if (!preg_match('/^[A-Za-z0-9_\-]{3,32}$/', $username)) {
            json_out(['status' => 'error', 'detail' => '用户名格式不正确'], 400);
        }
        if (mb_strlen($password) < 8) {
            json_out(['status' => 'error', 'detail' => '密码至少 8 位'], 400);
        }
        if (Db::one('SELECT username FROM users WHERE username=?', [$username])) {
            json_out(['status' => 'error', 'detail' => '用户名已存在'], 409);
        }
        Db::run('INSERT INTO users(username, password, email, sponsor, created, role, disabled, '
            . 'quota, email_verified) VALUES(?,?,?,0,?,?,0,?,1)',
            [$username, Auth::hashPw($password), $email, microtime(true),
             in_array($role, ['admin', 'user'], true) ? $role : 'user',
             (int) Settings::get('default_quota', 12)]);
        Auth::audit('user_create', $username, ['role' => $role]);
        json_out(['status' => 'ok']);
    }

    public function userSponsor(array $p): string
    {
        $this->needAdmin();
        $username = trim((string) input('username', ''));
        $days = (int) input('days', 30);
        $tier = trim((string) input('tier', '管理员发放'));
        if ($days <= 0 || $days > 3650) {
            json_out(['status' => 'error', 'detail' => '天数取值非法'], 400);
        }
        Pay::grant($username, $days, $tier, 0.0);
        Auth::audit('user_sponsor', $username, ['days' => $days]);
        json_out(['status' => 'ok']);
    }

    public function userVerify(array $p): string
    {
        $this->needAdmin();
        $username = trim((string) input('username', ''));
        Db::run('UPDATE users SET email_verified=1 WHERE username=?', [$username]);
        Auth::audit('user_verify', $username);
        json_out(['status' => 'ok']);
    }

    public function userReverify(array $p): string
    {
        $this->needAdmin();
        $username = trim((string) input('username', ''));
        Db::run('UPDATE users SET email_verified=0 WHERE username=?', [$username]);
        Auth::audit('user_reverify', $username);
        json_out(['status' => 'ok']);
    }

    // ---------------------------------------------------------------- 构建 / 队列 / 产物

    public function builds(array $p): string
    {
        $this->needAdmin();
        json_out(['status' => 'ok', 'builds' => $this->buildRows()]);
    }

    public function buildOp(array $p): string
    {
        $this->needAdmin();
        $action = (string) input('action', '');
        $hash = (string) input('request_hash', '');
        if (!preg_match(Util::HASH_RE, $hash)) {
            json_out(['status' => 'error', 'detail' => '请求标识非法'], 400);
        }
        switch ($action) {
            case 'cancel':
                Db::run("UPDATE jobs SET status='cancelled', detail='管理员取消', updated=? "
                    . "WHERE request_hash=? AND status IN ('queued','running')", [microtime(true), $hash]);
                Db::run("UPDATE builds SET status='cancelled' WHERE request_hash=?", [$hash]);
                break;
            case 'retry':
                $b = Db::one('SELECT payload FROM builds WHERE request_hash=?', [$hash]);
                if (!$b) {
                    json_out(['status' => 'error', 'detail' => '构建不存在'], 404);
                }
                $pl = json_decode((string) $b['payload'], true) ?: [];
                Builder::enqueue($hash, $pl);
                Builder::pump();
                break;
            case 'delete':
                Artifacts::delete($hash, true);
                Db::run('DELETE FROM builds WHERE request_hash=?', [$hash]);
                Db::run('DELETE FROM jobs WHERE request_hash=?', [$hash]);
                Download::revokeBuild($hash);
                break;
            case 'resend':
                $b = Db::one('SELECT * FROM builds WHERE request_hash=?', [$hash]);
                if (!$b) {
                    json_out(['status' => 'error', 'detail' => '构建不存在'], 404);
                }
                Download::revokeBuild($hash);
                $this->issueLinks($hash, (string) $b['username']);
                break;
            default:
                json_out(['status' => 'error', 'detail' => '未知操作'], 400);
        }
        Auth::audit('build_' . $action, $hash);
        json_out(['status' => 'ok']);
    }

    /** 为某次构建的全部产物签发下载令牌。 */
    private function issueLinks(string $hash, string $username): int
    {
        $dir = Builder::storeDir($hash);
        if (!is_dir($dir)) {
            return 0;
        }
        $n = 0;
        foreach (scandir($dir) ?: [] as $f) {
            if ($f === '.' || $f === '..' || str_starts_with($f, '.')) {
                continue;
            }
            if (Util::safeFilename($f) === '') {
                continue;
            }
            Download::issue($hash, $username, $f);
            $n++;
        }
        return $n;
    }

    public function artifacts(array $p): string
    {
        $this->needAdmin();
        json_out(['status' => 'ok'] + Artifacts::scan());
    }

    public function artifactOp(array $p): string
    {
        $this->needAdmin();
        $action = (string) input('action', '');
        switch ($action) {
            case 'delete':
                $hash = (string) input('request_hash', '');
                $r = Artifacts::delete($hash, false);
                break;
            case 'delete_record':
                $hash = (string) input('request_hash', '');
                $r = Artifacts::delete($hash, true);
                Db::run('DELETE FROM builds WHERE request_hash=?', [$hash]);
                Db::run('DELETE FROM jobs WHERE request_hash=?', [$hash]);
                break;
            case 'delete_orphans':
                $r = Artifacts::deleteOrphans();
                break;
            default:
                json_out(['status' => 'error', 'detail' => '未知操作'], 400);
        }
        Auth::audit('artifact_' . $action, (string) input('request_hash', ''));
        json_out(['status' => 'ok'] + $r);
    }

    public function queueOp(array $p): string
    {
        $this->needAdmin();
        $action = (string) input('action', '');
        switch ($action) {
            case 'clear_finished':
                $n = Db::run("DELETE FROM jobs WHERE status IN ('done','failed','cancelled')");
                Auth::audit('queue_clear', '', ['removed' => $n]);
                json_out(['status' => 'ok', 'removed' => $n]);
            case 'purge_store':
                $r = Artifacts::purgeAll(true);
                Download::revokeAll();
                Auth::audit('queue_purge_store', '', $r);
                json_out(['status' => 'ok'] + $r);
            case 'toggle_builder':
                $on = (bool) input('value', true);
                Settings::set('builder.enabled', $on);
                Auth::audit('queue_toggle_builder', '', ['value' => $on]);
                json_out(['status' => 'ok', 'enabled' => $on]);
            case 'set_concurrency':
                $n = (int) input('value', 1);
                if ($n < 1 || $n > 16) {
                    json_out(['status' => 'error', 'detail' => '并发需在 1–16'], 400);
                }
                Settings::set('builder.max_concurrent', $n);
                Auth::audit('queue_set_concurrency', '', ['value' => $n]);
                json_out(['status' => 'ok', 'concurrency' => $n]);
            case 'set_backend':
                $b = (string) input('value', 'local');
                if (!in_array($b, ['local', 'github'], true)) {
                    json_out(['status' => 'error', 'detail' => '后端取值非法'], 400);
                }
                Settings::set('builder.backend', $b);
                Auth::audit('queue_set_backend', '', ['value' => $b]);
                json_out(['status' => 'ok', 'backend' => $b]);
            case 'pump':
                $r = Builder::pump();
                json_out(['status' => 'ok', 'scheduled' => $r[0], 'detail' => $r[1]]);
            default:
                json_out(['status' => 'error', 'detail' => '未知操作'], 400);
        }
    }

    public function buildBackend(array $p): string
    {
        $this->needAdmin();
        $cur = (string) Settings::get('builder.backend', 'local');
        json_out(['status' => 'ok', 'configured' => $cur, 'effective' => $cur,
                  'github_available' => Settings::bool('gh.enabled', false)
                      && \Kwrt\Queues::available() !== [],
                  'gh_queues' => \Kwrt\Queues::summary(),
                  'gh_queue_count' => count(\Kwrt\Queues::available())]);
    }

    // ---------------------------------------------------------------- 站点设置

    public function settings(array $p): string
    {
        $this->needAdmin();
        json_out(['status' => 'ok', 'groups' => Settings::groupsForUi(),
                  'domain' => [
                      'bound' => Net::boundDomain(),
                      'base_url' => Net::baseUrl(),
                      'cdn_enabled' => Net::cdnEnabled(),
                      'cdn_base' => Net::cdnBase(),
                      'trusted_hosts' => Net::trustedHosts(),
                      'trusted_proxies' => Net::trustedProxies(),
                      'client_ip' => Net::clientIp(),
                      'peer_ip' => Net::peerIp(),
                  ]]);
    }

    /**
     * 保存设置。
     * 兼容两种提交：Form 的 key/value，或 JSON 的多键对象。
     * 页面开关的**自锁保护**：不允许把「能进入后台的入口」全关掉。
     */
    public function settingsPost(array $p): string
    {
        $admin = $this->needAdmin();
        $key = (string) input('key', '');
        if ($key !== '') {
            if (!Settings::has($key)) {
                json_out(['status' => 'error', 'detail' => '未知配置项: ' . $key], 400);
            }
            $this->guardSwitch($key, input('value', ''));
            [$ok, $why] = Settings::set($key, input('value', ''));
            if (!$ok) {
                json_out(['status' => 'error', 'detail' => $why], 400);
            }
            // 审计日志屏蔽密钥类配置的值
            Auth::audit('setting_set', $key, Settings::isSecret($key) ? '[已设置]' : (string) input('value', ''));
            json_out(['status' => 'ok', 'key' => $key, 'value' => Settings::isSecret($key) ? '' : Settings::get($key)]);
        }

        $kv = json_body();
        if (!$kv) {
            $kv = $_POST;
            unset($kv['_csrf']);
        }
        if (!is_array($kv) || !$kv) {
            json_out(['status' => 'error', 'detail' => '没有要保存的配置'], 400);
        }
        foreach ($kv as $k => $v) {
            $this->guardSwitch((string) $k, $v);
        }
        $r = Settings::setMany($kv);
        if (($r['status'] ?? '') !== 'ok') {
            json_out($r, 400);
        }
        Auth::audit('setting_set_bulk', '', ['count' => count($kv), 'keys' => array_keys($kv)]);
        json_out(['status' => 'ok', 'saved' => count($kv)]);
    }

    /**
     * 页面开关的自锁保护。
     *
     * 场景：管理员把「登录」关掉，然后退出 → 再也登不回来。
     * 这里至少保证「后台可达」：不允许在关闭登录的同时…… 更根本的做法是
     * 明确提示风险并要求显式确认参数 confirm_lockout=1。
     */
    private function guardSwitch(string $key, mixed $value): void
    {
        if (!str_starts_with($key, 'page.') || !str_ends_with($key, '_enabled')) {
            return;
        }
        $off = in_array($value, [false, 0, '0', 'false', 'off', ''], true);
        if (!$off) {
            return;
        }
        if ($key === 'page.login_enabled' && (string) input('confirm_lockout', '') !== '1') {
            json_out([
                'status' => 'error',
                'error_code' => 'LOCKOUT_RISK',
                'detail' => '关闭登录会让你在退出后无法再进入后台。'
                    . '确认要关闭请带上 confirm_lockout=1 重新提交。',
            ], 409);
        }
        if ($key === 'page.index_enabled') {
            json_out([
                'status' => 'error',
                'error_code' => 'INDEX_LOCKED',
                'detail' => '首页不允许关闭（它同时是后台的兜底入口）。',
            ], 409);
        }
    }

    public function siteGet(array $p): string
    {
        $this->needAdmin();
        json_out(['status' => 'ok', 'settings' => Settings::load()]);
    }

    public function sitePost(array $p): string
    {
        return $this->settingsPost($p);
    }

    // ---------------------------------------------------------------- 邮件 / 验证 / 令牌

    public function mailTest(array $p): string
    {
        $this->needAdmin();
        $to = trim((string) input('to', ''));
        if (!filter_var($to, FILTER_VALIDATE_EMAIL)) {
            json_out(['status' => 'error', 'detail' => '收件人邮箱不合法'], 400);
        }
        [$ok, $detail] = Mailer::sendTest($to);
        Db::run('INSERT INTO email_send_log(purpose, to_addr, username, ok, detail, created) '
            . 'VALUES(?,?,?,?,?,?)', ['test', $to, null, $ok ? 1 : 0, (string) $detail, microtime(true)]);
        Auth::audit('mail_test', $to, ['ok' => $ok]);
        json_out(['status' => $ok ? 'ok' : 'error', 'detail' => $detail], $ok ? 200 : 502);
    }

    public function mailLog(array $p): string
    {
        $this->needAdmin();
        json_out(['status' => 'ok', 'log' =>
            Db::all('SELECT * FROM email_send_log ORDER BY id DESC LIMIT 200')]);
    }

    public function githubTest(array $p): string
    {
        $this->needAdmin();
        // ★ 用**轮转选中的那个队列**来测，而不是硬编码读 gh.token/gh.repo。
        //   多队列场景下，前者才是构建真正会用到的那一个；
        //   测了 A 队列却派发到 B 队列，「测试连接」就失去了意义。
        $q = \Kwrt\Queues::pick();
        if ($q === null) {
            json_out(['status' => 'error', 'detail' => 'GitHub 未配置（gh.queues 与单队列配置均为空）'], 400);
        }
        $tok = (string) $q['token'];
        $repo = (string) $q['repo'];
        $ch = curl_init("https://api.github.com/repos/{$repo}");
        curl_setopt_array($ch, [
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_TIMEOUT => 15,
            CURLOPT_HTTPHEADER => [
                'Authorization: Bearer ' . $tok,
                'Accept: application/vnd.github+json',
                'User-Agent: Kwrt-PHP',
            ],
        ]);
        $resp = curl_exec($ch);
        $code = (int) curl_getinfo($ch, CURLINFO_HTTP_CODE);
        curl_close($ch);
        $j = json_decode((string) $resp, true);
        // 只回显仓库信息，绝不回显 token
        json_out([
            'status' => $code === 200 ? 'ok' : 'error',
            'http' => $code,
            'repo' => $j['full_name'] ?? '',
            'private' => (bool) ($j['private'] ?? false),
            'detail' => $code === 200 ? '连接正常' : (string) ($j['message'] ?? '连接失败'),
        ], $code === 200 ? 200 : 502);
    }

    public function tokens(array $p): string
    {
        $this->needAdmin();
        $status = (string) ($_GET['status'] ?? '');
        $rows = Download::listFor(null, '', false, 500);
        if ($status === 'active') {
            $rows = array_values(array_filter($rows, static fn($r) =>
                (int) $r['revoked'] === 0 && (float) $r['expires'] > microtime(true)));
        }
        json_out(['status' => 'ok', 'tokens' => $rows, 'stats' => Download::stats()]);
    }

    public function tokensOp(array $p): string
    {
        $this->needAdmin();
        $action = (string) input('action', '');
        $token = (string) input('token', '');
        switch ($action) {
            case 'revoke':
                Db::run('UPDATE dl_tokens SET revoked=1 WHERE token=?', [$token]);
                break;
            case 'revoke_build':
                Download::revokeBuild((string) input('request_hash', ''));
                break;
            case 'revoke_user':
                Download::revokeUser((string) input('username', ''));
                break;
            case 'extend':
                if (!Download::extend($token, (int) input('hours', 72))) {
                    json_out(['status' => 'error', 'detail' => '令牌不存在'], 404);
                }
                break;
            case 'cleanup':
                $n = Download::cleanupExpired(7);
                Auth::audit('token_cleanup', '', ['removed' => $n]);
                json_out(['status' => 'ok', 'removed' => $n]);
            default:
                json_out(['status' => 'error', 'detail' => '未知操作'], 400);
        }
        Auth::audit('token_' . $action, $token ?: '-');
        json_out(['status' => 'ok']);
    }

    // ---------------------------------------------------------------- 提议 / 封禁 / 日志

    public function proposals(array $p): string
    {
        $this->needAdmin();
        json_out(['status' => 'ok', 'proposals' =>
            Db::all('SELECT * FROM proposals ORDER BY created DESC LIMIT 300')]);
    }

    public function proposalOp(array $p): string
    {
        $this->needAdmin();
        $action = (string) input('action', '');
        $pid = (int) input('id', 0);
        $status = in_array($action, ['approve', 'reject', 'pending'], true) ? $action : null;
        if ($status === 'approve') { $status = 'approved'; }
        if ($status === 'reject') { $status = 'rejected'; }
        if ($status === null) {
            json_out(['status' => 'error', 'detail' => '未知操作'], 400);
        }
        Db::run('UPDATE proposals SET status=?, reply=? WHERE id=?',
            [$status, mb_substr((string) input('reply', ''), 0, 600), $pid]);
        Auth::audit('proposal_' . $action, (string) $pid);
        json_out(['status' => 'ok']);
    }

    public function bans(array $p): string
    {
        $this->needAdmin();
        json_out(['status' => 'ok', 'bans' => Db::all('SELECT * FROM bans ORDER BY created DESC LIMIT 300')]);
    }

    public function banOp(array $p): string
    {
        $this->needAdmin();
        $action = (string) input('action', '');
        if ($action === 'add') {
            $kind = (string) input('kind', 'ip');
            $value = trim((string) input('value', ''));
            if (!in_array($kind, ['ip', 'user'], true)) {
                json_out(['status' => 'error', 'detail' => '封禁类型非法'], 400);
            }
            if ($value === '') {
                json_out(['status' => 'error', 'detail' => '封禁对象不能为空'], 400);
            }
            if ($kind === 'ip' && !filter_var($value, FILTER_VALIDATE_IP)) {
                json_out(['status' => 'error', 'detail' => 'IP 格式不正确'], 400);
            }
            Db::run('INSERT INTO bans(kind, `value`, reason, created) VALUES(?,?,?,?)',
                [$kind, $value, mb_substr((string) input('reason', ''), 0, 200), microtime(true)]);
            Auth::audit('ban_add', "{$kind}:{$value}");
            json_out(['status' => 'ok']);
        }
        if ($action === 'remove') {
            $id = (int) input('id', 0);
            Db::run('DELETE FROM bans WHERE id=?', [$id]);
            Auth::audit('ban_remove', (string) $id);
            json_out(['status' => 'ok']);
        }
        json_out(['status' => 'error', 'detail' => '未知操作'], 400);
    }

    public function logs(array $p): string
    {
        $this->needAdmin();
        json_out(['status' => 'ok', 'logs' =>
            Db::all('SELECT * FROM admin_logs ORDER BY created DESC LIMIT 500')]);
    }

    public function logsClear(array $p): string
    {
        $this->needAdmin();
        $n = Db::run('DELETE FROM admin_logs');
        Auth::audit('logs_clear', '', ['removed' => $n]);
        json_out(['status' => 'ok', 'removed' => $n]);
    }

    // ---------------------------------------------------------------- 插件目录

    public function catalogGet(array $p): string
    {
        $this->needAdmin();
        json_out(['status' => 'ok'] + Catalog::load());
    }

    public function catalogOp(array $p): string
    {
        $this->needAdmin();
        $action = (string) input('action', '');
        $cat = Catalog::load();

        switch ($action) {
            case 'add_preset': {
                $n = trim((string) input('name', ''));
                if (!preg_match('/^[A-Za-z0-9][A-Za-z0-9._+\-]{0,79}$/', $n)) {
                    json_out(['status' => 'error', 'detail' => '软件包名不合法'], 400);
                }
                foreach ($cat['presets'] as $x) {
                    if ($x['n'] === $n) {
                        json_out(['status' => 'error', 'detail' => '该软件包已存在'], 409);
                    }
                }
                $cat['presets'][] = [
                    'c' => trim((string) input('cat', 'other')),
                    'n' => $n,
                    'l' => mb_substr(trim((string) input('label', $n)), 0, 60),
                    'd' => mb_substr(trim((string) input('desc', '')), 0, 200),
                ];
                break;
            }
            case 'remove_preset': {
                $n = (string) input('name', '');
                $cat['presets'] = array_values(array_filter($cat['presets'],
                    static fn($x) => $x['n'] !== $n));
                break;
            }
            case 'add_cat':
                $k = trim((string) input('key', ''));
                if (!preg_match('/^[a-z0-9_\-]{1,32}$/', $k)) {
                    json_out(['status' => 'error', 'detail' => '分类键不合法'], 400);
                }
                $cat['cats'][] = ['k' => $k, 'l' => mb_substr(trim((string) input('label', $k)), 0, 40)];
                break;
            case 'remove_cat': {
                $k = (string) input('key', '');
                $cat['cats'] = array_values(array_filter($cat['cats'], static fn($x) => $x['k'] !== $k));
                break;
            }
            case 'add_suite': {
                $k = trim((string) input('key', ''));
                $pkgs = Util::packages(preg_split('/\s+/', trim((string) input('pkgs', ''))) ?: []);
                if ($k === '' || !$pkgs) {
                    json_out(['status' => 'error', 'detail' => '套件需要名称与至少一个软件包'], 400);
                }
                if (count($pkgs) > 60) {
                    json_out(['status' => 'error', 'detail' => '单个套件最多 60 个包'], 400);
                }
                $cat['suites'][] = ['k' => $k,
                    'l' => mb_substr(trim((string) input('label', $k)), 0, 40), 'pkgs' => $pkgs];
                break;
            }
            case 'remove_suite': {
                $k = (string) input('key', '');
                $cat['suites'] = array_values(array_filter($cat['suites'], static fn($x) => $x['k'] !== $k));
                break;
            }
            case 'reset': {
                $f = Config::path('data', 'pkg_catalog.default.json');
                if (!is_file($f)) {
                    json_out(['status' => 'error', 'detail' => '默认目录文件缺失'], 500);
                }
                $cat = json_decode((string) file_get_contents($f), true) ?: $cat;
                break;
            }
            default:
                json_out(['status' => 'error', 'detail' => '未知操作'], 400);
        }

        Db::upsert('settings', [
            'key' => 'build.pkg_catalog',
            'value' => json_encode($cat, JSON_UNESCAPED_UNICODE),
            'updated' => microtime(true),
        ], ['key']);
        Auth::audit('catalog_' . $action, '', ['action' => $action]);
        json_out(['status' => 'ok', 'presets' => count($cat['presets']),
                  'cats' => count($cat['cats']), 'suites' => count($cat['suites'])]);
    }

    // ---------------------------------------------------------------- 支付 / 退款 / 赞助

    public function payInfo(array $p): string
    {
        $this->needAdmin();
        json_out([
            'status' => 'ok',
            'enabled' => Pay::enabled(),
            'app_id' => (string) Settings::get('pay.alipay_app_id', ''),
            'notify_url' => Pay::notifyUrl(),
            'sandbox' => Settings::bool('pay.alipay_sandbox', false),
            'has_private_key' => (string) Settings::get('pay.alipay_private_key', '') !== '',
            'has_public_key' => (string) Settings::get('pay.alipay_public_key', '') !== '',
            // ★ 币种键是 sponsor.currency（下拉 CNY/USD/EUR/JPY/HKD），不是 pay.currency。
            //   pay.currency 是个没人设的自由文本键，读它的后果：
            //   管理员把货币单位改成 USD，前台按 sponsor.currency 显示 $，
            //   而后台仪表盘仍显示默认的 CNY —— 同一站两套币种。
            //   ApiController:149 早有注释说明这一点，这里漏改了。
            'currency' => (string) Settings::get('sponsor.currency', 'CNY'),
            'refund_enabled' => Settings::bool('pay.refund_enabled', true),
            'stats' => [
                'total' => (int) Db::val('SELECT COUNT(*) FROM pay_orders', [], 0),
                'paid' => (int) Db::val("SELECT COUNT(*) FROM pay_orders WHERE status='paid'", [], 0),
                'amount' => (float) Db::val("SELECT COALESCE(SUM(amount),0) FROM pay_orders WHERE status='paid'", [], 0),
            ],
        ]);
    }

    public function payTest(array $p): string
    {
        $this->needAdmin();
        $r = Pay::precreate('__selftest__', '测试套餐', 0.01, 1);
        // 真实调用网关；失败就如实报失败，绝不伪造二维码
        Auth::audit('pay_test', $r['out_trade_no'], ['ok' => $r['ok']]);
        json_out([
            'status' => $r['ok'] ? 'ok' : 'error',
            'out_trade_no' => $r['out_trade_no'],
            'has_qr' => $r['qr_code'] !== '',
            'detail' => $r['detail'],
        ], $r['ok'] ? 200 : 502);
    }

    public function payOrders(array $p): string
    {
        $this->needAdmin();
        json_out(['status' => 'ok', 'orders' =>
            Db::all('SELECT * FROM pay_orders ORDER BY created DESC LIMIT 300')]);
    }

    public function payOrderOp(array $p): string
    {
        $this->needAdmin();
        $action = (string) input('action', '');
        $no = trim((string) input('out_trade_no', ''));
        $o = Db::one('SELECT * FROM pay_orders WHERE out_trade_no=?', [$no]);
        if (!$o) {
            json_out(['status' => 'error', 'detail' => '订单不存在'], 404);
        }
        switch ($action) {
            case 'query':
                $paid = Pay::syncOrder($no);
                $o = Db::one('SELECT * FROM pay_orders WHERE out_trade_no=?', [$no]);
                json_out(['status' => 'ok', 'paid' => $paid, 'order_status' => $o['status']]);
            case 'mark_paid':
                // ★ 必须先**原子认领**再发放，顺序不能反。
                //   原实现是「先 grant() 再改状态」且丢弃 rowCount —— 管理员点两次
                //   就发放两次赞助天数（状态只在第二次没变，权益却发了两遍）。
                //   Python 侧早已修好并留了注释（main.py:2451-2461），这里补齐。
                $n = Db::run(
                    "UPDATE pay_orders SET status='paid', paid_at=? "
                    . "WHERE out_trade_no=? AND status='paid_pending'",
                    [microtime(true), $no]);
                if ($n !== 1) {
                    json_out([
                        'status' => 'error',
                        'detail' => "订单当前状态为 {$o['status']}，未发放权益"
                                  . '（仅「已收款待确认」可人工发放；已发放的请勿重复操作）',
                    ], 409);
                }
                Pay::grant((string) $o['username'], (int) $o['days'], (string) $o['tier_name'],
                    (float) $o['amount']);
                Auth::audit('pay_granted', $no, ['username' => $o['username']]);
                break;
            case 'close':
                Db::run("UPDATE pay_orders SET status='closed' WHERE out_trade_no=? "
                    . "AND status NOT IN (" . Pay::paidStatesSql() . ")", [$no]);
                break;
            default:
                json_out(['status' => 'error', 'detail' => '未知操作'], 400);
        }
        Auth::audit('pay_order_' . $action, $no);
        json_out(['status' => 'ok']);
    }

    public function payVerifyStats(array $p): string
    {
        $this->needAdmin();
        json_out(['status' => 'ok', 'stats' => [
            'paid' => (int) Db::val("SELECT COUNT(*) FROM pay_orders WHERE status='paid'", [], 0),
            'pending' => (int) Db::val("SELECT COUNT(*) FROM pay_orders WHERE status IN ('created','pending')", [], 0),
            // 已收款但权益未发放 —— 后台需要一眼看到有多少笔在等人工确认
            'awaiting' => (int) Db::val("SELECT COUNT(*) FROM pay_orders WHERE status='paid_pending'", [], 0),
            'failed' => (int) Db::val("SELECT COUNT(*) FROM pay_orders WHERE status='failed'", [], 0),
            'notify_configured' => Pay::notifyUrl() !== '',
        ]]);
    }

    public function refunds(array $p): string
    {
        $this->needAdmin();
        $status = (string) ($_GET['status'] ?? '');
        $sql = 'SELECT * FROM refund_requests';
        $args = [];
        if ($status !== '') {
            $sql .= ' WHERE status=?';
            $args[] = $status;
        }
        $sql .= ' ORDER BY created DESC LIMIT 300';
        json_out(['status' => 'ok', 'refunds' => Db::all($sql, $args)]);
    }

    /**
     * 退款审批。
     * 关键纪律：网关失败**绝不**把申请标成 approved；线下退款才走 offline 分支。
     */
    public function refundOp(array $p): string
    {
        $admin = $this->needAdmin();
        $rid = (int) input('rid', 0);
        $action = (string) input('action', '');
        $note = mb_substr(trim((string) input('note', '')), 0, 500);
        $r = Db::one('SELECT * FROM refund_requests WHERE id=?', [$rid]);
        if (!$r) {
            json_out(['status' => 'error', 'detail' => '退款申请不存在'], 404);
        }
        if ((string) $r['status'] !== 'pending') {
            json_out(['status' => 'error', 'detail' => '该申请已处理过'], 409);
        }

        if ($action === 'reject') {
            $n = Db::run("UPDATE refund_requests SET status='rejected', reviewed_at=?, reviewer=?, "
                . "review_note=? WHERE id=? AND status='pending'",
                [microtime(true), $admin['username'], $note, $rid]);
            if ($n !== 1) {
                json_out(['status' => 'error', 'detail' => '该申请已被其他人处理'], 409);
            }
            Auth::audit('refund_reject', (string) $rid);
            json_out(['status' => 'ok']);
        }

        if ($action !== 'approve') {
            json_out(['status' => 'error', 'detail' => '未知操作'], 400);
        }

        $offline = (string) input('offline', '') === '1';
        if ($offline) {
            // 线下退款：不调网关，直接撤销权益
            $n = Db::run("UPDATE refund_requests SET status='approved', reviewed_at=?, reviewer=?, "
                . "review_note=?, refund_amount=? WHERE id=? AND status='pending'",
                [microtime(true), $admin['username'], $note . '（线下退款）',
                 (float) $r['amount'], $rid]);
            if ($n !== 1) {
                json_out(['status' => 'error', 'detail' => '该申请已被其他人处理'], 409);
            }
            Db::run("UPDATE pay_orders SET status='refunded' WHERE out_trade_no=?", [$r['out_trade_no']]);
            Pay::revokeSponsor((string) $r['username'], (int) Db::val(
                'SELECT days FROM pay_orders WHERE out_trade_no=?', [$r['out_trade_no']], 0));
            Auth::audit('refund_offline', (string) $rid);
            json_out(['status' => 'ok', 'offline' => true]);
        }

        // 线上退款：先原子占位（pending → processing），避免并发重复退款
        $n = Db::run("UPDATE refund_requests SET status='processing', reviewer=? "
            . "WHERE id=? AND status='pending'", [$admin['username'], $rid]);
        if ($n !== 1) {
            json_out(['status' => 'error', 'detail' => '该申请已被其他人处理'], 409);
        }
        // ★ 调网关前必须复查订单**当前**状态。
        //   只挡住 refund_requests 自身的并发还不够：同一笔订单可能有两条申请
        //   （例如在途时又提交了一条），第二条会把已退款的订单再退一次。
        //   这里用订单状态做最后一道闸。
        $oStatus = (string) Db::val('SELECT status FROM pay_orders WHERE out_trade_no=?',
            [$r['out_trade_no']], '');
        if (!in_array($oStatus, Pay::PAID_STATES, true)) {
            Db::run("UPDATE refund_requests SET status='failed', reviewed_at=?, reviewer=?, "
                . "review_note=? WHERE id=? AND status='processing'",
                [microtime(true), $admin['username'],
                 "关联订单状态为 {$oStatus}，不是可退款状态（可能已退过款）", $rid]);
            Auth::audit('refund_blocked_order_state', (string) $rid, ['order_status' => $oStatus]);
            json_out(['status' => 'error',
                'detail' => "关联订单状态为 {$oStatus}，无法退款（该订单可能已经退过款）"], 409);
        }
        // 幂等键用 RF{rid}，与 Python 侧同款 —— 重试不会退第二次
        $res = Pay::refund((string) $r['out_trade_no'], (float) $r['amount'],
            (string) ($r['reason'] ?? '退款'), 'RF' . $rid);
        Db::run('UPDATE refund_requests SET gateway_raw=? WHERE id=?',
            [$res['raw'], $rid]);
        if (!$res['ok']) {
            // 网关失败：退回 pending，绝不标记成功
            Db::run("UPDATE refund_requests SET status='pending', reviewer=NULL WHERE id=?", [$rid]);
            Auth::audit('refund_gateway_failed', (string) $rid, ['detail' => $res['detail']]);
            json_out(['status' => 'error', 'detail' => '网关退款失败：' . $res['detail']], 502);
        }
        Db::run("UPDATE refund_requests SET status='approved', reviewed_at=?, review_note=?, "
            . "refund_amount=? WHERE id=?",
            [microtime(true), $note, (float) $r['amount'], $rid]);
        Db::run("UPDATE pay_orders SET status='refunded' WHERE out_trade_no=?", [$r['out_trade_no']]);
        Pay::revokeSponsor((string) $r['username'],
            (int) Db::val('SELECT days FROM pay_orders WHERE out_trade_no=?', [$r['out_trade_no']], 0));
        Auth::audit('refund_approve', (string) $rid);
        json_out(['status' => 'ok']);
    }

    public function sponsorClaims(array $p): string
    {
        $this->needAdmin();
        json_out(['status' => 'ok', 'claims' =>
            Db::all('SELECT * FROM sponsor_claims ORDER BY created DESC LIMIT 300')]);
    }

    public function sponsorClaimOp(array $p): string
    {
        $this->needAdmin();
        $action = (string) input('action', '');
        $id = (int) input('id', 0);
        $c = Db::one('SELECT * FROM sponsor_claims WHERE id=?', [$id]);
        if (!$c) {
            json_out(['status' => 'error', 'detail' => '申请不存在'], 404);
        }
        if ($action === 'approve') {
            if ((string) $c['status'] !== 'pending') {
                json_out(['status' => 'error', 'detail' => '该申请已处理'], 409);
            }
            Db::run("UPDATE sponsor_claims SET status='approved' WHERE id=? AND status='pending'", [$id]);
            // 按套餐名反查天数
            $days = 30;
            $tiers = Settings::get('sponsor.tiers', []);
            if (is_string($tiers)) {
                $tiers = json_decode($tiers, true) ?: [];
            }
            foreach ((array) $tiers as $t) {
                if ((string) ($t['name'] ?? '') === (string) $c['tier']) {
                    $days = (int) ($t['days'] ?? 30);
                    break;
                }
            }
            Pay::grant((string) $c['username'], $days, (string) $c['tier'], (float) $c['amount']);
            Auth::audit('claim_approve', (string) $id);
            json_out(['status' => 'ok', 'days' => $days]);
        }
        if ($action === 'reject') {
            Db::run("UPDATE sponsor_claims SET status='rejected' WHERE id=? AND status='pending'", [$id]);
            Auth::audit('claim_reject', (string) $id);
            json_out(['status' => 'ok']);
        }
        json_out(['status' => 'error', 'detail' => '未知操作'], 400);
    }
}
