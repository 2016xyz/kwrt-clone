<?php
declare(strict_types=1);

namespace Kwrt;

/**
 * 版本号 —— 与 Python 版同源。
 *
 * ★ 真源是**仓库根的 VERSION 文件**：Python 读 app/version.py 的常量，
 *   PHP 读这个文件。两者若各写一份常量，早晚会跑偏，
 *   而「后台显示的版本号」一旦不可信，排障时会把人带进沟里。
 *   为稳妥起见：以 VERSION 文件为准；文件缺失时回落到下面的常量，
 *   并有 tests 断言两者一致。
 */
final class Version
{
    /** 回落常量（仅当 VERSION 文件不可读时使用） */
    private const FALLBACK = '1.0.2';

    public static function raw(): string
    {
        static $cached = null;
        if ($cached !== null) {
            return $cached;
        }
        $file = Config::root() . '/VERSION';
        if (is_file($file)) {
            $v = trim((string) @file_get_contents($file));
            // 只接受 数字.数字[.数字] 形态，防止文件被写坏后污染页脚
            if (preg_match('/^\d+(\.\d+){0,2}$/', $v)) {
                return $cached = $v;
            }
        }
        return $cached = self::FALLBACK;
    }

    /** 展示用（与 Python 的 version.display() 同格式） */
    public static function display(): string
    {
        return 'v' . self::raw();
    }
}
