'use strict';

/* 空状态标记只在 index.html 维护一份：脚本在 body 末尾执行，此时 #empty 已存在。
   重新渲染时直接复用这份快照，避免两处各写一遍导致走样。 */
const EMPTY_HTML = (() => {
  const el = document.getElementById('empty');
  return el ? el.outerHTML : '';
})();

/* ============================================================
   会话渲染
   ============================================================ */
function curSession() {
  return state.sessions.sessions.find((s) => s.id === state.sessions.current) || null;
}

function renderSessions() {
  const box = $('#session-list');
  box.innerHTML = '';
  // 剧场会话与主会话分列表展示：GAL 模式只看 gal_scope=gal，其余模式反之。
  const inGal = state.uiMode === 'gal';
  const all = (state.sessions.sessions || []).filter((s) => {
    const isGal = (s.gal_scope || 'main') === 'gal';
    return inGal ? isGal : !isGal;
  });
  all.forEach((s) => {
    const live = !!(state.streams && state.streams[s.id])
      || !!(state.streamingSids && state.streamingSids[s.id]);
    const unread = !!(state.unread && state.unread[s.id]);
    const el = document.createElement('div');
    el.className = 'session-item' + (s.id === state.sessions.current ? ' active' : '')
      + (live ? ' streaming' : '') + (unread ? ' unread' : '')
      + (s.pinned ? ' pinned' : '');
    // 悬停显示完整会话名：标题过长时列表里会被省略号截断，靠 title 看全名
    el.title = (s.pinned ? '已置顶 · ' : '') + (s.title || '新对话');
    const dot = document.createElement('span');
    dot.className = 'sdot';
    dot.title = live ? '正在生成' : (unread ? '有新回复' : '');
    const title = document.createElement('span');
    title.className = 'stitle';
    title.textContent = s.title || '新对话';
    const pin = document.createElement('span');
    pin.className = 'spin';
    pin.innerHTML = icon('pin');
    pin.title = s.pinned ? '取消置顶' : '置顶会话';
    pin.onclick = (e) => { e.stopPropagation(); toggleSessionPin(s.id); };
    const exp = document.createElement('span');
    exp.className = 'sexp';
    exp.textContent = '⤓';
    exp.title = '导出为 Markdown';
    exp.onclick = (e) => { e.stopPropagation(); exportSession(s.id); };
    const ren = document.createElement('span');
    ren.className = 'sren';
    ren.innerHTML = icon('pencil');
    ren.title = '重命名';
    ren.onclick = (e) => { e.stopPropagation(); renameSession(s.id); };
    const del = document.createElement('span');
    del.className = 'sdel';
    del.innerHTML = icon('trash');
    del.title = '删除会话';
    del.onclick = (e) => { e.stopPropagation(); deleteSession(s.id); };
    // 操作键收进一个悬浮层：不占标题宽度，窄侧栏下标题才不会被挤成「AI...」
    const acts = document.createElement('span');
    acts.className = 'sacts';
    acts.appendChild(pin);
    acts.appendChild(exp);
    acts.appendChild(ren);
    acts.appendChild(del);
    el.appendChild(dot);
    if (s.pinned) {                       // 已置顶：标题前常驻一枚小图钉
      const mark = document.createElement('span');
      mark.className = 'spin-mark';
      mark.innerHTML = icon('pin');
      mark.title = '已置顶';
      el.appendChild(mark);
    }
    el.appendChild(title);
    el.appendChild(acts);
    el.onclick = () => selectSession(s.id);
    el.ondblclick = () => renameSession(s.id);
    // 键盘可达：会话项本身与四个操作键都能 Tab 到、Enter 触发
    makeClickable(el, () => selectSession(s.id), '会话：' + (s.title || '新对话'));
    makeClickable(pin, () => toggleSessionPin(s.id),
                  (s.pinned ? '取消置顶会话：' : '置顶会话：') + (s.title || '新对话'));
    makeClickable(exp, () => exportSession(s.id), '导出会话：' + (s.title || '新对话'));
    makeClickable(ren, () => renameSession(s.id), '重命名会话：' + (s.title || '新对话'));
    makeClickable(del, () => deleteSession(s.id), '删除会话：' + (s.title || '新对话'));
    box.appendChild(el);
  });
  updateStatusLine();
  syncAgentUI();
  if (typeof renderKbBadge === 'function') renderKbBadge();   // 侧栏知识库「未绑定」角标
  if (typeof syncAutoChatFromSession === 'function') syncAutoChatFromSession();
}

/* ---------------- 会话搜索浮层 ---------------- */
const ssState = { open: false, items: [], timer: null };

function ssOpen() {
  const modal = $('#session-search-modal');
  const input = $('#ss-input');
  if (!modal || !input) return;
  ssState.open = true;
  modal.classList.remove('hidden');
  input.value = '';
  const btn = $('#session-search-btn');
  if (btn) { btn.classList.add('active'); btn.setAttribute('aria-expanded', 'true'); }
  input.focus();
  ssFetch('');
}

function ssClose() {
  const modal = $('#session-search-modal');
  const input = $('#ss-input');
  ssState.open = false;
  ssState.items = [];
  clearTimeout(ssState.timer);
  if (modal) modal.classList.add('hidden');
  if (input) input.value = '';
  const btn = $('#session-search-btn');
  if (btn) { btn.classList.remove('active'); btn.setAttribute('aria-expanded', 'false'); }
}

async function ssFetch(q) {
  const list = $('#ss-results');
  if (list) list.innerHTML = '<div class="ss-empty">读取中…</div>';
  try {
    const r = await api('/api/session/search', {
      q: q,
      scope: state.uiMode === 'gal' ? 'gal' : 'main',
    });
    if (!ssState.open) return;                 // 请求返回前已关闭
    ssState.items = (r && r.items) || [];
    ssRender(q);
  } catch (e) {
    if (list) list.innerHTML = '<div class="ss-empty">搜索失败：' + escapeHtml(String(e)) + '</div>';
  }
}

function ssRender(q) {
  const list = $('#ss-results');
  if (!list) return;
  list.innerHTML = '';
  if (!ssState.items.length) {
    const d = document.createElement('div');
    d.className = 'ss-empty';
    d.textContent = q ? '没有匹配的会话' : '还没有会话';
    list.appendChild(d);
    return;
  }
  ssState.items.forEach((it) => {
    const row = document.createElement('div');
    row.className = 'ss-row';
    const head = document.createElement('div');
    head.className = 'ss-row-head';
    const title = document.createElement('span');
    title.className = 'ss-title';
    title.textContent = it.title || '新对话';
    const meta = document.createElement('span');
    meta.className = 'ss-meta';
    meta.textContent = (it.msg_count || 0) + ' 条';
    head.appendChild(title);
    head.appendChild(meta);
    row.appendChild(head);
    (it.hits || []).slice(0, 2).forEach((h) => {
      const p = document.createElement('div');
      p.className = 'ss-hit';
      p.textContent = h.snippet;
      row.appendChild(p);
    });
    row.onclick = () => ssPick(it.id);
    list.appendChild(row);
  });
}

async function ssPick(id) {
  ssClose();
  await selectSession(id);
}

function bindSessionSearch() {
  const btn = $('#session-search-btn');
  const modal = $('#session-search-modal');
  const input = $('#ss-input');
  if (btn) btn.onclick = () => ssOpen();
  if ($('#ss-close')) $('#ss-close').onclick = () => ssClose();
  if (modal) modal.addEventListener('click', (e) => { if (e.target === modal) ssClose(); });
  if (input) {
    input.addEventListener('input', (e) => {
      clearTimeout(ssState.timer);
      const v = e.target.value;
      ssState.timer = setTimeout(() => ssFetch(v), 180);
    });
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') { e.preventDefault(); ssClose(); }
      else if (e.key === 'Enter' && ssState.items.length) {
        e.preventDefault(); ssPick(ssState.items[0].id);
      }
    });
  }
}

/* 把某个会话导出成 Markdown 文件下载到本地 */
async function exportSession(sid) {
  try {
    const r = await api('/api/session/export', { session_id: sid });
    if (!r || !r.ok) throw new Error((r && r.error) || '导出失败');
    const blob = new Blob([r.markdown || ''], { type: 'text/markdown;charset=utf-8' });
    saveBlobUrl(URL.createObjectURL(blob), r.filename || 'conversation.md');
    toast('已导出 ' + (r.filename || ''));
  } catch (e) {
    toast('导出失败：' + (e && e.message ? e.message : e), 'error');
  }
}

/* 最后一条用户气泡上的文档标签（附件解析进度往它上面写） */
function lastUserDocChip() {
  const users = document.querySelectorAll('#chat .msg.user');
  const lastU = users[users.length - 1];
  return lastU ? lastU.querySelector('.msg-doc') : null;
}

/* ============================================================
   对话内查找（Ctrl+F）：高亮命中并逐条跳转
   ============================================================ */
const findState = { hits: [], idx: -1, q: '' };

function findClear() {
  const marks = Array.prototype.slice.call(document.querySelectorAll('#chat mark.find-hit'));
  const parents = new Set();
  marks.forEach((m) => {
    if (!m.parentNode) return;
    parents.add(m.parentNode);
    m.parentNode.replaceChild(document.createTextNode(m.textContent), m);   // 还原成纯文本
  });
  parents.forEach((p) => { try { p.normalize(); } catch (e) { /* 忽略 */ } });
  findState.hits = [];
  findState.idx = -1;
}

function findRun(q) {
  findClear();
  findState.q = q || '';
  const box = $('#find-count');
  if (!findState.q) { if (box) box.textContent = ''; return; }
  const root = $('#chat');
  if (!root) return;
  const needle = findState.q.toLowerCase();
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(n) {
      if (!n.nodeValue || n.nodeValue.toLowerCase().indexOf(needle) < 0) return NodeFilter.FILTER_REJECT;
      const p = n.parentElement;
      // 操作条按钮、搜索过程、图片、查找框本身都不该被高亮
      if (!p || p.closest('.msg-actions, .find-bar, .msg-image, .search-steps')) return NodeFilter.FILTER_REJECT;
      return NodeFilter.FILTER_ACCEPT;
    },
  });
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  nodes.forEach((n) => {
    const text = n.nodeValue;
    const low = text.toLowerCase();
    const frag = document.createDocumentFragment();
    let i = 0;
    while (i < text.length) {
      const k = low.indexOf(needle, i);
      if (k < 0) { frag.appendChild(document.createTextNode(text.slice(i))); break; }
      if (k > i) frag.appendChild(document.createTextNode(text.slice(i, k)));
      const mk = document.createElement('mark');
      mk.className = 'find-hit';
      mk.textContent = text.slice(k, k + needle.length);
      frag.appendChild(mk);
      i = k + needle.length;
    }
    if (n.parentNode) n.parentNode.replaceChild(frag, n);
  });
  findState.hits = Array.prototype.slice.call(root.querySelectorAll('mark.find-hit'));
  if (findState.hits.length) findGo(0);
  else if (box) box.textContent = '0/0';
}

function findGo(step) {
  const hits = findState.hits;
  const box = $('#find-count');
  if (!hits.length) { if (box) box.textContent = '0/0'; return; }
  if (findState.idx >= 0 && hits[findState.idx]) hits[findState.idx].classList.remove('cur');
  findState.idx = ((findState.idx + (step || 1)) % hits.length + hits.length) % hits.length;
  const cur = hits[findState.idx];
  cur.classList.add('cur');
  try { cur.scrollIntoView({ block: 'center', behavior: 'smooth' }); } catch (e) { cur.scrollIntoView(); }
  if (box) box.textContent = (findState.idx + 1) + '/' + hits.length;
}

function findClose() {
  const bar = $('#find-bar');
  if (bar) bar.classList.add('hidden');
  findClear();
  findState.q = '';
}

function findOpen() {
  const bar = $('#find-bar');
  if (!bar) return;
  bar.classList.remove('hidden');
  const inp = $('#find-input');
  if (inp) { inp.focus(); inp.select(); }
}

function setupFindBar() {
  const inp = $('#find-input');
  if (!inp) return;
  let timer = null;
  inp.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(() => findRun(inp.value.trim()), 180);    // 防抖：别每敲一下就重排一次 DOM
  });
  inp.addEventListener('keydown', (e) => {
    if (isImeComposing(e)) return;
    if (e.key === 'Enter') { e.preventDefault(); findGo(e.shiftKey ? -1 : 1); }
    else if (e.key === 'Escape') { e.preventDefault(); findClose(); }
  });
  if ($('#find-next')) $('#find-next').onclick = () => findGo(1);
  if ($('#find-prev')) $('#find-prev').onclick = () => findGo(-1);
  if ($('#find-close')) $('#find-close').onclick = () => findClose();
}

function updateStatusLine() {
  const c = state.config || {};
  const p = state.profile || {};
  const tag = (v) => (v && String(v).trim() ? '已配置' : '未配置');
  $('#status-line').innerHTML =
    '聊天 API：' + tag(c.chat_api_key) + ' · 图像 API：' + tag(c.vision_api_key) +
    '<br>AI 记录画像：' + (p.auto_learn_enabled ? '开启' : '关闭');
}

/* ---------------- 上下文容量圆环 ---------------- */
const ctxRemind = { session: null, fired: {} };   // 每会话已触发的提醒档位（10 / 5）
const CTX_RING_R = 9.5;
const CTX_RING_LEN = 2 * Math.PI * CTX_RING_R;
let _ctxRing = null;

/* 懒建圆环 DOM：首次有上下文数据时创建，之后复用同一份节点 */
function ctxRingEnsure() {
  if (_ctxRing) return _ctxRing;
  const dock = document.createElement('div');
  dock.id = 'ctx-ring-dock';
  dock.className = 'ctx-ring-dock hidden';
  const wrap = document.createElement('div');
  wrap.className = 'ctx-ring-wrap';
  const ring = document.createElement('button');
  ring.id = 'btn-ctx-ring';
  ring.type = 'button';
  ring.className = 'ctx-ring';
  ring.title = '压缩历史对话';
  ring.setAttribute('aria-label', '压缩历史对话');
  ring.innerHTML =
    '<svg viewBox="0 0 24 24" aria-hidden="true">' +
      '<circle class="ctx-ring-track" cx="12" cy="12" r="' + CTX_RING_R + '"/>' +
      '<circle class="ctx-ring-arc" cx="12" cy="12" r="' + CTX_RING_R + '"/>' +
    '</svg>';
  const pct = document.createElement('button');
  pct.id = 'btn-ctx-detail';
  pct.type = 'button';
  pct.className = 'ctx-ring-pct';
  pct.title = '查看上下文占用明细 / 恢复原文';
  pct.setAttribute('aria-label', '查看上下文占用明细');
  wrap.appendChild(ring);
  wrap.appendChild(pct);
  dock.appendChild(wrap);
  ring.onclick = () => compressContextManually();
  pct.onclick = () => openCtxDetail();
  const arc = ring.querySelector('.ctx-ring-arc');
  arc.style.strokeDasharray = CTX_RING_LEN;
  arc.style.strokeDashoffset = CTX_RING_LEN;
  _ctxRing = { hasCtx: false, dock, arc, pct };
  return _ctxRing;
}

/* 圆环只挂在最后一条 AI 回复的操作条里（与复制 / 选择文本同一行，靠右）；其余情况收起 */
function placeCtxRing() {
  const r = _ctxRing;
  if (!r || !r.hasCtx) return;
  const chat = $('#chat');
  const rows = chat ? chat.querySelectorAll('.msg') : [];
  const last = rows[rows.length - 1];
  if (!chat || state.uiMode === 'gal' || !last || !last.classList.contains('ai')) {
    r.dock.classList.add('hidden');
    return;
  }
  const bar = last.querySelector('.msg-actions');
  if (!bar) { r.dock.classList.add('hidden'); return; }
  if (r.dock.parentNode !== bar) bar.appendChild(r.dock);
  r.dock.classList.remove('hidden');
}

function renderContextBar(ctx) {
  syncCompressMarks(ctx);      // 压缩标记跟着 context 一起同步（GAL 内部会跳过）
  const r = ctxRingEnsure();
  r.hasCtx = !!(ctx && ctx.limit);
  if (!r.hasCtx) { r.dock.classList.add('hidden'); return; }
  const pct = Math.max(0, Math.min(100, ctx.pct || 0));
  const remainPct = ctx.remain / ctx.limit;
  let level = '';
  if (remainPct < 0.05) level = 'danger';
  else if (remainPct < 0.10) level = 'warn';
  r.dock.classList.toggle('warn', level === 'warn');
  r.dock.classList.toggle('danger', level === 'danger');
  r.arc.style.strokeDashoffset = CTX_RING_LEN * (1 - pct / 100);
  r.pct.textContent = Math.round(pct) + '%';
  r.pct.setAttribute('aria-label', '上下文占用 ' + pct + '%，点击查看明细');
  placeCtxRing();
  remindContext(ctx, remainPct);
}

function remindContext(ctx, remainPct) {
  const sid = state.sessions.current;
  if (ctxRemind.session !== sid) { ctxRemind.session = sid; ctxRemind.fired = {}; }
  if (remainPct >= 0.10) { ctxRemind.fired = {}; return; }   // 回落后重置，允许再次提醒
  if (remainPct < 0.05) {
    if (!ctxRemind.fired['5']) {
      ctxRemind.fired['5'] = true; ctxRemind.fired['10'] = true;
      toast('上下文剩余不足 5%，超出上限时会临时省略较早消息');
    }
  } else if (!ctxRemind.fired['10']) {
    ctxRemind.fired['10'] = true;
    toast('上下文剩余不足 10%，可点回复末尾的圆环压缩历史，或稍后新建会话');
  }
}

async function compressContextManually() {
  if (state.streaming) { toast('正在生成回复，请稍后再试'); return; }
  if (!confirm('把较早的对话历史压缩成摘要？\n摘要会替换那部分原文（本地原文仍保留，可在「明细 / 恢复」里撤销）。')) return;
  const btn = $('#btn-ctx-ring');
  if (btn) { btn.disabled = true; btn.classList.add('busy'); }
  compressPending();
  try {
    const r = await api('/api/context/compress', { session_id: state.sessions.current });
    if (r && r.sessions) applySessionsPayload(r.sessions);
    renderSessions();
    if (r && r.context) renderContextBar(r.context);   // 成功时自动把 pending 升级成分隔线
    if (!r || !r.ok) {
      compressPendingClear();
      toast((r && r.message) || '未能压缩', 'error');
    } else if (!(r.context && Array.isArray(r.context.compress_marks))) {
      compressPendingClear();                          // 旧后端没有标记字段：别留下假进行中
    }
    if ($('#ctx-modal') && !$('#ctx-modal').classList.contains('hidden')) openCtxDetail();
  } catch (e) {
    compressPendingClear();
    toastErr('压缩', e);
  } finally {
    if (btn) { btn.disabled = false; btn.classList.remove('busy'); }
  }
}

/* ---------------- 上下文详情（占用明细 / 摘要 / 恢复） ---------------- */
async function openCtxDetail() {
  try {
    const r = await api('/api/context/summary', { session_id: state.sessions.current });
    if (r && r.context) renderContextBar(r.context);
    ctxRenderDetail(r);
    showWithAnim($('#ctx-modal'));
  } catch (e) {
    toastErr('读取压缩详情', e);
  }
}

function closeCtxDetail() {
  hideWithAnim($('#ctx-modal'));
}

function ctxRenderDetail(r) {
  if (!r || !r.ok) { toast((r && r.message) || '读取压缩详情失败', 'error'); return; }
  const c = r.context || {};
  $('#ctx-detail-total').textContent =
    '已用 ' + fmtNum(c.used) + ' / ' + fmtNum(c.limit) + ' · 剩余 ' +
    fmtNum(c.remain) + '（' + (c.pct || 0) + '%）';
  const parts = c.parts || {};
  const rows = [
    ['系统提示', parts.system],
    ['历史摘要', parts.summary],
    ['对话正文', parts.dialog],
    ['附件正文', parts.attachment],
    ['图片', parts.image],
  ];
  const total = Math.max(1, c.used || 0);
  $('#ctx-parts').innerHTML = rows.map(([name, v]) => {
    const n = Number(v || 0);
    const w = Math.min(100, Math.round(n * 100 / total));
    return '<div class="ctx-part"><span class="ctx-part-name">' + name + '</span>' +
      '<span class="ctx-part-bar"><i style="width:' + w + '%"></i></span>' +
      '<span class="ctx-part-val">' + fmtNum(n) + '</span></div>';
  }).join('');
  const meta = [];
  if (r.compressed) {
    meta.push('已压缩 ' + r.compressed + ' 条（剩余 ' + r.remaining + ' / ' + r.total + ' 条原文）');
    if (r.at) meta.push(new Date(r.at * 1000).toLocaleString());
    meta.push('摘要 ' + fmtNum(r.chars) + ' 字');
  } else {
    meta.push('尚未压缩');
  }
  if (r.busy) meta.push('后台压缩中…');
  $('#ctx-detail-meta').textContent = meta.join(' · ');
  const box = $('#ctx-summary-text');
  if (r.summary) {
    box.textContent = r.summary;
    box.classList.remove('hidden');
  } else {
    box.textContent = '';
    box.classList.add('hidden');
  }
  $('#ctx-detail-restore').disabled = !r.compressed;
  ctxRenderInput(r.input);
}

/* 输入明细：整回合累计 vs 逐轮调用。和「占用明细」同一个入口，不同页签。 */

/* 输入构成的分类与展示顺序（后端 context.input_breakdown 的 key） */
const CTX_IN_ORDER = ['rules', 'memory', 'other_sys', 'summary', 'user',
  'reply', 'tool_result', 'tools_doc', 'schema'];
const CTX_IN_LABELS = {
  rules: '规则/画像', memory: '记忆/任务', other_sys: '其他系统', summary: '摘要',
  user: '用户消息', reply: '回复历史', tool_result: '工具结果',
  tools_doc: '工具说明', schema: '工具定义'
};

function ctxInParts(r) {
  const p = r && r.in_parts;
  if (!p || typeof p !== 'object') return null;
  let total = 0;
  // 用全部 key 求和（不只已知分类）：后端以后加分类时，百分比也不会算错
  Object.keys(p).forEach((k) => { total += Number(p[k] || 0); });
  if (!(total > 0)) return null;
  return { p: p, total: total };
}

function ctxInBar(p, total, small) {
  const segs = CTX_IN_ORDER.filter((k) => Number(p[k] || 0) > 0).map((k) => {
    const pct = Number(p[k]) * 100 / total;
    return '<span class="ctx-in-seg ctx-in-' + k + '" style="width:'
      + pct.toFixed(2) + '%"></span>';
  }).join('');
  return '<span class="ctx-in-bar' + (small ? ' small' : '') + '">' + segs + '</span>';
}

function ctxInputMix(r) {
  const it = ctxInParts(r);
  if (!it) return '';
  const chips = CTX_IN_ORDER.filter((k) => Number(it.p[k] || 0) > 0).map((k) => {
    const v = Number(it.p[k]);
    const pct = v * 100 / it.total;
    return '<span class="ctx-in-chip"><i class="ctx-in-dot ctx-in-' + k + '"></i>'
      + CTX_IN_LABELS[k] + ' <b>' + fmtNum(v) + '</b>（' + pct.toFixed(1) + '%）</span>';
  }).join('');
  return '<p class="ctx-in-title">第 ' + (r.n || '?') + ' 次调用的输入构成（按 token 估算拆分）</p>'
    + ctxInBar(it.p, it.total, false)
    + '<div class="ctx-in-legend">' + chips + '</div>';
}

function ctxRenderInput(inp) {
  const totalEl = $('#ctx-input-total');
  const metaEl = $('#ctx-input-meta');
  const tableEl = $('#ctx-input-table');
  const mixEl = $('#ctx-input-mix');
  if (!totalEl || !metaEl || !tableEl) return;
  if (!inp || !inp.usage) {
    totalEl.textContent = '还没有带 token 用量的回复';
    metaEl.textContent = '';
    tableEl.innerHTML = '';
    if (mixEl) { mixEl.innerHTML = ''; mixEl.classList.add('hidden'); }
    return;
  }
  const u = inp.usage || {};
  const prompt = Number(u.prompt || 0);
  const cached = Number(u.cached || 0);
  const uncached = Math.max(0, prompt - cached);
  const pct = prompt > 0 ? (cached * 100 / prompt) : 0;
  totalEl.textContent = '累计输入 ' + fmtNum(prompt)
    + ' · 缓存 ' + fmtNum(cached) + '（' + pct.toFixed(1) + '%）'
    + ' · 未命中 ' + fmtNum(uncached)
    + ' · 输出 ' + fmtNum(u.completion)
    + ' · 合计 ' + fmtNum(u.total);
  const meta = [];
  if (inp.at) meta.push('回合 ' + clockText(inp.at));
  if (inp.rounds) meta.push('工具轮 ' + inp.rounds);
  const rd = inp.rounds_detail || [];
  if (rd.length) meta.push('模型调用 ' + rd.length + ' 次');
  if (inp.total_ms) meta.push('耗时 ' + (inp.total_ms / 1000).toFixed(1) + ' 秒');
  const lastPrompt = Number(u.last_prompt
    || (rd.length ? rd[rd.length - 1].prompt : 0) || 0);
  if (lastPrompt) meta.push('末轮上下文 ' + fmtNum(lastPrompt));
  if (inp.source === 'usage_log') meta.push('逐轮明细来自 token_usage.jsonl（旧消息）');
  metaEl.textContent = meta.join(' · ');
  if (!rd.length) {
    tableEl.innerHTML = '<p class="hint">这一轮没有保存逐轮明细（旧数据或未启用）。</p>';
    if (mixEl) { mixEl.innerHTML = ''; mixEl.classList.add('hidden'); }
    return;
  }
  // 构成面板取最近一次「有构成数据」的调用：旧回合没记 in_parts 就自动跳过
  const lastMix = rd.slice().reverse().find((r) => ctxInParts(r));
  if (mixEl) {
    mixEl.innerHTML = lastMix ? ctxInputMix(lastMix)
      : '<p class="hint">这一轮的明细没有保存输入构成（旧数据）；下一次生成后即可看到分类。</p>';
    mixEl.classList.remove('hidden');
  }
  const rows = rd.map((r, i) => {
    const last = i === rd.length - 1;
    const state = escapeHtml(r.state || r.finish || '');
    const ttft = (r.ttft_ms === null || r.ttft_ms === undefined)
      ? '' : (r.ttft_ms + ' ms');
    const it = ctxInParts(r);
    let mixCell = '<span class="hint">—</span>';
    if (it) {
      const tip = CTX_IN_ORDER.filter((k) => Number(it.p[k] || 0) > 0)
        .map((k) => CTX_IN_LABELS[k] + ' ' + fmtNum(it.p[k])).join(' · ');
      mixCell = '<span title="' + escapeHtml(tip) + '">'
        + ctxInBar(it.p, it.total, true) + '</span>';
    }
    return '<tr' + (last ? ' class="ctx-last"' : '') + '><td>' + (i + 1)
      + '</td><td>' + (r.n || '') + '</td><td>' + fmtNum(r.prompt)
      + '</td><td>' + fmtNum(r.completion) + '</td><td>' + state
      + '</td><td>' + ttft + '</td><td class="ctx-in-cell">' + mixCell
      + '</td></tr>';
  }).join('');
  tableEl.innerHTML = '<table><thead><tr><th>#</th><th>轮次</th><th>输入</th>'
    + '<th>输出</th><th>状态</th><th>首包</th><th>构成</th></tr></thead><tbody>'
    + rows + '</tbody></table>';
}

async function ctxRestore() {
  if (!confirm('恢复原文？摘要会被清空，较早的对话重新计入上下文，可能立刻再次触发压缩。')) return;
  try {
    const r = await api('/api/context/restore', { session_id: state.sessions.current });
    if (r && r.sessions) applySessionsPayload(r.sessions);
    renderSessions();
    compressClearAll();
    renderMessages();
    if (r && r.context) renderContextBar(r.context);
    toast((r && r.message) || '已恢复原文');
    await openCtxDetail();
  } catch (e) {
    toastErr('恢复', e);
  }
}

let _selectSeq = 0;   // 连点会话时只认最后一次响应，避免旧响应把界面刷回去

async function selectSession(id) {
  // 普通对话：切会话**不停止**正在生成的流（后台继续跑）；GAL 舞台只有一份，维持原行为
  if (state.uiMode === 'gal' && state.streaming) await stopStreaming();
  // 换会话时取消「修改历史消息」状态，避免下次回来误把它当成正在编辑
  if (typeof cancelEditTarget === 'function') cancelEditTarget(false);
  const prev = state.sessions.current;
  cacheLooseMessages();                   // 切走前把已加载的消息留在内存里，切回来能秒出
  const seq = ++_selectSeq;
  state.sessions.current = id;
  delete state.unread[id];
  renderSessions();                       // 先高亮，给出即时反馈（不等网络往返）
  if (typeof renderSendQueue === 'function') renderSendQueue();   // 队列按会话切换显示
  // 回访过的会话：先用缓存渲染，网络返回后再校准，避免每次都干等请求
  const cached = state.msgCache && state.msgCache[id];
  const target = (state.sessions.sessions || []).find((x) => x.id === id);
  if (cached && target && Array.isArray(cached.messages)) {
    target.messages = cached.messages;
    target.msg_loaded_start = Number(cached.msg_loaded_start) || 0;
    target.msg_total = cached.messages.length;
    renderMessages();
    if (typeof refreshStreamingUI === 'function') refreshStreamingUI();
  }
  try {
    // 服务端返回的是精简数据：新选中的会话只带最近 80 条（msg_total / msg_start）
    const r = await api('/api/session/select', { id });
    if (seq !== _selectSeq) return;       // 更晚的一次点击已经接管
    if (r && r.sessions) applySessionsPayload(r.sessions);
    renderSessions();
    renderMessages();
    // 这个会话可能还在后台生成：把它的 live DOM 重新挂到末尾
    if (typeof attachStreamHost === 'function') attachStreamHost(id);
    if (typeof refreshStreamingUI === 'function') refreshStreamingUI();
    resetIdeForSession();
    if (state.uiMode === 'gal' && typeof galRenderFromSession === 'function') galRenderFromSession();
    if (r) renderContextBar(r.context);
  } catch (e) {
    if (seq !== _selectSeq) return;       // 已被后续点击取代：不要还原成旧会话
    state.sessions.current = prev;        // 失败 → 还原高亮，别让界面和实际内容对不上
    renderSessions();
    if (typeof renderSendQueue === 'function') renderSendQueue();
    toastErr('切换会话', e);
  }
}

async function deleteSession(id) {
  const s = state.sessions.sessions.find((x) => x.id === id);
  const label = (s && s.title) || '这个会话';
  if (state.streams && state.streams[id]) {
    // 生成中的会话不能删：后端收尾时会把这条会话重新写回文件（表现为删了又出现）
    toast('这个会话正在生成，先点「停止」再删除');
    return;
  }
  if (!confirm('删除会话「' + label + '」？\n它的全部对话与存档都会一并删除，且不可恢复。')) return;
  try {
    const r = await api('/api/session/delete', { id });
    applySessionsPayload(r.sessions);
    if (typeof dropQueuedForSession === 'function') dropQueuedForSession(id);
    renderSessions();
    renderMessages();
    if (typeof renderSendQueue === 'function') renderSendQueue();
    resetIdeForSession();
    if (r) renderContextBar(r.context);
    toast('已删除会话：' + label);
  } catch (e) { toastErr('删除', e); }
}

async function newSession() {
  // 新建会话不打断其它会话的生成：多线程工作就是靠这个
  const inGal = state.uiMode === 'gal';
  const mem = (state.config && state.config.gal_memory) || {};
  try {
    const r = await api('/api/session/new', inGal ? {
      gal_scope: 'gal', title: 'GAL 剧场',
      gal_use_profile: !!mem.profile,
      gal_use_kb: !!mem.kb,
      gal_use_rules: !!mem.rules,
    } : {});
    applySessionsPayload(r.sessions);
    renderSessions();
    renderMessages();
    if (typeof renderSendQueue === 'function') renderSendQueue();   // 新会话队列为空
    resetIdeForSession();
    if (inGal && typeof galRenderFromSession === 'function') galRenderFromSession();
    if (r) renderContextBar(r.context);
    $('#input').focus();
  } catch (e) { toastErr('新建会话', e); }
}

async function renameSession(id) {
  const s = state.sessions.sessions.find((x) => x.id === id);
  // Electron 不支持 window.prompt，统一走应用内输入框
  const name = await appPrompt('重命名会话', (s && s.title) || '');
  if (!name || !name.trim()) return;
  try {
    const r = await api('/api/session/rename', { id, title: name.trim() });
    if (!r || !r.ok) throw new Error((r && r.error) || '重命名失败');
    applySessionsPayload(r.sessions);
    renderSessions();
  } catch (e) { toastErr('重命名', e); }
}

async function toggleSessionPin(id) {
  const s = state.sessions.sessions.find((x) => x.id === id);
  if (!s) return;
  const next = !s.pinned;
  try {
    const r = await api('/api/session/pin', { id, pinned: next });
    if (!r || !r.ok) throw new Error((r && r.error) || '置顶失败');
    applySessionsPayload(r.sessions);
    renderSessions();
    toast(next ? '已置顶' : '已取消置顶');
  } catch (e) { toastErr('置顶', e); }
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

// 距离底部多少像素算「贴着底」。跟随用严格阈值，按钮提示用宽松阈值。
function isAtBottom(px) {
  const c = $('#chat');
  if (!c) return true;
  return c.scrollHeight - c.scrollTop - c.clientHeight < (px == null ? 24 : px);
}

// 「回到底部」按钮的显隐阈值：滚得比较远才提示
function nearBottom() {
  return isAtBottom(120);
}

// 是否处于「跟随最新输出」状态：用户主动往上翻就锁定，直到回到底部 / 点回到底部 / 发新消息
let _followBottom = true;

function setFollowBottom(on) {
  _followBottom = !!on;
}

// 「还算贴着底」的容差：跟随判定专用。太小的话，手抖滚一格或内容长两行就会被判成「用户离开了」。
const FOLLOW_SLOP = 40;

// 程序滚动计数：给 scrollTop 赋值也会触发 scroll 事件，而事件是**异步派发**的。
// 不区分「自己滚的」和「用户滚的」，程序刚滚到底、事件回来时内容又长高了，
// 就会把跟随状态关掉（流式越快越容易中）。
let _programmaticScroll = 0;

function _scrollToBottomNow(box) {
  if (!box) return;
  _programmaticScroll += 1;
  box.scrollTop = box.scrollHeight;
  requestAnimationFrame(() => {
    _programmaticScroll = Math.max(0, _programmaticScroll - 1);
  });
}

// 流式期间必须「即时定位」：每帧都调用，若用平滑滚动会互相打断而产生抖动
let _scrollRaf = 0;
function scrollToBottom(force) {
  const c = $('#chat');
  if (!c) return;
  if (force) {
    if (_scrollRaf) { cancelAnimationFrame(_scrollRaf); _scrollRaf = 0; }
    _followBottom = true;
    _scrollToBottomNow(c);
    return;
  }
  // 用户在翻看历史：流式增量一帧都不许把窗口拽回去
  if (!_followBottom) return;
  // 每个 delta 都会调到这里；合并到下一帧，避免每个 token 都强制一次 layout
  if (_scrollRaf) return;
  _scrollRaf = requestAnimationFrame(() => {
    _scrollRaf = 0;
    const box = $('#chat');
    if (!box || !_followBottom) return;
    // 跟随只表示「用户没往上翻」，不等于「当前位置还贴着底」。
    // 以前这里再判一次 isAtBottom，于是「这一帧内容长高了」被误判成
    // 「用户离开了底部」—— 一次 delta 超过阈值就永久断跟随。
    _scrollToBottomNow(box);
  });
}

// 内容高度突变（图片加载 / 代码高亮 / 工具卡展开 / 字体）不会产生 delta，
// 光靠 scrollToBottom 会停在半路。跟随状态下补一次贴底。
let _chatGrowthObserver = null;
function watchChatGrowth() {
  const box = $('#chat');
  if (!box || _chatGrowthObserver || !window.MutationObserver) return;
  const pinImages = (node) => {
    if (!node || node.nodeType !== 1) return;
    const imgs = node.tagName === 'IMG' ? [node] : Array.from(node.querySelectorAll('img'));
    imgs.forEach((img) => {
      if (img.complete) return;
      img.addEventListener('load', () => {
        if (_followBottom) scrollToBottom();
      }, { once: true });
    });
  };
  const obs = new MutationObserver((records) => {
    if (!_followBottom) return;
    for (const r of records) {
      (r.addedNodes || []).forEach(pinImages);
    }
    scrollToBottom();
  });
  obs.observe(box, { childList: true, subtree: true, characterData: true });
  _chatGrowthObserver = obs;
}

// 「回到底部」按钮的定位：按输入区实际高度动态设置 bottom，
// 避免输入区变高（进度条/附件条/提示换行）时按钮压到输入框上
function positionToBottomBtn() {
  const btn = $('#btn-bottom');
  const composer = document.querySelector('.composer');
  if (!btn || !composer) return;
  btn.style.bottom = (composer.getBoundingClientRect().height + 14) + 'px';
}

// 只有用户主动点「回到底部」时才用平滑滚动
let _smoothScrollTimer = null;
// 平滑滚动会持续几百毫秒，期间每个 scroll 事件都停在「中途」，
// 不豁免的话会被判成「用户离开了底部」，刚点完按钮跟随又关了。
let _smoothScrollUntil = 0;
function scrollToBottomSmooth() {
  const c = $('#chat');
  _followBottom = true;
  c.classList.add('smooth');
  _smoothScrollUntil = Date.now() + 500;
  _scrollToBottomNow(c);
  clearTimeout(_smoothScrollTimer);
  _smoothScrollTimer = setTimeout(() => c.classList.remove('smooth'), 450);
}

function buildActions(row, getText, index, role) {
  const bar = document.createElement('div');
  bar.className = 'msg-actions';
  // 按钮组单独一层：只有它在悬停时淡入，圆环（上下文占用）保持常显、靠右
  const list = document.createElement('div');
  list.className = 'msg-act-list';
  // 图标按钮：文字放到 data-tip，悬停由 CSS 气泡显示（无原生 title 的延迟）
  const mk = (ico, label, fn) => {
    const b = document.createElement('button');
    b.className = 'msg-act tip';
    b.dataset.tip = label;
    b.setAttribute('aria-label', label);
    b.innerHTML = icon(ico);
    b.onclick = fn;
    list.appendChild(b);
    return b;
  };
  mk('copy', '复制', () => { navigator.clipboard.writeText(getText()).then(() => toast('已复制', 'success')); });
  if (role === 'user') {
    mk('history', '撤回', () => withdrawUserMessage(index));
    mk('pencil', '修改', () => editUserMessage(index));
    mk('trash', '删除', () => deleteMessage(index)).classList.add('act-danger');
  } else {
    row._selBtn = mk('select', '选择文本', () => toggleRawView(row));
    // mousedown 阻止默认：点按钮会先清空选区，那样就拿不到用户划选的文字了
    mk('chat', '添加到对话', () => addSelectionToChat(row))
      .addEventListener('mousedown', (e) => e.preventDefault());
    if (typeof index === 'number') mk('branch', '创建分支', () => forkFromMessage(index));
    if (typeof index === 'number') mk('refresh', '重新发言', () => regenerateReply(index));
    // 「继续输出」：仅当该条是最后一条、被中断过、且非 GAL 模式时提供
    const cs = curSession();
    const cms = (cs && typeof index === 'number') ? (cs.messages || [])[index] : null;
    const isLastMsg = cs && typeof index === 'number' &&
                      index === (cs.messages || []).length - 1;
    const _remoteRunning = !!(cs && state.streamingSids && state.streamingSids[String(cs.id)]);
    // 刷新 / 重开窗口时，最后一条可能只是「任务仍在运行」的进度快照。
    // 这时显示「后台运行中」而不是「继续输出」，避免看起来像输出停了。
    if (isLastMsg && cms && cms._progress && _remoteRunning) {
      const chip = document.createElement('span');
      chip.className = 'msg-run-chip';
      chip.textContent = '后台运行中…';
      chip.title = '任务仍在后台运行，完成后会自动刷新';
      bar.appendChild(chip);
    }
    // 步骤时间线：步级撤销 / 重做（整轮回滚之外更细的一层）
    if (isLastMsg && state.uiMode !== 'gal') mk('list', '步骤', () => openStepsPanel());
    // 「回滚本轮」：只在该条是本轮最后一条、且本轮确实写过文件时出现
    if (isLastMsg && row._wroteTurn) mk('reset', '回滚本轮改动', () => turnUndo());
    // 有 end_reason 时「继续输出」已经在中断提示条里，操作条不再重复放一个
    if (isLastMsg && cms && cms.interrupted && !cms.end_reason
        && !_remoteRunning && state.uiMode !== 'gal') {
      mk('play', '继续输出', () => continueReply(index));
    }
    mk('trash', '删除', () => deleteMessage(index)).classList.add('act-danger');
  }
  bar.appendChild(list);
  return bar;
}

/* 写操作步骤：步级撤销 / 重做。数据来自 agent.list_steps（跨进程持久化）。 */
async function openStepsPanel() {
  const sid = state.sessions.current;
  if (!sid) return;
  const old = document.querySelector('.steps-panel');
  if (old) old.remove();
  let steps = [];
  try {
    const res = await fetch('/api/agent/steps', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Auth-Token': TOKEN },
      body: JSON.stringify({ session_id: sid }),
    });
    const data = await res.json();
    steps = (data && data.steps) || [];
  } catch (e) {
    toast('读取步骤失败：' + e.message, 'error');
    return;
  }

  const panel = document.createElement('div');
  panel.className = 'steps-panel';
  const head = document.createElement('div');
  head.className = 'steps-head';
  head.innerHTML = '<span>写操作步骤（' + steps.length + '）</span>';
  const close = document.createElement('button');
  close.type = 'button';
  close.className = 'steps-close';
  close.textContent = '×';
  close.onclick = () => panel.remove();
  head.appendChild(close);
  panel.appendChild(head);

  const body = document.createElement('div');
  body.className = 'steps-body';
  if (!steps.length) {
    body.innerHTML = '<div class="steps-empty">本次会话还没有可撤销的写操作。</div>';
  }
  steps.forEach((s, i) => {
    const row = document.createElement('div');
    row.className = 'steps-row' + (s.undone ? ' undone' : '');
    const info = document.createElement('div');
    info.className = 'steps-info';
    const short = (s.files || []).map((p) => String(p).split(/[\\/]/).slice(-2).join('/'));
    info.innerHTML = '<b>' + (i + 1) + '.</b> ' + escapeHtml(s.label || '写操作')
      + '<div class="steps-files">'
      + escapeHtml(short.slice(0, 4).join('、') || '（无文件）')
      + (short.length > 4 ? ' …' : '') + '</div>';
    row.appendChild(info);
    const act = document.createElement('button');
    act.type = 'button';
    act.className = 'steps-act';
    act.textContent = s.undone ? '重做' : '撤销';
    act.onclick = async () => {
      act.disabled = true;
      try {
        const r = await fetch(s.undone ? '/api/agent/redo' : '/api/agent/undo', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-Auth-Token': TOKEN },
          body: JSON.stringify({ token: s.token }),
        });
        const d = await r.json();
        if (!d || !d.ok) toast('操作失败', 'error');
        else toast(s.undone ? '已重做' : '已撤销', 'success');
      } catch (e) {
        toast('请求失败：' + e.message, 'error');
      }
      openStepsPanel();
    };
    row.appendChild(act);
    body.appendChild(row);
  });
  panel.appendChild(body);
  document.body.appendChild(panel);
}

/* 「添加到对话」：优先取该条消息里划选的文字，没有选区就用整条回复 */
function addSelectionToChat(row) {
  const box = row._content;
  let text = '';
  const sel = window.getSelection();
  if (sel && sel.rangeCount && !sel.isCollapsed && box) {
    const rng = sel.getRangeAt(0);
    // 选区必须落在这一条消息的内容里，避免把别处的选中误引进来
    if (box.contains(rng.commonAncestorContainer)) text = String(sel.toString());
  }
  if (!text.trim()) text = String(row._raw || (box && box.innerText) || '');
  if (!addQuote(text)) { toast('这条回复没有可引用的文字', 'error'); return; }
  toast('已添加到对话');
}

// 记忆写入标记：AI 本轮改了哪些记忆 + 可撤销
function buildMemoryChip(mem, index) {
  const box = document.createElement('div');
  box.className = 'mem-chip' + (mem.undone ? ' undone' : '');
  const problems = mem.problems || [];
  const warnOnly = !mem.applied && problems.length;   // 一条都没生效：只提示，不给撤销
  if (warnOnly) box.classList.add('warn');
  const ico = document.createElement('span');
  ico.className = 'mem-icon';
  ico.innerHTML = warnOnly ? icon('alert') : icon('brain');
  const text = document.createElement('span');
  text.className = 'mem-text';
  if (warnOnly) {
    text.textContent = '记忆未生效：' + problems.join('、');
  } else {
    const scopes = (mem.scopes || []).join('、');
    text.textContent = '已更新记忆：' + (mem.summary || '') + (scopes ? '（' + scopes + '）' : '') +
      (problems.length ? '；未生效：' + problems.join('、') : '');
  }
  box.appendChild(ico);
  box.appendChild(text);
  if (mem.undone) {
    const tag = document.createElement('span');
    tag.className = 'mem-undone';
    tag.textContent = '已撤销';
    box.appendChild(tag);
    return box;
  }
  // 本轮有节点/连线变更 → 直接跳到图谱看新增/修改内容（不要求用户自己找）
  const changed = mem.changed || {};
  const hasChanges = Object.keys(changed).some((k) => {
    const c = changed[k] || {};
    return (c.node_add || []).length || (c.node_update || []).length
      || (c.edge_add || []).length;
  });
  if (hasChanges && typeof kbOpenChanges === 'function') {
    const jump = document.createElement('button');
    jump.className = 'mem-jump';
    jump.textContent = '查看更新';
    jump.title = '打开知识图谱，高亮本轮新增 / 修改的节点与连线';
    jump.onclick = () => kbOpenChanges(changed);
    box.appendChild(jump);
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
    if (r.sessions) applySessionsPayload(r.sessions);
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

  // 图片附件：气泡上方展示缩略图（点击放大 / 还原；多张并排）
  const ims = (msg && (msg.images || (msg.image ? [msg.image] : []))) || [];
  if (ims.length) {
    const gallery = document.createElement('div');
    gallery.className = 'msg-images';
    ims.forEach((im) => {
      if (!im || (!im.name && !im.url)) return;
      const box = document.createElement('div');
      box.className = 'msg-image';
      box.title = im.size
        ? ('图片 · ' + Math.max(1, Math.round(im.size / 1024)) + ' KB（点击放大 / 还原）')
        : '点击放大 / 还原';
      const el = document.createElement('img');
      el.alt = '图片';
      box.appendChild(el);
      box.onclick = () => box.classList.toggle('zoom');
      if (im.url) {
        el.src = im.url;                              // 刚发出：直接用本地预览图
      } else if (im.name) {
        attachBlobUrl(state.sessions.current, im.name)
          .then((u) => { el.src = u; })
          .catch(() => { box.remove(); });
      }
      gallery.appendChild(box);
    });
    if (gallery.children.length) wrap.appendChild(gallery);
  }

  // 文档附件：气泡上方显示一个文件名标签（解析结果稍后由 SSE 补上字数 / 失败原因）
  const dc = (msg && msg.doc) || null;
  if (dc && dc.name) {
    const chip = document.createElement('div');
    chip.className = 'msg-doc' + (dc.error ? ' err' : (dc.warn ? ' warn' : ''));
    chip.style.marginTop = '0';
    chip.innerHTML = icon('attach') + ' ' + escapeHtml(dc.name) + (dc.error ? '（解析失败）' : docChipSuffix(dc));
    if (dc.error) chip.title = String(dc.error);
    else if (dc.warn) chip.title = String(dc.warn);
    wrap.appendChild(chip);
  }

  const b = document.createElement('div');
  b.className = 'bubble';
  b.textContent = text;
  const imgOnlyLabel = /^【(?:\d+ 张)?图片】$/.test(text || '');
  if (!(ims.length && imgOnlyLabel)) wrap.appendChild(b);   // 纯图片消息不再重复「【图片】」占位

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
  makeClickable(head, () => box.classList.toggle('open'), '展开 / 收起思考过程');
  box.appendChild(head);
  box.appendChild(body);
  box._body = body;
  box._label = label;
  return box;
}

// 思考收起时的一行摘要：流式中取「最后一个已完成段落」的首行，结束后取全文首行。
// 去掉 ** 标记，避免摘要里出现 Markdown 符号。
function thinkLineSummary(text, settled) {
  const s = String(text || '');
  if (!s.trim()) return '';
  if (settled) return (s.split('\n')[0] || '').replace(/\*\*/g, '').trim();
  const parts = s.split(/\r?\n[\t ]*\r?\n/).filter((p) => p.trim());
  if (!parts.length) return '';
  const pick = parts.length > 1 ? parts[parts.length - 2] : parts[parts.length - 1];
  return (pick.split('\n')[0] || '').replace(/\*\*/g, '').trim();
}

function workDuration(ms) {
  const sec = Math.max(1, Math.round((Number(ms) || 0) / 1000));
  if (sec < 60) return sec + ' 秒';
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return m + ' 分' + (s ? ' ' + s + ' 秒' : '');
}

function processFoldLabel(timing, endKind) {
  const kind = String(endKind || '');
  if (kind === 'stopped') return '过程 · 已停止';
  if (kind === 'billing') return '过程 · 余额不足';
  if (kind === 'network' || kind === 'server' || kind === 'error') return '过程 · 中断';
  const ms = timing && timing.total_ms;
  return ms ? '过程 · 工作 ' + workDuration(ms) : '过程';
}

function clipText(s, n) {
  const t = String(s || '').trim();
  return t.length > n ? t.slice(0, Math.max(1, n - 1)) + '…' : t;
}

// 过程折叠：思考 + 工具在上，最终答案在下；默认收起。
function buildProcessFold(flow, timing, endKind, open) {
  const wrap = document.createElement('div');
  wrap.className = 'process-fold' + (open ? '' : ' collapsed');
  const head = document.createElement('div');
  head.className = 'process-fold-head';
  const title = document.createElement('span');
  title.className = 'process-fold-title';
  title.textContent = processFoldLabel(timing, endKind);
  const caret = document.createElement('span');
  caret.className = 'process-fold-caret';
  caret.textContent = open ? '▾' : '▸';
  head.appendChild(title);
  head.appendChild(caret);
  const body = document.createElement('div');
  body.className = 'process-fold-body';
  body.appendChild(flow);
  wrap.appendChild(head);
  wrap.appendChild(body);
  const toggle = () => {
    const expanded = wrap.classList.contains('collapsed');
    wrap.classList.toggle('collapsed', !expanded);
    caret.textContent = expanded ? '▾' : '▸';
    head.setAttribute('aria-expanded', expanded ? 'true' : 'false');
  };
  head.onclick = toggle;
  makeClickable(head, toggle, '展开 / 收起过程');
  head.setAttribute('aria-expanded', open ? 'true' : 'false');
  return wrap;
}

// 来源条目（可复用于「参考来源」块和「联网搜索」过程块）
function sourceItemRows(items) {
  const frag = document.createDocumentFragment();
  (items || []).forEach((it, i) => {
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
    frag.appendChild(row);
  });
  return frag;
}

function buildSourcesBlock(items, open) {
  const box = buildThinkBlock('', !!open);
  box.classList.add('sources');
  box._label.textContent = '参考来源 · ' + items.length;
  box._body.textContent = '';
  box._body.appendChild(sourceItemRows(items));
  return box;
}

/* 联网搜索过程块：上面是步骤列表（边搜边追加），完成后下面接来源列表 */
function buildSearchBox() {
  const box = buildThinkBlock('', true);
  box.classList.add('sources');
  box._label.innerHTML = icon('search') + ' 联网搜索';
  box._body.textContent = '';
  const steps = document.createElement('div');
  steps.className = 'search-steps';
  box._body.appendChild(steps);
  box._steps = steps;
  box._fetchLine = null;
  box._planLine = null;
  return box;
}

function searchStep(box, text, cls) {
  const d = document.createElement('div');
  d.className = 'search-step' + (cls ? ' ' + cls : '');
  d.textContent = text;
  box._steps.appendChild(d);
  scrollToBottom();
  return d;
}

// 把来源列表接到搜索块下面，并把标题改成最终态
function appendSearchSources(box, items) {
  const srcWrap = document.createElement('div');
  srcWrap.className = 'search-src';
  const div = document.createElement('div');
  div.className = 'search-step';
  div.textContent = '─── 参考来源 ───';
  srcWrap.appendChild(div);
  srcWrap.appendChild(sourceItemRows(items));
  box._body.appendChild(srcWrap);
  // 一轮里可能搜多次：把「步骤」容器挪到来源块之后，第二次及以后的步骤才会排在时间线上
  // （否则新步骤会插到第一组来源的上方，看起来像顺序错乱）
  if (box._steps) box._body.appendChild(box._steps);
  box._label.innerHTML = icon('search') + ' 联网搜索 · ' + (items || []).length + ' 条来源';
  return box;
}

// 缓存命中率：命中的 token 本来就是输入的一部分，所以分母用 prompt。
// 没有命中 / 输入为 0 时返回空串，调用处直接拼在后面。
function cacheHitPct(cached, prompt) {
  const c = Number(cached) || 0;
  const p = Number(prompt) || 0;
  if (c <= 0 || p <= 0) return '';
  const pct = Math.round(Math.max(0, Math.min(100, (c / p) * 100)) * 10) / 10;
  return '（' + pct + '%）';
}

// 输出完成的系统时间（本地时区）：排查「这轮到底什么时候结束的」比浏览器相对时间可靠。
// 带日期：跨天回看历史时，只显示时分秒分不清是哪一天。
function clockText(ts) {
  const n = Number(ts) || 0;
  if (n <= 0) return '';
  const d = new Date(n * 1000);
  if (!Number.isFinite(d.getTime())) return '';
  const p = (v) => String(v).padStart(2, '0');
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) +
    ' ' + p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
}

function usageText(u, doneAt) {
  if (!u) return '';
  const f = (n) => (n || 0).toLocaleString('en-US');
  let s = (u.model ? u.model + ' · ' : '') +
    '输入 ' + f(u.prompt) + ' · 输出 ' + f(u.completion) + ' · 合计 ' + f(u.total) + ' tokens';
  if (u.cached) s += ' · 缓存命中 ' + f(u.cached) + cacheHitPct(u.cached, u.prompt);
  const t = clockText(doneAt);
  if (t) s += '\n完成时间 ' + t;      // 单独一行，接在 token 那一行下面
  return s;
}

/* ---------- Agent：工具卡片 / 提问卡片 ---------- */
const TOOL_LABELS = {
  list_dir: ['folder', '列目录'], read_file: ['note', '读文件'],
  read_files: ['note', '批量读取'], search_files: ['search', '搜索文件'],
  search_code: ['search', '语义检索'],
  repo_map: ['list', '仓库结构'],
  write_file: ['pencil', '写文件'], edit_file: ['pencil', '修改文件'], make_dir: ['folder-plus', '新建目录'],
  move_file: ['box', '移动'], delete_file: ['trash', '删除'], ask_user: ['question', '提问'],
  run_command: ['terminal', '运行命令'],
  render_check: ['image', '渲染自检'],
  computer_open: ['globe', '打开网页'],
  computer_see: ['search', '查看页面'],
  computer_act: ['pencil', '页面操作'],
  computer_read: ['list', '读取页面'],
  computer_shot: ['image', '页面截图'],
  computer_windows: ['list', '桌面窗口'],
  set_reminder: ['history', '定时提醒'],
  lsp_diagnostics: ['check-square', '类型检查'], lsp_definition: ['branch', '跳转定义'],
  lsp_references: ['search', '查找引用'], lsp_rename: ['pencil', '语义重命名'],
  git_status: ['branch', 'Git 状态'], git_diff: ['history', 'Git 改动'],
  git_log: ['history', '提交历史'], git_commit: ['check', 'Git 提交'],
};

// 步骤组摘要：按「动作 + 数量」聚合，比「读文件×3」更像人话（对齐 Trae 的 trace 文案）
const TOOL_SUMMARY = {
  list_dir: (n) => '查看 ' + n + ' 个目录',
  read_file: (n) => '读取 ' + n + ' 个文件',
  read_files: (n) => '批量读取 ' + n + ' 次',
  search_files: (n) => '搜索 ' + n + ' 次',
  search_code: (n) => '语义检索 ' + n + ' 次',
  repo_map: (n) => '查看结构 ' + n + ' 次',
  write_file: (n) => '写入 ' + n + ' 个文件',
  edit_file: (n) => '修改 ' + n + ' 个文件',
  make_dir: (n) => '新建 ' + n + ' 个目录',
  move_file: (n) => '移动 ' + n + ' 个文件',
  delete_file: (n) => '删除 ' + n + ' 个文件',
  run_command: (n) => '执行 ' + n + ' 条命令',
  render_check: (n) => '渲染自检 ' + n + ' 次',
  computer_open: (n) => '打开 ' + n + ' 个页面',
  computer_see: (n) => '查看页面 ' + n + ' 次',
  computer_act: (n) => '页面操作 ' + n + ' 次',
  computer_read: (n) => '读取页面 ' + n + ' 次',
  computer_shot: (n) => '页面截图 ' + n + ' 次',
  computer_windows: (n) => '列出窗口 ' + n + ' 次',
  set_reminder: (n) => '设置 ' + n + ' 个提醒',
  lsp_diagnostics: (n) => '类型检查 ' + n + ' 次',
  lsp_definition: (n) => '跳转定义 ' + n + ' 次',
  lsp_references: (n) => '查找引用 ' + n + ' 次',
  lsp_rename: (n) => '语义重命名 ' + n + ' 次',
  git_status: (n) => '查看 Git 状态 ' + n + ' 次',
  git_diff: (n) => '查看改动 ' + n + ' 个文件',
  git_log: (n) => '查看提交历史 ' + n + ' 次',
  git_commit: (n) => 'Git 提交 ' + n + ' 次',
};
// 运行中「正在…」用的动词（要短，后面直接跟参数）
const TOOL_VERBS = {
  list_dir: '查看目录', read_file: '读取', search_files: '搜索', write_file: '写入',
  edit_file: '修改', make_dir: '新建目录', move_file: '移动', delete_file: '删除',
  set_reminder: '设置提醒', search_code: '检索', repo_map: '查看结构', run_command: '执行',
  render_check: '渲染自检',
  computer_open: '打开网页', computer_see: '查看页面', computer_act: '操作页面',
  computer_read: '读取页面', computer_shot: '页面截图',
  computer_windows: '桌面窗口',
  lsp_diagnostics: '类型检查', lsp_definition: '跳转定义',
  lsp_references: '查找引用', lsp_rename: '语义重命名',
  git_status: '查看 Git 状态', git_diff: '查看改动',
  git_log: '查看提交历史', git_commit: 'Git 提交',
};

function toolArgText(name, args) {
  args = args || {};
  if (name === 'run_command') {
    return (Array.isArray(args.argv) ? args.argv.join(' ') : '') || args.cmd || args.command || '';
  }
  if (name === 'move_file') return (args.from || '') + ' → ' + (args.to || '');
  if (name === 'computer_open') return args.url || '';
  if (name === 'computer_act') {
    return [args.target, args.action, args.ref != null ? '[' + args.ref + ']' : '', args.value]
      .filter((x) => x !== '' && x != null).join(' ');
  }
  if (name === 'computer_see') return args.target || '';
  if (name === 'computer_shot') return args.target || '';
  if (name === 'computer_read') return [args.kind || 'console', args.target].filter(Boolean).join(' ');
  if (name === 'render_check') return [args.path, args.viewport].filter(Boolean).join('   ');
  if (name === 'search_files') return [args.pattern, args.glob].filter(Boolean).join('   ');
  if (name === 'lsp_rename') {
    return [args.path, args.symbol && (args.symbol + ' → ' + (args.new_name || ''))]
      .filter(Boolean).join('   ');
  }
  if (String(name).startsWith('lsp_')) {
    return [args.path, args.line && ('第 ' + args.line + ' 行'), args.symbol]
      .filter(Boolean).join('   ');
  }
  if (name === 'git_commit') return String(args.message || '').replace(/\n/g, ' ');
  return args.path || '';
}

// 能定位到具体文件的工具 → 其路径在工具卡里渲染成可点击链接（点了切到 IDE 打开）
const TOOL_PATH_TOOLS = ['read_file', 'write_file', 'edit_file', 'delete_file', 'render_check',
  'lsp_diagnostics', 'lsp_definition', 'lsp_references', 'lsp_rename', 'git_diff'];

// Git 高危子命令：与后端 agent._cmd_kind 的高风险集合保持一致（只用于前端标注）
const GIT_RISK_SUBS = ['checkout', 'restore', 'reset', 'clean', 'push', 'switch', 'rebase'];

function toolGitInfo(name, args) {
  if (name !== 'run_command') return null;
  args = args || {};
  let argv = args.argv;
  if (typeof argv === 'string') argv = argv.split(/\s+/).filter(Boolean);
  if (!Array.isArray(argv) || !argv.length) {
    const cmd = String(args.cmd || args.command || '').trim();
    argv = cmd ? cmd.split(/\s+/).filter(Boolean) : [];
  }
  if (!argv.length) return null;
  const exe = String(argv[0]).replace(/\\/g, '/').split('/').pop().toLowerCase();
  if (exe !== 'git' && exe !== 'git.exe') return null;
  let sub = '';
  for (let i = 1; i < argv.length; i += 1) {
    const t = String(argv[i]);
    if (t.startsWith('-')) continue;
    sub = t;
    break;
  }
  sub = sub || 'git';
  return { sub: sub, risk: GIT_RISK_SUBS.includes(sub.toLowerCase()) };
}

async function openGitPanel() {
  if (typeof setUiMode === 'function') await setUiMode('ide');
  if (typeof ideSwitchSide === 'function') ideSwitchSide('git');
  if (typeof gitRefresh === 'function') gitRefresh();
}

function toolArgHtml(name, args) {
  args = args || {};
  const link = (p) => '<a class="tool-path" data-path="' + escapeHtml(String(p)) +
    '" title="在 IDE 中打开">' + escapeHtml(String(p)) + '</a>';
  if (name === 'move_file') {
    if (args.from || args.to) {
      return (args.from ? link(args.from) : '') + ' → ' + (args.to ? link(args.to) : '');
    }
  } else if (TOOL_PATH_TOOLS.includes(name) && args.path) {
    return link(args.path);
  }
  return escapeHtml(toolArgText(name, args));
}

// 点工具卡里的路径：切到 IDE 模式并在应用内打开该文件
async function openToolPath(path) {
  if (!path) return;
  const rel = String(path).replace(/\\/g, '/');
  setUiMode('ide');
  await openIdeFile(rel);
}

function fmtToolMs(ms) {
  ms = parseInt(ms, 10) || 0;
  return ms >= 1000 ? (ms / 1000).toFixed(1) + 's' : ms + 'ms';
}

/* ---------- 执行计划卡（Plan / TODO） ----------
   模型通过 update_plan 工具维护；这里只负责把最新计划画成一张可折叠清单。 */
const PLAN_MARK = { done: '✓', doing: '●', pending: '○', blocked: '!' };

function buildPlanCard(plan, collapsed) {
  const card = document.createElement('div');
  card.className = 'plan-card';
  card.innerHTML =
    '<div class="plan-head">' +
      '<span class="plan-ico">' + icon('check-square') + '</span>' +
      '<span class="plan-title"></span>' +
      '<span class="plan-delta-count"></span>' +
      '<span class="tool-caret">' + icon('chevron') + '</span>' +
    '</div>' +
    '<div class="plan-body"></div>';
  const head = card.querySelector('.plan-head');
  const toggle = () => {
    card.classList.toggle('collapsed');
    head.setAttribute('aria-expanded', card.classList.contains('collapsed') ? 'false' : 'true');
  };
  head.addEventListener('click', toggle);
  makeClickable(head, toggle, '展开 / 收起执行计划');
  planFill(card, plan || { steps: [] });
  if (collapsed) {
    card.classList.add('collapsed');
    head.setAttribute('aria-expanded', 'false');
  } else {
    head.setAttribute('aria-expanded', 'true');
  }
  return card;
}

/* 计划 diff：比较「上一版 → 这一版」，标出这次具体改了哪几步。
   返回 { map: {id: {label, cls}}, added, changed, removed }；没有基准时全空。 */
function planDiff(prev, next) {
  const out = { map: {}, added: 0, changed: 0, removed: 0 };
  const before = Array.isArray(prev) ? prev : ((prev && prev.steps) || []);
  const after = Array.isArray(next) ? next : ((next && next.steps) || []);
  if (!before.length || !after.length) return out;
  const old = {};
  before.forEach((s) => { if (s && s.id != null) old[String(s.id)] = s; });
  const now = {};
  after.forEach((s) => { if (s && s.id != null) now[String(s.id)] = s; });
  after.forEach((s) => {
    const id = String((s && s.id) || '');
    if (!id) return;
    const p = old[id];
    if (!p) { out.map[id] = { label: '新增', cls: 'new' }; out.added += 1; return; }
    const bits = [];
    const st = String(s.status || '');
    if (String(p.status || '') !== st) {
      bits.push(st === 'done' ? '完成'
        : st === 'blocked' ? '受阻'
          : st === 'doing' ? '进行中' : '重置');
    }
    if (String(p.text || '') !== String(s.text || '')) bits.push('改写');
    if (String(p.verify || '') !== String(s.verify || '')) bits.push('验收');
    if (String(p.ref || '') !== String(s.ref || '')) bits.push('引用');
    if (String(p.milestone || '') !== String(s.milestone || '')) bits.push('移组');
    if (String((p.deps || []).join(',')) !== String((s.deps || []).join(','))) bits.push('依赖');
    if (String((p.accept || []).join(',')) !== String((s.accept || []).join(','))) bits.push('验收项');
    if (bits.length) { out.map[id] = { label: bits.join('·'), cls: 'mod' }; out.changed += 1; }
  });
  before.forEach((s) => {
    const id = String((s && s.id) || '');
    if (id && !now[id]) out.removed += 1;
  });
  return out;
}

function planStepHtml(s, delta) {
  const st = (s && s.status) || 'pending';
  const verify = (s && s.verify) ? String(s.verify) : '';
  const ref = (s && s.ref) ? String(s.ref) : '';
  const accept = (s && Array.isArray(s.accept)) ? s.accept.filter(Boolean).join(' ') : '';
  const d = delta || null;
  return '<div class="plan-step ' + st + (d ? ' changed ' + d.cls : '') + '">' +
      '<span class="plan-mark">' + (PLAN_MARK[st] || PLAN_MARK.pending) + '</span>' +
      '<span class="plan-text">' + escapeHtml((s && s.text) || '') + '</span>' +
      (accept ? '<span class="plan-accept" title="关联的验收项">⚑ ' + escapeHtml(accept) + '</span>' : '') +
      (d ? '<span class="plan-delta ' + d.cls + '">' + escapeHtml(d.label) + '</span>' : '') +
      (verify ? '<span class="plan-verify">↳ ' + escapeHtml(verify) + '</span>' : '') +
      (ref ? '<span class="plan-ref">↗ ' + escapeHtml(ref) + '</span>' : '') +
    '</div>';
}

function planFill(card, plan) {
  // 兼容旧调用：plan 直接是 steps 数组
  const isArr = Array.isArray(plan);
  const steps = isArr ? plan : ((plan && plan.steps) || []);
  // 同一版计划重复到达时不重画：否则刚闪出来的改动徽标会被擦掉
  if (card._diffBase && JSON.stringify(card._diffBase) === JSON.stringify(isArr ? steps : plan)) {
    return;
  }
  const mss = isArr ? [] : ((plan && plan.milestones) || []);
  const rev = Number(!isArr && plan && plan.revision) || 1;
  const goal = (!isArr && plan && plan.goal) || '';
  const doneWhen = (!isArr && plan && plan.done_when) || [];
  const log = (!isArr && plan && plan.log) || {};
  const diff = planDiff(card._diffBase, isArr ? steps : plan);
  card._diffBase = isArr ? steps : plan;
  const done = steps.filter((s) => s.status === 'done').length;
  const doing = steps.find((s) => s.status === 'doing');
  const title = card.querySelector('.plan-title');
  const doingMs = doing ? mss.find((m) => m.id === doing.milestone) : null;
  if (title) {
    let t;
    if (steps.length && done === steps.length) {
      t = '计划完成 · ' + steps.length + ' 步' + (rev > 1 ? ' · v' + rev : '');
    } else if (doing) {
      t = '计划 v' + rev + ' · ' + done + '/' + steps.length + ' · 正在'
        + (doingMs ? ' ' + (doingMs.id || '') + '「' + (doingMs.title || '') + '」：' : '')
        + (doing.text || '');
    } else {
      t = '计划 v' + rev + ' · ' + done + '/' + steps.length + ' 步';
    }
    title.textContent = t;
    card.title = goal || '';
  }
  // 本次改动：头部一行小字 + 具体步骤上的徽标（步骤徽标在 planStepHtml 里）
  const deltaEl = card.querySelector('.plan-delta-count');
  if (deltaEl) {
    const bits = [];
    if (diff.added) bits.push('新增 ' + diff.added);
    if (diff.changed) bits.push('改动 ' + diff.changed);
    if (diff.removed) bits.push('删除 ' + diff.removed);
    deltaEl.textContent = bits.length ? '本次 ' + bits.join(' · ') : '';
    deltaEl.style.display = bits.length ? '' : 'none';
  }
  const body = card.querySelector('.plan-body');
  if (body) {
    let html = '';
    if (goal) {
      html += '<div class="plan-meta"><div class="plan-goal">目标：'
        + escapeHtml(goal) + '</div></div>';
    }
    if (diff.removed) {
      html += '<div class="plan-delta-note">本次删除了 ' + diff.removed + ' 步</div>';
    }
    if (mss.length) {
      const used = new Set();
      mss.forEach((m) => {
        const ss = steps.filter((s) => s.milestone === m.id);
        ss.forEach((s) => used.add(s));
        if (!ss.length) return;
        const d = ss.filter((s) => s.status === 'done').length;
        const blocked = ss.some((s) => s.status === 'blocked');
        const cls = d === ss.length ? ' done' : (blocked ? ' blocked' : (ss.some((s) => s.status === 'doing') ? ' doing' : ''));
        html += '<div class="plan-ms' + cls + '">'
          + '<div class="plan-ms-head"><span class="plan-ms-id">' + escapeHtml(m.id || '') + '</span>'
          + '<span class="plan-ms-title">' + escapeHtml(m.title || '') + '</span>'
          + '<span class="plan-ms-count">' + d + '/' + ss.length + '</span></div>'
          + (m.done_when ? '<div class="plan-ms-done">验收：' + escapeHtml(m.done_when) + '</div>' : '')
          + ss.map((s) => planStepHtml(s, diff.map[String(s.id || '')])).join('')
          + '</div>';
      });
      const rest = steps.filter((s) => !used.has(s));
      if (rest.length) {
        html += rest.map((s) => planStepHtml(s, diff.map[String(s.id || '')])).join('');
      }
    } else {
      html += steps.map((s) => planStepHtml(s, diff.map[String(s.id || '')])).join('');
    }
    if (doneWhen.length) {
      // 整体完成判据（两行以内，避免卡片太长）
      const dw = doneWhen.slice(0, 4).map((x) => '<li>' + escapeHtml(String(x)) + '</li>').join('');
      html += '<div class="plan-done-when"><span>完成判据</span><ul>' + dw + '</ul></div>';
    }
    const retro = (!isArr && plan && plan.retro) ? String(plan.retro) : '';
    if (retro) {
      html += '<div class="plan-retro"><span>复盘</span><div>' + escapeHtml(retro) + '</div></div>';
    }
    const logs = []
      .concat((log.decisions || []).slice(-3).map((x) => '决策：' + (x.text || '')))
      .concat((log.discoveries || []).slice(-3).map((x) => '发现：' + (x.text || '')));
    if (logs.length) {
      html += '<details class="plan-log"><summary>变更记录（' + logs.length + '）</summary>'
        + logs.map((x) => '<div class="plan-log-item">' + escapeHtml(x) + '</div>').join('')
        + '</details>';
    }
    body.innerHTML = html;
  }
  card.classList.toggle('all-done', steps.length > 0 && done === steps.length);
}

// 流式更新：同一个 row 复用同一张卡，只刷新内容
function planRender(row, plan, note) {
  if (!row) return;
  const isArr = Array.isArray(plan);
  const steps = isArr ? plan : ((plan && plan.steps) || []);
  if (!steps.length) return;
  const full = isArr ? { steps: steps } : plan;
  let card = row._planCard;
  if (!card) {
    card = buildPlanCard(full, false);
    if (row._content && row._content.parentNode === row._wrap) {
      row._wrap.insertBefore(card, row._content);     // 计划在正文上方
    } else if (row._wrap) {
      row._wrap.appendChild(card);
    }
    row._planCard = card;
    row._planNote = note || '';
  }
  planFill(card, full);
  row._planSteps = steps;
  row._plan = full;
  state.lastPlan = steps;          // 输入框上方的审阅提示栏要展开「任务项」
}

// 本轮结束：全部完成才自动折叠（没做完就保持展开，方便看到卡在哪一步）
function planAutoCollapse(row) {
  const card = row && row._planCard;
  const steps = (row && row._planSteps) || [];
  if (!card || !steps.length || !steps.every((s) => s.status === 'done')) return;
  card.classList.add('collapsed');
  const head = card.querySelector('.plan-head');
  if (head) head.setAttribute('aria-expanded', 'false');
}

/* ---------- 工作流卡（run_workflow）：节点状态 + 暂停 / 继续 / 取消 ---------- */
const WF_MARK = { done: '✓', failed: '✗', skipped: '⏭', running: '●', pending: '○' };
const WF_STATUS = { running: '执行中', completed: '已完成', failed: '失败',
                    paused: '已暂停', cancelled: '已取消', blocked: '受阻' };

function wfNodeInfo(s) {
  const bits = [];
  if (s.ms) bits.push((s.ms / 1000).toFixed(1) + 's');
  if (s.tokens) bits.push(s.tokens + 'tok');
  if (s.status === 'failed' && s.error) bits.push(s.error);
  return bits.join(' · ');
}

function wfButton(text, onClick) {
  const b = document.createElement('button');
  b.className = 'wf-btn';
  b.type = 'button';
  b.textContent = text;
  b.addEventListener('click', (e) => { e.stopPropagation(); onClick(); });
  return b;
}

async function wfPost(action, id) {
  try {
    const r = await api('/api/workflow/' + action, { id: id });
    if (!r || !r.ok) toast('操作失败（' + action + '）');
  } catch (e) { toast('操作失败：' + e); }
}

function wfResume(id) {
  toast('继续执行工作流…');
  const p = streamChat({ workflow: id, content: '继续执行工作流 ' + id });
  if (p && p.catch) p.catch((e) => toast('继续失败：' + e));
}

function workflowRender(row, wf) {
  if (!row || !wf || !wf.id) return;
  let card = row._wfCard;
  if (!card) {
    card = document.createElement('div');
    card.className = 'wf-card';
    card.innerHTML = '<div class="wf-head"><span class="wf-title"></span>'
      + '<span class="wf-meta"></span></div>'
      + '<div class="wf-body"></div><div class="wf-actions"></div>';
    if (row._content && row._content.parentNode === row._wrap) {
      row._wrap.insertBefore(card, row._content);      // 工作流在正文上方
    } else if (row._wrap) {
      row._wrap.appendChild(card);
    }
    row._wfCard = card;
  }
  const steps = wf.steps || [];
  const done = steps.filter((s) => s.status === 'done').length;
  const title = card.querySelector('.wf-title');
  if (title) title.textContent = '工作流 · ' + (wf.name || wf.id);
  const meta = card.querySelector('.wf-meta');
  if (meta) {
    const tok = (wf.used && wf.used.tokens) ? ' · ' + wf.used.tokens + ' tok' : '';
    meta.textContent = (WF_STATUS[wf.status] || wf.status || '') + ' '
      + done + '/' + steps.length + tok;
  }
  const body = card.querySelector('.wf-body');
  if (body) {
    body.innerHTML = steps.map((s) =>
      '<div class="wf-node ' + escapeHtml(s.status || 'pending') + '">'
      + '<span class="wf-mark">' + (WF_MARK[s.status] || '○') + '</span>'
      + '<span class="wf-id">' + escapeHtml(s.id || '') + '</span>'
      + '<span class="wf-type">' + escapeHtml(s.type || '') + '</span>'
      + '<span class="wf-info">' + escapeHtml(wfNodeInfo(s)) + '</span>'
      + '</div>').join('');
  }
  const acts = card.querySelector('.wf-actions');
  if (acts) {
    acts.innerHTML = '';
    const st = wf.status || '';
    if (st === 'running') acts.appendChild(wfButton('暂停', () => wfPost('pause', wf.id)));
    if (st === 'paused' || st === 'blocked' || st === 'failed' || st === 'cancelled') {
      acts.appendChild(wfButton('继续', () => wfResume(wf.id)));
    }
    if (st === 'running' || st === 'paused') {
      acts.appendChild(wfButton('取消', () => wfPost('cancel', wf.id)));
    }
    acts.style.display = acts.children.length ? '' : 'none';
  }
  row._wfState = wf;
}

/* ---------- 回滚本轮 / 写入审批 ---------- */
async function turnUndo() {
  const s = curSession();
  if (!s) return;
  if (!window.confirm('回滚本轮所有写操作？\n文件会恢复到本轮开始前的状态（对话不会回退）。')) return;
  try {
    const r = await api('/api/agent/turn_undo', { session_id: s.id });
    if (!r || !r.ok) { toast((r && r.error) || '回滚失败'); return; }
    toast('已回滚 ' + r.done + ' 处改动' + (r.failed ? '，' + r.failed + ' 处已过期' : ''));
    if (state.uiMode === 'ide' && typeof loadIdeTree === 'function') loadIdeTree();
  } catch (e) { toast('回滚失败：' + e); }
}

// 写入 / 命令审批卡：后端在真正执行前挂起，等这里点「允许 / 拒绝」
// 「提问」是可选项：不点就不发请求、不花 token；解释由模型给出，不会执行任何操作
function buildApprovalCard(obj) {
  const info = TOOL_LABELS[obj.name] || [null, obj.name];
  const arg = (typeof toolArgText === 'function') ? toolArgText(obj.name, obj.args) : '';
  const reason = obj.reason ? String(obj.reason) : '';
  // 命令与浏览器操作不支持「本轮都允许」：放开一次等于交出任意代码执行权 / 任意站点访问权
  const canRemember = obj.name !== 'run_command' &&
    !String(obj.name || '').startsWith('computer_');
  const card = document.createElement('div');
  card.className = 'approve-card';
  card.innerHTML =
    '<div class="approve-head"><span class="approve-ico">' + icon('alert') + '</span>' +
      '<span class="approve-title">等待确认：' + escapeHtml(info[1]) +
        (arg ? ' · ' + escapeHtml(arg) : '') + '</span></div>' +
    (reason ? '<div class="approve-reason">' + escapeHtml(reason) + '</div>' : '') +
    '<div class="approve-actions">' +
      '<button class="approve-ok" type="button">允许</button>' +
      (canRemember ? '<button class="approve-always" type="button">本轮都允许</button>' : '') +
      '<button class="approve-no" type="button">拒绝</button>' +
      '<button class="approve-ask-btn" type="button">提问</button>' +
    '</div>' +
    '<div class="approve-ask" hidden>' +
      '<div class="approve-ask-note">模型解释，未执行任何操作</div>' +
      '<div class="approve-qa"></div>' +
      '<div class="approve-ask-row">' +
        '<input class="approve-ask-input" type="text" maxlength="400" ' +
          'placeholder="例如：这条命令会改什么？为什么要跑它？">' +
        '<button class="approve-ask-send" type="button">发送</button>' +
      '</div>' +
    '</div>';
  const askBox = card.querySelector('.approve-ask');
  const askInput = card.querySelector('.approve-ask-input');
  const askSend = card.querySelector('.approve-ask-send');
  const qaBox = card.querySelector('.approve-qa');
  const qa = [];                        // 已问过的 [{q,a}]，追问时一并回传
  card.querySelector('.approve-ask-btn').onclick = () => {
    askBox.hidden = !askBox.hidden;
    if (!askBox.hidden) askInput.focus();
  };
  const addQA = (q, a) => {
    const item = document.createElement('div');
    item.className = 'approve-qa-item';
    const qEl = document.createElement('div');
    qEl.className = 'approve-q';
    qEl.textContent = q;
    const aEl = document.createElement('div');
    aEl.className = 'approve-a';
    aEl.textContent = a;
    item.appendChild(qEl);
    item.appendChild(aEl);
    qaBox.appendChild(item);
    return aEl;
  };
  const ask = () => {
    if (askSend.disabled) return;
    const q = askInput.value.trim();
    const shown = q || '这次操作要做什么？为什么需要？会不会改动已有内容？';
    const aEl = addQA(shown, '正在询问…');
    askSend.disabled = true;
    askInput.disabled = true;
    api('/api/agent/explain', { aid: obj.aid, question: q, history: qa })
      .then((r) => {
        const answer = (r && r.answer) || '';
        aEl.textContent = answer || '（无回答）';
        qa.push({ q: shown, a: answer });
        askInput.value = '';
      })
      .catch((e) => { aEl.textContent = '解释失败：' + (e && e.message ? e.message : e); })
      .finally(() => {
        // 卡片已被允许 / 拒绝时不再恢复输入，避免误以为审批还开着
        if (!card.classList.contains('allowed') && !card.classList.contains('denied')) {
          askSend.disabled = false;
          askInput.disabled = false;
          askInput.focus();
        }
      });
  };
  askSend.onclick = ask;
  askInput.onkeydown = (e) => {
    if (e.key === 'Enter' && !e.isComposing) { e.preventDefault(); ask(); }
  };
  const settle = (ok, remember) => {
    card.classList.add(ok ? 'allowed' : 'denied');
    const acts = card.querySelector('.approve-actions');
    if (acts) acts.remove();
    if (askBox) askBox.hidden = true;
    const t = card.querySelector('.approve-title');
    const head = ok ? (remember ? '已允许（本轮不再询问）：' : '已允许：') : '已拒绝：';
    if (t) t.textContent = head + info[1] + (arg ? ' · ' + arg : '');
  };
  const send = (ok, remember) => {
    card.querySelectorAll('button').forEach((b) => { b.disabled = true; });
    api('/api/agent/approve', { aid: obj.aid, ok: ok, remember: !!remember })
      .then((r) => {
        if (!r || !r.ok) {
          // 审批记录已经不存在（超时 / 被别的操作处理）：不能再显示「已允许」
          card.classList.add('denied');
          const acts = card.querySelector('.approve-actions');
          if (acts) acts.remove();
          if (askBox) askBox.hidden = true;
          const t = card.querySelector('.approve-title');
          if (t) t.textContent = '审批已失效：' + info[1] + (arg ? ' · ' + arg : '');
          return toast('这次审批已失效（可能已超时或被处理）');
        }
        settle(ok, remember);
      })
      .catch((e) => {
        // 网络失败不代表操作已被拒绝：恢复按钮，让用户重试
        card.querySelectorAll('button').forEach((b) => { b.disabled = false; });
        toast('确认失败：' + e);
      });
  };
  card.querySelector('.approve-ok').onclick = () => send(true, false);
  const alwaysBtn = card.querySelector('.approve-always');
  if (alwaysBtn) alwaysBtn.onclick = () => send(true, true);
  card.querySelector('.approve-no').onclick = () => send(false, false);
  return card;
}

/* collapse=true：结果默认收起，点这一行才展开（普通对话用；GAL 传 false 保持原样） */
function buildToolCard(name, args, collapse) {
  const gi = toolGitInfo(name, args);
  const info = gi ? ['branch', 'Git · ' + gi.sub] : (TOOL_LABELS[name] || ['gear', name]);
  const card = document.createElement('div');
  card.className = 'tool-card running'
    + (gi ? ' git-op' : '')
    + (gi && gi.risk ? ' git-risk' : '')
    + (collapse === false ? ' always-open' : '');
  card.innerHTML =
    '<div class="tool-head">' +
      '<span class="tool-ico">' + icon(info[0]) + '</span>' +
      '<span class="tool-name">' + escapeHtml(info[1]) + '</span>' +
      (gi && gi.risk ? '<span class="tool-git-risk">高危</span>' : '') +
      '<span class="tool-arg">' + toolArgHtml(name, args) + '</span>' +
      '<span class="tool-meta"></span>' +
      '<span class="tool-state">执行中…</span>' +
      '<span class="tool-caret">' + icon('chevron') + '</span>' +
    '</div>' +
    '<pre class="tool-result"></pre>' +
    '<div class="tool-actions"></div>';
  if (gi) {
    const hint = document.createElement('div');
    hint.className = 'tool-git-hint';
    hint.textContent = gi.risk
      ? '高危 Git 操作：可能覆盖工作区。可在 IDE「源代码管理 → AI 操作」核对。'
      : '';
    if (hint.textContent) {
      const head = card.querySelector('.tool-head');
      if (head) head.insertAdjacentElement('afterend', hint);
    }
    const acts = card.querySelector('.tool-actions');
    if (acts) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'review-btn';
      btn.textContent = '查看 Git';
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        openGitPanel();
      });
      acts.appendChild(btn);
    }
  }
  const toolHead = card.querySelector('.tool-head');
  const toolToggle = () => { if (card.classList.contains('has-result')) card.classList.toggle('open'); };
  toolHead.addEventListener('click', toolToggle);
  makeClickable(toolHead, toolToggle, '展开 / 收起工具结果');
  // 路径可点击：切到 IDE 模式并在应用内打开（stopPropagation 避免触发上面的折叠）
  card.querySelectorAll('.tool-path').forEach((a) => {
    a.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      openToolPath(a.dataset.path);
    });
  });
  return card;
}

function addWriteActions(card, obj) {
  const act = card.querySelector('.tool-actions');
  const token = obj && obj.undo;
  if (!act || !token) return;
  const settled = (state.ideSettledTokens || {})[token];
  const path = String(((obj.args || {}).path) || '');
  const btns = state.reviewCardBtns[token] = state.reviewCardBtns[token] || [];

  const view = document.createElement('button');
  if (path && typeof reviewOpenFor === 'function') {
    view.type = 'button';
    view.className = 'tool-undo';
    view.dataset.role = 'view';
    view.textContent = '查看变更';
    view.onclick = () => reviewOpenFor(path, token);
    if (settled) { view.disabled = true; view.textContent = '已处理'; }
    act.appendChild(view);
    btns.push(view);
  }

  const b = document.createElement('button');
  b.type = 'button';
  b.className = 'tool-undo';
  b.dataset.role = 'undo';
  b.textContent = '撤回';
  b.onclick = async () => {
    b.disabled = true;
    try {
      const r = await api('/api/agent/undo', { token: token });
      if (r && r.ok) {
        b.textContent = '已撤回';
        state.ideSettledTokens[token] = 'undone';
        if (typeof ideReviewSyncCards === 'function') ideReviewSyncCards([token], '已撤回');
        if (typeof reviewForgetToken === 'function') reviewForgetToken(token);
        toast('已撤回该次改动');
      } else { b.disabled = false; toast('撤回失败或已过期'); }
    } catch (e) { b.disabled = false; toast('撤回失败：' + e); }
  };
  if (settled) { b.disabled = true; b.textContent = settled === 'accepted' ? '已保留' : '已撤回'; }
  act.appendChild(b);
  btns.push(b);
}

function finishToolCard(card, obj) {
  obj = obj || {};
  card.classList.remove('running');
  const ok = obj.ok !== false;                 // 旧记录没有 ok 字段 → 视为成功
  card.classList.add(ok ? 'ok' : 'failed');
  const st = card.querySelector('.tool-state');
  if (st) st.textContent = ok ? (obj.write ? '已写入' : '完成') : '失败';
  const meta = card.querySelector('.tool-meta');
  if (meta) {
    const bits = [];
    if (obj.ms != null) bits.push(fmtToolMs(obj.ms));
    if (parseInt(obj.lines, 10) > 1) bits.push(obj.lines + ' 行');
    meta.textContent = bits.join(' · ');
  }
  const pre = card.querySelector('.tool-result');
  if (pre && obj.result) {
    pre.textContent = obj.result;
    card.classList.add('has-result');
  }
  if (obj.undo) addWriteActions(card, obj);
}

/* ---------- 一轮的工具调用归成一个可折叠「步骤组」（Trae 式：一行一步骤） ---------- */
function toolGroup(row) {
  if (row._toolGroup) return row._toolGroup;
  const el = document.createElement('div');
  el.className = 'tool-group running';
  el.innerHTML =
    '<div class="tool-group-head">' +
      '<span class="tool-group-ico">' + icon('gear') + '</span>' +
      '<span class="tool-group-title">运行中…</span>' +
      '<span class="tool-caret">' + icon('chevron') + '</span>' +
    '</div>' +
    '<div class="tool-group-body"></div>';
  const gHead = el.querySelector('.tool-group-head');
  const g = { el: el, body: el.querySelector('.tool-group-body'),
              pending: 0, done: 0, ms: 0, failed: 0, kinds: {},
              current: null, userToggled: false, hTimer: null,
              row: row, step: row._toolStep || 0 };
  gHead.addEventListener('click', () => setToolGroupOpen(g, el.classList.contains('collapsed'), true));
  makeClickable(gHead, () => setToolGroupOpen(g, el.classList.contains('collapsed'), true),
                '展开 / 收起这组工具');
  // 有过程流就放进流里（与思考块按时间线排开），否则退回旧的末尾容器
  if (row._flow) row._flow.appendChild(el);
  else if (row._toolsBox) row._toolsBox.appendChild(el);
  else row._wrap.appendChild(el);
  row._toolGroup = g;
  return g;
}

/* 展开 / 收起：用实测高度做过渡 —— max-height 写死会在长组上截断，动画也会「先跳一半」 */
function setToolGroupOpen(g, open, byUser) {
  if (!g) return;
  const head = g.el.querySelector('.tool-group-head');
  clearTimeout(g.hTimer);
  if (open) {
    g.el.classList.remove('collapsed');
    g.body.style.maxHeight = g.body.scrollHeight + 'px';
    g.hTimer = setTimeout(() => {
      if (!g.el.classList.contains('collapsed')) g.body.style.maxHeight = '';
    }, 240);
  } else {
    g.body.style.maxHeight = g.body.scrollHeight + 'px';
    void g.body.offsetHeight;            // 先落一次高度，0 才有过渡起点
    g.body.style.maxHeight = '0px';
    g.el.classList.add('collapsed');
  }
  if (head) head.setAttribute('aria-expanded', open ? 'true' : 'false');
  if (byUser) g.userToggled = true;
}

// 本轮结束 / 历史回显：默认折成一行；用户手动展开过就不再自动收
function autoCollapseToolGroup(g) {
  if (!g || g.pending > 0 || g.userToggled) return;
  if (g.el.classList.contains('collapsed')) return;
  g.el.classList.add('collapsed');
  g.body.style.maxHeight = '0px';
  const head = g.el.querySelector('.tool-group-head');
  if (head) head.setAttribute('aria-expanded', 'false');
}

function toolGroupTitle(g) {
  const t = g.el.querySelector('.tool-group-title');
  const ico = g.el.querySelector('.tool-group-ico');
  if (!t) return;
  const running = g.pending > 0;
  g.el.classList.toggle('running', running);
  g.el.classList.toggle('failed', !running && g.failed > 0);
  // 状态图标：运行中齿轮转、完成打勾、失败警示
  if (ico) ico.innerHTML = icon(running ? 'gear' : (g.failed > 0 ? 'alert' : 'check'));
  if (running) {
    let s = '运行中 · 已完成 ' + g.done + ' 步';
    // 有计划时把当前步骤带上：用户能对上「现在在第几步」
    const steps = (g.row && g.row._planSteps) || [];
    if (steps.length) {
      const doing = steps.find((x) => x.status === 'doing');
      const idx = doing ? steps.indexOf(doing) + 1 : g.done;
      s = '步骤 ' + Math.min(idx, steps.length) + '/' + steps.length
        + ' · 已完成 ' + g.done + ' 步';
      if (doing && doing.text) {
        const t2 = String(doing.text);
        s += ' · ' + (t2.length > 18 ? t2.slice(0, 18) + '…' : t2);
      }
    }
    if (g.current && g.current.name) {
      const verb = TOOL_VERBS[g.current.name]
        || (TOOL_LABELS[g.current.name] || [null, '调用'])[1];
      let arg = String(g.current.arg || '');
      if (arg.length > 28) arg = arg.slice(0, 28) + '…';
      s += ' · 正在' + verb + (arg ? ' ' + arg : '');
    }
    // 存一份「基础文案」：stream.js 的每秒计时器在它后面追加「· 已 Ns」，
    // 不存的话每次刷新都会把上一次的秒数再叠一遍。
    g.el._titleBase = s;
    t.textContent = s;
    return;
  }
  const kinds = Object.keys(g.kinds)
    .sort((a, b) => g.kinds[b] - g.kinds[a])
    .slice(0, 3)
    .map((k) => {
      const n = g.kinds[k];
      const fmt = TOOL_SUMMARY[k];
      return fmt ? fmt(n) : ((TOOL_LABELS[k] || [null, k])[1] + ' ' + n + ' 次');
    })
    .join(' · ');
  let s = '已执行 ' + g.done + ' 个步骤';
  if (kinds) s += ' · ' + kinds;
  if (g.ms) s += ' · ' + fmtToolMs(g.ms);
  if (g.failed) s += ' · ' + g.failed + ' 个失败';
  g.el._titleBase = s;
  t.textContent = s;
}

/* 提问卡：一组问题（可翻页预览、自由来回、最后统一提交）
   qs : [{question, options[], answer?}]  —— 有 answer 表示已作答（历史回显）
   qid: 后端提问 id（历史回显时为 null，此时不提交） */
function buildAskGroup(qs, qid) {
  const list = (qs || []).map((q) => ({
    question: String((q && q.question) || ''),
    options: ((q && q.options) || []).map((o) => String(o)).filter((o) => o.trim()),
    multiple: !!(q && q.multiple),
    answer: (q && q.answer != null) ? String(q.answer) : null,
  }));
  const multi = list.length > 1;
  // Solo 模式：底部按钮逐题推进（下一个问题），到最后一题才变回「提交全部」。
  // 其他模式保持原来的「随时提交全部」。
  const solo = (typeof state !== 'undefined' && state.uiMode === 'solo');
  const card = document.createElement('div');
  card.className = 'ask-card';

  // 已作答：只读展示「问题 → 回答」
  const doneHtml = (arr) =>
    '<div class="ask-q">' + icon('question') + ' 提问' + (multi ? '（' + arr.length + ' 个）' : '') + '</div>' +
    '<div class="ask-chosen-list">' + arr.map((q) =>
      '<div class="ask-chosen-item"><div class="ask-chosen-q">' + escapeHtml(q.question) + '</div>' +
      '<div class="ask-chosen-a">' + escapeHtml(q.answer == null ? '（未回答）' : q.answer) + '</div></div>').join('') +
    '</div>';

  if (list.length && list.every((q) => q.answer != null)) {
    card.classList.add('done');
    card.innerHTML = doneHtml(list);
    return card;
  }

  card.innerHTML =
    '<div class="ask-head"><span class="ask-q">' + icon('question') + ' 提问' + (multi ? '（' + list.length + ' 个）' : '') + '</span>' +
      '<span class="ask-pager">' +
        '<button type="button" class="ask-nav ask-prev" title="上一个">‹</button>' +
        '<span class="ask-idx">1 / ' + (list.length || 1) + '</span>' +
        '<button type="button" class="ask-nav ask-next" title="下一个">›</button>' +
      '</span></div>' +
    '<div class="ask-q-text"></div>' +
    '<div class="ask-opts"></div>' +
    '<div class="ask-input-row"><input class="ask-input field-input" placeholder="也可以自己输入回答…">' +
      '<button type="button" class="ask-send primary">提交</button></div>' +
    '<div class="ask-wait">⏳ 正在等待你的回复…（可用下方输入框或语音）</div>' +
    '<div class="ask-foot">' +
      '<button type="button" class="ask-prev-step" disabled>上一个问题</button>' +
      '<span class="ask-progress"></span>' +
      '<button type="button" class="ask-submit primary">提交全部</button></div>';
  bindAskInputSync(card);        // 单向同步：底部输入框内容（含语音识别结果）会填进卡片

  const qText = card.querySelector('.ask-q-text');
  const optsBox = card.querySelector('.ask-opts');
  const input = card.querySelector('.ask-input');
  const send = card.querySelector('.ask-send');
  const prev = card.querySelector('.ask-prev');
  const next = card.querySelector('.ask-next');
  const idxEl = card.querySelector('.ask-idx');
  const progress = card.querySelector('.ask-progress');
  const submitAll = card.querySelector('.ask-submit');
  const prevStep = card.querySelector('.ask-prev-step');
  if (!multi) {                                   // 单问：隐藏翻页与「提交全部」，回到原来的轻量样式
    card.querySelector('.ask-pager').style.display = 'none';
    card.querySelector('.ask-foot').style.display = 'none';
  } else {
    send.textContent = '记录';
    // 只给 Solo 加「上一个问题」；其他模式保留原来的翻页按钮
    if (!solo) prevStep.style.display = 'none';
  }

  // 选项答案与手写答案分开存：选选项不再把文字塞进输入框，
  // 翻到下一题时输入框只显示该题自己的手写内容，不会残留上一题的选项。
  // chosen 统一用数组：多选存多个，单选最多一个。
  const drafts = list.map(() => '');
  const chosen = list.map(() => []);
  let cur = 0;
  // 选项与手写内容可以并存：两边都有时一起提交（「选项A、选项B；手写补充」）。
  const answerOf = (i) => {
    const picked = chosen[i].join('、');
    const typed = (drafts[i] || '').trim();
    if (picked && typed) return picked + '；' + typed;
    return picked || typed;
  };
  const answered = () => list.filter((_q, i) => !!answerOf(i)).length;

  function render() {
    const q = list[cur] || { question: '', options: [] };
    qText.textContent = q.question;
    optsBox.innerHTML = '';
    const picked = chosen[cur];
    q.options.forEach((o) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'ask-opt' + (picked.indexOf(o) >= 0 ? ' sel' : '');
      b.textContent = o;
      b.onclick = () => {
        if (card.dataset.done) return;
        // 保留手写内容：选中气泡与输入框文字可以并存，提交时一起带上。
        const arr = chosen[cur];
        if (q.multiple) {
          // 多选：点气泡只切换选中，选完由「提交」统一定稿
          const at = arr.indexOf(o);
          if (at >= 0) arr.splice(at, 1);
          else arr.push(o);
          render();
          return;
        }
        // 单选也支持「再点一次取消」：不再点一下就直接提交，
        // 统一由「提交」按钮 / Enter 定稿，否则没有第二次点击的机会。
        const at = chosen[cur].indexOf(o);
        chosen[cur] = at >= 0 ? [] : [o];
        render();
      };
      optsBox.appendChild(b);
    });
    input.value = drafts[cur] || '';
    input.placeholder = q.multiple
      ? '可多选；再点已选项可取消，选好后点「提交」'
      : '点选项选中 / 再点取消，也可以自己输入，最后点「提交」';
    idxEl.textContent = (cur + 1) + ' / ' + list.length;
    prev.disabled = cur <= 0;
    next.disabled = cur >= list.length - 1;
    if (prevStep) prevStep.disabled = cur <= 0;
    progress.textContent = '已答 ' + answered() + '/' + list.length;
    if (multi && solo) {
      submitAll.textContent = cur >= list.length - 1 ? '提交全部' : '下一个问题';
    }
  }

  async function submit(answers) {
    if (card.dataset.done) return;
    card.dataset.done = '1';
    const answers2 = list.map((q, i) => ({ question: q.question,
      answer: ((answers[i] || '').trim() || '（未回答）') }));
    card.classList.add('done');
    card.innerHTML = doneHtml(answers2);          // 换成只读汇总
    // 提问期间底部输入框可能被同步填过内容 → 提交后清掉，避免残留
    const mainInput = $('#input');
    if (mainInput && mainInput.value) { mainInput.value = ''; autoGrow(); hideSttChip(); }
    if (qid) { try { await api('/api/agent/answer', { qid: qid, answers: answers }); } catch (e) { /* 忽略 */ } }
    scrollToBottom(true);
  }

  const saveDraft = () => {
    if (!multi) return;
    drafts[cur] = input.value.trim();   // 手写内容与已选项并存，不再互相清除
  };
  const resetSync = () => { if (typeof card._askSyncReset === 'function') card._askSyncReset(); };
  const step = (d) => {
    saveDraft();
    cur = Math.max(0, Math.min(list.length - 1, cur + d));
    render();
    resetSync();
    scrollToBottom();
  };

  prev.onclick = () => step(-1);
  next.onclick = () => step(1);
  if (prevStep) prevStep.onclick = () => step(-1);
  send.onclick = () => {
    if (!multi) {
      // 单问：手写内容与所选选项都带上
      submit([answerOf(0)]);
      return;
    }
    saveDraft();
    if (cur < list.length - 1) { cur += 1; render(); resetSync(); scrollToBottom(); }
  };
  input.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' || isImeComposing(e)) return;
    e.preventDefault();
    send.onclick();
  });
  input.addEventListener('input', () => {
    // 手写内容与已选气泡并存；drafts 在所有模式下都要记录，
    // 否则单问多选点气泡触发 render() 时会把刚输入的文字回填丢掉。
    drafts[cur] = input.value.trim();
    if (!multi) return;
    progress.textContent = '已答 ' + answered() + '/' + list.length;
  });
  submitAll.onclick = () => {
    saveDraft();
    if (solo && multi && cur < list.length - 1) {   // Solo：先逐题往下走
      cur += 1;
      render();
      resetSync();
      scrollToBottom();
      return;
    }
    submit(list.map((_q, i) => answerOf(i)));
  };

  render();
  return card;
}

/* 提问卡 ← 底部主输入框：单向同步
   目的：提问期间可以直接用主输入框把话写进来（尤其「语音输入」），再点卡片上的「提交」。
   用轮询而不是只听 input 事件：语音识别 / 预设回填 / GAL 选项都是直接给 value 赋值，
   监听事件会全部漏掉。
   只做「主输入框 → 卡片」单方向：卡片里打字不再反向写回底部输入框。此前是双向同步，
   两个框内容会互相复制（看起来像"镜像"），且提交后底部输入框残留同步过来的文字。 */
function bindAskInputSync(card) {
  const ai = card.querySelector('.ask-input');
  const main = $('#input');
  if (!ai || !main) return;
  let last = main.value;
  // 翻题时把基线对齐到主输入框当前内容，避免上一题残留的文字被同步进新题
  card._askSyncReset = () => { last = main.value; };
  const pull = () => {
    // 已提交（卡片被替换）→ 停止同步
    if (ai.disabled || !document.body.contains(ai)) { clearInterval(timer); return; }
    if (main.value !== last) {
      last = main.value;
      ai.value = main.value;
      ai.dispatchEvent(new Event('input', { bubbles: true }));
    }
  };
  const timer = setInterval(pull, 350);
}

// 历史回显：把 assistant 消息里保存的工具记录渲染出来
function renderToolLog(wrap, tools) {
  if (!tools || !tools.length) return;
  const box = document.createElement('div');
  box.className = 'tool-list';
  const row = { _toolsBox: box };      // 复用「步骤组」的归组逻辑
  tools.forEach((t) => {
    if (t.tool === 'ask_user') {
      const asks = Array.isArray(t.asks) ? t.asks
                 : [{ question: t.question, options: t.options, answer: t.answer }];
      box.appendChild(buildAskGroup(asks, null));
      return;
    }
    const card = buildToolCard(t.tool, t.args);
    finishToolCard(card, { name: t.tool, args: t.args, result: t.result, undo: t.undo,
                           write: t.write, ok: t.ok, ms: t.ms, lines: t.lines });
    const g = toolGroup(row);
    g.body.appendChild(card);
    g.done++;
    g.ms += parseInt(t.ms, 10) || 0;
    g.kinds[t.tool] = (g.kinds[t.tool] || 0) + 1;
    if (t.ok === false) g.failed++;
  });
  if (row._toolGroup) {
    toolGroupTitle(row._toolGroup);
    autoCollapseToolGroup(row._toolGroup);      // 历史回显默认折成一行
  }
  wrap.appendChild(box);
}

function buildAiRow(contentHtml, rawText, index, reasoning, sources, usage, memory, tools, plan) {
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
  if (plan && plan.steps && plan.steps.length) wrap.appendChild(buildPlanCard(plan, true));
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

/* 把 blocks 还原成一条消息的结构：
   - 折叠区（process-fold）：只放思考 + 工具组，这一块才是「过程」；
   - 正文段（content）：每段正文单独成段，按落库顺序排列 —— 正文就是正文，
     不进灰底折叠区，也不用「最后一段才算答案」去猜；
   - 提问卡：落在它被问出来的位置，而不是一律堆到消息末尾。
   返回 { fold, parts }，parts 是可以直接追加进 .content-wrap 的节点序列。 */
function buildBlocksView(blocks, timing, endKind, opts) {
  const options = opts || {};
  const list = blocks || [];
  const flow = document.createElement('div');
  flow.className = 'chat-flow';
  const ctx = { _flow: flow, _toolGroup: null };
  const finalizeGroup = () => {
    if (!ctx._toolGroup) return;
    toolGroupTitle(ctx._toolGroup);
    autoCollapseToolGroup(ctx._toolGroup);
    ctx._toolGroup = null;
  };
  const parts = [];
  list.forEach((b) => {
    if (!b || typeof b !== 'object') return;
    if (b.kind === 'reasoning' && b.text) {
      finalizeGroup();
      const box = buildThinkBlock(b.text, false);
      const head = thinkLineSummary(b.text, true);
      // 失败尝试的思考也保留：标成「上次尝试」而不是普通思考
      box._label.textContent = (b.retry ? '上次尝试（上游中断）' : '思考')
        + (head ? ' · ' + head : '');
      flow.appendChild(box);
      return;
    }
    if (b.kind === 'tool') {
      const t = (options.tools || [])[b.i];
      if (!t) return;
      if (t.tool === 'ask_user') {
        finalizeGroup();                       // 提问前后各成一组，卡片夹在中间
        if (t.asks && t.asks.length) parts.push(buildAskGroup(t.asks, null));
        return;
      }
      const g = toolGroup(ctx);
      const card = buildToolCard(t.tool, t.args);
      finishToolCard(card, { name: t.tool, args: t.args, result: t.result, undo: t.undo,
                             write: t.write, ok: t.ok, ms: t.ms, lines: t.lines });
      g.body.appendChild(card);
      g.done++;
      g.ms += parseInt(t.ms, 10) || 0;
      g.kinds[t.tool] = (g.kinds[t.tool] || 0) + 1;
      if (t.ok === false) g.failed++;
      return;
    }
    if (b.kind === 'text') {
      finalizeGroup();                        // 正文分界：下一批工具另起一组
      const text = typeof b.text === 'string' ? b.text : '';
      if (!text.trim()) return;
      const d = document.createElement('div');
      d.className = 'content';                // 与最终答案同一套排版
      d.innerHTML = safeRenderMarkdown(text);
      parts.push(d);
    }
  });
  finalizeGroup();
  const fold = flow.childNodes.length
    ? buildProcessFold(flow, timing, endKind, options.open === true)
    : null;
  return { fold: fold, flow: flow, parts: parts };
}

/* 历史回显（新消息）：按落库的 blocks 还原「过程折叠 + 正文分段 + 提问卡原位」。
   旧消息没有 blocks，继续走 buildAiRow，行为完全不变。 */
function buildAiRowFromBlocks(m, index) {
  const row = document.createElement('div');
  row.className = 'msg ai';
  if (typeof index === 'number') row.dataset.idx = index;
  const av = document.createElement('div');
  av.className = 'avatar';
  av.textContent = '✦';
  const wrap = document.createElement('div');
  wrap.className = 'content-wrap';
  if (m.sources && m.sources.length) wrap.appendChild(buildSourcesBlock(m.sources, false));
  if (m.plan && m.plan.steps && m.plan.steps.length) wrap.appendChild(buildPlanCard(m.plan, true));

  const view = buildBlocksView(m.blocks, m.timing, m.end_reason && m.end_reason.kind,
                               { tools: m.tools, open: false });
  if (view.fold) {
    wrap.appendChild(view.fold);
    if (window.enhanceRichMedia) window.enhanceRichMedia(view.fold);
  }
  const secs = [];
  const asks = [];
  view.parts.forEach((p) => {
    wrap.appendChild(p);
    if (p.classList.contains('ask-card')) asks.push(p);
    else secs.push(p);
  });
  if (!secs.length) {                 // 只有思考 / 工具的消息：正文退回落库文本
    const d = document.createElement('div');
    d.className = 'content';
    d.innerHTML = safeRenderMarkdown(m.content || '');
    wrap.appendChild(d);
    secs.push(d);
  }
  const c = secs[secs.length - 1];

  row.appendChild(av);
  row.appendChild(wrap);
  row._content = c;
  row._secs = secs;
  row._asks = asks;
  row._wrap = wrap;
  row._raw = m.content || '';
  if (m.memory) wrap.appendChild(buildMemoryChip(m.memory, index));
  if (m.content) wrap.appendChild(buildActions(row, () => row._raw, index, 'ai'));
  if (m.usage || m.done_at) {
    const u = document.createElement('div');
    u.className = 'usage-line';
    u.textContent = usageText(m.usage, m.done_at)
      || ('完成时间 ' + clockText(m.done_at));
    wrap.appendChild(u);
    row._usageEl = u;
  }
  return row;
}

function appendAi(contentHtml, rawText, index, reasoning, sources, usage, memory) {
  const row = buildAiRow(contentHtml, rawText, index, reasoning, sources, usage, memory);
  row.classList.add('enter');                    // 只有新消息播入场动画，历史重渲染不播
  $('#chat').appendChild(row);
  placeCtxRing();                               // 新行会插到圆环之后，重新挂回末尾
  if (window.enhanceRichMedia) window.enhanceRichMedia(row);
  scrollToBottom(true);
  return row;
}

const RENDER_PAGE = 60;   // 首屏只渲染最近 N 条，长历史时避免一次性渲染卡顿

/* 中断原因：一行小字 + 可选动作（继续输出 / 重试）。
   kind 决定颜色：network / server 红，length / max_rounds / empty 黄，stopped 灰。 */
function buildEndNote(reason, index) {
  const kind = String((reason && reason.kind) || '');
  const note = document.createElement('div');
  note.className = 'end-note'
    + (kind === 'network' || kind === 'server' || kind === 'billing' ? ' end-err'
       : (kind === 'stopped' || kind === 'plan') ? ' end-mute' : ' end-warn');
  const mark = document.createElement('span');
  mark.className = 'end-mark';
  mark.textContent = kind === 'plan' ? '◆' : '⏹';
  const txt = document.createElement('span');
  txt.className = 'end-text';
  txt.textContent = (reason && reason.text) || '本轮中断';
  note.appendChild(mark);
  note.appendChild(txt);
  const act = reason && reason.action;
  if (act === 'continue' && typeof index === 'number'
      && typeof continueReply === 'function') {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'end-act';
    b.textContent = '继续输出';
    b.onclick = () => continueReply(index);
    note.appendChild(b);
  } else if (act === 'resume' && typeof index === 'number'
             && typeof continueReply === 'function') {
    // 进程重启后留下的断点：后端会把「已完成步骤」一起回灌，避免重复写文件
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'end-act';
    b.textContent = '继续任务';
    b.onclick = () => continueReply(index, true);
    note.appendChild(b);
  } else if (act === 'retry' && typeof index === 'number'
             && typeof regenerateReply === 'function') {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'end-act';
    b.textContent = '重试';
    b.onclick = () => regenerateReply(index);
    note.appendChild(b);
  }
  return note;
}

// 为一条消息建 DOM 行（历史重渲染与增量展开共用）
function rowForMsg(msgs, idx) {
  const m = msgs[idx];
  if (m.role === 'user') return buildUserRow(m.content, idx, m);
  // 新消息有 blocks：按「过程在上、答案在下」回显；旧消息 / GAL 继续走原路径。
  const row = (m.blocks && m.blocks.length && !m.gal)
    ? buildAiRowFromBlocks(m, idx)
    : buildAiRow(safeRenderMarkdown(m.content), m.content, idx, m.reasoning,
                 m.sources, m.usage, m.memory, m.tools, m.plan);
  if (m.end_reason) {
    try {
      row._wrap.appendChild(buildEndNote(m.end_reason, idx));
    } catch (e) {
      /* 提示渲染失败不能让整条消息消失 */
    }
  }
  return row;
}

/* 单条消息的渲染边界：一条坏消息不能让整个会话列表变空白 */
function fallbackRow(msgs, idx, err) {
  const m = msgs[idx] || {};
  const row = document.createElement('div');
  row.className = 'msg ' + (m.role === 'user' ? 'user' : 'ai');
  const body = document.createElement('div');
  body.className = 'content md-fallback';
  body.textContent = String(m.content == null ? '（这条消息无法渲染）' : m.content);
  row.appendChild(body);
  if (window.console && console.error) console.error('[chat] 消息渲染失败，已降级', idx, err);
  return row;
}

function rowForMsgSafe(msgs, idx) {
  try {
    return rowForMsg(msgs, idx);
  } catch (e) {
    return fallbackRow(msgs, idx, e);
  }
}

/* ---------------- 历史压缩分隔线 ---------------- */
/* 压缩标记的唯一键：一个压缩点（upto）只应有一条分隔线。
   at 来自服务器时钟，跨设备会漂移，不能当唯一键。 */
function compressMarkKey(mark) {
  const upto = Number(mark && mark.upto);
  if (Number.isFinite(upto) && upto > 0) return 'u' + upto;
  return 'a' + String((mark && mark.at) || '');
}

function compressMarkTitle(mark) {
  const bits = [];
  if (mark && mark.count) bits.push('本次压缩 ' + mark.count + ' 条较早消息');
  if (mark && mark.at) {
    try { bits.push(new Date(Number(mark.at)).toLocaleString()); } catch (e) { /* 时间格式化失败不影响提示 */ }
  }
  return bits.join(' · ');
}

function compressDividerEl(mark, opts) {
  const el = document.createElement('div');
  el.className = 'compress-divider' + (opts && opts.pending ? ' pending' : '');
  if (mark) el.dataset.key = compressMarkKey(mark);
  const label = document.createElement('span');
  label.textContent = (opts && opts.pending)
    ? '正在压缩较早的历史对话…'
    : '历史对话已被压缩';
  el.appendChild(label);
  if (mark && mark.at) el.dataset.at = String(mark.at);
  if (mark) el.title = compressMarkTitle(mark);
  return el;
}

function compressPlaceAtEnd(chat, el) {
  // 本轮助手回复还在流式渲染时，分隔线应该落在它前面（压缩发生在回复开始之前）
  const host = chat.querySelector('.stream-host');
  if (host && host.parentNode === chat) chat.insertBefore(el, host);
  else chat.appendChild(el);
}

/* 从 context 同步压缩标记：全量对账（补 / 升级 / 删）。
   消息流里**只保留最近一条**压缩线：更早的压缩记录在进度条「详情」里看，
   否则编辑 / 反复压缩会让分隔线越堆越多（对齐 Trae 的「无感压缩」）。 */
function syncCompressMarks(ctx) {
  if (!ctx || !Array.isArray(ctx.compress_marks) || state.uiMode === 'gal') return;
  const chat = $('#chat');
  if (!chat) return;
  const marks = ctx.compress_marks.filter((m) => m && typeof m === 'object');
  const s = (state.sessions && Array.isArray(state.sessions.sessions)) ? curSession() : null;
  if (s) s.compress_marks = marks;
  const latest = marks.length ? marks[marks.length - 1] : null;
  const key = latest ? compressMarkKey(latest) : '';
  // 1) 删掉「已经不是最近一条」的历史分隔线（pending 留到下一步统一升级 / 清理）
  chat.querySelectorAll('.compress-divider:not(.pending)').forEach((el) => {
    if (!key || el.dataset.key !== key) {
      clearTimeout(el._timer);
      el.remove();
    }
  });
  const pending = chat.querySelector('.compress-divider.pending');
  if (!latest) {
    if (!pending) compressFlushQueue();      // 压缩失败 / 取消：别让排队消息一直等
    return;
  }
  let el = chat.querySelector('.compress-divider[data-key="' + key + '"]');
  if (!el && pending) el = pending;          // 升级「正在压缩」为已完成
  if (!el) {
    el = compressDividerEl(latest);
    compressPlaceAtEnd(chat, el);
  } else {
    clearTimeout(el._timer);
    el.classList.remove('pending');
    el.classList.add('fresh');
    el.dataset.key = key;
    el.dataset.at = String(latest.at || '');
    el.title = compressMarkTitle(latest);
    const label = el.querySelector('span');
    if (label) label.textContent = '历史对话已被压缩';
    const streamHost = chat.querySelector('.stream-host');
    if (streamHost && streamHost.parentNode === chat) chat.insertBefore(el, streamHost);
  }
  compressFlushQueue();
}

/* 压缩完成后放行排队消息（队列本身在 stream.js） */
function compressFlushQueue() {
  if (typeof flushSendQueue !== 'function') return;
  setTimeout(flushSendQueue, 180);
}

function compressPending() {
  if (state.uiMode === 'gal') return null;
  const chat = $('#chat');
  if (!chat) return null;
  let el = chat.querySelector('.compress-divider.pending');
  if (el) return el;
  el = compressDividerEl(null, { pending: true });
  el.dataset.started = String(Math.floor(Date.now() / 1000));
  compressPlaceAtEnd(chat, el);
  el.scrollIntoView({ block: 'nearest' });
  el._timer = setTimeout(() => {
    if (el.classList.contains('pending')) el.remove();   // 压缩失败时别留下假的「进行中」
    compressFlushQueue();                                // 别让排队消息一直等
  }, 120000);
  return el;
}

function compressPendingClear() {
  const chat = $('#chat');
  if (!chat) return;
  const el = chat.querySelector('.compress-divider.pending');
  if (el) { clearTimeout(el._timer); el.remove(); }
  compressFlushQueue();        // 压缩失败 / 取消：排队消息不能一直等
}

/* 该会话是否正在压缩较早历史（发送 / 修改要先排队） */
function compressIsPending() {
  const chat = $('#chat');
  return !!(chat && chat.querySelector('.compress-divider.pending'));
}

function compressClearAll() {
  const chat = $('#chat');
  if (!chat) return;
  chat.querySelectorAll('.compress-divider').forEach((el) => {
    clearTimeout(el._timer);
    el.remove();
  });
  compressFlushQueue();
}

// 「展开更早」按钮：本地已有更早消息就在本地展开；否则向后端按需拉一页
function makeMoreBtn(start) {
  const s = curSession();
  const loadedFrom = s ? (Number(s.msg_loaded_start) || 0) : 0;
  const more = document.createElement('button');
  more.className = 'load-more';
  if (start <= loadedFrom && loadedFrom > 0) {
    more.textContent = '↑ 加载更早的消息';
    more.onclick = () => loadOlderMessages(loadedFrom);
    return more;
  }
  const span = Math.max(0, start - loadedFrom);
  more.textContent = '↑ 展开更早的 ' + Math.min(span, RENDER_PAGE) + ' 条';
  more.onclick = () => renderMessages(Math.max(loadedFrom, start - RENDER_PAGE));
  return more;
}

/* 按需拉一页更早的消息，填回全局下标后再增量插到顶部 */
async function loadOlderMessages(before, limit, silent) {
  const s = curSession();
  if (!s || !s.id) return false;
  const from = Number.isFinite(before) ? before : (Number(s.msg_loaded_start) || 0);
  if (from <= 0) return false;
  const btn = $('#chat .load-more');
  if (btn) { btn.disabled = true; btn.textContent = '正在加载…'; }
  try {
    const r = await api('/api/session/messages', {
      session_id: s.id, before: from, limit: limit || RENDER_PAGE,
    });
    if (!r || !r.ok || !Array.isArray(r.messages)) {
      throw new Error((r && (r.error || r.message)) || '加载失败');
    }
    const start = Number(r.from) || 0;
    r.messages.forEach((m, i) => { s.messages[start + i] = m; });
    s.msg_loaded_start = Math.min(Number(s.msg_loaded_start) || 0, start);
    s.msg_total = Number(r.total) || s.messages.length;
    if (!silent) renderMessages(start, false);   // 增量插到最前面，保持视觉位置
    return true;
  } catch (e) {
    if (btn) { btn.disabled = false; btn.textContent = '↑ 加载更早的消息'; }
    toastErr('加载更早消息', e);
    return false;
  }
}

function renderMessages(fromIndex, forceWindow) {
  const chat = $('#chat');
  const expanding = typeof fromIndex === 'number';
  const oldHeight = chat.scrollHeight;
  const oldTop = chat.scrollTop;
  const s = curSession();
  if (!s || !(s.messages || []).length) {
    findState.hits = [];                       // 整块重排后，之前的高亮节点已经不存在了
    findState.idx = -1;
    chat.dataset.start = '';
    chat.innerHTML = EMPTY_HTML;
    bindChips();
    placeCtxRing();
    if (typeof turnDotsSync === 'function') turnDotsSync();
    if (typeof ideReviewTipRefresh === 'function') ideReviewTipRefresh();
    return;
  }

  const msgs = s.messages;
  const total = msgs.length;
  // 服务端只回了最近一段：loadedFrom 之前的位置是 null，需要时再向后端拉
  const loadedFrom = Number(s.msg_loaded_start) || 0;
  const marks = Array.isArray(s.compress_marks) ? s.compress_marks : [];
  const marksAt = {};
  // 只画最近一条：历史的压缩记录在「进度条 → 详情」里查，消息流不堆叠
  const latestMark = marks.length ? marks[marks.length - 1] : null;
  if (latestMark && typeof latestMark === 'object') {
    let idx = Number(latestMark.index);
    if (!Number.isFinite(idx)) idx = total;
    idx = Math.max(0, Math.min(idx, total));
    marksAt[idx] = [latestMark];
  }
  const addMarks = (frag, idx) => {
    (marksAt[idx] || []).forEach((m) => frag.appendChild(compressDividerEl(m)));
  };

  // 增量展开：已有内容仍然有效，只把新增的更早消息插到最前面，避免整块重排（长会话关键优化）
  // forceWindow=true 时跳过增量，直接把 fromIndex 当首行重排（对话记录跳转用：避免整段插入后图片
  // 异步加载把滚动位置顶跑）
  if (expanding && !forceWindow && chat.querySelector('.msg')) {
    const prevStart = Number(chat.dataset.start);
    if (Number.isFinite(prevStart) && fromIndex < prevStart && fromIndex >= loadedFrom) {
      findState.hits = [];
      findState.idx = -1;
      const frag = document.createDocumentFragment();
      for (let idx = fromIndex; idx < prevStart; idx++) {
        addMarks(frag, idx);
        frag.appendChild(rowForMsgSafe(msgs, idx));
      }
      chat.insertBefore(frag, chat.querySelector('.msg, .compress-divider'));
      chat.dataset.start = String(fromIndex);
      const more = chat.querySelector('.load-more');
      if (fromIndex > 0) {
        if (more) more.replaceWith(makeMoreBtn(fromIndex));
        else chat.insertBefore(makeMoreBtn(fromIndex), chat.firstChild);
      } else if (more) {
        more.remove();
      }
      chat.scrollTop = oldTop + (chat.scrollHeight - oldHeight);   // 保持视觉位置不动
      placeCtxRing();
      if (typeof turnDotsSync === 'function') turnDotsSync();
      return;
    }
  }

  // 要展开 / 跳转的位置还没加载：先拉那一页，再整体重画
  if (expanding && fromIndex < loadedFrom) {
    loadOlderMessages(fromIndex + 1, RENDER_PAGE, true)
      .then((ok) => { if (ok) renderMessages(fromIndex, true); });
    return;
  }

  const start = expanding ? Math.max(loadedFrom, fromIndex) : Math.max(loadedFrom, total - RENDER_PAGE);
  if (window.disposeRichMedia) window.disposeRichMedia();   // 清掉旧图表实例，避免内存泄漏
  chat.innerHTML = '';
  findState.hits = [];
  findState.idx = -1;

  // 先在一个 fragment 里建好，再一次性插入 —— 只触发一次布局
  const frag = document.createDocumentFragment();
  if (start > 0) frag.appendChild(makeMoreBtn(start));
  for (let idx = start; idx < total; idx++) {
    if (!msgs[idx]) continue;          // 还没加载的旧消息：等「加载更早」补
    addMarks(frag, idx);
    frag.appendChild(rowForMsgSafe(msgs, idx));
  }
  addMarks(frag, total);
  chat.appendChild(frag);
  chat.dataset.start = String(start);
  if (window.enhanceRichMedia) window.enhanceRichMedia(chat);
  // 展开更早时保持视觉位置不动，否则滚到底部
  chat.scrollTop = expanding ? oldTop + (chat.scrollHeight - oldHeight) : chat.scrollHeight;
  placeCtxRing();
  if (typeof turnDotsSync === 'function') turnDotsSync();
  if (typeof ideReviewTipRefresh === 'function') ideReviewTipRefresh();
}

function bindChips() {
  $$('.chip').forEach((c) => {
    c.onclick = () => { $('#input').value = c.dataset.prompt; sendMessage(); };
  });
  // 空状态里的模式卡：直接切到对应工作区（Solo / IDE / 大厅 / 工作台）
  $$('#empty .mode-card').forEach((c) => {
    c.onclick = () => {
      if (typeof setUiMode === 'function') setUiMode(c.dataset.mode);
    };
  });
}
