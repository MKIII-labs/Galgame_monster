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
  pendingImage: null,        // 待发送图片：{ mime, b64, url }
  pendingPresets: {},
  uiMode: 'solo',            // solo | ide
  ideTree: null,
  ideOpen: '',               // 当前打开的文件（沙箱内相对路径）
  ideDir: '',                // 当前选中的目录（新建时的默认父目录）
  ideDirty: false,
  ideChanged: new Set(),     // AI 改动过、需要在文件树里高亮的文件
  // IDE 运行时字段：首次进入 IDE 前就需有默认值，否则点文件会因 undefined 抛错
  ideSide: 'explorer',
  ideTabList: [],            // 已打开的标签（沙箱内相对路径）
  ideCache: {},              // rel -> { text, enc }
  ideDirtyMap: {},           // rel -> 是否有未保存改动
  ideModels: {},             // rel -> Monaco model
  ideEditor: null,           // Monaco editor 实例
  ideMonaco: null,           // Monaco 命名空间
  ideMonacoDefaults: null,   // 从 VS Code 导入的编辑器默认项
  idePaletteMode: 'file',
  idePaletteSel: 0,
  idePaletteItems: [],
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
  const lines = String(code).split('\n').length;
  const fold = lines > 28;   // 超长代码块默认折叠，避免刷屏
  return '<div class="code-block' + (fold ? ' folded' : '') + '"><div class="code-head"><span>' +
    escapeHtml(lang || 'text') + '</span><span class="code-tools">' +
    (fold ? '<button class="copy" data-fold data-total="' + lines + '">展开全部（' + lines + ' 行）</button>' : '') +
    '<button class="copy" data-copy>复制</button></span></div><pre><code class="hljs" data-lang="' +
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
  syncAgentUI();
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
  resetIdeForSession();
  if (r) renderContextBar(r.context);
}

async function deleteSession(id) {
  if (state.streaming) await stopStreaming();
  const r = await api('/api/session/delete', { id });
  state.sessions = r.sessions;
  renderSessions();
  renderMessages();
  resetIdeForSession();
  if (r) renderContextBar(r.context);
}

async function newSession() {
  if (state.streaming) await stopStreaming();
  const r = await api('/api/session/new', {});
  state.sessions = r.sessions;
  renderSessions();
  renderMessages();
  resetIdeForSession();
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
  mk('复制', () => { navigator.clipboard.writeText(getText()).then(() => toast('已复制', 'success')); });
  if (role === 'user') {
    mk('撤回', () => withdrawUserMessage(index));
    mk('修改', () => editUserMessage(index));
    mk('删除', () => deleteMessage(index)).classList.add('act-danger');
  } else {
    row._selBtn = mk('选择文本', () => toggleRawView(row));
    if (typeof index === 'number') mk('重新发言', () => regenerateReply(index));
    mk('删除', () => deleteMessage(index)).classList.add('act-danger');
  }
  return bar;
}

// 记忆写入标记：AI 本轮改了哪些记忆 + 可撤销
function buildMemoryChip(mem, index) {
  const box = document.createElement('div');
  box.className = 'mem-chip' + (mem.undone ? ' undone' : '');
  const problems = mem.problems || [];
  const warnOnly = !mem.applied && problems.length;   // 一条都没生效：只提示，不给撤销
  if (warnOnly) box.classList.add('warn');
  const icon = document.createElement('span');
  icon.className = 'mem-icon';
  icon.textContent = warnOnly ? '⚠️' : '🧠';
  const text = document.createElement('span');
  text.className = 'mem-text';
  if (warnOnly) {
    text.textContent = '记忆未生效：' + problems.join('、');
  } else {
    const scopes = (mem.scopes || []).join('、');
    text.textContent = '已更新记忆：' + (mem.summary || '') + (scopes ? '（' + scopes + '）' : '') +
      (problems.length ? '；未生效：' + problems.join('、') : '');
  }
  box.appendChild(icon);
  box.appendChild(text);
  if (mem.undone) {
    const tag = document.createElement('span');
    tag.className = 'mem-undone';
    tag.textContent = '已撤销';
    box.appendChild(tag);
    return box;
  }
  if (!mem.undo) return box;        // 没有实际变更时不给撤销按钮，避免死按钮
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

// 聊天图片附件：按需拉取（带鉴权）→ 缓存 Blob URL，供消息气泡预览
const attachBlobCache = new Map();
function attachBlobUrl(sid, name) {
  const key = sid + '/' + name;
  if (attachBlobCache.has(key)) return Promise.resolve(attachBlobCache.get(key));
  return fetch('/api/attach/get', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Auth-Token': TOKEN },
    body: JSON.stringify({ session_id: sid, name: name }),
  }).then((res) => {
    if (!res.ok) throw new Error('HTTP ' + res.status);
    return res.blob();
  }).then((blob) => {
    const url = URL.createObjectURL(blob);
    attachBlobCache.set(key, url);
    return url;
  });
}

function buildUserRow(text, index, msg) {
  const row = document.createElement('div');
  row.className = 'msg user';
  if (typeof index === 'number') row.dataset.idx = index;
  const wrap = document.createElement('div');
  wrap.className = 'user-wrap';

  // 图片附件：气泡上方展示缩略图（点击放大 / 还原）
  const im = (msg && msg.image) || null;
  if (im && (im.name || im.url)) {
    const box = document.createElement('div');
    box.className = 'msg-image';
    box.title = im.size
      ? ('图片 · ' + Math.max(1, Math.round(im.size / 1024)) + ' KB（点击放大 / 还原）')
      : '点击放大 / 还原';
    const el = document.createElement('img');
    el.alt = '图片';
    box.appendChild(el);
    box.onclick = () => box.classList.toggle('zoom');
    wrap.appendChild(box);
    if (im.url) {
      el.src = im.url;                              // 刚发出：直接用本地预览图
    } else if (im.name) {
      attachBlobUrl(state.sessions.current, im.name)
        .then((u) => { el.src = u; })
        .catch(() => { box.remove(); });
    }
  }

  const b = document.createElement('div');
  b.className = 'bubble';
  b.textContent = text;
  if (!(im && (text === '【图片】' || !text))) wrap.appendChild(b);   // 纯图片消息不再重复「【图片】」占位

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

/* ---------- Agent：工具卡片 / 提问卡片 ---------- */
const TOOL_LABELS = {
  list_dir: ['📂', '列目录'], read_file: ['📄', '读文件'], search_files: ['🔎', '搜索文件'],
  write_file: ['✏️', '写文件'], edit_file: ['✏️', '修改文件'], make_dir: ['📁', '新建目录'],
  move_file: ['📦', '移动'], delete_file: ['🗑', '删除'], ask_user: ['❓', '提问'],
};

function toolArgText(name, args) {
  args = args || {};
  if (name === 'move_file') return (args.from || '') + ' → ' + (args.to || '');
  if (name === 'search_files') return [args.pattern, args.glob].filter(Boolean).join('   ');
  return args.path || '';
}

function buildToolCard(name, args) {
  const info = TOOL_LABELS[name] || ['🔧', name];
  const card = document.createElement('div');
  card.className = 'tool-card running';
  card.innerHTML =
    '<div class="tool-head">' +
      '<span class="tool-ico">' + info[0] + '</span>' +
      '<span class="tool-name">' + escapeHtml(info[1]) + '</span>' +
      '<span class="tool-arg">' + escapeHtml(toolArgText(name, args)) + '</span>' +
      '<span class="tool-state">执行中…</span>' +
    '</div>' +
    '<pre class="tool-result hidden"></pre>' +
    '<div class="tool-actions"></div>';
  return card;
}

function addUndoButton(card, token) {
  const act = card.querySelector('.tool-actions');
  if (!act || !token) return;
  const b = document.createElement('button');
  b.type = 'button';
  b.className = 'tool-undo';
  b.textContent = '撤回';
  b.onclick = async () => {
    b.disabled = true;
    try {
      const r = await api('/api/agent/undo', { token: token });
      if (r && r.ok) { b.textContent = '已撤回'; toast('已撤回该次改动'); }
      else { b.disabled = false; toast('撤回失败或已过期'); }
    } catch (e) { b.disabled = false; toast('撤回失败：' + e); }
  };
  act.appendChild(b);
}

function finishToolCard(card, obj) {
  card.classList.remove('running');
  const st = card.querySelector('.tool-state');
  if (st) st.textContent = obj.write ? '已写入' : '完成';
  const pre = card.querySelector('.tool-result');
  if (pre) { pre.textContent = obj.result || ''; pre.classList.remove('hidden'); }
  if (obj.undo) addUndoButton(card, obj.undo);
}

function buildAskCard(q) {
  const opts = (q.options || []).map((o) => String(o)).filter((o) => o.trim());
  const card = document.createElement('div');
  card.className = 'ask-card';
  const done = q.answer != null;
  card.innerHTML =
    '<div class="ask-q">❓ ' + escapeHtml(q.question || '') + '</div>' +
    '<div class="ask-opts">' + opts.map((o, i) =>
      '<button type="button" class="ask-opt" data-i="' + i + '">' + escapeHtml(o) + '</button>').join('') + '</div>' +
    (done ? '' :
      '<div class="ask-input-row"><input class="ask-input field-input" placeholder="也可以自己输入回答…">' +
      '<button type="button" class="ask-send primary">提交</button></div>') +
    '<div class="ask-chosen">' + (done ? '回答：' + escapeHtml(q.answer) : '') + '</div>';
  if (done) return card;
  const input = card.querySelector('.ask-input');
  const send = card.querySelector('.ask-send');
  const submit = async (val) => {
    if (card.dataset.done || !val) return;
    card.dataset.done = '1';
    card.querySelectorAll('.ask-opt').forEach((b) => { b.disabled = true; });
    input.disabled = true;
    send.disabled = true;
    card.classList.add('done');
    card.querySelector('.ask-chosen').textContent = '回答：' + val;
    try { await api('/api/agent/answer', { qid: q.qid, answer: val }); } catch (e) { /* 忽略 */ }
    scrollToBottom(true);
  };
  card.querySelectorAll('.ask-opt').forEach((b) => {
    b.onclick = () => submit(opts[Number(b.dataset.i)]);
  });
  send.onclick = () => submit(input.value.trim());
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); submit(input.value.trim()); }
  });
  return card;
}

// 历史回显：把 assistant 消息里保存的工具记录渲染出来
function renderToolLog(wrap, tools) {
  if (!tools || !tools.length) return;
  const box = document.createElement('div');
  box.className = 'tool-list';
  tools.forEach((t) => {
    if (t.tool === 'ask_user') {
      box.appendChild(buildAskCard({ question: t.question, options: t.options, answer: t.answer }));
      return;
    }
    const card = buildToolCard(t.tool, t.args);
    finishToolCard(card, { result: t.result, undo: t.undo, write: t.write });
    box.appendChild(card);
  });
  wrap.appendChild(box);
}

function buildAiRow(contentHtml, rawText, index, reasoning, sources, usage, memory, tools) {
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
  renderToolLog(wrap, tools);
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
      : buildAiRow(renderMarkdown(m.content), m.content, idx, m.reasoning, m.sources, m.usage, m.memory, m.tools));
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
  syncSendBtn();
  // 图片 / 停止按钮共用同一插槽，用 .off 交叉淡入替代 display:none 硬切
  $('#btn-stop').classList.toggle('off', !on);
  $('#btn-image').classList.toggle('off', on);
}

// 输入为空且没有待发图片时，发送按钮置灰；流式期间、录音 / 识别 / 矫正期间同样禁用
function syncSendBtn() {
  const btn = $('#btn-send');
  if (!btn) return;
  const empty = !$('#input').value.trim() && !state.pendingImage;
  btn.disabled = !!state.streaming || !!mic.active || !!mic.busy || empty;
}

async function stopStreaming() {
  if (state.uiMode === 'gal') galStopAudio();   // 停止生成时也打断朗读
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
  if (state.streaming || mic.active || mic.busy) return;   // 录音/识别/矫正未完成时不发送，避免两段文本并存
  const input = $('#input');
  const text = input.value.trim();
  const img = state.pendingImage;
  if (!text && !img) return;                 // 允许「纯图片」发送
  if (!hasChatKey()) { toast('请先在「设置 → 聊天模型」中填写密钥', 'error'); return; }
  input.value = '';
  autoGrow();
  hideSttChip();
  hideEmpty();
  // 发新消息前，把还处于「预览」的用户消息恢复为当前版本
  if (verPreview && verPreview.sid === state.sessions.current) refreshUserRow(verPreview.index);
  verPreview = null;
  const extra = { content: text };
  if (state.uiMode === 'gal') {
    extra.mode = 'gal';
    extra.gal_character_id = galState.charId || (curSession() || {}).gal_character_id || '';
    galSetUserEcho(text || '【图片】');   // 对白区顶部回显你这句话（暗淡色）
    galResetDialog();
    galSetStatus('思考中…');          // 必须放在 galResetDialog 之后，否则会被清掉
  }
  if (img) extra.image = { mime: img.mime, data: img.b64 };
  appendUser(text || '【图片】', curSession() ? curSession().messages.length : undefined,
             img ? { image: { url: img.url, size: Math.round(img.b64.length * 3 / 4) } } : undefined);
  clearAttach();
  await streamChat(extra);
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
  if (state.uiMode === 'gal') { galSetStatus('思考中…'); galSetTyping(false); galState.skip = false; galState.streamedTurn = false; }   // 新一轮：解除上轮的「跳过朗读」
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
    const tip = c.querySelector('.recognize-tip');
    if (tip) tip.remove();
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
        if (obj.type === 'recognizing') {
          const tip = document.createElement('span');
          tip.className = 'recognize-tip';
          tip.textContent = obj.mode === 'vision' ? '📷 正在识别图片内容…' : '📷 正在看图…';
          c.insertBefore(tip, typing);
          scrollToBottom();
        } else if (obj.type === 'searching') {
          if (!sourcesBox) {
            sourcesBox = buildThinkBlock('', true);
            sourcesBox.classList.add('sources');
            sourcesBox._label.textContent = '正在联网搜索…';
            row._wrap.insertBefore(sourcesBox, row._wrap.firstChild);
          }
          if (state.uiMode === 'gal') { galSetStatus('查阅中…'); galExecLine('查阅资料…'); }
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
          if (state.uiMode === 'gal') galExecLine('在回想相关的事…');
          scrollToBottom();
        } else if (obj.type === 'sources') {
          if (sourcesBox) sourcesBox.remove();
          sourcesBox = buildSourcesBlock(obj.items, false);
          row._wrap.insertBefore(sourcesBox, row._wrap.firstChild);
          if (state.uiMode === 'gal') {
            galExecLine('已留意到一些细节', 'ok');
            galSetStatus('思考中…');
          }
          scrollToBottom();
        } else if (obj.type === 'search_error') {
          if (sourcesBox) { sourcesBox.remove(); sourcesBox = null; }
          const note = document.createElement('div');
          note.className = 'search-note';
          note.textContent = obj.message;
          row._wrap.insertBefore(note, row._wrap.firstChild);
          if (state.uiMode === 'gal') galExecLine('暂时没能查到相关信息', 'err');
          scrollToBottom();
        } else if (obj.type === 'usage') {
          if (!row._usageEl) {
            row._usageEl = document.createElement('div');
            row._usageEl.className = 'usage-line';
            row._wrap.appendChild(row._usageEl);
          }
          row._usageEl.textContent = usageText(obj.usage);
          if (state.uiMode === 'gal') galExecUsage(obj.usage);
        } else if (obj.type === 'reasoning') {
          if (!thinkBox) {
            thinkBox = buildThinkBlock('', true);
            thinkBox._label.textContent = '深度思考中…';
            row._wrap.insertBefore(thinkBox, row._content);
          }
          thinkBox._body.textContent += obj.text;
          if (state.uiMode === 'gal') { galSetStatus('思考中…'); galThinkAppend(obj.text); }
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
          if (state.uiMode === 'gal') { galExecLine('被打断了…' + String(obj.message || '').slice(0, 60), 'err'); galSetStatus(''); }
          scrollToBottom();
        } else if (obj.type === 'tool') {
          if (!row._toolsBox) {
            row._toolsBox = document.createElement('div');
            row._toolsBox.className = 'tool-list';
            row._wrap.appendChild(row._toolsBox);
          }
          const card = buildToolCard(obj.name, obj.args);
          const useGal = state.uiMode === 'gal' && galCfg('gal_show_exec', true) !== false;
          (useGal ? $('#gal-tools') : row._toolsBox).appendChild(card);
          row._toolQueue = row._toolQueue || {};
          (row._toolQueue[obj.name] = row._toolQueue[obj.name] || []).push(card);
          if (state.uiMode === 'gal') galSetStatus('稍候…');
          scrollToBottom();
        } else if (obj.type === 'tool_done') {
          const q = row._toolQueue && row._toolQueue[obj.name];
          const card = q && q.length ? q.shift() : null;
          if (card) finishToolCard(card, obj);
          if (state.uiMode === 'gal') galSetStatus('思考中…');
          ideOnToolDone(obj);
          scrollToBottom();
        } else if (obj.type === 'ask') {
          if (!row._toolsBox) {
            row._toolsBox = document.createElement('div');
            row._toolsBox.className = 'tool-list';
            row._wrap.appendChild(row._toolsBox);
          }
          const askCard = buildAskCard(obj);
          const askGal = state.uiMode === 'gal' && galCfg('gal_show_exec', true) !== false;
          (askGal ? $('#gal-tools') : row._toolsBox).appendChild(askCard);
          scrollToBottom();
        } else if (obj.type === 'mem_write') {
          memNote = obj.memory;
          if (state.uiMode === 'gal' && obj.memory) {
            galExecLine('记住了：' + (obj.memory.summary || ''), 'ok');
          }
        } else if (obj.type === 'context') {
          renderContextBar(obj.context);
        } else if (obj.type === 'compressing') {
          if (state.uiMode === 'gal') {
            galExecLine('回想先前的对话…'); galSetStatus('回想中…');
          } else {
            toast('正在压缩较早的历史对话…');
          }
        } else if (obj.type === 'gal_speech') {
          galState.streamedTurn = true;      // 本轮已逐段流式渲染（结束时不再回填重排）
          galSetStatus('');
          galSetTyping(true);
          galAddSegment(obj);
        } else if (obj.type === 'gal_stage') {
          if (obj.bg) galSetBackground(obj.bg);
          if (obj.bgm) galSetBgm(obj.bgm);
        } else if (obj.type === 'gal_options') {
          // 选项不立即出现：先暂存在 pendingOptions，等本轮最后一段文字打完再显示
          galState.pendingOptions = (obj.options || []).slice();
          galFlushOptions();
        } else if (obj.type === 'gal_node') {
          galSetNode(obj);
          if (obj.auto) {
            galAppendDivider('—— 幕间 · 剧情推进 ——');   // AI 自动剧本进入下一幕
            if (obj.scene) galAddSegment({ narration: obj.scene });   // 幕的场景描述进对话框
          }
        } else if (obj.sessions) {
          state.sessions = obj.sessions;
          renderSessions();
          if (obj.context) renderContextBar(obj.context);
          if (state.uiMode === 'gal') {
            galSetTyping(false);
            galSetStatus('');
            renderMessages();
            // 本轮已逐段流式渲染过 → 不回填重排，否则会把逐段效果抹成一次性整段。
            // 另外只认「done」（本轮真正结束）：流开头的 "session" 事件也带 sessions，
            // 若在那时回填，会把上一轮的对白与选项重新画出来（表现为选项一直悬浮、点了没反应）。
            if (obj.type === 'done' && !galState.streamedTurn) galRenderFromSession();
            const rp = $('#gal-review-panel');
            if (rp && !rp.classList.contains('hidden')) galRenderReview();   // 回顾面板打开时同步刷新
          }
        }
      }
    }
  } catch (e) {
    acc += (acc ? '\n\n' : '') + '❌ 请求失败：' + e;
  } finally {
    if (state.uiMode === 'gal') { galSetTyping(false); galSetStatus(''); }
    if (typing.parentNode) typing.remove();
    const tip = c.querySelector('.recognize-tip');
    if (tip) tip.remove();
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
    if (state.uiMode === 'gal') galFlushOptions();   // 流已结束 → 若有暂存选项则显示
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

// 仅删除这一条消息（不影响前后其它消息）
async function deleteMessage(index) {
  if (state.streaming || typeof index !== 'number') return;
  const s = curSession();
  const m = s && s.messages[index];
  if (!m) return;
  const label = m.role === 'user' ? '用户消息' : 'AI 回复';
  if (!confirm('删除这条' + label + '？此操作不可恢复。')) return;
  try {
    const r = await api('/api/message/withdraw', {
      session_id: state.sessions.current, index: index, only_this: true,
    });
    if (!r || !r.ok) { toast('删除失败', 'error'); return; }
    if (r.sessions) state.sessions = r.sessions;
    renderSessions();
    renderMessages();
    if (r.context) renderContextBar(r.context);
    toast('已删除该条消息', 'success');
  } catch (e) { toast('删除失败：' + e, 'error'); }
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
  if (!hasChatKey()) { toast('请先在「设置 → 聊天模型」中填写密钥', 'error'); return; }
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
  const body = {
    resend: { mode: opts.mode, index: opts.index, vidx: opts.vidx },
    content: opts.content || '',
  };
  if (state.uiMode === 'gal') {
    body.mode = 'gal';
    body.gal_character_id = galState.charId || (curSession() || {}).gal_character_id || '';
    galSetUserEcho(opts.content || '');
    galResetDialog();
    galSetStatus('思考中…');
  }
  await streamChat(body);
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
   图片：先暂存为「待发送附件」，可继续输入文字后一起发送
   ============================================================ */
function attachImageBlob(blob) {
  const reader = new FileReader();
  reader.onload = () => {
    const mime = (reader.result.match(/^data:([^;]+);/) || [])[1] || 'image/png';
    state.pendingImage = {
      mime,
      b64: String(reader.result).split(',')[1] || '',
      url: String(reader.result),
    };
    renderAttach();
  };
  reader.readAsDataURL(blob);
}

function renderAttach() {
  const img = state.pendingImage;
  const bar = $('#attach-bar');
  if (!bar) return;
  bar.classList.toggle('hidden', !img);
  if (img) $('#attach-thumb').src = img.url;
  syncSendBtn();
}

function clearAttach() {
  state.pendingImage = null;
  const thumb = $('#attach-thumb');
  if (thumb) thumb.removeAttribute('src');
  renderAttach();
}

/* ============================================================
   设置弹窗
   ============================================================ */
const PROVIDER_META = {
  chat: { title: '聊天模型', desc: '用于智能聊天与记忆读写', hint: '填写任意 OpenAI 兼容接口（地址 + 模型名 + 密钥）', protocol: false },
  vision: { title: '视觉模型', desc: '用于图像识别', hint: '支持 DashScope 原生协议，或 OpenAI 兼容的视觉接口', protocol: true },
};

// 上下文 / 深度思考两组输入已迁到独立面板，这里保留元素引用供聊天预设读写
const chatCtx = { limitEl: null, reserveEl: null, thinkEl: null };

/* ---- 设置面板：动态面板共用的排版片段（统一类名与搜索索引 data-search）---- */
function mkPane(name) {
  const p = document.createElement('section');
  p.className = 'pane hidden';
  p.dataset.pane = name;
  return p;
}
const setGroupHtml = (title, search) =>
  '<h4 class="set-group"' + (search ? ' data-search="' + search + '"' : '') + '>' + title + '</h4>';
const labelHtml = (text, search) =>
  '<label class="field-label"' + (search ? ' data-search="' + search + '"' : '') + '>' + text + '</label>';
const hintHtml = (text, search) =>
  '<p class="hint"' + (search ? ' data-search="' + search + '"' : '') + '>' + text + '</p>';
const checkRowHtml = (inputId, text, search) =>
  '<label class="check-row"' + (search ? ' data-search="' + search + '"' : '') + '>' +
  '<input type="checkbox" id="' + inputId + '"> ' + text + '</label>';
const qIn = (pane, id) => pane.querySelector('#' + id);

function buildApiPane(provider) {
  const meta = PROVIDER_META[provider];
  const cfg = state.config;
  state.pendingPresets[provider] = Object.assign({}, cfg[provider + '_presets'] || {});

  const pane = mkPane(provider);
  let html = hintHtml('<strong>' + meta.title + '</strong> —— ' + meta.desc + '<br>' + meta.hint,
                      '接口 地址 模型 密钥 预设 api');
  if (provider === 'vision') {
    html += setGroupHtml('使用方式', '识图 聊天模型 视觉');
    html += checkRowHtml('vision-use-chat', '使用聊天模型识别图片（聊天模型自带视觉能力时）', '聊天模型 识图 视觉');
    html += hintHtml('开启后不再使用下面的视觉 API，改由「聊天 API」的模型与密钥识别图片（需聊天模型支持视觉，如 GPT-4o / deepseek-flash / 通义兼容模式）。详见「功能说明 → 图像识别」。');
    html += setGroupHtml('接入', '接口 地址 模型 密钥 协议');
  } else {
    html += setGroupHtml('接入', '接口 地址 模型 密钥 预设');
  }
  if (meta.protocol) {
    html += labelHtml('接口协议', '协议 dashscope openai 兼容');
    html += '<div class="radio-row">' +
      '<label><input type="radio" name="vision-proto" value="native"> DashScope 原生</label>' +
      '<label><input type="radio" name="vision-proto" value="openai"> OpenAI 兼容</label>' +
      '</div>';
    html += hintHtml('原生：图片放 <code>input.messages</code>，结果取 <code>output.choices</code>（qwen-vl-plus / max）。<br>' +
                     '兼容：图片放 <code>messages</code>（<code>image_url</code>），结果取 <code>choices</code>（GPT-4o、vLLM/Ollama、deepseek-flash）。详见「功能说明 → 图像识别」。');
  }
  html += labelHtml('预设', '预设 另存 删除') +
    '<div class="preset-row"><select id="' + provider + '-preset"></select>' +
    '<button type="button" id="' + provider + '-preset-save">另存为预设</button>' +
    '<button type="button" id="' + provider + '-preset-del">删除预设</button></div>';
  html += labelHtml('接口地址', '接口 地址 url endpoint baseurl') +
    '<input id="' + provider + '-url" class="field-input" type="text">';
  html += labelHtml('模型名', '模型 model') +
    '<input id="' + provider + '-model" class="field-input" type="text">';
  html += labelHtml('API 密钥', '密钥 key api token 认证') +
    '<input id="' + provider + '-key" class="field-input" type="password">';
  html += '<label class="check-row"><input type="checkbox" id="' + provider + '-show-key"> 显示密钥</label>';
  html += setGroupHtml('账户余额', '余额 balance 查询 充值 赠金') +
    '<div class="balance-row"><span class="balance-text" id="' + provider + '-balance">未查询</span>' +
    '<button type="button" class="ghost" id="' + provider + '-balance-refresh">刷新</button></div>' +
    labelHtml('余额查询地址（可选；留空时按接口地址自动识别 DeepSeek）', '余额 balance 接口 地址') +
    '<input id="' + provider + '-balance-url" class="field-input" type="text" placeholder="https://api.deepseek.com/user/balance">';
  pane.innerHTML = html;

  const P = (suffix) => qIn(pane, provider + '-' + suffix);
  const q = P;
  q('url').value = cfg[provider + '_url'] || '';
  q('model').value = cfg[provider + '_model'] || '';
  q('key').value = cfg[provider + '_api_key'] || '';
  if (meta.protocol) {
    const val = cfg[provider + '_protocol'] || (provider === 'vision' ? 'native' : 'openai');
    const radio = pane.querySelector('input[name="vision-proto"][value="' + val + '"]');
    if (radio) radio.checked = true;
  }
  q('show-key').onchange = (e) => { q('key').type = e.target.checked ? 'text' : 'password'; };
  q('balance-url').value = cfg[provider + '_balance_url'] || '';
  q('balance-refresh').onclick = () => refreshBalance(provider, pane);
  if (provider === 'vision') {
    const uc = q('use-chat');
    uc.checked = !!cfg.vision_use_chat;
    uc.onchange = () => setVisionFieldsDisabled(pane, uc.checked);
    setVisionFieldsDisabled(pane, uc.checked);
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
      if (p.context_limit != null && chatCtx.limitEl) chatCtx.limitEl.value = p.context_limit;
      if (p.reserve_output != null && chatCtx.reserveEl) chatCtx.reserveEl.value = p.reserve_output;
    }
    if (meta.protocol && p.protocol) {
      const r = pane.querySelector('input[name="' + provider + '-proto"][value="' + p.protocol + '"]');
      if (r) r.checked = true;
    }
  };

  q('preset-save').onclick = () => {
    const name = prompt('预设名称');
    if (!name || !name.trim()) return;
    const n = name.trim();
    state.pendingPresets[provider][n] = {
      url: q('url').value.trim(), model: q('model').value.trim(), api_key: q('key').value.trim(),
    };
    if (provider === 'chat') {
      state.pendingPresets[provider][n].context_limit = chatCtx.limitEl ? (parseInt(chatCtx.limitEl.value, 10) || 128000) : 128000;
      state.pendingPresets[provider][n].reserve_output = chatCtx.reserveEl ? (parseInt(chatCtx.reserveEl.value, 10) || 0) : 0;
    }
    if (meta.protocol) {
      const r = pane.querySelector('input[name="' + provider + '-proto"]:checked');
      state.pendingPresets[provider][n].protocol = r ? r.value : '';
    }
    refreshPresetSelect(n);
    toast('预设定为「' + n + '」，点保存生效');
  };

  q('preset-del').onclick = () => {
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
    if (provider === 'vision') {
      out.vision_use_chat = !!q('use-chat').checked;
    }
    return out;
  };
  return pane;
}

/* 上下文与压缩面板 */
function buildContextPane() {
  const cfg = state.config || {};
  const pane = mkPane('context');
  pane.innerHTML =
    hintHtml('估算当前会话离模型上下文上限还有多远，避免「聊到一半突然报错」；达到上限时 AI 会自动压缩历史（总结成摘要，不是删除）。', '上下文 上限 token 压缩 估算') +
    setGroupHtml('容量', 'token 上限 预留 容量') +
    labelHtml('上下文上限（tokens）', '上限 token 上下文') +
    '<input id="context-limit" class="field-input" type="number" min="1000" step="1000">' +
    labelHtml('为回复预留（tokens）', '预留 token 回复') +
    '<input id="context-reserve" class="field-input" type="number" min="0" step="256">' +
    '<details class="fold"><summary>填写说明与估算口径</summary><div class="fold-body">' +
    '<p><strong>只填数字</strong>：128K 填 <code>128000</code>，1M 填 <code>1000000</code>（1000 token ≈ 1700 汉字）。<br>' +
    '「为回复预留」是给模型回答留出的额度，会先从上限里扣掉后再算可用空间。<br>' +
    '请按所用模型的上下文窗口填写（默认 128000 / 4096）；估算为近似值，仅供参考。<br>' +
    '校准系数、进度条阈值与自动压缩的完整规则见「功能说明 → 上下文上限」。</p>' +
    '</div></details>';
  const q = (id) => qIn(pane, id);
  q('context-limit').value = cfg.chat_context_limit != null ? cfg.chat_context_limit : 128000;
  q('context-reserve').value = cfg.chat_reserve_output != null ? cfg.chat_reserve_output : 4096;
  chatCtx.limitEl = q('context-limit');
  chatCtx.reserveEl = q('context-reserve');
  pane._collect = () => ({
    chat_context_limit: parseInt(q('context-limit').value, 10) || 128000,
    chat_reserve_output: parseInt(q('context-reserve').value, 10) || 0,
  });
  return pane;
}

/* 深度思考面板 */
function buildThinkPane() {
  const cfg = state.config || {};
  const pane = mkPane('think');
  pane.innerHTML =
    hintHtml('控制输入框左下「✦ 深度思考」开关与档位如何传给接口，避免不支持的接口报错。', '深度思考 开关 档位 reasoning') +
    setGroupHtml('参数模式', '思考 模式 reasoning effort thinking') +
    labelHtml('思考参数模式', '模式 参数 thinking reasoning') +
    '<select id="think-mode" class="field-input">' +
    '<option value="">自动（按接口地址判断）</option>' +
    '<option value="off">不发送</option>' +
    '<option value="openai">OpenAI 兼容（只发 reasoning_effort）</option>' +
    '<option value="deepseek">DeepSeek（thinking + reasoning_effort）</option>' +
    '</select>' +
    '<details class="fold"><summary>各模式的含义</summary><div class="fold-body">' +
    '<p>DeepSeek：开→ 发 <code>thinking.enabled</code>，关→ 发 <code>thinking.disabled</code>；档位走 <code>reasoning_effort</code>（low / medium 会被 DeepSeek 映射为 high）。<br>' +
    'OpenAI 兼容：开→ 发 <code>reasoning_effort</code>，关→ 不发（该协议无法表达「禁用」）。<br>' +
    '「低 / 中 / 高 / 最高」档位的完整映射表见「功能说明 → 深度思考」。</p>' +
    '</div></details>';
  const q = (id) => qIn(pane, id);
  q('think-mode').value = cfg.chat_think_mode || '';
  chatCtx.thinkEl = q('think-mode');
  pane._collect = () => ({ chat_think_mode: q('think-mode').value });
  return pane;
}

/* ---------------- 余额 / 视觉开关辅助 ---------------- */
async function refreshBalance(provider, pane) {
  const el = pane.querySelector('#' + provider + '-balance');
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
  ['vision-preset', 'vision-preset-save', 'vision-preset-del', 'vision-url',
   'vision-model', 'vision-key', 'vision-show-key'].forEach((id) => {
    const el = qIn(pane, id);
    if (el) el.disabled = disabled;
  });
  pane.querySelectorAll('input[name="vision-proto"]').forEach((el) => { el.disabled = disabled; });
}

function buildSearchPane() {
  const cfg = state.config || {};
  const providers = state.searchProviders || {};
  const pane = mkPane('search');

  let opts = '';
  Object.keys(providers).forEach((k) => {
    opts += '<option value="' + k + '">' + providers[k].name + '</option>';
  });

  pane.innerHTML =
    hintHtml('开启输入框里的「🔍 联网搜索」后，会先把问题改写成多个检索式并行搜索，必要时抓取网页正文，再把结果作为参考资料交给模型，并展示引用来源。完整说明见「功能说明 → 联网搜索」。', '联网 搜索 检索 引用 来源') +
    setGroupHtml('服务商', '服务商 搜索 provider') +
    labelHtml('服务商', '服务商 provider bocha') +
    '<select id="search-provider" class="field-input">' + opts + '</select>' +
    '<p class="hint" id="search-provider-hint"></p>' +
    setGroupHtml('接入', '接口 地址 密钥 api') +
    labelHtml('接口地址', '接口 地址 url endpoint baseurl') +
    '<input id="search-url" class="field-input" type="text">' +
    labelHtml('API 密钥', '密钥 key api token 认证') +
    '<input id="search-key" class="field-input" type="password">' +
    '<label class="check-row"><input type="checkbox" id="search-show-key"> 显示密钥</label>' +
    setGroupHtml('检索行为', '检索 条数 改写 抓取') +
    labelHtml('结果条数（1–20，每个检索式）', '条数 结果 数量 count') +
    '<input id="search-count" class="field-input" type="number" min="1" max="20">' +
    checkRowHtml('search-rewrite', '查询改写（用聊天模型把问题转成 2–3 个检索式，更准）', '查询改写 关键词 改写') +
    checkRowHtml('search-fetch', '抓取正文（对命中网页取正文，而不是只有摘要）', '抓取 正文 网页') +
    labelHtml('抓取正文条数（1–10）', '抓取 条数 count') +
    '<input id="search-fetch-count" class="field-input" type="number" min="1" max="10">';

  const q = (s) => qIn(pane, 'search-' + s);
  q('provider').value = cfg.search_provider || 'bocha';
  q('url').value = cfg.search_url || '';
  q('key').value = cfg.search_api_key || '';
  q('count').value = cfg.search_count || 5;
  q('rewrite').checked = cfg.search_rewrite !== false;
  q('fetch').checked = cfg.search_fetch !== false;
  q('fetch-count').value = cfg.search_fetch_count || 4;

  const syncHint = () => {
    const p = providers[q('provider').value];
    q('provider-hint').textContent = p ? p.hint : '';
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

// 通用加载骨架（占位文案统一观感）
const skeletonHtml = (rows) => {
  let s = '<div class="skeleton">';
  for (let i = 0; i < (rows || 4); i++) s += '<div class="sk-line"></div>';
  return s + '</div>';
};

async function openUsage(rangeKey) {
  const key = USAGE_RANGES[rangeKey] ? rangeKey : 'day7';
  const r = USAGE_RANGES[key];
  showWithAnim($('#usage-modal'));
  $$('#usage-modal .tab').forEach((t) => t.classList.toggle('active', t.dataset.usageRange === key));
  $('#usage-cards').innerHTML = '';
  $('#usage-tables').innerHTML = skeletonHtml(5);
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
  const sel = $('#learning-preset');
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
  const cur = $('#learning-prompt').value.trim();
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
    if (!v) { $('#learning-prompt').value = ''; return; }
    const kind = v.split(':')[0];
    const name = v.slice(kind.length + 1);
    $('#learning-prompt').value = (kind === 'builtin' ? builtin[name] : userPresets[name]) || '';
  };

  $('#learning-preset-save').onclick = () => {
    const text = $('#learning-prompt').value.trim();
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

  $('#learning-preset-del').onclick = () => {
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
  $$('#modal [data-pane="chat"],#modal [data-pane="vision"],#modal [data-pane="search"],#modal [data-pane="context"],#modal [data-pane="think"]').forEach((el) => el.remove());
  const anchor = body.querySelector('[data-pane="profile"]');
  const chatPane = buildApiPane('chat');
  const visionPane = buildApiPane('vision');
  body.insertBefore(chatPane, anchor);
  body.insertBefore(visionPane, anchor);
  body.insertBefore(buildSearchPane(), anchor);
  body.insertBefore(buildContextPane(), anchor);
  body.insertBefore(buildThinkPane(), anchor);

  // 打开设置时自动查一次余额（不阻塞界面）
  refreshBalance('chat', chatPane);
  refreshBalance('vision', visionPane);

  const p = state.profile || {};
  $('#profile-manual').value = p.manual || '';
  $('#profile-auto-learn').checked = p.auto_learn_enabled !== false;
  $('#learning-prompt').value = p.learning_prompt_custom || '';
  buildLearningPresetSelect();

  const theme = (state.config && state.config.theme) || 'light';
  const radio = document.querySelector('input[name="theme"][value="' + theme + '"]');
  if (radio) radio.checked = true;
  // GAL 演出
  const gTheme = (state.config && state.config.gal_theme) || 'sakura';
  const gtr = document.querySelector('input[name="gal-theme"][value="' + gTheme + '"]');
  if (gtr) gtr.checked = true;
  const gMode = (state.config && state.config.gal_seg_mode) || 'append';
  const gmr = document.querySelector('input[name="gal-seg-mode"][value="' + gMode + '"]');
  if (gmr) gmr.checked = true;
  if ($('#gal-auto')) $('#gal-auto').checked = !!(state.config && state.config.gal_auto);
  if ($('#gal-min-show')) $('#gal-min-show').value = String((state.config && state.config.gal_min_show) != null ? state.config.gal_min_show : 0.6);
  if ($('#gal-typewriter')) $('#gal-typewriter').checked = !(state.config && state.config.gal_typewriter === false);
  if ($('#gal-cps')) $('#gal-cps').value = String((state.config && state.config.gal_cps) || 12);
  if ($('#gal-fx')) $('#gal-fx').checked = !(state.config && state.config.gal_fx === false);
  if ($('#gal-show-think')) $('#gal-show-think').checked = !!(state.config && state.config.gal_show_think);
  if ($('#gal-show-exec')) $('#gal-show-exec').checked = !(state.config && state.config.gal_show_exec === false);
  if ($('#gal-dialog-max')) $('#gal-dialog-max').value = String((state.config && state.config.gal_dialog_max) || 46);
  if ($('#gal-sprite-fx')) $('#gal-sprite-fx').checked = !(state.config && state.config.gal_sprite_fx === false);
  if ($('#gal-bar-auto')) $('#gal-bar-auto').checked = !(state.config && state.config.gal_bar_auto === false);
  if ($('#gal-auto-script')) $('#gal-auto-script').checked = !!(state.config && state.config.gal_auto_script);
  if ($('#gal-auto-acts')) $('#gal-auto-acts').value = String((state.config && state.config.gal_auto_script_acts) || 5);
  if ($('#gal-auto-rounds')) $('#gal-auto-rounds').value = String((state.config && state.config.gal_auto_script_rounds) || 4);
  const sp = $('#speech-polish');
  if (sp) sp.checked = !!(state.config && state.config.speech_polish);
  const c0 = state.config || {};
  const te = $('#speech-tts-enabled'); if (te) te.checked = !!c0.tts_enabled;
  const tp = $('#speech-tts-provider'); if (tp) tp.value = c0.tts_provider || 'openai';
  const tu = $('#speech-tts-url'); if (tu) tu.value = c0.tts_url || '';
  const tk = $('#speech-tts-key'); if (tk) tk.value = c0.tts_api_key || '';
  const tm = $('#speech-tts-model'); if (tm) tm.value = c0.tts_model || '';
  const tv = $('#speech-tts-voice'); if (tv) tv.value = c0.tts_voice || '';

  buildSettingsSearchIndex();          // 动态面板刚重建，索引必须在这之后重建
  resetSettingsSearch();
  setSettingsTab('chat');
  showWithAnim($('#modal'));
}

// 设置弹窗：切换左侧导航对应的面板
function setSettingsTab(name) {
  $$('#modal .settings-nav .snav-btn').forEach((b) => b.classList.toggle('active', b.dataset.tab === name));
  $$('#modal .pane').forEach((pn) => pn.classList.toggle('hidden', pn.dataset.pane !== name));
  const content = $('#modal .settings-content');
  if (content) content.scrollTop = 0;
}

/* ---- 设置面板：搜索 + 键盘操作（与「快捷键」面板的说明一一对应）---- */
const SETTINGS_TABS = ['chat', 'vision', 'search', 'context', 'think', 'profile',
                       'learning', 'appearance', 'gal', 'speech', 'keys', 'help'];
let settingsSearchIndex = [];
let settingsSearchHits = [];
let settingsSearchSel = 0;

function buildSettingsSearchIndex() {
  settingsSearchIndex = [];
  $$('#modal .settings-nav .snav-btn').forEach((b) => {
    settingsSearchIndex.push({ pane: b.dataset.tab, el: b, text: b.textContent.trim(), search: b.dataset.search || '' });
  });
  $$('#modal .pane [data-search]').forEach((el) => {
    const pane = el.closest('.pane');
    settingsSearchIndex.push({
      pane: pane ? pane.dataset.pane : '', el: el,
      text: el.textContent.trim().slice(0, 40), search: el.dataset.search || '',
    });
  });
}

function settingsPaneName(pane) {
  const b = $('#modal .settings-nav .snav-btn[data-tab="' + pane + '"]');
  return b ? b.textContent.trim() : (pane || '');
}

function resetSettingsSearch() {
  const inp = $('#settings-search-input');
  const box = $('#settings-search-list');
  if (inp) inp.value = '';
  if (box) { box.classList.add('hidden'); box.innerHTML = ''; }
  settingsSearchHits = [];
  settingsSearchSel = 0;
}

function runSettingsSearch(q) {
  const box = $('#settings-search-list');
  if (!box) return;
  const kws = String(q || '').trim().toLowerCase().split(/\s+/).filter(Boolean);
  if (!kws.length) { box.classList.add('hidden'); box.innerHTML = ''; settingsSearchHits = []; return; }
  const hits = [];
  settingsSearchIndex.forEach((it) => {
    const hay = (it.text + ' ' + it.search).toLowerCase();
    if (!kws.every((k) => hay.includes(k))) return;
    const inText = kws.every((k) => it.text.toLowerCase().includes(k));
    hits.push({ it: it, score: inText ? 0 : 1 });          // 标签正文命中优先于仅同义词命中
  });
  hits.sort((a, b) => a.score - b.score);
  settingsSearchHits = hits.slice(0, 12).map((h) => h.it);
  settingsSearchSel = 0;
  if (!settingsSearchHits.length) {
    box.innerHTML = '<div class="settings-search-empty">无匹配项</div>';
    box.classList.remove('hidden');
    return;
  }
  box.innerHTML = settingsSearchHits.map((it, i) =>
    '<div class="settings-search-item' + (i === 0 ? ' sel' : '') + '" data-i="' + i + '">' +
    '<span class="settings-search-pane">' + escapeHtml(settingsPaneName(it.pane)) + '</span>' +
    '<span class="settings-search-text">' + escapeHtml(it.text || '') + '</span></div>').join('');
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
  setSettingsTab(pane);
  const box = $('#settings-search-list');
  if (box) { box.classList.add('hidden'); box.innerHTML = ''; }
  settingsSearchHits = [];
  requestAnimationFrame(() => {
    const det = el && el.closest ? el.closest('details') : null;
    if (det) det.open = true;
    if (el && el.scrollIntoView) el.scrollIntoView({ block: 'center' });
    flashSettingsTarget(el);
  });
}

// 设置内的键盘操作：Esc 关闭 / Ctrl+S 保存 / ←→ 切分类 / 搜索框内 ↑↓·Enter·Esc
// 用捕获相（true），先于 document 冒泡相上的 GAL、IDE 监听执行，避免互相打架。
function onSettingsKeydown(e) {
  const modal = $('#modal');
  if (!modal || modal.classList.contains('hidden')) return;
  const inp = $('#settings-search-input');
  const inSearch = !!inp && document.activeElement === inp;
  if (e.key === 'Escape') {
    e.preventDefault(); e.stopPropagation();
    if (inSearch && inp.value) { inp.value = ''; runSettingsSearch(''); }
    else closeModal();
    return;
  }
  const mod = e.ctrlKey || e.metaKey;
  const k = (e.key || '').toLowerCase();
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
    const i = cur ? SETTINGS_TABS.indexOf(cur.dataset.tab) : -1;
    if (i < 0) return;
    const next = e.key === 'ArrowRight'
      ? Math.min(SETTINGS_TABS.length - 1, i + 1)
      : Math.max(0, i - 1);
    e.preventDefault();
    setSettingsTab(SETTINGS_TABS[next]);
  }
}

function setupSettingsSearch() {
  const inp = $('#settings-search-input');
  const box = $('#settings-search-list');
  if (!inp || !box) return;
  inp.addEventListener('input', () => runSettingsSearch(inp.value));
  inp.addEventListener('focus', () => { if (inp.value) runSettingsSearch(inp.value); });
  box.addEventListener('click', (e) => {
    const item = e.target.closest('.settings-search-item');
    if (!item) return;
    const it = settingsSearchHits[Number(item.dataset.i)];
    if (it) settingsJumpTo(it.pane, it.el);
  });
}

function closeModal() { hideWithAnim($('#modal')); }

async function saveModal() {
  const cfg = Object.assign({}, state.config);
  $$('#modal .pane').forEach((pane) => {
    if (pane._collect) Object.assign(cfg, pane._collect());
  });
  const tr = document.querySelector('input[name="theme"]:checked');
  if (tr) cfg.theme = tr.value;
  // GAL 演出
  const gtr = document.querySelector('input[name="gal-theme"]:checked');
  if (gtr) cfg.gal_theme = gtr.value;
  const gmr = document.querySelector('input[name="gal-seg-mode"]:checked');
  if (gmr) cfg.gal_seg_mode = gmr.value;
  if ($('#gal-auto')) cfg.gal_auto = !!$('#gal-auto').checked;
  if ($('#gal-min-show')) cfg.gal_min_show = Math.max(0, Math.min(5, parseFloat($('#gal-min-show').value) || 0));
  if ($('#gal-typewriter')) cfg.gal_typewriter = !!$('#gal-typewriter').checked;
  if ($('#gal-cps')) cfg.gal_cps = Math.max(8, Math.min(120, parseInt($('#gal-cps').value, 10) || 12));
  if ($('#gal-fx')) cfg.gal_fx = !!$('#gal-fx').checked;
  if ($('#gal-show-think')) cfg.gal_show_think = !!$('#gal-show-think').checked;
  if ($('#gal-show-exec')) cfg.gal_show_exec = !!$('#gal-show-exec').checked;
  if ($('#gal-dialog-max')) cfg.gal_dialog_max = Math.max(30, Math.min(70, parseInt($('#gal-dialog-max').value, 10) || 46));
  if ($('#gal-sprite-fx')) cfg.gal_sprite_fx = !!$('#gal-sprite-fx').checked;
  if ($('#gal-bar-auto')) cfg.gal_bar_auto = !!$('#gal-bar-auto').checked;
  if ($('#gal-auto-script')) cfg.gal_auto_script = !!$('#gal-auto-script').checked;
  if ($('#gal-auto-acts')) cfg.gal_auto_script_acts = Math.max(1, Math.min(12, parseInt($('#gal-auto-acts').value, 10) || 5));
  if ($('#gal-auto-rounds')) cfg.gal_auto_script_rounds = Math.max(1, Math.min(12, parseInt($('#gal-auto-rounds').value, 10) || 4));
  cfg.speech_polish = !!$('#speech-polish').checked;
  cfg.tts_enabled = !!($('#speech-tts-enabled') || {}).checked;
  if ($('#speech-tts-provider')) cfg.tts_provider = $('#speech-tts-provider').value;
  if ($('#speech-tts-url')) cfg.tts_url = $('#speech-tts-url').value.trim();
  if ($('#speech-tts-key')) cfg.tts_api_key = $('#speech-tts-key').value.trim();
  if ($('#speech-tts-model')) cfg.tts_model = $('#speech-tts-model').value.trim();
  if ($('#speech-tts-voice')) cfg.tts_voice = $('#speech-tts-voice').value.trim();

  const profile = {
    manual: $('#profile-manual').value,
    auto_learn_enabled: $('#profile-auto-learn').checked,
    learning_prompt_custom: $('#learning-prompt').value,
  };
  const wasAutoScript = !!(state.config && state.config.gal_auto_script);   // 保存前的开关状态

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
  galApplyTheme();
  galApplyViewPrefs();
  // 刚开启「AI 自动写剧本」→ 立即为当前会话生成（用户可看到进度）
  // 刚关闭 → 清掉该会话的自动剧本标记（保留已生成的内容，便于再次开启）
  if (state.uiMode === 'gal' && cfg.gal_auto_script !== wasAutoScript) {
    if (cfg.gal_auto_script) setTimeout(() => { galGenerateScript(); }, 260);
    else api('/api/gal/session', { id: state.sessions.current, gal_auto_on: false }).catch(() => {});
  }
  if (state.uiMode === 'gal') { galState.ttsOn = !!cfg.tts_enabled; galSyncTtsBtn(); }
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
  // 原生 UI 跟随主题：color-scheme 影响控件/滚动条，theme-color 影响应用窗口的标题栏
  root.style.colorScheme = t;
  const meta = document.getElementById('theme-color');
  if (meta) meta.setAttribute('content', t === 'dark' ? '#1e1e1e' : '#ffffff');
  // 应用窗口标题栏：由系统绘制，网页改不动，交给本地后端用 DWM 设置（Windows）
  try {
    fetch('/api/window/theme', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Auth-Token': TOKEN },
      body: JSON.stringify({ theme: t, bg: t === 'dark' ? '#1e1e1e' : '#ffffff' }),
    }).catch(() => {});
  } catch (e) { /* 忽略 */ }
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
  syncSendBtn();
}

/* ============================================================
   语音输入（本地实时流式识别：边说边出字 → 可选 AI 矫正 → 填输入框）
   ============================================================ */
const STT_CHUNK_MS = 500;      // 每 0.5 秒把新录音发给后端做一次流式识别

const mic = {
  active: false, busy: false, gen: 0,
  stream: null, ctx: null, source: null, node: null, sink: null,
  pending: [], sampleRate: 48000, timer: null, pusher: null, startedAt: 0,
  sid: '', prefix: '', text: '', sending: false,
};

// 只改文字节点，绝不重写 innerHTML——否则每 500ms 重建子元素会把用户的
// mousedown / mouseup 分到不同节点上，导致 click 丢失（“点一下停不下来”）。
function micSetUI(on, secs) {
  const btn = $('#btn-mic');
  if (!btn) return;
  btn.classList.toggle('recording', on);
  const dot = btn.querySelector('.pill-dot');
  const dtxt = on ? '⏺' : '🎤';
  if (dot && dot.textContent !== dtxt) dot.textContent = dtxt;
  const label = $('#mic-label');
  const ltxt = on ? ('录音中 ' + (secs || 0) + 's') : '语音输入';
  if (label && label.textContent !== ltxt) label.textContent = ltxt;
  btn.title = on ? '录音中…（点一下停止，随后自动矫正）'
    : '语音输入：点一下开始，边说边出字；再点一下停止';
}

function toggleMic() {
  if (mic.busy) return;                 // 启动/停止进行中，忽略连点
  return mic.active ? micStop() : micStart();
}

// 语音降噪开关
function syncDenoisePill() {
  const b = $('#btn-denoise');
  if (b) b.classList.toggle('active', !!state.denoise);
}

function toggleDenoise() {
  state.denoise = !state.denoise;
  syncDenoisePill();
  const cfg = Object.assign({}, state.config || {}, { speech_denoise: state.denoise });
  api('/api/config', cfg).then(() => { state.config = cfg; }).catch(() => {});
  toast(state.denoise ? '已开启语音降噪（嘈杂环境更准）' : '已关闭语音降噪');
}

// 把当前识别文本 + 录音前已有文字写回输入框
function setMicInput() {
  const input = $('#input');
  input.value = (mic.prefix ? mic.prefix + ' ' : '') + (mic.text || '');
  autoGrow();
}

async function sttStart(denoise) {
  const r = await api('/api/stt/start', { denoise: !!denoise });
  if (!r || !r.ok) throw new Error((r && r.error) || '无法开始识别');
  return r.sid;
}

async function sttChunk(sid, buf) {
  const res = await fetch('/api/stt/chunk', {
    method: 'POST',
    headers: { 'Content-Type': 'application/octet-stream', 'X-Auth-Token': TOKEN, 'X-Stt-Sid': sid },
    body: buf,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || !data.ok) throw new Error(data.error || ('HTTP ' + res.status));
  return data.text || '';
}

async function sttFinish(sid) {
  const r = await api('/api/stt/finish', { sid: sid });
  return (r && r.ok) ? (r.text || '') : '';
}

function _concatF32(chunks) {
  let total = 0;
  chunks.forEach((c) => { total += c.length; });
  const all = new Float32Array(total);
  let off = 0;
  chunks.forEach((c) => { all.set(c, off); off += c.length; });
  return all;
}

// 定时把累积的新录音送去识别
async function micPush() {
  if (!mic.active || !mic.sid || mic.sending) return;   // sid 未就绪时先攒着，不丢录音
  const gen = mic.gen;
  const pend = mic.pending;
  mic.pending = [];
  if (!pend.length) return;
  const pcm = encodePcm16(resample(_concatF32(pend), mic.sampleRate, 16000));
  mic.sending = true;
  try {
    const text = await sttChunk(mic.sid, pcm);
    if (gen === mic.gen && mic.active && text) { mic.text = text; setMicInput(); }
  } catch (e) {
    /* 单次失败忽略，下一轮继续 */
  } finally {
    mic.sending = false;
  }
}

async function micStart() {
  if (mic.busy) return;
  mic.busy = true;
  const gen = ++mic.gen;
  try {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      toast('当前浏览器不支持麦克风采集');
      return;
    }
    // 先要麦克风：权限弹窗立即出现，不被后端模型加载拖住
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (e) {
      toast('无法访问麦克风（请检查系统或浏览器权限）');
      return;
    }
    if (gen !== mic.gen) {
      try { stream.getTracks().forEach((t) => t.stop()); } catch (e) { /* 忽略 */ }
      return;
    }
    const AC = window.AudioContext || window.webkitAudioContext;
    const ctx = new AC();
    try { await ctx.resume(); } catch (e) { /* 忽略 */ }
    if (!ctx.createScriptProcessor) {
      try { stream.getTracks().forEach((t) => t.stop()); } catch (e) { /* 忽略 */ }
      try { ctx.close(); } catch (e) { /* 忽略 */ }
      toast('当前浏览器不支持录音采集方式');
      return;
    }
    const source = ctx.createMediaStreamSource(stream);
    const node = ctx.createScriptProcessor(4096, 1, 1);
    const sink = ctx.createGain();
    sink.gain.value = 0;                 // 静音，避免麦克风原声回放到扬声器
    mic.stream = stream; mic.ctx = ctx; mic.source = source; mic.node = node; mic.sink = sink;
    mic.pending = []; mic.sampleRate = ctx.sampleRate || 48000;
    mic.sid = ''; mic.text = ''; mic.sending = false;      // sid 稍后拿到（录音先缓冲，不丢）
    mic.prefix = $('#input').value.replace(/\s*$/, '');
    hideSttChip();
    const sidPromise = sttStart(state.denoise);            // 后端并行加载模型
    node.onaudioprocess = (e) => {
      mic.pending.push(new Float32Array(e.inputBuffer.getChannelData(0)));   // 复制一份
    };
    source.connect(node); node.connect(sink); sink.connect(ctx.destination);
    mic.active = true;
    mic.startedAt = Date.now();
    micSetUI(true, 0);
    mic.timer = setInterval(() => micSetUI(true, Math.round((Date.now() - mic.startedAt) / 1000)), 500);
    mic.pusher = setInterval(micPush, STT_CHUNK_MS);
    mic.busy = false;                    // 已在录音，允许停止 / 再点
    syncSendBtn();
    sidPromise.then((sid) => {
      if (gen === mic.gen && mic.active) mic.sid = sid;
    }).catch((e) => {
      if (gen === mic.gen && mic.active) { toast(e.message || String(e)); micStop(); }
    });
  } finally {
    mic.busy = false;
    syncSendBtn();
  }
}

async function micStop() {
  if (!mic.active || mic.busy) return;
  mic.busy = true;                       // 覆盖「识别 + 矫正」全程：期间禁用发送、不可重录
  const gen = ++mic.gen;                 // 作废所有旧的异步回写
  mic.active = false;
  clearInterval(mic.timer); mic.timer = null;
  clearInterval(mic.pusher); mic.pusher = null;
  micSetUI(false, 0);
  try { mic.node.onaudioprocess = null; } catch (e) { /* 忽略 */ }
  try { mic.source.disconnect(); mic.node.disconnect(); mic.sink.disconnect(); } catch (e) { /* 忽略 */ }
  try { mic.stream.getTracks().forEach((t) => t.stop()); } catch (e) { /* 忽略 */ }
  const rate = mic.sampleRate;
  try { mic.ctx.close(); } catch (e) { /* 忽略 */ }
  const pend = mic.pending; mic.pending = [];
  mic.stream = mic.ctx = mic.source = mic.node = mic.sink = null;
  mic.sending = false;
  const sid = mic.sid; mic.sid = '';
  const prefix = mic.prefix;
  const btn = $('#btn-mic');
  if (btn) btn.classList.add('busy');
  const label = $('#mic-label');
  if (label) label.textContent = '处理中…';
  syncSendBtn();                         // 立即禁用发送键
  try {
    let text = '';
    if (pend.length) {                   // 把最后一段没送出的音频补上
      try { mic.text = (await sttChunk(sid, encodePcm16(resample(_concatF32(pend), rate, 16000)))) || mic.text; }
      catch (e) { /* 忽略 */ }
    }
    text = mic.text || '';
    toast('正在精修识别结果…');           // 两遍识别：离线模型对整段重解码
    try { const f = await sttFinish(sid); if (f) text = f; } catch (e) { /* 忽略 */ }
    text = (text || '').trim();
    mic.text = text;
    setMicInput();
    if (!text) { toast('没听清，请再说一次'); return; }
    // 可选：自动 AI 矫正（此阶段发送键仍禁用，避免「发出去的」与「矫正后的」两段文本并存）
    if (state.config && state.config.speech_polish) {
      toast('正在 AI 矫正…');
      try {
        const r = await api('/api/stt/polish', { text: text });
        if (gen === mic.gen && r && r.ok && r.text && r.text !== text) {
          const inp = $('#input');
          inp.value = (prefix ? prefix + ' ' : '') + r.text;
          autoGrow();
          showSttChip(text, prefix);
        }
      } catch (e) { /* 矫正失败就用原文 */ }
    }
    const input = $('#input');
    input.focus();
    try { input.setSelectionRange(input.value.length, input.value.length); } catch (e) { /* 忽略 */ }
  } finally {
    if (btn) btn.classList.remove('busy');
    micSetUI(false, 0);
    mic.busy = false;
    syncSendBtn();                       // 一切都结束了，按输入内容恢复发送键
  }
}

// 「AI 已矫正 · 撤回原文」提示条
function showSttChip(rawText, prefix) {
  const chip = $('#stt-chip');
  if (!chip) return;
  chip._raw = rawText;
  chip._prefix = prefix || '';
  chip.classList.remove('hidden');
}

function hideSttChip() {
  const chip = $('#stt-chip');
  if (chip) { chip.classList.add('hidden'); chip._raw = ''; chip._prefix = ''; }
}

function revertSttChip() {
  const chip = $('#stt-chip');
  if (!chip || chip._raw == null) return;
  const input = $('#input');
  input.value = (chip._prefix ? chip._prefix + ' ' : '') + chip._raw;
  autoGrow();
  input.focus();
  hideSttChip();
  toast('已恢复原始转写');
}

// 线性插值重采样
function resample(input, inRate, outRate) {
  if (!input.length || inRate === outRate) return input;
  const ratio = inRate / outRate;
  const outLen = Math.max(1, Math.floor(input.length / ratio));
  const out = new Float32Array(outLen);
  for (let i = 0; i < outLen; i++) {
    const pos = i * ratio;
    const i0 = Math.floor(pos);
    const i1 = Math.min(i0 + 1, input.length - 1);
    const frac = pos - i0;
    out[i] = input[i0] * (1 - frac) + input[i1] * frac;
  }
  return out;
}

// float32 [-1,1] → 16-bit 小端 PCM
function encodePcm16(f32) {
  const buf = new ArrayBuffer(f32.length * 2);
  const view = new DataView(buf);
  for (let i = 0; i < f32.length; i++) {
    const s = Math.max(-1, Math.min(1, f32[i]));
    view.setInt16(i * 2, s < 0 ? s * 0x8000 : s * 0x7FFF, true);
  }
  return buf;
}

/* ============================================================
   Agent：会话开关与沙箱目录
   ============================================================ */
function syncAgentUI() {
  const s = curSession();
  const on = !!(s && s.agent_enabled);
  const btn = $('#btn-agent');
  if (btn) {
    btn.classList.toggle('active', on);
    const dot = btn.querySelector('.pill-dot');
    if (dot) dot.textContent = on ? '🛠' : '🛠';
  }
  const sb = $('#btn-sandbox');
  if (sb) {
    sb.classList.toggle('hidden', !on);
    const p = (s && s.sandbox) || '';
    const nm = $('#sandbox-name');
    if (nm) nm.textContent = p ? (p.split(/[\\/]/).filter(Boolean).pop() || p) : '选择目录';
    sb.title = p ? ('沙箱：' + p + '（点击更换）') : '选择 Agent 沙箱目录';
  }
  const or = $('#btn-outside-read');
  if (or) {
    or.classList.toggle('hidden', !on);
    or.classList.toggle('active', !!(on && s && s.allow_outside_read));
  }
}

// 允许 / 禁止 Agent 读取沙箱外的文件（只读，按会话保存）
async function toggleOutsideRead() {
  const s = curSession();
  if (!s) return;
  const on = !s.allow_outside_read;
  try {
    const r = await api('/api/session/agent', { id: s.id, allow_outside_read: on });
    applySessions(r);
  } catch (e) { toast('切换失败：' + e, 'error'); return; }
  syncAgentUI();
  toast(on ? '已允许读取沙箱外文件（只读，写入仍限沙箱内）' : '已关闭沙箱外读取');
}

async function toggleAgent() {
  const s = curSession();
  if (!s) return;
  const on = !s.agent_enabled;
  try {
    const r = await api('/api/session/agent', { id: s.id, agent_enabled: on });
    applySessions(r);
  } catch (e) { toast('切换 Agent 失败：' + e); return; }
  if (on) {
    toast('已开启 Agent，请选择沙箱目录');
    if (!(s.sandbox || '')) pickSandbox();
  } else {
    toast('已关闭 Agent');
  }
  syncAgentUI();
}

async function pickSandbox() {
  toast('请在弹出的窗口中选择目录…');
  try {
    const r = await api('/api/sandbox/pick', { initial: (curSession() || {}).sandbox || '' });
    if (r && r.ok && r.path) {
      const res = await api('/api/session/agent', { id: state.sessions.current, sandbox: r.path });
      applySessions(res);
      syncAgentUI();
      toast('沙箱已设为：' + r.path);
    } else {
      toast('未选择目录');
    }
  } catch (e) { toast('选择目录失败：' + e); }
}

/* ============================================================
   IDE / Solo 模式：以「当前会话沙箱」为根的工作区
   ============================================================ */
/* ============================================================
   GAL 演出模式（见 docs/GAL_MODE_DESIGN.md）
   ============================================================ */
const galState = { characters: [], charId: '', char: null, assetCache: new Map(),
                   lastOptions: [], spriteIndex: null, ttsOn: false, audio: null, skip: false,
                   bgm: '', bgmAudio: null, node: null, statusTimer: null, statusBase: '',
                   ttsWarned: '', ttsQueue: [], ttsRunning: false, segIndex: 0, _audioDone: null,
                   pending: [], segBusy: false, typer: null, fxAudio: null, userEcho: '',
                   showQueue: [], showRunning: false, streamedTurn: false,
                   pendingOptions: null,
                   fast: false, autoOverride: null };

// 立绘图片：带鉴权取回 → 缓存 Blob URL（token 不进 URL）
function galAssetUrl(charId, path) {
  const key = charId + '/' + path;
  if (galState.assetCache.has(key)) return Promise.resolve(galState.assetCache.get(key));
  return fetch('/api/gal/asset', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Auth-Token': TOKEN },
    body: JSON.stringify({ id: charId, path: path }),
  }).then((res) => {
    if (!res.ok) throw new Error('HTTP ' + res.status);
    return res.blob();
  }).then((blob) => {
    const url = URL.createObjectURL(blob);
    galState.assetCache.set(key, url);
    return url;
  });
}

async function galLoadCharacters() {
  try {
    const r = await api('/api/gal/characters', {});
    galState.characters = (r && r.characters) || [];
  } catch (e) { galState.characters = []; }
  galRenderCharBtn();
  galRenderCharCards();
}

// 工具条上的当前角色按钮（头像 + 名字）
function galRenderCharBtn() {
  const btn = $('#gal-char-btn');
  if (!btn) return;
  const c = galState.char;
  const nm = $('#gal-char-btn-name');
  if (nm) nm.textContent = c ? (c.name || c.id) : '选择角色';
  const av = $('#gal-char-btn-avatar');
  if (av) {
    const path = (c && (c.sprites || [])[0]) ? c.sprites[0].path : '';
    if (c && path) {
      av.style.display = '';
      galAssetUrl(c.id, path).then((u) => { av.src = u; })
        .catch(() => { av.removeAttribute('src'); av.style.display = 'none'; });
    } else { av.removeAttribute('src'); av.style.display = 'none'; }
  }
  if (c && c.color) btn.style.setProperty('--gal-char-color', c.color);
  else btn.style.removeProperty('--gal-char-color');
}

// 角色卡片面板（缩略图网格 + 搜索）
function galRenderCharCards() {
  const box = $('#gal-chars-list');
  if (!box) return;
  const q = (($('#gal-chars-search') || {}).value || '').trim().toLowerCase();
  const cur = galState.charId || '';
  const list = galState.characters.filter((c) =>
    !q || String(c.name || '').toLowerCase().includes(q) || String(c.id || '').toLowerCase().includes(q));
  const cnt = $('#gal-chars-count');
  if (cnt) cnt.textContent = list.length + ' / ' + galState.characters.length;
  box.innerHTML = '';
  if (!list.length) {
    const em = document.createElement('div');
    em.className = 'gal-chars-empty';
    em.textContent = galState.characters.length ? '没有匹配的角色' : '还没有角色：点「📦 导入」或「＋ 新建」';
    box.appendChild(em);
    return;
  }
  list.forEach((c) => {
    const card = document.createElement('button');
    card.className = 'gal-char-card' + (c.id === cur ? ' active' : '');
    if (c.color) card.style.setProperty('--gal-char-color', c.color);
    const img = document.createElement('img');
    img.className = 'gal-char-thumb'; img.alt = '';
    if (c.default_sprite) {
      galAssetUrl(c.id, c.default_sprite).then((u) => { img.src = u; }).catch(() => { img.classList.add('empty'); });
    } else { img.classList.add('empty'); }
    const meta = document.createElement('div');
    meta.className = 'gal-char-meta';
    const nm = document.createElement('div'); nm.className = 'gal-char-name'; nm.textContent = c.name || c.id;
    const sub = document.createElement('div'); sub.className = 'gal-char-sub'; sub.textContent = (c.sprite_count || 0) + ' 张立绘';
    meta.appendChild(nm); meta.appendChild(sub);
    card.appendChild(img); card.appendChild(meta);
    card.onclick = () => { galSetCharacter(c.id); galToggleChars(false); };
    box.appendChild(card);
  });
}

function galToggleChars(force) {
  const p = $('#gal-chars');
  if (!p) return;
  const show = (typeof force === 'boolean') ? force : p.classList.contains('hidden');
  p.classList.toggle('hidden', !show);
  if (show) {
    galRenderCharCards();
    galRefreshPacksHint();
    const s = $('#gal-chars-search');
    if (s) { s.value = ''; s.focus(); }
  }
}

/* ---- AI 剧本：查看大纲 / 生成 / 重新生成 ---- */
function galToggleScript(force) {
  const p = $('#gal-script-panel');
  if (!p) return;
  const show = (typeof force === 'boolean') ? force : p.classList.contains('hidden');
  p.classList.toggle('hidden', !show);
  if (show) galRenderScript();
}

async function galRenderScript() {
  const list = $('#gal-script-list');
  const st = $('#gal-script-status');
  if (!list) return;
  list.innerHTML = '<div class="gal-review-empty">读取中…</div>';
  let r = null;
  try { r = await api('/api/gal/script', { id: state.sessions.current }); }
  catch (e) { list.innerHTML = '<div class="gal-review-empty">读取失败：' + escapeHtml(String(e)) + '</div>'; return; }
  const story = (r && r.story) || {};
  const nodes = (story && story.nodes) || [];
  const outline = (r && r.outline) || '';
  const curId = (r && r.node_id) || '';
  if (st) {
    st.textContent = (r && r.on)
      ? ('已启用 · 已展开 ' + nodes.length + ' 幕')
      : (nodes.length ? '未启用' : '尚未生成');
  }
  list.innerHTML = '';
  if (!outline && !nodes.length) {
    const em = document.createElement('div');
    em.className = 'gal-review-empty';
    em.textContent = '还没有剧本。点右上「✨ 生成 / 重新生成」，AI 会依据角色设定与用户画像编写大纲。';
    list.appendChild(em);
    return;
  }
  if (outline) {
    const d = document.createElement('div');
    d.className = 'gal-review-row';
    const who = document.createElement('span');
    who.className = 'gal-review-who'; who.textContent = '大纲';
    d.appendChild(who);
    const b = document.createElement('div');
    b.className = 'gal-review-body';
    b.innerHTML = renderMarkdown(outline);
    d.appendChild(b);
    list.appendChild(d);
  }
  nodes.forEach((n, i) => {
    const d = document.createElement('div');
    d.className = 'gal-review-row';
    const who = document.createElement('span');
    who.className = 'gal-review-who';
    who.textContent = ('第 ' + (i + 1) + ' 幕') + (n.id === curId ? ' · 当前' : '');
    d.appendChild(who);
    const b = document.createElement('div');
    b.className = 'gal-review-body';
    const flag = n.type === 'ending' ? '（结局）' : ('（' + (n.maxRounds || 0) + ' 轮）');
    b.innerHTML = renderMarkdown('**' + escapeHtml(n.id) + '** ' + escapeHtml(flag) + ' ' + (n.prompt || ''));
    d.appendChild(b);
    list.appendChild(d);
  });
}

async function galGenerateScript() {
  const cid = galState.charId || (curSession() || {}).gal_character_id || '';
  if (!cid) { toast('请先选择角色', 'error'); return; }
  const st = $('#gal-script-status');
  const btn = $('#gal-script-gen');
  const old = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = '构思中…'; }
  if (st) st.textContent = '构思剧情中…';
  toast('AI 正在构思剧情…');
  try {
    const r = await api('/api/gal/script', {
      action: 'generate', id: state.sessions.current, character_id: cid,
    });
    if (r && r.node_id) galSetNode({ node_id: r.node_id, round: 0, max_rounds: 0 });
    const first = ((r && r.story && r.story.nodes) || []).find((n) => n.id === (r && r.node_id));
    if (first && first.prompt) galAddSegment({ narration: first.prompt });   // 首幕场景进对话框
    await galRenderScript();
    toast('剧本已生成，开始演出吧');
  } catch (e) {
    toast('生成失败：' + e, 'error');
    await galRenderScript();
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = old; }
  }
}

/* ---- 对话回顾：覆盖在舞台上的整段对话记录 ---- */
function galToggleReview(force) {
  const p = $('#gal-review-panel');
  if (!p) return;
  const show = (typeof force === 'boolean') ? force : p.classList.contains('hidden');
  p.classList.toggle('hidden', !show);
  if (show) galRenderReview();
}

function galRenderReview() {
  const list = $('#gal-review-list');
  const cnt = $('#gal-review-count');
  if (!list) return;
  const s = curSession();
  const msgs = (s && s.messages) || [];
  list.innerHTML = '';
  if (cnt) cnt.textContent = msgs.length ? (msgs.length + ' 条记录') : '';
  if (!msgs.length) {
    const em = document.createElement('div');
    em.className = 'gal-review-empty';
    em.textContent = '还没有对话记录';
    list.appendChild(em);
    return;
  }
  const name = (galState.char && galState.char.name) || '';
  msgs.forEach((m) => {
    const role = m.role || '';
    if (role !== 'user' && role !== 'assistant') return;      // 跳过系统 / 工具消息
    const row = document.createElement('div');
    row.className = 'gal-review-row' + (role === 'user' ? ' me' : '');
    const who = document.createElement('span');
    who.className = 'gal-review-who';
    who.textContent = role === 'user' ? '你' : (name || '角色');
    row.appendChild(who);
    const body = document.createElement('div');
    body.className = 'gal-review-body';
    const segs = (m.gal && Array.isArray(m.gal.dialog) && m.gal.dialog.length) ? m.gal.dialog : null;
    if (segs) {
      segs.forEach((sg) => {
        const isNarr = !sg.speech && sg.narration;
        const d = document.createElement('div');
        d.className = 'gal-review-seg' + (isNarr ? ' narr' : '');
        d.innerHTML = renderMarkdown(isNarr ? ('（' + sg.narration + '）') : (sg.speech || ''));
        body.appendChild(d);
      });
    } else {
      body.innerHTML = renderMarkdown(m.content || '');
    }
    row.appendChild(body);
    list.appendChild(row);
  });
  list.scrollTop = list.scrollHeight;
}

// 演出主题（全部 UI）：挂在 <html> 上，核心 token 就地覆盖，
// 因此 #app 之外的设置 / 知识库 / 笔记 / 用量弹窗与 toast 也会跟随
function galApplyTheme() {
  const root = document.documentElement;
  if (state.uiMode !== 'gal') { root.removeAttribute('data-gal-theme'); return; }
  const t = String(galCfg('gal_theme', 'sakura'));
  root.setAttribute('data-gal-theme', (t === 'night' || t === 'washi') ? t : 'sakura');
}

// GAL 视图偏好：对白框高度 / 立绘过渡 / 工具条沉浸淡出（设置保存后立即生效）
function galApplyViewPrefs() {
  const c = state.config || {};
  const d = $('#gal-dialog');
  if (d) {
    const h = Math.max(30, Math.min(70, parseInt(c.gal_dialog_max, 10) || 46));
    d.style.maxHeight = h + '%';
  }
  const root = document.documentElement;
  root.classList.toggle('gal-no-sprite-fx', c.gal_sprite_fx === false);
  root.classList.toggle('gal-bar-static', c.gal_bar_auto === false);
}

// 角色主题色：应用到角色按钮、舞台与对话框名字
function galApplyCharColor() {
  const color = (galState.char && galState.char.color) || '';
  const stage = $('#gal-panel');
  if (stage) {
    if (color) stage.style.setProperty('--gal-char-color', color);
    else stage.style.removeProperty('--gal-char-color');
  }
  const nm = $('#gal-name');
  if (nm) {
    nm.style.color = '';                          // 文字色交给主题，保证对比度
    nm.style.background = color || '';            // 角色主题色用作名牌底色
  }
}

async function galSetCharacter(cid) {
  galState.charId = cid || '';
  galState.char = null;
  if (cid) {
    try { const r = await api('/api/gal/character/get', { id: cid }); galState.char = (r && r.character) || null; }
    catch (e) { galState.char = null; }
    try {
      applySessions(await api('/api/gal/session', {
        id: state.sessions.current, gal_enabled: true, gal_character_id: cid,
      }));
    } catch (e) { /* 忽略 */ }
  }
  const nm = $('#gal-role-name');
  if (nm) nm.textContent = '';
  if (galState.char && (galState.char.sprites || []).length) galShowSprite(galState.char.sprites[0].path, 0);
  else { const box = $('#gal-sprites'); if (box) box.innerHTML = ''; }
  galRenderCharBtn();
  galApplyCharColor();
  galRenderTtsPresets();
}

/* ---- 角色管理：删除 / 导入 / 导出 ---- */
function fileToB64(file) {
  return new Promise((resolve, reject) => {
    const fr = new FileReader();
    fr.onload = () => resolve(String(fr.result).split(',')[1] || '');
    fr.onerror = () => reject(new Error('读取失败'));
    fr.readAsDataURL(file);
  });
}

async function galDeleteCharacter() {
  const cid = galState.charId || ((curSession() || {}).gal_character_id) || '';
  if (!cid) { toast('请先选择要删除的角色'); return; }
  const c = galState.characters.find((x) => x.id === cid);
  const label = (c && c.name) || cid;
  if (!confirm('删除角色「' + label + '」？其立绘与剧本会一并删除，且不可恢复。')) return;
  try {
    const r = await api('/api/gal/character/delete', { id: cid });
    galState.characters = (r && r.characters) || [];
    await galSetCharacter('');
    galRenderCharBtn(); galRenderCharCards();
    toast('已删除角色：' + label);
  } catch (e) { toast('删除失败：' + e, 'error'); }
}

async function galExportCharacter() {
  const cid = galState.charId || ((curSession() || {}).gal_character_id) || '';
  if (!cid) { toast('请先选择要导出的角色'); return; }
  try {
    const res = await fetch('/api/gal/character/export', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Auth-Token': TOKEN },
      body: JSON.stringify({ id: cid }),
    });
    if (!res.ok) {
      let msg = 'HTTP ' + res.status;
      try { msg = (await res.json()).error || msg; } catch (e) { /* 忽略 */ }
      toast('导出失败：' + msg, 'error');
      return;
    }
    const cd = res.headers.get('Content-Disposition') || '';
    const m = /filename\*=UTF-8''([^;]+)/i.exec(cd);
    const name = m ? decodeURIComponent(m[1]) : (cid + '.char');
    const a = document.createElement('a');
    a.href = URL.createObjectURL(await res.blob());
    a.download = name;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 4000);
    toast('已导出：' + name);
  } catch (e) { toast('导出失败：' + e, 'error'); }
}

async function galImportPacks(files) {
  const list = Array.from(files || []);
  if (!list.length) return;
  let ok = 0, lastId = '', skippedModel = false, speech = 0;
  for (const f of list) {
    try {
      const b64 = await fileToB64(f);
      const r = await api('/api/gal/character/import', { filename: f.name, data: b64 });
      ok++;
      if (r.character && r.character.id) lastId = r.character.id;
      if (r.skipped_voice_model) skippedModel = true;
      speech += (r.speech_files || 0);
    } catch (e) { toast('「' + f.name + '」导入失败：' + e, 'error'); }
  }
  if (ok) {
    await galLoadCharacters();
    if (lastId) await galSetCharacter(lastId);
    galToggleChars(true);
    toast('已导入 ' + ok + ' 个角色'
          + (speech ? '（含 ' + speech + ' 条原声台词）' : '')
          + (skippedModel ? '，已忽略体积较大的语音模型' : ''));
  }
}

// 批量导入 data/gal/packs/ 下的角色包（你手动下载的 .char 放这里即可）
async function galImportPacksDir() {
  try {
    const r = await api('/api/gal/character/import_dir', {});
    galState.characters = (r && r.characters) || galState.characters;
    galRenderCharBtn(); galRenderCharCards(); galRefreshPacksHint();
    const n = (r && r.imported) || 0;
    if (n) {
      if (r.ids && r.ids.length) await galSetCharacter(r.ids[r.ids.length - 1]);
      toast('已从 packs 目录导入 ' + n + ' 个角色');
    } else {
      toast('packs 目录里没有可导入的 .char / .zip');
    }
    if (r && r.failed && r.failed.length) toast('部分失败：' + r.failed.join('；'), 'error');
  } catch (e) { toast('批量导入失败：' + e, 'error'); }
}

async function galRefreshPacksHint() {
  const el = $('#gal-packs-hint');
  if (!el) return;
  try {
    const r = await api('/api/gal/packs', {});
    const n = ((r && r.packs) || []).length;
    el.textContent = n
      ? ('data/gal/packs/ 里有 ' + n + ' 个待导入角色包，点「📂 导入 packs 目录」即可')
      : '把社区下载的 .char / .zip 放到 data/gal/packs/，再点「📂 导入 packs 目录」（会解出原版立绘、参考音频与原声台词）';
  } catch (e) { /* 忽略 */ }
}

function galShowSprite(path, index) {
  const box = $('#gal-sprites');
  if (!box) return;
  if (typeof index === 'number') galState.spriteIndex = index;
  if (!path) { box.innerHTML = ''; return; }
  const key = galState.charId + '/' + path;
  const cur = box.querySelector('img.gal-sprite');
  if (cur && cur.dataset.key === key) return;      // 同一张立绘：不重播动画
  const scale = (galState.char && galState.char.sprite_scale) || 1;
  galAssetUrl(galState.charId, path).then((u) => {
    const img = document.createElement('img');
    img.className = 'gal-sprite';
    img.alt = '';
    img.dataset.key = key;
    img.style.maxHeight = Math.round(92 * scale) + '%';
    img.style.opacity = '0';
    img.style.transform = 'translateY(14px) scale(.985)';
    img.addEventListener('load', () => {
      img.style.opacity = '1';
      img.style.transform = 'none';
      // 旧立绘淡出后再移除 → 形成交叉过渡
      Array.from(box.querySelectorAll('img.gal-sprite')).forEach((el) => {
        if (el === img) return;
        el.style.opacity = '0';
        el.style.transform = 'translateY(10px)';
        setTimeout(() => { if (el.parentNode) el.parentNode.removeChild(el); }, 420);
      });
    }, { once: true });
    img.src = u;
    box.appendChild(img);
  }).catch(() => { /* 资源缺失时保持上一张 */ });
}

function galResetDialog() {
  const t = $('#gal-text'); if (t) { t.textContent = ''; t.classList.remove('md'); }
  const n = $('#gal-name'); if (n) n.textContent = galState.char ? galState.char.name : '';
  const o = $('#gal-options'); if (o) { o.classList.remove('gal-options-out'); o.innerHTML = ''; }
  const tl = $('#gal-tools'); if (tl) tl.innerHTML = '';
  galState.segIndex = 0;
  galState.ttsQueue.length = 0;
  galState.pending.length = 0;
  galState.pendingOptions = null;
  galState.showQueue.length = 0; galState.showRunning = false;
  galState.segBusy = false;
  if (galState.typer) { galState.typer.finish(); galState.typer = null; }
  if (galState.fxAudio) { try { galState.fxAudio.pause(); } catch (e) { /* 忽略 */ } galState.fxAudio = null; }
  galThinkClear();
  galSetStatus('');
  galSetTyping(false);
}

/* 你上一句的回显（暗淡色）：让用户看到 AI 在回复什么 */
function galSetUserEcho(text) {
  const el = $('#gal-user-echo');
  const t = String(text || '').trim();
  galState.userEcho = t;
  if (!el) return;
  if (!t) { el.classList.add('hidden'); el.textContent = ''; return; }
  el.classList.remove('hidden');
  el.innerHTML = '';
  const who = document.createElement('span');
  who.className = 'gal-user-echo-who';
  who.textContent = '你：';
  el.appendChild(who);
  el.appendChild(document.createTextNode(t));
}

/* ---- 演出配置读取 ---- */
function galCfg(k, dflt) {
  const v = (state.config || {})[k];
  return (v === undefined || v === null) ? dflt : v;
}
function galSegMode() { return galCfg('gal_seg_mode', 'append') === 'single' ? 'single' : 'append'; }
function galMinShowMs() {
  const v = parseFloat(galCfg('gal_min_show', 0.6));
  return (isFinite(v) && v > 0 ? v : 0) * 1000;
}

/* ---- 逐段对白：追加 or 单行推进；打字机；关键词特效 ---- */
function galBuildSegEl(obj) {
  const t = $('#gal-text');
  if (!t) return null;
  t.classList.remove('md');
  const isNarr = !!obj.narration;
  const raw = isNarr ? ('（' + obj.narration + '）') : (obj.text || '');
  if (!raw) return null;
  const seg = document.createElement('div');
  seg.className = 'gal-seg' + (isNarr ? ' narr' : '');
  const nm = String(obj.name || '').trim();
  const sp = String(obj.speaker || '').trim();
  if (!isNarr && sp && sp !== nm) {                 // 换了说话人时才标名字
    const who = document.createElement('span');
    who.className = 'gal-seg-who';
    who.textContent = sp + '：';
    seg.appendChild(who);
  }
  const body = document.createElement('span');
  body.className = 'gal-seg-text';
  body.textContent = raw;
  seg.appendChild(body);
  return { el: seg, body: body, text: raw, narr: isNarr };
}

// 对白框（#gal-dialog）才是真正的滚动容器；输出 / 打字过程中保持贴底
function galScrollDialog() {
  const d = $('#gal-dialog');
  if (d) d.scrollTop = d.scrollHeight;
}

// 立绘层底边 = 对白框顶边 + 间距 → 立绘永远不会被对白框 / 选项压住
function galSyncSpriteLift() {
  const stage = document.querySelector('#gal-panel .gal-stage');
  const d = $('#gal-dialog');
  if (!stage || !d) return;
  const lift = stage.getBoundingClientRect().bottom - d.getBoundingClientRect().top + 10;
  stage.style.setProperty('--gal-sprite-lift', Math.max(0, Math.round(lift)) + 'px');
}

// 打字机：逐字显示；点击可提前补全（返回 Promise，resolve 表示显示完成）
function galTypewrite(body, text) {
  return new Promise((resolve) => {
    const cps = Math.max(8, parseInt(galCfg('gal_cps', 12), 10) || 12);
    if (galCfg('gal_typewriter', true) === false || cps >= 200 || !text) {
      body.textContent = text; galScrollDialog(); resolve(); return;
    }
    const chars = Array.from(text);
    const perChar = 1000 / cps;                    // 每字毫秒：速度设置全程线性生效
    let shown = 0, carry = 0, last = performance.now();
    body.textContent = '';
    const h = { timer: null, finish: null };
    h.finish = () => {
      clearInterval(h.timer);
      body.textContent = text;
      galScrollDialog();
      if (galState.typer === h) galState.typer = null;
      resolve();
    };
    h.timer = setInterval(() => {
      const now = performance.now();
      carry += Math.min(now - last, 100) * (galState.fast ? 6 : 1);   // 按住 Ctrl：快进；单帧最多补 100ms，防切页回来一次性灌满
      last = now;
      const add = Math.floor(carry / perChar);
      if (add <= 0) return;
      carry -= add * perChar;
      shown = Math.min(chars.length, shown + add);
      body.textContent = chars.slice(0, shown).join('');
      galScrollDialog();
      if (shown >= chars.length) h.finish();
    }, 16);
    galState.typer = h;
  });
}

// 关键词特效：CSS 动画 + 可选音效（before/after/loop/stop）
function galPlayFx(list) {
  if (!list || !list.length) return;
  if (galCfg('gal_fx', true) === false) return;
  const layer = $('#gal-fx-layer');
  const stage = document.querySelector('#gal-panel .gal-stage');
  list.forEach((fx) => {
    if (fx.css === 'flash' || fx.css === 'blackout') {
      if (layer) { layer.className = ''; void layer.offsetWidth; layer.className = 'fx-' + fx.css; }
    } else if (fx.css) {
      if (stage) {
        const cls = 'fx-' + fx.css;
        stage.classList.remove(cls); void stage.offsetWidth; stage.classList.add(cls);
        setTimeout(() => stage.classList.remove(cls), 900);
      }
    }
    if (fx.audio) galPlayFxAudio(fx);
  });
}
function galPlayFxAudio(fx) {
  const stop = () => { if (galState.fxAudio) { try { galState.fxAudio.pause(); } catch (e) { /* 忽略 */ } galState.fxAudio = null; } };
  if (fx.mode === 'stop') { stop(); return; }
  fetch('/api/gal/effect/audio', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Auth-Token': TOKEN },
    body: JSON.stringify({ path: fx.audio }),
  }).then((r) => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.blob(); })
    .then((b) => {
      if (fx.mode === 'loop') stop();
      const a = new Audio(URL.createObjectURL(b));
      if (fx.mode === 'loop') { a.loop = true; galState.fxAudio = a; }
      a.play().catch(() => { /* 需用户交互后才可自动播放 */ });
    }).catch(() => { /* 音效缺失时静默 */ });
}

function galAddSegment(obj) {
  if (galSegMode() === 'single') { galState.pending.push(obj); galShowNext(); return; }
  galState.showQueue.push(obj);
  if (!galState.showRunning) galPumpShow();
}

// 追加模式：排队逐段显示——每段打字完再出下一段（不再瞬间补全上一段）
async function galPumpShow() {
  if (galState.showRunning) return;
  galState.showRunning = true;
  try {
    while (galState.showQueue.length) {
      const obj = galState.showQueue.shift();
      const seg = galBuildSegEl(obj);
      if (!seg) continue;
      const t = $('#gal-text');
      if (!t) break;
      t.appendChild(seg.el);
      galScrollDialog();
      galState.segIndex = obj.index || (galState.segIndex + 1);
      if (obj.path) galShowSprite(obj.path, typeof obj.sprite_index === 'number' ? obj.sprite_index : undefined);
      if (obj.fx && obj.fx.length) galPlayFx(obj.fx);
      await galTypewrite(seg.body, seg.text);
      if (!seg.narr && obj.text) galEnqueueSpeech(obj.text);
    }
  } finally {
    galState.showRunning = false;
    galFlushOptions();                     // 本轮段落都出完了 → 若有暂存选项则显示
  }
}

// 单行推进：一次只显示一句，点击 / 语音播完（自动播放）切下一句
async function galShowNext() {
  if (galState.segBusy) return;
  const obj = galState.pending.shift();
  if (!obj) return;
  const t = $('#gal-text');
  if (!t) return;
  galState.segBusy = true;
  t.innerHTML = '';
  t.classList.remove('md');
  const seg = galBuildSegEl(obj);
  if (!seg) { galState.segBusy = false; galShowNext(); return; }
  t.appendChild(seg.el);
  galScrollDialog();
  galState.segIndex = obj.index || (galState.segIndex + 1);
  if (obj.path) galShowSprite(obj.path, typeof obj.sprite_index === 'number' ? obj.sprite_index : undefined);
  const startedAt = Date.now();
  await galTypewrite(seg.body, seg.text);
  if (obj.fx && obj.fx.length) galPlayFx(obj.fx);
  if (!seg.narr && obj.text && galState.ttsOn && !galState.skip) await galSpeak(obj.text);
  const rest = galState.fast ? 0 : Math.max(0, galMinShowMs() - (Date.now() - startedAt));
  if (rest > 0) await new Promise((r) => setTimeout(r, rest));
  galState.segBusy = false;
  // 自动播放：本句播完后再等 1.5s 才切下一句，给读者留出喘息
  if (galAutoOn() && galState.pending.length && !galState.skip) setTimeout(galShowNext, 1500);
  else galFlushOptions();                  // 本句是最后一句且非自动播放 → 显示暂存的选项
}

// 推进：打字中 → 立即补全；否则单行模式切下一句（点击舞台 / 空格 / 回车 都走这里）
function galAdvance() {
  if (galState.typer) { galState.typer.finish(); return; }
  if (galSegMode() === 'single' && !galState.segBusy && galState.pending.length) galShowNext();
}

// 朗读队列：追加模式下按段顺序播，段与段之间不重叠
function galEnqueueSpeech(text) {
  if (!galState.ttsOn || galState.skip || !text) return;
  galState.ttsQueue.push(text);
  if (!galState.ttsRunning) galTtsPump();
}
function galSetSkipVisible(on) {
  const b = $('#gal-skip');
  if (b) b.classList.toggle('hidden', !on);
}

/* ---- 自动播放（工具条「▶ 自动」/ A 键）---- */
function galAutoOn() {
  return (galState.autoOverride == null) ? !!galCfg('gal_auto', false) : !!galState.autoOverride;
}
function galSyncAutoBtn() {
  const b = $('#gal-auto-btn');
  if (b) b.classList.toggle('active', galAutoOn());
}
function galToggleAuto() {
  galState.autoOverride = !galAutoOn();
  galSyncAutoBtn();
  toast('自动播放：' + (galState.autoOverride ? '开' : '关'));
}

/* ---- GAL 演出快捷键 ----
   空格 / 回车：推进（打字中先补全）；按住 Ctrl：快进；A：自动播放；↑：打开回顾 */
function galHotkeys(e) {
  if (state.uiMode !== 'gal') return;
  const t = e.target;
  if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
  const modal = $('#gal-modal');
  if (modal && !modal.classList.contains('hidden')) return;      // 编辑器打开时不拦截
  const setModal = $('#modal');
  if (setModal && !setModal.classList.contains('hidden')) return;   // 设置弹窗打开时不推进剧情
  if (e.key === ' ' || e.key === 'Enter') { e.preventDefault(); galAdvance(); return; }
  if (e.key === 'ArrowUp') {
    const rp = $('#gal-review-panel');
    if (rp && rp.classList.contains('hidden')) { e.preventDefault(); galToggleReview(true); }
    return;
  }
  if ((e.key === 'a' || e.key === 'A') && !e.ctrlKey && !e.metaKey && !e.altKey) {
    e.preventDefault(); galToggleAuto();
  }
}
async function galTtsPump() {
  if (galState.ttsRunning) return;
  galState.ttsRunning = true;
  galSetSkipVisible(true);        // 正在朗读时才露出「跳过」
  try {
    while (galState.ttsQueue.length && !galState.skip) {
      const piece = galState.ttsQueue.shift();
      await galSpeak(piece);
    }
  } finally {
    galState.ttsRunning = false;
    galSetSkipVisible(false);
  }
}

function galRenderOptions(options) {
  const box = $('#gal-options');
  if (!box) return;
  galState.lastOptions = (options || []).slice();
  box.classList.remove('gal-options-out');
  box.innerHTML = '';
  galState.lastOptions.forEach((op, i) => {
    const b = document.createElement('button');
    b.className = 'gal-option';
    b.style.animationDelay = (i * 45) + 'ms';     // 逐条错开淡入
    b.textContent = (i + 1) + '. ' + (op.text || '');
    b.onclick = () => galSendUserText(op.text || '');
    box.appendChild(b);
  });
}

// 选项延迟显示：等本轮「流结束 + 没有在打字 + 待显示队列为空」才真正渲染，
// 否则用户会点到还没就绪的选项（点击被 state.streaming 吞掉，表现为"点了没反应、选项还浮着"）。
function galFlushOptions() {
  if (!galState.pendingOptions || !galState.pendingOptions.length) return;
  if (state.streaming || galState.segBusy || galState.pending.length) return;
  const opts = galState.pendingOptions;
  galState.pendingOptions = null;
  galRenderOptions(opts);
}

// 选项区淡出：动画结束（或 reduced-motion 立即）后清空，然后回调 done（保证只调一次）
function galHideOptions(done) {
  const box = $('#gal-options');
  let closed = false;
  const finish = () => { if (!closed) { closed = true; if (typeof done === 'function') done(); } };
  if (!box || !box.childElementCount) { if (box) box.innerHTML = ''; finish(); return; }
  const onEnd = (e) => { if (e.target === box) clear(); };   // 只认容器自身的淡出（子按钮也有动画）
  const clear = () => {
    box.removeEventListener('animationend', onEnd);
    box.classList.remove('gal-options-out');
    box.innerHTML = '';
    finish();
  };
  box.classList.add('gal-options-out');
  box.addEventListener('animationend', onEnd);
  setTimeout(clear, 400);                       // 兜底：动画事件未触发时也要清掉
}

function galSendUserText(text) {
  if (!text || state.streaming) return;
  const instant = !(state.config && state.config.gal_options_instant === false);
  const input = $('#input');
  if (input) input.value = text;
  // 先让选项淡出，再真正发送：sendMessage → galResetDialog 会立刻清空选项区，
  // 若同步发送就会把淡出动画掐掉（看起来还是"瞬间消失"）。
  galHideOptions(() => {
    if (instant) { galSetStatus('思考中…'); sendMessage(); }
    else { if (input) input.focus(); autoGrow(); syncSendBtn(); }
  });
}

// 从会话历史渲染舞台（进入 GAL 模式 / 切换会话 / 一轮结束后）
function galRenderFromSession() {
  const s = curSession();
  if (!s) return;
  if (s.gal_character_id && s.gal_character_id !== galState.charId) {
    galSetCharacter(s.gal_character_id);
    return;
  }
  const msgs = s.messages || [];
  let last = null, lastUser = '';
  for (let i = msgs.length - 1; i >= 0; i--) {
    if (msgs[i].role === 'assistant') {
      last = msgs[i];
      for (let k = i - 1; k >= 0; k--) {          // 该回复之前最近的一条用户消息
        if (msgs[k].role === 'user') { lastUser = msgs[k].content || ''; break; }
      }
      break;
    }
  }
  galResetDialog();
  galSetUserEcho(lastUser);
  if (!last) return;
  const g = last.gal || null;
  const sprites = (galState.char && galState.char.sprites) || [];
  if (g && typeof g.sprite_index === 'number' && sprites[g.sprite_index]) {
    galShowSprite(sprites[g.sprite_index].path, g.sprite_index);
  }
  const tbox = $('#gal-text');
  if (tbox) {
    tbox.innerHTML = '';
    tbox.classList.add('md');
    const nm = (galState.char && galState.char.name) || '';
    const segs = (g && Array.isArray(g.dialog) && g.dialog.length) ? g.dialog : null;
    if (segs) {
      const list = (galSegMode() === 'single') ? segs.slice(-1) : segs;   // 单行推进只回显最后一句
      list.forEach((s) => {
        const isNarr = !s.speech && s.narration;
        const d = document.createElement('div');
        d.className = 'gal-seg' + (isNarr ? ' narr' : '');
        const sp = String(s.speaker || '').trim();
        if (!isNarr && sp && sp !== nm) {
          const who = document.createElement('span');
          who.className = 'gal-seg-who'; who.textContent = sp + '：';
          d.appendChild(who);
        }
        const body = document.createElement('span');
        body.className = 'gal-seg-text';
        body.innerHTML = renderMarkdown(isNarr ? ('（' + s.narration + '）') : (s.speech || ''));
        d.appendChild(body);
        tbox.appendChild(d);
      });
    } else {   // 兼容旧消息（单个 speech 对象）
      let combined = '';
      if (g && g.narration) combined += '（' + g.narration + '）\n';
      combined += (last.content || '');
      tbox.innerHTML = renderMarkdown(combined);
    }
    galScrollDialog();
  }
  // 按最后一段的立绘/情绪回填舞台
  if (g && Array.isArray(g.dialog)) {
    for (let i = g.dialog.length - 1; i >= 0; i--) {
      const si = g.dialog[i].sprite_index;
      if (typeof si === 'number' && sprites[si]) { galShowSprite(sprites[si].path, si); break; }
    }
  }
  if (g && (g.options || []).length) galRenderOptions(g.options);
  // 回放该轮的工具卡片（GAL 模式下 Agent 的工具时间线显示在舞台上）
  const tools = last.tools || [];
  if (tools.length && galCfg('gal_show_exec', true) !== false) {
    const box = $('#gal-tools');
    if (box) {
      tools.forEach((t) => {
        const card = buildToolCard(t.tool, t.args);
        finishToolCard(card, { result: t.result, undo: t.undo, write: t.write });
        box.appendChild(card);
      });
    }
  }
  if (last.memory && last.memory.summary) galExecLine('记住了：' + last.memory.summary, 'ok');
  if (last.usage) galExecUsage(last.usage);
}

/* ---- 状态提示：思考中（用呼吸点 + 轮换短语表现进度，不暴露机器细节） ---- */
const GAL_STATUS_PHRASES = ['思考中', '斟酌着', '斟酌中', '思绪流转'];
function galSetStatus(text) {
  const box = $('#gal-status');
  const t = $('#gal-status-text');
  clearInterval(galState.statusTimer);
  galState.statusTimer = null;
  galState.statusBase = text || '';
  if (t) t.textContent = galState.statusBase;
  if (box) box.classList.toggle('hidden', !text);
  if (!text) return;
  let i = galStatusPhraseIndex(text);
  galState.statusTimer = setInterval(() => {
    const el = $('#gal-status-text');
    if (!el || !galState.statusBase) return;
    i = (i + 1) % GAL_STATUS_PHRASES.length;
    el.textContent = GAL_STATUS_PHRASES[i] + '…';
  }, 2600);
}
// 状态短语起始位置：同类状态从同一句开始，避免每次跳变
function galStatusPhraseIndex(text) {
  const s = String(text || '');
  if (s.indexOf('回想') >= 0) return 1;
  if (s.indexOf('查阅') >= 0) return 2;
  if (s.indexOf('稍候') >= 0) return 3;
  return 0;
}
// 用量：GAL 模式下不展示 token 这类机器指标（只在内部对话记录里保留）
function galExecUsage(usage) {
  if (!usage) return;
  if (galCfg('gal_show_exec', true) === false) return;
  const total = parseInt(usage.total_tokens || usage.total || 0, 10) || 0;
  if (total > 0) galExecLine('本轮共 ' + total + ' 字迹');
}
// 深度思考的实时内容：让 GAL 舞台上也能看到模型正在「想什么」
// 受「设置 → GAL 演出 → 显示 AI 思考过程」控制；关闭时只保留状态栏的「思考中…」
function galThinkAppend(text) {
  if (!text) return;
  if (galCfg('gal_show_think', false) === false) return;
  const box = $('#gal-think');
  if (!box) return;
  box.classList.remove('hidden');
  box.textContent += text;
  box.scrollTop = box.scrollHeight;
}
function galThinkClear() {
  const box = $('#gal-think');
  if (box) { box.textContent = ''; box.classList.add('hidden'); }
}
// GAL「幕间」区：把 AI 的执行过程（搜索 / 工具 / 记忆 / 用量 / 报错）显示在舞台上，
// 让 GAL 模式下也能像 Solo 那样看清它在干什么；受「显示 AI 行为」开关控制。
function galExecLine(text, kind) {
  if (!text) return;
  if (galCfg('gal_show_exec', true) === false) return;
  const box = $('#gal-tools');
  if (!box) return;
  const el = document.createElement('div');
  el.className = 'gal-exec-line' + (kind ? (' ' + kind) : '');
  el.textContent = text;
  box.appendChild(el);
  box.scrollTop = box.scrollHeight;
}
function galSetTyping(on) {
  const el = $('#gal-text');
  if (el) el.classList.toggle('typing', !!on);
}

/* ---- 背景 / BGM ---- */
const galMediaCache = new Map();
function galMediaUrl(kind, path) {
  const key = kind + '/' + path;
  if (galMediaCache.has(key)) return Promise.resolve(galMediaCache.get(key));
  return fetch('/api/gal/media', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Auth-Token': TOKEN },
    body: JSON.stringify({ kind: kind, path: path }),
  }).then((res) => { if (!res.ok) throw new Error('HTTP ' + res.status); return res.blob(); })
    .then((blob) => { const u = URL.createObjectURL(blob); galMediaCache.set(key, u); return u; });
}
function galSetBackground(path) {
  const cur = $('#gal-bg');
  const next = $('#gal-bg-next');
  if (!cur) return;
  if (!path) {
    cur.style.backgroundImage = '';
    if (next) next.style.backgroundImage = '';
    return;
  }
  galMediaUrl('bg', path).then((u) => {
    const img = 'url("' + u + '")';
    if (cur.style.backgroundImage === img) return;         // 同一张不重复淡入
    if (!next) { cur.style.backgroundImage = img; return; }
    next.style.backgroundImage = img;
    next.style.opacity = '1';
    setTimeout(() => {                                     // 交叉淡入：新图淡入后再换到底层
      cur.style.backgroundImage = img;
      next.style.opacity = '0';
    }, 480);
  }).catch(() => { /* 忽略 */ });
}
function galSetBgm(path) {
  if (galState.bgm === (path || '')) return;
  galState.bgm = path || '';
  if (galState.bgmAudio) { try { galState.bgmAudio.pause(); } catch (e) { /* 忽略 */ } galState.bgmAudio = null; }
  if (!galState.bgm) return;
  galMediaUrl('bgm', galState.bgm).then((u) => {
    const a = new Audio(u);
    a.loop = true; a.volume = 0.45;
    a.play().catch(() => { /* 需用户交互后才可自动播放 */ });
    galState.bgmAudio = a;
  }).catch(() => { /* 忽略 */ });
}

/* ---- 台词朗读（TTS / 立绘绑定音频） ---- */
function galCurrentSprite() {
  const sprites = (galState.char && galState.char.sprites) || [];
  const i = galState.spriteIndex;
  return (typeof i === 'number' && sprites[i]) ? sprites[i] : null;
}
function galSyncTtsBtn() {
  const b = $('#gal-tts');
  if (b) b.classList.toggle('active', !!galState.ttsOn);
}
async function galToggleTts() {
  galState.ttsOn = !galState.ttsOn;
  galSyncTtsBtn();
  try {
    const cfg = Object.assign({}, state.config, { tts_enabled: galState.ttsOn });
    await api('/api/config', cfg);
    state.config.tts_enabled = galState.ttsOn;
  } catch (e) { /* 忽略 */ }
  toast(galState.ttsOn ? '已开启台词朗读（需在设置里配置 TTS）' : '已关闭台词朗读');
}
function galStopAudio() {
  galState.skip = true;                                  // 本轮不再朗读（含尚未开始的朗读）
  galState.ttsQueue.length = 0;                          // 清空待播队列
  const finish = galState._audioDone;
  galState._audioDone = null;
  if (galState.audio) { try { galState.audio.pause(); } catch (e) { /* 忽略 */ } galState.audio = null; }
  if (finish) { try { finish(); } catch (e) { /* 忽略 */ } }   // 让 galPlayUrl 的 await 立刻返回
}
function galSkipSpeech() { galStopAudio(); toast('已跳过本轮朗读'); }
function galPlayUrl(url) {
  return new Promise((resolve) => {
    if (!url || galState.skip) return resolve();
    const a = new Audio(url);
    galState.audio = a;
    const done = () => {
      if (galState.audio === a) galState.audio = null;
      if (galState._audioDone === done) galState._audioDone = null;
      resolve();
    };
    galState._audioDone = done;
    a.onended = done;
    a.onerror = done;
    a.play().catch(done);
  });
}
/* 朗读前清洗文本：去掉代码、*动作*、（动作）/（颜文字）、表情与装饰符号
   （参考原模组 Shinsekai 的 TextProcessor.remove_parentheses） */
function galCleanSpeech(text) {
  let s = String(text || '');
  s = s.replace(/```[\s\S]*?```/g, ' ');                 // 围栏代码块
  s = s.replace(/`[^`\n]*`/g, ' ');                       // 行内代码
  s = s.replace(/\$\$[\s\S]*?\$\$/g, ' ');                // 公式（不朗读 LaTeX）
  s = s.replace(/\\\[[\s\S]*?\\\]/g, ' ');
  s = s.replace(/\\\([\s\S]*?\\\)/g, ' ');
  s = s.replace(/\$[^\n$]*\$/g, ' ');
  s = s.replace(/\*[^*\n]{0,40}\*/g, ' ');               // *动作 / 强调*
  s = s.replace(/[（(][^（）()\n]{0,40}[)）]/g, ' ');      // （动作）/（颜文字）
  s = s.replace(/[\u0300-\u036F\u200B-\u200F\u2028-\u202F\u20E3\uFE00-\uFE0F]/g, ''); // 组合/零宽/变体符
  s = s.replace(/[\u{1F000}-\u{1FAFF}\u{2190}-\u{2BFF}\u{2600}-\u{27BF}]/gu, ''); // emoji/装饰符号
  s = s.replace(/[\u0600-\u06FF\u0E00-\u0E7F\u1100-\u11FF\u3130-\u318F\uFFA0-\uFFDC]/g, ''); // 颜文字里常见的阿拉伯/泰/谚文字符
  s = s.replace(/\s{2,}/g, ' ').trim();
  return s;
}

/* 朗读切分：短句整段一次合成；超长才按标点切分（对齐原模组 tts_split_enabled 的默认不切） */
function galSplitSpeech(text, maxLen) {
  const t = String(text || '').trim();
  if (!t) return [];
  if (!maxLen || t.length <= maxLen) return [t];
  const parts = t.split(/(?<=[。！？!?；;…，,、])/).map((s) => s.trim()).filter(Boolean);
  const out = []; let cur = '';
  for (const p of parts) {
    if (!cur) cur = p;
    else if (cur.length + p.length <= maxLen) cur += p;
    else { out.push(cur); cur = p; }
  }
  if (cur) out.push(cur);
  return out.length ? out : [t];
}

async function galSpeak(text) {
  // 注意：不要在这里重置 skip —— 用户可能已在「生成中」按了「跳过朗读」，
  // 重置会让回复到达后照样出声。skip 只在「新的一轮开始时」清空。
  if (!galState.ttsOn || !text || galState.skip) return;
  const spr = galCurrentSprite();
  if (state.config.gal_tts_priority !== 'tts' && spr && spr.voice_path) {
    try { await galPlayUrl(await galAssetUrl(galState.charId, spr.voice_path)); return; } catch (e) { /* 落到 TTS */ }
  }
  const clean = galCleanSpeech(text);
  if (!clean) return;                                   // 整句都是动作/颜文字 → 不朗读
  const maxLen = parseInt(state.config.tts_max_len, 10) || 80;
  const chunks = galSplitSpeech(clean, maxLen);
  const preset = (galState.char && galState.char.tts ? galState.char.tts.active : '') || '';
  let firstErr = '';
  // 合成单段 → 返回可播放的 blob URL（失败返回 null，并记录首个错误）
  const synth = async (piece) => {
    try {
      const res = await fetch('/api/gal/tts', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Auth-Token': TOKEN },
        body: JSON.stringify({ text: piece, character_id: galState.charId, preset: preset }),
      });
      if (!res.ok) {
        if (!firstErr) {
          try { firstErr = (await res.json()).error || ('HTTP ' + res.status); }
          catch (e) { firstErr = 'HTTP ' + res.status; }
        }
        return null;
      }
      return URL.createObjectURL(await res.blob());
    } catch (e) { if (!firstErr) firstErr = String(e); return null; }
  };
  let next = synth(chunks[0]);                          // 立即开始合成第一段
  for (let i = 0; i < chunks.length; i++) {
    if (galState.skip) break;
    const url = await next;
    if (i + 1 < chunks.length) next = synth(chunks[i + 1]);  // 边播边预取下一段，消除句间空档
    if (url) await galPlayUrl(url);
  }
  if (firstErr && galState.ttsWarned !== firstErr) {
    galState.ttsWarned = firstErr;
    toast('朗读失败：' + firstErr + '（可在「设置 → 语音输入」检查 TTS 配置）', 'error');
  }
}

/* 设置里的 TTS 试听与音色列表 */
async function galTestTts() {
  const val = (id) => { const el = $(id); return el ? el.value.trim() : ''; };
  const cfg = Object.assign({}, state.config, {
    tts_provider: val('#speech-tts-provider') || 'edge',
    tts_url: val('#speech-tts-url'),
    tts_api_key: val('#speech-tts-key'),
    tts_model: val('#speech-tts-model'),
    tts_voice: val('#speech-tts-voice'),
  });
  try { await api('/api/config', cfg); state.config = cfg; } catch (e) { /* 忽略 */ }
  galState.skip = false;
  toast('正在合成测试语音…');
  try {
    const res = await fetch('/api/gal/tts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Auth-Token': TOKEN },
      body: JSON.stringify({ text: '你好，这是台词朗读测试。', force: true }),
    });
    if (!res.ok) {
      let msg = 'HTTP ' + res.status;
      try { msg = (await res.json()).error || msg; } catch (e) { /* 忽略 */ }
      toast('合成失败：' + msg, 'error');
      return;
    }
    const url = URL.createObjectURL(await res.blob());
    toast('已开始播放测试语音');
    await galPlayUrl(url);
  } catch (e) { toast('测试失败：' + e, 'error'); }
}

async function galLoadTtsVoices(silent) {
  try {
    const r = await api('/api/gal/tts/voices', {});
    const list = (r && r.voices) || [];
    const dl = $('#speech-tts-voice-list');
    if (dl) {
      dl.innerHTML = '';
      list.forEach((v) => { const o = document.createElement('option'); o.value = v.short_name; dl.appendChild(o); });
    }
    if (silent) return;
    if (!list.length) { toast('未能获取音色（Edge 服务不可达或未安装 edge-tts）', 'error'); return; }
    alert('Edge 中文音色（' + list.length + ' 个）：\n\n' + list.map((v) => v.short_name).join('\n') +
          '\n\n默认：' + ((r && r.default) || 'zh-CN-XiaoxiaoNeural'));
  } catch (e) { if (!silent) toast('获取音色失败：' + e, 'error'); }
}

/* ---- 角色的配音预设（TTS） ---- */
let galEdTts = { active: '', presets: {} };
let galEdColor = '';                 // 编辑器里的主题色（'' 表示跟随默认）

// #rgb → #rrggbb（<input type=color> 只接受 6 位）
function _hex6(c) {
  const s = String(c || '').trim();
  if (/^#[0-9a-fA-F]{6}$/.test(s)) return s;
  if (/^#[0-9a-fA-F]{3}$/.test(s)) return '#' + s[1] + s[1] + s[2] + s[2] + s[3] + s[3];
  return '';
}

function galEdTtsRender() {
  const sel = $('#gal-ed-tts-preset');
  if (!sel) return;
  sel.innerHTML = '';
  const none = document.createElement('option');
  none.value = ''; none.textContent = '（跟随全局设置）';
  sel.appendChild(none);
  Object.keys(galEdTts.presets).forEach((n) => {
    const o = document.createElement('option'); o.value = n; o.textContent = n;
    sel.appendChild(o);
  });
  sel.value = galEdTts.active || '';
  const p = galEdTts.presets[galEdTts.active] || {};
  $('#gal-ed-tts-provider').value = p.provider || '';
  $('#gal-ed-tts-voice').value = p.voice || '';
  $('#gal-ed-tts-model').value = p.model || '';
  $('#gal-ed-tts-speed').value = (p.speed != null ? String(p.speed) : '');
  $('#gal-ed-tts-url').value = p.url || '';
  $('#gal-ed-tts-prompt').value = p.prompt_text || '';
  $('#gal-ed-tts-lang').value = p.prompt_lang || '';
  // 参考音频下拉：角色目录下的音频文件（用于 GPT-SoVITS 音色克隆）
  const refSel = $('#gal-ed-tts-ref');
  if (refSel) {
    const cur = p.ref_audio || '';
    refSel.innerHTML = '';
    const noneO = document.createElement('option');
    noneO.value = ''; noneO.textContent = '（不使用参考音频）';
    refSel.appendChild(noneO);
    galEditorAudioFiles().forEach((f) => {
      const o = document.createElement('option'); o.value = f; o.textContent = f;
      refSel.appendChild(o);
    });
    if (cur && !galEditorAudioFiles().includes(cur)) {
      const o = document.createElement('option'); o.value = cur; o.textContent = cur + '（缺失）';
      refSel.appendChild(o);
    }
    refSel.value = cur;
  }
  // 微调权重下拉：角色目录下的 .ckpt（GPT）/ .pth（SoVITS），可选，需本地 GPT-SoVITS
  [['#gal-ed-tts-gpt', 'gpt_weights', 'gpt', '（不使用 GPT 权重）'],
   ['#gal-ed-tts-sovits', 'sovits_weights', 'sovits', '（不使用 SoVITS 权重）']].forEach((a) => {
    const el = $(a[0]);
    if (!el) return;
    const cur = p[a[1]] || '';
    const files = galEditorWeightFiles(a[2]);
    el.innerHTML = '';
    const o0 = document.createElement('option'); o0.value = ''; o0.textContent = a[3];
    el.appendChild(o0);
    files.forEach((f) => { const o = document.createElement('option'); o.value = f; o.textContent = f; el.appendChild(o); });
    if (cur && !files.includes(cur)) {
      const o = document.createElement('option'); o.value = cur; o.textContent = cur + '（缺失）';
      el.appendChild(o);
    }
    el.value = cur;
  });
  const hasActive = !!galEdTts.active;
  const hasAny = Object.keys(galEdTts.presets).length > 0;
  ['gal-ed-tts-provider', 'gal-ed-tts-voice', 'gal-ed-tts-model', 'gal-ed-tts-speed',
   'gal-ed-tts-url', 'gal-ed-tts-ref', 'gal-ed-tts-lang', 'gal-ed-tts-prompt',
   'gal-ed-tts-gpt', 'gal-ed-tts-sovits'].forEach((id) => {
    const el = $('#' + id); if (el) el.disabled = !hasActive;
  });
  const delBtn = $('#gal-ed-tts-del'); if (delBtn) delBtn.disabled = !hasAny;      // 有预设就能删
  const testBtn = $('#gal-ed-tts-test'); if (testBtn) testBtn.disabled = !hasActive;
}

// 角色目录下的音频文件（参考音频 / 绑定音频）
function galEditorAudioFiles() {
  const audio = /\.(wav|mp3|ogg|m4a|flac)$/i;
  return (galEditorFiles || []).filter((f) => audio.test(f));
}

// 角色目录下的 GPT-SoVITS 微调权重（gpt=.ckpt / sovits=.pth）
function galEditorWeightFiles(kind) {
  const re = kind === 'gpt' ? /\.ckpt$/i : /\.pth$/i;
  return (galEditorFiles || []).filter((f) => re.test(f));
}

// 立绘「绑定音频」下拉：从角色目录的音频里选
function galFillVoiceSelect(sel, cur) {
  if (!sel) return;
  sel.innerHTML = '';
  const none = document.createElement('option');
  none.value = ''; none.textContent = '绑定音频：不绑定';
  sel.appendChild(none);
  const files = galEditorAudioFiles();
  files.forEach((f) => {
    const o = document.createElement('option'); o.value = f; o.textContent = f;
    sel.appendChild(o);
  });
  if (cur && !files.includes(cur)) {
    const o = document.createElement('option'); o.value = cur; o.textContent = cur + '（缺失）';
    sel.appendChild(o);
  }
  sel.value = cur || '';
}
function galRefreshVoiceSelects() {
  $$('#gal-ed-sprites .gal-ed-voice').forEach((sel) => galFillVoiceSelect(sel, sel.value));
}

function galEdTtsWrite() {          // 把表单写回「当前」预设
  if (!galEdTts.active) return;
  const sp = parseFloat($('#gal-ed-tts-speed').value);
  galEdTts.presets[galEdTts.active] = {
    provider: $('#gal-ed-tts-provider').value,
    voice: $('#gal-ed-tts-voice').value.trim(),
    model: $('#gal-ed-tts-model').value.trim(),
    speed: (isFinite(sp) && sp > 0) ? sp : 1.0,
    url: $('#gal-ed-tts-url').value.trim(),
    ref_audio: ($('#gal-ed-tts-ref') || {}).value || '',
    prompt_text: ($('#gal-ed-tts-prompt') || {}).value.trim(),
    prompt_lang: ($('#gal-ed-tts-lang') || {}).value || '',
    gpt_weights: ($('#gal-ed-tts-gpt') || {}).value || '',
    sovits_weights: ($('#gal-ed-tts-sovits') || {}).value || '',
  };
}

// 编辑器里试听「当前」预设（用未保存的表单值，不落盘）
async function galEdTtsTest() {
  if (!galEdTts.active) { toast('请先新建或选择一个预设'); return; }
  galEdTtsWrite();
  const p = galEdTts.presets[galEdTts.active] || {};
  galState.skip = false;
  toast('正在合成测试语音…');
  try {
    const res = await fetch('/api/gal/tts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Auth-Token': TOKEN },
      body: JSON.stringify({
        text: '你好，这是该配音预设的试听。', force: true, override: p,
        character_id: ($('#gal-ed-id') || {}).value || '',   // 便于后端解析参考音频路径
      }),
    });
    if (!res.ok) {
      let msg = 'HTTP ' + res.status;
      try { msg = (await res.json()).error || msg; } catch (e) { /* 忽略 */ }
      toast('合成失败：' + msg, 'error');
      return;
    }
    toast('已开始播放试听');
    await galPlayUrl(URL.createObjectURL(await res.blob()));
  } catch (e) { toast('测试失败：' + e, 'error'); }
}

// GAL 工具条上的配音预设快捷切换
function galRenderTtsPresets() {
  const sel = $('#gal-tts-preset');
  if (!sel) return;
  const t = (galState.char && galState.char.tts) || { active: '', presets: {} };
  const names = Object.keys(t.presets || {});
  sel.innerHTML = '';
  const none = document.createElement('option');
  none.value = ''; none.textContent = '音色：跟随全局';
  sel.appendChild(none);
  names.forEach((n) => {
    const o = document.createElement('option'); o.value = n; o.textContent = '音色：' + n;
    sel.appendChild(o);
  });
  sel.value = t.active || '';
  sel.title = names.length ? '该角色的配音预设（一键切换）'
                           : '该角色暂无配音预设：在「✎ 角色」里添加';
}

async function galSwitchTtsPreset(name) {
  if (!galState.charId) return;
  try {
    const r = await api('/api/gal/tts/active', { id: galState.charId, preset: name || '' });
    galState.char = (r && r.character) || galState.char;
    galRenderTtsPresets();
    toast(name ? ('配音预设已切换为「' + name + '」') : '配音改为跟随全局设置');
  } catch (e) { toast('切换失败：' + e, 'error'); }
}

/* ---- 快速克隆音色：参考音频 → 试听 → 存为预设（需本地 GPT-SoVITS） ---- */
let galCloneFiles = [];

function galToggleClone(force) {
  const p = $('#gal-clone-panel');
  if (!p) return;
  const show = (typeof force === 'boolean') ? force : p.classList.contains('hidden');
  p.classList.toggle('hidden', !show);
  if (show) galCloneRefresh();
}

/* ---- 右上角「⋯」抽屉：收起低频操作 ---- */
function galToggleMenu(force) {
  const m = $('#gal-menu');
  if (!m) return;
  const show = (typeof force === 'boolean') ? force : m.classList.contains('hidden');
  m.classList.toggle('hidden', !show);
}

async function galCloneRefresh() {
  const cid = galState.charId || (curSession() || {}).gal_character_id || '';
  const hint = $('#gal-clone-hint');
  const sel = $('#gal-clone-ref');
  const audio = /\.(wav|mp3|ogg|m4a|flac)$/i;
  galCloneFiles = [];
  if (cid) {
    try { galCloneFiles = (await api('/api/gal/character/files', { id: cid })).files || []; }
    catch (e) { galCloneFiles = []; }
  }
  if (hint) hint.textContent = cid ? ('当前角色：' + ((galState.char && galState.char.name) || cid)) : '请先选择角色';
  if (!sel) return;
  const prev = sel.value;
  const files = galCloneFiles.filter((f) => audio.test(f));
  sel.innerHTML = '';
  const o0 = document.createElement('option');
  o0.value = '';
  o0.textContent = files.length ? '（选择参考音频）' : '（角色目录暂无音频，请先上传）';
  sel.appendChild(o0);
  files.forEach((f) => { const o = document.createElement('option'); o.value = f; o.textContent = f; sel.appendChild(o); });
  if (prev && files.includes(prev)) sel.value = prev;
}

function galCloneOverride() {
  return {
    provider: 'gpt-sovits',
    ref_audio: ($('#gal-clone-ref') || {}).value || '',
    prompt_text: (($('#gal-clone-prompt') || {}).value || '').trim(),
    prompt_lang: ($('#gal-clone-lang') || {}).value || 'zh',
  };
}

async function galCloneTest() {
  const cid = galState.charId || (curSession() || {}).gal_character_id || '';
  if (!cid) { toast('请先选择角色', 'error'); return; }
  const override = galCloneOverride();
  if (!override.ref_audio) { toast('请先选择参考音频', 'error'); return; }
  const text = (($('#gal-clone-text') || {}).value || '').trim();
  if (!text) { toast('请填写试听文本', 'error'); return; }
  const btn = $('#gal-clone-test');
  const old = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = '合成中…'; }
  toast('正在克隆并合成…（首次需加载模型，可能较慢）');
  try {
    const res = await fetch('/api/gal/tts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Auth-Token': TOKEN },
      body: JSON.stringify({ text: text, force: true, override: override, character_id: cid }),
    });
    if (!res.ok) {
      let msg = 'HTTP ' + res.status;
      try { msg = (await res.json()).error || msg; } catch (e) { /* 忽略 */ }
      toast('合成失败：' + msg, 'error');
      return;
    }
    const au = $('#gal-clone-audio');
    if (au) {
      au.src = URL.createObjectURL(await res.blob());
      au.classList.remove('hidden');
      try { await au.play(); } catch (e) { /* 自动播放被拦截，用户手动点 */ }
    }
    toast('试听就绪');
  } catch (e) { toast('合成失败：' + e, 'error'); }
  finally { if (btn) { btn.disabled = false; btn.textContent = old; } }
}

async function galCloneSave() {
  const cid = galState.charId || (curSession() || {}).gal_character_id || '';
  if (!cid) { toast('请先选择角色', 'error'); return; }
  const override = galCloneOverride();
  if (!override.ref_audio) { toast('请先选择参考音频', 'error'); return; }
  const name = (($('#gal-clone-name') || {}).value || '').trim() || '克隆音色';
  let char = null;
  try { char = (await api('/api/gal/character/get', { id: cid })).character; }
  catch (e) { toast('读取角色失败：' + e, 'error'); return; }
  if (!char) { toast('角色不存在', 'error'); return; }
  const tts = char.tts || { active: '', presets: {} };
  const presets = tts.presets || {};
  presets[name] = Object.assign({}, presets[name], override, { speed: 1.0, voice: '', model: '', url: '' });
  tts.presets = presets;
  if (!tts.active) tts.active = name;
  char.tts = tts;
  try { await api('/api/gal/character/save', { id: cid, character: char }); }
  catch (e) { toast('保存失败：' + e, 'error'); return; }
  try { galState.char = (await api('/api/gal/character/get', { id: cid })).character || galState.char; }
  catch (e) { /* 忽略 */ }
  galRenderTtsPresets();
  toast('已存为配音预设「' + name + '」');
}

async function galCloneUpload(file) {
  const cid = galState.charId || (curSession() || {}).gal_character_id || '';
  if (!cid) { toast('请先选择角色', 'error'); return; }
  if (!file) return;
  try {
    const b64 = await fileToB64(file);
    const r = await api('/api/gal/character/upload', { id: cid, filename: file.name, data: b64 });
    galCloneFiles = r.files || galCloneFiles;
    await galCloneRefresh();
    if (r.path && $('#gal-clone-ref')) $('#gal-clone-ref').value = r.path;
    toast('已上传：' + r.path);
  } catch (e) { toast('上传失败：' + e, 'error'); }
}

/* ---- 剧情节点显示 ---- */
function galSetNode(obj) {
  galState.node = obj || null;
  const el = $('#gal-role-name');
  if (!el) return;
  if (obj && obj.node_id) {
    // AI 自动剧本的节点 id 形如 a1 / a2 → 用「第 N 幕」表述，更不破坏沉浸感
    const m = /^a(\d+)$/.exec(String(obj.node_id));
    const head = m ? ('第 ' + m[1] + ' 幕') : ('节点 ' + obj.node_id);
    el.textContent = head +
      (obj.max_rounds ? ('（' + (obj.round || 0) + '/' + obj.max_rounds + '）') : '');
  } else {
    el.textContent = '';
  }
}

// 分幕提示：直接追加一段暗淡旁白分隔（不走打字机 / 朗读 / 队列）
function galAppendDivider(text) {
  const t = $('#gal-text');
  if (!t || !text) return;
  const d = document.createElement('div');
  d.className = 'gal-seg narr gal-divider';
  d.textContent = text;
  t.appendChild(d);
  t.scrollTop = t.scrollHeight;
}

/* ---- 角色 / 立绘 / 剧本编辑器 ---- */
let galEditorFiles = [];

async function galOpenEditor(cid, isNew) {
  const modal = $('#gal-modal');
  if (!modal) return;
  galEditorFiles = [];
  const useId = isNew ? '' : (cid || '');
  $('#gal-ed-id').value = useId;
  $('#gal-ed-id').disabled = !isNew;                 // 已有角色不允许改 id（目录名）
  $('#gal-ed-name').value = '';
  $('#gal-ed-scale').value = '1';
  $('#gal-ed-profile').value = '';
  galEdColor = '';
  if ($('#gal-ed-color')) $('#gal-ed-color').value = '#7c9cff';
  $('#gal-ed-story').value = '';
  $('#gal-ed-story-on').checked = false;
  $('#gal-ed-sprites').innerHTML = '';
  let char = null, story = null;
  if (useId) {
    try { char = (await api('/api/gal/character/get', { id: useId })).character; } catch (e) { /* 忽略 */ }
    try { story = (await api('/api/gal/story/get', { id: useId })).story; } catch (e) { /* 忽略 */ }
    try { galEditorFiles = (await api('/api/gal/character/files', { id: useId })).files || []; } catch (e) { /* 忽略 */ }
  }
  const dl = $('#gal-ed-filelist');
  if (dl) {
    dl.innerHTML = '';
    galEditorFiles.forEach((f) => { const o = document.createElement('option'); o.value = f; dl.appendChild(o); });
  }
  if (char) {
    $('#gal-ed-name').value = char.name || '';
    $('#gal-ed-scale').value = String(char.sprite_scale || 1);
    $('#gal-ed-profile').value = char.profile || '';
    galEdColor = char.color || '';
    if ($('#gal-ed-color')) $('#gal-ed-color').value = _hex6(galEdColor) || '#7c9cff';
    (char.sprites || []).forEach((s) => galEditorAddRow(s));
  } else {
    galEditorAddRow(null);
  }
  // 配音预设
  galEdTts = { active: '', presets: {} };
  if (char && char.tts) {
    galEdTts.active = char.tts.active || '';
    const src = char.tts.presets || {};
    Object.keys(src).forEach((n) => { galEdTts.presets[n] = Object.assign({}, src[n]); });
  }
  galEdTtsRender();
  galLoadTtsVoices(true);          // 静默填充音色联想（失败也不提示）
  const nm2 = $('#gal-name');
  if (nm2) nm2.textContent = char ? char.name : '';
  if (story) {
    $('#gal-ed-story').value = JSON.stringify(story, null, 2);
    const s = curSession();
    $('#gal-ed-story-on').checked = !!(s && s.gal_node_id);
  }
  showWithAnim(modal);
}

function galEditorAddRow(sprite) {
  const box = $('#gal-ed-sprites');
  if (!box) return;
  const row = document.createElement('div');
  row.className = 'gal-ed-row';
  const idx = document.createElement('span'); idx.className = 'gal-ed-idx';
  const thumb = document.createElement('img');
  thumb.className = 'gal-ed-thumb'; thumb.alt = ''; thumb.title = '点击预览';
  const path = document.createElement('input');
  path.className = 'field-input gal-ed-path'; path.placeholder = 'sprites/xx.png';
  path.value = (sprite && sprite.path) || '';
  path.setAttribute('list', 'gal-ed-filelist');
  const tags = document.createElement('input');
  tags.className = 'field-input gal-ed-tags'; tags.placeholder = '情绪标签：微笑,开心';
  tags.value = ((sprite && sprite.emotion_tags) || []).join(',');
  const voice = document.createElement('select');
  voice.className = 'field-input gal-ed-voice';
  galFillVoiceSelect(voice, (sprite && sprite.voice_path) || '');
  const up = document.createElement('button');
  up.className = 'ghost gal-ed-move'; up.textContent = '↑'; up.title = '上移';
  up.onclick = () => { const p = row.previousElementSibling; if (p) { box.insertBefore(row, p); galEditorRenumber(); } };
  const down = document.createElement('button');
  down.className = 'ghost gal-ed-move'; down.textContent = '↓'; down.title = '下移';
  down.onclick = () => { const nx = row.nextElementSibling; if (nx) { box.insertBefore(nx, row); galEditorRenumber(); } };
  const del = document.createElement('button');
  del.className = 'ghost gal-ed-del'; del.textContent = '✕'; del.title = '移除该立绘';
  del.onclick = () => { row.remove(); galEditorRenumber(); };
  // 缩略图：路径填写后按需取回预览
  const refreshThumb = () => {
    const p = path.value.trim();
    const cid = ($('#gal-ed-id').value || '').trim();
    if (!p || !cid) { thumb.removeAttribute('src'); thumb.style.visibility = 'hidden'; return; }
    galAssetUrl(cid, p).then((u) => { thumb.src = u; thumb.style.visibility = 'visible'; })
      .catch(() => { thumb.removeAttribute('src'); thumb.style.visibility = 'hidden'; });
  };
  path.addEventListener('change', refreshThumb);
  path.addEventListener('blur', refreshThumb);
  thumb.onclick = () => { if (thumb.src) window.open(thumb.src, '_blank'); };
  row.appendChild(idx); row.appendChild(thumb); row.appendChild(path);
  row.appendChild(tags); row.appendChild(voice);
  row.appendChild(up); row.appendChild(down); row.appendChild(del);
  box.appendChild(row);
  galEditorRenumber();
  refreshThumb();
}

function galEditorRenumber() {
  $$('#gal-ed-sprites .gal-ed-row').forEach((r, i) => {
    const idx = r.querySelector('.gal-ed-idx');
    if (idx) idx.textContent = String(i + 1);
  });
}

function galEditorCollect() {
  galEdTtsWrite();
  const sprites = [];
  $$('#gal-ed-sprites .gal-ed-row').forEach((r) => {
    const p = r.querySelector('.gal-ed-path').value.trim();
    if (!p) return;
    sprites.push({
      path: p,
      emotion_tags: r.querySelector('.gal-ed-tags').value.split(/[,，]/).map((s) => s.trim()).filter(Boolean),
      voice_path: r.querySelector('.gal-ed-voice').value.trim(),
    });
  });
  let scale = parseFloat($('#gal-ed-scale').value);
  if (!isFinite(scale)) scale = 1;
  return {
    name: $('#gal-ed-name').value.trim(),
    color: galEdColor || '',
    sprite_scale: scale,
    profile: $('#gal-ed-profile').value,
    sprites: sprites,
    tts: { active: galEdTts.active || '', presets: galEdTts.presets },
  };
}

async function galEditorUpload(files) {
  const cid = $('#gal-ed-id').value.trim();
  if (!cid) { toast('请先填写角色 id', 'error'); return; }
  const list = Array.from(files || []);
  if (!list.length) return;
  let ok = 0;
  for (const file of list) {
    try {
      const b64 = await fileToB64(file);
      const r = await api('/api/gal/character/upload', { id: cid, filename: file.name, data: b64 });
      galEditorFiles = r.files || galEditorFiles;
      if (r.path) { galEditorAddRow({ path: r.path, emotion_tags: [], voice_path: '' }); ok++; }
    } catch (e) { toast('上传失败：' + file.name + '：' + e, 'error'); }
  }
  const dl = $('#gal-ed-filelist');
  if (dl) {
    dl.innerHTML = '';
    galEditorFiles.forEach((f) => { const o = document.createElement('option'); o.value = f; dl.appendChild(o); });
  }
  galRefreshVoiceSelects();
  if (ok) toast('已上传 ' + ok + ' 张图片');
}

// 上传 GPT-SoVITS 参考音频：写入角色目录并绑到当前配音预设
async function galUploadRefAudio(file) {
  const cid = $('#gal-ed-id').value.trim();
  if (!cid) { toast('请先填写角色 id', 'error'); return; }
  if (!galEdTts.active) { toast('请先在「配音」里新建或选择一个预设'); return; }
  if (!file) return;
  try {
    const b64 = await fileToB64(file);
    const r = await api('/api/gal/character/upload', { id: cid, filename: file.name, data: b64 });
    galEditorFiles = r.files || galEditorFiles;
    const dl = $('#gal-ed-filelist');
    if (dl) {
      dl.innerHTML = '';
      galEditorFiles.forEach((f) => { const o = document.createElement('option'); o.value = f; dl.appendChild(o); });
    }
    galEdTtsWrite();                               // 先保留其它字段
    if (r.path) {
      galEdTts.presets[galEdTts.active] = Object.assign({}, galEdTts.presets[galEdTts.active], { ref_audio: r.path });
      galEdTtsRender();
      galRefreshVoiceSelects();
      toast('已上传参考音频：' + r.path);
    }
  } catch (e) { toast('上传失败：' + e, 'error'); }
}

// AI 帮写角色设定（复用「设置 → 聊天模型」）
async function galAiDraft() {
  const name = $('#gal-ed-name').value.trim();
  if (!name) { toast('请先填写角色名', 'error'); return; }
  const btn = $('#gal-ed-draft');
  const old = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = '生成中…'; }
  try {
    const r = await api('/api/gal/character/draft', {
      name: name, profile: $('#gal-ed-profile').value.trim(),
    });
    if (r && r.profile) { $('#gal-ed-profile').value = r.profile; toast('已生成角色设定'); }
    else toast('生成结果为空', 'error');
  } catch (e) { toast('生成失败：' + e, 'error'); }
  finally { if (btn) { btn.disabled = false; btn.textContent = old; } }
}

async function galSaveEditor() {
  const cid = $('#gal-ed-id').value.trim();
  if (!cid) { toast('请填写角色 id', 'error'); return; }
  try {
    await api('/api/gal/character/save', { id: cid, character: galEditorCollect() });
    // 剧本（可为空）
    const raw = $('#gal-ed-story').value.trim();
    if (raw) {
      let story = null;
      try { story = JSON.parse(raw); } catch (e) { toast('剧本 JSON 解析失败，已跳过剧本', 'error'); }
      if (story) await api('/api/gal/story/save', { id: cid, story: story });
    }
    // 剧本启用状态
    await api('/api/gal/session', {
      id: state.sessions.current,
      gal_character_id: cid,
      gal_story_on: !!$('#gal-ed-story-on').checked,
    });
  } catch (e) { toast('保存失败：' + e, 'error'); return; }
  hideWithAnim($('#gal-modal'));
  await galLoadCharacters();
  await galSetCharacter(cid);
  toast('已保存角色：' + cid);
}

async function galEnterMode() {
  await galLoadCharacters();
  const s = curSession();
  await galSetCharacter((s && s.gal_character_id) || galState.charId || '');
  galApplyTheme();
  galApplyViewPrefs();
  requestAnimationFrame(() => galSyncSpriteLift());
  galState.ttsOn = !!(state.config && state.config.tts_enabled);
  galState.skip = false;
  galSyncTtsBtn();
  galSyncAutoBtn();
  galSetNode(s && s.gal_node_id ? { node_id: s.gal_node_id, round: s.gal_round || 0, max_rounds: 0 } : null);
  galRenderFromSession();
}

function applyUiMode(mode) {
  const m = (mode === 'ide' || mode === 'gal') ? mode : 'solo';
  state.uiMode = m;
  const main = $('#main');
  if (main) {
    main.classList.toggle('ide-mode', m === 'ide');
    main.classList.toggle('gal-mode', m === 'gal');
  }
  const panel = $('#ide-panel');
  if (panel) panel.classList.toggle('hidden', m !== 'ide');
  const gp = $('#gal-panel');
  if (gp) gp.classList.toggle('hidden', m !== 'gal');
  $$('#mode-switch .ms-btn').forEach((b) => b.classList.toggle('active', b.dataset.mode === m));
  galApplyTheme();                     // GAL 主题随模式进入/退出切换
  if (m !== 'gal') { galToggleReview(false); galToggleScript(false); }   // 离开 GAL 模式时收起浮层
  if (m === 'ide') { ideInitOnce(); loadIdeTree(); }
  if (m === 'gal') { galEnterMode(); }
}

// IDE 首次进入时初始化：装载本地 Monaco，并做一次性绑定
let ideInited = false;
function ideInitOnce() {
  if (ideInited) return;
  ideInited = true;
  ideSwitchSide('explorer');
  loadMonaco().then((m) => {
    if (!m) return;                 // Monaco 加载失败则继续用内置 textarea，功能不受影响
    state.ideMonaco = m;
    ideEnsureEditor(m);
    if (state.ideOpen) ideActivate(state.ideOpen);
    else ideShowView('editor');
    ideUpdateStatus();
  });
}

let monacoLoading = null;
// 本地内置的 Monaco（离线可用）。注入时机放在 window.onload 之后，
// 避免它的 AMD loader 抢先定义 define，导致 echarts / vis-network 等 UMD 库注册成 AMD 模块而不挂全局变量。
function loadMonaco() {
  if (monacoLoading) return monacoLoading;
  monacoLoading = new Promise((resolve) => {
    if (window.monaco) return resolve(window.monaco);
    const boot = () => {
      const s = document.createElement('script');
      s.src = '/static/vendor/monaco/vs/loader.js';
      s.onload = () => {
        if (!window.require) return resolve(null);
        const base = location.origin + '/static/vendor/monaco/';
        window.MonacoEnvironment = {
          getWorker() {
            const body = "self.MonacoEnvironment={baseUrl:'" + base + "'};" +
              "importScripts('" + base + "vs/base/worker/workerMain.js');";
            return new Worker(URL.createObjectURL(new Blob([body], { type: 'text/javascript' })));
          },
        };
        window.require.config({ paths: { vs: '/static/vendor/monaco/vs' } });
        window.require(['vs/editor/editor.main'], () => resolve(window.monaco), () => resolve(null));
      };
      s.onerror = () => resolve(null);
      document.head.appendChild(s);
    };
    if (document.readyState === 'complete') boot();
    else window.addEventListener('load', boot, { once: true });
  });
  return monacoLoading;
}

// 按扩展名推断 Monaco 语言
const IDE_LANGS = {
  py: 'python', js: 'javascript', mjs: 'javascript', cjs: 'javascript', jsx: 'javascript',
  ts: 'typescript', tsx: 'typescript', json: 'json', html: 'html', htm: 'html', vue: 'html',
  css: 'css', scss: 'scss', less: 'less', md: 'markdown', markdown: 'markdown',
  sh: 'shell', bash: 'shell', zsh: 'shell', ps1: 'powershell', bat: 'bat', cmd: 'bat',
  yml: 'yaml', yaml: 'yaml', toml: 'ini', ini: 'ini', cfg: 'ini', conf: 'ini',
  xml: 'xml', java: 'java', c: 'c', h: 'c', cpp: 'cpp', cc: 'cpp', hpp: 'cpp',
  cs: 'csharp', go: 'go', rs: 'rust', php: 'php', rb: 'ruby', sql: 'sql', lua: 'lua', txt: 'plaintext',
};
function ideLangOf(rel) {
  const name = (rel || '').split('/').pop() || '';
  const ext = name.indexOf('.') >= 0 ? name.split('.').pop().toLowerCase() : '';
  return IDE_LANGS[ext] || 'plaintext';
}

// 可在 IDE 里预览的图片类型（SVG 仍按文本编辑）
const IDE_IMAGE_EXTS = ['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'ico', 'avif'];
function ideIsImage(rel) {
  const name = (rel || '').split('/').pop() || '';
  const ext = name.indexOf('.') >= 0 ? name.split('.').pop().toLowerCase() : '';
  return IDE_IMAGE_EXTS.indexOf(ext) >= 0;
}

// 编辑区三态视图切换：Monaco / 纯文本兜底 / 图片预览
function ideShowView(kind) {
  const ed = $('#ide-editor'); if (ed) ed.classList.toggle('hidden', kind !== 'editor');
  const fb = $('#ide-fallback'); if (fb) fb.classList.toggle('hidden', kind !== 'fallback');
  const im = $('#ide-image'); if (im) im.classList.toggle('hidden', kind !== 'image');
}

function ideEnsureEditor(monaco) {
  if (state.ideEditor) return state.ideEditor;
  const host = $('#ide-editor');
  if (!host) return null;
  const d = state.ideMonacoDefaults || {};
  state.ideEditor = monaco.editor.create(host, {
    value: '', language: 'plaintext',
    theme: (state.config && state.config.theme === 'light') ? 'vs' : 'vs-dark',
    automaticLayout: true,
    fontSize: d.fontSize || 13,
    tabSize: d.tabSize || 2,
    minimap: { enabled: true },
    scrollBeyondLastLine: false,
    renderWhitespace: 'selection',
    smoothScrolling: true,
    padding: { top: 8 },
  });
  state.ideEditor.onDidChangeModelContent(() => {
    if (state.ideOpen) ideSetDirty(state.ideOpen, true);
  });
  state.ideEditor.onDidChangeCursorPosition((e) => {
    const el = $('#ide-status-pos');
    if (el) el.textContent = '行 ' + e.position.lineNumber + '，列 ' + e.position.column;
  });
  return state.ideEditor;
}

// 编辑器主题跟随全局深浅色
function ideSyncMonacoTheme() {
  const m = state.ideMonaco;
  if (m) m.editor.setTheme((state.config && state.config.theme === 'light') ? 'vs' : 'vs-dark');
}

function syncIdeRoot() {
  const s = curSession();
  const el = $('#ide-root');
  if (el) el.textContent = (s && s.sandbox) ? s.sandbox : '（未设置沙箱）';
}

// 切会话时重置工作区（沙箱可能不同），并在 IDE 模式下重新加载目录树
function resetIdeForSession() {
  Object.keys(state.ideModels || {}).forEach((k) => {
    try { state.ideModels[k].dispose(); } catch (e) { /* 忽略 */ }
  });
  state.ideModels = {};
  state.ideCache = {};
  state.ideDirtyMap = {};
  state.ideTabList = [];
  state.ideOpen = '';
  state.ideDir = '';
  state.ideChanged.clear();
  if (state.ideEditor) state.ideEditor.setModel(null);
  const t = $('#ide-text'); if (t) t.value = '';
  renderIdeTabs();
  ideUpdateStatus();
  if (state.uiMode === 'ide') loadIdeTree();
  if (state.uiMode === 'gal') {
    galState.charId = ''; galState.char = null; galState.assetCache = new Map();
    galLoadCharacters().then(() => galRenderFromSession());
  }
}

async function loadIdeTree() {
  const box = $('#ide-tree');
  if (!box) return;
  syncIdeRoot();
  const s = curSession();
  if (!s || !s.sandbox) {
    box.innerHTML = '<div class="ide-empty">尚未设置沙箱目录。<br>请先点输入框上方的「🛠 Agent」并选择目录。</div>';
    return;
  }
  let r;
  try { r = await api('/api/sandbox/tree', { session_id: state.sessions.current }); }
  catch (e) { box.innerHTML = '<div class="ide-empty">读取目录失败：' + escapeHtml(String(e)) + '</div>'; return; }
  if (!r || !r.ok) {
    box.innerHTML = '<div class="ide-empty">' + escapeHtml((r && r.error) || '读取失败') + '</div>';
    return;
  }
  state.ideTree = r.tree;
  renderIdeTree(r.tree);
}

// 文件树图标：内联 SVG + 按扩展名着色（VS Code / Seti 风格近似，离线可用）
const IDE_SVG_FILE = '<svg viewBox="0 0 16 16" aria-hidden="true" focusable="false"><path fill="currentColor" d="M9.6 1.5H4.2A1.2 1.2 0 0 0 3 2.7v10.6a1.2 1.2 0 0 0 1.2 1.2h7.6a1.2 1.2 0 0 0 1.2-1.2V5.1L9.6 1.5Zm-.1 1.9 2.3 2.3H9.5V3.4Z"/></svg>';
const IDE_SVG_FOLDER = '<svg viewBox="0 0 16 16" aria-hidden="true" focusable="false"><path fill="currentColor" d="M1.6 3.2h4l1.3 1.5H14a1 1 0 0 1 1 1V12a1 1 0 0 1-1 1H2a1 1 0 0 1-1-1V4.2a1 1 0 0 1 1-1Z"/></svg>';

const IDE_ICON_TYPES = {
  py: 'py', pyw: 'py', js: 'js', mjs: 'js', cjs: 'js', jsx: 'jsx',
  ts: 'ts', tsx: 'ts', json: 'json', html: 'html', htm: 'html', vue: 'vue',
  css: 'css', scss: 'scss', sass: 'scss', less: 'scss',
  md: 'md', markdown: 'md', xml: 'xml', yml: 'yml', yaml: 'yml',
  toml: 'toml', ini: 'toml', cfg: 'toml', conf: 'toml',
  sh: 'sh', bash: 'sh', zsh: 'sh', ps1: 'sh', bat: 'sh', cmd: 'sh',
  c: 'c', h: 'c', cpp: 'cpp', cc: 'cpp', hpp: 'cpp', cs: 'cs',
  java: 'java', go: 'go', rs: 'rs', php: 'php', rb: 'rb', sql: 'sql', lua: 'lua',
  txt: 'txt', log: 'txt', lock: 'lock',
  png: 'img', jpg: 'img', jpeg: 'img', gif: 'img', webp: 'img', bmp: 'img', svg: 'img', ico: 'img',
};
function ideIconClass(name) {
  const n = String(name || '').toLowerCase();
  if (n === '.gitignore' || n === '.gitattributes' || n.endsWith('.lock') || n === 'package-lock.json') {
    return n.endsWith('.lock') ? 'ico-lock' : 'ico-git';
  }
  const ext = n.indexOf('.') >= 0 ? n.split('.').pop() : '';
  const t = IDE_ICON_TYPES[ext];
  return t ? ('ico-' + t) : 'ico-file';
}

// 构建一个可复用的图标元素
function ideIcoEl(name, isDir) {
  const ico = document.createElement('span');
  ico.className = 'ide-ico ' + (isDir ? 'ico-folder' : ideIconClass(name));
  ico.innerHTML = isDir ? IDE_SVG_FOLDER : IDE_SVG_FILE;
  return ico;
}

function buildIdeNodes(items, prefix) {
  const wrap = document.createElement('div');
  (items || []).forEach((it) => {
    const rel = prefix ? (prefix + '/' + it.name) : it.name;
    const node = document.createElement('div');
    node.className = 'ide-node';
    const row = document.createElement('div');
    row.className = 'ide-row';
    row.dataset.path = rel;
    if (state.ideOpen === rel) row.classList.add('active');
    if (state.ideChanged.has(rel)) row.classList.add('changed');
    const care = document.createElement('span');
    care.className = 'ide-caret';
    care.textContent = it.dir ? '▸' : '';
    const nm = document.createElement('span');
    nm.className = 'ide-name';
    nm.textContent = it.name;
    row.appendChild(care); row.appendChild(ideIcoEl(it.name, it.dir)); row.appendChild(nm);
    node.appendChild(row);
    if (it.dir) {
      const kids = document.createElement('div');
      kids.className = 'ide-children hidden';
      kids.appendChild(buildIdeNodes(it.children || [], rel));
      node.appendChild(kids);
      row.onclick = () => {
        state.ideDir = rel;
        const nowHidden = kids.classList.toggle('hidden');
        care.textContent = nowHidden ? '▸' : '▾';
      };
    } else {
      row.onclick = () => openIdeFile(rel);
    }
    wrap.appendChild(node);
  });
  return wrap;
}

function renderIdeTree(tree) {
  const box = $('#ide-tree');
  if (!box) return;
  box.innerHTML = '';
  const kids = (tree && tree.children) || [];
  if (!kids.length) { box.innerHTML = '<div class="ide-empty">（沙箱为空）</div>'; return; }
  box.appendChild(buildIdeNodes(kids, ''));
}

function setUiMode(mode) {
  applyUiMode(mode);
  const cfg = Object.assign({}, state.config || {}, { ui_mode: state.uiMode });
  api('/api/config', cfg).then(() => { state.config = cfg; }).catch(() => {});
}

/* ---------- 标签页 / 状态栏 / 侧边栏切换 ---------- */
function renderIdeTabs() {
  const box = $('#ide-tabs');
  if (!box) return;
  box.innerHTML = '';
  (state.ideTabList || []).forEach((rel) => {
    const t = document.createElement('div');
    t.className = 'ide-tab' + (rel === state.ideOpen ? ' active' : '');
    t.title = rel;
    const nm = document.createElement('span');
    nm.className = 'ide-tab-name';
    nm.textContent = rel.split('/').pop();
    t.appendChild(ideIcoEl(rel.split('/').pop(), false));
    t.appendChild(nm);
    if (state.ideDirtyMap[rel]) {
      const dot = document.createElement('span');
      dot.className = 'ide-tab-dot';
      dot.textContent = '●';
      t.appendChild(dot);
    }
    const x = document.createElement('span');
    x.className = 'ide-tab-x';
    x.textContent = '✕';
    x.title = '关闭';
    x.onclick = (e) => { e.stopPropagation(); ideCloseTab(rel); };
    t.appendChild(x);
    t.onclick = () => ideActivate(rel);
    box.appendChild(t);
  });
}

function ideSetDirty(rel, on) {
  if (!rel) return;
  state.ideDirtyMap[rel] = !!on;
  renderIdeTabs();
  ideUpdateStatus();
}

function ideUpdateStatus() {
  const rel = state.ideOpen;
  const c = (rel && state.ideCache[rel]) || {};
  const p = $('#ide-status-path'); if (p) p.textContent = rel || '—';
  const enc = $('#ide-status-enc');
  if (enc) {
    enc.textContent = rel
      ? (c.image ? (c.size != null ? (Math.max(1, Math.round(c.size / 1024)) + ' KB') : '') : (c.enc || 'UTF-8'))
      : '';
  }
  const lang = $('#ide-status-lang'); if (lang) lang.textContent = rel ? (c.image ? '图片' : ideLangOf(rel)) : '';
  const pos = $('#ide-status-pos'); if (pos && c.image) pos.textContent = '';
  const d = $('#ide-status-dirty'); if (d) d.classList.toggle('hidden', !(rel && state.ideDirtyMap[rel]));
}

function ideSwitchSide(which) {
  state.ideSide = which;
  $$('#ide-panel .act-btn[data-act]').forEach((b) => b.classList.toggle('active', b.dataset.act === which));
  const ex = $('#ide-side-explorer'); if (ex) ex.classList.toggle('hidden', which !== 'explorer');
  const se = $('#ide-side-search'); if (se) se.classList.toggle('hidden', which !== 'search');
  if (which === 'search') { const i = $('#ide-search-input'); if (i) i.focus(); }
}

// 高亮目录树中当前文件
function ideSyncTreeActive() {
  $$('#ide-tree .ide-row').forEach((el) => {
    el.classList.toggle('active', el.dataset.path === state.ideOpen);
    el.classList.toggle('changed', state.ideChanged.has(el.dataset.path));
  });
}

/* ---------- 打开 / 激活 / 关闭标签 ---------- */
async function openIdeFile(rel) {
  if (!state.ideTabList.includes(rel)) {
    if (ideIsImage(rel)) {
      // 图片：走 raw 接口取二进制 → Blob URL 预览
      let blob;
      try {
        const res = await fetch('/api/sandbox/raw', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-Auth-Token': TOKEN },
          body: JSON.stringify({ session_id: state.sessions.current, path: rel }),
        });
        if (!res.ok) {
          let msg = '打开失败';
          try { const j = await res.json(); msg = j.error || msg; } catch (e) { /* 忽略 */ }
          toast(msg, 'error'); return;
        }
        blob = await res.blob();
      } catch (e) { return toast('打开失败：' + e, 'error'); }
      state.ideCache[rel] = { image: true, url: URL.createObjectURL(blob), size: blob.size };
      state.ideDirtyMap[rel] = false;
      state.ideTabList.push(rel);
      state.ideChanged.delete(rel);
    } else {
      let r;
      try { r = await api('/api/sandbox/file', { session_id: state.sessions.current, path: rel }); }
      catch (e) { return toast('打开失败：' + e, 'error'); }
      if (!r || !r.ok) { toast((r && r.error) || '打开失败', 'error'); return; }
      state.ideCache[rel] = { text: r.text || '', enc: r.encoding || '' };
      state.ideDirtyMap[rel] = false;
      state.ideTabList.push(rel);
      state.ideChanged.delete(rel);
      if (r.truncated) toast('文件较大，仅载入前 2MB');
    }
  }
  ideActivate(rel);
}

function ideActivate(rel) {
  if (!rel) return;
  state.ideOpen = rel;
  state.ideDir = rel.indexOf('/') >= 0 ? rel.slice(0, rel.lastIndexOf('/')) : '';
  const c = state.ideCache[rel] || { text: '', enc: '' };
  if (c.image) {
    if (state.ideEditor) state.ideEditor.setModel(null);
    const img = $('#ide-image-img');
    if (img) img.src = c.url || '';
    ideShowView('image');
  } else if (state.ideMonaco && state.ideEditor) {
    let model = state.ideModels[rel];
    if (!model) {
      model = state.ideMonaco.editor.createModel(c.text, ideLangOf(rel));
      const ts = (state.ideMonacoDefaults || {}).tabSize;
      if (ts) model.updateOptions({ tabSize: ts, insertSpaces: true });
      state.ideModels[rel] = model;
    }
    state.ideEditor.setModel(model);
    state.ideEditor.focus();
    ideShowView('editor');
  } else {
    const t = $('#ide-text');
    if (t) { t.value = c.text; t.disabled = false; }
    ideShowView('fallback');
  }
  renderIdeTabs();
  ideSyncTreeActive();
  ideUpdateStatus();
}

function ideCloseTab(rel) {
  if (state.ideDirtyMap[rel] && !confirm('「' + rel + '」有未保存的修改，仍要关闭吗？')) return;
  const i = (state.ideTabList || []).indexOf(rel);
  if (i < 0) return;
  state.ideTabList.splice(i, 1);
  if (state.ideModels[rel]) { try { state.ideModels[rel].dispose(); } catch (e) { /* 忽略 */ } delete state.ideModels[rel]; }
  const cached = state.ideCache[rel];
  if (cached && cached.image && cached.url) { try { URL.revokeObjectURL(cached.url); } catch (e) { /* 忽略 */ } }
  delete state.ideCache[rel];
  delete state.ideDirtyMap[rel];
  if (state.ideOpen === rel) {
    const next = state.ideTabList[Math.max(0, i - 1)] || '';
    if (next) ideActivate(next);
    else {
      state.ideOpen = '';
      if (state.ideEditor) state.ideEditor.setModel(null);
      const t = $('#ide-text'); if (t) t.value = '';
      const img = $('#ide-image-img'); if (img) img.removeAttribute('src');
      ideShowView('editor');
      renderIdeTabs(); ideSyncTreeActive(); ideUpdateStatus();
    }
  } else {
    renderIdeTabs();
  }
}

async function saveIdeFile() {
  const rel = state.ideOpen;
  if (!rel) return;
  if (state.ideCache[rel] && state.ideCache[rel].image) { toast('图片不支持编辑保存'); return; }
  const btn = $('#ide-save');
  if (btn) btn.classList.add('busy');
  const content = (state.ideMonaco && state.ideModels[rel])
    ? state.ideModels[rel].getValue()
    : ($('#ide-text') ? $('#ide-text').value : '');
  try {
    const r = await api('/api/sandbox/save', { session_id: state.sessions.current, path: rel, content: content });
    if (!r || !r.ok) { toast((r && r.error) || '保存失败'); return; }
    state.ideDirtyMap[rel] = false;
    if (state.ideCache[rel]) {
      state.ideCache[rel].text = content;
      if (r.encoding) state.ideCache[rel].enc = r.encoding;
    }
    renderIdeTabs(); ideUpdateStatus();
    toast('已保存' + (r.encoding ? '（' + r.encoding + '）' : ''));
  } catch (e) {
    toast('保存失败：' + e);
  } finally {
    if (btn) btn.classList.remove('busy');
  }
}

async function ideNew(isDir) {
  const base = state.ideDir || '';
  const input = prompt(isDir ? '新建文件夹（可含子路径）：' : '新建文件（可含子路径）：', base ? base + '/' : '');
  if (input == null) return;
  const rel = input.trim().replace(/^\/+/, '');
  if (!rel) return;
  let r;
  try { r = await api('/api/sandbox/new', { session_id: state.sessions.current, path: rel, dir: !!isDir }); }
  catch (e) { return toast('新建失败：' + e); }
  if (!r || !r.ok) { toast((r && r.error) || '新建失败'); return; }
  toast('已创建：' + rel);
  await loadIdeTree();
  if (!isDir) openIdeFile(rel);
}

async function ideRename() {
  if (!state.ideOpen) return;
  const old = state.ideOpen;
  const cur = old.indexOf('/') >= 0 ? old.slice(old.lastIndexOf('/') + 1) : old;
  const name = prompt('重命名为：', cur);
  if (name == null || !name.trim() || name.trim() === cur) return;
  let r;
  try { r = await api('/api/sandbox/rename', { session_id: state.sessions.current, path: old, name: name.trim() }); }
  catch (e) { return toast('重命名失败：' + e); }
  if (!r || !r.ok) { toast((r && r.error) || '重命名失败'); return; }
  const dir = old.indexOf('/') >= 0 ? old.slice(0, old.lastIndexOf('/') + 1) : '';
  const neu = dir + name.trim();
  ideCloseTab(old);                    // 关闭旧标签（忽略未保存提示已由用户确认过重命名）
  state.ideChanged.delete(old);
  if (state.ideModels[old]) { try { state.ideModels[old].dispose(); } catch (e) { /* 忽略 */ } delete state.ideModels[old]; }
  delete state.ideCache[old]; delete state.ideDirtyMap[old];
  const i = (state.ideTabList || []).indexOf(old);
  if (i >= 0) state.ideTabList.splice(i, 1);
  toast('已重命名');
  await loadIdeTree();
  openIdeFile(neu);
}

async function ideDelete() {
  if (!state.ideOpen) return;
  const rel = state.ideOpen;
  if (!confirm('确定删除「' + rel + '」？此操作不可撤销。')) return;
  let r;
  try { r = await api('/api/sandbox/delete', { session_id: state.sessions.current, path: rel }); }
  catch (e) { return toast('删除失败：' + e); }
  if (!r || !r.ok) { toast((r && r.error) || '删除失败'); return; }
  toast('已删除');
  const i = (state.ideTabList || []).indexOf(rel);
  if (i >= 0) state.ideTabList.splice(i, 1);
  if (state.ideModels[rel]) { try { state.ideModels[rel].dispose(); } catch (e) { /* 忽略 */ } delete state.ideModels[rel]; }
  delete state.ideCache[rel]; delete state.ideDirtyMap[rel];
  state.ideOpen = '';
  if (state.ideEditor) state.ideEditor.setModel(null);
  renderIdeTabs(); ideUpdateStatus();
  await loadIdeTree();
}

/* ---------- 沙箱内搜索 ---------- */
async function ideSearch() {
  const box = $('#ide-search-hits');
  const inp = $('#ide-search-input');
  if (!box || !inp) return;
  const pat = (inp.value || '').trim();
  if (!pat) { box.innerHTML = ''; return; }
  box.innerHTML = '<div class="ide-empty">搜索中…</div>';
  try {
    const r = await api('/api/sandbox/search', { session_id: state.sessions.current, pattern: pat });
    if (!r || !r.ok) { box.innerHTML = '<div class="ide-empty">' + escapeHtml((r && r.error) || '搜索失败') + '</div>'; return; }
    const lines = (r.text || '').split('\n').filter((x) => x.trim());
    if (!lines.length) { box.innerHTML = '<div class="ide-empty">无匹配</div>'; return; }
    box.innerHTML = lines.map((ln) => '<div class="ide-hit" title="' + escapeHtml(ln) + '">' + escapeHtml(ln) + '</div>').join('');
  } catch (e) {
    box.innerHTML = '<div class="ide-empty">搜索失败：' + escapeHtml(String(e)) + '</div>';
  }
}

/* ---------- 命令面板 / 快速打开（Ctrl+Shift+P / Ctrl+P） ---------- */
const IDE_COMMANDS = [
  { title: '保存当前文件', run: () => saveIdeFile() },
  { title: '新建文件', run: () => ideNew(false) },
  { title: '新建文件夹', run: () => ideNew(true) },
  { title: '重命名当前文件', run: () => ideRename() },
  { title: '删除当前文件', run: () => ideDelete() },
  { title: '刷新目录树', run: () => loadIdeTree() },
  { title: '在沙箱内搜索', run: () => ideSwitchSide('search') },
  { title: '显示资源管理器', run: () => ideSwitchSide('explorer') },
  { title: '在 VS Code 中打开当前沙箱', run: () => ideOpenInVsCode() },
  { title: '从 VS Code 导入设置与扩展清单', run: () => ideImportVsCode() },
  { title: '切换到 Solo 模式', run: () => setUiMode('solo') },
];

function ideFileList() {
  const out = [];
  (function walk(items, prefix) {
    (items || []).forEach((it) => {
      const rel = prefix ? prefix + '/' + it.name : it.name;
      if (it.dir) walk(it.children || [], rel); else out.push(rel);
    });
  })((state.ideTree || {}).children || [], '');
  return out;
}

function idePaletteOpen(mode) {
  state.idePaletteMode = mode;
  state.idePaletteSel = 0;
  const box = $('#ide-cmd');
  const inp = $('#ide-cmd-input');
  if (!box || !inp) return;
  box.classList.remove('hidden');
  inp.value = '';
  inp.placeholder = (mode === 'file') ? '输入文件名快速打开…' : '输入命令…';
  inp.focus();
  idePaletteRender('');
}

function idePaletteClose() {
  const b = $('#ide-cmd');
  if (b) b.classList.add('hidden');
}

function idePaletteRender(q) {
  const box = $('#ide-cmd-list');
  if (!box) return;
  const query = (q || '').toLowerCase();
  let items;
  if (state.idePaletteMode === 'file') {
    items = ideFileList().filter((p) => p.toLowerCase().indexOf(query) >= 0)
      .map((p) => ({ title: p, run: () => openIdeFile(p) }));
  } else {
    items = IDE_COMMANDS.filter((c) => c.title.toLowerCase().indexOf(query) >= 0);
  }
  state.idePaletteItems = items.slice(0, 60);
  if (state.idePaletteSel >= state.idePaletteItems.length) state.idePaletteSel = 0;
  box.innerHTML = state.idePaletteItems.map((c, i) =>
    '<div class="ide-cmd-item' + (i === state.idePaletteSel ? ' active' : '') + '" data-i="' + i + '">' +
    escapeHtml(c.title) + '</div>').join('') || '<div class="ide-empty">无匹配</div>';
  $$('#ide-cmd-list .ide-cmd-item').forEach((el) => { el.onclick = () => idePaletteRun(Number(el.dataset.i)); });
}

function idePaletteRun(i) {
  const it = (state.idePaletteItems || [])[i];
  if (!it) return;
  idePaletteClose();
  try { it.run(); } catch (e) { /* 忽略 */ }
}

/* ---------- 阶段 3：VS Code 联动 ---------- */
async function ideOpenInVsCode() {
  try {
    const r = await api('/api/sandbox/open_vscode', { session_id: state.sessions.current, path: state.ideOpen || '' });
    if (!r || !r.ok) { toast((r && r.error) || '打开失败'); return; }
    toast('已在 VS Code 中打开');
  } catch (e) { toast('打开失败：' + e); }
}

function ideInfoShow(title, rows) {
  const box = $('#ide-info');
  if (!box) return;
  $('#ide-info-title').textContent = title;
  $('#ide-info-body').innerHTML = rows;
  box.classList.remove('hidden');
}

async function ideImportVsCode() {
  let r;
  try { r = await api('/api/vscode/import', {}); }
  catch (e) { return toast('读取 VS Code 配置失败：' + e); }
  if (!r || !r.ok) { toast((r && r.error) || '读取 VS Code 配置失败'); return; }
  const st = r.settings || {};
  state.ideMonacoDefaults = st;
  const LABELS = {
    fontSize: '字号 (editor.fontSize)',
    tabSize: '缩进 (editor.tabSize)',
    fontFamily: '字体 (editor.fontFamily)',
    wordWrap: '自动换行 (editor.wordWrap)',
    renderWhitespace: '空白字符 (editor.renderWhitespace)',
    mouseWheelZoom: 'Ctrl+滚轮缩放 (editor.mouseWheelZoom)',
  };
  const applied = Object.keys(LABELS).filter((k) => st[k] !== undefined && st[k] !== '');
  if (state.ideEditor) {
    const opt = {};
    if (st.fontSize) opt.fontSize = st.fontSize;
    if (st.tabSize) opt.tabSize = st.tabSize;
    if (st.fontFamily) opt.fontFamily = st.fontFamily;
    if (st.wordWrap) opt.wordWrap = st.wordWrap;
    if (st.renderWhitespace) opt.renderWhitespace = st.renderWhitespace;
    if (typeof st.mouseWheelZoom === 'boolean') opt.mouseWheelZoom = st.mouseWheelZoom;
    if (Object.keys(opt).length) state.ideEditor.updateOptions(opt);
  }
  if (st.tabSize) {
    Object.keys(state.ideModels || {}).forEach((k) => {
      try { state.ideModels[k].updateOptions({ tabSize: st.tabSize, insertSpaces: true }); } catch (e) { /* 忽略 */ }
    });
  }
  const exts = r.extensions || [];
  const paths = r.paths || {};
  let html = '<p class="hint">只读同步：设置已应用到编辑器；扩展仅列出，<b>不会安装</b>（要真正使用插件请用「在 VS Code 中打开」）。</p>';
  html += '<h4>已应用设置（共读取 ' + escapeHtml(String(st._count || 0)) + ' 项）</h4>';
  html += applied.length
    ? '<ul>' + applied.map((k) => '<li>' + escapeHtml(LABELS[k]) + '：' + escapeHtml(String(st[k])) + '</li>').join('') + '</ul>'
    : '<p class="hint">你的 VS Code 没有设置这几项（字号 / 缩进 / 字体 / 换行 / 空白字符 / 滚轮缩放）。</p>';
  html += '<h4>检测到的扩展（' + exts.length + '）</h4>';
  html += exts.length
    ? '<ul>' + exts.slice(0, 200).map((e) => '<li>' + escapeHtml(e) + '</li>').join('') + '</ul>'
    : '<p class="hint">未检测到扩展目录。</p>';
  html += '<h4>配置来源</h4><ul>' +
    '<li>设置：' + escapeHtml(paths.settings || '未找到') + '</li>' +
    '<li>快捷键：' + escapeHtml(paths.keybindings || '未找到') + '</li>' +
    '<li>扩展目录：' + escapeHtml(paths.extensions || '未找到') + '</li></ul>';
  ideInfoShow('从 VS Code 导入', html);
}

// AI 改文件后：高亮 + 刷新；若改的正是当前打开的文件且未编辑，则自动重载
let ideReloadTimer = null;
function ideOnToolDone(obj) {
  if (!obj || !obj.write) return;
  const a = obj.args || {};
  [a.path, a.from, a.to].forEach((p) => { if (p) state.ideChanged.add(String(p).replace(/\\/g, '/')); });
  if (state.uiMode !== 'ide') return;
  clearTimeout(ideReloadTimer);
  ideReloadTimer = setTimeout(async () => {
    const open = state.ideOpen;
    await loadIdeTree();
    const openIsImage = !!(open && state.ideCache[open] && state.ideCache[open].image);
    if (open && state.ideChanged.has(open) && !state.ideDirtyMap[open] && !openIsImage) {
      try {
        const r = await api('/api/sandbox/file', { session_id: state.sessions.current, path: open });
        if (r && r.ok && state.ideOpen === open) {
          state.ideCache[open] = { text: r.text || '', enc: r.encoding || '' };
          if (state.ideModels[open]) state.ideModels[open].setValue(r.text || '');
          else { const t = $('#ide-text'); if (t) t.value = r.text || ''; }
          ideSetDirty(open, false);
          state.ideChanged.delete(open);
          await loadIdeTree();
          toast('AI 修改了当前文件，已刷新');
        }
      } catch (e) { /* 忽略 */ }
    }
  }, 250);
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

/* ---------- 节点外观（大小 / 颜色）与图谱力学 ---------- */
// 与后端 NODE_PALETTE 保持一致
const KB_PALETTE = [
  ['red', '#e5484d'], ['orange', '#e8890c'], ['yellow', '#d9a406'],
  ['green', '#30a46c'], ['teal', '#12a594'], ['blue', '#4d6bfe'],
  ['purple', '#8e4ec6'], ['pink', '#d6409f'], ['gray', '#8b8d98'],
];
const KB_GRAPH_DEFAULT = { repel: 9000, center: 0.3, link: 0.04, distance: 130 };

// 把 hex 按比例加深/变浅（amt 为负＝加深）
function shadeColor(hex, amt) {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex || '');
  if (!m) return hex;
  const num = parseInt(m[1], 16);
  const f = (v) => Math.max(0, Math.min(255, Math.round(v + v * amt)));
  const r = f((num >> 16) & 255), g = f((num >> 8) & 255), b = f(num & 255);
  return '#' + [r, g, b].map((v) => v.toString(16).padStart(2, '0')).join('');
}

// 由 state.kbData.graph 生成 vis 物理参数
function kbPhysicsOptions() {
  const g = Object.assign({}, KB_GRAPH_DEFAULT, (state.kbData && state.kbData.graph) || {});
  return {
    stabilization: true,
    barnesHut: {
      gravitationalConstant: -Math.abs(Number(g.repel) || KB_GRAPH_DEFAULT.repel),
      centralGravity: Number(g.center),
      springConstant: Number(g.link),
      springLength: Number(g.distance),
    },
  };
}

const KB_SLIDERS = [
  ['#kg-repel', '#kg-repel-val', 'repel', (v) => String(Math.round(v))],
  ['#kg-center', '#kg-center-val', 'center', (v) => Number(v).toFixed(2)],
  ['#kg-link', '#kg-link-val', 'link', (v) => Number(v).toFixed(3)],
  ['#kg-distance', '#kg-distance-val', 'distance', (v) => String(Math.round(v))],
];

// 用当前 kbData.graph 刷新四个滑块与数值
function syncGraphSliders() {
  const g = Object.assign({}, KB_GRAPH_DEFAULT, (state.kbData && state.kbData.graph) || {});
  KB_SLIDERS.forEach(([sel, valSel, key, fmt]) => {
    const el = $(sel);
    if (!el) return;
    el.value = g[key];
    const out = $(valSel);
    if (out) out.textContent = fmt(el.value);
  });
}

// 拖动滑块：实时改物理参数并保存
function bindKbGraphSettings() {
  const apply = () => {
    if (!state.kbData) return;
    const g = Object.assign({}, KB_GRAPH_DEFAULT, state.kbData.graph || {});
    KB_SLIDERS.forEach(([sel, valSel, key, fmt]) => {
      const el = $(sel);
      if (!el) return;
      g[key] = (key === 'center' || key === 'link') ? parseFloat(el.value) : parseInt(el.value, 10);
      const out = $(valSel);
      if (out) out.textContent = fmt(el.value);
    });
    state.kbData.graph = g;
    if (kbNetwork) {
      const p = kbPhysicsOptions().barnesHut;
      kbNetwork.setOptions({ physics: { enabled: true, barnesHut: p } });
    }
    scheduleKbSave();
  };
  KB_SLIDERS.forEach(([sel]) => {
    const el = $(sel);
    if (el) el.oninput = apply;
  });
  const reset = $('#kg-reset');
  if (reset) {
    reset.onclick = () => {
      if (!state.kbData) return;
      state.kbData.graph = Object.assign({}, KB_GRAPH_DEFAULT);
      syncGraphSliders();
      if (kbNetwork) kbNetwork.setOptions({ physics: kbPhysicsOptions() });
      scheduleKbSave();
      toast('已恢复图谱默认');
    };
  }
}

// 渲染节点颜色色块（含「默认」），当前选中高亮
function renderColorSwatches(selected) {
  const box = $('#kd-color');
  if (!box) return;
  state.kbColor = selected || '';
  const items = [['默认', '']].concat(KB_PALETTE);      // [名称, hex]
  box.innerHTML = items.map(([name, hex]) => {
    const on = (state.kbColor || '') === hex ? ' on' : '';
    const cls = 'kb-color' + (hex ? '' : ' kb-color-def') + on;
    const st = hex ? ' style="background:' + hex + '"' : '';
    return '<button type="button" class="' + cls + '"' + st +
           ' data-c="' + hex + '" title="' + (name || '') + '"></button>';
  }).join('');
  box.querySelectorAll('.kb-color').forEach((b) => {
    b.onclick = () => {
      state.kbColor = b.dataset.c || '';
      renderColorSwatches(state.kbColor);
    };
  });
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
  state.kbData = { nodes: [], edges: [], graph: {} };
  state.kbError = null;
  showNodeForm(null);
  let data = null;
  try {
    if (key === 'session') {
      const r = await api('/api/memory/get', { session_id: state.sessions.current });
      if (token !== kbLoadSeq) return;
      if (!r.ok) state.kbError = '会话记忆读取失败：' + (r.error || '');
      else {
        data = { nodes: r.memory.nodes || [], edges: r.memory.edges || [], graph: r.memory.graph || {} };
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
        data = { nodes: r.kb.nodes || [], edges: r.kb.edges || [], graph: r.kb.graph || {} };
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
  state.kbData = data || { nodes: [], edges: [], graph: {} };
  if (!state.kbData.graph) state.kbData.graph = {};
  syncGraphSliders();
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

/* 对称关系（相关 / 同义 / 对立）默认画双向箭头；sym 标记由后端词表下发 */
const SYM_RELATIONS_FALLBACK = ['related', 'same', 'opposite'];

function isSymmetricRel(code) {
  const m = relationMeta(code);
  if (m && typeof m.sym === 'boolean') return m.sym;
  return SYM_RELATIONS_FALLBACK.indexOf(code || 'related') >= 0;
}

// 老数据没有 directed 字段时，按关系类型推断
function isEdgeDirected(e) {
  if (e && typeof e.directed === 'boolean') return e.directed;
  return !isSymmetricRel(e && e.relation);
}

function edgeArrows(directed) {
  return directed
    ? { to: { enabled: true, scaleFactor: 0.55 } }
    : { to: { enabled: true, scaleFactor: 0.55 },
        from: { enabled: true, scaleFactor: 0.55 } };
}

function renderRelationHelp() {
  const box = $('#help-relations');
  if (!box) return;
  const list = state.relations || [];
  box.innerHTML = list.length
    ? list.map((r) => '<li><span style="color:' + relationColor(r.code) + '">●</span> ' +
        '<strong>' + escapeHtml(r.label) + '</strong>（' + escapeHtml(r.code) + '）：' +
        escapeHtml(r.desc) + (r.sym ? '　<b>⇄ 默认双向</b>' : '') + '</li>').join('')
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
    const sz = (typeof n.size === 'number' && n.size > 0) ? n.size : 16;
    const o = {
      id: n.id, label: String(n.title || ''), title: String(n.content || '').slice(0, 200),
      shape: 'dot', size: sz,
      font: { color: fontColor, size: 14 },
    };
    if (n.color) {                       // 自定义填充色：边框取同色加深，深浅主题都清晰
      const border = shadeColor(n.color, -0.28);
      o.color = { background: n.color, border: border,
                  highlight: { background: n.color, border: shadeColor(n.color, -0.4) } };
    } else {
      o.color = { background: dark ? '#2a2a2a' : '#ffffff', border: lineColor };
    }
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
      arrows: edgeArrows(isEdgeDirected(e)),
      width: 1.4,
    };
  });
  try {
    const ds = { nodes: new vis.DataSet(nodes), edges: new vis.DataSet(edges) };
    kbNetwork = new vis.Network(el, ds, {
    physics: kbPhysicsOptions(),
    interaction: { hover: true },
    locale: 'cn',
    locales: VIS_LOCALES,
    manipulation: {
      enabled: true, initiallyActive: false, addNode: false, editNode: false,
      addEdge: (data, cb) => {
        const id = 'e_' + Math.random().toString(36).slice(2, 10);
        const rel = 'related';
        const label = relationLabel(rel);
        const directed = !isSymmetricRel(rel);
        state.kbData.edges.push({ id, from: data.from, to: data.to,
                                  label: label, relation: rel, directed: directed,
                                  type: 'relation' });
        cb({ id, from: data.from, to: data.to, label: label,
             font: { color: relationColor(rel), size: 11, align: 'middle' },
             color: { color: relationColor(rel) },
             arrows: edgeArrows(directed), width: 1.4 });
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
  const sz = (typeof node.size === 'number' && node.size > 0) ? node.size : 16;
  $('#kd-size').value = sz;
  $('#kd-size-val').textContent = sz;
  renderColorSwatches(node.color || '');
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
  const sz = parseInt($('#kd-size').value, 10);
  if (sz && sz !== 16) n.size = sz; else delete n.size;
  if (state.kbColor) n.color = state.kbColor; else delete n.color;
  n.updated_at = Date.now() / 1000;
  if (kbNetwork && kbNetwork._ds) {
    const patch = { id: n.id, label: n.title, title: String(n.content || '').slice(0, 200),
                    size: n.size || 16 };
    if (n.color) {
      patch.color = { background: n.color, border: shadeColor(n.color, -0.28),
                      highlight: { background: n.color, border: shadeColor(n.color, -0.4) } };
    } else {
      const dark = document.documentElement.getAttribute('data-theme') === 'dark';
      patch.color = { background: dark ? '#2a2a2a' : '#ffffff',
                      border: dark ? '#3a3a3a' : '#dedee2' };
    }
    kbNetwork._ds.nodes.update(patch);
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
  syncEdgeDir();                                  // 方向随关系类型给个默认值
  $('#ke-relation').onchange = syncEdgeDir;       // 换关系时重新给默认（之后可手动改）
  $('#kb-node-form').classList.add('hidden');
  $('#kb-node-empty').classList.add('hidden');
  $('#kb-edge-form').classList.remove('hidden');
}

// 选关系时把方向切到该关系的默认值：对称关系双向，其余单向
function syncEdgeDir() {
  const sel = $('#ke-dir');
  if (!sel) return;
  const rel = $('#ke-relation').value || 'related';
  sel.value = isSymmetricRel(rel) ? '0' : '1';
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
  const directed = ($('#ke-dir') ? $('#ke-dir').value : '1') === '1';
  const label = relationLabel(rel);
  state.kbData.edges = state.kbData.edges || [];
  // 无向边反着连也算重复，避免出现 A⇄B / B⇄A 两条重影
  const dup = state.kbData.edges.some((e) => (e.relation || 'related') === rel &&
    ((e.from === kbEdgeFrom && e.to === to) ||
     (!directed && e.from === to && e.to === kbEdgeFrom)));
  if (dup) {
    toast('这两个节点之间已有相同关系');
    return;
  }
  const e = { id: 'e_' + Math.random().toString(36).slice(2, 10),
              from: kbEdgeFrom, to: to, label: label, relation: rel,
              directed: directed, type: 'relation' };
  state.kbData.edges.push(e);
  if (kbNetwork && kbNetwork._ds) {
    kbNetwork._ds.edges.add({
      id: e.id, from: e.from, to: e.to, label: e.label,
      font: { color: relationColor(rel), size: 11, align: 'middle' },
      color: { color: relationColor(rel) },
      arrows: edgeArrows(directed), width: 1.4,
    });
  } else {
    drawKbGraph();
  }
  scheduleKbSave();
  toast('已添加连线：' + label + (directed ? '（单向）' : '（双向）'));
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
    const r = await api('/api/kb/save', { id: key, nodes: data.nodes, edges: data.edges, graph: data.graph });
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
  $('#kd-size').oninput = () => { $('#kd-size-val').textContent = $('#kd-size').value; };
  bindKbGraphSettings();
  $('#profile-open-kb').onclick = () => {
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
  document.querySelectorAll('#chat code.math-pending, #gal-text code.math-pending').forEach((el) => {
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
  state.sessions = data.sessions;
  state.kbs = data.kbs || [];
  state.builtinPresets = data.builtin_presets || {};
  state.builtinPresetLabel = data.builtin_preset_label || '默认（内置）';
  state.searchProviders = data.search_providers || {};
  state.relations = data.relations || [];
  renderRelationHelp();

  applyTheme(state.config.theme);
  galApplyTheme();
  renderSessions();
  renderMessages();
  renderContextBar(data.context);
  // 首屏先渲染完；把已经就绪的库补齐（未就绪的等 onLibReady 回调）
  Object.keys(window.__libs || {}).forEach(onLibReady);

  $('#btn-new').onclick = newSession;
  $('#btn-send').onclick = sendMessage;
  $('#attach-remove').onclick = clearAttach;
  $('#btn-stop').onclick = stopStreaming;
  $('#btn-compress').onclick = compressContextManually;
  $('#btn-settings').onclick = openModal;
  $('#btn-close-modal').onclick = closeModal;
  $('#btn-cancel').onclick = closeModal;
  $('#btn-save').onclick = saveModal;
  setupSettingsSearch();                                  // 设置内搜索框（只绑一次）
  document.addEventListener('keydown', onSettingsKeydown, true);   // 设置内键盘操作（捕获相）

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

  // 语音降噪开关（持久化到 config.speech_denoise）
  state.denoise = !!state.config.speech_denoise;
  syncDenoisePill();
  $('#btn-denoise').onclick = toggleDenoise;

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
  input.addEventListener('input', () => { autoGrow(); hideSttChip(); });
  syncSendBtn();                 // 初始：输入为空时发送键置灰
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  });

  // 语音输入
  $('#btn-mic').onclick = toggleMic;
  $('#stt-revert').onclick = revertSttChip;

  // Agent：开关 + 沙箱目录
  $('#btn-agent').onclick = toggleAgent;
  $('#btn-sandbox').onclick = pickSandbox;
  if ($('#btn-outside-read')) $('#btn-outside-read').onclick = toggleOutsideRead;
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
    const gm = $('#gal-menu');
    if (gm && !gm.classList.contains('hidden')) { galToggleMenu(false); return; }
    const cp = $('#gal-clone-panel');
    if (cp && !cp.classList.contains('hidden')) { galToggleClone(false); return; }
    const sp = $('#gal-script-panel');
    if (sp && !sp.classList.contains('hidden')) { galToggleScript(false); return; }
    const p = $('#gal-review-panel');
    if (p && !p.classList.contains('hidden')) { galToggleReview(false); return; }
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
  if ($('#gal-tts')) $('#gal-tts').onclick = galToggleTts;
  if ($('#gal-skip')) $('#gal-skip').onclick = galSkipSpeech;
  if ($('#speech-tts-test')) $('#speech-tts-test').onclick = galTestTts;
  if ($('#speech-tts-voices')) $('#speech-tts-voices').onclick = galLoadTtsVoices;
  if ($('#gal-edit')) {
    $('#gal-edit').onclick = () => galOpenEditor(galState.charId || (curSession() || {}).gal_character_id || '', false);
  }
  if ($('#gal-new-char')) $('#gal-new-char').onclick = () => galOpenEditor('', true);
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
  // 底部两层联动：① 选项浮层高度变化 → 把对白框顶上去；② 对白框顶边 → 立绘层底边（立绘永不被压）
  if (typeof ResizeObserver !== 'undefined') {
    const optBox = $('#gal-options');
    const dlg = $('#gal-dialog');
    if (optBox) {
      new ResizeObserver(() => {
        if (!dlg) return;
        const h = optBox.childElementCount ? optBox.offsetHeight : 0;
        dlg.style.bottom = h ? (h + 26) + 'px' : '';
        galSyncSpriteLift();
      }).observe(optBox);
    }
    if (dlg) new ResizeObserver(galSyncSpriteLift).observe(dlg);
    window.addEventListener('resize', galSyncSpriteLift);
  }
  document.addEventListener('keydown', (e) => {
    galState.fast = !!e.ctrlKey;      // 按住 Ctrl 快进：以实时修饰键为准，避免 keyup 丢失后卡在 6 倍速
    galHotkeys(e);
  });
  document.addEventListener('keyup', (e) => { galState.fast = !!e.ctrlKey; });
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
    $('#gal-ed-tts-add').onclick = () => {
      const n = (prompt('新建配音预设名称（如：日常 / 激动）') || '').trim();
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
  if ($('#ide-notice-close')) $('#ide-notice-close').onclick = () => $('#ide-notice').classList.add('hidden');
  $('#ide-newfile').onclick = () => ideNew(false);
  $('#ide-newdir').onclick = () => ideNew(true);
  $('#ide-rename').onclick = ideRename;
  $('#ide-deldir').onclick = ideDelete;
  $('#ide-save').onclick = saveIdeFile;
  $('#ide-text').addEventListener('input', () => ideSetDirty(state.ideOpen, true));
  // 活动栏
  $$('#ide-panel .act-btn[data-act]').forEach((b) => {
    b.onclick = () => ideSwitchSide(b.dataset.act);
  });
  if ($('#ide-open-vscode')) $('#ide-open-vscode').onclick = ideOpenInVsCode;
  // 沙箱内搜索
  if ($('#ide-search-input')) {
    $('#ide-search-input').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); ideSearch(); }
    });
  }
  // 命令面板
  if ($('#ide-cmd-input')) {
    $('#ide-cmd-input').addEventListener('input', (e) => { state.idePaletteSel = 0; idePaletteRender(e.target.value); });
    $('#ide-cmd-input').addEventListener('keydown', (e) => {
      const items = state.idePaletteItems || [];
      if (e.key === 'ArrowDown') { e.preventDefault(); state.idePaletteSel = Math.min(items.length - 1, state.idePaletteSel + 1); idePaletteRender($('#ide-cmd-input').value); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); state.idePaletteSel = Math.max(0, state.idePaletteSel - 1); idePaletteRender($('#ide-cmd-input').value); }
      else if (e.key === 'Enter') { e.preventDefault(); idePaletteRun(state.idePaletteSel); }
      else if (e.key === 'Escape') { e.preventDefault(); idePaletteClose(); }
    });
    $('#ide-cmd').addEventListener('mousedown', (e) => { if (e.target && e.target.id === 'ide-cmd') idePaletteClose(); });
  }
  if ($('#ide-info-close')) $('#ide-info-close').onclick = () => $('#ide-info').classList.add('hidden');
  // IDE 快捷键（仅 IDE 模式生效）
  document.addEventListener('keydown', (e) => {
    if (state.uiMode !== 'ide') return;
    const mod = e.ctrlKey || e.metaKey;
    if (!mod) return;
    const k = (e.key || '').toLowerCase();
    if (k === 's') { e.preventDefault(); saveIdeFile(); }
    else if (k === 'p' && e.shiftKey) { e.preventDefault(); idePaletteOpen('cmd'); }
    else if (k === 'p') { e.preventDefault(); idePaletteOpen('file'); }
  });
  applyUiMode((state.config && state.config.ui_mode) || 'solo');

  // 图片
  $('#btn-image').onclick = () => $('#file-input').click();
  $('#file-input').onchange = (e) => {
    if (e.target.files && e.target.files[0]) attachImageBlob(e.target.files[0]);
    e.target.value = '';
  };
  document.addEventListener('paste', (e) => {
    const items = (e.clipboardData || {}).items || [];
    for (const it of items) {
      if (it.type && it.type.startsWith('image/')) {
        const f = it.getAsFile();
        if (f) { attachImageBlob(f); e.preventDefault(); }
        return;
      }
    }
  });
  window.addEventListener('dragover', (e) => e.preventDefault());
  window.addEventListener('drop', (e) => {
    e.preventDefault();
    const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
    if (f && f.type && f.type.startsWith('image/')) attachImageBlob(f);
  });

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
