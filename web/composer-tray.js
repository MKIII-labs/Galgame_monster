/* ============================================================
   输入区「待发送托盘」
   ------------------------------------------------------------
   把排队消息 / 上下文引用 / 选中文本 / 附件四条原本各自出现的带子
   合并成一个托盘：只在至少有一项时出现，并给出数量提示。
   子节点与 ID 全部沿用原实现，这里只做显隐与计数。
   ============================================================ */
'use strict';

(function () {
  const $ = (sel) => document.querySelector(sel);
  const tray = $('#pending-tray');
  const countEl = $('#pending-count');
  if (!tray) return;

  const GROUPS = [
    ['附件', ['#attach-thumbs .attach-thumb-wrap', '#attach-docs .attach-doc', '#attach-pastes .attach-paste']],
    ['引用', ['#ref-bar .quote-chip', '#quote-bar .quote-chip']],
  ];

  function countIn(selectors) {
    let n = 0;
    selectors.forEach((sel) => { n += document.querySelectorAll(sel).length; });
    return n;
  }

  // 排队消息只有一条时列表是收起的（DOM 里没有 .queue-item），
  // 所以队列条本身可见时按至少 1 条计。
  function queueCount() {
    const n = document.querySelectorAll('#queue-list .queue-item').length;
    const bar = document.querySelector('#queue-bar');
    return (bar && !bar.classList.contains('hidden')) ? Math.max(n, 1) : n;
  }

  let raf = 0;
  function sync() {
    raf = 0;
    const parts = [];
    let total = 0;
    GROUPS.forEach(([label, sels]) => {
      const n = countIn(sels);
      if (n) { total += n; parts.push(label + ' ' + n); }
    });
    const qn = queueCount();
    if (qn) { total += qn; parts.push('排队 ' + qn); }
    if (total) {
      tray.classList.remove('hidden');
      if (countEl) countEl.textContent = parts.join(' · ');
    } else {
      tray.classList.add('hidden');
      if (countEl) countEl.textContent = '';
    }
  }

  function schedule() {
    if (raf) return;
    raf = requestAnimationFrame(sync);
  }

  // 子条目的显隐与增删都由原逻辑直接改 DOM，这里统一观察后重算
  new MutationObserver(schedule).observe(tray, {
    subtree: true,
    childList: true,
    attributes: true,
    attributeFilter: ['class'],
  });

  sync();
  window.uiPendingTray = { sync };
})();
