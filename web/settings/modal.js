'use strict';

/* 设置弹窗 · modal.js
   弹窗焦点栈 / 动态面板 / 全局规则 / 设置卡片
   原 web/settings.js 拆分而来：按 index.html 中的顺序加载，共享全局作用域。 */

/* 弹窗焦点约束：挂在 document 上而不是逐个弹窗上。
   原因：焦点可能已经落到背景（浏览器自动化 / 鼠标点击某些元素不会转移焦点），
   只有监听 document 的捕获阶段才能把 Tab 与 focus 拉回弹窗内。
   多个弹窗可能叠加，用栈记录「当前最上层」的那个。 */
const FOCUSABLE_SEL = 'a[href],button:not([disabled]),input:not([disabled]),'
  + 'select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])';
const _modalStack = [];
/* 焦点约束的豁免名单：划词问答 / 划词工具条 / 笔记批注这些浮层 append 在 body 上、
   DOM 位置在弹窗（#notes-modal 等）之外，但它们是从弹窗内容里「长出来」的子界面。
   不豁免的话：弹窗开着时点浮层里的输入框，focusin 会立刻把焦点拉回弹窗，
   表现为「看得见、点不出光标、打不了字」（与 selkit.js insideOwn 同一份名单）。 */
const MODAL_FOCUS_EXEMPT = '.askkit, .selbar, .selbar-more-menu, .nt-anno-pop';
function _inExemptLayer(node) {
  return !!(node && node.closest && node.closest(MODAL_FOCUS_EXEMPT));
}
function _topModal() { return _modalStack[_modalStack.length - 1] || null; }
function _focusablesIn(box) {
  return Array.from(box.querySelectorAll(FOCUSABLE_SEL))
    .filter((n) => n.getClientRects().length && n.getAttribute('aria-hidden') !== 'true');
}
function _pushModal(el) {
  if (!el || _modalStack.indexOf(el) >= 0) return;
  _modalStack.push(el);
}
function _popModal(el) {
  const i = _modalStack.indexOf(el);
  if (i >= 0) _modalStack.splice(i, 1);
}
document.addEventListener('keydown', (e) => {
  const box = _topModal();
  if (!box || e.key !== 'Tab') return;
  if (_inExemptLayer(document.activeElement)) return;   // 浮层内 Tab 走自然顺序
  const list = _focusablesIn(box);
  if (!list.length) { e.preventDefault(); return; }
  const first = list[0];
  const last = list[list.length - 1];
  const active = document.activeElement;
  if (e.shiftKey && (active === first || !box.contains(active))) {
    e.preventDefault(); last.focus();
  } else if (!e.shiftKey && (active === last || !box.contains(active))) {
    e.preventDefault(); first.focus();
  }
}, true);
document.addEventListener('focusin', (e) => {
  const box = _topModal();
  if (!box || box.contains(e.target)) return;
  if (_inExemptLayer(e.target)) return;
  const list = _focusablesIn(box);
  if (list.length) list[0].focus({ preventScroll: true });
}, true);
// 记录最后一次按下的可点元素：有些触发方式不会让按钮拿到焦点，
// 关闭弹窗时用它兜底，焦点才回得到「设置」这类按钮上。
let _lastDownTarget = null;
let _lastDownAt = 0;
document.addEventListener('pointerdown', (e) => {
  const t = e.target && e.target.closest
    ? e.target.closest('button, a, [role="button"], .snav-btn') : null;
  if (t) { _lastDownTarget = t; _lastDownAt = Date.now(); }
}, true);
// 程序化 click（自动化 / el.click()）不产生 pointerdown，这里再兜一层
document.addEventListener('click', (e) => {
  const t = e.target && e.target.closest
    ? e.target.closest('button, a, [role="button"], .snav-btn') : null;
  if (t) { _lastDownTarget = t; _lastDownAt = Date.now(); }
}, true);

/* 浮层显隐：退场先播动画，animationend（或兜底超时）后再真正 display:none，
   避免关闭时「啪」地消失 */
function hideWithAnim(el, done) {
  if (!el || el.classList.contains('hidden')) { if (done) done(); return; }
  let fired = false;
  const finish = () => {
    if (fired) return;
    fired = true;
    el.removeEventListener('animationend', finish);
    _popModal(el);
    el.classList.remove('closing');
    el.classList.add('hidden');
    restoreFocusAfterModal();
    if (done) done();
  };
  el.classList.add('closing');
  el.addEventListener('animationend', finish);
  setTimeout(finish, 300);   // 兜底：动画被禁用或被中断时也能收起
}
function showWithAnim(el) {
  if (!el) return;
  const wasHidden = el.classList.contains('hidden');   // 只有「真的从关到开」才记触发元素
  // 复位上一轮拖动留下的位置（窗口重新打开时回到居中）
  const card = el.querySelector ? el.querySelector('.modal-card') : null;
  if (card) { card.style.transform = ''; card.style.animation = ''; }
  el.classList.remove('closing', 'hidden');
  if (!wasHidden) return;
  _pushModal(el);
  // 触发元素优先取「刚刚按下的那个按钮」：部分点击（自动化 / 某些控件）不会转移焦点，
  // 只看 activeElement 会把焦点还给更早之前的按钮。
  const act = document.activeElement;
  const recentClick = _lastDownTarget && (Date.now() - _lastDownAt < 1500)
    && document.contains(_lastDownTarget);
  _modalOpener = recentClick ? _lastDownTarget
    : ((act && act !== document.body) ? act : _lastDownTarget);
  // 把焦点交给弹窗容器（而不是输入框）：Tab 就会走进弹窗内部而不是背景页面，
  // 同时不会在移动端一打开就弹出软键盘。卡片补个 tabindex="-1" 才能接收焦点。
  if (card) {
    if (!card.hasAttribute('tabindex')) card.setAttribute('tabindex', '-1');
    try { card.focus({ preventScroll: true }); } catch (e) { /* 忽略 */ }
  }
}
/* 弹窗焦点管理（7 个弹窗共用这两个函数，一处生效）：
   打开时焦点进弹窗 → 关闭时还给原来的触发按钮，避免键盘用户「焦点丢失」。 */
let _modalOpener = null;
function restoreFocusAfterModal() {
  const el = _modalOpener;
  _modalOpener = null;
  if (!el || !document.contains(el) || typeof el.focus !== 'function') return;
  try { el.focus({ preventScroll: true }); } catch (e) { /* 忽略 */ }
}
// 是否处于「打开」状态（退场动画进行中也算已关闭）
function isOpen(el) {
  return !!el && !el.classList.contains('hidden') && !el.classList.contains('closing');
}
// 内容区重新淡入（遮住 vis / ECharts 初始化时的空白）
function replayFadeIn(el) {
  if (!el) return;
  el.classList.remove('fade-in');
  void el.offsetWidth;
  el.classList.add('fade-in');
}

function deepCopy(obj) {
  return obj == null ? obj : JSON.parse(JSON.stringify(obj));
}

/* 动态面板（依赖配置的页）统一在这里重建，打开设置与「恢复默认」共用 */
/* ============================================================
   按需挂载：只把当前页的 pane 留在 DOM 里。
   离开的 pane 不销毁，移到 SETTINGS_PANE_STORE（detached），字段值 / 事件 /
   装饰状态都保留；collectSettings / fillStaticSettings 通过 $ 的
   __gmDetachedEl 回退继续读写它们，保存时不会丢字段。
   ============================================================ */
const SETTINGS_PANE_STORE = new Map();
const SETTINGS_FIELD_INDEX = new Map();
let settingsLazyReady = false;

window.__gmDetachedEl = function (sel) {
  if (typeof sel !== 'string' || sel.charAt(0) !== '#' || sel.indexOf(' ') >= 0) return null;
  return SETTINGS_FIELD_INDEX.get(sel.slice(1)) || null;
};

function allSettingsPanes() {
  return $$('#modal .pane').concat(Array.from(SETTINGS_PANE_STORE.values()));
}

function settingsPaneEl(name) {
  return document.querySelector('#modal .pane[data-pane="' + name + '"]')
    || SETTINGS_PANE_STORE.get(name) || null;
}

function indexPaneFields(pane) {
  if (!pane) return;
  pane.querySelectorAll('[id]').forEach((el) => SETTINGS_FIELD_INDEX.set(el.id, el));
}

function forgetPane(name) {
  const pane = SETTINGS_PANE_STORE.get(name);
  if (!pane) return;
  pane.querySelectorAll('[id]').forEach((el) => {
    if (SETTINGS_FIELD_INDEX.get(el.id) === el) SETTINGS_FIELD_INDEX.delete(el.id);
  });
  SETTINGS_PANE_STORE.delete(name);
}

function attachPane(name) {
  const pane = SETTINGS_PANE_STORE.get(name);
  if (!pane) return null;
  SETTINGS_PANE_STORE.delete(name);
  const content = $('#modal .settings-content');
  if (content) content.appendChild(pane);
  return pane;
}

function detachPane(pane) {
  if (!pane || !pane.dataset.pane) return;
  indexPaneFields(pane);
  SETTINGS_PANE_STORE.set(pane.dataset.pane, pane);
  pane.remove();
}

/* 只保留 names 里的 pane；其余移入 detached 存储 */
function keepOnlyPanes(names) {
  const keep = new Set(names || []);
  allSettingsPanes().forEach((pane) => {
    if (!keep.has(pane.dataset.pane)) detachPane(pane);
  });
}

function settingsQuery(sel) {
  const hit = document.querySelector(sel);
  if (hit) return hit;
  for (const pane of allSettingsPanes()) {
    const el = pane.querySelector(sel);
    if (el) return el;
  }
  return null;
}

function buildDynamicPanes() {
  const content = $('#modal .settings-content');
  if (!content) return [];
  const dynamic = ['chat', 'search', 'context', 'agent', 'flow', 'ext'];
  dynamic.forEach((name) => {
    forgetPane(name);                       // 丢掉上一轮 detached 的旧面板
    const old = document.querySelector('#modal .pane[data-pane="' + name + '"]');
    if (old) old.remove();
  });
  const anchor = content.querySelector('[data-pane="profile"]');
  // 模型接入 = 聊天 + 视觉；联网与数据源 = 搜索 + B 站
  const modelPane = buildApiPane('chat', mkPane('chat'));
  buildApiPane('vision', modelPane);
  const netPane = buildSearchPane();
  buildBiliPane(netPane);
  const panes = [modelPane, netPane, buildContextPane()].concat(buildAgentPane());
  panes.forEach((p) => content.insertBefore(p, anchor));
  return panes;
}

/* ---- 全局规则：多条规则 + 单条开关 ----
   存储：user_profile.json 的 rules=[{id,text,enabled}]；旧字段 manual 载入时折算成一条，
   保存后只写 rules。注入文本由后端 profile.manual_block() 拼接（只取启用的）。 */
let profileRuleSeq = 0;

function profileRulesFrom(p) {
  const src = (p && Array.isArray(p.rules)) ? p.rules : [];
  if (src.length) return src.map(normalizeProfileRule);
  const legacy = String((p && p.manual) || '').trim();     // 老配置：单条文本
  return legacy ? [{ id: 'r_legacy', text: legacy, enabled: true }] : [];
}

function normalizeProfileRule(r) {
  if (typeof r === 'string') return { id: '', text: r, enabled: true };
  const o = r || {};
  return {
    id: String(o.id || ''),
    text: String(o.text || ''),
    enabled: o.enabled !== false && String(o.enabled).toLowerCase() !== 'false',
  };
}

function renumberProfileRules() {
  const box = $('#profile-rules');
  if (!box) return;
  const rows = box.querySelectorAll('.rule-item');
  Array.prototype.forEach.call(rows, (row, i) => {
    const idx = row.querySelector('.rule-index');
    if (idx) idx.textContent = '规则 ' + (i + 1);
  });
}

function buildProfileRuleRow(rule) {
  profileRuleSeq += 1;
  const r = normalizeProfileRule(rule);
  const row = document.createElement('div');
  row.className = 'rule-item' + (r.enabled ? '' : ' off');
  row.dataset.id = r.id || ('r_' + Date.now().toString(36) + profileRuleSeq);
  row.innerHTML =
    '<div class="rule-head">' +
      '<label class="ui-switch" title="启用 / 停用这条规则">' +
        '<input type="checkbox" data-act="rule-toggle"' + (r.enabled ? ' checked' : '') + '>' +
        '<span class="ui-switch-track"></span>' +
      '</label>' +
      '<span class="rule-index"></span>' +
      '<button type="button" class="rule-expand" data-act="rule-expand" ' +
        'aria-expanded="false" title="展开 / 收起这条规则的编辑框">展开</button>' +
      '<button type="button" class="rule-del" data-act="rule-del">删除</button>' +
    '</div>' +
    '<textarea class="ta rule-text" rows="3" ' +
      'placeholder="例如：每次先指出我最核心的问题，再给证据和下一步。"></textarea>';
  row.querySelector('.rule-text').value = r.text || '';
  return row;
}

function renderProfileRules(rules) {
  const box = $('#profile-rules');
  if (!box) return;
  box.innerHTML = '';
  const list = Array.isArray(rules) ? rules : [];
  if (!list.length) {
    const tip = document.createElement('p');
    tip.className = 'hint rule-empty';
    tip.textContent = '还没有规则。点下面「添加一条规则」开始写。';
    box.appendChild(tip);
    return;
  }
  list.forEach((r) => box.appendChild(buildProfileRuleRow(r)));
  renumberProfileRules();
}

function collectProfileRules() {
  const box = $('#profile-rules');
  if (!box) return [];
  const out = [];
  Array.prototype.forEach.call(box.querySelectorAll('.rule-item'), (row) => {
    const ta = row.querySelector('.rule-text');
    const text = ta ? ta.value.trim() : '';
    if (!text) return;                       // 空规则不写盘
    const cb = row.querySelector('input[data-act="rule-toggle"]');
    out.push({ id: row.dataset.id || '', text: text, enabled: !cb || cb.checked });
  });
  return out;
}

function addProfileRule() {
  const box = $('#profile-rules');
  if (!box) return;
  const tip = box.querySelector('.rule-empty');
  if (tip) tip.remove();
  const row = buildProfileRuleRow({ text: '', enabled: true });
  box.appendChild(row);
  renumberProfileRules();
  const ta = row.querySelector('.rule-text');
  if (ta) ta.focus();
}

/* 静态面板的值回填：打开设置与「恢复默认」共用 */
function fillStaticSettings(cfg, profile) {
  const p = profile || {};
  const c = cfg || {};
  $('#profile-rules') && renderProfileRules(profileRulesFrom(p));
  $('#profile-auto-learn').checked = p.auto_learn_enabled !== false;
  $('#learning-prompt').value = p.learning_prompt_custom || '';
  buildLearningPresetSelect(c);

  // 以当前实际界面为准（左下角按钮可能刚切过主题，尚未保存）
  const theme = normalizeTheme(document.documentElement.getAttribute('data-theme') || c.theme);
  const radio = settingsQuery('input[name="theme"][value="' + theme + '"]');
  if (radio) radio.checked = true;
  syncThemeCards(theme);
  syncAuroraPrefs();

  // GAL 演出设置面板已移除（模式入口不再暴露）；这里只保留通用外观项
  if ($('#ide-whitespace')) $('#ide-whitespace').value = c.ide_whitespace || 'all';
  if ($('#ide-autosave')) $('#ide-autosave').value = c.ide_autosave || 'off';
  if ($('#ui-process-follow')) $('#ui-process-follow').checked = c.ui_process_follow !== false;

  const sp = $('#speech-polish');
  if (sp) sp.checked = !!c.speech_polish;
  const sd = $('#speech-denoise');
  if (sd) { sd.checked = !!c.speech_denoise; sd.onchange = () => toggleDenoise(sd.checked); }

  const ie = $('#image-enabled'); if (ie) ie.checked = !!c.image_enabled;
  const iu = $('#image-comfy-url');
  if (iu) iu.value = c.image_comfy_url || 'http://127.0.0.1:8188';
  const iw = $('#image-workflow'); if (iw) iw.value = c.image_workflow || '';
  const te2 = $('#traj-enabled');
  if (te2) te2.checked = c.trajectory_enabled !== false;
  const mp = $('#mobile-port');
  if (mp) mp.value = String(c.mobile_port != null ? c.mobile_port : 57890);
  const md = $('#mobile-device-days');
  if (md) md.value = String(c.mobile_device_days != null ? c.mobile_device_days : 180);
  const tm2 = $('#traj-max-field');
  if (tm2) tm2.value = String(c.trajectory_max_field != null ? c.trajectory_max_field : 200000);
  const ci = $('#codeindex-enabled');
  if (ci) ci.checked = c.codeindex_enabled !== false;
  const cimf = $('#codeindex-max-files');
  if (cimf) cimf.value = String(c.codeindex_max_files != null ? c.codeindex_max_files : 0);
  const cimc = $('#codeindex-max-chunks');
  if (cimc) cimc.value = String(c.codeindex_max_chunks != null ? c.codeindex_max_chunks : 0);
  const cikb = $('#codeindex-max-file-kb');
  if (cikb) cikb.value = String(c.codeindex_max_file_kb != null ? c.codeindex_max_file_kb : 512);
  // 自动派发（子代理页）
  const ar = $('#agent-auto-route');
  if (ar) ar.checked = c.agent_auto_route !== false;
  const ap = $('#agent-auto-plan');
  if (ap) ap.checked = c.agent_auto_plan !== false;
  const av = $('#agent-auto-review');
  if (av) av.checked = !!c.agent_auto_review;
  const apr = $('#agent-plan-review');
  if (apr) apr.checked = c.agent_plan_review !== false;
  const asa = $('#agent-script-agents');
  if (asa) asa.checked = c.agent_script_agents !== false;
  const asn = $('#agent-script-agent-calls');
  if (asn) asn.value = String(c.agent_script_agent_calls != null ? c.agent_script_agent_calls : 8);
  const awi = $('#agent-write-isolation');
  if (awi) awi.value = c.agent_write_isolation || 'serial';
  const awt = $('#agent-worktree-isolation');
  if (awt) awt.value = c.agent_worktree_isolation || 'auto';
  const anv = $('#agent-native-vision');
  if (anv) anv.checked = c.agent_native_vision !== false;
  const it = $('#image-test');
  if (it) {
    it.onclick = async () => {
      const out = $('#image-test-result');
      if (out) out.textContent = '检测中…';
      try {
        const r = await api('/api/image/status', {
          image_comfy_url: (($('#image-comfy-url') || {}).value || '').trim(),
          image_workflow: (($('#image-workflow') || {}).value || '').trim(),
        });
        if (!r.ok) throw new Error(r.error || '检测失败');
        const bits = [r.reachable ? 'ComfyUI 已连接' : '连不上 ComfyUI',
          r.workflow_exists ? '工作流已找到' : '工作流文件不存在'];
        if (out) {
          out.textContent = bits.join(' · ') + (r.error ? '（' + r.error + '）' : '');
          out.style.color = (r.reachable && r.workflow_exists) ? '' : 'var(--danger)';
        }
      } catch (e) {
        if (out) { out.textContent = e.message; out.style.color = 'var(--danger)'; }
      }
    };
  }
}

/* ============================================================
   设置页卡片：头部（标题 + 本节操作）/ 主体 / 折叠 / 页内锚点
   ============================================================ */

const SETTINGS_CARD_STATE_KEY = 'settings_card_collapsed_v1';
/* 专家向分区：第一次打开时默认收起（点锚点或标题即可展开），常用分区保持展开 */
const SETTINGS_CARD_ADVANCED = {
  chat: ['思考落库'],
  search: ['信息量上限', '研究类任务（复刻 / 还原 / 从零实现）'],
  agent: [
    '只读并行与预取', '工具结果预算', '并行探索（子代理）', '子代理 · 嵌套与后台',
    '研究闸门（复刻 / 还原类任务）', '工作流（多阶段编排）', '变更审阅（撤销快照）',
    '编辑器 · AI Tab 补全', '代码工具（pyright / Git）', '外部工具（MCP）',
    '生命周期钩子（Hooks）',
  ],
};

const SETTINGS_SECTION_ADV_KEY = 'settings_section_adv_v1';

/* 页内任务分区：把一页里的卡片按任务归组，避免「一长列同等权重的卡片」。
   未列出的 pane 保持原样；match 用卡片标题匹配。 */
const SETTINGS_PANE_SECTIONS = {
  chat: [
    { id: 'access', title: '模型接入', match: /聊天接口|视觉接口|使用方式|接入|预设|轻量任务的模型|账户余额/ },
    { id: 'behavior', title: '对话行为', match: /推理参数|断流重试|思考落库/ },
  ],
  context: [
    { id: 'context', title: '上下文与记忆', match: /.+/ },
  ],
  search: [
    { id: 'provider', title: '搜索服务商', match: /联网搜索|服务商|免注册引擎|接入|账户余额/ },
    { id: 'behavior', title: '检索行为', match: /检索行为|信息量上限|深度检索上限/ },
    { id: 'bili', title: 'B 站（可选）', match: /B 站|登录凭证|评论/ },
  ],
  agent: [
    { id: 'exec', title: '执行策略', match: /执行节奏|提问卡|通知|工具与写入/ },
    { id: 'approval', title: '审批与命令', match: /只读并行|工具结果预算|写入审批|命令执行与审计/ },
    { id: 'flow', title: '子代理与流程', match: /并行探索|子代理 · 嵌套|研究闸门|工作流|变更审阅/ },
    { id: 'ext', title: '编辑器与外部工具', match: /编辑器 · AI Tab|代码工具|外部工具（MCP）|生命周期钩子|电脑操作/ },
  ],
  appearance: [
    { id: 'look', title: '外观', match: /界面设计风格|氛围|动效/ },
    { id: 'editor', title: '编辑器与对话过程', match: /对话过程|空白字符|自动保存/ },
  ],
  profile: [
    { id: 'profile', title: '用户与学习', match: /.+/ },
  ],
  mobile: [
    { id: 'mobile', title: '移动访问', match: /.+/ },
  ],
  data: [
    { id: 'storage', title: '存储与维护', match: /应用信息|数据|会话存储|轨迹|代码库语义索引|右键菜单/ },
    { id: 'danger', title: '配置历史与重置', match: /配置历史|重置/, danger: true },
  ],
  image: [
    { id: 'image', title: '图片生成', match: /.+/ },
  ],
  skills: [
    { id: 'skills', title: 'Skill', match: /.+/ },
  ],
  agents: [
    { id: 'agents', title: '子代理团队与编排', match: /.+/ },
  ],
  flow: [
    { id: 'flow', title: '流程', match: /.+/ },
  ],
  ext: [
    { id: 'ext', title: '外部工具', match: /.+/ },
  ],
  speech: [
    { id: 'speech', title: '语音输入', match: /.+/ },
  ],
  keys: [
    { id: 'keys', title: '快捷键', match: /.+/ },
  ],
  remind: [
    { id: 'remind', title: '提醒与主动聊天', match: /.+/ },
  ],
};

function readSectionAdv() {
  try { return JSON.parse(localStorage.getItem(SETTINGS_SECTION_ADV_KEY) || '{}') || {}; }
  catch (e) { return {}; }
}

function writeSectionAdv(map) {
  try { localStorage.setItem(SETTINGS_SECTION_ADV_KEY, JSON.stringify(map)); } catch (e) { /* 忽略 */ }
}

function sectionAdvancedOn(sec) {
  const map = readSectionAdv();
  return !!map[sec.dataset.section || ''];
}

function isAdvancedCard(card) {
  const pane = card.closest('.pane');
  const name = pane ? pane.dataset.pane : '';
  const list = SETTINGS_CARD_ADVANCED[name] || [];
  // 只收「 curated 高级卡片」，不按 pane 默认级别隐藏：
  // 否则 context / agent 整页都会被判成高级，打开就是空的。
  return list.indexOf(card.dataset.card || '') >= 0;
}

function applySectionAdvanced() {
  $$('#modal .set-section').forEach((sec) => {
    const on = sectionAdvancedOn(sec);
    const cards = Array.from(sec.querySelectorAll('.set-card'));
    const advCards = cards.filter(isAdvancedCard);
    sec.classList.toggle('adv-open', on);
    cards.forEach((card) => {
      card.classList.toggle('adv-hidden', isAdvancedCard(card) && !on);
    });
    const btn = sec.querySelector('[data-act="toggle-section-adv"]');
    if (btn) {
      btn.classList.toggle('hidden', !advCards.length);
      btn.textContent = on ? '隐藏高级项' : ('显示高级项' + (advCards.length ? '（' + advCards.length + '）' : ''));
      btn.setAttribute('aria-expanded', on ? 'true' : 'false');
    }
  });
}

function setSectionAdvanced(sec, on) {
  if (!sec || !sec.dataset.section) return;
  const map = readSectionAdv();
  map[sec.dataset.section] = on ? 1 : 0;
  writeSectionAdv(map);
  applySectionAdvanced();
}

function toggleSectionAdvanced(sec) {
  if (!sec) return;
  setSectionAdvanced(sec, !sectionAdvancedOn(sec));
}

function groupPaneSections(pane) {
  const name = pane.dataset.pane;
  const rules = SETTINGS_PANE_SECTIONS[name];
  if (!rules || !rules.length) return;
  const cards = Array.from(pane.querySelectorAll(':scope > .set-card'));
  if (cards.length < 2) return;
  const buckets = rules.map((rule) => ({ rule: rule, cards: [] }));
  cards.forEach((card) => {
    const title = card.dataset.card || '';
    let hit = buckets.find((b) => b.rule.match.test(title));
    if (!hit) hit = buckets[buckets.length - 1];
    hit.cards.push(card);
  });
  const first = cards[0];
  const frag = document.createDocumentFragment();
  const built = [];
  buckets.forEach(({ rule, cards: list }) => {
    if (!list.length) return;
    const sec = document.createElement('section');
    sec.className = 'set-section' + (rule.danger ? ' set-section-danger' : '');
    sec.dataset.section = name + ':' + rule.id;
    sec.dataset.sectionTitle = rule.title;
    const head = document.createElement('div');
    head.className = 'set-section-head';
    head.innerHTML = '<h3 class="set-section-title">' + escapeHtml(rule.title) + '</h3>' +
      '<button type="button" class="set-section-adv" data-act="toggle-section-adv" aria-expanded="false"></button>';
    sec.appendChild(head);
    const body = document.createElement('div');
    body.className = 'set-section-body';
    sec.appendChild(body);
    frag.appendChild(sec);
    built.push({ sec: sec, cards: list });
  });
  // 先把分区壳插进 pane，再把卡片搬进去：否则 first 已经不是 pane 的子节点了。
  pane.insertBefore(frag, first);
  built.forEach(({ sec, cards: list }) => {
    const body = sec.querySelector('.set-section-body');
    list.forEach((card) => body.appendChild(card));
  });
  applySectionAdvanced();
}

function readCardCollapsed() {
  try { return JSON.parse(localStorage.getItem(SETTINGS_CARD_STATE_KEY) || '{}') || {}; }
  catch (e) { return {}; }
}

function writeCardCollapsed(map) {
  try { localStorage.setItem(SETTINGS_CARD_STATE_KEY, JSON.stringify(map)); } catch (e) { /* 忽略 */ }
}

function cardStateId(card) {
  const pane = card.closest('.pane');
  return (pane ? pane.dataset.pane : '') + '|' + (card.dataset.card || '');
}

function setCardCollapsed(card, on, persist, animate) {
  if (!card) return;
  const body = card.querySelector('.set-card-body');
  if (body && animate !== false) foldTransition(card, body, !on, { cls: 'collapsed' });
  else card.classList.toggle('collapsed', !!on);
  const btn = card.querySelector('[data-act="collapse-card"]');
  if (btn) {
    btn.setAttribute('aria-expanded', on ? 'false' : 'true');
    btn.title = on ? '展开本节' : '收起本节';
  }
  if (persist) {
    const map = readCardCollapsed();
    map[cardStateId(card)] = on ? 1 : 0;
    writeCardCollapsed(map);
  }
}

function syncCardCollapse(pane) {
  const saved = readCardCollapsed();
  const advanced = SETTINGS_CARD_ADVANCED[pane.dataset.pane] || [];
  pane.querySelectorAll('.set-card').forEach((card) => {
    const id = cardStateId(card);
    const on = id in saved ? !!saved[id] : advanced.indexOf(card.dataset.card) >= 0;
    setCardCollapsed(card, on, false, false);
  });
}

/* 一行设置：标签在左、控件在右；长文本类（url / 密钥 / 多行）保持标签在上 */
function layoutSettingRows(pane) {
  pane.querySelectorAll('.set-card-body').forEach((body) => {
    Array.from(body.children).forEach((el) => {
      if (!el.matches || !el.matches('label.field-label')) return;
      const next = el.nextElementSibling;
      if (!next || el.closest('.set-row')) return;
      let mode = '';
      if (next.tagName === 'SELECT') mode = 'short';
      else if (next.tagName === 'INPUT') {
        const t = (next.type || 'text').toLowerCase();
        if (t === 'number') mode = 'short';
        else if (t !== 'checkbox' && t !== 'radio' && t !== 'hidden') mode = 'wide';
      } else if (next.matches('.limit-row, .range-row')) mode = 'short';
      if (!mode) return;
      const row = document.createElement('div');
      row.className = 'set-row ' + mode;
      el.parentNode.insertBefore(row, el);
      row.appendChild(el);
      const ctl = document.createElement('div');
      ctl.className = 'set-row-ctl';
      row.appendChild(ctl);
      ctl.appendChild(next);
    });
  });
}

/* 分组卡片：每个 h4.set-group 与其后内容包成一张卡（静态面板只包一次） */
function decorateSettingsPanes() {
  allSettingsPanes().forEach((pane) => {
    if (pane.dataset.carded === '1') return;
    const nodes = Array.from(pane.children);
    let body = null;
    nodes.forEach((node) => {
      if (node.matches && node.matches('h4.set-group')) {
        const card = document.createElement('section');
        card.className = 'set-card';
        card.dataset.card = node.textContent.trim();
        pane.insertBefore(card, node);

        const head = document.createElement('div');
        head.className = 'set-card-head';
        card.appendChild(head);
        head.appendChild(node);

        const acts = document.createElement('div');
        acts.className = 'set-card-acts';
        acts.innerHTML =
          '<button type="button" class="set-card-act caret" data-act="collapse-card" ' +
          'aria-expanded="true" title="收起本节"><svg class="ic"><use href="#i-chevron"/></svg></button>';
        head.appendChild(acts);

        body = document.createElement('div');
        body.className = 'set-card-body';
        card.appendChild(body);
      } else if (body) {
        body.appendChild(node);
      }
    });
    pane.dataset.carded = '1';
    layoutSettingRows(pane);
    syncCardCollapse(pane);
    groupPaneSections(pane);
  });
}

/* ---- 页内锚点条：卡片多于一屏时给出分区跳转 ---- */
function settingsAnchorsEl() { return $('#settings-anchors'); }

function visiblePagePanes(page) {
  const want = settingsPagePanes(page);
  return want
    .map((name) => document.querySelector('#modal .pane[data-pane="' + name + '"]'))
    .filter((pane) => pane && !pane.classList.contains('hidden'));
}

/* 右侧目录的条目：优先按任务分区，没有分区的 pane 退回按卡片 */
function settingsAnchorItems() {
  const items = [];
  $$('#modal .pane:not(.hidden)').forEach((pane) => {
    const secs = Array.from(pane.querySelectorAll('.set-section'));
    if (secs.length) {
      secs.forEach((sec) => items.push({ el: sec, label: sec.dataset.sectionTitle || '' }));
    } else {
      Array.from(pane.querySelectorAll('.set-card')).forEach((card) => {
        items.push({ el: card, label: card.dataset.card || '' });
      });
    }
  });
  return items;
}

function renderSettingsAnchors(name) {
  const bar = settingsAnchorsEl();
  if (!bar) return;
  const toc = $('#settings-toc');
  const items = settingsAnchorItems();
  if (name === 'help' || items.length < 2) {
    bar.classList.add('hidden');
    bar.innerHTML = '';
    if (toc) toc.classList.add('hidden');
    return;
  }
  bar.classList.remove('hidden');
  if (toc) toc.classList.remove('hidden');
  bar.innerHTML = items.map((it, i) =>
    '<button type="button" class="set-anchor" data-i="' + i + '">' +
    escapeHtml(it.label) + '</button>').join('');
  bar.querySelectorAll('.set-anchor').forEach((b) => {
    b.onclick = () => {
      const it = items[Number(b.dataset.i)];
      if (!it || !it.el) return;
      if (it.el.classList.contains('set-card')) setCardCollapsed(it.el, false, true);
      it.el.scrollIntoView({ block: 'start', behavior: 'smooth' });
    };
  });
  updateAnchorsActive();
}

let _anchorRaf = 0;

function updateAnchorsActive() {
  const bar = settingsAnchorsEl();
  const content = $('#modal .settings-content');
  if (!bar || !content || bar.classList.contains('hidden')) return;
  const items = settingsAnchorItems();
  if (!items.length) return;
  const line = content.getBoundingClientRect().top + 72;
  let idx = 0;
  items.forEach((it, i) => { if (it.el.getBoundingClientRect().top <= line) idx = i; });
  bar.querySelectorAll('.set-anchor').forEach((b, i) => b.classList.toggle('active', i === idx));
}

function bindAnchorsScroll() {
  const content = $('#modal .settings-content');
  if (!content || content.dataset.anchorsBound === '1') return;
  content.dataset.anchorsBound = '1';
  content.addEventListener('scroll', () => {
    if (_anchorRaf) return;
    _anchorRaf = requestAnimationFrame(() => { _anchorRaf = 0; updateAnchorsActive(); });
  }, { passive: true });
}


/* 长说明默认折叠，首屏只留结论（有 id 的提示会被 JS 动态更新，不折叠） */
function foldLongHints() {
  allSettingsPanes().forEach((pane) => pane.querySelectorAll('p.hint').forEach((p) => {
    if (p.id || p.dataset.folded === '1' || p.closest('details')) return;
    if ((p.textContent || '').trim().length < 120) return;
    const det = document.createElement('details');
    det.className = 'hint-fold';
    const sum = document.createElement('summary');
    sum.textContent = '说明';
    const body = document.createElement('div');
    body.className = 'hint-fold-body';
    p.parentNode.insertBefore(det, p);
    det.appendChild(sum);
    det.appendChild(body);
    body.appendChild(p);
    p.dataset.folded = '1';
  }));
}
