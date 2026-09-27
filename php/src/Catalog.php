<?php
/**
 * 软件包目录（预设插件 / 分类 / 套件）。
 *
 * 数据来源：settings 表的 `build.pkg_catalog`（由管理后台「插件目录」维护）。
 * —— 刻意**不**去解析旧的 web/assets/js/presets.js：
 *    PHP 版要求不依赖静态前端资源，且后台改过的目录必须立即生效。
 * 库里没有时回落到 data/pkg_catalog.default.json（随仓库分发的初始清单）。
 */
declare(strict_types=1);

namespace Kwrt;

final class Catalog
{
    private static ?array $cache = null;

    private const EMPTY = ['cats' => [], 'presets' => [], 'suites' => []];

    public static function load(): array
    {
        if (self::$cache !== null) {
            return self::$cache;
        }
        $raw = null;
        try {
            $raw = Db::val('SELECT `value` FROM settings WHERE `key`=?', ['build.pkg_catalog']);
        } catch (\Throwable) {
            $raw = null;
        }
        $d = null;
        if (is_string($raw) && $raw !== '') {
            $d = json_decode($raw, true);
        }
        if (!is_array($d) || !isset($d['presets'])) {
            $f = Config::path('data', 'pkg_catalog.default.json');
            if (is_file($f)) {
                $j = json_decode((string) file_get_contents($f), true);
                if (is_array($j)) {
                    $d = $j;
                }
            }
        }
        if (!is_array($d)) {
            return self::$cache = self::EMPTY;
        }
        $out = [
            'cats'    => array_values($d['cats'] ?? []),
            'presets' => array_values($d['presets'] ?? []),
            'suites'  => array_values($d['suites'] ?? []),
        ];
        // 过滤掉非法包名，避免把脏数据带进构建参数
        $out['presets'] = array_values(array_filter($out['presets'], static function ($p) {
            return isset($p['n']) && preg_match('/^[A-Za-z0-9][A-Za-z0-9._+\-]{0,79}$/', (string) $p['n']);
        }));
        return self::$cache = $out;
    }

    public static function presets(): array
    {
        return self::load()['presets'];
    }

    public static function cats(): array
    {
        return self::load()['cats'];
    }

    public static function suites(): array
    {
        return self::load()['suites'];
    }

    /** 全部合法包名集合（用于校验前端提交的 packages）。 */
    public static function allowedNames(): array
    {
        $s = [];
        foreach (self::presets() as $p) {
            $s[(string) $p['n']] = true;
        }
        foreach (self::suites() as $su) {
            foreach (($su['pkgs'] ?? []) as $n) {
                $s[(string) $n] = true;
            }
        }
        return array_keys($s);
    }

    /** 按分类分组，供页面渲染。 */
    public static function grouped(): array
    {
        $out = [];
        foreach (self::cats() as $c) {
            $out[(string) ($c['k'] ?? '')] = [
                'label'  => (string) ($c['l'] ?? $c['k'] ?? ''),
                'items'  => [],
            ];
        }
        foreach (self::presets() as $p) {
            $k = (string) ($p['c'] ?? 'other');
            $out[$k] ??= ['label' => $k, 'items' => []];
            $out[$k]['items'][] = $p;
        }
        return $out;
    }
}
