'use strict';

/* ============================================================
   第三方库异步就绪后的「补齐渲染」
   （首屏不等 CDN；库到位后再补公式 / 代码高亮 / 图表 / 图谱）
   ============================================================ */
function onLibReady(name) {
  if (name === 'katex') { upgradeMath(); return; }
  if (name === 'hljs') { upgradeCode(); return; }
  if (name === 'echarts') {
    const m = $('#usage-modal');
    if (isOpen(m)) {
      const t = $('#usage-modal .tab.active');
      openUsage(t ? t.dataset.usageRange : 'day7');
    }
    return;
  }
  if (name === 'vis') {
    const m = $('#kb-modal');
    if (isOpen(m) && state.kbData) drawKbGraph();
  }
}
window.__onLibReady = onLibReady;

/* 自绘标题栏（仅 Electron 壳）：按钮走 preload 暴露的 window.gmWin。
   双击拖动区 = 最大化 / 还原；最大化状态由壳推送，用来切换「最大化 / 还原」图标。 */
function bindTitlebar() {
  const w = window.gmWin;
  if (!w) return;
  const min = $('#tb-min');
  const max = $('#tb-max');
  const close = $('#tb-close');
  if (min) min.onclick = () => w.minimize();
  if (max) max.onclick = () => w.toggleMaximize();
  if (close) close.onclick = () => w.close();
  const bar = $('#titlebar');
  if (bar) {
    bar.addEventListener('dblclick', (e) => {
      if (e.target && e.target.closest && e.target.closest('.tb-actions')) return;
      w.toggleMaximize();
    });
  }
  if (max && typeof w.onMaximized === 'function') {
    w.onMaximized((on) => {
      const use = max.querySelector('use');
      if (use) use.setAttribute('href', on ? '#i-win-restore' : '#i-win-max');
      max.title = on ? '还原' : '最大化';
      max.setAttribute('aria-label', max.title);
    });
  }
}

function upgradeMath() {
  if (!window.katex) return;
  document.querySelectorAll('code.math-pending').forEach((el) => {
    const tex = el.getAttribute('data-tex') || '';
    const display = el.getAttribute('data-display') === '1';
    try {
      const tmp = document.createElement('span');
      tmp.innerHTML = katex.renderToString(tex, {
        displayMode: display, throwOnError: false, strict: false,
      });
      if (tmp.firstChild) el.replaceWith(tmp.firstChild);
    } catch (e) { }
  });
  // 缓存里可能还留着 math-pending 的 HTML；清掉，后续重渲染直接出公式
  try {
    if (typeof clearMarkdownCache === 'function') clearMarkdownCache();
  } catch (e) { }
}

// 消息重新渲染时可能再次插入 math-pending（尤其是 KaTeX 晚于首屏加载）。
// 监听 #chat，发现新的待渲染公式就补一次。
let _mathUpgradeTimer = null;
function scheduleMathUpgrade() {
  if (!window.katex) return;
  clearTimeout(_mathUpgradeTimer);
  _mathUpgradeTimer = setTimeout(upgradeMath, 30);
}
function watchMathNodes() {
  if (typeof MutationObserver === 'undefined') return;
  const obs = new MutationObserver((mutations) => {
    for (const mutation of mutations) {
      for (const node of mutation.addedNodes) {
        if (!node || node.nodeType !== 1) continue;
        const hit = (node.matches && node.matches('code.math-pending'))
          || (node.querySelector && node.querySelector('code.math-pending'));
        if (hit) { scheduleMathUpgrade(); return; }
      }
    }
  });
  const chatEl = document.getElementById('chat');
  if (chatEl) obs.observe(chatEl, { childList: true, subtree: true });
}

function upgradeCode() {
  if (!window.hljs) return;
  document.querySelectorAll('#chat pre code.hljs').forEach((el) => {
    if (el.dataset.upgraded) return;
    el.dataset.upgraded = '1';
    const lang = el.getAttribute('data-lang');
    const raw = el.textContent;
    try {
      el.innerHTML = (lang && hljs.getLanguage(lang))
        ? hljs.highlight(raw, { language: lang }).value
        : hljs.highlightAuto(raw).value;
    } catch (e) { }
  });
}

/* Ctrl+Shift+V（Solo）：切换 AI 回复的「渲染 / 源码」。
   有选区时用选区所在的那条；否则用最后一条 AI 回复。 */
function toggleChatRenderByKey() {
  const rows = Array.prototype.slice.call(
    document.querySelectorAll('#chat .msg.ai[data-idx]'));
  if (!rows.length) { toast('还没有可切换的 AI 回复'); return; }
  let row = null;
  try {
    const sel = window.getSelection();
    if (sel && sel.rangeCount && !sel.isCollapsed) {
      let node = sel.getRangeAt(0).startContainer;
      while (node && node !== document) {
        if (node.nodeType === 1 && node.classList && node.classList.contains('msg')) break;
        node = node.parentNode;
      }
      if (node && node.nodeType === 1 && node.classList.contains('ai')
          && node.dataset.idx !== undefined) {
        row = node;
      }
    }
  } catch (e) { /* 读不到选区就退回最后一条 */ }
  if (!row) row = rows[rows.length - 1];
  if (typeof row._raw !== 'string' || !row._content) {
    toast('这条回复暂不支持源码视图');
    return;
  }
  toggleRawView(row);
}

/* ============================================================
   初始化
   ============================================================ */
async function init() {
  const res = await fetch('/api/state', { headers: { 'X-Auth-Token': TOKEN } });
  const data = await res.json();
  state.config = data.config;
  state.profile = data.profile;
  applySessionsPayload(data.sessions);
  state.kbs = data.kbs || [];
  state.builtinPresets = data.builtin_presets || {};
  state.builtinPresetLabel = data.builtin_preset_label || '默认（内置）';
  state.searchProviders = data.search_providers || {};
  state.chatSearchAvailable = !!data.chat_search_available;
  state.chatVisionAvailable = !!data.chat_vision_available;
  state.searchEffective = data.search_effective || '';
  state.pendingOpen = data.pending_open || '';
  // 会话运行态：sid -> 'approval' | 'ask'（侧栏显示「待批准 / 待回答」）
  state.pending = data.pending && typeof data.pending === 'object' ? data.pending : {};
  state.contextMenu = data.context_menu || {};
  state.relations = data.relations || [];
  renderRelationHelp();
  watchMathNodes();
  // 本地 KaTeX 正常会很快加载；8 秒后仍有待渲染公式，说明库没起来（文件缺失/被拦截）
  setTimeout(() => {
    if (!window.katex && document.querySelector('code.math-pending')) {
      toast('公式库未加载，公式会显示为源码', 'error');
    }
  }, 8000);

  // 上次窗口异常关闭 / 页面刷新时后端可能还有任务在跑：先记进 streamingSids，
  // 侧栏才会显示「生成中」；再自动挂上 attach，任务结束后自动刷新。
  const _running = Array.isArray(data.streaming) ? data.streaming : [];
  state.streamingSids = {};
  _running.forEach((id) => { state.streamingSids[String(id)] = true; });
  // 主题以本地记录优先（<head> 里已经按它画了首帧，这里保持一致，避免闪一下旧配色）；
  // 没有本地记录时再用配置里的值，老配置 light / dark 仍然属于「现代扁平化」。
  let bootTheme = state.config.theme;
  try { bootTheme = localStorage.getItem('gm.theme') || bootTheme; } catch (e) { /* 忽略 */ }
  applyTheme(bootTheme);
  renderSessions();
  renderMessages();
  // 上次断线 / 重开窗口时丢失的审批卡、提问卡：按服务端快照补回来
  if (typeof renderPendingCards === 'function') renderPendingCards();
  renderContextBar(data.context);
  // 远端流（刷新后本地没有流对象）也要立刻把「停止」按钮亮起来：
  // 以前只填了 streamingSids，没有同步 UI，结果界面显示「模型输出中」却没有停止键。
  if (typeof refreshStreamingUI === 'function') refreshStreamingUI();
  if (_running.indexOf(String(state.sessions.current)) >= 0
      && typeof keepRecovering === 'function') {
    toast('检测到后台任务仍在运行，完成后会自动刷新');
    keepRecovering(state.sessions.current).catch(() => {});
  }
  // 首屏先渲染完；把已经就绪的库补齐（未就绪的等 onLibReady 回调）
  Object.keys(window.__libs || {}).forEach(onLibReady);

  $('#btn-new').onclick = newSession;
  $('#btn-send').onclick = sendMessage;
  // 「回到实时输出」：把后台仍在跑的任务接回前端完整流式渲染
  if ($('#btn-resume')) $('#btn-resume').onclick = () => resumeLiveOutput(state.sessions.current);
  $('#attach-remove').onclick = clearAttach;
  $('#btn-stop').onclick = stopStreaming;
  if ($('#queue-cancel')) $('#queue-cancel').onclick = () => cancelSendQueue();
  if ($('#queue-toggle')) $('#queue-toggle').onclick = () => toggleSendQueue();
  $('#btn-close-ctx').onclick = () => closeCtxDetail();
  $('#btn-close-ctx2').onclick = () => closeCtxDetail();
  $('#ctx-detail-compress').onclick = () => compressContextManually();
  $('#ctx-detail-restore').onclick = () => ctxRestore();
  // 上下文明细：同一个弹窗里的两个页签（占用明细 / token 明细）
  document.querySelectorAll('.ctx-tab').forEach((b) => {
    b.onclick = () => {
      document.querySelectorAll('.ctx-tab').forEach((x) => {
        const on = x === b;
        x.classList.toggle('active', on);
        x.setAttribute('aria-selected', on ? 'true' : 'false');
      });
      const name = b.dataset.ctxTab;
      const up = $('#ctx-pane-usage');
      const ip = $('#ctx-pane-input');
      if (up) up.classList.toggle('hidden', name !== 'usage');
      if (ip) ip.classList.toggle('hidden', name !== 'input');
    };
  });
  // 设置入口 / 快捷键由 settings.js 在 DOM 就绪后统一绑定，避免和启动顺序耦合
  bindKbEvents();
  bindNotesEvents();
  bindTitlebar();

  // 弹窗无障碍语义：7 个 .modal 共用，一处补齐
  // （原来都没有 role="dialog" / aria-modal，屏幕阅读器不知道这是对话框，也无法把焦点限制进来）
  $$('.modal').forEach((m) => {
    if (!m.hasAttribute('role')) m.setAttribute('role', 'dialog');
    m.setAttribute('aria-modal', 'true');
    if (!m.hasAttribute('aria-label')) {
      const t = m.querySelector('.settings-title, .modal-head-title, .modal-tabs');
      const txt = t ? String(t.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 40) : '';
      if (txt) m.setAttribute('aria-label', txt);
    }
  });

  // 六个弹窗支持按住标题栏拖动移动
  $$('.modal .modal-card').forEach((card) => {
    const head = card.querySelector('.modal-head');
    if (head) makeDraggable(card, head);
  });

  // 刷新页面：和 Ctrl+R 等价（后端仍在跑的任务会在重载后自动挂回）
  $('#btn-reload').onclick = () => location.reload();

  $('#btn-theme').onclick = () => {
    const style = document.documentElement.getAttribute('data-style') || 'aurora';
    if (style === 'flat') {
      // 现代扁平化：这个按钮只负责浅色 ↔ 深色
      const cur = document.documentElement.getAttribute('data-theme') || 'light';
      const next = cur === 'dark' ? 'light' : 'dark';
      applyTheme(next);
      api('/api/config', Object.assign({}, state.config, { theme: next })).then(() => {
        state.config.theme = next;
      });
      return;
    }
    // 雾光：左下角按钮是这套主题专属的「氛围」入口（静谧 → 流动 → 辉光）
    if (typeof cycleMood === 'function') cycleMood();
  };

  // 雾光卡片：指针跟随高光（CSS 用 --mx / --my 画一层很淡的径向光）
  // 只在悬停到卡片时写变量，避免全局 pointermove 频繁触发布局
  document.addEventListener('pointermove', (e) => {
    if (document.documentElement.getAttribute('data-style') !== 'aurora') return;
    const el = e.target && e.target.closest
      ? e.target.closest('.hall-card,.work-task,.mk-plugin,.mk-card,.theme-card,.mood-card')
      : null;
    if (!el) return;
    const r = el.getBoundingClientRect();
    el.style.setProperty('--mx', (e.clientX - r.left) + 'px');
    el.style.setProperty('--my', (e.clientY - r.top) + 'px');
  }, { passive: true });

  // 深度思考开关 + 档位（状态存进 config.json，重启保留）
  // 档位不再用常驻下拉：和 Agent 一样收进小箭头浮层，工具栏只留一个胶囊
  const thinkPill = $('#btn-think');
  const thinkMore = $('#btn-think-more');
  const thinkMenu = $('#think-menu');
  state.thinkEnabled = !!state.config.chat_think_enabled;
  state.thinkLevel = state.config.chat_think_level || 'high';

  function closeThinkMenu() {
    thinkMenu.classList.add('hidden');
    thinkMore.setAttribute('aria-expanded', 'false');
  }

  function toggleThinkMenu(force) {
    const open = (typeof force === 'boolean') ? force : thinkMenu.classList.contains('hidden');
    thinkMenu.classList.toggle('hidden', !open);
    thinkMore.setAttribute('aria-expanded', open ? 'true' : 'false');
  }

  function syncThinkUI() {
    thinkPill.classList.toggle('active', !!state.thinkEnabled);
    thinkMore.classList.toggle('hidden', !state.thinkEnabled);
    if (!state.thinkEnabled) closeThinkMenu();      // 关掉思考时不留一个悬空浮层
    $$('#think-menu .think-opt').forEach((b) => {
      b.classList.toggle('active', b.dataset.level === state.thinkLevel);
    });
  }
  syncThinkUI();

  thinkPill.onclick = () => {
    state.thinkEnabled = !state.thinkEnabled;
    syncThinkUI();
    api('/api/config', { chat_think_enabled: state.thinkEnabled })
      .then(() => { state.config.chat_think_enabled = state.thinkEnabled; });
  };

  thinkMore.onclick = (e) => { e.stopPropagation(); toggleThinkMenu(); };

  $$('#think-menu .think-opt').forEach((btn) => {
    btn.onclick = (e) => {
      e.stopPropagation();
      state.thinkLevel = btn.dataset.level;
      syncThinkUI();
      closeThinkMenu();
      api('/api/config', { chat_think_level: state.thinkLevel })
        .then(() => { state.config.chat_think_level = state.thinkLevel; });
    };
  });

  // 点外部 / Esc 收起，与 Agent 浮层同一套交互
  document.addEventListener('click', (e) => {
    if (!e.target.closest || !e.target.closest('.think-wrap')) closeThinkMenu();
  });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeThinkMenu(); });

  // 联网搜索开关（未配置密钥时不允许点亮）
  state.searchEnabled = !!state.config.search_enabled && hasSearchKey();
  if (state.config.search_enabled && !hasSearchKey()) {
    api('/api/config', { search_enabled: false }).then(() => { state.config.search_enabled = false; });
  }
  syncSearchPill();

  // 语音降噪（持久化到 config.speech_denoise；开关在「设置 → 语音与朗读」）
  state.denoise = !!state.config.speech_denoise;
  syncDenoisePill();

  $('#btn-search').onclick = () => {
    if (!state.searchEnabled && !hasSearchKey()) {
      toast(state.config.search_provider === 'auto'
        ? '自动模式下所有引擎都被关闭了，请在「设置 → 联网与数据源」至少勾选一个'
        : '尚未配置搜索 API 密钥，请在「设置 → 联网与数据源」中填写');
      return;
    }
    state.searchEnabled = !state.searchEnabled;
    syncSearchPill();
    api('/api/config', { search_enabled: state.searchEnabled })
      .then(() => { state.config.search_enabled = state.searchEnabled; });
  };

  // 用量统计
  $('#btn-close-usage').onclick = closeUsage;
  $$('#usage-modal .tab').forEach((t) => {
    t.onclick = () => openUsage(t.dataset.usageRange);
  });
  window.addEventListener('resize', () => {
    if (usageChart) usageChart.resize();
    if (usageBars) usageBars.resize();
  });

  // 双击会话名可重命名
  $('#session-list').addEventListener('dblclick', (e) => {
    const item = e.target.closest('.session-item');
    if (!item) return;
    const idx = Array.from($('#session-list').children).indexOf(item);
    const s = state.sessions.sessions[idx];
    if (s) renameSession(s.id);
  });

  // 输入框
  const input = $('#input');
  input.addEventListener('input', () => { autoGrow(); hideSttChip(); });
  syncSendBtn();                 // 初始：输入为空时发送键置灰
  input.addEventListener('keydown', (e) => {
    // 中文输入法组词时，Enter 是确认候选字，不能发送
    if (isImeComposing(e)) return;
    // Enter 发送 —— 键位可改（设置 → 快捷键，或按 ? 打开改键清单）
    if (e.shiftKey) return;                        // Shift+Enter 始终是换行，不参与改键
    const hit = hkHit(e, ['composer']);
    if (!hit || hit.id !== 'cmp.sendEnter') return;
    e.preventDefault();
    sendMessage();
  });
  initComposer();                // 输入框增强（composer.js）：补全 / 指令 / 清空 / 快捷键 / AI 优化
  initHotkeys();                 // 快捷键：实时 HUD + 「?」清单（hotkeys.js）
  initHall();                    // 功能大厅：卡片入口与返回（hall.js）

  // 语音输入（不传参数就是 Solo 主输入框）
  $('#btn-mic').onclick = () => toggleMic();
  $('#stt-revert').onclick = revertSttChip;

  // Agent：开关 + 沙箱目录
  $('#btn-agent').onclick = toggleAgent;
  $('#btn-sandbox').onclick = pickSandbox;
  if ($('#btn-sandbox-open')) $('#btn-sandbox-open').onclick = openSandboxFolder;
  $('#btn-sandbox-path').onclick = pickSandboxPath;
  if ($('#btn-sandbox-clear')) $('#btn-sandbox-clear').onclick = clearSandbox;
  // 写入审批档位：手动 / 自动 / 完全访问 / 自定义
  document.querySelectorAll('#agent-menu .approve-opt').forEach((btn) => {
    btn.onclick = (e) => {
      e.stopPropagation();          // 别让「点外部收起」把点击吃掉
      setApprovalMode(btn.dataset.mode);
    };
  });
  // Agent 设置浮层（小箭头）：点外部或按 Esc 收起
  if ($('#btn-agent-more')) {
    $('#btn-agent-more').onclick = (e) => { e.stopPropagation(); toggleAgentMenu(); };
    document.addEventListener('click', (e) => {
      if (!e.target.closest || !e.target.closest('.agent-wrap')) closeAgentMenu();
    });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeAgentMenu(); });
  }
  syncAgentUI();

  // IDE / Solo 模式
  const PREFETCH_GROUP = { ide: 'xterm', hall: 'hall', work: 'work' };
  $$('#mode-switch .ms-btn').forEach((b) => {
    b.onclick = () => setUiMode(b.dataset.mode);
    // 悬停 / 聚焦就提前下载该模式的重脚本，点击时少等一次网络往返
    const warm = () => {
      const g = PREFETCH_GROUP[b.dataset.mode];
      if (g && typeof gmPrefetchGroup === 'function') gmPrefetchGroup(g);
    };
    b.addEventListener('mouseenter', warm, { once: true });
    b.addEventListener('focus', warm, { once: true });
  });
  const warmPlugins = () => {
    if (typeof gmPrefetchGroup === 'function') gmPrefetchGroup('plugins');
  };
  const pluginsBtn = $('#btn-plugins');
  if (pluginsBtn) {
    pluginsBtn.addEventListener('mouseenter', warmPlugins, { once: true });
    pluginsBtn.addEventListener('focus', warmPlugins, { once: true });
  }
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    // 几个弹窗原先都不支持 Esc（只能点 ✕），这里一并接上，键盘用户不必再去够鼠标
    const um = $('#usage-modal');
    if (um && !um.classList.contains('hidden')) { closeUsage(); return; }
    const nm = $('#notes-modal');
    if (nm && !nm.classList.contains('hidden')) { closeNotesModal(); return; }
    const km = $('#kb-modal');
    if (km && !km.classList.contains('hidden')) { closeKbModal(); return; }
  });
  $('#ide-refresh').onclick = () => loadIdeTree();
  ideInitNotice();
  if ($('#ide-tree')) $('#ide-tree').addEventListener('keydown', ideTreeKeydown);
  $('#ide-newfile').onclick = () => ideNew(false);
  $('#ide-newdir').onclick = () => ideNew(true);
  $('#ide-rename').onclick = ideRename;
  $('#ide-deldir').onclick = ideDelete;
  $('#ide-save').onclick = saveIdeFile;
  if ($('#ide-diff-btn')) $('#ide-diff-btn').onclick = () => ideShowDiff(state.ideOpen);
  $('#ide-text').addEventListener('input', () => ideSetDirty(state.ideOpen, true));
  // 活动栏（点当前已激活的图标 = 收起文件管理区，见 ideToggleSide）
  $$('#ide-panel .act-btn[data-act]').forEach((b) => {
    b.onclick = () => ideToggleSide(b.dataset.act);
  });
  if ($('#ide-open-vscode')) $('#ide-open-vscode').onclick = ideOpenInVsCode;
  if ($('#ide-anno-add')) $('#ide-anno-add').onclick = () => ideAnnoAdd();
  if ($('#ide-anno-refresh')) $('#ide-anno-refresh').onclick = () => ideAnnoLoad(state.ideOpen);
  if ($('#ide-doc-refresh')) $('#ide-doc-refresh').onclick = () => ideDocsLoad($('#ide-doc-q') ? $('#ide-doc-q').value : '');
  if ($('#ide-doc-q')) {
    $('#ide-doc-q').addEventListener('input', () => {
      clearTimeout(window.__ideDocTimer);
      window.__ideDocTimer = setTimeout(
        () => ideDocsLoad($('#ide-doc-q').value), 250);
    });
  }
  // 沙箱内搜索
  if ($('#ide-search-input')) {
    $('#ide-search-input').addEventListener('keydown', (e) => {
      if (isImeComposing(e)) return;
      if (e.key === 'Enter') { e.preventDefault(); ideSearch(); }
    });
  }
  // 命令面板
  if ($('#ide-cmd-input')) {
    $('#ide-cmd-input').addEventListener('input', (e) => { state.idePaletteSel = 0; idePaletteRender(e.target.value); });
    $('#ide-cmd-input').addEventListener('keydown', (e) => {
      if (isImeComposing(e)) return;
      const items = state.idePaletteItems || [];
      if (e.key === 'ArrowDown') { e.preventDefault(); state.idePaletteSel = Math.min(items.length - 1, state.idePaletteSel + 1); idePaletteRender($('#ide-cmd-input').value); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); state.idePaletteSel = Math.max(0, state.idePaletteSel - 1); idePaletteRender($('#ide-cmd-input').value); }
      else if (e.key === 'Enter') { e.preventDefault(); idePaletteRun(state.idePaletteSel); }
      else if (e.key === 'Escape') { e.preventDefault(); idePaletteClose(); }
    });
    $('#ide-cmd').addEventListener('mousedown', (e) => { if (e.target && e.target.id === 'ide-cmd') idePaletteClose(); });
  }
  if ($('#ide-info-close')) $('#ide-info-close').onclick = () => $('#ide-info').classList.add('hidden');
  // IDE 快捷键（仅 IDE 模式生效；键位可在「?」清单里改）
  document.addEventListener('keydown', (e) => {
    if (state.uiMode !== 'ide') return;
    const hit = hkHit(e, ['ide']);
    if (!hit) return;
    // 调试键只在焦点不在输入框 / 编辑器文本区时生效（与旧实现一致）
    if (hit.id.indexOf('ide.debug') === 0) {
      const tag = (e.target && e.target.tagName || '').toLowerCase();
      if (tag === 'input' || tag === 'textarea' || tag === 'select') return;
      if (document.querySelector('.modal:not(.hidden)')) return;   // 弹窗打开时不启动调试
    }
    e.preventDefault();
    if (hit.id === 'ide.save') saveIdeFile();
    else if (hit.id === 'ide.open') idePaletteOpen('file');
    else if (hit.id === 'ide.palette') idePaletteOpen('cmd');
    else if (hit.id === 'ide.sidebar') ideToggleSidebar();
    else if (hit.id === 'ide.terminal') ideTermToggle();
    else if (hit.id === 'ide.terminalNew') ideTermNew();
    else if (hit.id.indexOf('ide.debug') === 0
             && typeof ideDebugShortcut === 'function') ideDebugShortcut(hit.id);
  });
  // 对话内查找（Ctrl+F）；设置面板打开时不接管
  document.addEventListener('keydown', (e) => {
    const m = $('#modal');
    if (m && !m.classList.contains('hidden')) return;
    // 编辑器聚焦时把 Ctrl+F 让给 Monaco —— 那是 VS Code 的「编辑器内查找」。
    // （这条就是需求里说的"冲突时按 VS Code 语义分流"：同一组合键在编辑器里/外含义不同。）
    if (typeof ideEditorFocused === 'function' && ideEditorFocused()) return;
    const hit = hkHit(e, ['global']);
    if (!hit || hit.id !== 'global.find') return;
    e.preventDefault();
    findOpen();
  });
  setupFindBar();

  // Ctrl+Shift+V：切换「渲染 / 源码」。笔记里是预览 ↔ 编辑；Solo 里切最后一条
  // AI 回复（有选区时切选区所在的那条）。IDE 模式让给 Monaco 的 Markdown 预览。
  document.addEventListener('keydown', (e) => {
    const hit = hkHit(e, ['global']);
    if (!hit || hit.id !== 'global.render') return;
    const settings = $('#modal');
    if (settings && !settings.classList.contains('hidden')) return;   // 设置弹窗优先
    const notes = $('#notes-modal');
    if (notes && !notes.classList.contains('hidden')) {
      if (!state.noteCurrent) { toast('先打开一篇笔记'); return; }
      e.preventDefault();
      // 块编辑 ↔ Markdown 源码（预览 / 分栏用顶部的视图切换）
      const next = typeof notesToggleView === 'function' ? notesToggleView() : '';
      toast(next === 'source' ? '已切换到 Markdown 源码' : '已切换到块编辑');
      return;
    }
    if (state.uiMode !== 'solo') return;      // IDE 的 Ctrl+Shift+V 有别的语义
    e.preventDefault();
    toggleChatRenderByKey();
  });

  // 会话搜索：居中浮层（匹配标题与对话正文）
  if (typeof bindSessionSearch === 'function') bindSessionSearch();
  // 会话记录：按目录分组 / 平铺切换
  if (typeof bindSessionGroupToggle === 'function') bindSessionGroupToggle();
  // 上次关在剧场时，若当前会话不是剧场会话，就落到大厅，避免会话列表空白
  const embedCfg = window.__GM_EMBED__ || {};
  let startMode = (state.config && state.config.ui_mode) || 'solo';
  // 嵌入模式用 URL 指定工作区（?mode=ide|hall|work），优先于上次配置
  if (embedCfg.mode) startMode = embedCfg.mode;
  // GAL 剧场已下线：历史配置里若是 gal，统一落到大厅
  if (startMode === 'gal') startMode = 'hall';
  applyUiMode(startMode);
  // ?hall=<id>：嵌入时直接展开某个大厅模块（模块脚本按需加载，等加载完再打开）
  if (embedCfg.hall && typeof hallOpenFeature === 'function') {
    const openEmbedHall = () => {
      try { hallOpenFeature(embedCfg.hall); } catch (e) { /* 模块 id 不存在就留在大厅首页 */ }
    };
    if (typeof window.gmLoadGroup === 'function') {
      window.gmLoadGroup('hall').then(openEmbedHall).catch(() => { /* 加载失败时留在大厅首页 */ });
    } else {
      openEmbedHall();
    }
  }
  // ?settings=<tab>：嵌入时直接落到某个设置页签
  if (embedCfg.settings) {
    try { window.gmOpenSettings({ tab: embedCfg.settings }); } catch (e) { /* 非法页签忽略 */ }
  }
  // 右键菜单冷启动：这时 SSE 还没连上，服务端把请求放在 /api/state 里兜底
  if (state.pendingOpen && typeof openPathInApp === 'function') {
    const p = state.pendingOpen;
    setTimeout(() => openPathInApp(p), 0);
  }
  // 设置页「数据与高级」的右键菜单按钮：按已注册状态给初始文案
  const cmBtn = $('#app-context-menu');
  if (cmBtn) {
    const on = !!(state.contextMenu && state.contextMenu.installed);
    cmBtn.dataset.installed = on ? '1' : '0';
    cmBtn.textContent = on ? '移除右键菜单' : '注册右键菜单';
  }

  // 图片
  // 附件：一个按钮 + 下拉菜单（上传图片 / 上传文档）
  const attachMenu = $('#attach-menu');
  const hideAttachMenu = () => { if (attachMenu) attachMenu.classList.add('hidden'); };
  if ($('#btn-attach')) {
    $('#btn-attach').onclick = (e) => {
      e.stopPropagation();
      if (attachMenu) attachMenu.classList.toggle('hidden');
    };
  }
  if (attachMenu) {
    attachMenu.onclick = (e) => {
      const b = e.target.closest('[data-attach]');
      if (!b) return;
      hideAttachMenu();
      if (b.dataset.attach === 'image') $('#file-input').click();
      else $('#doc-input').click();
    };
  }
  document.addEventListener('click', hideAttachMenu);
  $('#file-input').onchange = (e) => {
    if (e.target.files && e.target.files[0]) attachImageBlob(e.target.files[0]);
    e.target.value = '';
  };
  $('#doc-input').onchange = (e) => {
    // 支持一次选多个：全部追加到待发送附件（不再互相覆盖）
    for (const f of (e.target.files || [])) attachDocBlob(f);
    e.target.value = '';
  };
  // 粘贴：图片走图片通道，其余文件走文档通道（读剪贴板里的文件需 Chrome 91+ / Edge 等 Chromium 桌面版）
  document.addEventListener('paste', (e) => {
    const dt = e.clipboardData || {};
    const picked = [];
    for (const it of (dt.items || [])) {
      if (it.kind === 'file') { const f = it.getAsFile(); if (f) picked.push(f); }
    }
    if (!picked.length && dt.files) for (const f of dt.files) picked.push(f);
    if (!picked.length) return;                 // 剪贴板里没有文件 → 保留文本粘贴的默认行为
    e.preventDefault();
    // 一次粘贴多个文件时全部附上；文档也支持多个，追加而不是覆盖
    for (const f of picked) {
      if (f.type && f.type.startsWith('image/')) attachImageBlob(f);
      else attachDocBlob(f);
    }
  });
  window.addEventListener('dragover', (e) => e.preventDefault());
  window.addEventListener('drop', (e) => {
    e.preventDefault();
    const files = Array.from((e.dataTransfer && e.dataTransfer.files) || []);
    if (!files.length) return;
    for (const f of files) {
      if (f.type && f.type.startsWith('image/')) attachImageBlob(f);   // 图片 → 图片通道
      else attachDocBlob(f);                                          // 其余文件 → 文档通道
    }
  });

  // 自主推送：定时提醒 / 主动聊天
  if ($('#remind-add')) $('#remind-add').onclick = addReminder;
  if ($('#autochat-on')) $('#autochat-on').onchange = (e) => toggleAutoChat(e.target.checked);
  if ($('#reminders-enabled') && typeof bindReminderSwitches === 'function') bindReminderSwitches();
  if ($('#autochat-min')) $('#autochat-min').onchange = updateAutoChatGap;
  if ($('#autochat-max')) $('#autochat-max').onchange = updateAutoChatGap;
  if ($('#autochat-test')) $('#autochat-test').onclick = testAutoChat;
  startEvents();          // 常驻通道：服务端可在没人提问时主动推送
  // 从后台 / 休眠切回前台：SSE 可能已断，待办状态先对一次账
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && typeof syncPendingState === 'function') syncPendingState();
  });

  // IDE 模式：拖动分隔条调整右侧聊天栏宽度（双击恢复默认；松手后记住）
  (function setupIdeResizer() {
    const rz = $('#ide-resizer');
    if (!rz) return;
    const DEFAULT_W = 460;
    const clamp = (v) => Math.max(300, Math.min(820, Math.round(v)));
    // 按实际栏宽给排版降级：narrow ≤440 收紧内边距，tiny ≤340 让出头像
    const tune = (w) => {
      const col = $('.chat-col');
      if (!col) return;
      col.dataset.cw = w <= 340 ? 'tiny' : (w <= 440 ? 'narrow' : '');
    };
    const apply = (w) => {
      document.documentElement.style.setProperty('--ide-chat-w', w + 'px');
      try { localStorage.setItem('gm.ideChatW', String(w)); } catch (e) { /* 隐私模式 */ }
      tune(w);
    };
    const saved = Number((state.config || {}).ide_chat_width);
    if (saved >= 300 && saved <= 820) apply(saved);
    // 窗口缩放 / 媒体查询（≤1200px 会把栏强制缩到 320）也会改变实际宽度，
    // 只靠拖动时的 apply() 会让 data-cw 停在旧值。盯住真实宽度最稳。
    const col0 = $('.chat-col');
    if (col0 && typeof ResizeObserver === 'function') {
      const ro = new ResizeObserver(() => {
        const w = Math.round(col0.getBoundingClientRect().width);
        if (w > 0) tune(w);
      });
      ro.observe(col0);
    }
    const persist = () => {
      const col = $('.chat-col');
      const w = col ? clamp(col.getBoundingClientRect().width) : DEFAULT_W;
      const cfg = Object.assign({}, state.config || {}, { ide_chat_width: w });
      api('/api/config', cfg).then(() => { state.config = cfg; }).catch(() => {});
    };
    rz.addEventListener('mousedown', (e) => {
      e.preventDefault();
      const col = $('.chat-col');
      if (!col) return;
      const startX = e.clientX;
      const startW = col.getBoundingClientRect().width;
      rz.classList.add('dragging');
      document.body.style.userSelect = 'none';
      // 拖动时给一个宽度气泡：只拖不看数字很难停到想要的宽度
      let tip = null;
      const showTip = (ev, w) => {
        if (!tip) {
          tip = document.createElement('div');
          tip.className = 'ide-resizer-tip';
          document.body.appendChild(tip);
        }
        tip.textContent = w + 'px';
        tip.style.left = ev.clientX + 'px';
        tip.style.top = (ev.clientY + 20) + 'px';
      };
      const onMove = (ev) => {
        const w = clamp(startW - (ev.clientX - startX));
        apply(w);
        showTip(ev, w);
      };
      const onUp = () => {
        rz.classList.remove('dragging');
        document.body.style.userSelect = '';
        if (tip) { tip.remove(); tip = null; }
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
        persist();
      };
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });
    rz.addEventListener('dblclick', () => { apply(DEFAULT_W); persist(); });
  })();

  // IDE 模式：拖动调整左侧文件管理区宽度（双击恢复 260px；松手后记住）
  (function setupIdeSideResizer() {
    const rz = $('#ide-side-resizer');
    if (!rz) return;
    const clamp = (v) => Math.max(180, Math.min(460, Math.round(v)));
    const apply = (w) => document.documentElement.style.setProperty('--ide-side-w', clamp(w) + 'px');
    const saved = Number((state.config || {}).ide_side_width);
    if (saved >= 180 && saved <= 460) apply(saved);
    // 宽度按容器 #ide-sides 计（面板本身是 100%，容器还带着 4px 拖拽条）
    const persist = () => {
      const wrap = $('#ide-sides');
      const w = (wrap && wrap.offsetParent) ? clamp(wrap.getBoundingClientRect().width) : 260;
      const cfg = Object.assign({}, state.config || {}, { ide_side_width: w });
      api('/api/config', cfg).then(() => { state.config = cfg; }).catch(() => {});
    };
    rz.addEventListener('mousedown', (e) => {
      e.preventDefault();
      const wrap = $('#ide-sides');
      if (!wrap) return;
      const startX = e.clientX;
      const startW = wrap.getBoundingClientRect().width;
      rz.classList.add('dragging');
      document.body.style.userSelect = 'none';
      const onMove = (ev) => apply(startW + (ev.clientX - startX));
      const onUp = () => {
        rz.classList.remove('dragging');
        document.body.style.userSelect = '';
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
        persist();
      };
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });
    rz.addEventListener('dblclick', () => { apply(260); persist(); });
  })();

  // 代码块：复制 / 超长折叠展开
  const onCodeBlockClick = (e) => {
    const foldBtn = e.target.closest('[data-fold]');
    if (foldBtn) {
      const block = foldBtn.closest('.code-block');
      const folded = block.classList.toggle('folded');
      foldBtn.textContent = folded ? ('展开全部（' + foldBtn.dataset.total + ' 行）') : '收起代码';
      return;
    }
    const btn = e.target.closest('[data-copy]');
    if (!btn) return;
    const code = btn.closest('.code-block').querySelector('code');
    navigator.clipboard.writeText(code ? code.textContent : '').then(() => toast('已复制代码', 'success'));
  };
  $('#chat').addEventListener('click', onCodeBlockClick);

  // 设置弹窗左侧导航切换
  $$('#modal .settings-nav .snav-btn').forEach((tab) => {
    tab.onclick = () => setSettingsTab(tab.dataset.tab);
  });

  // 自动跟随（贴底 / 回到底部按钮 / 用户上翻暂停）统一由 web/follow.js 绑定：
  // 尺寸突变与用户滚动在那里分开判定，避免「内容一跳就断跟随」。
  bindChatFollow();

  // 输入区高度会变（上下文进度条显隐 / 附件条 / 提示换行 / 文本域长高），
  // 按钮的 bottom 必须跟着走，否则会压到输入框上
  positionToBottomBtn();
  if (window.ResizeObserver) {
    new ResizeObserver(positionToBottomBtn).observe(document.querySelector('.composer'));
  }
  window.addEventListener('resize', positionToBottomBtn);

  // 输入框 / 侧栏 可收起（状态记住，写进 config）
  function applyComposerCollapsed(on, animate) {
    const c = document.querySelector('.composer');
    const body = c && c.querySelector('.composer-body');
    if (body && animate) {
      body.classList.add('animating');
      clearTimeout(c._collapseTimer);
      c._collapseTimer = setTimeout(() => body.classList.remove('animating'), 240);
    }
    if (c) c.classList.toggle('collapsed', !!on);
    const h = $('#composer-handle');
    if (h) {
      const label = on ? '展开输入框' : '收起输入框';
      h.dataset.tip = label;                 // CSS 气泡文案
      h.setAttribute('aria-label', label);
      h.setAttribute('aria-expanded', on ? 'false' : 'true');
    }
  }
  function applySidebarCollapsed(on) {
    const app = $('#app');
    if (app) app.classList.toggle('side-collapsed', !!on);
    // 与 <head> 内联脚本的首帧状态保持同步；展开时移除，避免 CSS 继续压成 0 宽
    document.documentElement.toggleAttribute('data-side-collapsed', !!on);
    // 顶栏会话开关：所有模式共用，收起后仍然可见
    const top = $('#btn-sidebar-top');
    if (top) {
      const label = on ? '展开会话列表' : '收起会话列表';
      top.title = label;
      top.setAttribute('aria-label', label);
      top.setAttribute('aria-controls', 'sidebar');
      top.setAttribute('aria-expanded', on ? 'false' : 'true');
      top.classList.toggle('is-on', !on);
    }
    syncModeSwitchHome();
  }
  applyComposerCollapsed(!!(state.config && state.config.composer_collapsed));

  /* ---------------- 会话侧栏：按模式记忆 ---------------- */
  // Solo 以会话列表为主要工作区，默认展开；IDE / 大厅 / 工作台 / 插件默认收起。
  // 每个模式单独记忆手动开合，互不影响；Solo 仍同步服务端偏好，其余只存本地。
  const SIDEBAR_DEFAULT_COLLAPSED = {
    solo: false, ide: true, hall: true, work: true, plugins: true,
  };
  const SIDEBAR_MODE_KEY = 'gm.sidebarByMode.v1';
  function sidebarIsNarrow() {
    try { return window.matchMedia('(max-width: 900px)').matches; } catch (e) { return false; }
  }
  function sidebarModeMap() {
    try {
      const raw = localStorage.getItem(SIDEBAR_MODE_KEY);
      const obj = raw ? JSON.parse(raw) : null;
      return obj && typeof obj === 'object' && !Array.isArray(obj) ? obj : {};
    } catch (e) { return {}; }
  }
  function sidebarSaveMode(mode, collapsed) {
    try {
      const map = sidebarModeMap();
      map[mode] = collapsed ? 1 : 0;
      localStorage.setItem(SIDEBAR_MODE_KEY, JSON.stringify(map));
    } catch (e) { /* 隐私模式：只影响本次会话 */ }
  }
  function sidebarCollapsedFor(mode) {
    const m = mode || 'solo';
    if (sidebarIsNarrow()) {
      try {
        const sm = localStorage.getItem('gm.sideMobile');
        return sm === null ? true : sm === '1';
      } catch (e) { return true; }
    }
    const map = sidebarModeMap();
    if (Object.prototype.hasOwnProperty.call(map, m)) return !!map[m];
    // 首次迁移：Solo 沿用旧的全局偏好，其余模式按默认策略
    if (m === 'solo' && state.config && typeof state.config.sidebar_collapsed === 'boolean') {
      return !!state.config.sidebar_collapsed;
    }
    return SIDEBAR_DEFAULT_COLLAPSED[m] !== false;
  }
  function applySidebarForMode(mode) {
    const m = mode || state.uiMode || 'solo';
    // 手机 / 平板切模式时把抽屉收起来，避免挡在内容上
    const target = sidebarIsNarrow() ? true : sidebarCollapsedFor(m);
    const app = $('#app');
    if (!app || app.classList.contains('side-collapsed') === target) return;
    app.classList.add('no-side-anim');       // 切模式不做宽度动画，避免 IDE 连续 resize
    applySidebarCollapsed(target);
    requestAnimationFrame(() => requestAnimationFrame(() => app.classList.remove('no-side-anim')));
  }
  window.gmSidebarSync = (mode) => {
    try { applySidebarForMode(mode); } catch (e) { /* 侧栏同步失败不影响切模式 */ }
  };

  applySidebarCollapsed(sidebarCollapsedFor(state.uiMode || 'solo'));
  if ($('#composer-handle')) $('#composer-handle').onclick = () => {
    const c = document.querySelector('.composer');
    const on = !(c && c.classList.contains('collapsed'));
    applyComposerCollapsed(on, true);
    state.config = Object.assign({}, state.config || {}, { composer_collapsed: on });
    api('/api/config', { composer_collapsed: on }).catch(() => {});
  };
  function toggleSidebar(force) {
    const on = (typeof force === 'boolean') ? force : !$('#app').classList.contains('side-collapsed');
    // IDE 模式下侧栏宽度过渡会带着 Monaco 一起变宽：过渡期间冻结自动布局，
    // 否则每一帧都全量 layout，动画看起来就是「卡一下」（见 base.css 的说明）。
    if (state.uiMode === 'ide' && typeof ideFreezeEditorLayout === 'function') {
      ideFreezeEditorLayout(240);
    }
    applySidebarCollapsed(on);
    if (sidebarIsNarrow()) {
      try { localStorage.setItem('gm.sideMobile', on ? '1' : '0'); } catch (e) { /* 隐私模式 */ }
    } else {
      const mode = state.uiMode || 'solo';
      sidebarSaveMode(mode, on);
      // Solo 继续同步服务端偏好（跨设备）；其他模式只存本地，避免互相覆盖
      if (mode === 'solo') {
        state.config = Object.assign({}, state.config || {}, { sidebar_collapsed: on });
        api('/api/config', { sidebar_collapsed: on }).catch(() => {});
      }
    }
  }
  if ($('#btn-sidebar-top')) $('#btn-sidebar-top').onclick = () => toggleSidebar();
  bindChips();

  if (!String(state.config.chat_api_key || '').trim()) {
    setTimeout(() => { window.gmOpenSettings({ tab: 'chat' }); toast('首次使用：请先填写聊天 API 密钥'); }, 400);
  }
  input.focus();
}

document.addEventListener('DOMContentLoaded', init);

/* ============================================================
   模式切换：固定放在左侧导航栏
   ------------------------------------------------------------
   旧壳层曾在侧栏收起时把 #mode-switch 挪到悬浮容器；新壳层把模式切换
   固定在 #app-rail，侧栏开合都不再搬动它，这里只做一次兜底校正。
   ============================================================ */
function syncModeSwitchHome() {
  const ms = document.getElementById('mode-switch');
  const rail = document.getElementById('app-rail');
  if (!ms || !rail) return;
  if (!rail.contains(ms)) rail.insertBefore(ms, rail.children[1] || null);
}

/* Ctrl+Alt+1..4 直接切模式：不依赖侧栏，也不依赖当前模式 */
document.addEventListener('keydown', (e) => {
  if (!e.ctrlKey || !e.altKey || e.shiftKey || e.metaKey) return;
  const mode = { '1': 'solo', '2': 'ide', '3': 'hall', '4': 'work' }[e.key];
  if (!mode || typeof setUiMode !== 'function') return;
  e.preventDefault();
  setUiMode(mode);
});
