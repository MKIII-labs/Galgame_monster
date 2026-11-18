'use strict';

/* 设置弹窗 · history.js
   配置历史：列出最近的保存前快照，查看差异、回滚到任意版本。
   快照由后端在每次保存前生成（最近 50 个，密钥已脱敏）；回滚时密钥保持当前值。 */

let _settingsHistory = [];
let _settingsHistoryDiffId = '';

function settingsHistoryTime(ts) {
  const d = new Date((Number(ts) || 0) * 1000);
  if (!Number(ts)) return '—';
  const p = (x) => String(x).padStart(2, '0');
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) +
    ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
}

function settingsHistoryValue(value) {
  if (value === undefined) return '（无）';
  if (value === null) return 'null';
  if (typeof value === 'string') {
    return value.length > 80 ? value.slice(0, 80) + '…' : value;
  }
  if (typeof value === 'object') {
    let text = '';
    try { text = JSON.stringify(value); } catch (e) { text = String(value); }
    return text.length > 80 ? text.slice(0, 80) + '…' : text;
  }
  return String(value);
}

function renderSettingsHistory() {
  const box = $('#settings-history-list');
  if (!box) return;
  if (!_settingsHistory.length) {
    box.innerHTML = '<p class="hint">还没有历史版本。保存过一次配置后就会出现。</p>';
    return;
  }
  box.innerHTML = _settingsHistory.map((it) => {
    const keys = (it.changed || []).slice(0, 3).join('、');
    const more = (it.changed || []).length > 3 ? '…' : '';
    return '<div class="settings-history-row" data-id="' + escapeHtml(it.id) + '">' +
      '<div class="settings-history-main">' +
        '<div class="settings-history-time">' + escapeHtml(settingsHistoryTime(it.ts)) + '</div>' +
        '<div class="settings-history-keys">' +
          ((it.changed || []).length
            ? ('保存前：' + escapeHtml(keys) + more)
            : '（无键名记录）') +
        '</div>' +
      '</div>' +
      '<div class="settings-history-acts">' +
        '<button type="button" class="link-btn" data-act="diff">差异</button>' +
        '<button type="button" class="link-btn danger" data-act="rollback">还原</button>' +
      '</div></div>';
  }).join('');
}

async function loadSettingsHistory() {
  const box = $('#settings-history-list');
  if (!box) return;
  box.textContent = '加载中…';
  try {
    const r = await api('/api/settings', { op: 'history' });
    _settingsHistory = r.items || [];
    renderSettingsHistory();
  } catch (e) {
    box.innerHTML = '<p class="hint">读取失败：' + escapeHtml(e.message) + '</p>';
  }
}

function hideSettingsHistoryDiff() {
  _settingsHistoryDiffId = '';
  const box = $('#settings-history-diff');
  if (box) {
    box.classList.add('hidden');
    box.innerHTML = '';
  }
}

async function showSettingsHistoryDiff(id) {
  const box = $('#settings-history-diff');
  if (!box) return;
  if (_settingsHistoryDiffId === id) { hideSettingsHistoryDiff(); return; }
  try {
    const r = await api('/api/settings', { op: 'diff', id: id });
    const rows = (r.diff && r.diff.changed) || [];
    _settingsHistoryDiffId = id;
    box.innerHTML = '<div class="settings-history-diff-head">与当前配置的差异（' +
      escapeHtml(settingsHistoryTime(r.diff && r.diff.ts)) + '）</div>' +
      (rows.length ? rows.map((row) =>
        '<div class="settings-history-diff-row">' +
          '<code>' + escapeHtml(row.key) + '</code>' +
          '<span class="shd-before">' + escapeHtml(settingsHistoryValue(row.before)) + '</span>' +
          '<span class="shd-arrow">→</span>' +
          '<span class="shd-after">' + escapeHtml(settingsHistoryValue(row.after)) + '</span>' +
        '</div>').join('') : '<p class="hint">没有可显示的差异。</p>');
    box.classList.remove('hidden');
  } catch (e) {
    toastErr('读取差异', e);
  }
}

async function rollbackSettingsHistory(id) {
  if (!id) return;
  let msg = '回滚到这个历史版本？\n当前配置会先留一份快照，API 密钥不会被覆盖。';
  if (typeof settingsChangedNow === 'function' && settingsChangedNow()) {
    msg += '\n当前未保存的改动会丢失。';
  }
  if (!window.confirm(msg)) return;
  try {
    const r = await api('/api/settings', { op: 'rollback', id: id });
    state.config = r.config || state.config;
    draftCfg = deepCopy(state.config);
    draftProfile = deepCopy(state.profile || {});
    settingsSnapshot = JSON.stringify(collectSettings());
    if (typeof hideSettingsHistoryDiff === 'function') hideSettingsHistoryDiff();
    if (typeof rebuildSettingsFromDraft === 'function') rebuildSettingsFromDraft();
    settingsSnapshot = JSON.stringify(collectSettings());
    settingsDirty = false;
    updateSettingsDirty();
    applyTheme(state.config.theme);
    if (typeof ideApplyWhitespace === 'function') { try { ideApplyWhitespace(); } catch (e) { /* 忽略 */ } }
    if (typeof loadSettingsHistory === 'function') loadSettingsHistory();
    toast('已回滚到所选版本（密钥保持当前值）');
  } catch (e) {
    toastErr('回滚', e);
  }
}

function initSettingsHistory() {
  const btn = $('#settings-history-refresh');
  if (btn) btn.addEventListener('click', loadSettingsHistory);
  const list = $('#settings-history-list');
  if (list) {
    list.addEventListener('click', (e) => {
      const row = e.target.closest ? e.target.closest('.settings-history-row') : null;
      if (!row) return;
      const act = e.target.closest ? e.target.closest('[data-act]') : null;
      if (!act) return;
      if (act.dataset.act === 'diff') showSettingsHistoryDiff(row.dataset.id);
      else if (act.dataset.act === 'rollback') rollbackSettingsHistory(row.dataset.id);
    });
  }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initSettingsHistory);
} else {
  initSettingsHistory();
}
