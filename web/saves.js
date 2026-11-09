'use strict';

/* ============================================================
   会话存档 / 倒带
   每轮自动留档；可从旧档「回退」（覆盖当前会话）或「分支」（复制出新会话）。
   ============================================================ */

function openSaves() {
  showWithAnim($('#saves-modal'));
  renderSaves();
}

function closeSaves() {
  hideWithAnim($('#saves-modal'));
}

function savesTimeText(ts) {
  const d = new Date((ts || 0) * 1000);
  if (isNaN(d.getTime())) return '';
  const p = (n) => String(n).padStart(2, '0');
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) +
         ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
}

async function renderSaves() {
  const list = $('#saves-list');
  const meta = $('#saves-session');
  if (!list) return;
  const s = curSession();
  if (meta) meta.textContent = s ? ('会话：' + (s.title || '未命名')) : '';
  list.innerHTML = '<div class="saves-empty">读取中…</div>';
  let r = null;
  try { r = await api('/api/save/list', { session_id: s && s.id }); }
  catch (e) {
    list.innerHTML = '<div class="saves-empty">读取失败：' + escapeHtml(String(e)) + '</div>';
    return;
  }
  const items = (r && r.items) || [];
  list.innerHTML = '';
  if (!items.length) {
    list.innerHTML = '<div class="saves-empty">还没有存档点。每轮对话后会自动留档，' +
                     '也可以点「手动存档」按钮立刻留一个。</div>';
    return;
  }
  items.forEach((it) => {
    const row = document.createElement('div');
    row.className = 'saves-row' + (it.manual ? ' manual' : '');
    const main = document.createElement('div');
    main.className = 'saves-main';
    main.innerHTML =
      '<div class="saves-label">' + escapeHtml(it.label || '（无标题）') +
        (it.manual ? '<span class="saves-tag">手动</span>' : '') + '</div>' +
      '<div class="saves-sub">' + escapeHtml(savesTimeText(it.ts)) +
        ' · ' + (it.msg_count || 0) + ' 条消息</div>';
    const ops = document.createElement('div');
    ops.className = 'saves-ops';
    const mk = (html, title, cls) => {
      const b = document.createElement('button');
      b.className = 'gal-tbtn' + (cls ? ' ' + cls : '');
      b.title = title;
      b.innerHTML = html;
      return b;
    };
    const bRewind = mk(icon('reset') + ' 回退', '回到这个存档点（丢弃之后的进度）');
    const bBranch = mk('⑂ 分支', '以这个存档点为起点复制出新会话（原会话保留）');
    const bDel = mk(icon('trash'), '删除这个存档点', 'danger');
    bRewind.onclick = () => savesRestore(it.id, 'overwrite');
    bBranch.onclick = () => savesRestore(it.id, 'branch');
    bDel.onclick = () => savesDelete(it.id);
    ops.appendChild(bRewind);
    ops.appendChild(bBranch);
    ops.appendChild(bDel);
    row.appendChild(main);
    row.appendChild(ops);
    list.appendChild(row);
  });
}

async function savesMake() {
  const s = curSession();
  if (!s) return;
  const label = await appPrompt('给这个存档起个名字（可留空）', '');
  if (label === null) return;              // 取消
  try {
    const r = await api('/api/save/make', { session_id: s.id, label: label || '' });
    if (!r || !r.ok) throw new Error((r && r.error) || '失败');
    toast('已存档');
    renderSaves();
  } catch (e) { toast('存档失败：' + e); }
}

async function savesRestore(saveId, mode) {
  const s = curSession();
  if (!s) return;
  const ask = mode === 'branch'
    ? '以这个存档点为起点，复制出一条新的会话线？\n（当前会话原样保留）'
    : '回到这个存档点？\n之后的对话进度会被丢弃，且不可恢复。';
  if (!window.confirm(ask)) return;
  try {
    const r = await api('/api/save/restore', { session_id: s.id, save_id: saveId, mode: mode });
    if (!r || !r.ok) throw new Error((r && r.error) || '失败');
    applySessions(r, { keepCurrent: mode !== 'branch' });
    if (mode === 'branch') {
      closeSaves();
      toast('已分叉出新会话');
    } else {
      toast('已回退到该存档点');
      renderSaves();
    }
    if (state.uiMode === 'gal') galRenderFromSession();
    else renderMessages();
  } catch (e) { toast('操作失败：' + e); }
}

/* 从某条 AI 回复处创建分支：按消息位置精确切片，不依赖存档点 */
async function forkFromMessage(index) {
  const s = curSession();
  if (!s || typeof index !== 'number') return;
  if (!window.confirm('以这条回复为起点，复制出一条新的会话线？\n（当前会话原样保留）')) return;
  try {
    const r = await api('/api/save/branch', { session_id: s.id, upto: index + 1 });
    if (!r || !r.ok) throw new Error((r && r.error) || '失败');
    applySessions(r, { keepCurrent: false });   // 分叉：服务端把 current 指向新会话
    toast('已分叉出新会话');
    if (state.uiMode === 'gal') galRenderFromSession();
    else renderMessages();
  } catch (e) { toast('创建分支失败：' + e); }
}

async function savesDelete(saveId) {
  const s = curSession();
  if (!s) return;
  if (!window.confirm('删除这个存档点？\n（不影响当前对话）')) return;
  try {
    const r = await api('/api/save/delete', { session_id: s.id, save_id: saveId });
    if (!r || !r.ok) throw new Error('失败');
    toast('已删除');
    renderSaves();
  } catch (e) { toast('删除失败：' + e); }
}

function bindSavesEvents() {
  ['#btn-saves', '#gal-save-btn'].forEach((sel) => {
    const el = $(sel);
    if (el) el.onclick = () => openSaves();
  });
  if ($('#btn-close-saves')) $('#btn-close-saves').onclick = () => closeSaves();
  if ($('#saves-make')) $('#saves-make').onclick = savesMake;
  const modal = $('#saves-modal');
  if (modal) {
    modal.addEventListener('click', (e) => { if (e.target === modal) closeSaves(); });
  }
}
