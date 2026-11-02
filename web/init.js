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
// 监听 #chat / #gal-text，发现新的待渲染公式就补一次。
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
  ['chat', 'gal-text'].forEach((id) => {
    const el = document.getElementById(id);
    if (el) obs.observe(el, { childList: true, subtree: true });
  });
}

function upgradeCode() {
  if (!window.hljs) return;
  document.querySelectorAll('#chat pre code.hljs, #gal-text pre code.hljs').forEach((el) => {
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
  applyTheme(state.config.theme);
  galApplyTheme();
  applyIdeTheme();
  renderSessions();
  renderMessages();
  renderContextBar(data.context);
  if (_running.indexOf(String(state.sessions.current)) >= 0
      && typeof keepRecovering === 'function') {
    toast('检测到后台任务仍在运行，完成后会自动刷新');
    keepRecovering(state.sessions.current).catch(() => {});
  }
  // 首屏先渲染完；把已经就绪的库补齐（未就绪的等 onLibReady 回调）
  Object.keys(window.__libs || {}).forEach(onLibReady);

  $('#btn-new').onclick = newSession;
  $('#btn-send').onclick = sendMessage;
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
  $('#btn-settings').onclick = openModal;
  $('#btn-close-modal').onclick = closeModal;
  $('#btn-cancel').onclick = closeModal;
  $('#btn-save').onclick = saveModal;
  setupSettingsSearch();                                  // 设置内搜索框（只绑一次）
  document.addEventListener('keydown', onSettingsKeydown, true);   // 设置内键盘操作（捕获相）

  bindKbEvents();
  bindNotesEvents();
  bindSavesEvents();
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

  $('#btn-theme').onclick = () => {
    const next = isDarkTheme() ? 'light' : 'dark';
    applyTheme(next);
    api('/api/config', Object.assign({}, state.config, { theme: next })).then(() => {
      state.config.theme = next;
    });
  };

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
      toast('尚未配置搜索 API 密钥，请在「设置 → 联网与数据源」中填写');
      return;
    }
    state.searchEnabled = !state.searchEnabled;
    syncSearchPill();
    api('/api/config', { search_enabled: state.searchEnabled })
      .then(() => { state.config.search_enabled = state.searchEnabled; });
  };

  // 用量统计
  $('#btn-usage').onclick = () => openUsage('day7');
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
  if ($('#btn-outside-read')) $('#btn-outside-read').onclick = toggleOutsideRead;
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
  $$('#mode-switch .ms-btn').forEach((b) => { b.onclick = () => setUiMode(b.dataset.mode); });
  if ($('#gal-char-btn')) $('#gal-char-btn').onclick = () => galToggleChars();
  if ($('#gal-manage')) $('#gal-manage').onclick = () => galToggleChars(true);
  if ($('#gal-script')) $('#gal-script').onclick = () => galToggleScript();
  if ($('#gal-script-close')) $('#gal-script-close').onclick = () => galToggleScript(false);
  if ($('#gal-script-gen')) $('#gal-script-gen').onclick = galGenerateScript;
  if ($('#gal-review')) $('#gal-review').onclick = () => galToggleReview();
  if ($('#gal-review-close')) $('#gal-review-close').onclick = () => galToggleReview(false);
  if ($('#gal-journal')) $('#gal-journal').onclick = () => galToggleJournal();
  if ($('#gal-journal-top')) $('#gal-journal-top').onclick = () => galToggleJournal();
  if ($('#gal-jview-list')) $('#gal-jview-list').onclick = () => galSetJournalView('list');
  if ($('#gal-jview-graph')) $('#gal-jview-graph').onclick = () => galSetJournalView('graph');
  if ($('#gal-journal-close')) $('#gal-journal-close').onclick = () => galToggleJournal(false);
  if ($('#gal-clone')) $('#gal-clone').onclick = () => galToggleClone();
  if ($('#gal-clone-close')) $('#gal-clone-close').onclick = () => galToggleClone(false);
  if ($('#gal-clone-test')) $('#gal-clone-test').onclick = galCloneTest;
  if ($('#gal-clone-save')) $('#gal-clone-save').onclick = galCloneSave;
  if ($('#gal-clone-upload')) {
    $('#gal-clone-upload').onchange = (e) => {
      const f = e.target.files && e.target.files[0];
      e.target.value = '';
      galCloneUpload(f);
    };
  }
  if ($('#gal-more')) $('#gal-more').onclick = (e) => { e.stopPropagation(); galToggleMenu(); };
  if ($('#gal-menu')) {
    $('#gal-menu').addEventListener('click', (e) => { if (e.target.closest('button')) galToggleMenu(false); });
  }
  document.addEventListener('click', (e) => {
    const m = $('#gal-menu');
    if (!m || m.classList.contains('hidden')) return;
    if (e.target.closest('#gal-menu') || e.target.closest('#gal-more')) return;
    galToggleMenu(false);
  });
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    const sv = $('#saves-modal');
    if (sv && !sv.classList.contains('hidden')) { closeSaves(); return; }   // 存档弹窗最上层
    // 另外四个弹窗原先都不支持 Esc（只能点 ✕），这里一并接上，键盘用户不必再去够鼠标
    const um = $('#usage-modal');
    if (um && !um.classList.contains('hidden')) { closeUsage(); return; }
    const nm = $('#notes-modal');
    if (nm && !nm.classList.contains('hidden')) { closeNotesModal(); return; }
    const km = $('#kb-modal');
    if (km && !km.classList.contains('hidden')) { closeKbModal(); return; }
    const gmed = $('#gal-modal');
    if (gmed && !gmed.classList.contains('hidden')) { hideWithAnim(gmed); return; }
    const ic = $('#gal-intro-card');
    if (ic && !ic.classList.contains('hidden')) { galCloseIntroCard(); return; }   // 开场卡在最上层
    const gm = $('#gal-menu');
    if (gm && !gm.classList.contains('hidden')) { galToggleMenu(false); return; }
    const bp = $('#gal-bgs');
    if (bp && !bp.classList.contains('hidden')) { galToggleBgs(false); return; }
    const mp = $('#gal-bgms');
    if (mp && !mp.classList.contains('hidden')) { galToggleBgms(false); return; }
    const cp = $('#gal-clone-panel');
    if (cp && !cp.classList.contains('hidden')) { galToggleClone(false); return; }
    const sp = $('#gal-script-panel');
    if (sp && !sp.classList.contains('hidden')) { galToggleScript(false); return; }
    const p = $('#gal-review-panel');
    if (p && !p.classList.contains('hidden')) { galToggleReview(false); return; }
    const jp = $('#gal-journal-panel');
    if (jp && !jp.classList.contains('hidden')) { galToggleJournal(false); return; }
  });
  if ($('#gal-refresh')) {
    $('#gal-refresh').onclick = async () => { await galLoadCharacters(); toast('角色列表已刷新'); };
  }
  if ($('#gal-chars-search')) {
    $('#gal-chars-search').oninput = () => galRenderCharCards();
    $('#gal-chars-search').onkeydown = (e) => { if (e.key === 'Escape') galToggleChars(false); };
  }
  if ($('#gal-import')) $('#gal-import').onclick = () => $('#gal-import-file').click();
  if ($('#gal-import-file')) {
    $('#gal-import-file').onchange = (e) => {
      const files = e.target.files;
      e.target.value = '';
      galImportPacks(files);
    };
  }
  if ($('#gal-import-dir')) $('#gal-import-dir').onclick = galImportPacksDir;
  if ($('#gal-export')) $('#gal-export').onclick = galExportCharacter;
  if ($('#gal-del-char')) $('#gal-del-char').onclick = galDeleteCharacter;
  // 点击面板外部时收起角色面板
  document.addEventListener('click', (e) => {
    const panel = $('#gal-chars');
    if (!panel || panel.classList.contains('hidden')) return;
    if (e.target.closest('#gal-chars') || e.target.closest('#gal-char-btn') || e.target.closest('#gal-manage')) return;
    galToggleChars(false);
  });
  // 背景管理面板
  if ($('#gal-bg-btn')) $('#gal-bg-btn').onclick = () => galToggleBgs();
  if ($('#gal-bgs-search')) {
    $('#gal-bgs-search').oninput = () => galRenderBgCards();
    $('#gal-bgs-search').onkeydown = (e) => { if (e.key === 'Escape') galToggleBgs(false); };
  }
  if ($('#gal-bg-add')) $('#gal-bg-add').onclick = () => $('#gal-bg-file').click();
  if ($('#gal-bg-file')) {
    $('#gal-bg-file').onchange = (e) => {
      const files = e.target.files;
      e.target.value = '';
      galBgUpload(files);
    };
  }
  if ($('#gal-bg-edit')) $('#gal-bg-edit').onclick = () => galBgOpenEditor();
  if ($('#gal-bg-ed-save')) $('#gal-bg-ed-save').onclick = galBgSaveEditor;
  if ($('#gal-bg-ed-cancel')) $('#gal-bg-ed-cancel').onclick = () => galBgOpenEditor(false);
  if ($('#gal-bg-use')) $('#gal-bg-use').onclick = galBgUse;
  if ($('#gal-bg-del')) $('#gal-bg-del').onclick = galBgDelete;
  // 音乐（BGM）管理面板
  if ($('#gal-bgm-btn')) $('#gal-bgm-btn').onclick = () => galToggleBgms();
  if ($('#gal-bgms-search')) {
    $('#gal-bgms-search').oninput = () => galRenderBgmCards();
    $('#gal-bgms-search').onkeydown = (e) => { if (e.key === 'Escape') galToggleBgms(false); };
  }
  if ($('#gal-bgm-add')) $('#gal-bgm-add').onclick = () => $('#gal-bgm-file').click();
  if ($('#gal-bgm-file')) {
    $('#gal-bgm-file').onchange = (e) => {
      const files = e.target.files;
      e.target.value = '';
      galBgmUpload(files);
    };
  }
  if ($('#gal-bgm-edit')) $('#gal-bgm-edit').onclick = () => galBgmOpenEditor();
  if ($('#gal-bgm-ed-save')) $('#gal-bgm-ed-save').onclick = galBgmSaveEditor;
  if ($('#gal-bgm-ed-cancel')) $('#gal-bgm-ed-cancel').onclick = () => galBgmOpenEditor(false);
  if ($('#gal-bgm-play')) $('#gal-bgm-play').onclick = galBgmPreview;
  if ($('#gal-bgm-use')) $('#gal-bgm-use').onclick = galBgmUse;
  if ($('#gal-bgm-stop')) $('#gal-bgm-stop').onclick = galBgmStop;
  if ($('#gal-bgm-del')) $('#gal-bgm-del').onclick = galBgmDelete;
  if ($('#gal-stats-btn')) $('#gal-stats-btn').onclick = galToggleStats;
  if ($('#gal-stats-reset')) $('#gal-stats-reset').onclick = galResetStats;
  // 角色介绍开场卡
  if ($('#gal-intro-start')) $('#gal-intro-start').onclick = () => {
    galCloseIntroCard();
    const inp = $('#input');
    if (inp) inp.focus();
  };
  if ($('#gal-intro-close')) $('#gal-intro-close').onclick = galCloseIntroCard;
  // 点击面板外部时收起背景面板
  document.addEventListener('click', (e) => {
    const panel = $('#gal-bgs');
    if (!panel || panel.classList.contains('hidden')) return;
    if (e.target.closest('#gal-bgs') || e.target.closest('#gal-bg-btn')) return;
    galToggleBgs(false);
  });
  // 点击面板外部时收起音乐面板
  document.addEventListener('click', (e) => {
    const panel = $('#gal-bgms');
    if (!panel || panel.classList.contains('hidden')) return;
    if (e.target.closest('#gal-bgms') || e.target.closest('#gal-bgm-btn')) return;
    galToggleBgms(false);
  });
  if ($('#gal-tts')) $('#gal-tts').onclick = galToggleTts;
  if ($('#gal-skip')) $('#gal-skip').onclick = galSkipSpeech;
  if ($('#speech-tts-test')) $('#speech-tts-test').onclick = galTestTts;
  if ($('#speech-tts-voices')) $('#speech-tts-voices').onclick = galLoadTtsVoices;
  if ($('#gal-edit')) {
    $('#gal-edit').onclick = () => galOpenEditor(galState.charId || (curSession() || {}).gal_character_id || '', false);
  }
  if ($('#gal-new-char')) $('#gal-new-char').onclick = () => galOpenEditor('', true);
  // 首次进 GAL 的引导气泡：点「知道了」关闭，之后不再出现
  if ($('#gal-tip-ok')) $('#gal-tip-ok').onclick = () => galCloseTip();
  // 对白框右下角的推进指示器：点它等同按空格（打字中 = 立即补全）
  if ($('#gal-adv-hint')) $('#gal-adv-hint').onclick = () => galAdvance();
  // 段末的「轮到你了」提示：点它聚焦底部输入框，直接说点什么
  const nudgeEl = $('#gal-nudge');
  if (nudgeEl) {
    const focusComposer = () => { const i = $('#input'); if (i) { i.focus(); autoGrow(); } };
    nudgeEl.onclick = focusComposer;
    nudgeEl.onkeydown = (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault(); e.stopPropagation();   // 别让它冒泡成「推进对话」
        focusComposer();
      }
    };
  }
  // GAL 输入区：工具条默认收起（只留 ⋯ / 附件或停止 / 麦克风 / 发送），点「⋯」展开或收起
  const moreBtn = $('#btn-more');
  if (moreBtn) {
    moreBtn.onclick = () => {
      const box = document.querySelector('.composer-box');
      if (!box) return;
      const open = box.classList.toggle('gal-tools-open');
      moreBtn.title = open ? '收起输入选项' : '更多输入选项';
    };
  }
  // GAL：聚焦输入框时，立绘轻轻「凑近」一点（表示她在听你说）
  const composerInput = $('#input');
  if (composerInput) {
    composerInput.addEventListener('focus', () => document.documentElement.classList.add('gal-input-focus'));
    composerInput.addEventListener('blur', () => document.documentElement.classList.remove('gal-input-focus'));
  }
  // 角色编辑器：主题色 + AI 帮写
  if ($('#gal-ed-color')) {
    $('#gal-ed-color').oninput = (e) => { galEdColor = e.target.value; };
  }
  if ($('#gal-ed-color-clear')) {
    $('#gal-ed-color-clear').onclick = () => {
      galEdColor = '';
      if ($('#gal-ed-color')) $('#gal-ed-color').value = '#7c9cff';
    };
  }
  if ($('#gal-ed-draft')) $('#gal-ed-draft').onclick = galAiDraft;
  if ($('#gal-auto-btn')) $('#gal-auto-btn').onclick = galToggleAuto;
  // 点击舞台（对白框 / 立绘区）推进；按钮与面板不拦
  const galStage = document.querySelector('#gal-panel .gal-stage');
  if (galStage) {
    galStage.addEventListener('click', (e) => {
      if (e.target.closest('button, a, select, input, textarea, .gal-review, .gal-chars, .gal-menu, .gal-bar')) return;
      galAdvance();
    });
  }
  // 选项现在浮在画面中部（传统日式 galgame 的做法），不再顶起对白框；
  // 只需要跟着对白框自身高度同步立绘层底边（立绘永不被压住）。
  if (typeof ResizeObserver !== 'undefined') {
    const dlg = $('#gal-dialog');
    if (dlg) {
      dlg.style.bottom = '';                        // 清掉旧逻辑可能留下的偏移
      new ResizeObserver(galSyncSpriteLift).observe(dlg);
    }
    window.addEventListener('resize', galSyncSpriteLift);
  }
  document.addEventListener('keydown', (e) => {
    // 焦点在输入框里时 Ctrl 属于编辑操作，不该触发剧情快进
    galState.fast = !isTypingTarget(e.target) && !!e.ctrlKey;
    galHotkeys(e);
  });
  document.addEventListener('keyup', (e) => { galState.fast = !isTypingTarget(e.target) && !!e.ctrlKey; });
  window.addEventListener('blur', () => { galState.fast = false; });   // 失焦兜底
  // 工具条：配音预设快捷切换
  if ($('#gal-tts-preset')) $('#gal-tts-preset').onchange = (e) => galSwitchTtsPreset(e.target.value);
  // 角色编辑器：配音预设
  if ($('#gal-ed-tts-preset')) {
    $('#gal-ed-tts-preset').onchange = (e) => {
      galEdTtsWrite();                       // 先把旧预设的改动写回
      galEdTts.active = e.target.value || '';
      galEdTtsRender();
    };
  }
  if ($('#gal-ed-tts-add')) {
    $('#gal-ed-tts-add').onclick = async () => {
      const n = ((await appPrompt('新建配音预设名称（如：日常 / 激动）')) || '').trim();
      if (!n) return;
      if (galEdTts.presets[n]) { toast('预设「' + n + '」已存在'); return; }
      galEdTtsWrite();
      galEdTts.presets[n] = { provider: '', voice: '', model: '', speed: 1.0, url: '' };
      galEdTts.active = n;
      galEdTtsRender();
    };
  }
  if ($('#gal-ed-tts-del')) {
    $('#gal-ed-tts-del').onclick = () => {
      const n = galEdTts.active;
      if (!n) { toast('请先在下拉里选择要删除的预设'); return; }
      if (!confirm('删除配音预设「' + n + '」？')) return;
      delete galEdTts.presets[n];
      galEdTts.active = Object.keys(galEdTts.presets)[0] || '';
      galEdTtsRender();
    };
  }
  if ($('#gal-ed-tts-test')) $('#gal-ed-tts-test').onclick = galEdTtsTest;
  ['gal-ed-tts-provider', 'gal-ed-tts-voice', 'gal-ed-tts-model', 'gal-ed-tts-speed',
   'gal-ed-tts-url', 'gal-ed-tts-prompt', 'gal-ed-tts-ref', 'gal-ed-tts-lang',
   'gal-ed-tts-gpt', 'gal-ed-tts-sovits'].forEach((id) => {
    const el = $('#' + id);
    if (el) { el.oninput = () => galEdTtsWrite(); el.onchange = () => galEdTtsWrite(); }
  });
  if ($('#gal-ed-ref-upload')) {
    $('#gal-ed-ref-upload').onchange = (e) => {
      const f = e.target.files && e.target.files[0];
      e.target.value = '';
      galUploadRefAudio(f);
    };
  }
  if ($('#gal-modal-close')) $('#gal-modal-close').onclick = () => hideWithAnim($('#gal-modal'));
  if ($('#gal-ed-cancel')) $('#gal-ed-cancel').onclick = () => hideWithAnim($('#gal-modal'));
  if ($('#gal-ed-save')) $('#gal-ed-save').onclick = galSaveEditor;
  if ($('#gal-ed-add-sprite')) $('#gal-ed-add-sprite').onclick = () => galEditorAddRow(null);
  if ($('#gal-ed-upload')) {
    $('#gal-ed-upload').onchange = (e) => {
      const files = e.target.files;
      e.target.value = '';
      galEditorUpload(files);
    };
  }
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
    e.preventDefault();
    if (hit.id === 'ide.save') saveIdeFile();
    else if (hit.id === 'ide.open') idePaletteOpen('file');
    else if (hit.id === 'ide.palette') idePaletteOpen('cmd');
    else if (hit.id === 'ide.sidebar') ideToggleSidebar();
    else if (hit.id === 'ide.terminal') ideTermToggle();
  });
  // 对话内查找（Ctrl+F）；GAL 模式下不接管，设置面板打开时也不抢
  document.addEventListener('keydown', (e) => {
    if (state.uiMode === 'gal') return;
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

  // 会话搜索：居中浮层（匹配标题与对话正文）
  if (typeof bindSessionSearch === 'function') bindSessionSearch();
  // 会话记录：按目录分组 / 平铺切换
  if (typeof bindSessionGroupToggle === 'function') bindSessionGroupToggle();
  // 上次关在剧场时，若当前会话不是剧场会话，就落到大厅，避免会话列表空白
  let startMode = (state.config && state.config.ui_mode) || 'solo';
  if (startMode === 'gal') {
    const cur = (state.sessions.sessions || []).find((s) => s.id === state.sessions.current);
    if (!cur || (cur.gal_scope || 'main') !== 'gal') startMode = 'hall';
  }
  applyUiMode(startMode);
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
    if (e.target.files && e.target.files[0]) attachDocBlob(e.target.files[0]);
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
    // 一次粘贴多张图时全部附上；文档仍保持「一次一个」
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
  if ($('#btn-autochat')) {
    $('#btn-autochat').onclick = () => { toggleAutopilot(); };
  }
  apLiveBind();           // 自动化看板：展开 / 收起
  if ($('#remind-add')) $('#remind-add').onclick = addReminder;
  if ($('#autochat-on')) $('#autochat-on').onchange = (e) => toggleAutoChat(e.target.checked);
  if ($('#reminders-enabled') && typeof bindReminderSwitches === 'function') bindReminderSwitches();
  if ($('#autochat-min')) $('#autochat-min').onchange = updateAutoChatGap;
  if ($('#autochat-max')) $('#autochat-max').onchange = updateAutoChatGap;
  if ($('#autochat-test')) $('#autochat-test').onclick = testAutoChat;
  startEvents();          // 常驻通道：服务端可在没人提问时主动推送

  // IDE 模式：拖动分隔条调整右侧聊天栏宽度（双击恢复默认；松手后记住）
  (function setupIdeResizer() {
    const rz = $('#ide-resizer');
    if (!rz) return;
    const clamp = (v) => Math.max(260, Math.min(760, Math.round(v)));
    const apply = (w) => document.documentElement.style.setProperty('--ide-chat-w', w + 'px');
    const saved = Number((state.config || {}).ide_chat_width);
    if (saved >= 260 && saved <= 760) apply(saved);
    const persist = () => {
      const col = $('.chat-col');
      const w = col ? clamp(col.getBoundingClientRect().width) : 380;
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
      const onMove = (ev) => apply(clamp(startW - (ev.clientX - startX)));
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
    rz.addEventListener('dblclick', () => { apply(380); persist(); });
  })();

  // IDE 模式：拖动调整左侧文件管理区宽度（双击恢复 260px；松手后记住）
  (function setupIdeSideResizer() {
    const rz = $('#ide-side-resizer');
    if (!rz) return;
    const clamp = (v) => Math.max(180, Math.min(460, Math.round(v)));
    const apply = (w) => document.documentElement.style.setProperty('--ide-side-w', clamp(w) + 'px');
    const saved = Number((state.config || {}).ide_side_width);
    if (saved >= 180 && saved <= 460) apply(saved);
    const persist = () => {
      const side = $('#ide-side-explorer');
      const w = (side && side.offsetParent) ? clamp(side.getBoundingClientRect().width) : 260;
      const cfg = Object.assign({}, state.config || {}, { ide_side_width: w });
      api('/api/config', cfg).then(() => { state.config = cfg; }).catch(() => {});
    };
    rz.addEventListener('mousedown', (e) => {
      e.preventDefault();
      const side = $('#ide-side-explorer');
      if (!side) return;
      const startX = e.clientX;
      const startW = side.getBoundingClientRect().width;
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

  // 代码块：复制 / 超长折叠展开（Solo 与 GAL 舞台都要）
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
  if ($('#gal-text')) $('#gal-text').addEventListener('click', onCodeBlockClick);

  // 设置弹窗左侧导航切换
  $$('#modal .settings-nav .snav-btn').forEach((tab) => {
    tab.onclick = () => setSettingsTab(tab.dataset.tab);
  });

  // 回到底部按钮（用 .off 过渡显隐，而非 display:none 硬切）
  // 同时维护「是否跟随最新输出」：只有贴着底才跟随，用户上翻立即锁定
  $('#chat').addEventListener('scroll', () => {
    const far = !nearBottom();
    $('#btn-bottom').classList.toggle('off', !far);
    // 自己滚的（流式贴底 / 点「回到底部」）不算用户操作：scroll 事件是异步派发的，
    // 等它回来时内容往往又长高了，再判一次 isAtBottom 就会把跟随关掉。
    if (_programmaticScroll > 0 || Date.now() < _smoothScrollUntil) return;
    setFollowBottom(isAtBottom(FOLLOW_SLOP));
  });
  // 滚轮向上 = 明确要看历史：立刻锁住，不等 scroll 事件排队
  $('#chat').addEventListener('wheel', (e) => {
    if (e.deltaY < 0) setFollowBottom(false);
  }, { passive: true });
  $('#chat').addEventListener('touchmove', () => {
    if (_programmaticScroll > 0) return;
    setFollowBottom(isAtBottom(FOLLOW_SLOP));
  }, { passive: true });
  // 图片加载 / 代码高亮 / 工具卡展开不会产生 delta，跟随状态下要补一次贴底
  watchChatGrowth();
  $('#btn-bottom').onclick = () => scrollToBottomSmooth();

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
    const t = $('#sidebar-toggle');
    if (t) {
      const label = on ? '展开侧栏' : '收起侧栏';
      t.title = label;
      t.setAttribute('aria-label', label);
      t.setAttribute('aria-expanded', on ? 'false' : 'true');
    }
    const ex = $('#sidebar-expand');
    if (ex) ex.setAttribute('aria-expanded', on ? 'false' : 'true');
    syncModeSwitchHome();
  }
  applyComposerCollapsed(!!(state.config && state.config.composer_collapsed));
  applySidebarCollapsed(!!(state.config && state.config.sidebar_collapsed));
  if ($('#composer-handle')) $('#composer-handle').onclick = () => {
    const c = document.querySelector('.composer');
    const on = !(c && c.classList.contains('collapsed'));
    applyComposerCollapsed(on, true);
    state.config = Object.assign({}, state.config || {}, { composer_collapsed: on });
    api('/api/config', { composer_collapsed: on }).catch(() => {});
  };
  function toggleSidebar(force) {
    const on = (typeof force === 'boolean') ? force : !$('#app').classList.contains('side-collapsed');
    applySidebarCollapsed(on);
    state.config = Object.assign({}, state.config || {}, { sidebar_collapsed: on });
    api('/api/config', { sidebar_collapsed: on }).catch(() => {});
  }
  if ($('#sidebar-toggle')) $('#sidebar-toggle').onclick = () => toggleSidebar(true);
  if ($('#sidebar-expand')) $('#sidebar-expand').onclick = () => toggleSidebar(false);
  bindChips();

  if (!String(state.config.chat_api_key || '').trim()) {
    setTimeout(() => { openModal({ tab: 'chat' }); toast('首次使用：请先填写聊天 API 密钥'); }, 400);
  }
  input.focus();
}

/* ============================================================
   内置笔记库（Obsidian 核心版）
   文件树 / 编辑·预览 / [[双链]]跳转 / 关系图谱 / 反向链接 / 搜索
   ============================================================ */

let notesGraphNet = null;
let noteSaveTimer = null;
let noteSearchTimer = null;
const notesPos = {};   // 节点坐标缓存：重绘时保持布局稳定

function flattenNotes(tree, out) {
  const acc = out || [];
  (tree || []).forEach((n) => {
    if (n.type === 'file') acc.push(n);
    else flattenNotes(n.children, acc);
  });
  return acc;
}

function notesNameIndex() {
  const map = {};
  flattenNotes(state.noteTree).forEach((f) => {
    map[String(f.name).toLowerCase()] = f.path;
    map[String(f.path).replace(/\.md$/i, '').toLowerCase()] = f.path;
  });
  return map;
}

// [[名字]] → 已存在的笔记路径（找不到返回 null）
function resolveNotePath(name) {
  const key = String(name || '').trim().toLowerCase();
  if (!key) return null;
  const idx = notesNameIndex();
  return idx[key] || idx[key.split('/').pop()] || null;
}

async function loadNotes() {
  const r = await api('/api/notes/tree', {});
  if (!r || !r.ok) { toast('笔记库读取失败'); return; }
  state.noteTree = r.tree || [];
  state.noteGraph = r.graph || { nodes: [], edges: [] };
  renderNotesTree();
  drawNotesGraph();
}

function renderNotesTree() {
  const box = $('#nt-tree');
  if (!box) return;
  if (!state.noteOpen) state.noteOpen = new Set();
  const open = state.noteOpen;
  const rows = [];
  const walk = (nodes, depth) => {
    (nodes || []).forEach((n) => {
      const pad = 8 + depth * 12;
      if (n.type === 'dir') {
        const isOpen = open.has(n.path);
        rows.push('<div class="nt-row" data-dir="' + escapeHtml(n.path) + '" style="padding-left:' + pad + 'px">' +
          '<span class="nt-caret">' + (isOpen ? '▾' : '▸') + '</span>' + icon('folder') + ' ' + escapeHtml(n.name) + '</div>');
        if (isOpen) walk(n.children, depth + 1);
      } else {
        rows.push('<div class="nt-row" data-note="' + escapeHtml(n.path) + '" style="padding-left:' + pad + 'px">' +
          '<span class="nt-caret"></span>' + icon('note') + ' ' + escapeHtml(n.name) + '</div>');
      }
    });
  };
  walk(state.noteTree, 0);
  box.innerHTML = rows.join('') || '<div class="hint">还没有笔记，点「笔记」按钮新建一篇。</div>';
  box.querySelectorAll('[data-dir]').forEach((el) => {
    const toggle = () => {
      const p = el.dataset.dir;
      if (open.has(p)) open.delete(p); else open.add(p);
      renderNotesTree();
    };
    el.onclick = toggle;
    makeClickable(el, toggle, '文件夹：' + (el.textContent || '').trim());
  });
  box.querySelectorAll('[data-note]').forEach((el) => {
    if (el.dataset.note === state.noteCurrent) el.classList.add('active');
    const openNote2 = () => openNote(el.dataset.note);
    el.onclick = openNote2;
    makeClickable(el, openNote2, '笔记：' + (el.textContent || '').trim());
  });
}

async function openNote(path) {
  if (!path) return;
  if (state.noteCurrent && state.noteCurrent !== path) await saveNoteNow();
  const r = await api('/api/notes/get', { path: path });
  if (!r || !r.ok) { toast('打开失败：' + ((r && r.error) || '笔记不存在')); return; }
  const prev = state.noteCurrent;
  state.noteCurrent = path;
  $('#nt-path').textContent = path;
  $('#nt-textarea').value = r.content || '';
  renderBacklinks(r.backlinks || []);
  setNoteMode(state.noteMode || 'preview');
  renderNotesTree();
  highlightNotesNode(prev, path);   // 只改高亮，不重建图谱（不丢缩放/平移）
}

function setNoteMode(mode) {
  state.noteMode = mode === 'edit' ? 'edit' : 'preview';
  const has = !!state.noteCurrent;
  const ta = $('#nt-textarea');
  const pv = $('#nt-preview-box');
  if (state.noteMode === 'preview' && has) {
    pv.innerHTML = safeRenderMarkdown(ta.value);
    pv.querySelectorAll('a.wikilink').forEach((a) => {
      if (!resolveNotePath(a.dataset.note)) a.classList.add('missing');
    });
  }
  ta.classList.toggle('hidden', !has || state.noteMode !== 'edit');
  pv.classList.toggle('hidden', !has || state.noteMode !== 'preview');
  $('#nt-empty').classList.toggle('hidden', has);
  $('#nt-edit').classList.toggle('active', state.noteMode === 'edit');
  $('#nt-preview').classList.toggle('active', state.noteMode === 'preview');
}

function scheduleNoteSave() {
  clearTimeout(noteSaveTimer);
  noteSaveTimer = setTimeout(saveNoteNow, 900);
}

async function saveNoteNow() {
  clearTimeout(noteSaveTimer);
  if (!state.noteCurrent) return;
  const r = await api('/api/notes/save', { path: state.noteCurrent, content: $('#nt-textarea').value });
  if (!r || !r.ok) { toast('保存失败：' + ((r && r.error) || '')); return; }
  state.noteGraph = r.graph || state.noteGraph;
  const bl = await api('/api/notes/backlinks', { path: state.noteCurrent });
  renderBacklinks((bl && bl.backlinks) || []);
  if (state.noteGraphTab !== 'backlinks') drawNotesGraph();
}

async function newNote(pathArg) {
  let path = pathArg;
  if (!path) {
    const name = await appPrompt('新笔记标题（可写 文件夹/子标题 建到子目录）');
    if (!name || !name.trim()) return;
    path = name.trim();
  }
  if (!/\.md$/i.test(path)) path += '.md';
  const r = await api('/api/notes/new', { path: path });
  if (!r || !r.ok) {
    if (r && r.path) { await loadNotes(); await openNote(r.path); toast('该笔记已存在，已为你打开'); return; }
    toast('新建失败：' + ((r && r.error) || ''));
    return;
  }
  state.noteTree = r.tree;
  state.noteGraph = r.graph;
  renderNotesTree();
  drawNotesGraph();
  await openNote(r.path);
  setNoteMode('edit');
  $('#nt-textarea').focus();
}

async function newNoteFolder() {
  const name = await appPrompt('新文件夹名称（可写 一级/二级）');
  if (!name || !name.trim()) return;
  const r = await api('/api/notes/folder/new', { path: name.trim() });
  if (!r || !r.ok) { toast('新建文件夹失败：' + ((r && r.error) || '')); return; }
  state.noteTree = r.tree;
  state.noteGraph = r.graph;
  state.noteOpen.add(name.trim().replace(/\\/g, '/'));
  renderNotesTree();
  toast('已创建文件夹');
}

async function renameCurrentNote() {
  if (!state.noteCurrent) { toast('先打开一篇笔记'); return; }
  const cur = state.noteCurrent.split('/').pop().replace(/\.md$/i, '');
  const name = await appPrompt('重命名为（会自动更新其它笔记里的 [[旧名]] 链接）', cur);
  if (!name || !name.trim() || name.trim() === cur) return;
  await saveNoteNow();
  const r = await api('/api/notes/rename', { path: state.noteCurrent, name: name.trim() });
  if (!r || !r.ok) { toast('重命名失败：' + ((r && r.error) || '')); return; }
  state.noteTree = r.tree;
  state.noteGraph = r.graph;
  state.noteCurrent = null;
  renderNotesTree();
  drawNotesGraph();
  await openNote(r.path);
  toast('已重命名，相关双链已同步更新');
}

async function deleteCurrentNote() {
  if (!state.noteCurrent) { toast('先打开一篇笔记'); return; }
  if (!confirm('删除笔记「' + state.noteCurrent + '」？该操作不可撤销')) return;
  clearTimeout(noteSaveTimer);
  const r = await api('/api/notes/delete', { path: state.noteCurrent });
  if (!r || !r.ok) { toast('删除失败'); return; }
  state.noteTree = r.tree;
  state.noteGraph = r.graph;
  state.noteCurrent = null;
  state.noteMode = 'preview';
  $('#nt-textarea').value = '';
  $('#nt-path').textContent = '未打开笔记';
  renderBacklinks([]);
  setNoteMode('preview');
  renderNotesTree();
  drawNotesGraph();
}

function renderBacklinks(list) {
  const box = $('#nt-backlinks');
  if (!box) return;
  const items = list || [];
  box.innerHTML = items.length
    ? items.map((b) => '<div class="bl" data-note="' + escapeHtml(b.path) + '"><b>' + escapeHtml(b.name) + '</b>' +
        (b.snippet ? '<div>' + escapeHtml(b.snippet) + '</div>' : '') + '</div>').join('')
    : '<div class="hint">还没有其它笔记链接到这一篇。在别的笔记里写 <code>[[这篇的标题]]</code> 就会出现在这里。</div>';
  box.querySelectorAll('.bl').forEach((el) => { el.onclick = () => openNote(el.dataset.note); });
}

function drawNotesGraph() {
  const el = $('#nt-graph');
  if (!el || el.classList.contains('hidden')) return;
  const view = notesGraphNet ? (() => {
    try { return { pos: notesGraphNet.getViewPosition(), scale: notesGraphNet.getScale() }; } catch (e) { return null; }
  })() : null;
  if (notesGraphNet) { try { notesGraphNet.destroy(); } catch (e) { } notesGraphNet = null; }

  const g = state.noteGraph || { nodes: [], edges: [] };
  if (!window.vis || !window.vis.Network || !window.vis.DataSet) {
    el.innerHTML = '<p class="hint" style="padding:16px">图谱库 vis-network 未加载（本地文件缺失且 CDN 不可用）。</p>';
    return;
  }
  el.innerHTML = '';
  if (!g.nodes.length) {
    el.innerHTML = '<p class="hint" style="padding:16px">还没有笔记。新建几篇，用 <code>[[双链]]</code> 互相关联，这里就会长出关系网络。</p>';
    return;
  }
  const th = graphTheme();
  const cur = state.noteCurrent;
  const activeColor = {
    background: th.accentSoft, border: th.accent,
    highlight: { background: th.accentSoft, border: th.accent },
    hover: { background: th.accentSoft, border: th.accent },
  };
  const nodes = g.nodes.map((n) => {
    const missing = !!n.missing;
    const active = n.id === cur;
    const o = {
      id: n.id,
      label: missing ? (n.label + ' ?') : n.label,
      shape: 'dot',
      size: active ? 22 : (missing ? 12 : 16),
      borderWidth: 2,
      font: { color: th.font, size: 13, strokeWidth: 3, strokeColor: th.labelBg },
      color: missing
        ? { background: th.warnSoft, border: th.warn,
            highlight: { background: th.warnSoft, border: th.warn },
            hover: { background: th.warnSoft, border: th.warn } }
        : (active ? activeColor : graphNodeColor(null, th)),
      shadow: { enabled: true, color: th.nodeShadow, size: 6, x: 0, y: 1 },
    };
    if (missing) o.shapeProperties = { borderDashes: [4, 4] };
    const p = notesPos[n.id];
    if (p && typeof p.x === 'number') { o.x = p.x; o.y = p.y; }
    return o;
  });
  const edges = g.edges.map((e) => ({
    id: e.id, from: e.from, to: e.to,
    color: { color: th.edge, highlight: th.edge, hover: th.edge },
    arrows: { to: { enabled: true, scaleFactor: 0.6 } },
    width: 1.5,
  }));
  try {
    const ds = { nodes: new vis.DataSet(nodes), edges: new vis.DataSet(edges) };
    notesGraphNet = new vis.Network(el, ds, {
      physics: { stabilization: { enabled: true, iterations: 150 },
                 barnesHut: { gravitationalConstant: -6000, springLength: 120 } },
      interaction: { hover: true },
      locale: 'cn',
      locales: VIS_LOCALES,
    });
    notesGraphNet._ds = ds;
    notesGraphNet.on('stabilized', () => captureNotesPositions());
    notesGraphNet.on('dragEnd', (p) => captureNotesPositions(p.nodes));
    notesGraphNet.on('click', (p) => {
      if (!p.nodes || !p.nodes.length) return;
      const id = p.nodes[0];
      if (String(id).indexOf('missing:') === 0) {
        const nd = (state.noteGraph.nodes || []).find((x) => x.id === id);
        newNote(((nd ? nd.label : '') + '.md').replace(/[\\/:*?"<>|]/g, ''));
      } else {
        openNote(id);
      }
    });
    if (view) { try { notesGraphNet.moveTo({ position: view.pos, scale: view.scale }); } catch (e) { } }
  } catch (err) {
    notesGraphNet = null;
    el.innerHTML = '<p class="hint" style="padding:16px">图谱渲染失败：' +
      escapeHtml(err && err.message ? err.message : String(err)) + '</p>';
  }
}

function captureNotesPositions(ids) {
  if (!notesGraphNet) return;
  let pos = {};
  try { pos = notesGraphNet.getPositions(ids || undefined); } catch (e) { return; }
  Object.keys(pos).forEach((k) => { notesPos[k] = pos[k]; });
}

function highlightNotesNode(prev, cur) {
  if (!notesGraphNet || !notesGraphNet._ds) return;
  const th = graphTheme();
  const activeColor = {
    background: th.accentSoft, border: th.accent,
    highlight: { background: th.accentSoft, border: th.accent },
    hover: { background: th.accentSoft, border: th.accent },
  };
  const paint = (id, active) => {
    if (!id) return;
    const nd = (state.noteGraph.nodes || []).find((x) => x.id === id);
    if (!nd || nd.missing) return;
    notesGraphNet._ds.nodes.update({
      id: id,
      size: active ? 22 : 16,
      color: active ? activeColor : graphNodeColor(null, th),
    });
  };
  paint(prev, false);
  paint(cur, true);
}

function switchNotesTab(name) {
  state.noteGraphTab = name;
  $$('#notes-modal .ntab').forEach((t) => t.classList.toggle('active', t.dataset.ntab === name));
  $('#nt-graph').classList.toggle('hidden', name !== 'graph');
  $('#nt-backlinks').classList.toggle('hidden', name !== 'backlinks');
  if (name === 'graph') drawNotesGraph();
}

async function searchNotes(q) {
  const box = $('#nt-hits');
  if (!q) { box.classList.add('hidden'); box.innerHTML = ''; return; }
  const r = await api('/api/notes/search', { q: q });
  const hits = (r && r.hits) || [];
  box.classList.remove('hidden');
  box.innerHTML = hits.length
    ? hits.map((h) => '<div class="hit" data-note="' + escapeHtml(h.path) + '"><b>' + escapeHtml(h.name) + '</b>' +
        (h.snippet ? '<span>' + escapeHtml(h.snippet) + '</span>' : '') + '</div>').join('')
    : '<div class="hint">没有匹配的笔记。</div>';
  box.querySelectorAll('.hit').forEach((el) => { el.onclick = () => openNote(el.dataset.note); });
}

async function openNotesModal() {
  if (!state.noteOpen) state.noteOpen = new Set();
  showWithAnim($('#notes-modal'));
  replayFadeIn($('#nt-graph'));
  await loadNotes();
  switchNotesTab(state.noteGraphTab || 'graph');
  if (!state.noteCurrent) {
    const files = flattenNotes(state.noteTree);
    if (files.length) await openNote(files[0].path);
  }
}

// 供工作台「记忆 → 打开来源」跳到指定笔记
window.notesOpen = async function (rel) {
  if (!rel) return;
  await openNotesModal();
  await openNote(rel);
};

async function closeNotesModal() {
  await saveNoteNow();
  hideWithAnim($('#notes-modal'), () => {
    if (notesGraphNet) { try { notesGraphNet.destroy(); } catch (e) { } notesGraphNet = null; }
  });
}

function bindNotesEvents() {
  $('#btn-notes').onclick = () => openNotesModal();
  $('#btn-close-notes').onclick = closeNotesModal;
  $('#nt-new').onclick = () => newNote();
  $('#nt-new-folder').onclick = newNoteFolder;
  $('#nt-rename').onclick = renameCurrentNote;
  $('#nt-delete').onclick = deleteCurrentNote;
  $('#nt-edit').onclick = () => setNoteMode('edit');
  $('#nt-preview').onclick = () => setNoteMode('preview');
  $('#nt-textarea').addEventListener('input', scheduleNoteSave);
  $('#nt-textarea').addEventListener('blur', () => { if (state.noteMode === 'edit') saveNoteNow(); });
  $('#nt-search').addEventListener('input', (e) => {
    clearTimeout(noteSearchTimer);
    const q = e.target.value.trim();
    noteSearchTimer = setTimeout(() => searchNotes(q), 250);
  });
  $$('#notes-modal .ntab').forEach((t) => { t.onclick = () => switchNotesTab(t.dataset.ntab); });
  $('#nt-preview-box').addEventListener('click', (e) => {
    const a = e.target.closest('a.wikilink');
    if (!a) return;
    e.preventDefault();
    const name = a.dataset.note || '';
    const path = resolveNotePath(name);
    if (path) { openNote(path); return; }
    const clean = name.replace(/[\\/:*?"<>|]/g, '').trim();
    if (clean) newNote(clean + '.md');
  });
}

document.addEventListener('DOMContentLoaded', init);

/* ============================================================
   模式切换的「挪家」+ 快捷键
   ------------------------------------------------------------
   侧栏收起后 .sidebar-body 整体隐藏，模式按钮也就跟着没了；
   但 Solo ↔ IDE 是最常来回切的一对，不该逼用户先展开侧栏。
   做法是移动**同一个** #mode-switch 节点（不复制 DOM，active 状态只有一份）：
     · 侧栏展开 → 回到 .side-top（原来的位置，文字按钮）
     · 侧栏收起 → 挂到 #mode-float（图标按钮；所有模式都竖排在左下角）
   ============================================================ */
function syncModeSwitchHome() {
  const ms = document.getElementById('mode-switch');
  const float = document.getElementById('mode-float');
  const host = document.querySelector('.side-top');
  const app = document.getElementById('app');
  if (!ms || !float || !host || !app) return;
  if (app.classList.contains('side-collapsed')) {
    if (ms.parentElement !== float) float.appendChild(ms);
    float.classList.remove('hidden');
  } else {
    if (ms.parentElement !== host) {
      const anchor = document.getElementById('btn-new');
      host.insertBefore(ms, anchor && anchor.nextSibling ? anchor.nextSibling : null);
    }
    float.classList.add('hidden');
  }
}

/* Ctrl+Alt+1..4 直接切模式：不依赖侧栏，也不依赖当前模式 */
document.addEventListener('keydown', (e) => {
  if (!e.ctrlKey || !e.altKey || e.shiftKey || e.metaKey) return;
  const mode = { '1': 'solo', '2': 'ide', '3': 'hall', '4': 'work' }[e.key];
  if (!mode || typeof setUiMode !== 'function') return;
  e.preventDefault();
  setUiMode(mode);
});
