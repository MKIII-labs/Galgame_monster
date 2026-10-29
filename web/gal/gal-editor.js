let galEditorFiles = [];

async function galOpenEditor(cid, isNew) {
  const modal = $('#gal-modal');
  if (!modal) return;
  galEditorFiles = [];
  const useId = isNew ? '' : (cid || '');
  $('#gal-ed-id').value = useId;
  $('#gal-ed-id').disabled = !isNew;                 // 已有角色不允许改 id（目录名）
  $('#gal-ed-name').value = '';
  $('#gal-ed-scale').value = '1';
  $('#gal-ed-profile').value = '';
  if ($('#gal-ed-intro')) $('#gal-ed-intro').value = '';
  galEdColor = '';
  if ($('#gal-ed-color')) $('#gal-ed-color').value = '#7c9cff';
  $('#gal-ed-story').value = '';
  $('#gal-ed-story-on').checked = false;
  $('#gal-ed-sprites').innerHTML = '';
  let char = null, story = null;
  if (useId) {
    try { char = (await api('/api/gal/character/get', { id: useId })).character; } catch (e) { /* 忽略 */ }
    try { story = (await api('/api/gal/story/get', { id: useId })).story; } catch (e) { /* 忽略 */ }
    try { galEditorFiles = (await api('/api/gal/character/files', { id: useId })).files || []; } catch (e) { /* 忽略 */ }
  }
  const dl = $('#gal-ed-filelist');
  if (dl) {
    dl.innerHTML = '';
    galEditorFiles.forEach((f) => { const o = document.createElement('option'); o.value = f; dl.appendChild(o); });
  }
  if (char) {
    $('#gal-ed-name').value = char.name || '';
    $('#gal-ed-scale').value = String(char.sprite_scale || 1);
    $('#gal-ed-profile').value = char.profile || '';
    if ($('#gal-ed-intro')) $('#gal-ed-intro').value = char.intro || '';
    galEdColor = char.color || '';
    if ($('#gal-ed-color')) $('#gal-ed-color').value = _hex6(galEdColor) || '#7c9cff';
    (char.sprites || []).forEach((s) => galEditorAddRow(s));
  } else {
    galEditorAddRow(null);
  }
  // 配音预设
  galEdTts = { active: '', presets: {} };
  if (char && char.tts) {
    galEdTts.active = char.tts.active || '';
    const src = char.tts.presets || {};
    Object.keys(src).forEach((n) => { galEdTts.presets[n] = Object.assign({}, src[n]); });
  }
  galEdTtsRender();
  galLoadTtsVoices(true);          // 静默填充音色联想（失败也不提示）
  const nm2 = $('#gal-name');
  if (nm2) nm2.textContent = char ? char.name : '';
  if (story) {
    $('#gal-ed-story').value = JSON.stringify(story, null, 2);
    const s = curSession();
    $('#gal-ed-story-on').checked = !!(s && s.gal_node_id);
  }
  showWithAnim(modal);
}

function galEditorAddRow(sprite) {
  const box = $('#gal-ed-sprites');
  if (!box) return;
  const row = document.createElement('div');
  row.className = 'gal-ed-row';
  const idx = document.createElement('span'); idx.className = 'gal-ed-idx';
  const thumb = document.createElement('img');
  thumb.className = 'gal-ed-thumb'; thumb.alt = ''; thumb.title = '点击预览';
  const path = document.createElement('input');
  path.className = 'field-input gal-ed-path'; path.placeholder = 'sprites/xx.png';
  path.value = (sprite && sprite.path) || '';
  path.setAttribute('list', 'gal-ed-filelist');
  const tags = document.createElement('input');
  tags.className = 'field-input gal-ed-tags'; tags.placeholder = '情绪标签：微笑,开心';
  tags.value = ((sprite && sprite.emotion_tags) || []).join(',');
  const voice = document.createElement('select');
  voice.className = 'field-input gal-ed-voice';
  galFillVoiceSelect(voice, (sprite && sprite.voice_path) || '');
  const up = document.createElement('button');
  up.className = 'ghost gal-ed-move'; up.textContent = '↑'; up.title = '上移';
  up.onclick = () => { const p = row.previousElementSibling; if (p) { box.insertBefore(row, p); galEditorRenumber(); } };
  const down = document.createElement('button');
  down.className = 'ghost gal-ed-move'; down.textContent = '↓'; down.title = '下移';
  down.onclick = () => { const nx = row.nextElementSibling; if (nx) { box.insertBefore(nx, row); galEditorRenumber(); } };
  const del = document.createElement('button');
  del.className = 'ghost gal-ed-del'; del.textContent = '✕'; del.title = '移除该立绘';
  del.onclick = () => { row.remove(); galEditorRenumber(); };
  // 缩略图：路径填写后按需取回预览
  const refreshThumb = () => {
    const p = path.value.trim();
    const cid = ($('#gal-ed-id').value || '').trim();
    if (!p || !cid) { thumb.removeAttribute('src'); thumb.style.visibility = 'hidden'; return; }
    galAssetUrl(cid, p).then((u) => { thumb.src = u; thumb.style.visibility = 'visible'; })
      .catch(() => { thumb.removeAttribute('src'); thumb.style.visibility = 'hidden'; });
  };
  path.addEventListener('change', refreshThumb);
  path.addEventListener('blur', refreshThumb);
  thumb.onclick = () => { if (thumb.src) window.open(thumb.src, '_blank'); };
  row.appendChild(idx); row.appendChild(thumb); row.appendChild(path);
  row.appendChild(tags); row.appendChild(voice);
  row.appendChild(up); row.appendChild(down); row.appendChild(del);
  box.appendChild(row);
  galEditorRenumber();
  refreshThumb();
}

function galEditorRenumber() {
  $$('#gal-ed-sprites .gal-ed-row').forEach((r, i) => {
    const idx = r.querySelector('.gal-ed-idx');
    if (idx) idx.textContent = String(i + 1);
  });
}

function galEditorCollect() {
  galEdTtsWrite();
  const sprites = [];
  $$('#gal-ed-sprites .gal-ed-row').forEach((r) => {
    const p = r.querySelector('.gal-ed-path').value.trim();
    if (!p) return;
    sprites.push({
      path: p,
      emotion_tags: r.querySelector('.gal-ed-tags').value.split(/[,，]/).map((s) => s.trim()).filter(Boolean),
      voice_path: r.querySelector('.gal-ed-voice').value.trim(),
    });
  });
  let scale = parseFloat($('#gal-ed-scale').value);
  if (!isFinite(scale)) scale = 1;
  return {
    name: $('#gal-ed-name').value.trim(),
    color: galEdColor || '',
    sprite_scale: scale,
    profile: $('#gal-ed-profile').value,
    intro: $('#gal-ed-intro') ? $('#gal-ed-intro').value : '',
    sprites: sprites,
    tts: { active: galEdTts.active || '', presets: galEdTts.presets },
  };
}

async function galEditorUpload(files) {
  const cid = $('#gal-ed-id').value.trim();
  if (!cid) { toast('请先填写角色 id', 'error'); return; }
  const list = Array.from(files || []);
  if (!list.length) return;
  let ok = 0;
  for (const file of list) {
    try {
      const b64 = await fileToB64(file);
      const r = await api('/api/gal/character/upload', { id: cid, filename: file.name, data: b64 });
      galEditorFiles = r.files || galEditorFiles;
      if (r.path) { galEditorAddRow({ path: r.path, emotion_tags: [], voice_path: '' }); ok++; }
    } catch (e) { toast('上传失败：' + file.name + '：' + e, 'error'); }
  }
  const dl = $('#gal-ed-filelist');
  if (dl) {
    dl.innerHTML = '';
    galEditorFiles.forEach((f) => { const o = document.createElement('option'); o.value = f; dl.appendChild(o); });
  }
  galRefreshVoiceSelects();
  if (ok) toast('已上传 ' + ok + ' 张图片');
}

// 上传 GPT-SoVITS 参考音频：写入角色目录并绑到当前配音预设
async function galUploadRefAudio(file) {
  const cid = $('#gal-ed-id').value.trim();
  if (!cid) { toast('请先填写角色 id', 'error'); return; }
  if (!galEdTts.active) { toast('请先在「配音」里新建或选择一个预设'); return; }
  if (!file) return;
  try {
    const b64 = await fileToB64(file);
    const r = await api('/api/gal/character/upload', { id: cid, filename: file.name, data: b64 });
    galEditorFiles = r.files || galEditorFiles;
    const dl = $('#gal-ed-filelist');
    if (dl) {
      dl.innerHTML = '';
      galEditorFiles.forEach((f) => { const o = document.createElement('option'); o.value = f; dl.appendChild(o); });
    }
    galEdTtsWrite();                               // 先保留其它字段
    if (r.path) {
      galEdTts.presets[galEdTts.active] = Object.assign({}, galEdTts.presets[galEdTts.active], { ref_audio: r.path });
      galEdTtsRender();
      galRefreshVoiceSelects();
      toast('已上传参考音频：' + r.path);
    }
  } catch (e) { toastErr('上传', e); }
}

// AI 帮写角色设定（复用「设置 → 模型接入」的聊天接口）
async function galAiDraft() {
  const name = $('#gal-ed-name').value.trim();
  if (!name) { toast('请先填写角色名', 'error'); return; }
  const btn = $('#gal-ed-draft');
  const old = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = '生成中…'; }
  try {
    const r = await api('/api/gal/character/draft', {
      name: name, profile: $('#gal-ed-profile').value.trim(),
    });
    if (r && r.profile) { $('#gal-ed-profile').value = r.profile; toast('已生成角色设定'); }
    else toast('生成结果为空', 'error');
  } catch (e) { toastErr('生成', e); }
  finally { if (btn) { btn.disabled = false; btn.textContent = old; } }
}

async function galSaveEditor() {
  const cid = $('#gal-ed-id').value.trim();
  if (!cid) { toast('请填写角色 id', 'error'); return; }
  try {
    await api('/api/gal/character/save', { id: cid, character: galEditorCollect() });
    // 剧本（可为空）
    const raw = $('#gal-ed-story').value.trim();
    if (raw) {
      let story = null;
      try { story = JSON.parse(raw); } catch (e) { toast('剧本 JSON 解析失败，已跳过剧本', 'error'); }
      if (story) await api('/api/gal/story/save', { id: cid, story: story });
    }
    // 剧本启用状态
    await api('/api/gal/session', {
      id: state.sessions.current,
      gal_character_id: cid,
      gal_story_on: !!$('#gal-ed-story-on').checked,
    });
  } catch (e) { toastErr('保存', e); return; }
  hideWithAnim($('#gal-modal'));
  await galLoadCharacters();
  await galSetCharacter(cid);
  toast('已保存角色：' + cid);
}
