'use strict';

/* ============================================================
   输入框增强（L1 交互 + L2 AI 按需）

   L1（纯本地，< 10ms，不调模型）：
     清空按钮 / 实时反馈（字符数·提示）/ 历史补全 /
     `/` 指令菜单 / `@` 引用记忆条目 / 快捷键 /
     焦点管理 / 粘贴优化 / 无障碍（role+aria+纯键盘可达）

   L2（按需触发，走模型，见 improveInput）：
     AI 优化输入：润色 / 精简 / 扩写 / 翻译 → POST /api/compose
   ============================================================ */

const CMP_HISTORY_KEY = 'composer_history_v1';
const CMP_HISTORY_MAX = 50;
const CMP_POP_MAX = 6;
const CMP_LONG_TEXT = 5000;        // 超过此字数提示「内容较长」

// `/` 指令表：kind=local 本地立即执行；kind=ai 交给 AI 处理；group 只用于面板分组
const CMP_COMMANDS = [
  { cmd: '/clear',     desc: '清空输入框',              kind: 'local', group: '会话' },
  { cmd: '/new',       desc: '新建会话',                kind: 'local', group: '会话' },
  { cmd: '/compress',  desc: '压缩较早的历史对话',      kind: 'local', group: '会话' },
  { cmd: '/kb',        desc: '打开知识库 / 记忆图谱',   kind: 'local', group: '视图' },
  { cmd: '/settings',  desc: '打开设置',                kind: 'local', group: '视图' },
  { cmd: '/theme',     desc: '切换明暗主题',            kind: 'local', group: '视图' },
  { cmd: '/polish',    desc: 'AI 润色当前输入',         kind: 'ai', action: 'polish', group: 'AI' },
  { cmd: '/spec',      desc: 'AI 改成规格（目标/范围/边界/验收）', kind: 'ai', action: 'spec', group: 'AI' },
  { cmd: '/shorten',   desc: 'AI 精简当前输入',         kind: 'ai', action: 'shorten', group: 'AI' },
  { cmd: '/expand',    desc: 'AI 扩写当前输入',         kind: 'ai', action: 'expand', group: 'AI' },
  { cmd: '/translate', desc: 'AI 翻译当前输入（如 /translate 英文）', kind: 'ai', action: 'translate', group: 'AI' },
];

let _popOpen = false;
let _popItems = [];
let _popIndex = -1;
let _refNodes = [];        // `@` 引用的数据源之一：当前会话的记忆条目（预加载缓存）
let _refFiles = [];        // `@` 引用的数据源之二：当前沙箱里的文件（相对路径）
let _refFilesKey = '';     // 会话 + 沙箱路径；变了才重新拉树，避免每次敲 @ 都请求

function cmpInput() { return $('#input'); }

/* ---------- 历史记录（localStorage） ---------- */
function cmpHistory() {
  try { return JSON.parse(localStorage.getItem(CMP_HISTORY_KEY) || '[]') || []; }
  catch (e) { return []; }
}

function cmpRemember(text) {
  const t = (text || '').trim();
  if (!t || t.length < 2) return;
  let h = cmpHistory().filter((x) => x !== t);
  h.unshift(t);
  if (h.length > CMP_HISTORY_MAX) h = h.slice(0, CMP_HISTORY_MAX);
  try { localStorage.setItem(CMP_HISTORY_KEY, JSON.stringify(h)); } catch (e) { /* 忽略 */ }
}

/* ---------- `@` 引用源（记忆条目 + 沙箱文件） ---------- */
async function refreshRefNodes() {
  try {
    const r = await api('/api/memory/get', { session_id: state.sessions.current });
    _refNodes = (r && r.ok && r.memory && r.memory.nodes) || [];
  } catch (e) { _refNodes = []; }
}

// 沙箱树 → 扁平相对路径列表（目录不出现，只列文件；最多 4 层，与后端 tree 的 max_depth 对齐）
function cmpFlattenTree(nodes, prefix, depth) {
  const out = [];
  (nodes || []).forEach((n) => {
    const p = prefix ? (prefix + '/' + n.name) : n.name;
    if (n.dir) {
      if (depth < 4 && n.children && n.children.length) {
        out.push.apply(out, cmpFlattenTree(n.children, p, depth + 1));
      }
    } else {
      out.push(p);
    }
  });
  return out;
}

async function refreshRefFiles(s, key) {
  if (!s || !s.sandbox) { _refFiles = []; return; }
  try {
    const r = await api('/api/sandbox/tree', { session_id: state.sessions.current });
    if (key !== _refFilesKey) return;           // 期间切了会话 / 换了目录，丢弃这次结果
    _refFiles = (r && r.ok && r.tree) ? cmpFlattenTree(r.tree.children, '', 0) : [];
  } catch (e) { _refFiles = []; }
}

function ensureRefSources() {
  const s = (typeof curSession === 'function') ? curSession() : null;
  const key = String(state.sessions.current || '') + '|' + String((s && s.sandbox) || '');
  if (key !== _refFilesKey) { _refFilesKey = key; refreshRefFiles(s, key); }
  if (!_refNodes.length) refreshRefNodes();
}

/* ---------- 发送前预检（非阻塞：只提示缺什么，不拦发送） ----------
   研究结论（Ask-or-Assume / UX 提示质量研究）：让用户越用越会写的关键不是教程，
   而是「在他自己的句子上指出缺哪一块」。所以这里只做两件事：
   ① 输入停顿时判断缺 目标 / 范围 / 边界 / 验收；② 给一个「按建议改写」入口（/spec）。 */
let _precheckTimer = null;
let _precheckGaps = [];
let _precheckFor = '';
let _precheckBusy = false;
let _precheckFirstAt = 0;         // 本轮连续输入的起点：用来算 maxWait
let _precheckLastSentAt = 0;      // 上次真正发请求的时间：两次请求的最小间隔
const _precheckSeen = new Map();  // 同一段文字不重复调模型
// 只对「会改东西」的输入做预检；闲聊、短句、/ 指令都不打扰
const _TASK_RE = /改|加|删|重构|优化|修|实现|调整|替换|做成|写一?个|新增|去掉|迁移|拆分/;
const PRECHECK_IDLE = 900;        // 停止输入这么久就检查
const PRECHECK_MAX_WAIT = 2200;   // 连续打字时最多憋这么久也要检查一次
const PRECHECK_MIN_GAP = 2000;    // 两次真实请求的最小间隔
const PRECHECK_SEEN_MAX = 30;

function cmpClearPrecheck() {
  _precheckGaps = [];
  _precheckFor = '';
  _precheckFirstAt = 0;
}

/* 建议是否还对得上当前文本：
   完全一致 → 用；检查完之后又接着打字（被检查的那段是当前文本前缀）→ 仍然用。
   以前只认完全一致，于是「预检跑完了但用户还在打字」= 结果被扔掉、永远看不到提示。 */
function cmpPrecheckMatches(cur) {
  if (!_precheckFor) return false;
  if (cur === _precheckFor) return true;
  return cur.length > _precheckFor.length && cur.indexOf(_precheckFor) === 0;
}

async function _precheckRun(text) {
  const cached = _precheckSeen.get(text);
  if (cached) {
    _precheckGaps = cached.slice(0, 3);
    _precheckFor = text;
    cmpUpdateHint();
    return;
  }
  _precheckBusy = true;
  _precheckLastSentAt = Date.now();
  cmpUpdateHint();
  try {
    const r = await api('/api/compose', { text: text, action: 'check' });
    if (!r || !r.ok) return;          // 失败静默：预检不该影响输入
    const gaps = (r.gaps || []).slice(0, 3);
    _precheckSeen.set(text, gaps);
    while (_precheckSeen.size > PRECHECK_SEEN_MAX) {
      _precheckSeen.delete(_precheckSeen.keys().next().value);
    }
    _precheckGaps = gaps;
    _precheckFor = text;
  } catch (e) {
    /* 静默：预检失败不该影响输入 */
  } finally {
    _precheckBusy = false;
    cmpUpdateHint();
  }
}

function cmpSchedulePrecheck() {
  clearTimeout(_precheckTimer);
  const inp = cmpInput();
  if (!inp) return;
  if (!cmpPrecheckWanted(inp.value.trim())) {
    if (_precheckGaps.length || _precheckBusy) { cmpClearPrecheck(); cmpUpdateHint(); }
    return;
  }
  const now = Date.now();
  if (!_precheckFirstAt) _precheckFirstAt = now;
  // 取最早的那个截止时间：① 停手 900ms 就想检查；② 连续打字也别永远等不到，最多憋 2.2s；
  // ③ 同时保证两次请求至少隔 2s，别一个字符一次调用。
  let due = Math.min(now + PRECHECK_IDLE, _precheckFirstAt + PRECHECK_MAX_WAIT);
  if (_precheckLastSentAt) due = Math.max(due, _precheckLastSentAt + PRECHECK_MIN_GAP);
  _precheckTimer = setTimeout(() => {
    _precheckFirstAt = Date.now();     // 下一轮重新计 maxWait
    const box = cmpInput();
    const cur = box ? box.value.trim() : '';
    if (!cmpPrecheckWanted(cur)) return;
    _precheckRun(cur);
  }, Math.max(0, due - now));
}

function cmpPrecheckWanted(text) {
  return !!text && text.length >= 12 && !text.startsWith('/') && _TASK_RE.test(text);
}

/* ---------- 实时反馈（字符数 / 状态提示） ---------- */
function cmpUpdateHint() {
  const el = $('#composer-hint');
  const inp = cmpInput();
  if (!el || !inp) return;
  const n = inp.value.length;
  const over = n > CMP_LONG_TEXT;
  const streaming = typeof state !== 'undefined' && !!state.streaming;
  // 正在修改历史消息：优先显示状态，避免用户以为在写一条新消息
  if (editTarget && editTarget.sid === String((state && state.sessions && state.sessions.current) || '')) {
    el.textContent = '正在修改第 ' + (editTarget.index + 1) + ' 条消息 · Enter 发送 · Esc 取消';
    el.classList.add('warn');
    galSyncDraft(inp.value);
    return;
  }
  // 预检建议优先显示：只在「还对得上当前内容」时有效，避免提示过期内容
  if (_precheckGaps.length && cmpPrecheckMatches(inp.value.trim())) {
    el.innerHTML = '建议补充：' + _precheckGaps.map(escapeHtml).join(' · ') +
      ' <button type="button" class="cmp-fix">按建议改写</button>';
    el.classList.add('warn');
    const b = el.querySelector('.cmp-fix');
    if (b) b.onclick = () => improveInput('spec');
    galSyncDraft(inp.value);
    return;
  }
  let txt = over
    ? ('内容较长（' + n + ' 字），建议精简或拆分')
    : (n ? (n + ' 字 · ' + (streaming ? 'Enter 排队' : 'Enter 发送') + ' · Shift+Enter 换行')
         : (streaming ? '正在生成… · 可继续输入，Enter 排队，本轮结束后自动发送'
                      : 'Enter 发送 · Shift+Enter 换行 · / 指令 · @ 引用'));
  // 有请求在路上时给个反馈：否则「点了没反应」和「没触发」用户分不出来
  if (_precheckBusy && n) txt = '预检中… · ' + txt;
  if (el.textContent !== txt) el.textContent = txt;
  el.classList.toggle('warn', over);
  // GAL：把正在打的字实时映射到对白区（「草稿变剧中台词」）。
  // 挂在这里是因为所有改动输入内容的路径（打字 / 清空 / 撤销 / AI 优化 / 历史补全 / 语音）最终都会走到它。
  galSyncDraft(inp.value);
}

function cmpUpdateClear() {
  const btn = $('#btn-clear-input');
  const inp = cmpInput();
  if (!btn || !inp) return;
  btn.classList.toggle('hidden', !inp.value);
}

/* ---------- 补全面板 ---------- */
function cmpClosePop() {
  const pop = $('#composer-pop');
  const inp = cmpInput();
  if (!pop || !_popOpen) return;
  _popOpen = false; _popItems = []; _popIndex = -1;
  pop.classList.add('hidden');
  pop.innerHTML = '';
  if (inp) inp.setAttribute('aria-expanded', 'false');
}

function cmpRenderPop() {
  const pop = $('#composer-pop');
  const inp = cmpInput();
  if (!pop) return;
  if (!_popItems.length) { cmpClosePop(); return; }
  pop.innerHTML = _popItems.map((it, i) => {
    if (it.kind === 'group') {
      return '<div class="cmp-group">' + escapeHtml(it.label) + '</div>';
    }
    return '<div class="cmp-item' + (i === _popIndex ? ' on' : '') + '" role="option" id="cmp-opt-' + i +
      '" aria-selected="' + (i === _popIndex) + '" data-i="' + i + '">' +
      '<span class="cmp-main">' + escapeHtml(it.label) + '</span>' +
      (it.hint ? '<span class="cmp-hint">' + escapeHtml(it.hint) + '</span>' : '') +
    '</div>';
  }).join('');
  pop.classList.remove('hidden');
  _popOpen = true;
  if (inp) inp.setAttribute('aria-expanded', 'true');
  pop.querySelectorAll('.cmp-item').forEach((el) => {
    el.onmousedown = (e) => { e.preventDefault(); cmpAccept(Number(el.dataset.i)); };
  });
}

// 给条目列表插入分组标题（同一 group 连续出现时只插一次）
function cmpWithGroups(items) {
  const out = [];
  let last = '';
  items.forEach((it) => {
    if (it.group && it.group !== last) {
      out.push({ kind: 'group', label: it.group });
      last = it.group;
    }
    out.push(it);
  });
  return out;
}

function cmpShow(items) {
  _popItems = items;
  _popIndex = -1;
  for (let i = 0; i < items.length; i++) {
    if (items[i].kind !== 'group') { _popIndex = i; break; }
  }
  cmpRenderPop();
}

// 上下键：跳过分组标题，只在可选条目之间移动
function cmpMove(dir) {
  const n = _popItems.length;
  for (let k = 0; k < n; k++) {
    const i = (_popIndex + dir + n) % n;
    _popIndex = i;
    if (_popItems[i] && _popItems[i].kind !== 'group') break;
  }
  cmpRenderPop();
}

function cmpAccept(i) {
  if (i < 0 || i >= _popItems.length) return;
  const it = _popItems[i];
  if (it.kind === 'group') return;
  cmpClosePop();
  if (it.kind === 'command') { cmpRunCommand(it.cmd, it.action); return; }
  if (it.kind === 'ref') { cmpInsertRef(it.insert || it.label); return; }
  const inp = cmpInput();                       // history
  if (inp) {
    inp.value = it.label;
    autoGrow(); cmpUpdateHint(); cmpUpdateClear();
    inp.focus();
  }
}

/* ---------- 触发检测 ---------- */
function cmpDetect() {
  const inp = cmpInput();
  if (!inp) return;
  const pos = inp.selectionStart || 0;
  const before = inp.value.slice(0, pos);
  const lineStart = before.lastIndexOf('\n') + 1;
  const line = before.slice(lineStart);

  // `/` 指令：行首；命令名或说明都能命中
  if (line.startsWith('/')) {
    const q = line.slice(1).toLowerCase();
    const items = CMP_COMMANDS
      .filter((c) => !q || c.cmd.slice(1).startsWith(q) || c.desc.toLowerCase().indexOf(q) >= 0)
      .map((c) => ({ kind: 'command', cmd: c.cmd, action: c.action, label: c.cmd, hint: c.desc, group: c.group }));
    cmpShow(cmpWithGroups(items));
    return;
  }

  // `@` 引用：光标前最近的 @，且其前为空白或行首；沙箱文件 + 记忆条目
  const at = line.lastIndexOf('@');
  if (at >= 0 && (at === 0 || /\s/.test(line[at - 1]))) {
    const q = line.slice(at + 1).toLowerCase();
    ensureRefSources();
    const files = _refFiles
      .filter((f) => !q || f.toLowerCase().indexOf(q) >= 0)
      .slice(0, CMP_POP_MAX)
      .map((f) => ({ kind: 'ref', label: f, insert: f, hint: '文件', group: '沙箱文件' }));
    const mems = _refNodes
      .filter((n) => !q || String(n.title || '').toLowerCase().indexOf(q) >= 0)
      .slice(0, CMP_POP_MAX)
      .map((n) => ({ kind: 'ref', label: String(n.title || ''), insert: String(n.title || ''),
                     hint: String(n.category || '记忆'), group: '记忆条目' }));
    cmpShow(cmpWithGroups(files.concat(mems).slice(0, CMP_POP_MAX)));
    return;
  }

  // 历史补全：整框非空、单行、前缀匹配
  const v = inp.value.trim();
  if (v.length >= 2 && inp.value.indexOf('\n') < 0) {
    const low = v.toLowerCase();
    const items = cmpHistory()
      .filter((h) => h !== v && h.toLowerCase().startsWith(low))
      .slice(0, CMP_POP_MAX)
      .map((h) => ({ kind: 'history', label: h, hint: '历史' }));
    if (items.length) { cmpShow(items); return; }
  }
  cmpClosePop();
}

/* ---------- `@` 引用插入 ---------- */
function cmpInsertRef(text) {
  const inp = cmpInput();
  if (!inp) return;
  const pos = inp.selectionStart || 0;
  const before = inp.value.slice(0, pos);
  const at = before.lastIndexOf('@');
  if (at < 0) return;
  const after = inp.value.slice(pos);
  inp.value = before.slice(0, at) + '@' + text + ' ' + after;
  const np = at + text.length + 2;
  try { inp.setSelectionRange(np, np); } catch (e) { /* 忽略 */ }
  autoGrow(); cmpUpdateHint(); cmpUpdateClear();
  inp.focus();
}

/* ---------- 指令执行 ---------- */
function cmpCommandArg(cmd) {
  const inp = cmpInput();
  if (!inp) return '';
  const v = inp.value.trim();
  return v.indexOf(cmd) === 0 ? v.slice(cmd.length).trim() : '';
}

function cmpStripCommand(cmd) {
  const inp = cmpInput();
  if (!inp) return;
  const v = inp.value;
  if (v.trim().indexOf(cmd) === 0) inp.value = v.trim().slice(cmd.length).trim();
}

function cmpRunCommand(cmd, action) {
  const inp = cmpInput();
  switch (cmd) {
    case '/clear':
      if (inp) { inp.value = ''; autoGrow(); cmpUpdateHint(); cmpUpdateClear(); inp.focus(); }
      break;
    case '/new': if (typeof newSession === 'function') newSession(); break;
    case '/compress': if (typeof compressContextManually === 'function') compressContextManually(); break;
    case '/kb': if (typeof openKbModal === 'function') openKbModal(); break;
    case '/settings': if (typeof openModal === 'function') openModal(); break;
    case '/theme': { const b = $('#btn-theme'); if (b) b.click(); break; }
    case '/translate': {
      const lang = cmpCommandArg('/translate');
      cmpStripCommand('/translate');
      improveInput('translate', lang || '英文');
      break;
    }
    case '/polish': case '/spec': case '/shorten': case '/expand':
      cmpStripCommand(cmd);
      improveInput(action || cmd.slice(1));
      break;
    default: break;
  }
}

/* ---------- L2：AI 优化输入 ---------- */
function cmpSetHint(html) {
  const el = $('#composer-hint');
  if (el) el.innerHTML = html;
}

function cmpShowUndo(before) {
  const el = $('#composer-hint');
  if (!el) return;
  el.innerHTML = '已优化 · <button type="button" class="cmp-undo">撤销</button>';
  const b = el.querySelector('.cmp-undo');
  if (b) {
    b.onclick = () => {
      const inp = cmpInput();
      if (inp) { inp.value = before; autoGrow(); cmpUpdateHint(); cmpUpdateClear(); inp.focus(); }
    };
  }
}

/* AI 优化按钮的三态反馈：busy（改中）/ done（改完）/ 空闲。
   done 只在按钮上闪 1.2s，之后自动摘掉；连续点两次时强制重启动画。 */
function cmpImproveFx(btn, state) {
  if (!btn) return;
  if (state === 'busy') {
    btn.classList.remove('done');
    void btn.offsetWidth;              // 强制回流：连续优化时动画能重新播
    btn.classList.add('busy');
  } else if (state === 'done') {
    btn.classList.remove('busy');
    btn.classList.add('done');
    clearTimeout(btn._doneTimer);
    btn._doneTimer = setTimeout(() => btn.classList.remove('done'), 1200);
  } else {
    btn.classList.remove('busy');
  }
}

async function improveInput(action, targetLang) {
  const inp = cmpInput();
  if (!inp) return;
  const text = inp.value.trim();
  if (!text) { toast('先输入一些内容，再用 AI 优化'); inp.focus(); return; }
  // 模型正在输出时也允许优化输入：/api/compose 是独立的轻量请求，
  // 不占用当前会话的流，也不会插进正在生成的那条回复里。
  const btn = $('#btn-improve');
  cmpImproveFx(btn, 'busy');
  const before = inp.value;
  cmpSetHint('AI 优化中…');
  try {
    const r = await api('/api/compose', {
      text: text, action: action || 'polish', target_lang: targetLang || '',
    });
    if (!r || !r.ok) { toast((r && (r.message || r.error)) || 'AI 优化失败', 'error'); cmpUpdateHint(); return; }
    const out = String(r.text || '').trim();
    if (!out) { toast('AI 没有返回内容', 'error'); cmpUpdateHint(); return; }
    inp.value = out;
    autoGrow(); cmpUpdateClear();
    cmpClearPrecheck();
    cmpShowUndo(before);
    cmpImproveFx(btn, 'done');
    if (r.intent) {
      const el = $('#composer-hint');
      if (el) el.innerHTML = '意图：' + escapeHtml(r.intent) + ' · 已优化 · ' +
        '<button type="button" class="cmp-undo">撤销</button>';
      const b = el.querySelector('.cmp-undo');
      if (b) b.onclick = () => { inp.value = before; autoGrow(); cmpUpdateHint(); cmpUpdateClear(); inp.focus(); };
    }
  } catch (e) {
    toast('AI 优化失败：' + e, 'error');
    cmpUpdateHint();
  } finally {
    cmpImproveFx(btn, '');          // 失败时也要退出 busy；done 由自己的 timer 收
    inp.focus();
  }
}

/* ---------- 键盘（capture：抢在「Enter 发送」之前处理） ---------- */
function cmpKeydown(e) {
  // 输入法组词期间不抢 Enter / Tab / 方向键，让候选框先处理
  if (isImeComposing(e)) return;
  if (_popOpen) {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault(); e.stopImmediatePropagation();
      cmpMove(e.key === 'ArrowDown' ? 1 : -1);
      return;
    }
    if (e.key === 'Tab' || (e.key === 'Enter' && !e.shiftKey)) {
      e.preventDefault(); e.stopImmediatePropagation();
      cmpAccept(_popIndex);
      return;
    }
    if (e.key === 'Escape') {
      e.preventDefault(); e.stopImmediatePropagation();
      cmpClosePop();
      return;
    }
  }
  // 正在修改历史消息时，Esc 先退出「修改」状态（再按一次才清空输入）
  if (e.key === 'Escape' && typeof editTarget !== 'undefined' && editTarget) {
    e.preventDefault(); e.stopImmediatePropagation();
    cancelEditTarget(false);
    return;
  }
  const hit = hkHit(e, ['composer']);
  if (!hit) return;
  const inp = cmpInput();
  if (hit.id === 'cmp.send') {                    // Ctrl/⌘+Enter：发送
    e.preventDefault(); e.stopImmediatePropagation();
    if (inp && inp.value.trim()) sendMessage();
    return;
  }
  if (hit.id === 'cmp.polish') {                  // Ctrl/⌘+I：AI 润色
    e.preventDefault(); e.stopImmediatePropagation();
    improveInput('polish');
    return;
  }
  if (hit.id === 'cmp.clear') {                   // Esc：有内容先清空
    if (inp && inp.value) {
      e.preventDefault(); e.stopImmediatePropagation();
      inp.value = ''; autoGrow(); cmpUpdateHint(); cmpUpdateClear();
    }
    return;
  }
  if (hit.id === 'cmp.history') {                 // 空框按 ↑：调出上一条历史
    if (inp && !inp.value.trim()) {
      const h = cmpHistory();
      if (h.length) {
        e.preventDefault(); e.stopImmediatePropagation();
        inp.value = h[0]; autoGrow(); cmpUpdateHint(); cmpUpdateClear();
      }
    }
  }
}

/* ---------- 粘贴优化 ---------- */
function cmpPaste(e) {
  const dt = e.clipboardData || window.clipboardData;
  if (!dt) return;
  const txt = dt.getData('text') || '';
  if (txt.length > CMP_LONG_TEXT) toast('粘贴内容较长（' + txt.length + ' 字），可考虑精简');
}

/* ---------- 初始化 ---------- */
function initComposer() {
  const inp = cmpInput();
  if (!inp) return;

  inp.addEventListener('input', () => { cmpUpdateHint(); cmpUpdateClear(); cmpDetect(); cmpSchedulePrecheck(); });
  inp.addEventListener('click', () => cmpDetect());
  inp.addEventListener('keydown', cmpKeydown, true);          // capture 阶段
  inp.addEventListener('paste', cmpPaste);
  inp.addEventListener('blur', () => setTimeout(cmpClosePop, 120));   // 稍延迟，让候选点击先生效

  const clr = $('#btn-clear-input');
  if (clr) clr.onclick = () => {
    inp.value = ''; cmpClosePop();
    autoGrow(); cmpUpdateHint(); cmpUpdateClear(); inp.focus();
  };

  const imp = $('#btn-improve');
  if (imp) imp.onclick = () => improveInput('polish');

  cmpUpdateHint();
  cmpUpdateClear();
  ensureRefSources();             // 记忆条目 + 沙箱文件（沙箱变了会在下次敲 @ 时刷新）
  try { inp.focus(); } catch (e) { /* 忽略 */ }
}

/* 记录发送过的内容，供「历史补全」使用（不改变原发送逻辑） */
(function cmpHookSend() {
  const orig = window.sendMessage;
  if (typeof orig !== 'function') return;
  window.sendMessage = function () {
    const inp = cmpInput();
    if (inp && inp.value.trim()) cmpRemember(inp.value.trim());
    return orig.apply(this, arguments);
  };
})();
