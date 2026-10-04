/* ==========================================================================
   广告位「装配」脚本（仅 PHP 版使用）
   --------------------------------------------------------------------------
   Python 版是 Vue 驱动，模板里直接渲染广告；PHP 版是服务端渲染的静态页面，
   所以这里用一个独立脚本在加载后把广告挂到 DOM 上：
     · 顶部/底部跑马灯 —— 插到 <main> 前后
     · 弹出模态框   —— 追加到 body，按 delay / frequency 依次弹出
   渲染与过滤逻辑全部复用 ads.js 里的 K.adsFilter / K.mdToHtml / K.adsAlreadySeen，
   两个版本共用同一套实现，避免「同一份配置两种表现」。
   ========================================================================== */
(function (global) {
  'use strict';
  var K = global.K || {};
  if (typeof K.mdToHtml !== 'function') { return; }

  function h(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  /* ---------------- 跑马灯 ---------------- */
  function buildMarquee(a) {
    var bar = h('div', 'ad-marquee' + (a.position === 'bottom' ? ' is-bottom' : ''));
    bar.style.background = a.bg || 'var(--primary)';
    bar.style.color = a.color || '#ffffff';
    if (a.title) bar.appendChild(h('span', 'ad-marquee-tag', a.title));
    var view = h('div', 'ad-marquee-view');
    var track = h('div', 'ad-marquee-track');
    track.setAttribute('data-ad-id', a.id || '');
    track.setAttribute('data-ad-speed', String(a.speed || 60));
    for (var i = 0; i < 2; i++) {
      var span = h('span', 'ad-md');
      span.innerHTML = K.mdToHtml(a.content);     // 已经过转义 + 白名单
      track.appendChild(span);
    }
    view.appendChild(track);
    bar.appendChild(view);
    return bar;
  }

  function measure() {
    var nodes = document.querySelectorAll('.ad-marquee-track');
    Array.prototype.forEach.call(nodes, function (el) {
      var one = el.firstElementChild ? el.firstElementChild.getBoundingClientRect().width
                                     : el.getBoundingClientRect().width / 2;
      var speed = parseFloat(el.getAttribute('data-ad-speed')) || 60;
      el.style.animationDuration = Math.max(6, Math.round(Math.max(one, 1) / speed * 10) / 10) + 's';
    });
  }

  /* ---------------- 弹出 ---------------- */
  var queue = [];
  var timer = null;

  function showNext() {
    if (timer) { clearTimeout(timer); timer = null; }
    if (!queue.length || document.querySelector('.ad-popup-mask')) { return; }
    var ad = queue.shift();
    timer = setTimeout(function () {
      timer = null;
      openPopup(ad);
    }, Math.max(0, (ad.delay || 0) * 1000));
  }

  function openPopup(a) {
    var mask = h('div', 'ad-popup-mask');
    var box = h('div', 'ad-popup');
    box.setAttribute('role', 'dialog');
    box.setAttribute('aria-modal', 'true');

    function close() {
      K.adsMarkSeen(a);
      if (mask.parentNode) { mask.parentNode.removeChild(mask); }
      showNext();
    }

    if (a.closable) {
      var x = h('button', 'ad-popup-close', '\u00d7');
      x.setAttribute('type', 'button');
      x.setAttribute('aria-label', '关闭');
      x.addEventListener('click', close);
      box.appendChild(x);
      mask.addEventListener('click', function (ev) { if (ev.target === mask) { close(); } });
      document.addEventListener('keydown', function (ev) {
        if (ev.key === 'Escape' && mask.parentNode) { close(); }
      });
    }

    if (a.image && K.adSafeUrl(a.image)) {
      var img = h('img', 'ad-popup-img');
      img.src = K.adSafeUrl(a.image);
      img.alt = a.title || '';
      box.appendChild(img);
    }
    if (a.title) box.appendChild(h('h3', 'ad-popup-title', a.title));

    var body = h('div', 'ad-md');
    body.innerHTML = K.mdToHtml(a.content);       // 已经过转义 + 白名单
    box.appendChild(body);

    var act = h('div', 'ad-popup-act');
    var link = K.adSafeUrl(a.link || '');
    if (link) {
      var btn = h('a', 'btn btn-primary btn-sm', a.link_text || '查看详情');
      btn.href = link;
      btn.target = '_blank';
      btn.rel = 'noopener noreferrer nofollow';
      btn.addEventListener('click', close);
      act.appendChild(btn);
    }
    if (a.closable) {
      var cl = h('button', 'btn btn-ghost btn-sm', '关闭');
      cl.setAttribute('type', 'button');
      cl.addEventListener('click', close);
      act.appendChild(cl);
    }
    if (act.childNodes.length) box.appendChild(act);

    mask.appendChild(box);
    document.body.appendChild(mask);
  }

  /* ---------------- 启动 ---------------- */
  function boot() {
    fetch('/api/v1/site', { headers: { Accept: 'application/json' }, credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        var list = K.adsFilter((d && d.ads) || []);
        var main = document.getElementById('main');
        if (main && main.parentNode) {
          list.filter(function (a) { return a.mode === 'marquee' && a.position === 'top'; })
            .reverse().forEach(function (a) { main.parentNode.insertBefore(buildMarquee(a), main); });
          list.filter(function (a) { return a.mode === 'marquee' && a.position === 'bottom'; })
            .forEach(function (a) { main.parentNode.insertBefore(buildMarquee(a), main.nextSibling); });
          measure();
          global.addEventListener('resize', measure);
        }
        queue = list.filter(function (a) {
          return a.mode === 'popup' && !K.adsAlreadySeen(a);
        });
        showNext();
      })
      .catch(function () { /* 广告失败不该影响页面 */ });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})(window);
