<?php
/**
 * 设置项定义（schema）。
 *
 * ★ 本文件由 php/scripts/gen_settings_schema.py 从 Python 版
 *   app/sitesettings.py 自动生成，**请勿手改**。
 *   要增删设置项，请改 Python 侧再重新生成，两版永远同源：
 *
 *       python3 php/scripts/gen_settings_schema.py
 *
 * 生成基准：131 项设置 / 15 个分组
 */
declare(strict_types=1);

namespace Kwrt;

final class SettingsSchema
{
    /** 分组：[key, 标签, 说明]，顺序即管理端展示顺序。 */
    public const GROUPS = [
        ['brand', '品牌与外观', '站点名称、Logo、主题色'],
        ['home', '首页文案', '主标题、副标题、提示语'],
        ['ads', '广告位', '前台弹出 / 滚动广告，支持 Markdown 与超链接'],
        ['links', '页脚与联系', '页脚补充文案、软件库入口、联系方式'],
        ['build', '构建设置', '构建后端、并发、默认版本、配额'],
        ['download', '下载与链接', '有效期、访问控制、外链策略'],
        ['mail', '邮件通知', 'SMTP 服务器与通知模板'],
        ['github', 'GitHub 构建', 'GitHub Actions 触发配置'],
        ['update', '程序更新', '从 GitHub 检查本程序的版本更新'],
        ['sponsor', '赞助设置', '赞助开关、货币、套餐与金额'],
        ['pay', '在线支付', '支付宝当面付：APPID、密钥、网关闭环'],
        ['domain', '域名与 CDN', '域名绑定、可信 Host/代理、CDN 回源与静态缓存'],
        ['pages', '页面与入口', '逐页开关：关闭后该页对外不可见'],
        ['entry', '公众号与 App', 'App 下载、公众号引导、PWA 安装'],
        ['security', '安全与验证', '登录/注册验证码、防自动化'],
    ];

    /** 全部设置项：k=键 g=分组 t=类型 d=默认值 label=标签（+ 类型专属约束）。 */
    public const SCHEMA = [
        ['k' => 'site_name', 'g' => 'brand', 't' => 'text', 'label' => '站点名称', 'd' => 'Kwrt(OpenWrt)软路由固件下载与在线定制编译', 'max' => 120, 'hint' => '显示在浏览器标题与页头'],
        ['k' => 'site_short', 'g' => 'brand', 't' => 'text', 'label' => '站点简称', 'd' => 'Kwrt', 'max' => 24, 'hint' => 'Logo 旁显示的短名称'],
        ['k' => 'site_desc', 'g' => 'brand', 't' => 'textarea', 'label' => '站点简介', 'd' => '为你的设备在线定制并实时编译 OpenWrt 固件', 'max' => 300],
        ['k' => 'logo_url', 'g' => 'brand', 't' => 'text', 'label' => 'Logo 图片地址', 'd' => '/static/logo.png', 'max' => 300],
        ['k' => 'favicon_url', 'g' => 'brand', 't' => 'text', 'label' => '站点图标地址', 'd' => '/static/logo.png', 'max' => 300],
        ['k' => 'primary_color', 'g' => 'brand', 't' => 'color', 'label' => '主色', 'd' => '#2563eb', 'hint' => '影响按钮与强调色；蓝＝信任、专业'],
        ['k' => 'theme_default', 'g' => 'brand', 't' => 'select', 'label' => '默认主题', 'd' => 'auto', 'opts' => '["auto", "light", "dark"]', 'hint' => 'auto 跟随系统'],
        ['k' => 'homepage_links', 'g' => 'brand', 't' => 'json', 'label' => '页头导航链接', 'd' => '[]', 'hint' => 'JSON 数组，如 [{"label":"文档","url":"/docs"}]。只允许 http(s) 链接与站内相对路径（/docs），其它一律忽略'],
        ['k' => 'hero_title', 'g' => 'home', 't' => 'text', 'label' => '主标题', 'd' => '下载或定制适用于您设备的 OpenWrt 固件', 'max' => 140],
        ['k' => 'hero_subtitle', 'g' => 'home', 't' => 'textarea', 'label' => '副标题', 'd' => '输入设备名称或型号，选择需要的软件包，服务器将为你实时编译固件。', 'max' => 300],
        ['k' => 'devices_hint', 'g' => 'home', 't' => 'text', 'label' => '设备检索提示', 'd' => '输入设备的名称或型号', 'max' => 80],
        ['k' => 'customize_title', 'g' => 'home', 't' => 'text', 'label' => '定制区标题', 'd' => '自定义构建固件 (为爱发电,请勿滥用,欢迎 赞助)', 'max' => 140],
        ['k' => 'announcement', 'g' => 'home', 't' => 'textarea', 'label' => '站点公告', 'd' => '', 'max' => 600, 'hint' => '留空则不显示；显示在首页顶部'],
        ['k' => 'footer_text', 'g' => 'home', 't' => 'textarea', 'label' => '页脚文案', 'd' => '本站为 openwrt.ai 功能复刻演示站，固件由 OpenWrt 官方 ImageBuilder 实时编译。', 'max' => 400],
        ['k' => 'show_help', 'g' => 'home', 't' => 'bool', 'label' => '显示帮助入口', 'd' => true],
        ['k' => 'ads', 'g' => 'ads', 't' => 'json', 'label' => '广告列表', 'd' => '[]', 'hint' => 'JSON 数组，每项一个广告，通常由下方可视化编辑器维护。字段说明：
  enabled    是否启用（false 则不展示）
  mode       popup=弹出模态框；marquee=滚动跑马灯
  title      标题（弹出框显示为标题，滚动条显示为前置标签）
  content    正文，支持 Markdown 与超链接
  image      可选配图地址（弹出框顶部大图）
  link       可选跳转地址；link_text 为按钮文字
  closable   是否允许用户关闭（false 则强制展示）
  delay      弹出延迟秒数（0=立即）
  frequency  session=每个会话一次；always=每次访问；once=仅一次
  speed      滚动速度（px/秒，10–400）
  position   top/bottom，滚动条位置
  bg / color 滚动条背景色与文字色
  start/end  可选起止时间，如 2026-01-01（只写日期时 end 含当天）
示例：[{"enabled":true,"mode":"popup","title":"公告","content":"新版上线，**欢迎体验** → [详情](https://example.com)","delay":1,"frequency":"session"}]'],
        ['k' => 'footer_moat_title', 'g' => 'links', 't' => 'text', 'label' => '页脚·壁垒标题', 'd' => '', 'max' => 80],
        ['k' => 'footer_moat_text', 'g' => 'links', 't' => 'textarea', 'label' => '页脚·壁垒说明', 'd' => '从设备识别、依赖求解、交叉编译到产物签发，全链路自研实现，不依赖任何第三方托管服务。', 'max' => 600],
        ['k' => 'packages_url', 'g' => 'links', 't' => 'text', 'label' => '自建软件库入口', 'd' => '/packages/', 'max' => 300],
        ['k' => 'contact_email', 'g' => 'links', 't' => 'email', 'label' => '联系邮箱', 'd' => '', 'max' => 160],
        ['k' => 'contact_text', 'g' => 'links', 't' => 'textarea', 'label' => '联系页说明', 'd' => '', 'max' => 600],
        ['k' => 'icp_text', 'g' => 'links', 't' => 'text', 'label' => '备案信息', 'd' => '', 'max' => 120],
        ['k' => 'builder.enabled', 'g' => 'build', 't' => 'bool', 'label' => '启用构建服务', 'd' => true],
        ['k' => 'builder.backend', 'g' => 'build', 't' => 'select', 'label' => '构建后端', 'd' => 'local', 'opts' => '["local", "github"]', 'hint' => 'local＝本机 ImageBuilder 实编；github＝GitHub Actions'],
        ['k' => 'builder.max_concurrent', 'g' => 'build', 't' => 'number', 'label' => '并发构建数', 'd' => 1, 'max' => 8, 'min' => 1, 'hint' => '受磁盘与内存限制，建议保持 1～2'],
        ['k' => 'default_quota', 'g' => 'build', 't' => 'number', 'label' => '默认插件配额', 'd' => 12, 'max' => 9999, 'min' => 0, 'hint' => '非赞助用户可勾选 luci-app 数量上限'],
        ['k' => 'build_default_version', 'g' => 'build', 't' => 'select', 'label' => '默认固件分支', 'd' => '25.12', 'opts' => '["25.12", "24.10"]'],
        ['k' => 'build_vip_hint', 'g' => 'build', 't' => 'text', 'label' => 'VIP 通道提示', 'd' => '赞助用户可使用 VIP 通道，减少排队等待', 'max' => 140],
        ['k' => 'build_allow_anonymous', 'g' => 'build', 't' => 'bool', 'label' => '允许匿名构建', 'd' => true],
        ['k' => 'download.link_ttl_hours', 'g' => 'download', 't' => 'number', 'label' => '下载链接有效期（小时）', 'd' => 72, 'max' => 8760, 'min' => 1, 'hint' => '邮件与 API 返回的下载链接到期后失效'],
        ['k' => 'download.require_login', 'g' => 'download', 't' => 'bool', 'label' => '下载需登录', 'd' => false, 'hint' => '开启后仅登录用户可下载自建固件'],
        ['k' => 'download.max_hits', 'g' => 'download', 't' => 'number', 'label' => '单链接最大下载次数', 'd' => 0, 'max' => 100000, 'min' => 0, 'hint' => '0 表示不限次数'],
        ['k' => 'download.serve_local', 'g' => 'download', 't' => 'bool', 'label' => '允许从本站下载', 'd' => true],
        ['k' => 'download.external_host', 'g' => 'download', 't' => 'text', 'label' => '外部下载域名', 'd' => '', 'max' => 200, 'hint' => '填写后下载链接指向该域名（如 CDN/对象存储）'],
        ['k' => 'download.email_on_ready', 'g' => 'download', 't' => 'bool', 'label' => '构建完成自动发邮件', 'd' => true, 'hint' => '需先在「邮件通知」中配置 SMTP'],
        ['k' => 'mail.enabled', 'g' => 'mail', 't' => 'bool', 'label' => '启用邮件通知', 'd' => false],
        ['k' => 'mail.host', 'g' => 'mail', 't' => 'text', 'label' => 'SMTP 服务器', 'd' => '', 'max' => 200, 'hint' => '如 smtp.qq.com / smtp.163.com'],
        ['k' => 'mail.port', 'g' => 'mail', 't' => 'number', 'label' => 'SMTP 端口', 'd' => 465, 'max' => 65535, 'min' => 1],
        ['k' => 'mail.encryption', 'g' => 'mail', 't' => 'select', 'label' => '加密方式', 'd' => 'ssl', 'opts' => '["ssl", "starttls", "none"]'],
        ['k' => 'mail.user', 'g' => 'mail', 't' => 'text', 'label' => 'SMTP 账号', 'd' => '', 'max' => 200],
        ['k' => 'mail.password', 'g' => 'mail', 't' => 'password', 'label' => 'SMTP 密码/授权码', 'd' => '', 'max' => 200],
        ['k' => 'mail.from_name', 'g' => 'mail', 't' => 'text', 'label' => '发件人名称', 'd' => 'Kwrt 固件构建', 'max' => 80],
        ['k' => 'mail.from_addr', 'g' => 'mail', 't' => 'email', 'label' => '发件人地址', 'd' => '', 'max' => 160],
        ['k' => 'mail.subject_tpl', 'g' => 'mail', 't' => 'text', 'label' => '邮件主题模板', 'd' => '[Kwrt] 你的 {target} 固件已构建完成', 'max' => 200, 'hint' => '可用变量：{target} {profile} {version} {hours}'],
        ['k' => 'mail.body_tpl', 'g' => 'mail', 't' => 'textarea', 'label' => '邮件正文模板', 'd' => '你好 {username}：

你定制的 {target} / {profile} 固件（{version}）已构建完成，共 {count} 个文件，耗时 {duration}。

下载链接（{hours} 小时内有效）：
{links}

提示：链接过期后可在站点的构建记录中重新生成。
', 'max' => 3000, 'hint' => '可用变量：{username} {target} {profile} {version} {count} {duration} {hours} {links}'],
        ['k' => 'mail.notify_fail', 'g' => 'mail', 't' => 'bool', 'label' => '构建失败时也通知', 'd' => true],
        ['k' => 'mail.verify_register', 'g' => 'mail', 't' => 'bool', 'label' => '注册需邮箱验证', 'd' => false, 'hint' => '开启后新用户必须点击邮件中的链接才能激活账号；关闭则注册即用'],
        ['k' => 'mail.verify_ttl_hours', 'g' => 'mail', 't' => 'number', 'label' => '验证链接有效期（小时）', 'd' => 24, 'max' => 720, 'min' => 1],
        ['k' => 'mail.verify_subject', 'g' => 'mail', 't' => 'text', 'label' => '验证邮件主题', 'd' => '[Kwrt] 请验证你的邮箱', 'max' => 200],
        ['k' => 'mail.verify_body', 'g' => 'mail', 't' => 'textarea', 'label' => '验证邮件正文', 'd' => '你好 {username}：

感谢注册 {site}。请点击下面的链接完成邮箱验证：

{link}

链接 {hours} 小时内有效。若这不是你本人的操作，忽略本邮件即可。
', 'max' => 3000, 'hint' => '可用变量：{username} {site} {link} {hours} {email}'],
        ['k' => 'mail.reset_ttl_hours', 'g' => 'mail', 't' => 'number', 'label' => '重置链接有效期（小时）', 'd' => 2, 'max' => 168, 'min' => 1, 'hint' => '默认 2 小时。改口令的链接比注册验证更敏感，别设太长'],
        ['k' => 'mail.reset_subject', 'g' => 'mail', 't' => 'text', 'label' => '找回密码邮件主题', 'd' => '[Kwrt] 重置你的登录密码', 'max' => 200],
        ['k' => 'mail.reset_body', 'g' => 'mail', 't' => 'textarea', 'label' => '找回密码邮件正文', 'd' => '你好 {username}：

我们收到了重置 {site} 登录密码的请求。点击下面的链接设置新密码：

{link}

链接 {hours} 小时内有效，且只能使用一次。
如果这不是你本人的操作，请忽略本邮件 —— 你的密码不会发生任何变化。
', 'max' => 3000, 'hint' => '可用变量：{username} {site} {link} {hours} {email}'],
        ['k' => 'gh.enabled', 'g' => 'github', 't' => 'bool', 'label' => '启用 GitHub Actions 构建', 'd' => false],
        ['k' => 'gh.repo', 'g' => 'github', 't' => 'text', 'label' => '仓库（owner/repo）', 'd' => '', 'max' => 200, 'hint' => '需已放置构建 workflow'],
        ['k' => 'gh.workflow', 'g' => 'github', 't' => 'text', 'label' => 'Workflow 文件名', 'd' => 'build-firmware.yml', 'max' => 200],
        ['k' => 'gh.ref', 'g' => 'github', 't' => 'text', 'label' => '分支 / 引用', 'd' => 'main', 'max' => 120],
        ['k' => 'gh.token', 'g' => 'github', 't' => 'password', 'label' => 'GitHub Token', 'd' => '', 'hint' => '需 repo + workflow 权限'],
        ['k' => 'gh.queues', 'g' => 'github', 't' => 'json', 'label' => '构建队列（多队列）', 'd' => '[]', 'hint' => 'JSON 数组。每项：{"name":"主队列","repo":"owner/repo","token":"ghp_xxx","workflow":"build-firmware.yml","ref":"main","enabled":true}。多个队列之间**轮流派发**，可用于分担构建压力、或给不同 openwrt 分支挂不同仓库。留空则使用上面的单队列配置'],
        ['k' => 'gh.artifact_pattern', 'g' => 'github', 't' => 'text', 'label' => '产物名匹配', 'd' => 'openwrt-*', 'max' => 120],
        ['k' => 'gh.mirror_artifacts', 'g' => 'github', 't' => 'bool', 'label' => '把产物回传到本站存储', 'd' => true, 'hint' => '开启后 GitHub 产物会下载回本站，下载链接有效期由本站统一管理；关闭则直接使用 GitHub 产物地址（有效期由 GitHub 保留策略决定）'],
        ['k' => 'gh.mirror_timeout', 'g' => 'github', 't' => 'number', 'label' => '产物回传超时（秒）', 'd' => 3600, 'max' => 21600, 'min' => 60, 'hint' => '镜像体积较大、跨国带宽较慢时需放宽'],
        ['k' => 'update.enabled', 'g' => 'update', 't' => 'bool', 'label' => '启用更新检查', 'd' => true],
        ['k' => 'update.repo', 'g' => 'update', 't' => 'text', 'label' => '更新源仓库（owner/repo）', 'd' => '2016xyz/kwrt-clone', 'max' => 200, 'hint' => '本程序源码所在仓库。检查时优先读它的 Release；没有 Release 就读该仓库的 VERSION 文件。私有仓库需在下面配 Token'],
        ['k' => 'update.ref', 'g' => 'update', 't' => 'text', 'label' => '回退分支 / 引用', 'd' => 'main', 'max' => 120, 'hint' => '仓库没有 Release 时，从哪个分支读 VERSION 文件'],
        ['k' => 'update.token', 'g' => 'update', 't' => 'password', 'label' => '更新源 Token', 'd' => '', 'hint' => '私有仓库必填；留空则回落到上面 GitHub 构建的 Token'],
        ['k' => 'update.include_prerelease', 'g' => 'update', 't' => 'bool', 'label' => '包含预发布版本', 'd' => false, 'hint' => '开启后 -beta / -rc 之类的版本也会被当作可用更新'],
        ['k' => 'update.cache_minutes', 'g' => 'update', 't' => 'number', 'label' => '检查结果缓存（分钟）', 'd' => 10, 'max' => 1440, 'min' => 1, 'hint' => 'GitHub 未认证请求每小时只有 60 次，缓存可避免把限额打满'],
        ['k' => 'sponsor.enabled', 'g' => 'sponsor', 't' => 'bool', 'label' => '启用赞助', 'd' => true],
        ['k' => 'sponsor.currency', 'g' => 'sponsor', 't' => 'select', 'label' => '货币单位', 'd' => 'CNY', 'opts' => '["CNY", "USD", "EUR", "JPY", "HKD"]'],
        ['k' => 'sponsor.note', 'g' => 'sponsor', 't' => 'textarea', 'label' => '赞助说明', 'd' => '赞助完全自愿。赞助后可解锁全部定制选项并享受 VIP 构建通道，但不提供任何形式的固件保证或技术支持承诺。', 'max' => 800],
        ['k' => 'sponsor.pay_qr', 'g' => 'sponsor', 't' => 'text', 'label' => '收款码图片地址', 'd' => '', 'max' => 300],
        ['k' => 'sponsor.contact', 'g' => 'sponsor', 't' => 'text', 'label' => '赞助后联系说明', 'd' => '', 'max' => 200],
        ['k' => 'sponsor.auto_approve', 'g' => 'sponsor', 't' => 'bool', 'label' => '允许自助确认赞助', 'd' => true, 'hint' => '关闭后需管理员在后台手动标记'],
        ['k' => 'sponsor.tiers', 'g' => 'sponsor', 't' => 'json', 'label' => '赞助套餐', 'd' => '[{"name": "月付赞助", "amount": 10, "days": 30, "perks": ["解锁全部定制项", "VIP 构建通道", "自定义主机名与签名"]}, {"name": "季付赞助", "amount": 26, "days": 90, "perks": ["解锁全部定制项", "VIP 构建通道", "自定义主机名与签名", "插件数量不限"]}, {"name": "年付赞助", "amount": 88, "days": 365, "perks": ["解锁全部定制项", "VIP 构建通道", "自定义主机名与签名", "插件数量不限", "优先技术支持"]}]', 'hint' => 'JSON 数组，每项含 name / amount / days / perks[]'],
        ['k' => 'pay.alipay_enabled', 'g' => 'pay', 't' => 'bool', 'label' => '启用支付宝当面付', 'd' => false, 'hint' => '开启后用户在赞助页扫码支付，服务端查单确认到账并自动置位赞助态'],
        ['k' => 'pay.alipay_app_id', 'g' => 'pay', 't' => 'text', 'label' => '应用 APPID', 'd' => '', 'max' => 64, 'hint' => '支付宝开放平台 → 应用信息 → APPID'],
        ['k' => 'pay.alipay_private_key', 'g' => 'pay', 't' => 'textarea', 'label' => '应用私钥', 'd' => '', 'max' => 4000, 'hint' => '粘贴密钥内容即可，无需自行添加 PEM 头（PKCS#8 / PKCS#1 均可）'],
        ['k' => 'pay.alipay_public_key', 'g' => 'pay', 't' => 'textarea', 'label' => '支付宝公钥', 'd' => '', 'max' => 4000, 'hint' => '注意是「支付宝公钥」，不是你的应用公钥——填错会导致验签失败'],
        ['k' => 'pay.alipay_sandbox', 'g' => 'pay', 't' => 'bool', 'label' => '使用沙箱环境', 'd' => true, 'hint' => '上线前请关闭，切到正式网关'],
        ['k' => 'pay.alipay_gateway', 'g' => 'pay', 't' => 'text', 'label' => '网关地址（留空用默认）', 'd' => '', 'max' => 200],
        ['k' => 'pay.notify_url', 'g' => 'pay', 't' => 'text', 'label' => '异步通知地址', 'd' => '', 'max' => 300, 'hint' => '留空则按站点地址自动推导为 {site}/api/v1/alipay/notify，须为公网可访问'],
        ['k' => 'pay.alipay_subject_prefix', 'g' => 'pay', 't' => 'text', 'label' => '订单标题前缀', 'd' => '赞助', 'max' => 40],
        ['k' => 'pay.auto_activate', 'g' => 'pay', 't' => 'bool', 'label' => '支付成功后自动置位赞助', 'd' => true, 'hint' => '关闭后仅记录已支付订单，由管理员在后台确认发放'],
        ['k' => 'pay.order_ttl_minutes', 'g' => 'pay', 't' => 'number', 'label' => '订单超时（分钟）', 'd' => 15, 'max' => 1440, 'min' => 1, 'hint' => '超时未支付的订单自动关闭'],
        ['k' => 'pay.poll_seconds', 'g' => 'pay', 't' => 'number', 'label' => '前端查单间隔（秒）', 'd' => 3, 'max' => 30, 'min' => 1],
        ['k' => 'pay.refund_enabled', 'g' => 'pay', 't' => 'bool', 'label' => '开启退款申请', 'd' => true, 'hint' => '开启后用户可对已支付订单提交退款申请，需管理员在「退款审核」中审批'],
        ['k' => 'pay.currency', 'g' => 'pay', 't' => 'text', 'label' => '货币代码', 'd' => 'CNY', 'max' => 8, 'hint' => '仅用于展示，如 CNY / USD'],
        ['k' => 'pay.result_note', 'g' => 'pay', 't' => 'textarea', 'label' => '支付成功提示', 'd' => '', 'max' => 300, 'hint' => '支付完成后在订单页展示的补充说明，留空则不显示'],
        ['k' => 'site.domain', 'g' => 'domain', 't' => 'text', 'label' => '绑定域名', 'd' => '', 'max' => 120, 'hint' => '如 fw.example.com。填写后所有绝对链接（验证邮件、下载链接、支付回调）一律用该域名生成，不再取请求头 Host —— 这是防 Host 头注入的关键'],
        ['k' => 'site.scheme', 'g' => 'domain', 't' => 'select', 'label' => '对外协议', 'd' => 'auto', 'opts' => '["auto", "https", "http"]', 'hint' => 'auto＝信任反代/CDN 的 X-Forwarded-Proto；不确定时选 https'],
        ['k' => 'site.trusted_hosts', 'g' => 'domain', 't' => 'textarea', 'label' => '可信 Host 白名单', 'd' => '', 'hint' => '每行一个，可用逗号分隔。绑定了域名后，不在此列表且不等于绑定域名的 Host 一律拒绝，避免 Host 头注入'],
        ['k' => 'site.allow_any_host', 'g' => 'domain', 't' => 'bool', 'label' => '未绑域名时接受任意 Host', 'd' => true, 'hint' => '仅在「绑定域名」为空且白名单也为空时生效。正式上线建议关闭'],
        ['k' => 'site.trusted_proxies', 'g' => 'domain', 't' => 'textarea', 'label' => '可信代理 / CDN 网段', 'd' => '', 'hint' => '每行一个 CIDR，如 172.16.0.0/12、2400:cb00::/32（Cloudflare）。★ 只有直连对端落在这里面，才会采信 X-Forwarded-For / CF-Connecting-IP；否则一律以 REMOTE_ADDR 为准 —— 否则源 IP 可被伪造，封禁与限速同时失效'],
        ['k' => 'site.cdn_enabled', 'g' => 'domain', 't' => 'bool', 'label' => '启用 CDN', 'd' => false],
        ['k' => 'site.cdn_base', 'g' => 'domain', 't' => 'text', 'label' => 'CDN 资源域名', 'd' => '', 'max' => 200, 'hint' => '如 https://cdn.example.com。启用后页面里的静态资源一律走该域名，并自动带 ?v=版本号做长缓存失效'],
        ['k' => 'site.asset_version', 'g' => 'domain', 't' => 'text', 'label' => '静态资源版本号', 'd' => '', 'max' => 40, 'hint' => '改这里即可让 CDN 上的旧静态资源立即失效（配合 long max-age immutable）'],
        ['k' => 'site.force_https', 'g' => 'domain', 't' => 'bool', 'label' => '强制跳转 HTTPS', 'd' => false, 'hint' => '开启后 http 请求 301 到 https，请确认证书已就绪再开'],
        ['k' => 'page.disabled_behavior', 'g' => 'pages', 't' => 'select', 'label' => '关闭页面的表现', 'd' => '404', 'opts' => '["404", "redirect", "message"]', 'hint' => '404＝当作不存在；redirect＝跳到指定地址；message＝显示一段提示'],
        ['k' => 'page.disabled_redirect', 'g' => 'pages', 't' => 'text', 'label' => '跳转目标', 'd' => '/', 'max' => 300, 'hint' => '仅在表现选 redirect 时生效'],
        ['k' => 'page.disabled_message', 'g' => 'pages', 't' => 'textarea', 'label' => '下线提示文案', 'd' => '该功能已暂时下线，请稍后再试。', 'max' => 300],
        ['k' => 'page.index_enabled', 'g' => 'pages', 't' => 'bool', 'label' => '首页', 'd' => true, 'hint' => '关闭后首页不可用，请确认仍有其他入口，否则站点等于关闭'],
        ['k' => 'page.packages_enabled', 'g' => 'pages', 't' => 'bool', 'label' => '软件库', 'd' => true],
        ['k' => 'page.newpkg_enabled', 'g' => 'pages', 't' => 'bool', 'label' => '插件提议', 'd' => true],
        ['k' => 'page.fadian_enabled', 'g' => 'pages', 't' => 'bool', 'label' => '为爱发电', 'd' => true],
        ['k' => 'page.contact_enabled', 'g' => 'pages', 't' => 'bool', 'label' => '联系我们', 'd' => true],
        ['k' => 'page.login_enabled', 'g' => 'pages', 't' => 'bool', 'label' => '登录', 'd' => true, 'hint' => '关闭后登录页不可用；已登录会话仍然有效，但无法再登录。★ 关闭前请确认你能从后台恢复，否则会把自己锁在门外'],
        ['k' => 'page.register_enabled', 'g' => 'pages', 't' => 'bool', 'label' => '注册', 'd' => true],
        ['k' => 'page.reset_enabled', 'g' => 'pages', 't' => 'bool', 'label' => '找回密码', 'd' => true, 'hint' => '关闭后 /reset/ 页面与重置接口不可用（已发出的重置链接也会失效）。该功能依赖 SMTP：邮件未配置时用户申请会得到明确提示，不会静默失败'],
        ['k' => 'registration_open', 'g' => 'pages', 't' => 'bool', 'label' => '开放注册', 'd' => true, 'hint' => '关闭后不再受理新注册（注册页仍可访问，但提交会被拒绝）。与「注册」开关的区别：那个控制页面可见性，这个控制是否收人。'],
        ['k' => 'page.download_enabled', 'g' => 'pages', 't' => 'bool', 'label' => '固件下载', 'd' => true, 'hint' => '关闭后预编译镜像与产物下载入口不可见'],
        ['k' => 'entry.app_enabled', 'g' => 'entry', 't' => 'bool', 'label' => '显示 App 下载入口', 'd' => false],
        ['k' => 'entry.app_name', 'g' => 'entry', 't' => 'text', 'label' => 'App 名称', 'd' => 'Kwrt 助手', 'max' => 60],
        ['k' => 'entry.app_desc', 'g' => 'entry', 't' => 'textarea', 'label' => 'App 简介', 'd' => '', 'max' => 300],
        ['k' => 'entry.app_android_url', 'g' => 'entry', 't' => 'text', 'label' => 'Android 下载地址', 'd' => '', 'max' => 400, 'hint' => 'APK 直链或应用商店地址'],
        ['k' => 'entry.app_ios_url', 'g' => 'entry', 't' => 'text', 'label' => 'iOS 下载地址', 'd' => '', 'max' => 400],
        ['k' => 'entry.app_qrcode_url', 'g' => 'entry', 't' => 'text', 'label' => 'App 下载二维码图片', 'd' => '', 'max' => 400, 'hint' => '留空则前端按下载地址自动生成二维码'],
        ['k' => 'entry.wechat_enabled', 'g' => 'entry', 't' => 'bool', 'label' => '显示公众号入口', 'd' => false],
        ['k' => 'entry.wechat_official_name', 'g' => 'entry', 't' => 'text', 'label' => '公众号名称', 'd' => '', 'max' => 60],
        ['k' => 'entry.wechat_qrcode_url', 'g' => 'entry', 't' => 'text', 'label' => '公众号二维码图片', 'd' => '', 'max' => 400],
        ['k' => 'entry.wechat_follow_hint', 'g' => 'entry', 't' => 'textarea', 'label' => '关注引导文案', 'd' => '关注公众号获取固件更新通知与构建进度提醒。', 'max' => 300],
        ['k' => 'security.captcha_enabled', 'g' => 'security', 't' => 'bool', 'label' => '登录验证码', 'd' => false, 'hint' => '开启后登录必须填写图形验证码，用于挡自动化撞库。验证码由服务端生成、一次性使用、有有效期，答案不以明文入库'],
        ['k' => 'security.captcha_on_register', 'g' => 'security', 't' => 'bool', 'label' => '注册验证码', 'd' => false, 'hint' => '开启后注册也必须填写验证码（防批量注册小号）。独立于登录开关'],
        ['k' => 'security.captcha_on_reset', 'g' => 'security', 't' => 'bool', 'label' => '找回密码验证码', 'd' => true, 'hint' => '找回密码**申请**接口是否要求验证码，默认开。该接口会真实发信，没有验证码就是现成的邮件轰炸放大器'],
        ['k' => 'security.captcha_length', 'g' => 'security', 't' => 'number', 'label' => '验证码位数', 'd' => 4, 'max' => 6, 'min' => 3, 'hint' => '3–6 位。位数越多越难被 OCR，也越容易让人输错，4 位是常用折中'],
        ['k' => 'security.captcha_ttl_min', 'g' => 'security', 't' => 'number', 'label' => '验证码有效期（分钟）', 'd' => 5, 'max' => 30, 'min' => 1, 'hint' => '过期后必须刷新。太短会让正常用户频繁重输，太长会扩大暴力破解窗口'],
        ['k' => 'entry.wechat_appid', 'g' => 'entry', 't' => 'text', 'label' => '公众号 AppID', 'd' => '', 'max' => 64, 'hint' => '仅用于展示/后续扩展，不参与登录流程'],
        ['k' => 'entry.qrcode_api', 'g' => 'entry', 't' => 'text', 'label' => '二维码生成服务', 'd' => '', 'max' => 300, 'hint' => '留空则用内置的前端二维码库离线生成，不请求第三方；填了则用该服务，形如 https://api.example.com/qr?data={url}'],
        ['k' => 'entry.pwa_enabled', 'g' => 'entry', 't' => 'bool', 'label' => '启用 PWA（可安装到桌面）', 'd' => true],
        ['k' => 'entry.pwa_short_name', 'g' => 'entry', 't' => 'text', 'label' => 'PWA 短名称', 'd' => 'Kwrt', 'max' => 24],
        ['k' => 'entry.pwa_theme_color', 'g' => 'entry', 't' => 'color', 'label' => 'PWA 主题色', 'd' => '#2563eb'],
    ];

    /** 键 => 定义 的索引。 */
    public static function byKey(): array
    {
        $m = [];
        foreach (self::SCHEMA as $s) {
            $m[$s['k']] = $s;
        }
        return $m;
    }

    /** 某分组下的全部设置项。 */
    public static function ofGroup(string $g): array
    {
        return array_values(array_filter(self::SCHEMA, static fn($s) => $s['g'] === $g));
    }
}
