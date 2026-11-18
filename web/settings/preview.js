'use strict';

/* 设置弹窗 · preview.js
   保存前预览：外观类（主题）直接应用到界面，但不写 localStorage；
   其余设置只在底部摘要里说明「保存后生效」，避免误以为已经生效。 */

/* 真正能「立即看到」的键；其余键只出现在摘要里 */
const SETTINGS_PREVIEW_KEYS = /^theme$/;
const SETTINGS_KEY_LABELS = {
  theme: '界面主题',
  ui_process_follow: '过程智能跟随',
  ide_whitespace: '空白字符显示',
  ide_autosave: 'IDE 自动保存',
  settings_level: '设置显示级别',
  chat_url: '聊天接口',
  chat_model: '聊天模型',
  chat_think_mode: '思考模式',
  search_provider: '搜索服务商',
};

function settingsKeyLabel(key) {
  const k = String(key || '');
  if (SETTINGS_KEY_LABELS[k]) return SETTINGS_KEY_LABELS[k];
  if (k.indexOf('profile.') === 0) return '画像：' + k.slice(8).replace(/_/g, ' ');
  return k.replace(/_/g, ' ');
}

/* 与打开设置时的快照对比，列出所有变更键（config 用原键名，profile 加前缀） */
function settingsChangeKeys() {
  if (!settingsSnapshot) return [];
  let base;
  try { base = JSON.parse(settingsSnapshot); } catch (e) { return []; }
  const cur = collectSettings();
  const out = [];
  ['config', 'profile'].forEach((scope) => {
    const before = base[scope] || {};
    const after = cur[scope] || {};
    const keys = new Set(Object.keys(before).concat(Object.keys(after)));
    keys.forEach((key) => {
      if (JSON.stringify(before[key]) !== JSON.stringify(after[key])) {
        out.push(scope === 'profile' ? 'profile.' + key : key);
      }
    });
  });
  return out;
}

function syncSettingsPreviewButton() {
  const btn = $('#settings-preview');
  if (!btn) return;
  const has = settingsChangeKeys().some((key) => SETTINGS_PREVIEW_KEYS.test(key));
  btn.classList.toggle('hidden', !has);
}

function renderSettingsChangeSummary() {
  const box = $('#settings-change-summary');
  if (!box) return;
  const keys = settingsChangeKeys();
  if (!keys.length) {
    box.classList.add('hidden');
    box.textContent = '';
    syncSettingsPreviewButton();
    return;
  }
  const previewable = keys.filter((key) => SETTINGS_PREVIEW_KEYS.test(key));
  const sample = keys.slice(0, 3).map(settingsKeyLabel).join('、') +
    (keys.length > 3 ? '…' : '');
  const tail = previewable.length
    ? '（外观可预览，其余保存后生效）'
    : '（保存后生效）';
  box.textContent = '未保存 ' + keys.length + ' 项：' + sample + tail;
  box.classList.remove('hidden');
  syncSettingsPreviewButton();
}

function settingsPreviewAppearance() {
  const cfg = collectSettings().config;
  // persist:false —— 只改界面，不写 gm.theme；取消设置会回到已保存主题
  applyTheme(cfg.theme, { persist: false });
  document.body.classList.add('settings-previewing');
  toast('已预览外观（未保存）；取消会恢复原主题');
}

function settingsPreviewReset() {
  const wasPreviewing = document.body.classList.contains('settings-previewing');
  document.body.classList.remove('settings-previewing');
  if (!wasPreviewing) return;              // 没预览过就别重复应用主题（会闪一下）
  applyTheme((state.config && state.config.theme) || 'aurora');
}

function settingsClearPreview() {
  document.body.classList.remove('settings-previewing');
}

function initSettingsPreview() {
  const btn = $('#settings-preview');
  if (btn) btn.addEventListener('click', settingsPreviewAppearance);
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initSettingsPreview);
} else {
  initSettingsPreview();
}
