<?php
/**
 * 本地构建物盘点 / 删除 / 孤儿清理。
 *
 * 「孤儿」= store/ 下有目录，但 builds 表里已没有对应记录（重启或历史清理后常见）。
 * 这类目录不在任何列表里、会一直占盘，所以单独识别并可一键清理。
 *
 * ★ 安全边界：本模块**绝不触碰 store/uploads/** —— 那是用户上传区，
 *   误删会影响用户数据。所有操作都要求 hash 通过白名单，并做目录归属校验。
 */
declare(strict_types=1);

namespace Kwrt;

final class Artifacts
{
    /** 永远不参与清理的目录名。 */
    private const PROTECTED = ['uploads'];

    /**
     * 盘点：按构建分组，标出孤儿。
     * @return array{items:array,total_bytes:int,total_files:int,orphan_bytes:int,
     *               orphan_count:int,uploads:array,store_bytes:int,by_hash:array}
     */
    public static function scan(): array
    {
        $store = Config::path('store');
        $items = [];
        $byHash = [];
        $totalBytes = 0;
        $totalFiles = 0;
        $orphanBytes = 0;
        $orphanCount = 0;

        if (is_dir($store)) {
            foreach (scandir($store) ?: [] as $d) {
                if ($d === '.' || $d === '..' || in_array($d, self::PROTECTED, true)) {
                    continue;
                }
                // 目录名必须是合法 hash，否则不认（避免把杂物当构建物删掉）
                if (!preg_match(Util::HASH_RE, $d)) {
                    continue;
                }
                $dir = $store . DIRECTORY_SEPARATOR . $d;
                if (!is_dir($dir)) {
                    continue;
                }
                $st = Util::dirStat($dir);
                $b = Db::one('SELECT username, target, profile, status, created FROM builds '
                    . 'WHERE request_hash=?', [$d]);
                $orphan = $b === null;
                if ($orphan) {
                    $orphanBytes += $st['bytes'];
                    $orphanCount++;
                }
                $totalBytes += $st['bytes'];
                $totalFiles += $st['files'];
                $byHash[$d] = ['bytes' => $st['bytes'], 'files' => $st['files']];
                $items[] = [
                    'request_hash' => $d,
                    'bytes' => $st['bytes'],
                    'files' => $st['files'],
                    'mtime' => $st['mtime'],
                    'orphan' => $orphan,
                    'username' => $b['username'] ?? '',
                    'target' => $b['target'] ?? '',
                    'profile' => $b['profile'] ?? '',
                    'status' => $b['status'] ?? '',
                ];
            }
        }
        usort($items, static fn($a, $b) => $b['bytes'] <=> $a['bytes']);

        return [
            'items' => $items,
            'total_bytes' => $totalBytes,
            'total_files' => $totalFiles,
            'orphan_bytes' => $orphanBytes,
            'orphan_count' => $orphanCount,
            'uploads' => self::uploadsScan(),
            'store_bytes' => Util::dirStat($store)['bytes'],
            'by_hash' => $byHash,
            'protected' => self::PROTECTED,
        ];
    }

    /** 用户上传区（只读展示，不提供删除入口）。 */
    public static function uploadsScan(): array
    {
        $base = Config::path('store', 'uploads');
        $st = Util::dirStat($base);
        $staged = 0;
        $sd = $base . DIRECTORY_SEPARATOR . '_staged';
        if (is_dir($sd)) {
            foreach (scandir($sd) ?: [] as $u) {
                if ($u !== '.' && $u !== '..') {
                    $staged++;
                }
            }
        }
        return ['bytes' => $st['bytes'], 'files' => $st['files'], 'staged' => $staged];
    }

    /** 目录归属校验：必须是 store 下的合法 hash 目录，且不是受保护目录。 */
    private static function safeDir(string $hash): ?string
    {
        if (!preg_match(Util::HASH_RE, $hash) || in_array($hash, self::PROTECTED, true)) {
            return null;
        }
        $store = Config::path('store');
        $dir = $store . DIRECTORY_SEPARATOR . $hash;
        if (!is_dir($dir)) {
            return null;
        }
        $real = realpath($dir);
        $rstore = realpath($store);
        if ($real === false || $rstore === false) {
            return null;
        }
        // realpath 归属校验：确保真的在 store 之内（挡符号链接逃逸）
        if (!str_starts_with($real, rtrim($rstore, DIRECTORY_SEPARATOR) . DIRECTORY_SEPARATOR)) {
            return null;
        }
        return $real;
    }

    /** 删除某个构建的产物。dropRecord=true 时连记录一起删。 */
    public static function delete(string $hash, bool $dropRecord = false): array
    {
        $dir = self::safeDir($hash);
        if ($dir === null) {
            return ['freed' => 0, 'files' => 0, 'revoked_tokens' => 0, 'removed' => false,
                    'record_dropped' => false, 'detail' => '目录不存在或不允许删除'];
        }
        $st = Util::dirStat($dir);
        $ok = Util::rrmdir($dir);
        $revoked = Download::revokeBuild($hash);
        $dropped = false;
        if ($dropRecord) {
            Db::run('DELETE FROM builds WHERE request_hash=?', [$hash]);
            $dropped = true;
        }
        return ['freed' => $ok ? $st['bytes'] : 0, 'files' => $st['files'],
                'revoked_tokens' => $revoked, 'removed' => $ok, 'record_dropped' => $dropped,
                'detail' => ''];
    }

    /** 清理全部孤儿目录。 */
    public static function deleteOrphans(): array
    {
        $scan = self::scan();
        $removed = 0;
        $freed = 0;
        $removedHash = [];
        foreach ($scan['items'] as $it) {
            if (!$it['orphan']) {
                continue;
            }
            $r = self::delete((string) $it['request_hash'], false);
            if ($r['removed']) {
                $removed++;
                $freed += $r['freed'];
                $removedHash[] = $it['request_hash'];
            }
        }
        return ['removed' => $removed, 'freed' => $freed, 'hashes' => $removedHash];
    }

    /** 清空全部产物（keepUploads 默认保留用户上传）。 */
    public static function purgeAll(bool $keepUploads = true): array
    {
        $scan = self::scan();
        $removed = 0;
        $freed = 0;
        foreach ($scan['items'] as $it) {
            $r = self::delete((string) $it['request_hash'], false);
            if ($r['removed']) {
                $removed++;
                $freed += $r['freed'];
            }
        }
        return ['removed' => $removed, 'freed' => $freed,
                'uploads_kept' => $keepUploads];
    }
}
