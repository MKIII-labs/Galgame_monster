const galMediaCache = new Map();
function galMediaUrl(kind, path) {
  const key = kind + '/' + path;
  if (galMediaCache.has(key)) return Promise.resolve(galMediaCache.get(key));
  return fetch('/api/gal/media', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Auth-Token': TOKEN },
    body: JSON.stringify({ kind: kind, path: path }),
  }).then((res) => { if (!res.ok) throw new Error('HTTP ' + res.status); return res.blob(); })
    .then((blob) => { const u = URL.createObjectURL(blob); galMediaCache.set(key, u); return u; });
}
function galSetBackground(path, persist) {
  if (persist) {          // 手动切换：记到会话，刷新 / 切会话后仍在
    const sid = state.sessions && state.sessions.current;
    if (sid) api('/api/gal/session', { id: sid, gal_bg: path || '' }).then(applySessions)
      .catch(() => { toast('背景已切换，但没能记进会话 —— 下次进入该会话会恢复原来的背景', 'error'); });
  }
  const cur = $('#gal-bg');
  const next = $('#gal-bg-next');
  if (!cur) return;
  if (!path) {
    cur.style.backgroundImage = '';
    if (next) next.style.backgroundImage = '';
    return;
  }
  galMediaUrl('bg', path).then((u) => {
    const img = 'url("' + u + '")';
    if (cur.style.backgroundImage === img) return;         // 同一张不重复淡入
    if (!next) { cur.style.backgroundImage = img; return; }
    next.style.backgroundImage = img;
    next.style.opacity = '1';
    if (galState.bgTimer) { clearTimeout(galState.bgTimer); galState.bgTimer = null; }
    galState.bgTimer = setTimeout(() => {                  // 交叉淡入：新图淡入后再换到底层
      galState.bgTimer = null;
      cur.style.backgroundImage = img;
      next.style.opacity = '0';
    }, 480);
  }).catch(() => { toast('背景图读取失败：文件可能已被删除，或不在 data/gal/backgrounds/ 里', 'error'); });
}
// BGM 音量（0–1）：读设置里的 gal_bgm_volume（默认 0.5）
function galBgmVolume() {
  const v = Number(galCfg('gal_bgm_volume', 0.5));
  return (isFinite(v) && v >= 0) ? Math.max(0, Math.min(1, v)) : 0.5;
}

// 线性淡入 / 淡出（直接改 audio.volume，不依赖 Web Audio）
function galFadeAudio(audio, to, ms, onDone) {
  if (!audio) { if (onDone) onDone(); return; }
  const from = (typeof audio.volume === 'number') ? audio.volume : 0;
  const steps = 12;
  const dt = Math.max(16, Math.floor(ms / steps));
  let i = 0;
  const t = setInterval(() => {
    i++;
    try { audio.volume = Math.max(0, Math.min(1, from + (to - from) * (i / steps))); } catch (e) { /* 忽略 */ }
    if (i >= steps) { clearInterval(t); if (onDone) onDone(); }
  }, dt);
}

function galSetBgm(path) {
  const p = path || '';
  if (galState.bgm === p) return;
  galState.bgm = p;
  const old = galState.bgmAudio;
  galState.bgmAudio = null;
  if (old) galFadeAudio(old, 0, 550, () => { try { old.pause(); } catch (e) { /* 忽略 */ } });
  if (!p) return;
  galMediaUrl('bgm', p).then((u) => {
    if (galState.bgm !== p) return;                 // 期间又切了，丢弃这次
    const a = new Audio(u);
    a.loop = true;
    a.volume = 0;
    galState.bgmAudio = a;
    const target = galBgmVolume();
    const fadeIn = () => galFadeAudio(a, target, 900);
    a.play().then(fadeIn).catch(() => {
      // 刷新 / 切会话后浏览器禁止「无交互自动播放」：挂一次性手势监听，用户第一次点/按键时续播
      const retry = () => {
        document.removeEventListener('pointerdown', retry);
        document.removeEventListener('keydown', retry);
        if (galState.bgmAudio === a && galState.bgm) a.play().then(fadeIn).catch(() => { /* 仍失败就算了 */ });
      };
      document.addEventListener('pointerdown', retry);
      document.addEventListener('keydown', retry);
    });
  }).catch(() => { /* 忽略 */ });
}

// 切换 / 停止 BGM 并记到会话（刷新、切会话后仍在）
function galSetBgmPersist(path) {
  const sid = state.sessions && state.sessions.current;
  if (sid) api('/api/gal/session', { id: sid, gal_bgm: path || '' }).then(applySessions).catch(() => {});
  galSetBgm(path || '');
}

// 设置里改了音量 → 实时应用到正在播放的 BGM
function galApplyBgmVolume() {
  const a = galState.bgmAudio;
  if (a) { try { a.volume = galBgmVolume(); } catch (e) { /* 忽略 */ } }
}

/* ---- 台词朗读（TTS / 立绘绑定音频） ---- */
function galCurrentSprite() {
  const sprites = (galState.char && galState.char.sprites) || [];
  const i = galState.spriteIndex;
  return (typeof i === 'number' && sprites[i]) ? sprites[i] : null;
}
function galSyncTtsBtn() {
  const b = $('#gal-tts');
  if (b) b.classList.toggle('active', !!galState.ttsOn);
}
async function galToggleTts() {
  galState.ttsOn = !galState.ttsOn;
  galSyncTtsBtn();
  try {
    const cfg = Object.assign({}, state.config, { tts_enabled: galState.ttsOn });
    await api('/api/config', cfg);
    state.config.tts_enabled = galState.ttsOn;
  } catch (e) { /* 忽略 */ }
  toast(galState.ttsOn ? '已开启台词朗读（需在设置里配置 TTS）' : '已关闭台词朗读');
}
// 「停止生成」：停掉朗读与特效音、清定时器，但不动 BGM（BGM 是舞台背景，不该被对话停止影响）
function galStopAudio() { galStopAudioAll(); }
function galSkipSpeech() { galStopAudio(); toast('已跳过本轮朗读'); }
function galPlayUrl(url, gen) {
  return new Promise((resolve) => {
    if (!url || galState.skip || galState.lineSkip ||
        (gen !== undefined && gen !== galState.audioGen)) return resolve();
    const a = new Audio(url);
    galState.audio = a;
    const done = () => {
      if (galState.audio === a) galState.audio = null;
      if (galState._audioDone === done) galState._audioDone = null;
      resolve();
    };
    galState._audioDone = done;
    a.onended = done;
    a.onerror = done;
    a.play().catch(done);
  });
}
// 试听（设置里的 TTS 测试）：独立播放，不碰本轮朗读状态，也不受「跳过朗读 / 代次号」影响
function galPlayTestUrl(url) {
  return new Promise((resolve) => {
    if (!url) return resolve();
    const prev = galState._testAudio;
    if (prev) { try { prev.pause(); } catch (e) { /* 忽略 */ } }
    const a = new Audio(url);
    galState._testAudio = a;
    const done = () => {
      if (galState._testAudio === a) galState._testAudio = null;
      try { URL.revokeObjectURL(url); } catch (e) { /* 忽略 */ }
      resolve();
    };
    a.onended = done;
    a.onerror = done;
    a.play().catch(done);
  });
}

/* 朗读前清洗文本：去掉代码、*动作*、（动作）/（颜文字）、表情与装饰符号
   （参考原模组 Shinsekai 的 TextProcessor.remove_parentheses） */
function galCleanSpeech(text) {
  let s = String(text || '');
  s = s.replace(/```[\s\S]*?```/g, ' ');                 // 围栏代码块
  s = s.replace(/`[^`\n]*`/g, ' ');                       // 行内代码
  s = s.replace(/\$\$[\s\S]*?\$\$/g, ' ');                // 公式（不朗读 LaTeX）
  s = s.replace(/\\\[[\s\S]*?\\\]/g, ' ');
  s = s.replace(/\\\([\s\S]*?\\\)/g, ' ');
  s = s.replace(/\$[^\n$]*\$/g, ' ');
  s = s.replace(/\*[^*\n]{0,40}\*/g, ' ');               // *动作 / 强调*
  s = s.replace(/[（(][^（）()\n]{0,40}[)）]/g, ' ');      // （动作）/（颜文字）
  s = s.replace(/[\u0300-\u036F\u200B-\u200F\u2028-\u202F\u20E3\uFE00-\uFE0F]/g, ''); // 组合/零宽/变体符
  s = s.replace(/[\u{1F000}-\u{1FAFF}\u{2190}-\u{2BFF}\u{2600}-\u{27BF}]/gu, ''); // emoji/装饰符号
  s = s.replace(/[\u0600-\u06FF\u0E00-\u0E7F\u1100-\u11FF\u3130-\u318F\uFFA0-\uFFDC]/g, ''); // 颜文字里常见的阿拉伯/泰/谚文字符
  s = s.replace(/\s{2,}/g, ' ').trim();
  return s;
}

/* 朗读切分：短句整段一次合成；超长才按标点切分（对齐原模组 tts_split_enabled 的默认不切） */
function galSplitSpeech(text, maxLen) {
  const t = String(text || '').trim();
  if (!t) return [];
  if (!maxLen || t.length <= maxLen) return [t];
  const parts = t.split(/(?<=[。！？!?；;…，,、])/).map((s) => s.trim()).filter(Boolean);
  const out = []; let cur = '';
  for (const p of parts) {
    if (!cur) cur = p;
    else if (cur.length + p.length <= maxLen) cur += p;
    else { out.push(cur); cur = p; }
  }
  if (cur) out.push(cur);
  return out.length ? out : [t];
}

async function galSpeak(text) {
  // 注意：不要在这里重置 skip —— 用户可能已在「生成中」按了「跳过朗读」，
  // 重置会让回复到达后照样出声。skip 只在「新的一轮开始时」清空。
  if (!galState.ttsOn || !text || galState.skip || galState.lineSkip) return;
  const gen = galState.audioGen;
  const spr = galCurrentSprite();
  if (state.config.gal_tts_priority !== 'tts' && spr && spr.voice_path) {
    try { await galPlayUrl(await galAssetUrl(galState.charId, spr.voice_path), gen); return; } catch (e) { /* 落到 TTS */ }
  }
  const clean = galCleanSpeech(text);
  if (!clean) return;                                   // 整句都是动作/颜文字 → 不朗读
  const maxLen = parseInt(state.config.tts_max_len, 10) || 80;
  const chunks = galSplitSpeech(clean, maxLen);
  const preset = (galState.char && galState.char.tts ? galState.char.tts.active : '') || '';
  let firstErr = '';
  // 合成单段 → 返回可播放的 blob URL（失败返回 null，并记录首个错误）
  const synth = async (piece) => {
    try {
      const res = await fetch('/api/gal/tts', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Auth-Token': TOKEN },
        body: JSON.stringify({ text: piece, character_id: galState.charId, preset: preset }),
      });
      if (!res.ok) {
        if (!firstErr) {
          try { firstErr = (await res.json()).error || ('HTTP ' + res.status); }
          catch (e) { firstErr = 'HTTP ' + res.status; }
        }
        return null;
      }
      return URL.createObjectURL(await res.blob());
    } catch (e) { if (!firstErr) firstErr = String(e); return null; }
  };
  let next = synth(chunks[0]);                          // 立即开始合成第一段
  for (let i = 0; i < chunks.length; i++) {
    if (galState.skip || galState.lineSkip || gen !== galState.audioGen) break;
    const url = await next;
    if (url && (gen !== galState.audioGen || galState.lineSkip || galState.skip)) {
      URL.revokeObjectURL(url); break;                  // 已开新一轮 / 用户跳过 → 不播，直接回收
    }
    if (i + 1 < chunks.length) next = synth(chunks[i + 1]);  // 边播边预取下一段，消除句间空档
    if (url) { try { await galPlayUrl(url, gen); } finally { URL.revokeObjectURL(url); } }
  }
  if (firstErr && galState.ttsWarned !== firstErr) {
    galState.ttsWarned = firstErr;
    toast('朗读失败：' + firstErr + '（可在「设置 → 语音输入」检查 TTS 配置）', 'error');
  }
}

/* 设置里的 TTS 试听与音色列表 */
async function galTestTts() {
  const val = (id) => { const el = $(id); return el ? el.value.trim() : ''; };
  const cfg = Object.assign({}, state.config, {
    tts_provider: val('#speech-tts-provider') || 'edge',
    tts_url: val('#speech-tts-url'),
    tts_api_key: val('#speech-tts-key'),
    tts_model: val('#speech-tts-model'),
    tts_voice: val('#speech-tts-voice'),
  });
  try { await api('/api/config', cfg); state.config = cfg; } catch (e) { /* 忽略 */ }
  toast('正在合成测试语音…');
  try {
    const res = await fetch('/api/gal/tts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Auth-Token': TOKEN },
      body: JSON.stringify({ text: '你好，这是台词朗读测试。', force: true }),
    });
    if (!res.ok) {
      let msg = 'HTTP ' + res.status;
      try { msg = (await res.json()).error || msg; } catch (e) { /* 忽略 */ }
      toast('合成失败：' + msg, 'error');
      return;
    }
    const url = URL.createObjectURL(await res.blob());
    toast('已开始播放测试语音');
    await galPlayTestUrl(url);
  } catch (e) { toastErr('测试', e); }
}

async function galLoadTtsVoices(silent) {
  try {
    const r = await api('/api/gal/tts/voices', {});
    const list = (r && r.voices) || [];
    const dl = $('#speech-tts-voice-list');
    if (dl) {
      dl.innerHTML = '';
      list.forEach((v) => { const o = document.createElement('option'); o.value = v.short_name; dl.appendChild(o); });
    }
    if (silent) return;
    if (!list.length) { toast('未能获取音色（Edge 服务不可达或未安装 edge-tts）', 'error'); return; }
    alert('Edge 中文音色（' + list.length + ' 个）：\n\n' + list.map((v) => v.short_name).join('\n') +
          '\n\n默认：' + ((r && r.default) || 'zh-CN-XiaoxiaoNeural'));
  } catch (e) { if (!silent) toast('获取音色失败：' + e, 'error'); }
}

/* ---- 角色的配音预设（TTS） ---- */