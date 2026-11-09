/* 工作台 · views
   由 work.js 按功能拆分；普通脚本共享全局作用域，加载顺序见 index.html。 */
'use strict';

function workDayKey(ts) {
  return workDateValue(new Date(Number(ts) * 1000));
}

function workEventsOnDay(dayStart, events) {
  return (events || WORK.events).filter((e) => {
    const t = Number(e.occurrence_at || 0);
    return t >= dayStart && t < dayStart + 86400;
  }).sort((a, b) => a.occurrence_at - b.occurrence_at);
}

function workTasksOnDay(dayStart) {
  return WORK.tasks.filter((t) => {
    const due = Number(t.due_at || (t.meta && t.meta.due_at) || 0);
    return due >= dayStart && due < dayStart + 86400;
  }).sort((a, b) => Number(a.due_at || 0) - Number(b.due_at || 0));
}

function workSetView(view) {
  const ok = ['today', 'week', 'month', 'board', 'stats'];
  WORK.view = ok.includes(view) ? view : 'today';
  try { localStorage.setItem('gm.work.view', WORK.view); } catch (e) { /* 忽略 */ }
  workApplyView();
}

function workApplyView() {
  const view = WORK.view || 'today';
  const cols = document.querySelector('#work-panel .work-cols');
  if (cols) cols.classList.toggle('hidden', view !== 'today');
  const map = { week: 'work-view-week', month: 'work-view-month',
                board: 'work-view-board', stats: 'work-view-stats' };
  Object.keys(map).forEach((v) => {
    const el = workEl(map[v]);
    if (el) el.classList.toggle('hidden', view !== v);
  });
  document.querySelectorAll('#work-views-tabs button').forEach((b) => {
    const on = b.dataset.wview === view;
    b.classList.toggle('on', on);
    b.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  if (view === 'week') {
    workRenderWeek();
    if (!WORK.stats) {
      workLoadStats().then(() => { if (WORK.view === 'week') workRenderWeek(); });
    }
  } else if (view === 'month') {
    const key = workMonthKey(WORK.monthOffset);
    if (WORK.monthLoadedFor !== key) workLoadMonth();
    else workRenderMonth();
  } else if (view === 'board') workRenderBoard();
  else if (view === 'stats') workLoadStats();
}

/* ---------------- 周视图 ---------------- */

function workWeekStart(ts) {
  const d = new Date(Number(ts) * 1000);
  const dow = (d.getDay() + 6) % 7;              // 周一 = 0
  d.setHours(0, 0, 0, 0);
  return d.getTime() / 1000 - dow * 86400;
}

function workRenderWeek() {
  const box = workEl('work-view-week');
  if (!box) return;
  box.innerHTML = '';
  const start = workWeekStart(Date.now() / 1000);
  const head = workElNew('div', 'wv-board-tools');
  const range = new Date(start * 1000);
  head.appendChild(workElNew('span', '', '本周 · ' + (range.getMonth() + 1) + ' 月 '
    + range.getDate() + ' 日起 · 拖到「月」看整月'));
  const focusMin = WORK.stats ? WORK.stats.focus_minutes : 0;
  if (focusMin) head.appendChild(workElNew('span', '', '近 7 天专注 ' + focusMin + ' 分钟'));
  box.appendChild(head);

  const grid = workElNew('div', 'wv-week');
  const todayKey = workDayKey(Date.now() / 1000);
  for (let i = 0; i < 7; i++) {
    const dayStart = start + i * 86400;
    const day = new Date(dayStart * 1000);
    const col = workElNew('div', 'wv-day' + (workDayKey(dayStart) === todayKey ? ' today' : ''));
    const h = workElNew('div', 'wv-day-head');
    h.appendChild(workElNew('span', '', (day.getMonth() + 1) + '/' + day.getDate()
      + ' ' + workWeekday(dayStart)));
    const evs = workEventsOnDay(dayStart);
    const tks = workTasksOnDay(dayStart);
    if (WORK.stats && WORK.stats.focus_by_day) {
      const idx = Math.round((workDayStart(dayStart) - workDayStart(WORK.stats.start_at)) / 86400);
      const fm = (idx >= 0 && idx < (WORK.stats.focus_by_day || []).length)
        ? WORK.stats.focus_by_day[idx] : 0;
      if (fm) h.appendChild(workElNew('small', '', '专注 ' + fm + 'm'));
    }
    col.appendChild(h);
    const body = workElNew('div', 'wv-day-body');
    evs.forEach((e) => {
      const it = workElNew('div', 'wv-item event');
      it.appendChild(workElNew('div', 'wv-time', workFmt(e.occurrence_at)));
      it.appendChild(workElNew('div', '', e.title));
      it.onclick = () => workOpenEventDialog(e);
      body.appendChild(it);
    });
    tks.forEach((t) => {
      const due = Number(t.due_at || 0);
      const it = workElNew('div', 'wv-item task'
        + (due < Date.now() / 1000 && !['done', 'canceled'].includes(t.status) ? ' overdue' : ''));
      it.appendChild(workElNew('div', 'wv-time', '任务 · ' + workFmt(due)));
      it.appendChild(workElNew('div', '', t.title));
      it.onclick = () => workOpenTask(t.id);
      body.appendChild(it);
    });
    if (!evs.length && !tks.length) body.appendChild(workElNew('div', 'wv-empty', '没有安排'));
    col.appendChild(body);
    grid.appendChild(col);
  }
  box.appendChild(grid);
}

/* ---------------- 月视图 ---------------- */

function workMonthKey(offset) {
  const d = new Date();
  d.setDate(1);
  d.setMonth(d.getMonth() + (offset || 0));
  return d.getFullYear() + '-' + (d.getMonth() + 1);
}

async function workLoadMonth() {
  const key = workMonthKey(WORK.monthOffset);
  const d = new Date();
  d.setDate(1);
  d.setMonth(d.getMonth() + (WORK.monthOffset || 0));
  const start = new Date(d.getFullYear(), d.getMonth(), 1).getTime() / 1000;
  const end = new Date(d.getFullYear(), d.getMonth() + 1, 1).getTime() / 1000;
  try {
    const r = await api('/api/calendar/events', { op: 'range', start_at: start, end_at: end });
    WORK.monthEvents = r.events || [];
  } catch (e) {
    WORK.monthEvents = [];
  }
  WORK.monthLoadedFor = key;
  workRenderMonth();
}

function workRenderMonth() {
  const box = workEl('work-view-month');
  if (!box) return;
  box.innerHTML = '';
  const base = new Date();
  base.setDate(1);
  base.setMonth(base.getMonth() + (WORK.monthOffset || 0));
  const y = base.getFullYear();
  const m = base.getMonth();
  const first = new Date(y, m, 1);
  const gridStart = new Date(y, m, 1 - ((first.getDay() + 6) % 7));
  const todayKey = workDayKey(Date.now() / 1000);
  let selKey = WORK.selectedDay || todayKey;
  if (new Date(selKey + 'T00:00:00').getMonth() !== m) {
    selKey = workDateValue(new Date(y, m, 1));   // 翻月后默认选中当月 1 号
    WORK.selectedDay = selKey;
  }

  const head = workElNew('div', 'wv-month-head');
  const prev = workElNew('button', 'work-btn small ghost', '‹');
  prev.title = '上个月';
  prev.onclick = () => { WORK.monthOffset -= 1; workLoadMonth(); };
  const next = workElNew('button', 'work-btn small ghost', '›');
  next.title = '下个月';
  next.onclick = () => { WORK.monthOffset += 1; workLoadMonth(); };
  const title = workElNew('div', 'wv-month-title', y + ' 年 ' + (m + 1) + ' 月');
  const today = workElNew('button', 'work-btn small', '回到今天');
  today.onclick = () => {
    WORK.monthOffset = 0;
    WORK.selectedDay = workDayKey(Date.now() / 1000);
    workLoadMonth();
  };
  head.appendChild(prev);
  head.appendChild(title);
  head.appendChild(next);
  head.appendChild(today);
  box.appendChild(head);

  const grid = workElNew('div', 'wv-month-grid');
  ['一', '二', '三', '四', '五', '六', '日'].forEach((d) => {
    grid.appendChild(workElNew('div', 'wv-month-dow', '周' + d));
  });
  const events = WORK.monthEvents.length ? WORK.monthEvents : WORK.events;
  for (let i = 0; i < 42; i++) {
    const day = new Date(gridStart.getFullYear(), gridStart.getMonth(), gridStart.getDate() + i);
    const dayStart = day.getTime() / 1000;
    const key = workDateValue(day);
    const out = day.getMonth() !== m;
    const cell = workElNew('div', 'wv-mday' + (out ? ' out' : '')
      + (key === todayKey ? ' today' : '') + (key === selKey ? ' sel' : ''));
    const num = workElNew('div', 'wv-mday-num');
    num.textContent = String(day.getDate());
    cell.appendChild(num);
    const evs = workEventsOnDay(dayStart, events);
    const tks = workTasksOnDay(dayStart);
    evs.slice(0, 2).forEach((e) => {
      cell.appendChild(workElNew('div', 'wv-mday-item', workFmt(e.occurrence_at) + ' ' + e.title));
    });
    tks.slice(0, 2).forEach((t) => {
      cell.appendChild(workElNew('div', 'wv-mday-item task', '任务 · ' + t.title));
    });
    const more = evs.length + tks.length - 4;
    if (more > 0) cell.appendChild(workElNew('div', 'wv-mday-more', '+' + more));
    cell.onclick = () => {
      WORK.selectedDay = key;
      workRenderMonth();
    };
    grid.appendChild(cell);
  }
  box.appendChild(grid);

  // 选中日期的明细
  const sel = new Date(selKey + 'T00:00:00');
  const selStart = sel.getTime() / 1000;
  const list = workElNew('div', 'wv-day-list');
  list.appendChild(workElNew('div', 'wv-day-list-head',
    (sel.getMonth() + 1) + ' 月 ' + sel.getDate() + ' 日 · '
    + workWeekday(selStart)));
  const evs = workEventsOnDay(selStart, events);
  const tks = workTasksOnDay(selStart);
  if (!evs.length && !tks.length) {
    list.appendChild(workElNew('div', 'wv-empty', '这一天没有安排'));
  }
  evs.forEach((e) => list.appendChild(workAgendaRow(e, false)));
  tks.forEach((t) => list.appendChild(workTaskCard(t)));
  box.appendChild(list);
}

/* ---------------- 看板视图 ---------------- */

const WORK_BOARD_COLS = [
  ['pending', '待开始'], ['running', '进行中'], ['paused', '已暂停'],
  ['failed', '失败'], ['done', '已完成'], ['canceled', '已取消'],
];

function workBoardMove(id, status) {
  const task = WORK.tasks.find((t) => t.id === id);
  if (!task || task.status === status) return;
  const op = { running: 'start', paused: 'pause', done: 'complete', canceled: 'cancel' }[status];
  if (!op) return;
  workTaskAct(id, op);
}

function workBoardReorder(srcId, targetId) {
  const ids = WORK.tasks.map((t) => t.id);
  const from = ids.indexOf(srcId);
  const to = ids.indexOf(targetId);
  if (from < 0 || to < 0 || from === to) return;
  ids.splice(to, 0, ids.splice(from, 1)[0]);
  api('/api/work/tasks', { op: 'reorder', ids })
    .then(() => workLoad(true))
    .catch((e) => toastErr('调整顺序失败', e));
}

function workBatchAct(action, value) {
  const ids = Array.from(WORK.batch);
  if (!ids.length) return;
  if (action === 'delete' && !window.confirm('删除选中的 ' + ids.length + ' 个任务？')) return;
  api('/api/work/tasks', { op: 'batch', ids, action, value })
    .then(async (r) => {
      if (!r || !r.ok) throw new Error((r && r.error) || '批量操作失败');
      WORK.batch.clear();
      WORK.stats = null;
      await workLoad(true);
      toast('已处理 ' + (r.done || []).length + ' 个任务', 'success');
    })
    .catch((e) => toastErr('批量操作失败', e));
}

function workRenderBoard() {
  const box = workEl('work-view-board');
  if (!box) return;
  box.innerHTML = '';
  const tools = workElNew('div', 'wv-board-tools');
  const batch = workElNew('div', 'wv-batch' + (WORK.batch.size ? '' : ' hidden'));
  batch.appendChild(workElNew('span', '', '已选 '));
  batch.appendChild(workElNew('b', '', String(WORK.batch.size)));
  const mk = (label, fn, cls) => {
    const b = workElNew('button', 'work-btn small ' + (cls || 'ghost'), label);
    b.onclick = fn;
    batch.appendChild(b);
  };
  mk('完成', () => workBatchAct('complete'));
  mk('取消', () => workBatchAct('cancel'));
  mk('删除', () => workBatchAct('delete'));
  const pri = workSelect([['', '优先级'], ['1', 'P1'], ['2', 'P2'], ['3', 'P3']]);
  pri.onchange = () => {
    if (pri.value) { workBatchAct('priority', Number(pri.value)); pri.value = ''; }
  };
  batch.appendChild(pri);
  const clear = workElNew('button', 'work-icon-btn', '✕');
  clear.title = '清空选择';
  clear.onclick = () => { WORK.batch.clear(); workRenderBoard(); };
  batch.appendChild(clear);
  tools.appendChild(batch);
  tools.appendChild(workElNew('span', '', '拖动卡片改状态；勾选后可批量处理'));
  box.appendChild(tools);

  const board = workElNew('div', 'wv-board');
  WORK_BOARD_COLS.forEach(([status, label]) => {
    const col = workElNew('div', 'wv-bcol');
    col.dataset.status = status;
    const head = workElNew('div', 'wv-bhead');
    head.appendChild(workElNew('span', '', label));
    const items = WORK.tasks.filter((t) => t.status === status);
    head.appendChild(workElNew('span', 'work-count', String(items.length)));
    col.appendChild(head);
    const body = workElNew('div', 'wv-bbody');
    items.forEach((t) => body.appendChild(workBoardCard(t)));
    if (!items.length) body.appendChild(workElNew('div', 'wv-empty', '拖到这里'));
    col.appendChild(body);
    col.ondragover = (e) => { e.preventDefault(); col.classList.add('drag-over'); };
    col.ondragleave = () => col.classList.remove('drag-over');
    col.ondrop = (e) => {
      e.preventDefault();
      col.classList.remove('drag-over');
      const id = e.dataTransfer.getData('text/plain');
      if (id) workBoardMove(id, status);
    };
    board.appendChild(col);
  });
  box.appendChild(board);
}

function workBoardCard(t) {
  const card = workElNew('div', 'wv-bcard' + (t.status === 'done' ? ' done' : ''));
  card.dataset.id = t.id;
  card.draggable = true;
  const head = workElNew('div', 'wv-bcard-head');
  const cb = document.createElement('input');
  cb.type = 'checkbox';
  cb.className = 'wv-bcard-check';
  cb.checked = WORK.batch.has(t.id);
  cb.onclick = (e) => e.stopPropagation();
  cb.onchange = () => {
    if (cb.checked) WORK.batch.add(t.id); else WORK.batch.delete(t.id);
    workRenderBoard();
  };
  head.appendChild(cb);
  head.appendChild(workElNew('div', 'wv-bcard-title', t.title));
  const pri = workPriorityChip(t.priority);
  if (pri) head.appendChild(pri);
  card.appendChild(head);

  const bits = [];
  const due = Number(t.due_at || (t.meta && t.meta.due_at) || 0);
  if (due) bits.push('截止 ' + workFmt(due, true));
  if (t.parent_id) {
    const p = WORK.tasks.find((x) => x.id === t.parent_id);
    if (p) bits.push('子任务 · ' + p.title);
  }
  if (t.depends_on) {
    const wait = t.depends_on.split(',').map((id) => WORK.tasks.find((x) => x.id === id))
      .filter((x) => x && x.status !== 'done');
    if (wait.length) bits.push('等待 ' + wait.map((x) => x.title).join('、'));
  }
  if (t.tags) bits.push(t.tags);
  if (t.progress) bits.push(t.progress.done + '/' + t.progress.total + ' 步');
  const meta = workElNew('div', 'wv-bcard-meta'
    + (due && due < Date.now() / 1000 && !['done', 'canceled'].includes(t.status) ? ' overdue' : ''),
    bits.join(' · '));
  card.appendChild(meta);

  card.onclick = () => workOpenTask(t.id);
  card.ondragstart = (e) => {
    e.dataTransfer.setData('text/plain', t.id);
    e.dataTransfer.effectAllowed = 'move';
    card.classList.add('dragging');
  };
  card.ondragend = () => card.classList.remove('dragging');
  // 同列拖到另一张卡上 = 调整顺序；跨列仍走列容器的 drop = 改状态
  card.ondragover = (e) => { e.preventDefault(); e.stopPropagation(); };
  card.ondrop = (e) => {
    e.preventDefault();
    e.stopPropagation();
    const src = e.dataTransfer.getData('text/plain');
    const source = WORK.tasks.find((x) => x.id === src);
    if (!source || src === t.id) return;
    if (source.status === t.status) workBoardReorder(src, t.id);
    else workBoardMove(src, t.status);
  };
  return card;
}

/* ---------------- 统计视图 ---------------- */

