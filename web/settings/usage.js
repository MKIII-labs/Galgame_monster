'use strict';

/* 设置弹窗 · usage.js
   Token 用量统计图
   原 web/settings.js 拆分而来：按 index.html 中的顺序加载，共享全局作用域。 */

/* ============================================================
   Token 用量统计
   ============================================================ */
let usageChart = null;
let usageBars = null;

const USAGE_RANGES = {
  day7: { mode: 'day', span: 7 },
  day30: { mode: 'day', span: 30 },
  month12: { mode: 'month', span: 12 },
};

// 接入通道：流水里的 api 字段（chat / light / vision）
const USAGE_API_LABEL = { chat: '对话主模型', light: '轻量任务', vision: '图像识别' };
// 用量来源：区分「对话」与剧本生成 / 节点判定等内部轻量调用
const USAGE_SRC_LABEL = {
  chat: '对话', vision: '图像识别', char_profile: '角色设定生成',
  gal_script: '剧本生成', gal_act: '续写下一幕', gal_node: '剧情节点判定',
};

const fmtNum = (n) => (n || 0).toLocaleString('en-US');

// 调用明细里的时间戳（秒）按本地时区展示
const fmtTime = (ts) => {
  const d = new Date((Number(ts) || 0) * 1000);
  return isNaN(d.getTime()) ? '' : d.toLocaleString('zh-CN', { hour12: false });
};

// 通用加载骨架（占位文案统一观感）——统一由 page-transition.js 的 gmSkeletonHtml 生成

async function openUsage(rangeKey) {
  const key = USAGE_RANGES[rangeKey] ? rangeKey : 'day7';
  const r = USAGE_RANGES[key];
  showWithAnim($('#usage-modal'));
  $$('#usage-modal .tab').forEach((t) => t.classList.toggle('active', t.dataset.usageRange === key));
  $('#usage-cards').innerHTML = '';
  $('#usage-tables').innerHTML = gmSkeletonHtml(5);
  // 图表要先销毁再清容器：直接 innerHTML='' 会让旧的 ECharts 实例挂在已移除的节点上
  if (usageChart) { usageChart.dispose(); usageChart = null; }
  if (usageBars) { usageBars.dispose(); usageBars = null; }
  $('#usage-chart').innerHTML = gmLoadingHtml('正在统计用量…', 'center');
  $('#usage-bars').innerHTML = gmLoadingHtml('正在统计模型用量…', 'center');
  const rb = $('#usage-balance-refresh');
  if (rb) rb.onclick = () => loadUsageBalance();
  loadUsageBalance();                  // 余额单独拉，不阻塞下面的用量主体
  try {
    const res = await fetch('/api/usage?mode=' + r.mode + '&span=' + r.span,
      { headers: { 'X-Auth-Token': TOKEN } });
    renderUsage(await res.json());
  } catch (e) {
    $('#usage-tables').innerHTML = '<p class="usage-empty">加载失败：' + escapeHtml(String(e)) + '</p>';
  }
}

/* 用量弹窗顶部的账户余额：复用设置页那套 /api/balance + formatBalance。
   失败时给「去哪儿检查」的引导，而不是只丢一句技术错误。 */
async function loadUsageBalance() {
  const el = $('#usage-balance-text');
  if (!el) return;
  el.classList.remove('warn');
  el.textContent = '查询中…';
  try {
    const r = await api('/api/balance', { provider: 'chat' });
    if (r.ok) { el.textContent = formatBalance(r); return; }
    el.textContent = r.error || '未配置余额查询（可在「设置 → 接入与密钥」里填写）';
    el.classList.add('warn');
  } catch (e) {
    el.textContent = '查询失败：接口地址不可达或密钥无效 —— 可在「设置 → 接入与密钥 → 账户余额」里检查';
    el.classList.add('warn');
  }
}

function closeUsage() {
  hideWithAnim($('#usage-modal'), () => {
    if (usageChart) { usageChart.dispose(); usageChart = null; }
    if (usageBars) { usageBars.dispose(); usageBars = null; }
  });
}

function renderUsage(data) {
  const s = data.summary || {};
  const d = data.derived || {};
  // 批 1 观测：单次调用分布 + 前缀复用（旧后端没有这两块时优雅降级）
  const cs = data.call_stats || {};
  const os = data.obs_stats || {};
  // 未命中构成（批 1 补测）：旧后端没有这个字段时整体降级为「—」
  const mp = os.miss_parts || {};
  const missTotal = Number(mp.total) || 0;
  const missNew = Number(mp.new) || 0;
  const missCold = Number(mp.cold) || 0;
  const missRebuild = Number(mp.rebuild) || 0;
  const missDrift = Number(mp.drift) || 0;
  const missTurns = Number(mp.turns) || 0;
  const hasCall = Number(cs.count) > 0;
  const pct1 = (v) => (v == null ? '—' : Math.round(Number(v) * 1000) / 10 + '%');
  const peak = d.peak || null;
  const unit = data.mode === 'month' ? '月' : '日';
  const pctText = (v) => (v == null ? '—' : v + '%');
  const pctOf = (a, b) => (Number(b) ? Math.round(Number(a) * 1000 / Number(b)) / 10 : null);
  const shareOf = (v, total) => {
    const t = Number(total) || 0;
    const pct = t > 0 ? Math.min(100, (Number(v) || 0) * 100 / t) : 0;
    return '<span class="usage-share"><span class="us-track"><i class="us-fill" style="width:' +
      pct.toFixed(2) + '%"></i></span>' + (t > 0 ? Math.round(pct * 10) / 10 + '%' : '—') + '</span>';
  };

  const cards = [
    ['总 tokens', fmtNum(s.total), '日均 ' + fmtNum(d.avg_active) + '（仅计有消耗的' + unit + '）'],
    ['输入', fmtNum(s.prompt), '占合计 ' + pctText(pctOf(s.prompt, s.total))],
    ['输出', fmtNum(s.completion), '占合计 ' + pctText(pctOf(s.completion, s.total))],
    ['缓存命中', fmtNum(s.cached), '命中率 ' + pctText(d.hit_rate) + '（占输入）'],
    ['调用次数', fmtNum(s.calls), '平均每次 ' + fmtNum(d.avg_call) + ' tokens'],
    // 单次调用看的是「每次模型请求的输入」，比回合累计更贴近真实成本
    ['单次调用 P50', hasCall ? fmtNum(cs.p50) : '—',
      hasCall ? ('P90 ' + fmtNum(cs.p90) + ' · P95 ' + fmtNum(cs.p95) +
                 ' · 样本 ' + fmtNum(cs.count) + ' 次')
              : '旧记录没有逐轮明细'],
    // 前缀复用：越低说明 system/历史越不稳定，缓存越容易失效
    ['前缀复用', os.avg_prefix_ratio == null ? '—' : pct1(os.avg_prefix_ratio),
      os.warm ? ('system 变更 ' + fmtNum(os.sys_changed) + '/' + fmtNum(os.warm) +
                 ' 轮 · 折叠 ' + fmtNum(os.fold) + ' 次')
              : '旧记录没有前缀采样'],
    // 未命中构成：未命中单价约为命中价的 50 倍，钱主要花在这里
    ['未命中构成', missTotal ? pct1(missNew / missTotal) + ' 是新增' : '—',
      missTotal ? ('首请求 ' + pct1(missCold / missTotal) + ' · 新增 ' + pct1(missNew / missTotal) +
                   ' · 折叠重建 ' + pct1(missRebuild / missTotal) + ' · 漂移 ' + pct1(missDrift / missTotal) +
                   '（' + fmtNum(missTurns) + ' 个回合）')
                : '旧记录没有拆解数据'],
    ['峰值' + unit, peak ? fmtNum(peak.total) : '—', peak ? peak.bucket : '该区间无记录'],
    ['活跃' + unit, fmtNum(d.active), '区间共 ' + fmtNum((data.labels || []).length) + ' ' + unit],
  ];
  $('#usage-cards').innerHTML = cards.map(([l, v, sub]) =>
    '<div class="usage-card"><div class="uc-label">' + l + '</div>' +
    '<div class="uc-value">' + v + '</div>' +
    (sub ? '<div class="uc-sub">' + sub + '</div>' : '') + '</div>').join('');

  const byModel = data.by_model || [];
  const byEndpoint = data.by_endpoint || [];
  const byApi = data.by_api || [];
  const bySource = Object.keys(data.by_source || {})
    .map((k) => Object.assign({ source: k }, data.by_source[k]))
    .sort((a, b) => b.total - a.total);
  const rows = data.rows || [];
  const MAX_ROWS = 200;

  const head = (cols) => '<tr>' + cols.map((c) => '<th>' + c + '</th>').join('') + '</tr>';
  const tr = (cells) => '<tr>' + cells.map((c) => '<td>' + c + '</td>').join('') + '</tr>';
  const numCols = ['占比', '输入', '输出', '合计', '缓存命中', '次数'];
  const modelCols = ['模型'].concat(numCols);
  const rowCols = [data.mode === 'month' ? '月份' : '日期', '模型', '输入', '输出',
                   '合计', '缓存命中', '次数'];
  const empty = '<p class="usage-empty">该时间段暂无用量记录</p>';
  const table = (cols, list, rowFn) => list.length
    ? '<table><thead>' + head(cols) + '</thead><tbody>' + list.map(rowFn).join('') + '</tbody></table>'
    : empty;

  let html = '<h3>按 API 端点（服务商）</h3>';
  html += table(['端点'].concat(numCols), byEndpoint, (r) => tr([
    escapeHtml(r.endpoint), shareOf(r.total, s.total), fmtNum(r.prompt), fmtNum(r.completion),
    fmtNum(r.total), fmtNum(r.cached), fmtNum(r.calls)]));

  // 接入通道：区分主对话、轻量任务（标题/摘要等）与图像识别，便于判断 token 花在哪一类请求
  html += '<h3>按接入通道</h3>';
  html += table(['通道'].concat(numCols), byApi, (r) => tr([
    escapeHtml(USAGE_API_LABEL[r.api] || r.api), shareOf(r.total, s.total), fmtNum(r.prompt),
    fmtNum(r.completion), fmtNum(r.total), fmtNum(r.cached), fmtNum(r.calls)]));

  html += '<h3>按模型汇总</h3>';
  html += table(modelCols, byModel, (r) => tr([
    escapeHtml(r.model), shareOf(r.total, s.total), fmtNum(r.prompt), fmtNum(r.completion),
    fmtNum(r.total), fmtNum(r.cached), fmtNum(r.calls)]));

  html += '<h3>按来源</h3>';
  html += table(['来源'].concat(numCols), bySource, (r) => tr([
    escapeHtml(USAGE_SRC_LABEL[r.source] || r.source), shareOf(r.total, s.total),
    fmtNum(r.prompt), fmtNum(r.completion), fmtNum(r.total), fmtNum(r.cached), fmtNum(r.calls)]));

  html += '<h3>按' + (data.mode === 'month' ? '月份' : '日期') + '明细</h3>';
  html += rows.length
    ? '<table><thead>' + head(rowCols) + '</thead><tbody>' +
      rows.slice(0, MAX_ROWS).map((r) => tr([r.bucket, escapeHtml(r.model), fmtNum(r.prompt),
        fmtNum(r.completion), fmtNum(r.total), fmtNum(r.cached), fmtNum(r.calls)])).join('') +
      '</tbody></table>' +
      (rows.length > MAX_ROWS
        ? '<p class="usage-empty">仅显示前 ' + MAX_ROWS + ' 行（共 ' + rows.length + ' 行）</p>' : '')
    : empty;

  // 逐次调用明细：用于定位「哪一次、哪个接口」花掉了 token
  const calls = data.calls || [];
  const callCols = ['时间', '通道', '端点', '模型', '来源', '输入', '输出', '合计', '缓存命中'];
  html += '<h3>最近调用明细（溯源）</h3>';
  html += calls.length
    ? '<table><thead>' + head(callCols) + '</thead><tbody>' +
      calls.map((r) => tr([fmtTime(r.ts), escapeHtml(USAGE_API_LABEL[r.api] || r.api),
        escapeHtml(r.endpoint), escapeHtml(r.model),
        escapeHtml(USAGE_SRC_LABEL[r.source] || r.source), fmtNum(r.prompt),
        fmtNum(r.completion), fmtNum(r.total), fmtNum(r.cached)])).join('') + '</tbody></table>' +
      ((data.calls_total || 0) > calls.length
        ? '<p class="usage-empty">仅显示最近 ' + calls.length + ' 条（共 ' + data.calls_total + ' 条）</p>' : '')
    : '<p class="usage-empty">该时间段暂无调用记录</p>';

  $('#usage-tables').innerHTML = html;

  const dayWord = data.mode === 'month' ? '每月' : '每日';
  const ct = $('#usage-chart-title');
  if (ct) ct.textContent = '趋势（按模型 · ' + dayWord + ' tokens）';
  const bt = $('#usage-bars-title');
  if (bt) bt.textContent = dayWord + '输入 / 输出与调用次数';

  // 图表放在最后渲染：表格插入后 .modal-body 才可能出现滚动条，
  // 滚动条会压缩容器宽度；若先画图，canvas 会比可视区宽，右侧标签被裁掉。
  drawUsageChart(data);
  drawUsageBars(data);
}

// 横轴标签：日模式只留「月-日」，月模式保留完整「年-月」（跨年时不能省）
const usageAxisLabel = (mode, k) => (mode === 'month' ? String(k) : String(k).slice(5));

function usageAxisColors() {
  const dark = isDarkTheme();
  return { axis: dark ? '#a3a3ad' : '#6b6b76', line: dark ? '#333336' : '#e6e6ea' };
}

function drawUsageChart(data) {
  const el = $('#usage-chart');
  const labels = data.labels || [];
  const models = data.models || [];
  if (!window.echarts) {
    el.innerHTML = '<p class="usage-empty">图表库未加载（需联网加载 ECharts），下方表格仍可查看明细。</p>';
    return;
  }
  el.innerHTML = '';
  if (usageChart) { usageChart.dispose(); usageChart = null; }
  const c = usageAxisColors();
  usageChart = echarts.init(el);
  usageChart.setOption({
    tooltip: { trigger: 'axis', valueFormatter: (v) => Number(v).toLocaleString('en-US') },
    legend: models.length ? { data: models, bottom: 0, textStyle: { color: c.axis } } : undefined,
    // containLabel: 让 ECharts 为轴标签预留空间；
    // right 再留出余量，避免最右侧标签（如 2026-10-09）贴边被裁
    grid: {
      left: 8, right: 40, top: 18, bottom: models.length ? 46 : 26,
      containLabel: true,
    },
    xAxis: {
      type: 'category', data: labels.map((k) => usageAxisLabel(data.mode, k)),
      boundaryGap: false,
      axisLabel: { color: c.axis, hideOverlap: true },
      axisLine: { lineStyle: { color: c.line } },
    },
    yAxis: { type: 'value', axisLabel: { color: c.axis }, splitLine: { lineStyle: { color: c.line } } },
    series: models.map((m) => ({
      name: m, type: 'line', smooth: true, symbol: 'circle', symbolSize: 5,
      data: labels.map((k) => ((data.series || {})[m] || {})[k] || 0),
      areaStyle: { opacity: 0.08 },
    })),
  });
  // 容器尺寸可能在 init 之后才最终确定（弹窗刚显示、出现滚动条等），
  // 再同步一次，避免 canvas 比容器宽导致右侧标签跑出可视区
  requestAnimationFrame(() => { if (usageChart) usageChart.resize(); });
}

// 柱状图：输入 / 输出堆叠（左轴 tokens）+ 调用次数折线（右轴）。
// 折线看「哪天贵」，柱子看「贵在输入还是输出」。
function drawUsageBars(data) {
  const el = $('#usage-bars');
  if (!el) return;
  if (!window.echarts) {
    el.innerHTML = '<p class="usage-empty">图表库未加载（需联网加载 ECharts）。</p>';
    return;
  }
  const daily = data.daily || [];
  el.innerHTML = '';
  if (usageBars) { usageBars.dispose(); usageBars = null; }
  const c = usageAxisColors();
  const axisNum = (v) => (v >= 1e8 ? (v / 1e8).toFixed(1) + '亿'
    : v >= 1e4 ? (v / 1e4).toFixed(v >= 1e5 ? 0 : 1) + '万' : v);
  usageBars = echarts.init(el);
  usageBars.setOption({
    tooltip: {
      trigger: 'axis', axisPointer: { type: 'shadow' },
      valueFormatter: (v) => Number(v).toLocaleString('en-US'),
    },
    legend: { data: ['输入', '输出', '调用次数'], bottom: 0, textStyle: { color: c.axis } },
    // top 要留出轴名（tokens / 次数）的高度，否则会被画布上沿裁掉
    grid: { left: 8, right: 40, top: 34, bottom: 46, containLabel: true },
    xAxis: {
      type: 'category',
      data: daily.map((r) => usageAxisLabel(data.mode, r.bucket)),
      axisLabel: { color: c.axis, hideOverlap: true },
      axisLine: { lineStyle: { color: c.line } },
    },
    yAxis: [
      { type: 'value', name: 'tokens', nameTextStyle: { color: c.axis, fontSize: 11 },
        axisLabel: { color: c.axis, formatter: axisNum },
        splitLine: { lineStyle: { color: c.line } } },
      { type: 'value', name: '次数', nameTextStyle: { color: c.axis, fontSize: 11 },
        axisLabel: { color: c.axis }, splitLine: { show: false }, minInterval: 1 },
    ],
    series: [
      { name: '输入', type: 'bar', stack: 'tok', barMaxWidth: 28,
        data: daily.map((r) => r.prompt) },
      { name: '输出', type: 'bar', stack: 'tok', barMaxWidth: 28,
        data: daily.map((r) => r.completion) },
      { name: '调用次数', type: 'line', yAxisIndex: 1, smooth: true,
        symbol: 'circle', symbolSize: 5, data: daily.map((r) => r.calls) },
    ],
  });
  requestAnimationFrame(() => { if (usageBars) usageBars.resize(); });
}

function buildLearningPresetSelect(cfg) {
  cfg = cfg || settingsDraft();
  const builtin = state.builtinPresets || {};
  if (!cfg.learning_presets) cfg.learning_presets = {};
  const userPresets = cfg.learning_presets;
  const sel = $('#learning-preset');
  sel.innerHTML = '';

  const add = (value, label) => {
    const o = document.createElement('option');
    o.value = value;
    o.textContent = label;
    sel.appendChild(o);
    return o;
  };
  add('', state.builtinPresetLabel || '默认（内置）');
  Object.keys(builtin).forEach((n) => add('builtin:' + n, '内置 · ' + n));
  Object.keys(userPresets).forEach((n) => add('user:' + n, n));

  // 自动识别当前文本对应哪个预设
  const cur = $('#learning-prompt').value.trim();
  let match = '';
  if (cur) {
    Object.keys(builtin).forEach((n) => { if (!match && builtin[n].trim() === cur) match = 'builtin:' + n; });
    Object.keys(userPresets).forEach((n) => { if (!match && userPresets[n].trim() === cur) match = 'user:' + n; });
    if (!match) {
      add('__custom__', '当前内容（未保存为预设）');
      match = '__custom__';
    }
  }
  sel.value = match;

  sel.onchange = () => {
    const v = sel.value;
    if (v === '__custom__') return;
    if (!v) { $('#learning-prompt').value = ''; return; }
    const kind = v.split(':')[0];
    const name = v.slice(kind.length + 1);
    $('#learning-prompt').value = (kind === 'builtin' ? builtin[name] : userPresets[name]) || '';
  };

  $('#learning-preset-save').onclick = async () => {
    const text = $('#learning-prompt').value.trim();
    if (!text) { toast('学习指令为空，无法保存为预设'); return; }
    const name = await appPrompt('预设名称');
    if (!name || !name.trim()) return;
    const n = name.trim();
    userPresets[n] = text;
    const existing = Array.from(sel.options).find((o) => o.value === 'user:' + n);
    if (existing) existing.textContent = n;
    else add('user:' + n, n);
    sel.value = 'user:' + n;
    toast('预设「' + n + '」已加入，点「保存」生效');
    queueDirtyCheck();
  };

  $('#learning-preset-del').onclick = () => {
    const v = sel.value;
    if (!v || v.indexOf('user:') !== 0) { toast('内置预设不可删除'); return; }
    const n = v.slice(5);
    delete userPresets[n];
    const opt = Array.from(sel.options).find((o) => o.value === v);
    if (opt) opt.remove();
    sel.value = '';
    toast('已删除，点「保存」生效');
    queueDirtyCheck();
  };
}
