function galToggleScript(force) {
  const p = $('#gal-script-panel');
  if (!p) return;
  const show = (typeof force === 'boolean') ? force : p.classList.contains('hidden');
  p.classList.toggle('hidden', !show);
  if (show) galRenderScript();
}

async function galRenderScript() {
  const list = $('#gal-script-list');
  const st = $('#gal-script-status');
  if (!list) return;
  list.innerHTML = '<div class="gal-review-empty">读取中…</div>';
  let r = null;
  try { r = await api('/api/gal/script', { id: state.sessions.current }); }
  catch (e) { list.innerHTML = '<div class="gal-review-empty">读取失败：' + escapeHtml(String(e)) + '</div>'; return; }
  const story = (r && r.story) || {};
  const nodes = (story && story.nodes) || [];
  const outline = (r && r.outline) || '';
  const curId = (r && r.node_id) || '';
  if (st) {
    st.textContent = (r && r.on)
      ? ('已启用 · 已展开 ' + nodes.length + ' 幕')
      : (nodes.length ? '未启用' : '尚未生成');
  }
  list.innerHTML = '';
  if (!outline && !nodes.length) {
    const em = document.createElement('div');
    em.className = 'gal-review-empty';
    em.textContent = '还没有剧本。点右上「✨ 生成 / 重新生成」，AI 会依据角色设定与用户画像编写大纲。';
    list.appendChild(em);
    return;
  }
  if (outline) {
    const d = document.createElement('div');
    d.className = 'gal-review-row';
    const who = document.createElement('span');
    who.className = 'gal-review-who'; who.textContent = '大纲';
    d.appendChild(who);
    const b = document.createElement('div');
    b.className = 'gal-review-body';
    b.innerHTML = safeRenderMarkdown(outline);
    d.appendChild(b);
    list.appendChild(d);
  }
  nodes.forEach((n, i) => {
    const d = document.createElement('div');
    d.className = 'gal-review-row';
    const who = document.createElement('span');
    who.className = 'gal-review-who';
    who.textContent = ('第 ' + (i + 1) + ' 幕') + (n.id === curId ? ' · 当前' : '');
    d.appendChild(who);
    const b = document.createElement('div');
    b.className = 'gal-review-body';
    const flag = n.type === 'ending' ? '（结局）' : ('（' + (n.maxRounds || 0) + ' 轮）');
    b.innerHTML = safeRenderMarkdown('**' + escapeHtml(n.id) + '** ' + escapeHtml(flag) + ' ' + (n.prompt || ''));
    d.appendChild(b);
    list.appendChild(d);
  });
}

async function galGenerateScript() {
  const cid = galState.charId || (curSession() || {}).gal_character_id || '';
  if (!cid) { toast('请先选择角色', 'error'); return; }
  const st = $('#gal-script-status');
  const btn = $('#gal-script-gen');
  const old = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = '构思中…'; }
  if (st) st.textContent = '构思剧情中…';
  toast('AI 正在构思剧情…');
  try {
    const r = await api('/api/gal/script', {
      action: 'generate', id: state.sessions.current, character_id: cid,
    });
    if (r && r.node_id) galSetNode({ node_id: r.node_id, round: 0, max_rounds: 0 });
    const first = ((r && r.story && r.story.nodes) || []).find((n) => n.id === (r && r.node_id));
    if (first && first.prompt) galAddSegment({ narration: first.prompt });   // 首幕场景进对话框
    await galRenderScript();
    toast('剧本已生成，开始演出吧');
  } catch (e) {
    toastErr('生成', e);
    await galRenderScript();
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = old; }
  }
}

/* ---- 对话回顾：覆盖在舞台上的整段对话记录 ---- */
function galToggleReview(force) {
  const p = $('#gal-review-panel');
  if (!p) return;
  const show = (typeof force === 'boolean') ? force : p.classList.contains('hidden');
  p.classList.toggle('hidden', !show);
  if (show) galRenderReview();
}

function galRenderReview() {
  const list = $('#gal-review-list');
  const cnt = $('#gal-review-count');
  if (!list) return;
  const s = curSession();
  const msgs = (s && s.messages) || [];
  list.innerHTML = '';
  if (cnt) cnt.textContent = msgs.length ? (msgs.length + ' 条记录') : '';
  if (!msgs.length) {
    const em = document.createElement('div');
    em.className = 'gal-review-empty';
    em.textContent = '还没有对话记录';
    list.appendChild(em);
    return;
  }
  const name = (galState.char && galState.char.name) || '';
  msgs.forEach((m) => {
    const role = m.role || '';
    if (role !== 'user' && role !== 'assistant') return;      // 跳过系统 / 工具消息
    const row = document.createElement('div');
    row.className = 'gal-review-row' + (role === 'user' ? ' me' : '');
    const who = document.createElement('span');
    who.className = 'gal-review-who';
    who.textContent = role === 'user' ? '你' : (name || '角色');
    row.appendChild(who);
    const body = document.createElement('div');
    body.className = 'gal-review-body';
    const segs = (m.gal && Array.isArray(m.gal.dialog) && m.gal.dialog.length) ? m.gal.dialog : null;
    if (segs) {
      segs.forEach((sg) => {
        const isNarr = !sg.speech && sg.narration;
        const d = document.createElement('div');
        d.className = 'gal-review-seg' + (isNarr ? ' narr' : '');
        d.innerHTML = safeRenderMarkdown(isNarr ? ('（' + sg.narration + '）') : (sg.speech || ''));
        body.appendChild(d);
      });
    } else {
      body.innerHTML = safeRenderMarkdown(m.content || '');
    }
    row.appendChild(body);
    list.appendChild(row);
  });
  list.scrollTop = list.scrollHeight;
}

/* ---- 剧情档案：AI 记下来的「主角已知信息」+ 舞台方位示意图 ---- */
// 类别排序偏好（命中靠前的排前面，其余按类别名排到最后，再按条数）
const GAL_JOURNAL_ORDER = ['人物', '势力', '能力', '情报', '线索', '地点',
                           '规则', '世界', '主线', '悬念', '时间线'];

// 舞台方位：{place, marks:[{name,pos}]}；收到即更新缓存，档案面板开着就重绘
function galSetMap(map) {
  galState.map = (map && typeof map === 'object') ? map : {};
  const p = $('#gal-journal-panel');
  if (p && !p.classList.contains('hidden')) galRenderJournal();
}

function galToggleJournal(force) {
  const p = $('#gal-journal-panel');
  if (!p) return;
  const show = (typeof force === 'boolean') ? force : p.classList.contains('hidden');
  p.classList.toggle('hidden', !show);
  if (show) galRenderJournal();
}

// 渲染 3×3 方位示意图
function galRenderMap() {
  const box = $('#gal-map');
  if (!box) return;
  const m = galState.map || {};
  const marks = Array.isArray(m.marks) ? m.marks : [];
  const place = String(m.place || '').trim();
  if (!place && !marks.length) { box.classList.add('hidden'); box.innerHTML = ''; return; }
  box.classList.remove('hidden');
  // 归一化坐标 → 位置表（按名字索引，供连线使用）
  const at = {};
  marks.forEach((mk) => {
    const nm = String((mk && mk.name) || '').trim();
    if (nm) at[nm] = { x: galMapNum(mk.x), y: galMapNum(mk.y) };
  });
  const pct = (v) => (v * 100).toFixed(1) + '%';
  let html = '<div class="gal-map-place">📍 ' + escapeHtml(place || '未知地点') + '</div>';
  html += '<div class="gal-map-board">' +
          '<span class="gal-map-compass gal-map-c-n">北</span>' +
          '<span class="gal-map-compass gal-map-c-e">东</span>' +
          '<span class="gal-map-compass gal-map-c-s">南</span>' +
          '<span class="gal-map-compass gal-map-c-w">西</span>';
  const links = (Array.isArray(m.links) ? m.links : []).filter(
    (lk) => lk && at[String(lk.a || '')] && at[String(lk.b || '')] && lk.a !== lk.b);
  if (links.length) {
    let svg = '';
    links.forEach((lk) => {
      const a = at[String(lk.a)], b = at[String(lk.b)];
      svg += '<line x1="' + pct(a.x) + '" y1="' + pct(a.y) + '" x2="' + pct(b.x) + '" y2="' + pct(b.y) + '"/>';
    });
    links.forEach((lk) => {
      const lb = String(lk.label || '').trim();
      if (!lb) return;
      const a = at[String(lk.a)], b = at[String(lk.b)];
      svg += '<text x="' + pct((a.x + b.x) / 2) + '" y="' + pct((a.y + b.y) / 2) + '">' +
             escapeHtml(lb) + '</text>';
    });
    html += '<svg class="gal-map-links" preserveAspectRatio="none">' + svg + '</svg>';
  }
  marks.forEach((mk) => {
    const nm = String((mk && mk.name) || '').trim();
    if (!nm) return;
    const p = at[nm];
    const self = (nm === '你' || nm === '我' || nm === '主角');
    html += '<span class="gal-map-mark' + (self ? ' is-self' : '') +
            '" style="left:' + pct(p.x) + ';top:' + pct(p.y) + '">' + escapeHtml(nm) + '</span>';
  });
  html += '</div>';
  box.innerHTML = html;
}

// 地图坐标兜底：非法值一律落在正中
function galMapNum(v) {
  const f = Number(v);
  return (isFinite(f) && f >= 0 && f <= 1) ? f : 0.5;
}

// 档案数据缓存（列表 / 图谱共用）
let galJournalData = { nodes: [], edges: [] };
let galJournalView = 'list';        // list | graph
let galJournalNet = null;           // 图谱实例

// 切换「列表 / 关系图」两种视图
function galSetJournalView(view) {
  galJournalView = (view === 'graph') ? 'graph' : 'list';
  const isGraph = galJournalView === 'graph';
  const bL = $('#gal-jview-list'), bG = $('#gal-jview-graph');
  if (bL) bL.classList.toggle('active', !isGraph);
  if (bG) bG.classList.toggle('active', isGraph);
  const l = $('#gal-journal-list'), g = $('#gal-journal-graph');
  if (l) l.classList.toggle('hidden', isGraph);
  if (g) g.classList.toggle('hidden', !isGraph);
  if (isGraph) galRenderJournalGraph();
  else galRenderJournalList();
}

// 渲染档案面板：读取数据后按当前视图渲染
function galRenderJournal() {
  galRenderMap();
  const list = $('#gal-journal-list');
  const cnt = $('#gal-journal-count');
  if (!list) return;
  list.innerHTML = '<div class="gal-journal-empty">正在读取档案…</div>';
  const s = curSession();
  api('/api/gal/journal', { session_id: s && s.id }).then((r) => {
    galJournalData = { nodes: (r && r.nodes) || [], edges: (r && r.edges) || [] };
    if (r && r.map) galState.map = r.map;
    galRenderMap();
    if (cnt) cnt.textContent = galJournalData.nodes.length ? (galJournalData.nodes.length + ' 条') : '';
    galSetJournalView(galJournalView);
  }).catch(() => {
    list.innerHTML = '<div class="gal-journal-empty">档案读取失败。</div>';
  });
}

// 列表视图：按类别分栏
function galRenderJournalList() {
  const list = $('#gal-journal-list');
  if (!list) return;
  const data = galJournalData || { nodes: [], edges: [] };
  const nodes = data.nodes || [];
  list.innerHTML = '';
  if (!nodes.length) {
    list.innerHTML = '<div class="gal-journal-empty">还没有记录。随着剧情推进，AI 会把人物、势力、线索自动记到这里' +
                     '（来自本会话记忆，不含世界观预设）。</div>';
    return;
  }
  const groups = {};
  nodes.forEach((n) => {
    const c = String(n.category || '其他').trim() || '其他';
    (groups[c] = groups[c] || []).push(n);
  });
  const cats = Object.keys(groups).sort((a, b) => {
    const ia = GAL_JOURNAL_ORDER.indexOf(a), ib = GAL_JOURNAL_ORDER.indexOf(b);
    const ra = ia < 0 ? 99 : ia, rb = ib < 0 ? 99 : ib;
    return ra - rb || groups[b].length - groups[a].length || a.localeCompare(b);
  });
  cats.forEach((cat) => {
    const box = document.createElement('div');
    box.className = 'gal-journal-group';
    const h = document.createElement('div');
    h.className = 'gal-journal-cat';
    h.textContent = cat + '（' + groups[cat].length + '）';
    box.appendChild(h);
    groups[cat].forEach((n) => {
      const it = document.createElement('div');
      it.className = 'gal-journal-item';
      const tags = (n.tags || []).map((t) =>
        '<span class="gal-journal-tag">' + escapeHtml(String(t)) + '</span>').join('');
      it.innerHTML = '<div class="gal-journal-title">' + escapeHtml(n.title) +
                     (tags ? ' ' + tags : '') + '</div>' +
                     (n.content ? '<div class="gal-journal-body">' + safeRenderMarkdown(n.content) + '</div>' : '');
      box.appendChild(it);
    });
    list.appendChild(box);
  });
}

// 关系图视图：复用知识库图谱那套配色 / 箭头 / 力学（只读，可拖拽查看）
function galRenderJournalGraph() {
  const el = $('#gal-journal-graph');
  if (!el) return;
  if (galJournalNet) { try { galJournalNet.destroy(); } catch (e) { /* 忽略 */ } galJournalNet = null; }
  const data = galJournalData || { nodes: [], edges: [] };
  const nodesData = data.nodes || [];
  if (!nodesData.length) {
    el.innerHTML = '<div class="gal-journal-empty">还没有可画的关系——AI 记录人物 / 势力 / 线索后，这里会连成图谱。</div>';
    return;
  }
  if (!window.vis || !window.vis.Network || !window.vis.DataSet) {
    el.innerHTML = '<div class="gal-journal-empty">图谱库 vis-network 未加载（本地文件缺失且 CDN 不可用）；请切回「列表」查看。</div>';
    return;
  }
  el.innerHTML = '';
  const th = graphTheme();
  const vnodes = nodesData.map((n) => {
    const sz = (typeof n.size === 'number' && n.size > 0) ? n.size : 16;
    const o = { id: n.id, label: String(n.title || ''), title: String(n.content || '').slice(0, 200),
                shape: 'dot', size: sz, borderWidth: 2,
                color: graphNodeColor(n.color, th),
                shadow: { enabled: true, color: th.nodeShadow, size: 6, x: 0, y: 1 },
                font: { color: th.font, size: 13, strokeWidth: 3, strokeColor: th.labelBg } };
    if (typeof n.x === 'number' && typeof n.y === 'number') { o.x = n.x; o.y = n.y; }
    return o;
  });
  const vedges = (data.edges || []).map((e) => {
    const rel = e.relation || 'related';
    const col = relationColor(rel, th);
    return { id: e.id, from: e.from, to: e.to, label: e.label || relationLabel(rel),
             font: { color: col, size: 11, align: 'middle',
                     strokeWidth: 3, strokeColor: th.labelBg },
             color: { color: col, highlight: col, hover: col },
             arrows: edgeArrows(isEdgeDirected(e)), width: 1.6 };
  });
  try {
    const ds = { nodes: new vis.DataSet(vnodes), edges: new vis.DataSet(vedges) };
    galJournalNet = new vis.Network(el, ds, {
      physics: kbPhysicsOptions(), interaction: { hover: true }, locale: 'cn', locales: VIS_LOCALES,
    });
  } catch (err) {
    el.innerHTML = '<div class="gal-journal-empty">图谱渲染失败，请切回「列表」查看。</div>';
  }
}

// 演出主题（全部 UI）：挂在 <html> 上，核心 token 就地覆盖，
// 因此 #app 之外的设置 / 知识库 / 笔记 / 用量弹窗与 toast 也会跟随