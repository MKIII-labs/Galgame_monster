/* ============================================================
   图寻游戏 —— 功能大厅的第一个功能
   ------------------------------------------------------------
   一局 = 一张真实照片（Panoramax 开放图库）+ 一次世界地图标注。
   所有参与者（你 + 若干 AI）看同一张照片，各自在地图上标位置，
   全部标完后由后端算「与真实拍摄点的大圆距离」，最近者赢。

   真值只存在于服务端：题面接口不下发坐标，只有结算接口才返回 ——
   否则打开开发者工具就能直接看到答案。
   ============================================================ */
'use strict';

/* 参与者配色（按参与者在本局名单里的顺序取），地图标记与名单小圆点共用 */
const GEO_COLORS = ['#4d6bfe', '#e08a3c', '#2f9e6f', '#c0567a', '#7a5cd6'];

/* 地图底图：都用 Web Mercator（WGS-84），点击得到的经纬度与真实经纬度同一坐标系。
   绝不能换成高德 / 腾讯瓦片 —— 那是 GCJ-02 火星坐标，会让距离测算系统性偏出几百米。

   顺序按**浏览器里实测**的结果排：osmfr 在 Chromium 下会被 ORB 拦掉（返回体被判定
   与图片类型不符），而 rrze.fau.de 正常出图。写反了每局都要先白扔几个失败请求再切换。 */
const GEO_TILES = [
  'https://osm.rrze.fau.de/osmhd/{z}/{x}/{y}.png',
  'https://a.tile.openstreetmap.fr/osmfr/{z}/{x}/{y}.png',
];
const GEO_TILE_ATTR = '底图 © OpenStreetMap 贡献者 · 照片 Panoramax';
const LEAFLET_CSS = [
  'https://cdn.bootcdn.net/ajax/libs/leaflet/1.9.4/leaflet.css',
  'https://cdn.staticfile.org/leaflet/1.9.4/leaflet.css',
];
const LEAFLET_JS = [
  'https://cdn.bootcdn.net/ajax/libs/leaflet/1.9.4/leaflet.js',
  'https://cdn.staticfile.org/leaflet/1.9.4/leaflet.js',
  'https://unpkg.com/leaflet@1.9.4/dist/leaflet.js',
];

const geo = {
  el: {},
  gen: 0,               // 挂载代次：异步回来的响应若属旧代次，一律丢弃
  round: null,
  result: null,
  map: null,
  mapPromise: null,
  layer: null,          // 标点层
  lineLayer: null,      // 连线层（结算时只清这一层，标点 DOM 原地复用）
  markers: {},          // player_id -> marker
  pending: null,        // 你尚未确认的临时标记
  thinking: {},         // player_id -> true（AI 正在算）
  since: {},            // player_id -> 开始算的时刻（界面上显示已等了多少秒）
  tick: null,           // 秒表定时器：只在有人正在思考时跑
  failed: {},           // player_id -> 失败原因
  assist: { loading: false, text: '', open: false, err: '' },   // 玩家识图辅助
  aiCatalog: [],        // 完整 AI 名册（含风格介绍）
  aiOff: [],            // 本局关闭的 AI id
  aiPanelOpen: false,   // AI 选择面板是否展开
  submitted: false,
  revealed: false,
  busy: false,
  no: 0,
  cssDone: false,
  zoom: null,           // 全屏大图浮层（首次点开时才有）
};

hallRegister({
  id: 'geo',
  name: '图寻游戏',
  icon: 'i-globe',
  tag: '对局',
  desc: '一张真实照片，你和 AI 一起在世界地图上标出拍摄地点，谁标得近谁赢。',
  meta: '1 张图 · 世界地图 · 最多 4 位 AI 同场',
  mount: geoMount,
  unmount: geoUnmount,
  onShow: geoOnShow,
  onHide: geoCloseZoom,
});

/* ---------------- 挂载 / 卸载 ---------------- */

function geoMount(root) {
  geo.gen += 1;
  // .geo-root 这一层必须有：它是「左右分栏」的容器（.hall-feature 本身是竖排的），
  // 少了它两列会退化成上下堆叠 —— 照片列抢走大部分高度、地图被挤到下面还被裁掉
  root.innerHTML = [
    '<div class="geo-root">',
    '  <div class="geo-photo-col">',
    '    <div class="geo-bar">',
    '      <span class="geo-pill accent" id="geo-no">第 1 局</span>',
    '      <span class="geo-pill geo-ai-pill">AI 对手',
    '        <span id="geo-ai-switch"></span>',
    '        <button type="button" id="geo-ai-pick" class="geo-ai-pick">选择 ▾</button></span>',
    '      <span class="geo-bar-spacer"></span>',
    '      <span class="geo-pill" id="geo-vision"></span>',
    '      <div class="geo-ai-panel hidden" id="geo-ai-panel"></div>',
    '    </div>',
    '    <div class="geo-photo-box" id="geo-photo-box"></div>',
    '    <div class="geo-credit" id="geo-credit"></div>',
    '    <div class="geo-assist-bar">',
    '      <button type="button" id="geo-assist" class="geo-btn">识图辅助</button>',
    '      <span class="geo-assist-note">只描述特征，不判断地点</span>',
    '    </div>',
    '    <div class="geo-assist-result hidden" id="geo-assist-result"></div>',
    '    <div class="geo-result hidden" id="geo-result"></div>',
    '    <div class="geo-players" id="geo-players"></div>',
    '  </div>',
    '  <div class="geo-side-col">',
    '    <div class="geo-map" id="geo-map"></div>',
    '    <div class="geo-msg" id="geo-msg"></div>',
    '    <div class="geo-actions">',
    '      <button id="geo-confirm" class="geo-btn primary" disabled>确认标注</button>',
    '      <button id="geo-reveal" class="geo-btn" disabled>直接结算</button>',
    '      <button id="geo-next" class="geo-btn ghost">换一题</button>',
    '    </div>',
    '  </div>',
    '</div>',
  ].join('');

  ['no', 'aiPick', 'aiPanel', 'aiSwitch', 'vision', 'photoBox', 'credit', 'assist', 'assistResult',
    'result', 'players', 'map', 'msg', 'confirm', 'reveal', 'next'].forEach((k) => {
    geo.el[k] = root.querySelector('#geo-' + k.replace(/[A-Z]/g, (c) => '-' + c.toLowerCase()));
  });

  const savedOff = (state && state.config || {}).hall_geo_ai_off;
  geo.aiOff = Array.isArray(savedOff) ? savedOff.map(String) : [];
  geo.aiPanelOpen = false;
  geo.el.aiPick.onclick = (ev) => {
    ev.stopPropagation();
    geo.aiPanelOpen = !geo.aiPanelOpen;
    geoRenderAiPanel();
  };
  geo.el.aiPanel.onclick = (ev) => ev.stopPropagation();
  document.addEventListener('click', geoAiOutside);
  geo.el.aiSwitch.appendChild(hallAiSwitch('geo', 'AI 对手', (on) => {
    geo.aiPanelOpen = false;
    geoRenderAiPanel();
    toast(on ? 'AI 对手已开启（换一局生效）' : 'AI 对手已关闭（换一局生效）');
    if (!geo.round || geo.revealed) geoNewRound();
  }));
  geoRenderAiPanel();
  geo.el.confirm.onclick = geoConfirm;
  geo.el.reveal.onclick = () => geoReveal();
  geo.el.next.onclick = () => geoNewRound();
  geo.el.assist.onclick = geoAssist;
  geo.el.map.innerHTML = '<div class="geo-loading-map">正在加载世界地图…</div>';

  geoNewRound();
}

function geoUnmount() {
  geo.gen += 1;                       // 让在途的取题 / AI 请求全部作废
  geoTickStop();
  geoDestroyZoom();
  if (geo.map && typeof geo.map.remove === 'function') {
    try { geo.map.remove(); } catch (e) { /* 地图容器已被替换 */ }
  }
  geo.map = null;
  geo.mapPromise = null;
  geo.layer = null;
  geo.lineLayer = null;
  geo.markers = {};
  geo.pending = null;
  geo.round = null;
  geo.result = null;
  geo.thinking = {};
  geo.failed = {};
  geo.assist = { loading: false, text: '', open: false, err: '' };
  geo.aiCatalog = [];
  geo.aiOff = [];
  geo.aiPanelOpen = false;
  geo.submitted = false;
  geo.revealed = false;
  geo.busy = false;
  document.removeEventListener('click', geoAiOutside);
  geo.el = {};
}

/* ---------------- AI 选手开关 ----------------
   每个 AI 可以单独关闭；选择写入 config，换一题后生效。 */

function geoAiOutside() {
  if (!geo.aiPanelOpen) return;
  geo.aiPanelOpen = false;
  geoRenderAiPanel();
}

function geoAiEnabledIds() {
  return geo.aiCatalog
    .filter((p) => !geo.aiOff.includes(p.id))
    .map((p) => p.id);
}

function geoRenderAiPanel() {
  const e = geo.el;
  if (!e.aiPick || !e.aiPanel) return;
  const on = hallAiEnabled('geo');
  const total = geo.aiCatalog.length;
  const active = geoAiEnabledIds().length;
  e.aiPick.disabled = !on || !total;
  e.aiPick.textContent = !on ? '已关闭'
    : (total ? active + '/' + total + ' ▾' : '选择 ▾');
  e.aiPick.title = on ? '选择本局出战的 AI（取消勾选即关闭）' : 'AI 对手已关闭';

  const open = on && geo.aiPanelOpen && total > 0;
  e.aiPanel.classList.toggle('hidden', !open);
  e.aiPanel.innerHTML = '';
  if (!open) return;

  const head = document.createElement('div');
  head.className = 'geo-ai-head';
  head.textContent = '本局出战 · 取消勾选即关闭';
  e.aiPanel.appendChild(head);

  geo.aiCatalog.forEach((p) => {
    const row = document.createElement('label');
    row.className = 'geo-ai-row';
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = !geo.aiOff.includes(p.id);
    cb.onchange = () => geoSetAiOn(p.id, cb.checked);
    const info = document.createElement('span');
    info.className = 'geo-ai-info';
    const name = document.createElement('span');
    name.className = 'geo-ai-name';
    name.textContent = p.name;
    if (p.tag) {
      const tag = document.createElement('span');
      tag.className = 'geo-ptag';
      tag.textContent = p.tag;
      name.appendChild(tag);
    }
    const desc = document.createElement('span');
    desc.className = 'geo-ai-desc';
    desc.textContent = p.desc || '';
    info.appendChild(name);
    info.appendChild(desc);
    row.appendChild(cb);
    row.appendChild(info);
    e.aiPanel.appendChild(row);
  });

  const foot = document.createElement('div');
  foot.className = 'geo-ai-foot';
  foot.textContent = '选择会保存，点「换一题」后生效';
  e.aiPanel.appendChild(foot);
}

function geoSetAiOn(id, on) {
  const off = new Set(geo.aiOff);
  if (on) off.delete(id); else off.add(id);
  geo.aiOff = Array.from(off);
  try {
    if (typeof state !== 'undefined' && state.config) {
      state.config.hall_geo_ai_off = geo.aiOff.slice();
    }
    // /api/config 是增量合并：只提交这一个键，避免覆盖别处刚改过的设置
    if (typeof api === 'function') {
      api('/api/config', { hall_geo_ai_off: geo.aiOff }).catch(() => {});
    }
  } catch (e) { /* 配置写入失败不影响本局选择 */ }
  geoRenderAiPanel();
  toast('已保存，换一题后生效');
}

function geoOnShow() {
  if (geo.map && typeof geo.map.invalidateSize === 'function') {
    setTimeout(() => { try { geo.map.invalidateSize(); } catch (e) { /* 容器已销毁 */ } }, 0);
  }
}

/* ---------------- 地图 ---------------- */

function geoLoadScript(urls) {
  return new Promise((resolve, reject) => {
    let i = 0;
    const next = () => {
      if (i >= urls.length) { reject(new Error('所有 CDN 都取不到地图库')); return; }
      const s = document.createElement('script');
      s.src = urls[i++];
      s.async = true;
      s.onload = () => resolve();
      s.onerror = () => { s.remove(); next(); };
      document.head.appendChild(s);
    };
    next();
  });
}

function geoLoadLeaflet() {
  if (window.L) return Promise.resolve();
  if (!geo.cssDone) {
    geo.cssDone = true;
    const l = document.createElement('link');
    l.rel = 'stylesheet';
    l.href = LEAFLET_CSS[0];
    l.onerror = () => {
      l.remove();
      const l2 = document.createElement('link');
      l2.rel = 'stylesheet';
      l2.href = LEAFLET_CSS[1];
      document.head.appendChild(l2);
    };
    document.head.appendChild(l);
  }
  return geoLoadScript(LEAFLET_JS).then(() => {
    if (!window.L) throw new Error('地图库加载后仍不可用');
  });
}

function geoAddTiles(m, idx) {
  if (idx >= GEO_TILES.length) return;
  const layer = window.L.tileLayer(GEO_TILES[idx], { maxZoom: 19, attribution: GEO_TILE_ATTR });
  let swapped = false;
  layer.on('tileerror', () => {
    if (swapped) return;                  // 只换一次，避免单个瓦片抖动就来回切源
    swapped = true;
    m.removeLayer(layer);
    geoAddTiles(m, idx + 1);
  });
  layer.addTo(m);
}

function geoEnsureMap() {
  if (geo.map) return Promise.resolve(geo.map);
  if (geo.mapPromise) return geo.mapPromise;
  const gen = geo.gen;
  geo.mapPromise = geoLoadLeaflet().then(() => {
    const box = geo.el.map;
    if (gen !== geo.gen || !box || !window.L) return null;
    box.innerHTML = '';
    const m = window.L.map(box, {
      minZoom: 1, maxZoom: 18, zoomSnap: 0.5, worldCopyJump: true,
    });
    geoAddTiles(m, 0);
    m.setView([26, 8], 2);
    m.on('click', geoMapClick);
    geo.layer = window.L.layerGroup().addTo(m);
    geo.lineLayer = window.L.layerGroup().addTo(m);   // 连线单独一层，结算时只清它（标点不动）
    geo.map = m;
    return m;
  }).catch((e) => {
    geo.mapPromise = null;
    if (gen === geo.gen && geo.el.map) {
      geo.el.map.innerHTML = '<div class="geo-loading-map">地图加载失败：'
        + geoEsc((e && e.message) || e) + '<br>请检查网络后重试。</div>';
    }
    return null;
  });
  return geo.mapPromise;
}

function geoColorOf(pid) {
  if (!geo.round) return GEO_COLORS[0];
  const i = geo.round.players.findIndex((p) => p.id === pid);
  return GEO_COLORS[(i < 0 ? 0 : i) % GEO_COLORS.length];
}

/* ---------------- 标点与上方标签 ----------------
   标签直接做成 divIcon 的子节点，而不是另起一层浮层：
   Leaflet 每帧只对图标根节点做一次 translate3d（合成层变换，不触发布局），
   标签跟着一起走 —— 于是「平移 / 缩放时重算标签位置」这件事**不需要写任何代码**，
   每帧 JS 开销为 0。另起浮层则要在每个 move 事件里改一次位置，那是白付的成本。 */
function geoPinIcon(o) {
  const color = o.color || GEO_COLORS[0];
  const style = '--c:' + color;
  const tip = o.tip || (o.clickable ? '点击把地图移到这里' : '');
  return window.L.divIcon({
    className: 'geo-pin-wrap' + (o.drag ? ' draggable' : '')
      + (o.clickable ? ' followable' : ''),
    html: '<span class="geo-pin ' + (o.cls || '') + '" style="' + style + '"></span>'
        + '<span class="geo-pin-label ' + (o.cls || '') + (o.below ? ' below' : '')
        + '" style="' + style + '"'
        + (tip ? ' title="' + geoEsc(tip) + '"' : '') + '>'
        + geoEsc(o.label || '') + '</span>',
    iconSize: [22, 22],        // 与 .geo-pin 同尺寸，锚点才是圆心
    iconAnchor: [11, 11],
  });
}

function geoDrawPin(pid, lat, lng, opts) {
  const o = opts || {};
  if (geo.markers[pid]) geo.layer.removeLayer(geo.markers[pid]);
  const drag = !!o.drag;
  const clickable = !drag && o.clickable !== false;
  const mk = window.L.marker([lat, lng], {
    icon: geoPinIcon({ cls: o.cls, color: o.color || geoColorOf(pid),
                       label: o.label, tip: o.tip, drag: drag, below: o.below,
                       clickable: clickable }),
    // 已落地的标点可点击定位；拖拽中的临时点保持原行为
    interactive: drag || clickable,
    draggable: drag,
    keyboard: false,
    bubblingMouseEvents: false,
    zIndexOffset: o.z || 0,
  });
  if (drag && o.onDragEnd) mk.on('dragend', o.onDragEnd);
  if (clickable) {
    mk.on('click', (ev) => {
      if (ev && ev.originalEvent) ev.originalEvent.stopPropagation();
      geoJumpToPin(pid);
    });
  }
  mk.addTo(geo.layer);
  geo.markers[pid] = mk;
  return mk;
}

/* 标签节点缓存：更新内容时不再走一次 querySelector */
function geoLabelEl(mk) {
  if (!mk) return null;
  if (mk._geoLbl && mk._geoLbl.isConnected) return mk._geoLbl;
  const el = typeof mk.getElement === 'function' ? mk.getElement() : null;
  mk._geoLbl = el ? el.querySelector('.geo-pin-label') : null;
  return mk._geoLbl;
}

/* 就地改文字（不重建标点 DOM）：只有真变了才写，避免无谓的重绘 */
function geoSetPinLabel(pid, text) {
  const el = geoLabelEl(geo.markers[pid]);
  if (el && el.textContent !== text) el.textContent = text;
}

/* 点「跟踪」只是把地图飞过去一次，不保持追踪状态 */
function geoJumpToPin(pid) {
  const mk = geo.markers[pid];
  if (!mk || !geo.map) return;
  try {
    // 名单或理由展开会改变右列高度，Leaflet 缓存的尺寸可能还是旧的；
    // 不先同步，飞过去的点会按旧高度居中 —— 表现就是标点偏到中心上方。
    geo.map.invalidateSize({ pan: false });
    geo.map.flyTo(mk.getLatLng(), Math.max(4, geo.map.getZoom()), { duration: 0.6 });
  } catch (e) { /* 地图已销毁 */ }
}

function geoLabelText(name, res) {
  return (res && res.rank != null) ? (name + ' · ' + res.distance_km.toFixed(2) + ' km') : name;
}

function geoHumanId() {
  const p = geo.round && geo.round.players.find((x) => x.kind === 'human');
  return p ? p.id : '';
}

function geoNameOf(pid) {
  const p = geo.round && geo.round.players.find((x) => x.id === pid);
  return p ? p.name : '';
}

function geoMapClick(e) {
  if (!geo.round || !geo.map || geo.revealed || geo.busy || geo.submitted) return;
  const ll = e.latlng;
  if (!geo.pending) {
    geo.pending = geoDrawPin('__pending__', ll.lat, ll.lng, {
      cls: 'pending', color: geoColorOf(geoHumanId()),
      label: geoNameOf(geoHumanId()) || '你',
      drag: true, z: 500, onDragEnd: geoUpdatePending,
    });
  } else {
    geo.pending.setLatLng(ll);
  }
  geoUpdatePending();
}

function geoRemovePending() {
  if (geo.markers.__pending__) geo.layer.removeLayer(geo.markers.__pending__);
  delete geo.markers.__pending__;
  geo.pending = null;
}

function geoUpdatePending() {
  if (!geo.pending) return;
  const ll = geo.pending.getLatLng();
  geoMsg('已选 ' + ll.lat.toFixed(4) + ', ' + ll.lng.toFixed(4)
    + '　—— 可拖动标记微调，确认后不能再改');
  geoSyncButtons();
}

/* ---------------- 对局流程 ---------------- */

function geoMsg(text, kind) {
  const el = geo.el.msg;
  if (!el) return;
  el.textContent = text || '';
  el.classList.toggle('err', kind === 'err');
  el.classList.toggle('ok', kind === 'ok');
}

function geoTickStart() {
  geoTickStop();
  geo.tick = setInterval(() => {
    if (!Object.keys(geo.thinking).some((k) => geo.thinking[k])) { geoTickStop(); return; }
    geoRenderPlayers();
  }, 1000);
}

function geoTickStop() {
  if (geo.tick) { clearInterval(geo.tick); geo.tick = null; }
}

function geoSyncButtons() {
  const e = geo.el;
  if (!e.confirm) return;
  const locked = geo.busy || geo.revealed || !geo.round;
  e.confirm.disabled = locked || geo.submitted || !geo.pending;
  e.confirm.textContent = geo.submitted ? '已确认' : '确认标注';
  e.reveal.disabled = locked || !geo.submitted;
  e.next.disabled = geo.busy;
  e.next.textContent = geo.revealed ? '下一局' : '换一题';
  e.assist.disabled = !geo.round || geo.assist.loading;
  e.assist.textContent = geo.assist.loading ? '分析中…'
    : (geo.assist.open && geo.assist.text ? '收起识图辅助' : '识图辅助');
}

function geoResetRoundView() {
  geo.round = null;
  geo.result = null;
  geo.submitted = false;
  geo.revealed = false;
  geo.thinking = {};
  geo.since = {};
  geo.failed = {};
  geo.assist = { loading: false, text: '', open: false, err: '' };
  geoTickStop();
  geoCloseZoom();                     // 换局时若大图还开着，先收掉（否则会停在新题的画面上）
  geo.pending = null;
  geo.markers = {};
  if (geo.layer) geo.layer.clearLayers();
  if (geo.lineLayer) geo.lineLayer.clearLayers();
  if (geo.el.result) { geo.el.result.classList.add('hidden'); geo.el.result.innerHTML = ''; }
  if (geo.el.assistResult) {
    geo.el.assistResult.classList.add('hidden');
    geo.el.assistResult.innerHTML = '';
  }
  geo.el.players.innerHTML = '';
  geo.el.credit.textContent = '';
  geo.el.photoBox.classList.remove('can-zoom');
  geo.el.photoBox.onclick = null;
  geo.el.photoBox.innerHTML = '<div class="geo-photo-msg">正在从开放图库取一张随机照片…</div>';
  geoSyncButtons();
}

async function geoNewRound() {
  if (geo.busy) return;
  const gen = geo.gen;
  geo.busy = true;
  geo.no += 1;
  geoResetRoundView();
  geo.el.no.textContent = '第 ' + geo.no + ' 局';
  geoMsg('正在取题…');
  geoSyncButtons();

  let data;
  try {
    // AI 关闭时沿用 ai_count=0；开启时用「关闭名单」按人组队，
    // 这样首次请求（还没拿到名册）也能把上次关掉的选手排除掉
    const body = hallAiEnabled('geo') ? { ai_off: geo.aiOff } : { ai_count: 0 };
    data = await api('/api/hall/geo/new', body);
  } catch (e) {
    if (gen !== geo.gen) return;
    geo.busy = false;
    geo.el.photoBox.innerHTML = '<div class="geo-photo-msg">取题失败：'
      + geoEsc(e.message || e) + '<br><button class="geo-retry" id="geo-retry-1">重试</button></div>';
    const b = geo.el.photoBox.querySelector('#geo-retry-1');
    if (b) b.onclick = () => geoNewRound();
    geoMsg('取不到照片，可直接重试', 'err');
    geoSyncButtons();
    return;
  }
  if (gen !== geo.gen) return;

  geo.round = data.round;
  geo.busy = false;
  if (Array.isArray(data.round.ai_roster)) geo.aiCatalog = data.round.ai_roster;
  geoRenderAiPanel();
  geoRenderVision(data.round.ai_vision);
  geoRenderPlayers();
  geoSetPhoto(data.round);
  geoMsg('照片已就绪 —— 在地图上点一下标出你判断的拍摄位置');
  geoSyncButtons();

  geoEnsureMap().then((m) => {
    if (!m || gen !== geo.gen || geo.result) return;
    m.setView([26, 8], 2);
  });
}

function geoEsc(s) {
  return String(s == null ? '' : s).replace(/[&<>"]/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

function geoSetPhoto(round) {
  const box = geo.el.photoBox;
  box.innerHTML = '';
  const img = document.createElement('img');
  img.alt = '待定位的真实照片';
  img.referrerPolicy = 'no-referrer';
  img.src = round.image_url;
  img.onerror = () => {
    box.classList.remove('can-zoom');
    box.onclick = null;
    box.innerHTML = '<div class="geo-photo-msg">照片没能加载出来（图源可能暂时不可达）'
      + '<br><button class="geo-retry" id="geo-retry-2">换一题</button></div>';
    const b = box.querySelector('#geo-retry-2');
    if (b) b.onclick = () => geoNewRound();
  };
  box.appendChild(img);
  // 题面往往要看路牌 / 车牌 / 门牌这类细节才敢下判断，所以给一个全屏大图入口
  const zoom = document.createElement('button');
  zoom.type = 'button';
  zoom.className = 'geo-zoom-btn';
  zoom.title = '放大查看（滚轮缩放 / 拖动平移）';
  zoom.innerHTML = '<svg class="ic"><use href="#i-search"/></svg>';
  zoom.onclick = (e) => { e.stopPropagation(); geoOpenZoom(); };
  box.appendChild(zoom);
  box.classList.add('can-zoom');
  box.onclick = geoOpenZoom;

  const parts = [];
  if (round.taken_at) parts.push('拍摄于 ' + String(round.taken_at).slice(0, 10));
  if (round.credit) parts.push(round.credit);
  if (round.license) parts.push(round.license);
  geo.el.credit.textContent = parts.join(' · ');
}

/* ---------------- 题面放大（全屏大图） ----------------
   浮层挂在 <body> 上（而不是大厅里）：.hall-body 是滚动容器，
   放在里面会被它的裁剪与滚动位置带着跑。 */
const GEO_ZOOM_MIN = 0.1;
const GEO_ZOOM_MAX = 8;

function geoBuildZoom() {
  const wrap = document.createElement('div');
  wrap.className = 'geo-zoom hidden';
  wrap.innerHTML = [
    '<div class="geo-zoom-bar">',
    '  <span class="geo-zoom-pct">100%</span>',
    '  <button type="button" data-z="out" title="缩小">−</button>',
    '  <button type="button" data-z="in" title="放大">＋</button>',
    '  <button type="button" data-z="fit" title="缩放到刚好放得下整张图">适应窗口</button>',
    '  <button type="button" data-z="one" title="一个图片像素对应一个屏幕像素">原始尺寸</button>',
    '  <span class="geo-zoom-spacer"></span>',
    '  <button type="button" data-z="close" title="关闭（Esc）">✕ 关闭</button>',
    '</div>',
    '<div class="geo-zoom-stage"><img alt="待定位的真实照片（放大查看）"></div>',
    '<div class="geo-zoom-hint">滚轮缩放 · 按住拖动平移 · 双击切换「适应窗口 / 原始尺寸」 · 图片外轻点或 Esc 关闭</div>',
  ].join('');
  document.body.appendChild(wrap);

  const stage = wrap.querySelector('.geo-zoom-stage');
  const z = geo.zoom = {
    overlay: wrap, img: wrap.querySelector('img'), stage: stage,
    pct: wrap.querySelector('.geo-zoom-pct'),
    open: false, scale: 1, x: 0, y: 0, drag: null, onKey: null,
  };

  wrap.querySelector('.geo-zoom-bar').addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-z]');
    if (!btn) return;
    const k = btn.dataset.z;
    if (k === 'close') geoCloseZoom();
    else if (k === 'fit') geoZoomFit();
    else if (k === 'one') geoZoomTo(1, 0, 0);
    else geoZoomTo(z.scale * (k === 'in' ? 1.25 : 1 / 1.25), 0, 0);
  });
  stage.addEventListener('wheel', geoZoomWheel, { passive: false });
  stage.addEventListener('pointerdown', geoZoomDown);
  stage.addEventListener('pointermove', geoZoomMove);
  stage.addEventListener('pointerup', geoZoomUp);
  stage.addEventListener('pointercancel', geoZoomCancel);
  stage.addEventListener('dblclick', () => {
    if (Math.abs(z.scale - 1) < 0.01) geoZoomFit(); else geoZoomTo(1, 0, 0);
  });
  z.onKey = (e) => {
    if (e.key === 'Escape' && z.open) { e.stopPropagation(); geoCloseZoom(); }
  };
  return z;
}

function geoOpenZoom() {
  const src = geo.el.photoBox && geo.el.photoBox.querySelector('img');
  if (!src || !src.src) return;
  const z = geo.zoom || geoBuildZoom();
  if (z.open) return;
  z.img.src = src.currentSrc || src.src;
  z.open = true;
  z.overlay.classList.remove('hidden');
  window.addEventListener('keydown', z.onKey, true);
  // 先按「适应窗口」开场；图片可能还没解码完，等 load 再算一次
  if (z.img.complete && z.img.naturalWidth) requestAnimationFrame(geoZoomFit);
  else z.img.onload = () => requestAnimationFrame(geoZoomFit);
}

function geoCloseZoom() {
  const z = geo.zoom;
  if (!z || !z.open) return;
  z.open = false;
  z.drag = null;
  z.stage.classList.remove('dragging');
  z.overlay.classList.add('hidden');
  window.removeEventListener('keydown', z.onKey, true);
  z.img.removeAttribute('src');          // 松掉这张大图，别一直占着解码后的内存
}

function geoDestroyZoom() {
  const z = geo.zoom;
  if (!z) return;
  geoCloseZoom();
  window.removeEventListener('keydown', z.onKey, true);
  if (z.overlay.parentNode) z.overlay.parentNode.removeChild(z.overlay);
  geo.zoom = null;
}

/* 以舞台内某点（相对舞台中心的偏移）为锚点缩放到目标倍数：
   锚点对应的图像位置要保持不动 → x' = ax - (ax - x) · (s'/s) */
function geoZoomTo(scale, ax, ay) {
  const z = geo.zoom;
  if (!z) return;
  const next = Math.max(GEO_ZOOM_MIN, Math.min(GEO_ZOOM_MAX, scale));
  if (next === z.scale) return;
  const k = next / z.scale;
  z.x = ax - (ax - z.x) * k;
  z.y = ay - (ay - z.y) * k;
  z.scale = next;
  geoZoomApply();
}

function geoZoomFit() {
  const z = geo.zoom;
  if (!z || !z.img.naturalWidth) return;
  const r = z.stage.getBoundingClientRect();
  if (!r.width || !r.height) return;
  z.scale = Math.max(GEO_ZOOM_MIN, Math.min(GEO_ZOOM_MAX,
    Math.min(r.width / z.img.naturalWidth, r.height / z.img.naturalHeight)));
  z.x = 0;
  z.y = 0;
  geoZoomApply();
}

function geoZoomApply() {
  const z = geo.zoom;
  if (!z) return;
  // 前面的 translate(-50%,-50%) 负责把图片摆到舞台正中（配合 CSS 的 left/top:50%），
  // 于是「图片中心相对舞台中心的偏移」就正好等于 (x, y) —— 锚点缩放公式才成立
  z.img.style.transform = 'translate(-50%,-50%) translate(' + z.x + 'px,' + z.y + 'px)'
    + ' scale(' + z.scale + ')';
  z.pct.textContent = Math.round(z.scale * 100) + '%';
}

function geoZoomWheel(e) {
  const z = geo.zoom;
  if (!z || !z.open) return;
  e.preventDefault();
  const r = z.stage.getBoundingClientRect();
  geoZoomTo(z.scale * (e.deltaY < 0 ? 1.12 : 1 / 1.12),
    e.clientX - (r.left + r.width / 2), e.clientY - (r.top + r.height / 2));
}

function geoZoomDown(e) {
  const z = geo.zoom;
  if (!z || !z.open || e.button !== 0) return;
  z.drag = { sx: e.clientX, sy: e.clientY, x: z.x, y: z.y,
             moved: 0, onImg: e.target === z.img };
  z.stage.classList.add('dragging');
  try { z.stage.setPointerCapture(e.pointerId); } catch (err) { /* 不支持捕获也能拖 */ }
}

function geoZoomMove(e) {
  const z = geo.zoom;
  if (!z || !z.drag) return;
  z.x = z.drag.x + (e.clientX - z.drag.sx);
  z.y = z.drag.y + (e.clientY - z.drag.sy);
  z.drag.moved = Math.max(z.drag.moved,
    Math.abs(e.clientX - z.drag.sx), Math.abs(e.clientY - z.drag.sy));
  geoZoomApply();
}

function geoZoomUp() {
  const z = geo.zoom;
  if (!z) return;
  const d = z.drag;
  z.drag = null;
  z.stage.classList.remove('dragging');
  // 在图片外的空白处轻点即关（常见看图习惯）；点在图片上不关，免得放大看细节时误触
  if (d && !d.onImg && d.moved < 4) geoCloseZoom();
}

function geoZoomCancel() {
  const z = geo.zoom;
  if (!z) return;
  z.drag = null;
  z.stage.classList.remove('dragging');
}

function geoRenderVision(mode) {
  const el = geo.el.vision;
  if (!el) return;
  if (mode === 'none') {
    el.textContent = 'AI 只能盲猜（未配置模型密钥）';
    el.title = '在「设置 → 模型接入」里配置视觉模型或聊天模型后，AI 才能真正看图判断位置';
  } else if (mode === 'chat') {
    el.textContent = 'AI 用聊天模型看图';
    el.title = '未配置视觉模型，AI 退而使用聊天模型识图；若该模型不支持图像，AI 会退化为盲猜';
  } else {
    el.textContent = 'AI 用视觉模型看图';
    el.title = 'AI 使用已配置的视觉模型判断拍摄地点';
  }
}

/* 玩家识图辅助：只有点按钮才请求；只描述画面特征，不判断国家/城市 */
async function geoAssist() {
  if (!geo.round || geo.assist.loading) return;
  if (geo.assist.open) {
    geo.assist.open = false;
    geoRenderAssist();
    geoSyncButtons();
    return;
  }
  geo.assist.open = true;
  geo.assist.err = '';
  if (geo.assist.text) {              // 本局已分析过，展开即看，不再请求
    geoRenderAssist();
    geoSyncButtons();
    return;
  }
  const gen = geo.gen;
  const rid = geo.round.id;
  geo.assist.loading = true;
  geoRenderAssist();
  geoSyncButtons();
  try {
    const r = await api('/api/hall/geo/analyze', { round_id: rid });
    if (gen !== geo.gen || !geo.round || geo.round.id !== rid) return;
    geo.assist.text = String(r.text || '').trim();
    if (!geo.assist.text) geo.assist.err = '模型没有返回可用描述，可稍后再试';
  } catch (e) {
    if (gen !== geo.gen || !geo.round || geo.round.id !== rid) return;
    geo.assist.err = e.message || String(e);
  }
  geo.assist.loading = false;
  geoRenderAssist();
  geoSyncButtons();
}

function geoRenderAssist() {
  const box = geo.el.assistResult;
  if (!box) return;
  const a = geo.assist;
  const show = a.open && (a.loading || a.text || a.err);
  box.classList.toggle('hidden', !show);
  box.innerHTML = '';
  if (!show) return;

  const title = document.createElement('div');
  title.className = 'geo-assist-title';
  title.textContent = '画面特征 · 只观察，不判断地点';
  box.appendChild(title);

  if (a.loading) {
    const l = document.createElement('div');
    l.className = 'geo-assist-loading';
    l.textContent = '正在读图…';
    box.appendChild(l);
    return;
  }
  if (a.err) {
    const err = document.createElement('div');
    err.className = 'geo-assist-err';
    err.textContent = a.err;
    box.appendChild(err);
    return;
  }
  const ul = document.createElement('ul');
  ul.className = 'geo-assist-list';
  a.text.split(/\r?\n/).forEach((s) => {
    const t = s.trim();
    if (!t) return;
    const li = document.createElement('li');
    li.textContent = t;
    ul.appendChild(li);
  });
  box.appendChild(ul);
}

async function geoConfirm() {
  if (!geo.round || !geo.pending || geo.busy || geo.submitted) return;
  const gen = geo.gen;
  const ll = geo.pending.getLatLng();
  geo.busy = true;
  geoSyncButtons();
  geoMsg('正在提交你的标注…');
  try {
    const r = await api('/api/hall/geo/guess', {
      round_id: geo.round.id, player_id: geoHumanId(), lat: ll.lat, lng: ll.lng,
    });
    if (gen !== geo.gen) return;
    geo.round.guesses[geoHumanId()] = r.guess;
    geo.submitted = true;
    geoDrawPin(geoHumanId(), r.guess.lat, r.guess.lng, { label: geoNameOf(geoHumanId()) });
    geoRemovePending();
  } catch (e) {
    if (gen !== geo.gen) return;
    geo.busy = false;
    geoMsg('提交失败：' + (e.message || e), 'err');
    geoSyncButtons();
    return;
  }
  geo.busy = false;
  geoRenderPlayers();
  geoSyncButtons();

  if (!geo.round.players.some((p) => p.kind === 'ai')) { geoReveal(); return; }
  geoRunAI();
}

async function geoRunAI() {
  if (!geo.round) return;
  const gen = geo.gen;
  const list = geo.round.players.filter((p) => p.kind === 'ai' && !geo.round.guesses[p.id]);
  if (!list.length) { geoMaybeReveal(); return; }
  list.forEach((p) => { geo.thinking[p.id] = true; geo.since[p.id] = Date.now(); });
  geoRenderPlayers();
  geoTickStart();
  geoMsg('AI 正在看图判断位置 —— 模型要把整张照片读完再推理，通常十几到几十秒');
  // 并行发起：三个人各算各的，总时长取决于最慢的那个，而不是三者相加
  await Promise.all(list.map((p) => geoAskOne(p)));
  if (gen !== geo.gen) return;
  geoRenderPlayers();
  const stuck = geo.round.players.filter((p) => !geo.round.guesses[p.id]);
  if (stuck.length) {
    geoMsg('有 ' + stuck.length + ' 位没能给出标注（见上方名单），你可以直接结算', 'err');
    geoSyncButtons();
    return;
  }
  geoMaybeReveal();
}

async function geoAskOne(p) {
  const gen = geo.gen;
  try {
    const r = await api('/api/hall/geo/ai', { round_id: geo.round.id, player_id: p.id });
    if (gen !== geo.gen || !geo.round) return;
    geo.round.guesses[p.id] = r.guess;
    geoDrawPin(p.id, r.guess.lat, r.guess.lng, { label: geoLabelText(p.name, null) });
  } catch (e) {
    geo.failed[p.id] = e.message || String(e);
  } finally {
    if (gen === geo.gen) { geo.thinking[p.id] = false; geoRenderPlayers(); }
  }
}

function geoMaybeReveal() {
  if (!geo.round || geo.revealed) return;
  if (geo.round.players.some((p) => !geo.round.guesses[p.id])) return;
  geoReveal();
}

async function geoReveal() {
  if (!geo.round || geo.revealed || geo.busy) return;
  const gen = geo.gen;
  geo.busy = true;
  geoSyncButtons();
  geoMsg('正在计算每位参与者与真实拍摄点的距离…');
  try {
    const r = await api('/api/hall/geo/reveal', { round_id: geo.round.id });
    if (gen !== geo.gen) return;
    geo.result = r.result;
    geo.revealed = true;
    geoDrawReveal();
    geoRenderPlayers();
    geoRenderResult();
  } catch (e) {
    if (gen !== geo.gen) return;
    geoMsg('结算失败：' + (e.message || e), 'err');
  } finally {
    if (gen === geo.gen) { geo.busy = false; geoSyncButtons(); }
  }
}

/* 结算：只补「连线 + 真值点」，玩家标点**就地复用**（不重建 DOM、不重新 addTo），
   标签也只改文字 —— 于是结算这一帧没有节点增删引起的样式重算，
   只剩下新增的 1 个真值标点与 n 条连线需要绘制。 */
function geoDrawReveal() {
  const res = geo.result;
  if (!res || !geo.map) return;
  geo.lineLayer.clearLayers();
  const pts = [];
  res.results.forEach((x) => {
    if (x.lat == null || x.lng == null) return;
    const mk = geo.markers[x.player_id];
    // 名次越靠前越后画：标签叠在一起时，看得清的是成绩更好的那个
    if (mk) mk.setZIndexOffset(1000 - (x.rank || 99));
    geoSetPinLabel(x.player_id, geoLabelText(x.name, x));
    window.L.polyline([[x.lat, x.lng], [res.truth.lat, res.truth.lng]],
      { color: geoColorOf(x.player_id), weight: 1.6, opacity: 0.8, dashArray: '5,5' })
      .addTo(geo.lineLayer);
    pts.push([x.lat, x.lng]);
  });
  geoDrawPin('__truth__', res.truth.lat, res.truth.lng, {
    cls: 'truth', color: '#f14c4c', z: 5000, below: true,
    label: geoTruthLabel(res.truth),
    tip: '真实拍摄点：' + res.truth.lat.toFixed(4) + ', ' + res.truth.lng.toFixed(4)
         + (res.credit ? '（' + res.credit + '）' : '') + ' · 点击定位',
  });
  pts.push([res.truth.lat, res.truth.lng]);
  geoFitPoints(pts);
}

function geoTruthLabel() {
  return '真实位置';
}

/* 视野适配：直接用已知坐标算边界，不去遍历图层读元素 */
function geoFitPoints(pts) {
  if (!geo.map || !pts || !pts.length) return;
  try { geo.map.invalidateSize({ pan: false }); } catch (e) { /* 地图已销毁 */ }
  geo.map.fitBounds(window.L.latLngBounds(pts).pad(0.25), { padding: [28, 28], maxZoom: 9 });
}

function geoRenderPlayers() {
  const box = geo.el.players;
  if (!box || !geo.round) return;
  box.innerHTML = '';
  const rows = geo.result ? geo.result.results : null;
  const list = geo.round.players.slice();
  if (rows) {
    const rankOf = (id) => {
      const r = rows.find((x) => x.player_id === id);
      return r && r.rank != null ? r.rank : 99;
    };
    list.sort((a, b) => rankOf(a.id) - rankOf(b.id));
  }
  list.forEach((p) => box.appendChild(geoPlayerEl(p, rows)));
}

function geoPlayerEl(p, rows) {
  const res = rows ? rows.find((x) => x.player_id === p.id) : null;
  const guess = geo.round.guesses[p.id];

  const wrap = document.createElement('div');
  wrap.className = 'geo-pblock';
  const row = document.createElement('div');
  row.className = 'geo-player';
  if (res && res.rank === 1) row.classList.add('win');
  else if (guess) row.classList.add('done');

  const dot = document.createElement('span');
  dot.className = 'geo-dot';
  dot.style.setProperty('--c', geoColorOf(p.id));
  const nm = document.createElement('span');
  nm.className = 'geo-pname';
  nm.textContent = p.name;
  if (p.desc) nm.title = p.desc;      // 风格介绍：悬停名字即可看
  row.appendChild(dot);
  row.appendChild(nm);
  if (p.kind === 'ai') {
    const t = document.createElement('span');
    t.className = 'geo-ptag';
    t.textContent = p.tag || 'AI';
    row.appendChild(t);
  }
  const st = document.createElement('span');
  st.className = 'geo-pstate';
  if (guess || res) {
    const fb = document.createElement('button');
    fb.type = 'button';
    fb.className = 'geo-follow';
    fb.textContent = '跟踪';
    fb.title = '把地图移到这个标点';
    fb.onclick = (ev) => {
      ev.stopPropagation();
      geoJumpToPin(p.id);
    };
    row.appendChild(fb);
  }
  if (res) {
    st.textContent = res.rank == null ? '未标注'
      : ('第 ' + res.rank + ' 名 · ' + res.distance_km.toFixed(2) + ' km');
  } else if (guess) {
    st.textContent = '已标注';
  } else if (geo.thinking[p.id]) {
    const sec = Math.round((Date.now() - (geo.since[p.id] || Date.now())) / 1000);
    st.textContent = '思考中… ' + sec + 's';
  } else if (geo.failed[p.id]) {
    st.textContent = '未能标注';
  } else {
    st.textContent = '待标注';
  }
  row.appendChild(st);
  wrap.appendChild(row);

  const note = (res && res.note) || (guess && guess.note) || geo.failed[p.id] || '';
  if (note) {
    const n = document.createElement('div');
    n.className = 'geo-pnote';
    n.textContent = ((res && res.mode) === 'blind' || (guess && guess.mode === 'blind')
      ? '盲猜：' : '') + note;
    wrap.appendChild(n);
  }
  // 选点理由：每人标注完成后立刻展开（AI 才有，真人留空）
  const reason = (res && Array.isArray(res.reason) && res.reason.length)
    ? res.reason
    : ((guess && Array.isArray(guess.reason)) ? guess.reason : []);
  if (reason.length) {
    const d = document.createElement('details');
    d.className = 'geo-preason';
    d.open = true;
    const s = document.createElement('summary');
    s.textContent = (p.kind === 'ai' ? 'AI 选点理由' : '选点理由')
      + '（' + reason.length + ' 条）';
    d.appendChild(s);
    const ul = document.createElement('ul');
    reason.forEach((x) => {
      const li = document.createElement('li');
      li.textContent = String(x);
      ul.appendChild(li);
    });
    d.appendChild(ul);
    wrap.appendChild(d);
  }
  return wrap;
}

function geoRenderResult() {
  const res = geo.result;
  const box = geo.el.result;
  if (!res || !box) return;
  box.innerHTML = '';
  const win = res.winner;
  const head = document.createElement('div');
  if (win) {
    head.innerHTML = '本局冠军：<b>' + geoEsc(win.name) + '</b>（'
      + win.distance_km.toFixed(2) + ' km）';
  } else {
    head.textContent = '本局无人完成标注';
  }
  const truth = document.createElement('div');
  truth.className = 'geo-truth';
  truth.textContent = '真实拍摄点：' + res.truth.lat.toFixed(4) + ', ' + res.truth.lng.toFixed(4);
  box.appendChild(head);
  box.appendChild(truth);
  if (geo.map && geo.markers.__truth__) {
    const fb = document.createElement('button');
    fb.type = 'button';
    fb.className = 'geo-btn ghost';
    fb.textContent = '跟踪真实位置';
    fb.onclick = () => geoJumpToPin('__truth__');
    box.appendChild(fb);
  }
  box.classList.remove('hidden');
  geoMsg(win && win.kind === 'human' ? '你赢了这一局' : '本局结束，可以开下一局',
    win && win.kind === 'human' ? 'ok' : '');
}
