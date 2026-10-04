<?php
/**
 * 本机构建引擎：下载 OpenWrt ImageBuilder → make image → 收集产物 → 签发下载令牌。
 *
 * 对应 Python 版 app/builder.py 的 build() 流程。所有外部命令都以**参数数组**
 * 形式传入 proc_open（不拼 shell 字符串），并复用 Util 的白名单校验 ——
 * target/profile/version 在入口已过白名单，这里再用 escapeshellarg 做第二道。
 */
declare(strict_types=1);

namespace Kwrt;

final class Engine
{
    private const IB_MIRRORS = [
        'https://downloads.openwrt.org/releases/{version}/targets/{target}/',
        'https://mirror.sjtu.edu.cn/openwrt/releases/{version}/targets/{target}/',
        'https://mirrors.aliyun.com/openwrt/releases/{version}/targets/{target}/',
    ];

    public function __construct(
        private string $hash,
        private array $payload,
        private \Closure $progress
    ) {
    }

    /** 过程日志：写到 work/<hash>/engine.log。构建目录被清理后不会再有用，
     *  但排查「明明跑了却没产物」这类问题时，它是唯一的线索。 */
    private function log(string $msg): void
    {
        $f = Config::path('work', $this->hash, 'engine.log');
        @mkdir(dirname($f), 0755, true);
        @file_put_contents($f, '[' . date('H:i:s') . '] ' . $msg . "\n", FILE_APPEND);
    }

    /** 本次构建的起始时间（用于邮件里的耗时）。 */
    private float $t0 = 0.0;

    public function run(): void
    {
        $this->t0 = microtime(true);
        $target = Util::checkTarget((string) ($this->payload['target'] ?? ''));
        $profile = Util::checkProfile((string) ($this->payload['profile'] ?? ''));
        $version = Util::checkVersion((string) ($this->payload['version'] ?? '25.12'));
        $packages = Util::packages((array) ($this->payload['packages'] ?? []));
        $rootfs = (int) ($this->payload['rootfs_size_mb'] ?? 512);
        $fs = (string) ($this->payload['filesystem'] ?? 'squashfs');

        $work = Builder::workDir($this->hash);
        $store = Builder::storeDir($this->hash);
        $this->log("start work={$work} store={$store}");
        Util::rrmdir($work);
        if (!@mkdir($work, 0755, true) && !is_dir($work)) {
            throw new \RuntimeException("无法创建工作目录: {$work}");
        }
        if (!@mkdir($store, 0755, true) && !is_dir($store)) {
            throw new \RuntimeException("无法创建产物目录: {$store}");
        }
        $this->log('dirs ok, work_exists=' . (is_dir($work) ? '1' : '0')
            . ' store_exists=' . (is_dir($store) ? '1' : '0'));

        // 1) 找到 ImageBuilder 压缩包（本地缓存优先）
        ($this->progress)('running', '准备 ImageBuilder');
        $ibUrl = str_replace(['{version}', '{target}'], [$version, $target], self::IB_MIRRORS[0]);
        $ibName = 'openwrt-imagebuilder-' . str_replace('/', '-', $target) . '.Linux-x86_64';
        $cacheDir = Config::path('cache');
        @mkdir($cacheDir, 0755, true);

        $tarball = $this->fetchImageBuilder($ibUrl, $ibName, $cacheDir);
        if ($tarball === null) {
            throw new \RuntimeException('无法下载 ImageBuilder（已尝试多个镜像）');
        }

        // 2) 解压
        ($this->progress)('running', '解压 ImageBuilder');
        $this->runCmd(['tar', '--zstd', '-xf', $tarball, '-C', $work], $work);
        // 优先按约定名精确找，找不到再扫 Makefile 兜底
        $exact = $work . '/' . self::ibBaseName((string) $this->payload['version'],
                                                (string) $this->payload['target']);
        $ib = (is_dir($exact) && is_file($exact . '/Makefile')) ? $exact : $this->findDir($work);
        if ($ib === null) {
            throw new \RuntimeException('解压后未找到 ImageBuilder 目录');
        }

        // 3) 注入 uci-defaults（自定义主机名 / LAN IP / 网络开关）
        $this->injectDefaults($ib);

        // 3.5) 接入第三方插件源（luci-app-openclash 这类不在官方仓库里，
        //      只有 kiddin9 feed 提供；apk 后端跳过，因为上游是 .ipk 格式）
        $this->ensureRepositories($ib);

        // 4) 组装 make 参数
        ($this->progress)('running', '开始编译（make image）');
        $make = ['make', '-C', $ib, 'image', 'PROFILE=' . $profile];
        if ($packages) {
            $make[] = 'PACKAGES=' . implode(' ', $packages);
        }
        if ($fs === 'ext4') {
            $make[] = 'CONFIG_TARGET_ROOTFS_EXT4FS=y';
        }
        $filesDir = $ib . '/files';
        if (is_dir($filesDir)) {
            $make[] = 'FILES=' . $filesDir;
        }

        $log = $store . '/build.log';
        $code = $this->runCmd($make, $ib, $log);
        if ($code !== 0) {
            throw new \RuntimeException('编译失败（make 退出码 ' . $code . '），详见 build.log');
        }

        // 5) 收集产物
        ($this->progress)('running', '收集产物');
        $binDir = $ib . '/bin/targets/' . $target;
        $this->log("collect binDir={$binDir} exists=" . (is_dir($binDir) ? '1' : '0'));
        if (!is_dir($binDir)) {
            // 把 bin/ 下真实存在的结构记下来，便于定位命名/路径不一致
            $alt = $ib . '/bin';
            $this->log('bin 下实际内容: ' . (is_dir($alt)
                ? implode(', ', array_slice(scandir($alt) ?: [], 2, 10)) : '(无 bin 目录)'));
        }
        $n = $this->collect($binDir, $store);
        $this->log("collect 结果 n={$n}");

        // 6) 签发下载令牌 + 通知
        //    ★ 收尾统一走 Notifier —— 本地与远端后端必须共用同一套
        //      （签发令牌口径、外链基址、下载次数上限、邮件开关都在那里），
        //      两边各写一份的话迟早只改一边。
        ($this->progress)('running', '签发下载链接');
        $files = [];
        foreach ($this->files($store) as $f) {
            $files[] = ['name' => $f, 'size' => (int) (filesize($store . '/' . $f) ?: 0)];
        }
        $info = Notifier::buildDone($this->hash, $this->payload, $files,
            microtime(true) - $this->t0);
        $links = $info['links'];
        $this->log('收尾：本地产物 ' . $info['local'] . ' 个 / 外链 ' . $info['external']
            . ' 个；邮件 ' . (string) ($info['mail']['detail'] ?? ''));
        ($this->progress)('done', '构建完成，共 ' . $n . ' 个文件'
            . ($info['ttl_hours'] > 0 ? "（下载链接 {$info['ttl_hours']} 小时内有效）" : ''));
        $this->log("done n={$n} links=" . count($links) . " —— 保留 work/ 供排查");
        // 产物已收集完成，工作目录可以清理；但收集数为 0 时**保留**它，
        // 否则「构建成功却没有产物」这种情况会连现场一起丢掉，无从排查。
        // 排障用的开关，走环境变量而不是设置项 —— 它不属于业务配置，
        // 放进 settings 会让人以为后台能改（而且 schema 里本来也没有这个键）。
        $keepWorkdir = getenv('KWRT_KEEP_WORKDIR') === '1';
        if ($n > 0 && !$keepWorkdir) {
            Util::rrmdir($work);
            $this->log('work 已清理');
        } else {
            $this->log('收集为 0，保留 work/ 以便排查');
        }
    }

    // ---------------------------------------------------------------- 内部

    /**
     * 命名规则必须与 Python 版**逐字一致**，否则两边各下一份、缓存互不命中：
     *     openwrt-imagebuilder-{version}-{target-with-dashes}.Linux-x86_64.tar.zst
     * （曾漏掉版本号，导致前缀永远匹配不上、每次都重新下载 46 MB。）
     */
    private static function ibBaseName(string $version, string $target): string
    {
        return 'openwrt-imagebuilder-' . $version . '-' . str_replace('/', '-', $target)
            . '.Linux-x86_64';
    }

    private function fetchImageBuilder(string $url, string $ibName, string $cacheDir): ?string
    {
        $target = (string) $this->payload['target'];
        $version = (string) $this->payload['version'];
        $prefix = self::ibBaseName($version, $target);
        $hit = null;
        foreach (scandir($cacheDir) ?: [] as $f) {
            if (str_starts_with($f, $prefix) && str_ends_with($f, '.tar.zst')
                && filesize($cacheDir . '/' . $f) > 1024 * 1024) {
                $hit = $cacheDir . '/' . $f;
            }
        }
        if ($hit !== null) {
            return $hit;
        }
        foreach (self::IB_MIRRORS as $m) {
            $base = str_replace(['{version}', '{target}'], [$version, $target], $m);
            $idx = $this->get($base);
            if ($idx === null) {
                continue;
            }
            if (!preg_match('/href="([^"]*imagebuilder[^"]*\.tar\.zst)"/i', $idx, $mm)) {
                continue;
            }
            $file = basename($mm[1]);
            $dst = $cacheDir . '/' . $file;
            $data = $this->get($base . $file, 300);
            if ($data === null) {
                continue;
            }
            file_put_contents($dst, $data);
            return $dst;
        }
        return null;
    }

    private function get(string $url, int $timeout = 60): ?string
    {
        $ch = curl_init($url);
        curl_setopt_array($ch, [
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_FOLLOWLOCATION => true,
            CURLOPT_MAXREDIRS => 3,
            CURLOPT_TIMEOUT => $timeout,
            CURLOPT_USERAGENT => 'Kwrt-Builder-PHP/1.0',
        ]);
        $r = curl_exec($ch);
        $code = (int) curl_getinfo($ch, CURLINFO_HTTP_CODE);
        curl_close($ch);
        return ($r === false || $code !== 200) ? null : (string) $r;
    }

    private function findDir(string $base): ?string
    {
        foreach (scandir($base) ?: [] as $d) {
            if ($d === '.' || $d === '..') {
                continue;
            }
            $p = $base . '/' . $d;
            if (is_dir($p) && is_file($p . '/Makefile')) {
                return $p;
            }
        }
        return null;
    }

    /**
     * 接入第三方插件源，并放宽签名校验。
     *
     * 对应 Python 版 app/builder.py 的 ensure_repositories()。
     * 只有 opkg 后端能接（apk 版 ImageBuilder 用不了 .ipk 格式的上游 feed）。
     */
    private function ensureRepositories(string $ib): void
    {
        $target = (string) ($this->payload['target'] ?? '');
        $branchOrVer = (string) ($this->payload['version'] ?? '');
        $r = Releases::resolve($branchOrVer);
        $feeds = Releases::thirdPartyFeeds($r['branch'], Releases::arch($target), $r['backend']);
        $this->log('第三方源: backend=' . $r['backend'] . ' feeds=' . json_encode($feeds));
        if (!$feeds) {
            return;
        }
        $conf = $ib . '/repositories.conf';
        if (!is_file($conf)) {
            $this->log('未找到 repositories.conf，跳过第三方源');
            return;
        }
        $txt = (string) file_get_contents($conf);
        foreach ($feeds as $i => $f) {
            if (strpos($txt, $f) === false) {
                $txt .= 'src/gz kwrt_extra_' . $i . ' ' . $f . "\n";
            }
        }
        // 第三方源的包通常未按官方密钥签名，须关闭校验，否则 opkg 直接拒装
        $txt = str_replace('option check_signature', '# option check_signature', $txt);
        file_put_contents($conf, $txt);
        $this->log('已接入第三方源（签名校验已关闭）');
    }

    private function injectDefaults(string $ib): void
    {
        $dir = $ib . '/files/etc/uci-defaults';
        if (!is_dir($dir) && !@mkdir($dir, 0755, true) && !is_dir($dir)) {
            return;
        }
        $lines = ['#!/bin/sh'];
        if (($h = (string) ($this->payload['hostname'] ?? '')) !== '') {
            $lines[] = 'uci set system.@system[0].hostname=' . escapeshellarg($h);
        }
        if (!empty($this->payload['ipv6'])) {
            $lines[] = 'uci set network.lan.ip6assign=64';
        }
        if (empty($this->payload['dhcp'])) {
            $lines[] = 'uci set dhcp.lan.ignore=1';
        }
        $lines[] = 'uci commit';
        $lines[] = 'exit 0';
        file_put_contents($dir . '/99-kwrt', implode("\n", $lines) . "\n");
        chmod($dir . '/99-kwrt', 0755);

        // 自定义文件包：解压进 rootfs 覆盖层。
        // ★ 必须走 Archive::safeExtract —— 原来是裸
        //     $this->runCmd(['tar', '-xzf', $real, '-C', $ib . '/files'], $ib);
        //   既不校验任何归档条目（Tar-Slip：`../` 条目可写到 ImageBuilder 之外，
        //   配合符号链接成员更可跳到任意路径 —— 构建机任意文件写入），
        //   又只认 gzip（上传白名单里却写着 .zip / .7z，那两种必然解压失败），
        //   而失败返回值还被忽略 —— 用户以为文件进了固件，其实没有。
        //   Python 侧有 builder._safe_extract，这一侧一直没有。
        $fp = (string) ($this->payload['files_path'] ?? '');
        if ($fp !== '') {
            $src = Config::path('store', 'uploads', ltrim($fp, '/'));
            $real = Util::under(Config::path('store', 'uploads'), $src);
            if ($real === null || !is_file($real)) {
                throw new \RuntimeException('自定义文件包不存在或已被清理（'
                    . $fp . '），请重新上传后再构建');
            }
            $filesDir = $ib . '/files';
            if (!is_dir($filesDir) && !@mkdir($filesDir, 0755, true) && !is_dir($filesDir)) {
                throw new \RuntimeException('无法创建 files 目录');
            }
            try {
                Archive::safeExtract($real, $filesDir);
            } catch (\Throwable $e) {
                throw new \RuntimeException('自定义文件包解压失败：' . $e->getMessage());
            }
            $this->log('已解压自定义文件包: ' . basename($real));
        }
    }

    /**
     * 用 proc_open 以参数数组方式执行（不经过 shell，杜绝命令注入）。
     *
     * ★ 退出码必须从 **proc_get_status()** 里取，不能用 proc_close() 的返回值。
     *   PHP 的行为是：proc_get_status() 一旦读到进程结束，就把退出码**消费掉**，
     *   之后 proc_close() 返回 -1。
     *   实测后果很严重：编译其实**成功**了（都打印到 "Calculating checksums..."、
     *   8 个镜像文件都产出了），却因为拿到 -1 被判成「编译失败」——
     *   构建平台最怕的就是这种「明明成功却报失败」。
     */
    private function runCmd(array $argv, string $cwd, string $logFile = ''): int
    {
        $spec = [0 => ['file', '/dev/null', 'r'], 1 => ['pipe', 'w'], 2 => ['pipe', 'w']];
        $fh = $logFile !== '' ? fopen($logFile, 'ab') : null;
        // proc_open 在某些环境下确实会失败（如 exec 被禁用），要给出可辨的错误码
        $p = @proc_open($argv, $spec, $pipes, $cwd);
        if (!is_resource($p)) {
            if ($fh) {
                fwrite($fh, "[engine] 无法启动进程: " . implode(' ', $argv) . "\n");
                fclose($fh);
            }
            return 127;
        }
        stream_set_blocking($pipes[1], false);
        stream_set_blocking($pipes[2], false);
        $exit = null;
        $idle = 0;
        while (true) {
            $s = proc_get_status($p);
            $got = false;
            foreach ([1, 2] as $i) {
                $chunk = stream_get_contents($pipes[$i]);
                if ($chunk !== false && $chunk !== '') {
                    $got = true;
                    if ($fh) {
                        fwrite($fh, $chunk);
                    }
                }
            }
            if (!$s['running']) {
                $exit = (int) $s['exitcode'];
                break;
            }
            // 长时间无输出也要继续等（make 的某些阶段会静默很久）
            $idle = $got ? 0 : $idle + 1;
            usleep(200000);
        }
        foreach ([1, 2] as $i) {
            fclose($pipes[$i]);
        }
        if ($fh) {
            fclose($fh);
        }
        @proc_close($p);        // 只用于释放资源，返回值不可信
        return $exit ?? -1;
    }

    /** 把 bin/ 下的镜像文件搬到产物目录。 */
    private function collect(string $binDir, string $store): int
    {
        if (!is_dir($binDir)) {
            throw new \RuntimeException('未找到编译输出目录: ' . $binDir);
        }
        $n = 0;
        $it = new \RecursiveIteratorIterator(
            new \RecursiveDirectoryIterator($binDir, \FilesystemIterator::SKIP_DOTS)
        );
        foreach ($it as $f) {
            if (!$f->isFile()) {
                continue;
            }
            $name = $f->getBasename();
            if (!preg_match('/\.(img|gz|bin|itb|tar|zst|manifest|json|txt|sha256sums)$/i', $name)) {
                continue;
            }
            if (Util::safeFilename($name) === '') {
                continue;
            }
            if (copy($f->getPathname(), $store . '/' . $name)) {
                $n++;
            }
        }
        return $n;
    }

    private function files(string $store): array
    {
        $out = [];
        foreach (scandir($store) ?: [] as $f) {
            if ($f === '.' || $f === '..' || str_starts_with($f, '.') || $f === 'build.log') {
                continue;
            }
            if (is_file($store . '/' . $f) && Util::safeFilename($f) !== '') {
                $out[] = $f;
            }
        }
        sort($out);
        return $out;
    }
}
