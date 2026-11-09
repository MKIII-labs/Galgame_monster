/* 工作台 · core
   由 work.js 按功能拆分；普通脚本共享全局作用域，加载顺序见 index.html。 */
'use strict';

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
  range: 'today',          // today | week（「今天」三栏里的日程范围）
  taskFilter: '',
  taskQuery: '',
  showWorking: false,      // 产物栏是否显示工作区临时文件
  caps: null,              // 能力清单（步骤编辑用）
  wfDraft: null,           // 工作流表单草稿
  wfTemplates: [],
  // ---- 视图 ----
  view: 'today',           // today | week | month | board | stats
  monthOffset: 0,          // 月视图：相对当前月的偏移
  monthEvents: [],         // 月视图单独拉取的日程
  selectedDay: '',         // 月视图选中的日期（YYYY-MM-DD）
  stats: null,             // /api/work/stats 缓存
  statsDays: 7,
  monthLoadedFor: '',      // 月视图已加载的月份 key（空 = 还没加载）
  batch: new Set(),        // 看板批量选择的 task id
  // ---- 专注计时 ----
  focus: { running: false, remain: 25 * 60, total: 25 * 60,
           taskId: '', title: '', startedAt: 0, timer: null },
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
  workApplyView();
  workRenderFocusBtn();
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
        op: 'range', start_at: workDayStart(now) - 31 * 86400,
        end_at: workDayStart(now) + 90 * 86400,
      }),
      api('/api/work/tasks', { op: 'list', limit: 200 }),
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
  workApplyView();
  workRenderFocusBtn();
  const sub = workEl('work-sub');
  if (sub) {
    const d = new Date();
    sub.textContent = d.getFullYear() + ' 年 ' + (d.getMonth() + 1) + ' 月 '
      + d.getDate() + ' 日 · ' + WORK.tasks.filter((t) => t.status === 'running').length
      + ' 个任务进行中';
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
  // 从设置里打开「备份与恢复」时可能还没进过工作台：先把弹窗的关闭/确认绑定补上，
  // 否则弹窗能弹出但右上角 ✕ 和底部按钮都没反应。
  if (!WORK.bound) workBind();
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

function workBind() {
  WORK.bound = true;
  const bind = (id, fn) => { const el = workEl(id); if (el) el.onclick = fn; };
  bind('work-refresh', () => workLoad(true));
  bind('work-new-event', () => workOpenEventDialog());
  bind('work-new-image', workOpenImageDialog);
  bind('work-clean-events', workCleanupEvents);
  bind('work-new-task', workOpenTaskDialog);
  bind('work-templates', workOpenTemplatesDialog);
  bind('work-focus', workOpenFocusDialog);
  document.querySelectorAll('#work-views-tabs button').forEach((b) => {
    b.onclick = () => workSetView(b.dataset.wview);
  });
  try {
    const saved = localStorage.getItem('gm.work.view');
    if (saved) WORK.view = saved;
  } catch (e) { /* 忽略 */ }
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

