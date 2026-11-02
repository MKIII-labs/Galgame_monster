/* ============================================================
   工作台（Work）—— 个人日程 / 任务执行 / 产物
   ------------------------------------------------------------
   与大厅一样，是「整屏」模式：进入后隐藏右侧对话列。
   后端接口：
     POST /api/calendar/events  {op: range|create|update|delete}
     POST /api/work/tasks       {op: list|create|start|pause|resume|cancel|...}
     POST /api/work/artifacts   {op: list}
   任务进度通过 /api/events 的 task_update 事件实时刷新（auto.js 转发进来）。
   ============================================================ */
'use strict';

const WORK = {
  events: [],
  tasks: [],
  artifacts: [],
  timer: null,
  bound: false,
  onOk: null,
  currentTask: null,
  sig: { events: '', tasks: '', artifacts: '' },
  scene: 'general',
  scenes: [],
  range: 'today',          // today | week
  taskFilter: '',
  taskQuery: '',
  showWorking: false,      // 产物栏是否显示工作区临时文件
  caps: null,              // 能力清单（步骤编辑用）
  wfDraft: null,           // 工作流表单草稿
  wfTemplates: [],
};

function workEl(id) { return document.getElementById(id); }

function workElNew(tag, cls, text) {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  if (text != null) el.textContent = text;
  return el;
}

function workFmt(ts, withDate) {
  const d = new Date(Number(ts) * 1000);
  if (isNaN(d.getTime())) return '?';
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  if (!withDate) return hh + ':' + mm;
  return (d.getMonth() + 1) + '-' + d.getDate() + ' ' + hh + ':' + mm;
}

function workDayStart(ts) {
  const d = new Date(Number(ts) * 1000);
  d.setHours(0, 0, 0, 0);
  return d.getTime() / 1000;
}

function workLocalInput(ts) {
  const d = new Date(Number(ts) * 1000);
  const p = (n) => String(n).padStart(2, '0');
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate())
    + 'T' + p(d.getHours()) + ':' + p(d.getMinutes());
}

/* ---------------- 时间选择器 ----------------
   原生 datetime-local 的时/分要靠键盘和小箭头微调，点选很别扭。
   这里拆成「日期快捷 chips + 日期输入 + 时/分下拉 + 常用时间」，全部点选完成。
   simple: true 时只保留日期 + 时分（用于「自定义结束时间」这类次要场景）。 */
function workPad2(n) { return String(n).padStart(2, '0'); }

function workDateValue(d) {
  return d.getFullYear() + '-' + workPad2(d.getMonth() + 1) + '-' + workPad2(d.getDate());
}

function workDateTimePicker(initTs, opts) {
  const o = opts || {};
  const base = new Date(Number(initTs) * 1000);
  const cur = isNaN(base.getTime()) ? new Date() : base;
  cur.setSeconds(0, 0);

  const wrap = workElNew('div', 'work-dt');
  const dayRow = workElNew('div', 'work-dt-days');
  const dateIn = document.createElement('input');
  dateIn.type = 'date';
  dateIn.className = 'work-dt-date';
  dateIn.value = workDateValue(cur);
  const dayChips = [];
  if (!o.simple) {
    [['今天', 0], ['明天', 1], ['后天', 2]].forEach(([label, off]) => {
      const b = workElNew('button', 'work-dt-chip', label);
      b.type = 'button';
      b.onclick = () => {
        const d = new Date();
        d.setDate(d.getDate() + off);
        dateIn.value = workDateValue(d);
        sync(true);
      };
      dayChips.push([b, off]);
      dayRow.appendChild(b);
    });
  }
  dayRow.appendChild(dateIn);
  wrap.appendChild(dayRow);

  const timeRow = workElNew('div', 'work-dt-time');
  const hourSel = document.createElement('select');
  hourSel.className = 'work-dt-sel';
  hourSel.setAttribute('aria-label', '时');
  for (let h = 0; h < 24; h++) {
    const op = document.createElement('option');
    op.value = workPad2(h);
    op.textContent = workPad2(h);
    hourSel.appendChild(op);
  }
  hourSel.value = workPad2(cur.getHours());
  const minSel = document.createElement('select');
  minSel.className = 'work-dt-sel';
  minSel.setAttribute('aria-label', '分');
  // 分钟按 5 分钟步进；编辑到 09:07 这种非整档时间时把当前值补进列表
  const fillMinutes = (m) => {
    const val = Number(m);
    const list = [];
    for (let i = 0; i < 60; i += 5) list.push(i);
    if (Number.isFinite(val) && list.indexOf(val) < 0) list.push(val);
    list.sort((a, b) => a - b);
    minSel.innerHTML = '';
    list.forEach((v) => {
      const op = document.createElement('option');
      op.value = workPad2(v);
      op.textContent = workPad2(v);
      minSel.appendChild(op);
    });
    if (Number.isFinite(val)) minSel.value = workPad2(val);
  };
  fillMinutes(cur.getMinutes());
  timeRow.appendChild(hourSel);
  timeRow.appendChild(workElNew('span', 'work-dt-unit', '时'));
  timeRow.appendChild(minSel);
  timeRow.appendChild(workElNew('span', 'work-dt-unit', '分'));
  wrap.appendChild(timeRow);

  const quickChips = [];
  if (!o.simple) {
    const quickRow = workElNew('div', 'work-dt-quick');
    [['09:00', 9, 0], ['12:00', 12, 0], ['14:00', 14, 0], ['19:00', 19, 0]]
      .forEach(([label, h, m]) => {
        const b = workElNew('button', 'work-dt-chip', label);
        b.type = 'button';
        b.onclick = () => {
          if (!dateIn.value) dateIn.value = workDateValue(new Date());
          hourSel.value = workPad2(h);
          fillMinutes(m);
          sync(true);
        };
        quickChips.push([b, h, m]);
        quickRow.appendChild(b);
      });
    wrap.appendChild(quickRow);
  }

  let lastTs = cur.getTime() / 1000;

  function readTs() {
    const p = String(dateIn.value || '').split('-').map(Number);
    if (p.length !== 3 || !p[0] || !p[1] || !p[2]) return NaN;
    return new Date(p[0], p[1] - 1, p[2],
      Number(hourSel.value), Number(minSel.value), 0, 0).getTime() / 1000;
  }

  function sync(notify) {
    const ts = readTs();
    if (isFinite(ts)) lastTs = ts;
    const dv = dateIn.value;
    dayChips.forEach(([b, off]) => {
      const d = new Date();
      d.setDate(d.getDate() + off);
      b.classList.toggle('on', dv === workDateValue(d));
    });
    const h = Number(hourSel.value);
    const m = Number(minSel.value);
    const todayStr = workDateValue(new Date());
    quickChips.forEach(([b, qh, qm]) => {
      b.classList.toggle('on', dv === todayStr && h === qh && m === qm);
    });
    if (notify && o.onChange) o.onChange(lastTs);
  }

  dateIn.onchange = () => sync(true);
  hourSel.onchange = () => sync(true);
  minSel.onchange = () => sync(true);
  sync(false);

  return {
    el: wrap,
    get() {
      const ts = readTs();
      if (isFinite(ts)) lastTs = ts;
      return lastTs;
    },
    set(ts) {
      const d = new Date(Number(ts) * 1000);
      if (isNaN(d.getTime())) return;
      d.setSeconds(0, 0);
      dateIn.value = workDateValue(d);
      hourSel.value = workPad2(d.getHours());
      fillMinutes(d.getMinutes());
      lastTs = d.getTime() / 1000;
      sync(false);
    },
  };
}

/* ---------------- 生命周期 ---------------- */
function workEnterMode() {
  if (!WORK.bound) workBind();
  // 工作台里的任务确认 / 完成也要能发系统通知；切换模式是用户手势，适合请求权限 + 预热提示音
  if (typeof ensureNotifyPermission === 'function') ensureNotifyPermission();
  if (typeof warmNotifySound === 'function') warmNotifySound();
  const cols = document.querySelector('.work-cols');
  if (cols) cols.scrollTop = 0;      // 每次进来从「今日」顶部开始
  workLoadScene();
  workLoad(true);
  if (WORK.timer) clearInterval(WORK.timer);
  // 日程 / 产物变更走 SSE 推送，30 秒只做一次兜底轮询（断线时也不会停更）
  WORK.timer = setInterval(() => workLoad(false), 30000);
}

function workLeaveMode() {
  if (WORK.timer) { clearInterval(WORK.timer); WORK.timer = null; }
  workCloseDialog();
}

function workOnTaskEvent(task) {
  if (!task || !task.id) return;
  const i = WORK.tasks.findIndex((t) => t.id === task.id);
  if (i >= 0) WORK.tasks[i] = task;
  else WORK.tasks.unshift(task);
  WORK.sig.tasks = JSON.stringify(WORK.tasks);
  if (state.uiMode === 'work') workRenderTasks();
}

/* ---------------- 数据 ---------------- */
async function workLoad(showError) {
  const now = Date.now() / 1000;
  // 产物列表要扫目录，通常比日程/任务慢；先并行发出，但不让首屏等它。
  const artsP = api('/api/work/artifacts', { op: 'list', include_working: WORK.showWorking })
    .catch((e) => ({ __err: e }));
  try {
    const [cal, tasks] = await Promise.all([
      api('/api/calendar/events', {
        op: 'range', start_at: workDayStart(now),
        end_at: workDayStart(now) + 30 * 86400,
      }),
      api('/api/work/tasks', { op: 'list' }),
    ]);
    WORK.events = cal.events || [];
    WORK.tasks = tasks.tasks || [];
    WORK.lastErr = '';
    workRenderAll(!!showError);          // 今日 / 任务先出来
    const arts = await artsP;
    if (arts && arts.__err) {
      if (showError) toastErr('产物加载失败', arts.__err);
    } else if (arts) {
      WORK.artifacts = arts.artifacts || [];
      workRenderAll(!!showError);        // 产物到了再补一列
    }
  } catch (e) {
    if (showError || WORK.lastErr !== e.message) {
      WORK.lastErr = e.message;
      toastErr('工作台加载失败', e);
    }
  }
}

/* ---------------- 渲染 ---------------- */
function workRenderAll(force) {
  // 只有数据真的变了才重绘：4 秒轮询如果每次都重建 DOM，
  // 会让正在看的卡片闪一下，也会让浏览器的元素引用失效（自动化测试踩过）。
  const se = JSON.stringify(WORK.events);
  const st = JSON.stringify(WORK.tasks);
  const sa = JSON.stringify(WORK.artifacts);
  if (force || se !== WORK.sig.events) {
    WORK.sig.events = se;
    workRenderToday();
  }
  if (force || st !== WORK.sig.tasks) {
    WORK.sig.tasks = st;
    workRenderTasks();
  }
  if (force || sa !== WORK.sig.artifacts) {
    WORK.sig.artifacts = sa;
    workRenderArtifacts();
  }
  const sub = workEl('work-sub');
  if (sub) {
    const d = new Date();
    sub.textContent = d.getFullYear() + ' 年 ' + (d.getMonth() + 1) + ' 月 '
      + d.getDate() + ' 日 · ' + WORK.tasks.filter((t) => t.status === 'running').length
      + ' 个任务进行中';
  }
}

function workAgendaRow(e, withDate) {
  const card = workElNew('div', 'work-agenda');
  card.title = '点击编辑';
  card.tabIndex = 0;
  card.setAttribute('role', 'button');
  const time = workElNew('div', 'work-agenda-time' + (withDate ? ' wide' : ''),
    workFmt(e.occurrence_at, withDate));
  const main = workElNew('div', 'work-agenda-main');
  main.appendChild(workElNew('div', 'work-agenda-title', e.title));
  const meta = [];
  if (e.source === 'legacy_reminder') meta.push('提醒');
  if (e.location) meta.push(e.location);
  if (e.rrule) meta.push('重复');
  if (e.source === 'task') meta.push('任务截止');
  if (meta.length) main.appendChild(workElNew('div', 'work-agenda-meta', meta.join(' · ')));
  const actions = workElNew('div', 'work-agenda-actions');
  const del = workElNew('button', 'work-icon-btn', '✕');
  del.title = '删除日程';
  del.onclick = (ev) => {
    ev.stopPropagation();
    workDeleteEvent(e.id, e.title);
  };
  actions.appendChild(del);
  card.appendChild(time);
  card.appendChild(main);
  card.appendChild(actions);
  card.onclick = () => workOpenEventDialog(e);
  card.onkeydown = (ev) => {
    if (ev.key !== 'Enter' && ev.key !== ' ') return;
    ev.preventDefault();
    workOpenEventDialog(e);
  };
  return card;
}

function workWeekday(ts) {
  return '周' + '日一二三四五六'[new Date(Number(ts) * 1000).getDay()];
}

function workRenderToday() {
  const box = workEl('work-today-body');
  if (!box) return;
  box.innerHTML = '';
  const start = workDayStart(Date.now() / 1000);
  const span = WORK.range === 'week' ? 7 : 1;
  const end = start + span * 86400;
  const today = WORK.events.filter((e) => e.occurrence_at >= start
    && e.occurrence_at < start + 86400);
  const badge = workEl('work-today-count');
  if (badge) {
    const n = WORK.events.filter((e) => e.occurrence_at >= start
      && e.occurrence_at < end).length;
    badge.textContent = String(n);
    badge.title = WORK.range === 'week' ? ('未来 7 天 ' + n + ' 条') : ('今日 ' + n + ' 条');
  }

  if (WORK.range === 'week') {
    for (let i = 0; i < 7; i++) {
      const dayStart = start + i * 86400;
      const items = WORK.events.filter((e) => e.occurrence_at >= dayStart
        && e.occurrence_at < dayStart + 86400);
      const d = new Date(dayStart * 1000);
      const label = (i === 0 ? '今天 · ' : '') + (d.getMonth() + 1) + ' 月 '
        + d.getDate() + ' 日 · ' + workWeekday(dayStart);
      box.appendChild(workElNew('div', 'work-agenda-sep', label));
      if (!items.length) {
        box.appendChild(workElNew('div', 'work-agenda-empty', '无日程'));
        continue;
      }
      items.forEach((e) => box.appendChild(workAgendaRow(e, false)));
    }
    return;
  }

  const upcoming = WORK.events.filter((e) => e.occurrence_at >= start + 86400
    && e.occurrence_at < start + 30 * 86400).slice(0, 8);
  if (!today.length && !upcoming.length) {
    box.appendChild(workEmpty('还没有日程', '直接在上方输入「明天 10 点 开会」试试'));
    return;
  }
  if (!today.length) {
    box.appendChild(workElNew('div', 'work-agenda-empty', '今天没有日程'));
  }
  today.forEach((e) => box.appendChild(workAgendaRow(e, false)));
  if (upcoming.length) {
    box.appendChild(workElNew('div', 'work-agenda-sep', '接下来'));
    upcoming.forEach((e) => box.appendChild(workAgendaRow(e, true)));
  }
}

function workStatusText(s) {
  return ({ pending: '待开始', running: '进行中', paused: '已暂停',
    done: '已完成', failed: '失败', canceled: '已取消' })[s] || s;
}

function workVisibleTasks() {
  const q = WORK.taskQuery.trim().toLowerCase();
  return WORK.tasks.filter((t) => {
    if (WORK.taskFilter && t.status !== WORK.taskFilter) return false;
    if (!q) return true;
    return (t.title || '').toLowerCase().includes(q)
      || (t.tags || '').toLowerCase().includes(q)
      || (t.intent || '').toLowerCase().includes(q);
  });
}

function workPriorityChip(p) {
  const n = Math.min(3, Number(p) || 0);
  if (n <= 0) return null;
  const el = workElNew('span', 'work-pri p' + n, 'P' + n);
  el.title = ['', '低', '中', '高'][n] + '优先级';
  return el;
}

function workRenderTasks() {
  const box = workEl('work-task-body');
  if (!box) return;
  box.innerHTML = '';
  const list = workVisibleTasks();
  const badge = workEl('work-task-count');
  if (badge) {
    badge.textContent = String(list.length);
    badge.title = list.length === WORK.tasks.length
      ? ('共 ' + WORK.tasks.length + ' 个任务')
      : ('筛选后 ' + list.length + ' / 共 ' + WORK.tasks.length);
  }
  if (!list.length) {
    box.appendChild(workEmpty(
      WORK.tasks.length ? '没有匹配的任务' : '还没有任务',
      WORK.tasks.length ? '换个关键词或筛选条件'
        : '在对话里让 AI 拆解，或点右上角「＋ 任务」'));
    return;
  }
  const order = { running: 0, pending: 1, paused: 2, failed: 3, done: 4, canceled: 5 };
  list.slice().sort((a, b) => {
    const pa = Number(a.priority) || 0;
    const pb = Number(b.priority) || 0;
    if (pa !== pb) return pb - pa;              // 优先级高的在前
    return (order[a.status] ?? 9) - (order[b.status] ?? 9);
  }).forEach((t) => box.appendChild(workTaskCard(t)));
}

function workTaskCard(t) {
  const card = workElNew('div', 'work-task' + (t.status === 'done' ? ' done' : ''));
  const head = workElNew('div', 'work-task-head');
  head.appendChild(workElNew('span', 'work-task-title', t.title));
  const pri = workPriorityChip(t.priority);
  if (pri) head.appendChild(pri);
  head.appendChild(workElNew('span', 'work-chip ' + t.status, workStatusText(t.status)));
  card.appendChild(head);

  const bar = workElNew('div', 'work-bar');
  const fill = workElNew('i', 'work-bar-fill');
  fill.style.width = (t.progress ? t.progress.percent : 0) + '%';
  bar.appendChild(fill);
  card.appendChild(bar);

  const meta = workElNew('div', 'work-task-meta');
  const done = t.progress ? t.progress.done : 0;
  const total = t.progress ? t.progress.total : (t.steps || []).length;
  const due = t.meta && t.meta.due_at;
  const bits = [done + '/' + total + ' 步'];
  if (t.tags) bits.push(t.tags);
  if (due) {
    bits.push('截止 ' + (typeof fmtTaskTime === 'function'
      ? fmtTaskTime(due) : new Date(due * 1000).toLocaleString()));
  }
  if (t.error) bits.push(t.error.slice(0, 60));
  meta.textContent = bits.join(' · ');
  card.appendChild(meta);

  card.onclick = () => workOpenTask(t.id);
  card.tabIndex = 0;
  card.setAttribute('role', 'button');
  card.onkeydown = (e) => {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    e.preventDefault();
    workOpenTask(t.id);
  };
  return card;
}

function workSizeText(n) {
  const v = Number(n) || 0;
  if (v <= 0) return '';
  if (v < 1024) return v + ' B';
  if (v < 1024 * 1024) return (v / 1024).toFixed(1) + ' KB';
  return (v / 1024 / 1024).toFixed(1) + ' MB';
}

function workRenderArtifacts() {
  const box = workEl('work-artifact-body');
  if (!box) return;
  box.innerHTML = '';
  const badge = workEl('work-artifact-count');
  if (badge) badge.textContent = String(WORK.artifacts.length);
  if (!WORK.artifacts.length) {
    box.appendChild(workEmpty('还没有产物',
      WORK.showWorking ? '任务写出的文件会出现在这里' : '点右上角「交付物」可显示临时文件'));
    return;
  }
  // 按任务分组：同一个任务产出的文件放在一起，先看交付物再看是谁做的
  const groups = new Map();
  WORK.artifacts.forEach((a) => {
    const key = a.task_id || '';
    if (!groups.has(key)) {
      groups.set(key, {
        title: a.task_title || (a.task_id ? '任务 ' + a.task_id : '未关联任务'),
        items: [],
      });
    }
    groups.get(key).items.push(a);
  });
  groups.forEach((g) => {
    box.appendChild(workElNew('div', 'work-artifact-group', g.title));
    g.items.forEach((a) => box.appendChild(workArtifactRow(a)));
  });
}

function workArtifactRow(a) {
  const row = workElNew('div', 'work-artifact');
  row.title = a.path || a.name;
  row.tabIndex = 0;
  row.setAttribute('role', 'button');
  const main = workElNew('div', 'work-artifact-main');
  main.appendChild(workElNew('div', 'work-artifact-name', a.name));
  const sub = [];
  if (a.path) sub.push(a.path);
  const sz = workSizeText(a.size);
  if (sz) sub.push(sz);
  if (a.kind === 'working') sub.push('临时');
  if (sub.length) main.appendChild(workElNew('div', 'work-artifact-path', sub.join(' · ')));
  row.appendChild(main);

  const acts = workElNew('div', 'work-artifact-acts');
  const locate = workElNew('button', 'work-btn small ghost', '定位');
  locate.title = '打开所在文件夹';
  locate.onclick = async (ev) => {
    ev.stopPropagation();
    await workRevealArtifact(a.id);
  };
  const del = workElNew('button', 'work-icon-btn', '✕');
  del.title = '从产物栏移除（不会删除磁盘文件）';
  del.onclick = async (ev) => {
    ev.stopPropagation();
    await workDeleteArtifact(a.id, a.name);
  };
  acts.appendChild(locate);
  acts.appendChild(del);
  row.appendChild(acts);

  const open = async () => {
    try {
      await workPreviewArtifact(a.id);
    } catch (e) {
      toastErr('预览失败', e);
    }
  };
  row.onclick = open;
  row.onkeydown = (ev) => {
    if (ev.key !== 'Enter' && ev.key !== ' ') return;
    ev.preventDefault();
    open();
  };
  return row;
}

async function workRevealArtifact(id) {
  try {
    const r = await api('/api/work/artifacts', { op: 'reveal', id });
    if (!r.ok) throw new Error(r.error || '打开失败');
  } catch (e) {
    toastErr('打开所在文件夹失败', e);
  }
}

async function workDeleteArtifact(id, name) {
  if (!window.confirm('从产物栏移除「' + name + '」？\n只会删掉记录，磁盘上的文件不会动。')) return;
  try {
    const r = await api('/api/work/artifacts', { op: 'delete', id });
    if (!r.ok) throw new Error(r.error || '移除失败');
    await workLoad(true);
    toast('已从产物栏移除', 'success');
  } catch (e) {
    toastErr('移除产物失败', e);
  }
}

function workEmpty(title, sub) {
  const box = workElNew('div', 'work-empty');
  box.appendChild(workElNew('div', 'work-empty-title', title));
  if (sub) box.appendChild(workElNew('div', 'work-empty-sub', sub));
  return box;
}

/* ---------------- 弹窗 ---------------- */
function workOpenDialog(title, build, onOk, opts) {
  const body = workEl('work-dialog-body');
  if (!body) return;
  body.innerHTML = '';
  build(body);
  workEl('work-dialog-title').textContent = title;
  const o = opts || {};
  const okBtn = workEl('work-dialog-ok');
  const cancelBtn = workEl('work-dialog-cancel');
  if (okBtn) okBtn.textContent = o.okText || '保存';
  if (cancelBtn) {
    cancelBtn.textContent = o.cancelText || '取消';
    cancelBtn.classList.toggle('hidden', !!o.hideCancel);
  }
  workEl('work-modal').classList.remove('hidden');
  WORK.onOk = onOk;
  const first = body.querySelector('input, textarea, select');
  if (first) setTimeout(() => first.focus(), 30);
}

function workCloseDialog() {
  const m = workEl('work-modal');
  if (m) m.classList.add('hidden');
  WORK.onOk = null;
  WORK.currentTask = null;
}

function workField(box, label, input) {
  const wrap = workElNew('label', 'work-field');
  wrap.appendChild(workElNew('span', 'work-field-label', label));
  wrap.appendChild(input);
  box.appendChild(wrap);
  return input;
}

// 时间选择器包含多个控件，用 div 包一层：label 会把点击转发给第一个控件
function workFieldBox(box, label, el) {
  const wrap = workElNew('div', 'work-field');
  wrap.appendChild(workElNew('span', 'work-field-label', label));
  wrap.appendChild(el);
  box.appendChild(wrap);
  return wrap;
}

function workRruleKind(rrule) {
  const s = String(rrule || '').toLowerCase();
  if (s.includes('daily')) return 'daily';
  if (s.includes('weekly')) return 'weekly';
  if (s.includes('monthly')) return 'monthly';
  return '';
}

function workOpenEventDialog(ev) {
  const editing = !!ev;
  const now = Date.now() / 1000;
  const start = editing ? Number(ev.occurrence_at) : Math.ceil(now / 3600) * 3600;
  const end = editing ? Number(ev.occurrence_end || start + 3600) : start + 3600;
  // 常用时长：命中整档就直接选中，否则落到「自定义」
  const DURS = [[30, '30 分钟'], [60, '1 小时'], [120, '2 小时'], [180, '3 小时']];
  let titleEl, locEl, repeatEl, remindEl, startPick, endPick, endField;
  workOpenDialog(editing ? '编辑日程' : '新建日程', (body) => {
    titleEl = workField(body, '标题', workElNew('input', 'work-input'));
    titleEl.placeholder = '例如：产品周会';
    titleEl.value = editing ? (ev.title || '') : '';

    let mode = 'custom';
    DURS.forEach(([m]) => {
      if (Math.round((end - start) / 60) === m) mode = String(m);
    });
    // 结束时间由「开始 + 时长」算出来；只有自定义模式才让用户直接改
    endPick = workDateTimePicker(end, { simple: true });
    startPick = workDateTimePicker(start, {
      onChange: (ts) => {
        if (mode === 'custom') {
          if (endPick.get() <= ts) endPick.set(ts + 1800);
        } else {
          endPick.set(ts + Number(mode) * 60);
        }
      },
    });
    workFieldBox(body, '开始', startPick.el);

    const durChips = workElNew('div', 'work-dt-chips');
    workFieldBox(body, '时长', durChips);
    const durBtns = DURS.map(([m, label]) => {
      const b = workElNew('button', 'work-dt-chip', label);
      b.type = 'button';
      b.onclick = () => {
        mode = String(m);
        endPick.set(startPick.get() + m * 60);
        durBtns.forEach(([btn, v]) => btn.classList.toggle('on', v === mode));
        endField.hidden = true;
      };
      durChips.appendChild(b);
      return [b, String(m)];
    });
    const customBtn = workElNew('button', 'work-dt-chip', '自定义');
    customBtn.type = 'button';
    customBtn.onclick = () => {
      mode = 'custom';
      durBtns.forEach(([btn, v]) => btn.classList.toggle('on', v === mode));
      if (endPick.get() <= startPick.get()) endPick.set(startPick.get() + 3600);
      endField.hidden = false;
    };
    durChips.appendChild(customBtn);
    durBtns.push([customBtn, 'custom']);
    durBtns.forEach(([b, v]) => b.classList.toggle('on', v === mode));

    endField = workFieldBox(body, '结束', endPick.el);
    endField.hidden = mode !== 'custom';

    locEl = workField(body, '地点（可选）', workElNew('input', 'work-input'));
    locEl.placeholder = '例如：3 楼会议室';
    locEl.value = editing ? (ev.location || '') : '';
    repeatEl = workField(body, '重复', workElNew('select', 'work-input'));
    [['', '不重复'], ['daily', '每天'], ['weekly', '每周'], ['monthly', '每月']]
      .forEach(([v, t]) => {
        const o = document.createElement('option');
        o.value = v; o.textContent = t; repeatEl.appendChild(o);
      });
    repeatEl.value = editing ? workRruleKind(ev.rrule) : '';
    remindEl = workField(body, '提醒', workElNew('select', 'work-input'));
    const remindOpts = [[0, '不提醒'], [5, '提前 5 分钟'], [10, '提前 10 分钟'],
      [30, '提前 30 分钟'], [60, '提前 1 小时']];
    const curRemind = editing ? Number(ev.remind_before || 0) : 10;
    if (curRemind && !remindOpts.some(([v]) => v === curRemind)) {
      remindOpts.push([curRemind, '提前 ' + Math.round(curRemind / 60) + ' 分钟']);
    }
    remindOpts.forEach(([v, t]) => {
      const o = document.createElement('option');
      o.value = String(v); o.textContent = t; remindEl.appendChild(o);
    });
    remindEl.value = String(curRemind);
    if (editing && ev.rrule) {
      body.appendChild(workElNew('div', 'work-hint',
        '这是一条重复日程，保存后会修改整个系列的时间。'));
    }
  }, async () => {
    const title = titleEl.value.trim();
    if (!title) { toast('请填写标题', 'error'); return false; }
    const startTs = startPick.get();
    const endTs = endPick.get();
    if (endTs <= startTs) { toast('结束时间必须晚于开始时间', 'error'); return false; }
    const payload = {
      op: editing ? 'update' : 'create',
      title,
      start_at: workLocalInput(startTs).replace('T', ' '),
      end_at: workLocalInput(endTs).replace('T', ' '),
      location: locEl.value.trim(),
      rrule: repeatEl.value,
      remind_before: Number(remindEl.value || 0),
    };
    if (editing) payload.id = ev.id;
    try {
      const r = await api('/api/calendar/events', payload);
      if (!r.ok) throw new Error(r.error || (editing ? '保存失败' : '创建失败'));
      if (!editing && r.event && r.event._conflicts && r.event._conflicts.length) {
        toast('已创建，但与 ' + r.event._conflicts.length + ' 个日程重叠', 'error');
      } else {
        toast(editing ? '日程已更新' : '日程已创建', 'success');
      }
      await workLoad(true);
      return true;
    } catch (e) {
      toastErr(editing ? '保存日程失败' : '创建日程失败', e);
      return false;
    }
  });
}

function workOpenTaskDialog() {
  let duePick = null;
  workOpenDialog('新建任务', (body) => {
    const title = workField(body, '任务标题', workElNew('input', 'work-input'));
    title.id = 'work-task-title';
    title.placeholder = '例如：整理本周会议纪要';
    const ta = workField(body, '步骤（一行一个）', workElNew('textarea', 'work-input work-textarea'));
    ta.id = 'work-task-steps';
    ta.rows = 6;
    ta.placeholder = '收集材料\n写初稿\n校对并发出';
    // 截止时间默认关闭：不勾选就不建截止日程
    const dueOn = workElNew('label', 'work-check');
    const dueCb = document.createElement('input');
    dueCb.type = 'checkbox';
    dueCb.id = 'work-task-due-on';
    dueOn.appendChild(dueCb);
    dueOn.appendChild(document.createTextNode(' 设置截止时间（会同步到日程并提醒）'));
    body.appendChild(dueOn);
    duePick = workDateTimePicker(Math.ceil(Date.now() / 3600000) * 3600 + 3600,
      { simple: true });
    const dueField = workFieldBox(body, '截止时间', duePick.el);
    dueField.hidden = true;
    dueCb.onchange = () => { dueField.hidden = !dueCb.checked; };
    const pri = workField(body, '优先级', workSelect(
      [['0', '普通'], ['1', '低'], ['2', '中'], ['3', '高']]));
    pri.id = 'work-task-priority';
    const tags = workField(body, '标签（可选）', workElNew('input', 'work-input'));
    tags.id = 'work-task-tags';
    tags.placeholder = '例如：工作, 周报';
    const remind = workField(body, '提前提醒', workElNew('select', 'work-input'));
    remind.id = 'work-task-remind';
    [[0, '不提醒'], [5, '提前 5 分钟'], [10, '提前 10 分钟'],
      [30, '提前 30 分钟'], [60, '提前 1 小时']].forEach(([v, t]) => {
      const o = document.createElement('option');
      o.value = String(v); o.textContent = t; remind.appendChild(o);
    });
    remind.value = '10';
    const auto = workElNew('label', 'work-check');
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.id = 'work-task-auto';
    auto.appendChild(cb);
    auto.appendChild(document.createTextNode(' 创建后立即开始'));
    body.appendChild(auto);
    body.appendChild(workElNew('div', 'work-hint',
      '单轮内能做完的，直接在对话里让 Agent 做；需要跨轮、暂停、留产物的用任务。'));
  }, async () => {
    const title = workEl('work-task-title').value.trim();
    const steps = workEl('work-task-steps').value.split('\n')
      .map((s) => s.trim()).filter(Boolean);
    const hasDue = workEl('work-task-due-on').checked;
    const due = hasDue ? workLocalInput(duePick.get()).replace('T', ' ') : '';
    const remindMin = Number(workEl('work-task-remind').value || 0);
    const auto = workEl('work-task-auto').checked;
    if (!title) { toast('请填写任务标题', 'error'); return false; }
    if (!steps.length) { toast('至少写一个步骤', 'error'); return false; }
    try {
      const payload = {
        op: 'create', title, steps, auto_start: auto,
        priority: Number(workEl('work-task-priority').value || 0),
        tags: workEl('work-task-tags').value.trim(),
      };
      if (due) {
        payload.due_at = due;
        payload.remind_before = remindMin * 60;
      }
      const r = await api('/api/work/tasks', payload);
      if (!r.ok) throw new Error(r.error || '创建失败');
      toast(due ? '任务已创建，并同步了截止日程' : '任务已创建', 'success');
      await workLoad(true);
      return true;
    } catch (e) {
      toastErr('创建任务失败', e);
      return false;
    }
  });
}

/* ---------- 图片生成 ---------- */
function workOpenImageDialog() {
  let size = 'square';
  let promptEl = null;
  workOpenDialog('生成图片', (body) => {
    promptEl = workField(body, '提示词',
      workElNew('textarea', 'work-input work-textarea'));
    promptEl.rows = 5;
    promptEl.placeholder = '描述你想要的画面，例如：雨夜的霓虹街道，电影感，35mm';
    const chips = workElNew('div', 'work-dt-chips');
    workFieldBox(body, '尺寸', chips);
    const opts = [
      ['square', '方形 1024²'],
      ['portrait', '竖图 832×1216'],
      ['landscape', '横图 1216×832'],
    ];
    const btns = opts.map(([v, label]) => {
      const b = workElNew('button', 'work-dt-chip' + (v === size ? ' on' : ''), label);
      b.type = 'button';
      b.onclick = () => {
        size = v;
        btns.forEach(([btn, val]) => btn.classList.toggle('on', val === size));
      };
      chips.appendChild(b);
      return [b, v];
    });
    body.appendChild(workElNew('div', 'work-hint',
      '本地 FLUX 3 Image Klein（16 步）。首次生成要加载模型，可能多等十几秒；'
      + '结果会放进右侧「产物」栏，点开可预览。'));
  }, async () => {
    const prompt = promptEl.value.trim();
    if (!prompt) { toast('请填写提示词', 'error'); return false; }
    const okBtn = workEl('work-dialog-ok');
    const label = okBtn ? okBtn.textContent : '生成';
    if (okBtn) { okBtn.disabled = true; okBtn.textContent = '生成中…'; }
    try {
      const r = await api('/api/image/generate', { prompt, size });
      if (!r.ok) throw new Error(r.error || '生成失败');
      await workLoad(true);
      toast('图片已生成，见「产物」栏', 'success');
      const art = r.artifact;
      workCloseDialog();
      if (art && art.id) workPreviewArtifact(art.id).catch(() => {});
      return false;                       // 弹窗已在上面关掉
    } catch (e) {
      toastErr('生成图片失败', e);
      if (okBtn) { okBtn.disabled = false; okBtn.textContent = label || '生成'; }
      return false;
    }
  }, { okText: '生成' });
}

async function workOpenTask(id) {
  try {
    const r = await api('/api/work/tasks', { op: 'get', id });
    if (!r.ok || !r.task) throw new Error(r.error || '找不到任务');
    WORK.currentTask = r.task;
    workRenderTaskDialog();
  } catch (e) {
    toastErr('打开任务失败', e);
  }
}

function workTaskEditable(t) {
  return ['pending', 'paused', 'failed'].includes(t.status);
}

function workRenderTaskDialog() {
  const t = WORK.currentTask;
  if (!t) return;
  const editable = workTaskEditable(t);
  workOpenDialog('任务详情', (body) => {
    const top = workElNew('div', 'work-detail-top');
    top.appendChild(workElNew('div', 'work-detail-title', t.title));
    const pri = workPriorityChip(t.priority);
    if (pri) top.appendChild(pri);
    const editInfo = workElNew('button', 'work-btn small ghost', '编辑信息');
    editInfo.title = '修改标题 / 优先级 / 标签';
    editInfo.onclick = () => workOpenTaskInfoDialog(t);
    top.appendChild(editInfo);
    body.appendChild(top);

    const meta = workElNew('div', 'work-detail-meta');
    const bits = [workStatusText(t.status),
      t.progress.done + '/' + t.progress.total + ' 步'];
    if (t.tags) bits.push(t.tags);
    if (t.meta && t.meta.due_at) {
      bits.push('截止 ' + (typeof fmtTaskTime === 'function'
        ? fmtTaskTime(t.meta.due_at) : new Date(t.meta.due_at * 1000).toLocaleString()));
    }
    meta.textContent = bits.join(' · ');
    body.appendChild(meta);

    (t.steps || []).forEach((s, i) => {
      const row = workElNew('div', 'work-step ' + s.status);
      row.appendChild(workElNew('span', 'work-step-idx', String(i + 1)));
      const mid = workElNew('div', 'work-step-main');
      mid.appendChild(workElNew('div', 'work-step-title', s.title));
      const note = [];
      if (s.tool) note.push(s.tool);
      if (s.retry) note.push('已重试 ' + s.retry + ' 次');
      if (s.result) note.push(s.result.slice(0, 80));
      if (s.error) note.push(s.error.slice(0, 80));
      if (note.length) mid.appendChild(workElNew('div', 'work-step-note', note.join(' · ')));
      row.appendChild(mid);
      row.appendChild(workElNew('span', 'work-chip ' + s.status, workStatusText(s.status)));

      if (s.status === 'blocked') {
        const ok = workElNew('button', 'work-btn primary small', '确认执行');
        ok.onclick = () => workTaskAct(t.id, 'confirm_step', { step_id: s.id });
        row.appendChild(ok);
      } else if (!s.tool && s.status === 'pending') {
        const ok = workElNew('button', 'work-btn small', '标记完成');
        ok.onclick = () => workTaskAct(t.id, 'complete_step', { step_id: s.id });
        row.appendChild(ok);
      }
      const locked = ['done', 'running'].includes(s.status);
      if (editable && !locked) {
        const tools = workElNew('span', 'work-step-tools');
        const up = workElNew('button', 'work-icon-btn', '↑');
        up.title = '上移';
        up.onclick = () => workTaskAct(t.id, 'move_step', { step_id: s.id, delta: -1 });
        const down = workElNew('button', 'work-icon-btn', '↓');
        down.title = '下移';
        down.onclick = () => workTaskAct(t.id, 'move_step', { step_id: s.id, delta: 1 });
        const edit = workElNew('button', 'work-btn small ghost', '改');
        edit.title = '编辑这一步';
        edit.onclick = () => workOpenStepDialog(t, s);
        const del = workElNew('button', 'work-icon-btn', '✕');
        del.title = '删除这一步';
        del.onclick = () => {
          if (!window.confirm('删除步骤「' + s.title + '」？')) return;
          workTaskAct(t.id, 'delete_step', { step_id: s.id });
        };
        tools.appendChild(up);
        tools.appendChild(down);
        tools.appendChild(edit);
        tools.appendChild(del);
        row.appendChild(tools);
      }
      body.appendChild(row);
    });

    if (editable) {
      const add = workElNew('button', 'work-btn small', '＋ 添加步骤');
      add.onclick = () => workOpenStepDialog(t, null);
      body.appendChild(add);
    }

    const actions = workElNew('div', 'work-detail-actions');
    if (t.status === 'pending') {
      const b = workElNew('button', 'work-btn primary', '开始');
      b.onclick = () => workTaskAct(t.id, 'start');
      actions.appendChild(b);
    } else if (t.status === 'paused') {
      const b = workElNew('button', 'work-btn primary', '继续');
      b.onclick = () => workTaskAct(t.id, 'resume');
      actions.appendChild(b);
    } else if (t.status === 'failed') {
      const b = workElNew('button', 'work-btn primary', '重试失败步骤');
      b.onclick = () => workTaskAct(t.id, 'retry');
      actions.appendChild(b);
    }
    if (t.status === 'running') {
      const b = workElNew('button', 'work-btn', '暂停');
      b.onclick = () => workTaskAct(t.id, 'pause');
      actions.appendChild(b);
    }
    if (!['done', 'canceled'].includes(t.status)) {
      const b = workElNew('button', 'work-btn ghost', '取消任务');
      b.onclick = () => workTaskAct(t.id, 'cancel');
      actions.appendChild(b);
    }
    if (t.status !== 'running') {
      const b = workElNew('button', 'work-btn ghost', '删除任务');
      b.onclick = () => {
        if (!window.confirm('删除任务「' + t.title + '」？\n任务记录与产物登记会一起删除。')) return;
        workTaskAct(t.id, 'delete');
      };
      actions.appendChild(b);
    }
    body.appendChild(actions);
  }, async () => true, { okText: '关闭', hideCancel: true });
}

async function workTaskAct(id, op, extra) {
  try {
    const r = await api('/api/work/tasks', Object.assign({ op, id }, extra || {}));
    if (!r.ok) throw new Error(r.error || '操作失败');
    if (op === 'delete') {
      WORK.currentTask = null;
      await workLoad(true);
      workCloseDialog();
      toast('任务已删除', 'success');
      return;
    }
    WORK.currentTask = r.task;
    await workLoad(true);
    if (WORK.currentTask) workRenderTaskDialog();
    toast('已' + ({ start: '开始', pause: '暂停', resume: '继续', cancel: '取消',
      retry: '重试', confirm_step: '确认', complete_step: '完成',
      move_step: '移动', delete_step: '删除步骤' }[op] || '操作'), 'success');
  } catch (e) {
    toastErr('任务操作失败', e);
  }
}

function workOpenTaskInfoDialog(t) {
  workOpenDialog('编辑任务', (body) => {
    const title = workField(body, '标题', workElNew('input', 'work-input'));
    title.value = t.title || '';
    const pri = workField(body, '优先级', workSelect(
      [['0', '普通'], ['1', '低'], ['2', '中'], ['3', '高']]));
    pri.value = String(Math.min(3, Number(t.priority) || 0));
    const tags = workField(body, '标签', workElNew('input', 'work-input'));
    tags.placeholder = '例如：工作, 周报';
    tags.value = t.tags || '';
  }, async () => {
    const body = workEl('work-dialog-body');
    const inputs = body.querySelectorAll('input, select');
    const title = inputs[0].value.trim();
    if (!title) { toast('标题不能为空', 'error'); return false; }
    try {
      const r = await api('/api/work/tasks', {
        op: 'update', id: t.id, title,
        priority: Number(inputs[1].value || 0),
        tags: inputs[2].value.trim(),
      });
      if (!r.ok) throw new Error(r.error || '保存失败');
      WORK.currentTask = r.task;
      await workLoad(true);
      workCloseDialog();
      WORK.currentTask = r.task;      // closeDialog 会清空，重新挂上再渲染
      workRenderTaskDialog();
      toast('任务信息已更新', 'success');
      return false;
    } catch (e) {
      toastErr('保存任务失败', e);
      return false;
    }
  });
}

async function workCapList() {
  if (WORK.caps) return WORK.caps;
  try {
    const r = await api('/api/work/caps', {});
    WORK.caps = r.items || [];
  } catch (e) {
    WORK.caps = [];
  }
  return WORK.caps;
}

async function workOpenStepDialog(t, step) {
  const editing = !!step;
  const caps = await workCapList();
  const choices = [['', '（手动步骤：由你点「标记完成」）']]
    .concat(caps.map((c) => [c.id, c.id + (c.perm === 'write' ? ' · 写' : '') + ' — ' + c.desc]));
  workOpenDialog(editing ? '编辑步骤' : '添加步骤', (body) => {
    const title = workField(body, '步骤标题', workElNew('input', 'work-input'));
    title.value = editing ? step.title : '';
    title.placeholder = '例如：整理本周会议纪要';
    const tool = workField(body, '绑定能力', workSelect(choices));
    tool.value = editing ? (step.tool || '') : '';
    const args = workField(body, '参数 JSON（可选）',
      workElNew('textarea', 'work-input work-textarea'));
    args.rows = 4;
    args.placeholder = '例如 {"path": "report.md", "content": "..."}';
    args.value = editing && step.args && Object.keys(step.args).length
      ? JSON.stringify(step.args, null, 2) : '';
  }, async () => {
    const body = workEl('work-dialog-body');
    const inputs = body.querySelectorAll('input, select, textarea');
    const title = inputs[0].value.trim();
    if (!title) { toast('步骤标题不能为空', 'error'); return false; }
    const tool = inputs[1].value;
    const raw = inputs[2].value.trim();
    let argsObj = {};
    if (raw) {
      try {
        argsObj = JSON.parse(raw);
      } catch (e) {
        toast('参数不是合法 JSON', 'error');
        return false;
      }
    }
    const payload = editing
      ? { op: 'update_step', id: t.id, step_id: step.id, title, tool, args: argsObj }
      : { op: 'add_step', id: t.id, title, tool, args: argsObj };
    try {
      const r = await api('/api/work/tasks', payload);
      if (!r.ok) throw new Error(r.error || '保存失败');
      WORK.currentTask = r.task;
      await workLoad(true);
      workCloseDialog();
      WORK.currentTask = r.task;      // closeDialog 会清空，重新挂上再渲染
      workRenderTaskDialog();
      toast(editing ? '步骤已更新' : '步骤已添加', 'success');
      return false;
    } catch (e) {
      toastErr('保存步骤失败', e);
      return false;
    }
  });
}

async function workDeleteEvent(id, title) {
  if (!window.confirm('删除日程「' + title + '」？')) return;
  try {
    const r = await api('/api/calendar/events', { op: 'delete', id });
    if (!r.ok) throw new Error('删除失败');
    await workLoad(true);
    toast('日程已删除', 'success');
  } catch (e) {
    toastErr('删除日程失败', e);
  }
}

async function workCleanupEvents() {
  if (!window.confirm('清理所有已结束的一次性日程？重复日程会保留，操作不可恢复。')) return;
  try {
    const r = await api('/api/calendar/events', { op: 'cleanup' });
    if (!r.ok) throw new Error(r.error || '清理失败');
    await workLoad(true);
    toast(r.removed ? ('已清理 ' + r.removed + ' 条过期日程') : '没有可清理的过期日程',
      'success');
  } catch (e) {
    toastErr('清理日程失败', e);
  }
}

/* ---------------- 数据备份 ---------------- */
function workBackupName() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, '0');
  return 'galgame-backup-' + d.getFullYear() + p(d.getMonth() + 1) + p(d.getDate())
    + '-' + p(d.getHours()) + p(d.getMinutes()) + p(d.getSeconds()) + '.zip';
}

function workBackupProgress(btn, bytes, total) {
  if (!btn) return;
  const mb = (bytes / 1048576).toFixed(1);
  btn.textContent = total
    ? ('导出中 ' + mb + ' / ' + (total / 1048576).toFixed(1) + ' MB')
    : ('导出中 ' + mb + ' MB');
}

async function workBackupExport(btn) {
  const name = workBackupName();
  const label = btn ? btn.textContent : '';
  const reset = () => { if (btn) { btn.disabled = false; btn.textContent = label || '导出备份'; } };
  if (btn) { btn.disabled = true; btn.textContent = '正在打包…'; }

  // Electron 壳：主进程弹保存框并把响应流式写盘。
  // 不先把 500MB 的 zip 读进渲染进程，也不会出现「点了没反应」。
  const gw = window.gmWin;
  if (gw && typeof gw.saveUrl === 'function') {
    let off = null;
    try {
      if (typeof gw.onSaveProgress === 'function') {
        off = gw.onSaveProgress((p) => workBackupProgress(btn, p.bytes || 0, p.total || 0));
      }
      const r = await gw.saveUrl({
        url: location.origin + '/api/backup/export',
        name,
        method: 'POST',
        headers: { 'X-Auth-Token': TOKEN },
        title: '保存备份',
      });
      if (r && r.ok) toast('备份已保存：' + r.path, 'success');
      else if (r && r.canceled) toast('已取消导出');
      else throw new Error((r && r.error) || '导出失败');
    } catch (e) {
      toastErr('导出备份失败', e);
    } finally {
      if (off) off();
      reset();
    }
    return;
  }

  // 浏览器：先选保存位置再请求。showSaveFilePicker 需要用户手势，
  // 必须放在 await fetch 之前，否则会被当成「非用户触发」而失败。
  if (typeof window.showSaveFilePicker === 'function') {
    let handle = null;
    try {
      handle = await window.showSaveFilePicker({
        suggestedName: name,
        types: [{ description: 'ZIP 备份', accept: { 'application/zip': ['.zip'] } }],
      });
    } catch (e) {
      if (e && e.name === 'AbortError') { reset(); toast('已取消导出'); return; }
      handle = null;                     // 选择器不可用就退回普通下载
    }
    if (handle) {
      try {
        const res = await fetch('/api/backup/export', {
          method: 'POST', headers: { 'X-Auth-Token': TOKEN },
        });
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const total = Number(res.headers.get('Content-Length')) || 0;
        const writable = await handle.createWritable();
        const reader = res.body.getReader();
        let got = 0;
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          await writable.write(value);
          got += value.length;
          workBackupProgress(btn, got, total);
        }
        await writable.close();
        toast('备份已保存', 'success');
      } catch (e) {
        toastErr('导出备份失败', e);
      } finally {
        reset();
      }
      return;
    }
  }

  try {
    const res = await fetch('/api/backup/export', {
      method: 'POST', headers: { 'X-Auth-Token': TOKEN },
    });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    // 大文件还没下完就 revoke 可能中断下载，留足时间
    setTimeout(() => URL.revokeObjectURL(url), 60000);
    toast('已开始下载到浏览器默认下载目录', 'success');
  } catch (e) {
    toastErr('导出备份失败', e);
  } finally {
    reset();
  }
}

async function workBackupPreflight(file) {
  const info = workEl('backup-info');
  const restore = workEl('backup-restore');
  try {
    if (info) info.textContent = '正在预检…';
    if (restore) restore.disabled = true;
    const buf = await file.arrayBuffer();
    const res = await fetch('/api/backup/preflight', {
      method: 'POST',
      headers: { 'X-Auth-Token': TOKEN, 'Content-Type': 'application/zip' },
      body: buf,
    });
    const r = await res.json();
    if (!res.ok || !r.ok) throw new Error(r.error || '预检失败');
    WORK.backupFile = file;
    const s = r.summary || {};
    if (info) {
      info.textContent = '预检通过：会话 ' + (s.sessions || 0) +
        ' · 知识库 ' + (s.knowledge || 0) + ' · 笔记 ' + (s.notes || 0) +
        ' · 附件 ' + (s.uploads || 0) + ' · 存档 ' + (s.saves || 0) +
        ' · 任务 ' + (s.tasks || 0);
    }
    if (restore) restore.disabled = false;
    toast('预检通过', 'success');
  } catch (e) {
    WORK.backupFile = null;
    if (restore) restore.disabled = true;
    if (info) info.textContent = '预检失败：' + e.message;
    toastErr('预检失败', e);
  }
}

async function workBackupRestore() {
  const file = WORK.backupFile;
  if (!file) { toast('请先选择并预检备份文件', 'error'); return; }
  if (!window.confirm('恢复会用备份覆盖知识库、会话、笔记、附件、GAL 存档和任务/日程。\n'
    + 'knowledge / notes / uploads / saves 目录会被备份整体替换（备份里没有的文件会删除）。\n'
    + '恢复前会自动生成一份安全备份。继续？')) return;
  try {
    const buf = await file.arrayBuffer();
    const res = await fetch('/api/backup/restore', {
      method: 'POST',
      headers: {
        'X-Auth-Token': TOKEN, 'Content-Type': 'application/zip',
        'X-Backup-Confirm': 'yes',
      },
      body: buf,
    });
    const r = await res.json();
    if (!res.ok || !r.ok) throw new Error(r.error || '恢复失败');
    toast('恢复完成，页面即将刷新', 'success');
    setTimeout(() => location.reload(), 600);
  } catch (e) { toastErr('恢复失败', e); }
}

function workOpenBackupDialog() {
  WORK.backupFile = null;
  workOpenDialog('数据备份', (body) => {
    body.appendChild(workElNew('p', 'work-hint',
      '备份包含：知识库、会话、笔记、记忆索引、附件、GAL 存档、任务与日程。'
      + '不包含 API Key 和浏览器缓存。'));
    const exportBtn = workElNew('button', 'work-btn primary', '导出备份');
    exportBtn.type = 'button';
    exportBtn.onclick = () => workBackupExport(exportBtn);
    body.appendChild(exportBtn);
    body.appendChild(workElNew('div', 'work-hint', '选择备份文件（会先预检，不会立即写入）：'));
    const file = workElNew('input', 'work-input');
    file.type = 'file';
    file.accept = '.zip';
    file.onchange = () => {
      if (file.files && file.files[0]) workBackupPreflight(file.files[0]);
    };
    body.appendChild(file);
    const info = workElNew('div', 'work-hint', '尚未选择备份');
    info.id = 'backup-info';
    body.appendChild(info);
    const restore = workElNew('button', 'work-btn ghost', '确认恢复');
    restore.type = 'button';
    restore.id = 'backup-restore';
    restore.disabled = true;
    restore.onclick = workBackupRestore;
    body.appendChild(restore);
  }, async () => true, { okText: '关闭', hideCancel: true });
}

/* ---------------- 快速添加 / 视图切换 ---------------- */
async function workQuickAdd() {
  const input = workEl('work-quick');
  const text = ((input && input.value) || '').trim();
  if (!text) {
    toast('先写点什么，例如「明天 10 点 开会」', 'error');
    return;
  }
  try {
    const r = await api('/api/work/quick_add', { text });
    if (!r.ok) throw new Error(r.error || '创建失败');
    if (input) input.value = '';
    await workLoad(true);
    toast('已添加：' + ((r.event && r.event.title) || text), 'success');
  } catch (e) {
    toastErr('快速添加失败', e);
  }
}

function workToggleRange() {
  WORK.range = WORK.range === 'week' ? 'today' : 'week';
  const btn = workEl('work-range');
  if (btn) btn.textContent = WORK.range === 'week' ? '7 天' : '今天';
  workRenderToday();
}

function workToggleWorking() {
  WORK.showWorking = !WORK.showWorking;
  const btn = workEl('work-artifact-toggle');
  if (btn) btn.textContent = WORK.showWorking ? '全部' : '交付物';
  workLoad(true);
}

/* ---------------- 绑定 ---------------- */
function workBind() {
  WORK.bound = true;
  const bind = (id, fn) => { const el = workEl(id); if (el) el.onclick = fn; };
  bind('work-refresh', () => workLoad(true));
  bind('work-new-event', () => workOpenEventDialog());
  bind('work-new-image', workOpenImageDialog);
  bind('work-clean-events', workCleanupEvents);
  bind('work-new-task', workOpenTaskDialog);
  bind('work-workflows', workOpenWorkflows);
  bind('work-memory', workOpenMemory);
  bind('work-backup', workOpenBackupDialog);
  bind('work-range', workToggleRange);
  bind('work-quick-add', workQuickAdd);
  bind('work-artifact-toggle', workToggleWorking);
  const quick = workEl('work-quick');
  if (quick) {
    quick.onkeydown = (e) => {
      if (e.key !== 'Enter') return;
      e.preventDefault();
      workQuickAdd();
    };
  }
  const search = workEl('work-task-search');
  if (search) {
    search.oninput = () => {
      WORK.taskQuery = search.value;
      workRenderTasks();
    };
  }
  const filter = workEl('work-task-filter');
  if (filter) {
    filter.onchange = () => {
      WORK.taskFilter = filter.value;
      workRenderTasks();
    };
  }
  const sceneSel = workEl('work-scene');
  if (sceneSel) sceneSel.onchange = () => workSetScene(sceneSel.value);
  bind('work-dialog-close', workCloseDialog);
  bind('work-dialog-cancel', workCloseDialog);
  bind('work-dialog-ok', async () => {
    if (!WORK.onOk) return;
    const ok = await WORK.onOk();
    if (ok) workCloseDialog();
  });
  const modal = workEl('work-modal');
  if (modal) {
    modal.addEventListener('mousedown', (e) => {
      if (e.target === modal) workCloseDialog();
    });
  }
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (state.uiMode !== 'work') return;
    const m = workEl('work-modal');
    if (m && !m.classList.contains('hidden')) {
      e.preventDefault();
      workCloseDialog();
    }
  });
}

/* ============================================================
   场景 / 产物预览 / 工作流 / 记忆
   ============================================================ */

function workSelect(items, cls) {
  const sel = document.createElement('select');
  sel.className = 'work-input' + (cls ? ' ' + cls : '');
  items.forEach(([v, t]) => {
    const o = document.createElement('option');
    o.value = v;
    o.textContent = t;
    sel.appendChild(o);
  });
  return sel;
}

function workFieldWrap(body, label, input) {
  const wrap = workElNew('label', 'work-field');
  wrap.appendChild(workElNew('span', 'work-field-label', label));
  wrap.appendChild(input);
  body.appendChild(wrap);
  return wrap;
}

function workSceneName(id) {
  const hit = (WORK.scenes || []).find((s) => s.id === id);
  return hit ? hit.name : (id || '通用');
}

async function workLoadScene() {
  const sid = (state.sessions && state.sessions.current) || '';
  try {
    const r = await api('/api/scene', { op: 'get', session_id: sid });
    if (!r.ok) return;
    WORK.scene = r.scene || 'general';
    WORK.scenes = r.scenes || [];
    const sel = workEl('work-scene');
    if (!sel) return;
    sel.innerHTML = '';
    WORK.scenes.forEach((s) => {
      const o = document.createElement('option');
      o.value = s.id;
      o.textContent = '场景：' + s.name;
      o.title = s.desc || '';
      sel.appendChild(o);
    });
    sel.value = WORK.scene;
  } catch (e) { /* 场景不是关键路径，失败不影响工作台 */ }
}

async function workSetScene(scene) {
  const sid = (state.sessions && state.sessions.current) || '';
  try {
    const r = await api('/api/scene', { op: 'set', session_id: sid, scene });
    if (!r.ok) throw new Error(r.error || '切换失败');
    WORK.scene = r.scene;
    toast('场景已切换：' + workSceneName(r.scene), 'success');
  } catch (e) {
    toastErr('切换场景失败', e);
  }
}

function workCsvTable(text) {
  const rows = String(text || '').split(/\r?\n/).filter((x) => x.trim() !== '').slice(0, 200);
  const wrap = workElNew('div', 'work-csv-wrap');
  const table = document.createElement('table');
  table.className = 'work-csv';
  rows.forEach((line, ri) => {
    const tr = document.createElement('tr');
    line.split(',').slice(0, 12).forEach((cell) => {
      const td = document.createElement(ri === 0 ? 'th' : 'td');
      td.textContent = cell.replace(/^"|"$/g, '');
      tr.appendChild(td);
    });
    table.appendChild(tr);
  });
  wrap.appendChild(table);
  return wrap;
}

/* ---------- 产物预览 ---------- */
async function workPreviewArtifact(id) {
  const r = await api('/api/work/artifacts', { op: 'preview', id });
  if (!r.ok) throw new Error(r.error || '无法预览');
  const p = r.preview || {};
  const name = ((r.artifact && r.artifact.name) || '').toLowerCase();
  workOpenDialog('预览：' + ((r.artifact && r.artifact.name) || '产物'), (body) => {
    if (p.kind === 'image') {
      const img = document.createElement('img');
      img.className = 'work-preview-img';
      img.src = p.data_url;
      img.alt = (r.artifact && r.artifact.name) || '产物';
      body.appendChild(img);
    } else if (p.kind === 'text') {
      if (/\.(md|markdown)$/.test(name) && typeof safeRenderMarkdown === 'function') {
        const md = workElNew('div', 'work-preview-md');
        md.innerHTML = safeRenderMarkdown(p.text || '');
        body.appendChild(md);
      } else if (/\.(csv|tsv)$/.test(name)) {
        body.appendChild(workCsvTable(p.text || ''));
      } else {
        const pre = document.createElement('pre');
        pre.className = 'work-preview-text';
        pre.textContent = p.text || '';
        body.appendChild(pre);
      }
      if (p.truncated) {
        body.appendChild(workElNew('div', 'work-hint', '文件较大，只显示前 256KB。'));
      }
    } else {
      body.appendChild(workElNew('div', 'work-hint',
        '该类型暂不支持预览（' + Math.round((p.size || 0) / 1024) + ' KB）。'));
    }
    const reveal = workElNew('div', 'work-detail-actions');
    const locate = workElNew('button', 'work-btn small ghost', '打开所在文件夹');
    locate.onclick = () => workRevealArtifact(id);
    reveal.appendChild(locate);
    body.appendChild(reveal);
  }, async () => true, { okText: '关闭', hideCancel: true });
}

/* ---------- 工作流 ---------- */
function workTriggerDesc(wf) {
  if (wf.trigger_type === 'manual') return '手动';
  if (wf.trigger_type === 'event') {
    return wf.trigger_event === 'task.done' ? '任务完成时' : '日程到点时';
  }
  const names = ['一', '二', '三', '四', '五', '六', '日'];
  const days = String(wf.trigger_weekdays || '').split(',')
    .filter((x) => x !== '').map((x) => '周' + (names[Number(x)] || '?'));
  return (days.length ? days.join(' ') : '每天') + ' ' + (wf.trigger_at || '');
}

async function workOpenWorkflows() {
  let data;
  try {
    data = await api('/api/work/workflows', { op: 'list' });
  } catch (e) {
    toastErr('加载工作流失败', e);
    return;
  }
  if (!data.ok) { toast(data.error || '加载失败', 'error'); return; }
  WORK.wfTemplates = data.templates || [];

  workOpenDialog('工作流', (body) => {
    const head = workElNew('div', 'work-detail-actions');
    const add = workElNew('button', 'work-btn primary', '＋ 新建工作流');
    add.onclick = () => workWorkflowForm(WORK.wfTemplates, () => workOpenWorkflows());
    head.appendChild(add);
    head.appendChild(workElNew('span', 'work-hint',
      '工作流是多步动作；单次提醒请用「＋ 日程」。定时/事件触发需保持程序运行'));
    body.appendChild(head);

    if (!(data.workflows || []).length) {
      body.appendChild(workEmpty('还没有工作流', '从模板开始最快'));
    }
    (data.workflows || []).forEach((wf) => {
      body.appendChild(workWorkflowRow(wf, WORK.wfTemplates));
    });

    const runs = data.runs || [];
    if (runs.length) {
      body.appendChild(workElNew('div', 'work-field-label', '最近运行'));
      runs.forEach((r) => {
        const det = document.createElement('details');
        det.className = 'work-wf-run';
        const sum = document.createElement('summary');
        const st = r.status === 'done' ? '成功' : r.status === 'running' ? '运行中' : '失败';
        const used = r.finished_at ? Math.max(0, r.finished_at - r.started_at) : 0;
        sum.textContent = st + ' · ' + workFmt(r.started_at, true)
          + (used ? ' · 用时 ' + Math.round(used) + ' 秒' : '');
        det.appendChild(sum);
        const pre = document.createElement('pre');
        pre.className = 'work-wf-detail';
        pre.textContent = r.detail || '（没有输出）';
        det.appendChild(pre);
        const again = workElNew('button', 'work-btn small ghost', '重跑这条工作流');
        again.onclick = async (ev) => {
          ev.preventDefault();
          try {
            const rr = await api('/api/work/workflows', { op: 'run', id: r.workflow_id });
            toast(rr.ok ? '已重新触发' : (rr.error || '触发失败'), rr.ok ? 'success' : 'error');
            setTimeout(() => workOpenWorkflows(), 500);
          } catch (e) { toastErr('触发失败', e); }
        };
        det.appendChild(again);
        body.appendChild(det);
      });
    }
  }, async () => true, { okText: '关闭', hideCancel: true });
}

function workWorkflowRow(wf, templates) {
  const row = workElNew('div', 'work-wf');
  const main = workElNew('div', 'work-wf-main');
  main.appendChild(workElNew('div', 'work-wf-name', wf.name));
  const acts = (wf.actions || []).map((a) => workActionName(a.type)).join(' → ');
  main.appendChild(workElNew('div', 'work-wf-meta',
    workTriggerDesc(wf) + ' · ' + (wf.actions || []).length + ' 个动作'
    + (acts ? '：' + acts : '')));
  row.appendChild(main);

  const actions = workElNew('div', 'work-wf-actions');
  const run = workElNew('button', 'work-btn small', '运行');
  run.onclick = async () => {
    try {
      const r = await api('/api/work/workflows', { op: 'run', id: wf.id });
      toast(r.ok ? '已触发' : (r.error || '触发失败'), r.ok ? 'success' : 'error');
      setTimeout(() => workOpenWorkflows(), 500);
    } catch (e) { toastErr('触发失败', e); }
  };
  const edit = workElNew('button', 'work-btn small ghost', '编辑');
  edit.onclick = () => workWorkflowForm(templates || [], () => workOpenWorkflows(), wf);
  const toggle = workElNew('button', 'work-btn small ghost', wf.enabled ? '停用' : '启用');
  toggle.onclick = async () => {
    try {
      await api('/api/work/workflows', { op: 'toggle', id: wf.id, enabled: !wf.enabled });
      workOpenWorkflows();
    } catch (e) { toastErr('切换失败', e); }
  };
  const del = workElNew('button', 'work-icon-btn', '✕');
  del.title = '删除工作流';
  del.onclick = async () => {
    if (!window.confirm('删除工作流「' + wf.name + '」？')) return;
    try {
      await api('/api/work/workflows', { op: 'delete', id: wf.id });
      workOpenWorkflows();
    } catch (e) { toastErr('删除失败', e); }
  };
  actions.appendChild(run);
  actions.appendChild(edit);
  actions.appendChild(toggle);
  actions.appendChild(del);
  row.appendChild(actions);
  return row;
}

function workActionName(type) {
  return ({
    'task.create': '创建任务', notify: '发通知', 'calendar.create': '创建日程',
    'session.message': '发消息到会话', 'tts.speak': '本机朗读',
    capability: '调用能力', 'agent.run': 'AI 只读巡检',
  })[type] || type;
}

function workWfBlankAction(type) {
  const a = { type: type || 'notify', args: {} };
  if (a.type === 'task.create') a.args = { title: '', steps: [], auto_start: false };
  else if (a.type === 'notify') a.args = { title: '', text: '' };
  else if (a.type === 'calendar.create') a.args = { title: '', start_at: '', end_at: '' };
  else if (a.type === 'session.message') a.args = { session_id: '', text: '' };
  else if (a.type === 'tts.speak') a.args = { text: '' };
  else if (a.type === 'capability') a.args = { id: '', args: {} };
  else if (a.type === 'agent.run') {
    a.args = { prompt: '', sandbox: '', max_rounds: 6, report: 'findings' };
  }
  return a;
}

function workWfActionArgs(a) {
  const box = workElNew('div', 'work-wf-act-args');
  const args = a.args || (a.args = {});
  const input = (key, ph) => {
    const el = workElNew('input', 'work-input');
    el.value = args[key] == null ? '' : String(args[key]);
    el.placeholder = ph || '';
    el.oninput = () => { args[key] = el.value; };
    box.appendChild(el);
    return el;
  };
  if (a.type === 'task.create') {
    input('title', '任务标题');
    const ta = workElNew('textarea', 'work-input work-textarea');
    ta.rows = 3;
    ta.placeholder = '一行一个步骤';
    ta.value = (args.steps || [])
      .map((s) => (typeof s === 'string' ? s : (s && s.title) || '')).join('\n');
    ta.oninput = () => {
      args.steps = ta.value.split('\n').map((s) => s.trim()).filter(Boolean);
    };
    box.appendChild(ta);
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = !!args.auto_start;
    cb.onchange = () => { args.auto_start = cb.checked; };
    const label = workElNew('label', 'work-check');
    label.appendChild(cb);
    label.appendChild(document.createTextNode(' 创建后立即开始'));
    box.appendChild(label);
  } else if (a.type === 'notify') {
    input('title', '通知标题');
    input('text', '通知内容，可用 {{date}} / {{time}}');
  } else if (a.type === 'calendar.create') {
    input('title', '日程标题');
    ['start_at', 'end_at'].forEach((k, idx) => {
      const el = document.createElement('input');
      el.type = 'datetime-local';
      el.className = 'work-input';
      el.value = String(args[k] || '').replace(' ', 'T').slice(0, 16);
      el.onchange = () => { args[k] = el.value; };
      workFieldWrap(box, idx === 0 ? '开始' : '结束', el);
    });
  } else if (a.type === 'session.message') {
    const sel = workSelect([['', '当前会话（运行时）']].concat(
      ((state.sessions && state.sessions.sessions) || [])
        .map((s) => [s.id, s.title || s.id])));
    sel.value = args.session_id || '';
    sel.onchange = () => { args.session_id = sel.value; };
    box.appendChild(sel);
    input('text', '消息内容，可用 {{date}} / {{time}} / {{weekday}}');
  } else if (a.type === 'tts.speak') {
    input('text', '要朗读的内容，可用 {{date}} / {{time}}');
  } else if (a.type === 'capability') {
    input('id', '能力 id，如 calendar.list');
    const ta = workElNew('textarea', 'work-input work-textarea');
    ta.rows = 3;
    ta.placeholder = '参数 JSON，如 {"limit": 10}';
    ta.value = typeof args.args === 'string' ? args.args
      : (args.args && Object.keys(args.args).length
        ? JSON.stringify(args.args, null, 2) : '');
    ta.oninput = () => { args.args = ta.value; };
    box.appendChild(ta);
  } else if (a.type === 'agent.run') {
    const ta = workElNew('textarea', 'work-input work-textarea');
    ta.rows = 4;
    ta.placeholder = '提示词：让 AI 只读检查什么，如「检查 tests 里有没有明显失效的断言」';
    ta.value = args.prompt || '';
    ta.oninput = () => { args.prompt = ta.value; };
    box.appendChild(ta);
    input('sandbox', '沙箱目录，如 D:\\Tools\\code\\project\\Galgame_monster');
    const rounds = document.createElement('input');
    rounds.type = 'number';
    rounds.min = '1';
    rounds.max = '20';
    rounds.className = 'work-input';
    rounds.value = String(args.max_rounds || 6);
    rounds.oninput = () => { args.max_rounds = Number(rounds.value) || 6; };
    workFieldWrap(box, '最大轮数', rounds);
    const rep = workSelect([['findings', '有发现才汇报（无发现静默）'],
      ['always', '每轮都记录并提示']]);
    rep.value = args.report || 'findings';
    rep.onchange = () => { args.report = rep.value; };
    workFieldWrap(box, '汇报方式', rep);
    box.appendChild(workElNew('div', 'work-hint',
      '只读巡检：可以读文件、搜代码，不会改文件也不会执行命令。'));
  }
  return box;
}

function workWfActionCard(a, i, render, body) {
  const card = workElNew('div', 'work-wf-act');
  const head = workElNew('div', 'work-wf-act-head');
  head.appendChild(workElNew('span', 'work-wf-act-idx', '动作 ' + (i + 1)));
  const type = workSelect([
    ['task.create', '创建任务'], ['notify', '发通知'], ['calendar.create', '创建日程'],
    ['session.message', '发消息到会话'], ['tts.speak', '本机朗读'],
    ['capability', '调用能力'], ['agent.run', 'AI 只读巡检'],
  ]);
  type.value = a.type;
  type.onchange = () => {
    const blank = workWfBlankAction(type.value);
    a.type = blank.type;
    a.args = blank.args;
    render(body);
  };
  head.appendChild(type);
  const tools = workElNew('span', 'work-wf-act-tools');
  const up = workElNew('button', 'work-icon-btn', '↑');
  up.title = '上移';
  up.onclick = () => {
    if (i <= 0) return;
    const list = WORK.wfDraft.actions;
    [list[i - 1], list[i]] = [list[i], list[i - 1]];
    render(body);
  };
  const down = workElNew('button', 'work-icon-btn', '↓');
  down.title = '下移';
  down.onclick = () => {
    const list = WORK.wfDraft.actions;
    if (i >= list.length - 1) return;
    [list[i + 1], list[i]] = [list[i], list[i + 1]];
    render(body);
  };
  const del = workElNew('button', 'work-icon-btn', '✕');
  del.title = '删除动作';
  del.onclick = () => {
    WORK.wfDraft.actions.splice(i, 1);
    render(body);
  };
  tools.appendChild(up);
  tools.appendChild(down);
  tools.appendChild(del);
  head.appendChild(tools);
  card.appendChild(head);
  card.appendChild(workWfActionArgs(a));
  return card;
}

function workWorkflowForm(templates, done, existing) {
  const draft = existing ? {
    id: existing.id,
    name: existing.name || '',
    trigger_type: existing.trigger_type || 'manual',
    trigger_at: existing.trigger_at || '',
    trigger_weekdays: existing.trigger_weekdays || '',
    trigger_event: existing.trigger_event || 'task.done',
    actions: JSON.parse(JSON.stringify(existing.actions || [])),
  } : {
    id: '', name: '', trigger_type: 'manual', trigger_at: '',
    trigger_weekdays: '', trigger_event: 'task.done', actions: [],
  };
  WORK.wfDraft = draft;

  const render = (body) => {
    body.innerHTML = '';
    if (!existing) {
      const tpls = (templates || []).slice().sort(
        (a, b) => (b.scene === WORK.scene ? 1 : 0) - (a.scene === WORK.scene ? 1 : 0));
      const tpl = workSelect([['', '从模板开始…']].concat(
        tpls.map((t) => [t.id, t.name + (t.scene === WORK.scene ? ' · 推荐' : '')
          + ' · ' + t.desc])), 'work-tpl');
      tpl.onchange = () => {
        const hit = (templates || []).find((x) => x.id === tpl.value);
        if (!hit) return;
        draft.name = hit.name || '';
        draft.trigger_type = hit.trigger_type || 'manual';
        draft.trigger_at = hit.trigger_at || '';
        draft.trigger_weekdays = hit.trigger_weekdays || '';
        draft.trigger_event = hit.trigger_event || 'task.done';
        draft.actions = JSON.parse(JSON.stringify(hit.actions || []));
        render(body);
      };
      workFieldWrap(body, '模板', tpl);
    }

    const name = workElNew('input', 'work-input');
    name.value = draft.name;
    name.placeholder = '例如：每日站会提醒';
    name.oninput = () => { draft.name = name.value; };
    workFieldWrap(body, '名称', name);

    const trigger = workSelect([['manual', '手动'], ['time', '定时'], ['event', '事件']]);
    trigger.value = draft.trigger_type;
    trigger.onchange = () => { draft.trigger_type = trigger.value; render(body); };
    workFieldWrap(body, '触发方式', trigger);

    if (draft.trigger_type === 'time') {
      const time = document.createElement('input');
      time.type = 'time';
      time.className = 'work-input';
      time.value = draft.trigger_at || '';
      time.onchange = () => { draft.trigger_at = time.value; };
      workFieldWrap(body, '时间', time);
      const days = workElNew('input', 'work-input');
      days.value = draft.trigger_weekdays || '';
      days.placeholder = '0=周一 … 6=周日，逗号分隔；留空=每天';
      days.oninput = () => { draft.trigger_weekdays = days.value; };
      workFieldWrap(body, '星期', days);
    }
    if (draft.trigger_type === 'event') {
      const ev = workSelect([['task.done', '任务完成'], ['calendar.due', '日程到点']]);
      ev.value = draft.trigger_event || 'task.done';
      ev.onchange = () => { draft.trigger_event = ev.value; };
      workFieldWrap(body, '事件', ev);
    }

    body.appendChild(workElNew('div', 'work-field-label', '动作（按顺序执行）'));
    draft.actions.forEach((a, i) => {
      body.appendChild(workWfActionCard(a, i, render, body));
    });
    const add = workElNew('button', 'work-btn small', '＋ 添加动作');
    add.onclick = () => {
      draft.actions.push(workWfBlankAction('task.create'));
      render(body);
    };
    body.appendChild(add);
    body.appendChild(workElNew('div', 'work-hint',
      '可用占位符：{{date}} / {{time}} / {{weekday}}；事件触发还可用 {{event.title}}。'));
  };

  workOpenDialog(existing ? '编辑工作流' : '新建工作流', render, async () => {
    const d = WORK.wfDraft;
    if (!d.name.trim()) { toast('请填写工作流名称', 'error'); return false; }
    if (d.trigger_type === 'time' && !d.trigger_at) {
      toast('定时触发需要填写时间', 'error');
      return false;
    }
    if (!d.actions.length) { toast('至少添加一个动作', 'error'); return false; }
    let actions;
    try {
      actions = d.actions.map((a) => {
        const args = Object.assign({}, a.args || {});
        if (a.type === 'task.create') {
          if (!String(args.title || '').trim()) throw new Error('「创建任务」缺少标题');
          if (!Array.isArray(args.steps)) args.steps = [];
        } else if (a.type === 'calendar.create') {
          if (!String(args.title || '').trim()) throw new Error('「创建日程」缺少标题');
          args.start_at = String(args.start_at || '').replace('T', ' ');
          args.end_at = String(args.end_at || '').replace('T', ' ');
        } else if (a.type === 'session.message') {
          if (!String(args.text || '').trim()) throw new Error('「发消息到会话」缺少内容');
        } else if (a.type === 'tts.speak') {
          if (!String(args.text || '').trim()) throw new Error('「本机朗读」缺少内容');
        } else if (a.type === 'capability') {
          if (!String(args.id || '').trim()) throw new Error('「调用能力」缺少能力 id');
          let inner = args.args;
          if (typeof inner === 'string') {
            try {
              inner = inner.trim() ? JSON.parse(inner) : {};
            } catch (e) {
              throw new Error('能力参数不是合法 JSON');
            }
          }
          args.args = inner || {};
        } else if (a.type === 'agent.run') {
          if (!String(args.prompt || '').trim()) throw new Error('「AI 只读巡检」缺少提示词');
          if (!String(args.sandbox || '').trim()) throw new Error('「AI 只读巡检」缺少沙箱目录');
          args.max_rounds = Math.max(1, Number(args.max_rounds) || 6);
        }
        return { type: a.type, args };
      });
    } catch (e) {
      toast(e.message, 'error');
      return false;
    }
    try {
      const payload = {
        name: d.name.trim(), trigger_type: d.trigger_type,
        trigger_at: d.trigger_at || '', trigger_weekdays: d.trigger_weekdays || '',
        trigger_event: d.trigger_event || '', actions,
      };
      const r = existing
        ? await api('/api/work/workflows',
          Object.assign({ op: 'update', id: existing.id }, payload))
        : await api('/api/work/workflows', Object.assign({ op: 'create' }, payload));
      if (!r.ok) throw new Error(r.error || '保存失败');
      toast(existing ? '工作流已更新' : '工作流已创建', 'success');
      done();
      return true;
    } catch (e) {
      toastErr('保存工作流失败', e);
      return false;
    }
  }, { okText: '保存' });
}

/* ---------- 记忆 ---------- */
async function workOpenMemory(query, includeMirror) {
  const q = typeof query === 'string' ? query : '';
  const withMirror = !!includeMirror;
  let r;
  try {
    const payload = { op: 'search', query: q, limit: 30 };
    if (!withMirror) payload.sources = ['manual', 'task'];
    r = await api('/api/memory', payload);
  } catch (e) {
    toastErr('加载记忆失败', e);
    return;
  }
  if (!r.ok) { toast(r.error || '加载失败', 'error'); return; }
  const stats = r.stats || {};

  workOpenDialog('记忆', (body) => {
    const by = Object.entries(stats.by_source || {})
      .map(([k, v]) => k + ' ' + v).join(' / ');
    body.appendChild(workElNew('div', 'work-hint',
      '共 ' + (stats.total || 0) + ' 条' + (by ? ' · ' + by : '')));
    body.appendChild(workElNew('div', 'work-hint',
      '这里只做全局搜索；编辑画像 / 知识库 / 笔记请走侧栏对应入口。'));

    const mirror = document.createElement('input');
    mirror.type = 'checkbox';
    mirror.checked = withMirror;
    const mLabel = workElNew('label', 'work-check');
    mLabel.appendChild(mirror);
    mLabel.appendChild(document.createTextNode(' 包含知识库 / 画像 / 笔记（只读）'));
    mirror.onchange = () => workOpenMemory(q, mirror.checked);
    body.appendChild(mLabel);

    const search = workElNew('div', 'work-memory-search');
    const input = workElNew('input', 'work-input');
    input.placeholder = '搜索记忆…';
    input.value = q;
    const btn = workElNew('button', 'work-btn', '搜索');
    btn.onclick = () => workOpenMemory(input.value, withMirror);
    search.appendChild(input);
    search.appendChild(btn);
    body.appendChild(search);

    if (!(r.items || []).length) {
      body.appendChild(workEmpty('没有匹配的记忆',
        withMirror ? '换个关键词，或点「重建索引」' : '在下面添加一条，或勾选上方查看知识库 / 笔记'));
    }
    (r.items || []).forEach((it) => {
      const isMirror = it.source === 'kb' || it.source === 'session'
        || it.source === 'note';
      const row = workElNew('div', 'work-memory-item');
      const main = workElNew('div', 'work-memory-main');
      const title = it.title || '';
      let bodyText = it.text || '';
      if (title && bodyText.startsWith(title)) {
        bodyText = bodyText.slice(title.length).trim();
      }
      if (title) main.appendChild(workElNew('div', 'work-memory-title', title));
      if (bodyText) main.appendChild(workElNew('div', 'work-memory-text', bodyText));
      main.appendChild(workElNew('div', 'work-memory-meta',
        (it.type || 'fact') + ' · '
        + (it.source_name || (isMirror ? '知识库镜像 · 只读' : (it.source || 'manual')))
        + ' · 重要度 ' + Number(it.importance || 0).toFixed(1)));
      row.appendChild(main);
      if (isMirror) {
        const open = workElNew('button', 'work-btn small', '打开来源');
        open.title = it.source === 'note' ? '在笔记库中打开' : '在知识库中定位这条记忆';
        open.onclick = () => {
          workCloseDialog();
          if (it.source === 'note') {
            if (typeof notesOpen === 'function') notesOpen(it.note_path || it.source_id);
            return;
          }
          if (typeof kbOpenNode === 'function') {
            kbOpenNode(it.source === 'session' ? 'session' : it.kb_id,
                       it.source_id || '');
          }
        };
        row.appendChild(open);
      } else {
        const del = workElNew('button', 'work-icon-btn', '✕');
        del.title = '删除这条记忆';
        del.onclick = async () => {
          try {
            await api('/api/memory', { op: 'delete', id: it.id });
            workOpenMemory(input.value, withMirror);
          } catch (e) { toastErr('删除失败', e); }
        };
        row.appendChild(del);
      }
      body.appendChild(row);
    });

    body.appendChild(workElNew('div', 'work-field-label', '添加记忆'));
    const ta = workElNew('textarea', 'work-input work-textarea');
    ta.rows = 3;
    ta.placeholder = '例如：我对花生过敏 / 我习惯用简洁的回答';
    body.appendChild(ta);
    const typeSel = workSelect([['fact', '事实'], ['preference', '偏好'],
      ['event', '事件'], ['note', '笔记']]);
    body.appendChild(typeSel);
    const kbs = (r.kbs || []).filter((k) => k.name !== '用户画像');
    const targetSel = workSelect([
      ['auto', '自动（按类型）'],
      ['session', '本会话记忆'],
      ['profile', '用户画像'],
    ].concat(kbs.map((k) => ['kb:' + k.id, '知识库：' + k.name]))
      .concat([['global', '仅全局快速记忆']]));
    body.appendChild(targetSel);
    body.appendChild(workElNew('div', 'work-hint',
      '自动：偏好 → 用户画像；事实/事件 → 本会话；笔记 → 当前知识库（未选则本会话）。'));
    const add = workElNew('button', 'work-btn primary', '记住');
    add.onclick = async () => {
      if (!ta.value.trim()) { toast('写点什么再记住', 'error'); return; }
      const target = targetSel.value;
      const kbId = target.startsWith('kb:') ? target.slice(3) : '';
      const payload = {
        text: ta.value, type: typeSel.value, target, kb_id: kbId,
        session_id: (state.sessions && state.sessions.current) || '',
        importance: 0.7,
      };
      try {
        const sim = await api('/api/memory', Object.assign({ op: 'similar' }, payload));
        const hits = (sim && sim.items) || [];
        if (hits.length) {
          const names = hits.map((h) => '「' + (h.title || '') + '」('
            + (h.source_name || '') + ')').join('、');
          if (!window.confirm('已有相似条目：' + names + '\n仍然添加？')) return;
        }
        const rr = await api('/api/memory', Object.assign({ op: 'add' }, payload));
        if (!rr.ok) throw new Error(rr.error || '添加失败');
        toast('已写入：' + (rr.source_name || '记忆'), 'success');
        const graph = rr.target && rr.target !== 'global';
        workOpenMemory('', graph ? true : withMirror);
      } catch (e) { toastErr('添加失败', e); }
    };
    body.appendChild(add);

    const re = workElNew('button', 'work-btn ghost', '重建索引');
    re.title = '把知识库与会话记忆重新镜像进统一索引';
    re.onclick = async () => {
      try {
        const rr = await api('/api/memory', { op: 'reindex' });
        const n = rr.result ? rr.result.mirrored : 0;
        toast('已重建：' + n + ' 条', 'success');
        workOpenMemory('', withMirror);
      } catch (e) { toastErr('重建失败', e); }
    };
    body.appendChild(re);
  }, async () => true, { okText: '关闭', hideCancel: true });
}
