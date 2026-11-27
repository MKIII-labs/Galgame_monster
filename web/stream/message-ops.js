'use strict';

/* 流式对话 · message-ops.js
   消息操作：撤回 / 回滚 / 编辑 / 重发
   原 web/stream.js 拆分而来：按 index.html 中的顺序加载，共享全局作用域。 */

/* ============================================================
   撤回 / 修改 / 重新发言 / 源码视图
   ============================================================ */
/* 撤回一条用户消息：和 AI 回复下的「回退到此」是同一套逻辑 ——
   把它之后的 AI 回复一并移除，并把这段对话产生的文件写操作还原
   （先弹文件级 diff 预览，冲突 / 已跳过都会标出来）。 */
async function withdrawUserMessage(index) {
  return rollbackMessage(index, {
    title: '撤回这条消息？',
    doneText: '已撤回',
    plainText: '已撤回该消息及其后的回复',
  });
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
      li.title = (f.path || '') + (f.reason ? '\n' + f.reason : '');
      const ico = f.kind === 'dir' ? 'i-folder' : (RB_ICON[act] || 'i-pencil');
      li.innerHTML = '<svg class="ic"><use href="#' + ico + '"/></svg>'
        + '<span class="rb-path"></span><span class="rb-stat"></span><span class="rb-tag"></span>';
      li.querySelector('.rb-path').textContent = f.path || '';
      const tags = [RB_LABEL[act] || '将被修改'];
      if (f.skipped) tags.push('已跳过');
      if (f.conflict) tags.push('你改过');
      li.querySelector('.rb-tag').textContent = tags.join(' · ');
      if (f.skipped) li.classList.add('rb-skip');
      if (f.conflict) li.classList.add('rb-conflict');
      const adds = Number(f.adds) || 0;
      const dels = Number(f.dels) || 0;
      const stat = li.querySelector('.rb-stat');
      if (adds || dels) {
        stat.innerHTML = '<span class="rb-add">+' + adds + '</span> '
          + '<span class="rb-del">−' + dels + '</span>';
      }
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

/* 回退预览的副标题后缀：「…，并还原 N 个文件（+A −D）；其中 C 个你手动改过…」 */
function revertSub(lead, pv) {
  let sub = lead;
  if (pv) {
    const will = (pv.files || []).filter((f) => !f.skipped);
    sub += '，并还原 ' + will.length + ' 个文件';
    if (pv.adds || pv.dels) sub += '（+' + (pv.adds || 0) + ' −' + (pv.dels || 0) + '）';
    if (pv.conflicts) sub += '；其中 ' + pv.conflicts + ' 个你手动改过，回退会覆盖';
  }
  return sub + '。';
}

/* 丢弃 index 起的回复之前，先把这些消息产生的文件改动还原。
   先弹文件级预览（冲突 / 跳过与「回退」一致），确认后再按 token 撤销。
   返回 {ok, tokens, pv, undone}；ok=false 表示用户取消（此时磁盘与消息都没动）。 */
async function revertDiscardedFiles(index, opts) {
  const o = opts || {};
  const s = curSession();
  const msgs = (s && s.messages) || [];
  if (!s || typeof index !== 'number') {
    return { ok: false, tokens: [], pv: null, undone: 0 };
  }
  const tokens = [];
  for (let i = index; i < msgs.length; i += 1) {
    ((msgs[i] && msgs[i].tools) || []).forEach((it) => {
      if (it && it.undo) tokens.push(it.undo);
    });
  }
  const count = msgs.length - index;
  const pv = tokens.length
    ? await fetchRollbackPreview({ session_id: s.id, tokens: tokens })
    : null;
  const sub = typeof o.sub === 'function' ? o.sub(count, pv, tokens) : (o.sub || '');
  if (!(await rollbackConfirm({
    title: o.title || '确定要回退到这条消息之前吗？',
    sub: sub,
    files: pv ? pv.files : [],
    empty: o.empty || '这段对话没有可还原的文件改动。',
  }))) {
    return { ok: false, tokens: tokens, pv: pv, undone: 0 };
  }
  let undone = 0;
  if (tokens.length) {
    try {
      const u = await api('/api/agent/undo', { session_id: s.id, tokens: tokens });
      undone = (u && u.done) || 0;
    } catch (e) {
      if (!confirm('文件改动还原失败（' + e.message + '），仍然继续？')) {
        return { ok: false, tokens: tokens, pv: pv, undone: 0 };
      }
    }
    // 磁盘文件已经回滚，编辑器里打开的内容要跟着重读
    if (undone && typeof ideReloadAll === 'function') ideReloadAll();
  }
  return { ok: true, tokens: tokens, pv: pv, undone: undone };
}

/* 回退到某条消息之前：先按相反顺序还原这条及其后的文件写操作，再删掉这些消息。
   opts 可换标题 / 提示文案：「撤回」用户消息复用的就是这套流程。 */
async function rollbackMessage(index, opts) {
  const o = opts || {};
  if (isStreaming() || typeof index !== 'number') return;
  const s = curSession();
  const msgs = (s && s.messages) || [];
  if (!s || index < 0 || index >= msgs.length) return;
  const rb = await revertDiscardedFiles(index, {
    title: o.title || '确定要回退到这条消息之前吗？',
    empty: '这段对话没有可还原的文件改动。',
    sub: (count, pv, tokens) => {
      let sub = '会移除它及其后的 ' + count + ' 条消息';
      if (pv) {
        const will = (pv.files || []).filter((f) => !f.skipped);
        sub += '，并还原 ' + will.length + ' 个文件';
        if (pv.adds || pv.dels) sub += '（+' + (pv.adds || 0) + ' −' + (pv.dels || 0) + '）';
        if (pv.conflicts) sub += '；其中 ' + pv.conflicts + ' 个你手动改过，回退会覆盖';
      } else if (tokens.length) {
        sub += '，并还原 ' + tokens.length + ' 处文件改动';
      } else {
        sub += '。';
      }
      return sub;
    },
  });
  if (!rb.ok) return;
  const tokens = rb.tokens;
  const pv = rb.pv;
  const r = await api('/api/message/withdraw', { session_id: state.sessions.current, index: index });
  if (r.sessions) applySessionsPayload(r.sessions);
  renderSessions();
  renderMessages();
  if (r && r.context) renderContextBar(r.context);
  toast(tokens.length
    ? ((o.doneText || '已回退') + '，还原 '
       + ((pv && (pv.files || []).filter((f) => !f.skipped).length) || rb.undone)
       + ' 个文件' + (pv && pv.conflicts ? '（覆盖了 ' + pv.conflicts + ' 个手改文件）' : ''))
    : (o.plainText || '已回退到该消息之前'),
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
  if (isStreaming() || typeof index !== 'number') return;
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
    if (r.context) renderContextBar(r.context);
    toast('已删除该条消息', 'success');
  } catch (e) { toastErr('删除', e); }
}

/* 修改已发送的用户消息：不开内嵌编辑框，把文本退回底部输入框。
   发送时仍走 resend(mode:'edit') —— 丢弃其后的回复并重新生成，保留版本链；
   发送前先弹文件预览，把那些回复写过的文件一起还原（见 send.js 的 edit 分支）。
   附件不随文字退回（历史里只有落盘引用），需要重新添加。 */
function editUserMessage(index) {
  // 用实时状态判断：缓存的 state.streaming 会过期，漏判时会在生成中途重排消息流
  const busy = (typeof isStreaming === 'function') ? isStreaming() : state.streaming;
  if (busy) { toast('正在生成回复，等这轮结束再修改', 'error'); return; }
  if (typeof index !== 'number') return;
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
  // 附件也随「修改」一起回到输入框（只沿用落盘文件，不重新上传）
  if (typeof restoreEditAttachments === 'function') restoreEditAttachments(m);
  toast(m.images || m.image || m.doc || (m.docs && m.docs.length)
    ? '已退回输入框（附件已带回，可增删）· Esc 取消修改'
    : '已退回输入框，改完直接发送 · Esc 取消修改');
}

/* 取消「修改历史消息」：清掉高亮与标记，输入框里的文字保留 */
function cancelEditTarget(clearInput) {
  if (!editTarget) return;
  if (typeof dropReusedAttachments === 'function') dropReusedAttachments();
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
  if (isStreaming() || typeof index !== 'number') return;
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
  if (isStreaming() || typeof index !== 'number') return;
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
  if (isStreaming() || typeof index !== 'number') return;
  const s = curSession();
  const m = s && s.messages[index];
  if (!m || !Array.isArray(m.versions)) return;
  if (vidx < 0 || vidx >= m.versions.length || vidx === m.vidx) return;
  // 换版本会丢弃其后的回复并重新生成：先把那些回复的文件改动还原（弹窗预览）
  const rb = await revertDiscardedFiles(index, {
    title: '切换到第 ' + (vidx + 1) + ' 个版本并重新生成？',
    empty: '这条消息之后没有可还原的文件改动。',
    sub: (count, pv) => revertSub(
      Math.max(0, count - 1)
        ? ('会移除它之后的 ' + (count - 1) + ' 条回复并重新生成')
        : '会用这个版本重新生成回复',
      pv),
  });
  if (!rb.ok) return;
  verPreview = null;
  await resend({ mode: 'version', index: index, vidx: vidx });
}

async function resend(opts) {
  // opts.sid：调用方在 await 之前定下的目标会话；不给就取当前会话
  const sid = String(opts.sid || state.sessions.current || '');
  // 以该会话的实时流状态为准：state.streaming 是快照，切会话后可能过期
  if (typeof isStreaming === 'function' ? isStreaming(sid) : state.streaming) return;
  const s = (state.sessions.sessions || []).find((x) => String(x.id) === sid) || null;
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
  // 只有目标会话仍在前台时才需要立刻重画；后台会话等切回去由 renderMessages 重建
  if (String(state.sessions.current || '') === sid) renderMessages();
  const body = {
    resend: { mode: opts.mode, index: opts.index, vidx: opts.vidx },
    content: opts.content || '',
  };
  // 「修改」时附件也参与：新选的照常发；reuse_* 只是落盘文件名，服务端沿用原附件
  if (opts.imgs && opts.imgs.length) {
    body.images = opts.imgs.map((it) => ({ mime: it.mime, data: it.b64 }));
  }
  if (opts.docs && opts.docs.length) {
    body.docs = opts.docs.map((d) => ({ name: d.name, mime: d.mime, data: d.b64 }));
  }
  if (opts.reuseImages) body.reuse_images = opts.reuseImages;
  if (opts.reuseDocs) body.reuse_docs = opts.reuseDocs;
  await streamChat(body, null, null, { sid: sid });
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
    const trace = row.querySelector('.search-trace');
    if (trace && typeof decorateCitations === 'function') {
      decorateCitations(row._content, trace);
    }
    if (window.enhanceRichMedia) window.enhanceRichMedia(row._content);
  }
  if (row._selBtn) {
    const label = row._rawMode ? '显示渲染' : '选择文本';
    row._selBtn.dataset.tip = label;
    row._selBtn.setAttribute('aria-label', label);
    row._selBtn.innerHTML = icon(row._rawMode ? 'note' : 'select');
  }
}
