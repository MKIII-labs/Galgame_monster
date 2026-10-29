'use strict';

/* Node 单元测试环境（scripts/hotkeys_test.js）：把纯逻辑层拉进来。
   浏览器里不执行这段 —— 那边由 index.html 先加载 hk-core.js，顶层 const/function
   落在同一个全局作用域，直接可用。 */
if (typeof module !== 'undefined' && module.exports && typeof hkHitIn === 'undefined') {
  Object.assign(globalThis, require('./hk-core.js'));
}

/* ============================================================
   统一快捷键：注册表 + 解析 + 分发 + 实时 HUD + 「?」清单

   - 所有快捷键都从 HK_DEFS 这一张表出发：界面显示与实际判定同源，改键后提示立刻跟着变
   - 判定用 e.code（物理键位），不受输入法 / 大小写 / 中英切换影响
   - 改键存在 localStorage['hotkeys_v1']，只记与默认不同的项
   ============================================================ */

const HK_STORE_KEY = 'hotkeys_v1';
const HK_HUD_KEY = 'hotkeys_hud_open';
/* 纯逻辑（HK_MOD_KEYS / HK_KEY_LABEL / hkKeyOf / hkLabel / hkSpecProblem /
   重键检测 / Monaco 键位换算）已拆到 web/hk-core.js —— 那一层不碰 DOM 与
   localStorage，可以被 scripts/hotkeys_test.js 直接 require 做单元测试。
   本文件在浏览器里先加载 hk-core.js，顶层 const/function 同域可用。 */

// 「?」清单里的分组（顺序即展示顺序）
const HK_SCOPES = [
  { id: 'gal', name: 'GAL 演出' },
  { id: 'ide', name: 'IDE 工作区' },
  { id: 'editor', name: '编辑器（VS Code）' },
  { id: 'composer', name: '输入框' },
  { id: 'global', name: '全局' },
];

/* keys：默认按键（可给多个候选，任一命中即可）；fixed 不可改键；
   hold = 按住生效；dyn = 运行时动态判定；both 只做展示、不参与分发 */
const HK_DEFS = [
  { id: 'gal.advance', scope: 'gal', name: '推进对话（打字中先补全）', keys: ['Space', 'Enter', 'NumpadEnter'] },
  { id: 'gal.fast',    scope: 'gal', name: '按住快进', keys: ['ControlLeft', 'ControlRight'], hold: true, fixed: true },
  { id: 'gal.auto',    scope: 'gal', name: '自动播放开关', keys: ['KeyA'] },
  { id: 'gal.review',  scope: 'gal', name: '打开回顾', keys: ['ArrowUp'] },
  { id: 'gal.option',  scope: 'gal', name: '选择第 N 个选项', keys: [], dyn: true, fixed: true },
  { id: 'gal.esc',     scope: 'gal', name: '关闭最上层浮层', keys: ['Escape'], fixed: true, displayOnly: true },

  { id: 'ide.save',    scope: 'ide', name: '保存当前文件', keys: ['Ctrl+KeyS', 'Meta+KeyS'] },
  { id: 'ide.open',    scope: 'ide', name: '快速打开文件', keys: ['Ctrl+KeyP', 'Meta+KeyP'] },
  { id: 'ide.palette', scope: 'ide', name: '命令面板', keys: ['Ctrl+Shift+KeyP', 'Meta+Shift+KeyP'] },
  { id: 'ide.sidebar', scope: 'ide', name: '收起 / 展开文件管理区', keys: ['Ctrl+KeyB', 'Meta+KeyB'] },
  { id: 'ide.terminal', scope: 'ide', name: '打开 / 关闭终端', keys: ['Ctrl+Backquote', 'Meta+Backquote'] },

  /* ---- 编辑器（VS Code 原生）----
     这些动作由**内置的 Monaco 自己实现**（Monaco 就是 VS Code 的编辑器本体），
     所以默认状态下我们**一行都不介入**，行为与 VS Code 完全一致（含多光标、
     只读、查找框开着等边界场景 —— 那些都由 Monaco 自己管）。
     只有当你为某条规则改了键，才会：① 解绑 Monaco 的原键 ② 把新键绑回同一个动作
     （见 web/gal/gal-ide.js 的 ideApplyEditorKeybindings）。
     `mono` = Monaco 的 action / command id，改键时用它把新键绑回原动作。
     注意：`Ctrl+F` / `Ctrl+/` 这两条在本表里同时存在于「编辑器」与「全局」两个作用域，
     分流规则是「编辑器聚焦时让给 Monaco」 —— 这也是 VS Code 的语义。 */
  { id: 'ed.selectLine',  scope: 'editor', name: '选中整行（连按可扩展）', keys: ['Ctrl+KeyL', 'Meta+KeyL'], mono: 'expandLineSelection' },
  { id: 'ed.undo',        scope: 'editor', name: '撤销', keys: ['Ctrl+KeyZ', 'Meta+KeyZ'], mono: 'undo' },
  { id: 'ed.redo',        scope: 'editor', name: '重做', keys: ['Ctrl+Shift+KeyZ', 'Ctrl+KeyY', 'Meta+Shift+KeyZ', 'Meta+KeyY'], mono: 'redo' },
  { id: 'ed.comment',     scope: 'editor', name: '切换行注释', keys: ['Ctrl+Slash', 'Meta+Slash'], mono: 'editor.action.commentLine' },
  { id: 'ed.delLine',     scope: 'editor', name: '删除当前行', keys: ['Ctrl+Shift+KeyK', 'Meta+Shift+KeyK'], mono: 'editor.action.deleteLines' },
  { id: 'ed.moveUp',      scope: 'editor', name: '向上移动行', keys: ['Alt+ArrowUp'], mono: 'editor.action.moveLinesUpAction' },
  { id: 'ed.moveDown',    scope: 'editor', name: '向下移动行', keys: ['Alt+ArrowDown'], mono: 'editor.action.moveLinesDownAction' },
  { id: 'ed.copyUp',      scope: 'editor', name: '向上复制行', keys: ['Shift+Alt+ArrowUp'], mono: 'editor.action.copyLinesUpAction' },
  { id: 'ed.copyDown',    scope: 'editor', name: '向下复制行', keys: ['Shift+Alt+ArrowDown'], mono: 'editor.action.copyLinesDownAction' },
  { id: 'ed.insertBelow', scope: 'editor', name: '在下方插入行', keys: ['Ctrl+Enter'], mono: 'editor.action.insertLineAfter' },
  { id: 'ed.insertAbove', scope: 'editor', name: '在上方插入行', keys: ['Ctrl+Shift+Enter'], mono: 'editor.action.insertLineBefore' },
  { id: 'ed.selNext',     scope: 'editor', name: '选中下一个相同词', keys: ['Ctrl+KeyD', 'Meta+KeyD'], mono: 'editor.action.addSelectionToNextFindMatch' },
  { id: 'ed.indent',      scope: 'editor', name: '增加缩进', keys: ['Ctrl+BracketRight', 'Meta+BracketRight'], mono: 'editor.action.indentLines' },
  { id: 'ed.outdent',     scope: 'editor', name: '减少缩进', keys: ['Ctrl+BracketLeft', 'Meta+BracketLeft'], mono: 'editor.action.outdentLines' },
  { id: 'ed.find',        scope: 'editor', name: '编辑器内查找', keys: ['Ctrl+KeyF', 'Meta+KeyF'], mono: 'actions.find' },
  { id: 'ed.replace',     scope: 'editor', name: '编辑器内替换', keys: ['Ctrl+KeyH', 'Meta+KeyH'], mono: 'editor.action.startFindReplaceAction' },
  { id: 'ed.gotoLine',    scope: 'editor', name: '跳转到行…', keys: ['Ctrl+KeyG', 'Meta+KeyG'], mono: 'editor.action.gotoLine' },

  { id: 'cmp.send',      scope: 'composer', name: '发送消息（Ctrl+Enter）', keys: ['Ctrl+Enter', 'Meta+Enter'] },
  { id: 'cmp.sendEnter', scope: 'composer', name: '发送消息（Enter）', keys: ['Enter'] },
  { id: 'cmp.polish',  scope: 'composer', name: 'AI 润色输入', keys: ['Ctrl+KeyI', 'Meta+KeyI'] },
  { id: 'cmp.clear',   scope: 'composer', name: '清空输入框', keys: ['Escape'] },
  { id: 'cmp.history', scope: 'composer', name: '调出上一条历史', keys: ['ArrowUp'] },

  { id: 'global.find', scope: 'global', name: '对话内查找', keys: ['Ctrl+KeyF', 'Meta+KeyF'] },
  { id: 'global.help', scope: 'global', name: '快捷键清单', keys: ['Shift+Slash', 'Ctrl+Slash', 'Meta+Slash'] },
];

function hkDef(id) {
  return HK_DEFS.find((d) => d.id === id) || null;
}

// 一条规则的全部按键 → 展示名数组
function hkLabelList(id) {
  const d = hkDef(id);
  if (!d) return [];
  if (d.dyn) return ['1 – 9'];
  if (d.hold) return ['按住 Ctrl'];
  const seen = new Set();
  const out = [];
  hkKeysOf(id).forEach((k) => {
    const s = hkLabel(k);
    if (!seen.has(s)) { seen.add(s); out.push(s); }
  });
  return out;
}

/* ---------- 存储 ---------- */
let _hkOverrides = null;

function hkOverrides() {
  if (_hkOverrides) return _hkOverrides;
  let raw = {};
  try { raw = JSON.parse(localStorage.getItem(HK_STORE_KEY) || '{}') || {}; } catch (e) { raw = {}; }
  const out = {};
  HK_DEFS.forEach((d) => {
    const v = raw[d.id];
    if (!d.fixed && typeof v === 'string' && v) out[d.id] = v;
  });
  _hkOverrides = out;
  return out;
}

function hkSaveOverrides(o) {
  _hkOverrides = o;
  try { localStorage.setItem(HK_STORE_KEY, JSON.stringify(o)); } catch (e) { /* 忽略 */ }
  hkSyncEditorKeybindings();
}

/* 改键后把「编辑器」作用域的覆盖同步到 Monaco（解绑原键 + 绑新键）。
   默认没有任何覆盖时**一条都不下发** —— 保证不改键就等于完全不介入 Monaco。 */
function hkSyncEditorKeybindings() {
  if (typeof ideApplyEditorKeybindings === 'function') {
    try { ideApplyEditorKeybindings(); } catch (e) { /* 忽略：编辑器不在时什么都不用做 */ }
  }
}

// 该规则当前生效的按键（改键后只保留新键）
function hkKeysOf(id) {
  return hkKeysOfDef(hkDef(id), hkOverrides());
}

function hkIsCustom(id) {
  return !!hkOverrides()[id];
}

/* 找出本次按键命中的规则；判定逻辑在 hk-core.js 的 hkHitIn（含「用户自定义优先」），
   这里只负责把 localStorage 里的覆盖项喂进去。 */
function hkHit(e, scopes) {
  return hkHitIn(HK_DEFS, hkKeyOf(e), scopes, hkOverrides());
}

/* ---------- 改键 ---------- */
function hkResetOne(id) {
  const o = Object.assign({}, hkOverrides());
  delete o[id];
  hkSaveOverrides(o);
  hkRenderAll();
  hkHudRefresh();
}

function hkResetAll() {
  hkSaveOverrides({});
  hkRenderAll();
  hkHudRefresh();
}

let _hkRecording = '';

function hkRecordStart(id, btn) {
  if (_hkRecording) return;
  const d = hkDef(id);
  if (!d || d.fixed) return;
  _hkRecording = id;
  const old = btn.textContent;
  btn.textContent = '按下新键…';
  btn.classList.add('rec');
  const done = (tip) => {
    document.removeEventListener('keydown', onKey, true);
    _hkRecording = '';
    btn.classList.remove('rec');
    btn.textContent = old;
    if (tip) toast(tip, 'error');
  };
  const onKey = (e) => {
    e.preventDefault();
    e.stopImmediatePropagation();                        // 录制期间不让设置面板 / 全局快捷键插进来
    if (e.key === 'Escape') { done(''); return; }
    if (HK_MOD_KEYS.indexOf(e.code) >= 0) return;        // 只按修饰键不算
    const spec = hkKeyOf(e);
    const bad = hkSpecProblem(d.scope, spec);
    if (bad) { done(bad); return; }
    // 同作用域重键 → 拒绝（同作用域里两个动作抢一个键必然自相矛盾）
    const dup = hkFindDup(HK_DEFS, hkOverrides(), id, spec);
    if (dup) { done('「' + hkLabel(spec) + '」已被「' + dup.name + '」占用，请换一个'); return; }
    // 跨作用域重键 → 允许（互不干扰的作用域本来就靠「焦点 + 谁先响应」分流），
    // 但要说清楚：按「用户自定义优先」规则，改过的这条会压过对方的默认键。
    const cross = hkFindCross(HK_DEFS, hkOverrides(), id, spec);
    const o = Object.assign({}, hkOverrides());
    o[id] = spec;
    hkSaveOverrides(o);
    done('');
    hkRenderAll();
    hkHudRefresh();
    toast('「' + d.name + '」已改为 ' + hkLabel(spec));
    if (cross.length) {
      toast('注意：' + hkLabel(spec) + ' 原本是「' + cross[0].name + '」的键；'
            + '按你的自定义，现在优先执行「' + d.name + '」');
    }
  };
  document.addEventListener('keydown', onKey, true);
}

/* ---------- 改键清单：同一套渲染，多处挂载（「?」弹窗 + 设置面板的「快捷键」页） ---------- */
/* 每个面板 = 容器选择器 + 搜索框选择器。改键 / 重置后要重渲染**所有**已挂载的面板，
   否则会出现「设置里改了、按 ? 打开还是旧键」的不一致。 */
const HK_PANELS = [
  { box: '#hk-groups', search: '#hk-search' },
  { box: '#set-hk-groups', search: '#set-hk-search' },
];
function hkRenderAll() {
  HK_PANELS.forEach((p) => hkRenderPanel(p.box, p.search));
}

function hkPanelOpen(force) {
  const p = $('#hk-panel');
  if (!p) return;
  const show = (typeof force === 'boolean') ? force : p.classList.contains('hidden');
  if (show) { hkRenderAll(); showWithAnim(p); }
  else hideWithAnim(p);
}

function hkRenderPanel(boxSel, searchSel) {
  const box = $(boxSel);
  if (!box) return;
  const q = (($(searchSel) || {}).value || '').trim().toLowerCase();
  const mode = (state && state.uiMode) || 'solo';
  box.innerHTML = '';
  let shown = 0;
  HK_SCOPES.forEach((sc) => {
    const rows = HK_DEFS.filter((d) => d.scope === sc.id)
      .filter((d) => !q || (d.name + ' ' + hkLabelList(d.id).join(' ')).toLowerCase().includes(q));
    if (!rows.length) return;
    shown += rows.length;
    const sec = document.createElement('div');
    sec.className = 'hk-sec';
    const h = document.createElement('div');
    h.className = 'hk-sec-title';
    h.textContent = sc.name;
    sec.appendChild(h);
    rows.forEach((d) => sec.appendChild(hkRowEl(d, mode)));
    box.appendChild(sec);
  });
  if (!shown) {
    const em = document.createElement('div');
    em.className = 'hk-empty';
    em.textContent = '没有匹配的快捷键';
    box.appendChild(em);
  }
}

function hkRowEl(d, mode) {
  const row = document.createElement('div');
  row.className = 'hk-row' + (hkScopeActive(d, mode) ? '' : ' off');
  const nm = document.createElement('div');
  nm.className = 'hk-row-name';
  nm.textContent = d.name;
  row.appendChild(nm);

  const keys = document.createElement('div');
  keys.className = 'hk-row-keys';
  hkLabelList(d.id).forEach((t) => {
    const k = document.createElement('kbd');
    k.textContent = t;
    keys.appendChild(k);
  });
  row.appendChild(keys);

  const act = document.createElement('div');
  act.className = 'hk-row-act';
  if (d.fixed) {
    const s = document.createElement('span');
    s.className = 'hk-fixed';
    s.textContent = d.dyn ? '按显示序号' : (d.hold ? '按住类' : '固定');
    act.appendChild(s);
  } else {
    const rb = document.createElement('button');
    rb.className = 'ghost hk-btn';
    rb.textContent = '改键';
    rb.onclick = () => hkRecordStart(d.id, rb);
    act.appendChild(rb);
    if (hkIsCustom(d.id)) {
      const xb = document.createElement('button');
      xb.className = 'ghost hk-btn';
      xb.textContent = '重置';
      xb.onclick = () => hkResetOne(d.id);
      act.appendChild(xb);
    }
  }
  row.appendChild(act);
  return row;
}

function hkScopeActive(d, mode) {
  if (d.scope === 'global') return true;
  if (d.scope === 'composer') return true;
  if (d.scope === 'editor') return mode === 'ide';   // 编辑器快捷键只在 IDE 模式下有意义
  return d.scope === mode;
}

/* ---------- 实时 HUD ---------- */
let _hkHudTimer = null;

function hkHudOpen() {
  const h = $('#hk-hud');
  return !!(h && !h.classList.contains('collapsed'));
}

function hkHudToggle(force) {
  const h = $('#hk-hud');
  if (!h) return;
  const open = (typeof force === 'boolean') ? force : h.classList.contains('collapsed');
  h.classList.toggle('collapsed', !open);
  try { localStorage.setItem(HK_HUD_KEY, open ? '1' : '0'); } catch (e) { /* 忽略 */ }
  if (open) { hkHudRefresh(); hkHudTimerStart(); }
  else hkHudTimerStop();
}

function hkHudTimerStart() {
  hkHudTimerStop();
  // 展开期间才轮询：选项浮层 / 打字状态 / 模式切换都会即时反映到提示条上
  _hkHudTimer = setInterval(hkHudRefresh, 500);
}

function hkHudTimerStop() {
  if (_hkHudTimer) { clearInterval(_hkHudTimer); _hkHudTimer = null; }
}

function hkPrimaryLabel(id) {
  const l = hkLabelList(id);
  return l[0] || '';
}

// 当前模式 + 状态下最该出现的几条
function hkHudItems() {
  const mode = (state && state.uiMode) || 'solo';
  const out = [];
  if (mode === 'gal') {
    const box = $('#gal-options');
    const hasOpts = !!(box && !box.classList.contains('gal-options-out') &&
                       box.querySelector('.gal-option'));
    if (hasOpts) out.push(['1 – 9', '选择']);
    const typing = !!(typeof galState !== 'undefined' && galState.typer);
    out.push([hkPrimaryLabel('gal.advance'), typing ? '补全' : '推进']);
    out.push(['按住 Ctrl', '快进']);
    out.push([hkPrimaryLabel('gal.auto'), '自动']);
    if (!hasOpts) out.push([hkPrimaryLabel('gal.review'), '回顾']);
  } else if (mode === 'ide') {
    out.push([hkPrimaryLabel('ide.save'), '保存']);
    out.push([hkPrimaryLabel('ide.open'), '打开']);
    out.push([hkPrimaryLabel('ide.palette'), '命令']);
  } else {
    out.push(['Enter', '发送']);
    out.push([hkPrimaryLabel('global.find'), '查找']);
  }
  out.push(['?', '全部']);
  return out;
}

function hkHudRefresh() {
  const box = $('#hk-hud-list');
  if (!box || !hkHudOpen()) return;
  const items = hkHudItems();
  const sig = JSON.stringify(items);
  if (box.dataset.sig === sig) return;                 // 内容没变就不重排，避免闪烁
  box.dataset.sig = sig;
  box.innerHTML = '';
  items.forEach((it) => {
    const s = document.createElement('span');
    s.className = 'hk-hud-item';
    const k = document.createElement('kbd');
    k.textContent = it[0];
    const t = document.createElement('span');
    t.textContent = it[1];
    s.appendChild(k); s.appendChild(t);
    if (it[0] === '?') {
      s.classList.add('hk-hud-more');
      s.onclick = () => hkPanelOpen(true);
    }
    box.appendChild(s);
  });
}

/* ---------- 初始化 ---------- */
function initHotkeys() {
  const hud = $('#hk-hud');
  if (hud) {
    let open = false;
    try { open = localStorage.getItem(HK_HUD_KEY) === '1'; } catch (e) { open = false; }
    hud.classList.toggle('collapsed', !open);
    const tg = $('#hk-hud-toggle');
    if (tg) tg.onclick = () => hkHudToggle();
    if (open) { hkHudRefresh(); hkHudTimerStart(); }
  }
  const p = $('#hk-panel');
  if (p) {
    const c = $('#hk-panel-close');
    if (c) c.onclick = () => hkPanelOpen(false);
    p.addEventListener('click', (e) => { if (e.target === p) hkPanelOpen(false); });
    const s = $('#hk-search');
    if (s) s.oninput = () => hkRenderPanel('#hk-groups', '#hk-search');
    const ra = $('#hk-reset-all');
    if (ra) ra.onclick = () => {
      if (confirm('把所有快捷键恢复为默认？')) { hkResetAll(); toast('已恢复默认快捷键'); }
    };
  }
  // 设置面板 → 「快捷键」页：同一套改键 UI 的第二处挂载
  const setSearch = $('#set-hk-search');
  if (setSearch) setSearch.oninput = () => hkRenderPanel('#set-hk-groups', '#set-hk-search');
  const setReset = $('#set-hk-reset-all');
  if (setReset) setReset.onclick = () => {
    if (confirm('把所有快捷键恢复为默认？')) { hkResetAll(); toast('已恢复默认快捷键'); }
  };
  hkRenderAll();          // 初始化时渲染一次（数据量极小，隐藏着也无成本）
  // 全局监听走捕获阶段：才能在输入框自己的 keydown 之前处理 Esc
  document.addEventListener('keydown', (e) => {
    if (_hkRecording) return;
    // Esc：清单打开时优先关清单（输入框内的「清空」让位）
    if (e.key === 'Escape' && isOpen($('#hk-panel'))) {
      e.preventDefault(); e.stopImmediatePropagation();
      hkPanelOpen(false);
      return;
    }
    const hit = hkHit(e, ['global']);
    if (!hit || hit.id !== 'global.help') return;
    // 编辑器聚焦时把 Ctrl+/ 让给 Monaco —— 那是 VS Code 的「切换行注释」。
    // 让路判断必须在这里：Monaco 的输入区是个 <textarea>，会被 isTypingTarget 命中，
    // 而下面的判断在按住 Ctrl 时**不**返回，于是 Ctrl+/ 会被我们抢走。
    if (typeof ideEditorFocused === 'function' && ideEditorFocused()) return;
    // 输入框内只认 Ctrl/⌘ 组合，避免正常打「?」时误触
    if (isTypingTarget(e.target) && !(e.ctrlKey || e.metaKey)) return;
    e.preventDefault(); e.stopImmediatePropagation();
    hkPanelOpen(!isOpen($('#hk-panel')));
  }, true);
}

// Node 单元测试用（浏览器里 module 未定义，这段不执行）
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { HK_DEFS, HK_SCOPES, hkDef, hkKeysOf, hkHit };
}

