/* ============================================================
   IDE 底部面板页签（终端 / 问题 / 大纲 / 结果）
   ------------------------------------------------------------
   #ide-term 与 #ide-intel 的实现、事件与快捷键全部沿用原代码；
   这里只负责：
   - 统一页签栏与显隐（一次只显示一个面板）；
   - 把问题 / 大纲的数量与「结果」页签可用性同步到共享页签；
   - 关闭按钮同时关闭两个面板。
   ============================================================ */
'use strict';

(function () {
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));

  const wrap = $('#ide-bottom');
  const term = $('#ide-term');
  const intel = $('#ide-intel');
  if (!wrap || !term || !intel) return;

  const tabs = $$('#ide-bottom .ide-bottom-tab');
  const closeBtn = $('#ide-bottom-close');
  const isOpen = (el) => !!el && !el.classList.contains('hidden');
  const intelTab = () => {
    const b = $('#ide-intel .ide-intel-tab.active');
    return b ? b.dataset.intelTab : '';
  };

  function sync() {
    const termOn = isOpen(term);
    const intelOn = isOpen(intel);
    wrap.classList.toggle('hidden', !termOn && !intelOn);

    tabs.forEach((b) => {
      const kind = b.dataset.btab;
      const on = (kind === 'term' && termOn) || (intelOn && kind === intelTab());
      b.classList.toggle('active', on);
      b.setAttribute('aria-selected', on ? 'true' : 'false');
    });

    const pc = $('#ide-bottom-pcount');
    const oc = $('#ide-bottom-ocount');
    const srcP = $('#ide-intel-pcount');
    const srcO = $('#ide-intel-ocount');
    if (pc) pc.textContent = srcP ? (srcP.textContent || '').trim() : '';
    if (oc) oc.textContent = srcO ? (srcO.textContent || '').trim() : '';

    const srcR = $('#ide-intel .ide-intel-tab[data-intel-tab="results"]');
    const dstR = tabs.find((b) => b.dataset.btab === 'results');
    if (dstR) dstR.classList.toggle('hidden', !srcR || srcR.classList.contains('hidden'));
  }

  function select(kind) {
    if (kind === 'term') {
      if (typeof ideTermToggle === 'function') ideTermToggle(true);
      setTimeout(sync, 0);
      return;
    }
    if (typeof ideTermToggle === 'function') ideTermToggle(false);
    const btn = $('#ide-intel .ide-intel-tab[data-intel-tab="' + kind + '"]');
    if (btn) {
      if (isOpen(intel) && btn.classList.contains('active')) {
        if (typeof ideIntelToggle === 'function') ideIntelToggle(kind, true);
      } else {
        btn.click();
      }
    }
    setTimeout(sync, 0);
  }

  tabs.forEach((b) => {
    b.onclick = () => select(b.dataset.btab);
  });

  if (closeBtn) {
    closeBtn.onclick = () => {
      if (isOpen(term) && typeof ideTermToggle === 'function') ideTermToggle(false);
      if (isOpen(intel) && typeof ideIntelToggle === 'function') ideIntelToggle(null, false);
      setTimeout(sync, 0);
    };
  }

  new MutationObserver(sync).observe(term, { attributes: true, attributeFilter: ['class'] });
  new MutationObserver(sync).observe(intel, {
    subtree: true,
    childList: true,
    characterData: true,
    attributes: true,
    attributeFilter: ['class'],
  });

  sync();
  window.ideBottom = { sync, select };
})();
