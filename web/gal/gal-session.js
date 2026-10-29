// 从会话历史渲染舞台（进入 GAL 模式 / 切换会话 / 一轮结束后）
async function galRenderFromSession() {
  const s = curSession();
  if (!s) return;
  if (s.gal_character_id && s.gal_character_id !== galState.charId) {
    await galSetCharacter(s.gal_character_id);   // 换角色后继续往下渲染（原来直接 return，切会话就不回显）
  }
  const msgs = s.messages || [];
  let last = null, lastUser = '';
  for (let i = msgs.length - 1; i >= 0; i--) {
    if (msgs[i].role === 'assistant') {
      last = msgs[i];
      for (let k = i - 1; k >= 0; k--) {          // 该回复之前最近的一条用户消息
        if (msgs[k].role === 'user') { lastUser = msgs[k].content || ''; break; }
      }
      break;
    }
  }
  galResetDialog();
  galSetUserEcho(lastUser);
  galRenderStats(s.gal_stats || []);      // 先渲染状态栏：空会话也要把它清掉，别留着上一个会话的
  galRenderIntro();                       // 空会话时把「角色介绍」当作序章显示
  if (!last) {                            // 空会话：上一个会话留在舞台上的背景 / 立绘也要清掉
    galSetBackground('');
    galSetCast([]);
    return;
  }
  const g = last.gal || null;
  // 舞台立绘：按**本会话**重建，别留着上一个会话的
  //   有 cast 声明 → 照它排位（[] 表示清空）；没有声明 → 回到单张模式（保留当前那张，
  //   具体是谁交给下面的 sprite_index），顺手清掉上一个会话多出来的立绘
  galState.activeSprite = (g && typeof g.sprite_index === 'number') ? g.sprite_index : null;
  if (!g) galSetCast([]);
  else if (Array.isArray(g.cast)) galSetCast(g.cast);
  else { galState.cast = []; galRenderSprites(); }
  const sprites = (galState.char && galState.char.sprites) || [];
  if (g && typeof g.sprite_index === 'number' && sprites[g.sprite_index]) {
    galShowSprite(sprites[g.sprite_index].path, g.sprite_index);
  }
  const tbox = $('#gal-text');
  if (tbox) {
    tbox.innerHTML = '';
    tbox.classList.add('md');
    const nm = (galState.char && galState.char.name) || '';
    const segs = (g && Array.isArray(g.dialog) && g.dialog.length) ? g.dialog : null;
    if (segs) {
      const list = (galSegMode() === 'single') ? segs.slice(-1) : segs;   // 单行推进只回显最后一句
      list.forEach((s) => {
        const isNarr = !s.speech && s.narration;
        const d = document.createElement('div');
        d.className = 'gal-seg' + (isNarr ? ' narr' : '');
        const sp = String(s.speaker || '').trim();
        if (!isNarr && sp && sp !== nm) {
          const who = document.createElement('span');
          who.className = 'gal-seg-who'; who.textContent = sp + '：';
          d.appendChild(who);
        }
        const body = document.createElement('span');
        body.className = 'gal-seg-text';
        body.innerHTML = safeRenderMarkdown(isNarr ? ('（' + s.narration + '）') : (s.speech || ''));
        d.appendChild(body);
        tbox.appendChild(d);
      });
    } else {   // 兼容旧消息（单个 speech 对象）
      let combined = '';
      if (g && g.narration) combined += '（' + g.narration + '）\n';
      combined += (last.content || '');
      tbox.innerHTML = safeRenderMarkdown(combined);
    }
    galScrollDialog();
  }
  // 按最后一段的立绘/情绪回填舞台
  if (g && Array.isArray(g.dialog)) {
    for (let i = g.dialog.length - 1; i >= 0; i--) {
      const si = g.dialog[i].sprite_index;
      if (typeof si === 'number' && sprites[si]) { galShowSprite(sprites[si].path, si); break; }
    }
  }
  if (g && (g.options || []).length) galRenderOptions(g.options);
  // 舞台（背景 / BGM）也按这一轮回填 —— 后端早就把 bg/bgm 存进 msg.gal 了，之前没读，刷新就丢
  if (g) {
    // 本轮没换背景时，回落到会话记住的背景（手动切换过）；两处都没有 → 传空串**清掉**，
    // 否则切到没有背景的会话时会一直留着上一个会话的背景
    galSetBackground(g.bg || s.gal_bg || '');
    // BGM：本条明确写了就切；明确 stop 就停；否则回落到会话记住的当前 BGM
    if (g.bgm) galSetBgm(g.bgm);              // 浏览器可能拒绝无交互播放，会等首次点击/按键续播
    else if (g.bgm_stop) galSetBgm('');
    else if (s.gal_bgm) galSetBgm(s.gal_bgm);
  } else {
    galSetBackground(s.gal_bg || '');         // 空会话：同样把上一个会话的背景清掉
    if (s.gal_bgm) galSetBgm(s.gal_bgm);
  }
  // 幕节点：切会话 / 刷新后也要更新，否则一直留着上一个会话的「第 N 幕」
  galSetNode(s.gal_node_id ? { node_id: s.gal_node_id, round: s.gal_round || 0, max_rounds: 0 } : null);
  // 回放该轮的工具卡片（GAL 模式下 Agent 的工具时间线显示在舞台上）
  const tools = last.tools || [];
  if (tools.length && galCfg('gal_show_exec', true) !== false) {
    const box = $('#gal-tools');
    if (box) {
      tools.forEach((t) => {
        const card = buildToolCard(t.tool, t.args);
        finishToolCard(card, { result: t.result, undo: t.undo, write: t.write });
        box.appendChild(card);
      });
    }
  }
  if (last.memory && last.memory.summary) galExecLine('记住了：' + last.memory.summary, 'ok');
  if (last.usage) galExecUsage(last.usage);
  galSyncAdvHint();          // 切会话 / 刷新后按已有对白重新决定 ▼ 是否露出
  galHideNudge();            // 「轮到你了」提示不跨会话保留（回看历史时不该催你说话）
}

/* ---- 状态提示：思考中（用呼吸点 + 轮换短语表现进度，不暴露机器细节） ---- */
const GAL_STATUS_PHRASES = ['思考中', '斟酌着', '斟酌中', '思绪流转'];
function galSetStatus(text) {
  const box = $('#gal-status');
  const t = $('#gal-status-text');
  clearInterval(galState.statusTimer);
  galState.statusTimer = null;
  galState.statusBase = text || '';
  if (t) t.textContent = galState.statusBase;
  if (box) box.classList.toggle('hidden', !text);
  galSyncAdvHint();          // 生成中隐藏「推进 ▼」，生成完自动露出来
  if (!text) return;
  let i = galStatusPhraseIndex(text);
  galState.statusTimer = setInterval(() => {
    const el = $('#gal-status-text');
    if (!el || !galState.statusBase) return;
    i = (i + 1) % GAL_STATUS_PHRASES.length;
    el.textContent = GAL_STATUS_PHRASES[i] + '…';
  }, 2600);
}
// 状态短语起始位置：同类状态从同一句开始，避免每次跳变
function galStatusPhraseIndex(text) {
  const s = String(text || '');
  if (s.indexOf('回想') >= 0) return 1;
  if (s.indexOf('查阅') >= 0) return 2;
  if (s.indexOf('稍候') >= 0) return 3;
  return 0;
}
// 用量：GAL 模式下不展示 token 这类机器指标（只在内部对话记录里保留）
function galExecUsage(usage) {
  if (!usage) return;
  if (galCfg('gal_show_exec', true) === false) return;
  const total = parseInt(usage.total_tokens || usage.total || 0, 10) || 0;
  if (total > 0) galExecLine('本轮共 ' + total + ' 字迹');
}
// 深度思考的实时内容：让 GAL 舞台上也能看到模型正在「想什么」
// 受「设置 → GAL 演出 → 显示 AI 思考过程」控制；关闭时只保留状态栏的「思考中…」
function galThinkAppend(text) {
  if (!text) return;
  if (galCfg('gal_show_think', false) === false) return;
  const box = $('#gal-think');
  if (!box) return;
  box.classList.remove('hidden');
  box.textContent += text;
  box.scrollTop = box.scrollHeight;
}
function galThinkClear() {
  const box = $('#gal-think');
  if (box) { box.textContent = ''; box.classList.add('hidden'); }
}
// GAL「幕间」区：把 AI 的执行过程（搜索 / 工具 / 记忆 / 用量 / 报错）显示在舞台上，
// 让 GAL 模式下也能像 Solo 那样看清它在干什么；受「显示 AI 行为」开关控制。
function galExecLine(text, kind) {
  if (!text) return;
  if (galCfg('gal_show_exec', true) === false) return;
  const box = $('#gal-tools');
  if (!box) return;
  const el = document.createElement('div');
  el.className = 'gal-exec-line' + (kind ? (' ' + kind) : '');
  el.textContent = text;
  box.appendChild(el);
  box.scrollTop = box.scrollHeight;
}
// 掷骰 / 判定结果：放在「幕间」区，且**不受**「显示 AI 行为」开关影响
// （这是玩家必须看到的反馈，不是 AI 内部流程）
function galShowRoll(obj) {
  if (!obj) return;
  const box = $('#gal-tools');
  if (!box) return;
  const el = document.createElement('div');
  const kind = obj.success === true ? ' ok' : (obj.success === false ? ' err' : '');
  el.className = 'gal-exec-line gal-roll' + kind;
  el.textContent = '🎲 ' + (obj.why ? (obj.why + '：') : '') + (obj.detail || '');
  box.appendChild(el);
  box.scrollTop = box.scrollHeight;
}

function galSetTyping(on) {
  const el = $('#gal-text');
  if (el) el.classList.toggle('typing', !!on);
}

/* ---- 背景 / BGM ---- */