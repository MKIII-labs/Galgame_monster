'use strict';

/* 设置弹窗 · dirty.js
   脏状态对比与按分区恢复默认
   原 web/settings.js 拆分而来：按 index.html 中的顺序加载，共享全局作用域。 */

/* ---- 脏状态：每次改动都与打开时的快照对比 ---- */
function countSettingDiff(a, b) {
  if (a === b) return 0;
  if (Array.isArray(a) || Array.isArray(b)) {
    return JSON.stringify(a) === JSON.stringify(b) ? 0 : 1;
  }
  if (a && b && typeof a === 'object' && typeof b === 'object') {
    const keys = new Set(Object.keys(a).concat(Object.keys(b)));
    let n = 0;
    keys.forEach((k) => { n += countSettingDiff(a[k], b[k]); });
    return n;
  }
  return 1;
}

function updateSettingsDirty() {
  if (!settingsSnapshot) return;
  const cur = JSON.stringify(collectSettings());
  settingsDirty = cur !== settingsSnapshot;
  let n = 0;
  try { n = countSettingDiff(JSON.parse(settingsSnapshot), JSON.parse(cur)); }
  catch (e) { n = settingsDirty ? 1 : 0; }
  const save = $('#btn-save');
  if (save) {
    save.disabled = !settingsDirty;
    save.textContent = settingsDirty && n ? ('保存（' + n + '）') : '保存';
  }
  const tip = $('#settings-dirty');
  if (tip) {
    tip.classList.toggle('hidden', !settingsDirty);
    tip.textContent = settingsDirty ? '未保存的改动' : '';
  }
  // 底部摘要：列出变更项，并提示哪些能预览、哪些保存后生效
  if (typeof renderSettingsChangeSummary === 'function') renderSettingsChangeSummary();
  if (typeof syncSettingsPreviewButton === 'function') syncSettingsPreviewButton();
}

function queueDirtyCheck() {
  clearTimeout(_dirtyTimer);
  _dirtyTimer = setTimeout(updateSettingsDirty, 80);
  // 草稿防抖写盘：关掉设置 / 刷新页面后还能恢复
  if (typeof scheduleSettingsDraft === 'function') scheduleSettingsDraft();
}

/* 同步判断（不依赖 80ms 的防抖），供保存 / 关闭前使用 */
function settingsChangedNow() {
  if (!settingsSnapshot) return false;
  try { return JSON.stringify(collectSettings()) !== settingsSnapshot; }
  catch (e) { return true; }
}

/* 把界面上所有设置收集成 {config, profile}（保存与脏状态共用） */
function collectSettings() {
  const cfg = Object.assign({}, draftCfg || state.config || {});
  allSettingsPanes().forEach((pane) => {
    if (pane._collect) Object.assign(cfg, pane._collect());
  });

  const tr = settingsQuery('input[name="theme"]:checked');
  if (tr) cfg.theme = tr.value;
  if ($('#ide-whitespace')) cfg.ide_whitespace = $('#ide-whitespace').value;
  if ($('#ide-autosave')) cfg.ide_autosave = $('#ide-autosave').value || 'off';
  if ($('#ui-process-follow')) cfg.ui_process_follow = !!$('#ui-process-follow').checked;

  cfg.speech_polish = !!$('#speech-polish').checked;

  cfg.image_enabled = !!($('#image-enabled') || {}).checked;
  if ($('#image-comfy-url')) {
    cfg.image_comfy_url = $('#image-comfy-url').value.trim() || 'http://127.0.0.1:8188';
  }
  if ($('#image-workflow')) cfg.image_workflow = $('#image-workflow').value.trim();

  cfg.trajectory_enabled = !!($('#traj-enabled') || {}).checked;
  if ($('#mobile-port')) {
    const mnp = parseInt($('#mobile-port').value, 10);
    cfg.mobile_port = Number.isFinite(mnp) && mnp >= 1024 && mnp <= 65535 ? mnp : 57890;
  }
  if ($('#mobile-device-days')) {
    const mnd = parseInt($('#mobile-device-days').value, 10);
    cfg.mobile_device_days = Number.isFinite(mnd) && mnd >= 1 ? Math.min(3650, mnd) : 180;
  }
  if ($('#traj-max-field')) {
    const tn = parseInt($('#traj-max-field').value, 10);
    cfg.trajectory_max_field = Number.isFinite(tn) && tn >= 0 ? tn : 200000;
  }
  if ($('#codeindex-enabled')) cfg.codeindex_enabled = !!$('#codeindex-enabled').checked;
  if ($('#codeindex-max-files')) {
    const n = parseInt($('#codeindex-max-files').value, 10);
    cfg.codeindex_max_files = Number.isFinite(n) && n >= 0 ? n : 0;
  }
  if ($('#codeindex-max-chunks')) {
    const n = parseInt($('#codeindex-max-chunks').value, 10);
    cfg.codeindex_max_chunks = Number.isFinite(n) && n >= 0 ? n : 0;
  }
  if ($('#codeindex-max-file-kb')) {
    const n = parseInt($('#codeindex-max-file-kb').value, 10);
    cfg.codeindex_max_file_kb = Number.isFinite(n) && n >= 16 ? n : 512;
  }

  // 自动派发（子代理页）
  cfg.agent_auto_route = !!($('#agent-auto-route') || {}).checked;
  cfg.agent_auto_plan = !!($('#agent-auto-plan') || {}).checked;
  cfg.agent_auto_review = !!($('#agent-auto-review') || {}).checked;
  if ($('#agent-plan-review')) {
    cfg.agent_plan_review = !!$('#agent-plan-review').checked;
  }
  cfg.agent_script_agents = !!($('#agent-script-agents') || {}).checked;
  if ($('#agent-script-agent-calls')) {
    const n = parseInt($('#agent-script-agent-calls').value, 10);
    cfg.agent_script_agent_calls = Number.isFinite(n) && n >= 0 ? n : 8;
  }
  if ($('#agent-write-isolation')) {
    const v = $('#agent-write-isolation').value;
    cfg.agent_write_isolation = ['off', 'serial', 'strict'].includes(v) ? v : 'serial';
  }
  if ($('#agent-worktree-isolation')) {
    const v = $('#agent-worktree-isolation').value;
    cfg.agent_worktree_isolation = ['off', 'auto', 'always'].includes(v) ? v : 'auto';
  }
  if ($('#agent-native-vision')) {
    cfg.agent_native_vision = !!$('#agent-native-vision').checked;
  }

  const profile = {
    // 旧字段 manual 在载入时已折算进 rules；保存后统一以 rules 为准
    rules: collectProfileRules(),
    manual: '',
    auto_learn_enabled: $('#profile-auto-learn').checked,
    learning_prompt_custom: $('#learning-prompt').value,
  };
  return { config: cfg, profile: profile };
}

/* ---- 恢复默认 ---- */
async function ensureSettingsDefaults() {
  if (settingsDefaults) return settingsDefaults;
  const r = await api('/api/settings/defaults');
  if (!r || !r.ok) throw new Error((r && r.error) || '无法读取默认值');
  settingsDefaults = { config: r.config || {}, profile: r.profile || {} };
  return settingsDefaults;
}

function pageResetSpec(name) {
  const config = [];
  const profile = [];
  settingsPagePanes(name).forEach((paneName) => {
    const pane = settingsPaneEl(paneName);
    if (pane && typeof pane._collect === 'function') {
      Object.keys(pane._collect()).forEach((k) => {
        if (SECRET_SETTING_KEY.test(k) || PRESET_SETTING_KEY.test(k)) return;
        if (config.indexOf(k) < 0) config.push(k);
      });
      return;
    }
    const spec = STATIC_RESET_KEYS[paneName];
    if (!spec) return;
    (spec.config || []).forEach((k) => { if (config.indexOf(k) < 0) config.push(k); });
    (spec.profile || []).forEach((k) => { if (profile.indexOf(k) < 0) profile.push(k); });
  });
  return (config.length || profile.length) ? { config, profile } : null;
}

function rebuildSettingsFromDraft() {
  buildDynamicPanes();
  fillStaticSettings(draftCfg, draftProfile);
  decorateSettingsPanes();
  foldLongHints();
  addRangeCompanions();
  buildSettingsSearchIndex();
  if (typeof applySettingsMeta === 'function') applySettingsMeta();
  setSettingsTab(settingsCurrentTab || 'model');
  updateSettingsDirty();
}

async function resetCurrentSettingsPage() {
  const name = settingsCurrentTab;
  const spec = pageResetSpec(name);
  if (!spec) { toast('本页没有可恢复的默认项'); return; }
  if (!window.confirm('把「' + settingsPaneName(name) + '」整页恢复为默认值？\n' +
      'API 密钥、B 站 SESSDATA 与自定义预设会保留；恢复后仍需点「保存」生效。')) return;
  let defs;
  try { defs = await ensureSettingsDefaults(); }
  catch (e) { toastErr('读取默认值', e); return; }
  const cur = collectSettings();
  spec.config.forEach((k) => { if (k in defs.config) cur.config[k] = deepCopy(defs.config[k]); });
  spec.profile.forEach((k) => { if (k in defs.profile) cur.profile[k] = deepCopy(defs.profile[k]); });
  draftCfg = cur.config;
  draftProfile = cur.profile;
  rebuildSettingsFromDraft();
  toast('已恢复本页默认值，点「保存」生效');
}

/* ---- 按分区恢复默认 ----
   卡片归属的配置键不靠手写映射表：临时扰动卡片里的每个控件，比较收集结果，找出它真正
   影响的键；随后把控件恢复原状，并校验整页收集结果与扰动前完全一致——校验不过就认为
   这次映射不可信，宁可不提供本节重置，也不误改别处的配置。 */
const CARD_RESET_SKIP = /(^|\.)(rules|manual)$|api_key|sessdata|_presets$/;

function flattenSettingPaths(obj, prefix, out) {
  out = out || {};
  Object.keys(obj || {}).forEach((k) => {
    const v = obj[k];
    const p = prefix ? prefix + '.' + k : k;
    if (v && typeof v === 'object' && !Array.isArray(v)) flattenSettingPaths(v, p, out);
    else out[p] = JSON.stringify(v);
  });
  return out;
}

function settingsOracle(pane) {
  if (typeof pane._collect === 'function') return () => ({ config: pane._collect() });
  return () => collectSettings();
}

function radioGroupOf(el) {
  if (!el.name) return [el];
  const out = [];
  allSettingsPanes().forEach((pane) => {
    pane.querySelectorAll('input[type="radio"][name="' + el.name + '"]').forEach((x) => out.push(x));
  });
  return out;
}

function perturbControl(el) {
  if (el.tagName === 'SELECT') {
    if (el.options.length < 2) return false;
    el.selectedIndex = (el.selectedIndex + 1) % el.options.length;
    return true;
  }
  const t = (el.type || 'text').toLowerCase();
  if (t === 'checkbox') { el.checked = !el.checked; return true; }
  if (t === 'radio') {
    const other = radioGroupOf(el).find((x) => x !== el && !x.checked);
    if (!other) return false;
    other.checked = true;
    return true;
  }
  if (t === 'number') {
    const n = parseFloat(el.value);
    el.value = String(Number.isFinite(n) ? n + 1 : 1);
    return true;
  }
  if (el.tagName === 'TEXTAREA' || ['text', 'search', 'url', 'password', 'email'].indexOf(t) >= 0) {
    el.value = 'zz' + el.value;
    return true;
  }
  return false;
}

function settingsFingerprint(flat) {
  return Object.keys(flat).sort().map((k) => k + '=' + flat[k]).join('\n');
}

function cardKeyMap(pane) {
  if (pane._cardKeyMap) return pane._cardKeyMap;
  const oracle = settingsOracle(pane);
  let base;
  try { base = flattenSettingPaths(oracle()); } catch (e) { return null; }
  const basePrint = settingsFingerprint(base);
  const controls = Array.from(pane.querySelectorAll('input, select, textarea'))
    .filter((el) => el.closest('.set-card') && el.type !== 'range' && !el.disabled);
  const map = new Map();
  controls.forEach((el) => {
    const radios = el.type === 'radio' ? radioGroupOf(el) : null;
    const saved = radios ? radios.map((x) => x.checked) : { value: el.value, checked: el.checked };
    let changed = null;
    try {
      if (!perturbControl(el)) return;
      const now = flattenSettingPaths(oracle());
      changed = Object.keys(now).filter((p) => now[p] !== base[p]);
    } catch (e) {
      changed = null;
    } finally {
      if (radios) radios.forEach((x, i) => { x.checked = saved[i]; });
      else { el.value = saved.value; el.checked = saved.checked; }
    }
    if (changed && changed.length) map.set(el, changed);
  });
  // 校验：全部控件恢复后，收集结果必须与扰动前完全一致，否则这次映射不可信
  try {
    if (settingsFingerprint(flattenSettingPaths(oracle())) !== basePrint) return null;
  } catch (e) { return null; }
  pane._cardKeyMap = map;
  return map;
}

function cardResetPaths(card) {
  const pane = card.closest('.pane');
  if (!pane) return [];
  const out = new Set();
  card.querySelectorAll('[data-cfg]').forEach((el) => {
    String(el.dataset.cfg || '').split(',').forEach((k) => {
      const t = k.trim();
      if (t) out.add('config.' + t);
    });
  });
  const map = cardKeyMap(pane);
  if (map) {
    map.forEach((paths, el) => {
      if (!card.contains(el)) return;
      paths.forEach((p) => out.add(p));
    });
  }
  return Array.from(out).filter((p) => !CARD_RESET_SKIP.test(p));
}

function findCardByTitle(pane, title) {
  if (!pane) return null;
  return Array.from(pane.querySelectorAll('.set-card'))
    .find((c) => (c.dataset.card || '') === title) || null;
}

async function resetSettingsCard(card) {
  const title = card.dataset.card || '本节';
  const paths = cardResetPaths(card);
  if (!paths.length) { toast('「' + title + '」没有可单独恢复的默认项'); return; }
  let defs;
  try { defs = await ensureSettingsDefaults(); }
  catch (e) { toastErr('读取默认值', e); return; }
  const cur = collectSettings();
  let hit = 0;
  paths.forEach((p) => {
    const i = p.indexOf('.');
    const group = p.slice(0, i);
    const key = p.slice(i + 1);
    const src = group === 'profile' ? defs.profile : defs.config;
    const dst = group === 'profile' ? cur.profile : cur.config;
    if (!src || !dst || !(key in src)) return;
    dst[key] = deepCopy(src[key]);
    hit++;
  });
  if (!hit) { toast('「' + title + '」没有可单独恢复的默认项'); return; }
  draftCfg = cur.config;
  draftProfile = cur.profile;
  rebuildSettingsFromDraft();
  requestAnimationFrame(() => {
    const pane = settingsPagePanes(settingsCurrentTab).map(settingsPaneEl).find(Boolean);
    const again = findCardByTitle(pane, title);
    if (!again) return;
    setCardCollapsed(again, false, true);
    updateAnchorsActive();
  });
  toast('已恢复「' + title + '」默认值，点「保存」生效');
}

async function resetAllSettings() {
  if (!window.confirm('把设置页里的可调项恢复为默认值？\nAPI 密钥、B 站 SESSDATA 与自定义预设会保留。恢复后仍需点「保存」生效。')) return;
  let defs;
  try { defs = await ensureSettingsDefaults(); }
  catch (e) { toastErr('读取默认值', e); return; }
  const cur = collectSettings();
  const keys = new Set();
  settingsTabs().forEach((t) => {
    const spec = pageResetSpec(t);
    if (spec) spec.config.forEach((k) => keys.add(k));
  });
  keys.forEach((k) => { if (k in defs.config) cur.config[k] = deepCopy(defs.config[k]); });
  Object.keys(defs.profile || {}).forEach((k) => { cur.profile[k] = deepCopy(defs.profile[k]); });
  draftCfg = cur.config;
  draftProfile = cur.profile;
  rebuildSettingsFromDraft();
  toast('已恢复全部默认值，点「保存」生效');
}
