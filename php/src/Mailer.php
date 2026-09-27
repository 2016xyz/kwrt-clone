<?php
/**
 * 邮件发送（SMTP 直连，无第三方库）。
 *
 * 与 Python 版同策略：
 *   · HTML 模板渲染，**默认对变量做 HTML 转义**，只有显式 raw 的片段原样插入
 *   · 纯文本作为 multipart/alternative 的降级分支
 *   · 收件人与主题都做注入防护（拒绝 CRLF）
 *   · 发送失败不抛给调用方，只记录 —— 邮件失败不该让主流程失败
 */
declare(strict_types=1);

namespace Kwrt;

final class Mailer
{
    private const TPL_DIR = 'templates/mail';

    public static function enabled(): bool
    {
        return Settings::bool('mail.enabled', false);
    }

    /**
     * 自定义模板的 `{var}` 替换 —— 与 Python 版 mailer.render() 同语义。
     *
     * 两个要点（照 Python 抄的，不能想当然）：
     *   · 支持的是**单花括号** `{var}`，与 HTML 文件模板的 `{{var}}` 是两套，
     *     因为后台那两个 textarea 的说明里写的就是 {username} 这种写法；
     *   · **未知变量原样保留** —— 正文里出现的 `{` 可能不是占位符，
     *     全部清掉会误伤用户自己写的内容。
     */
    public static function tpl(string $key, string $def, array $ctx): string
    {
        $t = (string) Settings::get($key, '');
        if (trim($t) === '') {
            $t = $def;
        }
        return (string) preg_replace_callback(
            '/\{([a-zA-Z_][a-zA-Z0-9_]*)\}/',
            static fn(array $m): string => array_key_exists($m[1], $ctx) ? (string) $ctx[$m[1]] : $m[0],
            $t
        );
    }

    /** 验证链接有效期（小时）—— 键名是 mail.verify_ttl_hours（Python 侧同款）。 */
    public static function verifyTtlHours(): int
    {
        return max(1, (int) Settings::get('mail.verify_ttl_hours', 24));
    }

    /** 发验证邮件。@return array{0:bool,1:string} */
    public static function sendVerify(string $to, string $username, string $url, int $hours): array
    {
        if (!self::enabled()) {
            return [false, '邮件通知未启用，跳过通知'];
        }
        $site = (string) Settings::get('site_name', 'Kwrt');
        // ★ 后台「验证邮件主题/正文」必须生效（mail.verify_subject / mail.verify_body）。
        //   Python 侧一直读它们，PHP 原先只发内置文案 —— 管理员改了没用。
        $ctx = [
            'username' => $username,
            'email'    => $to,
            'link'     => $url,
            'verify_url' => $url,          // 兼容旧内建模板里的 {{verify_url}}
            'hours'    => (string) $hours,
            'site'     => $site,
        ];
        $subject = self::tpl('mail.verify_subject', '[Kwrt] 请验证你的邮箱', $ctx);
        $text = self::tpl('mail.verify_body',
            "你好 {username}：\n\n请点击下面的链接完成邮箱验证：\n\n{link}\n\n链接 {hours} 小时内有效。\n",
            $ctx);
        $html = self::render('verify.html', [
            'site' => $site, 'username' => $username, 'verify_url' => $url, 'hours' => $hours,
        ]);
        return self::send($to, $subject, $text, $html, 'verify');
    }

    /**
     * 找回密码邮件。
     *
     * 与 sendVerify 同策略：主题/正文走后台可改的 mail.reset_subject / mail.reset_body。
     * 视觉上刻意用**琥珀色**（accent #b45309）而不是注册验证的蓝色 ——
     * 改口令是高影响操作，不该和「欢迎注册」长得一样。
     */
    public static function sendReset(string $to, string $username, string $url, float $hours): array
    {
        if (!self::enabled()) {
            return [false, '邮件通知未启用，跳过通知'];
        }
        $site = (string) Settings::get('site_name', 'Kwrt');
        $h = rtrim(rtrim(number_format($hours, 2, '.', ''), '0'), '.');
        $ctx = [
            'username'   => $username,
            'email'      => $to,
            'link'       => $url,
            'reset_url'  => $url,          // 兼容内建模板里的 {{reset_url}}
            'hours'      => $h,
            'ttl'        => $h,
            'site'       => $site,
        ];
        $subject = self::tpl('mail.reset_subject', '[Kwrt] 重置你的登录密码', $ctx);
        $text = self::tpl('mail.reset_body',
            "你好 {username}：\n\n我们收到了重置 {site} 登录密码的请求，请点击下面的链接设置新密码：\n\n"
            . "{link}\n\n该链接 {hours} 小时内有效，且只能使用一次。\n"
            . "如果这不是你本人的操作，请直接忽略本邮件，你的密码不会有任何变化。\n",
            $ctx);
        $html = self::render('reset.html', [
            'site' => $site, 'username' => $username, 'reset_url' => $url,
            'hours' => $h, 'ttl' => $h,
            'accent' => '#b45309',
        ], ['accent']);
        return self::send($to, $subject, $text, $html, 'reset');
    }

    /** 构建完成通知。 */
    public static function sendBuildDone(string $to, string $username, array $info): array
    {
        if (!self::enabled()) {
            return [false, '邮件通知未启用，跳过通知'];
        }
        $rows = '';
        foreach (($info['links'] ?? []) as $l) {
            $rows .= '<tr><td style="padding:6px 10px;border-bottom:1px solid #eee">'
                   . '<a href="' . e($l['url']) . '">' . e($l['filename']) . '</a></td>'
                   . '<td style="padding:6px 10px;border-bottom:1px solid #eee">'
                   . e(bytes_h((float) ($l['size'] ?? 0))) . '</td></tr>';
        }
        $ctx = [
            'site'     => (string) Settings::get('site_name', 'Kwrt'),
            'username' => $username,
            'target'   => (string) ($info['target'] ?? ''),
            'profile'  => (string) ($info['profile'] ?? ''),
            'version'  => (string) ($info['version'] ?? ''),
            'count'    => (int) ($info['count'] ?? 0),
            'duration' => (string) ($info['duration'] ?? ''),
            'hours'    => (int) Settings::get('download.link_ttl_hours', 72),
            'rows'     => $rows,
        ];
        $html = self::render('build_ok.html', $ctx);
        // ★ 后台「邮件主题模板 / 邮件正文模板」必须生效（mail.subject_tpl / mail.body_tpl）。
        //   body_tpl 的变量里 {links} 是**多行下载清单**，单独拼好再代入。
        //   纯文本分支：Python 侧就是把这些 render 后的文本当 text/plain 发出去的。
        $lines = [];
        foreach (($info['links'] ?? []) as $l) {
            $sz = (int) ($l['size'] ?? 0);
            $lines[] = '  · ' . ($l['filename'] ?? '') . '  ('
                     . ($sz > 0 ? bytes_h((float) $sz) : '-') . ")\n    " . ($l['url'] ?? '');
        }
        $tctx = [
            'username' => $username !== '' ? $username : '朋友',
            'site'     => (string) Settings::get('site_name', 'Kwrt'),
            'target'   => (string) ($info['target'] ?? '-'),
            'profile'  => (string) ($info['profile'] ?? '-'),
            'version'  => (string) ($info['version'] ?? '-'),
            'count'    => (int) ($info['count'] ?? 0),
            'duration' => (string) ($info['duration'] ?? '-'),
            'hours'    => (int) Settings::get('download.link_ttl_hours', 72),
            'links'    => implode("\n", $lines) !== '' ? implode("\n", $lines) : '(无产物)',
        ];
        $subject = self::tpl('mail.subject_tpl', '[Kwrt] 你的 {target} 固件已构建完成', $tctx);
        $text = self::tpl('mail.body_tpl',
            "你好 {username}：\n\n你定制的 {target} / {profile} 固件（{version}）已构建完成，"
            . "共 {count} 个文件，耗时 {duration}。\n\n下载链接（{hours} 小时内有效）：\n{links}\n",
            $tctx);
        return self::send($to, $subject, $text, $html, 'build_done', ['rows']);
    }

    public static function sendBuildFail(string $to, string $username, string $error): array
    {
        if (!self::enabled()) {
            return [false, '邮件通知未启用，跳过通知'];
        }
        // ★ 键名 mail.notify_fail（Python 侧 main.py 与 mailer.py 都查它）。
        //   关掉后失败不再发信 —— 原先 PHP 无论开关如何都会发。
        if (!Settings::bool('mail.notify_fail', true)) {
            return [false, '构建失败通知已关闭'];
        }
        $ctx = ['site' => (string) Settings::get('site_name', 'Kwrt'),
                'username' => $username, 'error' => $error];
        $html = self::render('build_fail.html', $ctx);
        $text = "构建失败：\n{$error}\n";
        return self::send($to, '固件构建失败', $text, $html, 'build_fail');
    }

    /** SMTP 测试邮件。 */
    public static function sendTest(string $to): array
    {
        $ctx = [
            'site'      => (string) Settings::get('site_name', 'Kwrt'),
            'host'      => (string) Settings::get('mail.host', ''),
            'enc'       => (string) Settings::get('mail.encryption', ''),
            'from_addr' => (string) Settings::get('mail.from_addr', ''),
            'now'       => date('Y-m-d H:i:s'),
        ];
        $html = self::render('test.html', $ctx);
        return self::send($to, 'SMTP 测试邮件', "这是一封测试邮件（{$ctx['now']}）。", $html, 'test');
    }

    // ------------------------------------------------------------- 模板

    /**
     * 渲染邮件模板。
     * @param array $raw 需要**原样插入**（已是安全 HTML）的键；其余一律转义
     */
    public static function render(string $name, array $ctx, array $raw = []): string
    {
        if (!preg_match('/^[a-z0-9_]+\.[a-z]+$/', $name)) {
            throw new \RuntimeException("邮件模板名非法: {$name}");
        }
        $layout = Config::path(self::TPL_DIR, 'layout.html');
        $body = '';
        if (is_file($layout)) {
            $body = (string) file_get_contents($layout);
        }
        $inner = '';
        $f = Config::path(self::TPL_DIR, $name);
        if (is_file($f)) {
            $inner = (string) file_get_contents($f);
        }
        $vars = ['title' => $ctx['site'] ?? '', 'accent' => '#2563eb', 'site' => $ctx['site'] ?? ''];
        $inner = self::subst($inner, $ctx, $raw);
        $body = self::subst($body, array_merge($vars, $ctx), array_merge($raw, ['content']));
        return str_replace('{{content}}', $inner, $body);
    }

    private static function subst(string $tpl, array $ctx, array $raw): string
    {
        return (string) preg_replace_callback('/\{\{([a-z0-9_]+)\}\}/i', static function ($m) use ($ctx, $raw) {
            $k = $m[1];
            $v = $ctx[$k] ?? '';
            return in_array($k, $raw, true) ? (string) $v : e($v);
        }, $tpl);
    }

    // ------------------------------------------------------------- SMTP

    /** @return array{0:bool,1:string} */
    public static function send(string $to, string $subject, string $text, string $html = '',
                                string $purpose = 'generic'): array
    {
        // 注入防护：收件人与主题绝不允许出现 CR/LF
        foreach (['to' => $to, 'subject' => $subject] as $what => $v) {
            if (preg_match('/[\r\n]/', $v)) {
                return [false, "{$what} 含非法换行"];
            }
        }
        if (!filter_var($to, FILTER_VALIDATE_EMAIL)) {
            return [false, '收件人邮箱格式不正确'];
        }
        if (!self::enabled()) {
            return [false, '邮件通知未启用，跳过通知'];
        }

        $host = (string) Settings::get('mail.host', '');
        $port = (int) Settings::get('mail.port', 465);
        // ★ 键名是 mail.user，不是 mail.username —— schema 里没有 mail.username。
        //   写错键名的后果是**静默失效**：Settings::get 返回默认值 ''，
        //   于是 SMTP AUTH 的用户名永远为空，任何需要认证的邮件服务器都发不出邮件，
        //   而且不会报「配置缺失」，只会在连接阶段被服务器拒绝，极难定位。
        //   （由 tools/verify_integrity.py 的「设置键是否存在」检查发现。）
        $user = (string) Settings::get('mail.user', '');
        $pass = (string) Settings::get('mail.password', '');
        $from = (string) Settings::get('mail.from_addr', $user);
        $fromName = (string) Settings::get('mail.from_name', (string) Settings::get('site_short', 'Kwrt'));
        $enc = strtolower((string) Settings::get('mail.encryption', 'ssl'));
        if ($host === '') {
            return [false, 'SMTP 服务器未配置'];
        }

        $boundary = 'kwrt-' . bin2hex(random_bytes(8));
        $headers = [
            'From' => sprintf('%s <%s>', self::encodeHeader($fromName), $from),
            'To' => $to,
            'Subject' => self::encodeHeader($subject),
            'Date' => date('r'),
            'Message-ID' => '<' . bin2hex(random_bytes(12)) . '@' . (Net::host() ?: 'localhost') . '>',
            'MIME-Version' => '1.0',
        ];
        if ($html !== '') {
            $headers['Content-Type'] = 'multipart/alternative; boundary="' . $boundary . '"';
            $body = "--{$boundary}\r\nContent-Type: text/plain; charset=UTF-8\r\n"
                  . "Content-Transfer-Encoding: base64\r\n\r\n" . chunk_split(base64_encode($text))
                  . "--{$boundary}\r\nContent-Type: text/html; charset=UTF-8\r\n"
                  . "Content-Transfer-Encoding: base64\r\n\r\n" . chunk_split(base64_encode($html))
                  . "--{$boundary}--\r\n";
        } else {
            $headers['Content-Type'] = 'text/plain; charset=UTF-8';
            $headers['Content-Transfer-Encoding'] = 'base64';
            $body = chunk_split(base64_encode($text));
        }

        $err = '';
        try {
            // 连接超时是引擎参数、不是业务配置：schema 里没有 mail.timeout，
            // 读 Settings 只会让人以为后台能调（实际永远取默认）。改为常量。
            $sock = self::connect($host, $port, $enc, 20);
        } catch (\Throwable $ex) {
            return [false, 'SMTP 连接失败: ' . $ex->getMessage()];
        }
        try {
            self::expect($sock, '220');
            $ehlo = Net::host() ?: 'localhost';
            self::cmd($sock, 'EHLO ' . $ehlo);
            self::expect($sock, '250');
            if ($enc === 'tls' || $enc === 'starttls') {
                self::cmd($sock, 'STARTTLS');
                self::expect($sock, '220');
                if (!stream_socket_enable_crypto($sock, true, STREAM_CRYPTO_METHOD_TLS_CLIENT)) {
                    throw new \RuntimeException('STARTTLS 握手失败');
                }
                self::cmd($sock, 'EHLO ' . $ehlo);
                self::expect($sock, '250');
            }
            if ($user !== '') {
                self::cmd($sock, 'AUTH LOGIN');
                self::expect($sock, '334');
                self::cmd($sock, base64_encode($user));
                self::expect($sock, '334');
                self::cmd($sock, base64_encode($pass));
                self::expect($sock, '235');
            }
            self::cmd($sock, 'MAIL FROM:<' . $from . '>');
            self::expect($sock, '250');
            self::cmd($sock, 'RCPT TO:<' . $to . '>');
            self::expect($sock, ['250', '251']);
            self::cmd($sock, 'DATA');
            self::expect($sock, '354');
            $data = '';
            foreach ($headers as $k => $v) {
                $data .= $k . ': ' . $v . "\r\n";
            }
            $data .= "\r\n" . $body;
            // 点填充：正文里以 . 开头的行必须转义，否则会被当成结束标记
            $data = preg_replace('/^\\./m', '..', $data) ?? $data;
            fwrite($sock, $data . "\r\n.\r\n");
            self::expect($sock, '250');
            self::cmd($sock, 'QUIT');
        } catch (\Throwable $ex) {
            $err = $ex->getMessage();
        } finally {
            @fclose($sock);
        }
        return $err === '' ? [true, '已发送'] : [false, $err];
    }

    private static function connect(string $host, int $port, string $enc, int $timeout)
    {
        $prefix = ($enc === 'ssl') ? 'ssl://' : '';
        $ctx = stream_context_create(['ssl' => ['verify_peer' => true, 'verify_peer_name' => true]]);
        $sock = @stream_socket_client("{$prefix}{$host}:{$port}", $eno, $estr, $timeout,
            STREAM_CLIENT_CONNECT, $ctx);
        if (!$sock) {
            throw new \RuntimeException("连接 {$host}:{$port} 失败（{$estr}）");
        }
        stream_set_timeout($sock, $timeout);
        return $sock;
    }

    private static function cmd($sock, string $line): void
    {
        fwrite($sock, $line . "\r\n");
    }

    /** @param string|array $want */
    private static function expect($sock, $want): void
    {
        $want = (array) $want;
        $line = '';
        while (($l = fgets($sock, 1024)) !== false) {
            $line = $l;
            if (strlen($l) < 4 || $l[3] !== '-') {
                break;
            }
        }
        $code = substr(trim((string) $line), 0, 3);
        if (!in_array($code, $want, true)) {
            throw new \RuntimeException('SMTP 响应异常: ' . trim((string) $line));
        }
    }

    /** 非 ASCII 主题/发件人名按 RFC 2047 编码。 */
    private static function encodeHeader(string $s): string
    {
        if (preg_match('/^[\x20-\x7e]*$/', $s)) {
            return $s;
        }
        return '=?UTF-8?B?' . base64_encode($s) . '?=';
    }
}
