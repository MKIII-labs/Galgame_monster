'use strict';

/* ============================================================
   页面切换过渡（顶层工作区 / 大厅 / 工作台 / 设置页签 / 大面板）
   ------------------------------------------------------------
   - 顶层工作区用 View Transitions API：只给 #main 命名过渡组，
     侧栏 / 顶栏保持不动，避免整窗一起动。
   - 内层页面（大厅模块、工作台视图、设置页签…）用 .page-in
     （淡入 + 4px 上移），不 snapshot，重内容不会掉帧。
   - 不支持 API / 减弱动效 / 页面不可见时直接切换，保证不卡死。
   ============================================================ */
(function () {
  function reducedMotion() {
    return !!(window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches);
  }

  /* 单块内容的入场：淡入 + 轻微上移（可重复触发） */
  function gmPageIn(el) {
    if (!el || reducedMotion()) return;
    if (typeof replayEnter === 'function') { replayEnter(el, 'page-in'); return; }
    el.classList.remove('page-in');
    void el.offsetWidth;
    el.classList.add('page-in');
  }

  /* 顶层切换：优先 View Transitions；否则执行 run 并给 fallbackEl 补一次入场。
     onFallback：没有快照能力时，需要额外给新内容加入场的回调。 */
  function gmPageTransition(run, fallbackEl, onFallback) {
    const fallback = () => {
      if (typeof onFallback === 'function') onFallback();
      else gmPageIn(fallbackEl);
    };
    if (typeof run !== 'function') return;
    if (document.hidden || reducedMotion() || !document.startViewTransition) {
      run();
      fallback();
      return;
    }
    if (window.__gmPageTx) { run(); return; }     // 连续点击：直接切，不排队
    window.__gmPageTx = true;
    let tx = null;
    try {
      tx = document.startViewTransition(() => { run(); });
    } catch (e) {
      window.__gmPageTx = false;
      run();
      fallback();
      return;
    }
    const done = () => { window.__gmPageTx = false; };
    if (tx && tx.finished && tx.finished.then) tx.finished.then(done, done);
    else done();
  }

  /* 入口悬停预热：只加载一次，失败不影响点击后的正常加载 */
  function gmPrefetchGroup(kind) {
    if (!kind || typeof window.gmLoadGroup !== 'function') return;
    try { window.gmLoadGroup(kind).catch(() => {}); } catch (e) { /* 忽略 */ }
  }

  /* 骨架 markup：settings 等用 innerHTML 的地方也复用这一套（行宽递减 + 读屏旁白） */
  function gmSkeletonHtml(rows) {
    const n = Number(rows) || 4;
    const widths = [38, 100, 88, 96, 74, 90, 62];   // 首行标题短，其余逐行收窄
    let s = '<div class="page-skeleton skeleton" aria-busy="true">';
    for (let i = 0; i < n; i += 1) {
      s += '<div class="sk-line" style="width:' + widths[i % widths.length] + '%"></div>';
    }
    s += '<span class="gm-sr-only" role="status" aria-live="polite">正在加载…</span></div>';
    return s;
  }

  function gmSkeleton(el, rows) {
    if (!el) return;
    gmSkeletonClear(el);
    el.insertAdjacentHTML('beforeend', gmSkeletonHtml(rows));
  }

  function gmSkeletonClear(el) {
    if (!el) return;
    const box = el.querySelector(':scope > .page-skeleton');
    if (box) box.remove();
  }

  /* 统一的行内加载旁白（转圈 + 一句状态）。extraClass 用于沿用各模块的定位容器，
     例如知识库图谱的绝对定位骨架、工作台 AI 面板的行内占位。 */
  function gmLoadingHtml(text, extraClass) {
    const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    const cls = 'gm-loading' + (extraClass ? ' ' + extraClass : '');
    return '<div class="' + cls + '" role="status" aria-live="polite">'
      + '<span class="gm-spin" aria-hidden="true"></span>'
      + '<span>' + esc(text || '正在加载…') + '</span></div>';
  }

  window.gmPageIn = gmPageIn;
  window.gmPageTransition = gmPageTransition;
  window.gmPrefetchGroup = gmPrefetchGroup;
  window.gmSkeleton = gmSkeleton;
  window.gmSkeletonHtml = gmSkeletonHtml;
  window.gmSkeletonClear = gmSkeletonClear;
  window.gmLoadingHtml = gmLoadingHtml;
})();
