/* 轨迹：模型视角的事件账本（只读）
   ------------------------------------------------------------
   三栏控制台：轮次导航 / 事件流水 + 观测区 / 详情检查器。
   列表只回预览与尺寸，正文在选中时按需取；大文本分段渲染。
   「实时」只拉最新几轮并合并，不再整份重读。 */
(function () {
  'use strict';

  const KIND = {
    user: { label: '输入', cls: 'user' },
    context: { label: '上下文', cls: 'ctx' },
    system: { label: '系统', cls: 'sys' },
    reasoning: { label: '思考', cls: 'think' },
    message: { label: '正文', cls: 'msg' },
    tool: { label: '工具', cls: 'tool' },
    subtool: { label: '子代理', cls: 'sub' },
    compacted: { label: '压缩', cls: 'compact' },
    review: { label: '验收', cls: 'review' },
  };
  const KIND_ORDER = ['user', 'context', 'system', 'reasoning', 'message',
    'tool', 'subtool', 'compacted', 'review'];
  const REASON = { initial: '首次', resume: '进程重启', change: '内容变化' };
  const PAGE = 8;                  // 首屏 / 每次加载的轮数
  const FOLLOW_MS = 3000;
  const CHUNK = 60000;             // 单个文本面板首屏渲染的字符数
  const MAX_ROWS = 600;            // 单轮一次渲染的上限，超出给「显示更多」

  const T = {
    sid: '', turns: [], cells: [], byRid: {}, stats: {}, tools: [],
    series: [], marks: [], hasMore: false, totalTurns: 0, source: 'events',
    filter: 'all', tool: '', q: '', errorsOnly: false,
    selected: '', selectedTurn: 0, tab: 'overview',
    detail: {}, expand: {}, collapsed: {}, stepFold: {}, more: {},
    viz: 'waterfall', vizFold: false, follow: true,
    loading: false, opened: false, atBottom: true,
    timer: null, qTimer: null, revKey: '', sig: '', uid: 0,
  };

  // ---------------------------------------------------------------- 小工具

  function el(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  function num(n) {
    const v = Number(n) || 0;
    const a = Math.abs(v);
    if (a >= 1e8) return (v / 1e8).toFixed(2) + '亿';
    if (a >= 1e6) return (v / 1e6).toFixed(2) + 'M';
    if (a >= 1e4) return (v / 1e3).toFixed(1) + 'K';
    return v.toLocaleString('en-US');
  }
  function pct(a, b) {
    const x = Number(a) || 0;
    const y = Number(b) || 0;
    if (!y) return '—';
    return Math.round((x / y) * 100) + '%';
  }
  function durMs(v) {
    const n = Number(v) || 0;
    if (n >= 60000) return (n / 60000).toFixed(1) + 'm';
    if (n >= 1000) return (n / 1000).toFixed(n >= 10000 ? 0 : 1) + 's';
    if (n >= 10) return Math.round(n) + 'ms';
    return n.toFixed(1) + 'ms';
  }
  function size(n) {
    const v = Number(n) || 0;
    if (v >= 1048576) return (v / 1048576).toFixed(1) + 'MB';
    if (v >= 1024) return (v / 1024).toFixed(1) + 'KB';
    return v + 'B';
  }
  function chars(n) {
    const v = Number(n) || 0;
    return v >= 10000 ? (v / 1000).toFixed(1) + 'K 字' : v.toLocaleString('en-US') + ' 字';
  }
  function oneLine(s, n) {
    const t = String(s == null ? '' : s).replace(/\s+/g, ' ').trim();
    return t.length > n ? t.slice(0, n) + '…' : t;
  }
  function clock(ts) {
    const n = Number(ts) || 0;
    if (!n) return '—';
    const d = new Date(n);
    return d.toLocaleTimeString('zh-CN', { hour12: false });
  }
  function pad(n) {
    const s = String(Number(n) || 0);
    return s.length >= 5 ? s : '00000'.slice(s.length) + s;
  }
  function cid(c) { return String(c.recordId || ('c_' + c.index)); }
  function stepKey(turn, g) { return turn + ':' + (g && (g.step || g.title) || '0'); }
  function kindOf(c) { return KIND[c.kind] || { label: c.kind || '?', cls: 'ctx' }; }
  function copyText(text) {
    const t = String(text == null ? '' : text);
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(t).then(() => toast('已复制', 'success'))
        .catch(() => window.prompt('复制以下内容：', t));
    } else {
      window.prompt('复制以下内容：', t);
    }
  }
  function isOpen() {
    const m = el('trajectory-modal');
    return !!m && !m.classList.contains('hidden');
  }
  function cssEsc(s) {
    if (window.CSS && CSS.escape) return CSS.escape(String(s));
    return String(s).replace(/["\\]/g, '\\$&');
  }
  function markHtml(raw, q) {
    const text = String(raw == null ? '' : raw);
    const ql = String(q || '').trim().toLowerCase();
    if (!ql) return esc(text);
    const lower = text.toLowerCase();
    let i = 0;
    let out = '';
    while (i <= lower.length) {
      const j = lower.indexOf(ql, i);
      if (j < 0) break;
      out += esc(text.slice(i, j)) + '<mark>' + esc(text.slice(j, j + ql.length)) + '</mark>';
      i = j + ql.length;
    }
    return out + esc(text.slice(i));
  }

  // ---------------------------------------------------------------- 数据索引

  function indexCells() {
    const out = [];
    const map = {};
    (T.turns || []).forEach((t) => {
      (t.groups || []).forEach((g) => {
        (g.cells || []).forEach((c) => {
          const item = { c: c, t: t, g: g };
          out.push(item);
          map[cid(c)] = item;
        });
      });
    });
    T.cells = out;
    T.byRid = map;
  }

  function searchBlob(c) {
    return [c.text, c.toolName, c.model, c.provider, c.errorText,
      c.source && c.source.label, c.search].join(' ').toLowerCase();
  }

  function matchCell(c) {
    if (T.filter !== 'all' && String(c.kind) !== T.filter) return false;
    if (T.tool && String(c.toolName || '') !== T.tool) return false;
    if (T.errorsOnly && !c.isError) return false;
    const q = T.q.trim().toLowerCase();
    if (q && searchBlob(c).indexOf(q) < 0) return false;
    return true;
  }
  function filterActive() {
    return T.filter !== 'all' || !!T.tool || T.errorsOnly || !!T.q.trim();
  }
  function filteredItems() {
    return T.cells.filter((x) => matchCell(x.c));
  }
  function signature(view) {
    const st = view.stats || {};
    const turns = view.turns || [];
    const last = turns[turns.length - 1] || {};
    const gs = last.groups || [];
    const g = gs[gs.length - 1] || {};
    const cs = g.cells || [];
    const c = cs[cs.length - 1] || {};
    return [st.cells, st.errors, last.turn, g.title, cs.length, c.index,
      c.toolName, c.isError ? 1 : 0, (c.sizes || {}).output].join('|');
  }

  // ---------------------------------------------------------------- 读取

  async function fetchView(before, limit) {
    const body = { session_id: T.sid, limit: limit || PAGE };
    if (before) body.before = before;
    const r = await api('/api/trajectory', body);
    if (!r || !r.ok) throw new Error((r && r.error) || '读取轨迹失败');
    T.revKey = JSON.stringify(r.rev || null);
    return r.view || {};
  }

  function applyView(view, prepend) {
    T.stats = view.stats || {};
    T.tools = view.tools || [];
    T.series = view.series || [];
    T.marks = view.marks || [];
    T.source = view.source || 'events';
    T.totalTurns = view.total_turns || (view.turns || []).length;
    if (prepend) {
      const seen = {};
      T.turns.forEach((t) => { seen[t.turn] = 1; });
      const add = (view.turns || []).filter((t) => !seen[t.turn]);
      T.turns = add.concat(T.turns);
    } else {
      T.turns = view.turns || [];
      T.collapsed = {};
      T.stepFold = {};
    }
    T.hasMore = !!view.has_more;
    T.sig = signature(view);
    indexCells();
    if (!T.selected || !T.byRid[T.selected]) {
      T.selected = '';
      T.selectedTurn = 0;
    }
  }

  async function load(before, prepend) {
    if (T.loading) return;
    T.loading = true;
    const list = el('traj-list');
    const oldTop = list.scrollTop;
    const oldH = list.scrollHeight;
    try {
      const view = await fetchView(before, PAGE);
      applyView(view, !!prepend);
      render();
      if (prepend) list.scrollTop = oldTop + Math.max(0, list.scrollHeight - oldH);
      else list.scrollTop = list.scrollHeight;
    } catch (e) {
      toastErr('读取轨迹', e);
      if (!T.cells.length) {
        list.innerHTML = '<div class="traj-empty">读取失败：' + esc(e.message || e) + '</div>';
      }
    } finally {
      T.loading = false;
    }
  }

  async function quietRefresh() {
    if (T.loading || !T.opened || document.hidden || !T.sid || !T.follow) return;
    try {
      const rv = await api('/api/trajectory/rev', { session_id: T.sid });
      const key = JSON.stringify((rv && rv.rev) || null);
      if (!rv || !rv.ok || key === T.revKey) return;
      const view = await fetchView(0, 3);
      if (signature(view) === T.sig) return;
      const incoming = view.turns || [];
      const first = incoming.length ? Number(incoming[0].turn) : 0;
      const keep = first ? T.turns.filter((t) => Number(t.turn) < first) : T.turns;
      T.stats = view.stats || {};
      T.tools = view.tools || [];
      T.series = view.series || [];
      T.marks = view.marks || [];
      T.totalTurns = view.total_turns || T.totalTurns;
      T.turns = keep.concat(incoming);
      T.sig = signature(view);
      indexCells();
      const list = el('traj-list');
      const stick = list.scrollHeight - list.scrollTop - list.clientHeight < 80;
      render();
      if (stick) {
        list.scrollTop = list.scrollHeight;
        el('traj-newtip').classList.add('hidden');
      } else {
        el('traj-newtip').classList.remove('hidden');
      }
    } catch (e) { /* 跟随失败静默，下一轮再试 */ }
  }

  function startFollow() {
    stopFollow();
    if (!T.follow) return;
    T.timer = setInterval(() => { if (T.opened) quietRefresh(); }, FOLLOW_MS);
  }
  function stopFollow() {
    if (T.timer) clearInterval(T.timer);
    T.timer = null;
  }

  // ---------------------------------------------------------------- 顶部 HUD

  function renderSub() {
    const st = T.stats || {};
    const parts = [];
    if (T.source === 'session') parts.push('旧会话回退');
    if (T.sid) parts.push(T.sid.slice(0, 12) + '…');
    parts.push((st.turns || T.turns.length) + ' 轮');
    el('traj-sub').textContent = parts.join(' · ');
  }

  function renderHud() {
    const st = T.stats || {};
    const tk = st.tokens || {};
    const errs = Number(st.errors) || 0;
    const tiles = [
      ['TURNS', st.turns || 0, '轮'],
      ['STEPS', st.steps || 0, '步'],
      ['CELLS', st.cells || 0, '条'],
      ['TOOLS', st.tools || 0, '次'],
      ['ERRORS', errs, errs ? '需检查' : '无'],
      ['CTX PEAK', num(st.peakContext || 0), 'token'],
      ['OUTPUT', num(tk.completion || 0), 'token'],
      ['CACHE', pct(tk.cached, tk.prompt), num(tk.cached || 0) + ' 命中'],
      ['REASON', num(tk.reasoning || 0), '思考 token'],
    ];
    el('traj-hud').innerHTML = tiles.map((x, i) =>
      '<span class="traj-tile' + (i === 4 && errs ? ' warn' : '') + '">' +
      '<i>' + esc(x[0]) + '</i><b>' + esc(x[1]) + '</b><em>' + esc(x[2]) + '</em></span>'
    ).join('');
  }

  function renderFilters() {
    const bk = (T.stats && T.stats.by_kind) || {};
    const kinds = Object.keys(bk).sort((a, b) => {
      const ia = KIND_ORDER.indexOf(a);
      const ib = KIND_ORDER.indexOf(b);
      return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
    });
    const chip = (kind, label, n) =>
      '<button type="button" class="traj-chip' + (T.filter === kind ? ' on' : '') +
      '" data-kind="' + esc(kind) + '">' + esc(label) +
      (n != null ? '<i>' + num(n) + '</i>' : '') + '</button>';
    const box = el('traj-filters');
    box.innerHTML = chip('all', '全部', null) +
      kinds.map((k) => chip(k, (KIND[k] || {}).label || k, bk[k])).join('');
    box.querySelectorAll('.traj-chip').forEach((b) => {
      b.onclick = () => {
        T.filter = b.dataset.kind || 'all';
        renderFilters();
        renderList();
        updateFoot();
      };
    });
  }

  function renderToolSelect() {
    const sel = el('traj-tool');
    const list = T.tools || [];
    sel.innerHTML = '<option value="">全部工具</option>' + list.slice(0, 80).map((d) =>
      '<option value="' + esc(d.name) + '">' + esc(d.name) + ' (' + d.count + ')</option>'
    ).join('');
    if (T.tool && !list.some((d) => d.name === T.tool)) T.tool = '';
    sel.value = T.tool;
  }

  // ---------------------------------------------------------------- 左：轮次导航

  function renderRail() {
    const turns = T.turns || [];
    let peak = 1;
    turns.forEach((t) => { peak = Math.max(peak, Number((t.stats || {}).peakIn) || 0); });
    const rows = turns.map((t) => {
      const st = t.stats || {};
      const w = Math.round(Math.max(2, (Number(st.peakIn) || 0) / peak * 100));
      const on = Number(t.turn) === Number(T.selectedTurn);
      return '<button type="button" class="traj-rail-item' +
        (on ? ' on' : '') + (st.errors ? ' err' : '') +
        '" data-turn="' + Number(t.turn) + '" title="Turn ' + Number(t.turn) +
        ' · ' + (st.steps || 0) + ' 步 · ' + chars(st.outTok) + ' 输出">' +
        '<span class="tr-no">T' + Number(t.turn) + '</span>' +
        '<span class="tr-bar"><i style="width:' + w + '%"></i></span>' +
        '<span class="tr-meta">' + (st.steps || 0) + '步 · ' + (st.tools || 0) + '工具' +
        '</span>' +
        (st.errors ? '<span class="tr-err">' + st.errors + '</span>' : '') +
        '</button>';
    }).join('');
    el('traj-rail').innerHTML =
      '<div class="traj-rail-h"><span class="traj-cap">轮次</span><b>' +
      (T.totalTurns || turns.length) + '</b></div>' +
      (rows || '<div class="traj-empty">暂无轮次</div>') +
      (T.hasMore ? '<button type="button" class="ghost traj-rail-more" ' +
        'id="traj-rail-more">加载更早</button>' : '');
    const box = el('traj-rail');
    const on = box.querySelector('.traj-rail-item.on');
    if (on) {
      const bb = box.getBoundingClientRect();
      const rb = on.getBoundingClientRect();
      if (rb.top < bb.top + 24 || rb.bottom > bb.bottom - 8) {
        on.scrollIntoView({ block: 'nearest' });
      }
    }
  }

  // ---------------------------------------------------------------- 观测区

  function vizEmpty(text) {
    return '<div class="traj-empty">' + esc(text) + '</div>';
  }

  function vizWaterfall() {
    const turns = T.turns || [];
    if (!turns.length) return vizEmpty('还没有可绘制的耗时数据。');
    const rows = turns.map((t) => {
      const cells = [];
      (t.groups || []).forEach((g) => (g.cells || []).forEach((c) => cells.push(c)));
      let start = Number(t.start) || 0;
      if (!start) {
        const first = cells.find((c) => c.startedAt);
        start = first ? Number(first.startedAt) : 0;
      }
      let end = Number(t.end) || 0;
      cells.forEach((c) => {
        const at = Number(c.startedAt) || 0;
        const fin = Number(c.endAt) || (at + (Number(c.timeSeconds) || 0) * 1000);
        if (fin > end) end = fin;
      });
      if (!end) end = start + 1;
      const span = Math.max(1, end - start);
      const blocks = cells.map((c) => {
        const at = Number(c.startedAt);
        if (!at) return '';
        const fin = Number(c.endAt) || (at + (Number(c.timeSeconds) || 0) * 1000);
        const left = Math.max(0, Math.min(100, ((at - start) / span) * 100));
        const d = Math.max(0, fin - at);
        const w = Math.max(0.55, Math.min(100 - left, (d / span) * 100));
        const k = kindOf(c);
        return '<i class="wf-b k-' + k.cls + (c.isError ? ' err' : '') +
          '" style="left:' + left.toFixed(2) + '%;width:' + w.toFixed(2) + '%"' +
          ' data-rid="' + esc(cid(c)) + '" title="#' + Number(c.index) + ' ' +
          esc(k.label) + ' · ' + esc(oneLine(c.text, 70)) +
          (d ? ' · ' + durMs(d) : '') + '"></i>';
      }).join('');
      const st = t.stats || {};
      return '<div class="wf-row' + (Number(t.turn) === Number(T.selectedTurn) ? ' on' : '') +
        '" data-turn="' + Number(t.turn) + '">' +
        '<span class="wf-label">T' + Number(t.turn) + '</span>' +
        '<span class="wf-track">' + blocks + '</span>' +
        '<span class="wf-dur">' + durMs(st.durMs || span) + '</span></div>';
    }).join('');
    return '<div class="traj-wf">' + rows + '</div>' +
      '<div class="traj-viz-foot"><span>每行一轮 · 横轴为轮内时间</span>' +
      '<span>点击色块跳到记录</span></div>';
  }

  function vizTokens() {
    const s = T.series || [];
    if (s.length < 2) return vizEmpty('还没有 token 数据。');
    const W = 900;
    const H = 148;
    const P = 10;
    const lineH = 92;                // 上下文折线区
    const barH = 24;                 // 输出柱区（独立基线）
    const barTop = P + lineH + 10;
    let maxIn = 1;
    let maxOut = 1;
    s.forEach((d) => {
      maxIn = Math.max(maxIn, Number(d[2]) || 0);
      maxOut = Math.max(maxOut, Number(d[3]) || 0);
    });
    const x = (i) => P + (i / (s.length - 1)) * (W - P * 2);
    const yIn = (v) => P + (1 - Math.sqrt((Number(v) || 0) / maxIn)) * lineH;
    const path = s.map((d, i) => (i ? 'L' : 'M') + x(i).toFixed(1) + ',' +
      yIn(d[2]).toFixed(1)).join(' ');
    const bars = s.map((d, i) => {
      const h = Math.max(0.4, ((Number(d[3]) || 0) / maxOut) * barH);
      return '<rect class="tk-out" x="' + (x(i) - 1.1).toFixed(1) + '" y="' +
        (barTop + barH - h).toFixed(1) + '" width="2.2" height="' + h.toFixed(1) + '"/>';
    }).join('');
    const grid = [0, 0.5, 1].map((r) => {
      const y = P + r * lineH;
      const v = Math.round(maxIn * Math.pow(1 - r, 2));
      return '<line x1="' + P + '" y1="' + y.toFixed(1) + '" x2="' + (W - P) +
        '" y2="' + y.toFixed(1) + '" class="tk-grid"/>' +
        '<text x="' + (P + 2) + '" y="' + (y - 3).toFixed(1) +
        '" class="tk-axis" text-anchor="start">' + num(v) + '</text>';
    }).join('');
    return '<div class="traj-tk"><svg viewBox="0 0 ' + W + ' ' + H +
      '" role="img" aria-label="token 曲线">' + grid +
      '<line x1="' + P + '" y1="' + (barTop + barH) + '" x2="' + (W - P) +
      '" y2="' + (barTop + barH) + '" class="tk-grid"/>' +
      '<text x="' + (P + 2) + '" y="' + (barTop + barH - 3) +
      '" class="tk-axis">输出</text>' +
      '<path class="tk-line" d="' + path + '"/>' + bars + '</svg>' +
      '<div class="traj-viz-foot"><span>上下文峰值 ' + num(maxIn) +
      ' · 输出峰值 ' + num(maxOut) + ' · 纵轴为平方根刻度</span><span>' +
      s.length + ' 个采样点</span></div></div>';
  }

  function vizTools() {
    const list = (T.tools || []).slice(0, 10);
    if (!list.length) return vizEmpty('还没有工具调用。');
    const max = Math.max.apply(null, list.map((d) => d.count).concat([1]));
    const rows = list.map((d) => {
      const ok = Math.max(0, d.count - (d.errors || 0));
      const okW = (ok / max) * 100;
      const errW = ((d.errors || 0) / max) * 100;
      return '<button type="button" class="traj-toolrow" data-tool="' + esc(d.name) +
        '" title="筛选 ' + esc(d.name) + '">' +
        '<span class="tt-name">' + esc(d.name) + '</span>' +
        '<span class="tt-bar"><i class="ok" style="width:' + okW.toFixed(2) + '%"></i>' +
        (errW ? '<i class="err" style="width:' + errW.toFixed(2) + '%"></i>' : '') +
        '</span>' +
        '<span class="tt-num">' + d.count + '</span>' +
        '<span class="tt-sub">' + (d.errors ? d.errors + ' 失败 · ' : '') +
        durMs(d.avgMs) + ' 平均 · ' + size(d.chars) + '</span></button>';
    }).join('');
    return '<div class="traj-tools">' + rows + '</div>' +
      '<div class="traj-viz-foot"><span>按调用次数排序 · 红色为失败</span>' +
      '<span>点击按工具筛选</span></div>';
  }

  function vizMarks() {
    const list = (T.marks || []).slice().reverse();
    if (!list.length) return vizEmpty('没有压缩 / 验收 / 计划变更记录。');
    const map = {
      compact: ['压缩', '压缩上下文'],
      review: ['验收', '自动验收'],
      plan: ['计划', '计划更新'],
      error: ['中断', '轮次失败'],
    };
    const rows = list.map((m) => {
      const k = map[m.type] || [m.type, m.type];
      let desc = k[1];
      if (m.type === 'review') desc = '验收 ' + (m.verdict || '—');
      else if (m.type === 'plan') desc = '计划 v' + (m.rev || 1) + ' · ' +
        (m.done || 0) + '/' + (m.total || 0) + ' 完成';
      else if (m.type === 'error') desc = '轮次结束：' + (m.label || '异常');
      return '<button type="button" class="traj-mark m-' + esc(m.type) +
        '" data-turn="' + Number(m.turn || 0) + '">' +
        '<span class="mk-t">' + esc(k[0]) + '</span>' +
        '<span class="mk-d">' + esc(desc) + '</span>' +
        '<span class="mk-turn">T' + Number(m.turn || 0) + '</span>' +
        '<span class="mk-at">' + esc(clock(m.at)) + '</span></button>';
    }).join('');
    return '<div class="traj-marks">' + rows + '</div>' +
      '<div class="traj-viz-foot"><span>按时间倒序</span><span>点击跳到对应轮次</span></div>';
  }

  function renderViz() {
    const tabs = el('traj-viz-tabs');
    tabs.querySelectorAll('button').forEach((b) => {
      b.classList.toggle('on', b.dataset.viz === T.viz);
    });
    const body = el('traj-viz-body');
    el('traj-viz').classList.toggle('folded', T.vizFold);
    el('traj-viz-fold').textContent = T.vizFold ? '展开' : '收起';
    if (T.vizFold) { body.innerHTML = ''; return; }
    if (T.viz === 'tokens') body.innerHTML = vizTokens();
    else if (T.viz === 'tools') body.innerHTML = vizTools();
    else if (T.viz === 'marks') body.innerHTML = vizMarks();
    else body.innerHTML = vizWaterfall();
    el('traj-viz-note').textContent = T.viz === 'waterfall' ? 'WATERFALL'
      : T.viz === 'tokens' ? 'TOKENS' : T.viz === 'tools' ? 'TOOLS' : 'MARKS';
  }

  // ---------------------------------------------------------------- 事件流

  function isCollapsed(t) {
    const v = T.collapsed[t.turn];
    if (v != null) return !!v;
    return false;                    // 默认全部展开，单轮行数由 MAX_ROWS 兜底
  }
  function isStepFolded(t, g) {
    const v = T.stepFold[stepKey(t.turn, g)];
    return !!v;
  }
  function turnCellCount(t) {
    let n = 0;
    (t.groups || []).forEach((g) => { n += (g.cells || []).length; });
    return n;
  }

  function turnHead(t, collapsed) {
    const st = t.stats || {};
    const bits = [];
    if (st.steps) bits.push(st.steps + ' 步');
    bits.push((st.cells || 0) + ' 条');
    if (st.tools) bits.push(st.tools + ' 工具');
    if (st.peakIn || st.outTok) {
      bits.push(num(st.peakIn) + ' 上下文 · ' + num(st.outTok) + ' 输出');
    }
    if (st.errors) bits.push(st.errors + ' 失败');
    if (st.durMs != null) bits.push(durMs(st.durMs));
    return '<button type="button" class="traj-turn-h" data-turn="' + Number(t.turn) + '">' +
      '<span class="traj-caret">▾</span><b>Turn ' + Number(t.turn) + '</b>' +
      '<span class="traj-turn-meta">' + esc(bits.join(' · ')) + '</span>' +
      '<span class="traj-turn-start">' + esc(clock(t.start)) + '</span>' +
      (t.reason ? '<span class="traj-turn-reason' + (st.errors ? ' err' : '') + '">' +
        esc(t.reason) + '</span>' : '') + '</button>';
  }

  function groupHead(t, g) {
    let tools = 0;
    let errors = 0;
    let peak = 0;
    let out = 0;
    let secs = 0;
    (g.cells || []).forEach((c) => {
      if (c.kind === 'tool' || c.kind === 'subtool') tools += 1;
      if (c.isError) errors += 1;
      peak = Math.max(peak, Number(c.input) || 0);
      out += Number(c.output) || 0;
      secs += Number(c.timeSeconds) || 0;
    });
    const meta = [];
    if (tools) meta.push(tools + ' 工具');
    if (peak || out) meta.push(num(peak) + ' → ' + num(out));
    if (secs) meta.push(durMs(secs * 1000));
    if (errors) meta.push(errors + ' 失败');
    return '<div class="traj-group-h" data-turn="' + Number(t.turn) +
      '" data-step="' + esc(g.step || g.title || '') + '">' +
      '<span class="traj-caret">▾</span>' +
      '<span class="tg-title">' + esc(g.title || '') + '</span>' +
      '<span class="tg-meta">' + esc(meta.join(' · ')) + '</span>' +
      '<span class="tg-count">' + (g.cells || []).length + ' 条</span></div>';
  }

  function metaBits(c) {
    const out = [];
    if (c.timeSeconds != null && c.timeSeconds > 0) out.push(durMs(c.timeSeconds * 1000));
    if (c.input || c.output) out.push(num(c.input) + ' → ' + num(c.output));
    if (c.ttftMs != null) out.push('首包 ' + durMs(c.ttftMs));
    if (c.decodeTps != null) out.push(c.decodeTps + ' tok/s');
    if (c.cacheRead) out.push('缓存 ' + num(c.cacheRead));
    if (c.resultChars) out.push(chars(c.resultChars));
    else if (c.sizes && c.sizes.output) out.push(chars(c.sizes.output));
    if (c.messages) out.push(c.messages + ' 条消息');
    if (c.systemReason) out.push(REASON[c.systemReason] || c.systemReason);
    if (c.toolCount) out.push(c.toolCount + ' 个工具');
    if (c.model) out.push([c.provider, c.model].filter(Boolean).join('/'));
    if (c.batch) out.push(c.batch);
    return out;
  }

  function rowHtml(c) {
    const k = kindOf(c);
    const meta = metaBits(c);
    const flags = [];
    if (c.isError) flags.push('<span class="traj-flag err">失败</span>');
    if (c.write) flags.push('<span class="traj-flag">写入</span>');
    if (c.subagent) flags.push('<span class="traj-flag">子代理</span>');
    if (c.kind === 'system' && c.diff) flags.push('<span class="traj-flag">变化</span>');
    return '<div class="traj-row k-' + k.cls + (c.isError ? ' err' : '') +
      (T.selected === cid(c) ? ' on' : '') + '" role="button" tabindex="-1"' +
      ' data-rid="' + esc(cid(c)) + '">' +
      '<span class="traj-row-led"></span>' +
      '<span class="traj-no">#' + pad(c.index) + '</span>' +
      '<span class="traj-body">' +
      '<span class="traj-line1"><span class="traj-kind">' + esc(k.label) + '</span>' +
      '<span class="traj-title">' + markHtml(c.text || '（空）', T.q) + '</span></span>' +
      '<span class="traj-line2">' + esc(meta.join(' · ') || '—') + '</span>' +
      '</span>' +
      '<span class="traj-row-flags">' + flags.join('') + '</span></div>';
  }

  function renderTurnBody(t) {
    let out = '';
    let rows = 0;
    const total = turnCellCount(t);
    for (const g of t.groups || []) {
      if (!(g.cells || []).length) continue;
      out += groupHead(t, g);
      if (isStepFolded(t, g)) continue;
      const cap = T.more[t.turn] ? 5000 : MAX_ROWS;
      for (const c of g.cells) {
        if (rows >= cap) break;
        out += rowHtml(c);
        rows += 1;
      }
      if (rows >= cap) break;
    }
    if (rows < total && !T.more[t.turn]) {
      out += '<button type="button" class="ghost traj-more-rows" data-turn="' +
        Number(t.turn) + '">显示剩余 ' + (total - rows) + ' 条</button>';
    }
    return out;
  }

  function renderFiltered() {
    const items = filteredItems();
    if (!items.length) {
      return '<div class="traj-empty">没有匹配的记录，试试清空搜索或筛选。</div>';
    }
    const byTurn = new Map();
    items.forEach((x) => {
      if (!byTurn.has(x.t.turn)) byTurn.set(x.t.turn, []);
      byTurn.get(x.t.turn).push(x);
    });
    let out = '';
    byTurn.forEach((group, turn) => {
      out += '<section class="traj-turn"><div class="traj-turn-h static">' +
        '<b>Turn ' + Number(turn) + '</b><span class="traj-turn-meta">命中 ' +
        group.length + ' 条</span></div>';
      let lastGroup = null;
      group.forEach((x) => {
        if (lastGroup !== x.g) {
          out += '<div class="traj-group-h static">' + esc(x.g.title || '') + '</div>';
          lastGroup = x.g;
        }
        out += rowHtml(x.c);
      });
      out += '</section>';
    });
    return out;
  }

  function renderList(opts) {
    const list = el('traj-list');
    const top = opts && opts.keepScroll ? list.scrollTop : null;
    let html = '';
    if (T.hasMore && !filterActive()) {
      html += '<button type="button" class="ghost traj-loadmore" id="traj-more">加载更早</button>';
    }
    if (filterActive()) {
      html += renderFiltered();
    } else {
      (T.turns || []).forEach((t) => {
        const collapsed = isCollapsed(t);
        html += '<section class="traj-turn' + (collapsed ? ' collapsed' : '') +
          '" data-turn="' + Number(t.turn) + '">' +
          turnHead(t, collapsed) +
          '<div class="traj-turn-body">' + (collapsed ? '' : renderTurnBody(t)) +
          '</div></section>';
      });
      if (!T.turns.length) {
        html += '<div class="traj-empty">这个会话还没有轨迹记录。</div>';
      }
    }
    list.innerHTML = html;
    if (top != null) list.scrollTop = top;
    updateFoot();
  }

  function updateFoot() {
    const total = T.cells.length;
    const shown = filterActive() ? filteredItems().length : total;
    el('traj-hits').textContent = filterActive() ? shown + ' / ' + total : '';
    el('traj-search-clear').classList.toggle('hidden', !T.q);
    el('traj-foot-stat').textContent = '显示 ' + shown + ' / ' + total +
      ' 条 · ' + T.turns.length + ' 个已加载轮次 · 共 ' + (T.totalTurns || 0) + ' 轮';
  }

  // ---------------------------------------------------------------- 检查器

  function detailOf(cell) {
    return T.detail[cid(cell)] || null;
  }

  function inlineParts(cell) {
    return {
      input: cell.inputDetail || '',
      output: cell.outputDetail || cell.result || '',
      thinking: cell.thinkingDetail || '',
      schema: cell.schemaDetail || '',
      diff: cell.promptDiff || '',
    };
  }

  function partsOf(cell) {
    if (cell.inline) return inlineParts(cell);
    const d = detailOf(cell);
    return (d && d.parts) || {};
  }

  function tabsFor(cell) {
    const sz = cell.sizes || {};
    const p = partsOf(cell);
    const out = [{ id: 'overview', label: '概览' }];
    const isThinking = cell.kind === 'reasoning';
    const hasThink = !isThinking && (sz.thinking || (p.thinking || '').length);
    if ((sz.input || (p.input || '').length) && cell.kind !== 'system') {
      out.push({ id: 'input', label: cell.kind === 'user' ? '内容' : '输入' });
    }
    if (isThinking || hasThink) out.push({ id: 'thinking', label: '思考' });
    if (!isThinking && (sz.output || (p.output || '').length)) {
      out.push({ id: 'output', label: cell.kind === 'tool' ? '结果' : '输出' });
    }
    if (cell.kind === 'system') {
      out.push({ id: 'input', label: '系统提示' });
      if (sz.diff || p.diff || cell.diff) out.push({ id: 'diff', label: '差异' });
    }
    if (sz.schema || p.schema || (cell.kind === 'tool' && cell.schemaRef)) {
      out.push({ id: 'schema', label: 'Schema' });
    }
    out.push({ id: 'raw', label: '原始' });
    return out;
  }

  function paneText(cell, tab) {
    if (tab === 'raw') return JSON.stringify(cell, null, 2);
    const p = partsOf(cell);
    if (tab === 'input') return p.input || '';
    if (tab === 'output') return p.output || '';
    if (tab === 'thinking') return p.thinking || '';
    if (tab === 'diff') return p.diff || '';
    if (tab === 'schema') return p.schema || '';
    return '';
  }

  function partsLoaded(cell) {
    if (cell.inline) return true;
    const d = detailOf(cell);
    return !!(d && !d.loading);
  }

  async function ensureDetail(cell) {
    if (!cell || cell.inline) return;
    const rid = cid(cell);
    const cur = T.detail[rid];
    if (cur) {
      if (cur.loading) return cur.promise;
      return;
    }
    const job = (async () => {
      try {
        const r = await api('/api/trajectory/cell', {
          session_id: T.sid, refs: cell.refs || {}, kind: cell.kind || '',
          seg: cell.seg || null, schema_ref: cell.schemaRef || 0,
          tool_name: cell.toolName || '', trace_id: cell.traceId || '',
          record_id: rid,
        });
        T.detail[rid] = { parts: (r && r.parts) || {}, loaded: !!(r && r.ok) };
      } catch (e) {
        T.detail[rid] = { parts: {}, loaded: true, error: String(e.message || e) };
      }
    })();
    T.detail[rid] = { parts: {}, loading: true, promise: job };
    await job;
    if (T.selected === rid) renderInspector(cell);
  }

  function kvRow(k, v, copyValue) {
    return '<dt>' + esc(k) + '</dt><dd>' + esc(v == null ? '' : v) +
      (copyValue ? '<button type="button" data-copy="' + esc(copyValue) +
        '" title="复制">复制</button>' : '') + '</dd>';
  }

  function overviewHtml(cell) {
    const item = T.byRid[cid(cell)];
    const rows = [];
    if (item) rows.push(kvRow('位置', 'Turn ' + Number(item.t.turn) + ' · ' +
      (item.g.title || '')));
    if (cell.startedAt) rows.push(kvRow('记录时间', new Date(Number(cell.startedAt)).toLocaleString()));
    if (cell.timeSeconds != null) rows.push(kvRow('耗时', durMs(cell.timeSeconds * 1000)));
    if (cell.ttftMs != null) rows.push(kvRow('首包', durMs(cell.ttftMs)));
    if (cell.decodeTps != null) rows.push(kvRow('解码速度', cell.decodeTps + ' tok/s'));
    if (cell.input) rows.push(kvRow('输入 token', num(cell.input)));
    if (cell.output) rows.push(kvRow('输出 token', num(cell.output)));
    if (cell.think) rows.push(kvRow('推理 token', num(cell.think)));
    if (cell.cacheRead) rows.push(kvRow('缓存命中', num(cell.cacheRead) + ' · ' +
      pct(cell.cacheRead, cell.input)));
    if (cell.cacheWrite) rows.push(kvRow('缓存写入', num(cell.cacheWrite)));
    if (cell.toolName) rows.push(kvRow('工具', cell.toolName, cell.toolName));
    if (cell.model) rows.push(kvRow('模型', [cell.provider, cell.model].filter(Boolean).join(' / ')));
    if (cell.toolCount) rows.push(kvRow('工具数', cell.toolCount + ' 个'));
    if (cell.systemReason) rows.push(kvRow('系统提示', REASON[cell.systemReason] || cell.systemReason));
    if (cell.callId) rows.push(kvRow('callId', cell.callId, cell.callId));
    if (cell.traceId) rows.push(kvRow('traceId', cell.traceId, cell.traceId));
    if (cell.source && cell.source.label) rows.push(kvRow('来源', cell.source.label));
    if (cell.batch) rows.push(kvRow('批次', cell.batch, cell.batch));
    if (cell.messages != null) rows.push(kvRow('子代理消息', cell.messages + ' 条'));
    const sz = cell.sizes || {};
    if (sz.input) rows.push(kvRow('输入大小', chars(sz.input)));
    if (sz.output) rows.push(kvRow('输出大小', chars(sz.output)));
    if (sz.thinking) rows.push(kvRow('思考大小', chars(sz.thinking)));
    if (sz.schema) rows.push(kvRow('Schema 大小', chars(sz.schema)));
    if (cell.isError) rows.push(kvRow('失败原因', cell.errorText || '（未记录）'));
    if (cell.write) rows.push(kvRow('写入文件', '是'));

    let html = '<dl class="traj-kv">' + rows.join('') + '</dl>';
    if (cell.parts && cell.parts.length) {
      html += '<div class="traj-sub-h">系统提示分段 · ' + cell.parts.length + '</div>' +
        '<div class="traj-parts">' + cell.parts.map((p) =>
          '<button type="button" class="traj-part' + (p.changed ? ' changed' : '') +
          '" data-rid="' + esc(p.recordId || '') + '"' +
          (p.recordId ? '' : ' disabled') + '>' +
          '<span class="tp-label">' + esc(p.label || '段落') + '</span>' +
          '<span class="tp-meta">' + chars(p.chars || 0) +
          (p.changed ? ' · 有变化' : '') + '</span></button>').join('') +
        '</div>';
    }
    if (cell.errorText && !cell.isError) {
      html += '<div class="traj-sub-h">错误信息</div><pre class="traj-pre">' +
        esc(cell.errorText) + '</pre>';
    }
    return html;
  }

  function jsonPretty(text) {
    try {
      return JSON.stringify(JSON.parse(text), null, 2);
    } catch (e) {
      return text;
    }
  }

  function textHtml(text, tab) {
    let raw = String(text == null ? '' : text);
    if (tab === 'diff') {
      return raw.split('\n').map((line) => {
        const cls = line.startsWith('+++') || line.startsWith('---') ? 'df-h'
          : line.startsWith('@@') ? 'df-at'
          : line.startsWith('+') ? 'df-add'
          : line.startsWith('-') ? 'df-del' : '';
        return '<span class="' + cls + '">' + esc(line) + '</span>';
      }).join('\n');
    }
    if (tab === 'input' || tab === 'schema') raw = jsonPretty(raw);
    const safe = markHtml(raw, T.q);
    if (tab !== 'input' && tab !== 'schema') return safe;
    return safe.replace(/(&quot;(?:[^&]|&(?!quot;))*?&quot;)(\s*:)/g,
      '<span class="tj-k">$1</span>$2');
  }

  function textPane(cell, tab, label, text) {
    const rid = cid(cell);
    const key = rid + ':' + tab;
    const body = String(text || '');
    const long = body.length > CHUNK && !T.expand[key];
    const shown = long ? body.slice(0, CHUNK) : body;
    return '<div class="traj-pane-bar"><span>' + esc(label) + ' · ' +
      chars(body.length) + '</span><span class="sp"></span>' +
      '<button type="button" data-act="wrap">换行：' + (T.wrap ? '开' : '关') + '</button>' +
      '<button type="button" data-act="copy-pane">复制</button></div>' +
      '<pre class="traj-pre' + (T.wrap ? '' : ' nowrap') + '">' +
      textHtml(shown, tab) + '</pre>' +
      (long ? '<button type="button" class="ghost traj-expand" data-act="expand" ' +
        'data-tab="' + esc(tab) + '">加载剩余 ' + chars(body.length - CHUNK) + '</button>' : '');
  }

  function renderInspector(cell) {
    const box = el('traj-inspector');
    if (!cell) {
      T.selected = '';
      box.innerHTML = '<div class="traj-empty">选择一条记录查看输入 / 输出 / 思考 / ' +
        '耗时 / Schema。<br><span class="traj-dim">j k 移动 · n 跳到失败 · 1-7 切换详情页</span></div>';
      return;
    }
    const rid = cid(cell);
    const k = kindOf(cell);
    const tabs = tabsFor(cell);
    if (!tabs.some((t) => t.id === T.tab)) T.tab = 'overview';
    const active = T.tab;
    let pane;
    if (active === 'overview') {
      pane = overviewHtml(cell);
    } else if (!partsLoaded(cell)) {
      ensureDetail(cell);
      pane = '<div class="traj-empty">读取中…</div>';
    } else {
      const text = paneText(cell, active);
      pane = text
        ? textPane(cell, active, (tabs.find((t) => t.id === active) || {}).label || active, text)
        : '<div class="traj-empty">' + (active === 'schema'
          ? '这条记录没有 schema 快照（旧日志，或工具名与请求头里的定义不一致）。'
          : '这一页没有内容。') + '</div>';
    }
    box.innerHTML =
      '<div class="traj-ins-head">' +
      '<span class="traj-ins-kind k-' + k.cls + (cell.isError ? ' err' : '') + '">' +
      esc(k.label) + '</span><b>#' + pad(cell.index) + '</b>' +
      '<span class="traj-ins-id" title="' + esc(rid) + '">' + esc(rid) + '</span>' +
      '<div class="traj-ins-actions">' +
      '<button type="button" data-act="copy">复制</button>' +
      '<button type="button" data-act="json">JSON</button></div></div>' +
      '<div class="traj-ins-meta">' + metaBits(cell).map((x) =>
        '<span class="traj-tag">' + esc(x) + '</span>').join('') +
      (cell.isError ? '<span class="traj-tag traj-err">失败</span>' : '') + '</div>' +
      '<div class="traj-tabbar">' + tabs.map((t, i) =>
        '<button type="button" class="traj-tab' + (t.id === active ? ' on' : '') +
        '" data-tab="' + t.id + '" title="快捷键 ' + (i + 1) + '">' + esc(t.label) +
        '</button>').join('') + '</div>' +
      '<div class="traj-pane">' + pane + '</div>';

    box.onclick = (e) => {
      const b = e.target.closest('button');
      if (!b) return;
      if (b.dataset.copy != null) { copyText(b.dataset.copy); return; }
      if (b.dataset.tab) { T.tab = b.dataset.tab; renderInspector(cell); return; }
      if (b.dataset.rid) { select(b.dataset.rid); return; }
      const act = b.dataset.act;
      if (act === 'copy') copyText(cell.text || '');
      else if (act === 'json') copyText(JSON.stringify(cell, null, 2));
      else if (act === 'copy-pane') copyText(paneText(cell, active));
      else if (act === 'wrap') { T.wrap = !T.wrap; renderInspector(cell); }
      else if (act === 'expand') {
        T.expand[rid + ':' + b.dataset.tab] = true;
        renderInspector(cell);
      }
    };
  }

  // ---------------------------------------------------------------- 选中

  function select(rid, opts) {
    const item = T.byRid[rid];
    if (!item) return;
    T.selected = rid;
    T.selectedTurn = Number(item.t.turn);
    let needRender = false;
    if (isCollapsed(item.t)) { T.collapsed[item.t.turn] = false; needRender = true; }
    if (isStepFolded(item.t, item.g)) { T.stepFold[stepKey(item.t.turn, item.g)] = false; needRender = true; }
    if (filterActive() && !matchCell(item.c)) {
      T.q = '';
      T.tool = '';
      T.filter = 'all';
      T.errorsOnly = false;
      el('traj-search').value = '';
      el('traj-errors').checked = false;
      renderFilters();
      renderToolSelect();
      needRender = true;
    }
    if (needRender) {
      renderList();
      renderRail();
      renderViz();
    } else {
      el('traj-list').querySelectorAll('.traj-row.on').forEach((r) => r.classList.remove('on'));
      const row = el('traj-list').querySelector('.traj-row[data-rid="' + cssEsc(rid) + '"]');
      if (row) row.classList.add('on');
      renderRail();
    }
    const row = el('traj-list').querySelector('.traj-row[data-rid="' + cssEsc(rid) + '"]');
    if (row && !(opts && opts.noScroll)) {
      const list = el('traj-list');
      const rb = row.getBoundingClientRect();
      const lb = list.getBoundingClientRect();
      if (rb.top < lb.top + 30 || rb.bottom > lb.bottom - 10) {
        row.scrollIntoView({ block: 'center' });
      }
    }
    // 鼠标点过之后把焦点交给列表，j / k 才不会还被搜索框吞掉
    try { (row || el('traj-list')).focus({ preventScroll: true }); } catch (e) {}
    renderInspector(item.c);
    if (!partsLoaded(item.c) && T.tab !== 'overview' && T.tab !== 'raw') {
      ensureDetail(item.c);
    }
  }

  function gotoTurn(n) {
    const t = (T.turns || []).find((x) => Number(x.turn) === Number(n));
    if (!t) return;
    T.collapsed[t.turn] = false;
    const first = t.groups && t.groups[0] && t.groups[0].cells[0];
    renderList();
    renderRail();
    renderViz();
    if (first) select(cid(first), { noScroll: true });
    const sec = el('traj-list').querySelector('.traj-turn[data-turn="' + Number(n) + '"]');
    if (sec) sec.scrollIntoView({ block: 'start' });
  }

  function visibleCells() {
    return filterActive() ? filteredItems().map((x) => x.c) : T.cells.map((x) => x.c);
  }

  function step(dir) {
    const list = visibleCells();
    if (!list.length) return;
    let i = list.findIndex((c) => cid(c) === T.selected);
    if (i < 0) i = dir > 0 ? -1 : list.length;
    const next = list[(i + dir + list.length) % list.length];
    if (next) select(cid(next));
  }

  function jumpFail(dir) {
    const errs = visibleCells().filter((c) => c.isError);
    if (!errs.length) { toast('当前筛选没有失败记录'); return; }
    let i = errs.findIndex((c) => cid(c) === T.selected);
    if (i < 0) i = dir > 0 ? -1 : 0;
    const next = errs[(i + dir + errs.length) % errs.length];
    if (next) select(cid(next));
  }

  // ---------------------------------------------------------------- 渲染总入口

  function render() {
    indexCells();
    renderSub();
    renderHud();
    renderFilters();
    renderToolSelect();
    renderRail();
    renderViz();
    renderList();
    renderInspector(T.selected ? (T.byRid[T.selected] || {}).c : null);
  }

  // ---------------------------------------------------------------- 打开 / 关闭

  async function open(rid) {
    const sid = (state.sessions && state.sessions.current) || '';
    if (!sid) { toast('先打开一个会话'); return; }
    T.sid = sid;
    T.turns = [];
    T.cells = [];
    T.byRid = {};
    T.stats = {};
    T.tools = [];
    T.series = [];
    T.marks = [];
    T.selected = '';
    T.selectedTurn = 0;
    T.detail = {};
    T.expand = {};
    T.collapsed = {};
    T.stepFold = {};
    T.more = {};
    T.filter = 'all';
    T.tool = '';
    T.q = '';
    T.errorsOnly = false;
    T.tab = 'overview';
    T.viz = 'waterfall';
    T.vizFold = false;
    T.opened = true;
    T.revKey = '';
    el('traj-search').value = '';
    el('traj-errors').checked = false;
    el('trajectory-modal').classList.remove('hidden');
    if (typeof gmPageIn === 'function') {
      const card = el('trajectory-modal').querySelector('.traj-shell');
      gmPageIn(card || el('trajectory-modal'));
    }
    document.body.classList.add('traj-locked');
    el('traj-list').innerHTML = '<div class="traj-empty">读取中…</div>';
    el('traj-inspector').innerHTML = '<div class="traj-empty">读取中…</div>';
    el('traj-hud').innerHTML = '';
    el('traj-rail').innerHTML = '';
    el('traj-viz-body').innerHTML = '';
    await load(0, false);
    if (rid) select(rid, { noScroll: true });
    try { el('traj-list').focus({ preventScroll: true }); } catch (e) {}
    startFollow();
  }

  function close() {
    stopFollow();
    T.opened = false;
    el('trajectory-modal').classList.add('hidden');
    document.body.classList.remove('traj-locked');
    hideExportMenu();
  }

  // ---------------------------------------------------------------- 导出

  function buildMarkdown() {
    const lines = ['# 轨迹 · ' + T.sid, ''];
    const sub = el('traj-sub').textContent || '';
    if (sub) lines.push('> ' + sub, '');
    const items = filterActive() ? filteredItems() : T.cells;
    let lastTurn = null;
    items.forEach((x) => {
      if (lastTurn !== x.t.turn) {
        lines.push('## Turn ' + Number(x.t.turn));
        lastTurn = x.t.turn;
      }
      const c = x.c;
      lines.push('- **' + kindOf(c).label + '** #' + c.index + ' ' +
        oneLine(c.text || '', 240));
      const meta = metaBits(c);
      if (meta.length) lines.push('  - ' + meta.join(' · '));
      if (c.isError && c.errorText) lines.push('  - 错误：' + oneLine(c.errorText, 300));
    });
    return lines.join('\n');
  }

  function download(text, name, type) {
    const blob = new Blob([text], { type: type || 'text/plain;charset=utf-8' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 4000);
  }

  async function exportJson() {
    try {
      const r = await api('/api/trajectory/export', { session_id: T.sid });
      if (!r || !r.ok) throw new Error((r && r.error) || '导出失败');
      download(JSON.stringify(r.data, null, 2),
        'trajectory-' + (T.sid || 'session') + '.json', 'application/json');
    } catch (e) {
      toastErr('导出轨迹', e);
    }
  }

  function hideExportMenu() {
    el('traj-export-menu').classList.add('hidden');
  }
  function toggleExportMenu() {
    el('traj-export-menu').classList.toggle('hidden');
  }

  // ---------------------------------------------------------------- 键盘

  function onKey(e) {
    if (!isOpen() || e.ctrlKey || e.metaKey || e.altKey) return;
    const tag = (e.target.tagName || '').toLowerCase();
    const typing = tag === 'input' || tag === 'select' || tag === 'textarea';
    if (e.key === 'Escape') {
      e.stopPropagation();           // 弹窗打开时独占 Esc，避免被全局浮层逻辑抢走
      if (!el('traj-export-menu').classList.contains('hidden')) { hideExportMenu(); return; }
      const box = el('traj-search');
      if (box.value) {               // 先清搜索，再按一次才关闭
        box.value = '';
        T.q = '';
        renderList();
        updateFoot();
        return;
      }
      close();
      return;
    }
    if (typing) return;
    if (e.key === '/') {
      e.preventDefault();
      el('traj-search').focus();
      return;
    }
    if (e.key === 'ArrowDown' || e.key === 'j') { e.preventDefault(); step(1); }
    else if (e.key === 'ArrowUp' || e.key === 'k') { e.preventDefault(); step(-1); }
    else if (e.key === 'n') { e.preventDefault(); jumpFail(1); }
    else if (e.key === 'p') { e.preventDefault(); jumpFail(-1); }
    else if (e.key === 'g') {
      e.preventDefault();
      const r = visibleCells()[0];
      if (r) select(cid(r));
    } else if (e.key === 'G') {
      e.preventDefault();
      const rs = visibleCells();
      if (rs.length) select(cid(rs[rs.length - 1]));
    } else if (e.key === '[' || e.key === ']') {
      e.preventDefault();
      const i = T.turns.findIndex((t) => Number(t.turn) === Number(T.selectedTurn));
      const next = T.turns[Math.max(0, Math.min(T.turns.length - 1, i + (e.key === ']' ? 1 : -1)))];
      if (next) gotoTurn(next.turn);
    } else if (/^[1-7]$/.test(e.key)) {
      const cell = T.selected && T.byRid[T.selected];
      if (!cell) return;
      const tabs = tabsFor(cell.c);
      const tab = tabs[Number(e.key) - 1];
      if (tab) { T.tab = tab.id; renderInspector(cell.c); }
    }
  }

  // ---------------------------------------------------------------- 绑定

  function bind() {
    el('traj-close').onclick = close;
    el('traj-refresh').onclick = () => { if (T.sid) load(0, false); };
    el('traj-follow').onchange = (e) => {
      T.follow = !!e.target.checked;
      if (T.follow) startFollow(); else stopFollow();
    };

    el('traj-export').onclick = (e) => { e.stopPropagation(); toggleExportMenu(); };
    el('traj-export-menu').onclick = (e) => {
      const b = e.target.closest('button');
      if (!b) return;
      hideExportMenu();
      const fmt = b.dataset.fmt;
      if (fmt === 'json') exportJson();
      else if (fmt === 'md') {
        download(buildMarkdown(), 'trajectory-' + (T.sid || 'session') + '.md', 'text/markdown;charset=utf-8');
      } else if (fmt === 'copy') {
        copyText(buildMarkdown());
      }
    };
    document.addEventListener('click', (e) => {
      if (!isOpen()) return;
      if (!e.target.closest('.traj-menu')) hideExportMenu();
    });

    const search = el('traj-search');
    search.oninput = () => {
      clearTimeout(T.qTimer);
      T.qTimer = setTimeout(() => {
        T.q = search.value || '';
        renderList();
        updateFoot();
      }, 120);
    };
    el('traj-search-clear').onclick = () => {
      search.value = '';
      T.q = '';
      renderList();
      updateFoot();
      search.focus();
    };

    el('traj-tool').onchange = (e) => {
      T.tool = e.target.value || '';
      renderList();
      updateFoot();
    };
    el('traj-errors').onchange = (e) => {
      T.errorsOnly = !!e.target.checked;
      renderList();
      updateFoot();
    };
    el('traj-fold').onclick = () => {
      const anyOpen = T.turns.some((t) => !isCollapsed(t));
      T.turns.forEach((t) => { T.collapsed[t.turn] = anyOpen; });
      renderList();
      el('traj-fold').textContent = anyOpen ? '展开全部' : '折叠全部';
    };

    el('traj-viz-tabs').onclick = (e) => {
      const b = e.target.closest('button[data-viz]');
      if (!b) return;
      T.viz = b.dataset.viz;
      T.vizFold = false;
      renderViz();
    };
    el('traj-viz-fold').onclick = () => {
      T.vizFold = !T.vizFold;
      renderViz();
    };
    el('traj-viz-body').onclick = (e) => {
      const tool = e.target.closest('[data-tool]');
      if (tool) {
        T.tool = tool.dataset.tool || '';
        renderToolSelect();
        renderList();
        updateFoot();
        return;
      }
      const rid = e.target.closest('[data-rid]');
      if (rid) { select(rid.dataset.rid); return; }
      const turn = e.target.closest('[data-turn]');
      if (turn) gotoTurn(turn.dataset.turn);
    };

    el('traj-rail').onclick = (e) => {
      if (e.target.closest('#traj-rail-more')) {
        const first = T.turns.length ? T.turns[0].turn : 0;
        if (first) load(first, true);
        return;
      }
      const b = e.target.closest('[data-turn]');
      if (b) gotoTurn(b.dataset.turn);
    };

    el('traj-newtip').onclick = () => {
      const list = el('traj-list');
      list.scrollTop = list.scrollHeight;
      el('traj-newtip').classList.add('hidden');
    };

    const list = el('traj-list');
    list.onscroll = () => {
      T.atBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 80;
      if (T.atBottom) el('traj-newtip').classList.add('hidden');
    };
    list.onclick = (e) => {
      if (e.target.closest('.traj-loadmore')) {
        const first = T.turns.length ? T.turns[0].turn : 0;
        if (first) load(first, true);
        return;
      }
      const more = e.target.closest('.traj-more-rows');
      if (more) {
        T.more[more.dataset.turn] = true;
        renderList();
        return;
      }
      const head = e.target.closest('.traj-turn-h');
      if (head && !head.classList.contains('static')) {
        const sec = head.closest('.traj-turn');
        const turn = Number(sec.dataset.turn);
        const now = isCollapsed(T.turns.find((t) => Number(t.turn) === turn));
        T.collapsed[turn] = !now;
        renderList();
        return;
      }
      const gh = e.target.closest('.traj-group-h');
      if (gh && !gh.classList.contains('static')) {
        const turn = gh.dataset.turn;
        const step = gh.dataset.step;
        const key = turn + ':' + step;
        T.stepFold[key] = !T.stepFold[key];
        renderList();
        return;
      }
      const row = e.target.closest('.traj-row');
      if (row) select(row.dataset.rid);
    };

    const modal = el('trajectory-modal');
    modal.addEventListener('click', (e) => { if (e.target === modal) close(); });
    // window 捕获：弹窗打开时先于页面其它快捷键拿到按键
    window.addEventListener('keydown', onKey, true);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bind);
  } else {
    bind();
  }
  window.openTrajectory = open;
})();
