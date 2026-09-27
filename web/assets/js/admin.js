/* ==========================================================================
   管理控制台逻辑
   ========================================================================== */
(function () {
  'use strict';
  if (!window.Vue) return;
  const { createApp, ref, reactive, computed, onMounted, watch } = Vue;

  const ICONS = {
    ov:      '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7.5" height="7.5" rx="1.6"/><rect x="13.5" y="3" width="7.5" height="7.5" rx="1.6"/><rect x="3" y="13.5" width="7.5" height="7.5" rx="1.6"/><rect x="13.5" y="13.5" width="7.5" height="7.5" rx="1.6"/></svg>',
    users:   '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="9" cy="8" r="3.4"/><path d="M2.8 20a6.4 6.4 0 0 1 12.4 0"/><path d="M16.5 5.2a3.4 3.4 0 0 1 0 6.6M18 20a6.4 6.4 0 0 0-2.2-4.8"/></svg>',
    builds:  '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 8l-9-5-9 5v8l9 5 9-5z"/><path d="M3 8l9 5 9-5M12 13v8"/></svg>',
    queue:   '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><rect x="3" y="4" width="18" height="4.5" rx="1.4"/><rect x="3" y="11" width="18" height="4.5" rx="1.4"/><path d="M3 19.5h18"/></svg>',
    site:    '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="12" r="3.2"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-2.9 1.2v.2a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-3-1.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1A1.7 1.7 0 0 0 3 15a2 2 0 0 1 0-4 1.7 1.7 0 0 0 1.2-3l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1A1.7 1.7 0 0 0 10 4.5V4a2 2 0 0 1 4 0v.1a1.7 1.7 0 0 0 3 1.2l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1A1.7 1.7 0 0 0 21 11a2 2 0 0 1 0 4z"/></svg>',
    mail:    '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="2.5" y="5" width="19" height="14" rx="2.4"/><path d="M3 7l9 6 9-6"/></svg>',
    verify:  '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6.5L9.4 17.5 4 12.2"/></svg>',
    tokens:  '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M10 13a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1.2 1.2"/><path d="M14 11a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l1.2-1.2"/></svg>',
    sponsor: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3.2l2.7 5.6 6.1.9-4.4 4.3 1 6.1L12 17.2l-5.4 2.9 1-6.1-4.4-4.3 6.1-.9z"/></svg>',
    pay:     '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="2.5" y="5.5" width="19" height="13" rx="2.4"/><path d="M2.5 10h19M6.5 14.5h4"/></svg>',
    prop:    '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v18M3 12h18"/><circle cx="12" cy="12" r="9"/></svg>',
    bans:    '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M5.6 5.6l12.8 12.8"/></svg>',
    logs:    '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2.5H7A2 2 0 0 0 5 4.5v15a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7.5z"/><path d="M14 2.5v5h5M9 13h6M9 17h4"/></svg>',
  };

  const TABS = [
    { k: 'ov',      label: '总览',     icon: ICONS.ov },
    { k: 'users',   label: '用户',     icon: ICONS.users },
    { k: 'builds',  label: '构建',     icon: ICONS.builds },
    { k: 'queue',   label: '队列存储', icon: ICONS.queue },
    { k: 'site',    label: '站点设置', icon: ICONS.site },
    { k: 'mail',    label: '邮件通知', icon: ICONS.mail },
    { k: 'verify',  label: '邮箱验证', icon: ICONS.verify },
    { k: 'tokens',  label: '下载链接', icon: ICONS.tokens },
    { k: 'sponsor', label: '赞助',     icon: ICONS.sponsor, badge: 'claims' },
    { k: 'pay',     label: '在线支付', icon: ICONS.pay },
    { k: 'refund',  label: '退款审核', icon: ICONS.pay, badge: 'refund' },
    { k: 'catalog', label: '插件目录', icon: ICONS.queue },
    { k: 'prop',    label: '插件提议', icon: ICONS.prop, badge: 'prop' },
    { k: 'bans',    label: '封禁',     icon: ICONS.bans },
    { k: 'logs',    label: '审计日志', icon: ICONS.logs },
  ];

  const PAY_STATUS = {
    created: '已创建', waiting: '等待支付', paid: '已支付', paid_pending: '待人工确认',
    expired: '已超时', cancelled: '已取消', closed: '已关闭', failed: '下单失败',
  };

  createApp({
    setup() {
      const authChecked = ref(false);
      const isAdmin = ref(false);
      const me = ref({});
      const site = ref({});
      const tab = ref('ov');
      const tabs = TABS;
      const badges = reactive({ claims: 0, prop: 0, refund: 0 });
      const loading = reactive({
        ov: false, users: false, usersave: false, mailtest: false,
        ghtest: false, paytest: false, refundact: false, catalogact: false,
      });
      const saving = ref(false);

      const ov = ref({});
      const backend = ref({});

      /* ---------------- 检查更新（GitHub） ----------------
         只读操作：查询更新源、显示结果。不落地任何文件、不重启服务。
         失败必须显示成失败 —— 把「查不了」显示成「已是最新」会让管理员
         错过安全更新，所以 status 单独留一个字段给模板判断。 */
      const upd = reactive({
        busy: false, checked: false, status: '', msg: '',
        current_display: '', latest_display: '', has_update: false,
        source: '', published_at: '', notes: '', html_url: '', cached: false,
      });

      async function checkUpdate(force) {
        if (upd.busy) return;
        upd.busy = true;
        if (force) upd.checked = false;   // 强制重查时先收起旧结果，避免看着像没反应
        await guard(async () => {
          const d = await K.api('/api/v1/admin/update/check'
                                + (force ? '?force=1' : ''));
          upd.checked = true;
          upd.status = d.status || 'ok';
          upd.msg = d.message || '';
          upd.current_display = d.current_display || '';
          upd.latest_display = d.latest_display || '';
          upd.has_update = !!d.has_update;
          upd.source = d.source || '';
          upd.published_at = d.published_at || '';
          upd.notes = d.notes || '';
          upd.html_url = d.html_url || '';
          upd.cached = !!d.cached;
          if (upd.status === 'error') toast(upd.msg, 'error');
          else if (upd.has_update) toast('发现新版本 ' + upd.latest_display, 'success');
          else toast(upd.msg, 'info');
        });
        upd.busy = false;
      }
      const mailReady = ref(false);

      const users = ref([]); const userQ = ref('');
      const userModal = ref(null);
      const sponsorModal = ref(null);

      const builds = ref([]);
      const art = ref({ items: [], total_bytes: 0, total_files: 0, orphan_bytes: 0, orphan_count: 0, uploads: {}, store_bytes: 0 });
      const tokens = ref([]); const tokenStats = ref(null);
      const claims = ref([]);
      const props = ref([]);
      const bans = ref([]);
      const logs = ref([]);
      const mailLog = ref([]); const mailTestTo = ref('');
      const verifyStats = ref({});

      const payInfo = ref({});
      const payOrders = ref([]); const payFilter = ref('');

      // 退款审核
      const refunds = ref([]); const refundFilter = ref('');
      const refundStats = ref({});
      const refundModal = ref(null);      // {req, action}
      const refundNote = ref('');
      const refundOffline = ref(false);

      // 插件目录（后台增删构建用插件/软件包）
      const catalog = ref({ cats: [], presets: [], suites: [] });
      const catalogCounts = ref({});
      const catForm = reactive({ name: '', label: '', cat: '', desc: '' });
      const catNewCat = reactive({ key: '', label: '' });
      const catNewSuite = reactive({ key: '', label: '', pkgs: '' });
      const catTab = ref('presets');
      const catQ = ref('');

      // 配置
      const schema = ref([]);
      const groups = ref([]);
      const cfg = reactive({});
      const cfgText = reactive({});   // json 类型的原始文本
      const orig = reactive({});
      const tiersEdit = ref([]);
      let origTiers = '';

      /* ---------------- 通用 ---------------- */
      function toast(m, t) { K.toast(m, t || 'info'); }

      async function guard(fn) {
        try { return await fn(); }
        catch (e) {
          const msg = (e && e.message) || '操作失败';
          toast(msg, 'error');
          if (e && e.status === 403) { isAdmin.value = false; }
          return null;
        }
      }

      /* ---------------- 总览 ---------------- */
      function fmtMB(mb) {
        if (mb == null) return '—';
        return mb >= 1024 ? (mb / 1024).toFixed(1) + ' GB' : mb + ' MB';
      }

      async function loadOverview() {
        loading.ov = true;
        await guard(async () => {
          const d = await K.api('/api/v1/admin/overview');
          // 后端返回的是嵌套结构；此处统一映射为扁平视图模型，
          // 避免模板里散落一堆 ov.users.total 之类的深层取值。
          const u = d.users || {}, b = d.builds || {}, q = d.queue || {};
          const disk = d.disk || {}, sp = d.sponsor || {}, dl = d.download || {};
          const conf = d.config || {};
          ov.value = {
            users: u.total || 0, sponsors: sp.sponsors || u.sponsor || 0,
            admins: u.admin || 0, disabled: u.disabled || 0,
            builds: b.total || 0, builds_done: b.done || 0, builds_failed: b.failed || 0,
            queue_running: q.running || 0, queue_queued: q.queued || 0,
            concurrency: q.concurrency || 2,
            proposals_pending: d.proposals_pending || 0, bans: d.bans || 0,
            disk: fmtMB(disk.store_mb),
            disk_free: fmtMB(disk.free_mb),
            store_mb: fmtMB(disk.store_mb),
            active_sponsors: sp.active || 0, sponsor_total_amount: sp.total_amount || 0,
            currency: sp.currency || 'CNY',
            dl_total: dl.total || 0, dl_active: dl.active || 0,
            dl_expired: dl.expired || 0, dl_revoked: dl.revoked || 0, dl_hits: dl.hits || 0,
            conf: conf, config: conf,
            backend: d.backend || {},
            // ★ 必须把 update 段带进视图模型。loadOverview 是**显式重建**一个新对象，
            //   接口多返回一个字段而这里忘了映射，页面就拿不到 —— 按钮会渲染出来
            //   但永远 disabled，看着像「功能没做」。本项目反复栽在这类
            //   「接口有值、页面没接线」上（见 verify_integrity 的 L3 检查）。
            update: d.update || {},
            recent_builds: d.recent_builds || [],
            checks: d.checks || {},
          };
          backend.value = d.backend || {};
          badges.claims = ov.value.checks.claims_pending != null
            ? ov.value.checks.claims_pending : badges.claims;
          badges.prop = ov.value.proposals_pending || 0;
          mailReady.value = !!(conf['mail.enabled'] && conf['mail.host'] && conf['mail.from_addr']);
        });
        loading.ov = false;
      }

      /* ---------------- 用户 ---------------- */
      async function loadUsers() {
        loading.users = true;
        await guard(async () => {
          const d = await K.api('/api/v1/admin/users');
          users.value = d.users || [];
        });
        loading.users = false;
      }
      const filteredUsers = computed(function () {
        const q = userQ.value.toLowerCase();
        if (!q) return users.value;
        return users.value.filter(function (u) {
          return (u.username || '').toLowerCase().indexOf(q) >= 0
            || (u.email || '').toLowerCase().indexOf(q) >= 0;
        });
      });
      function openCreateUser() {
        userModal.value = { mode: 'create', username: '', email: '', password: '',
                            role: 'user', quota: 12, disabled: false };
      }
      function editUser(u) {
        userModal.value = { mode: 'edit', username: u.username, email: u.email || '',
                            password: '', role: u.role || 'user',
                            quota: u.quota == null ? 12 : u.quota, disabled: !!u.disabled };
      }
      async function saveUser() {
        const m = userModal.value;
        if (!m || !m.username) { toast('请填写用户名', 'warning'); return; }
        loading.usersave = true;
        await guard(async () => {
          if (m.mode === 'create') {
            if (!m.password || m.password.length < 6) throw new Error('密码至少 6 位');
            await K.postForm('/api/v1/admin/user/create',
              { username: m.username, password: m.password, email: m.email,
                role: m.role, sponsor: 0 });
          } else {
            await K.postForm('/api/v1/admin/user',
              { username: m.username, action: 'update', email: m.email,
                role: m.role, quota: m.quota, disabled: m.disabled ? 1 : 0 });
            if (m.password) {
              await K.postForm('/api/v1/admin/user',
                { username: m.username, action: 'password', password: m.password });
            }
          }
          toast('已保存', 'success');
          userModal.value = null;
          await loadUsers();
        });
        loading.usersave = false;
      }
      async function delUser(u) {
        if (!confirm('确定删除用户 ' + u.username + '？该操作不可撤销。')) return;
        await guard(async () => {
          await K.postForm('/api/v1/admin/user', { username: u.username, action: 'delete' });
          toast('已删除 ' + u.username, 'success');
          await loadUsers();
        });
      }
      function sponsorUser(u) {
        const t = tiersEdit.value[0];
        sponsorModal.value = {
          username: u.username, sponsor: !!u.sponsor, until: u.sponsor_until,
          tier: t ? t.name : '__custom', days: t ? t.days : 30,
        };
      }
      async function sponsorAct(action) {
        const m = sponsorModal.value;
        if (!m) return;
        await guard(async () => {
          if (action === 'revoke') {
            if (!confirm('确定撤销 ' + m.username + ' 的赞助权益？')) return;
            await K.postForm('/api/v1/admin/user/sponsor',
              { username: m.username, action: 'revoke' });
            toast('已撤销赞助', 'success');
          } else {
            let days = m.days;
            if (m.tier !== '__custom') {
              const t = tiersEdit.value.find(function (x) { return x.name === m.tier; });
              if (t) days = t.days;
            }
            await K.postForm('/api/v1/admin/user/sponsor',
              { username: m.username, action: 'grant', days: days });
            toast('已授予 ' + days + ' 天赞助', 'success');
          }
          sponsorModal.value = null;
          await loadUsers();
        });
      }
      async function adminReverify(u) {
        await guard(async () => {
          const r = await K.postForm('/api/v1/admin/user/reverify', { username: u.username });
          toast(r.detail || '已重发', 'success');
        });
      }
      async function adminSetVerified(u) {
        if (!confirm('手动将该账号标记为已验证？')) return;
        await guard(async () => {
          await K.postForm('/api/v1/admin/user/verify', { username: u.username, verified: 1 });
          toast('已标记为已验证', 'success');
          await loadUsers();
        });
      }

      /* ---------------- 构建 ---------------- */
      async function loadBuilds() {
        await guard(async () => {
          const d = await K.api('/api/v1/admin/builds?limit=200');
          builds.value = d.builds || [];
        });
      }
      async function buildAct(b, action) {
        if (action === 'delete' && !confirm('删除该构建记录与产物？')) return;
        if (action === 'retry' && !confirm('重新提交该构建？')) return;
        await guard(async () => {
          await K.postForm('/api/v1/admin/build',
            { request_hash: b.request_hash, action: action });
          toast('已执行：' + action, 'success');
          await loadBuilds();
        });
      }

      /* ---------------- 本地构建物 ---------------- */
      async function loadArtifacts() {
        await guard(async () => {
          art.value = await K.api('/api/v1/admin/artifacts');
        });
      }
      async function artAct(item, action) {
        const label = { delete: '删除该构建的本地产物（保留构建记录）？',
                        delete_record: '删除产物并同时删除构建记录？该操作不可撤销。' }[action];
        if (label && !confirm(label)) return;
        await guard(async () => {
          const r = await K.postForm('/api/v1/admin/artifact',
            { action: action, request_hash: item ? item.request_hash : '' });
          toast('已释放 ' + K.bytes(r.freed || 0) + (r.revoked_tokens ? '，作废令牌 ' + r.revoked_tokens + ' 个' : ''), 'success');
          await loadArtifacts();
          await loadOverview();
        });
      }
      function orphanAct() {
        if (!confirm('清理 ' + (art.value.orphan_count || 0) + ' 个孤儿产物目录？该操作不可撤销。')) return;
        return guard(async () => {
          const r = await K.postForm('/api/v1/admin/artifact', { action: 'delete_orphans' });
          toast('已清理 ' + r.removed + ' 个，释放 ' + K.bytes(r.freed || 0), 'success');
          await loadArtifacts();
          await loadOverview();
        });
      }

      /* ---------------- 队列 ---------------- */
      async function queueAct(action) {
        const confirmMap = {
          purge_store: '清空全部构建产物？该操作不可撤销。',
          clear_finished: '清空已结束的队列任务？',
        };
        if (confirmMap[action] && !confirm(confirmMap[action])) return;
        await guard(async () => {
          const r = await K.postForm('/api/v1/admin/queue', { action: action });
          toast(r.detail || '已执行', 'success');
          await loadOverview();
        });
      }
      async function setBackend(name) {
        await guard(async () => {
          await K.postForm('/api/v1/admin/site', { 'builder.backend': name });
          toast('构建后端已切换为 ' + (name === 'github' ? 'GitHub' : '本机'), 'success');
          await loadOverview();
          await loadCfg();
        });
      }
      async function testGithub() {
        loading.ghtest = true;
        await guard(async () => {
          const r = await K.postForm('/api/v1/admin/github/test', {});
          toast(r.detail || '连接成功', 'success');
        });
        loading.ghtest = false;
      }

      /* ---------------- 站点设置 ---------------- */
      function fieldKey(f) { return f.k !== undefined ? f.k : f.key; }
      function groupKey(g) { return g.k !== undefined ? g.k : g.key; }

      async function loadCfg() {
        await guard(async () => {
          const d = await K.api('/api/v1/admin/site');
          schema.value = (d.schema || []).map(function (f) {
            return { k: fieldKey(f), g: f.g, t: f.t, label: f.label,
                     hint: f.hint || '', opts: f.opts || [], min: f.min, max: f.max,
                     secret: !!f.secret, d: f.d };
          });
          groups.value = (d.groups || []).map(function (g) {
            return { k: groupKey(g), label: g.label || g[0], desc: g.desc || g[2] || '' };
          });
          const vals = d.values || {};
          Object.keys(vals).forEach(function (k) {
            cfg[k] = vals[k];
            orig[k] = vals[k];
            const f = schema.value.find(function (x) { return x.k === k; });
            if (f && f.t === 'json') cfgText[k] = JSON.stringify(vals[k], null, 2);
          });
          tiersEdit.value = JSON.parse(JSON.stringify(vals['sponsor.tiers'] || []));
          origTiers = JSON.stringify(tiersEdit.value);
        });
      }
      function fieldsOf(g) {
        // 套餐用可视化编辑器，不再重复渲染原始 JSON 文本框
        return schema.value.filter(function (f) {
          return f.g === g && f.k !== 'sponsor.tiers';
        });
      }
      function groupDirty(g) {
        return fieldsOf(g).filter(function (f) { return isDirty(f.k); }).length
          + (g === 'sponsor' && JSON.stringify(tiersEdit.value) !== origTiers ? 1 : 0);
      }
      function isDirty(k) {
        const f = schema.value.find(function (x) { return x.k === k; });
        if (f && f.t === 'json') {
          // json 字段：规范化后再比对，否则永远显示「已改」
          try {
            const a = JSON.stringify(JSON.parse(cfgText[k] || 'null'));
            const b = JSON.stringify(orig[k] == null ? null : orig[k]);
            return a !== b;
          } catch (e) {
            return true;   // 解析失败视为有改动（保存时会被后端拒绝并提示）
          }
        }
        return JSON.stringify(cfg[k]) !== JSON.stringify(orig[k]);
      }
      const dirtyCount = computed(function () {
        let n = 0;
        schema.value.forEach(function (f) {
          if (f.k === 'sponsor.tiers') return;      // 该字段由可视化编辑器管理
          if (isDirty(f.k)) n++;
        });
        if (JSON.stringify(tiersEdit.value) !== origTiers) n++;
        return n;
      });
      function resetCfg() {
        Object.keys(orig).forEach(function (k) {
          cfg[k] = orig[k];
          const f = schema.value.find(function (x) { return x.k === k; });
          if (f && f.t === 'json') cfgText[k] = JSON.stringify(orig[k], null, 2);
        });
        tiersEdit.value = JSON.parse(origTiers || '[]');
        toast('已放弃未保存的改动', 'info');
      }
      async function saveAllCfg() {
        const payload = {};
        schema.value.forEach(function (f) {
          if (f.t === 'json') return;
          if (f.secret && (cfg[f.k] === '********' || cfg[f.k] === '')) return;  // 不覆盖原密钥
          if (isDirty(f.k)) payload[f.k] = cfg[f.k];
        });
        if (JSON.stringify(tiersEdit.value) !== origTiers) {
          payload['sponsor.tiers'] = JSON.parse(JSON.stringify(tiersEdit.value));
        }
        if (!Object.keys(payload).length) { toast('没有需要保存的改动', 'info'); return; }
        saving.value = true;
        await guard(async () => {
          const r = await K.postJson('/api/v1/admin/site', payload);
          if (r && r.errors && Object.keys(r.errors).length) {
            toast('部分字段被拒绝：' + Object.keys(r.errors).join('、'), 'error');
          } else {
            toast('已保存 ' + Object.keys(payload).length + ' 项', 'success');
          }
          await loadCfg();
          await loadSite();
          await loadOverview();
        });
        saving.value = false;
      }
      function addTier() {
        tiersEdit.value.push({ name: '新套餐', amount: 10, days: 30, perks: ['解锁全部定制项'] });
      }
      function removeTier(i) {
        if (!confirm('删除套餐「' + (tiersEdit.value[i].name || '') + '」？')) return;
        tiersEdit.value.splice(i, 1);
      }
      function setPerks(i, text) {
        tiersEdit.value[i].perks = text.split('\n').map(function (x) { return x.trim(); })
          .filter(Boolean);
      }

      /* ---------------- 邮件 ---------------- */
      async function loadMailLog() {
        await guard(async () => {
          const d = await K.api('/api/v1/admin/mail/log');
          mailLog.value = d.log || [];
        });
      }
      async function testMail() {
        if (!mailTestTo.value) return;
        loading.mailtest = true;
        try {
          await guard(async () => {
            const r = await K.postForm('/api/v1/admin/mail/test', { to: mailTestTo.value });
            toast(r.detail || '已发送', 'success');
          });
        } finally {
          // ★ 刷新投递记录必须放在「成功失败都要走」的位置。
          //   原来这一句写在 guard 回调里、紧跟在一个**可能抛异常**的请求后面：
          //   发信失败时接口回 400 → 抛出 → 后面被跳过 → 记录页保持旧内容。
          //   而「测试邮件失败」恰恰是最该在投递记录里看到的那一条
          //   （后端本来就记 ok=0 的失败记录，见 verify.log_send）。
          //   这类「异常一抛、收尾被跳过」的写法在别处也要当心。
          await loadMailLog();
          loading.mailtest = false;
        }
      }

      /* ---------------- 邮箱验证 ---------------- */
      async function loadVerifyStats() {
        await guard(async () => {
          verifyStats.value = await K.api('/api/v1/admin/pay/verify_stats');
        });
      }

      /* ---------------- 下载链接 ---------------- */
      async function loadTokens() {
        await guard(async () => {
          const d = await K.api('/api/v1/admin/tokens?limit=300');
          tokens.value = d.tokens || [];
          tokenStats.value = d.stats || null;
        });
      }
      async function tokenAct(action) {
        await guard(async () => {
          const r = await K.postForm('/api/v1/admin/tokens', { action: action });
          toast(r.detail || '已执行', 'success');
          await loadTokens();
        });
      }
      async function tokenItemAct(t, action) {
        if (action === 'revoke' && !confirm('吊销该下载链接？用户将无法再下载。')) return;
        await guard(async () => {
          await K.postForm('/api/v1/admin/tokens',
            { action: action, token: t.token, request_hash: t.request_hash,
              filename: t.filename });
          toast('已执行：' + action, 'success');
          await loadTokens();
        });
      }

      /* ---------------- 赞助 ---------------- */
      async function loadClaims() {
        await guard(async () => {
          const d = await K.api('/api/v1/admin/sponsor/claims');
          claims.value = d.claims || [];
          badges.claims = claims.value.filter(function (c) { return c.status === 'pending'; }).length;
        });
      }
      async function claimAct(c, action) {
        await guard(async () => {
          await K.postForm('/api/v1/admin/sponsor/claim',
            { id: c.id, action: action, days: c.days || 30 });
          toast(action === 'approve' ? '已通过' : '已拒绝', 'success');
          await loadClaims();
        });
      }

      /* ---------------- 在线支付 ---------------- */
      function payStatusText(s) { return PAY_STATUS[s] || s || '—'; }
      async function loadPay() {
        await guard(async () => {
          payInfo.value = await K.api('/api/v1/admin/pay/info');
        });
        await loadPayOrders();
      }
      async function loadPayOrders() {
        await guard(async () => {
          const q = payFilter.value ? ('?status=' + payFilter.value) : '';
          const d = await K.api('/api/v1/admin/pay/orders' + q);
          payOrders.value = d.orders || [];
        });
      }

      /* ---------------- 退款审核 ---------------- */
      async function loadRefunds() {
        await guard(async () => {
          const q = refundFilter.value ? ('?status=' + refundFilter.value) : '';
          const d = await K.api('/api/v1/admin/refunds' + q);
          refunds.value = d.requests || [];
          refundStats.value = d.stats || {};
          badges.refund = d.pending || 0;
        });
      }
      function openRefund(r, action) {
        refundModal.value = { req: r, action: action };
        refundNote.value = '';
        refundOffline.value = false;
      }
      function closeRefund() { refundModal.value = null; }
      async function refundAct() {
        const m = refundModal.value;
        if (!m) return;
        const verb = m.action === 'approve' ? '同意退款' : '驳回申请';
        if (!confirm('确认' + verb + '？' + (m.action === 'approve' ?
            '\n\n将通过支付宝发起真实退款，并回收该订单对应的赞助天数。' : ''))) return;
        loading.refundact = true;
        await guard(async () => {
          const fd = { rid: String(m.req.id), action: m.action, note: refundNote.value || '' };
          if (m.action === 'approve' && refundOffline.value) fd.offline = '1';
          const r = await K.postForm('/api/v1/admin/refund', fd);
          toast(r.detail || '操作成功', 'success');
          closeRefund();
          await loadRefunds();
        });
        loading.refundact = false;
      }
      function refundStatusBadge(s) {
        return ({ pending: 'badge-warning', approved: 'badge-success',
                  rejected: 'badge', failed: 'badge-danger' })[s] || 'badge';
      }

      /* ---------------- 插件目录 ---------------- */
      async function loadCatalog() {
        await guard(async () => {
          const d = await K.api('/api/v1/admin/catalog');
          catalog.value = d.catalog || { cats: [], presets: [], suites: [] };
          catalogCounts.value = d.counts || {};
        });
      }
      async function catalogAct(payload) {
        loading.catalogact = true;
        await guard(async () => {
          const r = await K.postJson('/api/v1/admin/catalog', payload);
          catalog.value = r.catalog || catalog.value;
          catalogCounts.value = {
            presets: (r.catalog.presets || []).length,
            cats: (r.catalog.cats || []).length,
            suites: (r.catalog.suites || []).length,
          };
          toast('已更新', 'success');
        });
        loading.catalogact = false;
      }
      async function addPreset() {
        if (!catForm.name.trim()) { toast('请填写软件包名', 'warning'); return; }
        await catalogAct({ action: 'add_preset', name: catForm.name.trim(),
                           label: catForm.label.trim(), cat: catForm.cat,
                           desc: catForm.desc.trim() });
        catForm.name = ''; catForm.label = ''; catForm.desc = '';
      }
      function removePreset(p) {
        if (!confirm('从构建清单中移除 ' + p.n + ' ？')) return;
        catalogAct({ action: 'remove_preset', name: p.n });
      }
      async function addCat() {
        await catalogAct({ action: 'add_cat', key: catNewCat.key.trim(),
                           label: catNewCat.label.trim() });
        catNewCat.key = ''; catNewCat.label = '';
      }
      function removeCat(c) {
        if (!confirm('删除分类「' + c.l + '」？')) return;
        catalogAct({ action: 'remove_cat', key: c.k });
      }
      async function addSuite() {
        await catalogAct({ action: 'add_suite', key: catNewSuite.key.trim(),
                           label: catNewSuite.label.trim(),
                           pkgs: catNewSuite.pkgs.split(/[,\s]+/).filter(Boolean) });
        catNewSuite.key = ''; catNewSuite.label = ''; catNewSuite.pkgs = '';
      }
      function removeSuite(s) {
        if (!confirm('删除套件「' + s.l + '」？')) return;
        catalogAct({ action: 'remove_suite', key: s.k });
      }
      function resetCatalog() {
        if (!confirm('恢复为内置默认插件清单？\n\n你在后台的增删改动将被覆盖。')) return;
        catalogAct({ action: 'reset' });
      }
      function catName(k) {
        const c = (catalog.value.cats || []).find(function (x) { return x.k === k; });
        return c ? c.l : k;
      }
      const catalogFiltered = computed(function () {
        const q = catQ.value.trim().toLowerCase();
        const ps = catalog.value.presets || [];
        if (!q) return ps;
        return ps.filter(function (p) {
          return (p.n + ' ' + p.l + ' ' + (p.d || '')).toLowerCase().indexOf(q) >= 0;
        });
      });
      async function testPay() {
        loading.paytest = true;
        await guard(async () => {
          const r = await K.postForm('/api/v1/admin/pay/test', {});
          toast(r.detail || '测试下单成功，签名与验签均通过', 'success');
          await loadPay();
        });
        loading.paytest = false;
      }
      /* 复制异步通知地址：优先用 Clipboard API，失败则退回 execCommand。 */
      async function copyNotify() {
        const url = (payInfo.value && payInfo.value.notify_url) || '';
        if (!url) { toast('暂无可复制的地址', 'warning'); return; }
        await K.copy(url);
      }
      async function payOrderAct(o, action) {
        if (action === 'confirm' &&
            !confirm('确认为 ' + o.username + ' 发放 ' + o.days + ' 天赞助？')) return;
        await guard(async () => {
          const r = await K.postForm('/api/v1/admin/pay/order',
            { out_trade_no: o.out_trade_no, action: action });
          toast(r.detail || '已执行', 'success');
          await loadPay();
        });
      }

      /* ---------------- 提议 ---------------- */
      async function loadProp() {
        await guard(async () => {
          const d = await K.api('/api/v1/admin/proposals?limit=100');
          props.value = (d.proposals || []).map(function (p) {
            p._reply = p.reply || '';
            return p;
          });
          badges.prop = props.value.filter(function (p) { return p.status === 'pending'; }).length;
        });
      }
      async function propAct(p, action) {
        await guard(async () => {
          await K.postForm('/api/v1/admin/proposal',
            { id: p.id, action: action, reply: p._reply || '' });
          toast('已处理', 'success');
          await loadProp();
        });
      }

      /* ---------------- 封禁 ---------------- */
      const banForm = reactive({ type: 'ip', value: '', reason: '' });
      async function loadBans() {
        await guard(async () => {
          const d = await K.api('/api/v1/admin/bans');
          bans.value = d.bans || [];
        });
      }
      async function addBan() {
        if (!banForm.value) return;
        await guard(async () => {
          await K.postForm('/api/v1/admin/ban',
            { action: 'add', type: banForm.type, value: banForm.value, reason: banForm.reason });
          banForm.value = ''; banForm.reason = '';
          toast('已添加封禁', 'success');
          await loadBans();
        });
      }
      async function rmBan(b) {
        await guard(async () => {
          await K.postForm('/api/v1/admin/ban', { action: 'remove', type: b.type, value: b.value });
          toast('已解除', 'success');
          await loadBans();
        });
      }

      /* ---------------- 日志 ---------------- */
      async function loadLogs() {
        await guard(async () => {
          const d = await K.api('/api/v1/admin/logs?limit=300');
          logs.value = d.logs || [];
        });
      }
      async function clearLogs() {
        if (!confirm('清空全部审计日志？')) return;
        await guard(async () => {
          await K.postForm('/api/v1/admin/logs/clear', {});
          toast('已清空', 'success');
          await loadLogs();
        });
      }

      /* ---------------- 导航 ---------------- */
      const LOADERS = {
        ov:      function () { loadOverview(); },
        users:   function () { loadUsers(); },
        builds:  function () { loadBuilds(); },
        queue:   function () { loadOverview(); loadArtifacts(); },
        site:    function () { loadCfg(); },
        mail:    function () { loadCfg(); loadMailLog(); },
        verify:  function () { loadCfg(); loadVerifyStats(); },
        tokens:  function () { loadTokens(); loadCfg(); },
        sponsor: function () { loadClaims(); loadOverview(); },
        pay:     function () { loadPay(); loadCfg(); },
        refund:  function () { loadRefunds(); },
        catalog: function () { loadCatalog(); },
        prop:    function () { loadProp(); },
        bans:    function () { loadBans(); },
        logs:    function () { loadLogs(); },
      };
      function go(k) {
        tab.value = k;
        location.hash = k;
        const fn = LOADERS[k];
        if (fn) fn();
      }

      async function loadSite() {
        try { site.value = await K.api('/api/v1/site'); } catch (e) { }
      }

      /* ---------------- 生命周期 ---------------- */
      watch(tab, function (k) { location.hash = k; });
      // 反向同步：直接改地址栏 hash（或浏览器前进/后退）也要切换模块，
      // 否则刷新到 #refund 会停在默认的总览页。
      function syncFromHash() {
        const k = (location.hash || '').replace('#', '');
        if (k && k !== tab.value && TABS.some(function (t) { return t.k === k; })) {
          go(k);
        }
      }
      window.addEventListener('hashchange', syncFromHash);

      onMounted(async function () {
        await loadSite();
        try {
          me.value = await K.api('/api/v1/user');
          // 兼容两种形态：is_admin 布尔字段，或 role 字符串
          isAdmin.value = !!(me.value.is_admin || me.value.role === 'admin');
        } catch (e) { isAdmin.value = false; }
        authChecked.value = true;
        if (!isAdmin.value) return;
        await loadCfg();          // 套餐数据供用户弹窗使用
        const want = (location.hash || '').replace('#', '');
        go(TABS.some(function (t) { return t.k === want; }) ? want : 'ov');
      });

      return {
        authChecked, isAdmin, me, site, tab, tabs, badges, loading, saving,
        ov, backend, mailReady, users, userQ, filteredUsers, userModal, sponsorModal,
        upd, checkUpdate,
        builds, art, loadArtifacts, artAct, orphanAct,
        builds, tokens, tokenStats, claims, props, bans, logs, mailLog, mailTestTo,
        verifyStats, payInfo, payOrders, payFilter,
        refunds, refundFilter, refundStats, refundModal, refundNote, refundOffline,
        loadRefunds, openRefund, closeRefund, refundAct, refundStatusBadge,
        catalog, catalogCounts, catForm, catNewCat, catNewSuite, catTab, catQ,
        loadCatalog, addPreset, removePreset, addCat, removeCat, addSuite,
        removeSuite, resetCatalog, catName, catalogFiltered,
        schema, groups, cfg, cfgText, tiersEdit, dirtyCount,
        fieldsOf, groupDirty, resetCfg, saveAllCfg, addTier, removeTier, setPerks,
        loadOverview, loadUsers, openCreateUser, editUser, saveUser, delUser,
        sponsorUser, sponsorAct, adminReverify, adminSetVerified,
        loadBuilds, buildAct, queueAct, setBackend, testGithub,
        loadMailLog, testMail, loadVerifyStats, loadTokens, tokenAct, tokenItemAct,
        loadClaims, claimAct, payStatusText, loadPay, loadPayOrders, testPay, copyNotify, payOrderAct,
        loadProp, propAct, banForm, loadBans, addBan, rmBan, loadLogs, clearLogs,
        go, logout, K,
      };

      async function logout() {
        await fetch('/api/v1/logout', { method: 'POST' });
        location.href = '/';
      }
    }
  }).mount('#app');
})();
