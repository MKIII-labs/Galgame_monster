/* ============================================================
   功能大厅：模拟股市
   ------------------------------------------------------------
   行情由后端 app/stock.py 拉取并冻结成快照；前端只负责展示与下单。
   人类与 AI 共用同一套撮合规则：T+1、100 股整手、涨跌停、费用、滑点。
   ============================================================ */
'use strict';

const STOCK_DEFAULT_SYMBOLS = [
  { symbol: 'sh600519', name: '贵州茅台', type: '股票' },
  { symbol: 'sz000001', name: '平安银行', type: '股票' },
  { symbol: 'sh510300', name: '沪深300ETF', type: 'ETF' },
  { symbol: 'sz300750', name: '宁德时代', type: '股票' },
];

const CRYPTO_DEFAULT_SYMBOLS = [
  { symbol: 'BTCUSDT', name: '比特币', type: '币' },
  { symbol: 'ETHUSDT', name: '以太坊', type: '币' },
  { symbol: 'SOLUSDT', name: 'Solana', type: '币' },
];

const stockState = {
  gen: 0,
  el: {},
  game: null,
  mode: 'replay',
  market: 'stock',
  selected: STOCK_DEFAULT_SYMBOLS.slice(),
  results: [],
  symbol: '',
  side: 'buy',
  busy: false,
  aiBusy: {},
  aiErrors: {},
  aiRunFor: '',
  autoRun: false,
  autoTimer: null,
  searchTimer: null,
  barsCache: {},
  barsFor: '',
  barsCursor: '',
  games: [],
  chartBars: [],
  chartMarks: [],       // 当前标的的成交标注（与 chartBars 同一时间口径）
  markFilter: 'all',    // 成交标注筛选：all / me / ai
  tab: 'pos',           // 侧栏标签：pos / rival / train
  analysis: null,       // 训练辅助数据（预测 / 状态 / 计分板 / 参考散户）
  analysisSeq: 0,
  predictDir: 'up',     // 预测方向：up / flat / down
  marksCache: {},       // cursor|symbol -> marks
  optimistic: null,     // 下单中的本地待成交单（乐观更新）
  addPop: null,         // 「加入标的」弹层
  zoom: null,           // K 线全屏放大浮层（首次点「放大」时才建）
  history: null,        // 时间轴回溯浮层
  searchPanel: null,    // 历史分析检索浮层
  compare: null,        // 多节点对比浮层
  timeline: [],         // 可回溯日期
  risk: null,           // 组合风险指标（/risk 异步加载）
};

hallRegister({
  id: 'stock',
  name: '模拟股市',
  icon: 'i-chart',
  tag: '对局',
  desc: '用真实 A 股 / ETF 或 BTC / ETH / SOL 行情，和三个不同风格的 AI 比一比谁更会交易。',
  meta: 'A股 · ETF · 币市 · 历史回放 / 实时每日盘',
  mount: stockMount,
  unmount: stockUnmount,
  onHide: stockHideAll,     // 离开大厅时收起所有浮层，否则会盖在别的模式上
});

/* 收起全部浮层（K 线放大 / 时间轴回溯 / 分析检索 / 多节点对比） */
function stockHideAll() {
  stockCloseZoom();
  stockCloseHistory();
  stockCloseSearch();
  stockCloseCompare();
  stockCloseAddSymbol();
}

/* ---------------- 挂载 / 卸载 ---------------- */

function stockMount(root) {
  const gen = ++stockState.gen;
  stockState.el = { root: root };
  stockState.game = null;
  stockState.symbol = '';
  stockState.barsCache = {};
  stockState.barsFor = '';
  stockState.barsCursor = '';
  stockState.chartMarks = [];
  stockState.marksCache = {};
  stockState.tab = 'pos';
  stockState.optimistic = null;
  stockState.addPop = null;
  stockState.analysis = null;
  stockState.analysisSeq = 0;
  stockState.predictDir = 'up';
  stockState.timeline = [];
  stockState.aiBusy = {};
  stockState.aiErrors = {};
  stockState.aiRunFor = '';
  stockState.autoRun = false;
  stockClearTimer();
  if (stockState.onResize) window.removeEventListener('resize', stockState.onResize);
  stockState.onResize = () => {
    if (stockState.game) stockDrawNav();      // 侧栏宽度变了，净值曲线跟着重算
  };
  window.addEventListener('resize', stockState.onResize);
  root.innerHTML = '<div class="stock-loading">正在加载模拟盘…</div>';
  stockLoadSetup(gen);
}

function stockUnmount() {
  stockState.gen += 1;
  stockClearTimer();
  if (stockState.onDocClick) {
    document.removeEventListener('click', stockState.onDocClick);
    stockState.onDocClick = null;
  }
  if (stockState.onResize) {
    window.removeEventListener('resize', stockState.onResize);
    stockState.onResize = null;
  }
  stockState.game = null;
  stockState.analysis = null;
  stockState.analysisSeq += 1;
  stockState.el = {};
  stockDestroyZoom();
  stockDestroyHistory();
  stockDestroySearch();
  stockDestroyCompare();
  stockCloseAddSymbol();
}

function stockClearTimer() {
  if (stockState.autoTimer) {
    clearInterval(stockState.autoTimer);
    stockState.autoTimer = null;
  }
}

function stockEsc(s) {
  return String(s == null ? '' : s).replace(/[&<>"]/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

/* ---- 市场差异：A 股 vs 币市 ---- */
function stockIsCrypto() {
  if (stockState.game) return stockState.game.market === 'crypto';
  return stockState.market === 'crypto';
}

function stockUnit() {
  return stockIsCrypto() ? '枚' : '股';
}

function stockCur() {
  return stockIsCrypto() ? 'USDT' : '元';
}

// 币市数量可能是 0.000001 级别：去掉末尾多余的 0，股票保持整数
function stockQtyText(v) {
  const n = Number(v) || 0;
  if (!stockIsCrypto()) return String(v);
  return String(Number(n.toFixed(6)));
}

function stockBenchName() {
  const bm = stockState.game && stockState.game.benchmark;
  if (bm && bm.name) return bm.name;
  return stockIsCrypto() ? '比特币' : '沪深300';
}

function stockScrollTop() {
  const body = stockState.el.root && stockState.el.root.closest('.hall-body');
  if (body) body.scrollTop = 0;
}

function stockMoney(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) return '0.00';
  return n.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function stockPct(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) return '0.00%';
  return (n >= 0 ? '+' : '') + n.toFixed(2) + '%';
}

function stockMsg(text, err) {
  const box = stockState.el.msg;
  if (!box) { if (text) toast(text, err ? 'error' : 'success'); return; }
  box.textContent = text || '';
  box.classList.toggle('err', !!err);
  box.classList.toggle('hidden', !text);
}

/* ---------------- 开局设置 ---------------- */

async function stockLoadSetup(gen) {
  try {
    const data = await api('/api/hall/stock/list', {});
    if (gen !== stockState.gen) return;
    stockState.games = data.games || [];
    stockRenderSetup(stockState.games);
  } catch (e) {
    if (gen !== stockState.gen) return;
    stockState.el.root.innerHTML = '<div class="stock-loading err">加载失败：'
      + stockEsc(e.message || e) + '</div>';
  }
}

function stockRenderSetup(games) {
  const root = stockState.el.root;
  stockCloseZoom();   // 回开局设置就没有行情了，放大浮层必须收掉
  stockCloseAddSymbol();
  stockState.el.chart = null;
  const cryptoSetup = stockState.market === 'crypto';
  const chips = stockState.selected.map((it) => (
    '<span class="stock-chip" data-sym="' + stockEsc(it.symbol) + '">'
    + stockEsc(it.name) + '<b>' + stockEsc(it.symbol) + '</b>'
    + (cryptoSetup ? '' : '<button class="stock-chip-x" title="移除">×</button>') + '</span>'
  )).join('');
  const gamesHtml = games.length ? (
    '<div class="stock-games">'
    + '<div class="stock-games-title">最近的模拟盘</div>'
    + games.map((g) => (
      '<div class="stock-game-row">'
      + '<div><b>' + stockEsc(g.name) + '</b>'
      + '<span>' + stockEsc(g.market === 'crypto' ? '币市' : 'A股')
      + ' · ' + stockEsc(g.mode === 'live' ? '实时' : '回放')
      + ' · ' + stockEsc(g.cursor_date) + ' · ' + (g.status === 'finished' ? '已结束' : '进行中')
      + '</span></div>'
      + '<div class="stock-game-num">' + stockPct(g.return_pct) + '</div>'
      + '<button class="stock-btn" data-open="' + stockEsc(g.id) + '">继续</button>'
      + '<button class="stock-btn ghost" data-del="' + stockEsc(g.id) + '">删除</button>'
      + '</div>'
    )).join('')
    + '</div>'
  ) : '';

  root.innerHTML = [
    '<div class="stock-setup">',
    '  <div class="stock-setup-head">',
    '    <h3>' + (cryptoSetup ? '模拟币市' : '模拟股市') + '</h3>',
    '    <p>' + (cryptoSetup
      ? '真实 BTC / ETH / SOL 日线，7×24、无涨跌停、可买小数；和 A 股共用同一套对局与 AI 对手。'
      : '真实 A 股 / ETF 行情，历史回放或实时每日盘；可以和 AI 对战，也可以关掉 AI 独自练习。')
      + '</p>',
    '  </div>',
    '  <div class="stock-mode-row stock-market-row">',
    '    <button class="stock-mode' + (!cryptoSetup ? ' active' : '')
      + '" data-market="stock">A 股</button>',
    '    <button class="stock-mode' + (cryptoSetup ? ' active' : '')
      + '" data-market="crypto">币市</button>',
    '    <span class="stock-mode-tip">'
      + (cryptoSetup ? 'BTC / ETH / SOL · 单边手续费 0.1%' : '沪深 A 股 / ETF')
      + '</span>',
    '  </div>',
    '  <div class="stock-mode-row">',
    '    <button class="stock-mode' + (stockState.mode === 'replay' ? ' active' : '')
      + '" data-mode="replay">历史回放</button>',
    '    <button class="stock-mode' + (stockState.mode === 'live' ? ' active' : '')
      + '" data-mode="live">实时每日盘</button>',
    '    <span class="stock-mode-tip" id="stock-mode-tip">'
      + (stockState.mode === 'replay'
        ? '冻结一段历史行情，点「下一日」快速推进。'
        : '每个真实交易日收盘后结算一次，节奏慢但更像实盘。')
      + '</span>',
    '  </div>',
    cryptoSetup ? '' : [
      '  <div class="stock-search-box">',
      '    <input id="stock-q" type="text" placeholder="搜索股票 / ETF：茅台、510300、宁德时代…" autocomplete="off">',
      '    <div id="stock-results" class="stock-results hidden"></div>',
      '  </div>',
    ].join(''),
    '  <div class="stock-chips" id="stock-chips">' + chips + '</div>',
    '  <div class="stock-opts">',
    '    <label id="stock-days-wrap">回放天数 <select id="stock-days">'
      + [10, 20, 30, 45, 60].map((n) => '<option value="' + n + '"'
        + (n === 20 ? ' selected' : '') + '>' + n + ' 个交易日</option>').join('')
      + '</select></label>',
    '    <div class="stock-opt-ai">',
    '      <span class="stock-opt-title">AI 对手</span>',
    '      <div class="stock-opt-ai-row">',
    '        <span id="stock-ai-switch"></span>',
    '        <select id="stock-ai">'
      + [1, 2, 3].map((n) => '<option value="' + n + '"'
        + (n === 3 ? ' selected' : '') + '>' + n + ' 位</option>').join('')
      + '</select>',
    '      </div>',
    '    </div>',
    '    <label>初始资金 <select id="stock-cash">'
      + [500000, 1000000, 5000000].map((n) => '<option value="' + n + '"'
        + (n === 1000000 ? ' selected' : '') + '>'
        + (n >= 10000 ? (n / 10000) + ' 万' : n) + '</option>').join('')
      + '</select></label>',
    '    <button class="stock-btn primary" id="stock-create">开始模拟</button>',
    '  </div>',
    '  <div class="stock-msg hidden" id="stock-setup-msg"></div>',
    gamesHtml,
    '</div>',
  ].join('');

  stockState.el.setupMsg = root.querySelector('#stock-setup-msg');
  stockState.el.q = root.querySelector('#stock-q');
  stockState.el.results = root.querySelector('#stock-results');
  stockState.el.chips = root.querySelector('#stock-chips');
  stockState.el.days = root.querySelector('#stock-days');
  stockState.el.daysWrap = root.querySelector('#stock-days-wrap');
  stockState.el.ai = root.querySelector('#stock-ai');
  stockState.el.aiSwitch = root.querySelector('#stock-ai-switch');
  stockState.el.cash = root.querySelector('#stock-cash');
  stockState.el.create = root.querySelector('#stock-create');
  const cfgNow = (typeof state !== 'undefined' && state.config) || {};
  let savedAiCount = Number(cfgNow.hall_stock_ai_count);
  savedAiCount = Number.isFinite(savedAiCount) && savedAiCount > 0
    ? Math.max(1, Math.min(3, Math.round(savedAiCount))) : 3;
  const aiOn = hallAiEnabled('stock');
  stockState.el.ai.value = String(savedAiCount);
  stockState.el.ai.disabled = !aiOn;
  stockState.el.ai.style.display = aiOn ? '' : 'none';
  stockState.el.aiSwitch.appendChild(hallAiSwitch('stock', 'AI 对手', (on) => {
    stockState.el.ai.disabled = !on;
    stockState.el.ai.style.display = on ? '' : 'none';
    stockState.el.ai.value = String(savedAiCount);
    stockSetupMsg(on ? 'AI 对手已开启' : 'AI 对手已关闭，本局你将独自模拟');
  }));
  stockState.el.ai.onchange = () => {
    const n = Math.max(1, Math.min(3, Number(stockState.el.ai.value) || 3));
    savedAiCount = n;
    if (typeof state !== 'undefined' && state.config) state.config.hall_stock_ai_count = n;
    if (typeof api === 'function') {
      api('/api/config', { hall_stock_ai_count: n }).catch(() => {});
    }
  };

  root.querySelectorAll('.stock-market-row .stock-mode').forEach((btn) => {
    btn.onclick = () => {
      const m = btn.dataset.market === 'crypto' ? 'crypto' : 'stock';
      if (stockState.market === m) return;
      stockState.market = m;
      stockState.selected = (m === 'crypto'
        ? CRYPTO_DEFAULT_SYMBOLS : STOCK_DEFAULT_SYMBOLS).slice();
      stockRenderSetup(stockState.games);
    };
  });
  root.querySelectorAll('.stock-mode[data-mode]').forEach((btn) => {
    btn.onclick = () => {
      stockState.mode = btn.dataset.mode === 'live' ? 'live' : 'replay';
      stockRenderSetup(stockState.games);
    };
  });
  if (stockState.el.daysWrap) {
    stockState.el.daysWrap.classList.toggle('hidden', stockState.mode === 'live');
  }

  if (stockState.el.q && stockState.el.results) {
    stockState.el.q.oninput = () => {
      clearTimeout(stockState.searchTimer);
      const q = stockState.el.q.value.trim();
      if (!q) { stockState.el.results.classList.add('hidden'); return; }
      stockState.searchTimer = setTimeout(() => stockSearch(q), 260);
    };
    stockState.el.q.onkeydown = (e) => {
      if (e.key === 'Enter' && !isImeComposing(e)) stockSearch(stockState.el.q.value.trim());
    };
    stockState.el.results.querySelectorAll('[data-add]').forEach((btn) => {
      btn.onclick = () => stockAddSymbol(btn.dataset.add);
    });
  }
  root.querySelectorAll('.stock-chip-x').forEach((btn) => {
    btn.onclick = () => {
      const sym = btn.parentNode.dataset.sym;
      stockState.selected = stockState.selected.filter((x) => x.symbol !== sym);
      stockRenderSetup(stockState.games);
    };
  });
  stockState.el.create.onclick = () => stockCreateGame(games);
  root.querySelectorAll('[data-open]').forEach((btn) => {
    btn.onclick = () => stockLoadGame(btn.dataset.open);
  });
  root.querySelectorAll('[data-del]').forEach((btn) => {
    btn.onclick = async () => {
      if (!confirm('删除这个模拟盘？')) return;
      try {
        await api('/api/hall/stock/delete', { game_id: btn.dataset.del });
        stockLoadSetup(stockState.gen);
      } catch (e) { stockSetupMsg(e.message || e, true); }
    };
  });
  stockScrollTop();
}

function stockSetupMsg(text, err) {
  const box = stockState.el.setupMsg;
  if (box) {
    box.textContent = text || '';
    box.classList.toggle('err', !!err);
    box.classList.toggle('hidden', !text);
  }
  if (text) toast(text, err ? 'error' : 'success');
}

async function stockSearch(q) {
  if (!q) return;
  const gen = stockState.gen;
  try {
    const data = await api('/api/hall/stock/search', { q: q });
    if (gen !== stockState.gen) return;
    if (!stockState.el.q || stockState.el.q.value.trim() !== q) return;
    stockState.results = data.items || [];
    const box = stockState.el.results;
    if (!box) return;
    if (!stockState.results.length) {
      box.innerHTML = '<div class="stock-result-empty">没有匹配的 A 股 / ETF</div>';
    } else {
      box.innerHTML = stockState.results.map((it) => (
        '<button class="stock-result" data-add="' + stockEsc(it.symbol) + '">'
        + '<span>' + stockEsc(it.name) + '</span>'
        + '<b>' + stockEsc(it.symbol) + '</b>'
        + '<em>' + stockEsc(it.type) + '</em></button>'
      )).join('');
      box.querySelectorAll('[data-add]').forEach((btn) => {
        btn.onclick = () => stockAddSymbol(btn.dataset.add);
      });
    }
    box.classList.remove('hidden');
  } catch (e) {
    if (gen === stockState.gen) stockSetupMsg(e.message || e, true);
  }
}

function stockAddSymbol(symbol) {
  if (stockState.selected.some((x) => x.symbol === symbol)) {
    stockSetupMsg('已经在观察池里了');
    return;
  }
  if (stockState.selected.length >= 12) {
    stockSetupMsg('最多选择 12 只标的');
    return;
  }
  const hit = stockState.results.find((x) => x.symbol === symbol);
  stockState.selected.push({
    symbol: symbol,
    name: (hit && hit.name) || symbol,
    type: (hit && hit.type) || '股票',
  });
  stockRenderSetup(stockState.games);
  if (stockState.el.results) stockState.el.results.classList.add('hidden');
}

async function stockCreateGame(games) {
  if (stockState.busy) return;
  if (stockState.selected.length < 2) {
    stockSetupMsg('至少选择 2 个标的', true);
    return;
  }
  stockState.busy = true;
  const btn = stockState.el.create;
  if (btn) { btn.disabled = true; btn.textContent = '正在拉取行情…'; }
  stockSetupMsg('正在抓取历史行情并冻结快照，首次可能需要几秒…');
  try {
    const data = await api('/api/hall/stock/create', {
      mode: stockState.mode,
      market: stockState.market,
      symbols: stockState.selected.map((x) => x.symbol),
      days: Number(stockState.el.days.value) || 20,
      ai_count: hallAiEnabled('stock')
        ? (Number(stockState.el.ai.value) || 3) : 0,
      initial_cash: Number(stockState.el.cash.value) || 1000000,
    });
    stockState.game = data.game;
    stockState.symbol = (data.game.symbols[0] || {}).symbol || '';
    stockState.barsCache = {};
    stockState.barsFor = '';
    stockState.aiBusy = {};
    stockState.aiErrors = {};
    stockState.aiRunFor = '';
    stockState.autoRun = true;
    stockRenderGame();
    stockScrollTop();
    stockEnsureAI();
  } catch (e) {
    stockSetupMsg(e.message || e, true);
  } finally {
    stockState.busy = false;
    if (btn) { btn.disabled = false; btn.textContent = '开始模拟'; }
  }
}

async function stockLoadGame(gameId) {
  const gen = stockState.gen;
  try {
    const data = await api('/api/hall/stock/state', { game_id: gameId });
    if (gen !== stockState.gen) return;
    stockState.game = data.game;
    stockState.market = data.game.market === 'crypto' ? 'crypto' : 'stock';
    stockState.symbol = (data.game.symbols[0] || {}).symbol || '';
    stockState.barsCache = {};
    stockState.barsFor = '';
    stockState.aiBusy = {};
    stockState.aiErrors = {};
    stockState.aiRunFor = '';
    stockState.autoRun = false;
    stockRenderGame();
    stockScrollTop();
  } catch (e) {
    toast(e.message || e, 'error');
  }
}

/* ---------------- 对局界面 ---------------- */

/* ---------------- 对局界面 ----------------
   渲染分三步：外壳（结构，只在进局/换局时重建）→ 全量（applyFull）→ 增量（applyPatch）。
   原来的做法是每次下单 / 撤单 / 推进 / AI 决策都重拉整份 state 再整页 innerHTML 重绘，
   操作反馈被全量渲染拖慢；现在只回变化的切片并只更新对应节点。 */

function stockRenderGame() {
  const g = stockState.game;
  if (!g) { stockLoadSetup(stockState.gen); return; }
  const root = stockState.el.root;
  const prevQty = (stockState.el.qty && stockState.el.qty.value) || '100';

  if (!stockState.symbol) stockState.symbol = (g.symbols[0] || {}).symbol || '';

  root.innerHTML = [
    '<div class="stock-root">',
    '  <div class="stock-main">',
    '    <div class="stock-toolbar">',
    '      <div class="stock-tb-group">',
    '        <span class="stock-pill accent" id="stock-pill-cursor"></span>',
    '        <span class="stock-pill" id="stock-pill-mode"></span>',
    '        <span class="stock-pill" id="stock-pill-remain"></span>',
    '        <span class="stock-pill dim" id="stock-pill-next"></span>',
    '      </div>',
    '      <div class="stock-tb-group">',
    '        <span class="stock-pill" id="stock-pill-bench"></span>',
    '        <span class="stock-pill regime" id="stock-pill-regime"></span>',
    '      </div>',
    '      <span class="stock-spacer"></span>',
    '      <div class="stock-tb-actions">',
    '        <button class="stock-btn primary" id="stock-advance">下一日</button>',
    '        <button class="stock-btn" id="stock-auto">自动播放</button>',
    '        <button class="stock-btn" id="stock-ai-all"'
      + ' title="让所有还没决策的 AI 一次做完">AI 一键决策</button>',
    '        <span class="stock-menu-wrap">',
    '          <button class="stock-btn" id="stock-history-menu" aria-haspopup="menu"'
      + ' aria-expanded="false">更多 ▾</button>',
    '          <div class="stock-menu hidden" id="stock-history-dropdown" role="menu">',
    '            <button type="button" data-hm="back" role="menuitem">'
      + '<b>时间轴回溯</b><span>还原到某一天，只看不改</span></button>',
    '            <button type="button" data-hm="search" role="menuitem">'
      + '<b>分析检索</b><span>按时间 / 类型 / 标的 / 关键词查找</span></button>',
    '            <button type="button" data-hm="compare" role="menuitem">'
      + '<b>历史对比</b><span>最多三个时间点并列查看</span></button>',
    '            <button type="button" data-hm="report" role="menuitem">'
      + '<b>导出报告</b><span>下载本局 Markdown 复盘报告</span></button>',
    '          </div>',
    '        </span>',
    '        <button class="stock-btn ghost" id="stock-new">新开一局</button>',
    '      </div>',
    '    </div>',
    '    <div class="stock-msg hidden" id="stock-msg"></div>',
    '    <div class="stock-chart-card">',
    '      <div class="stock-chart-head">',
    '        <span class="stock-chart-title">K 线</span>',
    '        <span class="stock-chart-legend" id="stock-legend"></span>',
    '        <span class="stock-spacer"></span>',
    '        <select class="stock-mark-filter" id="stock-mark-filter"'
      + ' title="成交标注按谁筛选">',
    '          <option value="all">成交：全部</option>',
    '          <option value="me">成交：只看我</option>',
    '          <option value="ai">成交：只看 AI</option>',
    '        </select>',
    '        <button class="stock-btn" id="stock-zoom-open"'
      + ' title="全屏放大查看 K 线（滚轮缩放 · 按住拖动平移）">放大</button>',
    '      </div>',
    '      <div class="stock-symbol-bar" id="stock-symbols"></div>',
    '      <div class="stock-chart-wrap" id="stock-chart-wrap">',
    '        <svg id="stock-chart" class="stock-chart" viewBox="0 0 720 360"></svg>',
    '        <div class="stock-tip hidden" id="stock-tip"></div>',
    '      </div>',
    '    </div>',
    '    <div class="stock-order-card">',
    '      <div class="stock-order-head"><b>下单</b><span id="stock-account"></span></div>',
    '      <div class="stock-order-row">',
    '        <div class="stock-buy-sell">',
    '          <button data-side="buy" class="active">买入</button>',
    '          <button data-side="sell">卖出</button>',
    '        </div>',
    '        <input id="stock-qty" type="number" min="' + (stockIsCrypto() ? '0.000001' : '100')
      + '" step="' + (stockIsCrypto() ? '0.001' : '100') + '" value="' + stockEsc(prevQty) + '">',
    '        <div class="stock-quick" id="stock-quick">',
    '          <button type="button" data-frac="0.25" title="按可用资金 / 可卖数量的四分之一">1/4</button>',
    '          <button type="button" data-frac="0.5" title="按可用资金 / 可卖数量的二分之一">1/2</button>',
    '          <button type="button" data-frac="1" title="尽量用满可用资金 / 全部可卖">全部</button>',
    '        </div>',
    '        <label class="stock-limit-toggle" title="不填价格即市价单，下一交易日开盘撮合">',
    '          <input type="checkbox" id="stock-limit-on"> 限价',
    '        </label>',
    '        <input id="stock-limit" class="stock-limit-input hidden" type="number"'
      + ' min="0" step="' + (stockIsCrypto() ? '0.0001' : '0.01') + '"'
      + ' placeholder="限价，留空=市价">',
    '        <span class="stock-est" id="stock-est"></span>',
    '        <button class="stock-btn primary" id="stock-submit">提交委托</button>',
    '      </div>',
    '      <div class="stock-pending" id="stock-pending"></div>',
    '    </div>',
    '  </div>',
    '  <aside class="stock-side">',
    '    <div class="stock-tabs" role="tablist">',
    '      <button type="button" class="stock-tab active" role="tab"'
      + ' aria-selected="true" data-tab="pos">持仓成交</button>',
    '      <button type="button" class="stock-tab" role="tab"'
      + ' aria-selected="false" data-tab="rival">对手</button>',
    '      <button type="button" class="stock-tab" role="tab"'
      + ' aria-selected="false" data-tab="train">训练</button>',
    '    </div>',
    '    <div class="stock-tabpanel" data-panel="pos" role="tabpanel">',
    '      <div class="stock-card">',
    '        <div class="stock-card-title">我的持仓'
      + ' <span class="stock-ov-dim">按当前标的展开</span></div>',
    '        <div id="stock-holdings"></div>',
    '      </div>',
    '      <div class="stock-card">',
    '        <div class="stock-card-title">我的成交</div>',
    '        <div id="stock-trades"></div>',
    '      </div>',
    '    </div>',
    '    <div class="stock-tabpanel hidden" data-panel="rival" role="tabpanel">',
    '      <div class="stock-card">',
    '        <div class="stock-card-title">参考散户'
      + ' <span class="stock-ov-dim">风格对照 · 按总资产</span></div>',
    '        <div id="stock-players"></div>',
    '        <div class="stock-nav-wrap" id="stock-nav-wrap">',
    '          <svg class="stock-nav-svg" id="stock-nav"></svg>',
    '        </div>',
    '        <div class="stock-nav-legend" id="stock-nav-legend"></div>',
    '      </div>',
    '      <div class="stock-card">',
    '        <div class="stock-card-title">AI 复盘</div>',
    '        <div id="stock-journals"></div>',
    '      </div>',
    '    </div>',
    '    <div class="stock-tabpanel hidden" data-panel="train" role="tabpanel">',
    '      <div class="stock-card">',
    '        <div class="stock-card-title">组合风险'
      + ' <span class="stock-ov-dim">最大回撤 · 胜率 · 盈亏比</span></div>',
    '        <div id="stock-risk"></div>',
    '      </div>',
    '      <div class="stock-card">',
    '        <div class="stock-card-title">决策计分板'
      + ' <span class="stock-ov-dim">看判断质量，不只看盈亏</span></div>',
    '        <div id="stock-scoreboard"></div>',
    '      </div>',
    '      <div class="stock-predict-card">',
    '        <div class="stock-card-title">预测训练'
      + ' <span class="stock-ov-dim">先下注、后揭晓</span></div>',
    '        <div class="stock-predict-row">',
    '          <select id="stock-p-target" title="预测对象">',
    '            <option value="">当前标的</option>',
    '            <option value="benchmark">' + stockEsc(stockBenchName()) + ' 大盘</option>',
    '          </select>',
    '          <select id="stock-p-horizon" title="几个交易日之后">',
    '            <option value="1">1 日后</option>',
    '            <option value="3" selected>3 日后</option>',
    '            <option value="5">5 日后</option>',
    '          </select>',
    '          <div class="stock-predict-dir" id="stock-p-dir">',
    '            <button type="button" data-pdir="up" class="active">看涨</button>',
    '            <button type="button" data-pdir="flat">看平</button>',
    '            <button type="button" data-pdir="down">看跌</button>',
    '          </div>',
    '          <button class="stock-btn primary" id="stock-p-submit">提交预测</button>',
    '        </div>',
    '        <div class="stock-predict-status" id="stock-p-status"></div>',
    '        <div class="stock-predict-list" id="stock-p-list"></div>',
    '      </div>',
    '    </div>',
    '  </aside>',
    '</div>',
  ].join('');

  Object.assign(stockState.el, {
    symbols: root.querySelector('#stock-symbols'),
    players: root.querySelector('#stock-players'),
    journals: root.querySelector('#stock-journals'),
    trades: root.querySelector('#stock-trades'),
    pending: root.querySelector('#stock-pending'),
    account: root.querySelector('#stock-account'),
    qty: root.querySelector('#stock-qty'),
    est: root.querySelector('#stock-est'),
    submit: root.querySelector('#stock-submit'),
    advance: root.querySelector('#stock-advance'),
    auto: root.querySelector('#stock-auto'),
    aiAll: root.querySelector('#stock-ai-all'),
    newBtn: root.querySelector('#stock-new'),
    historyMenu: root.querySelector('#stock-history-menu'),
    historyDropdown: root.querySelector('#stock-history-dropdown'),
    legend: root.querySelector('#stock-legend'),
    markFilter: root.querySelector('#stock-mark-filter'),
    pillCursor: root.querySelector('#stock-pill-cursor'),
    pillMode: root.querySelector('#stock-pill-mode'),
    pillRemain: root.querySelector('#stock-pill-remain'),
    pillBench: root.querySelector('#stock-pill-bench'),
    pillRegime: root.querySelector('#stock-pill-regime'),
    pTarget: root.querySelector('#stock-p-target'),
    pHorizon: root.querySelector('#stock-p-horizon'),
    pDir: root.querySelector('#stock-p-dir'),
    pSubmit: root.querySelector('#stock-p-submit'),
    pStatus: root.querySelector('#stock-p-status'),
    pList: root.querySelector('#stock-p-list'),
    nav: root.querySelector('#stock-nav'),
    navWrap: root.querySelector('#stock-nav-wrap'),
    navLegend: root.querySelector('#stock-nav-legend'),
    scoreboard: root.querySelector('#stock-scoreboard'),
    pillNext: root.querySelector('#stock-pill-next'),
    zoomOpen: root.querySelector('#stock-zoom-open'),
    chart: root.querySelector('#stock-chart'),
    chartWrap: root.querySelector('#stock-chart-wrap'),
    tip: root.querySelector('#stock-tip'),
    msg: root.querySelector('#stock-msg'),
    holdings: root.querySelector('#stock-holdings'),
    riskBox: root.querySelector('#stock-risk'),
    quick: root.querySelector('#stock-quick'),
    limitOn: root.querySelector('#stock-limit-on'),
    limitInput: root.querySelector('#stock-limit'),
  });

  root.querySelectorAll('.stock-order-row [data-side]').forEach((btn) => {
    btn.onclick = () => {
      stockState.side = btn.dataset.side === 'sell' ? 'sell' : 'buy';
      btn.parentNode.querySelectorAll('[data-side]').forEach((x) => {
        x.classList.toggle('active', x === btn);
      });
      stockUpdateEstimate();
    };
  });
  stockState.el.submit.onclick = () => stockSubmitOrder();
  stockState.el.qty.oninput = stockUpdateEstimate;
  // 快捷仓位：按可用现金 / 可卖折算成数量，省得手算
  if (stockState.el.quick) {
    stockState.el.quick.addEventListener('click', (e) => {
      const btn = e.target.closest('button[data-frac]');
      if (!btn) return;
      stockFillQuick(Number(btn.dataset.frac));
    });
  }
  // 限价开关：勾上才显示价格输入框，不填价格仍按市价单走
  if (stockState.el.limitOn) {
    stockState.el.limitOn.onchange = () => {
      const on = !!stockState.el.limitOn.checked;
      if (stockState.el.limitInput) {
        stockState.el.limitInput.classList.toggle('hidden', !on);
        if (on && !stockState.el.limitInput.value) {
          const sel = stockSelectedSymbol();
          if (sel && sel.last) stockState.el.limitInput.value = String(sel.last);
        }
        if (!on) stockState.el.limitInput.value = '';
      }
      stockUpdateEstimate();
    };
  }
  if (stockState.el.limitInput) stockState.el.limitInput.oninput = stockUpdateEstimate;
  // 侧栏三组标签：切换只改显示，训练页里净值曲线隐藏时量不到宽度，切回来要重绘
  root.querySelectorAll('.stock-tab').forEach((tab) => {
    tab.onclick = () => {
      const key = tab.dataset.tab;
      root.querySelectorAll('.stock-tab').forEach((x) => {
        const on = x === tab;
        x.classList.toggle('active', on);
        x.setAttribute('aria-selected', on ? 'true' : 'false');
      });
      root.querySelectorAll('.stock-tabpanel').forEach((p) => {
        p.classList.toggle('hidden', p.dataset.panel !== key);
      });
      if (key === 'rival') stockDrawNav();
      if (key === 'train') {
        stockRenderRisk();
        if (!stockState.risk) stockLoadRisk();
      }
    };
  });
  stockState.el.advance.onclick = () => stockNextDay();
  stockState.el.auto.onclick = () => stockToggleAuto();
  if (stockState.el.aiAll) stockState.el.aiAll.onclick = () => stockDecideAll();
  // 「历史分析」下拉：三个入口收在一处，避免工具栏一排同名按钮
  const menuBtn = stockState.el.historyMenu;
  const menu = stockState.el.historyDropdown;
  const closeMenu = () => {
    if (!menu || !menuBtn) return;
    menu.classList.add('hidden');
    menuBtn.setAttribute('aria-expanded', 'false');
  };
  if (menuBtn && menu) {
    menuBtn.onclick = (e) => {
      e.stopPropagation();
      const willOpen = menu.classList.contains('hidden');
      menu.classList.toggle('hidden', !willOpen);
      menuBtn.setAttribute('aria-expanded', willOpen ? 'true' : 'false');
    };
    menu.onclick = (e) => {
      const item = e.target.closest('button[data-hm]');
      if (!item) return;
      closeMenu();
      const k = item.dataset.hm;
      if (k === 'back') stockOpenHistory();
      else if (k === 'search') stockOpenSearch();
      else if (k === 'compare') stockOpenCompare();
      else if (k === 'report') stockExportReport();
    };
    if (stockState.onDocClick) document.removeEventListener('click', stockState.onDocClick);
    stockState.onDocClick = () => closeMenu();
    document.addEventListener('click', stockState.onDocClick);
  }
  stockState.el.newBtn.onclick = () => {
    stockClearTimer();
    stockHideAll();
    stockState.game = null;
    stockLoadSetup(stockState.gen);
  };
  stockState.el.zoomOpen.onclick = () => stockOpenZoom();
  if (stockState.el.markFilter) {
    const mf = stockState.el.markFilter;
    mf.value = stockState.markFilter;
    mf.onchange = () => {
      stockState.markFilter = (mf.value === 'me' || mf.value === 'ai') ? mf.value : 'all';
      stockDrawChart();          // 只重绘 K 线，不重新请求数据
    };
  }
  if (stockState.el.pDir) {
    stockState.el.pDir.addEventListener('click', (e) => {
      const btn = e.target.closest('button[data-pdir]');
      if (!btn) return;
      stockState.predictDir = btn.dataset.pdir;
      stockState.el.pDir.querySelectorAll('button[data-pdir]').forEach((x) => {
        x.classList.toggle('active', x === btn);
      });
    });
  }
  if (stockState.el.pSubmit) stockState.el.pSubmit.onclick = () => stockSubmitPrediction();

  stockApplyFull(g);
  stockLoadBars();
  stockLoadAnalysis();

  if (stockState.autoRun) {
    stockState.autoRun = false;
    stockEnsureAI();
  }
}

/* 工具栏是增量更新的重点：只改文字与禁用态，不重建 DOM */
function stockRenderToolbar() {
  const g = stockState.game;
  if (!g || !stockState.el.pillCursor) return;
  const finished = g.status === 'finished';
  const bm = g.benchmark || {};
  const remain = g.mode === 'live'
    ? '实时每日盘'
    : (finished ? '已结束' : '剩余 ' + g.remaining_days + ' 个交易日');
  stockState.el.pillCursor.textContent = g.cursor_date;
  stockState.el.pillMode.textContent = g.mode === 'live' ? '实时' : '回放';
  stockState.el.pillRemain.textContent = remain;
  stockState.el.pillBench.innerHTML = stockEsc(stockBenchName()) + ' <i class="'
    + (Number(bm.return_pct) >= 0 ? 'up' : 'down') + '">' + stockPct(bm.return_pct) + '</i>';
  stockState.el.pillNext.textContent = g.mode === 'live'
    ? '下一交易日：收盘后更新'
    : (finished ? '回放已结束' : '下一交易日：' + (g.next_date || '—'));
  if (stockState.el.pillRegime) {
    const rg = g.regime || {};
    stockState.el.pillRegime.textContent = rg.label || '—';
    stockState.el.pillRegime.className = 'stock-pill regime ' + (rg.trend || '');
    stockState.el.pillRegime.title = rg.vol20
      ? (stockBenchName() + '：20日波动 ' + rg.vol20 + '% · 量比 ' + rg.volume_ratio)
      : '由' + stockBenchName() + '计算的行情状态';
  }
  if (stockState.el.advance) stockState.el.advance.disabled = finished;
  if (stockState.el.submit) stockState.el.submit.disabled = finished;
  if (stockState.el.auto) {
    stockState.el.auto.textContent = stockState.autoTimer ? '停止自动' : '自动播放';
  }
  // 一键决策：显示还剩几个 AI 没决策；有请求在飞时禁用，避免重复触发
  if (stockState.el.aiAll) {
    const ais = (g.players || []).filter((p) => p.kind === 'ai');
    const undecided = ais.filter((p) => !p.decided).length;
    const busy = Object.keys(stockState.aiBusy || {}).length > 0;
    const btn = stockState.el.aiAll;
    btn.style.display = ais.length ? '' : 'none';
    btn.disabled = finished || !undecided || busy;
    btn.textContent = busy ? 'AI 决策中…'
      : (undecided ? 'AI 一键决策 (' + undecided + ')' : 'AI 已全部决策');
  }
}

/* 全量渲染：进局 / 换局 / 兜底时用 */
function stockApplyFull(game) {
  stockState.game = game;
  stockRenderToolbar();
  stockRenderSymbols();
  stockRenderPlayers();
  stockRenderJournals();
  stockRenderTrades();
  stockRenderHoldings();
  stockRenderPending();
  stockUpdateAccount();
  stockUpdateEstimate();
  stockLoadRisk();
}

/* 把增量切片并回本地 state：只覆盖 patch 里真的带了的字段 */
function stockMergePatch(patch) {
  const g = stockState.game;
  if (!g || !patch) return false;
  if (patch.id && patch.id !== g.id) return false;
  ['cursor_date', 'status', 'next_date', 'remaining_days', 'ai_ready', 'ai_count',
    'benchmark', 'regime'].forEach((k) => {
    if (patch[k] !== undefined) g[k] = patch[k];
  });
  if (patch.players) {
    const byId = {};
    patch.players.forEach((p) => { byId[p.id] = p; });
    const merged = (g.players || []).map((p) => byId[p.id] || p);
    patch.players.forEach((p) => {
      if (!merged.some((x) => x.id === p.id)) merged.push(p);
    });
    g.players = merged;
    g.leaderboard = patch.leaderboard
      || merged.slice().sort((a, b) => b.equity - a.equity);
  }
  ['symbols', 'pending_orders', 'trades'].forEach((k) => {
    if (patch[k] !== undefined) g[k] = patch[k];
  });
  if (patch.journals) {
    g.journals = g.journals || {};
    Object.keys(patch.journals).forEach((k) => { g.journals[k] = patch.journals[k]; });
  }
  return true;
}

/* 增量渲染：只更新发生变化的区块，整页不再重建 */
function stockApplyPatch(patch) {
  if (!stockState.game) { stockReloadState(); return; }
  const cursorBefore = stockState.game.cursor_date;
  if (!stockMergePatch(patch)) { stockReloadState(); return; }
  stockRenderToolbar();
  stockRenderPlayers();
  stockRenderSymbols();
  stockRenderHoldings();
  if (patch.pending_orders !== undefined) stockRenderPending();
  if (patch.trades !== undefined) stockRenderTrades();
  if (patch.journals) stockRenderJournals();
  stockUpdateAccount();
  if (stockState.game.cursor_date !== cursorBefore) {
    stockLoadBars();
    stockLoadAnalysis();      // 新的一天：预测可能刚结算，参考散户曲线也变了
    stockLoadRisk();          // 资产曲线加了一日，回撤 / 波动要跟着重算
  }
  if (stockState.zoom && stockState.zoom.open) stockZoomDraw();
}

async function stockReloadState() {
  const g = stockState.game;
  if (!g) return;
  const gen = stockState.gen;
  try {
    const data = await api('/api/hall/stock/state', { game_id: g.id });
    if (gen !== stockState.gen || !stockState.game || stockState.game.id !== g.id) return;
    stockApplyFull(data.game);
  } catch (e) { /* 兜底失败就保持旧界面，下个操作还会再试 */ }
}

function stockRenderSymbols() {
  const g = stockState.game;
  const box = stockState.el.symbols;
  if (!box) return;
  const human = (g.players || []).find((p) => p.kind === 'human');
  const held = {};
  ((human && human.positions) || []).forEach((p) => { held[p.symbol] = p.qty; });
  box.innerHTML = (g.symbols || []).map((s) => {
    const cls = 'stock-sym' + (s.symbol === stockState.symbol ? ' active' : '');
    const up = Number(s.change_pct) >= 0;
    return '<button class="' + cls + '" data-sym="' + stockEsc(s.symbol) + '">'
      + '<span class="stock-sym-name">' + stockEsc(s.name)
      + (held[s.symbol] ? ' <em>持仓</em>' : '') + '</span>'
      + '<span class="stock-sym-code">' + stockEsc(s.symbol) + '</span>'
      + '<span class="stock-sym-price ' + (up ? 'up' : 'down') + '"'
      + ' title="当日涨跌幅（相对前一交易日收盘）">'
      + stockMoney(s.last) + ' <i>' + stockPct(s.change_pct) + '</i></span>'
      + '</button>';
  }).join('')
    + (stockIsCrypto() ? '' : (
      '<button class="stock-sym stock-sym-add" id="stock-sym-add"'
      + ' title="中途加入一只股票 / ETF">＋ 加入标的</button>'));
  box.querySelectorAll('[data-sym]').forEach((btn) => {
    btn.onclick = () => {
      stockState.symbol = btn.dataset.sym;
      stockRenderSymbols();
      stockLoadBars();
      stockUpdateEstimate();
    };
  });
  const addBtn = box.querySelector('#stock-sym-add');
  if (addBtn) addBtn.onclick = () => stockOpenAddSymbol();
}

/* ---------------- 中途加入标的 ---------------- */

function stockOpenAddSymbol() {
  if (!stockState.game) return;
  if (stockState.addPop && stockState.addPop.open) {
    stockState.addPop.q.focus();
    return;
  }
  const wrap = document.createElement('div');
  wrap.className = 'stock-add-mask';
  wrap.innerHTML = [
    '<div class="stock-add-pop">',
    '  <div class="stock-add-head"><b>加入观察池</b>',
    '    <span>加入后 AI、K 线与下单立即可用</span>',
    '    <button type="button" class="stock-add-x" title="关闭（Esc）">✕</button></div>',
    '  <input id="stock-add-q" type="text" autocomplete="off"',
    '    placeholder="搜索股票 / ETF：茅台、510300、宁德时代…">',
    '  <div class="stock-add-list" id="stock-add-list"></div>',
    '  <div class="stock-add-msg" id="stock-add-msg"></div>',
    '</div>',
  ].join('');
  document.body.appendChild(wrap);

  const pop = stockState.addPop = {
    wrap: wrap,
    q: wrap.querySelector('#stock-add-q'),
    list: wrap.querySelector('#stock-add-list'),
    msg: wrap.querySelector('#stock-add-msg'),
    open: true, seq: 0, timer: null, onKey: null,
  };
  const close = () => stockCloseAddSymbol();
  wrap.addEventListener('click', (e) => { if (e.target === wrap) close(); });
  wrap.querySelector('.stock-add-x').onclick = close;
  pop.onKey = (e) => {
    if (e.key === 'Escape') { e.stopPropagation(); close(); }
  };
  window.addEventListener('keydown', pop.onKey, true);
  pop.q.oninput = () => {
    clearTimeout(pop.timer);
    const q = pop.q.value.trim();
    if (!q) { pop.list.innerHTML = ''; pop.msg.textContent = ''; return; }
    pop.timer = setTimeout(() => stockAddSearch(pop, q), 260);
  };
  pop.q.onkeydown = (e) => {
    if (e.key === 'Enter' && !isImeComposing(e)) {
      clearTimeout(pop.timer);
      stockAddSearch(pop, pop.q.value.trim());
    }
  };
  pop.q.focus();
}

async function stockAddSearch(pop, q) {
  if (!pop || !pop.open || !q) return;
  const seq = ++pop.seq;
  pop.msg.textContent = '搜索中…';
  try {
    const data = await api('/api/hall/stock/search', { q: q });
    if (seq !== pop.seq || !pop.open) return;
    const items = data.items || [];
    const have = new Set(((stockState.game && stockState.game.symbols) || [])
      .map((s) => s.symbol));
    pop.msg.textContent = '';
    pop.list.innerHTML = items.length ? items.map((it) => (
      '<button class="stock-add-item" data-add="' + stockEsc(it.symbol) + '"'
      + (have.has(it.symbol) ? ' disabled' : '') + '>'
      + '<span>' + stockEsc(it.name) + '</span>'
      + '<b>' + stockEsc(it.symbol) + '</b>'
      + '<em>' + (have.has(it.symbol) ? '已在池中' : '＋ 加入') + '</em></button>'
    )).join('') : '<div class="stock-empty small">没有匹配的 A 股 / ETF</div>';
    pop.list.querySelectorAll('[data-add]').forEach((btn) => {
      btn.onclick = () => stockAddSymbolToGame(pop, btn.dataset.add);
    });
  } catch (e) {
    if (seq !== pop.seq || !pop.open) return;
    pop.msg.textContent = e.message || e;
  }
}

async function stockAddSymbolToGame(pop, symbol) {
  if (!pop || !stockState.game) return;
  pop.msg.textContent = '正在拉取行情并加入…';
  pop.list.querySelectorAll('button').forEach((b) => { b.disabled = true; });
  try {
    const data = await api('/api/hall/stock/add_symbol', {
      game_id: stockState.game.id, symbol: symbol,
    });
    if (!data.game) throw new Error('加入失败');
    stockState.game = data.game;
    stockState.symbol = data.symbol || symbol;
    stockState.barsCache = {};
    stockState.marksCache = {};
    stockState.barsCursor = '';
    stockApplyFull(stockState.game);
    stockLoadBars();
    stockLoadAnalysis();
    stockMsg('已加入 ' + (data.name || symbol) + '，现在可以看 K 线与下单');
    stockCloseAddSymbol();
  } catch (e) {
    pop.msg.textContent = e.message || e;
    pop.list.querySelectorAll('button').forEach((b) => { b.disabled = false; });
  }
}

function stockCloseAddSymbol() {
  const pop = stockState.addPop;
  if (!pop) return;
  pop.open = false;
  pop.seq += 1;
  clearTimeout(pop.timer);
  if (pop.onKey) window.removeEventListener('keydown', pop.onKey, true);
  if (pop.wrap.parentNode) pop.wrap.parentNode.removeChild(pop.wrap);
  stockState.addPop = null;
}

function stockRenderPlayers() {
  const g = stockState.game;
  const box = stockState.el.players;
  if (!box) return;
  const ref = stockState.analysis && stockState.analysis.reference;
  const refMap = {};
  ((ref && ref.players) || []).forEach((p) => { refMap[p.id] = p; });
  const order = (g.leaderboard || []).slice();
  box.innerHTML = order.map((p, i) => {
    const me = p.kind === 'human';
    const r = refMap[p.id];
    let status = '';
    if (!me) {
      if (stockState.aiBusy[p.id]) status = '<span class="stock-ai-state busy">决策中…</span>';
      else if (p.decided) status = '<span class="stock-ai-state ok">已决策</span>';
      else status = '<button class="stock-ai-btn" data-ai="' + stockEsc(p.id) + '">让 AI 决策</button>';
    }
    // 持仓优先用 game_state 里的（下单后立刻是最新的），名称兜底查观察池
    const allPos = (p.positions && p.positions.length)
      ? p.positions : ((r && r.positions) || []);
    const pos = allPos.slice(0, 3).map((x) => (
      '<span class="stock-pos" title="' + stockEsc(x.symbol) + '">'
      + stockEsc(x.name || stockSymbolName(x.symbol)) + ' ' + stockQtyText(x.qty)
      + stockUnit() + '</span>'
    )).join('') + (allPos.length > 3
      ? '<span class="stock-pos dim">+' + (allPos.length - 3) + '</span>' : '')
      || '<span class="stock-pos dim">空仓</span>';
    // 参考散户的行为特征：仓位 / 换手 / 集中度 / 回撤
    // 仓位与集中度按当前持仓现算，避免刚下单时显示上一交易日的旧值
    const mvNow = allPos.reduce((sum, x) => sum + (Number(x.market_value) || 0), 0);
    const top3Now = allPos.slice().sort(
      (a, b) => (Number(b.market_value) || 0) - (Number(a.market_value) || 0))
      .slice(0, 3).reduce((sum, x) => sum + (Number(x.market_value) || 0), 0);
    const posPct = p.equity > 0 ? (mvNow / p.equity * 100) : 0;
    const conc = mvNow > 0 ? (top3Now / mvNow * 100) : 0;
    const styleTag = r ? '<span class="stock-ref-style">' + stockEsc(r.style) + '</span>' : '';
    const metrics = r
      ? '<div class="stock-ref-metrics">仓位 ' + posPct.toFixed(1) + '% · 换手 '
        + r.turnover + '% · 集中 ' + conc.toFixed(1) + '% · 回撤 ' + r.max_drawdown + '%</div>'
      : '';
    const recent = r
      ? '<span class="stock-ref-recent ' + (r.recent5 >= 0 ? 'up' : 'down') + '">近5日 '
        + stockPct(r.recent5) + '</span>' : '';
    const err = stockState.aiErrors[p.id]
      ? '<div class="stock-ai-err">' + stockEsc(stockState.aiErrors[p.id]) + '</div>' : '';
    return '<div class="stock-player' + (me ? ' me' : '') + '">'
      + '<div class="stock-player-top"><span class="stock-rank">' + (i + 1) + '</span>'
      + '<b>' + stockEsc(p.name) + '</b>' + styleTag + status + '</div>'
      + '<div class="stock-player-num">'
      + '<span class="stock-player-label">总收益率</span>'
      + '<span title="相对初始资金的总收益率（含浮盈浮亏）" class="'
      + (p.return_pct >= 0 ? 'up' : 'down') + '">' + stockPct(p.return_pct) + '</span>'
      + recent
      + '<em>总资产 ' + stockMoney(p.equity) + '</em></div>'
      + metrics
      + '<div class="stock-player-pos">' + pos + '</div>' + err + '</div>';
  }).join('');
  box.querySelectorAll('[data-ai]').forEach((btn) => {
    btn.onclick = () => stockDecideAI(btn.dataset.ai);
  });
  stockDrawNav();
}

/* 四条净值曲线：你 + 三个参考散户，归一化到开局 1.0 */
function stockDrawNav() {
  const svg = stockState.el.nav;
  const wrap = stockState.el.navWrap;
  const legend = stockState.el.navLegend;
  if (!svg || !wrap) return;
  const ref = stockState.analysis && stockState.analysis.reference;
  svg.innerHTML = '';
  if (legend) legend.innerHTML = '';
  if (!ref || !ref.dates || ref.dates.length < 2) {
    if (legend) legend.innerHTML = '<span class="stock-ov-dim">净值曲线：推进几天后出现</span>';
    return;
  }
  const w = Math.max(180, Math.round(wrap.clientWidth) || 0);
  const hgt = 92;
  svg.setAttribute('viewBox', '0 0 ' + w + ' ' + hgt);
  const left = 2, right = 2, top = 8, bottom = hgt - 6;
  let lo = Infinity, hi = -Infinity;
  (ref.players || []).forEach((p) => {
    (p.nav || []).forEach((v) => { lo = Math.min(lo, v); hi = Math.max(hi, v); });
  });
  if (!Number.isFinite(lo) || !Number.isFinite(hi) || hi - lo < 1e-6) {
    lo -= 0.01; hi += 0.01;
  }
  const pad = (hi - lo) * 0.15;
  lo -= pad; hi += pad;
  const y = (v) => top + (hi - v) / (hi - lo) * (bottom - top);
  if (lo <= 1 && hi >= 1) {
    svg.appendChild(stockSvg('line', {
      x1: left, y1: y(1), x2: w - right, y2: y(1),
      stroke: '#7d838d', 'stroke-width': 0.5, opacity: 0.32, 'stroke-dasharray': '3 3',
    }));
  }
  const colors = { you: '#4d6bfe', ai_value: '#e0a33c',
    ai_trend: '#e5534b', ai_contrarian: '#2f9e6f' };
  (ref.players || []).forEach((p) => {
    const nav = p.nav || [];
    if (nav.length < 2) return;
    let d = '';
    nav.forEach((v, i) => {
      const x = left + (i / (nav.length - 1)) * (w - left - right);
      d += (i ? ' L' : 'M') + x.toFixed(1) + ' ' + y(v).toFixed(1);
    });
    svg.appendChild(stockSvg('path', {
      d: d, fill: 'none', stroke: colors[p.id] || '#8a8f98',
      'stroke-width': 1.5, opacity: 0.95, 'stroke-linejoin': 'round',
    }));
    if (legend) {
      legend.innerHTML += '<span class="stock-nav-item"><i style="background:'
        + (colors[p.id] || '#8a8f98') + '"></i>' + stockEsc(p.style)
        + (p.kind === 'human' ? '' : ' ' + stockEsc(p.name.replace(/^(价值派|趋势派|逆势派)/, '')))
        + '</span>';
    }
  });
}

/* 决策计分板：买入 5 日胜率 / 平均超额 / 卖出避跌 */
function stockRenderScoreboard() {
  const box = stockState.el.scoreboard;
  if (!box) return;
  const sc = stockState.analysis && stockState.analysis.scoreboard;
  const s = (sc && sc.stats) || null;
  if (!s || (!s.buy_count && !s.sell_count)) {
    box.innerHTML = '<div class="stock-empty small">还没有可评估的成交'
      + '（成交后需再走满 5 个交易日）</div>';
    return;
  }
  const row = (label, value, cls) => '<div class="stock-score-row"><span>' + label
    + '</span><b class="' + (cls || '') + '">' + value + '</b></div>';
  box.innerHTML = [
    row('买入后 5 日胜率', s.buy_count
      ? (s.buy_win5 + '/' + s.buy_count + '（' + s.buy_win5_pct + '%）') : '—'),
    row('买入平均 5 日收益', s.buy_count ? stockPct(s.buy_avg_fwd5) : '—',
      s.buy_avg_fwd5 >= 0 ? 'up' : 'down'),
    row('平均超额（vs ' + stockBenchName() + '）', s.buy_count ? stockPct(s.buy_avg_excess5) : '—',
      s.buy_avg_excess5 >= 0 ? 'up' : 'down'),
    row('卖出后 5 日避跌', s.sell_count
      ? (s.sell_avoid5 + '/' + s.sell_count + '（' + s.sell_avoid5_pct + '%）') : '—'),
  ].join('');
}

function stockRenderJournals() {
  const g = stockState.game;
  const box = stockState.el.journals;
  if (!box) return;
  const ais = (g.players || []).filter((p) => p.kind === 'ai');
  if (!ais.length) { box.innerHTML = '<div class="stock-empty">本局没有 AI 对手</div>'; return; }
  box.innerHTML = ais.map((p) => {
    const rows = (g.journals && g.journals[p.id]) || [];
    const text = rows.length
      ? rows.slice(0, 2).map((r) => '<div class="stock-journal"><i>' + stockEsc(r.date)
        + '</i>' + stockEsc(r.text) + '</div>').join('')
      : '<div class="stock-empty small">还没有复盘</div>';
    return '<div class="stock-journal-block"><b>' + stockEsc(p.name) + '</b>' + text + '</div>';
  }).join('');
}

function stockRenderTrades() {
  const box = stockState.el.trades;
  if (!box) return;
  const rows = stockState.game.trades || [];
  if (!rows.length) { box.innerHTML = '<div class="stock-empty small">还没有成交</div>'; return; }
  const sc = stockState.analysis && stockState.analysis.scoreboard;
  const fwdMap = {};
  ((sc && sc.trades) || []).forEach((t) => { fwdMap[t.id] = t; });
  box.innerHTML = rows.slice(0, 10).map((t) => {
    const f = fwdMap[t.id];
    // 5 日收益衡量的是「这笔判断对不对」，与账户当前盈亏无关
    const fwd = (f && f.fwd5 !== null && f.fwd5 !== undefined)
      ? '<i class="stock-trade-fwd ' + (f.fwd5 >= 0 ? 'up' : 'down') + '"'
        + ' title="成交后第 5 个交易日的涨跌幅">5日' + stockPct(f.fwd5) + '</i>'
      : '';
    return '<div class="stock-trade"><span class="' + (t.side === 'buy' ? 'up' : 'down') + '">'
      + (t.side === 'buy' ? '买' : '卖') + '</span>'
      + '<b>' + stockEsc(t.name || t.symbol) + '</b>'
      + '<em>' + stockQtyText(t.qty) + stockUnit() + ' @ ' + stockMoney(t.price) + '</em>'
      + fwd
      + '<i>' + stockEsc(t.date) + '</i></div>';
  }).join('');
}

/* 我的持仓：全部持仓一张紧凑表，点一行切换到该标的 */
function stockRenderHoldings() {
  const box = stockState.el.holdings;
  const g = stockState.game;
  if (!box || !g) return;
  const human = (g.players || []).find((p) => p.kind === 'human');
  const rows = (human && human.positions) || [];
  if (!rows.length) {
    box.innerHTML = '<div class="stock-empty small">空仓（下单后下一交易日成交）</div>';
    return;
  }
  box.innerHTML = rows.map((p) => {
    const pnl = Number(p.pnl_pct) || 0;
    return '<div class="stock-hold" data-hold="' + stockEsc(p.symbol) + '"'
      + ' title="点击切换到 ' + stockEsc(p.name || p.symbol) + '">'
      + '<div class="stock-hold-top"><b>'
      + stockEsc(p.name || stockSymbolName(p.symbol)) + '</b>'
      + '<span class="' + (pnl >= 0 ? 'up' : 'down') + '">' + stockPct(pnl) + '</span></div>'
      + '<div class="stock-hold-num"><span>' + stockQtyText(p.qty) + stockUnit()
      + '<em>可卖 ' + stockQtyText(p.available_qty) + '</em></span>'
      + '<span>成本 ' + stockMoney(p.avg_cost) + '<em>现价 ' + stockMoney(p.last) + '</em></span>'
      + '<span>市值 ' + stockMoney(p.market_value) + '</span></div></div>';
  }).join('');
  box.querySelectorAll('[data-hold]').forEach((el) => {
    el.onclick = () => {
      stockState.symbol = el.dataset.hold;
      stockRenderSymbols();
      stockLoadBars();
      stockUpdateAccount();
    };
  });
}

/* 组合风险：口径全部来自后端 stockkit.risk，这里只负责显示，不自己算 */
function stockRenderRisk() {
  const box = stockState.el.riskBox;
  if (!box) return;
  const r = stockState.risk;
  if (!r) { box.innerHTML = '<div class="stock-empty small">风险指标加载中…</div>'; return; }
  const num = (v) => Number(v) || 0;
  const rows = [
    ['最大回撤', stockPct(-num(r.max_drawdown)), 'down'],
    ['年化波动', num(r.annualized_volatility).toFixed(2) + '%', ''],
    ['夏普比率', num(r.sharpe).toFixed(2), ''],
    ['胜率', num(r.win_rate).toFixed(2) + '%', ''],
    ['盈亏比', num(r.profit_factor).toFixed(2), ''],
    ['超额收益', stockPct(num(r.excess_return)), num(r.excess_return) >= 0 ? 'up' : 'down'],
  ];
  box.innerHTML = rows.map((x) => '<div class="stock-risk-row"><span>' + x[0]
    + '</span><b class="' + x[2] + '">' + x[1] + '</b></div>').join('')
    + '<div class="stock-risk-note">平仓 ' + num(r.closed_trades).toFixed(0) + ' 笔 · 样本 '
    + num(r.days).toFixed(0) + ' 个交易日</div>';
}

async function stockLoadRisk() {
  const g = stockState.game;
  if (!g) return;
  const gen = stockState.gen;
  const gid = g.id;
  try {
    const data = await api('/api/hall/stock/risk', { game_id: gid });
    if (gen !== stockState.gen || !stockState.game || stockState.game.id !== gid) return;
    stockState.risk = data.risk || data;
  } catch (e) {
    stockState.risk = null;
  }
  stockRenderRisk();
}

/* 快捷仓位：按可用资金 / 可卖数量折算，A 股向下取整到 100 股 */
function stockFillQuick(frac) {
  const g = stockState.game;
  const sel = stockSelectedSymbol();
  if (!g || !sel || !frac) return;
  const human = (g.players || []).find((p) => p.kind === 'human');
  if (stockState.side === 'sell') {
    const pos = human && (human.positions || []).find((x) => x.symbol === sel.symbol);
    const avail = (pos && Number(pos.available_qty)) || 0;
    const qty = Math.floor(avail * frac * 1e6) / 1e6;
    if (stockState.el.qty) stockState.el.qty.value = qty > 0 ? String(qty) : '';
  } else {
    const price = Number(sel.last) || 0;
    const cash = (human && Number(human.cash)) || 0;
    if (price <= 0) return;
    let qty = cash * frac / price;
    qty = stockIsCrypto() ? Math.floor(qty * 1e6) / 1e6 : Math.floor(qty / 100) * 100;
    if (stockState.el.qty) stockState.el.qty.value = qty > 0 ? String(qty) : '';
  }
  stockUpdateEstimate();
}

/* 导出报告：后端返回文本 + 文件名，这里造一个 Blob 直接下载 */
async function stockExportReport() {
  const g = stockState.game;
  if (!g) return;
  stockMsg('正在生成复盘报告…');
  try {
    const data = await api('/api/hall/stock/report', { game_id: g.id, fmt: 'markdown' });
    const r = data.report || data;
    const text = String(r.text || '');
    if (!text) throw new Error('报告内容为空');
    const blob = new Blob([text], { type: 'text/markdown;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = r.filename || ('stock-' + g.id + '.md');
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    stockMsg('报告已下载：' + a.download);
  } catch (e) {
    stockMsg(e.message || e, true);
  }
}

function stockRenderPending() {
  const box = stockState.el.pending;
  if (!box) return;
  const rows = stockState.game.pending_orders || [];
  if (!rows.length) { box.innerHTML = '<div class="stock-empty small">没有待成交委托</div>'; return; }
  box.innerHTML = '<div class="stock-pending-title">待成交（下一交易日开盘）</div>'
    + rows.map((o) => (
      '<div class="stock-pending-row"><span class="' + (o.side === 'buy' ? 'up' : 'down') + '">'
      + (o.side === 'buy' ? '买' : '卖') + '</span><b>' + stockEsc(o.name || o.symbol) + '</b>'
      + '<em>' + stockQtyText(o.qty) + stockUnit() + '</em>'
      + (o.local
        ? '<span class="stock-pending-local">提交中…</span>'
        : '<button class="stock-link" data-cancel="' + stockEsc(o.id) + '">撤单</button>')
      + '</div>'
    )).join('');
  box.querySelectorAll('[data-cancel]').forEach((btn) => {
    btn.onclick = async () => {
      // 乐观撤单：先从本地列表移除，服务端确认后再用 patch 校正
      const g = stockState.game;
      const prevPending = (g.pending_orders || []).slice();
      const oid = btn.dataset.cancel;
      g.pending_orders = prevPending.filter((o) => o.id !== oid);
      stockRenderPending();
      try {
        const data = await api('/api/hall/stock/cancel',
          { game_id: g.id, order_id: oid, delta: 1 });
        if (data.patch) stockApplyPatch(data.patch);
        else if (data.game) stockApplyFull(data.game);
      } catch (e) {
        g.pending_orders = prevPending;
        stockRenderPending();
        toast(e.message || e, 'error');
      }
    };
  });
}

function stockSelectedSymbol() {
  return (stockState.game.symbols || []).find((s) => s.symbol === stockState.symbol)
    || (stockState.game.symbols || [])[0] || null;
}

function stockUpdateAccount() {
  const g = stockState.game;
  const human = (g.players || []).find((p) => p.kind === 'human');
  const sel = stockSelectedSymbol();
  const pos = human && (human.positions || []).find((p) => p.symbol === (sel && sel.symbol));
  if (stockState.el.account) {
    stockState.el.account.innerHTML = '现金 <b>' + stockMoney(human ? human.cash : 0) + '</b>'
      + ' · 总资产 <b>' + stockMoney(human ? human.equity : 0) + '</b> ' + stockCur()
      + (pos ? ' · 可卖 <b>' + stockQtyText(pos.available_qty) + '</b> ' + stockUnit() : '');
  }
}

function stockUpdateEstimate() {
  const sel = stockSelectedSymbol();
  const qty = Number((stockState.el.qty && stockState.el.qty.value) || 0);
  if (stockState.el.est && sel) {
    const amount = Number(sel.last) * (Number.isFinite(qty) ? qty : 0);
    stockState.el.est.textContent = '约 ' + stockMoney(amount) + ' ' + stockCur();
  }
}

async function stockSubmitOrder() {
  if (stockState.busy || !stockState.game) return;
  const sel = stockSelectedSymbol();
  if (!sel) return;
  const qty = Number((stockState.el.qty && stockState.el.qty.value) || 0);
  if (!Number.isFinite(qty) || qty <= 0) {
    stockMsg('数量必须大于 0', true);
    return;
  }
  // 限价单：勾了「限价」且填了价格才生效；没勾/没填一律按市价单（limit_price=0）走，
  // 与后端约定一致（0 = 市价）。
  const limitOn = !!(stockState.el.limitOn && stockState.el.limitOn.checked);
  let limitPrice = 0;
  if (limitOn) {
    limitPrice = Number((stockState.el.limitInput && stockState.el.limitInput.value) || 0);
    if (!Number.isFinite(limitPrice) || limitPrice <= 0) {
      stockMsg('限价单需要填写大于 0 的委托价，或取消勾选「限价」改用市价单', true);
      return;
    }
    // 涨跌停越界在这里挡一次，不必等后端报错：A 股 ±10%，币市不设限价区间
    if (!stockIsCrypto()) {
      const base = Number(sel.last) || 0;
      if (base > 0) {
        const hi = base * 1.1, lo = base * 0.9;
        if (limitPrice > hi + 1e-9 || limitPrice < lo - 1e-9) {
          stockMsg('委托价 ' + stockMoney(limitPrice) + ' 超出当日涨跌停区间 '
            + stockMoney(lo) + ' ~ ' + stockMoney(hi), true);
          return;
        }
      }
    }
  }
  const g = stockState.game;
  stockState.busy = true;
  // 乐观更新：本地先把这笔待成交挂上，界面立刻有反馈，随后用服务端增量校正
  const prevPending = (g.pending_orders || []).slice();
  g.pending_orders = prevPending.concat([{
    id: 'local_' + Date.now(), symbol: sel.symbol, name: sel.name,
    side: stockState.side, qty: qty, limit_price: limitPrice, local: true,
  }]);
  stockRenderPending();
  stockMsg('正在提交委托…');
  try {
    const data = await api('/api/hall/stock/order', {
      game_id: g.id,
      symbol: sel.symbol,
      side: stockState.side,
      qty: qty,
      limit_price: limitPrice,
      delta: 1,
    });
    if (data.patch) stockApplyPatch(data.patch);
    else if (data.game) stockApplyFull(data.game);
    stockMsg((stockState.side === 'buy' ? '买入' : '卖出') + '委托已提交，下一交易日开盘撮合');
  } catch (e) {
    g.pending_orders = prevPending;              // 失败就把乐观更新回滚
    stockRenderPending();
    stockMsg(e.message || e, true);
  } finally {
    stockState.busy = false;
  }
}

async function stockNextDay() {
  if (stockState.busy || !stockState.game) return;
  if (stockState.game.status === 'finished') return;
  const undecided = (stockState.game.players || []).filter(
    (p) => p.kind === 'ai' && !p.decided).length;
  if (undecided && !confirm('还有 ' + undecided + ' 个 AI 没有决策，继续会跳过它们本日决策。继续？')) return;
  stockState.busy = true;
  stockMsg('正在结算…');
  try {
    const data = await api('/api/hall/stock/next', { game_id: stockState.game.id, delta: 1 });
    if (data.patch) stockApplyPatch(data.patch);
    else if (data.game && data.game.players) stockApplyFull(data.game);
    stockState.aiBusy = {};
    stockState.aiErrors = {};
    stockState.aiRunFor = '';
    stockState.autoRun = !data.no_new_day && !data.finished;
    const cursor = (stockState.game && stockState.game.cursor_date) || '';
    stockMsg(data.message || (data.finished ? '回放已结束' : '已推进到 ' + cursor));
    // 推进后自动让 AI 决策（改成增量渲染后，这段逻辑不能只挂在 stockRenderGame 上）
    if (stockState.autoRun) {
      stockState.autoRun = false;
      stockEnsureAI();
    }
  } catch (e) {
    stockMsg(e.message || e, true);
  } finally {
    stockState.busy = false;
  }
}

function stockToggleAuto() {
  if (stockState.autoTimer) {
    stockClearTimer();
    stockState.el.auto.textContent = '自动播放';
    return;
  }
  if (stockState.game && stockState.game.status === 'finished') {
    toast('这一局已经结束了');
    return;
  }
  stockState.autoTimer = setInterval(() => {
    const g = stockState.game;
    if (!g || stockState.busy || g.status === 'finished') return;
    const busyAI = Object.keys(stockState.aiBusy).length > 0;
    if (g.ai_ready && !busyAI) stockNextDay();
  }, 5200);
  stockState.el.auto.textContent = '停止自动';
  toast('自动播放：AI 决策完成后每 5 秒推进一天');
}

async function stockEnsureAI(force) {
  const g = stockState.game;
  if (!g) return;
  if (!force && stockState.aiRunFor === g.cursor_date) return;
  stockState.aiRunFor = g.cursor_date;
  const pending = (g.players || []).filter(
    (p) => p.kind === 'ai' && !p.decided && !stockState.aiBusy[p.id]);
  if (!pending.length) return;
  await Promise.all(pending.map((p) => stockDecideAI(p.id)));
}

/* 一键决策：把所有还没决策的 AI 并行跑完，不用逐个点 */
async function stockDecideAll() {
  const g = stockState.game;
  if (!g) return;
  const pending = (g.players || []).filter(
    (p) => p.kind === 'ai' && !p.decided && !stockState.aiBusy[p.id]);
  if (!pending.length) { toast('所有 AI 都已决策'); return; }
  stockState.aiRunFor = g.cursor_date;
  stockMsg('正在让 ' + pending.length + ' 个 AI 同时决策…');
  await Promise.all(pending.map((p) => stockDecideAI(p.id)));
  const left = (stockState.game ? stockState.game.players : []).filter(
    (p) => p.kind === 'ai' && !p.decided);
  stockMsg(left.length
    ? '还有 ' + left.length + ' 个 AI 未完成，可再点一次'
    : '全部 AI 已决策，可以推进到下一日');
}

async function stockDecideAI(playerId) {
  if (!stockState.game || stockState.aiBusy[playerId]) return;
  const gid = stockState.game.id;
  const cursor = stockState.game.cursor_date;
  const who = (stockState.game.players || []).find((p) => p.id === playerId);
  stockState.aiBusy[playerId] = true;
  stockState.aiErrors[playerId] = '';
  stockRenderPlayers();
  stockRenderToolbar();
  try {
    const data = await api('/api/hall/stock/ai', {
      game_id: gid,
      player_id: playerId,
      delta: 1,
    });
    delete stockState.aiBusy[playerId];
    if (!stockState.game || stockState.game.id !== gid
        || stockState.game.cursor_date !== cursor) { stockRenderToolbar(); return; }
    if (data.patch) stockApplyPatch(data.patch);
    else if (data.game) stockApplyFull(data.game);
    if (data.journal) toast((who ? who.name : playerId) + '：' + data.journal);
  } catch (e) {
    delete stockState.aiBusy[playerId];
    stockState.aiErrors[playerId] = e.message || String(e);
    if (stockState.game && stockState.game.id === gid
        && stockState.game.cursor_date === cursor) {
      stockRenderPlayers();
      stockRenderToolbar();
    }
    toast('AI 决策失败：' + (e.message || e), 'error');
  }
}

/* ---------------- 训练辅助：盲预测 / 市场状态 / 计分板 ---------------- */

async function stockLoadAnalysis() {
  const g = stockState.game;
  if (!g) return;
  const seq = ++stockState.analysisSeq;
  try {
    const data = await api('/api/hall/stock/analysis', { game_id: g.id });
    if (seq !== stockState.analysisSeq) return;
    if (!stockState.game || stockState.game.id !== g.id) return;
    stockState.analysis = data;
    stockRenderPredict();
    stockRenderPlayers();
    stockRenderScoreboard();
    stockRenderTrades();       // 成交列表补上「5 日收益」
  } catch (e) { /* 训练面板拿不到不影响下单 / 行情等主流程 */ }
}

function stockRenderPredict() {
  const box = stockState.el.pList;
  const statusBox = stockState.el.pStatus;
  if (!box) return;
  const g = stockState.game;
  const a = stockState.analysis;
  const finished = !!g && g.status === 'finished';
  if (stockState.el.pSubmit) stockState.el.pSubmit.disabled = finished;
  const items = (a && a.predictions) || [];
  const pending = items.filter((x) => !x.settled).slice(0, 5);
  const done = items.filter((x) => x.settled).slice(0, 6);
  const rowOf = (x) => {
    const cls = x.direction === 'up' ? 'up' : (x.direction === 'down' ? 'down' : 'flat');
    const right = x.settled
      ? '<span class="stock-pred-res ' + (x.hit ? 'ok' : 'bad') + '">'
        + (x.hit ? '✅ 对' : '❌ 错') + ' · 实际 ' + stockPct(x.actual_pct) + '</span>'
      : '<span class="stock-pred-wait">还有 ' + x.remaining_days + ' 日揭晓</span>';
    return '<div class="stock-pred-row' + (x.settled ? '' : ' pending') + '">'
      + '<span class="stock-pred-target">' + stockEsc(x.target_name) + '</span>'
      + '<span class="stock-pred-dir ' + cls + '">' + stockEsc(x.direction_label) + '</span>'
      + '<span class="stock-pred-h">' + x.horizon + ' 日</span>'
      + '<span class="stock-pred-thr">阈值 ±' + x.threshold + '%</span>'
      + right + '</div>';
  };
  box.innerHTML = pending.concat(done).map(rowOf).join('')
    || '<div class="stock-empty small">还没有预测。选好方向后点「提交预测」。</div>';

  if (!statusBox) return;
  const st = (a && a.prediction_stats) || null;
  if (!st || (!st.settled && !st.pending)) {
    statusBox.textContent = '每天每种标的 / 期限可各下注一次，到期自动揭晓。';
    return;
  }
  let text = '已结算 ' + st.settled + ' · 命中 ' + st.hits + '（' + st.accuracy + '%）';
  if (st.streak && st.streak.n) {
    text += ' · 当前连续 ' + st.streak.n + (st.streak.type === 'hit' ? ' 对' : ' 错');
  }
  if (st.pending) text += ' · 待揭晓 ' + st.pending;
  const groups = (st.by_regime || []).slice(0, 3);
  statusBox.innerHTML = stockEsc(text) + (groups.length
    ? '<div class="stock-pred-groups">' + groups.map((x) => '<span>' + stockEsc(x.label)
      + ' ' + x.hits + '/' + x.settled + '（' + x.accuracy + '%）</span>').join('') + '</div>'
    : '');
}

async function stockSubmitPrediction() {
  if (stockState.busy || !stockState.game) return;
  if (stockState.game.status === 'finished') {
    stockMsg('这一局已经结束了', true);
    return;
  }
  const target = (stockState.el.pTarget && stockState.el.pTarget.value === 'benchmark')
    ? 'benchmark' : (stockState.symbol || '');
  if (!target) { stockMsg('先在 K 线上选一只标的', true); return; }
  const horizon = Number((stockState.el.pHorizon && stockState.el.pHorizon.value) || 3);
  const direction = stockState.predictDir || 'up';
  const btn = stockState.el.pSubmit;
  if (btn) btn.disabled = true;
  try {
    const data = await api('/api/hall/stock/predict', {
      game_id: stockState.game.id, target: target, horizon: horizon,
      direction: direction,
    });
    if (data.analysis) {
      stockState.analysis = data.analysis;
      stockRenderPredict();
      stockRenderPlayers();
      stockRenderScoreboard();
      stockRenderTrades();
    }
    const label = { up: '看涨', down: '看跌', flat: '看平' }[direction] || direction;
    const name = target === 'benchmark' ? stockBenchName() : stockSymbolName(target);
    stockMsg('预测已锁定：' + name + ' ' + horizon + ' 日后 ' + label + '，到期自动揭晓');
  } catch (e) {
    stockMsg(e.message || e, true);
  } finally {
    stockRenderPredict();
  }
}

/* ---------------- K 线图 ---------------- */

async function stockLoadBars() {
  const g = stockState.game;
  if (!g || !stockState.symbol) return;
  const cursor = g.cursor_date;
  const symbol = stockState.symbol;
  if (stockState.barsCursor !== cursor) {
    stockState.barsCache = {};
    stockState.marksCache = {};
    stockState.barsCursor = cursor;
  }
  const mkey = cursor + '|' + symbol;
  if (stockState.barsCache[symbol]) {
    stockState.chartBars = stockState.barsCache[symbol];
    stockState.chartMarks = stockState.marksCache[mkey] || [];
    stockDrawChart();
    return;
  }
  try {
    // marks=1：这一标的的成交点一起取回，用来在 K 线上标 ▲买入 / ▼卖出
    const data = await api('/api/hall/stock/bars', {
      game_id: g.id, symbol: symbol, limit: 120, marks: 1,
    });
    if (!stockState.game || stockState.game.cursor_date !== cursor
        || stockState.symbol !== symbol) return;
    stockState.barsFor = cursor;
    stockState.barsCache[symbol] = data.bars || [];
    stockState.marksCache[mkey] = data.marks || [];
    stockState.chartBars = data.bars || [];
    stockState.chartMarks = data.marks || [];
    stockDrawChart();
  } catch (e) {
    if (!stockState.game || stockState.game.cursor_date !== cursor
        || stockState.symbol !== symbol) return;
    stockState.chartBars = [];
    stockState.chartMarks = [];
    stockDrawChart('行情加载失败：' + (e.message || e));
  }
}

function stockSvg(tag, attrs) {
  const el = document.createElementNS('http://www.w3.org/2000/svg', tag);
  Object.keys(attrs || {}).forEach((k) => el.setAttribute(k, attrs[k]));
  return el;
}

/* 小图（对局页那张）的几何：720×300 下沿用原值，保证现有小图外观稳定；
   gapRight 是后加的「绘图区右留白」—— 让最新一根 K 线不贴着右侧价格刻度 */
const STOCK_CHART_GEOM = {
  w: 720, h: 360, left: 8, right: 62, gapRight: 14, top: 14,
  priceBottom: 276, volTop: 292, volBottom: 346,
  fs: 1, barMax: 8,
};

/* 放大浮层的几何：viewBox 单位直接取舞台像素，于是字号/线宽/格子都按屏幕实际尺寸算，
   整屏放大后不会出现「一根 K 线被拉成一块砖、日期字糊成一团」 */
function stockZoomGeom(w, h) {
  const fs = Math.max(1, Math.min(2.4, w / 720));
  return {
    w: w, h: h,
    left: Math.round(10 * fs), right: Math.round(66 * fs),
    gapRight: Math.round(16 * fs),
    top: Math.round(h * 0.05),
    priceBottom: Math.round(h * 0.675),
    volTop: Math.round(h * 0.745),
    volBottom: Math.round(h * 0.955),
    fs: fs, barMax: Math.max(8, Math.round(12 * fs)),
  };
}

function stockSymbolName(sym) {
  const list = ((stockState.game && stockState.game.symbols) || [])
    .concat(stockState.selected || []);
  const hit = list.find((s) => s && s.symbol === sym);
  return (hit && hit.name) || sym;
}

function stockDrawChart(emptyText) {
  const svg = stockState.el.chart;
  if (!svg) return;
  stockDrawKline({
    svg: svg,
    tip: stockState.el.tip,
    wrap: stockState.el.chartWrap,
    bars: stockState.chartBars || [],
    marks: stockVisibleMarks(),
    geom: STOCK_CHART_GEOM,
    emptyText: emptyText,
  });
  stockUpdateLegend();
  // 换标的 / 推进交易日之后，放大浮层里的图也要跟着刷新（原来贴右端就继续贴右端）
  if (stockState.zoom && stockState.zoom.open) stockZoomDraw();
}

/* 「谁下的单」：人类显示「你」，AI 去掉「价值派 / 趋势派 / 逆势派」前缀只留名字 */
function stockWho(m) {
  if (!m) return '';
  if (m.player_kind === 'human' || m.player_id === 'you') return '你';
  const name = String(m.player_name || m.player_id || '');
  return name.replace(/^(价值派|趋势派|逆势派)/, '') || name;
}

/* 图表上的成交筛选：全部 / 只看我 / 只看 AI */
function stockVisibleMarks() {
  const all = stockState.chartMarks || [];
  const f = stockState.markFilter;
  const mine = (m) => m.player_kind === 'human' || m.player_id === 'you';
  if (f === 'me') return all.filter(mine);
  if (f === 'ai') return all.filter((m) => !mine(m));
  return all;
}

/* 图例：按当前可见区间统计买卖笔数，让「▲/▼ 到底是什么」不用悬停也能看懂 */
function stockUpdateLegend() {
  const box = stockState.el.legend;
  if (!box) return;
  const dates = {};
  (stockState.chartBars || []).forEach((b) => { dates[b.date] = 1; });
  let buy = 0, sell = 0;
  const who = {};
  stockVisibleMarks().forEach((m) => {
    if (!dates[m.date]) return;
    if (m.side === 'sell') sell += 1;
    else buy += 1;
    const w = stockWho(m);
    who[w] = (who[w] || 0) + 1;
  });
  if (!buy && !sell) {
    box.innerHTML = '<span class="stock-ov-dim">当前区间无成交</span>';
    return;
  }
  const parts = Object.keys(who).map((w) => w + ' ' + who[w]);
  box.innerHTML = '<span class="up">▲ 买入 ' + buy + ' 笔</span>'
    + '<span class="down">▼ 卖出 ' + sell + ' 笔</span>'
    + '<span class="stock-ov-dim">' + stockEsc(parts.join(' · ')) + '</span>';
}

/* 真正的绘制：小图与放大浮层共用同一套逻辑。
   bars 是「要画的这几根」，maSource/maOffset 用来补足左边被裁掉的均线预热数据。 */
function stockDrawKline(opt) {
  const svg = opt.svg;
  const geom = opt.geom;
  const bars = opt.bars || [];
  const W = geom.w, H = geom.h, left = geom.left, right = geom.right;
  const top = geom.top, priceBottom = geom.priceBottom;
  const volTop = geom.volTop, volBottom = geom.volBottom;
  const fs = geom.fs || 1;
  svg.innerHTML = '';
  // 绘图区右边界再往左收 gapRight：最新一根 K 线与右侧价格刻度之间留出可看的空白
  const plotRight = W - right - (geom.gapRight || 0);
  const plotW = plotRight - left;
  if (!bars.length) {
    const t = stockSvg('text', {
      x: W / 2, y: H / 2, 'text-anchor': 'middle',
      fill: '#8a8f98', 'font-size': String(13 * fs),
    });
    t.textContent = opt.emptyText || '暂无行情';
    svg.appendChild(t);
    return { w: W, h: H, left: left, plotW: Math.max(1, plotW), step: 1 };
  }
  let lo = Infinity, hi = -Infinity;
  bars.forEach((b) => {
    lo = Math.min(lo, Number(b.low));
    hi = Math.max(hi, Number(b.high));
  });
  if (!Number.isFinite(lo) || !Number.isFinite(hi) || hi <= lo) {
    lo = Number(bars[0].low) || 0;
    hi = lo + 1;
  }
  const pad = (hi - lo) * 0.05;
  lo -= pad; hi += pad;
  const y = (v) => top + (hi - v) / (hi - lo) * (priceBottom - top);
  const step = plotW / bars.length;
  const bw = Math.max(1, Math.min(geom.barMax || 8, step * 0.58));

  // 网格与价格刻度
  const gridColor = '#7d838d';
  for (let i = 0; i <= 4; i += 1) {
    const v = lo + (hi - lo) * i / 4;
    const yy = y(v);
    svg.appendChild(stockSvg('line', {
      x1: left, y1: yy, x2: plotRight, y2: yy,
      stroke: gridColor, 'stroke-width': 0.5 * fs, opacity: 0.18,
    }));
    const tx = stockSvg('text', {
      x: plotRight + 5 * fs, y: yy + 4 * fs, fill: '#8a8f98', 'font-size': String(10 * fs),
    });
    tx.textContent = v.toFixed(2);
    svg.appendChild(tx);
  }

  // 悬停高亮：鼠标所在那一根的整列底色（放在 K 线之下，不挡内容）
  const hl = stockSvg('rect', {
    x: left, y: top, width: Math.max(1, step), height: priceBottom - top,
    fill: '#4d6bfe', opacity: 0, 'pointer-events': 'none',
  });
  svg.appendChild(hl);

  // K 线 + 成交量
  const maxVol = Math.max.apply(null, bars.map((b) => Number(b.volume) || 0)) || 1;
  bars.forEach((b, i) => {
    const x = left + (i + 0.5) * step;
    const o = Number(b.open), c = Number(b.close), h = Number(b.high), l = Number(b.low);
    const up = c >= o;
    const color = up ? '#e5534b' : '#2f9e6f';
    svg.appendChild(stockSvg('line', {
      x1: x, y1: y(h), x2: x, y2: y(l),
      stroke: color, 'stroke-width': fs,
    }));
    const yy = y(Math.max(o, c));
    const hh = Math.max(1, Math.abs(y(o) - y(c)));
    svg.appendChild(stockSvg('rect', {
      x: x - bw / 2, y: yy, width: bw, height: hh || 1, fill: color,
    }));
    const vh = (Number(b.volume) || 0) / maxVol * (volBottom - volTop);
    svg.appendChild(stockSvg('rect', {
      x: x - bw / 2, y: volBottom - vh, width: bw, height: Math.max(1, vh),
      fill: color, opacity: 0.42,
    }));
  });

  // 均线：closes 取 maSource（含窗口左边被裁掉的预热数据），off 是窗口在其中的起点，
  // 否则放大到中段时 MA20 会从窗口第 20 根才开始画，看上去像断了一截
  const off = opt.maOffset || 0;
  const closes = (opt.maSource || bars).map((b) => Number(b.close));
  const drawMA = (n, color) => {
    let d = '';
    for (let i = 0; i < bars.length; i += 1) {
      const j = i + off;
      if (j + 1 < n) continue;
      const avg = closes.slice(j - n + 1, j + 1).reduce((a, b) => a + b, 0) / n;
      const x = left + (i + 0.5) * step;
      d += (d ? ' L' : 'M') + x.toFixed(2) + ' ' + y(avg).toFixed(2);
    }
    if (d) svg.appendChild(stockSvg('path', {
      d: d, fill: 'none', stroke: color, 'stroke-width': 1.2 * fs, opacity: 0.9,
    }));
  };
  drawMA(5, '#e0a33c');
  drawMA(20, '#4d6bfe');

  // 成交标注：▲买入（蓝，标在成交价下方）· ▼卖出（橙，标在成交价上方）。
  // 用蓝/橙而不是红/绿，是为了和「红涨绿跌」的 K 线本体区分开，一眼能看出这是「我的动作」
  // 而不是「当天涨了」。x 由成交日定位到 K 线、y 由成交价走同一套价格刻度，与 K 线精准对齐。
  // 同一天多笔：横向扇形展开（而不是无限纵向堆叠），并画一条引线连回成交价，
  // 这样 5 笔以上也不会溢出绘图区，且每笔都看得出对应哪根 K 线的哪个价位。
  const tradeMarks = opt.marks || [];
  const marksByDate = {};
  const markEls = [];
  if (tradeMarks.length) {
    const idxByDate = {};
    bars.forEach((b, i) => { idxByDate[b.date] = i; });
    const groupAt = {};
    tradeMarks.forEach((m) => {
      (groupAt[m.date] = groupAt[m.date] || []).push(m);
    });
    Object.keys(groupAt).forEach((d) => {
      const list = groupAt[d];
      marksByDate[d] = list;
      const i = idxByDate[d];
      if (i === undefined) return;                 // 不在可见窗口里的成交不画
      const cx = left + (i + 0.5) * step;
      const n = list.length;
      list.forEach((m, k) => {
        const buy = m.side !== 'sell';
        const color = buy ? '#4d6bfe' : '#e0a33c';
        const py = y(Number(m.price) || Number(bars[i].close));
        // 扇形展开：第 k 笔相对中轴偏移，越靠后的偏移越大；单笔时偏移为 0
        const fan = n > 1 ? (k - (n - 1) / 2) : 0;
        const x = cx + fan * Math.max(6, bw * 1.15) * fs;
        const off = (12 + Math.abs(fan) * 3) * fs;
        const apexY = buy ? py + off * 0.32 : py - off * 0.32;
        const baseY = buy ? py + off : py - off;
        const half = Math.max(5, bw * 1.1);
        const g = stockSvg('g', {});
        // 引线：从三角尖端连回成交价，明确「这笔标的是这个价位」
        g.appendChild(stockSvg('line', {
          x1: x.toFixed(2), y1: apexY.toFixed(2),
          x2: cx.toFixed(2), y2: py.toFixed(2),
          stroke: color, 'stroke-width': 0.8 * fs, opacity: 0.55,
        }));
        const tri = stockSvg('path', {
          d: 'M' + x.toFixed(2) + ' ' + apexY.toFixed(2)
            + ' L' + (x - half).toFixed(2) + ' ' + baseY.toFixed(2)
            + ' L' + (x + half).toFixed(2) + ' ' + baseY.toFixed(2) + ' Z',
          fill: color, opacity: 0.95,
        });
        g.appendChild(tri);
        const label = (buy ? '买' : '卖') + stockQtyText(m.qty) + stockUnit()
          + (stockWho(m) ? '·' + stockWho(m) : '');
        const tx = stockSvg('text', {
          x: x.toFixed(2), y: (buy ? baseY + 9 * fs : baseY - 4 * fs).toFixed(2),
          fill: color, 'font-size': String(10 * fs), 'text-anchor': 'middle',
          'paint-order': 'stroke', stroke: 'rgba(0,0,0,.55)', 'stroke-width': 2 * fs,
        });
        tx.textContent = label;
        g.appendChild(tx);
        g.setAttribute('class', 'stock-mark');
        svg.appendChild(g);
        markEls.push({ el: g, tri: tri, date: d });
      });
    });
  }

  // 日期刻度：按绘图区宽度自适应个数，小图 5 个、放大到整屏能到 8 个
  const tickCount = Math.max(2, Math.min(8, Math.round(plotW / (90 * fs))));
  const tickEvery = Math.max(1, Math.floor((bars.length - 1) / Math.max(1, tickCount - 1)));
  for (let i = 0; i < bars.length; i += tickEvery) {
    const b = bars[i];
    const x = left + (i + 0.5) * step;
    const tx = stockSvg('text', {
      x: x, y: H - 6 * fs, fill: '#8a8f98', 'font-size': String(9 * fs), 'text-anchor': 'middle',
    });
    tx.textContent = String(b.date || '').slice(2);
    svg.appendChild(tx);
  }

  // 联动锚点（多节点对比）：把同一天在各屏里都用竖线highlight出来
  const anchorDate = opt.anchorDate || '';
  if (anchorDate) {
    const ai = bars.findIndex((b) => b.date === anchorDate);
    if (ai >= 0) {
      const x = left + (ai + 0.5) * step;
      svg.appendChild(stockSvg('line', {
        x1: x, y1: top, x2: x, y2: priceBottom,
        stroke: '#4d6bfe', 'stroke-width': 1 * fs, opacity: 0.7, 'stroke-dasharray': '3 3',
      }));
    }
  }

  // 悬浮提示（含当日成交详情）
  const overlay = stockSvg('rect', {
    x: left, y: top, width: plotW, height: priceBottom - top,
    fill: 'none', 'pointer-events': 'all',
  });
  const tip = opt.tip;
  const wrap = opt.wrap;
  // 十字光标：横竖两条虚线 + 右侧价格签 + 顶部日期签，跟着鼠标走
  /* display:none 而不是 opacity:0 —— 隐藏时必须不参与布局，
     否则不可见的价签矩形会被判定为「遮挡」其他元素 */
  const crossG = stockSvg('g', { display: 'none', 'pointer-events': 'none' });
  const crossV = stockSvg('line', {
    y1: top, y2: volBottom, stroke: '#8a8f98', 'stroke-width': 1 * fs,
    'stroke-dasharray': '4 3', opacity: 0.75,
  });
  const crossH = stockSvg('line', {
    x1: left, x2: plotRight, stroke: '#8a8f98', 'stroke-width': 1 * fs,
    'stroke-dasharray': '4 3', opacity: 0.75,
  });
  const priceTagBg = stockSvg('rect', {
    x: plotRight + 2 * fs, width: right - 6 * fs, height: 15 * fs, rx: 3 * fs, fill: '#4d6bfe',
  });
  const priceTagTx = stockSvg('text', {
    x: plotRight + 5 * fs, y: 0, fill: '#fff',
    'font-size': String(9.5 * fs), 'dominant-baseline': 'middle',
  });
  const dateTagBg = stockSvg('rect', {
    y: H - 16 * fs, width: 62 * fs, height: 14 * fs, rx: 3 * fs, fill: '#4d6bfe',
  });
  const dateTagTx = stockSvg('text', {
    y: H - 6 * fs, fill: '#fff', 'font-size': String(9.5 * fs), 'text-anchor': 'middle',
  });
  crossG.appendChild(crossV);
  crossG.appendChild(crossH);
  crossG.appendChild(priceTagBg);
  crossG.appendChild(priceTagTx);
  crossG.appendChild(dateTagBg);
  crossG.appendChild(dateTagTx);
  svg.appendChild(crossG);

  overlay.addEventListener('mousemove', (e) => {
    const rect = svg.getBoundingClientRect();
    const px = (e.clientX - rect.left) / Math.max(1, rect.width) * W;
    const py = (e.clientY - rect.top) / Math.max(1, rect.height) * H;
    const idx = Math.max(0, Math.min(bars.length - 1,
      Math.floor((px - left) / step)));
    const b = bars[idx];
    if (!b) return;
    hl.setAttribute('x', (left + idx * step).toFixed(2));
    hl.setAttribute('opacity', '0.10');
    // 十字：竖线吸附到 K 线中轴，横线跟随鼠标实际 y，价签显示该处的价格
    const cx = left + (idx + 0.5) * step;
    crossV.setAttribute('x1', cx.toFixed(2));
    crossV.setAttribute('x2', cx.toFixed(2));
    const cy = Math.max(top, Math.min(priceBottom, py));
    crossH.setAttribute('y1', cy.toFixed(2));
    crossH.setAttribute('y2', cy.toFixed(2));
    const priceAt = hi - (cy - top) / Math.max(1, priceBottom - top) * (hi - lo);
    priceTagBg.setAttribute('y', (cy - 7.5 * fs).toFixed(2));
    priceTagTx.setAttribute('y', cy.toFixed(2));
    priceTagTx.textContent = stockMoney(priceAt);
    const tagW = 62 * fs;
    dateTagBg.setAttribute('x', (cx - tagW / 2).toFixed(2));
    dateTagBg.setAttribute('width', tagW.toFixed(2));
    dateTagTx.setAttribute('x', cx.toFixed(2));
    dateTagTx.textContent = String(b.date || '');
    crossG.setAttribute('display', 'inline');
    if (opt.onHoverDate) opt.onHoverDate(b.date);
    if (tip) {
      const dayTrades = marksByDate[b.date] || [];
      tip.innerHTML = '<b>' + stockEsc(b.date) + '</b>'
        + '<span>开 ' + stockMoney(b.open) + '　收 ' + stockMoney(b.close) + '</span>'
        + '<span>高 ' + stockMoney(b.high) + '　低 ' + stockMoney(b.low) + '</span>'
        + dayTrades.map((m) => '<span class="stock-tip-trade ' + (m.side === 'sell' ? 'down' : 'up')
          + '">' + (m.side === 'sell' ? '▼ 卖出 ' : '▲ 买入 ') + stockQtyText(m.qty) + stockUnit() + ' @ '
          + stockMoney(m.price) + (m.amount ? '（' + stockMoney(m.amount) + ' ' + stockCur() + '）' : '')
          + ' · ' + stockEsc(stockWho(m))
          + '</span>').join('');
      tip.classList.remove('hidden');
      const wr = (wrap || svg.parentNode).getBoundingClientRect();
      const tx = Math.max(6, Math.min(wr.width - 150, e.clientX - wr.left + 10));
      tip.style.left = tx + 'px';
      tip.style.top = '8px';
    }
  });
  overlay.addEventListener('mouseleave', () => {
    hl.setAttribute('opacity', '0');
    crossG.setAttribute('display', 'none');   // 鼠标移出要把十字一起收掉
    if (tip) tip.classList.add('hidden');
    if (opt.onHoverDate) opt.onHoverDate('');
  });
  svg.appendChild(overlay);
  return { w: W, h: H, left: left, plotW: plotW, step: step };
}

/* ---------------- K 线放大（全屏浮层） ----------------
   浮层挂在 <body> 上而不是大厅容器里：大厅容器带 overflow 限制，塞在里面放不成全屏。
   缩放模型就一句话：可见窗口 = chartBars 的 [start, start + count) 这一段 ——
   count 越小 = 放得越大（看到的根数越少），start 是窗口左端在整段行情里的下标。
   于是「滚轮缩放」= 改 count、「拖动平移」= 改 start，不用碰任何 transform。 */

const STOCK_ZOOM_MIN_BARS = 10;      // 放到最大也留 10 根，再少就看不出形态了
const STOCK_ZOOM_DEFAULT_BARS = 60;  // 打开 / 双击复位时的默认可见根数

function stockZoomTotal() {
  return (stockState.chartBars || []).length;
}

/* 窗口合法化：count 收在 [min(10, 总数), 总数]，start 收在 [0, 总数 - count]；
   顺手记下「是否贴住右端」—— 推进交易日刷新数据后，原本贴右端的窗口继续贴右端 */
function stockZoomSet(start, count) {
  const z = stockState.zoom;
  if (!z) return;
  const total = stockZoomTotal();
  const min = Math.min(STOCK_ZOOM_MIN_BARS, Math.max(1, total));
  z.count = Math.max(min, Math.min(Math.max(1, total), Math.round(count)));
  z.start = Math.max(0, Math.min(Math.max(0, total - z.count), Math.round(start)));
  z.atRight = (z.start + z.count >= total);
}

function stockBuildZoom() {
  const wrap = document.createElement('div');
  wrap.className = 'stock-zoom hidden';
  wrap.innerHTML = [
    '<div class="stock-zoom-bar">',
    '  <span class="stock-zoom-title" id="stock-zoom-title">K 线</span>',
    '  <span class="stock-zoom-info" id="stock-zoom-info"></span>',
    '  <span class="stock-zoom-spacer"></span>',
    '  <button type="button" data-z="out" title="缩小：看到更多 K 线">−</button>',
    '  <button type="button" data-z="in" title="放大：看到更少 K 线">＋</button>',
    '  <button type="button" data-z="fit" title="把全部行情放进一屏">适应全部</button>',
    '  <button type="button" data-z="last" title="回到最近的一段">最新</button>',
    '  <button type="button" data-z="close" title="关闭（Esc）">✕ 关闭</button>',
    '</div>',
    '<div class="stock-zoom-stage">',
    '  <svg class="stock-zoom-svg"></svg>',
    '  <div class="stock-tip hidden"></div>',
    '</div>',
    '<div class="stock-zoom-hint">滚轮缩放 · 按住拖动平移 · 双击复位 · Esc 关闭</div>',
  ].join('');
  document.body.appendChild(wrap);

  const stage = wrap.querySelector('.stock-zoom-stage');
  const z = stockState.zoom = {
    overlay: wrap,
    stage: stage,
    svg: wrap.querySelector('.stock-zoom-svg'),
    tip: wrap.querySelector('.stock-tip'),
    title: wrap.querySelector('#stock-zoom-title'),
    info: wrap.querySelector('#stock-zoom-info'),
    open: false, start: 0, count: STOCK_ZOOM_DEFAULT_BARS, atRight: true,
    drag: null, metrics: null, raf: 0, onKey: null, onResize: null,
  };

  wrap.querySelector('.stock-zoom-bar').addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-z]');
    if (!btn) return;
    const k = btn.dataset.z;
    if (k === 'close') stockCloseZoom();
    else if (k === 'in') stockZoomBy(1 / 1.28);
    else if (k === 'out') stockZoomBy(1.28);
    else if (k === 'fit') { stockZoomSet(0, stockZoomTotal()); stockZoomDraw(); }
    else if (k === 'last') {
      // 只挪起点的话，在「适应全部」之后点它等于没点（窗口已覆盖全部、起点恒为 0），
      // 所以窗口已经覆盖全部时顺便收窄到默认根数，保证点了一定有变化
      const total = stockZoomTotal();
      const count = z.count >= total ? Math.min(total, STOCK_ZOOM_DEFAULT_BARS) : z.count;
      stockZoomSet(total - count, count);
      stockZoomDraw();
    }
  });
  stage.addEventListener('wheel', stockZoomWheel, { passive: false });
  stage.addEventListener('pointerdown', stockZoomDown);
  stage.addEventListener('pointermove', stockZoomMove);
  stage.addEventListener('pointerup', stockZoomUp);
  stage.addEventListener('pointercancel', stockZoomUp);
  stage.addEventListener('dblclick', () => {
    stockZoomSet(stockZoomTotal() - STOCK_ZOOM_DEFAULT_BARS, STOCK_ZOOM_DEFAULT_BARS);
    stockZoomDraw();
  });
  z.onKey = (e) => {
    if (e.key === 'Escape' && z.open) { e.stopPropagation(); stockCloseZoom(); }
  };
  z.onResize = () => { if (z.open) stockZoomDraw(); };
  window.addEventListener('resize', z.onResize);
  return z;
}

/* 以窗口中点为锚点缩放（＋/− 按钮用；滚轮用鼠标位置当锚点，见 stockZoomWheel） */
function stockZoomBy(factor) {
  const z = stockState.zoom;
  if (!z) return;
  const total = stockZoomTotal();
  const min = Math.min(STOCK_ZOOM_MIN_BARS, Math.max(1, total));
  const count = Math.max(min, Math.min(Math.max(1, total), Math.round(z.count * factor)));
  stockZoomSet(z.start + z.count / 2 - count / 2, count);
  stockZoomDraw();
}

function stockOpenZoom() {
  const total = stockZoomTotal();
  if (!total) { toast('暂无行情，先点「下一日」再看'); return; }
  const z = stockState.zoom || stockBuildZoom();
  if (z.open) return;
  z.open = true;
  z.drag = null;
  stockZoomSet(total - STOCK_ZOOM_DEFAULT_BARS, STOCK_ZOOM_DEFAULT_BARS);
  z.overlay.classList.remove('hidden');   // 必须先显出来，否则舞台量出来是 0×0
  window.addEventListener('keydown', z.onKey, true);
  stockZoomDraw();
}

function stockCloseZoom() {
  const z = stockState.zoom;
  if (!z || !z.open) return;
  z.open = false;
  z.drag = null;
  z.stage.classList.remove('dragging');
  z.tip.classList.add('hidden');
  z.overlay.classList.add('hidden');
  window.removeEventListener('keydown', z.onKey, true);
}

function stockDestroyZoom() {
  const z = stockState.zoom;
  if (!z) return;
  stockCloseZoom();
  window.removeEventListener('resize', z.onResize);
  if (z.raf) cancelAnimationFrame(z.raf);
  if (z.overlay.parentNode) z.overlay.parentNode.removeChild(z.overlay);
  stockState.zoom = null;
}

function stockZoomSchedule() {
  const z = stockState.zoom;
  if (!z || !z.metrics || z.raf) return;
  z.raf = requestAnimationFrame(() => { z.raf = 0; stockZoomDraw(); });
}

function stockZoomDraw() {
  const z = stockState.zoom;
  if (!z || !z.open) return;
  const total = stockZoomTotal();
  const wasRight = z.atRight !== false;
  if (wasRight) stockZoomSet(total - z.count, z.count);   // 数据变多时继续贴最新
  else stockZoomSet(z.start, z.count);
  const stage = z.stage;
  const w = Math.max(320, Math.round(stage.clientWidth) || 0);
  const h = Math.max(180, Math.round(stage.clientHeight) || 0);
  z.svg.setAttribute('viewBox', '0 0 ' + w + ' ' + h);
  const all = stockState.chartBars || [];
  const bars = all.slice(z.start, z.start + z.count);
  z.metrics = stockDrawKline({
    svg: z.svg, tip: z.tip, wrap: stage, bars: bars,
    marks: stockVisibleMarks(),
    geom: stockZoomGeom(w, h), emptyText: '暂无行情',
    maSource: all, maOffset: z.start,
  });
  z.title.textContent = stockSymbolName(stockState.symbol);
  z.info.textContent = '可见 ' + (z.start + 1) + '–' + (z.start + bars.length)
    + ' / 共 ' + total + ' 根';
  z.atRight = (z.start + z.count >= total);
  const fit = z.overlay.querySelector('button[data-z="fit"]');
  if (fit) fit.disabled = (z.count >= total);
}

function stockZoomWheel(e) {
  const z = stockState.zoom;
  if (!z || !z.open || !z.metrics) return;
  e.preventDefault();
  const m = z.metrics;
  const rect = z.svg.getBoundingClientRect();
  const px = (e.clientX - rect.left) / Math.max(1, rect.width) * m.w;
  // 鼠标下方那根的「绝对根坐标」（含小数），缩放后让它停在原处 —— 就是看图的习惯
  const anchor = z.start + (px - m.left) / m.step;
  const total = stockZoomTotal();
  const min = Math.min(STOCK_ZOOM_MIN_BARS, Math.max(1, total));
  const count = Math.max(min, Math.min(Math.max(1, total),
    Math.round(z.count * (e.deltaY < 0 ? 1 / 1.28 : 1.28))));
  if (count === z.count) return;
  stockZoomSet(anchor - (px - m.left) / (m.plotW / count), count);
  stockZoomSchedule();
}

function stockZoomDown(e) {
  const z = stockState.zoom;
  if (!z || !z.open || e.button !== 0) return;
  e.preventDefault();
  z.drag = { sx: e.clientX, start: z.start };
  z.stage.classList.add('dragging');
  try { z.stage.setPointerCapture(e.pointerId); } catch (err) { /* 不支持捕获也能拖 */ }
}

function stockZoomMove(e) {
  const z = stockState.zoom;
  if (!z || !z.drag || !z.metrics) return;
  // 向右拖 = 把画面往右带 = 露出更早的行情，所以 start 要减小
  stockZoomSet(z.drag.start - (e.clientX - z.drag.sx) / z.metrics.step, z.count);
  stockZoomSchedule();
}

function stockZoomUp() {
  const z = stockState.zoom;
  if (!z) return;
  z.drag = null;
  z.stage.classList.remove('dragging');
}

/* ---------------- 通用浮层工具 ---------------- */

function stockOvEl(cls) {
  const wrap = document.createElement('div');
  wrap.className = 'stock-ov ' + cls + ' hidden';
  return wrap;
}

function stockBindEsc(owner, close) {
  owner.onKey = (e) => {
    if (e.key === 'Escape' && owner.open) {
      e.stopPropagation();
      close();
    }
  };
  window.addEventListener('keydown', owner.onKey, true);
}

function stockUnbindEsc(owner) {
  if (owner && owner.onKey) window.removeEventListener('keydown', owner.onKey, true);
}

async function stockLoadTimeline(force) {
  if (!force && stockState.timeline.length) return stockState.timeline;
  const g = stockState.game;
  if (!g) return [];
  const data = await api('/api/hall/stock/timeline', { game_id: g.id });
  stockState.timeline = data.dates || [];
  return stockState.timeline;
}

function _stockKindChips(kinds, active) {
  return '<button class="stock-chip-btn' + (!active ? ' active' : '')
    + '" data-kind="">全部</button>'
    + kinds.map((k) => '<button class="stock-chip-btn'
      + (k === active ? ' active' : '') + '" data-kind="' + stockEsc(k) + '">'
      + stockEsc(k) + '</button>').join('');
}

/* ---------------- 时间轴回溯（只读快照） ---------------- */

/* 回溯视图：把 state_at 的结果画成「当时的行情 + 持仓 + 分析 + 成交」。
   行情用同一套 stockDrawKline，所以标注（▲买 ▼卖）在回溯里也在。 */
function stockBuildHistory() {
  const wrap = stockOvEl('stock-history');
  wrap.innerHTML = [
    '<div class="stock-ov-bar">',
    '  <button type="button" data-h="back">← 返回对局</button>',
    '  <b>时间轴回溯</b>',
    '  <span class="stock-ov-badge">只读快照 · 不影响当前对局</span>',
    '  <span class="stock-ov-info" id="stock-hist-info"></span>',
    '  <span class="stock-ov-spacer"></span>',
    '  <button type="button" data-h="prev">‹ 前一天</button>',
    '  <input type="range" id="stock-hist-range" min="0" max="0" value="0">',
    '  <button type="button" data-h="next">后一天 ›</button>',
    '  <input type="date" id="stock-hist-date">',
    '  <button type="button" data-h="latest">回到最新</button>',
    '  <button type="button" data-h="close">✕ 关闭</button>',
    '</div>',
    '<div class="stock-ov-body">',
    '  <div class="stock-hist-main">',
    '    <div class="stock-hist-head"><span id="stock-hist-title">K 线</span>',
    '      <span class="stock-ov-dim" id="stock-hist-sub"></span></div>',
    '    <div class="stock-hist-symbols" id="stock-hist-symbols"></div>',
    '    <div class="stock-hist-stage"><svg class="stock-hist-svg"></svg>',
    '      <div class="stock-tip hidden"></div></div>',
    '  </div>',
    '  <div class="stock-hist-side">',
    '    <div class="stock-card"><div class="stock-card-title">该时刻资产 / 持仓</div>',
    '      <div id="stock-hist-assets"></div></div>',
    '    <div class="stock-card"><div class="stock-card-title">AI 分析</div>',
    '      <div id="stock-hist-analyses"></div></div>',
    '    <div class="stock-card"><div class="stock-card-title">历史成交</div>',
    '      <div id="stock-hist-trades"></div></div>',
    '  </div>',
    '</div>',
    '<div class="stock-ov-hint">只读：行情 / 持仓 / 资产 / AI 分析都还原到该日收盘；'
      + '这里不能下单，看完点「← 返回对局」继续。</div>',
  ].join('');
  document.body.appendChild(wrap);

  const h = stockState.history = {
    overlay: wrap,
    info: wrap.querySelector('#stock-hist-info'),
    range: wrap.querySelector('#stock-hist-range'),
    dateInput: wrap.querySelector('#stock-hist-date'),
    title: wrap.querySelector('#stock-hist-title'),
    sub: wrap.querySelector('#stock-hist-sub'),
    symbols: wrap.querySelector('#stock-hist-symbols'),
    stage: wrap.querySelector('.stock-hist-stage'),
    svg: wrap.querySelector('.stock-hist-svg'),
    tip: wrap.querySelector('.stock-tip'),
    assets: wrap.querySelector('#stock-hist-assets'),
    analyses: wrap.querySelector('#stock-hist-analyses'),
    trades: wrap.querySelector('#stock-hist-trades'),
    open: false, idx: 0, date: '', symbol: '', node: null, seq: 0,
    onKey: null, onResize: null, raf: 0,
  };
  const jump = (delta) => {
    const total = stockState.timeline.length;
    if (!total) return;
    stockHistoryLoad(stockState.timeline[Math.max(0, Math.min(total - 1, h.idx + delta))]);
  };
  wrap.querySelector('.stock-ov-bar').addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-h]');
    if (!btn) return;
    const k = btn.dataset.h;
    if (k === 'close' || k === 'back') stockCloseHistory();
    else if (k === 'prev') jump(-1);
    else if (k === 'next') jump(1);
    else if (k === 'latest') stockHistoryLoad(stockState.timeline[stockState.timeline.length - 1]);
  });
  h.range.addEventListener('input', () => {
    const total = stockState.timeline.length;
    if (!total) return;
    stockHistoryLoad(stockState.timeline[Math.max(0, Math.min(total - 1, Number(h.range.value)))]
      || '');
  });
  h.dateInput.addEventListener('change', () => {
    if (h.dateInput.value) stockHistoryLoad(h.dateInput.value);
  });
  h.symbols.addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-sym]');
    if (!btn) return;
    stockHistoryLoad(h.date, btn.dataset.sym);
  });
  h.onResize = () => { if (h.open) stockHistoryDraw(); };
  window.addEventListener('resize', h.onResize);
  return h;
}

async function stockOpenHistory() {
  if (!stockState.game) return;
  const h = stockState.history || stockBuildHistory();
  if (h.open) return;
  h.open = true;
  h.overlay.classList.remove('hidden');   // 先显出来，否则舞台量出来是 0×0
  stockBindEsc(h, stockCloseHistory);
  try {
    const dates = await stockLoadTimeline(true);
    if (!dates.length) { stockCloseHistory(); toast('这一局还没有可回溯的交易日'); return; }
    h.range.min = '0';
    h.range.max = String(dates.length - 1);
    stockHistoryLoad(dates[dates.length - 1]);
  } catch (e) {
    h.info.textContent = '加载失败：' + (e.message || e);
  }
}

async function stockHistoryLoad(date, symbol) {
  const h = stockState.history;
  if (!h || !h.open) return;
  const g = stockState.game;
  if (!g) return;
  const seq = ++h.seq;
  h.info.textContent = '加载中…';
  try {
    const data = await api('/api/hall/stock/state_at', {
      game_id: g.id,
      date: date || '',
      symbol: symbol || h.symbol || stockState.symbol || '',
    });
    if (seq !== h.seq || !h.open) return;
    h.node = data.state;
    h.date = data.state.as_of;
    h.symbol = (data.state.chart || {}).symbol || '';
    stockHistoryRender();
  } catch (e) {
    if (seq !== h.seq || !h.open) return;
    h.info.textContent = '加载失败：' + (e.message || e);
  }
}

function stockHistoryRender() {
  const h = stockState.history;
  const st = h.node;
  if (!h || !st) return;
  const total = stockState.timeline.length;
  h.idx = Math.max(0, stockState.timeline.indexOf(st.as_of));
  h.range.value = String(h.idx);
  h.dateInput.value = st.as_of;
  h.info.textContent = (total ? '第 ' + (h.idx + 1) + ' / ' + total + ' 个交易日 · ' : '')
    + '截至 ' + st.as_of;
  h.title.textContent = (stockSymbolName(h.symbol) || 'K 线') + ' · 回溯';
  const bm = st.benchmark || {};
  h.sub.textContent = stockBenchName() + ' ' + stockPct(bm.return_pct);

  h.symbols.innerHTML = (st.symbols || []).map((s) => (
    '<button class="stock-sym' + (s.symbol === h.symbol ? ' active' : '')
    + '" data-sym="' + stockEsc(s.symbol) + '">'
    + '<span class="stock-sym-name">' + stockEsc(s.name) + '</span>'
    + '<span class="stock-sym-price ' + (Number(s.change_pct) >= 0 ? 'up' : 'down') + '">'
    + stockMoney(s.last) + ' <i>' + stockPct(s.change_pct) + '</i></span></button>'
  )).join('');

  h.assets.innerHTML = (st.players || []).map((p) => {
    const pos = (p.positions || []).map((x) => (
      '<div class="stock-pos-row"><b>' + stockEsc(stockSymbolName(x.symbol)) + '</b>'
      + '<em>' + stockQtyText(x.qty) + ' ' + stockUnit() + ' · 成本 ' + stockMoney(x.avg_cost) + ' · 现价 '
      + stockMoney(x.last) + '</em>'
      + '<span class="' + (x.pnl_pct >= 0 ? 'up' : 'down') + '">' + stockPct(x.pnl_pct)
      + '</span></div>'
    )).join('') || '<div class="stock-empty small">该时刻空仓</div>';
    return '<div class="stock-hist-player"><div class="stock-hist-player-top">'
      + '<b>' + stockEsc(p.name) + '</b>'
      + '<span class="' + (p.return_pct >= 0 ? 'up' : 'down') + '"'
      + ' title="总收益率（相对初始资金，含浮盈浮亏）">' + stockPct(p.return_pct)
      + '</span><em>' + stockMoney(p.equity) + '</em></div>'
      + '<div class="stock-hist-player-sub">现金 ' + stockMoney(p.cash)
      + ' · 市值 ' + stockMoney(p.market_value)
      + ' · 已实现 ' + stockMoney(p.realized_pnl) + '</div>' + pos + '</div>';
  }).join('');

  const rows = st.analyses || [];
  h.analyses.innerHTML = rows.length ? rows.map((a) => (
    '<div class="stock-analysis"><div class="stock-analysis-top"><i>' + stockEsc(a.date)
    + '</i><em>' + stockEsc(a.kind) + '</em><b>'
    + stockEsc((st.players.find((p) => p.id === a.player_id) || {}).name || a.player_id)
    + '</b></div><div class="stock-analysis-text">' + stockEsc(a.text) + '</div>'
    + (a.keywords ? '<div class="stock-analysis-kw">'
      + stockEsc(a.keywords).split(',').filter(Boolean).map((k) => '<span>#' + stockEsc(k)
        + '</span>').join('') + '</div>' : '')
    + '</div>'
  )).join('') : '<div class="stock-empty small">该时刻还没有 AI 分析</div>';

  const trades = st.trades || [];
  h.trades.innerHTML = trades.length ? trades.slice(0, 20).map((t) => (
    '<div class="stock-trade"><span class="' + (t.side === 'buy' ? 'up' : 'down') + '">'
    + (t.side === 'buy' ? '买' : '卖') + '</span><b>'
    + stockEsc(t.name || stockSymbolName(t.symbol)) + '</b><em>' + stockQtyText(t.qty)
    + stockUnit() + ' @ '
    + stockMoney(t.price) + '</em><i>' + stockEsc(stockWho(t)) + ' · '
    + stockEsc(t.date) + '</i></div>'
  )).join('') : '<div class="stock-empty small">该时刻还没有成交</div>';

  stockHistoryDraw();
}

function stockHistoryDraw() {
  const h = stockState.history;
  if (!h || !h.open || !h.node) return;
  const chart = h.node.chart || {};
  const w = Math.max(320, Math.round(h.stage.clientWidth) || 0);
  const ah = Math.max(200, Math.round(h.stage.clientHeight) || 0);
  h.svg.setAttribute('viewBox', '0 0 ' + w + ' ' + ah);
  stockDrawKline({
    svg: h.svg, tip: h.tip, wrap: h.stage,
    bars: chart.bars || [], marks: chart.marks || [],
    geom: stockZoomGeom(w, ah), emptyText: '该时刻暂无行情',
  });
}

function stockCloseHistory() {
  const h = stockState.history;
  if (!h || !h.open) return;
  h.open = false;
  h.seq += 1;
  h.tip.classList.add('hidden');
  h.overlay.classList.add('hidden');
  stockUnbindEsc(h);
}

function stockDestroyHistory() {
  const h = stockState.history;
  if (!h) return;
  stockCloseHistory();
  window.removeEventListener('resize', h.onResize);
  if (h.overlay.parentNode) h.overlay.parentNode.removeChild(h.overlay);
  stockState.history = null;
}

/* ---------------- 历史分析检索 ---------------- */

const STOCK_SEARCH_KINDS = ['大盘', '个股', '持仓', '风险', '机会', '复盘'];

function stockBuildSearch() {
  const wrap = stockOvEl('stock-search');
  wrap.innerHTML = [
    '<div class="stock-ov-bar">',
    '  <button type="button" data-s="back">← 返回对局</button>',
    '  <b>历史分析检索</b>',
    '  <span class="stock-ov-dim">先筛条件，再点任意一条回看当天的行情</span>',
    '  <span class="stock-ov-dim" id="stock-search-engine"></span>',
    '  <span class="stock-ov-spacer"></span>',
    '  <button type="button" data-s="close">✕ 关闭</button>',
    '</div>',
    '<div class="stock-search-filters">',
    '  <input id="stock-s-q" type="text" placeholder="关键词：突破 / 放量 / 回撤…（留空表示不限）">',
    '  <input id="stock-s-symbol" type="text" placeholder="标的代码，例如 600000（可留空）">',
    '  <input id="stock-s-from" type="date" title="起始日期">',
    '  <input id="stock-s-to" type="date" title="结束日期">',
    '  <select id="stock-s-size">',
    '    <option value="20">每页 20</option>',
    '    <option value="50">每页 50</option>',
    '    <option value="100">每页 100</option>',
    '  </select>',
    '  <button class="stock-btn primary" id="stock-s-go">检索</button>',
    '</div>',
    '<div class="stock-search-kinds" id="stock-s-kinds"></div>',
    '<div class="stock-ov-info" id="stock-s-meta"></div>',
    '<div class="stock-search-list" id="stock-s-list"></div>',
    '<div class="stock-search-page">',
    '  <button type="button" data-s="prev">‹ 上一页</button>',
    '  <span id="stock-s-page"></span>',
    '  <button type="button" data-s="next">下一页 ›</button>',
    '</div>',
  ].join('');
  document.body.appendChild(wrap);

  const p = stockState.searchPanel = {
    overlay: wrap,
    engine: wrap.querySelector('#stock-search-engine'),
    q: wrap.querySelector('#stock-s-q'),
    symbol: wrap.querySelector('#stock-s-symbol'),
    from: wrap.querySelector('#stock-s-from'),
    to: wrap.querySelector('#stock-s-to'),
    size: wrap.querySelector('#stock-s-size'),
    kinds: wrap.querySelector('#stock-s-kinds'),
    meta: wrap.querySelector('#stock-s-meta'),
    list: wrap.querySelector('#stock-s-list'),
    page: wrap.querySelector('#stock-s-page'),
    open: false, pageNo: 1, total: 0, pages: 0, kind: '', seq: 0, onKey: null,
  };
  p.kinds.innerHTML = _stockKindChips(STOCK_SEARCH_KINDS, '');
  p.kinds.addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-kind]');
    if (!btn) return;
    p.kind = btn.dataset.kind;
    p.kinds.innerHTML = _stockKindChips(STOCK_SEARCH_KINDS, p.kind);
    stockSearchRun(1);
  });
  wrap.querySelector('.stock-ov-bar').addEventListener('click', (e) => {
    if (e.target.closest('button[data-s="close"]')
        || e.target.closest('button[data-s="back"]')) stockCloseSearch();
  });
  wrap.querySelector('.stock-search-page').addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-s]');
    if (!btn) return;
    if (btn.dataset.s === 'prev') stockSearchRun(p.pageNo - 1);
    else if (btn.dataset.s === 'next') stockSearchRun(p.pageNo + 1);
  });
  p.size.onchange = () => stockSearchRun(1);
  p.from.onchange = () => stockSearchRun(1);
  p.to.onchange = () => stockSearchRun(1);
  wrap.querySelector('#stock-s-go').onclick = () => stockSearchRun(1);
  p.q.onkeydown = (e) => { if (e.key === 'Enter' && !isImeComposing(e)) stockSearchRun(1); };
  p.symbol.onkeydown = (e) => { if (e.key === 'Enter' && !isImeComposing(e)) stockSearchRun(1); };
  // 检索 → 回看：点任意一条结果，直接打开时间轴回溯并定位到那一天
  p.list.addEventListener('click', (e) => {
    const card = e.target.closest('.stock-analysis[data-date]');
    if (!card) return;
    const date = card.dataset.date;
    stockCloseSearch();
    Promise.resolve(stockOpenHistory())
      .then(() => stockHistoryLoad(date))
      .catch(() => {});
  });
  return p;
}

async function stockOpenSearch() {
  if (!stockState.game) return;
  const p = stockState.searchPanel || stockBuildSearch();
  if (p.open) return;
  p.open = true;
  p.overlay.classList.remove('hidden');
  stockBindEsc(p, stockCloseSearch);
  try {
    const meta = await api('/api/hall/stock/analysis_kinds', {});
    if (p.open) p.engine.textContent = '检索引擎：' + (meta.engine || 'like');
  } catch (e) { /* 引擎信息拿不到不影响检索 */ }
  stockSearchRun(1);
}

async function stockSearchRun(pageNo) {
  const p = stockState.searchPanel;
  if (!p || !p.open || !stockState.game) return;
  const page = Math.max(1, Number(pageNo) || 1);
  const seq = ++p.seq;
  p.meta.textContent = '检索中…';
  try {
    const data = await api('/api/hall/stock/analyses', {
      game_id: stockState.game.id,
      q: p.q.value.trim(),
      kind: p.kind,
      symbol: p.symbol.value.trim(),
      date_from: p.from.value,
      date_to: p.to.value,
      page: page,
      size: Number(p.size.value) || 20,
    });
    if (seq !== p.seq || !p.open) return;
    p.pageNo = data.page || 1;
    p.total = data.total || 0;
    p.pages = data.pages || 0;
    // 百万级下精确命中数要遍历全部命中行（秒级），后端改为封顶计数，这里按「2 万+」展示
    const totalText = data.total_capped
      ? (Math.max(0, p.total - 1)).toLocaleString('zh-CN') + '+ 条'
      : p.total.toLocaleString('zh-CN') + ' 条';
    p.meta.textContent = '命中 ' + totalText + ' · 第 ' + p.pageNo + ' 页 · 耗时 '
      + (data.took_ms || 0) + ' ms · 引擎 ' + (data.engine || 'like');
    const items = data.items || [];
    if (!items.length) {
      p.list.innerHTML = '<div class="stock-empty">没有符合条件的分析记录</div>';
    } else {
      const nameOf = {};
      (stockState.game.players || []).forEach((x) => { nameOf[x.id] = x.name; });
      p.list.innerHTML = items.map((a) => (
        '<div class="stock-analysis" data-date="' + stockEsc(a.date) + '">'
        + '<div class="stock-analysis-top"><i>' + stockEsc(a.date) + '</i>'
        + '<em>' + stockEsc(a.kind) + '</em><b>'
        + stockEsc(a.player_name || nameOf[a.player_id] || a.player_id) + '</b>'
        + (a.symbols && a.symbols.length ? '<span class="stock-analysis-syms">'
          + a.symbols.map((s) => stockEsc(stockSymbolName(s))).join(' · ') + '</span>' : '')
        + '</div><div class="stock-analysis-text">' + stockEsc(a.text) + '</div>'
        + (a.keywords ? '<div class="stock-analysis-kw">'
          + stockEsc(a.keywords).split(',').filter(Boolean).map((k) => '<span>#'
            + stockEsc(k) + '</span>').join('') + '</div>' : '')
        + '<span class="stock-analysis-go">查看当天 →</span>'
        + '</div>'
      )).join('');
    }
    p.page.textContent = p.pageNo + ' / ' + Math.max(1, p.pages);
    const prev = p.overlay.querySelector('button[data-s="prev"]');
    const next = p.overlay.querySelector('button[data-s="next"]');
    if (prev) prev.disabled = p.pageNo <= 1;
    if (next) next.disabled = !data.has_more;      // 封顶计数时按「本页是否满」判断有无下一页
  } catch (e) {
    if (seq !== p.seq || !p.open) return;
    p.meta.textContent = '检索失败：' + (e.message || e);
    p.list.innerHTML = '';
  }
}

function stockCloseSearch() {
  const p = stockState.searchPanel;
  if (!p || !p.open) return;
  p.open = false;
  p.seq += 1;
  p.overlay.classList.add('hidden');
  stockUnbindEsc(p);
}

function stockDestroySearch() {
  const p = stockState.searchPanel;
  if (!p) return;
  stockCloseSearch();
  if (p.overlay.parentNode) p.overlay.parentNode.removeChild(p.overlay);
  stockState.searchPanel = null;
}

/* ---------------- 历史对比（最多 3 个节点 + 多屏联动） ---------------- */

const STOCK_COMPARE_MAX = 3;

function stockBuildCompare() {
  const wrap = stockOvEl('stock-compare');
  wrap.innerHTML = [
    '<div class="stock-ov-bar">',
    '  <button type="button" data-c="back">← 返回对局</button>',
    '  <b>历史对比</b>',
    '  <span class="stock-ov-dim">三屏各看一个时间点；参考线只在各屏画出同一天的竖线</span>',
    '  <span class="stock-ov-spacer"></span>',
    '  <label class="stock-ov-check"><input type="checkbox" id="stock-c-link" checked>'
      + ' 同步参考线</label>',
    '  <input type="range" id="stock-c-range" min="0" max="0" value="0">',
    '  <span class="stock-ov-info" id="stock-c-anchor"></span>',
    '  <button type="button" data-c="reset">重置节点</button>',
    '  <button type="button" data-c="close">✕ 关闭</button>',
    '</div>',
    '<div class="stock-compare-grid" id="stock-c-grid"></div>',
    '<div class="stock-ov-hint">每屏可单独改日期；开启「同步参考线」后，悬停任一屏或拖动滑杆，'
      + '三屏会同时标出同一天（虚线）。若某屏的时间点早于参考线，该屏不画线。</div>',
  ].join('');
  document.body.appendChild(wrap);

  const c = stockState.compare = {
    overlay: wrap,
    grid: wrap.querySelector('#stock-c-grid'),
    range: wrap.querySelector('#stock-c-range'),
    link: wrap.querySelector('#stock-c-link'),
    anchorLabel: wrap.querySelector('#stock-c-anchor'),
    open: false, dates: [], cols: [], timeline: [], anchor: '', seq: 0,
    onKey: null, raf: 0,
  };
  wrap.querySelector('.stock-ov-bar').addEventListener('click', (e) => {
    if (e.target.closest('button[data-c="close"]')
        || e.target.closest('button[data-c="back"]')) stockCloseCompare();
    else if (e.target.closest('button[data-c="reset"]')) stockCompareReset();
  });
  // 滑杆只移动「参考线」，不重新加载三屏 —— 否则三屏会变成同一天，对比就没意义了
  c.range.addEventListener('input', () => {
    const d = c.timeline[Number(c.range.value)];
    if (!d) return;
    c.anchor = null;
    stockCompareAnchor(d);
  });
  c.link.onchange = () => {
    c.anchor = null;
    stockCompareAnchor(c.link.checked
      ? (c.timeline[Number(c.range.value)] || '') : '');
  };
  return c;
}

/* 重置为默认的三个节点：最早 / 中间 / 最新 */
async function stockCompareReset() {
  const c = stockState.compare;
  if (!c || !c.open || c.timeline.length < 2) return;
  const n = c.timeline.length;
  const picks = n >= 3
    ? [c.timeline[0], c.timeline[Math.floor((n - 1) / 2)], c.timeline[n - 1]]
    : [c.timeline[0], c.timeline[1]];
  c.anchor = null;
  await stockCompareLoad(picks);
}

async function stockOpenCompare() {
  if (!stockState.game) return;
  const c = stockState.compare || stockBuildCompare();
  if (c.open) return;
  c.open = true;
  c.overlay.classList.remove('hidden');
  stockBindEsc(c, stockCloseCompare);
  try {
    const dates = await stockLoadTimeline(true);
    if (dates.length < 2) { stockCloseCompare(); toast('至少要有 2 个交易日才能对比'); return; }
    c.timeline = dates;
    c.range.min = '0';
    c.range.max = String(dates.length - 1);
    c.range.value = String(dates.length - 1);
    // 默认取「最早 / 中间 / 最新」三个节点
    const picks = dates.length >= 3
      ? [dates[0], dates[Math.floor((dates.length - 1) / 2)], dates[dates.length - 1]]
      : [dates[0], dates[1]];
    await stockCompareLoad(picks);
  } catch (e) {
    toast('对比加载失败：' + (e.message || e), 'error');
  }
}

async function stockCompareLoad(dates) {
  const c = stockState.compare;
  if (!c || !c.open) return;
  const g = stockState.game;
  const list = (dates || []).slice(0, STOCK_COMPARE_MAX);
  const seq = ++c.seq;
  c.grid.innerHTML = '<div class="stock-empty">正在加载对比数据…</div>';
  try {
    const data = await api('/api/hall/stock/compare', {
      game_id: g.id,
      dates: list,
      symbol: stockState.symbol || '',
    });
    if (seq !== c.seq || !c.open) return;
    c.dates = list;
    c.cols = data.nodes || [];
    stockCompareRender();
  } catch (e) {
    if (seq !== c.seq || !c.open) return;
    c.grid.innerHTML = '<div class="stock-empty">对比加载失败：' + stockEsc(e.message || e)
      + '</div>';
  }
}

function stockCompareRender() {
  const c = stockState.compare;
  if (!c || !c.cols.length) return;
  const nameOf = {};
  ((c.cols[0].players) || []).forEach((p) => { nameOf[p.id] = p.name; });
  c.grid.innerHTML = c.cols.map((node, i) => (
    '<div class="stock-cmp-col" data-col="' + i + '">'
    + '<div class="stock-cmp-head"><b>节点 ' + (i + 1) + '</b>'
    + '<input type="date" class="stock-cmp-date" data-col="' + i + '"'
    + ' title="只改这一屏的时间点" value="'
    + stockEsc(node.as_of) + '">'
    + '<span class="stock-ov-dim">看第 '
    + (Math.max(0, c.timeline.indexOf(node.as_of)) + 1) + ' / ' + c.timeline.length
    + ' 个交易日</span></div>'
    + '<div class="stock-cmp-chart"><svg class="stock-cmp-svg"></svg>'
    + '<div class="stock-tip hidden"></div></div>'
    + '<div class="stock-cmp-sec"><div class="stock-card-title">资产 / 持仓</div>'
    + ((node.players || []).map((p) => (
      '<div class="stock-hist-player"><div class="stock-hist-player-top"><b>'
      + stockEsc(p.name) + '</b><span class="' + (p.return_pct >= 0 ? 'up' : 'down') + '"'
      + ' title="总收益率（相对初始资金，含浮盈浮亏）">'
      + stockPct(p.return_pct) + '</span><em>' + stockMoney(p.equity) + '</em></div>'
      + '<div class="stock-hist-player-sub">现金 ' + stockMoney(p.cash) + ' · 市值 '
      + stockMoney(p.market_value) + '</div>'
      + ((p.positions || []).map((x) => '<div class="stock-pos-row"><b>'
        + stockEsc(stockSymbolName(x.symbol)) + '</b><em>' + stockQtyText(x.qty)
        + ' ' + stockUnit() + '</em>'
        + '<span class="' + (x.pnl_pct >= 0 ? 'up' : 'down') + '">' + stockPct(x.pnl_pct)
        + '</span></div>').join('') || '<div class="stock-empty small">空仓</div>')
      + '</div>'
    )).join('')) + '</div>'
    + '<div class="stock-cmp-sec"><div class="stock-card-title">AI 分析</div>'
    + ((node.analyses || []).slice(0, 4).map((a) => (
      '<div class="stock-analysis"><div class="stock-analysis-top"><i>' + stockEsc(a.date)
      + '</i><em>' + stockEsc(a.kind) + '</em><b>'
      + stockEsc(nameOf[a.player_id] || a.player_id) + '</b></div>'
      + '<div class="stock-analysis-text">' + stockEsc(a.text) + '</div></div>'
    )).join('') || '<div class="stock-empty small">该时刻还没有 AI 分析</div>') + '</div>'
    + '</div>'
  )).join('');

  c.grid.querySelectorAll('input.stock-cmp-date').forEach((inp) => {
    inp.onchange = () => {
      const idx = Number(inp.dataset.col);
      const d = inp.value;
      if (!d) return;
      // 改日期只影响这一屏，另外两屏保持各自的时间点
      stockCompareLoad(c.cols.map((n, i) => (i === idx ? d : n.as_of)));
    };
  });
  // 重新加载后参考线的日期不属于新节点区间，直接清掉，避免多画一轮
  c.anchor = '';
  c.anchorLabel.textContent = '';
  c.cols.forEach((node, i) => stockCompareDraw(i));
}

function stockCompareDraw(i) {
  const c = stockState.compare;
  const node = c && c.cols[i];
  if (!node) return;
  const col = c.grid.querySelector('.stock-cmp-col[data-col="' + i + '"]');
  if (!col) return;
  const stage = col.querySelector('.stock-cmp-chart');
  const svg = col.querySelector('.stock-cmp-svg');
  const tip = col.querySelector('.stock-tip');
  const w = Math.max(240, Math.round(stage.clientWidth) || 0);
  const hgt = Math.max(140, Math.round(stage.clientHeight) || 0);
  svg.setAttribute('viewBox', '0 0 ' + w + ' ' + hgt);
  const chart = node.chart || {};
  stockDrawKline({
    svg: svg, tip: tip, wrap: stage,
    bars: chart.bars || [], marks: chart.marks || [],
    geom: stockZoomGeom(w, hgt), emptyText: '该时刻暂无行情',
    anchorDate: c.anchor,
    onHoverDate: (d) => { if (c.link.checked) stockCompareAnchor(d); },
  });
}

/* 多屏联动：锚点日同步画到每一屏上（悬停 / 拖时间轴都会走到这里） */
function stockCompareAnchor(date) {
  const c = stockState.compare;
  if (!c || !c.open) return;
  if (c.anchor === date) return;
  c.anchor = date || '';
  c.anchorLabel.textContent = c.anchor ? '参考线 ' + c.anchor : '';
  if (c.raf) return;
  c.raf = requestAnimationFrame(() => {
    c.raf = 0;
    c.cols.forEach((_n, i) => stockCompareDraw(i));
  });
}

function stockCloseCompare() {
  const c = stockState.compare;
  if (!c || !c.open) return;
  c.open = false;
  c.seq += 1;
  if (c.raf) { cancelAnimationFrame(c.raf); c.raf = 0; }
  c.overlay.classList.add('hidden');
  stockUnbindEsc(c);
}

function stockDestroyCompare() {
  const c = stockState.compare;
  if (!c) return;
  stockCloseCompare();
  if (c.overlay.parentNode) c.overlay.parentNode.removeChild(c.overlay);
  stockState.compare = null;
}
