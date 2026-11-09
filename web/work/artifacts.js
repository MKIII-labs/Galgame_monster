/* 工作台 · artifacts
   由 work.js 按功能拆分；普通脚本共享全局作用域，加载顺序见 index.html。 */
'use strict';

function workSizeText(n) {
  const v = Number(n) || 0;
  if (v <= 0) return '';
  if (v < 1024) return v + ' B';
  if (v < 1024 * 1024) return (v / 1024).toFixed(1) + ' KB';
  return (v / 1024 / 1024).toFixed(1) + ' MB';
}

function workRenderArtifacts() {
  const box = workEl('work-artifact-body');
  if (!box) return;
  box.innerHTML = '';
  const badge = workEl('work-artifact-count');
  if (badge) badge.textContent = String(WORK.artifacts.length);
  if (!WORK.artifacts.length) {
    box.appendChild(workEmpty('还没有产物',
      WORK.showWorking ? '任务写出的文件会出现在这里' : '点右上角「交付物」可显示临时文件'));
    return;
  }
  // 按任务分组：同一个任务产出的文件放在一起，先看交付物再看是谁做的
  const groups = new Map();
  WORK.artifacts.forEach((a) => {
    const key = a.task_id || '';
    if (!groups.has(key)) {
      groups.set(key, {
        title: a.task_title || (a.task_id ? '任务 ' + a.task_id : '未关联任务'),
        items: [],
      });
    }
    groups.get(key).items.push(a);
  });
  groups.forEach((g) => {
    box.appendChild(workElNew('div', 'work-artifact-group', g.title));
    g.items.forEach((a) => box.appendChild(workArtifactRow(a)));
  });
}

function workArtifactRow(a) {
  const row = workElNew('div', 'work-artifact');
  row.title = a.path || a.name;
  row.tabIndex = 0;
  row.setAttribute('role', 'button');
  const main = workElNew('div', 'work-artifact-main');
  main.appendChild(workElNew('div', 'work-artifact-name', a.name));
  const sub = [];
  if (a.path) sub.push(a.path);
  const sz = workSizeText(a.size);
  if (sz) sub.push(sz);
  if (a.kind === 'working') sub.push('临时');
  if (sub.length) main.appendChild(workElNew('div', 'work-artifact-path', sub.join(' · ')));
  row.appendChild(main);

  const acts = workElNew('div', 'work-artifact-acts');
  const locate = workElNew('button', 'work-btn small ghost', '定位');
  locate.title = '打开所在文件夹';
  locate.onclick = async (ev) => {
    ev.stopPropagation();
    await workRevealArtifact(a.id);
  };
  const del = workElNew('button', 'work-icon-btn', '✕');
  del.title = '从产物栏移除（不会删除磁盘文件）';
  del.onclick = async (ev) => {
    ev.stopPropagation();
    await workDeleteArtifact(a.id, a.name);
  };
  acts.appendChild(locate);
  acts.appendChild(del);
  row.appendChild(acts);

  const open = async () => {
    try {
      await workPreviewArtifact(a.id);
    } catch (e) {
      toastErr('预览失败', e);
    }
  };
  row.onclick = open;
  row.onkeydown = (ev) => {
    if (ev.key !== 'Enter' && ev.key !== ' ') return;
    ev.preventDefault();
    open();
  };
  return row;
}

async function workRevealArtifact(id) {
  try {
    const r = await api('/api/work/artifacts', { op: 'reveal', id });
    if (!r.ok) throw new Error(r.error || '打开失败');
  } catch (e) {
    toastErr('打开所在文件夹失败', e);
  }
}

async function workDeleteArtifact(id, name) {
  if (!window.confirm('从产物栏移除「' + name + '」？\n只会删掉记录，磁盘上的文件不会动。')) return;
  try {
    const r = await api('/api/work/artifacts', { op: 'delete', id });
    if (!r.ok) throw new Error(r.error || '移除失败');
    await workLoad(true);
    toast('已从产物栏移除', 'success');
  } catch (e) {
    toastErr('移除产物失败', e);
  }
}

function workToggleWorking() {
  WORK.showWorking = !WORK.showWorking;
  const btn = workEl('work-artifact-toggle');
  if (btn) btn.textContent = WORK.showWorking ? '全部' : '交付物';
  workLoad(true);
}

/* ============================================================
   多视图：今天 / 周 / 月 / 看板 / 统计
   ============================================================ */

function workCsvTable(text) {
  const rows = String(text || '').split(/\r?\n/).filter((x) => x.trim() !== '').slice(0, 200);
  const wrap = workElNew('div', 'work-csv-wrap');
  const table = document.createElement('table');
  table.className = 'work-csv';
  rows.forEach((line, ri) => {
    const tr = document.createElement('tr');
    line.split(',').slice(0, 12).forEach((cell) => {
      const td = document.createElement(ri === 0 ? 'th' : 'td');
      td.textContent = cell.replace(/^"|"$/g, '');
      tr.appendChild(td);
    });
    table.appendChild(tr);
  });
  wrap.appendChild(table);
  return wrap;
}

/* ---------- 产物预览 ---------- */

async function workPreviewArtifact(id) {
  const r = await api('/api/work/artifacts', { op: 'preview', id });
  if (!r.ok) throw new Error(r.error || '无法预览');
  const p = r.preview || {};
  const name = ((r.artifact && r.artifact.name) || '').toLowerCase();
  workOpenDialog('预览：' + ((r.artifact && r.artifact.name) || '产物'), (body) => {
    if (p.kind === 'image') {
      const img = document.createElement('img');
      img.className = 'work-preview-img';
      img.src = p.data_url;
      img.alt = (r.artifact && r.artifact.name) || '产物';
      body.appendChild(img);
    } else if (p.kind === 'text') {
      if (/\.(md|markdown)$/.test(name) && typeof safeRenderMarkdown === 'function') {
        const md = workElNew('div', 'work-preview-md');
        md.innerHTML = safeRenderMarkdown(p.text || '');
        body.appendChild(md);
      } else if (/\.(csv|tsv)$/.test(name)) {
        body.appendChild(workCsvTable(p.text || ''));
      } else {
        const pre = document.createElement('pre');
        pre.className = 'work-preview-text';
        pre.textContent = p.text || '';
        body.appendChild(pre);
      }
      if (p.truncated) {
        body.appendChild(workElNew('div', 'work-hint', '文件较大，只显示前 256KB。'));
      }
    } else {
      body.appendChild(workElNew('div', 'work-hint',
        '该类型暂不支持预览（' + Math.round((p.size || 0) / 1024) + ' KB）。'));
    }
    const reveal = workElNew('div', 'work-detail-actions');
    const locate = workElNew('button', 'work-btn small ghost', '打开所在文件夹');
    locate.onclick = () => workRevealArtifact(id);
    reveal.appendChild(locate);
    body.appendChild(reveal);
  }, async () => true, { okText: '关闭', hideCancel: true });
}

/* ---------- 工作流 ---------- */

