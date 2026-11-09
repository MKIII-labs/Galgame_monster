/* ============================================================
   划词浮层内核（SelKit）
   ------------------------------------------------------------
   对话 / 笔记 / PDF 三处原来各写一套：触发、定位、关闭、选区清洗、
   工具条 DOM 全都不一样。这里只留一套，各页面注册一个适配器：

     SelKit.register({
       id: 'reader',
       roots: () => [document.querySelector('.rd-flow')],
       maxText: 1600,
       label: (ctx) => 'PDF · 第 ' + ctx.page + ' 页',
       clean: (text) => ...,           // 可选：PDF 公式去重
       capture: (range) => ({ text, rect, page, blockId }),
       actions: [
         { id: 'ask', label: '提问', icon: 'chat', primary: true, run: fn },
         { id: 'copy', label: '复制', icon: 'copy', run: fn },
       ],
     });

   内核负责：选区校验、清洗与截断、浮层渲染、定位、Esc / 滚动 / 外部点击关闭、
   快捷键（Ctrl+Enter 提问、Ctrl+Shift+Enter 翻译）、保住选区。
   适配器只负责：这段选区属于谁、带什么上下文、点了之后做什么。
   ============================================================ */
'use strict';

(function () {
  const ADAPTERS = [];
  const EDGE = 8;
  const HARD_MAX = 4000;
  const S = {
    adapter: null,
    ctx: null,
    bar: null,
    more: null,
    timer: 0,
    shownAt: 0,
  };

  function ic(name) {
    return (typeof window.icon === 'function' && name) ? window.icon(name) : '';
  }

  function elementOf(node) {
    if (!node) return null;
    return node.nodeType === 1 ? node : node.parentElement;
  }

  function isEditable(node) {
    const el = elementOf(node);
    if (!el || !el.closest) return false;
    return !!el.closest('input, textarea, select, [contenteditable=""], [contenteditable="true"]');
  }

  function insideOwn(node) {
    const el = elementOf(node);
    if (!el || !el.closest) return false;
    // 浮层本身 + 划词问答面板 / 笔记批注弹层：点它们时不要再弹一次
    return !!el.closest('.selbar, .selbar-more-menu, .nt-anno-pop, .askkit');
  }

  function shown(el) {
    return !!(el && el.offsetParent !== null);
  }

  function findAdapter(range) {
    for (let i = 0; i < ADAPTERS.length; i++) {
      const a = ADAPTERS[i];
      let roots = [];
      try { roots = (a.roots && a.roots()) || []; } catch (e) { roots = []; }
      for (let j = 0; j < roots.length; j++) {
        const root = roots[j];
        if (root && shown(root) && root.contains(range.commonAncestorContainer)) return a;
      }
    }
    return null;
  }

  function clean(text, adapter) {
    let t = String(text || '').replace(/\r\n?/g, '\n');
    if (adapter && adapter.clean) t = adapter.clean(t);
    return String(t == null ? '' : t).replace(/[ \t]+\n/g, '\n').trim();
  }

  /* 复制专用：保留换行与连续空格（`.replace(/\s+/g,' ')` 只适合给模型看的提问文本）。
     不能直接把选区 clone 到 innerText：容器一旦设 pre-wrap，HTML 缩进产生的空白文本
     也会被当成内容（复制出来一堆空行）。这里按节点类型自己拼：
       - 文本：white-space 含 pre 的原样保留，其余把连续空白压成一个空格；
       - <br> / 块级元素：补一个换行（不重复）；
       - script/style / display:none：跳过。 */
  function isBlockEl(el) {
    try {
      const d = getComputedStyle(el).display;
      return d === 'block' || d === 'list-item' || d === 'table'
        || d === 'table-row' || d === 'table-cell' || d === 'flex'
        || d === 'grid' || d === 'flow-root';
    } catch (e) {
      return false;
    }
  }

  function keepsWhitespace(el) {
    try {
      const w = getComputedStyle(el).whiteSpace || '';
      return w.indexOf('pre') === 0 || w === 'break-spaces';
    } catch (e) {
      return false;
    }
  }

  function plainText(range) {
    if (!range) return '';
    let out = '';
    const pushBlock = () => {
      out = out.replace(/[ \t]+$/, '');
      if (out && !/\n$/.test(out)) out += '\n';
    };
    const pushBreak = () => {
      out = out.replace(/[ \t]+$/, '');
      out += '\n';                        // <br> 是显式换行，连着两个就是空行
    };
    const pushText = (raw, preserve) => {
      if (!raw) return;
      if (preserve) { out += raw; return; }
      let t = String(raw).replace(/\s+/g, ' ');
      if (!out && /^\s+$/.test(t)) return;            // 容器自身的缩进
      if (out && /[ \n]$/.test(out) && t.charAt(0) === ' ') t = t.slice(1);
      if (t) out += t;
    };
    let root = range.commonAncestorContainer;
    if (root && root.nodeType === 3) root = root.parentElement;
    if (!root) return String(range.toString() || '');
    try {
      const walker = document.createTreeWalker(
        root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT, {
          acceptNode(n) {
            if (n.nodeType === 3) {
              try {
                return range.intersectsNode(n) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
              } catch (e) { return NodeFilter.FILTER_REJECT; }
            }
            if (n.tagName === 'SCRIPT' || n.tagName === 'STYLE' || n.tagName === 'NOSCRIPT') {
              return NodeFilter.FILTER_REJECT;
            }
            try {
              const cs = getComputedStyle(n);
              if (cs && (cs.display === 'none' || cs.visibility === 'hidden')) {
                return NodeFilter.FILTER_REJECT;
              }
            } catch (e) { /* 拿不到样式就按可见处理 */ }
            try {
              return range.intersectsNode(n) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
            } catch (e) { return NodeFilter.FILTER_REJECT; }
          },
        });
      let n;
      while ((n = walker.nextNode())) {
        if (n.nodeType === 3) {
          let raw = n.nodeValue || '';
          if (range.startContainer === n || range.endContainer === n) {
            const s = range.startContainer === n ? range.startOffset : 0;
            const e = range.endContainer === n ? range.endOffset : raw.length;
            raw = raw.slice(s, e);
          }
          pushText(raw, keepsWhitespace(n.parentElement));
          continue;
        }
        if (n.tagName === 'BR') { pushBreak(); continue; }
        if (isBlockEl(n)) pushBlock();
      }
    } catch (e) {
      out = String(range.toString() || '');
    }
    out = out.replace(/\r\n?/g, '\n').replace(/[\u200b-\u200d\ufeff]/g, '');
    out = out.replace(/[ \t]+\n/g, '\n').replace(/\n{3,}/g, '\n\n');
    // 去掉选区首尾的空行/行尾空白，但不要 trim()：代码块的缩进也是内容
    return out.replace(/^(?:[ \t]*\n)+/, '').replace(/[ \t]+$/, '').replace(/\n+$/, '');
  }

  function schedule() {
    clearTimeout(S.timer);
    S.timer = setTimeout(pick, 0);
  }

  function pick() {
    const sel = window.getSelection();
    if (!sel || !sel.rangeCount || sel.isCollapsed) return close();
    if (insideOwn(sel.anchorNode) || isEditable(sel.anchorNode)) return close();
    const range = sel.getRangeAt(0);
    if (!String(range.toString() || '').trim()) return close();
    const adapter = findAdapter(range);
    if (!adapter) return close();
    let ctx = null;
    try { ctx = adapter.capture(range, sel); } catch (e) {
      // 适配器内部出错不能静默：否则浮层只是「不弹」，很难查
      console.error('[SelKit] capture failed:', adapter.id, e);
      ctx = null;
    }
    if (!ctx) return close();
    let text = ctx.text != null ? String(ctx.text) : clean(range.toString(), adapter);
    text = String(text || '').trim();
    if (!text) return close();
    const cap = Number(adapter.maxText || HARD_MAX) || HARD_MAX;
    if (text.length > cap) {
      text = text.slice(0, cap);
      ctx.truncated = true;
    }
    ctx.text = text;
    ctx.rect = ctx.rect || range.getBoundingClientRect();
    if (!ctx.rect || (!ctx.rect.width && !ctx.rect.height)) return close();
    ctx.adapter = adapter;
    S.adapter = adapter;
    S.ctx = ctx;
    render(ctx);
  }

  /* ---------------- 浮层 ---------------- */
  function ensureBar() {
    if (S.bar) return S.bar;
    const bar = document.createElement('div');
    bar.className = 'selbar hidden';
    bar.setAttribute('role', 'toolbar');
    // 保住选区：mousedown 的默认行为会清空 window.getSelection()
    bar.addEventListener('mousedown', (e) => e.preventDefault());
    document.body.appendChild(bar);
    S.bar = bar;
    return bar;
  }

  function sep() {
    const s = document.createElement('span');
    s.className = 'selbar-sep';
    return s;
  }

  function button(action, ctx, onClick) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'selbar-btn';
    if (action.swatch) {
      const dot = document.createElement('span');
      dot.className = 'selbar-dot';
      dot.style.background = action.swatch;
      b.appendChild(dot);
    } else {
      b.innerHTML = ic(action.icon);
    }
    const label = document.createElement('span');
    label.textContent = action.label || action.id;
    b.appendChild(label);
    b.addEventListener('mousedown', (e) => e.preventDefault());
    b.addEventListener('click', (e) => { e.stopPropagation(); onClick(); });
    return b;
  }

  function render(ctx) {
    const bar = ensureBar();
    bar.innerHTML = '';
    const adapter = ctx.adapter;
    let label = ctx.label;
    if (!label && adapter.label) {
      try { label = adapter.label(ctx); } catch (e) { label = ''; }
    }
    if (label) {
      const src = document.createElement('span');
      src.className = 'selbar-src';
      src.textContent = label;
      src.title = label;
      bar.appendChild(src);
      bar.appendChild(sep());
    }
    if (ctx.truncated) {
      const tip = document.createElement('span');
      tip.className = 'selbar-trunc';
      tip.textContent = '已截断';
      bar.appendChild(tip);
    }
    const actions = (adapter.actions || []).filter((a) => !a.when || a.when(ctx));
    const primary = actions.filter((a) => a.primary);
    const more = actions.filter((a) => !a.primary);
    primary.forEach((a) => bar.appendChild(button(a, ctx, () => run(a, ctx))));
    if (more.length) {
      if (primary.length) bar.appendChild(sep());
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'selbar-btn selbar-more';
      b.innerHTML = '<span>更多</span><span class="selbar-caret">▾</span>';
      b.addEventListener('mousedown', (e) => e.preventDefault());
      b.addEventListener('click', (e) => {
        e.stopPropagation();
        if (S.more && !S.more.classList.contains('hidden')) hideMore();
        else showMore(more, ctx, bar);
      });
      bar.appendChild(b);
    }
    bar.classList.remove('hidden');
    S.shownAt = Date.now();
    place(bar, ctx.rect);
  }

  /* 刚弹出时的滚动多半是适配器自己的「联动」滚动（选中左边 → 右边自动定位），
     不能把它当成用户翻页而关掉浮层；稍后用户自己滚动仍然关闭。 */
  function onScroll() {
    if (Date.now() - S.shownAt < 800) return;
    close();
  }

  function place(el, rect) {
    el.style.visibility = 'hidden';
    el.classList.remove('hidden');
    const w = el.offsetWidth || 200;
    const h = el.offsetHeight || 32;
    let left = rect.left + rect.width / 2 - w / 2;
    left = Math.max(EDGE, Math.min(window.innerWidth - w - EDGE, left));
    let top = rect.top - h - 8;
    if (top < EDGE) top = Math.min(window.innerHeight - h - EDGE, rect.bottom + 8);
    top = Math.max(EDGE, top);
    el.style.left = left + 'px';
    el.style.top = top + 'px';
    el.style.visibility = '';
  }

  function ensureMore() {
    if (S.more) return S.more;
    const menu = document.createElement('div');
    menu.className = 'selbar-more-menu hidden';
    menu.addEventListener('mousedown', (e) => e.preventDefault());
    document.body.appendChild(menu);
    S.more = menu;
    return menu;
  }

  function showMore(actions, ctx, bar) {
    const menu = ensureMore();
    menu.innerHTML = '';
    actions.forEach((a) => {
      const b = button(a, ctx, () => { hideMore(); run(a, ctx); });
      b.classList.add('selbar-menu-item');
      menu.appendChild(b);
    });
    menu.classList.remove('hidden');
    menu.style.visibility = 'hidden';
    const r = bar.getBoundingClientRect();
    const w = menu.offsetWidth || 140;
    const h = menu.offsetHeight || 120;
    let left = Math.max(EDGE, Math.min(window.innerWidth - w - EDGE, r.left));
    let top = r.bottom + 4;
    if (top + h > window.innerHeight - EDGE) top = Math.max(EDGE, r.top - h - 4);
    menu.style.left = left + 'px';
    menu.style.top = top + 'px';
    menu.style.visibility = '';
  }

  function hideMore() {
    if (S.more) S.more.classList.add('hidden');
  }

  function run(action, ctx) {
    close();
    try { action.run(ctx); } catch (e) { console.error('[SelKit]', action.id, e); }
  }

  function close() {
    clearTimeout(S.timer);
    if (S.bar) S.bar.classList.add('hidden');
    hideMore();
    S.adapter = null;
    S.ctx = null;
  }

  /* ---------------- 事件 ---------------- */
  function onPointerUp(e) {
    if (e.button !== 0 || insideOwn(e.target)) return;
    schedule();
  }

  function onMouseDown(e) {
    if (insideOwn(e.target)) return;
    if (S.more && !S.more.classList.contains('hidden')) {
      hideMore();
      return;
    }
    if (S.bar && !S.bar.classList.contains('hidden')) close();
  }

  function onKeyDown(e) {
    if (e.key === 'Escape') {
      if (S.bar && !S.bar.classList.contains('hidden')) {
        e.preventDefault();
        close();
      }
      return;
    }
    if (!S.ctx || !(e.ctrlKey || e.metaKey)) return;
    const id = e.shiftKey ? 'translate' : 'ask';
    const action = (S.adapter.actions || []).find(
      (a) => a.id === id && (!a.when || a.when(S.ctx)));
    if (!action) return;
    e.preventDefault();
    run(action, S.ctx);
  }

  function init() {
    if (init.done) return;
    init.done = true;
    document.addEventListener('pointerup', onPointerUp, true);
    document.addEventListener('mousedown', onMouseDown, true);
    document.addEventListener('keydown', onKeyDown, true);
    document.addEventListener('keyup', (e) => {
      if (e.key === 'Shift') schedule();
    }, true);
    document.addEventListener('selectionchange', () => {
      const sel = window.getSelection();
      if (!sel || sel.isCollapsed) close();
    });
    window.addEventListener('scroll', onScroll, true);
    window.addEventListener('resize', close);
  }

  window.SelKit = {
    register(adapter) {
      if (!adapter || !adapter.id || !adapter.capture) return;
      ADAPTERS.push(adapter);
      init();
    },
    close,
    plainText,
    get current() { return S.ctx; },
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init, { once: true });
  } else {
    init();
  }
})();
