'use strict';

/* 设置弹窗 · theme.js
   主题 / 雾光氛围 / 动效
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
  // 只在切换瞬间挂一次过渡类，避免长期挂着全局 transition 影响性能
  root.classList.add('theme-anim');
  clearTimeout(root._themeTimer);
  root._themeTimer = setTimeout(() => root.classList.remove('theme-anim'), 320);
  root.setAttribute('data-theme', t);
  root.setAttribute('data-style', meta.style);
  root.setAttribute('data-theme-mode', meta.dark ? 'dark' : 'light');
  // 记到本地：下次开屏前就能恢复主题，避免闪一下反色。
  // 设置页里只是「预览」时不写 gm.theme，点取消可以干净地回到已保存主题。
  try {
    if (persist) localStorage.setItem('gm.theme', t);
    if (meta.style === 'flat') localStorage.setItem('gm.flatMode', meta.dark ? 'dark' : 'light');
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
   设计风格 / 主题模式 / 雾光氛围
   ------------------------------------------------------------
   · 雾光（aurora）：单一模式，拥有独立的氛围（静谧 / 流动 / 辉光）
   · 现代扁平化（flat）：保留浅色 / 深色，左下角按钮在这套风格下切换深浅
   设置里的隐藏 radio 仍保存具体主题（aurora / light / dark），保存逻辑不变。
   ============================================================ */
const AURORA_MOODS = ['calm', 'flow', 'glow'];
const AURORA_MOOD_META = {
  calm: { label: '静谧' },
  flow: { label: '流动' },
  glow: { label: '辉光' },
};

function currentMood() {
  const m = document.documentElement.getAttribute('data-mood');
  return AURORA_MOODS.indexOf(m) >= 0 ? m : 'flow';
}
function currentMotion() {
  return document.documentElement.getAttribute('data-motion') === 'lite' ? 'lite' : 'full';
}

function syncAuroraMood(mood) {
  const current = AURORA_MOODS.indexOf(mood) >= 0 ? mood : currentMood();
  $$('.mood-card').forEach((card) => {
    const on = card.dataset.moodChoice === current;
    card.classList.toggle('is-on', on);
    card.setAttribute('aria-checked', on ? 'true' : 'false');
    card.tabIndex = on ? 0 : -1;
  });
}

function syncMotionCards(mode) {
  const current = mode === 'lite' ? 'lite' : currentMotion();
  $$('.motion-card').forEach((card) => {
    const on = card.dataset.motionChoice === current;
    card.classList.toggle('is-on', on);
    card.setAttribute('aria-checked', on ? 'true' : 'false');
    card.tabIndex = on ? 0 : -1;
  });
}

// 左下角按钮随风格切换语义：雾光=氛围，扁平化=深浅
function syncThemeButton(theme) {
  const t = normalizeTheme(theme || document.documentElement.getAttribute('data-theme'));
  const style = themeStyle(t);
  const btn = document.getElementById('btn-theme');
  if (!btn) return;
  btn.dataset.style = style;
  const label = btn.querySelector('.theme-rail-label');
  if (style === 'flat') {
    const next = t === 'dark' ? '浅色' : '深色';
    btn.title = '切换深浅（当前' + (t === 'dark' ? '深色' : '浅色') + '，点击切到' + next + '）';
    btn.setAttribute('aria-label', '切换深浅');
    if (label) label.textContent = '深浅';
  } else {
    const meta = AURORA_MOOD_META[currentMood()] || AURORA_MOOD_META.flow;
    btn.title = '氛围：' + meta.label + '（点击切换 静谧 / 流动 / 辉光）';
    btn.setAttribute('aria-label', '切换氛围');
    if (label) label.textContent = '氛围';
    btn.dataset.mood = currentMood();
  }
}

function syncThemeCards(theme) {
  const current = normalizeTheme(theme
    || (document.querySelector('input[name="theme"]:checked') || {}).value
    || document.documentElement.getAttribute('data-theme'));
  const style = themeStyle(current);
  const radio = document.querySelector('input[name="theme"][value="' + current + '"]');
  if (radio) radio.checked = true;

  $$('.theme-card').forEach((card) => {
    const on = card.dataset.styleChoice === style;
    card.classList.toggle('is-on', on);
    card.setAttribute('aria-checked', on ? 'true' : 'false');
    card.tabIndex = on ? 0 : -1;
  });

  // 扁平化才显示深浅模式；雾光才显示氛围 / 动效
  const modeRow = document.getElementById('theme-mode-row');
  if (modeRow) modeRow.hidden = style !== 'flat';
  const flatMode = current === 'dark' ? 'dark' : 'light';
  $$('.theme-mode-btn').forEach((btn) => {
    const on = style === 'flat' && btn.dataset.flatMode === flatMode;
    btn.classList.toggle('is-on', on);
    btn.setAttribute('aria-checked', on ? 'true' : 'false');
    btn.tabIndex = on ? 0 : -1;
  });
  const moodSection = document.getElementById('mood-section');
  if (moodSection) moodSection.hidden = style !== 'aurora';

  syncAuroraMood(currentMood());
  syncMotionCards(currentMotion());
  syncThemeButton(current);
}

// 选「雾光」= 切到 aurora；选「现代扁平化」= 回到上次的浅色 / 深色
// 设置页里只做预览（不写 gm.theme），点保存才真正持久化
function applyStyleChoice(style) {
  if (style === 'flat') {
    let last = 'light';
    try { last = localStorage.getItem('gm.flatMode') === 'dark' ? 'dark' : 'light'; } catch (e) { /* 忽略 */ }
    applyTheme(last, { persist: false });
  } else {
    applyTheme('aurora', { persist: false });
  }
}

function applyFlatMode(mode) {
  applyTheme(mode === 'dark' ? 'dark' : 'light', { persist: false });
}

function applyMood(mood) {
  const m = AURORA_MOODS.indexOf(mood) >= 0 ? mood : 'flow';
  document.documentElement.setAttribute('data-mood', m);
  try { localStorage.setItem('gm.mood', m); } catch (e) { /* 忽略 */ }
  syncAuroraMood(m);
  syncThemeButton();
  // 切换反馈：氛围按钮做一次轻脉冲（结束后移除，避免覆盖氛围球的持续动画）
  const btn = document.getElementById('btn-theme');
  if (btn) {
    btn.classList.remove('is-mood-pulse');
    void btn.offsetWidth;
    btn.classList.add('is-mood-pulse');
    clearTimeout(btn._moodPulseTimer);
    btn._moodPulseTimer = setTimeout(() => btn.classList.remove('is-mood-pulse'), 700);
  }
}

function applyMotion(mode) {
  const m = mode === 'lite' ? 'lite' : 'full';
  document.documentElement.setAttribute('data-motion', m);
  try { localStorage.setItem('gm.motion', m); } catch (e) { /* 忽略 */ }
  syncMotionCards(m);
}

// 快速循环：静谧 → 流动 → 辉光
function cycleMood() {
  const next = AURORA_MOODS[(AURORA_MOODS.indexOf(currentMood()) + 1) % AURORA_MOODS.length];
  applyMood(next);
  return next;
}

// 开屏恢复：氛围 / 动效写在 <html> 上，这里只同步设置面板与按钮
function syncAuroraPrefs() {
  syncAuroraMood(currentMood());
  syncMotionCards(currentMotion());
  syncThemeButton();
}

document.addEventListener('click', (e) => {
  const hit = e.target && e.target.closest;
  if (!hit) return;
  const styleCard = hit.call(e.target, '.theme-card');
  if (styleCard && styleCard.dataset.styleChoice) {
    applyStyleChoice(styleCard.dataset.styleChoice);
    return;
  }
  const flatBtn = hit.call(e.target, '.theme-mode-btn');
  if (flatBtn && flatBtn.dataset.flatMode) {
    applyFlatMode(flatBtn.dataset.flatMode);
    return;
  }
  const moodCard = hit.call(e.target, '.mood-card');
  if (moodCard && moodCard.dataset.moodChoice) {
    applyMood(moodCard.dataset.moodChoice);
    return;
  }
  const motionCard = hit.call(e.target, '.motion-card');
  if (motionCard && motionCard.dataset.motionChoice) {
    applyMotion(motionCard.dataset.motionChoice);
  }
});

document.addEventListener('keydown', (e) => {
  const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[e.key];
  if (!step) return;
  const hit = e.target && e.target.closest;
  if (!hit) return;
  const groups = [
    ['.theme-card', 'styleChoice', applyStyleChoice],
    ['.theme-mode-btn', 'flatMode', applyFlatMode],
    ['.mood-card', 'moodChoice', applyMood],
    ['.motion-card', 'motionChoice', applyMotion],
  ];
  for (const [sel, key, run] of groups) {
    const card = hit.call(e.target, sel);
    if (!card) continue;
    const items = $$(sel).filter((el) => !el.closest('[hidden]'));
    const i = items.indexOf(card);
    if (i < 0) return;
    e.preventDefault();
    const next = items[(i + step + items.length) % items.length];
    if (!next) return;
    run(next.dataset[key]);
    next.focus();
    return;
  }
});
