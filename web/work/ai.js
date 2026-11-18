/* 工作台 · AI 助手
   ------------------------------------------------------------
   建议模式：AI 只生成计划 / 步骤 / 复盘，写入动作全部由用户点击确认。
   数据接口：POST /api/work/assist {op: plan_today | breakdown | review}
   依赖：core.js（WORK / workEl / workLoad）、tasks.js（workOpenTaskDialog）、
         markdown.js（safeRenderMarkdown）、全局 api / toast。
   ============================================================ */
'use strict';

const WORK_AI = {
  tab: 'plan',
  bound: false,
  plan: null,            // { ok, ai, plan:{focus,blocks,note}, message }
  planBusy: false,
  planApplied: new Set(),
  breakdown: null,       // { taskId, title, ai, steps, note, message }
  breakdownBusy: false,
  review: null,          // { ai, markdown, message }
  reviewBusy: false,
};

function workAiEl(id) { return document.getElementById(id); }

function workAiEsc(s) {
  if (typeof escapeHtml === 'function') return escapeHtml(String(s == null ? '' : s));
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function workAiToday() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, '0');
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate());
}

function workAiSetStatus(text, tone) {
  const el = workAiEl('work-ai-status');
  if (!el) return;
  el.textContent = text;
  el.className = tone || '';
}

function workAiBtn(text, cls, fn) {
  const b = document.createElement('button');
  b.type = 'button';
  b.className = 'work-btn ' + (cls || '');
  b.textContent = text;
  b.onclick = fn;
  return b;
}

function workAiSkeleton(text) {
  const d = document.createElement('div');
  d.className = 'work-ai-loading';
  d.innerHTML = '<span class="work-ai-spin"></span><span>' + workAiEsc(text) + '</span>';
  return d;
}

function workAiSection(title, extra) {
  const box = document.createElement('div');
  box.className = 'work-ai-sect';
  if (title) {
    const head = document.createElement('div');
    head.className = 'work-ai-sect-head';
    head.innerHTML = '<b>' + workAiEsc(title) + '</b>' +
      (extra ? '<span>' + workAiEsc(extra) + '</span>' : '');
    box.appendChild(head);
  }
  return box;
}

/* ---------------- 概览 / 下一步 ---------------- */

function workAiRenderOverview() {
  const set = (id, v) => { const el = workAiEl(id); if (el) el.textContent = String(v); };
  const now = Date.now() / 1000;
  const start = workDayStart(now);
  const todayEvents = (WORK.events || []).filter((e) => {
    const t = Number(e.occurrence_at || 0);
    return t >= start && t < start + 86400;
  });
  const open = (WORK.tasks || []).filter((t) =>
    ['pending', 'running', 'paused', 'failed'].includes(t.status));
  const running = open.filter((t) => t.status === 'running');
  const overdue = open.filter((t) => Number(t.due_at || 0) > 0 && Number(t.due_at || 0) < now);
  set('work-m-today', todayEvents.length);
  set('work-m-open', open.length);
  set('work-m-running', running.length);
  set('work-m-overdue', overdue.length);
  const note = workAiEl('work-overview-note');
  if (!note) return;
  const plan = WORK_AI.plan && WORK_AI.plan.plan;
  if (plan && plan.note) note.textContent = plan.note;
  else if (overdue.length) note.textContent = '有 ' + overdue.length + ' 个任务已逾期，先处理它们。';
  else if (running.length) note.textContent = '正在推进 ' + running.length + ' 个任务。';
  else note.textContent = '让 AI 帮你安排今天';
}

function workAiRenderFocus() {
  const box = workAiEl('work-focus-list');
  const sub = workAiEl('work-focus-sub');
  if (!box) return;
  const now = Date.now() / 1000;
  const open = (WORK.tasks || []).filter((t) =>
    ['pending', 'running', 'paused', 'failed'].includes(t.status));
  let items = [];
  const plan = WORK_AI.plan && WORK_AI.plan.plan;
  let label = '';
  if (plan && Array.isArray(plan.focus) && plan.focus.length) {
    items = plan.focus.map((f) => {
      const t = open.find((x) => x.id === f.id);
      return t ? { t, why: f.why || '', ai: true } : null;
    }).filter(Boolean);
    label = WORK_AI.plan.ai ? 'AI 推荐' : '本地建议';
  }
  if (!items.length) {
    // 计划里的任务已经完成 / 被删时，退回本地排序，避免「下一步」空着
    items = open.slice().sort((a, b) => {
      const pa = Number(a.priority) || 0;
      const pb = Number(b.priority) || 0;
      if (pa !== pb) return pb - pa;
      const da = Number(a.due_at || 0), db = Number(b.due_at || 0);
      if (da && db) return da - db;
      if (da) return -1;
      if (db) return 1;
      return Number(b.updated_at || 0) - Number(a.updated_at || 0);
    }).slice(0, 3).map((t) => ({ t, why: '', ai: false }));
    label = items.length ? '按优先级 / 截止时间' : '';
  }
  if (sub) sub.textContent = label;
  box.innerHTML = '';
  if (!items.length) {
    box.innerHTML = '<div class="work-focus-empty">今天还没有要推进的任务。可以让 AI 拆一个新任务，或点「＋」新建。</div>';
    return;
  }
  items.forEach(({ t, why }) => {
    const row = document.createElement('div');
    row.className = 'work-focus-row';
    const main = document.createElement('div');
    main.className = 'work-focus-main';
    main.innerHTML = '<b>' + workAiEsc(t.title) + '</b>' +
      (why ? '<span>' + workAiEsc(why) + '</span>' : '') +
      (Number(t.due_at || 0) > 0 && Number(t.due_at) < now
        ? '<em>已逾期</em>' : '');
    const go = workAiBtn(
      t.status === 'running' ? '继续'
        : (t.status === 'paused' ? '继续' : (t.status === 'failed' ? '重试' : '开始')),
      'small primary',
      async (e) => {
        e.stopPropagation();
        if (t.status === 'running') { workOpenTask(t.id); return; }
        const op = t.status === 'paused' ? 'resume'
          : (t.status === 'failed' ? 'retry' : 'start');
        try {
          const r = await api('/api/work/tasks', { op, id: t.id });
          if (!r.ok) throw new Error(r.error || '启动失败');
          toast('已开始：' + t.title, 'success');
          await workLoad(true);
        } catch (err) { toastErr('启动任务失败', err); }
      });
    row.appendChild(main);
    row.appendChild(go);
    row.onclick = () => workOpenTask(t.id);
    box.appendChild(row);
  });
}

/* ---------------- 计划 ---------------- */

async function workAiLoadPlan() {
  if (WORK_AI.planBusy) return;
  WORK_AI.planBusy = true;
  WORK_AI.planApplied = new Set();
  workAiSetStatus('正在读今天的日程与任务…');
  workAiRender();
  let r = null;
  try { r = await api('/api/work/assist', { op: 'plan_today' }); }
  catch (e) { r = { ok: false, error: e.message || String(e) }; }
  WORK_AI.planBusy = false;
  if (!r || !r.ok) {
    WORK_AI.plan = null;
    toastErr('生成今日计划失败', new Error((r && r.error) || '未知错误'));
    workAiSetStatus('生成失败，可重试');
    workAiRender();
    return;
  }
  WORK_AI.plan = r;
  workAiSetStatus(r.ai ? 'AI 建议 · 由你确认后写入' : '本地建议 · 模型不可用', r.ai ? 'ok' : 'warn');
  workAiRender();
  workAiRenderFocus();
  workAiRenderOverview();
}

function workAiRenderPlan() {
  const body = workAiEl('work-ai-body');
  const foot = workAiEl('work-ai-foot');
  body.innerHTML = '';
  foot.innerHTML = '';
  if (WORK_AI.planBusy) {
    body.appendChild(workAiSkeleton('正在读今天的日程与任务…'));
    return;
  }
  if (!WORK_AI.plan) {
    const intro = document.createElement('div');
    intro.className = 'work-ai-intro';
    intro.innerHTML = '<b>把今天交给 AI 安排</b>'
      + '<p>它会读今天的日程和未完成任务，给出 1–3 个优先项和可选时间块；'
      + '你勾选后才写入日程。</p>';
    body.appendChild(intro);
    foot.appendChild(workAiBtn('生成今日计划', 'primary', workAiLoadPlan));
    return;
  }
  const p = WORK_AI.plan.plan || {};
  const badge = document.createElement('div');
  badge.className = 'work-ai-badge ' + (WORK_AI.plan.ai ? 'ai' : 'local');
  badge.textContent = WORK_AI.plan.ai ? 'AI 建议' : '本地建议';
  body.appendChild(badge);

  if (Array.isArray(p.focus) && p.focus.length) {
    const sec = workAiSection('优先推进');
    p.focus.forEach((f) => {
      const t = (WORK.tasks || []).find((x) => x.id === f.id);
      if (!t) return;
      const row = document.createElement('button');
      row.type = 'button';
      row.className = 'work-ai-row';
      row.innerHTML = '<span class="work-ai-row-main"><b>' + workAiEsc(t.title) + '</b>' +
        (f.why ? '<em>' + workAiEsc(f.why) + '</em>' : '') + '</span>';
      row.onclick = () => workOpenTask(t.id);
      sec.appendChild(row);
    });
    body.appendChild(sec);
  }

  const blocks = Array.isArray(p.blocks) ? p.blocks : [];
  if (blocks.length) {
    const sec = workAiSection('建议时间块', '勾选后加入今日日程');
    blocks.forEach((b, i) => {
      const done = WORK_AI.planApplied.has(i);
      const row = document.createElement('label');
      row.className = 'work-ai-block' + (done ? ' done' : '');
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.dataset.block = String(i);
      cb.checked = !done;
      cb.disabled = done;
      const text = document.createElement('span');
      text.innerHTML = '<b>' + workAiEsc(b.start) + '–' + workAiEsc(b.end) + '</b>'
        + '<em>' + workAiEsc(b.title) + '</em>';
      row.appendChild(cb);
      row.appendChild(text);
      sec.appendChild(row);
    });
    body.appendChild(sec);
  }

  if (p.note) {
    const note = document.createElement('div');
    note.className = 'work-ai-note';
    note.textContent = p.note;
    body.appendChild(note);
  }
  if (WORK_AI.plan.message) {
    const msg = document.createElement('div');
    msg.className = 'work-ai-note warn';
    msg.textContent = WORK_AI.plan.message;
    body.appendChild(msg);
  }

  const pending = blocks.filter((b, i) => !WORK_AI.planApplied.has(i)).length;
  if (pending) {
    foot.appendChild(workAiBtn('加入今日日程（' + pending + '）', 'primary', workAiApplyPlan));
  } else if (blocks.length) {
    const ok = document.createElement('span');
    ok.className = 'work-ai-done';
    ok.textContent = '时间块已加入日程';
    foot.appendChild(ok);
  }
  foot.appendChild(workAiBtn('重新生成', 'ghost', workAiLoadPlan));
}

async function workAiApplyPlan() {
  const boxes = Array.prototype.slice.call(
    document.querySelectorAll('#work-ai-body [data-block]:checked'));
  if (!boxes.length) { toast('先勾选要加入日程的时间块'); return; }
  const blocks = (WORK_AI.plan && WORK_AI.plan.plan && WORK_AI.plan.plan.blocks) || [];
  const date = workAiToday();
  let ok = 0, conflicts = 0, skipped = 0;
  for (const cb of boxes) {
    const i = Number(cb.dataset.block);
    const b = blocks[i];
    if (!b) continue;
    // 同一天同一时间已经有同名日程：不重复创建
    const startTs = new Date(date + 'T' + b.start).getTime() / 1000;
    const dup = (WORK.events || []).some((e) =>
      Math.abs(Number(e.occurrence_at || 0) - startTs) < 60
      && String(e.title || '') === b.title);
    if (dup) { skipped++; continue; }
    try {
      const r = await api('/api/calendar/events', {
        op: 'create', title: b.title,
        start_at: date + ' ' + b.start,
        end_at: date + ' ' + b.end,
        remind_before: 5,
        notes: b.task_id ? '来自 AI 今日计划' : '',
      });
      if (r && r.ok) {
        ok++;
        WORK_AI.planApplied.add(i);
        if (r.event && r.event._conflicts && r.event._conflicts.length) conflicts++;
      }
    } catch (e) { /* 单条失败继续 */ }
  }
  if (ok) {
    toast(conflicts
      ? '已加入 ' + ok + ' 个时间块，其中 ' + conflicts + ' 个与现有日程重叠'
      : '已加入 ' + ok + ' 个时间块', conflicts ? 'error' : 'success');
    await workLoad(true);
    workAiRender();
  } else if (skipped) {
    toast('这些时间块已经在日程里了');
  } else {
    toast('没有加入任何时间块，请检查时间是否冲突', 'error');
  }
}

/* ---------------- 任务拆解 ---------------- */

function workAiBreakdown(taskId) {
  const t = (WORK.tasks || []).find((x) => x.id === taskId);
  WORK_AI.tab = 'breakdown';
  workAiSetTab('breakdown');
  if (t) {
    WORK_AI.breakdown = null;
    workAiLoadBreakdown({ task_id: t.id, title: t.title, intent: t.intent || '' });
  }
}

async function workAiLoadBreakdown(payload) {
  if (WORK_AI.breakdownBusy) return;
  WORK_AI.breakdownBusy = true;
  workAiSetStatus('正在拆解任务…');
  workAiRender();
  let r = null;
  try { r = await api('/api/work/assist', Object.assign({ op: 'breakdown' }, payload)); }
  catch (e) { r = { ok: false, error: e.message || String(e) }; }
  WORK_AI.breakdownBusy = false;
  if (!r || !r.ok) {
    toastErr('任务拆解失败', new Error((r && r.error) || '未知错误'));
    workAiSetStatus('建议模式 · 由你确认后写入');
    workAiRender();
    return;
  }
  WORK_AI.breakdown = Object.assign({
    taskId: payload.task_id || '', title: payload.title || '',
  }, r);
  workAiSetStatus(r.ai ? 'AI 建议 · 由你确认后写入' : '本地建议 · 模型不可用', r.ai ? 'ok' : 'warn');
  workAiRender();
}

function workAiRenderBreakdown() {
  const body = workAiEl('work-ai-body');
  const foot = workAiEl('work-ai-foot');
  body.innerHTML = '';
  foot.innerHTML = '';
  if (WORK_AI.breakdownBusy) {
    body.appendChild(workAiSkeleton('正在拆解任务…'));
    return;
  }
  const bd = WORK_AI.breakdown;
  if (!bd) {
    // 表单：选已有任务，或给新任务起名
    const sec = workAiSection('拆解已有任务');
    const sel = document.createElement('select');
    sel.className = 'work-input';
    sel.id = 'work-ai-task';
    sel.innerHTML = '<option value="">选择任务…</option>' + (WORK.tasks || [])
      .filter((t) => !['done', 'canceled'].includes(t.status))
      .slice(0, 60)
      .map((t) => '<option value="' + workAiEsc(t.id) + '">'
        + workAiEsc(t.title) + '</option>').join('');
    sec.appendChild(sel);
    sec.appendChild(workAiBtn('拆解选中任务', 'primary', () => {
      const id = (workAiEl('work-ai-task') || {}).value;
      const t = (WORK.tasks || []).find((x) => x.id === id);
      if (!t) { toast('先选一个任务'); return; }
      workAiLoadBreakdown({ task_id: t.id, title: t.title, intent: t.intent || '' });
    }));
    body.appendChild(sec);

    const sec2 = workAiSection('拆一个新任务');
    const title = document.createElement('input');
    title.className = 'work-input';
    title.id = 'work-ai-new-title';
    title.placeholder = '例如：整理本周会议纪要';
    const intent = document.createElement('textarea');
    intent.className = 'work-input work-textarea';
    intent.id = 'work-ai-new-intent';
    intent.rows = 2;
    intent.placeholder = '补充说明（可选）：目标、已有材料、限制…';
    sec2.appendChild(title);
    sec2.appendChild(intent);
    sec2.appendChild(workAiBtn('拆解并准备新建', 'primary', () => {
      const ttl = (workAiEl('work-ai-new-title') || {}).value || '';
      if (!ttl.trim()) { toast('先写一个任务标题'); return; }
      workAiLoadBreakdown({ title: ttl.trim(), intent: (workAiEl('work-ai-new-intent') || {}).value || '' });
    }));
    body.appendChild(sec2);
    return;
  }

  const badge = document.createElement('div');
  badge.className = 'work-ai-badge ' + (bd.ai ? 'ai' : 'local');
  badge.textContent = (bd.ai ? 'AI 拆解' : '本地拆解') + ' · ' + bd.title;
  body.appendChild(badge);

  const sec = workAiSection('可执行步骤', '勾选后追加');
  bd.steps.forEach((s, i) => {
    const row = document.createElement('label');
    row.className = 'work-ai-step';
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.dataset.step = String(i);
    cb.checked = true;
    const text = document.createElement('span');
    text.textContent = s;
    row.appendChild(cb);
    row.appendChild(text);
    sec.appendChild(row);
  });
  body.appendChild(sec);
  if (bd.note) {
    const note = document.createElement('div');
    note.className = 'work-ai-note';
    note.textContent = bd.note;
    body.appendChild(note);
  }

  const apply = () => {
    const picked = Array.prototype.slice.call(
      document.querySelectorAll('#work-ai-body [data-step]:checked'))
      .map((cb) => bd.steps[Number(cb.dataset.step)])
      .filter(Boolean);
    if (!picked.length) { toast('至少勾选一个步骤'); return; }
    if (bd.taskId) workAiAppendSteps(bd.taskId, picked);
    else workOpenTaskDialog({ name: 'AI 拆解', title: bd.title, steps: picked });
  };
  foot.appendChild(workAiBtn(bd.taskId ? '追加到任务' : '用这些步骤新建任务', 'primary', apply));
  foot.appendChild(workAiBtn('重新拆解', 'ghost', () => {
    WORK_AI.breakdown = null;
    workAiRender();
  }));
}

async function workAiAppendSteps(taskId, steps) {
  let ok = 0;
  for (const title of steps) {
    try {
      const r = await api('/api/work/tasks', { op: 'add_step', id: taskId, title });
      if (r && r.ok) ok++;
    } catch (e) { /* 继续 */ }
  }
  if (ok) {
    toast('已追加 ' + ok + ' 个步骤', 'success');
    await workLoad(true);
    if (WORK_AI.breakdown) WORK_AI.breakdown = null;
    workAiRender();
  } else {
    toast('步骤没有追加成功', 'error');
  }
}

/* ---------------- 复盘 ---------------- */

async function workAiLoadReview(days) {
  if (WORK_AI.reviewBusy) return;
  WORK_AI.reviewBusy = true;
  workAiSetStatus('正在整理近 ' + days + ' 天…');
  workAiRender();
  let r = null;
  try { r = await api('/api/work/assist', { op: 'review', days }); }
  catch (e) { r = { ok: false, error: e.message || String(e) }; }
  WORK_AI.reviewBusy = false;
  if (!r || !r.ok) {
    toastErr('生成复盘失败', new Error((r && r.error) || '未知错误'));
    workAiSetStatus('建议模式 · 由你确认后写入');
    workAiRender();
    return;
  }
  WORK_AI.review = Object.assign({ days }, r);
  workAiSetStatus(r.ai ? 'AI 复盘 · 只读' : '本地复盘 · 模型不可用', r.ai ? 'ok' : 'warn');
  workAiRender();
}

function workAiRenderReview() {
  const body = workAiEl('work-ai-body');
  const foot = workAiEl('work-ai-foot');
  body.innerHTML = '';
  foot.innerHTML = '';
  if (WORK_AI.reviewBusy) {
    body.appendChild(workAiSkeleton('正在整理复盘…'));
    return;
  }
  if (!WORK_AI.review) {
    const intro = document.createElement('div');
    intro.className = 'work-ai-intro';
    intro.innerHTML = '<b>近 7 天工作复盘</b>'
      + '<p>AI 会读任务完成数、专注时长、逾期与标签分布，写一份「进展 / 卡点 / 下一步」。</p>';
    body.appendChild(intro);
    foot.appendChild(workAiBtn('生成复盘', 'primary', () => workAiLoadReview(7)));
    foot.appendChild(workAiBtn('近 14 天', 'ghost', () => workAiLoadReview(14)));
    return;
  }
  const badge = document.createElement('div');
  badge.className = 'work-ai-badge ' + (WORK_AI.review.ai ? 'ai' : 'local');
  badge.textContent = (WORK_AI.review.ai ? 'AI 复盘' : '本地复盘')
    + ' · 近 ' + (WORK_AI.review.days || 7) + ' 天';
  body.appendChild(badge);

  const md = document.createElement('div');
  md.className = 'work-ai-markdown';
  if (typeof safeRenderMarkdown === 'function') {
    md.innerHTML = safeRenderMarkdown(WORK_AI.review.markdown || '');
  } else {
    md.innerHTML = workAiEsc(WORK_AI.review.markdown || '').replace(/\n/g, '<br>');
  }
  body.appendChild(md);

  if (WORK_AI.review.message) {
    const note = document.createElement('div');
    note.className = 'work-ai-note warn';
    note.textContent = WORK_AI.review.message;
    body.appendChild(note);
  }
  foot.appendChild(workAiBtn('复制复盘', 'primary', () => {
    if (navigator.clipboard) {
      navigator.clipboard.writeText(WORK_AI.review.markdown || '')
        .then(() => toast('已复制复盘', 'success'));
    }
  }));
  foot.appendChild(workAiBtn('重新生成', 'ghost', () => workAiLoadReview(WORK_AI.review.days || 7)));
}

/* ---------------- 渲染入口 / 绑定 ---------------- */

function workAiSetTab(tab) {
  if (['plan', 'breakdown', 'review'].indexOf(tab) < 0) tab = 'plan';
  WORK_AI.tab = tab;
  document.querySelectorAll('#work-ai-tabs button').forEach((b) => {
    b.classList.toggle('on', b.dataset.wai === tab);
  });
  workAiRender();
}

function workAiRender() {
  const panel = workAiEl('work-ai-panel');
  if (!panel) return;
  if (WORK_AI.tab === 'breakdown') workAiRenderBreakdown();
  else if (WORK_AI.tab === 'review') workAiRenderReview();
  else workAiRenderPlan();
}

function workAiBind() {
  if (WORK_AI.bound) return;
  WORK_AI.bound = true;
  document.querySelectorAll('#work-ai-tabs button').forEach((b) => {
    b.onclick = () => workAiSetTab(b.dataset.wai);
  });
  const close = workAiEl('work-ai-close');
  if (close) close.onclick = () => {
    const panel = workAiEl('work-ai-panel');
    if (panel) panel.classList.add('hidden');
    const t = workAiEl('work-ai-toggle');
    if (t) t.classList.remove('on');
  };
  const toggle = workAiEl('work-ai-toggle');
  if (toggle) toggle.onclick = () => {
    const panel = workAiEl('work-ai-panel');
    if (!panel) return;
    const show = panel.classList.contains('hidden');
    panel.classList.toggle('hidden', !show);
    toggle.classList.toggle('on', show);
    if (show) workAiRender();
  };
  const panel = workAiEl('work-ai-panel');
  if (panel && toggle && !panel.classList.contains('hidden')) toggle.classList.add('on');
}

function workAiEnter() {
  workAiBind();
  workAiRender();
  workAiRenderOverview();
  workAiRenderFocus();
}

window.workAiBreakdown = workAiBreakdown;
