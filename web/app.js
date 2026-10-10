/* ============================================================
   Galgame_monster —— 前端逻辑
   ============================================================ */
'use strict';

const TOKEN = window.__TOKEN__ || '';
const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

const state = {
  config: null,
  profile: null,
  sessions: { current: null, sessions: [] },
  streaming: false,
  acc: '',
  pendingPresets: {},
};

/* ---------------- HTTP ---------------- */
async function api(path, body) {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Auth-Token': TOKEN },
    body: JSON.stringify(body || {}),
  });
  if (!res.ok) throw new Error('HTTP ' + res.status);
  return res.json();
}

function toast(msg) {
  const t = $('#toast');
  t.textContent = msg;
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

/* ============================================================
   Markdown 渲染（自实现，避免额外依赖）
   ============================================================ */
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function mathHtml(tex, display) {
  if (window.katex) {
    try {
      return katex.renderToString(tex, { displayMode: display, throwOnError: false });
    } catch (e) { /* 落到下面 */ }
  }
  return '<code class="inline math-pending" data-tex="' + escapeHtml(tex) +
    '" data-display="' + (display ? '1' : '0') + '">' + escapeHtml(tex) + '</code>';
}

function codeBlockHtml(lang, code) {
  let body;
  if (window.hljs && lang && hljs.getLanguage(lang)) {
    try { body = hljs.highlight(code, { language: lang }).value; } catch (e) { body = escapeHtml(code); }
  } else if (window.hljs) {
    try { body = hljs.highlightAuto(code).value; } catch (e) { body = escapeHtml(code); }
  } else {
    body = escapeHtml(code);
  }
  return '<div class="code-block"><div class="code-head"><span>' + escapeHtml(lang || 'text') +
    '</span><button class="copy" data-copy>复制</button></div><pre><code class="hljs" data-lang="' +
    escapeHtml(lang || '') + '">' + body + '</code></pre></div>';
}

function inlineFmt(s) {
  // 双链 [[目标]] / [[目标|别名]] / [[目标#锚点]]（放在最前面，避免被其它规则干扰）
  s = s.replace(/\[\[([^\[\]\n]+?)\]\]/g, (m, raw) => {
    const seg = String(raw).split('|');
    const head = seg[0];
    const target = head.split('#')[0].trim();
    const anchor = (head.split('#')[1] || '').trim();
    const label = (seg[1] || head).trim();
    if (!target) return m;
    return '<a class="wikilink" data-note="' + target + '" data-anchor="' + anchor + '">' + label + '</a>';
  });
  s = s.replace(/`([^`]+)`/g, '<code class="inline">$1</code>');
  s = s.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/(^|[^*\w])\*([^*\n]+)\*/g, '$1<em>$2</em>');
  s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g,
    '<a href="$2" target="_blank" rel="noreferrer">$1</a>');
  return s;
}

function renderMarkdown(src) {
  if (!src) return '';
  let text = String(src);

  // 1) 抽出围栏代码块
  const codes = [];
  text = text.replace(/```([^\n`]*)\n?([\s\S]*?)(?:```|$)/g, (m, lang, code) => {
    codes.push({ lang: (lang || '').trim(), code: code.replace(/\n$/, '') });
    return '\u0000C' + (codes.length - 1) + '\u0000';
  });

  // 2) 抽出数学公式（$$ / \[ 为块级，$ / \( 为行内）
  const maths = [];
  const pushMath = (tex, display) => {
    maths.push({ tex, display });
    return '\u0000M' + (maths.length - 1) + '\u0000';
  };
  text = text.replace(/\$\$([\s\S]+?)\$\$/g, (m, t) => pushMath(t, true));
  text = text.replace(/\\\[([\s\S]+?)\\\]/g, (m, t) => pushMath(t, true));
  text = text.replace(/\\\(([\s\S]+?)\\\)/g, (m, t) => pushMath(t, false));
  text = text.replace(/\$([^\n$]+?)\$/g, (m, t) => pushMath(t, false));

  // 3) 转义剩余文本
  text = escapeHtml(text);

  // 4) 块级解析
  const lines = text.split('\n');
  let html = '';
  let i = 0;
  const isBlank = (l) => /^\s*$/.test(l);

  while (i < lines.length) {
    let line = lines[i];

    if (isBlank(line)) { i++; continue; }

    // 标题
    let m = line.match(/^(#{1,6})\s+(.*)$/);
    if (m) {
      const lv = Math.min(m[1].length, 6);
      html += '<h' + lv + '>' + inlineFmt(m[2]) + '</h' + lv + '>';
      i++; continue;
    }

    // 分割线
    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) { html += '<hr>'; i++; continue; }

    // 引用
    if (/^\s*&gt;\s?/.test(line)) {
      const buf = [];
      while (i < lines.length && /^\s*&gt;\s?/.test(lines[i])) {
        buf.push(lines[i].replace(/^\s*&gt;\s?/, ''));
        i++;
      }
      html += '<blockquote>' + inlineFmt(buf.join('<br>')) + '</blockquote>';
      continue;
    }

    // 表格
    if (line.indexOf('|') >= 0 && i + 1 < lines.length &&
        /^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(lines[i + 1])) {
      const splitRow = (r) => r.replace(/^\s*\|/, '').replace(/\|\s*$/, '').split('|').map((c) => c.trim());
      const head = splitRow(line);
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].indexOf('|') >= 0 && !isBlank(lines[i])) {
        rows.push(splitRow(lines[i])); i++;
      }
      html += '<table><thead><tr>' + head.map((c) => '<th>' + inlineFmt(c) + '</th>').join('') +
        '</tr></thead><tbody>' +
        rows.map((r) => '<tr>' + head.map((_, k) => '<td>' + inlineFmt(r[k] || '') + '</td>').join('') + '</tr>').join('') +
        '</tbody></table>';
      continue;
    }

    // 列表
    if (/^\s*([-*+]|\d+\.)\s+/.test(line)) {
      const ordered = /^\s*\d+\.\s+/.test(line);
      const items = [];
      while (i < lines.length && /^\s*([-*+]|\d+\.)\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*([-*+]|\d+\.)\s+/, ''));
        i++;
      }
      const tag = ordered ? 'ol' : 'ul';
      html += '<' + tag + '>' + items.map((t) => '<li>' + inlineFmt(t) + '</li>').join('') + '</' + tag + '>';
      continue;
    }

    // 段落
    const para = [];
    while (i < lines.length && !isBlank(lines[i]) &&
           !/^(#{1,6})\s+/.test(lines[i]) &&
           !/^\s*&gt;\s?/.test(lines[i]) &&
           !/^\s*([-*+]|\d+\.)\s+/.test(lines[i]) &&
           !/^\s*([-*_])(\s*\1){2,}\s*$/.test(lines[i])) {
      para.push(lines[i]); i++;
      if (i < lines.length && lines[i].indexOf('|') >= 0 && /^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(lines[i] || '')) break;
    }
    html += '<p>' + inlineFmt(para.join('<br>')) + '</p>';
  }

  // 5) 还原代码块与公式
  html = html.replace(/\u0000C(\d+)\u0000/g, (m, idx) => {
    const c = codes[+idx];
    return c ? codeBlockHtml(c.lang, c.code) : '';
  });
  html = html.replace(/\u0000M(\d+)\u0000/g, (m, idx) => {
    const k = maths[+idx];
    return k ? mathHtml(k.tex, k.display) : '';
  });
  return html;
}

/* ============================================================
   会话渲染
   ============================================================ */
function curSession() {
  return state.sessions.sessions.find((s) => s.id === state.sessions.current) || null;
}

function renderSessions() {
  const box = $('#session-list');
  box.innerHTML = '';
  state.sessions.sessions.forEach((s) => {
    const el = document.createElement('div');
    el.className = 'session-item' + (s.id === state.sessions.current ? ' active' : '');
    el.title = '双击可重命名';
    const title = document.createElement('span');
    title.className = 'stitle';
    title.textContent = s.title || '新对话';
    const ren = document.createElement('span');
    ren.className = 'sren';
    ren.textContent = '✎';
    ren.title = '重命名';
    ren.onclick = (e) => { e.stopPropagation(); renameSession(s.id); };
    const del = document.createElement('span');
    del.className = 'sdel';
    del.textContent = '🗑';
    del.title = '删除会话';
    del.onclick = (e) => { e.stopPropagation(); deleteSession(s.id); };
    el.appendChild(title);
    el.appendChild(ren);
    el.appendChild(del);
    el.onclick = () => selectSession(s.id);
    el.ondblclick = () => renameSession(s.id);
    box.appendChild(el);
  });
  updateStatusLine();
}

function updateStatusLine() {
  const c = state.config || {};
  const p = state.profile || {};
  const tag = (v) => (v && String(v).trim() ? '已配置' : '未配置');
  $('#status-line').innerHTML =
    '聊天 API：' + tag(c.chat_api_key) + ' · 图像 API：' + tag(c.vision_api_key) +
    '<br>AI 记录画像：' + (p.auto_learn_enabled ? '开启' : '关闭');
}

/* ---------------- 上下文容量进度条 ---------------- */
const ctxRemind = { session: null, fired: {} };   // 每会话已触发的提醒档位（10 / 5）

function renderContextBar(ctx) {
  const bar = $('#ctx-bar');
  if (!bar) return;
  if (!ctx || !ctx.limit) { bar.classList.add('hidden'); return; }
  bar.classList.remove('hidden');
  const pct = Math.max(0, Math.min(100, ctx.pct || 0));
  const remainPct = ctx.remain / ctx.limit;
  let level = '';
  if (remainPct < 0.05) level = 'danger';
  else if (remainPct < 0.10) level = 'warn';
  bar.classList.toggle('warn', level === 'warn');
  bar.classList.toggle('danger', level === 'danger');
  $('#ctx-fill').style.width = pct + '%';
  $('#ctx-text').textContent =
    fmtNum(ctx.used) + ' / ' + fmtNum(ctx.limit) + ' · ' + pct + '% · 剩余 ' +
    fmtNum(ctx.remain) + (ctx.compressed ? ' · 已压缩' : '');
  remindContext(ctx, remainPct);
}

function remindContext(ctx, remainPct) {
  const sid = state.sessions.current;
  if (ctxRemind.session !== sid) { ctxRemind.session = sid; ctxRemind.fired = {}; }
  if (remainPct >= 0.10) { ctxRemind.fired = {}; return; }   // 回落后重置，允许再次提醒
  if (remainPct < 0.05) {
    if (!ctxRemind.fired['5']) {
      ctxRemind.fired['5'] = true; ctxRemind.fired['10'] = true;
      toast('⚠️ 上下文剩余不足 5%，即将触发历史压缩');
    }
  } else if (!ctxRemind.fired['10']) {
    ctxRemind.fired['10'] = true;
    toast('上下文剩余不足 10%，请注意');
  }
}

async function compressContextManually() {
  if (state.streaming) { toast('正在生成回复，请稍后再试'); return; }
  const btn = $('#btn-compress');
  if (btn) btn.disabled = true;
  try {
    const r = await api('/api/context/compress', { session_id: state.sessions.current });
    if (r && r.sessions) state.sessions = r.sessions;
    renderSessions();
    if (r && r.context) renderContextBar(r.context);
    toast((r && r.message) || (r && r.ok ? '已压缩较早的历史对话' : '未能压缩'));
  } catch (e) {
    toast('压缩失败：' + e);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function selectSession(id) {
  if (state.streaming) await stopStreaming();
  state.sessions.current = id;
  // 服务端返回的是精简数据：新选中的会话（即当前会话）会带上它的消息
  const r = await api('/api/session/select', { id });
  if (r && r.sessions) state.sessions = r.sessions;
  renderSessions();
  renderMessages();
  if (r) renderContextBar(r.context);
}

async function deleteSession(id) {
  if (state.streaming) await stopStreaming();
  const r = await api('/api/session/delete', { id });
  state.sessions = r.sessions;
  renderSessions();
  renderMessages();
  if (r) renderContextBar(r.context);
}

async function newSession() {
  if (state.streaming) await stopStreaming();
  const r = await api('/api/session/new', {});
  state.sessions = r.sessions;
  renderSessions();
  renderMessages();
  if (r) renderContextBar(r.context);
  $('#input').focus();
}

async function renameSession(id) {
  const s = state.sessions.sessions.find((x) => x.id === id);
  const name = prompt('重命名会话', (s && s.title) || '');
  if (!name || !name.trim()) return;
  const r = await api('/api/session/rename', { id, title: name.trim() });
  state.sessions = r.sessions;
  renderSessions();
}

/* ============================================================
   消息渲染
   ============================================================ */
function hideEmpty() {
  const e = $('#empty');
  if (e) e.remove();
}

function showEmptyIfNeeded() {
  const chat = $('#chat');
  if (chat.querySelector('.msg')) {
    const e = $('#empty');
    if (e) e.remove();
  } else if (!$('#empty')) {
    location.reload();
  }
}

function nearBottom() {
  const c = $('#chat');
  return c.scrollHeight - c.scrollTop - c.clientHeight < 120;
}

// 流式期间必须「即时定位」：每帧都调用，若用平滑滚动会互相打断而产生抖动
function scrollToBottom(force) {
  const c = $('#chat');
  if (force || nearBottom()) c.scrollTop = c.scrollHeight;
}

// 只有用户主动点「回到底部」时才用平滑滚动
let _smoothScrollTimer = null;
function scrollToBottomSmooth() {
  const c = $('#chat');
  c.classList.add('smooth');
  c.scrollTop = c.scrollHeight;
  clearTimeout(_smoothScrollTimer);
  _smoothScrollTimer = setTimeout(() => c.classList.remove('smooth'), 450);
}

function buildActions(row, getText, index, role) {
  const bar = document.createElement('div');
  bar.className = 'msg-actions';
  const mk = (label, fn) => {
    const b = document.createElement('button');
    b.textContent = label;
    b.onclick = fn;
    bar.appendChild(b);
    return b;
  };
  mk('复制', () => { navigator.clipboard.writeText(getText()).then(() => toast('已复制')); });
  if (role === 'user') {
    mk('撤回', () => withdrawUserMessage(index));
    mk('修改', () => editUserMessage(index));
  } else {
    row._selBtn = mk('选择文本', () => toggleRawView(row));
    if (typeof index === 'number') mk('重新发言', () => regenerateReply(index));
  }
  return bar;
}

// 记忆写入标记：AI 本轮改了哪些记忆 + 可撤销
function buildMemoryChip(mem, index) {
  const box = document.createElement('div');
  box.className = 'mem-chip' + (mem.undone ? ' undone' : '');
  const icon = document.createElement('span');
  icon.className = 'mem-icon';
  icon.textContent = '🧠';
  const text = document.createElement('span');
  text.className = 'mem-text';
  const scopes = (mem.scopes || []).join('、');
  text.textContent = '已更新记忆：' + (mem.summary || '') + (scopes ? '（' + scopes + '）' : '');
  box.appendChild(icon);
  box.appendChild(text);
  if (mem.undone) {
    const tag = document.createElement('span');
    tag.className = 'mem-undone';
    tag.textContent = '已撤销';
    box.appendChild(tag);
    return box;
  }
  const btn = document.createElement('button');
  btn.className = 'mem-undo';
  btn.textContent = '撤销';
  btn.onclick = () => undoMemory(mem.undo, index, btn);
  box.appendChild(btn);
  return box;
}

async function undoMemory(token, index, btn) {
  if (state.streaming || !token) return;
  if (btn) btn.disabled = true;
  try {
    const r = await api('/api/memory/undo', {
      token: token, session_id: state.sessions.current, index: index,
    });
    if (!r || !r.ok) { toast('撤销失败'); if (btn) btn.disabled = false; return; }
    if (r.sessions) state.sessions = r.sessions;
    renderSessions();
    renderMessages();
    if (r.context) renderContextBar(r.context);
    toast('已撤销这次记忆更新');
    refreshKbIfOpen();
  } catch (e) {
    toast('撤销失败：' + e);
    if (btn) btn.disabled = false;
  }
}

// 版本预览状态：点箭头只改显示（不请求接口、不改数据），确认后才真正切换
let verPreview = null;   // { sid, index, vidx }

function buildUserRow(text, index, msg) {
  const row = document.createElement('div');
  row.className = 'msg user';
  if (typeof index === 'number') row.dataset.idx = index;
  const wrap = document.createElement('div');
  wrap.className = 'user-wrap';
  const b = document.createElement('div');
  b.className = 'bubble';
  b.textContent = text;
  wrap.appendChild(b);

  // 历史版本切换（DeepSeek 式 ‹ n/N ›）：只有被「修改」过的消息才有多版本。
  // 点箭头只「预览」（改显示、不发请求）；确认后点「发送此版本」才真正切换并重新生成。
  const vers = msg && Array.isArray(msg.versions) ? msg.versions : null;
  if (vers && vers.length > 1 && typeof index === 'number') {
    const committed = typeof msg.vidx === 'number' ? msg.vidx : vers.length - 1;
    const pv = (verPreview && verPreview.sid === state.sessions.current
                && verPreview.index === index) ? verPreview.vidx : null;
    const shown = pv === null ? committed : pv;
    if (pv !== null) b.textContent = vers[shown];     // 预览：只覆盖显示内容

    const nav = document.createElement('div');
    nav.className = 'ver-nav';
    const mkArrow = (label, target, tip, disabled) => {
      const btn = document.createElement('button');
      btn.className = 'ver-btn'; btn.textContent = label; btn.title = tip;
      btn.disabled = disabled;
      btn.onclick = () => previewVersion(index, target);
      return btn;
    };
    const info = document.createElement('span');
    info.className = 'ver-idx';
    info.textContent = (shown + 1) + '/' + vers.length;
    nav.appendChild(mkArrow('‹', shown - 1, '上一个版本', shown <= 0));
    nav.appendChild(info);
    nav.appendChild(mkArrow('›', shown + 1, '下一个版本', shown >= vers.length - 1));

    if (pv !== null && pv !== committed) {
      const tip = document.createElement('span');
      tip.className = 'ver-preview-tip';
      tip.textContent = '预览中';
      const send = document.createElement('button');
      send.className = 'ver-send'; send.textContent = '发送此版本';
      send.onclick = () => switchVersion(index, pv);
      const cancel = document.createElement('button');
      cancel.className = 'ver-cancel'; cancel.textContent = '取消';
      cancel.onclick = () => cancelVersionPreview(index);
      nav.appendChild(tip); nav.appendChild(send); nav.appendChild(cancel);
    }
    wrap.appendChild(nav);
  }

  if (typeof index === 'number') wrap.appendChild(buildActions(row, () => text, index, 'user'));
  row.appendChild(wrap);
  row._wrap = wrap;
  row._bubble = b;
  return row;
}

function appendUser(text, index, msg) {
  const row = buildUserRow(text, index, msg);   // 必须把 index 传下去，否则这条消息没有「撤回/修改」按钮
  row.classList.add('enter');                    // 只有新消息播入场动画，历史重渲染不播
  $('#chat').appendChild(row);
  scrollToBottom(true);
  return row;
}

function buildThinkBlock(text, open) {
  const box = document.createElement('div');
  box.className = 'think' + (open ? ' open' : '');
  const head = document.createElement('div');
  head.className = 'think-head';
  const dot = document.createElement('span');
  dot.textContent = '✦';
  const label = document.createElement('span');
  label.className = 'think-label';
  label.textContent = '深度思考';
  const caret = document.createElement('span');
  caret.className = 'think-caret';
  caret.textContent = '▸';
  head.appendChild(dot);
  head.appendChild(label);
  head.appendChild(caret);
  const body = document.createElement('div');
  body.className = 'think-body';
  body.textContent = text || '';
  head.onclick = () => box.classList.toggle('open');
  box.appendChild(head);
  box.appendChild(body);
  box._body = body;
  box._label = label;
  return box;
}

function buildSourcesBlock(items, open) {
  const box = buildThinkBlock('', !!open);
  box.classList.add('sources');
  box._label.textContent = '参考来源 · ' + items.length;
  const body = box._body;
  body.textContent = '';
  items.forEach((it, i) => {
    const row = document.createElement('div');
    row.className = 'src-item';
    const a = document.createElement('a');
    a.href = it.url || '#';
    a.target = '_blank';
    a.rel = 'noreferrer';
    a.textContent = '[' + (i + 1) + '] ' + (it.title || it.url || '来源');
    row.appendChild(a);
    const meta = [];
    if (it.site) meta.push(it.site);
    if (it.snippet) meta.push(it.snippet.slice(0, 130) + (it.snippet.length > 130 ? '…' : ''));
    if (meta.length) {
      const m = document.createElement('span');
      m.className = 'src-meta';
      m.textContent = meta.join(' · ');
      row.appendChild(m);
    }
    body.appendChild(row);
  });
  return box;
}

function usageText(u) {
  if (!u) return '';
  const f = (n) => (n || 0).toLocaleString('en-US');
  let s = (u.model ? u.model + ' · ' : '') +
    '输入 ' + f(u.prompt) + ' · 输出 ' + f(u.completion) + ' · 合计 ' + f(u.total) + ' tokens';
  if (u.cached) s += ' · 缓存命中 ' + f(u.cached);
  return s;
}

function buildAiRow(contentHtml, rawText, index, reasoning, sources, usage, memory) {
  const row = document.createElement('div');
  row.className = 'msg ai';
  if (typeof index === 'number') row.dataset.idx = index;
  const av = document.createElement('div');
  av.className = 'avatar';
  av.textContent = '✦';
  const wrap = document.createElement('div');
  wrap.className = 'content-wrap';
  if (sources && sources.length) wrap.appendChild(buildSourcesBlock(sources, false));
  if (reasoning) wrap.appendChild(buildThinkBlock(reasoning, false));
  const c = document.createElement('div');
  c.className = 'content';
  c.innerHTML = contentHtml;
  wrap.appendChild(c);
  row.appendChild(av);
  row.appendChild(wrap);
  row._content = c;
  row._wrap = wrap;
  row._raw = rawText || '';
  if (memory) wrap.appendChild(buildMemoryChip(memory, index));
  if (rawText) wrap.appendChild(buildActions(row, () => row._raw, index, 'ai'));
  if (usage) {
    const u = document.createElement('div');
    u.className = 'usage-line';
    u.textContent = usageText(usage);
    wrap.appendChild(u);
    row._usageEl = u;
  }
  return row;
}

function appendAi(contentHtml, rawText, index, reasoning, sources, usage, memory) {
  const row = buildAiRow(contentHtml, rawText, index, reasoning, sources, usage, memory);
  row.classList.add('enter');                    // 只有新消息播入场动画，历史重渲染不播
  $('#chat').appendChild(row);
  scrollToBottom(true);
  return row;
}

const RENDER_PAGE = 60;   // 首屏只渲染最近 N 条，长历史时避免一次性渲染卡顿

function renderMessages(fromIndex) {
  const chat = $('#chat');
  const expanding = typeof fromIndex === 'number';
  const oldHeight = chat.scrollHeight;
  const oldTop = chat.scrollTop;
  chat.innerHTML = '';
  const s = curSession();
  if (!s || !(s.messages || []).length) {
    chat.innerHTML = '<div id="empty" class="empty">' +
      '<div class="empty-logo">✦</div><h1>有什么可以帮你的？</h1>' +
      '<p class="empty-sub">支持 Markdown、LaTeX 公式、图片识别与用户画像</p>' +
      '<div class="chips">' +
      '<button class="chip" data-prompt="用简单的语言解释一下量子纠缠">解释量子纠缠</button>' +
      '<button class="chip" data-prompt="帮我写一个 Python 快速排序，并解释思路">写个快速排序</button>' +
      '<button class="chip" data-prompt="用一句话总结一下相对论">总结相对论</button>' +
      '</div></div>';
    bindChips();
    return;
  }

  const msgs = s.messages;
  const total = msgs.length;
  const start = expanding ? Math.max(0, fromIndex) : Math.max(0, total - RENDER_PAGE);

  // 先在一个 fragment 里建好，再一次性插入 —— 只触发一次布局
  const frag = document.createDocumentFragment();
  if (start > 0) {
    const more = document.createElement('button');
    more.className = 'load-more';
    more.textContent = '↑ 展开更早的 ' + Math.min(start, RENDER_PAGE) + ' 条';
    more.onclick = () => renderMessages(Math.max(0, start - RENDER_PAGE));
    frag.appendChild(more);
  }
  for (let idx = start; idx < total; idx++) {
    const m = msgs[idx];
    frag.appendChild(m.role === 'user'
      ? buildUserRow(m.content, idx, m)
      : buildAiRow(renderMarkdown(m.content), m.content, idx, m.reasoning, m.sources, m.usage, m.memory));
  }
  chat.appendChild(frag);
  // 展开更早时保持视觉位置不动，否则滚到底部
  chat.scrollTop = expanding ? oldTop + (chat.scrollHeight - oldHeight) : chat.scrollHeight;
}

function bindChips() {
  $$('.chip').forEach((c) => {
    c.onclick = () => { $('#input').value = c.dataset.prompt; sendMessage(); };
  });
}

/* ============================================================
   发送 / 流式
   ============================================================ */
function setStreaming(on) {
  state.streaming = on;
  $('#btn-send').disabled = on;
  // 图片 / 停止按钮共用同一插槽，用 .off 交叉淡入替代 display:none 硬切
  $('#btn-stop').classList.toggle('off', !on);
  $('#btn-image').classList.toggle('off', on);
}

async function stopStreaming() {
  try { await api('/api/stop', { session_id: state.sessions.current }); } catch (e) { }
}

function hasChatKey() {
  return !!(state.config && String(state.config.chat_api_key || '').trim());
}

function hasSearchKey() {
  return !!(state.config && String(state.config.search_api_key || '').trim());
}

function syncSearchPill() {
  const pill = $('#btn-search');
  if (!pill) return;
  const ok = hasSearchKey();
  pill.classList.toggle('active', !!state.searchEnabled && ok);
  pill.classList.toggle('disabled', !ok);
}

async function sendMessage() {
  if (state.streaming) return;
  const input = $('#input');
  const text = input.value.trim();
  if (!text) return;
  if (!hasChatKey()) { toast('请先在「设置 → 聊天 API」中填写密钥'); return; }
  input.value = '';
  autoGrow();
  hideEmpty();
  // 发新消息前，把还处于「预览」的用户消息恢复为当前版本
  if (verPreview && verPreview.sid === state.sessions.current) refreshUserRow(verPreview.index);
  verPreview = null;
  appendUser(text, curSession() ? curSession().messages.length : undefined);
  await streamChat({ content: text });
}

async function streamChat(extra) {
  // AI 占位：先摆一组「呼吸点」表示正在生成，首个 token 到达即换成正文
  const row = appendAi('', '', undefined);
  const c = row._content;
  c.style.whiteSpace = 'pre-wrap';
  const typing = document.createElement('span');
  typing.className = 'typing';
  typing.innerHTML = '<i></i><i></i><i></i>';
  c.appendChild(typing);
  const cur = document.createElement('span');
  cur.className = 'cursor';

  setStreaming(true);
  state.acc = '';
  let acc = '';
  let textNode = null;          // 流式期间只往这个节点追加，避免每个 chunk 重写整段
  let thinkBox = null;
  let sourcesBox = null;
  let contentStarted = false;
  let memNote = null;

  // 增量写入正文（首次调用时把呼吸点换成文本节点）
  const pushText = (t) => {
    if (typing.parentNode) typing.remove();
    if (!textNode) {
      textNode = document.createTextNode('');
      c.insertBefore(textNode, cur.parentNode ? cur : null);
      c.appendChild(cur);       // 光标始终跟在文本末尾
    }
    textNode.appendData(t);
  };

  try {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Auth-Token': TOKEN },
      body: JSON.stringify(Object.assign({
        session_id: state.sessions.current,
        think_enabled: !!state.thinkEnabled,
        think_level: state.thinkLevel || 'high',
        search_enabled: !!state.searchEnabled,
      }, extra || {})),
    });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let pos;
      while ((pos = buf.indexOf('\n\n')) >= 0) {
        const chunk = buf.slice(0, pos);
        buf = buf.slice(pos + 2);
        const line = chunk.replace(/^data:\s*/, '');
        if (!line) continue;
        let obj;
        try { obj = JSON.parse(line); } catch (e) { continue; }
        if (obj.type === 'searching') {
          if (!sourcesBox) {
            sourcesBox = buildThinkBlock('', true);
            sourcesBox.classList.add('sources');
            sourcesBox._label.textContent = '正在联网搜索…';
            row._wrap.insertBefore(sourcesBox, row._wrap.firstChild);
          }
          scrollToBottom();
        } else if (obj.type === 'search_queries') {
          if (!sourcesBox) {
            sourcesBox = buildThinkBlock('', true);
            sourcesBox.classList.add('sources');
            row._wrap.insertBefore(sourcesBox, row._wrap.firstChild);
          }
          const qs = (obj.queries || []).map((s) => {
            s = String(s);
            return s.length > 18 ? s.slice(0, 18) + '…' : s;
          });
          sourcesBox._label.textContent = '正在搜索：' + qs.join(' / ');
          scrollToBottom();
        } else if (obj.type === 'sources') {
          if (sourcesBox) sourcesBox.remove();
          sourcesBox = buildSourcesBlock(obj.items, false);
          row._wrap.insertBefore(sourcesBox, row._wrap.firstChild);
          scrollToBottom();
        } else if (obj.type === 'search_error') {
          if (sourcesBox) { sourcesBox.remove(); sourcesBox = null; }
          const note = document.createElement('div');
          note.className = 'search-note';
          note.textContent = obj.message;
          row._wrap.insertBefore(note, row._wrap.firstChild);
          scrollToBottom();
        } else if (obj.type === 'usage') {
          if (!row._usageEl) {
            row._usageEl = document.createElement('div');
            row._usageEl.className = 'usage-line';
            row._wrap.appendChild(row._usageEl);
          }
          row._usageEl.textContent = usageText(obj.usage);
        } else if (obj.type === 'reasoning') {
          if (!thinkBox) {
            thinkBox = buildThinkBlock('', true);
            thinkBox._label.textContent = '深度思考中…';
            row._wrap.insertBefore(thinkBox, row._content);
          }
          thinkBox._body.textContent += obj.text;
          scrollToBottom();
        } else if (obj.type === 'delta') {
          if (thinkBox && !contentStarted) {
            contentStarted = true;
            thinkBox.classList.remove('open');
            thinkBox._label.textContent = '已深度思考';
          }
          acc += obj.text;
          pushText(obj.text);
          scrollToBottom();
        } else if (obj.type === 'error') {
          const t = (acc ? '\n\n' : '') + obj.message;
          acc += t;
          pushText(t);
          scrollToBottom();
        } else if (obj.type === 'mem_write') {
          memNote = obj.memory;
        } else if (obj.type === 'context') {
          renderContextBar(obj.context);
        } else if (obj.type === 'compressing') {
          toast('正在压缩较早的历史对话…');
        } else if (obj.sessions) {
          state.sessions = obj.sessions;
          renderSessions();
          if (obj.context) renderContextBar(obj.context);
        }
      }
    }
  } catch (e) {
    acc += (acc ? '\n\n' : '') + '❌ 请求失败：' + e;
  } finally {
    if (typing.parentNode) typing.remove();
    cur.remove();
    c.style.whiteSpace = '';
    if (thinkBox) {
      thinkBox.classList.remove('open');
      thinkBox._label.textContent = '已深度思考';
    }
    if (acc.trim()) {
      row._raw = acc;
      const atBottom = nearBottom();      // 重排 markdown 会改变高度，先记住是否贴底
      c.innerHTML = renderMarkdown(acc);
      const last = curSession();
      const idx = last ? last.messages.length - 1 : undefined;
      if (typeof idx === 'number') row.dataset.idx = idx;
      if (memNote) row._wrap.appendChild(buildMemoryChip(memNote, idx));
      row._wrap.appendChild(buildActions(row, () => row._raw, idx, 'ai'));
      refreshKbIfOpen();
      if (atBottom) scrollToBottom(true);  // 原本贴底就重新贴底，消除「结尾跳一下」
    } else if (!thinkBox) {
      c.innerHTML = '<p style="color:var(--text-dim)">（无内容）</p>';
    }
    setStreaming(false);
    scrollToBottom();
    // 同步服务端最新会话（标题/消息）
    try {
      const st = await fetch('/api/state', { headers: { 'X-Auth-Token': TOKEN } });
      if (st.ok) {
        const d = await st.json();
        state.sessions = d.sessions;
        renderSessions();
        renderContextBar(d.context);
      }
    } catch (e) { }
  }
}

/* ============================================================
   撤回 / 修改 / 重新发言 / 源码视图
   ============================================================ */
async function withdrawUserMessage(index) {
  if (state.streaming || typeof index !== 'number') return;
  if (!confirm('撤回这条消息？它之后的 AI 回复也会一并移除。')) return;
  const r = await api('/api/message/withdraw', { session_id: state.sessions.current, index: index });
  if (r.sessions) state.sessions = r.sessions;
  renderSessions();
  renderMessages();
  if (r) renderContextBar(r.context);
}

function editUserMessage(index) {
  if (state.streaming || typeof index !== 'number') return;
  // 进入编辑前先退出该条的版本预览（否则输入框内容与气泡显示不一致）
  if (verPreview && verPreview.index === index) { verPreview = null; refreshUserRow(index); }
  const s = curSession();
  const m = s && s.messages[index];
  if (!m || m.role !== 'user') return;
  const row = $('#chat').querySelector('.msg.user[data-idx="' + index + '"]');
  if (!row || row.querySelector('.edit-box')) return;
  const wrap = row._wrap;
  const bubble = row._bubble;
  const actions = wrap ? wrap.querySelector('.msg-actions') : null;
  if (bubble) bubble.style.display = 'none';
  if (actions) actions.style.display = 'none';
  row.classList.add('editing');

  const box = document.createElement('div');
  box.className = 'edit-box';
  const ta = document.createElement('textarea');
  ta.className = 'edit-ta';
  ta.value = m.content || '';
  const btns = document.createElement('div');
  btns.className = 'edit-btns';
  const ok = document.createElement('button');
  ok.className = 'primary'; ok.textContent = '发送';
  const cancel = document.createElement('button');
  cancel.className = 'ghost'; cancel.textContent = '取消';
  btns.appendChild(ok); btns.appendChild(cancel);
  box.appendChild(ta); box.appendChild(btns);
  wrap.appendChild(box);
  ta.style.height = Math.min(ta.scrollHeight + 4, 220) + 'px';
  ta.focus();
  ta.setSelectionRange(ta.value.length, ta.value.length);

  const close = () => {
    box.remove();
    row.classList.remove('editing');
    if (bubble) bubble.style.display = '';
    if (actions) actions.style.display = '';
  };
  cancel.onclick = close;
  ta.oninput = () => { ta.style.height = 'auto'; ta.style.height = Math.min(ta.scrollHeight + 4, 220) + 'px'; };
  ok.onclick = async () => {
    const txt = ta.value.trim();
    if (!txt) { close(); return; }
    close();
    await resend({ mode: 'edit', index: index, content: txt });
  };
}

async function regenerateReply(index) {
  if (state.streaming || typeof index !== 'number') return;
  if (!hasChatKey()) { toast('请先在「设置 → 聊天 API」中填写密钥'); return; }
  await resend({ mode: 'regenerate', index: index });
}

// 只重建这一条用户消息的 DOM（不整屏重排、不滚动），供版本预览用
function refreshUserRow(index) {
  const s = curSession();
  const m = s && s.messages[index];
  if (!m) return;
  const old = $('#chat').querySelector('.msg.user[data-idx="' + index + '"]');
  if (!old) return;
  old.replaceWith(buildUserRow(m.content, index, m));
}

function previewVersion(index, vidx) {
  if (state.streaming || typeof index !== 'number') return;
  const s = curSession();
  const m = s && s.messages[index];
  if (!m || !Array.isArray(m.versions)) return;
  if (vidx < 0 || vidx >= m.versions.length) return;
  const committed = typeof m.vidx === 'number' ? m.vidx : m.versions.length - 1;
  // 切回当前版本 == 取消预览
  verPreview = vidx === committed ? null : { sid: state.sessions.current, index: index, vidx: vidx };
  refreshUserRow(index);
}

function cancelVersionPreview(index) {
  verPreview = null;
  refreshUserRow(index);
}

async function switchVersion(index, vidx) {
  if (state.streaming || typeof index !== 'number') return;
  const s = curSession();
  const m = s && s.messages[index];
  if (!m || !Array.isArray(m.versions)) return;
  if (vidx < 0 || vidx >= m.versions.length || vidx === m.vidx) return;
  verPreview = null;
  await resend({ mode: 'version', index: index, vidx: vidx });
}

async function resend(opts) {
  if (state.streaming) return;
  const s = curSession();
  if (!s) return;
  const m = s.messages[opts.index];
  // 本地先做同样的处理，界面立刻反映；服务端同样会处理
  if (opts.mode === 'edit') {
    if (!m) return;
    const vers = Array.isArray(m.versions) ? m.versions : [m.content];
    if (vers[vers.length - 1] !== opts.content) vers.push(opts.content);
    m.versions = vers;
    m.vidx = vers.length - 1;
    m.content = opts.content;
    s.messages.length = opts.index + 1;
  } else if (opts.mode === 'version') {
    if (!m || !Array.isArray(m.versions)) return;
    m.vidx = opts.vidx;
    m.content = m.versions[opts.vidx];
    s.messages.length = opts.index + 1;
  } else {
    s.messages.length = opts.index;
  }
  renderMessages();
  await streamChat({
    resend: { mode: opts.mode, index: opts.index, vidx: opts.vidx },
    content: opts.content || '',
  });
}

function toggleRawView(row) {
  if (typeof row._raw !== 'string' || !row._content) return;
  row._rawMode = !row._rawMode;
  if (row._rawMode) {
    row._content.innerHTML = '';
    const pre = document.createElement('div');
    pre.className = 'raw-text';
    pre.textContent = row._raw;
    row._content.appendChild(pre);
    const rng = document.createRange();
    rng.selectNodeContents(pre);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(rng);
    toast('已切换为源码视图，可直接复制源码');
  } else {
    row._content.innerHTML = renderMarkdown(row._raw);
  }
  if (row._selBtn) row._selBtn.textContent = row._rawMode ? '显示渲染' : '选择文本';
}

/* ============================================================
   图片（上传 / 粘贴 / 拖拽）
   ============================================================ */
function sendImageBlob(blob) {
  const reader = new FileReader();
  reader.onload = async () => {
    const mime = (reader.result.match(/^data:([^;]+);/) || [])[1] || 'image/png';
    const b64 = String(reader.result).split(',')[1] || '';
    hideEmpty();
    const row = appendAi('<p style="color:var(--text-dim)">📷 正在识别图片…</p>', '', undefined);
    try {
      const r = await api('/api/recognize', { session_id: state.sessions.current, mime, data: b64 });
      const text = r.text || '';
      row._raw = '用户上传了一张图片，图片内容为：\n' + text;
      row._content.innerHTML = renderMarkdown(text);
      row.appendChild(buildActions(row, () => row._raw, undefined, 'ai'));
      state.sessions = r.sessions;
      renderSessions();
      if (r) renderContextBar(r.context);
    } catch (e) {
      row._content.innerHTML = '<p>❌ 识别失败：' + escapeHtml(String(e)) + '</p>';
    }
    scrollToBottom(true);
  };
  reader.readAsDataURL(blob);
}

/* ============================================================
   设置弹窗
   ============================================================ */
const PROVIDER_META = {
  chat: { title: '聊天模型', desc: '用于智能聊天与记忆读写', hint: '填写任意 OpenAI 兼容接口（地址 + 模型名 + 密钥）', protocol: false },
  vision: { title: '视觉模型', desc: '用于图像识别', hint: '支持 DashScope 原生协议，或 OpenAI 兼容的视觉接口', protocol: true },
};

function buildApiPane(provider) {
  const meta = PROVIDER_META[provider];
  const cfg = state.config;
  state.pendingPresets[provider] = Object.assign({}, cfg[provider + '_presets'] || {});

  const pane = document.createElement('section');
  pane.className = 'pane hidden';
  pane.dataset.pane = provider;
  pane.innerHTML =
    '<p class="hint"><strong>' + meta.title + '</strong> —— ' + meta.desc + '<br>' + meta.hint + '</p>' +
    (provider === 'vision' ?
      '<label class="check-row"><input type="checkbox" data-role="use-chat"> 使用聊天模型识别图片（聊天模型自带视觉能力时）</label>' +
      '<p class="hint">开启后不再使用下面的视觉 API，改由「聊天 API」的模型与密钥识别图片（需聊天模型支持视觉，如 GPT-4o / deepseek-flash / 通义兼容模式）。</p>' : '') +
    (meta.protocol ? '<label class="field-label">接口协议</label>' +
      '<div class="radio-row">' +
      '<label><input type="radio" name="' + provider + '-proto" value="native"> DashScope 原生</label>' +
      '<label><input type="radio" name="' + provider + '-proto" value="openai"> OpenAI 兼容</label>' +
      '</div><p class="hint">DashScope 原生：图片放 input.messages[].content，结果取 output.choices[0].message.content（qwen-vl-plus / qwen-vl-max）<br>' +
      'OpenAI 兼容：图片放 messages[].content（type=image_url），结果取 choices[0].message.content（GPT-4o、vLLM/Ollama、deepseek-flash）</p>' : '') +
    (provider === 'chat' ? '<label class="field-label">思考参数模式</label>' +
      '<select class="field-input" data-role="think-mode">' +
      '<option value="">自动（按接口地址判断）</option>' +
      '<option value="off">不发送</option>' +
      '<option value="openai">OpenAI 兼容（只发 reasoning_effort）</option>' +
      '<option value="deepseek">DeepSeek（thinking + reasoning_effort）</option>' +
      '</select>' +
      '<p class="hint">控制「深度思考」开关如何传给 API。<br>' +
      'DeepSeek：开→ <code>thinking.enabled</code>，关→ <code>thinking.disabled</code>；档位走 <code>reasoning_effort</code>（low/medium 会被 DeepSeek 映射为 high）。<br>' +
      'OpenAI 兼容：开→ 发 <code>reasoning_effort</code>，关→ 不发（该协议无法表达「禁用」）。</p>' : '') +
    (provider === 'chat' ?
      '<label class="field-label">上下文上限（tokens）</label>' +
      '<input class="field-input" type="number" min="1000" step="1000" data-role="ctx-limit">' +
      '<label class="field-label">为回复预留（tokens）</label>' +
      '<input class="field-input" type="number" min="0" step="256" data-role="ctx-reserve">' +
      '<p class="hint">用于估算当前会话的「对话上限」占用。<br>' +
      '① 单位换算：1 个汉字 / 中文标点 ≈ 0.6 token，1 个英文字符 ≈ 0.3 token；即 1000 token ≈ 1700 汉字，' +
      '128000 ≈ 21 万汉字，1000000 ≈ 170 万汉字。<strong>只填数字</strong>：128K 填 <code>128000</code>，1M 填 <code>1000000</code>。<br>' +
      '② 已用 ≈ 本地估算 × 校准系数；校准系数 = 最近一次 API 返回的真实 ' +
      '<code>prompt_tokens</code> ÷ 当时的本地估算，自动修正误差。<br>' +
      '③ 剩余 = 上限 − 已用；剩余低于上限 10% / 5% 时进度条转黄/红并弹出提醒。<br>' +
      '④ <strong>达到上限时，AI 会自动压缩你的对话历史记录</strong>：把较早的对话总结成摘要（不是删除），' +
      '保留最近 8 条原文后再发送；也可点输入框上方进度条旁的「压缩历史」手动触发。<br>' +
      '请按所用模型的上下文窗口填写上限（默认 128000 / 4096）；估算为近似值，仅供参考。</p>' : '') +
    '<label class="field-label">预设</label>' +
    '<div class="preset-row"><select data-role="preset"></select>' +
    '<button data-role="save-preset">另存为预设</button>' +
    '<button data-role="del-preset">删除预设</button></div>' +
    '<label class="field-label">接口地址</label>' +
    '<input class="field-input" type="text" data-role="url">' +
    '<label class="field-label">模型名</label>' +
    '<input class="field-input" type="text" data-role="model">' +
    '<label class="field-label">API 密钥</label>' +
    '<input class="field-input" type="password" data-role="key">' +
    '<label class="check-row"><input type="checkbox" data-role="show-key"> 显示密钥</label>' +
    '<label class="field-label">账户余额</label>' +
    '<div class="balance-row"><span class="balance-text" data-role="balance">未查询</span>' +
    '<button type="button" class="ghost" data-role="balance-refresh">刷新</button></div>' +
    '<label class="field-label">余额查询地址（可选；留空时按接口地址自动识别 DeepSeek）</label>' +
    '<input class="field-input" type="text" data-role="balance-url" placeholder="https://api.deepseek.com/user/balance">';

  const q = (r) => pane.querySelector('[data-role="' + r + '"]');
  q('url').value = cfg[provider + '_url'] || '';
  q('model').value = cfg[provider + '_model'] || '';
  q('key').value = cfg[provider + '_api_key'] || '';
  if (meta.protocol) {
    const val = cfg[provider + '_protocol'] || (provider === 'vision' ? 'native' : 'openai');
    const radio = pane.querySelector('input[name="' + provider + '-proto"][value="' + val + '"]');
    if (radio) radio.checked = true;
  }
  q('show-key').onchange = (e) => { q('key').type = e.target.checked ? 'text' : 'password'; };
  q('balance-url').value = cfg[provider + '_balance_url'] || '';
  q('balance-refresh').onclick = () => refreshBalance(provider, pane);
  if (provider === 'vision') {
    q('use-chat').checked = !!cfg.vision_use_chat;
    q('use-chat').onchange = () => setVisionFieldsDisabled(pane, q('use-chat').checked);
    setVisionFieldsDisabled(pane, q('use-chat').checked);
  }
  if (provider === 'chat') {
    q('think-mode').value = cfg.chat_think_mode || '';
    q('ctx-limit').value = cfg.chat_context_limit != null ? cfg.chat_context_limit : 128000;
    q('ctx-reserve').value = cfg.chat_reserve_output != null ? cfg.chat_reserve_output : 4096;
  }

  const refreshPresetSelect = (selected) => {
    const sel = q('preset');
    sel.innerHTML = '<option value="">（未选择）</option>';
    Object.keys(state.pendingPresets[provider]).forEach((name) => {
      const o = document.createElement('option');
      o.value = name; o.textContent = name;
      if (name === selected) o.selected = true;
      sel.appendChild(o);
    });
  };
  refreshPresetSelect('');

  q('preset').onchange = (e) => {
    const p = state.pendingPresets[provider][e.target.value];
    if (!p) return;
    q('url').value = p.url || '';
    q('model').value = p.model || '';
    q('key').value = p.api_key || '';
    if (provider === 'chat') {
      if (p.context_limit != null) q('ctx-limit').value = p.context_limit;
      if (p.reserve_output != null) q('ctx-reserve').value = p.reserve_output;
    }
    if (meta.protocol && p.protocol) {
      const r = pane.querySelector('input[name="' + provider + '-proto"][value="' + p.protocol + '"]');
      if (r) r.checked = true;
    }
  };

  q('save-preset').onclick = () => {
    const name = prompt('预设名称');
    if (!name || !name.trim()) return;
    const n = name.trim();
    state.pendingPresets[provider][n] = {
      url: q('url').value.trim(), model: q('model').value.trim(), api_key: q('key').value.trim(),
    };
    if (provider === 'chat') {
      state.pendingPresets[provider][n].context_limit = parseInt(q('ctx-limit').value, 10) || 128000;
      state.pendingPresets[provider][n].reserve_output = parseInt(q('ctx-reserve').value, 10) || 0;
    }
    if (meta.protocol) {
      const r = pane.querySelector('input[name="' + provider + '-proto"]:checked');
      state.pendingPresets[provider][n].protocol = r ? r.value : '';
    }
    refreshPresetSelect(n);
    toast('预设定为「' + n + '」，点保存生效');
  };

  q('del-preset').onclick = () => {
    const name = q('preset').value;
    if (!name) { toast('先选择要删除的预设'); return; }
    delete state.pendingPresets[provider][name];
    refreshPresetSelect('');
  };

  pane._collect = () => {
    const out = {};
    out[provider + '_url'] = q('url').value.trim();
    out[provider + '_model'] = q('model').value.trim();
    out[provider + '_api_key'] = q('key').value.trim();
    out[provider + '_presets'] = state.pendingPresets[provider];
    if (meta.protocol) {
      const r = pane.querySelector('input[name="' + provider + '-proto"]:checked');
      out[provider + '_protocol'] = r ? r.value : '';
    }
    out[provider + '_balance_url'] = q('balance-url').value.trim();
    if (provider === 'chat') {
      out.chat_think_mode = q('think-mode').value;
      out.chat_context_limit = parseInt(q('ctx-limit').value, 10) || 128000;
      out.chat_reserve_output = parseInt(q('ctx-reserve').value, 10) || 0;
    } else {
      out.vision_use_chat = !!q('use-chat').checked;
    }
    return out;
  };
  return pane;
}

/* ---------------- 余额 / 视觉开关辅助 ---------------- */
async function refreshBalance(provider, pane) {
  const el = pane.querySelector('[data-role="balance"]');
  if (!el) return;
  el.textContent = '查询中…';
  try {
    const r = await api('/api/balance', { provider });
    el.textContent = r.ok ? formatBalance(r) : (r.error || '查询失败');
  } catch (e) {
    el.textContent = '查询失败';
  }
}

function formatBalance(r) {
  if (!r.items || !r.items.length) {
    return '已返回数据（未识别字段）：' + JSON.stringify(r.raw || {}).slice(0, 120);
  }
  const parts = r.items.map((it) => {
    const bits = [];
    if (it.granted) bits.push('赠金 ' + it.granted);
    if (it.topped) bits.push('充值 ' + it.topped);
    return (it.label ? it.label + ' ' : '') + (it.total || '') + (bits.length ? '（' + bits.join(' / ') + '）' : '');
  });
  let txt = parts.join('；');
  if (r.is_available === false) txt += ' · ⚠️ 余额不足';
  return txt;
}

function setVisionFieldsDisabled(pane, disabled) {
  ['preset', 'save-preset', 'del-preset', 'url', 'model', 'key', 'show-key'].forEach((r) => {
    const el = pane.querySelector('[data-role="' + r + '"]');
    if (el) el.disabled = disabled;
  });
  pane.querySelectorAll('input[name="vision-proto"]').forEach((el) => { el.disabled = disabled; });
}

function buildSearchPane() {
  const cfg = state.config || {};
  const providers = state.searchProviders || {};
  const pane = document.createElement('section');
  pane.className = 'pane hidden';
  pane.dataset.pane = 'search';

  let opts = '';
  Object.keys(providers).forEach((k) => {
    opts += '<option value="' + k + '">' + providers[k].name + '</option>';
  });

  pane.innerHTML =
    '<p class="hint">开启输入框里的「🔍 联网搜索」后，会先把你的问题改写成多个检索式并行搜索，必要时抓取网页正文，再把结果作为参考资料交给模型回答，并展示引用来源。</p>' +
    '<label class="field-label">服务商</label>' +
    '<select class="field-input" data-role="provider">' + opts + '</select>' +
    '<p class="hint" data-role="hint"></p>' +
    '<label class="field-label">接口地址</label>' +
    '<input class="field-input" type="text" data-role="url">' +
    '<label class="field-label">API 密钥</label>' +
    '<input class="field-input" type="password" data-role="key">' +
    '<label class="check-row"><input type="checkbox" data-role="show-key"> 显示密钥</label>' +
    '<label class="field-label">结果条数（1–20，每个检索式）</label>' +
    '<input class="field-input" type="number" min="1" max="20" data-role="count">' +
    '<label class="check-row"><input type="checkbox" data-role="rewrite"> 查询改写（用聊天模型把问题转成 2–3 个检索式，更准）</label>' +
    '<label class="check-row"><input type="checkbox" data-role="fetch"> 抓取正文（对命中网页取正文，而不是只有摘要）</label>' +
    '<label class="field-label">抓取正文条数（1–10）</label>' +
    '<input class="field-input" type="number" min="1" max="10" data-role="fetch-count">';

  const q = (r) => pane.querySelector('[data-role="' + r + '"]');
  q('provider').value = cfg.search_provider || 'bocha';
  q('url').value = cfg.search_url || '';
  q('key').value = cfg.search_api_key || '';
  q('count').value = cfg.search_count || 5;
  q('rewrite').checked = cfg.search_rewrite !== false;
  q('fetch').checked = cfg.search_fetch !== false;
  q('fetch-count').value = cfg.search_fetch_count || 4;

  const syncHint = () => {
    const p = providers[q('provider').value];
    q('hint').textContent = p ? p.hint : '';
    q('url').placeholder = p ? ('留空则使用 ' + p.url) : '留空则使用默认地址';
  };
  syncHint();
  q('provider').onchange = syncHint;
  q('show-key').onchange = (e) => { q('key').type = e.target.checked ? 'text' : 'password'; };

  const syncFetch = () => { q('fetch-count').disabled = !q('fetch').checked; };
  q('fetch').onchange = syncFetch;
  syncFetch();

  pane._collect = () => ({
    search_provider: q('provider').value,
    search_url: q('url').value.trim(),
    search_api_key: q('key').value.trim(),
    search_count: parseInt(q('count').value, 10) || 5,
    search_rewrite: q('rewrite').checked,
    search_fetch: q('fetch').checked,
    search_fetch_count: parseInt(q('fetch-count').value, 10) || 4,
  });
  return pane;
}

/* ============================================================
   Token 用量统计
   ============================================================ */
let usageChart = null;

const USAGE_RANGES = {
  day7: { mode: 'day', span: 7 },
  day30: { mode: 'day', span: 30 },
  month12: { mode: 'month', span: 12 },
};

const fmtNum = (n) => (n || 0).toLocaleString('en-US');

async function openUsage(rangeKey) {
  const key = USAGE_RANGES[rangeKey] ? rangeKey : 'day7';
  const r = USAGE_RANGES[key];
  showWithAnim($('#usage-modal'));
  $$('#usage-modal .tab').forEach((t) => t.classList.toggle('active', t.dataset.usageRange === key));
  $('#usage-cards').innerHTML = '';
  $('#usage-tables').innerHTML = '<p class="usage-empty">加载中…</p>';
  try {
    const res = await fetch('/api/usage?mode=' + r.mode + '&span=' + r.span,
      { headers: { 'X-Auth-Token': TOKEN } });
    renderUsage(await res.json());
  } catch (e) {
    $('#usage-tables').innerHTML = '<p class="usage-empty">加载失败：' + escapeHtml(String(e)) + '</p>';
  }
}

function closeUsage() {
  hideWithAnim($('#usage-modal'), () => {
    if (usageChart) { usageChart.dispose(); usageChart = null; }
  });
}

function renderUsage(data) {
  const s = data.summary || {};
  const cards = [
    ['总 tokens', fmtNum(s.total)],
    ['输入', fmtNum(s.prompt)],
    ['输出', fmtNum(s.completion)],
    ['缓存命中', fmtNum(s.cached)],
    ['调用次数', fmtNum(s.calls)],
  ];
  $('#usage-cards').innerHTML = cards.map(([l, v]) =>
    '<div class="usage-card"><div class="uc-label">' + l + '</div>' +
    '<div class="uc-value">' + v + '</div></div>').join('');

  const byModel = data.by_model || [];
  const rows = data.rows || [];
  const MAX_ROWS = 200;
  const head = (cols) => '<tr>' + cols.map((c) => '<th>' + c + '</th>').join('') + '</tr>';
  const modelCols = ['模型', '输入', '输出', '合计', '缓存命中', '次数'];
  const rowCols = [data.mode === 'month' ? '月份' : '日期'].concat(modelCols);
  const tr = (cells) => '<tr>' + cells.map((c) => '<td>' + c + '</td>').join('') + '</tr>';

  let html = '<h3>按模型汇总</h3>';
  html += byModel.length
    ? '<table><thead>' + head(modelCols) + '</thead><tbody>' +
      byModel.map((r) => tr([escapeHtml(r.model), fmtNum(r.prompt), fmtNum(r.completion),
        fmtNum(r.total), fmtNum(r.cached), fmtNum(r.calls)])).join('') + '</tbody></table>'
    : '<p class="usage-empty">该时间段暂无用量记录</p>';

  html += '<h3>按' + (data.mode === 'month' ? '月份' : '日期') + '明细</h3>';
  html += rows.length
    ? '<table><thead>' + head(rowCols) + '</thead><tbody>' +
      rows.slice(0, MAX_ROWS).map((r) => tr([r.bucket, escapeHtml(r.model), fmtNum(r.prompt),
        fmtNum(r.completion), fmtNum(r.total), fmtNum(r.cached), fmtNum(r.calls)])).join('') +
      '</tbody></table>' +
      (rows.length > MAX_ROWS
        ? '<p class="usage-empty">仅显示前 ' + MAX_ROWS + ' 行（共 ' + rows.length + ' 行）</p>' : '')
    : '<p class="usage-empty">该时间段暂无用量记录</p>';

  $('#usage-tables').innerHTML = html;

  // 图表放在最后渲染：表格插入后 .modal-body 才可能出现滚动条，
  // 滚动条会压缩容器宽度；若先画图，canvas 会比可视区宽，右侧标签被裁掉。
  drawUsageChart(data);
}

function drawUsageChart(data) {
  const el = $('#usage-chart');
  const labels = data.labels || [];
  const models = data.models || [];
  if (!window.echarts) {
    el.innerHTML = '<p class="usage-empty">图表库未加载（需联网加载 ECharts），下方表格仍可查看明细。</p>';
    return;
  }
  el.innerHTML = '';
  if (usageChart) { usageChart.dispose(); usageChart = null; }
  const dark = document.documentElement.getAttribute('data-theme') === 'dark';
  const axisColor = dark ? '#a3a3ad' : '#6b6b76';
  const lineColor = dark ? '#333336' : '#e6e6ea';
  usageChart = echarts.init(el);
  usageChart.setOption({
    tooltip: { trigger: 'axis', valueFormatter: (v) => Number(v).toLocaleString('en-US') },
    legend: models.length ? { data: models, bottom: 0, textStyle: { color: axisColor } } : undefined,
    // containLabel: 让 ECharts 为轴标签预留空间；
    // right 再留出余量，避免最右侧标签（如 2026-10-09）贴边被裁
    grid: {
      left: 8, right: 40, top: 18, bottom: models.length ? 46 : 26,
      containLabel: true,
    },
    xAxis: {
      type: 'category', data: labels, boundaryGap: false,
      axisLabel: { color: axisColor, hideOverlap: true },
      axisLine: { lineStyle: { color: lineColor } },
    },
    yAxis: { type: 'value', axisLabel: { color: axisColor }, splitLine: { lineStyle: { color: lineColor } } },
    series: models.map((m) => ({
      name: m, type: 'line', smooth: true, symbol: 'circle', symbolSize: 5,
      data: labels.map((k) => ((data.series || {})[m] || {})[k] || 0),
      areaStyle: { opacity: 0.08 },
    })),
  });
  // 容器尺寸可能在 init 之后才最终确定（弹窗刚显示、出现滚动条等），
  // 再同步一次，避免 canvas 比容器宽导致右侧标签跑出可视区
  requestAnimationFrame(() => { if (usageChart) usageChart.resize(); });
}

function buildLearningPresetSelect() {
  const builtin = state.builtinPresets || {};
  if (!state.config.learning_presets) state.config.learning_presets = {};
  const userPresets = state.config.learning_presets;
  const sel = $('#learn-preset');
  sel.innerHTML = '';

  const add = (value, label) => {
    const o = document.createElement('option');
    o.value = value;
    o.textContent = label;
    sel.appendChild(o);
    return o;
  };
  add('', state.builtinPresetLabel || '默认（内置）');
  Object.keys(builtin).forEach((n) => add('builtin:' + n, '内置 · ' + n));
  Object.keys(userPresets).forEach((n) => add('user:' + n, n));

  // 自动识别当前文本对应哪个预设
  const cur = $('#pf-prompt').value.trim();
  let match = '';
  if (cur) {
    Object.keys(builtin).forEach((n) => { if (!match && builtin[n].trim() === cur) match = 'builtin:' + n; });
    Object.keys(userPresets).forEach((n) => { if (!match && userPresets[n].trim() === cur) match = 'user:' + n; });
    if (!match) {
      add('__custom__', '当前内容（未保存为预设）');
      match = '__custom__';
    }
  }
  sel.value = match;

  sel.onchange = () => {
    const v = sel.value;
    if (v === '__custom__') return;
    if (!v) { $('#pf-prompt').value = ''; return; }
    const kind = v.split(':')[0];
    const name = v.slice(kind.length + 1);
    $('#pf-prompt').value = (kind === 'builtin' ? builtin[name] : userPresets[name]) || '';
  };

  $('#btn-save-learn-preset').onclick = () => {
    const text = $('#pf-prompt').value.trim();
    if (!text) { toast('学习指令为空，无法保存为预设'); return; }
    const name = prompt('预设名称');
    if (!name || !name.trim()) return;
    const n = name.trim();
    userPresets[n] = text;
    const existing = Array.from(sel.options).find((o) => o.value === 'user:' + n);
    if (existing) existing.textContent = n;
    else add('user:' + n, n);
    sel.value = 'user:' + n;
    toast('预设「' + n + '」已加入，点「保存」生效');
  };

  $('#btn-del-learn-preset').onclick = () => {
    const v = sel.value;
    if (!v || v.indexOf('user:') !== 0) { toast('内置预设不可删除'); return; }
    const n = v.slice(5);
    delete userPresets[n];
    const opt = Array.from(sel.options).find((o) => o.value === v);
    if (opt) opt.remove();
    sel.value = '';
    toast('已删除，点「保存」生效');
  };
}

/* 浮层显隐：退场先播动画，animationend（或兜底超时）后再真正 display:none，
   避免关闭时「啪」地消失 */
function hideWithAnim(el, done) {
  if (!el || el.classList.contains('hidden')) { if (done) done(); return; }
  let fired = false;
  const finish = () => {
    if (fired) return;
    fired = true;
    el.removeEventListener('animationend', finish);
    el.classList.remove('closing');
    el.classList.add('hidden');
    if (done) done();
  };
  el.classList.add('closing');
  el.addEventListener('animationend', finish);
  setTimeout(finish, 300);   // 兜底：动画被禁用或被中断时也能收起
}
function showWithAnim(el) {
  if (!el) return;
  el.classList.remove('closing', 'hidden');
}
// 是否处于「打开」状态（退场动画进行中也算已关闭）
function isOpen(el) {
  return !!el && !el.classList.contains('hidden') && !el.classList.contains('closing');
}
// 内容区重新淡入（遮住 vis / ECharts 初始化时的空白）
function replayFadeIn(el) {
  if (!el) return;
  el.classList.remove('fade-in');
  void el.offsetWidth;
  el.classList.add('fade-in');
}

function openModal() {
  const body = $('.modal-body');
  // 清掉旧的动态面板
  $$('[data-pane="chat"],[data-pane="vision"],[data-pane="search"]').forEach((el) => el.remove());
  const anchor = body.querySelector('[data-pane="profile"]');
  const chatPane = buildApiPane('chat');
  const visionPane = buildApiPane('vision');
  body.insertBefore(chatPane, anchor);
  body.insertBefore(visionPane, anchor);
  body.insertBefore(buildSearchPane(), anchor);

  // 打开设置时自动查一次余额（不阻塞界面）
  refreshBalance('chat', chatPane);
  refreshBalance('vision', visionPane);

  const p = state.profile || {};
  $('#pf-manual').value = p.manual || '';
  $('#pf-auto-learn').checked = p.auto_learn_enabled !== false;
  $('#pf-prompt').value = p.learning_prompt_custom || '';
  buildLearningPresetSelect();

  const theme = (state.config && state.config.theme) || 'light';
  const radio = document.querySelector('input[name="theme"][value="' + theme + '"]');
  if (radio) radio.checked = true;

  $$('#modal .modal-tabs .tab').forEach((t, i) => t.classList.toggle('active', i === 0));
  $$('#modal .pane').forEach((pn) => pn.classList.toggle('hidden', pn.dataset.pane !== 'chat'));
  showWithAnim($('#modal'));
}

function closeModal() { hideWithAnim($('#modal')); }

async function saveModal() {
  const cfg = Object.assign({}, state.config);
  $$('[data-pane="chat"],[data-pane="vision"],[data-pane="search"]').forEach((pane) => {
    if (pane._collect) Object.assign(cfg, pane._collect());
  });
  const tr = document.querySelector('input[name="theme"]:checked');
  if (tr) cfg.theme = tr.value;

  const profile = {
    manual: $('#pf-manual').value,
    auto_learn_enabled: $('#pf-auto-learn').checked,
    learning_prompt_custom: $('#pf-prompt').value,
  };

  await api('/api/config', cfg);
  await api('/api/profile', profile);
  state.config = cfg;
  // 密钥被清空时，顺带关掉「联网搜索」开关
  if (state.searchEnabled && !hasSearchKey()) {
    state.searchEnabled = false;
    api('/api/config', { search_enabled: false }).then(() => { state.config.search_enabled = false; });
  }
  syncSearchPill();
  state.profile = profile;
  applyTheme(cfg.theme);
  updateStatusLine();
  closeModal();
  toast('已保存');
}

/* ============================================================
   主题
   ============================================================ */
function applyTheme(theme) {
  const t = theme === 'dark' ? 'dark' : 'light';
  const root = document.documentElement;
  // 只在切换瞬间挂一次过渡类，避免长期挂着全局 transition 影响性能
  root.classList.add('theme-anim');
  clearTimeout(root._themeTimer);
  root._themeTimer = setTimeout(() => root.classList.remove('theme-anim'), 260);
  root.setAttribute('data-theme', t);
  const light = document.getElementById('hljs-light');
  const dark = document.getElementById('hljs-dark');
  if (light) { light.disabled = (t === 'dark'); light.media = 'all'; }
  if (dark) { dark.disabled = (t !== 'dark'); dark.media = 'all'; }
}

/* ============================================================
   输入框自适应
   ============================================================ */
function autoGrow() {
  const el = $('#input');
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 190) + 'px';
}

/* ============================================================
   知识库 / 记忆图谱（vis-network）
   ============================================================ */
let kbNetwork = null;
let kbEdgeFrom = null;   // 「新增连线」的起点节点 id
let kbSaveTimer = null;
let kbLoadSeq = 0;      // 加载代次，防止异步竞态

function kbMeta(id) {
  return (state.kbs || []).find((k) => k.id === id) || null;
}

function profileKbId() {
  const k = (state.kbs || []).find((x) => x.name === '用户画像');
  return k ? k.id : null;
}

function applySessions(r) {
  if (!r) return;
  if (r.sessions) state.sessions = r.sessions;
  renderSessions();
  if ('context' in r) renderContextBar(r.context);
}

function refreshKbSelect(selected) {
  const sel = $('#kb-select');
  const s = curSession();
  const bound = (s && s.kb_ids) || [];
  let html = '<option value="session">本会话记忆' +
    (s && s.ai_write === false ? '（AI 写入已关）' : '') + '</option>';
  (state.kbs || []).forEach((k) => {
    const writable = bound.includes(k.id) && k.ai_write !== false;
    html += '<option value="' + k.id + '">' + escapeHtml(k.name) +
      (k.name === '用户画像' ? '（AI 自主记录）' : '') +
      (writable ? ' ⚡AI 写入' : '') + '</option>';
  });
  sel.innerHTML = html;
  sel.value = selected || state.kbCurrent || 'session';
  renderKbAiTarget();
}

// 明确标出 AI 每轮实际会写入哪些目标，避免"看错地方"
function renderKbAiTarget() {
  const el = $('#kb-ai-target');
  if (!el) return;
  const s = curSession();
  const names = [];
  if (s && s.ai_write !== false) names.push('本会话记忆');
  ((s && s.kb_ids) || []).forEach((id) => {
    const m = kbMeta(id);
    if (m && m.ai_write !== false) names.push(m.name);
  });
  const pm = kbMeta(profileKbId());
  const autoOn = !!(state.profile && state.profile.auto_learn_enabled);
  if (pm && autoOn && names.indexOf(pm.name) < 0) names.push(pm.name);
  el.textContent = names.length
    ? 'AI 每轮会写入：' + names.join('、')
    : 'AI 写入已全部关闭';
  el.classList.toggle('off', !names.length);
}

async function openKbModal(initialKey) {
  const key = initialKey || state.kbCurrent || 'session';
  refreshKbSelect(key);
  renderKbBindList();
  const s = curSession();
  $('#sess-ai-write').checked = !s || s.ai_write !== false;
  showWithAnim($('#kb-modal'));
  replayFadeIn($('#kb-graph'));
  await loadKbGraph(key);
}

function closeKbModal() {
  scheduleKbSave();
  hideWithAnim($('#kb-modal'));
}

function renderKbBindList() {
  const box = $('#kb-bind-list');
  const s = curSession();
  const bound = (s && s.kb_ids) || [];
  const pid = profileKbId();
  const list = (state.kbs || []).filter((k) => k.id !== pid);
  box.innerHTML = list.length
    ? list.map((k) => '<label class="check-row"><input type="checkbox" data-kbid="' + k.id + '"' +
        (bound.includes(k.id) ? ' checked' : '') + '> ' + escapeHtml(k.name) + '</label>').join('')
    : '<div class="hint">还没有通用知识库，点上方「＋ 新建」创建一个。</div>';
  box.querySelectorAll('input[data-kbid]').forEach((cb) => {
    cb.onchange = async () => {
      const ids = Array.from(box.querySelectorAll('input[data-kbid]:checked')).map((x) => x.dataset.kbid);
      applySessions(await api('/api/session/bind', { id: state.sessions.current, kb_ids: ids }));
      // 绑定变化会改变 AI 的写入目标，刷新下拉标注与提示行
      refreshKbSelect($('#kb-select').value || state.kbCurrent);
    };
  });
  renderKbAiTarget();
}

async function loadKbGraph(key) {
  clearTimeout(kbSaveTimer);
  const token = ++kbLoadSeq;      // 防止快速切换时旧请求覆盖新结果
  state.kbCurrent = key;
  state.kbData = { nodes: [], edges: [] };
  state.kbError = null;
  showNodeForm(null);
  let data = null;
  try {
    if (key === 'session') {
      const r = await api('/api/memory/get', { session_id: state.sessions.current });
      if (token !== kbLoadSeq) return;
      if (!r.ok) state.kbError = '会话记忆读取失败：' + (r.error || '');
      else {
        data = { nodes: r.memory.nodes || [], edges: r.memory.edges || [] };
        $('#kb-ai-write').checked = r.ai_write !== false;
      }
      $('#kb-ai-write').disabled = true;
      $('#kb-rename').disabled = true;
      $('#kb-del').disabled = true;
    } else {
      const r = await api('/api/kb/get', { id: key });
      if (token !== kbLoadSeq) return;
      if (!r.ok) state.kbError = '知识库读取失败：' + (r.error || '');
      else {
        data = { nodes: r.kb.nodes || [], edges: r.kb.edges || [] };
        const meta = kbMeta(key);
        const isProfile = !!meta && meta.name === '用户画像';
        $('#kb-ai-write').checked = meta ? meta.ai_write !== false : true;
        $('#kb-rename').disabled = isProfile;   // 系统库不可重命名/删除
        $('#kb-del').disabled = isProfile;
      }
      $('#kb-ai-write').disabled = false;
    }
  } catch (err) {
    if (token !== kbLoadSeq) return;
    state.kbError = '知识库加载失败：' + (err && err.message ? err.message : String(err));
  }
  // 无论成功失败都要落到画布上，避免"数据写进去了但界面一片空白"
  state.kbData = data || { nodes: [], edges: [] };
  drawKbGraph();
}

// vis-network 的语言表（知识库图谱 / 笔记图谱共用）。
// 注意：locales 是「整体替换」内置表，且 vis 内部会回退读取 locales["en"]["close"]，
// 所以 en 与 cn 都必须给全，否则初始化时抛 "Cannot read properties of undefined (reading 'close')"。
const VIS_LOCALES = {
  en: {
    edit: 'Edit', del: 'Delete selected', back: 'Back',
    addNode: 'Add Node', addEdge: 'Add Edge', editNode: 'Edit Node', editEdge: 'Edit Edge',
    addDescription: 'Click in an empty space to place a new node.',
    edgeDescription: 'Click on a node and drag the edge to another node to connect them.',
    editEdgeDescription: 'Click on the control points and drag them to a node to connect to it.',
    createEdgeError: 'Cannot link edges to a cluster.',
    deleteClusterError: 'Clusters cannot be deleted.',
    editClusterError: 'Clusters cannot be edited.',
    close: 'Close',
  },
  cn: {
    edit: '编辑', del: '删除所选', back: '返回',
    addNode: '添加节点', addEdge: '添加连线', editNode: '编辑节点', editEdge: '编辑连线',
    addDescription: '单击空白处放置新节点。',
    edgeDescription: '按住鼠标从一个节点拖到另一个节点即可连线。',
    editEdgeDescription: '拖动控制点到另一个节点以改接连线。',
    createEdgeError: '无法把连线连接到边。',
    deleteClusterError: '无法删除群组。',
    editClusterError: '无法编辑群组。',
    close: '关闭',
  },
};

/* 关系类型：颜色 + 取词（词表本体由后端 /api/state 下发，避免两处维护） */
const RELATION_COLORS = {
  related: '#8a8a94', isa: '#4d6bfe', partof: '#12a594', attribute: '#8e5cf7',
  same: '#2ea043', opposite: '#e5534b', instance: '#d9822b', cause: '#c2410c',
  condition: '#0d8fd9', sequence: '#0891b2', means: '#16a34a', purpose: '#7c3aed',
  turn: '#a16207',
};

function relationColor(code) {
  return RELATION_COLORS[code] || '#8a8a94';
}

function relationMeta(code) {
  return (state.relations || []).find((r) => r.code === code) || null;
}

function relationLabel(code) {
  const m = relationMeta(code);
  return m ? m.label : (code || '相关');
}

function renderRelationHelp() {
  const box = $('#help-relations');
  if (!box) return;
  const list = state.relations || [];
  box.innerHTML = list.length
    ? list.map((r) => '<li><span style="color:' + relationColor(r.code) + '">●</span> ' +
        '<strong>' + escapeHtml(r.label) + '</strong>（' + escapeHtml(r.code) + '）：' +
        escapeHtml(r.desc) + '</li>').join('')
    : '<li class="hint">关系词表由后端下发，加载后显示。</li>';
}

function fillRelationSelect(sel, selected) {
  const list = state.relations || [];
  sel.innerHTML = list.length
    ? list.map((r) => '<option value="' + escapeHtml(r.code) + '">' + escapeHtml(r.label) +
        ' － ' + escapeHtml(r.desc) + '</option>').join('')
    : '<option value="related">相关</option>';
  sel.value = selected || 'related';
  if (!sel.value) sel.value = 'related';
}

function drawKbGraph() {
  const el = $('#kb-graph');
  // 销毁旧实例：否则多个 vis.Network 会共用容器，出现"上一个库的节点还在"的残影
  if (kbNetwork) { try { kbNetwork.destroy(); } catch (e) { } kbNetwork = null; }
  if (!state.kbData) { el.innerHTML = ''; return; }
  const nodesData = state.kbData.nodes || [];
  const edgesData = state.kbData.edges || [];

  // 兜底视图：图谱库缺失或渲染异常时，用列表把节点显示出来（仍可点选编辑），
  // 避免"数据写进去了但界面一片空白"。
  const fallback = (msg) => {
    el.innerHTML = '<div class="kb-fallback"><p class="hint">' + escapeHtml(msg) + '</p>' +
      (nodesData.length
        ? '<ul class="kb-flist">' + nodesData.map((n, i) =>
            '<li data-i="' + i + '"><b>' + escapeHtml(String(n.title || '(无标题)')) + '</b>' +
            (n.category ? '<span class="kb-fcat">' + escapeHtml(String(n.category)) + '</span>' : '') +
            (n.content ? '<div class="kb-fcontent">' + escapeHtml(String(n.content)) + '</div>' : '') +
            '</li>').join('') + '</ul>'
        : '<p class="hint">（暂无节点，点上方「＋ 节点」新增）</p>') + '</div>';
    el.querySelectorAll('li[data-i]').forEach((li) => {
      li.onclick = () => showNodeForm(nodesData[Number(li.dataset.i)]);
    });
  };

  if (state.kbError) { fallback(state.kbError); return; }
  if (!window.vis || !window.vis.Network || !window.vis.DataSet) {
    fallback('图谱库 vis-network 未加载（需联网）；已切换为列表视图，可正常查看与编辑。');
    return;
  }
  el.innerHTML = '';
  const dark = document.documentElement.getAttribute('data-theme') === 'dark';
  const fontColor = dark ? '#e5e5e5' : '#171717';
  const lineColor = dark ? '#3a3a3a' : '#dedee2';
  const nodes = nodesData.map((n) => {
    const o = {
      id: n.id, label: String(n.title || ''), title: String(n.content || '').slice(0, 200),
      shape: 'dot', size: 16,
      font: { color: fontColor, size: 14 },
      color: { background: dark ? '#2a2a2a' : '#ffffff', border: lineColor },
    };
    if (typeof n.x === 'number' && typeof n.y === 'number') { o.x = n.x; o.y = n.y; }
    return o;
  });
  const edges = (state.kbData.edges || []).map((e) => {
    const rel = e.relation || 'related';
    const col = relationColor(rel);
    return {
      id: e.id, from: e.from, to: e.to,
      label: e.label || relationLabel(rel),
      font: { color: col, size: 11, align: 'middle' },
      color: { color: col, highlight: col },
      arrows: { to: { enabled: true, scaleFactor: 0.55 } },
      width: 1.4,
    };
  });
  try {
    const ds = { nodes: new vis.DataSet(nodes), edges: new vis.DataSet(edges) };
    kbNetwork = new vis.Network(el, ds, {
    physics: { stabilization: true, barnesHut: { gravitationalConstant: -9000, springLength: 130 } },
    interaction: { hover: true },
    locale: 'cn',
    locales: VIS_LOCALES,
    manipulation: {
      enabled: true, initiallyActive: false, addNode: false, editNode: false,
      addEdge: (data, cb) => {
        const id = 'e_' + Math.random().toString(36).slice(2, 10);
        const rel = 'related';
        const label = relationLabel(rel);
        state.kbData.edges.push({ id, from: data.from, to: data.to,
                                  label: label, relation: rel, type: 'relation' });
        cb({ id, from: data.from, to: data.to, label: label,
             font: { color: relationColor(rel), size: 11, align: 'middle' },
             color: { color: relationColor(rel) },
             arrows: { to: { enabled: true, scaleFactor: 0.55 } }, width: 1.4 });
        scheduleKbSave();
      },
      editEdge: (data, cb) => { cb(data); },
      deleteEdge: (data, cb) => {
        (data.edges || []).forEach((eid) => {
          state.kbData.edges = state.kbData.edges.filter((e) => e.id !== eid);
        });
        cb(data);
        scheduleKbSave();
      },
    },
  });
  kbNetwork._ds = ds;
  kbNetwork.on('click', (p) => {
    if (p.nodes && p.nodes.length) showNodeForm(state.kbData.nodes.find((n) => n.id === p.nodes[0]));
    else showNodeForm(null);
  });
  kbNetwork.on('dragEnd', (p) => {
    if (!p.nodes || !p.nodes.length) return;
    const pos = kbNetwork.getPositions(p.nodes);
    p.nodes.forEach((id) => {
      const n = state.kbData.nodes.find((x) => x.id === id);
      if (n && pos[id]) { n.x = pos[id].x; n.y = pos[id].y; }
    });
    scheduleKbSave();
  });
  kbNetwork.on('doubleClick', (p) => {
    if ((p.nodes && p.nodes.length) || (p.edges && p.edges.length)) return;
    addKbNode(p.pointer && p.pointer.canvas);
  });
  } catch (err) {
    kbNetwork = null;
    fallback('图谱渲染失败：' + (err && err.message ? err.message : String(err)) + '；已切换为列表视图。');
  }
}

function showNodeForm(node) {
  // 打开节点详情时自动收起「新增连线」面板
  kbEdgeFrom = null;
  $('#kb-edge-form').classList.add('hidden');
  if (!node) {
    state.kbSelNode = null;
    $('#kb-node-form').classList.add('hidden');
    $('#kb-node-empty').classList.remove('hidden');
    return;
  }
  state.kbSelNode = node.id;
  $('#kb-node-form').classList.remove('hidden');
  $('#kb-node-empty').classList.add('hidden');
  $('#kd-title').value = node.title || '';
  $('#kd-category').value = node.category || '';
  $('#kd-content').value = node.content || '';
  $('#kd-tags').value = (node.tags || []).join(', ');
  $('#kd-locked').checked = !!node.locked;
}

function addKbNode(pos) {
  const now = Date.now() / 1000;
  const n = {
    id: 'n_' + Math.random().toString(36).slice(2, 10),
    title: '新节点', content: '', category: '其他', tags: [], locked: false,
    x: null, y: null, created_at: now, updated_at: now,
  };
  state.kbData.nodes.push(n);
  if (kbNetwork && kbNetwork._ds) {
    const vn = { id: n.id, label: n.title, title: '', shape: 'dot', size: 16 };
    if (pos) { n.x = pos.x; n.y = pos.y; vn.x = pos.x; vn.y = pos.y; }
    kbNetwork._ds.nodes.add(vn);
  } else {
    drawKbGraph();   // 列表兜底视图下也要让新节点立刻可见
  }
  scheduleKbSave();
  showNodeForm(n);
}

function applyNodeForm() {
  const n = state.kbData.nodes.find((x) => x.id === state.kbSelNode);
  if (!n) return;
  n.title = $('#kd-title').value.trim() || n.title;
  n.category = $('#kd-category').value.trim() || '其他';
  n.content = $('#kd-content').value;
  n.tags = $('#kd-tags').value.split(',').map((t) => t.trim()).filter(Boolean);
  n.locked = $('#kd-locked').checked;
  n.updated_at = Date.now() / 1000;
  if (kbNetwork && kbNetwork._ds) {
    kbNetwork._ds.nodes.update({ id: n.id, label: n.title, title: String(n.content || '').slice(0, 200) });
  } else {
    drawKbGraph();
  }
  scheduleKbSave();
  toast('已应用');
}

// ---------- 新增连线 ----------

function showEdgeForm(fromId) {
  const nodes = state.kbData ? (state.kbData.nodes || []) : [];
  if (nodes.length < 2) { toast('至少要有两个节点才能连线'); return; }
  const from = fromId || state.kbSelNode;
  if (!from) { toast('请先点选一个节点作为起点'); return; }
  kbEdgeFrom = from;
  const fn = nodes.find((n) => n.id === from);
  $('#ke-from').textContent = '起点：' + (fn ? (fn.title || '(无标题)') : '');
  $('#ke-to').innerHTML = nodes.filter((n) => n.id !== from)
    .map((n) => '<option value="' + escapeHtml(n.id) + '">' + escapeHtml(String(n.title || '(无标题)')) + '</option>')
    .join('');
  fillRelationSelect($('#ke-relation'));
  $('#kb-node-form').classList.add('hidden');
  $('#kb-node-empty').classList.add('hidden');
  $('#kb-edge-form').classList.remove('hidden');
}

function hideEdgeForm() {
  kbEdgeFrom = null;
  $('#kb-edge-form').classList.add('hidden');
  if (state.kbSelNode) showNodeForm(state.kbData.nodes.find((n) => n.id === state.kbSelNode));
  else showNodeForm(null);
}

function addKbEdge() {
  const to = $('#ke-to').value;
  const rel = $('#ke-relation').value || 'related';
  if (!kbEdgeFrom || !to || kbEdgeFrom === to) return;
  const label = relationLabel(rel);
  state.kbData.edges = state.kbData.edges || [];
  if (state.kbData.edges.some((e) => e.from === kbEdgeFrom && e.to === to
      && (e.relation || 'related') === rel)) {
    toast('这两个节点之间已有相同关系');
    return;
  }
  const e = { id: 'e_' + Math.random().toString(36).slice(2, 10),
              from: kbEdgeFrom, to: to, label: label, relation: rel, type: 'relation' };
  state.kbData.edges.push(e);
  if (kbNetwork && kbNetwork._ds) {
    kbNetwork._ds.edges.add({
      id: e.id, from: e.from, to: e.to, label: e.label,
      font: { color: relationColor(rel), size: 11, align: 'middle' },
      color: { color: relationColor(rel) },
      arrows: { to: { enabled: true, scaleFactor: 0.55 } }, width: 1.4,
    });
  } else {
    drawKbGraph();
  }
  scheduleKbSave();
  toast('已添加连线：' + label);
  hideEdgeForm();
}

function deleteKbNode() {
  const id = state.kbSelNode;
  if (!id || !confirm('删除这个节点？')) return;
  state.kbData.nodes = state.kbData.nodes.filter((n) => n.id !== id);
  const gone = state.kbData.edges.filter((e) => e.from === id || e.to === id).map((e) => e.id);
  state.kbData.edges = state.kbData.edges.filter((e) => e.from !== id && e.to !== id);
  if (kbNetwork && kbNetwork._ds) {
    kbNetwork._ds.nodes.remove(id);
    if (gone.length) kbNetwork._ds.edges.remove(gone);
  } else {
    drawKbGraph();
  }
  showNodeForm(null);
  scheduleKbSave();
}

function scheduleKbSave() {
  clearTimeout(kbSaveTimer);
  kbSaveTimer = setTimeout(saveKbGraph, 700);
}

// AI 本轮写入记忆后，刷新正打开的图谱
function refreshKbIfOpen() {
  const m = $('#kb-modal');
  if (!isOpen(m)) return;
  const key = state.kbCurrent;
  if (!key) return;
  loadKbGraph(key);
}

async function saveKbGraph() {
  // 用调用时的快照，避免异步期间切换库导致写错目标
  const key = state.kbCurrent;
  const data = state.kbData;
  const sid = state.sessions.current;
  if (!key || !data) return;
  if (key === 'session') {
    await api('/api/memory/save', { session_id: sid, memory: data });
    const s = curSession();
    if (s && state.sessions.current === sid) s.mem_count = data.nodes.length;
  } else {
    const r = await api('/api/kb/save', { id: key, nodes: data.nodes, edges: data.edges });
    if (r && r.kbs && state.kbCurrent === key) {
      state.kbs = r.kbs;
      // 只刷新下拉框的选项，保留用户当前选中的项（否则会把选择弹回旧库）
      refreshKbSelect($('#kb-select').value || key);
      renderKbBindList();
    }
  }
}

function bindKbEvents() {
  $('#btn-kb').onclick = () => openKbModal();
  $('#btn-close-kb').onclick = closeKbModal;
  // 刷新 = 从磁盘重新加载（只读，不会覆盖已写入的内容）
  $('#kb-refresh').onclick = () => { loadKbGraph(state.kbCurrent || 'session'); toast('已刷新'); };
  $('#kb-select').onchange = async (e) => {
    await saveKbGraph();          // 先保存当前库，再切换，避免丢改动
    loadKbGraph(e.target.value);
  };
  $('#kb-add-node').onclick = () => addKbNode(null);
  $('#kb-add-edge').onclick = () => showEdgeForm();
  $('#ke-add').onclick = addKbEdge;
  $('#ke-cancel').onclick = hideEdgeForm;
  $('#kd-save').onclick = applyNodeForm;
  $('#kd-del').onclick = deleteKbNode;
  $('#btn-open-profile-kb').onclick = () => {
    const pid = profileKbId();
    closeModal();
    openKbModal(pid || 'session');
  };
  $('#sess-ai-write').onchange = async () => {
    applySessions(await api('/api/session/bind',
      { id: state.sessions.current, ai_write: $('#sess-ai-write').checked }));
    refreshKbSelect($('#kb-select').value || state.kbCurrent);
  };
  $('#kb-ai-write').onchange = async () => {
    if (state.kbCurrent === 'session') return;
    const r = await api('/api/kb/write', { id: state.kbCurrent, ai_write: $('#kb-ai-write').checked });
    if (r && r.kbs) {
      state.kbs = r.kbs;
      renderKbBindList();
      refreshKbSelect($('#kb-select').value || state.kbCurrent);
    }
  };
  $('#kb-new').onclick = async () => {
    const name = prompt('新知识库名称');
    if (!name || !name.trim()) return;
    await saveKbGraph();          // 先保存当前库的改动，再新建
    const r = await api('/api/kb/create', { name: name.trim() });
    state.kbs = r.kbs;
    refreshKbSelect(r.kb.id);
    renderKbBindList();
    loadKbGraph(r.kb.id);
  };
  $('#kb-rename').onclick = async () => {
    if (state.kbCurrent === 'session') return;
    const meta = kbMeta(state.kbCurrent);
    const name = prompt('重命名知识库', meta ? meta.name : '');
    if (!name || !name.trim()) return;
    try {
      const r = await api('/api/kb/save', { id: state.kbCurrent, name: name.trim() });
      if (r && r.kbs) { state.kbs = r.kbs; refreshKbSelect(state.kbCurrent); renderKbBindList(); }
    } catch (e) { toast('重命名失败（系统库不可改名）'); }
  };
  $('#kb-del').onclick = async () => {
    if (state.kbCurrent === 'session') return;
    const meta = kbMeta(state.kbCurrent);
    if (!confirm('删除知识库「' + (meta ? meta.name : '') + '」？不可恢复。')) return;
    const r = await api('/api/kb/delete', { id: state.kbCurrent });
    if (!r.ok) { toast(r.error || '删除失败'); return; }
    state.kbs = r.kbs;
    refreshKbSelect('session');
    renderKbBindList();
    loadKbGraph('session');
  };
}

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

function upgradeMath() {
  if (!window.katex) return;
  document.querySelectorAll('#chat code.math-pending').forEach((el) => {
    const tex = el.getAttribute('data-tex') || '';
    const display = el.getAttribute('data-display') === '1';
    try {
      const tmp = document.createElement('span');
      tmp.innerHTML = katex.renderToString(tex, { displayMode: display, throwOnError: false });
      if (tmp.firstChild) el.replaceWith(tmp.firstChild);
    } catch (e) { }
  });
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

/* ============================================================
   初始化
   ============================================================ */
async function init() {
  const res = await fetch('/api/state', { headers: { 'X-Auth-Token': TOKEN } });
  const data = await res.json();
  state.config = data.config;
  state.profile = data.profile;
  state.sessions = data.sessions;
  state.kbs = data.kbs || [];
  state.builtinPresets = data.builtin_presets || {};
  state.builtinPresetLabel = data.builtin_preset_label || '默认（内置）';
  state.searchProviders = data.search_providers || {};
  state.relations = data.relations || [];
  renderRelationHelp();

  applyTheme(state.config.theme);
  renderSessions();
  renderMessages();
  renderContextBar(data.context);
  // 首屏先渲染完；把已经就绪的库补齐（未就绪的等 onLibReady 回调）
  Object.keys(window.__libs || {}).forEach(onLibReady);

  $('#btn-new').onclick = newSession;
  $('#btn-send').onclick = sendMessage;
  $('#btn-stop').onclick = stopStreaming;
  $('#btn-compress').onclick = compressContextManually;
  $('#btn-settings').onclick = openModal;
  $('#btn-close-modal').onclick = closeModal;
  $('#btn-cancel').onclick = closeModal;
  $('#btn-save').onclick = saveModal;

  bindKbEvents();
  bindNotesEvents();

  $('#btn-theme').onclick = () => {
    const next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    applyTheme(next);
    api('/api/config', Object.assign({}, state.config, { theme: next })).then(() => {
      state.config.theme = next;
    });
  };

  // 深度思考开关 + 档位（状态存进 config.json，重启保留）
  const thinkPill = $('#btn-think');
  const thinkLevelSel = $('#think-level');
  state.thinkEnabled = !!state.config.chat_think_enabled;
  state.thinkLevel = state.config.chat_think_level || 'high';
  thinkLevelSel.value = state.thinkLevel;

  function syncThinkUI() {
    thinkPill.classList.toggle('active', !!state.thinkEnabled);
    thinkLevelSel.classList.toggle('hidden', !state.thinkEnabled);
  }
  syncThinkUI();

  thinkPill.onclick = () => {
    state.thinkEnabled = !state.thinkEnabled;
    syncThinkUI();
    api('/api/config', { chat_think_enabled: state.thinkEnabled })
      .then(() => { state.config.chat_think_enabled = state.thinkEnabled; });
  };

  thinkLevelSel.onchange = () => {
    state.thinkLevel = thinkLevelSel.value;
    api('/api/config', { chat_think_level: state.thinkLevel })
      .then(() => { state.config.chat_think_level = state.thinkLevel; });
  };

  // 联网搜索开关（未配置密钥时不允许点亮）
  state.searchEnabled = !!state.config.search_enabled && hasSearchKey();
  if (state.config.search_enabled && !hasSearchKey()) {
    api('/api/config', { search_enabled: false }).then(() => { state.config.search_enabled = false; });
  }
  syncSearchPill();
  $('#btn-search').onclick = () => {
    if (!state.searchEnabled && !hasSearchKey()) {
      toast('尚未配置搜索 API 密钥，请在「设置 → 联网搜索」中填写');
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
  window.addEventListener('resize', () => { if (usageChart) usageChart.resize(); });

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
  input.addEventListener('input', autoGrow);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  });

  // 图片
  $('#btn-image').onclick = () => $('#file-input').click();
  $('#file-input').onchange = (e) => {
    if (e.target.files && e.target.files[0]) sendImageBlob(e.target.files[0]);
    e.target.value = '';
  };
  document.addEventListener('paste', (e) => {
    const items = (e.clipboardData || {}).items || [];
    for (const it of items) {
      if (it.type && it.type.startsWith('image/')) {
        const f = it.getAsFile();
        if (f) { sendImageBlob(f); e.preventDefault(); }
        return;
      }
    }
  });
  window.addEventListener('dragover', (e) => e.preventDefault());
  window.addEventListener('drop', (e) => {
    e.preventDefault();
    const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
    if (f && f.type && f.type.startsWith('image/')) sendImageBlob(f);
  });

  // 代码块复制 + 设置里文案
  $('#chat').addEventListener('click', (e) => {
    const btn = e.target.closest('[data-copy]');
    if (!btn) return;
    const code = btn.closest('.code-block').querySelector('code');
    navigator.clipboard.writeText(code ? code.textContent : '').then(() => toast('已复制代码'));
  });

  // 设置 tab 切换（限定在设置弹窗内，避免影响用量弹窗的同名 tab）
  $$('#modal .modal-tabs .tab').forEach((tab) => {
    tab.onclick = () => {
      $$('#modal .modal-tabs .tab').forEach((t) => t.classList.remove('active'));
      tab.classList.add('active');
      $$('#modal .pane').forEach((pn) => pn.classList.toggle('hidden', pn.dataset.pane !== tab.dataset.tab));
    };
  });

  // 回到底部按钮（用 .off 过渡显隐，而非 display:none 硬切）
  $('#chat').addEventListener('scroll', () => {
    const far = !nearBottom();
    $('#btn-bottom').classList.toggle('off', !far);
  });
  $('#btn-bottom').onclick = () => scrollToBottomSmooth();

  bindChips();

  if (!String(state.config.chat_api_key || '').trim()) {
    setTimeout(() => { openModal(); toast('首次使用：请先填写聊天 API 密钥'); }, 400);
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
          '<span class="nt-caret">' + (isOpen ? '▾' : '▸') + '</span>📁 ' + escapeHtml(n.name) + '</div>');
        if (isOpen) walk(n.children, depth + 1);
      } else {
        rows.push('<div class="nt-row" data-note="' + escapeHtml(n.path) + '" style="padding-left:' + pad + 'px">' +
          '<span class="nt-caret"></span>📄 ' + escapeHtml(n.name) + '</div>');
      }
    });
  };
  walk(state.noteTree, 0);
  box.innerHTML = rows.join('') || '<div class="hint">还没有笔记，点「＋ 笔记」新建一篇。</div>';
  box.querySelectorAll('[data-dir]').forEach((el) => {
    el.onclick = () => {
      const p = el.dataset.dir;
      if (open.has(p)) open.delete(p); else open.add(p);
      renderNotesTree();
    };
  });
  box.querySelectorAll('[data-note]').forEach((el) => {
    if (el.dataset.note === state.noteCurrent) el.classList.add('active');
    el.onclick = () => openNote(el.dataset.note);
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
    pv.innerHTML = renderMarkdown(ta.value);
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
    const name = prompt('新笔记标题（可写 文件夹/子标题 建到子目录）');
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
  const name = prompt('新文件夹名称（可写 一级/二级）');
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
  const name = prompt('重命名为（会自动更新其它笔记里的 [[旧名]] 链接）', cur);
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
    el.innerHTML = '<p class="hint" style="padding:16px">图谱库 vis-network 未加载（需联网）。</p>';
    return;
  }
  el.innerHTML = '';
  if (!g.nodes.length) {
    el.innerHTML = '<p class="hint" style="padding:16px">还没有笔记。新建几篇，用 <code>[[双链]]</code> 互相关联，这里就会长出关系网络。</p>';
    return;
  }
  const dark = document.documentElement.getAttribute('data-theme') === 'dark';
  const fontColor = dark ? '#e5e5e5' : '#171717';
  const lineColor = dark ? '#3a3a3a' : '#dedee2';
  const cur = state.noteCurrent;
  const nodes = g.nodes.map((n) => {
    const o = {
      id: n.id,
      label: n.missing ? (n.label + ' ?') : n.label,
      shape: 'dot',
      size: n.id === cur ? 22 : (n.missing ? 12 : 16),
      font: { color: fontColor, size: 13 },
      color: n.missing
        ? { background: 'rgba(217,130,43,.15)', border: '#d9822b' }
        : (n.id === cur ? { background: 'rgba(77,107,254,.18)', border: '#4d6bfe' }
                        : { background: dark ? '#2a2a2a' : '#ffffff', border: lineColor }),
    };
    if (n.missing) o.shapeProperties = { borderDashes: [4, 4] };
    const p = notesPos[n.id];
    if (p && typeof p.x === 'number') { o.x = p.x; o.y = p.y; }
    return o;
  });
  const edges = g.edges.map((e) => ({
    id: e.id, from: e.from, to: e.to,
    color: { color: lineColor },
    arrows: { to: { enabled: true, scaleFactor: 0.5 } },
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
  const dark = document.documentElement.getAttribute('data-theme') === 'dark';
  const lineColor = dark ? '#3a3a3a' : '#dedee2';
  const paint = (id, active) => {
    if (!id) return;
    const nd = (state.noteGraph.nodes || []).find((x) => x.id === id);
    if (!nd || nd.missing) return;
    notesGraphNet._ds.nodes.update({
      id: id,
      size: active ? 22 : 16,
      color: active ? { background: 'rgba(77,107,254,.18)', border: '#4d6bfe' }
                    : { background: dark ? '#2a2a2a' : '#ffffff', border: lineColor },
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
