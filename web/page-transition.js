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

  function gmSkeleton(el, rows) {
    if (!el) return;
    gmSkeletonClear(el);
    const box = document.createElement('div');
    box.className = 'page-skeleton skeleton';
    const n = Number(rows) || 4;
    for (let i = 0; i < n; i += 1) {
      const line = document.createElement('div');
      line.className = 'sk-line';
      line.style.width = i === 0 ? '38%' : (i % 3 === 0 ? '72%' : '100%');
      box.appendChild(line);
    }
    el.appendChild(box);
  }

  function gmSkeletonClear(el) {
    if (!el) return;
    const box = el.querySelector(':scope > .page-skeleton');
    if (box) box.remove();
  }

  window.gmPageIn = gmPageIn;
  window.gmPageTransition = gmPageTransition;
  window.gmPrefetchGroup = gmPrefetchGroup;
  window.gmSkeleton = gmSkeleton;
  window.gmSkeletonClear = gmSkeletonClear;
})();
