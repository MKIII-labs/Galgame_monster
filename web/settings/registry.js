'use strict';

/* 设置弹窗 · registry.js
   把「分页 / 卡片」映射成级别元数据：设置页用它决定哪些卡片默认收起。
   - 分页给默认级别，卡片可按标题覆盖（危险项标 expert）；
   - 级别只影响设置页的默认折叠，不是安全边界。 */

const SETTINGS_PANE_META = {
  chat: 'basic',
  search: 'advanced',
  context: 'advanced',
  agent: 'advanced',
  remind: 'advanced',
  agents: 'advanced',
  skills: 'advanced',
  profile: 'basic',
  appearance: 'basic',
  speech: 'advanced',
  keys: 'advanced',
  data: 'advanced',
  flow: 'advanced',
  ext: 'advanced',
  mobile: 'advanced',
  help: 'basic',
};

/* 卡片级覆盖：按分页 + 卡片标题匹配；先命中先应用 */
const SETTINGS_CARD_RULES = [
  { pane: 'agent', match: /审批|写入|命令|沙箱|隔离|规则/, level: 'expert' },
  { pane: 'agent', match: /轮次|并行|预算|续跑|重复|超时/, level: 'advanced' },
  { pane: 'chat', match: /重试|断流|推理|原生工具|上下文|压缩/, level: 'advanced' },
  { pane: 'chat', match: /接口|模型|密钥|预设/, level: 'basic' },
  { pane: 'data', match: /重置|清空|删除|恢复出厂/, level: 'expert' },
  { pane: 'data', match: /配置历史|备份|迁移|会话存储/, level: 'advanced' },
  { pane: 'appearance', match: /./, level: 'basic' },
  { pane: 'help', match: /./, level: 'basic' },
];

function settingsPaneMeta(name) {
  return { level: SETTINGS_PANE_META[name] || 'advanced' };
}

function settingsCardMeta(card) {
  if (!card) return { level: 'advanced' };
  const pane = card.closest ? card.closest('.pane') : null;
  const name = pane ? pane.dataset.pane : '';
  let level = settingsPaneMeta(name).level;
  const title = String(card.dataset.card || '');
  SETTINGS_CARD_RULES.forEach((rule) => {
    if (rule.pane && rule.pane !== name) return;
    if (rule.match && !rule.match.test(title)) return;
    if (rule.level) level = rule.level;
  });
  return { level: level };
}

/* 给卡片打上级别（供默认折叠与调试查看），然后重建搜索索引 */
function applySettingsMeta() {
  allSettingsPanes().forEach((pane) => {
    pane.querySelectorAll('.set-card').forEach((card) => {
      card.dataset.level = settingsCardMeta(card).level;
    });
  });
  if (typeof buildSettingsSearchIndex === 'function') buildSettingsSearchIndex();
}
