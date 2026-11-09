/* 工作台 · tasks
   由 work.js 按功能拆分；普通脚本共享全局作用域，加载顺序见 index.html。 */
'use strict';

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
  const dueTs = Number(t.due_at || (t.meta && t.meta.due_at) || 0);
  const overdue = dueTs > 0 && dueTs < Date.now() / 1000
    && !['done', 'canceled'].includes(t.status);
  const card = workElNew('div', 'work-task' + (t.status === 'done' ? ' done' : '')
    + (overdue ? ' overdue' : ''));
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
  const due = Number(t.due_at || (t.meta && t.meta.due_at) || 0);
  const bits = [done + '/' + total + ' 步'];
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

function workOpenTaskDialog(tpl) {
  let duePick = null;
  const preset = tpl || {};
  workOpenDialog(preset.name ? ('从模板新建：' + preset.name) : '新建任务', (body) => {
    const title = workField(body, '任务标题', workElNew('input', 'work-input'));
    title.id = 'work-task-title';
    title.placeholder = '例如：整理本周会议纪要';
    title.value = preset.title || '';
    const ta = workField(body, '步骤（一行一个）', workElNew('textarea', 'work-input work-textarea'));
    ta.id = 'work-task-steps';
    ta.rows = 6;
    ta.placeholder = '收集材料\n写初稿\n校对并发出';
    if (preset.steps && preset.steps.length) {
      ta.value = preset.steps.map((s) => (typeof s === 'string' ? s : s.title || '')).join('\n');
    }
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
    pri.value = String(Number(preset.priority) || 0);
    const tags = workField(body, '标签（可选）', workElNew('input', 'work-input'));
    tags.id = 'work-task-tags';
    tags.placeholder = '例如：工作, 周报';
    tags.value = preset.tags || '';
    const repeat = workField(body, '重复', workSelect(
      [['', '不重复'], ['daily', '每天'], ['weekly', '每周'], ['monthly', '每月']]));
    repeat.id = 'work-task-repeat';
    const estimate = workField(body, '预计分钟（可选）', workElNew('input', 'work-input'));
    estimate.id = 'work-task-estimate';
    estimate.placeholder = '例如：45';
    estimate.value = preset.estimate_min ? String(preset.estimate_min) : '';
    const parent = workField(body, '父任务（可选，做成子任务）', workSelect(
      [['', '无']].concat(WORK.tasks
        .filter((t) => !['done', 'canceled'].includes(t.status))
        .slice(0, 40).map((t) => [t.id, t.title]))));
    parent.id = 'work-task-parent';
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
        repeat_rule: workEl('work-task-repeat').value,
        estimate_min: Number(workEl('work-task-estimate').value || 0),
        parent_id: workEl('work-task-parent').value,
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
    const due = Number(t.due_at || (t.meta && t.meta.due_at) || 0);
    if (t.tags) bits.push(t.tags);
    if (due) {
      bits.push('截止 ' + (typeof fmtTaskTime === 'function'
        ? fmtTaskTime(due) : new Date(due * 1000).toLocaleString()));
    }
    if (t.repeat_rule) bits.push('重复：' + ({ daily: '每天', weekly: '每周', monthly: '每月' }[t.repeat_rule] || t.repeat_rule));
    if (t.estimate_min) bits.push('预计 ' + t.estimate_min + ' 分钟');
    if (t.parent_id) {
      const p = WORK.tasks.find((x) => x.id === t.parent_id);
      if (p) bits.push('父任务：' + p.title);
    }
    if (t.depends_on) {
      const wait = t.depends_on.split(',').map((id) => WORK.tasks.find((x) => x.id === id))
        .filter((x) => x && x.status !== 'done');
      if (wait.length) bits.push('等待：' + wait.map((x) => x.title).join('、'));
    }
    const kids = WORK.tasks.filter((x) => x.parent_id === t.id);
    if (kids.length) bits.push('子任务 ' + kids.length + ' 个');
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
      const focus = workElNew('button', 'work-btn ghost', '专注 25 分钟');
      focus.onclick = () => {
        workCloseDialog();
        workFocusStart(25, t.id, t.title);
        toast('已开始专注 25 分钟', 'success');
      };
      actions.appendChild(focus);
      const doneBtn = workElNew('button', 'work-btn primary', '标记完成');
      doneBtn.onclick = () => workTaskAct(t.id, 'complete');
      actions.appendChild(doneBtn);
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
    WORK.stats = null;
    await workLoad(true);
    if (WORK.currentTask) workRenderTaskDialog();
    toast('已' + ({ start: '开始', pause: '暂停', resume: '继续', cancel: '取消',
      retry: '重试', confirm_step: '确认', complete_step: '完成',
      complete: '完成', move_step: '移动', delete_step: '删除步骤' }[op] || '操作'), 'success');
  } catch (e) {
    toastErr('任务操作失败', e);
  }
}

function workOpenTaskInfoDialog(t) {
  let duePick = null;
  workOpenDialog('编辑任务', (body) => {
    const title = workField(body, '标题', workElNew('input', 'work-input'));
    title.id = 'work-info-title';
    title.value = t.title || '';
    const pri = workField(body, '优先级', workSelect(
      [['0', '普通'], ['1', '低'], ['2', '中'], ['3', '高']]));
    pri.id = 'work-info-pri';
    pri.value = String(Math.min(3, Number(t.priority) || 0));
    const tags = workField(body, '标签', workElNew('input', 'work-input'));
    tags.id = 'work-info-tags';
    tags.placeholder = '例如：工作, 周报';
    tags.value = t.tags || '';

    // 截止时间：可清除
    const dueOn = workElNew('label', 'work-check');
    const dueCb = document.createElement('input');
    dueCb.type = 'checkbox';
    dueCb.id = 'work-info-due-on';
    dueCb.checked = Number(t.due_at || 0) > 0;
    dueOn.appendChild(dueCb);
    dueOn.appendChild(document.createTextNode(' 设置截止时间（会同步到日程并提醒）'));
    body.appendChild(dueOn);
    duePick = workDateTimePicker(Number(t.due_at || 0) > 0
      ? Number(t.due_at) : Math.ceil(Date.now() / 3600000) * 3600 + 3600,
      { simple: true });
    const dueField = workFieldBox(body, '截止时间', duePick.el);
    dueField.hidden = !dueCb.checked;
    dueCb.onchange = () => { dueField.hidden = !dueCb.checked; };

    const repeat = workField(body, '重复', workSelect(
      [['', '不重复'], ['daily', '每天'], ['weekly', '每周'], ['monthly', '每月']]));
    repeat.id = 'work-info-repeat';
    repeat.value = t.repeat_rule || '';
    const estimate = workField(body, '预计分钟', workElNew('input', 'work-input'));
    estimate.id = 'work-info-est';
    estimate.placeholder = '例如：45';
    estimate.value = t.estimate_min ? String(t.estimate_min) : '';
    const parent = workField(body, '父任务', workSelect(
      [['', '无']].concat(WORK.tasks
        .filter((x) => x.id !== t.id && !['done', 'canceled'].includes(x.status))
        .slice(0, 40).map((x) => [x.id, x.title]))));
    parent.id = 'work-info-parent';
    parent.value = t.parent_id || '';

    const depsBox = workElNew('div', 'work-check-list');
    const deps = String(t.depends_on || '').split(',').map((x) => x.trim()).filter(Boolean);
    WORK.tasks.filter((x) => x.id !== t.id && !['done', 'canceled'].includes(x.status))
      .slice(0, 30).forEach((x) => {
        const lab = workElNew('label', 'work-check');
        const cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.value = x.id;
        cb.checked = deps.includes(x.id);
        cb.dataset.dep = '1';
        lab.appendChild(cb);
        lab.appendChild(document.createTextNode(' ' + x.title));
        depsBox.appendChild(lab);
      });
    workFieldBox(body, '前置任务（完成前不能开始）', depsBox);

    const tplBtn = workElNew('button', 'work-btn small ghost', '存为模板');
    tplBtn.onclick = async () => {
      const name = await appPrompt('模板名称', t.title);
      if (name === null || !name.trim()) return;
      try {
        const r = await api('/api/work/templates', {
          op: 'save', name: name.trim(), title: t.title,
          steps: (t.steps || []).map((s) => ({ title: s.title, tool: s.tool, args: s.args })),
          tags: t.tags || '', priority: t.priority || 0,
          estimate_min: t.estimate_min || 0,
        });
        if (!r.ok) throw new Error(r.error || '保存失败');
        toast('已存为模板：' + name.trim(), 'success');
      } catch (e) { toastErr('保存模板失败', e); }
    };
    body.appendChild(tplBtn);
  }, async () => {
    const body = workEl('work-dialog-body');
    const title = workEl('work-info-title').value.trim();
    if (!title) { toast('标题不能为空', 'error'); return false; }
    const deps = Array.from(body.querySelectorAll('input[data-dep]:checked'))
      .map((x) => x.value);
    try {
      const payload = {
        op: 'update', id: t.id, title,
        priority: Number(workEl('work-info-pri').value || 0),
        tags: workEl('work-info-tags').value.trim(),
        repeat_rule: workEl('work-info-repeat').value,
        estimate_min: Number(workEl('work-info-est').value || 0),
        parent_id: workEl('work-info-parent').value,
        depends_on: deps.join(','),
      };
      if (workEl('work-info-due-on').checked) {
        payload.due_at = workLocalInput(duePick.get()).replace('T', ' ');
        payload.remind_before = 600;
      } else {
        payload.due_at = '';
      }
      const r = await api('/api/work/tasks', payload);
      if (!r.ok) throw new Error(r.error || '保存失败');
      WORK.currentTask = r.task;
      WORK.stats = null;
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

