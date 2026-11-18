'use strict';

/* 设置弹窗 · draft.js
   未保存修改的本地草稿：自动写 localStorage，重新打开设置时提示恢复 / 丢弃。
   草稿只存「工作副本」（config + profile），不含密钥的额外副本——它们本来就在 config 里。 */

const SETTINGS_DRAFT_KEY = 'settings_draft_v2';
let _settingsDraftTimer = null;

function settingsDraftTime(ts) {
  const d = new Date(Number(ts) || Date.now());
  const p = (x) => String(x).padStart(2, '0');
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) +
    ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
}

function readSettingsDraft() {
  try {
    const data = JSON.parse(localStorage.getItem(SETTINGS_DRAFT_KEY) || 'null');
    if (!data || typeof data !== 'object' || !data.config) return null;
    return data;
  } catch (e) {
    return null;
  }
}

function clearSettingsDraft(refreshBar) {
  try { localStorage.removeItem(SETTINGS_DRAFT_KEY); } catch (e) { /* 忽略 */ }
  if (refreshBar !== false) updateSettingsDraftBar();
}

function saveSettingsDraftNow() {
  if (!settingsSnapshot) return;          // 设置还没打开 / 还没建快照
  if (!settingsChangedNow()) {
    clearSettingsDraft(false);
    return;
  }
  try {
    const cur = collectSettings();
    localStorage.setItem(SETTINGS_DRAFT_KEY, JSON.stringify({
      t: Date.now(),
      tab: settingsCurrentTab,
      config: cur.config,
      profile: cur.profile,
    }));
  } catch (e) {
    // localStorage 满 / 隐私模式：草稿是增强功能，失败不影响设置本身
  }
  updateSettingsDraftBar();
}

function scheduleSettingsDraft() {
  clearTimeout(_settingsDraftTimer);
  _settingsDraftTimer = setTimeout(saveSettingsDraftNow, 600);
}

function updateSettingsDraftBar() {
  const bar = $('#settings-draft-bar');
  if (!bar) return;
  const data = readSettingsDraft();
  if (!data) {
    bar.classList.add('hidden');
    return;
  }
  const txt = $('#settings-draft-text');
  if (txt) txt.textContent = '发现未保存的草稿（' + settingsDraftTime(data.t) + '）';
  bar.classList.remove('hidden');
}

function restoreSettingsDraft() {
  const data = readSettingsDraft();
  if (!data) { updateSettingsDraftBar(); return; }
  draftCfg = Object.assign({}, draftCfg || {}, data.config || {});
  draftProfile = Object.assign({}, draftProfile || {}, data.profile || {});
  if (typeof rebuildSettingsFromDraft === 'function') rebuildSettingsFromDraft();
  if (data.tab) setSettingsTab(data.tab);
  settingsDirty = true;
  if (typeof updateSettingsDirty === 'function') updateSettingsDirty();
  if (typeof applySettingsMeta === 'function') applySettingsMeta();
  updateSettingsDraftBar();
  toast('已恢复草稿；点「保存」后写入配置');
}

function discardSettingsDraft() {
  clearSettingsDraft();
  toast('已丢弃草稿');
}

function initSettingsDraft() {
  const restore = $('#settings-draft-restore');
  if (restore) restore.addEventListener('click', restoreSettingsDraft);
  const discard = $('#settings-draft-discard');
  if (discard) discard.addEventListener('click', discardSettingsDraft);
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initSettingsDraft);
} else {
  initSettingsDraft();
}
