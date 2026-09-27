<?php
declare(strict_types=1);

namespace Kwrt;
/**
 * 表结构定义 —— **单一真源**。
 *
 * SQLITE 常量是真源（与 Python 版逐字对齐，保证两版能操作同一个 users.db）；
 * MYSQL 常量由 php/scripts/gen_mysql_schema.py 从它**派生**，不要手改。
 *
 * 为什么不手写两份：本项目已有一次教训 —— 同一份设置 schema 手写两遍，
 * 结果两版漂移（reports/09）。DDL 手写两套同样会漂，而且是**静默**漂：
 * 一方能跑，另一方只在新装建表时炸。故沿用生成式同源。
 *
 * 改动流程：
 *     ① 改这里的 SQLITE
 *     ② python3 php/scripts/gen_mysql_schema.py
 *     ③ git diff 看 MYSQL 变化是否符合预期
 */
final class Schema
{
    /** SQLite 建表语句（真源；与 Python 版表结构一致）。 */
    public const SQLITE = [
     // GitHub 兜底查找 run 时的跨进程认领表。
     // ★ 必须进 Schema 真源、不能内联建表：MySQL 的 DDL 由生成器从这里派生，
     //   内联的话 MySQL 侧就没有这张表（而且列类型也是 SQLite 的写法）。
     'gh_run_claims' => 'CREATE TABLE IF NOT EXISTS gh_run_claims(
             run_id INTEGER PRIMARY KEY, created REAL NOT NULL
         )',
     'users' => 'CREATE TABLE IF NOT EXISTS users(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                email TEXT,
                sponsor INTEGER DEFAULT 0,
                created REAL,
                role TEXT DEFAULT "user", disabled INTEGER DEFAULT 0, last_login REAL,
                quota INTEGER DEFAULT 12, sponsor_until REAL, sponsor_tier TEXT DEFAULT "",
                sponsor_amount REAL DEFAULT 0, email_verified INTEGER DEFAULT 1
            )',
        'sessions' => 'CREATE TABLE IF NOT EXISTS sessions(
                token TEXT PRIMARY KEY, username TEXT, created REAL, expires REAL
            )',
        'builds' => 'CREATE TABLE IF NOT EXISTS builds(
                request_hash TEXT PRIMARY KEY, username TEXT, target TEXT, profile TEXT,
                packages TEXT, status TEXT, created REAL, payload TEXT
            )',
        'proposals' => 'CREATE TABLE IF NOT EXISTS proposals(
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, url TEXT, note TEXT,
                created REAL, status TEXT DEFAULT "pending", reply TEXT DEFAULT "", ip TEXT DEFAULT ""
            )',
        'jobs' => 'CREATE TABLE IF NOT EXISTS jobs(
                request_hash TEXT PRIMARY KEY, status TEXT, detail TEXT, payload TEXT,
                result TEXT, created REAL, updated REAL
            )',
        'admin_logs' => 'CREATE TABLE IF NOT EXISTS admin_logs(
                id INTEGER PRIMARY KEY AUTOINCREMENT, admin TEXT, action TEXT, target TEXT,
                detail TEXT, ip TEXT, created REAL
            )',
        'settings' => 'CREATE TABLE IF NOT EXISTS settings(
                key TEXT PRIMARY KEY, value TEXT, updated REAL
            )',
        'bans' => 'CREATE TABLE IF NOT EXISTS bans(
                id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, value TEXT, reason TEXT, created REAL
            )',
        'dl_tokens' => 'CREATE TABLE IF NOT EXISTS dl_tokens(
                token TEXT PRIMARY KEY, request_hash TEXT, username TEXT, filename TEXT,
                created REAL, expires REAL, max_hits INTEGER DEFAULT 0, hits INTEGER DEFAULT 0,
                revoked INTEGER DEFAULT 0, last_hit REAL, last_ip TEXT
            )',
        'sponsor_claims' => 'CREATE TABLE IF NOT EXISTS sponsor_claims(
                id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT, tier TEXT,
                amount REAL, note TEXT, status TEXT, created REAL
            )',
        'pay_orders' => 'CREATE TABLE IF NOT EXISTS pay_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                out_trade_no TEXT NOT NULL UNIQUE, username TEXT NOT NULL,
                tier_name TEXT NOT NULL, amount REAL NOT NULL, days INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT "created", qr_code TEXT, trade_no TEXT,
                buyer_id TEXT, created REAL NOT NULL, expires REAL, paid_at REAL,
                last_query REAL, raw TEXT
            )',
        'email_verifications' => 'CREATE TABLE IF NOT EXISTS email_verifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT, token_hash TEXT NOT NULL UNIQUE,
                username TEXT NOT NULL, email TEXT NOT NULL, created REAL NOT NULL,
                expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0, used_at REAL, ip TEXT
            )',
        // 找回密码令牌表 —— 与 Python 版 app/reset.py 同构（两版共用一个 users.db）。
        // 令牌原文**从不落库**，只存 SHA-256；used 配合 UPDATE ... WHERE used=0
        // 做一次性消费；每账号同时只保留一条有效令牌。
        'password_resets' => 'CREATE TABLE IF NOT EXISTS password_resets (
                id INTEGER PRIMARY KEY AUTOINCREMENT, token_hash TEXT NOT NULL UNIQUE,
                username TEXT NOT NULL, email TEXT NOT NULL, created REAL NOT NULL,
                expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0, used_at REAL, ip TEXT
            )',
        // 更新检查的结果缓存 —— 与 Python 版 app/update.py 同构（两版共用一张表）。
        // 为什么必须落库而不是进程内缓存：GitHub 未认证请求每小时仅 60 次，
        // 反复点「检查更新」会把限额打满，连固件构建派发一起挂掉；
        // 而 PHP 每个请求都是独立进程，进程内缓存等于没有。
        // 固定只有 id=1 一行（最新一次结果），payload 是 JSON。
        'update_checks' => 'CREATE TABLE IF NOT EXISTS update_checks (
                id INTEGER PRIMARY KEY, checked_at REAL NOT NULL, payload TEXT NOT NULL
            )',
        'email_send_log' => 'CREATE TABLE IF NOT EXISTS email_send_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, purpose TEXT NOT NULL, to_addr TEXT NOT NULL,
                username TEXT, ok INTEGER NOT NULL, detail TEXT, created REAL NOT NULL
            )',
        'refund_requests' => 'CREATE TABLE IF NOT EXISTS refund_requests(
                id INTEGER PRIMARY KEY AUTOINCREMENT, out_trade_no TEXT NOT NULL,
                username TEXT NOT NULL, amount REAL NOT NULL, reason TEXT NOT NULL, detail TEXT,
                status TEXT NOT NULL DEFAULT "pending", created REAL NOT NULL, reviewed_at REAL,
                reviewer TEXT, review_note TEXT, trade_no TEXT, refund_amount REAL, gateway_raw TEXT
            )',
        'rate_limits' => 'CREATE TABLE IF NOT EXISTS rate_limits(
                id TEXT PRIMARY KEY, hits INTEGER NOT NULL DEFAULT 0, window_start REAL NOT NULL
            )',
        'login_fails' => 'CREATE TABLE IF NOT EXISTS login_fails(
                id TEXT PRIMARY KEY, fails INTEGER NOT NULL DEFAULT 0,
                until REAL NOT NULL DEFAULT 0, updated REAL
            )',
        // 验证码与通用小键值表：原先散在 Captcha.php / Queues.php 里各自建，
        // 收到真源里，MySQL 版才会一并被生成 —— 否则用 MySQL 时这两张表永远建不出来。
        'captchas' => 'CREATE TABLE IF NOT EXISTS captchas(
                id TEXT PRIMARY KEY, answer_hash TEXT NOT NULL, created REAL NOT NULL,
                used INTEGER NOT NULL DEFAULT 0, ip TEXT DEFAULT \'\'
            )',
        'app_secrets' => 'CREATE TABLE IF NOT EXISTS app_secrets(
                name TEXT PRIMARY KEY, value TEXT NOT NULL, created REAL NOT NULL
            )',
    ];

    /** 高频查询索引（SQLite）。 */
    public const INDEX_SQLITE = [
        'CREATE INDEX IF NOT EXISTS idx_sessions_user  ON sessions(username)',
        'CREATE INDEX IF NOT EXISTS idx_builds_user    ON builds(username, created DESC)',
        'CREATE INDEX IF NOT EXISTS idx_tokens_hash    ON dl_tokens(request_hash)',
        'CREATE INDEX IF NOT EXISTS idx_tokens_user    ON dl_tokens(username)',
        'CREATE INDEX IF NOT EXISTS idx_pay_user       ON pay_orders(username, created DESC)',
        'CREATE INDEX IF NOT EXISTS idx_refund_status  ON refund_requests(status, created DESC)',
        'CREATE INDEX IF NOT EXISTS idx_ev_user        ON email_verifications(username)',
        'CREATE INDEX IF NOT EXISTS idx_pr_user        ON password_resets(username)',
        'CREATE INDEX IF NOT EXISTS idx_pr_hash        ON password_resets(token_hash)',
        'CREATE INDEX IF NOT EXISTS idx_pr_created     ON password_resets(created DESC)',
        'CREATE INDEX IF NOT EXISTS idx_logs_created   ON admin_logs(created DESC)',
    ];

    /**
     * MySQL 建表语句 —— **由 php/scripts/gen_mysql_schema.py 从 SQLITE 生成**，
     * 不要手改：改 SQLITE 后重新生成，并用 --check 校验同步。
     */
    public const MYSQL = [
        'gh_run_claims' => 'CREATE TABLE IF NOT EXISTS `gh_run_claims` (
    `run_id` INT,
    `created` DOUBLE NOT NULL,
    PRIMARY KEY (`run_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci',
        'users' => 'CREATE TABLE IF NOT EXISTS `users` (
    `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `username` VARCHAR(191) NOT NULL,
    `password` TEXT NOT NULL,
    `email` TEXT,
    `sponsor` INT DEFAULT 0,
    `created` DOUBLE,
    `role` VARCHAR(255) DEFAULT \'user\',
    `disabled` INT DEFAULT 0,
    `last_login` DOUBLE,
    `quota` INT DEFAULT 12,
    `sponsor_until` DOUBLE,
    `sponsor_tier` VARCHAR(255) DEFAULT \'\',
    `sponsor_amount` DOUBLE DEFAULT 0,
    `email_verified` INT DEFAULT 1,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_users_username` (`username`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci',
        'sessions' => 'CREATE TABLE IF NOT EXISTS `sessions` (
    `token` VARCHAR(191),
    `username` VARCHAR(191),
    `created` DOUBLE,
    `expires` DOUBLE,
    PRIMARY KEY (`token`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci',
        'builds' => 'CREATE TABLE IF NOT EXISTS `builds` (
    `request_hash` VARCHAR(191),
    `username` VARCHAR(191),
    `target` TEXT,
    `profile` TEXT,
    `packages` TEXT,
    `status` TEXT,
    `created` DOUBLE,
    `payload` TEXT,
    PRIMARY KEY (`request_hash`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci',
        'proposals' => 'CREATE TABLE IF NOT EXISTS `proposals` (
    `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `name` TEXT,
    `url` TEXT,
    `note` TEXT,
    `created` DOUBLE,
    `status` VARCHAR(255) DEFAULT \'pending\',
    `reply` VARCHAR(255) DEFAULT \'\',
    `ip` VARCHAR(255) DEFAULT \'\',
    PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci',
        'jobs' => 'CREATE TABLE IF NOT EXISTS `jobs` (
    `request_hash` VARCHAR(191),
    `status` TEXT,
    `detail` TEXT,
    `payload` TEXT,
    `result` TEXT,
    `created` DOUBLE,
    `updated` DOUBLE,
    PRIMARY KEY (`request_hash`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci',
        'admin_logs' => 'CREATE TABLE IF NOT EXISTS `admin_logs` (
    `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `admin` TEXT,
    `action` TEXT,
    `target` TEXT,
    `detail` TEXT,
    `ip` TEXT,
    `created` DOUBLE,
    PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci',
        'settings' => 'CREATE TABLE IF NOT EXISTS `settings` (
    `key` VARCHAR(191),
    `value` TEXT,
    `updated` DOUBLE,
    PRIMARY KEY (`key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci',
        'bans' => 'CREATE TABLE IF NOT EXISTS `bans` (
    `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `kind` TEXT,
    `value` TEXT,
    `reason` TEXT,
    `created` DOUBLE,
    PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci',
        'dl_tokens' => 'CREATE TABLE IF NOT EXISTS `dl_tokens` (
    `token` VARCHAR(191),
    `request_hash` VARCHAR(191),
    `username` VARCHAR(191),
    `filename` TEXT,
    `created` DOUBLE,
    `expires` DOUBLE,
    `max_hits` INT DEFAULT 0,
    `hits` INT DEFAULT 0,
    `revoked` INT DEFAULT 0,
    `last_hit` DOUBLE,
    `last_ip` TEXT,
    PRIMARY KEY (`token`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci',
        'sponsor_claims' => 'CREATE TABLE IF NOT EXISTS `sponsor_claims` (
    `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `username` TEXT,
    `tier` TEXT,
    `amount` DOUBLE,
    `note` TEXT,
    `status` TEXT,
    `created` DOUBLE,
    PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci',
        'pay_orders' => 'CREATE TABLE IF NOT EXISTS `pay_orders` (
    `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `out_trade_no` VARCHAR(191) NOT NULL,
    `username` VARCHAR(191) NOT NULL,
    `tier_name` TEXT NOT NULL,
    `amount` DOUBLE NOT NULL,
    `days` INT NOT NULL,
    `status` VARCHAR(255) NOT NULL DEFAULT \'created\',
    `qr_code` TEXT,
    `trade_no` TEXT,
    `buyer_id` TEXT,
    `created` DOUBLE NOT NULL,
    `expires` DOUBLE,
    `paid_at` DOUBLE,
    `last_query` DOUBLE,
    `raw` TEXT,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_pay_orders_out_trade_no` (`out_trade_no`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci',
        'email_verifications' => 'CREATE TABLE IF NOT EXISTS `email_verifications` (
    `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `token_hash` VARCHAR(191) NOT NULL,
    `username` VARCHAR(191) NOT NULL,
    `email` TEXT NOT NULL,
    `created` DOUBLE NOT NULL,
    `expires` DOUBLE NOT NULL,
    `used` INT NOT NULL DEFAULT 0,
    `used_at` DOUBLE,
    `ip` TEXT,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_email_verifications_token_hash` (`token_hash`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci',
        'password_resets' => 'CREATE TABLE IF NOT EXISTS `password_resets` (
    `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `token_hash` VARCHAR(191) NOT NULL,
    `username` VARCHAR(191) NOT NULL,
    `email` TEXT NOT NULL,
    `created` DOUBLE NOT NULL,
    `expires` DOUBLE NOT NULL,
    `used` INT NOT NULL DEFAULT 0,
    `used_at` DOUBLE,
    `ip` TEXT,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_password_resets_token_hash` (`token_hash`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci',
        'update_checks' => 'CREATE TABLE IF NOT EXISTS `update_checks` (
    `id` INT,
    `checked_at` DOUBLE NOT NULL,
    `payload` TEXT NOT NULL,
    PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci',
        'email_send_log' => 'CREATE TABLE IF NOT EXISTS `email_send_log` (
    `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `purpose` TEXT NOT NULL,
    `to_addr` TEXT NOT NULL,
    `username` TEXT,
    `ok` INT NOT NULL,
    `detail` TEXT,
    `created` DOUBLE NOT NULL,
    PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci',
        'refund_requests' => 'CREATE TABLE IF NOT EXISTS `refund_requests` (
    `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `out_trade_no` TEXT NOT NULL,
    `username` TEXT NOT NULL,
    `amount` DOUBLE NOT NULL,
    `reason` TEXT NOT NULL,
    `detail` TEXT,
    `status` VARCHAR(191) NOT NULL DEFAULT \'pending\',
    `created` DOUBLE NOT NULL,
    `reviewed_at` DOUBLE,
    `reviewer` TEXT,
    `review_note` TEXT,
    `trade_no` TEXT,
    `refund_amount` DOUBLE,
    `gateway_raw` TEXT,
    PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci',
        'rate_limits' => 'CREATE TABLE IF NOT EXISTS `rate_limits` (
    `id` VARCHAR(191),
    `hits` INT NOT NULL DEFAULT 0,
    `window_start` DOUBLE NOT NULL,
    PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci',
        'login_fails' => 'CREATE TABLE IF NOT EXISTS `login_fails` (
    `id` VARCHAR(191),
    `fails` INT NOT NULL DEFAULT 0,
    `until` DOUBLE NOT NULL DEFAULT 0,
    `updated` DOUBLE,
    PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci',
        'captchas' => 'CREATE TABLE IF NOT EXISTS `captchas` (
    `id` VARCHAR(191),
    `answer_hash` TEXT NOT NULL,
    `created` DOUBLE NOT NULL,
    `used` INT NOT NULL DEFAULT 0,
    `ip` VARCHAR(255) DEFAULT \'\',
    PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci',
        'app_secrets' => 'CREATE TABLE IF NOT EXISTS `app_secrets` (
    `name` VARCHAR(191),
    `value` TEXT NOT NULL,
    `created` DOUBLE NOT NULL,
    PRIMARY KEY (`name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci',
    ];

    /** MySQL 索引语句（同上，由生成器派生）。 */
    public const INDEX_MYSQL = [
        'CREATE INDEX idx_sessions_user  ON sessions(username)',
        'CREATE INDEX idx_builds_user    ON builds(username, created DESC)',
        'CREATE INDEX idx_tokens_hash    ON dl_tokens(request_hash)',
        'CREATE INDEX idx_tokens_user    ON dl_tokens(username)',
        'CREATE INDEX idx_pay_user       ON pay_orders(username, created DESC)',
        'CREATE INDEX idx_refund_status  ON refund_requests(status, created DESC)',
        'CREATE INDEX idx_ev_user        ON email_verifications(username)',
        'CREATE INDEX idx_pr_user        ON password_resets(username)',
        'CREATE INDEX idx_pr_hash        ON password_resets(token_hash)',
        'CREATE INDEX idx_pr_created     ON password_resets(created DESC)',
        'CREATE INDEX idx_logs_created   ON admin_logs(created DESC)',
    ];

}
