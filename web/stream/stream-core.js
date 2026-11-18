'use strict';

/* 流式对话 · stream-core.js
   主流程 streamChat：请求 / 事件 / 渲染 / 收尾
   原 web/stream.js 拆分而来：按 index.html 中的顺序加载，共享全局作用域。 */

/* attach：接管仍在后台运行的任务，把数据源从 /api/chat 换成 /api/chat/attach。
   事件、渲染、收尾全部走下面同一套管线，因此体验与正常发送完全一致
   （思考块 / 工具卡 / 提问卡都实时重建）。 */
async function streamChat(extra, resume, attach) {
  // 本条流归属的会话：之后所有 DOM / 滚动 / 停止都按它判断，
  // 用户切到别的会话不影响这一条继续跑。
  const sid = String(state.sessions.current || '');
  const isCur = () => String(state.sessions.current || '') === sid;
  // 局部遮蔽全局 scrollToBottom：后台会话收流时绝不拽动用户正在看的窗口
  const scrollToBottom = (force) => { if (isCur()) window.scrollToBottom(force); };
  // attach 接管：live 数据是唯一来源，旧数据（若有）先清掉再回放。
  // 历史里的「进度快照行」由 renderMessages 按 live 数据投影替换，不再单独删 DOM。
  if (attach) {
    const oldLive = liveOf(sid, true);
    if (oldLive && oldLive.blocks.length) liveDrop(sid);
  }
  const live = liveOf(sid);
  // live 投影用的工具 FIFO：与 DOM 的 row._toolQueue 同规则，避免同名并行工具错配
  const liveToolQ = {};
  let reasoningRetry = false;
  // AI 占位：先摆一组「呼吸点」表示正在生成，首个 token 到达即换成正文
  // resume：续写模式——复用上一条被打断的气泡，把已有文本当流式起点
  let row;
  let base = '';
  if (resume && resume.row) {
    row = resume.row;
    base = resume.base || '';
    // 续写：把旧的「多段正文 / 提问卡」并回**一段仍然挂在文档里的**正文节点，
    // 否则收尾重排会留下重复内容。
    // 这里以前写的是「删掉第 2 段起的全部，再把 row._content 当保留段」——
    // 但收尾重排后 `_content` 指向的是**最后一段**（buildBlocksView 的 parts 末尾），
    // 多段正文时被删掉的正是它自己：整轮续写的 token 全写进一个脱离文档的节点，
    // 界面一直不变化，只有切会话 / 刷新重建才看得到（现场 00:30 的「继续输出」）。
    let keep = row._content;
    const secs = (row._secs || []).filter((n) => n && n.parentNode);
    if (secs.length && secs.indexOf(keep) < 0) keep = secs[secs.length - 1];
    (row._secs || []).forEach((n) => { if (n !== keep) n.remove(); });
    (row._asks || []).forEach((n) => n.remove());
    if (!keep || !keep.parentNode) {
      // 极端情况（行被别处重排过）：新建一段正文再接流，别把输出写进死节点
      keep = document.createElement('div');
      keep.className = 'content';
      const anchor = row._wrap.querySelector('.msg-actions, .live-bar, .end-note');
      row._wrap.insertBefore(keep, anchor || null);
    }
    row._secs = [keep];
    row._asks = [];
    row._content = keep;
    keep.style.display = '';
    keep.textContent = base;                      // 清掉旧的 markdown 渲染，改为纯文本起点
    row._raw = base;
    const oldBar = row._wrap.querySelector('.msg-actions');
    if (oldBar) oldBar.remove();                  // 移除旧操作条，流结束后统一重建
    row._usageEl = row._usageEl || row._wrap.querySelector('.usage-line') || null;
  } else {
    row = appendAi('', '', undefined);
    // 接管后台续写：新行先把上一轮的计划继承过来，进度不必等模型下次 update_plan
    if (attach && row && typeof planSeedFromSession === 'function') planSeedFromSession(row);
  }
  // 把这一轮**整条消息**装进 host：切走时 #chat 会被重建，host 只是离开文档，节点与事件都还在。
  // 必须包住 row 本身而不是 row._wrap —— 否则切回来时 attachStreamHost 会把 host 移到 #chat 下，
  // .content-wrap 脱离 .msg，max-width / 居中 / 内边距全部失效（表现为内容贴满两侧）。
  const host = document.createElement('div');
  host.className = 'stream-host';
  if (row.parentNode) {
    row.parentNode.insertBefore(host, row);
    host.appendChild(row);
  }
  registerStream(sid, host, row);
  const stRef = state.streams[sid];
  let c = row._content;
  // 正文分段：提问卡插在两次正文之间，后续输出另起一段，不再堆到消息末尾
  row._secs = [c];
  row._asks = [];
  c.style.whiteSpace = 'pre-wrap';
  // 实时状态条：左侧扫描光环图标 + 当前动作 + 已用秒数。
  // 目的：模型思考 / 跑工具时界面不能只有闪烁光标，否则用户分不清「在跑」和「卡死」。
  const typing = document.createElement('span');
  typing.className = 'live-bar';
  // 图标：静止细环 + 旋转扫描弧 + 呼吸核心点（纯 CSS，不逐点设延迟）
  typing.innerHTML = '<span class="live-orb" aria-hidden="true">'
                   + '<i class="live-orb-ring"></i><i class="live-orb-core"></i></span>'
                   + '<span class="live-text">正在发送…</span>'
                   + '<span class="live-sec"></span>';
  // 放在 content 外层：正文每次重绘都会清空 c.textContent，放里面会被冲掉
  row._wrap.appendChild(typing);
  // 状态条常驻在正文下方，直到本轮结束。
  // 每个 delta 都 appendChild 会把节点「移除再插入」，CSS 动画随之重头播一遍（图标会抖），
  // 所以只有它不在末尾时才挪。
  const keepLiveLast = () => {
    if (row._wrap && row._wrap.lastElementChild !== typing) row._wrap.appendChild(typing);
  };
  // 上行链路中断：只提示 + 给一个「停止本轮」，重试策略不变，由用户自己决定等还是停。
  let retryNote = null;
  const clearRetryNote = () => {
    if (retryNote && retryNote.parentNode) retryNote.remove();
    retryNote = null;
  };
  const showRetryNote = (obj) => {
    const st = String((obj && obj.state) || '');
    if (st !== 'upload' && st !== 'network') { clearRetryNote(); return; }
    if (!retryNote || !retryNote.isConnected) {
      retryNote = document.createElement('div');
      retryNote.className = 'retry-note';
      retryNote.innerHTML = '<span class="rn-ico">⚠</span>'
        + '<span class="rn-text"></span>'
        + '<button type="button" class="rn-stop">停止本轮</button>';
      retryNote.querySelector('.rn-stop').onclick = () => {
        if (typeof stopStreaming === 'function') stopStreaming();
      };
      if (row._wrap) row._wrap.insertBefore(retryNote, typing);
    }
    const n = Math.max(1, Number(obj && obj.n) || 1);
    const why = st === 'network' ? '网络不通' : '请求发送中断';
    retryNote.querySelector('.rn-text').textContent =
      why + '，已重试 ' + n + ' 次 · 可能是网络或代理不稳定；'
      + '可以继续等待，或点右侧停止本轮。';
  };
  // 子代理卡片：按 group 分卡（探索小队 / 子代理小队 / 自动复核 / 工作流子代理…），
  // 同一组内每个代理一行；放在正文外层，不会被正文重绘冲掉。
  const subagentBoxes = Object.create(null);   // group -> card
  let subagentTimer = 0;
  const subagentRows = {};        // id -> {el, group, done, fail, t0}
  const SUB_ROLE = { searcher: '侦察', reader: '阅读', finder: '定位', worker: '执行',
    critic: '反证者', researcher: '资料调研员', reviewer: '代码审查员',
    'fidelity-reviewer': '还原复核员', implementer: '实现者', planner: '规划师' };
  const SUB_GROUP = (g) => String(g || '探索小队');
  // 运行中的行每 0.4 秒刷一次耗时：子代理常常几十秒没有任何事件，
  // 没有秒数用户分不清「在跑」和「卡死」。卡片被重绘掉就自动停表。
  const subAgentTick = () => {
    const ids = Object.keys(subagentRows);
    let alive = false;
    ids.forEach((id) => {
      const s = subagentRows[id];
      if (!s.el || !s.el.isConnected) return;
      if (!s.done) {
        alive = true;
        const t = s.el.querySelector('.subagent-time');
        if (t) t.textContent = ((Date.now() - s.t0) / 1000).toFixed(1) + 's';
      }
    });
    if (!alive && subagentTimer) { clearInterval(subagentTimer); subagentTimer = 0; }
  };
  const subagentSummary = (group) => {
    const box = subagentBoxes[SUB_GROUP(group)];
    if (!box) return;
    const ids = Object.keys(subagentRows).filter((id) => subagentRows[id].group === SUB_GROUP(group));
    const done = ids.filter((id) => subagentRows[id].done).length;
    const fail = ids.filter((id) => subagentRows[id].fail).length;
    const sum = box.querySelector('.subagent-sum');
    if (sum) {
      sum.textContent = '完成 ' + done + '/' + ids.length + (fail ? ' · 失败 ' + fail : '');
    }
  };
  const ensureSubagentBox = (group) => {
    const key = SUB_GROUP(group);
    const old = subagentBoxes[key];
    if (old && old.isConnected) return old;
    if (!isCur()) return null;
    const box = document.createElement('div');
    box.className = 'subagent-card';
    box.dataset.group = key;
    box.innerHTML = '<div class="subagent-head">'
      + '<button type="button" class="subagent-fold" title="收起 / 展开">'
      + '<span class="subagent-caret">▾</span></button>'
      + '<span class="subagent-title">' + escapeHtml(key) + '</span>'
      + '<span class="subagent-sum">启动中…</span></div>'
      + '<div class="subagent-body"></div>';
    const fold = box.querySelector('.subagent-fold');
    fold.onclick = () => {
      const folded = box.classList.toggle('folded');
      fold.querySelector('.subagent-caret').textContent = folded ? '▸' : '▾';
    };
    row._wrap.insertBefore(box, typing);
    subagentBoxes[key] = box;
    if (!subagentTimer) subagentTimer = setInterval(subAgentTick, 400);
    return box;
  };
  const subagentRow = (id, group) => {
    const key = SUB_GROUP(group);
    const box = ensureSubagentBox(key);
    if (!box) return null;
    const rk = key + '|' + id;
    if (!subagentRows[rk]) {
      const r = document.createElement('div');
      r.className = 'subagent-row running';
      r.innerHTML = '<div class="subagent-top">'
        + '<span class="subagent-dot"></span>'
        + '<span class="subagent-role"></span>'
        + '<div class="subagent-main">'
        + '<div class="subagent-ask"></div>'
        + '<div class="subagent-step">排队中…</div>'
        + '</div>'
        + '<span class="subagent-time"></span>'
        + '<span class="subagent-ico">⋯</span>'
        + '</div>'
        + '<div class="subagent-detail">'
        + '<div class="subagent-metrics"></div>'
        + '<div class="subagent-result"></div>'
        + '</div>';
      // 点一行展开：看完整问题、指标与结论预览（默认收起，不抢正文）
      r.querySelector('.subagent-top').onclick = () => {
        const d = r.querySelector('.subagent-detail');
        const next = !foldIsOpen(r, 'open', true);
        foldTransition(r, d, next, { cls: 'open', positive: true, ms: 180 });
      };
      box.querySelector('.subagent-body').appendChild(r);
      subagentRows[rk] = { el: r, group: key, id: id,
                           done: false, fail: false, t0: Date.now() };
    }
    return subagentRows[rk];
  };
  const subagentOf = (obj) => subagentRows[SUB_GROUP(obj.group) + '|' + obj.id];
  // 注意：不能叫 liveText —— 会遮蔽全局的 liveText(L, t)，回放 delta 时
  // 调用的就变成这个 DOM 元素，抛 TypeError 把整条流打断。
  const liveTip = typing.querySelector('.live-text');
  const liveSec = typing.querySelector('.live-sec');
  const setLive = (t) => {
    if (liveTip && t && liveTip.textContent !== t) liveTip.textContent = t;
  };
  const setLiveSec = (s) => {
    if (liveSec) liveSec.textContent = s ? String(s) : '';
  };
  const cur = document.createElement('span');
  cur.className = 'cursor';
  // 输出期间给正文挂一个 streaming 标记：光标呼吸 / 新块淡入都只在这段时间生效
  if (c && c.classList) c.classList.add('streaming');

  let acc = base;
  // 当前「正文段」在 acc 里的起点：遇到工具调用时把这一段搬进过程折叠，
  // 答案区只保留最后一次工具之后的正文（与 buildBlocksView 的历史口径一致）。
  let segStart = base.length;
  let serverFinalText = null;   // 服务端收尾校正后的正文（摘掉裸工具 JSON / 伪标签）
  let textNode = null;          // 流式期间只往这个节点追加，避免每个 chunk 重写整段
  // —— 流式实时 Markdown（NOTE 43.96）：定稿区 + 尾巴区 ——
  // 定稿区只追加已完整的块，永不重建（富媒体 / 代码高亮不会重跑）；
  // 尾巴区是当前没写完的一段，节流重渲染。
  const liveEnabled = true;
  let liveBox = null;           // .md-live：已定稿的块
  let tailBox = null;           // .md-tail：当前未完成的一段
  let liveRendered = 0;         // acc 中已推进定稿区的长度（永远落在行首）
  // 逐帧「追赶」：acc 是「已收到」，revealPos 是「已上屏」。
  // 网络一次可能给几百字，直接贴屏就是一块一块往外蹦；这里按 ~2-4 字/帧 稳定写入，
  // 积压多了自动加速（上限 48 字/帧），保证观感连续又绝不落后于网络。
  let revealPos = base.length;
  let revealRaf = 0;
  let paintTimer = 0;
  let lastPaintAt = 0;
  // 重绘节流：短板 32ms（≈30fps，与原生流式输出同档），短尾/长尾/后台自动降频。
  // 以前固定 90ms，肉眼能看出「一段一段往外蹦」。
  const PAINT_MS = 32;
  const PAINT_MS_LONG = 56;         // 尾巴偏长：解析成本上来了，降一档
  const PAINT_MS_FENCE = 90;        // 代码围栏未闭合：每帧重解析代价最高，再降一档
  const PAINT_MS_HIDDEN = 200;      // 标签页不可见：让出主线程，回来后立即补一帧
  const TAIL_RENDER_MAX = 4000;     // 尾巴超过这个长度就不逐帧渲染（避免长代码块反复高亮）
  let tailFence = false;            // 尾巴是否落在未闭合的代码围栏里（决定重绘频率）
  // 尾巴纯文本判定：没有换行、没有 Markdown 标记、也不是列表项开头。
  const TAIL_PLAIN_RE = /^[^\n`*_~[\]<>#|!\\]*$/;
  const tailIsPlain = (t) => !!t && TAIL_PLAIN_RE.test(t)
    && !/^\s*(?:[-+]|\d+[.)])\s/.test(t);
  let thinkBox = null;
  let flow = null;              // 过程流：思考块 + 工具组按时间线排在这里
  let flowWrap = null;          // 外层折叠容器（头 + body）
  let flowToggled = false;      // 用户手动展开 / 收起过过程
  let toolPending = null;       // 「正在生成工具调用参数」的轻量行
  let sourcesBox = null;
  let memNote = null;
  let gotDone = false;          // 是否收到 done 事件（收到就不必在收尾再兜底拉一次 /api/state）
  let doneBlocks = null;        // 服务端落库后的块：流结束时按它重绘一次（V3）
  let doneTiming = null;
  let doneTools = null;
  let doneMsgIdx = null;        // 本轮回复在会话里的消息下标（点击通知时精确定位）
  let streamError = null;       // 流式请求异常（用于后台通知）
  let endReason = null;         // 非正常结束时服务端给的原因（长度/网络/轮数/手动停止…）
  let sawError = false;         // 收到过 error 事件：此时不再接受 replace 覆盖
  // 最近一条错误文案。**不立即写进正文**：上游失败后通常会重试，重试成功时
  // 这句错误会残留成一个独立段落（用户看到的「先出现一段中断文字，过一会儿合并」）。
  // 只有整轮结束时一句正文都没有，才把它补进正文，避免空气泡。
  let lastErrText = '';
  let askedNotified = false;    // 本轮是否已提醒过「等你回答」
  // 等待计时：模型思考 / 工具执行期间可能几十秒没有任何正文，
  // 界面必须给出秒数，否则用户分不清「在跑」与「卡死」（见 NOTE 43.89）。
  let phase = 'idle';           // idle | think | tool | output
  let phaseAt = Date.now();     // 当前阶段起点
  let retryWait = null;         // 自动重试的退避倒计时（{until}）；到点后由下一次事件接管
  const streamStart = Date.now();   // 本轮实际开始时刻：过程折叠头显示「工作 X 秒」
  let lastEventAt = Date.now(); // 最后收到任何 SSE 事件（含 tick）的时间
  let lastContentAt = Date.now(); // 最后收到非 tick 事件（步骤/思考/正文/工具）
  let staleShown = false;       // 「15s 连事件都没有」提示是否已显示
  let contentStaleShown = false; // 「15s 没有思考/正文」提示是否已显示
  let waitingApproval = false;  // 正在等用户点「允许 / 拒绝」：不算模型空转

  // 后台标签页被冻结时定时器会停摆：切回前台先重置计时基准，
  // 否则会把「刚才没在计时」误判成「连接断了」，一回来就闪一次中断提示。
  const onVisible = () => {
    if (document.hidden) return;
    lastEventAt = Date.now();
    lastContentAt = Date.now();
    staleShown = false;
    contentStaleShown = false;
  };

  // 思考标题节流：流式时最多每 150ms 更新一次，避免逐 token 抖动
  let thinkLabelAt = 0;
  const refreshThinkLabel = (box, settled) => {
    if (!box) return;
    const now = Date.now();
    if (!settled && now - thinkLabelAt < 150) return;
    thinkLabelAt = now;
    const head = thinkLineSummary(box._body.textContent, settled);
    const step = box._step || 1;
    box._hasSummary = !!head;      // 有摘要后，每秒计时器不再把标题改回「思考中… Ns」
    box._label.textContent = '步骤 ' + step + (head ? ' · ' + head : ' · 思考中');
    // V4：状态条不只说“思考中”，而是「当前步骤 + 最后一小段思考」
    if (head) setLive('第 ' + step + ' 步 · ' + clipText(head, 60));
  };

  // 过程流：思考块与工具组按时间顺序排在这里，正文固定在它下面。
  // 外面再包一层折叠容器：流式时展开，done 后折成「任务耗时 X」。
  const ensureFlow = () => {
    if (flow) return flow;
    // 续写（同一行再次 streamChat）：复用上一段的过程折叠，别叠成两条
    if (row._flowWrap && row._flow && row._flowWrap.isConnected) {
      flowWrap = row._flowWrap;
      flow = row._flow;
      flowWrap.classList.remove('collapsed');
      flowWrap.classList.remove('has-fail');
      flowWrap.classList.remove('done');
      flowWrap.classList.remove('stopped');
      flowWrap.classList.add('running');
      row._flowLabel.textContent = '过程 · 进行中';
      if (row._flowMeta) row._flowMeta.textContent = '';
      if (row._flowTime) row._flowTime.textContent = '';
      row._flowCaret.textContent = '▾';
      row._flowHead.setAttribute('aria-expanded', 'true');
      flowToggled = false;
      return flow;
    }
    flowWrap = document.createElement('div');
    flowWrap.className = 'process-fold running';
    const head = document.createElement('div');
    head.className = 'process-fold-head';
    const dot = document.createElement('span');
    dot.className = 'process-fold-dot';
    dot.setAttribute('aria-hidden', 'true');
    const title = document.createElement('span');
    title.className = 'process-fold-title';
    title.textContent = '过程 · 进行中';
    // 折叠头上的一行统计（N 步 / N 步失败）：收起时也能看出这一轮做了多少事
    const meta = document.createElement('span');
    meta.className = 'process-fold-meta';
    const time = document.createElement('span');
    time.className = 'process-fold-time';
    const caret = document.createElement('span');
    caret.className = 'process-fold-caret';
    caret.textContent = '▾';
    head.appendChild(dot);
    head.appendChild(title);
    head.appendChild(meta);
    head.appendChild(time);
    head.appendChild(caret);
    const body = document.createElement('div');
    body.className = 'chat-flow process-fold-body';
    flowWrap.appendChild(head);
    flowWrap.appendChild(body);
    row._wrap.insertBefore(flowWrap, row._content);
    row._flowWrap = flowWrap;
    row._flowHead = head;
    row._flowLabel = title;
    row._flowMeta = meta;
    row._flowTime = time;
    row._flowCaret = caret;
    const toggle = () => {
      flowToggled = true;
      const next = !foldIsOpen(flowWrap, 'collapsed');
      foldTransition(flowWrap, body, next, { cls: 'collapsed' });
      caret.textContent = next ? '▾' : '▸';
      head.setAttribute('aria-expanded', next ? 'true' : 'false');
    };
    head.onclick = toggle;
    makeClickable(head, toggle, '展开 / 收起过程');
    head.setAttribute('aria-expanded', 'true');
    flow = body;
    row._flow = body;
    return flow;
  };

  const clearToolPending = () => {
    if (toolPending) {
      toolPending.remove();
      toolPending = null;
    }
  };

  const fmtBytes = (n) => {
    n = Number(n) || 0;
    if (n < 1024) return n + ' B';
    if (n < 1048576) return Math.round(n / 1024) + ' KB';
    return (Math.round(n / 1048576 * 10) / 10) + ' MB';
  };

  // 本轮结束：过程折成一行；用户手动展开过就保持原样。
  const foldLiveProcess = () => {
    if (!flowWrap) return;
    if (!flow || !flow.childNodes.length) {
      flowWrap.remove();
      flowWrap = null;
      flow = null;
      row._flowWrap = null;
      row._flow = null;
      row._flowHead = null;
      row._flowLabel = null;
      row._flowMeta = null;
      row._flowTime = null;
      row._flowCaret = null;
      return;
    }
    const kind = (endReason && endReason.kind) || '';
    if (kind === 'stopped') row._flowLabel.textContent = '过程 · 已停止';
    else if (kind === 'network' || kind === 'server' || kind === 'error') {
      row._flowLabel.textContent = '过程 · 中断';
    } else {
      row._flowLabel.textContent = '任务耗时 ' + workDuration(Date.now() - streamStart);
    }
    if (row._flowTime) row._flowTime.textContent = '';
    flowWrap.classList.remove('running');
    flowWrap.classList.toggle('done', !kind || kind === 'done');
    flowWrap.classList.toggle('stopped', kind === 'stopped');
    // 折叠头统计：做了几步、几步失败（失败标红，收起也看得见）
    const stats = (typeof processStats === 'function')
      ? processStats(flow) : { steps: 0, failed: 0 };
    if (row._flowMeta) {
      row._flowMeta.textContent = (typeof flowMetaText === 'function') ? flowMetaText(stats) : '';
    }
    flowWrap.classList.toggle('has-fail', stats.failed > 0
      || kind === 'network' || kind === 'server' || kind === 'error' || kind === 'billing');
    // 有失败就不自动收：错误必须留在视线里，否则用户以为一切正常
    if (stats.failed > 0) return;
    if (!flowToggled) {
      // 延迟 600ms 再收：让用户看清最后一步的完成状态，而不是收尾瞬间「啪」地消失
      const wrap = flowWrap;
      setTimeout(() => {
        if (!wrap || !wrap.isConnected || flowToggled) return;
        // 收起也走统一的高度过渡；高度落定后再补偿贴底，避免视图停在半路
        foldTransition(wrap, flow, false, {
          cls: 'collapsed',
          done: () => {
            if (_followBottom && typeof isAtBottom === 'function' && isAtBottom(120)) {
              scrollToBottom(true);
            }
          },
        });
        if (row._flowCaret) row._flowCaret.textContent = '▸';
        if (row._flowHead) row._flowHead.setAttribute('aria-expanded', 'false');
      }, 600);
    }
  };

  // 收尾当前思考块：空的直接去掉，别留一个空壳占位；
  // 非空的标题换成一行摘要（点开还能看全文）。
  const closeThink = (label) => {
    if (!thinkBox) return;
    const text = thinkBox._body.textContent || '';
    if (!text.trim()) thinkBox.remove();
    else {
      foldTransition(thinkBox, thinkBox._body, false, { cls: 'open', positive: true, ms: 180 });
      const head = thinkLineSummary(text, true);
      const step = thinkBox._step || 1;
      thinkBox._hasSummary = !!head;
      thinkBox._label.textContent = head
        ? ('步骤 ' + step + ' · ' + head)
        : (label || ('步骤 ' + step + ' · 已思考'));
    }
    thinkBox = null;
  };

  // 把正文容器切成两块：liveBox（已定稿，只追加）+ tailBox（未完成，节流重渲染）
  const ensureLive = () => {
    if (liveBox) return;
    c.style.whiteSpace = '';      // 块级渲染不再需要保留源码换行
    c.textContent = '';           // 清掉呼吸点 / 续写模式的纯文本起点
    liveBox = document.createElement('div');
    liveBox.className = 'md-live';
    tailBox = document.createElement('div');
    tailBox.className = 'md-tail';
    c.appendChild(liveBox);
    c.appendChild(tailBox);
    c.appendChild(cur);           // 光标始终停在最后一个块之后
    textNode = null;
  };

  // 从 from 往后找最后一个可定稿的位置：围栏外的空行，或刚闭合的围栏行。
  // 只在围栏外切，定稿区里的 Markdown 才不会被切成半截。
  const scanLive = (text, from) => {
    let i = from, fence = false, cut = from;
    while (i < text.length) {
      const nl = text.indexOf('\n', i);
      const line = nl < 0 ? text.slice(i) : text.slice(i, nl);
      if (/^\s{0,3}(?:```|~~~)/.test(line)) {
        fence = !fence;
        if (!fence && nl >= 0) cut = nl + 1;   // 围栏刚闭合：这一块已经完整，可以定稿
        if (nl < 0) break;
        i = nl + 1;
        continue;
      }
      if (!fence && nl >= 0 && !line.trim()) cut = nl + 1;
      if (nl < 0) break;
      i = nl + 1;
    }
    return { cut, fence };
  };

  // 流式时把「可能是协议标记的前半截」压在缓冲里：
  // 模型吐 `<tool calls>` 或 ```tool 是分片到达的，直接上屏会先闪出 `<tool cal` / ```to
  // 再被收尾替换掉。这里只压很短的尾巴（<64 字符），一旦出现 `>` 或不像标记就立刻放行。
  const holdBackPartialMarker = (tail) => {
    let cut = tail.length;
    const lt = tail.lastIndexOf('<');
    if (lt >= 0 && tail.indexOf('>', lt) < 0) {
      const frag = tail.slice(lt);
      if (frag.length < 64
          && /^<\/?\s*[A-Za-z][\w:.\-\u4e00-\u9fff]*(\s[^<>]*)?$/.test(frag)
          && /tool|parameter|invoke|function_calls|thinking|antml/i.test(frag)) {
        cut = lt;
      }
    }
    const m = /(^|\n)([ \t]*)(`{1,3}|~{1,3})([A-Za-z+-]*)$/.exec(tail.slice(0, cut));
    if (m) {
      const w = (m[4] || '').toLowerCase();
      const prefixes = ['t', 'to', 'too', 'tool', 'tool_', 'tool_c', 'tool_ca',
        'tool_cal', 'tool_call', 'tool_calls', 'j', 'js', 'jso', 'json'];
      if (prefixes.some((x) => x.startsWith(w))) cut = m.index + m[1].length;
    }
    return tail.slice(0, cut);
  };

  // 用户正在尾巴区里选字：这一帧不能重建，否则 innerHTML 会把选区清掉。
  // 松手（选区塌陷 / 移出）后下一帧自动补上，最多晚 120ms。
  const selectionInside = (node) => {
    if (!node) return false;
    try {
      const sel = window.getSelection && window.getSelection();
      if (!sel || sel.isCollapsed || !sel.rangeCount) return false;
      const r = sel.getRangeAt(0);
      return node.contains(r.startContainer) || node.contains(r.endContainer);
    } catch (e) { return false; }
  };

  const paintLive = () => {
    paintTimer = 0;
    if (!liveBox) return;
    if (selectionInside(tailBox)) { paintTimer = setTimeout(paintLive, 120); return; }
    lastPaintAt = Date.now();
    // 用跟随状态而不是当前高度：这一帧内容已经长高了，再算 isAtBottom 会误判成「用户在看历史」
    const wasAtBottom = isCur() && _followBottom;
    const view = acc.slice(0, revealPos);      // 只渲染「已经追上来的」部分
    const { cut, fence } = scanLive(view, liveRendered);
    tailFence = fence;
    let next = cut;
    // 尾巴太长说明模型卡在一个超长段落 / 未闭合代码块里：
    // 围栏外就整段推进定稿区（只切到最后一行，半行留在尾巴），
    // 围栏内则退回纯文本，避免每帧高亮几千行代码。
    if (view.length - next > TAIL_RENDER_MAX && !fence) {
      const nl = view.lastIndexOf('\n');
      if (nl >= next) next = nl + 1;
    }
    if (next > liveRendered) {
      const html = safeRenderMarkdown(view.slice(liveRendered, next));
      if (html) {
        liveBox.insertAdjacentHTML('beforeend', html);
        if (window.enhanceRichMedia) window.enhanceRichMedia(liveBox);
      }
      liveRendered = next;
    }
    const tail = holdBackPartialMarker(view.slice(liveRendered));
    if (tail.length > TAIL_RENDER_MAX) {
      tailBox.className = 'md-tail plain';
      tailBox.textContent = tail;
    } else if (!fence && tailIsPlain(tail)) {
      // 纯文本快通道：没有换行、也没有任何 Markdown 标记时直接写 textContent。
      // 省掉每帧的 Markdown 解析 + innerHTML 重建（长回答时这是主要的掉帧来源）；
      // 单行纯文本用两种方式渲染结果一致，不会出现「先原始后变格式」的跳变。
      tailBox.className = 'md-tail plain';
      tailBox.textContent = tail;
    } else {
      tailBox.className = 'md-tail';
      tailBox.innerHTML = safeRenderMarkdown(tail);
      if (window.enhanceRichMedia) window.enhanceRichMedia(tailBox);
    }
    // 输出阶段把「已经上屏多少字」带上，让用户看到确实在推进
    if (phase === 'output') setLive('正在输出 · ' + revealPos + ' 字');
    if (wasAtBottom) {
      if (typeof followScroll === 'function') followScroll();
      else scrollToBottom(true);
    }
  };

  // 重绘间隔自适应：短尾按 30fps 追；长尾 / 未闭合代码围栏 / 后台标签页逐档降频，
  // 保证「看起来连续」的同时不把主线程占满。
  const paintGapFor = (tailLen) => {
    if (document.hidden) return PAINT_MS_HIDDEN;
    if (tailFence) return PAINT_MS_FENCE;
    if (tailLen > 1200) return PAINT_MS_LONG;
    return PAINT_MS;
  };
  const schedulePaint = () => {
    if (paintTimer) return;
    const gap = paintGapFor(Math.max(0, acc.length - liveRendered));
    const wait = Math.max(0, gap - (Date.now() - lastPaintAt));
    paintTimer = setTimeout(paintLive, wait);
  };

  // 揭示节奏：积压越多追得越快。2 字/帧 ≈ 120 字/秒 是「读起来像在写」的舒适区，
  // 48 字/帧 ≈ 2900 字/秒 是兜底上限；两者都远低于网络到达速度时才需要追赶。
  const REVEAL_MIN = 2;
  const REVEAL_MAX = 48;
  const revealTick = () => {
    revealRaf = 0;
    if (!liveEnabled || !liveBox) return;
    const backlog = acc.length - revealPos;
    if (backlog <= 0) return;
    const step = Math.min(REVEAL_MAX, Math.max(REVEAL_MIN, Math.ceil(backlog / 8)));
    revealPos = Math.min(acc.length, revealPos + step);
    schedulePaint();
    if (revealPos < acc.length) revealRaf = requestAnimationFrame(revealTick);
  };
  const startReveal = () => {
    if (!liveEnabled || revealRaf) return;
    if (revealPos >= acc.length) return;
    revealRaf = requestAnimationFrame(revealTick);
  };
  // 需要「立刻全部上屏」的时刻（提问卡插入、收尾重排、停止输出）：别让动画拖住逻辑
  const flushReveal = () => {
    if (revealRaf) { cancelAnimationFrame(revealRaf); revealRaf = 0; }
    if (revealPos !== acc.length) revealPos = acc.length;
    if (liveEnabled && liveBox) paintLive();
  };

  /* 某段正文后面紧跟工具调用：这一段是过程旁白，搬进过程折叠。
     与收尾重排同一口径（最后一次真实工具之后的正文才是答案），
     这样流式期间不会出现「长旁白留在正文、收尾又被折走」的跳变。
     收尾工具名统一用 chat.js 的 isFinishTool（FINISH_TOOL_NAMES 只在那一处维护）。 */
  const foldSegmentToProcess = (toolName) => {
    if (!String(toolName || '').trim()) return;   // 名字还没到：等 tool 事件，别误折 finish
    if (isFinishTool(toolName)) return;
    const seg = acc.slice(segStart);
    if (!seg.trim()) return;                     // 这一段没有正文：无需搬
    if (paintTimer) { clearTimeout(paintTimer); paintTimer = 0; }
    flushReveal();                               // 先把已收到的字全部落屏，别漏半个词
    const d = document.createElement('div');
    d.className = 'content narration';           // 与历史重建时的旁白同一套样式
    d.innerHTML = safeRenderMarkdown(seg);
    if (typeof enhanceToc === 'function') enhanceToc(d);
    ensureFlow().appendChild(d);
    // 清空当前正文段：后续 delta 另起一段，答案区不会累积过程旁白。
    c.textContent = '';
    c.style.display = '';
    liveBox = null;
    tailBox = null;
    textNode = null;
    liveRendered = acc.length;
    revealPos = acc.length;
    segStart = acc.length;
    c.appendChild(cur);                          // textContent='' 会把光标一起清掉
    followProcessStep(d);
  };

  // 增量写入正文（首次调用时把呼吸点换成文本节点）
  const pushText = (t) => {
    keepLiveLast();                 // 状态条常驻在正文下方，直到本轮结束
    const tip = c.querySelector('.recognize-tip');
    if (tip) tip.remove();
    if (!liveEnabled) {
      if (!textNode) {
        textNode = document.createTextNode('');
        c.insertBefore(textNode, cur.parentNode ? cur : null);
        c.appendChild(cur);       // 光标始终跟在文本末尾
      }
      textNode.appendData(t);
      return;
    }
    ensureLive();
    schedulePaint();             // acc 已累加，这里只触发重绘
    startReveal();               // 按帧把新到的文字「写」上屏
  };

  // 提问卡落在「真正被问出来」的位置：先把当前正文段收尾并上屏，卡片插在它后面，
  // 之后的内容另起一段正文 —— 否则卡片会被后续输出顶到消息最下方（历史重排也一样）。
  const splitAtAsk = (card) => {
    if (paintTimer) { clearTimeout(paintTimer); paintTimer = 0; }
    flushReveal();                                    // 尾巴先落屏，别丢字
    if (!String(c.textContent || '').trim()) c.style.display = 'none';   // 空段不占位
    const anchor = typing.parentNode === row._wrap ? typing : null;
    row._wrap.insertBefore(card, anchor);
    const next = document.createElement('div');
    next.className = 'content';
    next.style.whiteSpace = 'pre-wrap';
    row._wrap.insertBefore(next, anchor);
    c = next;
    row._content = c;
    row._secs.push(c);
    row._asks.push(card);
    liveBox = null;              // 新段重新建定稿区 / 尾巴区
    tailBox = null;
    textNode = null;
    liveRendered = acc.length;   // 新段只渲染这之后的内容
    segStart = acc.length;       // 提问卡之前的正文留在原段，不参与后续折叠
    c.appendChild(cur);          // 光标跟到新段
  };

  // 每秒刷新等待时长。后端每 5 秒一个 tick，所以「15 秒没有任何事件」
  // 基本只可能是通道断了 —— 这是「慢」与「断」唯一能区分开的信号。
  const waiter = setInterval(() => {
    if (!isCur()) return;                    // 后台会话不折腾 DOM
    const now = Date.now();
    // 过程折叠头的实时耗时：收起时不刷，避免让用户以为折叠里还在动
    if (row._flowTime && row._flowWrap && row._flowWrap.isConnected
        && !row._flowWrap.classList.contains('collapsed')) {
      row._flowTime.textContent = '已 ' + Math.max(1, Math.round((now - streamStart) / 1000)) + 's';
    }
    // 自动重试的退避期：显示倒计时，别让「上游空转 15s」的提示抢在前面
    if (retryWait) {
      const left = Math.ceil((retryWait.until - now) / 1000);
      if (left > 0) {
        setLiveSec('等待 ' + left + 's');
        return;
      }
      retryWait = null;
    }
    const idle = now - lastEventAt;
    if (idle > 15000) {
      const sec = Math.round(idle / 1000);
      if (!staleShown) {
        staleShown = true;
        setLive('连接中断，正在自动重连');
        if (thinkBox) thinkBox._label.textContent = '连接中断，正在自动重连…';
        // 15 秒一个字节都没有（连 tick 都没有）：这条连接已经死了，
        // 继续 await reader.read() 会让 finally 永远不执行 —— 「正在输出…」残留、
        // 停止键也点不动。这里主动断开，交给 finally → keepRecovering 补状态。
        if (ac) {
          console.warn('[stream] 15s 无事件，断开会话流以触发恢复 sid=' + sid);
          try { ac.abort(); } catch (e) { /* 已断开 */ }
          localAborted = true;
        }
      }
      // 计时必须继续走，不能冻在第一次提示的 16s
      setLiveSec(sec + 's');
      if (thinkBox && !thinkBox._hasSummary) {
        thinkBox._label.textContent = '连接中断，正在自动重连… 已 ' + sec + 's';
      }
      return;
    }
    staleShown = false;
    // 连接活着（tick 在来），但一直没有思考 / 正文 / 工具：这才是「上游空转」。
    // 以前 15 秒提示用的是 lastEventAt，tick 每 5 秒续命，所以永远不触发。
    const contentIdle = now - lastContentAt;
    // 工具调用参数正在生成：状态条已经显示工具名 + 大小；超过 15s 没新分片再提示
    if (phase === 'toolgen') {
      if (contentIdle > 15000) {
        setLive('工具调用生成中');
        setLiveSec(Math.round(contentIdle / 1000) + 's');
      }
      return;
    }
    // 工具正在跑 / 等用户审批时不算「模型没产出」
    if (phase !== 'tool' && !waitingApproval && contentIdle > 15000) {
      if (!contentStaleShown) {
        contentStaleShown = true;
        setLive('模型尚未产出思考或正文');
      }
      setLiveSec(Math.round(contentIdle / 1000) + 's');
      if (thinkBox && !thinkBox._hasSummary) {
        thinkBox._label.textContent = '等待模型产出… ' + Math.round(contentIdle / 1000) + 's';
      }
      return;
    }
    contentStaleShown = false;
    if (phase !== 'think' && phase !== 'tool') { setLiveSec(''); return; }
    const sec = Math.max(1, Math.round((now - phaseAt) / 1000));
    setLiveSec(sec + 's');
    if (phase === 'think' && thinkBox && !thinkBox._hasSummary) {
      // 还没有思考摘要时才显示「思考中… Ns」；有摘要后标题保持摘要，秒数在状态条里
      thinkBox._label.textContent = '步骤 ' + (thinkBox._step || 1) + ' · 思考中… ' + sec + 's';
    } else if (phase === 'tool' && row._toolGroup) {
      const t = row._toolGroup.el.querySelector('.tool-group-title');
      if (t) t.textContent = (row._toolGroup.el._titleBase || '') + ' · 已 ' + sec + 's';
    }
  }, 1000);
  // 计时器与前台监听一起收：流结束（done / 异常 / 恢复）后不再重复判定
  const stopWaiter = () => {
    clearInterval(waiter);
    document.removeEventListener('visibilitychange', onVisible);
  };
  document.addEventListener('visibilitychange', onVisible);

  // fetch 的中断句柄：停止超时 / 连接长时间无事件时主动断开，
  // 保证 finally 一定执行（清掉「正在输出…」），而不是无限 await reader.read()。
  const ac = (typeof AbortController === 'function') ? new AbortController() : null;
  let localAborted = false;     // 本端主动断开：不计入「请求失败」文案
  let busyRejected = false;     // 服务端回 409（已在生成 / 正在压缩）：本条没有落库
  let busyMessage = '';         // 409 的服务端说明（提示用户为什么不发）
  // 非 2xx 的明确拒绝（400/403/404/500…）：请求根本没被受理，后台不会有任务在跑。
  // 不能把它当成「连接提前结束」去挂 attach 轮询，否则界面会假进入「后台运行中」。
  let httpRejected = false;
  let attachGone = false;       // 接管模式：服务端已没有运行中的任务
  if (stRef) {
    stRef.abort = () => {
      localAborted = true;
      try { if (ac) ac.abort(); } catch (e) { /* 已断开 */ }
    };
  }

  try {
    let res;
    if (attach) {
      // 接管模式：不发送新消息，只从 since 回放服务端已产生的事件，再继续跟随到 done
      res = await fetch('/api/chat/attach', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Auth-Token': TOKEN },
        signal: ac ? ac.signal : undefined,
        body: JSON.stringify({ session_id: sid, since: attach.since || 0 }),
      });
      if (!res.ok || !res.body) {
        // 404 = 没有可接管的任务（已经结束 / 已过期）：按正常收尾，不往正文写错误
        const gone = new Error('没有正在运行的任务');
        gone.__attachGone = true;
        throw gone;
      }
    } else {
      res = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Auth-Token': TOKEN },
        signal: ac ? ac.signal : undefined,
        body: JSON.stringify(Object.assign({
          session_id: state.sessions.current,
          think_enabled: !!state.thinkEnabled,
          think_level: state.thinkLevel || 'high',
          search_enabled: !!state.searchEnabled,
          ui_context: currentUiContext() || undefined,
          problems: (extra && (
            (typeof extra.content === 'string' && extra.content.indexOf('@problems') >= 0)
            || (Array.isArray(extra.refs) && extra.refs.some((r) => r && r.type === 'problems'))
          )) ? currentProblems() : undefined,
        }, extra || {})),
      });
      if (!res.ok) {
        let msg = 'HTTP ' + res.status;
        httpRejected = true;
        try {
          const j = await res.json();
          if (j && j.message) msg = j.message;
          else if (j && j.error === 'busy') msg = '该会话正在生成中，请稍候。';
          // 409 = 这一条根本没进后端：本地追加的用户消息是幽灵数据，
          // 收尾时要按服务端会话重画，并把「生成中 + 停止键」恢复出来。
          if (res.status === 409 || (j && j.error === 'busy')) {
            busyRejected = true;
            busyMessage = msg;
          }
        } catch (e) { /* 非 JSON 响应，忽略 */ }
        throw new Error(msg);
      }
      setSendBusy(false);              // 后端已受理（拿到响应头），发送按钮复原
    }
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let pos;
      while ((pos = buf.indexOf('\n\n')) >= 0) {
        const chunk = buf.slice(0, pos);
        buf = buf.slice(pos + 2);
        const line = chunk.replace(/^data:\s*/, '');
        if (!line) continue;
        let obj;
        try { obj = JSON.parse(line); } catch (e) { continue; }
        lastEventAt = Date.now();          // 任何事件都算「后端还活着」
        if (stRef) stRef.lastEvent = lastEventAt;   // 手动压缩据此判断「本轮是否还在跑」
        if (obj.type !== 'tick') {
          lastContentAt = Date.now();      // tick 不算内容：空心跳不能掩盖「没有产出」
          contentStaleShown = false;
          waitingApproval = false;
        }
        if (obj.type === 'tick') {
          // 后端存活心跳（每 5 秒一条）：只用来刷新 lastEventAt，不渲染
        } else if (obj.type === 'recalling') {
          // 记忆检索（装配阶段，通常 <200ms；检索池大或模型挑选时可能几秒）
          if (!row._recallTip) {
            row._recallTip = document.createElement('span');
            row._recallTip.className = 'recognize-tip';
            row._recallTip.textContent = '正在检索记忆…';
            c.appendChild(row._recallTip);
          }
        } else if (obj.type === 'doc_parsing') {
          docChipRender(obj, 'parsing');
          const _cnt = obj.count ? ('（' + ((Number(obj.index) || 0) + 1) + '/' + obj.count + '）') : '';
          setLive('正在解析附件' + _cnt + '｜' + (obj.name || '附件'));
          scrollToBottom();
        } else if (obj.type === 'doc_progress') {
          // 逐页进度：长 PDF / 扫描件解析可能要几十秒，状态条必须持续动，否则像卡死
          docChipRender(obj, 'progress');
          if (obj.total) {
            const stageLabel = ({ text: '读取', render: '渲染', ocr: '识别' })[obj.stage] || '解析';
            setLive('正在' + stageLabel + '附件 ' + obj.done + '/' + obj.total + ' 页｜' +
                    (obj.name || ''));
          }
        } else if (obj.type === 'doc_ok') {
          docChipRender(obj, obj.error ? 'error' : 'ok');
          if (obj.warn && !obj.error) toast('附件已读入：' + obj.warn);
          scrollToBottom();
        } else if (obj.type === 'recognizing') {
          const tip = document.createElement('span');
          tip.className = 'recognize-tip';
          tip.innerHTML = icon('image')
            + (obj.mode === 'vision' ? ' 正在识别图片内容…' : ' 正在看图…');
          // 注意：typing 是 row._wrap 的子节点，不在 c 里；以前 c.insertBefore(tip, typing)
          // 会直接抛 NotFoundError，把整条流打断 —— 表现为发完图片后界面静默、只能切会话重看。
          c.appendChild(tip);
          scrollToBottom();
        } else if (obj.type === 'searching') {
          if (!sourcesBox) {
            sourcesBox = buildSearchBox();
            row._wrap.insertBefore(sourcesBox, row._wrap.firstChild);
          }
          if (sourcesBox._planLine) sourcesBox._planLine.textContent = '已判断：需要联网';
          else searchStep(sourcesBox, '已判断：需要联网');
          scrollToBottom();
        } else if (obj.type === 'search_queries') {
          if (!sourcesBox) {
            sourcesBox = buildSearchBox();
            row._wrap.insertBefore(sourcesBox, row._wrap.firstChild);
          }
          const qs = (obj.queries || []).map((s) => String(s));
          searchStep(sourcesBox, '已生成 ' + qs.length + ' 个检索式：' + qs.join(' / '));
        } else if (obj.type === 'search_hits') {
          if (sourcesBox) searchStep(sourcesBox, '命中 ' + obj.count + ' 条来源');
        } else if (obj.type === 'search_fetching') {
          if (sourcesBox) sourcesBox._fetchLine = searchStep(sourcesBox, '读取网页 0/' + obj.total + '…');
        } else if (obj.type === 'search_read') {
          if (sourcesBox) {
            if (sourcesBox._fetchLine) {
              sourcesBox._fetchLine.textContent = '读取网页 ' + obj.done + '/' + obj.total;
            }
            searchStep(sourcesBox,
              (obj.ok ? '✓ ' : '⚠ ') + (obj.title || obj.url) +
              (obj.ok ? '' : '（正文抓取失败，改用摘要）'),
              obj.ok ? '' : 'warn');
          }
        } else if (obj.type === 'search_reuse') {
          if (!sourcesBox) {
            sourcesBox = buildSearchBox();
            row._wrap.insertBefore(sourcesBox, row._wrap.firstChild);
          }
          searchStep(sourcesBox, '复用上一轮已有的来源，未重复联网');
          appendSearchSources(sourcesBox, obj.items);
          scrollToBottom();
        } else if (obj.type === 'sources') {
          if (!sourcesBox) {
            sourcesBox = buildSearchBox();
            row._wrap.insertBefore(sourcesBox, row._wrap.firstChild);
          }
          appendSearchSources(sourcesBox, obj.items);
          scrollToBottom();
        } else if (obj.type === 'search_error') {
          // 同一轮里可能已经搜过一两次：只加一行提示，别把整张卡片（含前面搜到的来源）删掉
          if (sourcesBox) {
            searchStep(sourcesBox, '⚠ ' + obj.message, 'warn');
            scrollToBottom();
          } else {
            const note = document.createElement('div');
            note.className = 'search-note';
            note.textContent = obj.message;
            row._wrap.insertBefore(note, row._wrap.firstChild);
            scrollToBottom();
          }
        } else if (obj.type === 'usage') {
          row._usageData = obj.usage;
          if (!row._usageEl) {
            row._usageEl = document.createElement('div');
            row._usageEl.className = 'usage-line';
            row._wrap.appendChild(row._usageEl);
          }
          setUsage(row._usageEl, obj.usage, row._doneAt,
                   usageCalls({ timing: row._timing || {} }), row._timing || null);
        } else if (obj.type === 'timing') {
          // 请求链路埋点：分段耗时（load / assemble / doc / compress / first_token / total），
          // 只输出到控制台供排查，不打扰界面；同时留给用量行标「N 次调用累计」。
          row._timing = obj.timing || null;
          if (window.console && console.debug) console.debug('[timing]', obj.timing);
        } else if (obj.type === 'step') {
          // 每一步开始：上一步的思考收尾、工具组换新，过程按时间线往下排
          clearToolPending();
          if (thinkBox) closeThink('步骤 ' + (thinkBox._step || 1) + ' · 已思考');
          if (row._toolGroup) autoCollapseToolGroup(row._toolGroup);
          row._toolGroup = null;
          row._toolStep = obj.n || 0;
          if (row._recallTip) { row._recallTip.remove(); row._recallTip = null; }
          // 上一轮的正文与这一轮之间留一个空行，最终 markdown 才不会粘成一段
          if (acc && acc.slice(-2) !== '\n\n') { acc += '\n\n'; pushText('\n\n'); }
          thinkBox = buildThinkBlock('', true);
          thinkBox._step = obj.n || 0;
          thinkBox._label.textContent = '步骤 ' + (obj.n || 1) + ' · 思考中…';
          setLive('第 ' + (obj.n || 1) + ' 步 · 思考中');
          setLiveSec('');
          phase = 'think';
          phaseAt = Date.now();
          ensureFlow().appendChild(thinkBox);
          followProcessStep(thinkBox);
        } else if (obj.type === 'subagent_start') {
          const s = subagentRow(obj.id, obj.group);
          if (s) {
            s.t0 = Date.now();
            s.el.style.setProperty('--depth', String(Math.max(0, Number(obj.depth) || 0)));
            const role = s.el.querySelector('.subagent-role');
            role.textContent = (obj.name && obj.name !== obj.role)
              ? obj.name
              : (SUB_ROLE[obj.role] || obj.name || (obj.role ? String(obj.role) : '子代理'));
            role.dataset.role = obj.role || 'reader';
            const askEl = s.el.querySelector('.subagent-ask');
            askEl.textContent = obj.ask || '';
            askEl.title = obj.ask || '';
            s.el.querySelector('.subagent-step').textContent =
              (obj.fork && obj.prefix_tokens)
                ? ('继承上下文 ' + Math.round(obj.prefix_tokens / 1000) + 'k …')
                : '开始…';
            subagentSummary(s.group);
            if (s && s.el) followProcessStep(s.el);
            else scrollToBottom();
          }
        } else if (obj.type === 'subagent_step') {
          const s = subagentOf(obj);
          if (s && s.el) {
            s.el.querySelector('.subagent-step').textContent =
              (obj.tool || '') + (obj.target ? ' · ' + obj.target : '');
          }
        } else if (obj.type === 'subagent_done') {
          const s = subagentOf(obj);
          if (s && s.el) {
            const ok = !!obj.ok;
            s.done = true;
            s.fail = !ok;
            s.el.classList.remove('running');
            s.el.classList.add(ok ? 'done' : 'fail');
            s.el.querySelector('.subagent-ico').textContent = ok ? '✓' : '✕';
            const secs = ((obj.ms || 0) / 1000).toFixed(1) + 's';
            s.el.querySelector('.subagent-time').textContent = secs;
            s.el.querySelector('.subagent-step').textContent = ok
              ? ('完成 · ' + secs + ' · ' + (obj.rounds || 0) + ' 轮 · '
                 + (obj.tools || 0) + ' 次工具 · ' + (obj.chars || 0) + ' 字')
              : (obj.error || '失败');
            // 展开后的指标栏：只放这一行真正有的数，不补 0 占位
            const mt = [['轮次', obj.rounds || 0], ['工具', obj.tools || 0],
                        ['字数', obj.chars || 0]];
            if (obj.tokens) mt.push(['token', obj.tokens]);
            if (obj.evidence != null) mt.push(['证据分', obj.evidence]);
            if (obj.missing) mt.push(['缺失引用', obj.missing]);
            s.el.querySelector('.subagent-metrics').innerHTML = mt.map((x) =>
              '<span class="subagent-metric"><b>' + escapeHtml(String(x[1])) + '</b>'
              + escapeHtml(String(x[0])) + '</span>').join('');
            const res = s.el.querySelector('.subagent-result');
            res.textContent = ok ? String(obj.preview || '') : String(obj.error || '');
            subagentSummary(s.group);
          }
        } else if (obj.type === 'subagent_all') {
          const box = subagentBoxes[SUB_GROUP(obj.group)];
          if (box) {
            const title = box.querySelector('.subagent-title');
            if (title) title.textContent = SUB_GROUP(obj.group) + ' · ' + (obj.n || 0) + ' 个';
            const sum = box.querySelector('.subagent-sum');
            if (sum) {
              sum.textContent = '完成 ' + (obj.ok != null ? obj.ok : 0) + '/'
                + (obj.n || 0) + ' · ' + (((obj.ms || 0) / 1000).toFixed(1)) + 's'
                + (obj.tokens ? ' · ' + obj.tokens + ' token' : '')
                + (obj.accept === false ? ' · 验收未过' : '');
            }
            box.classList.add('all-done');
          }
        } else if (obj.type === 'auto_continue') {
          // 自动续跑：在过程流里留一条轻提示，让用户知道不是卡住了，是它自己在往下做
          const tip = document.createElement('div');
          tip.className = 'auto-continue';
          tip.textContent = '自动续跑 ' + obj.n + '/' + (obj.limit < 0 ? '∞' : obj.limit)
            + (obj.why ? ' · ' + obj.why : '');
          ensureFlow().appendChild(tip);
          followProcessStep(tip);
        } else if (obj.type === 'reasoning') {
          retryWait = null;
          if (!thinkBox) {
            thinkBox = buildThinkBlock('', true);
            thinkBox._step = row._toolStep || 1;
            thinkBox._label.textContent = '步骤 ' + thinkBox._step + ' · 思考中…';
            ensureFlow().appendChild(thinkBox);
          }
          thinkBox._body.textContent += obj.text;
          liveReasoning(live, obj.text, reasoningRetry);
          reasoningRetry = false;
          refreshThinkLabel(thinkBox, false);   // 标题 / 状态条显示「最近完成段落」的首行
          phase = 'output';          // 思考内容在流，不再需要秒数
          followProcessStep(thinkBox);
        } else if (obj.type === 'base') {
          // 接管续写任务时的补帧：续写起点正文（上一轮已写好的部分）先摆出来，
          // 否则新行只显示本次新增的段落，看起来像「上一轮输出丢了」
          const bt = String(obj.text || '');
          if (!acc && bt) {
            acc = bt;
            segStart = acc.length;       // 续写起点是上一轮的答案，不能被当成过程旁白折走
            row._raw = bt;
            revealPos = acc.length;      // 续写起点整段直接可见，不做逐字动画
            liveText(live, bt);
            pushText(bt);
            scrollToBottom();
          }
        } else if (obj.type === 'delta') {
          retryWait = null;
          if (thinkBox) closeThink('步骤 ' + (thinkBox._step || 1) + ' · 已思考');
          setLive('正在输出');
          setLiveSec('');
          phase = 'output';
          acc += obj.text;
          liveText(live, obj.text);
          pushText(obj.text);
          scrollToBottom();
        } else if (obj.type === 'replace') {
          // 已经收到过 error 时不接受替换：否则真正的报错会被收尾文案盖掉
          const _rt = String(obj.text || '');
          if (!sawError && (_rt.trim() || !acc.trim())) serverFinalText = _rt;
        } else if (obj.type === 'warn') {
          // 收尾提醒：回答被截断 / 本轮没有正文（以前是无声结束，看不到原因）
          const t = (acc ? '\n\n' : '') + obj.message;
          acc += t;
          liveText(live, t);
          pushText(t);
          scrollToBottom();
        } else if (obj.type === 'rewind') {
          // 整步重试：把本次失败尝试已经流出来的正文从界面与 acc 里回滚。
          // 只回滚「本次尝试」的字符数，前面成功轮次的内容保留。
          const n = Math.max(0, parseInt(obj.chars, 10) || 0);
          if (n > 0 && acc) {
            acc = acc.slice(0, Math.max(0, acc.length - n));
            if (segStart > acc.length) segStart = acc.length;
            // 只重绘「当前正文段」：segStart 之前的旁白已经搬进过程折叠，
            // 从这里重放会把它们又画一遍到正文区。
            revealPos = Math.min(Math.max(revealPos, segStart), acc.length);
            liveTrim(live, n);
            serverFinalText = null;
            if (liveEnabled && liveBox) {
              liveRendered = segStart;
              liveBox.innerHTML = '';
              tailBox.innerHTML = '';
              paintLive();
            } else if (textNode) {
              textNode.data = acc;
            }
          }
        } else if (obj.type === 'retry') {
          // 重试不清空：上一次尝试的思考折叠成「上次尝试（上游中断）」留在过程里，
          // 新一次尝试另起一个思考块（下一条 reasoning 会创建）。
          clearToolPending();
          if (thinkBox) {
            const text = thinkBox._body.textContent || '';
            if (!text.trim()) {
              thinkBox.remove();
            } else {
              foldTransition(thinkBox, thinkBox._body, false, {
                cls: 'open', positive: true, ms: 180,
              });
              const head = thinkLineSummary(text, true);
              thinkBox._hasSummary = !!head;
              thinkBox._label.textContent = '上次尝试（上游中断）'
                + (head ? ' · ' + head : '');
            }
            thinkBox = null;
          }
          reasoningRetry = true;
          phase = 'think';
          phaseAt = Date.now();
          const mode = String(obj.mode || 'normal');
          const waitMs = Math.max(0, Number(obj.delay_ms) || 0);
          retryWait = waitMs ? { until: Date.now() + waitMs } : null;
          showRetryNote(obj);
          const why = obj.why ? '上游中断（' + obj.why + '）' : '上游中断';
          const times = mode === 'always'
            ? ('第 ' + (obj.n || 1) + ' 次')
            : ((obj.n || 1) + '/' + (obj.limit || 1));
          setLive(why + '，正在重试 · ' + times);
          setLiveSec(waitMs ? ('等待 ' + Math.ceil(waitMs / 1000) + 's') : '');
          lastContentAt = Date.now();
          contentStaleShown = false;
        } else if (obj.type === 'model_wait') {
          // 请求已发出、还没拿到响应头：这段时间只有心跳，必须明确说在等什么
          phase = 'think';
          phaseAt = Date.now();
          setLive('等待上游响应…');
          setLiveSec(obj.timeout ? ('最长 ' + obj.timeout + 's') : '');
          lastContentAt = Date.now();
          contentStaleShown = false;
        } else if (obj.type === 'model_ready') {
          clearRetryNote();
          setLive('上游已响应，正在生成…');
          setLiveSec('');
          lastContentAt = Date.now();
          contentStaleShown = false;
        } else if (obj.type === 'end_reason') {
          // 结束原因：不拼进正文，收尾时单独渲染成一条提示（带「继续输出 / 重试」）
          endReason = obj.reason || null;
        } else if (obj.type === 'error') {
          clearRetryNote();
          clearToolPending();
          compressPendingClear();
          phase = 'idle';
          sawError = true;
          // 错误只放在实时状态条与「结束原因」里，不进正文：
          // 重试成功时正文保持干净；整轮真失败时由收尾逻辑补回（见 finally）。
          lastErrText = String(obj.message || '');
          setLive(lastErrText);
          scrollToBottom();
        } else if (obj.type === 'approval') {
          // 写入审批：后端已挂起工具调用，等这里点「允许 / 拒绝」
          if (typeof buildApprovalCard === 'function') {
            const card = buildApprovalCard(obj);
            // 审批卡属于「过程」，必须按事件顺序落在过程流里。
            // 以前追加到 row._wrap 末尾（在状态条之后），审批通过后的正文会插到它前面，
            // 卡片就被夹在对话中间；收尾重排时也无法归位。
            if (obj.aid) card.dataset.pendingApproval = String(obj.aid);
            ensureFlow().appendChild(card);
            followProcessStep(card);
          }
          setLive('等待你确认');
          setLiveSec('');
          keepLiveLast();
          waitingApproval = true;
          phase = 'idle';              // 等用户点「允许 / 拒绝」
          scrollToBottom();
        } else if (obj.type === 'workflow') {
          // 工作流：节点状态 + 暂停 / 继续 / 取消（卡片由 chat.js 负责）
          if (typeof workflowRender === 'function') workflowRender(row, obj);
          scrollToBottom();
        } else if (obj.type === 'plan') {
          // 执行计划：模型通过 update_plan / plan_patch 维护，独立成一张清单卡
          livePlan(live, obj.plan || obj);
          if (typeof planRender === 'function') planRender(row, obj);
          scrollToBottom();
        } else if (obj.type === 'plan_review') {
          // 计划审批：后端已挂起，等这里点「批准并执行 / 按意见重排」
          if (typeof planReviewRender === 'function') planReviewRender(row, obj);
          setLive('计划待你批准');
          setLiveSec('');
          keepLiveLast();
          waitingApproval = true;
          phase = 'idle';
          scrollToBottom();
        } else if (obj.type === 'plan_review_done') {
          if (typeof planReviewDone === 'function') planReviewDone(row, obj);
          scrollToBottom();
        } else if (obj.type === 'acceptance_review') {
          // 验收子代理的判定卡（独立 AI 打分 + 证据 + 差距）
          if (typeof acceptanceReviewRender === 'function') {
            acceptanceReviewRender(row, obj.report || {});
          }
          scrollToBottom();
        } else if (obj.type === 'acceptance_challenge') {
          // 主模型申诉 → 验收代理裁决（接受降级 / 拒绝则交用户裁决）
          if (typeof acceptanceChallengeRender === 'function') {
            acceptanceChallengeRender(row, obj);
          }
          scrollToBottom();
        } else if (obj.type === 'plan_revised') {
          // 计划完成判据中途修订：原值 → 新值 + 原因
          if (typeof planRevisedRender === 'function') {
            planRevisedRender(row, obj);
          }
          scrollToBottom();
        } else if (obj.type === 'review_tool') {
          // 验收代理正在做核对（读文件 / 跑命令），只是状态提示
          setLive('验收代理正在核对 · ' + String(obj.name || '工具'));
          setLiveSec('');
          scrollToBottom();
        } else if (obj.type === 'tool_stream') {
          // 原生工具调用参数正在生成：整轮结束前先把名字 / 大小显示出来。
          // 否则界面只有「思考中」或「尚未产出」，用户不知道它在写工具参数。
          foldSegmentToProcess(obj.name);     // 正文后紧跟工具：先把这段旁白折进过程区
          if (!toolPending) {
            toolPending = document.createElement('div');
            toolPending.className = 'tool-pending';
            ensureFlow().appendChild(toolPending);
          }
          const name = obj.name || '工具';
          const many = (obj.calls || 0) > 1 ? ' · ' + obj.calls + ' 个调用' : '';
          toolPending.textContent = '正在生成 ' + name + ' 参数 · '
            + fmtBytes(obj.bytes) + many;
          setLive('正在生成工具调用 ' + name + ' · ' + fmtBytes(obj.bytes));
          setLiveSec('');
          phase = 'toolgen';
          followProcessStep(toolPending);
        } else if (obj.type === 'script_log') {
          // Code Mode 脚本的实时日志：只更新状态条，不写进正文
          setLive('脚本 · ' + String(obj.text || '').slice(0, 60));
        } else if (obj.type === 'tool') {
          foldSegmentToProcess(obj.name);     // 兜底：没有 tool_stream 的供应商在这里折
          clearToolPending();
          // Skill：独立常驻卡（像子代理卡一样留在正文外），不进过程折叠。
          // live 数据仍记一条，切会话 / 刷新重建时 appendSkillCards 能还原。
          if (isSkillTool(obj.name) && isCur()
              && typeof buildSkillCard === 'function') {
            const sk = buildSkillCard(obj.name, obj.args);
            row._wrap.insertBefore(sk, typing);
            const sq = (row._skillQueue = row._skillQueue || {});
            (sq[obj.name] = sq[obj.name] || []).push(sk);
            const _li = liveTool(live, obj.name, obj.args);
            (liveToolQ[obj.name] = liveToolQ[obj.name] || []).push(_li);
            setLive('使用 Skill · ' + (skillArgName(obj.args) || '读取中'));
            setLiveSec('');
            scrollToBottom();
            continue;
          }
          // 普通对话走过程流（工具组直接进 flow），不再需要末尾的 tool-list 容器
          if (!row._flow && !row._toolsBox) {
            row._toolsBox = document.createElement('div');
            row._toolsBox.className = 'tool-list';
            row._wrap.appendChild(row._toolsBox);
            keepLiveLast();
          }
          const _li = liveTool(live, obj.name, obj.args);
          (liveToolQ[obj.name] = liveToolQ[obj.name] || []).push(_li);
          const card = buildToolCard(obj.name, obj.args, true);
          const g = toolGroup(row);        // 归进「步骤组」，标题实时更新
          // 新步骤自动展开：上一步收起的组如果又被复用，先展开再放卡片
          if (g.collapseTimer) { clearTimeout(g.collapseTimer); g.collapseTimer = 0; }
          if (g.el.classList.contains('collapsed')) setToolGroupOpen(g, true, false);
          g.body.appendChild(card);
          g.pending++;
          // 记下当前步骤，组头显示「正在修改 xxx」而不是笼统的「运行中…」
          g.current = { name: obj.name,
                        arg: (typeof toolArgText === 'function'
                              ? toolArgText(obj.name, obj.args) : '') };
          toolGroupTitle(g);
          markProcessStep(card);           // 当前步骤高亮（并行时多张同时亮）
          row._toolQueue = row._toolQueue || {};
          (row._toolQueue[obj.name] = row._toolQueue[obj.name] || []).push(card);
          const _lact = (typeof toolArgText === 'function')
            ? toolArgText(obj.name, obj.args) : '';
          setLive('调用 ' + obj.name + (_lact ? ' · ' + _lact : ''));
          setLiveSec('');
          phase = 'tool';              // 工具执行期间只有这一步在跑，标题里显示已用秒数
          phaseAt = Date.now();
          followProcessStep(card);    // 只把新步骤带进视野，不强行贴整条消息底部
        } else if (obj.type === 'tool_output') {
          // 命令执行中的增量输出：找当前还在跑的那张卡（同名按队列取最早的）
          const q = row._toolQueue && row._toolQueue[obj.name];
          const card = q && q.length ? q[0] : null;
          if (card && typeof appendToolOutput === 'function') {
            appendToolOutput(card, obj.text);
          }
        } else if (obj.type === 'tool_done') {
          const _sq = row._skillQueue && row._skillQueue[obj.name];
          if (_sq && _sq.length && typeof finishSkillCard === 'function') {
            finishSkillCard(_sq.shift(), obj);
            const _si = liveToolQ[obj.name];
            if (_si && _si.length) liveToolDone(live, _si.shift(), obj);
            scrollToBottom();
            continue;
          }
          const _lq = liveToolQ[obj.name];
          liveToolDone(live, _lq && _lq.length ? _lq.shift() : -1, obj);
          const q = row._toolQueue && row._toolQueue[obj.name];
          const card = q && q.length ? q.shift() : null;
          if (card) finishToolCard(card, obj);
          // 智能体打开预览：工具结果里带 GM_PREVIEW_URL 时，直接弹出 IDE 预览面板
          if (isCur() && typeof idePreviewOpenProxy === 'function') {
            const m = /GM_PREVIEW_URL:(\S+)/.exec(String(obj.result || ''));
            if (m) idePreviewOpenProxy(m[1]);
          }
          if (row._toolGroup) {              // 只有普通对话会建这个组
            const g = row._toolGroup;
            g.pending = Math.max(0, g.pending - 1);
            g.done++;
            g.ms += parseInt(obj.ms, 10) || 0;
            g.kinds[obj.name] = (g.kinds[obj.name] || 0) + 1;
            if (obj.ok === false) g.failed++;
            if (obj.write) row._wroteTurn = true;      // 供「回滚本轮」按钮判断
            toolGroupTitle(g);
            // 这一组都跑完了：延迟收起已完成的步骤（有失败 / 用户手动展开过时
            // autoCollapseToolGroup 自己会拒绝，错误始终留在视线里）
            if (g.pending === 0) {
              if (g.collapseTimer) clearTimeout(g.collapseTimer);
              g.collapseTimer = setTimeout(() => {
                g.collapseTimer = 0;
                if (g.pending === 0) autoCollapseToolGroup(g);
              }, 1200);
            }
          }
          // 同一批还有工具在跑就继续计时；全跑完则回到「等模型」
          setLive((row._toolGroup && row._toolGroup.pending > 0) ? '并行执行中' : '继续思考');
          phase = (row._toolGroup && row._toolGroup.pending > 0) ? 'tool' : 'think';
          phaseAt = Date.now();
          ideOnToolDone(obj, row, sid);
          if (!card) scrollToBottom();
          else followProcessStep(card);
        } else if (obj.type === 'diagram') {
          // archify 产物：对话内嵌一张可交互图卡（URL 免鉴权、仅本机可访问）
          const item = obj.item || {};
          if (item.url && row._wrap && typeof buildDiagramCard === 'function') {
            let dup = false;
            row._wrap.querySelectorAll('.diagram-card').forEach((n) => {
              if (n.dataset.url === item.url) dup = true;
            });
            if (!dup) {
              const card = buildDiagramCard(item);
              card.dataset.url = item.url;
              row._wrap.appendChild(card);
              scrollToBottom();
            }
          }
        } else if (obj.type === 'ask') {
          if (!askedNotified && chatWindowAway()) {
            askedNotified = true;
            const qs = obj.questions || [];
            const title = qs.length > 1
              ? 'AI 有 ' + qs.length + ' 个问题等你回答' : 'AI 在等你回答';
            const body = String((qs[0] || {}).question || '').slice(0, 90);
            if (typeof notifyOnce === 'function') {
              notifyOnce(obj.qid ? 'ask:' + obj.qid : '', title, body, 'ask',
                         { sessionId: sid });
            } else if (typeof showSystemNotify === 'function') {
              showSystemNotify(title, body, 'ask', { sessionId: sid });
            }
          }
          const askMeta = obj.expires_in
            ? { expiresAt: Date.now() + Math.max(1, Number(obj.expires_in) || 0) * 1000 }
            : {};
          liveAsk(live, liveTool(live, 'ask_user', {}), obj.questions, obj.qid);
          const askCard = buildAskGroup(obj.questions, obj.qid, askMeta);
          if (obj.qid) askCard.dataset.pendingAsk = String(obj.qid);
          splitAtAsk(askCard);      // 卡片留在提问发生的那个位置
          setLive('等待你回答');
          setLiveSec('');
          keepLiveLast();
          phase = 'idle';              // 在等用户回答，不该继续显示「思考中 120s」
          scrollToBottom();
        } else if (obj.type === 'mem_write') {
          memNote = obj.memory;
        } else if (obj.type === 'context') {
          renderContextBar(obj.context);
        } else if (obj.type === 'compressing') {
          compressPending();
        } else if (obj.sessions) {
          applySessionsPayload(obj.sessions);
          if (obj.type === 'done') {
            clearRetryNote();
            gotDone = true;
            if (typeof obj.msg_idx === 'number') doneMsgIdx = obj.msg_idx;
            row._doneAt = obj.done_at || row._doneAt;
            const timingNow = obj.timing || row._timing || null;
            // 没有 usage（例如本轮没产出 token）时也要显示「完成耗时」：只建一行即可
            if (!row._usageEl && (row._usageData
                || (timingNow && Number(timingNow.total_ms) > 0))) {
              row._usageEl = document.createElement('div');
              row._usageEl.className = 'usage-line';
              row._wrap.appendChild(row._usageEl);
            }
            if (row._usageEl && (row._usageData || timingNow)) {
              // 输出完成时间 + 完成耗时挂在缓存命中后面：排查「这轮什么时候结束 / 花了多久」
              setUsage(row._usageEl, row._usageData || null, row._doneAt,
                       usageCalls({ timing: timingNow || {} }), timingNow);
            }
            clearToolPending();
            phase = 'idle';
            doneBlocks = Array.isArray(obj.blocks) ? obj.blocks : null;
            doneTiming = obj.timing || null;
            doneTools = Array.isArray(obj.tools) ? obj.tools : null;
            if (thinkBox) closeThink();          // 最后一个思考块落定，标题换成摘要
            foldLiveProcess();                   // 整个过程折成「过程 · 工作 X 秒」
            // 本轮查到的文件 / 代码引用：自动检索的放在回答下方；
            // 显式引用已经在用户气泡上，这里用服务端解析结果校正一次（刷新前后一致）。
            if (typeof setUserRefs === 'function') {
              const prev = row.previousElementSibling;
              if (prev && prev.classList && prev.classList.contains('user')) {
                setUserRefs(prev, (obj.references || []).filter((r) => r && !r.auto));
              }
            }
            if (obj.references && obj.references.length
                && typeof buildReferencesBlock === 'function' && !row._refsBlock) {
              const autoRefs = obj.references.filter((r) => r && r.auto);
              if (autoRefs.length) {
                row._refsBlock = buildReferencesBlock(autoRefs);
                row._wrap.appendChild(row._refsBlock);
              }
            }
            // 本轮结束：把工具轨迹折成一行摘要（用户手动展开过则保持展开）
            if (row._toolGroup) autoCollapseToolGroup(row._toolGroup);
            if (typeof planAutoCollapse === 'function') planAutoCollapse(row);
            // 变更审阅：把本轮写过的文件一次性交给用户确认（撤销 / 保留）
            if (typeof ideReviewEndTurn === 'function') ideReviewEndTurn(row, false, sid);
          }
          renderSessions();
          if (obj.context && isCur()) renderContextBar(obj.context);
        }
      }
    }
  } catch (e) {
    if (localAborted || (e && e.name === 'AbortError')) {
      // 本端主动断开（停止超时 / 15s 无事件）：不是请求失败，不要往正文里塞错误文案。
      streamError = null;
      console.warn('[stream] 已断开本地流 sid=' + sid
        + (stRef && stRef.hardStop ? '（停止超时兜底）' : '（连接无响应）'));
    } else if (busyRejected) {
      // 409：这条请求后端根本没接收，正文不该出现「请求失败」占位。
      streamError = null;
    } else if (attach && e && e.__attachGone) {
      // 接管时任务已经收尾：不写错误文案，直接拉状态对齐
      attachGone = true;
      streamError = null;
    } else {
      streamError = e;
      acc += (acc ? '\n\n' : '') + '请求失败：' + e;
    }
  } finally {
    if (stRef && stRef.stopTimer) { clearTimeout(stRef.stopTimer); stRef.stopTimer = 0; }
    stopWaiter();
    setSendBusy(false);              // 任何异常路径都别把发送按钮留在转圈态
    if (paintTimer) { clearTimeout(paintTimer); paintTimer = 0; }
    flushReveal();                   // 收尾前把还没「写」出来的字一次补齐，避免结尾缺字
    if (c && c.classList) c.classList.remove('streaming');
    const stopped = !!(stRef && stRef.stopping);
    if (typing.parentNode) typing.remove();
    const tip = c.querySelector('.recognize-tip');
    if (tip) tip.remove();
    cur.remove();
    (row._secs || [c]).forEach((n) => { n.style.whiteSpace = ''; });
    clearToolPending();
    if (thinkBox) closeThink();
    if (!gotDone) foldLiveProcess();     // 流异常没收到 done 时也要收尾
    if (flow) {
      // 收尾时把还开着的思考块统一折起；只替换默认标题，别误伤思考摘要
      flow.querySelectorAll('.think').forEach((b) => {
        foldTransition(b, b.querySelector('.think-body'), false, {
          cls: 'open', positive: true, ms: 180,
        });
        const lb = b.querySelector('.think-label');
        if (lb && (/^步骤 \d+ · 思考中…$/.test(lb.textContent)
                   || lb.textContent === '深度思考中…')) {
          lb.textContent = lb.textContent.replace('思考中…', '已思考');
        }
      });
    }
    // 整轮只拿到错误、没有任何正文：把最后一条错误补进正文，避免空气泡。
    // 重试成功时 acc 已有正文，这里不会触发，中途那条错误就不会残留成独立段落。
    if (!String(acc || '').trim() && !String(serverFinalText || '').trim() && lastErrText) {
      acc = lastErrText;
    }
    const shown = serverFinalText != null ? serverFinalText : acc;
    // 后台会话在 slim 数据里没有 messages，不能拿它算下标 / 建操作条
    const bg = !isCur();
    const mine = state.sessions.sessions.find((x) => x.id === sid) || null;
    let idx;
    if (shown.trim()) {
      row._raw = shown;
      // 重排 markdown 会改变高度，先记住「用户是否还在跟随」（不是当前位置是否贴底）
      const wasAtBottom = !bg && _followBottom;
      // V3：流结束时按服务端落库的 blocks 重排一次——思考 / 工具进过程折叠，
      // 正文按段落 + 提问卡按真实位置排开。后台会话不重绘。
      const rebuild = !bg && doneBlocks && doneBlocks.length;
      if (rebuild) {
        const wasOpen = !!(row._flowWrap && !row._flowWrap.classList.contains('collapsed'));
        const view = buildBlocksView(doneBlocks, doneTiming,
                                     (endReason && endReason.kind) || '',
                                     { tools: doneTools || [], open: wasOpen });
        if (row._flowWrap) row._flowWrap.remove();
        // 流式期间切出来的正文段 / 提问卡作废，统一按落库的块重排
        (row._secs || []).forEach((n) => n.remove());
        (row._asks || []).forEach((n) => n.remove());
        row._secs = [];
        row._asks = [];
        const tail = (row._usageEl && row._usageEl.parentNode === row._wrap)
          ? row._usageEl : null;
        const put = (n) => row._wrap.insertBefore(n, tail);
        if (view.fold) {
          put(view.fold);
          row._flowWrap = view.fold;
          row._flow = view.flow;
          row._flowHead = view.fold.querySelector('.process-fold-head');
          row._flowLabel = view.fold.querySelector('.process-fold-title');
          row._flowCaret = view.fold.querySelector('.process-fold-caret');
          if (window.enhanceRichMedia) window.enhanceRichMedia(view.fold);
        } else {
          row._flowWrap = null;
          row._flow = null;
          row._flowHead = null;
          row._flowLabel = null;
          row._flowCaret = null;
        }
        view.parts.forEach((p) => {
          put(p);
          if (p.classList.contains('ask-card')) row._asks.push(p);
          else row._secs.push(p);
        });
        if (row._secs.length) {
          c = row._secs[row._secs.length - 1];
          row._content = c;
        } else {
          // 没有可展示的正文段。若这一轮的正文全是过场旁白（已收进过程折叠），
          // 就在答案区留一个空节点；只有真的没有任何正文块时才退回全文。
          const hadText = (doneBlocks || []).some((x) => x && x.kind === 'text'
            && String(x.text || '').trim());
          const empty = document.createElement('div');
          empty.className = 'content';
          if (!hadText) {
            empty.innerHTML = safeRenderMarkdown(shown);
            if (typeof enhanceToc === 'function') enhanceToc(empty);
          }
          put(empty);
          c = empty;
          row._content = c;
          row._secs = [empty];
        }
      } else {
        // 没拿到块（断流 / 旧服务端）：正文合并回第一段，避免同一段文字在多段里重复。
        // 流式期间折进过程区的旁白也已经包含在 shown 里，先撤掉，别在正文和过程各显示一遍。
        if (row._flowWrap) {
          row._flowWrap.querySelectorAll('.content.narration').forEach((n) => n.remove());
          if (row._flow && !row._flow.childNodes.length) {
            row._flowWrap.remove();
            row._flowWrap = null;
            row._flow = null;
            row._flowHead = null;
            row._flowLabel = null;
            row._flowMeta = null;
            row._flowCaret = null;
          }
        }
        if ((row._secs || []).length > 1) {
          const first = row._secs[0];
          row._secs.slice(1).forEach((n) => n.remove());
          row._secs = [first];
          first.style.display = '';
          c = first;
          row._content = c;
        }
        c.innerHTML = safeRenderMarkdown(shown);
        if (typeof enhanceToc === 'function') enhanceToc(c);
      }
      // 后台探索批次：回合结束后仍在跑，单独成卡并轮询进度（不放进过程折叠）
      if (typeof appendExploreBatchCards === 'function') {
        appendExploreBatchCards(row._wrap, doneTools);
      }
      // 图卡按最终顺序重排：清掉流式期间追加的，再统一挂到正文之后
      row._wrap.querySelectorAll('.diagram-card').forEach((n) => n.remove());
      if (typeof appendDiagramCards === 'function') {
        appendDiagramCards(row._wrap, doneTools);
      }
      // 多段正文都要走富媒体增强（公式 / 图表），所以按整条消息扫一遍
      if (window.enhanceRichMedia) window.enhanceRichMedia(row._wrap);
      idx = (!bg && mine && Array.isArray(mine.messages))
        ? mine.messages.length - 1 : undefined;
      if (typeof idx === 'number') row.dataset.idx = idx;
      if (!bg) {
        if (memNote) row._wrap.appendChild(buildMemoryChip(memNote, idx));
        row._wrap.appendChild(buildActions(row, () => row._raw, idx, 'ai'));
        refreshKbIfOpen(memNote && memNote.changed);
      }
      if (wasAtBottom) scrollToBottom(true);  // 原本贴底才重新贴底，消除「结尾跳一下」
    } else if (serverFinalText != null || !flow) {
      // 服务端校正后正文为空（只吐了裸工具 JSON）时也要清掉流式残留
      c.innerHTML = '<p style="color:var(--text-dim)">（无内容）</p>';
    }
    // 空正文 / 请求失败的中断轮：上面没有建操作条，这里补上。
    // 否则「历史压缩」圆环挂不到这条消息上——表现就是「只有完整输出完才能压缩」。
    if (!bg && !row._wrap.querySelector('.msg-actions')) {
      if (typeof idx !== 'number' && mine && Array.isArray(mine.messages)) {
        idx = mine.messages.length - 1;
      }
      if (typeof idx === 'number') row.dataset.idx = idx;
      row._raw = shown;
      row._wrap.appendChild(buildActions(row, () => row._raw || '', idx, 'ai'));
    }
    // 中断原因放在这条消息的最末尾
    if (!bg && endReason
        && typeof buildEndNote === 'function' && row._wrap) {
      row._wrap.appendChild(buildEndNote(
        endReason, typeof idx === 'number' ? idx : undefined));
    }
    // 文件改动声明：正文 / 过程折叠 / 操作条都落位之后再摆一次，保持在操作条上方
    if (!bg && typeof ideReviewCardRefresh === 'function') ideReviewCardRefresh();
    // 停止 / 断流时收不到 done：本轮已经写过的文件也要结算进审阅队列，
    // 否则 pending 留到下一轮，会和同一文件的下一次写入合并成一条（baseToken 串轮）。
    if (!bg && !gotDone && typeof ideReviewEndTurn === 'function') ideReviewEndTurn(row, true, sid);
    // 后台会话：丢掉 live host（结果已落盘，切回去会从消息重建），标未读
    const bgTitle = bg ? ((mine && mine.title) || '新对话') : '';
    unregisterStream(sid, !bg);
    if (bg) {
      state.unread[sid] = true;
      renderSessions();
    }
    // 409（会话已在生成 / 正在压缩）：本条没进后端，本地追加的用户消息是幽灵数据。
    // 撤掉它、把「生成中 + 停止键」恢复出来，再挂 attach 跟到真正那一轮结束。
    if (busyRejected) {
      if (host.parentNode) host.remove();
      unregisterStream(sid, false);
      if (!state.streamingSids) state.streamingSids = {};
      state.streamingSids[String(sid)] = true;
      renderMessages();
      renderSessions();
      refreshStreamingUI();
      toast(busyMessage || '该会话正在生成中，请稍候。', 'error');
      if (isCur()) keepRecovering(sid, new Error('会话正忙'), 24, false).catch(() => {});
      return;
    }
    // 后台提醒：所有终止都提醒，文案写结束原因（手动停止除外）；后台会话无论窗口是否前台都提醒。
    // 断流但任务还在服务端跑 → 不在这里提前报「请求失败」，等 keepRecovering 拿回真正原因再提醒。
    const deferNotify = !gotDone && isCur() && !httpRejected;
    if (!deferNotify) {
      notifyChatDone({
        text: shown,
        error: streamError || (!gotDone ? new Error('连接提前结束') : null),
        stopped: stopped,
        reason: endReason,
        title: bgTitle,
        sessionId: sid,
        messageIndex: doneMsgIdx,
      });
    }
    scrollToBottom();
    // 兜底同步：正常流程 done 事件已带回最新会话。
    // 没收到 done（断网 / 关页面 / 连接被掐）时，任务其实还在服务端跑：
    // 挂到 /api/chat/attach 等它结束再拉一次状态，而不是立刻拉一份还没落库的旧数据。
    // 手动停止后只盯 2 分钟（24 × 5s）：后端若真卡住，不能让它永远显示「生成中」。
    // 被服务端明确拒绝（非 2xx）时没有「后台任务」可等，只做一次状态对齐。
    if (!gotDone && isCur() && !httpRejected) {
      if (attachGone) {
        // 接管时任务已经结束：只拉一次状态对齐，不进恢复轮询
        waitJobThenRefresh(sid).catch(() => {});
      } else {
        keepRecovering(sid, streamError || new Error('连接提前结束'),
                       stopped ? 24 : 120, stopped).catch(() => {});
      }
    }
  }
}
