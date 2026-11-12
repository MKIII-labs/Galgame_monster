'use strict';

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

/* ---------- 实时消息的数据层（单一数据源） ----------
   一次回答的内容只存在这里：文本段 / 思考段 / 工具 / 计划。
   DOM 只是它的一份投影，不是状态本身。
   这样切会话、刷新、断线重连都按同一份数据重建，不会出现
   「落库的进度快照 + 正在增长的实时行」两条并存。 */
function liveOf(sid, peek) {
  const key = String(sid || state.sessions.current || '');
  if (!key) return null;
  if (!state.live) state.live = {};
  let L = state.live[key];
  if (!L && !peek) {
    L = { blocks: [], tools: [], plan: null, timing: null, text: '' };
    state.live[key] = L;
  }
  return L || null;
}

function liveDrop(sid) {
  const key = String(sid || '');
  if (state.live && key) delete state.live[key];
}

/* 正文：连续的 delta 归到同一个 text 段；工具 / 提问 / 思考会切断它，
   与后端 turn_blocks 的分段口径一致。 */
function liveText(L, t) {
  const s = String(t || '');
  if (!L || !s) return;
  L.text += s;
  const last = L.blocks[L.blocks.length - 1];
  if (last && last.kind === 'text') last.text += s;
  else L.blocks.push({ kind: 'text', text: s });
}

/* 接管 / 续写：起点正文是「整段给定」而不是增量，直接重置成一段 text */
function liveSetText(L, t) {
  if (!L) return;
  L.text = String(t || '');
  L.blocks = L.text ? [{ kind: 'text', text: L.text }] : [];
}

function liveReasoning(L, t, retry) {
  const s = String(t || '');
  if (!L || !s) return;
  const last = L.blocks[L.blocks.length - 1];
  if (last && last.kind === 'reasoning' && !retry) last.text += s;
  else {
    const b = { kind: 'reasoning', text: s };
    if (retry) b.retry = 1;
    L.blocks.push(b);
  }
}

/* 工具：先占一个下标，结束再回填结果。
   下标与 tools 数组一致，投影时 buildBlocksView 直接按 i 取。 */
function liveTool(L, name, args) {
  if (!L) return -1;
  L.blocks.push({ kind: 'tool', i: L.tools.length });
  L.tools.push({ tool: String(name || ''), args: args || {}, ok: null });
  return L.tools.length - 1;
}

function liveToolDone(L, i, obj) {
  if (!L || i < 0 || !L.tools[i]) return;
  const t = L.tools[i];
  t.result = obj.result;
  t.ok = obj.ok !== false;
  if (obj.ms != null) t.ms = obj.ms;
  if (obj.undo) t.undo = obj.undo;
  if (obj.write) t.write = true;
  if (obj.lines != null) t.lines = obj.lines;
}

function liveAsk(L, i, questions, qid) {
  if (!L || i < 0 || !L.tools[i]) return;
  L.tools[i].asks = questions || [];
  // qid 必须跟着走：投影重建的提问卡要靠它把答案送回后端
  if (qid) L.tools[i].qid = qid;
}

function livePlan(L, plan) {
  if (!L || !plan) return;
  L.plan = plan;
}

/* 整步重试回滚：从尾部砍掉 n 个字符（只动正文，已发生的工具块保持不变） */
function liveTrim(L, n) {
  if (!L || !n) return;
  L.text = L.text.slice(0, Math.max(0, L.text.length - n));
  let left = n;
  while (left > 0 && L.blocks.length) {
    const b = L.blocks[L.blocks.length - 1];
    if (b.kind !== 'text') break;
    if (b.text.length <= left) { left -= b.text.length; L.blocks.pop(); }
    else { b.text = b.text.slice(0, b.text.length - left); left = 0; }
  }
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
  // 「回到实时输出」提示条：服务端在跑、但本地没有流（刷新 / 重开 / 断线）时出现
  refreshResumeBar();
}

/* 后台任务提示条显隐：只有在「当前会话仍被服务端标记为运行中」且「本地没有流对象」
   时才显示。本地流接管成功后 streamOf() 为真，提示条自动消失。 */
function refreshResumeBar() {
  const bar = $('#resume-bar');
  if (!bar) return;
  const sid = String(state.sessions.current || '');
  const st = streamOf(sid);
  // 纯文本回放（replay）不算「已接管」：用户仍该看到「回到实时输出」切到完整流
  const show = !!sid && (!st || st.replay)
    && !!(state.streamingSids && state.streamingSids[sid]);
  bar.classList.toggle('hidden', !show);
}

function registerStream(sid, host, row) {
  const key = String(sid);
  state.streams[key] = { sid: key, host, row, stopping: false, lastEvent: Date.now(),
                         abort: null, hardStop: false };
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
  // 本轮结束：实时数据作废，之后一律按落库消息渲染，避免与正式消息重复
  liveDrop(key);
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
  const docs = state.pendingDocs || [];
  const docLoading = docs.some((d) => d.loading);
  const empty = !$('#input').value.trim() && !state.pendingImage
    && !(state.pendingImages && state.pendingImages.length)
    && !docs.length
    && !(state.pendingPastes && state.pendingPastes.length);
  // 生成中不禁用发送：点发送 / Enter 都进队列，本轮结束后自动发出（不打断当前回复）
  btn.disabled = !!mic.active || !!mic.busy || empty || docLoading;
  // 说清楚「点下去会发生什么」：原来 title 恒为「发送」，用户只能猜
  btn.title = docLoading ? '附件还在读取中，请稍候…'
            : state.streaming ? '排队发送（本轮结束后自动发出）'
            : (compressBusyNow() ? '排队发送（压缩完成后自动发出）'
            : (mic.active ? '正在录音 —— 说完再点发送'
            : (mic.busy ? '正在识别语音…'
            : (empty ? '先输入内容，或附上一张图片 / 文档' : '发送（Enter）'))));
}

const STOP_ACK_TIMEOUT = 5000;      // /api/stop 请求本身的等待上限
const STOP_HARD_TIMEOUT = 8000;     // 本地流：8 秒没收到 done 就主动断开
const REMOTE_STOP_TIMEOUT = 30000;  // 远端任务：30 秒仍未收尾就先解除界面状态

/* 给任意 Promise 加超时；超时/异常都回落到 fallback，不让「停止」把界面锁死。 */
function withinTimeout(p, ms, fallback) {
  return new Promise((resolve) => {
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      resolve(fallback);
    }, ms);
    Promise.resolve(p).then((v) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(v);
    }, () => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(fallback);
    });
  });
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
  const liveTip = st && st.row ? st.row.querySelector('.live-text') : null;
  if (liveTip) liveTip.textContent = '正在停止…';
  toast('正在停止…');
  // 明确点「停止」= 连排队也一起取消，避免用户以为已经停下却又自动发出去
  if (typeof cancelSendQueue === 'function') cancelSendQueue(true);
  if (state.uiMode === 'gal') galStopAudio();   // 停止生成时也打断朗读
  const sidToStop = (st && st.sid) || sidKey;
  const ack = await withinTimeout(api('/api/stop', { session_id: sidToStop }),
                                  STOP_ACK_TIMEOUT, null);
  if (!ack) {
    // 后端没回 ACK（请求排队 / 进程忙）：信号仍可能已被 pending stop 兜住，
    // 这里只记录，不阻塞后面的硬兜底。
    console.warn('[stream] /api/stop 未在 5s 内确认，继续按已发送处理 sid=' + sidToStop);
  }
  if (st) {
    // 本地流硬兜底：后端若卡在无法中断的调用里，到点主动断开 fetch，
    // 让 finally 收尾（清掉「正在输出…」与停止按钮），随后由 attach 把真实结果补回来。
    st.stopTimer = setTimeout(() => {
      const cur = state.streams[String(st.sid)];
      if (!cur || cur !== st || !cur.stopping) return;
      cur.hardStop = true;
      console.warn('[stream] 停止超时，强制断开本地流 sid=' + sidToStop);
      if (typeof cur.abort === 'function') cur.abort();
    }, STOP_HARD_TIMEOUT);
  }
  if (remote && typeof waitJobThenRefresh === 'function') {
    // 没有本地流可收 done：等后端确认收尾后刷新状态（手动停止，不发通知）
    const r = await withinTimeout(waitJobThenRefresh(sidToStop), REMOTE_STOP_TIMEOUT, null);
    if (r && r.ok) {
      state.stopping = false;
      refreshStreamingUI();
      renderMessages();
    } else if (!r) {
      // 后端迟迟不确认：先解除界面上的「生成中」，否则停止键永远点不动。
      if (state.streamingSids) delete state.streamingSids[String(sidToStop)];
      state.stopping = false;
      refreshStreamingUI();
      renderMessages();
      toast('后台任务仍未确认结束，已先解除界面状态；可稍后刷新或再次点停止', 'error');
    }
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
  const c = state.config || {};
  // 自动模式走免注册引擎链，不需要密钥；至少要有一个引擎参与
  if (String(c.search_provider || 'auto') === 'auto' && c.search_modsearch_enabled !== false) {
    const ee = c.search_engine_enabled || {};
    return ee.firecrawl !== false || ee.keenable !== false || ee.youcom !== false;
  }
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
  done: '本轮已完成',
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
             : kind === 'done' ? 'done'
             : kind ? 'warn' : (opts.error ? 'error' : 'done');
  const target = { sessionId: opts.sessionId };
  if (typeof opts.messageIndex === 'number') target.messageIndex = opts.messageIndex;
  showSystemNotify(who + ' · ' + label, clean.slice(0, 90) || ('本轮' + label), tone, target);
}

function syncSearchPill() {
  const pill = $('#btn-search');
  if (!pill) return;
  const ok = hasSearchKey();
  pill.classList.toggle('active', !!state.searchEnabled && ok);
  pill.classList.toggle('disabled', !ok);
}

// 发送受理反馈：点下发送 → 按钮进入转圈态，后端接收请求（拿到响应头）后复原。
// 只覆盖「点击 → 受理」这段空窗；之后的进度由消息内的 .live-bar 表达。
let _sendBusyTimer = 0;
function setSendBusy(on) {
  const btn = $('#btn-send');
  if (_sendBusyTimer) { clearTimeout(_sendBusyTimer); _sendBusyTimer = 0; }
  if (!btn) return;
  btn.classList.toggle('is-busy', !!on);
  if (on) {
    btn.setAttribute('aria-busy', 'true');
    // 兜底：本地模型冷加载可能要十几秒，别让按钮一直转圈（真正的进度仍在消息里）
    _sendBusyTimer = setTimeout(() => setSendBusy(false), 15000);
  } else {
    btn.removeAttribute('aria-busy');
  }
}

// 发送后把焦点还给输入框（触屏设备不抢焦点，避免软键盘反复弹）
function focusComposer(input) {
  if (!input) return;
  try {
    if (window.matchMedia && matchMedia('(pointer: coarse)').matches) return;
    input.focus({ preventScroll: true });
  } catch (e) { /* 忽略：焦点失败不影响发送 */ }
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
  let text = String(
    (replay ? payload.text : (fromQueue ? payload : input.value)) || '').trim();
  // `#web` / `#web <url>`：用户显式指定本轮联网（对齐 Trae 的 #Web）。
  //   `#web 问题`        → 强制本轮开放 web_search，并要求先检索再回答
  //   `#web <url> 问题`  → 直接抓该页正文当上下文，不搜索
  // 标记只用于解析：送给模型的正文里去掉，气泡仍显示你打的原文。
  let webForce = false, webUrl = '';
  let sendText = text;
  if (state.uiMode !== 'gal') {
    const mWeb = /(?:^|\s)#web(?:\s+(https?:\/\/\S+))?\s*/i.exec(text);
    if (mWeb) {
      webForce = true;
      webUrl = mWeb[1] || '';
      sendText = (text.slice(0, mWeb.index) + ' ' +
                  text.slice(mWeb.index + mWeb[0].length)).trim();
      if (!sendText) {
        if (!webUrl) { toast('#web 后面写一句你要查什么'); return; }
        sendText = '请阅读这个页面，概括要点并指出与我任务相关的部分。';
      }
    }
  }
  // 粘贴的长文本块：按顺序并入正文（#web 只解析用户手打的部分）
  const pastes = fromQueue ? [] : (state.pendingPastes || []).slice();
  if (pastes.length) {
    const body = pastes.map((p) => p.text).filter(Boolean);
    if (body.length) {
      text = [text].concat(body).filter(Boolean).join('\n\n');
      sendText = [sendText].concat(body).filter(Boolean).join('\n\n');
    }
  }
  const imgs = fromQueue
    ? (replay ? (payload.imgs || []) : [])
    : ((state.pendingImages && state.pendingImages.length)
      ? state.pendingImages.slice()
      : (state.pendingImage ? [state.pendingImage] : []));
  const img = imgs[0] || null;
  const pendingDocs = fromQueue ? [] : (state.pendingDocs || []);
  if (pendingDocs.some((d) => d.loading)) {
    toast('附件还在读取中，请稍候…', 'error');
    return;
  }
  const docs = fromQueue
    ? (replay ? (payload.docs || []) : [])
    : pendingDocs.filter((d) => d.b64).slice();
  const quotes = fromQueue
    ? (replay ? (payload.quotes || []) : [])
    : (state.pendingQuotes || []);
  // `#` 菜单的上下文引用：只带定位信息，正文由服务端在发送时现取
  const refs = fromQueue
    ? (replay ? (payload.refs || []) : [])
    : ((typeof cmpRefPayload === 'function') ? cmpRefPayload() : []);
  // 正在修改历史消息：文本已经在输入框里，发送时走原有 edit 语义
  // （丢弃该条之后的回复并重新生成，保留版本链），不发成一条新消息
  if (editTarget && !fromQueue
      && editTarget.sid === String(state.sessions.current || '')) {
    if (state.streaming) { toast('正在生成回复，稍后再发送这次修改', 'error'); return; }
    if (!text) return;
    if (webForce) { toast('「修改」不支持 #web，请作为新消息发送', 'error'); return; }
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
    if (!text && !imgs.length && !docs.length && !quotes.length && !refs.length && !planMode) return;
    sendQueue.push({
      __q: true,
      text: text,
      sid: state.sessions.current,
      plan: planMode,
      // 只存引用，不复制 base64 内容；结构对齐 pendingImages，回放时可直接复用
      imgs: imgs.map((it) => ({ mime: it.mime, b64: it.b64, url: it.url, seq: it.seq })),
      docs: docs.map((d) => ({
        name: d.name, mime: d.mime, b64: d.b64, size: d.size, seq: d.seq,
      })),
      quotes: quotes.map((q) => ({ text: q.text })),
      refs: refs.map((r) => Object.assign({}, r)),
    });
    input.value = '';
    autoGrow();
    clearAttach();
    clearQuotes();
    if (typeof clearCtxRefs === 'function') clearCtxRefs();
    if (typeof cmpClearPrecheck === 'function') cmpClearPrecheck();
    if (typeof cmpUpdateHint === 'function') cmpUpdateHint();
    if (typeof cmpUpdateClear === 'function') cmpUpdateClear();
    renderSendQueue();
    focusComposer(input);
    toast(state.streaming ? '已排队，本轮回复结束后自动发送'
                          : '正在压缩较早的历史，已排队，完成后自动发送');
    return;
  }
  if (!text && !imgs.length && !docs.length && !quotes.length && !refs.length && !planMode) return;   // 允许「纯图片 / 纯文档 / 纯引用」
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
    focusComposer(input);
  }
  // 发新消息前，把还处于「预览」的用户消息恢复为当前版本
  if (verPreview && verPreview.sid === state.sessions.current) refreshUserRow(verPreview.index);
  verPreview = null;
  const extra = { content: sendText };
  if (webForce) {
    extra.web_search_force = true;
    if (webUrl) extra.web_fetch_url = webUrl;
  }
  if (planMode) extra.plan_mode = true;
  // 纯附件消息的占位文案：一个文档显示文件名，多个只显示数量
  const docLabel = docs.length === 1 ? ('【附件：' + docs[0].name + '】')
    : (docs.length ? ('【附件 ' + docs.length + ' 个】') : '');
  const imgLabel = imgs.length > 1 ? ('【' + imgs.length + ' 张图片】') : '【图片】';
  const refLabel = refs.length ? ('【引用 ' + refs.length + ' 项】') : '';
  if (state.uiMode === 'gal') {
    extra.mode = 'gal';
    extra.gal_character_id = galState.charId || (curSession() || {}).gal_character_id || '';
    galSetUserEcho(text || docLabel || imgLabel || refLabel);
    galResetDialog();
    galSetStatus('思考中…');          // 必须放在 galResetDialog 之后，否则会被清掉
    galPlayIntake();                  // 200ms 内的「说出口」确认：对白框轻吸一下
  }
  if (imgs.length) extra.images = imgs.map((it) => ({ mime: it.mime, data: it.b64 }));
  if (img) extra.image = { mime: img.mime, data: img.b64 };   // 兼容只认 image 的旧客户端
  if (docs.length) extra.docs = docs.map((d) => ({ name: d.name, mime: d.mime, data: d.b64 }));
  if (quotes.length) extra.quotes = quotes.map((q) => ({ text: q.text }));
  if (refs.length) extra.refs = refs;
  const echo = {};
  if (imgs.length) {
    echo.images = imgs.map((it) => ({ url: it.url, size: Math.round(it.b64.length * 3 / 4) }));
  }
  if (docs.length) echo.docs = docs.map((d) => ({ name: d.name, size: d.size, mime: d.mime }));
  // 用户气泡上的引用 chip：先放一份临时数据，done 事件再用服务端解析结果校正。
  // workspace 只是检索开关，不进 chip（服务端也不会为它生成引用）。
  const echoRefs = refs
    .filter((r) => r && r.type && r.type !== 'workspace' && !r.auto)
    .map((r) => ({
      type: r.type, path: r.path, start: r.start, end: r.end, title: r.title,
      virtual: !(r.type === 'file' || r.type === 'code'),
    }));
  if (echoRefs.length) echo.refs = echoRefs;
  // 刚发出的附件还没有落盘名，先记在内存里，点开预览不必等会话重载
  if (docs.length && typeof rememberSentDoc === 'function') docs.forEach((d) => rememberSentDoc(d));
  if (!fromQueue) setSendBusy(true);          // 点击即反馈：按钮转圈，请求受理后复原
  appendUser(text || (planMode ? '（规划当前任务）' : (docLabel || imgLabel || refLabel)),
             curSession() ? curSession().messages.length : undefined,
             (imgs.length || docs.length || echoRefs.length) ? echo : undefined);
  if (!fromQueue) {
    clearAttach();
    clearQuotes();
    if (typeof clearCtxRefs === 'function') clearCtxRefs();
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

function currentProblems() {
  try {
    const items = (typeof IDE_INTEL !== 'undefined' && IDE_INTEL.items) || [];
    return items.slice(0, 40).map((it) => ({
      path: it.file || it.path || state.ideOpen || '',
      line: it.line || it.startLineNumber || 0,
      severity: it.severity || 'warning',
      message: String(it.message || '').slice(0, 400),
    }));
  } catch (e) {
    return [];
  }
}

/* attach：接管仍在后台运行的任务，把数据源从 /api/chat 换成 /api/chat/attach。
   事件、渲染、收尾全部走下面同一套管线，因此体验与正常发送完全一致
   （思考块 / 工具卡 / 提问卡都实时重建）。 */
async function streamChat(extra, resume, attach) {
  // 本条流归属的会话：之后所有 DOM / 滚动 / 停止都按它判断，
  // 用户切到别的会话不影响这一条继续跑。
  const sid = String(state.sessions.current || '');
  const isCur = () => String(state.sessions.current || '') === sid;
  // 局部遮蔽全局 scrollToBottom：后台会话收流时绝不拽动用户正在看的窗口
  const scrollToBottom = (force) => { if (isCur()) window.scrollToBottom(force); };
  // attach 接管：live 数据是唯一来源，旧数据（若有）先清掉再回放。
  // 历史里的「进度快照行」由 renderMessages 按 live 数据投影替换，不再单独删 DOM。
  if (attach) {
    const oldLive = liveOf(sid, true);
    if (oldLive && oldLive.blocks.length) liveDrop(sid);
  }
  const live = liveOf(sid);
  // live 投影用的工具 FIFO：与 DOM 的 row._toolQueue 同规则，避免同名并行工具错配
  const liveToolQ = {};
  let reasoningRetry = false;
  // AI 占位：先摆一组「呼吸点」表示正在生成，首个 token 到达即换成正文
  // resume：续写模式——复用上一条被打断的气泡，把已有文本当流式起点
  let row;
  let base = '';
  if (resume && resume.row) {
    row = resume.row;
    base = resume.base || '';
    // 续写：把旧的「多段正文 / 提问卡」并回**一段仍然挂在文档里的**正文节点，
    // 否则收尾重排会留下重复内容。
    // 这里以前写的是「删掉第 2 段起的全部，再把 row._content 当保留段」——
    // 但收尾重排后 `_content` 指向的是**最后一段**（buildBlocksView 的 parts 末尾），
    // 多段正文时被删掉的正是它自己：整轮续写的 token 全写进一个脱离文档的节点，
    // 界面一直不变化，只有切会话 / 刷新重建才看得到（现场 00:30 的「继续输出」）。
    let keep = row._content;
    const secs = (row._secs || []).filter((n) => n && n.parentNode);
    if (secs.length && secs.indexOf(keep) < 0) keep = secs[secs.length - 1];
    (row._secs || []).forEach((n) => { if (n !== keep) n.remove(); });
    (row._asks || []).forEach((n) => n.remove());
    if (!keep || !keep.parentNode) {
      // 极端情况（行被别处重排过）：新建一段正文再接流，别把输出写进死节点
      keep = document.createElement('div');
      keep.className = 'content';
      const anchor = row._wrap.querySelector('.msg-actions, .live-bar, .end-note');
      row._wrap.insertBefore(keep, anchor || null);
    }
    row._secs = [keep];
    row._asks = [];
    row._content = keep;
    keep.style.display = '';
    keep.textContent = base;                      // 清掉旧的 markdown 渲染，改为纯文本起点
    row._raw = base;
    const oldBar = row._wrap.querySelector('.msg-actions');
    if (oldBar) oldBar.remove();                  // 移除旧操作条，流结束后统一重建
    row._usageEl = row._usageEl || row._wrap.querySelector('.usage-line') || null;
  } else {
    row = appendAi('', '', undefined);
    // 接管后台续写：新行先把上一轮的计划继承过来，进度不必等模型下次 update_plan
    if (attach && row && typeof planSeedFromSession === 'function') planSeedFromSession(row);
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
  // 状态条常驻在正文下方，直到本轮结束。
  // 每个 delta 都 appendChild 会把节点「移除再插入」，CSS 动画随之重头播一遍（点阵脉冲会抖），
  // 所以只有它不在末尾时才挪。
  const keepLiveLast = () => {
    if (row._wrap && row._wrap.lastElementChild !== typing) row._wrap.appendChild(typing);
  };
  // 探索小队（只读子代理）：独立小卡片，放在正文外层，不会被正文重绘冲掉
  let subagentBox = null;
  let subagentTimer = 0;
  const subagentRows = {};        // id -> {el, done, fail, t0}
  const SUB_ROLE = { searcher: '侦察', reader: '阅读', finder: '定位', worker: '执行',
    critic: '复核', researcher: '调研', reviewer: '审查' };
  // 运行中的行每 0.4 秒刷一次耗时：子代理常常几十秒没有任何事件，
  // 没有秒数用户分不清「在跑」和「卡死」。卡片被重绘掉就自动停表。
  const subAgentTick = () => {
    if (!subagentBox || !subagentBox.isConnected) {
      if (subagentTimer) { clearInterval(subagentTimer); subagentTimer = 0; }
      return;
    }
    Object.keys(subagentRows).forEach((id) => {
      const s = subagentRows[id];
      if (s.done || !s.el) return;
      const t = s.el.querySelector('.subagent-time');
      if (t) t.textContent = ((Date.now() - s.t0) / 1000).toFixed(1) + 's';
    });
  };
  const subagentSummary = () => {
    if (!subagentBox) return;
    const ids = Object.keys(subagentRows);
    const done = ids.filter((id) => subagentRows[id].done).length;
    const fail = ids.filter((id) => subagentRows[id].fail).length;
    const sum = subagentBox.querySelector('.subagent-sum');
    if (sum) {
      sum.textContent = '完成 ' + done + '/' + ids.length + (fail ? ' · 失败 ' + fail : '');
    }
  };
  const ensureSubagentBox = () => {
    if (subagentBox) return subagentBox;
    if (state.uiMode === 'gal' || !isCur()) return null;
    const box = document.createElement('div');
    box.className = 'subagent-card';
    box.innerHTML = '<div class="subagent-head">'
      + '<button type="button" class="subagent-fold" title="收起 / 展开">'
      + '<span class="subagent-caret">▾</span></button>'
      + '<span class="subagent-title">探索小队</span>'
      + '<span class="subagent-sum">启动中…</span></div>'
      + '<div class="subagent-body"></div>';
    const fold = box.querySelector('.subagent-fold');
    fold.onclick = () => {
      const folded = box.classList.toggle('folded');
      fold.querySelector('.subagent-caret').textContent = folded ? '▸' : '▾';
    };
    row._wrap.insertBefore(box, typing);
    subagentBox = box;
    if (!subagentTimer) subagentTimer = setInterval(subAgentTick, 400);
    return box;
  };
  const subagentRow = (id) => {
    const box = ensureSubagentBox();
    if (!box) return null;
    if (!subagentRows[id]) {
      const r = document.createElement('div');
      r.className = 'subagent-row running';
      r.innerHTML = '<div class="subagent-top">'
        + '<span class="subagent-dot"></span>'
        + '<span class="subagent-role"></span>'
        + '<div class="subagent-main">'
        + '<div class="subagent-ask"></div>'
        + '<div class="subagent-step">排队中…</div>'
        + '</div>'
        + '<span class="subagent-time"></span>'
        + '<span class="subagent-ico">⋯</span>'
        + '</div>'
        + '<div class="subagent-detail">'
        + '<div class="subagent-metrics"></div>'
        + '<div class="subagent-result"></div>'
        + '</div>';
      // 点一行展开：看完整问题、指标与结论预览（默认收起，不抢正文）
      r.querySelector('.subagent-top').onclick = () => {
        const d = r.querySelector('.subagent-detail');
        const next = !foldIsOpen(r, 'open', true);
        foldTransition(r, d, next, { cls: 'open', positive: true, ms: 180 });
      };
      box.querySelector('.subagent-body').appendChild(r);
      subagentRows[id] = { el: r, done: false, fail: false, t0: Date.now() };
    }
    return subagentRows[id];
  };
  // 注意：不能叫 liveText —— 会遮蔽全局的 liveText(L, t)，回放 delta 时
  // 调用的就变成这个 DOM 元素，抛 TypeError 把整条流打断。
  const liveTip = typing.querySelector('.live-text');
  const liveSec = typing.querySelector('.live-sec');
  const setLive = (t) => {
    if (liveTip && t && liveTip.textContent !== t) liveTip.textContent = t;
  };
  const setLiveSec = (s) => {
    if (liveSec) liveSec.textContent = s ? String(s) : '';
  };
  if (state.uiMode === 'gal') typing.style.display = 'none';   // GAL 有独立状态行
  const cur = document.createElement('span');
  cur.className = 'cursor';
  // 输出期间给正文挂一个 streaming 标记：光标呼吸 / 新块淡入都只在这段时间生效
  if (c && c.classList) c.classList.add('streaming');

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
  // 逐帧「追赶」：acc 是「已收到」，revealPos 是「已上屏」。
  // 网络一次可能给几百字，直接贴屏就是一块一块往外蹦；这里按 ~2-4 字/帧 稳定写入，
  // 积压多了自动加速（上限 48 字/帧），保证观感连续又绝不落后于网络。
  let revealPos = base.length;
  let revealRaf = 0;
  let paintTimer = 0;
  let lastPaintAt = 0;
  // 重绘节流：短板 32ms（≈30fps，与原生流式输出同档），短尾/长尾/后台自动降频。
  // 以前固定 90ms，肉眼能看出「一段一段往外蹦」。
  const PAINT_MS = 32;
  const PAINT_MS_LONG = 56;         // 尾巴偏长：解析成本上来了，降一档
  const PAINT_MS_FENCE = 90;        // 代码围栏未闭合：每帧重解析代价最高，再降一档
  const PAINT_MS_HIDDEN = 200;      // 标签页不可见：让出主线程，回来后立即补一帧
  const TAIL_RENDER_MAX = 4000;     // 尾巴超过这个长度就不逐帧渲染（避免长代码块反复高亮）
  let tailFence = false;            // 尾巴是否落在未闭合的代码围栏里（决定重绘频率）
  // 尾巴纯文本判定：没有换行、没有 Markdown 标记、也不是列表项开头。
  const TAIL_PLAIN_RE = /^[^\n`*_~[\]<>#|!\\]*$/;
  const tailIsPlain = (t) => !!t && TAIL_PLAIN_RE.test(t)
    && !/^\s*(?:[-+]|\d+[.)])\s/.test(t);
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
  let doneMsgIdx = null;        // 本轮回复在会话里的消息下标（点击通知时精确定位）
  let streamError = null;       // 流式请求异常（用于后台通知）
  let endReason = null;         // 非正常结束时服务端给的原因（长度/网络/轮数/手动停止…）
  let sawError = false;         // 收到过 error 事件：此时不再接受 replace 覆盖
  // 最近一条错误文案。**不立即写进正文**：上游失败后通常会重试，重试成功时
  // 这句错误会残留成一个独立段落（用户看到的「先出现一段中断文字，过一会儿合并」）。
  // 只有整轮结束时一句正文都没有，才把它补进正文，避免空气泡。
  let lastErrText = '';
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

  // 后台标签页被冻结时定时器会停摆：切回前台先重置计时基准，
  // 否则会把「刚才没在计时」误判成「连接断了」，一回来就闪一次中断提示。
  const onVisible = () => {
    if (document.hidden) return;
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
      flowWrap.classList.remove('has-fail');
      row._flowLabel.textContent = '过程 · 进行中';
      if (row._flowMeta) row._flowMeta.textContent = '';
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
    // 折叠头上的一行统计（N 步 / N 步失败）：收起时也能看出这一轮做了多少事
    const meta = document.createElement('span');
    meta.className = 'process-fold-meta';
    const caret = document.createElement('span');
    caret.className = 'process-fold-caret';
    caret.textContent = '▾';
    head.appendChild(title);
    head.appendChild(meta);
    head.appendChild(caret);
    const body = document.createElement('div');
    body.className = 'chat-flow process-fold-body';
    flowWrap.appendChild(head);
    flowWrap.appendChild(body);
    row._wrap.insertBefore(flowWrap, row._content);
    row._flowWrap = flowWrap;
    row._flowHead = head;
    row._flowLabel = title;
    row._flowMeta = meta;
    row._flowCaret = caret;
    const toggle = () => {
      flowToggled = true;
      const next = !foldIsOpen(flowWrap, 'collapsed');
      foldTransition(flowWrap, body, next, { cls: 'collapsed' });
      caret.textContent = next ? '▾' : '▸';
      head.setAttribute('aria-expanded', next ? 'true' : 'false');
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
      row._flowMeta = null;
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
    // 折叠头统计：做了几步、几步失败（失败标红，收起也看得见）
    const stats = (typeof processStats === 'function')
      ? processStats(flow) : { steps: 0, failed: 0 };
    if (row._flowMeta) {
      row._flowMeta.textContent = (typeof flowMetaText === 'function') ? flowMetaText(stats) : '';
    }
    flowWrap.classList.toggle('has-fail', stats.failed > 0);
    // 有失败就不自动收：错误必须留在视线里，否则用户以为一切正常
    if (stats.failed > 0) return;
    if (!flowToggled) {
      // 延迟 600ms 再收：让用户看清最后一步的完成状态，而不是收尾瞬间「啪」地消失
      const wrap = flowWrap;
      setTimeout(() => {
        if (!wrap || !wrap.isConnected || flowToggled) return;
        // 收起也走统一的高度过渡；高度落定后再补偿贴底，避免视图停在半路
        foldTransition(wrap, flow, false, {
          cls: 'collapsed',
          done: () => {
            if (_followBottom && typeof isAtBottom === 'function' && isAtBottom(120)) {
              scrollToBottom(true);
            }
          },
        });
        if (row._flowCaret) row._flowCaret.textContent = '▸';
        if (row._flowHead) row._flowHead.setAttribute('aria-expanded', 'false');
      }, 600);
    }
  };

  // 收尾当前思考块：空的直接去掉，别留一个空壳占位；
  // 非空的标题换成一行摘要（点开还能看全文）。
  const closeThink = (label) => {
    if (!thinkBox) return;
    const text = thinkBox._body.textContent || '';
    if (!text.trim()) thinkBox.remove();
    else {
      foldTransition(thinkBox, thinkBox._body, false, { cls: 'open', positive: true, ms: 180 });
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

  // 用户正在尾巴区里选字：这一帧不能重建，否则 innerHTML 会把选区清掉。
  // 松手（选区塌陷 / 移出）后下一帧自动补上，最多晚 120ms。
  const selectionInside = (node) => {
    if (!node) return false;
    try {
      const sel = window.getSelection && window.getSelection();
      if (!sel || sel.isCollapsed || !sel.rangeCount) return false;
      const r = sel.getRangeAt(0);
      return node.contains(r.startContainer) || node.contains(r.endContainer);
    } catch (e) { return false; }
  };

  const paintLive = () => {
    paintTimer = 0;
    if (!liveBox) return;
    if (selectionInside(tailBox)) { paintTimer = setTimeout(paintLive, 120); return; }
    lastPaintAt = Date.now();
    // 用跟随状态而不是当前高度：这一帧内容已经长高了，再算 isAtBottom 会误判成「用户在看历史」
    const wasAtBottom = isCur() && _followBottom;
    const view = acc.slice(0, revealPos);      // 只渲染「已经追上来的」部分
    const { cut, fence } = scanLive(view, liveRendered);
    tailFence = fence;
    let next = cut;
    // 尾巴太长说明模型卡在一个超长段落 / 未闭合代码块里：
    // 围栏外就整段推进定稿区（只切到最后一行，半行留在尾巴），
    // 围栏内则退回纯文本，避免每帧高亮几千行代码。
    if (view.length - next > TAIL_RENDER_MAX && !fence) {
      const nl = view.lastIndexOf('\n');
      if (nl >= next) next = nl + 1;
    }
    if (next > liveRendered) {
      const html = safeRenderMarkdown(view.slice(liveRendered, next));
      if (html) {
        liveBox.insertAdjacentHTML('beforeend', html);
        if (window.enhanceRichMedia) window.enhanceRichMedia(liveBox);
      }
      liveRendered = next;
    }
    const tail = holdBackPartialMarker(view.slice(liveRendered));
    if (tail.length > TAIL_RENDER_MAX) {
      tailBox.className = 'md-tail plain';
      tailBox.textContent = tail;
    } else if (!fence && tailIsPlain(tail)) {
      // 纯文本快通道：没有换行、也没有任何 Markdown 标记时直接写 textContent。
      // 省掉每帧的 Markdown 解析 + innerHTML 重建（长回答时这是主要的掉帧来源）；
      // 单行纯文本用两种方式渲染结果一致，不会出现「先原始后变格式」的跳变。
      tailBox.className = 'md-tail plain';
      tailBox.textContent = tail;
    } else {
      tailBox.className = 'md-tail';
      tailBox.innerHTML = safeRenderMarkdown(tail);
      if (window.enhanceRichMedia) window.enhanceRichMedia(tailBox);
    }
    // 输出阶段把「已经上屏多少字」带上，让用户看到确实在推进
    if (phase === 'output') setLive('正在输出 · ' + revealPos + ' 字');
    if (wasAtBottom) {
      if (typeof followScroll === 'function') followScroll();
      else scrollToBottom(true);
    }
  };

  // 重绘间隔自适应：短尾按 30fps 追；长尾 / 未闭合代码围栏 / 后台标签页逐档降频，
  // 保证「看起来连续」的同时不把主线程占满。
  const paintGapFor = (tailLen) => {
    if (document.hidden) return PAINT_MS_HIDDEN;
    if (tailFence) return PAINT_MS_FENCE;
    if (tailLen > 1200) return PAINT_MS_LONG;
    return PAINT_MS;
  };
  const schedulePaint = () => {
    if (paintTimer) return;
    const gap = paintGapFor(Math.max(0, acc.length - liveRendered));
    const wait = Math.max(0, gap - (Date.now() - lastPaintAt));
    paintTimer = setTimeout(paintLive, wait);
  };

  // 揭示节奏：积压越多追得越快。2 字/帧 ≈ 120 字/秒 是「读起来像在写」的舒适区，
  // 48 字/帧 ≈ 2900 字/秒 是兜底上限；两者都远低于网络到达速度时才需要追赶。
  const REVEAL_MIN = 2;
  const REVEAL_MAX = 48;
  const revealTick = () => {
    revealRaf = 0;
    if (!liveEnabled || !liveBox) return;
    const backlog = acc.length - revealPos;
    if (backlog <= 0) return;
    const step = Math.min(REVEAL_MAX, Math.max(REVEAL_MIN, Math.ceil(backlog / 8)));
    revealPos = Math.min(acc.length, revealPos + step);
    schedulePaint();
    if (revealPos < acc.length) revealRaf = requestAnimationFrame(revealTick);
  };
  const startReveal = () => {
    if (!liveEnabled || revealRaf) return;
    if (revealPos >= acc.length) return;
    revealRaf = requestAnimationFrame(revealTick);
  };
  // 需要「立刻全部上屏」的时刻（提问卡插入、收尾重排、停止输出）：别让动画拖住逻辑
  const flushReveal = () => {
    if (revealRaf) { cancelAnimationFrame(revealRaf); revealRaf = 0; }
    if (revealPos !== acc.length) revealPos = acc.length;
    if (liveEnabled && liveBox) paintLive();
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
    startReveal();               // 按帧把新到的文字「写」上屏
  };

  // 提问卡落在「真正被问出来」的位置：先把当前正文段收尾并上屏，卡片插在它后面，
  // 之后的内容另起一段正文 —— 否则卡片会被后续输出顶到消息最下方（历史重排也一样）。
  const splitAtAsk = (card) => {
    if (paintTimer) { clearTimeout(paintTimer); paintTimer = 0; }
    flushReveal();                                    // 尾巴先落屏，别丢字
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
        // 15 秒一个字节都没有（连 tick 都没有）：这条连接已经死了，
        // 继续 await reader.read() 会让 finally 永远不执行 —— 「正在输出…」残留、
        // 停止键也点不动。这里主动断开，交给 finally → keepRecovering 补状态。
        if (ac) {
          console.warn('[stream] 15s 无事件，断开会话流以触发恢复 sid=' + sid);
          try { ac.abort(); } catch (e) { /* 已断开 */ }
          localAborted = true;
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

  // fetch 的中断句柄：停止超时 / 连接长时间无事件时主动断开，
  // 保证 finally 一定执行（清掉「正在输出…」），而不是无限 await reader.read()。
  const ac = (typeof AbortController === 'function') ? new AbortController() : null;
  let localAborted = false;     // 本端主动断开：不计入「请求失败」文案
  let busyRejected = false;     // 服务端回 409（已在生成 / 正在压缩）：本条没有落库
  let busyMessage = '';         // 409 的服务端说明（提示用户为什么不发）
  // 非 2xx 的明确拒绝（400/403/404/500…）：请求根本没被受理，后台不会有任务在跑。
  // 不能把它当成「连接提前结束」去挂 attach 轮询，否则界面会假进入「后台运行中」。
  let httpRejected = false;
  let attachGone = false;       // 接管模式：服务端已没有运行中的任务
  if (stRef) {
    stRef.abort = () => {
      localAborted = true;
      try { if (ac) ac.abort(); } catch (e) { /* 已断开 */ }
    };
  }

  try {
    let res;
    if (attach) {
      // 接管模式：不发送新消息，只从 since 回放服务端已产生的事件，再继续跟随到 done
      res = await fetch('/api/chat/attach', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Auth-Token': TOKEN },
        signal: ac ? ac.signal : undefined,
        body: JSON.stringify({ session_id: sid, since: attach.since || 0 }),
      });
      if (!res.ok || !res.body) {
        // 404 = 没有可接管的任务（已经结束 / 已过期）：按正常收尾，不往正文写错误
        const gone = new Error('没有正在运行的任务');
        gone.__attachGone = true;
        throw gone;
      }
    } else {
      res = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Auth-Token': TOKEN },
        signal: ac ? ac.signal : undefined,
        body: JSON.stringify(Object.assign({
          session_id: state.sessions.current,
          think_enabled: !!state.thinkEnabled,
          think_level: state.thinkLevel || 'high',
          search_enabled: !!state.searchEnabled,
          ui_context: currentUiContext() || undefined,
          problems: (extra && (
            (typeof extra.content === 'string' && extra.content.indexOf('@problems') >= 0)
            || (Array.isArray(extra.refs) && extra.refs.some((r) => r && r.type === 'problems'))
          )) ? currentProblems() : undefined,
        }, extra || {})),
      });
      if (!res.ok) {
        let msg = 'HTTP ' + res.status;
        httpRejected = true;
        try {
          const j = await res.json();
          if (j && j.message) msg = j.message;
          else if (j && j.error === 'busy') msg = '该会话正在生成中，请稍候。';
          // 409 = 这一条根本没进后端：本地追加的用户消息是幽灵数据，
          // 收尾时要按服务端会话重画，并把「生成中 + 停止键」恢复出来。
          if (res.status === 409 || (j && j.error === 'busy')) {
            busyRejected = true;
            busyMessage = msg;
          }
        } catch (e) { /* 非 JSON 响应，忽略 */ }
        throw new Error(msg);
      }
      setSendBusy(false);              // 后端已受理（拿到响应头），发送按钮复原
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
        if (stRef) stRef.lastEvent = lastEventAt;   // 手动压缩据此判断「本轮是否还在跑」
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
          docChipRender(obj, 'parsing');
          if (state.uiMode === 'gal') galSetStatus('正在解析附件…');
          const _cnt = obj.count ? ('（' + ((Number(obj.index) || 0) + 1) + '/' + obj.count + '）') : '';
          setLive('正在解析附件' + _cnt + '｜' + (obj.name || '附件'));
          scrollToBottom();
        } else if (obj.type === 'doc_progress') {
          // 逐页进度：长 PDF / 扫描件解析可能要几十秒，状态条必须持续动，否则像卡死
          docChipRender(obj, 'progress');
          if (state.uiMode === 'gal') {
            galSetStatus('正在解析附件…' + (obj.total ? ('第 ' + obj.done + '/' + obj.total + ' 页') : ''));
          }
          if (obj.total) {
            const stageLabel = ({ text: '读取', render: '渲染', ocr: '识别' })[obj.stage] || '解析';
            setLive('正在' + stageLabel + '附件 ' + obj.done + '/' + obj.total + ' 页｜' +
                    (obj.name || ''));
          }
        } else if (obj.type === 'doc_ok') {
          docChipRender(obj, obj.error ? 'error' : 'ok');
          if (obj.warn && !obj.error) toast('附件已读入：' + obj.warn);
          scrollToBottom();
        } else if (obj.type === 'recognizing') {
          const tip = document.createElement('span');
          tip.className = 'recognize-tip';
          tip.innerHTML = icon('image')
            + (obj.mode === 'vision' ? ' 正在识别图片内容…' : ' 正在看图…');
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
          setUsage(row._usageEl, obj.usage, row._doneAt,
                   usageCalls({ timing: row._timing || {} }), row._timing || null);
          if (state.uiMode === 'gal') galExecUsage(obj.usage);
        } else if (obj.type === 'timing') {
          // 请求链路埋点：分段耗时（load / assemble / doc / compress / first_token / total），
          // 只输出到控制台供排查，不打扰界面；同时留给用量行标「N 次调用累计」。
          row._timing = obj.timing || null;
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
          followProcessStep(thinkBox);
        } else if (obj.type === 'subagent_start') {
          const s = subagentRow(obj.id);
          if (s) {
            s.t0 = Date.now();
            s.el.style.setProperty('--depth', String(Math.max(0, Number(obj.depth) || 0)));
            const role = s.el.querySelector('.subagent-role');
            role.textContent = obj.name || SUB_ROLE[obj.role]
              || (obj.role ? String(obj.role) : '子代理');
            role.dataset.role = obj.role || 'reader';
            const askEl = s.el.querySelector('.subagent-ask');
            askEl.textContent = obj.ask || '';
            askEl.title = obj.ask || '';
            s.el.querySelector('.subagent-step').textContent =
              (obj.fork && obj.prefix_tokens)
                ? ('继承上下文 ' + Math.round(obj.prefix_tokens / 1000) + 'k …')
                : '开始…';
            subagentSummary();
            if (s && s.el) followProcessStep(s.el);
            else scrollToBottom();
          }
        } else if (obj.type === 'subagent_step') {
          const s = subagentRows[obj.id];
          if (s && s.el) {
            s.el.querySelector('.subagent-step').textContent =
              (obj.tool || '') + (obj.target ? ' · ' + obj.target : '');
          }
        } else if (obj.type === 'subagent_done') {
          const s = subagentRows[obj.id];
          if (s && s.el) {
            const ok = !!obj.ok;
            s.done = true;
            s.fail = !ok;
            s.el.classList.remove('running');
            s.el.classList.add(ok ? 'done' : 'fail');
            s.el.querySelector('.subagent-ico').textContent = ok ? '✓' : '✕';
            const secs = ((obj.ms || 0) / 1000).toFixed(1) + 's';
            s.el.querySelector('.subagent-time').textContent = secs;
            s.el.querySelector('.subagent-step').textContent = ok
              ? ('完成 · ' + secs + ' · ' + (obj.rounds || 0) + ' 轮 · '
                 + (obj.tools || 0) + ' 次工具 · ' + (obj.chars || 0) + ' 字')
              : (obj.error || '失败');
            // 展开后的指标栏：只放这一行真正有的数，不补 0 占位
            const mt = [['轮次', obj.rounds || 0], ['工具', obj.tools || 0],
                        ['字数', obj.chars || 0]];
            if (obj.tokens) mt.push(['token', obj.tokens]);
            if (obj.evidence != null) mt.push(['证据分', obj.evidence]);
            if (obj.missing) mt.push(['缺失引用', obj.missing]);
            s.el.querySelector('.subagent-metrics').innerHTML = mt.map((x) =>
              '<span class="subagent-metric"><b>' + escapeHtml(String(x[1])) + '</b>'
              + escapeHtml(String(x[0])) + '</span>').join('');
            const res = s.el.querySelector('.subagent-result');
            res.textContent = ok ? String(obj.preview || '') : String(obj.error || '');
            subagentSummary();
          }
        } else if (obj.type === 'subagent_all') {
          if (subagentBox) {
            const title = subagentBox.querySelector('.subagent-title');
            if (title) title.textContent = '探索小队 · ' + (obj.n || 0) + ' 个';
            const sum = subagentBox.querySelector('.subagent-sum');
            if (sum) {
              sum.textContent = '完成 ' + (obj.ok != null ? obj.ok : 0) + '/'
                + (obj.n || 0) + ' · ' + (((obj.ms || 0) / 1000).toFixed(1)) + 's'
                + (obj.tokens ? ' · ' + obj.tokens + ' token' : '')
                + (obj.accept === false ? ' · 验收未过' : '');
            }
            subagentBox.classList.add('all-done');
          }
        } else if (obj.type === 'auto_continue') {
          // 自动续跑：在过程流里留一条轻提示，让用户知道不是卡住了，是它自己在往下做
          const tip = document.createElement('div');
          tip.className = 'auto-continue';
          tip.textContent = '自动续跑 ' + obj.n + '/' + (obj.limit < 0 ? '∞' : obj.limit)
            + (obj.why ? ' · ' + obj.why : '');
          ensureFlow().appendChild(tip);
          followProcessStep(tip);
        } else if (obj.type === 'reasoning') {
          retryWait = null;
          if (!thinkBox) {
            thinkBox = buildThinkBlock('', true);
            thinkBox._step = row._toolStep || 1;
            thinkBox._label.textContent = '步骤 ' + thinkBox._step + ' · 思考中…';
            ensureFlow().appendChild(thinkBox);
          }
          thinkBox._body.textContent += obj.text;
          liveReasoning(live, obj.text, reasoningRetry);
          reasoningRetry = false;
          refreshThinkLabel(thinkBox, false);   // 标题 / 状态条显示「最近完成段落」的首行
          phase = 'output';          // 思考内容在流，不再需要秒数
          if (state.uiMode === 'gal') { galSetStatus('思考中…'); galThinkAppend(obj.text); }
          followProcessStep(thinkBox);
        } else if (obj.type === 'base') {
          // 接管续写任务时的补帧：续写起点正文（上一轮已写好的部分）先摆出来，
          // 否则新行只显示本次新增的段落，看起来像「上一轮输出丢了」
          const bt = String(obj.text || '');
          if (!acc && bt) {
            acc = bt;
            row._raw = bt;
            revealPos = acc.length;      // 续写起点整段直接可见，不做逐字动画
            liveText(live, bt);
            pushText(bt);
            scrollToBottom();
          }
        } else if (obj.type === 'delta') {
          retryWait = null;
          if (thinkBox) closeThink('步骤 ' + (thinkBox._step || 1) + ' · 已思考');
          setLive('正在输出');
          setLiveSec('');
          phase = 'output';
          acc += obj.text;
          liveText(live, obj.text);
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
          liveText(live, t);
          pushText(t);
          if (state.uiMode === 'gal') galExecLine(String(obj.message || '').slice(0, 60), 'warn');
          scrollToBottom();
        } else if (obj.type === 'rewind') {
          // 整步重试：把本次失败尝试已经流出来的正文从界面与 acc 里回滚。
          // 只回滚「本次尝试」的字符数，前面成功轮次的内容保留。
          const n = Math.max(0, parseInt(obj.chars, 10) || 0);
          if (n > 0 && acc) {
            acc = acc.slice(0, Math.max(0, acc.length - n));
            revealPos = Math.min(revealPos, acc.length);
            liveTrim(live, n);
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
              foldTransition(thinkBox, thinkBox._body, false, {
                cls: 'open', positive: true, ms: 180,
              });
              const head = thinkLineSummary(text, true);
              thinkBox._hasSummary = !!head;
              thinkBox._label.textContent = '上次尝试（上游中断）'
                + (head ? ' · ' + head : '');
            }
            thinkBox = null;
          }
          reasoningRetry = true;
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
        } else if (obj.type === 'model_wait') {
          // 请求已发出、还没拿到响应头：这段时间只有心跳，必须明确说在等什么
          phase = 'think';
          phaseAt = Date.now();
          setLive('等待上游响应…');
          setLiveSec(obj.timeout ? ('最长 ' + obj.timeout + 's') : '');
          lastContentAt = Date.now();
          contentStaleShown = false;
        } else if (obj.type === 'model_ready') {
          setLive('上游已响应，正在生成…');
          setLiveSec('');
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
          // 错误只放在实时状态条与「结束原因」里，不进正文：
          // 重试成功时正文保持干净；整轮真失败时由收尾逻辑补回（见 finally）。
          lastErrText = String(obj.message || '');
          setLive(lastErrText);
          if (state.uiMode === 'gal') { galExecLine('被打断了…' + lastErrText.slice(0, 60), 'err'); galSetStatus(''); }
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
          livePlan(live, obj.plan || obj);
          if (typeof planRender === 'function') planRender(row, obj);
          scrollToBottom();
        } else if (obj.type === 'plan_review') {
          // 计划审批：后端已挂起，等这里点「批准并执行 / 按意见重排」
          if (typeof planReviewRender === 'function') planReviewRender(row, obj);
          setLive('计划待你批准');
          setLiveSec('');
          keepLiveLast();
          waitingApproval = true;
          phase = 'idle';
          scrollToBottom();
        } else if (obj.type === 'plan_review_done') {
          if (typeof planReviewDone === 'function') planReviewDone(row, obj);
          scrollToBottom();
        } else if (obj.type === 'acceptance_review') {
          // 验收子代理的判定卡（独立 AI 打分 + 证据 + 差距）
          if (typeof acceptanceReviewRender === 'function') {
            acceptanceReviewRender(row, obj.report || {});
          }
          scrollToBottom();
        } else if (obj.type === 'review_tool') {
          // 验收代理正在做核对（读文件 / 跑命令），只是状态提示
          setLive('验收代理正在核对 · ' + String(obj.name || '工具'));
          setLiveSec('');
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
          followProcessStep(toolPending);
        } else if (obj.type === 'script_log') {
          // Code Mode 脚本的实时日志：只更新状态条，不写进正文
          setLive('脚本 · ' + String(obj.text || '').slice(0, 60));
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
          const _li = liveTool(live, obj.name, obj.args);
          (liveToolQ[obj.name] = liveToolQ[obj.name] || []).push(_li);
          // GAL 的执行区本来就是单行紧凑式：结果保持直接展示（collapse=false）
          const card = buildToolCard(obj.name, obj.args, !useGal);
          if (useGal) {
            $('#gal-tools').appendChild(card);
          } else {
            const g = toolGroup(row);        // 归进「步骤组」，标题实时更新
            // 新步骤自动展开：上一步收起的组如果又被复用，先展开再放卡片
            if (g.collapseTimer) { clearTimeout(g.collapseTimer); g.collapseTimer = 0; }
            if (g.el.classList.contains('collapsed')) setToolGroupOpen(g, true, false);
            g.body.appendChild(card);
            g.pending++;
            // 记下当前步骤，组头显示「正在修改 xxx」而不是笼统的「运行中…」
            g.current = { name: obj.name,
                          arg: (typeof toolArgText === 'function'
                                ? toolArgText(obj.name, obj.args) : '') };
            toolGroupTitle(g);
            markProcessStep(card);           // 当前步骤高亮（并行时多张同时亮）
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
          if (useGal) scrollToBottom();
          else followProcessStep(card);    // 只把新步骤带进视野，不强行贴整条消息底部
        } else if (obj.type === 'tool_output') {
          // 命令执行中的增量输出：找当前还在跑的那张卡（同名按队列取最早的）
          const q = row._toolQueue && row._toolQueue[obj.name];
          const card = q && q.length ? q[0] : null;
          if (card && typeof appendToolOutput === 'function') {
            appendToolOutput(card, obj.text);
          }
        } else if (obj.type === 'tool_done') {
          const _lq = liveToolQ[obj.name];
          liveToolDone(live, _lq && _lq.length ? _lq.shift() : -1, obj);
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
            // 这一组都跑完了：延迟收起已完成的步骤（有失败 / 用户手动展开过时
            // autoCollapseToolGroup 自己会拒绝，错误始终留在视线里）
            if (g.pending === 0) {
              if (g.collapseTimer) clearTimeout(g.collapseTimer);
              g.collapseTimer = setTimeout(() => {
                g.collapseTimer = 0;
                if (g.pending === 0) autoCollapseToolGroup(g);
              }, 1200);
            }
          }
          // 同一批还有工具在跑就继续计时；全跑完则回到「等模型」
          setLive((row._toolGroup && row._toolGroup.pending > 0) ? '并行执行中' : '继续思考');
          phase = (row._toolGroup && row._toolGroup.pending > 0) ? 'tool' : 'think';
          phaseAt = Date.now();
          if (state.uiMode === 'gal') galSetStatus('思考中…');
          ideOnToolDone(obj, row, sid);
          if (state.uiMode === 'gal' || !card) scrollToBottom();
          else followProcessStep(card);
        } else if (obj.type === 'diagram') {
          // archify 产物：对话内嵌一张可交互图卡（URL 免鉴权、仅本机可访问）
          const item = obj.item || {};
          if (item.url && row._wrap && typeof buildDiagramCard === 'function') {
            let dup = false;
            row._wrap.querySelectorAll('.diagram-card').forEach((n) => {
              if (n.dataset.url === item.url) dup = true;
            });
            if (!dup) {
              const card = buildDiagramCard(item);
              card.dataset.url = item.url;
              row._wrap.appendChild(card);
              scrollToBottom();
            }
          }
        } else if (obj.type === 'ask') {
          if (!askedNotified && chatWindowAway()) {
            askedNotified = true;
            const qs = obj.questions || [];
            const title = qs.length > 1
              ? 'AI 有 ' + qs.length + ' 个问题等你回答' : 'AI 在等你回答';
            const body = String((qs[0] || {}).question || '').slice(0, 90);
            if (typeof notifyOnce === 'function') {
              notifyOnce(obj.qid ? 'ask:' + obj.qid : '', title, body, 'ask',
                         { sessionId: sid });
            } else if (typeof showSystemNotify === 'function') {
              showSystemNotify(title, body, 'ask', { sessionId: sid });
            }
          }
          const askMeta = obj.expires_in
            ? { expiresAt: Date.now() + Math.max(1, Number(obj.expires_in) || 0) * 1000 }
            : {};
          liveAsk(live, liveTool(live, 'ask_user', {}), obj.questions, obj.qid);
          const askCard = buildAskGroup(obj.questions, obj.qid, askMeta);
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
            if (typeof obj.msg_idx === 'number') doneMsgIdx = obj.msg_idx;
            row._doneAt = obj.done_at || row._doneAt;
            const timingNow = obj.timing || row._timing || null;
            // 没有 usage（例如本轮没产出 token）时也要显示「完成耗时」：只建一行即可
            if (!row._usageEl && (row._usageData
                || (timingNow && Number(timingNow.total_ms) > 0))) {
              row._usageEl = document.createElement('div');
              row._usageEl.className = 'usage-line';
              row._wrap.appendChild(row._usageEl);
            }
            if (row._usageEl && (row._usageData || timingNow)) {
              // 输出完成时间 + 完成耗时挂在缓存命中后面：排查「这轮什么时候结束 / 花了多久」
              setUsage(row._usageEl, row._usageData || null, row._doneAt,
                       usageCalls({ timing: timingNow || {} }), timingNow);
            }
            clearToolPending();
            phase = 'idle';
            doneBlocks = Array.isArray(obj.blocks) ? obj.blocks : null;
            doneTiming = obj.timing || null;
            doneTools = Array.isArray(obj.tools) ? obj.tools : null;
            if (thinkBox) closeThink();          // 最后一个思考块落定，标题换成摘要
            foldLiveProcess();                   // 整个过程折成「过程 · 工作 X 秒」
            // 本轮查到的文件 / 代码引用：自动检索的放在回答下方；
            // 显式引用已经在用户气泡上，这里用服务端解析结果校正一次（刷新前后一致）。
            if (typeof setUserRefs === 'function') {
              const prev = row.previousElementSibling;
              if (prev && prev.classList && prev.classList.contains('user')) {
                setUserRefs(prev, (obj.references || []).filter((r) => r && !r.auto));
              }
            }
            if (obj.references && obj.references.length
                && typeof buildReferencesBlock === 'function' && !row._refsBlock) {
              const autoRefs = obj.references.filter((r) => r && r.auto);
              if (autoRefs.length) {
                row._refsBlock = buildReferencesBlock(autoRefs);
                row._wrap.appendChild(row._refsBlock);
              }
            }
            // 本轮结束：把工具轨迹折成一行摘要（用户手动展开过则保持展开）
            if (row._toolGroup) autoCollapseToolGroup(row._toolGroup);
            if (typeof planAutoCollapse === 'function') planAutoCollapse(row);
            // 变更审阅：把本轮写过的文件一次性交给用户确认（撤销 / 保留）
            if (typeof ideReviewEndTurn === 'function') ideReviewEndTurn(row, false, sid);
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
    if (localAborted || (e && e.name === 'AbortError')) {
      // 本端主动断开（停止超时 / 15s 无事件）：不是请求失败，不要往正文里塞错误文案。
      streamError = null;
      console.warn('[stream] 已断开本地流 sid=' + sid
        + (stRef && stRef.hardStop ? '（停止超时兜底）' : '（连接无响应）'));
    } else if (busyRejected) {
      // 409：这条请求后端根本没接收，正文不该出现「请求失败」占位。
      streamError = null;
    } else if (attach && e && e.__attachGone) {
      // 接管时任务已经收尾：不写错误文案，直接拉状态对齐
      attachGone = true;
      streamError = null;
    } else {
      streamError = e;
      acc += (acc ? '\n\n' : '') + '请求失败：' + e;
    }
  } finally {
    if (stRef && stRef.stopTimer) { clearTimeout(stRef.stopTimer); stRef.stopTimer = 0; }
    stopWaiter();
    setSendBusy(false);              // 任何异常路径都别把发送按钮留在转圈态
    if (paintTimer) { clearTimeout(paintTimer); paintTimer = 0; }
    flushReveal();                   // 收尾前把还没「写」出来的字一次补齐，避免结尾缺字
    if (c && c.classList) c.classList.remove('streaming');
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
        foldTransition(b, b.querySelector('.think-body'), false, {
          cls: 'open', positive: true, ms: 180,
        });
        const lb = b.querySelector('.think-label');
        if (lb && (/^步骤 \d+ · 思考中…$/.test(lb.textContent)
                   || lb.textContent === '深度思考中…')) {
          lb.textContent = lb.textContent.replace('思考中…', '已思考');
        }
      });
    }
    // 整轮只拿到错误、没有任何正文：把最后一条错误补进正文，避免空气泡。
    // 重试成功时 acc 已有正文，这里不会触发，中途那条错误就不会残留成独立段落。
    if (!String(acc || '').trim() && !String(serverFinalText || '').trim() && lastErrText) {
      acc = lastErrText;
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
          // 没有可展示的正文段。若这一轮的正文全是过场旁白（已收进过程折叠），
          // 就在答案区留一个空节点；只有真的没有任何正文块时才退回全文。
          const hadText = (doneBlocks || []).some((x) => x && x.kind === 'text'
            && String(x.text || '').trim());
          const empty = document.createElement('div');
          empty.className = 'content';
          if (!hadText) {
            empty.innerHTML = safeRenderMarkdown(shown);
            if (typeof enhanceToc === 'function') enhanceToc(empty);
          }
          put(empty);
          c = empty;
          row._content = c;
          row._secs = [empty];
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
        if (typeof enhanceToc === 'function') enhanceToc(c);
      }
      // 后台探索批次：回合结束后仍在跑，单独成卡并轮询进度（不放进过程折叠）
      if (typeof appendExploreBatchCards === 'function') {
        appendExploreBatchCards(row._wrap, doneTools);
      }
      // 图卡按最终顺序重排：清掉流式期间追加的，再统一挂到正文之后
      row._wrap.querySelectorAll('.diagram-card').forEach((n) => n.remove());
      if (typeof appendDiagramCards === 'function') {
        appendDiagramCards(row._wrap, doneTools);
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
    // 空正文 / 请求失败的中断轮：上面没有建操作条，这里补上。
    // 否则「历史压缩」圆环挂不到这条消息上——表现就是「只有完整输出完才能压缩」。
    if (!bg && !row._wrap.querySelector('.msg-actions')) {
      if (typeof idx !== 'number' && mine && Array.isArray(mine.messages)) {
        idx = mine.messages.length - 1;
      }
      if (typeof idx === 'number') row.dataset.idx = idx;
      row._raw = shown;
      row._wrap.appendChild(buildActions(row, () => row._raw || '', idx, 'ai'));
    }
    // 中断原因放在这条消息的最末尾（GAL 模式不加，避免破坏演出）
    if (!bg && endReason && state.uiMode !== 'gal'
        && typeof buildEndNote === 'function' && row._wrap) {
      row._wrap.appendChild(buildEndNote(
        endReason, typeof idx === 'number' ? idx : undefined));
    }
    // 文件改动声明：正文 / 过程折叠 / 操作条都落位之后再摆一次，保持在操作条上方
    if (!bg && typeof ideReviewCardRefresh === 'function') ideReviewCardRefresh();
    // 停止 / 断流时收不到 done：本轮已经写过的文件也要结算进审阅队列，
    // 否则 pending 留到下一轮，会和同一文件的下一次写入合并成一条（baseToken 串轮）。
    if (!bg && !gotDone && typeof ideReviewEndTurn === 'function') ideReviewEndTurn(row, true, sid);
    // 后台会话：丢掉 live host（结果已落盘，切回去会从消息重建），标未读
    const bgTitle = bg ? ((mine && mine.title) || '新对话') : '';
    unregisterStream(sid, !bg);
    if (bg) {
      state.unread[sid] = true;
      renderSessions();
    }
    // 409（会话已在生成 / 正在压缩）：本条没进后端，本地追加的用户消息是幽灵数据。
    // 撤掉它、把「生成中 + 停止键」恢复出来，再挂 attach 跟到真正那一轮结束。
    if (busyRejected) {
      if (host.parentNode) host.remove();
      unregisterStream(sid, false);
      if (!state.streamingSids) state.streamingSids = {};
      state.streamingSids[String(sid)] = true;
      renderMessages();
      renderSessions();
      refreshStreamingUI();
      toast(busyMessage || '该会话正在生成中，请稍候。', 'error');
      if (isCur()) keepRecovering(sid, new Error('会话正忙'), 24, false).catch(() => {});
      return;
    }
    // 后台提醒：所有终止都提醒，文案写结束原因（手动停止除外）；后台会话无论窗口是否前台都提醒。
    // 断流但任务还在服务端跑 → 不在这里提前报「请求失败」，等 keepRecovering 拿回真正原因再提醒。
    const deferNotify = !gotDone && isCur() && !httpRejected;
    if (!deferNotify) {
      notifyChatDone({
        text: shown,
        error: streamError || (!gotDone ? new Error('连接提前结束') : null),
        stopped: stopped,
        reason: endReason,
        title: bgTitle,
        sessionId: sid,
        messageIndex: doneMsgIdx,
      });
    }
    if (state.uiMode === 'gal' && isCur()) galFlushOptions();   // 流已结束 → 若有暂存选项则显示
    scrollToBottom();
    // 兜底同步：正常流程 done 事件已带回最新会话。
    // 没收到 done（断网 / 关页面 / 连接被掐）时，任务其实还在服务端跑：
    // 挂到 /api/chat/attach 等它结束再拉一次状态，而不是立刻拉一份还没落库的旧数据。
    // 手动停止后只盯 2 分钟（24 × 5s）：后端若真卡住，不能让它永远显示「生成中」。
    // 被服务端明确拒绝（非 2xx）时没有「后台任务」可等，只做一次状态对齐。
    if (!gotDone && isCur() && !httpRejected) {
      if (attachGone) {
        // 接管时任务已经结束：只拉一次状态对齐，不进恢复轮询
        waitJobThenRefresh(sid).catch(() => {});
      } else {
        keepRecovering(sid, streamError || new Error('连接提前结束'),
                       stopped ? 24 : 120, stopped).catch(() => {});
      }
    }
  }
}

/* 流异常结束（没收到 done）后一直盯到任务真正结束：
   单次 attach 失败或任务还在跑时，每 5 秒再试一次。
   tries 默认 120 次（约 10 分钟）；手动停止只给 24 次（约 2 分钟），
   到点仍不结束就解除界面上的「生成中」，不能让用户一直看到假运行。
   以前只试一次，attach 一失败就没人再管，界面永远停在半截回答上。 */
async function keepRecovering(sid, err, tries, stopped) {
  const limit = Math.max(1, Number(tries) || 120);
  // 恢复期间继续显示「生成中 + 停止键」：任务可能真的还在后台跑，
  // 用户必须还能再点一次停止（否则又变成「无内容、无停止按钮」的假死）。
  if (!state.streamingSids) state.streamingSids = {};
  state.streamingSids[String(sid)] = true;
  if (typeof refreshStreamingUI === 'function') refreshStreamingUI();
  let failures = 0;
  for (let i = 0; i < limit; i++) {
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
      // 任务真正结束：按服务端回放的 end_reason 补一条通知（窗口在后台 / 别的会话才弹）。
      // 关键：服务端只在**异常时**才发 end_reason；拿不到原因 + 落库末条是完整
      // assistant，说明这一轮其实正常完成 —— 不能因为本地流丢过 done 就报「连接中断」。
      if (!stopped) {
        const cur = String(state.sessions.current || '') === String(sid);
        const s = state.sessions.sessions.find((x) => x.id === sid) || null;
        const msgs = (s && Array.isArray(s.messages)) ? s.messages : [];
        const last = msgs.length ? msgs[msgs.length - 1] : null;
        const kind = String((r.reason && r.reason.kind) || '');
        // 两条独立证据：attach 回放到了 done（本轮所有结束路径都会发 done），
        // 或落库末条是完整 assistant。任一成立就按「正常完成」提醒。
        const finishedOk = r.done === true
          || !!(last && last.role === 'assistant'
                && !last.interrupted
                && String(last.content || '').trim());
        const text = finishedOk ? String(last.content || '') : '';
        notifyChatDone({
          text: text,
          error: (finishedOk && !kind) ? null : (err || null),
          stopped: false, reason: r.reason || null,
          title: cur ? '' : ((s && s.title) || '新对话'),
          sessionId: sid,
        });
      }
      return;
    }
    await new Promise((res) => setTimeout(res, 5000));
  }
  // 等满上限仍没结束：清掉「后台运行中」标记，避免一直骗人
  if (state.streamingSids) delete state.streamingSids[String(sid)];
  renderSessions();
  if (typeof refreshStreamingUI === 'function') refreshStreamingUI();
  renderMessages();
  if (stopped) toast('已解除界面等待；后台若仍未收尾，可再次点停止或刷新页面', 'error');
}

/* 「回到流式输出」：把仍在后台运行的任务重新接到前端。
   本地流还活着（同页切走过）→ 只切回会话，live DOM 由 attachStreamHost 自动挂回；
   否则用 /api/chat/attach 从 0 回放并继续跟随，渲染走与正常发送完全同一条管线。 */
const _resumingOut = new Set();
async function resumeLiveOutput(sid) {
  const key = String(sid || '');
  if (!key || _resumingOut.has(key)) return;
  const local = streamOf(key);
  // 纯文本回放不算「本地流已接管」：它不完整，点「回到实时输出」要换成完整 attach
  const alreadyLocal = !!local && !local.replay;
  if (!alreadyLocal && !(state.streamingSids && state.streamingSids[key])) {
    toast('这个会话没有正在运行的任务');
    return;
  }
  _resumingOut.add(key);
  const atCur = () => String(state.sessions.current || '') === key;
  try {
    if (alreadyLocal) {
      // 本地流还活着：切过去即可，live DOM 由 attachStreamHost 挂回末尾
      if (!atCur()) {
        if (typeof selectSession !== 'function') return;
        await selectSession(key);
      } else if (typeof attachStreamHost === 'function') {
        attachStreamHost(key);
      }
      return;
    }
    // 停掉正在进行的纯文本回放（刷新后 init 自动挂的那条），等它退出再接管：
    // 否则它会先写 textContent、再 pullState 整块重画，把新接管的实时行冲掉。
    const old = _replays.get(key);
    if (old) {
      old.takenOver = true;
      try { if (old.stop) old.stop(); } catch (e) { /* 已关闭 */ }
      for (let i = 0; i < 16 && _replays.has(key); i++) {
        await new Promise((r) => setTimeout(r, 60));
      }
    }
    toast('正在接管输出…');
    if (!atCur()) {
      if (typeof selectSession !== 'function') return;
      await selectSession(key);
    }
    if (!atCur()) return;                  // 切会话失败：别把新行画进别的会话
    await streamChat({}, null, { since: 0 });
  } catch (e) {
    toast('接管输出失败：' + ((e && e.message) || e), 'error');
  } finally {
    _resumingOut.delete(key);
  }
}

/* 断线重连：把 attach 回放的事件重新渲染到界面。
   以前只等 done，用户看到的是落库的进度快照 + 「后台运行中」，期间一个字都不动；
   这里先把快照行换成临时实时行，delta / 思考 / 工具 / 重试状态继续更新，
   任务结束后再由 pullState 用落库结果重画一次（保证最终内容和 markdown 一致）。 */
function startJobReplay(sid) {
  if (String(state.sessions.current || '') !== String(sid)) return null;
  const curStream = state.streams && state.streams[String(sid)];
  if (curStream && !curStream.replay) return null;   // 主链路还活着，别重复插行
  const chat = $('#chat');
  const s = (typeof curSession === 'function') ? curSession() : null;
  if (!chat || !s) return null;
  const msgs = s.messages || [];
  const last = msgs[msgs.length - 1];
  if (last && last._progress) {
    // 先把 15 秒前的进度快照行撤掉，避免「快照 + 实时行」两份内容并排
    const old = chat.querySelector('.msg[data-idx="' + (msgs.length - 1) + '"]');
    if (old) old.remove();
  }
  const row = appendAi('', '', undefined);
  if (!row) return null;
  // 回放行也要登记成流：renderMessages 会优先复用它，而不是清掉后按数据重建。
  // 不登记的话，一次轮询重建就会把 DOM 换掉，而回放的闭包仍往旧节点写，
  // 界面永远停在「已重新连接，正在同步…」。
  const host = document.createElement('div');
  host.className = 'stream-host';
  if (row.parentNode) {
    row.parentNode.insertBefore(host, row);
    host.appendChild(row);
  }
  registerStream(sid, host, row);
  if (state.streams[String(sid)]) state.streams[String(sid)].replay = true;
  // 回放也写进 live 数据：这样切走再切回时 renderMessages 能按数据重建，
  // 而不是只剩一张 15 秒前的进度快照
  liveDrop(sid);
  const live = liveOf(sid);
  // 接管后台续写：把上一轮的计划继承到新行，进度从接管那一刻就能接着看
  if (typeof planSeedFromSession === 'function') planSeedFromSession(row);
  const content = row._content;
  content.style.whiteSpace = 'pre-wrap';
  const bar = document.createElement('span');
  bar.className = 'live-bar';
  bar.innerHTML = '<span class="live-text">已重新连接，正在同步…</span>'
                + '<span class="live-sec"></span>';
  row._wrap.appendChild(bar);
  // 注意：这里不能叫 liveText —— 会遮蔽全局的实时数据函数 liveText(L, t)，
  // 回放 delta 时调用的就变成这个 DOM 元素，抛 TypeError 把整条回放打断。
  const liveTip = bar.querySelector('.live-text');
  const liveSec = bar.querySelector('.live-sec');
  let text = '';
  let thinkChars = 0;
  let timer = 0;
  let ended = false;
  let lastStatus = '';
  const active = () => !ended && row.isConnected
    && String(state.sessions.current || '') === String(sid);
  const paint = () => {
    if (!active()) return;
    content.textContent = text;
    scrollToBottom();
  };
  const schedule = () => {
    if (timer) return;
    timer = setTimeout(() => { timer = 0; paint(); }, 80);
  };
  const status = (msg, sec) => {
    if (!active() || msg === lastStatus) return;
    lastStatus = msg;
    liveTip.textContent = msg;
    liveSec.textContent = sec || '';
  };
  return {
    event(obj) {
      if (!obj || ended) return;
      const t = obj.type;
      if (t === 'base') {
        // 续写任务的起点正文：先补到空行上，再接本次新增的增量
        text = String(obj.text || '');
        liveSetText(live, text);
        status('正在输出…', '');
        schedule();
      } else if (t === 'delta') {
        text += obj.text || '';
        liveText(live, obj.text);
        if (lastStatus !== '正在输出…') status('正在输出…', '');
        schedule();
      } else if (t === 'plan') {
        // 接管首帧会补发当前计划快照（carried=true）；之后模型更新计划也走这里
        livePlan(live, obj.plan || obj);
        if (typeof planRender === 'function') planRender(row, obj);
      } else if (t === 'rewind') {
        const n = Math.max(0, parseInt(obj.chars, 10) || 0);
        if (n > 0 && text) {
          text = text.slice(0, Math.max(0, text.length - n));
          schedule();
        }
      } else if (t === 'reasoning') {
        thinkChars += String(obj.text || '').length;
        status('模型思考中…', thinkChars ? (thinkChars + ' 字') : '');
      } else if (t === 'tool') {
        status('调用 ' + (obj.name || '工具') + '…', '');
      } else if (t === 'tool_done') {
        status((obj.ok === false ? '工具失败：' : '完成：') + (obj.name || ''), '');
      } else if (t === 'model_wait') {
        status('等待上游响应…', obj.timeout ? ('最长 ' + obj.timeout + 's') : '');
      } else if (t === 'model_ready') {
        status('上游已响应，正在生成…', '');
      } else if (t === 'retry') {
        status('上游中断（' + (obj.why || '未知') + '），正在重试…',
               obj.delay_ms ? ('等待 ' + Math.ceil(obj.delay_ms / 1000) + 's') : '');
      } else if (t === 'warn' || t === 'error') {
        status(obj.message || '…', '');
      } else if (t === 'end_reason') {
        status('本轮结束，正在同步结果…', '');
      }
    },
    finish() {
      ended = true;
      if (timer) { clearTimeout(timer); timer = 0; }
      unregisterStream(sid, false);   // 连 host 一起撤掉，之后按落库消息重画
      liveDrop(sid);                  // 回放结束：结果已落库，实时数据作废
    },
  };
}

/* 进行中的 attach 回放（按 sid）：手动「回到实时输出」时用它把旧回放停掉，
   避免纯文本回放与完整流式渲染两套逻辑同时往消息区写。 */
const _replays = new Map();

/* 断线后任务仍在后台跑：挂 attach，边回放边渲染，结束后再拉一次状态对齐。
   返回 { ok, reason }：ok 只代表「任务确实结束了」；reason 是 attach 回放里的
   end_reason（断流期间服务端算好的结束原因，用于补一条带原因的通知）。 */
async function waitJobThenRefresh(sid) {
  const key = String(sid || '');
  const h = { takenOver: false, stop: null };   // 被完整接管时会置 takenOver
  _replays.set(key, h);
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
  let sawDone = false;          // attach 回放到 done 事件 = 这一轮确实正常收尾
  const replay = startJobReplay(sid);
  try {
    const res = await fetch('/api/chat/attach', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Auth-Token': TOKEN },
      body: JSON.stringify({ session_id: sid, since: 0 }),
    });
    if (!res.ok || !res.body) {
      // 没有可挂的任务（已结束 / 已过期）：以 /api/state 的 streaming 为准
      if (h.takenOver) return { ok: false, reason: 'taken-over' };
      if (replay) replay.finish();
      return { ok: (await pullState()) === true, reason: reason, done: sawDone };
    }
    const reader = res.body.getReader();
    // 手动「回到实时输出」时用它立刻掐断这条回放，避免两套流同时写消息区
    h.stop = () => { try { reader.cancel(); } catch (e) { /* 已关闭 */ } };
    const dec = new TextDecoder();
    let buf = '';
    let finished = false;
    while (!finished && !h.takenOver) {
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
        if (replay) replay.event(obj);
        if (obj.type === 'end_reason') reason = obj.reason || null;
        if (obj.type === 'done') { sawDone = true; finished = true; break; }
      }
    }
    // 已被完整接管：不 pullState（会把刚接管的实时行整块重画掉）
    if (h.takenOver) return { ok: false, reason: 'taken-over' };
    try { reader.cancel(); } catch (e) { /* 已关闭 */ }
    if (replay) replay.finish();
    return { ok: (await pullState()) !== false, reason: reason, done: sawDone };
  } catch (e) {
    if (h.takenOver) return { ok: false, reason: 'taken-over' };
    if (replay) replay.finish();
    // attach 断了：补一次状态，但只有「后端确认不在跑」才算恢复完成
    try { return { ok: (await pullState()) === true, reason: reason, done: sawDone }; }
    catch (e2) { return { ok: false, reason: reason, done: sawDone }; }
  } finally {
    if (replay) replay.finish();
    if (_replays.get(key) === h) _replays.delete(key);
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

/* ---------------- 回退 / 分享 ---------------- */

/* 回滚前预览：后端把这次的 undo token 翻成「会改到哪些文件」。
   失败返回 null —— 预览不是关键路径，不能因为它拦住回滚。 */
async function fetchRollbackPreview(payload) {
  try {
    const r = await api('/api/agent/undo/preview', payload);
    if (r && Array.isArray(r.files)) return r;
    return { files: [], total: 0, skipped: 0 };
  } catch (e) {
    return null;
  }
}

const RB_ICON = { modify: 'i-pencil', delete: 'i-trash', create: 'i-file-plus' };
const RB_LABEL = { modify: '将被修改', delete: '将被删除', create: '将新建' };

/* 回滚确认弹窗：文件清单 + 取消 / 确认。resolve(true) 才继续执行回滚。 */
function rollbackConfirm(opts) {
  const o = opts || {};
  return new Promise((resolve) => {
    const modal = $('#rollback-modal');
    if (!modal) { resolve(window.confirm(o.title || '确定回滚？')); return; }
    const lead = $('#rb-lead');
    const sub = $('#rb-sub');
    const list = $('#rb-files');
    const btnOk = $('#rb-ok');
    const btnCancel = $('#rb-cancel');
    const btnClose = $('#rb-close');
    lead.textContent = o.title || '确定要回滚到此步骤并重新开始吗？';
    sub.textContent = o.sub || '';
    list.innerHTML = '';
    const files = o.files || [];
    if (!files.length) {
      const li = document.createElement('li');
      li.className = 'rb-file rb-empty';
      li.textContent = o.empty || '没有需要还原的文件改动。';
      list.appendChild(li);
    }
    files.forEach((f) => {
      const act = f.action || 'modify';
      const li = document.createElement('li');
      li.className = 'rb-file';
      li.dataset.action = act;
      li.title = f.path || '';
      const ico = f.kind === 'dir' ? 'i-folder' : (RB_ICON[act] || 'i-pencil');
      li.innerHTML = '<svg class="ic"><use href="#' + ico + '"/></svg>'
        + '<span class="rb-path"></span><span class="rb-tag"></span>';
      li.querySelector('.rb-path').textContent = f.path || '';
      li.querySelector('.rb-tag').textContent = RB_LABEL[act] || '将被修改';
      list.appendChild(li);
    });
    let done = false;
    const finish = (val) => {
      if (done) return;
      done = true;
      modal.classList.add('hidden');
      btnOk.onclick = null;
      btnCancel.onclick = null;
      btnClose.onclick = null;
      document.removeEventListener('keydown', onKey, true);
      modal.removeEventListener('click', onOverlay);
      resolve(val);
    };
    const onKey = (e) => {
      if (e.key === 'Escape') { e.stopPropagation(); finish(false); }
      else if (e.key === 'Enter') { e.preventDefault(); finish(true); }
    };
    const onOverlay = (e) => { if (e.target === modal) finish(false); };
    btnOk.onclick = () => finish(true);
    btnCancel.onclick = () => finish(false);
    btnClose.onclick = () => finish(false);
    document.addEventListener('keydown', onKey, true);
    modal.addEventListener('click', onOverlay);
    modal.classList.remove('hidden');
    btnOk.focus();
  });
}

/* 回退到某条消息之前：先按相反顺序还原这条及其后的文件写操作，再删掉这些消息。
   和「撤回」的区别：撤回只删消息，回退会把沙箱里的改动一起还原。 */
async function rollbackMessage(index) {
  if (state.streaming || typeof index !== 'number') return;
  const s = curSession();
  const msgs = (s && s.messages) || [];
  if (!s || index < 0 || index >= msgs.length) return;
  const tokens = [];
  for (let i = index; i < msgs.length; i += 1) {
    ((msgs[i] && msgs[i].tools) || []).forEach((it) => {
      if (it && it.undo) tokens.push(it.undo);
    });
  }
  const count = msgs.length - index;
  const pv = await fetchRollbackPreview({
    session_id: state.sessions.current, tokens: tokens,
  });
  let sub = '会移除它及其后的 ' + count + ' 条消息';
  if (tokens.length) {
    sub += pv ? '，并还原 ' + (pv.files || []).length + ' 个文件'
              : '，并还原 ' + tokens.length + ' 处文件改动';
  } else {
    sub += '。';
  }
  if (!(await rollbackConfirm({
    title: '确定要回退到这条消息之前吗？',
    sub: sub,
    files: pv ? pv.files : [],
    empty: '这段对话没有可还原的文件改动。',
  }))) return;
  let undone = 0;
  if (tokens.length) {
    try {
      const u = await api('/api/agent/undo', { session_id: state.sessions.current, tokens: tokens });
      undone = (u && u.done) || 0;
    } catch (e) {
      if (!confirm('文件改动还原失败（' + e.message + '），仍然只删除消息？')) return;
    }
  }
  const r = await api('/api/message/withdraw', { session_id: state.sessions.current, index: index });
  if (r.sessions) applySessionsPayload(r.sessions);
  renderSessions();
  renderMessages();
  if (typeof ideReloadAll === 'function') ideReloadAll();   // IDE 打开着的文件跟着回到磁盘内容
  if (state.uiMode === 'gal') galRenderFromSession();
  if (r && r.context) renderContextBar(r.context);
  toast(tokens.length ? '已回退，' + undone + '/' + tokens.length + ' 处改动已还原' : '已回退到该消息之前',
    'success');
}

/* 分享一条问答：交给 web/share.js 的面板（选范围 / 脱敏 / 生成自包含文件）。
   本地应用不做托管链接，产物是 HTML / 包 / 长图。 */
function shareMessage(index) {
  if (typeof window.openSharePanel === 'function') return window.openSharePanel(index);
  toast('分享面板未加载，请刷新页面', 'error');
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
  toast(m.images || m.image || m.doc || (m.docs && m.docs.length)
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
  if (typeof index !== 'number') return;
  // 用实时状态判断，不要用缓存的 state.streaming：它只在 register/unregister 时刷新，
  // 过期时会「点了完全没反应」，看起来就像按钮坏了（现场 00:30）。
  // 后端那轮本来就在跑（刷新 / 断线后转成后台）：点到「继续输出 / 继续任务」的意图
  // 就是回到它，直接接管实时输出；只弹 toast 会让用户以为按钮失灵。
  if (isStreaming()) {
    if (typeof resumeLiveOutput === 'function') {
      resumeLiveOutput(state.sessions.current);
    } else {
      toast('这一轮还在生成，等它结束再点「继续输出」');
    }
    return;
  }
  if (state.uiMode === 'gal') return;                 // GAL 模式不提供
  if (!hasChatKey()) { toast('请先在「设置 → 模型接入」中填写密钥', 'error'); return; }
  const s = curSession();
  const msgs = (s && s.messages) || [];
  // 后端「继续输出 / 继续任务」永远接着**最后一条 assistant** 写
  //（cont_base = messages[-1].content）。历史行上的按钮可能挂在更早那条上，
  // 前端若按被点的那条续写，两边起点不同：界面从旧正文长，落库却在最新正文后面接，
  // 表现为「一点继续，前面的输出就没了」。
  let target = index;
  for (let i = msgs.length - 1; i >= 0; i--) {
    if (msgs[i] && msgs[i].role === 'assistant') { target = i; break; }
  }
  const m = msgs[target];
  if (!m || m.role !== 'assistant') return;
  const row = $('#chat').querySelector('.msg.ai[data-idx="' + target + '"]');
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

/* 把解析状态写回「最后一条用户消息」里的文档标签（多附件按 index 定位）。
   后端事件只带 index，不依赖 DOM 顺序重排；找不到对应标签时静默跳过。 */
function docChipRender(obj, phase) {
  const chips = (typeof lastUserDocChips === 'function') ? lastUserDocChips() : [];
  const idx = Math.max(0, Number(obj && obj.index) || 0);
  const chip = chips[idx] || (chips.length === 1 ? chips[0] : null);
  if (!chip) return;
  const name = (obj && obj.name) || chip.dataset.name || '附件';
  chip.dataset.name = name;
  chip.classList.remove('parsing', 'warn', 'err');
  chip.classList.remove('hidden');
  let suffix = '';
  if (phase === 'parsing') {
    suffix = '（正在解析…）';
    chip.classList.add('parsing');
    chip.title = '正在解析，请稍候';
  } else if (phase === 'progress') {
    const stageLabel = ({ text: '读取', render: '渲染', ocr: '识别' })[obj.stage] || '解析';
    suffix = obj.total
      ? ('（' + stageLabel + ' ' + obj.done + '/' + obj.total + ' 页）')
      : '（正在解析…）';
    chip.classList.add('parsing');
    chip.title = '正在解析，请稍候';
  } else if (phase === 'error') {
    suffix = '（解析失败）';
    chip.classList.add('err');
    chip.title = ((obj && obj.error) || '解析失败') + ' · 点击重试';
  } else {
    suffix = '（' + ((obj && obj.chars) || 0) + ' 字）';
    if (obj && obj.warn) chip.classList.add('warn');
    chip.title = (obj && obj.warn) || '点击预览';
  }
  chip.innerHTML = icon('attach') + ' ' + escapeHtml(name) + suffix;
}

const DOC_MAX_BYTES = 2 * 1024 * 1024 * 1024; // 单个附件宽松上限 2GB（防误拖超大文件；实际仍受浏览器内存约束）
const MAX_PENDING_DOCS = 10;                  // 一次最多带 10 个文档（再多会让请求体和上下文一起失控）
const MAX_PENDING_IMAGES = 4;                 // 一次最多带 4 张图，避免请求体过大
// 粘贴长文本：只在输入框上方生成一个轻量文本块（不生成文件、不上传、不走文档解析）。
// 发送时按顺序并入消息正文；短文本仍走原生粘贴。
const PASTE_BLOCK_MIN = 800;                  // 超过这个字数才收成文本块
const MAX_PENDING_PASTES = 6;                 // 一次最多带 6 段，避免输入框上方堆满
let pasteSeq = 0;                             // 文本块自增 id（删除 / 预览用）
let attachSeq = 0;                            // 连续粘贴多张时保持顺序
let attachEpoch = 0;                          // 清空 / 发送后作废还在读取的附件
let imageLoads = 0;                           // 正在读取、还没 onload 的图片数

function attachImageBlob(blob) {
  if (!blob) return;
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
// 允许多个：连续选择 / 拖拽 / 粘贴都追加到列表，不再互相覆盖。
// 读取期间先放一个 loading 占位项，让用户立刻看到附件并看到读取百分比。
function findPendingDoc(seq) {
  return (state.pendingDocs || []).find((d) => d.seq === seq) || null;
}

function attachDocBlob(file) {
  if (!file) return;
  if (file.size > DOC_MAX_BYTES) { toast('附件太大（上限 2GB）', 'error'); return; }
  if (!file.size) { toast('附件是空文件：' + (file.name || '未命名'), 'error'); return; }
  if ((state.pendingDocs || []).length >= MAX_PENDING_DOCS) {
    toast('一次最多发送 ' + MAX_PENDING_DOCS + ' 个文档', 'error');
    return;
  }
  const name = file.name || '附件';
  const seq = ++attachSeq;
  const epoch = attachEpoch;
  const item = {
    name,
    mime: file.type || '',
    b64: '',
    size: file.size || 0,
    seq,
    loading: true,
    progress: 0,
  };
  state.pendingDocs = (state.pendingDocs || []).concat(item)
    .sort((a, b) => (a.seq || 0) - (b.seq || 0));
  renderAttach();
  const reader = new FileReader();
  reader.onprogress = (ev) => {
    if (epoch !== attachEpoch) return;
    if (!ev.lengthComputable || !ev.total) return;
    const cur = findPendingDoc(seq);
    if (!cur || !cur.loading) return;
    cur.progress = Math.min(99, Math.round(ev.loaded * 100 / ev.total));
    renderAttach();
  };
  reader.onload = () => {
    if (epoch !== attachEpoch) return;        // 读取期间已清空 / 已发送
    const cur = findPendingDoc(seq);
    if (!cur) return;                         // 读取期间被用户移除了
    const s = String(reader.result || '');
    cur.b64 = s.split(',')[1] || '';
    cur.mime = cur.mime || (s.match(/^data:([^;]+);/) || [])[1] || 'application/octet-stream';
    cur.loading = false;
    cur.progress = 100;
    renderAttach();
    const list = state.pendingDocs.filter((d) => d.b64);
    const n = list.length;
    toast(n > 1 ? ('已附带 ' + name + '（共 ' + n + ' 个文档）')
                : ('已附带 ' + name + '，可继续输入文字后一起发送'));
  };
  reader.onerror = () => {
    removePendingDoc(seq);
    toast('附件读取失败', 'error');
  };
  reader.readAsDataURL(file);
}

function removePendingDoc(seq) {
  state.pendingDocs = (state.pendingDocs || []).filter((d) => d.seq !== seq);
  renderAttach();
}

/* 粘贴长文本 → 轻量文本块：只存原文，发送时并入消息正文。
   不生成 File、不上传、不占附件通道，也不用「还原」。 */
function shouldPasteAsBlock(text) {
  return String(text || '').length >= PASTE_BLOCK_MIN;
}

function attachPastedBlock(text) {
  const s = String(text || '');
  if (!s) return;
  if ((state.pendingPastes || []).length >= MAX_PENDING_PASTES) {
    toast('一次最多添加 ' + MAX_PENDING_PASTES + ' 段粘贴文本', 'error');
    return;
  }
  state.pendingPastes = (state.pendingPastes || []).concat({ id: ++pasteSeq, text: s });
  renderAttach();
}

function removePendingPaste(id) {
  state.pendingPastes = (state.pendingPastes || []).filter((p) => p.id !== id);
  renderAttach();
}

function previewPendingPaste(p) {
  if (!p) return;
  const label = '粘贴文本 · ' + p.text.length + ' 字.txt';
  docPreviewOpen(new Blob([p.text], { type: 'text/plain;charset=utf-8' }), label, 'text/plain');
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
  const docs = state.pendingDocs || [];
  const pastes = state.pendingPastes || [];
  const bar = $('#attach-bar');
  if (!bar) return;
  bar.classList.toggle('hidden', !imgs.length && !docs.length && !pastes.length);
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
      thumb.title = '点击查看大图';
      thumb.style.cursor = 'zoom-in';
      thumb.onclick = () => imgZoomOpen(im.url, '待发送图片');
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
  const dbox = $('#attach-docs');
  if (dbox) {
    dbox.classList.toggle('hidden', !docs.length);
    dbox.innerHTML = '';
    docs.forEach((d) => {
      const chip = document.createElement('span');
      const loading = !!d.loading;
      chip.className = 'attach-doc' + (loading ? ' loading' : '');
      chip.title = loading
        ? ('正在读取附件… ' + (d.progress || 0) + '%')
        : ('点击预览：' + d.name);
      const suffix = loading ? ('（读取 ' + (d.progress || 0) + '%）') : docChipSuffix(d);
      chip.innerHTML = icon('attach')
        + '<span class="attach-doc-name">' + escapeHtml(d.name) + suffix + '</span>';
      const x = document.createElement('button');
      x.type = 'button';
      x.className = 'attach-doc-x';
      x.title = '移除这个附件';
      x.textContent = '✕';
      x.onclick = (e) => { e.stopPropagation(); removePendingDoc(d.seq); };
      chip.appendChild(x);
      if (!loading) chip.onclick = () => docPreviewPending(d);
      dbox.appendChild(chip);
    });
  }
  const pbox = $('#attach-pastes');
  if (pbox) {
    pbox.classList.toggle('hidden', !pastes.length);
    pbox.innerHTML = '';
    pastes.forEach((p, i) => {
      const item = document.createElement('span');
      item.className = 'attach-paste';
      item.title = '点击预览这段文本';
      item.innerHTML = icon('attach')
        + '<span class="attach-paste-name">粘贴文本' + (pastes.length > 1 ? ' ' + (i + 1) : '')
        + ' · ' + p.text.length + ' 字</span>';
      const x = document.createElement('button');
      x.type = 'button';
      x.className = 'attach-paste-x';
      x.title = '移除这段文本';
      x.textContent = '✕';
      x.onclick = (e) => { e.stopPropagation(); removePendingPaste(p.id); };
      item.appendChild(x);
      item.onclick = () => previewPendingPaste(p);
      pbox.appendChild(item);
    });
  }
  const txt = $('#attach-text');
  if (txt) {
    const parts = [];
    if (imgs.length) parts.push(imgs.length > 1 ? (imgs.length + ' 张图片') : '图片');
    const readyDocs = docs.filter((d) => !d.loading);
    if (readyDocs.length) {
      parts.push(readyDocs.length > 1
        ? (readyDocs.length + ' 个文档（发送时自动抽取正文）')
        : '文档（发送时自动抽取正文）');
    }
    const loadingDocs = docs.length - readyDocs.length;
    if (loadingDocs) parts.push(loadingDocs + ' 个附件正在读取');
    if (pastes.length) parts.push(pastes.length + ' 段粘贴文本');
    txt.textContent = parts.length
      ? ('已附带 ' + parts.join(' + ') + ' · 可继续输入文字后一起发送')
      : '';
  }
  syncSendBtn();
}

function clearAttach() {
  attachEpoch++;
  state.pendingImage = null;
  state.pendingImages = [];
  state.pendingDocs = [];
  state.pendingPastes = [];
  const box = $('#attach-thumbs');
  if (box) box.innerHTML = '';
  const dbox = $('#attach-docs');
  if (dbox) dbox.innerHTML = '';
  const pbox = $('#attach-pastes');
  if (pbox) pbox.innerHTML = '';
  const thumb = $('#attach-thumb');
  if (thumb) thumb.removeAttribute('src');   // 兼容旧 DOM
  renderAttach();
}

/* ---------- 引用块（「添加到对话」）----------
   只在输入框上方显示一个「选中文本 N」小块；发送时作为 quotes 随请求带走，
   由后端拼进本轮 prompt，不写入消息正文。 */
const QUOTE_MAX_ONE = 4000;      // 与后端 _quotes_block 的上限保持一致

function addQuote(text, label) {
  const t = String(text || '').trim();
  if (!t) return false;
  const clipped = t.length > QUOTE_MAX_ONE ? t.slice(0, QUOTE_MAX_ONE) + '…（已截断）' : t;
  const name = String(label || '').trim().slice(0, 24);
  state.pendingQuotes = (state.pendingQuotes || []).concat({ text: clipped, label: name });
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
    const label = q.label || ('选中文本 ' + (i + 1));
    chip.innerHTML = icon('chat') + '<span class="quote-label">' + escapeHtml(label) + '</span>';
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
