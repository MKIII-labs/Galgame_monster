'use strict';

/* 流式对话 · send.js
   发送入口、通知、搜索开关与发送按钮状态
   原 web/stream.js 拆分而来：按 index.html 中的顺序加载，共享全局作用域。 */

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
  // 「新对话」草案：第一次发送时才真正创建会话（点按钮不再等接口）
  if (typeof isDraftSession === 'function' && isDraftSession()) {
    const realSid = await materializeDraft();
    if (!realSid) return;
  }
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
                   content: payload.__edit.content,
                   imgs: payload.__edit.imgs || [],
                   docs: payload.__edit.docs || [],
                   reuseImages: payload.__edit.reuseImages || [],
                   reuseDocs: payload.__edit.reuseDocs || [] });
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
    if (typeof isStreaming === 'function' ? isStreaming() : state.streaming) {
      toast('正在生成回复，稍后再发送这次修改', 'error');
      return;
    }
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
    // 附件也参与这次修改：reuse_* 是「沿用原消息里的哪些附件」，
    // 新选的图 / 文档照常带上（服务端会追加到这条消息）。
    const editImgs = imgs.filter((it) => it.b64);
    const editDocs = docs.filter((d) => d.b64);
    const reuseImages = (state.pendingImages || [])
      .filter((it) => it.reuse).map((it) => it.reuse);
    const reuseDocs = (state.pendingDocs || [])
      .filter((d) => d.reuse).map((d) => d.reuse);
    // 发送前先把这些回复产生的文件改动还原（弹窗预览）；取消则输入框与编辑状态都保留
    const rb = await revertDiscardedFiles(idx, {
      title: '修改这条消息并重新生成？',
      empty: '这条消息之后没有可还原的文件改动。',
      sub: (count, pv) => revertSub(
        Math.max(0, count - 1)
          ? ('会移除它之后的 ' + (count - 1) + ' 条回复并重新生成')
          : '会重新生成回复',
        pv),
    });
    if (!rb.ok) return;
    cancelEditTarget(false);
    input.value = '';
    autoGrow();
    clearAttach();
    if (typeof cmpClearPrecheck === 'function') cmpClearPrecheck();
    hideSttChip();
    // 正在压缩较早历史：修改先排队，压缩完成后自动发出（避免摘要在中途被改乱）
    if (compressBusyNow()) {
      sendQueue.push({
        __q: true,
        text: text,
        sid: state.sessions.current,
        __edit: { index: idx, content: text, imgs: editImgs, docs: editDocs,
                  reuseImages: reuseImages, reuseDocs: reuseDocs },
      });
      renderSendQueue();
      toast('正在压缩较早的历史，这条修改已排队，完成后自动发送');
      return;
    }
    await resend({ mode: 'edit', index: idx, content: text,
                   imgs: editImgs, docs: editDocs,
                   reuseImages: reuseImages, reuseDocs: reuseDocs });
    return;
  }
  // 生成中 / 压缩中：不再丢弃 —— 文本 / 图片 / 文档 / 引用全部进队列，空闲后自动发送
  // 用实时状态：切到新会话后缓存的 state.streaming 仍是上一个会话的 true，会误排队
  const _busy = (typeof isStreaming === 'function') ? isStreaming() : state.streaming;
  if (_busy || compressBusyNow()) {
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
    toast(_busy ? '已排队，本轮回复结束后自动发送'
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
