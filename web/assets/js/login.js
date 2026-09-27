/* ==========================================================================
   账户页逻辑：登录 / 注册 / 邮箱验证 / 赞助（含支付宝当面付扫码轮询）
   -------------------------------------------------------------------------- */
(function () {
  'use strict';
  if (!window.Vue) return;
  const { createApp, ref, reactive, computed, onMounted, onUnmounted, watch } = Vue;

  createApp({
    setup() {
      const site = ref({});
      const user = ref({ logged_in: false });
      const tiers = ref([]);
      const pay = ref({ available: false });
      const tab = ref('login');
      const showPw = ref(false);
      const pending = ref(null);          // 注册后待验证信息
      const loginNeedsVerify = ref(false);
      const resending = ref(false);
      const chosen = ref('');
      const busy = reactive({ login: false, reg: false, claim: false });
      const paying = ref(false);
      const order = ref(null);            // 支付弹窗状态
      const dls = ref([]);
      const showDl = ref(false);
      const year = new Date().getFullYear();
      const menu = ref(false);

      const lf = reactive({ username: '', password: '', captcha: '' });
      const rf = reactive({ username: '', email: '', password: '', password2: '', captcha: '' });
      // 验证码：on 表示后台开关已开启（由 /api/v1/site 的 captcha 字段告知），
      // id 是本次签发的编号，提交时随表单一起送上去做一次性校验。
      const capLogin = reactive({ on: false, id: '', svg: '', length: 4 });
      const capReg = reactive({ on: false, id: '', svg: '', length: 4 });

      const sym = computed(function () {
        return { CNY: '¥', USD: '$', EUR: '€', JPY: '¥', HKD: 'HK$' }[site.value.currency] || '¥';
      });
      const needVerify = computed(function () { return !!site.value.verify_register; });
      // 「忘记密码」入口是否显示：由后台页面开关 page.reset_enabled 决定。
      // 与注册开关同理 —— 服务端已按 netcfg.PAGE_SWITCHES 拦掉 /reset/，
      // 前端隐藏只是避免给用户一条点了就 404 的死链。
      const resetEnabled = computed(function () { return !!((site.value.nav || {}).reset); });
      const untilText = computed(function () {
        const u = user.value.sponsor_until;
        if (!u) return '—';
        const d = Math.ceil((u * 1000 - Date.now()) / 86400000);
        return d > 0 ? d + ' 天后' : '已到期';
      });

      /* ---------------- 验证码 ---------------- */
      // which: 'login' | 'register'
      async function loadCaptcha(which) {
        const box = which === 'register' ? capReg : capLogin;
        try {
          const r = await K.api('/api/v1/captcha');
          box.id = r.id || '';
          box.svg = r.svg || '';
          box.length = r.length || 4;
          box.on = true;
          // 换一张时清掉已输入的旧字符，否则用户会拿旧码配新图
          if (which === 'register') rf.captcha = ''; else lf.captcha = '';
        } catch (e) {
          // 后台没开验证码时接口返回 404 —— 这不是错误，隐藏输入框即可
          box.on = false; box.id = ''; box.svg = '';
        }
      }

      /* ---------------- 数据加载 ---------------- */
      async function loadSite() {
        try {
          site.value = await K.api('/api/v1/site');
          tiers.value = site.value.sponsor_tiers || [];
          pay.value = site.value.pay || { available: false };
          if (tiers.value.length) chosen.value = tiers.value[0].name;
          if (site.value.site_short) document.title = '账户 · ' + site.value.site_short;
          // 后台开关决定是否显示验证码输入框（服务端也独立校验，前端只是体验层）
          const cap = site.value.captcha || {};
          capLogin.on = !!cap.login;
          capReg.on = !!cap.register;
          if (capLogin.on) loadCaptcha('login');
          if (capReg.on) loadCaptcha('register');
        } catch (e) { /* 站点信息拉取失败不阻塞登录 */ }
      }
      async function loadUser() {
        try {
          user.value = await K.api('/api/v1/user');
        } catch (e) { user.value = { logged_in: false }; }
      }
      async function loadDownloads() {
        try {
          const r = await K.api('/api/v1/downloads');
          dls.value = (r && r.downloads) || [];
        } catch (e) { dls.value = []; }
      }

      /* ---------------- 登录 ---------------- */
      async function doLogin() {
        loginNeedsVerify.value = false;
        busy.login = true;
        try {
          const r = await K.postForm('/api/v1/login', {
            username: lf.username, password: lf.password,
            captcha_id: capLogin.id, captcha_code: lf.captcha
          });
          user.value = r;
          K.toast('登录成功', 'success');
          const next = new URLSearchParams(location.search).get('next');
          if (next && next.charAt(0) === '/') { location.href = next; return; }
          await loadUser();
        } catch (e) {
          const msg = (e && e.message) || '登录失败';
          if (e && e.data && e.data.unverified) {
            loginNeedsVerify.value = true;
            K.toast('邮箱尚未验证，请先完成邮箱验证', 'warning');
          } else {
            K.toast(msg, 'error');
          }
          // 验证码是一次性的：无论因何失败，这张都已作废，必须换新的
          if (capLogin.on) loadCaptcha('login');
        } finally { busy.login = false; }
      }

      /* ---------------- 注册 ---------------- */
      async function doReg() {
        if (rf.password !== rf.password2) { K.toast('两次输入的密码不一致', 'error'); return; }
        if (rf.password.length < 6) { K.toast('密码至少 6 位', 'error'); return; }
        busy.reg = true;
        try {
          const r = await K.postForm('/api/v1/register', {
            username: rf.username, password: rf.password, email: rf.email || '',
            captcha_id: capReg.id, captcha_code: rf.captcha
          });
          if (r.status === 'pending_verification') {
            pending.value = { username: r.username, email: r.email, mail_ok: r.mail_ok };
            if (r.mail_ok) K.toast('验证邮件已发送', 'success');
            else K.toast(r.message || '验证邮件发送失败', 'warning');
          } else {
            user.value = r;
            K.toast('注册成功', 'success');
            await loadUser();
          }
        } catch (e) {
          K.toast((e && e.message) || '注册失败', 'error');
          if (capReg.on) loadCaptcha('register');
        } finally { busy.reg = false; }
      }

      /* ---------------- 重发验证邮件 ---------------- */
      async function doResend() {
        const uname = (pending.value && pending.value.username) || lf.username;
        if (!uname) { K.toast('请先填写用户名', 'warning'); return; }
        resending.value = true;
        try {
          const r = await K.postForm('/api/v1/resend_verify', { username: uname });
          K.toast(r.detail || '验证邮件已发送', 'success');
        } catch (e) {
          K.toast((e && e.message) || '发送失败', 'error');
        } finally { resending.value = false; }
      }

      /* ---------------- 赞助：线下确认 ---------------- */
      async function doClaim() {
        if (!chosen.value) return;
        busy.claim = true;
        try {
          const r = await K.postForm('/api/v1/sponsor/claim', { tier: chosen.value });
          K.toast(r.detail || '已提交', 'success');
          await loadUser();
        } catch (e) {
          K.toast((e && e.message) || '提交失败', 'error');
        } finally { busy.claim = false; }
      }

      /* ---------------- 赞助：支付宝当面付 ---------------- */
      let pollTimer = null;
      let tickTimer = null;

      function stopPoll() {
        if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
        if (tickTimer) { clearInterval(tickTimer); tickTimer = null; }
      }

      async function startPay() {
        if (!chosen.value) return;
        paying.value = true;
        try {
          const r = await K.postForm('/api/v1/sponsor/pay', { tier: chosen.value });
          openOrder(r);
        } catch (e) {
          K.toast((e && e.message) || '下单失败', 'error');
        } finally { paying.value = false; }
      }

      function openOrder(r) {
        const secs = r.expires_in || 900;
        order.value = {
          out_trade_no: r.out_trade_no,
          qr_code: r.qr_code,
          qr_img: '/api/v1/sponsor/pay/' + encodeURIComponent(r.out_trade_no) + '/qr.png',
          amount: r.amount, days: r.days, tier: r.tier,
          state: 'waiting', deadline: Date.now() + secs * 1000,
          leftText: fmtLeft(secs), detail: ''
        };
        tickTimer = setInterval(function () {
          if (!order.value) return;
          const left = Math.floor((order.value.deadline - Date.now()) / 1000);
          if (left <= 0) { order.value.state = 'expired'; stopPoll(); return; }
          order.value.leftText = fmtLeft(left);
        }, 1000);
        schedulePoll(1200);
      }

      function fmtLeft(s) {
        const m = Math.floor(s / 60), sec = s % 60;
        return m + ':' + String(sec).padStart(2, '0');
      }

      function schedulePoll(delay) {
        if (pollTimer) clearTimeout(pollTimer);
        pollTimer = setTimeout(poll, delay || (pay.value.poll_seconds || 3) * 1000);
      }

      async function poll() {
        if (!order.value) return;
        if (order.value.state !== 'waiting') return;
        try {
          const r = await K.api('/api/v1/sponsor/pay/' + encodeURIComponent(order.value.out_trade_no));
          if (r.paid) {
            order.value.state = r.order_status === 'paid_pending' ? 'pending' : 'paid';
            order.value.detail = r.detail || '';
            stopPoll();
            await loadUser();
            K.toast(r.detail || '支付成功', 'success');
            return;
          }
          if (r.order_status === 'expired') {
            order.value.state = 'expired';
            stopPoll(); return;
          }
        } catch (e) {
          // 查单偶发失败不终止轮询，继续等待（不据此判定支付失败）
        }
        schedulePoll();
      }

      async function cancelOrder() {
        if (!order.value) return;
        const paid = order.value.state !== 'waiting';
        if (!paid) {
          try { await K.postForm('/api/v1/sponsor/pay/' +
                  encodeURIComponent(order.value.out_trade_no) + '/cancel', {}); } catch (e) { }
        }
        stopPoll();
        order.value = null;
        await loadUser();
      }

      /* ---------------- 其他 ---------------- */
      async function doLogout() {
        await fetch('/api/v1/logout', { method: 'POST' });
        location.reload();
      }
      async function openDownloads() {
        showDl.value = true;
        await loadDownloads();
      }
      function switchTab(t) {
        tab.value = t;
        loginNeedsVerify.value = false;
        pending.value = null;
      }

      // 表单自动填充：减少重复输入
      watch(lf, function (v) { try { localStorage.setItem('kwrt_login_user', v.username || ''); } catch (e) { } },
            { deep: true });

      onMounted(async function () {
        await Promise.all([loadSite(), loadUser()]);
        try {
          const saved = localStorage.getItem('kwrt_login_user');
          if (saved && !lf.username) lf.username = saved;
        } catch (e) { }
        const want = new URLSearchParams(location.search).get('tab');
        if (want === 'reg') tab.value = 'reg';
        if (site.value.verify_register && new URLSearchParams(location.search).get('verify')) {
          tab.value = 'login';
        }
      });
      onUnmounted(stopPoll);

      return {
        site, user, tiers, pay, tab, showPw, pending, loginNeedsVerify, resending,
        chosen, busy, paying, order, dls, showDl, year, lf, rf, menu,
        sym, needVerify, untilText, resetEnabled,
        capLogin, capReg, loadCaptcha,
        doLogin, doReg, doResend, doClaim, startPay, cancelOrder,
        doLogout, openDownloads, switchTab, K
      };
    }
  }).mount('#app');
})();
