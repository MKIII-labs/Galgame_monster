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

  /* ---- 运行 / 调试 ----
     这些键由 gal-ide.js 的调试面板实现（不是 Monaco 动作），所以放在 ide 作用域：
     只在 IDE 模式下、焦点不在输入框 / 编辑器里时生效。 */
  { id: 'ide.debugStart', scope: 'ide', name: '启动调试 / 继续运行', keys: ['F5'] },
  { id: 'ide.debugNext',  scope: 'ide', name: '单步跳过', keys: ['F10'] },
  { id: 'ide.debugIn',    scope: 'ide', name: '单步进入', keys: ['F11'] },
  { id: 'ide.debugOut',   scope: 'ide', name: '单步跳出', keys: ['Shift+F11'] },
  { id: 'ide.debugStop',  scope: 'ide', name: '停止调试', keys: ['Shift+F5'] },

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

  /* ---- IDE 进阶（由 gal-ide.js 通过 addAction 注册的 gm.* 动作）----
     默认键就是 VS Code / Trae 的习惯；只有改键时才会解绑默认键并绑回同一个动作
     （见 gal-ide.js::ideApplyEditorKeybindings，按 mono 找到动作 id）。 */
  { id: 'ed.gotoDef',     scope: 'editor', name: '转到定义', keys: ['F12'], mono: 'gm.gotoDef' },
  { id: 'ed.gotoRefs',    scope: 'editor', name: '查找引用', keys: ['Shift+F12'], mono: 'gm.gotoRefs' },
  { id: 'ed.renameSymbol', scope: 'editor', name: '重命名符号', keys: ['F2'], mono: 'gm.renameSymbol' },
  { id: 'ed.outline',     scope: 'editor', name: '文件大纲', keys: ['Ctrl+Shift+KeyO'], mono: 'gm.outline' },
  { id: 'ed.problems',    scope: 'editor', name: '问题面板', keys: ['Ctrl+Shift+KeyM'], mono: 'gm.problems' },
  { id: 'ed.inlineEdit',  scope: 'editor', name: 'AI 改写选中代码', keys: ['Ctrl+KeyI'], mono: 'gm.inlineEdit' },
  { id: 'ed.inlineAsk',   scope: 'editor', name: 'AI 问问选中代码', keys: ['Ctrl+Alt+KeyI'], mono: 'gm.inlineAsk' },
  { id: 'ed.mdPreview',   scope: 'editor', name: 'Markdown 预览', keys: ['Ctrl+Shift+KeyV'], mono: 'gm.mdPreview' },
  { id: 'ed.texBuild',    scope: 'editor', name: '编译 LaTeX', keys: ['Ctrl+Alt+KeyB'], mono: 'gm.texBuild' },
  { id: 'ed.runFile',     scope: 'editor', name: '运行 / 编译并运行', keys: ['Ctrl+Alt+KeyR'], mono: 'gm.runFile' },
  { id: 'ed.formatDoc',   scope: 'editor', name: '格式化文档', keys: ['Shift+Alt+KeyF'], mono: 'gm.formatDoc' },
  { id: 'ed.addAnno',     scope: 'editor', name: '为选中行添加批注', keys: ['Ctrl+Alt+KeyM'], mono: 'gm.addAnno' },

  { id: 'cmp.send',      scope: 'composer', name: '发送消息（Ctrl+Enter）', keys: ['Ctrl+Enter', 'Meta+Enter'] },
  { id: 'cmp.sendEnter', scope: 'composer', name: '发送消息（Enter）', keys: ['Enter'] },
  { id: 'cmp.polish',  scope: 'composer', name: 'AI 润色输入', keys: ['Ctrl+KeyI', 'Meta+KeyI'] },
  { id: 'cmp.clear',   scope: 'composer', name: '清空输入框', keys: ['Escape'] },
  { id: 'cmp.history', scope: 'composer', name: '调出上一条历史', keys: ['ArrowUp'] },

  { id: 'global.find', scope: 'global', name: '对话内查找', keys: ['Ctrl+KeyF', 'Meta+KeyF'] },
  { id: 'global.render', scope: 'global', name: '切换渲染 / 源码（对话与笔记）', keys: ['Ctrl+Shift+KeyV', 'Meta+Shift+KeyV'] },
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
    // 空串也是有效覆盖 = 用户主动解除了绑定
    if (!d.fixed && typeof v === 'string') out[d.id] = v;
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
  return Object.prototype.hasOwnProperty.call(hkOverrides(), id);
}

function hkScopeName(id) {
  const s = HK_SCOPES.find((x) => x.id === id);
  return s ? s.name : (id || '');
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
}

function hkResetAll() {
  hkSaveOverrides({});
  hkRenderAll();
}

/* 只清掉某个作用域的覆盖项（编辑器作用域会连带把 Monaco 侧恢复默认） */
function hkResetScope(scope) {
  const o = Object.assign({}, hkOverrides());
  let n = 0;
  HK_DEFS.forEach((d) => {
    if (d.scope !== scope) return;
    if (Object.prototype.hasOwnProperty.call(o, d.id)) { delete o[d.id]; n += 1; }
  });
  if (!n) { toast('「' + hkScopeName(scope) + '」没有改过的键'); return; }
  hkSaveOverrides(o);
  hkRenderAll();
  toast('已恢复「' + hkScopeName(scope) + '」的 ' + n + ' 项默认键');
}

/* 解除绑定：存空串，命中判定会跳过这条动作（编辑器作用域同时解绑 Monaco 默认键） */
function hkUnbind(id) {
  const d = hkDef(id);
  if (!d || d.fixed) return;
  const o = Object.assign({}, hkOverrides());
  o[id] = '';
  hkSaveOverrides(o);
  hkRenderAll();
  toast('「' + d.name + '」已解除绑定');
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
  hkDocRefreshKeys();
  hkRenderHelpSummary();
}

function hkPanelOpen(force) {
  const p = $('#hk-panel');
  if (!p) return;
  const show = (typeof force === 'boolean') ? force : p.classList.contains('hidden');
  if (show) { hkRenderAll(); showWithAnim(p); }
  else hideWithAnim(p);
}

/* ---------- 改键清单：同一套渲染，多处挂载（「?」弹窗 + 设置面板的「快捷键」页） ---------- */
/* 每个面板 = 容器选择器 + 搜索框选择器。容器上记着两个筛选状态（dataset），
   重渲染时不会丢：scope = 作用域，only = 仅看已改。 */

// 说明里的「生效条件」按 def id 反查，改键行直接显示，不用再翻下面的完整说明
function hkDocCondOf(id) {
  const groups = hkDocGroups();
  for (let i = 0; i < groups.length; i += 1) {
    const rows = groups[i].rows || [];
    for (let j = 0; j < rows.length; j += 1) {
      if (rows[j].def === id && rows[j].cond) return rows[j].cond;
    }
  }
  return '';
}

// 只在「用户改过这条」时检查冲突：默认键之间的跨作用域同键是有意的分流，不该报警
function hkConflictsOf(d) {
  if (!hkIsCustom(d.id)) return [];
  const keys = hkKeysOf(d.id);
  if (!keys.length) return [];
  const out = [];
  HK_DEFS.forEach((o) => {
    if (o.id === d.id || o.dyn || o.hold || o.displayOnly) return;
    const shared = hkKeysOf(o.id).filter((k) => keys.indexOf(k) >= 0);
    if (shared.length) out.push({ def: o, keys: shared });
  });
  return out;
}

function hkRowHay(d) {
  return [d.name, hkScopeName(d.scope), hkDocCondOf(d.id),
          hkLabelList(d.id).join(' ')].join(' ').toLowerCase();
}

function hkChipEl(boxSel, searchSel, id, label, active) {
  const b = document.createElement('button');
  b.type = 'button';
  b.className = 'hk-chip' + (active ? ' active' : '');
  b.textContent = label;
  b.onclick = () => {
    const box = $(boxSel);
    if (box) box.dataset.scope = id;
    hkRenderPanel(boxSel, searchSel);
  };
  return b;
}

function hkToolsEl(boxSel, searchSel, scope, only) {
  const bar = document.createElement('div');
  bar.className = 'hk-tools';
  const chips = document.createElement('div');
  chips.className = 'hk-chips';
  chips.appendChild(hkChipEl(boxSel, searchSel, '', '全部', !scope));
  HK_SCOPES.forEach((sc) => {
    chips.appendChild(hkChipEl(boxSel, searchSel, sc.id, sc.name, scope === sc.id));
  });
  bar.appendChild(chips);

  const lab = document.createElement('label');
  lab.className = 'hk-only';
  const cb = document.createElement('input');
  cb.type = 'checkbox';
  cb.checked = !!only;
  cb.onchange = () => {
    const box = $(boxSel);
    if (box) box.dataset.only = cb.checked ? '1' : '';
    hkRenderPanel(boxSel, searchSel);
  };
  lab.appendChild(cb);
  lab.appendChild(document.createTextNode(' 仅看已改'));
  bar.appendChild(lab);
  return bar;
}

function hkRenderPanel(boxSel, searchSel) {
  const box = $(boxSel);
  if (!box) return;
  const q = (($(searchSel) || {}).value || '').trim().toLowerCase();
  const mode = (state && state.uiMode) || 'solo';
  const scope = box.dataset.scope || '';
  const only = box.dataset.only === '1';
  box.innerHTML = '';
  box.appendChild(hkToolsEl(boxSel, searchSel, scope, only));

  let shown = 0;
  HK_SCOPES.forEach((sc) => {
    if (scope && sc.id !== scope) return;
    const rows = HK_DEFS.filter((d) => d.scope === sc.id)
      .filter((d) => !only || hkIsCustom(d.id))
      .filter((d) => !q || hkRowHay(d).includes(q));
    if (!rows.length) return;
    shown += rows.length;
    const sec = document.createElement('div');
    sec.className = 'hk-sec';
    const head = document.createElement('div');
    head.className = 'hk-sec-head';
    const h = document.createElement('div');
    h.className = 'hk-sec-title';
    h.textContent = sc.name;
    head.appendChild(h);
    if (rows.some((d) => hkIsCustom(d.id))) {
      const rb = document.createElement('button');
      rb.type = 'button';
      rb.className = 'ghost hk-btn hk-sec-reset';
      rb.textContent = '恢复本组';
      rb.title = '只把「' + sc.name + '」里改过的键恢复为默认';
      rb.onclick = () => hkResetScope(sc.id);
      head.appendChild(rb);
    }
    sec.appendChild(head);
    rows.forEach((d) => sec.appendChild(hkRowEl(d, mode)));
    box.appendChild(sec);
  });
  if (!shown) {
    const em = document.createElement('div');
    em.className = 'hk-empty';
    em.textContent = only ? '这个分组里还没有改过的键' : '没有匹配的快捷键';
    box.appendChild(em);
  }
  const total = HK_DEFS.filter((d) => !d.fixed).length;
  const changed = HK_DEFS.filter((d) => hkIsCustom(d.id)).length;
  const sum = document.createElement('div');
  sum.className = 'hk-sum';
  sum.textContent = '共 ' + total + ' 项可改键 · 已改 ' + changed + ' 项';
  box.appendChild(sum);
}

function hkRowEl(d, mode) {
  const row = document.createElement('div');
  row.className = 'hk-row' + (hkScopeActive(d, mode) ? '' : ' off');

  const main = document.createElement('div');
  main.className = 'hk-row-main';
  const nm = document.createElement('div');
  nm.className = 'hk-row-name';
  nm.appendChild(document.createTextNode(d.name));
  const sc = document.createElement('span');
  sc.className = 'hk-row-scope';
  sc.textContent = hkScopeName(d.scope);
  nm.appendChild(sc);
  const conf = hkConflictsOf(d);
  if (conf.length) {
    const sameScope = conf.some((x) => x.def.scope === d.scope);
    const badge = document.createElement('span');
    badge.className = 'hk-badge' + (sameScope ? ' danger' : ' warn');
    badge.textContent = sameScope ? '同组冲突' : '跨组同键';
    badge.title = conf.map((x) => x.keys.map(hkLabel).join(' / ') + ' · ' +
      hkScopeName(x.def.scope) + ' · ' + x.def.name).join('\n');
    nm.appendChild(badge);
  }
  main.appendChild(nm);

  const cond = hkDocCondOf(d.id);
  if (cond) {
    const cd = document.createElement('div');
    cd.className = 'hk-row-cond';
    cd.textContent = '生效：' + cond;
    cd.title = cond;
    main.appendChild(cd);
  }
  row.appendChild(main);

  const keys = document.createElement('div');
  keys.className = 'hk-row-keys';
  const list = hkLabelList(d.id);
  if (!list.length) {
    const none = document.createElement('span');
    none.className = 'hk-none';
    none.textContent = '未绑定';
    keys.appendChild(none);
  } else {
    list.forEach((t) => {
      const k = document.createElement('kbd');
      k.textContent = t;
      keys.appendChild(k);
    });
  }
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
    if (list.length) {
      const ub = document.createElement('button');
      ub.className = 'ghost hk-btn';
      ub.textContent = '解除';
      ub.title = '取消绑定，之后按这个键不再触发该动作';
      ub.onclick = () => hkUnbind(d.id);
      act.appendChild(ub);
    }
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

/* ---------- 完整说明：数据在 hk-doc.js，按键从注册表现取 ---------- */
/* 这些函数只负责渲染。可改键的条目引用 HK_DEFS 的 id，所以改键 / 重置后
   说明里的键位会跟着变，不会再出现「上面改了、下面还是旧键」。 */
function hkDocGroups() {
  return (typeof HK_DOC_GROUPS !== 'undefined' && Array.isArray(HK_DOC_GROUPS))
    ? HK_DOC_GROUPS : [];
}

function hkDocKeys(row) {
  if (row && row.def) return hkLabelList(row.def);
  return (row && row.keys) ? row.keys.slice() : [];
}

function hkDocName(row) {
  if (row && row.def) {
    const d = hkDef(row.def);
    return d ? d.name : row.def;
  }
  return (row && row.name) || '';
}

function hkDocKeyEl(text, defId) {
  const k = document.createElement('kbd');
  k.textContent = text;
  if (defId) k.dataset.def = defId;
  return k;
}

function hkDocRowEl(row) {
  const tr = document.createElement('tr');
  const keys = hkDocKeys(row);

  const tdK = document.createElement('td');
  const kbox = document.createElement('span');
  kbox.className = 'hk-doc-keys';
  if (row.def) {
    kbox.dataset.def = row.def;
    keys.forEach((t) => kbox.appendChild(hkDocKeyEl(t, row.def)));
  } else {
    keys.forEach((t) => kbox.appendChild(hkDocKeyEl(t, '')));
  }
  if (!keys.length) kbox.textContent = '—';
  tdK.appendChild(kbox);

  const tdN = document.createElement('td');
  tdN.appendChild(document.createTextNode(hkDocName(row)));
  if (row.def && !(hkDef(row.def) || {}).fixed) {
    const tag = document.createElement('span');
    tag.className = 'hk-doc-tag';
    tag.textContent = '可改键';
    tdN.appendChild(tag);
  }

  const tdC = document.createElement('td');
  tdC.className = 'hk-doc-cond';
  tdC.textContent = row.cond || '—';
  if (row.note) {
    const n = document.createElement('div');
    n.className = 'hk-doc-note';
    n.textContent = row.note;
    tdC.appendChild(n);
  }

  tr.appendChild(tdK);
  tr.appendChild(tdN);
  tr.appendChild(tdC);
  tr.dataset.search = [hkDocName(row), keys.join(' '), row.cond || '',
                       row.note || '', row.search || ''].join(' ').toLowerCase();
  return tr;
}

function hkRenderDoc(boxSel) {
  const box = $(boxSel);
  if (!box) return;
  const groups = hkDocGroups();
  box.innerHTML = '';
  if (!groups.length) return;
  groups.forEach((g) => {
    const sec = document.createElement('div');
    sec.className = 'hk-doc-sec';
    if (g.id) sec.dataset.group = g.id;
    // 给设置页全局搜索一个落点（buildSettingsSearchIndex 只收 [data-search]）
    if (g.search) sec.dataset.search = g.search;
    const h = document.createElement('div');
    h.className = 'hk-doc-title';
    h.textContent = g.title;
    sec.appendChild(h);
    const table = document.createElement('table');
    table.className = 'key-table';
    const thead = document.createElement('thead');
    thead.innerHTML = '<tr><th>按键</th><th>作用</th><th>生效条件</th></tr>';
    table.appendChild(thead);
    const tb = document.createElement('tbody');
    (g.rows || []).forEach((row) => tb.appendChild(hkDocRowEl(row)));
    table.appendChild(tb);
    sec.appendChild(table);
    box.appendChild(sec);
  });
  hkFilterDoc(($('#set-hk-search') || {}).value || '');
}

// 改键 / 重置后只换 kbd 文本，不重建节点（重建会丢掉 set-card 装饰）
function hkDocRefreshKeys() {
  $$('#set-hk-doc .hk-doc-keys[data-def]').forEach((box) => {
    const id = box.dataset.def;
    box.innerHTML = '';
    hkLabelList(id).forEach((t) => box.appendChild(hkDocKeyEl(t, id)));
    if (!box.children.length) box.textContent = '—';
  });
  const f = $('#set-hk-search');
  if (f) hkFilterDoc(f.value);
}

function hkFilterDoc(q) {
  const box = $('#set-hk-doc');
  if (!box) return;
  const kws = String(q || '').trim().toLowerCase().split(/\s+/).filter(Boolean);
  box.querySelectorAll('.hk-doc-sec').forEach((sec) => {
    let visible = 0;
    sec.querySelectorAll('tbody tr').forEach((tr) => {
      const hit = !kws.length || kws.every((k) => (tr.dataset.search || '').includes(k));
      tr.classList.toggle('hidden', !hit);
      if (hit) visible += 1;
    });
    sec.classList.toggle('hidden', !visible);
  });
}

/* 「功能说明 → 快捷键总览」的摘要：同一张表生成，改键后也跟着变 */
function hkRenderHelpSummary() {
  const box = document.getElementById('help-key-summary');
  if (!box) return;
  const groups = hkDocGroups();
  if (!groups.length) {
    box.innerHTML = '<li class="hint">完整清单见左侧「快捷键」。</li>';
    return;
  }
  const total = groups.reduce((n, g) => n + (g.rows || []).length, 0);
  const part = groups.map((g) => {
    const rows = g.rows || [];
    const head = rows.slice(0, 2).map((r) => {
      const k = hkDocKeys(r)[0] || '';
      return (k ? '<kbd>' + escapeHtml(k) + '</kbd> ' : '') + escapeHtml(hkDocName(r));
    }).join('、');
    const more = rows.length > 2 ? ' 等 ' + rows.length + ' 条' : '';
    return '<li><strong>' + escapeHtml(g.title) + '</strong>：' + head + more + '</li>';
  }).join('');
  box.innerHTML = part + '<li class="hk-doc-sum">共 ' + total +
    ' 条，完整清单与改键在左侧「<strong>快捷键</strong>」。</li>';
}

/* ---------- 初始化 ---------- */
function initHotkeys() {
  const tg = $('#hk-hud-toggle');
  if (tg) {
    // 右下角这个按钮只做「打开功能说明」，不再有 HUD 展开态
    tg.onclick = () => {
      if (typeof openModal === 'function') openModal({ tab: 'help' });
    };
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
    // 「?」清单只放改键；完整说明在设置里，这里给一个直达入口
    const od = $('#hk-open-doc');
    if (od) od.onclick = () => {
      hkPanelOpen(false);
      if (typeof openModal === 'function') openModal({ tab: 'keys' });
    };
  }
  // 设置面板 → 「快捷键」页：同一套改键 UI + 自动生成的完整说明
  const setSearch = $('#set-hk-search');
  if (setSearch) {
    setSearch.oninput = () => {
      hkRenderPanel('#set-hk-groups', '#set-hk-search');
      hkFilterDoc(setSearch.value);
    };
  }
  const setReset = $('#set-hk-reset-all');
  if (setReset) setReset.onclick = () => {
    if (confirm('把所有快捷键恢复为默认？')) { hkResetAll(); toast('已恢复默认快捷键'); }
  };
  hkRenderDoc('#set-hk-doc');   // 先渲染说明，再统一刷新（改键后只换 kbd 文本）
  hkRenderAll();                // 初始化时渲染一次（数据量极小，隐藏着也无成本）
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

