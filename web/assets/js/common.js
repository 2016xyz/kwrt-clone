/* ==========================================================================
   共享基础库 —— jQuery 工具 + 主题 + API 封装 + 通用组件
   被首页 / 登录页 / 管理控制台共用
   ========================================================================== */
(function (global) {
  'use strict';

  /* ---------------- 主题（跟随系统 + 手动切换 + 持久化） ---------------- */
  const THEME_KEY = 'kwrt.theme';

  function systemTheme() {
    return (global.matchMedia && global.matchMedia('(prefers-color-scheme: dark)').matches)
      ? 'dark' : 'light';
  }
  function getTheme() {
    try { return localStorage.getItem(THEME_KEY) || 'auto'; } catch (e) { return 'auto'; }
  }
  function applyTheme(t) {
    const eff = t === 'auto' ? systemTheme() : t;
    document.documentElement.setAttribute('data-theme', eff);
    document.documentElement.setAttribute('data-theme-mode', t);
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute('content', eff === 'dark' ? '#080e1a' : '#ffffff');
    return eff;
  }
  function setTheme(t) {
    try { localStorage.setItem(THEME_KEY, t); } catch (e) { /* 隐私模式忽略 */ }
    return applyTheme(t);
  }
  function cycleTheme() {
    const order = ['auto', 'light', 'dark'];
    const next = order[(order.indexOf(getTheme()) + 1) % order.length];
    setTheme(next);
    return next;
  }
  // 立即生效，避免白闪
  applyTheme(getTheme());
  if (global.matchMedia) {
    global.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function () {
      if (getTheme() === 'auto') applyTheme('auto');
    });
  }
  const THEME_ICON = { auto: '◐', light: '☀', dark: '☾' };
  const THEME_TEXT = { auto: '跟随系统', light: '浅色', dark: '深色' };

  /* ---------------- 格式化 ---------------- */
  function bytes(n) {
    if (n === null || n === undefined || n < 0) return '-';
    const u = ['B', 'KB', 'MB', 'GB', 'TB'];
    let i = 0, v = Number(n);
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return (i === 0 ? v : v.toFixed(v >= 100 ? 0 : 1)) + ' ' + u[i];
  }
  function datetime(ts) {
    if (!ts) return '-';
    const d = new Date(Number(ts) * 1000);
    if (isNaN(d.getTime())) return '-';
    const p = function (x) { return String(x).padStart(2, '0'); };
    return d.getFullYear() + '/' + p(d.getMonth() + 1) + '/' + p(d.getDate()) +
           ' ' + p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
  }
  function timeAgo(ts) {
    if (!ts) return '-';
    const s = Math.max(0, Math.floor(Date.now() / 1000 - Number(ts)));
    if (s < 60) return s + ' 秒前';
    if (s < 3600) return Math.floor(s / 60) + ' 分钟前';
    if (s < 86400) return Math.floor(s / 3600) + ' 小时前';
    if (s < 2592000) return Math.floor(s / 86400) + ' 天前';
    return datetime(ts);
  }
  function duration(sec) {
    if (sec === null || sec === undefined) return '-';
    const s = Math.round(Number(sec));
    if (s < 60) return s + ' 秒';
    const m = Math.floor(s / 60), r = s % 60;
    if (m < 60) return m + ' 分 ' + r + ' 秒';
    return Math.floor(m / 60) + ' 时 ' + (m % 60) + ' 分';
  }
  function esc(s) {
    return String(s === null || s === undefined ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  /* ---------------- Toast 通知 ---------------- */
  /* 内联 SVG 图标集 —— 全局只定义一次。
     不用字符/emoji 图标：不同字体栈下会渲染成豆腐块或彩色 emoji。 */
  const SVG_SUN = '<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"><circle cx="12" cy="12" r="4.2"/><path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.2 5.2l1.4 1.4M17.4 17.4l1.4 1.4M18.8 5.2l-1.4 1.4M6.6 17.4l-1.4 1.4"/></svg>';
  const SVG_MOON = '<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M20.5 14.2A8.6 8.6 0 1 1 9.8 3.5a6.8 6.8 0 0 0 10.7 10.7z"/></svg>';
  const SVG_AUTO = '<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"><circle cx="12" cy="12" r="8.4"/><path d="M12 3.6v16.8"/></svg>';
  const SVG_CHECK = '<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9.2"/><path d="M8.3 12.3l2.5 2.5 4.9-5.2"/></svg>';
  const SVG_WARN = '<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3.2l9 15.6H3z"/><path d="M12 9.4v4.2M12 16.4h.01"/></svg>';
  const SVG_X = '<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9.2"/><path d="M9 9l6 6M15 9l-6 6"/></svg>';
  const SVG_INFO = '<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><circle cx="12" cy="12" r="9.2"/><path d="M12 11v5.2M12 7.8h.01"/></svg>';
  const SVG_BOX = '<svg viewBox="0 0 24 24" width="42" height="42" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M21 8l-9-5-9 5v8l9 5 9-5z"/><path d="M3 8l9 5 9-5M12 13v8"/></svg>';
  const TOAST_ICON = { success: SVG_CHECK, error: SVG_X, warning: SVG_WARN, info: SVG_INFO };

  function toastWrap() {
    let w = document.querySelector('.toast-wrap');
    if (!w) { w = document.createElement('div'); w.className = 'toast-wrap'; document.body.appendChild(w); }
    return w;
  }
  function toast(msg, type, ms) {
    type = type || 'info';
    const el = document.createElement('div');
    el.className = 'toast toast-' + type;
    el.innerHTML = '<span class="ic" aria-hidden="true">' + (TOAST_ICON[type] || SVG_INFO) +
                   '</span><span style="flex:1">' + esc(msg) + '</span>';
    el.setAttribute('role', type === 'error' ? 'alert' : 'status');
    toastWrap().appendChild(el);
    setTimeout(function () {
      el.classList.add('out');
      setTimeout(function () { el.remove(); }, 240);
    }, ms || (type === 'error' ? 5200 : 3200));
    return el;
  }

  /* ---------------- API 封装 ---------------- */
  function api(path, opt) {
    opt = opt || {};
    const init = {
      method: opt.method || 'GET',
      credentials: 'same-origin',
      headers: Object.assign({ 'Accept': 'application/json' }, opt.headers || {})
    };
    if (opt.body instanceof FormData) {
      init.body = opt.body;
    } else if (opt.body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(opt.body);
    }
    return fetch(path, init).then(function (r) {
      const ct = r.headers.get('content-type') || '';
      const parse = ct.indexOf('json') >= 0 ? r.json() : r.text().then(function (t) { return { detail: t }; });
      return parse.then(function (data) {
        if (!r.ok) {
          const err = new Error((data && (data.detail || data.message)) || ('HTTP ' + r.status));
          err.status = r.status; err.data = data;
          throw err;
        }
        return data;
      });
    });
  }
  function form(obj) {
    const fd = new FormData();
    Object.keys(obj || {}).forEach(function (k) {
      const v = obj[k];
      if (v !== undefined && v !== null) fd.append(k, v);
    });
    return fd;
  }
  const postForm = function (p, o) { return api(p, { method: 'POST', body: form(o) }); };
  const postJson = function (p, o) { return api(p, { method: 'POST', body: o || {} }); };

  /* ---------------- 一次性校验头（与后端同构：XOR 0x50 → base64） ---------------- */
  function verifHeader() {
    const uuid = (global.crypto && crypto.randomUUID)
      ? crypto.randomUUID()
      : 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function (c) {
          const r = Math.random() * 16 | 0;
          const v = c === 'x' ? r : (r & 0x3 | 0x8);
          return v.toString(16);
        });
    const s = JSON.stringify({ uuid: uuid });
    let raw = '';
    for (let i = 0; i < s.length; i++) raw += String.fromCharCode(s.charCodeAt(i) ^ 80);
    return btoa(raw);
  }

  /* ---------------- 剪贴板 ---------------- */
  function copy(text) {
    const done = function () { toast('已复制到剪贴板', 'success'); };
    if (navigator.clipboard && global.isSecureContext) {
      navigator.clipboard.writeText(text).then(done).catch(function () { fallback(); });
      return;
    }
    fallback();
    function fallback() {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.cssText = 'position:fixed;opacity:0;pointer-events:none';
      document.body.appendChild(ta); ta.select();
      let ok = false;
      try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
      ta.remove();
      toast(ok ? '已复制到剪贴板' : '复制失败，请手动选择', ok ? 'success' : 'error');
    }
  }

  /* ---------------- 防抖 / 节流 ---------------- */
  function debounce(fn, wait) {
    let t;
    return function () {
      const a = arguments, self = this;
      clearTimeout(t);
      t = setTimeout(function () { fn.apply(self, a); }, wait || 240);
    };
  }
  function throttle(fn, wait) {
    let last = 0, timer = null, lastArgs = null;
    return function () {
      const now = Date.now(), self = this;
      lastArgs = arguments;
      if (now - last >= (wait || 300)) { last = now; fn.apply(self, lastArgs); }
      else if (!timer) {
        timer = setTimeout(function () {
          timer = null; last = Date.now(); fn.apply(self, lastArgs);
        }, (wait || 300) - (now - last));
      }
    };
  }

  /* ---------------- 设备判定（用于构建/通知等场景） ---------------- */
  const ua = navigator.userAgent;
  const device = {
    isMobile: /Android|iPhone|iPod|Windows Phone|BlackBerry|mobile/i.test(ua),
    isTablet: /iPad|Android(?!.*Mobile)|Tablet|PlayBook|Silk/i.test(ua),
    isTouch: ('ontouchstart' in global) || navigator.maxTouchPoints > 0
  };
  device.type = device.isTablet ? 'tablet' : (device.isMobile ? 'mobile' : 'desktop');

  /* ---------------- 状态标签映射 ---------------- */
  const STATUS_MAP = {
    queued:    { text: '排队中', cls: 'badge-warning', dot: true },
    started:   { text: '构建中', cls: 'badge-info',    dot: true },
    running:   { text: '构建中', cls: 'badge-info',    dot: true },
    done:      { text: '已完成', cls: 'badge-success', dot: false },
    failed:    { text: '失败',   cls: 'badge-danger',  dot: false },
    cancelled: { text: '已取消', cls: '',              dot: false },
    expired:   { text: '已过期', cls: '',              dot: false }
  };
  function statusMeta(s) { return STATUS_MAP[s] || { text: s || '-', cls: '', dot: false }; }

  /* ---------------- 暴露 ---------------- */
  global.K = {
    // 主题
    getTheme: getTheme, setTheme: setTheme, cycleTheme: cycleTheme, applyTheme: applyTheme,
    THEME_ICON: THEME_ICON, THEME_TEXT: THEME_TEXT,
    // 格式化
    bytes: bytes, datetime: datetime, timeAgo: timeAgo, duration: duration, esc: esc,
    // 通知
    toast: toast,
    // 网络
    api: api, form: form, postForm: postForm, postJson: postJson, verifHeader: verifHeader,
    // 工具
    copy: copy, debounce: debounce, throttle: throttle, device: device,
    statusMeta: statusMeta, STATUS_MAP: STATUS_MAP,
    // 图标（SVG 字符串与其定义同处一个作用域，直接暴露给后续脚本使用）
    SVG: { sun: SVG_SUN, moon: SVG_MOON, auto: SVG_AUTO, check: SVG_CHECK,
           warn: SVG_WARN, x: SVG_X, info: SVG_INFO, box: SVG_BOX },
    TOAST_ICON: TOAST_ICON
  };
})(window);

/* ===================== Vue 3 全局组件 / 混入 ===================== */
(function (global) {
  'use strict';
  if (!global.Vue) return;

  /* 本文件里的组件注册必须走应用实例。
   *
   * 注意：Vue 3 移除了 Vue 2 的 Vue.component() 全局注册 API，
   * 只剩 createApp() 返回实例上的 app.component()。原先直接调用
   * global.Vue.component(...) 会抛 "Vue.component is not a function"，
   * 导致 theme-toggle 等组件全部注册失败（页面上主题切换按钮点不动）。
   *
   * 各页面都是 createApp({...}).mount('#app') 的写法，因此这里把
   * createApp 包一层：任何新建的应用都自动带上这套组件。
   * 效果等价于 Vue 2 的全局注册，且不必改每个页面的脚本。
   */
  const COMPONENTS = {};

  /* 主题切换按钮 */
  COMPONENTS['theme-toggle'] = {
    template:
      '<button class="btn btn-ghost btn-icon btn-sm" :title="title" :aria-label="title"' +
      ' @click="onClick" v-html="icon"></button>',
    data: function () { return { mode: global.K.getTheme() }; },
    computed: {
      icon: function () {
        const S = global.K.SVG || {};
        return this.mode === 'light' ? S.sun : this.mode === 'dark' ? S.moon : S.auto;
      },
      title: function () { return '主题：' + (global.K.THEME_TEXT[this.mode] || '跟随系统'); }
    },
    methods: {
      onClick: function () {
        this.mode = global.K.cycleTheme();
        global.K.toast('主题已切换：' + global.K.THEME_TEXT[this.mode], 'info', 1600);
      }
    }
  };

  /* 状态徽章 */
  COMPONENTS['status-badge'] = {
    props: { status: String, label: String },
    computed: {
      meta: function () { return global.K.statusMeta(this.status); }
    },
    template:
      '<span class="badge" :class="[meta.cls, meta.dot ? \'badge-dot badge-pulse\' : \'\']">' +
      '{{ label || meta.text }}</span>'
  };

  /* 复制按钮 */
  COMPONENTS['copy-btn'] = {
    props: { text: String, label: { type: String, default: '复制' } },
    template:
      '<button class="btn btn-ghost btn-xs" :title="\'复制: \' + text"' +
      ' @click="$root && $root.copy ? $root.copy(text) : 0">{{ label }}</button>'
  };

  /* 空态 */
  COMPONENTS['empty-state'] = {
    props: { icon: { type: String, default: '' }, title: { type: String, default: '暂无数据' },
             desc: { type: String, default: '' } },
    // 注意：SVG 不能内联进 v-html 表达式 —— 它自带的双引号会截断 HTML 属性，
    // 导致模板编译报错（Vue compiler error 54）。这里用计算属性返回。
    computed: {
      iconHtml: function () {
        return this.icon || (global.K.SVG && global.K.SVG.box) || '';
      }
    },
    template:
      '<div class="empty"><div class="ic" v-html="iconHtml"></div>' +
      '<h4>{{ title }}</h4><p v-if="desc" class="fs-sm">{{ desc }}</p><slot></slot></div>'
  };

  /* 骨架屏 */
  COMPONENTS['sk-lines'] = {
    props: { n: { type: Number, default: 4 } },
    template:
      '<div class="stack-sm"><div v-for="i in n" :key="i" class="skeleton"' +
      ' :style="{width: (i === n ? 62 : 100) + \'%\'}"></div></div>'
  };

  // 供页面脚本手动注册（一般用不到，自动包装已覆盖）
  global.K.vueComponents = COMPONENTS;

  if (typeof global.Vue.createApp === 'function') {
    const _createApp = global.Vue.createApp;
    global.Vue.createApp = function () {
      const app = _createApp.apply(this, arguments);
      Object.keys(COMPONENTS).forEach(function (n) {
        try { app.component(n, COMPONENTS[n]); } catch (e) { }
      });
      return app;
    };
  }

  // SVG 与 TOAST_ICON 已由文件开头的 IIFE 一并挂到 K 上，这里不再重复定义。
})(window);
