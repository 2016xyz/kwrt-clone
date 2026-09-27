/* 预设插件包清单
 *
 * 这些是常见的 OpenWrt 应用/主题/工具，勾选后会自动加入构建的 packages 列表。
 * 分类用于界面上分组展示；name 必须是真实存在的软件包名，否则构建会失败。
 *
 * 注意：清单只列出「名称 + 中文说明」，不预设任何安装与否的判断 ——
 * 是否可用最终取决于所选固件分支与目标架构。
 */
window.KWRT_PRESETS = [
  /* ---------------- 网络与代理 ---------------- */
  { c: 'proxy', n: 'luci-app-openclash',         l: 'OpenClash',            d: 'Clash 客户端，规则分流' },
  { c: 'proxy', n: 'luci-app-passwall',          l: 'PassWall',             d: '代理工具集' },
  { c: 'proxy', n: 'luci-app-passwall2',         l: 'PassWall2',            d: '代理工具集（新版）' },
  { c: 'proxy', n: 'luci-app-ssr-plus',          l: 'SSR-Plus',             d: '老牌代理工具' },
  { c: 'proxy', n: 'luci-app-homeproxy',         l: 'HomeProxy',            d: 'Sing-box 内核代理' },
  { c: 'proxy', n: 'luci-app-nikki',             l: 'Nikki',                d: 'Mihomo 内核代理' },
  { c: 'proxy', n: 'luci-app-momo',              l: 'Momo',                 d: 'Mihomo 内核代理' },
  { c: 'proxy', n: 'luci-app-daed',              l: 'Daed',                 d: 'eBPF 透明代理' },
  { c: 'proxy', n: 'luci-app-v2raya',            l: 'v2rayA',               d: 'V2Ray Web 面板' },
  { c: 'proxy', n: 'luci-app-hijpass',           l: 'HiJpass',              d: '代理工具' },
  { c: 'proxy', n: 'luci-app-xray',              l: 'luci-xray',            d: 'Xray 内核' },
  { c: 'proxy', n: 'luci-app-neko-box',          l: 'NeKoBox',              d: '代理工具' },
  { c: 'proxy', n: 'luci-app-subconverter',      l: '订阅转换',              d: '订阅链接转换' },
  { c: 'proxy', n: 'luci-app-naiveproxy',        l: 'naiveproxy',           d: 'Naive 代理' },

  /* ---------------- 内网穿透与组网 ---------------- */
  { c: 'net', n: 'luci-app-zerotier',            l: 'ZeroTier 内网穿透',     d: '虚拟局域网' },
  { c: 'net', n: 'luci-app-frpc',                l: 'frpc 内网穿透',         d: 'FRP 客户端' },
  { c: 'net', n: 'luci-app-frps',                l: 'frps 服务端',           d: 'FRP 服务端' },
  { c: 'net', n: 'luci-app-nps',                 l: 'NPS 内网穿透',          d: '轻量穿透' },
  { c: 'net', n: 'luci-app-npc',                 l: 'NPS 客户端',            d: 'NPS 客户端' },
  { c: 'net', n: 'luci-app-easytier',            l: 'EasyTier 异地组网',     d: '去中心化组网' },
  { c: 'net', n: 'luci-app-natmap',              l: 'NatMap 内网穿透',       d: 'NAT 打洞' },
  { c: 'net', n: 'luci-app-ddns',                l: 'DDNS 动态域名',         d: '动态域名解析' },
  { c: 'net', n: 'luci-app-ddns-go',             l: 'DDNS-GO',              d: '多平台 DDNS' },
  { c: 'net', n: 'luci-app-wireguard',           l: 'WireGuard',            d: '现代 VPN 协议' },
  { c: 'net', n: 'luci-app-softethervpn',        l: 'SoftEther VPN',        d: '多协议 VPN' },
  { c: 'net', n: 'luci-app-ipsec-server',        l: 'IPSec 服务端',          d: 'IPSec VPN' },
  { c: 'net', n: 'luci-app-easymesh',            l: 'EasyMesh 组网',         d: '多机 Mesh' },

  /* ---------------- 下载与存储 ---------------- */
  { c: 'dl', n: 'luci-app-aria2',                l: 'Aria2 下载',           d: '多协议下载器' },
  { c: 'dl', n: 'luci-app-qbittorrent',          l: 'qBittorrent',          d: 'BT 下载' },
  { c: 'dl', n: 'luci-app-transmission',         l: 'Transmission',         d: 'BT 下载' },
  { c: 'dl', n: 'luci-app-thunder',              l: '迅雷下载',              d: '迅雷远程下载' },
  { c: 'dl', n: 'luci-app-xlnetacc',             l: '迅雷快鸟',              d: '宽带提速' },
  { c: 'dl', n: 'luci-app-broadbandacc',         l: '宽带提速',              d: '宽带加速' },
  { c: 'dl', n: 'luci-app-aliyundrive-webdav',   l: '阿里云盘 WebDAV',       d: '云盘挂载' },
  { c: 'dl', n: 'luci-app-clouddrive2',          l: 'CloudDrive2',          d: '多云盘挂载' },
  { c: 'dl', n: 'luci-app-rclone',               l: 'Rclone 云盘同步',       d: '云盘挂载同步' },
  { c: 'dl', n: 'luci-app-openlist2',            l: 'OpenList（原 alist）',  d: '多存储文件列表' },
  { c: 'dl', n: 'luci-app-kodexplorer',          l: '可道云网盘',            d: '在线文件管理' },
  { c: 'dl', n: 'luci-app-linkease',             l: '易有云',                d: '文件管理' },
  { c: 'dl', n: 'luci-app-dufs',                 l: 'DuFS 文件管理',         d: '支持 WebDAV' },
  { c: 'dl', n: 'luci-app-filebrowser-q',        l: 'FileBrowser',          d: '文件网盘管理' },
  { c: 'dl', n: 'luci-app-quickfile',            l: '文件管理器',            d: '轻量文件管理' },

  /* ---------------- 文件共享 ---------------- */
  { c: 'share', n: 'luci-app-samba4',            l: 'Samba 共享',            d: 'Windows 文件共享' },
  { c: 'share', n: 'luci-app-ksmbd',             l: 'KSMBd 内核共享',        d: '内核级文件共享' },
  { c: 'share', n: 'luci-app-vsftpd',            l: 'FTP 服务器',            d: 'vsftpd' },
  { c: 'share', n: 'luci-app-webdav',            l: 'WebDAV (Nginx)',       d: '基于 Nginx' },
  { c: 'share', n: 'luci-app-unishare',          l: '多种文件共享',          d: '聚合共享' },
  { c: 'share', n: 'luci-app-cifs-mount',        l: 'CIFS 共享盘挂载',       d: '挂载 SMB' },
  { c: 'share', n: 'luci-app-minidlna',          l: 'MiniDLNA',             d: 'DLNA 媒体服务' },
  { c: 'share', n: 'luci-app-p910nd',            l: 'USB 打印机共享',        d: '打印服务器' },
  { c: 'share', n: 'luci-app-cupsd',             l: 'CUPS 打印服务',         d: '打印服务器' },

  /* ---------------- 磁盘与存储 ---------------- */
  { c: 'disk', n: 'luci-app-diskman',            l: '磁盘管理',              d: '分区与格式化' },
  { c: 'disk', n: 'luci-app-partexp',            l: '分区扩容',              d: '根分区扩容' },
  { c: 'disk', n: 'luci-app-hd-idle',            l: '硬盘休眠',              d: '空闲停转' },
  { c: 'disk', n: 'automount',                   l: '自动挂载 USB/硬盘',     d: '热插拔自动挂载' },

  /* ---------------- 网络优化与管理 ---------------- */
  { c: 'mgr', n: 'luci-app-turboacc',            l: '网络加速',              d: '流量 offload 加速' },
  { c: 'mgr', n: 'luci-app-mwan3-nft',           l: 'MWAN3 负载均衡',        d: '多线负载' },
  { c: 'mgr', n: 'luci-app-syncdial',            l: '多拨',                  d: '多拨合并' },
  { c: 'mgr', n: 'luci-app-eqosplus',            l: 'IP 限速',               d: '按 IP 限速' },
  { c: 'mgr', n: 'luci-app-qosmate',             l: 'QosMate 流控',          d: 'QoS 流量控制' },
  { c: 'mgr', n: 'luci-app-sqm-autorate',        l: 'SQM Autorate',         d: '自适应 QoS' },
  { c: 'mgr', n: 'luci-app-bandix',              l: 'Bandix 流量监控',       d: '限速与监控' },
  { c: 'mgr', n: 'luci-app-nlbwmon',             l: '流量统计',              d: '带宽统计' },
  { c: 'mgr', n: 'luci-app-wrtbwmon',            l: '流量监控',              d: '实时流量' },
  { c: 'mgr', n: 'luci-app-socat',               l: 'Socat 端口转发',        d: '端口映射' },
  { c: 'mgr', n: 'luci-app-firewall',            l: '防火墙增强',            d: '规则管理' },
  { c: 'mgr', n: 'luci-app-arpbind',             l: 'ARP 绑定',              d: 'IP/MAC 绑定' },
  { c: 'mgr', n: 'luci-app-netdata',             l: 'Netdata 性能监测',      d: '系统监控' },
  { c: 'mgr', n: 'luci-app-statistics',          l: 'collectd 统计',         d: '系统监控统计' },
  { c: 'mgr', n: 'luci-app-fastnet',             l: '网络体检/测速',         d: '网络诊断' },
  { c: 'mgr', n: 'btop',                         l: 'btop 性能监控',         d: '终端资源监控' },
  { c: 'mgr', n: 'luci-app-ttyd',                l: 'TTYD 网页终端',         d: '网页版命令行' },
  { c: 'mgr', n: 'luci-app-lucky',               l: 'Lucky 网络工具',        d: '多功能网络工具' },
  { c: 'mgr', n: 'luci-app-mosdns',              l: 'MosDNS',               d: 'DNS 转发分流' },
  { c: 'mgr', n: 'luci-app-smartdns',            l: 'SmartDNS',             d: 'DNS 加速' },
  { c: 'mgr', n: 'luci-app-acme',                l: 'ACME 证书申请',         d: 'HTTPS 证书' },
  { c: 'mgr', n: 'luci-app-watchcat',            l: '断网检测重启',          d: '离线自动重启' },
  { c: 'mgr', n: 'luci-app-timedreboot',         l: '定时重启',              d: '计划重启' },
  { c: 'mgr', n: 'luci-app-timewol',             l: '网络唤醒',              d: 'Wake on LAN' },
  { c: 'mgr', n: 'luci-app-taskplan',            l: '定时/开机任务',         d: '计划任务' },
  { c: 'mgr', n: 'luci-app-vlmcsd',              l: 'KMS 服务器',            d: 'Windows 激活' },
  { c: 'mgr', n: 'luci-app-snmpd',               l: 'SNMP 监控',             d: 'SNMP 服务' },
  { c: 'mgr', n: 'luci-app-wechatpush',          l: '通知推送',              d: '微信推送' },

  /* ---------------- 上网管控 ---------------- */
  { c: 'ctrl', n: 'luci-app-adguardhome',        l: 'AdGuard Home',         d: 'DNS 级去广告' },
  { c: 'ctrl', n: 'luci-app-adbyby-plus',        l: 'Adbyby 去广告',         d: '广告过滤' },
  { c: 'ctrl', n: 'luci-app-oaf',                l: '应用过滤',              d: '按应用管控' },
  { c: 'ctrl', n: 'luci-app-parentcontrol',      l: '家长控制',              d: '分时段管控' },
  { c: 'ctrl', n: 'luci-app-nft-timecontrol',    l: '上网时间控制',          d: '定时断网' },
  { c: 'ctrl', n: 'luci-app-accesscontrol-plus', l: '访问控制',              d: '上网权限' },
  { c: 'ctrl', n: 'luci-app-apfree-wifidog',     l: '无线认证',              d: 'WiFi 登录认证' },
  { c: 'ctrl', n: 'luci-app-guest-wifi',         l: '访客 WiFi',             d: '隔离访客网络' },
  { c: 'ctrl', n: 'luci-app-wifischedule',       l: 'WiFi 计划任务',         d: '定时开关 WiFi' },
  { c: 'ctrl', n: 'luci-app-cpulimit',           l: 'CPU 占用限制',          d: '限制单进程 CPU' },

  /* ---------------- 媒体与娱乐 ---------------- */
  { c: 'media', n: 'luci-app-unblockneteasemusic', l: '解锁网易云音乐',      d: '灰色歌曲解锁' },
  { c: 'media', n: 'luci-app-airplay2',          l: 'AirPlay2 音箱',         d: '苹果隔空播放' },
  { c: 'media', n: 'luci-app-msd_lite',          l: 'IPTV 组播',             d: '组播转单播' },
  { c: 'media', n: 'luci-app-iptvhelper',        l: 'IPTV 助手',             d: 'IPTV 转发' },
  { c: 'media', n: 'luci-app-uugamebooster',     l: 'UU 游戏加速器',         d: '游戏加速' },
  { c: 'media', n: 'luci-app-xunyou',            l: '迅游加速器',            d: '游戏加速' },
  { c: 'media', n: 'tvhelper',                   l: '盒子助手',              d: '电视盒子工具' },
  { c: 'media', n: 'luci-app-homeassistant',     l: 'Home Assistant',       d: '智能家居' },

  /* ---------------- 虚拟化与容器 ---------------- */
  { c: 'virt', n: 'docker',                      l: 'Docker',               d: '容器引擎' },
  { c: 'virt', n: 'luci-app-dockerman',          l: 'Docker 管理面板',       d: '容器管理界面' },
  { c: 'virt', n: 'open-vm-tools',               l: 'open-vm-tools',        d: 'VMware 工具' },
  { c: 'virt', n: 'qemu-ga',                     l: 'qemu-guest-agent',     d: 'QEMU 工具' },

  /* ---------------- 界面主题 ---------------- */
  { c: 'theme', n: 'luci-theme-argon',           l: 'Argon 主题',            d: '经典蓝色主题' },
  { c: 'theme', n: 'luci-theme-material',        l: 'Material 主题',         d: 'Material Design' },
  { c: 'theme', n: 'luci-theme-material3',       l: 'Material3 主题',        d: 'MD3 风格' },
  { c: 'theme', n: 'luci-theme-kucat',           l: 'KuCat 主题',            d: '轻量主题' },
  { c: 'theme', n: 'luci-theme-shadcn',          l: 'Shadcn 主题',           d: '现代风格' },
  { c: 'theme', n: 'luci-theme-alpha',           l: 'Alpha 主题',            d: '简约风格' },
  { c: 'theme', n: 'luci-theme-aurora',          l: 'Aurora 主题',           d: '极光风格' },
  { c: 'theme', n: 'luci-theme-spectra',         l: 'Spectra 主题',          d: '多彩风格' },
  { c: 'theme', n: 'luci-theme-routerich',       l: 'Routerich 主题',        d: '现代风格' },
  { c: 'theme', n: 'luci-theme-footstrap',       l: 'Footstrap 主题',        d: 'Bootstrap 风格' },
  { c: 'theme', n: 'luci-theme-teleofis',        l: 'Teleofis 主题',         d: '电信风格' },
  { c: 'theme', n: 'luci-theme-lightblue',       l: 'Lightblue 主题',        d: '浅蓝风格' },
  { c: 'theme', n: 'luci-theme-openwrt-2020',    l: 'OpenWrt 2020 主题',     d: '官方新版' },
  { c: 'theme', n: 'luci-theme-openwrt',         l: 'OpenWrt 主题',          d: '官方经典' },
];

/* 分类元信息（界面上的分组标题与顺序） */
window.KWRT_PRESET_CATS = [
  { k: 'proxy', l: '网络代理' },
  { k: 'net',   l: '穿透与组网' },
  { k: 'dl',    l: '下载与云盘' },
  { k: 'share', l: '文件共享' },
  { k: 'disk',  l: '磁盘存储' },
  { k: 'mgr',   l: '网络管理' },
  { k: 'ctrl',  l: '上网管控' },
  { k: 'media', l: '媒体娱乐' },
  { k: 'virt',  l: '虚拟化' },
  { k: 'theme', l: '界面主题' },
];

/* 一键代理套件：勾选后自动带上该套件所需的全部包 */
window.KWRT_SUITES = [
  { k: 'openclash', l: 'OpenClash',  pkgs: ['luci-app-openclash'] },
  { k: 'passwall',  l: 'PassWall',   pkgs: ['luci-app-passwall'] },
  { k: 'passwall2', l: 'PassWall2',  pkgs: ['luci-app-passwall2'] },
  { k: 'ssrplus',   l: 'SSR-Plus',   pkgs: ['luci-app-ssr-plus'] },
  { k: 'homeproxy', l: 'HomeProxy',  pkgs: ['luci-app-homeproxy'] },
  { k: 'nikki',     l: 'Nikki',      pkgs: ['luci-app-nikki'] },
  { k: 'momo',      l: 'Momo',       pkgs: ['luci-app-momo'] },
  { k: 'daed',      l: 'Daed',       pkgs: ['luci-app-daed'] },
  { k: 'v2raya',    l: 'v2rayA',     pkgs: ['luci-app-v2raya'] },
  { k: 'hijpass',   l: 'HiJpass',    pkgs: ['luci-app-hijpass'] },
  { k: 'nekobox',   l: 'NeKoBox',    pkgs: ['luci-app-neko-box'] },
  { k: 'xray',      l: 'luci-xray',  pkgs: ['luci-app-xray'] },
];
