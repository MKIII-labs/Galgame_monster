'use strict';

/* 流式对话 · live.js
   实时消息数据层：文本 / 思考 / 工具 / 计划
   原 web/stream.js 拆分而来：按 index.html 中的顺序加载，共享全局作用域。 */

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
  if (slot) slot.classList.toggle('live', !!on);
  const attachBtn = $('#btn-attach');
  if (attachBtn) attachBtn.classList.remove('off');
  // 提示行跟着切换：生成中 Enter 是「排队」而不是「发送」
  if (typeof cmpUpdateHint === 'function') { try { cmpUpdateHint(); } catch (e) { /* 忽略 */ } }
  // 本轮收尾后再发排队项：等一拍，让渲染与落库先完成
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
    if (!keepHost && st.host && st.host.parentNode) {
      st.host.remove();
    } else if (keepHost && st.host && st.host.parentNode) {
      // 收尾解包：把消息行挪回 #chat，别让带 data-idx 的行留在 .stream-host 里。
      // 否则按 data-idx 找锚点的 DOM 操作（压缩分隔线 / 加载更早消息）会拿到
      // 非 #chat 直接子节点，insertBefore 抛 NotFoundError。
      const host = st.host;
      const parent = host.parentNode;
      while (host.firstChild) parent.insertBefore(host.firstChild, host);
      host.remove();
    }
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
  const busy = isStreaming();
  btn.title = docLoading ? '附件还在读取中，请稍候…'
            : busy ? '排队发送（本轮结束后自动发出）'
            : (compressBusyNow() ? '排队发送（压缩完成后自动发出）'
            : (mic.active ? '正在录音 —— 说完再点发送'
            : (mic.busy ? '正在识别语音…'
            : (empty ? '先输入内容，或附上一张图片 / 文档' : '发送（Enter）'))));
}
