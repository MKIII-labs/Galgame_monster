/* 工作台 · focus
   由 work.js 按功能拆分；普通脚本共享全局作用域，加载顺序见 index.html。 */
'use strict';

function workClock(sec) {
  const s = Math.max(0, Math.round(sec));
  return String(Math.floor(s / 60)).padStart(2, '0') + ':'
    + String(s % 60).padStart(2, '0');
}

function workRenderFocusBtn() {
  const btn = workEl('work-focus');
  if (!btn) return;
  const f = WORK.focus;
  const active = f.running || f.remain < f.total;
  btn.textContent = active ? '专注 ' + workClock(f.remain) : '专注';
  btn.classList.toggle('running', !!f.running);
  btn.title = active ? '专注进行中，点开可暂停 / 结束' : '专注计时：开启一段专注';
}

function workFocusTick() {
  const f = WORK.focus;
  if (!f.running) return;
  f.remain -= 1;
  if (f.remain <= 0) {
    f.remain = 0;
    workFocusFinish(true);
    return;
  }
  workRenderFocusBtn();
}

function workFocusStart(minutes, taskId, title) {
  const f = WORK.focus;
  clearInterval(f.timer);
  f.total = Math.max(1, Number(minutes) || 25) * 60;
  f.remain = f.total;
  f.taskId = taskId || '';
  f.title = title || '';
  f.startedAt = Date.now() / 1000;
  f.running = true;
  f.timer = setInterval(workFocusTick, 1000);
  workRenderFocusBtn();
}

function workFocusPause() {
  const f = WORK.focus;
  if (!f.running) return;
  clearInterval(f.timer);
  f.timer = null;
  f.running = false;
  workRenderFocusBtn();
}

function workFocusFinish(auto) {
  const f = WORK.focus;
  clearInterval(f.timer);
  f.timer = null;
  const used = Math.max(1, Math.round((f.total - f.remain) / 60));
  const startAt = f.startedAt || (Date.now() / 1000 - used * 60);
  const payload = {
    op: 'add', task_id: f.taskId, title: f.title,
    start_at: startAt, end_at: Date.now() / 1000, minutes: used,
  };
  const wasActive = f.startedAt > 0;
  f.running = false;
  f.startedAt = 0;
  f.remain = f.total;
  f.taskId = '';
  f.title = '';
  workRenderFocusBtn();
  if (!wasActive) return;
  api('/api/work/focus', payload).then(() => {
    WORK.stats = null;
    toast((auto ? '专注完成，' : '已记录专注 ') + used + ' 分钟', 'success');
    if (WORK.view === 'stats') workLoadStats(true);
  }).catch((e) => toastErr('记录专注失败', e));
}

function workOpenFocusDialog() {
  const f = WORK.focus;
  const running = f.running;
  const hasPending = f.remain < f.total;
  let minuteSel = null;
  let taskSel = null;
  let titleInp = null;
  workOpenDialog('专注计时', (body) => {
    const now = workElNew('div', 'work-hint', hasPending
      ? '剩余 ' + workClock(f.remain) + ' · ' + (f.title || '未关联任务')
      : '选一个时长，专注结束后会自动记入统计。');
    body.appendChild(now);
    if (running || hasPending) {
      const row = workElNew('div', 'work-detail-actions');
      const pause = workElNew('button', 'work-btn small', running ? '暂停' : '继续');
      pause.onclick = () => {
        if (WORK.focus.running) workFocusPause();
        else {
          WORK.focus.running = true;
          WORK.focus.startedAt = WORK.focus.startedAt || Date.now() / 1000;
          WORK.focus.timer = setInterval(workFocusTick, 1000);
          workRenderFocusBtn();
        }
        workOpenFocusDialog();
      };
      row.appendChild(pause);
      const stop = workElNew('button', 'work-btn small ghost', '结束并记录');
      stop.onclick = () => { workFocusFinish(false); workCloseDialog(); };
      row.appendChild(stop);
      body.appendChild(row);
      return;
    }
    minuteSel = workSelect([['15', '15 分钟'], ['25', '25 分钟'],
      ['45', '45 分钟'], ['60', '60 分钟']]);
    minuteSel.value = '25';
    workField(body, '时长', minuteSel);
    const choices = [['', '不关联任务']].concat(
      WORK.tasks.filter((t) => !['done', 'canceled'].includes(t.status))
        .map((t) => [t.id, t.title]));
    taskSel = workSelect(choices);
    workField(body, '关联任务（可选）', taskSel);
    titleInp = workField(body, '备注（可选）', workElNew('input', 'work-input'));
    titleInp.placeholder = '例如：写周报';
  }, async () => {
    if (running || hasPending) return true;      // 运行中的弹窗：确定 = 关闭，不结束计时
    const taskId = taskSel ? taskSel.value : '';
    const task = WORK.tasks.find((t) => t.id === taskId);
    workFocusStart(Number(minuteSel.value || 25), taskId,
      (titleInp && titleInp.value.trim()) || (task && task.title) || '');
    toast('已开始专注', 'success');
    return true;
  }, { okText: running || hasPending ? '关闭' : '开始' });
}

/* ---------------- 任务模板 ---------------- */

