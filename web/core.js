/* ============================================================
   Galgame_monster —— 前端逻辑
   ============================================================ */
'use strict';

const TOKEN = window.__TOKEN__ || '';
const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

// 主题清单：dark=深色底（用于图表配色 / color-scheme / 应用标题栏）；title=标题栏底色
const THEME_META = {
  'light':         { dark: false, title: '#ffffff' },
  'dark':          { dark: true,  title: '#1a1b1e' },
  'high-contrast': { dark: true,  title: '#000000' },
  'midnight':      { dark: true,  title: '#161326' },
  'forest':        { dark: true,  title: '#0f1a16' },
  'sepia':         { dark: false, title: '#f4ecd8' },
};
// 当前主题是否深色底（图表 / 图谱等需要按明暗配色的地方统一用这个判断）
function isDarkTheme() {
  const t = document.documentElement.getAttribute('data-theme');
  return !!(THEME_META[t] && THEME_META[t].dark);
}

/* 中文输入法组词保护：组词时按 Enter 是「确认候选字」，不能触发发送 / 搜索 / 提交。
   浏览器差异：多数情况下 e.isComposing 为 true；部分 Windows 输入法在
   compositionend 之后才派发 keydown，因此额外用 keyCode===229 和短暂标记兜底。 */
let __imeComposing = false;
document.addEventListener('compositionstart', () => { __imeComposing = true; }, true);
document.addEventListener('compositionend', () => {
  setTimeout(() => { __imeComposing = false; }, 0);
}, true);
function isImeComposing(e) {
  return __imeComposing || !!(e && (e.isComposing || e.keyCode === 229));
}

const state = {
  config: null,
  profile: null,
  sessions: { current: null, sessions: [] },
  streaming: false,
  stopping: false,           // 已点「停止」，正在等后端收尾
  streams: {},               // sid -> { host, row, stopping }：每个会话一条独立的流
  streamingSids: {},         // sid -> true：后端仍在跑的会话（刷新 / 后台恢复用）
  unread: {},                // sid -> true：在后台生成完、还没切回去看的会话
  acc: '',
  pendingImage: null,        // 待发送图片：第一张（兼容旧逻辑）
  pendingImages: [],         // 待发送图片列表：{ mime, b64, url }
  pendingDoc: null,          // 待发送文档：{ name, mime, b64, size }
  pendingQuotes: [],         // 待发送引用：{ text }（只进本轮 prompt，不写入消息正文）
  pendingPresets: {},
  kbChanged: null,           // AI 本轮写入知识库/记忆的变更 id（供图谱高亮，关闭弹窗即清）
  kbFamily: '',              // 当前主题筛选（空 = 全部）
  uiMode: 'solo',            // solo | ide | gal | hall
  ideTree: null,
  ideOpen: '',               // 当前打开的文件（沙箱内相对路径）
  ideDir: '',                // 当前选中的目录（新建时的默认父目录）
  ideDirty: false,
  ideChanged: new Set(),     // AI 改动过、需要在文件树里高亮的文件
  // IDE 运行时字段：首次进入 IDE 前就需有默认值，否则点文件会因 undefined 抛错
  ideSide: 'explorer',
  ideSideHidden: false,      // 文件管理区（活动栏右侧）是否收起
  // 调试器：断点表 rel -> [line]；debugState 保存后端最近一次 stack/scopes
  ideBreakpoints: {},
  debugActive: false,
  debugPaused: false,
  debugState: null,
  debugTimer: null,
  debugDecorations: [],
  ideTabList: [],            // 已打开的标签（沙箱内相对路径）
  ideCache: {},              // rel -> { text, enc }
  ideDirtyMap: {},           // rel -> 是否有未保存改动
  ideModels: {},             // rel -> Monaco model
  ideEditor: null,           // Monaco editor 实例
  ideMonaco: null,           // Monaco 命名空间
  ideMonacoDefaults: null,   // 从 VS Code 导入的编辑器默认项
  // 变更审阅（Trae 式）：同一轮同一文件合并为一条，diff 取第一次写入前的快照
  ideDiffs: {},              // rel -> undo token（兼容旧的单文件 diff 入口）
  ideReview: [],             // 待审阅：[{ path, tokens, baseToken, name }]
  ideReviewPending: [],      // 本轮新增，回合结束后并入 ideReview
  ideReviewIdx: 0,           // 当前审阅到第几条
  ideReviewSurface: '',      // 'ide' | 'modal'
  ideDiffEditor: null,       // IDE 内嵌 diff 编辑器
  ideDiffModels: null,
  ideReviewEditor: null,     // Solo 弹窗 diff 编辑器
  ideReviewModels: null,
  ideSettledTokens: {},      // token -> 'accepted' | 'undone'
  reviewCardBtns: {},        // token -> [聊天卡片上的按钮]
  changeCards: [],           // 聊天流里的「变更记录」：{ row, items, el }
  lastPlan: [],              // 最近一次 update_plan 的步骤（提示栏展开任务项用）
  ideDiffDoc: null,          // IDE 内嵌审阅的合成文档（行号 / 折叠条定位）
  ideReviewDoc: null,        // Solo 弹窗里的同一份
  idePaletteMode: 'file',
  idePaletteSel: 0,
  idePaletteItems: [],
  msgCache: {},              // sid -> { messages, msg_loaded_start }：切回会话先秒出内容
};

/* 服务端只回最近 MSG_WINDOW 条消息（带 msg_total / msg_start）。
   前端把数组补齐成「全局下标」：老消息位置放 null，按下标访问的撤回 / 编辑 /
   版本预览等逻辑不用改；已展开过的旧消息优先保留，避免每次刷新回到 80 条。 */
function applySessionsPayload(payload) {
  if (!payload || !Array.isArray(payload.sessions)) return payload;
  if (!state.msgCache) state.msgCache = {};
  // 覆盖前先把「当前会话已加载的那份」收进缓存，否则流式 done / 轮询会把
  // 用户展开过的旧消息页丢掉（服务端每次只回最近 80 条）。
  const prevCur = (state.sessions && state.sessions.current) || '';
  if (prevCur && !state.msgCache[prevCur]) {
    const old = ((state.sessions && state.sessions.sessions) || []).find((x) => x.id === prevCur);
    if (old && Array.isArray(old.messages)) {
      state.msgCache[prevCur] = {
        messages: old.messages, msg_loaded_start: Number(old.msg_loaded_start) || 0,
      };
    }
  }
  payload.sessions.forEach((s) => {
    const incoming = Array.isArray(s.messages) ? s.messages : null;
    const total = Number(s.msg_total);
    if (!incoming) return;
    if (!Number.isFinite(total)) {
      s.msg_loaded_start = 0;
      return;
    }
    const newStart = Math.max(0, total - incoming.length);
    const cached = state.msgCache ? state.msgCache[s.id] : null;
    if (total === 0 && cached) delete state.msgCache[s.id];
    if (cached && Array.isArray(cached.messages)
        && cached.messages.length <= total
        && cached.messages.length >= incoming.length && newStart <= cached.messages.length) {
      // 已展开过更早的消息：保留前面的页，只覆盖（并补齐）尾部窗口
      cached.messages.length = total;
      for (let i = 0; i < incoming.length; i++) cached.messages[newStart + i] = incoming[i];
      s.messages = cached.messages;
      s.msg_loaded_start = Math.min(Number(cached.msg_loaded_start) || 0, newStart);
    } else if (newStart > 0) {
      s.messages = new Array(newStart).fill(null).concat(incoming);
      s.msg_loaded_start = newStart;
    } else {
      s.messages = incoming;
      s.msg_loaded_start = 0;
    }
  });
  // 会话被删除后，缓存也跟着清掉，避免下次切回已经不存在的数据
  const alive = new Set(payload.sessions.map((s) => s.id));
  Object.keys(state.msgCache).forEach((k) => { if (!alive.has(k)) delete state.msgCache[k]; });
  state.sessions = payload;
  return payload;
}

/* 切走之前把当前会话已加载的消息留在内存里（最多 3 个），切回来先渲染。 */
function cacheLooseMessages() {
  const list = (state.sessions && state.sessions.sessions) || [];
  const s = list.find((x) => x.id === state.sessions.current);
  if (!s || !Array.isArray(s.messages)) return;
  if (!state.msgCache) state.msgCache = {};
  state.msgCache[s.id] = { messages: s.messages, msg_loaded_start: s.msg_loaded_start || 0 };
  const keys = Object.keys(state.msgCache);
  while (keys.length > 3) delete state.msgCache[keys.shift()];
}

/* ---------------- HTTP ---------------- */
/* 把 HTTP 失败翻译成「能看懂 + 知道去哪儿检查」的文案。
   全站的失败提示都从 api() 出去，所以引导性文案在这里统一加，而不是让每个调用点各自拼。 */
async function api(path, body) {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Auth-Token': TOKEN },
    body: JSON.stringify(body || {}),
  });
  if (res.ok) return res.json();
  let msg = '';
  try {
    const j = await res.json();                     // 服务端通常带 error / message
    if (j && (j.message || j.error)) msg = String(j.message || j.error);
  } catch (e) { /* 响应不是 JSON（如 500 页面）→ 走下面的状态码推断 */ }
  if (!msg) {
    const s = res.status;
    if (s === 401 || s === 403) msg = '鉴权失败：密钥无效或未授权（去「设置 → 接入与密钥」检查）';
    else if (s === 404) msg = '接口不存在：服务端版本与前端不匹配';
    else if (s === 409) msg = '操作冲突：同一会话可能已有任务在执行';
    else if (s === 413) msg = '内容过大：请精简后重试';
    else if (s >= 500) msg = '服务端出错：可稍后重试，或查看控制台日志';
    else msg = '请求失败';
    msg += '（HTTP ' + s + '）';
  }
  throw new Error(msg);
}

function toast(msg, type) {
  const t = $('#toast');
  t.textContent = msg;
  t.classList.remove('ok', 'err');
  if (type === 'success' || type === 'ok') t.classList.add('ok');
  else if (type === 'error' || type === 'err') t.classList.add('err');
  t.classList.remove('hidden');
  void t.offsetWidth;              // 强制重排，保证从隐藏态开始播入场过渡
  t.classList.add('show');
  clearTimeout(t._timer);
  clearTimeout(t._hideTimer);
  t._timer = setTimeout(() => {
    t.classList.remove('show');
    t._hideTimer = setTimeout(() => t.classList.add('hidden'), 200);
  }, 1800);
}

// 「XX 失败」统一提示（等价于 toast('XX失败：' + e, 'error')）
function toastErr(what, e) {
  toast(what + '失败：' + e, 'error');
}

/* 让 <div>/<span> 这类非交互元素也能用键盘操作（Tab 可达 + Enter/Space 触发）。
   项目里不少列表项 / 折叠头是 div + onclick，键盘用户原本完全够不着。 */
function makeClickable(el, fn, label) {
  if (!el) return el;
  el.tabIndex = 0;
  el.setAttribute('role', 'button');
  if (label) el.setAttribute('aria-label', label);
  el.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); e.stopPropagation(); fn(); }
  });
  return el;
}

/* 让浮层支持「按住标题栏拖动」：位移用 transform: translate 表达，不破坏原有的居中布局。
   用 pointer capture，指针移出窗口也能继续拖；松手保留位置，下次打开时由 showWithAnim 复位。 */
function makeDraggable(winEl, handleEl) {
  if (!winEl || !handleEl) return;
  let sx = 0, sy = 0, ox = 0, oy = 0, dragging = false;
  const parse = (s) => {
    const m = /translate\((-?[\d.]+)px,\s*(-?[\d.]+)px\)/.exec(s || '');
    return m ? [parseFloat(m[1]), parseFloat(m[2])] : [0, 0];
  };
  handleEl.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;
    if (e.target.closest('button, a, input, select, textarea, .tab')) return;   // 标题栏里的控件不触发拖动
    const p = parse(winEl.style.transform);
    ox = p[0]; oy = p[1];
    sx = e.clientX; sy = e.clientY;
    dragging = true;
    winEl.style.animation = 'none';       // 关掉入场动画，避免与拖动的 transform 打架
    winEl.classList.add('dragging');
    try { if (handleEl.setPointerCapture) handleEl.setPointerCapture(e.pointerId); } catch (err) { /* 忽略 */ }
    e.preventDefault();
  });
  handleEl.addEventListener('pointermove', (e) => {
    if (!dragging) return;
    let dx = ox + (e.clientX - sx), dy = oy + (e.clientY - sy);
    // 至少留 80px 在视口内，避免把窗口拖没
    const w = winEl.offsetWidth, h = winEl.offsetHeight;
    const maxX = Math.max(0, (window.innerWidth + w) / 2 - 80);
    const maxY = Math.max(0, (window.innerHeight + h) / 2 - 80);
    dx = Math.min(maxX, Math.max(-maxX, dx));
    dy = Math.min(maxY, Math.max(-maxY, dy));
    winEl.style.transform = 'translate(' + dx + 'px, ' + dy + 'px)';
  });
  const end = (e) => {
    if (!dragging) return;
    dragging = false;
    winEl.classList.remove('dragging');
    if (handleEl.releasePointerCapture && e.pointerId != null) {
      try { handleEl.releasePointerCapture(e.pointerId); } catch (err) { /* 忽略 */ }
    }
  };
  handleEl.addEventListener('pointerup', end);
  handleEl.addEventListener('pointercancel', end);
}

// 触发浏览器下载一个 Blob URL，并延迟回收
function saveBlobUrl(url, filename) {
  const a = document.createElement('a');
  a.href = url;
  a.download = filename || '';
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 4000);
}

// SVG 图标（图标库见 index.html 顶部的 #icon-sprite）；返回可直接塞进 innerHTML 的字符串
function icon(name, cls) {
  return '<svg class="ic' + (cls ? ' ' + cls : '') + '"><use href="#i-' + name + '"/></svg>';
}

/* ============================================================
   应用内输入框
   Electron 不支持 window.prompt()（调用会直接抛错），所有需要
   用户输入文本的地方统一走这里。返回 Promise<string|null>：
   点「确定」得到原始输入，取消 / 关闭 / Esc / 点遮罩得到 null。
   ============================================================ */
let __promptResolve = null;

function appPrompt(message, defaultValue) {
  const modal = $('#input-modal');
  const input = $('#input-dialog');
  const label = $('#input-label');
  if (!modal || !input) {              // 界面元素缺失时退回原生 prompt（普通浏览器可用）
    try { return Promise.resolve(window.prompt(message, defaultValue || '')); }
    catch (e) { return Promise.resolve(null); }
  }
  const show = (typeof showWithAnim === 'function')
    ? showWithAnim : (el) => el.classList.remove('hidden');
  const hide = (typeof hideWithAnim === 'function')
    ? hideWithAnim : (el) => el.classList.add('hidden');
  // 同时只保留一个输入框：上一次没关就按取消处理
  if (__promptResolve) { const prev = __promptResolve; __promptResolve = null; prev(null); }
  if (label) label.textContent = message || '请输入';
  input.value = defaultValue || '';
  return new Promise((resolve) => {
    __promptResolve = (val) => {
      __promptResolve = null;
      resolve(val);
      hide(modal);
    };
    const ok = () => { if (__promptResolve) __promptResolve(input.value); };
    const cancel = () => { if (__promptResolve) __promptResolve(null); };
    const okBtn = $('#input-ok');
    const cancelBtn = $('#input-cancel');
    const closeBtn = $('#input-cancel-x');
    if (okBtn) okBtn.onclick = ok;
    if (cancelBtn) cancelBtn.onclick = cancel;
    if (closeBtn) closeBtn.onclick = cancel;
    input.onkeydown = (e) => {
      if (isImeComposing(e)) return;
      if (e.key === 'Enter') { e.preventDefault(); ok(); }
      else if (e.key === 'Escape') { e.preventDefault(); cancel(); }
    };
    modal.onclick = (e) => { if (e.target === modal) cancel(); };
    show(modal);
    setTimeout(() => { try { input.focus(); input.select(); } catch (e) { /* 忽略 */ } }, 30);
  });
}
