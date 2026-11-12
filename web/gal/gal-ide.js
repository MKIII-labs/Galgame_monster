async function galEnterMode() {
  await galLoadCharacters();
  const s = curSession();
  await galSetCharacter((s && s.gal_character_id) || galState.charId || '');
  galApplyTheme();
  galApplyViewPrefs();
  requestAnimationFrame(() => galSyncSpriteLift());
  galState.ttsOn = !!(state.config && state.config.tts_enabled);
  galState.skip = false;
  galSyncTtsBtn();
  galSyncAutoBtn();
  galSetNode(s && s.gal_node_id ? { node_id: s.gal_node_id, round: s.gal_round || 0, max_rounds: 0 } : null);
  galRenderFromSession();
  galMaybeShowTip();                       // 首次进 GAL：右上角气泡引导（看过一次就不再出现）
}

/* ---- 首次进入 GAL 的引导气泡 ----
   只在第一次进入时出现；用户点「知道了」后写入 localStorage，之后不再打扰。 */
const GAL_TIP_KEY = 'gal_tip_seen_v1';
function galMaybeShowTip() {
  let seen = false;
  try { seen = localStorage.getItem(GAL_TIP_KEY) === '1'; } catch (e) { seen = true; }   // 存不了就别反复打扰
  if (seen) return;
  const box = $('#gal-tip');
  if (!box) return;
  // 等舞台先渲染出来再弹，避免和首帧的角色 / 背景加载挤在一起
  setTimeout(() => { if (state.uiMode === 'gal') box.classList.remove('hidden'); }, 700);
}
function galCloseTip() {
  const box = $('#gal-tip');
  if (box) box.classList.add('hidden');
  try { localStorage.setItem(GAL_TIP_KEY, '1'); } catch (e) { /* 忽略 */ }
}

/* 模式切换的入场淡入：只作用于「刚刚显示出来的那块」。
   IDE 跳过 —— Monaco 在 180ms 动画里每帧 resize，会明显掉帧。
   只做 opacity，不加 transform：面板里存在 position:fixed 的浮层（GAL 开场卡等），
   transform 会临时改变它们的包含块，导致瞬间错位。 */
function replayModeEnter(m) {
  if (typeof replayEnter !== 'function') return;
  if (m === 'ide') return;
  const sel = m === 'gal' ? '#gal-panel'
    : m === 'hall' ? '#hall-panel'
    : m === 'work' ? '#work-panel'
    : m === 'plugins' ? '#plugins-panel'
    : '.chat-col';
  replayEnter($(sel), 'panel-in');
}

function applyUiMode(mode) {
  const m = (mode === 'ide' || mode === 'gal' || mode === 'hall' || mode === 'work'
    || mode === 'plugins') ? mode : 'solo';
  // 切模式时收起全屏看图浮层，否则它会盖在别的模式上
  if (typeof imgZoomClose === 'function') imgZoomClose();
  const prevMode = state.uiMode;
  state.uiMode = m;
  const main = $('#main');
  if (main) {
    main.classList.toggle('ide-mode', m === 'ide');
    main.classList.toggle('gal-mode', m === 'gal');
    main.classList.toggle('hall-mode', m === 'hall');
    main.classList.toggle('work-mode', m === 'work');
    main.classList.toggle('plugins-mode', m === 'plugins');
  }
  const panel = $('#ide-panel');
  if (panel) panel.classList.toggle('hidden', m !== 'ide');
  // IDE 模式下侧栏宽度不做动画：Monaco 每帧 resize 都会重排，180ms 动画 = 明显卡顿
  const appEl = $('#app');
  if (appEl) appEl.classList.toggle('mode-ide', m === 'ide');
  const gp = $('#gal-panel');
  if (gp) gp.classList.toggle('hidden', m !== 'gal');
  const hp = $('#hall-panel');
  if (hp) hp.classList.toggle('hidden', m !== 'hall');
  const wp = $('#work-panel');
  if (wp) wp.classList.toggle('hidden', m !== 'work');
  const pp = $('#plugins-panel');
  if (pp) pp.classList.toggle('hidden', m !== 'plugins');
  $$('#mode-switch .ms-btn').forEach((b) => b.classList.toggle('active', b.dataset.mode === m));
  const pb = $('#btn-plugins');
  if (pb) pb.classList.toggle('active', m === 'plugins');
  galApplyTheme();                     // GAL 主题随模式进入/退出切换
  applyIdeTheme();                     // IDE 主题同理（属性只在 IDE 模式存在，别的模式零影响）
  if (m !== 'gal') {
    galToggleReview(false); galToggleScript(false);   // 离开 GAL 模式时收起浮层
    galStopAudioAll({ bgm: true });                   // 并停掉朗读 / BGM / 特效音，别在别的模式里继续响
    clearTimeout(_galCursorTimer); _galCursorTimer = null;
    const gp0 = $('#gal-panel'); if (gp0) gp0.classList.remove('gal-cursor-off');
  }
  if (m === 'ide') { ideInitOnce(); loadIdeTree(); ideWatchStart(); }
  else { ideWatchStop(); }
  if (m === 'gal') { galEnterMode(); }
  if (m === 'hall') {
    if (typeof hallEnterMode === 'function') hallEnterMode();
  } else if (typeof hallLeaveMode === 'function') {
    hallLeaveMode();                   // 离开大厅：把挂在 <body> 上的浮层（大图）收掉
  }
  if (m === 'work') {
    if (typeof workEnterMode === 'function') workEnterMode();
  } else if (typeof workLeaveMode === 'function') {
    workLeaveMode();
  }
  if (m === 'plugins' && typeof marketEnter === 'function') marketEnter();
  // 会话列表按模式过滤：进/出剧场时立刻换一份列表
  if (typeof renderSessions === 'function' && state.sessions && state.sessions.sessions) {
    renderSessions();
  }
  if (prevMode !== m) replayModeEnter(m);      // 只在真的换了模式时播入场，初始化那次不播
}

// IDE 首次进入时初始化：装载本地 Monaco，并做一次性绑定
let ideInited = false;

// 主题在 light / dark 间切换时，Seti 图标要换成对应的一套颜色（VS Code 也是两套）。
let ideThemeIconObs = null;
function ideRefreshThemeIcons() {
  // 只换图标，不重建整棵树：renderIdeTree 会把展开状态全部收起来
  const tree = $('#ide-tree');
  if (tree) {
    $$('#ide-tree .ide-row').forEach((row) => {
      const nm = row.querySelector('.ide-name');
      const old = row.querySelector('.ide-ico');
      if (!nm || !old) return;
      row.replaceChild(ideIcoEl(nm.textContent, row.dataset.dir === '1'), old);
    });
  }
  if (typeof renderIdeTabs === 'function') renderIdeTabs();
}
function ideWatchThemeIcons() {
  if (ideThemeIconObs || !window.MutationObserver) return;
  ideThemeIconObs = new MutationObserver(() => {
    try { ideRefreshThemeIcons(); } catch (e) { /* 忽略 */ }
  });
  ideThemeIconObs.observe(document.documentElement,
                          { attributes: true, attributeFilter: ['data-theme-mode'] });
}

/* 顶部提示条：只在第一次进 IDE 时出现，点 ✕ 后记住，不再打扰。
   常驻说明条是"网页横幅"而不是 IDE 的观感来源，所以默认不再一直挂在那儿。 */
const IDE_NOTICE_KEY = 'ide_notice_seen_v1';
function ideInitNotice() {
  const box = $('#ide-notice');
  if (!box) return;
  let seen = false;
  try { seen = localStorage.getItem(IDE_NOTICE_KEY) === '1'; } catch (e) { seen = false; }
  if (seen) box.classList.add('hidden');
  const x = $('#ide-notice-close');
  if (x) {
    x.onclick = () => {
      box.classList.add('hidden');
      try { localStorage.setItem(IDE_NOTICE_KEY, '1'); } catch (e) { /* 存不了就下次再提醒 */ }
    };
  }
  ideRenderWelcome();
}

/* 工作区行：折叠 / 展开整棵文件树，状态记在 localStorage */
const IDE_WS_KEY = 'ide_ws_collapsed_v1';
function ideBindWorkspace() {
  const ws = $('#ide-workspace');
  const tree = $('#ide-tree');
  if (!ws || !tree) return;
  const apply = (collapsed, animate) => {
    ws.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
    if (animate && typeof foldTransition === 'function') {
      foldTransition(ws, tree, !collapsed, { cls: 'collapsed' });
    } else {
      ws.classList.toggle('collapsed', collapsed);
    }
  };
  let collapsed = false;
  try { collapsed = localStorage.getItem(IDE_WS_KEY) === '1'; } catch (e) { collapsed = false; }
  apply(collapsed, false);
  ws.onclick = () => {
    const collapsedNow = !foldIsOpen(ws, 'collapsed');
    const next = !collapsedNow;              // next = 目标折叠态
    apply(next, true);
    try { localStorage.setItem(IDE_WS_KEY, next ? '1' : '0'); } catch (e) { /* 忽略 */ }
  };
  ws.onkeydown = (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); ws.click(); }
  };
  // 拖到「工作区」这一行 = 移到沙箱根目录
  ws.addEventListener('dragover', (e) => {
    if (!ideDragHasSource(e)) return;
    e.preventDefault();
    ws.classList.add('drop-target');
  });
  ws.addEventListener('dragleave', () => ws.classList.remove('drop-target'));
  ws.addEventListener('drop', (e) => {
    e.preventDefault();
    ws.classList.remove('drop-target');
    const from = ideDropSource(e) || state.ideDrag;
    if (from) ideMoveEntry(from, '');
  });
}

function ideInitOnce() {
  if (ideInited) return;
  ideInited = true;
  if (typeof ideTermInitOnce === 'function') ideTermInitOnce();
  state.ideSide = 'explorer';
  state.ideSideHidden = !!(state.config && state.config.ide_side_collapsed);   // 恢复上次的收起状态
  ideSyncSideUI();
  ideBindWorkspace();
  ideDragBindGlobal();                // 原生拖出后没有 dragend：兜底清理拖拽源
  ideCtxBind();                       // 文件树右键菜单（容器只绑定一次）
  ideTabsBind();                      // 顶部标签页：右键菜单 / 滚轮 / 快捷键
  ideWatchThemeIcons();               // light / dark 切换时重画文件图标
  ideWatchGitFocus();                 // 窗口重新聚焦时补拉 git 装饰
  ideIntelBindUI();
  const repAll = $('#ide-replace-all');
  if (repAll) repAll.onclick = () => ideReplaceAll();
  const pvBtn = $('#ide-preview-btn');
  if (pvBtn) pvBtn.onclick = () => idePreviewToggle();
  const pvOpen = $('#ide-preview-open');
  if (pvOpen) pvOpen.onclick = () => idePreviewOpen();
  const pvReload = $('#ide-preview-reload');
  if (pvReload) pvReload.onclick = () => idePreviewReload();
  const pvClose = $('#ide-preview-close');
  if (pvClose) pvClose.onclick = () => idePreviewToggle(false);
  const pvPick = $('#ide-preview-pick');
  if (pvPick) pvPick.onclick = () => idePreviewTogglePick();
  const pvConsole = $('#ide-preview-console-btn');
  if (pvConsole) pvConsole.onclick = () => idePreviewConsoleToggle();
  const pvClear = $('#ide-preview-console-clear');
  if (pvClear) pvClear.onclick = () => idePreviewClearLogs();
  const pvSend = $('#ide-preview-console-send');
  if (pvSend) pvSend.onclick = () => idePreviewSendLogs();
  const pvConClose = $('#ide-preview-console-close');
  if (pvConClose) pvConClose.onclick = () => idePreviewConsoleToggle(false);
  idePreviewBindBridge();
  const pvUrl = $('#ide-preview-url');
  if (pvUrl) pvUrl.onkeydown = (e) => {
    if (e.key === 'Enter') { e.preventDefault(); idePreviewOpen(); }
  };
  const mdBtn = $('#ide-md-btn');
  if (mdBtn) mdBtn.onclick = () => ideMdToggle();
  const mdClose = $('#ide-md-close');
  if (mdClose) mdClose.onclick = () => ideMdToggle(false);
  const mdPdf = $('#ide-md-pdf');
  if (mdPdf) mdPdf.onclick = () => ideMdExportPdf();
  const texBtn = $('#ide-run-btn');
  if (texBtn) texBtn.onclick = () => ideRunCurrent();
  loadMonaco().then((m) => {
    if (!m) return;                 // Monaco 加载失败则继续用内置 textarea，功能不受影响
    state.ideMonaco = m;
    ideRegisterLatexLanguage(m);     // 必须在建 model 之前：否则 tex 会先被当成 plaintext
    ideRegisterMonacoThemes(m);
    ideApplyEditorKeybindings();     // 用户改过的编辑器快捷键（没改过则一条都不下发）
    ideEnsureEditor(m);
    if (state.ideOpen) ideActivate(state.ideOpen);
    else ideShowView('editor');
    ideUpdateStatus();
  });
}

let monacoLoading = null;
// 本地内置的 Monaco（离线可用）。注入时机放在 window.onload 之后，
// 避免它的 AMD loader 抢先定义 define，导致 echarts / vis-network 等 UMD 库注册成 AMD 模块而不挂全局变量。
function loadMonaco() {
  if (monacoLoading) return monacoLoading;
  monacoLoading = new Promise((resolve) => {
    if (window.monaco) return resolve(window.monaco);
    const boot = () => {
      const s = document.createElement('script');
      s.src = '/static/vendor/monaco/vs/loader.js';
      s.onload = () => {
        if (!window.require) return resolve(null);
        const base = location.origin + '/static/vendor/monaco/';
        window.MonacoEnvironment = {
          getWorker() {
            const body = "self.MonacoEnvironment={baseUrl:'" + base + "'};" +
              "importScripts('" + base + "vs/base/worker/workerMain.js');";
            return new Worker(URL.createObjectURL(new Blob([body], { type: 'text/javascript' })));
          },
        };
        window.require.config({ paths: { vs: '/static/vendor/monaco/vs' } });
        window.require(['vs/editor/editor.main'], () => resolve(window.monaco), () => resolve(null));
      };
      s.onerror = () => resolve(null);
      document.head.appendChild(s);
    };
    if (document.readyState === 'complete') boot();
    else window.addEventListener('load', boot, { once: true });
  });
  return monacoLoading;
}

// 按扩展名推断 Monaco 语言
const IDE_LANGS = {
  py: 'python', js: 'javascript', mjs: 'javascript', cjs: 'javascript', jsx: 'javascript',
  ts: 'typescript', tsx: 'typescript', json: 'json', html: 'html', htm: 'html', vue: 'html',
  css: 'css', scss: 'scss', less: 'less', md: 'markdown', markdown: 'markdown',
  sh: 'shell', bash: 'shell', zsh: 'shell', ps1: 'powershell', bat: 'bat', cmd: 'bat',
  yml: 'yaml', yaml: 'yaml', toml: 'ini', ini: 'ini', cfg: 'ini', conf: 'ini',
  xml: 'xml', java: 'java',
  // C / C++ 共用 Monaco 的 cpp 语言（它的注册扩展名就包含 .c / .h）
  c: 'cpp', h: 'cpp', cc: 'cpp', cpp: 'cpp', cxx: 'cpp', hpp: 'cpp', hxx: 'cpp',
  cs: 'csharp', go: 'go', rs: 'rust', php: 'php', rb: 'ruby', sql: 'sql', lua: 'lua', txt: 'plaintext',
  // LaTeX：内置 Monaco 没打包 latex，语法在 ideRegisterLatexLanguage 里自己注册
  tex: 'latex', sty: 'latex', cls: 'latex', bib: 'latex',
};
function ideLangOf(rel) {
  const name = (rel || '').split('/').pop() || '';
  const ext = name.indexOf('.') >= 0 ? name.split('.').pop().toLowerCase() : '';
  return IDE_LANGS[ext] || 'plaintext';
}

/* LaTeX 语法高亮：内置 Monaco 没打包 latex，这里注册一份够用的（命令 / 环境 / 注释 / 数学）。
   和 VS Code 内置 latex 扩展同级——只负责着色与括号配对，编译另走 /api/ide/tex/build。 */
function ideRegisterLatexLanguage(M) {
  if (!M || !M.languages || M.__gmLatexRegistered) return;
  M.__gmLatexRegistered = true;
  M.languages.register({ id: 'latex', extensions: ['.tex', '.sty', '.cls', '.bib'] });
  M.languages.setLanguageConfiguration('latex', {
    comments: { lineComment: '%' },
    brackets: [['{', '}'], ['[', ']'], ['(', ')']],
    autoClosingPairs: [
      { open: '{', close: '}' }, { open: '[', close: ']' },
      { open: '(', close: ')' }, { open: '$', close: '$' },
    ],
    surroundingPairs: [{ open: '{', close: '}' }, { open: '[', close: ']' }],
  });
  M.languages.setMonarchTokensProvider('latex', {
    defaultToken: '',
    tokenPostfix: '.tex',
    tokenizer: {
      root: [
        [/%.*$/, 'comment'],
        [/\\(?:begin|end)\s*\{[^}]*\}/, 'keyword.control'],
        [/\\(?:documentclass|usepackage|input|include|bibliography|bibliographystyle|label|ref|eqref|cite|item|chapter|section|subsection|subsubsection|paragraph|textbf|textit|emph)\b/, 'keyword'],
        [/\\[a-zA-Z@]+\*?/, 'type'],
        [/\$\$/, { token: 'string', next: '@displaymath' }],
        [/\$/, { token: 'string', next: '@inlinemath' }],
        [/\\\[/, { token: 'string', next: '@bracketmath' }],
        [/\\\(/, { token: 'string', next: '@parenmath' }],
        [/[{}]/, '@brackets'],
        [/[\[\]()]/, '@brackets'],
        [/\d+(?:\.\d+)?/, 'number'],
        [/[&~^_]/, 'operator'],
      ],
      inlinemath: [
        [/[^$]+/, 'string'],
        [/\$/, { token: 'string', next: '@pop' }],
      ],
      displaymath: [
        [/[^$]+/, 'string'],
        [/\$\$/, { token: 'string', next: '@pop' }],
      ],
      bracketmath: [
        [/[^\]]+/, 'string'],
        [/\\\]/, { token: 'string', next: '@pop' }],
      ],
      parenmath: [
        [/[^)]+/, 'string'],
        [/\\\)/, { token: 'string', next: '@pop' }],
      ],
    },
  });
}

// 可在 IDE 里预览的图片类型（SVG 仍按文本编辑）
const IDE_IMAGE_EXTS = ['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'ico', 'avif'];
function ideIsImage(rel) {
  const name = (rel || '').split('/').pop() || '';
  const ext = name.indexOf('.') >= 0 ? name.split('.').pop().toLowerCase() : '';
  return IDE_IMAGE_EXTS.indexOf(ext) >= 0;
}
// PDF / Word / PPT 走沉浸式阅读器，不在 Monaco 里打开
function ideIsPdf(rel) {
  const name = (rel || '').split('/').pop() || '';
  const ext = name.indexOf('.') >= 0 ? name.split('.').pop().toLowerCase() : '';
  return ext === 'pdf' || ext === 'docx' || ext === 'pptx';
}

// 编辑区五态视图切换：Monaco / 纯文本兜底 / 图片预览 / diff / Git diff
function ideShowView(kind) {
  const ed = $('#ide-editor'); if (ed) ed.classList.toggle('hidden', kind !== 'editor');
  const fb = $('#ide-fallback'); if (fb) fb.classList.toggle('hidden', kind !== 'fallback');
  const im = $('#ide-image'); if (im) im.classList.toggle('hidden', kind !== 'image');
  const df = $('#ide-diff'); if (df) df.classList.toggle('hidden', kind !== 'diff');
  const gd = $('#ide-git-diff'); if (gd) gd.classList.toggle('hidden', kind !== 'gitdiff');
  const gr = $('#ide-git-review'); if (gr) gr.classList.toggle('hidden', kind !== 'gitreview');
  ideWelcomeVisible(kind === 'editor' && !state.ideOpen);
}

/* ---------- 欢迎页 / 最近打开 ---------- */
const IDE_RECENT_KEY = 'ide_recent_v1';
function ideRecentList() {
  try {
    const v = JSON.parse(localStorage.getItem(IDE_RECENT_KEY) || '[]');
    return Array.isArray(v) ? v : [];
  } catch (e) { return []; }
}
function ideWelcomeVisible(on) {
  const w = $('#ide-welcome');
  if (w) w.classList.toggle('hidden', !on);
}
function ideRenderWelcome() {
  const box = $('#ide-welcome-recent');
  if (!box) return;
  const list = ideRecentList();
  if (!list.length) { box.innerHTML = ''; return; }
  box.innerHTML = '<div class="ide-welcome-h">最近打开</div>' +
    list.map((p) => '<button type="button" class="ide-welcome-recent-item" data-path="' +
      escapeHtml(p) + '">' + escapeHtml(p) + '</button>').join('');
  $$('#ide-welcome-recent .ide-welcome-recent-item').forEach((el) => {
    el.onclick = () => openIdeFile(el.dataset.path);
  });
}
function idePushRecent(rel) {
  const list = ideRecentList().filter((x) => x !== rel);
  list.unshift(rel);
  try { localStorage.setItem(IDE_RECENT_KEY, JSON.stringify(list.slice(0, 8))); } catch (e) { /* 忽略 */ }
  ideRenderWelcome();
}

/* ---------------- 颜色装饰（CSS / HTML 里的色块 + 取色器） ----------------
   Monaco 的 defaultColorDecorators 走的是语言 worker 的正则扫描，只对「没有专属 worker」的
   语言生效；html / css / scss / less 都被各自的语言 worker 接管，而这份构建的 worker 没有
   实现 computeDefaultDocumentColors，所以这四个语言不显示色块（VS Code 由语言服务提供）。
   这里补一个正则版 provider：hex / rgb(a) / hsl(a) 都识别，点色块可打开 Monaco 取色器。 */
const IDE_COLOR_LANGS = ['html', 'css', 'scss', 'less'];
const IDE_COLOR_RE = /#[0-9a-fA-F]{3,8}\b|\brgba?\([^)]*\)|\bhsla?\([^)]*\)/g;

function ideHslToRgb(h, s, l) {
  if (!s) return { red: l, green: l, blue: l };
  const q = l < 0.5 ? l * (1 + s) : l + s - l * s;
  const p = 2 * l - q;
  const hue = (t) => {
    let x = t;
    if (x < 0) x += 1;
    if (x > 1) x -= 1;
    if (x < 1 / 6) return p + (q - p) * 6 * x;
    if (x < 1 / 2) return q;
    if (x < 2 / 3) return p + (q - p) * (2 / 3 - x) * 6;
    return p;
  };
  return { red: hue(h + 1 / 3), green: hue(h), blue: hue(h - 1 / 3) };
}

// 把 #rgb / #rgba / #rrggbb / #rrggbbaa / rgb(a)() / hsl(a)() 解析成 Monaco 的 Color 数值
//（red/green/blue/alpha 都是 0–1）。解析不了返回 null，交给调用方跳过。
function ideParseColorToken(raw) {
  const s = String(raw || '').trim();
  let m = /^#([0-9a-fA-F]{3,8})$/.exec(s);
  if (m) {
    let h = m[1];
    if (h.length === 3 || h.length === 4) h = h.split('').map((c) => c + c).join('');
    if (h.length === 6) h += 'ff';
    if (h.length !== 8) return null;
    return {
      red: parseInt(h.slice(0, 2), 16) / 255,
      green: parseInt(h.slice(2, 4), 16) / 255,
      blue: parseInt(h.slice(4, 6), 16) / 255,
      alpha: parseInt(h.slice(6, 8), 16) / 255,
    };
  }
  const parts = (m2) => m2[1].split(/[,\s/]+/).filter(Boolean);
  m = /^rgba?\(([^)]*)\)$/i.exec(s);
  if (m) {
    const p = parts(m);
    if (p.length < 3) return null;
    const ch = (v) => (v.indexOf('%') >= 0 ? parseFloat(v) / 100 : parseFloat(v) / 255);
    const a = p[3] === undefined ? 1
      : (p[3].indexOf('%') >= 0 ? parseFloat(p[3]) / 100 : parseFloat(p[3]));
    const out = { red: ch(p[0]), green: ch(p[1]), blue: ch(p[2]), alpha: a };
    return Object.values(out).some((v) => Number.isNaN(v)) ? null : out;
  }
  m = /^hsla?\(([^)]*)\)$/i.exec(s);
  if (m) {
    const p = parts(m);
    if (p.length < 3) return null;
    const h = (((parseFloat(p[0]) % 360) + 360) % 360) / 360;
    const sat = parseFloat(p[1]) / 100;
    const light = parseFloat(p[2]) / 100;
    const a = p[3] === undefined ? 1
      : (p[3].indexOf('%') >= 0 ? parseFloat(p[3]) / 100 : parseFloat(p[3]));
    if ([h, sat, light, a].some((v) => Number.isNaN(v))) return null;
    return Object.assign(ideHslToRgb(h, sat, light), { alpha: a });
  }
  return null;
}

function ideColorHex(c) {
  const h = (v) => Math.max(0, Math.min(255, Math.round((Number(v) || 0) * 255)))
    .toString(16).padStart(2, '0');
  const alpha = Number(c.alpha);
  return '#' + h(c.red) + h(c.green) + h(c.blue) + (alpha < 1 ? h(alpha) : '');
}

let _ideColorRegistered = false;

function ideRegisterColorProviders(monaco) {
  if (_ideColorRegistered || !monaco || !monaco.languages) return;
  _ideColorRegistered = true;
  const provider = {
    provideDocumentColors(model) {
      const text = model.getValue();
      const out = [];
      IDE_COLOR_RE.lastIndex = 0;          // 带 g 的正则：每次重扫前归零
      let m;
      while ((m = IDE_COLOR_RE.exec(text))) {
        const color = ideParseColorToken(m[0]);
        if (!color) continue;
        const a = model.getPositionAt(m.index);
        const b = model.getPositionAt(m.index + m[0].length);
        out.push({ color: color,
                   range: new monaco.Range(a.lineNumber, a.column, b.lineNumber, b.column) });
      }
      return out;
    },
    provideColorPresentations(model, info) {
      const hex = ideColorHex(info.color || {});
      return [{ label: hex, textEdit: { range: info.range, text: hex } }];
    },
  };
  IDE_COLOR_LANGS.forEach((lang) => {
    try { monaco.languages.registerColorProvider(lang, provider); } catch (e) { /* 忽略 */ }
  });
}

function ideEnsureEditor(monaco) {
  if (state.ideEditor) return state.ideEditor;
  ideRegisterColorProviders(monaco);
  const host = $('#ide-editor');
  if (!host) return null;
  const d = state.ideMonacoDefaults || {};
  /* 编辑器选项**不写死任何字体相关项**：留空即用 Monaco 内置默认，而 Monaco 的默认值
     就是 VS Code 的默认值（内置 Monaco 的 EDITOR_FONT_DEFAULTS：
       fontFamily Windows = "Consolas, 'Courier New', monospace" / macOS = Menlo…、
       fontSize = 14（macOS 12）、fontWeight = normal、lineHeight = 0（由编辑器按字号算）、
       letterSpacing = 0；
     另外 scrollBeyondLastLine 默认 true、smoothScrolling 默认 false、padding 默认 0）。
     以前这里写死了 fontSize 13 / tabSize 2 / padding.top 8 / smoothScrolling:true，
     都属于「自定义偏离」，已按 VS Code 官方默认移除。
     只有用户主动「从 VS Code 导入设置」时才用导入值覆盖（见 ideImportVsCode）。 */
  const opt = {
    value: '', language: 'plaintext',
    theme: ideMonacoThemeName(),
    automaticLayout: true,
    renderWhitespace: ideWhitespaceMode(),   // 由「设置 → 主题与外观 → 空白字符可视化」控制
    breadcrumbs: { enabled: true },          // 编辑器顶部的路径层级（VS Code 默认开启）
    // AI 补全用 Monaco 原生的灰色 ghost text；不要悬浮工具条（Tab 接受 / Esc 关闭）
    inlineSuggest: { enabled: true, showToolbar: 'never' },
  };
  if (d.fontSize) opt.fontSize = d.fontSize;
  if (d.tabSize) opt.tabSize = d.tabSize;
  if (d.fontFamily) opt.fontFamily = d.fontFamily;
  if (d.wordWrap) opt.wordWrap = d.wordWrap;
  if (typeof d.mouseWheelZoom === 'boolean') opt.mouseWheelZoom = d.mouseWheelZoom;
  state.ideEditor = monaco.editor.create(host, opt);
  state.ideEditor.onDidChangeModelContent(() => {
    if (state.ideOpen) ideSetDirty(state.ideOpen, true);
    // 开始编辑 = 这个标签被固定，之后打开别的文件不再顶掉它
    if (state.idePreviewTab && state.idePreviewTab === state.ideOpen) idePinTab(state.ideOpen);
    ideMdSchedule();                       // Markdown 预览跟着打字实时刷新（节流）
    ideMdLintSchedule();                   // Markdown 语法检查（波浪线 + 问题面板）
  });
  state.ideEditor.onDidChangeCursorPosition((e) => {
    const el = $('#ide-status-pos');
    if (el) el.textContent = '行 ' + e.position.lineNumber + '，列 ' + e.position.column;
  });
  ideAiInit(state.ideEditor, monaco);
  ideIntelBindKeys(state.ideEditor, monaco);
  debugBindEditor(state.ideEditor, monaco);
  debugApplyDecorations();
  return state.ideEditor;
}

/* ============================================================
   AI 内联补全（Tab 补全）
   ------------------------------------------------------------
   交互对齐 Trae / Copilot：光标停顿后出灰色 ghost text，Tab 接受、Esc 关闭。
   · 只在行尾 / 后面只剩收尾符号时请求，避免在标识符中间乱补；
   · 每次请求带取消：光标一动旧结果直接丢弃（Monaco 的 cancellation token）；
   · 没配 light_model 时不生效（避免拿主模型每 350ms 烧一次 token）。
   ============================================================ */
const IDE_AI_LANGS = ['javascript', 'typescript', 'python', 'html', 'css', 'scss',
  'less', 'json', 'shell', 'powershell', 'bat', 'yaml', 'java', 'c', 'cpp',
  'csharp', 'go', 'rust', 'php', 'ruby', 'sql', 'lua', 'vue'];
// 最近一次「已交给 Monaco 的建议」：用于判断 Tab 该不该被拦截。
// 内容 / 光标一变就失效，不会出现「按 Tab 却没东西可接受」的死键位。
// texts 是本次请求的全部候选，Alt+] / Alt+[ 在它们之间切换。
const IDE_AI = { key: '', texts: [], text: '', ver: -1, line: -1, col: -1 };

function ideAiReady() {
  const c = state.config || {};
  if (c.ide_ai_complete === false) return false;
  return !!String(c.light_model || '').trim();
}

function ideAiDelay() {
  const n = Number((state.config || {}).ide_ai_complete_delay_ms);
  return Number.isFinite(n) && n >= 0 ? n : 350;
}

function ideAiSleep(ms, token) {
  return new Promise((resolve) => {
    let done = false;
    const fin = () => { if (!done) { done = true; resolve(); } };
    const timer = setTimeout(fin, ms);
    if (token && token.onCancellationRequested) {
      token.onCancellationRequested(() => { clearTimeout(timer); fin(); });
    }
  });
}

async function ideAiFetch(rel, model, position, token) {
  const full = model.getValue();
  const offset = model.getOffsetAt(position);
  const prefix = full.slice(Math.max(0, offset - 6000), offset);
  const suffix = full.slice(offset, offset + 2000);
  const key = rel + '|' + model.getVersionId() + '|' + offset;
  if (key === IDE_AI.key) return IDE_AI.texts;
  const ctrl = new AbortController();
  if (token && token.onCancellationRequested) token.onCancellationRequested(() => ctrl.abort());
  let data = null;
  try {
    const res = await fetch('/api/ide/complete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Auth-Token': TOKEN },
      body: JSON.stringify({
        session_id: state.sessions.current, path: rel,
        language: model.getLanguageId(), prefix: prefix, suffix: suffix,
        candidates: 2,
      }),
      signal: ctrl.signal,
    });
    if (res.ok) data = await res.json();
  } catch (e) {
    return [];                       // 取消 / 断网 / 超时：静默，不打扰编辑
  }
  let list = [];
  if (data && data.ok) {
    if (Array.isArray(data.texts)) list = data.texts.map((x) => String(x || '')).filter(Boolean);
    else if (data.text) list = [String(data.text)];
  }
  IDE_AI.key = key;
  IDE_AI.texts = list;
  return list;
}

async function ideAiProvide(model, position, token) {
  if (!ideAiReady()) return { items: [] };
  // 只服务主编辑区当前模型：diff / git-diff 那些编辑器也会命中同一个 provider
  if (!state.ideEditor || state.ideEditor.getModel() !== model) return { items: [] };
  if (IDE_AI_LANGS.indexOf(model.getLanguageId()) < 0) return { items: [] };
  const line = model.getLineContent(position.lineNumber);
  const before = line.slice(0, position.column - 1);
  const after = line.slice(position.column - 1);
  // 行尾 / 后面只剩收尾符号：一定要补；行中只在「光标不在标识符中间」时补，
  // 否则改名或写长变量名时会被一堆无关建议打断。
  const tailOnly = /^[ \t]*[)\]};,]*[ \t]*$/.test(after);
  const inWord = /[\w$]$/.test(before) && /^[\w$]/.test(after);
  if (!tailOnly && inWord) return { items: [] };
  if (!before.trim() && !after.trim()) return { items: [] };          // 空行不补
  await ideAiSleep(ideAiDelay(), token);
  if (token && token.isCancellationRequested) return { items: [] };
  const verAtStart = model.getVersionId();
  const texts = await ideAiFetch(state.ideOpen || '', model, position, token);
  if (!texts.length || (token && token.isCancellationRequested)) return { items: [] };
  // 请求期间内容 / 光标可能已经变了：过期结果不交给 Monaco，也不写进 Tab 判定状态
  const cur = state.ideEditor.getPosition();
  if (model.getVersionId() !== verAtStart || !cur ||
      cur.lineNumber !== position.lineNumber || cur.column !== position.column) {
    return { items: [] };
  }
  IDE_AI.ver = model.getVersionId();
  IDE_AI.line = position.lineNumber;
  IDE_AI.col = position.column;
  IDE_AI.text = texts[0];
  return { items: texts.map((t) => ({ insertText: t })) };
}

function ideAiInit(editor, monaco) {
  const provider = {
    provideInlineCompletions: (model, position, ctx, token) =>
      ideAiProvide(model, position, token),
    freeInlineCompletions: () => {},
  };
  IDE_AI_LANGS.forEach((lang) => {
    try { monaco.languages.registerInlineCompletionsProvider(lang, provider); }
    catch (e) { /* 旧版本 Monaco 没有这个 API：静默停用 */ }
  });
  // Tab 接受：只在「建议确实存在且位置没变」时劫持，否则保持缩进
  editor.onKeyDown((e) => {
    if (e.keyCode === monaco.KeyCode.Escape) { IDE_AI.text = ''; IDE_AI.texts = []; return; }
    if (e.keyCode !== monaco.KeyCode.Tab) return;
    if (e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;
    const model = editor.getModel();
    const pos = editor.getPosition();
    if (!model || !pos || !IDE_AI.text) return;
    if (IDE_AI.ver !== model.getVersionId()) return;
    if (IDE_AI.line !== pos.lineNumber || IDE_AI.col !== pos.column) return;
    e.preventDefault();
    e.stopPropagation();
    IDE_AI.text = '';
    IDE_AI.texts = [];
    editor.trigger('ide-ai', 'editor.action.inlineSuggest.commit', null);
  });
  // Alt+] / Alt+[ 在多个候选之间切换（对齐 Trae / Copilot）
  try {
    editor.addAction({
      id: 'ide-ai-next',
      label: '下一个 AI 补全候选',
      keybindings: [monaco.KeyMod.Alt | monaco.KeyCode.BracketRight],
      run: (ed) => { ed.trigger('ide-ai', 'editor.action.inlineSuggest.showNext', null); },
    });
    editor.addAction({
      id: 'ide-ai-prev',
      label: '上一个 AI 补全候选',
      keybindings: [monaco.KeyMod.Alt | monaco.KeyCode.BracketLeft],
      run: (ed) => { ed.trigger('ide-ai', 'editor.action.inlineSuggest.showPrevious', null); },
    });
  } catch (e) { /* 忽略：旧版本 Monaco 没有多候选切换 */ }
  // Alt+\ 手动触发一次（自动模式下用来「催」一下）
  try {
    editor.addAction({
      id: 'ide-ai-trigger',
      label: '触发 AI 补全',
      keybindings: [monaco.KeyMod.Alt | 92],      // 92 = KeyCode.Backslash
      run: (ed) => { ed.trigger('ide-ai', 'editor.action.inlineSuggest.trigger', null); },
    });
  } catch (e) { /* 忽略：快捷键注册失败不影响自动补全 */ }
}

// 编辑器主题跟随 IDE 主题（与 Solo 界面主题解耦；配置为空时才回落到界面的明暗）
function ideSyncMonacoTheme() {
  const m = state.ideMonaco;
  if (m) m.editor.setTheme(ideMonacoThemeName());
}

/* ============================================================
   IDE 主题：Monaco 侧的色板（CSS 侧的界面配色见 web/css/ide.css）
   ------------------------------------------------------------
   · vscode-dark / vscode-light 直接复用 Monaco 内置的 vs-dark / vs —— 这两个
     内置主题本身就是 VS Code 的 Dark+ / Light+，重复定义反而容易跑偏。
   · Trae 三套用 defineTheme 注册。语法色与编辑器色全部取自 Trae 自带的
     theme-icube 扩展：dark_color.json / dark_vs.json / dark_plus.json /
     light_color.json / light_vs.json / light_plus.json / dark_blue.json。
   · **刻意不写 editorWhitespace.foreground**：只有留空才会用 Monaco 内置的
     VS Code 标准值（深色 #e3e4e229 / 浅色 #33333333），空格才会渲染成
     与原生一致的浅灰中点「·」、制表符渲染成「→」。
   ============================================================ */
const IDE_WS_MODES = ['none', 'selection', 'trailing', 'boundary', 'all'];
const IDE_MONACO_PREFIX = 'gal-ide-';        // 注册名加前缀，避免与内置主题重名

// Trae 三套共用的语法色（深色系：dark_vs + dark_plus）
const TRAE_DARK_RULES = [
  { token: 'comment', foreground: '737780' },
  { token: 'constant', foreground: '80BBFF' },
  { token: 'constant.numeric', foreground: 'F48CCA' },
  { token: 'number', foreground: 'F48CCA' },
  { token: 'string', foreground: '82D99F' },
  { token: 'regexp', foreground: '82D99F' },
  { token: 'keyword', foreground: 'B38CFF' },
  { token: 'keyword.operator', foreground: 'D5D8E0' },
  { token: 'storage', foreground: 'B38CFF' },
  { token: 'type', foreground: '81CFE0' },
  { token: 'namespace', foreground: '81CFE0' },
  { token: 'function', foreground: 'F29D79' },
  { token: 'variable', foreground: 'DED47E' },
  { token: 'variable.predefined', foreground: '80BBFF' },
  { token: 'key', foreground: 'E0E3EE' },
  { token: 'tag', foreground: 'F2858C' },
  { token: 'attribute.name', foreground: 'DED47E' },
  { token: 'attribute.value', foreground: '82D99F' },
  { token: 'delimiter', foreground: 'D5D8E0' },
  { token: 'operator', foreground: 'D5D8E0' },
  { token: 'identifier', foreground: 'D1D3DB' },
];

const IDE_MONACO_THEMES = {
  /* 与 Dark+ / Light+ 语法色相同，只是编辑器面层换成了「当前默认」的现代主题值
     （Default Dark Modern / Light Modern 的 tokenColors 就是 dark_plus / light_plus） */
  'vscode-modern-dark': {
    base: 'vs-dark',
    rules: [],
    colors: {
      'editor.background': '#1F1F1F',
      'editor.foreground': '#CCCCCC',
      'editorLineNumber.foreground': '#6E7681',
      'editorLineNumber.activeForeground': '#CCCCCC',
      'editorIndentGuide.background1': '#404040',
      'editorIndentGuide.activeBackground1': '#707070',
      'editorWidget.background': '#202020',
      'editorWidget.border': '#313131',
      'editorSuggestWidget.background': '#202020',
      'editorHoverWidget.background': '#202020',
      'scrollbarSlider.background': '#79797966',
      'scrollbarSlider.hoverBackground': '#BFBFBF66',
      'scrollbarSlider.activeBackground': '#BFBFBF66',
    },
  },
  'vscode-modern-light': {
    base: 'vs',
    rules: [],
    colors: {
      'editor.background': '#FFFFFF',
      'editor.foreground': '#3B3B3B',
      'editorLineNumber.foreground': '#6E7681',
      'editorLineNumber.activeForeground': '#171184',
      'editorIndentGuide.background1': '#D3D3D3',
      'editorIndentGuide.activeBackground1': '#939393',
      'editorWidget.background': '#F8F8F8',
      'editorWidget.border': '#E5E5E5',
      'editorSuggestWidget.background': '#F8F8F8',
      'editorHoverWidget.background': '#F8F8F8',
      'scrollbarSlider.background': '#64646466',
      'scrollbarSlider.hoverBackground': '#00000099',
      'scrollbarSlider.activeBackground': '#00000099',
    },
  },
  'trae-dark': {
    base: 'vs-dark',
    rules: TRAE_DARK_RULES,
    colors: {
      'editor.background': '#1A1B1D',
      'editor.foreground': '#D1D3DB',
      'editorCursor.foreground': '#FFFFFF',
      'editorLineNumber.foreground': '#979AA4',
      'editorLineNumber.activeForeground': '#E0E3EE',
      'editor.selectionBackground': '#3579FF47',
      'editor.lineHighlightBackground': '#292c34',
      'editorIndentGuide.background1': '#E0E2F217',
      'editorIndentGuide.activeBackground1': '#E0E2F22E',
      'editorWidget.background': '#2A2D31',
      'editorWidget.border': '#E0E2F221',
      'editorSuggestWidget.background': '#2A2D31',
      'editorSuggestWidget.selectedBackground': '#E0E2F214',
      'editorHoverWidget.background': '#2A2D31',
      'scrollbarSlider.background': '#DDE2E940',
      'scrollbarSlider.hoverBackground': '#DDE2E959',
      'scrollbarSlider.activeBackground': '#DDE2E959',
    },
  },
  'trae-light': {
    base: 'vs',
    rules: [
      { token: 'comment', foreground: '8393A3' },
      { token: 'constant', foreground: '175CE6' },
      { token: 'constant.numeric', foreground: 'E54595' },
      { token: 'number', foreground: 'E54595' },
      { token: 'string', foreground: '4DA621' },
      { token: 'regexp', foreground: '4DA621' },
      { token: 'keyword', foreground: '5F36B2' },
      { token: 'keyword.operator', foreground: '000000' },
      { token: 'storage', foreground: '5F36B2' },
      { token: 'type', foreground: 'B15EF2' },
      { token: 'namespace', foreground: 'B15EF2' },
      { token: 'function', foreground: '4078F2' },
      { token: 'variable', foreground: 'C99100' },
      { token: 'variable.predefined', foreground: '175CE6' },
      { token: 'key', foreground: '17181A' },
      { token: 'tag', foreground: 'E24864' },
      { token: 'attribute.name', foreground: '17181A' },
      { token: 'attribute.value', foreground: '4DA621' },
      { token: 'delimiter', foreground: '000000' },
      { token: 'operator', foreground: '000000' },
      { token: 'identifier', foreground: '31353A' },
    ],
    colors: {
      'editor.background': '#FFFFFF',
      'editor.foreground': '#000000',
      'editorCursor.foreground': '#000000',
      'editorLineNumber.foreground': '#72767B',
      'editorLineNumber.activeForeground': '#111214',
      'editor.selectionBackground': '#2F74FF2e',
      'editor.lineHighlightBackground': '#72767B14',
      'editorIndentGuide.background1': '#4D6B9917',
      'editorIndentGuide.activeBackground1': '#4D6B995c',
      'editorWidget.background': '#FFFFFF',
      'editorWidget.border': '#4D6B9917',
      'editorSuggestWidget.background': '#F8F9FA',
      'editorSuggestWidget.selectedBackground': '#5663771C',
      'editorHoverWidget.background': '#FFFFFF',
      'scrollbarSlider.background': '#DDE2E9',
      'scrollbarSlider.hoverBackground': '#C1C3CD',
      'scrollbarSlider.activeBackground': '#C1C3CD',
    },
  },
  'trae-blue': {
    base: 'vs-dark',
    rules: TRAE_DARK_RULES,
    colors: {
      'editor.background': '#171B26',
      'editor.foreground': '#DADDE5',
      'editorCursor.foreground': '#FFFFFF',
      'editorLineNumber.foreground': '#979AA4',
      'editorLineNumber.activeForeground': '#E0E3EE',
      'editor.selectionBackground': '#3579FF47',
      'editor.lineHighlightBackground': '#282B3A',
      'editorIndentGuide.background1': '#AABCF214',
      'editorIndentGuide.activeBackground1': '#AABCF23D',
      'editorWidget.background': '#252A38',
      'editorWidget.border': '#AABCF229',
      'editorSuggestWidget.background': '#252A38',
      'editorSuggestWidget.selectedBackground': '#AABCF233',
      'editorHoverWidget.background': '#252A38',
      'scrollbarSlider.background': '#DDE2E940',
      'scrollbarSlider.hoverBackground': '#DDE2E959',
      'scrollbarSlider.activeBackground': '#DDE2E959',
    },
  },
};

// 注册自定义主题（每次进入 IDE 只跑一次；重复注册同 id 也没问题）
function ideRegisterMonacoThemes(m) {
  Object.keys(IDE_MONACO_THEMES).forEach((k) => {
    const d = IDE_MONACO_THEMES[k];
    try {
      m.editor.defineTheme(IDE_MONACO_PREFIX + k, {
        base: d.base, inherit: true, rules: d.rules, colors: d.colors,
      });
    } catch (e) { /* 忽略：Monaco 版本差异不至于让编辑器整体不可用 */ }
  });
}

// 当前该用哪个 Monaco 主题
function ideMonacoThemeName() {
  const t = String((state.config && state.config.ide_theme) || '');
  if (state.uiMode === 'ide') {
    if (t === IDE_VSCODE_CURRENT) {          // 跟随：用解析出来的预设/临时主题
      const p = _vscodeCur.preset;
      if (p === 'vscode-dark') return 'vs-dark';
      if (p === 'vscode-light') return 'vs';
      if (p && IDE_MONACO_THEMES[p]) return IDE_MONACO_PREFIX + p;
      if (_vscodeCur.editor) return IDE_MONACO_PREFIX + IDE_VSCODE_CURRENT;
      return _vscodeCur.dark ? 'vs-dark' : 'vs';
    }
    if (t === 'vscode-dark') return 'vs-dark';
    if (t === 'vscode-light') return 'vs';
    if (IDE_MONACO_THEMES[t]) return IDE_MONACO_PREFIX + t;
  }
  return isDarkTheme() ? 'vs-dark' : 'vs';   // 空值 / 非 IDE 模式 = 跟随界面明暗
}

// 空白字符可视化档位（默认「全部」＝能看见每一个空格）
function ideWhitespaceMode() {
  const v = String((state.config && state.config.ide_whitespace) || 'all');
  return IDE_WS_MODES.indexOf(v) >= 0 ? v : 'all';
}
function ideApplyWhitespace() {
  if (state.ideEditor) state.ideEditor.updateOptions({ renderWhitespace: ideWhitespaceMode() });
}

/* ============================================================
   编辑器快捷键（VS Code 原生）的「改键」同步
   ------------------------------------------------------------
   设计要点：**默认零介入**。
   · 没有任何自定义时，本函数一条 keybinding 都不下发 → Monaco 用自己的默认键，
     行为就是 VS Code 原生（多光标 / 只读 / 查找框开着 等边界场景都由它自己管）。
   · 只有用户给某条 editor 规则改了键，才下发两条规则：
       ① { keybinding: 原键, command: null }  解绑 Monaco 的默认键
          （否则会变成"新旧两个键都能触发"，与"改键"的语义不符）
       ② { keybinding: 新键, command: mono, when: 'editorTextFocus' }
          把新键绑回同一个动作。
     两件事都交给 Monaco 自己的 keybinding 服务完成 —— 所以我们**不需要拦截键盘事件**，
     也就不会踩「焦点判断写错 → 键被吞掉 / 在聊天框里误触发」这类坑。
   · 「重置为默认」会重新调用本函数：先 dispose 上一次的动态绑定，再（可能）下发新的，
     于是回到"零介入"。 */
let _editorKb = null;

// 编辑器是否拥有键盘焦点（Monaco 的输入区是它自己的隐藏 textarea）
function ideEditorFocused() {
  const ed = state.ideEditor;
  return !!(ed && typeof ed.hasTextFocus === 'function' && ed.hasTextFocus());
}

// 'Alt+Shift+ArrowUp' → Monaco 的 keybinding 数字；名字解析不出来返回 null（**不猜**）
function ideMonacoKeybinding(spec) {
  const m = state.ideMonaco;
  const p = hkMonacoParts(spec);
  if (!m || !p) return null;
  let n = (m.KeyCode || {})[p.key];
  if (typeof n !== 'number') return null;
  p.mods.forEach((k) => {
    const v = (m.KeyMod || {})[k];
    if (typeof v === 'number') n |= v;
  });
  return n;
}

function ideApplyEditorKeybindings() {
  const m = state.ideMonaco;
  if (!m || !m.editor || typeof m.editor.addKeybindingRules !== 'function') return;
  if (_editorKb && typeof _editorKb.dispose === 'function') {
    try { _editorKb.dispose(); } catch (e) { /* 忽略 */ }     // 撤掉上一次的动态绑定
  }
  _editorKb = null;
  const ov = hkOverrides();
  const rules = [];
  HK_DEFS.forEach((d) => {
    // 没改过键 = 一个字都不动；改了（含解除绑定 = 空串）才需要介入 Monaco
    if (d.scope !== 'editor' || !d.mono ||
        !Object.prototype.hasOwnProperty.call(ov, d.id)) return;
    (d.keys || []).forEach((k) => {
      const n = ideMonacoKeybinding(k);
      if (n != null) rules.push({ keybinding: n, command: null });
    });
    if (ov[d.id]) {
      const n2 = ideMonacoKeybinding(ov[d.id]);
      if (n2 != null) rules.push({ keybinding: n2, command: d.mono, when: 'editorTextFocus' });
    }
  });
  if (!rules.length) return;
  try { _editorKb = m.editor.addKeybindingRules(rules); }
  catch (e) { console.warn('[IDE] 应用编辑器自定义快捷键失败', e); }
}

/* ============================================================
   「跟随 VS Code 当前主题」
   ------------------------------------------------------------
   · VS Code 是另一个进程，订阅不到它的切换事件 → 只能读它落盘的 settings.json
     并**周期性轮询**（本实现 5s）。所以是"近似实时"，不是事件驱动。
   · 命中官方内置主题（Dark+ / Light+ / Dark Modern / Light Modern）时后端直接回
     预设 id，前端套用已有整套 CSS 预设 + Monaco 主题 —— **零色值重复**。
   · 第三方主题才回 token 表，这里把它们写成 #main / #sidebar 上的内联 CSS 变量。
     映射是**有损的**（我们只用到约 24 个 token，主题里有几百个），缺项会继承界面主题，
     所以这是"近似跟随"，不是逐像素等同。
   · **语法高亮不跟随**：任意主题的 TextMate scope 到 Monaco token 的转换必然失真，
     编辑器语法色仍用与明暗匹配的内置 Dark+ / Light+。 */
// 与后端 app/vscode.py 的 _TOKEN_MAP 键保持一致（最后一项 --accent-soft 是本地派生）
const IDE_VSCODE_VARS = ['--bg', '--bg-side', '--bg-hover', '--bg-active', '--bg-input', '--text',
  '--text-dim', '--border', '--border-strong', '--accent', '--accent-hover', '--on-accent',
  '--bubble-user', '--bubble-user-text', '--code-bg', '--code-head', '--inline-bg', '--quote-bar',
  '--ide-activity-bg', '--ide-activity-fg', '--ide-activity-fg-dim', '--ide-tab-bg',
  '--ide-status-bg', '--ide-status-fg',
  '--ide-side-fg', '--ide-sel-fg', '--ide-tab-fg', '--ide-tab-fg-dim', '--accent-soft'];

// 尚未取回 VS Code 主题时保持空值（跟随界面），不要预置深色，否则会闪一下深色
let _vscodeCur = { preset: '', tokens: null, editor: null, dark: false, stamp: '', name: '' };
let _vscodeCurTimer = null;

function ideVscodeCurrent() {
  return state.uiMode === 'ide'
    && String((state.config && state.config.ide_theme) || '') === IDE_VSCODE_CURRENT;
}
// 供 settings.js 的 applyIdeTheme 读取（避免跨文件直接碰 _vscodeCur）
function ideVscodePreset() { return _vscodeCur.preset || ''; }

// 把 token 表写成内联变量（仅第三方主题走这里；内置预设走 CSS）
function ideApplyVscodeVars() {
  [$('#main'), $('#sidebar')].forEach((el) => {
    if (!el) return;
    IDE_VSCODE_VARS.forEach((k) => el.style.removeProperty(k));
    el.style.removeProperty('color-scheme');
    if (!ideVscodeCurrent() || _vscodeCur.preset) return;
    const tk = _vscodeCur.tokens || {};
    Object.keys(tk).forEach((k) => el.style.setProperty(k, tk[k]));
    // VS Code 没有 --accent-soft 这个 token，而我们的 CSS 大量用到 → 由 accent 派生
    if (!tk['--accent-soft'] && tk['--accent']) {
      const m = /^#([0-9a-fA-F]{6})$/.exec(String(tk['--accent']).trim());
      if (m) {
        const n = parseInt(m[1], 16);
        el.style.setProperty('--accent-soft', 'rgba(' + ((n >> 16) & 255) + ','
          + ((n >> 8) & 255) + ',' + (n & 255) + ',.18)');
      }
    }
    el.style.colorScheme = _vscodeCur.dark ? 'dark' : 'light';
  });
}

// 第三方主题下临时注册一个 Monaco 主题（内置预设有各自的主题，不用这个）
function ideRegisterVscodeCurrentMonaco() {
  const m = state.ideMonaco;
  if (!m || _vscodeCur.preset || !_vscodeCur.editor) return;
  try {
    m.editor.defineTheme(IDE_MONACO_PREFIX + IDE_VSCODE_CURRENT, {
      base: _vscodeCur.dark ? 'vs-dark' : 'vs',
      inherit: true, rules: [], colors: _vscodeCur.editor,
    });
  } catch (e) { /* 忽略 */ }
}

async function ideRefreshVscodeCurrent() {
  if (!ideVscodeCurrent()) return;
  let r = null;
  try { r = await api('/api/vscode/theme', {}); } catch (e) { return; }   // 失败则沿用上一次结果
  if (!r || !r.ok) return;
  _vscodeCur = {
    preset: String(r.preset || ''),
    tokens: r.tokens || {},
    editor: r.editor || {},
    dark: !!r.dark,
    stamp: String(r.stamp || ''),
    name: String(r.name || ''),
  };
  document.documentElement.setAttribute('data-ide-theme', _vscodeCur.preset || IDE_VSCODE_CURRENT);
  ideApplyVscodeVars();
  ideRegisterVscodeCurrentMonaco();
  ideSyncMonacoTheme();
}

function ideStartVscodeThemePoll() {
  if (_vscodeCurTimer) return;
  _vscodeCurTimer = setInterval(() => { ideRefreshVscodeCurrent(); }, 5000);
}
function ideStopVscodeThemePoll() {
  clearInterval(_vscodeCurTimer);
  _vscodeCurTimer = null;
}

function syncIdeRoot() {
  const s = curSession();
  const el = $('#ide-root');
  if (el) el.textContent = (s && s.sandbox) ? s.sandbox : '（未设置沙箱）';
}

// 切会话时重置工作区（沙箱可能不同），并在 IDE 模式下重新加载目录树
function resetIdeForSession() {
  Object.keys(state.ideModels || {}).forEach((k) => {
    try { state.ideModels[k].dispose(); } catch (e) { /* 忽略 */ }
  });
  state.ideModels = {};
  state.ideCache = {};
  state.ideDirtyMap = {};
  state.ideTabList = [];
  state.idePreviewTab = '';
  state.ideOpen = '';
  state.ideDir = '';
  state.ideChanged.clear();
  state.ideGitMarks = {};
  state.ideGitDirs = {};
  state.ideDiffs = {};
  if (state.ideDiffModels) {
    state.ideDiffModels.forEach((m) => { try { m.dispose(); } catch (e) { /* 忽略 */ } });
    state.ideDiffModels = null;
  }
  if (state.ideEditor) state.ideEditor.setModel(null);
  ideShowView('editor');
  const t = $('#ide-text'); if (t) t.value = '';
  renderIdeTabs();
  ideUpdateStatus();
  if (state.uiMode === 'ide') loadIdeTree();
  if (typeof ideTermResetForSession === 'function') ideTermResetForSession();
  if (state.uiMode === 'gal') {
    // 切会话：停掉上一会话在响的朗读 / BGM / 特效音，并释放它的立绘/背景 Blob URL
    galStopAudioAll({ bgm: true });
    galClearAssetCache();
    galClearMediaCache();
    galState.autoOverride = null;         // 「自动播放」的一次性覆盖不跨会话
    galState.charId = ''; galState.char = null;
    galState.statPrev = {};               // 状态参数的上一次值不跨会话，避免误闪「涨/跌」
    galClearSprites();                    // 舞台立绘（多角色）不跨会话：清空并移除残留 DOM
    galState.map = {};                    // 方位示意图也不跨会话
    galSetNode(null);
    galLoadCharacters().then(() => galRenderFromSession());
  }
  // 换会话：输入框上方的待审阅提示跟着切到新会话（有遗留改动就继续显示）；
  // 已卸载消息里的改动卡顺手清掉（下次写入 / 收尾会重建）。
  ideTipSid = state.sessions.current;
  if (typeof ideReviewTipRefresh === 'function') ideReviewTipRefresh();
  if (typeof ideReviewCardRefresh === 'function') ideReviewCardRefresh();
}

async function loadIdeTree() {
  const box = $('#ide-tree');
  if (!box) return;
  syncIdeRoot();
  const s = curSession();
  if (!s || !s.sandbox) {
    box.innerHTML = '<div class="ide-empty">尚未设置沙箱目录。<br>请先点输入框上方的「Agent」并选择目录。</div>';
    return;
  }
  let r;
  try { r = await api('/api/sandbox/tree', { session_id: state.sessions.current }); }
  catch (e) { box.innerHTML = '<div class="ide-empty">读取目录失败：' + escapeHtml(String(e)) + '</div>'; return; }
  if (!r || !r.ok) {
    box.innerHTML = '<div class="ide-empty">' + escapeHtml((r && r.error) || '读取失败') + '</div>';
    return;
  }
  state.ideTree = r.tree;
  renderIdeTree(r.tree);
  ideWatchSig = JSON.stringify(r.tree);      // 监听基线：下一次不一致才重绘
  ideRefreshGitMarks();                      // git 装饰与目录树并行拉取，不阻塞首屏
}

// 文件树图标：内联 SVG + 按扩展名着色（VS Code / Seti 风格近似，离线可用）
const IDE_SVG_FILE = '<svg viewBox="0 0 16 16" aria-hidden="true" focusable="false"><path fill="currentColor" d="M9.6 1.5H4.2A1.2 1.2 0 0 0 3 2.7v10.6a1.2 1.2 0 0 0 1.2 1.2h7.6a1.2 1.2 0 0 0 1.2-1.2V5.1L9.6 1.5Zm-.1 1.9 2.3 2.3H9.5V3.4Z"/></svg>';
const IDE_SVG_FOLDER = '<svg viewBox="0 0 16 16" aria-hidden="true" focusable="false"><path fill="currentColor" d="M1.6 3.2h4l1.3 1.5H14a1 1 0 0 1 1 1V12a1 1 0 0 1-1 1H2a1 1 0 0 1-1-1V4.2a1 1 0 0 1 1-1Z"/></svg>';

const IDE_ICON_TYPES = {
  py: 'py', pyw: 'py', js: 'js', mjs: 'js', cjs: 'js', jsx: 'jsx',
  ts: 'ts', tsx: 'ts', json: 'json', html: 'html', htm: 'html', vue: 'vue',
  css: 'css', scss: 'scss', sass: 'scss', less: 'scss',
  md: 'md', markdown: 'md', xml: 'xml', yml: 'yml', yaml: 'yml',
  toml: 'toml', ini: 'toml', cfg: 'toml', conf: 'toml',
  sh: 'sh', bash: 'sh', zsh: 'sh', ps1: 'sh', bat: 'sh', cmd: 'sh',
  c: 'c', h: 'c', cpp: 'cpp', cc: 'cpp', hpp: 'cpp', cs: 'cs',
  java: 'java', go: 'go', rs: 'rs', php: 'php', rb: 'rb', sql: 'sql', lua: 'lua',
  txt: 'txt', log: 'txt', lock: 'lock',
  png: 'img', jpg: 'img', jpeg: 'img', gif: 'img', webp: 'img', bmp: 'img', svg: 'img', ico: 'img',
};

// Seti 主题里 js / py / md / json 这些不在 fileExtensions，而是在 languageIds；
// 这里补一份「扩展名 → VS Code languageId」的常用映射（与 VS Code 的语言注册表一致）。
const IDE_LANG_BY_EXT = {
  js: 'javascript', mjs: 'javascript', cjs: 'javascript', jsx: 'javascriptreact',
  ts: 'typescript', mts: 'typescript', cts: 'typescript', tsx: 'typescriptreact',
  py: 'python', pyw: 'python', pyi: 'python',
  md: 'markdown', markdown: 'markdown', mdx: 'markdown',
  json: 'json', jsonc: 'json', jsonl: 'jsonl',
  html: 'html', htm: 'html', xhtml: 'html',
  css: 'css', scss: 'scss', sass: 'sass', less: 'less',
  vue: 'vue', yml: 'yaml', yaml: 'yaml',
  sh: 'shellscript', bash: 'shellscript', zsh: 'shellscript', ps1: 'powershell',
  bat: 'bat', cmd: 'bat',
  c: 'c', h: 'c', cpp: 'cpp', cc: 'cpp', cxx: 'cpp', hpp: 'cpp',
  cs: 'csharp', java: 'java', go: 'go', rs: 'rust', php: 'php', rb: 'ruby',
  sql: 'sql', lua: 'lua', r: 'r', swift: 'swift', kt: 'kotlin', dart: 'dart',
  tex: 'latex', latex: 'latex', xml: 'xml', gradle: 'gradle',
  tf: 'terraform', tfvars: 'terraform', ex: 'elixir', exs: 'elixir',
  hs: 'haskell', clj: 'clojure', coffee: 'coffeescript',
  gitignore: 'ignore', gitattributes: 'ignore', env: 'dotenv',
};
function ideIconClass(name) {
  const n = String(name || '').toLowerCase();
  if (n === '.gitignore' || n === '.gitattributes' || n.endsWith('.lock') || n === 'package-lock.json') {
    return n.endsWith('.lock') ? 'ico-lock' : 'ico-git';
  }
  const ext = n.indexOf('.') >= 0 ? n.split('.').pop() : '';
  const t = IDE_ICON_TYPES[ext];
  return t ? ('ico-' + t) : 'ico-file';
}

// VS Code Seti 图标字体：优先按文件名 / 最长扩展名匹配；找不到就用主题的默认文件图标。
// 返回 {ch, color}；字体映射还没加载 / 类型缺失时返回 null，调用方回退内联 SVG。
function ideSetiIcon(name, isDir) {
  const map = window.GM_SETI;
  if (!map || !map.defs) return null;
  const mode = document.documentElement.getAttribute('data-theme-mode') === 'light'
    ? 'light' : 'dark';
  const pack = (mode === 'light' && map.light) ? map.light : map.dark;
  if (!pack) return null;
  const n = String(name || '').toLowerCase();
  let id = '';
  if (isDir) {
    id = (pack.folderNames || {})[n] || '';
    if (!id) return null;          // Seti 主题本身不带文件夹图标：继续用黄色 SVG
  } else {
    id = (pack.fileNames || {})[n] || '';
    if (!id && n.indexOf('.') >= 0) {
      const parts = n.split('.');
      for (let i = 1; i < parts.length && !id; i++) {
        const suffix = parts.slice(i).join('.');
        id = (pack.fileExtensions || {})[suffix] || '';
      }
    }
    if (!id) {
      const ext = n.indexOf('.') >= 0 ? n.split('.').pop() : '';
      const lang = IDE_LANG_BY_EXT[ext];
      if (lang) id = (pack.languageIds || {})[lang] || '';
    }
    if (!id) id = pack.file || '';
  }
  const def = (map.defs || {})[id];
  if (!def || !def[0]) return null;
  return { ch: def[0], color: def[1] || '' };
}

// 构建一个可复用的图标元素
function ideIcoEl(name, isDir) {
  const ico = document.createElement('span');
  ico.className = 'ide-ico';
  const seti = ideSetiIcon(name, isDir);
  if (seti) {
    ico.classList.add('seti');
    ico.textContent = seti.ch;
    if (seti.color) ico.style.color = seti.color;
    return ico;
  }
  ico.classList.add(isDir ? 'ico-folder' : ideIconClass(name));
  ico.innerHTML = isDir ? IDE_SVG_FOLDER : IDE_SVG_FILE;
  return ico;
}

/* ---------------- 文件树：多选 / 拖拽移动 / 外部改动监听 ---------------- */

function ideToggleSel(rel) {
  if (!state.ideSel) state.ideSel = [];
  const i = state.ideSel.indexOf(rel);
  if (i >= 0) state.ideSel.splice(i, 1);
  else state.ideSel.push(rel);
  ideSyncSel();
}

function ideClearSel() {
  if (!(state.ideSel || []).length) return;
  state.ideSel = [];
  ideSyncSel();
}

function ideSyncSel() {
  const sel = state.ideSel || [];
  $$('#ide-tree .ide-row').forEach((r) => {
    r.classList.toggle('multi', sel.indexOf(r.dataset.path) >= 0);
  });
}

/* 文件 / 目录改名或移动后，把所有以旧路径为前缀的运行时状态一起换到新路径。
   包括标签、Monaco model（URI 也要重建，否则面包屑还指向旧文件）、缓存、
   未保存标记、多选、AI 改动标记与断点。 */
function ideRemapPrefix(oldP, newP) {
  if (!oldP || !newP || oldP === newP) return;
  const under = (p) => p === oldP || p.indexOf(oldP + '/') === 0;
  const mapped = (p) => newP + p.slice(oldP.length);
  state.ideTabList = (state.ideTabList || []).map((p) => (under(p) ? mapped(p) : p));
  state.ideSel = (state.ideSel || []).map((p) => (under(p) ? mapped(p) : p));
  Object.keys(state.ideModels || {}).forEach((p) => {
    if (!under(p)) return;
    const np = mapped(p);
    const model = state.ideModels[p];
    const dirty = !!state.ideDirtyMap[p];
    delete state.ideModels[p];
    delete state.ideDirtyMap[p];
    if (state.ideMonaco && model) {
      const text = model.getValue();
      try { if (state.ideEditor && state.ideEditor.getModel() === model) state.ideEditor.setModel(null); } catch (e) { /* 忽略 */ }
      try { model.dispose(); } catch (e) { /* 忽略 */ }
      const uri = state.ideMonaco.Uri.from({ scheme: 'sandbox', path: '/' + np });
      const nm = state.ideMonaco.editor.createModel(text, ideLangOf(np), uri);
      const ts = (state.ideMonacoDefaults || {}).tabSize;
      if (ts) nm.updateOptions({ tabSize: ts, insertSpaces: true });
      state.ideModels[np] = nm;
    } else if (model) {
      state.ideModels[np] = model;
    }
    state.ideDirtyMap[np] = dirty;
  });
  // 没有 Monaco model 的标签也要移植未保存标记
  Object.keys(state.ideDirtyMap || {}).forEach((p) => {
    if (!under(p)) return;
    state.ideDirtyMap[mapped(p)] = state.ideDirtyMap[p];
    delete state.ideDirtyMap[p];
  });
  Object.keys(state.ideCache || {}).forEach((p) => {
    if (!under(p)) return;
    state.ideCache[mapped(p)] = state.ideCache[p];
    delete state.ideCache[p];
  });
  if (state.ideChanged && state.ideChanged.size) {
    const next = new Set();
    state.ideChanged.forEach((p) => next.add(under(p) ? mapped(p) : p));
    state.ideChanged = next;
  }
  if (state.ideBreakpoints) {
    Object.keys(state.ideBreakpoints).forEach((p) => {
      if (!under(p)) return;
      state.ideBreakpoints[mapped(p)] = state.ideBreakpoints[p];
      delete state.ideBreakpoints[p];
    });
  }
  if (state.ideOpen && under(state.ideOpen)) state.ideOpen = mapped(state.ideOpen);
  if (state.ideDir && under(state.ideDir)) state.ideDir = mapped(state.ideDir);
  if (state.ideClip && state.ideClip.paths) {
    state.ideClip.paths = state.ideClip.paths.map((p) => (under(p) ? mapped(p) : p));
  }
}

/* 拖拽移动：后端只做「同一沙箱内的移动」，目标已存在会拒绝。
   打开着的标签 / 缓存 / 未保存标记都要跟着换路径，否则会指向不存在的文件。 */
async function ideMoveEntry(from, toDir) {
  const name = String(from || '').split('/').pop();
  if (!from) return;
  let r = null;
  try {
    r = await api('/api/sandbox/move', {
      session_id: state.sessions.current, from: from, to: toDir || '',
    });
  } catch (e) { return toast('移动失败：' + e); }
  if (!r || !r.ok) return toast((r && r.error) || '移动失败');
  if (r.unchanged) return;
  const dest = r.path || (toDir ? (toDir + '/' + name) : name);
  ideRemapPrefix(from, dest);
  await loadIdeTree();
  renderIdeTabs();
  if (state.ideOpen === dest && state.ideMonaco) ideActivate(dest);
  toast('已移动到 ' + (toDir || '根目录'));
}

/* ---------------- 文件树：右键菜单 / 剪贴板 ----------------
   对齐 VS Code：右键任意文件或目录都能重命名 / 复制 / 剪切 / 粘贴 / 删除，
   并支持 F2 / Delete / Ctrl+C / Ctrl+X / Ctrl+V。 */
function ideParentOf(rel) {
  return rel.indexOf('/') >= 0 ? rel.slice(0, rel.lastIndexOf('/')) : '';
}

/** 右键 / 快捷键作用的对象：右键点在已多选的行上就用整个多选，否则只用这一行。 */
function ideTargetsOf(row) {
  const sel = (state.ideSel || []).slice();
  if (row && row.dataset && row.dataset.path) {
    if (sel.indexOf(row.dataset.path) >= 0) return sel;
    return [row.dataset.path];
  }
  return sel.length ? sel : (state.ideOpen ? [state.ideOpen] : []);
}

/** 粘贴目标目录：右键目录 = 它本身；右键文件 = 它的父目录；空白 = 根目录。 */
function ideCtxDirOf(row) {
  if (!row || !row.dataset || !row.dataset.path) return '';
  return row.dataset.dir === '1' ? row.dataset.path : ideParentOf(row.dataset.path);
}

function ideClipSet(mode, paths) {
  const list = (paths || []).filter(Boolean);
  if (!list.length) return;
  state.ideClip = { mode: mode, paths: list };
  toast((mode === 'cut' ? '已剪切 ' : '已复制 ') + list.length + ' 项');
  ideCtxClose();
}

async function idePaste(destDir) {
  const clip = state.ideClip;
  if (!clip || !clip.paths.length) return toast('剪贴板是空的');
  const to = destDir == null ? '' : String(destDir);
  let done = 0;
  const failed = [];
  const moves = [];
  let renamed = 0;
  for (const from of clip.paths) {
    try {
      if (clip.mode === 'cut') {
        const r = await api('/api/sandbox/move', {
          session_id: state.sessions.current, from: from, to: to,
        });
        if (!r || !r.ok) { failed.push((r && r.error) || from); continue; }
        done += 1;
        if (!r.unchanged && r.path && r.path !== from) moves.push([from, r.path]);
      } else {
        const r = await api('/api/sandbox/copy', {
          session_id: state.sessions.current, from: from, to: to,
        });
        if (!r || !r.ok) { failed.push((r && r.error) || from); continue; }
        done += 1;
        if (r.renamed) renamed += 1;
      }
    } catch (e) { failed.push(String(e)); }
  }
  if (clip.mode === 'cut') {
    moves.forEach(([a, b]) => ideRemapPrefix(a, b));
    if (done) { state.ideClip = null; ideClearSel(); }   // 剪切成功才清空，失败还能重试
  }
  await loadIdeTree();
  ideSyncSel();
  renderIdeTabs();
  if (state.ideOpen && state.ideMonaco) ideActivate(state.ideOpen);
  if (!done) return toast(failed[0] || '粘贴失败');
  toast(failed.length
    ? ('已粘贴 ' + done + ' 项，' + failed.length + ' 项失败')
    : ('已粘贴 ' + done + ' 项' + (renamed ? '（重名已自动改名）' : '')));
}

async function ideDuplicate(paths) {
  const list = (paths || []).filter(Boolean);
  if (!list.length) return;
  let done = 0;
  const failed = [];
  for (const from of list) {
    try {
      const r = await api('/api/sandbox/copy', {
        session_id: state.sessions.current, from: from, to: ideParentOf(from),
      });
      if (r && r.ok) done += 1;
      else failed.push((r && r.error) || from);
    } catch (e) { failed.push(String(e)); }
  }
  await loadIdeTree();
  if (!done) return toast(failed[0] || '创建副本失败');
  toast(failed.length
    ? ('已创建 ' + done + ' 个副本，' + failed.length + ' 项失败')
    : ('已创建 ' + done + ' 个副本'));
}

function ideCopyPath(paths) {
  const text = (paths || []).join('\n');
  if (!text) return;
  const done = () => toast('已复制路径');
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done).catch(() => window.prompt('复制路径：', text));
  } else {
    window.prompt('复制路径：', text);
  }
}

async function ideRevealPath(rel) {
  try {
    const r = await api('/api/sandbox/reveal', { session_id: state.sessions.current, path: rel || '' });
    if (!r || !r.ok) toast((r && r.error) || '打开资源管理器失败');
  } catch (e) { toast('打开资源管理器失败：' + e); }
}

async function ideOpenPathInVsCode(rel) {
  try {
    const r = await api('/api/sandbox/open_vscode', { session_id: state.sessions.current, path: rel || '' });
    if (!r || !r.ok) toast((r && r.error) || '打开失败');
    else toast('已在 VS Code 中打开');
  } catch (e) { toast('打开失败：' + e); }
}

function ideCtxClose() {
  const el = document.getElementById('ide-ctxmenu');
  if (el) el.classList.add('hidden');
}

function ideCtxOpen(x, y, row) {
  const el = document.getElementById('ide-ctxmenu');
  if (!el) return;
  // 右键未选中的行 = 只选中这一行（和 VS Code 一致）；右键已选中的行保留多选
  if (row && row.dataset.path && (state.ideSel || []).indexOf(row.dataset.path) < 0) {
    state.ideSel = [row.dataset.path];
    ideSyncSel();
  }
  const paths = row ? ideTargetsOf(row) : [];
  const has = paths.length > 0;
  const hasClip = !!(state.ideClip && state.ideClip.paths.length);
  const dir = ideCtxDirOf(row);
  const items = [
    { label: '新建文件', run: () => ideNew(false, dir) },
    { label: '新建文件夹', run: () => ideNew(true, dir) },
    { sep: true },
    { label: '重命名', key: 'F2', disabled: !has, run: () => ideRenameTarget(paths[0]) },
    { label: '删除', key: 'Delete', disabled: !has, danger: true, run: () => ideDeleteTargets(paths) },
    { sep: true },
    { label: '复制', key: 'Ctrl+C', disabled: !has, run: () => ideClipSet('copy', paths) },
    { label: '剪切', key: 'Ctrl+X', disabled: !has, run: () => ideClipSet('cut', paths) },
    { label: '粘贴', key: 'Ctrl+V', disabled: !hasClip, run: () => idePaste(dir) },
    { label: '创建副本', disabled: !has, run: () => ideDuplicate(paths) },
    { sep: true },
    { label: '复制路径', disabled: !has, run: () => ideCopyPath(paths) },
    { label: '在资源管理器中显示', disabled: !has, run: () => ideRevealPath(paths[0]) },
    { label: '在 VS Code 中打开', disabled: !has, run: () => ideOpenPathInVsCode(paths[0]) },
    { sep: true },
    { label: '刷新', run: () => loadIdeTree() },
  ];
  ideMenuShow(el, x, y, items);
}

/* 通用右键菜单渲染：文件树与标签页共用同一个 #ide-ctxmenu 容器。 */
function ideMenuShow(el, x, y, items) {
  el.innerHTML = items.map((it, i) => {
    if (it.sep) return '<div class="ide-ctx-sep"></div>';
    return '<button type="button" class="ide-ctx-item' + (it.disabled ? ' disabled' : '')
      + (it.danger ? ' danger' : '') + '" data-i="' + i + '"'
      + (it.disabled ? ' disabled' : '') + '>'
      + '<span class="ide-ctx-label">' + escapeHtml(it.label) + '</span>'
      + (it.key ? '<span class="ide-ctx-key">' + escapeHtml(it.key) + '</span>' : '')
      + '</button>';
  }).join('');
  el.querySelectorAll('.ide-ctx-item').forEach((b) => {
    b.onclick = () => {
      const it = items[Number(b.dataset.i)];
      ideCtxClose();
      if (it && !it.disabled) it.run();
    };
  });
  el.classList.remove('hidden');
  // 先放左上角量尺寸，再按视口边缘翻转，避免菜单被截断
  el.style.left = '0px'; el.style.top = '0px';
  const w = el.offsetWidth || 200;
  const h = el.offsetHeight || 260;
  el.style.left = Math.max(4, Math.min(x, window.innerWidth - w - 4)) + 'px';
  el.style.top = Math.max(4, Math.min(y, window.innerHeight - h - 4)) + 'px';
  el.focus();
}

function ideCtxBind() {
  const box = $('#ide-tree');
  if (!box) return;
  if (!document.getElementById('ide-ctxmenu')) {
    const el = document.createElement('div');
    el.id = 'ide-ctxmenu';
    el.className = 'ide-ctxmenu hidden';
    el.setAttribute('role', 'menu');
    el.tabIndex = -1;
    el.addEventListener('mousedown', (e) => e.stopPropagation());
    el.addEventListener('contextmenu', (e) => e.preventDefault());
    document.body.appendChild(el);
  }
  if (box.dataset.ctxBound === '1') return;
  box.dataset.ctxBound = '1';
  box.addEventListener('contextmenu', (e) => {
    e.preventDefault();
    const row = e.target && e.target.closest ? e.target.closest('.ide-row') : null;
    ideCtxOpen(e.clientX, e.clientY, row);
  });
  // 点别处 / 滚动 / 窗口失焦都收起；Esc 见 ideTreeKeydown
  document.addEventListener('mousedown', (e) => {
    const el = document.getElementById('ide-ctxmenu');
    if (el && !el.classList.contains('hidden') && !el.contains(e.target)) el.classList.add('hidden');
  }, true);
  box.addEventListener('scroll', ideCtxClose);
  window.addEventListener('blur', ideCtxClose);
}

/* 外部改动监听：每 5 秒比一次目录树；当前打开的文件没改过且内容变了就重新载入。
   Electron 里没有文件系统 watch，轮询是最省事又不会漏的做法（开销：一次 tree + 一次 file）。 */
const IDE_WATCH_MS = 5000;
let ideWatchTimer = 0;
let ideWatchSig = '';

function ideWatchStart() {
  ideWatchStop();
  ideWatchTimer = setInterval(ideWatchTick, IDE_WATCH_MS);
}

function ideWatchStop() {
  if (ideWatchTimer) { clearInterval(ideWatchTimer); ideWatchTimer = 0; }
}

async function ideWatchTick() {
  if (state.uiMode !== 'ide' || !state.sessions.current) return;
  let r = null;
  try { r = await api('/api/sandbox/tree', { session_id: state.sessions.current }); }
  catch (e) { return; }
  if (!r || !r.ok) return;
  const sig = JSON.stringify(r.tree);
  if (ideWatchSig && sig !== ideWatchSig) {
    state.ideTree = r.tree;
    renderIdeTree(r.tree);
    ideSyncTreeActive();
  }
  ideWatchSig = sig;
  const rel = state.ideOpen;
  if (!rel || state.ideDirtyMap[rel] || !state.ideMonaco) return;
  const model = state.ideModels[rel];
  if (!model) return;
  let f = null;
  try { f = await api('/api/sandbox/file', { session_id: state.sessions.current, path: rel }); }
  catch (e) { return; }
  if (!f || !f.ok) return;
  const text = f.text || '';
  if (text === model.getValue()) return;
  const pos = state.ideEditor && state.ideEditor.getPosition();
  model.setValue(text);
  if (state.ideCache[rel]) state.ideCache[rel].text = text;
  if (pos && state.ideEditor) { try { state.ideEditor.setPosition(pos); } catch (e) { /* 忽略 */ } }
  toast('「' + rel + '」已在外部修改，已重新载入');
  if (ideLspEligible(rel)) ideIntelSchedule(rel);
}

/* ---------------- 文件树：拖出真实文件 ----------------
   Electron 壳里用系统级拖拽（webContents.startDrag），拖到桌面 / 资源管理器 / 其他应用
   拿到的是磁盘上的文件本体；纯浏览器受安全限制，只能给路径文本（保持原行为）。 */

/** 能不能拖出真实文件：需要 Electron 壳的 startDrag + 已设置的沙箱目录
    （树上只有相对路径，绝对路径由 ideAbsPath 拼出来）。 */
function ideCanDragOut() {
  if (!window.gmWin || typeof window.gmWin.startDrag !== 'function') return false;
  const s = (typeof curSession === 'function') ? curSession() : null;
  return !!(s && s.sandbox);
}

/* drop 事件里还原「被拖进目录的那一项」的沙箱相对路径。
   Electron 里拖拽已换成系统级真实文件：dragstart 被 preventDefault 取消，dataTransfer
   里不再有 text/plain，只剩 Files —— 用 webUtils（preload 的 pathForFile）取绝对路径，
   再按沙箱根目录换算回相对路径；落在沙箱外说明是从系统拖进来的，不处理。 */
function ideDropSource(e) {
  const dt = e && e.dataTransfer;
  if (!dt) return '';
  if (dt.files && dt.files.length
      && window.gmWin && typeof window.gmWin.pathForFile === 'function') {
    const abs = String(window.gmWin.pathForFile(dt.files[0]) || '');
    const s = (typeof curSession === 'function') ? curSession() : null;
    const root = String((s && s.sandbox) || '');
    if (abs && root) {
      const n = (p) => String(p).replace(/\\/g, '/').replace(/\/+$/, '');
      const a = n(abs), r = n(root);
      if (a !== r && a.indexOf(r + '/') === 0) return a.slice(r.length + 1);
      return '';
    }
  }
  try { return dt.getData('text/plain') || ''; } catch (err) { return ''; }
}

/** 拖拽源是否指向某个目录行（原生拖拽没有 text/plain，只能靠 state.ideDrag / Files）。 */
function ideDragHasSource(e) {
  if (state.ideDrag) return true;
  return !!(e && e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length);
}

/* 原生拖拽不会再有 dragend（dragstart 已被 preventDefault 取消），
   用鼠标移动 / 窗口重新聚焦 / 任意 drop 兜底清掉拖拽源：
   残留会让「从资源管理器拖文件到输入框」被误判成内部 # 引用。 */
let ideDragBound = false;
function ideDragBindGlobal() {
  if (ideDragBound) return;
  ideDragBound = true;
  const clear = () => {
    if (!state.ideDrag) return;
    state.ideDrag = '';
    $$('#ide-tree .ide-row.dragging').forEach((r) => r.classList.remove('dragging'));
  };
  window.addEventListener('mousemove', clear);
  window.addEventListener('focus', clear);
  document.addEventListener('drop', clear);
}

/* ---------------- 文件树的 Git 装饰（VS Code explorer 风格） ----------------
   文件名按 git 状态着色，行尾给 M / U / A / D / R 字母，文件夹行给一个小圆点。
   数据来自 /api/git/status（路径已经换算成「沙箱相对」，与树里的 rel 一致）。 */

const IDE_GIT_CH = { m: 'M', a: 'A', d: 'D', r: 'R', u: 'U', c: 'U' };
const IDE_GIT_TIP = {
  m: '已修改（未暂存/未提交）', a: '新增', d: '已删除', r: '重命名',
  u: '未跟踪', c: '冲突',
};

function ideGitMarkOf(f) {
  if (f && f.untracked) return { ch: 'U', cls: 'u' };
  const s = String((f && f.status) || 'M');
  if (s === 'A') return { ch: 'A', cls: 'a' };
  if (s === 'D') return { ch: 'D', cls: 'd' };
  if (s === 'R' || s === 'C') return { ch: s, cls: 'r' };
  if (s === 'U' || s === 'T') return { ch: 'U', cls: 'c' };
  return { ch: 'M', cls: 'm' };
}

/* 目录圆点的颜色取「子文件里最重的那一种状态」：冲突 > 删除 > 修改 > 新增 > 未跟踪 > 重命名。
   VS Code 也是用这个思路给文件夹装饰上色的。 */
const IDE_GIT_PRIORITY = ['c', 'd', 'm', 'a', 'u', 'r'];

function ideGitDirsOf(marks) {
  const dirs = {};
  Object.keys(marks || {}).forEach((p) => {
    const cls = marks[p] && marks[p].cls;
    if (!cls) return;
    const seg = String(p || '').split('/');
    for (let i = 1; i < seg.length; i++) {
      const d = seg.slice(0, i).join('/');
      const cur = dirs[d];
      if (!cur || IDE_GIT_PRIORITY.indexOf(cls) < IDE_GIT_PRIORITY.indexOf(cur)) dirs[d] = cls;
    }
  });
  return dirs;
}

/* 给单行套上装饰。新建行与增量刷新共用，先清掉旧的再按当前数据重建。 */
function ideApplyGitMark(row, rel, isDir) {
  if (!row) return;
  row.classList.remove('git-m', 'git-u', 'git-a', 'git-d', 'git-r', 'git-c');
  const old = row.querySelector('.ide-git-mark, .ide-git-dot');
  if (old) old.remove();
  if (isDir) {
    const cls = (state.ideGitDirs || {})[rel];
    if (!cls) return;
    const dot = document.createElement('span');
    dot.className = 'ide-git-dot ' + cls;
    dot.title = '目录内有改动' + (IDE_GIT_TIP[cls] ? '：' + IDE_GIT_TIP[cls] : '');
    row.appendChild(dot);
    return;
  }
  const mk = (state.ideGitMarks || {})[rel];
  if (!mk) return;
  row.classList.add('git-' + mk.cls);
  const b = document.createElement('span');
  b.className = 'ide-git-mark ' + mk.cls;
  b.textContent = mk.ch || IDE_GIT_CH[mk.cls] || 'M';
  b.title = IDE_GIT_TIP[mk.cls] || '';
  row.appendChild(b);
}

function ideApplyGitMarks() {
  $$('#ide-tree .ide-row').forEach((row) => {
    ideApplyGitMark(row, row.dataset.path || '', row.dataset.dir === '1');
  });
}

let _ideGitSeq = 0;
let _ideGitAt = 0;

/* 拉一次 git 状态。失败（非 git 仓库 / 未设置沙箱）就当没有装饰，不影响文件树。 */
async function ideRefreshGitMarks() {
  if (state.uiMode !== 'ide' || !state.sessions.current) return;
  const seq = ++_ideGitSeq;
  _ideGitAt = Date.now();
  let r;
  try { r = await api('/api/git/status', { session_id: state.sessions.current }); }
  catch (e) { return; }
  if (seq !== _ideGitSeq) return;             // 并发刷新：只认最后一次
  const marks = {};
  ((r && r.files) || []).forEach((f) => {
    const p = String((f && f.path) || '');
    if (p) marks[p] = ideGitMarkOf(f);
  });
  state.ideGitMarks = marks;
  state.ideGitDirs = ideGitDirsOf(marks);
  ideApplyGitMarks();
}

/* 窗口重新获得焦点时补拉一次（外部改了文件 / 切回窗口时状态可能已变）。
   1.5 秒内只拉一次，避免频繁切窗口打爆接口。 */
function ideWatchGitFocus() {
  if (ideWatchGitFocus.bound) return;
  ideWatchGitFocus.bound = true;
  window.addEventListener('focus', () => {
    if (state.uiMode !== 'ide') return;
    if (Date.now() - _ideGitAt < 1500) return;
    ideRefreshGitMarks();
  });
}

function buildIdeNodes(items, prefix, depth) {
  const d = depth || 0;
  const wrap = document.createElement('div');
  (items || []).forEach((it) => {
    const rel = prefix ? (prefix + '/' + it.name) : it.name;
    const node = document.createElement('div');
    node.className = 'ide-node';
    const row = document.createElement('div');
    row.className = 'ide-row';
    row.dataset.path = rel;
    row.dataset.dir = it.dir ? '1' : '0';
    row.tabIndex = -1;                       // 键盘导航用 roving tabindex（见 ideTreeKeydown）
    row.style.setProperty('--ide-depth', String(d));   // 缩进量交给 CSS，背景仍满宽
    if (state.ideOpen === rel) row.classList.add('active');
    if (state.ideChanged.has(rel)) row.classList.add('changed');
    const care = document.createElement('span');
    care.className = 'ide-caret';
    if (it.dir) {
      care.innerHTML = icon('chevron');
      row.setAttribute('aria-expanded', 'false');   // 箭头旋转由 CSS 按这个属性控制
    }
    const nm = document.createElement('span');
    nm.className = 'ide-name';
    nm.textContent = it.name;
    row.appendChild(care); row.appendChild(ideIcoEl(it.name, it.dir)); row.appendChild(nm);
    ideApplyGitMark(row, rel, !!it.dir);     // 先建行再补 git 装饰：刷新时只改这两个节点
    node.appendChild(row);
    if (it.dir) {
      const kids = document.createElement('div');
      kids.className = 'ide-children hidden';
      kids.appendChild(buildIdeNodes(it.children || [], rel, d + 1));
      node.appendChild(kids);
      // 拖拽：拖到目录行 = 移进这个目录；拖到目录行本身不展开，避免误触
      row.addEventListener('dragover', (e) => {
        if (!ideDragHasSource(e) || state.ideDrag === rel) return;
        e.preventDefault();
        row.classList.add('drop-target');
      });
      row.addEventListener('dragleave', () => row.classList.remove('drop-target'));
      row.addEventListener('drop', (e) => {
        e.preventDefault();
        row.classList.remove('drop-target');
        const from = ideDropSource(e) || state.ideDrag;
        if (from && from !== rel) ideMoveEntry(from, rel);
      });
      row.onclick = (e) => {
        if (e.ctrlKey || e.metaKey) { ideToggleSel(rel); return; }
        ideClearSel();
        ideToggleDir(row, kids);
      };
    } else {
      row.onclick = (e) => {
        if (e.ctrlKey || e.metaKey) { ideToggleSel(rel); return; }
        ideClearSel();
        // 单击 = 预览标签（再点别的文件会被替换），双击 = 固定
        openIdeFile(rel, { preview: true });
      };
      row.ondblclick = () => openIdeFile(rel, { pin: true });
    }
    // 文件与目录都能被拖走；拖动源由 state.ideDrag 记录（dataTransfer 在部分环境为空）
    row.draggable = true;
    row.addEventListener('dragstart', (e) => {
      state.ideDrag = rel;
      // Electron 壳：直接发起系统级拖拽，拖到桌面 / 资源管理器拿到的是真实文件本体。
      // startDrag 必须在 dragstart 里同步发起，且要 preventDefault：否则 Chromium 会
      // 再起一个 HTML5 拖拽，两个拖拽会话互相打架。
      if (ideCanDragOut()) {
        try { e.preventDefault(); } catch (err) { /* 忽略 */ }
        try { window.gmWin.startDrag({ file: ideAbsPath(rel) }); } catch (err) { /* 主进程记日志 */ }
        return;
      }
      try {
        e.dataTransfer.setData('text/plain', rel);
        e.dataTransfer.effectAllowed = 'move';
      } catch (err) { /* 忽略 */ }
      row.classList.add('dragging');
    });
    row.addEventListener('dragend', () => {
      state.ideDrag = '';
      row.classList.remove('dragging');
      $$('#ide-tree .ide-row.drop-target').forEach((r) => r.classList.remove('drop-target'));
    });
    wrap.appendChild(node);
  });
  return wrap;
}

// 展开 / 收起一个目录（点击与键盘共用）；箭头方向由 CSS 按 aria-expanded 旋转
function ideToggleDir(row, kids) {
  if (!row || !kids) return;
  state.ideDir = row.dataset.path || '';
  const nowHidden = kids.classList.toggle('hidden');
  row.setAttribute('aria-expanded', nowHidden ? 'false' : 'true');
}

function renderIdeTree(tree) {
  const box = $('#ide-tree');
  if (!box) return;
  box.innerHTML = '';
  const kids = (tree && tree.children) || [];
  if (!kids.length) { box.innerHTML = '<div class="ide-empty">（沙箱为空）</div>'; return; }
  box.appendChild(buildIdeNodes(kids, ''));
  ideResetRoving();
  if (state.ideOpen) ideRevealInTree(state.ideOpen);
}

/* ---------- 文件树键盘导航（VS Code 的手感核心：手不离键盘） ----------
   可见行 = 没有被收起目录藏住的行；roving tabindex 只让其中一行进入 Tab 序。 */
function ideVisibleRows() {
  return $$('#ide-tree .ide-row').filter((el) => el.offsetParent !== null);
}

function ideResetRoving() {
  const rows = ideVisibleRows();
  rows.forEach((el) => { el.tabIndex = -1; });
  const target = rows.find((r) => r.classList.contains('active')) || rows[0];
  if (target) target.tabIndex = 0;
}

function ideRowFocus(row) {
  if (!row) return;
  ideVisibleRows().forEach((el) => { el.tabIndex = -1; });
  row.tabIndex = 0;
  row.focus();
  row.scrollIntoView({ block: 'nearest' });
}

function ideTreeKeydown(e) {
  const rows = ideVisibleRows();
  if (!rows.length) return;
  const curEl = document.activeElement && document.activeElement.closest
    ? document.activeElement.closest('.ide-row') : null;
  let i = rows.indexOf(curEl);
  if (i < 0) i = rows.findIndex((r) => r.classList.contains('active'));
  if (i < 0) i = 0;
  const row = rows[i];
  if (!row) return;
  const isDir = row.dataset.dir === '1';
  const kids = isDir ? row.parentElement.querySelector(':scope > .ide-children') : null;
  if (e.key === 'Escape') { ideCtxClose(); return; }
  if (e.ctrlKey || e.metaKey) {
    const k = (e.key || '').toLowerCase();
    if (k === 'c') { e.preventDefault(); ideClipSet('copy', ideTargetsOf(row)); return; }
    if (k === 'x') { e.preventDefault(); ideClipSet('cut', ideTargetsOf(row)); return; }
    if (k === 'v') { e.preventDefault(); idePaste(ideCtxDirOf(row)); return; }
    if (k === 'a') {
      e.preventDefault();
      state.ideSel = rows.map((r) => r.dataset.path).filter(Boolean);
      ideSyncSel();
      return;
    }
  }
  switch (e.key) {
    case 'ArrowDown':
      e.preventDefault(); ideRowFocus(rows[Math.min(rows.length - 1, i + 1)]); break;
    case 'ArrowUp':
      e.preventDefault(); ideRowFocus(rows[Math.max(0, i - 1)]); break;
    case 'ArrowRight':
      e.preventDefault();
      if (!isDir) break;
      if (kids && kids.classList.contains('hidden')) ideToggleDir(row, kids);
      else {
        const next = rows[i + 1];
        if (next && next.dataset.path.indexOf(row.dataset.path + '/') === 0) ideRowFocus(next);
      }
      break;
    case 'ArrowLeft':
      e.preventDefault();
      if (isDir && kids && !kids.classList.contains('hidden')) ideToggleDir(row, kids);
      else {
        const parentKids = row.closest('.ide-children');
        const prow = parentKids && parentKids.parentElement
          ? parentKids.parentElement.querySelector(':scope > .ide-row') : null;
        if (prow) ideRowFocus(prow);
      }
      break;
    case 'Enter':
      e.preventDefault(); row.click(); break;
    case 'F2':
      e.preventDefault();
      if (row.dataset.path) ideRenameTarget(row.dataset.path);
      break;
    case 'Delete': {
      e.preventDefault();
      const sel = (state.ideSel || []).slice();
      const list = sel.indexOf(row.dataset.path) >= 0 ? sel : [row.dataset.path];
      ideDeleteTargets(list);
      break;
    }
    case 'Home':
      e.preventDefault(); ideRowFocus(rows[0]); break;
    case 'End':
      e.preventDefault(); ideRowFocus(rows[rows.length - 1]); break;
    default: break;
  }
}

/* 在树里定位当前文件：逐级展开父目录并滚动到可见。
   没有这一步，AI 改完文件后用户常常"知道它改了，但找不到在哪"。 */
function ideRevealInTree(rel) {
  const box = $('#ide-tree');
  if (!box || !rel) return;
  const row = Array.from(box.querySelectorAll('.ide-row')).find((el) => el.dataset.path === rel);
  if (!row) return;
  let n = row.parentElement;
  while (n && n !== box) {
    if (n.classList && n.classList.contains('ide-children') && n.classList.contains('hidden')) {
      n.classList.remove('hidden');
      const prow = n.previousElementSibling;
      if (prow && prow.classList.contains('ide-row')) {
        prow.setAttribute('aria-expanded', 'true');
        const care = prow.querySelector('.ide-caret');
        if (care) care.textContent = '▾';
      }
    }
    n = n.parentElement;
  }
  row.scrollIntoView({ block: 'nearest' });
}

async function setUiMode(mode) {
  const target = (mode === 'ide' || mode === 'gal' || mode === 'hall' || mode === 'work'
    || mode === 'plugins') ? mode : 'solo';
  // 离开剧场：切回进场前的主会话（剧场会话不会出现在 Solo / IDE 列表里）
  if (state.uiMode === 'gal' && target !== 'gal') {
    const back = state.galReturnSession
      || (((state.sessions.sessions || []).find((s) => (s.gal_scope || 'main') !== 'gal')) || {}).id;
    state.galReturnSession = '';
    if (back && (state.sessions.sessions || []).some((s) => s.id === back)
        && state.sessions.current !== back) {
      try { await selectSession(back); } catch (e) { /* 切回失败时保持现状 */ }
    }
  }
  // 进入剧场前记住主会话；剧场入口自己会先选中 gal 会话
  if (target === 'gal' && state.uiMode !== 'gal') {
    const cur = (state.sessions.sessions || []).find((s) => s.id === state.sessions.current);
    if (cur && (cur.gal_scope || 'main') !== 'gal') state.galReturnSession = cur.id;
  }
  applyUiMode(target);
  const cfg = Object.assign({}, state.config || {}, { ui_mode: state.uiMode });
  api('/api/config', cfg).then(() => { state.config = cfg; }).catch(() => {});
}

/* ---------- 标签页 / 状态栏 / 侧边栏切换 ---------- */
/* 某文件的待审变更统计：直接从审阅队列聚合，
   处理掉就自动变空 —— 不需要另存一张表，也不会残留旧数字。 */
function ideChangeStatOf(rel) {
  let adds = 0, dels = 0, n = 0;
  const seen = [];
  (state.ideReview || []).concat(state.ideReviewPending || []).forEach((it) => {
    if (!it || it.path !== rel || seen.indexOf(it) >= 0) return;   // 流式期间同一条会同时在两个队列里
    seen.push(it);
    adds += Number(it.adds) || 0;
    dels += Number(it.dels) || 0;
    n += 1;
  });
  return n ? { adds: adds, dels: dels } : null;
}

/* ---------------- 顶部标签页（对齐 VS Code 的行为） ----------------
   · 预览标签：资源树单击 = 斜体预览，再单击别的文件时原地替换；双击 / 开始编辑 /
     点击标签本身即固定。VS Code 的 tabs 默认就是这个模型。
   · 同名文件用「上级目录/文件名」消歧，仍重名再逐级加。
   · 关闭当前标签后优先激活右侧邻居。
   · 右键：关闭 / 关闭其他 / 关闭右侧 / 全部关闭 / 复制路径。
   · 快捷键：Ctrl+W 关闭，Ctrl+Tab / Ctrl+Shift+Tab / Ctrl+PgUp / Ctrl+PgDn 切换。
   ================================================================ */
function ideDropTab(rel) {
  if (state.ideModels[rel]) { try { state.ideModels[rel].dispose(); } catch (e) { /* 忽略 */ } delete state.ideModels[rel]; }
  const cached = state.ideCache[rel];
  if (cached && cached.image && cached.url) { try { URL.revokeObjectURL(cached.url); } catch (e) { /* 忽略 */ } }
  delete state.ideCache[rel];
  delete state.ideDirtyMap[rel];
  if (state.idePreviewTab === rel) state.idePreviewTab = '';
}

function ideMarkTabKind(rel, preview) {
  if (preview) {
    const old = state.idePreviewTab;
    // 旧预览标签还没被改过 → 被新预览原地替换（这是 VS Code 预览标签的核心语义）
    if (old && old !== rel && !state.ideDirtyMap[old] && (state.ideTabList || []).includes(old)) {
      state.ideTabList.splice(state.ideTabList.indexOf(old), 1);
      ideDropTab(old);
    }
    state.idePreviewTab = rel;
  } else if (state.idePreviewTab === rel) {
    state.idePreviewTab = '';
  }
}

function idePinTab(rel) {
  if (!rel || state.idePreviewTab !== rel) return;
  state.idePreviewTab = '';
  renderIdeTabs();
}

function ideTabLabel(rel) {
  const base = rel.split('/').pop();
  const dup = (state.ideTabList || []).filter((p) => p.split('/').pop() === base);
  if (dup.length < 2) return base;
  const parts = rel.split('/');
  for (let n = 2; n <= parts.length; n += 1) {
    const cand = parts.slice(-n).join('/');
    if (dup.filter((p) => p.split('/').slice(-n).join('/') === cand).length < 2) return cand;
  }
  return rel;
}

function ideCycleTab(dir) {
  const list = state.ideTabList || [];
  if (list.length < 2) return;
  const i = Math.max(0, list.indexOf(state.ideOpen));
  ideActivate(list[(i + dir + list.length) % list.length]);
}

function renderIdeTabs() {
  const box = $('#ide-tabs');
  if (!box) return;
  box.innerHTML = '';
  let activeEl = null;
  (state.ideTabList || []).forEach((rel, idx) => {
    const isActive = rel === state.ideOpen;
    const isPreview = state.idePreviewTab === rel;
    const t = document.createElement('div');
    t.className = 'ide-tab' + (isActive ? ' active' : '') + (isPreview ? ' preview' : '');
    t.title = rel + (isPreview ? '\n预览标签：双击或开始编辑后固定' : '');
    t.dataset.i = String(idx);
    const nm = document.createElement('span');
    nm.className = 'ide-tab-name';
    nm.textContent = ideTabLabel(rel);
    t.appendChild(ideIcoEl(rel.split('/').pop(), false));
    t.appendChild(nm);
    if (state.ideDirtyMap[rel]) {
      const dot = document.createElement('span');
      dot.className = 'ide-tab-dot';
      dot.textContent = '●';
      t.appendChild(dot);
    }
    const st = ideChangeStatOf(rel);
    if (st) {
      const s = document.createElement('span');
      s.className = 'ide-tab-stat';
      s.innerHTML = '<b class="add">+' + st.adds + '</b> <b class="del">−' + st.dels + '</b>';
      s.title = '待审查：+' + st.adds + ' 行 / −' + st.dels + ' 行';
      t.appendChild(s);
    }
    const x = document.createElement('span');
    x.className = 'ide-tab-x';
    x.textContent = '✕';
    x.title = '关闭';
    x.onclick = (e) => { e.stopPropagation(); ideCloseTab(rel); };
    t.appendChild(x);
    t.onclick = () => { if (isPreview) idePinTab(rel); ideActivate(rel); };
    t.addEventListener('dblclick', (e) => { e.preventDefault(); idePinTab(rel); });
    // 中键关闭（VS Code 的常规操作）
    t.addEventListener('auxclick', (e) => { if (e.button === 1) { e.preventDefault(); ideCloseTab(rel); } });
    t.addEventListener('mousedown', (e) => { if (e.button === 1) e.preventDefault(); });
    // 拖拽排序
    t.draggable = true;
    t.addEventListener('dragstart', (e) => {
      state.ideDragTab = rel;
      t.classList.add('dragging');
      if (e.dataTransfer) e.dataTransfer.effectAllowed = 'move';
    });
    t.addEventListener('dragover', (e) => { e.preventDefault(); t.classList.add('drop-target'); });
    t.addEventListener('dragleave', () => t.classList.remove('drop-target'));
    t.addEventListener('drop', (e) => {
      e.preventDefault();
      t.classList.remove('drop-target');
      const from = state.ideDragTab;
      if (!from || from === rel) return;
      const list = (state.ideTabList || []).slice();
      const i = list.indexOf(from), j = list.indexOf(rel);
      if (i < 0 || j < 0) return;
      list.splice(i, 1);
      list.splice(j, 0, from);
      state.ideTabList = list;
      renderIdeTabs();
    });
    t.addEventListener('dragend', () => {
      state.ideDragTab = '';
      $$('#ide-tabs .ide-tab').forEach((el) => el.classList.remove('dragging', 'drop-target'));
    });
    box.appendChild(t);
    if (isActive) activeEl = t;
  });
  // 激活的标签必须可见：标签多到溢出时把它滚进视口（VS Code 同样行为）
  if (activeEl && box.scrollWidth > box.clientWidth) {
    try { activeEl.scrollIntoView({ block: 'nearest', inline: 'nearest' }); } catch (e) { /* 忽略 */ }
  }
}

/* 关闭一批标签；有未保存改动时只弹一次确认。 */
function ideCloseTabs(pred, title) {
  const targets = (state.ideTabList || []).filter(pred);
  if (!targets.length) return;
  const closeAll = () => {
    targets.forEach((p) => {
      const i = (state.ideTabList || []).indexOf(p);
      if (i < 0) return;
      state.ideTabList.splice(i, 1);
      ideDropTab(p);
    });
    if ((state.ideTabList || []).includes(state.ideOpen)) { renderIdeTabs(); return; }
    const next = state.ideTabList[0] || '';
    if (next) ideActivate(next);
    else ideClearEditor();
  };
  const dirty = targets.filter((p) => state.ideDirtyMap[p]);
  if (dirty.length) {
    ideConfirm('未保存的修改', '有 ' + dirty.length + ' 个文件未保存，仍要' + title + '吗？',
      title, closeAll);
    return;
  }
  closeAll();
}

function ideClearEditor() {
  state.ideOpen = '';
  if (state.ideEditor) state.ideEditor.setModel(null);
  const t = $('#ide-text'); if (t) t.value = '';
  const img = $('#ide-image-img'); if (img) img.removeAttribute('src');
  ideShowView('editor');
  renderIdeTabs(); ideSyncTreeActive(); ideUpdateStatus(); ideUpdateDiffBtn();
}

function ideTabCtxOpen(x, y, rel) {
  const el = document.getElementById('ide-ctxmenu');
  if (!el) return;
  const list = state.ideTabList || [];
  const i = list.indexOf(rel);
  const items = [
    { label: '关闭', key: 'Ctrl+W', run: () => ideCloseTab(rel) },
    { label: '关闭其他', disabled: list.length < 2, run: () => ideCloseTabs((p) => p !== rel, '关闭其他') },
    { label: '关闭右侧', disabled: i < 0 || i >= list.length - 1, run: () => ideCloseTabs((p) => list.indexOf(p) > i, '关闭右侧') },
    { label: '全部关闭', disabled: !list.length, run: () => ideCloseTabs(() => true, '全部关闭') },
    { sep: true },
    { label: '复制路径', run: () => ideCopyPath([rel]) },
  ];
  ideMenuShow(el, x, y, items);
}

function ideTabsBind() {
  const box = $('#ide-tabs');
  if (!box || box.dataset.bound === '1') return;
  box.dataset.bound = '1';
  // 溢出时滚轮/触控板直接横向滚（滚动条已隐藏）
  box.addEventListener('wheel', (e) => {
    if (Math.abs(e.deltaY) <= Math.abs(e.deltaX)) return;
    box.scrollLeft += e.deltaY;
    e.preventDefault();
  }, { passive: false });
  box.addEventListener('contextmenu', (e) => {
    const t = e.target && e.target.closest ? e.target.closest('.ide-tab') : null;
    if (!t) return;
    e.preventDefault();
    const rel = (state.ideTabList || [])[Number(t.dataset.i)];
    if (rel) ideTabCtxOpen(e.clientX, e.clientY, rel);
  });
  // 捕获阶段拦下，避免 Monaco / Electron 先吃掉这些键
  document.addEventListener('keydown', (e) => {
    if (state.uiMode !== 'ide') return;
    if (!(e.ctrlKey || e.metaKey) || e.altKey) return;
    const k = (e.key || '').toLowerCase();
    if (k === 'w' && state.ideOpen) {
      e.preventDefault(); e.stopPropagation();
      ideCloseTab(state.ideOpen);
    } else if (k === 'tab') {
      e.preventDefault(); e.stopPropagation();
      ideCycleTab(e.shiftKey ? -1 : 1);
    } else if (e.key === 'PageDown' || e.key === 'PageUp') {
      e.preventDefault(); e.stopPropagation();
      ideCycleTab(e.key === 'PageDown' ? 1 : -1);
    }
  }, true);
}

function ideSetDirty(rel, on) {
  if (!rel) return;
  state.ideDirtyMap[rel] = !!on;
  renderIdeTabs();
  ideUpdateStatus();
}

function ideUpdateStatus() {
  const rel = state.ideOpen;
  const c = (rel && state.ideCache[rel]) || {};
  // 没有打开文件时整条状态栏不占位：否则只剩一个「—」和一片空白，看着像无用的底栏
  const bar = document.querySelector('.ide-status');
  if (bar) bar.classList.toggle('hidden', !rel);
  // 状态栏左侧只放文件名（完整路径进 title）：VS Code 的状态栏不放路径，长路径会把整条压满
  const p = $('#ide-status-path');
  if (p) { p.textContent = rel ? rel.split('/').pop() : '—'; p.title = rel || ''; }
  const enc = $('#ide-status-enc');
  if (enc) {
    enc.textContent = rel
      ? (c.image ? (c.size != null ? (Math.max(1, Math.round(c.size / 1024)) + ' KB') : '') : (c.enc || 'UTF-8'))
      : '';
  }
  const lang = $('#ide-status-lang'); if (lang) lang.textContent = rel ? (c.image ? '图片' : ideLangOf(rel)) : '';
  const pos = $('#ide-status-pos'); if (pos && c.image) pos.textContent = '';
  const d = $('#ide-status-dirty'); if (d) d.classList.toggle('hidden', !(rel && state.ideDirtyMap[rel]));
}

/* 保存成功的反馈走状态栏（VS Code 也是在状态栏而不弹窗）：
   临时占用编码那一格，2 秒后恢复。Ctrl+S 连着按也不会叠一堆 toast。 */
let _ideFlashTimer = null;
function ideFlashStatus(text) {
  const el = $('#ide-status-enc');
  if (!el) return;
  el.textContent = text;
  el.classList.add('ide-status-flash');
  clearTimeout(_ideFlashTimer);
  _ideFlashTimer = setTimeout(() => {
    el.classList.remove('ide-status-flash');
    ideUpdateStatus();
  }, 2000);
}

/* ---------------- 语言服务前台化（pyright） ----------------
   后端 app/api/ide_intel.py 把 lsp.py 的结构化接口暴露出来；这里做四件事：
     1. 打开 / 保存 Python 文件后拉诊断 → Monaco 波浪线 + 问题面板
     2. 大纲（documentSymbol）
     3. 转到定义 / 查找引用（跨文件跳转；多条结果落到「结果」页签）
     4. 重命名符号（先出方案，确认后一次写盘，走 undo 栈）
   只接 Python；其它语言一次请求都不发，避免在没装 pyright 的机器上反复报错。 */
const IDE_INTEL = {
  tab: 'problems',      // problems | outline | results
  open: false,
  file: '',
  items: [],
  symbols: [],
  results: [],
  avail: null,          // null 未知 / true 可用 / false 不可用
  error: '',
  timer: 0,
};

function ideLspEligible(rel) { return /\.pyi?$/i.test(String(rel || '')); }

function ideIntelUpdateBadge() {
  const el = $('#ide-status-problems');
  if (!el) return;
  const errs = IDE_INTEL.items.filter((x) => x.severity === 8).length;
  const warns = IDE_INTEL.items.length - errs;
  el.classList.toggle('hidden', !IDE_INTEL.open && !IDE_INTEL.items.length);
  el.textContent = '✖ ' + errs + '  ⚠ ' + warns;
  el.classList.toggle('has-err', errs > 0);
}

function ideIntelClear(rel) {
  const M = state.ideMonaco;
  const model = state.ideModels[rel];
  if (M && model) { try { M.editor.setModelMarkers(model, 'gm-lsp', []); } catch (e) { /* 忽略 */ } }
  if (IDE_INTEL.file === rel) {
    IDE_INTEL.items = [];
    IDE_INTEL.symbols = [];
    ideIntelUpdateBadge();
  }
}

async function ideIntelDiagnose(rel) {
  if (!rel || !ideLspEligible(rel) || !state.ideEditor) return;
  let r = null;
  try {
    r = await api('/api/ide/lsp/diagnostics', { session_id: state.sessions.current, path: rel });
  } catch (e) {
    return;                              // 后端不可达：静默，别打断编辑
  }
  if (state.ideOpen !== rel) return;     // 期间切了文件：这次结果作废
  if (!r || !r.ok) {
    IDE_INTEL.avail = false;
    IDE_INTEL.error = (r && r.error) || '语言服务不可用';
    ideIntelClear(rel);
    if (IDE_INTEL.open) ideIntelRender();
    return;
  }
  IDE_INTEL.avail = true;
  IDE_INTEL.error = '';
  IDE_INTEL.file = rel;
  IDE_INTEL.items = r.items || [];
  const M = state.ideMonaco;
  const model = state.ideModels[rel];
  if (M && model) {
    const markers = IDE_INTEL.items.map((it) => ({
      severity: it.severity,                       // 8 错误 / 4 警告（Monaco 自己的枚举值）
      message: it.message + (it.code ? '  [' + it.code + ']' : ''),
      source: 'pyright',
      startLineNumber: it.line + 1,
      startColumn: it.col + 1,
      endLineNumber: (it.endLine != null ? it.endLine : it.line) + 1,
      endColumn: Math.max(it.endCol || 0, it.col + 1) + 1,
    }));
    try { M.editor.setModelMarkers(model, 'gm-lsp', markers); } catch (e) { /* 忽略 */ }
  }
  ideIntelUpdateBadge();
  if (IDE_INTEL.open) ideIntelRender();
}

function ideIntelSchedule(rel) {
  clearTimeout(IDE_INTEL.timer);
  IDE_INTEL.timer = setTimeout(() => ideIntelDiagnose(rel), 260);
}

/* 切文件：面板里的数据换成本文件的；Python 才排一次诊断 */
function ideIntelOnOpen(rel) {
  IDE_INTEL.file = rel || '';
  IDE_INTEL.items = [];
  IDE_INTEL.symbols = [];
  IDE_INTEL.results = [];
  ideIntelUpdateBadge();
  if (IDE_INTEL.open) {
    if (IDE_INTEL.tab === 'outline') ideIntelLoadSymbols(rel);
    else ideIntelRender();
  }
  if (ideLspEligible(rel)) ideIntelSchedule(rel);
  else if (ideMdEligible(rel)) ideMdLintSchedule(rel);
}

async function ideIntelLoadSymbols(rel) {
  // LaTeX 用正则抽章节（不编译、不依赖语言服务）
  if (rel && /\.tex$/i.test(rel)) {
    let t = null;
    try {
      t = await api('/api/ide/tex/outline', { session_id: state.sessions.current, path: rel });
    } catch (e) { t = null; }
    if (state.ideOpen !== rel) return;
    IDE_INTEL.symbols = (t && t.ok && t.items) || [];
    ideIntelRender();
    return;
  }
  if (!rel || !ideLspEligible(rel)) { IDE_INTEL.symbols = []; ideIntelRender(); return; }
  let r = null;
  try {
    r = await api('/api/ide/lsp/symbols', { session_id: state.sessions.current, path: rel });
  } catch (e) { r = null; }
  if (state.ideOpen !== rel) return;
  IDE_INTEL.symbols = (r && r.ok && r.items) || [];
  ideIntelRender();
}

function ideIntelToggle(tab, force) {
  const box = $('#ide-intel');
  if (!box) return;
  const want = (typeof force === 'boolean') ? force : !IDE_INTEL.open;
  if (want && tab) IDE_INTEL.tab = tab;
  IDE_INTEL.open = want;
  box.classList.toggle('hidden', !want);
  if (want) {
    if (IDE_INTEL.tab === 'outline') ideIntelLoadSymbols(state.ideOpen);
    else ideIntelRender();
  }
  ideIntelUpdateBadge();
}

function ideIntelRender() {
  const body = $('#ide-intel-body');
  if (!body) return;
  $$('#ide-intel .ide-intel-tab').forEach((b) => {
    b.classList.toggle('active', b.dataset.intelTab === IDE_INTEL.tab);
    if (b.dataset.intelTab === 'results') b.classList.toggle('hidden', !IDE_INTEL.results.length);
  });
  const pc = $('#ide-intel-pcount');
  if (pc) pc.textContent = IDE_INTEL.items.length ? String(IDE_INTEL.items.length) : '';
  const oc = $('#ide-intel-ocount');
  if (oc) oc.textContent = IDE_INTEL.symbols.length ? String(IDE_INTEL.symbols.length) : '';
  body.innerHTML = '';

  if (IDE_INTEL.tab === 'problems') {
    if (!IDE_INTEL.items.length) {
      body.innerHTML = '<div class="ide-empty">' + (IDE_INTEL.avail === false
        ? ('语言服务不可用：' + escapeHtml(IDE_INTEL.error || '未检测到 pyright'))
        : (ideMdEligible(state.ideOpen) ? '这个 Markdown 文件没有语法问题' : '当前文件没有错误或警告')) + '</div>';
      return;
    }
    IDE_INTEL.items.forEach((it) => {
      const row = document.createElement('div');
      row.className = 'ide-intel-row ' + (it.severity === 8 ? 'err' : 'warn');
      const where = (it.file && it.file !== IDE_INTEL.file) ? (it.file + ' · ') : '';
      row.innerHTML = '<span class="ide-intel-ico">' + (it.severity === 8 ? '✖' : '⚠') + '</span>'
        + '<span class="ide-intel-text">' + escapeHtml(it.message) + '</span>'
        + '<span class="ide-intel-meta">' + (it.code ? escapeHtml(it.code) + ' · ' : '')
        + escapeHtml(where) + 'L' + (it.line + 1) + '</span>';
      row.onclick = () => ideIntelOpenLocation(it.file || IDE_INTEL.file || state.ideOpen,
        it.line + 1, (it.col || 0) + 1);
      body.appendChild(row);
    });
    return;
  }

  if (IDE_INTEL.tab === 'outline') {
    if (!IDE_INTEL.symbols.length) {
      body.innerHTML = '<div class="ide-empty">' + (ideLspEligible(state.ideOpen)
        ? '没有符号（空文件，或语言服务还没分析完）' : '只有 Python 文件支持大纲') + '</div>';
      return;
    }
    IDE_INTEL.symbols.forEach((s) => {
      const row = document.createElement('div');
      row.className = 'ide-intel-row';
      row.style.paddingLeft = (10 + (s.depth || 0) * 14) + 'px';
      row.innerHTML = '<span class="ide-intel-text">' + escapeHtml(s.name || '')
        + (s.detail ? ' <span class="ide-intel-meta">' + escapeHtml(s.detail) + '</span>' : '')
        + '</span><span class="ide-intel-meta">L' + (s.line || 0) + '</span>';
      row.onclick = () => ideIntelOpenLocation(state.ideOpen, s.line, 1);
      body.appendChild(row);
    });
    return;
  }

  // results：定义 / 引用
  if (!IDE_INTEL.results.length) {
    body.innerHTML = '<div class="ide-empty">没有结果</div>';
    return;
  }
  IDE_INTEL.results.forEach((r) => {
    const row = document.createElement('div');
    row.className = 'ide-intel-row';
    row.innerHTML = '<span class="ide-intel-text">' + escapeHtml(r.label) + '</span>'
      + '<span class="ide-intel-meta">' + escapeHtml(r.meta || '') + '</span>';
    row.onclick = () => ideIntelOpenLocation(r.path, r.line, r.col);
    body.appendChild(row);
  });
}

async function ideIntelOpenLocation(rel, line, col) {
  if (!rel) return;
  if (state.ideOpen !== rel || !state.ideModels[rel]) await openIdeFile(rel);
  if (state.ideOpen !== rel) return;
  const ed = state.ideEditor;
  if (!ed) return;
  const n = Math.max(1, parseInt(line, 10) || 1);
  const c = Math.max(1, parseInt(col, 10) || 1);
  ed.setPosition({ lineNumber: n, column: c });
  ed.revealLineInCenter(n);
  ed.focus();
}

async function ideIntelGoto(kind) {
  const rel = state.ideOpen;
  if (!rel) return;
  if (!ideLspEligible(rel)) return toast('只有 Python 文件支持语言跳转');
  const pos = state.ideEditor && state.ideEditor.getPosition();
  if (!pos) return;
  let r = null;
  try {
    r = await api('/api/ide/lsp/goto', {
      session_id: state.sessions.current, path: rel, kind: kind,
      line: pos.lineNumber, col: pos.column,
    });
  } catch (e) { return toast('跳转失败：' + e); }
  if (!r) return;
  if (!r.ok) return toast(r.error || '语言服务不可用');
  const items = r.items || [];
  if (!items.length) return toast(kind === 'references' ? '没有找到引用' : '没有找到定义');
  if (kind !== 'references' && items.length === 1) {
    return ideIntelOpenLocation(items[0].path, items[0].line, items[0].col);
  }
  IDE_INTEL.results = items.map((x) => ({
    label: String(x.path || '').split('/').pop() + ':' + x.line,
    meta: x.path, path: x.path, line: x.line, col: x.col,
  }));
  ideIntelToggle('results', true);
}

async function ideRenameSymbol() {
  const rel = state.ideOpen;
  if (!rel) return;
  if (!ideLspEligible(rel)) return toast('只有 Python 文件支持重命名');
  const ed = state.ideEditor;
  const pos = ed && ed.getPosition();
  const model = ed && ed.getModel();
  if (!pos || !model) return;
  const word = model.getWordAtPosition(pos);
  if (!word) return toast('把光标放到要重命名的符号上');
  const name = await appPrompt('把「' + word.word + '」重命名为：', word.word);
  if (!name || name === word.word) return;
  const base = { session_id: state.sessions.current, path: rel,
                 line: pos.lineNumber, col: pos.column, new_name: name };
  let plan = null;
  try { plan = await api('/api/ide/lsp/rename', base); }
  catch (e) { return toast('重命名失败：' + e); }
  if (!plan || !plan.ok) return toast((plan && plan.error) || '没有拿到重命名方案');
  const files = (plan.files || []).map((f) => f.path);
  ideConfirm('重命名符号',
    '将修改 ' + files.length + ' 个文件：\n' + files.slice(0, 12).join('\n')
      + (files.length > 12 ? '\n…' : ''), '应用', async () => {
      let done = null;
      try { done = await api('/api/ide/lsp/rename', Object.assign({ apply: true }, base)); }
      catch (e) { return toast('写入失败：' + e); }
      if (!done || !done.ok) return toast((done && done.error) || '写入失败');
      await ideReloadFiles(done.files || []);
      toast('已重命名，影响 ' + (done.count || 0) + ' 个文件（可在 AI 变更里撤销）');
      ideIntelSchedule(state.ideOpen);
    });
}

/* 重命名会改多个文件：把已打开的那些模型作废再重新载入，避免编辑器里还是旧内容 */
async function ideReloadFiles(list) {
  (list || []).forEach((rel) => {
    if (state.ideModels[rel]) {
      try { state.ideModels[rel].dispose(); } catch (e) { /* 忽略 */ }
      delete state.ideModels[rel];
    }
    delete state.ideCache[rel];
    state.ideDirtyMap[rel] = false;
  });
  const cur = state.ideOpen;
  if (cur && (list || []).indexOf(cur) >= 0) {
    state.ideTabList = (state.ideTabList || []).filter((x) => x !== cur);
    await openIdeFile(cur);
  }
  loadIdeTree();
}

/* 磁盘被外部改了（时间线回滚 / git 切换分支）：把打开的文件重新读一遍。
   有未保存改动的标签跳过，避免把用户正在写的丢掉。 */
async function ideReloadAll() {
  if (state.uiMode !== 'ide' || !state.ideMonaco) return;
  const cur = state.ideOpen;
  const list = (state.ideTabList || []).slice();
  const reload = list.filter((rel) => !state.ideDirtyMap[rel]);
  // 先重读内容到缓存（标签不删，切回去时直接从缓存建模型）
  for (const rel of reload) {
    try {
      const r = await api('/api/sandbox/file', { session_id: state.sessions.current, path: rel });
      if (r && r.ok) state.ideCache[rel] = { text: r.text || '', enc: r.encoding || '' };
    } catch (e) { /* 读不到就保留旧缓存 */ }
    if (state.ideModels[rel]) {
      try { state.ideModels[rel].dispose(); } catch (e) { /* 忽略 */ }
      delete state.ideModels[rel];
    }
  }
  if (cur && reload.indexOf(cur) >= 0) await ideActivate(cur);
  loadIdeTree();
  if (cur && reload.indexOf(cur) >= 0) ideIntelSchedule(cur);
}

/* 当前编辑器选区 → 一条 `#code` 上下文（供对话输入框的 # 菜单使用） */
function ideCurrentCodeRef() {
  if (state.uiMode !== 'ide' || !state.ideOpen || !state.ideEditor) return null;
  const ed = state.ideEditor;
  const model = ed.getModel();
  const sel = ed.getSelection();
  if (!model || !sel || sel.isEmpty()) return null;
  const text = model.getValueInRange(sel);
  if (!text || !text.trim()) return null;
  return {
    path: state.ideOpen,
    start: sel.startLineNumber,
    end: sel.endLineNumber,
    text: text,
  };
}

/* ---------------- 内联编辑（Ctrl+I） ----------------
   选中一段代码 → 一句要求 → 模型只返回替换后的片段 → 直接写进缓冲区。
   不落盘：满意就 Ctrl+S；不满意 Ctrl+Z 一下回到原样。 */
async function ideInlineEditAI() {
  const rel = state.ideOpen;
  const ed = state.ideEditor;
  if (!rel || !ed) return;
  const model = ed.getModel();
  const sel = ed.getSelection();
  if (!model || !sel || sel.isEmpty()) return toast('先选中要改的代码，再按 Ctrl+I');
  const chosen = model.getValueInRange(sel);
  if (!chosen.trim()) return toast('选区是空的');
  const instruction = await appPrompt('让 AI 怎么改这段代码？（例如：改成列表推导 / 加上类型注解）', '');
  if (!instruction || !instruction.trim()) return;
  const full = model.getValue();
  const startOff = model.getOffsetAt(sel.getStartPosition());
  const endOff = model.getOffsetAt(sel.getEndPosition());
  ideFlashStatus('AI 改写中…');
  let r = null;
  try {
    r = await api('/api/ide/inline_edit', {
      session_id: state.sessions.current, path: rel,
      language: model.getLanguageId(), selection: chosen,
      before: full.slice(Math.max(0, startOff - 4000), startOff),
      after: full.slice(endOff, endOff + 2000),
      instruction: instruction.trim(),
    });
  } catch (e) { return toast('内联编辑失败：' + e); }
  if (!r || !r.ok) return toast((r && r.error) || '内联编辑失败');
  if (state.ideOpen !== rel || ed.getModel() !== model) return;   // 期间切了文件
  ed.pushUndoStop();
  ed.executeEdits('gm-inline-edit', [{ range: sel, text: r.text, forceMoveMarkers: true }]);
  ed.pushUndoStop();
  ideSetDirty(rel, true);
  toast('已替换选区（Ctrl+Z 撤销，Ctrl+S 保存）');
}

/* 选中代码 → 旁注卡片里多轮追问（只读，不改代码；要改写仍按 Ctrl+I）。
   卡片本身在 aside.js 里，这里只负责冻结选区并把位置传过去。 */
function ideInlineAsk() {
  const rel = state.ideOpen;
  const ed = state.ideEditor;
  if (!rel || !ed) return;
  const model = ed.getModel();
  const sel = ed.getSelection();
  if (!model || !sel || sel.isEmpty()) return toast('先选中要问的代码');
  const chosen = model.getValueInRange(sel);
  if (!chosen.trim()) return toast('选区是空的');
  if (typeof window.asideOpenCode !== 'function') return toast('旁注组件还没加载');
  const full = model.getValue();
  const startOff = model.getOffsetAt(sel.getStartPosition());
  const endOff = model.getOffsetAt(sel.getEndPosition());
  let rect = null;
  const dom = ed.getDomNode && ed.getDomNode();
  const pos = ed.getScrolledVisiblePosition && ed.getScrolledVisiblePosition(sel.getStartPosition());
  if (dom && pos) {
    const box = dom.getBoundingClientRect();
    rect = {
      left: box.left + pos.left,
      top: box.top + pos.top,
      bottom: box.top + pos.top + (pos.height || 20),
      right: box.left + pos.left + 260,
    };
  }
  window.asideOpenCode({
    path: rel,
    language: model.getLanguageId(),
    text: chosen,
    before: full.slice(Math.max(0, startOff - 4000), startOff),
    after: full.slice(endOff, endOff + 2000),
    startLine: sel.startLineNumber,
    endLine: sel.endLineNumber,
  }, rect);
}

/* ---------------- 网页预览 ----------------
   沙箱目录由后端临时挂到 127.0.0.1:<随机端口>（只读、只监听本机），
   也可以直接填自己的 dev server 地址。关掉面板就把临时服务停掉。 */
let idePreviewBase = '';

/* 预览桥：app/preview.py 会往沙箱内的 HTML 注入一段脚本，负责
   ① 元素选择（悬停高亮 + 点击回传选择器 / 样式 / HTML）
   ② console.error / warn / onerror / 未处理 Promise 的收集
   外部 dev server 没有这段脚本，两个按钮保持禁用并在 title 里说明。 */
let idePreviewBridge = false;
let idePreviewPicking = false;
let idePreviewLogs = [];

function idePreviewFrameEl() { return $('#ide-preview-frame'); }

function idePreviewPost(msg) {
  const f = idePreviewFrameEl();
  if (!f || !f.getAttribute('src')) return;
  try { if (f.contentWindow) f.contentWindow.postMessage(msg, '*'); } catch (e) { /* 页面已卸载 */ }
}

function idePreviewResetBridge() {
  idePreviewBridge = false;
  idePreviewPicking = false;
  idePreviewLogs = [];
  idePreviewSyncControls();
  idePreviewRenderLogs();
}

function idePreviewSyncControls() {
  const pick = $('#ide-preview-pick');
  if (pick) {
    pick.disabled = !idePreviewBridge;
    pick.classList.toggle('active', idePreviewPicking);
    pick.title = idePreviewBridge
      ? (idePreviewPicking ? '取消选择元素（Esc）' : '选择元素并发送到对话')
      : '当前页面不支持元素选择（只有沙箱内的 HTML 会注入预览桥）';
  }
  const con = $('#ide-preview-console-btn');
  if (con) {
    con.disabled = !idePreviewBridge;
    con.title = idePreviewBridge
      ? ('控制台（' + idePreviewLogs.length + ' 条）')
      : '当前页面不支持控制台采集（只有沙箱内的 HTML 会注入预览桥）';
  }
  const badge = $('#ide-preview-console-badge');
  if (badge) {
    const n = idePreviewLogs.length;
    badge.textContent = String(n > 99 ? '99+' : n);
    badge.classList.toggle('hidden', !n);
  }
  const count = $('#ide-preview-console-count');
  if (count) count.textContent = idePreviewLogs.length + ' 条';
}

function idePreviewRenderLogs() {
  idePreviewSyncControls();
  const box = $('#ide-preview-console-list');
  if (!box) return;
  box.innerHTML = '';
  if (!idePreviewLogs.length) {
    const empty = document.createElement('div');
    empty.className = 'ide-preview-empty';
    empty.textContent = idePreviewBridge ? '暂无错误或警告' : '当前页面没有预览桥，无法采集控制台';
    box.appendChild(empty);
    return;
  }
  idePreviewLogs.forEach((item) => {
    const row = document.createElement('div');
    row.className = 'ide-preview-log ' + (item.kind === 'warn' ? 'warn' : 'error');
    const kind = document.createElement('span');
    kind.className = 'ide-preview-log-kind';
    kind.textContent = item.kind === 'warn' ? 'warn' : 'error';
    const text = document.createElement('div');
    text.className = 'ide-preview-log-text';
    text.textContent = String(item.text || '');
    text.title = String(item.text || '');
    const send = document.createElement('button');
    send.type = 'button';
    send.className = 'ide-btn ide-preview-log-send';
    send.textContent = '发送';
    send.onclick = () => idePreviewSendLog(item);
    row.appendChild(kind);
    row.appendChild(text);
    row.appendChild(send);
    box.appendChild(row);
  });
  box.scrollTop = box.scrollHeight;
}

function idePreviewLogText(item) {
  const kind = item.kind === 'warn' ? 'warn' : 'error';
  let when = '';
  if (item.time) {
    try { when = new Date(item.time).toLocaleTimeString('zh-CN', { hour12: false }); } catch (e) { when = ''; }
  }
  return '【预览控制台 · ' + kind + (when ? ' · ' + when : '') + '】\n'
    + (item.url ? '页面：' + item.url + '\n' : '') + String(item.text || '');
}

function idePreviewSendLog(item) {
  if (!addQuote(idePreviewLogText(item), '控制台 ' + (item.kind === 'warn' ? '警告' : '错误'))) return;
  toast('已把控制台信息加入对话引用', 'success');
  const inp = $('#input');
  if (inp) inp.focus();
}

function idePreviewSendLogs() {
  if (!idePreviewLogs.length) return toast('控制台没有可发送的内容');
  const parts = [];
  let used = 0;
  idePreviewLogs.forEach((item) => {
    const s = idePreviewLogText(item);
    if (used + s.length > 3600) return;
    parts.push(s);
    used += s.length + 2;
  });
  addQuote(parts.join('\n\n'), '控制台 ' + parts.length + ' 条');
  toast('已把控制台信息加入对话引用', 'success');
  const inp = $('#input');
  if (inp) inp.focus();
}

function idePreviewClearLogs() {
  idePreviewLogs = [];
  idePreviewPost({ __gmPreviewCmd: 'clear' });
  idePreviewRenderLogs();
}

function idePreviewTogglePick(force) {
  if (!idePreviewBridge) return toast('当前页面不支持元素选择：只有沙箱内的 HTML 会注入预览桥');
  const on = (typeof force === 'boolean') ? force : !idePreviewPicking;
  idePreviewPicking = on;                 // 先反馈 UI，页面回报 pick-state 再校正
  idePreviewSyncControls();
  idePreviewPost({ __gmPreviewCmd: 'pick', on: on });
}

function idePreviewConsoleToggle(force) {
  const box = $('#ide-preview-console');
  if (!box) return;
  const show = (typeof force === 'boolean') ? force : box.classList.contains('hidden');
  box.classList.toggle('hidden', !show);
  if (!show) return;
  idePreviewPost({ __gmPreviewCmd: 'state' });
  idePreviewRenderLogs();
}

function idePreviewElementToChat(d) {
  const lines = ['【预览元素】'];
  if (d.page) lines.push('页面：' + d.page);
  if (d.title) lines.push('标题：' + d.title);
  if (d.selector) lines.push('选择器：' + d.selector);
  const tag = '<' + (d.tag || '') + (d.id ? '#' + d.id : '')
    + (d.classes ? ' class="' + d.classes + '"' : '') + '>';
  lines.push('标签：' + tag);
  if (d.text) lines.push('文本：' + d.text);
  if (d.rect) lines.push('位置：x=' + d.rect.x + ', y=' + d.rect.y + ', ' + d.rect.w + '×' + d.rect.h);
  if (d.styles) lines.push('样式：' + d.styles);
  if (d.html) lines.push('HTML：\n' + d.html);
  if (!addQuote(lines.join('\n'), '预览元素')) return;
  toast('已把元素加入对话引用', 'success');
  const inp = $('#input');
  if (inp) inp.focus();
}

function idePreviewBindBridge() {
  window.addEventListener('message', (e) => {
    const f = idePreviewFrameEl();
    if (!f || e.source !== f.contentWindow) return;
    const d = e.data || {};
    if (d.__gmPreview !== 1) return;
    if (d.type === 'ready') {
      idePreviewBridge = true;
      idePreviewSyncControls();
      idePreviewPost({ __gmPreviewCmd: 'state' });
    } else if (d.type === 'pick-state') {
      idePreviewPicking = !!(d.data && d.data.on);
      idePreviewSyncControls();
    } else if (d.type === 'pick') {
      idePreviewPicking = false;
      idePreviewSyncControls();
      idePreviewElementToChat(d.data || {});
    } else if (d.type === 'pick-cancel') {
      idePreviewPicking = false;
      idePreviewSyncControls();
    } else if (d.type === 'console-batch') {
      idePreviewLogs = Array.isArray(d.data) ? d.data.slice(-30) : [];
      idePreviewRenderLogs();
    }
  });
}

async function idePreviewToggle(force) {
  const box = $('#ide-preview');
  if (!box) return;
  const show = (typeof force === 'boolean') ? force : box.classList.contains('hidden');
  if (!show) { await idePreviewClose(); return; }
  box.classList.remove('hidden');
  const inp = $('#ide-preview-url');
  const frame = $('#ide-preview-frame');
  const rel = state.ideOpen || '';
  const isHtml = /\.html?$/i.test(rel);
  // 输入框跟当前文件对齐；已经填了 dev server 地址就不覆盖
  if (inp && (!inp.value || (isHtml && !/^https?:\/\//i.test(inp.value)))) {
    inp.value = isHtml ? rel : 'http://localhost:5173';
  }
  // 当前打开的就是网页：直接渲染出来，不用再多点一次「打开」
  if (isHtml && frame && !frame.getAttribute('src') && inp && inp.value === rel) {
    await idePreviewOpen();
    return;
  }
  if (inp) inp.focus();
}

async function idePreviewOpen() {
  const inp = $('#ide-preview-url');
  const frame = $('#ide-preview-frame');
  if (!inp || !frame) return;
  let v = (inp.value || '').trim();
  if (!v) return toast('先填一个地址，或打开一个 html 文件');
  // 习惯写法 localhost:5173 / 127.0.0.1:8000 没带协议：补 http://，别当成沙箱路径
  if (/^(localhost|127\.0\.0\.1|\[::1\])(:\d+)?(\/|$)/i.test(v)) {
    v = 'http://' + v;
    inp.value = v;
  }
  if (/^https?:\/\//i.test(v)) {
    idePreviewResetBridge();
    frame.src = v;
    return;
  }
  let r = null;
  try {
    r = await api('/api/ide/preview/start', { session_id: state.sessions.current, path: v });
  } catch (e) { return toast('预览启动失败：' + e); }
  if (!r || !r.ok) return toast((r && r.error) || '预览启动失败');
  idePreviewBase = r.base || '';
  idePreviewResetBridge();
  frame.src = r.url;
}

function idePreviewReload() {
  const frame = $('#ide-preview-frame');
  if (!frame || !frame.src) return;
  idePreviewResetBridge();
  try {
    const u = new URL(frame.src);
    u.searchParams.set('_t', String(Date.now()));   // 静态服务禁了缓存，再加一层防浏览器缓存
    frame.src = u.toString();
  } catch (e) { frame.src = frame.src; }
}

async function idePreviewClose() {
  const box = $('#ide-preview');
  if (box) box.classList.add('hidden');
  const frame = $('#ide-preview-frame');
  if (frame) frame.removeAttribute('src');
  idePreviewResetBridge();
  idePreviewConsoleToggle(false);
  if (idePreviewBase) {
    idePreviewBase = '';
    try { await api('/api/ide/preview/stop', { session_id: state.sessions.current }); }
    catch (e) { /* 停不掉就下次启动时复用 */ }
  }
}

/* ---------------- Markdown 预览（右半屏） ----------------
   渲染复用聊天那套 renderMarkdown（含 KaTeX 公式），不另写解析器；
   PDF 导出 = 把渲染结果包成完整 HTML，交给后端用 headless Edge 打印。 */
const IDE_MD_RE = /\.(md|markdown)$/i;

const IDE_MD_PRINT_CSS = [
  'body{font-family:"MiSans","Microsoft YaHei",-apple-system,sans-serif;line-height:1.75;',
  'color:#16181d;margin:26px 30px;font-size:13.5px}',
  'h1{font-size:22px}h2{font-size:18px}h3{font-size:16px}h4{font-size:14px}',
  'h1,h2,h3,h4{line-height:1.4;margin:18px 0 8px}',
  'p{margin:8px 0}ul,ol{padding-left:24px}li{margin:3px 0}',
  'pre{background:#f6f7f9;border:1px solid #e3e6ea;border-radius:6px;padding:10px;',
  'overflow:auto;font-size:12px;white-space:pre-wrap;word-break:break-word}',
  'code{font-family:Consolas,Menlo,monospace;font-size:12.4px}',
  'table{border-collapse:collapse;margin:10px 0}th,td{border:1px solid #dcdfe4;padding:5px 9px}',
  'th{background:#f6f7f9}blockquote{border-left:3px solid #dcdfe4;margin:10px 0;',
  'padding:2px 12px;color:#555}img{max-width:100%}a{color:#2b5cd9;text-decoration:none}',
  'h1,h2,h3,h4{page-break-after:avoid}pre,table,img,blockquote{page-break-inside:avoid}',
].join('');

function ideMdEligible(rel) { return IDE_MD_RE.test(String(rel || '')); }

let ideMdTimer = 0;

function ideMdToggle(force) {
  const rel = state.ideOpen;
  const wrap = document.querySelector('.ide-editor-wrap');
  const box = document.getElementById('ide-md');
  if (!wrap || !box) return;
  if (!ideMdEligible(rel)) return toast('只有 Markdown 文件支持预览');
  const show = (typeof force === 'boolean') ? force : box.classList.contains('hidden');
  box.classList.toggle('hidden', !show);
  wrap.classList.toggle('md-split', show);
  const btn = document.getElementById('ide-md-btn');
  if (btn) btn.classList.toggle('active', show);
  if (show) ideMdRender();
  else if (state.ideEditor) { try { state.ideEditor.layout(); } catch (e) { /* 忽略 */ } }
}

function ideMdClose() {
  const box = document.getElementById('ide-md');
  if (!box || box.classList.contains('hidden')) return;
  ideMdToggle(false);
}

function ideMdSchedule() {
  if (!ideMdEligible(state.ideOpen)) return;
  const box = document.getElementById('ide-md');
  if (!box || box.classList.contains('hidden')) return;
  clearTimeout(ideMdTimer);
  ideMdTimer = setTimeout(ideMdRender, 180);      // 打字时别每个字符都重排
}

function ideMdRender() {
  const rel = state.ideOpen;
  const body = document.getElementById('ide-md-body');
  const model = rel && state.ideModels[rel];
  if (!body || !model) return;
  const text = model.getValue();
  body.innerHTML = (typeof safeRenderMarkdown === 'function')
    ? safeRenderMarkdown(text)
    : ('<pre>' + escapeHtml(text) + '</pre>');
}

/* ---------------- Markdown 语法纠错（IDE 侧） ----------------
   规则引擎和笔记共用 web/mdlint.js；这里只做三件事：
     1. 打字 / 打开 / 保存后节流跑检查
     2. 结果写进 Monaco markers（波浪线 + 悬停说明）
     3. 同一份结果喂给「问题」面板（与 pyright 分流，互不覆盖）
   不受笔记的纠错开关影响：只要打开的是 .md 就一直检查。 */
const IDE_MD_LINT = { timer: 0 };

function ideMdLintSchedule(rel) {
  clearTimeout(IDE_MD_LINT.timer);
  const target = rel || state.ideOpen;
  if (!target || !ideMdEligible(target)) return;
  IDE_MD_LINT.timer = setTimeout(() => ideMdLint(target), 300);
}

function ideMdLint(rel) {
  const M = state.ideMonaco;
  const model = rel && state.ideModels[rel];
  if (!M || !model || !window.MDLint || !ideMdEligible(rel)) return;
  if (state.ideOpen !== rel) return;
  const items = MDLint.lint(model.getValue(), {});
  const markers = items.map((it) => {
    const col = Math.max(1, it.col || 1);
    return {
      severity: it.severity === 'error' ? 8 : (it.severity === 'info' ? 1 : 4),
      message: it.message + (it.rule ? '  [' + it.rule + ']' : ''),
      source: 'markdown',
      code: it.rule || '',
      startLineNumber: it.line + 1,
      startColumn: col,
      endLineNumber: (it.endLine != null ? it.endLine : it.line) + 1,
      endColumn: Math.max(col + 1, it.endCol || (col + 1)),
    };
  });
  try { M.editor.setModelMarkers(model, 'markdown', markers); } catch (e) { /* 忽略 */ }

  IDE_INTEL.file = rel;
  IDE_INTEL.avail = true;
  IDE_INTEL.error = '';
  IDE_INTEL.items = items.map((it) => ({
    severity: it.severity === 'error' ? 8 : 4,
    message: it.message,
    code: it.rule,
    file: rel,
    line: it.line,
    col: Math.max(0, (it.col || 1) - 1),
    fixable: !!it.fix,
  }));
  ideIntelUpdateBadge();
  if (IDE_INTEL.open) ideIntelRender();
}

function ideMdFixAll() {
  const rel = state.ideOpen;
  const ed = state.ideEditor;
  const model = rel && state.ideModels[rel];
  if (!rel || !model || !ideMdEligible(rel)) return toast('先打开一个 Markdown 文件');
  if (!window.MDLint) return;
  const fixable = MDLint.lint(model.getValue(), {}).filter((x) => x.fix);
  if (!fixable.length) return toast('没有可自动修正的问题');
  const r = MDLint.fixAll(model.getValue(), fixable);
  if (!r || r.text === model.getValue()) return toast('没有可自动修正的问题');
  try {
    // executeEdits 保留撤销栈：Ctrl+Z 能一步撤回整次修正
    ed.pushUndoStop();
    ed.executeEdits('md-lint', [{ range: model.getFullModelRange(), text: r.text }]);
    ed.pushUndoStop();
  } catch (e) {
    model.setValue(r.text);
  }
  ideSetDirty(rel, true);
  ideMdLintSchedule(rel);
  toast('已修正 ' + r.fixed + ' 处');
}

async function ideMdExportPdf() {
  const rel = state.ideOpen;
  const model = rel && state.ideModels[rel];
  if (!rel || !model) return toast('先打开一个 Markdown 文件');
  if (!model.getValue().trim()) return toast('文件是空的');
  const bodyHtml = (typeof safeRenderMarkdown === 'function')
    ? safeRenderMarkdown(model.getValue())
    : ('<pre>' + escapeHtml(model.getValue()) + '</pre>');
  const doc = '<!doctype html><html><head><meta charset="utf-8">'
    + '<link rel="stylesheet" href="' + location.origin
    + '/static/vendor/katex/katex.min.css">'
    + '<style>' + IDE_MD_PRINT_CSS + '</style></head><body>'
    + bodyHtml + '</body></html>';
  ideFlashStatus('生成 PDF…');
  let r = null;
  try {
    r = await api('/api/ide/export_pdf', {
      session_id: state.sessions.current, name: rel, html: doc,
    });
  } catch (e) { return toast('生成失败：' + e); }
  if (!r || !r.ok) return toast((r && r.error) || '生成失败');
  toast('已生成 ' + r.path + '（' + Math.max(1, Math.round((r.size || 0) / 1024)) + ' KB）');
  loadIdeTree();
}

/* ---------------- LaTeX 编译 ----------------
   和 VS Code 的 LaTeX Workshop 同一个模型：只调用本机已有的 latexmk / xelatex，
   我们只负责「探测 → 调起 → 解析日志 → 回填问题面板」。
   编译前自动保存（编译器读的是磁盘）；成功后用临时静态服务把 PDF 嵌进预览面板。 */
function ideTexEligible(rel) { return /\.tex$/i.test(String(rel || '')); }

function ideTexShowProblems(rel, errors) {
  const items = (errors || []).map((e) => {
    const ln = Math.max(1, Number(e.line) || 1);
    return {
      line: ln - 1, col: 0, endLine: ln - 1, endCol: 1,
      severity: 8, message: e.message || '编译错误',
      code: e.context || '', file: e.file || rel,
    };
  });
  ideToolProblems(rel, items, 'latex');
}

async function ideTexShowPdf(pdfRel) {
  if (!pdfRel) return;
  const box = $('#ide-preview');
  const frame = $('#ide-preview-frame');
  const inp = $('#ide-preview-url');
  if (!box || !frame) return;
  let r = null;
  try {
    r = await api('/api/ide/preview/start', {
      session_id: state.sessions.current, path: pdfRel,
    });
  } catch (e) { return toast('打开 PDF 失败：' + e); }
  if (!r || !r.ok) return toast((r && r.error) || '打开 PDF 失败');
  idePreviewBase = r.base || '';
  if (inp) inp.value = pdfRel;
  box.classList.remove('hidden');
  frame.src = r.url;
}

async function ideTexBuild() {
  const rel = state.ideOpen;
  if (!ideTexEligible(rel)) return toast('先打开一个 .tex 文件');
  if (state.ideDirtyMap[rel]) await saveIdeFile();     // 编译器读磁盘，先落盘
  if (state.ideDirtyMap[rel]) return;                  // 保存失败就别编了
  const btn = $('#ide-run-btn');
  if (btn) btn.classList.add('busy');
  ideFlashStatus('编译中…');
  let r = null;
  try {
    r = await api('/api/ide/tex/build', { session_id: state.sessions.current, path: rel });
  } catch (e) {
    if (btn) btn.classList.remove('busy');
    return toast('编译失败：' + e);
  }
  if (btn) btn.classList.remove('busy');
  if (!r) return;
  const errors = r.errors || [];
  if (errors.length) ideTexShowProblems(rel, errors);
  if (!r.ok) {
    toast((r.error || '编译失败') + (r.restored ? '（已保留上一版 PDF）' : ''));
    if (!errors.length && r.log) {
      const tail = String(r.log).trim().split('\n').pop() || '';
      ideTexShowProblems(rel, [{ message: tail.slice(0, 200) }]);
    }
    return;
  }
  toast('编译完成 · ' + (r.engine || 'latex') + ' · '
    + (((r.ms || 0) / 1000).toFixed(1)) + 's');
  loadIdeTree();
  await ideTexShowPdf(r.pdf);
}

async function ideTexClean() {
  const rel = state.ideOpen;
  if (!ideTexEligible(rel)) return toast('先打开一个 .tex 文件');
  let r = null;
  try {
    r = await api('/api/ide/tex/clean', { session_id: state.sessions.current, path: rel });
  } catch (e) { return toast('清理失败：' + e); }
  if (!r || !r.ok) return toast((r && r.error) || '清理失败');
  toast(r.count ? ('已清理 ' + r.count + ' 个辅助文件') : '没有需要清理的辅助文件');
  loadIdeTree();
}

/* 重新打开上一次编译出的 PDF（切走文件后想再看一眼） */
async function ideTexOpenPdf() {
  const rel = state.ideOpen;
  if (!ideTexEligible(rel)) return toast('先打开一个 .tex 文件');
  const pdf = String(rel).replace(/\.tex$/i, '.pdf');
  await ideTexShowPdf(pdf);
}

/* ---------------- Python / C：运行、构建、格式化 ----------------
   同样是「调本机工具」：python 直接跑；C 用 gcc 编译成同名 exe 再跑。
   跑在集成终端里（stdin 可用、能交互），不是在后台偷偷执行。 */
function idePyEligible(rel) { return /\.py$/i.test(String(rel || '')); }
function ideCEligible(rel) { return /\.(c|cc|cpp|cxx)$/i.test(String(rel || '')); }

function ideQuote(rel) { return '"' + String(rel).replace(/"/g, '\\"') + '"'; }

/* 拼沙箱内的绝对路径：终端是持久 shell，用户可能 cd 过，
   用相对路径跑程序会在别的目录下找不到文件。 */
function ideAbsPath(rel) {
  const s = (typeof curSession === 'function') ? curSession() : null;
  const root = (s && s.sandbox) ? String(s.sandbox) : '';
  if (!root) return String(rel);
  const sep = root.indexOf('\\') >= 0 ? '\\' : '/';
  return root.replace(/[\\/]+$/, '') + sep + String(rel).replace(/[\\/]+/g, sep);
}

async function ideRunInTerminal(cmd) {
  ideTermToggle(true);
  for (let i = 0; i < 40; i++) {               // 等 PTY 就绪（最多 6 秒）
    if (typeof ideTermState !== 'undefined' && ideTermState && ideTermState.alive) break;
    await new Promise((r) => setTimeout(r, 150));
  }
  ideTermSend(cmd + '\r');
}

/* 工具输出 → 问题面板 + 波浪线（ruff / gcc / latex 共用） */
function ideToolProblems(rel, items, engine) {
  IDE_INTEL.file = rel;
  IDE_INTEL.items = items || [];
  IDE_INTEL.avail = true;
  const M = state.ideMonaco;
  const model = state.ideModels[rel];
  if (M && model) {
    const markers = IDE_INTEL.items.filter((it) => !it.file || it.file === rel).map((it) => ({
      severity: it.severity || 8,
      message: (it.message || '') + (it.code ? '  ' + it.code : ''),
      source: engine || 'tool',
      startLineNumber: (it.line || 0) + 1,
      startColumn: (it.col || 0) + 1,
      endLineNumber: ((it.endLine != null ? it.endLine : it.line) || 0) + 1,
      endColumn: Math.max(it.endCol || 0, (it.col || 0) + 1) + 1,
    }));
    try { M.editor.setModelMarkers(model, 'gm-' + (engine || 'tool'), markers); } catch (e) { /* 忽略 */ }
  }
  ideIntelToggle('problems', true);
}

async function ideRunPython() {
  const rel = state.ideOpen;
  if (!idePyEligible(rel)) return toast('先打开一个 .py 文件');
  if (state.ideDirtyMap[rel]) await saveIdeFile();
  if (state.ideDirtyMap[rel]) return;          // 保存失败就别跑
  await ideRunInTerminal('python ' + ideQuote(ideAbsPath(rel)));
}

async function ideCCheck(rel) {
  if (!ideCEligible(rel)) return;
  let r = null;
  try {
    r = await api('/api/ide/c/check', { session_id: state.sessions.current, path: rel });
  } catch (e) { return; }
  if (state.ideOpen !== rel) return;
  if (!r || !r.ok) return;                     // 没装 gcc 时静默，不打扰编辑
  ideToolProblems(rel, r.items || [], 'gcc');
}

async function ideBuildC() {
  const rel = state.ideOpen;
  if (!ideCEligible(rel)) return toast('先打开一个 .c / .cpp 文件');
  if (state.ideDirtyMap[rel]) await saveIdeFile();
  if (state.ideDirtyMap[rel]) return;
  const btn = $('#ide-run-btn');
  if (btn) btn.classList.add('busy');
  ideFlashStatus('编译中…');
  let r = null;
  try {
    r = await api('/api/ide/c/build', { session_id: state.sessions.current, path: rel });
  } catch (e) {
    if (btn) btn.classList.remove('busy');
    return toast('构建失败：' + e);
  }
  if (btn) btn.classList.remove('busy');
  if (!r) return;
  if (r.errors && r.errors.length) ideToolProblems(rel, r.errors, 'gcc');
  if (!r.ok) return toast(r.error || '构建失败');
  toast('构建成功 · ' + r.exe);
  loadIdeTree();
  await ideRunInTerminal(ideQuote(ideAbsPath(r.exe)));
}

async function ideRuffCheck() {
  const rel = state.ideOpen;
  if (!idePyEligible(rel)) return toast('先打开一个 .py 文件');
  let r = null;
  try {
    r = await api('/api/ide/py/lint', { session_id: state.sessions.current, path: rel });
  } catch (e) { return toast('检查失败：' + e); }
  if (!r || !r.ok) return toast((r && r.error) || '检查失败');
  ideToolProblems(rel, r.items || [], 'ruff');
  toast(r.items && r.items.length
    ? ('ruff 发现 ' + r.items.length + ' 个问题') : 'ruff 没有发现问题');
}

async function ideFormatDoc() {
  const rel = state.ideOpen;
  if (!rel) return;
  if (state.ideDirtyMap[rel]) await saveIdeFile();
  let r = null;
  try {
    r = await api('/api/ide/format', { session_id: state.sessions.current, path: rel });
  } catch (e) { return toast('格式化失败：' + e); }
  if (!r || !r.ok) return toast((r && r.error) || '格式化失败');
  await ideReloadFiles([rel]);                 // 磁盘已变，重读
  toast(r.note || '已格式化');
}

function ideRunCurrent() {
  const rel = state.ideOpen;
  if (ideTexEligible(rel)) return ideTexBuild();
  if (idePyEligible(rel)) return ideRunPython();
  if (ideCEligible(rel)) return ideBuildC();
  toast('这个文件类型不支持运行');
}

function ideIntelBindUI() {
  $$('#ide-intel .ide-intel-tab').forEach((b) => {
    b.onclick = () => {
      IDE_INTEL.tab = b.dataset.intelTab;
      if (IDE_INTEL.tab === 'outline') ideIntelLoadSymbols(state.ideOpen);
      else ideIntelRender();
      ideIntelUpdateBadge();
    };
  });
  const x = $('#ide-intel-close');
  if (x) x.onclick = () => ideIntelToggle(null, false);
  const chip = $('#ide-status-problems');
  if (chip) chip.onclick = () => ideIntelToggle('problems', true);
}

/* Monaco 快捷键 / 右键菜单：定义 F12、引用 Shift+F12、重命名 F2、大纲 Ctrl+Shift+O、问题 Ctrl+Shift+M */
function ideIntelBindKeys(ed, M) {
  if (!ed || !M || ed._gmIntelBound) return;
  ed._gmIntelBound = true;
  const add = (id, label, keys, run, order) => ed.addAction({
    id: id, label: label, keybindings: keys,
    contextMenuGroupId: 'navigation', contextMenuOrder: order || 1.5, run: run,
  });
  add('gm.gotoDef', '转到定义', [M.KeyCode.F12], () => { ideIntelGoto('definition'); }, 1);
  add('gm.gotoRefs', '查找引用', [M.KeyMod.Shift | M.KeyCode.F12], () => { ideIntelGoto('references'); }, 2);
  add('gm.renameSymbol', '重命名符号', [M.KeyCode.F2], () => { ideRenameSymbol(); }, 3);
  add('gm.outline', '文件大纲', [M.KeyMod.CtrlCmd | M.KeyMod.Shift | M.KeyCode.KeyO],
      () => { ideIntelToggle('outline', true); }, 4);
  add('gm.problems', '问题面板', [M.KeyMod.CtrlCmd | M.KeyMod.Shift | M.KeyCode.KeyM],
      () => { ideIntelToggle('problems'); }, 5);
  add('gm.inlineEdit', 'AI 改写选中代码', [M.KeyMod.CtrlCmd | M.KeyCode.KeyI],
      () => { ideInlineEditAI(); }, 0);
  add('gm.inlineAsk', 'AI 问问选中代码',
      [M.KeyMod.CtrlCmd | M.KeyMod.Alt | M.KeyCode.KeyI],
      () => { ideInlineAsk(); }, 0.5);
  add('gm.mdPreview', 'Markdown 预览',
      [M.KeyMod.CtrlCmd | M.KeyMod.Shift | M.KeyCode.KeyV],
      () => { ideMdToggle(); }, 6);
  add('gm.texBuild', '编译 LaTeX',
      [M.KeyMod.CtrlCmd | M.KeyMod.Alt | M.KeyCode.KeyB],
      () => { ideTexBuild(); }, 7);
  add('gm.runFile', '运行 / 编译并运行',
      [M.KeyMod.CtrlCmd | M.KeyMod.Alt | M.KeyCode.KeyR],
      () => { ideRunCurrent(); }, 8);
  add('gm.formatDoc', '格式化文档',
      [M.KeyMod.Shift | M.KeyMod.Alt | M.KeyCode.KeyF],
      () => { ideFormatDoc(); }, 9);
  add('gm.addAnno', '添加批注',
      [M.KeyMod.CtrlCmd | M.KeyMod.Alt | M.KeyCode.KeyM],
      () => { ideAnnoAdd(); }, 10);
}

/* ---------- AI 变更审阅（Trae 式） ----------
   后端在写文件前用 agent._snap 留了原文；token 由 tool_done 事件带过来。
   同一轮里对同一文件的多次写入合并成一条，diff 取**第一次写入前**的快照，
   因此看到的是整轮累计改动；撤销 / 保留作用于这一条里的全部 token。 */

function ideReviewItemOf(token) {
  const all = (state.ideReview || []).concat(state.ideReviewPending || []);
  return all.find((it) => (it.tokens || []).indexOf(token) >= 0) || null;
}

let ideReviewBusy = false;      // 防止「保留 / 撤销」被连点触发两次
let ideReviewSeq = 0;           // 每次渲染换一组唯一 URI，避免 Monaco 模型重名

function ideReviewCounts(name, args) {
  const lines = (s) => String(s == null ? '' : s).replace(/\r\n/g, '\n').split('\n').length;
  if (!args) return { adds: 0, dels: 0 };
  if (name === 'edit_file') {
    return { adds: lines(args.new), dels: lines(args.old) };
  }
  if (name === 'write_file') {
    return { adds: lines(args.content), dels: 0 };
  }
  if (name === 'apply_worktree') {     // 隔离工作区回写：增删行数由后端算好
    return { adds: Number(args.adds) || 0, dels: Number(args.dels) || 0 };
  }
  return { adds: 0, dels: 0 };
}

function ideReviewAdd(rel, token, name, args, row, sid) {
  if (!rel || !token) return;
  rel = String(rel).replace(/\\/g, '/');
  const owner = sid || state.sessions.current;
  if (!state.ideReviewPending) state.ideReviewPending = [];
  const pending = state.ideReviewPending;
  // 本轮第一次写入：允许提示栏重新自动出现（上一轮点过「稍后处理」不该一直压着）
  if (!pending.some((x) => (x.sid || owner) === owner)) ideTipClosed = false;
  // 合并键带会话：多个会话同时生成时，同路径的写入不能串成一条
  let it = pending.find((x) => x.path === rel && (x.sid || owner) === owner);
  if (!it) {
    it = { path: rel, tokens: [], baseToken: token, name: name || 'edit_file',
           sid: owner, adds: 0, dels: 0 };
    pending.push(it);
  }
  if (it.tokens.indexOf(token) < 0) it.tokens.push(token);
  const c = ideReviewCounts(name, args);
  it.adds += c.adds;
  it.dels += c.dels;
  if (!state.ideDiffs) state.ideDiffs = {};
  state.ideDiffs[rel] = it.baseToken;
  // 同时进审阅队列：提示栏不必等整轮结束，改完一个文件就能「查看变更 / 全部保留」
  if ((state.ideReview || []).indexOf(it) < 0) state.ideReview.push(it);
  ideUpdateDiffBtn();
  // 模型每改完一个文件就把改动声明挂到当前回复末尾，本轮还没结束也能看到
  ideReviewLiveCard(row, owner);
}

/* 流式过程中边改边更新：卡片保存一份快照，后续写入会同步反映到同一张卡上；
   用户中途结算后，已结算的文件仍留在卡片里（状态变成「已保留 / 已撤销」）。 */
function ideReviewLiveCard(row, sid) {
  if (!row || !row._wrap || !row._wrap.isConnected) return;   // 后台会话 / 已卸载的行不建卡
  const owner = sid || state.sessions.current;
  const items = (state.ideReviewPending || []).filter((it) => (it.sid || owner) === owner);
  if (!items.length) return;
  let c = (state.changeCards || []).find((x) => x.row === row);
  if (!c) {
    // 续写：这一行可能已有历史回显卡，先摘掉，避免同一条消息出现两张
    const stale = row._wrap.querySelector('.files-changed');
    if (stale) stale.remove();
    c = { row: row, items: [], el: null, open: true, sid: owner };
    state.changeCards.push(c);
  }
  items.forEach((it) => {
    if ((c.items || []).indexOf(it) < 0) c.items.push(it);
  });
  ideReviewCardRefresh();
  ideReviewLiveTip = true;             // 提示栏同步提前出现（标签改成「文件已修改」）
  ideReviewTipShow();
}

function ideReviewEndTurn(row, quiet, sid) {
  // 只结算本次流所属会话的条目：后台会话同时生成时不能互相吞掉
  const owner = sid || state.sessions.current;
  const all = state.ideReviewPending || [];
  const pending = all.filter((it) => (it.sid || owner) === owner);
  state.ideReviewPending = all.filter((it) => (it.sid || owner) !== owner);
  if (!pending.length) return;
  // 流式期间已经提前入队的条目不要重复入队
  const fresh = pending.filter((it) => (state.ideReview || []).indexOf(it) < 0);
  const queued = pending.filter((it) => (state.ideReview || []).indexOf(it) >= 0);
  const start = queued.length ? state.ideReview.indexOf(queued[0])
    : state.ideReview.length - fresh.length;
  fresh.forEach((it) => state.ideReview.push(it));
  ideUpdateDiffBtn();
  // 回复末尾的文件改动声明：先按行数估算显示，真实增删数到了再刷新一次。
  // 待审阅状态同时交给输入框上方的提示栏，两处入口互不依赖。
  if (row) ideReviewCardBuild(row, pending, owner);
  ideReviewCardRefresh();
  if (!state.ideReview.length) return;
  // 提示栏 / 自动弹 diff 都只服务于当前会话：后台会话收尾时不抢当前界面
  const isCurSid = owner === state.sessions.current;
  if (isCurSid) {
    if (!quiet) ideReviewLiveTip = false;   // 正常收尾：标签回到「任务已完成」
    ideTipClosed = false;                   // 收尾提醒：中途点过「稍后处理」也再出现一次
    ideReviewTipShow();
  }
  // 新一批改动进来：上一批的 AI 摘要已经过时，先收起来
  ['#ide-review-ai', '#review-ai'].forEach((sel) => {
    const b = $(sel);
    if (b) { b.classList.add('hidden'); b.innerHTML = ''; }
  });
  ideReviewPrefetchStats(pending);
  // 只在能展示 diff 的模式里自动打开；大厅 / GAL 里只入队，等用户点「查看变更」。
  // 中途停止（quiet）不抢视图；后台会话的改动也不在别的会话里弹 diff。
  if (!quiet && isCurSid && (state.uiMode === 'ide' || state.uiMode === 'solo')) {
    ideReviewShow(start);
  }
}

/* 用后端算的真实增删数覆盖掉前面的行数估算，保证标签页 / 提示栏 / 聊天记录一致 */
async function ideReviewPrefetchStats(items) {
  await Promise.all((items || []).map(async (it) => {
    if (!it || !it.baseToken) return;
    try {
      const r = await api('/api/sandbox/diff', {
        session_id: it.sid || state.sessions.current,
        undo: it.baseToken, path: it.path, stat: 1,
      });
      if (!r || !r.ok) return;
      it.adds = Number(r.adds) || 0;
      it.dels = Number(r.dels) || 0;
    } catch (e) { /* 统计失败不影响审阅本身 */ }
  }));
  renderIdeTabs();
  ideReviewCardRefresh();
  ideReviewTipRefresh();
}

function ideUpdateDiffBtn() {
  const b = $('#ide-diff-btn');
  const has = (state.ideReview || []).length || (state.ideReviewPending || []).length;
  if (b) b.classList.toggle('hidden', !has);
  // 标签页上的 +N −M 跟着队列走：入队 / 处理完都在这里统一刷新，不另存一张统计表
  renderIdeTabs();
}

function ideReviewRoot(surface) {
  return surface === 'ide' ? $('#ide-diff') : $('#review-modal');
}

function ideReviewEl(surface, id) {
  return $(surface === 'ide' ? '#ide-review-' + id : '#review-' + id);
}

function ideReviewSurfaceForShow() {
  return state.uiMode === 'ide' ? 'ide' : 'modal';
}

function ideReviewDisposeModels(surface) {
  // 先把编辑器与旧模型解绑，再 dispose：否则 Monaco 会报
  // 「TextModel got disposed before DiffEditorWidget model got reset」，
  // 严重时会让下一次渲染拿不到新模型（面板空白）。
  const ed = surface === 'ide' ? state.ideDiffEditor : state.ideReviewEditor;
  if (ed) {
    try { ed.setModel(null); } catch (e) { /* 忽略 */ }
  }
  const list = surface === 'ide' ? state.ideDiffModels : state.ideReviewModels;
  (list || []).forEach((m) => { try { m.dispose(); } catch (e) { /* 忽略 */ } });
  if (surface === 'ide') state.ideDiffModels = null;
  else state.ideReviewModels = null;
}

/* 审阅视图是一个普通只读编辑器，不是 DiffEditor。
   DiffEditor 的 inline 模式只能给「改动行」上色，做不到
   「变更前整块浅红 / 变更后整块浅绿」，所以改成合成文档 + 整行装饰。 */
function ideReviewEditor(surface, M) {
  const opt = {
    automaticLayout: true,
    readOnly: true,
    renderLineHighlight: 'none',
    lineNumbersMinChars: 5,
    glyphMargin: false,
    folding: false,
    minimap: { enabled: false },
    scrollBeyondLastLine: false,
    renderOverviewRuler: false,
    wordWrap: 'off',
    lineNumbers: () => '',
    theme: ideMonacoThemeName(),
  };
  if (surface === 'ide') {
    if (!state.ideDiffEditor) {
      const host = $('#ide-diff-editor');
      if (!host) return null;
      state.ideDiffEditor = M.editor.create(host, opt);
      ideReviewBindGap(state.ideDiffEditor, 'ide');
    }
    return state.ideDiffEditor;
  }
  if (!state.ideReviewEditor) {
    const host = $('#review-diff');
    if (!host) return null;
    state.ideReviewEditor = M.editor.create(host, opt);
    ideReviewBindGap(state.ideReviewEditor, 'modal');
  }
  return state.ideReviewEditor;
}

/* 折叠条：点一下多展开 20 行上下文，重渲染后回到那条改动的位置 */
let ideDiffCtx = 3;
function ideReviewBindGap(ed, surface) {
  ed.onMouseDown((e) => {
    const ln = e.target && e.target.position && e.target.position.lineNumber;
    if (!ln) return;
    const doc = surface === 'ide' ? state.ideDiffDoc : state.ideReviewDoc;
    const row = doc && doc.rows && doc.rows[ln - 1];
    if (!row || row.cls !== 'dv-gap' || row.hunk == null || row.hunk < 0) return;
    ideDiffCtx = Math.min(ideDiffCtx + 20, 200);
    ideReviewRender(state.ideReview[state.ideReviewIdx], surface, row.hunk);
  });
}

/* 把 hunks 拼成一段合成文档：
   每个 hunk 依次是「旧块行」「新块行」，块之间用一行灰字表示折叠了多少行。
   行号按真实文件行号映射：删除行给旧行号，新增行给新行号。 */
function ideDiffDoc(r) {
  const rows = [];
  const hunkAt = [];
  (r.hunks || []).forEach((h, hi) => {
    const gap = Number(h.gap_before) || 0;
    if (gap > 0) rows.push({ text: '⋯ ' + gap + ' 个隐藏的行 ⋯', cls: 'dv-gap', no: '', hunk: hi });
    hunkAt.push(rows.length + 1);
    const o = h.old || [], n = h.new || [];
    const ok = h.old_kinds || [], nk = h.new_kinds || [];
    o.forEach((line, i) => rows.push({
      text: line,
      cls: ok[i] === 'del' ? 'dv-del' : 'dv-ctx-old',
      no: String((Number(h.old_start) || 1) + i),
    }));
    n.forEach((line, i) => rows.push({
      text: line,
      cls: nk[i] === 'ins' ? 'dv-ins' : 'dv-ctx-new',
      no: String((Number(h.new_start) || 1) + i),
    }));
  });
  const tail = Number(r.gap_after) || 0;
  if (tail > 0) rows.push({ text: '⋯ ' + tail + ' 个隐藏的行 ⋯', cls: 'dv-gap', no: '', hunk: -1 });
  if (!rows.length) rows.push({ text: '这次改动没有可显示的内容', cls: 'dv-gap', no: '', hunk: -1 });
  return { rows: rows, text: rows.map((x) => x.text).join('\n'), hunkAt: hunkAt };
}

/* 快照过期：把面板换成说明文字，只留「保留」（文件本身仍是改完后的内容）。
   以前这里直接把条目从队列删掉，用户只看到一句 toast，以为功能坏了。 */
async function ideReviewMarkExpired(it, surface, why) {
  const M = state.ideMonaco || await loadMonaco();
  if (!M) return;
  const ed = ideReviewEditor(surface, M);
  if (!ed) return;
  ideReviewDisposeModels(surface);
  let text = '这条改动的「改动前快照」已经过期，无法再显示 diff，也无法撤销。\n'
           + '文件本身没有被回滚，磁盘上仍是改完后的内容。';
  if (why) text += '\n\n原因：' + why;
  text += '\n\n点「保留」把它从审阅队列里移除即可（不会改动文件）。';
  const model = M.editor.createModel(text, 'plaintext',
    M.Uri.from({ scheme: 'review', path: '/' + it.path,
                 query: 'expired-' + (++ideReviewSeq) }));
  if (surface === 'ide') state.ideDiffModels = [model];
  else state.ideReviewModels = [model];
  M.editor.setTheme(ideMonacoThemeName());
  ed.setModel(model);
  ed.updateOptions({ lineNumbers: () => '' });
  ed.deltaDecorations([], []);
  ideReviewHideHunkBar();
  // 没有快照就没有可撤销的凭据：撤销置灰，保留仍可用
  ['ide-review-undo', 'review-undo'].forEach((id) => {
    const b = $('#' + id);
    if (b) b.disabled = true;
  });
  ['ide-review-keep', 'review-keep'].forEach((id) => {
    const b = $('#' + id);
    if (b) b.disabled = false;
  });
  toast('快照已过期：只能保留，无法查看 diff / 撤销');
}

async function ideReviewRender(it, surface, focusHunk) {
  const list = state.ideReview || [];
  if (!it) return;
  const idxEl = ideReviewEl(surface, 'idx');
  const fileEl = ideReviewEl(surface, 'file');
  if (fileEl) fileEl.textContent = it.path;
  if (idxEl) idxEl.textContent = (state.ideReviewIdx + 1) + ' / ' + list.length;

  let r;
  try {
    r = await api('/api/sandbox/diff', {
      // 用条目自己的会话：审阅队列可能留着上一个会话的文件，
      // 用「当前会话」会把路径解析到另一个沙箱里（表现就是一直读不到）。
      session_id: it.sid || state.sessions.current, undo: it.baseToken, path: it.path,
      context: ideDiffCtx,
    });
  } catch (e) {
    return toast('读取改动失败：' + e);
  }
  if (!r || !r.ok) {
    // 快照真的过期了（超出保留条数 / 已被撤销 / 换过进程）：不再静默把条目删掉，
    // 改成「只能保留」的占位，免得用户以为整个功能坏了（见 NOTE 43.126）。
    it.expired = true;
    await ideReviewMarkExpired(it, surface, (r && r.error) || '');
    return;
  }
  it.expired = false;
  it.newSha = r.new_sha || '';       // 逐段撤销时回传，后端据此判断文件有没有被外部改过
  // 上一条可能是过期占位，这里把按钮恢复成正常可用
  ['ide-review-undo', 'review-undo', 'ide-review-keep', 'review-keep'].forEach((id) => {
    const b = $('#' + id);
    if (b) b.disabled = false;
  });

  const M = state.ideMonaco || await loadMonaco();
  if (!M) return toast('编辑器未加载，无法显示 diff');
  const ed = ideReviewEditor(surface, M);
  if (!ed) return;
  const doc = ideDiffDoc(r);
  if (surface === 'ide') state.ideDiffDoc = doc;
  else state.ideReviewDoc = doc;

  ideReviewDisposeModels(surface);
  const lang = ideLangOf(it.path);
  const tag = (++ideReviewSeq) + '-' + String(it.baseToken || '').slice(-6);
  const model = M.editor.createModel(doc.text, lang,
    M.Uri.from({ scheme: 'review', path: '/' + it.path, query: surface + '-' + tag }));
  if (surface === 'ide') state.ideDiffModels = [model];
  else state.ideReviewModels = [model];
  M.editor.setTheme(ideMonacoThemeName());
  ed.setModel(model);

  // 行号列只放真实文件行号；折叠条那一行留空
  ed.updateOptions({ lineNumbers: (n) => (doc.rows[n - 1] || {}).no || '' });
  const decos = [];
  doc.rows.forEach((row, i) => {
    if (!row.cls) return;
    decos.push({
      range: { startLineNumber: i + 1, startColumn: 1, endLineNumber: i + 1, endColumn: 1 },
      options: { isWholeLine: true, className: row.cls },
    });
  });
  ed.deltaDecorations([], decos);

  // 真实增删数写回条目：标签页、提示栏、聊天记录都从这里取，不再用行数估算
  it.adds = Number(r.adds) || 0;
  it.dels = Number(r.dels) || 0;
  renderIdeTabs();
  ideReviewCardRefresh();
  ideReviewTipRefresh();

  ed.setScrollTop(0);
  if (focusHunk != null && doc.hunkAt[focusHunk]) {
    ed.revealLineInCenter(doc.hunkAt[focusHunk]);
  }
  ideReviewHunkBar(it, surface, doc);
  try { ed.focus(); } catch (e) { /* 忽略 */ }
}

/* ---- 逐段审阅（hunk 级） ----
   工具条放在编辑器上方的独立一行，不用 Monaco contentWidget：
   widget 是浮层、不占位，两个改动段相距两三行时按钮会互相盖住（实测重叠 12px）。
   一行 chip 永远不重叠，也能一眼看到「一共几段」。 */
function ideReviewHunkBar(it, surface, doc) {
  const bar = $(surface === 'ide' ? '#ide-hunk-bar' : '#review-hunk-bar');
  if (!bar) return;
  const kept = it.kept || {};
  const items = [];
  (doc.hunkAt || []).forEach((line, hi) => {
    if (!line || kept[hi]) return;
    items.push({ hi: hi, line: line, n: items.length + 1 });
  });
  bar.innerHTML = '';
  bar.classList.toggle('hidden', !items.length);
  if (!items.length) return;
  const label = document.createElement('span');
  label.className = 'hunk-bar-label';
  label.textContent = '改动段';
  bar.appendChild(label);
  items.forEach((x) => {
    const chip = document.createElement('span');
    chip.className = 'hunk-chip';
    const name = document.createElement('button');
    name.type = 'button';
    name.className = 'hunk-chip-name';
    name.textContent = '第 ' + x.n + ' 段';
    name.title = '定位到这一段';
    name.onclick = () => {
      const ed = surface === 'ide' ? state.ideDiffEditor : state.ideReviewEditor;
      if (!ed) return;
      ed.revealLineInCenter(x.line);
      ed.setPosition({ lineNumber: x.line, column: 1 });
    };
    const keep = document.createElement('button');
    keep.type = 'button';
    keep.className = 'hunk-keep';
    keep.textContent = '保留';
    keep.onclick = () => {
      it.kept = it.kept || {};
      it.kept[x.hi] = true;
      chip.remove();
      if (!bar.querySelector('.hunk-chip')) bar.classList.add('hidden');
    };
    const und = document.createElement('button');
    und.type = 'button';
    und.className = 'hunk-undo';
    und.textContent = '撤销';
    und.onclick = () => ideRevertHunk(it, surface, x.hi);
    chip.appendChild(name);
    chip.appendChild(keep);
    chip.appendChild(und);
    bar.appendChild(chip);
  });
}

function ideReviewHideHunkBar() {
  ['#ide-hunk-bar', '#review-hunk-bar'].forEach((sel) => {
    const el = $(sel);
    if (el) { el.innerHTML = ''; el.classList.add('hidden'); }
  });
}

/* 撤销单独一段：后端按「同一份 old 快照 + 同样 context」重算 hunk，
   只把选中的那段恢复成旧内容；文件被外部改过时后端会拒绝并给原因。 */
async function ideRevertHunk(it, surface, hi) {
  if (ideReviewBusy || !it) return;
  ideReviewBusy = true;
  try {
    const r = await api('/api/sandbox/revert_hunks', {
      session_id: it.sid || state.sessions.current,
      undo: it.baseToken, path: it.path, hunks: [hi], context: ideDiffCtx,
      sha: it.newSha || '',
    });
    if (!r || !r.ok) return toast((r && r.error) || '撤销这一段失败');
    it.kept = {};                          // 段下标会变，保留标记作废
    toast(r.changed ? '已撤销这一段，其余改动保留' : '这一段没有产生改动');
    if (state.ideOpen === it.path) await ideReloadFiles([it.path]);
    await ideReviewRender(it, surface);
    if ((Number(it.adds) || 0) + (Number(it.dels) || 0) === 0) {
      ideReviewDrop(it);
    } else {
      ideIntelSchedule(state.ideOpen);     // 文件变了，顺带刷新诊断
    }
  } finally {
    ideReviewBusy = false;
  }
}

/* 某条审阅已经没有任何改动：从队列里移除，并显示下一条 */
function ideReviewDrop(it) {
  const list = state.ideReview || [];
  const i = list.indexOf(it);
  if (i >= 0) list.splice(i, 1);
  // 改动声明里的对应行也去掉（整条已无增删，留着只会显示 0 / 0）
  (state.changeCards || []).forEach((c) => {
    c.items = (c.items || []).filter((x) => x !== it);
  });
  ideUpdateDiffBtn();
  ideReviewCardRefresh();
  ideReviewTipRefresh();
  if (!list.length) { ideReviewClose(); return; }
  state.ideReviewIdx = Math.min(state.ideReviewIdx, list.length - 1);
  ideReviewRender(list[state.ideReviewIdx], state.ideReviewSurface || 'ide');
}

async function ideReviewShow(idx) {
  const list = state.ideReview || [];
  if (!list.length) return ideReviewClose();
  state.ideReviewIdx = Math.max(0, Math.min(Number(idx) || 0, list.length - 1));
  const it = list[state.ideReviewIdx];
  const surface = ideReviewSurfaceForShow();
  state.ideReviewSurface = surface;

  if (surface === 'ide') {
    ideShowView('diff');
  } else {
    const modal = $('#review-modal');
    if (modal) {
      if (typeof showWithAnim === 'function') showWithAnim(modal);
      else modal.classList.remove('hidden');
    }
    // 让快捷键有落点：焦点进弹窗，Ctrl+Enter / Ctrl+Backspace 才能生效
    setTimeout(() => { const k = $('#review-keep'); if (k) k.focus(); }, 30);
  }
  const prev = ideReviewEl(surface, 'prev');
  const next = ideReviewEl(surface, 'next');
  if (prev) prev.disabled = state.ideReviewIdx <= 0;
  if (next) next.disabled = state.ideReviewIdx >= list.length - 1;
  ideDiffCtx = 3;                    // 换文件回到默认上下文，避免上次展开过的一直带着
  await ideReviewRender(it, surface);
}

function ideReviewClose(opts) {
  const surface = state.ideReviewSurface;
  state.ideReviewSurface = '';
  ideReviewHideHunkBar();
  // 摘要只对「当前这批改动」有意义，关面板时一起清掉，避免下次打开看到旧结论
  const aiBox = $(surface === 'modal' ? '#review-ai' : '#ide-review-ai');
  if (aiBox) { aiBox.classList.add('hidden'); aiBox.innerHTML = ''; }
  if (surface === 'modal') {
    const modal = $('#review-modal');
    if (modal) {
      if (typeof hideWithAnim === 'function') hideWithAnim(modal);
      else modal.classList.add('hidden');
    }
  } else if (state.uiMode === 'ide' && !(opts && opts.keepView)) {
    ideShowView('editor');
  }
}

/* ---------- AI 摘要（审阅队列 / Git 审查共用同一套渲染） ----------
   后端只返回 {summary, risks, flow}；这里拼成 Markdown，交给聊天那套
   safeRenderMarkdown + enhanceRichMedia 渲染（```mermaid 会自动变成流程图）。 */
let ideReviewAiBusy = false;

function ideReviewAiMarkdown(r) {
  const md = ['**摘要**：' + ((r && r.summary) || '（模型没有返回摘要）')];
  const risks = (r && r.risks) || [];
  if (risks.length) {
    md.push('**风险 / 待确认**');
    risks.forEach((x) => md.push('- ' + x));
  }
  if (r && r.flow) md.push('```mermaid\n' + r.flow + '\n```');
  const src = md.join('\n\n');
  const html = (typeof safeRenderMarkdown === 'function')
    ? safeRenderMarkdown(src) : escapeHtml(src);
  const meta = [];
  if (r && r.label) meta.push(r.label);
  if (r && r.files && r.files.length) meta.push(r.files.length + ' 个文件');
  if (r && r.skipped) meta.push('跳过 ' + r.skipped + ' 个');
  if (r && r.truncated) meta.push('diff 已截断');
  return { html: html, meta: meta.join(' · ') };
}

/* box 是摘要容器；bodyHtml 已经转义 / 渲染过，这里只负责外壳与收起 */
function ideReviewAiDraw(box, bodyHtml, metaText, cls) {
  if (!box) return;
  box.innerHTML =
    '<div class="ide-review-ai-head"><span class="ide-review-ai-title">AI 摘要</span>' +
    '<span class="ide-review-ai-meta">' + escapeHtml(metaText || '') + '</span>' +
    '<button type="button" class="ico-btn" data-ai-close title="收起">✕</button></div>' +
    '<div class="' + (cls || 'ide-review-ai-load') + '">' + bodyHtml + '</div>';
  box.classList.remove('hidden');
  const x = box.querySelector('[data-ai-close]');
  if (x) x.onclick = () => { box.classList.add('hidden'); box.innerHTML = ''; };
  if (cls && cls.indexOf('work-preview-md') >= 0 && window.enhanceRichMedia) {
    window.enhanceRichMedia(box);
  }
}

/* 把整个审阅队列一起交给模型：比只看当前文件更容易看出跨文件的相互影响。
   其它会话的文件不在当前沙箱里，直接跳过并计入「跳过 N 个」。 */
async function ideReviewAiSummary(surface) {
  if (ideReviewAiBusy) return;
  const box = $(surface === 'ide' ? '#ide-review-ai' : '#review-ai');
  const btn = $(surface === 'ide' ? '#ide-review-ai-btn' : '#review-ai-btn');
  const list = state.ideReview || [];
  const cur = state.sessions.current;
  const items = list.filter((it) => !it.expired && it.baseToken &&
    (!it.sid || it.sid === cur));
  if (!items.length) return toast('没有可审查的改动（快照可能已过期）');
  ideReviewAiBusy = true;
  if (btn) btn.disabled = true;
  ideReviewAiDraw(box, '正在生成 AI 摘要…', '', 'ide-review-ai-load');
  try {
    const r = await api('/api/ide/review', {
      session_id: cur, scope: 'ai',
      items: items.map((it) => ({ path: it.path, undo: it.baseToken })),
    });
    if (!r || !r.ok) throw new Error((r && r.error) || '生成失败');
    r.skipped = (list.length - items.length) + (Number(r.skipped) || 0);
    const m = ideReviewAiMarkdown(r);
    ideReviewAiDraw(box, m.html, m.meta, 'ide-review-ai-body work-preview-md');
  } catch (e) {
    ideReviewAiDraw(box, escapeHtml(e.message || String(e)), '',
                    'ide-review-ai-err');
  } finally {
    ideReviewAiBusy = false;
    if (btn) btn.disabled = false;
  }
}

function ideReviewSyncCards(tokens, label) {
  const map = state.reviewCardBtns || {};
  (tokens || []).forEach((t) => {
    (map[t] || []).forEach((b) => {
      b.disabled = true;
      b.textContent = b.dataset && b.dataset.role === 'view' ? '已处理' : label;
    });
  });
}

async function ideReviewSettle(action) {
  if (ideReviewBusy) return;
  const list = state.ideReview || [];
  const it = list[state.ideReviewIdx];
  if (!it) return;
  const tokens = (it.tokens || []).slice();
  if (!tokens.length) return;
  const accept = action === 'accept';
  // 快照已过期：服务端没有可撤销 / 可接纳的凭据。撤销做不到，保留 = 本地移出队列。
  if (it.expired) {
    if (!accept) return toast('快照已过期，无法撤销；文件仍是改完后的内容');
    list.splice(state.ideReviewIdx, 1);
    ideUpdateDiffBtn();
    if (state.ideDiffs) delete state.ideDiffs[it.path];
    if (!list.length) {
      ideReviewClose();
      return toast('已移出审阅队列（快照已过期，文件未改动）');
    }
    toast('已移出审阅队列（快照已过期，文件未改动）');
    return ideReviewShow(Math.min(state.ideReviewIdx, list.length - 1));
  }
  ideReviewBusy = true;
  try {
    let r;
    try {
      // 带上 session_id：进程重启后后端要先把该会话的磁盘快照并回内存
      r = await api(accept ? '/api/agent/accept' : '/api/agent/undo',
                    { session_id: it.sid || state.sessions.current, tokens: tokens });
    } catch (e) {
      return toast((accept ? '保留' : '撤销') + '失败：' + e);
    }
    if (!r || !r.ok) {
      return toast((r && r.error)
        || (accept ? '保留失败：快照可能已过期' : '撤销失败：快照可能已过期'));
    }
    tokens.forEach((t) => { state.ideSettledTokens[t] = accept ? 'accepted' : 'undone'; });
    ideReviewSyncCards(tokens, accept ? '已保留' : '已撤回');
    ideReviewCardRefresh();
    ideReviewTipRefresh();
    if (state.ideDiffs) delete state.ideDiffs[it.path];
    list.splice(state.ideReviewIdx, 1);
    ideUpdateDiffBtn();
    if (!list.length) {
      ideReviewClose();
      toast(accept ? '已保留这次改动' : '已撤销这次改动');
      return;
    }
    await ideReviewShow(Math.min(state.ideReviewIdx, list.length - 1));
  } finally {
    ideReviewBusy = false;
  }
}

function ideReviewStep(delta) {
  const list = state.ideReview || [];
  if (list.length < 2) return;
  const next = Math.max(0, Math.min(state.ideReviewIdx + delta, list.length - 1));
  if (next !== state.ideReviewIdx) ideReviewShow(next);
}

/* ---------- 聊天流里的「文件改动声明」（对齐 Trae 的 files changed bar） ---------- */

function ideReviewItemStatus(it) {
  const tokens = it.tokens || [];
  // 没有 token = 这些写入已经被逐条撤回（reviewForgetToken 会摘掉条目）
  if (!tokens.length) return 'undone';
  const states = tokens.map((t) => state.ideSettledTokens[t]);
  if (states.every((s) => s === 'accepted')) return 'accepted';
  if (states.every((s) => s === 'undone')) return 'undone';
  return 'pending';
}

const FC_ICON = '<svg viewBox="0 0 16 16" width="13" height="13" fill="none" ' +
  'stroke="currentColor" stroke-width="1.25" stroke-linejoin="round">' +
  '<path d="M4 1.9h4.6L12.1 5.4v8.7H4z"/><path d="M8.6 1.9v3.5h3.5"/></svg>';

function ideChangedBase(p) {
  const s = String(p || '').replace(/\\/g, '/');
  const i = s.lastIndexOf('/');
  return i < 0 ? s : s.slice(i + 1);
}

/* 点文件名跳到 IDE 里对应的文件内容（不是 diff）。
   条目属于别的会话时，当前沙箱里没有这个文件，先提示切回原会话。 */
function ideChangedOpen(it) {
  if (!it || !it.path) return;
  if (it.sid && state.sessions.current && it.sid !== state.sessions.current) {
    return toast('这个文件属于之前的会话，切回该会话后才能打开');
  }
  if (typeof openReference === 'function') return openReference({ path: it.path });
  if (typeof setUiMode !== 'function' || typeof openIdeFile !== 'function') {
    return toast('IDE 还没有准备好');
  }
  setUiMode('ide').then(() => openIdeFile(it.path))
    .catch((e) => toast('打开失败：' + (e && e.message ? e.message : e), 'error'));
}

/* 审阅界面顶部的「在 IDE 查看」：收起 diff，切到 IDE 打开当前这个文件的正文。
   只收起界面，不动审阅队列（撤销 / 保留仍然可用）。 */
function ideReviewOpenInIde() {
  const it = (state.ideReview || [])[state.ideReviewIdx];
  if (!it || !it.path) return;
  if (it.sid && state.sessions.current && it.sid !== state.sessions.current) {
    return toast('这个文件属于之前的会话，切回该会话后才能打开');
  }
  ideReviewClose();
  ideChangedOpen(it);
}

/* 卡片挂在「正文之后、操作条/等待提示之前」：回合结束时正文 / 过程折叠会重排一次，
   每次刷新都重新定位，避免卡片被排到过程区上面或落到「正在生成」提示下面。 */
function ideChangedPlace(c) {
  const wrap = c && c.row && c.row._wrap;
  if (!wrap || !c.el) return;
  let anchor = null;
  for (let i = 0; i < wrap.children.length; i++) {
    const n = wrap.children[i];
    if (n.classList.contains('msg-actions') || n.classList.contains('live-bar')
        || n.classList.contains('end-note') || n.classList.contains('typing')) {
      anchor = n;
      break;
    }
  }
  if (anchor) wrap.insertBefore(c.el, anchor);
  else wrap.appendChild(c.el);
}

/* 单行文件（live 审阅卡与历史回显卡共用同一套排版 / 点击行为）。 */
function ideChangedRowEl(g) {
  const row = document.createElement('button');
  row.type = 'button';
  row.className = 'fc-file';
  row.title = g.path;
  row.innerHTML =
    '<span class="fc-ico">' + FC_ICON + '</span>' +
    '<span class="fc-name"></span>' +
    '<span class="fc-path"></span>' +
    '<span class="fc-file-stat"><b class="add"></b> <b class="del"></b></span>';
  row.querySelector('.fc-name').textContent = ideChangedBase(g.path);
  row.querySelector('.fc-path').textContent = '.\\' + String(g.path).replace(/\//g, '\\');
  row.querySelector('.fc-file-stat .add').textContent = '+' + g.adds;
  row.querySelector('.fc-file-stat .del').textContent = '−' + g.dels;
  row.onclick = () => ideChangedOpen(g);
  return row;
}

/* 从落库的 tools 还原改动列表：只有成功的写入才有 undo 令牌，失败的编辑不算。 */
function ideChangedGroupsFromTools(tools) {
  const groups = [];
  (tools || []).forEach((t) => {
    if (!t || !t.write || !t.undo) return;
    const name = String(t.tool || '');
    if (name !== 'write_file' && name !== 'edit_file' && name !== 'apply_worktree') return;
    const a = t.args || {};
    const path = String(a.path || '').replace(/\\/g, '/');
    if (!path) return;
    const c = ideReviewCounts(name, a);
    let g = groups.find((x) => x.path === path);
    if (!g) {
      g = { path: path, sid: '', adds: 0, dels: 0 };
      groups.push(g);
    }
    g.adds += c.adds;
    g.dels += c.dels;
  });
  return groups;
}

/* 历史消息末尾的「N 个文件已更改」：卡片以前只在流式 tool_done 时建在内存里，
   刷新 / 切会话 / 重启后整条就没了；这里用落库的 tools 重建一份（默认收起）。 */
function ideChangedHistoryCard(tools) {
  const groups = ideChangedGroupsFromTools(tools);
  if (!groups.length) return null;
  const el = document.createElement('div');
  el.className = 'files-changed collapsed';
  el._open = false;
  const head = document.createElement('div');
  head.className = 'fc-head';
  head.setAttribute('role', 'button');
  head.setAttribute('aria-expanded', 'false');
  head.tabIndex = 0;
  head.innerHTML =
    '<span class="fc-ico fc-ico-head">' + FC_ICON + '</span>' +
    '<span class="fc-title"></span>' +
    '<span class="fc-caret">⌄</span>' +
    '<span class="fc-status"></span>' +
    '<span class="fc-stat"><b class="add"></b> <b class="del"></b></span>' +
    '<button type="button" class="fc-open" title="在 IDE 中打开">↗</button>';
  head.querySelector('.fc-title').textContent = groups.length + ' 个文件已更改';
  const adds = groups.reduce((n, g) => n + g.adds, 0);
  const dels = groups.reduce((n, g) => n + g.dels, 0);
  head.querySelector('.fc-stat .add').textContent = '+' + adds;
  head.querySelector('.fc-stat .del').textContent = '−' + dels;
  head.querySelector('.fc-open').onclick = (e) => {
    e.stopPropagation();
    ideChangedOpen(groups[0]);
  };
  const list = document.createElement('div');
  list.className = 'fc-list';
  groups.forEach((g) => list.appendChild(ideChangedRowEl(g)));
  el.appendChild(head);
  el.appendChild(list);
  const setOpen = (next) => {
    el._open = next;
    const caretEl = head.querySelector('.fc-caret');
    if (caretEl) caretEl.textContent = next ? '⌃' : '⌄';
    head.setAttribute('aria-expanded', next ? 'true' : 'false');
    foldTransition(el, list, next, { cls: 'collapsed' });
  };
  head.onclick = () => setOpen(!foldIsOpen(el, 'collapsed'));
  head.onkeydown = (e) => {
    if (e.target !== head) return;
    if (e.key !== 'Enter' && e.key !== ' ') return;
    e.preventDefault();
    setOpen(!foldIsOpen(el, 'collapsed'));
  };
  return el;
}

/* 挂到一条历史 AI 消息的末尾（正文之后、操作条之前）。 */
function ideChangedHistoryAttach(wrap, tools) {
  if (!wrap || !wrap.querySelector) return;
  if (wrap.querySelector('.files-changed')) return;     // 已有 live 卡 / 已挂过
  const el = ideChangedHistoryCard(tools);
  if (!el) return;
  let anchor = null;
  for (let i = 0; i < wrap.children.length; i++) {
    const n = wrap.children[i];
    if (n.classList.contains('msg-actions') || n.classList.contains('live-bar')
        || n.classList.contains('end-note') || n.classList.contains('typing')) {
      anchor = n;
      break;
    }
  }
  if (anchor) wrap.insertBefore(el, anchor);
  else wrap.appendChild(el);
}

function ideReviewCardBuild(row, items, sid) {
  if (!row || !items || !items.length) return;
  // 流式期间已经挂过同一张卡：合并条目，保留折叠状态和 DOM，避免闪一下
  const ex = (state.changeCards || []).find((c) => c.row === row);
  if (ex) {
    items.forEach((it) => {
      if ((ex.items || []).indexOf(it) < 0) ex.items.push(it);
    });
    return;
  }
  // 续写：这一行可能已有历史回显卡，先摘掉，避免同一条消息出现两张
  const stale = row._wrap && row._wrap.querySelector
    && row._wrap.querySelector('.files-changed');
  if (stale) stale.remove();
  state.changeCards.push({
    row: row, items: items.slice(), el: null, open: true,
    sid: sid || state.sessions.current,
  });
}

/* 一轮结束时把「文件改动声明」挂到回复末尾：不区分审批模式，也不等审阅结果。
   待处理时也能展开看每个文件、点进去定位；处理完之后补一个状态标记。 */
function ideReviewCardRefresh() {
  const alive = [];
  (state.changeCards || []).forEach((c) => {
    if (!c.row || !c.row._wrap) return;              // 行已彻底销毁
    const items = (c.items || []).filter((it) => it && it.path);
    if (!items.length) {                             // 条目全被撤销 / 丢弃：卡片本身也摘掉
      if (c.el && c.el.parentNode) c.el.remove();
      return;
    }
    if (!c.row._wrap.isConnected) {
      // 会话还在生成时先留着，切回来继续更新；已结束会话的旧消息卡直接丢，
      // 否则会一直攥着卸载的 DOM 子树，长期运行的内存只会越积越多。
      if (c.sid && state.streams && state.streams[c.sid]) { alive.push(c); return; }
      if (c.el && c.el.parentNode) c.el.remove();
      return;
    }
    alive.push(c);
    if (!c.el) {
      c.el = document.createElement('div');
      c.el.className = 'files-changed';
      c.el._open = c.open !== false;
    }
    const open = c.el._open !== false;
    c.open = open;
    // 同一文件在「中途结算后又被改」时会有两批条目：按路径并成一行，计数累加
    const groups = [];
    items.forEach((it) => {
      let g = groups.find((x) => x.path === it.path);
      if (!g) {
        g = { path: it.path, sid: it.sid, adds: 0, dels: 0, states: [] };
        groups.push(g);
      }
      g.adds += Number(it.adds) || 0;
      g.dels += Number(it.dels) || 0;
      g.states.push(ideReviewItemStatus(it));
    });
    const adds = groups.reduce((n, g) => n + g.adds, 0);
    const dels = groups.reduce((n, g) => n + g.dels, 0);
    const states = groups.reduce((a, g) => a.concat(g.states), []);
    const done = states.every((s) => s !== 'pending');
    const status = !done ? ''
      : states.every((s) => s === 'accepted') ? '已保留'
      : states.every((s) => s === 'undone') ? '已撤销' : '已处理';

    // 重绘前停掉可能还在跑的高度动画：它持有的是旧的 list 节点
    if (c.el._foldAnim) {
      try { c.el._foldAnim.cancel(); } catch (e) { /* 忽略 */ }
      c.el._foldAnim = null;
    }
    c.el.innerHTML = '';
    const head = document.createElement('div');
    head.className = 'fc-head';
    head.setAttribute('role', 'button');
    head.setAttribute('aria-expanded', open ? 'true' : 'false');
    head.tabIndex = 0;
    head.innerHTML =
      '<span class="fc-ico fc-ico-head">' + FC_ICON + '</span>' +
      '<span class="fc-title"></span>' +
      '<span class="fc-caret">' + (open ? '⌃' : '⌄') + '</span>' +
      '<span class="fc-status"></span>' +
      '<span class="fc-stat"><b class="add"></b> <b class="del"></b></span>' +
      '<button type="button" class="fc-open" title="在 IDE 中打开">↗</button>';
    head.querySelector('.fc-title').textContent = groups.length + ' 个文件已更改';
    const st = head.querySelector('.fc-status');
    st.textContent = status;
    st.classList.toggle('undone', status === '已撤销');
    head.querySelector('.fc-stat .add').textContent = '+' + adds;
    head.querySelector('.fc-stat .del').textContent = '−' + dels;
    const jump = head.querySelector('.fc-open');
    jump.onclick = (e) => { e.stopPropagation(); ideChangedOpen(groups[0]); };

    const list = document.createElement('div');
    list.className = 'fc-list';
    const setOpen = (next) => {
      c.el._open = next;
      c.open = next;
      const caretEl = head.querySelector('.fc-caret');
      if (caretEl) caretEl.textContent = next ? '⌃' : '⌄';
      head.setAttribute('aria-expanded', next ? 'true' : 'false');
      foldTransition(c.el, list, next, { cls: 'collapsed' });
    };
    head.onclick = () => setOpen(!foldIsOpen(c.el, 'collapsed'));
    head.onkeydown = (e) => {
      if (e.target !== head) return;            // 焦点在内层 ↗ 按钮时别重复响应
      if (e.key !== 'Enter' && e.key !== ' ') return;
      e.preventDefault();
      setOpen(!foldIsOpen(c.el, 'collapsed'));
    };
    groups.forEach((g) => list.appendChild(ideChangedRowEl(g)));

    c.el.appendChild(head);
    c.el.appendChild(list);
    c.el.classList.toggle('collapsed', !open);   // 折叠态由根节点统一表达
    ideChangedPlace(c);
  });
  state.changeCards = alive;
}

/* ---------- 输入框上方的审阅提示栏 ----------
   一旦有改完的文件就出现（不再等整轮结束）；处理完或者点 ✕ 就收起。
   左侧两个图标分别展开「任务项」和「待审查文件」，右侧是本轮的批量操作。 */
const IDE_PLAN_MARK = { done: '✓', doing: '●', pending: '○', blocked: '!' };
let ideTipSid = '';
let ideTipPanel = '';
let ideReviewLiveTip = false;      // true：本轮还在生成，提示栏文案用「文件已修改」
let ideTipClosed = false;          // 用户点过「稍后处理」：本轮内不再自动弹出

function ideReviewTipItems() {
  // pending 里的条目可能已经提前进 ideReview，按对象去重；
  // 别的会话的文件不在当前输入框上方提示（切会话后由刷新统一收起）。
  const cur = state.sessions.current;
  const out = [];
  (state.ideReview || []).concat(state.ideReviewPending || []).forEach((it) => {
    if (out.indexOf(it) >= 0) return;
    if (it.sid && cur && it.sid !== cur) return;
    if (ideReviewItemStatus(it) === 'pending') out.push(it);
  });
  return out;
}

function ideReviewTipShow() {
  if (ideTipClosed) return;            // 用户点了「稍后处理」
  if (!ideReviewTipItems().length) return ideReviewTipHide();
  ideTipSid = state.sessions.current;
  ideReviewTipRefresh();
}

function ideReviewTipHide() {
  const bar = $('#review-tip');
  if (bar) bar.classList.add('hidden');
  ideReviewTipFill('');
}

function ideReviewTipRefresh() {
  const bar = $('#review-tip');
  if (!bar) return;
  // 换了会话就不再占着输入框上方：那些改动属于上一轮对话
  if (ideTipSid && ideTipSid !== state.sessions.current) return ideReviewTipHide();
  const items = ideReviewTipItems();
  if (!items.length) return ideReviewTipHide();
  bar.classList.remove('hidden');
  const lab = bar.querySelector('.rt-label');
  if (lab) {
    lab.textContent = ideReviewLiveTip
      ? '文件已修改，需要审查' : '任务已完成，文件需要审查';
  }
  const n = $('#rt-files-n');
  if (n) n.textContent = String(items.length);
  if (ideTipPanel) ideReviewTipFill(ideTipPanel);
}

function ideReviewTipPanel(which) {
  ideReviewTipFill(ideTipPanel === which ? '' : which);
}

function ideReviewTipFill(which) {
  const box = $('#rt-panel');
  if (!box) return;
  ideTipPanel = which || '';
  box.innerHTML = '';
  if (!which) {
    box.classList.add('hidden');
    return;
  }
  if (which === 'tasks') {
    const steps = state.lastPlan || [];
    if (!steps.length) {
      box.innerHTML = '<div class="rt-empty">本轮没有执行计划</div>';
    } else {
      const cap = document.createElement('div');
      cap.className = 'rt-cap';
      const done = steps.filter((s) => s.status === 'done').length;
      cap.textContent = '任务项 ' + done + ' / ' + steps.length;
      box.appendChild(cap);
      steps.forEach((s) => {
        const row = document.createElement('div');
        row.className = 'rt-step ' + (s.status || 'pending');
        row.innerHTML = '<span class="rt-mark">' +
          (IDE_PLAN_MARK[s.status] || IDE_PLAN_MARK.pending) + '</span>' +
          '<span class="rt-step-text"></span>';
        row.querySelector('.rt-step-text').textContent = s.text || '';
        box.appendChild(row);
      });
    }
  } else {
    const items = ideReviewTipItems();
    if (!items.length) {
      box.innerHTML = '<div class="rt-empty">没有待审查的文件</div>';
    } else {
      items.forEach((it) => {
        const row = document.createElement('button');
        row.type = 'button';
        row.className = 'rt-file';
        row.title = it.path || '';
        row.innerHTML = '<span class="rt-file-ico">▤</span>' +
          '<span class="rt-file-name"></span>' +
          '<span class="rt-file-stat"></span>';
        row.querySelector('.rt-file-name').textContent = (it.path || '').split('/').pop();
        row.querySelector('.rt-file-stat').innerHTML =
          '<b class="add">+' + (it.adds || 0) + '</b> <b class="del">−' + (it.dels || 0) + '</b>';
        row.onclick = () => { ideReviewTipFill(''); reviewOpenFor(it.path, it.baseToken); };
        box.appendChild(row);
      });
    }
  }
  box.classList.remove('hidden');
}

function ideReviewTipBind() {
  const bar = $('#review-tip');
  if (!bar) return;
  const on = (sel, fn) => { const el = bar.querySelector(sel); if (el) el.onclick = fn; };
  on('#rt-tasks', () => ideReviewTipPanel('tasks'));
  on('#rt-files', () => ideReviewTipPanel('files'));
  on('#rt-close', () => { ideTipClosed = true; ideReviewTipHide(); });
  on('#rt-keep', () => {
    const items = ideReviewTipItems();
    if (items.length) ideReviewSettleAll('accept', items);
  });
  on('#rt-undo', () => {
    const items = ideReviewTipItems();
    if (items.length) ideReviewSettleAll('undo', items);
  });
  // 点面板外面收起。用 composedPath：点文件行会重画面板，节点已脱离 DOM，contains 判不出来
  document.addEventListener('mousedown', (e) => {
    if (!ideTipPanel) return;
    const path = e.composedPath ? e.composedPath() : [];
    if (path.indexOf(bar) >= 0) return;
    ideReviewTipFill('');
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && ideTipPanel) ideReviewTipFill('');
  });
}

async function ideReviewSettleAll(action, items) {
  if (ideReviewBusy) return;
  const tokens = [];
  (items || []).forEach((it) => (it.tokens || []).forEach((t) => {
    if (!state.ideSettledTokens[t]) tokens.push(t);
  }));
  if (!tokens.length) return;
  ideReviewBusy = true;
  const accept = action === 'accept';
  try {
    let r;
    try {
      r = await api(accept ? '/api/agent/accept' : '/api/agent/undo', { tokens: tokens });
    } catch (e) {
      return toast((accept ? '保留' : '撤销') + '失败：' + e);
    }
    if (!r || !r.ok) {
      return toast((r && r.error) || (accept ? '保留失败' : '撤销失败'));
    }
    tokens.forEach((t) => { state.ideSettledTokens[t] = accept ? 'accepted' : 'undone'; });
    ideReviewSyncCards(tokens, accept ? '已保留' : '已撤回');
    (items || []).forEach((it) => {
      // 提示栏可能提前出现，条目还在 pending 里，两处都要摘掉
      [state.ideReview, state.ideReviewPending].forEach((arr) => {
        const i = (arr || []).indexOf(it);
        if (i >= 0) arr.splice(i, 1);
      });
      if (state.ideDiffs) delete state.ideDiffs[it.path];
    });
    ideUpdateDiffBtn();
    ideReviewCardRefresh();
    ideReviewTipRefresh();
    // 全部保留 / 全部撤销只做结算，不带着视图跳走：
    // 不打开下一个文件的 diff，也不把 IDE 从 diff 切回编辑器。
    if (!state.ideReview.length) ideReviewClose({ keepView: true });
    toast(accept ? '已保留本轮全部改动' : '已撤销本轮全部改动');
  } finally {
    ideReviewBusy = false;
  }
}

// 聊天卡片单独撤回了一条写入：把它从审阅队列里摘掉（整条没有 token 了就删条目）
function reviewForgetToken(token) {
  const lists = [state.ideReviewPending || [], state.ideReview || []];
  let currentGone = false;
  lists.forEach((arr) => {
    for (let i = arr.length - 1; i >= 0; i--) {
      const it = arr[i];
      const at = (it.tokens || []).indexOf(token);
      if (at < 0) continue;
      it.tokens.splice(at, 1);
      if (it.tokens.length) continue;
      if (arr === state.ideReview && i === state.ideReviewIdx && state.ideReviewSurface) {
        currentGone = true;
      }
      arr.splice(i, 1);
    }
  });
  ideUpdateDiffBtn();
  ideReviewCardRefresh();
  ideReviewTipRefresh();
  if (!currentGone) return;
  if ((state.ideReview || []).length) {
    ideReviewShow(Math.min(state.ideReviewIdx, state.ideReview.length - 1));
  } else {
    ideReviewClose();
  }
}

// 聊天卡片上的「查看变更」：优先打开它所属的整组（同一轮同一文件）
function reviewOpenFor(path, token) {
  if (!token) return;
  if (state.ideSettledTokens && state.ideSettledTokens[token]) {
    return toast('这次改动已经处理过了');
  }
  let it = ideReviewItemOf(token);
  if (!it) {
    it = { path: String(path || '').replace(/\\/g, '/'), tokens: [token],
           baseToken: token, name: 'edit_file', sid: state.sessions.current };
  } else {
    const pi = (state.ideReviewPending || []).indexOf(it);
    if (pi >= 0) state.ideReviewPending.splice(pi, 1);
  }
  if ((state.ideReview || []).indexOf(it) < 0) state.ideReview.push(it);
  ideUpdateDiffBtn();
  ideReviewShow(state.ideReview.indexOf(it));
}

async function ideShowDiff(rel) {
  if (!(state.ideReview || []).length) {
    const token = rel && state.ideDiffs && state.ideDiffs[rel];
    if (!token) return toast('没有可对比的改动');
    state.ideReview = [{ path: rel, tokens: [token], baseToken: token,
                         name: 'edit_file', sid: state.sessions.current }];
  }
  let idx = rel ? state.ideReview.findIndex((x) => x.path === rel) : state.ideReviewIdx;
  if (idx < 0) idx = 0;
  await ideReviewShow(idx);
}

function ideReviewKeydown(e) {
  if (!state.ideReviewSurface) return;
  const root = ideReviewRoot(state.ideReviewSurface);
  if (!root || root.classList.contains('hidden')) return;
  const a = document.activeElement;
  if (a && a !== document.body && !root.contains(a)) return;   // 焦点在聊天输入框时不抢快捷键
  if (e.key === 'Escape' && state.ideReviewSurface === 'modal') {
    e.preventDefault();
    ideReviewClose();
    return;
  }
  if (!(e.ctrlKey || e.metaKey)) return;
  if (e.key === 'Enter') {
    e.preventDefault();
    ideReviewSettle('accept');
  } else if (e.key === 'Backspace') {
    e.preventDefault();
    ideReviewSettle('undo');
  }
}

/* ---- 文件管理区（活动栏右侧那排视图：资源管理器 / 沙箱内搜索）的收起与展开 ----
   交互对齐 VS Code：点**当前已激活**的活动栏图标 = 收起，再点任意图标 = 展开；
   另配 Ctrl+B（同 VS Code 的 Toggle Primary Side Bar）。
   收起时活动栏不再高亮 —— 否则会「看着是选中态，右边却空着」。
   收起 / 展开动的是 #ide-sides 的宽度（.collapsed），过渡期间冻结 Monaco 布局；
   各视图之间仍然用 .hidden 瞬间切换，不参与动画。 */
const IDE_SIDE_LABEL = { explorer: '资源管理器', search: '沙箱内搜索', git: '源代码管理', debug: '运行和调试', annos: '批注', docs: '文献库' };

/* 侧栏做宽度过渡时冻结 Monaco 的 automaticLayout：
   否则过渡的每一帧都会触发一次全量 layout（收起/展开会明显卡一下）。
   过渡结束再恢复，并手动 layout 一次把编辑器对齐到最终宽度。 */
let _ideLayoutTimer = 0;

function ideFreezeEditorLayout(ms) {
  const ed = state.ideEditor;
  if (!ed || typeof ed.updateOptions !== 'function') return;
  clearTimeout(_ideLayoutTimer);
  try { ed.updateOptions({ automaticLayout: false }); } catch (e) { return; }
  _ideLayoutTimer = setTimeout(() => {
    try {
      ed.updateOptions({ automaticLayout: true });
      ed.layout();
    } catch (e) { /* 编辑器已销毁：忽略 */ }
  }, Math.max(120, Number(ms) || 240));
}

// 把 state 同步到界面（显隐 + 活动栏高亮 + 悬停提示）
// animate=true 时给侧栏宽度做过渡；初始化那次传 false，避免进 IDE 就演一遍动画。
function ideSyncSideUI(animate) {
  const hid = !!state.ideSideHidden;
  const map = {
    explorer: '#ide-side-explorer', search: '#ide-side-search', git: '#ide-side-git',
    debug: '#ide-side-debug', annos: '#ide-side-annos', docs: '#ide-side-docs',
  };
  Object.keys(map).forEach((k) => {
    const el = $(map[k]);
    if (el) el.classList.toggle('hidden', state.ideSide !== k);
  });
  // 收起 / 展开只动容器：各视图保持挂载，展开时不会先白一下再渲染
  const sides = $('#ide-sides');
  if (sides) {
    const changed = sides.classList.contains('collapsed') !== hid;
    sides.classList.toggle('collapsed', hid);
    if (changed && animate) ideFreezeEditorLayout(240);
  }
  const sr = $('#ide-side-resizer');
  if (sr) sr.classList.toggle('hidden', hid);   // 侧栏收起时不留一条 4px 的拖拽条
  $$('#ide-panel .act-btn[data-act]').forEach((b) => {
    const act = b.dataset.act;
    const active = !hid && act === state.ideSide;
    b.classList.toggle('active', active);
    const nm = IDE_SIDE_LABEL[act] || act;
    b.title = hid ? ('展开' + nm + '（Ctrl+B）')
      : (active ? ('收起' + nm + '（Ctrl+B）') : nm);
  });
}

function idePersistSideHidden() {
  const cfg = Object.assign({}, state.config || {}, { ide_side_collapsed: !!state.ideSideHidden });
  api('/api/config', cfg).then(() => { state.config = cfg; }).catch(() => {});
}

// 只控制显隐（Ctrl+B / 收起按钮走这里）
function ideSetSideHidden(hidden) {
  state.ideSideHidden = !!hidden;
  ideSyncSideUI(true);                 // 收起 / 展开都走宽度过渡
  idePersistSideHidden();
}

// 收起 ↔ 展开
function ideToggleSidebar() { ideSetSideHidden(!state.ideSideHidden); }

// 切到某个视图（= 展开；命令面板里的「显示资源管理器 / 在沙箱内搜索」也走这里）
function ideSwitchSide(which) {
  state.ideSide = which;
  state.ideSideHidden = false;
  ideSyncSideUI(true);                 // 从收起态切图标时也是展开动画
  if (which === 'search') { const i = $('#ide-search-input'); if (i) i.focus(); }
  if (which === 'git' && typeof gitRefresh === 'function') gitRefresh();   // 每次切进来都拉一次最新状态
  if (which === 'debug') debugRenderBreakpoints();
  if (which === 'annos') ideAnnoLoad(state.ideOpen);
  if (which === 'docs') ideDocsLoad($('#ide-doc-q') ? $('#ide-doc-q').value : '');
  idePersistSideHidden();
}

// 点活动栏图标：点「当前已激活」的那个 = 收起
function ideToggleSide(which) {
  if (which === state.ideSide && !state.ideSideHidden) { ideSetSideHidden(true); return; }
  ideSwitchSide(which);
}

// 高亮目录树中当前文件
function ideSyncTreeActive() {
  $$('#ide-tree .ide-row').forEach((el) => {
    el.classList.toggle('active', el.dataset.path === state.ideOpen);
    el.classList.toggle('changed', state.ideChanged.has(el.dataset.path));
  });
}

function ideFindRow(rel) {
  return Array.from($$('#ide-tree .ide-row')).find((el) => el.dataset.path === rel) || null;
}

/* 树内联重命名输入框（VS Code 是在树里直接改，不弹 prompt）。
   Enter 提交 / Esc 取消 / 失焦提交；输入法组合期间的 Enter 不算提交。 */
function ideInlineEdit(row, value, onCommit, onCancel) {
  if (!row || row.querySelector('.ide-inline-input')) return;
  const nameEl = row.querySelector('.ide-name');
  if (!nameEl) return;
  const oldText = nameEl.textContent;
  const input = document.createElement('input');
  input.className = 'ide-inline-input';
  input.value = value;
  nameEl.replaceWith(input);
  input.focus();
  const dot = value.lastIndexOf('.');
  try { input.setSelectionRange(0, dot > 0 ? dot : value.length); } catch (e) { /* 忽略 */ }
  let done = false;
  const finish = (commit) => {
    if (done) return;
    done = true;
    const v = (input.value || '').trim();
    const span = document.createElement('span');
    span.className = 'ide-name';
    span.textContent = oldText;
    if (input.parentElement) input.replaceWith(span);
    if (commit && v) onCommit(v);
    else if (onCancel) onCancel();
  };
  input.addEventListener('keydown', (e) => {
    e.stopPropagation();                 // 别让树的 ↑↓/Enter 抢走
    if (isImeComposing(e)) return;
    if (e.key === 'Enter') { e.preventDefault(); finish(true); }
    else if (e.key === 'Escape') { e.preventDefault(); finish(false); }
  });
  input.addEventListener('blur', () => finish(true));
}

/* 轻量确认框：复用 IDE 信息浮层（#ide-info）的样式，替掉浏览器原生 confirm() */
function ideConfirm(title, text, okLabel, onOk) {
  const box = $('#ide-info');
  const body = $('#ide-info-body');
  if (!box || !body) return;
  $('#ide-info-title').textContent = title;
  body.innerHTML = '<p class="ide-confirm-text">' + escapeHtml(text) + '</p>' +
    '<div class="ide-confirm-actions">' +
    '<button type="button" class="ide-btn danger" id="ide-confirm-ok">' + escapeHtml(okLabel || '确定') + '</button>' +
    '<button type="button" class="ide-btn" id="ide-confirm-cancel">取消</button></div>';
  box.classList.remove('hidden');
  const close = () => box.classList.add('hidden');
  const ok = $('#ide-confirm-ok');
  const cancel = $('#ide-confirm-cancel');
  if (ok) ok.onclick = () => { close(); try { onOk(); } catch (e) { /* 忽略 */ } };
  if (cancel) cancel.onclick = close;
  if (ok) ok.focus();
}

/* ---------- 打开 / 激活 / 关闭标签 ---------- */
function ideSetRowLoading(rel, on) {
  const row = Array.from($$('#ide-tree .ide-row')).find((el) => el.dataset.path === rel);
  if (row) row.classList.toggle('loading', !!on);
}

async function openIdeFile(rel, opts) {
  if (!rel) return;
  // PDF 不走编辑器：二进制文件没法当文本打开，交给沉浸式阅读器（左原页 + 右双语阅读流）
  if (ideIsPdf(rel)) {
    idePushRecent(rel);
    if (window.Reader && window.Reader.open) return window.Reader.open(rel);
    return toast('阅读器脚本未加载，请刷新页面', 'error');
  }
  if (state.ideTabList.includes(rel)) {
    ideActivate(rel);
    // 双击 / 明确打开同一个文件 = 把它从预览标签固定下来
    if (opts && opts.pin) idePinTab(rel);
    return;
  }
  /* 请求序号：连续点 A→B 时，若 A 的响应后到，不能让它把已经打开的 B 覆盖掉。
     同时给对应行加 loading，点击后立刻有反馈，不用等磁盘。 */
  const seq = (state.ideOpenReq || 0) + 1;
  state.ideOpenReq = seq;
  ideSetRowLoading(rel, true);
  try {
    if (ideIsImage(rel)) {
      // 图片：走 raw 接口取二进制 → Blob URL 预览
      let blob;
      try {
        const res = await fetch('/api/sandbox/raw', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-Auth-Token': TOKEN },
          body: JSON.stringify({ session_id: state.sessions.current, path: rel }),
        });
        if (!res.ok) {
          let msg = '打开失败';
          try { const j = await res.json(); msg = j.error || msg; } catch (e) { /* 忽略 */ }
          toast(msg, 'error'); return;
        }
        blob = await res.blob();
      } catch (e) { return toast('打开失败：' + e, 'error'); }
      if (seq !== state.ideOpenReq) return;       // 已有更新的打开请求
      state.ideCache[rel] = { image: true, url: URL.createObjectURL(blob), size: blob.size };
      state.ideDirtyMap[rel] = false;
      state.ideTabList.push(rel);
      ideMarkTabKind(rel, !!(opts && opts.preview));
      state.ideChanged.delete(rel);
    } else {
      let r;
      try { r = await api('/api/sandbox/file', { session_id: state.sessions.current, path: rel }); }
      catch (e) { return toast('打开失败：' + e, 'error'); }
      if (seq !== state.ideOpenReq) return;
      if (!r || !r.ok) { toast((r && r.error) || '打开失败', 'error'); return; }
      state.ideCache[rel] = { text: r.text || '', enc: r.encoding || '' };
      state.ideDirtyMap[rel] = false;
      state.ideTabList.push(rel);
      ideMarkTabKind(rel, !!(opts && opts.preview));
      state.ideChanged.delete(rel);
      if (r.truncated) toast('文件较大，仅载入前 2MB');
    }
  } finally {
    ideSetRowLoading(rel, false);
  }
  if (seq !== state.ideOpenReq) return;
  ideActivate(rel);
  idePushRecent(rel);
}

function ideActivate(rel) {
  if (!rel) return;
  state.ideOpen = rel;
  state.ideDir = rel.indexOf('/') >= 0 ? rel.slice(0, rel.lastIndexOf('/')) : '';
  const c = state.ideCache[rel] || { text: '', enc: '' };
  if (c.image) {
    if (state.ideEditor) state.ideEditor.setModel(null);
    const img = $('#ide-image-img');
    if (img) img.src = c.url || '';
    ideShowView('image');
  } else if (state.ideMonaco && state.ideEditor) {
    let model = state.ideModels[rel];
    if (!model) {
      // 给 model 一个 URI，面包屑才有路径可显示（没有 URI 时只显示符号层级）
      const uri = state.ideMonaco.Uri.from({ scheme: 'sandbox', path: '/' + rel });
      model = state.ideMonaco.editor.createModel(c.text, ideLangOf(rel), uri);
      const ts = (state.ideMonacoDefaults || {}).tabSize;
      if (ts) model.updateOptions({ tabSize: ts, insertSpaces: true });
      state.ideModels[rel] = model;
    }
    state.ideEditor.setModel(model);
    state.ideEditor.focus();
    ideShowView('editor');
    debugApplyDecorations();
    if (typeof ideAnnoLoad === 'function') ideAnnoLoad(rel);
  } else {
    const t = $('#ide-text');
    if (t) { t.value = c.text; t.disabled = false; }
    ideShowView('fallback');
  }
  renderIdeTabs();
  ideSyncTreeActive();
  ideUpdateStatus();
  ideRevealInTree(rel);
  ideUpdateDiffBtn();
  ideIntelOnOpen(rel);
  // 预览只对 Markdown 有意义：切到别的文件自动收起，切回来自动重画
  if (!ideMdEligible(rel)) {
    ideMdClose();
  } else {
    const mdBox = document.getElementById('ide-md');
    if (mdBox && !mdBox.classList.contains('hidden')) ideMdRender();
  }
  // 「运行 / 编译」按钮：tex / py / c 才显示，标题按类型变
  const runBtn = $('#ide-run-btn');
  if (runBtn) {
    const canTex = ideTexEligible(rel);
    const canPy = idePyEligible(rel);
    const canC = ideCEligible(rel);
    runBtn.classList.toggle('hidden', !(canTex || canPy || canC));
    runBtn.title = canTex ? '编译 LaTeX（Ctrl+Alt+B）'
      : (canPy ? '运行（Ctrl+Alt+R）' : '编译并运行（Ctrl+Alt+R）');
  }
  if (ideCEligible(rel)) ideCCheck(rel);       // C：打开就做一次语法检查（gcc 很快）
}

function ideCloseTab(rel) {
  if (state.ideDirtyMap[rel]) {
    ideConfirm('未保存的修改', '「' + rel + '」有未保存的修改，仍要关闭吗？', '仍然关闭',
      () => ideCloseTabForce(rel));
    return;
  }
  ideCloseTabForce(rel);
}

function ideCloseTabForce(rel) {
  const i = (state.ideTabList || []).indexOf(rel);
  if (i < 0) return;
  state.ideTabList.splice(i, 1);
  ideDropTab(rel);
  if (state.ideOpen === rel) {
    // VS Code：优先激活右侧邻居，没有右侧才回退到左侧
    const next = state.ideTabList[i] || state.ideTabList[i - 1] || '';
    if (next) ideActivate(next);
    else ideClearEditor();
  } else {
    renderIdeTabs();
  }
}

async function saveIdeFile() {
  const rel = state.ideOpen;
  if (!rel) return;
  if (state.ideCache[rel] && state.ideCache[rel].image) { toast('图片不支持编辑保存'); return; }
  const btn = $('#ide-save');
  if (btn) btn.classList.add('busy');
  const content = (state.ideMonaco && state.ideModels[rel])
    ? state.ideModels[rel].getValue()
    : ($('#ide-text') ? $('#ide-text').value : '');
  try {
    const r = await api('/api/sandbox/save', { session_id: state.sessions.current, path: rel, content: content });
    if (!r || !r.ok) { toast((r && r.error) || '保存失败'); return; }
    state.ideDirtyMap[rel] = false;
    if (state.ideCache[rel]) {
      state.ideCache[rel].text = content;
      if (r.encoding) state.ideCache[rel].enc = r.encoding;
    }
    renderIdeTabs(); ideUpdateStatus();
    ideFlashStatus('已保存');
    ideRefreshGitMarks();              // 保存后文件名 / 状态字母立刻跟着变
    // 语言服务读的是磁盘上的文件：保存后重跑一次诊断，错误才跟着更新
    if (ideLspEligible(rel)) ideIntelSchedule(rel);
    if (ideMdEligible(rel)) ideMdLintSchedule();
    if (ideCEligible(rel)) ideCCheck(rel);
  } catch (e) {
    toast('保存失败：' + e);
  } finally {
    if (btn) btn.classList.remove('busy');
  }
}

async function ideNew(isDir, baseOverride) {
  const box = $('#ide-tree');
  if (!box) return;
  // 右键菜单指定了目标目录就用它；顶部按钮仍沿用当前选中目录
  const base = baseOverride != null ? String(baseOverride) : (state.ideDir || '');
  // 目标容器：当前选中目录的子级；没有选中目录就放在根
  let host = box;
  if (base) {
    const brow = ideFindRow(base);
    const kids = brow && brow.parentElement
      ? brow.parentElement.querySelector(':scope > .ide-children') : null;
    if (kids) {
      if (kids.classList.contains('hidden')) ideToggleDir(brow, kids);
      host = kids;
    }
  }
  const row = document.createElement('div');
  row.className = 'ide-row';
  row.dataset.dir = isDir ? '1' : '0';
  row.dataset.path = '';
  const care = document.createElement('span');
  care.className = 'ide-caret';
  row.appendChild(care);
  row.appendChild(ideIcoEl(isDir ? 'folder' : 'new.txt', isDir));
  const nm = document.createElement('span');
  nm.className = 'ide-name';
  row.appendChild(nm);
  host.appendChild(row);
  row.scrollIntoView({ block: 'nearest' });
  ideInlineEdit(row, isDir ? '新建文件夹' : '新建文件.txt', async (name) => {
    row.remove();
    const rel = ((base ? base + '/' : '') + name).replace(/^\/+/, '');
    let r;
    try { r = await api('/api/sandbox/new', { session_id: state.sessions.current, path: rel, dir: !!isDir }); }
    catch (e) { return toast('新建失败：' + e); }
    if (!r || !r.ok) { toast((r && r.error) || '新建失败'); return; }
    await loadIdeTree();
    if (!isDir) openIdeFile(rel);
  }, () => { if (row.parentElement) row.remove(); });
}

// 顶部按钮：优先重命名唯一选中项，否则重命名当前打开的文件（保持旧行为）
async function ideRename() {
  const sel = (state.ideSel || []).slice();
  const rel = sel.length === 1 ? sel[0] : (state.ideOpen || sel[0] || '');
  if (rel) ideRenameTarget(rel);
}

/* 重命名任意文件 / 目录（右键菜单与 F2）。目录改名会连所有子路径一起换。 */
async function ideRenameTarget(rel) {
  if (!rel) return;
  const row = ideFindRow(rel);
  if (!row) return;
  const cur = rel.indexOf('/') >= 0 ? rel.slice(rel.lastIndexOf('/') + 1) : rel;
  ideRevealInTree(rel);                  // 父目录被收起时先展开，内联输入框才看得见
  ideInlineEdit(row, cur, async (name) => {
    if (!name || name === cur) return;
    let r;
    try { r = await api('/api/sandbox/rename', { session_id: state.sessions.current, path: rel, name }); }
    catch (e) { return toast('重命名失败：' + e); }
    if (!r || !r.ok) { toast((r && r.error) || '重命名失败'); return; }
    const dir = rel.indexOf('/') >= 0 ? rel.slice(0, rel.lastIndexOf('/') + 1) : '';
    const neu = dir + name;
    ideRemapPrefix(rel, neu);
    toast('已重命名');
    await loadIdeTree();
    ideSyncSel();
    if (state.ideOpen && state.ideModels[state.ideOpen]) ideActivate(state.ideOpen);
    else renderIdeTabs();
  });
}

async function ideDelete() {
  // 多选时批量删；没有多选就删当前文件（保持原来的单文件行为）
  const sel = (state.ideSel || []).slice();
  const list = sel.length ? sel : (state.ideOpen ? [state.ideOpen] : []);
  ideDeleteTargets(list);
}

/* 删除任意文件 / 目录（右键菜单、Delete 键与顶部按钮共用）。
   目录按 VS Code 的习惯连内容一起删，确认框里会明确列出要删的路径。 */
async function ideDeleteTargets(paths) {
  const list = (paths || []).filter(Boolean);
  if (!list.length) return;
  const title = list.length > 1 ? ('删除 ' + list.length + ' 个项目') : '删除文件';
  const text = list.length > 1
    ? ('确定删除这 ' + list.length + ' 项？此操作不可撤销。\n'
       + list.slice(0, 8).join('\n') + (list.length > 8 ? '\n…' : ''))
    : ('确定删除「' + list[0] + '」？此操作不可撤销。');
  ideConfirm(title, text, '删除', async () => {
    let done = 0;
    const failed = [];
    for (const p of list) {
      const prefix = p + '/';
      try {
        const r = await api('/api/sandbox/delete', {
          session_id: state.sessions.current, path: p, recursive: true,
        });
        if (r && r.ok) done += 1;
        else failed.push((r && r.error) || p);
      } catch (e) { failed.push(String(e)); }
      // 目录删掉后，它下面所有打开的标签 / 缓存也要一起清掉
      (state.ideTabList || []).slice().forEach((t) => {
        if (t === p || t.indexOf(prefix) === 0) ideCloseTabForce(t);
      });
      Object.keys(state.ideModels || {}).forEach((t) => {
        if (t !== p && t.indexOf(prefix) !== 0) return;
        try { state.ideModels[t].dispose(); } catch (e) { /* 忽略 */ }
        delete state.ideModels[t];
      });
      Object.keys(state.ideCache || {}).forEach((t) => {
        if (t === p || t.indexOf(prefix) === 0) delete state.ideCache[t];
      });
      Object.keys(state.ideDirtyMap || {}).forEach((t) => {
        if (t === p || t.indexOf(prefix) === 0) delete state.ideDirtyMap[t];
      });
      if (state.ideOpen === p || state.ideOpen.indexOf(prefix) === 0) state.ideOpen = '';
    }
    ideClearSel();
    if (!state.ideOpen && state.ideEditor) state.ideEditor.setModel(null);
    renderIdeTabs(); ideUpdateStatus();
    await loadIdeTree();
    toast(failed.length
      ? ('已删除 ' + done + ' 项，' + failed.length + ' 项失败')
      : ('已删除 ' + done + ' 项'));
  });
}

/* ---------- 沙箱内搜索 ---------- */
/* 后端返回的是纯文本（`路径:行号: 内容`）。这里解析成可点击条目，
   点一下直接打开文件并定位到那一行 —— 只能"看"不能"跳"的搜索结果基本没用。 */
const IDE_HIT_RE = /^(.+?):(\d+):\s?(.*)$/;

function ideRevealLine(rel, line) {
  if (!state.ideOpen || state.ideOpen !== rel || !state.ideEditor) return;
  const n = Math.max(1, parseInt(line, 10) || 1);
  const model = state.ideEditor.getModel();
  const M = state.ideMonaco;
  if (model && M && M.Selection) {
    state.ideEditor.setSelection(new M.Selection(n, 1, n, model.getLineMaxColumn(n)));
  }
  state.ideEditor.revealLineInCenter(n);
  state.ideEditor.focus();
}

async function ideSemanticSearch(pat, box) {
  box.innerHTML = '<div class="ide-empty">语义检索中…</div>';
  try {
    const r = await api('/api/codeindex/search', {
      session_id: state.sessions.current, query: pat, limit: 20,
    });
    if (r && r.building) {
      box.innerHTML = '<div class="ide-empty">全仓索引构建中（'
        + escapeHtml(((r.status || {}).done || 0) + '/' + ((r.status || {}).total || '?'))
        + ' 文件）…稍后再试</div>';
      return;
    }
    if (!r || !r.ok) {
      box.innerHTML = '<div class="ide-empty">'
        + escapeHtml((r && r.error) || '语义检索失败') + '</div>';
      return;
    }
    const items = r.items || [];
    if (!items.length) { box.innerHTML = '<div class="ide-empty">没有语义命中</div>'; return; }
    box.innerHTML = items.map((it, i) =>
      '<div class="ide-hit" data-i="' + i + '" title="'
      + escapeHtml(it.path + ':' + it.start) + '">'
      + '<span class="ide-hit-path">' + escapeHtml(it.path) + ':' + Number(it.start)
      + (it.symbol ? ' · ' + escapeHtml(it.symbol) : '') + '</span>'
      + '<span class="ide-hit-text">' + escapeHtml((it.snippet || '').split('\n')[0] || '')
      + '</span></div>').join('');
    $$('#ide-search-hits .ide-hit').forEach((el) => {
      el.onclick = () => {
        const it = items[Number(el.dataset.i)];
        if (!it) return;
        openIdeFile(it.path).then(() => ideRevealLine(it.path, it.start));
      };
    });
  } catch (e) {
    box.innerHTML = '<div class="ide-empty">语义检索失败：' + escapeHtml(String(e)) + '</div>';
  }
}

async function ideSearch() {
  const box = $('#ide-search-hits');
  const inp = $('#ide-search-input');
  if (!box || !inp) return;
  const pat = (inp.value || '').trim();
  if (!pat) { box.innerHTML = ''; return; }
  const sem = $('#ide-search-semantic');
  if (sem && sem.checked) return ideSemanticSearch(pat, box);
  box.innerHTML = '<div class="ide-empty">搜索中…</div>';
  try {
    const r = await api('/api/sandbox/search', { session_id: state.sessions.current, pattern: pat });
    if (!r || !r.ok) { box.innerHTML = '<div class="ide-empty">' + escapeHtml((r && r.error) || '搜索失败') + '</div>'; return; }
    const lines = (r.text || '').split('\n').filter((x) => x.trim());
    const items = [];
    lines.forEach((ln) => {
      const m = IDE_HIT_RE.exec(ln);
      if (m) { items.push({ path: m[1], line: Number(m[2]), text: m[3] }); return; }
      if (/（文件名/.test(ln)) {
        items.push({ path: ln.split(/\s{2}/)[0], line: 0, text: '文件名匹配' });
      }
    });
    if (!items.length) { box.innerHTML = '<div class="ide-empty">无匹配</div>'; return; }
    box.innerHTML = items.map((it, i) =>
      '<div class="ide-hit" data-i="' + i + '" title="' + escapeHtml(it.path + (it.line ? ':' + it.line : '')) + '">' +
      '<span class="ide-hit-path">' + escapeHtml(it.path) + (it.line ? ':' + it.line : '') + '</span>' +
      '<span class="ide-hit-text">' + escapeHtml(it.text) + '</span></div>').join('');
    $$('#ide-search-hits .ide-hit').forEach((el) => {
      el.onclick = () => {
        const it = items[Number(el.dataset.i)];
        if (!it) return;
        openIdeFile(it.path).then(() => { if (it.line) ideRevealLine(it.path, it.line); });
      };
    });
  } catch (e) {
    box.innerHTML = '<div class="ide-empty">搜索失败：' + escapeHtml(String(e)) + '</div>';
  }
}

/* ---------------- 运行和调试 ---------------- */
function debugRel() { return state.ideOpen || ''; }

function debugApplyDecorations() {
  const ed = state.ideEditor;
  const M = state.ideMonaco;
  if (!ed || !M || !M.Range) return;
  const rel = debugRel();
  const deco = [];
  (state.ideBreakpoints[rel] || []).forEach((line) => {
    deco.push({ range: new M.Range(line, 1, line, 1),
      options: { glyphMarginClassName: 'ide-bp-glyph' } });
  });
  const ds = state.debugState;
  if (state.debugPaused && ds && (ds.frames || []).length) {
    const top = ds.frames[0];
    if (top && top.path === rel && top.line) {
      deco.push({ range: new M.Range(top.line, 1, top.line, 1),
        options: { isWholeLine: true, className: 'ide-debug-line' } });
    }
  }
  state.debugDecorations = ed.deltaDecorations(state.debugDecorations || [], deco);
}

function debugBindEditor(ed, M) {
  if (!ed || !M || !M.editor) return;
  ed.onMouseDown((e) => {
    const t = e.target && e.target.type;
    if (t !== M.editor.MouseTargetType.GUTTER_GLYPH_MARGIN
        && t !== M.editor.MouseTargetType.GUTTER_LINE_NUMBERS) return;
    const rel = debugRel();
    const line = e.target.position && e.target.position.lineNumber;
    if (!rel || !line) return;
    debugToggleBreakpoint(rel, line);
  });
}

function debugToggleBreakpoint(rel, line) {
  const arr = (state.ideBreakpoints[rel] || []).slice();
  const i = arr.indexOf(line);
  if (i >= 0) arr.splice(i, 1); else arr.push(line);
  arr.sort((a, b) => a - b);
  state.ideBreakpoints[rel] = arr;
  debugApplyDecorations();
  debugRenderBreakpoints();
  debugSyncBreakpoints();
}

async function debugSyncBreakpoints() {
  if (!state.debugActive) return;
  const sid = (state.sessions && state.sessions.current) || '';
  if (!sid) return;
  try {
    await api('/api/debug/breakpoints', {
      session_id: sid, breakpoints: state.ideBreakpoints,
    });
  } catch (e) { /* 断点同步失败不打断编辑 */ }
}

function debugRenderBreakpoints() {
  const box = $('#debug-breakpoints');
  if (!box) return;
  const items = [];
  Object.keys(state.ideBreakpoints || {}).forEach((rel) => {
    (state.ideBreakpoints[rel] || []).forEach((line) => items.push({ rel, line }));
  });
  if (!items.length) {
    box.innerHTML = '<div class="ide-empty">在编辑器行号左侧点击添加</div>';
    return;
  }
  box.innerHTML = items.map((it, i) =>
    '<button type="button" class="ide-debug-bp" data-i="' + i + '">'
    + '<span>●</span><span class="df-path">' + escapeHtml(it.rel) + ':' + it.line
    + '</span></button>').join('');
  $$('#debug-breakpoints .ide-debug-bp').forEach((el) => {
    el.onclick = () => {
      const it = items[Number(el.dataset.i)];
      if (it) openIdeFile(it.rel).then(() => ideRevealLine(it.rel, it.line));
    };
  });
}

function debugRenderState(ds) {
  state.debugState = ds || null;
  state.debugPaused = !!(ds && ds.paused);
  const stack = $('#debug-stack');
  const vars = $('#debug-vars');
  const out = $('#debug-output');
  if (stack) {
    const frames = (ds && ds.frames) || [];
    stack.innerHTML = frames.length ? frames.map((f, i) =>
      '<button type="button" class="ide-debug-frame' + (i === 0 ? ' on' : '')
      + '" data-i="' + i + '"><span>' + escapeHtml(f.name || '(anonymous)')
      + '</span><span class="df-path">' + escapeHtml((f.path || '') + ':' + (f.line || ''))
      + '</span></button>').join('')
      : '<div class="ide-empty">' + (state.debugActive ? '运行中…' : '未启动') + '</div>';
    $$('#debug-stack .ide-debug-frame').forEach((el) => {
      el.onclick = () => {
        const f = frames[Number(el.dataset.i)];
        if (f && f.path) openIdeFile(f.path).then(() => ideRevealLine(f.path, f.line));
      };
    });
  }
  if (vars) {
    const scopes = (ds && ds.scopes) || [];
    vars.innerHTML = scopes.length ? scopes.map((sc) =>
      '<div class="ide-debug-scope"><b>' + escapeHtml(sc.name || 'scope') + '</b>'
      + (sc.variables || []).map((v) =>
        '<div class="ide-debug-var"><i>' + escapeHtml(v.name) + '</i><span>'
        + escapeHtml(v.value) + '</span></div>').join('') + '</div>').join('')
      : '<div class="ide-empty">—</div>';
  }
  if (out) out.textContent = ((ds && ds.output) || []).join('\n');
  debugApplyDecorations();
  // 停在某个文件时自动跳过去（只在最上方栈帧变化时跳，避免每 600ms 抢焦点）
  if (state.debugPaused && ds && (ds.frames || []).length) {
    const top = ds.frames[0];
    const key = top.path + ':' + top.line;
    if (top.path && key !== state.debugLastFrame) {
      state.debugLastFrame = key;
      openIdeFile(top.path).then(() => ideRevealLine(top.path, top.line));
    }
  }
}

async function debugPoll(force) {
  if (!force && !state.debugActive) return;
  clearTimeout(state.debugTimer);
  const sid = (state.sessions && state.sessions.current) || '';
  if (!sid) return;
  try {
    const r = await api('/api/debug/state', { session_id: sid });
    if (r && r.ok) {
      state.debugActive = !!r.running;
      debugRenderState(r);
    }
  } catch (e) { /* 调试进程退出时接口可能先失败 */ }
  if (state.debugActive) state.debugTimer = setTimeout(() => debugPoll(false), 600);
}

function debugParseArgs(text) {
  const s = String(text || '').trim();
  if (!s) return [];
  const out = [];
  const re = /"([^"]*)"|'([^']*)'|(\S+)/g;
  let m;
  while ((m = re.exec(s))) out.push(m[1] != null ? m[1] : (m[2] != null ? m[2] : m[3]));
  return out;
}

async function debugStart() {
  const sid = (state.sessions && state.sessions.current) || '';
  if (!sid) return toast('先打开一个会话');
  const prog = ($('#debug-program') && $('#debug-program').value.trim()) || state.ideOpen;
  if (!prog) return toast('先打开一个文件，或填写脚本路径');
  const btn = $('#debug-start');
  if (btn) btn.disabled = true;
  try {
    const r = await api('/api/debug/start', {
      session_id: sid, program: prog,
      args: debugParseArgs($('#debug-args') && $('#debug-args').value),
      breakpoints: state.ideBreakpoints,
      stop_on_entry: !!($('#debug-stop-entry') && $('#debug-stop-entry').checked),
    });
    if (!r || !r.ok) throw new Error((r && r.error) || '启动失败');
    state.debugActive = true;
    state.debugLastFrame = '';
    debugPoll(true);
    toast('调试已启动：' + (r.program || prog));
  } catch (e) {
    toastErr('启动调试', e);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function debugStop() {
  const sid = (state.sessions && state.sessions.current) || '';
  if (!sid) return;
  try { await api('/api/debug/stop', { session_id: sid }); } catch (e) { /* 忽略 */ }
  state.debugActive = false;
  state.debugPaused = false;
  clearTimeout(state.debugTimer);
  debugRenderState(null);
}

async function debugAction(act) {
  const sid = (state.sessions && state.sessions.current) || '';
  if (!sid) return toast('先打开一个会话');
  try {
    const r = await api('/api/debug/action', { session_id: sid, action: act });
    if (!r || !r.ok) throw new Error((r && r.error) || '操作失败');
    debugPoll(true);
  } catch (e) {
    toastErr('调试操作', e);
  }
}

async function debugEvaluate() {
  const sid = (state.sessions && state.sessions.current) || '';
  const inp = $('#debug-eval-input');
  const box = $('#debug-eval-result');
  const expr = inp ? inp.value.trim() : '';
  if (!sid || !expr) return;
  try {
    const r = await api('/api/debug/evaluate', { session_id: sid, expression: expr });
    if (box) box.textContent = (r && r.ok) ? String(r.value) : ((r && r.error) || '求值失败');
  } catch (e) {
    if (box) box.textContent = '求值失败：' + e;
  }
}

function debugBindControls() {
  const start = $('#debug-start');
  if (start) start.onclick = debugStart;
  const stop = $('#debug-stop');
  if (stop) stop.onclick = debugStop;
  const refresh = $('#debug-refresh');
  if (refresh) refresh.onclick = () => debugPoll(true);
  $$('#ide-side-debug .ide-debug-toolbar [data-dact]').forEach((b) => {
    b.onclick = () => debugAction(b.dataset.dact);
  });
  const prog = $('#debug-program');
  if (prog) {
    prog.addEventListener('focus', () => {
      if (!prog.value && state.ideOpen) prog.value = state.ideOpen;
    });
  }
  const ev = $('#debug-eval-input');
  if (ev) {
    ev.addEventListener('keydown', (e) => {
      if (isImeComposing(e)) return;
      if (e.key === 'Enter') { e.preventDefault(); debugEvaluate(); }
    });
  }
  // 调试快捷键由 init.js 的统一 ide 作用域分发调用（键位可在「?」清单里改）
  debugRenderBreakpoints();
}

/* 调试快捷键的具体动作。键位判定 / 焦点保护都在 init.js 的 ide 分发里，
   这里只做「这个 id 对应干什么」——避免两处各写一份 F5/F10/F11。 */
function ideDebugShortcut(id) {
  if (id === 'ide.debugStart') {
    if (state.debugPaused) debugAction('continue'); else debugStart();
  } else if (id === 'ide.debugNext') {
    debugAction('next');
  } else if (id === 'ide.debugIn') {
    debugAction('step_in');
  } else if (id === 'ide.debugOut') {
    debugAction('step_out');
  } else if (id === 'ide.debugStop') {
    debugStop();
  }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', debugBindControls);
} else {
  debugBindControls();
}


/* 全局替换：先 dry_run 出方案（改几个文件、改哪几处），确认后才写盘。
   写盘由后端登记 undo，可在「AI 变更」里撤销。 */
async function ideReplaceAll() {
  const pin = $('#ide-search-input');
  const rin = $('#ide-replace-input');
  const rxc = $('#ide-search-regex');
  if (!pin || !rin) return;
  const query = (pin.value || '').trim();
  if (!query) return toast('先在搜索框输入要查找的内容');
  const base = {
    session_id: state.sessions.current, query: query,
    replacement: rin.value || '', regex: !!(rxc && rxc.checked), dry_run: true,
  };
  const btn = $('#ide-replace-all');
  if (btn) btn.disabled = true;
  try {
    const plan = await api('/api/ide/replace', base);
    if (!plan || !plan.ok) return toast((plan && plan.error) || '算方案失败');
    if (!plan.total) return toast('没有匹配到内容');
    const files = plan.files || [];
    const lines = files.slice(0, 10).map((f) => f.path + '（' + f.count + ' 处）');
    const more = files.length > 10 ? '\n… 共 ' + files.length + ' 个文件' : '';
    const skip = plan.skipped ? '\n（跳过 ' + plan.skipped + ' 个非 UTF-8 / 超大文件）' : '';
    const cut = plan.truncated ? '\n（已达单次上限，剩余部分请再执行一次）' : '';
    ideConfirm('全局替换',
      '将在 ' + files.length + ' 个文件里替换 ' + plan.total + ' 处：\n'
        + lines.join('\n') + more + skip + cut,
      '全部替换', async () => {
        let done = null;
        try {
          done = await api('/api/ide/replace', Object.assign({}, base, { dry_run: false }));
        } catch (e) { return toast('替换失败：' + e); }
        if (!done || !done.ok) return toast((done && done.error) || '替换失败');
        await ideReloadFiles((done.files || []).map((x) => x.path));
        toast('已替换 ' + done.total + ' 处（' + (done.files || []).length + ' 个文件，可在 AI 变更里撤销）');
        ideSearch();
      });
  } catch (e) {
    toast('替换失败：' + e);
  } finally {
    if (btn) btn.disabled = false;
  }
}

/* ---------- 命令面板 / 快速打开（Ctrl+Shift+P / Ctrl+P） ---------- */
const IDE_COMMANDS = [
  { title: '保存当前文件', key: 'Ctrl+S', run: () => saveIdeFile() },
  { title: '快速打开文件', key: 'Ctrl+P', run: () => idePaletteOpen('file') },
  { title: '新建文件', run: () => ideNew(false) },
  { title: '新建文件夹', run: () => ideNew(true) },
  { title: '重命名当前文件', run: () => ideRename() },
  { title: '删除当前文件', run: () => ideDelete() },
  { title: '刷新目录树', run: () => loadIdeTree() },
  { title: '在沙箱内搜索', run: () => ideSwitchSide('search') },
  { title: '添加批注（选中行）', key: 'Ctrl+Alt+M', run: () => ideAnnoAdd() },
  { title: '显示批注面板', run: () => ideSwitchSide('annos') },
  { title: '显示文献库', run: () => ideSwitchSide('docs') },
  { title: '全仓语义搜索', run: () => {
    ideSwitchSide('search');
    const c = $('#ide-search-semantic'); if (c) c.checked = true;
    const i = $('#ide-search-input'); if (i) i.focus();
  } },
  { title: '开始 / 继续调试', key: 'F5', run: () => {
    ideSwitchSide('debug');
    if (state.debugActive && state.debugPaused) debugAction('continue');
    else debugStart();
  } },
  { title: '单步跳过', key: 'F10', run: () => debugAction('next') },
  { title: '单步进入', key: 'F11', run: () => debugAction('step_in') },
  { title: '单步跳出', key: 'Shift+F11', run: () => debugAction('step_out') },
  { title: '停止调试', key: 'Shift+F5', run: debugStop },
  { title: '全局替换', run: () => { ideSwitchSide('search'); const i = $('#ide-replace-input'); if (i) i.focus(); } },
  { title: '问题面板', key: 'Ctrl+Shift+M', run: () => ideIntelToggle('problems', true) },
  { title: '文件大纲', key: 'Ctrl+Shift+O', run: () => ideIntelToggle('outline', true) },
  { title: '转到定义', key: 'F12', run: () => ideIntelGoto('definition') },
  { title: '查找引用', key: 'Shift+F12', run: () => ideIntelGoto('references') },
  { title: '重命名符号', key: 'F2', run: () => ideRenameSymbol() },
  { title: 'AI 改写选中代码', key: 'Ctrl+I', run: () => ideInlineEditAI() },
  { title: 'AI 问问选中代码（多轮追问）', key: 'Ctrl+Alt+I', run: () => ideInlineAsk() },
  { title: '网页预览', run: () => idePreviewToggle(true) },
  { title: 'Markdown 预览', key: 'Ctrl+Shift+V', run: () => ideMdToggle() },
  { title: 'Markdown：修正可自动修的问题', run: () => ideMdFixAll() },
  { title: '导出当前 Markdown 为 PDF', run: () => ideMdExportPdf() },
  { title: '编译 LaTeX（生成 PDF）', key: 'Ctrl+Alt+B', run: () => ideTexBuild() },
  { title: '打开当前 LaTeX 的 PDF', run: () => ideTexOpenPdf() },
  { title: '清理 LaTeX 辅助文件', run: () => ideTexClean() },
  { title: '运行当前文件 / 编译并运行', key: 'Ctrl+Alt+R', run: () => ideRunCurrent() },
  { title: '格式化文档', key: 'Shift+Alt+F', run: () => ideFormatDoc() },
  { title: 'Python：ruff 检查', run: () => ideRuffCheck() },
  { title: 'C：语法检查（gcc）', run: () => ideCCheck(state.ideOpen) },
  { title: '显示资源管理器', run: () => ideSwitchSide('explorer') },
  { title: '收起 / 展开文件管理区', key: 'Ctrl+B', run: () => ideToggleSidebar() },
  { title: '打开 / 关闭终端', key: 'Ctrl+`', run: () => ideTermToggle() },
  { title: '查看 AI 对当前文件的改动', run: () => ideShowDiff(state.ideOpen) },
  { title: 'AI 改动时间线（回到某一步）', run: () => { if (typeof openStepsPanel === 'function') openStepsPanel(); } },
  { title: '在 VS Code 中打开当前沙箱', run: () => ideOpenInVsCode() },
  { title: '从 VS Code 导入设置与扩展清单', run: () => ideImportVsCode() },
  { title: '切换到 Solo 模式', run: () => setUiMode('solo') },
];

function ideFileList() {
  const out = [];
  (function walk(items, prefix) {
    (items || []).forEach((it) => {
      const rel = prefix ? prefix + '/' + it.name : it.name;
      if (it.dir) walk(it.children || [], rel); else out.push(rel);
    });
  })((state.ideTree || {}).children || [], '');
  return out;
}

function idePaletteOpen(mode) {
  state.idePaletteMode = mode;
  state.idePaletteSel = 0;
  const box = $('#ide-cmd');
  const inp = $('#ide-cmd-input');
  if (!box || !inp) return;
  box.classList.remove('hidden');
  inp.value = '';
  inp.placeholder = (mode === 'file') ? '输入文件名快速打开…' : '输入命令…';
  inp.focus();
  idePaletteRender('');
}

function idePaletteClose() {
  const b = $('#ide-cmd');
  if (b) b.classList.add('hidden');
}

function idePaletteRender(q) {
  const box = $('#ide-cmd-list');
  if (!box) return;
  const query = (q || '').toLowerCase();
  let items;
  if (state.idePaletteMode === 'file') {
    items = ideFileList().filter((p) => p.toLowerCase().indexOf(query) >= 0)
      .map((p) => ({ title: p, run: () => openIdeFile(p) }));
  } else {
    items = IDE_COMMANDS.filter((c) => c.title.toLowerCase().indexOf(query) >= 0);
  }
  state.idePaletteItems = items.slice(0, 60);
  if (state.idePaletteSel >= state.idePaletteItems.length) state.idePaletteSel = 0;
  box.innerHTML = state.idePaletteItems.map((c, i) =>
    '<div class="ide-cmd-item' + (i === state.idePaletteSel ? ' active' : '') + '" data-i="' + i + '">' +
    '<span class="ide-cmd-title">' + escapeHtml(c.title) + '</span>' +
    (c.key ? '<span class="ide-cmd-key">' + escapeHtml(c.key) + '</span>' : '') +
    '</div>').join('') || '<div class="ide-empty">无匹配</div>';
  $$('#ide-cmd-list .ide-cmd-item').forEach((el) => { el.onclick = () => idePaletteRun(Number(el.dataset.i)); });
  // ↑↓ 移动时让选中项始终留在可视区内（否则长列表里选中项会跑到视口外）
  const act = box.querySelector('.ide-cmd-item.active');
  if (act) act.scrollIntoView({ block: 'nearest' });
}

function idePaletteRun(i) {
  const it = (state.idePaletteItems || [])[i];
  if (!it) return;
  idePaletteClose();
  try { it.run(); } catch (e) { /* 忽略 */ }
}

/* ---------- 阶段 3：VS Code 联动 ---------- */
async function ideOpenInVsCode() {
  try {
    const r = await api('/api/sandbox/open_vscode', { session_id: state.sessions.current, path: state.ideOpen || '' });
    if (!r || !r.ok) { toast((r && r.error) || '打开失败'); return; }
    toast('已在 VS Code 中打开');
  } catch (e) { toast('打开失败：' + e); }
}

function ideInfoShow(title, rows) {
  const box = $('#ide-info');
  if (!box) return;
  $('#ide-info-title').textContent = title;
  $('#ide-info-body').innerHTML = rows;
  box.classList.remove('hidden');
}

async function ideImportVsCode() {
  let r;
  try { r = await api('/api/vscode/import', {}); }
  catch (e) { return toast('读取 VS Code 配置失败：' + e); }
  if (!r || !r.ok) { toast((r && r.error) || '读取 VS Code 配置失败'); return; }
  const st = r.settings || {};
  state.ideMonacoDefaults = st;
  const LABELS = {
    fontSize: '字号 (editor.fontSize)',
    tabSize: '缩进 (editor.tabSize)',
    fontFamily: '字体 (editor.fontFamily)',
    wordWrap: '自动换行 (editor.wordWrap)',
    mouseWheelZoom: 'Ctrl+滚轮缩放 (editor.mouseWheelZoom)',
  };
  const applied = Object.keys(LABELS).filter((k) => st[k] !== undefined && st[k] !== '');
  if (state.ideEditor) {
    const opt = {};
    if (st.fontSize) opt.fontSize = st.fontSize;
    if (st.tabSize) opt.tabSize = st.tabSize;
    if (st.fontFamily) opt.fontFamily = st.fontFamily;
    if (st.wordWrap) opt.wordWrap = st.wordWrap;
    if (typeof st.mouseWheelZoom === 'boolean') opt.mouseWheelZoom = st.mouseWheelZoom;
    if (Object.keys(opt).length) state.ideEditor.updateOptions(opt);
  }
  if (st.tabSize) {
    Object.keys(state.ideModels || {}).forEach((k) => {
      try { state.ideModels[k].updateOptions({ tabSize: st.tabSize, insertSpaces: true }); } catch (e) { /* 忽略 */ }
    });
  }
  const exts = r.extensions || [];
  const paths = r.paths || {};
  let html = '<p class="hint">只读同步：设置已应用到编辑器；扩展仅列出，<b>不会安装</b>（要真正使用插件请用「在 VS Code 中打开」）。</p>';
  html += '<h4>已应用设置（共读取 ' + escapeHtml(String(st._count || 0)) + ' 项）</h4>';
  html += applied.length
    ? '<ul>' + applied.map((k) => '<li>' + escapeHtml(LABELS[k]) + '：' + escapeHtml(String(st[k])) + '</li>').join('') + '</ul>'
    : '<p class="hint">你的 VS Code 没有设置这几项（字号 / 缩进 / 字体 / 换行 / 滚轮缩放）。</p>';
  // 「空白字符」不跟着导入值走：它由「设置 → 主题与外观」统一管，避免两个来源互相打架
  html += '<p class="hint">空白字符显示（editor.renderWhitespace）不受此处影响，'
        + '请到「设置 → 主题与外观 → 空白字符可视化」调整'
        + (st.renderWhitespace ? '（你的 VS Code 当前是 ' + escapeHtml(String(st.renderWhitespace)) + '）。' : '。') + '</p>';
  html += '<h4>检测到的扩展（' + exts.length + '）</h4>';
  html += exts.length
    ? '<ul>' + exts.slice(0, 200).map((e) => '<li>' + escapeHtml(e) + '</li>').join('') + '</ul>'
    : '<p class="hint">未检测到扩展目录。</p>';
  html += '<h4>配置来源</h4><ul>' +
    '<li>设置：' + escapeHtml(paths.settings || '未找到') + '</li>' +
    '<li>快捷键：' + escapeHtml(paths.keybindings || '未找到') + '</li>' +
    '<li>扩展目录：' + escapeHtml(paths.extensions || '未找到') + '</li></ul>';
  ideInfoShow('从 VS Code 导入', html);
}

// AI 改文件后：高亮 + 刷新；若改的正是当前打开的文件且未编辑，则自动重载
let ideReloadTimer = null;
function ideOnToolDone(obj, row, sid) {
  if (!obj || !obj.write) return;
  const a = obj.args || {};
  const paths = [a.path, a.from, a.to]
    .filter((p) => !!p)
    .map((p) => String(p).replace(/\\/g, '/'));
  paths.forEach((p) => { state.ideChanged.add(p); });
  // 记下这次写操作：同一轮同一文件合并成一条审阅记录
  if (obj.undo && (obj.name === 'write_file' || obj.name === 'edit_file'
                   || obj.name === 'apply_worktree')) {
    const p = String(a.path || '').replace(/\\/g, '/');
    if (p) ideReviewAdd(p, obj.undo, obj.name, a, row, sid);
  }
  ideUpdateDiffBtn();
  if (state.uiMode !== 'ide') return;
  clearTimeout(ideReloadTimer);
  ideReloadTimer = setTimeout(async () => {
    const open = state.ideOpen;
    await loadIdeTree();
    const openIsImage = !!(open && state.ideCache[open] && state.ideCache[open].image);
    if (open && state.ideChanged.has(open) && !state.ideDirtyMap[open] && !openIsImage) {
      try {
        const r = await api('/api/sandbox/file', { session_id: state.sessions.current, path: open });
        if (r && r.ok && state.ideOpen === open) {
          state.ideCache[open] = { text: r.text || '', enc: r.encoding || '' };
          if (state.ideModels[open]) state.ideModels[open].setValue(r.text || '');
          else { const t = $('#ide-text'); if (t) t.value = r.text || ''; }
          ideSetDirty(open, false);
          state.ideChanged.delete(open);
          await loadIdeTree();
          toast('AI 修改了当前文件，已刷新');
        }
      } catch (e) { /* 忽略 */ }
    }
  }, 250);
}

/* 审阅面板按钮绑定：脚本在 </body> 前加载，此处 DOM 已就绪 */
(function ideReviewBindUI() {
  const bind = (id, fn) => {
    const b = document.getElementById(id);
    if (b) b.onclick = fn;
  };
  bind('ide-review-prev', () => ideReviewStep(-1));
  bind('ide-review-next', () => ideReviewStep(1));
  bind('ide-review-undo', () => ideReviewSettle('undo'));
  bind('ide-review-keep', () => ideReviewSettle('accept'));
  bind('ide-review-ai-btn', () => ideReviewAiSummary('ide'));
  bind('ide-review-open-ide', () => ideReviewOpenInIde());
  bind('review-prev', () => ideReviewStep(-1));
  bind('review-next', () => ideReviewStep(1));
  bind('review-undo', () => ideReviewSettle('undo'));
  bind('review-keep', () => ideReviewSettle('accept'));
  bind('review-ai-btn', () => ideReviewAiSummary('modal'));
  bind('review-open-ide', () => ideReviewOpenInIde());
  bind('review-close', () => ideReviewClose());
  document.addEventListener('keydown', ideReviewKeydown);
  ideReviewTipBind();
})();

/* ---------------- 文件批注（统一 annotations 表） ----------------
   行区间锚定 + quote 兜底；左侧活动栏「批注」面板列出当前文件的批注。
   与 PDF 阅读器、笔记库共用 /api/anno/*，后续换存储不影响这里。 */

state.ideAnnos = [];
state.ideAnnoDecos = [];

function ideAnnoLines(a) {
  const s = Math.max(1, Number(a.line_start || 1));
  const e = Math.max(s, Number(a.line_end || s));
  return [s, e];
}

async function ideAnnoLoad(rel) {
  const key = rel || state.ideOpen || '';
  state.ideAnnoFile = key;
  const fileEl = $('#ide-anno-file');
  if (fileEl) fileEl.textContent = key || '（未打开文件）';
  if (!key) { state.ideAnnos = []; ideAnnoRender(); ideAnnoDecorate(); return; }
  try {
    const r = await api('/api/anno/list', { session_id: state.sessions.current, path: key });
    if (state.ideAnnoFile !== key) return;      // 期间切了文件，丢弃旧结果
    state.ideAnnos = (r && r.ok && r.items) || [];
  } catch (e) { state.ideAnnos = []; }
  ideAnnoRender();
  ideAnnoDecorate();
}

function ideAnnoRender() {
  const box = $('#ide-anno-list');
  const badge = $('#ide-anno-badge');
  const list = state.ideAnnos || [];
  if (badge) {
    badge.textContent = list.length ? String(list.length) : '';
    badge.classList.toggle('hidden', !list.length);
  }
  if (!box) return;
  if (!list.length) {
    box.innerHTML = '<div class="ide-empty">还没有批注。选中行后按 Ctrl+Alt+M 添加。</div>';
    return;
  }
  box.innerHTML = list.map((a) => {
    const [s, e] = ideAnnoLines(a);
    const head = s === e ? ('第 ' + s + ' 行') : ('第 ' + s + '–' + e + ' 行');
    const body = a.comment ? escapeHtml(a.comment)
      : (a.quote ? escapeHtml(a.quote.slice(0, 120)) : '高亮');
    return '<div class="ide-anno-item" data-id="' + escapeHtml(a.id) + '" data-line="' + s + '">'
      + '<div class="ide-anno-head"><span>' + head + '</span>'
      + '<button data-del title="删除">✕</button></div>'
      + '<div class="ide-anno-body">' + body + '</div>'
      + (a.quote && a.comment
        ? '<div class="ide-anno-quote">' + escapeHtml(a.quote.slice(0, 120)) + '</div>' : '')
      + '</div>';
  }).join('');
  $$('#ide-anno-list .ide-anno-item').forEach((row) => {
    row.onclick = (ev) => {
      if (ev.target.closest('[data-del]')) return;
      ideAnnoJump(Number(row.dataset.line));
    };
    const del = row.querySelector('[data-del]');
    if (del) del.onclick = async (ev) => {
      ev.stopPropagation();
      if (!confirm('删除这条批注？')) return;
      await api('/api/anno/delete', {
        session_id: state.sessions.current, id: row.dataset.id, path: state.ideAnnoFile,
      });
      ideAnnoLoad(state.ideAnnoFile);
    };
  });
}

function ideAnnoDecorate() {
  const ed = state.ideEditor;
  const M = state.ideMonaco;
  if (!ed || !M || !M.Range) return;
  const deco = [];
  (state.ideAnnos || []).forEach((a) => {
    const [s, e] = ideAnnoLines(a);
    deco.push({
      range: new M.Range(s, 1, e, 1),
      options: {
        isWholeLine: true,
        className: 'ide-anno-line',
        hoverMessage: { value: a.comment || a.quote || '批注' },
      },
    });
  });
  state.ideAnnoDecos = ed.deltaDecorations(state.ideAnnoDecos || [], deco);
}

function ideAnnoJump(line) {
  const ed = state.ideEditor;
  if (!ed || !line) return;
  ed.revealLineInCenter(line);
  ed.setPosition({ lineNumber: line, column: 1 });
  ed.focus();
}

async function ideAnnoAdd() {
  const ed = state.ideEditor;
  const rel = state.ideOpen;
  if (!ed || !rel) return toast('先打开一个文件');
  const sel = ed.getSelection();
  const model = ed.getModel();
  if (!sel || !model) return;
  const s = sel.startLineNumber;
  const e = sel.endLineNumber;
  const quote = String(model.getValueInRange(sel) || '').trim().slice(0, 400);
  const comment = window.prompt('批注内容（留空 = 只做行高亮）：', '');
  if (comment === null) return;
  const txt = String(comment || '').trim();
  if (!txt && !quote) return toast('没有可批注的内容');
  const r = await api('/api/anno/save', {
    session_id: state.sessions.current, path: rel,
    kind: txt ? 'note' : 'highlight',
    quote: quote, comment: txt,
    anchor: { line_start: s, line_end: e },
  });
  if (!r || !r.ok) return toast('保存失败：' + ((r && r.error) || ''));
  ideSwitchSide('annos');
  ideAnnoLoad(rel);
  toast('已添加批注');
}

/* ---------------- 文献库（沙箱内自动生成的文献条目） ---------------- */
state.ideDocs = [];

async function ideDocsLoad(q) {
  try {
    const r = await api('/api/doc/list', { session_id: state.sessions.current, q: q || '' });
    state.ideDocs = (r && r.ok && r.items) || [];
  } catch (e) { state.ideDocs = []; }
  ideDocsRender();
}

function ideDocsRender() {
  const box = $('#ide-doc-list');
  if (!box) return;
  const list = state.ideDocs || [];
  if (!list.length) {
    box.innerHTML = '<div class="ide-empty">还没有文献。打开 PDF 后会自动生成条目。</div>';
    return;
  }
  box.innerHTML = list.map((d) => {
    const authors = (d.authors || []).join(', ');
    const meta = [authors, d.year || '', d.venue || ''].filter(Boolean).join(' · ');
    const tags = (d.tags || []).map((t) => '#' + t).join(' ');
    return '<div class="ide-doc-item" data-path="' + escapeHtml(d.doc_key) + '">'
      + '<div class="ide-doc-title">' + escapeHtml(d.title || d.doc_key) + '</div>'
      + (meta ? '<div class="ide-doc-meta">' + escapeHtml(meta) + '</div>' : '')
      + (tags ? '<div class="ide-doc-tags">' + escapeHtml(tags) + '</div>' : '')
      + '</div>';
  }).join('');
  $$('#ide-doc-list .ide-doc-item').forEach((row) => {
    row.onclick = () => openIdeFile(row.dataset.path);
  });
}
