/* ============================================================
   背景管理：上传 / 分类标签 / 设为当前舞台背景
   ============================================================ */
async function galLoadBgs() {
  try {
    const r = await api('/api/gal/backgrounds', {});
    galState.bgs = (r && r.items) || [];
    if (r && r.dims && r.dims.length) galState.bgDims = r.dims;
  } catch (e) {
    galState.bgs = galState.bgs || [];
  }
  return galState.bgs;
}

// 把一张背景的所有标签摊平成数组（分类维度 + extra）
function galBgTagsFlat(b) {
  const tg = (b && b.tags) || {};
  const out = [];
  (galState.bgDims || []).forEach((d) => { (tg[d] || []).forEach((t) => out.push(t)); });
  (tg.extra || []).forEach((t) => out.push(t));
  return out;
}

function galBgSearchText(b) {
  return [((b && b.name) || ''), ((b && b.file) || ''), galBgTagsFlat(b).join(' ')]
    .join(' ').toLowerCase();
}

function galBgCur() {
  return (galState.bgs || []).find((b) => b.file === galState.bgSel) || null;
}

function galRenderBgCards() {
  const box = $('#gal-bgs-list');
  if (!box) return;
  const q = (($('#gal-bgs-search') || {}).value || '').trim().toLowerCase();
  const all = galState.bgs || [];
  const list = all.filter((b) => !q || galBgSearchText(b).includes(q));
  const cnt = $('#gal-bgs-count');
  if (cnt) cnt.textContent = list.length + ' / ' + all.length;
  box.innerHTML = '';
  if (!list.length) {
    const em = document.createElement('div');
    em.className = 'gal-chars-empty';
    em.textContent = all.length ? '没有匹配的背景' : '还没有背景：点「添加背景」上传图片';
    box.appendChild(em);
    return;
  }
  list.forEach((b) => {
    const card = document.createElement('button');
    card.className = 'gal-char-card gal-bg-card' + (b.file === galState.bgSel ? ' active' : '');
    const img = document.createElement('img');
    img.className = 'gal-char-thumb gal-bg-thumb';
    img.alt = '';
    galMediaUrl('bg', b.file).then((u) => { img.src = u; }).catch(() => { img.classList.add('empty'); });
    const meta = document.createElement('div');
    meta.className = 'gal-char-meta';
    const nm = document.createElement('div');
    nm.className = 'gal-char-name';
    nm.textContent = b.name || b.file;
    const sub = document.createElement('div');
    sub.className = 'gal-char-sub';
    sub.textContent = galBgTagsFlat(b).join(' · ') || '（无标签）';
    meta.appendChild(nm);
    meta.appendChild(sub);
    card.appendChild(img);
    card.appendChild(meta);
    card.onclick = () => { galState.bgSel = b.file; galRenderBgCards(); };
    card.ondblclick = () => { galState.bgSel = b.file; galBgUse(); };
    box.appendChild(card);
  });
}

function galToggleBgs(force) {
  const p = $('#gal-bgs');
  if (!p) return;
  const show = (typeof force === 'boolean') ? force : p.classList.contains('hidden');
  p.classList.toggle('hidden', !show);
  if (show) {
    galToggleChars(false);      // 与角色面板同位置，避免叠在一起
    galRenderBgCards();
    galLoadBgs().then(() => galRenderBgCards());
    const s = $('#gal-bgs-search');
    if (s) { s.value = ''; s.focus(); }
  }
}

// 设为当前舞台背景：立即切换 + 记到会话（刷新 / 切会话后仍在）
function galBgUse() {
  const b = galBgCur();
  if (!b) { toast('请先选中一张背景'); return; }
  galSetBackground(b.file, true);
  toast('已切换背景：' + (b.name || b.file));
}

// 打开 / 关闭「名称 + 分类标签」编辑区
function galBgOpenEditor(open) {
  const box = $('#gal-bg-editor');
  if (!box) return;
  const b = galBgCur();
  const show = (typeof open === 'boolean') ? open : box.classList.contains('hidden');
  if (show && !b) { toast('请先选中一张背景'); return; }
  box.classList.toggle('hidden', !show);
  if (!show) { galState.bgEditing = ''; return; }
  galState.bgEditing = b.file;
  const nameEl = $('#gal-bg-ed-name');
  if (nameEl) nameEl.value = b.name || '';
  const extraEl = $('#gal-bg-ed-extra');
  if (extraEl) extraEl.value = (((b.tags || {}).extra) || []).join('、');
  const tagsBox = $('#gal-bg-ed-tags');
  if (tagsBox) {
    tagsBox.innerHTML = '';
    const tg = b.tags || {};
    (galState.bgDims || []).forEach((d) => {
      const row = document.createElement('div');
      row.className = 'gal-bg-ed-row';
      const lb = document.createElement('label');
      lb.textContent = d;
      const inp = document.createElement('input');
      inp.className = 'field-input';
      inp.type = 'text';
      inp.dataset.dim = d;
      inp.placeholder = d + '标签，用逗号分隔';
      inp.value = (tg[d] || []).join('、');
      row.appendChild(lb);
      row.appendChild(inp);
      tagsBox.appendChild(row);
    });
  }
}

function galBgReadEditor() {
  const name = (($('#gal-bg-ed-name') || {}).value || '').trim();
  const tags = {};
  $$('#gal-bg-ed-tags input[data-dim]').forEach((inp) => {
    tags[inp.dataset.dim] = String(inp.value || '').split(/[、,，\s]+/).map((s) => s.trim()).filter(Boolean);
  });
  tags.extra = String((($('#gal-bg-ed-extra') || {}).value) || '')
    .split(/[、,，\s]+/).map((s) => s.trim()).filter(Boolean);
  return { name, tags };
}

async function galBgSaveEditor() {
  const b = galBgCur();
  if (!b) return;
  const payload = galBgReadEditor();
  try {
    const r = await api('/api/gal/background/save', { file: b.file, name: payload.name, tags: payload.tags });
    if (!r || !r.ok) throw new Error((r && r.error) || '保存失败');
    const i = (galState.bgs || []).findIndex((x) => x.file === b.file);
    if (i >= 0) galState.bgs[i] = r.item;
    galBgOpenEditor(false);
    galRenderBgCards();
    toast('已保存');
  } catch (e) { toastErr('保存', e); }
}

async function galBgDelete() {
  const b = galBgCur();
  if (!b) { toast('请先选中一张背景'); return; }
  if (!confirm('删除背景「' + (b.name || b.file) + '」？图片文件会被一并删除，不可恢复。')) return;
  try {
    const r = await api('/api/gal/background/delete', { file: b.file });
    if (!r || !r.ok) throw new Error((r && r.error) || '删除失败');
    // 删掉的正好是当前舞台背景 → 一并清掉会话记录与画面
    if (((curSession() || {}).gal_bg || '') === b.file) galSetBackground('', true);
    galState.bgSel = '';
    await galLoadBgs();
    galBgOpenEditor(false);
    galRenderBgCards();
    toast('已删除');
  } catch (e) { toastErr('删除', e); }
}

async function galBgUpload(files) {
  const list = Array.from(files || []);
  if (!list.length) return;
  let ok = 0;
  for (const f of list) {
    try {
      const b64 = await fileToB64(f);
      const r = await api('/api/gal/background/upload', { filename: f.name, data: b64 });
      if (!r || !r.ok) throw new Error((r && r.error) || '上传失败');
      ok++;
      if (r.item) galState.bgSel = r.item.file;
    } catch (e) {
      toast('「' + f.name + '」上传失败：' + (e && e.message ? e.message : e), 'error');
    }
  }
  await galLoadBgs();
  galRenderBgCards();
  if (ok) toast('已添加 ' + ok + ' 张背景');
}

/* ============================================================
   BGM 管理：上传 / 分类标签 / 设为当前舞台 BGM
   ============================================================ */
async function galLoadBgms() {
  try {
    const r = await api('/api/gal/bgms', {});
    galState.bgms = (r && r.items) || [];
    if (r && r.dims && r.dims.length) galState.bgmDims = r.dims;
  } catch (e) {
    galState.bgms = galState.bgms || [];
  }
  return galState.bgms;
}

// 把一首 BGM 的所有标签摊平成数组（分类维度 + extra）
function galBgmTagsFlat(b) {
  const tg = (b && b.tags) || {};
  const out = [];
  (galState.bgmDims || []).forEach((d) => { (tg[d] || []).forEach((t) => out.push(t)); });
  (tg.extra || []).forEach((t) => out.push(t));
  return out;
}

function galBgmSearchText(b) {
  return [((b && b.name) || ''), ((b && b.file) || ''), galBgmTagsFlat(b).join(' ')]
    .join(' ').toLowerCase();
}

function galBgmCur() {
  return (galState.bgms || []).find((b) => b.file === galState.bgmSel) || null;
}

function galRenderBgmCards() {
  const box = $('#gal-bgms-list');
  if (!box) return;
  const q = (($('#gal-bgms-search') || {}).value || '').trim().toLowerCase();
  const all = galState.bgms || [];
  const list = all.filter((b) => !q || galBgmSearchText(b).includes(q));
  const cnt = $('#gal-bgms-count');
  if (cnt) cnt.textContent = list.length + ' / ' + all.length;
  box.innerHTML = '';
  if (!list.length) {
    const em = document.createElement('div');
    em.className = 'gal-chars-empty';
    em.textContent = all.length ? '没有匹配的音乐' : '还没有音乐：点「添加音乐」上传音频';
    box.appendChild(em);
    return;
  }
  const cur = galState.bgm;
  list.forEach((b) => {
    const card = document.createElement('button');
    card.className = 'gal-char-card gal-bgm-card' + (b.file === galState.bgmSel ? ' active' : '');
    const ico = document.createElement('div');
    ico.className = 'gal-bgm-icon';
    ico.innerHTML = icon('music');   // 正在播放的高亮一下（颜色由类控制）
    const meta = document.createElement('div');
    meta.className = 'gal-char-meta';
    const nm = document.createElement('div');
    nm.className = 'gal-char-name';
    nm.textContent = b.name || b.file;
    const sub = document.createElement('div');
    sub.className = 'gal-char-sub';
    sub.textContent = galBgmTagsFlat(b).join(' · ') || '（无标签）';
    meta.appendChild(nm);
    meta.appendChild(sub);
    card.appendChild(ico);
    card.appendChild(meta);
    card.onclick = () => { galState.bgmSel = b.file; galRenderBgmCards(); };
    card.ondblclick = () => { galState.bgmSel = b.file; galBgmUse(); };
    box.appendChild(card);
  });
}

function galToggleBgms(force) {
  const p = $('#gal-bgms');
  if (!p) return;
  const show = (typeof force === 'boolean') ? force : p.classList.contains('hidden');
  p.classList.toggle('hidden', !show);
  if (show) {
    galToggleChars(false);      // 三个面板同位置，互斥
    galToggleBgs(false);
    galRenderBgmCards();
    galLoadBgms().then(() => galRenderBgmCards());
    const s = $('#gal-bgms-search');
    if (s) { s.value = ''; s.focus(); }
  }
}

// 设为当前舞台 BGM：立即播放 + 记到会话（刷新 / 切会话后仍在）
function galBgmUse() {
  const b = galBgmCur();
  if (!b) { toast('请先选中一首音乐'); return; }
  galSetBgmPersist(b.file);
  toast('正在播放：' + (b.name || b.file));
}

function galBgmStop() {
  galSetBgmPersist('');
  toast('已停止 BGM');
}

// 试听：只播一次，不影响当前舞台 BGM
function galBgmPreview() {
  const b = galBgmCur();
  if (!b) { toast('请先选中一首音乐'); return; }
  if (galState.bgmPreview) { try { galState.bgmPreview.pause(); } catch (e) { /* 忽略 */ } galState.bgmPreview = null; }
  galMediaUrl('bgm', b.file).then((u) => {
    const a = new Audio(u);
    a.volume = galBgmVolume();
    galState.bgmPreview = a;
    a.play().catch(() => toast('浏览器拦截了试听，请先点一下页面', 'error'));
  }).catch(() => toast('试听失败', 'error'));
}

// 打开 / 关闭「名称 + 分类标签」编辑区
function galBgmOpenEditor(open) {
  const box = $('#gal-bgm-editor');
  if (!box) return;
  const b = galBgmCur();
  const show = (typeof open === 'boolean') ? open : box.classList.contains('hidden');
  if (show && !b) { toast('请先选中一首音乐'); return; }
  box.classList.toggle('hidden', !show);
  if (!show) { galState.bgmEditing = ''; return; }
  galState.bgmEditing = b.file;
  const nameEl = $('#gal-bgm-ed-name');
  if (nameEl) nameEl.value = b.name || '';
  const extraEl = $('#gal-bgm-ed-extra');
  if (extraEl) extraEl.value = (((b.tags || {}).extra) || []).join('、');
  const tagsBox = $('#gal-bgm-ed-tags');
  if (tagsBox) {
    tagsBox.innerHTML = '';
    const tg = b.tags || {};
    (galState.bgmDims || []).forEach((d) => {
      const row = document.createElement('div');
      row.className = 'gal-bg-ed-row';
      const lb = document.createElement('label');
      lb.textContent = d;
      const inp = document.createElement('input');
      inp.className = 'field-input';
      inp.type = 'text';
      inp.dataset.dim = d;
      inp.placeholder = d + '标签，用逗号分隔';
      inp.value = (tg[d] || []).join('、');
      row.appendChild(lb);
      row.appendChild(inp);
      tagsBox.appendChild(row);
    });
  }
}

function galBgmReadEditor() {
  const name = (($('#gal-bgm-ed-name') || {}).value || '').trim();
  const tags = {};
  $$('#gal-bgm-ed-tags input[data-dim]').forEach((inp) => {
    tags[inp.dataset.dim] = String(inp.value || '').split(/[、,，\s]+/).map((s) => s.trim()).filter(Boolean);
  });
  tags.extra = String((($('#gal-bgm-ed-extra') || {}).value) || '')
    .split(/[、,，\s]+/).map((s) => s.trim()).filter(Boolean);
  return { name, tags };
}

async function galBgmSaveEditor() {
  const b = galBgmCur();
  if (!b) return;
  const payload = galBgmReadEditor();
  try {
    const r = await api('/api/gal/bgm/save', { file: b.file, name: payload.name, tags: payload.tags });
    if (!r || !r.ok) throw new Error((r && r.error) || '保存失败');
    const i = (galState.bgms || []).findIndex((x) => x.file === b.file);
    if (i >= 0) galState.bgms[i] = r.item;
    galBgmOpenEditor(false);
    galRenderBgmCards();
    toast('已保存');
  } catch (e) { toastErr('保存', e); }
}

async function galBgmDelete() {
  const b = galBgmCur();
  if (!b) { toast('请先选中一首音乐'); return; }
  if (!confirm('删除音乐「' + (b.name || b.file) + '」？音频文件会被一并删除，不可恢复。')) return;
  try {
    const r = await api('/api/gal/bgm/delete', { file: b.file });
    if (!r || !r.ok) throw new Error((r && r.error) || '删除失败');
    // 删掉的正好是当前舞台 BGM → 一并停掉
    if (galState.bgm === b.file || ((curSession() || {}).gal_bgm || '') === b.file) galSetBgmPersist('');
    galState.bgmSel = '';
    await galLoadBgms();
    galBgmOpenEditor(false);
    galRenderBgmCards();
    toast('已删除');
  } catch (e) { toastErr('删除', e); }
}

async function galBgmUpload(files) {
  const list = Array.from(files || []);
  if (!list.length) return;
  let ok = 0;
  for (const f of list) {
    try {
      const b64 = await fileToB64(f);
      const r = await api('/api/gal/bgm/upload', { filename: f.name, data: b64 });
      if (!r || !r.ok) throw new Error((r && r.error) || '上传失败');
      ok++;
      if (r.item) galState.bgmSel = r.item.file;
    } catch (e) {
      toast('「' + f.name + '」上传失败：' + (e && e.message ? e.message : e), 'error');
    }
  }
  await galLoadBgms();
  galRenderBgmCards();
  if (ok) toast('已添加 ' + ok + ' 首音乐');
}
