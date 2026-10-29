/* 学习观测面板：把 observations 里「做过什么、结果如何」读出来给用户看。
   数据来自 GET /api/observe → app/observe.py 的 summary()。 */
const OBSERVE_SOURCE_LABEL = {
  chat: '对话', math_arena: '数学竞技场', math_paper: '模拟卷', dojo: '研究道场',
};
let observeDays = 7;
let observeSubject = '';
let observeTopic = '';

function observeTime(sec) {
  const t = Number(sec || 0);
  if (!t) return '—';
  const d = new Date(t * 1000);
  const pad = (n) => String(n).padStart(2, '0');
  return (d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
}

function observeSourceName(src) {
  return OBSERVE_SOURCE_LABEL[src] || src || '对话';
}

function observeValueText(raw) {
  let v = raw;
  if (typeof raw === 'string') {
    try { v = JSON.parse(raw); } catch (e) { v = { note: raw }; }
  }
  if (!v || typeof v !== 'object') return '';
  const parts = [];
  if (typeof v.correct === 'boolean') parts.push(v.correct ? '正确' : '错误');
  if (v.score) parts.push('得分 ' + v.score);
  if (v.elapsed_ms) parts.push(Math.round(Number(v.elapsed_ms) / 1000) + 's');
  if (v.note) parts.push(String(v.note));
  return parts.join(' · ');
}

async function openObserve(days) {
  if (days) observeDays = days;
  showWithAnim($('#observe-modal'));
  $$('#observe-modal .tab').forEach((t) => {
    t.classList.toggle('active', Number(t.dataset.observeDays) === observeDays);
  });
  await loadObserve();
}

function closeObserve() {
  hideWithAnim($('#observe-modal'));
}

async function loadObserve() {
  $('#observe-cards').innerHTML = '';
  $('#observe-groups').innerHTML = '<p class="observe-empty">加载中…</p>';
  $('#observe-items').innerHTML = '';
  const clear = $('#observe-clear-filter');
  if (clear) clear.classList.toggle('hidden', !(observeSubject || observeTopic));
  try {
    let qs = '?days=' + observeDays;
    if (observeSubject) qs += '&subject=' + encodeURIComponent(observeSubject);
    if (observeTopic) qs += '&topic=' + encodeURIComponent(observeTopic);
    const res = await fetch('/api/observe' + qs, { headers: { 'X-Auth-Token': TOKEN } });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || '加载失败');
    renderObserve(data);
  } catch (e) {
    $('#observe-groups').innerHTML = '<p class="observe-empty">加载失败：' + escapeHtml(String(e)) + '</p>';
  }
}

function renderObserve(data) {
  const cards = [
    ['记录总数', fmtNum(data.count)],
    ['已判正确率', data.accuracy == null ? '—' : data.accuracy + '%'],
    ['覆盖主题', fmtNum((data.groups || []).length)],
    ['最近记录', observeTime(data.last)],
  ];
  $('#observe-cards').innerHTML = cards.map(([l, v]) =>
    '<div class="usage-card"><div class="uc-label">' + l + '</div>' +
    '<div class="uc-value">' + v + '</div></div>').join('');

  const groups = data.groups || [];
  $('#observe-groups').innerHTML = groups.length
    ? groups.map((g) => {
      const name = [g.subject, g.topic].filter(Boolean).join(' / ') || '未分类';
      const rate = g.accuracy == null ? '未判定' : '正确率 ' + g.accuracy + '%';
      return '<button type="button" class="observe-group" data-subject="' +
        escapeHtml(g.subject || '') + '" data-topic="' + escapeHtml(g.topic || '') + '">' +
        '<span class="og-name">' + escapeHtml(name) + '</span>' +
        '<span class="og-meta">' + g.attempts + ' 次 · ' + rate +
        ' · ' + observeTime(g.last) + '</span></button>';
    }).join('')
    : '<p class="observe-empty">近 ' + data.days + ' 天没有观测记录</p>';

  const items = data.items || [];
  $('#observe-items').innerHTML = items.length
    ? items.map((r) => {
      const name = [r.subject, r.topic].filter(Boolean).join(' / ') || '未分类';
      let mark = '';
      try {
        const v = JSON.parse(r.value || '{}');
        if (typeof v.correct === 'boolean') {
          mark = v.correct ? '<span class="oi-mark ok">✓</span>' : '<span class="oi-mark bad">✗</span>';
        }
      } catch (e) { /* value 不是 JSON 就不显示判定 */ }
      const meta = observeValueText(r.value);
      const evidence = String(r.evidence || '').replace(/\s+/g, ' ').trim();
      return '<div class="observe-item">' +
        '<div class="oi-head"><span class="oi-name">' + escapeHtml(name) + '</span>' +
        '<span class="oi-time">' + observeTime(r.created_at) + '</span></div>' +
        '<div class="oi-meta"><span class="oi-tag">' + escapeHtml(observeSourceName(r.source)) + '</span>' +
        mark + (meta ? '<span class="oi-value">' + escapeHtml(meta) + '</span>' : '') + '</div>' +
        (evidence ? '<div class="oi-evidence">' + escapeHtml(evidence.slice(0, 240)) + '</div>' : '') +
        '</div>';
    }).join('')
    : '<p class="observe-empty">没有明细</p>';
}

document.addEventListener('click', (e) => {
  const g = e.target.closest ? e.target.closest('.observe-group') : null;
  if (!g) return;
  observeSubject = g.dataset.subject || '';
  observeTopic = g.dataset.topic || '';
  loadObserve();
});
