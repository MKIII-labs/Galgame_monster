'use strict';

/* 设置弹窗 · data.js
   数据与高级：版本 / 数据目录 / 备份
   原 web/settings.js 拆分而来：按 index.html 中的顺序加载，共享全局作用域。 */

/* ---- 数据与高级 ---- */
async function loadAppInfo() {
  const el = $('#appinfo-body');
  if (!el) return;
  el.textContent = '加载中…';
  try {
    const r = await api('/api/app/info');
    if (!r || !r.ok) throw new Error((r && r.error) || '读取失败');
    el.innerHTML =
      '<div class="appinfo-row"><span>版本</span><b>' + escapeHtml(r.version || '') + '</b></div>' +
      '<div class="appinfo-row"><span>数据目录</span><code>' + escapeHtml(r.data_dir || '') + '</code></div>' +
      '<div class="appinfo-row"><span>Python</span><code>' + escapeHtml(r.python || '') + '</code></div>' +
      '<div class="appinfo-row"><span>系统</span><code>' + escapeHtml(r.platform || '') + '</code></div>';
  } catch (e) {
    el.textContent = '读取失败：' + (e.message || e);
  }
}

/* 运行状态：/api/health 的只读视图（存储体积 / 孤儿目录 / 内存缓存 / 分段耗时） */
function fmtBytes(n) {
  n = Number(n) || 0;
  if (n >= 1073741824) return (n / 1073741824).toFixed(2) + ' GB';
  if (n >= 1048576) return (n / 1048576).toFixed(1) + ' MB';
  if (n >= 1024) return (n / 1024).toFixed(0) + ' KB';
  return n + ' B';
}

async function loadHealth() {
  const box = $('#health-body');
  if (!box) return;
  box.textContent = '读取中…';
  const row = (label, val, warn) => '<div class="health-row' + (warn ? ' warn' : '') + '">'
    + '<span>' + escapeHtml(label) + '</span><b>' + escapeHtml(String(val)) + '</b></div>';
  const msRow = (label, key, timing) => {
    const t = timing[key] || {};
    if (!t.n) return '<div class="health-sub">' + escapeHtml(label) + '：暂无数据</div>';
    return '<div class="health-sub">' + escapeHtml(label) + '：p50 ' + t.p50
      + ' ms · p90 ' + t.p90 + ' ms · p99 ' + t.p99 + ' ms（n=' + t.n + '）</div>';
  };
  try {
    const r = await api('/api/health', null, { method: 'GET' });
    if (!r || !r.ok) throw new Error((r && r.error) || '读取失败');
    const st = r.storage || {};
    const rt = r.runtime || {};
    const tm = r.timing || {};
    let html = '';
    if (st.error) {
      html += row('存储指标', '读取失败：' + st.error, true);
    } else {
      html += row('会话数', st.sessions || 0);
      html += row('事件流体积', fmtBytes(st.event_bytes));
      html += row('最大单会话事件', fmtBytes(st.largest_event_bytes), !!st.event_warn);
      html += row('物化缓存体积', fmtBytes(st.cache_bytes));
      html += row('孤儿事件目录', st.orphan_dirs || 0, Number(st.orphan_dirs) > 0);
      html += row('已删除会话墓碑', st.tombstones || 0);
    }
    html += row('活跃流', rt.active_streams || 0);
    html += row('SSE 任务', rt.sse_jobs || 0);
    html += row('事件内存缓存', rt.events_last || 0);
    html += row('JSON 缓存', rt.json_cache || 0);
    html += msRow('最近 200 轮 · 装配', 'assemble_ms', tm);
    html += msRow('最近 200 轮 · 首 token', 'first_token_ms', tm);
    html += msRow('最近 200 轮 · 总耗时', 'total_ms', tm);
    if (st.event_warn) {
      html += '<p class="hint">有会话的事件日志超过 64MB：事件流只追加、不自动删除，'
        + '建议定期运行 scripts/prune_session_events.py 归档。</p>';
    }
    if (Number(st.orphan_dirs) > 0) {
      html += '<p class="hint">孤儿目录是索引里已不存在的会话事件目录；'
        + '用 scripts/prune_session_events.py 校验后移入隔离区，不要直接删除。</p>';
    }
    box.innerHTML = html;
    const at = $('#health-at');
    if (at) at.textContent = '更新于 ' + new Date().toLocaleTimeString();
  } catch (e) {
    box.textContent = '读取失败：' + (e.message || e);
  }
}


async function openDataDir() {
  try {
    const r = await api('/api/app/open-data-dir');
    if (!r || !r.ok) throw new Error((r && r.error) || '打开失败');
  } catch (e) {
    toastErr('打开数据目录', e);
  }
}

/* 代码库语义索引：状态 / 重建（后台增量，轮询到就绪） */
function codeindexStateText(r) {
  r = r || {};
  const st = r.state || 'idle';
  if (st === 'running') {
    return '索引中：' + (r.done || 0) + '/' + (r.total || '?') + ' 文件 · '
      + (r.chunks || 0) + ' 块';
  }
  if (st === 'error') return '索引失败：' + (r.error || '未知错误');
  if (st === 'ready') {
    let t = '就绪：' + (r.files || 0) + ' 文件 / ' + (r.chunks || 0) + ' 块';
    if (r.model_ready === false) t += '（语义模型未下载，当前用关键词兜底）';
    return t;
  }
  return '尚未建立索引';
}

async function codeindexRefresh() {
  const box = $('#codeindex-status');
  if (!box) return;
  const sid = (state.sessions && state.sessions.current) || '';
  if (!sid) { box.textContent = '先打开一个会话'; return; }
  try {
    const r = await api('/api/codeindex/status', { session_id: sid });
    box.textContent = codeindexStateText(r);
    if (r && r.state === 'running') setTimeout(codeindexRefresh, 1200);
  } catch (e) {
    box.textContent = '读取索引状态失败：' + e;
  }
}

async function codeindexRebuild() {
  const sid = (state.sessions && state.sessions.current) || '';
  if (!sid) return toast('先打开一个会话');
  const btn = $('#codeindex-rebuild');
  if (btn) btn.disabled = true;
  try {
    await api('/api/codeindex/reindex', { session_id: sid });
    toast('已开始重建索引');
    codeindexRefresh();
  } catch (e) {
    toastErr('重建索引', e);
  } finally {
    if (btn) btn.disabled = false;
  }
}

/* 资源管理器右键菜单：按钮是开关，注册状态存在 dataset.installed 上 */
async function toggleContextMenu() {
  const btn = $('#app-context-menu');
  const installed = !!(btn && btn.dataset.installed === '1');
  if (btn) btn.disabled = true;
  try {
    const r = await api('/api/app/context-menu', { action: installed ? 'remove' : 'install' });
    const st = (r && r.status) || {};
    if (btn) {
      btn.dataset.installed = st.installed ? '1' : '0';
      btn.textContent = st.installed ? '移除右键菜单' : '注册右键菜单';
    }
    if (!r || !r.ok) throw new Error((r && (r.message || r.error)) || '操作失败');
    toast(r.message || (st.installed ? '已注册右键菜单' : '已移除右键菜单'), 'success');
  } catch (e) {
    toastErr('资源管理器右键菜单', e);
  } finally {
    if (btn) btn.disabled = false;
  }
}

function closeModal(force) {
  if (!force && settingsChangedNow()) {
    if (!window.confirm('有未保存的改动，关闭后将丢弃。确定关闭？')) return false;
    // 主题卡是即时预览：取消关闭时回到已保存的主题，避免「取消了但界面已经变了」
    const saved = normalizeTheme((state.config && state.config.theme) || 'light');
    applyTheme(saved);
  }
  settingsDirty = false;
  if (typeof clearSettingsDraft === 'function') clearSettingsDraft();
  if (typeof settingsPreviewReset === 'function') settingsPreviewReset();
  saveSettingsScroll(settingsCurrentTab);
  hideWithAnim($('#modal'));
  return true;
}

async function saveModal() {
  if (!settingsChangedNow()) { toast('没有需要保存的改动'); return; }
  settingsDirty = true;
  // 保存是最高频操作之一：先给出「进行中」反馈，失败也要说清楚（原实现没有 try/catch，失败时界面毫无反应）
  const saveBtn = $('#btn-save');
  const saveTxt = saveBtn ? saveBtn.textContent : '';
  const restoreSaveBtn = () => { if (saveBtn) { saveBtn.disabled = false; saveBtn.textContent = saveTxt; } };
  if (saveBtn) { saveBtn.disabled = true; saveBtn.textContent = '保存中…'; }
  const collected = collectSettings();
  const cfg = collected.config;
  const profile = collected.profile;

  try {
    await api('/api/config', cfg);
    await api('/api/profile', profile);
  } catch (e) {
    restoreSaveBtn();
    toastErr('保存', e);          // 失败 → 不改内存里的 config，保证界面与磁盘一致
    return;
  }
  restoreSaveBtn();
  state.config = cfg;
  state.profile = profile;
  draftCfg = deepCopy(cfg);
  draftProfile = deepCopy(profile);
  settingsSnapshot = JSON.stringify(collectSettings());
  if (typeof clearSettingsDraft === 'function') clearSettingsDraft();
  if (typeof settingsClearPreview === 'function') settingsClearPreview();
  // 密钥被清空时，顺带关掉「联网搜索」开关
  if (state.searchEnabled && !hasSearchKey()) {
    state.searchEnabled = false;
    api('/api/config', { search_enabled: false }).then(() => { state.config.search_enabled = false; });
  }
  syncSearchPill();
  applyTheme(cfg.theme);
  if (typeof avaMotionSync === 'function') avaMotionSync();
  ideApplyWhitespace();
  updateStatusLine();
  updateSettingsDirty();
  closeModal(true);
  toast('已保存');
}
