'use strict';

/* 设置弹窗 · misc.js
   输入框自适应 / 语音输入 / Agent 菜单 / 入口绑定
   原 web/settings.js 拆分而来：按 index.html 中的顺序加载，共享全局作用域。 */

/* ============================================================
   输入框自适应
   ============================================================ */
function autoGrow() {
  const el = $('#input');
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 190) + 'px';
  syncSendBtn();
}

/* ============================================================
   语音输入（本地实时流式识别：边说边出字 → 可选 AI 矫正 → 填输入框）
   默认目标是 Solo 主输入框；其它输入框（如大厅弹窗）调用 toggleMic 时
   传入自己的 inputId / btnId / onUI 即可复用同一套录音与识别。
   ============================================================ */
const STT_CHUNK_MS = 500;      // 每 0.5 秒把新录音发给后端做一次流式识别

const mic = {
  active: false, busy: false, gen: 0,
  stream: null, ctx: null, source: null, node: null, sink: null,
  pending: [], sampleRate: 48000, timer: null, pusher: null, startedAt: 0,
  sid: '', prefix: '', text: '', sending: false,
  inputId: '', btnId: '', onUI: null,   // 为空 = Solo 主输入框 / 主麦克风按钮
};

// 当前录音要写回的输入框与要改状态的按钮。
// 指定了 id 但元素已经不在了（弹窗关掉）就返回 null，绝不回落到 Solo 主输入框。
function micEl() {
  return mic.inputId ? document.getElementById(mic.inputId) : $('#input');
}

function micBtnEl() {
  return mic.btnId ? document.getElementById(mic.btnId) : $('#btn-mic');
}

// 不是 Solo 主输入框时，用宿主自己的按钮绘制逻辑
function micSetUI(on, secs) {
  if (mic.onUI) {
    try { mic.onUI(on, secs || 0, !!mic.busy); } catch (e) { /* 忽略 */ }
    return;
  }
  const btn = micBtnEl();
  if (!btn) return;
  btn.classList.toggle('recording', on);
  // 只在「麦克风 / 录音」状态切换时重写一次图标 —— 每帧重写 innerHTML 会把点击打断
  const want = on ? 'record' : 'mic';
  if (btn.dataset.ico !== want) {
    btn.dataset.ico = want;
    btn.innerHTML = icon(want);
  }
  btn.title = on ? ('录音中 ' + (secs || 0) + 's（点一下停止，随后自动矫正）')
    : '语音输入：点一下开始，边说边出字；再点一下停止';
}

function toggleMic(inputId, btnId, onUI) {
  if (mic.busy) return;                 // 启动/停止进行中，忽略连点
  if (mic.active) {
    // 另一个输入框正在录音时不抢麦
    if (inputId && (mic.inputId || 'input') !== inputId) return;
    return micStop();
  }
  return micStart({ inputId: inputId || '', btnId: btnId || '', onUI: onUI || null });
}

// 关掉指定输入框上的录音（弹窗关闭 / 切换模式时用）
function micStopIf(inputId) {
  if (!mic.active) return null;
  if ((mic.inputId || 'input') !== (inputId || 'input')) return null;
  return micStop();
}

// 语音降噪开关（在「设置 → 语音与朗读」）
function syncDenoisePill() {
  const c = $('#speech-denoise');
  if (c) c.checked = !!state.denoise;
}

function toggleDenoise(on) {
  state.denoise = (typeof on === 'boolean') ? on : !state.denoise;
  syncDenoisePill();
  // 这一项是「即时生效」：同步草稿与快照，避免它单独把设置页标成未保存
  if (draftCfg) draftCfg.speech_denoise = state.denoise;
  if (settingsSnapshot) {
    try {
      const snap = JSON.parse(settingsSnapshot);
      snap.config.speech_denoise = state.denoise;
      settingsSnapshot = JSON.stringify(snap);
    } catch (e) { /* 忽略 */ }
  }
  updateSettingsDirty();
  const cfg = Object.assign({}, state.config || {}, { speech_denoise: state.denoise });
  api('/api/config', cfg).then(() => { state.config = cfg; }).catch(() => {});
  toast(state.denoise ? '已开启语音降噪（嘈杂环境更准）' : '已关闭语音降噪');
}

// 把当前识别文本 + 录音前已有文字写回输入框
function setMicInput() {
  const input = micEl();
  if (!input) return;
  input.value = (mic.prefix ? mic.prefix + ' ' : '') + (mic.text || '');
  micAfterWrite(input);
}

// 写回后：Solo 主输入框要自适应高度；其它输入框派发 input 事件，
// 让宿主（如引导制作弹窗）把自己的草稿状态同步过去
function micAfterWrite(input) {
  const main = $('#input');
  if (input !== main) input.dispatchEvent(new Event('input', { bubbles: true }));
  if (input === main) autoGrow();
  try {
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
  } catch (e) { /* 忽略 */ }
}

async function sttStart(denoise) {
  const r = await api('/api/stt/start', { denoise: !!denoise });
  if (!r || !r.ok) throw new Error((r && r.error) || '无法开始识别');
  return r.sid;
}

async function sttChunk(sid, buf) {
  const res = await fetch('/api/stt/chunk', {
    method: 'POST',
    headers: { 'Content-Type': 'application/octet-stream', 'X-Auth-Token': TOKEN, 'X-Stt-Sid': sid },
    body: buf,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || !data.ok) throw new Error(data.error || ('HTTP ' + res.status));
  return data.text || '';
}

async function sttFinish(sid) {
  const r = await api('/api/stt/finish', { sid: sid });
  return (r && r.ok) ? (r.text || '') : '';
}

function _concatF32(chunks) {
  let total = 0;
  chunks.forEach((c) => { total += c.length; });
  const all = new Float32Array(total);
  let off = 0;
  chunks.forEach((c) => { all.set(c, off); off += c.length; });
  return all;
}

// 定时把累积的新录音送去识别
async function micPush() {
  if (!mic.active || !mic.sid || mic.sending) return;   // sid 未就绪时先攒着，不丢录音
  const gen = mic.gen;
  const pend = mic.pending;
  mic.pending = [];
  if (!pend.length) return;
  const pcm = encodePcm16(resample(_concatF32(pend), mic.sampleRate, 16000));
  mic.sending = true;
  try {
    const text = await sttChunk(mic.sid, pcm);
    if (gen === mic.gen && mic.active && text) { mic.text = text; setMicInput(); }
  } catch (e) {
    /* 单次失败忽略，下一轮继续 */
  } finally {
    mic.sending = false;
  }
}

async function micStart(opts) {
  if (mic.busy) return;
  const o = opts || {};
  const inputId = o.inputId || '';
  // 指定了输入框但元素不在（弹窗已关）就别录，免得把字写进 Solo 主输入框
  if (inputId && !document.getElementById(inputId)) return;
  mic.busy = true;
  const gen = ++mic.gen;
  mic.inputId = inputId;
  mic.btnId = o.btnId || '';
  mic.onUI = o.onUI || null;
  try {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      toast('当前浏览器不支持麦克风采集');
      return;
    }
    // 先要麦克风：权限弹窗立即出现，不被后端模型加载拖住
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (e) {
      toast('无法访问麦克风（请检查系统或浏览器权限）');
      return;
    }
    if (gen !== mic.gen) {
      try { stream.getTracks().forEach((t) => t.stop()); } catch (e) { /* 忽略 */ }
      return;
    }
    const AC = window.AudioContext || window.webkitAudioContext;
    const ctx = new AC();
    try { await ctx.resume(); } catch (e) { /* 忽略 */ }
    if (!ctx.createScriptProcessor) {
      try { stream.getTracks().forEach((t) => t.stop()); } catch (e) { /* 忽略 */ }
      try { ctx.close(); } catch (e) { /* 忽略 */ }
      toast('当前浏览器不支持录音采集方式');
      return;
    }
    const source = ctx.createMediaStreamSource(stream);
    const node = ctx.createScriptProcessor(4096, 1, 1);
    const sink = ctx.createGain();
    sink.gain.value = 0;                 // 静音，避免麦克风原声回放到扬声器
    mic.stream = stream; mic.ctx = ctx; mic.source = source; mic.node = node; mic.sink = sink;
    mic.pending = []; mic.sampleRate = ctx.sampleRate || 48000;
    mic.sid = ''; mic.text = ''; mic.sending = false;      // sid 稍后拿到（录音先缓冲，不丢）
    mic.prefix = (micEl() ? micEl().value : '').replace(/\s*$/, '');
    hideSttChip();
    const sidPromise = sttStart(state.denoise);            // 后端并行加载模型
    node.onaudioprocess = (e) => {
      mic.pending.push(new Float32Array(e.inputBuffer.getChannelData(0)));   // 复制一份
    };
    source.connect(node); node.connect(sink); sink.connect(ctx.destination);
    mic.active = true;
    mic.startedAt = Date.now();
    micSetUI(true, 0);
    mic.timer = setInterval(() => micSetUI(true, Math.round((Date.now() - mic.startedAt) / 1000)), 500);
    mic.pusher = setInterval(micPush, STT_CHUNK_MS);
    mic.busy = false;                    // 已在录音，允许停止 / 再点
    syncSendBtn();
    sidPromise.then((sid) => {
      if (gen === mic.gen && mic.active) mic.sid = sid;
    }).catch((e) => {
      if (gen === mic.gen && mic.active) { toast(e.message || String(e)); micStop(); }
    });
  } finally {
    mic.busy = false;
    if (!mic.active) {                   // 没起来（无权限 / 不支持）：清掉这次的目标
      mic.inputId = ''; mic.btnId = ''; mic.onUI = null;
    }
    syncSendBtn();
  }
}

async function micStop() {
  if (!mic.active || mic.busy) return;
  mic.busy = true;                       // 覆盖「识别 + 矫正」全程：期间禁用发送、不可重录
  const gen = ++mic.gen;                 // 作废所有旧的异步回写
  const onUI = mic.onUI;                 // 宿主自己的按钮绘制（弹窗等）
  const isMain = !mic.inputId;            // 是否是 Solo 主输入框
  mic.active = false;
  clearInterval(mic.timer); mic.timer = null;
  clearInterval(mic.pusher); mic.pusher = null;
  micSetUI(false, 0);
  try { mic.node.onaudioprocess = null; } catch (e) { /* 忽略 */ }
  try { mic.source.disconnect(); mic.node.disconnect(); mic.sink.disconnect(); } catch (e) { /* 忽略 */ }
  try { mic.stream.getTracks().forEach((t) => t.stop()); } catch (e) { /* 忽略 */ }
  const rate = mic.sampleRate;
  try { mic.ctx.close(); } catch (e) { /* 忽略 */ }
  const pend = mic.pending; mic.pending = [];
  mic.stream = mic.ctx = mic.source = mic.node = mic.sink = null;
  mic.sending = false;
  const sid = mic.sid; mic.sid = '';
  const prefix = mic.prefix;
  const btn = onUI ? null : micBtnEl();
  if (btn) { btn.classList.add('busy'); btn.title = '正在精修识别结果…'; }
  syncSendBtn();                         // 立即禁用发送键
  try {
    let text = '';
    if (pend.length) {                   // 把最后一段没送出的音频补上
      try { mic.text = (await sttChunk(sid, encodePcm16(resample(_concatF32(pend), rate, 16000)))) || mic.text; }
      catch (e) { /* 忽略 */ }
    }
    text = mic.text || '';
    toast('正在精修识别结果…');           // 两遍识别：离线模型对整段重解码
    try { const f = await sttFinish(sid); if (f) text = f; } catch (e) { /* 忽略 */ }
    text = (text || '').trim();
    mic.text = text;
    setMicInput();
    if (!text) { toast('没听清，请再说一次'); return; }
    // 可选：自动 AI 矫正（此阶段发送键仍禁用，避免「发出去的」与「矫正后的」两段文本并存）
    if (state.config && state.config.speech_polish) {
      toast('正在 AI 矫正…');
      try {
        const r = await api('/api/stt/polish', { text: text });
        if (gen === mic.gen && r && r.ok && r.text && r.text !== text) {
          const inp = micEl();
          if (inp) {
            inp.value = (prefix ? prefix + ' ' : '') + r.text;
            micAfterWrite(inp);
            if (isMain) showSttChip(text, prefix);   // 撤回原文的提示条只有 Solo 主输入框有
          }
        }
      } catch (e) { /* 矫正失败就用原文 */ }
    }
    const input = micEl();
    if (input) {
      input.focus();
      try { input.setSelectionRange(input.value.length, input.value.length); } catch (e) { /* 忽略 */ }
    }
  } finally {
    if (btn) btn.classList.remove('busy');
    mic.busy = false;
    mic.inputId = ''; mic.btnId = ''; mic.onUI = null;
    if (onUI) { try { onUI(false, 0, false); } catch (e) { /* 忽略 */ } } else micSetUI(false, 0);
    syncSendBtn();                       // 一切都结束了，按输入内容恢复发送键
  }
}

// 「AI 已矫正 · 撤回原文」提示条
function showSttChip(rawText, prefix) {
  const chip = $('#stt-chip');
  if (!chip) return;
  chip._raw = rawText;
  chip._prefix = prefix || '';
  chip.classList.remove('hidden');
}

function hideSttChip() {
  const chip = $('#stt-chip');
  if (chip) { chip.classList.add('hidden'); chip._raw = ''; chip._prefix = ''; }
}

function revertSttChip() {
  const chip = $('#stt-chip');
  if (!chip || chip._raw == null) return;
  const input = $('#input');
  input.value = (chip._prefix ? chip._prefix + ' ' : '') + chip._raw;
  autoGrow();
  input.focus();
  hideSttChip();
  toast('已恢复原始转写');
}

// 线性插值重采样
function resample(input, inRate, outRate) {
  if (!input.length || inRate === outRate) return input;
  const ratio = inRate / outRate;
  const outLen = Math.max(1, Math.floor(input.length / ratio));
  const out = new Float32Array(outLen);
  for (let i = 0; i < outLen; i++) {
    const pos = i * ratio;
    const i0 = Math.floor(pos);
    const i1 = Math.min(i0 + 1, input.length - 1);
    const frac = pos - i0;
    out[i] = input[i0] * (1 - frac) + input[i1] * frac;
  }
  return out;
}

// float32 [-1,1] → 16-bit 小端 PCM
function encodePcm16(f32) {
  const buf = new ArrayBuffer(f32.length * 2);
  const view = new DataView(buf);
  for (let i = 0; i < f32.length; i++) {
    const s = Math.max(-1, Math.min(1, f32[i]));
    view.setInt16(i * 2, s < 0 ? s * 0x8000 : s * 0x7FFF, true);
  }
  return buf;
}

/* ============================================================
   Agent：会话开关与沙箱目录
   ============================================================ */
/* Agent 设置浮层：沙箱目录 / 路径 / 写入与命令审批收在开关右侧的小箭头里，
   避免这 3 个胶囊和主开关一起把工具栏挤换行 */
function closeAgentMenu() {
  const menu = $('#agent-menu');
  const more = $('#btn-agent-more');
  if (menu) menu.classList.add('hidden');
  if (more) more.setAttribute('aria-expanded', 'false');
}

function toggleAgentMenu(force) {
  const menu = $('#agent-menu');
  const more = $('#btn-agent-more');
  if (!menu || !more) return;
  const open = (typeof force === 'boolean') ? force : menu.classList.contains('hidden');
  menu.classList.toggle('hidden', !open);
  more.setAttribute('aria-expanded', open ? 'true' : 'false');
}

/* 草案 / 首页还没有真实会话：Agent 开关先记在本地。
   发出第一条消息时 materializeDraft() 会把这份设置一起带进新会话。 */
function draftAgent() {
  if (!state.draftAgent) state.draftAgent = { enabled: false, sandbox: '' };
  return state.draftAgent;
}

/* 当前 Agent 开关作用在哪个对象上：真实会话，或草案的本地记录。 */
function agentTarget() {
  return curSession() || draftAgent();
}

function syncAgentUI() {
  const s = curSession();
  const on = s ? !!s.agent_enabled : !!draftAgent().enabled;
  const btn = $('#btn-agent');
  if (btn) btn.classList.toggle('active', on);
  // 设置入口只在 Agent 开启时出现；关掉 Agent 时顺手收起浮层
  const more = $('#btn-agent-more');
  if (more) more.classList.toggle('hidden', !on);
  if (!on) closeAgentMenu();
  const sandboxPath = s ? (s.sandbox || '') : (draftAgent().sandbox || '');
  const sb = $('#btn-sandbox');
  if (sb) {
    sb.classList.toggle('hidden', !on);
    const nm = $('#sandbox-name');
    if (nm) nm.textContent = sandboxPath
      ? (sandboxPath.split(/[\\/]/).filter(Boolean).pop() || sandboxPath) : '目录';
    sb.title = sandboxPath ? ('沙箱：' + sandboxPath + '（点击更换）')
      : '选择 Agent 沙箱目录（未选时只能用绝对路径读取文件）';
  }
  // 只有已经选了目录才需要「清空」；未选目录时隐藏
  const sbClear = $('#btn-sandbox-clear');
  if (sbClear) sbClear.classList.toggle('hidden', !on || !sandboxPath);
  // 沙箱目录右侧的「在文件管理器中打开」：没有目录时不显示
  const sbOpen = $('#btn-sandbox-open');
  if (sbOpen) {
    sbOpen.classList.toggle('hidden', !on || !sandboxPath);
    sbOpen.title = sandboxPath
      ? ('在文件管理器中打开：' + sandboxPath) : '在文件管理器中打开沙箱目录';
  }
  const pathBtn = $('#btn-sandbox-path');
  if (pathBtn) pathBtn.classList.toggle('hidden', !on);
  // 写入审批档位：勾选当前生效的那一项
  const ap = normApproval(s && s.approval_mode);
  document.querySelectorAll('#agent-menu .approve-opt').forEach((b) => {
    b.classList.toggle('active', b.dataset.mode === ap);
  });
  // 自动续跑：全局配置（不属于会话），只保留「关闭 / 无上限」两档
  const acRaw = parseInt((state.config || {}).agent_auto_continue, 10);
  const acv = (Number.isFinite(acRaw) && acRaw === 0) ? 0 : -1;
  document.querySelectorAll('#agent-menu .continue-opt').forEach((b) => {
    b.classList.toggle('active', parseInt(b.dataset.continue, 10) === acv);
    b.onclick = () => setAutoContinue(parseInt(b.dataset.continue, 10) || 0);
  });
}

/* 自动续跑：模型想收尾但任务没做完时，由后端自动推它继续。
   只保留两档：0 = 关闭，-1 = 无上限。 */
async function setAutoContinue(n) {
  const raw = parseInt(n, 10);
  const v = raw === 0 ? 0 : -1;
  const cfg = Object.assign({}, state.config || {}, { agent_auto_continue: v });
  try {
    await api('/api/config', cfg);
  } catch (e) { toast('设置失败：' + e); return; }
  state.config = cfg;
  syncAgentUI();
  toast(v === 0 ? '已关闭自动续跑' : '自动续跑：无上限');
}

/* 写入审批档位：ask 手动 / ai 自动 / full 完全访问 / custom 自定义。
   默认 ai：只读与普通命令放行，安装 / 联网 / 改历史类才问。
   旧值 auto / always 语义就是自动审批；once 按手动处理。 */
const APPROVAL_ALIASES = { auto: 'ai', always: 'ai', once: 'ask' };
const APPROVAL_MODES = ['ask', 'ai', 'full', 'custom'];
function normApproval(mode) {
  const k = String(mode || '').trim().toLowerCase();
  const v = APPROVAL_ALIASES[k] || k;
  return APPROVAL_MODES.indexOf(v) >= 0 ? v : 'ai';
}

async function setApprovalMode(mode) {
  const s = curSession();
  if (!s) return;
  const m = normApproval(mode);
  try {
    const r = await api('/api/session/agent', { id: s.id, approval_mode: m });
    applySessions(r);
  } catch (e) { toast('设置失败：' + e); syncAgentUI(); return; }
  syncAgentUI();
  if (m === 'ask') toast('手动审批：每次写入 / 执行命令前都会问你');
  else if (m === 'ai') toast('自动审批：只读与普通命令放行，安装 / 联网 / 改历史类才问你');
  else if (m === 'full') toast('完全访问：不拦截，命令白名单 / 内联执行 / 站点授权 / Git 提交策略都不再生效（仍限在沙箱内）');
  else {
    // 自定义：顺便把规则文件的状态说清楚，否则「选了没生效」很难排查
    try {
      const r = await api('/api/agent/rules', { session_id: s.id });
      if (!r.exists) toast('未找到 ' + (r.path || '.gm/agent-rules.json') + '：未命中规则时会照常询问');
      else if (r.error) toast('规则文件有问题：' + r.error);
      else toast('自定义规则已生效');
    } catch (e) { /* 提示失败不影响已切换的模式 */ }
  }
}

async function toggleAgent() {
  const s = curSession();
  if (!s) {
    // 首页 / 草案：先记在本地，等第一条消息落库时随会话一起创建
    const d = draftAgent();
    d.enabled = !d.enabled;
    syncAgentUI();
    if (d.enabled) {
      toast('已开启 Agent' + (d.sandbox ? '（新会话生效）'
        : '：未选目录时只能用绝对路径读取文件；写文件 / 执行命令请先选目录'));
    } else {
      toast('已关闭 Agent');
    }
    return;
  }
  const on = !s.agent_enabled;
  try {
    const r = await api('/api/session/agent', { id: s.id, agent_enabled: on });
    applySessions(r);
  } catch (e) { toast('切换 Agent 失败：' + e); return; }
  if (on) {
    // 不在这里自动弹目录选择框：那会与用户手动点 📁 叠加成两个窗口，也会让原本
    // 只想"先开着、稍后再选目录"的用户被强行弹窗打断。改成一句话指路。
    toast(!(s.sandbox || '')
      ? '已开启 Agent：未选目录时只能用绝对路径读取文件；写文件 / 执行命令请先选目录'
      : '已开启 Agent');
  } else {
    toast('已关闭 Agent');
  }
  syncAgentUI();
}

// 正在等系统目录选择框返回（防重复触发，见下方注释）
let _pickerBusy = false;

async function pickSandbox() {
  // 同一次选择过程只允许一个请求在飞：服务端虽已加互斥，但前端不挡的话
  // 用户连点会连发几个请求，每个都会多一次 toast、也让"到底哪个窗口是新的"更混乱。
  if (_pickerBusy) { toast('已经有一个「选择目录」窗口开着，请先在它里面选或取消'); return; }
  _pickerBusy = true;
  const btn = $('#btn-sandbox');
  if (btn) btn.classList.add('busy');
  toast('请在弹出的窗口中选择目录…');
  try {
    const cur = curSession();
    const r = await api('/api/sandbox/pick', {
      initial: cur ? (cur.sandbox || '') : draftAgent().sandbox,
    });
    if (r && r.ok && r.path) {
      if (!cur) {
        // 草案：先记本地，建会话时一并写入
        draftAgent().sandbox = r.path;
        syncAgentUI();
        toast('沙箱已设为：' + r.path + '（新会话生效）');
        return;
      }
      const res = await api('/api/session/agent', { id: state.sessions.current, sandbox: r.path });
      applySessions(res);
      syncAgentUI();
      toast('沙箱已设为：' + r.path);
    } else if (r && r.busy) {
      toast('已经有一个「选择目录」窗口开着，请先在它里面选或取消');
    } else {
      toast('未选择目录');
    }
  } catch (e) { toast('选择目录失败：' + e); }
  finally {
    _pickerBusy = false;
    if (btn) btn.classList.remove('busy');
  }
}

/* 在文件管理器里打开当前会话的沙箱目录（与分组头的小按钮共用同一个接口） */
async function openSandboxFolder() {
  const s = curSession();
  const path = (s && s.sandbox) || '';
  if (!path) { toast('还没有选择沙箱目录', 'error'); return; }
  try {
    await api('/api/open/folder', { path: path });
  } catch (e) {
    toastErr('打开文件夹', e);
  }
}

// 手动输入 / 粘贴沙箱路径：不想在旧版系统目录框里一层层点时用
async function pickSandboxPath() {
  const s = curSession();
  const raw = await appPrompt('输入或粘贴沙箱目录的绝对路径：',
    s ? (s.sandbox || '') : draftAgent().sandbox);
  if (raw === null) return;
  const path = raw.trim().replace(/^"(.*)"$/, '$1');
  if (!path) { toast('路径不能为空', 'error'); return; }
  try {
    const chk = await api('/api/sandbox/check', { path });
    if (!chk || !chk.ok || !chk.path) {
      toast('路径不存在或不是目录：' + path, 'error');
      return;
    }
    if (!s) {
      draftAgent().sandbox = chk.path;
      syncAgentUI();
      toast('沙箱已设为：' + chk.path + '（新会话生效）');
      return;
    }
    const res = await api('/api/session/agent', { id: s.id, sandbox: chk.path });
    applySessions(res);
    syncAgentUI();
    toast('沙箱已设为：' + chk.path);
  } catch (e) { toastErr('设置沙箱失败', e); }
}

/* 清空沙箱目录：回到「新对话未选目录」的状态。
   未选目录时仍可用绝对路径读取文件 / 看图；写入、删除、执行命令前要重新选目录。 */
async function clearSandbox() {
  const s = curSession();
  if (!s) {
    if (!draftAgent().sandbox) { toast('当前没有设置沙箱目录'); return; }
    draftAgent().sandbox = '';
    syncAgentUI();
    toast('已清空沙箱目录（新会话生效）');
    return;
  }
  if (!(s.sandbox || '')) { toast('当前没有设置沙箱目录'); return; }
  try {
    const res = await api('/api/session/agent', { id: s.id, sandbox: '' });
    applySessions(res);
  } catch (e) { toastErr('清空沙箱目录', e); return; }
  syncAgentUI();
  toast('已清空沙箱目录：现在只能用绝对路径读取文件；写入 / 执行命令前请重新选择');
}

/* ============================================================
   IDE / Solo 模式：以「当前会话沙箱」为根的工作区
   ============================================================ */

/* ============================================================
   设置入口绑定（原在 init.js 中）
   ------------------------------------------------------------
   放在本文件末尾，等 DOM 就绪后绑定入口与快捷键；
   这样设置相关的 DOM / 键盘逻辑集中在一处，不再和 init 的启动顺序耦合。
   ============================================================ */
(function bindSettingsEntry() {
  function bind() {
    const openBtn = document.querySelector('#btn-settings');
    if (openBtn) openBtn.onclick = () => openModal();
    const closeX = document.querySelector('#btn-close-modal');
    if (closeX) closeX.onclick = () => closeModal();
    const cancel = document.querySelector('#btn-cancel');
    if (cancel) cancel.onclick = () => closeModal();
    const save = document.querySelector('#btn-save');
    if (save) save.onclick = saveModal;
    setupSettingsSearch();
    document.addEventListener('keydown', onSettingsKeydown, true);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bind);
  else bind();
})();
