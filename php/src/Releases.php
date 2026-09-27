<?php
/**
 * 固件分支 → 实际发布版本的解析。
 *
 * 对应 Python 版 app/releases.py 的 resolve()。**必须逐条同源**：
 * 后台下拉框给的是**分支号**（25.12），而镜像站只认**发布号**（25.12.5）——
 *     https://downloads.openwrt.org/releases/25.12/targets/x86/64/     → 404
 *     https://downloads.openwrt.org/releases/25.12.5/targets/x86/64/   → 200
 *
 * ★ 这个缺陷的发现过程值得记下来：真编测试脚本自己传的是 "25.12.5"（发布号），
 *   一路通过；而**界面上用户点出来的值是 "25.12"（分支号）**，
 *   结果真用户从界面构建必然卡在「无法下载 ImageBuilder」。
 *   测试与真实路径取值不一致，测试全绿也照样漏 —— 断言必须走界面真实取值。
 */
declare(strict_types=1);

namespace Kwrt;

final class Releases
{
    /** 分支 => [发布号, 包管理器]。与 Python BRANCH_RELEASE 逐条一致。 */
    public const BRANCH_RELEASE = [
        '25.12' => ['25.12.5', 'apk'],
        '24.10' => ['24.10.8', 'opkg'],
        '23.05' => ['23.05.6', 'opkg'],
    ];

    /**
     * 接受「分支名」（25.12）、「发布号」（25.12.5）或本分支下的具体补丁版本，
     * 返回 ['branch'=>…, 'release'=>…, 'backend'=>…]。
     *
     * 与 Python 同样只接受 `major.<1-3 位数字>` 形式，避免把 "25.120" 误判进 25.12
     * 分支；也**不会**把请求的 25.12.9 静默换成 25.12.5
     * （那样用户以为按指定版本构建，实际不是）。
     */
    public static function resolve(string $branchOrVersion): array
    {
        $v = trim($branchOrVersion);
        if ($v !== '') {
            foreach (self::BRANCH_RELEASE as $b => [$rel, $backend]) {
                if ($v === $b || $v === $rel) {
                    return ['branch' => $b, 'release' => $rel, 'backend' => $backend];
                }
                if (preg_match('/^' . preg_quote($b, '/') . '\.[0-9]{1,3}$/', $v)) {
                    return ['branch' => $b, 'release' => $v, 'backend' => $backend];
                }
            }
        }
        // 不认识的取值：默认最新分支（与 Python 一致）
        $keys = array_keys(self::BRANCH_RELEASE);
        sort($keys, SORT_STRING);
        $b = end($keys);
        [$rel, $backend] = self::BRANCH_RELEASE[$b];
        return ['branch' => $b, 'release' => $rel, 'backend' => $backend];
    }

    /** 分支列表，供后台/前台下拉框使用。 */
    public static function branches(): array
    {
        $out = [];
        foreach (self::BRANCH_RELEASE as $b => [$rel, $backend]) {
            $out[$b] = $rel;
        }
        return $out;
    }

    /** target（x86/64）→ 包架构（x86_64）。 */
    public static function arch(string $target): string
    {
        return str_replace('/', '_', $target);
    }

    /**
     * 第三方源（kiddin9 feed）。这些插件不在官方仓库里
     * （如 luci-app-openclash），只有该 feed 提供 —— 实测其 1084 个包里包含它。
     * apk 后端的上游 feed 是 .ipk 格式，无法直接用于 apk 版 ImageBuilder，故为空。
     */
    public const THIRD_PARTY = [
        'opkg' => ['https://dl.openwrt.ai/packages-{branch}/{arch}/kiddin9'],
        'apk'  => [],
    ];

    /** 只有第三方 feed 才有的插件（与 Python THIRD_PARTY_ONLY 对应）。 */
    public const THIRD_PARTY_ONLY = [
        'luci-app-openclash',
        'luci-app-passwall',
        'luci-app-passwall2',
        'luci-app-ssr-plus',
        'luci-app-vssr',
        'luci-app-store',
        'luci-app-dockerman',
        'openclash',
        'luci-theme-argon',
        'luci-app-argon-config',
    ];

    public static function thirdPartyFeeds(string $branch, string $arch, string $backend): array
    {
        $out = [];
        foreach (self::THIRD_PARTY[$backend] ?? [] as $tpl) {
            $out[] = str_replace(['{branch}', '{arch}'], [$branch, $arch], $tpl);
        }
        return $out;
    }

    /** 请求的插件里是否含「只有第三方 feed 才有」的。 */
    public static function needsThirdParty(array $packages): bool
    {
        foreach ($packages as $p) {
            $name = ltrim((string) $p, '-');
            $name = trim(explode(' ', $name)[0]);
            foreach (self::THIRD_PARTY_ONLY as $k) {
                if ($name === $k) {
                    return true;
                }
            }
        }
        return false;
    }

    /**
     * 勾了第三方插件但后端是 apk（25.12）时，回落到 opkg 后端（24.10）——
     * 否则这些包根本装不上（与 Python pick_version 行为一致）。
     */
    public static function resolveForBuild(string $branchOrVersion, array $packages): array
    {
        $r = self::resolve($branchOrVersion);
        if ($r['backend'] === 'apk' && self::needsThirdParty($packages)) {
            $fb = self::resolve('24.10');
            $fb['fallback_from'] = $r['release'];
            return $fb;
        }
        return $r;
    }
}
