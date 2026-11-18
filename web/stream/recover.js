'use strict';

/* 流式对话 · recover.js
   断流恢复：attach 重试与任务回放
   原 web/stream.js 拆分而来：按 index.html 中的顺序加载，共享全局作用域。 */


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
      if (typeof renderPendingCards === 'function') renderPendingCards();
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
