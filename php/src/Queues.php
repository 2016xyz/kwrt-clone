<?php
declare(strict_types=1);

namespace Kwrt;

/**
 * GitHub 构建队列（多队列）—— 与 Python 版 app/backends.py 的
 * list_queues() / pick_queue() 保持同一套语义与同一份配置键。
 *
 * 说明：PHP 版目前只做**本地构建**，不派发 GitHub workflow
 * （派发与产物回传在 Python 版；两版共用同一个 users.db 与同一份设置）。
 * 因此这里提供的是队列的**读取与选择**能力，用于：
 *   · 后台「测试连接」测试的是**真正会被使用**的那个队列，而不是硬编码的那一个；
 *   · 后台能显示当前配置了几个队列、分别指向哪里。
 * 这样两版对 gh.queues 的解读不会跑偏。
 */
final class Queues
{
    /** 把后台的 gh.queues（JSON 数组）规范化成队列列表。 */
    public static function all(): array
    {
        $raw = Settings::get('gh.queues', []);
        if (is_string($raw)) {
            $raw = json_decode($raw, true);
        }
        $out = [];
        $i = 0;
        foreach ((array) $raw as $q) {
            $i++;
            if (!is_array($q)) {
                continue;
            }
            // 容错：缺 repo 或 token 的项**跳过**，而不是让整个后端失效 ——
            // 配置里一行写错不该导致所有队列都不能用。
            $repo = trim((string) ($q['repo'] ?? ''));
            $token = trim((string) ($q['token'] ?? ''));
            if ($repo === '' || $token === '') {
                continue;
            }
            $out[] = [
                'name'     => trim((string) ($q['name'] ?? ('队列' . $i))) ?: ('队列' . $i),
                'repo'     => $repo,
                'token'    => $token,
                'workflow' => trim((string) ($q['workflow'] ?? 'build-firmware.yml')) ?: 'build-firmware.yml',
                'ref'      => trim((string) ($q['ref'] ?? 'main')) ?: 'main',
                'enabled'  => (bool) ($q['enabled'] ?? true),
            ];
        }
        return array_values(array_filter($out, static fn($q) => $q['enabled']));
    }

    /**
     * 当前可用队列。
     *
     * 兼容旧配置：gh.queues 为空时用 gh.repo/gh.token/gh.workflow/gh.ref
     * 合成**一个**默认队列 —— 老用户升级后不必改任何配置就照常工作。
     */
    public static function available(): array
    {
        $qs = self::all();
        if ($qs) {
            return $qs;
        }
        $repo = trim((string) Settings::get('gh.repo', ''));
        $token = trim((string) Settings::get('gh.token', ''));
        if ($repo !== '' && $token !== '') {
            return [[
                'name'     => '默认队列',
                'repo'     => $repo,
                'token'    => $token,
                'workflow' => trim((string) Settings::get('gh.workflow', 'build-firmware.yml')) ?: 'build-firmware.yml',
                'ref'      => trim((string) Settings::get('gh.ref', 'main')) ?: 'main',
                'enabled'  => true,
            ]];
        }
        return [];
    }

    /** 轮转挑一个队列；无可用队列返回 null。 */
    public static function pick(): ?array
    {
        $qs = self::available();
        if (!$qs) {
            return null;
        }
        // 轮转游标存库里，不能走 Settings —— 那套会按 schema 校验键名，
        // 而 gh.queue_cursor 有意不进 schema（它不是给管理员配的项）。
        // 用 app_secrets 这张通用小表（验证码的盐也放这里）。
        Db::ensureTable('app_secrets');
        $row = Db::one("SELECT `value` FROM app_secrets WHERE name='gh_queue_cursor'");
        $cur = (int) ($row['value'] ?? 0);
        $idx = $cur % count($qs);
        Db::upsert('app_secrets', [
            'name' => 'gh_queue_cursor',
            'value' => (string) (($cur + 1) % max(1, count($qs) * 1000)),
            'created' => microtime(true),
        ], ['name']);
        return $qs[$idx];
    }

    /** 供后台展示（不含 token）。 */
    public static function summary(): array
    {
        return array_map(static fn($q) => [
            'name' => $q['name'], 'repo' => $q['repo'],
            'workflow' => $q['workflow'], 'ref' => $q['ref'],
        ], self::available());
    }
}
