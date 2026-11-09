/* 工作台 · backup
   由 work.js 按功能拆分；普通脚本共享全局作用域，加载顺序见 index.html。 */
'use strict';

function workBackupName() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, '0');
  return 'galgame-backup-' + d.getFullYear() + p(d.getMonth() + 1) + p(d.getDate())
    + '-' + p(d.getHours()) + p(d.getMinutes()) + p(d.getSeconds()) + '.zip';
}

function workBackupProgress(btn, bytes, total) {
  if (!btn) return;
  const mb = (bytes / 1048576).toFixed(1);
  btn.textContent = total
    ? ('导出中 ' + mb + ' / ' + (total / 1048576).toFixed(1) + ' MB')
    : ('导出中 ' + mb + ' MB');
}

function workFmtBytes(n) {
  const v = Number(n) || 0;
  if (v >= 1073741824) return (v / 1073741824).toFixed(2) + ' GB';
  return (v / 1048576).toFixed(1) + ' MB';
}

/* 把备份文件 POST 到后端。用 XHR 而不是 fetch：
   - 有 upload.onprogress，能显示真实上传百分比；
   - File 直接交给 send()，浏览器按块流式发送，不像 arrayBuffer() 那样
     在渲染进程里再占一整份内存（588MB 备份时这点很关键）。 */

function workBackupPost(url, file, extraHeaders, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', url, true);
    xhr.setRequestHeader('X-Auth-Token', TOKEN);
    xhr.setRequestHeader('Content-Type', 'application/zip');
    Object.keys(extraHeaders || {}).forEach((k) => xhr.setRequestHeader(k, extraHeaders[k]));
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && onProgress) onProgress(e.loaded, e.total);
    };
    xhr.onload = () => {
      let r = null;
      try { r = JSON.parse(xhr.responseText); } catch (e) { r = null; }
      if (xhr.status >= 200 && xhr.status < 300 && r) resolve(r);
      else reject(new Error((r && r.error) || ('上传失败 HTTP ' + xhr.status)));
    };
    xhr.onerror = () => reject(new Error('上传失败（连接中断）'));
    xhr.send(file);
  });
}

function workBackupUpload(file, onProgress) {
  return workBackupPost('/api/backup/preflight/job', file, null, onProgress)
    .then((r) => {
      if (!r.ok || !r.id) throw new Error(r.error || '上传失败');
      return r.id;
    });
}

/* 轮询后台哈希校验进度 */

async function workBackupPoll(id, onProgress) {
  for (let i = 0; i < 1200; i++) {          // 约 8 分钟上限
    await new Promise((r) => setTimeout(r, 400));
    const r = await api('/api/backup/preflight/status', { id });
    if (!r || !r.ok) throw new Error((r && r.error) || '预检任务丢失');
    if (r.state === 'done') return r.result || {};
    if (r.state === 'error') throw new Error(r.error || '预检失败');
    onProgress(r.pct || 0, r.done || 0, r.total || 0);
  }
  throw new Error('预检超时（文件过大或服务端异常）');
}

async function workBackupExport(btn) {
  const name = workBackupName();
  const label = btn ? btn.textContent : '';
  const reset = () => { if (btn) { btn.disabled = false; btn.textContent = label || '导出备份'; } };
  if (btn) { btn.disabled = true; btn.textContent = '正在打包…'; }

  // Electron 壳：主进程弹保存框并把响应流式写盘。
  // 不先把 500MB 的 zip 读进渲染进程，也不会出现「点了没反应」。
  const gw = window.gmWin;
  if (gw && typeof gw.saveUrl === 'function') {
    let off = null;
    try {
      if (typeof gw.onSaveProgress === 'function') {
        off = gw.onSaveProgress((p) => workBackupProgress(btn, p.bytes || 0, p.total || 0));
      }
      const r = await gw.saveUrl({
        url: location.origin + '/api/backup/export',
        name,
        method: 'POST',
        headers: { 'X-Auth-Token': TOKEN },
        title: '保存备份',
      });
      if (r && r.ok) toast('备份已保存：' + r.path, 'success');
      else if (r && r.canceled) toast('已取消导出');
      else throw new Error((r && r.error) || '导出失败');
    } catch (e) {
      toastErr('导出备份失败', e);
    } finally {
      if (off) off();
      reset();
    }
    return;
  }

  // 浏览器：先选保存位置再请求。showSaveFilePicker 需要用户手势，
  // 必须放在 await fetch 之前，否则会被当成「非用户触发」而失败。
  if (typeof window.showSaveFilePicker === 'function') {
    let handle = null;
    try {
      handle = await window.showSaveFilePicker({
        suggestedName: name,
        types: [{ description: 'ZIP 备份', accept: { 'application/zip': ['.zip'] } }],
      });
    } catch (e) {
      if (e && e.name === 'AbortError') { reset(); toast('已取消导出'); return; }
      handle = null;                     // 选择器不可用就退回普通下载
    }
    if (handle) {
      try {
        const res = await fetch('/api/backup/export', {
          method: 'POST', headers: { 'X-Auth-Token': TOKEN },
        });
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const total = Number(res.headers.get('Content-Length')) || 0;
        const writable = await handle.createWritable();
        const reader = res.body.getReader();
        let got = 0;
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          await writable.write(value);
          got += value.length;
          workBackupProgress(btn, got, total);
        }
        await writable.close();
        toast('备份已保存', 'success');
      } catch (e) {
        toastErr('导出备份失败', e);
      } finally {
        reset();
      }
      return;
    }
  }

  try {
    const res = await fetch('/api/backup/export', {
      method: 'POST', headers: { 'X-Auth-Token': TOKEN },
    });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    // 大文件还没下完就 revoke 可能中断下载，留足时间
    setTimeout(() => URL.revokeObjectURL(url), 60000);
    toast('已开始下载到浏览器默认下载目录', 'success');
  } catch (e) {
    toastErr('导出备份失败', e);
  } finally {
    reset();
  }
}

async function workBackupPreflight(file) {
  const info = workEl('backup-info');
  const restore = workEl('backup-restore');
  const setInfo = (t) => { if (info) info.textContent = t; };
  try {
    if (restore) restore.disabled = true;
    WORK.backupFile = null;
    setInfo('上传中 0%（' + workFmtBytes(file.size) + '）');
    const id = await workBackupUpload(file, (loaded, total) => {
      const pct = total ? Math.round(loaded * 100 / total) : 0;
      setInfo('上传中 ' + pct + '%（' + workFmtBytes(loaded) + ' / ' + workFmtBytes(total) + '）');
    });
    setInfo('上传完成，正在校验…');
    const result = await workBackupPoll(id, (pct, done, total) => {
      setInfo('校验中 ' + pct + '%（' + workFmtBytes(done) + ' / ' + workFmtBytes(total) + '）');
    });
    WORK.backupFile = file;
    const s = (result && result.summary) || {};
    setInfo('预检通过：会话 ' + (s.sessions || 0) +
      ' · 知识库 ' + (s.knowledge || 0) + ' · 笔记 ' + (s.notes || 0) +
      ' · 附件 ' + (s.uploads || 0) + ' · 存档 ' + (s.saves || 0) +
      ' · 任务 ' + (s.tasks || 0));
    if (restore) restore.disabled = false;
    toast('预检通过', 'success');
  } catch (e) {
    WORK.backupFile = null;
    if (restore) restore.disabled = true;
    setInfo('预检失败：' + e.message);
    toastErr('预检失败', e);
  }
}

async function workBackupRestore() {
  const file = WORK.backupFile;
  if (!file) { toast('请先选择并预检备份文件', 'error'); return; }
  if (!window.confirm('恢复会用备份覆盖知识库、会话、笔记、附件、GAL 存档和任务/日程。\n'
    + 'knowledge / notes / uploads / saves 目录会被备份整体替换（备份里没有的文件会删除）。\n'
    + '恢复前会自动生成一份安全备份。继续？')) return;
  const info = workEl('backup-info');
  const restoreBtn = workEl('backup-restore');
  const setInfo = (t) => { if (info) info.textContent = t; };
  try {
    if (restoreBtn) restoreBtn.disabled = true;
    setInfo('正在上传备份 0%（' + workFmtBytes(file.size) + '）');
    const r = await workBackupPost('/api/backup/restore', file,
      { 'X-Backup-Confirm': 'yes' },
      (loaded, total) => {
        const pct = total ? Math.round(loaded * 100 / total) : 0;
        setInfo('正在上传备份 ' + pct + '%（' + workFmtBytes(loaded) + ' / ' + workFmtBytes(total) + '）');
      });
    if (!r.ok) throw new Error(r.error || '恢复失败');
    setInfo('恢复完成，正在刷新…');
    toast('恢复完成，页面即将刷新', 'success');
    setTimeout(() => location.reload(), 600);
  } catch (e) {
    if (restoreBtn) restoreBtn.disabled = false;
    setInfo('恢复失败：' + e.message);
    toastErr('恢复失败', e);
  }
}

function workOpenBackupDialog() {
  WORK.backupFile = null;
  workOpenDialog('数据备份', (body) => {
    body.appendChild(workElNew('p', 'work-hint',
      '备份包含：知识库、会话、笔记、记忆索引、附件、GAL 存档、任务与日程。'
      + '不包含 API Key 和浏览器缓存。'));
    const exportBtn = workElNew('button', 'work-btn primary', '导出备份');
    exportBtn.type = 'button';
    exportBtn.onclick = () => workBackupExport(exportBtn);
    body.appendChild(exportBtn);
    body.appendChild(workElNew('div', 'work-hint', '选择备份文件（会先预检，不会立即写入）：'));
    const file = workElNew('input', 'work-input');
    file.type = 'file';
    file.accept = '.zip';
    file.onchange = () => {
      if (file.files && file.files[0]) workBackupPreflight(file.files[0]);
    };
    body.appendChild(file);
    const info = workElNew('div', 'work-hint', '尚未选择备份');
    info.id = 'backup-info';
    body.appendChild(info);
    const restore = workElNew('button', 'work-btn ghost', '确认恢复');
    restore.type = 'button';
    restore.id = 'backup-restore';
    restore.disabled = true;
    restore.onclick = workBackupRestore;
    body.appendChild(restore);
  }, async () => true, { okText: '关闭', hideCancel: true });
}

/* ---------------- 快速添加 / 视图切换 ---------------- */

