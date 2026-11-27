'use strict';

/* 流式对话 · stop.js
   停止与中断：超时兜底
   原 web/stream.js 拆分而来：按 index.html 中的顺序加载，共享全局作用域。 */

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
  const sidToStop = (st && st.sid) || sidKey;
  if (st) st.stopping = true;
  // 按会话记「正在停止」：切到别的会话时不能继承这里的状态
  if (!state.stoppingSids) state.stoppingSids = {};
  if (sidToStop) state.stoppingSids[String(sidToStop)] = true;
  state.stopping = true;
  // 立即给反馈：搜索阶段（规划/检索/抓正文）可能要一两秒才会真正收尾，
  // 不给反馈的话会被误认为「暂停键失灵」
  const b = $('#btn-stop');
  if (b) {
    b.disabled = true;
    b.classList.add('stopping');
    b.title = '正在停止…';
    b.setAttribute('aria-busy', 'true');
  }
  // 状态条也立刻改成「正在停止」：搜索 / 工具轮里服务端要过一会儿才真正收尾
  const liveTip = st && st.row ? st.row.querySelector('.live-text') : null;
  if (liveTip) liveTip.textContent = '正在停止…';
  toast('正在停止…');
  // 明确点「停止」= 连排队也一起取消，避免用户以为已经停下却又自动发出去
  if (typeof cancelSendQueue === 'function') cancelSendQueue(true);
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
      if (state.stoppingSids) delete state.stoppingSids[String(sidToStop)];
      state.stopping = false;
      refreshStreamingUI();
      renderMessages();
    } else if (!r) {
      // 后端迟迟不确认：先解除界面上的「生成中」，否则停止键永远点不动。
      if (state.streamingSids) delete state.streamingSids[String(sidToStop)];
      if (state.stoppingSids) delete state.stoppingSids[String(sidToStop)];
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
