/* ============================================================
   界面重构壳层（P1）：顶栏同步 / 右侧检查器
   ------------------------------------------------------------
   只做展示与入口整合：
   - 上下文 / 工具 / 用量 / 轨迹四个查看器收进右侧检查器；
   - 原有 openCtxDetail / openUsage / #btn-trajectory 仍可复用；
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
    gal: ['演出', 'GAL'],
  };

  function currentMode() {
    const main = $('#main');
    if (!main) return 'solo';
    for (const key of ['ide', 'hall', 'work', 'plugins', 'gal']) {
      if (main.classList.contains(key + '-mode')) return key;
    }
    return 'solo';
  }

  function syncTopbar() {
    const meta = MODE_META[currentMode()] || MODE_META.solo;
    const title = $('#app-page-title');
    const sub = $('#app-page-sub');
    if (title && title.textContent !== meta[0]) title.textContent = meta[0];
    if (sub && sub.textContent !== meta[1]) sub.textContent = meta[1];
    const pb = $('#btn-plugins');
    if (pb) pb.classList.toggle('active', currentMode() === 'plugins');
  }

  const main = $('#main');
  if (main) {
    new MutationObserver(syncTopbar).observe(main, { attributes: true, attributeFilter: ['class'] });
  }
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
    if (!el) return;
    const rows = $$('#chat .msg.ai');
    const last = rows[rows.length - 1];
    if (!last) {
      el.textContent = '还没有 AI 回复。';
      return;
    }
    const count = last.querySelectorAll('.tool-card').length;
    el.textContent = count
      ? `最近一条回复包含 ${count} 个工具调用。`
      : '最近一条回复没有工具调用。';
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
    last.scrollIntoView({ block: 'center', behavior: 'smooth' });
  }

  function openInspector(tab) {
    if (!insp) return;
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
      const btn = $('#btn-trajectory');
      if (btn) btn.click();
    };
  }
  const toolsMore = $('#insp-tools-more');
  if (toolsMore) toolsMore.onclick = scrollToLastReply;

  // 首屏上下文数据由 chat.js 的 renderContextBar 缓存到 window.__lastContext
  if (window.__lastContext) setContext(window.__lastContext);

  window.uiShell = { openInspector, closeInspector, syncTopbar, selectTab };
})();
