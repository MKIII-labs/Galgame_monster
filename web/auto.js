'use strict';

/* ============================================================
   自主推送：常驻通道（/api/events）+ 系统通知 + 提醒 / 主动聊天
   ------------------------------------------------------------
   与 /api/chat 的流式不同，这条连接**长期挂着**：服务端在没人提问时
   （定时提醒到点、AI 主动发消息）也能把内容推过来。
   ============================================================ */

const AUTO = {
  ctrl: null,      // AbortController：重连前先掐掉旧连接
  retry: 0,        // 连续失败次数（用于退避）
  notified: false, // 是否已问过通知权限
  notifiedKeys: new Set(),   // 同一件事只提醒一次（任务确认 / 任务完成等）
  liveNotifs: [],  // 浏览器模式下持有 Notification 引用，避免 onclick 随 GC 丢失
  notifyTargets: {},         // 通知 id -> 点击后的落点（Electron 主进程点击回调按 id 找回）
  notifySeq: 0,
  // 自动化看板：回合跑在服务端，这里靠 autopilot_live 事件堆出实时状态
  live: {
    sid: '', gid: '', goal: null, state: '', phase: '', act: '', turn: 0,
    log: [], text: '', startedAt: 0, open: false, closed: false,
    fetchAt: 0, timer: 0,
  },
};

/** Electron 壳是否可用（主进程通知点击可靠，且能还原最小化窗口） */
function nativeNotify() {
  return (typeof window !== 'undefined' && window.gmWin
          && typeof window.gmWin.notify === 'function') ? window.gmWin : null;
}

function rememberNotifyTarget(target) {
  AUTO.notifySeq += 1;
  const id = 'n' + AUTO.notifySeq + '_' + Date.now().toString(36);
  AUTO.notifyTargets[id] = target || null;
  const keys = Object.keys(AUTO.notifyTargets);
  if (keys.length > 50) delete AUTO.notifyTargets[keys[0]];
  return id;
}

function notifySupported() {
  return typeof Notification !== 'undefined';
}

// 请求系统通知权限（要在用户交互里调用，浏览器才认）
async function ensureNotifyPermission() {
  if (!notifySupported()) return 'unsupported';
  if (Notification.permission === 'granted') return 'granted';
  if (Notification.permission === 'denied') return 'denied';
  if (AUTO.notified) return Notification.permission;
  AUTO.notified = true;
  try {
    return await Notification.requestPermission();
  } catch (e) {
    return 'denied';
  }
}

// 不同事件用不同音高区分：完成（高）/ 需要你（中）/ 需要留意（中低）/ 出错（低）/ 提醒（次高）
const NOTIFY_TONES = {
  done: 880,      // A5：完成
  ask: 660,       // E5：等你回答 / 待处理
  warn: 587,      // D5：输出被截断 / 轮数上限 / 暂停
  error: 392,     // G4：余额不足 / 连接中断 / 失败
  remind: 784,    // G5：提醒到点
};
let _notifyCtx = null;

/* 通知配置（设置 → Agent → 通知）：横幅 / 声音 / 按事件开关 / 音量 */
function notifyCfg() {
  const c = (typeof state !== 'undefined' && state && state.config) || {};
  const vol = Number(c.notify_volume == null ? 60 : c.notify_volume);
  return {
    banner: c.notify_banner !== false,
    sound: c.notify_sound !== false,
    volume: Math.max(0, Math.min(100, Number.isFinite(vol) ? vol : 60)) / 100,
    done: c.notify_done !== false,
    ask: c.notify_ask !== false,
    error: c.notify_error !== false,
  };
}

/* 提示音：单音短提示（WebAudio 现场合成，不需要音频文件）。
   浏览器要求先有用户手势才让出声，所以发送 / 点按时先 warmNotifySound() 预热。 */
function playNotifySound(kind) {
  try {
    const cfg = notifyCfg();
    if (!cfg.sound || cfg.volume <= 0) return;
    if (kind === 'done' && !cfg.done) return;
    if (kind === 'ask' && !cfg.ask) return;
    if (kind === 'error' && !cfg.error) return;
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return;
    if (!_notifyCtx) _notifyCtx = new AC();
    const ctx = _notifyCtx;
    if (ctx.state === 'suspended') ctx.resume().catch(() => {});
    const freq = NOTIFY_TONES[kind] || NOTIFY_TONES.done;
    const t = ctx.currentTime;
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = 'sine';
    osc.frequency.setValueAtTime(freq, t);
    gain.gain.setValueAtTime(0.0001, t);
    gain.gain.linearRampToValueAtTime(0.16 * cfg.volume, t + 0.015);
    gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.22);
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.start(t);
    osc.stop(t + 0.24);
  } catch (e) { /* 没有音频设备不影响通知 */ }
}

// 在用户手势里预热音频上下文：后台标签页被浏览器挂起时也能正常出声
function warmNotifySound() {
  try {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return;
    if (!_notifyCtx) _notifyCtx = new AC();
    if (_notifyCtx.state === 'suspended') _notifyCtx.resume().catch(() => {});
  } catch (e) { /* ignore */ }
}

// kind：done / ask / warn / error / remind —— 决定提示音音高（缺省按完成）
// target：点击通知后跳到哪儿，{ sessionId } 开会话、{ taskId } 开任务详情；不传就只把窗口拉前台
function showSystemNotify(title, text, kind, target) {
  const cfg = notifyCfg();
  // 声音与横幅分开控制：横幅关掉时仍按事件播提示音；声音开关在 playNotifySound 里判断
  playNotifySound(kind);
  if (!cfg.banner) return;
  // Electron 壳：通知交给主进程创建。Windows 上渲染进程的 HTML5 Notification
  // 点击事件不可靠（点了没反应），主进程的 'click' 稳定，且能先还原最小化窗口。
  const gw = nativeNotify();
  if (gw) {
    try {
      gw.notify({
        id: rememberNotifyTarget(target),
        target: target || null,
        title: title || 'Galgame_monster',
        body: text || '',
      });
      return;
    } catch (e) { /* 主进程通知失败就退回浏览器通知 */ }
  }
  if (!notifySupported() || Notification.permission !== 'granted') return;
  try {
    // silent：上面的 playNotifySound 已经出过提示音了，浏览器/系统不要再补一声
    const n = new Notification(title || 'Galgame_monster', { body: text || '', silent: true });
    // 持有引用：某些环境下通知对象被 GC 后 onclick 会一起失效（点了没反应）
    AUTO.liveNotifs.push(n);
    if (AUTO.liveNotifs.length > 20) AUTO.liveNotifs.shift();
    n.onclose = () => {
      const i = AUTO.liveNotifs.indexOf(n);
      if (i >= 0) AUTO.liveNotifs.splice(i, 1);
    };
    n.onclick = () => {
      if (n._jumped) return;
      n._jumped = true;
      try { window.focus(); } catch (e) { /* ignore */ }
      n.close();
      notifyJump(target);
    };
  } catch (e) { /* 通知失败不影响会话消息 */ }
}

/* 点通知后的落点：任务 → 工作台任务详情；会话 → 切回 Solo 并定位到对应消息。
   跳转失败只吞掉异常，不能让用户点完通知什么反应都没有。 */
async function notifyJump(target) {
  if (!target) return;
  if (AUTO.jumping) return;              // 重复 IPC / 多监听时只跳一次
  AUTO.jumping = true;
  try {
    if (target.taskId && typeof workOpenTask === 'function') {
      if (typeof setUiMode === 'function') await setUiMode('work');
      await workOpenTask(target.taskId);
      return;
    }
    if (target.sessionId) {
      if (typeof setUiMode === 'function' && state.uiMode !== 'solo') await setUiMode('solo');
      if (target.sessionId !== state.sessions.current && typeof selectSession === 'function') {
        await selectSession(target.sessionId);
      }
      const idx = Number(target.messageIndex);
      if (Number.isFinite(idx) && typeof ssFocusMessage === 'function') {
        try { await ssFocusMessage(idx); return; } catch (e) { /* 定位失败回退到底部 */ }
      }
      if (typeof scrollToBottom === 'function') scrollToBottom(true);
    }
  } catch (e) { /* 跳转失败不影响通知本身 */ }
  finally { setTimeout(() => { AUTO.jumping = false; }, 300); }
}

/* Electron 主进程的通知点击回调：主进程已经把窗口还原 / 聚焦，
   这里只需要按落点切会话或打开任务。target 随 IPC 一起回来，
   页面刷新过也能跳（不依赖内存里的 id 映射）。 */
function bindNativeNotifyClicks() {
  const gw = nativeNotify();
  if (!gw || typeof gw.onNotifyClick !== 'function') return;
  if (AUTO.nativeNotifyBound) return;    // 脚本重复加载时只绑一次，避免一次点击跳多次
  AUTO.nativeNotifyBound = true;
  gw.onNotifyClick((payload) => {
    const id = payload && payload.id;
    if (id) {
      AUTO.handledNotifies = AUTO.handledNotifies || new Set();
      if (AUTO.handledNotifies.has(id)) return;
      AUTO.handledNotifies.add(id);
      if (AUTO.handledNotifies.size > 100) AUTO.handledNotifies.clear();
    }
    const target = (payload && payload.target) || (id ? AUTO.notifyTargets[id] : null);
    if (id) delete AUTO.notifyTargets[id];
    notifyJump(target);
  });
}
bindNativeNotifyClicks();

// 同一件事只弹一次：任务确认、任务完成、提问卡等事件会重复推送
function notifyOnce(key, title, text, kind, target) {
  if (!key) { showSystemNotify(title, text, kind, target); return; }
  if (AUTO.notifiedKeys.has(key)) return;
  if (AUTO.notifiedKeys.size > 200) AUTO.notifiedKeys.clear();
  AUTO.notifiedKeys.add(key);
  showSystemNotify(title, text, kind, target);
}

function startEvents() {
  if (AUTO.ctrl) { try { AUTO.ctrl.abort(); } catch (e) { /* ignore */ } }
  AUTO.ctrl = new AbortController();
  (async () => {
    let ok = false;
    try {
      const res = await fetch('/api/events', {
        headers: { 'X-Auth-Token': TOKEN },
        signal: AUTO.ctrl.signal,
      });
      if (!res.ok || !res.body) throw new Error('HTTP ' + res.status);
      ok = true;
      AUTO.retry = 0;
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = '';
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        const frames = buf.split('\n\n');
        buf = frames.pop();
        for (const frame of frames) {
          const line = frame.split('\n').find((l) => l.startsWith('data: '));
          if (!line) continue;                   // ": ping" / ": connected" 注释帧
          let obj = null;
          try { obj = JSON.parse(line.slice(6)); } catch (e) { continue; }
          handleAutoEvent(obj);
        }
      }
    } catch (e) {
      if (e && e.name === 'AbortError') return;
    }
    // 断线重连：3s 起，最多退到 30s
    AUTO.retry = Math.min((AUTO.retry || 0) + 1, 10);
    const wait = Math.min(3000 * AUTO.retry, 30000);
    if (!ok) console.warn('[自主推送] 连接失败，' + wait + 'ms 后重试');
    setTimeout(startEvents, wait);
  })();
}

/* 原地刷新当前会话：补最新消息但不切会话、不滚到底。
   自动化事件（autopilot / task_update）会高频推送，直接用 selectSession 会把
   用户正在看的位置拽走，所以这里保持 scrollTop 与「是否跟随底部」不变。 */
async function refreshSessionQuiet(sid) {
  if (!sid || sid !== state.sessions.current) return;
  try {
    const r = await api('/api/session/select', { id: sid });
    if (!r || !r.sessions) return;
    applySessionsPayload(r.sessions);
    if (typeof renderSessions === 'function') renderSessions();
    if (typeof renderMessages === 'function') renderMessages(undefined, undefined, { keepScroll: true });
  } catch (e) { /* 静默失败：事件刷新不是关键路径 */ }
}

function handleAutoEvent(obj) {
  if (!obj || !obj.type) return;
  // 会话运行态（待审批 / 待回答）：只改侧栏小圆点，不弹通知
  if (obj.type === 'session_pending') {
    const sid = String(obj.sid || '');
    if (sid) {
      state.pending = state.pending || {};
      if (obj.kind) state.pending[sid] = String(obj.kind);
      else delete state.pending[sid];
      if (typeof renderSessions === 'function') renderSessions();
    }
    return;
  }
  // 自动化看板：回合内的阶段 / 工具 / 输出片段
  if (obj.type === 'autopilot_live') {
    apLiveOnEvent(obj);
    return;
  }
  // 自动化目标：每回合结束、暂停、完成都会推一次
  if (obj.type === 'autopilot') {
    const g = obj.goal || {};
    const mine = !g.session_id || g.session_id === state.sessions.current;
    const st = g.state || '';
    apLiveFromGoal(g, false);
    if (mine) syncAutopilotPill(st || false);
    if (st === 'done') {
      notifyOnce('autopilot-done:' + g.id, '自动化完成', g.objective || '', 'done',
                 { sessionId: g.session_id });
      toast('自动化已完成（验收通过）', 'success');
    } else if (st === 'needs_human') {
      notifyOnce('autopilot-human:' + g.id + ':' + (g.turn || 0),
                 '自动化需要你', g.last_reason || '', 'ask',
                 { sessionId: g.session_id });
      toast('自动化需要你：' + (g.last_reason || '')
            + '。回答 / 处理后点「自动化」恢复', 'error');
    } else if (st === 'paused' || st === 'failed') {
      const label = st === 'failed' ? '自动化失败' : '自动化已暂停';
      notifyOnce('autopilot-paused:' + g.id + ':' + (g.turn || 0),
                 label, g.last_reason || '', 'error', { sessionId: g.session_id });
      toast(label + '：' + (g.last_reason || '') + '。点「自动化」恢复', 'error');
    } else if (st === 'retrying') {
      // 只提示第一次重试，避免每次退避都弹通知
      if ((g.retries || 0) <= 1) {
        notifyOnce('autopilot-retry:' + g.id,
                   '自动化重试中', g.last_reason || '', 'warn',
                   { sessionId: g.session_id });
      }
    }
    // 自动化不走前端请求，没有实时流；回合结束时消息已落库，这里原地刷新一次。
    // 不能走 selectSession：那会重渲染并滚到最底，表现为「一弹通知就跳走」。
    if (mine && st !== 'retrying' && !state.streaming) {
      refreshSessionQuiet(state.sessions.current);
    }
    return;
  }
  if (obj.type === 'notify') {
    showSystemNotify(obj.title || '提醒', obj.text || '', 'remind',
                     { sessionId: obj.session_id });
    if (obj.session_id === state.sessions.current) {
      appendAutoMessage(obj.text || '', obj.title || '提醒');
    } else {
      toast('⏰ ' + (obj.text || '有一条提醒'), 'success');
    }
    return;
  }
  if (obj.type === 'auto_msg') {
    if (obj.session_id === state.sessions.current) {
      appendAutoMessage(obj.text || '', '');
    } else {
      toast(obj.text || '', 'success');
    }
    return;
  }
  // 资源管理器右键菜单：把该目录落成会话并切到 IDE
  if (obj.type === 'open_path') {
    openPathInApp(obj.path || '');
    return;
  }
  // 后台预压缩完成：事件里已带最新占用，直接刷新当前会话的进度条
  if (obj.type === 'context_update') {
    const cur = (state.sessions || {}).current;
    if (!obj.session_id || obj.session_id === cur) {
      if (obj.context && typeof renderContextBar === 'function') {
        renderContextBar(obj.context);   // 内部会同步压缩分隔线
      }
      if (state.uiMode === 'gal') toast('已压缩较早的历史，点进度条「详情」可查看摘要');
    }
    return;
  }
  // 后台预压缩开始：只提示，不阻塞发送
  if (obj.type === 'context_compressing') {
    const cur = (state.sessions || {}).current;
    if (!obj.session_id || obj.session_id === cur) {
      if (state.uiMode === 'gal') toast('上下文接近上限，正在后台压缩较早的历史');
      else compressPending();
    }
    return;
  }
  // 日程 / 产物变更：工作台里直接刷新（事件驱动，30 秒轮询只是兜底）
  if (obj.type === 'calendar_update' || obj.type === 'artifact_update') {
    if (state.uiMode === 'work' && typeof workLoad === 'function') workLoad(false);
    return;
  }
  // 任务引擎的进度事件：交给工作台刷新。其它模式下不打扰用户，
  // 只在本轮「刚结束」时弹一条系统通知。
  if (obj.type === 'task_update') {
    if (typeof workOnTaskEvent === 'function') workOnTaskEvent(obj.task);
    const t = obj.task || {};
    const steps = t.steps || [];
    // 用户看不到工作台时，才用系统通知催一下；正在工作台里就不打扰
    const away = document.hidden || !document.hasFocus();
    const nudge = away || state.uiMode !== 'work';
    const blocked = steps.find((s) => s.status === 'blocked');
    if (blocked && nudge) {
      notifyOnce('task-blocked:' + t.id + ':' + blocked.id, '任务等待确认',
        (t.title || '任务') + ' · ' + (blocked.title || '下一步'), 'ask', { taskId: t.id });
    }
    const manual = (t.status === 'paused')
      ? steps.find((s) => s.status === 'pending' && !s.tool) : null;
    if (manual && nudge) {
      notifyOnce('task-manual:' + t.id + ':' + manual.id, '任务等待你完成',
        (t.title || '任务') + ' · ' + (manual.title || '下一步'), 'ask', { taskId: t.id });
    }
    if (nudge && (t.status === 'done' || t.status === 'failed')) {
      notifyOnce('task-' + t.status + ':' + t.id + ':' + (t.updated_at || t.finished_at || ''),
        t.status === 'done' ? '任务完成' : '任务失败', t.title || '',
        t.status === 'done' ? 'done' : 'error', { taskId: t.id });
    }
  }
}

// 右键菜单 / 冷启动：把目录落成会话（同目录复用最近的一条），并切到 IDE
async function openPathInApp(path) {
  if (!path) return;
  try {
    const r = await api('/api/open/path', { path });
    if (!r || !r.ok) throw new Error((r && (r.error || r.message)) || '打开失败');
    if (typeof applySessions === 'function') applySessions(r, { keepCurrent: false });
    if (typeof renderMessages === 'function') renderMessages();
    if (typeof resetIdeForSession === 'function') resetIdeForSession();
    if (typeof setUiMode === 'function') await setUiMode('ide');
    if (typeof loadIdeTree === 'function') loadIdeTree();
    toast((r.created ? '已新建会话并打开：' : '已打开已有会话：') + path, 'success');
  } catch (e) {
    toastErr('打开目录', e);
  }
}

// 把一条「不是用户触发」的消息插进当前视图（solo 加气泡 / GAL 加对白段）
function appendAutoMessage(text, tag) {
  if (!text) return;
  // 同步进前端会话数据，避免「DOM 有、内存没有」导致后续按 index 的操作错位
  const s = (typeof curSession === 'function') ? curSession() : null;
  if (s && Array.isArray(s.messages)) {
    s.messages.push({ role: 'assistant', content: text, auto: true });
  }
  if (state.uiMode === 'gal') {
    const name = (typeof galState !== 'undefined' && galState.charName) || '';
    galAddSegment({ speaker: name, text: tag ? ('【' + tag + '】' + text) : text });
    return;
  }
  try {
    appendAi(safeRenderMarkdown(text), text, undefined);
  } catch (e) {
    appendAi(text, text, undefined);
  }
}

/* ---------------- 提醒任务面板 ---------------- */

function fmtTaskTime(ts) {
  const d = new Date((ts || 0) * 1000);
  if (!ts) return '—';
  const p = (n) => String(n).padStart(2, '0');
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate())
    + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
}

async function loadTasks() {
  const box = $('#remind-list');
  if (!box) return;
  try {
    const r = await api('/api/tasks', { op: 'list' });
    const items = (r && r.tasks) || [];
    const sysOk = !!(r && r.system_notify);
    const hint = $('#remind-syshint');
    if (hint) hint.textContent = sysOk
      ? '系统通知：可用（即使本程序没开也会弹）'
      : '系统通知：不可用，将只在程序运行时提醒';
    const rt = items.filter((t) => t.kind === 'reminder');
    const ac = items.filter((t) => t.kind === 'auto_chat');
    box.innerHTML = '';
    if (!rt.length) {
      box.innerHTML = '<div class="remind-empty">还没有提醒任务</div>';
    }
    rt.forEach((t) => {
      const row = document.createElement('div');
      row.className = 'remind-row' + (t.enabled ? '' : ' off');
      row.innerHTML =
        '<span class="remind-when">' + fmtTaskTime(t.at) + '</span>'
        + '<span class="remind-text"></span>'
        + (t.repeat ? '<span class="remind-rep">' + t.repeat + '</span>' : '')
        + '<button class="remind-del" title="删除">✕</button>';
      row.querySelector('.remind-text').textContent = t.text || '（提醒）';
      row.querySelector('.remind-del').onclick = async () => {
        if (!confirm('删除提醒「' + (t.text || '') + '」？')) return;
        try {
          await api('/api/tasks', { op: 'delete', id: t.id });
          loadTasks();
        } catch (e) { toastErr('删除提醒', e); }
      };
      box.appendChild(row);
    });
    // 主动聊天：把当前会话的任务状态同步到开关
    const cur = state.sessions.current;
    const mine = ac.find((t) => t.session_id === cur && !t.paused);
    const on = $('#autochat-on');
    if (on) on.checked = !!mine;
    // 顶部那个按钮现在表示「自动化」，不能再被主动聊天的状态覆盖

    // 主动聊天：列出所有已开启的会话（含下次触发时间）
    const acBox = $('#autochat-list');
    if (acBox) {
      acBox.innerHTML = '';
      if (!ac.length) {
        acBox.innerHTML = '<div class="remind-empty">当前没有开启主动聊天的会话</div>';
      }
      ac.forEach((t) => {
        const row = document.createElement('div');
        row.className = 'remind-row' + (t.paused ? ' off' : '');
        row.innerHTML = '<span class="remind-when">' + fmtTaskTime(t.at) + '</span>'
          + '<span class="remind-text"></span>'
          + '<span class="remind-rep">' + (t.paused ? '已暂停' : '主动聊天') + '</span>'
          + '<button class="remind-del" title="关闭这个会话的主动聊天">✕</button>';
        row.querySelector('.remind-text').textContent = sessionTitle(t.session_id);
        row.querySelector('.remind-del').onclick = async () => {
          await api('/api/tasks', { op: 'delete', id: t.id });
          loadTasks();
        };
        acBox.appendChild(row);
      });
    }
  } catch (e) {
    toastErr('读取提醒任务', e);
  }
}

/* ---------------- 提醒开关（总开关 + 细分） ----------------
   总开关关闭时，细分项全部禁用；值写 config，后端会同步注销 / 恢复 Windows 计划任务。 */
const REMINDER_SWITCHES = [
  ['reminders-enabled', 'reminders_enabled'],
  ['reminder-inapp-enabled', 'reminder_inapp_enabled'],
  ['reminder-system-enabled', 'reminder_system_enabled'],
  ['reminder-calendar-enabled', 'reminder_calendar_enabled'],
  ['auto-chat-enabled', 'auto_chat_enabled'],
];
const REMINDER_SUB_IDS = ['#reminder-inapp-enabled', '#reminder-system-enabled',
  '#reminder-calendar-enabled', '#auto-chat-enabled'];

function syncReminderSwitches() {
  const cfg = state.config || {};
  REMINDER_SWITCHES.forEach(([id, key]) => {
    const el = $('#' + id);
    if (el) el.checked = cfg[key] !== false;
  });
  const master = cfg.reminders_enabled !== false;
  REMINDER_SUB_IDS.forEach((sel) => {
    const el = $(sel);
    if (el) el.disabled = !master;
  });
}

function bindReminderSwitches() {
  REMINDER_SWITCHES.forEach(([id, key]) => {
    const el = $('#' + id);
    if (!el) return;
    el.onchange = async () => {
      const patch = {};
      patch[key] = !!el.checked;
      state.config = Object.assign({}, state.config || {}, patch);
      syncReminderSwitches();
      try {
        await api('/api/config', patch);
        toast(el.checked ? '已开启' : '已关闭', 'ok');
      } catch (e) {
        el.checked = !el.checked;
        state.config[key] = el.checked;
        syncReminderSwitches();
        toastErr('保存提醒开关', e);
      }
    };
  });
  syncReminderSwitches();
}

async function addReminder() {
  const atEl = $('#remind-at');
  const textEl = $('#remind-text');
  const repEl = $('#remind-repeat');
  const at = atEl ? atEl.value : '';
  const text = textEl ? textEl.value.trim() : '';
  if (!at) { toast('请先选时间', 'error'); return; }
  if (!text) { toast('请填提醒内容', 'error'); return; }
  try {
    const rep = repEl ? repEl.value : '';
    const r = await api('/api/calendar/events', {
      op: 'create',
      title: text,
      start_at: at.replace('T', ' '),
      rrule: rep === 'daily' ? 'FREQ=DAILY' : (rep === 'weekly' ? 'FREQ=WEEKLY' : ''),
      remind_before: 0,
      session_id: state.sessions.current,
    });
    if (!r || !r.ok) { toast('添加失败：' + ((r && r.error) || ''), 'error'); return; }
    if (textEl) textEl.value = '';
    toast('已加入工作台「日程」', 'success');
    loadTasks();
  } catch (e) {
    toastErr('添加提醒', e);
  }
}

/* ---------- 自动化看板 ----------
   回合在服务端跑（autopilot._run_turn 用假 handler 调 chat），浏览器本来一整轮
   都看不到东西。后端把关键帧转成 autopilot_live 事件，这里堆成看板：
   顶栏是「阶段 + 当前工具 + 第几回合」，展开后是完成定义 / 计划 / 最近回合。
   只在普通对话页、且只对当前会话生效。 */

const AP_PHASE_TEXT = {
  plan: '立项中', run: '执行中', verify: '机检中', review: '评审中',
  boss: '决策中', done: '已结束',
};
const AP_STATE_TEXT = {
  running: '执行中', retrying: '重试中', needs_human: '需要你',
  paused: '已暂停', failed: '已失败', done: '已完成',
};
const AP_TOOL_TEXT = {
  read_file: '读文件', write_file: '写文件', list_dir: '列目录', grep: '搜索',
  run_command: '执行命令', delete_file: '删文件', move_file: '移动文件',
};

function apToolName(name) {
  return AP_TOOL_TEXT[name] || name || '工具';
}

function apLiveMin(ts) {
  if (!ts) return '0:00';
  const sec = Math.max(0, Math.floor((Date.now() - ts) / 1000));
  return Math.floor(sec / 60) + ':' + String(sec % 60).padStart(2, '0');
}

function apLiveReset(sid, gid) {
  AUTO.live = {
    sid: sid || '', gid: gid || '', goal: null, state: '', phase: '', act: '',
    turn: 0, log: [], text: '', startedAt: Date.now(), open: AUTO.live.open,
    closed: false, fetchAt: 0, timer: 0,
  };
}

function apLiveLog(kind, text) {
  const txt = String(text || '').trim();
  if (!txt) return;
  const log = AUTO.live.log;
  log.push({ kind: kind, text: txt.slice(0, 240), at: Date.now() });
  if (log.length > 60) log.splice(0, log.length - 60);
}

function apLiveVisible() {
  const L = AUTO.live;
  if (!L.sid || L.sid !== state.sessions.current) return false;
  if (state.uiMode !== 'solo') return false;     // GAL 舞台不插这条
  if (L.closed) return false;
  return !!(L.goal || L.phase || L.log.length);
}

function apLiveScheduleRender() {
  if (AUTO.live.timer) return;
  AUTO.live.timer = setTimeout(() => { AUTO.live.timer = 0; apLiveRender(); }, 120);
}

function apLiveOnEvent(obj) {
  if (!obj || !obj.session_id || obj.session_id !== state.sessions.current) return;
  if (state.uiMode !== 'solo') return;
  const L = AUTO.live;
  if (L.sid !== obj.session_id) apLiveReset(obj.session_id, obj.goal_id);
  if (obj.goal_id && L.gid !== obj.goal_id) { L.gid = obj.goal_id; L.closed = false; }
  if (obj.turn) L.turn = Number(obj.turn) || L.turn;
  const kind = String(obj.kind || '');
  if (kind === 'phase') {
    L.phase = String(obj.phase || '');
    if (L.phase === 'done') {
      L.state = String(obj.state || 'done');
      L.act = '';
    } else {
      L.state = 'running';
    }
    apLiveLog('phase', obj.text || '');
  } else if (kind === 'tool') {
    const a = obj.args || {};
    const arg = a.path || a.to || a.from || a.cmd || a.command || a.pattern
      || a.query || a.url || a.name || a.file || a.dir || '';
    L.act = apToolName(obj.name) + (arg ? ' · ' + String(arg).slice(0, 50) : '');
    apLiveLog('tool', L.act);
  } else if (kind === 'tool_done') {
    L.act = '';
    if (obj.ok === false) apLiveLog('fail', apToolName(obj.name) + ' 失败');
  } else if (kind === 'step') {
    apLiveLog('step', '第 ' + (Number(obj.n) || 1) + ' 步');
  } else if (kind === 'warn') {
    apLiveLog('warn', obj.message || '');
  } else if (kind === 'verdict') {
    apLiveLog('ok', '验收 ' + (Number(obj.ok) || 0) + ' / ' + (Number(obj.total) || 0) + ' 通过');
    apLiveFetch(true);
  } else if (kind === 'delta') {
    L.text = (L.text + String(obj.text || '')).slice(-1500);
  } else if (kind === 'replace') {
    L.text = String(obj.text || '').slice(-1500);
  }
  apLiveScheduleRender();
}

/* 目标快照：full=true 是 /api/autopilot status 的完整 goal（带完成定义 / 计划 / 回合记录）；
   false 是事件里的精简版，先合并进来看板不空，再补一次完整拉取。 */
function apLiveFromGoal(g, full) {
  if (!g || !g.session_id || !g.state) return;
  if (g.session_id !== state.sessions.current || state.uiMode !== 'solo') return;
  const L = AUTO.live;
  if (L.sid !== g.session_id) apLiveReset(g.session_id, g.id);
  if (g.id && L.gid !== g.id) { L.gid = g.id; L.closed = false; }
  L.state = g.state;
  if (g.turn) L.turn = Number(g.turn) || L.turn;
  if (g.started_at && !L.startedAt) L.startedAt = Number(g.started_at) * 1000;
  if (full) {
    L.goal = g;
  } else {
    L.goal = Object.assign({}, L.goal || {}, g);
    apLiveFetch(false);
  }
  apLiveRender();
}

async function apLiveFetch(force) {
  const sid = state.sessions.current;
  const L = AUTO.live;
  if (!sid || L.sid !== sid) return;
  if (!force && Date.now() - (L.fetchAt || 0) < 4000) return;
  L.fetchAt = Date.now();
  try {
    const r = await api('/api/autopilot', { op: 'status', session_id: sid });
    if (!r || !r.ok || !r.goal) return;
    if (AUTO.live.sid !== sid) return;
    AUTO.live.goal = r.goal;
    AUTO.live.state = r.goal.state || AUTO.live.state;
    apLiveRender();
  } catch (e) { /* 看板拉不到不影响自动化本身 */ }
}

function apLiveBodyHtml() {
  const L = AUTO.live;
  const g = L.goal || {};
  const def = g.definition || {};
  const verdict = Array.isArray(g.verdict) ? g.verdict : [];
  const rows = verdict.length ? verdict : (def.checks || []).concat(def.rubric || []);
  const pass = rows.filter((v) => v && v.ok === true).length;
  const parts = [];
  if (g.objective) {
    parts.push('<div class="ap-sec"><div class="ap-sec-title">目标</div>'
      + '<div class="ap-goal">' + escapeHtml(String(g.objective).slice(0, 400)) + '</div></div>');
  }
  if (rows.length) {
    const items = rows.slice(0, 12).map((v) => {
      const ok = !!(v && v.ok === true);
      const bad = !!(v && v.ok === false);
      const out = v && v.out ? String(v.out).replace(/\s+/g, ' ').slice(0, 160) : '';
      return '<li class="ap-check ' + (ok ? 'ok' : (bad ? 'bad' : 'skip')) + '"'
        + (out ? ' title="' + escapeHtml(out) + '"' : '') + '>'
        + '<span class="ap-mark">' + (ok ? '✓' : (bad ? '✗' : '·')) + '</span>'
        + '<span class="ap-check-text">'
        + escapeHtml(String((v && v.text) || '').slice(0, 160)) + '</span></li>';
    }).join('');
    parts.push('<div class="ap-sec"><div class="ap-sec-title">完成定义'
      + '<span class="ap-badge">' + pass + ' / ' + rows.length + ' 通过</span></div>'
      + '<ul class="ap-checks">' + items + '</ul></div>');
  }
  const plan = Array.isArray(g.plan) ? g.plan.filter(Boolean) : [];
  if (plan.length) {
    parts.push('<div class="ap-sec"><div class="ap-sec-title">计划</div>'
      + '<ol class="ap-plan">' + plan.slice(0, 8).map((s) =>
        '<li>' + escapeHtml(String(s).slice(0, 160)) + '</li>').join('') + '</ol></div>');
  }
  const ledger = Array.isArray(g.ledger) ? g.ledger.slice(-5) : [];
  if (ledger.length) {
    parts.push('<div class="ap-sec"><div class="ap-sec-title">最近回合</div>'
      + '<ul class="ap-ledger">' + ledger.map((e) =>
        '<li>第 ' + (Number(e.turn) || 0) + ' 回合 · 工具 ' + (Number(e.tools) || 0)
        + ' · 写入 ' + (Number(e.writes) || 0)
        + (e.checks_total ? ' · 验收 ' + (Number(e.checks_ok) || 0) + '/' + e.checks_total : '')
        + '</li>').join('') + '</ul></div>');
  }
  const log = L.log.slice(-10);
  if (log.length) {
    parts.push('<div class="ap-sec"><div class="ap-sec-title">实时动态</div>'
      + '<div class="ap-log">' + log.map((it) =>
        '<div class="ap-log-row ' + it.kind + '">' + escapeHtml(it.text) + '</div>').join('')
      + '</div></div>');
  }
  if (L.text) {
    parts.push('<div class="ap-sec"><div class="ap-sec-title">正在输出</div>'
      + '<div class="ap-text">' + escapeHtml(L.text.slice(-700)) + '</div></div>');
  }
  return parts.length ? parts.join('') : '<div class="ap-empty">还没有可展示的进度</div>';
}

function apLiveRender() {
  const box = $('#ap-live');
  if (!box) return;
  const L = AUTO.live;
  const on = apLiveVisible();
  box.classList.toggle('hidden', !on);
  if (!on) return;
  const g = L.goal || {};
  const st = L.state || g.state || '';
  const running = st === 'running' || st === 'retrying';
  box.classList.toggle('running', running);
  box.classList.toggle('done', st === 'done');
  box.classList.toggle('bad', st === 'needs_human' || st === 'paused' || st === 'failed');
  const phaseEl = $('#ap-live-phase');
  if (phaseEl) {
    phaseEl.textContent = '自动化 · '
      + (AP_STATE_TEXT[st] || AP_PHASE_TEXT[L.phase] || '运行中');
  }
  const actEl = $('#ap-live-act');
  if (actEl) actEl.textContent = L.act ? '· ' + L.act : '';
  const metaEl = $('#ap-live-meta');
  if (metaEl) {
    const meta = [];
    const maxTurns = Number(g.max_turns) || 0;
    if (L.turn) meta.push('第 ' + L.turn + (maxTurns ? ' / ' + maxTurns : '') + ' 回合');
    if (running) meta.push(apLiveMin(L.startedAt));
    metaEl.textContent = meta.join(' · ');
  }
  const chev = $('#ap-live-chev');
  if (chev) chev.textContent = L.open ? '▾' : '▴';
  const head = $('#ap-live-head');
  if (head) head.setAttribute('aria-expanded', L.open ? 'true' : 'false');
  const body = $('#ap-live-body');
  if (!body) return;
  body.classList.toggle('hidden', !L.open);
  if (L.open) body.innerHTML = apLiveBodyHtml();
}

function apLiveBind() {
  const head = $('#ap-live-head');
  const box = $('#ap-live');
  if (!head || !box) return;
  head.onclick = () => {
    AUTO.live.open = !AUTO.live.open;
    apLiveRender();
    if (AUTO.live.open) apLiveFetch(true);
  };
  const x = $('#ap-live-x');
  if (x) x.onclick = () => { AUTO.live.closed = true; apLiveRender(); };
}

/* ---------- 自动化（Autopilot） ----------
   一次输入 → 生成完成定义 → 自主执行 → 机检 / 独立评审 → 无进展看门狗。
   中断自动续跑；提问按 auto / smart / ask 三档处理。
   原来的「主动聊天」仍保留在设置页，只是不再由这个按钮创建。 */

async function toggleAutopilot() {
  const btn = $('#btn-autochat');
  const on = !!(btn && btn.classList.contains('active'));
  const sid = state.sessions.current;
  if (on) {
    try {
      const r = await api('/api/autopilot', { op: 'pause', session_id: sid });
      if (!r || !r.ok) { toast('暂停失败', 'error'); return; }
      syncAutopilotPill('paused');
      apLiveFromGoal(r.goal, false);
      toast('已暂停自动化，你可以直接接管', 'success');
    } catch (e) {
      toastErr('暂停自动化', e);
    }
    return;
  }
  const inp = $('#input');
  const text = inp ? inp.value.trim() : '';
  // 「继续 / 开始 / 做吧」这类短指令：有 active 目标时按恢复处理
  const cont = /^(继续|接着|开始|做吧|继续吧|go|ok|好的?|可以|行)[，。!！\s]*$/i.test(text);
  // 未运行：有 active 的暂停 / 需要你 / 失败目标时，默认恢复它；
  // 但如果输入框里是一段新的目标，就新建（旧目标转历史），不能再把很久以前的 objective 发一遍。
  let resumeIt = false;
  try {
    const st = await api('/api/autopilot', { op: 'status', session_id: sid });
    const g = st && st.goal;
    if (g && ['paused', 'needs_human', 'failed'].includes(g.state)) {
      const same = !!text && text === String(g.objective || '').trim();
      if (!text || cont || same) {
        resumeIt = true;
      } else if (g.state === 'needs_human') {
        // 「需要你」时输入框可能是回答，也可能是新目标：让用户自己选。
        resumeIt = !confirm('当前有一个「需要你」的自动化目标。\n\n'
          + '确定：放弃它，用输入框内容新建目标？\n'
          + '取消：恢复原目标（如果输入框是回答，请先在对话里发出去）。');
      }
    }
  } catch (e) { /* 查询失败就按新建处理 */ }
  if (resumeIt) {
    try {
      const r = await api('/api/autopilot', { op: 'resume', session_id: sid });
      if (!r || !r.ok) { toast('恢复失败：' + ((r && r.error) || ''), 'error'); return; }
      syncAutopilotPill((r.goal && r.goal.state) || 'running');
      apLiveFromGoal(r.goal, true);
      toast('已恢复自动化，继续按目标推进', 'success');
    } catch (e) {
      toastErr('恢复自动化', e);
    }
    return;
  }
  if (!text) {
    toast('先在输入框写清目标，再开自动化', 'error');
    if (inp) inp.focus();
    return;
  }
  const p = await ensureNotifyPermission();
  if (p === 'denied') toast('系统通知被拒绝，结束时只会显示在会话里', 'error');
  try {
    const r = await api('/api/autopilot', {
      op: 'start', session_id: sid, objective: text,
    });
    if (!r || !r.ok) {
      toast((r && (r.message || r.error)) || '启动失败', 'error');
      return;
    }
    if (inp) inp.value = '';
    if (typeof autoGrow === 'function') autoGrow();
    if (typeof cmpClearPrecheck === 'function') cmpClearPrecheck();
    syncAutopilotPill(true);
    apLiveFromGoal(r.goal, true);      // 立刻把看板立起来，别等第一条事件
    toast('已启动自动化：会自己规划、执行、验收；中断也会自动续跑', 'success');
  } catch (e) {
    toastErr('启动自动化', e);
  }
}

function syncAutopilotPill(state) {
  const b = $('#btn-autochat');
  if (!b) return;
  // 兼容旧的布尔调用：true = running，false = 没有在跑
  const s = typeof state === 'string' ? state : (state ? 'running' : 'idle');
  const on = s === 'running' || s === 'retrying';
  b.classList.toggle('active', on);
  b.setAttribute('aria-pressed', on ? 'true' : 'false');
  const t = b.querySelector('.autochat-text');
  if (t) {
    t.textContent = s === 'retrying' ? '重试中'
      : (s === 'needs_human' ? '需要你'
        : (s === 'paused' ? '已暂停' : (on ? '运行中' : '自动化')));
  }
}

async function toggleAutoChat(on) {
  if (on) {
    const p = await ensureNotifyPermission();
    if (p === 'denied') toast('系统通知被拒绝，只会显示在会话里', 'error');
  }
  const lo = parseInt(($('#autochat-min') || {}).value, 10) || 40;
  const hi = parseInt(($('#autochat-max') || {}).value, 10) || 120;
  try {
    const r = await api('/api/tasks', {
      op: 'autochat', session_id: state.sessions.current,
      enabled: !!on, gap_min: lo, gap_max: hi,
    });
    if (!r || !r.ok) { toast('设置失败', 'error'); return; }
    toast(on ? '已开启主动聊天' : '已关闭主动聊天', 'success');
    syncAutoChatPill(!!on);
    loadTasks();
  } catch (e) {
    toastErr('设置主动聊天', e);
  }
}

function syncAutoChatPill(on) {
  const b = $('#btn-autochat');
  if (!b) return;
  b.classList.toggle('active', !!on);
  b.setAttribute('aria-pressed', on ? 'true' : 'false');
}

// 切换会话后：按钮反映这个会话有没有在跑的自动化目标（异步查一次，不阻塞渲染）
async function syncAutoChatFromSession() {
  const sid = state.sessions.current;
  if (!sid) { syncAutopilotPill(false); apLiveClear(); return; }
  try {
    const r = await api('/api/autopilot', { op: 'status', session_id: sid });
    const g = (r && r.goal) || null;
    syncAutopilotPill(g && g.state ? g.state : false);
    // 这个 status 已经是完整 goal（带完成定义 / 计划 / 回合记录）：直接喂给看板
    if (g) apLiveFromGoal(g, true); else apLiveClear();
  } catch (e) {
    syncAutopilotPill(false);
  }
}

// 这个会话没有自动化目标了：收起看板（但保留已展开/已关闭的用户选择）
function apLiveClear() {
  if (!AUTO.live.sid) return;
  AUTO.live.sid = '';
  apLiveRender();
}

// 会话 id → 标题（主动聊天列表里显示用）
function sessionTitle(sid) {
  const list = (state.sessions && state.sessions.sessions) || [];
  const s = list.find((x) => x.id === sid);
  return (s && s.title) || sid;
}

// 改间隔即时生效（当前会话已开启时，不用关掉再开）
async function updateAutoChatGap() {
  const on = $('#autochat-on');
  if (!on || !on.checked) return;
  const lo = parseInt(($('#autochat-min') || {}).value, 10) || 40;
  const hi = parseInt(($('#autochat-max') || {}).value, 10) || 120;
  try {
    const r = await api('/api/tasks', {
      op: 'autochat', session_id: state.sessions.current,
      enabled: true, gap_min: lo, gap_max: hi,
    });
    if (r && r.ok) { toast('间隔已更新（约 20 秒后先发一条）', 'success'); loadTasks(); }
  } catch (e) {
    toastErr('更新间隔', e);
  }
}

// 立即发一条试试
async function testAutoChat() {
  try {
    toast('正在生成…');
    const r = await api('/api/tasks', { op: 'autochat_test', session_id: state.sessions.current });
    if (!r || !r.ok) { toast('生成失败——检查「模型接入」里的聊天接口密钥是否已配置', 'error'); return; }
    toast('已发送一条主动消息', 'success');
  } catch (e) {
    toastErr('试发', e);
  }
}
