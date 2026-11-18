'use strict';

/* 设置弹窗 · search.js
   设置内搜索 / 键盘操作 / 数值滑块联动
   原 web/settings.js 拆分而来：按 index.html 中的顺序加载，共享全局作用域。 */

/* 范围型数值：滑块 + 数字输入联动 */
function addRangeCompanions() {
  RANGE_FIELDS.forEach(([id, min, max, step]) => {
    const num = document.querySelector('#modal #' + id);
    if (!num) return;
    let wrap = num.closest('.range-row');
    let range = wrap ? wrap.querySelector('input[type="range"]') : null;
    if (!range) {
      wrap = document.createElement('div');
      wrap.className = 'range-row';
      range = document.createElement('input');
      range.type = 'range';
      range.className = 'range';
      range.min = String(min);
      range.max = String(max);
      range.step = String(step);
      num.parentNode.insertBefore(wrap, num);
      wrap.appendChild(range);
      wrap.appendChild(num);
      num.addEventListener('input', () => { range.value = num.value; });
    }
    range.value = num.value;
    range.oninput = () => {
      num.value = range.value;
      num.dispatchEvent(new Event('input', { bubbles: true }));
    };
  });
}

/* 页签顺序以 DOM 为准，避免再维护一份数组 */
function settingsTabs() {
  return $$('#modal .settings-nav .snav-btn').map((b) => b.dataset.tab);
}

function settingsLastTab() {
  try {
    const t = settingsNormalizeTab(localStorage.getItem(SETTINGS_LAST_TAB_KEY));
    if (t && document.querySelector('#modal .snav-btn[data-tab="' + t + '"]')) return t;
  } catch (e) { /* 忽略 */ }
  return 'model';
}

/* 二级分段（目前只有「能力与工具」）：同一页下再按能力域切 pane */
const SETTINGS_SUB_STATE = {};

function settingsVisiblePanes(page, paneHint) {
  const all = settingsPagePanes(page);
  const conf = settingsPageConf(page);
  if (!conf || !conf.subnav || !conf.subnav.length) return all;
  let sub = paneHint ? settingsSubOfPane(page, paneHint) : null;
  if (!sub) {
    sub = conf.subnav.find((s) => s.id === SETTINGS_SUB_STATE[page]) || conf.subnav[0];
  }
  SETTINGS_SUB_STATE[page] = sub.id;
  return sub.panes.filter((p) => all.indexOf(p) >= 0);
}

function renderSettingsSubnav(page) {
  const bar = $('#settings-subnav');
  if (!bar) return;
  const conf = settingsPageConf(page);
  if (!conf || !conf.subnav || !conf.subnav.length) {
    bar.classList.add('hidden');
    bar.innerHTML = '';
    return;
  }
  const cur = SETTINGS_SUB_STATE[page] || conf.subnav[0].id;
  bar.classList.remove('hidden');
  bar.innerHTML = conf.subnav.map((s) =>
    '<button type="button" class="settings-subnav-btn' + (s.id === cur ? ' active' : '') +
    '" data-sub="' + escapeHtml(s.id) + '" role="tab" aria-selected="' +
    (s.id === cur ? 'true' : 'false') + '">' + escapeHtml(s.title) + '</button>').join('');
  bar.querySelectorAll('[data-sub]').forEach((b) => {
    b.onclick = () => setSettingsSub(page, b.dataset.sub);
  });
}

function setSettingsSub(page, subId) {
  SETTINGS_SUB_STATE[page] = subId;
  const show = settingsVisiblePanes(page);
  show.forEach(attachPane);
  $$('#modal .pane').forEach((pn) => pn.classList.toggle('hidden', show.indexOf(pn.dataset.pane) < 0));
  renderSettingsSubnav(page);
  renderSettingsAnchors(page);
  const content = $('#modal .settings-content');
  if (content) content.scrollTop = 0;
  if (settingsLazyReady) keepOnlyPanes(show);
  updateAnchorsActive();
}

function readSettingsScroll() {
  try { return JSON.parse(localStorage.getItem(SETTINGS_SCROLL_KEY) || '{}') || {}; }
  catch (e) { return {}; }
}

function saveSettingsScroll(tab) {
  const content = $('#modal .settings-content');
  if (!content || !tab) return;
  const map = readSettingsScroll();
  map[tab] = content.scrollTop;
  try { localStorage.setItem(SETTINGS_SCROLL_KEY, JSON.stringify(map)); } catch (e) { /* 忽略 */ }
}

function restoreSettingsScroll(tab) {
  const content = $('#modal .settings-content');
  if (!content) return;
  const map = readSettingsScroll();
  content.scrollTop = Number(map[tab] || 0) || 0;
}

function updateSettingsPageHead(name) {
  const meta = SETTINGS_PAGE_META[name] || [name || '', ''];
  const title = $('#settings-page-title');
  const desc = $('#settings-page-desc');
  if (title) title.textContent = meta[0];
  if (desc) desc.textContent = meta[1];
  const reset = $('#settings-page-reset');
  if (reset) reset.classList.toggle('hidden', !pageResetSpec(name));
  if (name === 'system') {
    loadAppInfo();
    if (typeof loadSettingsHistory === 'function') loadSettingsHistory();
    if (typeof loadMobileStatus === 'function') loadMobileStatus();
  }
}

function openModal(opts) {
  opts = opts || {};
  const modal = $('#modal');
  if (isOpen(modal)) { if (opts.tab) setSettingsTab(opts.tab); return; }

  // 窄屏：设置是整窗页面，先把侧栏抽屉收起，避免抽屉压在设置上方
  try {
    if (window.matchMedia('(max-width: 900px)').matches) {
      const app = $('#app');
      if (app && !app.classList.contains('side-collapsed')) {
        const sideBtn = $('#btn-sidebar-top');
        if (sideBtn) sideBtn.click();
      }
    }
  } catch (e) { /* 忽略 */ }

  draftCfg = deepCopy(state.config || {});
  draftProfile = deepCopy(state.profile || {});
  settingsDefaults = null;
  settingsCurrentTab = '';

  const panes = buildDynamicPanes();
  fillStaticSettings(draftCfg, draftProfile);

  // 打开设置时自动查一次余额（不阻塞界面）
  refreshBalance('chat', panes[0]);
  refreshBalance('vision', panes[1]);

  if (typeof loadSkills === 'function') loadSkills();    // Skill 列表（静态面板，只刷数据）
  if (typeof loadAgents === 'function') loadAgents();    // 子代理定义（静态面板，只刷数据）
  if (typeof loadTasks === 'function') loadTasks();      // 提醒 / 主动聊天面板
  if (typeof syncReminderSwitches === 'function') syncReminderSwitches();

  const mobileOpen = window.matchMedia('(max-width: 900px)').matches;
  setSettingsTab(opts.tab || settingsLastTab());
  // 手机端从「设置」入口进来先看分类列表；深链指定页签才直接进详情
  if (mobileOpen && !opts.tab) modal.classList.remove('mobile-page');
  showWithAnim(modal);
  // 先让弹窗出来，再做全量装饰 / 搜索索引 / 快照：这些都要遍历 13 个分页，
  // 同步做会让「点设置」到「看到界面」多等上百毫秒。
  requestAnimationFrame(() => {
    // 打开动画开始后再对齐导航：窄屏导航自身滚动，确保当前分类在可视区内
    const an = $('#modal .settings-nav .snav-btn.active');
    if (an && an.scrollIntoView) {
      try { an.scrollIntoView({ block: 'nearest' }); } catch (e) { /* 忽略 */ }
    }
    decorateSettingsPanes();
    foldLongHints();
    addRangeCompanions();
    bindAnchorsScroll();
    initHelpPane();                    // 依赖 set-card，必须在 decorate 之后
    helpSyncHeadHeight(helpPaneEl());
    resetHelpFilter();
    if (opts.anchor) openHelpAnchor(opts.anchor);
    if (typeof applySettingsMeta === 'function') applySettingsMeta();
    if (typeof updateSettingsDraftBar === 'function') updateSettingsDraftBar();
    buildSettingsSearchIndex();        // 动态面板刚重建，索引必须在这之后重建
    resetSettingsSearch();
    renderSettingsAnchors(settingsCurrentTab);
    restoreSettingsScroll(settingsCurrentTab);
    updateAnchorsActive();
    // 装饰完成后再做按需挂载：只保留当前页 pane，其余移到 detached 存储
    keepOnlyPanes(settingsVisiblePanes(settingsCurrentTab));
    settingsLazyReady = true;
    settingsSnapshot = JSON.stringify(collectSettings());
    updateSettingsDirty();
  });
}

// 设置弹窗：切换左侧导航对应的面板（一页可含多个 pane）
function setSettingsTab(name) {
  // 兼容旧入口传 pane id（keys / data / mobile / chat …）：
  // 先记住 pane 提示（决定二级分段），再映射成所在页。
  const raw = String(name || '');
  const paneHint = (raw && settingsPaneEl(raw)) ? raw : '';
  name = settingsNormalizeTab(raw);
  if (!name || !document.querySelector('#modal .snav-btn[data-tab="' + name + '"]')) {
    name = paneHint ? settingsPageOfPane(paneHint) : 'model';
  }
  if (settingsCurrentTab && settingsCurrentTab !== name) saveSettingsScroll(settingsCurrentTab);
  $$('#modal .settings-nav .snav-btn').forEach((b) => b.classList.toggle('active', b.dataset.tab === name));
  const show = settingsVisiblePanes(name, paneHint);
  show.forEach(attachPane);                 // 目标页的面板可能还在 detached 存储里
  $$('#modal .pane').forEach((pn) => pn.classList.toggle('hidden', show.indexOf(pn.dataset.pane) < 0));
  // 外观页平时是 detached 的：重新挂上时补一次主题卡片状态
  if (name === 'ui' && typeof syncThemeCards === 'function') {
    const th = normalizeTheme(document.documentElement.getAttribute('data-theme')
      || ((typeof draftCfg !== 'undefined' && draftCfg && draftCfg.theme) || ''));
    syncThemeCards(th);
    if (typeof syncAuroraPrefs === 'function') syncAuroraPrefs();
  }
  // 可见 pane 按页面定义顺序排到内容末尾，避免静态 pane 的 DOM 顺序打乱页面阅读顺序
  const contentEl = $('#modal .settings-content');
  if (contentEl) {
    show.forEach((paneName) => {
      const pane = contentEl.querySelector('.pane[data-pane="' + paneName + '"]');
      if (pane) contentEl.appendChild(pane);
    });
  }
  renderSettingsSubnav(name);
  // 窄屏下设置导航自己滚动：把当前分类带进视野，避免「打开了主题页却看不到选中的导航项」
  const activeNav = $('#modal .settings-nav .snav-btn.active');
  if (activeNav && activeNav.scrollIntoView) {
    try { activeNav.scrollIntoView({ block: 'nearest' }); } catch (e) { /* 忽略 */ }
  }
  settingsCurrentTab = name;
  try { localStorage.setItem(SETTINGS_LAST_TAB_KEY, name); } catch (e) { /* 忽略 */ }
  const content = $('#modal .settings-content');
  if (content) content.scrollTop = 0;
  if (content && typeof gmPageIn === 'function') gmPageIn(content);   // 页签切换淡入，不再瞬移
  updateSettingsPageHead(name);
  renderSettingsAnchors(name);
  // 移动端：选中页后进入详情，返回键回分类列表
  const modal = $('#modal');
  if (modal) modal.classList.toggle('mobile-page', window.matchMedia('(max-width: 900px)').matches);
  if (name === 'help') {
    initHelpPane();
    requestAnimationFrame(() => {
      helpSyncHeadHeight(helpPaneEl());
      helpUpdateSpy();
    });
  }
  if (settingsLazyReady) keepOnlyPanes(show);
  if (isOpen($('#modal'))) requestAnimationFrame(() => {
    restoreSettingsScroll(name);
    updateAnchorsActive();
  });
}

/* ---- 设置面板：搜索 + 键盘操作（与「快捷键」面板的说明一一对应）---- */
let settingsSearchIndex = [];
let settingsSearchHits = [];
let settingsSearchSel = 0;

/* 搜索时把没有命中的分类按钮藏起来，清空后恢复（pane → page 映射） */
function filterSettingsNav(paneSet) {
  const pages = paneSet ? new Set(Array.from(paneSet).map(settingsPageOfPane)) : null;
  $$('#modal .settings-nav .snav-btn').forEach((b) => {
    const on = !pages || pages.has(b.dataset.tab);
    b.classList.toggle('hidden', !on);
  });
  $$('#modal .settings-nav .snav-group').forEach((g) => {
    const any = Array.from(g.querySelectorAll('.snav-btn')).some((b) => !b.classList.contains('hidden'));
    g.classList.toggle('hidden', !any);
  });
}

function markSearchText(text, kws) {
  let html = escapeHtml(text || '');
  kws.forEach((k) => {
    if (!k) return;
    const re = new RegExp('(' + k.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ')', 'ig');
    html = html.replace(re, '<mark>$1</mark>');
  });
  return html;
}

function buildSettingsSearchIndex() {
  settingsSearchIndex = [];
  $$('#modal .settings-nav .snav-btn').forEach((b) => {
    settingsSearchIndex.push({ pane: b.dataset.tab, el: b, text: b.textContent.trim(), search: b.dataset.search || '' });
  });
  allSettingsPanes().forEach((pane) => {
    pane.querySelectorAll('[data-search]').forEach((el) => {
      settingsSearchIndex.push({
        pane: pane.dataset.pane || '', el: el,
        text: el.textContent.trim().slice(0, 40), search: el.dataset.search || '',
      });
    });
  });
}

function settingsPaneName(pane) {
  const pageId = settingsPageOfPane(pane);
  const b = $('#modal .settings-nav .snav-btn[data-tab="' + pageId + '"]');
  const pageName = b ? b.textContent.trim() : (pane || '');
  const sub = settingsSubOfPane(pageId, pane);
  return sub ? pageName + ' · ' + sub.title : pageName;
}

function resetSettingsSearch() {
  const inp = $('#settings-search-input');
  const box = $('#settings-search-list');
  if (inp) inp.value = '';
  if (box) { box.classList.add('hidden'); box.innerHTML = ''; }
  settingsSearchHits = [];
  settingsSearchSel = 0;
  filterSettingsNav(null);
}

function runSettingsSearch(q) {
  const box = $('#settings-search-list');
  if (!box) return;
  const kws = String(q || '').trim().toLowerCase().split(/\s+/).filter(Boolean);
  if (!kws.length) {
    box.classList.add('hidden'); box.innerHTML = '';
    settingsSearchHits = [];
    filterSettingsNav(null);
    return;
  }
  const hits = [];
  settingsSearchIndex.forEach((it) => {
    // 被当前级别 / 标签隐藏的分区不参与搜索：搜不到就点不到
    if (it.el && it.el.closest && it.el.closest('.meta-hidden')) return;
    const hay = (it.text + ' ' + it.search).toLowerCase();
    if (!kws.every((k) => hay.includes(k))) return;
    const inText = kws.every((k) => it.text.toLowerCase().includes(k));
    hits.push({ it: it, score: inText ? 0 : 1 });          // 标签正文命中优先于仅同义词命中
  });
  const order = settingsTabs();
  hits.sort((a, b) => {
    const pa = order.indexOf(a.it.pane);
    const pb = order.indexOf(b.it.pane);
    return (pa - pb) || (a.score - b.score);
  });
  settingsSearchHits = hits.slice(0, 20).map((h) => h.it);
  settingsSearchSel = 0;
  filterSettingsNav(new Set(hits.map((h) => h.it.pane)));
  if (!settingsSearchHits.length) {
    box.innerHTML = '<div class="settings-search-empty">无匹配项，试试「密钥」「压缩」「主题」或「B 站」</div>';
    box.classList.remove('hidden');
    return;
  }
  box.innerHTML = '<div class="settings-search-count">找到 ' + hits.length + ' 项</div>' +
    settingsSearchHits.map((it, i) =>
      '<div class="settings-search-item' + (i === 0 ? ' sel' : '') + '" data-i="' + i + '">' +
      '<span class="settings-search-pane">' + escapeHtml(settingsPaneName(it.pane)) + '</span>' +
      '<span class="settings-search-text">' + markSearchText(it.text || '', kws) + '</span></div>').join('');
  box.classList.remove('hidden');
}

function settingsSearchMove(delta) {
  if (!settingsSearchHits.length) return;
  settingsSearchSel = Math.max(0, Math.min(settingsSearchHits.length - 1, settingsSearchSel + delta));
  $$('#settings-search-list .settings-search-item').forEach((el, i) => el.classList.toggle('sel', i === settingsSearchSel));
}

function flashSettingsTarget(el) {
  if (!el) return;
  const t = el.closest('.check-row, .field-label, .set-group, .snav-btn') || el;
  t.classList.add('settings-flash');
  setTimeout(() => t.classList.remove('settings-flash'), 900);
}

function settingsJumpTo(pane, el) {
  if (!pane) return;
  setSettingsTab(pane);   // 传 pane 而不是页：让「能力与工具」自动切到所属二级分段
  resetSettingsSearch();
  requestAnimationFrame(() => {
    const det = el && el.closest ? el.closest('details') : null;
    if (det) det.open = true;
    const sec = el && el.closest ? el.closest('.set-section') : null;
    if (sec) setSectionAdvanced(sec, true);              // 命中的高级项先展开
    const card = el && el.closest ? el.closest('.set-card') : null;
    if (card) setCardCollapsed(card, false, true);      // 命中的分区若被收起，先展开
    if (el && el.scrollIntoView) el.scrollIntoView({ block: 'center' });
    flashSettingsTarget(el);
    updateAnchorsActive();
  });
}

// 设置内的键盘操作：Esc 关闭 / Ctrl+S 保存 / ←→ 切分类 / 搜索框内 ↑↓·Enter·Esc
// 用捕获相（true），先于 document 冒泡相上的 GAL、IDE 监听执行，避免互相打架。
function onSettingsKeydown(e) {
  if (_hkRecording) return;                        // 正在录制快捷键 → 键盘交给录制，别切分类 / 关弹窗
  if (isImeComposing(e)) return;                   // 输入法组词时，Enter/Esc 先交给候选框
  const modal = $('#modal');
  if (!modal || modal.classList.contains('hidden')) return;
  const inp = $('#settings-search-input');
  const inSearch = !!inp && document.activeElement === inp;
  if (e.key === 'Escape') {
    e.preventDefault(); e.stopPropagation();
    const hf = $('#help-filter');
    if (hf && document.activeElement === hf && hf.value) {
      hf.value = ''; helpApplyFilter('');          // 先清筛选，别顺手关掉整个设置
    } else if (inSearch && inp.value) {
      inp.value = ''; runSettingsSearch('');
    } else {
      closeModal();
    }
    return;
  }
  const mod = e.ctrlKey || e.metaKey;
  const k = (e.key || '').toLowerCase();
  if (mod && k === 'k') {                          // 设置内搜索：Ctrl/Cmd+K
    e.preventDefault(); e.stopPropagation();
    if (inp) { inp.focus(); inp.select(); }
    return;
  }
  if (mod && (k === 's' || k === 'p')) {          // 顺带吞掉 Ctrl+P / Ctrl+Shift+P，防 IDE 面板叠在弹窗上
    e.preventDefault(); e.stopPropagation();
    if (k === 's') saveModal();
    return;
  }
  if (inSearch) {
    if (e.key === 'ArrowDown') { e.preventDefault(); settingsSearchMove(1); return; }
    if (e.key === 'ArrowUp') { e.preventDefault(); settingsSearchMove(-1); return; }
    if (e.key === 'Enter') {
      e.preventDefault();
      const it = settingsSearchHits[settingsSearchSel];
      if (it) settingsJumpTo(it.pane, it.el);
      return;
    }
  }
  if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
    const t = e.target;
    if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable)) return;
    const cur = $$('#modal .settings-nav .snav-btn').find((b) => b.classList.contains('active'));
    const tabs = settingsTabs();
    const i = cur ? tabs.indexOf(cur.dataset.tab) : -1;
    if (i < 0) return;
    const next = e.key === 'ArrowRight'
      ? Math.min(tabs.length - 1, i + 1)
      : Math.max(0, i - 1);
    e.preventDefault();
    setSettingsTab(tabs[next]);
  }
}

function setupSettingsSearch() {
  const inp = $('#settings-search-input');
  const box = $('#settings-search-list');
  if (!inp || !box) return;

  // 手机端：详情页返回分类列表；回到桌面宽度时清掉移动态
  const back = $('#settings-back');
  if (back && !back.dataset.bound) {
    back.dataset.bound = '1';
    back.onclick = () => {
      const modal = $('#modal');
      if (modal) modal.classList.remove('mobile-page');
      const active = $('#modal .settings-nav .snav-btn.active');
      if (active && active.focus) active.focus();
    };
  }
  window.addEventListener('resize', () => {
    if (window.innerWidth > 900) {
      const modal = $('#modal');
      if (modal) modal.classList.remove('mobile-page');
    }
  }, { passive: true });

  inp.addEventListener('input', () => runSettingsSearch(inp.value));
  inp.addEventListener('focus', () => { if (inp.value) runSettingsSearch(inp.value); });
  box.addEventListener('click', (e) => {
    const item = e.target.closest('.settings-search-item');
    if (!item) return;
    const it = settingsSearchHits[Number(item.dataset.i)];
    if (it) settingsJumpTo(it.pane, it.el);
  });

  // 「数据与高级」页
  const openData = $('#app-open-data');
  if (openData) openData.onclick = openDataDir;
  const ciRefresh = $('#codeindex-refresh');
  if (ciRefresh) ciRefresh.onclick = codeindexRefresh;
  const ciRebuild = $('#codeindex-rebuild');
  if (ciRebuild) ciRebuild.onclick = codeindexRebuild;
  const backup = $('#app-backup');
  if (backup) backup.onclick = () => {
    if (typeof workOpenBackupDialog === 'function') { workOpenBackupDialog(); return; }
    // 工作台脚本按需加载：首次点备份时再拉取，拉取成功后直接打开弹窗
    if (typeof window.gmLoadGroup === 'function') {
      window.gmLoadGroup('work').then(() => {
        if (typeof workOpenBackupDialog === 'function') workOpenBackupDialog();
        else toast('备份与恢复在工作台里');
      }).catch(() => toast('备份与恢复在工作台里'));
      return;
    }
    toast('备份与恢复在工作台里');
  };
  const compactSessions = $('#app-compact-sessions');
  if (compactSessions) compactSessions.onclick = async () => {
    const status = $('#app-storage-status');
    const setStatus = (t) => { if (status) status.textContent = t; };
    const mb = (n) => (Number(n || 0) / 1048576).toFixed(1) + ' MB';
    compactSessions.disabled = true;
    setStatus('正在检查会话存储…');
    try {
      const dry = await api('/api/storage/compact', { apply: false });
      if (!dry.ok) throw new Error(dry.error || '检查失败');
      const stats = dry.stats || {};
      if (!stats.messages) {
        setStatus('当前 sessions.json：' + mb(dry.before) + '，没有可压缩的正文副本。');
        toast('会话存储已经很干净');
        return;
      }
      const ok = window.confirm(
        '可压缩 ' + stats.messages + ' 条消息：' + mb(dry.before)
        + ' → ' + mb(dry.after) + '。\n\n'
        + '只会移除附件正文的重复副本 / 二进制乱码，不删消息；'
        + '写回前会自动备份 sessions.json。确定继续？');
      if (!ok) { setStatus('已取消。'); return; }
      setStatus('正在压缩并写回…');
      const r = await api('/api/storage/compact', { apply: true });
      if (!r.ok) throw new Error(r.error || '压缩失败');
      setStatus('已压缩：' + mb(r.before) + ' → ' + mb(r.after)
        + (r.backup ? '，备份已生成。' : '。'));
      toast('会话存储已压缩', 'success');
    } catch (e) {
      setStatus('压缩失败：' + (e && e.message ? e.message : e));
      toastErr('压缩会话失败', e);
    } finally {
      compactSessions.disabled = false;
    }
  };
  const resetAll = $('#app-reset-all');
  if (resetAll) resetAll.onclick = resetAllSettings;
  const ctxMenu = $('#app-context-menu');
  if (ctxMenu) ctxMenu.onclick = toggleContextMenu;
  const resetPage = $('#settings-page-reset');
  if (resetPage) resetPage.onclick = resetCurrentSettingsPage;

  // 任何输入 / 选择都重算「未保存的改动」（弹窗内的搜索框不算）
  const modal = $('#modal');
  if (modal) {
    const onEdit = (e) => { if (e.target.closest && e.target.closest('.pane')) queueDirtyCheck(); };
    modal.addEventListener('input', onEdit);
    modal.addEventListener('change', onEdit);
    // 卡片：标题行点击折叠 / 展开；右上角两个按钮分别恢复本节默认、折叠
    modal.addEventListener('click', (e) => {
      const secBtn = e.target.closest && e.target.closest('[data-act="toggle-section-adv"]');
      if (secBtn) {
        toggleSectionAdvanced(secBtn.closest('.set-section'));
        return;
      }
      const card = e.target.closest && e.target.closest('.set-card');
      if (!card) return;
      const act = e.target.closest('.set-card-act');
      if (act) {
        if (act.dataset.act === 'collapse-card') {
          setCardCollapsed(card, foldIsOpen(card, 'collapsed'), true);
        } else if (act.dataset.act === 'reset-card') {
          resetSettingsCard(card);
        }
        return;
      }
      if (e.target.closest('.set-card-head')) {
        setCardCollapsed(card, foldIsOpen(card, 'collapsed'), true);
      }
    });
  }

  // 全局规则：展开 / 删除按钮走事件委托（规则行是动态生成的）
  const rulesBox = $('#profile-rules');
  if (rulesBox) {
    rulesBox.addEventListener('click', (e) => {
      const expand = e.target.closest && e.target.closest('[data-act="rule-expand"]');
      if (expand) {
        const row = expand.closest('.rule-item');
        if (!row) return;
        const open = row.classList.toggle('open');
        expand.textContent = open ? '收起' : '展开';
        expand.setAttribute('aria-expanded', open ? 'true' : 'false');
        return;
      }
      const del = e.target.closest && e.target.closest('[data-act="rule-del"]');
      if (!del) return;
      const row = del.closest('.rule-item');
      if (!row) return;
      row.remove();
      if (!rulesBox.querySelector('.rule-item')) renderProfileRules([]);
      else renumberProfileRules();
      updateSettingsDirty();
    });
    rulesBox.addEventListener('change', (e) => {
      const cb = e.target;
      if (!cb || cb.dataset.act !== 'rule-toggle') return;
      const row = cb.closest('.rule-item');
      if (row) row.classList.toggle('off', !cb.checked);
    });
  }
  const addRule = $('#profile-rule-add');
  if (addRule) {
    addRule.onclick = () => {
      addProfileRule();
      updateSettingsDirty();
    };
  }
}
