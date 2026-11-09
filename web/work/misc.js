/* 工作台 · misc
   由 work.js 按功能拆分；普通脚本共享全局作用域，加载顺序见 index.html。 */
'use strict';

function workOpenImageDialog() {
  let size = 'square';
  let promptEl = null;
  workOpenDialog('生成图片', (body) => {
    promptEl = workField(body, '提示词',
      workElNew('textarea', 'work-input work-textarea'));
    promptEl.rows = 5;
    promptEl.placeholder = '描述你想要的画面，例如：雨夜的霓虹街道，电影感，35mm';
    const chips = workElNew('div', 'work-dt-chips');
    workFieldBox(body, '尺寸', chips);
    const opts = [
      ['square', '方形 1024²'],
      ['portrait', '竖图 832×1216'],
      ['landscape', '横图 1216×832'],
    ];
    const btns = opts.map(([v, label]) => {
      const b = workElNew('button', 'work-dt-chip' + (v === size ? ' on' : ''), label);
      b.type = 'button';
      b.onclick = () => {
        size = v;
        btns.forEach(([btn, val]) => btn.classList.toggle('on', val === size));
      };
      chips.appendChild(b);
      return [b, v];
    });
    body.appendChild(workElNew('div', 'work-hint',
      '本地 FLUX 3 Image Klein（16 步）。首次生成要加载模型，可能多等十几秒；'
      + '结果会放进右侧「产物」栏，点开可预览。'));
  }, async () => {
    const prompt = promptEl.value.trim();
    if (!prompt) { toast('请填写提示词', 'error'); return false; }
    const okBtn = workEl('work-dialog-ok');
    const label = okBtn ? okBtn.textContent : '生成';
    if (okBtn) { okBtn.disabled = true; okBtn.textContent = '生成中…'; }
    try {
      const r = await api('/api/image/generate', { prompt, size });
      if (!r.ok) throw new Error(r.error || '生成失败');
      await workLoad(true);
      toast('图片已生成，见「产物」栏', 'success');
      const art = r.artifact;
      workCloseDialog();
      if (art && art.id) workPreviewArtifact(art.id).catch(() => {});
      return false;                       // 弹窗已在上面关掉
    } catch (e) {
      toastErr('生成图片失败', e);
      if (okBtn) { okBtn.disabled = false; okBtn.textContent = label || '生成'; }
      return false;
    }
  }, { okText: '生成' });
}

function workSceneName(id) {
  const hit = (WORK.scenes || []).find((s) => s.id === id);
  return hit ? hit.name : (id || '通用');
}

async function workLoadScene() {
  const sid = (state.sessions && state.sessions.current) || '';
  try {
    const r = await api('/api/scene', { op: 'get', session_id: sid });
    if (!r.ok) return;
    WORK.scene = r.scene || 'general';
    WORK.scenes = r.scenes || [];
    const sel = workEl('work-scene');
    if (!sel) return;
    sel.innerHTML = '';
    WORK.scenes.forEach((s) => {
      const o = document.createElement('option');
      o.value = s.id;
      o.textContent = '场景：' + s.name;
      o.title = s.desc || '';
      sel.appendChild(o);
    });
    sel.value = WORK.scene;
  } catch (e) { /* 场景不是关键路径，失败不影响工作台 */ }
}

async function workSetScene(scene) {
  const sid = (state.sessions && state.sessions.current) || '';
  try {
    const r = await api('/api/scene', { op: 'set', session_id: sid, scene });
    if (!r.ok) throw new Error(r.error || '切换失败');
    WORK.scene = r.scene;
    toast('场景已切换：' + workSceneName(r.scene), 'success');
  } catch (e) {
    toastErr('切换场景失败', e);
  }
}

async function workOpenMemory(query, includeMirror) {
  const q = typeof query === 'string' ? query : '';
  const withMirror = !!includeMirror;
  let r;
  try {
    const payload = { op: 'search', query: q, limit: 30 };
    if (!withMirror) payload.sources = ['manual', 'task'];
    r = await api('/api/memory', payload);
  } catch (e) {
    toastErr('加载记忆失败', e);
    return;
  }
  if (!r.ok) { toast(r.error || '加载失败', 'error'); return; }
  const stats = r.stats || {};

  workOpenDialog('记忆', (body) => {
    const by = Object.entries(stats.by_source || {})
      .map(([k, v]) => k + ' ' + v).join(' / ');
    body.appendChild(workElNew('div', 'work-hint',
      '共 ' + (stats.total || 0) + ' 条' + (by ? ' · ' + by : '')));
    body.appendChild(workElNew('div', 'work-hint',
      '这里只做全局搜索；编辑画像 / 知识库 / 笔记请走侧栏对应入口。'));

    const mirror = document.createElement('input');
    mirror.type = 'checkbox';
    mirror.checked = withMirror;
    const mLabel = workElNew('label', 'work-check');
    mLabel.appendChild(mirror);
    mLabel.appendChild(document.createTextNode(' 包含知识库 / 画像 / 笔记（只读）'));
    mirror.onchange = () => workOpenMemory(q, mirror.checked);
    body.appendChild(mLabel);

    const search = workElNew('div', 'work-memory-search');
    const input = workElNew('input', 'work-input');
    input.placeholder = '搜索记忆…';
    input.value = q;
    const btn = workElNew('button', 'work-btn', '搜索');
    btn.onclick = () => workOpenMemory(input.value, withMirror);
    search.appendChild(input);
    search.appendChild(btn);
    body.appendChild(search);

    if (!(r.items || []).length) {
      body.appendChild(workEmpty('没有匹配的记忆',
        withMirror ? '换个关键词，或点「重建索引」' : '在下面添加一条，或勾选上方查看知识库 / 笔记'));
    }
    (r.items || []).forEach((it) => {
      const isMirror = it.source === 'kb' || it.source === 'session'
        || it.source === 'note';
      const row = workElNew('div', 'work-memory-item');
      const main = workElNew('div', 'work-memory-main');
      const title = it.title || '';
      let bodyText = it.text || '';
      if (title && bodyText.startsWith(title)) {
        bodyText = bodyText.slice(title.length).trim();
      }
      if (title) main.appendChild(workElNew('div', 'work-memory-title', title));
      if (bodyText) main.appendChild(workElNew('div', 'work-memory-text', bodyText));
      main.appendChild(workElNew('div', 'work-memory-meta',
        (it.type || 'fact') + ' · '
        + (it.source_name || (isMirror ? '知识库镜像 · 只读' : (it.source || 'manual')))
        + ' · 重要度 ' + Number(it.importance || 0).toFixed(1)));
      row.appendChild(main);
      if (isMirror) {
        const open = workElNew('button', 'work-btn small', '打开来源');
        open.title = it.source === 'note' ? '在笔记库中打开' : '在知识库中定位这条记忆';
        open.onclick = () => {
          workCloseDialog();
          if (it.source === 'note') {
            if (typeof notesOpen === 'function') notesOpen(it.note_path || it.source_id);
            return;
          }
          if (typeof kbOpenNode === 'function') {
            kbOpenNode(it.source === 'session' ? 'session' : it.kb_id,
                       it.source_id || '');
          }
        };
        row.appendChild(open);
      } else {
        const del = workElNew('button', 'work-icon-btn', '✕');
        del.title = '删除这条记忆';
        del.onclick = async () => {
          try {
            await api('/api/memory', { op: 'delete', id: it.id });
            workOpenMemory(input.value, withMirror);
          } catch (e) { toastErr('删除失败', e); }
        };
        row.appendChild(del);
      }
      body.appendChild(row);
    });

    body.appendChild(workElNew('div', 'work-field-label', '添加记忆'));
    const ta = workElNew('textarea', 'work-input work-textarea');
    ta.rows = 3;
    ta.placeholder = '例如：我对花生过敏 / 我习惯用简洁的回答';
    body.appendChild(ta);
    const typeSel = workSelect([['fact', '事实'], ['preference', '偏好'],
      ['event', '事件'], ['note', '笔记']]);
    body.appendChild(typeSel);
    const kbs = (r.kbs || []).filter((k) => k.name !== '用户画像');
    const targetSel = workSelect([
      ['auto', '自动（按类型）'],
      ['session', '本会话记忆'],
      ['profile', '用户画像'],
    ].concat(kbs.map((k) => ['kb:' + k.id, '知识库：' + k.name]))
      .concat([['global', '仅全局快速记忆']]));
    body.appendChild(targetSel);
    body.appendChild(workElNew('div', 'work-hint',
      '自动：偏好 → 用户画像；事实/事件 → 本会话；笔记 → 当前知识库（未选则本会话）。'));
    const add = workElNew('button', 'work-btn primary', '记住');
    add.onclick = async () => {
      if (!ta.value.trim()) { toast('写点什么再记住', 'error'); return; }
      const target = targetSel.value;
      const kbId = target.startsWith('kb:') ? target.slice(3) : '';
      const payload = {
        text: ta.value, type: typeSel.value, target, kb_id: kbId,
        session_id: (state.sessions && state.sessions.current) || '',
        importance: 0.7,
      };
      try {
        const sim = await api('/api/memory', Object.assign({ op: 'similar' }, payload));
        const hits = (sim && sim.items) || [];
        if (hits.length) {
          const names = hits.map((h) => '「' + (h.title || '') + '」('
            + (h.source_name || '') + ')').join('、');
          if (!window.confirm('已有相似条目：' + names + '\n仍然添加？')) return;
        }
        const rr = await api('/api/memory', Object.assign({ op: 'add' }, payload));
        if (!rr.ok) throw new Error(rr.error || '添加失败');
        toast('已写入：' + (rr.source_name || '记忆'), 'success');
        const graph = rr.target && rr.target !== 'global';
        workOpenMemory('', graph ? true : withMirror);
      } catch (e) { toastErr('添加失败', e); }
    };
    body.appendChild(add);

    const re = workElNew('button', 'work-btn ghost', '重建索引');
    re.title = '把知识库与会话记忆重新镜像进统一索引';
    re.onclick = async () => {
      try {
        const rr = await api('/api/memory', { op: 'reindex' });
        const n = rr.result ? rr.result.mirrored : 0;
        toast('已重建：' + n + ' 条', 'success');
        workOpenMemory('', withMirror);
      } catch (e) { toastErr('重建失败', e); }
    };
    body.appendChild(re);
  }, async () => true, { okText: '关闭', hideCancel: true });
}

