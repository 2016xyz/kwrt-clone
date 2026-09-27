# 现场故障处置：PHP 7.x 下整站白屏

**现场报错**

```
Fatal error: Dynamic class names are not allowed in compile-time ::class fetch
in /www/wwwroot/op.2016xlx.cn/public/index.php on line 215
```

## 结论

| 项 | 结论 |
|---|---|
| **性质** | **自研代码的兼容性缺陷**（不是环境小问题）：入口文件用了 PHP 8.0 语法，在 PHP 7.x 上是**编译期**错误 |
| **直接原因** | 服务器运行 **PHP 7.x**（该报错文案是 PHP 7 独有的） |
| **现场根因** | `php/public/index.php:215` 用了 `$e::class` —— PHP 8.0 才允许 |
| **已修复** | ✅ 三处 `$e::class` → `get_class($e)`；`install.php` 的非捕获 catch → 捕获变量；**新增版本前置守卫** |
| **是否建议上线** | 修复后可以。但**服务器必须切到 PHP 8.0+**（PHP 7.4 已于 2022-11 停止安全支持） |

## 一、为什么这个报错这么难懂（这才是真问题）

`$obj::class`、非捕获式 `catch (\Throwable)`、联合类型参数都属于 PHP 8.0 的**语法**，
在 PHP 7.x 上是**编译期**错误 —— 也就是说：**整个文件根本编译不过**。

后果极其糟糕：文件里写再多运行时检查都没用，一个都不会执行。
用户只能看到那句 `Dynamic class names are not allowed in compile-time ::class fetch`，
既没说「你的 PHP 太老」，也没说「去哪改」。

**所以修复的重点不是「让代码跑在 PHP 7 上」，而是「让版本不对这件事说话」。**

## 二、实测证据

### 2.1 用真 PHP 7.4 容器定性（而非推测）

```
$ docker run --rm -v $PWD:/app -w /app php:7.4-cli php -l <file>
```

修复前：

| 文件 | PHP 7.4 结果 |
|---|---|
| `php/public/index.php` | ✗ `Cannot use ::class with dynamic class name`（第 215 行） |
| `php/public/install.php` | ✗ `syntax error, unexpected ')'`（第 76 行 —— 非捕获式 catch） |
| `php/scripts/build-worker.php` | ✗ 第 67 行 |
| `php/scripts/reap.php` | ✗ 第 69 行 |
| `php/src/` 27 个文件 | **8 个不兼容** |

> 最可惜的一处：`install.php` 本该是「环境检查页」，它第 109 行就有
> `PHP 版本 ≥ 8.0` 这一项 —— 但它自己先因非捕获 catch 编译不过，
> 用户根本走不到那一屏。**一个用来诊断版本问题的页面，因为版本问题而打不开。**

### 2.2 修复后（PHP 7.4.33 容器实测）

```
=== PHP 7.4 语法检查 ===
  ✓ php/public/index.php        可解析（守卫能执行）
  ✓ php/public/install.php      可解析（守卫能执行）
  ✓ php/public/router.php       可解析（守卫能执行）
  ✓ php/scripts/build-worker.php 可解析（守卫能执行）
  ✓ php/scripts/reap.php        可解析（守卫能执行）

=== 实跑 index.php ===
Kwrt(OpenWrt) 固件站 · PHP 版启动失败
=====================================

原因：需要 PHP 8.0 或更高版本，当前是 7.4.33。
（代码里用了 PHP 8.0 语法，低版本会在编译阶段直接失败，
  报错形如「Dynamic class names are not allowed in compile-time ::class fetch」。）

怎么修 ——
  宝塔面板：网站 → 你的站点 → 设置 → 「PHP 版本」选 8.0 / 8.1 / 8.2 → 保存
  ...
```

## 三、修复内容

| 文件 | 改动 |
|---|---|
| `php/public/index.php:215` | `$e::class` → `get_class($e)`（PHP 5.5+ 通用） |
| `php/scripts/build-worker.php:67` | 同上 |
| `php/scripts/reap.php:69` | 同上 |
| `php/public/install.php:76` | `catch (\Throwable) {` → `catch (\Throwable $e) {` |
| 四个入口文件 | 新增**版本前置守卫**（`PHP_VERSION_ID < 80000` 时打印可操作提示并 `exit(1)`） |

### 守卫为什么必须放在「最前面」且只用 PHP 5/7 语法

- 它是唯一能在 PHP 7 下运行的东西 —— 所以它自己不能含任何 PHP 8 语法；
- 必须在任何 `require` / `spl_autoload_register` / 类引用**之前**，
  否则 autoload 触发时会先炸在 `php/src/` 的 PHP 8 语法上。

**踩坑记录**：第一次插入时放在了 `declare(strict_types=1);` 与
`namespace Kwrt;` **之间** —— PHP 规定这两者之间不能有任何语句，直接语法错误。
正确位置是 `namespace Kwrt;` **之后**。

## 四、机械守卫（防止复发）

新增 `tools/verify_php_compat.py`，6 项：

| 编号 | 检查 | 结果 |
|---|---|---|
| C-1 | 4 个入口/脚本都含版本守卫 | ✓ |
| C-2 | 守卫先于任何 `require` / `autoload` | ✓ |
| C-3 | 入口文件不含 PHP 8.0 专有**语法**（静态扫描，无 docker 也能跑） | ✓ |
| C-4 | **PHP 7.4 容器**下入口文件全部可解析 | ✓ |
| C-5 | **PHP 7.4 容器**下实跑 `index.php` 得到可读提示而非 Fatal error | ✓ |
| C-6 | PHP 8 下 55 个文件 lint 通过（防回归） | ✓ |

C-4/C-5 是**真跑**的，不是静态推断 —— 与 2.1 用的同一套判据。

> 检查器自身的误报已当场修掉：C-3 原先把 `$router->match($method, $path)`
> 这个方法调用误判成 PHP 8 的 `match` 表达式（而 C-4 已证明 7.4 可解析，
> 说明是检查器错了）。现已加负向后顾排除 `->` `::` `$`。
> 「检查器一直喊狼来了」比没有检查器更糟。

## 五、为什么不把代码降级适配 PHP 7.4

| 方案 | 代价 |
|---|---|
| 降级适配 7.4 | 要改掉全站 `str_starts_with` / `str_contains` / 联合类型 / 构造器提升 / 非捕获 catch 等几十处；**测试矩阵翻倍**（每条断言要在两个大版本上验）；而 PHP 7.4 已于 **2022-11 停止安全支持** |
| 要求 PHP 8.0+ 并让失败可读 | 一次改动 5 个文件；用户一句面板操作解决 |

选后者。**降级适配等于把安全债留在生产环境里。**

## 六、现场还需确认的事

1. **部署路径对不上** —— 报错里是 `/www/wwwroot/op.2016xlx.cn/public/index.php`，
   而仓库结构是 `php/public/index.php`。若你把 `php/public` 移到了 `public/`，
   需确认 **`src/` 是否也移到了同级**：

   ```php
   // index.php 里
   spl_autoload_register(... __DIR__ . '/../src/' ...)
   ```

   即 `src/` 必须是 `public/` 的**兄弟目录**。若只移了 `public/` 而 `src/` 还在
   `php/src/`，切到 PHP 8 后会立刻变成 `Class "Kwrt\Config" not found`。
   建议直接恢复仓库原结构，把站点**运行目录**指到 `/php/public`。

2. **`op.2016xlx.cn` 这个域名**：切到 PHP 8 后到后台「域名与 CDN → 绑定域名」
   填上它，否则 Host 白名单可能不放行（本机 127.0.0.1 始终放行，不会被锁在门外）。

3. **切换 PHP 版本后重载 PHP-FPM**，否则 `php-fpm.d/www.conf` 里那些
   `env[KWRT_MYSQL_*]` 不会生效，表现为「连的还是 SQLite」。

## 七、回归

| 套件 | 结果 |
|---|---|
| `verify_php_compat.py` | **6/6** ✓ |
| `verify_audit_fixes.py` | 14/14 ✓ |
| `verify_integrity.py` | 22/22 ✓ |
| `verify_php.py` | 26/26 ✓ |
| `verify_round6.py` | 42/42 ✓ |
| `verify_installer.py` | 18/18 ✓ |
| `verify_launcher.sh` | 11/11 ✓ |

> 过程中 `verify_php.py` 一度失败：**端口 8099 被上一轮遗留的 php 进程占用**
> （pid 31960）。杀掉后 26/26。属既有已知坑，非本次改动引入。

## 八、文档

- `docs/宝塔面板部署指南.md` 新增 **§零「PHP 版本必须是 8.0 以上」**（放在最前，
  因为这是白屏级问题），含症状、原因、宝塔操作路径、以及为什么不降级适配。
- `docs/更新升级与完整性检查.md` 故障速查表加了这一条，自检清单加入 compat 脚本。
- `README.md` 补 PHP 版本要求与那句可搜索的报错原文。
