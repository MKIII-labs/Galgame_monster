/* 工作台 · events
   由 work.js 按功能拆分；普通脚本共享全局作用域，加载顺序见 index.html。 */
'use strict';

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

