/* 工作台 · templates
   由 work.js 按功能拆分；普通脚本共享全局作用域，加载顺序见 index.html。 */
'use strict';

async function workOpenTemplatesDialog() {
  let items = [];
  try {
    const r = await api('/api/work/templates', { op: 'list' });
    items = r.templates || [];
  } catch (e) {
    toastErr('读取模板失败', e);
    return;
  }
  workOpenDialog('任务模板', (body) => {
    if (!items.length) {
      body.appendChild(workElNew('div', 'work-hint', '还没有模板。把常用流程存成模板，下次一键建任务。'));
    }
    items.forEach((tpl) => {
      const row = workElNew('div', 'work-step');
      const main = workElNew('div', 'work-step-main');
      main.appendChild(workElNew('div', 'work-step-title', tpl.name));
      main.appendChild(workElNew('div', 'work-step-note',
        tpl.title + ' · ' + (tpl.steps || []).length + ' 步'
        + (tpl.tags ? ' · ' + tpl.tags : '')));
      row.appendChild(main);
      const use = workElNew('button', 'work-btn small primary', '使用');
      use.onclick = () => {
        workCloseDialog();
        workOpenTaskDialog(tpl);
      };
      row.appendChild(use);
      const del = workElNew('button', 'work-icon-btn', '✕');
      del.title = '删除模板';
      del.onclick = async () => {
        if (!window.confirm('删除模板「' + tpl.name + '」？')) return;
        try {
          await api('/api/work/templates', { op: 'delete', id: tpl.id });
          workCloseDialog();
          workOpenTemplatesDialog();
        } catch (e) { toastErr('删除模板失败', e); }
      };
      row.appendChild(del);
      body.appendChild(row);
    });
    const add = workElNew('button', 'work-btn small', '＋ 新建模板');
    add.onclick = () => { workCloseDialog(); workOpenTemplateEditor(); };
    body.appendChild(add);
  }, async () => true, { okText: '关闭', hideCancel: true });
}

function workOpenTemplateEditor() {
  workOpenDialog('新建模板', (body) => {
    const name = workField(body, '模板名称', workElNew('input', 'work-input'));
    name.placeholder = '例如：每周复盘';
    const title = workField(body, '任务标题', workElNew('input', 'work-input'));
    title.placeholder = '例如：写周报';
    const steps = workField(body, '步骤（一行一个）',
      workElNew('textarea', 'work-input work-textarea'));
    steps.rows = 6;
    steps.placeholder = '收集材料\n写初稿\n校对并发出';
    const tags = workField(body, '标签（可选）', workElNew('input', 'work-input'));
    const pri = workField(body, '优先级', workSelect(
      [['0', '普通'], ['1', '低'], ['2', '中'], ['3', '高']]));
    const est = workField(body, '预计分钟（可选）', workElNew('input', 'work-input'));
  }, async () => {
    const body = workEl('work-dialog-body');
    const inputs = body.querySelectorAll('input, textarea, select');
    const name = inputs[0].value.trim();
    const title = inputs[1].value.trim();
    const raw = inputs[2].value.split('\n').map((s) => s.trim()).filter(Boolean);
    if (!name || !title || !raw.length) {
      toast('模板名称、任务标题和步骤都要填', 'error');
      return false;
    }
    try {
      const r = await api('/api/work/templates', {
        op: 'save', name, title, steps: raw,
        tags: inputs[3].value.trim(),
        priority: Number(inputs[4].value || 0),
        estimate_min: Number(inputs[5].value || 0),
      });
      if (!r.ok) throw new Error(r.error || '保存失败');
      toast('模板已保存', 'success');
      return true;
    } catch (e) {
      toastErr('保存模板失败', e);
      return false;
    }
  });
}

/* ---------------- 绑定 ---------------- */

