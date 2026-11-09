/* 工作台 · workflows
   由 work.js 按功能拆分；普通脚本共享全局作用域，加载顺序见 index.html。 */
'use strict';

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

