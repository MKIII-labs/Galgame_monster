/* ============================================================
   舞台状态参数（stats）：AI 依角色设定自建的临时数值
   ============================================================ */
// 进度条颜色按图标类型区分（可爱风；无匹配时用主题强调色）
const GAL_STAT_COLORS = {
  '❤': '#ff6b8b', '❤️🩹': '#ff5a5a', '🕐': '#7aa2f7', '🍖': '#ff9f43', '💧': '#4fc3f7',
  '📦': '#c08b5c', '✨': '#a78bfa', '💰': '#f0b429', '⚡': '#f7d154', '🌡': '#4dd0c4',
  '☁': '#9aa7bd', '🎭': '#c084fc', '⭐': '#f6c344', '🏅': '#e8a33d', '🔥': '#ff7a45', '⚠': '#f2544b',
};

function galStatColor(it) {
  return GAL_STAT_COLORS[it && it.icon] || 'var(--accent)';
}

function galStatRow(it) {
  const row = document.createElement('div');
  row.className = 'gal-stat';
  const ic = document.createElement('span');
  ic.className = 'gal-stat-icon';
  ic.textContent = (it && it.icon) || '●';
  const lb = document.createElement('span');
  lb.className = 'gal-stat-label';
  lb.textContent = (it && (it.label || it.key)) || '';
  row.appendChild(ic);
  row.appendChild(lb);
  const val = it ? it.value : null;
  if (typeof val === 'number') {
    const max = (typeof it.max === 'number') ? it.max : null;
    const min = (typeof it.min === 'number') ? it.min : 0;
    if (max !== null && max > min) {
      const bar = document.createElement('span');
      bar.className = 'gal-stat-bar';
      const fill = document.createElement('i');
      fill.style.width = Math.max(0, Math.min(100, ((val - min) / (max - min)) * 100)) + '%';
      fill.style.background = galStatColor(it);
      bar.appendChild(fill);
      row.appendChild(bar);
      const v = document.createElement('span');
      v.className = 'gal-stat-val';
      v.textContent = val + ' / ' + max;
      row.appendChild(v);
    } else {
      row.classList.add('nolimit');
      const v = document.createElement('span');
      v.className = 'gal-stat-val';
      v.textContent = String(val);
      row.appendChild(v);
    }
  } else {
    const b = document.createElement('span');
    b.className = 'gal-stat-badge';
    b.textContent = (val == null) ? '' : String(val);
    row.appendChild(b);
  }
  return row;
}

// 渲染状态栏；列表为空 → 隐藏并清数据；仅在设置里关掉 → 隐藏但保留数据（便于开关恢复）
// 状态栏是否占据左上角：给 .gal-stage 打标记，「幕间」工具卡才好右移让位
function galSetStatsFlag(on) {
  const st = document.querySelector('.gal-stage');
  if (st) st.classList.toggle('gal-has-stats', !!on);
}

function galDedupeStats(items) {
  // 同一个 key 或同一个 label 视为同一条：模型可能把「生命力」写成 key=生命 / key=生命力，
  // 后端已按同口径合并，这里再兜一层，保证历史攒下的重复也不会显示成两份（后者覆盖前者）。
  const out = [];
  const byKey = new Map();
  const byLabel = new Map();
  (Array.isArray(items) ? items : []).forEach((it) => {
    if (!it) return;
    const lb = it.label || it.key;
    let at = byKey.has(it.key) ? byKey.get(it.key) : byLabel.get(lb);
    if (at === undefined) {
      at = out.push(Object.assign({}, it)) - 1;
    } else {
      out[at] = Object.assign({}, out[at], it);
    }
    byKey.set(it.key, at);
    byLabel.set(out[at].label || out[at].key, at);
  });
  return out;
}

function galRenderStats(items) {
  const box = $('#gal-stats');
  if (!box) return;
  const list = galDedupeStats(items);
  galState.stats = list;
  if (!list.length || galCfg('gal_show_stats', true) === false) {
    box.classList.add('hidden');
    box.innerHTML = '';
    galSetStatsFlag(false);
    return;
  }
  const prev = galState.statPrev || {};
  const next = {};
  box.innerHTML = '';
  list.forEach((it) => {
    const el = galStatRow(it);
    const before = prev[it.key];
    if (before !== undefined && String(before) !== String(it.value)) {
      el.classList.add('flash');
      el.classList.add((typeof it.value === 'number' && typeof before === 'number' && it.value < before) ? 'down' : 'up');
    }
    next[it.key] = it.value;
    box.appendChild(el);
  });
  galState.statPrev = next;
  box.classList.remove('hidden');
  galSetStatsFlag(true);
}

function galApplyStats(payload) {
  const items = (payload && (payload.items || payload)) || [];
  galRenderStats(Array.isArray(items) ? items : []);
}

// 快捷开关：写进设置（与设置面板里的「显示舞台状态参数」同一项）
async function galToggleStats() {
  const next = galCfg('gal_show_stats', true) === false;
  state.config = state.config || {};
  state.config.gal_show_stats = next;
  galRenderStats(galState.stats || []);
  try { await api('/api/config', { gal_show_stats: next }); }
  catch (e) { toastErr('保存设置', e); }
  toast(next ? '状态栏已开启' : '状态栏已隐藏');
}

async function galResetStats() {
  if (!(galState.stats || []).length) { toast('当前没有状态参数'); return; }
  if (!confirm('清空本会话的全部状态参数？（AI 之后可以重新建立）')) return;
  try {
    const r = await api('/api/gal/stats/reset', { id: state.sessions.current });
    if (!r || !r.ok) throw new Error((r && r.error) || '重置失败');
    applySessions(r);
    galState.statPrev = {};
    galRenderStats([]);
    toast('已重置状态栏');
  } catch (e) { toastErr('重置', e); }
}

/* ---- AI 剧本：查看大纲 / 生成 / 重新生成 ---- */