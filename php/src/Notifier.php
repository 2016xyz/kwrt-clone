<?php
/**
 * 构建结果通知 —— 与 Python 版 main.py 的 on_build_finished() 同语义。
 *
 * 为什么单独一个类：本地后端（Engine）与远端后端（Github）都要在结束时做同一件事
 * —— 签发限时下载链接、按开关发邮件、写回状态。放在一处才不会两边跑偏
 * （这个项目已经踩过多次「同一语义两处实现、只改了一处」）。
 *
 * 开关（键名与 Python 侧逐字一致）：
 *   download.link_ttl_hours   下载令牌有效期
 *   download.external_host    外链域名：产物由 CDN/对象存储托管时，链接指向它
 *   download.email_on_ready   构建完成后自动发邮件
 *   mail.notify_fail          构建失败时也通知
 */
declare(strict_types=1);

namespace Kwrt;

final class Notifier
{
    /**
     * 下载链接的站点前缀。
     *
     * 与 Python 的 _download_base() 同序：external_host > 本站 base_url > 空（相对路径）。
     */
    public static function downloadBase(): string
    {
        $ext = trim((string) Settings::get('download.external_host', ''));
        if ($ext !== '') {
            return rtrim($ext, '/');
        }
        return rtrim(Net::baseUrl(), '/');
    }

    /**
     * 构建完成：整理下载链接 + 按需发邮件。
     *
     * @param array $files 元素形如 ['name','size','url'(可选),'external'(可选)]
     * @return array{links:array,mail:array} 链接清单与邮件结果（供任务详情展示）
     */
    public static function buildDone(string $hash, array $payload, array $files,
                                     float $duration = 0.0): array
    {
        $ttl = max(1, (int) Settings::get('download.link_ttl_hours', 72));
        $base = self::downloadBase();
        $b = Db::one('SELECT username FROM builds WHERE request_hash=?', [$hash]);
        $username = (string) ($payload['username'] ?? ($b['username'] ?? ''));

        $links = [];
        $local = $ext = 0;
        foreach ($files as $f) {
            $name = (string) ($f['name'] ?? '');
            if ($name === '' || Util::safeFilename($name) === '') {
                continue;
            }
            // 产物已在本站存储 → 签发限时令牌（有效期由管理员配置）
            $path = Download::resolvePath($hash, $name);
            if ($path !== null && is_file($path)) {
                $tok = Download::issue($hash, $username, $name);
                $links[] = [
                    'filename' => $name,
                    'size'     => (int) (filesize($path) ?: 0),
                    'url'      => $base . '/dl/t/' . $tok,
                    'path'     => '/dl/t/' . $tok,
                    'expires_hours' => $ttl,
                ];
                $local++;
                continue;
            }
            // 产物仍在 GitHub 侧 → 直接给出远端地址（有效期由 GitHub 保留策略决定）
            $u = (string) ($f['url'] ?? '');
            if ($u === '') {
                continue;
            }
            $links[] = [
                'filename' => $name,
                'size'     => (int) ($f['size'] ?? 0),
                'url'      => $u,
                'path'     => $u,
                'external' => true,
                'expires_hours' => null,
                'note'     => '托管于 GitHub，有效期由 GitHub 产物保留策略决定',
            ];
            $ext++;
        }

        Db::run("UPDATE builds SET status='done' WHERE request_hash=?", [$hash]);

        $mail = ['ok' => false, 'to' => '', 'detail' => '未发送'];
        if (Settings::bool('download.email_on_ready', true)) {
            $to = self::userEmail($payload, $username);
            if ($to !== '') {
                [$ok, $detail] = Mailer::sendBuildDone($to, $username, [
                    'target'   => (string) ($payload['target'] ?? ''),
                    'profile'  => (string) ($payload['profile'] ?? ''),
                    'version'  => (string) ($payload['version'] ?? ''),
                    'count'    => count($links),
                    'duration' => $duration > 0 ? ((int) $duration . ' 秒') : '-',
                    'links'    => $links,
                ]);
                $mail = ['ok' => $ok, 'to' => $to, 'detail' => $detail];
            } else {
                $mail = ['ok' => false, 'to' => '', 'detail' => '用户未填写邮箱，跳过通知'];
            }
        } else {
            $mail = ['ok' => false, 'to' => '', 'detail' => '构建完成通知已关闭'];
        }

        return [
            'links' => $links,
            'local' => $local,
            'external' => $ext,
            'ttl_hours' => $local > 0 ? $ttl : 0,
            'mail' => $mail,
        ];
    }

    /** 构建失败：按 mail.notify_fail 决定是否发信（关掉就不发）。 */
    public static function buildFail(string $hash, array $payload, string $error): array
    {
        if (!Settings::bool('mail.notify_fail', true)) {
            return ['ok' => false, 'to' => '', 'detail' => '构建失败通知已关闭'];
        }
        $b = Db::one('SELECT username FROM builds WHERE request_hash=?', [$hash]);
        $username = (string) ($payload['username'] ?? ($b['username'] ?? ''));
        $to = self::userEmail($payload, $username);
        if ($to === '') {
            return ['ok' => false, 'to' => '', 'detail' => '用户未填写邮箱，跳过通知'];
        }
        [$ok, $detail] = Mailer::sendBuildFail($to, $username, $error);
        return ['ok' => $ok, 'to' => $to, 'detail' => $detail];
    }

    /** 取收件人：先看任务载荷里的 email，再按用户名查库（与 Python 同序）。 */
    private static function userEmail(array $payload, string $username): string
    {
        $to = trim((string) ($payload['email'] ?? ''));
        if ($to !== '') {
            return $to;
        }
        if ($username === '') {
            return '';
        }
        $u = Db::one('SELECT email FROM users WHERE username=?', [$username]);
        return trim((string) ($u['email'] ?? ''));
    }
}
