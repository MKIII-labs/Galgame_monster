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

function applyUiMode(mode) {
  const m = (mode === 'ide' || mode === 'gal' || mode === 'hall' || mode === 'work')
    ? mode : 'solo';
  state.uiMode = m;
  const main = $('#main');
  if (main) {
    main.classList.toggle('ide-mode', m === 'ide');
    main.classList.toggle('gal-mode', m === 'gal');
    main.classList.toggle('hall-mode', m === 'hall');
    main.classList.toggle('work-mode', m === 'work');
  }
  const panel = $('#ide-panel');
  if (panel) panel.classList.toggle('hidden', m !== 'ide');
  const gp = $('#gal-panel');
  if (gp) gp.classList.toggle('hidden', m !== 'gal');
  const hp = $('#hall-panel');
  if (hp) hp.classList.toggle('hidden', m !== 'hall');
  const wp = $('#work-panel');
  if (wp) wp.classList.toggle('hidden', m !== 'work');
  $$('#mode-switch .ms-btn').forEach((b) => b.classList.toggle('active', b.dataset.mode === m));
  galApplyTheme();                     // GAL 主题随模式进入/退出切换
  applyIdeTheme();                     // IDE 主题同理（属性只在 IDE 模式存在，别的模式零影响）
  if (m !== 'gal') {
    galToggleReview(false); galToggleScript(false);   // 离开 GAL 模式时收起浮层
    galStopAudioAll({ bgm: true });                   // 并停掉朗读 / BGM / 特效音，别在别的模式里继续响
    clearTimeout(_galCursorTimer); _galCursorTimer = null;
    const gp0 = $('#gal-panel'); if (gp0) gp0.classList.remove('gal-cursor-off');
  }
  if (m === 'ide') { ideInitOnce(); loadIdeTree(); }
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
  // 会话列表按模式过滤：进/出剧场时立刻换一份列表
  if (typeof renderSessions === 'function' && state.sessions && state.sessions.sessions) {
    renderSessions();
  }
}

// IDE 首次进入时初始化：装载本地 Monaco，并做一次性绑定
let ideInited = false;

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
  const apply = (collapsed) => {
    ws.classList.toggle('collapsed', collapsed);
    ws.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
    tree.classList.toggle('hidden', collapsed);
  };
  let collapsed = false;
  try { collapsed = localStorage.getItem(IDE_WS_KEY) === '1'; } catch (e) { collapsed = false; }
  apply(collapsed);
  ws.onclick = () => {
    const next = !ws.classList.contains('collapsed');
    apply(next);
    try { localStorage.setItem(IDE_WS_KEY, next ? '1' : '0'); } catch (e) { /* 忽略 */ }
  };
  ws.onkeydown = (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); ws.click(); }
  };
}

function ideInitOnce() {
  if (ideInited) return;
  ideInited = true;
  if (typeof ideTermInitOnce === 'function') ideTermInitOnce();
  state.ideSide = 'explorer';
  state.ideSideHidden = !!(state.config && state.config.ide_side_collapsed);   // 恢复上次的收起状态
  ideSyncSideUI();
  ideBindWorkspace();
  loadMonaco().then((m) => {
    if (!m) return;                 // Monaco 加载失败则继续用内置 textarea，功能不受影响
    state.ideMonaco = m;
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
  xml: 'xml', java: 'java', c: 'c', h: 'c', cpp: 'cpp', cc: 'cpp', hpp: 'cpp',
  cs: 'csharp', go: 'go', rs: 'rust', php: 'php', rb: 'ruby', sql: 'sql', lua: 'lua', txt: 'plaintext',
};
function ideLangOf(rel) {
  const name = (rel || '').split('/').pop() || '';
  const ext = name.indexOf('.') >= 0 ? name.split('.').pop().toLowerCase() : '';
  return IDE_LANGS[ext] || 'plaintext';
}

// 可在 IDE 里预览的图片类型（SVG 仍按文本编辑）
const IDE_IMAGE_EXTS = ['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'ico', 'avif'];
function ideIsImage(rel) {
  const name = (rel || '').split('/').pop() || '';
  const ext = name.indexOf('.') >= 0 ? name.split('.').pop().toLowerCase() : '';
  return IDE_IMAGE_EXTS.indexOf(ext) >= 0;
}

// 编辑区五态视图切换：Monaco / 纯文本兜底 / 图片预览 / diff / Git diff
function ideShowView(kind) {
  const ed = $('#ide-editor'); if (ed) ed.classList.toggle('hidden', kind !== 'editor');
  const fb = $('#ide-fallback'); if (fb) fb.classList.toggle('hidden', kind !== 'fallback');
  const im = $('#ide-image'); if (im) im.classList.toggle('hidden', kind !== 'image');
  const df = $('#ide-diff'); if (df) df.classList.toggle('hidden', kind !== 'diff');
  const gd = $('#ide-git-diff'); if (gd) gd.classList.toggle('hidden', kind !== 'gitdiff');
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

function ideEnsureEditor(monaco) {
  if (state.ideEditor) return state.ideEditor;
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
  };
  if (d.fontSize) opt.fontSize = d.fontSize;
  if (d.tabSize) opt.tabSize = d.tabSize;
  if (d.fontFamily) opt.fontFamily = d.fontFamily;
  if (d.wordWrap) opt.wordWrap = d.wordWrap;
  if (typeof d.mouseWheelZoom === 'boolean') opt.mouseWheelZoom = d.mouseWheelZoom;
  state.ideEditor = monaco.editor.create(host, opt);
  state.ideEditor.onDidChangeModelContent(() => {
    if (state.ideOpen) ideSetDirty(state.ideOpen, true);
  });
  state.ideEditor.onDidChangeCursorPosition((e) => {
    const el = $('#ide-status-pos');
    if (el) el.textContent = '行 ' + e.position.lineNumber + '，列 ' + e.position.column;
  });
  return state.ideEditor;
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
    if (d.scope !== 'editor' || !d.mono || !ov[d.id]) return;   // 没改过键 = 一个字都不动
    (d.keys || []).forEach((k) => {
      const n = ideMonacoKeybinding(k);
      if (n != null) rules.push({ keybinding: n, command: null });
    });
    const n2 = ideMonacoKeybinding(ov[d.id]);
    if (n2 != null) rules.push({ keybinding: n2, command: d.mono, when: 'editorTextFocus' });
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
  state.ideOpen = '';
  state.ideDir = '';
  state.ideChanged.clear();
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
function ideIconClass(name) {
  const n = String(name || '').toLowerCase();
  if (n === '.gitignore' || n === '.gitattributes' || n.endsWith('.lock') || n === 'package-lock.json') {
    return n.endsWith('.lock') ? 'ico-lock' : 'ico-git';
  }
  const ext = n.indexOf('.') >= 0 ? n.split('.').pop() : '';
  const t = IDE_ICON_TYPES[ext];
  return t ? ('ico-' + t) : 'ico-file';
}

// 构建一个可复用的图标元素
function ideIcoEl(name, isDir) {
  const ico = document.createElement('span');
  ico.className = 'ide-ico ' + (isDir ? 'ico-folder' : ideIconClass(name));
  ico.innerHTML = isDir ? IDE_SVG_FOLDER : IDE_SVG_FILE;
  return ico;
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
    node.appendChild(row);
    if (it.dir) {
      const kids = document.createElement('div');
      kids.className = 'ide-children hidden';
      kids.appendChild(buildIdeNodes(it.children || [], rel, d + 1));
      node.appendChild(kids);
      row.onclick = () => ideToggleDir(row, kids);
    } else {
      row.onclick = () => openIdeFile(rel);
    }
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
  const target = (mode === 'ide' || mode === 'gal' || mode === 'hall' || mode === 'work')
    ? mode : 'solo';
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
  (state.ideReview || []).concat(state.ideReviewPending || []).forEach((it) => {
    if (!it || it.path !== rel) return;
    adds += Number(it.adds) || 0;
    dels += Number(it.dels) || 0;
    n += 1;
  });
  return n ? { adds: adds, dels: dels } : null;
}

function renderIdeTabs() {
  const box = $('#ide-tabs');
  if (!box) return;
  box.innerHTML = '';
  (state.ideTabList || []).forEach((rel) => {
    const t = document.createElement('div');
    t.className = 'ide-tab' + (rel === state.ideOpen ? ' active' : '');
    t.title = rel;
    const nm = document.createElement('span');
    nm.className = 'ide-tab-name';
    nm.textContent = rel.split('/').pop();
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
    t.onclick = () => ideActivate(rel);
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
  });
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
  return { adds: 0, dels: 0 };
}

function ideReviewAdd(rel, token, name, args) {
  if (!rel || !token) return;
  rel = String(rel).replace(/\\/g, '/');
  let it = (state.ideReviewPending || []).find((x) => x.path === rel);
  if (!it) {
    it = { path: rel, tokens: [], baseToken: token, name: name || 'edit_file',
           sid: state.sessions.current, adds: 0, dels: 0 };
    state.ideReviewPending.push(it);
  }
  if (it.tokens.indexOf(token) < 0) it.tokens.push(token);
  const c = ideReviewCounts(name, args);
  it.adds += c.adds;
  it.dels += c.dels;
  if (!state.ideDiffs) state.ideDiffs = {};
  state.ideDiffs[rel] = it.baseToken;
  ideUpdateDiffBtn();
}

function ideReviewEndTurn(row) {
  const pending = state.ideReviewPending || [];
  state.ideReviewPending = [];
  if (!pending.length) return;
  const start = state.ideReview.length;
  pending.forEach((it) => state.ideReview.push(it));
  ideUpdateDiffBtn();
  // 聊天里不再插「待审查卡」：待处理状态交给输入框上方的提示栏，
  // 处理完才在聊天里留一行记录（见 ideReviewCardRefresh）。
  if (row) ideReviewCardBuild(row, pending);
  if (!state.ideReview.length) return;
  ideReviewTipShow();
  ideReviewPrefetchStats(pending);
  // 只在能展示 diff 的模式里自动打开；大厅 / GAL 里只入队，等用户点「查看变更」
  if (state.uiMode === 'ide' || state.uiMode === 'solo') {
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
  try { ed.focus(); } catch (e) { /* 忽略 */ }
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

function ideReviewClose() {
  const surface = state.ideReviewSurface;
  state.ideReviewSurface = '';
  if (surface === 'modal') {
    const modal = $('#review-modal');
    if (modal) {
      if (typeof hideWithAnim === 'function') hideWithAnim(modal);
      else modal.classList.add('hidden');
    }
  } else if (state.uiMode === 'ide') {
    ideShowView('editor');
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

/* ---------- 聊天流里的「变更卡」（对齐 VS Code / Trae 的 files changed bar） ---------- */

function ideReviewItemStatus(it) {
  const tokens = it.tokens || [];
  if (!tokens.length) return 'pending';
  const states = tokens.map((t) => state.ideSettledTokens[t]);
  if (states.every((s) => s === 'accepted')) return 'accepted';
  if (states.every((s) => s === 'undone')) return 'undone';
  return 'pending';
}

/* 聊天流里的「变更记录」：只在处理完之后留一行。
   待审查入口交给输入框上方的提示栏，避免同一件事在界面上出现两次。 */
function ideReviewCardBuild(row, items) {
  if (!row || !items || !items.length) return;
  state.changeCards = (state.changeCards || []).filter((c) => c.row !== row);
  state.changeCards.push({ row: row, items: items, el: null });
}

function ideReviewCardRefresh() {
  (state.changeCards || []).forEach((c) => {
    const total = c.items.length;
    const pending = c.items.filter((it) => ideReviewItemStatus(it) === 'pending').length;
    if (pending) return;                       // 还没处理完：提示栏负责
    if (!c.row || !c.row._wrap || !c.row._wrap.isConnected) return;
    const adds = c.items.reduce((n, it) => n + (it.adds || 0), 0);
    const dels = c.items.reduce((n, it) => n + (it.dels || 0), 0);
    const states = c.items.map(ideReviewItemStatus);
    const verb = states.every((s) => s === 'accepted') ? '已保留'
      : states.every((s) => s === 'undone') ? '已撤销' : '已处理';
    if (!c.el) {
      c.el = document.createElement('div');
      c.el.className = 'change-log';
      c.row._wrap.appendChild(c.el);
    }
    c.el.innerHTML = '<span class="change-log-mark">✓</span>' +
      '<span class="change-log-text"></span>' +
      '<span class="change-log-stat"></span>';
    c.el.querySelector('.change-log-text').textContent =
      '本轮 ' + total + ' 个文件的变更' + verb;
    c.el.querySelector('.change-log-stat').innerHTML =
      '<b class="add">+' + adds + '</b> <b class="del">−' + dels + '</b>';
  });
}

/* ---------- 输入框上方的审阅提示栏 ----------
   一轮改完、还有文件没处理时出现；处理完或者点 ✕ 就收起。
   左侧两个图标分别展开「任务项」和「待审查文件」，右侧是本轮的批量操作。 */
const IDE_PLAN_MARK = { done: '✓', doing: '●', pending: '○', blocked: '!' };
let ideTipSid = '';
let ideTipPanel = '';

function ideReviewTipItems() {
  return (state.ideReview || []).filter((it) => ideReviewItemStatus(it) === 'pending');
}

function ideReviewTipShow() {
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
  on('#rt-close', () => ideReviewTipHide());
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
      const i = (state.ideReview || []).indexOf(it);
      if (i >= 0) state.ideReview.splice(i, 1);
      if (state.ideDiffs) delete state.ideDiffs[it.path];
    });
    ideUpdateDiffBtn();
    ideReviewCardRefresh();
    ideReviewTipRefresh();
    if (!state.ideReview.length) ideReviewClose();
    else ideReviewShow(Math.min(state.ideReviewIdx, state.ideReview.length - 1));
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
   显隐只用已有的 .hidden 类（`display:none`），所以编辑区会自动补上空间。 */
const IDE_SIDE_LABEL = { explorer: '资源管理器', search: '沙箱内搜索', git: '源代码管理' };

// 把 state 同步到界面（显隐 + 活动栏高亮 + 悬停提示）
function ideSyncSideUI() {
  const hid = !!state.ideSideHidden;
  const ex = $('#ide-side-explorer');
  const se = $('#ide-side-search');
  const gi = $('#ide-side-git');
  if (ex) ex.classList.toggle('hidden', hid || state.ideSide !== 'explorer');
  if (se) se.classList.toggle('hidden', hid || state.ideSide !== 'search');
  if (gi) gi.classList.toggle('hidden', hid || state.ideSide !== 'git');
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
  ideSyncSideUI();
  idePersistSideHidden();
}

// 收起 ↔ 展开
function ideToggleSidebar() { ideSetSideHidden(!state.ideSideHidden); }

// 切到某个视图（= 展开；命令面板里的「显示资源管理器 / 在沙箱内搜索」也走这里）
function ideSwitchSide(which) {
  state.ideSide = which;
  state.ideSideHidden = false;
  ideSyncSideUI();
  if (which === 'search') { const i = $('#ide-search-input'); if (i) i.focus(); }
  if (which === 'git' && typeof gitRefresh === 'function') gitRefresh();   // 每次切进来都拉一次最新状态
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

async function openIdeFile(rel) {
  if (!rel) return;
  if (state.ideTabList.includes(rel)) { ideActivate(rel); return; }
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
  if (state.ideModels[rel]) { try { state.ideModels[rel].dispose(); } catch (e) { /* 忽略 */ } delete state.ideModels[rel]; }
  const cached = state.ideCache[rel];
  if (cached && cached.image && cached.url) { try { URL.revokeObjectURL(cached.url); } catch (e) { /* 忽略 */ } }
  delete state.ideCache[rel];
  delete state.ideDirtyMap[rel];
  if (state.ideOpen === rel) {
    const next = state.ideTabList[Math.max(0, i - 1)] || '';
    if (next) ideActivate(next);
    else {
      state.ideOpen = '';
      if (state.ideEditor) state.ideEditor.setModel(null);
      const t = $('#ide-text'); if (t) t.value = '';
      const img = $('#ide-image-img'); if (img) img.removeAttribute('src');
      ideShowView('editor');
      renderIdeTabs(); ideSyncTreeActive(); ideUpdateStatus();
      ideUpdateDiffBtn();
    }
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
  } catch (e) {
    toast('保存失败：' + e);
  } finally {
    if (btn) btn.classList.remove('busy');
  }
}

async function ideNew(isDir) {
  const box = $('#ide-tree');
  if (!box) return;
  const base = state.ideDir || '';
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

async function ideRename() {
  if (!state.ideOpen) return;
  const old = state.ideOpen;
  const row = ideFindRow(old);
  if (!row) return;
  const cur = old.indexOf('/') >= 0 ? old.slice(old.lastIndexOf('/') + 1) : old;
  ideInlineEdit(row, cur, async (name) => {
    if (!name || name === cur) return;
    let r;
    try { r = await api('/api/sandbox/rename', { session_id: state.sessions.current, path: old, name }); }
    catch (e) { return toast('重命名失败：' + e); }
    if (!r || !r.ok) { toast((r && r.error) || '重命名失败'); return; }
    const dir = old.indexOf('/') >= 0 ? old.slice(0, old.lastIndexOf('/') + 1) : '';
    const neu = dir + name;
    ideCloseTabForce(old);               // 旧标签直接关掉（用户已经在树里确认过新名字）
    state.ideChanged.delete(old);
    state.ideChanged.add(neu);
    toast('已重命名');
    await loadIdeTree();
    openIdeFile(neu);
  });
}

async function ideDelete() {
  if (!state.ideOpen) return;
  const rel = state.ideOpen;
  ideConfirm('删除文件', '确定删除「' + rel + '」？此操作不可撤销。', '删除', async () => {
    let r;
    try { r = await api('/api/sandbox/delete', { session_id: state.sessions.current, path: rel }); }
    catch (e) { return toast('删除失败：' + e); }
    if (!r || !r.ok) { toast((r && r.error) || '删除失败'); return; }
    toast('已删除');
    const i = (state.ideTabList || []).indexOf(rel);
    if (i >= 0) state.ideTabList.splice(i, 1);
    if (state.ideModels[rel]) { try { state.ideModels[rel].dispose(); } catch (e) { /* 忽略 */ } delete state.ideModels[rel]; }
    delete state.ideCache[rel]; delete state.ideDirtyMap[rel];
    state.ideOpen = '';
    if (state.ideEditor) state.ideEditor.setModel(null);
    renderIdeTabs(); ideUpdateStatus();
    await loadIdeTree();
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

async function ideSearch() {
  const box = $('#ide-search-hits');
  const inp = $('#ide-search-input');
  if (!box || !inp) return;
  const pat = (inp.value || '').trim();
  if (!pat) { box.innerHTML = ''; return; }
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
  { title: '显示资源管理器', run: () => ideSwitchSide('explorer') },
  { title: '收起 / 展开文件管理区', key: 'Ctrl+B', run: () => ideToggleSidebar() },
  { title: '打开 / 关闭终端', key: 'Ctrl+`', run: () => ideTermToggle() },
  { title: '查看 AI 对当前文件的改动', run: () => ideShowDiff(state.ideOpen) },
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
function ideOnToolDone(obj) {
  if (!obj || !obj.write) return;
  const a = obj.args || {};
  const paths = [a.path, a.from, a.to]
    .filter((p) => !!p)
    .map((p) => String(p).replace(/\\/g, '/'));
  paths.forEach((p) => { state.ideChanged.add(p); });
  // 记下这次写操作：同一轮同一文件合并成一条审阅记录
  if (obj.undo && (obj.name === 'write_file' || obj.name === 'edit_file')) {
    const p = String(a.path || '').replace(/\\/g, '/');
    if (p) ideReviewAdd(p, obj.undo, obj.name, a);
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
  bind('review-prev', () => ideReviewStep(-1));
  bind('review-next', () => ideReviewStep(1));
  bind('review-undo', () => ideReviewSettle('undo'));
  bind('review-keep', () => ideReviewSettle('accept'));
  bind('review-close', () => ideReviewClose());
  document.addEventListener('keydown', ideReviewKeydown);
  ideReviewTipBind();
})();
