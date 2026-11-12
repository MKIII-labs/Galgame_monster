'use strict';

/* 空状态标记只在 index.html 维护一份：脚本在 body 末尾执行，此时 #empty 已存在。
   重新渲染时直接复用这份快照，避免两处各写一遍导致走样。 */
const EMPTY_HTML = (() => {
  const el = document.getElementById('empty');
  return el ? el.outerHTML : '';
})();

/* ============================================================
   会话渲染
   ============================================================ */
function curSession() {
  return state.sessions.sessions.find((s) => s.id === state.sessions.current) || null;
}

const SESSION_GROUP_COLLAPSED_KEY = 'gm.session.group.collapsed.v1';
const SESSION_GROUP_WATERMARK = 5;      // 每组默认先显示几条
const SESSION_GROUP_NONE = '__none__';   // 还没选目录的会话

function sessionGroupMode() {
  return (state.config && state.config.session_group_mode) === 'flat' ? 'flat' : 'dir';
}

/* 折叠状态存 localStorage（跨刷新保留）；水位只存内存，刷新后回到 5 条，
   和 DSH 的 watermark 语义一致。 */
function sessionGroupPrefs() {
  if (!state.sessionGroups) {
    let collapsed = {};
    try {
      const raw = localStorage.getItem(SESSION_GROUP_COLLAPSED_KEY);
      const obj = raw ? JSON.parse(raw) : {};
      if (obj && typeof obj === 'object' && !Array.isArray(obj)) collapsed = obj;
    } catch (e) { collapsed = {}; }
    state.sessionGroups = { collapsed: collapsed, watermark: {} };
  }
  return state.sessionGroups;
}

function sessionLive(s) {
  return !!(state.streams && state.streams[s.id])
    || !!(state.streamingSids && state.streamingSids[s.id]);
}

function sessionUnread(s) {
  return !!(state.unread && state.unread[s.id]);
}

/* 待审批 / 待回答由后端登记（session_status），经 /api/state 与常驻 SSE 同步过来；
   运行中 / 未读仍是前端自己的状态。优先级：待处理 > 运行中 > 未读 > 空闲。 */
function sessionPending(s) {
  return (state.pending && state.pending[s.id]) || '';
}

function sessionStatusOf(s) {
  const pending = sessionPending(s);
  if (pending === 'approval') return { kind: 'pending-approval', label: '待批准', title: '等你批准' };
  if (pending === 'ask') return { kind: 'pending-ask', label: '待回答', title: '等你回答' };
  if (sessionLive(s)) return { kind: 'streaming', label: '', title: '正在生成' };
  if (sessionUnread(s)) return { kind: 'unread', label: '', title: '有新回复' };
  return { kind: '', label: '', title: '' };
}

/* 拖拽排序：只允许在同一分组（平铺时是整列）且同为置顶 / 非置顶的行之间移动。
   跨目录拖动没有意义（组是派生的），跨置顶分界会被 pinned-first 排序立刻弹回去。 */
function sessionDragOrder(drag, targetId, before) {
  const inGal = state.uiMode === 'gal';
  const flat = sessionGroupMode() === 'flat';
  const key = drag.sandbox_key || SESSION_GROUP_NONE;
  const rows = (state.sessions.sessions || []).filter((x) => {
    const isGal = (x.gal_scope || 'main') === 'gal';
    if (inGal ? !isGal : isGal) return false;
    return flat || (x.sandbox_key || SESSION_GROUP_NONE) === key;
  });
  const ids = rows.map((x) => x.id).filter((id) => id !== drag.id);
  let at = ids.indexOf(targetId);
  if (at < 0) return null;
  if (!before) at += 1;
  ids.splice(at, 0, drag.id);
  return ids;
}

/* 键盘排序：同一分组、同一置顶分区内上下移动一格（Alt+↑/↓）。
   既给键盘用户一条路，也让拖拽逻辑有一个可自动验证的入口。 */
function moveSessionBy(s, delta) {
  const inGal = state.uiMode === 'gal';
  const flat = sessionGroupMode() === 'flat';
  const key = s.sandbox_key || SESSION_GROUP_NONE;
  const rows = (state.sessions.sessions || []).filter((x) => {
    const isGal = (x.gal_scope || 'main') === 'gal';
    if (inGal ? !isGal : isGal) return false;
    if (!flat && (x.sandbox_key || SESSION_GROUP_NONE) !== key) return false;
    return !!x.pinned === !!s.pinned;
  });
  const ids = rows.map((x) => x.id);
  const at = ids.indexOf(s.id);
  const to = at + delta;
  if (at < 0 || to < 0 || to >= ids.length) return;
  ids.splice(at, 1);
  ids.splice(to, 0, s.id);
  applySessionOrder(ids);
}

function applySessionOrder(ids) {
  const arr = state.sessions.sessions || [];
  const index = new Map(arr.map((s, i) => [s.id, i]));
  const picked = ids.map((id) => arr[index.get(id)]);
  const slots = ids.map((id) => index.get(id)).sort((a, b) => a - b);
  slots.forEach((slot, k) => { if (picked[k]) arr[slot] = picked[k]; });
  renderSessions();
  api('/api/session/reorder', { ids: ids }).then((r) => {
    if (r && r.sessions && typeof applySessionsPayload === 'function') {
      applySessionsPayload(r.sessions);
      renderSessions();
    }
  }).catch((e) => toastErr('保存排序', e));
}

/* 单个会话行：与平铺模式共用，分组只是外面套一层组头。 */
function sessionRowEl(s) {
  const status = sessionStatusOf(s);
  const el = document.createElement('div');
  el.className = 'session-item' + (s.id === state.sessions.current ? ' active' : '')
    + (status.kind ? ' ' + status.kind : '')
    + (s.pinned ? ' pinned' : '');
  // 悬停显示完整会话名：标题过长时列表里会被省略号截断，靠 title 看全名
  el.title = (s.pinned ? '已置顶 · ' : '') + (s.title || '新对话');
  const dot = document.createElement('span');
  dot.className = 'sdot';
  dot.title = status.title;
  const title = document.createElement('span');
  title.className = 'stitle';
  title.textContent = s.title || '新对话';
  const pin = document.createElement('span');
  pin.className = 'spin';
  pin.innerHTML = icon('pin');
  pin.title = s.pinned ? '取消置顶' : '置顶会话';
  pin.onclick = (e) => { e.stopPropagation(); toggleSessionPin(s.id); };
  const exp = document.createElement('span');
  exp.className = 'sexp';
  exp.textContent = '⤓';
  exp.title = '导出为 Markdown';
  exp.onclick = (e) => { e.stopPropagation(); exportSession(s.id); };
  const ren = document.createElement('span');
  ren.className = 'sren';
  ren.innerHTML = icon('pencil');
  ren.title = '重命名';
  ren.onclick = (e) => { e.stopPropagation(); renameSession(s.id); };
  const del = document.createElement('span');
  del.className = 'sdel';
  del.innerHTML = icon('trash');
  del.title = '删除会话';
  del.onclick = (e) => { e.stopPropagation(); deleteSession(s.id); };
  // 操作键收进一个悬浮层：不占标题宽度，窄侧栏下标题才不会被挤成「AI...」
  const acts = document.createElement('span');
  acts.className = 'sacts';
  acts.appendChild(pin);
  acts.appendChild(exp);
  acts.appendChild(ren);
  acts.appendChild(del);
  el.appendChild(dot);
  if (s.pinned) {                       // 已置顶：标题前常驻一枚小图钉
    const mark = document.createElement('span');
    mark.className = 'spin-mark';
    mark.innerHTML = icon('pin');
    mark.title = '已置顶';
    el.appendChild(mark);
  }
  el.appendChild(title);
  if (status.label) {                   // 待批准 / 待回答：光靠圆点分不清，补两个字
    const st = document.createElement('span');
    st.className = 'sstatus';
    st.textContent = status.label;
    el.appendChild(st);
  }
  el.appendChild(acts);
  el.onclick = () => selectSession(s.id);
  el.ondblclick = () => renameSession(s.id);
  // 键盘可达：会话项本身与四个操作键都能 Tab 到、Enter 触发
  makeClickable(el, () => selectSession(s.id), '会话：' + (s.title || '新对话'));
  makeClickable(pin, () => toggleSessionPin(s.id),
                (s.pinned ? '取消置顶会话：' : '置顶会话：') + (s.title || '新对话'));
  makeClickable(exp, () => exportSession(s.id), '导出会话：' + (s.title || '新对话'));
  makeClickable(ren, () => renameSession(s.id), '重命名会话：' + (s.title || '新对话'));
  makeClickable(del, () => deleteSession(s.id), '删除会话：' + (s.title || '新对话'));

  // ---- 拖拽排序（HTML5 DnD；触屏没有 hover/drag，不启用）----
  el.draggable = true;
  el.ondragstart = (e) => {
    state.dragSession = s;
    el.classList.add('dragging');
    try {
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', s.id);
    } catch (err) { /* 老浏览器不支持 dataTransfer 时忽略 */ }
  };
  el.ondragend = () => {
    state.dragSession = null;
    el.classList.remove('dragging');
    document.querySelectorAll('.session-item.drop-before, .session-item.drop-after')
      .forEach((x) => x.classList.remove('drop-before', 'drop-after'));
  };
  el.ondragover = (e) => {
    const drag = state.dragSession;
    if (!drag || drag.id === s.id) return;
    if (sessionGroupMode() !== 'flat'
        && (drag.sandbox_key || SESSION_GROUP_NONE) !== (s.sandbox_key || SESSION_GROUP_NONE)) return;
    if (!!drag.pinned !== !!s.pinned) return;        // 不跨置顶分界
    e.preventDefault();
    try { e.dataTransfer.dropEffect = 'move'; } catch (err) { /* ignore */ }
    const rect = el.getBoundingClientRect();
    const before = e.clientY < rect.top + rect.height / 2;
    el.classList.toggle('drop-before', before);
    el.classList.toggle('drop-after', !before);
  };
  el.ondragleave = () => el.classList.remove('drop-before', 'drop-after');
  // 键盘可达的排序（拖拽的等价入口）
  el.addEventListener('keydown', (e) => {
    if (!e.altKey || (e.key !== 'ArrowUp' && e.key !== 'ArrowDown')) return;
    e.preventDefault();
    moveSessionBy(s, e.key === 'ArrowUp' ? -1 : 1);
  });
  el.ondrop = (e) => {
    const drag = state.dragSession;
    e.preventDefault();
    const before = el.classList.contains('drop-before');
    el.classList.remove('drop-before', 'drop-after');
    if (!drag || drag.id === s.id) return;
    const ids = sessionDragOrder(drag, s.id, before);
    if (ids) applySessionOrder(ids);
  };
  return el;
}

/* 按 sandbox_key 分组：key/name 由后端在 slim_sessions 里算好（已做 normcase）。 */
function sessionGroupsOf(list) {
  const map = new Map();
  list.forEach((s) => {
    const key = s.sandbox_key || SESSION_GROUP_NONE;
    if (!map.has(key)) {
      map.set(key, {
        key: key,
        name: s.sandbox_name || '',
        path: s.sandbox || '',
        rows: [],
        updated: 0,
      });
    }
    const g = map.get(key);
    g.rows.push(s);
    g.updated = Math.max(g.updated, Number(s.updated_at) || 0);
  });
  const groups = Array.from(map.values());
  const cfg = state.config || {};
  const names = cfg.session_group_names || {};
  const pins = new Set(cfg.session_group_pins || []);
  groups.forEach((g) => {
    // 组内：置顶在前；sort 是稳定排序，其余保持后端给的顺序
    g.rows.sort((a, b) => (a.pinned === b.pinned ? 0 : (a.pinned ? -1 : 1)));
    g.alias = names[g.key] || '';
    g.display = g.path ? (g.alias || g.name || g.path) : '未选择目录';
    g.groupPinned = pins.has(g.key);
  });
  // 组间：置顶组最前，其余按最近更新倒序；「未选择目录」固定最后
  groups.sort((a, b) => {
    if (a.key === SESSION_GROUP_NONE) return 1;
    if (b.key === SESSION_GROUP_NONE) return -1;
    if (a.groupPinned !== b.groupPinned) return a.groupPinned ? -1 : 1;
    return b.updated - a.updated;
  });
  return groups;
}

async function openGroupFolder(path) {
  try {
    await api('/api/open/folder', { path: path });
  } catch (e) {
    toastErr('打开文件夹', e);
  }
}

function closeGroupMenus() {
  document.querySelectorAll('.sg-menu').forEach((x) => x.remove());
}

async function setGroupPin(group, pinned) {
  try {
    const r = await api('/api/session/group', { op: 'pin', key: group.key, pinned: !!pinned });
    state.config = Object.assign({}, state.config || {}, {
      session_group_pins: (r && r.group_pins) || [],
    });
    renderSessions();
    toast(pinned ? '已置顶分组' : '已取消置顶', 'ok');
  } catch (e) { toastErr('分组置顶', e); }
}

async function renameGroup(group) {
  const name = await appPrompt('重命名分组（只改显示名，不动磁盘目录）',
                               group.alias || group.name || '');
  if (name === null) return;
  try {
    const r = await api('/api/session/group', {
      op: 'rename', key: group.key, name: String(name).trim(),
    });
    state.config = Object.assign({}, state.config || {}, {
      session_group_names: (r && r.group_names) || {},
    });
    renderSessions();
    toast(String(name).trim() ? '已重命名分组' : '已恢复原名称', 'ok');
  } catch (e) { toastErr('重命名分组', e); }
}

async function deleteGroup(group) {
  const label = group.display || group.name || '未选择目录';
  const n = group.rows.length;
  if (!confirm('删除分组「' + label + '」？\n将删除组内 ' + n
               + ' 个会话，附件与存档一并删除，不可恢复。')) return;
  try {
    const r = await api('/api/session/group', {
      op: 'delete', key: group.key, ids: group.rows.map((s) => s.id),
    });
    if (!r || !r.ok) throw new Error((r && r.error) || '删除失败');
    applySessionsPayload(r.sessions, { keepCurrent: false });
    renderSessions();
    toast('已删除分组「' + label + '」', 'ok');
  } catch (e) { toastErr('删除分组', e); }
}

/* 组头的「⋯」菜单：置顶 / 重命名只写 config，删除才是真删会话 */
function openGroupMenu(anchor, group) {
  closeGroupMenus();
  const menu = document.createElement('div');
  menu.className = 'sg-menu';
  const add = (label, fn, danger) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = label;
    if (danger) b.className = 'danger';
    b.onclick = (e) => {
      e.stopPropagation();
      closeGroupMenus();
      fn();
    };
    menu.appendChild(b);
  };
  if (group.key !== SESSION_GROUP_NONE) {
    add(group.groupPinned ? '取消置顶分组' : '置顶分组',
        () => setGroupPin(group, !group.groupPinned));
    add('重命名分组', () => renameGroup(group));
  }
  add('删除组内全部会话', () => deleteGroup(group), true);
  anchor.appendChild(menu);
  const rect = anchor.getBoundingClientRect();
  menu.classList.toggle('up', rect.bottom + 150 > window.innerHeight);
  setTimeout(() => {
    const off = (ev) => {
      if (!menu.contains(ev.target)) {
        menu.remove();
        document.removeEventListener('click', off, true);
      }
    };
    document.addEventListener('click', off, true);
  }, 0);
}

function sessionGroupEl(group) {
  const prefs = sessionGroupPrefs();
  const collapsed = !!prefs.collapsed[group.key];
  const wrap = document.createElement('div');
  wrap.className = 'session-group' + (collapsed ? ' collapsed' : '');

  const headWrap = document.createElement('div');
  headWrap.className = 'session-group-head-wrap';

  const head = document.createElement('button');
  head.type = 'button';
  head.className = 'session-group-head';
  head.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
  head.title = group.path || '这些会话还没有选择目录';
  head.innerHTML = icon('folder')
    + '<span class="sg-name"></span>'
    + (group.groupPinned ? '<span class="sg-pin">' + icon('pin') + '</span>' : '')
    + '<span class="sg-count">' + group.rows.length + '</span>'
    + '<span class="sg-caret">' + icon('chevron') + '</span>';
  head.querySelector('.sg-name').textContent = group.display;
  head.onclick = () => {
    const next = !prefs.collapsed[group.key];          // true = 收起
    if (next) prefs.collapsed[group.key] = true;
    else delete prefs.collapsed[group.key];
    try {
      localStorage.setItem(SESSION_GROUP_COLLAPSED_KEY, JSON.stringify(prefs.collapsed));
    } catch (e) { /* 隐私模式 / 配额满：只影响跨刷新记忆 */ }
    // 只动这一组，不整列表重绘，折叠才有过渡动画
    foldTransition(wrap, wrap.querySelector('.session-group-body'), !next, {
      cls: 'collapsed',
    });
    head.setAttribute('aria-expanded', next ? 'false' : 'true');
  };
  headWrap.appendChild(head);

  // 目录分组：一键在文件管理器里打开真实目录（「未选择目录」没有路径，不显示）
  if (group.path) {
    const openBtn = document.createElement('button');
    openBtn.type = 'button';
    openBtn.className = 'session-group-open';
    openBtn.title = '在文件管理器中打开';
    openBtn.setAttribute('aria-label', '在文件管理器中打开：' + group.display);
    openBtn.innerHTML = icon('external');
    openBtn.onclick = (e) => {
      e.stopPropagation();
      openGroupFolder(group.path);
    };
    headWrap.appendChild(openBtn);
  }

  const menuBtn = document.createElement('button');
  menuBtn.type = 'button';
  menuBtn.className = 'session-group-menu';
  menuBtn.title = '分组操作';
  menuBtn.setAttribute('aria-label', '分组操作：' + group.display);
  menuBtn.textContent = '⋯';
  menuBtn.onclick = (e) => {
    e.stopPropagation();
    openGroupMenu(headWrap, group);
  };
  headWrap.appendChild(menuBtn);
  wrap.appendChild(headWrap);

  const body = document.createElement('div');
  body.className = 'session-group-body';
  const limit = Math.max(SESSION_GROUP_WATERMARK,
                         Number(prefs.watermark[group.key]) || SESSION_GROUP_WATERMARK);
  let shown = 0;
  group.rows.forEach((s, i) => {
    // 过了水位但「有状态」的会话照样露出来：正在跑 / 未读 / 当前会话
    if (i < limit || sessionLive(s) || sessionUnread(s) || s.id === state.sessions.current) {
      body.appendChild(sessionRowEl(s));
      shown += 1;
    }
  });
  wrap.appendChild(body);

  const rest = group.rows.length - shown;
  if (rest > 0) {
    const more = document.createElement('button');
    more.type = 'button';
    more.className = 'session-group-more';
    more.textContent = '显示更多（还有 ' + rest + ' 条）';
    more.onclick = () => {
      prefs.watermark[group.key] = limit + SESSION_GROUP_WATERMARK;
      renderSessions();
    };
    wrap.appendChild(more);
  } else if (limit > SESSION_GROUP_WATERMARK) {
    const less = document.createElement('button');
    less.type = 'button';
    less.className = 'session-group-more';
    less.textContent = '收起';
    less.onclick = () => {
      delete prefs.watermark[group.key];
      renderSessions();
    };
    wrap.appendChild(less);
  }
  return wrap;
}

function syncSessionGroupToggle() {
  const btn = $('#session-group-toggle');
  if (!btn) return;
  const grouped = sessionGroupMode() === 'dir';
  btn.classList.toggle('active', grouped);
  btn.setAttribute('aria-pressed', grouped ? 'true' : 'false');
  btn.title = grouped ? '按目录分组（点击切为平铺）' : '平铺（点击按目录分组）';
}

function bindSessionGroupToggle() {
  const btn = $('#session-group-toggle');
  if (!btn) return;
  btn.onclick = () => {
    const next = sessionGroupMode() === 'dir' ? 'flat' : 'dir';
    state.config = Object.assign({}, state.config || {}, { session_group_mode: next });
    renderSessions();
    api('/api/config', { session_group_mode: next }).catch((e) => toastErr('保存分组方式', e));
  };
}

function renderSessions() {
  const box = $('#session-list');
  box.innerHTML = '';
  // 剧场会话与主会话分列表展示：GAL 模式只看 gal_scope=gal，其余模式反之。
  const inGal = state.uiMode === 'gal';
  const all = (state.sessions.sessions || []).filter((s) => {
    const isGal = (s.gal_scope || 'main') === 'gal';
    return inGal ? isGal : !isGal;
  });
  if (sessionGroupMode() === 'flat') {
    all.forEach((s) => box.appendChild(sessionRowEl(s)));
  } else {
    sessionGroupsOf(all).forEach((g) => box.appendChild(sessionGroupEl(g)));
  }
  syncSessionGroupToggle();
  updateStatusLine();
  syncAgentUI();
  if (typeof renderKbBadge === 'function') renderKbBadge();   // 侧栏知识库「未绑定」角标
  if (typeof syncAutoChatFromSession === 'function') syncAutoChatFromSession();
}

/* ---------------- 会话搜索浮层 ---------------- */
const ssState = { open: false, items: [], timer: null, q: '' };

function ssOpen() {
  const modal = $('#session-search-modal');
  const input = $('#ss-input');
  if (!modal || !input) return;
  ssState.open = true;
  modal.classList.remove('hidden');
  input.value = '';
  const btn = $('#session-search-btn');
  if (btn) { btn.classList.add('active'); btn.setAttribute('aria-expanded', 'true'); }
  input.focus();
  ssFetch('');
}

function ssClose() {
  const modal = $('#session-search-modal');
  const input = $('#ss-input');
  ssState.open = false;
  ssState.items = [];
  clearTimeout(ssState.timer);
  if (modal) modal.classList.add('hidden');
  if (input) input.value = '';
  const btn = $('#session-search-btn');
  if (btn) { btn.classList.remove('active'); btn.setAttribute('aria-expanded', 'false'); }
}

async function ssFetch(q) {
  const list = $('#ss-results');
  ssState.q = String(q || '');
  if (list) list.innerHTML = '<div class="ss-empty">读取中…</div>';
  try {
    const r = await api('/api/session/search', {
      q: q,
      scope: state.uiMode === 'gal' ? 'gal' : 'main',
    });
    if (!ssState.open) return;                 // 请求返回前已关闭
    ssState.items = (r && r.items) || [];
    ssRender(q);
  } catch (e) {
    if (list) list.innerHTML = '<div class="ss-empty">搜索失败：' + escapeHtml(String(e)) + '</div>';
  }
}

function ssRender(q) {
  const list = $('#ss-results');
  if (!list) return;
  list.innerHTML = '';
  if (!ssState.items.length) {
    const d = document.createElement('div');
    d.className = 'ss-empty';
    d.textContent = q ? '没有匹配的会话' : '还没有会话';
    list.appendChild(d);
    return;
  }
  ssState.items.forEach((it) => {
    const row = document.createElement('div');
    row.className = 'ss-row';
    const head = document.createElement('div');
    head.className = 'ss-row-head';
    const title = document.createElement('span');
    title.className = 'ss-title';
    title.textContent = it.title || '新对话';
    const meta = document.createElement('span');
    meta.className = 'ss-meta';
    meta.textContent = (it.msg_count || 0) + ' 条';
    head.appendChild(title);
    head.appendChild(meta);
    row.appendChild(head);
    (it.hits || []).slice(0, 2).forEach((h) => {
      const p = document.createElement('div');
      p.className = 'ss-hit';
      p.textContent = h.snippet;
      p.title = '跳到这条消息';
      // 点命中片段 → 打开会话并滚到那条消息；点行内其它区域只打开会话
      p.onclick = (e) => { e.stopPropagation(); ssPick(it.id, h.index, ssState.q); };
      row.appendChild(p);
    });
    row.onclick = () => ssPick(it.id);
    list.appendChild(row);
  });
}

async function ssPick(id, msgIndex, q) {
  ssClose();
  await selectSession(id);
  if (Number.isFinite(msgIndex)) await ssFocusMessage(msgIndex, q);
}

/* 跳到某条消息：先把「更早」的分页补到目标之前，再以它上方几行为起点重排。
   重排后 renderMessages 会按「保持视觉位置」重设 scrollTop（展开更早的语义），
   这里必须覆盖它；单条消息可能上万像素，所以优先按命中词的真实位置对齐。
   富媒体/图片异步加载还会改高度，因此再跟几帧做校正。 */
async function ssFocusMessage(idx, q) {
  const s = curSession();
  const chat = $('#chat');
  if (!s || !chat) return;
  const start = Math.max(0, idx - 3);
  let guard = 0;
  while ((Number(s.msg_loaded_start) || 0) > start && guard < 30) {
    const ok = await loadOlderMessages(Number(s.msg_loaded_start), undefined, true);
    if (!ok) break;
    guard += 1;
  }
  renderMessages(start, true);
  _followBottom = false;      // 别让后续增量渲染把视图拉回底部

  const align = () => {
    const row = chat.querySelector('[data-idx="' + idx + '"]');
    if (!row) return false;
    const cr = chat.getBoundingClientRect();
    const range = ssFindTextRange(row, q);
    if (!range) {
      // 没有关键词（或在富文本里被拆散）：短消息居中，超长消息贴顶
      const rr = row.getBoundingClientRect();
      const pad = rr.height <= chat.clientHeight ? (chat.clientHeight - rr.height) / 2 : 24;
      chat.scrollTop += (rr.top - cr.top) - pad;
    } else {
      ssScrollSideways(range);
      const rect = range.getClientRects()[0];
      chat.scrollTop += (rect.top - cr.top) - chat.clientHeight / 2;
    }
    return true;
  };

  if (!align()) { toast('这条消息已不在当前视图里'); return; }
  for (let i = 0; i < 5; i++) {
    await new Promise((r) => setTimeout(r, 90));
    align();
  }
  const row = chat.querySelector('[data-idx="' + idx + '"]');
  if (!row) return;
  // 高亮要画在命中词上：这条消息可能有上万像素高，画在外框上根本看不见
  const range = ssFindTextRange(row, q);
  if (range) ssFlashTextRange(range);
  else {
    row.classList.add('ss-msg-flash');
    setTimeout(() => row.classList.remove('ss-msg-flash'), 1600);
  }
}

/* 在一条消息的文本节点里找关键词，返回 Range；找不到返回 null。
   跨节点拆散（富文本把词切开）时会退化成整条消息定位。 */
function ssFindTextRange(row, q) {
  const needle = String(q || '').trim().toLowerCase();
  if (!needle) return null;
  const walker = document.createTreeWalker(row, NodeFilter.SHOW_TEXT);
  let node;
  while ((node = walker.nextNode())) {
    const pos = (node.nodeValue || '').toLowerCase().indexOf(needle);
    if (pos < 0) continue;
    const range = document.createRange();
    range.setStart(node, pos);
    range.setEnd(node, pos + needle.length);
    if (range.getClientRects().length) return range;
  }
  return null;
}

/* 用 fixed 定位的浮层框住命中词：不改消息 DOM，不会被富文本重排冲掉。
   一个词可能跨行（或落在可横向滚动的代码块里），所以每个矩形都画一层。 */
function ssFlashTextRange(range) {
  const rects = Array.from(range.getClientRects()).filter((r) => r.height);
  if (!rects.length) return;
  const boxes = rects.map((rect) => {
    const box = document.createElement('div');
    box.className = 'ss-text-flash';
    box.style.left = (rect.left - 3) + 'px';
    box.style.top = (rect.top - 2) + 'px';
    box.style.width = (rect.width + 6) + 'px';
    box.style.height = (rect.height + 4) + 'px';
    document.body.appendChild(box);
    return box;
  });
  setTimeout(() => boxes.forEach((b) => b.remove()), 1800);
}

/* 命中落在可横向滚动的代码块里时，把那个块横向滚到命中处，
   否则文字被 clip 在聊天列外，浮层也跟着跑出视口。 */
function ssScrollSideways(range) {
  const rect = range.getClientRects()[0];
  if (!rect) return;
  let el = range.startContainer.parentElement;
  while (el && el !== document.body) {
    if (el.scrollWidth > el.clientWidth + 4) {
      const er = el.getBoundingClientRect();
      if (rect.left < er.left || rect.right > er.right) {
        el.scrollLeft += (rect.left - er.left) - (el.clientWidth - rect.width) / 2;
      }
      return;
    }
    el = el.parentElement;
  }
}

function bindSessionSearch() {
  const btn = $('#session-search-btn');
  const modal = $('#session-search-modal');
  const input = $('#ss-input');
  if (btn) btn.onclick = () => ssOpen();
  if ($('#ss-close')) $('#ss-close').onclick = () => ssClose();
  if (modal) modal.addEventListener('click', (e) => { if (e.target === modal) ssClose(); });
  if (input) {
    input.addEventListener('input', (e) => {
      clearTimeout(ssState.timer);
      const v = e.target.value;
      ssState.timer = setTimeout(() => ssFetch(v), 180);
    });
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') { e.preventDefault(); ssClose(); }
      else if (e.key === 'Enter' && ssState.items.length) {
        e.preventDefault();
        const first = ssState.items[0];
        const hit = (first.hits || [])[0];
        ssPick(first.id, hit ? hit.index : undefined);
      }
    });
  }
}

/* 把某个会话导出成 Markdown 文件下载到本地 */
async function exportSession(sid) {
  try {
    const r = await api('/api/session/export', { session_id: sid });
    if (!r || !r.ok) throw new Error((r && r.error) || '导出失败');
    const blob = new Blob([r.markdown || ''], { type: 'text/markdown;charset=utf-8' });
    saveBlobUrl(URL.createObjectURL(blob), r.filename || 'conversation.md');
    toast('已导出 ' + (r.filename || ''));
  } catch (e) {
    toast('导出失败：' + (e && e.message ? e.message : e), 'error');
  }
}

/* 最后一条用户气泡上的文档标签（附件解析进度往它上面写；多附件按顺序返回） */
function lastUserDocChips() {
  const users = document.querySelectorAll('#chat .msg.user');
  const lastU = users[users.length - 1];
  return lastU ? Array.prototype.slice.call(lastU.querySelectorAll('.msg-doc')) : [];
}

/* ============================================================
   对话内查找（Ctrl+F）：高亮命中并逐条跳转
   ============================================================ */
const findState = { hits: [], idx: -1, q: '' };

function findClear() {
  const marks = Array.prototype.slice.call(document.querySelectorAll('#chat mark.find-hit'));
  const parents = new Set();
  marks.forEach((m) => {
    if (!m.parentNode) return;
    parents.add(m.parentNode);
    m.parentNode.replaceChild(document.createTextNode(m.textContent), m);   // 还原成纯文本
  });
  parents.forEach((p) => { try { p.normalize(); } catch (e) { /* 忽略 */ } });
  findState.hits = [];
  findState.idx = -1;
}

function findRun(q) {
  findClear();
  findState.q = q || '';
  const box = $('#find-count');
  if (!findState.q) { if (box) box.textContent = ''; return; }
  const root = $('#chat');
  if (!root) return;
  const needle = findState.q.toLowerCase();
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(n) {
      if (!n.nodeValue || n.nodeValue.toLowerCase().indexOf(needle) < 0) return NodeFilter.FILTER_REJECT;
      const p = n.parentElement;
      // 操作条按钮、搜索过程、图片、查找框本身都不该被高亮
      if (!p || p.closest('.msg-actions, .find-bar, .msg-image, .search-steps')) return NodeFilter.FILTER_REJECT;
      return NodeFilter.FILTER_ACCEPT;
    },
  });
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  nodes.forEach((n) => {
    const text = n.nodeValue;
    const low = text.toLowerCase();
    const frag = document.createDocumentFragment();
    let i = 0;
    while (i < text.length) {
      const k = low.indexOf(needle, i);
      if (k < 0) { frag.appendChild(document.createTextNode(text.slice(i))); break; }
      if (k > i) frag.appendChild(document.createTextNode(text.slice(i, k)));
      const mk = document.createElement('mark');
      mk.className = 'find-hit';
      mk.textContent = text.slice(k, k + needle.length);
      frag.appendChild(mk);
      i = k + needle.length;
    }
    if (n.parentNode) n.parentNode.replaceChild(frag, n);
  });
  findState.hits = Array.prototype.slice.call(root.querySelectorAll('mark.find-hit'));
  if (findState.hits.length) findGo(0);
  else if (box) box.textContent = '0/0';
}

function findGo(step) {
  const hits = findState.hits;
  const box = $('#find-count');
  if (!hits.length) { if (box) box.textContent = '0/0'; return; }
  if (findState.idx >= 0 && hits[findState.idx]) hits[findState.idx].classList.remove('cur');
  findState.idx = ((findState.idx + (step || 1)) % hits.length + hits.length) % hits.length;
  const cur = hits[findState.idx];
  cur.classList.add('cur');
  try { cur.scrollIntoView({ block: 'center', behavior: 'smooth' }); } catch (e) { cur.scrollIntoView(); }
  if (box) box.textContent = (findState.idx + 1) + '/' + hits.length;
}

function findClose() {
  const bar = $('#find-bar');
  if (bar) bar.classList.add('hidden');
  findClear();
  findState.q = '';
}

function findOpen() {
  const bar = $('#find-bar');
  if (!bar) return;
  bar.classList.remove('hidden');
  const inp = $('#find-input');
  if (inp) { inp.focus(); inp.select(); }
}

function setupFindBar() {
  const inp = $('#find-input');
  if (!inp) return;
  let timer = null;
  inp.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(() => findRun(inp.value.trim()), 180);    // 防抖：别每敲一下就重排一次 DOM
  });
  inp.addEventListener('keydown', (e) => {
    if (isImeComposing(e)) return;
    if (e.key === 'Enter') { e.preventDefault(); findGo(e.shiftKey ? -1 : 1); }
    else if (e.key === 'Escape') { e.preventDefault(); findClose(); }
  });
  if ($('#find-next')) $('#find-next').onclick = () => findGo(1);
  if ($('#find-prev')) $('#find-prev').onclick = () => findGo(-1);
  if ($('#find-close')) $('#find-close').onclick = () => findClose();
}

function updateStatusLine() {
  const c = state.config || {};
  const p = state.profile || {};
  const tag = (v) => (v && String(v).trim() ? '已配置' : '未配置');
  $('#status-line').innerHTML =
    '聊天 API：' + tag(c.chat_api_key) + ' · 图像 API：' + tag(c.vision_api_key) +
    '<br>AI 记录画像：' + (p.auto_learn_enabled ? '开启' : '关闭');
}

/* ---------------- 上下文容量圆环 ---------------- */
const ctxRemind = { session: null, fired: {} };   // 每会话已触发的提醒档位（10 / 5）
const CTX_RING_R = 9.5;
const CTX_RING_LEN = 2 * Math.PI * CTX_RING_R;
let _ctxRing = null;

/* 懒建圆环 DOM：首次有上下文数据时创建，之后复用同一份节点 */
function ctxRingEnsure() {
  if (_ctxRing) return _ctxRing;
  const dock = document.createElement('div');
  dock.id = 'ctx-ring-dock';
  dock.className = 'ctx-ring-dock hidden';
  const wrap = document.createElement('div');
  wrap.className = 'ctx-ring-wrap';
  const ring = document.createElement('button');
  ring.id = 'btn-ctx-ring';
  ring.type = 'button';
  ring.className = 'ctx-ring';
  ring.title = '压缩历史对话';
  ring.setAttribute('aria-label', '压缩历史对话');
  ring.innerHTML =
    '<svg viewBox="0 0 24 24" aria-hidden="true">' +
      '<circle class="ctx-ring-track" cx="12" cy="12" r="' + CTX_RING_R + '"/>' +
      '<circle class="ctx-ring-arc" cx="12" cy="12" r="' + CTX_RING_R + '"/>' +
    '</svg>';
  const pct = document.createElement('button');
  pct.id = 'btn-ctx-detail';
  pct.type = 'button';
  pct.className = 'ctx-ring-pct';
  pct.title = '查看上下文占用明细 / 恢复原文';
  pct.setAttribute('aria-label', '查看上下文占用明细');
  wrap.appendChild(ring);
  wrap.appendChild(pct);
  dock.appendChild(wrap);
  ring.onclick = () => compressContextManually();
  pct.onclick = () => openCtxDetail();
  const arc = ring.querySelector('.ctx-ring-arc');
  arc.style.strokeDasharray = CTX_RING_LEN;
  arc.style.strokeDashoffset = CTX_RING_LEN;
  _ctxRing = { hasCtx: false, dock, arc, pct };
  return _ctxRing;
}

/* 圆环只挂在最后一条 AI 回复的操作条里（与复制 / 选择文本同一行，靠右）；其余情况收起 */
function placeCtxRing() {
  const r = _ctxRing;
  if (!r || !r.hasCtx) return;
  const chat = $('#chat');
  const rows = chat ? chat.querySelectorAll('.msg') : [];
  const last = rows[rows.length - 1];
  if (!chat || state.uiMode === 'gal' || !last || !last.classList.contains('ai')) {
    r.dock.classList.add('hidden');
    return;
  }
  const bar = last.querySelector('.msg-actions');
  if (!bar) { r.dock.classList.add('hidden'); return; }
  if (r.dock.parentNode !== bar) bar.appendChild(r.dock);
  r.dock.classList.remove('hidden');
}

/* 输入区的上下文预算：圆环只挂在最后一条 AI 回复上，打字时看不到还剩多少。
   这里把「本轮已用 + 当前草稿估算」压成一行小字，常驻在提示行右侧。 */
let _lastCtx = null;
let _ctxMeterBound = false;

function fmtTok(n) {
  n = Math.max(0, Number(n) || 0);
  if (n < 1000) return String(Math.round(n));
  if (n < 1e6) return (n / 1000).toFixed(n < 10000 ? 1 : 0).replace(/\.0$/, '') + 'k';
  return (n / 1e6).toFixed(2).replace(/\.?0+$/, '') + 'M';
}

// 草稿的 token 估算：中文按 ~0.62 token/字，其余按 4 字符/token（和服务端估算同一量级）
function estTokens(s) {
  const t = String(s || '');
  if (!t) return 0;
  let cjk = 0;
  for (let i = 0; i < t.length; i += 1) {
    const c = t.charCodeAt(i);
    if (c >= 0x2e80 && c <= 0x9fff) cjk += 1;
  }
  return Math.round(cjk * 0.62 + (t.length - cjk) / 4);
}

function updateCtxMeter() {
  const el = $('#ctx-meter');
  if (!el) return;
  if (!_ctxMeterBound) {
    _ctxMeterBound = true;
    const inp = $('#input');
    if (inp) inp.addEventListener('input', () => updateCtxMeter());
  }
  if (!_lastCtx) { el.classList.add('hidden'); el.textContent = ''; return; }
  if (_lastCtx.unlimited || !_lastCtx.limit) {
    el.classList.remove('hidden', 'warn', 'danger');
    el.textContent = '上下文不限';
    el.title = '当前窗口上限为 0（不限），进度圆环与按窗口压缩都不会触发';
    return;
  }
  const used = Math.max(0, Number(_lastCtx.used) || 0);
  const draft = estTokens($('#input') ? $('#input').value : '');
  const total = used + draft;
  const pct = Math.min(100, (total / _lastCtx.limit) * 100);
  el.classList.remove('hidden');
  el.classList.toggle('warn', pct >= 70 && pct < 90);
  el.classList.toggle('danger', pct >= 90);
  el.textContent = '上下文 ' + Math.round(pct) + '% · ' + fmtTok(total) + ' / ' + fmtTok(_lastCtx.limit)
    + (draft ? '（草稿 ' + fmtTok(draft) + '）' : '');
  el.title = '已用 ' + total + ' tokens（其中草稿 ' + draft + '），窗口 ' + _lastCtx.limit + ' tokens';
}

function renderContextBar(ctx) {
  syncCompressMarks(ctx);      // 压缩标记跟着 context 一起同步（GAL 内部会跳过）
  _lastCtx = ctx || null;
  updateCtxMeter();
  const r = ctxRingEnsure();
  r.hasCtx = !!(ctx && ctx.limit);
  if (!r.hasCtx) { r.dock.classList.add('hidden'); return; }
  const pct = Math.max(0, Math.min(100, ctx.pct || 0));
  const remainPct = ctx.remain / ctx.limit;
  let level = '';
  if (remainPct < 0.05) level = 'danger';
  else if (remainPct < 0.10) level = 'warn';
  r.dock.classList.toggle('warn', level === 'warn');
  r.dock.classList.toggle('danger', level === 'danger');
  r.arc.style.strokeDashoffset = CTX_RING_LEN * (1 - pct / 100);
  r.pct.textContent = Math.round(pct) + '%';
  r.pct.setAttribute('aria-label', '上下文占用 ' + pct + '%，点击查看明细');
  placeCtxRing();
  remindContext(ctx, remainPct);
}

function remindContext(ctx, remainPct) {
  const sid = state.sessions.current;
  if (ctxRemind.session !== sid) { ctxRemind.session = sid; ctxRemind.fired = {}; }
  if (remainPct >= 0.10) { ctxRemind.fired = {}; return; }   // 回落后重置，允许再次提醒
  if (remainPct < 0.05) {
    if (!ctxRemind.fired['5']) {
      ctxRemind.fired['5'] = true; ctxRemind.fired['10'] = true;
      toast('上下文剩余不足 5%，超出上限时会临时省略较早消息');
    }
  } else if (!ctxRemind.fired['10']) {
    ctxRemind.fired['10'] = true;
    toast('上下文剩余不足 10%，可点回复末尾的圆环压缩历史，或稍后新建会话');
  }
}

async function compressContextManually() {
  // 只拦「本轮真的还在产流」：本地流最近 15 秒还有事件（后端每 5 秒一个 tick）。
  // 不用 state.streaming —— 断线 / 刷新后它可能一直是 true，导致中断的轮次永远压不了。
  const _st = (state.streams || {})[String(state.sessions.current || '')];
  if (_st && Date.now() - (_st.lastEvent || 0) < 15000) {
    toast('正在生成回复，请稍后再试'); return;
  }
  if (!confirm('把较早的对话历史压缩成摘要？\n摘要会替换那部分原文（本地原文仍保留，可在「明细 / 恢复」里撤销）。')) return;
  const btn = $('#btn-ctx-ring');
  if (btn) { btn.disabled = true; btn.classList.add('busy'); }
  compressPending();
  try {
    const r = await api('/api/context/compress', { session_id: state.sessions.current });
    if (r && r.sessions) applySessionsPayload(r.sessions);
    renderSessions();
    if (r && r.context) renderContextBar(r.context);   // 成功时自动把 pending 升级成分隔线
    if (!r || !r.ok) {
      compressPendingClear();
      toast((r && r.message) || '未能压缩', 'error');
    } else if (!(r.context && Array.isArray(r.context.compress_marks))) {
      compressPendingClear();                          // 旧后端没有标记字段：别留下假进行中
    }
    if ($('#ctx-modal') && !$('#ctx-modal').classList.contains('hidden')) openCtxDetail();
  } catch (e) {
    compressPendingClear();
    toastErr('压缩', e);
  } finally {
    if (btn) { btn.disabled = false; btn.classList.remove('busy'); }
  }
}

/* ---------------- 上下文详情（占用明细 / 摘要 / 恢复） ---------------- */
async function openCtxDetail() {
  try {
    const r = await api('/api/context/summary', { session_id: state.sessions.current });
    if (r && r.context) renderContextBar(r.context);
    ctxRenderDetail(r);
    showWithAnim($('#ctx-modal'));
  } catch (e) {
    toastErr('读取压缩详情', e);
  }
}

function closeCtxDetail() {
  hideWithAnim($('#ctx-modal'));
}

function ctxRenderDetail(r) {
  if (!r || !r.ok) { toast((r && r.message) || '读取压缩详情失败', 'error'); return; }
  const c = r.context || {};
  $('#ctx-detail-total').textContent =
    '已用 ' + fmtNum(c.used) + ' / ' + fmtNum(c.limit) + ' · 剩余 ' +
    fmtNum(c.remain) + '（' + (c.pct || 0) + '%）';
  const parts = c.parts || {};
  const rows = [
    ['系统提示', parts.system],
    ['历史摘要', parts.summary],
    ['对话正文', parts.dialog],
    ['附件正文', parts.attachment],
    ['图片', parts.image],
  ];
  const total = Math.max(1, c.used || 0);
  $('#ctx-parts').innerHTML = rows.map(([name, v]) => {
    const n = Number(v || 0);
    const w = Math.min(100, Math.round(n * 100 / total));
    return '<div class="ctx-part"><span class="ctx-part-name">' + name + '</span>' +
      '<span class="ctx-part-bar"><i style="width:' + w + '%"></i></span>' +
      '<span class="ctx-part-val">' + fmtNum(n) + '</span></div>';
  }).join('');
  const meta = [];
  if (r.compressed) {
    meta.push('已压缩 ' + r.compressed + ' 条（剩余 ' + r.remaining + ' / ' + r.total + ' 条原文）');
    if (r.at) meta.push(new Date(r.at * 1000).toLocaleString());
    meta.push('摘要 ' + fmtNum(r.chars) + ' 字');
  } else {
    meta.push('尚未压缩');
  }
  if (r.busy) meta.push('后台压缩中…');
  $('#ctx-detail-meta').textContent = meta.join(' · ');
  const box = $('#ctx-summary-text');
  if (r.summary) {
    box.textContent = r.summary;
    box.classList.remove('hidden');
  } else {
    box.textContent = '';
    box.classList.add('hidden');
  }
  $('#ctx-detail-restore').disabled = !r.compressed;
  ctxRenderInput(r.input);
}

/* token 明细：整回合累计 vs 逐轮调用（输入 / 输出 / 推理都在这看）。和「占用明细」同一个入口，不同页签。 */

/* 输入构成的分类与展示顺序（后端 context.input_breakdown 的 key） */
const CTX_IN_ORDER = ['rules', 'memory', 'other_sys', 'summary', 'user',
  'reply', 'tool_result', 'tools_doc', 'schema'];
const CTX_IN_LABELS = {
  rules: '规则/画像', memory: '记忆/任务', other_sys: '其他系统', summary: '摘要',
  user: '用户消息', reply: '回复历史', tool_result: '工具结果',
  tools_doc: '工具说明', schema: '工具定义'
};

function ctxInParts(r) {
  const p = r && r.in_parts;
  if (!p || typeof p !== 'object') return null;
  let total = 0;
  // 用全部 key 求和（不只已知分类）：后端以后加分类时，百分比也不会算错
  Object.keys(p).forEach((k) => { total += Number(p[k] || 0); });
  if (!(total > 0)) return null;
  return { p: p, total: total };
}

function ctxInBar(p, total, small) {
  const segs = CTX_IN_ORDER.filter((k) => Number(p[k] || 0) > 0).map((k) => {
    const pct = Number(p[k]) * 100 / total;
    return '<span class="ctx-in-seg ctx-in-' + k + '" style="width:'
      + pct.toFixed(2) + '%"></span>';
  }).join('');
  return '<span class="ctx-in-bar' + (small ? ' small' : '') + '">' + segs + '</span>';
}

function ctxInputMix(r) {
  const it = ctxInParts(r);
  if (!it) return '';
  const chips = CTX_IN_ORDER.filter((k) => Number(it.p[k] || 0) > 0).map((k) => {
    const v = Number(it.p[k]);
    const pct = v * 100 / it.total;
    return '<span class="ctx-in-chip"><i class="ctx-in-dot ctx-in-' + k + '"></i>'
      + CTX_IN_LABELS[k] + ' <b>' + fmtNum(v) + '</b>（' + pct.toFixed(1) + '%）</span>';
  }).join('');
  return '<p class="ctx-in-title">第 ' + (r.n || '?') + ' 次调用的输入构成（按 token 估算拆分）</p>'
    + ctxInBar(it.p, it.total, false)
    + '<div class="ctx-in-legend">' + chips + '</div>';
}

function ctxRenderInput(inp) {
  const totalEl = $('#ctx-input-total');
  const metaEl = $('#ctx-input-meta');
  const tableEl = $('#ctx-input-table');
  const mixEl = $('#ctx-input-mix');
  if (!totalEl || !metaEl || !tableEl) return;
  if (!inp || !inp.usage) {
    totalEl.textContent = '还没有带 token 用量的回复';
    metaEl.textContent = '';
    tableEl.innerHTML = '';
    if (mixEl) { mixEl.innerHTML = ''; mixEl.classList.add('hidden'); }
    return;
  }
  const u = inp.usage || {};
  const prompt = Number(u.prompt || 0);
  const cached = Number(u.cached || 0);
  const uncached = Math.max(0, prompt - cached);
  const pct = prompt > 0 ? (cached * 100 / prompt) : 0;
  const rd = inp.rounds_detail || [];
  totalEl.textContent = '总输入 ' + fmtNum(prompt)
    + (rd.length ? '（' + rd.length + ' 次调用相加）' : '')
    + ' · 缓存 ' + fmtNum(cached) + '（' + pct.toFixed(1) + '%）'
    + ' · 未命中 ' + fmtNum(uncached)
    + ' · 总输出 ' + fmtNum(u.completion)
    + ' · 合计 ' + fmtNum(u.total);
  const meta = [];
  if (inp.at) meta.push('回合 ' + clockText(inp.at));
  if (inp.rounds) meta.push('工具轮 ' + inp.rounds);
  if (rd.length) meta.push('模型调用 ' + rd.length + ' 次');
  if (inp.total_ms) meta.push('耗时 ' + (inp.total_ms / 1000).toFixed(1) + ' 秒');
  const lastPrompt = Number(u.last_prompt
    || (rd.length ? rd[rd.length - 1].prompt : 0) || 0);
  if (lastPrompt) meta.push('末轮输入 ' + fmtNum(lastPrompt));
  // 派生量放在这里：用量行保持短，口径细节在明细里看
  if (rd.length > 1) meta.push('平均每轮输入 ' + fmtNum(Math.round(prompt / rd.length)));
  if (Number(u.reasoning || 0) > 0) meta.push('推理输出 ' + fmtNum(u.reasoning));
  if (inp.source === 'usage_log') meta.push('逐轮明细来自 token_usage.jsonl（旧消息）');
  metaEl.textContent = meta.join(' · ');
  if (!rd.length) {
    tableEl.innerHTML = '<p class="hint">这一轮没有保存逐轮明细（旧数据或未启用）。</p>';
    if (mixEl) { mixEl.innerHTML = ''; mixEl.classList.add('hidden'); }
    return;
  }
  // 构成面板取最近一次「有构成数据」的调用：旧回合没记 in_parts 就自动跳过
  const lastMix = rd.slice().reverse().find((r) => ctxInParts(r));
  if (mixEl) {
    mixEl.innerHTML = lastMix ? ctxInputMix(lastMix)
      : '<p class="hint">这一轮的明细没有保存输入构成（旧数据）；下一次生成后即可看到分类。</p>';
    mixEl.classList.remove('hidden');
  }
  const rows = rd.map((r, i) => {
    const last = i === rd.length - 1;
    const state = escapeHtml(r.state || r.finish || '');
    const ttft = (r.ttft_ms === null || r.ttft_ms === undefined)
      ? '' : (r.ttft_ms + ' ms');
    const it = ctxInParts(r);
    let mixCell = '<span class="hint">—</span>';
    if (it) {
      const tip = CTX_IN_ORDER.filter((k) => Number(it.p[k] || 0) > 0)
        .map((k) => CTX_IN_LABELS[k] + ' ' + fmtNum(it.p[k])).join(' · ');
      mixCell = '<span title="' + escapeHtml(tip) + '">'
        + ctxInBar(it.p, it.total, true) + '</span>';
    }
    return '<tr' + (last ? ' class="ctx-last"' : '') + '><td>' + (i + 1)
      + '</td><td>' + (r.n || '') + '</td><td>' + fmtNum(r.prompt)
      + '</td><td>' + fmtNum(r.completion) + '</td><td>' + state
      + '</td><td>' + ttft + '</td><td class="ctx-in-cell">' + mixCell
      + '</td></tr>';
  }).join('');
  tableEl.innerHTML = '<table><thead><tr><th>#</th><th>轮次</th><th>输入</th>'
    + '<th>输出</th><th>状态</th><th>首包</th><th>构成</th></tr></thead><tbody>'
    + rows + '</tbody></table>';
}

async function ctxRestore() {
  if (!confirm('恢复原文？摘要会被清空，较早的对话重新计入上下文，可能立刻再次触发压缩。')) return;
  try {
    const r = await api('/api/context/restore', { session_id: state.sessions.current });
    if (r && r.sessions) applySessionsPayload(r.sessions);
    renderSessions();
    compressClearAll();
    renderMessages();
    if (r && r.context) renderContextBar(r.context);
    toast((r && r.message) || '已恢复原文');
    await openCtxDetail();
  } catch (e) {
    toastErr('恢复', e);
  }
}

let _selectSeq = 0;   // 连点会话时只认最后一次响应，避免旧响应把界面刷回去

async function selectSession(id) {
  // 点会话就是「要回到对话」：插件页是整屏页面，不退出的话会看不到切换结果
  if (state.uiMode === 'plugins' && typeof setUiMode === 'function') setUiMode('solo');
  // 普通对话：切会话**不停止**正在生成的流（后台继续跑）；GAL 舞台只有一份，维持原行为
  if (state.uiMode === 'gal' && state.streaming) await stopStreaming();
  // 换会话时取消「修改历史消息」状态，避免下次回来误把它当成正在编辑
  if (typeof cancelEditTarget === 'function') cancelEditTarget(false);
  const prev = state.sessions.current;
  if (prev && prev !== id) rememberChatScroll();   // 切走前记住上一个会话读到哪
  cacheLooseMessages();                   // 切走前把已加载的消息留在内存里，切回来能秒出
  const seq = ++_selectSeq;
  state.sessions.current = id;
  _lastCtx = null;                       // 换会话先清掉旧预算，等新的 context 到了再显示
  if (typeof updateCtxMeter === 'function') updateCtxMeter();
  delete state.unread[id];
  renderSessions();                       // 先高亮，给出即时反馈（不等网络往返）
  if (typeof renderSendQueue === 'function') renderSendQueue();   // 队列按会话切换显示
  // 回访过的会话：先用缓存渲染，网络返回后再校准，避免每次都干等请求
  const cached = state.msgCache && state.msgCache[id];
  const target = (state.sessions.sessions || []).find((x) => x.id === id);
  if (cached && target && Array.isArray(cached.messages)) {
    target.messages = cached.messages;
    target.msg_loaded_start = Number(cached.msg_loaded_start) || 0;
    target.msg_total = cached.messages.length;
    renderMessages();
    if (typeof refreshStreamingUI === 'function') refreshStreamingUI();
    restoreChatScroll(id);              // 缓存快照：先回到上次读到的地方
  }
  try {
    // 服务端返回的是精简数据：新选中的会话只带最近 80 条（msg_total / msg_start）
    const r = await api('/api/session/select', { id });
    if (seq !== _selectSeq) return;       // 更晚的一次点击已经接管
    if (r && r.sessions) applySessionsPayload(r.sessions, { keepCurrent: false });
    renderSessions();
    renderMessages();
    // 这个会话可能还在后台生成：把它的 live DOM 重新挂到末尾
    if (typeof attachStreamHost === 'function') attachStreamHost(id);
    if (typeof refreshStreamingUI === 'function') refreshStreamingUI();
    restoreChatScroll(id);              // 网络数据落屏后按记录的阅读位置还原
    resetIdeForSession();
    if (state.uiMode === 'gal' && typeof galRenderFromSession === 'function') galRenderFromSession();
    if (r) renderContextBar(r.context);
  } catch (e) {
    if (seq !== _selectSeq) return;       // 已被后续点击取代：不要还原成旧会话
    state.sessions.current = prev;        // 失败 → 还原高亮，别让界面和实际内容对不上
    renderSessions();
    if (typeof renderSendQueue === 'function') renderSendQueue();
    toastErr('切换会话', e);
  }
}

async function deleteSession(id) {
  const s = state.sessions.sessions.find((x) => x.id === id);
  const label = (s && s.title) || '这个会话';
  if (state.streams && state.streams[id]) {
    // 生成中的会话不能删：后端收尾时会把这条会话重新写回文件（表现为删了又出现）
    toast('这个会话正在生成，先点「停止」再删除');
    return;
  }
  if (!confirm('删除会话「' + label + '」？\n它的全部对话与存档都会一并删除，且不可恢复。')) return;
  try {
    const r = await api('/api/session/delete', { id });
    applySessionsPayload(r.sessions, { keepCurrent: false });
    if (typeof dropQueuedForSession === 'function') dropQueuedForSession(id);
    renderSessions();
    renderMessages();
    if (typeof renderSendQueue === 'function') renderSendQueue();
    resetIdeForSession();
    if (r) renderContextBar(r.context);
    toast('已删除会话：' + label);
  } catch (e) { toastErr('删除', e); }
}

async function newSession() {
  // 从插件页新建对话 → 回到 Solo（否则新会话建好了却还停在插件页）
  if (state.uiMode === 'plugins' && typeof setUiMode === 'function') await setUiMode('solo');
  // 新建会话不打断其它会话的生成：多线程工作就是靠这个
  const inGal = state.uiMode === 'gal';
  const mem = (state.config && state.config.gal_memory) || {};
  try {
    const r = await api('/api/session/new', inGal ? {
      gal_scope: 'gal', title: 'GAL 剧场',
      gal_use_profile: !!mem.profile,
      gal_use_kb: !!mem.kb,
      gal_use_rules: !!mem.rules,
    } : {});
    applySessionsPayload(r.sessions, { keepCurrent: false });
    renderSessions();
    renderMessages();
    if (typeof renderSendQueue === 'function') renderSendQueue();   // 新会话队列为空
    resetIdeForSession();
    if (inGal && typeof galRenderFromSession === 'function') galRenderFromSession();
    if (r) renderContextBar(r.context);
    $('#input').focus();
  } catch (e) { toastErr('新建会话', e); }
}

async function renameSession(id) {
  const s = state.sessions.sessions.find((x) => x.id === id);
  // Electron 不支持 window.prompt，统一走应用内输入框
  const name = await appPrompt('重命名会话', (s && s.title) || '');
  if (!name || !name.trim()) return;
  try {
    const r = await api('/api/session/rename', { id, title: name.trim() });
    if (!r || !r.ok) throw new Error((r && r.error) || '重命名失败');
    applySessionsPayload(r.sessions);
    renderSessions();
  } catch (e) { toastErr('重命名', e); }
}

async function toggleSessionPin(id) {
  const s = state.sessions.sessions.find((x) => x.id === id);
  if (!s) return;
  const next = !s.pinned;
  try {
    const r = await api('/api/session/pin', { id, pinned: next });
    if (!r || !r.ok) throw new Error((r && r.error) || '置顶失败');
    applySessionsPayload(r.sessions);
    renderSessions();
    toast(next ? '已置顶' : '已取消置顶');
  } catch (e) { toastErr('置顶', e); }
}

/* ============================================================
   消息渲染
   ============================================================ */
function hideEmpty() {
  const e = $('#empty');
  if (e) e.remove();
}

function showEmptyIfNeeded() {
  const chat = $('#chat');
  if (chat.querySelector('.msg')) {
    const e = $('#empty');
    if (e) e.remove();
  } else if (!$('#empty')) {
    location.reload();
  }
}

// 距离底部多少像素算「贴着底」。跟随用严格阈值，按钮提示用宽松阈值。
function isAtBottom(px) {
  const c = $('#chat');
  if (!c) return true;
  return c.scrollHeight - c.scrollTop - c.clientHeight < (px == null ? 24 : px);
}

// 「回到底部」按钮的显隐阈值：滚得比较远才提示
function nearBottom() {
  return isAtBottom(120);
}

// 是否处于「跟随最新输出」状态：用户主动往上翻就锁定，直到回到底部 / 点回到底部 / 发新消息
let _followBottom = true;

function setFollowBottom(on) {
  _followBottom = !!on;
}

// 「还算贴着底」的容差：跟随判定专用。太小的话，手抖滚一格或内容长两行就会被判成「用户离开了」。
const FOLLOW_SLOP = 40;

// 程序滚动计数：给 scrollTop 赋值也会触发 scroll 事件，而事件是**异步派发**的。
// 不区分「自己滚的」和「用户滚的」，程序刚滚到底、事件回来时内容又长高了，
// 就会把跟随状态关掉（流式越快越容易中）。
let _programmaticScroll = 0;

function _scrollToBottomNow(box) {
  if (!box) return;
  _programmaticScroll += 1;
  box.scrollTop = box.scrollHeight;
  requestAnimationFrame(() => {
    _programmaticScroll = Math.max(0, _programmaticScroll - 1);
  });
}

// 流式期间必须「即时定位」：每帧都调用，若用平滑滚动会互相打断而产生抖动
let _scrollRaf = 0;
function scrollToBottom(force) {
  const c = $('#chat');
  if (!c) return;
  if (force) {
    if (_scrollRaf) { cancelAnimationFrame(_scrollRaf); _scrollRaf = 0; }
    _followBottom = true;
    _scrollToBottomNow(c);
    return;
  }
  // 用户在翻看历史：流式增量一帧都不许把窗口拽回去
  if (!_followBottom) return;
  // 每个 delta 都会调到这里；合并到下一帧，避免每个 token 都强制一次 layout
  if (_scrollRaf) return;
  _scrollRaf = requestAnimationFrame(() => {
    _scrollRaf = 0;
    const box = $('#chat');
    if (!box || !_followBottom) return;
    // 跟随只表示「用户没往上翻」，不等于「当前位置还贴着底」。
    // 以前这里再判一次 isAtBottom，于是「这一帧内容长高了」被误判成
    // 「用户离开了底部」—— 一次 delta 超过阈值就永久断跟随。
    _scrollToBottomNow(box);
  });
}

// 流式跟随：不再「每帧瞬间贴底」，而是按距离比例缓动追上去（类似 Codex / Ollama 的跟手感）。
// 距离越远追得越快；用户一上滚（_followBottom=false）立刻停，不和用户抢滚动条。
let _followRaf = 0;
function followScroll() {
  const c = $('#chat');
  if (!c || !_followBottom) return;
  if (_followRaf) return;
  _followRaf = requestAnimationFrame(function tick() {
    const box = $('#chat');
    if (!box || !_followBottom) { _followRaf = 0; return; }
    const target = box.scrollHeight - box.clientHeight;
    const dist = target - box.scrollTop;
    if (dist <= 0.6) { _followRaf = 0; return; }
    _programmaticScroll += 1;
    box.scrollTop = box.scrollTop + Math.max(1, dist * 0.35);
    requestAnimationFrame(() => { _programmaticScroll = Math.max(0, _programmaticScroll - 1); });
    _followRaf = requestAnimationFrame(tick);
  });
}

// 过程步骤跟随：新步骤出现时只把它带进视野，不强行拉到整条消息底部——
// 答案区在过程下方，贴底会把刚出现的步骤顶出屏幕。用户上翻（_followBottom=false）
// 立即暂停；回到底部 / 点「回到底部」后恢复。设置里可用 ui_process_follow 关闭。
function processFollowEnabled() {
  return !(state.config && state.config.ui_process_follow === false);
}

let _procFollowRaf = 0;
// 每个过程折叠区自己的「是否跟随」状态，和主窗口的 _followBottom 分开。
// 用户在过程区上翻只暂停过程跟随，不会把答案跟随一起关掉。
const _procFollow = new WeakMap();
// 程序滚动内层时，scroll 事件是异步派发的；用到期时间忽略自己滚出来的事件。
const _procScrollUntil = new WeakMap();

function processNearBottom(body) {
  if (!body) return true;
  return body.scrollHeight - body.scrollTop - body.clientHeight < 24;
}

function procSetScroll(body, top) {
  if (!body) return;
  _procScrollUntil.set(body, Date.now() + 160);
  body.scrollTop = top;
}

// 主窗口只负责「折叠区自身向下长高时别被顶出视口」：底边超出就补最小差值。
// 不再要求把**整个**折叠区塞进视口——那会在答案区同时流式时把窗口往上拽，
// 和 followScroll 的贴底互相打架（表现为窗口上下抽搐）；用户在读下方的答案时，
// 也不该因为过程区里的一次更新被拽回上方。
function ensureFoldVisible(fold, box) {
  if (!fold || !box) return;
  const fr = fold.getBoundingClientRect();
  const br = box.getBoundingClientRect();
  const pad = 24;
  if (fr.bottom <= br.bottom - pad) return;      // 没超出底部（被答案推上去的不要管）
  const delta = fr.bottom - (br.bottom - pad);
  _programmaticScroll += 1;
  box.scrollTop += delta;
  requestAnimationFrame(() => { _programmaticScroll = Math.max(0, _programmaticScroll - 1); });
}

function followProcessStep(el) {
  if (!el) { scrollToBottom(); return; }
  if (!processFollowEnabled() || !_followBottom) {
    scrollToBottom();          // 关闭跟随 / 已暂停：保持原来的贴底行为（暂停时它是空操作）
    return;
  }
  if (_procFollowRaf) return;
  _procFollowRaf = requestAnimationFrame(() => {
    _procFollowRaf = 0;
    const box = $('#chat');
    if (!box || !_followBottom || !el.isConnected) return;
    const fold = el.closest('.process-fold');
    if (fold && fold.classList.contains('collapsed')) return;   // 收起时只看统计，不滚动
    const body = fold ? fold.querySelector('.process-fold-body') : null;
    if (body) {
      // 1) 先滚过程自己的滚动条；用户在内层上翻过就暂停，回底自动恢复。
      if (_procFollow.get(body) !== false) {
        const br = body.getBoundingClientRect();
        const er = el.getBoundingClientRect();
        const pad = 16;
        let delta = 0;
        if (er.bottom > br.bottom - pad) delta = er.bottom - (br.bottom - pad);
        else if (er.top < br.top + pad) delta = er.top - (br.top + pad);
        if (delta) procSetScroll(body, body.scrollTop + delta);
      }
      // 2) 折叠区自身长高、底边超出视口时，主窗口才补一次最小滚动。
      ensureFoldVisible(fold, box);
      return;
    }
    // 没有过程折叠（例如探索小队卡片）：只把超出视口底部的部分带进来，不贴底，
    // 也不往上拽——上方的旧卡片更新时把窗口拉回去，同样会跟答案跟随打架。
    const br = box.getBoundingClientRect();
    const er = el.getBoundingClientRect();
    const pad = 24;
    if (er.bottom <= br.bottom - pad) return;
    _programmaticScroll += 1;
    box.scrollTop += er.bottom - (br.bottom - pad);
    requestAnimationFrame(() => { _programmaticScroll = Math.max(0, _programmaticScroll - 1); });
  });
}

// 内层滚动：用户上翻 -> 暂停过程跟随；回底 -> 恢复。
function watchProcessScroll() {
  const box = $('#chat');
  if (!box || box._procScrollBound) return;
  box._procScrollBound = true;
  box.addEventListener('scroll', (e) => {
    const body = e.target;
    if (!body || !body.classList || !body.classList.contains('process-fold-body')) return;
    if (Date.now() < (_procScrollUntil.get(body) || 0)) return;   // 忽略程序滚动
    _procFollow.set(body, processNearBottom(body));
  }, true);
}

// 当前过程流里最该被看到的一步：优先正在跑的步骤，其次最后一个思考 / 工具组。
function lastProcessAnchor(fold) {
  const box = $('#chat');
  if (!box) return null;
  if (!fold) {
    const folds = box.querySelectorAll('.process-fold:not(.collapsed)');
    fold = folds.length ? folds[folds.length - 1] : null;
  }
  if (!fold || fold.classList.contains('collapsed')) return null;
  const cur = fold.querySelectorAll('.tool-card.is-current');
  if (cur.length) return cur[cur.length - 1];
  const rows = fold.querySelectorAll('.chat-flow > *');
  return rows.length ? rows[rows.length - 1] : null;
}

// 探索小队卡片不在过程折叠里，单独给它一个锚点，避免它的文字更新触发主窗口贴底。
function lastSubagentAnchor() {
  const box = $('#chat');
  if (!box) return null;
  const cards = box.querySelectorAll('.subagent-card');
  const card = cards.length ? cards[cards.length - 1] : null;
  if (!card) return null;
  const rows = card.querySelectorAll('.subagent-row');
  return rows.length ? rows[rows.length - 1] : card;
}

// 工具组展开 / 收起、字体加载这类「高度变了但没有 DOM 增删」的情况，
// MutationObserver 看不到；用 ResizeObserver 补一次过程跟随。
let _procResizeObs = null;
function ensureProcResizeObserver() {
  if (_procResizeObs || !window.ResizeObserver) return null;
  _procResizeObs = new ResizeObserver((entries) => {
    if (!_followBottom) return;
    for (const e of entries) {
      const fold = e.target;
      if (!fold || !fold.isConnected || fold.classList.contains('collapsed')) continue;
      if (fold._foldAnim) continue;          // 折叠动画期间高度每帧都在变，别跟着滚
      const anchor = lastProcessAnchor(fold);
      if (anchor) followProcessStep(anchor);
    }
  });
  return _procResizeObs;
}

function observeProcessFold(fold) {
  if (!fold || !fold.classList || !fold.classList.contains('process-fold')) return;
  const obs = ensureProcResizeObserver();
  if (obs) obs.observe(fold);
}

// 内容高度突变（图片加载 / 代码高亮 / 工具卡展开 / 字体）不会产生 delta，
// 光靠 scrollToBottom 会停在半路。跟随状态下补一次贴底。
// 过程区例外：那里由 followProcessStep 精确跟随当前步骤，粗暴贴底会把步骤顶出屏幕。
let _chatGrowthObserver = null;
function watchChatGrowth() {
  const box = $('#chat');
  if (!box) return;
  watchProcessScroll();
  if (_chatGrowthObserver || !window.MutationObserver) return;
  const pinImages = (node) => {
    if (!node || node.nodeType !== 1) return;
    const imgs = node.tagName === 'IMG' ? [node] : Array.from(node.querySelectorAll('img'));
    imgs.forEach((img) => {
      if (img.complete) return;
      img.addEventListener('load', () => {
        if (!_followBottom) return;
        // 过程区 / 探索小队里的图：只把这张图带进视野，不要把主窗口贴底。
        if (img.closest && (img.closest('.process-fold') || img.closest('.subagent-card'))) {
          followProcessStep(img);
        } else {
          scrollToBottom();
        }
      }, { once: true });
    });
  };
  const processFoldOf = (node) => {
    const el = node && (node.nodeType === 1 ? node : node.parentElement);
    return (el && el.closest) ? el.closest('.process-fold') : null;
  };
  const inSubagent = (node) => {
    const el = node && (node.nodeType === 1 ? node : node.parentElement);
    return !!(el && el.closest && el.closest('.subagent-card'));
  };
  const obs = new MutationObserver((records) => {
    if (!_followBottom) return;
    let procFold = null;
    let sub = false;
    for (const r of records) {
      const added = Array.from(r.addedNodes || []);
      added.forEach(pinImages);
      let fold = processFoldOf(r.target);
      if (!fold) {
        // 整个过程折叠区刚被插入时，mutation 的 target 是父节点；看 addedNodes。
        for (const n of added) {
          if (n.nodeType === 1 && n.classList && n.classList.contains('process-fold')) {
            fold = n;
            observeProcessFold(n);
            break;
          }
        }
      }
      if (fold) {
        if (processFollowEnabled()) procFold = fold;
      } else if (inSubagent(r.target)
                 || added.some((n) => n.nodeType === 1 && n.classList
                   && n.classList.contains('subagent-card'))) {
        sub = true;
      }
      for (const n of Array.from(r.removedNodes || [])) {
        if (n.nodeType === 1 && n.classList && n.classList.contains('process-fold')
            && _procResizeObs) {
          _procResizeObs.unobserve(n);
        }
      }
    }
    if (procFold || sub) {
      const anchor = procFold ? lastProcessAnchor(procFold) : lastSubagentAnchor();
      if (anchor) followProcessStep(anchor);
      return;
    }
    followScroll();
  });
  obs.observe(box, { childList: true, subtree: true, characterData: true });
  box.querySelectorAll('.process-fold').forEach(observeProcessFold);
  _chatGrowthObserver = obs;
}

// 会话阅读位置：切走时记住每个会话滚到哪，切回来还原（不再一律跳到底部）。
// 只放内存：刷新页面后回到默认的「贴底」，和以前一致。
const _chatScrollMem = new Map();
function rememberChatScroll() {
  const c = $('#chat');
  const sid = state.sessions.current;
  if (!c || !sid) return;
  _chatScrollMem.set(String(sid), c.scrollTop);
}

function restoreChatScroll(sid) {
  const c = $('#chat');
  if (!c) return;
  const y = _chatScrollMem.get(String(sid || ''));
  if (typeof y === 'number') {
    c.scrollTop = y;
    _followBottom = isAtBottom();      // 停在历史里就不要再被流式输出拽走
  } else {
    _followBottom = true;
    _scrollToBottomNow(c);
  }
}

// 「回到底部」按钮的定位：按输入区实际高度动态设置 bottom，
// 避免输入区变高（进度条/附件条/提示换行）时按钮压到输入框上
function positionToBottomBtn() {
  const btn = $('#btn-bottom');
  const composer = document.querySelector('.composer');
  if (!btn || !composer) return;
  btn.style.bottom = (composer.getBoundingClientRect().height + 14) + 'px';
}

// 只有用户主动点「回到底部」时才用平滑滚动
let _smoothScrollTimer = null;
// 平滑滚动会持续几百毫秒，期间每个 scroll 事件都停在「中途」，
// 不豁免的话会被判成「用户离开了底部」，刚点完按钮跟随又关了。
let _smoothScrollUntil = 0;
function scrollToBottomSmooth() {
  const c = $('#chat');
  _followBottom = true;
  c.classList.add('smooth');
  _smoothScrollUntil = Date.now() + 500;
  _scrollToBottomNow(c);
  clearTimeout(_smoothScrollTimer);
  _smoothScrollTimer = setTimeout(() => c.classList.remove('smooth'), 450);
}

function buildActions(row, getText, index, role) {
  const bar = document.createElement('div');
  bar.className = 'msg-actions';
  // 按钮组单独一层：只有它在悬停时淡入，圆环（上下文占用）保持常显、靠右
  const list = document.createElement('div');
  list.className = 'msg-act-list';
  // 图标按钮：文字放到 data-tip，悬停由 CSS 气泡显示（无原生 title 的延迟）
  const mk = (ico, label, fn) => {
    const b = document.createElement('button');
    b.className = 'msg-act tip';
    b.dataset.tip = label;
    b.setAttribute('aria-label', label);
    b.innerHTML = icon(ico);
    b.onclick = fn;
    list.appendChild(b);
    return b;
  };
  mk('copy', '复制', () => { navigator.clipboard.writeText(getText()).then(() => toast('已复制', 'success')); });
  if (role === 'user') {
    mk('history', '撤回', () => withdrawUserMessage(index));
    mk('pencil', '修改', () => editUserMessage(index));
    // 提问标记：命名后出现在 Solo 右侧悬浮按钮，便于反复回看关键提问
    const pin = mk('pin', '标记提问', () => qmarkEdit(index));
    pin.dataset.qmark = '1';
    if (typeof qmarkOf === 'function' && qmarkOf(index)) pin.classList.add('act-on');
    mk('trash', '删除', () => deleteMessage(index)).classList.add('act-danger');
  } else {
    row._selBtn = mk('select', '选择文本', () => toggleRawView(row));
    // mousedown 阻止默认：点按钮会先清空选区，那样就拿不到用户划选的文字了
    mk('chat', '添加到对话', () => addSelectionToChat(row))
      .addEventListener('mousedown', (e) => e.preventDefault());
    if (typeof index === 'number') mk('branch', '创建分支', () => forkFromMessage(index));
    if (typeof index === 'number') mk('refresh', '重新发言', () => regenerateReply(index));
    if (typeof index === 'number') mk('external', '分享', () => shareMessage(index));
    // 回退到此：删掉这条及其后的消息，并把其中的文件改动一起还原
    if (typeof index === 'number') mk('reset', '回退到此（含文件改动）', () => rollbackMessage(index));
    // 「继续输出」：仅当该条是最后一条、被中断过、且非 GAL 模式时提供
    const cs = curSession();
    const cms = (cs && typeof index === 'number') ? (cs.messages || [])[index] : null;
    const isLastMsg = cs && typeof index === 'number' &&
                      index === (cs.messages || []).length - 1;
    const _remoteRunning = !!(cs && state.streamingSids && state.streamingSids[String(cs.id)]);
    // 刷新 / 重开窗口时，最后一条可能只是「任务仍在运行」的进度快照。
    // 这时给一个「回到流式输出」按钮：把 attach 流接回来，实时重建输出。
    if (isLastMsg && cms && cms._progress && _remoteRunning) {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'msg-run-chip msg-run-resume';
      chip.textContent = '回到流式输出';
      chip.title = '任务仍在后台运行：点击把实时输出接回这个会话';
      chip.onclick = () => resumeLiveOutput(cs.id);
      bar.appendChild(chip);
    }
    // 步骤时间线：步级撤销 / 重做（整轮回滚之外更细的一层）
    if (isLastMsg && state.uiMode !== 'gal') mk('list', '步骤', () => openStepsPanel());
    // 「回滚本轮」：只在该条是本轮最后一条、且本轮确实写过文件时出现
    if (isLastMsg && row._wroteTurn) mk('reset', '回滚本轮改动', () => turnUndo());
    // 有 end_reason 时「继续输出」已经在中断提示条里，操作条不再重复放一个
    if (isLastMsg && cms && cms.interrupted && !cms.end_reason
        && !_remoteRunning && state.uiMode !== 'gal') {
      mk('play', '继续输出', () => continueReply(index));
    }
    mk('trash', '删除', () => deleteMessage(index)).classList.add('act-danger');
  }
  bar.appendChild(list);
  return bar;
}

/* 写操作步骤：步级撤销 / 重做。数据来自 agent.list_steps（跨进程持久化）。 */
async function openStepsPanel() {
  const sid = state.sessions.current;
  if (!sid) return;
  const old = document.querySelector('.steps-panel');
  if (old) old.remove();
  let steps = [];
  try {
    const res = await fetch('/api/agent/steps', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Auth-Token': TOKEN },
      body: JSON.stringify({ session_id: sid }),
    });
    const data = await res.json();
    steps = (data && data.steps) || [];
  } catch (e) {
    toast('读取步骤失败：' + e.message, 'error');
    return;
  }

  const panel = document.createElement('div');
  panel.className = 'steps-panel';
  const head = document.createElement('div');
  head.className = 'steps-head';
  head.innerHTML = '<span>写操作步骤（' + steps.length + '）</span>';
  const close = document.createElement('button');
  close.type = 'button';
  close.className = 'steps-close';
  close.textContent = '×';
  close.onclick = () => panel.remove();
  head.appendChild(close);
  panel.appendChild(head);

  const body = document.createElement('div');
  body.className = 'steps-body';
  if (!steps.length) {
    body.innerHTML = '<div class="steps-empty">本次会话还没有可撤销的写操作。</div>';
  }
  steps.forEach((s, i) => {
    const row = document.createElement('div');
    row.className = 'steps-row' + (s.undone ? ' undone' : '');
    const info = document.createElement('div');
    info.className = 'steps-info';
    const short = (s.files || []).map((p) => String(p).split(/[\\/]/).slice(-2).join('/'));
    info.innerHTML = '<b>' + (i + 1) + '.</b> ' + escapeHtml(s.label || '写操作')
      + '<div class="steps-files">'
      + escapeHtml(short.slice(0, 4).join('、') || '（无文件）')
      + (short.length > 4 ? ' …' : '') + '</div>';
    row.appendChild(info);
    const act = document.createElement('button');
    act.type = 'button';
    act.className = 'steps-act';
    act.textContent = s.undone ? '重做' : '撤销';
    act.onclick = async () => {
      act.disabled = true;
      try {
        const r = await fetch(s.undone ? '/api/agent/redo' : '/api/agent/undo', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-Auth-Token': TOKEN },
          body: JSON.stringify({ token: s.token }),
        });
        const d = await r.json();
        if (!d || !d.ok) toast('操作失败', 'error');
        else toast(s.undone ? '已重做' : '已撤销', 'success');
      } catch (e) {
        toast('请求失败：' + e.message, 'error');
      }
      openStepsPanel();
    };
    row.appendChild(act);
    // 「回到这里」：把这一步和它之后的写操作一起撤销（时间线回滚）
    if (!s.undone) {
      const back = document.createElement('button');
      back.type = 'button';
      back.className = 'steps-act';
      back.title = '撤销这一步以及之后的所有写操作';
      back.textContent = '回到这里';
      back.onclick = async () => {
        back.disabled = true;
        // 先列出「这一步及之后」会撤销哪些文件，再让用户确认
        const todo = steps.filter((x) => (x.ts || 0) >= (s.ts || 0) && !x.undone);
        const pv = await fetchRollbackPreview({
          session_id: sid, tokens: todo.map((x) => x.token),
        });
        if (!(await rollbackConfirm({
          title: '确定要回到第 ' + (i + 1) + ' 步吗？',
          sub: '这一步和之后的 ' + todo.length + ' 次写操作会被撤销。',
          files: pv ? pv.files : [],
          empty: '这一步之后没有可撤销的文件改动。',
        }))) { back.disabled = false; return; }
        try {
          const r = await fetch('/api/agent/checkpoint', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Auth-Token': TOKEN },
            body: JSON.stringify({ session_id: sid, token: s.token }),
          });
          const d = await r.json();
          if (!d || !d.ok) toast((d && d.error) || '回滚失败', 'error');
          else toast('已回到第 ' + (i + 1) + ' 步（撤销 ' + (d.done || 0) + ' 次写操作）', 'success');
        } catch (e) {
          toast('请求失败：' + e.message, 'error');
        }
        // 磁盘文件已经回滚，编辑器里打开的内容要跟着重读
        if (typeof ideReloadAll === 'function') ideReloadAll();
        openStepsPanel();
      };
      row.appendChild(back);
    }
    body.appendChild(row);
  });
  panel.appendChild(body);
  document.body.appendChild(panel);
}

/* 「添加到对话」：优先取该条消息里划选的文字，没有选区就用整条回复 */
function addSelectionToChat(row) {
  const box = row._content;
  let text = '';
  const sel = window.getSelection();
  if (sel && sel.rangeCount && !sel.isCollapsed && box) {
    const rng = sel.getRangeAt(0);
    // 选区必须落在这一条消息的内容里，避免把别处的选中误引进来
    if (box.contains(rng.commonAncestorContainer)) text = String(sel.toString());
  }
  if (!text.trim()) text = String(row._raw || (box && box.innerText) || '');
  if (!addQuote(text)) { toast('这条回复没有可引用的文字', 'error'); return; }
  toast('已添加到对话');
}

// 记忆写入标记：AI 本轮改了哪些记忆 + 可撤销
function buildMemoryChip(mem, index) {
  const box = document.createElement('div');
  box.className = 'mem-chip' + (mem.undone ? ' undone' : '');
  const problems = mem.problems || [];
  const warnOnly = !mem.applied && problems.length;   // 一条都没生效：只提示，不给撤销
  if (warnOnly) box.classList.add('warn');
  const ico = document.createElement('span');
  ico.className = 'mem-icon';
  ico.innerHTML = warnOnly ? icon('alert') : icon('brain');
  const text = document.createElement('span');
  text.className = 'mem-text';
  if (warnOnly) {
    text.textContent = '记忆未生效：' + problems.join('、');
  } else {
    const scopes = (mem.scopes || []).join('、');
    text.textContent = '已更新记忆：' + (mem.summary || '') + (scopes ? '（' + scopes + '）' : '') +
      (problems.length ? '；未生效：' + problems.join('、') : '');
  }
  box.appendChild(ico);
  box.appendChild(text);
  if (mem.undone) {
    const tag = document.createElement('span');
    tag.className = 'mem-undone';
    tag.textContent = '已撤销';
    box.appendChild(tag);
    return box;
  }
  // 本轮有节点/连线变更 → 直接跳到图谱看新增/修改内容（不要求用户自己找）
  const changed = mem.changed || {};
  const hasChanges = Object.keys(changed).some((k) => {
    const c = changed[k] || {};
    return (c.node_add || []).length || (c.node_update || []).length
      || (c.edge_add || []).length;
  });
  if (hasChanges && typeof kbOpenChanges === 'function') {
    const jump = document.createElement('button');
    jump.className = 'mem-jump';
    jump.textContent = '查看更新';
    jump.title = '打开知识图谱，高亮本轮新增 / 修改的节点与连线';
    jump.onclick = () => kbOpenChanges(changed);
    box.appendChild(jump);
  }
  if (!mem.undo) return box;        // 没有实际变更时不给撤销按钮，避免死按钮
  const btn = document.createElement('button');
  btn.className = 'mem-undo';
  btn.textContent = '撤销';
  btn.onclick = () => undoMemory(mem.undo, index, btn);
  box.appendChild(btn);
  return box;
}

async function undoMemory(token, index, btn) {
  if (state.streaming || !token) return;
  if (btn) btn.disabled = true;
  try {
    const r = await api('/api/memory/undo', {
      token: token, session_id: state.sessions.current, index: index,
    });
    if (!r || !r.ok) { toast('撤销失败'); if (btn) btn.disabled = false; return; }
    if (r.sessions) applySessionsPayload(r.sessions);
    renderSessions();
    renderMessages();
    if (r.context) renderContextBar(r.context);
    toast('已撤销这次记忆更新');
    refreshKbIfOpen();
  } catch (e) {
    toast('撤销失败：' + e);
    if (btn) btn.disabled = false;
  }
}

// 版本预览状态：点箭头只改显示（不请求接口、不改数据），确认后才真正切换
let verPreview = null;   // { sid, index, vidx }

// 聊天图片附件：按需拉取（带鉴权）→ 缓存 Blob URL，供消息气泡预览
const attachBlobCache = new Map();
function attachBlobUrl(sid, name) {
  const key = sid + '/' + name;
  if (attachBlobCache.has(key)) return Promise.resolve(attachBlobCache.get(key));
  return fetch('/api/attach/get', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Auth-Token': TOKEN },
    body: JSON.stringify({ session_id: sid, name: name }),
  }).then((res) => {
    if (!res.ok) throw new Error('HTTP ' + res.status);
    return res.blob();
  }).then((blob) => {
    const url = URL.createObjectURL(blob);
    attachBlobCache.set(key, url);
    return url;
  });
}

// 点击消息图片 → 全屏查看器（与图寻的题面放大同一套手感）：
// 滚轮以指针为锚点缩放、按住拖动平移、双击切换「适应窗口 / 原始尺寸」、Esc 关闭。
const IMG_ZOOM_MIN = 0.1;
const IMG_ZOOM_MAX = 8;
let imgZoom = null;

function imgZoomBuild() {
  const wrap = document.createElement('div');
  wrap.className = 'img-zoom hidden';
  wrap.innerHTML = [
    '<div class="img-zoom-bar">',
    '  <span class="img-zoom-pct">100%</span>',
    '  <button type="button" data-z="out" title="缩小">−</button>',
    '  <button type="button" data-z="in" title="放大">＋</button>',
    '  <button type="button" data-z="fit" title="缩放到刚好放得下整张图">适应窗口</button>',
    '  <button type="button" data-z="one" title="一个图片像素对应一个屏幕像素">原始尺寸</button>',
    '  <span class="img-zoom-spacer"></span>',
    '  <button type="button" data-z="close" title="关闭（Esc）">✕ 关闭</button>',
    '</div>',
    '<div class="img-zoom-stage"><img alt="图片预览"></div>',
    '<div class="img-zoom-hint">滚轮缩放 · 按住拖动平移 · 双击切换「适应窗口 / 原始尺寸」 · 图片外轻点或 Esc 关闭</div>',
  ].join('');
  document.body.appendChild(wrap);

  const stage = wrap.querySelector('.img-zoom-stage');
  const z = imgZoom = {
    overlay: wrap, img: wrap.querySelector('img'), stage: stage,
    pct: wrap.querySelector('.img-zoom-pct'),
    open: false, scale: 1, x: 0, y: 0, drag: null, onKey: null,
  };

  wrap.querySelector('.img-zoom-bar').addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-z]');
    if (!btn) return;
    const k = btn.dataset.z;
    if (k === 'close') imgZoomClose();
    else if (k === 'fit') imgZoomFit();
    else if (k === 'one') imgZoomTo(1, 0, 0);
    else imgZoomTo(z.scale * (k === 'in' ? 1.25 : 1 / 1.25), 0, 0);
  });
  stage.addEventListener('wheel', imgZoomWheel, { passive: false });
  stage.addEventListener('pointerdown', imgZoomDown);
  stage.addEventListener('pointermove', imgZoomMove);
  stage.addEventListener('pointerup', imgZoomUp);
  stage.addEventListener('pointercancel', imgZoomCancel);
  stage.addEventListener('dblclick', () => {
    if (Math.abs(z.scale - 1) < 0.01) imgZoomFit(); else imgZoomTo(1, 0, 0);
  });
  z.onKey = (e) => {
    if (e.key === 'Escape' && z.open) { e.stopPropagation(); imgZoomClose(); }
  };
  return z;
}

function imgZoomOpen(src, alt) {
  if (!src) return;
  const z = imgZoom || imgZoomBuild();
  if (z.open) return;
  z.img.alt = alt || '图片预览';
  z.img.src = src;
  z.open = true;
  z.overlay.classList.remove('hidden');
  window.addEventListener('keydown', z.onKey, true);
  // 先按「适应窗口」开场；图片可能还没解码完，等 load 再算一次
  if (z.img.complete && z.img.naturalWidth) requestAnimationFrame(imgZoomFit);
  else z.img.onload = () => requestAnimationFrame(imgZoomFit);
}

function imgZoomClose() {
  const z = imgZoom;
  if (!z || !z.open) return;
  z.open = false;
  z.drag = null;
  z.stage.classList.remove('dragging');
  z.overlay.classList.add('hidden');
  window.removeEventListener('keydown', z.onKey, true);
  z.img.removeAttribute('src');          // 松掉大图，别一直占着解码后的内存
  z.scale = 1; z.x = 0; z.y = 0;
  imgZoomApply();
}

/* 以舞台内某点（相对舞台中心的偏移）为锚点缩放：
   锚点对应的图像位置保持不动 → x' = ax - (ax - x) · (s'/s) */
function imgZoomTo(scale, ax, ay) {
  const z = imgZoom;
  if (!z) return;
  const next = Math.max(IMG_ZOOM_MIN, Math.min(IMG_ZOOM_MAX, scale));
  if (next === z.scale) return;
  const k = next / z.scale;
  z.x = ax - (ax - z.x) * k;
  z.y = ay - (ay - z.y) * k;
  z.scale = next;
  imgZoomApply();
}

function imgZoomFit() {
  const z = imgZoom;
  if (!z || !z.img.naturalWidth) return;
  const r = z.stage.getBoundingClientRect();
  if (!r.width || !r.height) return;
  z.scale = Math.max(IMG_ZOOM_MIN, Math.min(IMG_ZOOM_MAX,
    Math.min(r.width / z.img.naturalWidth, r.height / z.img.naturalHeight)));
  z.x = 0;
  z.y = 0;
  imgZoomApply();
}

function imgZoomApply() {
  const z = imgZoom;
  if (!z) return;
  // translate(-50%,-50%) 先把图片摆到舞台正中（配合 CSS 的 left/top:50%），
  // 于是「图片中心相对舞台中心的偏移」正好等于 (x, y)，锚点缩放公式才成立
  z.img.style.transform = 'translate(-50%,-50%) translate(' + z.x + 'px,' + z.y + 'px)'
    + ' scale(' + z.scale + ')';
  z.pct.textContent = Math.round(z.scale * 100) + '%';
}

function imgZoomWheel(e) {
  const z = imgZoom;
  if (!z || !z.open) return;
  e.preventDefault();
  const r = z.stage.getBoundingClientRect();
  imgZoomTo(z.scale * (e.deltaY < 0 ? 1.12 : 1 / 1.12),
    e.clientX - (r.left + r.width / 2), e.clientY - (r.top + r.height / 2));
}

function imgZoomDown(e) {
  const z = imgZoom;
  if (!z || !z.open || e.button !== 0) return;
  z.drag = { sx: e.clientX, sy: e.clientY, x: z.x, y: z.y,
             moved: 0, onImg: e.target === z.img };
  z.stage.classList.add('dragging');
  try { z.stage.setPointerCapture(e.pointerId); } catch (err) { /* 不支持捕获也能拖 */ }
}

function imgZoomMove(e) {
  const z = imgZoom;
  if (!z || !z.drag) return;
  z.x = z.drag.x + (e.clientX - z.drag.sx);
  z.y = z.drag.y + (e.clientY - z.drag.sy);
  z.drag.moved = Math.max(z.drag.moved,
    Math.abs(e.clientX - z.drag.sx), Math.abs(e.clientY - z.drag.sy));
  imgZoomApply();
}

function imgZoomUp() {
  const z = imgZoom;
  if (!z) return;
  const d = z.drag;
  z.drag = null;
  z.stage.classList.remove('dragging');
  // 图片外的空白处轻点即关；点在图片上不关，免得看细节时误触
  if (d && !d.onImg && d.moved < 4) imgZoomClose();
}

function imgZoomCancel() {
  const z = imgZoom;
  if (!z) return;
  z.drag = null;
  z.stage.classList.remove('dragging');
}

/* ---------------- 文档附件预览 ----------------
   发送前用 base64 直接预览；发送后消息里只留落盘名（doc.file），走 /api/attach/get 取回。 */
const DOC_PV_TEXT = ['txt', 'md', 'markdown', 'tex', 'csv', 'json', 'log', 'xml', 'yaml', 'yml',
  'html', 'htm', 'css', 'js', 'ts', 'py', 'rs', 'go', 'java', 'c', 'cpp', 'h', 'ini', 'toml'];
let docPv = null;

function docPvBuild() {
  const wrap = document.createElement('div');
  wrap.className = 'doc-pv hidden';
  wrap.innerHTML =
    '<div class="doc-pv-bar">' +
    '  <span class="doc-pv-name"></span>' +
    '  <a class="doc-pv-dl" download>下载</a>' +
    '  <span class="doc-pv-spacer"></span>' +
    '  <button type="button" class="doc-pv-close" title="关闭（Esc）">✕ 关闭</button>' +
    '</div>' +
    '<div class="doc-pv-body"></div>';
  document.body.appendChild(wrap);
  const z = docPv = {
    wrap, body: wrap.querySelector('.doc-pv-body'),
    name: wrap.querySelector('.doc-pv-name'),
    dl: wrap.querySelector('.doc-pv-dl'),
    url: '', open: false, onKey: null,
  };
  wrap.querySelector('.doc-pv-close').onclick = () => docPreviewClose();
  wrap.addEventListener('click', (e) => { if (e.target === wrap) docPreviewClose(); });
  z.onKey = (e) => {
    if (e.key === 'Escape' && z.open) { e.stopPropagation(); docPreviewClose(); }
  };
  return z;
}

function docPreviewOpen(blob, name, mime, opts) {
  if (!blob) { toast('无法读取附件'); return; }
  const z = docPv || docPvBuild();
  const ext = String(name || '').split('.').pop().toLowerCase();
  z.name.textContent = name || '附件';
  if (z.url) URL.revokeObjectURL(z.url);
  z.url = URL.createObjectURL(blob);
  z.dl.href = z.url;
  z.dl.download = name || 'attachment';
  z.body.innerHTML = '';
  if (opts && typeof opts.text === 'string') {
    // 后端已抽好的文本（Office 文件）：版式不可还原，但至少能直接看内容
    if (opts.note) {
      const tip = document.createElement('p');
      tip.className = 'doc-pv-tip';
      tip.textContent = opts.note;
      z.body.appendChild(tip);
    }
    const pre = document.createElement('pre');
    pre.className = 'doc-pv-text';
    const LIMIT = 300000;
    pre.textContent = opts.text.length > LIMIT
      ? opts.text.slice(0, LIMIT) + '\n\n…（超过 300KB，已截断；可点「下载」看全文）'
      : (opts.text || '（空文件）');
    z.body.appendChild(pre);
  } else if (DOC_PV_TEXT.indexOf(ext) >= 0 || String(mime || '').indexOf('text/') === 0) {
    const pre = document.createElement('pre');
    pre.className = 'doc-pv-text';
    pre.textContent = '读取中…';
    z.body.appendChild(pre);
    blob.text().then((t) => {
      const LIMIT = 300000;
      pre.textContent = t.length > LIMIT
        ? t.slice(0, LIMIT) + '\n\n…（超过 300KB，已截断；可点「下载」看全文）'
        : (t || '（空文件）');
    }).catch(() => { pre.textContent = '读取失败，可点「下载」查看'; });
  } else if (ext === 'pdf' || String(mime || '') === 'application/pdf') {
    const frame = document.createElement('iframe');
    frame.className = 'doc-pv-frame';
    frame.src = z.url;
    z.body.appendChild(frame);
  } else {
    const p = document.createElement('p');
    p.className = 'doc-pv-tip';
    p.textContent = '这种格式暂不支持在应用内预览，可点右上角「下载」用本机应用打开。';
    z.body.appendChild(p);
  }
  z.open = true;
  z.wrap.classList.remove('hidden');
  window.addEventListener('keydown', z.onKey, true);
}

function docPreviewClose() {
  const z = docPv;
  if (!z || !z.open) return;
  z.open = false;
  z.wrap.classList.add('hidden');
  window.removeEventListener('keydown', z.onKey, true);
  z.body.innerHTML = '';                       // iframe / pre 一起清掉
  if (z.url) { URL.revokeObjectURL(z.url); z.url = ''; }
}

/* 待发送的文档：附件对象里带 base64，直接转 Blob */
function docPreviewPending(doc) {
  if (!doc || !doc.b64) { toast('无法读取附件'); return; }
  try {
    const raw = atob(String(doc.b64));
    const buf = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) buf[i] = raw.charCodeAt(i);
    docPreviewOpen(new Blob([buf], { type: doc.mime || 'application/octet-stream' }),
                   doc.name || '附件', doc.mime);
  } catch (e) {
    toast('附件读取失败');
  }
}

/* 刚发出的附件：消息里还没拿到落盘名，先把 base64 留在内存里，
   这样发完立刻点标签也能预览（刷新后自动失效，改走落盘文件）。 */
const SENT_DOC_CACHE_MAX = 8;
const sentDocCache = new Map();

function sentDocKey(name, size) {
  return String(name || '') + '|' + (Number(size) || 0);
}

function rememberSentDoc(d) {
  if (!d || !d.b64) return;
  const k = sentDocKey(d.name, d.size);
  sentDocCache.delete(k);
  sentDocCache.set(k, { b64: d.b64, mime: d.mime || '' });
  while (sentDocCache.size > SENT_DOC_CACHE_MAX) {
    sentDocCache.delete(sentDocCache.keys().next().value);
  }
}

function recallSentDoc(dc) {
  if (!dc) return null;
  return sentDocCache.get(sentDocKey(dc.name, dc.size)) || null;
}

/* 落盘附件按扩展名兜底 MIME：旧消息可能没存 mime，PDF 缺了类型 iframe 会变成下载 */
function docMimeOf(dc) {
  const mime = String((dc && dc.mime) || '');
  if (mime && mime !== 'application/octet-stream') return mime;
  const ext = String((dc && dc.name) || '').split('.').pop().toLowerCase();
  const map = {
    pdf: 'application/pdf',
    txt: 'text/plain',
    md: 'text/plain',
    markdown: 'text/plain',
    csv: 'text/csv',
    json: 'application/json',
    xml: 'application/xml',
    html: 'text/html',
    htm: 'text/html',
    docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    xlsx: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    pptx: 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
  };
  return map[ext] || mime;
}

/* 已发送的文档：消息里只留了落盘名，按它去后端取字节 */
async function docPreviewStored(dc, sid) {
  if (!dc) return;
  const file = dc.file || dc.name;
  if (!file) { toast('找不到附件文件'); return; }
  const ext = String(dc.name || file).split('.').pop().toLowerCase();
  try {
    const res = await fetch('/api/attach/get', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Auth-Token': TOKEN },
      body: JSON.stringify({ session_id: sid, name: file }),
    });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const blob = await res.blob();
    const mime = docMimeOf(dc) || blob.type || 'application/octet-stream';
    // Office 文件不能直接在浏览器里渲染：取后端抽好的纯文本做「文本预览」，
    // 版式与图表仍以右上角「下载」为准。
    if (OFFICE_PV_EXT.indexOf(ext) >= 0) {
      try {
        const r = await api('/api/attach/text', { session_id: sid, name: file });
        if (r && r.ok) {
          const note = ['文本预览（版式与图表请下载原文件）']
            .concat(r.truncated ? ['内容较长，已截断'] : [])
            .concat(r.warn ? [String(r.warn)] : [])
            .join(' · ');
          docPreviewOpen(new Blob([blob], { type: mime }), dc.name || file, mime,
                         { text: r.text || '（空文件）', note: note });
        } else {
          docPreviewOpen(new Blob([blob], { type: mime }), dc.name || file, mime,
                         { text: '（无法提取文本：' + ((r && r.error) || '未知原因') + '）',
                           note: '可点右上角「下载」用本机应用打开' });
        }
        return;
      } catch (e) {
        // 提取文本失败就退回原来的「不支持预览」提示
      }
    }
    // 重新包一层：服务端 Content-Type 与消息里记录的 mime 不一致时，以消息为准
    docPreviewOpen(new Blob([blob], { type: mime }), dc.name || file, mime);
  } catch (e) {
    toast('附件已不存在或读取失败');
  }
}

/* Office 附件：后端抽取纯文本做应用内预览 */
const OFFICE_PV_EXT = ['docx', 'xlsx', 'xlsm', 'pptx'];

/* 重画一个文档标签（解析状态 / 字数 / 失败原因变化时复用） */
function paintDocChip(chip, dc, msgIndex, docIndex) {
  if (!chip) return;
  dc = dc || {};
  const name = dc.name || chip.dataset.name || '附件';
  chip.dataset.name = name;
  if (Number.isFinite(msgIndex)) chip.dataset.msgIndex = String(msgIndex);
  if (Number.isFinite(docIndex)) chip.dataset.docIndex = String(docIndex);
  const err = !!dc.error;
  chip.className = 'msg-doc' + (err ? ' err' : (dc.warn ? ' warn' : ''));
  chip.innerHTML = icon('attach') + ' ' + escapeHtml(name)
    + (err ? '（解析失败）' : docChipSuffix(dc));
  chip.title = err
    ? ('解析失败：' + String(dc.error || '未知原因') + ' · 点击重试')
    : (dc.warn ? String(dc.warn) + ' · 点击预览' : '点击预览');
}

/* 解析失败后重试：重新读落盘文件、重跑 docparse，并回写消息里的字数 / 原因 */
async function docReparse(msgIndex, docIndex, chip) {
  if (!chip || !Number.isFinite(msgIndex) || chip.dataset.busy === '1') return;
  chip.dataset.busy = '1';
  const name = chip.dataset.name || '附件';
  chip.className = 'msg-doc parsing';
  chip.innerHTML = icon('attach') + ' ' + escapeHtml(name) + '（重新解析…）';
  chip.title = '正在重新解析…';
  const s = curSession();
  const m = s && s.messages ? s.messages[msgIndex] : null;
  let dc = null;
  if (m) dc = (Array.isArray(m.docs) && m.docs.length) ? m.docs[docIndex] : m.doc;
  try {
    const r = await api('/api/attach/reparse', {
      session_id: state.sessions.current,
      index: msgIndex,
      doc_index: Number.isFinite(docIndex) ? docIndex : 0,
    });
    if (r && r.ok) {
      if (dc) {
        dc.chars = r.chars || 0;
        delete dc.error;
        if (r.warn) dc.warn = r.warn; else delete dc.warn;
      }
      paintDocChip(chip, dc || { name: name, chars: r.chars || 0, warn: r.warn },
                   msgIndex, docIndex);
      toast('已重新解析：' + (r.chars || 0) + ' 字');
    } else {
      const err = (r && r.error) || '未知原因';
      if (dc) dc.error = err;
      paintDocChip(chip, dc || { name: name, error: err }, msgIndex, docIndex);
      toast('重新解析失败：' + err, 'error');
    }
  } catch (e) {
    const err = (e && e.message) ? e.message : String(e);
    if (dc) dc.error = err;
    paintDocChip(chip, dc || { name: name, error: err }, msgIndex, docIndex);
    toastErr('重新解析', e);
  } finally {
    chip.dataset.busy = '0';
  }
}

function buildUserRow(text, index, msg) {
  const row = document.createElement('div');
  row.className = 'msg user';
  if (typeof index === 'number') row.dataset.idx = index;
  const wrap = document.createElement('div');
  wrap.className = 'user-wrap';

  // 图片附件：气泡上方展示缩略图（点击放大 / 还原；多张并排）
  const ims = (msg && (msg.images || (msg.image ? [msg.image] : []))) || [];
  if (ims.length) {
    const gallery = document.createElement('div');
    gallery.className = 'msg-images';
    ims.forEach((im) => {
      if (!im || (!im.name && !im.url)) return;
      const box = document.createElement('div');
      box.className = 'msg-image';
      box.title = im.size
        ? ('图片 · ' + Math.max(1, Math.round(im.size / 1024)) + ' KB（点击查看大图）')
        : '点击查看大图（滚轮缩放 / 拖动平移）';
      const el = document.createElement('img');
      el.alt = '图片';
      box.appendChild(el);
      // 点击开全屏查看器：滚轮缩放、拖动平移、双击切换适应/原始尺寸
      box.onclick = () => imgZoomOpen(el.currentSrc || el.src, '图片预览');
      if (im.url) {
        el.src = im.url;                              // 刚发出：直接用本地预览图
      } else if (im.name) {
        attachBlobUrl(state.sessions.current, im.name)
          .then((u) => { el.src = u; })
          .catch(() => {
            // 文件已不在 data/uploads（历史清理 / 测试误删）：不要静默消失，给个明确占位
            box.classList.add('missing');
            box.title = '附件文件已不在 data/uploads 中（消息里的记录还在）';
            box.textContent = '图片已丢失';
            box.onclick = null;
          });
      }
      gallery.appendChild(box);
    });
    if (gallery.children.length) wrap.appendChild(gallery);
  }

  // 文档附件：气泡上方显示文件名标签（解析结果稍后由 SSE 补上字数 / 失败原因）
  // 多附件时逐个渲染；旧会话只有 doc 字段，同样走这里
  const docs = (msg && Array.isArray(msg.docs) && msg.docs.length)
    ? msg.docs
    : ((msg && msg.doc) ? [msg.doc] : []);
  if (docs.length) {
    const dbox = document.createElement('div');
    dbox.className = 'msg-docs';
    docs.forEach((dc, di) => {
      if (!dc || !dc.name) return;
      const chip = document.createElement('div');
      chip.className = 'msg-doc';
      const rowIndex = (typeof index === 'number') ? index : NaN;
      paintDocChip(chip, dc, rowIndex, di);
      chip.onclick = () => {
        // 解析失败的标签：点一下先重试解析；其余情况打开预览
        const mi = Number(chip.dataset.msgIndex);
        const dj = Number(chip.dataset.docIndex);
        if (chip.classList.contains('err') && Number.isFinite(mi)) {
          docReparse(mi, dj, chip);
          return;
        }
        // 刚发出的附件还在内存里，直接用 base64 预览；刷新后走落盘文件
        const hit = recallSentDoc(dc);
        if (hit) docPreviewPending({ name: dc.name, mime: dc.mime || hit.mime, b64: hit.b64 });
        else docPreviewStored(dc, state.sessions.current);
      };
      dbox.appendChild(chip);
    });
    if (dbox.children.length) wrap.appendChild(dbox);
  }

  // `#` 菜单的显式引用：新消息与刷新后的历史都显示在气泡上方
  const refsBox = buildUserRefs(msg && msg.refs);
  if (refsBox) wrap.appendChild(refsBox);

  const b = document.createElement('div');
  b.className = 'bubble';
  b.textContent = text;
  const imgOnlyLabel = /^【(?:\d+ 张)?图片】$/.test(text || '');
  if (!(ims.length && imgOnlyLabel)) wrap.appendChild(b);   // 纯图片消息不再重复「【图片】」占位

  // 历史版本切换（DeepSeek 式 ‹ n/N ›）：只有被「修改」过的消息才有多版本。
  // 点箭头只「预览」（改显示、不发请求）；确认后点「发送此版本」才真正切换并重新生成。
  const vers = msg && Array.isArray(msg.versions) ? msg.versions : null;
  if (vers && vers.length > 1 && typeof index === 'number') {
    const committed = typeof msg.vidx === 'number' ? msg.vidx : vers.length - 1;
    const pv = (verPreview && verPreview.sid === state.sessions.current
                && verPreview.index === index) ? verPreview.vidx : null;
    const shown = pv === null ? committed : pv;
    if (pv !== null) b.textContent = vers[shown];     // 预览：只覆盖显示内容

    const nav = document.createElement('div');
    nav.className = 'ver-nav';
    const mkArrow = (label, target, tip, disabled) => {
      const btn = document.createElement('button');
      btn.className = 'ver-btn'; btn.textContent = label; btn.title = tip;
      btn.disabled = disabled;
      btn.onclick = () => previewVersion(index, target);
      return btn;
    };
    const info = document.createElement('span');
    info.className = 'ver-idx';
    info.textContent = (shown + 1) + '/' + vers.length;
    nav.appendChild(mkArrow('‹', shown - 1, '上一个版本', shown <= 0));
    nav.appendChild(info);
    nav.appendChild(mkArrow('›', shown + 1, '下一个版本', shown >= vers.length - 1));

    if (pv !== null && pv !== committed) {
      const tip = document.createElement('span');
      tip.className = 'ver-preview-tip';
      tip.textContent = '预览中';
      const send = document.createElement('button');
      send.className = 'ver-send'; send.textContent = '发送此版本';
      send.onclick = () => switchVersion(index, pv);
      const cancel = document.createElement('button');
      cancel.className = 'ver-cancel'; cancel.textContent = '取消';
      cancel.onclick = () => cancelVersionPreview(index);
      nav.appendChild(tip); nav.appendChild(send); nav.appendChild(cancel);
    }
    wrap.appendChild(nav);
  }

  if (typeof index === 'number') wrap.appendChild(buildActions(row, () => text, index, 'user'));
  row.appendChild(wrap);
  row._wrap = wrap;
  row._bubble = b;
  return row;
}

function appendUser(text, index, msg) {
  const row = buildUserRow(text, index, msg);   // 必须把 index 传下去，否则这条消息没有「撤回/修改」按钮
  row.classList.add('enter');                    // 只有新消息播入场动画，历史重渲染不播
  $('#chat').appendChild(row);
  scrollToBottom(true);
  return row;
}

function buildThinkBlock(text, open) {
  const box = document.createElement('div');
  box.className = 'think' + (open ? ' open' : '');
  const head = document.createElement('div');
  head.className = 'think-head';
  const dot = document.createElement('span');
  dot.textContent = '✦';
  const label = document.createElement('span');
  label.className = 'think-label';
  label.textContent = '深度思考';
  const caret = document.createElement('span');
  caret.className = 'think-caret';
  caret.textContent = '▸';
  head.appendChild(dot);
  head.appendChild(label);
  head.appendChild(caret);
  const body = document.createElement('div');
  body.className = 'think-body';
  body.textContent = text || '';
  const toggleThink = () => {
    const next = !foldIsOpen(box, 'open', true);
    foldTransition(box, body, next, { cls: 'open', positive: true, ms: 180 });
  };
  head.onclick = toggleThink;
  makeClickable(head, toggleThink, '展开 / 收起思考过程');
  box.appendChild(head);
  box.appendChild(body);
  box._body = body;
  box._label = label;
  return box;
}

// 思考收起时的一行摘要：流式中取「最后一个已完成段落」的首行，结束后取全文首行。
// 去掉 ** 标记，避免摘要里出现 Markdown 符号。
function thinkLineSummary(text, settled) {
  const s = String(text || '');
  if (!s.trim()) return '';
  if (settled) return (s.split('\n')[0] || '').replace(/\*\*/g, '').trim();
  const parts = s.split(/\r?\n[\t ]*\r?\n/).filter((p) => p.trim());
  if (!parts.length) return '';
  const pick = parts.length > 1 ? parts[parts.length - 2] : parts[parts.length - 1];
  return (pick.split('\n')[0] || '').replace(/\*\*/g, '').trim();
}

function workDuration(ms) {
  const sec = Math.max(1, Math.round((Number(ms) || 0) / 1000));
  if (sec < 60) return sec + ' 秒';
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return m + ' 分' + (s ? ' ' + s + ' 秒' : '');
}

function processFoldLabel(timing, endKind) {
  const kind = String(endKind || '');
  if (kind === 'stopped') return '过程 · 已停止';
  if (kind === 'done') return '过程 · 已完成';
  if (kind === 'billing') return '过程 · 余额不足';
  if (kind === 'network' || kind === 'server' || kind === 'error') return '过程 · 中断';
  const ms = timing && timing.total_ms;
  return ms ? '过程 · 工作 ' + workDuration(ms) : '过程';
}

function clipText(s, n) {
  const t = String(s || '').trim();
  return t.length > n ? t.slice(0, Math.max(1, n - 1)) + '…' : t;
}

// 过程统计：只数真实步骤卡（不数思考块），折叠头上给出「几步 / 几步失败」。
function processStats(flow) {
  const cards = flow ? flow.querySelectorAll('.tool-card') : [];
  let failed = 0;
  cards.forEach((c) => { if (c.classList.contains('failed')) failed += 1; });
  return { steps: cards.length, failed: failed };
}

function flowMetaText(st) {
  const parts = [];
  if (st.steps) parts.push(st.steps + ' 步');
  if (st.failed) parts.push(st.failed + ' 步失败');
  return parts.join(' · ');
}

// 流式过程中每完成一个工具就刷新一次折叠头的统计（stream.js 建头时挂了 _flowMeta）
function updateFlowMeta(row) {
  if (!row || !row._flowWrap) return;
  const st = processStats(row._flow);
  if (row._flowMeta) row._flowMeta.textContent = flowMetaText(st);
  row._flowWrap.classList.toggle('has-fail', st.failed > 0);
}

// 当前步骤高亮：并行的多个工具会同时高亮，完成一个灭一个。
// 只影响样式，不改变过程流的排布 / 折叠状态。
function markProcessStep(card) {
  if (!card || !card.classList) return;
  card.classList.add('is-current');
  const g = card.closest ? card.closest('.tool-group') : null;
  if (g) g.classList.add('current');
}

function unmarkProcessStep(card) {
  if (!card || !card.classList) return;
  card.classList.remove('is-current');
  const g = card.closest ? card.closest('.tool-group') : null;
  if (g && !g.querySelector('.tool-card.is-current')) g.classList.remove('current');
}

// 过程折叠：思考 + 工具在上，最终答案在下；默认收起。
function buildProcessFold(flow, timing, endKind, open) {
  const st = processStats(flow);
  const wrap = document.createElement('div');
  wrap.className = 'process-fold' + (open ? '' : ' collapsed') + (st.failed ? ' has-fail' : '');
  const head = document.createElement('div');
  head.className = 'process-fold-head';
  const title = document.createElement('span');
  title.className = 'process-fold-title';
  title.textContent = processFoldLabel(timing, endKind);
  const meta = document.createElement('span');
  meta.className = 'process-fold-meta';
  meta.textContent = flowMetaText(st);
  const caret = document.createElement('span');
  caret.className = 'process-fold-caret';
  caret.textContent = open ? '▾' : '▸';
  head.appendChild(title);
  head.appendChild(meta);
  head.appendChild(caret);
  const body = document.createElement('div');
  body.className = 'process-fold-body';
  body.appendChild(flow);
  wrap.appendChild(head);
  wrap.appendChild(body);
  const toggle = () => {
    const next = !foldIsOpen(wrap, 'collapsed');
    foldTransition(wrap, body, next, { cls: 'collapsed' });
    caret.textContent = next ? '▾' : '▸';
    head.setAttribute('aria-expanded', next ? 'true' : 'false');
  };
  head.onclick = toggle;
  makeClickable(head, toggle, '展开 / 收起过程');
  head.setAttribute('aria-expanded', open ? 'true' : 'false');
  observeProcessFold(wrap);
  return wrap;
}

// 来源条目（可复用于「参考来源」块和「联网搜索」过程块）
function sourceItemRows(items) {
  const frag = document.createDocumentFragment();
  (items || []).forEach((it, i) => {
    const row = document.createElement('div');
    row.className = 'src-item';
    const a = document.createElement('a');
    a.href = it.url || '#';
    a.target = '_blank';
    a.rel = 'noreferrer';
    a.textContent = '[' + (i + 1) + '] ' + (it.title || it.url || '来源');
    row.appendChild(a);
    const meta = [];
    if (it.site) meta.push(it.site);
    if (it.snippet) meta.push(it.snippet.slice(0, 130) + (it.snippet.length > 130 ? '…' : ''));
    if (meta.length) {
      const m = document.createElement('span');
      m.className = 'src-meta';
      m.textContent = meta.join(' · ');
      row.appendChild(m);
    }
    frag.appendChild(row);
  });
  return frag;
}

function buildSourcesBlock(items, open) {
  const box = buildThinkBlock('', !!open);
  box.classList.add('sources');
  box._label.textContent = '参考来源 · ' + items.length;
  box._body.textContent = '';
  box._body.appendChild(sourceItemRows(items));
  return box;
}

/* 代码 / 文件引用：回答底部列出「路径:行号」，点击直接在 IDE 里打开 */
async function openReference(it) {
  if (!it || !it.path) return;
  if (typeof setUiMode !== 'function' || typeof openIdeFile !== 'function') return;
  try {
    if (state.uiMode !== 'ide') await setUiMode('ide');
    await openIdeFile(it.path);
    if (it.start && typeof ideRevealLine === 'function') {
      ideRevealLine(it.path, it.start);
    }
  } catch (e) {
    toast('打开引用失败：' + (e && e.message ? e.message : e), 'error');
  }
}

function referenceText(it) {
  let loc = it.path || '';
  if (it.start) loc += ':' + it.start + (it.end && it.end !== it.start ? '-' + it.end : '');
  if (it.page) loc += ' · 第 ' + it.page + ' 页';
  return loc;
}

/* ---------- 用户消息里的显式引用（# 菜单 chip） ----------
   发送时先在气泡上方显示一份「预置」chip，done 事件再用服务端解析出的
   显式引用（references 里 auto=false 的条目）校正一次，保证刷新前后一致。 */
const USER_REF_ICONS = {
  file: 'note', folder: 'folder', code: 'code', terminal: 'terminal',
  git: 'branch', doc: 'book', memory: 'brain', chat: 'chat',
  problems: 'alert', rule: 'check-square', workspace: 'search',
};
const USER_REF_LABELS = {
  terminal: '终端输出', git: '未提交改动', problems: 'IDE 问题',
  rule: '项目规则', workspace: '全仓检索',
};

function userRefIcon(it) {
  if (it && it.type && USER_REF_ICONS[it.type]) return USER_REF_ICONS[it.type];
  return (it && it.start) ? 'code' : 'note';
}

function userRefLabel(it) {
  if (!it) return '';
  if (it.path) return referenceText(it);
  if (it.title) return String(it.title);
  if (it.type && USER_REF_LABELS[it.type]) return USER_REF_LABELS[it.type];
  return '引用';
}

function buildUserRefs(items) {
  // workspace 只是「全仓检索」开关，不产生引用正文；历史里不显示，避免刷新后消失
  const list = (items || []).filter((it) => it && !it.auto
    && it.type !== 'workspace'
    && (it.path || it.title || it.type));
  if (!list.length) return null;
  const box = document.createElement('div');
  box.className = 'msg-refs';
  list.forEach((it) => {
    const chip = document.createElement('span');
    chip.className = 'msg-ref';
    const clickable = !it.virtual && it.path
      && (!it.type || it.type === 'file' || it.type === 'code');
    const label = userRefLabel(it);
    if (clickable) {
      chip.classList.add('link');
      chip.title = label + '（点击在 IDE 中打开）';
      chip.onclick = () => openReference(it);
    } else {
      chip.classList.add('virtual');
      chip.title = label + '（注入的上下文，不对应沙箱文件）';
    }
    chip.innerHTML = icon(userRefIcon(it)) +
      '<span class="msg-ref-label">' + escapeHtml(label) + '</span>';
    box.appendChild(chip);
  });
  return box;
}

/* 用服务端解析后的显式引用替换用户气泡上的临时 chip */
function setUserRefs(userRow, items) {
  if (!userRow || !userRow._wrap) return;
  const wrap = userRow._wrap;
  const old = wrap.querySelector('.msg-refs');
  if (old) old.remove();
  const box = buildUserRefs(items);
  if (!box) return;
  const bubble = wrap.querySelector('.bubble');
  if (bubble) wrap.insertBefore(box, bubble);
  else wrap.appendChild(box);
}

function buildReferencesBlock(items, open) {
  const box = buildThinkBlock('', !!open);
  box.classList.add('refs');
  const codeLike = (items || []).some((it) => it && it.start);
  box._label.innerHTML = icon(codeLike ? 'code' : 'book') + ' 引用 · ' + items.length;
  box._body.textContent = '';
  (items || []).forEach((it) => {
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'ref-item';
    row.appendChild(spanHtml('span', 'ref-path', referenceText(it)));
    if (it.symbol) row.appendChild(spanHtml('span', 'ref-meta', it.symbol));
    else if (it.auto) row.appendChild(spanHtml('span', 'ref-meta', '自动检索'));
    else if (it.virtual) row.appendChild(spanHtml('span', 'ref-meta', '上下文'));
    if (it.virtual) {
      row.classList.add('virtual');
      row.title = '这是注入的上下文，不对应沙箱文件';
    } else {
      row.onclick = () => openReference(it);
    }
    box._body.appendChild(row);
  });
  return box;
}

/* 小工具：创建带文本的 span（避免逐个 createElement/textContent 的重复代码） */
function spanHtml(tag, cls, text) {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  el.textContent = text == null ? '' : String(text);
  return el;
}

/* 联网搜索过程块：上面是步骤列表（边搜边追加），完成后下面接来源列表 */
function buildSearchBox() {
  const box = buildThinkBlock('', true);
  box.classList.add('sources');
  box._label.innerHTML = icon('search') + ' 联网搜索';
  box._body.textContent = '';
  const steps = document.createElement('div');
  steps.className = 'search-steps';
  box._body.appendChild(steps);
  box._steps = steps;
  box._fetchLine = null;
  box._planLine = null;
  return box;
}

function searchStep(box, text, cls) {
  const d = document.createElement('div');
  d.className = 'search-step' + (cls ? ' ' + cls : '');
  d.textContent = text;
  box._steps.appendChild(d);
  scrollToBottom();
  return d;
}

// 把来源列表接到搜索块下面，并把标题改成最终态
function appendSearchSources(box, items) {
  const srcWrap = document.createElement('div');
  srcWrap.className = 'search-src';
  const div = document.createElement('div');
  div.className = 'search-step';
  div.textContent = '─── 参考来源 ───';
  srcWrap.appendChild(div);
  srcWrap.appendChild(sourceItemRows(items));
  box._body.appendChild(srcWrap);
  // 一轮里可能搜多次：把「步骤」容器挪到来源块之后，第二次及以后的步骤才会排在时间线上
  // （否则新步骤会插到第一组来源的上方，看起来像顺序错乱）
  if (box._steps) box._body.appendChild(box._steps);
  box._label.innerHTML = icon('search') + ' 联网搜索 · ' + (items || []).length + ' 条来源';
  return box;
}

// 缓存命中率：命中的 token 本来就是输入的一部分，所以分母用 prompt。
// 没有命中 / 输入为 0 时返回空串，调用处直接拼在后面。
function cacheHitPct(cached, prompt) {
  const c = Number(cached) || 0;
  const p = Number(prompt) || 0;
  if (c <= 0 || p <= 0) return '';
  const pct = Math.round(Math.max(0, Math.min(100, (c / p) * 100)) * 10) / 10;
  return '（' + pct + '%）';
}

// 输出完成的系统时间（本地时区）：排查「这轮到底什么时候结束的」比浏览器相对时间可靠。
// 带日期：跨天回看历史时，只显示时分秒分不清是哪一天。
function clockText(ts) {
  const n = Number(ts) || 0;
  if (n <= 0) return '';
  const d = new Date(n * 1000);
  if (!Number.isFinite(d.getTime())) return '';
  const p = (v) => String(v).padStart(2, '0');
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) +
    ' ' + p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
}

// 用量行保持「一行文本」的观感：数字用 · 分隔，口径解释放悬停提示，不挤在正文里。
// usage.prompt 是整回合「每轮输入相加」的累计值，不是单次请求。
// timing.total_ms：从用户发出指令（服务端收到请求）到整轮输出完成，精确到毫秒。
function usageText(u, doneAt, calls, timing) {
  const ms = Number(timing && timing.total_ms) || 0;
  const elapsed = ms > 0 ? ('完成耗时 ' + (ms / 1000).toFixed(3) + ' 秒') : '';
  if (!u) return elapsed;
  const f = (n) => (n || 0).toLocaleString('en-US');
  const n = Number(calls) || 0;
  let s = (u.model ? u.model + ' · ' : '');
  if (n > 0) s += n + ' 次调用 · ';
  s += '总输入 ' + f(u.prompt) + ' · 总输出 ' + f(u.completion) +
    ' · 合计 ' + f(u.total);
  if (u.cached) {
    const uncached = Math.max(0, Number(u.prompt || 0) - Number(u.cached || 0));
    s += '\n缓存命中 ' + f(u.cached) + cacheHitPct(u.cached, u.prompt) +
      ' · 未命中 ' + f(uncached);
    if (u.last_prompt) s += ' · 末轮输入 ' + f(u.last_prompt);
  }
  const t = clockText(doneAt);
  if (t) s += '\n完成时间 ' + t;
  if (elapsed) s += '\n' + elapsed;
  return s;
}

function usageTitle(u, timing) {
  const base = '总输入是整回合累计（每次调用都会重发完整上下文）；' +
    '缓存命中已包含在输入里，计费只按未命中部分；末轮输入是最后一次请求的输入大小。';
  const ms = Number(timing && timing.total_ms) || 0;
  return ms > 0
    ? base + '完成耗时从服务端收到指令算起，包含工具执行与等待审批的时间，精确到毫秒。'
    : base;
}

// 统一入口：文本 + 悬停解释一起设置，避免各调用点重复。
function setUsage(el, u, doneAt, calls, timing) {
  if (!el) return;
  el.textContent = usageText(u, doneAt, calls, timing);
  el.title = usageTitle(u, timing);
}

// 这一回合实际调用模型几次：优先逐轮明细条数，其次 rounds（旧消息）。
function usageCalls(m) {
  const t = (m && m.timing) || {};
  const rd = t.rounds_detail || [];
  return rd.length || Number(t.rounds) || 0;
}

/* ---------- Agent：工具卡片 / 提问卡片 ---------- */
const TOOL_LABELS = {
  list_dir: ['folder', '列目录'], read_file: ['note', '读文件'],
  read_files: ['note', '批量读取'], search_files: ['search', '搜索文件'],
  search_code: ['search', '语义检索'],
  repo_map: ['list', '仓库结构'],
  write_file: ['pencil', '写文件'], edit_file: ['pencil', '修改文件'], make_dir: ['folder-plus', '新建目录'],
  archify: ['branch', '架构图'],
  move_file: ['box', '移动'], delete_file: ['trash', '删除'], ask_user: ['question', '提问'],
  finish: ['check', '声明完成'],
  run_command: ['terminal', '运行命令'],
  render_check: ['image', '渲染自检'],
  computer_open: ['globe', '打开网页'],
  computer_see: ['search', '查看页面'],
  computer_act: ['pencil', '页面操作'],
  computer_read: ['list', '读取页面'],
  computer_shot: ['image', '页面截图'],
  computer_windows: ['list', '桌面窗口'],
  set_reminder: ['history', '定时提醒'],
  lsp_diagnostics: ['check-square', '类型检查'], lsp_definition: ['branch', '跳转定义'],
  lsp_references: ['search', '查找引用'], lsp_rename: ['pencil', '语义重命名'],
  git_status: ['branch', 'Git 状态'], git_diff: ['history', 'Git 改动'],
  git_log: ['history', '提交历史'], git_commit: ['check', 'Git 提交'],
  explore: ['search', '并行探索'], delegate: ['brain', '子代理团队'],
  apply_worktree: ['check', '回写子代理改动'],
};

// 步骤组摘要：按「动作 + 数量」聚合，比「读文件×3」更像人话（对齐 Trae 的 trace 文案）
const TOOL_SUMMARY = {
  list_dir: (n) => '查看 ' + n + ' 个目录',
  read_file: (n) => '读取 ' + n + ' 个文件',
  read_files: (n) => '批量读取 ' + n + ' 次',
  search_files: (n) => '搜索 ' + n + ' 次',
  search_code: (n) => '语义检索 ' + n + ' 次',
  repo_map: (n) => '查看结构 ' + n + ' 次',
  write_file: (n) => '写入 ' + n + ' 个文件',
  edit_file: (n) => '修改 ' + n + ' 个文件',
  make_dir: (n) => '新建 ' + n + ' 个目录',
  move_file: (n) => '移动 ' + n + ' 个文件',
  delete_file: (n) => '删除 ' + n + ' 个文件',
  run_command: (n) => '执行 ' + n + ' 条命令',
  render_check: (n) => '渲染自检 ' + n + ' 次',
  computer_open: (n) => '打开 ' + n + ' 个页面',
  computer_see: (n) => '查看页面 ' + n + ' 次',
  computer_act: (n) => '页面操作 ' + n + ' 次',
  computer_read: (n) => '读取页面 ' + n + ' 次',
  computer_shot: (n) => '页面截图 ' + n + ' 次',
  computer_windows: (n) => '列出窗口 ' + n + ' 次',
  set_reminder: (n) => '设置 ' + n + ' 个提醒',
  lsp_diagnostics: (n) => '类型检查 ' + n + ' 次',
  lsp_definition: (n) => '跳转定义 ' + n + ' 次',
  lsp_references: (n) => '查找引用 ' + n + ' 次',
  lsp_rename: (n) => '语义重命名 ' + n + ' 次',
  git_status: (n) => '查看 Git 状态 ' + n + ' 次',
  git_diff: (n) => '查看改动 ' + n + ' 个文件',
  git_log: (n) => '查看提交历史 ' + n + ' 次',
  git_commit: (n) => 'Git 提交 ' + n + ' 次',
};
// 运行中「正在…」用的动词（要短，后面直接跟参数）
const TOOL_VERBS = {
  list_dir: '查看目录', read_file: '读取', search_files: '搜索', write_file: '写入',
  edit_file: '修改', make_dir: '新建目录', move_file: '移动', delete_file: '删除',
  set_reminder: '设置提醒', search_code: '检索', repo_map: '查看结构', run_command: '执行',
  render_check: '渲染自检',
  computer_open: '打开网页', computer_see: '查看页面', computer_act: '操作页面',
  computer_read: '读取页面', computer_shot: '页面截图',
  computer_windows: '桌面窗口',
  lsp_diagnostics: '类型检查', lsp_definition: '跳转定义',
  lsp_references: '查找引用', lsp_rename: '语义重命名',
  git_status: '查看 Git 状态', git_diff: '查看改动',
  git_log: '查看提交历史', git_commit: 'Git 提交',
};

function toolArgText(name, args) {
  args = args || {};
  if (name === 'run_command') {
    return (Array.isArray(args.argv) ? args.argv.join(' ') : '') || args.cmd || args.command || '';
  }
  if (name === 'move_file') return (args.from || '') + ' → ' + (args.to || '');
  if (name === 'computer_open') return args.url || '';
  if (name === 'computer_act') {
    return [args.target, args.action, args.ref != null ? '[' + args.ref + ']' : '', args.value]
      .filter((x) => x !== '' && x != null).join(' ');
  }
  if (name === 'computer_see') return args.target || '';
  if (name === 'computer_shot') return args.target || '';
  if (name === 'computer_read') return [args.kind || 'console', args.target].filter(Boolean).join(' ');
  if (name === 'render_check') return [args.path, args.viewport].filter(Boolean).join('   ');
  if (name === 'search_files') return [args.pattern, args.glob].filter(Boolean).join('   ');
  if (name === 'lsp_rename') {
    return [args.path, args.symbol && (args.symbol + ' → ' + (args.new_name || ''))]
      .filter(Boolean).join('   ');
  }
  if (String(name).startsWith('lsp_')) {
    return [args.path, args.line && ('第 ' + args.line + ' 行'), args.symbol]
      .filter(Boolean).join('   ');
  }
  if (name === 'git_commit') return String(args.message || '').replace(/\n/g, ' ');
  return args.path || '';
}

// 能定位到具体文件的工具 → 其路径在工具卡里渲染成可点击链接（点了切到 IDE 打开）
const TOOL_PATH_TOOLS = ['read_file', 'write_file', 'edit_file', 'delete_file', 'render_check',
  'lsp_diagnostics', 'lsp_definition', 'lsp_references', 'lsp_rename', 'git_diff'];

// Git 高危子命令：与后端 agent._cmd_kind 的高风险集合保持一致（只用于前端标注）
const GIT_RISK_SUBS = ['checkout', 'restore', 'reset', 'clean', 'push', 'switch', 'rebase'];

function toolGitInfo(name, args) {
  if (name !== 'run_command') return null;
  args = args || {};
  let argv = args.argv;
  if (typeof argv === 'string') argv = argv.split(/\s+/).filter(Boolean);
  if (!Array.isArray(argv) || !argv.length) {
    const cmd = String(args.cmd || args.command || '').trim();
    argv = cmd ? cmd.split(/\s+/).filter(Boolean) : [];
  }
  if (!argv.length) return null;
  const exe = String(argv[0]).replace(/\\/g, '/').split('/').pop().toLowerCase();
  if (exe !== 'git' && exe !== 'git.exe') return null;
  let sub = '';
  for (let i = 1; i < argv.length; i += 1) {
    const t = String(argv[i]);
    if (t.startsWith('-')) continue;
    sub = t;
    break;
  }
  sub = sub || 'git';
  return { sub: sub, risk: GIT_RISK_SUBS.includes(sub.toLowerCase()) };
}

async function openGitPanel() {
  if (typeof setUiMode === 'function') await setUiMode('ide');
  if (typeof ideSwitchSide === 'function') ideSwitchSide('git');
  if (typeof gitRefresh === 'function') gitRefresh();
}

function toolArgHtml(name, args) {
  args = args || {};
  const link = (p) => '<a class="tool-path" data-path="' + escapeHtml(String(p)) +
    '" title="在 IDE 中打开">' + escapeHtml(String(p)) + '</a>';
  if (name === 'move_file') {
    if (args.from || args.to) {
      return (args.from ? link(args.from) : '') + ' → ' + (args.to ? link(args.to) : '');
    }
  } else if (TOOL_PATH_TOOLS.includes(name) && args.path) {
    return link(args.path);
  }
  return escapeHtml(toolArgText(name, args));
}

// 点工具卡里的路径：切到 IDE 模式并在应用内打开该文件
async function openToolPath(path) {
  if (!path) return;
  const rel = String(path).replace(/\\/g, '/');
  setUiMode('ide');
  await openIdeFile(rel);
}

function fmtToolMs(ms) {
  ms = parseInt(ms, 10) || 0;
  return ms >= 1000 ? (ms / 1000).toFixed(1) + 's' : ms + 'ms';
}

/* ---------- 执行计划卡（Plan / TODO） ----------
   模型通过 update_plan 工具维护；这里只负责把最新计划画成一张可折叠清单。 */
const PLAN_MARK = { done: '✓', doing: '●', pending: '○', blocked: '!' };

// 历史回显的折叠规则要和实时一致：只有全部完成才收起，没做完就保持展开。
function planAllDone(plan) {
  const steps = (plan && plan.steps) || (Array.isArray(plan) ? plan : []);
  return steps.length > 0 && steps.every((s) => s.status === 'done');
}

function buildPlanCard(plan, collapsed) {
  const card = document.createElement('div');
  card.className = 'plan-card';
  card.innerHTML =
    '<div class="plan-head">' +
      '<span class="plan-ico">' + icon('check-square') + '</span>' +
      '<span class="plan-title"></span>' +
      '<span class="plan-delta-count"></span>' +
      '<span class="tool-caret">' + icon('chevron') + '</span>' +
    '</div>' +
    '<div class="plan-body"></div>';
  const head = card.querySelector('.plan-head');
  const body = card.querySelector('.plan-body');
  const toggle = () => {
    const next = !foldIsOpen(card, 'collapsed');
    foldTransition(card, body, next, { cls: 'collapsed' });
    // 记住这是「用户自己折的」：之后新计划到达时不再自动展开，尊重用户操作
    card._userCollapsed = !next;
    head.setAttribute('aria-expanded', next ? 'true' : 'false');
  };
  head.addEventListener('click', toggle);
  makeClickable(head, toggle, '展开 / 收起执行计划');
  planFill(card, plan || { steps: [] });
  if (collapsed) {
    card.classList.add('collapsed');
    head.setAttribute('aria-expanded', 'false');
  } else {
    head.setAttribute('aria-expanded', 'true');
  }
  return card;
}

/* 计划 diff：比较「上一版 → 这一版」，标出这次具体改了哪几步。
   返回 { map: {id: {label, cls}}, added, changed, removed }；没有基准时全空。 */
function planDiff(prev, next) {
  const out = { map: {}, added: 0, changed: 0, removed: 0 };
  const before = Array.isArray(prev) ? prev : ((prev && prev.steps) || []);
  const after = Array.isArray(next) ? next : ((next && next.steps) || []);
  if (!before.length || !after.length) return out;
  const old = {};
  before.forEach((s) => { if (s && s.id != null) old[String(s.id)] = s; });
  const now = {};
  after.forEach((s) => { if (s && s.id != null) now[String(s.id)] = s; });
  after.forEach((s) => {
    const id = String((s && s.id) || '');
    if (!id) return;
    const p = old[id];
    if (!p) { out.map[id] = { label: '新增', cls: 'new' }; out.added += 1; return; }
    const bits = [];
    const st = String(s.status || '');
    if (String(p.status || '') !== st) {
      bits.push(st === 'done' ? '完成'
        : st === 'blocked' ? '受阻'
          : st === 'doing' ? '进行中' : '重置');
    }
    if (String(p.text || '') !== String(s.text || '')) bits.push('改写');
    if (String(p.verify || '') !== String(s.verify || '')) bits.push('验收');
    if (String(p.ref || '') !== String(s.ref || '')) bits.push('引用');
    if (String(p.milestone || '') !== String(s.milestone || '')) bits.push('移组');
    if (String((p.deps || []).join(',')) !== String((s.deps || []).join(','))) bits.push('依赖');
    if (String((p.accept || []).join(',')) !== String((s.accept || []).join(','))) bits.push('验收项');
    if (bits.length) { out.map[id] = { label: bits.join('·'), cls: 'mod' }; out.changed += 1; }
  });
  before.forEach((s) => {
    const id = String((s && s.id) || '');
    if (id && !now[id]) out.removed += 1;
  });
  return out;
}

function planStepHtml(s, delta) {
  const st = (s && s.status) || 'pending';
  const verify = (s && s.verify) ? String(s.verify) : '';
  const ref = (s && s.ref) ? String(s.ref) : '';
  const accept = (s && Array.isArray(s.accept)) ? s.accept.filter(Boolean).join(' ') : '';
  const d = delta || null;
  return '<div class="plan-step ' + st + (d ? ' changed ' + d.cls : '') + '">' +
      '<span class="plan-mark">' + (PLAN_MARK[st] || PLAN_MARK.pending) + '</span>' +
      '<span class="plan-text">' + escapeHtml((s && s.text) || '') + '</span>' +
      (accept ? '<span class="plan-accept" title="关联的验收项">⚑ ' + escapeHtml(accept) + '</span>' : '') +
      (d ? '<span class="plan-delta ' + d.cls + '">' + escapeHtml(d.label) + '</span>' : '') +
      (verify ? '<span class="plan-verify">↳ ' + escapeHtml(verify) + '</span>' : '') +
      (ref ? '<span class="plan-ref">↗ ' + escapeHtml(ref) + '</span>' : '') +
    '</div>';
}

function planFill(card, plan) {
  // 兼容旧调用：plan 直接是 steps 数组
  const isArr = Array.isArray(plan);
  const steps = isArr ? plan : ((plan && plan.steps) || []);
  // 同一版计划重复到达时不重画：否则刚闪出来的改动徽标会被擦掉
  if (card._diffBase && JSON.stringify(card._diffBase) === JSON.stringify(isArr ? steps : plan)) {
    return;
  }
  const mss = isArr ? [] : ((plan && plan.milestones) || []);
  const rev = Number(!isArr && plan && plan.revision) || 1;
  const goal = (!isArr && plan && plan.goal) || '';
  const doneWhen = (!isArr && plan && plan.done_when) || [];
  const log = (!isArr && plan && plan.log) || {};
  const diff = planDiff(card._diffBase, isArr ? steps : plan);
  card._diffBase = isArr ? steps : plan;
  const done = steps.filter((s) => s.status === 'done').length;
  const doing = steps.find((s) => s.status === 'doing');
  const title = card.querySelector('.plan-title');
  const doingMs = doing ? mss.find((m) => m.id === doing.milestone) : null;
  if (title) {
    let t;
    if (steps.length && done === steps.length) {
      t = '计划完成 · ' + steps.length + ' 步' + (rev > 1 ? ' · v' + rev : '');
    } else if (doing) {
      t = '计划 v' + rev + ' · ' + done + '/' + steps.length + ' · 正在'
        + (doingMs ? ' ' + (doingMs.id || '') + '「' + (doingMs.title || '') + '」：' : '')
        + (doing.text || '');
    } else {
      t = '计划 v' + rev + ' · ' + done + '/' + steps.length + ' 步';
    }
    title.textContent = t;
    card.title = goal || '';
  }
  // 本次改动：头部一行小字 + 具体步骤上的徽标（步骤徽标在 planStepHtml 里）
  const deltaEl = card.querySelector('.plan-delta-count');
  if (deltaEl) {
    const bits = [];
    if (diff.added) bits.push('新增 ' + diff.added);
    if (diff.changed) bits.push('改动 ' + diff.changed);
    if (diff.removed) bits.push('删除 ' + diff.removed);
    deltaEl.textContent = bits.length ? '本次 ' + bits.join(' · ') : '';
    deltaEl.style.display = bits.length ? '' : 'none';
  }
  const body = card.querySelector('.plan-body');
  if (body) {
    let html = '';
    if (goal) {
      html += '<div class="plan-meta"><div class="plan-goal">目标：'
        + escapeHtml(goal) + '</div></div>';
    }
    if (diff.removed) {
      html += '<div class="plan-delta-note">本次删除了 ' + diff.removed + ' 步</div>';
    }
    if (mss.length) {
      const used = new Set();
      mss.forEach((m) => {
        const ss = steps.filter((s) => s.milestone === m.id);
        ss.forEach((s) => used.add(s));
        if (!ss.length) return;
        const d = ss.filter((s) => s.status === 'done').length;
        const blocked = ss.some((s) => s.status === 'blocked');
        const cls = d === ss.length ? ' done' : (blocked ? ' blocked' : (ss.some((s) => s.status === 'doing') ? ' doing' : ''));
        html += '<div class="plan-ms' + cls + '">'
          + '<div class="plan-ms-head"><span class="plan-ms-id">' + escapeHtml(m.id || '') + '</span>'
          + '<span class="plan-ms-title">' + escapeHtml(m.title || '') + '</span>'
          + '<span class="plan-ms-count">' + d + '/' + ss.length + '</span></div>'
          + (m.done_when ? '<div class="plan-ms-done">验收：' + escapeHtml(m.done_when) + '</div>' : '')
          + ss.map((s) => planStepHtml(s, diff.map[String(s.id || '')])).join('')
          + '</div>';
      });
      const rest = steps.filter((s) => !used.has(s));
      if (rest.length) {
        html += rest.map((s) => planStepHtml(s, diff.map[String(s.id || '')])).join('');
      }
    } else {
      html += steps.map((s) => planStepHtml(s, diff.map[String(s.id || '')])).join('');
    }
    if (doneWhen.length) {
      // 整体完成判据（两行以内，避免卡片太长）
      const dw = doneWhen.slice(0, 4).map((x) => '<li>' + escapeHtml(String(x)) + '</li>').join('');
      html += '<div class="plan-done-when"><span>完成判据</span><ul>' + dw + '</ul></div>';
    }
    const retro = (!isArr && plan && plan.retro) ? String(plan.retro) : '';
    if (retro) {
      html += '<div class="plan-retro"><span>复盘</span><div>' + escapeHtml(retro) + '</div></div>';
    }
    const logs = []
      .concat((log.decisions || []).slice(-3).map((x) => '决策：' + (x.text || '')))
      .concat((log.discoveries || []).slice(-3).map((x) => '发现：' + (x.text || '')));
    if (logs.length) {
      html += '<details class="plan-log"><summary>变更记录（' + logs.length + '）</summary>'
        + logs.map((x) => '<div class="plan-log-item">' + escapeHtml(x) + '</div>').join('')
        + '</details>';
    }
    body.innerHTML = html;
  }
  card.classList.toggle('all-done', steps.length > 0 && done === steps.length);
}

// 流式更新：同一个 row 复用同一张卡，只刷新内容
function planRender(row, plan, note) {
  if (!row) return;
  const isArr = Array.isArray(plan);
  const steps = isArr ? plan : ((plan && plan.steps) || []);
  if (!steps.length) return;
  const full = isArr ? { steps: steps } : plan;
  let card = row._planCard;
  if (!card) {
    card = buildPlanCard(full, false);
    if (row._content && row._content.parentNode === row._wrap) {
      row._wrap.insertBefore(card, row._content);     // 计划在正文上方
    } else if (row._wrap) {
      row._wrap.appendChild(card);
    }
    row._planCard = card;
    row._planNote = note || '';
  }
  if (full && full.carried) planMarkCarried(card);
  planFill(card, full);
  row._planSteps = steps;
  row._plan = full;
  state.lastPlan = steps;          // 输入框上方的审阅提示栏要展开「任务项」
  // 同一会话只留一张「活卡」：续跑接管后旧块的卡自动折叠，避免两张展开的卡同时存在
  planMarkLive(card);
  // 续跑后步骤重新变回未完成：把系统折叠的卡展开，别让用户以为计划卡「不会继续了」
  if (steps.some((s) => (s && s.status) !== 'done')) planAutoExpand(card);
}

/* 继承卡标记：接管 / 续跑新起的行里，计划是从上一轮继承来的，标题旁挂一个「续」徽标 */
function planMarkCarried(card) {
  if (!card || card._carried) return;
  card._carried = true;
  card.classList.add('plan-carried');
  const head = card.querySelector('.plan-head');
  const title = card.querySelector('.plan-title');
  if (!head || !title) return;
  const badge = document.createElement('span');
  badge.className = 'plan-carried-badge';
  badge.textContent = '续';
  badge.title = '从上一轮继承的计划';
  head.insertBefore(badge, title.nextSibling);
}

/* 旧卡：折叠 + 标注「已续至最新输出」；历史仍在，点开可看 */
function planSupersede(card) {
  if (!card || card.classList.contains('plan-superseded')) return;
  foldTransition(card, card.querySelector('.plan-body'), false, { cls: 'collapsed' });
  card.classList.add('plan-superseded');
  const head = card.querySelector('.plan-head');
  if (head) head.setAttribute('aria-expanded', 'false');
  if (head && !card.querySelector('.plan-superseded-note')) {
    const note = document.createElement('span');
    note.className = 'plan-superseded-note';
    note.textContent = '已续至最新输出';
    head.insertBefore(note, card.querySelector('.tool-caret') || null);
  }
}

/* 当前会话只允许一张展开的活卡：新事件指向哪张，其它卡就收起来 */
function planMarkLive(card) {
  if (!card || !card.parentNode) return;
  const chat = $('#chat');
  if (!chat) return;
  chat.querySelectorAll('.plan-card').forEach((other) => {
    if (other !== card) planSupersede(other);
  });
}

// 系统自动折叠（收尾 / supersede）的卡，遇到又「没做完」的计划时自动展开
function planAutoExpand(card) {
  if (!card || card._userCollapsed || !card.classList.contains('collapsed')) return;
  foldTransition(card, card.querySelector('.plan-body'), true, { cls: 'collapsed' });
  const head = card.querySelector('.plan-head');
  if (head) head.setAttribute('aria-expanded', 'true');
}

/* 渲染完历史：只有最新那条计划保持展开，更早的卡收成「已续至最新输出」。
   与实时一致的规则，避免刷新后满屏都是展开的计划卡。 */
function planSweepHistory() {
  const chat = $('#chat');
  if (!chat) return;
  const cards = Array.prototype.slice.call(chat.querySelectorAll('.plan-card'));
  if (cards.length < 2) return;
  const live = cards[cards.length - 1];
  cards.forEach((c) => { if (c !== live) planSupersede(c); });
}

/* 接管 / 续跑新建一行时，先把当前计划「继承」过来：
   不让新块空着等模型下一次 update_plan，进度从这里就能继续看。 */
function planSeedFromSession(row) {
  if (!row || row._planCard) return false;
  const s = (typeof curSession === 'function') ? curSession() : null;
  const msgs = (s && s.messages) || [];
  let plan = null;
  for (let i = msgs.length - 1; i >= 0; i--) {
    const p = msgs[i] && msgs[i].plan;
    if (p && Array.isArray(p.steps) && p.steps.length) { plan = p; break; }
  }
  if (!plan && (state.lastPlan || []).length) plan = { steps: state.lastPlan };
  if (!plan) return false;
  planRender(row, Object.assign({}, plan, { carried: true }));
  return true;
}

/* 切会话 / 刷新后恢复「任务」面板：取当前会话最后一条带 plan 的消息。
   实时 plan 事件只覆盖正在生成的这一轮，历史里必须从落库消息重建，
   否则会出现「当前会话有计划却显示没有」或「显示上一个会话的旧计划」。 */
function planSyncFromSession(s) {
  const msgs = (s && s.messages) || [];
  let steps = [];
  for (let i = msgs.length - 1; i >= 0; i--) {
    const p = msgs[i] && msgs[i].plan;
    if (p && Array.isArray(p.steps) && p.steps.length) {
      steps = p.steps;
      break;
    }
  }
  state.lastPlan = steps;
  if (typeof ideReviewTipRefresh === 'function') ideReviewTipRefresh();
}

// 本轮结束：全部完成才自动折叠（没做完就保持展开，方便看到卡在哪一步）
function planAutoCollapse(row) {
  const card = row && row._planCard;
  const steps = (row && row._planSteps) || [];
  if (!card || !steps.length || !steps.every((s) => s.status === 'done')) return;
  foldTransition(card, card.querySelector('.plan-body'), false, { cls: 'collapsed' });
  const head = card.querySelector('.plan-head');
  if (head) head.setAttribute('aria-expanded', 'false');
}

/* ---------- 计划审批卡（Claude 式 plan mode）----------
   计划第一次出现时后端会挂起，等这里批准 / 提意见 / 逐条改步骤。
   回答后卡片留在原地当记录；批准之后的计划微调不会再弹。 */
function planReviewRender(row, obj) {
  const wrap = row && row._wrap;
  if (!wrap) return;
  if (row._planReviewCard) row._planReviewCard.remove();
  const plan = (obj && obj.plan) || row._plan || {};
  const steps = Array.isArray(plan.steps) ? plan.steps : [];
  const card = document.createElement('div');
  card.className = 'plan-review-card';
  card._rid = (obj && obj.rid) || '';
  const head = document.createElement('div');
  head.className = 'plan-review-head';
  head.innerHTML = '<span class="plan-review-ico">' + icon('check-square') + '</span>'
    + '<span class="plan-review-title">计划待你批准 · 可逐条修改</span>'
    + '<span class="plan-review-state"></span>'
    + '<span class="plan-review-caret">' + icon('chevron') + '</span>';
  const toggle = () => {
    const next = !foldIsOpen(card, 'collapsed');
    foldTransition(card, card.querySelector('.plan-review-body'), next, { cls: 'collapsed' });
    head.setAttribute('aria-expanded', next ? 'true' : 'false');
  };
  head.onclick = toggle;
  makeClickable(head, toggle, '展开 / 收起计划审批');
  card.appendChild(head);
  const body = document.createElement('div');
  body.className = 'plan-review-body';

  const list = document.createElement('div');
  list.className = 'plan-review-list';
  steps.forEach((s) => {
    const item = document.createElement('div');
    item.className = 'plan-review-item';
    const tag = document.createElement('span');
    tag.className = 'plan-review-id';
    tag.textContent = s.id || '';
    const ta = document.createElement('textarea');
    ta.className = 'plan-review-text';
    ta.rows = 1;
    ta.value = s.text || '';
    ta.dataset.id = s.id || '';
    ta.dataset.status = s.status || 'pending';
    ta.dataset.milestone = s.milestone || '';
    ta.dataset.verify = s.verify || '';
    item.appendChild(tag);
    item.appendChild(ta);
    list.appendChild(item);
  });

  const comment = document.createElement('textarea');
  comment.className = 'plan-review-comment';
  comment.rows = 2;
  comment.placeholder = '写一句意见让模型重排计划（留空 = 只批准）';
  body.appendChild(list);
  body.appendChild(comment);

  const acts = document.createElement('div');
  acts.className = 'plan-review-actions';
  const ok = document.createElement('button');
  ok.type = 'button';
  ok.className = 'plan-review-ok';
  ok.textContent = '批准并执行';
  const re = document.createElement('button');
  re.type = 'button';
  re.className = 'plan-review-replan';
  re.textContent = '按意见重排';
  acts.appendChild(ok);
  acts.appendChild(re);
  body.appendChild(acts);
  card.appendChild(body);

  const fit = () => {
    list.querySelectorAll('.plan-review-text').forEach((t) => {
      t.style.height = 'auto';
      t.style.height = Math.max(26, t.scrollHeight) + 'px';
    });
    comment.style.height = 'auto';
    comment.style.height = Math.max(40, comment.scrollHeight) + 'px';
  };
  // 单行 textarea 会被内部滚动截掉上半行：按内容自动撑高，避免文字被遮挡
  list.addEventListener('input', (e) => {
    const t = e.target;
    if (!t || !t.classList || !t.classList.contains('plan-review-text')) return;
    t.style.height = 'auto';
    t.style.height = Math.max(26, t.scrollHeight) + 'px';
  });
  comment.addEventListener('input', () => {
    comment.style.height = 'auto';
    comment.style.height = Math.max(40, comment.scrollHeight) + 'px';
  });

  const settle = (text, cls) => {
    card.classList.add(cls || 'settled');
    // 交互完就收起：卡片留在原地当记录，但不再占一屏
    foldTransition(card, body, false, { cls: 'collapsed' });
    head.setAttribute('aria-expanded', 'false');
    const state = card.querySelector('.plan-review-state');
    if (state) state.textContent = text;
    if (acts.parentNode) acts.remove();
    comment.disabled = true;
    list.querySelectorAll('.plan-review-text').forEach((t) => { t.disabled = true; });
  };
  card._settle = settle;

  const send = (action) => {
    const edited = Array.from(list.querySelectorAll('.plan-review-text')).map((t) => ({
      id: t.dataset.id,
      text: t.value,
      status: t.dataset.status,
      milestone: t.dataset.milestone,
      verify: t.dataset.verify,
    }));
    const text = comment.value.trim();
    if (action === 'comment' && !text) {
      toast('先写一句要改什么，再让模型重排');
      comment.focus();
      return;
    }
    ok.disabled = true;
    re.disabled = true;
    api('/api/plan/review', {
      session_id: state.sessions.current,
      rid: card._rid,
      action: action,
      comment: text,
      steps: action === 'approve' ? edited : null,
    }).then((r) => {
      if (!r || !r.ok) {
        ok.disabled = false;
        re.disabled = false;
        toast((r && r.error) || '提交失败');
        return;
      }
      settle(action === 'approve' ? '已批准，开始执行…' : '已提交意见，等模型重排…',
             action === 'approve' ? 'approved' : 'replan');
    }).catch((e) => {
      ok.disabled = false;
      re.disabled = false;
      toast('提交失败：' + e);
    });
  };
  ok.onclick = () => send('approve');
  re.onclick = () => send('comment');

  if (row._planCard && row._planCard.parentNode === wrap) {
    wrap.insertBefore(card, row._planCard.nextSibling);
  } else {
    wrap.appendChild(card);
  }
  row._planReviewCard = card;
  try { requestAnimationFrame(fit); } catch (e) { fit(); }
  setTimeout(fit, 60);               // 字体 / 布局稳定后再量一次
  try { card.scrollIntoView({ block: 'nearest' }); } catch (e) { /* 老浏览器忽略 */ }
}

function planReviewDone(row, obj) {
  const card = row && row._planReviewCard;
  if (!card || typeof card._settle !== 'function') return;
  const a = String((obj && obj.action) || '');
  card._settle(a === 'comment' ? '已提交意见，等模型重排…' : '已批准，开始执行…',
               a === 'comment' ? 'replan' : 'approved');
}

/* ---------- 验收子代理判定卡（独立 AI 打分 + 证据 + 差距） ---------- */
function acceptanceReviewRender(row, report) {
  const wrap = row && row._wrap;
  if (!wrap || !report) return;
  const pass = !!report.passed;
  const crit = Array.isArray(report.criteria) ? report.criteria : [];
  const gaps = Array.isArray(report.gaps) ? report.gaps : [];
  const card = document.createElement('div');
  card.className = 'review-card ' + (pass ? 'pass' : 'fail');
  const critHtml = crit.map((c) => {
    const okc = !!c.passed;
    const ev = c.evidence ? '<span class="review-ev">' + escapeHtml(String(c.evidence)) + '</span>' : '';
    return '<div class="review-crit' + (okc ? '' : ' bad') + '">'
      + '<span class="review-mark">' + (okc ? '✓' : '✗') + '</span>'
      + '<span class="review-crit-text">' + escapeHtml(String(c.criterion || '')) + '</span>'
      + ev + '</div>';
  }).join('');
  const gapHtml = gaps.length
    ? '<div class="review-gaps"><b>差距</b><ul>'
      + gaps.map((g) => '<li>' + escapeHtml(String(g)) + '</li>').join('') + '</ul></div>'
    : '';
  card.innerHTML =
    '<div class="review-head">' +
      '<span class="review-ico">' + icon(pass ? 'check' : 'alert') + '</span>' +
      '<span class="review-verdict">' + (pass ? '验收通过' : '验收未通过') + '</span>' +
      '<span class="review-score">' + (Number(report.score) || 0) + ' 分</span>' +
      (report.model ? '<span class="review-model">' + escapeHtml(String(report.model)) + '</span>' : '') +
    '</div>' +
    (critHtml ? '<div class="review-crits">' + critHtml + '</div>' : '') +
    gapHtml +
    (report.next_action
      ? '<div class="review-next">下一步：' + escapeHtml(String(report.next_action)) + '</div>'
      : '');
  if (row._toolsBox && row._toolsBox.parentNode === wrap) {
    wrap.insertBefore(card, row._toolsBox.nextSibling);
  } else {
    wrap.appendChild(card);
  }
}

/* ---------- 工作流卡（run_workflow）：节点状态 + 暂停 / 继续 / 取消 ---------- */
const WF_MARK = { done: '✓', failed: '✗', skipped: '⏭', running: '●', pending: '○' };
const WF_STATUS = { running: '执行中', completed: '已完成', failed: '失败',
                    paused: '已暂停', cancelled: '已取消', blocked: '受阻' };

function wfNodeInfo(s) {
  const bits = [];
  if (s.ms) bits.push((s.ms / 1000).toFixed(1) + 's');
  if (s.tokens) bits.push(s.tokens + 'tok');
  if (s.status === 'failed' && s.error) bits.push(s.error);
  return bits.join(' · ');
}

function wfButton(text, onClick) {
  const b = document.createElement('button');
  b.className = 'wf-btn';
  b.type = 'button';
  b.textContent = text;
  b.addEventListener('click', (e) => { e.stopPropagation(); onClick(); });
  return b;
}

async function wfPost(action, id) {
  try {
    const r = await api('/api/workflow/' + action, { id: id });
    if (!r || !r.ok) toast('操作失败（' + action + '）');
  } catch (e) { toast('操作失败：' + e); }
}

function wfResume(id) {
  toast('继续执行工作流…');
  const p = streamChat({ workflow: id, content: '继续执行工作流 ' + id });
  if (p && p.catch) p.catch((e) => toast('继续失败：' + e));
}

function workflowRender(row, wf) {
  if (!row || !wf || !wf.id) return;
  let card = row._wfCard;
  if (!card) {
    card = document.createElement('div');
    card.className = 'wf-card';
    card.innerHTML = '<div class="wf-head"><span class="wf-title"></span>'
      + '<span class="wf-meta"></span></div>'
      + '<div class="wf-body"></div><div class="wf-actions"></div>';
    if (row._content && row._content.parentNode === row._wrap) {
      row._wrap.insertBefore(card, row._content);      // 工作流在正文上方
    } else if (row._wrap) {
      row._wrap.appendChild(card);
    }
    row._wfCard = card;
  }
  const steps = wf.steps || [];
  const done = steps.filter((s) => s.status === 'done').length;
  const title = card.querySelector('.wf-title');
  if (title) title.textContent = '工作流 · ' + (wf.name || wf.id);
  const meta = card.querySelector('.wf-meta');
  if (meta) {
    const tok = (wf.used && wf.used.tokens) ? ' · ' + wf.used.tokens + ' tok' : '';
    meta.textContent = (WF_STATUS[wf.status] || wf.status || '') + ' '
      + done + '/' + steps.length + tok;
  }
  const body = card.querySelector('.wf-body');
  if (body) {
    body.innerHTML = steps.map((s) =>
      '<div class="wf-node ' + escapeHtml(s.status || 'pending') + '">'
      + '<span class="wf-mark">' + (WF_MARK[s.status] || '○') + '</span>'
      + '<span class="wf-id">' + escapeHtml(s.id || '') + '</span>'
      + '<span class="wf-type">' + escapeHtml(s.type || '') + '</span>'
      + '<span class="wf-info">' + escapeHtml(wfNodeInfo(s)) + '</span>'
      + '</div>').join('');
  }
  const acts = card.querySelector('.wf-actions');
  if (acts) {
    acts.innerHTML = '';
    const st = wf.status || '';
    if (st === 'running') acts.appendChild(wfButton('暂停', () => wfPost('pause', wf.id)));
    if (st === 'paused' || st === 'blocked' || st === 'failed' || st === 'cancelled') {
      acts.appendChild(wfButton('继续', () => wfResume(wf.id)));
    }
    if (st === 'running' || st === 'paused') {
      acts.appendChild(wfButton('取消', () => wfPost('cancel', wf.id)));
    }
    acts.style.display = acts.children.length ? '' : 'none';
  }
  row._wfState = wf;
}

/* ---------- 回滚本轮 / 写入审批 ---------- */
async function turnUndo() {
  const s = curSession();
  if (!s) return;
  const pv = await fetchRollbackPreview({ session_id: s.id, scope: 'turn' });
  if (!(await rollbackConfirm({
    title: '回滚本轮所有写操作？',
    sub: '文件会恢复到本轮开始前的状态（对话不会回退）。',
    files: pv ? pv.files : [],
    empty: '本轮没有可回滚的文件改动。',
  }))) return;
  try {
    const r = await api('/api/agent/turn_undo', { session_id: s.id });
    if (!r || !r.ok) { toast((r && r.error) || '回滚失败'); return; }
    toast('已回滚 ' + r.done + ' 处改动' + (r.failed ? '，' + r.failed + ' 处已过期' : ''));
    if (state.uiMode === 'ide' && typeof loadIdeTree === 'function') loadIdeTree();
  } catch (e) { toast('回滚失败：' + e); }
}

// 写入 / 命令审批卡：后端在真正执行前挂起，等这里点「允许 / 拒绝」
// 「提问」是可选项：不点就不发请求、不花 token；解释由模型给出，不会执行任何操作
function buildApprovalCard(obj) {
  const info = TOOL_LABELS[obj.name] || [null, obj.name];
  const arg = (typeof toolArgText === 'function') ? toolArgText(obj.name, obj.args) : '';
  const reason = obj.reason ? String(obj.reason) : '';
  // 命令与浏览器操作不支持「本轮都允许」：放开一次等于交出任意代码执行权 / 任意站点访问权
  // apply_worktree 不是模型可调的工具（回写阶段专用），给它「本轮都允许」没有意义
  const canRemember = obj.name !== 'run_command' &&
    obj.name !== 'apply_worktree' &&
    !String(obj.name || '').startsWith('computer_');
  // 「始终允许」= 往 .gm/agent-rules.json 写一条最窄的规则；
  // 持久 shell / 浏览器桌面操作不提供（等于把任意命令或任意站点一次放开）
  const canAlways = canRemember && obj.name !== 'shell' &&
    obj.name !== 'lsp_rename';
  const card = document.createElement('div');
  card.className = 'approve-card';
  card.innerHTML =
    '<div class="approve-head"><span class="approve-ico">' + icon('alert') + '</span>' +
      '<span class="approve-title">等待确认：' + escapeHtml(info[1]) +
        (arg ? ' · ' + escapeHtml(arg) : '') + '</span></div>' +
    (reason ? '<div class="approve-reason">' + escapeHtml(reason) + '</div>' : '') +
    '<div class="approve-actions">' +
      '<button class="approve-ok" type="button">允许</button>' +
      (canRemember ? '<button class="approve-always" type="button">本轮都允许</button>' : '') +
      (canAlways ? '<button class="approve-always-rule" type="button" ' +
        'title="把这一条写进 .gm/agent-rules.json，以后同类操作不再询问">始终允许</button>' : '') +
      '<button class="approve-no" type="button">拒绝</button>' +
      '<button class="approve-ask-btn" type="button">提问</button>' +
    '</div>' +
    '<div class="approve-ask" hidden>' +
      '<div class="approve-ask-note">模型解释，未执行任何操作</div>' +
      '<div class="approve-qa"></div>' +
      '<div class="approve-ask-row">' +
        '<input class="approve-ask-input" type="text" maxlength="400" ' +
          'placeholder="例如：这条命令会改什么？为什么要跑它？">' +
        '<button class="approve-ask-send" type="button">发送</button>' +
      '</div>' +
    '</div>';
  const askBox = card.querySelector('.approve-ask');
  const askInput = card.querySelector('.approve-ask-input');
  const askSend = card.querySelector('.approve-ask-send');
  const qaBox = card.querySelector('.approve-qa');
  const qa = [];                        // 已问过的 [{q,a}]，追问时一并回传
  card.querySelector('.approve-ask-btn').onclick = () => {
    askBox.hidden = !askBox.hidden;
    if (!askBox.hidden) askInput.focus();
  };
  const addQA = (q, a) => {
    const item = document.createElement('div');
    item.className = 'approve-qa-item';
    const qEl = document.createElement('div');
    qEl.className = 'approve-q';
    qEl.textContent = q;
    const aEl = document.createElement('div');
    aEl.className = 'approve-a';
    aEl.textContent = a;
    item.appendChild(qEl);
    item.appendChild(aEl);
    qaBox.appendChild(item);
    return aEl;
  };
  const ask = () => {
    if (askSend.disabled) return;
    const q = askInput.value.trim();
    const shown = q || '这次操作要做什么？为什么需要？会不会改动已有内容？';
    const aEl = addQA(shown, '正在询问…');
    askSend.disabled = true;
    askInput.disabled = true;
    api('/api/agent/explain', { aid: obj.aid, question: q, history: qa })
      .then((r) => {
        const answer = (r && r.answer) || '';
        aEl.textContent = answer || '（无回答）';
        qa.push({ q: shown, a: answer });
        askInput.value = '';
      })
      .catch((e) => { aEl.textContent = '解释失败：' + (e && e.message ? e.message : e); })
      .finally(() => {
        // 卡片已被允许 / 拒绝时不再恢复输入，避免误以为审批还开着
        if (!card.classList.contains('allowed') && !card.classList.contains('denied')) {
          askSend.disabled = false;
          askInput.disabled = false;
          askInput.focus();
        }
      });
  };
  askSend.onclick = ask;
  askInput.onkeydown = (e) => {
    if (e.key === 'Enter' && !e.isComposing) { e.preventDefault(); ask(); }
  };
  const settle = (ok, remember, always) => {
    card.classList.add(ok ? 'allowed' : 'denied');
    const acts = card.querySelector('.approve-actions');
    if (acts) acts.remove();
    if (askBox) askBox.hidden = true;
    const t = card.querySelector('.approve-title');
    const head = ok
      ? (always ? '已允许（已写入项目规则）：' : (remember ? '已允许（本轮不再询问）：' : '已允许：'))
      : '已拒绝：';
    if (t) t.textContent = head + info[1] + (arg ? ' · ' + arg : '');
  };
  const send = (ok, remember, always) => {
    card.querySelectorAll('button').forEach((b) => { b.disabled = true; });
    api('/api/agent/approve', {
      session_id: state.sessions.current,
      aid: obj.aid, ok: ok, remember: !!remember, always: !!always,
    })
      .then((r) => {
        if (!r || !r.ok) {
          // 审批记录已经不存在（超时 / 被别的操作处理）：不能再显示「已允许」
          card.classList.add('denied');
          const acts = card.querySelector('.approve-actions');
          if (acts) acts.remove();
          if (askBox) askBox.hidden = true;
          const t = card.querySelector('.approve-title');
          if (t) t.textContent = '审批已失效：' + info[1] + (arg ? ' · ' + arg : '');
          return toast('这次审批已失效（可能已超时或被处理）');
        }
        settle(ok, remember, always && !!r.rule);
        if (always) toast(r.rule || '已允许（本次）');
      })
      .catch((e) => {
        // 网络失败不代表操作已被拒绝：恢复按钮，让用户重试
        card.querySelectorAll('button').forEach((b) => { b.disabled = false; });
        toast('确认失败：' + e);
      });
  };
  card.querySelector('.approve-ok').onclick = () => send(true, false, false);
  const alwaysBtn = card.querySelector('.approve-always');
  if (alwaysBtn) alwaysBtn.onclick = () => send(true, true, false);
  const alwaysRuleBtn = card.querySelector('.approve-always-rule');
  if (alwaysRuleBtn) alwaysRuleBtn.onclick = () => send(true, true, true);
  card.querySelector('.approve-no').onclick = () => send(false, false, false);
  return card;
}

/* collapse=true：结果默认收起，点这一行才展开（普通对话用；GAL 传 false 保持原样） */
function buildToolCard(name, args, collapse) {
  const gi = toolGitInfo(name, args);
  const info = gi ? ['branch', 'Git · ' + gi.sub] : (TOOL_LABELS[name] || ['gear', name]);
  const card = document.createElement('div');
  card.className = 'tool-card running'
    + (gi ? ' git-op' : '')
    + (gi && gi.risk ? ' git-risk' : '')
    + (collapse === false ? ' always-open' : '');
  // 完成事件（tool_done）不带 args，这里记一份，收尾时算 +/− 行数要用
  card._toolName = name;
  card._toolArgs = args;
  card.innerHTML =
    '<div class="tool-head">' +
      '<span class="tool-ico">' + icon(info[0]) + '</span>' +
      '<span class="tool-name">' + escapeHtml(info[1]) + '</span>' +
      (gi && gi.risk ? '<span class="tool-git-risk">高危</span>' : '') +
      '<span class="tool-arg">' + toolArgHtml(name, args) + '</span>' +
      '<span class="tool-meta"></span>' +
      '<span class="tool-state">执行中…</span>' +
      '<span class="tool-stat"></span>' +
      '<span class="tool-caret">' + icon('chevron') + '</span>' +
    '</div>' +
    '<pre class="tool-result"></pre>' +
    '<div class="tool-actions"></div>';
  if (gi) {
    const hint = document.createElement('div');
    hint.className = 'tool-git-hint';
    hint.textContent = gi.risk
      ? '高危 Git 操作：可能覆盖工作区。可在 IDE「源代码管理 → AI 操作」核对。'
      : '';
    if (hint.textContent) {
      const head = card.querySelector('.tool-head');
      if (head) head.insertAdjacentElement('afterend', hint);
    }
    const acts = card.querySelector('.tool-actions');
    if (acts) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'review-btn';
      btn.textContent = '查看 Git';
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        openGitPanel();
      });
      acts.appendChild(btn);
    }
  }
  const toolHead = card.querySelector('.tool-head');
  const toolToggle = () => {
    if (!card.classList.contains('has-result')) return;
    const next = !foldIsOpen(card, 'open', true);
    foldTransition(card, card.querySelector('.tool-result'), next, {
      cls: 'open', positive: true, ms: 180,
    });
  };
  toolHead.addEventListener('click', toolToggle);
  makeClickable(toolHead, toolToggle, '展开 / 收起工具结果');
  // 路径可点击：切到 IDE 模式并在应用内打开（stopPropagation 避免触发上面的折叠）
  card.querySelectorAll('.tool-path').forEach((a) => {
    a.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      openToolPath(a.dataset.path);
    });
  });
  return card;
}

function addWriteActions(card, obj) {
  const act = card.querySelector('.tool-actions');
  const token = obj && obj.undo;
  if (!act || !token) return;
  const settled = (state.ideSettledTokens || {})[token];
  const path = String(((obj.args || {}).path) || '');
  const btns = state.reviewCardBtns[token] = state.reviewCardBtns[token] || [];

  const view = document.createElement('button');
  if (path && typeof reviewOpenFor === 'function') {
    view.type = 'button';
    view.className = 'tool-undo';
    view.dataset.role = 'view';
    view.textContent = '查看变更';
    view.onclick = () => reviewOpenFor(path, token);
    if (settled) { view.disabled = true; view.textContent = '已处理'; }
    act.appendChild(view);
    btns.push(view);
  }

  const b = document.createElement('button');
  b.type = 'button';
  b.className = 'tool-undo';
  b.dataset.role = 'undo';
  b.textContent = '撤回';
  b.onclick = async () => {
    b.disabled = true;
    try {
      const r = await api('/api/agent/undo', { token: token });
      if (r && r.ok) {
        b.textContent = '已撤回';
        state.ideSettledTokens[token] = 'undone';
        if (typeof ideReviewSyncCards === 'function') ideReviewSyncCards([token], '已撤回');
        if (typeof reviewForgetToken === 'function') reviewForgetToken(token);
        toast('已撤回该次改动');
      } else { b.disabled = false; toast('撤回失败或已过期'); }
    } catch (e) { b.disabled = false; toast('撤回失败：' + e); }
  };
  if (settled) { b.disabled = true; b.textContent = settled === 'accepted' ? '已保留' : '已撤回'; }
  act.appendChild(b);
  btns.push(b);
}

/* ---------- 命令执行中的增量输出 ----------
   只保留最后 200 行，避免长日志把 DOM 撑爆；完整输出仍由工具完成时的
   result 覆盖（finishToolCard 会去掉 streaming 并把 pre 换成最终结果）。 */
const TOOL_OUTPUT_MAX_LINES = 200;

function appendToolOutput(card, text) {
  if (!card || !text) return;
  const pre = card.querySelector('.tool-result');
  if (!pre) return;
  const wasEmpty = !pre.textContent;
  // 用户往上翻看时不要强行拉回底部
  const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
  card.classList.add('streaming');
  const lines = (pre.textContent + String(text)).split('\n');
  const kept = lines.length > TOOL_OUTPUT_MAX_LINES
    ? lines.slice(lines.length - TOOL_OUTPUT_MAX_LINES) : lines;
  pre.textContent = kept.join('\n');
  if (wasEmpty || atBottom) pre.scrollTop = pre.scrollHeight;
}

function finishToolCard(card, obj) {
  obj = obj || {};
  card.classList.remove('running');
  card.classList.remove('streaming');
  unmarkProcessStep(card);                   // 当前步骤高亮：完成 / 失败即熄灭
  const ok = obj.ok !== false;                 // 旧记录没有 ok 字段 → 视为成功
  card.classList.add(ok ? 'ok' : 'failed');
  const st = card.querySelector('.tool-state');
  if (st) st.textContent = ok ? (obj.write ? '已写入' : '完成') : '失败';
  const meta = card.querySelector('.tool-meta');
  if (meta) {
    const bits = [];
    if (obj.ms != null) bits.push(fmtToolMs(obj.ms));
    if (parseInt(obj.lines, 10) > 1) bits.push(obj.lines + ' 行');
    meta.textContent = bits.join(' · ');
  }
  // 写文件的工具卡：右侧补上和「文件改动卡」一致的 +adds −dels（口径同 ideReviewCounts）
  const stat = card.querySelector('.tool-stat');
  if (stat) {
    // tool_done 事件不带 args：用建卡时记下的参数算增删行数
    const a = obj.args || card._toolArgs;
    const n = String(obj.name || card._toolName || '');
    const c = (obj.write && ok && a && typeof ideReviewCounts === 'function')
      ? ideReviewCounts(n, a) : null;
    if (c && (c.adds || c.dels)) {
      stat.innerHTML = '<b class="add">+' + c.adds + '</b> '
        + '<b class="del">−' + c.dels + '</b>';
    }
  }
  const pre = card.querySelector('.tool-result');
  if (pre && obj.result) {
    pre.textContent = obj.result;
    card.classList.add('has-result');
  }
  if (obj.undo) addWriteActions(card, obj);
}

/* ---------- 一轮的工具调用归成一个可折叠「步骤组」（Trae 式：一行一步骤） ---------- */
function toolGroup(row) {
  if (row._toolGroup) return row._toolGroup;
  const el = document.createElement('div');
  el.className = 'tool-group running';
  el.innerHTML =
    '<div class="tool-group-head">' +
      '<span class="tool-group-ico">' + icon('gear') + '</span>' +
      '<span class="tool-group-title">运行中…</span>' +
      '<span class="tool-caret">' + icon('chevron') + '</span>' +
    '</div>' +
    '<div class="tool-group-body"></div>';
  const gHead = el.querySelector('.tool-group-head');
  const g = { el: el, body: el.querySelector('.tool-group-body'),
              pending: 0, done: 0, ms: 0, failed: 0, kinds: {},
              current: null, userToggled: false, hTimer: null,
              row: row, step: row._toolStep || 0 };
  gHead.addEventListener('click', () => setToolGroupOpen(g, el.classList.contains('collapsed'), true));
  makeClickable(gHead, () => setToolGroupOpen(g, el.classList.contains('collapsed'), true),
                '展开 / 收起这组工具');
  // 有过程流就放进流里（与思考块按时间线排开），否则退回旧的末尾容器
  if (row._flow) row._flow.appendChild(el);
  else if (row._toolsBox) row._toolsBox.appendChild(el);
  else row._wrap.appendChild(el);
  row._toolGroup = g;
  return g;
}

/* 展开 / 收起：用实测高度做过渡 —— max-height 写死会在长组上截断，动画也会「先跳一半」 */
function setToolGroupOpen(g, open, byUser) {
  if (!g) return;
  const head = g.el.querySelector('.tool-group-head');
  clearTimeout(g.hTimer);
  if (open) {
    g.el.classList.remove('collapsed');
    g.body.style.maxHeight = g.body.scrollHeight + 'px';
    g.hTimer = setTimeout(() => {
      if (!g.el.classList.contains('collapsed')) g.body.style.maxHeight = '';
    }, 240);
  } else {
    g.body.style.maxHeight = g.body.scrollHeight + 'px';
    void g.body.offsetHeight;            // 先落一次高度，0 才有过渡起点
    g.body.style.maxHeight = '0px';
    g.el.classList.add('collapsed');
  }
  if (head) head.setAttribute('aria-expanded', open ? 'true' : 'false');
  if (byUser) g.userToggled = true;
}

// 本轮结束 / 历史回显：默认折成一行；用户手动展开过就不再自动收。
// 有失败的步骤不收：错误必须留在视线里（折叠起来用户就看不见问题了）。
function autoCollapseToolGroup(g) {
  if (!g || g.pending > 0 || g.userToggled || g.failed > 0) return;
  if (g.el.classList.contains('collapsed')) return;
  // 先落到实测高度再收：直接从 auto 跳到 0 没有过渡起点，看起来就是「啪」地消失
  g.body.style.maxHeight = g.body.scrollHeight + 'px';
  void g.body.offsetHeight;
  g.el.classList.add('collapsed');
  g.body.style.maxHeight = '0px';
  const head = g.el.querySelector('.tool-group-head');
  if (head) head.setAttribute('aria-expanded', 'false');
}

function toolGroupTitle(g) {
  const t = g.el.querySelector('.tool-group-title');
  const ico = g.el.querySelector('.tool-group-ico');
  if (!t) return;
  const running = g.pending > 0;
  g.el.classList.toggle('running', running);
  g.el.classList.toggle('failed', !running && g.failed > 0);
  // 状态图标：运行中齿轮转、完成打勾、失败警示
  if (ico) ico.innerHTML = icon(running ? 'gear' : (g.failed > 0 ? 'alert' : 'check'));
  if (running) {
    let s = '运行中 · 已完成 ' + g.done + ' 步';
    // 有计划时把当前步骤带上：用户能对上「现在在第几步」
    const steps = (g.row && g.row._planSteps) || [];
    if (steps.length) {
      const doing = steps.find((x) => x.status === 'doing');
      const idx = doing ? steps.indexOf(doing) + 1 : g.done;
      s = '步骤 ' + Math.min(idx, steps.length) + '/' + steps.length
        + ' · 已完成 ' + g.done + ' 步';
      if (doing && doing.text) {
        const t2 = String(doing.text);
        s += ' · ' + (t2.length > 18 ? t2.slice(0, 18) + '…' : t2);
      }
    }
    if (g.current && g.current.name) {
      const verb = TOOL_VERBS[g.current.name]
        || (TOOL_LABELS[g.current.name] || [null, '调用'])[1];
      let arg = String(g.current.arg || '');
      if (arg.length > 28) arg = arg.slice(0, 28) + '…';
      s += ' · 正在' + verb + (arg ? ' ' + arg : '');
    }
    // 存一份「基础文案」：stream.js 的每秒计时器在它后面追加「· 已 Ns」，
    // 不存的话每次刷新都会把上一次的秒数再叠一遍。
    g.el._titleBase = s;
    t.textContent = s;
    return;
  }
  const kinds = Object.keys(g.kinds)
    .sort((a, b) => g.kinds[b] - g.kinds[a])
    .slice(0, 3)
    .map((k) => {
      const n = g.kinds[k];
      const fmt = TOOL_SUMMARY[k];
      return fmt ? fmt(n) : ((TOOL_LABELS[k] || [null, k])[1] + ' ' + n + ' 次');
    })
    .join(' · ');
  let s = '已执行 ' + g.done + ' 个步骤';
  if (kinds) s += ' · ' + kinds;
  if (g.ms) s += ' · ' + fmtToolMs(g.ms);
  if (g.failed) s += ' · ' + g.failed + ' 个失败';
  g.el._titleBase = s;
  t.textContent = s;
  // 每完成一步都刷新过程折叠头的「N 步 / N 步失败」，收起时也能看出这一轮的规模
  if (typeof updateFlowMeta === 'function') updateFlowMeta(g.row);
}

/* 提问卡的工具函数：选项规范化 / 富文本 / 草稿持久化 */
function askOption(o) {
  if (o && typeof o === 'object') {
    return {
      label: String(o.label || o.text || ''),
      desc: String(o.desc || o.detail || ''),
      recommended: !!o.recommended,
    };
  }
  return { label: String(o == null ? '' : o), desc: '', recommended: false };
}

/* 题面 / 选项里的 Markdown（模型可能给方案说明、代码片段）；失败回落纯文本 */
function askRich(text) {
  const s = String(text == null ? '' : text);
  return typeof safeRenderMarkdown === 'function' ? safeRenderMarkdown(s) : escapeHtml(s);
}

const ASK_DRAFT_KEY = 'ask_drafts_v1';

function askDraftLoad(qid, count) {
  if (!qid || !count) return null;
  try {
    const all = JSON.parse(localStorage.getItem(ASK_DRAFT_KEY) || '{}') || {};
    const one = all[qid];
    if (!one || !Array.isArray(one.drafts) || one.drafts.length !== count) return null;
    const chosen = Array.isArray(one.chosen) ? one.chosen : [];
    return {
      drafts: one.drafts.map((x) => String(x || '')),
      chosen: Array.from({ length: count },
        (_v, i) => (Array.isArray(chosen[i]) ? chosen[i].map((x) => String(x)) : [])),
      cur: Math.max(0, Math.min(count - 1, Number(one.cur) || 0)),
    };
  } catch (e) { return null; }
}

function askDraftSave(qid, drafts, chosen, cur) {
  if (!qid) return;
  try {
    const all = JSON.parse(localStorage.getItem(ASK_DRAFT_KEY) || '{}') || {};
    all[qid] = { drafts: drafts.slice(), chosen: chosen.map((x) => x.slice()), cur: cur };
    const keys = Object.keys(all);
    if (keys.length > 20) keys.slice(0, keys.length - 20).forEach((k) => { delete all[k]; });
    localStorage.setItem(ASK_DRAFT_KEY, JSON.stringify(all));
  } catch (e) { /* 存不了就算了，不影响答题 */ }
}

function askDraftDrop(qid) {
  if (!qid) return;
  try {
    const all = JSON.parse(localStorage.getItem(ASK_DRAFT_KEY) || '{}') || {};
    delete all[qid];
    localStorage.setItem(ASK_DRAFT_KEY, JSON.stringify(all));
  } catch (e) { /* 忽略 */ }
}

/* 「等待用户操作」卡：这一步只能由用户在本机亲手完成（登录 / 扫码 / 装依赖 /
   点系统弹窗 / 插设备）。卡片只给「我已完成」「无法完成」两个出口，
   agent 在后台阻塞等待提交结果，点完再继续往下做。 */
function buildActionCard(list, qid, meta) {
  const card = document.createElement('div');
  card.className = 'ask-card act-card';
  const head = (txt) => '<div class="ask-head"><span class="ask-q">' +
    icon('check-square') + ' ' + txt + '</span></div>';
  const summary = (arr) => '<div class="ask-chosen-list">' + arr.map((q, i) =>
    '<div class="ask-chosen-item"><div class="ask-chosen-q">' + escapeHtml(q.question) + '</div>' +
    '<div class="ask-chosen-a">' + escapeHtml(arr[i].answer == null ? '（未回答）' : arr[i].answer) + '</div></div>'
  ).join('') + '</div>';
  const body = list.map((q) => {
    let h = '<div class="act-item"><div class="act-title">' + escapeHtml(q.question) + '</div>';
    if (q.detail) h += '<div class="act-detail">' + escapeHtml(q.detail) + '</div>';
    if (q.items.length) {
      h += '<ol class="act-steps">' +
        q.items.map((s) => '<li>' + escapeHtml(s) + '</li>').join('') + '</ol>';
    }
    return h + '</div>';
  }).join('');

  if (list.length && list.every((q) => q.answer != null)) {   // 历史回显：只读
    card.classList.add('done');
    card.innerHTML = head('需要你操作') + summary(list);
    return card;
  }
  card.innerHTML = head('需要你操作') + body +
    '<div class="act-actions">' +
      '<button type="button" class="act-done primary">我已完成，继续</button>' +
      '<button type="button" class="act-fail">无法完成</button>' +
    '</div>' +
    '<div class="ask-wait">⏳ 正在等你在本机完成这一步…<span class="ask-timer"></span></div>';

  const timerEl = card.querySelector('.ask-timer');
  const expiresAt = Number((meta && meta.expiresAt) || 0);
  let timer = 0;
  const stopTimer = () => { if (timer) { clearInterval(timer); timer = 0; } };
  const markExpired = () => {
    if (card.dataset.done) return;
    card.dataset.done = '1';
    stopTimer();
    card.classList.add('timed-out');
    card.querySelectorAll('.act-actions button').forEach((b) => { b.disabled = true; });
    const w = card.querySelector('.ask-wait');
    if (w) w.textContent = '已超时：AI 已按「未完成」继续，可以重新向它提问。';
  };
  const submit = async (answer) => {
    if (card.dataset.done) return;
    const arr = list.map((q) => ({ question: q.question, answer: answer }));
    if (qid) {
      try {
        await api('/api/agent/answer', { qid: qid, answers: arr.map((x) => x.answer) });
      } catch (e) {
        // 超时 / qid 失效：切成「已超时」；其它错误保持可点，允许重试
        if (/超时|取消/.test(String(e.message || ''))) markExpired();
        else toast('提交失败：' + e.message, 'error');
        return;
      }
    }
    card.dataset.done = '1';
    stopTimer();
    card.classList.add('done');
    card.innerHTML = head('需要你操作') + summary(arr);
    scrollToBottom(true);
  };
  if (qid && expiresAt > 0) {
    const tick = () => {
      const left = expiresAt - Date.now();
      if (left <= 0) { markExpired(); return; }
      const s = Math.ceil(left / 1000);
      if (timerEl) {
        timerEl.textContent = ' · 剩 ' + Math.floor(s / 60) + ':'
          + String(s % 60).padStart(2, '0');
      }
    };
    tick();
    timer = setInterval(tick, 1000);
  }
  card.querySelector('.act-done').onclick = () => submit('已完成');
  card.querySelector('.act-fail').onclick = () => submit('无法完成');
  return card;
}

/* 提问卡：一组问题（可翻页预览、自由来回、最后统一提交）
   qs : [{question, options[{label,desc,recommended}|string], multiple, answer?}]
        answer 非空 = 已作答（历史回显）
   qid: 后端提问 id（历史回显时为 null，此时不提交）
   meta: { expiresAt } 等待截止时间（毫秒）；没有则不显示倒计时 */
function buildAskGroup(qs, qid, meta) {
  meta = meta || {};
  const list = (qs || []).map((q) => ({
    question: String((q && q.question) || ''),
    options: ((q && q.options) || []).map(askOption).filter((o) => o.label.trim()),
    multiple: !!(q && q.multiple),
    answer: (q && q.answer != null) ? String(q.answer) : null,
    action: !!(q && q.action),
    detail: (q && q.detail != null) ? String(q.detail) : '',
    items: ((q && q.items) || []).map((x) => String(x)).filter((x) => x.trim()),
  }));
  // 整组都是「等我操作」就换成操作卡（它不是选择题，没有选项与翻页）
  if (list.length && list.every((q) => q.action)) return buildActionCard(list, qid, meta);
  const multi = list.length > 1;
  // Solo 模式：底部按钮逐题推进（下一个问题），到最后一题才变回「提交全部」。
  // 其他模式保持原来的「随时提交全部」。
  const solo = (typeof state !== 'undefined' && state.uiMode === 'solo');
  const card = document.createElement('div');
  card.className = 'ask-card';

  // 已作答：只读展示「问题 → 回答」
  const doneHtml = (arr) =>
    '<div class="ask-q">' + icon('question') + ' 提问' + (multi ? '（' + arr.length + ' 个）' : '') + '</div>' +
    '<div class="ask-chosen-list">' + arr.map((q) =>
      '<div class="ask-chosen-item"><div class="ask-chosen-q">' + askRich(q.question) + '</div>' +
      '<div class="ask-chosen-a">' + escapeHtml(q.answer == null ? '（未回答）' : q.answer) + '</div></div>').join('') +
    '</div>';

  // 超时 / 未回答的历史卡：给一个「重新提问」出口（只填进输入框，不自动发送）
  function addRetryLink() {
    if (card.querySelector('.ask-retry')) return;
    const pending = list.filter((q) => q.answer == null
      || q.answer === '（未回答）' || q.answer === '（由 AI 自行决定）');
    if (!pending.length) return;
    const link = document.createElement('button');
    link.type = 'button';
    link.className = 'ask-link ask-retry';
    link.textContent = '重新提问';
    link.onclick = () => {
      const text = '刚才这些问题没有回答，请重新用提问卡问我：\n'
        + pending.map((q, i) => (i + 1) + '. ' + q.question).join('\n');
      const input = $('#input');
      if (!input) return;
      input.value = text;
      input.dispatchEvent(new Event('input', { bubbles: true }));
      if (typeof autoGrow === 'function') autoGrow();
      input.focus();
      toast('已填入输入框，确认后发送');
    };
    card.appendChild(link);
  }

  if (list.length && list.every((q) => q.answer != null)) {
    card.classList.add('done');
    card.innerHTML = doneHtml(list);
    addRetryLink();
    return card;
  }

  card.innerHTML =
    '<div class="ask-head"><span class="ask-q">' + icon('question') + ' 提问' + (multi ? '（' + list.length + ' 个）' : '') + '</span>' +
      '<span class="ask-head-right">' +
        '<span class="ask-timer"></span>' +
        '<span class="ask-pager">' +
          '<button type="button" class="ask-nav ask-prev" title="上一个">‹</button>' +
          '<span class="ask-idx">1 / ' + (list.length || 1) + '</span>' +
          '<button type="button" class="ask-nav ask-next" title="下一个">›</button>' +
        '</span>' +
      '</span></div>' +
    '<div class="ask-q-text"></div>' +
    '<div class="ask-opts"></div>' +
    '<div class="ask-input-row"><input class="ask-input field-input" placeholder="也可以自己输入回答…">' +
      '<button type="button" class="ask-send primary">提交</button></div>' +
    '<div class="ask-wait-row"><span class="ask-wait">⏳ 正在等待你的回复…（可用下方输入框或语音）</span>' +
      (qid ? '<button type="button" class="ask-link ask-dismiss" title="不等了，让 AI 按最稳妥的方案继续">让 AI 自己决定</button>' : '') +
    '</div>' +
    '<div class="ask-foot">' +
      '<button type="button" class="ask-prev-step" disabled>上一个问题</button>' +
      '<span class="ask-progress"></span>' +
      '<button type="button" class="ask-submit primary">提交全部</button></div>';
  bindAskInputSync(card);        // 单向同步：底部输入框内容（含语音识别结果）会填进卡片

  const qText = card.querySelector('.ask-q-text');
  const optsBox = card.querySelector('.ask-opts');
  const input = card.querySelector('.ask-input');
  const send = card.querySelector('.ask-send');
  const prev = card.querySelector('.ask-prev');
  const next = card.querySelector('.ask-next');
  const idxEl = card.querySelector('.ask-idx');
  const progress = card.querySelector('.ask-progress');
  const submitAll = card.querySelector('.ask-submit');
  const prevStep = card.querySelector('.ask-prev-step');
  const timerEl = card.querySelector('.ask-timer');
  if (!multi) {                                   // 单问：隐藏翻页与「提交全部」，回到原来的轻量样式
    card.querySelector('.ask-pager').style.display = 'none';
    card.querySelector('.ask-foot').style.display = 'none';
  } else {
    send.textContent = '记录';
    // 只给 Solo 加「上一个问题」；其他模式保留原来的翻页按钮
    if (!solo) prevStep.style.display = 'none';
  }

  // 选项答案与手写答案分开存：选选项不再把文字塞进输入框，
  // 翻到下一题时输入框只显示该题自己的手写内容，不会残留上一题的选项。
  // chosen 统一用数组：多选存多个，单选最多一个。刷新后从 localStorage 恢复。
  const restored = askDraftLoad(qid, list.length);
  const drafts = restored ? restored.drafts : list.map(() => '');
  const chosen = restored ? restored.chosen : list.map(() => []);
  let cur = restored ? restored.cur : 0;
  let expired = false;
  let timer = 0;

  const persist = () => askDraftSave(qid, drafts, chosen, cur);
  const stopTimer = () => { if (timer) { clearInterval(timer); timer = 0; } };
  // 选项与手写内容可以并存：两边都有时一起提交（「选项A、选项B；手写补充」）。
  const answerOf = (i) => {
    const picked = chosen[i].join('、');
    const typed = (drafts[i] || '').trim();
    if (picked && typed) return picked + '；' + typed;
    return picked || typed;
  };
  const answered = () => list.filter((_q, i) => !!answerOf(i)).length;

  function render() {
    const q = list[cur] || { question: '', options: [] };
    qText.innerHTML = askRich(q.question);
    optsBox.innerHTML = '';
    const picked = chosen[cur];
    q.options.forEach((o) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'ask-opt' + (picked.indexOf(o.label) >= 0 ? ' sel' : '')
        + (o.desc ? ' with-desc' : '') + (o.recommended ? ' rec' : '');
      b.innerHTML = '<span class="ask-opt-label">' + askRich(o.label) + '</span>' +
        (o.recommended ? '<span class="ask-opt-badge">推荐</span>' : '') +
        (o.desc ? '<span class="ask-opt-desc">' + askRich(o.desc) + '</span>' : '');
      b.title = o.label;
      b.onclick = () => {
        if (card.dataset.done || expired) return;
        // 保留手写内容：选中气泡与输入框文字可以并存，提交时一起带上。
        const arr = chosen[cur];
        if (q.multiple) {
          // 多选：点气泡只切换选中，选完由「提交」统一定稿
          const at = arr.indexOf(o.label);
          if (at >= 0) arr.splice(at, 1);
          else arr.push(o.label);
          persist();
          render();
          return;
        }
        // 单选也支持「再点一次取消」：不再点一下就直接提交，
        // 统一由「提交」按钮 / Enter 定稿，否则没有第二次点击的机会。
        const at = chosen[cur].indexOf(o.label);
        chosen[cur] = at >= 0 ? [] : [o.label];
        persist();
        render();
      };
      optsBox.appendChild(b);
    });
    input.value = drafts[cur] || '';
    input.placeholder = q.multiple
      ? '可多选；再点已选项可取消，选好后点「提交」'
      : '点选项选中 / 再点取消，也可以自己输入，最后点「提交」';
    idxEl.textContent = (cur + 1) + ' / ' + list.length;
    prev.disabled = cur <= 0;
    next.disabled = cur >= list.length - 1;
    if (prevStep) prevStep.disabled = cur <= 0;
    progress.textContent = '已答 ' + answered() + '/' + list.length;
    if (multi && solo) {
      submitAll.textContent = cur >= list.length - 1 ? '提交全部' : '下一个问题';
    }
  }

  // 后端已经不等了：把卡片锁成「已超时」，别再让用户以为还能提交
  function markExpired() {
    if (expired) return;
    expired = true;
    stopTimer();
    card.dataset.done = '1';
    card.classList.add('timed-out');
    [prev, next, send, submitAll, input].forEach((el) => { if (el) el.disabled = true; });
    if (prevStep) prevStep.disabled = true;
    optsBox.querySelectorAll('button').forEach((b) => { b.disabled = true; });
    const dismiss = card.querySelector('.ask-dismiss');
    if (dismiss) dismiss.remove();
    const waitEl = card.querySelector('.ask-wait');
    if (waitEl) {
      waitEl.textContent = '已超时：AI 已按「未回答」继续。需要的话可以让它重新提问。';
    }
    if (timerEl) { timerEl.textContent = '已超时'; timerEl.classList.add('urgent'); }
    addRetryLink();
    askDraftDrop(qid);
  }

  async function submit(answers) {
    if (card.dataset.done) return;
    if (expired) { toast('这条提问已超时，请让 AI 重新提问', 'error'); return; }
    const answers2 = list.map((q, i) => ({ question: q.question,
      answer: ((answers[i] || '').trim() || '（未回答）') }));
    if (qid) {
      try {
        // 先提交再改 UI：失败时卡片保持可操作，允许重试
        await api('/api/agent/answer', { qid: qid, answers: answers });
      } catch (e) {
        if (/超时|取消/.test(String(e.message || ''))) markExpired();
        else toast('回答提交失败：' + e.message, 'error');
        return;
      }
    }
    card.dataset.done = '1';
    stopTimer();
    askDraftDrop(qid);
    card.classList.add('done');
    card.innerHTML = doneHtml(answers2);          // 换成只读汇总
    // 提问期间底部输入框可能被同步填过内容 → 提交后清掉，避免残留
    const mainInput = $('#input');
    if (mainInput && mainInput.value) { mainInput.value = ''; autoGrow(); hideSttChip(); }
    scrollToBottom(true);
  }

  const saveDraft = () => {
    drafts[cur] = input.value.trim();   // 手写内容与已选项并存，不再互相清除
    persist();
  };
  const resetSync = () => { if (typeof card._askSyncReset === 'function') card._askSyncReset(); };
  const step = (d) => {
    saveDraft();
    cur = Math.max(0, Math.min(list.length - 1, cur + d));
    persist();
    render();
    resetSync();
    scrollToBottom();
  };

  // 「让 AI 自己决定」：唤醒后端等待线程，按最稳妥的方案继续
  const dismissBtn = card.querySelector('.ask-dismiss');
  if (dismissBtn) {
    dismissBtn.onclick = async () => {
      if (card.dataset.done || expired) return;
      dismissBtn.disabled = true;
      try {
        await api('/api/agent/ask/dismiss', { qid: qid });
      } catch (e) {
        dismissBtn.disabled = false;
        if (/超时|取消/.test(String(e.message || ''))) markExpired();
        else toast('操作失败：' + e.message, 'error');
        return;
      }
      const arr = list.map((q) => ({ question: q.question, answer: '（由 AI 自行决定）' }));
      card.dataset.done = '1';
      stopTimer();
      askDraftDrop(qid);
      card.classList.add('done');
      card.innerHTML = doneHtml(arr);
      toast('已交给 AI 决定', 'success');
      scrollToBottom(true);
    };
  }

  prev.onclick = () => step(-1);
  next.onclick = () => step(1);
  if (prevStep) prevStep.onclick = () => step(-1);
  send.onclick = () => {
    if (!multi) {
      // 单问：手写内容与所选选项都带上
      submit([answerOf(0)]);
      return;
    }
    saveDraft();
    if (cur < list.length - 1) { cur += 1; persist(); render(); resetSync(); scrollToBottom(); }
  };
  input.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' || isImeComposing(e)) return;
    e.preventDefault();
    send.onclick();
  });
  input.addEventListener('input', () => {
    // 手写内容与已选气泡并存；drafts 在所有模式下都要记录，
    // 否则单问多选点气泡触发 render() 时会把刚输入的文字回填丢掉。
    drafts[cur] = input.value.trim();
    persist();
    if (!multi) return;
    progress.textContent = '已答 ' + answered() + '/' + list.length;
  });
  submitAll.onclick = () => {
    saveDraft();
    if (solo && multi && cur < list.length - 1) {   // Solo：先逐题往下走
      cur += 1;
      persist();
      render();
      resetSync();
      scrollToBottom();
      return;
    }
    submit(list.map((_q, i) => answerOf(i)));
  };

  // 等待倒计时：只在「后端还在等回答」的卡片上出现
  const expiresAt = Number(meta.expiresAt || 0);
  if (qid && expiresAt > 0) {
    const tick = () => {
      const left = expiresAt - Date.now();
      if (left <= 0) { markExpired(); return; }
      const s = Math.ceil(left / 1000);
      if (timerEl) {
        timerEl.textContent = '剩 ' + Math.floor(s / 60) + ':'
          + String(s % 60).padStart(2, '0');
        timerEl.classList.toggle('urgent', left <= 60000);
      }
    };
    tick();
    timer = setInterval(tick, 1000);
  }

  render();
  return card;
}

/* 提问卡 ← 底部主输入框：单向同步
   目的：提问期间可以直接用主输入框把话写进来（尤其「语音输入」），再点卡片上的「提交」。
   用轮询而不是只听 input 事件：语音识别 / 预设回填 / GAL 选项都是直接给 value 赋值，
   监听事件会全部漏掉。
   只做「主输入框 → 卡片」单方向：卡片里打字不再反向写回底部输入框。此前是双向同步，
   两个框内容会互相复制（看起来像"镜像"），且提交后底部输入框残留同步过来的文字。 */
function bindAskInputSync(card) {
  const ai = card.querySelector('.ask-input');
  const main = $('#input');
  if (!ai || !main) return;
  let last = main.value;
  // 翻题时把基线对齐到主输入框当前内容，避免上一题残留的文字被同步进新题
  card._askSyncReset = () => { last = main.value; };
  const pull = () => {
    // 已提交（卡片被替换）→ 停止同步
    if (ai.disabled || !document.body.contains(ai)) { clearInterval(timer); return; }
    if (main.value !== last) {
      last = main.value;
      ai.value = main.value;
      ai.dispatchEvent(new Event('input', { bubbles: true }));
    }
  };
  const timer = setInterval(pull, 350);
}

// 历史回显：把 assistant 消息里保存的工具记录渲染出来
function renderToolLog(wrap, tools) {
  if (!tools || !tools.length) return;
  const box = document.createElement('div');
  box.className = 'tool-list';
  const row = { _toolsBox: box };      // 复用「步骤组」的归组逻辑
  tools.forEach((t) => {
    if (t.tool === 'ask_user') {
      const asks = Array.isArray(t.asks) ? t.asks
                 : [{ question: t.question, options: t.options, answer: t.answer }];
      box.appendChild(buildAskGroup(asks, null));
      return;
    }
    if (isBgExploreTool(t)) return;    // 后台批次单独成卡（下面统一渲染），不进步骤折叠
    const card = buildToolCard(t.tool, t.args);
    finishToolCard(card, { name: t.tool, args: t.args, result: t.result, undo: t.undo,
                           write: t.write, ok: t.ok, ms: t.ms, lines: t.lines });
    const g = toolGroup(row);
    g.body.appendChild(card);
    g.done++;
    g.ms += parseInt(t.ms, 10) || 0;
    g.kinds[t.tool] = (g.kinds[t.tool] || 0) + 1;
    if (t.ok === false) g.failed++;
  });
  if (row._toolGroup) {
    toolGroupTitle(row._toolGroup);
    autoCollapseToolGroup(row._toolGroup);      // 历史回显默认折成一行
  }
  wrap.appendChild(box);
  appendExploreBatchCards(wrap, tools);
  appendDiagramCards(wrap, tools);
}

/* ---------------- archify 图卡（对话内嵌可交互 HTML） ----------------
   非 Agent 模式也能画图：后端把产物写到 data/archify/<sid>/，经 /archify/ 路由
   以 iframe 内嵌（免 X-Auth-Token，仅本机可访问）；高度由注入脚本 postMessage 上报。 */
const DIAGRAM_TITLES = {
  architecture: '架构图', workflow: '流程图', sequence: '时序图', dataflow: '数据流',
  lifecycle: '状态图', erd: 'ERD', tree: '树状图', class: '类图',
  timeline: '时间线', waterfall: '瀑布图', explainer: '一页式解释',
};

function buildDiagramCard(item) {
  const box = document.createElement('div');
  box.className = 'diagram-card';
  box.innerHTML = '<div class="diagram-head">'
    + '<span class="diagram-title"></span>'
    + '<span class="diagram-sub">可交互</span>'
    + '<button type="button" class="diagram-open">新窗口打开</button>'
    + '</div>'
    + '<div class="diagram-body"><iframe class="diagram-frame" loading="lazy"'
    + ' sandbox="allow-scripts allow-popups allow-forms"></iframe></div>';
  const kind = String((item && item.type) || '');
  box.querySelector('.diagram-title').textContent = DIAGRAM_TITLES[kind] || '图';
  const frame = box.querySelector('.diagram-frame');
  frame.title = (DIAGRAM_TITLES[kind] || '图') + '预览';
  if (item && item.url) frame.src = item.url;
  box.querySelector('.diagram-open').onclick = () => {
    if (item && item.url) window.open(item.url, '_blank');
  };
  return box;
}

/* 历史回显：把 tool_log 里带 diagram 的步骤还原成图卡 */
function appendDiagramCards(wrap, tools) {
  if (!wrap) return;
  (tools || []).forEach((t) => {
    const d = t && t.diagram;
    if (!d || !d.url) return;
    let dup = false;
    wrap.querySelectorAll('.diagram-card').forEach((n) => {
      if (n.dataset.url === d.url) dup = true;
    });
    if (dup) return;
    const card = buildDiagramCard(d);
    card.dataset.url = d.url;
    wrap.appendChild(card);
  });
}

// 子页面（我们自己的 /archify/ 路由）上报自身高度 → 调整 iframe 高度
window.addEventListener('message', (e) => {
  const d = e.data;
  if (!d || d.__gmDiagram !== 1) return;
  const h = Math.max(240, Math.min(1400, Number(d.height) || 0));
  document.querySelectorAll('.diagram-frame').forEach((f) => {
    try {
      if (f.contentWindow === e.source) f.style.height = h + 'px';
    } catch (err) { /* 拿不到 contentWindow：忽略 */ }
  });
});

/* ---------------- 后台探索批次（explore background=true） ----------------
   后台线程在回合结束后仍会继续跑，但那时 SSE 已经关闭，事件到不了前端；
   这里按批次号轮询 /api/explore/status，把「跑没跑、跑到哪、结论是什么」
   显示成一张常驻卡片，不放进「过程」折叠里。 */
const EXPLORE_POLLS = {};      // batch -> setTimeout id

function isBgExploreTool(t) {
  return !!(t && t.tool === 'explore' && t.args && t.args.background && t.args.batch);
}

function exploreCardEls(batch) {
  return Array.prototype.slice.call(
    document.querySelectorAll('.explore-batch-card[data-batch="' + batch + '"]'));
}

function exploreRoleLabel(role) {
  return ({ searcher: '侦察', reader: '阅读', finder: '定位', worker: '执行',
    critic: '复核', researcher: '调研', reviewer: '审查' })[role] || (role || '子代理');
}

function appendExploreBatchCards(wrap, tools) {
  if (!wrap) return;
  (tools || []).forEach((t) => {
    if (!isBgExploreTool(t)) return;
    const batch = String(t.args.batch);
    if (wrap.querySelector('.explore-batch-card[data-batch="' + batch + '"]')) return;
    wrap.appendChild(buildExploreBatchCard(batch, t.args));
  });
}

function buildExploreBatchCard(batch, args) {
  const card = document.createElement('div');
  card.className = 'explore-batch-card';
  card.dataset.batch = batch;
  const goal = String((args && args.goal) || '').trim();
  const n = Number((args && args.questions) || 0) || 0;
  card.innerHTML =
    '<div class="explore-batch-head">' +
      '<span class="explore-batch-dot"></span>' +
      '<span class="explore-batch-title">后台探索' + (n ? ' · ' + n + ' 个问题' : '') + '</span>' +
      '<span class="explore-batch-sum">启动中…</span>' +
      '<button type="button" class="ghost explore-batch-cancel">取消</button>' +
    '</div>' +
    (goal ? '<div class="explore-batch-goal"></div>' : '') +
    '<div class="explore-batch-rows"></div>' +
    '<details class="explore-batch-result" hidden><summary>查看结论</summary>' +
      '<div class="explore-batch-text"></div></details>';
  if (goal) card.querySelector('.explore-batch-goal').textContent = goal;
  const cancel = card.querySelector('.explore-batch-cancel');
  cancel.onclick = async () => {
    cancel.disabled = true;
    try { await api('/api/explore/cancel', { batch }); }
    catch (e) { toast(e.message, 'err'); }
    cancel.disabled = false;
  };
  startExplorePoll(batch);
  return card;
}

function applyExploreStatus(batch, r) {
  const cards = exploreCardEls(batch);
  if (!cards.length) {          // 卡片已经随消息重绘消失：停表
    if (EXPLORE_POLLS[batch]) { clearTimeout(EXPLORE_POLLS[batch]); delete EXPLORE_POLLS[batch]; }
    return;
  }
  const ok = !!(r && r.ok);
  const st = ok ? (r.status || 'running') : 'unknown';
  const total = ok ? (Number(r.total) || 0) : 0;
  const done = ok ? (Number(r.done) || 0) : 0;
  const ms = ok ? (Number(r.ms) || 0) : 0;
  const stText = ({ running: '运行中', done: '已完成', failed: '失败', cancelled: '已取消' })[st] || st;
  cards.forEach((card) => {
    const sum = card.querySelector('.explore-batch-sum');
    if (sum) {
      sum.textContent = ok
        ? (st === 'running'
            ? ('运行中 ' + done + '/' + total + ' · ' + (ms / 1000).toFixed(0) + 's')
            : (stText + (ms ? ' · ' + (ms / 1000).toFixed(1) + 's' : '')))
        : ('状态不可用' + ((r && r.error) ? ('：' + r.error) : ''));
    }
    const dot = card.querySelector('.explore-batch-dot');
    if (dot) {
      dot.className = 'explore-batch-dot ' +
        (st === 'running' ? 'running' : st === 'done' ? 'done' : st === 'failed' ? 'fail' : 'muted');
    }
    const cancel = card.querySelector('.explore-batch-cancel');
    if (cancel) cancel.classList.toggle('hidden', st !== 'running');
    const rows = card.querySelector('.explore-batch-rows');
    if (!rows || !ok) return;
    const items = (r.partial || []).slice(-10);
    rows.innerHTML = items.map((x) =>
      '<div class="explore-batch-row ' + (x.ok ? 'done' : 'fail') + '">' +
        '<span class="explore-batch-role">' + escapeHtml(exploreRoleLabel(x.role)) + '</span>' +
        '<span class="explore-batch-preview">' +
          escapeHtml(String(x.text || '').replace(/\s+/g, ' ').slice(0, 100)) + '</span>' +
      '</div>').join('') +
      (st === 'running'
        ? '<div class="explore-batch-row running">' +
            '<span class="explore-batch-role">等待</span>' +
            '<span class="explore-batch-preview">还有 ' + Math.max(0, total - done) +
            ' 个问题未回传</span></div>'
        : '');
  });
}

async function loadExploreResult(batch) {
  let r = null;
  try { r = await api('/api/explore/result', { batch }); }
  catch (e) { return; }
  if (!r || !r.ok || !r.text) return;
  exploreCardEls(batch).forEach((card) => {
    const box = card.querySelector('.explore-batch-result');
    const text = card.querySelector('.explore-batch-text');
    if (!box || !text) return;
    text.textContent = r.text;
    box.hidden = false;
  });
}

function startExplorePoll(batch) {
  if (EXPLORE_POLLS[batch]) return;
  const step = async () => {
    delete EXPLORE_POLLS[batch];
    let r = null;
    try { r = await api('/api/explore/status', { batch }); }
    catch (e) { r = { ok: false, error: e.message }; }
    applyExploreStatus(batch, r);
    if (r && r.ok && r.status === 'running' && exploreCardEls(batch).length) {
      EXPLORE_POLLS[batch] = setTimeout(step, 2500);
    } else if (r && r.ok) {
      loadExploreResult(batch);
    }
  };
  EXPLORE_POLLS[batch] = setTimeout(step, 800);
}

function buildAiRow(contentHtml, rawText, index, reasoning, sources, usage, memory, tools, plan) {
  const row = document.createElement('div');
  row.className = 'msg ai';
  if (typeof index === 'number') row.dataset.idx = index;
  const av = document.createElement('div');
  av.className = 'avatar';
  av.textContent = '✦';
  const wrap = document.createElement('div');
  wrap.className = 'content-wrap';
  if (sources && sources.length) wrap.appendChild(buildSourcesBlock(sources, false));
  if (reasoning) wrap.appendChild(buildThinkBlock(reasoning, false));
  let planCard = null;
  if (plan && plan.steps && plan.steps.length) {
    planCard = buildPlanCard(plan, planAllDone(plan));
    wrap.appendChild(planCard);
  }
  const c = document.createElement('div');
  c.className = 'content';
  c.innerHTML = contentHtml;
  enhanceToc(c);
  wrap.appendChild(c);
  renderToolLog(wrap, tools);
  row.appendChild(av);
  row.appendChild(wrap);
  row._content = c;
  row._wrap = wrap;
  row._raw = rawText || '';
  // 历史行也要挂上：否则「继续输出 / 断线」时 planRender 找不到旧卡，会再建一张。
  row._planCard = planCard;
  row._plan = planCard ? plan : null;
  row._planSteps = planCard ? plan.steps : [];
  if (memory) wrap.appendChild(buildMemoryChip(memory, index));
  if (rawText) wrap.appendChild(buildActions(row, () => row._raw, index, 'ai'));
  if (usage) {
    const u = document.createElement('div');
    u.className = 'usage-line';
    setUsage(u, usage);
    wrap.appendChild(u);
    row._usageEl = u;
  }
  return row;
}

/* 收尾工具：模型写完最终答案后用它声明完成。判断「正文是不是过场旁白」时必须跳过，
   否则答案会被折进过程区。别名与后端 groups.py 的归一化保持一致。 */
const FINISH_TOOL_NAMES = new Set(['finish', 'done', 'complete', 'finished',
                                   'task_done', 'task_complete', 'finish_task']);

/* 「实质交付」判定（与后端 app/api/chat.py::_is_substantive_answer 同一口径）：
   长度 ≥ 120 字，或带 Markdown 结构（标题 / 代码块 / 表格）。
   用来区分「最终交付」和「先查一下 / 现在运行脚本」这类过程旁白 —— 只按
   「谁在后面」判定会把最后一句旁白当成答案（实测 670 条消息里有 128 条）。 */
const ANSWER_MIN_CHARS = 120;
function isSubstantiveAnswer(text) {
  const t = String(text || '').trim();
  if (t.length >= ANSWER_MIN_CHARS) return true;
  return /(^|\n)[ \t]{0,3}(#{1,6}\s|```|\|)/.test(t);
}

/* 把 blocks 还原成一条消息的结构：
   - 折叠区（process-fold）：思考 + 工具组，以及**夹在工具之间的过场旁白**；
   - 正文段（content）：按落库的 `ans` 标记（旧消息走同一套「实质交付」启发式）；
   - 提问卡：落在它被问出来的位置，而不是一律堆到消息末尾。
   返回 { fold, parts }，parts 是可以直接追加进 .content-wrap 的节点序列。 */
function buildBlocksView(blocks, timing, endKind, opts) {
  const options = opts || {};
  const list = blocks || [];
  const flow = document.createElement('div');
  flow.className = 'chat-flow';
  const ctx = { _flow: flow, _toolGroup: null };
  const finalizeGroup = () => {
    if (!ctx._toolGroup) return;
    toolGroupTitle(ctx._toolGroup);
    autoCollapseToolGroup(ctx._toolGroup);
    ctx._toolGroup = null;
  };
  // 最后一次收尾工具（finish / done …）的位置：旧消息的启发式要用它分「交付前 / 交付后」。
  let finishAt = -1;
  list.forEach((b, i) => {
    if (!b || typeof b !== 'object' || b.kind !== 'tool') return;
    const t = (options.tools || [])[b.i];
    if (t && FINISH_TOOL_NAMES.has(String(t.tool || ''))) finishAt = i;
  });
  // 答案块：服务端落库时会打 `ans` 标记；旧会话没有标记，用同一套「实质交付」启发式。
  // 旧数据里也可能有「把一句旁白标成答案」的历史标记（新规则之前的落库），
  // 所以只有标记本身是实质交付（或这一轮被停止）时才信它，否则按启发式重算。
  const partialEnd = endKind === 'stopped' || endKind === 'billing' || endKind === 'budget';
  const ansIds = [];
  list.forEach((b, i) => {
    if (b && b.kind === 'text' && b.ans === true) ansIds.push(i);
  });
  const marked = ansIds.length > 0
    && (partialEnd || ansIds.some((i) => isSubstantiveAnswer(list[i].text)));
  const answerIds = new Set();
  if (marked) {
    // 多个标记（旧数据）：取最后一个实质交付；都没有就取最后一个标记。
    let pick = -1;
    ansIds.forEach((i) => {
      if (isSubstantiveAnswer(list[i].text)) pick = i;
    });
    if (pick < 0) pick = ansIds[ansIds.length - 1];
    answerIds.add(pick);
  } else if (partialEnd) {
    // 暂停语义（对齐 Trae：停止只是暂停，已输出内容保留）：
    // 最后一段可见正文留在答案区，由 end_note「你手动停止了本轮」标注为未完成。
    for (let i = list.length - 1; i >= 0; i -= 1) {
      const b = list[i];
      if (b && b.kind === 'text' && String(b.text || '').trim()) {
        answerIds.add(i);
        break;
      }
    }
  } else {
    const textIds = [];
    list.forEach((b, i) => {
      if (b && b.kind === 'text' && String(b.text || '').trim()) textIds.push(i);
    });
    const substantive = (i) => isSubstantiveAnswer(list[i].text);
    const lastSub = (ids) => {
      for (let k = ids.length - 1; k >= 0; k -= 1) {
        if (substantive(ids[k])) return ids[k];
      }
      return -1;
    };
    let pick = -1;
    if (finishAt < 0) {
      pick = lastSub(textIds);
      if (pick < 0 && textIds.length) pick = textIds[textIds.length - 1];
    } else {
      const before = textIds.filter((i) => i < finishAt);
      const after = textIds.filter((i) => i > finishAt);
      // 紧邻 finish 的正文 = 最常见形态；只有它太短（旁白）时才往后 / 往前找。
      let adj = -1;
      const b = finishAt > 0 ? list[finishAt - 1] : null;
      if (b && b.kind === 'text' && String(b.text || '').trim()) adj = finishAt - 1;
      const post = lastSub(after);
      const prev = lastSub(before);
      if (adj >= 0 && substantive(adj)) pick = adj;
      else if (adj >= 0) pick = post >= 0 ? post : (prev >= 0 ? prev : adj);
      else if (post >= 0) pick = post;         // finish 前没有交付：取 finish 后的新交付
      else if (prev >= 0) pick = prev;
      else if (before.length) pick = before[before.length - 1];
      else if (after.length) pick = after[after.length - 1];
    }
    if (pick >= 0) answerIds.add(pick);
  }
  const parts = [];
  list.forEach((b, bi) => {
    if (!b || typeof b !== 'object') return;
    if (b.kind === 'reasoning' && b.text) {
      finalizeGroup();
      const box = buildThinkBlock(b.text, false);
      const head = thinkLineSummary(b.text, true);
      // 失败尝试的思考也保留：标成「上次尝试」而不是普通思考
      box._label.textContent = (b.retry ? '上次尝试（上游中断）' : '思考')
        + (head ? ' · ' + head : '');
      flow.appendChild(box);
      return;
    }
    if (b.kind === 'tool') {
      const t = (options.tools || [])[b.i];
      if (!t) return;
      if (t.tool === 'ask_user') {
        finalizeGroup();                       // 提问前后各成一组，卡片夹在中间
        if (t.asks && t.asks.length) parts.push(buildAskGroup(t.asks, t.qid || null));
        return;
      }
      if (isBgExploreTool(t)) return;          // 后台批次不放进过程折叠，稍后单独成卡
      const g = toolGroup(ctx);
      const card = buildToolCard(t.tool, t.args);
      finishToolCard(card, { name: t.tool, args: t.args, result: t.result, undo: t.undo,
                             write: t.write, ok: t.ok, ms: t.ms, lines: t.lines });
      g.body.appendChild(card);
      g.done++;
      g.ms += parseInt(t.ms, 10) || 0;
      g.kinds[t.tool] = (g.kinds[t.tool] || 0) + 1;
      if (t.ok === false) g.failed++;
      return;
    }
    if (b.kind === 'text') {
      finalizeGroup();                        // 正文分界：下一批工具另起一组
      const text = typeof b.text === 'string' ? b.text : '';
      if (!text.trim()) return;
      const d = document.createElement('div');
      d.className = 'content';                // 与最终答案同一套排版
      d.innerHTML = safeRenderMarkdown(text);
      enhanceToc(d);
      // 答案块按 `ans` 标记 / 顺序启发式判定；其余正文收进过程折叠当旁白。
      if (answerIds.has(bi)) {
        parts.push(d);
      } else {
        d.classList.add('narration');
        flow.appendChild(d);
      }
    }
  });
  finalizeGroup();
  const fold = flow.childNodes.length
    ? buildProcessFold(flow, timing, endKind, options.open === true)
    : null;
  return { fold: fold, flow: flow, parts: parts };
}

/* 历史回显（新消息）：按落库的 blocks 还原「过程折叠 + 正文分段 + 提问卡原位」。
   旧消息没有 blocks，继续走 buildAiRow，行为完全不变。 */
function buildAiRowFromBlocks(m, index) {
  const row = document.createElement('div');
  row.className = 'msg ai';
  if (typeof index === 'number') row.dataset.idx = index;
  const av = document.createElement('div');
  av.className = 'avatar';
  av.textContent = '✦';
  const wrap = document.createElement('div');
  wrap.className = 'content-wrap';
  if (m.sources && m.sources.length) wrap.appendChild(buildSourcesBlock(m.sources, false));
  let planCard = null;
  if (m.plan && m.plan.steps && m.plan.steps.length) {
    planCard = buildPlanCard(m.plan, planAllDone(m.plan));
    wrap.appendChild(planCard);
  }

  const view = buildBlocksView(m.blocks, m.timing, m.end_reason && m.end_reason.kind,
                               { tools: m.tools, open: false });
  if (view.fold) {
    wrap.appendChild(view.fold);
    if (window.enhanceRichMedia) window.enhanceRichMedia(view.fold);
  }
  const secs = [];
  const asks = [];
  view.parts.forEach((p) => {
    wrap.appendChild(p);
    if (p.classList.contains('ask-card')) asks.push(p);
    else secs.push(p);
  });
  if (!secs.length) {
    // 正文全是过场旁白时，它们已经在过程折叠里了：答案区留空，别把全文再贴回来。
    // 只有真的没有任何正文块，才退回落库文本兜底。
    const hadText = (m.blocks || []).some((b) => b && b.kind === 'text'
      && String(b.text || '').trim());
    const d = document.createElement('div');
    d.className = 'content';
    if (!hadText) {
      d.innerHTML = safeRenderMarkdown(m.content || '');
      enhanceToc(d);
    }
    wrap.appendChild(d);
    secs.push(d);
  }
  appendExploreBatchCards(wrap, m.tools);   // 后台探索批次：常驻卡，不进折叠
  appendDiagramCards(wrap, m.tools);        // archify 图卡：内嵌可交互 HTML
  const c = secs[secs.length - 1];

  row.appendChild(av);
  row.appendChild(wrap);
  row._content = c;
  row._secs = secs;
  row._asks = asks;
  row._wrap = wrap;
  row._raw = m.content || '';
  row._planCard = planCard;
  row._plan = planCard ? m.plan : null;
  row._planSteps = planCard ? m.plan.steps : [];
  if (m.memory) wrap.appendChild(buildMemoryChip(m.memory, index));
  // 空内容也建操作条：中断 / 报错的轮次没有正文，但「继续输出 / 重新发言 /
  // 上下文压缩」仍要能操作（圆环只挂在 .msg-actions 上）。
  wrap.appendChild(buildActions(row, () => row._raw, index, 'ai'));
  if (m.usage || m.done_at) {
    const u = document.createElement('div');
    u.className = 'usage-line';
    setUsage(u, m.usage, m.done_at, usageCalls(m), m.timing);
    if (!u.textContent) u.textContent = '完成时间 ' + clockText(m.done_at);
    wrap.appendChild(u);
    row._usageEl = u;
  }
  return row;
}

/* 按实时数据重建「正在生成的那条」。
   切会话 / 刷新后本地没有 live DOM 时用它，与历史行共用 buildBlocksView，
   所以外观和收尾后的正式消息完全一致，不会出现两种排版。 */
function buildLiveRow(sid, live) {
  const row = document.createElement('div');
  row.className = 'msg ai stream-live';
  const av = document.createElement('div');
  av.className = 'avatar';
  av.textContent = '✦';
  const wrap = document.createElement('div');
  wrap.className = 'content-wrap';
  let planCard = null;
  if (live.plan && live.plan.steps && live.plan.steps.length) {
    planCard = buildPlanCard(live.plan, planAllDone(live.plan));
    wrap.appendChild(planCard);
  }
  const view = buildBlocksView(live.blocks, live.timing, '',
                               { tools: live.tools || [], open: true });
  if (view.fold) {
    wrap.appendChild(view.fold);
    if (window.enhanceRichMedia) window.enhanceRichMedia(view.fold);
  }
  const secs = [];
  const asks = [];
  view.parts.forEach((p) => {
    wrap.appendChild(p);
    if (p.classList.contains('ask-card')) asks.push(p);
    else secs.push(p);
  });
  row.appendChild(av);
  row.appendChild(wrap);
  row._content = secs[secs.length - 1] || null;
  row._secs = secs;
  row._asks = asks;
  row._wrap = wrap;
  row._raw = live.text || '';
  row._planCard = planCard;
  row._plan = live.plan || null;
  row._planSteps = planCard ? live.plan.steps : [];
  return row;
}

function appendAi(contentHtml, rawText, index, reasoning, sources, usage, memory) {
  const row = buildAiRow(contentHtml, rawText, index, reasoning, sources, usage, memory);
  row.classList.add('enter');                    // 只有新消息播入场动画，历史重渲染不播
  $('#chat').appendChild(row);
  placeCtxRing();                               // 新行会插到圆环之后，重新挂回末尾
  if (window.enhanceRichMedia) window.enhanceRichMedia(row);
  scrollToBottom(true);
  return row;
}

const RENDER_PAGE = 60;   // 首屏只渲染最近 N 条，长历史时避免一次性渲染卡顿

/* 中断原因：一行小字 + 可选动作（继续输出 / 重试）。
   kind 决定颜色：network / server 红，length / max_rounds / empty 黄，
   stopped / done / plan 灰。 */
function buildEndNote(reason, index) {
  const kind = String((reason && reason.kind) || '');
  const note = document.createElement('div');
  note.className = 'end-note'
    + (kind === 'network' || kind === 'server' || kind === 'billing' ? ' end-err'
       : (kind === 'stopped' || kind === 'plan' || kind === 'done') ? ' end-mute' : ' end-warn');
  const mark = document.createElement('span');
  mark.className = 'end-mark';
  mark.textContent = kind === 'plan' ? '◆' : (kind === 'done' ? '✓' : '⏹');
  const txt = document.createElement('span');
  txt.className = 'end-text';
  txt.textContent = (reason && reason.text) || '本轮中断';
  note.appendChild(mark);
  note.appendChild(txt);
  const act = reason && reason.action;
  if (act === 'continue' && typeof index === 'number'
      && typeof continueReply === 'function') {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'end-act';
    b.textContent = '继续输出';
    b.onclick = () => continueReply(index);
    note.appendChild(b);
  } else if (act === 'resume' && typeof index === 'number'
             && typeof continueReply === 'function') {
    // 进程重启后留下的断点：后端会把「已完成步骤」一起回灌，避免重复写文件
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'end-act';
    b.textContent = '继续任务';
    b.onclick = () => continueReply(index, true);
    note.appendChild(b);
  } else if (act === 'retry' && typeof index === 'number'
             && typeof regenerateReply === 'function') {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'end-act';
    b.textContent = '重试';
    b.onclick = () => regenerateReply(index);
    note.appendChild(b);
  }
  return note;
}

// 为一条消息建 DOM 行（历史重渲染与增量展开共用）
function rowForMsg(msgs, idx) {
  const m = msgs[idx];
  if (m.role === 'user') return buildUserRow(m.content, idx, m);
  // 新消息有 blocks：按「过程在上、答案在下」回显；旧消息 / GAL 继续走原路径。
  const row = (m.blocks && m.blocks.length && !m.gal)
    ? buildAiRowFromBlocks(m, idx)
    : buildAiRow(safeRenderMarkdown(m.content), m.content, idx, m.reasoning,
                 m.sources, m.usage, m.memory, m.tools, m.plan);
  // 显式引用已经显示在用户气泡上，这里只保留自动检索的条目，避免重复
  const autoRefs = (m.references || []).filter((r) => r && r.auto);
  if (autoRefs.length) {
    row._wrap.appendChild(buildReferencesBlock(autoRefs));
  }
  // 旧路径（buildAiRow）拿不到 timing，统一在这里补「N 次调用累计」的口径。
  if (row._usageEl && m.usage) {
    setUsage(row._usageEl, m.usage, m.done_at, usageCalls(m), m.timing);
  }
  if (m.end_reason) {
    try {
      row._wrap.appendChild(buildEndNote(m.end_reason, idx));
    } catch (e) {
      /* 提示渲染失败不能让整条消息消失 */
    }
  }
  // 文件改动卡：流式时由 tool_done 事件建在内存里，刷新 / 切会话后这里用落库的
  // tools 重建一份，否则回复末尾的「N 个文件已更改」会整个消失。
  // 只给「已完成的回合」重建：没有 done_at 的多半是进度快照（tools 是累计的），
  // 每条都建卡会把同一批文件重复几十次。
  if (m.done_at && row._wrap && typeof ideChangedHistoryAttach === 'function') {
    try {
      ideChangedHistoryAttach(row._wrap, m.tools);
    } catch (e) {
      /* 卡片失败不影响消息本身 */
    }
  }
  return row;
}

/* 单条消息的渲染边界：一条坏消息不能让整个会话列表变空白 */
function fallbackRow(msgs, idx, err) {
  const m = msgs[idx] || {};
  const row = document.createElement('div');
  row.className = 'msg ' + (m.role === 'user' ? 'user' : 'ai');
  const body = document.createElement('div');
  body.className = 'content md-fallback';
  body.textContent = String(m.content == null ? '（这条消息无法渲染）' : m.content);
  row.appendChild(body);
  if (window.console && console.error) console.error('[chat] 消息渲染失败，已降级', idx, err);
  return row;
}

function rowForMsgSafe(msgs, idx) {
  try {
    return rowForMsg(msgs, idx);
  } catch (e) {
    return fallbackRow(msgs, idx, e);
  }
}

/* ---------------- 历史压缩分隔线 ---------------- */
/* 压缩标记的唯一键：一个压缩点（upto）只应有一条分隔线。
   at 来自服务器时钟，跨设备会漂移，不能当唯一键。 */
function compressMarkKey(mark) {
  const upto = Number(mark && mark.upto);
  if (Number.isFinite(upto) && upto > 0) return 'u' + upto;
  return 'a' + String((mark && mark.at) || '');
}

function compressMarkTitle(mark) {
  const bits = [];
  if (mark && mark.count) bits.push('本次压缩 ' + mark.count + ' 条较早消息');
  if (mark && mark.at) {
    try { bits.push(new Date(Number(mark.at)).toLocaleString()); } catch (e) { /* 时间格式化失败不影响提示 */ }
  }
  return bits.join(' · ');
}

function compressDividerEl(mark, opts) {
  const el = document.createElement('div');
  el.className = 'compress-divider' + (opts && opts.pending ? ' pending' : '');
  if (mark) el.dataset.key = compressMarkKey(mark);
  const label = document.createElement('span');
  label.textContent = (opts && opts.pending)
    ? '正在压缩较早的历史对话…'
    : '历史对话已被压缩';
  el.appendChild(label);
  if (mark && mark.at) el.dataset.at = String(mark.at);
  if (mark) el.title = compressMarkTitle(mark);
  return el;
}

function compressPlaceAtEnd(chat, el) {
  // 本轮助手回复还在流式渲染时，分隔线应该落在它前面（压缩发生在回复开始之前）
  const host = chat.querySelector('.stream-host');
  if (host && host.parentNode === chat) chat.insertBefore(el, host);
  else chat.appendChild(el);
}

/* 压缩线放在它真正的压缩位置：mark.index 那条消息之前。
   中途压缩完还会继续聊很多轮，分隔线必须留在原地；每次 context 同步都把它
   带回消息流底部（旧实现 compressPlaceAtEnd）就会「越聊越往下滑」。
   mark.index 超出已渲染范围（压缩点更早 / 已全被压缩）时退回底部。 */
function compressPlaceAtMark(chat, el, mark) {
  const idx = Number(mark && mark.index);
  if (Number.isFinite(idx) && idx >= 0) {
    const rows = chat.querySelectorAll('.msg[data-idx]');
    for (let i = 0; i < rows.length; i++) {
      const n = Number(rows[i].dataset.idx);
      if (Number.isFinite(n) && n >= idx) {
        // 已在目标位置就不动它（避免每次同步都触发一次重排）
        if (el.parentNode !== chat || rows[i].previousElementSibling !== el) {
          chat.insertBefore(el, rows[i]);
        }
        return;
      }
    }
  }
  compressPlaceAtEnd(chat, el);
}

/* 从 context 同步压缩标记：全量对账（补 / 升级 / 删）。
   消息流里**只保留最近一条**压缩线：更早的压缩记录在进度条「详情」里看，
   否则编辑 / 反复压缩会让分隔线越堆越多（对齐 Trae 的「无感压缩」）。 */
function syncCompressMarks(ctx) {
  if (!ctx || !Array.isArray(ctx.compress_marks) || state.uiMode === 'gal') return;
  const chat = $('#chat');
  if (!chat) return;
  const marks = ctx.compress_marks.filter((m) => m && typeof m === 'object');
  const s = (state.sessions && Array.isArray(state.sessions.sessions)) ? curSession() : null;
  if (s) s.compress_marks = marks;
  const latest = marks.length ? marks[marks.length - 1] : null;
  const key = latest ? compressMarkKey(latest) : '';
  // 1) 删掉「已经不是最近一条」的历史分隔线（pending 留到下一步统一升级 / 清理）
  chat.querySelectorAll('.compress-divider:not(.pending)').forEach((el) => {
    if (!key || el.dataset.key !== key) {
      clearTimeout(el._timer);
      el.remove();
    }
  });
  const pending = chat.querySelector('.compress-divider.pending');
  if (!latest) {
    if (!pending) compressFlushQueue();      // 压缩失败 / 取消：别让排队消息一直等
    return;
  }
  let el = chat.querySelector('.compress-divider[data-key="' + key + '"]');
  if (!el && pending) el = pending;          // 升级「正在压缩」为已完成
  if (!el) {
    el = compressDividerEl(latest);
    compressPlaceAtMark(chat, el, latest);
  } else {
    clearTimeout(el._timer);
    el.classList.remove('pending');
    el.classList.add('fresh');
    el.dataset.key = key;
    el.dataset.at = String(latest.at || '');
    el.title = compressMarkTitle(latest);
    const label = el.querySelector('span');
    if (label) label.textContent = '历史对话已被压缩';
    compressPlaceAtMark(chat, el, latest);     // 回到真正的压缩位置，不跟着流式回复跑到底部
  }
  compressFlushQueue();
}

/* 压缩完成后放行排队消息（队列本身在 stream.js） */
function compressFlushQueue() {
  if (typeof flushSendQueue !== 'function') return;
  setTimeout(flushSendQueue, 180);
}

function compressPending() {
  if (state.uiMode === 'gal') return null;
  const chat = $('#chat');
  if (!chat) return null;
  let el = chat.querySelector('.compress-divider.pending');
  if (el) return el;
  el = compressDividerEl(null, { pending: true });
  el.dataset.started = String(Math.floor(Date.now() / 1000));
  compressPlaceAtEnd(chat, el);
  el.scrollIntoView({ block: 'nearest' });
  el._timer = setTimeout(() => {
    if (el.classList.contains('pending')) el.remove();   // 压缩失败时别留下假的「进行中」
    compressFlushQueue();                                // 别让排队消息一直等
  }, 120000);
  return el;
}

function compressPendingClear() {
  const chat = $('#chat');
  if (!chat) return;
  const el = chat.querySelector('.compress-divider.pending');
  if (el) { clearTimeout(el._timer); el.remove(); }
  compressFlushQueue();        // 压缩失败 / 取消：排队消息不能一直等
}

/* 该会话是否正在压缩较早历史（发送 / 修改要先排队） */
function compressIsPending() {
  const chat = $('#chat');
  return !!(chat && chat.querySelector('.compress-divider.pending'));
}

function compressClearAll() {
  const chat = $('#chat');
  if (!chat) return;
  chat.querySelectorAll('.compress-divider').forEach((el) => {
    clearTimeout(el._timer);
    el.remove();
  });
  compressFlushQueue();
}

// 「展开更早」按钮：本地已有更早消息就在本地展开；否则向后端按需拉一页
function makeMoreBtn(start) {
  const s = curSession();
  const loadedFrom = s ? (Number(s.msg_loaded_start) || 0) : 0;
  const more = document.createElement('button');
  more.className = 'load-more';
  if (start <= loadedFrom && loadedFrom > 0) {
    more.textContent = '↑ 加载更早的消息';
    more.onclick = () => loadOlderMessages(loadedFrom);
    return more;
  }
  const span = Math.max(0, start - loadedFrom);
  more.textContent = '↑ 展开更早的 ' + Math.min(span, RENDER_PAGE) + ' 条';
  more.onclick = () => renderMessages(Math.max(loadedFrom, start - RENDER_PAGE));
  return more;
}

/* 按需拉一页更早的消息，填回全局下标后再增量插到顶部 */
async function loadOlderMessages(before, limit, silent) {
  const s = curSession();
  if (!s || !s.id) return false;
  const from = Number.isFinite(before) ? before : (Number(s.msg_loaded_start) || 0);
  if (from <= 0) return false;
  const btn = $('#chat .load-more');
  if (btn) { btn.disabled = true; btn.textContent = '正在加载…'; }
  try {
    const r = await api('/api/session/messages', {
      session_id: s.id, before: from, limit: limit || RENDER_PAGE,
    });
    if (!r || !r.ok || !Array.isArray(r.messages)) {
      throw new Error((r && (r.error || r.message)) || '加载失败');
    }
    const start = Number(r.from) || 0;
    r.messages.forEach((m, i) => { s.messages[start + i] = m; });
    s.msg_loaded_start = Math.min(Number(s.msg_loaded_start) || 0, start);
    s.msg_total = Number(r.total) || s.messages.length;
    if (!silent) renderMessages(start, false);   // 增量插到最前面，保持视觉位置
    return true;
  } catch (e) {
    if (btn) { btn.disabled = false; btn.textContent = '↑ 加载更早的消息'; }
    toastErr('加载更早消息', e);
    return false;
  }
}

// 长回答的目录：单条内容里 h2/h3 ≥ 3 个时，在正文最上方插一个可折叠目录。
// 只做「跳转」，不改 Markdown 渲染结果；历史消息与流式收尾共用。
let _tocSeq = 0;
function enhanceToc(root) {
  if (!root || root.querySelector('.md-toc')) return;
  const hs = Array.from(root.querySelectorAll('h2, h3'));
  if (hs.length < 3) return;
  const box = document.createElement('details');
  box.className = 'md-toc';
  const sum = document.createElement('summary');
  sum.textContent = '目录 · ' + hs.length + ' 节';
  box.appendChild(sum);
  const list = document.createElement('div');
  list.className = 'md-toc-list';
  hs.forEach((h) => {
    if (!h.id) h.id = 'mdh-' + (++_tocSeq);
    const a = document.createElement('a');
    a.className = 'md-toc-item' + (h.tagName === 'H3' ? ' lvl3' : '');
    a.href = '#' + h.id;
    a.textContent = (h.textContent || '').trim();
    a.onclick = (e) => {
      e.preventDefault();
      try { h.scrollIntoView({ block: 'start', behavior: 'smooth' }); } catch (err) { h.scrollIntoView(); }
      box.open = false;
    };
    list.appendChild(a);
  });
  box.appendChild(list);
  root.insertBefore(box, root.firstChild);
}

function renderMessages(fromIndex, forceWindow, opts) {
  const chat = $('#chat');
  const expanding = typeof fromIndex === 'number';
  const keepScroll = !!(opts && opts.keepScroll);   // 后台事件刷新：保持用户当前阅读位置
  const oldHeight = chat.scrollHeight;
  const oldTop = chat.scrollTop;
  const s = curSession();
  planSyncFromSession(s);                      // 任务面板跟着当前会话走
  if (!s || !(s.messages || []).length) {
    findState.hits = [];                       // 整块重排后，之前的高亮节点已经不存在了
    findState.idx = -1;
    chat.dataset.start = '';
    chat.innerHTML = EMPTY_HTML;
    bindChips();
    placeCtxRing();
    if (typeof turnDotsSync === 'function') turnDotsSync();
    if (typeof ideReviewTipRefresh === 'function') ideReviewTipRefresh();
    if (!keepScroll && typeof replayEnter === 'function') replayEnter(chat, 'chat-fade');
    return;
  }

  const msgs = s.messages;
  const total = msgs.length;
  // 服务端只回了最近一段：loadedFrom 之前的位置是 null，需要时再向后端拉
  const loadedFrom = Number(s.msg_loaded_start) || 0;
  const marks = Array.isArray(s.compress_marks) ? s.compress_marks : [];
  const marksAt = {};
  // 只画最近一条：历史的压缩记录在「进度条 → 详情」里查，消息流不堆叠
  const latestMark = marks.length ? marks[marks.length - 1] : null;
  if (latestMark && typeof latestMark === 'object') {
    let idx = Number(latestMark.index);
    if (!Number.isFinite(idx)) idx = total;
    idx = Math.max(0, Math.min(idx, total));
    marksAt[idx] = [latestMark];
  }
  const addMarks = (frag, idx) => {
    (marksAt[idx] || []).forEach((m) => frag.appendChild(compressDividerEl(m)));
  };

  // 增量展开：已有内容仍然有效，只把新增的更早消息插到最前面，避免整块重排（长会话关键优化）
  // forceWindow=true 时跳过增量，直接把 fromIndex 当首行重排（对话记录跳转用：避免整段插入后图片
  // 异步加载把滚动位置顶跑）
  if (expanding && !forceWindow && chat.querySelector('.msg')) {
    const prevStart = Number(chat.dataset.start);
    if (Number.isFinite(prevStart) && fromIndex < prevStart && fromIndex >= loadedFrom) {
      findState.hits = [];
      findState.idx = -1;
      const frag = document.createDocumentFragment();
      for (let idx = fromIndex; idx < prevStart; idx++) {
        addMarks(frag, idx);
        frag.appendChild(rowForMsgSafe(msgs, idx));
      }
      chat.insertBefore(frag, chat.querySelector('.msg, .compress-divider'));
      chat.dataset.start = String(fromIndex);
      const more = chat.querySelector('.load-more');
      if (fromIndex > 0) {
        if (more) more.replaceWith(makeMoreBtn(fromIndex));
        else chat.insertBefore(makeMoreBtn(fromIndex), chat.firstChild);
      } else if (more) {
        more.remove();
      }
      chat.scrollTop = oldTop + (chat.scrollHeight - oldHeight);   // 保持视觉位置不动
      placeCtxRing();
      if (typeof turnDotsSync === 'function') turnDotsSync();
      return;
    }
  }

  // 要展开 / 跳转的位置还没加载：先拉那一页，再整体重画
  if (expanding && fromIndex < loadedFrom) {
    loadOlderMessages(fromIndex + 1, RENDER_PAGE, true)
      .then((ok) => { if (ok) renderMessages(fromIndex, true); });
    return;
  }

  const start = expanding ? Math.max(loadedFrom, fromIndex) : Math.max(loadedFrom, total - RENDER_PAGE);
  // 正在生成的这一条由实时数据承担：历史只画到它之前，末尾再挂 live 投影。
  // 这是「一条回答只有一条投影」的唯一判定点——切会话、刷新、重连都走这里，
  // 不再需要在每条路径上单独删除「进度快照行」。
  const _sid = String(state.sessions.current || '');
  const _st = (typeof streamOf === 'function') ? streamOf(_sid) : null;
  const _live = (typeof liveOf === 'function') ? liveOf(_sid, true) : null;
  const _liveData = !!(_live && _live.blocks && _live.blocks.length);
  const _lastMsg = msgs[total - 1];
  // 实时行承担的是「哪一条」：有 data-idx 就精确跳过它；新行（还没有 idx）
  // 一律承担最后一条 assistant。**不再依赖 `_progress` 标记** —— 那个标记在后端
  // 收尾窗口里可能已经被清掉，靠它判断就会出现「快照行 + 实时行」两份。
  let _skipIdx = -1;
  const _liveRow = (_st && _st.row) ? _st.row : null;
  if (_liveRow) {
    _skipIdx = (_liveRow.dataset && _liveRow.dataset.idx !== undefined)
      ? Number(_liveRow.dataset.idx)
      : ((_lastMsg && _lastMsg.role === 'assistant') ? total - 1 : -1);
  } else if (_liveData) {
    _skipIdx = (_lastMsg && _lastMsg.role === 'assistant') ? total - 1 : -1;
  }
  // 实时 host 先摘下来再放回：innerHTML 会连它一起清掉，重挂时内容会闪一下
  const _keepHost = (_st && _st.host) || null;
  if (_keepHost && _keepHost.parentNode) _keepHost.parentNode.removeChild(_keepHost);
  if (window.disposeRichMedia) window.disposeRichMedia();   // 清掉旧图表实例，避免内存泄漏
  chat.innerHTML = '';
  findState.hits = [];
  findState.idx = -1;

  // 先在一个 fragment 里建好，再一次性插入 —— 只触发一次布局
  const frag = document.createDocumentFragment();
  if (start > 0) frag.appendChild(makeMoreBtn(start));
  for (let idx = start; idx < total; idx++) {
    if (!msgs[idx] || idx === _skipIdx) continue;   // 还没加载的旧消息 / 由实时行承担的那条
    addMarks(frag, idx);
    frag.appendChild(rowForMsgSafe(msgs, idx));
  }
  addMarks(frag, total);
  chat.appendChild(frag);
  if (_keepHost) {
    chat.appendChild(_keepHost);       // 本地流还活着：复用它的 DOM，内容不闪
  } else if (_liveData && state.uiMode !== 'gal') {
    chat.appendChild(buildLiveRow(_sid, _live));   // 只有数据没有 DOM：按数据重建
  }
  chat.dataset.start = String(start);
  // 压缩点落在本次渲染窗口之前（那段还没加载 / 已被折叠）时上面不会画到：
  // 兜底补一条，位置仍按真实压缩点算，不能因为没命中就丢掉或贴到底部。
  if (latestMark && typeof latestMark === 'object'
      && !chat.querySelector('.compress-divider')) {
    compressPlaceAtMark(chat, compressDividerEl(latestMark), latestMark);
  }
  if (window.enhanceRichMedia) window.enhanceRichMedia(chat);
  // 展开更早时保持视觉位置不动，否则滚到底部
  chat.scrollTop = expanding ? oldTop + (chat.scrollHeight - oldHeight)
                             : (keepScroll ? oldTop : chat.scrollHeight);
  placeCtxRing();
  if (typeof turnDotsSync === 'function') turnDotsSync();
  if (typeof ideReviewTipRefresh === 'function') ideReviewTipRefresh();
  // 历史里可能有多张计划卡：只留最新一张展开，其余收成「已续至最新输出」
  if (typeof planSweepHistory === 'function') planSweepHistory();
  // 切会话时整块换内容，给一帧淡入，避免「闪白 → 突然满屏文字」
  if (!expanding && !keepScroll && typeof replayEnter === 'function') {
    replayEnter(chat, 'chat-fade');
  }
  // 重排会把「排队中」的幽灵气泡冲掉：重建一次，保证它们始终在对话末尾
  if (typeof renderQueueGhosts === 'function') renderQueueGhosts();
}

function bindChips() {
  $$('.chip').forEach((c) => {
    c.onclick = () => { $('#input').value = c.dataset.prompt; sendMessage(); };
  });
  // 空状态里的模式卡：直接切到对应工作区（Solo / IDE / 大厅 / 工作台）
  $$('#empty .mode-card').forEach((c) => {
    c.onclick = () => {
      if (typeof setUiMode === 'function') setUiMode(c.dataset.mode);
    };
  });
}
