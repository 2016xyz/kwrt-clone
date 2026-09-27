<?php
/**
 * GitHub Actions 构建后端 —— 与 Python 版 app/backends.py 的 GitHubBackend 同语义。
 *
 * 为什么要有这个文件
 * ------------------
 * PHP 版原先只做本地构建。两版共用同一份设置，于是 `builder.backend=github` 时
 * Python 会真的远端派发、PHP 却静默本地构建 —— 「同一个设置两种行为」。
 * 本文件把远端派发补齐，使两版行为一致。
 *
 * 附带好处：宝塔默认把 exec/proc_open 放进 disable_functions，本地构建起不来；
 * 改用远端后端后，PHP 侧不再需要起子进程（磁盘与 CPU 也不再是本站的事）。
 *
 * 与 Python 版的逐条对齐（不能只对齐「能跑」）：
 *   · 尊重 `gh.enabled` 总开关（两版都查它，否则开关形同虚设）；
 *   · 多队列 + 轮转（游标在 Queues::pick()，存 DB，多进程安全）；
 *   · workflow_dispatch 带 `return_run_details`，且该字段必须在**请求体**里
 *     —— 放 query GitHub 会视而不见并返回 204 空响应，拿不到精确 run_id；
 *   · 拿不到精确 run_id 时走启发式兜底：45 秒时间窗 + 同分支 + **认领互斥**；
 *   · 产物名按 `gh.artifact_pattern` 过滤（glob 语义）；
 *   · 302 第二跳必须去掉 Authorization 头（签名不匹配），且 Location 要过白名单
 *     （否则是 SSRF / 本地文件读取）；
 *   · 回传支持断点续传 + 产物身份指纹（防止把两个产物的分片拼成一个坏包）；
 *   · 完整性校验：有 Content-Length 就收满，没有就查 zip 的 EOCD 签名；
 *   · `gh.mirror_artifacts` 决定是否回传本站，`gh.mirror_timeout` 是回传总时限。
 */
declare(strict_types=1);

namespace Kwrt;

final class Github
{
    private const API = 'https://api.github.com';
    private const UA  = 'Kwrt-Builder/1.0';

    /** 允许的重定向目标主机（GitHub 产物 302 落到 Azure Blob）。 */
    private const REDIRECT_HOSTS = [
        'github.com', 'api.github.com', 'codeload.github.com',
        'objects.githubusercontent.com', 'github-releases.githubusercontent.com',
        'release-assets.githubusercontent.com',
    ];
    private const REDIRECT_SUFFIXES = [
        '.githubusercontent.com',
        '.blob.core.windows.net',
        '.githubassets.com',
    ];

    /** 轮询上限：与 Python 版一致（90 分钟）。 */
    private const RUN_TIMEOUT = 5400;

    // ------------------------------------------------------------------ 配置

    public static function enabled(): bool
    {
        return Settings::bool('gh.enabled', false);
    }

    /** 队列参数优先，缺项回落全局设置（与 Python 的 _cfg 同序）。 */
    private static function cfg(?array $q, string $key, string $def): string
    {
        $v = ($q !== null && array_key_exists($key, $q)) ? $q[$key] : Settings::get('gh.' . $key, $def);
        return trim((string) ($v ?? '')) !== '' ? trim((string) $v) : $def;
    }

    public static function available(?array $queue = null): bool
    {
        if (!self::enabled()) {
            return false;
        }
        return self::cfg($queue, 'repo', '') !== '' && self::cfg($queue, 'token', '') !== '';
    }

    /** 当前生效后端（供后台展示，与 Python 的 backends.info() 同结构）。 */
    public static function info(): array
    {
        $want = strtolower((string) Settings::get('builder.backend', 'local'));
        $ok = self::available();
        return [
            'queues'           => Queues::summary(),
            'queue_count'      => count(Queues::available()),
            'configured'       => $want,
            'effective'        => ($want === 'github' && $ok) ? 'github' : 'local',
            'github_available' => $ok,
            'local_available'  => true,
            'labels'           => [
                'local'  => '本机构建（ImageBuilder 实编）',
                'github' => 'GitHub Actions 远程构建',
            ],
        ];
    }

    // ------------------------------------------------------------------ 派发

    /**
     * 派发并**同步**等到构建完成（与 Python 版一致：worker 进程里串行完成）。
     *
     * @return array{status:string,files:array,stdout:string,stderr:string,detail:string,duration:float}
     */
    public static function dispatch(string $hash, array $req, callable $onProgress): array
    {
        $queue = Queues::pick();
        if (!self::available($queue)) {
            return ['status' => 'failed', 'files' => [], 'stdout' => '',
                    'stderr' => 'GitHub 后端未配置完整（需要 gh.enabled / repo / token）',
                    'detail' => 'gh-not-configured', 'duration' => 0.0];
        }
        $repo = self::cfg($queue, 'repo', '');
        $token = self::cfg($queue, 'token', '');
        $workflow = self::cfg($queue, 'workflow', 'build-firmware.yml');
        $ref = self::cfg($queue, 'ref', 'main');
        $t0 = microtime(true);

        // uci-defaults 脚本可能含换行，用 base64 传递（与 Python 一致）
        $defaultsRaw = (string) ($req['defaults'] ?? '');
        $inputs = [
            'target'        => (string) ($req['target'] ?? ''),
            'profile'       => (string) ($req['profile'] ?? ''),
            'packages'      => implode(' ', (array) ($req['packages'] ?? [])),
            'version'       => (string) ($req['version'] ?? ''),
            'defaults'      => $defaultsRaw !== '' ? base64_encode($defaultsRaw) : '',
            'filesystem'    => (string) ($req['filesystem'] ?? 'squashfs'),
            'rootfs_size_mb'=> (string) ($req['rootfs_size_mb'] ?? 512),
        ];

        $qname = (string) ($queue['name'] ?? '默认队列');
        $onProgress('running', "向 GitHub 派发构建：{$repo} / {$workflow}（队列：{$qname}）");

        // return_run_details 必须放**请求体**，放 query 会被忽略并返回 204
        [$st, $body] = self::request($token, 'POST',
            "/repos/{$repo}/actions/workflows/{$workflow}/dispatches",
            ['ref' => $ref, 'inputs' => $inputs, 'return_run_details' => true]);

        $exact = null;
        if (is_array($body)) {
            $exact = $body['workflow_run_id'] ?? $body['run_id'] ?? null;
        }
        if (!in_array($st, [200, 201, 204], true)) {
            $msg = is_array($body) ? (string) ($body['message'] ?? json_encode($body)) : (string) $body;
            return ['status' => 'failed', 'files' => [], 'stdout' => '',
                    'stderr' => "GitHub 派发失败 HTTP {$st}: {$msg}",
                    'detail' => 'gh-dispatch-failed', 'duration' => microtime(true) - $t0];
        }

        $onProgress('running', '已派发，等待 GitHub 受理…');
        $runId = $exact ? (int) $exact : self::awaitRun($token, $repo, $workflow, $ref, $onProgress);
        if (!$runId) {
            return ['status' => 'failed', 'files' => [], 'stdout' => '',
                    'stderr' => '派发成功但未找到对应的 workflow run（请稍后在 GitHub Actions 页面查看）',
                    'detail' => 'gh-run-not-found', 'duration' => microtime(true) - $t0];
        }

        $onProgress('running', "GitHub run #{$runId} 已启动，等待构建完成…");
        return self::awaitCompletion($token, $repo, $runId, $hash, $onProgress, $t0);
    }

    /**
     * 兜底：派发后 GitHub 需几秒创建 run，轮询找出来。
     *
     * 45 秒时间窗（原 90 秒过宽，会跨越上一次派发）+ 同分支 + **认领互斥**。
     * 认领必须落库：PHP 是多进程，内存集合跨不了进程 —— 不互斥的话两次并发
     * 派发会各自领走同一个 run，最终把 A 的固件发给 B（产物错配）。
     */
    private static function awaitRun(string $token, string $repo, string $workflow,
                                     string $ref, callable $onProgress, int $tries = 12): ?int
    {
        $since = time() - 45;
        for ($i = 0; $i < $tries; $i++) {
            [$st, $body] = self::request($token, 'GET',
                "/repos/{$repo}/actions/workflows/{$workflow}/runs?per_page=20");
            if ($st === 200 && is_array($body)) {
                $cands = [];
                foreach ((array) ($body['workflow_runs'] ?? []) as $run) {
                    $ts = self::isoTs((string) ($run['created_at'] ?? ''));
                    $rid = (int) ($run['id'] ?? 0);
                    if ($ts === 0 || $rid === 0 || $ts < $since) {
                        continue;
                    }
                    if ((string) ($run['head_branch'] ?? '') !== $ref) {
                        continue;
                    }
                    $cands[] = [$ts, $rid];
                }
                rsort($cands);                       // 最新优先
                foreach ($cands as [, $rid]) {
                    if (self::claimRun($rid)) {
                        return $rid;
                    }
                }
            }
            sleep(5);
        }
        return null;
    }

    /** 原子认领 run_id（跨进程）。已被认领返回 false。 */
    private static function claimRun(int $runId): bool
    {
        try {
            // ★ 表来自 Schema 真源（生成器会派生出 MySQL 版 DDL）；
            //   写入走 Db::insertIgnore —— 原来这里内联了
            //   `CREATE TABLE ... run_id INTEGER PRIMARY KEY` 与
            //   **`INSERT OR IGNORE`**：后者是 SQLite 专有语法，MySQL 下直接
            //   syntax error，被 catch 吞掉 → 认领永远失败 → 兜底查找 run
            //   在 MySQL 上静默失效（表现为「派发成功但未找到 workflow run」）。
            //   被 verify_mysql 的 M-10（方言专有写法只允许出现在 Db.php）抓到。
            Db::ensureTable('gh_run_claims');
            $n = Db::insertIgnore('gh_run_claims', [
                'run_id' => $runId, 'created' => microtime(true),
            ]);
            if ($n === 1) {
                // 防无限增长：老记录顺手清掉
                Db::run('DELETE FROM gh_run_claims WHERE created < ?', [microtime(true) - 86400]);
                return true;
            }
            return false;
        } catch (\Throwable) {
            return false;
        }
    }

    /** 轮询 run 直到结束，成功则取产物并按需回传。 */
    private static function awaitCompletion(string $token, string $repo, int $runId,
                                            string $hash, callable $onProgress, float $t0): array
    {
        $start = microtime(true);
        $last = '';
        while (microtime(true) - $start < self::RUN_TIMEOUT) {
            [$st, $run] = self::request($token, 'GET', "/repos/{$repo}/actions/runs/{$runId}");
            if ($st !== 200 || !is_array($run)) {
                sleep(8);
                continue;
            }
            $status = (string) ($run['status'] ?? '');
            $concl = (string) ($run['conclusion'] ?? '');
            $msg = "GitHub: {$status}" . ($concl !== '' ? " / {$concl}" : '');
            if ($msg !== $last) {
                $onProgress('running', $msg);
                $last = $msg;
            }

            if ($status === 'completed') {
                $url = (string) ($run['html_url'] ?? '');
                if ($concl !== 'success') {
                    return ['status' => 'failed', 'files' => [], 'stdout' => '',
                            'stderr' => self::failedLogs($token, $repo, $runId),
                            'detail' => 'gh-' . $concl, 'gh_run_url' => $url,
                            'duration' => microtime(true) - $t0];
                }
                $arts = self::artifacts($token, $repo, $runId);
                $onProgress('running', 'GitHub 构建完成，取得 ' . count($arts) . ' 个产物');
                $files = $arts;
                try {
                    $mirrored = self::mirrorArtifacts($token, $repo, $arts, $hash, $onProgress);
                    if ($mirrored) {
                        $files = $mirrored;
                    } else {
                        $onProgress('running', '产物未回传本站，直接使用 GitHub 产物地址');
                    }
                } catch (\Throwable $e) {
                    // 回传失败不影响构建结论，但要如实说
                    $onProgress('running', '产物回传异常（不影响构建结果）：' . get_class($e)
                        . ': ' . $e->getMessage());
                }
                return ['status' => 'done', 'files' => $files, 'stdout' => '', 'stderr' => '',
                        'gh_run_url' => $url, 'duration' => microtime(true) - $t0];
            }
            sleep(12);
        }
        return ['status' => 'failed', 'files' => [], 'stdout' => '',
                'stderr' => 'GitHub 构建超时（>' . self::RUN_TIMEOUT . 's）',
                'detail' => 'gh-timeout', 'duration' => microtime(true) - $t0];
    }

    /** 产物清单（按 gh.artifact_pattern 过滤）。 */
    private static function artifacts(string $token, string $repo, int $runId): array
    {
        [$st, $body] = self::request($token, 'GET',
            "/repos/{$repo}/actions/runs/{$runId}/artifacts");
        $out = [];
        if ($st === 200 && is_array($body)) {
            foreach ((array) ($body['artifacts'] ?? []) as $a) {
                if (!empty($a['expired'])) {
                    continue;
                }
                if (!self::artifactMatch((string) ($a['name'] ?? ''))) {
                    continue;
                }
                $out[] = [
                    'id'       => (int) ($a['id'] ?? 0),
                    'name'     => (string) ($a['name'] ?? 'artifact'),
                    'size'     => (int) ($a['size_in_bytes'] ?? 0),
                    'sha256'   => '',
                    'url'      => (string) ($a['archive_download_url'] ?? ''),
                    'expired'  => false,
                    'external' => true,
                ];
            }
        }
        return $out;
    }

    /** 产物名匹配：gh.artifact_pattern（glob）。空或 * 表示不过滤。 */
    public static function artifactMatch(string $name): bool
    {
        $pat = trim((string) Settings::get('gh.artifact_pattern', 'openwrt-*'));
        if ($pat === '' || $pat === '*') {
            return true;
        }
        return fnmatch($pat, $name);
    }

    /** 失败时抓 job/step 摘要（比让用户去 Actions 翻日志友好）。 */
    private static function failedLogs(string $token, string $repo, int $runId, int $max = 80): string
    {
        [$st, $body] = self::request($token, 'GET', "/repos/{$repo}/actions/runs/{$runId}/jobs");
        $lines = [];
        if ($st === 200 && is_array($body)) {
            foreach ((array) ($body['jobs'] ?? []) as $job) {
                foreach ((array) ($job['steps'] ?? []) as $step) {
                    if ((string) ($step['conclusion'] ?? '') === 'failure') {
                        $lines[] = '✗ ' . ($job['name'] ?? '') . ' / ' . ($step['name'] ?? '');
                    }
                }
                if ((string) ($job['conclusion'] ?? '') === 'failure') {
                    $lines[] = 'job 失败: ' . ($job['name'] ?? '') . ' — ' . ($job['html_url'] ?? '');
                }
            }
        }
        if (!$lines) {
            $lines = ['GitHub 构建失败，请到 Actions 页面查看完整日志。'];
        }
        return implode("\n", array_slice($lines, 0, $max));
    }

    // ------------------------------------------------------------------ 产物回传

    /**
     * 把 GitHub 产物拉回 store/<hash>/ 并解压，返回本地文件清单。
     *
     * 这样「限时下载令牌」机制对两种后端一致生效。关闭或被要求不回传时返回 null，
     * 由上层回退为直接暴露 GitHub 产物地址。
     */
    public static function mirrorArtifacts(string $token, string $repo, array $arts,
                                           string $hash, callable $onProgress): ?array
    {
        if (!$arts || $hash === '' || !preg_match(Util::HASH_RE, $hash)) {
            return null;
        }
        if (!Settings::bool('gh.mirror_artifacts', true)) {
            return null;
        }
        $timeout = max(60, (int) Settings::get('gh.mirror_timeout', 3600));
        $dest = Config::path('store') . '/' . $hash;
        if (!is_dir($dest) && !@mkdir($dest, 0755, true) && !is_dir($dest)) {
            return null;
        }
        $deadline = microtime(true) + $timeout;

        foreach ($arts as $a) {
            $aid = (int) ($a['id'] ?? 0);
            if ($aid === 0) {
                continue;
            }
            // 产物名来自 GitHub，不能直接拼路径
            $safe = Util::safeFilename((string) ($a['name'] ?? 'artifact'));
            if ($safe === '') {
                $safe = 'artifact';
            }
            $mb = ((int) ($a['size'] ?? 0)) / 1048576;
            $onProgress('running', sprintf('回传产物「%s」(%.0f MB)…', $safe, $mb));

            $zip = $dest . '/.' . substr($safe, 0, 100) . '.zip';
            [$ok, $msg] = self::downloadArtifact($token, $repo, $aid, $zip, $deadline);
            if (!$ok || !is_file($zip) || filesize($zip) === 0) {
                $onProgress('running', "产物回传失败 {$safe}: {$msg}");
                @unlink($zip);
                continue;
            }
            try {
                self::safeUnzip($zip, $dest);
                @unlink($zip);
            } catch (\Throwable $e) {
                $onProgress('running', "产物解压失败 {$safe}: " . $e->getMessage());
                @unlink($zip);
            }
        }

        $out = [];
        foreach ((array) (scandir($dest) ?: []) as $fn) {
            $fp = $dest . '/' . $fn;
            if ($fn === '.' || $fn === '..' || !is_file($fp)) {
                continue;
            }
            $out[] = ['name' => $fn, 'size' => (int) filesize($fp),
                      'sha256' => (string) hash_file('sha256', $fp), 'external' => true];
        }
        if ($out) {
            $onProgress('running', '已回传 ' . count($out) . ' 个产物到本站存储');
        }
        return $out ?: null;
    }

    /**
     * 下载单个产物归档（断点续传 + 完整性校验）。
     *
     * 两个坑：
     *   1. archive_download_url 会 302 到预签名地址，**第二跳不能带 Authorization**
     *      （带上会让签名不匹配）；
     *   2. 不能用固定 socket 超时 —— 产物上百 MB，慢链路下会误判失败，
     *      而服务端仍在发数据，最终表现为 BrokenPipeError、整包白下。
     *
     * @return array{0:bool,1:string} [是否成功, 说明]
     */
    public static function downloadArtifact(string $token, string $repo, int $aid,
                                            string $destZip, float $deadline): array
    {
        $url = self::API . "/repos/{$repo}/actions/artifacts/{$aid}/zip";
        [$code, $loc] = self::headRedirect($token, $url);
        if ($loc === '') {
            return [false, "未取得重定向地址（HTTP {$code}）"];
        }
        [$okRedir, $why] = self::validateRedirect($loc);
        if (!$okRedir) {
            return [false, "重定向被拒绝（{$why}）"];
        }

        // 产物身份指纹：续传时若指纹变了，说明残留分片属于**另一个产物**，
        // 继续追加会拼出损坏的 zip —— 此时丢弃重下。
        $fpPath = $destZip . '.fp';
        $ident = self::artifactIdent($token, $repo, $aid);
        $lastErr = '';

        for ($attempt = 1; $attempt <= 3; $attempt++) {
            if ($deadline > 0 && microtime(true) >= $deadline) {
                return [false, '回传超时（可调大 gh.mirror_timeout）'];
            }
            $have = is_file($destZip) ? (int) filesize($destZip) : 0;
            if ($have > 0) {
                $prev = is_file($fpPath) ? trim((string) @file_get_contents($fpPath)) : '';
                if ($prev !== $ident) {
                    @unlink($destZip);
                    $have = 0;
                }
            }
            @file_put_contents($fpPath, $ident);

            $remain = $deadline > 0 ? max(60, min(300, (int) ($deadline - microtime(true)))) : 300;
            [$ok, $info] = self::httpDownload($loc, $destZip, $have, $remain);
            if (!$ok) {
                $lastErr = $info;
                sleep(min(5 * $attempt, 15));
                continue;
            }
            // 完整性：有 Content-Length 必须收满；没有就查 zip 的 EOCD 签名
            $got = is_file($destZip) ? (int) filesize($destZip) : 0;
            $total = (int) ($info !== '' ? $info : 0);
            if ($total > 0) {
                if ($got < $total) {
                    $lastErr = "下载不完整（{$got}/{$total} 字节）";
                    continue;
                }
            } elseif (!self::zipComplete($destZip)) {
                $lastErr = "下载可能不完整（无 Content-Length，zip 尾部校验失败，{$got} 字节）";
                continue;
            }
            @unlink($fpPath);
            return [true, 'ok'];
        }
        return [false, $lastErr !== '' ? $lastErr : '下载失败'];
    }

    /** 记录产物身份（id|大小|更新时间|名字），供续传前比对。 */
    private static function artifactIdent(string $token, string $repo, int $aid): string
    {
        [$st, $body] = self::request($token, 'GET',
            "/repos/{$repo}/actions/artifacts/{$aid}");
        $a = is_array($body) ? (array) ($body['artifact'] ?? []) : [];
        if ($st !== 200 || !$a) {
            return (string) $aid;
        }
        return ($a['id'] ?? '') . '|' . ($a['size_in_bytes'] ?? '') . '|'
             . ($a['updated_at'] ?? '') . '|' . ($a['name'] ?? '');
    }

    /** 校验 302 的 Location，返回 [ok, 原因]。 */
    public static function validateRedirect(string $loc): array
    {
        if ($loc === '') {
            return [false, '重定向地址为空'];
        }
        $u = @parse_url($loc);
        if (!is_array($u) || !isset($u['host'])) {
            return [false, '重定向地址无法解析'];
        }
        $scheme = strtolower((string) ($u['scheme'] ?? ''));
        if ($scheme !== 'https') {
            return [false, '重定向协议不允许: ' . ($scheme !== '' ? $scheme : '(空)')];
        }
        $host = strtolower((string) $u['host']);
        if ($host === '') {
            return [false, '重定向缺少主机名'];
        }
        if (in_array($host, self::REDIRECT_HOSTS, true)) {
            return [true, 'ok'];
        }
        foreach (self::REDIRECT_SUFFIXES as $sfx) {
            if (str_ends_with($host, $sfx)) {
                return [true, 'ok'];
            }
        }
        return [false, "重定向目标不在白名单: {$host}"];
    }

    /** 判定 zip 是否完整：尾部查找结尾中央目录（EOCD）签名 PK\x05\x06。 */
    public static function zipComplete(string $path): bool
    {
        if (!is_file($path)) {
            return false;
        }
        $size = (int) filesize($path);
        if ($size < 22) {
            return false;
        }
        $tail = min($size, 65557);
        $fh = @fopen($path, 'rb');
        if ($fh === false) {
            return false;
        }
        fseek($fh, $size - $tail);
        $data = (string) fread($fh, $tail);
        fclose($fh);
        return str_contains($data, "PK\x05\x06");
    }

    /**
     * 安全解压 zip —— 拒绝路径穿越、绝对路径、符号链接与设备文件。
     *
     * 产物来自 GitHub Actions（第三方仓库的 workflow 可写），按不可信输入处理。
     */
    public static function safeUnzip(string $zip, string $dest): int
    {
        // ★ 不能假设装了 zip 扩展：宝塔/精简版 PHP 常常没有它，而 phar 是默认开启的。
        //   实测本机 PHP 8.0（含宝塔常见发行）就没有 ext/zip —— 只有 ZipArchive 一条路
        //   的话，产物回传会在解压这一步全军覆没。
        if (!class_exists(\ZipArchive::class)) {
            return self::safeUnzipPhar($zip, $dest);
        }
        $z = new \ZipArchive();
        if ($z->open($zip) !== true) {
            throw new \RuntimeException('无法打开 zip 归档');
        }
        $base = realpath($dest);
        if ($base === false) {
            $z->close();
            throw new \RuntimeException('目标目录不可用');
        }
        $n = 0;
        try {
            for ($i = 0; $i < $z->numFiles; $i++) {
                $st = $z->statIndex($i);
                if ($st === false) {
                    continue;
                }
                $name = (string) $st['name'];
                if ($name === '' || str_contains($name, "\0")
                    || str_starts_with($name, '/') || str_starts_with($name, '\\')
                    || preg_match('#(^|[/\\\\])\.\.([/\\\\]|$)#', $name)) {
                    continue;                                  // 穿越/绝对路径 → 丢弃该条目
                }
                $target = $base . '/' . $name;
                if (str_ends_with($name, '/')) {
                    @mkdir($target, 0755, true);
                    continue;
                }
                // 符号链接：zip 用 unix 模式的高 16 位表示，S_IFLNK = 0xA000
                $mode = ($st['external_attributes'] ?? 0) >> 16;
                if (($mode & 0xF000) === 0xA000) {
                    continue;                                  // 符号链接 → 丢弃
                }
                $dir = dirname($target);
                if (!is_dir($dir) && !@mkdir($dir, 0755, true) && !is_dir($dir)) {
                    continue;
                }
                $src = $z->getStream($name);
                if ($src === false) {
                    continue;
                }
                $out = @fopen($target, 'wb');
                if ($out !== false) {
                    stream_copy_to_stream($src, $out);
                    fclose($out);
                    $n++;
                }
                if (is_resource($src)) {
                    fclose($src);
                }
            }
        } finally {
            $z->close();
        }
        return $n;
    }

    /**
     * PharData 兜底解压（无 ext/zip 时）。
     *
     * 与 ZipArchive 路径同样的安全判据：拒路径穿越、绝对路径与符号链接。
     * 不直接 extractTo() —— 它不做逐条校验，遇到穿越条目会照写。
     */
    private static function safeUnzipPhar(string $zip, string $dest): int
    {
        if (!class_exists(\PharData::class)) {
            throw new \RuntimeException(
                'PHP 缺少 zip 与 phar 扩展，无法解压产物。'
                . '请在面板为当前 PHP 版本安装 zip 扩展（宝塔：软件商店 → PHP → 设置 → 安装扩展）。');
        }
        $base = realpath($dest);
        if ($base === false) {
            throw new \RuntimeException('目标目录不可用');
        }
        try {
            $phar = new \PharData($zip);
        } catch (\Throwable $e) {
            throw new \RuntimeException('无法打开 zip 归档: ' . $e->getMessage());
        }
        $n = 0;
        $it = new \RecursiveIteratorIterator($phar, \RecursiveIteratorIterator::SELF_FIRST);
        foreach ($it as $file) {
            $pn = str_replace('\\', '/', (string) $file->getPathname());
            // 形如 phar:///abs/x.zip/dir/name → 取 .zip/ 之后的部分
            $rel = preg_replace('#^.*?\.zip/#', '', $pn);
            if ($rel === null || $rel === '' || $rel === $pn) {
                continue;
            }
            if (str_contains($rel, "\0") || str_starts_with($rel, '/')
                || preg_match('#(^|/)\.\.(/|$)#', $rel)) {
                continue;
            }
            if ($file->isLink()) {
                continue;                                  // 符号链接 → 丢弃
            }
            $target = $base . '/' . $rel;
            if ($file->isDir()) {
                @mkdir($target, 0755, true);
                continue;
            }
            $dir = dirname($target);
            if (!is_dir($dir) && !@mkdir($dir, 0755, true) && !is_dir($dir)) {
                continue;
            }
            $data = @file_get_contents($file->getPathname());
            if ($data === false) {
                continue;
            }
            if (@file_put_contents($target, $data) !== false) {
                $n++;
            }
        }
        return $n;
    }

    // ------------------------------------------------------------------ HTTP

    /**
     * GitHub REST 调用。
     *
     * @return array{0:int,1:mixed} [HTTP 状态码, 解析后的 body]；网络失败状态码为 0
     */
    public static function request(string $token, string $method, string $path,
                                   ?array $payload = null, int $timeout = 45): array
    {
        $ch = curl_init(self::API . $path);
        $headers = [
            'Authorization: Bearer ' . $token,
            'Accept: application/vnd.github+json',
            'X-GitHub-Api-Version: 2022-11-28',
            'User-Agent: ' . self::UA,
        ];
        $opt = [
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_CUSTOMREQUEST  => $method,
            CURLOPT_TIMEOUT        => $timeout,
            CURLOPT_FOLLOWLOCATION => false,
            CURLOPT_HTTPHEADER     => $headers,
        ];
        if ($payload !== null) {
            $opt[CURLOPT_POSTFIELDS] = json_encode($payload, JSON_UNESCAPED_UNICODE);
            $headers[] = 'Content-Type: application/json';
            $opt[CURLOPT_HTTPHEADER] = $headers;
        }
        curl_setopt_array($ch, $opt);
        $raw = curl_exec($ch);
        $err = curl_error($ch);
        $code = (int) curl_getinfo($ch, CURLINFO_HTTP_CODE);
        curl_close($ch);
        if ($raw === false) {
            return [0, ['message' => 'curl: ' . $err]];
        }
        $j = json_decode((string) $raw, true);
        return [$code, $j ?? (string) $raw];
    }

    /** 只取 302 的 Location（不跟随重定向）。 */
    private static function headRedirect(string $token, string $url): array
    {
        $ch = curl_init($url);
        curl_setopt_array($ch, [
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_FOLLOWLOCATION => false,
            CURLOPT_TIMEOUT        => 60,
            CURLOPT_HTTPHEADER     => [
                'Authorization: Bearer ' . $token,
                'Accept: application/vnd.github+json',
                'User-Agent: ' . self::UA,
            ],
        ]);
        curl_exec($ch);
        $code = (int) curl_getinfo($ch, CURLINFO_HTTP_CODE);
        $loc = (string) curl_getinfo($ch, CURLINFO_REDIRECT_URL);
        curl_close($ch);
        return [$code, $loc];
    }

    /**
     * 带续传的下载。
     *
     * @param int $have 已下载字节数（>0 时发 Range 请求）
     * @return array{0:bool,1:string} 成功时第 2 项是**完整总字节数**（0 表示服务端未给 Content-Length）
     */
    private static function httpDownload(string $url, string $dest, int $have, int $timeout): array
    {
        $fh = @fopen($dest, $have > 0 ? 'ab' : 'wb');
        if ($fh === false) {
            return [false, '无法写入目标文件'];
        }
        $headers = ['User-Agent: ' . self::UA];
        if ($have > 0) {
            $headers[] = 'Range: bytes=' . $have . '-';
        }
        $ch = curl_init($url);
        curl_setopt_array($ch, [
            CURLOPT_FILE           => $fh,
            CURLOPT_FOLLOWLOCATION => true,
            CURLOPT_TIMEOUT        => $timeout,
            CURLOPT_HTTPHEADER     => $headers,
        ]);
        $ok = curl_exec($ch);
        $err = curl_error($ch);
        $code = (int) curl_getinfo($ch, CURLINFO_HTTP_CODE);
        $len = (int) curl_getinfo($ch, CURLINFO_CONTENT_LENGTH_DOWNLOAD);
        curl_close($ch);
        fclose($fh);
        if ($ok === false) {
            return [false, 'curl: ' . $err];
        }
        if ($code !== 200 && $code !== 206) {
            return [false, "HTTP {$code}"];
        }
        // 服务端没接受续传（200）→ 文件已从头覆盖，不能把 have 算进总数
        $total = $code === 206 ? ($len > 0 ? $len + $have : 0) : ($len > 0 ? $len : 0);
        return [true, (string) $total];
    }

    /** ISO8601 → epoch；解析失败返回 0。 */
    private static function isoTs(string $s): int
    {
        if ($s === '') {
            return 0;
        }
        $t = strtotime($s);
        return $t === false ? 0 : $t;
    }
}
