// -*- coding: utf-8 -*-
/* 版式译页 v2：把译文按「原段落行数 / 行距」填回原页。
 *
 * 为什么单独成模块：
 *   · 旧实现在 reader.js 里做固定 line-height:1.22 + 9 次二分，译文短了就贴在
 *     块顶部，页面上留下大片空白；长了一点点就被 overflow:hidden 裁掉；
 *   · 版式计算是纯几何 + 文字测量，抽出来可以用 node 直接跑单测。
 *
 * 对外接口（window.RDTransPage）：
 *   render(layer, opts)   在页面上铺一层译文（DOM + 自适应字号）
 *   fit(...)              纯函数：给定框宽高 / 目标行数，求字号与行距
 *   wrap(...)             纯函数：按中西混排规则折行
 *   coverPieces(...)      纯函数：矩形差集（盖住原文时不盖住图表）
 *   normalize(text, b)    清掉译文里的非法占位符、补回标题编号
 */
(function (global) {
  'use strict';

  var LINE_MIN = 1.05;      // 行距 / 字号 的下限（再低就压字）
  var LINE_MAX = 1.9;       // 上限（避免「两行撑满整段」那种夸张行距）
  var GROW_MAX = 1.16;      // 相对原字号最多放大 16%（中文比英文略需要空间）
  var FONT_MIN = 4.2;       // 绝对下限（css px）
  var DEFAULT_STACK = '"MiSans", "Microsoft YaHei", "PingFang SC", sans-serif';

  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

  /* ---------------- 译文清洗 ---------------- */

  function sanitize(text, math) {
    var s = String(text || '');
    if (s.indexOf('⟦') < 0) return s;
    s = s.replace(/⟦(F\d+)⟧/g, function (m, k) {
      return (math && math[k]) ? m : '';     // 模型凭空造的占位符直接丢掉
    });
    return s.replace(/[ \t]{2,}/g, ' ').trim();
  }

  function normalize(text, b) {
    var s = sanitize(text, (b || {}).math);
    if (!s) return '';
    if (b && b.kind === 'heading') {
      // 旧译文里编号常被模型当成公式吃掉：从原文开头补回来
      var m = /^(\d+(?:\.\d+)*\.?)\s+/.exec(String(b.text || ''));
      if (m) {
        var num = m[1];
        var re = new RegExp('^' + num.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '(?:\\s|$)');
        if (!re.test(s)) s = (num + ' ' + s.replace(/^\s+/, '')).trim();
      }
    }
    return s;
  }

  /* ---------------- 文字测量与折行 ---------------- */

  function makeMeasurer(stack) {
    var ctx = document.createElement('canvas').getContext('2d');
    var cache = {};
    return function (text, px) {
      var key = px.toFixed(2) + '|' + text;
      var hit = cache[key];
      if (hit !== undefined) return hit;
      ctx.font = px.toFixed(2) + 'px ' + stack;
      var w = ctx.measureText(text).width;
      // 缓存只留常用片段，避免长文把内存撑大
      if (text.length <= 24) cache[key] = w;
      return w;
    };
  }

  function isCJK(ch) {
    var c = ch.charCodeAt(0);
    return (c >= 0x2e80 && c <= 0x9fff) || (c >= 0xf900 && c <= 0xfaff) ||
           (c >= 0xff00 && c <= 0xffef) || (c >= 0x3000 && c <= 0x303f);
  }

  function isWordChar(ch) {
    return /[A-Za-z0-9@._\-\/+%'’]/.test(ch);
  }

  /* 贪心折行：中文按字、英文按词；返回 [{text, w}]。 */
  function wrapDetailed(text, width, px, measure) {
    var out = [];
    var line = '', lineW = 0;
    var i = 0, n = text.length;
    var push = function () { out.push({ text: line, w: lineW }); line = ''; lineW = 0; };
    while (i < n) {
      var ch = text[i];
      if (ch === '\n') { push(); i += 1; continue; }
      var unit, step;
      if (/\s/.test(ch)) {
        var j = i + 1;
        while (j < n && /\s/.test(text[j])) j += 1;
        unit = ' ';
        step = j - i;
      } else if (isWordChar(ch) && !isCJK(ch)) {
        var k = i + 1;
        while (k < n && isWordChar(text[k]) && !isCJK(text[k])) k += 1;
        unit = text.slice(i, k);
        step = k - i;
      } else {
        unit = ch;
        step = 1;
      }
      var uw = measure(unit, px);
      if (lineW + uw > width && line) {
        if (unit === ' ') {                 // 行尾空格直接丢掉
          i += step;
          push();
          continue;
        }
        push();
        line = unit;
        lineW = uw;
      } else {
        line += unit;
        lineW += uw;
      }
      i += step;
    }
    if (line) push();
    return out;
  }

  function wrap(text, width, px, measure) {
    return wrapDetailed(text, width, px, measure).map(function (x) { return x.text; });
  }

  /* ---------------- 字号 / 行距 ---------------- */

  /* 目标：行数尽量等于原段行数；行距撑满原框（= 原段行距），
     放不下时先压行距、再降字号。返回 px。 */
  function fit(text, boxW, boxH, targetLines, basePx, minPx, maxPx, measure) {
    var inner = Math.max(6, boxW - 2);
    var target = Math.max(1, Number(targetLines) || 1);
    var minF = Math.max(FONT_MIN, Number(minPx) || FONT_MIN);
    var maxF = Math.max(Number(maxPx) || minF, minF);
    // 字号不能大到把「原段行数」挤掉：原段行距 = boxH / target，行距至少 1.05×字号。
    // 否则 6 行的段落在视觉上会被压成 5 行（行距小于字号，会叠字）。
    var cap = boxH / (target * LINE_MIN);
    if (cap >= minF) maxF = Math.min(maxF, cap);
    else maxF = minF;
    var count = function (f) { return wrapDetailed(text, inner, f, measure).length; };

    var best = maxF, lines = count(best);
    if (lines > target) {
      var lo = minF, hi = maxF;
      for (var i = 0; i < 10; i++) {
        var mid = (lo + hi) / 2;
        var c = count(mid);
        if (c <= target) { lo = mid; best = mid; lines = c; }
        else { hi = mid; }
      }
      if (count(minF) > target) { best = minF; lines = count(minF); }
    }

    var lh = clamp(boxH / Math.max(lines, target), best * LINE_MIN, best * LINE_MAX);
    for (var k = 0; k < 4 && lines * lh > boxH + 0.75 && best > minF; k++) {
      best = Math.max(minF, best * 0.93);
      lines = count(best);
      lh = clamp(boxH / Math.max(lines, target), best * LINE_MIN, best * LINE_MAX);
    }
    return {
      fontSize: +best.toFixed(2),
      lineHeight: +lh.toFixed(2),
      lines: lines,
      overflow: lines * lh > boxH + 0.75,
    };
  }

  /* ---------------- 遮盖与取色 ---------------- */

  function overlap(a, b) {
    return Math.min(a[2], b[2]) - Math.max(a[0], b[0]) > 0.5 &&
           Math.min(a[3], b[3]) - Math.max(a[1], b[1]) > 0.5;
  }

  /* 原文遮盖 = 段落矩形 -（公式 / 表格 / 图片矩形），避免把图表一起盖掉。 */
  function coverPieces(rect, obstacles) {
    var pieces = [rect.slice()];
    (obstacles || []).forEach(function (o) {
      var next = [];
      pieces.forEach(function (p) {
        if (!overlap(p, o)) { next.push(p); return; }
        if (o[1] > p[1]) next.push([p[0], p[1], p[2], Math.min(o[1], p[3])]);
        if (o[3] < p[3]) next.push([p[0], Math.max(o[3], p[1]), p[2], p[3]]);
        var top = Math.max(p[1], o[1]), bot = Math.min(p[3], o[3]);
        if (o[0] > p[0] && bot > top) next.push([p[0], top, Math.min(o[0], p[2]), bot]);
        if (o[2] < p[2] && bot > top) next.push([Math.max(o[2], p[0]), top, p[2], bot]);
      });
      pieces = next.filter(function (p) { return p[2] - p[0] > 0.5 && p[3] - p[1] > 0.5; });
    });
    return pieces;
  }

  /* 取段落四角最亮的像素当底色：文字块的角多半还是背景，比固定白底更贴纸色。 */
  function sampleColor(cv, rect, pw, ph) {
    try {
      var ctx = cv.getContext('2d', { willReadFrequently: true });
      var sx = clamp(Math.floor((rect[0] + 1) / pw * cv.width), 0, cv.width - 1);
      var sy = clamp(Math.floor((rect[1] + 1) / ph * cv.height), 0, cv.height - 1);
      var ex = clamp(Math.ceil((rect[2] - 1) / pw * cv.width), 0, cv.width - 1);
      var ey = clamp(Math.ceil((rect[3] - 1) / ph * cv.height), 0, cv.height - 1);
      var pts = [[sx, sy], [ex, sy], [sx, ey], [ex, ey]];
      var best = null, lum = -1;
      for (var i = 0; i < pts.length; i++) {
        var d = ctx.getImageData(pts[i][0], pts[i][1], 1, 1).data;
        var l = d[0] + d[1] + d[2];
        if (l > lum) { lum = l; best = d; }
      }
      if (best) return 'rgb(' + best[0] + ',' + best[1] + ',' + best[2] + ')';
    } catch (e) { /* 像素读不到就用白底 */ }
    return '#fff';
  }

  /* 允许向下扩展的边界：同页、横向有重叠、起点在当前块下方 */
  function bottomLimit(rect, pageRects) {
    var limit = 1e9;
    (pageRects || []).forEach(function (r) {
      if (!r || r.length !== 4) return;
      if (Math.min(rect[2], r[2]) - Math.max(rect[0], r[0]) <= 1) return;
      if (r[1] >= rect[3] - 0.5 && r[1] < limit) limit = r[1];
    });
    return limit;
  }

  function overlapArea(a, b) {
    var w = Math.min(a[2], b[2]) - Math.max(a[0], b[0]);
    var h = Math.min(a[3], b[3]) - Math.max(a[1], b[1]);
    return (w > 0 && h > 0) ? w * h : 0;
  }

  /* ---------------- DOM 渲染 ---------------- */

  function pct(v, total) { return (v / Math.max(1, total) * 100) + '%'; }

  function applyFont(el, lay) {
    el.style.fontSize = lay.fontSize.toFixed(2) + 'px';
    el.style.lineHeight = lay.lineHeight.toFixed(2) + 'px';
  }

  function render(layer, opts) {
    if (!layer) return;
    layer.innerHTML = '';
    var o = opts || {};
    var items = o.items || [];
    var size = o.size || { w: 595, h: 842 };
    var cv = o.canvas;
    var pxW = (cv && cv.clientWidth) || layer.clientWidth || 1;
    // 版面还没布局（标题栏隐藏 / 宽度 0）：先不画，等 resize 或切模式后重排，
    // 否则 scale 会被算成 1px/页宽，整页文字缩成一条线。
    if (pxW < 50) return {};
    var scale = pxW / Math.max(1, size.w);
    var measure = makeMeasurer(o.fontStack || DEFAULT_STACK);
    var obstacles = o.obstacles || [];
    var pageRects = o.pageRects || obstacles;
    var made = [];
    var els = {};

    items.forEach(function (it) {
      var b = it.block, rect = it.rect;
      if (!b || !rect || rect.length !== 4) return;
      var bw = rect[2] - rect[0], bh = rect[3] - rect[1];
      // 图内小标签 / 竖排碎片（arXiv 印章、坐标轴字母）：格子太窄，翻进去只会变成竖排乱码
      if (bw < 12 || bh < 5) return;
      // 与公式 / 表格 / 图片大面积重叠：多半是图里的文字标签，保留原样
      var solidArea = 0;
      obstacles.forEach(function (o) { solidArea += overlapArea(rect, o); });
      if (solidArea > bw * bh * 0.5) return;
      var text = normalize(it.text, b);
      if (!text) return;
      // 模型原样回显（没翻）：保留原页，避免同文不同字体看起来像重影
      if (text === String(b.text || '').trim()) return;

      var box = document.createElement('div');
      box.className = 'rd-tblock';
      box.dataset.id = b.id;
      box.dataset.kind = b.kind || 'text';
      if (b.role) box.dataset.role = b.role;
      box.style.left = pct(rect[0], size.w);
      box.style.top = pct(rect[1], size.h);
      box.style.width = pct(bw, size.w);
      box.style.height = pct(bh, size.h);

      coverPieces(rect, obstacles).forEach(function (p) {
        var cover = document.createElement('div');
        cover.className = 'rd-tcover';
        // 用 px 定位而不是百分比：单行块会向下加高 textbox，百分比会跟着被拉伸
        cover.style.left = ((p[0] - rect[0]) * scale) + 'px';
        cover.style.top = ((p[1] - rect[1]) * scale) + 'px';
        cover.style.width = ((p[2] - p[0]) * scale) + 'px';
        cover.style.height = ((p[3] - p[1]) * scale) + 'px';
        cover.style.background = sampleColor(cv, p, size.w, size.h);
        box.appendChild(cover);
      });

      var t = document.createElement('div');
      t.className = 'rd-ttext';
      t.innerHTML = o.formatInline ? o.formatInline(text, b.math || {})
                                   : escapeHtml(text);
      if (b.kind === 'heading') {
        t.style.fontWeight = '700';
        if ((b.level || 2) <= 1) t.style.textAlign = 'center';
      }
      box.appendChild(t);

      box.title = '点击可切换显示原文';
      box.onclick = function () {
        var sel = global.getSelection && global.getSelection();
        if (sel && !sel.isCollapsed) return;      // 正在划词，不要误触
        this.dataset.orig = this.dataset.orig === '1' ? '0' : '1';
      };
      layer.appendChild(box);
      els[b.id] = box;
      // 量宽用的文本：占位符换成公式源码（KaTeX 的真实宽度更接近它，
      // 否则「⟦F1⟧」四个字符会把行算得太宽、字号被压得过小）
      var meas = text.replace(/⟦(F\d+)⟧/g, function (m, k) {
        return (b.math && b.math[k]) || '';
      });
      made.push({ box: box, textEl: t, block: b, rect: rect, raw: meas });
    });

    global.requestAnimationFrame(function () {
      made.forEach(function (m) { layoutOne(m, scale, size, measure, pageRects); });
      // 排版完成后再回调：批注角标/高亮需要按最终位置贴
      if (typeof o.onReady === 'function') {
        try { o.onReady(els); } catch (e) { /* 批注渲染失败不影响正文 */ }
      }
    });
    return els;
  }

  function layoutOne(m, scale, size, measure, pageRects) {
    var b = m.block, rect = m.rect;
    // 原文字号不能直接用 b.size：解析拿到的是字形高度（≈0.45em），
    // 真实字号要从「段高 / 行数 = 行距」反推（论文正文行距 ≈ 1.11 倍字号）。
    // 单行块（标题 / 图注）没有行距可用，退回 size × 1.85 的经验值。
    var target = Number(b.lines) ||
      Math.max(1, Math.round((rect[3] - rect[1]) / Math.max(1, Number(b.size) || 10) / 1.18));
    var leadPt = (rect[3] - rect[1]) / Math.max(1, target);
    // 单行块（标题 / 图注 / 表格标签）没有行距可用：字形高度 ≈ 0.45em，标题再放大一点
    var mult = b.kind === 'heading' ? 2.2 : 1.85;
    var basePx = Math.max(leadPt / 1.15, (Number(b.size) || 10) * mult) * scale;
    var maxPx = basePx * GROW_MAX;
    var minPx = Math.max(FONT_MIN, basePx * 0.62);
    var boxW = m.box.clientWidth || (rect[2] - rect[0]) * scale;
    var boxH = m.box.clientHeight || (rect[3] - rect[1]) * scale;
    // 单行块的矩形只是「字形墨迹」高度，装不下一个正常行框：
    // 向下借一点空隙（到下一个块为止），否则会被压成比原文还小的字。
    if (target <= 1) {
      var want = basePx * 1.22;
      if (boxH < want) {
        var lim0 = bottomLimit(rect, pageRects);
        var avail = lim0 < 1e9 ? (lim0 - rect[1]) * scale : want;
        boxH = Math.max(boxH, Math.min(want, avail));
        m.box.style.height = pct(boxH / scale, size.h);
      }
    }
    var plain = m.raw || '';

    var lay = fit(plain, boxW, boxH, target, basePx, minPx, maxPx, measure);
    var grew = false;
    if (lay.overflow) {
      // 向下扩展到下一个块之前，再算一次（允许更多行）
      var limit = bottomLimit(rect, pageRects);
      if (limit > rect[3] + 1 && limit < 1e9) {
        var newH = (limit - rect[1]) * scale;
        var lead = boxH / Math.max(1, target);
        var t2 = Math.max(target, Math.round(newH / Math.max(1, lead)));
        var lay2 = fit(plain, boxW, newH, t2, basePx, minPx, maxPx, measure);
        if (!lay2.overflow) {
          m.box.style.height = pct(limit - rect[1], size.h);
          boxH = newH;
          lay = lay2;
          grew = true;
        }
      }
    }
    applyFont(m.textEl, lay);

    // DOM 兜底：KaTeX / 字体差异会让真实排版比估算宽一点点，真溢出就再缩
    var over = function () {
      return m.textEl.scrollHeight > boxH + 1 ||
             m.textEl.scrollWidth > m.textEl.clientWidth + 1;
    };
    var guard = 0;
    while (over() && guard < 6) {
      guard += 1;
      lay.fontSize = Math.max(FONT_MIN, lay.fontSize * 0.94);
      lay.lineHeight = Math.max(lay.fontSize * LINE_MIN,
                                Math.min(lay.lineHeight * 0.97, lay.fontSize * LINE_MAX));
      applyFont(m.textEl, lay);
    }
    if (over()) {
      m.box.dataset.overflow = '1';
      m.box.title = '译文比原文字框长，已按最小字号显示；可放大页面或切到「双语」阅读流看完整译文';
    }
    if (grew) m.box.dataset.expanded = '1';
  }

  function escapeHtml(s) {
    return String(s || '').replace(/[&<>"']/g, function (c) {
      return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c];
    });
  }

  global.RDTransPage = {
    render: render,
    fit: fit,
    wrap: wrap,
    coverPieces: coverPieces,
    normalize: normalize,
    sanitize: sanitize,
  };
  if (typeof module !== 'undefined' && module.exports) module.exports = global.RDTransPage;
})(typeof window !== 'undefined' ? window : globalThis);
