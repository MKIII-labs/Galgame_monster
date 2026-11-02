/* 轨迹：模型视角的事件账本（只读） */
(function () {
  'use strict';

  const KIND_LABEL = {
    user: '用户', context: '上下文', system: '系统', reasoning: '思考',
    message: '正文', tool: '工具', subtool: '子代理', compacted: '压缩',
  };
  const KIND_ORDER = ['user', 'context', 'system', 'reasoning', 'message',
    'tool', 'subtool', 'compacted'];
  const FOLLOW_MS = 2500;

  const T = {
    sid: '', turns: [], cells: [], stats: null, hasMore: false, source: 'events',
    filter: 'all', tool: '', q: '', errorsOnly: false,
    selectedRid: '', detail: {}, loading: false, opened: false,
    follow: true, wrap: true, tab: 'overview',
    timer: null, sig: '', qTimer: null, rev: '',
  };

  function el(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  function num(n) {
    const v = Number(n) || 0;
    return v >= 10000 ? (v / 1000).toFixed(1) + 'K' : String(v);
  }
  function ms(v) {
    if (v == null || !isFinite(v)) return '';
    return v >= 1000 ? (v / 1000).toFixed(1) + 's' : Math.round(v) + 'ms';
  }
  function oneLine(s, n) {
    const t = String(s == null ? '' : s).replace(/\s+/g, ' ').trim();
    return t.length > n ? t.slice(0, n) + '…' : t;
  }
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

  // ---------------------------------------------------------------- 数据索引

  function indexCells() {
    const out = [];
    (T.turns || []).forEach((t) => {
      (t.groups || []).forEach((g) => {
        (g.cells || []).forEach((c) => out.push({ c: c, turn: t, group: g }));
      });
    });
    T.cells = out;
    return out;
  }
  function cellByRid(rid) {
    if (!rid) return null;
    const hit = T.cells.find((x) => cid(x.c) === rid);
    return hit ? hit.c : null;
  }
  function itemByRid(rid) {
    return T.cells.find((x) => cid(x.c) === rid) || null;
  }
  function cid(c) {
    return c.recordId || ('idx_' + c.index);
  }
  function metaBits(c) {
    const out = [];
    if (c.timeSeconds != null) out.push(ms(c.timeSeconds * 1000));
    if (c.input || c.output) out.push(num(c.input) + '→' + num(c.output));
    if (c.ttftMs != null) out.push('首包 ' + ms(c.ttftMs));
    if (c.decodeTps != null) out.push(c.decodeTps + ' tok/s');
    if (c.resultChars) out.push('输出 ' + num(c.resultChars) + ' 字');
    if (c.messages) out.push(c.messages + ' 条消息');
    if (c.toolName) out.push(c.toolName);
    return out;
  }
  function searchBlob(c) {
    return [c.text, c.toolName, c.model, c.provider,
      c.source && c.source.label, c.errorText,
      String(c.inputDetail || '').slice(0, 1200),
      String(c.outputDetail || '').slice(0, 1200)].join(' ').toLowerCase();
  }
  function signature(v) {
    const st = v.stats || {};
    const turns = v.turns || [];
    const lt = turns[turns.length - 1] || {};
    const gs = lt.groups || [];
    const g = gs[gs.length - 1] || {};
    const cs = g.cells || [];
    const c = cs[cs.length - 1] || {};
    return [st.cells, st.errors, turns.length, gs.length, cs.length,
      c.recordId, oneLine(c.text, 200).length,
      String(c.outputDetail || '').length].join('|');
  }

  // ---------------------------------------------------------------- 读取

  async function fetchView(before) {
    const body = { session_id: T.sid, limit: 30 };
    if (before) body.before = before;
    const r = await api('/api/trajectory', body);
    if (!r || !r.ok) throw new Error((r && r.error) || '读取轨迹失败');
    T.rev = JSON.stringify(r.rev || null);
    return r.view || {};
  }

  async function load(before, prepend) {
    if (T.loading) return;
    T.loading = true;
    const list = el('traj-list');
    const oldTop = list.scrollTop;
    const oldHeight = list.scrollHeight;
    try {
      const view = await fetchView(before);
      T.stats = view.stats || {};
      T.source = view.source || 'events';
      T.hasMore = !!view.has_more;
      if (prepend) {
        T.turns = (view.turns || []).concat(T.turns);
        filterLoadedTurns();
      } else {
        T.turns = view.turns || [];
      }
      T.sig = signature(view);
      render();
      if (prepend) {
        list.scrollTop = oldTop + Math.max(0, list.scrollHeight - oldHeight);
      } else if (oldTop) {
        list.scrollTop = oldTop;
      }
    } catch (e) {
      toastErr('读取轨迹', e);
      if (!T.cells.length) {
        list.innerHTML = '<div class="traj-empty">读取失败：' + esc(e.message || e) + '</div>';
      }
    } finally {
      T.loading = false;
    }
  }
  // 旧数据里可能已经有同样的轮次，避免分页拼接出重复卡片
  function filterLoadedTurns() {
    const seen = {};
    T.turns = T.turns.filter((t) => {
      const k = String(t.turn);
      if (seen[k]) return false;
      seen[k] = 1;
      return true;
    });
  }

  async function quietRefresh() {
    if (T.loading || document.hidden || !T.sid) return;
    try {
      // 先比对轻量版本号，日志没变就不重读整份账本（大会话能到几十 MB）
      const rv = await api('/api/trajectory/rev', { session_id: T.sid });
      const key = JSON.stringify((rv && rv.rev) || null);
      if (!rv || !rv.ok || key === T.rev) return;
      const list = el('traj-list');
      const atBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 48;
      const view = await fetchView(0);
      if (signature(view) === T.sig) return;
      T.stats = view.stats || {};
      T.source = view.source || 'events';
      T.hasMore = !!view.has_more;
      T.turns = view.turns || [];
      T.sig = signature(view);
      render({ keepScroll: true });
      // 原本就在底部：继续跟随最新一条
      if (atBottom) list.scrollTop = list.scrollHeight;
    } catch (e) { /* 跟随失败静默，等下一次 */ }
  }

  // ---------------------------------------------------------------- 顶部 / 筛选

  function renderSub() {
    const st = T.stats || {};
    const parts = [];
    if (T.source === 'session') parts.push('旧会话回退');
    if (T.sid) parts.push(T.sid.slice(0, 10) + '…');
    parts.push((st.cells || 0) + ' 条记录');
    parts.push((st.errors || 0) + ' 个失败');
    // 每一步都带完整上下文，直接求和会把同一段提示重复计数，这里取峰值
    let peak = 0, outTok = 0;
    T.cells.forEach((x) => {
      peak = Math.max(peak, Number(x.c.input) || 0);
      outTok += Number(x.c.output) || 0;
    });
    if (peak || outTok) {
      parts.push(num(peak) + ' 上下文 · ' + num(outTok) + ' 输出');
    }
    el('traj-sub').textContent = parts.join(' · ');
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
      kinds.map((k) => chip(k, KIND_LABEL[k] || k, bk[k])).join('');
    box.querySelectorAll('.traj-chip').forEach((b) => {
      b.onclick = () => {
        T.filter = b.dataset.kind || 'all';
        renderFilters();
        applyFilter();
      };
    });
  }

  function renderToolSelect() {
    const counts = {};
    T.cells.forEach((x) => {
      const n = x.c.toolName;
      if (n) counts[n] = (counts[n] || 0) + 1;
    });
    const sel = el('traj-tool');
    const names = Object.keys(counts).sort((a, b) => counts[b] - counts[a]);
    sel.innerHTML = '<option value="">全部工具</option>' + names.map((n) =>
      '<option value="' + esc(n) + '">' + esc(n) + ' (' + counts[n] + ')</option>'
    ).join('');
    if (T.tool && !counts[T.tool]) T.tool = '';
    sel.value = T.tool;
  }

  // ---------------------------------------------------------------- 时间轴

  function renderTimeline() {
    const items = T.cells.filter((x) => x.c.timeSeconds != null && x.c.timeSeconds > 0);
    const box = el('traj-timeline');
    if (!items.length) {
      box.innerHTML = '<div class="traj-tl-axis"><span class="tl-note">' +
        '无耗时数据（旧会话或全部失败）</span></div>';
      return;
    }
    const max = Math.max.apply(null, items.map((x) => Number(x.c.timeSeconds) || 0)) || 1;
    const bars = items.map((x) => {
      const d = Number(x.c.timeSeconds) || 0;
      const w = Math.round(Math.max(5, Math.min(120, (d / max) * 115 + 5)));
      return '<span class="traj-span k-' + esc(x.c.kind) + (x.c.isError ? ' err' : '') +
        '" data-rid="' + esc(cid(x.c)) + '" style="width:' + w + 'px" title="#' +
        Number(x.c.index) + ' ' + esc(KIND_LABEL[x.c.kind] || x.c.kind) + ' ' +
        esc(ms(d * 1000)) + '"></span>';
    }).join('');
    box.innerHTML = '<div class="traj-tl-bars">' + bars + '</div>' +
      '<div class="traj-tl-axis"><span>单条耗时条</span><span>最长 ' +
      esc(ms(max * 1000)) + '</span><span class="tl-note">' + items.length +
      ' 条有耗时 · 点击跳到记录</span></div>';
    box.querySelectorAll('.traj-span').forEach((s) => {
      s.onclick = () => selectRid(s.dataset.rid);
    });
  }

  // ---------------------------------------------------------------- 列表

  function turnStats(t) {
    let cells = 0, errors = 0, tools = 0, steps = 0, secs = 0;
    let peakIn = 0, outTok = 0;
    (t.groups || []).forEach((g) => {
      if (/^第\s*\d+\s*步$/.test(String(g.title || ''))) steps += 1;
      (g.cells || []).forEach((c) => {
        cells += 1;
        if (c.isError) errors += 1;
        if (c.kind === 'tool' || c.kind === 'subtool') tools += 1;
        peakIn = Math.max(peakIn, Number(c.input) || 0);
        outTok += Number(c.output) || 0;
        secs += Number(c.timeSeconds) || 0;
      });
    });
    let dur = null;
    if (t.start && t.end && t.end > t.start) dur = (t.end - t.start) / 1000;
    else if (secs > 0) dur = secs;
    return { cells, errors, tools, peakIn, outTok, steps, dur };
  }

  function rowHtml(c) {
    const kind = String(c.kind || '');
    const label = KIND_LABEL[kind] || kind;
    const meta = metaBits(c);
    return '<div class="traj-row" role="button" tabindex="-1" data-idx="' +
      Number(c.index) + '" data-rid="' + esc(cid(c)) + '" data-kind="' + esc(kind) +
      '" data-tool="' + esc(c.toolName || '') + '" data-err="' +
      (c.isError ? '1' : '0') + '" data-title="' + esc(c.text || '') +
      '" data-text="' + esc(searchBlob(c)) + '">' +
      '<span class="traj-bar k-' + esc(kind) + '"></span>' +
      '<span class="traj-idx">#' + Number(c.index) + '</span>' +
      '<span class="traj-body"><span class="traj-line1">' +
      '<span class="traj-kind k-' + esc(kind) + (c.isError ? ' k-error' : '') + '">' +
      esc(label) + '</span>' +
      '<span class="traj-t-title">' + esc(c.text || '（空）') + '</span></span>' +
      '<span class="traj-line2"><span class="traj-t-meta">' +
      esc(meta.join(' · ') || '—') + '</span></span></span>' +
      '<span class="traj-side">' +
      (c.isError ? '<span class="traj-tag traj-err">失败</span>' : '') +
      '<button type="button" class="traj-row-copy" title="复制这条记录">⧉</button>' +
      '</span></div>';
  }

  function renderList(opts) {
    const list = el('traj-list');
    const keepTop = opts && opts.keepScroll ? list.scrollTop : null;
    const parts = [];
    if (T.hasMore) {
      parts.push('<button type="button" class="ghost traj-loadmore" id="traj-more">加载更早</button>');
    }
    (T.turns || []).forEach((t) => {
      const st = turnStats(t);
      const bits = [];
      if (st.steps) bits.push(st.steps + ' 步');
      bits.push(st.cells + ' 条');
      if (st.tools) bits.push(st.tools + ' 工具');
      if (st.peakIn || st.outTok) {
        bits.push(num(st.peakIn) + ' 上下文 · ' + num(st.outTok) + ' 输出');
      }
      if (st.errors) bits.push(st.errors + ' 失败');
      if (st.dur != null) bits.push(ms(st.dur * 1000));
      let html = '<section class="traj-turn" data-turn="' + Number(t.turn) + '">' +
        '<button type="button" class="traj-turn-h">' +
        '<span class="traj-caret">▾</span><b>Turn ' + Number(t.turn) + '</b>' +
        '<span class="traj-turn-meta">' + esc(bits.join(' · ')) + '</span>' +
        (t.reason ? '<span class="traj-turn-reason' +
          (st.errors ? ' err' : '') + '">' + esc(t.reason) + '</span>' : '') +
        '</button>';
      let lastG = null;
      (t.groups || []).forEach((g) => {
        const cells = g.cells || [];
        if (!cells.length) return;
        if (g.title !== lastG) {
          html += '<div class="traj-group-h">' + esc(g.title || '') +
            '<i>' + cells.length + '</i></div>';
          lastG = g.title;
        }
        html += cells.map(rowHtml).join('');
      });
      html += '</section>';
      parts.push(html);
    });
    list.innerHTML = parts.join('') ||
      '<div class="traj-empty">这个会话还没有轨迹记录。</div>';
    const more = el('traj-more');
    if (more) {
      more.onclick = () => {
        const first = (T.turns && T.turns[0] && T.turns[0].turn) || 0;
        if (first) load(first, true);
      };
    }
    if (keepTop != null) list.scrollTop = keepTop;
  }

  function visibleRows() {
    return Array.from(el('traj-list').querySelectorAll('.traj-row')).filter((r) => {
      if (r.classList.contains('hidden')) return false;
      const sec = r.closest('.traj-turn');
      return !sec || !sec.classList.contains('hidden');
    });
  }

  function matchCell(c) {
    if (T.filter !== 'all' && String(c.kind) !== T.filter) return false;
    if (T.tool && String(c.toolName || '') !== T.tool) return false;
    if (T.errorsOnly && !c.isError) return false;
    const q = T.q.trim().toLowerCase();
    if (q && searchBlob(c).indexOf(q) < 0) return false;
    return true;
  }

  function markHtml(raw, q) {
    if (!q) return esc(raw);
    const lower = String(raw).toLowerCase();
    const ql = q.toLowerCase();
    let i = 0;
    let out = '';
    while (i <= lower.length) {
      const j = lower.indexOf(ql, i);
      if (j < 0) break;
      out += esc(String(raw).slice(i, j)) + '<mark>' +
        esc(String(raw).slice(j, j + ql.length)) + '</mark>';
      i = j + ql.length;
    }
    return out + esc(String(raw).slice(i));
  }

  function applyFilter() {
    const q = T.q.trim().toLowerCase();
    const list = el('traj-list');
    const matched = {};
    let shown = 0;
    let total = 0;
    list.querySelectorAll('.traj-row').forEach((row) => {
      total += 1;
      const ok = (!T.errorsOnly || row.dataset.err === '1') &&
        (!T.tool || row.dataset.tool === T.tool) &&
        (T.filter === 'all' || row.dataset.kind === T.filter) &&
        (!q || (row.dataset.text || '').indexOf(q) >= 0);
      row.classList.toggle('hidden', !ok);
      if (ok) {
        shown += 1;
        matched[row.dataset.rid] = 1;
      }
      const title = row.querySelector('.traj-t-title');
      if (title) {
        const raw = row.dataset.title || '';
        if (q) title.innerHTML = markHtml(raw, q);
        else title.textContent = raw;
      }
    });
    list.querySelectorAll('.traj-turn').forEach((sec) => {
      let n = 0;
      sec.querySelectorAll('.traj-row').forEach((r) => {
        if (!r.classList.contains('hidden')) n += 1;
      });
      sec.classList.toggle('hidden', n === 0 && total > 0);
    });
    list.querySelectorAll('.traj-group-h').forEach((h) => {
      let n = h.nextElementSibling;
      let has = false;
      while (n && !n.classList.contains('traj-group-h') &&
             !n.classList.contains('traj-turn-h')) {
        if (n.classList.contains('traj-row') && !n.classList.contains('hidden')) {
          has = true;
          break;
        }
        n = n.nextElementSibling;
      }
      h.classList.toggle('hidden', !has);
    });
    el('traj-timeline').querySelectorAll('.traj-span').forEach((s) => {
      s.classList.toggle('dim', !matched[s.dataset.rid]);
      s.classList.toggle('on', s.dataset.rid === T.selectedRid);
    });
    let empty = el('traj-nomatch');
    if (total > 0 && shown === 0) {
      if (!empty) {
        empty = document.createElement('div');
        empty.id = 'traj-nomatch';
        empty.className = 'traj-empty';
        empty.textContent = '没有匹配的记录，试试清空搜索或筛选。';
        list.insertBefore(empty, list.firstChild);
      }
    } else if (empty) {
      empty.remove();
    }
    const hits = el('traj-hits');
    hits.textContent = q || T.filter !== 'all' || T.tool || T.errorsOnly
      ? shown + ' / ' + total : '';
    el('traj-search-clear').classList.toggle('hidden', !T.q);
    el('traj-foot-stat').textContent = '显示 ' + shown + ' / ' + total +
      ' 条 · ' + (T.turns || []).length + ' 轮';
  }

  // ---------------------------------------------------------------- 选中 / 检查器

  function expandTurnOf(row) {
    if (!row) return;
    const sec = row.closest('.traj-turn');
    if (sec) sec.classList.remove('collapsed');
  }

  function ensureVisible(row) {
    if (!row) return;
    const list = el('traj-list');
    const rb = row.getBoundingClientRect();
    const lb = list.getBoundingClientRect();
    if (rb.top < lb.top + 8 || rb.bottom > lb.bottom - 8) {
      row.scrollIntoView({ block: 'nearest' });
    }
  }

  function selectRid(rid, opts) {
    if (!rid) return;
    T.selectedRid = rid;
    const row = el('traj-list').querySelector('.traj-row[data-rid="' + cssEsc(rid) + '"]');
    expandTurnOf(row);
    el('traj-list').querySelectorAll('.traj-row').forEach((r) => {
      r.classList.toggle('on', r.dataset.rid === rid);
    });
    el('traj-timeline').querySelectorAll('.traj-span').forEach((s) => {
      s.classList.toggle('on', s.dataset.rid === rid);
    });
    if (row && !(opts && opts.noScroll)) ensureVisible(row);
    const cell = cellByRid(rid);
    renderInspector(cell);
    loadDetail(cell);
  }

  // CSS.escape 在个别旧内核里没有，这里给 data-rid 选择器做最小转义
  function cssEsc(s) {
    return String(s).replace(/["\\]/g, '\\$&');
  }

  async function loadDetail(cell) {
    if (!cell || cell.kind !== 'tool') return;
    if (!cell.traceId && !cell.callId) return;
    const key = cell.traceId || cell.callId;
    if (T.detail[key] != null) {
      if (cell.outputDetail !== T.detail[key]) {
        cell.outputDetail = T.detail[key];
        if (T.selectedRid === cid(cell)) renderInspector(cell);
      }
      return;
    }
    try {
      const r = await api('/api/trajectory/tool', {
        session_id: T.sid, trace_id: cell.traceId || '', call_id: cell.callId || '',
      });
      if (r && r.ok) {
        T.detail[key] = r.result || '';
        if (T.selectedRid === cid(cell)) {
          cell.outputDetail = T.detail[key] || cell.outputDetail;
          renderInspector(cell);
        }
      }
    } catch (e) { /* 详情取不到就保留已有副本 */ }
  }

  function tabsFor(cell) {
    const out = [{ id: 'overview', label: '概览' }];
    if (cell.inputDetail) out.push({ id: 'input', label: '输入', size: cell.inputDetail.length });
    const output = cell.outputDetail || cell.result;
    if (output) out.push({ id: 'output', label: '输出', size: String(output).length });
    if (cell.thinkingDetail) out.push({ id: 'thinking', label: '思考', size: cell.thinkingDetail.length });
    if (cell.promptDiff) out.push({ id: 'diff', label: '提示差异', size: cell.promptDiff.length });
    if (cell.schemaDetail) out.push({ id: 'schema', label: 'Schema', size: cell.schemaDetail.length });
    out.push({ id: 'raw', label: '原始' });
    return out;
  }

  function kvRow(k, v, copyValue) {
    return '<dt>' + esc(k) + '</dt><dd>' + esc(v == null ? '' : v) +
      (copyValue ? '<button type="button" data-copy="' + esc(copyValue) +
        '" title="复制">复制</button>' : '') + '</dd>';
  }

  function overviewHtml(cell) {
    const it = itemByRid(cid(cell));
    const rows = [];
    if (it) rows.push(kvRow('轮次', 'Turn ' + Number(it.turn.turn) + ' · ' + (it.group.title || '')));
    if (cell.startedAt) {
      rows.push(kvRow('记录时间', new Date(Number(cell.startedAt)).toLocaleString()));
    }
    if (cell.timeSeconds != null) rows.push(kvRow('耗时', ms(cell.timeSeconds * 1000)));
    if (cell.ttftMs != null) rows.push(kvRow('首包', ms(cell.ttftMs)));
    if (cell.decodeTps != null) rows.push(kvRow('解码速度', cell.decodeTps + ' tok/s'));
    if (cell.input) rows.push(kvRow('输入 token', num(cell.input)));
    if (cell.output) rows.push(kvRow('输出 token', num(cell.output)));
    if (cell.think) rows.push(kvRow('推理 token', num(cell.think)));
    if (cell.cacheRead) rows.push(kvRow('缓存命中', num(cell.cacheRead)));
    if (cell.cacheWrite) rows.push(kvRow('缓存写入', num(cell.cacheWrite)));
    if (cell.toolName) rows.push(kvRow('工具', cell.toolName, cell.toolName));
    if (cell.model) rows.push(kvRow('模型', [cell.provider, cell.model].filter(Boolean).join(' / ')));
    if (cell.toolCount) rows.push(kvRow('工具数', cell.toolCount + ' 个'));
    if (cell.systemReason) {
      const map = { initial: '首次', resume: '进程重启恢复', change: '内容变化' };
      rows.push(kvRow('系统提示', map[cell.systemReason] || cell.systemReason));
    }
    if (cell.callId) rows.push(kvRow('callId', cell.callId, cell.callId));
    if (cell.traceId) rows.push(kvRow('traceId', cell.traceId, cell.traceId));
    if (cell.source && cell.source.label) rows.push(kvRow('来源', cell.source.label));
    if (cell.batch) rows.push(kvRow('批次', cell.batch, cell.batch));
    if (cell.messages != null) rows.push(kvRow('子代理消息', cell.messages + ' 条'));
    const outLen = cell.resultChars || String(cell.outputDetail || cell.result || '').length;
    if (outLen) {
      const clipped = cell.resultChars && cell.outputDetail &&
        cell.resultChars > String(cell.outputDetail).length;
      rows.push(kvRow('输出大小', num(outLen) + ' 字' + (clipped ? ' · 已截断' : '')));
    }
    if (cell.isError) rows.push(kvRow('失败原因', cell.errorText || '（未记录）'));
    if (cell.write) rows.push(kvRow('写入文件', '是'));

    let html = '<dl class="traj-kv">' + rows.join('') + '</dl>';
    if (cell.parts && cell.parts.length) {
      html += '<div class="traj-sub-h">系统提示分段（' + cell.parts.length + '）</div>' +
        '<div class="traj-parts">' + cell.parts.map((p) =>
          '<button type="button" class="traj-part' + (p.changed ? ' changed' : '') +
          '" data-rid="' + esc(p.recordId || '') + '"' +
          (p.recordId ? '' : ' disabled') + '>' +
          '<span class="tp-label">' + esc(p.label || '段落') + '</span>' +
          '<span class="tp-meta">' + num(p.chars || 0) + ' 字' +
          (p.changed ? ' · 有变化' : '') + '</span></button>').join('') +
        '</div>';
    }
    if (cell.errorText && !cell.isError) {
      html += '<div class="traj-sub-h">错误信息</div><pre class="traj-pre">' +
        esc(cell.errorText) + '</pre>';
    }
    return html;
  }

  function paneText(cell, tab) {
    if (tab === 'input') return String(cell.inputDetail || '');
    if (tab === 'output') return String(cell.outputDetail || cell.result || '');
    if (tab === 'thinking') return String(cell.thinkingDetail || '');
    if (tab === 'diff') return String(cell.promptDiff || '');
    if (tab === 'schema') return String(cell.schemaDetail || '');
    if (tab === 'raw') return JSON.stringify(cell, null, 2);
    return String(cell.text || '');
  }

  function textPane(cell, tab, label, text) {
    return '<div class="traj-pane-bar"><span>' + esc(label) + ' · ' +
      num(String(text || '').length) + ' 字</span><span class="sp"></span>' +
      '<button type="button" data-act="wrap">换行：' + (T.wrap ? '开' : '关') + '</button>' +
      '<button type="button" data-act="copy-pane">复制</button></div>' +
      '<pre class="traj-pre' + (T.wrap ? '' : ' nowrap') + '">' + esc(text) + '</pre>';
  }

  function renderInspector(cell) {
    const box = el('traj-inspector');
    if (!cell) {
      T.selectedRid = '';
      box.innerHTML = '<div class="traj-empty">选择一条记录查看输入 / 输出 / 思考 / ' +
        '耗时 / Schema。<br>支持 ↑↓ 或 j/k 切换，n 跳到下一个失败。</div>';
      return;
    }
    const kind = String(cell.kind || '');
    const tabs = tabsFor(cell);
    if (!tabs.some((t) => t.id === T.tab)) T.tab = 'overview';
    const active = T.tab;
    const pane = active === 'overview'
      ? overviewHtml(cell)
      : textPane(cell, active,
        tabs.find((t) => t.id === active).label, paneText(cell, active));
    box.innerHTML =
      '<div class="traj-ins-head"><span class="traj-kind k-' + esc(kind) +
      (cell.isError ? ' k-error' : '') + '">' + esc(KIND_LABEL[kind] || kind) +
      '</span><b>#' + Number(cell.index) + '</b>' +
      '<div class="traj-ins-actions">' +
      '<button type="button" data-act="copy">复制</button>' +
      '<button type="button" data-act="json">JSON</button></div></div>' +
      '<div class="traj-ins-meta">' + metaBits(cell).map((x) =>
        '<span class="traj-tag">' + esc(x) + '</span>').join('') +
      (cell.isError ? '<span class="traj-tag traj-err">失败</span>' : '') + '</div>' +
      '<div class="traj-tabbar">' + tabs.map((t) =>
        '<button type="button" class="traj-tab' + (t.id === active ? ' on' : '') +
        '" data-tab="' + t.id + '">' + esc(t.label) +
        (t.size ? '<i>' + num(t.size) + '</i>' : '') + '</button>').join('') +
      '</div><div class="traj-pane">' + pane + '</div>';

    box.onclick = (e) => {
      const b = e.target.closest('button');
      if (!b) return;
      if (b.dataset.copy != null) { copyText(b.dataset.copy); return; }
      if (b.dataset.tab) { T.tab = b.dataset.tab; renderInspector(cell); return; }
      if (b.dataset.rid) { selectRid(b.dataset.rid); return; }
      const act = b.dataset.act;
      if (act === 'copy') copyText(cell.text || '');
      else if (act === 'json') copyText(JSON.stringify(cell, null, 2));
      else if (act === 'copy-pane') copyText(paneText(cell, active));
      else if (act === 'wrap') { T.wrap = !T.wrap; renderInspector(cell); }
    };
  }

  // ---------------------------------------------------------------- 渲染

  function render(opts) {
    indexCells();
    renderSub();
    renderFilters();
    renderToolSelect();
    renderTimeline();
    renderList(opts);
    if (T.selectedRid && !cellByRid(T.selectedRid)) T.selectedRid = '';
    renderInspector(cellByRid(T.selectedRid) || null);
    applyFilter();
  }

  // ---------------------------------------------------------------- 打开 / 关闭

  async function open(rid) {
    const sid = (state.sessions && state.sessions.current) || '';
    if (!sid) { toast('先打开一个会话'); return; }
    T.sid = sid;
    T.turns = [];
    T.cells = [];
    T.stats = null;
    T.selectedRid = '';
    T.detail = {};
    T.filter = 'all';
    T.tool = '';
    T.q = '';
    T.errorsOnly = false;
    T.tab = 'overview';
    T.opened = true;
    T.rev = '';
    el('traj-search').value = '';
    el('traj-errors').checked = false;
    el('trajectory-modal').classList.remove('hidden');
    document.body.classList.add('traj-locked');
    el('traj-list').innerHTML = '<div class="traj-empty">读取中…</div>';
    el('traj-inspector').innerHTML = '<div class="traj-empty">读取中…</div>';
    el('traj-timeline').innerHTML = '';
    el('traj-foot-stat').textContent = '';
    await load(0, false);
    if (rid) selectRid(rid);
    startFollow();
  }

  function close() {
    stopFollow();
    T.opened = false;
    el('trajectory-modal').classList.add('hidden');
    document.body.classList.remove('traj-locked');
    hideExportMenu();
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

  // ---------------------------------------------------------------- 导出

  function buildMarkdown() {
    const lines = ['# 轨迹 · ' + T.sid, ''];
    const sub = el('traj-sub').textContent || '';
    if (sub) lines.push('> ' + sub, '');
    (T.turns || []).forEach((t) => {
      const cells = [];
      (t.groups || []).forEach((g) => {
        (g.cells || []).forEach((c) => { if (matchCell(c)) cells.push(c); });
      });
      if (!cells.length) return;
      lines.push('## Turn ' + Number(t.turn) + (t.reason ? ' · ' + t.reason : ''));
      cells.forEach((c) => {
        lines.push('- **' + (KIND_LABEL[c.kind] || c.kind) + '** #' + c.index + ' ' +
          oneLine(c.text || '', 240));
        const meta = metaBits(c);
        if (meta.length) lines.push('  - ' + meta.join(' · '));
        if (c.isError && c.errorText) lines.push('  - 错误：' + oneLine(c.errorText, 300));
      });
      lines.push('');
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

  function step(dir) {
    const rows = visibleRows();
    if (!rows.length) return;
    let i = rows.findIndex((r) => r.dataset.rid === T.selectedRid);
    if (i < 0) i = dir > 0 ? -1 : rows.length;
    const next = rows[(i + dir + rows.length) % rows.length];
    selectRid(next.dataset.rid);
  }

  function jumpFail(dir) {
    const errs = visibleRows().filter((r) => r.dataset.err === '1');
    if (!errs.length) { toast('当前筛选没有失败记录'); return; }
    let i = errs.findIndex((r) => r.dataset.rid === T.selectedRid);
    if (i < 0) i = dir > 0 ? -1 : 0;
    const next = errs[(i + dir + errs.length) % errs.length];
    selectRid(next.dataset.rid);
  }

  function onKey(e) {
    if (!isOpen() || e.ctrlKey || e.metaKey || e.altKey) return;
    const tag = (e.target.tagName || '').toLowerCase();
    const typing = tag === 'input' || tag === 'select' || tag === 'textarea';
    if (e.key === 'Escape') {
      if (!el('traj-export-menu').classList.contains('hidden')) { hideExportMenu(); return; }
      if (typing && el('traj-search').value) {
        el('traj-search').value = '';
        T.q = '';
        applyFilter();
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
    else if (e.key === 'g') { e.preventDefault(); const r = visibleRows()[0]; if (r) selectRid(r.dataset.rid); }
    else if (e.key === 'G') { e.preventDefault(); const rs = visibleRows(); if (rs.length) selectRid(rs[rs.length - 1].dataset.rid); }
  }

  // ---------------------------------------------------------------- 绑定

  function bind() {
    const btn = el('btn-trajectory');
    if (btn) btn.onclick = () => open();
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
        applyFilter();
      }, 120);
    };
    el('traj-search-clear').onclick = () => {
      search.value = '';
      T.q = '';
      applyFilter();
      search.focus();
    };

    el('traj-tool').onchange = (e) => { T.tool = e.target.value || ''; applyFilter(); };
    el('traj-errors').onchange = (e) => { T.errorsOnly = !!e.target.checked; applyFilter(); };
    el('traj-fail-next').onclick = () => jumpFail(1);
    el('traj-fold').onclick = () => {
      const turns = Array.from(el('traj-list').querySelectorAll('.traj-turn'));
      const collapse = turns.some((s) => !s.classList.contains('collapsed'));
      turns.forEach((s) => s.classList.toggle('collapsed', collapse));
      el('traj-fold').textContent = collapse ? '展开全部' : '折叠全部';
    };

    const list = el('traj-list');
    list.onclick = (e) => {
      const head = e.target.closest('.traj-turn-h');
      if (head) {
        const sec = head.closest('.traj-turn');
        sec.classList.toggle('collapsed');
        return;
      }
      const row = e.target.closest('.traj-row');
      if (!row) return;
      if (e.target.closest('.traj-row-copy')) {
        const cell = cellByRid(row.dataset.rid);
        if (cell) copyText(cell.text || '');
        return;
      }
      selectRid(row.dataset.rid);
    };

    const modal = el('trajectory-modal');
    modal.addEventListener('click', (e) => { if (e.target === modal) close(); });
    document.addEventListener('keydown', onKey);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bind);
  } else {
    bind();
  }
  window.openTrajectory = open;
})();
