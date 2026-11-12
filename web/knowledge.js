'use strict';

/* ============================================================
   知识库 / 记忆图谱（vis-network）
   ============================================================ */
let kbNetwork = null;
let kbGraphRO = null;      // 图谱容器尺寸监听：canvas 必须跟随容器，否则 focus 会在旧尺寸里居中
let kbEdgeFrom = null;   // 「新增连线」的起点节点 id
let kbSaveTimer = null;
let kbLoadSeq = 0;      // 加载代次，防止异步竞态
let kbView = 'graph';   // graph | list：手动切换，列表视图不依赖 vis-network

function kbMeta(id) {
  return (state.kbs || []).find((k) => k.id === id) || null;
}

function profileKbId() {
  const k = (state.kbs || []).find((x) => x.name === '用户画像');
  return k ? k.id : null;
}

function kbFamilies() {
  return (state.kbData && Array.isArray(state.kbData.families)) ? state.kbData.families : [];
}

function currentKbFamily() {
  return kbFamilies().find((f) => f.id === state.kbFamily) || null;
}

function refreshKbFamilySelect() {
  const sel = $('#kb-family');
  const fams = kbFamilies();
  if (!fams.some((f) => f.id === state.kbFamily)) state.kbFamily = '';
  if (sel) {
    sel.innerHTML = '<option value="">全部主题</option>' +
      fams.map((f) => '<option value="' + f.id + '">' +
        escapeHtml(String(f.title || '未命名主题')) + '</option>').join('');
    sel.value = state.kbFamily;
  }
  const cur = currentKbFamily();
  const t = $('#kf-title');
  const d = $('#kf-desc');
  if (t) t.value = cur ? (cur.title || '') : '';
  if (d) d.value = cur ? (cur.description || '') : '';
}

function saveKbFamily() {
  const title = ($('#kf-title') || {}).value ? $('#kf-title').value.trim() : '';
  if (!title) { toast('请填写主题名称', 'error'); return; }
  const desc = ($('#kf-desc') || {}).value ? $('#kf-desc').value.trim() : '';
  const now = Date.now() / 1000;
  let f = currentKbFamily();
  const isNew = !f;
  if (f) {
    f.title = title;
    f.description = desc;
    f.updated_at = now;
  } else {
    f = { id: 'f_' + Math.random().toString(36).slice(2, 10), title: title,
      description: desc, members: [], created_at: now, updated_at: now };
    state.kbData.families = kbFamilies().concat(f);
  }
  refreshKbFamilySelect();
  drawKbGraph();
  scheduleKbSave();
  toast(isNew ? '主题已保存，可在顶部下拉框选择' : '主题已保存', 'success');
}

function addSelectedToFamily() {
  const f = currentKbFamily();
  if (!f) { toast('先选择或新建一个主题', 'error'); return; }
  if (!state.kbSelNode) { toast('先在图谱里选中一个节点', 'error'); return; }
  f.members = Array.isArray(f.members) ? f.members : [];
  if (!f.members.includes(state.kbSelNode)) f.members.push(state.kbSelNode);
  f.updated_at = Date.now() / 1000;
  refreshKbFamilySelect();
  drawKbGraph();
  scheduleKbSave();
  toast('已加入主题', 'success');
}

function deleteKbFamily() {
  const f = currentKbFamily();
  if (!f) { toast('当前没有选中主题', 'error'); return; }
  if (!window.confirm('删除主题「' + (f.title || '') + '」？节点本身不会删除。')) return;
  state.kbData.families = kbFamilies().filter((x) => x.id !== f.id);
  state.kbFamily = '';
  refreshKbFamilySelect();
  drawKbGraph();
  scheduleKbSave();
  toast('主题已删除', 'success');
}

/* ---------- 节点外观（大小 / 颜色）与图谱力学 ---------- */
// 与后端 NODE_PALETTE 保持一致
const KB_PALETTE = [
  ['red', '#e5484d'], ['orange', '#e8890c'], ['yellow', '#d9a406'],
  ['green', '#30a46c'], ['teal', '#12a594'], ['blue', '#4d6bfe'],
  ['purple', '#8e4ec6'], ['pink', '#d6409f'], ['gray', '#8b8d98'],
];
const KB_GRAPH_DEFAULT = { repel: 9000, center: 0.3, link: 0.04, distance: 130 };

// 把 hex 按比例加深/变浅（amt 为负＝加深）
function shadeColor(hex, amt) {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex || '');
  if (!m) return hex;
  const num = parseInt(m[1], 16);
  const f = (v) => Math.max(0, Math.min(255, Math.round(v + v * amt)));
  const r = f((num >> 16) & 255), g = f((num >> 8) & 255), b = f(num & 255);
  return '#' + [r, g, b].map((v) => v.toString(16).padStart(2, '0')).join('');
}

// hex → rgba(...)，用于从主题色派生出半透明底 / 光晕
function hexAlpha(hex, a) {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex || '');
  if (!m) return hex;
  const num = parseInt(m[1], 16);
  return 'rgba(' + ((num >> 16) & 255) + ',' + ((num >> 8) & 255) + ',' +
    (num & 255) + ',' + a + ')';
}

/* 图谱通用配色：统一从 CSS token 读取，避免 JS 里再维护一份明暗两套颜色。
   知识库、笔记图谱、GAL 关系图都用这一份；主题切换后重绘即可生效。 */
function graphTheme() {
  const cs = getComputedStyle(document.documentElement);
  const pick = (name, fallback) => {
    const v = String(cs.getPropertyValue(name) || '').trim();
    return v || fallback;
  };
  const dark = isDarkTheme();
  const accent = pick('--accent', '#4d6bfe');
  const warn = pick('--warn', '#c98a12');
  return {
    dark: dark,
    graphBg: pick('--kb-graph-bg', dark ? '#17181b' : '#eef0f4'),
    dot: pick('--kb-graph-dot', dark ? '#2c2f35' : '#d5d9e0'),
    nodeBg: pick('--kb-node-bg', dark ? '#3a3f48' : '#ffffff'),
    nodeBorder: pick('--kb-node-border', dark ? '#5e6673' : '#aab2bf'),
    nodeShadow: pick('--kb-node-shadow', 'rgba(0,0,0,.35)'),
    edge: pick('--kb-edge', dark ? '#8f95a1' : '#8b8f99'),
    labelBg: pick('--kb-label-bg', dark ? '#17181b' : '#eef0f4'),
    font: pick('--text', dark ? '#e8eaed' : '#16181d'),
    accent: accent,
    accentSoft: pick('--accent-soft', hexAlpha(accent, .16)),
    warn: warn,
    warnSoft: hexAlpha(warn, .15),
  };
}

// 单个节点的 vis 配色：hex 为空＝默认节点（跟随主题），否则用自定义填充色
function graphNodeColor(hex, th) {
  const t = th || graphTheme();
  if (!hex) {
    return {
      background: t.nodeBg,
      border: t.nodeBorder,
      highlight: { background: t.nodeBg, border: t.accent },
      hover: { background: shadeColor(t.nodeBg, t.dark ? .08 : -.04), border: t.accent },
    };
  }
  const border = shadeColor(hex, -0.28);
  return {
    background: hex,
    border: border,
    highlight: { background: hex, border: t.accent },
    hover: { background: shadeColor(hex, .08), border: border },
  };
}

// 由 state.kbData.graph 生成 vis 物理参数
function kbPhysicsOptions() {
  const g = Object.assign({}, KB_GRAPH_DEFAULT, (state.kbData && state.kbData.graph) || {});
  return {
    // fit 交给 drawKbGraph 在稳定后显式执行：
    // vis 默认会在 stabilization 结束时自动 fit，把随后的 focus 覆盖掉。
    stabilization: { enabled: true, fit: false },
    barnesHut: {
      gravitationalConstant: -Math.abs(Number(g.repel) || KB_GRAPH_DEFAULT.repel),
      centralGravity: Number(g.center),
      springConstant: Number(g.link),
      springLength: Number(g.distance),
    },
  };
}

const KB_SLIDERS = [
  ['#kg-repel', '#kg-repel-val', 'repel', (v) => String(Math.round(v))],
  ['#kg-center', '#kg-center-val', 'center', (v) => Number(v).toFixed(2)],
  ['#kg-link', '#kg-link-val', 'link', (v) => Number(v).toFixed(3)],
  ['#kg-distance', '#kg-distance-val', 'distance', (v) => String(Math.round(v))],
];

// 用当前 kbData.graph 刷新四个滑块与数值
function syncGraphSliders() {
  const g = Object.assign({}, KB_GRAPH_DEFAULT, (state.kbData && state.kbData.graph) || {});
  KB_SLIDERS.forEach(([sel, valSel, key, fmt]) => {
    const el = $(sel);
    if (!el) return;
    el.value = g[key];
    const out = $(valSel);
    if (out) out.textContent = fmt(el.value);
  });
}

// 拖动滑块：实时改物理参数并保存
function bindKbGraphSettings() {
  const apply = () => {
    if (!state.kbData) return;
    const g = Object.assign({}, KB_GRAPH_DEFAULT, state.kbData.graph || {});
    KB_SLIDERS.forEach(([sel, valSel, key, fmt]) => {
      const el = $(sel);
      if (!el) return;
      g[key] = (key === 'center' || key === 'link') ? parseFloat(el.value) : parseInt(el.value, 10);
      const out = $(valSel);
      if (out) out.textContent = fmt(el.value);
    });
    state.kbData.graph = g;
    if (kbNetwork) {
      const p = kbPhysicsOptions().barnesHut;
      kbNetwork.setOptions({ physics: { enabled: true, barnesHut: p } });
    }
    scheduleKbSave();
  };
  KB_SLIDERS.forEach(([sel]) => {
    const el = $(sel);
    if (el) el.oninput = apply;
  });
  const reset = $('#kg-reset');
  if (reset) {
    reset.onclick = () => {
      if (!state.kbData) return;
      state.kbData.graph = Object.assign({}, KB_GRAPH_DEFAULT);
      syncGraphSliders();
      if (kbNetwork) kbNetwork.setOptions({ physics: kbPhysicsOptions() });
      scheduleKbSave();
      toast('已恢复图谱默认');
    };
  }
}

// 渲染节点颜色色块（含「默认」），当前选中高亮
function renderColorSwatches(selected) {
  const box = $('#kd-color');
  if (!box) return;
  state.kbColor = selected || '';
  const items = [['默认', '']].concat(KB_PALETTE);      // [名称, hex]
  box.innerHTML = items.map(([name, hex]) => {
    const on = (state.kbColor || '') === hex ? ' on' : '';
    const cls = 'kb-color' + (hex ? '' : ' kb-color-def') + on;
    const st = hex ? ' style="background:' + hex + '"' : '';
    return '<button type="button" class="' + cls + '"' + st +
           ' data-c="' + hex + '" title="' + (name || '') + '"></button>';
  }).join('');
  box.querySelectorAll('.kb-color').forEach((b) => {
    b.onclick = () => {
      state.kbColor = b.dataset.c || '';
      renderColorSwatches(state.kbColor);
    };
  });
}

function applySessions(r, opts) {
  if (!r) return;
  if (r.sessions) applySessionsPayload(r.sessions, opts);
  renderSessions();
  // 当前会话可能被后端切走（存档恢复 / 删除等）：排队栏也要跟着换会话
  if (typeof renderSendQueue === 'function') renderSendQueue();
  if ('context' in r) renderContextBar(r.context);
}

function refreshKbSelect(selected) {
  const sel = $('#kb-select');
  const s = curSession();
  const bound = (s && s.kb_ids) || [];
  let html = '<option value="session">本会话记忆' +
    (s && s.ai_write === false ? '（AI 写入已关）' : '') + '</option>';
  (state.kbs || []).forEach((k) => {
    const writable = bound.includes(k.id) && k.ai_write !== false;
    html += '<option value="' + k.id + '">' + escapeHtml(k.name) +
      (k.name === '用户画像' ? '（AI 自主记录）' : '') +
      (writable ? ' ⚡AI 写入' : '') + '</option>';
  });
  sel.innerHTML = html;
  sel.value = selected || state.kbCurrent || 'session';
  renderKbAiTarget();
}

// 明确标出 AI 每轮实际会写入哪些目标，避免"看错地方"
function renderKbAiTarget() {
  const el = $('#kb-ai-target');
  if (!el) return;
  const s = curSession();
  const names = [];
  if (s && s.ai_write !== false) names.push('本会话记忆');
  ((s && s.kb_ids) || []).forEach((id) => {
    const m = kbMeta(id);
    if (m && m.ai_write !== false) names.push(m.name);
  });
  const pm = kbMeta(profileKbId());
  const autoOn = !!(state.profile && state.profile.auto_learn_enabled);
  if (pm && autoOn && names.indexOf(pm.name) < 0) names.push(pm.name);
  el.textContent = names.length
    ? 'AI 每轮会写入：' + names.join('、')
    : 'AI 写入已全部关闭';
  el.classList.toggle('off', !names.length);
}

async function openKbModal(initialKey) {
  kbMarkHintSeen();      // 打开弹窗即视为已看到绑定区，侧栏红点不再重复
  const key = initialKey || state.kbCurrent || 'session';
  refreshKbSelect(key);
  renderKbBindList();
  const s = curSession();
  $('#sess-ai-write').checked = !s || s.ai_write !== false;
  showWithAnim($('#kb-modal'));
  replayFadeIn($('#kb-graph'));
  await loadKbGraph(key);
}

// 供工作台「记忆」跳转：打开指定知识库并定位到节点
async function kbOpenNode(kbId, nodeId) {
  state.kbPendingFocus = nodeId ? { id: nodeId, scale: 1.15 } : null;
  await openKbModal(kbId || 'session');
  const node = ((state.kbData && state.kbData.nodes) || [])
    .find((n) => n.id === nodeId);
  if (node) showNodeForm(node);
}
window.kbOpenNode = kbOpenNode;

// 供「已更新记忆」标记跳转：打开本轮写入的库，并高亮新增/修改的节点与连线
async function kbOpenChanges(changed) {
  const keys = Object.keys(changed || {}).filter((k) => {
    const c = changed[k] || {};
    return (c.node_add || []).length || (c.node_update || []).length
      || (c.edge_add || []).length;
  });
  if (!keys.length) return;
  const key = (state.kbCurrent && keys.includes(state.kbCurrent))
    ? state.kbCurrent
    : (keys.includes('session') ? 'session' : keys[0]);
  state.kbChanged = changed;          // 让 drawKbGraph 把本轮变更描边 / 发光
  const c = changed[key] || {};
  const firstNode = (c.node_add || [])[0] || (c.node_update || [])[0];
  const firstEdge = (c.edge_add || [])[0];
  const changedNodes = (c.node_add || []).concat(c.node_update || []);
  // 真正的 focus / fit 交给 drawKbGraph：等物理稳定、canvas 尺寸正确后再做，
  // 否则会被 vis 的自动 fit 或旧画布尺寸覆盖。
  state.kbPendingFocus = firstNode
    ? { id: firstNode, scale: 1.25, select: changedNodes }
    : (firstEdge ? { edgeId: firstEdge, scale: 1.25 } : null);
  await openKbModal(key);
  if (firstNode) {
    const node = ((state.kbData && state.kbData.nodes) || [])
      .find((n) => n.id === firstNode);
    if (node) showNodeForm(node);
  }
}
window.kbOpenChanges = kbOpenChanges;

function closeKbModal() {
  scheduleKbSave();
  state.kbChanged = null;      // 关闭即清除本轮 AI 变更的高亮与提示
  state.kbPendingFocus = null;
  if (kbGraphRO) { try { kbGraphRO.disconnect(); } catch (e) { /* 忽略 */ } kbGraphRO = null; }
  resetKbSearch();
  const mm = $('#kb-more-menu'); if (mm) mm.classList.add('hidden');
  hideWithAnim($('#kb-modal'));
}

/* 「AI 读取这个知识库要花多少 token」的预估。
   必须把三个口径分开写，否则很容易被读成「每轮都花这么多」：
   · 整库规模 = 库里有多少（不是单次成本）
   · 日常每轮 = 检索式注入，有 INJECT_MAX_TOKENS 上限，整库不会全塞进去
   · GAL 编剧 = world_text 全量一次性（另有字数上限）
   · 规则类 = GAL 里每轮常驻，不占检索预算 */
function kbFmtNum(n) {
  const v = Number(n) || 0;
  return v >= 10000 ? (v / 10000).toFixed(1) + ' 万' : String(v);
}

async function renderKbTokenEst(ids) {
  const box = $('#kb-token-est');
  if (!box) return;
  const list = (ids || []).filter(Boolean);
  if (!list.length) {
    box.classList.add('hidden');
    box.innerHTML = '';
    return;
  }
  let r = null;
  try { r = await api('/api/kb/stats', { ids: list }); } catch (e) { r = null; }
  if (!r || !r.ok) { box.classList.add('hidden'); return; }
  const t = r.total || {};
  const cap = Number(r.inject_max_tokens) || 3000;
  const worldChars = Number(r.world_max_chars) || 9000;
  const density = t.chars ? (t.tokens / t.chars) : 0.5;   // 用这个库自己的字/token 密度折算上限
  const worldTokens = Math.round(worldChars * density);
  const lines = [];
  lines.push('<div><b>整库规模</b>：' + (t.libraries || list.length) + ' 个库 · '
    + (t.nodes || 0) + ' 条 · ' + kbFmtNum(t.chars) + '字 ≈ ' + kbFmtNum(t.tokens)
    + ' token <span class="kb-token-hint">（库的总量，不是每次对话的成本）</span></div>');
  lines.push('<div><b>日常每轮</b>：按检索命中注入，最多 <b>' + cap
    + '</b> token / 轮 —— 整库不会全塞进提示词</div>');
  if (r.memory_index_enabled !== false) {
    const idxLines = Number(r.memory_index_max_lines) || 200;
    const idxBytes = Number(r.memory_index_max_bytes) || 25000;
    lines.push('<div><b>常驻索引</b>：标题清单最多 ' + idxLines + ' 行 / '
      + kbFmtNum(idxBytes) + ' 字节（只列标题与一行钩子，正文仍按需检索）</div>');
  }
  lines.push('<div><b>GAL 编剧一次性</b>：整库上限 ' + kbFmtNum(worldChars) + ' 字 ≈ '
    + kbFmtNum(worldTokens) + ' token，每次生成只喂一次</div>');
  if (t.pinned_nodes) {
    lines.push('<div class="kb-token-warn"><b>规则类</b>：' + t.pinned_nodes + ' 条 ≈ '
      + kbFmtNum(t.pinned_tokens) + ' token —— GAL 模式每轮常驻注入，不占检索上限</div>');
  }
  lines.push('<div class="kb-token-hint">按中英混排折算的估算值，实际计费以接口返回的 usage 为准。</div>');
  box.innerHTML = lines.join('');
  box.classList.remove('hidden');
}

/* 会话记忆 / 用户画像的注入量估算。
   两个来源规则不同，文案里必须各自写清上限：
   · 画像：每轮固定注入最多 PROFILE_INJECT_LIMIT 条，其余靠检索
   · 会话记忆：和知识库共用 INJECT_MAX_TOKENS 的检索预算 */
async function renderMemTokenEst() {
  const pbox = $('#mem-est-profile');
  const sbox = $('#mem-est-session');
  if (!pbox && !sbox) return;
  let r = null;
  try {
    r = await api('/api/memory/estimate', { session_id: state.sessions.current });
  } catch (e) { r = null; }
  if (!r || !r.ok) {
    if (pbox) pbox.classList.add('hidden');
    if (sbox) sbox.classList.add('hidden');
    return;
  }
  const p = r.profile || {};
  const s = r.session || {};
  const cap = Number(r.profile_inject_limit) || 30;
  const budget = Number(r.inject_max_tokens) || 3000;
  if (pbox) {
    pbox.innerHTML = '<div><b>用户画像</b>：共 ' + (p.items || 0) + ' 条 · '
      + kbFmtNum(p.chars) + '字 ≈ ' + kbFmtNum(p.tokens) + ' token'
      + ' <span class="kb-token-hint">（每轮固定注入最多 ' + cap
      + ' 条，其余靠检索命中）</span></div>';
    pbox.classList.remove('hidden');
  }
  if (sbox) {
    sbox.innerHTML = '<div><b>会话记忆</b>：本会话共 ' + (s.items || 0) + ' 条 · '
      + kbFmtNum(s.chars) + '字 ≈ ' + kbFmtNum(s.tokens) + ' token'
      + ' <span class="kb-token-hint">（每轮按检索注入，与知识库共用 '
      + budget + ' token/轮 预算）</span></div>';
    sbox.classList.remove('hidden');
  }
}

function renderKbBindList() {
  const box = $('#kb-bind-list');
  const s = curSession();
  const bound = (s && s.kb_ids) || [];
  const pid = profileKbId();
  const list = (state.kbs || []).filter((k) => k.id !== pid);
  const warn = (list.length && !bound.length)
    ? '<div class="kb-bind-warn">' + icon('alert') + ' 本会话还没有绑定任何知识库，AI 读不到它们的内容'
      + '（世界设定 / 背景设定会被忽略）。勾选下面的库即可。</div>'
    : '';
  box.innerHTML = warn + (list.length
    ? list.map((k) => '<label class="check-row"><input type="checkbox" data-kbid="' + k.id + '"' +
        (bound.includes(k.id) ? ' checked' : '') + '> ' + escapeHtml(k.name) + '</label>').join('')
    : '<div class="hint">还没有通用知识库，点上方「新建」按钮创建一个。</div>');
  box.querySelectorAll('input[data-kbid]').forEach((cb) => {
    cb.onchange = async () => {
      const ids = Array.from(box.querySelectorAll('input[data-kbid]:checked')).map((x) => x.dataset.kbid);
      applySessions(await api('/api/session/bind', { id: state.sessions.current, kb_ids: ids }));
      // 绑定变化会改变 AI 的写入目标，刷新下拉标注与提示行
      refreshKbSelect($('#kb-select').value || state.kbCurrent);
      renderKbBindList();          // 绑定后立即更新警告行与侧栏角标
    };
  });
  renderKbAiTarget();
  renderKbBadge();
  renderKbTokenEst(bound);        // 「AI 读取预估」跟着绑定变化刷新
  renderMemTokenEst();            // 会话记忆 / 用户画像的注入量估算
  // 会话级「读取用户画像」：默认开；GAL 剧场有自己的读画像开关，这里隐藏
  const upEl = $('#sess-use-profile');
  if (upEl) upEl.checked = !s || s.use_profile !== false;
  const upRow = $('#kb-use-profile-row');
  if (upRow) upRow.classList.toggle('hidden', !!(s && s.gal_scope === 'gal'));
  // 会话级「读取会话记忆 / 知识库」：默认开；GAL 剧场用 gal_use_kb 单独控制，这里隐藏
  const umEl = $('#sess-use-memory');
  if (umEl) umEl.checked = !s || s.use_memory !== false;
  const umRow = $('#kb-use-memory-row');
  if (umRow) umRow.classList.toggle('hidden', !!(s && s.gal_scope === 'gal'));
}

const KB_HINT_KEY = 'kb_hint_seen';

// 侧栏知识库红点只做一次引导：看过绑定区后就不再重复提醒
function kbHintSeen() {
  try { return localStorage.getItem(KB_HINT_KEY) === '1'; } catch (e) { return false; }
}
function kbMarkHintSeen() {
  try { localStorage.setItem(KB_HINT_KEY, '1'); } catch (e) { /* 隐私模式：下次仍会提示 */ }
}

// 侧栏「知识库」按钮：当前会话有可绑库却未绑定时，标一个提醒（红点只出现一次）
function renderKbBadge() {
  const btn = $('#btn-kb');
  if (!btn) return;
  const s = curSession();
  const pid = profileKbId();
  const hasKb = (state.kbs || []).some((k) => k.id !== pid);
  const unbound = hasKb && !((s && s.kb_ids) || []).length;
  // 未绑定时仍保留悬停说明，红点见过一次就不再显示
  btn.classList.toggle('kb-unbound', unbound && !kbHintSeen());
  btn.title = unbound ? '本会话未绑定知识库，点此绑定' : '知识库 / 记忆图谱';
}

async function loadKbGraph(key) {
  clearTimeout(kbSaveTimer);
  const token = ++kbLoadSeq;      // 防止快速切换时旧请求覆盖新结果
  state.kbCurrent = key;
  state.kbData = { nodes: [], edges: [], graph: {} };
  state.kbError = null;
  state.kbFamily = '';
  showNodeForm(null);
  resetKbSearch();          // 换库时清掉上一次的搜索
  const promptBox = $('#kb-prompt');
  const promptSect = $('#kb-sect-prompt');
  const promptSave = $('#kb-prompt-save');
  const promptStatus = $('#kb-prompt-status');
  if (promptBox) promptBox.value = '';
  if (promptSect) promptSect.classList.add('hidden');
  if (promptSave) promptSave.disabled = true;
  if (promptStatus) promptStatus.textContent = '';
  let data = null;
  try {
    if (key === 'session') {
      const r = await api('/api/memory/get', { session_id: state.sessions.current });
      if (token !== kbLoadSeq) return;
      if (!r.ok) state.kbError = '会话记忆读取失败：' + (r.error || '');
      else {
        data = { nodes: r.memory.nodes || [], edges: r.memory.edges || [],
          graph: r.memory.graph || {}, families: r.memory.families || [] };
        $('#kb-ai-write').checked = r.ai_write !== false;
      }
      $('#kb-ai-write').disabled = true;
      $('#kb-rename').disabled = true;
      $('#kb-del').disabled = true;
    } else {
      const r = await api('/api/kb/get', { id: key });
      if (token !== kbLoadSeq) return;
      if (!r.ok) state.kbError = '知识库读取失败：' + (r.error || '');
      else {
        data = { nodes: r.kb.nodes || [], edges: r.kb.edges || [],
          graph: r.kb.graph || {}, families: r.kb.families || [] };
        const meta = kbMeta(key);
        const isProfile = !!meta && meta.name === '用户画像';
        $('#kb-ai-write').checked = meta ? meta.ai_write !== false : true;
        $('#kb-rename').disabled = isProfile;   // 系统库不可重命名/删除
        $('#kb-del').disabled = isProfile;
        if (promptBox) promptBox.value = (r.kb && r.kb.prompt) || '';
        if (promptSect) promptSect.classList.remove('hidden');
        if (promptSave) promptSave.disabled = false;
      }
      $('#kb-ai-write').disabled = false;
    }
  } catch (err) {
    if (token !== kbLoadSeq) return;
    state.kbError = '知识库加载失败：' + (err && err.message ? err.message : String(err));
  }
  // 无论成功失败都要落到画布上，避免"数据写进去了但界面一片空白"
  state.kbData = data || { nodes: [], edges: [], graph: {}, families: [] };
  if (!state.kbData.graph) state.kbData.graph = {};
  if (!Array.isArray(state.kbData.families)) state.kbData.families = [];
  refreshKbFamilySelect();
  syncGraphSliders();
  // 图谱初始化（vis-network）不便宜：让弹窗先完成一次绘制，再画图
  requestAnimationFrame(() => {
    if (token === kbLoadSeq) drawKbGraph();
  });
}

// vis-network 的语言表（知识库图谱 / 笔记图谱共用）。
// 注意：locales 是「整体替换」内置表，且 vis 内部会回退读取 locales["en"]["close"]，
// 所以 en 与 cn 都必须给全，否则初始化时抛 "Cannot read properties of undefined (reading 'close')"。
const VIS_LOCALES = {
  en: {
    edit: 'Edit', del: 'Delete selected', back: 'Back',
    addNode: 'Add Node', addEdge: 'Add Edge', editNode: 'Edit Node', editEdge: 'Edit Edge',
    addDescription: 'Click in an empty space to place a new node.',
    edgeDescription: 'Click on a node and drag the edge to another node to connect them.',
    editEdgeDescription: 'Click on the control points and drag them to a node to connect to it.',
    createEdgeError: 'Cannot link edges to a cluster.',
    deleteClusterError: 'Clusters cannot be deleted.',
    editClusterError: 'Clusters cannot be edited.',
    close: 'Close',
  },
  cn: {
    edit: '编辑', del: '删除所选', back: '返回',
    addNode: '添加节点', addEdge: '添加连线', editNode: '编辑节点', editEdge: '编辑连线',
    addDescription: '单击空白处放置新节点。',
    edgeDescription: '按住鼠标从一个节点拖到另一个节点即可连线。',
    editEdgeDescription: '拖动控制点到另一个节点以改接连线。',
    createEdgeError: '无法把连线连接到边。',
    deleteClusterError: '无法删除群组。',
    editClusterError: '无法编辑群组。',
    close: '关闭',
  },
};

/* 关系类型：颜色 + 取词（词表本体由后端 /api/state 下发，避免两处维护） */
const RELATION_COLORS = {
  related: '#8a8a94', isa: '#4d6bfe', partof: '#12a594', attribute: '#8e5cf7',
  same: '#2ea043', opposite: '#e5534b', instance: '#d9822b', cause: '#c2410c',
  condition: '#0d8fd9', sequence: '#0891b2', means: '#16a34a', purpose: '#7c3aed',
  turn: '#a16207',
};

// th 可选：传入主题时，「相关」改用主题的连线色，避免和画布底色撞在一起
function relationColor(code, th) {
  if (th && (!code || code === 'related')) return th.edge;
  return RELATION_COLORS[code] || (th ? th.edge : '#8a8a94');
}

function relationMeta(code) {
  return (state.relations || []).find((r) => r.code === code) || null;
}

// 已下线的旧关系码：词表里不再提供，但老图上的边要照旧显示中文名
const LEGACY_REL_LABEL = { sequence: '顺承', turn: '转折' };

function relationLabel(code) {
  const m = relationMeta(code);
  return m ? m.label : (LEGACY_REL_LABEL[code] || code || '相关');
}

/* 对称关系（相关 / 同义 / 对立）默认画双向箭头；sym 标记由后端词表下发 */
const SYM_RELATIONS_FALLBACK = ['related', 'same', 'opposite'];

function isSymmetricRel(code) {
  const m = relationMeta(code);
  if (m && typeof m.sym === 'boolean') return m.sym;
  return SYM_RELATIONS_FALLBACK.indexOf(code || 'related') >= 0;
}

// 老数据没有 directed 字段时，按关系类型推断
function isEdgeDirected(e) {
  if (e && typeof e.directed === 'boolean') return e.directed;
  return !isSymmetricRel(e && e.relation);
}

function edgeArrows(directed) {
  const s = 0.7;          // 0.55 在深色 / 高 DPI 下箭头太小，看不清方向
  return directed
    ? { to: { enabled: true, scaleFactor: s } }
    : { to: { enabled: true, scaleFactor: s },
        from: { enabled: true, scaleFactor: s } };
}

// 边的悬浮说明：关系名 + 一句话陈述；老数据没有 fact 就直说「未写依据」
function edgeTooltip(e) {
  const rel = (e && (e.label || relationLabel(e.relation))) || '相关';
  const fact = String((e && e.fact) || '').trim();
  let s = fact ? (rel + '：' + fact) : (rel + '（未写依据）');
  if (e && e.invalid_at) s = '已失效 · ' + s;
  return s;
}

function renderRelationHelp() {
  const box = $('#help-relations');
  if (!box) return;
  const list = state.relations || [];
  box.innerHTML = list.length
    ? list.map((r) => '<li><span style="color:' + relationColor(r.code) + '">●</span> ' +
        '<strong>' + escapeHtml(r.label) + '</strong>（' + escapeHtml(r.code) + '）：' +
        escapeHtml(r.desc) + (r.sym ? '　<b>⇄ 默认双向</b>' : '') + '</li>').join('')
    : '<li class="hint">关系词表由后端下发，加载后显示。</li>';
}

function fillRelationSelect(sel, selected) {
  const list = state.relations || [];
  sel.innerHTML = list.length
    ? list.map((r) => '<option value="' + escapeHtml(r.code) + '">' + escapeHtml(r.label) +
        ' － ' + escapeHtml(r.desc) + '</option>').join('')
    : '<option value="related">相关</option>';
  sel.value = selected || 'related';
  if (!sel.value) sel.value = 'related';
}

// 图谱顶部的「AI 本轮改动」提示条；点「知道了」清除高亮。本轮无变更则不显示。
function renderKbChangeTip(addNodes, updNodes, addEdges, graphEl) {
  const n = addNodes.size, u = updNodes.size, e = addEdges.size;
  if (!n && !u && !e) return;
  const bits = [];
  if (n) bits.push('新增 ' + n + ' 个节点');
  if (u) bits.push('修改 ' + u + ' 个节点');
  if (e) bits.push('新增 ' + e + ' 条连线');
  const tip = document.createElement('div');
  tip.id = 'kb-change-tip';
  tip.className = 'kb-change-tip';
  tip.innerHTML = icon('bulb') + ' <span>AI 本轮改动：' + bits.join(' · ') + '</span>';
  // 变更节点序列（新增在前、修改在后）。本轮常常同时改好几个，只高亮不跳转的话
  // 用户得自己在画布上一个个找，所以下面给一对上/下一个箭头。
  const seq = [];
  Array.from(addNodes).concat(Array.from(updNodes))
    .forEach((id) => { if (!seq.includes(id)) seq.push(id); });
  // 「定位」：用户拖走视角后，可以一键回到本轮第一个变更节点
  const firstEdgeId = Array.from(addEdges)[0];
  let focusId = seq[0] || '';
  if (!focusId && firstEdgeId) {
    const edge = ((state.kbData && state.kbData.edges) || [])
      .find((x) => x.id === firstEdgeId);
    if (edge) focusId = edge.from;
  }
  if (focusId) {
    const locate = document.createElement('button');
    locate.type = 'button';
    locate.className = 'kb-change-ok';
    locate.textContent = '定位';
    locate.onclick = () => kbFocusNode(focusId);
    tip.appendChild(locate);
  }
  // 上/下一个：循环切换；切到哪个就把哪个的详情同步到右侧面板
  if (seq.length > 1) {
    let idx = 0;
    const nav = document.createElement('span');
    nav.className = 'kb-change-nav';
    const count = document.createElement('span');
    count.className = 'kb-change-count';
    count.textContent = '1/' + seq.length;
    const arrow = (dir, title) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'kb-change-arrow';
      b.title = title;
      b.innerHTML = icon('chevron', dir < 0 ? 'left' : 'right');
      b.onclick = () => {
        idx = (idx + dir + seq.length) % seq.length;
        count.textContent = (idx + 1) + '/' + seq.length;
        kbFocusNode(seq[idx]);
      };
      return b;
    };
    nav.appendChild(arrow(-1, '上一个变更节点'));
    nav.appendChild(count);
    nav.appendChild(arrow(1, '下一个变更节点'));
    tip.appendChild(nav);
  }
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'kb-change-ok';
  btn.textContent = '知道了';
  btn.onclick = () => { state.kbChanged = null; drawKbGraph(); };
  tip.appendChild(btn);
  graphEl.parentNode.insertBefore(tip, graphEl);
}

// 空态统一外观：图标 + 说明居中，避免只剩一行灰字像渲染失败
function kbEmptyHtml(ico, msg) {
  return '<div class="kb-fallback"><div class="kb-empty">' + icon(ico) +
    '<p class="hint">' + escapeHtml(msg) + '</p></div></div>';
}

// 显式列表视图：按分类分组，点击条目打开右侧节点详情（不创建 vis 实例）
function renderKbList(el, nodes, allNodes, family) {
  if (!nodes.length) {
    const msg = allNodes.length
      ? ('主题「' + String((family && family.title) || '') + '」还没有成员。选中节点后点侧栏的「加入选中节点」。')
      : '这个知识库还是空的 —— 可以点上方「节点」手动新增，也可以直接在对话里让 AI 自己记。';
    el.innerHTML = kbEmptyHtml('brain', msg);
    return;
  }
  const ch = (state.kbChanged && state.kbChanged[state.kbCurrent]) || null;
  const addNodes = new Set((ch && ch.node_add) || []);
  const updNodes = new Set((ch && ch.node_update) || []);
  const groups = {};
  nodes.forEach((n) => {
    const c = String(n.category || '其他').trim() || '其他';
    (groups[c] = groups[c] || []).push(n);
  });
  const cats = Object.keys(groups).sort((a, b) =>
    groups[b].length - groups[a].length || a.localeCompare(b));
  let html = '<div class="kb-fallback">';
  cats.forEach((cat) => {
    html += '<div class="kb-fgroup">' + escapeHtml(cat) + '（' + groups[cat].length +
            '）</div><ul class="kb-flist">';
    groups[cat].forEach((n) => {
      const cls = [addNodes.has(n.id) ? 'is-new' : (updNodes.has(n.id) ? 'is-chg' : ''),
                   state.kbSelNode === n.id ? 'sel' : ''].filter(Boolean).join(' ');
      const tags = (n.tags || []).map((t) =>
        '<span class="kb-fcat">' + escapeHtml(String(t)) + '</span>').join('');
      const body = String(n.content || '');
      html += '<li data-id="' + escapeHtml(String(n.id)) + '"' +
        (cls ? ' class="' + cls + '"' : '') + '>' +
        '<b>' + escapeHtml(String(n.title || '(无标题)')) + '</b>' +
        (tags ? ' ' + tags : '') +
        (body ? '<div class="kb-fcontent">' + escapeHtml(body.slice(0, 160)) +
                (body.length > 160 ? '…' : '') + '</div>' : '') +
        '</li>';
    });
    html += '</ul>';
  });
  html += '</div>';
  el.innerHTML = html;
  el.querySelectorAll('li[data-id]').forEach((li) => {
    li.onclick = () => {
      const n = allNodes.find((x) => x.id === li.dataset.id);
      if (!n) return;
      kbHideBulk();          // 列表视图没有多选，清掉图谱里可能残留的批量条
      el.querySelectorAll('li.sel').forEach((x) => x.classList.remove('sel'));
      li.classList.add('sel');
      showNodeForm(n);
    };
  });
}

// 切换「图谱 / 列表」视图
function kbSetView(view) {
  kbView = (view === 'list') ? 'list' : 'graph';
  kbHideBulk();
  const bg = $('#kb-view-graph'), bl = $('#kb-view-list');
  if (bg) bg.classList.toggle('active', kbView === 'graph');
  if (bl) bl.classList.toggle('active', kbView === 'list');
  drawKbGraph();
}

function drawKbGraph() {
  const el = $('#kb-graph');
  const oldTip = document.getElementById('kb-change-tip');
  if (oldTip) oldTip.remove();          // 每次重绘先清掉旧的「AI 改动」提示条
  if (kbGraphRO) { try { kbGraphRO.disconnect(); } catch (e) { /* 忽略 */ } kbGraphRO = null; }
  // 销毁旧实例：否则多个 vis.Network 会共用容器，出现"上一个库的节点还在"的残影
  if (kbNetwork) { try { kbNetwork.destroy(); } catch (e) { } kbNetwork = null; }
  if (!state.kbData) { state.kbPendingFocus = null; el.innerHTML = ''; return; }
  const nodesData = state.kbData.nodes || [];
  const edgesData = state.kbData.edges || [];
  const family = currentKbFamily();
  const memberSet = family ? new Set(family.members || []) : null;
  const visibleNodes = memberSet ? nodesData.filter((n) => memberSet.has(n.id)) : nodesData;
  const visibleEdges = memberSet
    ? edgesData.filter((e) => memberSet.has(e.from) && memberSet.has(e.to)) : edgesData;

  // 兜底视图：图谱库缺失或渲染异常时，用列表把节点显示出来（仍可点选编辑），
  // 避免"数据写进去了但界面一片空白"。
  const fallback = (msg) => {
    state.kbPendingFocus = null;
    if (!nodesData.length) {
      el.innerHTML = kbEmptyHtml('graph', msg + '（暂无节点，点上方「节点」按钮新增）');
      return;
    }
    el.innerHTML = '<div class="kb-fallback"><p class="hint">' + escapeHtml(msg) + '</p>' +
      '<ul class="kb-flist">' + nodesData.map((n, i) =>
        '<li data-i="' + i + '"><b>' + escapeHtml(String(n.title || '(无标题)')) + '</b>' +
        (n.category ? '<span class="kb-fcat">' + escapeHtml(String(n.category)) + '</span>' : '') +
        (n.content ? '<div class="kb-fcontent">' + escapeHtml(String(n.content)) + '</div>' : '') +
        '</li>').join('') + '</ul></div>';
    el.querySelectorAll('li[data-i]').forEach((li) => {
      li.onclick = () => showNodeForm(nodesData[Number(li.dataset.i)]);
    });
  };

  if (state.kbError) { fallback(state.kbError); return; }
  // 手动切到列表视图：不创建 vis 实例（节点多时更易读，也不受网络影响）
  if (kbView === 'list') {
    state.kbPendingFocus = null;
    renderKbList(el, visibleNodes, nodesData, family);
    return;
  }
  if (!window.vis || !window.vis.Network || !window.vis.DataSet) {
    fallback('图谱库 vis-network 未加载（本地文件缺失且 CDN 不可用）；已切换为列表视图，可正常查看与编辑。');
    return;
  }
  if (!nodesData.length) {          // 空库：给明确空态，否则会渲染出一片空白画布，像是坏了
    state.kbPendingFocus = null;
    el.innerHTML = kbEmptyHtml('brain', '这个知识库还是空的 —— '
      + '可以点上方「节点」手动新增，也可以直接在对话里让 AI 自己记（它会用记忆工具写进来）。');
    return;
  }
  if (family && !visibleNodes.length) {
    state.kbPendingFocus = null;
    el.innerHTML = kbEmptyHtml('graph', '主题「' + String(family.title || '') + '」还没有成员。'
      + '选中节点后点侧栏的「加入选中节点」。');
    return;
  }
  el.innerHTML = '';
  // 本轮 AI 变更（当前库）：新增/改过的节点与新增的连线要高亮出来
  const ch = (state.kbChanged && state.kbChanged[state.kbCurrent]) || null;
  const addNodes = new Set((ch && ch.node_add) || []);
  const updNodes = new Set((ch && ch.node_update) || []);
  const addEdges = new Set((ch && ch.edge_add) || []);
  renderKbChangeTip(addNodes, updNodes, addEdges, el);
  const th = graphTheme();
  const nodes = visibleNodes.map((n) => {
    const baseSz = (typeof n.size === 'number' && n.size > 0) ? n.size : 16;
    const notes = Array.isArray(n.notes) ? n.notes : [];
    const noteGrow = notes.length ? Math.min(10, 10 * (1 - Math.exp(-notes.length / 8))) : 0;
    const sz = baseSz + noteGrow;
    const o = {
      id: n.id, label: String(n.title || ''), title: String(n.content || '').slice(0, 200),
      shape: 'dot', size: sz, borderWidth: 2,
      // 默认节点不再用白/黑同色，填充 + 描边 + 阴影一起把节点从画布上"抬起来"
      color: graphNodeColor(n.color, th),
      shadow: { enabled: true, color: th.nodeShadow, size: 7, x: 0, y: 1 },
      font: { color: th.font, size: sz >= 26 ? 15 : 14,
              strokeWidth: 3, strokeColor: th.labelBg },
    };
    if (notes.length) {                  // 追加越多越亮：尺寸 + 暖色光晕，有上限
      o.borderWidth = 2 + Math.min(2.5, notes.length * 0.4);
      o.shadow = { enabled: true, color: 'rgba(245,158,11,.35)', size: 6 + noteGrow };
    }
    if (addNodes.has(n.id) || updNodes.has(n.id)) {   // 本轮 AI 新增/改过的节点：描边 + 光晕 + 标签底色
      const isNew = addNodes.has(n.id);
      const hl = isNew ? '#22c55e' : '#f59e0b';
      const bg = o.color.background;
      o.borderWidth = 4;
      o.color = { background: bg, border: hl,
                  highlight: { background: bg, border: hl },
                  hover: { background: bg, border: hl } };
      o.shadow = { enabled: true, size: 22, x: 0, y: 0,
                   color: isNew ? 'rgba(34,197,94,.75)' : 'rgba(245,158,11,.75)' };
      o.font = Object.assign({}, o.font, {
        background: isNew ? 'rgba(34,197,94,.16)' : 'rgba(245,158,11,.16)',
      });
      o.label = String(n.title || '') + (isNew ? '  · 新' : '  · 改');
    }
    let px = null, py = null;
    if (typeof n.x === 'number' && typeof n.y === 'number') {
      px = n.x; py = n.y;
    }
    if (px !== null && py !== null) { o.x = px; o.y = py; }
    return o;
  });
  const edges = visibleEdges.map((e) => {
    const rel = e.relation || 'related';
    const col = relationColor(rel, th);
    const dead = !!e.invalid_at;             // S2：被新结论替代的旧边，虚线 + 淡化
    // 标签加画布色描边：彩色小字压在边和点阵上时不会糊成一团
    const o = {
      id: e.id, from: e.from, to: e.to,
      label: (e.label || relationLabel(rel)) + (dead ? '（已失效）' : ''),
      title: edgeTooltip(e),
      font: { color: col, size: dead ? 10 : 11, align: 'middle',
              strokeWidth: 3, strokeColor: th.labelBg },
      color: { color: col, highlight: col, hover: col,
               opacity: dead ? 0.4 : 1 },
      arrows: edgeArrows(isEdgeDirected(e)),
      width: dead ? 1.1 : 1.6,
    };
    if (dead) o.dashes = [4, 5];
    if (addEdges.has(e.id)) {            // 本轮 AI 新增的连线：加粗 + 绿色
      o.width = 3;
      o.color = { color: '#22c55e', highlight: '#22c55e', hover: '#22c55e' };
      o.font = { color: '#22c55e', size: 11, align: 'middle',
                 strokeWidth: 3, strokeColor: th.labelBg };
    }
    return o;
  });
  // 主题成员之间的“星座链”：只用于视觉，不写入 edges，避免污染语义关系
  const familyEdges = [];
  if (family && Array.isArray(family.members) && family.members.length > 1) {
    const ids = family.members.filter((id) => visibleNodes.some((n) => n.id === id));
    for (let i = 1; i < ids.length; i++) {
      familyEdges.push({
        id: 'family_' + family.id + '_' + i,
        from: ids[i - 1], to: ids[i],
        color: { color: 'rgba(245,158,11,.55)' }, width: 1.5, dashes: [4, 4],
        arrows: { to: { enabled: false } }, font: { size: 0 }, title: '',
      });
    }
  }
  const allEdges = edges.concat(familyEdges);
  try {
    const ds = { nodes: new vis.DataSet(nodes), edges: new vis.DataSet(allEdges) };
    kbNetwork = new vis.Network(el, ds, {
    physics: kbPhysicsOptions(),
    interaction: { hover: true, multiselect: true },
    nodes: { borderWidthSelected: 4 },   // 选中的节点（含搜索命中）描边更粗
    locale: 'cn',
    locales: VIS_LOCALES,
    manipulation: {
      enabled: true, initiallyActive: false,
      // vis 9.x 只接受函数；传 false 会在控制台报 "Expected function" 并让整组选项失效
      addNode: (data, cb) => { cb(null); },
      editNode: (data, cb) => { cb(null); },
      addEdge: (data, cb) => {
        // 拖拽连线不直接落库：先打开表单补「为什么」，没有陈述的边等于噪声
        cb(null);
        showEdgeForm(data.from);
        const toSel = $('#ke-to');
        if (toSel) toSel.value = data.to;
        toast('请补一句话说明这条关系的依据');
      },
      editEdge: (data, cb) => { cb(data); },
      deleteEdge: (data, cb) => {
        (data.edges || []).forEach((eid) => {
          state.kbData.edges = state.kbData.edges.filter((e) => e.id !== eid);
        });
        cb(data);
        scheduleKbSave();
      },
    },
  });
  kbNetwork._ds = ds;
  kbNetwork.on('click', (p) => {
    const ids = (p.nodes || []).slice();
    if (ids.length > 1) {                 // 多选：进入批量模式，不显示单节点表单
      kbShowBulk(ids);
      showNodeForm(null);
      return;
    }
    kbHideBulk();
    if (ids.length) showNodeForm(state.kbData.nodes.find((n) => n.id === ids[0]));
    else showNodeForm(null);
  });
  kbNetwork.on('dragEnd', (p) => {
    if (!p.nodes || !p.nodes.length) return;
    const pos = kbNetwork.getPositions(p.nodes);
    p.nodes.forEach((id) => {
      const n = state.kbData.nodes.find((x) => x.id === id);
      if (n && pos[id]) { n.x = pos[id].x; n.y = pos[id].y; }
    });
    scheduleKbSave();
  });
  kbNetwork.on('doubleClick', (p) => {
    if ((p.nodes && p.nodes.length) || (p.edges && p.edges.length)) return;
    addKbNode(p.pointer && p.pointer.canvas);
  });
  // 容器尺寸可能到弹窗打开后才稳定（窄屏上下堆叠、侧栏内容加载），
  // canvas 必须跟着容器走，否则 focus 只会在旧画布的一半高度里居中。
  if (typeof ResizeObserver !== 'undefined') {
    kbGraphRO = new ResizeObserver(() => {
      if (kbNetwork) kbFitCanvas();
    });
    kbGraphRO.observe(el);
  }
  requestAnimationFrame(() => { if (kbNetwork) kbFitCanvas(); });
  // stabilization.fit=false：等物理稳定后自己决定 fit 全图还是 focus 目标节点
  let settled = false;
  let settleTimer = null;
  const settle = () => {
    if (settled || !kbNetwork) return;
    settled = true;
    clearTimeout(settleTimer);
    try { kbApplyPendingFocus(); } catch (e) { /* 忽略 */ }
  };
  settleTimer = setTimeout(settle, 1200);
  kbNetwork.once('stabilizationIterationsDone', settle);
  } catch (err) {
    kbNetwork = null;
    fallback('图谱渲染失败：' + (err && err.message ? err.message : String(err)) + '；已切换为列表视图。');
  }
}

function fmtKbNoteTime(ts) {
  const d = new Date((Number(ts) || 0) * 1000);
  if (!Number.isFinite(d.getTime())) return '';
  const p = (n) => String(n).padStart(2, '0');
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) +
    ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
}

function renderKbNotes(node) {
  const box = $('#kd-notes-list');
  if (!box) return;
  const notes = (node && Array.isArray(node.notes)) ? node.notes : [];
  box.innerHTML = notes.length
    ? notes.map((n) => '<div class="kb-note"><b>' + fmtKbNoteTime(n.created_at) +
        '</b>' + escapeHtml(String(n.content || '')) + '</div>').join('')
    : '<div class="hint">还没有追加</div>';
  const input = $('#kd-note-input');
  if (input) input.value = '';
}

function addKbNodeNote() {
  const n = (state.kbData && state.kbData.nodes || []).find((x) => x.id === state.kbSelNode);
  const input = $('#kd-note-input');
  const text = input ? input.value.trim() : '';
  if (!n || !text) return;
  const now = Date.now() / 1000;
  n.notes = Array.isArray(n.notes) ? n.notes : [];
  n.notes.push({ id: 'note_' + Math.random().toString(36).slice(2, 10),
    content: text, created_at: now, source: 'user' });
  n.updated_at = now;
  if (input) input.value = '';
  drawKbGraph();
  showNodeForm(n);
  scheduleKbSave();
}

function showNodeForm(node) {
  // 打开节点详情时自动收起「新增连线」面板
  kbEdgeFrom = null;
  $('#kb-edge-form').classList.add('hidden');
  if (!node) {
    state.kbSelNode = null;
    $('#kb-node-form').classList.add('hidden');
    $('#kb-node-empty').classList.remove('hidden');
    return;
  }
  state.kbSelNode = node.id;
  const sect = $('#kb-sect-node'); if (sect) sect.open = true;   // 选中节点时展开「节点详情」
  $('#kb-node-form').classList.remove('hidden');
  $('#kb-node-empty').classList.add('hidden');
  $('#kd-title').value = node.title || '';
  $('#kd-category').value = node.category || '';
  $('#kd-content').value = node.content || '';
  $('#kd-tags').value = (node.tags || []).join(', ');
  $('#kd-aliases').value = (node.aliases || []).join(', ');
  renderKbLinks(node);
  const sz = (typeof node.size === 'number' && node.size > 0) ? node.size : 16;
  $('#kd-size').value = sz;
  $('#kd-size-val').textContent = sz;
  renderColorSwatches(node.color || '');
  $('#kd-locked').checked = !!node.locked;
  // 原话锚点：只有「AI 在对话里写下」的条目才有 source + source_msg
  const originBtn = $('#kd-origin');
  if (originBtn) {
    const ok = !!(node.source && typeof node.source_msg === 'number');
    originBtn.classList.toggle('hidden', !ok);
    originBtn.title = ok ? '回到写下这条记忆的那条回复' : '';
  }
  renderKbNotes(node);
}

// 回到写下这条记忆的那条回复。消息数组不删（压缩只移动 summary_upto），下标稳定。
async function kbJumpToOrigin() {
  const nodes = (state.kbData && state.kbData.nodes) || [];
  const n = nodes.find((x) => x.id === state.kbSelNode);
  if (!n || !n.source || typeof n.source_msg !== 'number') return;
  const sid = n.source, idx = n.source_msg;
  closeKbModal();
  try {
    if (typeof selectSession === 'function') await selectSession(sid);
    if (typeof ssFocusMessage === 'function') await ssFocusMessage(idx, '');
  } catch (e) {
    toast('跳转失败：原会话可能已删除');
  }
}

// ---------- 关联：入边 + 未链接提及 ----------
function renderKbLinks(node) {
  const box = $('#kd-links');
  if (!box) return;
  const nodes = (state.kbData && state.kbData.nodes) || [];
  const edges = (state.kbData && state.kbData.edges) || [];
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const rows = [];
  const linked = new Set();
  edges.forEach((e) => {
    if (e.from !== node.id && e.to !== node.id) return;
    const otherId = e.from === node.id ? e.to : e.from;
    const other = byId.get(otherId);
    if (!other) return;
    linked.add(otherId);
    const rel = e.label || relationLabel(e.relation);
    const dir = e.from === node.id ? '→' : (e.directed ? '←' : '⇄');
    const dead = e.invalid_at ? '已失效 · ' : '';
    rows.push({ id: otherId, title: other.title || '(无标题)',
                meta: dead + rel + ' ' + dir, tip: edgeTooltip(e) });
  });
  const title = String(node.title || '').trim();
  if (title) {
    nodes.forEach((n) => {
      if (n.id === node.id || linked.has(n.id)) return;
      const hay = [n.title].concat(n.aliases || []).map((x) => String(x || '')).join(' ');
      if (hay.indexOf(title) >= 0) {
        rows.push({ id: n.id, title: n.title || '(无标题)', meta: '提及（未连线）' });
      }
    });
  }
  if (!rows.length) {
    box.innerHTML = '<div class="hint">还没有关联。点「连线」建立关系，或在别的条目里提到它。</div>';
    return;
  }
  box.innerHTML = '';
  rows.slice(0, 30).forEach((r) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'kb-link-row';
    if (r.tip) b.title = r.tip;
    const t = document.createElement('span');
    t.className = 'kb-link-title';
    t.textContent = r.title;
    const m = document.createElement('span');
    m.className = 'kb-link-meta';
    m.textContent = r.meta;
    b.appendChild(t);
    b.appendChild(m);
    b.onclick = () => kbFocusNode(r.id);
    box.appendChild(b);
  });
}

function addKbNode(pos) {
  const now = Date.now() / 1000;
  const n = {
    id: 'n_' + Math.random().toString(36).slice(2, 10),
    title: '新节点', content: '', category: '其他', tags: [], aliases: [], locked: false,
    notes: [], x: null, y: null, created_at: now, updated_at: now,
  };
  state.kbData.nodes.push(n);
  if (kbNetwork && kbNetwork._ds) {
    const th = graphTheme();
    const vn = { id: n.id, label: n.title, title: '', shape: 'dot', size: 16, borderWidth: 2,
      color: graphNodeColor(null, th),
      shadow: { enabled: true, color: th.nodeShadow, size: 7, x: 0, y: 1 },
      font: { color: th.font, size: 14, strokeWidth: 3, strokeColor: th.labelBg } };
    if (pos) { n.x = pos.x; n.y = pos.y; vn.x = pos.x; vn.y = pos.y; }
    kbNetwork._ds.nodes.add(vn);
  } else {
    drawKbGraph();   // 列表兜底视图下也要让新节点立刻可见
  }
  scheduleKbSave();
  showNodeForm(n);
}

function applyNodeForm() {
  const n = state.kbData.nodes.find((x) => x.id === state.kbSelNode);
  if (!n) return;
  n.title = $('#kd-title').value.trim() || n.title;
  n.category = $('#kd-category').value.trim() || '其他';
  n.content = $('#kd-content').value;
  n.tags = $('#kd-tags').value.split(',').map((t) => t.trim()).filter(Boolean);
  n.aliases = $('#kd-aliases').value.split(',').map((t) => t.trim()).filter(Boolean).slice(0, 8);
  n.locked = $('#kd-locked').checked;
  const sz = parseInt($('#kd-size').value, 10);
  if (sz && sz !== 16) n.size = sz; else delete n.size;
  if (state.kbColor) n.color = state.kbColor; else delete n.color;
  n.updated_at = Date.now() / 1000;
  if (kbNetwork && kbNetwork._ds) {
    const patch = { id: n.id, label: n.title, title: String(n.content || '').slice(0, 200),
                    size: n.size || 16, color: graphNodeColor(n.color, graphTheme()) };
    kbNetwork._ds.nodes.update(patch);
  } else {
    drawKbGraph();
  }
  scheduleKbSave();
  toast('已应用');
}

// ---------- 新增连线 ----------

function showEdgeForm(fromId) {
  const nodes = state.kbData ? (state.kbData.nodes || []) : [];
  if (nodes.length < 2) { toast('至少要有两个节点才能连线'); return; }
  const from = fromId || state.kbSelNode;
  if (!from) { toast('请先点选一个节点作为起点'); return; }
  kbEdgeFrom = from;
  const fn = nodes.find((n) => n.id === from);
  $('#ke-from').textContent = '起点：' + (fn ? (fn.title || '(无标题)') : '');
  $('#ke-to').innerHTML = nodes.filter((n) => n.id !== from)
    .map((n) => '<option value="' + escapeHtml(n.id) + '">' + escapeHtml(String(n.title || '(无标题)')) + '</option>')
    .join('');
  fillRelationSelect($('#ke-relation'));
  syncEdgeDir();                                  // 方向随关系类型给个默认值
  $('#ke-relation').onchange = syncEdgeDir;       // 换关系时重新给默认（之后可手动改）
  const factEl = $('#ke-fact');
  if (factEl) { factEl.value = ''; }              // 每次开新连线都从空白开始
  $('#kb-node-form').classList.add('hidden');
  $('#kb-node-empty').classList.add('hidden');
  $('#kb-edge-form').classList.remove('hidden');
}

// 选关系时把方向切到该关系的默认值：对称关系双向，其余单向
function syncEdgeDir() {
  const sel = $('#ke-dir');
  if (!sel) return;
  const rel = $('#ke-relation').value || 'related';
  sel.value = isSymmetricRel(rel) ? '0' : '1';
}

function hideEdgeForm() {
  kbEdgeFrom = null;
  $('#kb-edge-form').classList.add('hidden');
  if (state.kbSelNode) showNodeForm(state.kbData.nodes.find((n) => n.id === state.kbSelNode));
  else showNodeForm(null);
}

function addKbEdge() {
  const to = $('#ke-to').value;
  const rel = $('#ke-relation').value || 'related';
  if (!kbEdgeFrom || !to || kbEdgeFrom === to) return;
  const fact = ($('#ke-fact') ? $('#ke-fact').value : '').trim();
  // 连线必须写「为什么」：只连一条没有任何陈述的边，等于给图谱添噪声
  if (!fact) {
    toast('请写一句话说明这条关系的依据');
    if ($('#ke-fact')) $('#ke-fact').focus();
    return;
  }
  const directed = ($('#ke-dir') ? $('#ke-dir').value : '1') === '1';
  const label = relationLabel(rel);
  state.kbData.edges = state.kbData.edges || [];
  // (from,to,relation) 唯一：无向边反着连也算重复，避免出现 A⇄B / B⇄A 两条重影
  const same = (e) => (e.relation || 'related') === rel &&
    ((e.from === kbEdgeFrom && e.to === to) ||
     (!directed && e.from === to && e.to === kbEdgeFrom));
  const dup = state.kbData.edges.filter((e) => same(e) && !e.invalid_at);
  let replaced = false;
  if (dup.length) {
    // 同一对节点同一关系已有有效边：陈述变了就等于「替代」旧结论（S2：标失效，不删除）
    const old = dup[0];
    if (String(old.fact || '').trim() === fact) {
      toast('这两个节点之间已有相同关系与相同陈述');
      return;
    }
    old.invalid_at = Date.now() / 1000;
    replaced = true;
  }
  const e = { id: 'e_' + Math.random().toString(36).slice(2, 10),
              from: kbEdgeFrom, to: to, label: label, relation: rel,
              directed: directed, type: 'relation', fact: fact,
              valid_at: Date.now() / 1000 };
  state.kbData.edges.push(e);
  if (replaced || !kbNetwork || !kbNetwork._ds) {
    drawKbGraph();                     // 有边失效：整图重画一次，虚线与新边一起生效
  } else {
    const th = graphTheme();
    const col = relationColor(rel, th);
    kbNetwork._ds.edges.add({
      id: e.id, from: e.from, to: e.to, label: e.label, title: e.fact,
      font: { color: col, size: 11, align: 'middle',
              strokeWidth: 3, strokeColor: th.labelBg },
      color: { color: col, highlight: col, hover: col },
      arrows: edgeArrows(directed), width: 1.6,
    });
  }
  scheduleKbSave();
  toast(replaced ? ('已替代旧陈述：' + label) :
        ('已添加连线：' + label + (directed ? '（单向）' : '（双向）')));
  hideEdgeForm();
}

function deleteKbNode() {
  const id = state.kbSelNode;
  if (!id || !confirm('删除这个节点？')) return;
  state.kbData.nodes = state.kbData.nodes.filter((n) => n.id !== id);
  const gone = state.kbData.edges.filter((e) => e.from === id || e.to === id).map((e) => e.id);
  state.kbData.edges = state.kbData.edges.filter((e) => e.from !== id && e.to !== id);
  if (kbNetwork && kbNetwork._ds) {
    kbNetwork._ds.nodes.remove(id);
    if (gone.length) kbNetwork._ds.edges.remove(gone);
  } else {
    drawKbGraph();
  }
  showNodeForm(null);
  scheduleKbSave();
}

function scheduleKbSave() {
  clearTimeout(kbSaveTimer);
  kbSaveTimer = setTimeout(saveKbGraph, 700);
}

// AI 本轮写入记忆后，刷新正打开的图谱，并把「本轮变更 id」带进来做高亮/提示
function refreshKbIfOpen(changed) {
  const m = $('#kb-modal');
  if (!isOpen(m)) return;          // 图谱没开：不记录变更（关闭态视为已清除）
  if (changed) state.kbChanged = changed;
  const key = state.kbCurrent;
  if (!key) return;
  loadKbGraph(key);
}

/* ---------- 批量操作（vis-network 多选） ---------- */
function kbShowBulk(ids) {
  state.kbSel = ids.slice();
  const box = $('#kb-bulk');
  if (!box) return;
  box.classList.remove('hidden');
  const n = $('#kb-bulk-count');
  if (n) n.textContent = String(ids.length);
}

function kbHideBulk() {
  state.kbSel = [];
  const box = $('#kb-bulk');
  if (box) box.classList.add('hidden');
}

function kbBulkNodes() {
  const ids = state.kbSel || [];
  return ids.map((id) => (state.kbData.nodes || []).find((n) => n.id === id)).filter(Boolean);
}

function kbBulkFamily() {
  const fam = currentKbFamily();
  if (!fam) { toast('先在左侧「主题」里选一个主题', 'error'); return; }
  const ids = state.kbSel || [];
  fam.members = Array.from(new Set([].concat(fam.members || [], ids)));
  drawKbGraph();
  scheduleKbSave();
  toast('已把 ' + ids.length + ' 个节点加入主题');
}

async function kbBulkTag() {
  const tag = ((await appPrompt('给选中的节点加一个标签：')) || '').trim();
  if (!tag) return;
  kbBulkNodes().forEach((n) => {
    n.tags = Array.from(new Set([].concat(n.tags || [], [tag])));
    n.updated_at = Date.now() / 1000;
  });
  drawKbGraph();
  scheduleKbSave();
  toast('已加标签');
}

function kbBulkMerge() {
  const nodes = kbBulkNodes();
  if (nodes.length < 2) { toast('至少选择两个节点', 'error'); return; }
  const target = nodes[0];
  const rest = nodes.slice(1);
  if (!confirm('合并到「' + (target.title || '无标题') + '」？\n将并入：' +
               rest.map((n) => n.title || '无标题').join('、') +
               '\n其余节点会删除，连线与备注会转到保留节点。')) return;
  const restIds = new Set(rest.map((n) => n.id));
  rest.forEach((src) => {
    (state.kbData.edges || []).forEach((e) => {
      if (e.from === src.id) e.from = target.id;
      if (e.to === src.id) e.to = target.id;
    });
    target.notes = (target.notes || []).concat(src.notes || []);
    target.tags = Array.from(new Set([].concat(target.tags || [], src.tags || [])));
    target.aliases = Array.from(new Set([].concat(
      target.aliases || [], [src.title], src.aliases || []
    ).filter(Boolean))).slice(0, 8);
    const sc = String(src.content || '').trim();
    if (sc) {
      const tc = String(target.content || '').trim();
      target.content = tc
        ? tc + '\n\n（合并自 ' + (src.title || '无标题') + '）\n' + sc
        : sc;
    }
  });
  (state.kbData.families || []).forEach((f) => {
    const members = f.members || [];
    if (members.some((m) => restIds.has(m))) {
      f.members = Array.from(new Set(members.filter((m) => !restIds.has(m)).concat([target.id])));
    }
  });
  state.kbData.nodes = state.kbData.nodes.filter((n) => !restIds.has(n.id));
  const seen = new Set();
  state.kbData.edges = (state.kbData.edges || []).filter((e) => {
    if (e.from === e.to) return false;
    const key = [e.from, e.to, e.relation || '', e.label || ''].join('|');
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
  target.updated_at = Date.now() / 1000;
  kbHideBulk();
  drawKbGraph();
  scheduleKbSave();
  showNodeForm(target);
  toast('已合并 ' + rest.length + ' 个节点');
}

function kbBulkDelete() {
  const nodes = kbBulkNodes();
  if (!nodes.length) return;
  if (!confirm('删除选中的 ' + nodes.length + ' 个节点？相关连线也会删除。')) return;
  const ids = new Set(nodes.map((n) => n.id));
  state.kbData.nodes = state.kbData.nodes.filter((n) => !ids.has(n.id));
  state.kbData.edges = (state.kbData.edges || [])
    .filter((e) => !ids.has(e.from) && !ids.has(e.to));
  (state.kbData.families || []).forEach((f) => {
    f.members = (f.members || []).filter((m) => !ids.has(m));
  });
  kbHideBulk();
  showNodeForm(null);
  drawKbGraph();
  scheduleKbSave();
  toast('已删除 ' + ids.size + ' 个节点');
}

/* ---------- 导出 ---------- */
async function kbExport(fmt) {
  try {
    const r = await api('/api/kb/export', {
      id: state.kbCurrent, format: fmt, session_id: state.sessions.current,
    });
    if (!r || !r.ok) { toast((r && r.error) || '导出失败', 'error'); return; }
    const blob = new Blob([r.content || ''], { type: r.mime || 'text/plain;charset=utf-8' });
    const a = document.createElement('a');
    const url = URL.createObjectURL(blob);
    a.href = url;
    a.download = r.filename || ('kb.' + (fmt === 'json' ? 'json' : 'md'));
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    toast('已导出 ' + a.download);
  } catch (e) {
    toastErr('导出', e);
  }
}

function kbExportPng() {
  const list = Array.prototype.slice.call(document.querySelectorAll('#kb-graph canvas'))
    .filter((c) => c.width && c.height);
  if (!list.length) { toast('当前没有可导出的图谱', 'error'); return; }
  const w = Math.max.apply(null, list.map((c) => c.width));
  const h = Math.max.apply(null, list.map((c) => c.height));
  const out = document.createElement('canvas');
  out.width = w;
  out.height = h;
  const ctx = out.getContext('2d');
  const th = graphTheme();
  // 与屏幕上的 .kb-graph 保持一致：先用画布底色铺满，再补点阵
  ctx.fillStyle = th.graphBg;
  ctx.fillRect(0, 0, w, h);
  const step = 22 * (window.devicePixelRatio || 1);
  ctx.fillStyle = th.dot;
  for (let y = step / 2; y < h; y += step) {
    for (let x = step / 2; x < w; x += step) {
      ctx.beginPath();
      ctx.arc(x, y, Math.max(1, window.devicePixelRatio || 1), 0, Math.PI * 2);
      ctx.fill();
    }
  }
  // vis-network 会分层绘制（边 / 节点 / 标签），逐层叠加才不会丢内容
  list.forEach((c) => ctx.drawImage(c, 0, 0, c.width, c.height));
  const a = document.createElement('a');
  a.href = out.toDataURL('image/png');
  const meta = state.kbCurrent === 'session' ? null : kbMeta(state.kbCurrent);
  a.download = ((state.kbCurrent === 'session' ? '会话记忆' : ((meta || {}).name || '知识库'))) + '.png';
  document.body.appendChild(a);
  a.click();
  a.remove();
  toast('已导出图谱图片');
}

/* ---------- 检索测试 ---------- */
async function kbRunRecall() {
  const q = ($('#kb-recall-q').value || '').trim();
  const list = $('#kb-recall-list');
  const meta = $('#kb-recall-meta');
  if (!list) return;
  if (!q) { list.innerHTML = ''; if (meta) meta.textContent = ''; return; }
  if (meta) meta.textContent = '检索中…';
  try {
    const r = await api('/api/memory', {
      op: 'recall', query: q, session_id: state.sessions.current,
    });
    if (!r || !r.ok) { if (meta) meta.textContent = '检索失败'; return; }
    const items = r.items || [];
    if (meta) {
      meta.textContent = items.length + ' 条 · 候选 ' + (r.candidates || 0) +
        (r.used_model ? ' · 已调用模型挑选' : ' · 未调用模型');
    }
    if (!items.length) {
      list.innerHTML = '<div class="hint">没有命中。换一种说法，或确认当前库已绑到本会话。</div>';
      return;
    }
    list.innerHTML = '';
    items.forEach((it) => {
      const row = document.createElement('button');
      row.type = 'button';
      row.className = 'kb-recall-item';
      const t = document.createElement('span');
      t.className = 'kb-recall-title';
      t.textContent = (it.kind === 'neighbor' ? '↳ ' : '') + (it.title || '(无标题)');
      const m = document.createElement('span');
      m.className = 'kb-recall-meta';
      m.textContent = (it.kind === 'neighbor' ? '关联·' + (it.relation || '') + ' · ' : '') +
        it.scope + ' · ' + it.score;
      row.appendChild(t);
      row.appendChild(m);
      row.onclick = () => {
        if (!it.node_id) return;
        if (it.scope_key && it.scope_key !== state.kbCurrent) {
          kbOpenNode(it.scope_key, it.node_id);
        } else {
          kbFocusNode(it.node_id);
        }
      };
      list.appendChild(row);
    });
  } catch (e) {
    if (meta) meta.textContent = '检索失败：' + (e.message || e);
  }
}

async function saveKbGraph() {
  // 用调用时的快照，避免异步期间切换库导致写错目标
  const key = state.kbCurrent;
  const data = state.kbData;
  const sid = state.sessions.current;
  if (!key || !data) return;
  if (key === 'session') {
    await api('/api/memory/save', { session_id: sid, memory: data });
    const s = curSession();
    if (s && state.sessions.current === sid) s.mem_count = data.nodes.length;
  } else {
    const r = await api('/api/kb/save', {
      id: key, nodes: data.nodes, edges: data.edges, graph: data.graph,
      families: data.families || [],
    });
    if (r && r.kbs && state.kbCurrent === key) {
      state.kbs = r.kbs;
      // 只刷新下拉框的选项，保留用户当前选中的项（否则会把选择弹回旧库）
      refreshKbSelect($('#kb-select').value || key);
      renderKbBindList();
    }
  }
}

/* ---------- 节点搜索：输入关键字 → 高亮命中 + 逐个定位 ---------- */
let kbHits = [];      // 当前命中的节点 id
let kbHitIdx = -1;
let kbGlobalToken = 0;

function kbMatchNodes(q) {
  const nodes = (state.kbData && state.kbData.nodes) || [];
  const low = q.toLowerCase();
  return nodes.filter((n) =>
    String(n.title || '').toLowerCase().indexOf(low) >= 0 ||
    String(n.content || '').toLowerCase().indexOf(low) >= 0 ||
    String(n.category || '').toLowerCase().indexOf(low) >= 0 ||
    (n.tags || []).some((t) => String(t).toLowerCase().indexOf(low) >= 0) ||
    (n.aliases || []).some((t) => String(t).toLowerCase().indexOf(low) >= 0)
  ).map((n) => n.id);
}

function updateKbSearchCount() {
  const el = $('#kb-search-count');
  if (!el) return;
  const q = ($('#kb-search') && $('#kb-search').value || '').trim();
  if (!q) { el.textContent = ''; el.classList.remove('none'); return; }
  el.textContent = kbHits.length ? ((kbHitIdx + 1) + '/' + kbHits.length) : '无匹配';
  el.classList.toggle('none', !kbHits.length);
}

// 让 vis 的 canvas 始终等于容器尺寸；否则 focus 只在旧画布（可能只有一半高）里居中
function kbFitCanvas() {
  const el = $('#kb-graph');
  if (!kbNetwork || !el) return;
  const w = el.clientWidth, h = el.clientHeight;
  if (!w || !h) return;
  try {
    kbNetwork.setSize(w + 'px', h + 'px');
    kbNetwork.redraw();
  } catch (e) { /* 图谱可能已被销毁 */ }
}

function kbFocusNodeNow(id, scale, select) {
  if (!kbNetwork || !id) return;
  kbFitCanvas();
  try {
    kbNetwork.focus(id, { scale: scale || 1.2, animation: true });
    const nodes = (state.kbData && state.kbData.nodes) || [];
    const ids = ((select && select.length) ? select : [id])
      .filter((x) => nodes.some((n) => n.id === x));
    kbNetwork.selectNodes(ids.length ? ids : [id]);
  } catch (e) { /* 图谱可能已被销毁 */ }
}

// 图谱刚创建时，等物理稳定后再决定「fit 全图」还是「focus 目标节点」。
// stabilization.fit=false 已关掉 vis 的自动 fit，因此这里的决定不会被覆盖。
function kbApplyPendingFocus() {
  if (!kbNetwork) return;
  const pending = state.kbPendingFocus;
  state.kbPendingFocus = null;
  const nodes = (state.kbData && state.kbData.nodes) || [];
  if (pending && pending.id && nodes.some((n) => n.id === pending.id)) {
    kbFocusNodeNow(pending.id, pending.scale, pending.select);
    return;
  }
  if (pending && pending.edgeId) {
    const edge = ((state.kbData && state.kbData.edges) || [])
      .find((e) => e.id === pending.edgeId);
    if (edge) {
      kbFocusNodeNow(edge.from, pending.scale);
      kbNetwork.selectEdges([pending.edgeId]);
      return;
    }
  }
  kbNetwork.fit({ animation: false });
  const ch = state.kbChanged && state.kbChanged[state.kbCurrent];
  const changed = ((ch && ch.node_add) || []).concat((ch && ch.node_update) || [])
    .filter((id) => nodes.some((n) => n.id === id));
  if (changed.length) kbNetwork.selectNodes(changed);
}

function kbFocusNode(id) {
  if (!id) return;
  const node = (state.kbData.nodes || []).find((n) => n.id === id);
  // 列表视图（或图谱库未加载）下没有 vis 实例：直接打开详情，并在列表里滚动到该条目
  if (kbView === 'list' || !kbNetwork) {
    if (node) showNodeForm(node);
    if (kbView === 'list') {
      const el = $('#kb-graph');
      const lis = el ? el.querySelectorAll('li[data-id]') : [];
      for (let i = 0; i < lis.length; i++) {
        if (lis[i].dataset.id !== id) continue;
        el.querySelectorAll('li.sel').forEach((x) => x.classList.remove('sel'));
        lis[i].classList.add('sel');
        lis[i].scrollIntoView({ block: 'nearest' });
        break;
      }
    }
    return;
  }
  if (node) showNodeForm(node);
  // 等一帧：侧栏 / 节点表单可能刚改变布局，先让 canvas 跟到最终尺寸
  requestAnimationFrame(() => kbFocusNodeNow(id, 1.15));
}

function kbFocusHit() {
  kbFocusNode(kbHits[kbHitIdx]);
}

async function kbGlobalSearch(q) {
  const box = $('#kb-global-results');
  const count = $('#kb-search-count');
  if (!box) return;
  if (!q) {
    box.classList.add('hidden');
    box.innerHTML = '';
    if (count) { count.textContent = ''; count.classList.remove('none'); }
    return;
  }
  const token = ++kbGlobalToken;
  try {
    const r = await api('/api/memory', { op: 'search', query: q, limit: 20 });
    if (token !== kbGlobalToken) return;
    const items = (r && r.items) || [];
    box.innerHTML = '';
    if (!items.length) {
      box.innerHTML = '<div class="kb-global-empty">没有匹配的记忆</div>';
    }
    items.forEach((it) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'kb-global-item';
      const t = document.createElement('div');
      t.className = 'kb-global-title';
      t.textContent = it.title || it.text || '';
      const m = document.createElement('div');
      m.className = 'kb-global-meta';
      m.textContent = (it.source_name || '') + ' · ' + (it.type || 'fact');
      b.appendChild(t);
      b.appendChild(m);
      b.onclick = () => {
        box.classList.add('hidden');
        if (it.source === 'kb' && it.kb_id === state.kbCurrent) {
          kbFocusNode(it.source_id);
        } else {
          kbOpenNode(it.source === 'session' ? 'session' : it.kb_id, it.source_id);
        }
      };
      box.appendChild(b);
    });
    box.classList.remove('hidden');
    if (count) {
      count.textContent = items.length + ' 条';
      count.classList.toggle('none', !items.length);
    }
  } catch (e) {
    if (token !== kbGlobalToken) return;
    box.innerHTML = '<div class="kb-global-empty">搜索失败：'
      + escapeHtml(e.message || String(e)) + '</div>';
    box.classList.remove('hidden');
  }
}

function applyKbSearch() {
  const q = ($('#kb-search') && $('#kb-search').value || '').trim();
  const scopeEl = $('#kb-search-scope');
  if (scopeEl && scopeEl.value === 'global') {
    kbHits = [];
    kbHitIdx = -1;
    if (kbNetwork) kbNetwork.unselectAll();
    kbGlobalSearch(q);
    return;
  }
  const box = $('#kb-global-results');
  if (box) box.classList.add('hidden');
  kbHits = q ? kbMatchNodes(q) : [];
  kbHitIdx = kbHits.length ? 0 : -1;
  if (kbNetwork) {
    if (kbHits.length) kbNetwork.selectNodes(kbHits);
    else kbNetwork.unselectAll();
  }
  updateKbSearchCount();
  if (kbHitIdx >= 0) kbFocusHit();
}

function kbSearchStep(dir) {
  const scopeEl = $('#kb-search-scope');
  if (scopeEl && scopeEl.value === 'global') return;
  const q = ($('#kb-search') && $('#kb-search').value || '').trim();
  if (!q) return;
  if (!kbHits.length) { applyKbSearch(); return; }
  kbHitIdx = (kbHitIdx + dir + kbHits.length) % kbHits.length;
  if (kbNetwork) kbNetwork.selectNodes(kbHits);
  updateKbSearchCount();
  kbFocusHit();
}

function resetKbSearch() {
  kbHits = []; kbHitIdx = -1;
  kbGlobalToken += 1;
  const inp = $('#kb-search');
  if (inp) inp.value = '';
  const box = $('#kb-global-results');
  if (box) {
    box.classList.add('hidden');
    box.innerHTML = '';
  }
  updateKbSearchCount();
}

function bindKbSearch() {
  const inp = $('#kb-search');
  if (inp) {
    inp.addEventListener('input', applyKbSearch);
    inp.addEventListener('keydown', (e) => {
      if (isImeComposing(e)) return;
      if (e.key === 'Enter') { e.preventDefault(); kbSearchStep(e.shiftKey ? -1 : 1); }
      else if (e.key === 'Escape') { e.preventDefault(); inp.value = ''; applyKbSearch(); inp.blur(); }
    });
  }
  const prev = $('#kb-search-prev');
  const next = $('#kb-search-next');
  if (prev) prev.onclick = () => kbSearchStep(-1);
  if (next) next.onclick = () => kbSearchStep(1);

  const scope = $('#kb-search-scope');
  if (scope) scope.onchange = () => applyKbSearch();
  // 点结果面板与搜索框以外的区域时收起「全部记忆」结果
  document.addEventListener('click', (e) => {
    const box = $('#kb-global-results');
    if (!box || box.classList.contains('hidden')) return;
    if (box.contains(e.target)) return;
    if (e.target === $('#kb-search') || e.target === scope) return;
    box.classList.add('hidden');
  });
}

/* ---------- 顶栏「⋯」更多菜单 ---------- */
function bindKbMore() {
  const btn = $('#kb-more-btn');
  const menu = $('#kb-more-menu');
  if (!btn || !menu) return;
  const exMd = $('#kb-export-md');
  if (exMd) exMd.onclick = () => kbExport('markdown');
  const exJson = $('#kb-export-json');
  if (exJson) exJson.onclick = () => kbExport('json');
  const exPng = $('#kb-export-png');
  if (exPng) exPng.onclick = () => kbExportPng();
  btn.onclick = (e) => { e.stopPropagation(); menu.classList.toggle('hidden'); };
  // 点菜单里的按钮后自动收起；点复选框（AI 开关）不收起，方便看到状态
  menu.addEventListener('click', (e) => { if (e.target.closest('button')) menu.classList.add('hidden'); });
  document.addEventListener('click', (e) => {
    if (menu.classList.contains('hidden')) return;
    if (!menu.contains(e.target) && e.target !== btn) menu.classList.add('hidden');
  });
}

/* ---------- 右栏拖动调宽 ---------- */
function bindKbResizer() {
  const bar = $('#kb-resizer');
  const side = $('#kb-modal .kb-side');
  if (!bar || !side) return;
  const saved = parseInt(localStorage.getItem('kb_side_w') || '', 10);   // 记住上次宽度
  if (saved) side.style.width = Math.max(220, Math.min(640, saved)) + 'px';
  let startX = 0, startW = 0, dragging = false;
  const end = (e) => {
    if (!dragging) return;
    dragging = false; bar.classList.remove('active');
    try { if (bar.releasePointerCapture) bar.releasePointerCapture(e.pointerId); } catch (err) { /* 忽略 */ }
    try { localStorage.setItem('kb_side_w', String(side.offsetWidth)); } catch (err) { /* 忽略 */ }
  };
  bar.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;
    dragging = true; startX = e.clientX; startW = side.offsetWidth;
    bar.classList.add('active');
    try { bar.setPointerCapture(e.pointerId); } catch (err) { /* 忽略 */ }
    e.preventDefault();
  });
  bar.addEventListener('pointermove', (e) => {
    if (!dragging) return;
    // 侧栏在右侧：往左拖变宽
    const w = Math.max(220, Math.min(640, startW - (e.clientX - startX)));
    side.style.width = w + 'px';
  });
  bar.addEventListener('pointerup', end);
  bar.addEventListener('pointercancel', end);
}

function bindKbEvents() {
  $('#btn-kb').onclick = () => openKbModal();
  $('#btn-close-kb').onclick = closeKbModal;
  // 刷新 = 从磁盘重新加载（只读，不会覆盖已写入的内容）
  const bulkFamily = $('#kb-bulk-family');
  if (bulkFamily) bulkFamily.onclick = () => kbBulkFamily();
  const bulkTag = $('#kb-bulk-tag');
  if (bulkTag) bulkTag.onclick = () => kbBulkTag();
  const bulkMerge = $('#kb-bulk-merge');
  if (bulkMerge) bulkMerge.onclick = () => kbBulkMerge();
  const bulkDel = $('#kb-bulk-del');
  if (bulkDel) bulkDel.onclick = () => kbBulkDelete();
  const recallRun = $('#kb-recall-run');
  if (recallRun) recallRun.onclick = () => kbRunRecall();
  const recallQ = $('#kb-recall-q');
  if (recallQ) {
    recallQ.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); kbRunRecall(); }
    });
  }
  $('#kb-refresh').onclick = () => { loadKbGraph(state.kbCurrent || 'session'); toast('已刷新'); };
  bindKbSearch();
  bindKbMore();
  bindKbResizer();
  $('#kb-select').onchange = async (e) => {
    await saveKbGraph();          // 先保存当前库，再切换，避免丢改动
    loadKbGraph(e.target.value);
  };
  $('#kb-add-node').onclick = () => addKbNode(null);
  $('#kb-add-edge').onclick = () => showEdgeForm();
  const viewGraph = $('#kb-view-graph');
  if (viewGraph) viewGraph.onclick = () => kbSetView('graph');
  const viewList = $('#kb-view-list');
  if (viewList) viewList.onclick = () => kbSetView('list');
  $('#ke-add').onclick = addKbEdge;
  $('#ke-cancel').onclick = hideEdgeForm;
  $('#kd-save').onclick = applyNodeForm;
  $('#kd-origin').onclick = kbJumpToOrigin;
  $('#kd-note-add').onclick = addKbNodeNote;
  $('#kd-del').onclick = deleteKbNode;
  $('#kd-size').oninput = () => { $('#kd-size-val').textContent = $('#kd-size').value; };
  bindKbGraphSettings();
  $('#profile-open-kb').onclick = () => {
    const pid = profileKbId();
    if (closeModal() === false) return;
    openKbModal(pid || 'session');
  };
  $('#sess-ai-write').onchange = async () => {
    applySessions(await api('/api/session/bind',
      { id: state.sessions.current, ai_write: $('#sess-ai-write').checked }));
    refreshKbSelect($('#kb-select').value || state.kbCurrent);
  };
  $('#sess-use-profile').onchange = async () => {
    applySessions(await api('/api/session/bind',
      { id: state.sessions.current, use_profile: $('#sess-use-profile').checked }));
    refreshKbSelect($('#kb-select').value || state.kbCurrent);
  };
  $('#sess-use-memory').onchange = async () => {
    applySessions(await api('/api/session/bind',
      { id: state.sessions.current, use_memory: $('#sess-use-memory').checked }));
    refreshKbSelect($('#kb-select').value || state.kbCurrent);
  };
  $('#kb-ai-write').onchange = async () => {
    if (state.kbCurrent === 'session') return;
    const r = await api('/api/kb/write', { id: state.kbCurrent, ai_write: $('#kb-ai-write').checked });
    if (r && r.kbs) {
      state.kbs = r.kbs;
      renderKbBindList();
      refreshKbSelect($('#kb-select').value || state.kbCurrent);
    }
  };
  const familySel = $('#kb-family');
  if (familySel) familySel.onchange = () => {
    state.kbFamily = familySel.value;
    refreshKbFamilySelect();
    drawKbGraph();
  };
  $('#kf-save').onclick = saveKbFamily;
  $('#kf-add-node').onclick = addSelectedToFamily;
  $('#kf-delete').onclick = deleteKbFamily;
  const promptInput = $('#kb-prompt');
  if (promptInput) promptInput.oninput = () => {
    const st = $('#kb-prompt-status');
    if (st) st.textContent = '未保存';
  };
  const promptSave = $('#kb-prompt-save');
  if (promptSave) promptSave.onclick = async () => {
    if (state.kbCurrent === 'session') return;
    const prompt = (promptInput ? promptInput.value : '').trim();
    try {
      const r = await api('/api/kb/save', { id: state.kbCurrent, prompt });
      if (!r || !r.ok) throw new Error((r && r.error) || '保存失败');
      const st = $('#kb-prompt-status');
      if (st) st.textContent = '已保存';
      toast('知识库提示词已保存', 'success');
    } catch (e) { toastErr('保存提示词失败', e); }
  };
  $('#kb-new').onclick = async () => {
    const name = await appPrompt('新知识库名称');
    if (!name || !name.trim()) return;
    await saveKbGraph();          // 先保存当前库的改动，再新建
    const r = await api('/api/kb/create', { name: name.trim() });
    state.kbs = r.kbs;
    refreshKbSelect(r.kb.id);
    renderKbBindList();
    loadKbGraph(r.kb.id);
  };
  $('#kb-rename').onclick = async () => {
    if (state.kbCurrent === 'session') return;
    const meta = kbMeta(state.kbCurrent);
    const name = await appPrompt('重命名知识库', meta ? meta.name : '');
    if (!name || !name.trim()) return;
    try {
      const r = await api('/api/kb/save', { id: state.kbCurrent, name: name.trim() });
      if (r && r.kbs) { state.kbs = r.kbs; refreshKbSelect(state.kbCurrent); renderKbBindList(); }
    } catch (e) { toast('重命名失败（系统库不可改名）'); }
  };
  $('#kb-del').onclick = async () => {
    if (state.kbCurrent === 'session') return;
    const meta = kbMeta(state.kbCurrent);
    if (!confirm('删除知识库「' + (meta ? meta.name : '') + '」？不可恢复。')) return;
    const r = await api('/api/kb/delete', { id: state.kbCurrent });
    if (!r.ok) { toast(r.error || '删除失败'); return; }
    state.kbs = r.kbs;
    refreshKbSelect('session');
    renderKbBindList();
    loadKbGraph('session');
  };
}
