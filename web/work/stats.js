/* 工作台 · stats
   由 work.js 按功能拆分；普通脚本共享全局作用域，加载顺序见 index.html。 */
'use strict';

async function workLoadStats(force) {
  if (WORK.stats && !force) { workRenderStats(); return; }
  try {
    const r = await api('/api/work/stats', { days: WORK.statsDays });
    WORK.stats = r.stats || null;
  } catch (e) {
    WORK.stats = null;
  }
  workRenderStats();
}

function workBarChart(values, labels, cls) {
  const wrap = workElNew('div', 'wv-bars');
  const max = Math.max(1, ...values.map((v) => Number(v) || 0));
  values.forEach((v, i) => {
    const bar = workElNew('div', 'wv-bar' + (cls ? ' ' + cls : ''));
    const fill = document.createElement('i');
    fill.style.height = Math.max(2, Math.round((Number(v) || 0) / max * 88)) + 'px';
    bar.appendChild(fill);
    bar.appendChild(workElNew('small', '', labels[i] || ''));
    bar.title = (labels[i] || '') + '：' + (Number(v) || 0);
    wrap.appendChild(bar);
  });
  return wrap;
}

function workRenderStats() {
  const box = workEl('work-view-stats');
  if (!box) return;
  box.innerHTML = '';
  const tabs = workElNew('div', 'wv-board-tools');
  const mk = (days, label) => {
    const b = workElNew('button', 'work-btn small ' + (WORK.statsDays === days ? '' : 'ghost'), label);
    b.onclick = () => { WORK.statsDays = days; WORK.stats = null; workLoadStats(); };
    tabs.appendChild(b);
  };
  mk(7, '近 7 天');
  mk(30, '近 30 天');
  box.appendChild(tabs);

  const st = WORK.stats;
  if (!st) {
    box.appendChild(workEmpty('正在统计…', '完成任务与专注记录后这里会有数据'));
    return;
  }
  const grid = workElNew('div', 'wv-stats');

  const kpi = workElNew('div', 'wv-card');
  kpi.appendChild(workElNew('h4', '', '概览'));
  const kpis = workElNew('div', 'wv-kpis');
  const addKpi = (val, label) => {
    const it = workElNew('div', 'wv-kpi');
    it.appendChild(workElNew('b', '', String(val)));
    it.appendChild(workElNew('span', '', label));
    kpis.appendChild(it);
  };
  const doneTotal = (st.done_by_day || []).reduce((a, b) => a + b, 0);
  const createdTotal = (st.created_by_day || []).reduce((a, b) => a + b, 0);
  addKpi(doneTotal, '完成任务');
  addKpi(createdTotal, '新建任务');
  addKpi(st.focus_minutes || 0, '专注分钟');
  addKpi(st.overdue || 0, '逾期未完成');
  kpi.appendChild(kpis);
  grid.appendChild(kpi);

  const labels = (st.days_at || []).map((ts) => {
    const d = new Date(Number(ts) * 1000);
    return (d.getMonth() + 1) + '/' + d.getDate();
  });
  const doneCard = workElNew('div', 'wv-card');
  doneCard.appendChild(workElNew('h4', '', '每日完成任务'));
  doneCard.appendChild(workBarChart(st.done_by_day || [], labels));
  grid.appendChild(doneCard);

  const focusCard = workElNew('div', 'wv-card');
  focusCard.appendChild(workElNew('h4', '', '每日专注分钟'));
  focusCard.appendChild(workBarChart(st.focus_by_day || [], labels, 'focus'));
  grid.appendChild(focusCard);

  const tagCard = workElNew('div', 'wv-card');
  tagCard.appendChild(workElNew('h4', '', '未完成任务的标签'));
  const tags = workElNew('div', 'wv-tagbar');
  (st.tag_count || []).forEach(([tag, n]) => {
    tags.appendChild(workElNew('span', 'wv-tag', tag + ' · ' + n));
  });
  if (!(st.tag_count || []).length) tags.appendChild(workElNew('span', 'wv-empty', '没有标签'));
  tagCard.appendChild(tags);
  grid.appendChild(tagCard);

  const od = workElNew('div', 'wv-card');
  od.appendChild(workElNew('h4', '', '逾期任务'));
  const list = workElNew('div', 'wv-list');
  (st.overdue_tasks || []).forEach((t) => {
    const row = workElNew('div', 'wv-list-row');
    row.appendChild(workElNew('span', '', t.title));
    row.appendChild(workElNew('b', '', workFmt(t.due_at, true)));
    row.onclick = () => workOpenTask(t.id);
    list.appendChild(row);
  });
  if (!(st.overdue_tasks || []).length) list.appendChild(workElNew('div', 'wv-empty', '没有逾期任务'));
  od.appendChild(list);
  grid.appendChild(od);

  box.appendChild(grid);
}

/* ---------------- 专注计时 ---------------- */

