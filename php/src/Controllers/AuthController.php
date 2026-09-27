<?php
/**
 * 鉴权控制器：登录 / 注册 / 退出 / 邮箱验证。
 *
 * 安全口径与 Python 版对齐：
 *   · 口令 PBKDF2-SHA256(240k) + 每用户随机盐，与 Python 写出的哈希**互相可验证**
 *   · 登录失败 8 次 / 300 秒锁定（用户名 + IP 双维度）
 *   · 注册与重发验证邮件按 IP 限速（5/小时、5/10 分钟）
 *   · 保留用户名黑名单
 *   · 邮箱验证令牌：库里只存 token 的 SHA256，明文只出现在邮件里
 */
declare(strict_types=1);

namespace Kwrt\Controllers;

use Kwrt\Auth;
use Kwrt\Captcha;
use Kwrt\Db;
use Kwrt\Mailer;
use Kwrt\Net;
use Kwrt\Reset;
use Kwrt\Settings;
use Kwrt\Util;
use Kwrt\View;

final class AuthController
{
    private const RESERVED = ['admin', 'root', 'system', 'administrator', 'kwrt', 'support',
                              'www', 'null', 'undefined', 'test', 'guest'];
    private const USERNAME_RE = '/^[A-Za-z0-9_\-]{3,32}$/';
    /**
     * 验证链接默认有效期。
     *
     * ★ 真源是设置项 mail.verify_ttl_hours（Python 侧读它，后台也能改）。
     *   原先这个值是写死的常量 86400 —— 管理员改设置没有任何效果。
     *   常量只作为读不到设置时的兜底。
     */
    private const VERIFY_TTL = 86400;

    private static function VERIFY_TTL_SEC(): int
    {
        return Mailer::verifyTtlHours() * 3600;
    }

    public function loginPage(array $p): string
    {
        if (Auth::currentUser()) {
            redirect('/');
        }
        return View::page('login', [
            'pageTitle' => '登录 / 注册 · ' . (string) Settings::get('site_short', 'Kwrt'),
        ]);
    }

    public function verifyPage(array $p): string
    {
        $token = (string) ($_GET['token'] ?? '');
        $msg = '验证链接无效或已过期。';
        $ok = false;
        if ($token !== '' && preg_match('/^[0-9a-f]{32,128}$/', $token)) {
            [$ok, $msg] = $this->consumeVerifyToken($token);
        }
        return View::page('verify', [
            'pageTitle' => '邮箱验证',
            'ok' => $ok,
            'message' => $msg,
        ]);
    }

    // ---------------------------------------------------------------- 登录 / 注册

    public function loginPost(array $p): string
    {
        if (!Settings::bool('page.login_enabled', true)) {
            json_out(['status' => 'error', 'detail' => '登录功能已关闭'], 403);
        }
        $username = trim((string) input('username', ''));
        $password = (string) input('password', '');
        // 验证码：放在**任何密码比对之前** —— 否则撞库请求仍会消耗一次密码校验，
        // 验证码就只是多一步而不是真正的门。开关由 security.captcha_enabled 控制。
        if (Captcha::enabled()) {
            [$ok, $why] = Captcha::verify((string) input('captcha_id', ''),
                                          (string) input('captcha_code', ''));
            if (!$ok) {
                Auth::audit('login_captcha_failed', $username, $why);
                json_out(['status' => 'error', 'detail' => $why, 'captcha' => true], 400);
            }
        }

        // 限速前置：被锁定时不给任何「用户名是否存在」的提示差异
        $wait = Auth::loginLocked($username);
        if ($wait > 0) {
            json_out(['status' => 'error', 'detail' => "尝试过于频繁，请 {$wait} 秒后再试"], 429);
        }
        if ($username === '' || $password === '') {
            json_out(['status' => 'error', 'detail' => '请输入用户名和密码'], 400);
        }

        $u = Db::one('SELECT * FROM users WHERE username=?', [$username]);
        if (!$u) {
            Auth::loginFail($username);
            json_out(['status' => 'error', 'detail' => '用户名或密码错误'], 403);
        }
        if ((int) ($u['disabled'] ?? 0) === 1) {
            json_out(['status' => 'error', 'detail' => '账号已被停用，请联系管理员'], 403);
        }
        [$ok, $needUpgrade] = Auth::verifyPw($password, (string) $u['password']);
        if (!$ok) {
            Auth::loginFail($username);
            json_out(['status' => 'error', 'detail' => '用户名或密码错误'], 403);
        }

        // 历史无盐哈希 → 登录成功即升级为加盐格式
        if ($needUpgrade) {
            Db::run('UPDATE users SET password=? WHERE username=?',
                [Auth::hashPw($password), $username]);
        }
        Auth::loginReset($username);
        Auth::login($u);
        Auth::audit('login', $username);
        json_out(['status' => 'ok', 'username' => $username,
                  'is_admin' => Auth::isAdmin($u), 'sponsor' => Auth::isSponsor($u)]);
    }

    public function registerPost(array $p): string
    {
        if (!Settings::bool('page.register_enabled', true)) {
            json_out(['status' => 'error', 'detail' => '注册功能已关闭'], 403);
        }
        // 注册验证码：独立开关 security.captcha_on_register，
        // 与登录开关分开 —— 很多站点只想挡批量注册，不想给登录加步骤。
        if (Captcha::enabledForRegister()) {
            [$cok, $cwhy] = Captcha::verify((string) input('captcha_id', ''),
                                            (string) input('captcha_code', ''));
            if (!$cok) {
                json_out(['status' => 'error', 'detail' => $cwhy, 'captcha' => true], 400);
            }
        }
        $ip = Net::clientIp();
        [$ok, $wait] = Auth::rateOk('register', $ip, 5, 3600);
        if (!$ok) {
            json_out(['status' => 'error', 'detail' => "注册过于频繁，请 {$wait} 秒后再试"], 429);
        }

        $username = trim((string) input('username', ''));
        $email = trim((string) input('email', ''));
        $password = (string) input('password', '');

        if (!preg_match(self::USERNAME_RE, $username)) {
            json_out(['status' => 'error', 'detail' => '用户名需 3–32 位字母/数字/下划线/短横线'], 400);
        }
        if (in_array(strtolower($username), self::RESERVED, true)) {
            json_out(['status' => 'error', 'detail' => '该用户名为系统保留'], 400);
        }
        if (mb_strlen($password) < 8) {
            json_out(['status' => 'error', 'detail' => '密码至少 8 位'], 400);
        }
        if ($email !== '' && !filter_var($email, FILTER_VALIDATE_EMAIL)) {
            json_out(['status' => 'error', 'detail' => '邮箱格式不正确'], 400);
        }

        $exists = Db::one('SELECT username, email_verified FROM users WHERE username=?', [$username]);
        if ($exists) {
            // 已注册但未验证：允许重新发验证邮件，不泄露「密码是否正确」
            if ((int) ($exists['email_verified'] ?? 1) === 0) {
                json_out(['status' => 'error', 'detail' => '该用户名已注册但未验证邮箱，可重新发送验证邮件'],
                    409);
            }
            json_out(['status' => 'error', 'detail' => '用户名已存在'], 409);
        }

        // ★ 键名是 mail.verify_register（Python 侧也用这个）。
        //   原写 mail.verify_enabled 在 schema 中不存在 → 永远取默认 false
        //   → **邮箱验证在 PHP 版从未生效过**，用户注册后可直接登录。
        $needVerify = $email !== '' && Settings::bool('mail.verify_register', false);
        $now = microtime(true);
        Db::run('INSERT INTO users(username, password, email, sponsor, created, role, disabled, '
            . 'quota, email_verified) VALUES(?,?,?,0,?,?,0,?,?)',
            [$username, Auth::hashPw($password), $email, $now, 'user',
             // ★ 键名是 default_quota（无 build. 前缀，与 Python 一致）
             (int) Settings::get('default_quota', 12), $needVerify ? 0 : 1]);

        $u = Db::one('SELECT * FROM users WHERE username=?', [$username]);
        Auth::login($u);
        Auth::audit('register', $username, ['email' => $email]);

        if ($needVerify) {
            $this->sendVerifyMail($username, $email);
        }
        json_out(['status' => 'ok', 'username' => $username, 'need_verify' => $needVerify]);
    }

    public function logoutPost(array $p): string
    {
        $u = Auth::currentUser();
        if ($u) {
            Auth::audit('logout', (string) $u['username']);
        }
        Auth::logout();
        json_out(['status' => 'ok']);
    }

    // ---------------------------------------------------------------- 邮箱验证

    public function verifyEmail(array $p): string
    {
        $token = trim((string) input('token', ''));
        if ($token === '' || !preg_match('/^[0-9a-f]{32,128}$/', $token)) {
            json_out(['status' => 'error', 'detail' => '验证链接无效'], 400);
        }
        [$ok, $msg] = $this->consumeVerifyToken($token);
        json_out(['status' => $ok ? 'ok' : 'error', 'detail' => $msg], $ok ? 200 : 400);
    }

    public function resendVerify(array $p): string
    {
        $ip = Net::clientIp();
        [$ok, $wait] = Auth::rateOk('resend_verify', $ip, 5, 600);
        if (!$ok) {
            json_out(['status' => 'error', 'detail' => "发送过于频繁，请 {$wait} 秒后再试"], 429);
        }
        $username = trim((string) input('username', ''));
        $u = Db::one('SELECT * FROM users WHERE username=?', [$username]);
        // 不泄露账号是否存在：无论结果都回同一句话
        if (!$u || (int) ($u['email_verified'] ?? 1) === 1 || empty($u['email'])) {
            json_out(['status' => 'ok', 'detail' => '如果账号存在且未验证，验证邮件已发送']);
        }
        // 同一账号 60 秒冷却
        $last = Db::val('SELECT created FROM email_send_log WHERE username=? AND purpose=? '
            . 'ORDER BY id DESC LIMIT 1', [$username, 'verify']);
        if ($last && microtime(true) - (float) $last < 60) {
            json_out(['status' => 'ok', 'detail' => '验证邮件刚刚已发送，请稍后查收']);
        }
        $this->sendVerifyMail($username, (string) $u['email']);
        json_out(['status' => 'ok', 'detail' => '如果账号存在且未验证，验证邮件已发送']);
    }

    private function sendVerifyMail(string $username, string $email): void
    {
        $token = bin2hex(random_bytes(32));
        $now = microtime(true);
        // 库里只存摘要；明文令牌只出现在邮件里（与 Python 版同策略）
        Db::run('DELETE FROM email_verifications WHERE username=? AND used=0', [$username]);
        Db::run('INSERT INTO email_verifications(token_hash, username, email, created, expires, used, ip) '
            . 'VALUES(?,?,?,?,?,0,?)',
            [hash('sha256', $token), $username, $email, $now, $now + self::VERIFY_TTL_SEC(), Net::clientIp()]);

        $url = Net::url('/verify/?token=' . $token);
        $hours = Mailer::verifyTtlHours();
        [$ok, $detail] = Mailer::sendVerify($email, $username, $url, $hours);
        Db::run('INSERT INTO email_send_log(purpose, to_addr, username, ok, detail, created) '
            . 'VALUES(?,?,?,?,?,?)',
            ['verify', $email, $username, $ok ? 1 : 0, (string) $detail, microtime(true)]);
    }

    /** @return array{0:bool,1:string} */
    private function consumeVerifyToken(string $token): array
    {
        $hash = hash('sha256', $token);
        return Db::tx(function () use ($hash) {
            $row = Db::one('SELECT * FROM email_verifications WHERE token_hash=?', [$hash]);
            if (!$row) {
                return [false, '验证链接无效或已过期。'];
            }
            if ((int) $row['used'] === 1) {
                return [false, '该验证链接已被使用过。'];
            }
            if ((float) $row['expires'] < microtime(true)) {
                return [false, '验证链接已过期，请重新发送验证邮件。'];
            }
            // 原子置为已用：并发点两次也只有一次生效
            $n = Db::run('UPDATE email_verifications SET used=1, used_at=? '
                . 'WHERE id=? AND used=0', [microtime(true), (int) $row['id']]);
            if ($n !== 1) {
                return [false, '该验证链接已被使用过。'];
            }
            Db::run('UPDATE users SET email_verified=1 WHERE username=?', [$row['username']]);
            return [true, '邮箱验证成功，现在可以正常接收构建通知了。'];
        });
    }

    // ================================================================ 找回密码

    /** 找回密码页：带 token 则进入「设置新密码」态，否则进入「申请」态。 */
    public function resetPage(array $p): string
    {
        $token = (string) ($_GET['token'] ?? '');
        $username = '';
        $bad = '';
        $step = 'request';
        if ($token !== '') {
            [$ok, $u, $why] = Reset::check($token);
            if ($ok) {
                $username = $u;
                $step = 'setpw';
            } else {
                $bad = $why;
                $step = 'bad';
            }
        }
        return View::page('reset', [
            'pageTitle'  => '找回密码',
            'step'       => $step,
            'token'      => $token,
            'username'   => $username,
            'bad'        => $bad,
            'ttlHours'   => Reset::ttlHours(),
            // 与 /api/v1/site 的 captcha.reset 同源，避免两个地方各判一次
            'capNeeded'  => Captcha::enabledForReset(),
        ]);
    }

    /**
     * 申请重置密码。
     *
     * ★ 反枚举：账号不存在 / 未登记邮箱 / 冷却中 / 已停用 —— 对外**一律同一句**。
     *   这些差异只进日志。若做成不同状态码，就等于给攻击者一个
     *   「该账号存在且刚申请过」的判定接口。
     */
    public function resetRequest(array $p): string
    {
        $ip = Net::clientIp();

        // 1) 验证码 —— 放在任何查询与发信之前。这个接口会真实发信，
        //    没有验证码就是现成的邮件轰炸放大器。
        if (Captcha::enabledForReset()) {
            [$cok, $cwhy] = Captcha::verify((string) input('captcha_id', ''),
                                            (string) input('captcha_code', ''));
            if (!$cok) {
                json_out(['status' => 'error', 'detail' => $cwhy, 'captcha' => true], 400);
            }
        }

        $account = trim((string) input('account', ''));
        if ($account === '') {
            json_out(['status' => 'error', 'detail' => '请填写用户名或邮箱'], 400);
        }

        // 2) 来源封禁与限速（站点级，对所有账号一视同仁，不构成枚举面）
        if (Auth::banned()) {
            json_out(['status' => 'error', 'detail' => '该来源已被管理员封禁'], 403);
        }
        [$ok, $wait] = Auth::rateOk('reset', $ip, 5, 3600);
        if (!$ok) {
            json_out(['status' => 'error', 'detail' => "请求过于频繁，请 {$wait} 秒后再试"], 429);
        }

        // 3) SMTP 没配好时明确报错 —— 这是**站点级**状态，对任何账号都一样，
        //    不泄露账号是否存在；若静默返回成功，用户会一直等一封永远不来的信。
        if (!Mailer::enabled()) {
            json_out(['status' => 'error',
                'detail' => '本站邮件服务尚未配置（邮件通知未启用），请联系管理员重置密码'], 503);
        }

        // 4) 查账号（用户名 或 邮箱，邮箱忽略大小写）
        $u = Db::one('SELECT username,email,disabled FROM users '
            . 'WHERE username=? OR (email<>\'\' AND LOWER(email)=LOWER(?)) LIMIT 1',
            [$account, $account]);
        if (!$u || (int) ($u['disabled'] ?? 0) === 1 || trim((string) ($u['email'] ?? '')) === '') {
            Auth::audit('reset_request_unknown', $account, '账号缺失/停用/无邮箱（对外统一回复）');
            json_out(['status' => 'ok', 'message' => Reset::REPLY]);
        }
        $username = (string) $u['username'];
        $email = trim((string) $u['email']);

        // 5) 账号冷却：60 秒内不重复发。
        //    ★ 这里**不能**返回 429 —— 那会让「存在且刚申请过」与「不存在」可区分。
        //      静默跳过发送，对外仍是同一句话。
        $left = Reset::cooldownLeft($username);
        if ($left > 0) {
            Auth::audit('reset_request_cooldown', $username, "冷却中，剩余 {$left}s");
            json_out(['status' => 'ok', 'message' => Reset::REPLY]);
        }

        $this->sendResetMail($username, $email, $ip);
        json_out(['status' => 'ok', 'message' => Reset::REPLY]);
    }

    /** 校验令牌（不消费），用于让用户在填新密码前知道在为哪个账号操作。 */
    public function resetCheck(array $p): string
    {
        [$ok, $username, $why] = Reset::check((string) input('token', ''));
        if (!$ok) {
            json_out(['status' => 'error', 'detail' => $why], 400);
        }
        json_out(['status' => 'ok', 'username' => $username, 'ttl_hours' => Reset::ttlHours()]);
    }

    /**
     * 用令牌设置新密码。
     *
     * 顺序很关键：先**原子消费**令牌（并发只放一方过），再改口令，
     * 然后注销该账号**全部会话** —— 否则被盗号者只要不刷新就能继续用旧会话。
     * 最后作废其余找回令牌与未用的邮箱验证令牌，并清掉登录失败锁定计数。
     */
    public function resetConfirm(array $p): string
    {
        $ip = Net::clientIp();
        $password = (string) input('password', '');

        if (mb_strlen($password) < 6) {
            json_out(['status' => 'error', 'detail' => '密码至少 6 位'], 400);
        }
        // 令牌爆破/暴力尝试限速（令牌 256 bit，此处只是兜底）
        [$ok, $wait] = Auth::rateOk('reset_confirm', $ip, 20, 3600);
        if (!$ok) {
            json_out(['status' => 'error', 'detail' => "尝试过于频繁，请 {$wait} 秒后再试"], 429);
        }

        [$rok, $username, $why] = Reset::consume((string) input('token', ''));
        if (!$rok) {
            Auth::audit('reset_confirm_failed', '', $why);
            json_out(['status' => 'error', 'detail' => $why], 400);
        }

        $n = Db::run('UPDATE users SET password=? WHERE username=?',
            [Auth::hashPw($password), $username]);
        if ($n === 0) {
            Auth::audit('reset_confirm_failed', $username, '账号已不存在');
            json_out(['status' => 'error', 'detail' => '账号不存在，请联系管理员'], 400);
        }
        Db::run('DELETE FROM sessions WHERE username=?', [$username]);

        Reset::invalidate($username);          // 作废其余找回令牌
        Db::run('UPDATE email_verifications SET used=1, used_at=? WHERE username=? AND used=0',
            [microtime(true), $username]);     // 顺手作废未用的邮箱验证令牌
        Auth::loginReset($username);           // 清失败计数，否则改完密码还可能被锁
        Auth::audit('password_reset', $username, '通过邮件链接重置密码');

        // 自动登录：用户刚证明了自己掌握邮箱，口令也是他自己刚设的
        $row = Db::one('SELECT * FROM users WHERE username=?', [$username]);
        if ($row) {
            Auth::login($row);
        }
        json_out(['status' => 'ok', 'username' => $username,
            'sponsor' => (int) ($row['sponsor'] ?? 0) === 1,
            'is_admin' => Auth::isAdmin($row)]);
    }

    private function sendResetMail(string $username, string $email, string $ip): void
    {
        $token = Reset::issue($username, $email, Reset::ttlHours(), $ip);
        $url = Net::url('/reset/?token=' . $token);
        [$mok, $mdetail] = Mailer::sendReset($email, $username, $url, Reset::ttlHours());
        Db::run('INSERT INTO email_send_log(purpose, to_addr, username, ok, detail, created) '
            . 'VALUES(?,?,?,?,?,?)',
            ['reset', $email, $username, $mok ? 1 : 0, $mdetail, microtime(true)]);
        if (!$mok) {
            // 发信失败对该账号是可观察的，但失败原因（SMTP 拒绝/网络不通）
            // 与账号是否存在无关，不构成枚举面；藏起来只会让用户白等。
            json_out(['status' => 'error', 'detail' => "邮件发送失败：{$mdetail}"], 502);
        }
        Auth::audit('reset_request_sent', $username, "已发送至 {$email}");
    }
}
