/* ============================================================
   功能大厅（Hall）—— AI 互动玩法的统一入口
   ------------------------------------------------------------
   新增一个功能只要三步（不需要改这个文件）：
     1) 新建 web/hall-<功能id>.js，在里面 hallRegister({ id, name, mount, unmount? })
     2) 在 index.html 里加一行 <script src="/static/hall-<功能id>.js"></script>
     3) 后端在 app/api/hall.py 加处理器，并在 app/httpd.py 挂 /api/hall/<功能id>/…
   卡片清单直接由注册表生成（单一来源），不存在「卡片上写了但功能没接」的漂移。
   ============================================================ */
'use strict';

const HALL_FEATURES = [];

function hallRegister(feature) {
  if (!feature || !feature.id || typeof feature.mount !== 'function') return;
  if (HALL_FEATURES.some((f) => f.id === feature.id)) return;   // 脚本重复加载时兜底
  feature.name = feature.name || feature.id;
  HALL_FEATURES.push(feature);
}

let hallCurrent = null;      // 当前打开的功能
let hallInited = false;

const HALL_AI_KEY = 'gm.hall.ai_enabled.v1';

/* 大厅模块内部的「AI 对手」开关：默认开启，状态写 config.json。
   这是给模块里的 AI 用的，不是禁用整个功能模块。 */
function hallAiEnabledMap() {
  try {
    const cfg = (typeof state !== 'undefined' && state.config && state.config.hall_ai_enabled);
    if (cfg && typeof cfg === 'object' && !Array.isArray(cfg)) return Object.assign({}, cfg);
  } catch (e) { /* state 尚未加载时走 localStorage */ }
  try {
    const raw = localStorage.getItem(HALL_AI_KEY);
    const obj = raw ? JSON.parse(raw) : {};
    return obj && typeof obj === 'object' && !Array.isArray(obj) ? obj : {};
  } catch (e) {
    return {};
  }
}

function hallSaveAiEnabled(map) {
  try { localStorage.setItem(HALL_AI_KEY, JSON.stringify(map)); } catch (e) { /* 隐私模式 / 配额满 */ }
  try {
    if (typeof state !== 'undefined' && state.config) {
      state.config.hall_ai_enabled = Object.assign({}, map);
      // 只提交这一个键，服务端 /api/config 是增量合并。
      if (typeof api === 'function') {
        api('/api/config', { hall_ai_enabled: map }).catch(() => {});
      }
    }
  } catch (e) { /* 配置写入失败时 localStorage 仍然有效 */ }
}

function hallAiEnabled(id) {
  return hallAiEnabledMap()[id] !== false;
}

function hallSetAiEnabled(id, enabled) {
  const map = hallAiEnabledMap();
  map[id] = !!enabled;
  hallSaveAiEnabled(map);
}

/* 模块内统一的 AI 开关控件。onChange(enabled) 由模块自己更新人数选择 / 重开一局。 */
function hallAiSwitch(id, label, onChange) {
  const on = hallAiEnabled(id);
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'hall-ai-switch' + (on ? ' on' : '');
  btn.setAttribute('role', 'switch');
  btn.setAttribute('aria-checked', on ? 'true' : 'false');
  btn.setAttribute('aria-label', (on ? '关闭 ' : '开启 ') + (label || 'AI 对手'));
  btn.title = on ? ('关闭 ' + (label || 'AI 对手')) : ('开启 ' + (label || 'AI 对手'));

  const track = document.createElement('span');
  track.className = 'hall-ai-switch-track';
  const knob = document.createElement('span');
  knob.className = 'hall-ai-switch-knob';
  track.appendChild(knob);
  const text = document.createElement('span');
  text.className = 'hall-ai-switch-label';
  text.textContent = label || 'AI 对手';
  btn.appendChild(track);
  btn.appendChild(text);

  btn.onclick = (e) => {
    e.preventDefault();
    e.stopPropagation();
    const next = !hallAiEnabled(id);
    hallSetAiEnabled(id, next);
    btn.classList.toggle('on', next);
    btn.setAttribute('aria-checked', next ? 'true' : 'false');
    btn.setAttribute('aria-label', (next ? '关闭 ' : '开启 ') + (label || 'AI 对手'));
    btn.title = next ? ('关闭 ' + (label || 'AI 对手')) : ('开启 ' + (label || 'AI 对手'));
    if (typeof onChange === 'function') onChange(next);
  };
  return btn;
}

function hallEl(id) { return document.getElementById(id); }

function hallSetChrome(title, sub) {
  const t = hallEl('hall-title');
  const s = hallEl('hall-sub');
  if (t) t.textContent = title;
  if (s) s.textContent = sub || '';
}

function hallSetBadge(text) {
  const b = hallEl('hall-badge');
  if (b) b.textContent = text || '';
}

function hallCardEl(f) {
  const soon = f.ready === false;
  const card = document.createElement('div');
  card.className = 'hall-card' + (soon ? ' soon' : '');
  card.title = soon ? (f.name + '（开发中）') : (f.desc || f.name);

  const ico = document.createElement('span');
  ico.className = 'hall-card-ico';
  ico.innerHTML = '<svg class="ic"><use href="#' + (f.icon || 'i-sparkle') + '"/></svg>';

  const name = document.createElement('span');
  name.className = 'hall-card-name';
  name.textContent = f.name;

  const desc = document.createElement('span');
  desc.className = 'hall-card-desc';
  desc.textContent = f.desc || '';

  const foot = document.createElement('span');
  foot.className = 'hall-card-foot';
  const meta = document.createElement('span');
  // meta / cta 允许是函数：卡片可以在不改动大厅渲染的前提下显示“继续上次”这类实时状态
  try {
    meta.textContent = typeof f.meta === 'function' ? (f.meta() || '') : (f.meta || '');
  } catch (e) {
    meta.textContent = '';
  }
  const go = document.createElement('button');
  go.type = 'button';
  go.className = 'hall-card-go';
  try {
    go.textContent = soon ? '开发中' : (typeof f.cta === 'function' ? (f.cta() || '进入 ›') : '进入 ›');
  } catch (e) {
    go.textContent = soon ? '开发中' : '进入 ›';
  }

  foot.appendChild(meta);
  foot.appendChild(go);

  card.appendChild(ico);
  card.appendChild(name);
  card.appendChild(desc);
  card.appendChild(foot);
  if (f.tag) {
    const tag = document.createElement('span');
    tag.className = 'hall-card-tag';
    tag.textContent = f.tag;
    card.appendChild(tag);
  }
  const openThis = () => {
    if (soon) { toast(f.name + ' 还在开发中'); return; }
    hallOpenFeature(f.id);
  };
  card.onclick = openThis;
  go.onclick = (e) => {
    e.preventDefault();
    e.stopPropagation();
    openThis();
  };
  return card;
}

/* 卸掉当前功能：功能自己的定时器 / 地图实例 / 请求都应在 unmount 里收干净 */
function hallTeardown() {
  if (!hallCurrent) return;
  const f = hallCurrent;
  hallCurrent = null;
  try {
    if (typeof f.unmount === 'function') f.unmount();
  } catch (e) {
    console.warn('[大厅] 卸载功能失败', f.id, e);
  }
}

function hallRenderHome() {
  hallTeardown();
  const body = hallEl('hall-body');
  if (!body) return;
  body.innerHTML = '';
  hallSetChrome('功能大厅', 'AI 可以陪你一起玩的互动玩法');
  hallSetBadge(HALL_FEATURES.length ? (HALL_FEATURES.length + ' 个功能') : '');
  const back = hallEl('hall-back');
  if (back) back.classList.add('hidden');

  if (!HALL_FEATURES.length) {
    const em = document.createElement('div');
    em.className = 'hall-empty';
    em.textContent = '还没有可用的功能。';
    body.appendChild(em);
    return;
  }
  const grid = document.createElement('div');
  grid.className = 'hall-grid';
  HALL_FEATURES.forEach((f) => grid.appendChild(hallCardEl(f)));
  body.appendChild(grid);
}

function hallOpenFeature(id) {
  const f = HALL_FEATURES.find((x) => x.id === id);
  if (!f) { toast('没有这个功能：' + id); return; }
  hallTeardown();
  const body = hallEl('hall-body');
  if (!body) return;
  body.innerHTML = '';
  hallSetChrome(f.name, f.desc || '');
  hallSetBadge(f.tag || '');
  const back = hallEl('hall-back');
  if (back) back.classList.remove('hidden');

  const root = document.createElement('div');
  root.className = 'hall-feature';
  body.appendChild(root);
  hallCurrent = f;
  try {
    f.mount(root);
  } catch (e) {
    root.textContent = '功能加载失败：' + ((e && e.message) || e);
    console.error('[大厅] 挂载功能失败', id, e);
  }
}

/* 进入大厅模式：首次进入渲染卡片墙；再次进入时让当前功能重新量一次尺寸
   （地图类组件在 display:none 的容器里量出来是 0×0，回到可见必须 invalidateSize） */
function hallEnterMode() {
  if (!hallInited) {
    hallInited = true;
    hallRenderHome();
    return;
  }
  if (hallCurrent && typeof hallCurrent.onShow === 'function') {
    try { hallCurrent.onShow(); } catch (e) { /* 重新量尺寸失败不该影响切模式 */ }
  }
}

/* 离开大厅模式：让功能收起自己挂在 <body> 上的浮层
   （否则切到 Solo / IDE 之后，全屏大图这类浮层会继续盖在别的模式上） */
function hallLeaveMode() {
  if (hallCurrent && typeof hallCurrent.onHide === 'function') {
    try { hallCurrent.onHide(); } catch (e) { /* 收浮层失败不该挡住切模式 */ }
  }
}

function initHall() {
  const back = hallEl('hall-back');
  if (back) back.onclick = () => hallRenderHome();
  if (typeof window.hallRegister !== 'function') window.hallRegister = hallRegister;
}
