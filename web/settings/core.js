'use strict';

/* 设置弹窗 · core.js
   基础常量 / 工作副本 / 脏状态 / 动态面板排版片段
   原 web/settings.js 拆分而来：按 index.html 中的顺序加载，共享全局作用域。 */


/* ============================================================
   设置弹窗
   ============================================================ */
const PROVIDER_META = {
  chat: { title: '聊天模型', desc: '用于智能聊天与记忆读写', hint: '填写任意 OpenAI 兼容接口（地址 + 模型名 + 密钥）', protocol: false },
  vision: { title: '视觉模型', desc: '用于图像识别', hint: '支持 DashScope 原生协议，或 OpenAI 兼容的视觉接口', protocol: true },
};

// 上下文输入已迁到独立面板，这里保留元素引用供聊天预设读写
const chatCtx = { limitEl: null, reserveEl: null };

/* ---- 设置页：工作副本、脏状态、页签记忆与默认值 ---- */
let draftCfg = null;
let draftProfile = null;
let settingsSnapshot = '';
let settingsDirty = false;
let settingsDefaults = null;
let settingsCurrentTab = '';
let _dirtyTimer = null;

/* 列表型配置兼容：旧版本可能把数组存成了换行 / 逗号分隔的字符串 */
function settingsList(value) {
  if (Array.isArray(value)) {
    return value.map((x) => String(x).trim()).filter(Boolean);
  }
  return String(value || '').split(/[\s,;，；]+/).map((x) => x.trim()).filter(Boolean);
}

const SETTINGS_LAST_TAB_KEY = 'settings_last_tab';
const SETTINGS_SCROLL_KEY = 'settings_scroll_v1';

/* 设置页结构：一页 = 一个任务，可以包含多个 pane（pane 仍是配置分组的物理单元）。
   这样不必搬动字段，就能把相关配置合到同一页。 */
const SETTINGS_PAGES = [
  { id: 'model', group: '常用', title: '模型与对话',
    desc: '聊天 / 视觉接口、推理参数、上下文预算与压缩',
    panes: ['chat', 'context'] },
  { id: 'search', group: '常用', title: '联网与检索',
    desc: '搜索服务商、抓取策略与 B 站读取',
    panes: ['search'] },
  { id: 'tools', group: '能力', title: '能力与工具',
    desc: 'Agent 执行、子代理、技能与外部工具、图像',
    panes: ['agent', 'flow', 'agents', 'ext', 'skills', 'image'],
    subnav: [
      { id: 'exec', title: '执行策略', panes: ['agent'] },
      { id: 'flow', title: '子代理与流程', panes: ['flow', 'agents'] },
      { id: 'ext', title: '技能与外部工具', panes: ['ext', 'skills'] },
      { id: 'image', title: '图像', panes: ['image'] },
    ] },
  { id: 'ui', group: '偏好', title: '界面与提醒',
    desc: '主题外观、快捷键、语音与提醒',
    panes: ['appearance', 'speech', 'keys', 'remind'] },
  { id: 'data', group: '系统', title: '数据与用户',
    desc: '用户规则、移动访问、存储维护与重置',
    panes: ['profile', 'mobile', 'data'] },
  { id: 'help', group: '系统', title: '帮助',
    desc: '各项功能的实际作用与差异',
    panes: ['help'] },
];

/* 旧入口 / 深链 / localStorage 里的页 id → 新页 id */
const SETTINGS_TAB_ALIAS = {
  chat: 'model', context: 'model',
  search: 'search',
  agent: 'tools', flow: 'tools', ext: 'tools', agents: 'tools',
  skills: 'tools', image: 'tools',
  ui: 'ui', appearance: 'ui', speech: 'ui', keys: 'ui', remind: 'ui',
  profile: 'data', mobile: 'data', data: 'data', system: 'data',
  help: 'help',
};

const SETTINGS_PAGE_META = {};
SETTINGS_PAGES.forEach((p) => { SETTINGS_PAGE_META[p.id] = [p.title, p.desc]; });

function settingsNormalizeTab(name) {
  const raw = String(name || '');
  return SETTINGS_TAB_ALIAS[raw] || raw;
}

function settingsPageConf(page) {
  return SETTINGS_PAGES.find((p) => p.id === page) || null;
}

function settingsPagePanes(page) {
  const hit = settingsPageConf(page);
  if (hit) return hit.panes.slice();
  // 不在导航里的孤立面板仍然按单 pane 处理（可能在 detached 存储里）
  return settingsPaneEl(page) ? [page] : [];
}

function settingsPageOfPane(pane) {
  const hit = SETTINGS_PAGES.find((p) => p.panes.indexOf(pane) >= 0);
  return hit ? hit.id : pane;
}

function settingsSubOfPane(page, pane) {
  const conf = settingsPageConf(page);
  for (const sub of (conf && conf.subnav) || []) {
    if (sub.panes.indexOf(pane) >= 0) return sub;
  }
  return null;
}


/* 静态页的「恢复本页默认」作用范围；动态页由 _collect() 的键推导。
   密钥与自定义预设不在重置范围内。 */
const STATIC_RESET_KEYS = {
  profile: { profile: ['manual', 'rules', 'auto_learn_enabled', 'learning_prompt_custom'] },
  appearance: { config: ['theme', 'ide_whitespace', 'ide_autosave', 'ui_process_follow'] },
  speech: { config: ['speech_polish', 'speech_denoise'] },
  image: { config: ['image_enabled', 'image_comfy_url', 'image_workflow'] },
  mobile: { config: ['mobile_port', 'mobile_device_days'] },
  data: { config: ['trajectory_enabled', 'trajectory_max_field',
                   'codeindex_enabled', 'codeindex_max_files',
                   'codeindex_max_chunks', 'codeindex_max_file_kb'] },
  agents: { config: [
    'agent_auto_route', 'agent_auto_plan', 'agent_auto_review',
    'agent_plan_review',
    'agent_script_agents', 'agent_script_agent_calls', 'agent_write_isolation',
    'agent_worktree_isolation',
    'agent_native_vision',
  ] },
};

const SECRET_SETTING_KEY = /(^|_)(api_key|sessdata)$/;
const PRESET_SETTING_KEY = /(_presets|learning_presets)$/;
const RANGE_FIELDS = [];

function settingsDraft() { return draftCfg || state.config || {}; }

/* ---- 设置面板：动态面板共用的排版片段（统一类名与搜索索引 data-search）---- */
function mkPane(name) {
  const p = document.createElement('section');
  p.className = 'pane hidden';
  p.dataset.pane = name;
  return p;
}
const setGroupHtml = (title, search) =>
  '<h4 class="set-group"' + (search ? ' data-search="' + search + '"' : '') + '>' + title + '</h4>';
const labelHtml = (text, search) =>
  '<label class="field-label"' + (search ? ' data-search="' + search + '"' : '') + '>' + text + '</label>';
const hintHtml = (text, search) =>
  '<p class="hint"' + (search ? ' data-search="' + search + '"' : '') + '>' + text + '</p>';
const checkRowHtml = (inputId, text, search) =>
  '<label class="check-row"' + (search ? ' data-search="' + search + '"' : '') + '>' +
  '<input type="checkbox" id="' + inputId + '"> ' + text + '</label>';
const qIn = (pane, id) => pane.querySelector('#' + id);
