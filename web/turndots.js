'use strict';

/* 对话记录：聊天区左缘的悬浮圆点导轨。
   悬停展开「最近 12 条提问」，点一条跳回原处。
   分支入口不放在这里 —— 仍走消息悬浮菜单里的「创建分支」。 */

const TD_LIMIT = 12;
const tdState = { open: false, timer: null, active: -1 };

/* 压成一行可读预览：去掉代码块与 Markdown 标记，图片 / 附件给占位文案 */
function tdPreview(m) {
  let t = String((m && m.content) || '');
  t = t.replace(/```[\s\S]*?```/g, ' [代码] ')
       .replace(/!\[[^\]]*\]\([^)]*\)/g, '[图片]')
       .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')
       .replace(/[#>*`_~]/g, '')
       .replace(/\s+/g, ' ')
       .trim();
  if (!t && m && (m.images || m.image)) t = '[图片]';
  if (!t && m && m.doc) t = '[附件] ' + (m.doc.name || '');
  return t || '（空消息）';
}

/* 全部用户提问；n 是「第几次提问」，idx 是消息下标（跳转用）。
   面板一次只显示约 12 条，但数据是全量的 —— 往上滑能一直看到第一条。
   结果按会话指纹缓存：滚动时每帧都会调用，不能每帧对全部消息跑正则。 */
let _tdItemsCache = { sig: '', items: [] };

function tdItems() {
  const s = typeof curSession === 'function' ? curSession() : null;
  const msgs = (s && s.messages) || [];
  let lastUserAt = -1;
  for (let i = msgs.length - 1; i >= 0; i -= 1) {
    if (msgs[i] && msgs[i].role === 'user') { lastUserAt = i; break; }
  }
  const lastUser = lastUserAt >= 0 ? msgs[lastUserAt] : null;
  const sig = [
    (s && s.id) || '', (s && s.updated_at) || 0, msgs.length, lastUserAt,
    lastUser ? String(lastUser.content || '').length : 0,
    lastUser && lastUser.images ? 1 : 0, lastUser && lastUser.doc ? 1 : 0,
  ].join('|');
  if (_tdItemsCache.sig === sig) return _tdItemsCache.items;
  const all = [];
  msgs.forEach((m, idx) => {
    if (!m || m.role !== 'user') return;
    all.push({ idx: idx, n: all.length + 1, text: tdPreview(m) });
  });
  _tdItemsCache = { sig: sig, items: all };
  return all;
}

function tdMarkActive(idx) {
  $$('#td-rail .td-dot').forEach((el) => {
    el.classList.toggle('on', el.dataset.idx === String(idx));
  });
  $$('#td-list .td-row').forEach((el) => {
    el.classList.toggle('active', el.dataset.idx === String(idx));
  });
}

function tdRender() {
  const box = $('#turn-dots');
  const list = $('#td-list');
  if (!box || !list) return;
  const items = tdItems();
  box.classList.toggle('hidden', !items.length);
  const cnt = $('#td-count');
  if (cnt) cnt.textContent = items.length ? items.length + ' 条' : '';
  list.innerHTML = '';
  items.forEach((it) => {
    const row = document.createElement('div');
    row.className = 'td-row';
    row.dataset.idx = String(it.idx);
    row.dataset.tip = '第 ' + it.n + ' 条：' + it.text.slice(0, 40);
    row.title = it.text;
    const no = document.createElement('span');
    no.className = 'td-no';
    no.textContent = String(it.n);
    const tx = document.createElement('span');
    tx.className = 'td-text';
    tx.textContent = it.text;
    row.appendChild(no);
    row.appendChild(tx);
    row.onclick = () => tdGoto(it.idx);
    if (typeof makeClickable === 'function') {
      makeClickable(row, () => tdGoto(it.idx), '第 ' + it.n + ' 条提问：' + it.text.slice(0, 20));
    }
    list.appendChild(row);
  });
  if (tdState.active >= 0) tdMarkActive(tdState.active);
  _tdDotSig = '';                 // 消息集合可能变了，导轨下一帧重建
  tdSyncDots();
}

/* 当前读到哪一条提问：以聊天可视区顶部那条为准。
   以前这里只认「最后一次点击 / 悬停」，滚走之后高亮还停在旧位置，
   看起来就像特效跟错了地方。 */
function tdActiveIdx() {
  const chat = $('#chat');
  if (!chat) return -1;
  const box = chat.getBoundingClientRect();
  const line = box.top + Math.min(48, box.height * 0.18);
  const rows = chat.querySelectorAll('.msg.user[data-idx]');
  if (!rows.length) return -1;
  const idxOf = (el) => Number(el.dataset.idx);
  const first = idxOf(rows[0]);
  if (!Number.isFinite(first)) return -1;
  // 消息行按文档顺序从上到下排列：二分找「最后一个 top <= line」，
  // 避免每帧对几百行逐个 getBoundingClientRect 造成强制布局。
  let lo = 0;
  let hi = rows.length - 1;
  let hit = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (rows[mid].getBoundingClientRect().top <= line) {
      hit = mid;
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  if (hit < 0) return first;
  const i = idxOf(rows[hit]);
  return Number.isFinite(i) ? i : first;
}

/* 圆点覆盖整段对话，最多 TD_LIMIT 个：问题多时以当前位置为中心取窗口，
   保证「当前」那个点一定在导轨上。 */
function tdDotWindow(active) {
  const items = tdItems();
  if (items.length <= TD_LIMIT) return items;
  let at = items.findIndex((it) => it.idx >= active);
  if (at < 0) at = items.length - 1;
  const half = Math.floor(TD_LIMIT / 2);
  const from = Math.max(0, Math.min(at - half, items.length - TD_LIMIT));
  return items.slice(from, from + TD_LIMIT);
}

let _tdDotSig = '';

function tdSyncDots() {
  const rail = $('#td-rail');
  if (!rail) return;
  const active = tdActiveIdx();
  if (active >= 0) tdState.active = active;
  const picked = tdDotWindow(tdState.active);
  // 滚动时每帧只更新高亮；圆点集合没变就不重建 DOM，避免 hover 闪烁
  const sig = picked.map((it) => it.idx + ':' + it.text.length).join(',');
  if (sig !== _tdDotSig) {
    _tdDotSig = sig;
    rail.innerHTML = '';
    picked.forEach((it) => {
      const dot = document.createElement('span');
      dot.className = 'td-dot';
      dot.dataset.idx = String(it.idx);
      dot.title = '第 ' + it.n + ' 条：' + it.text.slice(0, 40);
      dot.onclick = () => tdGoto(it.idx);
      rail.appendChild(dot);
    });
  }
  tdMarkActive(tdState.active);
}

/* 目标不在当前渲染窗口、或还没从后端加载时，交给 ssFocusMessage 先补数据再定位；
   它不可用才退回 tdJump。 */
function tdGoto(idx) {
  if (typeof ssFocusMessage === 'function') {
    ssFocusMessage(idx).catch(() => tdJump(idx));
    return;
  }
  tdJump(idx);
}

/* 跳到某条消息：以目标为第一行开窗重排（比整段插到最前面稳，也更快），
   然后顶部对齐 —— 单条消息可能高达数千像素，居中会落在正文中间。 */
function tdJump(idx) {
  const chat = $('#chat');
  if (!chat) return;
  const start = Number(chat.dataset.start);
  if (Number.isFinite(start) && idx < start && typeof renderMessages === 'function') {
    renderMessages(idx, true);
  }
  const row = chat.querySelector('.msg[data-idx="' + idx + '"]');
  if (!row) {
    toast('找不到这条消息，可能已被撤回', 'error');
    return;
  }
  try { row.scrollIntoView({ block: 'start', behavior: 'auto' }); }
  catch (e) { row.scrollIntoView(true); }
  row.classList.remove('jump-hit');
  void row.offsetWidth;                    // 强制重排，否则连续点同一行不会重新触发描边
  row.classList.add('jump-hit');
  setTimeout(() => row.classList.remove('jump-hit'), 900);
  tdState.active = idx;
  tdMarkActive(idx);
  tdOpen(false);
}

/* 面板在「聊天列去掉输入区」的剩余高度里垂直居中：
   直接用 50% 会按整列算，窄窗口输入区高时面板底部会压到输入框。 */
function tdFit() {
  const panel = $('#td-panel');
  const col = document.querySelector('.chat-col');
  const comp = document.querySelector('.composer');
  const dots = $('#turn-dots');
  if (!panel || !col || !comp || !dots) return;
  const colBox = col.getBoundingClientRect();
  const compBox = comp.getBoundingClientRect();
  const dotsBox = dots.getBoundingClientRect();
  const avail = Math.max(160, compBox.top - colBox.top - 12);
  panel.style.maxHeight = Math.round(avail) + 'px';
  panel.style.top = Math.round(colBox.top + avail / 2 - dotsBox.top) + 'px';
}

function tdOpen(on) {
  const panel = $('#td-panel');
  if (!panel) return;
  tdState.open = !!on;
  panel.classList.toggle('hidden', !tdState.open);
  if (tdState.open) {
    tdRender();
    tdFit();
    // 打开时把「当前读到的那条」滚到面板中间，而不是永远停在最新一条
    const list = $('#td-list');
    const act = list && list.querySelector('.td-row[data-idx="' + tdState.active + '"]');
    if (list && act) {
      list.scrollTop = Math.max(0, act.offsetTop - list.clientHeight / 2 + act.offsetHeight / 2);
    } else if (list) {
      list.scrollTop = list.scrollHeight;
    }
    tdSyncDots();
  }
}

/* 供 chat.js 在每次重排消息后调用 */
function turnDotsSync() {
  tdRender();
  qmarksRender();
}

/* ============================================================
   提问标记：给关键提问起名，Solo 模式右侧一个标记一个悬浮按钮。
   数据存在会话的 qmarks 里（服务端持久化），按钮点击复用 tdJump。
   ============================================================ */

function qmarkOf(idx) {
  const s = typeof curSession === 'function' ? curSession() : null;
  const list = (s && s.qmarks) || [];
  const want = Number(idx);
  return list.find((m) => Number(m && m.idx) === want) || null;
}

function qmarkList() {
  const s = typeof curSession === 'function' ? curSession() : null;
  return ((s && s.qmarks) || [])
    .filter((m) => m && Number.isFinite(Number(m.idx)))
    .map((m) => ({ idx: Number(m.idx), label: String(m.label || ''), text: String(m.text || '') }))
    .sort((a, b) => a.idx - b.idx);
}

function qmarksRender() {
  const box = $('#qmarks');
  if (!box) return;
  const items = qmarkList();
  box.innerHTML = '';
  box.classList.toggle('hidden', !items.length);
  items.forEach((it) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'qmark-btn';
    btn.dataset.idx = String(it.idx);
    btn.title = (it.label || '提问标记') + (it.text ? '：' + it.text : '')
      + '\n单击跳转，右键改名 / 删除';
    btn.innerHTML = (typeof icon === 'function' ? icon('pin') : '')
      + '<span class="qmark-label"></span>';
    btn.querySelector('.qmark-label').textContent = it.label || ('标记 ' + (it.idx + 1));
    btn.onclick = () => qmarkJump(it.idx);
    btn.oncontextmenu = (e) => { e.preventDefault(); qmarkEdit(it.idx); };
    box.appendChild(btn);
  });
}

/* 跳转优先用 ssFocusMessage：它会先把目标之前的消息分页补回来，
   再定位 / 高亮；tdJump 只适合「已经在当前渲染窗口里」的索引。 */
function qmarkJump(idx) {
  if (typeof ssFocusMessage === 'function') {
    ssFocusMessage(idx).catch(() => tdJump(idx));
    return;
  }
  tdJump(idx);
}

function syncQmarkBtn(idx) {
  const row = document.querySelector('.msg[data-idx="' + idx + '"]');
  const btn = row && row.querySelector('.msg-act[data-qmark]');
  if (btn) btn.classList.toggle('act-on', !!qmarkOf(idx));
}

/* 消息悬浮菜单 / 悬浮按钮右键共用：留空 = 删除标记 */
async function qmarkEdit(idx) {
  const s = typeof curSession === 'function' ? curSession() : null;
  const m = s && s.messages && s.messages[idx];
  if (!m || m.role !== 'user') { toast('只能标记用户提问', 'error'); return; }
  const cur = qmarkOf(idx);
  const preview = (typeof tdPreview === 'function' ? tdPreview(m) : String(m.content || ''))
    .slice(0, 18);
  const promptText = cur
    ? '重命名这个标记（留空 = 删除标记）'
    : '给这次提问起个名字（会显示在右侧悬浮按钮上）';
  let val;
  try {
    val = await appPrompt(promptText, cur ? cur.label : preview);
  } catch (e) {
    toast('无法打开输入框', 'error');
    return;
  }
  if (val === null) return;                       // 取消
  const label = String(val).trim().slice(0, 40);
  if (!label && !cur) return;                     // 新建时留空：什么都不做
  await qmarkSave(idx, label, preview);
}

async function qmarkSave(idx, label, preview) {
  const s = typeof curSession === 'function' ? curSession() : null;
  if (!s) return;
  try {
    const r = await api('/api/session/qmark', {
      id: s.id, idx: idx, label: label, text: preview,
    });
    if (!r || !r.ok) throw new Error((r && r.error) || '保存失败');
    if (r.sessions) applySessionsPayload(r.sessions);
    qmarksRender();
    syncQmarkBtn(idx);
    toast(label ? '已标记：' + label : '已取消标记', 'success');
  } catch (e) {
    toast('标记失败：' + (e.message || e), 'error');
  }
}

function bindTurnDots() {
  const box = $('#turn-dots');
  if (!box) return;
  box.addEventListener('mouseenter', () => {
    clearTimeout(tdState.timer);
    tdOpen(true);
  });
  box.addEventListener('mouseleave', () => {
    clearTimeout(tdState.timer);
    tdState.timer = setTimeout(() => tdOpen(false), 160);
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && tdState.open) tdOpen(false);
  });
  window.addEventListener('resize', () => { if (tdState.open) tdFit(); });
  // 列表往上滑时圆点跟着换：用 rAF 合并同一帧里的多次 scroll
  const list = $('#td-list');
  if (list) {
    let raf = 0;
    list.addEventListener('scroll', () => {
      if (raf) return;
      raf = requestAnimationFrame(() => { raf = 0; tdSyncDots(); });
    });
  }
  // 聊天区滚动：高亮跟着「当前读到的那条提问」走（这是导轨的主语义）
  const chat = $('#chat');
  if (chat) {
    let raf2 = 0;
    chat.addEventListener('scroll', () => {
      if (raf2) return;
      raf2 = requestAnimationFrame(() => { raf2 = 0; tdSyncDots(); });
    }, { passive: true });
  }
}

bindTurnDots();
