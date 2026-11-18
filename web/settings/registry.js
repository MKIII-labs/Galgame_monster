'use strict';

/* 设置弹窗 · registry.js
   把「分页 / 卡片」映射成可筛选的元数据：显示级别、标签、是否可预览。
   - 分页给默认级别与标签，卡片可按标题覆盖（危险项标 expert）；
   - 搜索、标签筛选、级别可见性都读这一份，新增设置项不需要改框架；
   - 级别是「可见性 / 编辑入口」，不是安全边界：只影响设置页显示。 */

const SETTINGS_LEVEL_ORDER = { basic: 1, advanced: 2, expert: 3 };
const SETTINGS_LEVEL_LABEL = { basic: '基础', advanced: '进阶', expert: '专家' };
const SETTINGS_LEVEL_DESC = {
  basic: '只显示高频、低风险项',
  advanced: '显示常用高级项',
  expert: '显示全部（含危险项）',
};

/* 分页级默认：未列出的分页按 advanced 处理 */
const SETTINGS_PANE_META = {
  chat: { level: 'basic', tags: ['model'] },
  search: { level: 'advanced', tags: ['net'] },
  context: { level: 'advanced', tags: ['context', 'model'] },
  agent: { level: 'advanced', tags: ['agent'] },
  remind: { level: 'advanced', tags: ['agent', 'data'] },
  agents: { level: 'advanced', tags: ['agent'] },
  skills: { level: 'advanced', tags: ['agent', 'memory'] },
  image: { level: 'advanced', tags: ['model', 'ui'] },
  profile: { level: 'basic', tags: ['memory'] },
  appearance: { level: 'basic', tags: ['ui'] },
  speech: { level: 'advanced', tags: ['voice'] },
  keys: { level: 'advanced', tags: ['ui'] },
  data: { level: 'advanced', tags: ['data'] },
  flow: { level: 'advanced', tags: ['agent'] },
  ext: { level: 'advanced', tags: ['agent'] },
  mobile: { level: 'advanced', tags: ['safety'] },
  help: { level: 'basic', tags: [] },
};

/* 卡片级覆盖：按分页 + 分区标题匹配；先命中先应用，后面的规则可继续叠加标签 */
const SETTINGS_CARD_RULES = [
  { pane: 'agent', match: /审批|写入|命令|沙箱|隔离|规则/, level: 'expert', tags: ['safety'] },
  { pane: 'agent', match: /轮次|并行|预算|续跑|重复|超时/, level: 'advanced' },
  { pane: 'chat', match: /重试|断流|推理|原生工具|上下文|压缩/, level: 'advanced' },
  { pane: 'chat', match: /接口|模型|密钥|预设/, level: 'basic' },
  { pane: 'data', match: /重置|清空|删除|恢复出厂/, level: 'expert', tags: ['safety'] },
  { pane: 'data', match: /配置历史|备份|迁移|会话存储/, level: 'advanced' },
  { pane: 'appearance', match: /./, level: 'basic' },
  { pane: 'help', match: /./, level: 'basic' },
];

function settingsCurrentLevel() {
  const raw = (draftCfg && draftCfg.settings_level)
    || (state.config && state.config.settings_level) || 'advanced';
  return SETTINGS_LEVEL_ORDER[raw] ? raw : 'advanced';
}

function settingsPaneMeta(name) {
  return SETTINGS_PANE_META[name] || { level: 'advanced', tags: [] };
}

function settingsCardMeta(card) {
  if (!card) return { level: 'advanced', tags: [] };
  const pane = card.closest ? card.closest('.pane') : null;
  const name = pane ? pane.dataset.pane : '';
  const base = settingsPaneMeta(name);
  let level = base.level || 'advanced';
  let tags = (base.tags || []).slice();
  const title = String(card.dataset.card || '');
  SETTINGS_CARD_RULES.forEach((rule) => {
    if (rule.pane && rule.pane !== name) return;
    if (rule.match && !rule.match.test(title)) return;
    if (rule.level) level = rule.level;
    if (rule.tags) tags = Array.from(new Set(tags.concat(rule.tags)));
  });
  return { level: level, tags: tags };
}

function settingsCardAllowed(card) {
  const meta = settingsCardMeta(card);
  return SETTINGS_LEVEL_ORDER[meta.level] <= SETTINGS_LEVEL_ORDER[settingsCurrentLevel()];
}

/* 按当前级别 / 标签打点；隐藏交给分区级「显示高级项」，不再全局按级别筛 */
function applySettingsMeta() {
  allSettingsPanes().forEach((pane) => {
    pane.querySelectorAll('.set-card').forEach((card) => {
      const meta = settingsCardMeta(card);
      card.dataset.level = meta.level;
      card.dataset.tags = meta.tags.join(' ');
      card.classList.remove('meta-hidden');
    });
  });
  if (typeof applySectionAdvanced === 'function') applySectionAdvanced();
  if (typeof buildSettingsSearchIndex === 'function') buildSettingsSearchIndex();
}

function setSettingsLevel(level) {
  if (!SETTINGS_LEVEL_ORDER[level]) return;
  if (draftCfg) draftCfg.settings_level = level;
  applySettingsMeta();
  if (typeof queueDirtyCheck === 'function') queueDirtyCheck();
  if (typeof renderSettingsAnchors === 'function') renderSettingsAnchors(settingsCurrentTab);
}

function syncSettingsLevelUI() {
  const level = settingsCurrentLevel();
  $$('#settings-level-bar .settings-level-btn').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.level === level);
  });
  const hint = $('#settings-level-hint');
  if (hint) hint.textContent = SETTINGS_LEVEL_DESC[level] || '';
}

function initSettingsRegistry() {
  const bar = $('#settings-level-bar');
  if (bar) {
    bar.addEventListener('click', (e) => {
      const btn = e.target.closest ? e.target.closest('.settings-level-btn') : null;
      if (btn) setSettingsLevel(btn.dataset.level);
    });
  }
  syncSettingsLevelUI();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initSettingsRegistry);
} else {
  initSettingsRegistry();
}
