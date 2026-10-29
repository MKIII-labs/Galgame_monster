'use strict';

/* ============================================================
   IDE 内置终端：后端 ConPTY（pywinpty）+ 前端 xterm.js

   - 一个会话一个 PowerShell 进程；切换会话后重新拉起
   - 输出走 SSE（/api/ide/term/stream），输入 / 尺寸 / 中断走 POST
   - 关闭面板只是隐藏，进程保留；「重启」才会换一个新 shell
   ============================================================ */

const IDE_TERM_H_KEY = 'ide_term_height_v1';
const IDE_TERM_DEFAULT_H = 240;

let ideTermState = null;        // { term, fit, tid, off, alive, sid, ctrl }
let ideTermInited = false;
let ideTermOpening = false;
let ideTermInBuf = '';
let ideTermInTimer = null;
let ideTermResizeTimer = null;

function ideTermHost() { return $('#ide-term-host'); }

function ideTermHint(msg) {
  const el = $('#ide-term-hint');
  if (!el) return;
  if (!msg) { el.textContent = ''; el.classList.add('hidden'); return; }
  el.textContent = msg;
  el.classList.remove('hidden');
}

function ideTermColors() {
  const panel = $('#ide-panel');
  const c = panel ? getComputedStyle(panel) : getComputedStyle(document.documentElement);
  const pick = (name, fallback) => (c.getPropertyValue(name) || '').trim() || fallback;
  return {
    background: pick('--bg', '#141517'),
    foreground: pick('--text', '#d4d4d4'),
    cursor: pick('--accent', '#4d6bfe'),
    cursorAccent: pick('--bg', '#141517'),
    selectionBackground: 'rgba(77, 107, 254, .35)',
    black: '#0b0b0c', red: '#e06c75', green: '#98c379', yellow: '#e5c07b',
    blue: '#61afef', magenta: '#c678dd', cyan: '#56b6c2', white: '#dcdfe4',
    brightBlack: '#5c6370', brightRed: '#ef7c86', brightGreen: '#a9d68a',
    brightYellow: '#f0cd92', brightBlue: '#7cc0f7', brightMagenta: '#d79ae6',
    brightCyan: '#72c7d2', brightWhite: '#f4f6f8',
  };
}

function ideTermCreate() {
  if (ideTermState) return ideTermState;
  const host = ideTermHost();
  if (!host || typeof window.Terminal !== 'function') return null;
  const term = new window.Terminal({
    fontSize: 13,
    fontFamily: '"Cascadia Mono", "Consolas", "Courier New", monospace',
    cursorBlink: true,
    scrollback: 5000,
    theme: ideTermColors(),
  });
  term.open(host);
  let fit = null;
  if (window.FitAddon && window.FitAddon.FitAddon) {
    fit = new window.FitAddon.FitAddon();
    term.loadAddon(fit);
  }
  ideTermState = { term, fit, tid: '', off: 0, alive: false, sid: '', ctrl: null, started: false };
  term.onData((d) => ideTermSend(d));
  term.onResize(({ cols, rows }) => {
    if (!ideTermState || !ideTermState.tid) return;
    clearTimeout(ideTermResizeTimer);
    ideTermResizeTimer = setTimeout(() => {
      api('/api/ide/term/resize', { tid: ideTermState.tid, cols, rows }).catch(() => {});
    }, 180);
  });
  host.addEventListener('mousedown', () => term.focus());
  return ideTermState;
}

function ideTermFit() {
  if (!ideTermState || !ideTermState.fit) return;
  try { ideTermState.fit.fit(); } catch (e) { /* 面板隐藏时尺寸为 0，忽略 */ }
}

function ideTermSend(data) {
  const st = ideTermState;
  if (!st) return;
  if (!st.alive) {
    // 进程已退出：回车重启（与 VS Code 里「按任意键重启终端」近似）
    if (data.indexOf('\r') >= 0 || data.indexOf('\n') >= 0) ideTermStart(true);
    return;
  }
  ideTermInBuf += data;
  if (ideTermInTimer) return;
  ideTermInTimer = setTimeout(() => {
    ideTermInTimer = null;
    const chunk = ideTermInBuf;
    ideTermInBuf = '';
    if (chunk) api('/api/ide/term/input', { tid: st.tid, data: chunk }).catch(() => {});
  }, 25);
}

function ideTermParsePoint(obj) {
  const st = ideTermState;
  if (!st) return;
  if (obj && obj.text) {
    if (obj.reset) st.term.reset();
    st.term.write(obj.text);
  }
  if (obj && typeof obj.off === 'number') st.off = obj.off;
  if (obj && obj.cwd) {
    const el = $('#ide-term-cwd');
    if (el) el.textContent = obj.cwd;
  }
  if (obj && obj.type === 'exit') {
    st.alive = false;
    ideTermHint('进程已退出，按回车重启');
  } else if (obj && obj.type === 'init') {
    st.alive = !!obj.alive;
    if (!st.alive) ideTermHint('进程已退出，按回车重启');
  }
}

function ideTermStream() {
  const st = ideTermState;
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
          if (tid !== (ideTermState && ideTermState.tid)) return;   // 已切到别的终端
          ideTermParsePoint(obj);
        }
      }
    })
    .catch((e) => {
      if (e && e.name === 'AbortError') return;
      ideTermHint('连接中断：' + (e && e.message ? e.message : e) + '（重新打开面板可恢复）');
    })
    .finally(() => {
      if (ideTermState && ideTermState.ctrl === ctrl) ideTermState.ctrl = null;
    });
}

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

async function ideTermStart(force) {
  if (ideTermOpening) return;
  const s = (typeof curSession === 'function') ? curSession() : null;
  if (!s) { ideTermHint('没有当前会话'); return; }
  const st = ideTermCreate();
  if (!st) { ideTermHint('xterm.js 未加载，无法启动终端'); return; }
  ideTermOpening = true;
  try {
    ideTermHint('');
    if (force && st.tid) {
      try { await api('/api/ide/term/kill', { tid: st.tid }); } catch (e) { /* 忽略 */ }
      st.tid = '';
    }
    ideTermFit();
    const cols = st.term.cols || 120;
    const rows = st.term.rows || 30;
    const r = await api('/api/ide/term/start', {
      session_id: s.id, cols, rows, force: !!force,
    });
    st.tid = r.tid;
    st.off = r.off || 0;
    st.alive = !!r.alive;
    st.sid = s.id;
    st.started = true;
    const cwd = $('#ide-term-cwd');
    if (cwd) cwd.textContent = r.cwd || 'PowerShell';
    if (r.text) {
      if (r.reset) st.term.reset();
      st.term.write(r.text);
    }
    if (r.alive) ideTermStream();
    else ideTermHint('进程已退出，按回车重启');
  } catch (e) {
    ideTermHint('终端启动失败：' + (e && e.message ? e.message : e));
  } finally {
    ideTermOpening = false;
  }
}

function ideTermEnsure() {
  const s = (typeof curSession === 'function') ? curSession() : null;
  const st = ideTermState;
  if (!st) { ideTermStart(false); return; }
  if (!st.tid || !s || st.sid !== s.id) { ideTermStart(false); return; }
  ideTermFit();
  if (st.alive && !st.ctrl) ideTermStream();
}

function ideTermToggle(force) {
  const el = $('#ide-term');
  if (!el) return;
  const open = typeof force === 'boolean' ? force : el.classList.contains('hidden');
  el.classList.toggle('hidden', !open);
  const rz = $('#ide-term-resizer');
  if (rz) rz.classList.toggle('hidden', !open);
  const act = $('#ide-term-act');
  if (act) act.classList.toggle('active', open);
  if (open) {
    ideTermApplyHeight();
    ideTermEnsure();
    setTimeout(() => {
      ideTermFit();
      if (ideTermState && ideTermState.term) ideTermState.term.focus();
    }, 40);
  } else if (ideTermState && ideTermState.term) {
    ideTermState.term.blur();
  }
}

function ideTermBindResizer() {
  const rz = $('#ide-term-resizer');
  const el = $('#ide-term');
  if (!rz || !el) return;
  rz.addEventListener('dblclick', () => {
    el.style.height = IDE_TERM_DEFAULT_H + 'px';
    try { localStorage.removeItem(IDE_TERM_H_KEY); } catch (e) { /* 忽略 */ }
    ideTermFit();
  });
  rz.addEventListener('mousedown', (e) => {
    e.preventDefault();
    const startY = e.clientY;
    const startH = el.getBoundingClientRect().height;
    const main = el.parentElement;
    const maxH = main ? Math.max(120, main.getBoundingClientRect().height * 0.75) : 600;
    const move = (ev) => {
      const h = Math.max(90, Math.min(maxH, startH - (ev.clientY - startY)));
      el.style.height = h + 'px';
      ideTermFit();
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

/* 切换会话：旧终端属于上一个沙箱，直接回收；面板开着就为新会话重新拉起 */
function ideTermResetForSession() {
  const st = ideTermState;
  if (!st) return;
  if (st.ctrl) { try { st.ctrl.abort(); } catch (e) { /* 忽略 */ } st.ctrl = null; }
  if (st.tid) api('/api/ide/term/kill', { tid: st.tid }).catch(() => {});
  st.tid = '';
  st.off = 0;
  st.alive = false;
  st.sid = '';
  try { st.term.reset(); st.term.clear(); } catch (e) { /* 忽略 */ }
  ideTermHint('');
  const el = $('#ide-term');
  if (el && !el.classList.contains('hidden')) ideTermStart(false);
}

function ideTermInitOnce() {
  if (ideTermInited) return;
  ideTermInited = true;
  const act = $('#ide-term-act');
  if (act) act.onclick = () => ideTermToggle();
  const close = $('#ide-term-close');
  if (close) close.onclick = () => ideTermToggle(false);
  const restart = $('#ide-term-restart');
  if (restart) restart.onclick = () => ideTermStart(true);
  const clear = $('#ide-term-clear');
  if (clear) clear.onclick = () => {
    if (!ideTermState || !ideTermState.tid) return;
    api('/api/ide/term/clear', { tid: ideTermState.tid }).catch(() => {});
    ideTermState.term.clear();
  };
  const intr = $('#ide-term-interrupt');
  if (intr) intr.onclick = () => {
    if (!ideTermState || !ideTermState.tid) return;
    api('/api/ide/term/interrupt', { tid: ideTermState.tid }).catch(() => {});
  };
  ideTermBindResizer();
  // 主题切换时同步 xterm 配色
  new MutationObserver(() => {
    if (ideTermState && ideTermState.term) ideTermState.term.options.theme = ideTermColors();
  }).observe(document.documentElement, {
    attributes: true, attributeFilter: ['data-theme', 'data-ide-theme'],
  });
}
