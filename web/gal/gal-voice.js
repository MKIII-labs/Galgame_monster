let galEdTts = { active: '', presets: {} };
let galEdColor = '';                 // 编辑器里的主题色（'' 表示跟随默认）

// #rgb → #rrggbb（<input type=color> 只接受 6 位）
function _hex6(c) {
  const s = String(c || '').trim();
  if (/^#[0-9a-fA-F]{6}$/.test(s)) return s;
  if (/^#[0-9a-fA-F]{3}$/.test(s)) return '#' + s[1] + s[1] + s[2] + s[2] + s[3] + s[3];
  return '';
}

function galEdTtsRender() {
  const sel = $('#gal-ed-tts-preset');
  if (!sel) return;
  sel.innerHTML = '';
  const none = document.createElement('option');
  none.value = ''; none.textContent = '（跟随全局设置）';
  sel.appendChild(none);
  Object.keys(galEdTts.presets).forEach((n) => {
    const o = document.createElement('option'); o.value = n; o.textContent = n;
    sel.appendChild(o);
  });
  sel.value = galEdTts.active || '';
  const p = galEdTts.presets[galEdTts.active] || {};
  $('#gal-ed-tts-provider').value = p.provider || '';
  $('#gal-ed-tts-voice').value = p.voice || '';
  $('#gal-ed-tts-model').value = p.model || '';
  $('#gal-ed-tts-speed').value = (p.speed != null ? String(p.speed) : '');
  $('#gal-ed-tts-url').value = p.url || '';
  $('#gal-ed-tts-prompt').value = p.prompt_text || '';
  $('#gal-ed-tts-lang').value = p.prompt_lang || '';
  // 参考音频下拉：角色目录下的音频文件（用于 GPT-SoVITS 音色克隆）
  const refSel = $('#gal-ed-tts-ref');
  if (refSel) {
    const cur = p.ref_audio || '';
    refSel.innerHTML = '';
    const noneO = document.createElement('option');
    noneO.value = ''; noneO.textContent = '（不使用参考音频）';
    refSel.appendChild(noneO);
    galEditorAudioFiles().forEach((f) => {
      const o = document.createElement('option'); o.value = f; o.textContent = f;
      refSel.appendChild(o);
    });
    if (cur && !galEditorAudioFiles().includes(cur)) {
      const o = document.createElement('option'); o.value = cur; o.textContent = cur + '（缺失）';
      refSel.appendChild(o);
    }
    refSel.value = cur;
  }
  // 微调权重下拉：角色目录下的 .ckpt（GPT）/ .pth（SoVITS），可选，需本地 GPT-SoVITS
  [['#gal-ed-tts-gpt', 'gpt_weights', 'gpt', '（不使用 GPT 权重）'],
   ['#gal-ed-tts-sovits', 'sovits_weights', 'sovits', '（不使用 SoVITS 权重）']].forEach((a) => {
    const el = $(a[0]);
    if (!el) return;
    const cur = p[a[1]] || '';
    const files = galEditorWeightFiles(a[2]);
    el.innerHTML = '';
    const o0 = document.createElement('option'); o0.value = ''; o0.textContent = a[3];
    el.appendChild(o0);
    files.forEach((f) => { const o = document.createElement('option'); o.value = f; o.textContent = f; el.appendChild(o); });
    if (cur && !files.includes(cur)) {
      const o = document.createElement('option'); o.value = cur; o.textContent = cur + '（缺失）';
      el.appendChild(o);
    }
    el.value = cur;
  });
  const hasActive = !!galEdTts.active;
  const hasAny = Object.keys(galEdTts.presets).length > 0;
  ['gal-ed-tts-provider', 'gal-ed-tts-voice', 'gal-ed-tts-model', 'gal-ed-tts-speed',
   'gal-ed-tts-url', 'gal-ed-tts-ref', 'gal-ed-tts-lang', 'gal-ed-tts-prompt',
   'gal-ed-tts-gpt', 'gal-ed-tts-sovits'].forEach((id) => {
    const el = $('#' + id); if (el) el.disabled = !hasActive;
  });
  const delBtn = $('#gal-ed-tts-del'); if (delBtn) delBtn.disabled = !hasAny;      // 有预设就能删
  const testBtn = $('#gal-ed-tts-test'); if (testBtn) testBtn.disabled = !hasActive;
}

// 角色目录下的音频文件（参考音频 / 绑定音频）
function galEditorAudioFiles() {
  const audio = /\.(wav|mp3|ogg|m4a|flac)$/i;
  return (galEditorFiles || []).filter((f) => audio.test(f));
}

// 角色目录下的 GPT-SoVITS 微调权重（gpt=.ckpt / sovits=.pth）
function galEditorWeightFiles(kind) {
  const re = kind === 'gpt' ? /\.ckpt$/i : /\.pth$/i;
  return (galEditorFiles || []).filter((f) => re.test(f));
}

// 立绘「绑定音频」下拉：从角色目录的音频里选
function galFillVoiceSelect(sel, cur) {
  if (!sel) return;
  sel.innerHTML = '';
  const none = document.createElement('option');
  none.value = ''; none.textContent = '绑定音频：不绑定';
  sel.appendChild(none);
  const files = galEditorAudioFiles();
  files.forEach((f) => {
    const o = document.createElement('option'); o.value = f; o.textContent = f;
    sel.appendChild(o);
  });
  if (cur && !files.includes(cur)) {
    const o = document.createElement('option'); o.value = cur; o.textContent = cur + '（缺失）';
    sel.appendChild(o);
  }
  sel.value = cur || '';
}
function galRefreshVoiceSelects() {
  $$('#gal-ed-sprites .gal-ed-voice').forEach((sel) => galFillVoiceSelect(sel, sel.value));
}

function galEdTtsWrite() {          // 把表单写回「当前」预设
  if (!galEdTts.active) return;
  const sp = parseFloat($('#gal-ed-tts-speed').value);
  galEdTts.presets[galEdTts.active] = {
    provider: $('#gal-ed-tts-provider').value,
    voice: $('#gal-ed-tts-voice').value.trim(),
    model: $('#gal-ed-tts-model').value.trim(),
    speed: (isFinite(sp) && sp > 0) ? sp : 1.0,
    url: $('#gal-ed-tts-url').value.trim(),
    ref_audio: ($('#gal-ed-tts-ref') || {}).value || '',
    prompt_text: ($('#gal-ed-tts-prompt') || {}).value.trim(),
    prompt_lang: ($('#gal-ed-tts-lang') || {}).value || '',
    gpt_weights: ($('#gal-ed-tts-gpt') || {}).value || '',
    sovits_weights: ($('#gal-ed-tts-sovits') || {}).value || '',
  };
}

// 编辑器里试听「当前」预设（用未保存的表单值，不落盘）
async function galEdTtsTest() {
  if (!galEdTts.active) { toast('请先新建或选择一个预设'); return; }
  galEdTtsWrite();
  const p = galEdTts.presets[galEdTts.active] || {};
  toast('正在合成测试语音…');
  try {
    const res = await fetch('/api/gal/tts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Auth-Token': TOKEN },
      body: JSON.stringify({
        text: '你好，这是该配音预设的试听。', force: true, override: p,
        character_id: ($('#gal-ed-id') || {}).value || '',   // 便于后端解析参考音频路径
      }),
    });
    if (!res.ok) {
      let msg = 'HTTP ' + res.status;
      try { msg = (await res.json()).error || msg; } catch (e) { /* 忽略 */ }
      toast('合成失败：' + msg, 'error');
      return;
    }
    toast('已开始播放试听');
    await galPlayTestUrl(URL.createObjectURL(await res.blob()));
  } catch (e) { toastErr('测试', e); }
}

// GAL 工具条上的配音预设快捷切换
function galRenderTtsPresets() {
  const sel = $('#gal-tts-preset');
  if (!sel) return;
  const t = (galState.char && galState.char.tts) || { active: '', presets: {} };
  const names = Object.keys(t.presets || {});
  sel.innerHTML = '';
  const none = document.createElement('option');
  none.value = ''; none.textContent = '音色：跟随全局';
  sel.appendChild(none);
  names.forEach((n) => {
    const o = document.createElement('option'); o.value = n; o.textContent = '音色：' + n;
    sel.appendChild(o);
  });
  sel.value = t.active || '';
  sel.title = names.length ? '该角色的配音预设（一键切换）'
                           : '该角色暂无配音预设：在「角色管理 → 编辑」里添加';
}

async function galSwitchTtsPreset(name) {
  if (!galState.charId) return;
  try {
    const r = await api('/api/gal/tts/active', { id: galState.charId, preset: name || '' });
    galState.char = (r && r.character) || galState.char;
    galRenderTtsPresets();
    toast(name ? ('配音预设已切换为「' + name + '」') : '配音改为跟随全局设置');
  } catch (e) { toastErr('切换', e); }
}

/* ---- 快速克隆音色：参考音频 → 试听 → 存为预设（需本地 GPT-SoVITS） ---- */
let galCloneFiles = [];

function galToggleClone(force) {
  const p = $('#gal-clone-panel');
  if (!p) return;
  const show = (typeof force === 'boolean') ? force : p.classList.contains('hidden');
  p.classList.toggle('hidden', !show);
  if (show) galCloneRefresh();
}

/* ---- 右上角「⋯」抽屉：收起低频操作 ---- */
function galToggleMenu(force) {
  const m = $('#gal-menu');
  if (!m) return;
  const show = (typeof force === 'boolean') ? force : m.classList.contains('hidden');
  m.classList.toggle('hidden', !show);
}

async function galCloneRefresh() {
  const cid = galState.charId || (curSession() || {}).gal_character_id || '';
  const hint = $('#gal-clone-hint');
  const sel = $('#gal-clone-ref');
  const audio = /\.(wav|mp3|ogg|m4a|flac)$/i;
  galCloneFiles = [];
  if (cid) {
    try { galCloneFiles = (await api('/api/gal/character/files', { id: cid })).files || []; }
    catch (e) { galCloneFiles = []; }
  }
  if (hint) hint.textContent = cid ? ('当前角色：' + ((galState.char && galState.char.name) || cid)) : '请先选择角色';
  if (!sel) return;
  const prev = sel.value;
  const files = galCloneFiles.filter((f) => audio.test(f));
  sel.innerHTML = '';
  const o0 = document.createElement('option');
  o0.value = '';
  o0.textContent = files.length ? '（选择参考音频）' : '（角色目录暂无音频，请先上传）';
  sel.appendChild(o0);
  files.forEach((f) => { const o = document.createElement('option'); o.value = f; o.textContent = f; sel.appendChild(o); });
  if (prev && files.includes(prev)) sel.value = prev;
}

function galCloneOverride() {
  return {
    provider: 'gpt-sovits',
    ref_audio: ($('#gal-clone-ref') || {}).value || '',
    prompt_text: (($('#gal-clone-prompt') || {}).value || '').trim(),
    prompt_lang: ($('#gal-clone-lang') || {}).value || 'zh',
  };
}

async function galCloneTest() {
  const cid = galState.charId || (curSession() || {}).gal_character_id || '';
  if (!cid) { toast('请先选择角色', 'error'); return; }
  const override = galCloneOverride();
  if (!override.ref_audio) { toast('请先选择参考音频', 'error'); return; }
  const text = (($('#gal-clone-text') || {}).value || '').trim();
  if (!text) { toast('请填写试听文本', 'error'); return; }
  const btn = $('#gal-clone-test');
  const old = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = '合成中…'; }
  toast('正在克隆并合成…（首次需加载模型，可能较慢）');
  try {
    const res = await fetch('/api/gal/tts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Auth-Token': TOKEN },
      body: JSON.stringify({ text: text, force: true, override: override, character_id: cid }),
    });
    if (!res.ok) {
      let msg = 'HTTP ' + res.status;
      try { msg = (await res.json()).error || msg; } catch (e) { /* 忽略 */ }
      toast('合成失败：' + msg, 'error');
      return;
    }
    const au = $('#gal-clone-audio');
    if (au) {
      au.src = URL.createObjectURL(await res.blob());
      au.classList.remove('hidden');
      try { await au.play(); } catch (e) { /* 自动播放被拦截，用户手动点 */ }
    }
    toast('试听就绪');
  } catch (e) { toastErr('合成', e); }
  finally { if (btn) { btn.disabled = false; btn.textContent = old; } }
}

async function galCloneSave() {
  const cid = galState.charId || (curSession() || {}).gal_character_id || '';
  if (!cid) { toast('请先选择角色', 'error'); return; }
  const override = galCloneOverride();
  if (!override.ref_audio) { toast('请先选择参考音频', 'error'); return; }
  const name = (($('#gal-clone-name') || {}).value || '').trim() || '克隆音色';
  let char = null;
  try { char = (await api('/api/gal/character/get', { id: cid })).character; }
  catch (e) { toastErr('读取角色', e); return; }
  if (!char) { toast('角色不存在', 'error'); return; }
  const tts = char.tts || { active: '', presets: {} };
  const presets = tts.presets || {};
  presets[name] = Object.assign({}, presets[name], override, { speed: 1.0, voice: '', model: '', url: '' });
  tts.presets = presets;
  if (!tts.active) tts.active = name;
  char.tts = tts;
  try { await api('/api/gal/character/save', { id: cid, character: char }); }
  catch (e) { toastErr('保存', e); return; }
  try { galState.char = (await api('/api/gal/character/get', { id: cid })).character || galState.char; }
  catch (e) { /* 忽略 */ }
  galRenderTtsPresets();
  toast('已存为配音预设「' + name + '」');
}

async function galCloneUpload(file) {
  const cid = galState.charId || (curSession() || {}).gal_character_id || '';
  if (!cid) { toast('请先选择角色', 'error'); return; }
  if (!file) return;
  try {
    const b64 = await fileToB64(file);
    const r = await api('/api/gal/character/upload', { id: cid, filename: file.name, data: b64 });
    galCloneFiles = r.files || galCloneFiles;
    await galCloneRefresh();
    if (r.path && $('#gal-clone-ref')) $('#gal-clone-ref').value = r.path;
    toast('已上传：' + r.path);
  } catch (e) { toastErr('上传', e); }
}

/* ---- 剧情节点显示 ---- */
function galSetNode(obj) {
  galState.node = obj || null;
  const el = $('#gal-role-name');
  if (!el) return;
  if (obj && obj.node_id) {
    // AI 自动剧本的节点 id 形如 a1 / a2 → 用「第 N 幕」表述，更不破坏沉浸感
    const m = /^a(\d+)$/.exec(String(obj.node_id));
    const head = m ? ('第 ' + m[1] + ' 幕') : ('节点 ' + obj.node_id);
    el.textContent = head +
      (obj.max_rounds ? ('（' + (obj.round || 0) + '/' + obj.max_rounds + '）') : '');
  } else {
    el.textContent = '';
  }
}

// 分幕提示：直接追加一段暗淡旁白分隔（不走打字机 / 朗读 / 队列）
function galAppendDivider(text) {
  const t = $('#gal-text');
  if (!t || !text) return;
  const d = document.createElement('div');
  d.className = 'gal-seg narr gal-divider';
  d.textContent = text;
  t.appendChild(d);
  t.scrollTop = t.scrollHeight;
}

/* ---- 角色 / 立绘 / 剧本编辑器 ---- */