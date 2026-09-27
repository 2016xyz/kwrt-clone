/* 管理后台交互：所有写操作走 JSON 接口，统一带 CSRF。 */
(function () {
  'use strict';
  var K = window.KWRT || {};
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };

  function toast(msg, ms) {
    var t = $('#toast');
    if (!t) { return; }
    t.textContent = msg; t.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(function () { t.hidden = true; }, ms || 2600);
  }

  function api(url, payload) {
    return fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRF-Token': K.csrf || ''
      },
      credentials: 'same-origin',
      body: JSON.stringify(payload || {})
    }).then(function (r) {
      return r.text().then(function (txt) {
        var d;
        try { d = JSON.parse(txt); } catch (e) { d = { raw: txt }; }
        if (!r.ok) {
          var err = new Error((d && (d.detail || d.error)) || ('HTTP ' + r.status));
          err.data = d; throw err;
        }
        return d;
      });
    });
  }

  // ---------------------------------------------------------------- 通用表单提交
  $$('form[data-admin]').forEach(function (f) {
    f.addEventListener('submit', function (ev) {
      ev.preventDefault();
      var kind = f.getAttribute('data-admin');
      var fd = new FormData(f);
      var body = {};
      fd.forEach(function (v, k) { if (k !== '_csrf') { body[k] = v; } });
      var url = {
        userCreate: '/api/v1/admin/user/create',
        userSponsor: '/api/v1/admin/user/sponsor',
        mailTest: '/api/v1/admin/mail/test',
        banAdd: '/api/v1/admin/ban'
      }[kind];
      if (kind === 'banAdd') { body.action = 'add'; }
      var btn = $('button[type="submit"]', f);
      if (btn) { btn.disabled = true; }
      api(url, body).then(function (d) {
        toast('成功');
        if (kind === 'mailTest') { toast('已发送，请查收'); }
        setTimeout(function () { location.reload(); }, 600);
      }).catch(function (e) {
        if (btn) { btn.disabled = false; }
        var hint = $('[data-hint]', f) || $('.save-hint', f);
        if (hint) { hint.textContent = e.message; }
        toast(e.message || '操作失败', 4200);
        // ★ 失败也要刷新投递记录。PHP 的 AdminController::mailTest 失败时
        //   照样写 email_send_log（ok=0），而「测试邮件失败」正是最该在
        //   投递记录里看到的那一条。原来只在 then 里 reload，失败时页面
        //   保持旧内容 —— 用户看到报错、翻记录却什么都没有，像功能坏了。
        //   延迟比成功分支长，是为了让上面的报错提示能读得完再刷新。
        if (kind === 'mailTest') { setTimeout(function () { location.reload(); }, 3200); }
      });
    });
  });

  // ---------------------------------------------------------------- 站点设置分组保存
  $$('form.settings-form').forEach(function (f) {
    f.addEventListener('submit', function (ev) {
      ev.preventDefault();
      var body = {};
      $$('input,select,textarea', f).forEach(function (el) {
        var n = el.getAttribute('name');
        if (!n || n === '_csrf') { return; }
        if (el.type === 'checkbox') { body[n] = el.checked ? 1 : 0; }
        else if (el.type === 'password' && el.value === '') { /* 留空不改 */ }
        else { body[n] = el.value; }
      });
      var btn = $('button[type="submit"]', f);
      var hint = $('.save-hint', f);
      if (btn) { btn.disabled = true; }
      api('/api/v1/admin/settings', body).then(function () {
        if (hint) { hint.textContent = '已保存 ✓'; }
        toast('已保存');
      }).catch(function (e) {
        // 关闭登录的自锁保护会返回 409，这里给出可读提示
        if (e.data && e.data.error_code === 'LOCKOUT_RISK') {
          if (confirm(e.message + '\n\n仍要关闭吗？')) {
            body.confirm_lockout = '1';
            return api('/api/v1/admin/settings', body).then(function () {
              toast('已保存（登录已关闭，请留意自锁风险）', 5000);
            });
          }
        }
        if (hint) { hint.textContent = e.message; }
        toast(e.message || '保存失败', 4200);
      }).finally(function () { if (btn) { btn.disabled = false; } });
    });
  });

  // ---------------------------------------------------------------- 用户操作
  $$('[data-user]').forEach(function (b) {
    b.addEventListener('click', function () {
      var action = b.getAttribute('data-user');
      var username = b.getAttribute('data-username');
      var body = { action: action, username: username };
      if (action === 'set_role') { body.role = b.getAttribute('data-role'); }
      if (action === 'set_sponsor') { body.value = b.getAttribute('data-value') === '1'; }
      if (action === 'set_quota') {
        var q = prompt('设置每日构建配额（0 = 不限）：', b.getAttribute('data-quota') || '12');
        if (q === null) { return; }
        body.quota = parseInt(q, 10);
      }
      if (action === 'reset_password') {
        var pw = prompt('输入新密码（至少 8 位）：');
        if (!pw) { return; }
        body.password = pw;
      }
      var map = { userVerify: 'user/verify', userReverify: 'user/reverify' };
      var url = map[action] ? ('/api/v1/admin/' + map[action]) : '/api/v1/admin/user';
      if (map[action]) { body = { username: username }; }
      if (action === 'delete' && !confirm('确定删除用户 ' + username + '？')) { return; }
      api(url, body).then(function () { toast('已执行'); setTimeout(function () { location.reload(); }, 500); })
        .catch(function (e) { toast(e.message || '失败', 4000); });
    });
  });

  // ---------------------------------------------------------------- 构建 / 产物 / 队列
  $$('[data-build]').forEach(function (b) {
    b.addEventListener('click', function () {
      var act = b.getAttribute('data-build');
      api('/api/v1/admin/build', { action: act, request_hash: b.getAttribute('data-hash') })
        .then(function () { toast('已执行'); setTimeout(function () { location.reload(); }, 500); })
        .catch(function (e) { toast(e.message, 4000); });
    });
  });
  $$('[data-art]').forEach(function (b) {
    b.addEventListener('click', function () {
      var act = b.getAttribute('data-art');
      var hash = b.getAttribute('data-hash') || '';
      if (act !== 'delete_orphans' && !confirm('确定删除？此操作不可撤销。')) { return; }
      api('/api/v1/admin/artifact', { action: act, request_hash: hash })
        .then(function (d) {
          toast('已处理：释放 ' + (d.freed || 0) + ' 字节');
          setTimeout(function () { location.reload(); }, 600);
        }).catch(function (e) { toast(e.message, 4000); });
    });
  });
  var ar = $('#artRefresh'); if (ar) { ar.addEventListener('click', function () { location.reload(); }); }
  $$('[data-queue]').forEach(function (b) {
    b.addEventListener('click', function () {
      var act = b.getAttribute('data-queue');
      var body = { action: act };
      if (act === 'toggle_builder') { body.value = b.getAttribute('data-value') === '1'; }
      if (act === 'set_backend') { body.value = b.getAttribute('data-value'); }
      if (act === 'set_concurrency') {
        var n = prompt('并发上限（1–16）：', '2');
        if (!n) { return; }
        body.value = parseInt(n, 10);
      }
      if (act === 'purge_store' && !confirm('清空所有构建产物？不可撤销。')) { return; }
      api('/api/v1/admin/queue', body).then(function () {
        toast('已执行'); setTimeout(function () { location.reload(); }, 600);
      }).catch(function (e) { toast(e.message, 4000); });
    });
  });

  // ---------------------------------------------------------------- 令牌 / 提议 / 封禁 / 日志
  $$('[data-token]').forEach(function (b) {
    b.addEventListener('click', function () {
      var act = b.getAttribute('data-token');
      api('/api/v1/admin/tokens', { action: act, token: b.getAttribute('data-token-id') || '' })
        .then(function () { toast('已执行'); setTimeout(function () { location.reload(); }, 500); })
        .catch(function (e) { toast(e.message, 4000); });
    });
  });
  $$('[data-prop]').forEach(function (b) {
    b.addEventListener('click', function () {
      var act = b.getAttribute('data-prop');
      var body = { action: act, id: b.getAttribute('data-id') };
      if (act === 'reply') {
        var r = prompt('回复内容：');
        if (!r) { return; }
        body.reply = r;
      }
      api('/api/v1/admin/proposal', body).then(function () {
        toast('已执行'); setTimeout(function () { location.reload(); }, 500);
      }).catch(function (e) { toast(e.message, 4000); });
    });
  });
  $$('[data-ban-remove]').forEach(function (b) {
    b.addEventListener('click', function () {
      api('/api/v1/admin/ban', { action: 'remove', id: b.getAttribute('data-ban-remove') })
        .then(function () { location.reload(); }).catch(function (e) { toast(e.message, 4000); });
    });
  });
  var lc = $('[data-logs]'); if (lc) {
    lc.addEventListener('click', function () {
      if (!confirm('清空全部审计日志？')) { return; }
      api('/api/v1/admin/logs/clear', {}).then(function () { location.reload(); });
    });
  }

  // ---------------------------------------------------------------- 支付 / 退款 / 赞助
  $$('[data-pay]').forEach(function (b) {
    b.addEventListener('click', function () {
      if (!confirm('将真实调用支付宝网关创建 0.01 元测试单，继续？')) { return; }
      api('/api/v1/admin/pay/test', {}).then(function (d) {
        toast(d.has_qr ? '网关返回二维码，配置正确' : '网关已响应但无二维码', 5000);
      }).catch(function (e) { toast(e.message, 5000); });
    });
  });
  $$('[data-order]').forEach(function (b) {
    b.addEventListener('click', function () {
      api('/api/v1/admin/pay/order', { action: b.getAttribute('data-order'), out_trade_no: b.getAttribute('data-no') })
        .then(function (d) { toast(d.paid ? '已支付' : '已查询'); setTimeout(function () { location.reload(); }, 700); })
        .catch(function (e) { toast(e.message, 4500); });
    });
  });
  $$('[data-refund-op]').forEach(function (b) {
    b.addEventListener('click', function () {
      var act = b.getAttribute('data-refund-op');
      var body = { rid: b.getAttribute('data-rid') };
      if (act === 'approve_offline') { body.action = 'approve'; body.offline = '1'; }
      else { body.action = act; }
      var note = prompt('备注（可选）：') || '';
      body.note = note;
      if (act === 'approve' && !confirm('将真实调用支付宝退款接口，继续？')) { return; }
      api('/api/v1/admin/refund', body)
        .then(function (d) { toast(d.offline ? '已按线下退款处理' : '退款成功'); setTimeout(function () { location.reload(); }, 800); })
        .catch(function (e) { toast(e.message, 6000); setTimeout(function () { location.reload(); }, 2000); });
    });
  });
  $$('[data-claim]').forEach(function (b) {
    b.addEventListener('click', function () {
      api('/api/v1/admin/sponsor/claim', { action: b.getAttribute('data-claim'), id: b.getAttribute('data-id') })
        .then(function () { toast('已处理'); setTimeout(function () { location.reload(); }, 500); })
        .catch(function (e) { toast(e.message, 4000); });
    });
  });
  $$('[data-catalog]').forEach(function (b) {
    b.addEventListener('click', function () {
      var act = b.getAttribute('data-catalog');
      var body = { action: act, name: b.getAttribute('data-name') || '' };
      api('/api/v1/admin/catalog', body)
        .then(function () { toast('已更新'); setTimeout(function () { location.reload(); }, 500); })
        .catch(function (e) { toast(e.message, 4000); });
    });
  });
  var cf = $('form[data-catalog="add_preset"]');
  if (cf) {
    cf.addEventListener('submit', function (ev) {
      ev.preventDefault();
      var fd = new FormData(cf);
      api('/api/v1/admin/catalog', {
        action: 'add_preset',
        name: fd.get('name'), label: fd.get('label'), desc: fd.get('desc'), cat: fd.get('cat')
      }).then(function () { toast('已新增'); setTimeout(function () { location.reload(); }, 500); })
        .catch(function (e) { toast(e.message, 4000); });
    });
  }

  // 复制回调地址
  var cn = $('#copyNotify');
  if (cn) {
    cn.addEventListener('click', function () {
      var el = cn.previousElementSibling;
      var txt = '';
      var box = document.querySelector('[data-notify-url]');
      if (box) { txt = box.textContent.trim(); }
      if (!txt) {
        var m = document.body.innerHTML.match(/https?:\/\/[^\s<"]+\/api\/v1\/alipay\/notify/);
        txt = m ? m[0] : '';
      }
      if (!txt) { toast('未找到地址'); return; }
      navigator.clipboard.writeText(txt).then(function () { toast('已复制'); },
        function () { prompt('手动复制：', txt); });
    });
  }

  // ---------------------------------------------------------------- 检查更新（GitHub）
  // 只读操作：查询更新源并显示结果，不落地任何文件、不重启服务。
  // 失败必须显示成失败 —— 把「查不了」显示成「已是最新」会让管理员错过安全更新。
  var ub = $('#checkUpdateBtn');
  if (ub) {
    var um = $('#updateMsg');
    var ubox = $('#updateBox');
    var uf = $('#checkUpdateForce');

    var esc = function (s) {
      return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
        return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
      });
    };

    var renderUpdate = function (d) {
      var failed = d.status === 'error';
      if (um) {
        um.textContent = ' ' + (d.message || '');
        um.className = 'sm ' + (failed ? 'err' : (d.has_update ? 'warn' : 'muted'));
      }
      if (!ubox) { return; }
      if (failed) {
        ubox.hidden = false;
        ubox.className = 'notice err';
        ubox.innerHTML = '<strong>检查更新失败</strong><div class="sm">' + esc(d.message || '')
          + '</div><div class="sm muted">失败不会被当成「已是最新」—— 请先解决上面的原因再重试。</div>';
      } else if (d.has_update) {
        ubox.hidden = false;
        ubox.className = 'notice';
        var when = (d.published_at || '').slice(0, 10);
        ubox.innerHTML = '<strong>发现新版本 ' + esc(d.latest_display || '') + '</strong>'
          + (when ? '<div class="sm muted">发布于 ' + esc(when) + ' · 来源：'
              + (d.source === 'release' ? 'GitHub Release' : 'VERSION 文件') + '</div>' : '')
          + (d.notes ? '<pre class="err-detail sm">' + esc(d.notes) + '</pre>' : '')
          + '<div class="sm muted">升级步骤见 docs/更新升级与完整性检查.md：'
          + '先备份（数据库 + store/ + 配置）→ 拉取新版本 → 重启服务。</div>'
          + (d.html_url ? '<div class="btn-row"><a class="btn btn-xs btn-outline" target="_blank"'
              + ' rel="noopener noreferrer" href="' + esc(d.html_url) + '">在 GitHub 查看</a></div>' : '');
      } else {
        ubox.hidden = false;
        ubox.className = 'notice';
        ubox.innerHTML = '<div class="sm">' + esc(d.message || '') + '</div>'
          + (d.cached ? '<div class="sm muted">缓存结果；点「强制」忽略缓存重查。</div>' : '');
      }
      if (uf) { uf.hidden = false; }
    };

    var runCheck = function (force) {
      if (ub.disabled) { return; }
      var url = ub.getAttribute('data-url') || '/api/v1/admin/update/check';
      ub.disabled = true;
      var old = ub.textContent;
      ub.textContent = '检查中…';
      if (force && ubox) { ubox.hidden = true; }
      fetch(url + (force ? '?force=1' : ''), { credentials: 'same-origin' })
        .then(function (r) {
          return r.text().then(function (txt) {
            var d;
            try { d = JSON.parse(txt); } catch (e) { d = { status: 'error', message: '返回不是合法 JSON' }; }
            if (!r.ok && d.status !== 'error') {
              d = { status: 'error', message: (d.detail || d.error || ('HTTP ' + r.status)) };
            }
            return d;
          });
        })
        .then(function (d) { renderUpdate(d); toast(d.message || '已检查', d.status === 'error' ? 5000 : 2600); })
        .catch(function (e) { renderUpdate({ status: 'error', message: e.message || '请求失败' }); })
        .finally(function () { ub.disabled = false; ub.textContent = old; });
    };

    ub.addEventListener('click', function () { runCheck(false); });
    if (uf) { uf.addEventListener('click', function () { runCheck(true); }); }
  }

  var lo = $('#logoutBtn');
  if (lo) {
    lo.addEventListener('click', function () {
      fetch('/api/v1/logout', { method: 'POST', credentials: 'same-origin',
        headers: { 'X-CSRF-Token': K.csrf || '' } }).finally(function () { location.href = '/'; });
    });
  }
})();
