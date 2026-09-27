# Kwrt 在线定制站

对 `https://openwrt.ai/?target=x86/64&id=generic` 的**功能完整复刻** —— 设备检索、固件定制、实时编译、产物下载、管理员控制台全链路可跑。

所有能力均为**真实实现**：真实 Vue 3 渲染、真实 OpenWrt ImageBuilder 编译、真实 SMTP 投递、真实 GitHub Actions 派发、真实限时下载令牌。没有静态页面，没有模拟数据。

---

### ⚠ PHP 版本要求：8.0 或更高

低版本（如宝塔常见默认的 7.4）会让**整站白屏**，报

```
Fatal error: Dynamic class names are not allowed in compile-time ::class fetch
```

看到这句就是 PHP 7.x —— 宝塔：网站 → 设置 → PHP 版本 → 选 8.0+。
现在版本不对时入口会先打印可读提示，不再抛那句 Fatal error。
机械守卫：`python3 tools/verify_php_compat.py`（会真的用 PHP 7.4 容器跑一遍）。


## ★ 安装向导（PHP 版）

PHP 版有网页安装向导，**不用碰终端**：

```
https://你的域名/install.php
```

四步：环境检查 → 数据库（SQLite 或 MySQL）→ 管理员 → 站点设置。
装完写 `data/install.lock`，向导从此拒绝运行；库里已有管理员时
即使删掉锁文件也会拦截（防接管）。详见 `docs/宝塔面板部署指南.md` §五。


## 一、核心特性

### 前台

| 能力 | 说明 |
|------|------|
| 设备检索 | 1255 台设备离线数据集，模糊匹配 + 高亮 + 键盘上下选择 |
| 预编译镜像下载 | 按类型排序、sha256 展示，经 `/dl` 中转真实下载 |
| 在线定制 | 119 个常用软件包（10 分类 / 12 套件）+ 软件包筛选 + 自定义包名（支持 `-包名` 移除） |
| 网络配置 | 主机名 / LAN IP / 端口 / 时区 / 接入方式（主路由·旁路由·AP·桥接） |
| 服务配置 | PPPoE、WIFI、IPv6、HTTPS、Web 服务器、主题 |
| 高级选项 | 根分区容量、文件系统（squashfs/ext4）、启动方式（EFI/Legacy）、VMDK |
| 自定义扩展 | uci-defaults 脚本、自定义文件包上传（tar.gz 解压进 rootfs） |
| 构建追踪 | 实时进度轮询，可关闭页面后台继续 |
| 限时下载 | 构建完成签发带有效期的下载链接，过期返回 410 并给出重新生成入口 |
| 账户体系 | 注册 / 登录 / 会话，赞助态解锁高级定制项 |
| 赞助流程 | 套餐与金额由管理员配置，支持自助确认或提交审核 |
| 多语言 | 11 种语言（zh-cn/en/ca/es/de/fr/it/no/pl/tr/ko） |

### 管理员控制台（15 个模块）

| 模块 | 能力 |
|------|------|
| 总览 | 用户/构建/队列/磁盘/赞助/下载链接 KPI，运行状态一览 |
| 用户管理 | 创建 / 改密 / 角色升降 / 配额 / 停用 / 踢下线 / 删除 |
| 构建任务 | 列表 / 取消（真实杀进程组）/ 重试 / 删除产物 / 重发下载链接 |
| 本地构建物 | 磁盘产物逐个可见（占用/文件数/属主/孤儿标记），可单删或**清理孤儿目录**，删产物同时作废相关下载令牌 |
| 队列存储 | 并发调整、引擎启停、清空产物；**本地 / GitHub 双构建后端切换** |
| 站点设置 | **61 项配置 / 8 分组**，schema 驱动表单，分分组保存 |
| 邮件通知 | SMTP 配置（SSL/STARTTLS/明文）、真实发送测试、投递记录；邮件走 **HTML 模板**（`app/templates/mail/`），纯文本作为降级分支 |
| 下载链接 | 有效期策略、次数上限、吊销 / 续期 / 重发 / 批量吊销 / 清理 |
| 赞助管理 | 可视化套餐编辑器（增删套餐、改金额与权益）、申请审核、统计 |
| 插件提议 | 审核 / 拒绝 / 回复 |
| 封禁 | 按 IP 或用户名 |
| 审计日志 | 全部管理写操作留痕（含来源 IP） |

### 双构建后端

| 后端 | 机制 |
|------|------|
| **本机** | 下载官方 ImageBuilder，`make image` 实编，68–92 秒出 6 个可启动镜像 |
| **GitHub** | `workflow_dispatch` 派发 → 轮询 run → 产物回传本站 → 签发限时链接 |

GitHub 后端未配置时**安全回落**到本机，不会因配置不全导致站点无法构建。

---

## 二、技术栈

- **后端**：FastAPI + Uvicorn，SQLite（`users.db`）
- **前端**：Vue 3（组合式 API）+ jQuery + 原生 CSS 设计系统
- **构建**：OpenWrt 官方 ImageBuilder
- **依赖本地化**：Vue 3 / jQuery 存放于 `web/vendor/`，**不依赖公网 CDN**

### 设计系统

`web/assets/css/app.css` 是自建设计系统，非引入框架：

- **配色心理学**：蓝＝信任（固件类产品第一信任色）／青绿＝进展／绿＝成功／琥珀＝警示／红＝危险；暗色主题用深石板蓝（`#080e1a`）而非纯黑，避免 OLED 高对比文字光晕
- **明暗双主题**：`auto`／`light`／`dark` 三态，跟随系统 + 本地持久化
- **响应式**：375 / 768 / 1280px 三端实测零横向溢出；移动端表格自动转卡片列表
- **组件库**：15 类组件、40+ 变体（按钮 6 类 × 5 尺寸、表单、卡片、表格、标签页、抽屉、弹窗、Toast、骨架屏、进度条）
- **无障碍**：`:focus-visible` 焦点环、`prefers-reduced-motion` 关闭动画、`aria-label`

---

## 三、快速开始

> **本仓库包含两套功能同款的实现**：
> Python 版（`app/` + `web/`，Vue 单页）与 **PHP 版（`php/`，服务端渲染）**。
> 两版**共用同一个 `users.db`** —— 口令哈希格式逐字节一致，数据可互通。
> 需要部署到只有 PHP 的主机、或想用 PHP 模板渲染前端时，走 PHP 版。

### PHP 版

```bash
./run-php.sh                                   # 内置服务器，默认 8080
php php/scripts/init_admin.php                 # 初始化首个管理员
php -S 0.0.0.0:8080 -t php/public php/public/router.php   # 等价手写命令
```

生产用 Nginx/Apache + PHP-FPM，配置见 **[php/README.md](php/README.md)**
（含域名绑定、CDN、页面开关、PWA/App 下载、公众号入口的完整说明）。

自检：

```bash
python3 tools/verify_php.py           # 25 项断言，自动起停服务
python3 tools/verify_php_build.py     # 真编固件端到端（需约 2.6 GB 磁盘）
BASE=http://127.0.0.1:8443 python3 tools/verify_python_features.py   # Python 侧域名/CDN/页面开关
```

设置项定义以 Python 侧为单一事实来源，改完要重新生成 PHP 侧：

```bash
python3 php/scripts/gen_settings_schema.py           # 生成
python3 php/scripts/gen_settings_schema.py --check    # 校验是否漂移
```

### Python 版一键安装（推荐）

```bash
git clone https://github.com/2016xyz/kwrt-clone.git && cd kwrt-clone
sudo ./install.sh
```

一条命令完成：识别发行版 → 装系统依赖 → 建 venv 装 Python 依赖 →
写 systemd 服务 → 生成初始管理员密码 → 启动并做健康检查。

装完会直接打印访问地址与管理员密码：

```
==> 安装完成

  访问地址   http://<你的IP>:8443/
  管理后台   http://<你的IP>:8443/admin/

  管理员账号 admin
  管理员密码 <随机生成，同时保存在 data/INITIAL_ADMIN.txt>
```

常用参数：

```bash
sudo ./install.sh --port 9000      # 换端口
sudo ./install.sh --host 127.0.0.1 # 只监听本机（配合反向代理）
sudo ./install.sh --no-systemd     # 只装依赖，不起 systemd（前台运行）
sudo ./install.sh --dry-run        # 只打印将执行的命令，不做任何改动
```

**幂等**：重复执行不会覆盖任何已有数据。`users.db` / `store/` / `data/`
一律保留；已存在管理员时**不会重置密码**。

### 升级

```bash
sudo ./update.sh              # git pull + 更新依赖 + 重启
sudo ./update.sh --no-pull    # 代码已手动更新时只重启
```

`update.sh` 明确不触碰 `users.db` / `store/` / `data/` / `kwrt.env`。

### 手动安装

<details>
<summary>不想用 install.sh 时（展开）</summary>

**环境要求**：Python 3.9+（**3.9 与 3.13 均已实测跑通**）；Linux x86_64；
构建功能需要额外的 perl 模块与打包工具。

> ### ⚠ 一定要用 `requirements.txt` 安装，不要手敲包名
>
> 踩过的坑：在 Python 3.9 上执行 `pip install fastapi uvicorn`，装上了**只支持
> Python 3.10+ 的版本**（旧版 pip 不会拦），于是启动时抛出一大段裸 traceback：
>
> ```
> File ".../uvicorn/main.py", line ...
> TypeError: unsupported operand type(s) for |: 'type' and 'NoneType'
> ```
>
> 这类报错**看着像代码坏了，实际是环境装错了**。正确做法：
>
> ```bash
> pip install -r requirements.txt   # 由 pip 按当前解释器解析出兼容版本
> ```
>
> 实测：同样的 Python 3.9，用 `requirements.txt` 会正确装出
> fastapi 0.128.8 / uvicorn 0.39.0（3.9 可用），全部页面 200。
> `run.sh` 现在会**逐个解释器实测导入**，并在失败时按真实错误区分
> 「版本不兼容」与「依赖没装」，而不是丢一句笼统的提示。

```bash
# RHEL / CentOS
dnf install -y perl-FindBin perl-IPC-Cmd perl-Digest-SHA perl-Time-Piece \
  perl-Data-Dumper perl-Thread-Queue perl-IO-Compress perl-Net-SSLeay \
  perl-Archive-Tar perl-ExtUtils-MakeMaker squashfs-tools zstd make gcc gawk unzip tar

# Debian / Ubuntu
apt install -y perl squashfs-tools zstd make gcc gawk unzip tar
```

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt          # 见 requirements.txt（含逐条用途说明）

# 首个管理员密码：环境变量优先，未设置则随机生成并打印一次
export KWRT_ADMIN_PASSWORD='改成你自己的强密码'
python -m uvicorn app.main:app --host 0.0.0.0 --port 8443
```

访问 `http://127.0.0.1:8443`。

> **务必用 systemd 时显式设置 `WorkingDirectory=` 指向项目根目录** ——
> 应用内有按相对路径解析的文件，缺失它会让部分链路（如验证邮件）静默失效。
> `install.sh` 生成的 unit 已包含该项。

启动日志会打印当前生效的并发、后端与邮件配置：

```
[admin] 队列并发 = 2, 构建引擎 = True, 后端 = local（配置：local）
[admin] 邮件通知 = False, 下载链接有效期 = 72 小时
```

</details>

### 管理员账号

站点**不再内置任何演示账号**。第一个管理员由启动逻辑按需创建：

- 若数据库里一个管理员都没有，则创建 `admin`，
  密码取环境变量 `KWRT_ADMIN_PASSWORD`，未设置则随机生成并**打印一次**；
- 已存在管理员时不做任何改动，也不会重置密码。

`install.sh` 会把随机密码同时写入 `data/INITIAL_ADMIN.txt`（权限 600）。

> ⚠️ 登录后请立即在「管理后台 → 用户」修改密码。

### 健康检查

```bash
curl -s http://127.0.0.1:8443/healthz
```

### PWA / SEO（两版同名设置，行为一致）

后台「站点设置 → 公众号与 App」的 `entry.pwa_enabled`（**默认开启**）控制：

| 路由 | 说明 |
|---|---|
| `/manifest.webmanifest` | Web App Manifest（名称/图标/主题色跟随设置） |
| `/service-worker.js` | 离线兜底 + 静态资源缓存 |
| `/offline.html` | 断网回落页（纯静态，不依赖外部资源） |
| `/robots.txt` | **自动 Disallow 已被后台关掉的页面**（关了还让收录 = 自相矛盾） |
| `/sitemap.xml` | 只收录仍开启的公开页面 |

Service Worker 里有两条纪律写死：**`/api/` 一律走网络绝不缓存**（否则用户会拿到过期的
构建状态），**SW 自身绝不长缓存**（否则新版永远发不出去）。

页面里会自动注入 `<link rel="manifest">`、`theme-color` 与 SW 注册脚本 ——
缺了这几行，「添加到主屏幕」拿不到图标名称、断网也没有兜底页。

> 图标 MIME 由扩展名推导（`.svg` → `image/svg+xml`）。
> 给 `.svg` 声明 `image/png`，浏览器会**静默拒绝**该图标 —— PWA 装上是白块，且不报任何错。

### ★ 页面开关 / 域名 / CDN（两版同名设置，行为一致）

后台「站点设置 → 页面与入口 / 域名与 CDN / 公众号与 App」。
判定逻辑两版同源（Python `app/netcfg.py` ↔ PHP `php/src/Net.php`），
逐项复验见 `tools/verify_python_features.py` 与 `tools/verify_php.py`。

> **绑了域名后请从本机确认还能进后台** —— 回环地址永远放行，
> 所以不会出现「设了域名把自己锁死且改不回来」。

### ★ 反代 / CDN 部署时必须设置 `--forwarded-allow-ips`

`run.sh` 已经默认带上（留空 = 不信转发头）。**不要**把这一项删掉：

uvicorn 的 `proxy_headers` **默认开启**，`forwarded_allow_ips` 默认是
`127.0.0.1,::1`。也就是说，只要直连对端是本机（本机跑 nginx 很常见），
uvicorn 就会**用 `X-Forwarded-For` 改写 `request.client`**（在应用代码之前）。
后果是应用层再也分不清「真实对端」和「伪造的客户端」，
`app/netcfg.py` 里那套「只在可信代理网段内才采信转发头」的判定被**完全架空** ——

> 实测复现：`bans` 里封禁了 `127.0.0.1`，请求带上
> `X-Forwarded-For: 9.9.9.9` 即可**绕过封禁**拿到 200。
> 加上 `--forwarded-allow-ips=""` 后，同一个请求被正确拒绝（403）。

有真实前置反代/CDN 时，把它设成其回源网段，并与后台
「站点设置 → 域名与 CDN → 可信代理 / CDN 网段」保持一致：

```bash
FORWARDED_ALLOW_IPS="172.16.0.0/12" ./run.sh
```

```json
{"ok":true,"jobs":0,"builds_done":0,"builder_enabled":true,
 "concurrency":2,"backend":"local","mail":false}
```

---

## 四、目录结构

```
.
├── install.sh               一键安装（依赖 + venv + systemd + 初始密码）
├── update.sh                升级（git pull + 重启，不碰运行时数据）
├── run.sh                   前台启动脚本
├── app/                     后端
│   ├── main.py              FastAPI 应用：路由、鉴权、管理接口
│   ├── builder.py           本机构建引擎（ImageBuilder）+ 构建队列
│   ├── backends.py          构建后端抽象：本地 / GitHub Actions
│   ├── sitesettings.py      站点配置中心（61 项 schema）
│   ├── dl.py                限时下载令牌（HMAC 签名 + TTL）
│   ├── mailer.py            邮件通知（SMTP + HTML 模板渲染）
│   ├── artifacts.py         本地构建物盘点 / 删除 / 孤儿清理
│   ├── dbutil.py            SQLite 连接收口（with 退出即提交并关闭）
│   ├── prefetch.py          第三方依赖预取与版本降级
│   ├── releases.py          分支 → 发行版映射
│   ├── templates/mail/      HTML 邮件模板（layout + build_ok/fail + verify + test）
│   └── jobs.py              构建任务持久化
├── web/                     前端
│   ├── index.html           首页（Vue 3）
│   ├── login.html           登录 / 注册
│   ├── admin.html           管理控制台（Vue 3）
│   ├── assets/css/app.css   设计系统
│   ├── assets/js/           common.js / app.js / admin.js
│   ├── vendor/              Vue 3 + jQuery（本地化）
│   ├── data/                常用软件包标签
│   └── langs/               11 种语言文件
├── data/                    离线元数据集（设备 / 软件包索引，10 MB）
├── tools/                   抓取与测试脚本
│   ├── fetch_data.py        元数据抓取
│   ├── e2e_v2.py            综合 e2e（60 项断言）
│   ├── gh_watch.py          GitHub Actions run 跟踪
│   ├── responsive_check.py  响应式回归（Playwright）
│   ├── verify_u_fixes.py    安全修复逐项复验
│   ├── extract_regression.py 归档解压安全回归
│   └── smtp_test_server.py  本地 SMTP 收件服务器（仅测试用）
├── reports/                 安全审计报告
└── config.json              站点引导配置（不含凭据）
```

### 运行时目录（不在版本控制内，首次运行自动创建）

| 目录 | 用途 |
|------|------|
| `users.db` | SQLite：用户、会话、构建记录、下载令牌、站点设置（**含凭据**） |
| `kwrt.env` | 初始管理员密码（权限 600，由 install.sh 生成） |
| `work/` | ImageBuilder 工作目录（约 2 GB，可清理后重建） |
| `cache/` | ImageBuilder 压缩包缓存 |
| `store/` | 构建产物与上传文件（可在管理台「队列与存储 → 本地构建物」逐个清理） |

---

## 五、安全说明

### 凭据存放位置

**所有敏感配置存于 `users.db` 的 `settings` 表**，不在源码中：

| 配置项 | 内容 |
|--------|------|
| `gh.token` | GitHub Personal Access Token |
| `mail.password` | SMTP 密码 / 授权码 |
| `dl.secret` | 下载令牌的 HMAC 签名密钥（自动生成） |

`users.db` 已在 `.gitignore` 中排除，**不会进入版本控制**。

### 已实现的安全机制

- 敏感配置在管理接口回传时**恒为掩码** `********`，且掩码值不会被写回数据库
- 前台 `/api/v1/site` 采用**白名单**字段，密钥类字段不在白名单内
- 管理接口全部强制实时鉴权（角色取自数据库，降权/停用立即生效）
- 下载令牌 HMAC 签名 + 有效期 + 次数上限 + 吊销，路径穿越已防护
- 全部管理写操作写入 `admin_logs` 审计
- 关键操作有自我保护：不能撤销自己的管理员权限、不能停用自己的账号

### 部署前必做

1. 修改三个默认账号的密码
2. 按需在管理台配置 SMTP 与 GitHub Token
3. 建议在反向代理层启用 HTTPS

---

## 六、测试

```bash
# 综合 e2e：站点配置 / 赞助 / 下载令牌 / 邮件 / 双后端 / 构建全链路
python3 tools/e2e_v2.py

# 本地 SMTP 收件服务器（验证邮件投递，仅测试用）
python3 tools/smtp_test_server.py 2525
# 随后在管理台「邮件通知」配置 127.0.0.1:2525，点「发送测试邮件」
# 收件记录落在 /tmp/smtp_inbox.jsonl

# 跟踪一个 GitHub Actions run 直到完成
GH_TOKEN=xxx python3 tools/gh_watch.py <run_id>
```

`tools/e2e_v2.py` 覆盖 60 项断言，包含会真实触发一次本机构建（约 70–160 秒）。

---

## 七、已知限制

| 项 | 说明 |
|----|------|
| 磁盘 | 单次构建产物约 140 MB，ImageBuilder 工作目录约 2 GB。内置 `check_disk()` 守卫，空间不足时返回可读错误而非 ENOSPC。生产部署建议单独挂载数据盘 |
| 第三方源 | `25.12` 分支为 apk 包管理，第三方（kiddin9）源仅提供 opkg 包。选择第三方插件时会**自动路由到 24.10 分支**（`pick_version`），此为有意设计 |
| GitHub 产物回传 | 受网络带宽制约，跨国链路回传上百 MB 产物可能耗时较长。可通过 `gh.mirror_artifacts` 关闭回传（改为直接使用 GitHub 产物地址，但放弃本站链接有效期管控），或调整 `gh.mirror_timeout` |
| 邮件投递 | 公网邮箱（QQ/163 等）需真实授权码；本仓库的测试流程使用本地 SMTP 服务器 |
| 单机部署 | 构建队列、下载令牌、任务状态均为进程内 + SQLite，未做多实例分布式协调 |

---

## 八、关于本仓库

- 本项目是 `openwrt.ai` 的**功能复刻实现**，用于学习与自建用途，与原站无隶属关系
- `data/` 中的设备与软件包元数据来自 OpenWrt 上游公开接口（`downloads.openwrt.org`），可经 `tools/fetch_data.py` 重新抓取
- 复刻过程与验证细节见配套报告（交付报告、管理员功能报告、UI 重构报告、GitHub 链路验证报告）
- 未附带开源许可证；如需授权请自行添加 `LICENSE`

---

## 九、API 速览

### 前台

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/site` | 站点信息（公开字段 + 赞助套餐 + 后端状态） |
| GET | `/api/v1/sponsor/tiers` | 赞助套餐与金额 |
| POST | `/api/v1/sponsor/claim` | 提交赞助声明 |
| GET | `/api/v1/downloads` | 我的有效下载链接 |
| GET | `/dl/t/{token}` | 限时下载入口 |
| POST | `/api/v1/build` | 提交构建（需一次性校验头） |
| GET | `/api/v1/build/{hash}` | 查询构建状态与下载链接 |
| POST | `/api/v1/login` `/register` `/logout` | 账户 |
| GET | `/api/v1/user` | 当前用户状态 |

### 管理端（均需管理员角色）

```
GET  /api/v1/admin/overview          总览
GET  /api/v1/admin/site              配置 schema + 当前值（掩码）
POST /api/v1/admin/site              批量写入配置
GET  /api/v1/admin/users             用户列表
POST /api/v1/admin/user              用户操作
POST /api/v1/admin/user/create       创建用户
POST /api/v1/admin/user/sponsor      授予 / 续期 / 撤销赞助
GET  /api/v1/admin/builds            构建列表
POST /api/v1/admin/build             取消 / 重试 / 删除
GET  /api/v1/admin/queue             队列状态
POST /api/v1/admin/queue             并发 / 启停 / 清空
GET  /api/v1/admin/tokens            下载令牌列表
POST /api/v1/admin/tokens            吊销 / 续期 / 重发 / 清理
GET  /api/v1/admin/sponsor/claims    赞助申请
POST /api/v1/admin/sponsor/claim     审核
POST /api/v1/admin/mail/test         发送测试邮件
GET  /api/v1/admin/mail/log          投递记录
POST /api/v1/admin/github/test       测试 GitHub 连接
GET  /api/v1/admin/build_backend     后端生效状态
GET  /api/v1/admin/proposals         插件提议
POST /api/v1/admin/proposal          审核提议
GET  /api/v1/admin/bans              封禁列表
POST /api/v1/admin/ban               增删封禁
GET  /api/v1/admin/logs              审计日志
POST /api/v1/admin/logs/clear        清空日志
```

---

## 十、代码规模

```
后端 Python          约 3,200 行
前端 JS（Vue 3）     约 1,400 行
设计系统 CSS           约 660 行
页面模板 HTML        约 1,800 行
工具脚本             约 1,300 行
────────────────────────────────
合计                 约 8,700 行
```

---

## ★ v1.0.2 新增

> 自 v1.0.1 起共 29 次提交、92 个文件、约 15,000 行改动。
> 这一版的主线是**把两版（Python / PHP）拉齐**，以及修掉一批只在真机上才暴露的缺陷。

### 新增功能

| 功能 | 说明 |
|---|---|
| 找回密码 | 管理员与普通用户均可自助重置，经 SMTP 发信；两版同构（`web/reset.html`） |
| 后台「检查更新」 | 从 GitHub 查本程序新版本。版本**逐段比数字**（`1.0.10 > 1.0.9`，字符串比较会判错）、失败**不冒充**「已是最新」、缓存绑配置、**只查不升** |
| 后台显示版本号 | 头部/页脚/`/healthz`/`/api/v1/site` 一处定义多处显示 |
| PHP 版安装向导 | 环境检查 → 数据库 → 管理员 → 站点设置，网页内完成 |
| PHP 版 MySQL + 宝塔部署 | 支持 MySQL 后端与宝塔面板摆法 |
| PHP 版远端构建 | 补齐 GitHub 派发、外链下载代理、自定义邮件模板，与 Python 版对齐 |

### 修掉的真缺陷（都带回归护栏）

**高危 / 必崩**

- 退款审批线上必崩 —— `with conn:` 会关闭连接，审批后继续用同一个连接的代码全废；顺带补上缺失的幂等键与漏掉的状态。
- 验证码可重放 + 支付重复发放 —— 两处 check-then-act 竞态，两版都修。
- `reap()` 在 SQL 里比时间踩 SQLite 类型序 —— **会杀掉所有正在跑的构建**，且看起来像正常回收。
- 构建队列死锁 + 假成功（任务卡住却报完成），以及路径守卫未收口。

**只在真机上才暴露**

- 邮件投递记录接口稳定 500 —— `deque` 不支持切片。
- 支付二维码 500 —— 依赖了**未声明**的 Pillow。
- 安装向导选 MySQL 建表必失败 —— `TEXT` 带 `DEFAULT`（1101）与 `TEXT` 进索引（1170）。
- 宝塔默认禁用 `putenv` —— 导致 MySQL 安装必 500。
- 宝塔部署：入口不再写死目录布局，支持常见的 4 种摆法，认不出时给人话而不是 PHP fatal。
- 部署后静态资源不生效 —— 根因是**三层缓存**叠加。
- 赞助完成后看不到订单与退款按钮 —— 订单区原长在赞助弹窗内部，
  且设备页入口卡片写的是「已赞助就隐藏」，付完钱入口当场消失。
  现在订单区是**独立顶层区块**，入口卡片不再因已赞助而消失，页头加常驻直达入口
  （见 `tools/verify_sponsor_ui.py`，8 项真实浏览器断言）。

**其他**

- PHP 7.x 下整站白屏 —— 入口改用 PHP 5/7 都能解析的语法 + 版本前置守卫。
- 10 个**静默失效**的设置项接线（`max_hits` / 网关地址 / 构建后端等），
  并新增设置项一致性机械检查 —— 这类问题的特征是「后台能改、改了没用」。
- 底栏不再显示 PHP 版本号（精确版本属信息泄露）。
- 补 `/login` 无斜杠别名，手打 URL 不再 404。

---

## ★ v1.0.1 新增

### 版本号（单一真源）
仓库根 `VERSION` 文件是唯一真源：Python 读 `app/version.py`，PHP 读 `php/src/Version.php`，
两者显示同一个数字（`tools/verify_round6.py` 的 V-1/V-4 会断言一致）。
后台头部、页脚、`/healthz`、`/api/v1/site` 都会显示。**发版只改 `VERSION` 文件**。

### 安装即开机自启 / 更新即自动重启
systemd 逻辑抽成一份 `scripts/systemd-install.sh`，`install.sh` 与 `update.sh` 共用：

| 场景 | 行为 |
|---|---|
| `install.sh` | 装 unit → `systemctl enable`（**会验证结果，失败即报错**）→ 启动 |
| `update.sh`（服务已存在） | `--enable-only`：只 enable + restart |
| `update.sh`（服务缺失） | **自动安装 unit + 开机自启 + 启动**，不再只打印「请手动重启」 |

退出码：`0` 成功 / `1` 失败 / `2` 环境不具备（无 systemctl 或未建 venv）——调用方据此降级。

### 登录/注册验证码（后台可开关）
后台「安全与验证」分组：
`security.captcha_enabled`（登录）、`security.captcha_on_register`（注册，独立开关）、
`security.captcha_length`（3–6 位）、`security.captcha_ttl_min`（1–30 分钟）。

- 服务端生成 SVG（**不依赖 Pillow 等图形库**，装不上也不会导致功能整块失效）
- 答案**只存 sha256 哈希**，库里没有明文
- **一次性**：校验后立即作废，重放同一个 id 一律拒绝
- 有有效期；按 IP 每 60 秒限签 30 张
- 校验在**密码比对之前**，否则撞库仍会消耗一次密码校验
- **盐持久化在 `app_secrets` 表**（不是进程内随机）——
  否则多 worker 部署时会在不同进程间随机校验失败
- **两版互通**：同一张表 + 同一个盐 + 同一个哈希算法，PHP 签发的 Python 能校验，反之亦然

### 多个 GitHub 构建队列
后台「GitHub 构建 → 构建队列（多队列）」填 JSON 数组：

```json
[{"name":"主队列","repo":"owner/repo","token":"ghp_xxx",
  "workflow":"build-firmware.yml","ref":"main","enabled":true}]
```

- 多队列之间**轮转派发**；每个队列可挂不同仓库 / 分支 / workflow
- 队列参数**绑到后端实例**上（dispatch 是同步的，全程同一个 repo/token，不会串号）
- 缺 `repo` 或 `token` 的项**跳过**，不会让整个后端失效
- `gh.queues` 留空则回落到单队列的 `gh.repo`/`gh.token`，**老配置无需改动**
- PHP 侧 `php/src/Queues.php` 同语义；后台「测试连接」测的是**真正会派发的那个队列**

### 自定义页头导航
`homepage_links`（后台「品牌与外观」）不再是隐藏项，可直接编辑 JSON 数组。
只允许 `http(s)` 与站内相对路径，`javascript:` / 协议相对 URL 一律忽略。
