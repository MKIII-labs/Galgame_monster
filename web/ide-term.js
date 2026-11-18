'use strict';

/* ============================================================
   IDE 内置终端：后端 ConPTY（pywinpty）+ 前端 xterm.js

   对齐 VS Code 的终端组：
   - 一个会话可以有多个终端，页签在 xterm 上方（26px），
     激活态是页签左侧 1px 竖条（terminal-tab-activeBorder）；
   - 新建 / 中断 / 重启 / 清屏在页签栏右侧；
   - 字号 14、回滚 1000（terminal.integrated.fontSize / scrollback 的默认值）；
   - 关闭面板只是隐藏，进程保留；关闭页签才回收进程。
   ============================================================ */

const IDE_TERM_H_KEY = 'ide_term_height_v1';
const IDE_TERM_DEFAULT_H = 240;
const IDE_TERM_FONT = 14;
const IDE_TERM_SCROLLBACK = 1000;

let ideTerms = [];             // 所有终端实例：{ k, term, fit, host, tid, ... }
let ideTermState = null;       // 当前激活实例（gal-ide.js 会直接读它）
let ideTermInited = false;
let ideTermSeq = 0;

function ideTermHost() { return $('#ide-term-host'); }
function ideTermTabsBox() { return $('#ide-term-tabs'); }

function ideTermHint(msg) {
  const el = $('#ide-term-hint');
  if (!el) return;
  if (!msg) { el.textContent = ''; el.classList.add('hidden'); return; }
  el.textContent = msg;
  el.classList.remove('hidden');
}

/* 终端最近输出快照（供对话 #terminal 引用）；没有任何内容时返回空串。
   只读 xterm 缓冲区，不触发后端；最多取最后 160 行，再按字符数截断。 */
function ideTermRecentText(maxChars) {
  const st = ideTermState;
  if (!st || !st.term) return '';
  let buf = null;
  try { buf = st.term.buffer.active; } catch (e) { return ''; }
  if (!buf) return '';
  const end = Math.min(buf.length - 1, buf.baseY + buf.cursorY);
  const start = Math.max(0, end - 159);
  const lines = [];
  for (let i = start; i <= end; i++) {
    const line = buf.getLine(i);
    if (!line) continue;
    const text = line.translateToString(true);
    if (text.trim()) lines.push(text);
  }
  const out = lines.join('\n');
  const cap = Math.max(200, Number(maxChars) || 8000);
  return out.length > cap ? out.slice(out.length - cap) : out;
}

function ideTermColors() {
  const panel = $('#ide-panel');
  const c = panel ? getComputedStyle(panel) : getComputedStyle(document.documentElement);
  const pick = (name, fallback) => (c.getPropertyValue(name) || '').trim() || fallback;
  return {
    // 终端底色跟编辑器一致（VS Code 的 terminal.background 默认就是编辑器底色）
    background: pick('--ide-editor-bg', '#141517'),
    foreground: pick('--text', '#d4d4d4'),
    cursor: pick('--accent', '#4d6bfe'),
    cursorAccent: pick('--ide-editor-bg', '#141517'),
    selectionBackground: 'rgba(77, 107, 254, .35)',
    black: '#0b0b0c', red: '#e06c75', green: '#98c379', yellow: '#e5c07b',
    blue: '#61afef', magenta: '#c678dd', cyan: '#56b6c2', white: '#dcdfe4',
    brightBlack: '#5c6370', brightRed: '#ef7c86', brightGreen: '#a9d68a',
    brightYellow: '#f0cd92', brightBlue: '#7cc0f7', brightMagenta: '#d79ae6',
    brightCyan: '#72c7d2', brightWhite: '#f4f6f8',
  };
}

function ideTermIsLive(st) { return !!st && st.term && ideTerms.indexOf(st) >= 0; }

/* ---------------- 实例创建 / 激活 / 页签 ---------------- */

function ideTermCreate() {
  const host = ideTermHost();
  if (!host || typeof window.Terminal !== 'function') return null;
  // 先不隐藏：xterm.open() 在 display:none 的容器里量不到字符尺寸，
  // 之后 fit() 会算出 NaN。创建后紧接着 setActive 会把其它 pane 收起来。
  const pane = document.createElement('div');
  pane.className = 'ide-term-pane';
  host.appendChild(pane);
  const term = new window.Terminal({
    fontSize: IDE_TERM_FONT,
    fontFamily: '"Cascadia Mono", "Consolas", "Courier New", monospace',
    cursorBlink: true,
    scrollback: IDE_TERM_SCROLLBACK,
    theme: ideTermColors(),
  });
  term.open(pane);
  let fit = null;
  if (window.FitAddon && window.FitAddon.FitAddon) {
    fit = new window.FitAddon.FitAddon();
    term.loadAddon(fit);
  }
  const st = {
    k: 't' + (++ideTermSeq), name: 'PowerShell',
    term, fit, host: pane,
    tid: '', off: 0, alive: false, sid: '', ctrl: null,
    started: false, opening: false, fresh: false,
    inBuf: '', inTimer: 0, resizeTimer: 0, cwd: '',
  };
  term.onData((d) => ideTermSendTo(st, d));
  term.onResize(({ cols, rows }) => {
    if (!st.tid) return;
    clearTimeout(st.resizeTimer);
    st.resizeTimer = setTimeout(() => {
      if (st.tid) api('/api/ide/term/resize', { tid: st.tid, cols, rows }).catch(() => {});
    }, 180);
  });
  pane.addEventListener('mousedown', () => {
    if (ideTermState !== st) ideTermSetActive(st, true);
    else term.focus();
  });
  ideTerms.push(st);
  return st;
}

function ideTermFit(st) {
  if (!st || !st.fit) return;
  try { st.fit.fit(); } catch (e) { /* 面板隐藏时尺寸为 0，忽略 */ }
}

function ideTermSetActive(st, focus) {
  if (!ideTermIsLive(st)) return;
  ideTermState = st;
  ideTerms.forEach((x) => {
    if (x.host) x.host.classList.toggle('hidden', x !== st);
  });
  ideTermRenderTabs();
  ideTermSyncActions();
  requestAnimationFrame(() => {
    ideTermFit(st);
    if (focus !== false) { try { st.term.focus(); } catch (e) { /* 忽略 */ } }
  });
}

function ideTermRenderTabs() {
  const box = ideTermTabsBox();
  if (!box) return;
  box.innerHTML = '';
  let activeEl = null;
  ideTerms.forEach((st) => {
    const tab = document.createElement('div');
    tab.className = 'ide-term-tab' + (st === ideTermState ? ' active' : '');
    tab.dataset.k = st.k;
    tab.setAttribute('role', 'tab');
    tab.setAttribute('aria-selected', st === ideTermState ? 'true' : 'false');
    tab.title = st.cwd || st.name;
    const ic = document.createElement('span');
    ic.className = 'ide-term-tab-ic';
    ic.innerHTML = '<svg class="ic"><use href="#i-terminal"/></svg>';
    const nm = document.createElement('span');
    nm.className = 'ide-term-tab-name';
    nm.textContent = st.name;
    const x = document.createElement('span');
    x.className = 'ide-term-tab-x';
    x.textContent = '✕';
    x.title = '关闭终端';
    x.onclick = (e) => { e.stopPropagation(); ideTermClose(st); };
    tab.appendChild(ic); tab.appendChild(nm); tab.appendChild(x);
    tab.onclick = () => { ideTermSetActive(st, true); ideTermShow(true); };
    tab.ondblclick = (e) => { e.preventDefault(); e.stopPropagation(); ideTermRename(st, tab); };
    tab.addEventListener('auxclick', (e) => {
      if (e.button === 1) { e.preventDefault(); ideTermClose(st); }
    });
    tab.addEventListener('contextmenu', (e) => {
      e.preventDefault();
      ideTermMenu(st, e.clientX, e.clientY);
    });
    box.appendChild(tab);
    if (st === ideTermState) activeEl = tab;
  });
  if (activeEl) {
    try { activeEl.scrollIntoView({ block: 'nearest', inline: 'nearest' }); } catch (e) { /* 忽略 */ }
  }
}

/* 双击页签重命名（VS Code 的 terminal rename） */
function ideTermRename(st, tab) {
  const nm = tab && tab.querySelector('.ide-term-tab-name');
  if (!nm || tab.querySelector('input')) return;
  const input = document.createElement('input');
  input.className = 'ide-term-tab-input';
  input.value = st.name;
  nm.replaceWith(input);
  input.focus();
  input.select();
  input.onclick = (e) => e.stopPropagation();
  let done = false;
  const commit = (ok) => {
    if (done) return;
    done = true;
    const v = input.value.trim();
    if (ok && v) st.name = v;
    ideTermRenderTabs();
    ideTermSyncActions();
  };
  input.onblur = () => commit(true);
  input.onkeydown = (e) => {
    if (e.key === 'Enter') { e.preventDefault(); commit(true); }
    else if (e.key === 'Escape') { e.preventDefault(); commit(false); }
  };
}

function ideTermMenu(st, x, y) {
  const el = document.getElementById('ide-ctxmenu');
  if (!el || typeof ideMenuShow !== 'function') return;
  ideMenuShow(el, x, y, [
    { label: '重命名', run: () => {
      const tab = ideTermTabsBox() && ideTermTabsBox().querySelector('[data-k="' + st.k + '"]');
      if (tab) ideTermRename(st, tab);
    } },
    { label: '清屏', run: () => ideTermClear(st) },
    { label: '重启终端', run: () => ideTermRestart(st) },
    { sep: true },
    { label: '关闭终端', run: () => ideTermClose(st) },
  ]);
}

function ideTermSyncActions() {
  const st = ideTermState;
  const intr = $('#ide-term-interrupt');
  const restart = $('#ide-term-restart');
  const clear = $('#ide-term-clear');
  if (intr) intr.disabled = !st || !st.alive;
  if (restart) restart.disabled = !st;
  if (clear) clear.disabled = !st;
}

/* ---------------- 显示 / 隐藏 / 新建 / 关闭 ---------------- */

function ideTermShow(open) {
  const el = $('#ide-term');
  if (!el) return;
  // 底部面板同一时间只显示一个页签：打开终端时收起问题 / 大纲
  if (open && typeof ideIntelToggle === 'function') {
    const intel = $('#ide-intel');
    if (intel && !intel.classList.contains('hidden')) ideIntelToggle(null, false);
  }
  el.classList.toggle('hidden', !open);
  const rz = $('#ide-term-resizer');
  if (rz) rz.classList.toggle('hidden', !open);
  const act = $('#ide-term-act');
  if (act) act.classList.toggle('active', open);
  if (open) ideTermApplyHeight();
  else if (ideTermState) { try { ideTermState.term.blur(); } catch (e) { /* 忽略 */ } }
}

function ideTermToggle(force) {
  const el = $('#ide-term');
  if (!el) return;
  const open = typeof force === 'boolean' ? force : el.classList.contains('hidden');
  ideTermShow(open);
  if (!open) return;
  // xterm 内核按需加载：首次打开终端时若还没就绪，加载完再启动
  if (typeof window.Terminal !== 'function' && typeof window.gmLoadGroup === 'function') {
    window.gmLoadGroup('xterm').then(() => {
      ideTermEnsure();
      setTimeout(() => {
        if (ideTermState) { ideTermFit(ideTermState); ideTermState.term.focus(); }
      }, 40);
    }).catch((e) => ideTermHint('xterm.js 加载失败：' + ((e && e.message) || e)));
    return;
  }
  ideTermEnsure();
  setTimeout(() => {
    if (ideTermState) { ideTermFit(ideTermState); ideTermState.term.focus(); }
  }, 40);
}

function ideTermNew() {
  const doNew = () => {
    ideTermShow(true);                 // 先显示面板，xterm 才能量到正确尺寸
    const st = ideTermCreate();
    if (!st) { ideTermHint('xterm.js 未加载，无法启动终端'); return; }
    st.fresh = true;
    ideTermSetActive(st, true);
    ideTermStartInst(st, false);
  };
  if (typeof window.Terminal !== 'function' && typeof window.gmLoadGroup === 'function') {
    window.gmLoadGroup('xterm').then(doNew)
      .catch((e) => ideTermHint('xterm.js 加载失败：' + ((e && e.message) || e)));
    return;
  }
  doNew();
}

function ideTermClose(st) {
  st = st || ideTermState;
  if (!st || !ideTermIsLive(st)) return;
  if (st.ctrl) { try { st.ctrl.abort(); } catch (e) { /* 忽略 */ } st.ctrl = null; }
  if (st.tid) api('/api/ide/term/kill', { tid: st.tid }).catch(() => {});
  const i = ideTerms.indexOf(st);
  try { st.term.dispose(); } catch (e) { /* 忽略 */ }
  if (st.host && st.host.parentElement) st.host.parentElement.removeChild(st.host);
  if (i >= 0) ideTerms.splice(i, 1);
  if (ideTermState === st) {
    ideTermState = null;
    const next = ideTerms[Math.max(0, i - 1)] || ideTerms[0] || null;
    if (next) ideTermSetActive(next, true);
    else { ideTermShow(false); ideTermHint(''); }
  }
  ideTermRenderTabs();
  ideTermSyncActions();
}

/* ---------------- 启动 / 输入 / 输出 ---------------- */

function ideTermEnsure() {
  const s = (typeof curSession === 'function') ? curSession() : null;
  let st = ideTermState;
  if (!st) {
    st = ideTermCreate();
    if (!st) return;
    ideTermSetActive(st, false);
  }
  if (!st.tid || !s || st.sid !== s.id) { ideTermStartInst(st, false); return; }
  ideTermFit(st);
  if (st.alive && !st.ctrl) ideTermStream(st);
}

async function ideTermStartInst(st, force) {
  if (!st || st.opening) return;
  const s = (typeof curSession === 'function') ? curSession() : null;
  if (!s) { ideTermHint('没有当前会话'); return; }
  st.opening = true;
  try {
    ideTermHint('');
    ideTermFit(st);
    const cols = st.term.cols || 120;
    const rows = st.term.rows || 30;
    const body = { session_id: s.id, cols, rows };
    if (force && st.tid) body.tid = st.tid;     // 重启这个实例：后端先关旧进程
    else if (st.fresh) body.new = true;         // 再开一个：不动同会话的其它终端
    const r = await api('/api/ide/term/start', body);
    st.tid = r.tid;
    st.off = r.off || 0;
    st.alive = !!r.alive;
    st.sid = s.id;
    st.started = true;
    st.fresh = false;
    st.cwd = r.cwd || '';
    if (r.text) {
      if (r.reset) st.term.reset();
      st.term.write(r.text);
    }
    if (r.alive) ideTermStream(st);
    else ideTermHint('进程已退出，按回车重启');
    ideTermRenderTabs();
    ideTermSyncActions();
  } catch (e) {
    ideTermHint('终端启动失败：' + (e && e.message ? e.message : e));
  } finally {
    st.opening = false;
  }
}

function ideTermRestart(st) {
  st = st || ideTermState;
  if (!st) return;
  if (st.ctrl) { try { st.ctrl.abort(); } catch (e) { /* 忽略 */ } st.ctrl = null; }
  ideTermStartInst(st, true);
}

function ideTermClear(st) {
  st = st || ideTermState;
  if (!st || !st.tid) return;
  api('/api/ide/term/clear', { tid: st.tid }).catch(() => {});
  try { st.term.clear(); } catch (e) { /* 忽略 */ }
}

function ideTermInterrupt(st) {
  st = st || ideTermState;
  if (!st || !st.tid) return;
  api('/api/ide/term/interrupt', { tid: st.tid }).catch(() => {});
}

function ideTermSendTo(st, data) {
  if (!st) return;
  if (!st.alive) {
    // 进程已退出：回车重启（与 VS Code 里「按任意键」的意图一致）
    if (data.indexOf('\r') >= 0 || data.indexOf('\n') >= 0) ideTermRestart(st);
    return;
  }
  st.inBuf += data;
  if (st.inTimer) return;
  st.inTimer = setTimeout(() => {
    st.inTimer = 0;
    const chunk = st.inBuf;
    st.inBuf = '';
    if (chunk) api('/api/ide/term/input', { tid: st.tid, data: chunk }).catch(() => {});
  }, 25);
}

// 兼容旧调用：往当前激活的终端发输入
function ideTermSend(data) { ideTermSendTo(ideTermState, data); }

function ideTermParsePoint(st, obj) {
  if (!st) return;
  if (obj && obj.text) {
    if (obj.reset) st.term.reset();
    st.term.write(obj.text);
  }
  if (obj && typeof obj.off === 'number') st.off = obj.off;
  if (obj && obj.cwd && obj.cwd !== st.cwd) {
    st.cwd = obj.cwd;
    const box = ideTermTabsBox();
    const tab = box && box.querySelector('[data-k="' + st.k + '"]');
    if (tab) tab.title = obj.cwd;
  }
  if (obj && obj.type === 'exit') {
    st.alive = false;
    ideTermHint('进程已退出，按回车重启');
    ideTermSyncActions();
  } else if (obj && obj.type === 'init') {
    st.alive = !!obj.alive;
    if (!st.alive) ideTermHint('进程已退出，按回车重启');
    ideTermSyncActions();
  }
}

function ideTermStream(st) {
  if (!st || !st.tid) return;
  if (st.ctrl) { try { st.ctrl.abort(); } catch (e) { /* 忽略 */ } }
  const ctrl = new AbortController();
  st.ctrl = ctrl;
  const tid = st.tid;
  const off = st.off || 0;
  const url = '/api/ide/term/stream?tid=' + encodeURIComponent(tid) + '&off=' + off;
  fetch(url, { headers: { 'X-Auth-Token': TOKEN }, signal: ctrl.signal })
    .then(async (res) => {
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const reader = res.body.getReader();
      const dec = new TextDecoder('utf-8');
      let buf = '';
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf('\n\n')) >= 0) {
          const frame = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          const line = frame.split('\n').find((l) => l.indexOf('data: ') === 0);
          if (!line) continue;
          let obj = null;
          try { obj = JSON.parse(line.slice(6)); } catch (e) { continue; }
          if (!ideTermIsLive(st) || st.tid !== tid) return;   // 已关闭 / 已重启
          ideTermParsePoint(st, obj);
        }
      }
    })
    .catch((e) => {
      if (e && e.name === 'AbortError') return;
      if (ideTermIsLive(st) && st.tid === tid) {
        ideTermHint('连接中断：' + (e && e.message ? e.message : e) + '（重新打开面板可恢复）');
      }
    })
    .finally(() => {
      if (st.ctrl === ctrl) st.ctrl = null;
    });
}

/* ---------------- 高度 / 会话切换 / 初始化 ---------------- */

function ideTermApplyHeight() {
  const el = $('#ide-term');
  if (!el) return;
  let h = IDE_TERM_DEFAULT_H;
  try {
    const v = parseInt(localStorage.getItem(IDE_TERM_H_KEY) || '', 10);
    if (v > 0) h = v;
  } catch (e) { /* 忽略 */ }
  el.style.height = h + 'px';
}

function ideTermBindResizer() {
  const rz = $('#ide-term-resizer');
  const el = $('#ide-term');
  if (!rz || !el) return;
  rz.addEventListener('dblclick', () => {
    el.style.height = IDE_TERM_DEFAULT_H + 'px';
    try { localStorage.removeItem(IDE_TERM_H_KEY); } catch (e) { /* 忽略 */ }
    if (ideTermState) ideTermFit(ideTermState);
  });
  rz.addEventListener('mousedown', (e) => {
    e.preventDefault();
    const startY = e.clientY;
    const startH = el.getBoundingClientRect().height;
    const main = el.closest('.ide-main') || el.parentElement;
    const maxH = main ? Math.max(120, main.getBoundingClientRect().height * 0.75) : 600;
    const move = (ev) => {
      const h = Math.max(90, Math.min(maxH, startH - (ev.clientY - startY)));
      el.style.height = h + 'px';
      if (ideTermState) ideTermFit(ideTermState);
    };
    const up = () => {
      document.removeEventListener('mousemove', move);
      document.removeEventListener('mouseup', up);
      try {
        localStorage.setItem(IDE_TERM_H_KEY, String(Math.round(el.getBoundingClientRect().height)));
      } catch (err) { /* 忽略 */ }
    };
    document.addEventListener('mousemove', move);
    document.addEventListener('mouseup', up);
  });
}

/* 切换会话：旧终端属于上一个沙箱，全部回收；面板开着就为新会话重新拉起一个 */
function ideTermResetForSession() {
  ideTerms.slice().forEach((st) => {
    if (st.ctrl) { try { st.ctrl.abort(); } catch (e) { /* 忽略 */ } st.ctrl = null; }
    if (st.tid) api('/api/ide/term/kill', { tid: st.tid }).catch(() => {});
    try { st.term.dispose(); } catch (e) { /* 忽略 */ }
    if (st.host && st.host.parentElement) st.host.parentElement.removeChild(st.host);
  });
  ideTerms = [];
  ideTermState = null;
  ideTermHint('');
  ideTermRenderTabs();
  ideTermSyncActions();
  const el = $('#ide-term');
  if (el && !el.classList.contains('hidden')) {
    const st = ideTermCreate();
    if (st) { ideTermSetActive(st, false); ideTermStartInst(st, false); }
  }
}

function ideTermInitOnce() {
  if (ideTermInited) return;
  ideTermInited = true;
  const act = $('#ide-term-act');
  if (act) act.onclick = () => ideTermToggle();
  const nw = $('#ide-term-new');
  if (nw) nw.onclick = () => ideTermNew();
  const restart = $('#ide-term-restart');
  if (restart) restart.onclick = () => ideTermRestart();
  const clear = $('#ide-term-clear');
  if (clear) clear.onclick = () => ideTermClear();
  const intr = $('#ide-term-interrupt');
  if (intr) intr.onclick = () => ideTermInterrupt();
  ideTermBindResizer();
  ideTermRenderTabs();
  ideTermSyncActions();
  // 主题切换时同步所有 xterm 配色
  new MutationObserver(() => {
    ideTerms.forEach((st) => {
      if (st.term) { try { st.term.options.theme = ideTermColors(); } catch (e) { /* 忽略 */ } }
    });
  }).observe(document.documentElement, {
    attributes: true, attributeFilter: ['data-theme'],
  });
}
