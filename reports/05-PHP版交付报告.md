# PHP 版交付报告（与 Python 版功能同款）

- **交付物**：`php/`（整站 PHP 实现，服务端渲染）+ `run-php.sh` + `tools/verify_php.py`
- **远端**：`github.com/2016xyz/kwrt-clone` @ `581e3a0`（已推送）
- **复验**：`python3 tools/verify_php.py` —— **25 项全部通过**（自动起停独立实例）
- **日期**：2026-09-25

---

## 一、TL;DR

| 项 | 结论 |
|---|---|
| 实现形态 | 8 个页面 + 15 个管理模块，**PHP 模板服务端渲染**，不依赖旧版 `web/` 静态页与 Vue 单页 |
| 与 Python 版数据互通 | **可共用同一个 `users.db`** —— 口令哈希逐字节一致，实测互相可验证 |
| 域名绑定 | 完成。所有绝对链接改用绑定域名；Host 白名单外 400 |
| CDN | 完成。静态资源走 CDN + 版本号 + 长缓存；**真实源 IP 带防伪造判定** |
| 页面开关 | 完成。一处判定全站生效，导航自动隐藏，接口不受影响，带自锁保护 |
| App / 公众号 | 完成。PWA（可安装到桌面）+ App 下载与公众号入口弹层 |
| 验证强度 | 25 项自动化断言 + 浏览器实测（**控制台 0 错误**）+ Python 侧无回归 |
| **未验证** | **本机真编固件未跑通**（磁盘仅 1.5 GB，需约 2.6 GB），只验到「入队→抢槽→拉起 worker」 |

---

## 二、跨语言兼容（本次最关键的一项）

两版必须能共用同一个库，否则「同款」是空话。**实测结论：可以。**

```
固定盐 salt456、口令 pw：
  PHP    hash_pbkdf2('sha256', $pw, $salt, 240000)
       → pbkdf2_sha256$240000$salt456$6e11e44ecba087b771d2b19dbe8add97045d502e328cd90a5a622afef94e3efa
  Python hashlib.pbkdf2_hmac('sha256', pw, salt, 240000).hex()
       → pbkdf2_sha256$240000$salt456$6e11e44ecba087b771d2b19dbe8add97045d502e328cd90a5a622afef94e3efa
  逐字节相同 ✓
```

| 场景 | 实测结果 |
|---|---|
| PHP 用固定盐生成 | 与 Python 输出**完全相同** |
| PHP 验证 Python 写的哈希 | 通过 ✓ |
| PHP 用错误口令验证 | 拒绝 ✓ |
| 历史无盐 SHA256 | 通过且标记需升级，登录后自动改写为 PBKDF2 ✓ |
| 新注册用户落库形态 | `pbkdf2_sha256$240000$…` ✓ |

表结构同样是逐字对齐 Python 版（并额外补了 `rate_limits` / `login_fails` 两张表）。

---

## 三、三项功能需求

### 3.1 域名绑定

新增设置组「域名与 CDN」：`site.domain` / `site.scheme` / `site.trusted_hosts` /
`site.allow_any_host` / `site.force_https`。

**为什么这不是「加个配置项」**：验证邮件链接、下载链接、支付宝 `notify_url`
都是绝对 URL。若用请求头 `Host` 去拼，攻击者带 `Host: evil.example` 就能让
**邮件里出现指向攻击者域名的登录链接** → 账号接管。所以绑定域名 + Host 白名单
是防注入的必要手段。

| 实测项 | 结果 |
|---|---|
| 绑定 `fw.example.com` 后自链接 | `http://fw.example.com/assets/css/app.css?v=…` ✓ |
| 伪造 `Host: evil.example` | **400** ✓ |
| 返回内容中是否含 `evil.example` | **0 处** ✓ |
| 从本机 `127.0.0.1` 访问后台 | **200**（未被锁死）✓ |

> **★ 过程中发现并修掉的自锁 bug**：绑定域名后白名单只剩该域名，
> 于是从本机访问也被 400 —— **包括管理后台本身**，即「设了域名就把自己锁死
> 且改不回来」。现改为**回环地址永远放行**，内网 IP 落在可信网段内也放行。
> 回环放行不削弱防护：外网 Host 仍是攻击者可控、仍受白名单约束。

### 3.2 CDN

| 实测项 | 结果 |
|---|---|
| 静态资源改走 CDN | `https://cdn.example.com/assets/css/app.css?v=v2026.09` ✓ |
| 静态资源缓存头 | `public, max-age=31536000, immutable` + `CDN-Cache-Control` + `Cloudflare-CDN-Cache-Control` ✓ |
| HTML 页面缓存头 | `no-cache, must-revalidate` ✓ |
| CSP 自动放行 CDN | `script-src 'self' https://cdn.example.com …` ✓ |

> Python 版原先对静态资源发 `no-cache, must-revalidate`，挂 CDN 等于白挂 ——
> PHP 版按「是否版本化」分流，这是接 CDN 的前提。

#### ★ 真实源 IP：CDN 场景下最容易出事的地方

本站有两处**以源 IP 为安全依据**：封禁与限速。挂 CDN 后 `REMOTE_ADDR` 变成节点 IP：

- 封禁会把 CDN 节点整个封掉 → **全站用户一起 403**
- 限速会把所有用户当成一个人 → **正常用户被误伤**

但 `X-Forwarded-For` **客户端可伪造**，无脑信任它，发一个
`X-Forwarded-For: 1.2.3.4` 就能任意换身份，**封禁与限速同时失效 —— 比不处理更糟**。

判定顺序（`php/src/Net.php`）：

```
对端不在「可信代理/CDN 网段」内 → 一律用 REMOTE_ADDR，转发头一概不信
对端可信 → 依次看 CF-Connecting-IP / True-Client-IP / X-Real-IP /
           X-Client-IP / Fastly-Client-IP / X-Azure-ClientIP
        → 再退到 X-Forwarded-For，右往左跳过可信代理取第一个不可信地址
```

| 实测项 | 结果 |
|---|---|
| 对端可信 + `X-Forwarded-For: 1.2.3.4` | 取到 `1.2.3.4` ✓ |
| 对端可信 + `CF-Connecting-IP: 5.6.7.8` | 取到 `5.6.7.8` ✓ |
| 对端不可信 + 伪造 XFF | 取到 `127.0.0.1`（**伪造被无视**）✓ |
| 对端不可信 + 伪造 CF-Connecting-IP | 取到 `127.0.0.1`（**伪造被无视**）✓ |
| `XFF: 9.9.9.9, 127.0.0.5, 127.0.0.9` | 取到 `9.9.9.9`（跳过可信代理）✓ |

### 3.3 页面开关（屏蔽某个页面）

逐页开关：首页 / 软件库 / 插件提议 / 为爱发电 / 联系我们 / 登录 / 注册 / 固件下载。

**判定收在一处**（`php/src/Pages.php` 的「路径 → 开关键」表），由路由层在分发前
统一执行 —— 让每个控制器自己判断必然漏，加页面的人不知道要加判断。

| 实测项 | 结果 |
|---|---|
| 关闭「插件提议」 | `/newpkg/` → **404** ✓ |
| 导航与页脚 | 入口自动隐藏（页面源码中 `href="/newpkg/"` 命中 **0**）✓ |
| 对应接口 | `/api/v1/proposals` 仍 **200**（关页面不连带关功能）✓ |
| 恢复后 | 200 ✓ |
| 关闭「登录」不带确认 | **409 `LOCKOUT_RISK`** 明确提示风险 ✓ |
| 带 `confirm_lockout=1` 后 | 生效，且 `/admin/` 仍 **200**（不会自锁）✓ |

`/admin/`、`/api/`、静态资源**永不受开关管辖**，否则关错一个开关就再也进不去后台。

### 3.4 App 与公众号

- **App 下载 / 公众号入口**：页头页脚入口 + 弹层；二维码优先用配置的图片，
  其次配置的生成服务，都没有则前端离线生成（不请求第三方）
- **「打包成 App」用 PWA**：自动提供 `/manifest.webmanifest` 与 `/service-worker.js`，
  可「添加到主屏幕」全屏独立运行；SW 缓存静态资源与离线页但**不缓存接口**
  （`/api/` 一律走网络），避免拿到过期的构建状态
- 要上应用商店用 WebView 壳指向本站即可，功能与网页版一致，不必重写客户端

---

## 四、PHP 相对 Python 的三处真实改进

| 项 | Python 版 | PHP 版 |
|---|---|---|
| 限速 / 登录锁定 | 进程内 dict，**多 worker 下是分片计数** | 落 `rate_limits` / `login_fails` 表，多 worker 正确 |
| 构建队列 | 进程内队列，**只能单进程运行** | `jobs` 表 + 一条原子 UPDATE 抢槽 + 独立 worker 进程，多 worker 正确 |
| 路径解析 | 从 cwd 解析根目录，systemd 必须设 `WorkingDirectory` 否则链路静默失效 | 从 `__DIR__` 反推绝对路径，不依赖 cwd |

---

## 五、设置项单一事实来源

新增 37 项设置（域名/CDN 9、页面开关 11、公众号与 App 14、支付补充 3），
统一加在 **Python 侧** `app/sitesettings.py`，再由生成脚本产出 PHP 侧：

```bash
python3 php/scripts/gen_settings_schema.py            # 生成
python3 php/scripts/gen_settings_schema.py --check     # CI 校验是否漂移
```

设置项共 **113 项 / 12 分组**。手抄两份必然漂移，故不做。

---

## 六、复验清单（25 项，全部通过）

| 项 | 内容 | 结果 |
|---|---|---|
| P-1 | 公开页面全部 200 且内容非空 | ✓ |
| P-2 | 公开接口/静态资源全部 200 | ✓ |
| P-3 | Content-Type 正确（不被兜底覆盖成 `text/html`） | ✓ |
| P-4 | PHP/Python 口令哈希逐字节一致 | ✓ |
| P-5 / P-5b | 注册→登录通过/错误密码被拒；新用户落库为 PBKDF2 | ✓ |
| P-6 | 历史无盐哈希登录后自动升级 | ✓ |
| P-7 | 跨站写请求被拒（CSRF） | ✓ |
| P-8 | 越权与路径穿越全部被拒 | ✓ |
| P-9a / P-9 | 管理员登录；页面开关（404/导航隐藏/接口不受影响/可恢复） | ✓ |
| P-10 | 关闭登录需二次确认；后台保持可达 | ✓ |
| P-11 | 域名绑定：自链接用绑定域名、伪造 Host 被拒不泄漏、本机不被锁死 | ✓ |
| P-12 | 源 IP：对端可信才采信转发头；不可信时伪造头全失效 | ✓ |
| P-13 / P-13b | CDN：走 CDN+版本号、HTML 不缓存、CSP 放行 CDN、长缓存 immutable | ✓ |
| P-14 / P-14b | 管理后台 15 模块渲染；设置中心 12 分组 | ✓ |
| P-15 / P-16 | 后台接口不回显密钥明文；审计日志不记密钥值 | ✓ |
| P-17 | 构建入参 6 个注入样本全部被拒 | ✓ |
| P-18 / P-18b | 孤儿识别；清理孤儿**不误删用户上传** | ✓ |
| P-19 | 构建队列抢槽（queued → running） | ✓ |
| P-20 | PHP 设置 schema 与 Python 侧同源未漂移 | ✓ |

**浏览器实测**（Playwright + 系统 Chromium）：6 个公开页 + 后台 15 模块 +
设置中心 12 分组，**控制台错误 0**；登录全链路通过。

**Python 侧无回归**：`verify_round5` 13/13、`extract_regression` 12/12、
`verify_u_fixes` 10/10、`healthz` 正常；真实 `users.db` 未被测试污染。

---

## 七、过程中修掉的自身缺陷（都有实测依据）

| 缺陷 | 现象 | 修法 |
|---|---|---|
| Router 的 `callable` 类型 | 不接受「类名 + 非静态方法」，路由注册即抛 | 自行解析并实例化控制器 |
| `json_out()` 只 echo 不返回 | 方法声明 `: string` 却无 return → **所有 JSON 端点 500** | 发出响应即终结请求，一个点解决全部端点 |
| 内置服务器缺 router.php | `/healthz`、`/manifest.webmanifest` **全部 404** | 新增 `php/public/router.php` |
| 兜底覆盖 Content-Type | SW 以 `text/html` 下发，浏览器拒执行 | 控制器已声明类型则不覆盖 |
| HTML 无 Cache-Control | 挂 CDN 后页面被边缘节点缓存，「刚关掉的页面」仍在各节点可见 | 显式 `no-cache` |
| 默认 logo / favicon 缺失 | 404 + 控制台报错 | 补真实 PNG |
| 绑定域名后本机被 400 | **管理后台也进不去，改不回来** | 回环地址永远放行 |
| **验证脚本自身两处假绿** | ① cookie 只取最后一个 `Set-Cookie`（拿到 csrf 而非会话 cookie，管理接口全 403）；② `/admin/` 302 到登录页也算 200 | 取具名 cookie；断言「确实是后台页面」 |

> 最后一条值得单独说：**断言写得不对，会让人以为代码坏了**（或反过来，
> 以为代码好了）。这次是脚本自己先错，导致 9 项「失败」全是假的。

---

## 八、未验证 / 需确认

1. **【未验证】本机真编固件**
   只验证到「入队 → 抢槽 → 拉起 worker」。真正的 `make image` 需要约 **2.6 GB**
   磁盘（工作目录 2 GB + 余量），本机当前仅 **1.5 GB** 可用，无法跑通。
   **故不对编译产物做任何断言。** 磁盘充足的环境请跑一次完整构建再上线。
2. **【需确认】生产部署形态**
   Nginx + PHP-FPM 的推荐配置已写在 `php/README.md`。请确认是否用
   `try_files` 方案、以及是否需要多机部署（PHP 版队列与限速已按多机正确设计）。
3. **【需确认】CDN 回源网段**
   `php/README.md` 列了 Cloudflare 常用段，但**请以 CDN 官方公布的当前列表为准**；
   填错会导致源 IP 判定失效（封禁/限速不准）。后台「总览」页会实时显示判定结果，
   可据此核对。
4. **【需确认】公众号 AppID 的用途**
   `entry.wechat_appid` 目前仅作展示/预留，未接入微信登录或消息推送。
   若要接，需要走微信开放平台授权（另需服务号 + 域名备案）。
5. **【需确认】支付回调地址**
   绑定域名后 `notify_url` 会自动用绑定域名生成，需确认该域名公网可达
   且已备案，否则支付宝回调打不进来。

---

## 九、部署速查

```bash
# 试跑（内置服务器）
./run-php.sh

# 初始化首个管理员
KWRT_ADMIN_PASSWORD='强密码' php php/scripts/init_admin.php

# 等价手写命令（★ 必须带 router.php）
php -S 0.0.0.0:8080 -t php/public php/public/router.php

# 自检
python3 tools/verify_php.py
```

生产用 Nginx/Apache + PHP-FPM，完整配置（含域名绑定、CDN、页面开关、
PWA/App、公众号、目录结构、安全须知）见 **[php/README.md](../php/README.md)**。
