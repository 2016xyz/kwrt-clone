#!/usr/bin/env python3
"""
站点配置中心 —— schema 驱动的可配置站点信息。

设计要点：
  · 所有可配置项集中在一张 SCHEMA 表里（键、分组、类型、默认值、校验、说明）。
    管理端据此自动渲染表单，新增配置项只需在 SCHEMA 里加一行。
  · 值统一存 settings 表（key/value 文本），读取时按类型反序列化并做校验。
  · 敏感项（SMTP 密码、GitHub Token）在对外接口中做掩码，不原文回传。
"""
import json
import os
import re
import sqlite3
import time

from . import dbutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "users.db")

# 分组元信息（顺序即界面顺序）
GROUPS = [
    ("brand",    "品牌与外观", "站点名称、Logo、主题色"),
    ("home",     "首页文案",   "主标题、副标题、提示语"),
    ("links",    "页脚与联系", "页脚补充文案、软件库入口、联系方式"),
    ("build",    "构建设置",   "构建后端、并发、默认版本、配额"),
    ("download", "下载与链接", "有效期、访问控制、外链策略"),
    ("mail",     "邮件通知",   "SMTP 服务器与通知模板"),
    ("github",   "GitHub 构建", "GitHub Actions 触发配置"),
    ("update",   "程序更新",   "从 GitHub 检查本程序的版本更新"),
    ("sponsor",  "赞助设置",   "赞助开关、货币、套餐与金额"),
    ("pay",      "在线支付",   "支付宝当面付：APPID、密钥、网关闭环"),
    ("domain",   "域名与 CDN", "域名绑定、可信 Host/代理、CDN 回源与静态缓存"),
    ("pages",    "页面与入口", "逐页开关：关闭后该页对外不可见"),
    ("entry",    "公众号与 App", "App 下载、公众号引导、PWA 安装"),
    ("security", "安全与验证",   "登录/注册验证码、防自动化"), 
]

# type: text | textarea | number | bool | select | url | email | color | password | json
SCHEMA = [
    # ---------------- 品牌与外观 ----------------
    dict(k="site_name",  g="brand", t="text", d="Kwrt(OpenWrt)软路由固件下载与在线定制编译",
         label="站点名称", max=120, hint="显示在浏览器标题与页头"),
    dict(k="site_short", g="brand", t="text", d="Kwrt", label="站点简称", max=24,
         hint="Logo 旁显示的短名称"),
    dict(k="site_desc",  g="brand", t="textarea", d="为你的设备在线定制并实时编译 OpenWrt 固件",
         label="站点简介", max=300),
    dict(k="logo_url",   g="brand", t="text", d="/static/logo.png", label="Logo 图片地址", max=300),
    dict(k="favicon_url",g="brand", t="text", d="/static/logo.png", label="站点图标地址", max=300),
    dict(k="primary_color", g="brand", t="color", d="#2563eb", label="主色",
         hint="影响按钮与强调色；蓝＝信任、专业"),
    dict(k="theme_default", g="brand", t="select", d="auto", label="默认主题",
         opts=["auto", "light", "dark"], hint="auto 跟随系统"),
    # ★ 去掉了 hidden=True：后台界面本来就支持 t=json 编辑（与 sponsor.tiers 同一套
    #   文本框 + JSON.parse 流程），hidden 只是让人**看不见**这个键 ——
    #   于是「页头导航链接」既不能配、也没代码用，等于彻底不存在。
    dict(k="homepage_links", g="brand", t="json", d=[], label="页头导航链接",
         hint='JSON 数组，如 [{"label":"文档","url":"/docs"}]。'
              '只允许 http(s) 链接与站内相对路径（/docs），其它一律忽略'),

    # ---------------- 首页文案 ----------------
    dict(k="hero_title", g="home", t="text", d="下载或定制适用于您设备的 OpenWrt 固件",
         label="主标题", max=140),
    dict(k="hero_subtitle", g="home", t="textarea",
         d="输入设备名称或型号，选择需要的软件包，服务器将为你实时编译固件。",
         label="副标题", max=300),
    dict(k="devices_hint", g="home", t="text", d="输入设备的名称或型号", label="设备检索提示", max=80),
    dict(k="customize_title", g="home", t="text",
         d="自定义构建固件 (为爱发电,请勿滥用,欢迎 赞助)", label="定制区标题", max=140),
    dict(k="announcement", g="home", t="textarea", d="", label="站点公告", max=600,
         hint="留空则不显示；显示在首页顶部"),
    dict(k="footer_text", g="home", t="textarea",
         d="本站为 openwrt.ai 功能复刻演示站，固件由 OpenWrt 官方 ImageBuilder 实时编译。",
         label="页脚文案", max=400),
    dict(k="show_help", g="home", t="bool", d=True, label="显示帮助入口"),

    # ---------------- 页脚与联系 ----------------
    dict(k="footer_moat_title", g="links", t="text", d="",
         label="页脚·壁垒标题", max=80),
    dict(k="footer_moat_text", g="links", t="textarea",
         d="从设备识别、依赖求解、交叉编译到产物签发，全链路自研实现，"
           "不依赖任何第三方托管服务。",
         label="页脚·壁垒说明", max=600),
    dict(k="packages_url", g="links", t="text", d="/packages/", label="自建软件库入口", max=300),
    dict(k="contact_email", g="links", t="email", d="", label="联系邮箱", max=160),
    dict(k="contact_text", g="links", t="textarea", d="", label="联系页说明", max=600),
    dict(k="icp_text", g="links", t="text", d="", label="备案信息", max=120),

    # ---------------- 构建设置 ----------------
    dict(k="builder.enabled", g="build", t="bool", d=True, label="启用构建服务"),
    dict(k="builder.backend", g="build", t="select", d="local", opts=["local", "github"],
         label="构建后端", hint="local＝本机 ImageBuilder 实编；github＝GitHub Actions"),
    dict(k="builder.max_concurrent", g="build", t="number", d=1, min=1, max=8, label="并发构建数",
         hint="受磁盘与内存限制，建议保持 1～2"),
    dict(k="default_quota", g="build", t="number", d=12, min=0, max=9999,
         label="默认插件配额", hint="非赞助用户可勾选 luci-app 数量上限"),
    dict(k="build_default_version", g="build", t="select", d="25.12", opts=["25.12", "24.10"],
         label="默认固件分支"),
    dict(k="build_vip_hint", g="build", t="text",
         d="赞助用户可使用 VIP 通道，减少排队等待", label="VIP 通道提示", max=140),
    dict(k="build_allow_anonymous", g="build", t="bool", d=True, label="允许匿名构建"),

    # ---------------- 下载与链接 ----------------
    dict(k="download.link_ttl_hours", g="download", t="number", d=72, min=1, max=8760,
         label="下载链接有效期（小时）", hint="邮件与 API 返回的下载链接到期后失效"),
    dict(k="download.require_login", g="download", t="bool", d=False,
         label="下载需登录", hint="开启后仅登录用户可下载自建固件"),
    dict(k="download.max_hits", g="download", t="number", d=0, min=0, max=100000,
         label="单链接最大下载次数", hint="0 表示不限次数"),
    dict(k="download.serve_local", g="download", t="bool", d=True, label="允许从本站下载"),
    dict(k="download.external_host", g="download", t="text", d="",
         label="外部下载域名", hint="填写后下载链接指向该域名（如 CDN/对象存储）", max=200),
    dict(k="download.email_on_ready", g="download", t="bool", d=True,
         label="构建完成自动发邮件", hint="需先在「邮件通知」中配置 SMTP"),

    # ---------------- 邮件通知 ----------------
    dict(k="mail.enabled", g="mail", t="bool", d=False, label="启用邮件通知"),
    dict(k="mail.host", g="mail", t="text", d="", label="SMTP 服务器", max=200,
         hint="如 smtp.qq.com / smtp.163.com"),
    dict(k="mail.port", g="mail", t="number", d=465, min=1, max=65535, label="SMTP 端口"),
    dict(k="mail.encryption", g="mail", t="select", d="ssl", opts=["ssl", "starttls", "none"],
         label="加密方式"),
    dict(k="mail.user", g="mail", t="text", d="", label="SMTP 账号", max=200),
    dict(k="mail.password", g="mail", t="password", d="", label="SMTP 密码/授权码",
         secret=True, max=200),
    dict(k="mail.from_name", g="mail", t="text", d="Kwrt 固件构建", label="发件人名称", max=80),
    dict(k="mail.from_addr", g="mail", t="email", d="", label="发件人地址", max=160),
    dict(k="mail.subject_tpl", g="mail", t="text",
         d="[Kwrt] 你的 {target} 固件已构建完成", label="邮件主题模板", max=200,
         hint="可用变量：{target} {profile} {version} {hours}"),
    dict(k="mail.body_tpl", g="mail", t="textarea",
         d=("你好 {username}：\n\n"
            "你定制的 {target} / {profile} 固件（{version}）已构建完成，"
            "共 {count} 个文件，耗时 {duration}。\n\n"
            "下载链接（{hours} 小时内有效）：\n{links}\n\n"
            "提示：链接过期后可在站点的构建记录中重新生成。\n"),
         label="邮件正文模板", max=3000,
         hint="可用变量：{username} {target} {profile} {version} {count} {duration} {hours} {links}"),
    dict(k="mail.notify_fail", g="mail", t="bool", d=True, label="构建失败时也通知"),

    # ---------------- 注册邮箱验证 ----------------
    dict(k="mail.verify_register", g="mail", t="bool", d=False,
         label="注册需邮箱验证",
         hint="开启后新用户必须点击邮件中的链接才能激活账号；关闭则注册即用"),
    dict(k="mail.verify_ttl_hours", g="mail", t="number", d=24, min=1, max=720,
         label="验证链接有效期（小时）"),
    dict(k="mail.verify_subject", g="mail", t="text",
         d="[Kwrt] 请验证你的邮箱", label="验证邮件主题", max=200),
    dict(k="mail.verify_body", g="mail", t="textarea",
         d=("你好 {username}：\n\n"
            "感谢注册 {site}。请点击下面的链接完成邮箱验证：\n\n"
            "{link}\n\n"
            "链接 {hours} 小时内有效。若这不是你本人的操作，忽略本邮件即可。\n"),
         label="验证邮件正文", max=3000,
         hint="可用变量：{username} {site} {link} {hours} {email}"),

    # ---------------- 找回密码（忘记密码）----------------
    dict(k="mail.reset_ttl_hours", g="mail", t="number", d=2, min=1, max=168,
         label="重置链接有效期（小时）",
         hint="默认 2 小时。改口令的链接比注册验证更敏感，别设太长"),
    dict(k="mail.reset_subject", g="mail", t="text",
         d="[Kwrt] 重置你的登录密码", label="找回密码邮件主题", max=200),
    dict(k="mail.reset_body", g="mail", t="textarea",
         d=("你好 {username}：\n\n"
            "我们收到了重置 {site} 登录密码的请求。点击下面的链接设置新密码：\n\n"
            "{link}\n\n"
            "链接 {hours} 小时内有效，且只能使用一次。\n"
            "如果这不是你本人的操作，请忽略本邮件 —— 你的密码不会发生任何变化。\n"),
         label="找回密码邮件正文", max=3000,
         hint="可用变量：{username} {site} {link} {hours} {email}"),

    # ---------------- GitHub 构建 ----------------
    dict(k="gh.enabled", g="github", t="bool", d=False, label="启用 GitHub Actions 构建"),
    dict(k="gh.repo", g="github", t="text", d="", label="仓库（owner/repo）", max=200,
         hint="需已放置构建 workflow"),
    dict(k="gh.workflow", g="github", t="text", d="build-firmware.yml", label="Workflow 文件名", max=200),
    dict(k="gh.ref", g="github", t="text", d="main", label="分支 / 引用", max=120),
    dict(k="gh.token", g="github", t="password", d="", label="GitHub Token",
         secret=True, hint="需 repo + workflow 权限"),
    # 多队列：一个 JSON 数组，每项一个可独立派发的 GitHub 构建队列。
    # 留空则回落到上面单队列的 gh.repo/gh.token（老配置无需改动）。
    dict(k="gh.queues", g="github", t="json", d=[], label="构建队列（多队列）",
         hint='JSON 数组。每项：{"name":"主队列","repo":"owner/repo",'
              '"token":"ghp_xxx","workflow":"build-firmware.yml","ref":"main",'
              '"enabled":true}。多个队列之间**轮流派发**，可用于分担构建压力、'
              '或给不同 openwrt 分支挂不同仓库。留空则使用上面的单队列配置'),
    dict(k="gh.artifact_pattern", g="github", t="text", d="openwrt-*", label="产物名匹配", max=120),
    dict(k="gh.mirror_artifacts", g="github", t="bool", d=True,
         label="把产物回传到本站存储",
         hint="开启后 GitHub 产物会下载回本站，下载链接有效期由本站统一管理；"
              "关闭则直接使用 GitHub 产物地址（有效期由 GitHub 保留策略决定）"),
    dict(k="gh.mirror_timeout", g="github", t="number", d=3600, min=60, max=21600,
         label="产物回传超时（秒）", hint="镜像体积较大、跨国带宽较慢时需放宽"),

    # ---------------- 程序更新 ----------------
    dict(k="update.enabled", g="update", t="bool", d=True, label="启用更新检查"),
    dict(k="update.repo", g="update", t="text", d="2016xyz/kwrt-clone",
         label="更新源仓库（owner/repo）", max=200,
         hint="本程序源码所在仓库。检查时优先读它的 Release；没有 Release 就读该仓库的 "
              "VERSION 文件。私有仓库需在下面配 Token"),
    dict(k="update.ref", g="update", t="text", d="main", label="回退分支 / 引用", max=120,
         hint="仓库没有 Release 时，从哪个分支读 VERSION 文件"),
    dict(k="update.token", g="update", t="password", d="", label="更新源 Token",
         secret=True, hint="私有仓库必填；留空则回落到上面 GitHub 构建的 Token"),
    dict(k="update.include_prerelease", g="update", t="bool", d=False,
         label="包含预发布版本", hint="开启后 -beta / -rc 之类的版本也会被当作可用更新"),
    dict(k="update.cache_minutes", g="update", t="number", d=10, min=1, max=1440,
         label="检查结果缓存（分钟）",
         hint="GitHub 未认证请求每小时只有 60 次，缓存可避免把限额打满"),

    # ---------------- 赞助设置 ----------------
    dict(k="sponsor.enabled", g="sponsor", t="bool", d=True, label="启用赞助"),
    dict(k="sponsor.currency", g="sponsor", t="select", d="CNY", opts=["CNY", "USD", "EUR", "JPY", "HKD"],
         label="货币单位"),
    dict(k="sponsor.note", g="sponsor", t="textarea",
         d="赞助完全自愿。赞助后可解锁全部定制选项并享受 VIP 构建通道，"
           "但不提供任何形式的固件保证或技术支持承诺。",
         label="赞助说明", max=800),
    dict(k="sponsor.pay_qr", g="sponsor", t="text", d="", label="收款码图片地址", max=300),
    dict(k="sponsor.contact", g="sponsor", t="text", d="", label="赞助后联系说明", max=200),
    dict(k="sponsor.auto_approve", g="sponsor", t="bool", d=True,
         label="允许自助确认赞助", hint="关闭后需管理员在后台手动标记"),
    # 套餐：JSON 数组。金额与权益全由管理员定义。
    dict(k="sponsor.tiers", g="sponsor", t="json",
         d=[
             {"name": "月付赞助", "amount": 10, "days": 30,
              "perks": ["解锁全部定制项", "VIP 构建通道", "自定义主机名与签名"]},
             {"name": "季付赞助", "amount": 26, "days": 90,
              "perks": ["解锁全部定制项", "VIP 构建通道", "自定义主机名与签名", "插件数量不限"]},
             {"name": "年付赞助", "amount": 88, "days": 365,
              "perks": ["解锁全部定制项", "VIP 构建通道", "自定义主机名与签名",
                        "插件数量不限", "优先技术支持"]},
         ],
         label="赞助套餐", hint="JSON 数组，每项含 name / amount / days / perks[]"),

    # ---------------- 在线支付（支付宝当面付） ----------------
    dict(k="pay.alipay_enabled", g="pay", t="bool", d=False,
         label="启用支付宝当面付",
         hint="开启后用户在赞助页扫码支付，服务端查单确认到账并自动置位赞助态"),
    dict(k="pay.alipay_app_id", g="pay", t="text", d="", label="应用 APPID", max=64,
         hint="支付宝开放平台 → 应用信息 → APPID"),
    dict(k="pay.alipay_private_key", g="pay", t="textarea", d="",
         label="应用私钥", secret=True, max=4000,
         hint="粘贴密钥内容即可，无需自行添加 PEM 头（PKCS#8 / PKCS#1 均可）"),
    dict(k="pay.alipay_public_key", g="pay", t="textarea", d="",
         label="支付宝公钥", secret=True, max=4000,
         hint="注意是「支付宝公钥」，不是你的应用公钥——填错会导致验签失败"),
    dict(k="pay.alipay_sandbox", g="pay", t="bool", d=True,
         label="使用沙箱环境", hint="上线前请关闭，切到正式网关"),
    dict(k="pay.alipay_gateway", g="pay", t="text", d="", label="网关地址（留空用默认）", max=200),
    dict(k="pay.notify_url", g="pay", t="text", d="", label="异步通知地址", max=300,
         hint="留空则按站点地址自动推导为 {site}/api/v1/alipay/notify，须为公网可访问"),
    dict(k="pay.alipay_subject_prefix", g="pay", t="text", d="赞助",
         label="订单标题前缀", max=40),
    dict(k="pay.auto_activate", g="pay", t="bool", d=True,
         label="支付成功后自动置位赞助",
         hint="关闭后仅记录已支付订单，由管理员在后台确认发放"),
    dict(k="pay.order_ttl_minutes", g="pay", t="number", d=15, min=1, max=1440,
         label="订单超时（分钟）", hint="超时未支付的订单自动关闭"),
    dict(k="pay.poll_seconds", g="pay", t="number", d=3, min=1, max=30,
         label="前端查单间隔（秒）"),
    dict(k="pay.refund_enabled", g="pay", t="bool", d=True,
         label="开启退款申请",
         hint="开启后用户可对已支付订单提交退款申请，需管理员在「退款审核」中审批"),
    dict(k="pay.currency", g="pay", t="text", d="CNY", label="货币代码", max=8,
         hint="仅用于展示，如 CNY / USD"),
    dict(k="pay.result_note", g="pay", t="textarea", d="",
         label="支付成功提示", max=300,
         hint="支付完成后在订单页展示的补充说明，留空则不显示"),

    # ---------------- 域名与 CDN ----------------
    dict(k="site.domain", g="domain", t="text", d="", label="绑定域名", max=120,
         hint="如 fw.example.com。填写后所有绝对链接（验证邮件、下载链接、支付回调）"
              "一律用该域名生成，不再取请求头 Host —— 这是防 Host 头注入的关键"),
    dict(k="site.scheme", g="domain", t="select", d="auto", label="对外协议",
         opts=["auto", "https", "http"],
         hint="auto＝信任反代/CDN 的 X-Forwarded-Proto；不确定时选 https"),
    dict(k="site.trusted_hosts", g="domain", t="textarea", d="",
         label="可信 Host 白名单",
         hint="每行一个，可用逗号分隔。绑定了域名后，不在此列表且不等于绑定域名的 "
              "Host 一律拒绝，避免 Host 头注入"),
    dict(k="site.allow_any_host", g="domain", t="bool", d=True,
         label="未绑域名时接受任意 Host",
         hint="仅在「绑定域名」为空且白名单也为空时生效。正式上线建议关闭"),
    dict(k="site.trusted_proxies", g="domain", t="textarea", d="",
         label="可信代理 / CDN 网段",
         hint="每行一个 CIDR，如 172.16.0.0/12、2400:cb00::/32（Cloudflare）。"
              "★ 只有直连对端落在这里面，才会采信 X-Forwarded-For / CF-Connecting-IP；"
              "否则一律以 REMOTE_ADDR 为准 —— 否则源 IP 可被伪造，封禁与限速同时失效"),
    dict(k="site.cdn_enabled", g="domain", t="bool", d=False, label="启用 CDN"),
    dict(k="site.cdn_base", g="domain", t="text", d="", label="CDN 资源域名", max=200,
         hint="如 https://cdn.example.com。启用后页面里的静态资源一律走该域名，"
              "并自动带 ?v=版本号做长缓存失效"),
    dict(k="site.asset_version", g="domain", t="text", d="", label="静态资源版本号", max=40,
         hint="改这里即可让 CDN 上的旧静态资源立即失效（配合 long max-age immutable）"),
    dict(k="site.force_https", g="domain", t="bool", d=False, label="强制跳转 HTTPS",
         hint="开启后 http 请求 301 到 https，请确认证书已就绪再开"),

    # ---------------- 页面与入口 ----------------
    dict(k="page.disabled_behavior", g="pages", t="select", d="404",
         label="关闭页面的表现", opts=["404", "redirect", "message"],
         hint="404＝当作不存在；redirect＝跳到指定地址；message＝显示一段提示"),
    dict(k="page.disabled_redirect", g="pages", t="text", d="/", label="跳转目标", max=300,
         hint="仅在表现选 redirect 时生效"),
    dict(k="page.disabled_message", g="pages", t="textarea",
         d="该功能已暂时下线，请稍后再试。", label="下线提示文案", max=300),
    dict(k="page.index_enabled",    g="pages", t="bool", d=True, label="首页",
         hint="关闭后首页不可用，请确认仍有其他入口，否则站点等于关闭"),
    dict(k="page.packages_enabled", g="pages", t="bool", d=True, label="软件库"),
    dict(k="page.newpkg_enabled",   g="pages", t="bool", d=True, label="插件提议"),
    dict(k="page.fadian_enabled",   g="pages", t="bool", d=True, label="为爱发电"),
    dict(k="page.contact_enabled",  g="pages", t="bool", d=True, label="联系我们"),
    dict(k="page.login_enabled",    g="pages", t="bool", d=True, label="登录",
         hint="关闭后登录页不可用；已登录会话仍然有效，但无法再登录。"
              "★ 关闭前请确认你能从后台恢复，否则会把自己锁在门外"),
    dict(k="page.register_enabled", g="pages", t="bool", d=True, label="注册"),
    dict(k="page.reset_enabled",    g="pages", t="bool", d=True, label="找回密码",
         hint="关闭后 /reset/ 页面与重置接口不可用（已发出的重置链接也会失效）。"
              "该功能依赖 SMTP：邮件未配置时用户申请会得到明确提示，不会静默失败"),
    # ★ 这个键此前被代码读取（app/main.py 的注册接口与 /api/v1/announcement）
    #   却**不在 schema 里** —— 于是 SS.get 永远返回默认值，管理员无从关闭注册。
    #   属于「代码以为有、实际不存在」的幽灵键，由 tools/verify_integrity.py 查出。
    #   注意它与 page.register_enabled 的区别：
    #     page.register_enabled  —— 注册**页面**是否可见
    #     registration_open      —— 是否**受理**注册（页面可开着但停止收人）
    dict(k="registration_open", g="pages", t="bool", d=True, label="开放注册",
         hint="关闭后不再受理新注册（注册页仍可访问，但提交会被拒绝）。"
              "与「注册」开关的区别：那个控制页面可见性，这个控制是否收人。"),
    dict(k="page.download_enabled", g="pages", t="bool", d=True, label="固件下载",
         hint="关闭后预编译镜像与产物下载入口不可见"),

    # ---------------- 公众号与 App ----------------
    dict(k="entry.app_enabled", g="entry", t="bool", d=False, label="显示 App 下载入口"),
    dict(k="entry.app_name", g="entry", t="text", d="Kwrt 助手", label="App 名称", max=60),
    dict(k="entry.app_desc", g="entry", t="textarea", d="",
         label="App 简介", max=300),
    dict(k="entry.app_android_url", g="entry", t="text", d="", label="Android 下载地址", max=400,
         hint="APK 直链或应用商店地址"),
    dict(k="entry.app_ios_url", g="entry", t="text", d="", label="iOS 下载地址", max=400),
    dict(k="entry.app_qrcode_url", g="entry", t="text", d="", label="App 下载二维码图片", max=400,
         hint="留空则前端按下载地址自动生成二维码"),
    dict(k="entry.wechat_enabled", g="entry", t="bool", d=False, label="显示公众号入口"),
    dict(k="entry.wechat_official_name", g="entry", t="text", d="", label="公众号名称", max=60),
    dict(k="entry.wechat_qrcode_url", g="entry", t="text", d="", label="公众号二维码图片", max=400),
    dict(k="entry.wechat_follow_hint", g="entry", t="textarea",
         d="关注公众号获取固件更新通知与构建进度提醒。", label="关注引导文案", max=300),
    # ---------------- 安全与验证 ----------------
    # 验证码开关是用户明确要求的「后台可自定义」项。默认**关闭**：
    # 打开它会让登录多一步，属于运营选择，不该替管理员决定。
    dict(k="security.captcha_enabled", g="security", t="bool", d=False, label="登录验证码",
         hint="开启后登录必须填写图形验证码，用于挡自动化撞库。"
              "验证码由服务端生成、一次性使用、有有效期，答案不以明文入库"),
    dict(k="security.captcha_on_register", g="security", t="bool", d=False, label="注册验证码",
         hint="开启后注册也必须填写验证码（防批量注册小号）。独立于登录开关"),
    dict(k="security.captcha_on_reset", g="security", t="bool", d=True, label="找回密码验证码",
         hint="找回密码**申请**接口是否要求验证码，默认开。"
              "该接口会真实发信，没有验证码就是现成的邮件轰炸放大器"),
    dict(k="security.captcha_length", g="security", t="number", d=4, min=3, max=6,
         label="验证码位数",
         hint="3–6 位。位数越多越难被 OCR，也越容易让人输错，4 位是常用折中"),
    dict(k="security.captcha_ttl_min", g="security", t="number", d=5, min=1, max=30,
         label="验证码有效期（分钟）",
         hint="过期后必须刷新。太短会让正常用户频繁重输，太长会扩大暴力破解窗口"),

    dict(k="entry.wechat_appid", g="entry", t="text", d="", label="公众号 AppID", max=64,
         hint="仅用于展示/后续扩展，不参与登录流程"),
    dict(k="entry.qrcode_api", g="entry", t="text", d="", label="二维码生成服务", max=300,
         hint="留空则用内置的前端二维码库离线生成，不请求第三方；"
              "填了则用该服务，形如 https://api.example.com/qr?data={url}"),
    dict(k="entry.pwa_enabled", g="entry", t="bool", d=True, label="启用 PWA（可安装到桌面）"),
    dict(k="entry.pwa_short_name", g="entry", t="text", d="Kwrt", label="PWA 短名称", max=24),
    dict(k="entry.pwa_theme_color", g="entry", t="color", d="#2563eb", label="PWA 主题色"),
]


def db():
    # 见 app/dbutil.py：with 退出时要「提交 + 关闭」，否则每次调用泄漏 1 个 fd
    c = sqlite3.connect(DB, timeout=20, factory=dbutil.ClosingConnection)
    c.row_factory = sqlite3.Row
    return c


def _init():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS settings(
            key TEXT PRIMARY KEY, value TEXT, updated REAL)""")


_init()

BY_KEY = {s["k"]: s for s in SCHEMA}


# --------------------------------------------------------------------------- #
# 类型转换
# --------------------------------------------------------------------------- #
def _enc(v, t):
    if t == "bool":
        return "true" if v else "false"
    if t == "json":
        return json.dumps(v, ensure_ascii=False)
    if t == "number":
        try:
            # int(float("inf")) 抛的是 OverflowError，不在 (TypeError, ValueError) 内
            f = float(v)
            if f != f or f in (float("inf"), float("-inf")):
                return "0"
            return str(int(f))
        except (TypeError, ValueError, OverflowError):
            return "0"
    return "" if v is None else str(v)


def _dec(raw, spec):
    t = spec["t"]
    if raw is None:
        return spec["d"]
    if t == "bool":
        return str(raw).lower() in ("1", "true", "yes", "on")
    if t == "number":
        try:
            f = float(raw)
            # 老值可能是 "1e400"/"nan" —— float("inf") 经 int() 抛 OverflowError，
            # 而它不在下面原本捕获的两个异常里，会让读取配置整条路径 500。
            if f != f or f in (float("inf"), float("-inf")):
                return spec["d"]
            return int(f)
        except (TypeError, ValueError, OverflowError):
            return spec["d"]
    if t == "json":
        try:
            v = json.loads(raw)
            return v if isinstance(v, (list, dict)) else spec["d"]
        except Exception:
            return spec["d"]
    return raw


def validate(key, value):
    """按 schema 校验并归一化一个值；返回 (ok, 归一化值, 错误信息)。"""
    spec = BY_KEY.get(key)
    if not spec:
        return False, None, f"未知配置项 {key}"
    t = spec["t"]
    if t == "bool":
        v = value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")
        return True, v, ""
    if t == "number":
        try:
            f = float(value)
            if f != f or f in (float("inf"), float("-inf")):
                return False, None, f"{spec['label']} 必须是有限数字"
            n = int(f)
        except (TypeError, ValueError, OverflowError):
            return False, None, f"{spec['label']} 必须是数字"
        lo, hi = spec.get("min"), spec.get("max")
        if lo is not None and n < lo:
            return False, None, f"{spec['label']} 不能小于 {lo}"
        if hi is not None and n > hi:
            return False, None, f"{spec['label']} 不能大于 {hi}"
        return True, n, ""
    if t == "select":
        v = str(value)
        if spec.get("opts") and v not in spec["opts"]:
            return False, None, f"{spec['label']} 只能取 {'/'.join(spec['opts'])}"
        return True, v, ""
    if t == "json":
        if isinstance(value, (list, dict)):
            v = value
        else:
            try:
                v = json.loads(value)
            except Exception:
                return False, None, f"{spec['label']} 不是合法 JSON"
        if not isinstance(v, (list, dict)):
            return False, None, f"{spec['label']} 必须是 JSON 数组或对象"
        if key == "sponsor.tiers":
            ok, err = _validate_tiers(v)
            if not ok:
                return False, None, err
        return True, v, ""
    if t == "email":
        v = str(value).strip()
        if v and "@" not in v:
            return False, None, f"{spec['label']} 不是合法邮箱"
        return True, v, ""
    if t == "url":
        v = str(value).strip()
        if v and not (v.startswith("http://") or v.startswith("https://") or v.startswith("/")):
            return False, None, f"{spec['label']} 必须以 http(s):// 或 / 开头"
        return True, v, ""
    if t == "color":
        v = str(value).strip()
        # 原先只校验「以 # 开头且长度 4/7」，#";x"yy 这种 7 字符的串也能过 ——
        # 该值会被渲染进 CSS 变量/内联 style，必须限定为纯十六进制。
        if v and not re.fullmatch(r"#(?:[0-9A-Fa-f]{3}|[0-9A-Fa-f]{6})", v):
            return False, None, f"{spec['label']} 需为 #RGB 或 #RRGGBB（仅十六进制）"
        return True, v, ""
    # text / textarea / password
    v = str(value)
    mx = spec.get("max")
    if mx and len(v) > mx:
        return False, None, f"{spec['label']} 长度不能超过 {mx} 个字符"
    return True, v, ""


def _validate_tiers(v):
    if not isinstance(v, list):
        return False, "赞助套餐必须是 JSON 数组"
    if len(v) > 20:
        return False, "赞助套餐最多 20 个"
    for i, it in enumerate(v):
        if not isinstance(it, dict):
            return False, f"第 {i + 1} 个套餐不是对象"
        if not str(it.get("name", "")).strip():
            return False, f"第 {i + 1} 个套餐缺少 name"
        try:
            amt = float(it.get("amount"))
        except (TypeError, ValueError):
            return False, f"第 {i + 1} 个套餐 amount 必须是数字"
        if amt < 0:
            return False, f"第 {i + 1} 个套餐 amount 不能为负"
        try:
            days = int(it.get("days", 0))
        except (TypeError, ValueError):
            return False, f"第 {i + 1} 个套餐 days 必须是整数"
        if days < 1:
            return False, f"第 {i + 1} 个套餐 days 至少为 1"
        perks = it.get("perks", [])
        if not isinstance(perks, list):
            return False, f"第 {i + 1} 个套餐 perks 必须是数组"
    return True, ""


# --------------------------------------------------------------------------- #
# 读写
# --------------------------------------------------------------------------- #
def get(key, default=None):
    spec = BY_KEY.get(key)
    with db() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if r is None:
        return spec["d"] if spec else default
    return _dec(r["value"], spec) if spec else r["value"]


def set_(key, value):
    ok, v, err = validate(key, value)
    if not ok:
        raise ValueError(err)
    with db() as c:
        c.execute("INSERT OR REPLACE INTO settings(key,value,updated) VALUES(?,?,?)",
                  (key, _enc(v, BY_KEY[key]["t"]), time.time()))
    return v


def all_values(include_secret=False):
    """读取全部配置项（默认对敏感项掩码）。"""
    with db() as c:
        rows = {r["key"]: r["value"] for r in c.execute("SELECT key,value FROM settings")}
    out = {}
    for spec in SCHEMA:
        k = spec["k"]
        v = _dec(rows.get(k), spec) if k in rows else spec["d"]
        if spec.get("secret") and not include_secret:
            out[k] = "********" if v else ""
        else:
            out[k] = v
    return out


def public_values():
    """对外（前台）可见的配置子集 —— 绝不含密钥类字段。"""
    keys = ["site_name", "site_short", "site_desc", "logo_url", "favicon_url",
            "primary_color", "theme_default", "hero_title", "hero_subtitle",
            "devices_hint", "customize_title", "announcement", "footer_text", "show_help",
            "footer_moat_title", "footer_moat_text",
            "packages_url", "contact_email", "contact_text", "icp_text",
            "build_default_version", "build_vip_hint", "build_allow_anonymous",
            "download.require_login", "download.link_ttl_hours",
            "sponsor.enabled", "sponsor.currency", "sponsor.note", "sponsor.pay_qr",
            "sponsor.contact", "sponsor.tiers", "sponsor.auto_approve",
            # 支付：只暴露「是否可用」与必要的前端参数，密钥类字段绝不出门
            "pay.alipay_enabled", "pay.auto_activate", "pay.poll_seconds",
            "pay.order_ttl_minutes",
            "builder.enabled", "default_quota",
            # 找回密码的链接有效期：找回页要显示「链接 X 小时内有效」。
            # 开关类信息走 /api/v1/site 的 nav.reset / captcha.reset，不在这里重复暴露。
            "mail.reset_ttl_hours"]
    out = {}
    for k in keys:
        try:
            out[k] = get(k)
        except Exception:
            pass
    return out


def schema_payload():
    """给管理端的表单描述（含分组与当前值）。"""
    vals = all_values()
    return {
        "groups": [{"k": g, "label": l, "desc": d} for g, l, d in GROUPS],
        "schema": [{k: v for k, v in s.items() if k != "d" or True} for s in SCHEMA],
        "values": vals,
    }
