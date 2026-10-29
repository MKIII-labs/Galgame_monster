'use strict';

/* ============================================================
   发送 / 流式
   ============================================================ */
/* ============================================================
   生成中的排队（Enter 不再被丢弃）
   语义：本轮回复完整结束后才逐条自动发送 —— 不打断模型，所以是「排队」，
   不是 Codex 那种 mid-turn 注入。
   ============================================================ */
const sendQueue = [];              // [{ text, sid, imgs, doc, quotes }]
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

/* 单条的附件 / 引用标记：图 2 · 文档 · 引用 1 */
function queueBadge(q) {
  const parts = [];
  const n = (q.imgs || []).length;
  if (n) parts.push(n + ' 图');
  if (q.doc) parts.push('文档');
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

  // 附件：队列里存的就是 pendingImages / pendingDoc 的同构结构，直接还原
  if ((item.imgs && item.imgs.length) || item.doc) {
    clearAttach();
    if (item.imgs && item.imgs.length) {
      state.pendingImages = item.imgs.map((it) => ({
        mime: it.mime, b64: it.b64, url: it.url, seq: it.seq,
      }));
      state.pendingImage = state.pendingImages[0];
      state.pendingDoc = null;
    } else {
      state.pendingDoc = {
        name: item.doc.name, mime: item.doc.mime,
        b64: item.doc.b64, size: item.doc.size,
      };
      state.pendingImage = null;
      state.pendingImages = [];
    }
    renderAttach();
  }
  if (item.quotes && item.quotes.length) {
    state.pendingQuotes = item.quotes.map((q) => ({ text: q.text }));
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

/* ---------- 多会话流：注册表 ----------
   每个生成中的会话一条流，DOM 包在 .stream-host 里：
   切走时 #chat 被重建，host 只是离开文档（节点与事件还在，继续收流）；
   切回来时把 host 重新挂到 #chat 末尾。 */
function streamOf(sid) {
  const key = String(sid || state.sessions.current || '');
  return state.streams[key] || null;
}

function isStreaming(sid) {
  const key = String(sid || state.sessions.current || '');
  if (streamOf(key)) return true;
  // 刷新 / 重开窗口后本地没有流对象，但后端还在跑：/api/state 的 streaming 已经记进来了。
  return !!(state.streamingSids && state.streamingSids[key]);
}

/* 该会话是否正在压缩较早历史：期间发送 / 修改一律排队（chat.js 负责判定） */
function compressBusyNow() {
  try {
    return typeof compressIsPending === 'function' && compressIsPending();
  } catch (e) {
    return false;
  }
}

function refreshStreamingUI() {
  const on = isStreaming();
  const st = streamOf();
  state.streaming = on;
  // 远端任务（刷新后没有本地流）点过停止时，保留「正在停止」直到 /api/state 确认收尾
  state.stopping = on && (state.stopping || !!(st && st.stopping));
  if (!on) state.stopping = false;
  syncSendBtn();
  // 生成中：附件按钮不隐藏（可以边生成边加附件，进排队），停止按钮并排出现
  $('#btn-stop').classList.toggle('off', !on);
  if (!on) resetStopBtn();
  const slot = document.querySelector('.swap-slot');
  if (slot) slot.classList.toggle('live', !!on && state.uiMode !== 'gal');
  const attachBtn = $('#btn-attach');
  if (attachBtn) attachBtn.classList.remove('off');
  // GAL：生成中输入区进入「等待」态 —— 只做视觉降权（仍可提前打字，不打断思路）
  const gbox = document.querySelector('.composer-box');
  if (gbox) gbox.classList.toggle('gal-waiting', !!on && state.uiMode === 'gal');
  // 提示行跟着切换：生成中 Enter 是「排队」而不是「发送」
  if (typeof cmpUpdateHint === 'function') { try { cmpUpdateHint(); } catch (e) { /* 忽略 */ } }
  // 本轮收尾后再发排队项：等一拍，让渲染与存档先落盘
  if (!on) {
    clearTimeout(_queueFlushTimer);
    _queueFlushTimer = setTimeout(flushSendQueue, 150);
  }
}

function registerStream(sid, host, row) {
  const key = String(sid);
  state.streams[key] = { sid: key, host, row, stopping: false };
  if (!state.streamingSids) state.streamingSids = {};
  state.streamingSids[key] = true;
  state.stopping = false;
  delete state.unread[key];
  renderSessions();
  refreshStreamingUI();
}

function unregisterStream(sid, keepHost) {
  const key = String(sid);
  const st = state.streams[key];
  if (st) {
    if (!keepHost && st.host && st.host.parentNode) st.host.remove();
    delete state.streams[key];
  }
  if (state.streamingSids) delete state.streamingSids[key];
  renderSessions();
  refreshStreamingUI();
}

function attachStreamHost(sid) {
  const st = streamOf(sid);
  if (!st || !st.host) return;
  if (st.host.parentNode) st.host.parentNode.removeChild(st.host);
  const chat = $('#chat');
  if (chat) chat.appendChild(st.host);
  scrollToBottom(true);
}

// 停止按钮回到默认态（可再次点击）
function resetStopBtn() {
  const b = $('#btn-stop');
  if (!b) return;
  b.disabled = false;
  b.classList.remove('stopping');
  b.title = '停止生成';
}

// 输入为空且没有待发图片时，发送按钮置灰；流式期间、录音 / 识别 / 矫正期间同样禁用
function syncSendBtn() {
  const btn = $('#btn-send');
  if (!btn) return;
  const empty = !$('#input').value.trim() && !state.pendingImage
    && !(state.pendingImages && state.pendingImages.length) && !state.pendingDoc;
  // 生成中不禁用发送：点发送 / Enter 都进队列，本轮结束后自动发出（不打断当前回复）
  btn.disabled = !!mic.active || !!mic.busy || empty;
  // 说清楚「点下去会发生什么」：原来 title 恒为「发送」，用户只能猜
  btn.title = state.streaming ? '排队发送（本轮结束后自动发出）'
            : (compressBusyNow() ? '排队发送（压缩完成后自动发出）'
            : (mic.active ? '正在录音 —— 说完再点发送'
            : (mic.busy ? '正在识别语音…'
            : (empty ? '先输入内容，或附上一张图片 / 文档' : '发送（Enter）'))));
}

async function stopStreaming() {
  const st = streamOf();
  const sidKey = String(state.sessions.current || '');
  // 刷新 / 重开窗口后本地没有流对象，但后端还在跑：也要能停。
  const remote = !st && !!(state.streamingSids && state.streamingSids[sidKey]);
  if ((!st && !remote) || (st && st.stopping)) return;
  if (st) st.stopping = true;
  state.stopping = true;
  // 立即给反馈：搜索阶段（规划/检索/抓正文）可能要一两秒才会真正收尾，
  // 不给反馈的话会被误认为「暂停键失灵」
  const b = $('#btn-stop');
  if (b) { b.disabled = true; b.classList.add('stopping'); b.title = '正在停止…'; }
  // 状态条也立刻改成「正在停止」：搜索 / 工具轮里服务端要过一会儿才真正收尾
  const liveText = st && st.row ? st.row.querySelector('.live-text') : null;
  if (liveText) liveText.textContent = '正在停止…';
  toast('正在停止…');
  // 明确点「停止」= 连排队也一起取消，避免用户以为已经停下却又自动发出去
  if (typeof cancelSendQueue === 'function') cancelSendQueue(true);
  if (state.uiMode === 'gal') galStopAudio();   // 停止生成时也打断朗读
  const sidToStop = (st && st.sid) || sidKey;
  try { await api('/api/stop', { session_id: sidToStop }); } catch (e) { }
  if (remote && typeof waitJobThenRefresh === 'function') {
    // 没有本地流可收 done：等后端确认收尾后刷新状态（手动停止，不发通知）
    waitJobThenRefresh(sidToStop).then((r) => {
      if (r && r.ok) { state.stopping = false; refreshStreamingUI(); renderMessages(); }
    }).catch(() => {});
  }
}

function hasChatKey() {
  return !!(state.config && String(state.config.chat_api_key || '').trim());
}

// 聊天模型自带搜索（目前是 DeepSeek：Search API 复用聊天密钥）也算「可联网」
function chatSearchAvailableNow() {
  const c = state.config || {};
  if (c.search_prefer_api) return false;
  return !!String(c.chat_api_key || '').trim()
    && /deepseek/i.test(String(c.chat_url || '') + ' ' + String(c.chat_model || ''));
}
function hasSearchKey() {
  return !!(state.config && String(state.config.search_api_key || '').trim())
    || chatSearchAvailableNow();
}

/* 后台提醒：窗口不在前台时，回复完成 / 中断 / 等你回答都发系统通知。
   依赖 auto.js 的 showSystemNotify / notifyOnce（脚本后加载，但调用发生在交互之后）。 */
function chatWindowAway() {
  return document.hidden || !document.hasFocus();
}

/* AI 输出终止的提醒：只在「窗口不在前台」或「这条流属于别的会话」时弹系统通知。
   文案 = 结束原因（输出完成 / API 余额不足 / 连接中断 …）；手动停止不提醒（和以前一致）。 */
const END_REASON_LABEL = {
  stopped: '已手动停止',
  budget: '达到本轮预算',
  billing: 'API 余额不足',
  network: '连接中断',
  idle: '本轮停在这里',
  empty: '本轮没有产出',
  max_rounds: '工具轮数达上限',
  length: '输出被截断',
  plan: '计划已生成',
  resume: '任务未完成，可继续',
};

function notifyChatDone(info) {
  const opts = info || {};
  const kind = String((opts.reason && opts.reason.kind) || '');
  if (opts.stopped || kind === 'stopped') return;      // 手动停止不提醒
  const bgSession = !!opts.title;
  if (!bgSession && !chatWindowAway()) return;
  if (typeof showSystemNotify !== 'function') return;
  const who = bgSession ? ('会话「' + String(opts.title).slice(0, 18) + '」') : 'AI 回复';
  const label = END_REASON_LABEL[kind] || (opts.error ? '连接中断' : '输出完成');
  // 正文优先用服务端给的原因（余额不足 / 超时 / 截断说明），其次回复摘要，最后请求异常
  const errText = opts.error ? String(opts.error.message || opts.error) : '';
  const clean = String((opts.reason && opts.reason.text) || opts.text || errText || '')
    .replace(/[#*`>|\-]/g, ' ')
    .replace(/\s+/g, ' ').trim();
  const tone = (kind === 'billing' || kind === 'network') ? 'error'
             : kind ? 'warn' : (opts.error ? 'error' : 'done');
  showSystemNotify(who + ' · ' + label, clean.slice(0, 90) || ('本轮' + label), tone);
}

function syncSearchPill() {
  const pill = $('#btn-search');
  if (!pill) return;
  const ok = hasSearchKey();
  pill.classList.toggle('active', !!state.searchEnabled && ok);
  pill.classList.toggle('disabled', !ok);
}

async function sendMessage(payload) {
  if (mic.active || mic.busy) return;   // 录音/识别/矫正未完成时不发送，避免两段文本并存
  const input = $('#input');
  // payload：字符串 / 队列对象（带 __q: true 标记）。注意 onclick 会把事件对象传进来，
  // 所以不能用「是不是对象」判断，必须认标记。
  const replay = !!payload && typeof payload === 'object' && payload.__q === true;
  const fromQueue = typeof payload === 'string' || replay;
  // /plan 规划模式：剥掉前缀，本轮只读研究 + 产出计划卡，不执行
  let planMode = !!replay && !!(payload && payload.plan);
  if (!fromQueue) {
    const raw = String(input.value || '');
    if (/^\/plan(?:\s|$)/i.test(raw.trim())) {
      planMode = true;
      input.value = raw.trim().replace(/^\/plan\s*/i, '');
      autoGrow();
      toast('规划模式：本轮只读，先出里程碑计划');
    }
  }
  // 排队回放的「修改历史消息」：压缩结束后由 flushSendQueue 重新走 resend
  if (replay && payload.__edit) {
    if (isStreaming() || compressBusyNow()) {
      sendQueue.unshift(payload);
      renderSendQueue();
      return;
    }
    await resend({ mode: 'edit', index: payload.__edit.index,
                   content: payload.__edit.content });
    return;
  }
  const text = String(
    (replay ? payload.text : (fromQueue ? payload : input.value)) || '').trim();
  const imgs = fromQueue
    ? (replay ? (payload.imgs || []) : [])
    : ((state.pendingImages && state.pendingImages.length)
      ? state.pendingImages.slice()
      : (state.pendingImage ? [state.pendingImage] : []));
  const img = imgs[0] || null;
  const doc = fromQueue ? (replay ? (payload.doc || null) : null) : state.pendingDoc;
  const quotes = fromQueue
    ? (replay ? (payload.quotes || []) : [])
    : (state.pendingQuotes || []);
  // 正在修改历史消息：文本已经在输入框里，发送时走原有 edit 语义
  // （丢弃该条之后的回复并重新生成，保留版本链），不发成一条新消息
  if (editTarget && !fromQueue
      && editTarget.sid === String(state.sessions.current || '')) {
    if (state.streaming) { toast('正在生成回复，稍后再发送这次修改', 'error'); return; }
    if (!text) return;
    if (!hasChatKey()) { toast('请先在「设置 → 模型接入」中填写密钥', 'error'); return; }
    const idx = editTarget.index;
    const s0 = curSession();
    const m0 = s0 && s0.messages[idx];
    if (!m0 || m0.role !== 'user') {
      cancelEditTarget(false);
      toast('这条消息已经变化，请重新点「修改」', 'error');
      return;
    }
    cancelEditTarget(false);
    input.value = '';
    autoGrow();
    if (typeof cmpClearPrecheck === 'function') cmpClearPrecheck();
    hideSttChip();
    // 正在压缩较早历史：修改先排队，压缩完成后自动发出（避免摘要在中途被改乱）
    if (compressBusyNow()) {
      sendQueue.push({
        __q: true,
        text: text,
        sid: state.sessions.current,
        __edit: { index: idx, content: text },
      });
      renderSendQueue();
      toast('正在压缩较早的历史，这条修改已排队，完成后自动发送');
      return;
    }
    await resend({ mode: 'edit', index: idx, content: text });
    return;
  }
  // 生成中 / 压缩中：不再丢弃 —— 文本 / 图片 / 文档 / 引用全部进队列，空闲后自动发送
  if (state.streaming || compressBusyNow()) {
    if (!text && !imgs.length && !doc && !quotes.length && !planMode) return;
    sendQueue.push({
      __q: true,
      text: text,
      sid: state.sessions.current,
      plan: planMode,
      // 只存引用，不复制 base64 内容；结构对齐 pendingImages，回放时可直接复用
      imgs: imgs.map((it) => ({ mime: it.mime, b64: it.b64, url: it.url, seq: it.seq })),
      doc: doc ? { name: doc.name, mime: doc.mime, b64: doc.b64, size: doc.size } : null,
      quotes: quotes.map((q) => ({ text: q.text })),
    });
    input.value = '';
    autoGrow();
    clearAttach();
    clearQuotes();
    if (typeof cmpClearPrecheck === 'function') cmpClearPrecheck();
    if (typeof cmpUpdateHint === 'function') cmpUpdateHint();
    if (typeof cmpUpdateClear === 'function') cmpUpdateClear();
    renderSendQueue();
    toast(state.streaming ? '已排队，本轮回复结束后自动发送'
                          : '正在压缩较早的历史，已排队，完成后自动发送');
    return;
  }
  if (!text && !imgs.length && !doc && !quotes.length && !planMode) return;   // 允许「纯图片 / 纯文档 / 纯引用」
  if (!hasChatKey()) { toast('请先在「设置 → 模型接入」中填写密钥', 'error'); return; }
  // 首次发送时请求系统通知权限（必须在用户手势里）；拒绝也不影响发送。
  // 同时预热提示音：浏览器只允许在有用户手势的上下文里之后放音。
  if (typeof ensureNotifyPermission === 'function') ensureNotifyPermission();
  if (typeof warmNotifySound === 'function') warmNotifySound();
  if (!fromQueue) {
    input.value = '';
    autoGrow();
    if (typeof cmpClearPrecheck === 'function') cmpClearPrecheck();
    hideSttChip();
    hideEmpty();
  }
  // 发新消息前，把还处于「预览」的用户消息恢复为当前版本
  if (verPreview && verPreview.sid === state.sessions.current) refreshUserRow(verPreview.index);
  verPreview = null;
  const extra = { content: text };
  if (planMode) extra.plan_mode = true;
  if (state.uiMode === 'gal') {
    extra.mode = 'gal';
    extra.gal_character_id = galState.charId || (curSession() || {}).gal_character_id || '';
    galSetUserEcho(text || (doc ? '【附件：' + doc.name + '】'
      : (imgs.length > 1 ? '【' + imgs.length + ' 张图片】' : '【图片】')));
    galResetDialog();
    galSetStatus('思考中…');          // 必须放在 galResetDialog 之后，否则会被清掉
    galPlayIntake();                  // 200ms 内的「说出口」确认：对白框轻吸一下
  }
  if (imgs.length) extra.images = imgs.map((it) => ({ mime: it.mime, data: it.b64 }));
  if (img) extra.image = { mime: img.mime, data: img.b64 };   // 兼容只认 image 的旧客户端
  if (doc) extra.doc = { name: doc.name, mime: doc.mime, data: doc.b64 };
  if (quotes.length) extra.quotes = quotes.map((q) => ({ text: q.text }));
  const echo = {};
  if (imgs.length) {
    echo.images = imgs.map((it) => ({ url: it.url, size: Math.round(it.b64.length * 3 / 4) }));
  }
  if (doc) echo.doc = { name: doc.name, size: doc.size };
  appendUser(text || (planMode ? '（规划当前任务）'
    : (doc ? '【附件：' + doc.name + '】'
    : (imgs.length > 1 ? '【' + imgs.length + ' 张图片】' : '【图片】'))),
             curSession() ? curSession().messages.length : undefined,
             (imgs.length || doc) ? echo : undefined);
  if (!fromQueue) {
    clearAttach();
    clearQuotes();
  }
  await streamChat(extra);
}

/* 当前界面上下文：只上报白名单字段，让助手知道用户正在看哪个模块。
   hallCurrent / stockState 都是其他脚本的顶层声明；用 typeof 兜底，避免加载顺序问题。 */
function currentUiContext() {
  try {
    if (typeof hallCurrent === 'undefined' || !hallCurrent) return null;
    const ctx = { mode: 'hall', module: String(hallCurrent.id || '') };
    if (ctx.module === 'stock' && typeof stockState !== 'undefined' && stockState.game) {
      ctx.game_id = stockState.game.id || '';
      ctx.symbol = stockState.symbol || '';
      ctx.cursor_date = stockState.game.cursor_date || '';
    }
    return ctx;
  } catch (e) {
    return null;
  }
}

async function streamChat(extra, resume) {
  // 本条流归属的会话：之后所有 DOM / 滚动 / 停止都按它判断，
  // 用户切到别的会话不影响这一条继续跑。
  const sid = String(state.sessions.current || '');
  const isCur = () => String(state.sessions.current || '') === sid;
  // 局部遮蔽全局 scrollToBottom：后台会话收流时绝不拽动用户正在看的窗口
  const scrollToBottom = (force) => { if (isCur()) window.scrollToBottom(force); };
  // AI 占位：先摆一组「呼吸点」表示正在生成，首个 token 到达即换成正文
  // resume：续写模式——复用上一条被打断的气泡，把已有文本当流式起点
  let row;
  let base = '';
  if (resume && resume.row) {
    row = resume.row;
    base = resume.base || '';
    // 续写：把旧的「多段正文 / 提问卡」并回当前段，否则收尾重排会留下重复内容
    (row._secs || []).slice(1).forEach((n) => n.remove());
    (row._asks || []).forEach((n) => n.remove());
    row._secs = [row._content];
    row._asks = [];
    row._content.style.display = '';
    row._content.textContent = base;              // 清掉旧的 markdown 渲染，改为纯文本起点
    row._raw = base;
    const oldBar = row._wrap.querySelector('.msg-actions');
    if (oldBar) oldBar.remove();                  // 移除旧操作条，流结束后统一重建
    row._usageEl = row._usageEl || row._wrap.querySelector('.usage-line') || null;
  } else {
    row = appendAi('', '', undefined);
  }
  // 把这一轮**整条消息**装进 host：切走时 #chat 会被重建，host 只是离开文档，节点与事件都还在。
  // 必须包住 row 本身而不是 row._wrap —— 否则切回来时 attachStreamHost 会把 host 移到 #chat 下，
  // .content-wrap 脱离 .msg，max-width / 居中 / 内边距全部失效（表现为内容贴满两侧）。
  const host = document.createElement('div');
  host.className = 'stream-host';
  if (row.parentNode) {
    row.parentNode.insertBefore(host, row);
    host.appendChild(row);
  }
  registerStream(sid, host, row);
  const stRef = state.streams[sid];
  let c = row._content;
  // 正文分段：提问卡插在两次正文之间，后续输出另起一段，不再堆到消息末尾
  row._secs = [c];
  row._asks = [];
  c.style.whiteSpace = 'pre-wrap';
  // 实时状态条：左侧 3×3 网格脉冲图标 + 当前动作 + 已用秒数。
  // 目的：模型思考 / 跑工具时界面不能只有闪烁光标，否则用户分不清「在跑」和「卡死」。
  const typing = document.createElement('span');
  typing.className = 'live-bar';
  // 5×5 点阵，去掉四个角 → 21 个点（占位格子保留，保证网格对齐）
  const _cells = [];
  for (let _r = 0; _r < 5; _r++) {
    for (let _c = 0; _c < 5; _c++) {
      const _corner = (_r === 0 || _r === 4) && (_c === 0 || _c === 4);
      _cells.push(_corner ? '<i class="off"></i>' : '<i></i>');
    }
  }
  typing.innerHTML = '<span class="live-grid">' + _cells.join('') + '</span>'
                   + '<span class="live-text">正在发送…</span>'
                   + '<span class="live-sec"></span>';
  Array.prototype.forEach.call(typing.querySelectorAll('.live-grid i'), (el, i) => {
    if (el.classList.contains('off')) return;
    // 中心扩散：中心点先亮，按「到中心的曼哈顿距离」逐圈向外推进
    const _r = Math.floor(i / 5);
    const _c = i % 5;
    const _d = Math.abs(_r - 2) + Math.abs(_c - 2);
    el.style.animationDelay = (_d * 0.13).toFixed(2) + 's';
  });
  // 放在 content 外层：正文每次重绘都会清空 c.textContent，放里面会被冲掉
  row._wrap.appendChild(typing);
  const keepLiveLast = () => { if (row._wrap) row._wrap.appendChild(typing); };
  // 探索小队（只读子代理）：独立小卡片，放在正文外层，不会被正文重绘冲掉
  let subagentBox = null;
  const subagentRows = {};
  const ensureSubagentBox = () => {
    if (subagentBox) return subagentBox;
    if (state.uiMode === 'gal' || !isCur()) return null;
    const box = document.createElement('div');
    box.className = 'subagent-card';
    box.innerHTML = '<div class="subagent-head">'
      + '<span class="subagent-title">探索小队</span>'
      + '<span class="subagent-sum"></span></div>'
      + '<div class="subagent-body"></div>';
    row._wrap.insertBefore(box, typing);
    subagentBox = box;
    return box;
  };
  const subagentRow = (id) => {
    const box = ensureSubagentBox();
    if (!box) return null;
    if (!subagentRows[id]) {
      const r = document.createElement('div');
      r.className = 'subagent-row';
      r.innerHTML = '<span class="subagent-ico">⋯</span>'
        + '<span class="st"></span><span class="sm"></span>';
      box.querySelector('.subagent-body').appendChild(r);
      subagentRows[id] = r;
    }
    return subagentRows[id];
  };
  const liveText = typing.querySelector('.live-text');
  const liveSec = typing.querySelector('.live-sec');
  const setLive = (t) => {
    if (liveText && t && liveText.textContent !== t) liveText.textContent = t;
  };
  const setLiveSec = (s) => {
    if (liveSec) liveSec.textContent = s ? String(s) : '';
  };
  if (state.uiMode === 'gal') typing.style.display = 'none';   // GAL 有独立状态行
  const cur = document.createElement('span');
  cur.className = 'cursor';

  if (state.uiMode === 'gal') { galSetStatus('思考中…'); galSetTyping(false); galState.skip = false; galState.streamedTurn = false; }   // 新一轮：解除上轮的「跳过朗读」
  let acc = base;
  let serverFinalText = null;   // 服务端收尾校正后的正文（摘掉裸工具 JSON / 伪标签）
  let textNode = null;          // 流式期间只往这个节点追加，避免每个 chunk 重写整段
  // —— 流式实时 Markdown（NOTE 43.96）：定稿区 + 尾巴区 ——
  // 定稿区只追加已完整的块，永不重建（富媒体 / 代码高亮不会重跑）；
  // 尾巴区是当前没写完的一段，节流重渲染。GAL 有自己的逐段渲染，不启用。
  const liveEnabled = state.uiMode !== 'gal';
  let liveBox = null;           // .md-live：已定稿的块
  let tailBox = null;           // .md-tail：当前未完成的一段
  let liveRendered = 0;         // acc 中已推进定稿区的长度（永远落在行首）
  let paintTimer = 0;
  let lastPaintAt = 0;
  const PAINT_MS = 90;              // 重绘节流：最多约 11 次/秒
  const TAIL_RENDER_MAX = 4000;     // 尾巴超过这个长度就不逐帧渲染（避免长代码块反复高亮）
  let thinkBox = null;
  let flow = null;              // 过程流：思考块 + 工具组按时间线排在这里
  let flowWrap = null;          // 外层折叠容器（头 + body）
  let flowToggled = false;      // 用户手动展开 / 收起过过程
  let toolPending = null;       // 「正在生成工具调用参数」的轻量行
  let sourcesBox = null;
  let memNote = null;
  let gotDone = false;          // 是否收到 done 事件（收到就不必在收尾再兜底拉一次 /api/state）
  let doneBlocks = null;        // 服务端落库后的块：流结束时按它重绘一次（V3）
  let doneTiming = null;
  let doneTools = null;
  let streamError = null;       // 流式请求异常（用于后台通知）
  let endReason = null;         // 非正常结束时服务端给的原因（长度/网络/轮数/手动停止…）
  let sawError = false;         // 收到过 error 事件：此时不再接受 replace 覆盖
  let askedNotified = false;    // 本轮是否已提醒过「等你回答」
  // 等待计时：模型思考 / 工具执行期间可能几十秒没有任何正文，
  // 界面必须给出秒数，否则用户分不清「在跑」与「卡死」（见 NOTE 43.89）。
  let phase = 'idle';           // idle | think | tool | output
  let phaseAt = Date.now();     // 当前阶段起点
  let retryWait = null;         // 自动重试的退避倒计时（{until}）；到点后由下一次事件接管
  const streamStart = Date.now();   // 本轮实际开始时刻：过程折叠头显示「工作 X 秒」
  let lastEventAt = Date.now(); // 最后收到任何 SSE 事件（含 tick）的时间
  let lastContentAt = Date.now(); // 最后收到非 tick 事件（步骤/思考/正文/工具）
  let staleShown = false;       // 「15s 连事件都没有」提示是否已显示
  let contentStaleShown = false; // 「15s 没有思考/正文」提示是否已显示
  let waitingApproval = false;  // 正在等用户点「允许 / 拒绝」：不算模型空转
  let recovering = false;       // 正在自动 attach / 补状态
  let recovered = false;        // 已经由 attach 补回结果（原流收尾不要再覆盖）
  let lastRecoverAt = 0;        // 上次自动重连时间：失败后 30 秒再试，别每秒打一次

  // 后台标签页被冻结时定时器会停摆：切回前台先重置计时基准，
  // 否则会把「刚才没在计时」误判成「连接断了」，一回来就闪一次中断提示。
  const onVisible = () => {
    if (document.hidden || recovered) return;
    lastEventAt = Date.now();
    lastContentAt = Date.now();
    staleShown = false;
    contentStaleShown = false;
  };

  // 思考标题节流：流式时最多每 150ms 更新一次，避免逐 token 抖动
  let thinkLabelAt = 0;
  const refreshThinkLabel = (box, settled) => {
    if (!box) return;
    const now = Date.now();
    if (!settled && now - thinkLabelAt < 150) return;
    thinkLabelAt = now;
    const head = thinkLineSummary(box._body.textContent, settled);
    const step = box._step || 1;
    box._hasSummary = !!head;      // 有摘要后，每秒计时器不再把标题改回「思考中… Ns」
    box._label.textContent = '步骤 ' + step + (head ? ' · ' + head : ' · 思考中');
    // V4：状态条不只说“思考中”，而是「当前步骤 + 最后一小段思考」
    if (head) setLive('第 ' + step + ' 步 · ' + clipText(head, 60));
  };

  // 过程流：思考块与工具组按时间顺序排在这里，正文固定在它下面。
  // 外面再包一层折叠容器：流式时展开，done 后折成「过程 · 工作 X 秒」。
  const ensureFlow = () => {
    if (flow) return flow;
    // 续写（同一行再次 streamChat）：复用上一段的过程折叠，别叠成两条
    if (row._flowWrap && row._flow && row._flowWrap.isConnected) {
      flowWrap = row._flowWrap;
      flow = row._flow;
      flowWrap.classList.remove('collapsed');
      row._flowLabel.textContent = '过程 · 进行中';
      row._flowCaret.textContent = '▾';
      row._flowHead.setAttribute('aria-expanded', 'true');
      flowToggled = false;
      return flow;
    }
    flowWrap = document.createElement('div');
    flowWrap.className = 'process-fold';
    const head = document.createElement('div');
    head.className = 'process-fold-head';
    const title = document.createElement('span');
    title.className = 'process-fold-title';
    title.textContent = '过程 · 进行中';
    const caret = document.createElement('span');
    caret.className = 'process-fold-caret';
    caret.textContent = '▾';
    head.appendChild(title);
    head.appendChild(caret);
    const body = document.createElement('div');
    body.className = 'chat-flow process-fold-body';
    flowWrap.appendChild(head);
    flowWrap.appendChild(body);
    row._wrap.insertBefore(flowWrap, row._content);
    row._flowWrap = flowWrap;
    row._flowHead = head;
    row._flowLabel = title;
    row._flowCaret = caret;
    const toggle = () => {
      flowToggled = true;
      const open = flowWrap.classList.contains('collapsed');
      flowWrap.classList.toggle('collapsed', !open);
      caret.textContent = open ? '▾' : '▸';
      head.setAttribute('aria-expanded', open ? 'true' : 'false');
    };
    head.onclick = toggle;
    makeClickable(head, toggle, '展开 / 收起过程');
    head.setAttribute('aria-expanded', 'true');
    flow = body;
    row._flow = body;
    return flow;
  };

  const clearToolPending = () => {
    if (toolPending) {
      toolPending.remove();
      toolPending = null;
    }
  };

  const fmtBytes = (n) => {
    n = Number(n) || 0;
    if (n < 1024) return n + ' B';
    if (n < 1048576) return Math.round(n / 1024) + ' KB';
    return (Math.round(n / 1048576 * 10) / 10) + ' MB';
  };

  // 本轮结束：过程折成一行；用户手动展开过就保持原样。
  const foldLiveProcess = () => {
    if (!flowWrap) return;
    if (!flow || !flow.childNodes.length) {
      flowWrap.remove();
      flowWrap = null;
      flow = null;
      row._flowWrap = null;
      row._flow = null;
      row._flowHead = null;
      row._flowLabel = null;
      row._flowCaret = null;
      return;
    }
    const kind = (endReason && endReason.kind) || '';
    if (kind === 'stopped') row._flowLabel.textContent = '过程 · 已停止';
    else if (kind === 'network' || kind === 'server' || kind === 'error') {
      row._flowLabel.textContent = '过程 · 中断';
    } else {
      row._flowLabel.textContent = '过程 · 工作 ' + workDuration(Date.now() - streamStart);
    }
    if (!flowToggled) {
      flowWrap.classList.add('collapsed');
      row._flowCaret.textContent = '▸';
      if (row._flowHead) row._flowHead.setAttribute('aria-expanded', 'false');
    }
  };

  // 收尾当前思考块：空的直接去掉，别留一个空壳占位；
  // 非空的标题换成一行摘要（点开还能看全文）。
  const closeThink = (label) => {
    if (!thinkBox) return;
    const text = thinkBox._body.textContent || '';
    if (!text.trim()) thinkBox.remove();
    else {
      thinkBox.classList.remove('open');
      const head = thinkLineSummary(text, true);
      const step = thinkBox._step || 1;
      thinkBox._hasSummary = !!head;
      thinkBox._label.textContent = head
        ? ('步骤 ' + step + ' · ' + head)
        : (label || ('步骤 ' + step + ' · 已思考'));
    }
    thinkBox = null;
  };

  // 把正文容器切成两块：liveBox（已定稿，只追加）+ tailBox（未完成，节流重渲染）
  const ensureLive = () => {
    if (liveBox) return;
    c.style.whiteSpace = '';      // 块级渲染不再需要保留源码换行
    c.textContent = '';           // 清掉呼吸点 / 续写模式的纯文本起点
    liveBox = document.createElement('div');
    liveBox.className = 'md-live';
    tailBox = document.createElement('div');
    tailBox.className = 'md-tail';
    c.appendChild(liveBox);
    c.appendChild(tailBox);
    c.appendChild(cur);           // 光标始终停在最后一个块之后
    textNode = null;
  };

  // 从 from 往后找最后一个可定稿的位置：围栏外的空行，或刚闭合的围栏行。
  // 只在围栏外切，定稿区里的 Markdown 才不会被切成半截。
  const scanLive = (text, from) => {
    let i = from, fence = false, cut = from;
    while (i < text.length) {
      const nl = text.indexOf('\n', i);
      const line = nl < 0 ? text.slice(i) : text.slice(i, nl);
      if (/^\s{0,3}(?:```|~~~)/.test(line)) {
        fence = !fence;
        if (!fence && nl >= 0) cut = nl + 1;   // 围栏刚闭合：这一块已经完整，可以定稿
        if (nl < 0) break;
        i = nl + 1;
        continue;
      }
      if (!fence && nl >= 0 && !line.trim()) cut = nl + 1;
      if (nl < 0) break;
      i = nl + 1;
    }
    return { cut, fence };
  };

  // 流式时把「可能是协议标记的前半截」压在缓冲里：
  // 模型吐 `<tool calls>` 或 ```tool 是分片到达的，直接上屏会先闪出 `<tool cal` / ```to
  // 再被收尾替换掉。这里只压很短的尾巴（<64 字符），一旦出现 `>` 或不像标记就立刻放行。
  const holdBackPartialMarker = (tail) => {
    let cut = tail.length;
    const lt = tail.lastIndexOf('<');
    if (lt >= 0 && tail.indexOf('>', lt) < 0) {
      const frag = tail.slice(lt);
      if (frag.length < 64
          && /^<\/?\s*[A-Za-z][\w:.\-\u4e00-\u9fff]*(\s[^<>]*)?$/.test(frag)
          && /tool|parameter|invoke|function_calls|thinking|antml/i.test(frag)) {
        cut = lt;
      }
    }
    const m = /(^|\n)([ \t]*)(`{1,3}|~{1,3})([A-Za-z+-]*)$/.exec(tail.slice(0, cut));
    if (m) {
      const w = (m[4] || '').toLowerCase();
      const prefixes = ['t', 'to', 'too', 'tool', 'tool_', 'tool_c', 'tool_ca',
        'tool_cal', 'tool_call', 'tool_calls', 'j', 'js', 'jso', 'json'];
      if (prefixes.some((x) => x.startsWith(w))) cut = m.index + m[1].length;
    }
    return tail.slice(0, cut);
  };

  const paintLive = () => {
    paintTimer = 0;
    if (!liveBox) return;
    lastPaintAt = Date.now();
    // 用跟随状态而不是当前高度：这一帧内容已经长高了，再算 isAtBottom 会误判成「用户在看历史」
    const wasAtBottom = isCur() && _followBottom;
    const { cut, fence } = scanLive(acc, liveRendered);
    let next = cut;
    // 尾巴太长说明模型卡在一个超长段落 / 未闭合代码块里：
    // 围栏外就整段推进定稿区（只切到最后一行，半行留在尾巴），
    // 围栏内则退回纯文本，避免每帧高亮几千行代码。
    if (acc.length - next > TAIL_RENDER_MAX && !fence) {
      const nl = acc.lastIndexOf('\n');
      if (nl >= next) next = nl + 1;
    }
    if (next > liveRendered) {
      const html = safeRenderMarkdown(acc.slice(liveRendered, next));
      if (html) {
        liveBox.insertAdjacentHTML('beforeend', html);
        if (window.enhanceRichMedia) window.enhanceRichMedia(liveBox);
      }
      liveRendered = next;
    }
    const tail = holdBackPartialMarker(acc.slice(liveRendered));
    if (tail.length > TAIL_RENDER_MAX) {
      tailBox.className = 'md-tail plain';
      tailBox.textContent = tail;
    } else {
      tailBox.className = 'md-tail';
      tailBox.innerHTML = safeRenderMarkdown(tail);
      if (window.enhanceRichMedia) window.enhanceRichMedia(tailBox);
    }
    // 输出阶段把「已经写了多少字」也带上，让用户看到确实在推进
    if (phase === 'output') setLive('正在输出 · ' + acc.length + ' 字');
    if (wasAtBottom) scrollToBottom(true);
  };

  const schedulePaint = () => {
    if (paintTimer) return;
    const wait = Math.max(0, PAINT_MS - (Date.now() - lastPaintAt));
    paintTimer = setTimeout(paintLive, wait);
  };

  // 增量写入正文（首次调用时把呼吸点换成文本节点）
  const pushText = (t) => {
    keepLiveLast();                 // 状态条常驻在正文下方，直到本轮结束
    const tip = c.querySelector('.recognize-tip');
    if (tip) tip.remove();
    if (!liveEnabled) {
      if (!textNode) {
        textNode = document.createTextNode('');
        c.insertBefore(textNode, cur.parentNode ? cur : null);
        c.appendChild(cur);       // 光标始终跟在文本末尾
      }
      textNode.appendData(t);
      return;
    }
    ensureLive();
    schedulePaint();             // acc 已累加，这里只触发重绘
  };

  // 提问卡落在「真正被问出来」的位置：先把当前正文段收尾并上屏，卡片插在它后面，
  // 之后的内容另起一段正文 —— 否则卡片会被后续输出顶到消息最下方（历史重排也一样）。
  const splitAtAsk = (card) => {
    if (paintTimer) { clearTimeout(paintTimer); paintTimer = 0; }
    if (liveEnabled && liveBox) paintLive();          // 尾巴先落屏，别丢字
    if (!String(c.textContent || '').trim()) c.style.display = 'none';   // 空段不占位
    const anchor = typing.parentNode === row._wrap ? typing : null;
    row._wrap.insertBefore(card, anchor);
    const next = document.createElement('div');
    next.className = 'content';
    next.style.whiteSpace = 'pre-wrap';
    row._wrap.insertBefore(next, anchor);
    c = next;
    row._content = c;
    row._secs.push(c);
    row._asks.push(card);
    liveBox = null;              // 新段重新建定稿区 / 尾巴区
    tailBox = null;
    textNode = null;
    liveRendered = acc.length;   // 新段只渲染这之后的内容
    c.appendChild(cur);          // 光标跟到新段
  };

  // 每秒刷新等待时长。后端每 5 秒一个 tick，所以「15 秒没有任何事件」
  // 基本只可能是通道断了 —— 这是「慢」与「断」唯一能区分开的信号。
  const waiter = setInterval(() => {
    if (!isCur()) return;                    // 后台会话不折腾 DOM
    const now = Date.now();
    // 自动重试的退避期：显示倒计时，别让「上游空转 15s」的提示抢在前面
    if (retryWait) {
      const left = Math.ceil((retryWait.until - now) / 1000);
      if (left > 0) {
        setLiveSec('等待 ' + left + 's');
        return;
      }
      retryWait = null;
    }
    const idle = now - lastEventAt;
    if (idle > 15000) {
      const sec = Math.round(idle / 1000);
      if (!staleShown) {
        staleShown = true;
        setLive('连接中断，正在自动重连');
        if (thinkBox) thinkBox._label.textContent = '连接中断，正在自动重连…';
        // 不再让你手动停止：挂到 /api/chat/attach 等任务结束并补状态
        if (!recovering && now - lastRecoverAt > 30000) {
          recovering = true;
          lastRecoverAt = now;
          waitJobThenRefresh(sid)
            .then((r) => { if (r && r.ok) finishRecovered(r); })
            .catch(() => {})
            .finally(() => { recovering = false; });
        }
      }
      // 计时必须继续走，不能冻在第一次提示的 16s
      setLiveSec(sec + 's');
      if (thinkBox && !thinkBox._hasSummary) {
        thinkBox._label.textContent = '连接中断，正在自动重连… 已 ' + sec + 's';
      }
      return;
    }
    staleShown = false;
    // 连接活着（tick 在来），但一直没有思考 / 正文 / 工具：这才是「上游空转」。
    // 以前 15 秒提示用的是 lastEventAt，tick 每 5 秒续命，所以永远不触发。
    const contentIdle = now - lastContentAt;
    // 工具调用参数正在生成：状态条已经显示工具名 + 大小；超过 15s 没新分片再提示
    if (phase === 'toolgen') {
      if (contentIdle > 15000) {
        setLive('工具调用生成中');
        setLiveSec(Math.round(contentIdle / 1000) + 's');
      }
      return;
    }
    // 工具正在跑 / 等用户审批时不算「模型没产出」
    if (phase !== 'tool' && !waitingApproval && contentIdle > 15000) {
      if (!contentStaleShown) {
        contentStaleShown = true;
        setLive('模型尚未产出思考或正文');
      }
      setLiveSec(Math.round(contentIdle / 1000) + 's');
      if (thinkBox && !thinkBox._hasSummary) {
        thinkBox._label.textContent = '等待模型产出… ' + Math.round(contentIdle / 1000) + 's';
      }
      return;
    }
    contentStaleShown = false;
    if (phase !== 'think' && phase !== 'tool') { setLiveSec(''); return; }
    const sec = Math.max(1, Math.round((now - phaseAt) / 1000));
    setLiveSec(sec + 's');
    if (phase === 'think' && thinkBox && !thinkBox._hasSummary) {
      // 还没有思考摘要时才显示「思考中… Ns」；有摘要后标题保持摘要，秒数在状态条里
      thinkBox._label.textContent = '步骤 ' + (thinkBox._step || 1) + ' · 思考中… ' + sec + 's';
    } else if (phase === 'tool' && row._toolGroup) {
      const t = row._toolGroup.el.querySelector('.tool-group-title');
      if (t) t.textContent = (row._toolGroup.el._titleBase || '') + ' · 已 ' + sec + 's';
    }
  }, 1000);
  // 计时器与前台监听一起收：流结束（done / 异常 / 恢复）后不再重复判定
  const stopWaiter = () => {
    clearInterval(waiter);
    document.removeEventListener('visibilitychange', onVisible);
  };
  document.addEventListener('visibilitychange', onVisible);

  // attach 补回结果后：停掉等待计时，恢复输入框 / 侧栏，别再让原流收尾覆盖
  const finishRecovered = (info) => {
    recovered = true;
    gotDone = true;
    stopWaiter();
    clearToolPending();
    if (typing.parentNode) typing.remove();
    if (cur.parentNode) cur.remove();
    unregisterStream(sid, false);
    setLive('');
    setLiveSec('');
    renderSessions();
    scrollToBottom();
    // 原流已经断了，这里补回结果：也算一次终止，按服务端回放的结束原因提醒
    // （窗口在后台 / 别的会话才弹；手动停止不提醒）
    const recReason = (info && info.reason) || null;
    const cur0 = String(state.sessions.current || '') === String(sid);
    const s0 = state.sessions.sessions.find((x) => x.id === sid) || null;
    notifyChatDone({
      text: '', error: null, stopped: false, reason: recReason,
      title: cur0 ? '' : ((s0 && s0.title) || '新对话'),
    });
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
        ui_context: currentUiContext() || undefined,
      }, extra || {})),
    });
    if (!res.ok) {
      let msg = 'HTTP ' + res.status;
      try {
        const j = await res.json();
        if (j && j.message) msg = j.message;
        else if (j && j.error === 'busy') msg = '该会话正在生成中，请稍候。';
      } catch (e) { /* 非 JSON 响应，忽略 */ }
      throw new Error(msg);
    }
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
        lastEventAt = Date.now();          // 任何事件都算「后端还活着」
        if (obj.type !== 'tick') {
          lastContentAt = Date.now();      // tick 不算内容：空心跳不能掩盖「没有产出」
          contentStaleShown = false;
          waitingApproval = false;
        }
        if (obj.type === 'tick') {
          // 后端存活心跳（每 5 秒一条）：只用来刷新 lastEventAt，不渲染
        } else if (obj.type === 'recalling') {
          // 记忆检索（装配阶段，通常 <200ms；检索池大或模型挑选时可能几秒）
          if (!row._recallTip) {
            row._recallTip = document.createElement('span');
            row._recallTip.className = 'recognize-tip';
            row._recallTip.textContent = '正在检索记忆…';
            c.appendChild(row._recallTip);
          }
        } else if (obj.type === 'doc_parsing') {
          const chip = lastUserDocChip();
          if (chip) {
            chip.dataset.name = obj.name || '';
            chip.innerHTML = icon('attach') + ' ' + escapeHtml(obj.name || '附件') + '（正在解析…）';
          } else if (state.uiMode === 'gal') {
            galSetStatus('正在解析附件…');
          }
          scrollToBottom();
        } else if (obj.type === 'doc_ok') {
          const chip = lastUserDocChip();
          if (chip) {
            chip.innerHTML = icon('attach') + ' ' + escapeHtml(obj.name || '附件') + '（' + (obj.chars || 0) + ' 字）';
            chip.title = obj.warn || '';
            chip.classList.toggle('warn', !!obj.warn);
          }
          if (obj.warn) toast('附件已读入：' + obj.warn);
          scrollToBottom();
        } else if (obj.type === 'recognizing') {
          const tip = document.createElement('span');
          tip.className = 'recognize-tip';
          tip.textContent = obj.mode === 'vision' ? '📷 正在识别图片内容…' : '📷 正在看图…';
          // 注意：typing 是 row._wrap 的子节点，不在 c 里；以前 c.insertBefore(tip, typing)
          // 会直接抛 NotFoundError，把整条流打断 —— 表现为发完图片后界面静默、只能切会话重看。
          c.appendChild(tip);
          scrollToBottom();
        } else if (obj.type === 'searching') {
          if (!sourcesBox) {
            sourcesBox = buildSearchBox();
            row._wrap.insertBefore(sourcesBox, row._wrap.firstChild);
          }
          if (sourcesBox._planLine) sourcesBox._planLine.textContent = '已判断：需要联网';
          else searchStep(sourcesBox, '已判断：需要联网');
          if (state.uiMode === 'gal') { galSetStatus('查阅中…'); galExecLine('查阅资料…'); }
          scrollToBottom();
        } else if (obj.type === 'search_queries') {
          if (!sourcesBox) {
            sourcesBox = buildSearchBox();
            row._wrap.insertBefore(sourcesBox, row._wrap.firstChild);
          }
          const qs = (obj.queries || []).map((s) => String(s));
          searchStep(sourcesBox, '已生成 ' + qs.length + ' 个检索式：' + qs.join(' / '));
          if (state.uiMode === 'gal') galExecLine('在回想相关的事…');
        } else if (obj.type === 'search_hits') {
          if (sourcesBox) searchStep(sourcesBox, '命中 ' + obj.count + ' 条来源');
        } else if (obj.type === 'search_fetching') {
          if (sourcesBox) sourcesBox._fetchLine = searchStep(sourcesBox, '读取网页 0/' + obj.total + '…');
        } else if (obj.type === 'search_read') {
          if (sourcesBox) {
            if (sourcesBox._fetchLine) {
              sourcesBox._fetchLine.textContent = '读取网页 ' + obj.done + '/' + obj.total;
            }
            searchStep(sourcesBox,
              (obj.ok ? '✓ ' : '⚠ ') + (obj.title || obj.url) +
              (obj.ok ? '' : '（正文抓取失败，改用摘要）'),
              obj.ok ? '' : 'warn');
          }
        } else if (obj.type === 'search_reuse') {
          if (!sourcesBox) {
            sourcesBox = buildSearchBox();
            row._wrap.insertBefore(sourcesBox, row._wrap.firstChild);
          }
          searchStep(sourcesBox, '复用上一轮已有的来源，未重复联网');
          appendSearchSources(sourcesBox, obj.items);
          if (state.uiMode === 'gal') {
            galExecLine('沿用先前的资料', 'ok');
            galSetStatus('思考中…');
          }
          scrollToBottom();
        } else if (obj.type === 'sources') {
          if (!sourcesBox) {
            sourcesBox = buildSearchBox();
            row._wrap.insertBefore(sourcesBox, row._wrap.firstChild);
          }
          appendSearchSources(sourcesBox, obj.items);
          if (state.uiMode === 'gal') {
            galExecLine('已留意到一些细节', 'ok');
            galSetStatus('思考中…');
          }
          scrollToBottom();
        } else if (obj.type === 'search_error') {
          // 同一轮里可能已经搜过一两次：只加一行提示，别把整张卡片（含前面搜到的来源）删掉
          if (sourcesBox) {
            searchStep(sourcesBox, '⚠ ' + obj.message, 'warn');
            if (state.uiMode === 'gal') galExecLine('暂时没能查到相关信息', 'err');
            scrollToBottom();
          } else {
            const note = document.createElement('div');
            note.className = 'search-note';
            note.textContent = obj.message;
            row._wrap.insertBefore(note, row._wrap.firstChild);
            if (state.uiMode === 'gal') galExecLine('暂时没能查到相关信息', 'err');
            scrollToBottom();
          }
        } else if (obj.type === 'usage') {
          row._usageData = obj.usage;
          if (!row._usageEl) {
            row._usageEl = document.createElement('div');
            row._usageEl.className = 'usage-line';
            row._wrap.appendChild(row._usageEl);
          }
          row._usageEl.textContent = usageText(obj.usage, row._doneAt);
          if (state.uiMode === 'gal') galExecUsage(obj.usage);
        } else if (obj.type === 'timing') {
          // 请求链路埋点：分段耗时（load / assemble / doc / compress / first_token / total），
          // 只输出到控制台供排查，不打扰界面
          if (window.console && console.debug) console.debug('[timing]', obj.timing);
        } else if (obj.type === 'step') {
          // 每一步开始：上一步的思考收尾、工具组换新，过程按时间线往下排
          clearToolPending();
          if (thinkBox) closeThink('步骤 ' + (thinkBox._step || 1) + ' · 已思考');
          if (row._toolGroup) autoCollapseToolGroup(row._toolGroup);
          row._toolGroup = null;
          row._toolStep = obj.n || 0;
          if (row._recallTip) { row._recallTip.remove(); row._recallTip = null; }
          // 上一轮的正文与这一轮之间留一个空行，最终 markdown 才不会粘成一段
          if (acc && acc.slice(-2) !== '\n\n') { acc += '\n\n'; pushText('\n\n'); }
          thinkBox = buildThinkBlock('', true);
          thinkBox._step = obj.n || 0;
          thinkBox._label.textContent = '步骤 ' + (obj.n || 1) + ' · 思考中…';
          setLive('第 ' + (obj.n || 1) + ' 步 · 思考中');
          setLiveSec('');
          phase = 'think';
          phaseAt = Date.now();
          ensureFlow().appendChild(thinkBox);
          scrollToBottom();
        } else if (obj.type === 'subagent_start') {
          const r = subagentRow(obj.id);
          if (r) {
            r.querySelector('.st').textContent = '[' + (obj.role || '?') + '] ' + (obj.ask || '');
            r.querySelector('.sm').textContent = '开始';
          }
          scrollToBottom();
        } else if (obj.type === 'subagent_step') {
          const r = subagentRows[obj.id];
          if (r) {
            r.querySelector('.sm').textContent = (obj.tool || '')
              + (obj.target ? ' · ' + obj.target : '');
          }
        } else if (obj.type === 'subagent_done') {
          const r = subagentRows[obj.id];
          if (r) {
            const ok = !!obj.ok;
            r.classList.add(ok ? 'done' : 'fail');
            r.querySelector('.subagent-ico').textContent = ok ? '✓' : '✕';
            r.querySelector('.sm').textContent = ok
              ? (((obj.ms || 0) / 1000).toFixed(1) + 's · ' + (obj.tools || 0) + ' 次')
              : (obj.error || '失败');
          }
          if (subagentBox) {
            const total = Object.keys(subagentRows).length;
            const fin = subagentBox.querySelectorAll('.subagent-row.done, .subagent-row.fail').length;
            subagentBox.querySelector('.subagent-sum').textContent = fin + '/' + total;
          }
        } else if (obj.type === 'subagent_all') {
          if (subagentBox) {
            const title = subagentBox.querySelector('.subagent-title');
            if (title) title.textContent = '探索小队 · ' + (obj.n || 0) + ' 个';
            subagentBox.querySelector('.subagent-sum').textContent =
              (((obj.ms || 0) / 1000).toFixed(1) + 's · ' + (obj.tokens || 0) + ' token');
          }
        } else if (obj.type === 'auto_continue') {
          // 自动续跑：在过程流里留一条轻提示，让用户知道不是卡住了，是它自己在往下做
          const tip = document.createElement('div');
          tip.className = 'auto-continue';
          tip.textContent = '自动续跑 ' + obj.n + '/' + (obj.limit < 0 ? '∞' : obj.limit)
            + (obj.why ? ' · ' + obj.why : '');
          ensureFlow().appendChild(tip);
          scrollToBottom();
        } else if (obj.type === 'reasoning') {
          retryWait = null;
          if (!thinkBox) {
            thinkBox = buildThinkBlock('', true);
            thinkBox._step = row._toolStep || 1;
            thinkBox._label.textContent = '步骤 ' + thinkBox._step + ' · 思考中…';
            ensureFlow().appendChild(thinkBox);
          }
          thinkBox._body.textContent += obj.text;
          refreshThinkLabel(thinkBox, false);   // 标题 / 状态条显示「最近完成段落」的首行
          phase = 'output';          // 思考内容在流，不再需要秒数
          if (state.uiMode === 'gal') { galSetStatus('思考中…'); galThinkAppend(obj.text); }
          scrollToBottom();
        } else if (obj.type === 'delta') {
          retryWait = null;
          if (thinkBox) closeThink('步骤 ' + (thinkBox._step || 1) + ' · 已思考');
          setLive('正在输出');
          setLiveSec('');
          phase = 'output';
          acc += obj.text;
          pushText(obj.text);
          scrollToBottom();
        } else if (obj.type === 'replace') {
          // 已经收到过 error 时不接受替换：否则真正的报错会被收尾文案盖掉
          const _rt = String(obj.text || '');
          if (!sawError && (_rt.trim() || !acc.trim())) serverFinalText = _rt;
        } else if (obj.type === 'warn') {
          // 收尾提醒：回答被截断 / 本轮没有正文（以前是无声结束，看不到原因）
          const t = (acc ? '\n\n' : '') + obj.message;
          acc += t;
          pushText(t);
          if (state.uiMode === 'gal') galExecLine(String(obj.message || '').slice(0, 60), 'warn');
          scrollToBottom();
        } else if (obj.type === 'rewind') {
          // 整步重试：把本次失败尝试已经流出来的正文从界面与 acc 里回滚。
          // 只回滚「本次尝试」的字符数，前面成功轮次的内容保留。
          const n = Math.max(0, parseInt(obj.chars, 10) || 0);
          if (n > 0 && acc) {
            acc = acc.slice(0, Math.max(0, acc.length - n));
            serverFinalText = null;
            if (liveEnabled && liveBox) {
              liveRendered = 0;
              liveBox.innerHTML = '';
              tailBox.innerHTML = '';
              paintLive();
            } else if (textNode) {
              textNode.data = acc;
            }
          }
        } else if (obj.type === 'retry') {
          // 重试不清空：上一次尝试的思考折叠成「上次尝试（上游中断）」留在过程里，
          // 新一次尝试另起一个思考块（下一条 reasoning 会创建）。
          clearToolPending();
          if (thinkBox) {
            const text = thinkBox._body.textContent || '';
            if (!text.trim()) {
              thinkBox.remove();
            } else {
              thinkBox.classList.remove('open');
              const head = thinkLineSummary(text, true);
              thinkBox._hasSummary = !!head;
              thinkBox._label.textContent = '上次尝试（上游中断）'
                + (head ? ' · ' + head : '');
            }
            thinkBox = null;
          }
          phase = 'think';
          phaseAt = Date.now();
          const mode = String(obj.mode || 'normal');
          const waitMs = Math.max(0, Number(obj.delay_ms) || 0);
          retryWait = waitMs ? { until: Date.now() + waitMs } : null;
          const why = obj.why ? '上游中断（' + obj.why + '）' : '上游中断';
          const times = mode === 'always'
            ? ('第 ' + (obj.n || 1) + ' 次')
            : ((obj.n || 1) + '/' + (obj.limit || 1));
          setLive(why + '，正在重试 · ' + times);
          setLiveSec(waitMs ? ('等待 ' + Math.ceil(waitMs / 1000) + 's') : '');
          lastContentAt = Date.now();
          contentStaleShown = false;
        } else if (obj.type === 'end_reason') {
          // 结束原因：不拼进正文，收尾时单独渲染成一条提示（带「继续输出 / 重试」）
          endReason = obj.reason || null;
        } else if (obj.type === 'error') {
          clearToolPending();
          compressPendingClear();
          phase = 'idle';
          sawError = true;
          const t = (acc ? '\n\n' : '') + obj.message;
          acc += t;
          pushText(t);
          if (state.uiMode === 'gal') { galExecLine('被打断了…' + String(obj.message || '').slice(0, 60), 'err'); galSetStatus(''); }
          scrollToBottom();
        } else if (obj.type === 'approval') {
          // 写入审批：后端已挂起工具调用，等这里点「允许 / 拒绝」
          if (!row._toolsBox) {
            row._toolsBox = document.createElement('div');
            row._toolsBox.className = 'tool-list';
            row._wrap.appendChild(row._toolsBox);
          }
          if (typeof buildApprovalCard === 'function') {
            row._toolsBox.appendChild(buildApprovalCard(obj));
          }
          setLive('等待你确认');
          setLiveSec('');
          keepLiveLast();
          waitingApproval = true;
          phase = 'idle';              // 等用户点「允许 / 拒绝」
          scrollToBottom();
        } else if (obj.type === 'workflow') {
          // 工作流：节点状态 + 暂停 / 继续 / 取消（卡片由 chat.js 负责）
          if (typeof workflowRender === 'function') workflowRender(row, obj);
          scrollToBottom();
        } else if (obj.type === 'plan') {
          // 执行计划：模型通过 update_plan / plan_patch 维护，独立成一张清单卡
          if (typeof planRender === 'function') planRender(row, obj);
          scrollToBottom();
        } else if (obj.type === 'tool_stream') {
          // 原生工具调用参数正在生成：整轮结束前先把名字 / 大小显示出来。
          // 否则界面只有「思考中」或「尚未产出」，用户不知道它在写工具参数。
          if (!toolPending) {
            toolPending = document.createElement('div');
            toolPending.className = 'tool-pending';
            ensureFlow().appendChild(toolPending);
          }
          const name = obj.name || '工具';
          const many = (obj.calls || 0) > 1 ? ' · ' + obj.calls + ' 个调用' : '';
          toolPending.textContent = '正在生成 ' + name + ' 参数 · '
            + fmtBytes(obj.bytes) + many;
          setLive('正在生成工具调用 ' + name + ' · ' + fmtBytes(obj.bytes));
          setLiveSec('');
          phase = 'toolgen';
          scrollToBottom();
        } else if (obj.type === 'script_log') {
          // Code Mode 脚本的实时日志：只更新状态条，不写进正文
          setLive('脚本 · ' + String(obj.text || '').slice(0, 60));
          scrollToBottom();
        } else if (obj.type === 'tool') {
          clearToolPending();
          const useGal = state.uiMode === 'gal' && galCfg('gal_show_exec', true) !== false;
          // 普通对话走过程流（工具组直接进 flow），不再需要末尾的 tool-list 容器
          if (!useGal && !row._flow && !row._toolsBox) {
            row._toolsBox = document.createElement('div');
            row._toolsBox.className = 'tool-list';
            row._wrap.appendChild(row._toolsBox);
            keepLiveLast();
          }
          // GAL 的执行区本来就是单行紧凑式：结果保持直接展示（collapse=false）
          const card = buildToolCard(obj.name, obj.args, !useGal);
          if (useGal) {
            $('#gal-tools').appendChild(card);
          } else {
            const g = toolGroup(row);        // 归进「步骤组」，标题实时更新
            g.body.appendChild(card);
            g.pending++;
            // 记下当前步骤，组头显示「正在修改 xxx」而不是笼统的「运行中…」
            g.current = { name: obj.name,
                          arg: (typeof toolArgText === 'function'
                                ? toolArgText(obj.name, obj.args) : '') };
            toolGroupTitle(g);
          }
          row._toolQueue = row._toolQueue || {};
          (row._toolQueue[obj.name] = row._toolQueue[obj.name] || []).push(card);
          const _lact = (typeof toolArgText === 'function')
            ? toolArgText(obj.name, obj.args) : '';
          setLive('调用 ' + obj.name + (_lact ? ' · ' + _lact : ''));
          setLiveSec('');
          phase = 'tool';              // 工具执行期间只有这一步在跑，标题里显示已用秒数
          phaseAt = Date.now();
          if (state.uiMode === 'gal') galSetStatus('稍候…');
          scrollToBottom();
        } else if (obj.type === 'tool_done') {
          const q = row._toolQueue && row._toolQueue[obj.name];
          const card = q && q.length ? q.shift() : null;
          if (card) finishToolCard(card, obj);
          if (row._toolGroup) {              // 只有普通对话会建这个组
            const g = row._toolGroup;
            g.pending = Math.max(0, g.pending - 1);
            g.done++;
            g.ms += parseInt(obj.ms, 10) || 0;
            g.kinds[obj.name] = (g.kinds[obj.name] || 0) + 1;
            if (obj.ok === false) g.failed++;
            if (obj.write) row._wroteTurn = true;      // 供「回滚本轮」按钮判断
            toolGroupTitle(g);
          }
          // 同一批还有工具在跑就继续计时；全跑完则回到「等模型」
          setLive((row._toolGroup && row._toolGroup.pending > 0) ? '并行执行中' : '继续思考');
          phase = (row._toolGroup && row._toolGroup.pending > 0) ? 'tool' : 'think';
          phaseAt = Date.now();
          if (state.uiMode === 'gal') galSetStatus('思考中…');
          ideOnToolDone(obj);
          scrollToBottom();
        } else if (obj.type === 'ask') {
          if (!askedNotified && chatWindowAway()) {
            askedNotified = true;
            const qs = obj.questions || [];
            const title = qs.length > 1
              ? 'AI 有 ' + qs.length + ' 个问题等你回答' : 'AI 在等你回答';
            const body = String((qs[0] || {}).question || '').slice(0, 90);
            if (typeof notifyOnce === 'function') {
              notifyOnce(obj.qid ? 'ask:' + obj.qid : '', title, body, 'ask');
            } else if (typeof showSystemNotify === 'function') {
              showSystemNotify(title, body, 'ask');
            }
          }
          const askCard = buildAskGroup(obj.questions, obj.qid);
          const askGal = state.uiMode === 'gal' && galCfg('gal_show_exec', true) !== false;
          if (askGal) $('#gal-tools').appendChild(askCard);
          else splitAtAsk(askCard);      // 卡片留在提问发生的那个位置
          setLive('等待你回答');
          setLiveSec('');
          keepLiveLast();
          phase = 'idle';              // 在等用户回答，不该继续显示「思考中 120s」
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
            compressPending();
          }
        } else if (obj.type === 'gal_speech') {
          galState.streamedTurn = true;      // 本轮已逐段流式渲染（结束时不再回填重排）
          galSetStatus('');
          galSetTyping(true);
          galAddSegment(obj);
        } else if (obj.type === 'gal_stage') {
          if (obj.bg) galSetBackground(obj.bg);
          if (obj.bgm) galSetBgm(obj.bgm);
          else if (obj.bgm_stop) galSetBgm('');
          if (obj.cast !== undefined && obj.cast !== null) galSetCast(obj.cast);
        } else if (obj.type === 'gal_map') {
          galSetMap(obj.map);
        } else if (obj.type === 'gal_stats') {
          galApplyStats(obj);
        } else if (obj.type === 'gal_roll') {
          galShowRoll(obj);
        } else if (obj.type === 'gal_options') {
          // 选项不立即出现：先暂存在 pendingOptions，等本轮最后一段文字打完再显示
          galState.pendingOptions = (obj.options || []).slice();
          galState.pendingNudge = String(obj.nudge || '');   // 无选项时「邀请玩家开口」的那句话
          galFlushOptions();
        } else if (obj.type === 'gal_node') {
          galSetNode(obj);
          if (obj.auto) {
            galAppendDivider('—— 幕间 · 剧情推进 ——');   // AI 自动剧本进入下一幕
            if (obj.scene) galAddSegment({ narration: obj.scene });   // 幕的场景描述进对话框
          }
        } else if (obj.sessions) {
          applySessionsPayload(obj.sessions);
          if (obj.type === 'done') {
            gotDone = true;
            row._doneAt = obj.done_at || row._doneAt;
            if (row._usageEl && row._usageData) {
              // 输出完成时间挂在缓存命中后面：排查「这轮什么时候结束的」不用再翻日志
              row._usageEl.textContent = usageText(row._usageData, row._doneAt);
            }
            clearToolPending();
            phase = 'idle';
            doneBlocks = Array.isArray(obj.blocks) ? obj.blocks : null;
            doneTiming = obj.timing || null;
            doneTools = Array.isArray(obj.tools) ? obj.tools : null;
            if (thinkBox) closeThink();          // 最后一个思考块落定，标题换成摘要
            foldLiveProcess();                   // 整个过程折成「过程 · 工作 X 秒」
            // 本轮结束：把工具轨迹折成一行摘要（用户手动展开过则保持展开）
            if (row._toolGroup) autoCollapseToolGroup(row._toolGroup);
            if (typeof planAutoCollapse === 'function') planAutoCollapse(row);
            // 变更审阅：把本轮写过的文件一次性交给用户确认（撤销 / 保留）
            if (typeof ideReviewEndTurn === 'function') ideReviewEndTurn(row);
          }
          renderSessions();
          if (obj.context && isCur()) renderContextBar(obj.context);
          if (state.uiMode === 'gal' && isCur()) {
            galSetTyping(false);
            galSetStatus('');
            renderMessages();
            // 本轮已逐段流式渲染过 → 不回填重排，否则会把逐段效果抹成一次性整段。
            // 另外只认「done」（本轮真正结束）：流开头的 "session" 事件也带 sessions，
            // 若在那时回填，会把上一轮的对白与选项重新画出来（表现为选项一直悬浮、点了没反应）。
            if (obj.type === 'done' && !galState.streamedTurn) galRenderFromSession();
            const rp = $('#gal-review-panel');
            if (rp && !rp.classList.contains('hidden')) galRenderReview();   // 回顾面板打开时同步刷新
            const jp = $('#gal-journal-panel');
            if (jp && !jp.classList.contains('hidden')) galRenderJournal();  // 档案面板打开时同步刷新
          }
        }
      }
    }
  } catch (e) {
    streamError = e;
    acc += (acc ? '\n\n' : '') + '请求失败：' + e;
  } finally {
    stopWaiter();
    if (paintTimer) { clearTimeout(paintTimer); paintTimer = 0; }
    const stopped = !!(stRef && stRef.stopping);
    if (state.uiMode === 'gal') { galSetTyping(false); galSetStatus(''); }
    if (typing.parentNode) typing.remove();
    const tip = c.querySelector('.recognize-tip');
    if (tip) tip.remove();
    cur.remove();
    (row._secs || [c]).forEach((n) => { n.style.whiteSpace = ''; });
    clearToolPending();
    if (thinkBox) closeThink();
    if (!gotDone) foldLiveProcess();     // 流异常没收到 done 时也要收尾
    if (flow) {
      // 收尾时把还开着的思考块统一折起；只替换默认标题，别误伤思考摘要
      flow.querySelectorAll('.think').forEach((b) => {
        b.classList.remove('open');
        const lb = b.querySelector('.think-label');
        if (lb && (/^步骤 \d+ · 思考中…$/.test(lb.textContent)
                   || lb.textContent === '深度思考中…')) {
          lb.textContent = lb.textContent.replace('思考中…', '已思考');
        }
      });
    }
    const shown = serverFinalText != null ? serverFinalText : acc;
    // 后台会话在 slim 数据里没有 messages，不能拿它算下标 / 建操作条
    const bg = !isCur();
    const mine = state.sessions.sessions.find((x) => x.id === sid) || null;
    let idx;
    if (shown.trim()) {
      row._raw = shown;
      // 重排 markdown 会改变高度，先记住「用户是否还在跟随」（不是当前位置是否贴底）
      const wasAtBottom = !bg && _followBottom;
      // V3：流结束时按服务端落库的 blocks 重排一次——思考 / 工具进过程折叠，
      // 正文按段落 + 提问卡按真实位置排开。旧会话 / GAL / 后台会话不重绘。
      const rebuild = !bg && state.uiMode !== 'gal' && doneBlocks && doneBlocks.length;
      if (rebuild) {
        const wasOpen = !!(row._flowWrap && !row._flowWrap.classList.contains('collapsed'));
        const view = buildBlocksView(doneBlocks, doneTiming,
                                     (endReason && endReason.kind) || '',
                                     { tools: doneTools || [], open: wasOpen });
        if (row._flowWrap) row._flowWrap.remove();
        // 流式期间切出来的正文段 / 提问卡作废，统一按落库的块重排
        (row._secs || []).forEach((n) => n.remove());
        (row._asks || []).forEach((n) => n.remove());
        row._secs = [];
        row._asks = [];
        const tail = (row._usageEl && row._usageEl.parentNode === row._wrap)
          ? row._usageEl : null;
        const put = (n) => row._wrap.insertBefore(n, tail);
        if (view.fold) {
          put(view.fold);
          row._flowWrap = view.fold;
          row._flow = view.flow;
          row._flowHead = view.fold.querySelector('.process-fold-head');
          row._flowLabel = view.fold.querySelector('.process-fold-title');
          row._flowCaret = view.fold.querySelector('.process-fold-caret');
          if (window.enhanceRichMedia) window.enhanceRichMedia(view.fold);
        } else {
          row._flowWrap = null;
          row._flow = null;
          row._flowHead = null;
          row._flowLabel = null;
          row._flowCaret = null;
        }
        view.parts.forEach((p) => {
          put(p);
          if (p.classList.contains('ask-card')) row._asks.push(p);
          else row._secs.push(p);
        });
        if (row._secs.length) {
          c = row._secs[row._secs.length - 1];
          row._content = c;
        } else {
          c.innerHTML = safeRenderMarkdown(shown);   // 只有过程、没有正文
          row._secs = [c];
        }
      } else {
        // 没拿到块（断流 / 旧服务端）：正文合并回第一段，避免同一段文字在多段里重复
        if ((row._secs || []).length > 1) {
          const first = row._secs[0];
          row._secs.slice(1).forEach((n) => n.remove());
          row._secs = [first];
          first.style.display = '';
          c = first;
          row._content = c;
        }
        c.innerHTML = safeRenderMarkdown(shown);
      }
      // 多段正文都要走富媒体增强（公式 / 图表），所以按整条消息扫一遍
      if (window.enhanceRichMedia) window.enhanceRichMedia(row._wrap);
      idx = (!bg && mine && Array.isArray(mine.messages))
        ? mine.messages.length - 1 : undefined;
      if (typeof idx === 'number') row.dataset.idx = idx;
      if (!bg) {
        if (memNote) row._wrap.appendChild(buildMemoryChip(memNote, idx));
        row._wrap.appendChild(buildActions(row, () => row._raw, idx, 'ai'));
        refreshKbIfOpen(memNote && memNote.changed);
      }
      if (wasAtBottom) scrollToBottom(true);  // 原本贴底才重新贴底，消除「结尾跳一下」
    } else if (serverFinalText != null || !flow) {
      // 服务端校正后正文为空（只吐了裸工具 JSON）时也要清掉流式残留
      c.innerHTML = '<p style="color:var(--text-dim)">（无内容）</p>';
    }
    // 中断原因放在这条消息的最末尾（GAL 模式不加，避免破坏演出）
    if (!bg && endReason && state.uiMode !== 'gal'
        && typeof buildEndNote === 'function' && row._wrap) {
      row._wrap.appendChild(buildEndNote(
        endReason, typeof idx === 'number' ? idx : undefined));
    }
    // 后台会话：丢掉 live host（结果已落盘，切回去会从消息重建），标未读
    const bgTitle = bg ? ((mine && mine.title) || '新对话') : '';
    unregisterStream(sid, !bg);
    if (bg) {
      state.unread[sid] = true;
      renderSessions();
    }
    // 后台提醒：所有终止都提醒，文案写结束原因（手动停止除外）；后台会话无论窗口是否前台都提醒。
    // 断流但任务还在服务端跑 → 不在这里提前报「请求失败」，等 keepRecovering 拿回真正原因再提醒。
    const deferNotify = !recovered && !gotDone && !recovering && isCur();
    if (!recovered && !recovering && !deferNotify) {
      notifyChatDone({
        text: shown,
        error: streamError || (!gotDone ? new Error('连接提前结束') : null),
        stopped: stopped,
        reason: endReason,
        title: bgTitle,
      });
    }
    if (state.uiMode === 'gal' && isCur()) galFlushOptions();   // 流已结束 → 若有暂存选项则显示
    scrollToBottom();
    // 兜底同步：正常流程 done 事件已带回最新会话。
    // 没收到 done（断网 / 关页面 / 连接被掐）时，任务其实还在服务端跑：
    // 挂到 /api/chat/attach 等它结束再拉一次状态，而不是立刻拉一份还没落库的旧数据。
    if (!recovered && !gotDone && !recovering && isCur()) {
      keepRecovering(sid, streamError || new Error('连接提前结束')).catch(() => {});
    }
  }
}

/* 流异常结束（没收到 done）后一直盯到任务真正结束：
   单次 attach 失败或任务还在跑时，每 5 秒再试一次（最多约 10 分钟）。
   以前只试一次，attach 一失败就没人再管，界面永远停在半截回答上。 */
async function keepRecovering(sid, err) {
  let failures = 0;
  for (let i = 0; i < 120; i++) {
    let r = null;
    try { r = await waitJobThenRefresh(sid); failures = 0; } catch (e) {
      r = null;
      failures += 1;
      // 连续失败通常不是「任务在跑」，而是后端进程已经退出（端口没了）。
      // 以前会继续显示「后台运行中」十分钟且无法终止，这里明确止损。
      if (failures >= 3) {
        if (state.streamingSids) delete state.streamingSids[String(sid)];
        renderSessions();
        if (typeof refreshStreamingUI === 'function') refreshStreamingUI();
        renderMessages();
        toast('后台服务已断开（进程已退出），请重新启动应用', 'error');
        return;
      }
    }
    if (r && r.ok) {
      // 任务真正结束：按服务端回放的 end_reason 补一条通知（窗口在后台 / 别的会话才弹）；
      // 拿不到原因就按「连接中断 + 原请求异常」说明，不能假装正常完成。
      const cur = String(state.sessions.current || '') === String(sid);
      const s = state.sessions.sessions.find((x) => x.id === sid) || null;
      notifyChatDone({
        text: '', error: err || null, stopped: false, reason: r.reason || null,
        title: cur ? '' : ((s && s.title) || '新对话'),
      });
      return;
    }
    await new Promise((res) => setTimeout(res, 5000));
  }
  // 等满 10 分钟仍没结束：清掉「后台运行中」标记，避免一直骗人
  if (state.streamingSids) delete state.streamingSids[String(sid)];
  renderSessions();
  if (typeof refreshStreamingUI === 'function') refreshStreamingUI();
  renderMessages();
}

/* 断线后任务仍在后台跑：等它结束（收到 done）再拉一次状态并重绘。
   不逐事件重放——那需要复用 300 多行渲染逻辑；结果已经落库，重绘即可。
   返回 { ok, reason }：ok 只代表「任务确实结束了」；reason 是 attach 回放里的
   end_reason（断流期间服务端算好的结束原因，用于补一条带原因的通知）。 */
async function waitJobThenRefresh(sid) {
  const pullState = async () => {
    const st = await fetch('/api/state', { headers: { 'X-Auth-Token': TOKEN } });
    if (!st.ok) return null;
    const d = await st.json();
    applySessionsPayload(d.sessions);
    const running = Array.isArray(d.streaming) ? d.streaming : [];
    state.streamingSids = {};
    running.forEach((id) => { state.streamingSids[String(id)] = true; });
    renderSessions();
    if (typeof refreshStreamingUI === 'function') refreshStreamingUI();
    if (String(state.sessions.current || '') === sid) {
      renderMessages();
      renderContextBar(d.context);
      scrollToBottom(true);
    }
    // 后端还在跑就返回 false：不能提前收尾，否则界面把光标收掉、结果却还没落库
    return running.indexOf(String(sid)) < 0;
  };
  let reason = null;
  try {
    const res = await fetch('/api/chat/attach', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Auth-Token': TOKEN },
      body: JSON.stringify({ session_id: sid, since: 0 }),
    });
    if (!res.ok || !res.body) {
      // 没有可挂的任务（已结束 / 已过期）：以 /api/state 的 streaming 为准
      return { ok: (await pullState()) === true, reason: reason };
    }
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    let finished = false;
    while (!finished) {
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
        if (obj.type === 'end_reason') reason = obj.reason || null;
        if (obj.type === 'done') { finished = true; break; }
      }
    }
    try { reader.cancel(); } catch (e) { /* 已关闭 */ }
    return { ok: (await pullState()) !== false, reason: reason };
  } catch (e) {
    // attach 断了：补一次状态，但只有「后端确认不在跑」才算恢复完成
    try { return { ok: (await pullState()) === true, reason: reason }; }
    catch (e2) { return { ok: false, reason: reason }; }
  }
}

/* ============================================================
   撤回 / 修改 / 重新发言 / 源码视图
   ============================================================ */
async function withdrawUserMessage(index) {
  if (state.streaming || typeof index !== 'number') return;
  if (!confirm('撤回这条消息？它之后的 AI 回复也会一并移除。')) return;
  const r = await api('/api/message/withdraw', { session_id: state.sessions.current, index: index });
  if (r.sessions) applySessionsPayload(r.sessions);
  renderSessions();
  renderMessages();
  if (state.uiMode === 'gal') galRenderFromSession();   // GAL：舞台对白与状态参数也要跟着回滚
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
    if (r.sessions) applySessionsPayload(r.sessions);
    renderSessions();
    renderMessages();
    if (state.uiMode === 'gal') galRenderFromSession();   // GAL：舞台对白与状态参数也要跟着回滚
    if (r.context) renderContextBar(r.context);
    toast('已删除该条消息', 'success');
  } catch (e) { toastErr('删除', e); }
}

/* 修改已发送的用户消息：不开内嵌编辑框，把文本退回底部输入框。
   发送时仍走 resend(mode:'edit') —— 丢弃其后的回复并重新生成，保留版本链。
   附件不随文字退回（历史里只有落盘引用），需要重新添加。 */
function editUserMessage(index) {
  if (state.streaming || typeof index !== 'number') return;
  // 进入编辑前先退出该条的版本预览（否则输入框内容与气泡显示不一致）
  if (verPreview && verPreview.index === index) { verPreview = null; refreshUserRow(index); }
  // 已经在改另一条 → 先清掉，避免两条文本拼在输入框里
  if (editTarget) cancelEditTarget(true);
  const s = curSession();
  const m = s && s.messages[index];
  if (!m || m.role !== 'user') return;
  const input = $('#input');
  if (!input) return;

  editTarget = { sid: String(state.sessions.current || ''), index: index };
  const draft = input.value.trim();
  input.value = (draft ? draft + '\n\n' : '') + (m.content || '');
  autoGrow();
  if (typeof cmpUpdateHint === 'function') cmpUpdateHint();
  if (typeof cmpUpdateClear === 'function') cmpUpdateClear();
  input.focus();
  try { input.setSelectionRange(input.value.length, input.value.length); } catch (e) { /* 忽略 */ }

  const row = $('#chat').querySelector('.msg.user[data-idx="' + index + '"]');
  if (row) row.classList.add('editing-target');
  toast(m.images || m.image || m.doc
    ? '已退回输入框（附件需要重新添加）· Esc 取消修改'
    : '已退回输入框，改完直接发送 · Esc 取消修改');
}

/* 取消「修改历史消息」：清掉高亮与标记，输入框里的文字保留 */
function cancelEditTarget(clearInput) {
  if (!editTarget) return;
  const row = $('#chat').querySelector('.msg.user[data-idx="' + editTarget.index + '"]');
  if (row) row.classList.remove('editing-target');
  editTarget = null;
  if (clearInput) {
    const inp = $('#input');
    if (inp) { inp.value = ''; autoGrow(); }
  }
  if (typeof cmpUpdateHint === 'function') cmpUpdateHint();
  if (typeof cmpUpdateClear === 'function') cmpUpdateClear();
}

async function regenerateReply(index) {
  if (state.streaming || typeof index !== 'number') return;
  if (!hasChatKey()) { toast('请先在「设置 → 模型接入」中填写密钥', 'error'); return; }
  await resend({ mode: 'regenerate', index: index });
}

// 继续输出：复用最后一条被打断的 AI 气泡，从断点接着写（仅 Solo/IDE）
// isResume = true 时是「进程重启后的断点续跑」：后端会附带已完成步骤摘要。
async function continueReply(index, isResume) {
  if (state.streaming || typeof index !== 'number') return;
  if (state.uiMode === 'gal') return;                 // GAL 模式不提供
  if (!hasChatKey()) { toast('请先在「设置 → 模型接入」中填写密钥', 'error'); return; }
  const s = curSession();
  const m = s && s.messages[index];
  if (!m || m.role !== 'assistant') return;
  const row = $('#chat').querySelector('.msg.ai[data-idx="' + index + '"]');
  if (!row || !row._content) { toast('找不到这条消息，请刷新后重试', 'error'); return; }
  // 旧的「你手动停止了本轮 / 继续输出」提示条先摘掉：本轮正常写完就不会再挂回来
  //（服务端也会清掉 end_reason；这里同步 DOM，免得要等切会话/刷新才消失）。
  row.querySelectorAll('.end-note').forEach((n) => n.remove());
  await streamChat({ continue: true, resume: !!isResume }, { row: row, base: m.content || '' });
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
    galPlayIntake();
  }
  await streamChat(body);
}

function toggleRawView(row) {
  if (typeof row._raw !== 'string' || !row._content) return;
  row._rawMode = !row._rawMode;
  // 一条消息可能有多段正文 + 提问卡：源码视图只留第一段显示全文，其余先藏起来
  const secs = (row._secs && row._secs.length > 1) ? row._secs : null;
  const asks = (secs && row._asks) || [];
  if (row._rawMode) {
    if (secs) {
      secs.forEach((s, i) => {
        s._rawSaved = s.innerHTML;
        s.style.display = i === 0 ? '' : 'none';
      });
      asks.forEach((a) => { a.style.display = 'none'; });
    }
    const box = secs ? secs[0] : row._content;
    box.innerHTML = '';
    const pre = document.createElement('div');
    pre.className = 'raw-text';
    pre.textContent = row._raw;
    box.appendChild(pre);
    const rng = document.createRange();
    rng.selectNodeContents(pre);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(rng);
    toast('已切换为源码视图，可直接复制源码');
  } else if (secs) {
    secs.forEach((s) => {
      s.style.display = '';
      if (s._rawSaved != null) { s.innerHTML = s._rawSaved; s._rawSaved = null; }
    });
    asks.forEach((a) => { a.style.display = ''; });
    if (window.enhanceRichMedia) window.enhanceRichMedia(row._content);
  } else {
    row._content.innerHTML = safeRenderMarkdown(row._raw);
    if (window.enhanceRichMedia) window.enhanceRichMedia(row._content);
  }
  if (row._selBtn) {
    const label = row._rawMode ? '显示渲染' : '选择文本';
    row._selBtn.dataset.tip = label;
    row._selBtn.setAttribute('aria-label', label);
    row._selBtn.innerHTML = icon(row._rawMode ? 'note' : 'select');
  }
}

/* ============================================================
   图片：先暂存为「待发送附件」，可继续输入文字后一起发送
   ============================================================ */
// 文档标签后缀：优先显示字数，其次文件体积
function docChipSuffix(dc) {
  if (!dc) return '';
  if (dc.chars) return '（' + dc.chars + ' 字）';
  if (dc.size) return '（' + Math.max(1, Math.round(dc.size / 1024)) + ' KB）';
  return '';
}

const DOC_MAX_BYTES = 2 * 1024 * 1024 * 1024; // 单个附件宽松上限 2GB（防误拖超大文件；实际仍受浏览器内存约束）
const MAX_PENDING_IMAGES = 4;                 // 一次最多带 4 张图，避免请求体过大
let attachSeq = 0;                            // 连续粘贴多张时保持顺序
let attachEpoch = 0;                          // 清空 / 发送后作废还在读取的附件
let imageLoads = 0;                           // 正在读取、还没 onload 的图片数

function attachImageBlob(blob) {
  if (!blob) return;
  if (state.pendingDoc) {
    toast('已有待发送文档，请先发送或移除后再添加图片', 'error');
    return;
  }
  const loaded = (state.pendingImages || []).length;
  if (loaded + imageLoads >= MAX_PENDING_IMAGES) {
    toast('一次最多发送 ' + MAX_PENDING_IMAGES + ' 张图片', 'error');
    return;
  }
  const seq = ++attachSeq;
  const epoch = attachEpoch;
  imageLoads++;
  const reader = new FileReader();
  const done = () => { imageLoads = Math.max(0, imageLoads - 1); };
  reader.onload = () => {
    done();
    if (epoch !== attachEpoch) return;        // 读取期间已清空 / 已发送
    if (state.pendingDoc) return;             // 读取期间改附了文档 → 不再塞图片
    const url = String(reader.result || '');
    const mime = (url.match(/^data:([^;]+);/) || [])[1] || 'image/png';
    const item = {
      mime,
      b64: url.split(',')[1] || '',
      url,
      seq,
    };
    state.pendingImages = (state.pendingImages || []).concat(item)
      .sort((a, b) => (a.seq || 0) - (b.seq || 0));
    state.pendingImage = state.pendingImages[0];
    renderAttach();
  };
  reader.onerror = () => { done(); toast('图片读取失败', 'error'); };
  reader.readAsDataURL(blob);
}

// 文档附件：读成 base64 暂存，正文由后端在发送时抽取（界面只显示文件名）
function attachDocBlob(file) {
  if (!file) return;
  if (state.pendingDoc || state.pendingImage
      || (state.pendingImages && state.pendingImages.length) || imageLoads) {
    toast('已有待发送附件，请先发送或移除后再添加文档', 'error');
    return;
  }
  if (file.size > DOC_MAX_BYTES) { toast('附件太大（上限 2GB）', 'error'); return; }
  const name = file.name || '附件';
  const epoch = attachEpoch;
  const reader = new FileReader();
  reader.onload = () => {
    if (epoch !== attachEpoch) return;        // 读取期间已清空 / 已发送
    const s = String(reader.result || '');
    state.pendingDoc = {
      name,
      mime: file.type || (s.match(/^data:([^;]+);/) || [])[1] || 'application/octet-stream',
      b64: s.split(',')[1] || '',
      size: file.size || 0,
    };
    state.pendingImage = null;
    state.pendingImages = [];
    renderAttach();
    toast('已附带 ' + name + '，可继续输入文字后一起发送');
  };
  reader.readAsDataURL(file);
}

function removePendingImage(index) {
  state.pendingImages = (state.pendingImages || []).filter((_, i) => i !== index);
  state.pendingImage = state.pendingImages[0] || null;
  renderAttach();
}

function renderAttach() {
  const imgs = (state.pendingImages && state.pendingImages.length)
    ? state.pendingImages
    : (state.pendingImage ? [state.pendingImage] : []);
  const doc = state.pendingDoc;
  const bar = $('#attach-bar');
  if (!bar) return;
  bar.classList.toggle('hidden', !imgs.length && !doc);
  const box = $('#attach-thumbs');
  if (box) {
    box.classList.toggle('hidden', !imgs.length);
    box.innerHTML = '';
    imgs.forEach((im, i) => {
      const wrap = document.createElement('span');
      wrap.className = 'attach-thumb-wrap';
      const thumb = document.createElement('img');
      thumb.className = 'attach-thumb';
      thumb.src = im.url;
      thumb.alt = '待发送图片 ' + (i + 1);
      const x = document.createElement('button');
      x.type = 'button';
      x.className = 'attach-thumb-x';
      x.title = '移除这张图片';
      x.textContent = '✕';
      x.onclick = (e) => { e.stopPropagation(); removePendingImage(i); };
      wrap.appendChild(thumb);
      wrap.appendChild(x);
      box.appendChild(wrap);
    });
  }
  const chip = $('#attach-doc');
  if (chip) {
    chip.classList.toggle('hidden', !doc);
    chip.innerHTML = doc ? (icon('attach') + ' ' + escapeHtml(doc.name) + docChipSuffix(doc)) : '';
  }
  const txt = $('#attach-text');
  if (txt) {
    txt.textContent = imgs.length > 1
      ? ('已附带 ' + imgs.length + ' 张图片 · 可继续输入文字后一起发送')
      : (imgs.length ? '已附带图片 · 可继续输入文字后一起发送'
      : (doc ? '已附带文档（发送时自动抽取正文）· 可继续输入文字后一起提问' : ''));
  }
  syncSendBtn();
}

function clearAttach() {
  attachEpoch++;
  state.pendingImage = null;
  state.pendingImages = [];
  state.pendingDoc = null;
  const box = $('#attach-thumbs');
  if (box) box.innerHTML = '';
  const thumb = $('#attach-thumb');
  if (thumb) thumb.removeAttribute('src');   // 兼容旧 DOM
  renderAttach();
}

/* ---------- 引用块（「添加到对话」）----------
   只在输入框上方显示一个「选中文本 N」小块；发送时作为 quotes 随请求带走，
   由后端拼进本轮 prompt，不写入消息正文。 */
const QUOTE_MAX_ONE = 4000;      // 与后端 _quotes_block 的上限保持一致

function addQuote(text) {
  const t = String(text || '').trim();
  if (!t) return false;
  const clipped = t.length > QUOTE_MAX_ONE ? t.slice(0, QUOTE_MAX_ONE) + '…（已截断）' : t;
  state.pendingQuotes = (state.pendingQuotes || []).concat({ text: clipped });
  renderQuoteBar();
  return true;
}

function removeQuote(index) {
  state.pendingQuotes = (state.pendingQuotes || []).filter((_, i) => i !== index);
  renderQuoteBar();
}

function clearQuotes() {
  if (!(state.pendingQuotes || []).length) return;
  state.pendingQuotes = [];
  renderQuoteBar();
}

function renderQuoteBar() {
  const bar = $('#quote-bar');
  if (!bar) return;
  const list = state.pendingQuotes || [];
  bar.classList.toggle('hidden', !list.length);
  bar.innerHTML = '';
  list.forEach((q, i) => {
    const chip = document.createElement('span');
    chip.className = 'quote-chip';
    chip.title = q.text.slice(0, 200) + (q.text.length > 200 ? '…' : '');
    chip.innerHTML = icon('chat') + '<span class="quote-label">选中文本 ' + (i + 1) + '</span>';
    const x = document.createElement('button');
    x.type = 'button';
    x.className = 'quote-x';
    x.title = '移除这条引用';
    x.textContent = '✕';
    x.onclick = () => removeQuote(i);
    chip.appendChild(x);
    bar.appendChild(chip);
  });
}
