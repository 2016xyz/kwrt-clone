/* ==========================================================================
   广告位运行时 —— 后台可配置的「弹出 / 滚动」广告
   --------------------------------------------------------------------------
   职责：
     1) K.mdToHtml(str)  —— 安全地把一段 Markdown 渲染成 HTML。
        先整体 HTML 转义，再做极小集子集替换，因此 **不可能** 注入标签；
        链接地址经 safeUrl 白名单（http/https/mailto/tel 与站内相对路径），
        javascript:/data: 之类一律丢弃，只留纯文本。
     2) K.adsFilter(list, now) —— 按 enabled 与起止时间窗过滤出「当前应展示」的广告。
     3) K.adsPopupPending(list, now) —— 弹出类广告在扣除「已关闭」记录后的待展示队列。
   被首页(index.html) 与管理后台(admin.html 预览) 共用。
   ========================================================================== */
(function (global) {
  'use strict';
  const K = global.K = global.K || {};

  /* ---------------- HTML 转义 ---------------- */
  const ENT = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return ENT[c]; });
  }

  /* ---------------- 链接白名单 ----------------
   * 只放行 http/https/mailto/tel 与「单个斜杠开头」的站内绝对路径。
   * 特别注意 //evil.com （协议相对地址）必须拒绝——它以 / 开头却指向外站。 */
  function safeUrl(u) {
    u = String(u == null ? '' : u).trim().replace(/[\u0000-\u001F\u007F]/g, '');
    if (!u) return '';
    if (/^(?:https?:\/\/|mailto:|tel:)[^\s]*$/i.test(u)) return u;
    if (u.charAt(0) === '/' && u.charAt(1) !== '/' && u.charAt(1) !== '\\') return u;
    return '';
  }

  /* ---------------- 行内 Markdown ----------------
   * 输入必须已是「转义后」的文本。 */
  function inline(s) {
    // URL 允许一层嵌套括号（如 https://en.wikipedia.org/wiki/Foo_(bar)）
    const URLP = '(?:[^()\\s]|\\([^()]*\\))+';
    // 图片 ![alt](url) —— 必须先于链接处理
    s = s.replace(new RegExp('!\\[([^\\]]*)\\]\\((' + URLP + ')(?:\\s+&quot;([^&]*)&quot;)?\\)', 'g'),
      function (m, alt, url) {
        const u = safeUrl(url);
        return u ? '<img class="ad-inline-img" src="' + u + '" alt="' + alt + '" loading="lazy">' : alt;
      });
    // 链接 [text](url)
    s = s.replace(new RegExp('\\[([^\\]]+)\\]\\((' + URLP + ')(?:\\s+&quot;([^&]*)&quot;)?\\)', 'g'),
      function (m, txt, url) {
        const u = safeUrl(url);
        return u
          ? '<a href="' + u + '" target="_blank" rel="noopener noreferrer nofollow">' + txt + '</a>'
          : txt;
      });
    // 行内代码 —— 先于加粗，避免 `**` 被吃掉
    s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
    // 加粗
    s = s.replace(/\*\*([^\n]+?)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/__([^\n]+?)__/g, '<strong>$1</strong>');
    // 删除线
    s = s.replace(/~~([^\n]+?)~~/g, '<del>$1</del>');
    // 斜体（* 与 _ 各一份；要求两侧非同类字符）
    s = s.replace(/(^|[^*\w])\*([^*\n]+?)\*(?![*\w])/g, '$1<em>$2</em>');
    s = s.replace(/(^|[^_\w])_([^_\n]+?)_(?![_\w])/g, '$1<em>$2</em>');
    return s;
  }

  const RE_BLANK = /^\s*$/;
  const RE_HR = /^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/;
  const RE_H = /^\s*(#{1,6})\s+(.*)$/;
  const RE_QUOTE = /^\s*>\s?/;
  const RE_UL = /^\s*[-*+]\s+/;
  const RE_OL = /^\s*\d+[.)]\s+/;

  /* ---------------- 块级 Markdown ---------------- */
  function mdToHtml(md) {
    if (md == null) return '';
    const lines = String(md).replace(/\r\n?/g, '\n').split('\n');
    const out = [];
    let i = 0;
    while (i < lines.length) {
      const l = lines[i];
      if (RE_BLANK.test(l)) { i++; continue; }
      if (RE_HR.test(l)) { out.push('<hr>'); i++; continue; }

      let m = l.match(RE_H);
      if (m) {
        const n = m[1].length;
        out.push('<h' + n + ' class="ad-h">' + inline(escapeHtml(m[2])) + '</h' + n + '>');
        i++; continue;
      }

      if (RE_QUOTE.test(l)) {
        const buf = [];
        while (i < lines.length && RE_QUOTE.test(lines[i])) {
          buf.push(lines[i].replace(RE_QUOTE, '')); i++;
        }
        out.push('<blockquote>' + inline(escapeHtml(buf.join('\n'))).replace(/\n/g, '<br>') +
                 '</blockquote>');
        continue;
      }

      if (RE_UL.test(l)) {
        const buf = [];
        while (i < lines.length && RE_UL.test(lines[i])) {
          buf.push(lines[i].replace(RE_UL, '')); i++;
        }
        out.push('<ul>' + buf.map(function (x) {
          return '<li>' + inline(escapeHtml(x)) + '</li>';
        }).join('') + '</ul>');
        continue;
      }

      if (RE_OL.test(l)) {
        const buf = [];
        while (i < lines.length && RE_OL.test(lines[i])) {
          buf.push(lines[i].replace(RE_OL, '')); i++;
        }
        out.push('<ol>' + buf.map(function (x) {
          return '<li>' + inline(escapeHtml(x)) + '</li>';
        }).join('') + '</ol>');
        continue;
      }

      // 段落：吃到空行 / 下个块级起始为止
      const buf = [l];
      i++;
      while (i < lines.length && !RE_BLANK.test(lines[i]) && !RE_H.test(lines[i]) &&
             !RE_QUOTE.test(lines[i]) && !RE_UL.test(lines[i]) && !RE_OL.test(lines[i]) &&
             !RE_HR.test(lines[i])) {
        buf.push(lines[i]); i++;
      }
      out.push('<p>' + inline(escapeHtml(buf.join('\n'))).replace(/\n/g, '<br>') + '</p>');
    }
    return out.join('');
  }

  /* ---------------- 时间窗 ----------------
   * 支持 "2026-01-01" 或 "2026-01-01 08:30"。只写日期时：
   *   start 取当日零点，end 取当日 23:59:59.999（含当天）。 */
  function parseBound(v, isEnd) {
    if (!v) return null;
    const s = String(v).trim();
    if (!s) return null;
    let t = Date.parse(s);
    if (isNaN(t)) return null;
    if (isEnd && s.length <= 10) t += 86399999;   // 纯日期 → 含当天
    return t;
  }

  function adsFilter(list, now) {
    now = now || Date.now();
    if (!Array.isArray(list)) return [];
    return list.filter(function (a) {
      if (!a || typeof a !== 'object') return false;
      if (a.enabled === false) return false;
      if (!String(a.content || '').trim() && !String(a.image || '').trim() &&
          !String(a.title || '').trim()) return false;      // 全空的广告不展示
      const st = parseBound(a.start, false);
      const en = parseBound(a.end, true);
      if (st !== null && now < st) return false;
      if (en !== null && now > en) return false;
      return true;
    }).map(function (a) {
      return {
        id: String(a.id || ''), enabled: a.enabled !== false,
        mode: (a.mode === 'marquee' ? 'marquee' : 'popup'),
        title: String(a.title || ''), content: String(a.content || ''),
        image: String(a.image || ''), link: String(a.link || ''),
        link_text: String(a.link_text || ''),
        closable: a.closable !== false,
        delay: Math.max(0, Math.min(120, parseInt(a.delay, 10) || 0)),
        frequency: (['session', 'always', 'once'].indexOf(a.frequency) >= 0 ? a.frequency : 'session'),
        speed: Math.max(10, Math.min(400, parseInt(a.speed, 10) || 60)),
        position: (a.position === 'bottom' ? 'bottom' : 'top'),
        bg: String(a.bg || ''), color: String(a.color || ''),
      };
    });
  }

  /* ---------------- 弹出频率记忆 ---------------- */
  function seenKey(id) { return 'kwrt.ad.seen.' + id; }
  function alreadySeen(ad) {
    if (ad.frequency === 'always') return false;
    try {
      if (ad.frequency === 'once') return !!localStorage.getItem(seenKey(ad.id));
      return !!sessionStorage.getItem(seenKey(ad.id));       // session（默认）
    } catch (e) { return false; }
  }
  function markSeen(ad) {
    if (ad.frequency === 'always') return;
    try {
      if (ad.frequency === 'once') localStorage.setItem(seenKey(ad.id), '1');
      else sessionStorage.setItem(seenKey(ad.id), '1');
    } catch (e) { /* 隐私模式忽略 */ }
  }

  global.K.mdToHtml = mdToHtml;
  global.K.adEscapeHtml = escapeHtml;
  global.K.adSafeUrl = safeUrl;
  global.K.adsFilter = adsFilter;
  global.K.adsAlreadySeen = alreadySeen;
  global.K.adsMarkSeen = markSeen;
})(window);
