'use strict';

/* 流式对话 · queue.js
   发送队列：排队 / 展开编辑 / 取消
   原 web/stream.js 拆分而来：按 index.html 中的顺序加载，共享全局作用域。 */


/* ============================================================
   发送 / 流式
   ============================================================ */
/* ============================================================
   生成中的排队（Enter 不再被丢弃）
   语义：本轮回复完整结束后才逐条自动发送 —— 不打断模型，所以是「排队」，
   不是 Codex 那种 mid-turn 注入。
   ============================================================ */
const sendQueue = [];              // [{ text, sid, imgs, docs, quotes }]
let _queueFlushTimer = null;
let _queueOpen = false;            // 展开态：逐条修改 / 取消（默认收起，只显示汇总）
let editTarget = null;             // { sid, index }：正在输入框里改的历史用户消息

/* 队列按会话隔离：全局数组保留所有会话的排队项，
   但界面只显示当前会话的，修改 / 取消也只能动当前会话的。 */
function queueSid() {
  return String((state.sessions && state.sessions.current) || '');
}

function currentQueueItems() {
  const sid = queueSid();
  const out = [];
  sendQueue.forEach((item, index) => {
    if (String(item.sid) === sid) out.push({ item, index });
  });
  return out;
}

function queueSummary(items) {
  items = items || currentQueueItems();
  const n = items.length;
  if (n === 1) {
    const q = items[0].item;
    const badge = queueBadge(q);
    return '已排队：' + String(q.text || '(仅附件)').replace(/\s+/g, ' ').slice(0, 60)
      + (badge ? '（' + badge + '）' : '');
  }
  return '已排队 ' + n + ' 条，本轮结束后依次发送';
}

/* 单条的附件 / 引用标记：图 2 · 文档 2 · 引用 1 */
function queueBadge(q) {
  const parts = [];
  const n = (q.imgs || []).length;
  if (n) parts.push(n + ' 图');
  const dn = (q.docs || []).length;
  if (dn) parts.push(dn > 1 ? ('文档 ' + dn) : '文档');
  const qn = (q.quotes || []).length;
  if (qn) parts.push('引用 ' + qn);
  return parts.join(' · ');
}

function renderSendQueue() {
  const bar = $('#queue-bar');
  if (!bar) return;
  const items = currentQueueItems();
  const n = items.length;
  const multi = n > 1;                              // 只有多条才需要展开
  if (!multi) _queueOpen = false;
  bar.classList.toggle('hidden', !n);
  const txt = $('#queue-text');
  if (txt) {
    txt.textContent = queueSummary(items);
    txt.title = items.map(({ item }) => item.text).join('\n---\n');
  }
  const tg = $('#queue-toggle');
  if (tg) {
    tg.classList.toggle('hidden', !multi);
    tg.setAttribute('aria-expanded', _queueOpen ? 'true' : 'false');
    // 列表在上方：收起时箭头朝上（表示往上展开），展开后朝下（点它收回）
    tg.textContent = _queueOpen ? '▾' : '▴';
  }
  const bc = $('#queue-cancel');
  if (bc) bc.classList.toggle('hidden', !multi);
  // 只有一条：把「修改 / 取消」直接放进汇总行，不必为了操作去展开
  const inline = $('#queue-inline');
  if (inline) {
    inline.innerHTML = '';
    inline.classList.toggle('hidden', n !== 1);
    if (n === 1) {
      const gi = items[0].index;
      const bEdit = document.createElement('button');
      bEdit.type = 'button';
      bEdit.className = 'queue-item-act';
      bEdit.textContent = '修改';
      bEdit.onclick = () => editQueueItem(gi);
      const bDel = document.createElement('button');
      bDel.type = 'button';
      bDel.className = 'queue-item-act danger';
      bDel.textContent = '取消';
      bDel.onclick = () => removeQueueItem(gi);
      inline.appendChild(bEdit);
      inline.appendChild(bDel);
    }
  }
  const list = $('#queue-list');
  if (!list) return;
  if (!_queueOpen || !multi) {
    list.classList.add('hidden');
    list.innerHTML = '';
    return;
  }
  list.classList.remove('hidden');
  // 条数很少，整体重建比 diff 简单；编辑中的输入框由 editQueueItem 自己接管
  list.innerHTML = '';
  items.forEach(({ item: q, index: gi }, i) => {
    const row = document.createElement('div');
    row.className = 'queue-item';
    const idx = document.createElement('span');
    idx.className = 'queue-item-idx';
    idx.textContent = String(i + 1);
    const span = document.createElement('span');
    span.className = 'queue-item-text';
    span.textContent = String(q.text || '(仅附件)').replace(/\s+/g, ' ');
    span.title = q.text || '';
    const badge = queueBadge(q);
    const bMeta = document.createElement('span');
    bMeta.className = 'queue-item-meta';
    bMeta.textContent = badge;
    bMeta.classList.toggle('hidden', !badge);
    const bEdit = document.createElement('button');
    bEdit.type = 'button';
    bEdit.className = 'queue-item-act';
    bEdit.textContent = '修改';
    bEdit.onclick = () => editQueueItem(gi);
    const bDel = document.createElement('button');
    bDel.type = 'button';
    bDel.className = 'queue-item-act danger';
    bDel.textContent = '取消';
    bDel.onclick = () => removeQueueItem(gi);
    row.appendChild(idx);
    row.appendChild(span);
    row.appendChild(bMeta);
    row.appendChild(bEdit);
    row.appendChild(bDel);
    list.appendChild(row);
  });
  if (typeof renderQueueGhosts === 'function') renderQueueGhosts();
}

/* 排队中的消息在对话末尾显示成「幽灵气泡」：
   底部队列条只有贴近输入框才看得到，用户翻着历史根本不知道排了什么。
   这里只读队列数据，不参与发送；取消复用 removeQueueItem。 */
function renderQueueGhosts() {
  const box = $('#chat');
  if (!box) return;
  const items = (typeof currentQueueItems === 'function') ? currentQueueItems() : [];
  let host = document.getElementById('queue-ghosts');
  if (!items.length) {
    if (host) host.remove();
    return;
  }
  if (!host) {
    host = document.createElement('div');
    host.id = 'queue-ghosts';
    host.className = 'queue-ghosts';
  }
  host.innerHTML = '';
  items.forEach(({ item, index }, i) => {
    const row = document.createElement('div');
    row.className = 'queue-ghost';
    const meta = document.createElement('span');
    meta.className = 'queue-ghost-meta';
    meta.textContent = '排队 ' + (i + 1);
    const txt = document.createElement('span');
    txt.className = 'queue-ghost-text';
    txt.textContent = String(item.text || '(仅附件)').replace(/\s+/g, ' ').slice(0, 120);
    txt.title = item.text || '';
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'queue-ghost-x';
    b.textContent = '取消';
    b.onclick = () => removeQueueItem(index);
    row.appendChild(meta);
    row.appendChild(txt);
    row.appendChild(b);
    host.appendChild(row);
  });
  // 始终挂在最后（重排 / 新消息插入后都要回到末尾）
  if (host.parentNode !== box || host.nextElementSibling) box.appendChild(host);
}

function toggleSendQueue() {
  if (currentQueueItems().length <= 1) return;      // 单条不需要展开
  _queueOpen = !_queueOpen;
  renderSendQueue();
}

/* 单条取消：只影响这一条，其余继续排队 */
function removeQueueItem(i) {
  const item = sendQueue[i];
  if (!item || String(item.sid) !== queueSid()) return;   // 只允许动当前会话的
  sendQueue.splice(i, 1);
  renderSendQueue();
  toast('已取消这条排队消息');
}

/* 单条修改：从队列取出，把文本 / 附件 / 引用一起退回输入框，由用户改完再发 */
function editQueueItem(i) {
  const item = sendQueue[i];
  if (!item || String(item.sid) !== queueSid()) return;   // 只允许退回当前会话的输入框
  if (editTarget) cancelEditTarget(false);      // 别和历史消息编辑叠在一起
  sendQueue.splice(i, 1);
  renderSendQueue();

  const input = $('#input');
  if (input) {
    // 输入框已有草稿时换行追加，别把用户正在写的东西吃掉
    const draft = input.value.trim();
    input.value = (draft ? draft + '\n\n' : '') + (item.text || '');
    autoGrow();
    if (typeof cmpUpdateHint === 'function') cmpUpdateHint();
    if (typeof cmpUpdateClear === 'function') cmpUpdateClear();
    input.focus();
    try { input.setSelectionRange(input.value.length, input.value.length); } catch (e) { /* 忽略 */ }
  }

  // 附件：队列里存的就是 pendingImages / pendingDocs 的同构结构，直接还原
  if ((item.imgs && item.imgs.length) || (item.docs && item.docs.length)) {
    clearAttach();
    if (item.imgs && item.imgs.length) {
      state.pendingImages = item.imgs.map((it) => ({
        mime: it.mime, b64: it.b64, url: it.url, seq: it.seq,
      }));
      state.pendingImage = state.pendingImages[0];
    }
    if (item.docs && item.docs.length) {
      state.pendingDocs = item.docs.map((it) => ({
        name: it.name, mime: it.mime, b64: it.b64, size: it.size, seq: it.seq,
      }));
    }
    renderAttach();
  }
  if (item.quotes && item.quotes.length) {
    state.pendingQuotes = item.quotes.map((q) => ({ text: q.text, label: q.label || '' }));
    renderQuoteBar();
  }
  toast('已退回输入框，改完再发');
}

function cancelSendQueue(quiet) {
  const sid = queueSid();
  const n = sendQueue.filter((q) => String(q.sid) === sid).length;
  if (!n) return;
  // 只清当前会话的排队项；别的会话的队列不能跟着一起没
  for (let i = sendQueue.length - 1; i >= 0; i--) {
    if (String(sendQueue[i].sid) === sid) sendQueue.splice(i, 1);
  }
  renderSendQueue();
  if (!quiet) toast(n > 1 ? ('已取消 ' + n + ' 条排队消息') : '已取消排队消息');
}

/* 删除会话时清掉它的排队项，避免残留成永远发不出去的幽灵消息 */
function dropQueuedForSession(sid) {
  const key = String(sid);
  for (let i = sendQueue.length - 1; i >= 0; i--) {
    if (String(sendQueue[i].sid) === key) sendQueue.splice(i, 1);
  }
  renderSendQueue();
}

function flushSendQueue() {
  _queueFlushTimer = null;
  // 只在「当前会话」空闲时发；后台会话的排队项不动（它是另一个 sid 的）
  if (isStreaming() || compressBusyNow() || !sendQueue.length) return;
  const sid = String(state.sessions.current || '');
  const i = sendQueue.findIndex((q) => String(q.sid) === sid);
  if (i < 0) { renderSendQueue(); return; }
  const item = sendQueue.splice(i, 1)[0];
  renderSendQueue();
  sendMessage(item);
}
