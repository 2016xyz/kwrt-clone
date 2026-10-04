/* ==========================================================================
   首页逻辑：设备检索 / 固件定制 / 真实构建 / 支付赞助
   -------------------------------------------------------------------------- */
(function () {
  'use strict';
  if (!window.Vue) return;
  const { createApp, ref, reactive, computed, watch, nextTick, onMounted, onUnmounted } = Vue;

  const LS_KEY = 'xsp_form_v1';

  const TIMEZONES = [
    { v: 'Asia/Shanghai', l: '中国标准时间 (UTC+8)' },
    { v: 'Asia/Hong_Kong', l: '香港 (UTC+8)' },
    { v: 'Asia/Taipei', l: '台北 (UTC+8)' },
    { v: 'Asia/Tokyo', l: '东京 (UTC+9)' },
    { v: 'Asia/Seoul', l: '首尔 (UTC+9)' },
    { v: 'Asia/Singapore', l: '新加坡 (UTC+8)' },
    { v: 'UTC', l: 'UTC' },
    { v: 'Europe/London', l: '伦敦 (UTC+0/+1)' },
    { v: 'Europe/Berlin', l: '柏林 (UTC+1/+2)' },
    { v: 'America/New_York', l: '纽约 (UTC-5/-4)' },
    { v: 'America/Los_Angeles', l: '洛杉矶 (UTC-8/-7)' },
  ];
  const THEMES = ['argon', 'design', 'opentomato', 'bootstrap', 'material', 'netgear',
                  'tomato', 'luci2', 'rosy', 'edge'];

  createApp({
    setup() {
      const site = ref({});
      const user = ref({ logged_in: false });
      const devices = ref([]);
      const booting = ref(true);
      const menu = ref(false);

      const query = ref('');
      const openList = ref(false);
      const hl = ref(0);
      const device = ref(null);
      const images = ref([]);
      const branch = ref('25.12');
      const availablePkgs = ref([]);
      const defaultPkgs = ref([]);
      const commonPkgs = ref([]);
      const selectedPkgs = ref([]);
      const pkgInput = ref('');
      const dragOver = ref(false);

      const job = ref(null);
      const building = ref(false);
      const openSponsor = ref(false);
      const tiers = ref([]);
      const pay = ref({ available: false });
      const chosenTier = ref('');
      const claiming = ref(false);
      const paying = ref(false);
      const payOrder = ref(null);

      /* ---------------- 我的订单与退款 ---------------- */
      const sponsorOrders = ref([]);
      const refundEnabled = ref(true);
      const refundTarget = ref(null);     // 正在申请退款的订单
      const refundReason = ref('');
      const refundDetail = ref('');
      const refundSending = ref(false);

      async function loadOrders() {
        if (!user.value.logged_in) { sponsorOrders.value = []; return; }
        try {
          const d = await K.api('/api/v1/sponsor/orders');
          sponsorOrders.value = d.orders || [];
          refundEnabled.value = d.refund_enabled !== false;
        } catch (e) { /* 未登录或接口异常时保持静默，不影响主页 */ }
      }
      // 订单状态中文映射（避免把后端的 created/waiting 直接露给用户）
      const ORDER_STATUS = {
        created: '待支付', waiting: '等待支付', paid: '已支付',
        paid_pending: '待确认', expired: '已超时', cancelled: '已取消',
        closed: '已关闭', failed: '下单失败', refunded: '已退款',
      };
      function orderStatusText(s) { return ORDER_STATUS[s] || s; }

      function openRefund(o) {
        refundTarget.value = o;
        refundReason.value = '';
        refundDetail.value = '';
      }
      function closeRefundApply() { refundTarget.value = null; }
      async function submitRefund() {
        const o = refundTarget.value;
        if (!o) return;
        if ((refundReason.value || '').trim().length < 5) {
          K.toast('退款理由至少 5 个字，请说明具体原因', 'warning');
          return;
        }
        refundSending.value = true;
        try {
          await K.postForm('/api/v1/sponsor/refund', {
            out_trade_no: o.out_trade_no,
            reason: refundReason.value.trim(),
            detail: (refundDetail.value || '').trim(),
          });
          K.toast('退款申请已提交，请等待站长审核', 'success');
          closeRefundApply();
          await loadOrders();
        } catch (e) {
          K.toast((e && e.message) || '提交失败，请稍后重试', 'error');
        } finally {
          refundSending.value = false;
        }
      }
      const year = new Date().getFullYear();

      let pollTimer = null, tickTimer = null;

      /* ---------------- 表单 ---------------- */
      const form = reactive({
        hostname: '', ip: '192.168.1.1', port: 80, password: '', timezone: 'Asia/Shanghai',
        filesystem: 'squashfs', boot: 'efi', rootfsSize: 1004, mode: 'router',
        gateway: '', pppoeUser: '', pppoePass: '', wifi: false, ssid: 'Kwrt', wifiKey: '',
        theme: '', webserver: 'uhttpd', uciDefaults: '', email: '', file: null,
        vmdk: false, rootfsTar: false, removeLinks: false, istoreos: false,
        ipv6: true, dhcp: true, eflasher: false,
        // 对齐上游站点的定制项
        kernelVer: '', wanlan: '', usbNet: '', usbWifi: '', httpsBackend: '',
        exposePorts: '', quickUrl: '',
      });

      /* ---------------- 预设插件包 ---------------- */
      // 目录以后台数据库为准（管理员可增删）；presets.js 仅作首屏兜底，
      // 加载完成后由 /api/v1/packages/catalog 覆盖。
      const PRESETS = ref(window.KWRT_PRESETS || []);
      const presetCats = ref(window.KWRT_PRESET_CATS || []);
      const suites = ref(window.KWRT_SUITES || []);
      const collapsed = reactive({});

      async function loadCatalog() {
        try {
          const d = await K.api('/api/v1/packages/catalog');
          if (d && Array.isArray(d.presets) && d.presets.length) {
            PRESETS.value = d.presets;
            presetCats.value = d.cats || [];
            suites.value = d.suites || [];
          }
        } catch (e) { /* 接口不可用时保留兜底清单，不阻断定制流程 */ }
      }

      function presetsByCat(k) { return PRESETS.value.filter(p => p.c === k); }
      // 预设项直接反映到 selectedPkgs（与手工输入的包共用同一个集合）
      // 注意：togglePkg 的**唯一实现**在下方「软件包」区（会调 saveForm 落盘）。
      // 此处原先又声明了一份不落盘的 togglePkg —— 函数提升后由后者覆盖前者，
      // 虽然当前行为正确，但两处定义极易在后续修改中分叉，故删除重复定义。
      // isPicked / hasPkg 是同一判定；基本实现是 hasPkg（下方「软件包」区），
      // 这里只保留一个别名，避免出现两份拷贝。
      function isPicked(n) { return hasPkg(n); }
      function suiteOn(s) {
        return s.pkgs.every(function (p) { return selectedPkgs.value.indexOf(p) >= 0; });
      }
      function toggleSuite(s) {
        const on = suiteOn(s);
        for (const p of s.pkgs) {
          const i = selectedPkgs.value.indexOf(p);
          if (on) { if (i >= 0) selectedPkgs.value.splice(i, 1); }
          else if (i < 0) { selectedPkgs.value.push(p); }
        }
      }
      // 由预设勾选的包（用于「自定义追加」区展示与移除）
      const extraPkgs = computed(function () {
        return selectedPkgs.value.filter(function (p) {
          return PRESETS.value.some(function (x) { return x.n === p; });
        });
      });
      function removeExtra(n) {
        // 注意：调用方传的是**包名**（与 togglePkg/removePkg 一致）。
        // 模板原写作 removeExtra(i)（传下标），而这里按名字查找 → indexOf 恒为 -1，
        // 「×」按钮点了没反应。已同步修正模板为 removeExtra(p)。
        const i = selectedPkgs.value.indexOf(n);
        if (i >= 0) {
          selectedPkgs.value.splice(i, 1);
          saveForm();                 // 与其他入口一致：改动落盘
        }
      }

      const toggles = [
        { k: 'vmdk', l: '打包 VMDK 虚拟磁盘', vip: true },
        { k: 'rootfsTar', l: '产出 rootfs.tar.gz', vip: true },
        { k: 'removeLinks', l: '移除默认软链接', vip: false },
        { k: 'istoreos', l: '集成 iStoreOS 应用商店', vip: false },
        { k: 'ipv6', l: '启用 IPv6', vip: false },
        { k: 'dhcp', l: '启用 DHCP 服务（关掉则做纯旁路由）', vip: false },
        { k: 'eflasher', l: '生成 EMMC 卡刷固件（仅 arm 设备）', vip: true },
      ];

      /* ---------------- 计算属性 ---------------- */
      const hotQueries = computed(function () {
        const s = new Set();
        const out = [];
        for (const d of devices.value) {
          const key = (d.target || '').split('/')[0];
          if (key && !s.has(key)) { s.add(key); out.push(d.target); }
          if (out.length >= 4) break;
        }
        return out.length ? out : ['x86/64', 'rockchip/armv8', 'bcm27xx/bcm2711'];
      });

      const filtered = computed(function () {
        const q = query.value.trim().toLowerCase();
        if (!q) return devices.value.slice(0, 60);
        const parts = q.split(/\s+/);
        const scored = [];
        for (const d of devices.value) {
          const hay = (d.rawTitle || '') + ' ' + d.target + ' ' + d.id;
          const low = hay.toLowerCase();
          let all = true, score = 0;
          for (const p of parts) {
            const i = low.indexOf(p);
            if (i < 0) { all = false; break; }
            score += i === 0 ? 3 : 1;
            if (d.id.toLowerCase() === p) score += 6;
          }
          if (all) scored.push({ d: d, s: score });
        }
        scored.sort(function (a, b) { return b.s - a.s; });
        return scored.map(function (x) { return x.d; });
      });

      const quotaText = computed(function () {
        return user.value.sponsor ? '不限' : (site.value.default_quota || 12);
      });
      const overQuota = computed(function () {
        if (user.value.sponsor) return false;
        const luci = selectedPkgs.value.filter(function (p) { return p.indexOf('luci-app') === 0; });
        return luci.length > (site.value.default_quota || 12);
      });
      const sym = computed(function () {
        return { CNY: '¥', USD: '$', EUR: '€', JPY: '¥', HKD: 'HK$' }[site.value.currency] || '¥';
      });
      const ip = computed(function () {
        return form.ip ? form.ip + (form.port && form.port !== 80 ? ':' + form.port : '') : '—';
      });
      const pkgSuggest = computed(function () {
        const q = pkgInput.value.trim().toLowerCase();
        const pool = availablePkgs.value;
        if (!q) return pool.slice(0, 40);
        return pool.filter(function (p) { return p.toLowerCase().indexOf(q) >= 0; }).slice(0, 40);
      });

      /* ---------------- 广告（后台可配置：弹出 / 滚动） ----------------
       * 数据来自 /api/v1/site 的 ads 字段（后台「广告位」设置）。
       *   marquee —— 顶部/底部跑马灯，正文支持 Markdown 与超链接；
       *   popup   —— 模态弹窗，按 delay 延迟弹出，frequency 决定是否重复展示。
       * 正文一律经 K.mdToHtml 转义 + 白名单渲染，杜绝 XSS。 */
      const ads = ref([]);
      const popupAd = ref(null);
      const marqueeDur = reactive({});
      let adQueue = [];
      let adTimer = null;

      const marqueeTop = computed(function () {
        return ads.value.filter(function (a) { return a.mode === 'marquee' && a.position === 'top'; });
      });
      const marqueeBottom = computed(function () {
        return ads.value.filter(function (a) { return a.mode === 'marquee' && a.position === 'bottom'; });
      });
      function adHtml(ad) { return K.mdToHtml(ad && ad.content); }
      function marqueeStyle(ad) {
        return { background: ad.bg || 'var(--primary)', color: ad.color || '#ffffff' };
      }
      // 跑马灯速度以 px/秒 表达：测量一份文案宽度后换算成动画时长。
      function measureMarquees() {
        nextTick(function () {
          const nodes = document.querySelectorAll('.ad-marquee-track');
          Array.prototype.forEach.call(nodes, function (el) {
            const id = el.getAttribute('data-ad-id');
            const one = el.firstElementChild
              ? el.firstElementChild.getBoundingClientRect().width
              : el.getBoundingClientRect().width / 2;
            const ad = ads.value.find(function (x) { return x.id === id; });
            const speed = (ad && ad.speed) || 60;
            marqueeDur[id] = Math.max(6, Math.round(Math.max(one, 1) / speed * 10) / 10);
          });
        });
      }
      function scheduleNextAd(ms) {
        if (adTimer) { clearTimeout(adTimer); adTimer = null; }
        if (!adQueue.length || popupAd.value) return;
        adTimer = setTimeout(function () {
          adTimer = null;
          if (popupAd.value || !adQueue.length) return;
          popupAd.value = adQueue.shift();
        }, Math.max(0, ms || 0));
      }
      function closeAd() {
        if (popupAd.value) K.adsMarkSeen(popupAd.value);
        popupAd.value = null;
        if (adQueue.length) scheduleNextAd(Math.max(900, (adQueue[0].delay || 0) * 1000));
      }
      function setupAds(list) {
        ads.value = K.adsFilter(list);
        adQueue = ads.value.filter(function (a) {
          return a.mode === 'popup' && !K.adsAlreadySeen(a);
        });
        measureMarquees();
        if (adQueue.length) scheduleNextAd((adQueue[0].delay || 0) * 1000);
      }
      function onAdResize() { measureMarquees(); }
      function onAdKeydown(e) {
        if (e.key === 'Escape' && popupAd.value && popupAd.value.closable) closeAd();
      }

      /* ---------------- 数据加载 ---------------- */
      async function loadSite() {
        site.value = await K.api('/api/v1/site');
        tiers.value = site.value.sponsor_tiers || [];
        pay.value = site.value.pay || { available: false };
        if (tiers.value.length) chosenTier.value = tiers.value[0].name;
        if (site.value.site_name) document.title = site.value.site_name;
        const l = site.value.logo_url;
        if (l) { const f = document.querySelector('link[rel="icon"]'); if (f) f.href = l; }
        setupAds(site.value.ads || []);
      }
      async function loadUser() {
        try { user.value = await K.api('/api/v1/user'); }
        catch (e) { user.value = { logged_in: false }; }
      }
      async function loadDevices() {
        try {
          const ov = await K.api('/json/v1/overview.json');
          const branches = ov.branches || {};
          const list = [];
          for (const name of Object.keys(branches)) {
            const meta = branches[name];
            if (!meta || meta.enabled === false) continue;
            const vers = Array.isArray(meta.versions) ? meta.versions
              : (typeof meta.versions === 'string' && meta.versions ? [meta.versions] : []);
            const v = vers[0] || name;
            try {
              const r = await K.api('/json/v1/' + meta.path.replace('{version}', v) + '/overview.json');
              for (const prof of (r.profiles || [])) {
                const t = (prof.titles && prof.titles[0]) || {};
                const title = ((t.vendor ? t.vendor + ' ' : '') + (t.model || prof.id)).trim();
                list.push({
                  id: prof.id, target: prof.target, title: title || prof.id,
                  branch: name, version: v,
                  rawTitle: (t.model || '') + ' ' + (t.vendor || '') + ' ' + prof.id + ' ' + prof.target,
                });
              }
            } catch (e) { /* 单分支失败不影响整体 */ }
          }
          devices.value = list;
        } catch (e) {
          K.toast('设备列表加载失败：' + e.message, 'error');
        }
      }
      async function loadCommon() {
        try {
          const d = await K.api('/data/common-packages.json');
          const arr = Array.isArray(d) ? d : (d.packages || []);
          commonPkgs.value = arr.map(function (x) {
            return typeof x === 'string' ? { name: x, label: x } : x;
          });
        } catch (e) { commonPkgs.value = []; }
      }

      /* ---------------- 设备选择 ---------------- */
      function move(step) {
        const n = Math.min(filtered.value.length, 60);
        if (!n) return;
        hl.value = (hl.value + step + n) % n;
      }
      function pickHighlighted() {
        const d = filtered.value[hl.value];
        if (d) pickDevice(d);
      }
      async function pickDevice(d) {
        openList.value = false;
        query.value = d.title || d.id;
        branch.value = d.branch || '25.12';
        device.value = { id: d.id, target: d.target, title: d.title, _version: d.version };
        images.value = [];
        try {
          const detail = await K.api('/json/v1/releases/' + d.version + '/targets/' + d.target + '/' + d.id + '.json');
          detail.title = d.title;
          device.value = detail;
          images.value = (detail.images || []).map(function (im) {
            return {
              name: im.name, size: im.size, sha256: im.sha256,
              kind: im.type || im.filesystem || '镜像',
              url: mirrorUrl(d.version, d.target, d.id, im.name),
            };
          });
          defaultPkgs.value = detail.default_packages || [];
          try {
            const idx = await K.api('/json/v1/releases/' + d.version + '/targets/' + d.target + '/index.json');
            availablePkgs.value = Object.keys(idx.packages || idx || {});
          } catch (e) { availablePkgs.value = []; }
        } catch (e) {
          K.toast('设备信息加载失败：' + e.message, 'error');
        }
        const p = new URLSearchParams(location.search);
        p.set('target', d.target); p.set('id', d.id);
        history.replaceState(null, '', '?' + p.toString());
      }
      function clearDevice() {
        device.value = null; images.value = []; job.value = null;
        query.value = ''; openList.value = false;
        history.replaceState(null, '', location.pathname);
      }
      function mirrorUrl(version, target, id, name) {
        return '/dl/mirror?' + new URLSearchParams(
          { version: version, target: target, id: id, name: name }).toString();
      }

      /* ---------------- 软件包 ---------------- */
      function hasPkg(p) { return selectedPkgs.value.indexOf(p) >= 0; }
      function togglePkg(p) {
        const i = selectedPkgs.value.indexOf(p);
        if (i >= 0) selectedPkgs.value.splice(i, 1);
        else selectedPkgs.value.push(p);
        saveForm();
      }
      function removePkg(p) { togglePkg(p); }
      function addPkg() {
        const raw = pkgInput.value.trim();
        if (!raw) return;
        const neg = raw.charAt(0) === '-';
        const name = neg ? raw.slice(1) : raw;
        if (!name) return;
        if (neg) {
          selectedPkgs.value = selectedPkgs.value.filter(function (x) { return x !== name; });
          K.toast('已移除 ' + name, 'info', 1500);
        } else if (selectedPkgs.value.indexOf(name) < 0) {
          selectedPkgs.value.push(name);
        }
        pkgInput.value = '';
        saveForm();
      }

      /* ---------------- 表单持久化 ---------------- */
      function saveForm() {
        try {
          const o = {};
          for (const k of Object.keys(form)) if (k !== 'file' && k !== 'password' && k !== 'pppoePass') o[k] = form[k];
          o._pkgs = selectedPkgs.value;
          localStorage.setItem(LS_KEY, JSON.stringify(o));
        } catch (e) { }
      }
      function restoreForm() {
        try {
          const s = JSON.parse(localStorage.getItem(LS_KEY) || '{}');
          for (const k of Object.keys(s)) if (k in form && k !== 'file') form[k] = s[k];
          if (Array.isArray(s._pkgs)) selectedPkgs.value = s._pkgs;
        } catch (e) { }
      }
      watch(form, saveForm, { deep: true });
      watch(selectedPkgs, saveForm, { deep: true });

      /* ---------------- 文件上传 ---------------- */
      function onFilePick(e) {
        const f = e.target.files && e.target.files[0];
        if (f) form.file = f;
      }
      function onDrop(e) {
        dragOver.value = false;
        const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
        if (f) form.file = f;
      }

      /* ---------------- 构建 ---------------- */
      async function uploadFile(file, hash) {
        const fd = new FormData();
        fd.append('file', file);
        if (hash) fd.append('request_hash', hash);
        const r = await fetch('/api/v1/upload', { method: 'POST', body: fd });
        if (!r.ok) {
          let d = {};
          try { d = await r.json(); } catch (e) { }
          throw new Error(d.detail || ('上传失败 HTTP ' + r.status));
        }
        return r.json();
      }

      /* ---------------- uci-defaults 合成 ----------------
         服务端只接受一个 defaults 脚本，主机名/IP/PPPoE/WIFI 等定制项
         都在这里合成为 /etc/uci-defaults 脚本，随固件首启执行一次。
         注意：只对赞助用户开放的项（主机名/描述）在此处按赞助态过滤，
         与界面上的禁用态保持一致，避免绕过。 */
      function buildDefaultsScript() {
        const sp = user.value.sponsor;
        const L = ['#!/bin/sh', '# 由夏诗意定制站生成，首次启动执行一次后自动删除'];
        if (sp && form.hostname) L.push('uci -q set system.@system[0].hostname="' + form.hostname + '"');
        if (form.timezone) L.push('uci -q set system.@system[0].timezone="' + form.timezone + '"');
        if (L.length > 2 && form.timezone) L.push('uci -q set system.@system[0].zonename="' + form.timezone + '"');
        if (form.ip) L.push('uci -q set network.lan.ipaddr="' + form.ip + '"');
        if (form.mode === 'bypass') {
          if (form.gateway) L.push('uci -q set network.lan.gateway="' + form.gateway + '"');
          L.push('uci -q set dhcp.lan.ignore="1"');
        }
        if (form.port && form.port !== 80) {
          L.push('uci -q set uhttpd.main.listen_http="0.0.0.0:' + form.port + '"');
        }
        if (form.pppoeUser) {
          L.push('uci -q set network.wan.proto="pppoe"');
          L.push('uci -q set network.wan.username="' + form.pppoeUser + '"');
          L.push('uci -q set network.wan.password="' + form.pppoePass + '"');
        }
        if (form.wifi) {
          L.push('uci -q set wireless.radio0.disabled="0"');
          L.push('uci -q set wireless.default_radio0.ssid="' + (form.ssid || 'Kwrt') + '"');
          if (form.wifiKey) {
            L.push('uci -q set wireless.default_radio0.encryption="psk2"');
            L.push('uci -q set wireless.default_radio0.key="' + form.wifiKey + '"');
          }
        }
        if (form.theme) L.push('uci -q set luci.main.mediaurlbase="/luci-static/' + form.theme + '"');
        if (form.webserver && form.webserver !== 'uhttpd') {
          L.push('uci -q set uhttpd.main.redirect_https="1"');
        }
        if (form.password) {
          L.push('uci -q set system.@system[0].password="' + form.password + '"');
        }
        // IPv6 / DHCP 开关
        if (!form.ipv6) {
          L.push('uci -q set network.wan6.disabled="1"');
          L.push('uci -q set dhcp.lan.dhcpv6="disabled"');
        }
        if (!form.dhcp) {
          L.push('uci -q set dhcp.lan.ignore="1"');
          L.push('# DHCP 已关闭：本机将作为纯旁路由/AP，不再下发地址');
        }
        // 网口模式：调整 WAN/LAN 的物理口归属
        if (form.wanlan === 'eth0wan') {
          L.push('uci -q set network.wan.device="eth0"');
          L.push('uci -q set network.lan.device="eth1"');
        } else if (form.wanlan === 'swap') {
          L.push('uci -q set network.wan.device="eth1"');
          L.push('uci -q set network.lan.device="eth0"');
        }
        // 后台 HTTPS
        if (sp && form.httpsBackend === 'self') {
          L.push('uci -q set uhttpd.main.listen_https="0.0.0.0:443"');
          L.push('uci -q set uhttpd.main.redirect_https="1"');
          L.push('uci -q set uhttpd.main.cert="/etc/uhttpd.crt"');
          L.push('uci -q set uhttpd.main.key="/etc/uhttpd.key"');
        }
        // USB 网卡驱动：装上对应 kmod，否则插上也不认
        const USB_NET = {
          r8152: 'kmod-usb-net-rtl8152', ax88179: 'kmod-usb-net-asix-ax88179',
          cdc: 'kmod-usb-net-cdc-ether',
        };
        const USB_WIFI = {
          mt76: 'kmod-mt76-usb', rtl88x2bu: 'kmod-rtl88x2bu',
          rtl8812au: 'kmod-rtl8812au-ct', ath9k: 'kmod-ath9k-htc',
        };
        if (form.usbNet && USB_NET[form.usbNet]) {
          // 由 startBuild 并入 packages（此处仅生成 uci 侧说明）
          L.push('# 已预置 USB 有线网卡驱动: ' + USB_NET[form.usbNet]);
        }
        if (sp && form.usbWifi && USB_WIFI[form.usbWifi]) {
          L.push('# 已预置 USB 无线网卡驱动: ' + USB_WIFI[form.usbWifi]);
        }
        // 暴露至公网的端口：只取数字与空格，逐条放行
        if (form.exposePorts) {
          const ports = String(form.exposePorts).split(/\s+/)
            .map(function (x) { return parseInt(x, 10); })
            .filter(function (n) { return n > 0 && n < 65536; });
          for (const p of ports.slice(0, 24)) {
            L.push('uci -q add firewall.allow_public=rule');
            L.push('uci -q set firewall.allow_public[-1].name="expose-' + p + '"');
            L.push('uci -q set firewall.allow_public[-1].src="wan"');
            L.push('uci -q set firewall.allow_public[-1].proto="tcp"');
            L.push('uci -q set firewall.allow_public[-1].dest_port="' + p + '"');
            L.push('uci -q set firewall.allow_public[-1].target="ACCEPT"');
          }
        }
        if (form.quickUrl) {
          L.push('# 后台快捷访问：请在客户端 hosts 中将 ' + form.quickUrl + ' 指向本机 LAN 地址');
        }
        L.push('uci commit system');
        L.push('uci commit network');
        L.push('uci commit wireless');
        L.push('uci commit uhttpd');
        L.push('[ -x /etc/init.d/network ] && /etc/init.d/network restart >/dev/null 2>&1');
        // 用户自定义脚本追加在后面，允许覆盖上面的默认值
        if (form.uciDefaults && form.uciDefaults.trim()) {
          L.push('');
          L.push('# ---- 用户自定义 ----');
          L.push(form.uciDefaults.trim().replace(/^#!\/bin\/sh\s*\n?/, ''));
        }
        const custom = L.join('\n') + '\n';
        // 若用户完全没填任何定制项，则不传 defaults，走镜像默认行为
        if (!form.hostname && !form.ip && !form.pppoeUser && !form.wifi &&
            !form.uciDefaults && !form.password && !form.theme) return '';
        return custom;
      }

      async function startBuild() {
        if (!device.value || building.value) return;
        if (overQuota.value) { K.toast('插件数量超出配额，请赞助后重试', 'warning'); return; }
        building.value = true;
        job.value = null;
        try {
          // 自定义文件包必须在提交构建之前上传完毕：
          // 提交后队列几乎立刻取走任务，那时再挂文件必然赶不上。
          let filesPath = '';
          if (form.file) {
            K.toast('正在上传自定义文件包…', 'info');
            const up = await uploadFile(form.file, '');
            filesPath = (up && up.files_path) || '';
          }

          // USB 网卡驱动以 kmod 形式并入 packages，否则插上也不认
          const USB_NET_K = { r8152: 'kmod-usb-net-rtl8152',
                              ax88179: 'kmod-usb-net-asix-ax88179',
                              cdc: 'kmod-usb-net-cdc-ether' };
          const USB_WIFI_K = { mt76: 'kmod-mt76-usb', rtl88x2bu: 'kmod-rtl88x2bu',
                               rtl8812au: 'kmod-rtl8812au-ct', ath9k: 'kmod-ath9k-htc' };
          // 注意：这里必须用 user.value.sponsor，不能直接写 sp ——
          // sp 只是 buildDefaultsScript 内部的局部变量，在此作用域未定义，
          // 会导致 ReferenceError 让整个提交静默失败。
          const isSponsor = !!(user.value && user.value.sponsor);
          const pkgsOut = selectedPkgs.value.slice();
          if (form.usbNet && USB_NET_K[form.usbNet]) pkgsOut.push(USB_NET_K[form.usbNet]);
          if (isSponsor && form.usbWifi && USB_WIFI_K[form.usbWifi]) {
            pkgsOut.push(USB_WIFI_K[form.usbWifi]);
          }

          const payload = {
            target: device.value.target,
            profile: device.value.id,
            version: branch.value,
            packages: pkgsOut,
            diff_packages: false,
            filesystem: form.filesystem,
            rootfs_size_mb: form.rootfsSize,
            // 服务端只认 defaults 脚本，定制项在此合成
            defaults: buildDefaultsScript(),
            // 自定义文件包（赞助用户）：服务端会解压覆盖到固件 files/ 目录
            files_path: filesPath,
            // 以下为站点自有扩展字段（记录在 payload 里，便于审计与回溯）
            hostname: form.hostname, ipaddr: form.ip, port: form.port,
            timezone: form.timezone, mode: form.mode, boot: form.boot,
            vmdk: form.vmdk, rootfs_tar: form.rootfsTar,
            remove_links: form.removeLinks, istoreos: form.istoreos,
            ipv6: form.ipv6, dhcp: form.dhcp,
            eflasher: isSponsor ? form.eflasher : false,
            // 上游站点的定制项
            kernel_v: form.kernelVer, wanlan: form.wanlan,
            usb_net: form.usbNet, usb_wireless: isSponsor ? form.usbWifi : '',
            https: form.httpsBackend === 'self', expose_ports: form.exposePorts,
            quick_url: form.quickUrl,
            email: form.email || (user.value.email || ''),
          };
          const headers = {};
          // verifHeader() 返回的是一个经过混淆的字符串（后端 /api/v1/build 直接读该头）。
          // 旧写法按数组取 vh[0]/vh[1]，会把字符串当对象展开，
          // 结果是 payload 里混进 {"0":"3","1":"e",...} 这种字符下标键。
          const vh = K.verifHeader();
          if (vh) headers['Ng-One-Time-Verif-Value'] = vh;
          const r = await fetch('/api/v1/build', {
            method: 'POST', headers: Object.assign({ 'Content-Type': 'application/json' }, headers),
            body: JSON.stringify(payload),
          });
          const data = await r.json();
          if (!r.ok) throw new Error(data.detail || ('HTTP ' + r.status));
          const hash = data.request_hash;
          if (filesPath) K.toast('自定义文件包将随本次构建解压', 'success');
          job.value = { request_hash: hash, status: 'queued', progress: 0, message: '已提交，等待调度' };
          schedulePoll(1200);
        } catch (e) {
          K.toast('构建提交失败：' + e.message, 'error');
        } finally { building.value = false; }
      }

      function schedulePoll(delay) {
        if (pollTimer) clearTimeout(pollTimer);
        pollTimer = setTimeout(pollJob, delay || 2000);
      }
      async function pollJob() {
        if (!job.value) return;
        try {
          const r = await K.api('/api/v1/build/' + job.value.request_hash);
          const prev = job.value.status;
          job.value = Object.assign({}, job.value, r);
          const pct = { queued: 8, running: 55, done: 100, failed: 100, cancelled: 100 }[r.status] || 30;
          if (r.status === 'running' && job.value.progress < 90) job.value.progress = Math.max(55, r.progress || 55);
          else job.value.progress = pct;
          if (r.status === 'done') {
            K.toast('固件构建完成', 'success');
            if (job.value.download_links && job.value.download_links.length) {
              K.toast('已签发 ' + job.value.download_links.length + ' 个下载链接', 'info');
            }
            return;
          }
          if (r.status === 'failed') {
            // 取最有用的错误原因：detail > stderr 头200字 > 通用提示
            const reason = r.detail || (r.stderr && r.stderr.trim().slice(-300)) || '请查看下方错误详情';
            K.toast('构建失败：' + reason.slice(0, 120), 'error', 8000);
            return;
          }
          if (r.status === 'cancelled') return;
          schedulePoll(2000);
        } catch (e) {
          schedulePoll(4000);   // 单次失败不终止轮询
        }
      }
      async function cancelBuild() {
        if (!job.value) return;
        try {
          await K.postForm('/api/v1/build/' + job.value.request_hash + '/cancel', {});
          K.toast('已请求取消', 'info');
          schedulePoll(1500);
        } catch (e) { K.toast('取消失败：' + e.message, 'error'); }
      }

      /* ---------------- 赞助 / 支付 ---------------- */
      /* 自定义金额的即时校验与「预计发放天数」预览。
         折算单价来自 /api/v1/site 的 sponsor_per_day（服务端算出的**有效**单价：
         后台配了用配的，没配则取套餐里最划算的那个）。
         这里算出来的天数只用于提示 —— 真正的天数永远由服务端 _custom_days() 决定。 */
      const amountRaw = ref('');                                   // 输入框原始字符串
      const customAmount = computed(function () { return Number(amountRaw.value || 0); });
      const amtMin = computed(function () { return Number(site.value['sponsor.min_amount'] || 1); });
      const amtMax = computed(function () { return Number(site.value['sponsor.max_amount'] || 99999); });
      const perDay = computed(function () { return Number(site.value.sponsor_per_day || 1); });
      const amountDays = computed(function () {
        const a = customAmount.value;
        if (!(a > 0)) return 0;
        return Math.min(3650, Math.max(1, Math.floor(a / (perDay.value || 1))));
      });
      const amountErr = computed(function () {
        if (!String(amountRaw.value).trim()) return '';
        const a = customAmount.value;
        if (!(a > 0)) return '请输入大于 0 的金额';
        if (a < amtMin.value) return '金额不能低于 ' + amtMin.value;
        if (a > amtMax.value) return '金额不能高于 ' + amtMax.value;
        return '';
      });
      const canSubmit = computed(function () {
        if (String(amountRaw.value).trim()) return !amountErr.value;
        return !!chosenTier.value;
      });
      function pickTier(t) {
        chosenTier.value = t.name;
        amountRaw.value = '';
      }
      function clearAmount() { amountRaw.value = ''; }
      function onAmountInput() {
        // 一旦填了金额就和套餐互斥 —— 两个都给服务端会以套餐优先，用户会困惑
        if (customAmount.value > 0) chosenTier.value = '';
      }

      async function doClaim() {
        const o = payOrder.value;
        const body = o && o.tier ? { tier: o.tier }
          : (o ? { amount: String(o.amount) }
            : (customAmount.value > 0 ? { amount: String(customAmount.value) }
              : { tier: chosenTier.value }));
        if (!body.tier && !body.amount) return;
        claiming.value = true;
        try {
          const r = await K.postForm('/api/v1/sponsor/claim', body);
          K.toast(r.detail || '已提交', 'success');
          if (payOrder.value) payOrder.value.state = (r.status === 'ok' ? 'paid' : 'pending');
          await loadUser();
          if (r.status === 'ok') openSponsor.value = false;
        } catch (e) { K.toast(e.message || '提交失败', 'error'); }
        finally { claiming.value = false; }
      }
      function stopPayPoll() {
        if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
        if (tickTimer) { clearInterval(tickTimer); tickTimer = null; }
      }
      /* 统一下单：用户输入金额（或选套餐）点按钮 → 服务端返回「收款码图片地址」
           mode=alipay  有支付宝当面付，url 指向该订单的二维码（轮询到账）
           mode=manual  没有当面付，url 指向服务端**实时生成**的收款码（扫码后自行确认） */
      async function startOrder() {
        if (!canSubmit.value) { K.toast(amountErr.value || '请先选择套餐或输入金额', 'error'); return; }
        if (!user.value.logged_in) { location.href = '/login/?next=/'; return; }
        paying.value = true;
        try {
          const body = (customAmount.value > 0 && !chosenTier.value)
            ? { amount: String(customAmount.value) }
            : { tier: chosenTier.value };
          const r = await K.postForm('/api/v1/sponsor/order', body);
          const mode = r.mode || 'alipay';
          payOrder.value = {
            mode: mode,
            out_trade_no: r.out_trade_no || '',
            tier: r.tier || '',
            qr_img: r.url,
            amount: r.amount, days: r.days,
            state: mode === 'alipay' ? 'waiting' : 'unpaid',
            deadline: Date.now() + (r.expires_in || 900) * 1000,
            leftText: fmtLeft(r.expires_in || 900),
          };
          if (mode !== 'alipay') return;      // 手动收款码不轮询，付款后由用户点确认
          tickTimer = setInterval(function () {
            if (!payOrder.value) return;
            const left = Math.floor((payOrder.value.deadline - Date.now()) / 1000);
            if (left <= 0) { payOrder.value.state = 'expired'; stopPayPoll(); return; }
            payOrder.value.leftText = fmtLeft(left);
          }, 1000);
          schedulePayPoll(1500);
        } catch (e) { K.toast(e.message || '生成收款码失败', 'error'); }
        finally { paying.value = false; }
      }
      function fmtLeft(s) {
        const m = Math.floor(s / 60);
        return m + ':' + String(s % 60).padStart(2, '0');
      }
      function schedulePayPoll(d) {
        if (pollTimer) clearTimeout(pollTimer);
        pollTimer = setTimeout(async function () {
          if (!payOrder.value || payOrder.value.state !== 'waiting') return;
          try {
            const r = await K.api('/api/v1/sponsor/pay/' + encodeURIComponent(payOrder.value.out_trade_no));
            if (r.paid) {
              payOrder.value.state = r.order_status === 'paid_pending' ? 'pending' : 'paid';
              stopPayPoll(); await loadUser();
              K.toast(r.detail || '支付成功', 'success');
              return;
            }
            if (r.order_status === 'expired') { payOrder.value.state = 'expired'; stopPayPoll(); return; }
          } catch (e) { /* 继续轮询 */ }
          schedulePayPoll();
        }, d || (pay.value.poll_seconds || 3) * 1000);
      }
      async function closeSponsor() {
        if (payOrder.value && payOrder.value.state === 'waiting') {
          try {
            await K.postForm('/api/v1/sponsor/pay/' +
              encodeURIComponent(payOrder.value.out_trade_no) + '/cancel', {});
          } catch (e) { }
        }
        stopPayPoll();
        payOrder.value = null;
        openSponsor.value = false;
        await loadUser();
        await loadOrders();
      }

      async function doLogout() {
        await fetch('/api/v1/logout', { method: 'POST' });
        location.reload();
      }

      watch(openSponsor, function (v) { if (!v) { stopPayPoll(); payOrder.value = null; } });

      onMounted(async function () {
        window.addEventListener('resize', onAdResize);
        window.addEventListener('keydown', onAdKeydown);
        // 无论初始化是否出错，遮罩都必须消失 —— 否则整页被永久盖住
        try {
          restoreForm();
          await Promise.all([loadSite(), loadUser()]);
          await Promise.all([loadDevices(), loadCommon(), loadCatalog()]);
          await loadOrders();
          const p = new URLSearchParams(location.search);
          const t = p.get('target'), id = p.get('id');
          if (t && id) {
            const m = devices.value.find(function (d) { return d.target === t && d.id === id; });
            if (m) pickDevice(m);
          }
        } catch (e) {
          console.error('[xsp] 初始化失败', e);
          K.toast('页面初始化异常：' + ((e && e.message) || e), 'error', 8000);
        } finally {
          booting.value = false;
        }
      });
      onUnmounted(function () {
        if (pollTimer) clearTimeout(pollTimer);
        if (tickTimer) clearInterval(tickTimer);
        if (adTimer) clearTimeout(adTimer);
        window.removeEventListener('resize', onAdResize);
        window.removeEventListener('keydown', onAdKeydown);
      });

      return {
        site, user, devices, booting, menu, query, openList, hl, device, images, branch,
        availablePkgs, commonPkgs, selectedPkgs, pkgInput, dragOver, job, building,
        openSponsor, tiers, pay, chosenTier, claiming, paying, payOrder, year,
        // 自定义金额 → 实时生成收款码
        amountRaw, amtMin, amtMax, amountDays, amountErr, canSubmit,
        pickTier, clearAmount, onAmountInput,
        // 我的订单与退款
        sponsorOrders, refundEnabled, refundTarget, refundReason, refundDetail,
        refundSending, loadOrders, openRefund, closeRefundApply, submitRefund, orderStatusText,
        form, toggles, hotQueries, filtered, quotaText, overQuota, sym, ip, pkgSuggest,
        timezones: TIMEZONES, themes: THEMES,
        // 预设插件包
        presetCats, suites, collapsed, presetsByCat, isPicked, toggleSuite, suiteOn,
        extraPkgs, removeExtra,
        // 广告位（弹出 / 滚动）
        marqueeTop, marqueeBottom, marqueeDur, popupAd, adHtml, marqueeStyle, closeAd,
        // 配额展示
        quota: computed(function () { return site.value.default_quota || 12; }),
        move, pickHighlighted, pickDevice, clearDevice, hasPkg, togglePkg, removePkg, addPkg,
        onFilePick, onDrop, startBuild, cancelBuild, doClaim, startOrder, closeSponsor, doLogout,
        buildDefaultsScript,
        K,
      };
    }
  }).mount('#app');
})();
