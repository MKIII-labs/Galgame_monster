/* ============================================================
   输入区「能力」浮层
   ------------------------------------------------------------
   把深度思考 / 联网搜索 / Agent / AI 优化从常驻工具栏收进一个浮层；
   原有按钮与 ID 不变，事件绑定不受影响。只做展示层的开合与状态提示。
   ============================================================ */
'use strict';

(function () {
  const $ = (sel) => document.querySelector(sel);
  const btn = $('#btn-cap');
  const tray = $('#cap-tray');
  const dot = $('#cap-dot');
  if (!btn || !tray) return;

  const ITEMS = [
    ['#btn-think', '深度思考'],
    ['#btn-search', '联网'],
    ['#btn-plan', '计划'],
    ['#btn-agent', 'Agent'],
  ];

  function activeNames() {
    return ITEMS.filter(([sel]) => {
      const el = $(sel);
      return el && el.classList.contains('active');
    }).map(([, name]) => name);
  }

  function sync() {
    const names = activeNames();
    if (dot) dot.classList.toggle('hidden', !names.length);
    btn.title = names.length
      ? '能力：已开启 ' + names.join(' / ')
      : '能力：深度思考 / 联网搜索 / Agent / AI 优化';
    btn.setAttribute('aria-label', btn.title);
  }

  function setOpen(open) {
    tray.classList.toggle('hidden', !open);
    btn.classList.toggle('is-open', open);
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open) {
      const first = tray.querySelector('button:not([disabled])');
      if (first) setTimeout(() => first.focus(), 0);
    }
  }

  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    setOpen(tray.classList.contains('hidden'));
  });

  document.addEventListener('click', (e) => {
    if (tray.classList.contains('hidden')) return;
    const t = e.target;
    if (t && t.closest && (t.closest('#cap-tray') || t.closest('#btn-cap'))) return;
    setOpen(false);
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !tray.classList.contains('hidden')) setOpen(false);
  });

  const left = $('.toolbar-left');
  if (left) {
    new MutationObserver(sync).observe(left, {
      subtree: true, attributes: true, attributeFilter: ['class'],
    });
  }
  sync();

  window.uiCap = { close: () => setOpen(false), sync };
})();
