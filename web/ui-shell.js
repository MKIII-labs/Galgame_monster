/* ============================================================
   界面重构壳层（P1）：顶栏同步 / 右侧检查器
   ------------------------------------------------------------
   只做展示与入口整合：
   - 上下文 / 工具 / 用量 / 轨迹四个查看器收进右侧检查器；
   - 原有 openCtxDetail / openUsage / openTrajectory 仍可复用；
   - 不改任何业务数据与接口。
   ============================================================ */
'use strict';

(function () {
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  const MODE_META = {
    solo: ['对话', 'Solo'],
    ide: ['工作区', 'IDE'],
    hall: ['功能大厅', '大厅'],
    work: ['工作台', '看板'],
    plugins: ['插件', '市场'],
  };

  function currentMode() {
    const main = $('#main');
    if (!main) return 'solo';
    for (const key of ['ide', 'hall', 'work', 'plugins']) {
      if (main.classList.contains(key + '-mode')) return key;
    }
    return 'solo';
  }

  function hallLabel() {
    const t = $('#hall-title');
    const txt = t ? (t.textContent || '').trim() : '';
    return (!txt || txt === '功能大厅') ? '互动玩法' : txt;
  }

  function workLabel() {
    const s = $('#work-sub');
    const txt = s ? (s.textContent || '').trim() : '';
    return txt || '看板';
  }

  function syncTopbar() {
    const mode = currentMode();
    const meta = MODE_META[mode] || MODE_META.solo;
    const title = $('#app-page-title');
    const sub = $('#app-page-sub');
    let subText = meta[1];
    if (mode === 'hall') subText = hallLabel();
    if (mode === 'work') subText = workLabel();
    if (title && title.textContent !== meta[0]) title.textContent = meta[0];
    if (sub && sub.textContent !== subText) sub.textContent = subText;
    const pb = $('#btn-plugins');
    if (pb) pb.classList.toggle('active', mode === 'plugins');
  }

  const main = $('#main');
  if (main) {
    new MutationObserver(syncTopbar).observe(main, { attributes: true, attributeFilter: ['class'] });
  }
  // 大厅模块标题 / 工作台副标题由各自脚本改写，顶栏跟着同步，避免两处标题各说各话
  ['#hall-title', '#work-sub'].forEach((sel) => {
    const node = $(sel);
    if (!node) return;
    new MutationObserver(syncTopbar).observe(node, {
      subtree: true, childList: true, characterData: true,
    });
  });
  syncTopbar();

  /* ---------------- 检查器 ---------------- */

  const insp = $('#inspector');
  const inspBtn = $('#btn-inspector');
  let activeTab = 'context';
  let usageLoaded = false;

  const fmt = (n) => (typeof fmtNum === 'function' ? fmtNum(n) : String(Number(n) || 0));
  const toastSafe = (msg, kind) => {
    if (typeof toast === 'function') toast(msg, kind || 'error');
  };

  function selectTab(name) {
    activeTab = name;
    $$('.insp-tab').forEach((b) => {
      const on = b.dataset.itab === name;
      b.classList.toggle('active', on);
      b.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    $$('.insp-pane').forEach((p) => p.classList.toggle('hidden', p.dataset.ipane !== name));
    if (name === 'usage') loadUsage();
    if (name === 'tools') syncTools();
    if (name === 'aside') loadAsides();
    if (name === 'plan') {
      // 切到计划页：清掉「计划有更新」的红点，并按当前会话最新计划重画
      $$('.insp-tab').forEach((b) => b.classList.remove('has-dot'));
      const ib = $('#btn-inspector');
      if (ib) ib.classList.remove('has-dot');
      if (typeof window.uiInspectorSyncPlan === 'function') window.uiInspectorSyncPlan();
    }
  }

  function setContext(ctx) {
    const bar = $('#insp-ctx-bar');
    const text = $('#insp-ctx-text');
    if (!bar || !text) return;
    if (!ctx) {
      bar.style.width = '0%';
      bar.classList.remove('warn', 'danger');
      text.textContent = '还没有上下文数据。';
      return;
    }
    if (!ctx.limit) {
      bar.style.width = '0%';
      bar.classList.remove('warn', 'danger');
      text.textContent = '上下文不限（窗口上限为 0）。';
      return;
    }
    const pct = Math.max(0, Math.min(100, Number(ctx.pct) || 0));
    bar.style.width = pct + '%';
    bar.classList.toggle('warn', pct >= 70 && pct < 90);
    bar.classList.toggle('danger', pct >= 90);
    text.textContent = `已用 ${fmt(ctx.used)} / ${fmt(ctx.limit)}（${Math.round(pct)}%）\n`
      + `剩余 ${fmt(ctx.remain)} tokens`;
  }
  window.uiInspectorSetContext = setContext;

  function syncTools() {
    const el = $('#insp-tools');
    const list = $('#insp-tools-list');
    if (!el) return;
    if (list) list.innerHTML = '';
    const rows = $$('#chat .msg.ai');
    const last = rows[rows.length - 1];
    if (!last) {
      el.textContent = '还没有 AI 回复。';
      return;
    }
    const cards = Array.from(last.querySelectorAll('.tool-card'));
    el.textContent = cards.length
      ? `最近一条回复包含 ${cards.length} 个工具调用（点击条目在消息流中定位）。`
      : '最近一条回复没有工具调用。';
    if (!list) return;
    // 只列最近 12 个，避免一次回复几十个工具时把检查器撑爆
    cards.slice(-12).forEach((card) => {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'insp-tool';
      if (card.classList.contains('failed')) chip.classList.add('bad');
      else if (card.classList.contains('ok')) chip.classList.add('ok');
      const name = card.querySelector('.tool-name');
      const state = card.querySelector('.tool-state');
      const arg = card.querySelector('.tool-arg');
      const label = (name && name.textContent) || '工具';
      chip.textContent = label.trim();
      chip.title = [label, arg && arg.textContent, state && state.textContent]
        .map((s) => String(s || '').trim()).filter(Boolean).join(' · ');
      chip.onclick = () => {
        card.scrollIntoView({ block: 'center', behavior: GM_SMOOTH });
        card.classList.add('insp-flash');
        setTimeout(() => card.classList.remove('insp-flash'), 1200);
      };
      list.appendChild(chip);
    });
  }

  async function loadUsage() {
    if (usageLoaded) return;
    usageLoaded = true;
    const el = $('#insp-usage');
    if (!el) return;
    el.textContent = '读取中…';
    try {
      const res = await fetch('/api/usage?mode=day&span=7', {
        headers: { 'X-Auth-Token': typeof TOKEN === 'string' ? TOKEN : '' },
      });
      const data = await res.json();
      const s = (data && data.summary) || {};
      el.textContent = `近 7 天：总 ${fmt(s.total)} tokens\n`
        + `输入 ${fmt(s.prompt)} · 输出 ${fmt(s.completion)}\n`
        + `调用 ${fmt(s.calls)} 次 · 缓存命中 ${fmt(s.cached)}`;
    } catch (e) {
      usageLoaded = false;
      el.textContent = '读取失败：' + ((e && e.message) || e);
    }
  }

  function scrollToLastReply() {
    const rows = $$('#chat .msg.ai');
    const last = rows[rows.length - 1];
    if (!last) { toastSafe('还没有 AI 回复'); return; }
    last.scrollIntoView({ block: 'center', behavior: GM_SMOOTH });
  }

  /* ---------------- 检查器：旁注（历史划词提问） ---------------- */

  const currentSid = () => String(
    (typeof state !== 'undefined' && state.sessions && state.sessions.current) || '');

  function fmtAsideTime(ts) {
    const n = Number(ts) || 0;
    if (!n) return '';
    const d = new Date(n * 1000);
    const pad = (x) => String(x).padStart(2, '0');
    return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }

  function openAsideThread(t, ev) {
    if (!window.AskKit) { toastSafe('问答面板未加载，请刷新页面'); return; }
    const sid = currentSid();
    const sel = (t && t.selection) || {};
    const rect = (ev && ev.currentTarget) ? ev.currentTarget.getBoundingClientRect() : null;
    window.AskKit.open({
      scope: 'session:' + sid,
      source: 'chat',
      threadId: t.id,
      selection: sel,
      includeMessage: !!t.include_message,
      messageText: t.message_text || '',
      anchor: t.anchor || sel.text || '',
      label: (sel.msg_index != null) ? ('对话 · #' + sel.msg_index) : '旁注',
      quote: t.title || sel.text || '',
      title: '旁注',
      rect,
      onQuote: (text) => {
        if (typeof addQuote === 'function') addQuote(text, '旁注');
        else toastSafe('当前界面不支持引用');
      },
      onCapture: (qa, r) => {
        if (typeof window.asideCapture !== 'function') {
          toastSafe('笔记面板未加载，请刷新页面');
          return;
        }
        window.asideCapture(sel.text || t.title || '', qa.history, sel, r);
      },
    });
  }

  function renderAsides(threads) {
    const box = $('#insp-asides');
    const hint = $('#insp-asides-hint');
    if (!box) return;
    box.innerHTML = '';
    if (!threads.length) {
      if (hint) hint.textContent = '这个会话还没有旁注。在对话里划选文字 → 提问。';
      return;
    }
    if (hint) hint.textContent = `共 ${threads.length} 条旁注（左侧最新，点击回看）`;
    threads.forEach((t) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'insp-aside';
      b.dataset.tid = t.id;
      const title = document.createElement('span');
      title.className = 'insp-aside-t';
      title.textContent = t.title || '旁注';
      const q = document.createElement('span');
      q.className = 'insp-aside-q';
      const hist = t.history || [];
      const lastQ = (hist.length && hist[hist.length - 1].q)
        || (t.selection && t.selection.text) || '';
      q.textContent = String(lastQ).replace(/\s+/g, ' ').slice(0, 90);
      const meta = document.createElement('span');
      meta.className = 'insp-aside-meta';
      meta.textContent = `${hist.length} 轮 · ${fmtAsideTime(t.updated_at || t.created_at)}`;
      b.appendChild(title);
      b.appendChild(q);
      b.appendChild(meta);
      b.onclick = (ev) => openAsideThread(t, ev);
      box.appendChild(b);
    });
  }

  async function loadAsides() {
    const box = $('#insp-asides');
    const hint = $('#insp-asides-hint');
    if (!box) return;
    const sid = currentSid();
    if (!sid) {
      box.innerHTML = '';
      if (hint) hint.textContent = '先打开一个会话，划选文字提问后会显示在这里。';
      return;
    }
    if (hint) hint.textContent = '读取中…';
    try {
      const res = await fetch('/api/ask/threads', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Auth-Token': typeof TOKEN === 'string' ? TOKEN : '',
        },
        body: JSON.stringify({ op: 'list', scope: 'session:' + sid, source: 'chat' }),
      });
      const data = await res.json();
      const threads = ((data && data.threads) || [])
        .filter((t) => (t.source || 'chat') === 'chat')
        .sort((a, b) => (Number(b.created_at) || 0) - (Number(a.created_at) || 0));
      renderAsides(threads);
    } catch (e) {
      box.innerHTML = '';
      if (hint) hint.textContent = '读取失败：' + ((e && e.message) || e);
    }
  }

  // 切会话时由 aside.js 调用：旁注线程跟着会话走，列表要重新拉
  window.uiInspectorRefreshAsides = () => {
    if (activeTab === 'aside' && insp && !insp.classList.contains('hidden')) loadAsides();
  };

  function openInspector(tab) {
    if (!insp) return;
    // 窄一点的大屏上，检查器 + 搜索侧栏会把聊天区挤到 400px 以下
    if (window.innerWidth < 1400 && typeof closeSearchPanel === 'function') {
      closeSearchPanel();
    }
    // 笔记停靠和检查器抢同一块右侧空间：先收起笔记（全屏时同理）
    const nm = document.querySelector('#notes-modal');
    if (nm && !nm.classList.contains('hidden')
        && typeof closeNotesModal === 'function') {
      try { closeNotesModal(); } catch (e) { /* 笔记是可选面板 */ }
    }
    insp.classList.remove('hidden');
    if (inspBtn) {
      inspBtn.classList.add('is-on');
      inspBtn.setAttribute('aria-expanded', 'true');
    }
    selectTab(tab || activeTab);
    if (!window.__lastContext) setContext(null);
  }

  function closeInspector() {
    if (!insp) return;
    insp.classList.add('hidden');
    if (inspBtn) {
      inspBtn.classList.remove('is-on');
      inspBtn.setAttribute('aria-expanded', 'false');
    }
  }

  if (inspBtn) {
    inspBtn.onclick = () => {
      if (insp && insp.classList.contains('hidden')) openInspector();
      else closeInspector();
    };
  }
  const closeBtn = $('#insp-close');
  if (closeBtn) closeBtn.onclick = closeInspector;

  $$('.insp-tab').forEach((b) => {
    b.onclick = () => selectTab(b.dataset.itab);
  });
  const ctxMore = $('#insp-ctx-more');
  if (ctxMore) ctxMore.onclick = () => { if (typeof openCtxDetail === 'function') openCtxDetail(); };
  const usageMore = $('#insp-usage-more');
  if (usageMore) usageMore.onclick = () => { if (typeof openUsage === 'function') openUsage('day7'); };
  const trajMore = $('#insp-traj-more');
  if (trajMore) {
    trajMore.onclick = () => {
      if (typeof openTrajectory === 'function') openTrajectory();
    };
  }
  const toolsMore = $('#insp-tools-more');
  if (toolsMore) toolsMore.onclick = scrollToLastReply;

  // 首屏上下文数据由 chat.js 的 renderContextBar 缓存到 window.__lastContext
  if (window.__lastContext) setContext(window.__lastContext);

  /* ---------------- 设置入口 ---------------- */

  window.gmOpenSettings = function (opts) {
    if (typeof openModal !== 'function') return;
    if (typeof opts === 'string') openModal({ tab: opts });
    else openModal(opts);
  };
  const settingsBtn = $('#btn-settings');
  if (settingsBtn) settingsBtn.onclick = () => window.gmOpenSettings();

  // 插件市场脚本按需加载，入口先在这里兜底绑定（hall-plugins.js 加载后会覆盖为同一行为）
  const pluginsBtn = $('#btn-plugins');
  if (pluginsBtn) {
    pluginsBtn.onclick = () => {
      if (typeof setUiMode === 'function') setUiMode('plugins');
      else if (typeof applyUiMode === 'function') applyUiMode('plugins');
    };
  }

  /* ---------------- 工作台「更多」浮层 ---------------- */
  (function workMore() {
    const btn = $('#work-more-btn');
    const menu = $('#work-more-menu');
    if (!btn || !menu) return;
    const setOpen = (open) => {
      menu.classList.toggle('hidden', !open);
      btn.classList.toggle('is-open', open);
      btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    };
    btn.onclick = (e) => {
      e.stopPropagation();
      setOpen(menu.classList.contains('hidden'));
    };
    document.addEventListener('click', (e) => {
      if (menu.classList.contains('hidden')) return;
      const t = e.target;
      if (t && t.closest && (t.closest('#work-more-menu') || t.closest('#work-more-btn'))) return;
      setOpen(false);
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && !menu.classList.contains('hidden')) setOpen(false);
    });
    // 点菜单里的任一工具后自动收起（原按钮事件不受影响）
    menu.addEventListener('click', (e) => {
      if (e.target && e.target.closest && e.target.closest('button')) setOpen(false);
    });
  })();

  /* ---------------- 无障碍：对话框语义 / 图标按钮名称 / 焦点陷阱 ---------------- */

  const DIALOGS = [
    ['#modal', '设置'],
    ['#review-modal', '变更审阅'],
    ['#usage-modal', '用量统计'],
    ['#ctx-modal', '上下文与压缩'],
    ['#kb-modal', '知识库'],
    ['#notes-modal', '笔记'],
    ['#input-modal', '输入'],
    ['#rollback-modal', '回滚确认'],
    ['#hk-panel', '快捷键'],
    ['#work-modal', '工作台弹窗'],
    ['#trajectory-modal', '轨迹'],
    ['#session-search-modal', '搜索会话'],
    ['#palette', '命令面板'],
  ];

  DIALOGS.forEach(([sel, label]) => {
    const el = $(sel);
    if (!el) return;
    const existing = el.getAttribute('role');
    if (!existing) el.setAttribute('role', 'dialog');
    // role=presentation 的根节点只做遮罩，对话框语义在内部面板上，不要重复标注
    if (existing === 'presentation') return;
    if (!el.getAttribute('aria-modal')) el.setAttribute('aria-modal', 'true');
    if (!el.getAttribute('aria-label')) el.setAttribute('aria-label', label);
  });

  function openDialog() {
    for (const [sel] of DIALOGS) {
      const el = $(sel);
      if (el && !el.classList.contains('hidden')) return el;
    }
    return null;
  }

  // Tab / Shift+Tab 在打开的弹窗内循环；不改变任何弹窗自己的关闭逻辑
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Tab') return;
    const dlg = openDialog();
    if (!dlg) return;
    const focusables = Array.from(dlg.querySelectorAll(
      'a[href],button:not([disabled]),input:not([disabled]):not([type="hidden"]),'
      + 'select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])'
    )).filter((el) => el.getClientRects().length);
    if (!focusables.length) return;
    const first = focusables[0];
    const last = focusables[focusables.length - 1];
    const active = document.activeElement;
    if (!dlg.contains(active)) { e.preventDefault(); first.focus(); return; }
    if (e.shiftKey && active === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && active === last) { e.preventDefault(); first.focus(); }
  }, true);

  // 纯图标按钮补可读名称：优先用 title 的第一段
  function nameIconButtons(root) {
    $$('button', root).forEach((b) => {
      if (b.getAttribute('aria-label')) return;
      if ((b.textContent || '').trim()) return;
      const t = (b.getAttribute('title') || '').split('：')[0].trim();
      if (t) b.setAttribute('aria-label', t.slice(0, 40));
    });
  }
  nameIconButtons(document);
  if (window.requestIdleCallback) requestIdleCallback(() => nameIconButtons(document), { timeout: 2000 });
  else setTimeout(() => nameIconButtons(document), 800);

  window.uiShell = { openInspector, closeInspector, syncTopbar, selectTab };
})();
