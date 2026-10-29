const GAL_THEMES = ['sakura', 'night', 'washi', 'mono', 'cyber', 'pixel'];
function galApplyTheme() {
  const root = document.documentElement;
  galSyncComposerPlaceholder();                 // 输入框占位符也要跟着模式 / 主题走
  if (state.uiMode !== 'gal') { root.removeAttribute('data-gal-theme'); return; }
  const t = String(galCfg('gal_theme', 'sakura'));
  root.setAttribute('data-gal-theme', GAL_THEMES.indexOf(t) >= 0 ? t : 'sakura');
}

/* 输入框占位符：GAL 下换成沉浸式文案（不再出现「给 AI 发送消息…」这种系统口吻），
   其它模式恢复 HTML 里的原文案（首次调用时记下来）。 */
let _galComposerPh = null;
function galSyncComposerPlaceholder() {
  const inp = $('#input');
  if (!inp) return;
  if (_galComposerPh == null) _galComposerPh = inp.placeholder || '';
  if (state.uiMode !== 'gal') { inp.placeholder = _galComposerPh; return; }
  const t = String(galCfg('gal_theme', 'sakura'));
  inp.placeholder = (t === 'pixel') ? '> _' : '说点什么…（Enter 发送）';
}

// GAL 视图偏好：对白框高度 / 立绘过渡 / 工具条沉浸淡出（设置保存后立即生效）
function galApplyViewPrefs() {
  const c = state.config || {};
  const d = $('#gal-dialog');
  if (d) {
    const h = Math.max(30, Math.min(70, parseInt(c.gal_dialog_max, 10) || 46));
    d.style.maxHeight = h + '%';
  }
  const root = document.documentElement;
  root.classList.toggle('gal-no-sprite-fx', c.gal_sprite_fx === false);
  root.classList.toggle('gal-bar-static', c.gal_bar_auto === false);
  galSyncCursorHide();
}

/* ---- 沉浸阅读：鼠标停在舞台上 3 秒不动就隐藏指针（移一下就立刻回来） ---- */
let _galCursorTimer = null;
function galSyncCursorHide() {
  const panel = $('#gal-panel');
  if (!panel) return;
  if (galCfg('gal_hide_cursor', true) === false) {      // 关掉时：清计时、恢复指针
    clearTimeout(_galCursorTimer);
    _galCursorTimer = null;
    panel.classList.remove('gal-cursor-off');
    return;
  }
  if (panel._cursorBound) return;                       // 只绑一次
  panel._cursorBound = true;
  const wake = () => {
    if (galCfg('gal_hide_cursor', true) === false) {     // 期间被关掉：立刻恢复、不再计时
      clearTimeout(_galCursorTimer);
      _galCursorTimer = null;
      panel.classList.remove('gal-cursor-off');
      return;
    }
    panel.classList.remove('gal-cursor-off');
    clearTimeout(_galCursorTimer);
    _galCursorTimer = setTimeout(() => panel.classList.add('gal-cursor-off'), 3000);
  };
  panel.addEventListener('mousemove', wake);
  panel.addEventListener('mousedown', wake);
  panel.addEventListener('mouseleave', () => {          // 离开舞台：立刻恢复并停表
    clearTimeout(_galCursorTimer);
    _galCursorTimer = null;
    panel.classList.remove('gal-cursor-off');
  });
}

// 角色主题色：应用到角色按钮、舞台与对话框名字
function galApplyCharColor() {
  const color = (galState.char && galState.char.color) || '';
  const stage = $('#gal-panel');
  if (stage) {
    if (color) stage.style.setProperty('--gal-char-color', color);
    else stage.style.removeProperty('--gal-char-color');
  }
  const nm = $('#gal-name');
  if (nm) {
    nm.style.color = '';                          // 文字色交给主题，保证对比度
    nm.style.background = color || '';            // 角色主题色用作名牌底色
  }
}

async function galSetCharacter(cid, opts) {
  const o = opts || {};
  galState.charId = cid || '';
  galState.char = null;
  if (cid) {
    try { const r = await api('/api/gal/character/get', { id: cid }); galState.char = (r && r.character) || null; }
    catch (e) { galState.char = null; }
    try {
      applySessions(await api('/api/gal/session', {
        id: state.sessions.current, gal_enabled: true, gal_character_id: cid,
      }));
    } catch (e) { /* 忽略 */ }
  }
  const nm = $('#gal-role-name');
  if (nm) nm.textContent = '';
  if (galState.char && (galState.char.sprites || []).length) galShowSprite(galState.char.sprites[0].path, 0);
  else { const box = $('#gal-sprites'); if (box) box.innerHTML = ''; }
  galRenderCharBtn();
  galApplyCharColor();
  galRenderTtsPresets();
  galRenderIntro();                       // 空会话时展示「角色介绍」
  if (o.showIntro) galShowIntroCard();    // 用户主动选角色 → 弹开场卡
}

/* ---- 角色介绍：舞台序章 + 开场卡（只给玩家看，不发给 AI） ---- */
function galIntroText() {
  return (galState.char && galState.char.intro) ? String(galState.char.intro).trim() : '';
}

// 当前会话还没有 AI 回复时才显示，避免盖住正在进行的剧情
function galRenderIntro() {
  const box = $('#gal-intro');
  if (!box) return;
  const s = curSession();
  const hasAi = !!((s && s.messages) || []).some((m) => m && m.role === 'assistant');
  const txt = galIntroText();
  if (!txt || hasAi) {
    box.classList.add('hidden');
    box.textContent = '';
    return;
  }
  box.innerHTML = safeRenderMarkdown(txt);
  box.classList.remove('hidden');
}

function galShowIntroCard() {
  const card = $('#gal-intro-card');
  if (!card) return;
  const txt = galIntroText();
  if (!txt) return;
  const c = galState.char || {};
  const av = $('#gal-intro-avatar');
  if (av) {
    const sp = (c.sprites || [])[0];
    if (sp) {
      av.style.display = '';
      galAssetUrl(c.id, sp.path).then((u) => { av.src = u; }).catch(() => { av.style.display = 'none'; });
    } else {
      av.removeAttribute('src');
      av.style.display = 'none';
    }
  }
  const nm = $('#gal-intro-name');
  if (nm) nm.textContent = c.name || c.id || '';
  const bd = $('#gal-intro-body');
  if (bd) bd.innerHTML = safeRenderMarkdown(txt);
  card.classList.remove('hidden');
  // 开场卡与舞台序章是**同一份角色介绍**：同时显示会透过半透明遮罩"叠"在一起。
  // 弹卡时先把舞台那份收起来，关卡时再恢复（见 galCloseIntroCard）。
  const stageIntro = $('#gal-intro');
  if (stageIntro) stageIntro.classList.add('hidden');
}

function galCloseIntroCard() {
  const card = $('#gal-intro-card');
  if (card) card.classList.add('hidden');
  galRenderIntro();     // 关卡后恢复舞台序章（空会话时才显示；已有 AI 回复则内部自动跳过）
}

/* ---- 角色管理：删除 / 导入 / 导出 ---- */
function fileToB64(file) {
  return new Promise((resolve, reject) => {
    const fr = new FileReader();
    fr.onload = () => resolve(String(fr.result).split(',')[1] || '');
    fr.onerror = () => reject(new Error('读取失败'));
    fr.readAsDataURL(file);
  });
}

async function galDeleteCharacter() {
  const cid = galState.charId || ((curSession() || {}).gal_character_id) || '';
  if (!cid) { toast('请先选择要删除的角色'); return; }
  const c = galState.characters.find((x) => x.id === cid);
  const label = (c && c.name) || cid;
  if (!confirm('删除角色「' + label + '」？其立绘与剧本会一并删除，且不可恢复。')) return;
  try {
    const r = await api('/api/gal/character/delete', { id: cid });
    galState.characters = (r && r.characters) || [];
    await galSetCharacter('');
    galRenderCharBtn(); galRenderCharCards();
    toast('已删除角色：' + label);
  } catch (e) { toastErr('删除', e); }
}

async function galExportCharacter() {
  const cid = galState.charId || ((curSession() || {}).gal_character_id) || '';
  if (!cid) { toast('请先选择要导出的角色'); return; }
  try {
    const res = await fetch('/api/gal/character/export', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Auth-Token': TOKEN },
      body: JSON.stringify({ id: cid }),
    });
    if (!res.ok) {
      let msg = 'HTTP ' + res.status;
      try { msg = (await res.json()).error || msg; } catch (e) { /* 忽略 */ }
      toast('导出失败：' + msg, 'error');
      return;
    }
    const cd = res.headers.get('Content-Disposition') || '';
    const m = /filename\*=UTF-8''([^;]+)/i.exec(cd);
    const name = m ? decodeURIComponent(m[1]) : (cid + '.char');
    saveBlobUrl(URL.createObjectURL(await res.blob()), name);
    toast('已导出：' + name);
  } catch (e) { toastErr('导出', e); }
}

async function galImportPacks(files) {
  const list = Array.from(files || []);
  if (!list.length) return;
  let ok = 0, lastId = '', skippedModel = false, speech = 0;
  for (const f of list) {
    try {
      const b64 = await fileToB64(f);
      const r = await api('/api/gal/character/import', { filename: f.name, data: b64 });
      ok++;
      if (r.character && r.character.id) lastId = r.character.id;
      if (r.skipped_voice_model) skippedModel = true;
      speech += (r.speech_files || 0);
    } catch (e) { toast('「' + f.name + '」导入失败：' + e, 'error'); }
  }
  if (ok) {
    await galLoadCharacters();
    if (lastId) await galSetCharacter(lastId);
    galToggleChars(true);
    toast('已导入 ' + ok + ' 个角色'
          + (speech ? '（含 ' + speech + ' 条原声台词）' : '')
          + (skippedModel ? '，已忽略体积较大的语音模型' : ''));
  }
}

// 批量导入 data/gal/packs/ 下的角色包（你手动下载的 .char 放这里即可）
async function galImportPacksDir() {
  try {
    const r = await api('/api/gal/character/import_dir', {});
    galState.characters = (r && r.characters) || galState.characters;
    galRenderCharBtn(); galRenderCharCards(); galRefreshPacksHint();
    const n = (r && r.imported) || 0;
    if (n) {
      if (r.ids && r.ids.length) await galSetCharacter(r.ids[r.ids.length - 1]);
      toast('已从 packs 目录导入 ' + n + ' 个角色');
    } else {
      toast('packs 目录里没有可导入的 .char / .zip');
    }
    if (r && r.failed && r.failed.length) toast('部分失败：' + r.failed.join('；'), 'error');
  } catch (e) { toastErr('批量导入', e); }
}

async function galRefreshPacksHint() {
  const el = $('#gal-packs-hint');
  if (!el) return;
  try {
    const r = await api('/api/gal/packs', {});
    const n = ((r && r.packs) || []).length;
    el.textContent = n
      ? ('data/gal/packs/ 里有 ' + n + ' 个待导入角色包，点「导入 packs 目录」即可')
      : '把社区下载的 .char / .zip 放到 data/gal/packs/，再点「导入 packs 目录」（会解出原版立绘、参考音频与原声台词）';
  } catch (e) { /* 忽略 */ }
}

// 立绘排位（百分比中心点）：1 张靠右、2 张左右、3 张左中右