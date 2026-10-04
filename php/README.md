# Kwrt PHP 版

与 Python 版**功能同款**的 PHP 实现：整站服务端渲染（PHP 模板），
不依赖旧版 `web/` 静态页与 Vue 单页。

## 与 Python 版的关系

| | Python 版 | PHP 版 |
|---|---|---|
| 目录 | `app/` + `web/` | `php/` |
| 前端 | Vue 3 单页（静态 HTML） | **PHP 模板服务端渲染** |
| 数据库 | `users.db`（SQLite） | **同一个 `users.db`** |
| 口令格式 | `pbkdf2_sha256$240000$盐$哈希` | **逐字节相同，可互相验证** |
| 运行 | uvicorn（单进程） | PHP-FPM / 内置服务器（多进程安全） |
| 构建队列 | 进程内队列（只能单进程） | **jobs 表 + 独立 worker（多 worker 正确）** |
| 限速计数 | 进程内 dict（多 worker 会分片） | **落库，多 worker 也正确** |
| 默认端口 | 8443 | 8080 |

**两版可以指向同一个库、同一份 `store/`、同一份 `data/`。**
口令哈希格式实测逐字节一致，所以 Python 建的用户能直接在 PHP 版登录，反之亦然。

设置项定义是**单一事实来源**：改 `app/sitesettings.py` → 跑生成脚本 → PHP 侧自动同步。

```bash
python3 php/scripts/gen_settings_schema.py           # 生成
python3 php/scripts/gen_settings_schema.py --check    # CI 校验是否漂移
```

---

## 一、快速开始

### 内置服务器（最快，适合试跑与内网）

```bash
# 必须带上 router.php，否则内置服务器会把 /healthz、/manifest.webmanifest
# 这类没有实体文件的路径直接 404（index.php 根本没机会运行）
php -S 0.0.0.0:8080 -t php/public php/public/router.php
```

或直接用附带脚本：

```bash
./run-php.sh              # 默认 8080
PORT=9000 ./run-php.sh
```

访问 <http://127.0.0.1:8080>。

### 创建首个管理员

```bash
KWRT_ADMIN_PASSWORD='你的强密码' php php/scripts/init_admin.php
```

（测试用脚手架：`KWRT_DB=/tmp/test.db php php/scripts/make_admin.php <用户名> <密码>`）

> ⚠️ `make_admin.php` 会**先删除再重建**目标用户 —— 它是销毁性脚本，不是"创建"脚本。
> 因此它强制要求：显式给出用户名与口令、显式设置 `KWRT_DB` 指向测试库；
> 若 `KWRT_DB` 看起来像生产库，还要再加 `--i-know-this-is-destructive`。
> **生产环境初始化管理员请用 `init_admin.php`。**

### Nginx + PHP-FPM（生产推荐）

```nginx
server {
    listen 80;
    server_name fw.example.com;
    root /srv/kwrt/php/public;
    index index.php;

    # 静态资源：交给 CDN 或至少长缓存
    location ~* \.(css|js|png|jpe?g|svg|webp|ico|woff2?)$ {
        expires 1y;
        add_header Cache-Control "public, max-age=31536000, immutable";
        try_files $uri =404;
    }

    location / {
        try_files $uri $uri/ /index.php?$query_string;
    }

    location ~ \.php$ {
        include fastcgi_params;
        fastcgi_pass unix:/run/php/php8.2-fpm.sock;
        fastcgi_param SCRIPT_FILENAME $document_root$fastcgi_script_name;
        # ★ 让 PHP 能拿到真实源 IP（配合后台「可信代理/CDN 网段」设置）
        fastcgi_param REMOTE_ADDR $remote_addr;
        fastcgi_param HTTP_X_FORWARDED_FOR $proxy_add_x_forwarded_for;
    }

    # 绝不能暴露的东西
    location ~* \.(db|sqlite|sqlite3|env|log)$ { deny all; }
    location ~ ^/(src|templates|scripts)/      { deny all; }
}
```

### Apache

仓库已带 `php/public/.htaccess`（Rewrite + 静态资源长缓存 + 敏感路径屏蔽）。
确认 `AllowOverride All` 且启用了 `mod_rewrite`、`mod_expires`、`mod_headers`。

### 环境变量

| 变量 | 说明 |
|---|---|
| `KWRT_DB` | 数据库路径，默认 `<项目根>/users.db` |
| `KWRT_DEBUG=1` | 开发模式：回显错误细节（**生产禁用**） |
| `KWRT_ADMIN_PASSWORD` | 初始化管理员密码 |

---

## 二、域名绑定

后台「站点设置 → **域名与 CDN**」。

| 设置项 | 说明 |
|---|---|
| `site.domain` | 绑定域名，如 `fw.example.com`。**填了之后所有绝对链接都用它生成** |
| `site.scheme` | `auto` / `https` / `http` |
| `site.trusted_hosts` | Host 白名单（每行一个）。不在名单里的 Host 一律 400 |
| `site.allow_any_host` | 未绑域名时是否接受任意 Host（正式上线建议关） |
| `site.force_https` | 开启后 http 301 到 https |

### 为什么必须绑定而不是靠请求头

验证邮件链接、下载链接、支付宝 `notify_url` 都是**绝对 URL**。
如果拿请求头里的 `Host` 去拼，攻击者只要在请求里带
`Host: evil.example`，就能让**发给用户的邮件里出现指向攻击者域名**的链接
→ 诱导登录 → 账号接管。所以绑定域名 + Host 白名单是必须的，不是可选优化。

> **实测踩过的坑（已修）**：绑定域名后，白名单里只剩该域名，
> 于是从服务器本机（`127.0.0.1`）访问也会被 400 拒绝 —— 包括管理后台本身，
> 结果是「设了域名立刻把自己锁在门外且改不回来」。
> 现在**回环地址永远放行**，且内网 IP 只要落在可信网段里也放行。

---

## 三、CDN

后台同一个分组里开启 `site.cdn_enabled` 并填 `site.cdn_base`（如 `https://cdn.example.com`）。

开启后会发生三件事：

1. **静态资源全部改走 CDN 域名**，并自动带 `?v=<站点版本号>`
   （改 `site.asset_version` 即可让 CDN 上的旧文件立即失效）
2. **缓存头分流**：
   - 版本化静态资源 → `Cache-Control: public, max-age=31536000, immutable`
     （另附 `CDN-Cache-Control` 与 `Cloudflare-CDN-Cache-Control`，兼容各 CDN）
   - 页面 HTML → `no-cache, must-revalidate`（保证改设置立即生效）
3. **CSP 自动把 CDN 域名加进 `script-src` / `style-src` / `img-src`**，
   否则浏览器会把 CDN 上的 JS/CSS 拦掉。

> Python 版原先对静态资源发的是 `Cache-Control: no-cache, must-revalidate`，
> 挂 CDN 等于白挂。PHP 版按「是否版本化」分流，这是接 CDN 的前提。

### ★ 真实源 IP：CDN 场景下最容易出事的地方

本站有两处以源 IP 为**安全依据**的逻辑：**封禁**与**限速**。
挂上 CDN 后 `REMOTE_ADDR` 会变成 CDN 节点 IP，于是：

- 封禁会把 CDN 节点整个封掉 → **全站用户一起 403**
- 限速会把所有用户当成同一个人 → **正常用户被误伤**

所以必须取回真实源 IP。但 `X-Forwarded-For` 是**客户端可伪造**的头，
不加判别地信任它，攻击者发一个 `X-Forwarded-For: 1.2.3.4` 就能任意换身份，
**封禁与限速同时失效 —— 比不处理更糟**。

因此判定顺序是：

```
直连对端（REMOTE_ADDR）不在「可信代理/CDN 网段」内
    → 一律用 REMOTE_ADDR，转发头一概不信
对端可信
    → 依次看 CF-Connecting-IP / True-Client-IP / X-Real-IP / X-Client-IP /
      Fastly-Client-IP / X-Azure-ClientIP
    → 再退到 X-Forwarded-For，从**右往左**找第一个不可信地址
```

请在 `site.trusted_proxies` 里填 CDN 的回源网段。常用参考：

| 服务 | 网段 |
|---|---|
| Cloudflare | `173.245.48.0/20`、`103.21.244.0/22`、`2400:cb00::/32` 等（以官方列表为准） |
| 本机反代 | `127.0.0.0/8` |
| 内网反代 | `10.0.0.0/8`、`172.16.0.0/12`、`192.168.0.0/16` |

**留空则使用内置私网段**（即只信本机与内网反代，不信任何公网 CDN）。
后台「总览」页会实时显示**当前请求判定出的源 IP**与直连对端，可据此核对配置。

---

## 四、页面开关（屏蔽某个页面）

后台「站点设置 → **页面与入口**」。每个页面一个开关：

首页 / 软件库 / 插件提议 / 为爱发电 / 联系我们 / 登录 / 注册 / 固件下载。

- 关闭后该路径**立即**返回所选表现（`404` / 跳转 / 提示页）
- **导航与页脚会自动隐藏该入口**，不用手改模板
- **接口不受页面开关管辖**（关掉 `/newpkg/` 页不影响 `/api/v1/proposals`），
  这样「关页面」不会连带把功能关死
- `/admin/`、`/api/`、静态资源**永远不受开关管辖** —— 否则关错一个开关就自锁
- 关闭「登录」需要显式带 `confirm_lockout=1` 二次确认，后台也会一直保持可达

判定收在**一处**（`php/src/Pages.php` 的「路径 → 开关键」表），
由路由层在分发前统一执行。新增页面只在表里补一行即可，不会漏。

---

## 五、App 与公众号

后台「站点设置 → **公众号与 App**」。

### App 下载入口

填 `entry.app_android_url` / `entry.app_ios_url`（APK 直链或应用商店地址），
`entry.app_enabled` 打开后，页头与页脚出现「App 下载」，弹层内给出下载按钮与二维码。

二维码有三种来源，按优先级：
1. `entry.app_qrcode_url` 直接给图片（最可控）
2. `entry.qrcode_api` 显式配置第三方生成服务
3. 都没有 → **前端离线生成**，不请求任何第三方

### 公众号入口

`entry.wechat_enabled` + `entry.wechat_qrcode_url` + `entry.wechat_official_name` /
`entry.wechat_follow_hint`。

### ★ 关于「打包成 App」

本站提供的是 **PWA**（渐进式 Web App），这是把网站变成 App 的正解：

- 开启 `entry.pwa_enabled` 后自动提供 `/manifest.webmanifest` 与 `/service-worker.js`
- 用户在手机浏览器里「添加到主屏幕」后，即可**全屏独立运行**，与原生 App 观感一致
- Service Worker 会缓存静态资源与离线页（`/offline.html`）**但不缓存接口**，
  避免拿到过期的构建状态

真正要发到应用商店的话，用 WebView 壳包一层即可（指向本站域名）——
功能与网页版完全一致，不需要另写一套客户端。**不建议**直接把整站塞进 APK：
那样每次改设置都要重新发版。

---

## 五之二、固件版本与第三方插件源

### 分支号 ≠ 发布号（一个真用户踩得到的坑）

界面的「固件分支」下拉框给的是**分支号**（`25.12`），而镜像站只认**发布号**（`25.12.5`）：

```
https://downloads.openwrt.org/releases/25.12/targets/x86/64/      → 404
https://downloads.openwrt.org/releases/25.12.5/targets/x86/64/    → 200
```

提交构建时会经 `Releases::resolveForBuild()` 解析：

| 提交值 | 解析结果 |
|---|---|
| `25.12` / `25.12.5` | `25.12.5`（apk 后端） |
| `24.10` / `24.10.8` | `24.10.8`（opkg 后端） |
| `23.05` / `23.05.6` | `23.05.6`（opkg 后端） |
| 其他/空 | 默认最新分支 |

解析规则与 Python 版 `app/releases.py` **逐条同源**：只接受 `major.<1–3 位数字>`，
所以 `25.120` 不会被误判进 25.12 分支；请求 `25.12.9` 也**不会**被静默换成 `25.12.5`
（那样用户以为按指定版本构建、实际不是）。

> **踩坑记录**：这个缺陷是「测试取值与真实路径不一致」导致的 ——
> 真编测试脚本自己传的是发布号 `25.12.5`，一路绿灯；
> 而用户从界面提交的是分支号 `25.12`，必然卡在「无法下载 ImageBuilder」。
> 现在测试用例**显式断言**「提交值必须等于界面下拉框的取值」，并对不上就报错。

### 第三方插件源

`luci-app-openclash`、`luci-app-passwall`、`luci-theme-argon` 等**不在官方仓库**里，
只有 kiddin9 feed 提供（实测其 1084 个包中包含 `luci-app-openclash`）。

```
https://dl.openwrt.ai/packages-{分支}/包架构/kiddin9
```

- 仅 **opkg** 后端接入（apk 版 ImageBuilder 用不了 .ipk 格式的上游 feed）。
- 接入时会把 `repositories.conf` 里的 `option check_signature` 注释掉 ——
  第三方源的包通常未按官方密钥签名，不关校验 opkg 会直接拒装。
- 勾选了这类插件但当前是 apk 后端（25.12）时，会**自动回落到 opkg 后端（24.10）**，
  并在提交响应里返回 `note` 说明回落原因，避免用户以为按所选版本构建。

> **它是第三方基础设施**：该 feed 由他人运营，可用性与长期稳定性不由本项目保证。
> 不希望依赖它，就不要在构建里勾选上述插件（其余 108 个预设走官方源，不受影响）。

## 六、目录结构

```
php/
├── public/                  Web 根目录（Nginx/Apache 指到这里）
│   ├── index.php            前端控制器：引导 → 安全前置 → 分发
│   ├── router.php           PHP 内置服务器的路由垫片（php -S 必须带）
│   ├── .htaccess            Apache 规则
│   ├── static/              logo 等站点图片
│   └── assets/              CSS / JS（可被 CDN 缓存）
├── src/
│   ├── Db.php               PDO(SQLite)：建表、事务、WAL
│   ├── Config.php           config.json + 绝对路径解析
│   ├── Net.php              ★ 域名绑定 / CDN / 真实源 IP / 缓存头 / 安全头
│   ├── Pages.php            ★ 页面开关（路径 → 开关键的统一判定）
│   ├── Settings.php         设置中心（校验 / 编解码 / 密钥屏蔽）
│   ├── SettingsSchema.php   由 Python 侧生成，勿手改
│   ├── Auth.php             PBKDF2 口令 / 会话 / CSRF / 限速 / 封禁
│   ├── Router.php           极简路由器（含控制器实例化）
│   ├── View.php             PHP 模板渲染
│   ├── Catalog.php          软件包目录
│   ├── Builder.php          构建队列（jobs 表 + 抢槽 + 独立 worker）
│   ├── Engine.php           ImageBuilder 编译流程
│   ├── Artifacts.php        构建物盘点 / 删除 / 孤儿清理
│   ├── Download.php         限时下载令牌（HMAC）
│   ├── Mailer.php           SMTP + HTML 邮件模板
│   ├── Pay.php              支付宝当面付
│   ├── Util.php             入参白名单 / 路径归属 / 文件名清洗
│   ├── helpers.php          模板辅助函数
│   └── Controllers/         PublicController / AuthController / ApiController / AdminController
├── templates/               PHP 模板（页面 + 邮件）
└── scripts/
    ├── gen_settings_schema.py  从 Python 侧生成设置 schema
    ├── build-worker.php        构建 worker（由队列 nohup 起来）
    ├── make_admin.php          测试用：重置管理员（销毁性，需 KWRT_DB 指向测试库）
    └── init_admin.php          生产用：初始化首个管理员
```

---

## 七、与 Python 版的差异（有意为之）

| 项 | 说明 |
|---|---|
| **无状态会话** | 不用 PHP session（`session_start`），改用自管 cookie + `sessions` 表。好处：多机部署不需要共享 session 存储 |
| **限速落库** | Python 版用进程内 dict，多 worker 下会分片计数；PHP 版落 `rate_limits` / `login_fails` 表，多 worker 正确 |
| **构建队列落库** | 同上，队列状态在 `jobs` 表，抢槽用一条原子 UPDATE，多 worker 不会重复取任务 |
| **路径不依赖 cwd** | Python 版从 cwd 解析根目录（systemd 必须设 `WorkingDirectory`，否则静默失效）；PHP 版从 `__DIR__` 反推绝对路径 |
| **页面开关集中判定** | 见第四节 |
| **模板默认转义** | 模板里必须写 `<?= e($x) ?>`；要输出原始 HTML 才写 `raw()`，放行是显式的 |

---

## 八、安全须知

- `KWRT_DEBUG` 生产必须关（否则 500 页面会回显堆栈）
- `users.db` 与 `php/` 源码目录**绝不能**放在 web 根下；`.htaccess` 已兜一层，Nginx 需自行加 `deny`
- 绑定域名后请核对后台「总览」里的源 IP 判定结果，确认 CDN 段配置正确
- 关闭「登录」页面会带来自锁风险，操作前确认能从服务端恢复
- 上传自定义文件包默认仅赞助用户可用（`store/uploads/_staged/<user>/`），
  且**不参与**构建物清理
