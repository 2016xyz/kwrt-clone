/* Kwrt PHP 版前端脚本
 * 原生 JS，无框架、无 CDN 依赖。所有网络请求都带 CSRF 头。
 * 页面本身已 SSR，本文件只负责交互增强 —— 禁用 JS 时页面仍可读、表单仍可提交。
 */
(function () {
  'use strict';

  var K = window.KWRT || {};
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };

  // ---------------------------------------------------------------- 工具
  function toast(msg, ms) {
    var t = $('#toast');
    if (!t) { return; }
    t.textContent = msg;
    t.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(function () { t.hidden = true; }, ms || 2600);
  }

  function req(method, url, body, opts) {
    opts = opts || {};
    var headers = { 'X-CSRF-Token': K.csrf || '' };
    var payload;
    if (body instanceof FormData) {
      payload = body;
    } else if (body !== undefined && body !== null) {
      headers['Content-Type'] = 'application/json';
      payload = JSON.stringify(body);
    }
    return fetch(url, {
      method: method, headers: headers, body: payload,
      credentials: 'same-origin'
    }).then(function (r) {
      return r.text().then(function (txt) {
        var data;
        try { data = JSON.parse(txt); } catch (e) { data = { raw: txt }; }
        if (!r.ok) {
          var err = new Error((data && (data.detail || data.error)) || ('HTTP ' + r.status));
          err.status = r.status; err.data = data;
          throw err;
        }
        return data;
      });
    });
  }
  window.kwrtReq = req;

  // ---------------------------------------------------------------- 主题
  var THEME_KEY = 'kwrt.theme';
  function applyTheme(mode) {
    document.documentElement.setAttribute('data-theme', mode);
    try { localStorage.setItem(THEME_KEY, mode); } catch (e) {}
  }
  (function initTheme() {
    var saved = null;
    try { saved = localStorage.getItem(THEME_KEY); } catch (e) {}
    if (saved) { applyTheme(saved); }
  })();
  var tb = $('#themeBtn');
  if (tb) {
    tb.addEventListener('click', function () {
      var order = ['auto', 'light', 'dark'];
      var cur = document.documentElement.getAttribute('data-theme') || 'auto';
      var next = order[(order.indexOf(cur) + 1) % order.length];
      applyTheme(next);
      toast('主题：' + ({ auto: '跟随系统', light: '浅色', dark: '深色' }[next]));
    });
  }

  // ---------------------------------------------------------------- 移动菜单
  var burger = $('#burger'), nav = $('#mainNav');
  if (burger && nav) {
    burger.addEventListener('click', function () {
      var open = nav.classList.toggle('open');
      burger.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
  }

  // ---------------------------------------------------------------- 弹层
  function openModal(id) {
    var m = document.getElementById(id);
    if (m) { m.hidden = false; }
  }
  function closeModals() {
    $$('.modal').forEach(function (m) { m.hidden = true; });
  }
  document.addEventListener('click', function (ev) {
    var open = ev.target.closest('[data-open]');
    if (open) {
      ev.preventDefault();
      var map = { app: 'appModal', wechat: 'wechatModal' };
      var t = map[open.getAttribute('data-open')];
      if (t) { openModal(t); }
      return;
    }
    if (ev.target.closest('[data-close]')) { closeModals(); }
  });
  document.addEventListener('keydown', function (ev) {
    if (ev.key === 'Escape') { closeModals(); }
  });

  // ---------------------------------------------------------------- 密码显隐
  document.addEventListener('click', function (ev) {
    var b = ev.target.closest('[data-toggle-pw]');
    if (!b) { return; }
    var inp = document.getElementById(b.getAttribute('data-toggle-pw'));
    if (!inp) { return; }
    var show = inp.type === 'password';
    inp.type = show ? 'text' : 'password';
    b.textContent = show ? '隐藏' : '显示';
  });

  // ---------------------------------------------------------------- 设备检索
  var devQ = $('#devQ'), devSug = $('#devSug');
  var DEVICES = null, devSel = -1, devHit = [];

  function loadDevices(cb) {
    if (DEVICES) { return cb(DEVICES); }
    fetch('/api/v1/site').then(function (r) { return r.json(); }).then(function (d) {
      DEVICES = d.devices || [];
      cb(DEVICES);
    }).catch(function () { DEVICES = []; cb([]); });
  }

  function score(item, q) {
    var n = (item.name || '').toLowerCase(), id = (item.id || '').toLowerCase();
    if (n === q || id === q) { return 100; }
    if (n.indexOf(q) === 0 || id.indexOf(q) === 0) { return 60; }
    if (n.indexOf(q) >= 0) { return 30; }
    if (id.indexOf(q) >= 0) { return 20; }
    return 0;
  }

  function renderSug(list) {
    devHit = list;
    devSel = -1;
    if (!list.length) { devSug.hidden = true; return; }
    devSug.innerHTML = list.map(function (d, i) {
      return '<button type="button" data-i="' + i + '" role="option">'
        + '<span>' + esc(d.name) + '</span> '
        + '<span class="s-id mono">' + esc(d.target || '') + '/' + esc(d.id) + '</span></button>';
    }).join('');
    devSug.hidden = false;
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function pickDevice(d) {
    if (d.target) {
      var t = $('#fTarget'); if (t) { t.value = d.target; }
    }
    var p = $('#fProfile'); if (p) { p.value = d.id || ''; }
    devSug.hidden = true;
    if (devQ) { devQ.value = d.name; }
    toast('已选择：' + d.name);
    var f = $('#fProfile'); if (f) { f.scrollIntoView({ behavior: 'smooth', block: 'center' }); }
  }

  if (devQ && devSug) {
    devQ.addEventListener('input', function () {
      var q = devQ.value.trim().toLowerCase();
      if (q.length < 1) { devSug.hidden = true; return; }
      loadDevices(function (all) {
        var hits = [];
        for (var i = 0; i < all.length && hits.length < 40; i++) {
          var s = score(all[i], q);
          if (s > 0) { hits.push({ d: all[i], s: s }); }
        }
        hits.sort(function (a, b) { return b.s - a.s; });
        renderSug(hits.slice(0, 20).map(function (h) { return h.d; }));
      });
    });
    devQ.addEventListener('keydown', function (ev) {
      if (devSug.hidden) { return; }
      var btns = $$('button', devSug);
      if (ev.key === 'ArrowDown') { ev.preventDefault(); devSel = Math.min(devSel + 1, btns.length - 1); }
      else if (ev.key === 'ArrowUp') { ev.preventDefault(); devSel = Math.max(devSel - 1, 0); }
      else if (ev.key === 'Enter') {
        ev.preventDefault();
        if (btns[devSel]) { btns[devSel].click(); }
        return;
      } else if (ev.key === 'Escape') { devSug.hidden = true; return; }
      else { return; }
      btns.forEach(function (b, i) { b.classList.toggle('sel', i === devSel); });
      if (btns[devSel]) { btns[devSel].scrollIntoView({ block: 'nearest' }); }
    });
    devSug.addEventListener('click', function (ev) {
      var b = ev.target.closest('button[data-i]');
      if (b && devHit[+b.getAttribute('data-i')]) { pickDevice(devHit[+b.getAttribute('data-i')]); }
    });
    document.addEventListener('click', function (ev) {
      if (!ev.target.closest('.search')) { devSug.hidden = true; }
    });
    var go = $('#devGo');
    if (go) {
      go.addEventListener('click', function () {
        if (devHit.length) { pickDevice(devHit[0]); }
        else { devQ.focus(); }
      });
    }
  }

  // ---------------------------------------------------------------- 软件包选择
  var pkgFilter = $('#pkgFilter');
  if (pkgFilter) {
    pkgFilter.addEventListener('input', function () {
      var q = pkgFilter.value.trim().toLowerCase();
      $$('.pkg-cat').forEach(function (cat) {
        var any = false;
        $$('.pkg-item', cat).forEach(function (it) {
          var txt = it.textContent.toLowerCase();
          var show = !q || txt.indexOf(q) >= 0;
          it.style.display = show ? '' : 'none';
          if (show) { any = true; }
        });
        cat.style.display = any ? '' : 'none';
      });
    });
  }

  // 套件：一键勾选一组包
  var SUITES = null;
  function getSuites(cb) {
    if (SUITES) { return cb(SUITES); }
    fetch('/api/v1/packages/catalog').then(function (r) { return r.json(); }).then(function (d) {
      SUITES = d.suites || [];
      cb(SUITES);
    }).catch(function () { SUITES = []; cb([]); });
  }
  document.addEventListener('click', function (ev) {
    var btn = ev.target.closest('[data-suite]');
    if (!btn) { return; }
    var key = btn.getAttribute('data-suite');
    getSuites(function (list) {
      var su = list.filter(function (s) { return s.k === key; })[0];
      if (!su) { return; }
      var on = !btn.classList.contains('on');
      btn.classList.toggle('on', on);
      (su.pkgs || []).forEach(function (n) {
        var cb = document.querySelector('input[name="packages[]"][value="' + CSS.escape(n) + '"]');
        if (cb) { cb.checked = on; }
        else if (on) {
          // 不在预设清单里的包，落到「额外软件包」输入框
          var ex = $('#fExtra');
          if (ex && ex.value.indexOf(n) < 0) {
            ex.value = (ex.value ? ex.value + ' ' : '') + n;
          }
        }
      });
      toast((on ? '已添加套件：' : '已移除套件：') + btn.textContent.trim());
    });
  });

  // ---------------------------------------------------------------- 构建提交
  var form = $('#buildForm');
  if (form) {
    form.addEventListener('submit', function (ev) {
      ev.preventDefault();
      if (!K.user) {
        location.href = '/login/?next=' + encodeURIComponent(location.pathname + location.search);
        return;
      }
      var btn = $('#buildBtn'), hint = $('#buildHint');
      var pkgs = $$('input[name="packages[]"]:checked').map(function (i) { return i.value; });
      var extra = ($('#fExtra') && $('#fExtra').value.trim()) || '';
      var payload = {
        target: ($('#fTarget') || {}).value || '',
        profile: ($('#fProfile') || {}).value || '',
        version: ($('#fVersion') || {}).value || '',
        filesystem: ($('#fFs') || {}).value || 'squashfs',
        rootfs_size_mb: parseInt((($('#fRoot') || {}).value) || '512', 10),
        packages: pkgs,
        extra: extra,
        hostname: ($('#fHost') || {}).value || '',
        lan_ip: ($('#fLan') || {}).value || '',
        ipv6: !!($('input[name="ipv6"]:checked')),
        dhcp: !!($('input[name="dhcp"]:checked')),
        eflasher: !!($('input[name="eflasher"]:checked')),
        usb_net: !!($('input[name="usb_net"]:checked')),
        usb_wireless: !!($('input[name="usb_wireless"]:checked')),
        wanlan: !!($('input[name="wanlan"]:checked'))
      };

      if (!payload.target || !payload.profile) {
        toast('请先填写目标设备与型号');
        return;
      }
      btn.disabled = true;
      if (hint) { hint.textContent = '提交中…'; }

      // 先上传自定义文件包（如果有）
      var fileInput = $('#fFiles');
      var chain = Promise.resolve(null);
      if (fileInput && fileInput.files && fileInput.files[0]) {
        var fd = new FormData();
        fd.append('file', fileInput.files[0]);
        chain = req('POST', '/api/v1/upload', fd).then(function (r) {
          return r.files_path || null;
        });
      }

      chain.then(function (filesPath) {
        if (filesPath) { payload.files_path = filesPath; }
        return req('POST', '/api/v1/build', payload);
      }).then(function (r) {
        if (hint) { hint.textContent = ''; }
        toast('已进入构建队列');
        pollBuild(r.request_hash);
      }).catch(function (e) {
        btn.disabled = false;
        if (hint) { hint.textContent = ''; }
        toast(e.message || '提交失败', 4200);
      });
    });
  }

  // ---------------------------------------------------------------- 构建进度
  var pollTimer = null;
  function pollBuild(hash) {
    var box = $('#buildProg'), bar = $('#progBar'), txt = $('#progText'), files = $('#progFiles');
    if (box) { box.hidden = false; }
    clearTimeout(pollTimer);
    var tries = 0;
    function tick() {
      tries++;
      req('GET', '/api/v1/build/' + encodeURIComponent(hash)).then(function (d) {
        var st = d.status || 'unknown';
        var pct = { queued: 10, running: 55, done: 100, failed: 100, cancelled: 100 }[st] || 20;
        if (bar) { bar.style.width = pct + '%'; }
        if (txt) {
          txt.textContent = '[' + st + '] ' + (d.detail || '') + (d.position ? ' · 队列位置 ' + d.position : '');
        }
        if (st === 'done') {
          if (files && d.download_links && d.download_links.length) {
            files.innerHTML = d.download_links.map(function (l) {
              return '<a class="btn btn-sm btn-outline" href="' + esc(l.url) + '">↓ ' + esc(l.filename) + '</a>';
            }).join('');
          }
          toast('构建完成');
          return;
        }
        if (st === 'failed') { toast('构建失败：' + (d.detail || '未知原因'), 5000); return; }
        if (tries < 1200) { pollTimer = setTimeout(tick, (K.pollMs || 3000)); }
      }).catch(function () {
        if (tries < 1200) { pollTimer = setTimeout(tick, 5000); }
      });
    }
    tick();
  }
  window.kwrtPollBuild = pollBuild;

  // ---------------------------------------------------------------- 验证码
  // 与 Python 版同表同盐：任一版签发的验证码，另一版都能校验。
  function loadCaptcha(which) {
    var box = document.querySelector('[data-captcha="' + which + '"]');
    if (!box) return;
    // 只问后端要不要用验证码（开关在后台），不要用前端猜
    req('GET', '/api/v1/captcha').then(function (d) {
      box.hidden = false;
      var inp = box.querySelector('input[name="captcha_id"]');
      if (inp) inp.value = d.id || '';
      var holder = box.querySelector('.captcha-svg');
      if (holder) holder.innerHTML = d.svg || '';
      var code = box.querySelector('input[name="captcha_code"]');
      if (code) code.value = '';   // 换图必须清空旧输入，否则拿旧码配新图
    }).catch(function () {
      box.hidden = true;           // 后台没开验证码 → 隐藏
    });
  }
  document.addEventListener('click', function (ev) {
    var t = ev.target.closest ? ev.target.closest('[data-captcha-refresh]') : null;
    if (t) { ev.preventDefault(); loadCaptcha(t.getAttribute('data-captcha-refresh')); }
  });
  ['login', 'register', 'reset'].forEach(function (w) { loadCaptcha(w); });

  // ---------------------------------------------------------------- 认证
  var loginForm = $('#loginForm');
  if (loginForm) {
    loginForm.addEventListener('submit', function (ev) {
      ev.preventDefault();
      var msg = $('#loginMsg');
      var fd = new FormData(loginForm);
      fd.delete('_csrf');
      var btn = $('button[type="submit"]', loginForm);
      btn.disabled = true;
      req('POST', '/api/v1/login', {
        username: fd.get('username'), password: fd.get('password'),
        captcha_id: fd.get('captcha_id') || '', captcha_code: fd.get('captcha_code') || ''
      }).then(function (d) {
        msg.textContent = '登录成功，正在跳转…';
        var next = new URLSearchParams(location.search).get('next') || '/';
        location.href = next;
      }).catch(function (e) {
        btn.disabled = false;
        msg.textContent = e.message || '登录失败';
        msg.style.color = 'var(--err)';
        // 验证码是一次性的：任何失败后这张都已作废，必须换新的
        loadCaptcha('login');
      });
    });
  }

  var regForm = $('#regForm');
  if (regForm) {
    regForm.addEventListener('submit', function (ev) {
      ev.preventDefault();
      var msg = $('#regMsg');
      var fd = new FormData(regForm);
      if (fd.get('password') !== fd.get('password2')) {
        msg.textContent = '两次输入的密码不一致';
        return;
      }
      var btn = $('button[type="submit"]', regForm);
      btn.disabled = true;
      req('POST', '/api/v1/register', {
        username: fd.get('username'), email: fd.get('email'), password: fd.get('password'),
        captcha_id: fd.get('captcha_id') || '', captcha_code: fd.get('captcha_code') || ''
      }).then(function () {
        msg.style.color = 'var(--ok)';
        msg.textContent = '注册成功，正在跳转…';
        location.reload();
      }).catch(function (e) {
        btn.disabled = false;
        msg.style.color = 'var(--err)';
        msg.textContent = e.message || '注册失败';
        loadCaptcha('register');
      });
    });
  }

  // 失效态在客户端也能切到（令牌过期/已用）—— 别让用户对着一个死表单白填
  function rstBad(detail) {
    $('#rstRequest').hidden = true;
    $('#rstSetpw').hidden = true;
    $('#rstSent').hidden = true;
    $('#rstDone').hidden = true;
    var b = $('#rstBad');
    if (b) { $('#rstBadDetail').textContent = detail || '重置链接无效或已过期。'; b.hidden = false; }
  }

  var resetForm = $('#resetForm');
  if (resetForm) {
    resetForm.addEventListener('submit', function (ev) {
      ev.preventDefault();
      var msg = $('#resetMsg');
      var fd = new FormData(resetForm);
      var btn = $('button[type="submit"]', resetForm);
      btn.disabled = true;
      req('POST', '/api/v1/reset_request', {
        account: fd.get('account'),
        captcha_id: fd.get('captcha_id') || '', captcha_code: fd.get('captcha_code') || ''
      }).then(function (d) {
        $('#rstRequest').hidden = true;
        $('#rstSentDetail').textContent = (d && d.message) ||
          '如果该账号存在且已绑定邮箱，重置邮件已经发出。';
        $('#rstSent').hidden = false;
      }).catch(function (e) {
        btn.disabled = false;
        msg.style.color = 'var(--err)';
        msg.textContent = e.message || '请求失败';
        // 验证码一次性：任何失败后这张都作废，必须换新的
        loadCaptcha('reset');
      });
    });
  }

  var again = $('#rstAgain');
  if (again) {
    again.addEventListener('click', function () {
      $('#rstSent').hidden = true;
      $('#rstRequest').hidden = false;
      loadCaptcha('reset');
    });
  }

  var restart = $('#rstRestart');
  if (restart) {
    restart.addEventListener('click', function () {
      // 清掉地址栏里的 token，否则刷新又回到失效态
      if (history.replaceState) { history.replaceState(null, '', '/reset/'); }
      $('#rstBad').hidden = true;
      $('#rstSetpw').hidden = true;
      $('#rstRequest').hidden = false;
      loadCaptcha('reset');
    });
  }

  var confirmForm = $('#resetConfirmForm');
  if (confirmForm) {
    confirmForm.addEventListener('submit', function (ev) {
      ev.preventDefault();
      var msg = $('#resetConfirmMsg');
      var fd = new FormData(confirmForm);
      if (fd.get('password') !== fd.get('password2')) {
        msg.style.color = 'var(--err)';
        msg.textContent = '两次输入的密码不一致';
        return;
      }
      var btn = $('button[type="submit"]', confirmForm);
      btn.disabled = true;
      req('POST', '/api/v1/reset_confirm', {
        token: fd.get('token'), password: fd.get('password')
      }).then(function (d) {
        $('#rstSetpw').hidden = true;
        $('#rstDoneUser').textContent = (d && d.username) || '';
        $('#rstDone').hidden = false;
      }).catch(function (e) {
        btn.disabled = false;
        var m = e.message || '重置失败';
        // 令牌类错误直接切到失效态，别让用户继续对着死表单试
        if (/过期|已被使用|无效|不正确/.test(m)) { rstBad(m); return; }
        msg.style.color = 'var(--err)';
        msg.textContent = m;
      });
    });
  }

  // 登录/注册 tab
  document.addEventListener('click', function (ev) {
    var tab = ev.target.closest('.tab[data-tab]');
    if (!tab) { return; }
    var which = tab.getAttribute('data-tab');
    $$('.tab[data-tab]').forEach(function (t) {
      var on = t === tab;
      t.classList.toggle('on', on);
      t.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    var lf = $('#loginForm'), rf = $('#regForm');
    if (lf) { lf.hidden = which !== 'login'; }
    if (rf) { rf.hidden = which !== 'reg'; }
  });

  var logoutBtn = $('#logoutBtn');
  if (logoutBtn) {
    logoutBtn.addEventListener('click', function () {
      req('POST', '/api/v1/logout', {}).then(function () { location.href = '/'; })
        .catch(function () { location.href = '/'; });
    });
  }

  // ---------------------------------------------------------------- 插件提议
  var pf = $('#proposeForm');
  if (pf) {
    pf.addEventListener('submit', function (ev) {
      ev.preventDefault();
      var hint = $('#proposeHint');
      var btn = $('button[type="submit"]', pf);
      btn.disabled = true;
      req('POST', '/api/v1/propose', {
        name: (($('#pName') || {}).value || '').trim(),
        url: (($('#pUrl') || {}).value || '').trim(),
        note: (($('#pNote') || {}).value || '').trim()
      }).then(function () {
        hint.textContent = '已提交，感谢！';
        setTimeout(function () { location.reload(); }, 700);
      }).catch(function (e) {
        btn.disabled = false;
        hint.textContent = e.message || '提交失败';
      });
    });
  }
  document.addEventListener('click', function (ev) {
    var b = ev.target.closest('[data-prop-del]');
    if (!b) { return; }
    if (!confirm('确定删除这条提议？')) { return; }
    req('DELETE', '/api/v1/propose/' + b.getAttribute('data-prop-del')).then(function () {
      location.reload();
    }).catch(function (e) { toast(e.message || '删除失败'); });
  });

  // ---------------------------------------------------------------- 赞助支付（动态收款码）
  // 统一走 /api/v1/sponsor/order：用户输入金额（或选套餐）点击后，服务端返回
  // 「收款码图片地址」，前端只负责显示。服务端决定用哪种：
  //   mode=alipay → 真实下单的支付宝支付二维码，轮询到账
  //   mode=manual → 服务端按金额**实时生成**的收款码，扫码后用户自行点确认
  var SYM = { CNY: '¥', USD: '$', EUR: '€', JPY: '¥', HKD: 'HK$' }[K.currency] || '¥';
  var payCtx = null;

  function fmtNum(v) {
    var n = Number(v);
    return isFinite(n) ? String(n) : '0';
  }

  function openPay(d) {
    var m = $('#payModal');
    if (!m) { return; }
    m.hidden = false;
    payCtx = { mode: d.mode, tier: d.tier || '', amount: d.amount };
    var qr = $('#payQr');
    if (qr) {
      qr.innerHTML = d.url
        ? '<img src="' + esc(d.url) + '" alt="收款二维码">'
        : '<p class="muted">未返回收款码</p>';
    }
    if ($('#payAmount')) {
      $('#payAmount').textContent = SYM + fmtNum(d.amount) + ' / ' + d.days + ' 天';
    }
    if ($('#payNo')) { $('#payNo').textContent = d.out_trade_no || ''; }
    var tip = $('#payTip');
    var cf = $('#payConfirm');
    if (d.mode === 'manual') {
      if ($('#payState')) { $('#payState').textContent = '等待付款'; }
      if (tip) {
        tip.hidden = false;
        tip.textContent = '请扫码支付 ' + SYM + fmtNum(d.amount) + '，付款后点击下方按钮提交确认。';
      }
      if (cf) { cf.hidden = false; }
      return;
    }
    if (tip) {
      tip.hidden = false;
      tip.textContent = '请用支付宝扫码支付，成功后本页面会自动更新。';
    }
    if (cf) { cf.hidden = true; }
    var no = d.out_trade_no;
    var tries = 0;
    var t = setInterval(function () {
      tries++;
      req('GET', '/api/v1/sponsor/pay/' + encodeURIComponent(no)).then(function (s) {
        if (s.paid || s.status === 'paid') {
          clearInterval(t);
          if ($('#payState')) { $('#payState').textContent = '支付成功！'; }
          toast('支付成功，赞助已生效');
          setTimeout(function () { location.reload(); }, 1200);
        } else if (tries > 200) { clearInterval(t); }
      }).catch(function () { if (tries > 200) { clearInterval(t); } });
    }, K.pollMs || 3000);
  }

  function orderSponsor(body) {
    return req('POST', '/api/v1/sponsor/order', body).then(function (d) {
      openPay(d);
      return d;
    });
  }

  document.addEventListener('click', function (ev) {
    var b = ev.target.closest('[data-buy]');
    if (!b) { return; }
    b.disabled = true;
    orderSponsor({ tier: b.getAttribute('data-buy') })
      .catch(function (e) { toast(e.message || '生成收款码失败', 4000); })
      .then(function () { b.disabled = false; });
  });

  // 自定义金额：输入 → 即时校验与天数预览 → 点「生成收款码」
  (function () {
    var box = $('.amount-box');
    var inp = $('#sponsorAmount');
    var btn = $('#sponsorAmountBtn');
    var hint = $('#sponsorAmountHint');
    if (!box || !inp || !btn) { return; }
    var lo = parseFloat(box.getAttribute('data-min')) || 1;
    var hi = parseFloat(box.getAttribute('data-max')) || 99999;
    var perDay = parseFloat(box.getAttribute('data-perday')) || 1;
    var DEF = '输入金额后点击按钮，即可生成对应金额的收款码。也可以直接选上面的套餐。';

    function sync(forceBad) {
      var v = parseFloat(inp.value);
      var bad = forceBad || '';
      if (!bad && String(inp.value).trim() !== '') {
        if (!(v > 0)) { bad = '请输入大于 0 的金额'; }
        else if (v < lo) { bad = '金额不能低于 ' + lo; }
        else if (v > hi) { bad = '金额不能高于 ' + hi; }
      }
      btn.disabled = !!bad || !(v > 0);
      if (!hint) { return; }
      if (bad) { hint.textContent = bad; return; }
      if (v > 0) {
        // 只是给用户看的预估，真正发放的天数由服务端折算
        var days = Math.min(3650, Math.max(1, Math.floor(v / perDay)));
        hint.textContent = '金额 ' + SYM + fmtNum(v) + '，预计发放 ' + days + ' 天赞助权益（最终以服务端为准）。';
        return;
      }
      hint.textContent = DEF;
    }

    inp.addEventListener('input', function () { sync(''); });
    inp.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); btn.click(); }
    });
    btn.addEventListener('click', function () {
      var v = parseFloat(inp.value);
      if (!(v > 0)) { return; }
      btn.disabled = true;
      orderSponsor({ amount: String(v) })
        .catch(function (e) {
          hint && (hint.textContent = e.message || '生成收款码失败');
          toast(e.message || '生成收款码失败', 4000);
        })
        .then(function () { sync(''); });
    });
    sync('');
  })();

  // manual 模式：用户扫码付完款后自行确认
  document.addEventListener('click', function (ev) {
    if (!ev.target.closest('#payConfirm')) { return; }
    if (!payCtx) { return; }
    var body = payCtx.tier ? { tier: payCtx.tier } : { amount: String(payCtx.amount) };
    var btn = $('#payConfirm');
    btn.disabled = true;
    req('POST', '/api/v1/sponsor/claim', body).then(function (d) {
      if ($('#payState')) {
        $('#payState').textContent = d.status === 'ok' ? '赞助已生效' : '已提交，等待站长确认';
      }
      toast(d.detail || '已提交', 4000);
      setTimeout(function () { location.reload(); }, 1500);
    }).catch(function (e) {
      btn.disabled = false;
      toast(e.message || '提交失败', 4000);
    });
  });

  // ---------------------------------------------------------------- 退款申请
  document.addEventListener('click', function (ev) {
    var b = ev.target.closest('[data-refund]');
    if (!b) { return; }
    var no = b.getAttribute('data-refund');
    var reason = prompt('请填写退款原因（至少 5 个字）：');
    if (!reason) { return; }
    if (reason.trim().length < 5) { toast('原因至少 5 个字'); return; }
    var detail = prompt('补充说明（可留空）：') || '';
    req('POST', '/api/v1/sponsor/refund', { out_trade_no: no, reason: reason.trim(), detail: detail })
      .then(function () { toast('退款申请已提交，等待审核'); setTimeout(function () { location.reload(); }, 900); })
      .catch(function (e) { toast(e.message || '提交失败', 4000); });
  });

  // ---------------------------------------------------------------- PWA
  if ('serviceWorker' in navigator && document.querySelector('link[rel="manifest"]')) {
    window.addEventListener('load', function () {
      navigator.serviceWorker.register('/service-worker.js').catch(function () {});
    });
  }

  // ---------------------------------------------------------------- 二维码（离线生成）
  $$('canvas[data-qr]').forEach(function (cv) {
    var data = cv.getAttribute('data-qr') || '';
    var api = cv.getAttribute('data-qr-api') || '';
    if (!data) { return; }
    if (api) {
      // 管理员显式配置了第三方生成服务
      var url = api.indexOf('{url}') >= 0
        ? api.replace('{url}', encodeURIComponent(data))
        : api + encodeURIComponent(data);
      var img = new Image();
      img.crossOrigin = 'anonymous';
      img.onload = function () { cv.getContext('2d').drawImage(img, 0, 0, cv.width, cv.height); };
      img.src = url;
      return;
    }
    if (window.KWRT_QR) { window.KWRT_QR(cv, data); }
  });
})();
