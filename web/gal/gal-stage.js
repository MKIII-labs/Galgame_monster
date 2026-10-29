const GAL_SLOTS = { 1: [80], 2: [30, 74], 3: [20, 50, 80] };
function galSlotCenters(n) {
  return GAL_SLOTS[Math.max(1, Math.min(3, n))] || GAL_SLOTS[1];
}
function galSpriteBox(multi) {
  const scale = (galState.char && galState.char.sprite_scale) || 1;
  return multi ? { w: 22, h: Math.round(54 * scale) } : { w: 28, h: Math.round(66 * scale) };
}

// 清空舞台上全部立绘（淡出后移除）
function galClearSprites() {
  const box = $('#gal-sprites');
  if (!box) return;
  galState.cast = [];
  galState.activeSprite = null;
  Array.from(box.querySelectorAll('img.gal-sprite')).forEach((el) => {
    el.style.opacity = '0';
    setTimeout(() => { if (el.parentNode) el.parentNode.removeChild(el); }, 380);
  });
}

// 设置舞台立绘：cast = 立绘编号数组（1 起）
//   null / undefined → 本条未声明，保持现状（交给单张逻辑）
//   []               → 显式清空舞台（安全剧情 / 纯叙述）
//   [1, 2, 3]        → 按编号显示多立绘
function galSetCast(list) {
  if (list === null || list === undefined) { galRenderSprites(); return; }
  const cast = list
    .map((n) => parseInt(n, 10))
    .filter((n) => Number.isFinite(n) && n >= 1);
  if (!cast.length) { galClearSprites(); return; }
  galState.cast = cast;
  galRenderSprites();
}

// 按 galState.cast 渲染多张立绘：自动排位，当前说话者高亮、其余变暗
function galRenderSprites() {
  const box = $('#gal-sprites');
  if (!box) return;
  const cast = galState.cast || [];
  const sprites = (galState.char && galState.char.sprites) || [];
  if (!cast.length) {           // 单张模式：清掉多余的，恢复默认尺寸（由 galShowSprite 管理那一张）
    const imgs = Array.from(box.querySelectorAll('img.gal-sprite'));
    imgs.slice(1).forEach((el) => el.remove());
    const only = imgs[0];
    if (only) {
      only.classList.remove('gal-dim');
      only.classList.add('gal-active');
      only.style.left = ''; only.style.right = ''; only.style.transform = '';
      only.style.maxWidth = ''; only.style.maxHeight = '';
      only.style.opacity = '1';
    }
    return;
  }
  const centers = galSlotCenters(cast.length);
  const size = galSpriteBox(cast.length > 1);
  const activeInCast = cast.some((n) => (n - 1) === galState.activeSprite);
  const want = {};
  cast.forEach((n) => { const sp = sprites[n - 1]; if (sp) want[galState.charId + '/' + sp.path] = true; });
  Array.from(box.querySelectorAll('img.gal-sprite')).forEach((el) => {   // 移除不在 cast 里的
    if (!want[el.dataset.key]) {
      el.style.opacity = '0';
      setTimeout(() => { if (el.parentNode) el.parentNode.removeChild(el); }, 380);
    }
  });
  cast.forEach((n, i) => {
    const sp = sprites[n - 1];
    if (!sp) return;
    const key = galState.charId + '/' + sp.path;
    const center = centers[i] != null ? centers[i] : 50;
    const isActive = !activeInCast || (galState.activeSprite === (n - 1));
    const apply = (el) => {
      el.style.left = center + '%';
      el.style.right = 'auto';
      el.style.transform = 'translateX(-50%)';
      el.style.maxWidth = size.w + '%';
      el.style.maxHeight = size.h + '%';
      el.classList.toggle('gal-active', isActive);
      el.classList.toggle('gal-dim', !isActive);
      el.style.opacity = '1';
    };
    const found = box.querySelector('img.gal-sprite[data-key="' + key + '"]');
    if (found) { apply(found); return; }
    galAssetUrl(galState.charId, sp.path).then((u) => {
      const el = document.createElement('img');
      el.className = 'gal-sprite';
      el.alt = '';
      el.dataset.key = key;
      el.style.opacity = '0';
      el.addEventListener('load', () => apply(el), { once: true });
      el.src = u;
      box.appendChild(el);
    }).catch(() => { /* 资源缺失就跳过这张 */ });
  });
}

function galShowSprite(path, index) {
  const box = $('#gal-sprites');
  if (!box) return;
  if (typeof index === 'number') galState.spriteIndex = index;
  // 多立绘模式：只切换「谁在说话」（高亮/变暗），不替换立绘
  if ((galState.cast || []).length) {
    if (typeof index === 'number') galState.activeSprite = index;
    galRenderSprites();
    return;
  }
  galState.activeSprite = (typeof index === 'number') ? index : null;
  if (!path) { box.innerHTML = ''; return; }
  const key = galState.charId + '/' + path;
  const cur = box.querySelector('img.gal-sprite');
  if (cur && cur.dataset.key === key) return;      // 同一张立绘：不重播动画
  galAssetUrl(galState.charId, path).then((u) => {
    const img = document.createElement('img');
    img.className = 'gal-sprite gal-active';
    img.alt = '';
    img.dataset.key = key;
    img.style.opacity = '0';
    img.style.transform = 'translateY(14px) scale(.985)';
    img.addEventListener('load', () => {
      img.style.opacity = '1';
      img.style.transform = 'none';
      // 旧立绘淡出后再移除 → 形成交叉过渡
      Array.from(box.querySelectorAll('img.gal-sprite')).forEach((el) => {
        if (el === img) return;
        el.style.opacity = '0';
        el.style.transform = 'translateY(10px)';
        setTimeout(() => { if (el.parentNode) el.parentNode.removeChild(el); }, 420);
      });
    }, { once: true });
    img.src = u;
    box.appendChild(img);
  }).catch(() => { /* 资源缺失时保持上一张 */ });
}

function galResetDialog() {
  galStopAudioAll();                                  // 停掉上一轮在响的朗读/特效音与定时器（不动 BGM）
  const t = $('#gal-text'); if (t) { t.textContent = ''; t.classList.remove('md'); }
  const n = $('#gal-name'); if (n) n.textContent = galState.char ? galState.char.name : '';
  const o = $('#gal-options'); if (o) { o.classList.remove('gal-options-out'); o.innerHTML = ''; }
  const tl = $('#gal-tools'); if (tl) tl.innerHTML = '';
  const gi = $('#gal-intro'); if (gi) { gi.classList.add('hidden'); gi.textContent = ''; }   // 新一轮：序章让位
  galState.segIndex = 0;
  galState.pending.length = 0;
  galState.pendingOptions = null;
  galState.pendingNudge = '';
  galHideNudge();                                     // 新一轮：上一轮那句「邀请你开口」要撤掉
  galState.showQueue.length = 0; galState.showRunning = false;
  galState.segBusy = false;
  galState.lineSkip = false;
  galState.lineAdvance = false;
  if (galState.typer) { galState.typer.finish(); galState.typer = null; }
  galThinkClear();
  galSetStatus('');
  galSetTyping(false);
}

/* 你上一句的回显（暗淡色）：让用户看到 AI 在回复什么 */
function galSetUserEcho(text) {
  const el = $('#gal-user-echo');
  const t = String(text || '').trim();
  galState.userEcho = t;
  if (!el) return;
  el.classList.remove('draft');            // 已发送 → 不再是草稿态
  if (!t) { el.classList.add('hidden'); el.textContent = ''; return; }
  el.classList.remove('hidden');
  el.innerHTML = '';
  const who = document.createElement('span');
  who.className = 'gal-user-echo-who';
  who.textContent = '你：';
  el.appendChild(who);
  el.appendChild(document.createTextNode(t));
}

/* 输入草稿实时变「剧中台词」：把正在打的字放到对白区顶部（复用「上一句回显」这块位置），
   以主角台词 + 闪烁光标呈现 —— 让玩家看到「我说的话正在进入故事」。
   草稿清空后自动退回显示上一句回显（galState.userEcho）。 */
function galSyncDraft(text) {
  if (state.uiMode !== 'gal') return;
  const el = $('#gal-user-echo');
  if (!el) return;
  const t = String(text || '').trim();
  if (!t) {                                          // 草稿空了 → 退回回显
    el.classList.remove('draft');
    if (galState.userEcho) galSetUserEcho(galState.userEcho);
    else { el.classList.add('hidden'); el.textContent = ''; }
    return;
  }
  if (t === galState.userEcho) { el.classList.remove('draft'); return; }   // 与已发送那句相同 → 当回显
  el.classList.remove('hidden');
  el.classList.add('draft');
  el.innerHTML = '';
  const who = document.createElement('span');
  who.className = 'gal-user-echo-who';
  who.textContent = '你：';
  el.appendChild(who);
  el.appendChild(document.createTextNode(t));
  // 对白很长时草稿会被滚出视野 → 只把它拉回可见（不硬拉到顶，免得打断正在读的段落）
  const body = document.querySelector('#gal-dialog .gal-dialog-body');
  if (body && el.offsetTop < body.scrollTop) body.scrollTop = Math.max(0, el.offsetTop - 8);
}

/* 「说出口」的确认反馈（200ms 内）：发出去的瞬间让对白框轻轻「吸」一下，
   表示这句话已经被剧情接住了；紧接着 galSetStatus 会显示「思考中…」。
   用对白框而不是草稿行做动画 —— 草稿行马上会被「你：…」回显替换，动画会看不出来。 */
function galPlayIntake() {
  const d = $('#gal-dialog');
  if (!d) return;
  d.classList.remove('gal-intake');
  void d.offsetWidth;                                // 重启动画
  d.classList.add('gal-intake');
  setTimeout(() => d.classList.remove('gal-intake'), 460);
}

/* ---- 演出配置读取 ---- */
function galCfg(k, dflt) {
  const v = (state.config || {})[k];
  return (v === undefined || v === null) ? dflt : v;
}
function galSegMode() { return galCfg('gal_seg_mode', 'append') === 'single' ? 'single' : 'append'; }
function galMinShowMs() {
  const v = parseFloat(galCfg('gal_min_show', 0.6));
  return (isFinite(v) && v > 0 ? v : 0) * 1000;
}

/* ---- 逐段对白：追加 or 单行推进；打字机；关键词特效 ---- */
function galBuildSegEl(obj) {
  const t = $('#gal-text');
  if (!t) return null;
  t.classList.remove('md');
  const isNarr = !!obj.narration;
  const raw = isNarr ? ('（' + obj.narration + '）') : (obj.text || '');
  if (!raw) return null;
  const seg = document.createElement('div');
  seg.className = 'gal-seg' + (isNarr ? ' narr' : '');
  const nm = String(obj.name || '').trim();
  const sp = String(obj.speaker || '').trim();
  if (!isNarr && sp && sp !== nm) {                 // 换了说话人时才标名字
    const who = document.createElement('span');
    who.className = 'gal-seg-who';
    who.textContent = sp + '：';
    seg.appendChild(who);
  }
  const body = document.createElement('span');
  body.className = 'gal-seg-text';
  body.textContent = raw;
  seg.appendChild(body);
  return { el: seg, body: body, text: raw, narr: isNarr };
}

// 对白框（#gal-dialog）才是真正的滚动容器；输出 / 打字过程中保持贴底
function galScrollDialog() {
  const b = document.querySelector('#gal-dialog .gal-dialog-body');
  if (b) { b.scrollTop = b.scrollHeight; return; }
  const d = $('#gal-dialog');
  if (d) d.scrollTop = d.scrollHeight;
}

// 段落打完字后把 Markdown 渲染出来：流式期间是纯文本（打字机没法逐字处理标记），
// 打完这一句就即时排版 —— 避免「只有刷新后才好看」的前后不一致
function galFinalizeSeg(seg) {
  if (!seg || !seg.body || !seg.text) return;
  try { seg.body.innerHTML = safeRenderMarkdown(seg.text); } catch (e) { /* 渲染失败就保持纯文本 */ }
  galScrollDialog();
}

// 立绘层底边 = 对白框顶边 + 间距 → 立绘永远不会被对白框 / 选项压住
function galSyncSpriteLift() {
  const stage = document.querySelector('#gal-panel .gal-stage');
  const d = $('#gal-dialog');
  if (!stage || !d) return;
  const lift = stage.getBoundingClientRect().bottom - d.getBoundingClientRect().top + 10;
  stage.style.setProperty('--gal-sprite-lift', Math.max(0, Math.round(lift)) + 'px');
}

// 打字机：逐字显示；点击可提前补全（返回 Promise，resolve 表示显示完成）
function galTypewrite(body, text) {
  return new Promise((resolve) => {
    const cps = Math.max(8, parseInt(galCfg('gal_cps', 12), 10) || 12);
    if (galCfg('gal_typewriter', true) === false || cps >= 200 || !text) {
      body.textContent = text; galScrollDialog(); galSyncAdvHint(); resolve(); return;
    }
    const chars = Array.from(text);
    const perChar = 1000 / cps;                    // 每字毫秒：速度设置全程线性生效
    let shown = 0, carry = 0, last = performance.now();
    body.textContent = '';
    const h = { timer: null, finish: null };
    h.finish = () => {
      clearInterval(h.timer);
      body.textContent = text;
      galScrollDialog();
      if (galState.typer === h) galState.typer = null;
      galSyncAdvHint();
      resolve();
    };
    h.timer = setInterval(() => {
      const now = performance.now();
      carry += Math.min(now - last, 100) * (galState.fast ? 6 : 1);   // 按住 Ctrl：快进；单帧最多补 100ms，防切页回来一次性灌满
      last = now;
      const add = Math.floor(carry / perChar);
      if (add <= 0) return;
      carry -= add * perChar;
      shown = Math.min(chars.length, shown + add);
      body.textContent = chars.slice(0, shown).join('');
      galScrollDialog();
      if (shown >= chars.length) h.finish();
    }, 16);
    galState.typer = h;
    galSyncAdvHint();
  });
}

/* 推进指示器（对话框右下角 ▼）：
   有对白 → 显示并闪烁；打字中 → 文案换成「点击补全」；生成中（状态提示可见）→ 隐藏 */
function galSyncAdvHint() {
  const h = $('#gal-adv-hint');
  if (!h) return;
  const txt = $('#gal-text');
  const hasText = !!(txt && (txt.textContent || '').trim());
  const st = $('#gal-status');
  const busy = !!(st && !st.classList.contains('hidden'));
  const typing = !!galState.typer;
  h.classList.toggle('show', state.uiMode === 'gal' && hasText && (!busy || typing));
  h.classList.toggle('is-typing', typing);
}

// 关键词特效：CSS 动画 + 可选音效（before/after/loop/stop）
function galPlayFx(list) {
  if (!list || !list.length) return;
  if (galCfg('gal_fx', true) === false) return;
  const layer = $('#gal-fx-layer');
  // 震动 / 淡入同时作用在「舞台 + 输入区」，让输入框那一块跟着一起动
  const targets = [document.querySelector('#gal-panel .gal-stage'),
                   document.querySelector('.composer')].filter(Boolean);
  list.forEach((fx) => {
    if (fx.css === 'flash' || fx.css === 'blackout') {
      if (layer) { layer.className = ''; void layer.offsetWidth; layer.className = 'fx-' + fx.css; }
    } else if (fx.css) {
      const cls = 'fx-' + fx.css;
      targets.forEach((el) => {
        el.classList.remove(cls); void el.offsetWidth; el.classList.add(cls);
        setTimeout(() => el.classList.remove(cls), 900);
      });
    }
    if (fx.audio) galPlayFxAudio(fx);
  });
}
function galPlayFxAudio(fx) {
  const stop = () => { if (galState.fxAudio) { try { galState.fxAudio.pause(); } catch (e) { /* 忽略 */ } galState.fxAudio = null; } };
  if (fx.mode === 'stop') { stop(); return; }
  fetch('/api/gal/effect/audio', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Auth-Token': TOKEN },
    body: JSON.stringify({ path: fx.audio }),
  }).then((r) => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.blob(); })
    .then((b) => {
      if (fx.mode === 'loop') stop();
      const u = URL.createObjectURL(b);
      const a = new Audio(u);
      if (fx.mode === 'loop') a.loop = true;
      galState.fxAudio = a;                 // 非循环音效也登记，「停止」时才能一并打断
      const cleanup = () => {
        URL.revokeObjectURL(u);             // 播完即回收，别让 Blob 一直挂着
        if (galState.fxAudio === a) galState.fxAudio = null;
      };
      a.onended = cleanup;
      a.onerror = cleanup;
      a.play().catch(cleanup);
    }).catch(() => { /* 音效缺失时静默 */ });
}

function galAddSegment(obj) {
  if (galSegMode() === 'single') { galState.pending.push(obj); galShowNext(); return; }
  galState.showQueue.push(obj);
  if (!galState.showRunning) galPumpShow();
}

// 追加模式：排队逐段显示——每段打字完再出下一段（不再瞬间补全上一段）
async function galPumpShow() {
  if (galState.showRunning) return;
  const gen = galState.audioGen;
  galState.showRunning = true;
  try {
    while (galState.showQueue.length && gen === galState.audioGen) {
      const obj = galState.showQueue.shift();
      const seg = galBuildSegEl(obj);
      if (!seg) continue;
      const t = $('#gal-text');
      if (!t) break;
      t.appendChild(seg.el);
      galScrollDialog();
      galState.segIndex = obj.index || (galState.segIndex + 1);
      if (obj.path) galShowSprite(obj.path, typeof obj.sprite_index === 'number' ? obj.sprite_index : undefined);
      if (obj.fx && obj.fx.length) galPlayFx(obj.fx);
      await galTypewrite(seg.body, seg.text);
      galFinalizeSeg(seg);                    // 打完这一句就排版（不用等刷新）
      // 打字期间若开了新一轮（代次号变了），这一段属于旧回合，不能再朗读
      if (gen !== galState.audioGen) break;
      if (!seg.narr && obj.text) galEnqueueSpeech(obj.text);
    }
  } finally {
    galState.showRunning = false;
    if (gen === galState.audioGen) galFlushOptions();   // 旧回合收尾时不要动新一轮的选项
  }
}

// 单行推进：一次只显示一句，点击 / 语音播完（自动播放）切下一句
async function galShowNext() {
  if (galState.segBusy) return;
  const obj = galState.pending.shift();
  if (!obj) return;
  const t = $('#gal-text');
  if (!t) return;
  const gen = galState.audioGen;
  galState.segBusy = true;
  galState.lineSkip = false;                 // 每句开始：清掉上一句的「跳过」标记
  galState.lineAdvance = false;
  t.innerHTML = '';
  t.classList.remove('md');
  const seg = galBuildSegEl(obj);
  if (!seg) { galState.segBusy = false; galShowNext(); return; }
  t.appendChild(seg.el);
  galScrollDialog();
  galState.segIndex = obj.index || (galState.segIndex + 1);
  if (obj.path) galShowSprite(obj.path, typeof obj.sprite_index === 'number' ? obj.sprite_index : undefined);
  const startedAt = Date.now();
  await galTypewrite(seg.body, seg.text);
  galFinalizeSeg(seg);                       // 打完这一句就排版（不用等刷新）
  if (gen !== galState.audioGen) { galState.segBusy = false; return; }   // 已开新一轮
  if (obj.fx && obj.fx.length) galPlayFx(obj.fx);
  if (!seg.narr && obj.text && galState.ttsOn && !galState.skip) await galSpeak(obj.text);
  const rest = galState.fast ? 0 : Math.max(0, galMinShowMs() - (Date.now() - startedAt));
  if (rest > 0 && !galState.lineSkip) {
    // 记下等待的 resolve：点击时可以立刻跳过，不必等满最短显示时间
    await new Promise((resolve) => {
      galState._lineDone = resolve;
      galState.autoTimer = setTimeout(() => {
        galState.autoTimer = null; galState._lineDone = null; resolve();
      }, rest);
    });
  }
  if (galState.autoTimer) { clearTimeout(galState.autoTimer); galState.autoTimer = null; }
  galState._lineDone = null;
  galState.segBusy = false;
  if (gen !== galState.audioGen) return;
  if (galState.lineAdvance) { galState.lineAdvance = false; galShowNext(); return; }  // 用户点了「跳过这句」
  // 自动播放：本句播完后再等 1.5s 才切下一句，给读者留出喘息
  if (galAutoOn() && galState.pending.length && !galState.skip) {
    galState.autoTimer = setTimeout(() => { galState.autoTimer = null; galShowNext(); }, 1500);
  } else {
    galFlushOptions();                  // 本句是最后一句且非自动播放 → 显示暂存的选项
  }
}

// 单行模式：跳过「当前这句」的朗读与最短显示等待，并立刻切下一句
function galSkipCurrentLine() {
  if (!galState.segBusy) return;
  galState.lineSkip = true;
  galState.lineAdvance = true;
  const ld = galState._lineDone;
  if (ld) { galState._lineDone = null; try { ld(); } catch (e) { /* 忽略 */ } }
  if (galState.autoTimer) { clearTimeout(galState.autoTimer); galState.autoTimer = null; }
  const f = galState._audioDone;
  if (f) {
    galState._audioDone = null;
    const a = galState.audio;
    if (a) { try { a.pause(); } catch (e) { /* 忽略 */ } }
    galState.audio = null;
    try { f(); } catch (e) { /* 忽略 */ }
  }
}

// 推进：打字中 → 立即补全；朗读/等待中 → 跳过这句；否则单行模式切下一句
function galAdvance() {
  if (galState.typer) { galState.typer.finish(); return; }
  if (galSegMode() !== 'single') return;
  if (galState.segBusy) { galSkipCurrentLine(); return; }
  if (galState.pending.length) galShowNext();
}

// 朗读队列：追加模式下按段顺序播，段与段之间不重叠
function galEnqueueSpeech(text) {
  if (!galState.ttsOn || galState.skip || !text) return;
  galState.ttsQueue.push(text);
  galState._ttsWake = true;               // 让正在跑的 pump 注意到有新内容
  if (!galState.ttsRunning) galTtsPump();
}
function galSetSkipVisible(on) {
  const b = $('#gal-skip');
  if (b) b.classList.toggle('hidden', !on);
}

/* ---- 自动播放（工具条「自动播放」/ A 键）---- */
function galAutoOn() {
  return (galState.autoOverride == null) ? !!galCfg('gal_auto', false) : !!galState.autoOverride;
}
function galSyncAutoBtn() {
  const b = $('#gal-auto-btn');
  if (b) b.classList.toggle('active', galAutoOn());
}
function galToggleAuto() {
  galState.autoOverride = !galAutoOn();
  galSyncAutoBtn();
  toast('自动播放：' + (galState.autoOverride ? '开' : '关'));
}

/* ---- GAL 演出快捷键 ----
   默认：空格 / 回车推进（打字中先补全）；按住 Ctrl 快进；A 自动播放；↑ 打开回顾。
   具体键位统一在 hotkeys.js 的 HK_DEFS 里登记，用户可在「?」清单里改。 */
// 焦点是否在「正在输入」的元素上（输入框 / 文本域 / 可编辑区 / 按钮）
function isTypingTarget(el) {
  const t = el || document.activeElement;
  return !!(t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable));
}
function galHotkeys(e) {
  if (state.uiMode !== 'gal') return;
  const t = e.target;
  if (isTypingTarget(t)) return;
  // 任一弹窗打开时都不响应（原先只挡了设置与角色编辑器，其它弹窗会误推进）
  if (document.querySelector('.modal:not(.hidden)')) return;
  // 有选项时：方向键 / 回车 / 空格 / 数字键都归「选项」，不再推进对话
  // （传统日式 galgame 的规矩：出现选择肢就先做选择，选完才继续）
  const opts = galOptionEls();
  if (opts.length) {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault(); galOptionMove(e.key === 'ArrowDown' ? 1 : -1); return;
    }
    if ((e.key === 'Enter' || e.key === ' ') && !e.ctrlKey && !e.metaKey && !e.altKey) {
      e.preventDefault(); galOptionConfirm(); return;
    }
    // 数字键 1–9：直接选第 N 项（选项本身标着「1. xxx」序号，与鼠标点击同一条路径）。
    // 序号按**显示顺序**取（不过滤 disabled），否则禁用项后面的序号会错位。
    if (/^[1-9]$/.test(e.key) && !e.ctrlKey && !e.metaKey && !e.altKey) {
      const idx = Number(e.key) - 1;
      if (idx < opts.length && !opts[idx].disabled) {
        e.preventDefault(); galUiSound('confirm'); opts[idx].click();
      }
      return;
    }
  }
  if (t && t.tagName === 'BUTTON') return;      // 焦点在按钮上（如选项）→ 空格/回车交给按钮本身激活
  // 其余键走统一快捷键表（可在「?」清单里改键）
  const hit = hkHit(e, ['gal']);
  if (!hit) return;
  if (hit.id === 'gal.advance') { e.preventDefault(); galAdvance(); return; }
  if (hit.id === 'gal.review') {
    const rp = $('#gal-review-panel');
    if (rp && rp.classList.contains('hidden')) { e.preventDefault(); galToggleReview(true); }
    return;
  }
  if (hit.id === 'gal.auto') { e.preventDefault(); galToggleAuto(); }
}
async function galTtsPump() {
  if (galState.ttsRunning) return;
  galState.ttsRunning = true;
  galSetSkipVisible(true);        // 正在朗读时才露出「跳过」
  try {
    // 外层循环处理「朗读过程中开了新一轮」：内层因代次号变化退出后，
    // 若期间又有新内容入队（_ttsWake），就用新的代次号再跑一遍
    while (galState._ttsWake) {
      galState._ttsWake = false;
      const gen = galState.audioGen;
      while (galState.ttsQueue.length && !galState.skip && gen === galState.audioGen) {
        await galSpeak(galState.ttsQueue.shift());
      }
    }
  } finally {
    galState.ttsRunning = false;
    galSetSkipVisible(false);
  }
}

function galRenderOptions(options) {
  const box = $('#gal-options');
  if (!box) return;
  galState.lastOptions = (options || []).slice();
  galState.optSel = -1;                           // 键盘光标：等玩家按 ↑↓ 才出现
  box.classList.remove('gal-options-out');
  box.innerHTML = '';
  galState.lastOptions.forEach((op, i) => {
    const b = document.createElement('button');
    b.className = 'gal-option';
    b.style.animationDelay = (i * 45) + 'ms';     // 逐条错开淡入
    const text = (i + 1) + '. ' + ((op && op.text) || '');
    const meta = !!(op && (op.tag || op.hint));
    if (meta) {                                   // 带标签 / 提示：换成可换行的布局
      b.classList.add('has-meta');
      const t = document.createElement('span');
      t.className = 'gal-option-text';
      t.textContent = text;
      b.appendChild(t);
      if (op.tag) {
        const g = document.createElement('span');
        g.className = 'gal-option-tag';
        g.textContent = op.tag;
        b.appendChild(g);
      }
      if (op.hint) {
        const h = document.createElement('span');
        h.className = 'gal-option-hint';
        h.textContent = op.hint;
        b.appendChild(h);
      }
    } else {
      b.textContent = text;
    }
    if (op && op.ok === false) {                  // 当前条件不满足 → 置灰不可点
      b.classList.add('disabled');
      b.disabled = true;
      b.title = op.hint || '条件不满足';
    } else {
      b.onclick = () => galSendUserText((op && op.text) || '');
    }
    b.onmouseenter = () => { if (!b.disabled) { galOptionSelect(i); galUiSound('hover'); } };
    box.appendChild(b);
  });
}

/* ---- 选项键盘光标（传统 galgame：↑↓ 移动、Enter / 空格 确认）---- */
function galOptionEls() {
  const box = $('#gal-options');
  if (!box || box.classList.contains('gal-options-out')) return [];
  return Array.from(box.querySelectorAll('.gal-option'));
}
function galOptionSelect(i) {
  const els = galOptionEls();
  if (!els.length) return;
  const n = els.length;
  galState.optSel = ((i % n) + n) % n;
  els.forEach((el, k) => el.classList.toggle('kb-sel', k === galState.optSel));
}
// 移动光标：自动跳过 disabled 项（最多绕一圈，避免全禁用时死循环）
function galOptionMove(dir) {
  const els = galOptionEls();
  if (!els.length) return;
  const n = els.length;
  let i = galState.optSel < 0 ? (dir > 0 ? -1 : 0) : galState.optSel;
  for (let step = 0; step < n; step++) {
    i = ((i + dir) % n + n) % n;
    if (!els[i].disabled) break;
  }
  galOptionSelect(i);
  galUiSound('hover');
}
function galOptionConfirm() {
  const els = galOptionEls();
  if (!els.length) return false;
  const el = els[galState.optSel >= 0 ? galState.optSel : 0];
  if (!el || el.disabled) return false;
  galUiSound('confirm');
  el.click();
  return true;
}

/* ---- 界面音效：WebAudio 现场合成，不依赖音频文件 ----
   浏览器要求先有用户手势才能出声；悬停 / 点击本身就是手势，所以正常可用。
   如果没听到，多半是系统或标签页静音，不影响功能。 */
let _galSfxCtx = null;
function galUiSound(kind) {
  if (galCfg('gal_ui_sfx', true) === false) return;
  try {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return;
    if (!_galSfxCtx) _galSfxCtx = new AC();
    const ctx = _galSfxCtx;
    if (ctx.state === 'suspended') ctx.resume();
    const conf = kind === 'confirm';
    const t0 = ctx.currentTime;
    const o = ctx.createOscillator();
    const g = ctx.createGain();
    o.type = conf ? 'triangle' : 'sine';
    o.frequency.setValueAtTime(conf ? 880 : 1320, t0);
    if (conf) o.frequency.exponentialRampToValueAtTime(520, t0 + 0.12);
    g.gain.setValueAtTime(0.0001, t0);
    g.gain.exponentialRampToValueAtTime(conf ? 0.16 : 0.05, t0 + 0.008);
    g.gain.exponentialRampToValueAtTime(0.0001, t0 + (conf ? 0.16 : 0.07));
    o.connect(g); g.connect(ctx.destination);
    o.start(t0); o.stop(t0 + (conf ? 0.2 : 0.09));
  } catch (e) { /* 忽略 */ }
}

// 选项延迟显示：等本轮「流结束 + 没有在打字 + 待显示队列为空」才真正渲染，
// 否则用户会点到还没就绪的选项（点击被 state.streaming 吞掉，表现为"点了没反应、选项还浮着"）。
// 本轮确实没有选项时，改成在段末给一句「邀请玩家开口」的沉浸式提示（见 galShowNudge）。
function galFlushOptions() {
  if (galState.pendingOptions && galState.pendingOptions.length) {
    if (state.streaming || galState.segBusy || galState.pending.length) return;
    const opts = galState.pendingOptions;
    galState.pendingOptions = null;
    galRenderOptions(opts);
    galHideNudge();                 // 有选项就不用再提示「你可以开口」
    return;
  }
  galShowNudge();
}

/* ---- 「轮到你了」提示：本轮没有选项时，最后一段对白末尾给一句邀请 ----
   文案优先用模型给的 nudge（贴合角色口吻与当前场景）；没给就用本地极克制的旁白体兜底。 */
function galShowNudge() {
  if (galCfg('gal_nudge', true) === false) return;
  if (state.uiMode !== 'gal') return;
  if (state.streaming || galState.segBusy || galState.pending.length) return;  // 还没播完
  if (galState.pendingOptions && galState.pendingOptions.length) return;       // 还有待显示选项
  if (galOptionEls().length) return;                                          // 选项已经渲染出来
  const t = $('#gal-text');
  if (!t || !(t.textContent || '').trim()) return;                            // 没有对白 → 不提示
  const box = $('#gal-nudge');
  if (!box) return;
  const el = $('#gal-nudge-text');
  if (el) el.textContent = galState.pendingNudge || '（也可以直接说点什么）';
  box.classList.remove('hidden');
}
function galHideNudge() {
  const box = $('#gal-nudge');
  if (box) box.classList.add('hidden');
}

// 选项区淡出：动画结束（或 reduced-motion 立即）后清空，然后回调 done（保证只调一次）
function galHideOptions(done) {
  const box = $('#gal-options');
  let closed = false;
  const finish = () => { if (!closed) { closed = true; if (typeof done === 'function') done(); } };
  if (!box || !box.childElementCount) { if (box) box.innerHTML = ''; finish(); return; }
  const onEnd = (e) => { if (e.target === box) clear(); };   // 只认容器自身的淡出（子按钮也有动画）
  const clear = () => {
    box.removeEventListener('animationend', onEnd);
    box.classList.remove('gal-options-out');
    box.innerHTML = '';
    finish();
  };
  box.classList.add('gal-options-out');
  box.addEventListener('animationend', onEnd);
  setTimeout(clear, 400);                       // 兜底：动画事件未触发时也要清掉
}

function galSendUserText(text) {
  if (!text || state.streaming) return;
  const instant = !(state.config && state.config.gal_options_instant === false);
  const input = $('#input');
  if (input) input.value = text;
  // 先让选项淡出，再真正发送：sendMessage → galResetDialog 会立刻清空选项区，
  // 若同步发送就会把淡出动画掐掉（看起来还是"瞬间消失"）。
  galHideOptions(() => {
    if (instant) { galSetStatus('思考中…'); sendMessage(); }
    else { if (input) input.focus(); autoGrow(); syncSendBtn(); }
  });
}
