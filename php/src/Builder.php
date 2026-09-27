<?php
/**
 * 构建引擎（本机 ImageBuilder）。
 *
 * 与 Python 版的差异（这是 PHP 侧刻意的取舍）：
 *   Python 版在进程内维护一个构建队列（BuildQueue + 线程池），
 *   因此它**只能单进程运行**（报告 §五 已把「多 worker 会串」列为需确认项）。
 *   PHP 没有常驻进程，所以这里把队列状态放在 **jobs 表**里，
 *   由「提交时抢一个空闲槽 + 后台进程跑」两段构成：
 *     · 抢槽：一条 UPDATE ... WHERE status='queued' 的原子语句，天然防并发重复取
 *     · 执行：nohup 起一个独立 PHP 进程跑 build-worker.php（不阻塞请求）
 *   副产品：**多 worker / 多机部署下队列也是正确的**，比 Python 版更强。
 */
declare(strict_types=1);

namespace Kwrt;

final class Builder
{
    public static function maxConcurrent(bool $vip = false): int
    {
        $k = $vip ? 'builder.vip_concurrent' : 'builder.max_concurrent';
        return max(1, (int) Settings::get($k, Config::get('builder.max_concurrent', 1)));
    }

    public static function enabled(): bool
    {
        $v = Settings::get('builder.enabled');
        return $v === null ? (bool) Config::get('builder.enabled', true) : (bool) $v;
    }

    public static function storeDir(string $hash): string
    {
        return Config::path('store', $hash);
    }

    public static function workDir(string $hash): string
    {
        return Config::path('work', $hash);
    }

    /** 入队：写 jobs 表，状态 queued。 */
    public static function enqueue(string $hash, array $payload): void
    {
        $now = microtime(true);
        Db::upsert('jobs', [
            'request_hash' => $hash, 'status' => 'queued', 'detail' => '已进入队列',
            'payload' => json_encode($payload, JSON_UNESCAPED_UNICODE),
            'result' => null, 'created' => $now, 'updated' => $now,
        ], ['request_hash']);
    }

    /**
     * 尝试抢占一个空闲槽并启动后台 worker。
     * @return array{0:bool,1:string,2:int} [是否已调度, 说明, 队列位置]
     */
    /**
     * 判定「僵尸任务」的时限：running 超过这个秒数仍未更新，就当作 worker 已死。
     *
     * 为什么需要：worker 是 nohup 起的独立进程，它可能压根没起来（exec 被禁用）、
     * 起来后立刻崩、或被 OOM killer 干掉。无论哪种，jobs.status 都会**永远**留在
     * running —— 每个僵尸吃掉一个并发槽位，几个僵尸就把队列彻底堵死，
     * 而用户看到的是「已排队，等待空闲槽」永远不变。
     *
     * 取 3 小时：Engine 内部对 make 的超时上限是 2 小时
     * （Python 侧同样硬编码 7200s），留 1 小时余量，避免把正常构建误判为僵尸。
     * 可用 KWRT_STALE_RUNNING_SECONDS 覆盖（多机/慢机器场景）。
     */
    /** 公开的阈值读取（供 php/scripts/reap.php 打印用）。 */
    public static function reapThreshold(): int
    {
        return self::staleAfterSeconds();
    }

    private static function staleAfterSeconds(): int
    {
        $env = getenv('KWRT_STALE_RUNNING_SECONDS');
        if (is_string($env) && $env !== '' && ctype_digit($env)) {
            return max(600, (int) $env);
        }
        return 3 * 3600;
    }

    /**
     * 回收僵尸任务：把长时间无更新的 running 任务判失败，归还并发槽位。
     *
     * 幂等，可在每次 pump() 前无脑调用。返回回收条数。
     */
    public static function reap(): int
    {
        $cutoff = microtime(true) - self::staleAfterSeconds();

        // ★★ 这里绝不能在 SQL 里比时间 —— 会踩 SQLite 的类型序（实测得出，非推测）：
        //   · 时间列是 REAL，但 PDO 把 PHP 浮点**绑定成 TEXT**（EMULATE_PREPARES=false
        //     时 typeof(?) 实测为 "text"）；
        //   · `列 > ?` 之所以正确，是因为**列的 REAL 亲和性**会把 TEXT 操作数转成数值；
        //   · 但 `COALESCE(updated, created, 0) < ?` 里 COALESCE 的表达式亲和性是
        //     NONE，不做任何转换 → 落到 SQLite 的类型序 NULL < 数值 < TEXT < BLOB，
        //     于是「REAL < TEXT」**恒为真** → 匹配到**所有**行。
        //   后果极重：pump() 每次提交都会调用 reap()，等于**每提交一次构建就杀掉
        //   所有正在跑的构建**（还顺手释放槽位、让新任务插队）。
        //   故改为取出候选后**在 PHP 里用浮点比较** —— 不依赖任何 SQL 类型推断。
        //   候选集只有「当前 running 的任务」，数量 ≤ maxConcurrent，开销可忽略。
        $rows = Db::all("SELECT request_hash, created, updated FROM jobs WHERE status='running'");
        $n = 0;
        foreach ($rows as $r) {
            $t = (float) ($r['updated'] ?? 0);
            if ($t <= 0) {
                $t = (float) ($r['created'] ?? 0);
            }
            if ($t <= 0 || $t >= $cutoff) {
                continue;   // 时间戳缺失或还没到阈值 → 不动它
            }
            self::fail((string) $r['request_hash'],
                '构建进程已失联（超过 ' . intdiv(self::staleAfterSeconds(), 3600)
                . ' 小时没有进度更新）—— 通常是 worker 进程被中断。'
                . '槽位已自动释放，可重新提交。');
            $n++;
        }
        return $n;
    }

    public static function pump(): array
    {
        return Db::tx(function () {
            // ★ 后端校验：两个后端都已实现（local / github），但**未知值必须明确报错**，
            //   不能静默按本地跑 —— 那会让「设置里选了 X」与「实际发生的事」不一致。
            $backend = strtolower((string) Settings::get('builder.backend', 'local'));
            if (!in_array($backend, ['local', 'github'], true)) {
                return [false, '未知的构建后端「' . $backend . '」。可选：local（本机实编）、'
                             . 'github（远端 GitHub Actions）。', 0];
            }
            if ($backend === 'github' && !Github::available()) {
                return [false, '构建后端设为 github，但 GitHub 未配置完整或总开关未开'
                             . '（需要 gh.enabled + repo + token）。请到后台「构建」检查，'
                             . '或把后端改回 local。', 0];
            }
            // 先回收僵尸，再算空余槽位 —— 否则被僵尸占住的槽位永远回不来
            self::reap();
            $running = (int) Db::val("SELECT COUNT(*) FROM jobs WHERE status='running'", [], 0);
            $max = self::maxConcurrent();
            if ($running >= $max) {
                $pos = (int) Db::val("SELECT COUNT(*) FROM jobs WHERE status='queued'", [], 0);
                return [false, '已排队，等待空闲槽', max(1, $pos)];
            }
            $next = Db::one("SELECT request_hash FROM jobs WHERE status='queued' "
                . 'ORDER BY created ASC LIMIT 1');
            if (!$next) {
                return [false, '队列为空', 0];
            }
            // 原子抢槽：只有把 queued 改成 running 的那一次调用才拿到执行权
            $n = Db::run("UPDATE jobs SET status='running', detail='构建中', updated=? "
                . "WHERE request_hash=? AND status='queued'", [microtime(true), $next['request_hash']]);
            if ($n !== 1) {
                return [false, '未抢到槽位', 0];
            }
            // 起不来就如实说 —— spawnWorker 内部已把任务判失败并释放了槽位
            if (!self::spawnWorker((string) $next['request_hash'])) {
                return [false, '构建进程启动失败，详见任务详情', 0];
            }
            return [true, '已开始构建', 0];
        });
    }

    /**
     * 能不能起子进程。
     *
     * `exec` / `proc_open` 被 `disable_functions` 禁用是**宝塔/虚拟主机的默认形态**。
     * 原实现直接 `@exec(...)`：`@` 把 "Call to undefined function" 或
     * "exec() has been disabled" 一起吞掉，于是什么都没发生，而调用方已经
     * 把任务标成 running 并告诉用户「已开始构建」——
     * 既是**假成功**，又**永久吃掉一个并发槽位**。
     */
    private static function canSpawn(): bool
    {
        $disabled = array_map('trim', explode(',', (string) ini_get('disable_functions')));
        foreach (['exec', 'proc_open', 'shell_exec'] as $fn) {
            if (function_exists($fn) && !in_array($fn, $disabled, true)) {
                return true;
            }
        }
        return false;
    }

    /**
     * 起一个独立进程跑构建（不阻塞当前请求）。
     *
     * @return bool 是否真的把进程发出去了（false 时调用方必须回滚槽位）
     */
    private static function spawnWorker(string $hash): bool
    {
        if (!self::canSpawn()) {
            self::fail($hash,
                '无法启动构建进程：PHP 的 exec / proc_open / shell_exec 全部被禁用。\n'
                . '宝塔面板：软件商店 → PHP → 设置 → 禁用函数，删掉 exec 与 proc_open，'
                . '然后重载 PHP-FPM。或改用「远端 GitHub 构建后端」（不需要本地 exec）。');
            return false;
        }
        $php = PHP_BINARY ?: '/usr/bin/php';
        // 布局探测友好：<root>/php/scripts 找不到就看 <root>/scripts
        // （扁平部署时 scripts 与 public 并列）。
        $worker = Config::root() . '/php/scripts/build-worker.php';
        if (!is_file($worker)) {
            $alt = Config::root() . '/scripts/build-worker.php';
            if (is_file($alt)) {
                $worker = $alt;
            }
        }
        if (!is_file($worker)) {
            self::fail($hash, '构建 worker 脚本缺失（php/scripts/build-worker.php）');
            return false;
        }
        if (!is_file($php)) {
            self::fail($hash, "找不到 PHP 可执行文件：{$php}");
            return false;
        }
        $cmd = escapeshellarg($php) . ' ' . escapeshellarg($worker) . ' ' . escapeshellarg($hash);
        // 用 nohup + 重定向，避免父进程退出把子进程带走
        $cmd = 'nohup ' . $cmd . ' > /dev/null 2>&1 &';
        // $cmd 全部由 escapeshellarg 组成，且 $hash 来自 DB 里已过 Util::HASH_RE 的值
        $out = [];
        $rc = -1;
        @exec($cmd, $out, $rc);
        // 带 `&` 时 shell 立刻返回，$rc 反映的是 fork 是否成功，不是 build 的成败。
        // 非 0 才代表连进程都没发出去 —— 这时必须如实报错，不能继续让任务挂着。
        if ($rc !== 0) {
            self::fail($hash, "启动构建进程失败（shell 返回 {$rc}）。请检查 PHP 的执行权限与禁用函数。");
            return false;
        }
        return true;
    }

    public static function fail(string $hash, string $why): void
    {
        $why = mb_substr($why, 0, 900);
        Db::run("UPDATE jobs SET status='failed', detail=?, updated=? WHERE request_hash=?",
            [$why, microtime(true), $hash]);
        // ★ 同步回写 builds.status：状态接口虽然优先读 jobs，但后台列表、
        //   「我的订单」等直接查 builds 的地方会一直显示 queued（像是永远没开始）。
        //   两处状态不一致本身就是 bug，容易在排查时误导人。
        Db::run("UPDATE builds SET status='failed' WHERE request_hash=? AND status NOT IN ('done')",
            [$hash]);
    }

    public static function job(string $hash): ?array
    {
        return Db::one('SELECT * FROM jobs WHERE request_hash=?', [$hash]);
    }

    public static function queueStats(): array
    {
        $now = microtime(true);
        return [
            'running'  => (int) Db::val("SELECT COUNT(*) FROM jobs WHERE status='running'", [], 0),
            'queued'   => (int) Db::val("SELECT COUNT(*) FROM jobs WHERE status='queued'", [], 0),
            'done'     => (int) Db::val("SELECT COUNT(*) FROM jobs WHERE status='done'", [], 0),
            'failed'   => (int) Db::val("SELECT COUNT(*) FROM jobs WHERE status='failed'", [], 0),
            'max'      => self::maxConcurrent(),
            'enabled'  => self::enabled(),
            'backend'  => (string) Settings::get('builder.backend', Config::get('builder.backend', 'local')),
            'disk_free' => (int) (@disk_free_space(Config::root()) ?: 0),
            'store_bytes' => Util::dirStat(Config::path('store'))['bytes'],
        ];
    }
}
