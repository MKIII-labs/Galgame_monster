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

/* 设置页结构：一页 = 一个主题（pane 仍是配置分组的物理单元，可能被多页复用）。
   导航只保留一层：左侧按「常用 / Agent / 体验 / 系统」分组，每页内部再按任务分区。 */
const SETTINGS_PAGES = [
  { id: 'chat', group: '常用', title: '模型接入',
    desc: '聊天 / 视觉接口、预设与账户余额',
    panes: ['chat'] },
  { id: 'context', group: '常用', title: '上下文与记忆',
    desc: '上下文预算、省费模式、压缩与记忆检索',
    panes: ['context'] },
  { id: 'search', group: '常用', title: '联网与检索',
    desc: '搜索服务商、抓取策略与信息量上限',
    panes: ['search'] },
  { id: 'agent', group: 'Agent', title: '执行与审批',
    desc: '执行节奏、通知、写入审批与命令审计',
    panes: ['agent'] },
  { id: 'flow', group: 'Agent', title: '子代理与流程',
    desc: '子代理团队、自动派发、并行探索与工作流',
    panes: ['agents', 'flow'] },
  { id: 'ext', group: 'Agent', title: '工具与扩展',
    desc: '编辑器补全、代码工具、MCP / Hooks、电脑操作与 Skill',
    panes: ['ext', 'skills'] },
  { id: 'appearance', group: '体验', title: '外观与对话',
    desc: '主题、氛围、对话过程与编辑器',
    panes: ['appearance'] },
  { id: 'speech', group: '体验', title: '语音与提醒',
    desc: '语音输入、朗读与定时提醒',
    panes: ['speech', 'remind'] },
  { id: 'keys', group: '体验', title: '快捷键',
    desc: '查看与修改快捷键',
    panes: ['keys'] },
  { id: 'profile', group: '系统', title: '用户规则',
    desc: '全局规则、自动记录与学习指令',
    panes: ['profile'] },
  { id: 'mobile', group: '系统', title: '手机访问',
    desc: '局域网网关、扫码配对与设备管理',
    panes: ['mobile'] },
  { id: 'data', group: '系统', title: '系统维护',
    desc: '应用信息、会话存储、运行状态与重置',
    panes: ['data'] },
  { id: 'help', group: '系统', title: '帮助',
    desc: '各项功能的实际作用与差异',
    panes: ['help'] },
];

/* 旧入口 / 深链 / localStorage 里的页 id → 新页 id */
const SETTINGS_TAB_ALIAS = {
  model: 'chat',                  // 原「模型与对话」拆成 模型接入 / 上下文与记忆
  tools: 'agent',                 // 原「能力与工具」拆成 执行 / 子代理 / 工具
  ui: 'appearance',               // 原「界面与提醒」拆成 外观 / 语音 / 快捷键
  system: 'data',
  agents: 'flow', skills: 'ext', remind: 'speech',
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


/* 静态页的「恢复本页默认」作用范围；动态页由 _collect() 的键推导。
   密钥与自定义预设不在重置范围内。 */
const STATIC_RESET_KEYS = {
  profile: { profile: ['manual', 'rules', 'auto_learn_enabled', 'learning_prompt_custom'] },
  appearance: { config: ['theme', 'ide_whitespace', 'ide_autosave', 'ui_process_follow', 'avatar_motion'] },
  speech: { config: ['speech_polish', 'speech_denoise'] },
  mobile: { config: ['mobile_port', 'mobile_device_days'] },
  data: { config: ['trajectory_enabled', 'trajectory_max_field', 'storage_auto_prune',
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
