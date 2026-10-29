'use strict';

/* ============================================================
   GAL 演出模式（见 docs/GAL_MODE_DESIGN.md）
   ============================================================ */
const galState = { characters: [], charId: '', char: null, assetCache: new Map(),
                   lastOptions: [], spriteIndex: null, ttsOn: false, audio: null, skip: false,
                   bgm: '', bgmAudio: null, node: null, statusTimer: null, statusBase: '',
                   cast: [], activeSprite: null, map: {},
                   ttsWarned: '', ttsQueue: [], ttsRunning: false, segIndex: 0, _audioDone: null,
                   pending: [], segBusy: false, typer: null, fxAudio: null, userEcho: '',
                   showQueue: [], showRunning: false, streamedTurn: false,
                   pendingOptions: null, pendingNudge: '', optSel: -1,
                   bgs: [], bgSel: '', bgDims: [], bgEditing: '',
                   bgms: [], bgmSel: '', bgmDims: [], bgmEditing: '', bgmPreview: null,
                   stats: [], statPrev: {},
                   fast: false, autoOverride: null,
                   // 生命周期：audioGen 是「代次号」——新一轮 / 切会话 / 切模式时自增，
                   // 所有异步的朗读、打字、等待都拿它判断「我还该不该继续」，避免旧回合串进新回合
                   audioGen: 0, autoTimer: null, bgTimer: null,
                   lineSkip: false, lineAdvance: false, _lineDone: null, _ttsWake: false,
                   _testAudio: null };

/* 新一轮 / 切会话 / 切模式：代次号 +1，让所有在跑的朗读/打字/等待立刻失效 */
function galBumpGen() { galState.audioGen += 1; return galState.audioGen; }

/* 统一停掉所有在响的音频 + 清朗读队列 + 清定时器。
   opts.bgm = true 时连 BGM 一起丢（切模式 / 切会话用；「停止生成」不动 BGM）。 */
function galStopAudioAll(opts) {
  const o = opts || {};
  galState.skip = true;                       // 本轮不再朗读
  galBumpGen();
  galState.ttsQueue.length = 0;
  galState.lineSkip = false;
  galState.lineAdvance = false;
  const finish = galState._audioDone;
  const lineDone = galState._lineDone;
  galState._audioDone = null;
  galState._lineDone = null;
  ['audio', 'fxAudio', '_testAudio'].forEach((k) => {
    const a = galState[k];
    if (a) { try { a.pause(); } catch (e) { /* 忽略 */ } }
    galState[k] = null;
  });
  if (o.bgm) {
    const b = galState.bgmAudio;
    if (b) { try { b.pause(); } catch (e) { /* 忽略 */ } }
    galState.bgmAudio = null;
    galState.bgm = '';
  }
  if (galState.autoTimer) { clearTimeout(galState.autoTimer); galState.autoTimer = null; }
  if (galState.bgTimer) { clearTimeout(galState.bgTimer); galState.bgTimer = null; }
  if (lineDone) { try { lineDone(); } catch (e) { /* 忽略 */ } }
  if (finish) { try { finish(); } catch (e) { /* 忽略 */ } }   // 让 galPlayUrl 的 await 立刻返回
  galSetSkipVisible(false);
}

// 释放立绘 / 背景 / BGM 的 Blob URL（长期不回收会一直占内存）
function galClearAssetCache() {
  (galState.assetCache || new Map()).forEach((u) => { try { URL.revokeObjectURL(u); } catch (e) { /* 忽略 */ } });
  galState.assetCache = new Map();
}
function galClearMediaCache() {
  galMediaCache.forEach((u) => { try { URL.revokeObjectURL(u); } catch (e) { /* 忽略 */ } });
  galMediaCache.clear();
}

// 立绘图片：带鉴权取回 → 缓存 Blob URL（token 不进 URL）
function galAssetUrl(charId, path) {
  const key = charId + '/' + path;
  if (galState.assetCache.has(key)) return Promise.resolve(galState.assetCache.get(key));
  return fetch('/api/gal/asset', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Auth-Token': TOKEN },
    body: JSON.stringify({ id: charId, path: path }),
  }).then((res) => {
    if (!res.ok) throw new Error('HTTP ' + res.status);
    return res.blob();
  }).then((blob) => {
    const url = URL.createObjectURL(blob);
    galState.assetCache.set(key, url);
    return url;
  });
}

async function galLoadCharacters() {
  try {
    const r = await api('/api/gal/characters', {});
    galState.characters = (r && r.characters) || [];
  } catch (e) { galState.characters = []; }
  galRenderCharBtn();
  galRenderCharCards();
}

// 工具条上的当前角色按钮（头像 + 名字）
function galRenderCharBtn() {
  const btn = $('#gal-char-btn');
  if (!btn) return;
  const c = galState.char;
  const nm = $('#gal-char-btn-name');
  if (nm) nm.textContent = c ? (c.name || c.id) : '选择角色';
  const av = $('#gal-char-btn-avatar');
  if (av) {
    const path = (c && (c.sprites || [])[0]) ? c.sprites[0].path : '';
    if (c && path) {
      av.style.display = '';
      galAssetUrl(c.id, path).then((u) => { av.src = u; })
        .catch(() => { av.removeAttribute('src'); av.style.display = 'none'; });
    } else { av.removeAttribute('src'); av.style.display = 'none'; }
  }
  if (c && c.color) btn.style.setProperty('--gal-char-color', c.color);
  else btn.style.removeProperty('--gal-char-color');
}

// 角色卡片面板（缩略图网格 + 搜索）
function galRenderCharCards() {
  const box = $('#gal-chars-list');
  if (!box) return;
  const q = (($('#gal-chars-search') || {}).value || '').trim().toLowerCase();
  const cur = galState.charId || '';
  const list = galState.characters.filter((c) =>
    !q || String(c.name || '').toLowerCase().includes(q) || String(c.id || '').toLowerCase().includes(q));
  const cnt = $('#gal-chars-count');
  if (cnt) cnt.textContent = list.length + ' / ' + galState.characters.length;
  box.innerHTML = '';
  if (!list.length) {
    const em = document.createElement('div');
    em.className = 'gal-chars-empty';
    em.textContent = galState.characters.length ? '没有匹配的角色' : '还没有角色：点「导入文件」或「新建」';
    box.appendChild(em);
    return;
  }
  list.forEach((c) => {
    // 卡片外层容器：让「编辑」按钮绝对定位在右上角，不再嵌进 <button>（交互元素不能嵌套）
    const wrap = document.createElement('div');
    wrap.className = 'gal-char-wrap';
    const card = document.createElement('button');
    card.className = 'gal-char-card' + (c.id === cur ? ' active' : '');
    card.title = '单击选中 · 点右上 ✎ 编辑';
    if (c.color) card.style.setProperty('--gal-char-color', c.color);
    const img = document.createElement('img');
    img.className = 'gal-char-thumb'; img.alt = '';
    if (c.default_sprite) {
      galAssetUrl(c.id, c.default_sprite).then((u) => { img.src = u; }).catch(() => { img.classList.add('empty'); });
    } else { img.classList.add('empty'); }
    const meta = document.createElement('div');
    meta.className = 'gal-char-meta';
    const nm = document.createElement('div'); nm.className = 'gal-char-name'; nm.textContent = c.name || c.id;
    const sub = document.createElement('div');
    sub.className = 'gal-char-sub';
    sub.textContent = (c.sprite_count || 0) + ' 张立绘';
    meta.appendChild(nm);
    meta.appendChild(sub);
    if (c.intro) {                              // 角色介绍（给玩家看的一段话）
      const it = document.createElement('div');
      it.className = 'gal-char-intro';
      it.textContent = c.intro;
      it.title = c.intro;
      meta.appendChild(it);
    }
    card.appendChild(img);
    card.appendChild(meta);
    card.onclick = () => { galSetCharacter(c.id, { showIntro: true }); galToggleChars(false); };
    // 右上角编辑入口：原先编辑按钮只埋在面板底部一排按钮里，已有角色很难被发现可以编辑
    const ed = document.createElement('button');
    ed.type = 'button';
    ed.className = 'gal-char-edit';
    ed.title = '编辑该角色';
    ed.setAttribute('aria-label', '编辑角色 ' + (c.name || c.id));
    ed.innerHTML = '<svg class="ic"><use href="#i-pencil"/></svg>';
    ed.onclick = (ev) => { ev.stopPropagation(); galOpenEditor(c.id, false); };
    wrap.appendChild(card);
    wrap.appendChild(ed);
    box.appendChild(wrap);
  });
}

function galToggleChars(force) {
  const p = $('#gal-chars');
  if (!p) return;
  const show = (typeof force === 'boolean') ? force : p.classList.contains('hidden');
  p.classList.toggle('hidden', !show);
  if (show) {
    galToggleBgs(false);        // 与背景面板同位置，避免叠在一起
    galRenderCharCards();
    galRefreshPacksHint();
    const s = $('#gal-chars-search');
    if (s) { s.value = ''; s.focus(); }
  }
}
