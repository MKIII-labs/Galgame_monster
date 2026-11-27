'use strict';

/* 设置弹窗 · theme.js
   明暗主题 / 应用窗口标题栏
   原 web/settings.js 拆分而来：按 index.html 中的顺序加载，共享全局作用域。 */

/* ============================================================
   主题
   ============================================================ */
/* ---------------- 应用窗口标题栏（Windows） ----------------
   标题栏由系统绘制，网页改不动；交给后端用 DWM 上色。
   颜色总取「当前实际渲染」的侧栏背景 —— 界面主题 / IDE 主题切换后都能跟上，
   不再只认 applyTheme 那一处。 */
let _winChromeTimer = null;
let _winChromeLast = '';

function windowChromeColor() {
  const el = document.querySelector('#sidebar');
  if (!el) return { dark: false, bg: '' };
  const cs = getComputedStyle(el);
  const scheme = String(cs.colorScheme || '') ||
                 String(getComputedStyle(document.documentElement).colorScheme || '');
  const dark = scheme.indexOf('dark') >= 0;
  const rgb = /^rgba?\(([^)]+)\)/.exec(String(cs.backgroundColor || ''));
  let bg = '';
  if (rgb) {
    const p = rgb[1].split(',').map((x) => parseFloat(x));
    if (p.length < 4 || p[3] !== 0) {         // 透明就交给后端用默认色
      const h = (n) => Math.max(0, Math.min(255, Math.round(n))).toString(16).padStart(2, '0');
      bg = '#' + h(p[0]) + h(p[1]) + h(p[2]);
    }
  }
  return { dark: dark, bg: bg };
}

function syncWindowChrome() {
  clearTimeout(_winChromeTimer);
  _winChromeTimer = setTimeout(() => {
    const c = windowChromeColor();
    const key = (c.dark ? 'dark' : 'light') + ':' + c.bg;
    if (key === _winChromeLast) return;       // 颜色没变就不重复请求
    _winChromeLast = key;
    try {
      fetch('/api/window/theme', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Auth-Token': TOKEN },
        body: JSON.stringify({ theme: c.dark ? 'dark' : 'light', bg: c.bg }),
      }).catch(() => {});
    } catch (e) { /* 忽略 */ }
  }, 200);
}

function applyTheme(theme, opts) {
  const t = normalizeTheme(theme);
  const meta = THEME_META[t];
  const persist = !opts || opts.persist !== false;
  const root = document.documentElement;
  root.setAttribute('data-theme', t);
  root.setAttribute('data-theme-mode', meta.dark ? 'dark' : 'light');
  // 记到本地：下次开屏前就能恢复主题，避免闪一下反色。
  // 设置页里只是「预览」时不写 gm.theme，点取消可以干净地回到已保存主题。
  try {
    if (persist) localStorage.setItem('gm.theme', t);
  } catch (e) { /* 忽略 */ }
  // 原生 UI 跟随主题：color-scheme 影响控件/滚动条，theme-color 影响应用窗口的标题栏
  root.style.colorScheme = meta.dark ? 'dark' : 'light';
  const tc = document.getElementById('theme-color');
  if (tc) tc.setAttribute('content', meta.title);
  // 应用窗口标题栏：按实际渲染背景同步（与 IDE 主题共用同一套逻辑）
  syncWindowChrome();
  const light = document.getElementById('hljs-light');
  const dark = document.getElementById('hljs-dark');
  if (light) { light.disabled = meta.dark; light.media = 'all'; }
  if (dark) { dark.disabled = !meta.dark; dark.media = 'all'; }
  // 代码编辑器（Monaco）也跟随明暗
  if (typeof ideSyncMonacoTheme === 'function') { try { ideSyncMonacoTheme(); } catch (e) { /* 忽略 */ } }
  // 图谱配色取自 CSS token：打开着的图谱要重绘一次，否则节点还停在旧主题的颜色
  const kbModal = document.getElementById('kb-modal');
  if (kbModal && !kbModal.classList.contains('hidden')
      && state.kbData && typeof drawKbGraph === 'function') {
    drawKbGraph();
  }
  const notesModal = document.getElementById('notes-modal');
  if (notesModal && !notesModal.classList.contains('hidden')
      && typeof drawNotesGraph === 'function') {
    drawNotesGraph();
  }
  // 设置面板 / 左下角按钮同步到当前风格与模式
  syncThemeCards(t);
}

/* ============================================================
   主题：浅色 / 深色
   ============================================================ */

function syncThemeButton(theme) {
  const t = normalizeTheme(theme || document.documentElement.getAttribute('data-theme'));
  const btn = document.getElementById('btn-theme');
  if (!btn) return;
  const label = btn.querySelector('.theme-rail-label');
  const isDark = t === 'dark';
  btn.title = '切换深浅（当前' + (isDark ? '深色' : '浅色') +
    '，点击切到' + (isDark ? '浅色' : '深色') + '）';
  btn.setAttribute('aria-label', '切换深浅');
  if (label) label.textContent = '深浅';
}

function syncThemeCards(theme) {
  const current = normalizeTheme(theme
    || (document.querySelector('input[name="theme"]:checked') || {}).value
    || document.documentElement.getAttribute('data-theme'));
  const radio = document.querySelector('input[name="theme"][value="' + current + '"]');
  if (radio) radio.checked = true;

  $$('.theme-card').forEach((card) => {
    const on = card.dataset.themeChoice === current;
    card.classList.toggle('is-on', on);
    card.setAttribute('aria-checked', on ? 'true' : 'false');
    card.tabIndex = on ? 0 : -1;
  });

  syncThemeButton(current);
}

// 设置页里只做预览（不写 gm.theme），点保存才真正持久化
function applyThemeChoice(theme) {
  const t = normalizeTheme(theme);
  applyTheme(t, { persist: false });
  const radio = document.querySelector('input[name="theme"][value="' + t + '"]');
  if (radio) radio.checked = true;
  syncThemeCards(t);
}

document.addEventListener('click', (e) => {
  const hit = e.target && e.target.closest;
  if (!hit) return;
  const card = hit.call(e.target, '.theme-card');
  if (card && card.dataset.themeChoice) {
    applyThemeChoice(card.dataset.themeChoice);
  }
});

document.addEventListener('keydown', (e) => {
  const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[e.key];
  if (!step) return;
  const hit = e.target && e.target.closest;
  if (!hit) return;
  const card = hit.call(e.target, '.theme-card');
  if (!card) return;
  const items = $$('.theme-card').filter((el) => !el.closest('[hidden]'));
  const i = items.indexOf(card);
  if (i < 0) return;
  e.preventDefault();
  const next = items[(i + step + items.length) % items.length];
  if (!next) return;
  applyThemeChoice(next.dataset.themeChoice);
  next.focus();
});
