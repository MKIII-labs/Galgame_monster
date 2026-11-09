// -*- coding: utf-8 -*-
/* 沉浸式 PDF 阅读器：左原页（pdf.js）+ 右双语阅读流。
 *
 * 不放进 Monaco：PDF 是二进制，编辑器打不开；这里用全屏覆盖层，
 * 原页只做「对照」，阅读流负责可读性（分段、双语、图表裁图）。
 * 翻译走 /api/reader/*，逐段 SSE 回填，结果落服务端缓存。
 */
(function () {
  'use strict';

  var RD = {
    doc: null, rel: '', pdf: null, pdfjs: null,
    pages: {},            // page -> {el, canvas, textEl, hlEl, rendered, rendering, task, page, tc, index, scale}
    tpages: {},           // 版式译页：page -> {el, canvas, layer, rendered, rendering, page, ...}
    blockEls: {},         // block.id -> 阅读流元素
    blockById: {},        // block.id -> 块数据（译页划词/批注也用它取原文上下文）
    tblockEls: {},        // pageNo -> {block.id -> 译页覆盖层元素}（批注/跳转用）
    crops: {},            // crop key -> objectURL
    cur: 1, zoom: 1, zoomMode: 'width', mode: 'pdf',
    fileType: 'pdf',      // pdf | docx | pptx（后两者没有原页图像）
    transOn: false,       // 是否已启用分栏 + 翻译（默认只看 PDF 原页）
    job: null, stream: null, streaming: false,
    pending: new Set(),   // 还没翻译的页（任务跑完后再接着发）
    requested: new Set(), // 本会话已经请求过的页
    totalBlocks: 0, doneBlocks: 0, skipped: 0,
    translations: {},      // block.id -> 译文（导出译文 PDF 用）
    docMeta: null,         // 文献条目（标题 / 作者 / 年份 / DOI）
    live: { on: false, page: 0, blockId: '', quote: '', rects: [] },   // 划词联动（临时）
    thumbs: {}, thumbOrder: [], thumbsOn: false,
    history: [],
    leftRaf: 0,
    opened: false,
  };

  function $(sel, root) { return (root || document).querySelector(sel); }
  function sessionId() {
    // state 是 core.js 里的顶层 const：直接按变量名读，不能写 window.state（那是 undefined）
    try { return (state.sessions && state.sessions.current) || ''; } catch (e) { return ''; }
  }
  function authHeaders() {
    return { 'Content-Type': 'application/json', 'X-Auth-Token': TOKEN };
  }

  /* 文本归一化：去掉空白 / 零宽 / 软连字符并转小写。
     左右两栏、查找、联动全部用同一套规则，避免「连字符 / 换行」导致匹配失败。 */
  function normStr(s) {
    return String(s == null ? '' : s)
      .replace(/[\s\u200b-\u200d\ufeff\u00ad]+/g, '')
      .toLowerCase();
  }

  /* ------------------------------------------------------------ 外壳 */

  function ensureRoot() {
    var root = document.getElementById('rd-root');
    if (root) return root;
    root = document.createElement('div');
    root.id = 'rd-root';
    root.className = 'rd-root hidden';
    root.innerHTML =
      '<div class="rd-bar">' +
        '<button class="rd-icon" data-act="back" title="返回编辑器（Esc）">←</button>' +
        '<div class="rd-meta">' +
          '<div class="rd-name"></div>' +
          '<div class="rd-sub">' +
            '<span class="rd-pagewrap"><input class="rd-pageinput" inputmode="numeric" value="1" title="页码：输入后回车（Ctrl+G 聚焦）"><span class="rd-pagetotal">/ 1</span></span>' +
            '<span class="rd-sep">·</span>' +
            '<span class="rd-stat"></span>' +
          '</div>' +
        '</div>' +
        '<div class="rd-modes">' +
          '<button data-mode="pdf" title="只看 PDF 原页，不翻译（Alt+0）">原页</button>' +
          '<button data-mode="orig" title="只看原文（Alt+1）">原文</button>' +
          '<button data-mode="dual" title="原文 + 译文（Alt+2）">双语</button>' +
          '<button data-mode="trans" title="只看译文（Alt+3）">译文</button>' +
          '<button data-mode="page" title="按原页版式显示译文（图像 / 表格 / 公式原位保留）">译页</button>' +
          '<button data-mode="page-dual" title="左原页 / 右译页，按页对照">版式对照</button>' +
        '</div>' +
        '<div class="rd-actions">' +
          '<button class="rd-icon" data-act="prev" title="上一页（←）">‹</button>' +
          '<button class="rd-icon" data-act="next" title="下一页（→）">›</button>' +
          '<button class="rd-btn" data-act="page" title="翻译当前页">译本页</button>' +
          '<button class="rd-btn primary" data-act="all" title="翻译整篇（已译过的会跳过）">译全文</button>' +
          '<button class="rd-btn" data-act="annos" title="本文批注（点击跳转）">批注<b class="rd-anno-n"></b></button>' +
          '<button class="rd-icon" data-act="find" title="全文查找（Ctrl+F）">⌕</button>' +
          '<button class="rd-icon" data-act="thumbs" title="缩略图">▤</button>' +
          '<button class="rd-icon" data-act="full" title="全屏 / 退出全屏">⤢</button>' +
          '<button class="rd-icon" data-act="settings" title="阅读设置">Aa</button>' +
        '</div>' +
      '</div>' +
      '<div class="rd-progress"><i></i></div>' +
      '<div class="rd-find hidden">' +
        '<input class="rd-find-input" placeholder="在全文里查找…（Enter 下一个，Shift+Enter 上一个）">' +
        '<span class="rd-find-stat"></span>' +
        '<button class="rd-find-prev" title="上一个（Shift+Enter）">↑</button>' +
        '<button class="rd-find-next" title="下一个（Enter）">↓</button>' +
        '<button class="rd-find-x" title="关闭（Esc）">✕</button>' +
        '<div class="rd-find-list"></div>' +
      '</div>' +
      '<div class="rd-settings hidden">' +
        '<div class="rd-set-row"><span>正文字号</span>' +
          '<button data-fs="-">A−</button><b class="rd-fs-val">16</b><button data-fs="+">A+</button></div>' +
        '<div class="rd-set-row"><span>行距</span>' +
          '<button data-lh="1.6">紧</button><button data-lh="1.85">中</button>' +
          '<button data-lh="2.1">松</button></div>' +
        '<div class="rd-set-row"><span>原页缩放</span>' +
          '<button data-pz="-">−</button><b class="rd-pz-val">100%</b><button data-pz="+">+</button></div>' +
        '<div class="rd-set-row"><span>缩放适配</span>' +
          '<button data-zm="width" class="on">页宽</button>' +
          '<button data-zm="page">页高</button>' +
          '<button data-zm="actual">实际</button></div>' +
        '<div class="rd-set-row"><span>原页对照</span>' +
          '<button data-left="toggle" class="rd-left-toggle">隐藏</button></div>' +
        '<div class="rd-set-row"><span>公式识别</span>' +
          '<button class="rd-formula-toggle" title="用视觉模型把公式转成 LaTeX">开</button></div>' +
        '<div class="rd-set-row"><span>翻译</span>' +
          '<button class="rd-retry-page" title="忽略缓存，重新翻译当前页">重译本页</button>' +
          '<button class="rd-retry-fail" title="只补译没有译文的段落，不动已译内容">重试失败段</button>' +
          '<button class="rd-gloss-open" title="查看 / 编辑本文术语表">术语表</button></div>' +
        '<div class="rd-gloss hidden">' +
          '<div class="rd-gloss-head"><span>术语表</span><b class="rd-gloss-n"></b>' +
            '<button class="rd-gloss-regen" title="从全文重新抽取">重新生成</button>' +
            '<button class="rd-gloss-save">保存</button></div>' +
          '<div class="rd-gloss-list"></div>' +
        '</div>' +
        '<div class="rd-set-row rd-exp-row"><span>重排导出</span>' +
          '<button data-exp="mono" title="纯译文重排：字体嵌入、标题书签、页码">仅译文 PDF</button>' +
          '<button data-exp="dual" title="原文 + 译文对照，适合精读">双语 PDF</button>' +
          '<button data-exp="md" title="Markdown，公式与图表以图片内嵌">Markdown</button></div>' +
        '<div class="rd-set-row rd-exp-row"><span>版式导出</span>' +
          '<button data-exp="layout-mono" title="保留原页版式：图表 / 公式 / 表格不动，译文原位覆盖">版式译文 PDF</button>' +
          '<button data-exp="layout-dual" title="同页左右并排：左原页 / 右译页">版式双语 PDF</button></div>' +
      '</div>' +
      '<div class="rd-body">' +
        '<div class="rd-thumbs hidden"><div class="rd-thumbs-inner"></div></div>' +
        '<div class="rd-left"><div class="rd-pages"></div></div>' +
        '<div class="rd-split" title="拖动调整宽度"></div>' +
        '<div class="rd-right"><div class="rd-tpages hidden"></div><div class="rd-flow"></div>' +
        '<div class="rd-loading">正在解析 PDF…</div></div>' +
      '</div>' +
      '<div class="rd-anno-drawer hidden">' +
        '<div class="rd-anno-drawer-head"><span>本文批注</span>' +
          '<span class="rd-anno-drawer-acts">' +
            '<button class="rd-anno-meta" title="文献信息（标题 / 作者 / 年份 / DOI）">文献</button>' +
            '<button class="rd-anno-export" title="导出批注（Markdown）">导出</button>' +
            '<button class="rd-anno-drawer-x" title="关闭">✕</button>' +
          '</span></div>' +
        '<div class="rd-anno-drawer-filters">' +
          '<button data-f="all" class="on">全部</button>' +
          '<button data-f="highlight">高亮</button>' +
          '<button data-f="qa">问答</button>' +
          '<button data-f="excerpt">摘录</button>' +
          '<button data-f="note">批注</button>' +
        '</div>' +
        '<div class="rd-anno-list"></div>' +
      '</div>' +
      '<div class="rd-meta-modal hidden">' +
        '<div class="rd-meta-card">' +
          '<div class="rd-meta-head"><span>文献信息</span>' +
            '<button class="rd-meta-x" title="关闭">✕</button></div>' +
          '<label class="rd-meta-field">标题<input data-m="title"></label>' +
          '<label class="rd-meta-field">作者<input data-m="authors" placeholder="多位作者用逗号分隔"></label>' +
          '<div class="rd-meta-row">' +
            '<label class="rd-meta-field">年份<input data-m="year" inputmode="numeric"></label>' +
            '<label class="rd-meta-field">出处<input data-m="venue" placeholder="期刊 / 会议 / arXiv"></label>' +
          '</div>' +
          '<label class="rd-meta-field">DOI<input data-m="doi"></label>' +
          '<label class="rd-meta-field">标签<input data-m="tags" placeholder="逗号分隔，如：方法, 极值组合"></label>' +
          '<label class="rd-meta-field">摘要<textarea data-m="abstract" rows="3"></textarea></label>' +
          '<div class="rd-meta-acts">' +
            '<button data-meta-cancel>取消</button>' +
            '<button data-meta-save class="primary">保存</button>' +
          '</div>' +
        '</div>' +
      '</div>';
    // 停靠进主内容区：左侧应用侧栏（笔记 / 会话）保持可用；需要专注时再全屏
    var host = document.getElementById('main') || document.body;
    host.appendChild(root);
    bindFullscreenSync();
    try {
      if (localStorage.getItem('rd_fullscreen') === '1') {
        root.classList.add('rd-fullscreen');
        document.documentElement.classList.add('rd-immersive');   // 标题栏也一起藏掉
        if (window.gmWin && typeof window.gmWin.setFullScreen === 'function') {
          window.gmWin.setFullScreen(true);                        // 上次退出时在全屏，这次恢复
        }
      }
    } catch (e) { /* 忽略 */ }
    loadPrefs();
    wireShell(root);
    syncFullBtn(root);
    return root;
  }

  /* 阅读偏好：字号 / 行距 / 是否显示原页，记在本机 */
  var PREFS_KEY = 'rd_prefs_v2';
  var prefs = { fs: 16, lh: 1.85, left: true, formula: true, zoomMode: 'width' };

  function loadPrefs() {
    try {
      var v = JSON.parse(localStorage.getItem(PREFS_KEY) || '{}');
      if (v && typeof v === 'object') {
        if (v.fs) prefs.fs = Math.max(13, Math.min(24, Number(v.fs) || 16));
        if (v.lh) prefs.lh = Math.max(1.4, Math.min(2.4, Number(v.lh) || 1.85));
        prefs.left = v.left !== false;
        prefs.formula = v.formula !== false;
        if (v.zoomMode === 'width' || v.zoomMode === 'page' || v.zoomMode === 'actual') {
          prefs.zoomMode = v.zoomMode;
        }
      }
    } catch (e) { /* 忽略 */ }
    applyPrefs();
  }

  function savePrefs() {
    try { localStorage.setItem(PREFS_KEY, JSON.stringify(prefs)); } catch (e) { /* 忽略 */ }
  }

  function applyPrefs() {
    var root = document.getElementById('rd-root');
    if (!root) return;
    root.style.setProperty('--rd-fs', prefs.fs + 'px');
    root.style.setProperty('--rd-lh', String(prefs.lh));
    root.classList.toggle('rd-no-left', !prefs.left);
    var fsEl = $('.rd-fs-val', root);
    if (fsEl) fsEl.textContent = prefs.fs;
    var pzEl = $('.rd-pz-val', root);
    if (pzEl) pzEl.textContent = Math.round(RD.zoom * 100) + '%';
    var lbtn = $('.rd-left-toggle', root);
    if (lbtn) lbtn.textContent = prefs.left ? '隐藏' : '显示';
    var ft = $('.rd-formula-toggle', root);
    if (ft) ft.textContent = prefs.formula ? '开' : '关';
    Array.prototype.forEach.call(root.querySelectorAll('[data-zm]'), function (b) {
      b.classList.toggle('on', b.dataset.zm === RD.zoomMode);
    });
  }

  function bumpText(delta) {
    prefs.fs = Math.max(13, Math.min(24, prefs.fs + delta));
    applyPrefs();
    savePrefs();
  }

  function wireShell(root) {
    Array.prototype.forEach.call(root.querySelectorAll('[data-mode]'), function (btn) {
      btn.onclick = function () { setMode(btn.dataset.mode); };
    });
    Array.prototype.forEach.call(root.querySelectorAll('[data-act]'), function (btn) {
      btn.onclick = function () {
        var act = btn.dataset.act;
        if (act === 'back') close();
        else if (act === 'prev') goPage(RD.cur - 1);
        else if (act === 'next') goPage(RD.cur + 1);
        else if (act === 'page') {
          // 点翻译才进入分栏：首次点用上次的分栏模式，之后只是重译本页
          if (!RD.transOn) setMode(modeForTranslate());
          rdEnsurePages([RD.cur], true);
        } else if (act === 'all') {
          RD.allRequested = true;
          if (!RD.transOn) setMode(modeForTranslate());
          translate(null);
        } else if (act === 'settings') $('.rd-settings', root).classList.toggle('hidden');
        else if (act === 'annos') toggleAnnoDrawer();
        else if (act === 'find') toggleFind();
        else if (act === 'thumbs') toggleThumbs();
        else if (act === 'full') toggleFullscreen();
      };
    });
    Array.prototype.forEach.call(root.querySelectorAll('[data-fs]'), function (b) {
      b.onclick = function () { bumpText(b.dataset.fs === '+' ? 1 : -1); };
    });
    Array.prototype.forEach.call(root.querySelectorAll('[data-lh]'), function (b) {
      b.onclick = function () { prefs.lh = Number(b.dataset.lh); applyPrefs(); savePrefs(); };
    });
    Array.prototype.forEach.call(root.querySelectorAll('[data-zm]'), function (b) {
      b.onclick = function () { setZoomMode(b.dataset.zm); };
    });
    var pageInput = $('.rd-pageinput', root);
    if (pageInput) {
      pageInput.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') {
          e.preventDefault();
          var n = parseInt(pageInput.value, 10);
          if (n) goPage(n);
          pageInput.blur();
        } else if (e.key === 'Escape') {
          e.preventDefault();
          pageInput.value = String(RD.cur);
          pageInput.blur();
        }
      });
      pageInput.addEventListener('blur', function () { pageInput.value = String(RD.cur); });
    }
    var findInput = $('.rd-find-input', root);
    if (findInput) {
      findInput.addEventListener('input', function () { findSchedule(); });
      findInput.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') {
          e.preventDefault();
          findStep(e.shiftKey ? -1 : 1);
        } else if (e.key === 'Escape') {
          e.preventDefault();
          toggleFind(false);
        }
      });
    }
    Array.prototype.forEach.call(root.querySelectorAll('[data-pz]'), function (b) {
      b.onclick = function () {
        setZoom(RD.zoom * (b.dataset.pz === '+' ? 1.15 : 1 / 1.15));
        applyPrefs();
      };
    });
    var lt = $('.rd-left-toggle', root);
    if (lt) {
      lt.onclick = function () {
        prefs.left = !prefs.left;
        applyPrefs();
        savePrefs();
        setTimeout(rerenderVisible, 60);
      };
    }
    wireSplitter(root);
    wireSelection(root);
    var ft = $('.rd-formula-toggle', root);
    if (ft) {
      ft.onclick = function () {
        prefs.formula = !prefs.formula;
        applyPrefs();
        savePrefs();
        Array.prototype.forEach.call(document.querySelectorAll('.rd-fig.rd-eq'), function (fig) {
          if (!prefs.formula) {
            fig.classList.remove('ai');
            fig.dataset.ai = 'done';       // 已识别过，下次开启直接复用
            return;
          }
          if (fig.dataset.latex) renderLatex(fig, fig.dataset.latex);
          else if (fig.dataset.loaded) maybeFormula(fig.querySelector('img'));
        });
      };
    }
    Array.prototype.forEach.call(root.querySelectorAll('[data-exp]'), function (btn) {
      btn.onclick = function () { exportDoc(btn.dataset.exp); };
    });
    var retryBtn = $('.rd-retry-page', root);
    if (retryBtn) {
      retryBtn.onclick = function () {
        if (RD.streaming) { toast('正在翻译，稍后再试'); return; }
        translate([RD.cur], true);
      };
    }
    var retryFail = $('.rd-retry-fail', root);
    if (retryFail) {
      retryFail.onclick = function () {
        if (RD.streaming) { toast('正在翻译，稍后再试'); return; }
        translate([RD.cur], false, true);
      };
    }
    var glossBtn = $('.rd-gloss-open', root);
    if (glossBtn) {
      glossBtn.onclick = function () {
        var box = $('.rd-gloss', root);
        if (!box) return;
        var show = box.classList.contains('hidden');
        box.classList.toggle('hidden', !show);
        if (show) loadGlossary();
      };
    }
    var glossRegen = $('.rd-gloss-regen', root);
    if (glossRegen) glossRegen.onclick = function () { regenGlossary(glossRegen); };
    var glossSave = $('.rd-gloss-save', root);
    if (glossSave) glossSave.onclick = saveGlossary;
    // 顶栏按钮：点下去先清掉文本选区。否则浏览器会把「选中的文字」滚回视口，
    // 把 goPage 的滚动覆盖掉，看起来就是「点了没反应」。
    var bar = $('.rd-bar', root);
    bar.addEventListener('mousedown', function (e) {
      if (!e.target.closest('button')) return;
      e.preventDefault();
      try {
        var s = window.getSelection();
        if (s) s.removeAllRanges();
      } catch (err) { /* 忽略 */ }
    }, true);
    var drawer = $('.rd-anno-drawer', root);
    if (drawer) {
      $('.rd-anno-drawer-x', drawer).onclick = function () { toggleAnnoDrawer(false); };
      var metaBtn = $('.rd-anno-meta', drawer);
      if (metaBtn) metaBtn.onclick = showDocMeta;
      var expBtn = $('.rd-anno-export', drawer);
      if (expBtn) expBtn.onclick = exportAnnos;
      Array.prototype.forEach.call(drawer.querySelectorAll('[data-f]'), function (b) {
        b.onclick = function () {
          annoFilter = b.dataset.f || 'all';
          Array.prototype.forEach.call(drawer.querySelectorAll('[data-f]'), function (x) {
            x.classList.toggle('on', x === b);
          });
          renderAnnoDrawer();
        };
      });
    }
    var metaModal = $('.rd-meta-modal', root);
    if (metaModal) {
      $('.rd-meta-x', metaModal).onclick = function () { metaModal.classList.add('hidden'); };
      $('[data-meta-cancel]', metaModal).onclick = function () { metaModal.classList.add('hidden'); };
      $('[data-meta-save]', metaModal).onclick = saveDocMeta;
      metaModal.addEventListener('mousedown', function (e) {
        if (e.target === metaModal) metaModal.classList.add('hidden');
      });
    }
    // 真正滚动的是 .rd-right（.rd-flow 只有内容高度，自身不滚）
    $('.rd-right', root).addEventListener('scroll', onFlowScroll, { passive: true });
    $('.rd-left', root).addEventListener('scroll', onLeftScroll, { passive: true });
    // 用户手动滚左栏后 800ms 内不让右栏的页同步把它抢回去
    $('.rd-left', root).addEventListener('wheel', function () { RD.leftManualUntil = Date.now() + 800; }, { passive: true });
    $('.rd-left', root).addEventListener('touchmove', function () { RD.leftManualUntil = Date.now() + 800; }, { passive: true });
    document.addEventListener('keydown', onKey, true);
  }

  /* ---------- 选中片段：精准翻译 / 提问 ---------- */
  var sel = { text: '', ctx: '', blockId: '', side: 'src', page: 0, question: '', answer: '' };

  /* KaTeX 会把一条公式拆成 MathML + HTML 两份：直接 toString 会把公式读两遍，
     还会夹带零宽字符和噪声字形。这里沿真实 DOM 走一遍选区，遇到公式就整体
     换回原始 TeX —— 这样「从公式中间开始划」也能拿到完整公式。 */
  function katexTex(k) {
    var ann = k.querySelector ? k.querySelector('annotation[encoding="application/x-tex"]') : null;
    if (ann && ann.textContent) return ann.textContent;
    return (k.getAttribute && k.getAttribute('data-latex')) || '';
  }

  function cleanRangeText(range) {
    if (!range) return '';
    var root = range.commonAncestorContainer;
    if (root && root.nodeType !== 1) root = root.parentNode;
    if (!root) return '';
    var parts = [];
    var seen = [];
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
    var n;
    while ((n = walker.nextNode())) {
      if (!range.intersectsNode(n)) continue;
      var host = n.parentElement ? n.parentElement.closest('.katex') : null;
      if (host) {
        if (seen.indexOf(host) >= 0) continue;   // 一条公式只取一次
        seen.push(host);
        var tex = katexTex(host);
        if (tex) parts.push(' $' + tex + '$ ');
        continue;
      }
      var raw = n.nodeValue || '';
      // 选区两端的文本节点只取落在范围内的那一段
      if (range.startContainer === n || range.endContainer === n) {
        var s = range.startContainer === n ? range.startOffset : 0;
        var e = range.endContainer === n ? range.endOffset : raw.length;
        raw = raw.slice(s, e);
      }
      parts.push(raw);
    }
    var out = parts.join('').replace(/[\u200b-\u200d\ufeff]/g, '').replace(/\s+/g, ' ').trim();
    if (!out && root.closest && root.closest('.katex')) {
      var t = katexTex(root.closest('.katex'));
      if (t) out = '$' + t + '$';
    }
    return out;
  }

  /* 复制专用：PDF 原页的文字层是一堆绝对定位的 span，没有 <br> / 块级元素，
     单靠 innerText 或 toString() 拿不到换行与词间空格。这里按 span 的几何位置
     还原：纵向跳行补 \n，横向有间隙且两边都不是空白时补一个空格。 */
  function pdfRangeText(range) {
    if (!range) return '';
    var root = range.commonAncestorContainer;
    if (root && root.nodeType !== 1) root = root.parentNode;
    if (!root) return '';
    var parts = [];
    var prev = null;
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
    var n;
    while ((n = walker.nextNode())) {
      if (!range.intersectsNode(n)) continue;
      var raw = n.nodeValue || '';
      if (range.startContainer === n || range.endContainer === n) {
        var s = range.startContainer === n ? range.startOffset : 0;
        var e = range.endContainer === n ? range.endOffset : raw.length;
        raw = raw.slice(s, e);
      }
      if (!raw) continue;
      var el = n.parentElement;
      var rect = el && el.getBoundingClientRect ? el.getBoundingClientRect() : null;
      if (rect && rect.width && rect.height) {
        if (prev) {
          var lineH = Math.max(4, Math.min(rect.height, prev.height));
          if (rect.top - prev.top > lineH * 0.6) {
            if (parts.length && !/\n$/.test(parts[parts.length - 1])) parts.push('\n');
          } else if (rect.left - prev.right > lineH * 0.18) {
            var last = parts.length ? parts[parts.length - 1].slice(-1) : '';
            if (!/\s/.test(last) && !/^\s/.test(raw)) parts.push(' ');
          }
        }
        prev = rect;
      }
      parts.push(raw);
    }
    return parts.join('')
      .replace(/[\u200b-\u200d\ufeff]/g, '')
      .replace(/^(?:[ \t]*\n)+/, '')
      .replace(/\n+$/, '');
  }

  /* 段落上下文同样带公式：用同一套清洗，避免把 KaTeX 的字形噪声发给模型 */
  function cleanNodeText(el) {
    if (!el) return '';
    var r = document.createRange();
    try { r.selectNodeContents(el); } catch (e) { return (el.textContent || '').trim(); }
    return cleanRangeText(r);
  }

  function wireSelection(root) {
    // 左栏原页的划词也走 SelKit（见 captureLeftPageSelection）：浮层 + 联动都在那里做。
    // 问答 / 翻译 / 批注统一由 AskKit 面板承载，这里只注册阅读器适配器。
    registerReaderSelection();
  }

  /* 阅读器的划词浮层由 selkit.js 统一渲染；问答 / 翻译 / 批注面板交给 askkit.js。 */
  var selKitRegistered = false;

  function registerReaderSelection() {
    if (selKitRegistered || !window.SelKit) return;
    selKitRegistered = true;
    window.SelKit.register({
      id: 'reader',
      roots: function () {
        return [
          document.querySelector('.rd-pages'),   // 左栏原页（PDF 文本层）
          document.querySelector('.rd-flow'),    // 右侧阅读流（原文 / 译文）
          document.querySelector('.rd-tpages'),  // 右侧版式译页
        ];
      },
      maxText: 1600,
      capture: function (range) {
        var pages = document.querySelector('.rd-pages');
        if (pages && pages.offsetParent !== null
          && pages.contains(range.commonAncestorContainer)) {
          var left = captureLeftPageSelection(range);
          if (left) left.label = 'PDF · 第 ' + (left.page || '?') + ' 页';
          return left;
        }
        var tpages = document.querySelector('.rd-tpages');
        var inDst = !!(tpages && tpages.offsetParent !== null
          && tpages.contains(range.commonAncestorContainer));
        var ctx = inDst ? capturePageSelection(range) : captureFlowSelection(range);
        if (!ctx) return null;
        var where = ctx.side === 'dst' ? '译页' : 'PDF';
        ctx.label = where + ' · 第 ' + (ctx.page || '?') + ' 页';
        return ctx;
      },
      actions: [
        { id: 'ask', label: '提问', icon: 'chat', primary: true,
          run: function (ctx) { openReaderAsk(ctx && ctx.rect); } },
        { id: 'translate', label: '翻译', icon: 'book', primary: true,
          run: function (ctx) { openReaderAsk(ctx && ctx.rect, 'translate'); } },
        { id: 'quote', label: '引用', icon: 'note', primary: true,
          run: function () {
            if (typeof addQuote !== 'function' || !sel.text) return;
            addQuote(sel.text, readerSelLabel());
            toast('已引用到对话');
          } },
        { id: 'note', label: '批注', icon: 'pencil',
          run: function (ctx) { openReaderAsk(ctx && ctx.rect, 'note'); } },
        { id: 'excerpt', label: '摘录', icon: 'copy',
          run: function () { excerptSelection(); } },
        { id: 'hl', label: '高亮', icon: 'check-square',
          run: function (ctx) { openReaderAsk(ctx && ctx.rect, 'hl'); } },
        { id: 'copy', label: '复制', icon: 'copy',
          run: function (ctx) { copySelection(ctx && ctx.copy_text); } },
      ],
    });
  }

  function captureFlowSelection(range) {
    if (!RD.doc) return null;
    var text = cleanRangeText(range);
    if (text.length < 2 || text.length > 1600) return null;
    var flow = document.querySelector('.rd-flow');
    if (!flow || !flow.contains(range.commonAncestorContainer)) return null;
    var node = range.commonAncestorContainer;
    var host = node.nodeType === 1 ? node : node.parentElement;
    var blk = host && host.closest ? host.closest('.rd-block') : null;
    // 跨块选 / 从公式图起划时，公共祖先会落在阅读流容器上，closest 找不到段落；
    // 退化为「与选区相交的第一个块」，保证批注仍能挂到具体段落上。
    if (!blk) {
      var blocks = flow.querySelectorAll('.rd-block, .rd-fig');
      for (var i = 0; i < blocks.length; i++) {
        if (range.intersectsNode(blocks[i])) { blk = blocks[i]; break; }
      }
    }
    var src = blk ? blk.querySelector('.rd-src') : null;
    var dst = blk ? blk.querySelector('.rd-dst') : null;
    var copyText = (window.SelKit && typeof window.SelKit.plainText === 'function')
      ? window.SelKit.plainText(range) : text;
    sel.text = text;
    sel.copyText = copyText || text;
    sel.ctx = src ? cleanNodeText(src).slice(0, 600) : '';
    sel.blockId = blk ? (blk.dataset.id || '') : '';
    sel.page = blk ? Number(blk.dataset.page || 0) : 0;
    sel.side = (dst && dst.contains(node)) ? 'dst' : 'src';
    sel.question = '';
    sel.answer = '';
    var rect = range.getBoundingClientRect();
    linkFromFlowSelection();      // 选中即联动左栏原文
    return { text: text, copy_text: sel.copyText, rect: rect, page: sel.page,
             blockId: sel.blockId, side: sel.side };
  }

  /* 译页模式划词：译文是普通 DOM，选区按 .rd-tblock 的 data-id 归段，side 记成 dst。 */
  function capturePageSelection(range) {
    if (!RD.doc) return null;
    var text = cleanRangeText(range);
    if (text.length < 2 || text.length > 1600) return null;
    var host = range.commonAncestorContainer;
    host = host.nodeType === 1 ? host : host.parentElement;
    var box = host && host.closest ? host.closest('.rd-tblock') : null;
    if (!box) {
      // 跨块选中：退化到与选区相交的第一个块
      var all = document.querySelectorAll('.rd-tpages .rd-tblock');
      for (var i = 0; i < all.length; i++) {
        if (range.intersectsNode(all[i])) { box = all[i]; break; }
      }
    }
    if (!box) return null;
    var b = (RD.blockById || {})[box.dataset.id] || {};
    sel.text = text;
    sel.copyText = (window.SelKit && typeof window.SelKit.plainText === 'function')
      ? (window.SelKit.plainText(range) || text) : text;
    sel.ctx = String(b.text || '').slice(0, 600);
    sel.blockId = box.dataset.id || '';
    var pageEl = box.closest('.rd-tpage');
    sel.page = pageEl ? Number(pageEl.dataset.page || 0) : 0;
    sel.side = 'dst';
    sel.question = '';
    sel.answer = '';
    var rect = range.getBoundingClientRect();
    // 右 → 左：译页划词时在左栏标出对应的原文段落
    linkToLeftPage(sel.page, sel.blockId, String(b.text || ''));
    return { text: text, copy_text: sel.copyText, rect: rect, page: sel.page,
             blockId: sel.blockId, side: sel.side };
  }

  /* 左栏原页划词：选区文字是原文，先按页找对应的 block，
     浮层照常弹出；批注 / 高亮 / 摘录都挂在这个 block 上、渲染在右侧
     （阅读流的 .rd-src 或版式译页的覆盖层），不会画回原页 canvas。 */
  function captureLeftPageSelection(range) {
    if (!RD.doc || RD.fileType !== 'pdf') return null;
    var host = range.commonAncestorContainer;
    host = host.nodeType === 1 ? host : host.parentElement;
    var pageEl = host && host.closest ? host.closest('.rd-page') : null;
    if (!pageEl) return null;
    var no = Number(pageEl.dataset.page || 0);
    var text = cleanRangeText(range);
    if (!no || text.length < 2 || text.length > 1600) return null;
    var rect = range.getBoundingClientRect();
    var b = findBlockForQuote(no, text);
    if (!b) {
      // 页眉 / 连字 / 图表注这类文字对不上块时，按选区的几何位置找最近的块
      var size = pageSize(no);
      var pr = pageEl.getBoundingClientRect();
      if (pr.width && pr.height) {
        b = findBlockNear(no,
          (rect.left + rect.width / 2 - pr.left) / pr.width * size.w,
          (rect.top + rect.height / 2 - pr.top) / pr.height * size.h);
      }
    }
    sel.text = text;
    sel.copyText = pdfRangeText(range) || text;
    sel.ctx = b ? String(b.text || '').slice(0, 600) : '';
    sel.blockId = b ? b.id : '';
    sel.page = no;
    // quote 是原文，记成 src：高亮落在右侧的原文段落上；
    // 版式译页没有原文字层，退化为块角标 —— 两边都在右侧，不碰左栏 canvas。
    sel.side = 'src';
    sel.question = '';
    sel.answer = '';
    linkToFlow(no, text, b);      // 左 → 右：标出对应译文块并滚过去
    return { text: text, copy_text: sel.copyText, rect: rect, page: no,
             blockId: sel.blockId, side: sel.side };
  }

  function copySelection(text) {
    var t = String(text || sel.copyText || sel.text || '');
    if (!t) return;
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(t).then(function () {
        toast('已复制选中的文字', 'ok');
      }).catch(function () { toast('复制失败', 'error'); });
    }
  }

  /* ---------- 高亮 / 批注 / 批注抽屉 ---------- */
  var HL_COLORS = { yellow: 1, green: 1, blue: 1, pink: 1, purple: 1 };

  async function saveHighlight(color) {
    if (!RD.doc || !sel.blockId || !sel.text) { toast('先在段落里选中文字'); return false; }
    var r = await api('/api/reader/anno/save', {
      session_id: sessionId(), path: RD.rel,
      block_id: sel.blockId, page: sel.page, side: sel.side,
      kind: 'highlight', color: color, quote: sel.text,
    });
    if (!r || !r.ok) { toast('高亮失败：' + ((r && r.error) || '')); return false; }
    RD.annos = r.items || [];
    applyReaderAnnos(sel.blockId);
    renderAnnoDrawer();
    toast('已高亮', 'ok');
    return true;
  }

  /* ---------- 划词问答：AskKit 统一面板 ---------- */

  function readerSelLabel() {
    return (sel.side === 'dst' ? '译页' : 'PDF') + ' · 第 ' + (sel.page || '?') + ' 页';
  }

  function readerDocTitle() {
    return (RD.docMeta && RD.docMeta.title)
      || (RD.doc && (RD.doc.title || RD.doc.name)) || RD.rel || 'PDF';
  }

  function openReaderAsk(rect, extraId) {
    if (!sel.text) return;
    if (!window.AskKit) { toast('问答面板未加载，请刷新页面', 'error'); return; }
    var label = readerSelLabel();
    var kit = window.AskKit.open({
      scope: 'doc:' + RD.rel,
      source: 'reader',
      selection: {
        kind: 'reader', text: sel.text, context: sel.ctx,
        page: sel.page, block_id: sel.blockId, side: sel.side,
        title: readerDocTitle(),
      },
      anchor: 'pdf:' + sel.page + ':' + (sel.blockId || '') + '|'
        + String(sel.text).slice(0, 40),
      label: label,
      quote: sel.text,
      title: '文献问答',
      rect: rect,
      persist: saveReaderAskAsAnno,
      onQuote: function (text) {
        if (typeof addQuote !== 'function') return;
        addQuote(text, label);
        window.AskKit.close();
      },
      extras: [
        { id: 'translate', label: '翻译', run: readerTranslate },
        { id: 'hl', label: '高亮', run: readerHighlight },
        { id: 'note', label: '批注', run: readerNote },
      ],
    });
    if (extraId && kit) kit.runExtra(extraId);
  }

  async function readerTranslate(kit) {
    kit.showTool('<div class="askkit-tool-loading">翻译中…</div>');
    try {
      var r = await api('/api/reader/selection', {
        session_id: sessionId(), text: sel.text, context: sel.ctx,
      });
      if (!r || !r.ok) throw new Error((r && r.error) || '翻译失败');
      var wrap = document.createElement('div');
      var body = document.createElement('div');
      body.textContent = r.text || '';
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'askkit-note-save';
      btn.textContent = '复制译文';
      btn.onclick = function () {
        if (navigator.clipboard && r.text) navigator.clipboard.writeText(r.text);
        toast('已复制译文', 'ok');
      };
      wrap.appendChild(body);
      wrap.appendChild(btn);
      kit.showTool(wrap);
    } catch (e) {
      kit.showTool('<div class="askkit-tool-err">翻译失败：'
        + escapeHtml((e && e.message) || e) + '</div>');
    }
  }

  function readerHighlight(kit) {
    var colors = [['yellow', '黄色'], ['green', '绿色'], ['blue', '蓝色'],
      ['pink', '粉色'], ['purple', '紫色']];
    var row = document.createElement('div');
    row.className = 'askkit-colors';
    colors.forEach(function (c) {
      var b = document.createElement('button');
      b.type = 'button';
      b.dataset.color = c[0];
      b.className = 'c-' + c[0];
      b.title = c[1];
      b.onclick = function () {
        saveHighlight(c[0]).then(function () { kit.hideTool(); });
      };
      row.appendChild(b);
    });
    kit.showTool(row);
  }

  function readerNote(kit) {
    var wrap = document.createElement('div');
    var ta = document.createElement('textarea');
    ta.className = 'askkit-note-input';
    ta.placeholder = '写批注…';
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'askkit-note-save';
    btn.textContent = '保存批注';
    btn.onclick = function () {
      var txt = String(ta.value || '').trim();
      if (!txt) { ta.focus(); return; }
      saveNoteAnnotation(txt).then(function (ok) {
        if (ok) kit.showTool('<div>批注已保存</div>');
      });
    };
    wrap.appendChild(ta);
    wrap.appendChild(btn);
    kit.showTool(wrap);
    setTimeout(function () { ta.focus(); }, 0);
  }

  async function saveReaderAskAsAnno(qa) {
    if (!RD.doc || !sel.blockId || !sel.text) throw new Error('这段内容没有对应的译文块');
    var r = await api('/api/reader/anno/save', {
      session_id: sessionId(), path: RD.rel,
      block_id: sel.blockId, page: sel.page, side: sel.side,
      quote: sel.text, question: (qa && qa.question) || '',
      answer: (qa && qa.answer) || '',
    });
    if (!r || !r.ok) throw new Error((r && r.error) || '保存失败');
    RD.annos = r.items || [];
    applyReaderAnnos(sel.blockId);
    renderAnnoDrawer();
  }

  /* ---------- 学术：摘录 / 文献信息 / 引用 / 导出 ---------- */

  function citationLine(a) {
    var d = RD.docMeta || {};
    var c = (a && a.citation) || {};
    var authors = (c.authors && c.authors.length) ? c.authors : (d.authors || []);
    var year = c.year || d.year || '';
    var who = authors.length ? authors.join(', ') : (d.title || '文献');
    return who + (year ? ' (' + year + ')' : '') + ', p.' + ((a && a.page) || '?');
  }

  async function excerptSelection() {
    if (!RD.doc || !sel.text) { toast('先选中一段文字'); return; }
    var r = await api('/api/anno/excerpt', {
      session_id: sessionId(), path: RD.rel,
      block_id: sel.blockId, page: sel.page, side: sel.side, quote: sel.text,
    });
    if (!r || !r.ok) { toast('摘录失败：' + ((r && r.error) || '')); return; }
    RD.annos = r.items || [];
    applyReaderAnnos(sel.blockId);
    renderAnnoDrawer();
    toast('已摘录到笔记：' + (r.note || ''), 'ok');
  }

  function showDocMeta() {
    var m = document.querySelector('.rd-meta-modal');
    if (!m) return;
    var d = RD.docMeta || {};
    var set = function (k, v) {
      var el = m.querySelector('[data-m="' + k + '"]');
      if (el) el.value = v == null ? '' : v;
    };
    set('title', d.title);
    set('authors', (d.authors || []).join(', '));
    set('year', d.year || '');
    set('venue', d.venue);
    set('doi', d.doi);
    set('tags', (d.tags || []).join(', '));
    set('abstract', d.abstract);
    m.classList.remove('hidden');
    var first = m.querySelector('[data-m="title"]');
    if (first) first.focus();
  }

  async function saveDocMeta() {
    var m = document.querySelector('.rd-meta-modal');
    if (!m || !RD.doc) return;
    var val = function (k) {
      var el = m.querySelector('[data-m="' + k + '"]');
      return el ? String(el.value || '').trim() : '';
    };
    var r = await api('/api/doc/save', {
      session_id: sessionId(), path: RD.rel,
      title: val('title'), authors: val('authors'), year: val('year'),
      venue: val('venue'), doi: val('doi'), tags: val('tags'), abstract: val('abstract'),
    });
    if (!r || !r.ok) { toast('保存失败：' + ((r && r.error) || '')); return; }
    RD.docMeta = r.doc || null;
    m.classList.add('hidden');
    toast('文献信息已保存', 'ok');
    renderAnnoDrawer();
  }

  async function exportAnnos() {
    if (!RD.doc) return;
    var r = await api('/api/anno/export', {
      session_id: sessionId(), path: RD.rel, format: 'md',
    });
    if (!r || !r.ok) { toast('导出失败'); return; }
    var blob = new Blob([r.text || ''], { type: 'text/markdown;charset=utf-8' });
    var a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = r.name || 'annotations.md';
    document.body.appendChild(a);
    a.click();
    setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
    toast('已导出批注', 'ok');
  }

  async function saveNoteAnnotation(txt) {
    if (!RD.doc || !sel.blockId || !sel.text) { toast('先选中一段文字'); return false; }
    var text = String(txt || '').trim();
    if (!text) return false;
    var r = await api('/api/reader/anno/save', {
      session_id: sessionId(), path: RD.rel,
      block_id: sel.blockId, page: sel.page, side: sel.side,
      kind: 'note', comment: text, quote: sel.text,
    });
    if (!r || !r.ok) { toast('保存失败：' + ((r && r.error) || '')); return false; }
    RD.annos = r.items || [];
    applyReaderAnnos(sel.blockId);
    renderAnnoDrawer();
    toast('批注已保存', 'ok');
    return true;
  }

  function clearBlockAnnos(el) {
    Array.prototype.forEach.call(el.querySelectorAll('.rd-anno-dot'), function (n) { n.remove(); });
    Array.prototype.forEach.call(el.querySelectorAll('mark.rd-hl'), function (m) {
      var p = m.parentNode;
      if (!p) return;
      while (m.firstChild) p.insertBefore(m.firstChild, m);
      p.removeChild(m);
      p.normalize();
    });
    el.classList.remove('has-anno');
  }

  /* 把 quote 在元素里包成 <mark>；找不到（译文重排 / 跨节点）返回 null，由左侧色条兜底。
     返回最后一个 mark：数字角标要贴在「高亮末尾」，和截图里的位置一致。 */
  function highlightIn(el, quote, color) {
    if (!el || !quote) return null;
    var full = el.textContent || '';
    var at = full.indexOf(quote);
    if (at < 0) return null;
    var end = at + quote.length;
    var walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, null);
    var nodes = [], pos = 0, n;
    while ((n = walker.nextNode())) {
      nodes.push({ node: n, start: pos, end: pos + n.nodeValue.length });
      pos += n.nodeValue.length;
    }
    var last = null;
    nodes.filter(function (t) { return t.end > at && t.start < end; }).forEach(function (t) {
      var s = Math.max(at, t.start) - t.start;
      var e = Math.min(end, t.end) - t.start;
      if (e <= s) return;
      var raw = t.node.nodeValue || '';
      var parent = t.node.parentNode;
      if (!parent) return;
      var mark = document.createElement('mark');
      mark.className = color === 'live' ? 'rd-hl rd-hl-live'
        : 'rd-hl c-' + (HL_COLORS[color] ? color : 'yellow');
      mark.textContent = raw.slice(s, e);
      t.node.nodeValue = raw.slice(0, s);
      parent.insertBefore(mark, t.node.nextSibling);
      if (e < raw.length) {
        parent.insertBefore(document.createTextNode(raw.slice(e)), mark.nextSibling);
      }
      last = mark;
    });
    return last;
  }

  function kindLabel(a) {
    if (a.kind === 'qa') return '问答';
    if (a.kind === 'highlight') return '高亮';
    if (a.kind === 'excerpt') return '摘录';
    return '批注';
  }

  function updateAnnoCount() {
    var n = (RD.annos || []).length;
    var b = document.querySelector('.rd-anno-n');
    if (b) b.textContent = n ? String(n) : '';
  }

  /* 批注编号：角标、抽屉、弹层共用一套（按页 + 创建顺序） */
  function annoNumbers() {
    var sorted = (RD.annos || []).slice().sort(function (a, b) {
      return (a.page || 0) - (b.page || 0) || (a.created || 0) - (b.created || 0);
    });
    var map = {};
    sorted.forEach(function (a, i) { map[a.id] = i + 1; });
    return map;
  }

  function annoTitle(a) {
    if (a.kind === 'qa') return a.question || '问答';
    if (a.comment) return a.comment;
    if (a.kind === 'excerpt') return '摘录';
    return '高亮';
  }

  /* onlyId：译文回填后只重画那一段，避免整篇重扫 */
  function applyReaderAnnos(onlyId) {
    var ids = onlyId ? [onlyId] : Object.keys(RD.blockEls);
    var by = {};
    var no = annoNumbers();
    (RD.annos || []).forEach(function (a) {
      if (!a.block_id) return;
      (by[a.block_id] = by[a.block_id] || []).push(a);
    });
    ids.forEach(function (id) {
      var el = RD.blockEls[id];
      if (!el) return;
      clearBlockAnnos(el);
      var list = by[id] || [];
      if (!list.length) return;
      el.classList.add('has-anno');
      var edge = [];
      list.forEach(function (a) {
        var host = el.querySelector(a.side === 'dst' ? '.rd-dst' : '.rd-src') || el;
        var mark = a.quote ? highlightIn(host, a.quote, a.color) : null;
        var dot = document.createElement('button');
        dot.type = 'button';
        dot.className = 'rd-anno-dot';
        dot.textContent = String(no[a.id] || '');
        dot.title = kindLabel(a) + '：' + String(annoTitle(a)).slice(0, 60);
        dot.onclick = function (e) {
          e.stopPropagation();
          e.preventDefault();
          showReaderAnno(list, dot);
        };
        if (mark) {
          // 数字角标贴在「高亮文字」右上角（正文行盒会自然绕开，不压字）
          mark.classList.add('rd-hl-dot');
          mark.appendChild(dot);
        } else {
          edge.push(dot);
        }
      });
      // 原文重排 / 跨节点找不到高亮：退回段落右上角，多个纵向错开
      edge.forEach(function (dot, i) {
        dot.classList.add('rd-anno-dot-edge');
        dot.style.top = (2 + i * 21) + 'px';
        el.appendChild(dot);
      });
    });
    // 译页覆盖层同步：保存 / 删除批注后立即生效（只重贴已渲染的页）
    Object.keys(RD.tblockEls || {}).forEach(function (p) { applyPageAnnos(Number(p)); });
    updateAnnoCount();
    if (!onlyId) renderAnnoDrawer();
  }

  var annoFilter = 'all';

  function toggleAnnoDrawer(force) {
    var d = document.querySelector('.rd-anno-drawer');
    if (!d) return;
    var open = typeof force === 'boolean' ? force : d.classList.contains('hidden');
    d.classList.toggle('hidden', !open);
    if (open) renderAnnoDrawer();
  }

  function renderAnnoDrawer() {
    var box = document.querySelector('.rd-anno-list');
    if (!box) return;
    var no = annoNumbers();
    var list = (RD.annos || []).slice();
    if (annoFilter === 'qa') list = list.filter(function (a) { return a.kind === 'qa'; });
    else if (annoFilter !== 'all') list = list.filter(function (a) { return a.kind === annoFilter; });
    list.sort(function (a, b) {
      return (a.page || 0) - (b.page || 0) || (a.created || 0) - (b.created || 0);
    });
    if (!list.length) {
      box.innerHTML = '<div class="rd-anno-empty">还没有批注。选中文字后点「高亮 / 提问」。</div>';
      return;
    }
    box.innerHTML = list.map(function (a) {
      var title = a.kind === 'qa'
        ? ('Q：' + escapeHtml(a.question || ''))
        : (a.comment ? escapeHtml(a.comment.slice(0, 80)) : '高亮');
      return '<div class="rd-anno-row" data-id="' + escapeHtml(a.id) + '">' +
        '<b class="rd-anno-no">' + (no[a.id] || '') + '</b>' +
        '<div class="rd-anno-row-main">' +
          '<div class="rd-anno-row-meta">p.' + (a.page || '?') + ' · ' + kindLabel(a) + '</div>' +
          '<div class="rd-anno-row-q">' + title + '</div>' +
          (a.quote ? '<div class="rd-anno-row-quote">' + escapeHtml(a.quote.slice(0, 90)) + '</div>' : '') +
        '</div>' +
        '</div>';
    }).join('');
    Array.prototype.forEach.call(box.querySelectorAll('.rd-anno-row'), function (row) {
      row.onclick = function () { jumpToAnno(row.dataset.id); };
    });
  }

  async function jumpToAnno(id) {
    var a = (RD.annos || []).find(function (x) { return x.id === id; });
    if (!a || !a.block_id) return;
    // 译页模式：阅读流是隐藏的，直接翻页并闪一下覆盖层里的块
    if ((RD.mode === 'page' || RD.mode === 'page-dual') && a.page) {
      goPage(Number(a.page));
      ensureTransPages();
      queueTransRender(Number(a.page));
      var tries = 0;
      var flash = function () {
        var box = ((RD.tblockEls || {})[Number(a.page)] || {})[a.block_id];
        if (!box) {
          if (tries++ < 3) setTimeout(flash, 300);
          return;
        }
        box.scrollIntoView({ block: 'center', behavior: 'smooth' });
        box.classList.add('rd-flash');
        setTimeout(function () { box.classList.remove('rd-flash'); }, 1200);
      };
      setTimeout(flash, 300);
      return;
    }
    if (!RD.transOn) setMode(a.side === 'dst' ? 'dual' : 'orig');
    await ensureFlow();                 // 阅读流是按需构建的，先等它把这一段建出来
    var el = RD.blockEls[a.block_id];
    if (!el) { toast('这一段的批注暂时定位不到（文档可能已改动）'); return; }
    setTimeout(function () {
      el.scrollIntoView({ block: 'center', behavior: 'smooth' });
      el.classList.add('rd-flash');
      setTimeout(function () { el.classList.remove('rd-flash'); }, 1200);
    }, 60);
  }

  var readerAnnoPop = null;

  function ensureReaderAnnoPop() {
    if (readerAnnoPop) return readerAnnoPop;
    var el = document.createElement('div');
    el.className = 'rd-anno-pop hidden';
    document.body.appendChild(el);
    el.addEventListener('mousedown', function (e) { e.stopPropagation(); });
    document.addEventListener('mousedown', function (e) {
      if (el.classList.contains('hidden')) return;
      if (el.contains(e.target) || e.target.closest('.rd-anno-dot')) return;
      el.classList.add('hidden');
    });
    readerAnnoPop = el;
    return el;
  }

  function showReaderAnno(items, anchor) {
    var pop = ensureReaderAnnoPop();
    var no = annoNumbers();
    pop.innerHTML = '<div class="rd-anno-head">本文批注 · ' + items.length + '</div>' +
      items.map(function (a) {
        var body = '';
        if (a.kind === 'qa') {
          var ans = (typeof safeRenderMarkdown === 'function')
            ? safeRenderMarkdown(a.answer || '') : escapeHtml(a.answer || '');
          body = '<div class="rd-anno-q">Q：' + escapeHtml(a.question || '这段内容讲了什么？') + '</div>' +
            '<div class="rd-anno-a">' + ans + '</div>';
        } else if (a.comment) {
          var cm = (typeof safeRenderMarkdown === 'function')
            ? safeRenderMarkdown(a.comment) : escapeHtml(a.comment);
          body = '<div class="rd-anno-a">' + cm + '</div>';
        }
        var canCopy = !!(a.answer || a.comment || a.quote);
        return '<div class="rd-anno-item" data-id="' + escapeHtml(a.id) + '">' +
          '<div class="rd-anno-kind"><b class="rd-anno-no">' + (no[a.id] || '') + '</b>' +
          'p.' + (a.page || '?') + ' · ' + kindLabel(a) + '</div>' +
          body +
          (a.quote ? '<div class="rd-anno-quote">“' + escapeHtml(a.quote.slice(0, 120)) + '”</div>' : '') +
          '<div class="rd-anno-acts">' +
          '<button data-cite>复制引用</button>' +
          (canCopy ? '<button data-copy>复制内容</button>' : '') +
          '<button data-del>删除</button></div></div>';
      }).join('');
    pop.classList.remove('hidden');
    var w = Math.min(440, window.innerWidth - 24);
    pop.style.width = w + 'px';
    var r = anchor.getBoundingClientRect();
    var h = pop.offsetHeight || 260;
    var left = Math.max(8, Math.min(window.innerWidth - w - 8, r.left - w + 80));
    var top = r.bottom + 8;
    if (top + h > window.innerHeight - 8) top = Math.max(8, r.top - h - 8);
    pop.style.left = left + 'px';
    pop.style.top = top + 'px';
    Array.prototype.forEach.call(pop.querySelectorAll('.rd-anno-item'), function (row) {
      var id = row.dataset.id;
      var citeBtn = row.querySelector('[data-cite]');
      if (citeBtn) {
        citeBtn.onclick = function () {
          var a = (RD.annos || []).find(function (x) { return x.id === id; });
          var line = citationLine(a);
          if (line && navigator.clipboard) {
            navigator.clipboard.writeText(line).then(function () { toast('已复制引用', 'ok'); });
          }
        };
      }
      var copyBtn = row.querySelector('[data-copy]');
      if (copyBtn) {
        copyBtn.onclick = function () {
          var a = (RD.annos || []).find(function (x) { return x.id === id; });
          var text = a ? (a.answer || a.comment || a.quote || '') : '';
          if (text && navigator.clipboard) {
            navigator.clipboard.writeText(text).then(function () { toast('已复制', 'ok'); });
          }
        };
      }
      row.querySelector('[data-del]').onclick = async function () {
        if (!confirm('删除这条批注？')) return;
        var res = await api('/api/reader/anno/delete', {
          session_id: sessionId(), path: RD.rel, id: id,
        });
        if (!res || !res.ok) { toast('删除失败'); return; }
        RD.annos = res.items || [];
        applyReaderAnnos();
        pop.classList.add('hidden');
        toast('批注已删除');
      };
    });
  }

  function wireSplitter(root) {
    var split = $('.rd-split', root);
    var left = $('.rd-left', root);
    var dragging = false;
    split.addEventListener('mousedown', function (e) {
      dragging = true;
      e.preventDefault();
      document.body.classList.add('rd-resizing');
      split.classList.add('on');
    });
    document.addEventListener('mousemove', function (e) {
      if (!dragging) return;
      var w = Math.max(220, Math.min(window.innerWidth * 0.7, e.clientX));
      left.style.width = w + 'px';
    });
    document.addEventListener('mouseup', function () {
      if (!dragging) return;
      dragging = false;
      split.classList.remove('on');
      document.body.classList.remove('rd-resizing');
      rerenderVisible();
    });
  }

  function onKey(e) {
    var root = document.getElementById('rd-root');
    if (!root || root.classList.contains('hidden')) return;
    if (e.key === 'Escape') {
      var ask = document.querySelector('.askkit');
      if (ask && !ask.classList.contains('hidden')) {
        e.preventDefault();
        if (window.AskKit) window.AskKit.close();
        return;
      }
      if (readerAnnoPop && !readerAnnoPop.classList.contains('hidden')) {
        e.preventDefault();
        readerAnnoPop.classList.add('hidden');
        return;
      }
      var drawer = document.querySelector('.rd-anno-drawer');
      if (drawer && !drawer.classList.contains('hidden')) {
        e.preventDefault();
        toggleAnnoDrawer(false);
        return;
      }
      var fbox = document.querySelector('.rd-find');
      if (fbox && !fbox.classList.contains('hidden')) {
        e.preventDefault();
        toggleFind(false);
        return;
      }
      if (RD.live.on) {
        e.preventDefault();
        clearLive();
        return;
      }
      e.preventDefault();
      close();
      return;
    }
    if (e.target && /input|textarea/i.test(e.target.tagName)) return;
    // 查找 / 页码：在输入框里也允许（和浏览器习惯一致）
    if (e.ctrlKey || e.metaKey) {
      if (e.key === 'f' || e.key === 'F') { e.preventDefault(); e.stopPropagation(); toggleFind(true); return; }
      if (e.key === 'g' || e.key === 'G') {
        e.preventDefault();
        var pi = document.querySelector('.rd-pageinput');
        if (pi) { pi.focus(); pi.select(); }
        return;
      }
    }
    if (e.altKey && e.key === 'ArrowLeft') { e.preventDefault(); historyBack(); return; }
    if (e.ctrlKey || e.metaKey) {
      if (e.key === '=' || e.key === '+') { e.preventDefault(); bumpText(1); return; }
      if (e.key === '-') { e.preventDefault(); bumpText(-1); return; }
      if (e.key === '0') {
        e.preventDefault();
        setZoom(1);
        applyPrefs();
        return;
      }
    }
    if (e.altKey && e.key === '0') {
      e.preventDefault();
      setMode('pdf');
      return;
    }
    if (e.altKey && ['1', '2', '3', '4', '5'].indexOf(e.key) >= 0) {
      e.preventDefault();
      setMode(['orig', 'dual', 'trans', 'page', 'page-dual'][Number(e.key) - 1]);
      return;
    }
    if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
      e.preventDefault();
      goPage(RD.cur + (e.key === 'ArrowRight' ? 1 : -1));
      return;
    }
    if (e.key === 'PageUp' || e.key === 'PageDown') {
      e.preventDefault();
      goPage(RD.cur + (e.key === 'PageDown' ? 1 : -1));
      return;
    }
    // 阅读快捷键（不在输入框里、且没有其它弹窗时才生效）
    if (!e.ctrlKey && !e.metaKey && !e.altKey && !document.querySelector('.modal:not(.hidden)')) {
      var k = e.key;
      if (k === 'j' || k === 'J') { e.preventDefault(); goPage(RD.cur + 1); return; }
      if (k === 'k' || k === 'K') { e.preventDefault(); goPage(RD.cur - 1); return; }
      if (k === ' ') { e.preventDefault(); goPage(RD.cur + (e.shiftKey ? -1 : 1)); return; }
      if (k === 'Home') { e.preventDefault(); goPage(1); return; }
      if (k === 'End') { e.preventDefault(); goPage(RD.doc ? RD.doc.parsed_pages : 1); return; }
      if (k === '+' || k === '=') { e.preventDefault(); setZoom(RD.zoom * 1.15); return; }
      if (k === '-') { e.preventDefault(); setZoom(RD.zoom / 1.15); return; }
      if (k === '0') { e.preventDefault(); setZoomMode('width'); return; }
    }
    if (e.key === 'o' || e.key === 'O') {
      e.preventDefault();
      var lt = document.querySelector('.rd-left-toggle');
      if (lt) lt.click();
    }
  }

  function updatePageInd() {
    var input = document.querySelector('.rd-pageinput');
    var totalEl = document.querySelector('.rd-pagetotal');
    var total = (RD.doc && (RD.doc.parsed_pages || RD.doc.pages)) || 1;
    if (totalEl) totalEl.textContent = '/ ' + total;
    if (input) {
      input.max = String(total);
      if (document.activeElement !== input) input.value = String(RD.cur);
    }
  }

  function updateProgress() {
    var bar = document.querySelector('.rd-progress i');
    var n = RD.totalBlocks || 0;
    if (bar) bar.style.width = (n ? Math.min(100, RD.doneBlocks / n * 100) : 0) + '%';
    updatePageInd();
    updateStat();
  }

  /* 版式模式不建阅读流，翻译进度要单独按 doc.blocks 统计 */
  var syncStatTimer = 0;
  function syncTransStats() {
    if (RD.flowBuilt || !RD.doc) return;
    var total = 0, done = 0;
    (RD.doc.blocks || []).forEach(function (b) {
      if (!b || (b.kind !== 'text' && b.kind !== 'list' && b.kind !== 'heading')) return;
      if (!String(b.text || '').trim()) return;
      total++;
      if (String((RD.translations || {})[b.id] || '').trim()) done++;
    });
    RD.totalBlocks = total;
    RD.doneBlocks = done;
    updateProgress();
  }

  function scheduleSyncStats() {
    if (RD.flowBuilt) return;
    clearTimeout(syncStatTimer);
    syncStatTimer = setTimeout(syncTransStats, 120);
  }

  function refreshTransBtns() {
    var busy = !!RD.streaming;
    Array.prototype.forEach.call(document.querySelectorAll('.rd-actions [data-act="page"], .rd-actions [data-act="all"]'),
      function (b) {
        // 不要用 disabled：翻译期间按钮变灰会让人以为界面卡死。保持可点，只显示转圈。
        b.classList.toggle('busy', busy);
      });
  }

  /* ------------------------------------------------------------ 打开 / 关闭 */

  async function open(rel) {
    var root = ensureRoot();
    root.classList.remove('hidden');
    $('.rd-loading', root).classList.remove('hidden');
    $('.rd-name', root).textContent = rel;
    $('.rd-flow', root).innerHTML = '';
    $('.rd-pages', root).innerHTML = '';
    $('.rd-tpages', root).innerHTML = '';
    $('.rd-tpages', root).classList.add('hidden');
    RD.doc = null; RD.rel = rel; RD.pages = {}; RD.tpages = {}; RD.tpagesBuilt = '';
    RD.blockEls = {}; RD.blockById = {}; RD.tblockEls = {}; RD.cur = 1;
    RD.transOn = false;
    RD.docMeta = null;
    RD.pending = new Set(); RD.requested = new Set();
    RD.totalBlocks = 0; RD.doneBlocks = 0; RD.skipped = 0;
    RD.translations = {};
    RD.allRequested = false;
    RD.streaming = false;
    RD.job = null;
    RD.zoom = 1;
    RD.zoomMode = prefs.zoomMode || 'width';
    RD.history = [];
    RD.flowBuilt = false;
    RD.flowBuilding = null;
    RENDER.gen++;                 // 作废上一个文档的在途渲染
    RENDER.queue.length = 0;
    clearLive();
    var findBox = $('.rd-find', root);
    if (findBox) findBox.classList.add('hidden');
    FIND.q = ''; FIND.items = []; FIND.idx = -1; FIND.total = 0;
    var thBox = $('.rd-thumbs', root);
    if (thBox) thBox.classList.add('hidden');
    var thInner = $('.rd-thumbs-inner', root);
    if (thInner) { thInner.innerHTML = ''; thInner.dataset.doc = ''; }
    RD.thumbs = {}; RD.thumbOrder = [];
    var thBtn = $('[data-act="thumbs"]', root);
    if (thBtn) thBtn.classList.remove('on');
    refreshTransBtns();
    applyPrefs();
    var setBox = $('.rd-settings', root);
    if (setBox) setBox.classList.add('hidden');
    setMode('pdf');          // 默认只看 PDF 原页，翻译由用户点按钮才启动
    try {
      var r = await api('/api/reader/open', {
        session_id: sessionId(), path: rel, lang: 'zh',
      });
      if (!r || !r.ok) throw new Error((r && r.error) || '打不开这个文档');
      RD.doc = r;
      RD.fileType = r.file_type || 'pdf';
      RD.formulaAI = r.formula_ai !== false;
      RD.annos = r.annotations || [];
      RD.docMeta = r.document || null;
      root.classList.toggle('rd-docmode', RD.fileType !== 'pdf');
      $('.rd-name', root).textContent = r.name + ' · ' + r.pages + ' 页';
      if (RD.fileType === 'pdf') {
        await loadPdf();
        buildPages();
        buildTransPages();
      }
      // 阅读流按需构建（pdf 模式只看原页，不必为 1000 页文档先建 2.7 万个节点）
      applyTranslations(r.translations || {});
      applyReaderAnnos();
      restorePos();
      updateProgress();
      $('.rd-loading', root).classList.add('hidden');
      if (RD.fileType === 'pdf') rdEnsurePages([1]);
      else {
        setMode('orig');      // Word / PPT：没有原页图像，直接看原文阅读流
        ensureFlow();
      }
    } catch (e) {
      $('.rd-loading', root).classList.add('hidden');
      toast('打开失败：' + (e.message || e), 'error');
      close();
    }
  }

  async function loadPdf() {
    if (!RD.pdfjs) {
      RD.pdfjs = await import('/static/vendor/pdfjs/pdf.min.mjs');
      RD.pdfjs.GlobalWorkerOptions.workerSrc = '/static/vendor/pdfjs/pdf.worker.min.mjs';
    }
    var res = await fetch('/api/reader/file', {
      method: 'POST', headers: authHeaders(),
      body: JSON.stringify({ session_id: sessionId(), path: RD.rel }),
    });
    if (!res.ok) throw new Error('读取 PDF 失败（HTTP ' + res.status + '）');
    var buf = await res.arrayBuffer();
    RD.pdf = await RD.pdfjs.getDocument({ data: new Uint8Array(buf) }).promise;
  }

  function close() {
    var root = document.getElementById('rd-root');
    if (!root) return;
    if (immersiveOn()) {
      // Esc 关闭阅读器时也要退出系统全屏，否则窗口会停在无标题栏状态
      toggleFullscreen(false);
    }
    savePos();
    if (RD.stream) { try { RD.stream.abort(); } catch (e) { /* 忽略 */ } RD.stream = null; }
    if (RD.job) {
      fetch('/api/reader/cancel', {
        method: 'POST', headers: authHeaders(),
        body: JSON.stringify({ session_id: sessionId(), job_id: RD.job }),
      }).catch(function () { /* 忽略 */ });
      RD.job = null;
    }
    RD.streaming = false;
    RENDER.gen++;                     // 取消在途渲染
    RENDER.queue.length = 0;
    Object.keys(RD.pages).forEach(function (n) { releasePage(RD.pages[n]); });
    if (pageObserver) { try { pageObserver.disconnect(); } catch (e) { /* 忽略 */ } pageObserver = null; }
    if (THUMB.observer) { try { THUMB.observer.disconnect(); } catch (e) { /* 忽略 */ } THUMB.observer = null; }
    clearLive();
    Object.keys(RD.crops).forEach(function (k) { URL.revokeObjectURL(RD.crops[k]); });
    RD.crops = {};
    Object.keys(RD.thumbs).forEach(function (k) {
      try { RD.thumbs[k].width = 0; RD.thumbs[k].height = 0; } catch (e) { /* 忽略 */ }
    });
    RD.thumbs = {}; RD.thumbOrder = [];
    if (RD.pdf) { try { RD.pdf.destroy(); } catch (e) { /* 忽略 */ } RD.pdf = null; }
    root.classList.add('hidden');
    var setBox = $('.rd-settings', root);
    if (setBox) setBox.classList.add('hidden');
    if (window.AskKit) window.AskKit.close();
    document.body.classList.remove('rd-resizing');
  }

  /* ------------------------------------------------------------ 左：原页
   *
   * 性能约定（1000 页基线）：
   *   · 页面 div 只放占位尺寸，canvas / 文本层 / 高亮层只在「可视 ±2 页」存在；
   *   · 离屏超过 6 页就 releasePage()：取消渲染、清空 canvas、卸载文本层、page.cleanup()；
   *   · 渲染走单飞队列（同时 2 个），缩放/拖分栏时整代作废（RENDER.gen），不排队旧任务；
   *   · PageIndex 只存 pt 坐标，缩放不需要重建，只在 paint 时乘 scale。
   */

  function pageSize(n) {
    var s = (RD.doc && RD.doc.sizes && RD.doc.sizes[n - 1]) || [595, 842];
    return { w: s[0], h: s[1] };
  }

  var RENDER = { queue: [], active: 0, max: 2, gen: 0, timer: 0 };

  function buildPages() {
    var host = $('.rd-pages');
    var frag = document.createDocumentFragment();
    var n = (RD.doc && RD.doc.parsed_pages) || 0;
    for (var i = 1; i <= n; i++) {
      var size = pageSize(i);
      var el = document.createElement('div');
      el.className = 'rd-page loading';
      el.dataset.page = String(i);
      el.style.aspectRatio = size.w + ' / ' + size.h;
      var cv = document.createElement('canvas');
      cv.className = 'rd-canvas';
      var hl = document.createElement('div');
      hl.className = 'rd-hl-layer';
      var tx = document.createElement('div');
      tx.className = 'rd-text-layer';
      el.appendChild(cv);
      el.appendChild(hl);
      el.appendChild(tx);
      el.onclick = function () {
        // 拖选文字时 mouseup 之后浏览器还会补一个 click：别让它把左栏滚回页首
        var s = window.getSelection();
        if (s && !s.isCollapsed) return;
        goPage(Number(this.dataset.page));
      };
      frag.appendChild(el);
      RD.pages[i] = {
        el: el, canvas: cv, hlEl: hl, textEl: tx,
        rendered: false, rendering: false, task: null,
        page: null, tc: null, index: null, scale: 0, fail: 0,
        baseW: size.w, baseH: size.h,
      };
    }
    host.appendChild(frag);
    observePages();
    markActivePage();
  }

  /* ---------------- 版式译页（原页 + 按 block.rect 内嵌译文） ---------------- */

  var TRENDER = { queue: [], running: 0, gen: 0, timers: {} };
  var tpageObserver = null;

  function buildTransPages() {
    if (!RD.doc || RD.fileType !== 'pdf') return;
    var host = $('.rd-tpages');
    if (!host) return;
    host.innerHTML = '';
    RD.tpages = {};
    RD.tblockEls = {};
    var n = RD.doc.parsed_pages || 0;
    var frag = document.createDocumentFragment();
    for (var i = 1; i <= n; i++) {
      var size = pageSize(i);
      var el = document.createElement('div');
      el.className = 'rd-tpage loading';
      el.dataset.page = String(i);
      el.style.aspectRatio = size.w + ' / ' + size.h;
      var cv = document.createElement('canvas');
      cv.className = 'rd-tcanvas';
      var layer = document.createElement('div');
      layer.className = 'rd-tlayer';
      el.appendChild(cv);
      el.appendChild(layer);
      frag.appendChild(el);
      RD.tpages[i] = { el: el, canvas: cv, layer: layer, rendered: false,
                       rendering: false, page: null, task: null, fail: 0, scale: 0 };
    }
    host.appendChild(frag);
    RD.tpagesBuilt = RD.doc.key || RD.rel;
    observeTransPages();
  }

  function observeTransPages() {
    if (tpageObserver) tpageObserver.disconnect();
    var rootEl = $('.rd-right');
    if (!rootEl) return;
    tpageObserver = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (!en.isIntersecting) return;
        var no = Number(en.target.dataset.page);
        queueTransRender(no);
        if (no < ((RD.doc && RD.doc.parsed_pages) || 0)) queueTransRender(no + 1);
      });
    }, { root: rootEl, rootMargin: '600px 0px' });
    Object.keys(RD.tpages).forEach(function (n) { tpageObserver.observe(RD.tpages[n].el); });
  }

  function ensureTransPages() {
    if (!RD.doc || RD.fileType !== 'pdf') return;
    if (RD.tpagesBuilt !== (RD.doc.key || RD.rel)) buildTransPages();
    var host = $('.rd-tpages');
    if (host) host.classList.remove('hidden');
    queueTransRender(RD.cur);
  }

  function queueTransRender(no) {
    var st = RD.tpages[no];
    if (!st || st.rendered || st.rendering) return;
    if (TRENDER.queue.indexOf(no) < 0) TRENDER.queue.push(no);
    pumpTransQueue();
  }

  function pumpTransQueue() {
    var right = $('.rd-right');
    if (!right || !right.clientWidth) return;       // 还在原页模式：等切换后再渲染
    while (TRENDER.running < 2 && TRENDER.queue.length) {
      var no = TRENDER.queue.shift();
      TRENDER.running++;
      renderTransPage(no).then(function () {}, function () {}).then(function () {
        TRENDER.running--;
        pumpTransQueue();
      });
    }
  }

  async function renderTransPage(no) {
    var st = RD.tpages[no];
    var right = $('.rd-right');
    if (!st || st.rendered || st.rendering || !RD.pdf || !right || !right.clientWidth) return;
    st.rendering = true;
    var gen = TRENDER.gen;
    try {
      var page = st.page || await RD.pdf.getPage(no);
      if (gen !== TRENDER.gen || RD.tpages[no] !== st) return;
      st.page = page;
      var base = page.getViewport({ scale: 1 });
      var availW = Math.max(240, Math.min(1000, right.clientWidth - 36));
      var scale = availW / base.width * RD.zoom;
      var dpr = Math.min(2, window.devicePixelRatio || 1);
      var vp = page.getViewport({ scale: scale * dpr });
      var cap = 4096;
      if (vp.width > cap || vp.height > cap) {
        dpr *= cap / Math.max(vp.width, vp.height);
        vp = page.getViewport({ scale: scale * dpr });
      }
      st.canvas.width = Math.max(1, Math.floor(vp.width));
      st.canvas.height = Math.max(1, Math.floor(vp.height));
      st.canvas.style.width = Math.floor(vp.width / dpr) + 'px';
      st.canvas.style.height = Math.floor(vp.height / dpr) + 'px';
      st.el.style.aspectRatio = '';
      st.el.style.width = Math.floor(vp.width / dpr) + 'px';
      var task = page.render({ canvasContext: st.canvas.getContext('2d'), viewport: vp });
      st.task = task;
      await task.promise;
      st.task = null;
      if (gen !== TRENDER.gen || RD.tpages[no] !== st) return;
      st.scale = scale;
      st.rendered = true;
      st.el.classList.remove('loading');
      renderTransOverlay(no);
      releaseFarTransPages(no);
    } catch (e) {
      if (e && e.name === 'RenderingCancelledException') return;
      st.fail = (st.fail || 0) + 1;
    } finally {
      st.rendering = false;
      if (!st.rendered && gen === TRENDER.gen && (st.fail || 0) < 4) {
        setTimeout(function () { queueTransRender(no); }, 120 * (st.fail || 1));
      }
    }
  }

  function releaseFarTransPages(cur) {
    Object.keys(RD.tpages).forEach(function (n) {
      var st = RD.tpages[n];
      if (!st || !st.rendered || Math.abs(Number(n) - cur) <= 6) return;
      st.rendered = false;
      st.canvas.width = 1;
      st.canvas.height = 1;
      st.layer.innerHTML = '';
      st.el.classList.add('loading');
    });
  }

  /* 译页布局：版式计算在 /static/transpage.js（纯函数可单测）。
     这里只负责把「本页所有 block.parts」收集成渲染项，并给出版面障碍物。 */
  function pageTransGeo(pageNo) {
    var all = [], obstacles = [];
    var blocks = (RD.doc && RD.doc.blocks) || [];
    for (var i = 0; i < blocks.length; i++) {
      var b = blocks[i];
      if (!b) continue;
      var solid = b.kind === 'figure' || b.kind === 'formula' || b.kind === 'table';
      var parts = b.parts || [];
      var hit = false;
      for (var j = 0; j < parts.length; j++) {
        var p = parts[j];
        if (!p || Number(p.page) !== Number(pageNo) || !p.rect || p.rect.length !== 4) continue;
        all.push(p.rect);
        if (solid) obstacles.push(p.rect);
        hit = true;
      }
      if (!hit && Number(b.page) === Number(pageNo) && b.rect && b.rect.length === 4) {
        all.push(b.rect);
        if (solid) obstacles.push(b.rect);
      }
    }
    return { all: all, obstacles: obstacles };
  }

  function renderTransOverlay(pageNo) {
    var st = RD.tpages[pageNo];
    if (!st || !st.rendered || !window.RDTransPage) return;
    var blocks = (RD.doc && RD.doc.blocks) || [];
    var trs = RD.translations || {};
    var items = [];
    for (var i = 0; i < blocks.length; i++) {
      var b = blocks[i];
      if (!b || (b.kind !== 'text' && b.kind !== 'list' && b.kind !== 'heading')) continue;
      var tr = trs[b.id];
      if (!tr || !String(tr).trim()) continue;
      var rect = null;
      var parts = b.parts || [];
      for (var j = 0; j < parts.length; j++) {
        if (Number(parts[j].page) === Number(pageNo)) { rect = parts[j].rect; break; }
      }
      if (!rect && Number(b.page) === Number(pageNo)) rect = b.rect;
      if (!rect) continue;
      RD.blockById[b.id] = b;
      items.push({ block: b, rect: rect, text: tr });
    }
    if (!items.length) { st.layer.innerHTML = ''; RD.tblockEls[pageNo] = {}; return; }
    var geo = pageTransGeo(pageNo);
    RD.tblockEls[pageNo] = {};
    window.RDTransPage.render(st.layer, {
      pageNo: pageNo, size: pageSize(pageNo), canvas: st.canvas,
      items: items, obstacles: geo.obstacles, pageRects: geo.all,
      formatInline: formatInline,
      onReady: function (els) {
        RD.tblockEls[pageNo] = els || {};
        applyPageAnnos(pageNo);
      },
    });
  }

  /* 译页模式的批注：把高亮/角标贴到覆盖层文字上。
     原文侧（side=src）在译页没有可高亮的文本层，只挂角标。 */
  function applyPageAnnos(pageNo) {
    var boxes = (RD.tblockEls || {})[pageNo];
    if (!boxes) return;
    var by = {};
    var no = annoNumbers();
    (RD.annos || []).forEach(function (a) {
      if (!a.block_id || Number(a.page) !== Number(pageNo)) return;
      (by[a.block_id] = by[a.block_id] || []).push(a);
    });
    Object.keys(boxes).forEach(function (id) {
      var box = boxes[id];
      if (!box) return;
      var textEl = box.querySelector('.rd-ttext');
      clearBlockAnnos(box);        // 角标/高亮都挂在 box 内，统一清一次
      var list = by[id] || [];
      if (!list.length) return;
      box.classList.add('has-anno');
      var edge = [];
      list.forEach(function (a) {
        var host = a.side === 'dst' ? textEl : null;
        var mark = (host && a.quote) ? highlightIn(host, a.quote, a.color) : null;
        var dot = document.createElement('button');
        dot.type = 'button';
        dot.className = 'rd-anno-dot';
        dot.textContent = String(no[a.id] || '');
        dot.title = kindLabel(a) + '：' + String(annoTitle(a)).slice(0, 60);
        dot.onclick = function (e) {
          e.stopPropagation();
          e.preventDefault();
          showReaderAnno(list, dot);
        };
        if (mark) {
          mark.classList.add('rd-hl-dot');
          mark.appendChild(dot);
        } else {
          edge.push(dot);
        }
      });
      edge.forEach(function (dot, i) {
        dot.classList.add('rd-anno-dot-edge');
        dot.style.top = (2 + i * 21) + 'px';
        box.appendChild(dot);
      });
    });
  }

  function scheduleTransOverlay(pageNo) {
    var st = RD.tpages[pageNo];
    if (!st || !st.rendered) return;
    clearTimeout(TRENDER.timers[pageNo]);
    TRENDER.timers[pageNo] = setTimeout(function () { renderTransOverlay(pageNo); }, 80);
  }

  function visibleTransPages() {
    var right = $('.rd-right');
    var out = [];
    if (!right || !right.clientWidth) return out;
    var top = right.scrollTop, bottom = top + right.clientHeight;
    Object.keys(RD.tpages).forEach(function (n) {
      var st = RD.tpages[n];
      var y = st.el.offsetTop;
      if (y < bottom + 900 && y + st.el.offsetHeight > top - 900) out.push(Number(n));
    });
    return out;
  }

  function rerenderTransVisible() {
    TRENDER.gen++;
    TRENDER.queue.length = 0;
    Object.keys(RD.tpages).forEach(function (n) {
      var st = RD.tpages[n];
      if (!st || !st.rendered) return;
      st.rendered = false;
      st.canvas.width = 1;
      st.canvas.height = 1;
      st.layer.innerHTML = '';
      st.el.classList.add('loading');
    });
    visibleTransPages().forEach(queueTransRender);
  }

  var pageObserver = null;
  function observePages() {
    if (pageObserver) pageObserver.disconnect();
    pageObserver = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (!en.isIntersecting) return;
        var no = Number(en.target.dataset.page);
        queueRender(no);
        // 预取下一页，滚动时更跟手
        if (no < (RD.doc && RD.doc.parsed_pages || 0)) queueRender(no + 1);
      });
    }, { root: $('.rd-left'), rootMargin: '800px 0px' });
    Object.keys(RD.pages).forEach(function (n) { pageObserver.observe(RD.pages[n].el); });
  }

  function queueRender(no) {
    var st = RD.pages[no];
    if (!st || st.rendered || st.rendering) return;
    if (RENDER.queue.indexOf(no) >= 0) return;
    RENDER.queue.push(no);
    pumpRender();
  }

  function pumpRender() {
    while (RENDER.active < RENDER.max && RENDER.queue.length) {
      var no = RENDER.queue.shift();
      var st = RD.pages[no];
      if (!st || st.rendered || st.rendering) continue;
      RENDER.active++;
      // 两个分支都要回减：renderPage 万一意外 reject 时不回减，active 会永久卡在 max，
      // 之后所有页都停在骨架屏（只能关掉阅读器重开）。
      renderPage(no).then(function () {
        RENDER.active--;
        pumpRender();
      }, function () {
        RENDER.active--;
        pumpRender();
      });
    }
  }

  /* 二维矩阵相乘（[a,b,c,d,e,f]），等价 pdf.js 的 Util.transform */
  function mul(m1, m2) {
    return [
      m1[0] * m2[0] + m1[2] * m2[1],
      m1[1] * m2[0] + m1[3] * m2[1],
      m1[0] * m2[2] + m1[2] * m2[3],
      m1[1] * m2[2] + m1[3] * m2[3],
      m1[0] * m2[4] + m1[2] * m2[5] + m1[4],
      m1[1] * m2[4] + m1[3] * m2[5] + m1[5],
    ];
  }

  /* 归一化字符索引（pt 坐标，与缩放无关）：norm 里第 i 个字符对应 chars[i] */
  function makeIndex(tc, baseVp) {
    var norm = '', chars = [], rotated = false;
    (tc.items || []).forEach(function (item) {
      if (!item.str) return;
      var tx = mul(baseVp.transform, item.transform);
      if (Math.abs(Math.atan2(tx[1], tx[0])) > 0.01) rotated = true;
      var h = Math.hypot(tx[2], tx[3]);
      var left = tx[4], top = tx[5] - h;
      var w = Number(item.width) || 0;
      var n = item.str.length;
      var step = n ? w / n : 0;
      for (var i = 0; i < n; i++) {
        var ch = item.str.charAt(i);
        if (/[\s\u200b-\u200d\ufeff\u00ad]/.test(ch)) continue;
        var low = ch.toLowerCase();     // 小写可能变长（如 'İ'），norm 与 chars 必须逐字符对齐
        for (var j = 0; j < low.length; j++) {
          chars.push({ x: left + step * i, y: top, w: step, h: h });
          norm += low.charAt(j);
        }
      }
    });
    return { norm: norm, chars: chars, rotated: rotated };
  }

  async function ensureTextData(no) {
    var st = RD.pages[no];
    if (!st) return null;
    if (st.index) return st.index;
    if (!RD.pdf) return null;
    try {
      if (!st.page) st.page = await RD.pdf.getPage(no);
      if (!st.tc) st.tc = await st.page.getTextContent();
      if (!st.index) st.index = makeIndex(st.tc, st.page.getViewport({ scale: 1 }));
      return st.index;
    } catch (e) {
      return null;
    }
  }

  /* 把透明文字铺到画布上：位置/字号来自 pdf.js，宽度按 item.width 做 scaleX 对齐 */
  function buildTextLayerDom(no) {
    var st = RD.pages[no];
    if (!st || !st.tc || !st.scale || !st.page) return;
    var vp = st.page.getViewport({ scale: st.scale });
    var items = st.tc.items || [];
    var styles = st.tc.styles || {};
    var frag = document.createDocumentFragment();
    var spans = [];
    items.forEach(function (item) {
      if (!item.str) return;
      var tx = mul(vp.transform, item.transform);
      var h = Math.hypot(tx[2], tx[3]);
      if (!h) return;
      var span = document.createElement('span');
      span.textContent = item.str;
      span.style.left = tx[4] + 'px';
      span.style.top = (tx[5] - h) + 'px';
      span.style.fontSize = h + 'px';
      var fam = (styles[item.fontName] || {}).fontFamily;
      if (fam) span.style.fontFamily = fam;
      frag.appendChild(span);
      spans.push({ span: span, want: (Number(item.width) || 0) * st.scale });
    });
    st.textEl.innerHTML = '';
    st.textEl.appendChild(frag);
    // offsetWidth 会强制一次布局：只在本页可见时调用，代价可控
    spans.forEach(function (it) {
      var got = it.span.offsetWidth || 0;
      if (it.want > 0 && got > 1 && Math.abs(it.want - got) > 0.6) {
        it.span.style.transform = 'scaleX(' + (it.want / got) + ')';
      }
    });
  }

  function releasePage(st) {
    if (!st) return;
    if (st.task) { try { st.task.cancel(); } catch (e) { /* 忽略 */ } st.task = null; }
    st.rendered = false;
    st.scale = 0;
    try { st.canvas.width = 0; st.canvas.height = 0; } catch (e) { /* 忽略 */ }
    if (st.textEl) st.textEl.innerHTML = '';
    if (st.hlEl) st.hlEl.innerHTML = '';
    st.el.classList.add('loading');
    st.el.style.aspectRatio = (st.baseW || 595) + ' / ' + (st.baseH || 842);
    st.el.style.width = '';       // 回到占位尺寸，等下次渲染按新宽度撑开
    try { if (st.page) st.page.cleanup(); } catch (e) { /* 忽略 */ }
    st.page = null;               // 文本索引 / tc 由 releaseFarPages 按距离回收，回来时 ensureTextData 重建
  }

  function releaseFarPages(center) {
    Object.keys(RD.pages).forEach(function (k) {
      var n = Number(k);
      var d = Math.abs(n - center);
      var st = RD.pages[n];
      if (!st) return;
      if (d > 6 && st.rendered) releasePage(st);
      // norm + chars 是「每字符一个对象」：1000 页文档全留着会到几百 MB，
      // 离得足够远就丢掉；联动 / 查找需要时会经 ensureTextData 重新提取。
      if (d > 12 && (st.index || st.tc)) { st.index = null; st.tc = null; }
    });
  }

  async function renderPage(no) {
    var st = RD.pages[no];
    if (!st || st.rendered || st.rendering || !RD.pdf) return;
    var left = $('.rd-left');
    if (!left || !left.clientWidth) return;
    st.rendering = true;
    var gen = RENDER.gen;
    try {
      var page = st.page || await RD.pdf.getPage(no);
      if (gen !== RENDER.gen || RD.pages[no] !== st) return;
      st.page = page;
      var base = page.getViewport({ scale: 1 });
      var availW = Math.max(180, left.clientWidth - 28);
      var availH = Math.max(180, left.clientHeight - 28);
      var fit;
      if (RD.zoomMode === 'page') fit = Math.min(availW / base.width, availH / base.height);
      else if (RD.zoomMode === 'actual') fit = 96 / 72;      // PDF pt → CSS px（96dpi）
      else fit = availW / base.width;
      var scale = fit * RD.zoom;
      var dpr = Math.min(2, window.devicePixelRatio || 1);
      var vp = page.getViewport({ scale: scale * dpr });
      // 单页像素上限：超大页别把显存吃穿
      var cap = 4096;
      if (vp.width > cap || vp.height > cap) {
        var k = cap / Math.max(vp.width, vp.height);
        dpr *= k;
        vp = page.getViewport({ scale: scale * dpr });
      }
      st.canvas.width = Math.max(1, Math.floor(vp.width));
      st.canvas.height = Math.max(1, Math.floor(vp.height));
      st.canvas.style.width = Math.floor(vp.width / dpr) + 'px';
      st.canvas.style.height = Math.floor(vp.height / dpr) + 'px';
      st.el.style.aspectRatio = '';
      st.el.style.width = Math.floor(vp.width / dpr) + 'px';
      // 文本层只依赖文字内容与缩放，不依赖画布像素：先铺好，切换模式/缩放时选中不会中断
      st.scale = scale;
      if (st.tc) buildTextLayerDom(no);
      var task = page.render({ canvasContext: st.canvas.getContext('2d'), viewport: vp });
      st.task = task;
      await task.promise;
      st.task = null;
      if (gen !== RENDER.gen || RD.pages[no] !== st) return;
      st.fail = 0;
      st.scale = scale;
      st.rendered = true;
      st.el.classList.remove('loading');
      await ensureTextData(no);
      if (gen !== RENDER.gen || !st.rendered) return;
      if (Math.abs((st.scale || 0) - scale) < 0.0001 && !st.textEl.childElementCount) buildTextLayerDom(no);
      paintLivePage(no);
      releaseFarPages(no);
    } catch (e) {
      // 上一轮被取消的渲染可能还占着同一块 canvas（pdf.js 会直接抛错）。
      // 计数后延迟重试，避免页面永远停在骨架。
      if (e && e.name === 'RenderingCancelledException') return;
      st.fail = (st.fail || 0) + 1;
    } finally {
      st.rendering = false;
      if (!st.rendered) {
        if (gen !== RENDER.gen) queueRender(no);
        else if ((st.fail || 0) < 4) setTimeout(function () { queueRender(no); }, 80 * (st.fail || 1));
      }
    }
  }

  function visiblePages() {
    var left = $('.rd-left');
    var out = [];
    if (!left) return out;
    var top = left.scrollTop, bottom = top + left.clientHeight;
    Object.keys(RD.pages).forEach(function (n) {
      var st = RD.pages[n];
      var y = st.el.offsetTop;
      if (y < bottom + 800 && y + st.el.offsetHeight > top - 800) out.push(Number(n));
    });
    return out;
  }

  function rerenderVisible() {
    RENDER.gen++;                    // 作废在途渲染
    RENDER.queue.length = 0;
    visiblePages().forEach(function (n) {
      var st = RD.pages[n];
      if (!st) return;
      if (st.rendered) releasePage(st);
      queueRender(n);
    });
  }

  /* 左栏宽度 / 缩放变了：120ms 防抖，只重排可视区 */
  function resizePages() {
    clearTimeout(RENDER.timer);
    RENDER.timer = setTimeout(function () {
      if (!RD.pdf || RD.fileType !== 'pdf') return;
      var left = $('.rd-left');
      if (!left || !left.clientWidth) {           // 左栏被隐藏时不要按 0 宽渲染
        if (RD.mode === 'page') rerenderTransVisible();
        return;
      }
      rerenderVisible();
      if (RD.mode === 'page' || RD.mode === 'page-dual') rerenderTransVisible();
    }, 120);
  }

  function setZoom(v) {
    RD.zoom = Math.max(0.5, Math.min(3, v));
    applyPrefs();
    resizePages();
  }

  function setZoomMode(mode) {
    if (['width', 'page', 'actual'].indexOf(mode) < 0) mode = 'width';
    RD.zoomMode = mode;
    RD.zoom = 1;
    prefs.zoomMode = mode;
    savePrefs();
    applyPrefs();
    resizePages();
  }

  function onLeftScroll() {
    if (RD.leftRaf) return;
    RD.leftRaf = requestAnimationFrame(function () {
      RD.leftRaf = 0;
      var left = $('.rd-left');
      if (!left) return;
      var top = left.scrollTop + 60;
      var best = RD.cur, bestD = 1e9;
      Object.keys(RD.pages).forEach(function (n) {
        var d = Math.abs(RD.pages[n].el.offsetTop - top);
        if (d < bestD) { bestD = d; best = Number(n); }
      });
      if (best !== RD.cur) {
        RD.cur = best;
        markActivePage();
        updatePageInd();
        rdEnsurePages([best]);
        notePos(best);
      }
      savePosSoon();      // 原页模式下只有左栏在滚，位置也要记下来
    });
  }

  function markActivePage() {
    Object.keys(RD.pages).forEach(function (n) {
      RD.pages[n].el.classList.toggle('on', Number(n) === RD.cur);
    });
    markThumbActive();
  }

  /* ---------------- 联动高亮（仅临时，不落库） ----------------
     先画 block 级矩形保证 <16ms 有反馈，再异步用 PageIndex 精修到字符级。 */
  function liveRectOfBlock(blockId) {
    var b = blockOf(blockId);
    if (!b || !b.rect || b.rect.length !== 4) return [];
    return [{ x: b.rect[0], y: b.rect[1], w: b.rect[2] - b.rect[0], h: b.rect[3] - b.rect[1] }];
  }

  function rectsForRange(idx, start, end) {
    var rects = [], cur = null;
    for (var i = start; i < end && i < idx.chars.length; i++) {
      var c = idx.chars[i];
      if (cur && Math.abs(c.y - cur.y) < c.h * 0.6 && c.x <= cur.x + cur.w + 1.5) {
        cur.w = Math.max(cur.x + cur.w, c.x + c.w) - cur.x;
        cur.h = Math.max(cur.h, c.h);
      } else {
        cur = { x: c.x, y: c.y, w: c.w, h: c.h };
        rects.push(cur);
      }
    }
    return rects;
  }

  /* 在页索引里找 quote 的所有命中，用 block 的位置消歧（重复页眉/常见词） */
  function findRectsInIndex(idx, quote, blockId) {
    if (!idx || idx.rotated) return null;
    var q = normStr(quote);
    if (q.length < 2) return null;
    var hits = [], pos = idx.norm.indexOf(q);
    while (pos >= 0 && hits.length < 40) {
      hits.push(pos);
      pos = idx.norm.indexOf(q, pos + 1);
    }
    if (!hits.length) return null;
    var best = hits[0];
    if (hits.length > 1) {
      var b = blockOf(blockId);
      if (b && b.rect) {
        var bestD = Infinity;
        hits.forEach(function (p) {
          var c = idx.chars[p];
          if (!c) return;
          var d = Math.abs(c.y - b.rect[1]) + Math.abs(c.x - b.rect[0]) * 0.2;
          if (d < bestD) { bestD = d; best = p; }
        });
      }
    }
    return rectsForRange(idx, best, best + q.length);
  }

  function paintLivePage(no) {
    var st = RD.pages[no];
    if (!st || !st.hlEl) return;
    st.hlEl.innerHTML = '';
    var live = RD.live;
    if (!live.on || live.page !== no || !st.scale) return;
    var scale = st.scale;
    live.rects.forEach(function (r) {
      var d = document.createElement('div');
      d.className = 'rd-live-rect';
      d.style.left = (r.x * scale) + 'px';
      d.style.top = (r.y * scale) + 'px';
      d.style.width = Math.max(1, r.w * scale) + 'px';
      d.style.height = Math.max(1, r.h * scale) + 'px';
      st.hlEl.appendChild(d);
    });
  }

  function clearLive() {
    RD.live.on = false;
    RD.live.page = 0;
    RD.live.rects = [];
    RD.live.blockId = '';
    RD.live.quote = '';
    Object.keys(RD.pages).forEach(function (n) {
      var st = RD.pages[n];
      if (st && st.hlEl) st.hlEl.innerHTML = '';
    });
    Array.prototype.forEach.call(document.querySelectorAll('mark.rd-hl-live'), function (m) {
      // 旧联动的 mark 正好压在当前选区上时先留着：拆节点会把选区弄丢
      var s = window.getSelection();
      if (s && !s.isCollapsed && s.rangeCount) {
        try { if (s.getRangeAt(0).intersectsNode(m)) return; } catch (e) { /* 忽略 */ }
      }
      var p = m.parentNode;
      if (!p) return;
      while (m.firstChild) p.insertBefore(m.firstChild, m);
      p.removeChild(m);
      p.normalize();
    });
  }

  function liveToPage(page, rects, blockId, quote) {
    clearLive();
    RD.live.on = true;
    RD.live.page = page;
    RD.live.rects = rects || [];
    RD.live.blockId = blockId || '';
    RD.live.quote = quote || '';
    paintLivePage(page);
  }

  /* 在阅读流里给 quote 套一层临时高亮（复用 highlightIn，用 'live' 这个特殊色名） */
  function paintLiveFlow(blockId, quote) {
    var el = RD.blockEls[blockId];
    if (!el || !quote) return;
    var host = el.querySelector('.rd-src');
    if (!host || host.offsetParent === null) return;   // 只看译文时 src 是隐藏的
    // 选区就在这段原文里时不要再套 mark：DOM 一变浏览器立刻丢掉选区，
    // selectionchange 会把刚弹出来的浮层关掉（原生选区本身已经是高亮）。
    var s = window.getSelection();
    if (s && !s.isCollapsed && s.rangeCount
      && host.contains(s.getRangeAt(0).commonAncestorContainer)) return;
    highlightIn(host, quote, 'live');
  }

  function scrollFlowToBlock(blockId) {
    var el = RD.blockEls[blockId];
    var flow = document.querySelector('.rd-right');
    if (!el || !flow) return;
    flow.scrollTo({ top: Math.max(0, el.offsetTop - 80), behavior: 'smooth' });
  }

  /* 右 → 左：先块级，再异步精修字符级 */
  function liveFromBlock(page, blockId, quote) {
    if (!page || !quote) return;
    liveToPage(page, liveRectOfBlock(blockId), blockId, quote);
    paintLiveFlow(blockId, quote);
    scrollFlowToBlock(blockId);
    var want = quote;
    ensureTextData(page).then(function (idx) {
      if (!RD.live.on || RD.live.page !== page || RD.live.quote !== want) return;
      var rr = findRectsInIndex(idx, want, blockId);
      if (!rr || !rr.length) return;
      RD.live.rects = rr;
      paintLivePage(page);
    });
  }

  /* 左 → 右：左栏选区先按页定位，再找包含这段文字的 block */
  function findBlockForQuote(no, quote) {
    var q = normStr(quote);
    if (!q) return null;
    var list = (RD.doc && RD.doc.blocks) || [];
    var best = null, bestScore = 0;
    list.forEach(function (b) {
      if (b.page !== no || !b.text) return;
      var nb = normStr(b.text);
      if (!nb) return;
      if (nb.indexOf(q) >= 0) { best = b; bestScore = 1e9; return; }
      var head = q.slice(0, 10), tail = q.slice(-10);
      var sc = 0;
      if (head && nb.indexOf(head) >= 0) sc += head.length;
      if (tail && nb.indexOf(tail) >= 0) sc += tail.length;
      if (sc > bestScore) { bestScore = sc; best = b; }
    });
    return best;
  }

  /* 按几何位置找同一页上最近的块（x / y 是 PDF pt 坐标） */
  function findBlockNear(no, x, y) {
    var list = (RD.doc && RD.doc.blocks) || [];
    var best = null, bestD = Infinity;
    list.forEach(function (b) {
      if (b.page !== no || !b.rect || b.rect.length !== 4) return;
      var dx = Math.max(b.rect[0] - x, 0, x - b.rect[2]);
      var dy = Math.max(b.rect[1] - y, 0, y - b.rect[3]);
      var d = dx * dx + dy * dy;
      if (d < bestD) { bestD = d; best = b; }
    });
    return best;
  }

  function linkFromFlowSelection() {
    if (RD.fileType !== 'pdf' || !sel.page || !sel.text) return;
    // 译文侧选中的是译文，左栏 PDF 里只有原文：用块原文去定位 / 高亮
    var b = blockOf(sel.blockId);
    var quote = (sel.side === 'dst' && b && b.text) ? String(b.text) : sel.text;
    liveFromBlock(sel.page, sel.blockId, quote);
  }

  /* 左 → 右：原页选区按 block 定位，先画块级矩形，再异步精修到字符级 */
  function linkToFlow(no, quote, b) {
    if (RD.fileType !== 'pdf' || !no || !quote) return;
    liveToPage(no, b ? liveRectOfBlock(b.id) : [], b ? b.id : '', quote);
    if (b) {
      paintLiveFlow(b.id, quote);
      scrollFlowToBlock(b.id);
    }
    var want = quote;
    ensureTextData(no).then(function (idx) {
      if (!RD.live.on || RD.live.page !== no || RD.live.quote !== want) return;
      var rr = findRectsInIndex(idx, want, b ? b.id : '');
      if (!rr || !rr.length) return;
      RD.live.rects = rr;
      paintLivePage(no);
    });
  }

  /* 右 → 左：译页 / 译文划词，用对应块的原文在左栏找位置（原文可能为空，只画块级） */
  function linkToLeftPage(no, blockId, quote) {
    if (RD.fileType !== 'pdf' || !no) return;
    var q = String(quote || '');
    liveToPage(no, liveRectOfBlock(blockId), blockId, q);
    if (!q) return;
    ensureTextData(no).then(function (idx) {
      if (!RD.live.on || RD.live.page !== no || RD.live.quote !== q) return;
      var rr = findRectsInIndex(idx, q, blockId);
      if (!rr || !rr.length) return;
      RD.live.rects = rr;
      paintLivePage(no);
    });
  }

  /* ---------------- 阅读位置历史（Alt+← 回退） ---------------- */
  function notePos(page) {
    if (!page) return;
    var h = RD.history;
    if (h[h.length - 1] === page) return;
    h.push(page);
    if (h.length > 20) h.shift();
  }

  function historyBack() {
    var h = RD.history;
    if (h.length < 2) return;
    h.pop();                       // 当前页
    var p = h.pop();
    if (p) goPage(p);
  }

  /* ---------------- 全文查找 ---------------- */
  var FIND = { q: '', items: [], idx: -1, timer: 0 };

  function toggleFind(force) {
    var box = document.querySelector('.rd-find');
    if (!box) return;
    var show = typeof force === 'boolean' ? force : box.classList.contains('hidden');
    box.classList.toggle('hidden', !show);
    if (show) {
      var inp = $('.rd-find-input', box);
      if (inp) { inp.focus(); inp.select(); if (inp.value.trim()) findSchedule(); }
    } else {
      clearLive();
    }
  }

  function findSchedule() {
    clearTimeout(FIND.timer);
    FIND.timer = setTimeout(findRun, 200);
  }

  async function findRun() {
    var inp = document.querySelector('.rd-find-input');
    if (!inp || !RD.doc) return;
    var q = inp.value.trim();
    FIND.q = q;
    FIND.items = [];
    FIND.idx = -1;
    FIND.total = 0;
    if (!q) { renderFindList(); return; }
    var r = null;
    try {
      r = await api('/api/reader/search', { session_id: sessionId(), path: RD.rel, q: q });
    } catch (e) { r = null; }
    if (!r || !r.ok) { renderFindList('查找失败'); return; }
    FIND.items = r.items || [];
    FIND.total = r.total || FIND.items.length;
    FIND.idx = FIND.items.length ? 0 : -1;
    renderFindList();
    if (FIND.items.length) findGoto(0);
  }

  function renderFindList(err) {
    var box = document.querySelector('.rd-find');
    if (!box) return;
    var stat = $('.rd-find-stat', box);
    if (stat) {
      if (err) stat.textContent = err;
      else if (!FIND.q) stat.textContent = '';
      else if (!FIND.items.length) stat.textContent = '无结果';
      else stat.textContent = (FIND.idx + 1) + '/' + FIND.items.length;
    }
    var list = $('.rd-find-list', box);
    if (list) {
      list.innerHTML = FIND.items.map(function (it, i) {
        return '<div class="rd-find-row' + (i === FIND.idx ? ' on' : '') + '" data-find-i="' + i + '">' +
          '<span class="rd-find-p">p.' + it.page + '</span>' +
          '<span class="rd-find-s">' + escapeHtml(it.snippet || it.quote || '') + '</span></div>';
      }).join('');
    }
    var full = $('.rd-find-more', box);
    if (!full && list && FIND.total > FIND.items.length) {
      var d = document.createElement('div');
      d.className = 'rd-find-more';
      d.textContent = '仅显示前 ' + FIND.items.length + ' 条（共 ' + FIND.total + ' 处）';
      list.parentNode.insertBefore(d, list.nextSibling);
    }
  }

  function findStep(dir) {
    if (!FIND.items.length) { findSchedule(); return; }
    var i = (FIND.idx + dir + FIND.items.length) % FIND.items.length;
    findGoto(i);
  }

  function findGoto(i) {
    var it = FIND.items[i];
    if (!it) return;
    FIND.idx = i;
    renderFindList();
    goPage(it.page);
    liveFromBlock(it.page, it.block_id, it.quote);
  }

  /* ---------------- 缩略图（懒加载 + LRU 60） ---------------- */
  var THUMB = { observer: null };

  function toggleThumbs(force) {
    var box = document.querySelector('.rd-thumbs');
    if (!box) return;
    var show = typeof force === 'boolean' ? force : box.classList.contains('hidden');
    box.classList.toggle('hidden', !show);
    RD.thumbsOn = show;
    var btn = document.querySelector('[data-act="thumbs"]');
    if (btn) btn.classList.toggle('on', show);
    if (show) {
      buildThumbs();
      setTimeout(function () { markThumbActive(true); resizePages(); }, 40);
    } else {
      setTimeout(resizePages, 60);
    }
  }

  function buildThumbs() {
    var inner = document.querySelector('.rd-thumbs-inner');
    if (!inner || !RD.doc) return;
    var n = RD.doc.parsed_pages || 0;
    var key = (RD.doc.key || RD.rel) + ':' + n;
    if (inner.dataset.doc === key) return;
    inner.dataset.doc = key;
    inner.innerHTML = '';
    var frag = document.createDocumentFragment();
    for (var i = 1; i <= n; i++) {
      var size = pageSize(i);
      var el = document.createElement('div');
      el.className = 'rd-thumb';
      el.dataset.page = String(i);
      el.style.aspectRatio = size.w + ' / ' + size.h;
      el.title = '第 ' + i + ' 页';
      el.onclick = function () { goPage(Number(this.dataset.page)); };
      frag.appendChild(el);
    }
    inner.appendChild(frag);
    if (THUMB.observer) THUMB.observer.disconnect();
    THUMB.observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (en.isIntersecting) renderThumb(Number(en.target.dataset.page));
      });
    }, { root: inner.parentElement, rootMargin: '300px 0px' });
    Array.prototype.forEach.call(inner.children, function (el) { THUMB.observer.observe(el); });
  }

  async function renderThumb(no) {
    if (!RD.pdf) return;
    var el = document.querySelector('.rd-thumb[data-page="' + no + '"]');
    if (!el || el.dataset.done === '1') return;
    el.dataset.done = '1';
    try {
      var page = await RD.pdf.getPage(no);
      var vp1 = page.getViewport({ scale: 1 });
      var vp = page.getViewport({ scale: 104 / vp1.width });
      var cv = document.createElement('canvas');
      cv.width = Math.ceil(vp.width);
      cv.height = Math.ceil(vp.height);
      await page.render({ canvasContext: cv.getContext('2d'), viewport: vp }).promise;
      el.innerHTML = '';
      el.appendChild(cv);
      RD.thumbs[no] = cv;
      RD.thumbOrder.push(no);
      while (RD.thumbOrder.length > 60) {
        var old = RD.thumbOrder.shift();
        var oc = RD.thumbs[old];
        delete RD.thumbs[old];
        if (oc) { try { oc.width = 0; oc.height = 0; } catch (e) { /* 忽略 */ } }
        var oe = document.querySelector('.rd-thumb[data-page="' + old + '"]');
        if (oe) { oe.innerHTML = ''; oe.dataset.done = ''; }
      }
    } catch (e) {
      el.dataset.done = '';       // 失败允许重试
    }
  }

  function markThumbActive(scroll) {
    var inner = document.querySelector('.rd-thumbs-inner');
    if (!inner) return;
    var box = inner.parentElement;
    if (!box || box.classList.contains('hidden')) return;
    Array.prototype.forEach.call(inner.querySelectorAll('.rd-thumb'), function (el) {
      el.classList.toggle('on', Number(el.dataset.page) === RD.cur);
    });
    if (scroll) {
      var cur = inner.querySelector('.rd-thumb.on');
      if (cur) cur.scrollIntoView({ block: 'nearest' });
    }
  }

  /* ------------------------------------------------------------ 右：阅读流 */

  /* 阅读流按页分块构建：1000 页 / 2.7 万段的文档不会把主线程一次卡死。
     默认「原页」模式根本不需要它，所以 open() 不再直接构建，切到原文/双语时再建。 */
  function ensureFlow() {
    if (RD.flowBuilt) return Promise.resolve();
    if (RD.flowBuilding) return RD.flowBuilding;
    RD.flowBuilding = renderFlow().finally(function () { RD.flowBuilding = null; });
    return RD.flowBuilding;
  }

  async function renderFlow() {
    var flow = $('.rd-flow');
    flow.innerHTML = '';
    RD.blockById = {};
    RD.blockEls = {};
    RD.totalBlocks = 0;
    RD.doneBlocks = 0;
    var byPage = {};
    (RD.doc.blocks || []).forEach(function (b) {
      (byPage[b.page] = byPage[b.page] || []).push(b);
    });
    var scan = {};
    (RD.doc.scan_pages || []).forEach(function (p) { scan[p] = 1; });
    var pages = RD.doc.parsed_pages || 0;
    var statEl = $('.rd-stat');

    function addMark(host, page) {
      var m = document.createElement('div');
      m.className = 'rd-mark';
      m.dataset.page = String(page);
      m.innerHTML = '<span>第 ' + page + ' 页</span>';
      host.appendChild(m);
    }

    function addBlock(host, b) {
      var el;
      if (b.kind === 'figure' || b.kind === 'formula') {
        el = document.createElement('figure');
        el.className = 'rd-fig' + (b.kind === 'formula' ? ' rd-eq' : '');
        var img = document.createElement('img');
        img.alt = b.kind === 'formula' ? '公式' : '图表';
        // 占位 1x1 透明图：懒加载完成前不显示浏览器的破图图标
        img.src = 'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==';
        img.dataset.cropPage = String(b.page);
        img.dataset.cropRect = (b.rect || []).join(',');
        el.appendChild(img);
        el.onclick = function () {
          if (el.classList.contains('ai')) return;   // 已转成 LaTeX，不需要放大截图
          el.classList.toggle('zoom');
        };
      } else if (b.kind === 'ref' || b.kind === 'table' || b.kind === 'meta') {
        // 参考文献 / 表格 / 作者单位：只显示原文，不翻译
        el = document.createElement('div');
        el.className = 'rd-block rd-raw' +
          (b.kind === 'ref' ? ' rd-ref' : '') +
          (b.kind === 'table' ? ' rd-table' : '') +
          (b.kind === 'meta' ? ' rd-meta' : '');
        var ref = document.createElement('div');
        ref.className = 'rd-src';
        ref.textContent = b.text || '';
        el.appendChild(ref);
      } else {
        el = document.createElement('div');
        el.className = 'rd-block rd-raw' +
          (b.kind === 'heading' ? ' rd-head' : '') +
          (b.kind === 'list' ? ' rd-list' : '') +
          (b.role ? ' rd-' + b.role : '');
        if (b.indent) el.style.setProperty('--rd-indent', String(b.indent));
        var src = document.createElement('div');
        src.className = 'rd-src';
        src.textContent = b.text || '';
        var dst = document.createElement('div');
        dst.className = 'rd-dst';
        el.appendChild(src);
        el.appendChild(dst);
        el.onclick = function () {
          if (el.dataset.state === 'fail') { translate([Number(el.dataset.page)]); return; }
          el.classList.toggle('show-src');
        };
      }
      el.id = 'rd-b-' + b.id;
      el.dataset.id = b.id;
      el.dataset.page = String(b.page);
      if (b.level) el.dataset.level = String(b.level);
      var plain = b.kind !== 'figure' && b.kind !== 'formula' && b.kind !== 'ref'
        && b.kind !== 'table' && b.kind !== 'meta';
      if (plain) el.dataset.state = 'pending';
      host.appendChild(el);
      RD.blockEls[b.id] = el;
      RD.blockById[b.id] = b;
      if (plain && (b.text || '').trim()) RD.totalBlocks++;
    }

    for (var p = 1; p <= pages; p++) {
      var holder = document.createElement('div');
      holder.className = 'rd-pageflow';
      holder.dataset.page = String(p);
      addMark(holder, p);
      var list = byPage[p] || [];
      if (!list.length) {
        if (scan[p]) {
          var note = document.createElement('div');
          note.className = 'rd-scan';
          note.textContent = '这一页没有可提取的文字层，请在左侧原页阅读。';
          holder.appendChild(note);
        }
      } else {
        list.forEach(function (b) { addBlock(holder, b); });
      }
      flow.appendChild(holder);
      if (p % 20 === 0) {
        if (statEl && !RD.streaming) statEl.textContent = '正在排版阅读流 ' + p + '/' + pages;
        await new Promise(function (res) { setTimeout(res, 0); });
      }
    }
    observeCrops();
    RD.flowBuilt = true;
    if (RD.translations && Object.keys(RD.translations).length) applyTranslations(RD.translations);
    if ((RD.annos || []).length) applyReaderAnnos();
    updateProgress();
    scrollFlowToPage(RD.cur, false);
  }

  function scrollFlowToPage(page, smooth) {
    var flow = $('.rd-right');
    var mark = flow && flow.querySelector('.rd-mark[data-page="' + page + '"]');
    if (mark) {
      flow.scrollTo({ top: Math.max(0, mark.offsetTop - 8), behavior: smooth === false ? 'auto' : 'smooth' });
    }
  }

  var cropObserver = null;
  function observeCrops() {
    if (cropObserver) cropObserver.disconnect();
    cropObserver = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (en.isIntersecting) loadCrop(en.target);
      });
    }, { root: $('.rd-right'), rootMargin: '500px 0px' });
    Array.prototype.forEach.call(document.querySelectorAll('.rd-fig img'), function (img) {
      cropObserver.observe(img);
    });
  }

  async function loadCrop(img) {
    if (img.dataset.loaded) return;
    img.dataset.loaded = '1';
    var page = Number(img.dataset.cropPage);
    var rect = (img.dataset.cropRect || '').split(',').map(Number);
    if (rect.length !== 4) return;
    var key = RD.doc.key + ':' + page + ':' + rect.join(',');
    if (RD.crops[key]) { img.src = RD.crops[key]; maybeFormula(img); return; }
    try {
      var res = await fetch('/api/reader/crop', {
        method: 'POST', headers: authHeaders(),
        body: JSON.stringify({ session_id: sessionId(), path: RD.rel, page: page, rect: rect }),
      });
      if (!res.ok) throw new Error('crop');
      var url = URL.createObjectURL(await res.blob());
      RD.crops[key] = url;
      img.src = url;
      maybeFormula(img);
    } catch (e) {
      img.closest('.rd-fig').classList.add('failed');
    }
  }

  /* 公式识别：裁图 → 视觉模型 → LaTeX → KaTeX 渲染；失败就保留图片 */
  function maybeFormula(img) {
    var fig = img.closest('.rd-fig');
    if (!fig || !fig.classList.contains('rd-eq')) return;
    if (!prefs.formula || RD.formulaAI === false) return;
    if (fig.dataset.ai) return;
    fig.dataset.ai = 'busy';
    RD.aiChain = (RD.aiChain || Promise.resolve()).then(function () {
      return recognizeFormula(fig);
    }).catch(function () { /* 单张失败不影响后续 */ });
  }

  function recognizeFormula(fig) {
    var img = fig.querySelector('img');
    if (!img) return Promise.resolve();
    var page = Number(img.dataset.cropPage);
    var rect = (img.dataset.cropRect || '').split(',').map(Number);
    if (rect.length !== 4) return Promise.resolve();
    return api('/api/reader/formula', {
      session_id: sessionId(), path: RD.rel, page: page, rect: rect,
    }).then(function (r) {
      if (!r || !r.ok || !r.latex) { fig.dataset.ai = 'fail'; return; }
      renderLatex(fig, r.latex);
    }).catch(function () { fig.dataset.ai = 'fail'; });
  }

  function renderLatex(fig, latex) {
    var span = fig.querySelector('.rd-katex');
    if (!span) {
      span = document.createElement('span');
      span.className = 'rd-katex';
      fig.appendChild(span);
    }
    if (window.katex) {
      try {
        // throwOnError=true：坏 LaTeX 直接抛错，保留截图；否则 KaTeX 会输出红色错误文本
        katex.render(latex, span, { displayMode: true, throwOnError: true, strict: false });
        if (span.querySelector('.katex-error')) throw new Error('katex-error');
        fig.dataset.latex = latex;
        fig.dataset.ai = 'done';
        fig.classList.add('ai');
        return;
      } catch (e) {
        span.textContent = '';
        fig.classList.remove('ai');    // 之前识别成功过：撤掉替代层，图片重新显示
        fig.dataset.ai = 'fail';       // 识别结果不可用：保留裁图，不显示红色错误
        return;
      }
    }
    var n = Number(fig.dataset.katexTry || 0);
    if (n > 12) { fig.dataset.ai = 'fail'; return; }
    fig.dataset.katexTry = n + 1;
    setTimeout(function () { renderLatex(fig, latex); }, 300);
  }

  var tScrollRaf = 0;
  function onTransScroll() {
    if (RD.jumpTarget || tScrollRaf) return;
    tScrollRaf = requestAnimationFrame(function () {
      tScrollRaf = 0;
      var right = $('.rd-right');
      if (!right) return;
      var top = right.scrollTop + 60;
      var best = RD.cur, bestD = 1e9;
      Object.keys(RD.tpages).forEach(function (n) {
        var d = Math.abs(RD.tpages[n].el.offsetTop - top);
        if (d < bestD) { bestD = d; best = Number(n); }
      });
      if (best !== RD.cur) {
        RD.cur = best;
        markActivePage();
        updatePageInd();
        notePos(best);
        rdEnsurePages([best]);
        if (RD.mode === 'page-dual') {
          var left = $('.rd-left');
          var st = RD.pages[best];
          if (left && st && Date.now() > (RD.leftManualUntil || 0)) {
            left.scrollTo({ top: Math.max(0, st.el.offsetTop - 12), behavior: 'smooth' });
          }
        }
      }
      savePosSoon();
    });
  }

  function onFlowScroll() {
    if (RD.mode === 'page' || RD.mode === 'page-dual') { onTransScroll(); return; }
    if (RD.jumpTarget) return;       // 程序化翻页动画中，别被中间帧改回旧页
    var flow = $('.rd-right');
    var top = flow.scrollTop + 80;
    var marks = flow.querySelectorAll('.rd-mark');
    var page = 1;
    for (var i = 0; i < marks.length; i++) {
      if (marks[i].offsetTop <= top) page = Number(marks[i].dataset.page);
      else break;
    }
    if (page !== RD.cur) {
      RD.cur = page;
      markActivePage();
      updatePageInd();
      notePos(page);
      var st = RD.pages[page];
      if (st) {
        var left = $('.rd-left');
        var y = st.el.offsetTop;
        // 用户刚手动滚过左栏就先别抢它
        if (Date.now() > (RD.leftManualUntil || 0) &&
            (y < left.scrollTop || y + st.el.offsetHeight > left.scrollTop + left.clientHeight)) {
          left.scrollTo({ top: Math.max(0, y - 12), behavior: 'smooth' });
        }
      }
      rdEnsurePages([page]);
    }
    savePosSoon();
  }

  function goPage(n) {
    if (!RD.doc) return;
    n = Math.max(1, Math.min(RD.doc.parsed_pages, n));
    RD.cur = n;
    RD.jumpTarget = n;
    notePos(n);
    markActivePage();
    updatePageInd();
    var flow = $('.rd-right');
    if (RD.mode === 'page' || RD.mode === 'page-dual') {
      var tp = RD.tpages[n];
      if (tp) flow.scrollTo({ top: Math.max(0, tp.el.offsetTop - 8), behavior: 'smooth' });
    } else {
      var mark = flow.querySelector('.rd-mark[data-page="' + n + '"]');
      if (mark) flow.scrollTo({ top: mark.offsetTop - 8, behavior: 'smooth' });
    }
    rdEnsurePages([n]);
    if (RD.fileType === 'pdf') {
      var st = RD.pages[n];
      if (st) $('.rd-left').scrollTo({ top: Math.max(0, st.el.offsetTop - 12), behavior: 'smooth' });
    }
    // 平滑滚动期间的中间帧会把页码判回旧页，动画结束后再放开滚动同步
    clearTimeout(RD._jumpTimer);
    RD._jumpTimer = setTimeout(function () {
      RD.jumpTarget = 0;
      RD.cur = n;
      markActivePage();
      updatePageInd();
    }, 600);
  }

  /* ------------------------------------------------------------ 模式与字号 */

  /* 上次用的分栏模式：从「原页」点翻译时用它恢复原文/双语/译文 */
  function savedSplitMode() {
    try {
      var m = localStorage.getItem('rd_mode');
      if (m === 'orig' || m === 'trans' || m === 'dual'
          || m === 'page' || m === 'page-dual') return m;
    } catch (e) { /* 忽略 */ }
    return 'dual';
  }

  /* 点「译本页 / 译全文」时要能看到译文：上次停在「原文」就退回双语 */
  function modeForTranslate() {
    var m = savedSplitMode();
    return m === 'orig' ? 'dual' : m;
  }

  function setMode(mode) {
    if (['pdf', 'orig', 'dual', 'trans', 'page', 'page-dual'].indexOf(mode) < 0) mode = 'dual';
    // Word / PPT 没有原页图像，版式模式退回阅读流
    if (RD.fileType !== 'pdf' && (mode === 'page' || mode === 'page-dual')) mode = 'dual';
    RD.mode = mode;
    RD.transOn = mode !== 'pdf';
    var root = document.getElementById('rd-root');
    if (!root) return;
    root.classList.remove('rd-mode-pdf', 'rd-mode-orig', 'rd-mode-dual', 'rd-mode-trans',
                          'rd-mode-page', 'rd-mode-page-dual');
    root.classList.add('rd-mode-' + mode);
    Array.prototype.forEach.call(root.querySelectorAll('[data-mode]'), function (b) {
      b.classList.toggle('on', b.dataset.mode === mode);
    });
    var tHost = $('.rd-tpages');
    setTimeout(resizePages, 80);        // 等布局生效后按新宽度重画原页
    if (!RD.transOn) {
      if (tHost) tHost.classList.add('hidden');
      return;                           // 原页模式：只渲染 PDF，不发翻译请求
    }
    if (mode === 'page' || mode === 'page-dual') {
      if (tHost) tHost.classList.remove('hidden');
      ensureTransPages();
      requestAnimationFrame(function () {
        var right = $('.rd-right');
        var tp = RD.tpages[RD.cur];
        if (right && tp) right.scrollTop = Math.max(0, tp.el.offsetTop - 8);
      });
      if (RD.doc) rdEnsurePages([RD.cur]);
      try { localStorage.setItem('rd_mode', mode); } catch (e) { /* 忽略 */ }
      return;
    }
    if (tHost) tHost.classList.add('hidden');
    ensureFlow();                       // 需要阅读流时才构建（按页分块，不卡主线程）
    try { localStorage.setItem('rd_mode', mode); } catch (e) { /* 忽略 */ }
    // 切到需要译文的视图时，先把当前页排进翻译队列
    if (mode !== 'orig' && RD.doc) rdEnsurePages([RD.cur]);
  }

  /* ------------------------------------------------------------ 停靠 / 全屏 */

  /* 沉浸模式 = 阅读器铺满 + 隐藏自绘标题栏 + 系统全屏（隐藏任务栏）。
     UI 与系统状态分开：OS 全屏被用户用快捷键退出时，只同步 UI，不再反向调用。 */
  function immersiveOn() {
    return document.documentElement.classList.contains('rd-immersive');
  }

  function applyImmersive(on) {
    var root = document.getElementById('rd-root');
    if (!root) return;
    root.classList.toggle('rd-fullscreen', !!on);
    document.documentElement.classList.toggle('rd-immersive', !!on);
    try { localStorage.setItem('rd_fullscreen', on ? '1' : '0'); } catch (e) { /* 忽略 */ }
    syncFullBtn(root);
    setTimeout(resizePages, 80);      // 宽度变了，原页要按新宽度重画
  }

  /* 默认停靠在 #main 里（左侧应用侧栏、笔记照常可用）；点 ⤢ 才进入沉浸模式 */
  function toggleFullscreen(force) {
    var on = typeof force === 'boolean' ? force : !immersiveOn();
    applyImmersive(on);
    try {
      if (window.gmWin && typeof window.gmWin.setFullScreen === 'function') {
        window.gmWin.setFullScreen(on);          // Electron 壳：真正系统全屏
      } else if (on) {
        if (document.documentElement.requestFullscreen && !document.fullscreenElement) {
          var p = document.documentElement.requestFullscreen();
          if (p && p.catch) p.catch(function () { /* 用户拒绝 / 不支持 */ });
        }
      } else if (document.exitFullscreen && document.fullscreenElement) {
        var q = document.exitFullscreen();
        if (q && q.catch) q.catch(function () { /* 忽略 */ });
      }
    } catch (e) { /* 全屏不可用：至少阅读器本身铺满 */ }
  }

  /* 系统全屏状态回推：只更新 UI（不再次触发 setFullScreen） */
  function bindFullscreenSync() {
    if (window.gmWin && typeof window.gmWin.onFullScreen === 'function') {
      window.gmWin.onFullScreen(function (v) { applyImmersive(!!v); });
    } else {
      document.addEventListener('fullscreenchange', function () {
        applyImmersive(!!document.fullscreenElement);
      });
    }
  }

  function syncFullBtn(root) {
    var host = root || document.getElementById('rd-root');
    if (!host) return;
    var btn = $('[data-act="full"]', host);
    if (!btn) return;
    var on = host.classList.contains('rd-fullscreen');
    btn.classList.toggle('on', on);
    btn.title = on ? '退出全屏（恢复停靠）' : '全屏';
    btn.textContent = on ? '⤡' : '⤢';
  }

  /* ------------------------------------------------------------ 导出译文 */

  /* 导出走服务端（app/reader_export.py），不再在前端把每页渲成 2x 图再盖白块：
     PDF 页面高度固定，译文一长只能缩到 3.5pt，双栏页还会压到栏间空白。
     现在输出的是重新排版的矢量 PDF：统一字号、图表/公式按原坐标裁回、
     字体嵌入（可复制/可搜索）、标题书签与页码。 */
  var EXPORT_LABEL = { mono: '仅译文 PDF', dual: '双语 PDF', md: 'Markdown',
                       'layout-mono': '版式译文 PDF', 'layout-dual': '版式双语 PDF' };

  function exportStat() {
    var tr = RD.translations || {};
    var total = 0, done = 0;
    (RD.doc && RD.doc.blocks || []).forEach(function (b) {
      if (b.kind === 'figure' || b.kind === 'formula' || b.kind === 'ref'
          || b.kind === 'table' || b.kind === 'meta') return;
      if (!String(b.text || '').trim()) return;
      total++;
      if (String(tr[b.id] || '').trim()) done++;
    });
    return { total: total, done: done, missing: total - done };
  }

  async function exportDoc(mode) {
    if (!RD.doc) return;
    var st = exportStat();
    if (mode !== 'md') {
      if (!st.done) { toast('还没有译文：先点「译全文」或「译本页」', 'error'); return; }
      if (st.missing && !window.confirm('还有 ' + st.missing +
          ' 段没有译文，导出时会保留原文。继续导出？')) return;
    }
    var btns = Array.prototype.slice.call(document.querySelectorAll('[data-exp]'));
    btns.forEach(function (b) { b.disabled = true; });
    var statEl = $('.rd-stat');
    var oldStat = statEl ? statEl.textContent : '';
    if (statEl) statEl.textContent = '正在生成' + (EXPORT_LABEL[mode] || '文件') + '…';
    try {
      var r = await api('/api/reader/export', {
        session_id: sessionId(), path: RD.rel, lang: 'zh', mode: mode,
      });
      if (!r || !r.ok) throw new Error((r && r.error) || '导出失败');
      if (statEl) statEl.textContent = '正在写入文件…';
      var saved = await saveExport(r);
      if (saved === 'canceled') return;
      toast('已导出：' + saved + (r.missing ? '（' + r.missing + ' 段缺译，已保留原文）' : ''), 'ok');
    } catch (e) {
      toast('导出失败：' + (e.message || e), 'error');
    } finally {
      btns.forEach(function (b) { b.disabled = false; });
      if (statEl) { statEl.textContent = oldStat; if (RD.totalBlocks) updateStat(); }
    }
  }

  /* Electron：主进程弹保存框并流式写盘；浏览器：fetch 成 blob 再触发下载。 */
  async function saveExport(info) {
    var url = location.origin + info.url;
    if (window.gmWin && typeof window.gmWin.saveUrl === 'function') {
      var r = await window.gmWin.saveUrl({
        url: url, name: info.name, method: 'GET',
        headers: { 'X-Auth-Token': TOKEN }, title: '保存导出文件',
      });
      if (r && r.ok) return r.path;
      if (r && r.canceled) return 'canceled';
      throw new Error((r && r.error) || '保存失败');
    }
    var res = await fetch(url, { headers: { 'X-Auth-Token': TOKEN } });
    if (!res.ok) throw new Error('下载失败（HTTP ' + res.status + '）');
    var blob = await res.blob();
    var a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = info.name;
    document.body.appendChild(a);
    a.click();
    setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 4000);
    return info.name;
  }

  /* ------------------------------------------------------------ 翻译 */

  function blockText(id) {
    var el = RD.blockEls[id];
    return el ? el.querySelector('.rd-src').textContent : '';
  }

  /* 译文里的公式：
     - ⟦F1⟧ = 服务端保护下来的原文符号（同原来一样原样还原）；
     - $...$ / $$...$$ = 模型自己写的 LaTeX。
     看起来像公式才交给 KaTeX（纯数字、含中文、长英文句子的不当公式）；
     KaTeX 还没加载时退回 math-pending，由 init.js 的 upgradeMath() 补渲染。 */
  function mathInlineHtml(tex, display) {
    var raw = String(tex || '');
    var t = raw.trim();
    if (!t) return '';
    if (/^[\d.,]+$/.test(t)) return null;                 // $5 / $12.30 不是公式
    if (/^\s/.test(raw) || /\s$/.test(raw)) return null;  // $5 and $ 这种货币写法
    if (/[\u3400-\u9fff]/.test(t)) return null;           // 含中文：不是公式
    if ((t.match(/[A-Za-z]{2,}/g) || []).length > 3) return null;  // 长英文句子
    if (window.katex) {
      // 模型偶尔写出坏 LaTeX：先试渲染（throwOnError），失败返回 false，
      // 调用方改成显示原始 TeX，避免阅读流里出现一块红色报错。
      try {
        return window.katex.renderToString(t, {
          displayMode: !!display, throwOnError: true, strict: false,
        });
      } catch (e) {
        return false;
      }
    }
    if (typeof mathHtml === 'function') return mathHtml(t, !!display);
    return '<span class="rd-math">' + escapeHtml(t) + '</span>';
  }

  function formatInline(text, math) {
    var raw = String(text || '');
    var out = '', last = 0, m;
    var re = /⟦(F\d+)⟧|\$\$([\s\S]+?)\$\$|\$([^$\n]+?)\$/g;
    while ((m = re.exec(raw))) {
      out += escapeHtml(raw.slice(last, m.index));
      if (m[1]) {
        // 占位符：内容必须还原，KaTeX 排不了就退回原文符号
        var v = (math && math[m[1]]) || '';
        var h = mathInlineHtml(v, false);
        out += (h == null || h === false ? escapeHtml(v) : h);
      } else {
        var tex = m[2] != null ? m[2] : m[3];
        var html = mathInlineHtml(tex, m[2] != null);
        out += (html === false ? escapeHtml(tex)
          : (html == null ? escapeHtml(m[0]) : html));
      }
      last = re.lastIndex;
    }
    out += escapeHtml(raw.slice(last));
    return out;
  }

  function blockOf(id) {
    if (RD.blockById && RD.blockById[id]) return RD.blockById[id];
    // 阅读流还没构建时也要能拿到块（联动高亮、查找、导出都依赖 rect）
    var list = (RD.doc && RD.doc.blocks) || [];
    for (var i = 0; i < list.length; i++) {
      if (list[i].id === id) {
        RD.blockById = RD.blockById || {};
        RD.blockById[id] = list[i];
        return list[i];
      }
    }
    return null;
  }

  function applyTranslations(map) {
    Object.keys(map || {}).forEach(function (id) { applyTrans(id, map[id]); });
    updateProgress();
    scheduleSyncStats();
  }

  function applyTrans(id, text) {
    if (text) RD.translations[id] = text;
    else delete RD.translations[id];
    var b = blockOf(id);
    if (b && b.page) scheduleTransOverlay(b.page);
    scheduleSyncStats();
    var el = RD.blockEls[id];
    if (!el) return;
    var dst = el.querySelector('.rd-dst');
    if (!dst) return;
    if (!text) {
      el.dataset.state = 'fail';
      dst.textContent = '（这段没译出来，点「翻译本页」重试）';
      return;
    }
    dst.innerHTML = formatInline(text, b ? b.math : null);
    dst.dataset.state = 'ok';
    el.dataset.state = 'ok';
    el.classList.remove('rd-raw');
    // 译文是整段重写的：这一段的高亮 / 批注标记要跟着重新铺一遍
    if ((RD.annos || []).length) applyReaderAnnos(id);
    if (!dst.dataset.counted) {
      dst.dataset.counted = '1';
      RD.doneBlocks++;
      updateProgress();
    }
  }

  function markWarn(id, list) {
    var el = RD.blockEls[id];
    if (!el) return;
    el.classList.add('rd-warn');
    el.title = '可能有数字 / 引用丢失：' + (list || []).join('、');
  }

  function updateStat() {
    var el = document.querySelector('.rd-stat');
    if (!el) return;
    if (!RD.totalBlocks) { el.textContent = ''; return; }
    var pct = Math.round(RD.doneBlocks / RD.totalBlocks * 100);
    var text = '已译 ' + RD.doneBlocks + '/' + RD.totalBlocks + ' 段 · ' + pct + '%';
    if (RD.skipped) text += ' · 跳过 ' + RD.skipped + ' 段（引用 / 表格 / 公式 / 页眉）';
    el.textContent = text;
  }

  function rdEnsurePages(pages, force) {
    if (!RD.transOn) return;                 // 原页模式：滚动/翻页都不触发翻译
    if (RD.allRequested && !force) return;   // 已要求翻译全文，滚动不必再按页排队
    (pages || []).forEach(function (p) { RD.pending.add(Number(p)); });
    if (RD.streaming || !RD.doc) return;
    var want = Array.from(RD.pending);
    RD.pending.clear();
    want = want.filter(function (p) { return force || !RD.requested.has(p); });
    if (!want.length) return;
    want.forEach(function (p) { RD.requested.add(p); });
    translate(want);
  }

  /* ---------------- 术语表（按文档缓存，可人工编辑） ---------------- */
  function glossEls() {
    var root = document.getElementById('rd-root');
    return root ? {
      box: $('.rd-gloss', root),
      list: $('.rd-gloss-list', root),
      n: $('.rd-gloss-n', root),
    } : {};
  }

  function renderGlossary(terms) {
    var els = glossEls();
    if (!els.list) return;
    els.list.innerHTML = '';
    if (els.n) els.n.textContent = terms && terms.length ? terms.length + ' 条' : '';
    if (!terms || !terms.length) {
      els.list.innerHTML = '<div class="rd-gloss-empty">还没有术语表，点「重新生成」从全文抽取</div>';
      return;
    }
    terms.forEach(function (t) {
      var row = document.createElement('div');
      row.className = 'rd-gloss-row';
      var src = document.createElement('span');
      src.className = 'rd-gloss-src';
      src.textContent = t.src || '';
      var dst = document.createElement('input');
      dst.className = 'rd-gloss-dst';
      dst.value = t.dst || '';
      var x = document.createElement('button');
      x.className = 'rd-gloss-x';
      x.title = '删除这一条';
      x.textContent = '✕';
      x.onclick = function () { row.remove(); };
      row.appendChild(src);
      row.appendChild(dst);
      row.appendChild(x);
      els.list.appendChild(row);
    });
  }

  async function loadGlossary() {
    if (!RD.rel) return;
    try {
      var r = await api('/api/reader/glossary', {
        session_id: sessionId(), path: RD.rel, lang: 'zh',
      });
      renderGlossary((r && r.terms) || []);
    } catch (e) { renderGlossary([]); }
  }

  async function saveGlossary() {
    var els = glossEls();
    if (!els.list) return;
    var terms = [];
    Array.prototype.forEach.call(els.list.querySelectorAll('.rd-gloss-row'), function (row) {
      var s = $('.rd-gloss-src', row);
      var d = $('.rd-gloss-dst', row);
      if (s && d && s.textContent.trim() && d.value.trim()) {
        terms.push({ src: s.textContent.trim(), dst: d.value.trim() });
      }
    });
    var r = await api('/api/reader/glossary', {
      session_id: sessionId(), path: RD.rel, lang: 'zh', action: 'save', terms: terms,
    });
    if (r && r.ok) toast('术语表已保存（之后翻译/重译会生效）', 'ok');
    else toast('术语表保存失败：' + ((r && r.error) || '未知错误'), 'error');
  }

  async function regenGlossary(btn) {
    if (!RD.rel) return;
    var label = btn ? btn.textContent : '';
    if (btn) { btn.disabled = true; btn.textContent = '抽取中…'; }
    try {
      var r = await api('/api/reader/glossary', {
        session_id: sessionId(), path: RD.rel, lang: 'zh', action: 'regenerate',
      });
      if (!r || !r.ok) throw new Error((r && r.error) || '抽取失败');
      renderGlossary(r.terms || []);
      toast('术语表已重新生成（' + (r.terms || []).length + ' 条）', 'ok');
    } catch (e) {
      toast('术语表生成失败：' + (e.message || e), 'error');
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = label || '重新生成'; }
    }
  }

  async function translate(pages, force, retry) {
    try {
      var payload = { session_id: sessionId(), path: RD.rel, lang: 'zh' };
      if (pages) payload.pages = pages;
      if (force) payload.force = true;
      if (retry) payload.retry = true;
      var r = await api('/api/reader/translate', payload);
      if (!r || !r.ok) throw new Error((r && r.error) || '启动翻译失败');
      if (!r.total) {                // 已经全译过：静默返回，不打扰阅读
        if (retry) toast('没有需要补译的段落', 'ok');
        return;
      }
      RD.job = r.job_id;
      RD.streaming = true;
      refreshTransBtns();
      streamEvents(r.job_id);
    } catch (e) {
      toast('翻译失败：' + (e.message || e), 'error');
    }
  }

  async function streamEvents(jobId) {
    var ctrl = new AbortController();
    RD.stream = ctrl;
    try {
      var res = await fetch('/api/reader/events', {
        method: 'POST', headers: authHeaders(),
        body: JSON.stringify({ job_id: jobId, since: 0 }),
        signal: ctrl.signal,
      });
      if (!res.ok || !res.body) throw new Error('事件流断开');
      var reader = res.body.getReader();
      var dec = new TextDecoder();
      var buf = '';
      while (true) {
        var chunk = await reader.read();
        if (chunk.done) break;
        buf += dec.decode(chunk.value, { stream: true });
        var idx;
        while ((idx = buf.indexOf('\n\n')) >= 0) {
          var frame = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          frame.split('\n').forEach(function (line) {
            if (line.indexOf('data: ') !== 0) return;
            var obj;
            try { obj = JSON.parse(line.slice(6)); } catch (e) { return; }
            if (obj.type === 'block') {
              applyTrans(obj.id, obj.text);
              if (obj.warn) markWarn(obj.id, obj.warn);
            } else if (obj.type === 'error') toast('部分段落翻译失败：' + (obj.message || ''), 'error');
            else if (obj.type === 'glossary') { if (obj.count) RD.terms = obj.count; }
            else if (obj.type === 'queued') {
              RD.skipped = Number(obj.skipped || 0);
              updateStat();
            } else if (obj.type === 'done') {
              RD.job = null;
              RD.skipped = Number(obj.skipped || RD.skipped || 0);
              if (obj.failed) {
                toast('有 ' + obj.failed + ' 段没译出来，可在「设置 → 翻译 → 重试失败段」重试', 'error');
              }
              updateProgress();
            }
          });
        }
      }
    } catch (e) {
      if (e.name !== 'AbortError') toast('翻译事件流中断：' + (e.message || e), 'error');
    } finally {
      RD.stream = null;
      RD.streaming = false;
      refreshTransBtns();
      updateProgress();
      if (RD.pending.size) rdEnsurePages([]);
    }
  }

  /* ------------------------------------------------------------ 阅读位置 */

  function posKey() { return 'rd_pos_' + (RD.doc ? RD.doc.key : RD.rel); }

  function savePosSoon() {
    clearTimeout(RD._posTimer);
    RD._posTimer = setTimeout(savePos, 400);
  }

  function savePos() {
    if (!RD.doc) return;
    try {
      var flow = $('.rd-right');
      localStorage.setItem(posKey(), JSON.stringify({ page: RD.cur, top: flow.scrollTop }));
    } catch (e) { /* 忽略 */ }
  }

  function restorePos() {
    try {
      var raw = localStorage.getItem(posKey());
      if (!raw) return;
      var p = JSON.parse(raw);
      if (!p) return;
      setTimeout(function () {
        var flow = $('.rd-right');
        if (!flow) return;
        flow.scrollTop = Number(p.top) || 0;
        // 按恢复后的滚动位置重新算当前页，否则页码徽标会停在 1
        var top = flow.scrollTop + 80;
        var page = 1;
        var marks = flow.querySelectorAll('.rd-mark');
        for (var i = 0; i < marks.length; i++) {
          if (marks[i].offsetTop <= top) page = Number(marks[i].dataset.page);
          else break;
        }
        RD.cur = page;
        markActivePage();
        updatePageInd();
        var st = RD.pages[page];
        if (st) $('.rd-left').scrollTo({ top: Math.max(0, st.el.offsetTop - 12) });
      }, 120);
    } catch (e) { /* 忽略 */ }
  }

  window.Reader = { open: open, close: close };
})();
