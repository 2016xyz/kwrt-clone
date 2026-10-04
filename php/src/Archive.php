<?php
declare(strict_types=1);

namespace Kwrt;

/**
 * 用户上传归档的安全解压（Tar-Slip / Zip-Slip 防护）。
 *
 * 为什么必须存在（真实漏洞）：
 *   Engine::injectDefaults() 原来是
 *       $this->runCmd(['tar', '-xzf', $real, '-C', $ib . '/files'], $ib);
 *   —— 对用户上传的压缩包**不做任何条目校验**。赞助用户只要上传一个内含
 *   `../../../../root/.ssh/authorized_keys` 或 `link -> /` + `link/x` 的
 *   tar.gz，就能写到 ImageBuilder 目录之外，构建机上是任意文件写入。
 *   同一份代码在 Python 侧有 builder._safe_extract()，PHP 侧从来没补上；
 *   而且 `tar -xzf` 只认 gzip，可上传白名单里却写着 .zip / .7z —— 
 *   那两种格式在 PHP 后端必然解压失败，失败还被静默忽略。
 *
 * 判据（与 Python 版 builder._safe_extract 一致）：
 *   · 成员名不得是绝对路径、不得含 `..` 段、不得含 NUL；
 *   · 不得含符号链接 / 硬链接 / 设备文件（借链接跳出目标是经典绕过）；
 *   · 先**列清单校验**，通过后才解压 —— 绝不「先解压再检查」，
 *     那时越界文件已经落盘了。
 */
final class Archive
{
    /** 允许解压的扩展名（与上传白名单一致）。 */
    public const EXTS = ['.zip', '.7z', '.tar.gz', '.tgz', '.tar'];

    /**
     * 把一个用户上传的归档安全解压到 $dest。
     *
     * @throws \RuntimeException 归档不可用、含越界条目、或解压工具缺失
     * @return int 解压出的文件数（zip 由 safeUnzip 返回；tar/7z 返回 0）
     */
    public static function safeExtract(string $archive, string $dest): int
    {
        if (!is_file($archive)) {
            throw new \RuntimeException('文件包不存在或已被清理');
        }
        $base = realpath($dest);
        if ($base === false) {
            if (!@mkdir($dest, 0755, true) && !is_dir($dest)) {
                throw new \RuntimeException('目标目录不可用');
            }
            $base = realpath($dest);
        }
        if ($base === false) {
            throw new \RuntimeException('目标目录不可用');
        }

        $lower = strtolower($archive);
        if (str_ends_with($lower, '.zip')) {
            // 复用 Github 里已经写好、已被线上验证过的 zip 安全解压
            return Github::safeUnzip($archive, $base);
        }
        if (str_ends_with($lower, '.7z')) {
            self::safeExtract7z($archive, $base);
            return 0;
        }
        self::safeExtractTar($archive, $base);
        return 0;
    }

    /** 本机是否有可用的 7z（上传入口据此提前拒绝 .7z，而不是等构建才失败）。 */
    public static function sevenzipAvailable(): bool
    {
        foreach (['7z', '7za', '7zr'] as $cand) {
            if (self::which($cand) !== '') {
                return true;
            }
        }
        return false;
    }

    /** 判断一个成员名是否越界（绝对路径 / `..` 段 / NUL）。 */
    public static function badName(string $name): bool
    {
        if ($name === '' || str_contains($name, "\0")) {
            return true;
        }
        if (str_starts_with($name, '/') || str_starts_with($name, '\\')) {
            return true;
        }
        if (preg_match('#^[A-Za-z]:#', $name)) {          // Windows 盘符
            return true;
        }
        $parts = preg_split('#[/\\\\]#', $name) ?: [];
        return in_array('..', $parts, true);
    }

    /** 链接目标是否仍在 $dest 之内。 */
    private static function linkInside(string $base, string $member, string $link): bool
    {
        if (str_starts_with($link, '/') || preg_match('#^[A-Za-z]:#', $link)) {
            return false;
        }
        $dir = dirname($member);
        $joined = $dir === '.' ? $link : $dir . '/' . $link;
        $real = realpath($base . '/' . $joined);
        $probe = $real !== false ? $real : $base . '/' . self::normpath($joined);
        return str_starts_with($probe, $base . '/') || $probe === $base;
    }

    private static function normpath(string $p): string
    {
        $out = [];
        foreach (preg_split('#[/\\\\]#', $p) ?: [] as $seg) {
            if ($seg === '' || $seg === '.') {
                continue;
            }
            if ($seg === '..') {
                array_pop($out);
                continue;
            }
            $out[] = $seg;
        }
        return implode('/', $out);
    }

    /** 跑一条命令并返回 stdout 行数组；非 0 退出视为失败。 */
    private static function lines(array $argv): array
    {
        if (!is_callable('proc_open')) {
            throw new \RuntimeException('本机禁用了 proc_open，无法解压文件包');
        }
        $spec = [0 => ['file', '/dev/null', 'r'], 1 => ['pipe', 'w'], 2 => ['pipe', 'w']];
        $p = @proc_open($argv, $spec, $pipes);
        if (!is_resource($p)) {
            throw new \RuntimeException('无法执行 ' . ($argv[0] ?? '?') . '（未安装或不可执行）');
        }
        $out = stream_get_contents($pipes[1]);
        $err = stream_get_contents($pipes[2]);
        fclose($pipes[1]);
        fclose($pipes[2]);
        $code = proc_close($p);
        if ($code !== 0) {
            throw new \RuntimeException(
                trim((string) $err) !== '' ? trim((string) $err) : ('退出码 ' . $code));
        }
        return preg_split("/\r?\n/", (string) $out) ?: [];
    }

    private static function which(string $bin): string
    {
        foreach (explode(PATH_SEPARATOR, (string) getenv('PATH')) as $dir) {
            if ($dir !== '' && is_executable($dir . '/' . $bin)) {
                return $dir . '/' . $bin;
            }
        }
        return '';
    }

    /**
     * tar.gz / tgz / tar：先用 `tar -tzf` 列成员名校验，再用 `tar -tvf` 查类型，
     * 全部通过后才解压。
     */
    private static function safeExtractTar(string $archive, string $base): void
    {
        $tar = self::which('tar');
        if ($tar === '') {
            throw new \RuntimeException('服务器未安装 tar，无法解压文件包');
        }
        $lower = strtolower($archive);
        $isPlain = str_ends_with($lower, '.tar');
        // ★ 必须显式给出两个不同的 flag，不能靠 str_replace 从 -tzf 推导 -tvf：
        //   '-tzf' 里并不含子串 '-tf'（是 - t z f），替换会静默不生效，
        //   于是类型检查那一步变成拿名字列表当类型列表看，恒不命中 ——
        //   符号链接成员就会被放过去。（自测抓到的：tar.gz 符号链接用例失败）
        $listFlag = $isPlain ? '-tf' : '-tzf';
        $verbFlag = $isPlain ? '-tvf' : '-tzvf';
        $xFlag = $isPlain ? '-xf' : '-xzf';

        foreach (self::lines([$tar, $listFlag, $archive]) as $nm) {
            if ($nm === '') {
                continue;
            }
            if (self::badName($nm)) {
                throw new \RuntimeException('归档包含越界路径: ' . $nm);
            }
        }
        // 类型检查：l=符号链接 h=硬链接 b/c=设备 p=管道 s=socket
        foreach (self::lines([$tar, $verbFlag, $archive]) as $line) {
            if ($line === '') {
                continue;
            }
            $t = $line[0];
            if (in_array($t, ['l', 'h', 'b', 'c', 'p', 's'], true)) {
                throw new \RuntimeException('归档包含链接或设备文件，已拒绝: '
                    . substr($line, 0, 120));
            }
            if (preg_match('#(^|[/\\\\])\.\.([/\\\\]|$)#', $line)) {
                throw new \RuntimeException('归档包含越界路径，已拒绝: ' . substr($line, 0, 120));
            }
        }
        self::lines(array_merge(
            [$tar, $xFlag, $archive],
            ['--no-same-owner', '--no-overwrite-dir', '-C', $base]));
    }

    /** 7z：先 `7z l -slt` 列清单校验，再 `7z x`。 */
    private static function safeExtract7z(string $archive, string $base): void
    {
        $exe = '';
        foreach (['7z', '7za', '7zr'] as $cand) {
            $exe = self::which($cand);
            if ($exe !== '') {
                break;
            }
        }
        if ($exe === '') {
            throw new \RuntimeException('服务器未安装 7z，无法解压 .7z 文件包（请改用 .zip / .tar.gz）');
        }
        $cur = null;
        $recs = [];
        foreach (self::lines([$exe, 'l', '-slt', $archive]) as $line) {
            if (str_starts_with($line, 'Path = ')) {
                if ($cur !== null) {
                    $recs[] = $cur;
                }
                $cur = ['path' => substr($line, 7), 'link' => ''];
            } elseif ($cur !== null && str_contains($line, 'Link = ')) {
                $cur['link'] = trim(explode('=', $line, 2)[1] ?? '');
            }
        }
        if ($cur !== null) {
            $recs[] = $cur;
        }
        $self = realpath($archive);
        foreach ($recs as $r) {
            $nm = $r['path'];
            if ($nm === '' || realpath($nm) === $self) {
                continue;                                   // 归档自身那条记录
            }
            if (self::badName($nm)) {
                throw new \RuntimeException('归档包含越界路径: ' . $nm);
            }
            if ($r['link'] !== '' && !self::linkInside($base, $nm, $r['link'])) {
                throw new \RuntimeException('归档包含越界链接: ' . $nm . ' -> ' . $r['link']);
            }
        }
        self::lines([$exe, 'x', '-y', '-o' . $base, $archive]);
    }
}
