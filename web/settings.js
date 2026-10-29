'use strict';

/* ============================================================
   设置弹窗
   ============================================================ */
const PROVIDER_META = {
  chat: { title: '聊天模型', desc: '用于智能聊天与记忆读写', hint: '填写任意 OpenAI 兼容接口（地址 + 模型名 + 密钥）', protocol: false },
  vision: { title: '视觉模型', desc: '用于图像识别', hint: '支持 DashScope 原生协议，或 OpenAI 兼容的视觉接口', protocol: true },
};

// 上下文输入已迁到独立面板，这里保留元素引用供聊天预设读写
const chatCtx = { limitEl: null, reserveEl: null };

/* ---- 设置页：工作副本、脏状态、页签记忆与默认值 ---- */
let draftCfg = null;
let draftProfile = null;
let settingsSnapshot = '';
let settingsDirty = false;
let settingsDefaults = null;
let settingsCurrentTab = '';
let _dirtyTimer = null;

const SETTINGS_LAST_TAB_KEY = 'settings_last_tab';
const SETTINGS_SCROLL_KEY = 'settings_scroll_v1';

const SETTINGS_PAGE_META = {
  chat: ['模型接入', '聊天 / 视觉接口、推理参数、断流重试与账户余额'],
  search: ['联网与数据源', '搜索服务商、抓取策略，以及 B 站视频 / 评论读取'],
  context: ['上下文与压缩', '模型窗口、输出上限、压缩阈值与记忆挑选'],
  agent: ['Agent 执行', '轮次、并行、工具预算、写入审批与命令执行'],
  remind: ['主动与提醒', '定时提醒与主动聊天（按会话）'],
  profile: ['用户画像与学习', '全局规则、自动记录与学习指令'],
  skills: ['Skill', '按需展开的能力包'],
  appearance: ['主题与外观', '界面主题、IDE 主题与空白字符'],
  gal: ['GAL 演出', '演出主题、节奏、特效与自动剧本'],
  speech: ['语音与朗读', '语音输入、降噪与台词朗读'],
  keys: ['快捷键', '改键与完整快捷键说明'],
  data: ['数据与高级', '版本、数据目录、备份恢复与重置'],
  help: ['功能说明', '各项功能的实际作用与差异'],
};

/* 静态页的「恢复本页默认」作用范围；动态页由 _collect() 的键推导。
   密钥与自定义预设不在重置范围内。 */
const STATIC_RESET_KEYS = {
  profile: { profile: ['manual', 'auto_learn_enabled', 'learning_prompt_custom'] },
  appearance: { config: ['theme', 'ide_theme', 'ide_whitespace'] },
  gal: { config: [
    'gal_theme', 'gal_dialog_max', 'gal_ui_sfx', 'gal_nudge', 'gal_typewriter', 'gal_cps',
    'gal_fx', 'gal_sprite_fx', 'gal_bar_auto', 'gal_bgm_volume', 'gal_seg_mode', 'gal_auto',
    'gal_min_show', 'gal_think', 'gal_show_think', 'gal_show_exec', 'gal_show_stats',
    'gal_search', 'gal_hide_cursor', 'gal_auto_script', 'gal_auto_script_acts',
    'gal_auto_script_rounds',
  ] },
  speech: { config: [
    'speech_polish', 'speech_denoise', 'tts_enabled', 'tts_provider', 'tts_url',
    'tts_model', 'tts_voice',
  ] },
};

const SECRET_SETTING_KEY = /(^|_)(api_key|sessdata)$/;
const PRESET_SETTING_KEY = /(_presets|learning_presets)$/;
const RANGE_FIELDS = [
  ['gal-cps', 8, 120, 1],
  ['gal-dialog-max', 30, 70, 1],
  ['gal-bgm-volume', 0, 1, 0.1],
  ['gal-min-show', 0, 5, 0.1],
];

function settingsDraft() { return draftCfg || state.config || {}; }

/* ---- 设置面板：动态面板共用的排版片段（统一类名与搜索索引 data-search）---- */
function mkPane(name) {
  const p = document.createElement('section');
  p.className = 'pane hidden';
  p.dataset.pane = name;
  return p;
}
const setGroupHtml = (title, search) =>
  '<h4 class="set-group"' + (search ? ' data-search="' + search + '"' : '') + '>' + title + '</h4>';
const labelHtml = (text, search) =>
  '<label class="field-label"' + (search ? ' data-search="' + search + '"' : '') + '>' + text + '</label>';
const hintHtml = (text, search) =>
  '<p class="hint"' + (search ? ' data-search="' + search + '"' : '') + '>' + text + '</p>';
const checkRowHtml = (inputId, text, search) =>
  '<label class="check-row"' + (search ? ' data-search="' + search + '"' : '') + '>' +
  '<input type="checkbox" id="' + inputId + '"> ' + text + '</label>';
const qIn = (pane, id) => pane.querySelector('#' + id);

function buildApiPane(provider, host) {
  const meta = PROVIDER_META[provider];
  const cfg = settingsDraft();
  state.pendingPresets[provider] = Object.assign({}, cfg[provider + '_presets'] || {});

  // host 为空 = 单独一页；传进来 = 并入已有的「模型接入」页（chat + vision 共用一页）
  const pane = host || mkPane(provider);
  const prevCollect = pane._collect;
  const title = provider === 'chat' ? '聊天接口' : '视觉接口（识图）';
  let html = setGroupHtml(title, provider === 'chat'
    ? '聊天 主模型 接口 密钥' : '视觉 识图 图片 模型') +
    hintHtml('<strong>' + meta.title + '</strong> —— ' + meta.desc + '<br>' + meta.hint,
             '接口 地址 模型 密钥 预设 api');
  if (provider === 'vision') {
    html += setGroupHtml('使用方式', '识图 聊天模型 视觉');
    html += checkRowHtml('vision-prefer-api', '优先使用视觉 API（改用下面的独立视觉服务）', '优先 视觉 api 聊天模型');
    html += hintHtml('默认用聊天模型直接识图（图片直发，少一次单独调用）；'
      + '想改用下面这套独立的视觉 API 就勾上面的选项。'
      + '详见「功能说明 → 图像识别」。');
    html += setGroupHtml('接入', '接口 地址 模型 密钥 协议');
  } else {
    html += setGroupHtml('接入', '接口 地址 模型 密钥 预设');
  }
  if (meta.protocol) {
    html += labelHtml('接口协议', '协议 dashscope openai 兼容');
    html += '<div class="radio-row">' +
      '<label><input type="radio" name="vision-proto" value="native"> DashScope 原生</label>' +
      '<label><input type="radio" name="vision-proto" value="openai"> OpenAI 兼容</label>' +
      '</div>';
    html += hintHtml('原生：图片放 <code>input.messages</code>，结果取 <code>output.choices</code>（qwen-vl-plus / max）。<br>' +
                     '兼容：图片放 <code>messages</code>（<code>image_url</code>），结果取 <code>choices</code>（GPT-4o、vLLM/Ollama、deepseek-flash）。详见「功能说明 → 图像识别」。');
  }
  if (provider === 'chat') {
    html += labelHtml('服务商协议', '协议 provider anthropic claude 兼容') +
      '<select id="chat-kind" class="field-input">' +
      '<option value="">自动（按接口地址判断）</option>' +
      '<option value="openai">OpenAI 兼容（默认）</option>' +
      '<option value="anthropic">Anthropic 原生（Claude）</option>' +
      '</select>';
    html += hintHtml('Anthropic 原生走 <code>/v1/messages</code>，工具调用改用文本协议；'
                     + 'DeepSeek / 通义 / OpenRouter / 本地模型保持 OpenAI 兼容即可。'
                     + '地址里含 <code>anthropic</code> 时，选「自动」也会走原生协议。');
  }
  html += labelHtml('预设', '预设 另存 删除') +
    '<div class="preset-row"><select id="' + provider + '-preset"></select>' +
    '<button type="button" id="' + provider + '-preset-save">另存为预设</button>' +
    '<button type="button" id="' + provider + '-preset-del">删除预设</button></div>';
  html += labelHtml('接口地址', '接口 地址 url endpoint baseurl') +
    '<input id="' + provider + '-url" class="field-input" type="text">';
  html += labelHtml('模型名', '模型 model') +
    '<input id="' + provider + '-model" class="field-input" type="text">';
  html += labelHtml('API 密钥', '密钥 key api token 认证') +
    '<input id="' + provider + '-key" class="field-input" type="password">';
  html += '<label class="check-row"><input type="checkbox" id="' + provider + '-show-key"> 显示密钥</label>';
  if (provider === 'chat') {
    html += setGroupHtml('轻量任务的模型（可选）', '轻量 小模型 摘要 记忆挑选 分级 调度');
    html += hintHtml('摘要 / 记忆挑选 / GAL 剧情判定 / 语音矫正这类轻量调用，可以改用更小更快的模型；'
                     + '三项都留空 = 沿用上面的主模型（默认，行为与从前一致）。');
    html += labelHtml('轻量模型名', '轻量 小模型 model') +
      '<input id="light-model" class="field-input" type="text" placeholder="留空则沿用主模型">';
    html += labelHtml('轻量接口地址（可选）', '轻量 小模型 地址 url') +
      '<input id="light-url" class="field-input" type="text" placeholder="留空则沿用上面的接口地址">';
    html += labelHtml('轻量 API 密钥（可选）', '轻量 小模型 密钥 key') +
      '<input id="light-key" class="field-input" type="password" placeholder="留空则沿用上面的密钥">';

    html += setGroupHtml('推理参数', '深度思考 思考 cot 思维链 档位 reasoning');
    html += hintHtml('控制输入框左下「深度思考」开关与档位如何传给接口，避免不支持的接口报错。',
                     '深度思考 开关 档位 reasoning');
    html += labelHtml('思考参数模式', '模式 参数 thinking reasoning');
    html += '<select id="think-mode" class="field-input">' +
      '<option value="">自动（按接口地址判断）</option>' +
      '<option value="off">不发送</option>' +
      '<option value="openai">OpenAI 兼容（只发 reasoning_effort）</option>' +
      '<option value="deepseek">DeepSeek（thinking + reasoning_effort）</option>' +
      '</select>';
    html += '<details class="fold"><summary>各模式的含义</summary><div class="fold-body">' +
      '<p>DeepSeek：开→ 发 <code>thinking.enabled</code>，关→ 发 <code>thinking.disabled</code>；档位走 <code>reasoning_effort</code>（low / medium 会被 DeepSeek 映射为 high）。<br>' +
      'OpenAI 兼容：开→ 发 <code>reasoning_effort</code>，关→ 不发（该协议无法表达「禁用」）。<br>' +
      '「低 / 中 / 高 / 最高」档位的完整映射表见「功能说明 → 深度思考」。</p>' +
      '</div></details>';

    html += setGroupHtml('断流重试', '断流 重试 超时 静默 中断 自动');
    html += labelHtml('重试策略', '重试 策略 无限 always normal');
    html += '<select id="chat-retry-mode" class="field-input">' +
      '<option value="always">瞬时错误一直重试（推荐，可随时停止）</option>' +
      '<option value="normal">只重试有限次数</option>' +
      '</select>';
    html += hintHtml('「一直重试」只针对断流 / 空转 / 超时 / 空响应 / 限流 / 5xx；' +
                     '余额不足、密钥无效、上下文溢出不会无限重试。' +
                     '界面会显示第几次重试和等待秒数，随时可以点停止。',
                     '重试 策略 无限 always normal');
    html += labelHtml('单轮零产出时自动重试（0 = 不重试）', '断流 重试 超时 静默 自动');
    html += '<input id="chat-round-retries" class="field-input" type="number" min="0" step="1">';
    html += hintHtml('这一轮失败（请求超时 / 上游 90 秒无数据 / 连接错误）时，' +
                     '自动重发同一轮，最多几次（默认 2）。重试前会把这次尝试已经流出的正文回滚，' +
                     '避免拼出两段回答；失败尝试的工具调用不会执行。填 0 = 关闭。',
                     '断流 重试 超时 静默 自动 0');
    html += labelHtml('请求发不出去时自动重试（0 = 跟随上一项）', '断流 重试 上传 网络');
    html += '<input id="chat-upload-retries" class="field-input" type="number" min="0" step="1">';
    html += hintHtml('请求体还没发完就断（上行写超时 / 连接被掐）时最多重发几次（默认 4）。' +
                     '这种失败一个字都没产出，重发不会重复回答；断网时会先探活，' +
                     '链路恢复了才重传，不会反复上传大请求体。',
                     '断流 重试 上传 网络');
    html += labelHtml('空心跳判定（秒，0 = 关）', '空心跳 卡住 思考 正文 静默');
    html += '<input id="chat-stream-stall" class="field-input" type="number" min="0" step="5">';
    html += hintHtml('上游一直在发空 data / role-only / usage，但连续这么久没有思考或正文，' +
                     '就中止本轮并重试（默认 45 秒，0 = 关）。' +
                     '完全没有任何 data 仍按 chat_stream_idle_timeout（默认 90 秒）处理。',
                     '空心跳 卡住 思考 正文 静默');

    html += setGroupHtml('思考落库', '思考 reasoning 落库 历史 回看 体积 隐私');
    html += checkRowHtml('chat-persist-reasoning',
      '把思考存进会话（刷新后仍可回看）', '思考 落库 历史 回看');
    html += labelHtml('每条消息保留的思考上限（字，0 = 不限）', '思考 上限 字数 体积');
    html += '<input id="chat-reasoning-keep" class="field-input" type="number" min="0" step="1000">';
    html += hintHtml('思考只存本地 sessions.json，不会上传。默认每条 12,000 字；' +
                     '超出时丢掉最早的思考、保留最近的。关掉后刷新就看不到本次思考，' +
                     '正文与工具记录不受影响。', '思考 上限 体积 隐私');
  }
  html += setGroupHtml('账户余额', '余额 balance 查询 充值 赠金') +
    '<div class="balance-row"><span class="balance-text" id="' + provider + '-balance">未查询</span>' +
    '<button type="button" class="ghost" id="' + provider + '-balance-refresh">刷新</button></div>' +
    labelHtml('余额查询地址（可选；留空时按接口地址自动识别 DeepSeek）', '余额 balance 接口 地址') +
    '<input id="' + provider + '-balance-url" class="field-input" type="text" placeholder="https://api.deepseek.com/user/balance">';
  pane.insertAdjacentHTML('beforeend', html);

  const P = (suffix) => qIn(pane, provider + '-' + suffix);
  const q = P;
  q('url').value = cfg[provider + '_url'] || '';
  q('model').value = cfg[provider + '_model'] || '';
  q('key').value = cfg[provider + '_api_key'] || '';
  if (meta.protocol) {
    const val = cfg[provider + '_protocol'] || (provider === 'vision' ? 'native' : 'openai');
    const radio = pane.querySelector('input[name="vision-proto"][value="' + val + '"]');
    if (radio) radio.checked = true;
  }
  q('show-key').onchange = (e) => { q('key').type = e.target.checked ? 'text' : 'password'; };
  if (provider === 'chat') {
    const kd = pane.querySelector('#chat-kind');
    if (kd) kd.value = cfg.chat_kind || '';
    const lm = pane.querySelector('#light-model');
    const lu = pane.querySelector('#light-url');
    const lk = pane.querySelector('#light-key');
    if (lm) lm.value = cfg.light_model || '';
    if (lu) lu.value = cfg.light_url || '';
    if (lk) lk.value = cfg.light_api_key || '';
    const tm = pane.querySelector('#think-mode');
    if (tm) tm.value = cfg.chat_think_mode || '';
    const rr = pane.querySelector('#chat-round-retries');
    if (rr) rr.value = cfg.chat_round_retries != null ? cfg.chat_round_retries : 2;
    const rm = pane.querySelector('#chat-retry-mode');
    if (rm) rm.value = cfg.chat_retry_mode === 'normal' ? 'normal' : 'always';
    const ur = pane.querySelector('#chat-upload-retries');
    if (ur) ur.value = cfg.chat_upload_retries != null ? cfg.chat_upload_retries : 4;
    const st = pane.querySelector('#chat-stream-stall');
    if (st) st.value = cfg.chat_stream_stall_timeout != null ? cfg.chat_stream_stall_timeout : 45;
    const pr = pane.querySelector('#chat-persist-reasoning');
    if (pr) pr.checked = cfg.chat_persist_reasoning !== false;
    const rk = pane.querySelector('#chat-reasoning-keep');
    if (rk) rk.value = cfg.chat_reasoning_keep_chars != null ? cfg.chat_reasoning_keep_chars : 12000;
  }
  q('balance-url').value = cfg[provider + '_balance_url'] || '';
  q('balance-refresh').onclick = () => refreshBalance(provider, pane);
  if (provider === 'vision') {
    const pa = q('prefer-api');
    pa.checked = !!cfg.vision_prefer_api;
    // 视觉 API 始终是兜底通道，输入框保持可编辑
    setVisionFieldsDisabled(pane, false);
  }

  const refreshPresetSelect = (selected) => {
    const sel = q('preset');
    sel.innerHTML = '<option value="">（未选择）</option>';
    Object.keys(state.pendingPresets[provider]).forEach((name) => {
      const o = document.createElement('option');
      o.value = name; o.textContent = name;
      if (name === selected) o.selected = true;
      sel.appendChild(o);
    });
  };
  refreshPresetSelect('');

  q('preset').onchange = (e) => {
    const p = state.pendingPresets[provider][e.target.value];
    if (!p) return;
    q('url').value = p.url || '';
    q('model').value = p.model || '';
    q('key').value = p.api_key || '';
    if (provider === 'chat') {
      if (p.context_limit != null && chatCtx.limitEl) chatCtx.limitEl.value = p.context_limit;
      if (p.reserve_output != null && chatCtx.reserveEl) chatCtx.reserveEl.value = p.reserve_output;
    }
    if (meta.protocol && p.protocol) {
      const r = pane.querySelector('input[name="' + provider + '-proto"][value="' + p.protocol + '"]');
      if (r) r.checked = true;
    }
  };

  q('preset-save').onclick = async () => {
    const name = await appPrompt('预设名称');
    if (!name || !name.trim()) return;
    const n = name.trim();
    state.pendingPresets[provider][n] = {
      url: q('url').value.trim(), model: q('model').value.trim(), api_key: q('key').value.trim(),
    };
    if (provider === 'chat') {
      // 0 = 自动，不能被 || 兜底成 128000（否则预设会把「自动」存成固定窗口）
      state.pendingPresets[provider][n].context_limit = chatCtx.limitEl ? (parseInt(chatCtx.limitEl.value, 10) || 0) : 0;
      state.pendingPresets[provider][n].reserve_output = chatCtx.reserveEl ? (parseInt(chatCtx.reserveEl.value, 10) || 0) : 0;
    }
    if (meta.protocol) {
      const r = pane.querySelector('input[name="' + provider + '-proto"]:checked');
      state.pendingPresets[provider][n].protocol = r ? r.value : '';
    }
    refreshPresetSelect(n);
    toast('预设定为「' + n + '」，点保存生效');
  };

  q('preset-del').onclick = () => {
    const name = q('preset').value;
    if (!name) { toast('先选择要删除的预设'); return; }
    delete state.pendingPresets[provider][name];
    refreshPresetSelect('');
  };

  const collect = () => {
    const out = {};
    out[provider + '_url'] = q('url').value.trim();
    out[provider + '_model'] = q('model').value.trim();
    out[provider + '_api_key'] = q('key').value.trim();
    out[provider + '_presets'] = state.pendingPresets[provider];
    if (meta.protocol) {
      const r = pane.querySelector('input[name="' + provider + '-proto"]:checked');
      out[provider + '_protocol'] = r ? r.value : '';
    }
    out[provider + '_balance_url'] = q('balance-url').value.trim();
    if (provider === 'vision') {
      out.vision_prefer_api = !!q('prefer-api').checked;
    }
    if (provider === 'chat') {
      const lm = pane.querySelector('#light-model');
      const lu = pane.querySelector('#light-url');
      const lk = pane.querySelector('#light-key');
      out.light_model = lm ? lm.value.trim() : '';
      out.light_url = lu ? lu.value.trim() : '';
      out.light_api_key = lk ? lk.value.trim() : '';
      const tm = pane.querySelector('#think-mode');
      out.chat_think_mode = tm ? tm.value : '';
      const kd = pane.querySelector('#chat-kind');
      out.chat_kind = kd ? kd.value : '';
      const rr = pane.querySelector('#chat-round-retries');
      out.chat_round_retries = rr ? Math.max(0, parseInt(rr.value, 10) || 0) : 2;
      const rm = pane.querySelector('#chat-retry-mode');
      out.chat_retry_mode = rm && rm.value === 'normal' ? 'normal' : 'always';
      const ur = pane.querySelector('#chat-upload-retries');
      out.chat_upload_retries = ur ? Math.max(0, parseInt(ur.value, 10) || 0) : 4;
      const st = pane.querySelector('#chat-stream-stall');
      out.chat_stream_stall_timeout = st ? Math.max(0, parseInt(st.value, 10) || 0) : 45;
      const pr = pane.querySelector('#chat-persist-reasoning');
      out.chat_persist_reasoning = !pr || pr.checked;
      const rk = pane.querySelector('#chat-reasoning-keep');
      out.chat_reasoning_keep_chars = rk ? Math.max(0, parseInt(rk.value, 10) || 0) : 12000;
    }
    return out;
  };
  // 合并页：把本 provider 的字段并到前一个 provider 的收集结果上
  pane._collect = () => Object.assign({}, prevCollect ? prevCollect() : {}, collect());
  return pane;
}

/* 上下文与压缩面板 */
function buildContextPane() {
  const cfg = settingsDraft();
  const pane = mkPane('context');
  pane.innerHTML =
    hintHtml('这一页管三件事：模型一次能装多少内容（进度圆环与压缩时机）、'
             + '单次回答最多写多长、以及记忆怎么挑。填错不会报错，但压缩时机会不准。',
             '上下文 上限 token 压缩 估算 记忆') +
    setGroupHtml('容量', 'token 上限 预留 容量') +
    labelHtml('模型上下文窗口（tokens）', '上限 token 上下文 窗口 快捷 1m 128k') +
    '<input id="context-limit" class="field-input" type="number" min="0" step="1000">' +
    '<div class="ctx-presets" data-search="快捷 1M 200K 128K 64K 自动">' +
    '<button type="button" data-tok="0">自动</button>' +
    '<button type="button" data-tok="64000">64K</button>' +
    '<button type="button" data-tok="128000">128K</button>' +
    '<button type="button" data-tok="200000">200K</button>' +
    '<button type="button" data-tok="1000000">1M</button>' +
    '</div>' +
    hintHtml('模型一次能「看到」的总量（输入 + 输出都算）。点上面的快捷按钮直接填入：' +
             '64K / 128K / 200K / 1M。<strong>0 = 自动</strong>：按模型名匹配' +
             '（deepseek-flash / v4 / gemini 等 1M，deepseek-chat 128K，claude 200K）；' +
             '手填的值以你为准，既能放大也能收紧（上限 1M）。' +
             '手填的话：128K 填 <code>128000</code>，1M 填 <code>1000000</code>（1000 token ≈ 1700 汉字）。',
             '上限 token 窗口 快捷 自动') +
    labelHtml('其中留给 AI 回答（tokens）', '预留 token 回复 输出') +
    '<input id="context-reserve" class="field-input" type="number" min="0" step="256">' +
    hintHtml('先从窗口里划出这一块，作为「同步压缩」的硬预留；默认 16384，常用 8192–16384。' +
             '填 0 表示不预留。实际单次输出还会被 max_tokens 和窗口 25% 夹紧，' +
             '所以这里不需要按回答长度去精确填。调大 → 更早触发同步压缩。',
             '预留 token 回复 默认 16384') +
    labelHtml('单次输出上限 max_tokens（0 = 用服务商默认）', '输出 上限 max_tokens 截断 长度') +
    '<input id="chat-max-tokens" class="field-input" type="number" min="0" max="65536" step="1024">' +
    hintHtml('单次模型调用最多生成多少 token，**硬截断**（思考模式的 reasoning 也计入）。' +
             '设得太小，长回复会在写工具块之前被截断，表现为「话说到一半停了、没有执行工具」；' +
             '默认 8192。填 0 表示不传，由服务商决定。实际还会被夹到窗口的 25%，' +
             '避免「输入 + 输出」越过窗口。',
             '输出 上限 截断 长度 max_tokens') +
    '<p class="hint" id="ctx-calc" data-search="上下文 可用 80% 压缩 计算 换算"></p>' +
    '<details class="fold"><summary>填写说明与估算口径</summary><div class="fold-body">' +
    '<p><strong>两者的关系</strong>：模型窗口 = 系统提示与聊天历史 + 本次回答。很多模型把输入和输出算在同一个窗口里，' +
    '所以程序会先把单次输出夹到窗口的 25%，再额外留 8192 token 余量；' +
    '「预留」只用于发送前的同步压缩硬上限。</p>' +
    '<p><strong>程序什么时候压缩</strong>：</p>' +
    '<ul>' +
    '<li>已用达到<strong>可用提示词预算</strong>（窗口 80% 与「窗口 − 输出上限 − 余量」取小）：后台开始把较早的对话总结成摘要，本轮照常发送、不等待，下一轮生效。</li>' +
    '<li>发送前发现<strong>「已用 + 预留」超过窗口</strong>：同步再压一次；若压缩后仍超出，本轮只临时省略较早的消息再发送，' +
    '本地原文不会被删除。</li>' +
    '</ul>' +
    '<p><strong>估算口径</strong>：中文约 0.6 token/字、英文约 0.3 token/字，再用接口返回的真实 usage 校准，' +
    '所以百分比是近似值。</p>' +
    '<p><strong>填错的后果</strong>：窗口填得比真实值大 → 进度条偏低、压缩太晚，可能真的报超限错误；' +
    '填得比真实值小 → 压缩过早，浪费窗口。</p>' +
    '</div></details>' +
    setGroupHtml('压缩', '上下文 压缩 摘要 阈值 保留') +
    labelHtml('预压缩阈值（%，30–95）', '上下文 压缩 阈值') +
    '<input id="compress-ratio-pct" class="field-input" type="number" min="30" max="95" step="1">' +
    labelHtml('压缩后保留原文比例（%，5–60）', '压缩 保留 原文 比例') +
    '<input id="compress-keep-pct" class="field-input" type="number" min="5" max="60" step="1">' +
    labelHtml('摘要输出 token 上限（0 = 不限）', '摘要 输出 token') +
    '<input id="compress-summary-tokens" class="field-input" type="number" min="0" step="256">' +
    hintHtml('压缩边界会对齐到工具调用 / 结果配对；摘要按「请求 / 概念 / 文件 / 错误 / 待办 / '
             + '当前 / 下一步 / 上下文」结构化，并 pin 住最初请求与当前计划。',
             '压缩 边界 摘要 结构 pin 计划') +
    setGroupHtml('记忆检索', '记忆 检索 挑选 模型 延迟 首字节') +
    checkRowHtml('memory-llm-pick',
      '用模型挑选记忆（更准，但会拖慢首字节）', '记忆 挑选 模型 检索 延迟 首字节') +
    hintHtml('关闭（默认）：按关键词 + IDF 排序直接取最相关的 12 条。' +
             '打开后会在「首字节之前」多一次轻量模型往返，实测装配耗时 p90 1.9 秒、最坏 7 秒；' +
             '记忆条目很多、或常问「上次那个」这类模糊指代时再打开。',
             '记忆 挑选 延迟 首字节') +
    labelHtml('本会话记忆软上限（条，0 = 不限）', '记忆 上限 整理 合并 去重') +
    '<input id="memory-soft-limit" class="field-input" type="number" min="0" step="10">' +
    hintHtml('超过这个条数时，记忆提示会要求模型先合并同主题重复、删除过时条目、' +
             '给关键事件补关系，再新增。默认 60。',
             '记忆 上限 整理 合并 去重') +
    labelHtml('每多少轮提醒整理（0 = 关）', '记忆 整理 提醒 轮次') +
    '<input id="memory-organize-every" class="field-input" type="number" min="0" step="5">' +
    hintHtml('长任务里每 N 个工具轮注入一次「整理记忆」提醒；默认 20。' +
             '条目超过软上限、或关系数为 0 且条目超过 20 条时才会出现。',
             '记忆 整理 提醒 轮次') +
    labelHtml('整理时列出最近多少条（12–80）', '记忆 整理 列表 条数') +
    '<input id="memory-list-recent" class="field-input" type="number" min="12" max="80" step="4">' +
    hintHtml('超过软上限时，在记忆提示里列更多「ID | 标题」方便合并；默认 40。',
             '记忆 整理 列表 条数');
  const q = (id) => qIn(pane, id);
  const intOr = (v, d) => {
    const n = parseInt(v, 10);
    return Number.isFinite(n) ? n : d;
  };
  q('context-limit').value = cfg.chat_context_limit != null ? cfg.chat_context_limit : 0;
  q('context-reserve').value = cfg.chat_reserve_output != null ? cfg.chat_reserve_output : 16384;
  q('chat-max-tokens').value = cfg.chat_max_tokens != null ? cfg.chat_max_tokens : 8192;
  q('compress-ratio-pct').value = Math.round((cfg.compress_ratio || 0.8) * 100);
  q('compress-keep-pct').value = Math.round((cfg.compress_keep_ratio || 0.16) * 100);
  q('compress-summary-tokens').value = cfg.compress_summary_tokens != null
    ? cfg.compress_summary_tokens : 8192;
  q('memory-llm-pick').checked = !!cfg.memory_llm_pick;
  q('memory-soft-limit').value = cfg.memory_soft_limit != null ? cfg.memory_soft_limit : 60;
  q('memory-organize-every').value = cfg.memory_organize_every != null
    ? cfg.memory_organize_every : 20;
  q('memory-list-recent').value = cfg.memory_list_recent != null ? cfg.memory_list_recent : 40;
  chatCtx.limitEl = q('context-limit');
  chatCtx.reserveEl = q('context-reserve');

  // 实时算式：把两个抽象数字变成「历史最多能用多少 / 什么时候压缩」
  const calcEl = q('ctx-calc');
  const fmt = (n) => Number(n || 0).toLocaleString('zh-CN');
  const updateCalc = () => {
    const limit = parseInt(q('context-limit').value, 10) || 0;
    const reserve = Math.max(0, parseInt(q('context-reserve').value, 10) || 0);
    if (limit <= 0) {
      calcEl.textContent = '已设为「自动」：按模型名匹配窗口（匹配不到时按 128K 估算）。';
      return;
    }
    // 与后端 request_budget() 同一个口径：输出先按窗口 25% 夹紧，再留 headroom。
    const maxTok = Math.max(0, parseInt(q('chat-max-tokens').value, 10) || 0);
    const ratioOut = Number(cfg.chat_output_window_ratio) || 0.25;
    const headroom = Number(cfg.chat_output_headroom) || 8192;
    const byRatioOut = ratioOut > 0 ? Math.max(1024, Math.floor(limit * ratioOut)) : 0;
    const out = maxTok > 0 ? (byRatioOut > 0 ? Math.min(maxTok, byRatioOut) : maxTok) : byRatioOut;
    const pct = Math.max(30, Math.min(95, parseInt(q('compress-ratio-pct').value, 10) || 80));
    const floor = Math.max(512, Math.min(8000, Math.floor(limit / 2)));
    const budget = Math.max(floor,
      Math.min(Math.floor(limit * pct / 100), limit - out - headroom));
    const hard = Math.max(0, limit - reserve);
    calcEl.innerHTML = '单次输出实际上限 <strong>' + fmt(out) + '</strong> tokens（窗口的 25%）；' +
      '可用提示词预算 <strong>' + fmt(budget) + '</strong> tokens（窗口 ' + pct + '% 与「窗口 − 输出 − ' +
      fmt(headroom) + '」取小）；用到预算时后台预压缩，超过 <strong>' + fmt(hard) +
      '</strong> tokens（窗口 − 预留）时同步压缩。';
  };
  q('context-limit').oninput = updateCalc;
  // 快捷容量：一键填入常见窗口，避免去记 128000 / 1000000 这种数字
  pane.querySelectorAll('.ctx-presets button').forEach((b) => {
    b.onclick = () => {
      q('context-limit').value = b.dataset.tok || '0';
      updateCalc();
    };
  });
  q('context-reserve').oninput = updateCalc;
  q('chat-max-tokens').oninput = updateCalc;
  q('compress-ratio-pct').oninput = updateCalc;
  updateCalc();

  pane._collect = () => ({
    // 0 是合法值（按模型自动），不能被 || 兜底吃掉
    chat_context_limit: Math.max(0, intOr(q('context-limit').value, 0)),
    chat_reserve_output: Math.max(0, intOr(q('context-reserve').value, 0)),
    // 0 是合法值（不传 max_tokens），不能兜底成默认值
    chat_max_tokens: Math.max(0, Math.min(65536, intOr(q('chat-max-tokens').value, 0))),
    memory_llm_pick: !!q('memory-llm-pick').checked,
    // 0 是合法值（= 不限 / 关闭），不能兜底成默认值
    memory_soft_limit: Math.max(0, intOr(q('memory-soft-limit').value, 60)),
    memory_organize_every: Math.max(0, intOr(q('memory-organize-every').value, 20)),
    memory_list_recent: Math.max(12, Math.min(80, intOr(q('memory-list-recent').value, 40))),
    compress_ratio: Math.max(0.3, Math.min(0.95, intOr(q('compress-ratio-pct').value, 80) / 100)),
    compress_keep_ratio: Math.max(0.05, Math.min(0.6,
      intOr(q('compress-keep-pct').value, 25) / 100)),
    compress_summary_tokens: Math.max(0, intOr(q('compress-summary-tokens').value, 8192)),
  });
  return pane;
}

/* ---- Agent 执行上限：默认值与 app/agent.py 的常量一致，这里只放最常调的 7 项 ---- */
function buildAgentPane() {
  const cfg = settingsDraft();
  const intVal = (v, d) => {
    const n = parseInt(v, 10);
    return Number.isFinite(n) ? n : d;
  };
  const acRaw = intVal(cfg.agent_auto_continue, 3);
  const acNow = acRaw < 0 ? -1 : Math.max(0, Math.min(10, acRaw));   // -1 = 无上限
  const gitModeNow = ['off', 'ask', 'auto'].indexOf(String(cfg.git_commit_mode || '')) >= 0
    ? String(cfg.git_commit_mode) : 'ask';
  // max 省略（传 0）= 不写 max 属性：Agent 那几项上限由用户自己取舍，表单不再定天花板
  const numInput = (id, val, min, max) =>
    '<input id="' + id + '" class="field-input" type="number" min="' + min + '"' +
    (max ? ' max="' + max + '"' : '') + ' step="1" value="' + val + '">';
  // 0 = 不限：给个一键填入，省得记这个约定
  const limitRow = (id, val) =>
    '<div class="limit-row">' + numInput(id, val, 0, 0) +
    '<button class="limit-unli" type="button" data-unli="' + id +
    '" title="填 0 = 不限：不设上限，自己取舍">不限</button></div>';
  const acBtn = (v, icon, label, desc) =>
    '<button class="approve-opt continue-opt' + (acNow === v ? ' active' : '') +
    '" type="button" data-continue="' + v + '">' +
    '<span class="approve-opt-ico"><svg class="ic"><use href="#i-' + icon + '"/></svg></span>' +
    '<span class="approve-opt-text"><b>' + label + '</b><i>' + desc + '</i></span>' +
    '<span class="approve-opt-check"><svg class="ic"><use href="#i-check"/></svg></span>' +
    '</button>';
  const gitModeBtn = (v, icon, label, desc) =>
    '<button class="approve-opt gitmode-opt' + (gitModeNow === v ? ' active' : '') +
    '" type="button" data-gitmode="' + v + '">' +
    '<span class="approve-opt-ico"><svg class="ic"><use href="#i-' + icon + '"/></svg></span>' +
    '<span class="approve-opt-text"><b>' + label + '</b><i>' + desc + '</i></span>' +
    '<span class="approve-opt-check"><svg class="ic"><use href="#i-check"/></svg></span>' +
    '</button>';

  const pane = mkPane('agent');
  pane.innerHTML =
    hintHtml('这些是 Agent 自主执行时的「安全阀」：调大能让它一次做完更多事，但更费 token，' +
             '也更容易一口气改出大范围改动。下面这几项都能填 0 = 不限，上限由你自己取舍。',
             'agent 安全阀 上限 默认 不限 0') +
    setGroupHtml('执行节奏', '执行 节奏 轮次 循环 自动继续') +
    labelHtml('单轮最多调用轮次', '轮次 循环 上限 工具调用 不限') +
    limitRow('agent-max-rounds', intVal(cfg.agent_max_rounds, 0)) +
    hintHtml('一次提问里最多循环多少次「调工具 → 再问模型」（默认 0 = 不限）。' +
             '填 0 = 不限：它会一直做到自己收尾、你点停止，或撞上每日 token 预算。', '轮次 循环 上限 不限') +
    labelHtml('重复调用拦截', '重复 原地打转 拦截 不限') +
    limitRow('agent-repeat-limit', intVal(cfg.agent_repeat_limit, 5)) +
    hintHtml('同一工具、同一组参数连续调用到第几次就拦掉（默认 5），避免它原地打转。' +
             '中间换过一次别的调用就重新计数；填 0 = 不拦截。', '重复 原地打转 拦截 不限') +
    '<div class="agent-approval-cap" data-search="自动续跑 继续 继续输出">' +
    '自动续跑（任务没做完时自己继续）</div>' +
    '<div class="agent-approval">' +
    acBtn(0, 'alert', '关闭', '每轮结束就停，等你说「继续」') +
    acBtn(3, 'refresh', '最多 3 次', '推荐：连续开发') +
    acBtn(5, 'refresh', '最多 5 次', '长任务') +
    acBtn(-1, 'refresh', '无上限', '一直推到做完') +
    '</div>' +
    hintHtml('模型想收尾、但计划里还有没做完的步骤（或结尾在问「要不要继续」）时，' +
             '由程序自己推它往下做。工具条里的同名选项与这里是同一份配置。',
             '自动续跑 继续 计划') +
    labelHtml('阶段总结放行阈值（字，0 = 关闭）', '总结 收尾 续跑 阈值 字数') +
    limitRow('agent-summary-stop', intVal(cfg.agent_summary_stop_chars, 180)) +
    hintHtml('一段正文达到这个字数、且带「已完成 / 结果 / 目前 / 验证通过」这类总结信号时，' +
             '允许本轮先收尾（给「继续输出」按钮），不再强制续跑。' +
             '一句「继续。S1：核对…」不会触发。默认 180。', '总结 收尾 续跑 阈值') +
    checkRowHtml('agent-meta-turn-stop',
      '提问轮先回答（为什么 / 进度 / 是否完成），不自动续跑',
      '提问 元问题 进度 中断 自动续跑') +
    setGroupHtml('自动化（Autopilot）', '自动化 autopilot 提问 策略 重启 恢复 看门狗 验收 评审') +
    labelHtml('遇到提问卡 / 需要拍板时', '提问 策略 自动化 自主 决策 提问卡') +
    '<select id="autonomy-question-policy" class="field-input">' +
    '<option value="smart">smart（可逆自决，不可逆 / 缺外部信息问我）</option>' +
    '<option value="auto">auto（一律自决，不阻塞）</option>' +
    '<option value="ask">ask（一律问我）</option>' +
    '</select>' +
    hintHtml('只对自动化目标生效；普通聊天仍是「一律问」。auto 也不会执行删除 / 覆盖 / '
             + '发布 / 付款 / 改凭据这类不可逆操作，而是选可回滚的最小方案。',
             '提问 策略 自动化 自主 决策 不可逆') +
    checkRowHtml('autopilot-verify',
      '没有机检命令时用独立评审员判定完成', '自动化 验收 评审 完成 判定') +
    hintHtml('评审员用新的上下文，只看目标、完成定义和原始证据（文件 / 命令输出 / '
             + '工作区改动），不看模型自己的自述；判定不了的项不会算通过。',
             '自动化 验收 评审 完成 证据 自述') +
    checkRowHtml('autopilot-resume-on-start',
      '应用重启后自动恢复未完成的自动化目标', '自动化 重启 恢复 续跑 断点') +
    hintHtml('开启后，进程重启 / 崩溃后会把 running 的目标接回线程；'
             + '关闭则统一转为暂停，等你手动恢复。', '自动化 重启 恢复 续跑 断点') +
    checkRowHtml('autopilot-use-budget',
      '启用回合 / 时长硬上限（关闭时用无进展看门狗）', '自动化 预算 回合 时长 上限 看门狗') +
    hintHtml('关闭（默认）时不按固定回合数硬刹，只在连续多回合没有实际进展时干预；'
             + '需要严格控制成本时再打开。', '自动化 预算 回合 时长 上限 看门狗') +
    labelHtml('无进展看门狗（连续几回合无实际进展就干预，0 = 关闭）',
              '自动化 看门狗 无进展 停滞 回合') +
    limitRow('autopilot-watchdog-turns', intVal(cfg.autopilot_watchdog_turns, 4)) +
    labelHtml('硬上限：最多回合', '自动化 预算 回合 上限') +
    limitRow('autopilot-max-turns', intVal(cfg.autopilot_max_turns, 30)) +
    labelHtml('硬上限：最长分钟', '自动化 预算 时长 上限') +
    limitRow('autopilot-max-minutes', intVal(cfg.autopilot_max_minutes, 90)) +
    setGroupHtml('工具与写入', '工具 写入 shell 命令 沙箱 写前读 大厅 提示词') +
    labelHtml('Shell 自由度', 'Shell 命令 管道 重定向 白名单') +
    '<select id="agent-shell-mode" class="field-input">' +
    '<option value="strict">strict（禁管道 / 重定向）</option>' +
    '<option value="standard">standard（允许管道与沙箱内重定向）</option>' +
    '<option value="trusted">trusted（允许 ; && || 串联，仅手动会话）</option>' +
    '</select>' +
    hintHtml('默认 standard：对齐 dsh 的完整 bash，但仍逐段过命令白名单；'
             + 'strict 更保守，trusted 只建议手动会话使用。',
             'Shell 自由度 管道 重定向 白名单') +
    checkRowHtml('agent-hall-prompt',
      '注入大厅功能说明（股票 / B 站）', '大厅 股票 B站 提示词 噪声') +
    hintHtml('关闭后系统提示不再介绍模拟股市、B 站与大厅界面数据，减少纯代码任务的噪声；'
             + '日历 / 日程始终注入，不受影响。',
             '大厅 股票 B站 提示词 噪声 日历') +
    setGroupHtml('只读并行与预取', '并行 并发 读取 预取 速度') +
    labelHtml('只读工具并发数', '并发 并行 读取 预取 速度') +
    limitRow('agent-prefetch-workers', intVal(cfg.agent_prefetch_workers, 10)) +
    hintHtml('一轮里的多个只读调用会同时跑（默认 10 个并行，最多 32 个）。' +
             '填 0 = 关闭并发、全部串行。写文件与命令始终按顺序执行。',
             '并发 并行 读取 预取 速度') +
    checkRowHtml('agent-tool-parallel',
      '执行阶段只读并行（屏障之后的读也并行）', '并行 执行 阶段 只读 屏障') +
    hintHtml('与预取不同：写 / 命令 / 交互之后的独立只读调用也会并发。'
             + '例如「跑测试 → 读 B/C/D」里的三次读会一起执行；写 / 命令本身仍串行。',
             '并行 执行 阶段 只读 屏障 命令后') +
    labelHtml('执行阶段并发数', '并行 执行 阶段 并发') +
    limitRow('agent-tool-parallel-max', intVal(cfg.agent_tool_parallel_max, 10)) +
    checkRowHtml('agent-require-read',
      '覆盖已有文件前必须先读', '写前读 版本 冲突 覆盖 安全') +
    hintHtml('开启后 write_file / edit_file 覆盖本会话还没读过的文件会被拒绝，'
             + '并提示先 read_file；读后文件又被外部改动会要求重读（防止拿过期内容覆盖）。'
             + '新建文件不受影响。',
             '写前读 版本 冲突 覆盖 安全 重读') +
    labelHtml('单轮最多预取几个只读调用', '预取 上限 并发 批量') +
    limitRow('agent-prefetch-max', intVal(cfg.agent_prefetch_max, 24)) +
    hintHtml('防止模型一次发几十个读把机器打满；填 0 = 关闭预取。',
             '预取 上限 并发 关闭') +
    setGroupHtml('工具结果预算', '工具结果 裁剪 折叠 上下文 预算') +
    labelHtml('单条结果回灌上限（0 = 不截断）', '工具结果 上限 截断') +
    limitRow('tool-result-max', intVal(cfg.tool_result_max, 8192)) +
    labelHtml('回灌头部字数', '头部 开头 截断') +
    limitRow('tool-result-head', intVal(cfg.tool_result_head, 4096)) +
    labelHtml('回灌尾部字数', '尾部 结尾 错误 结论') +
    limitRow('tool-result-tail', intVal(cfg.tool_result_tail, 1024)) +
    hintHtml('错误 / 测试失败 / 命令结论通常在末尾，所以头尾都保留；'
             + '中间省略的部分可用 read_tool_result 精读。', '尾部 错误 结论 精读') +
    labelHtml('旧结果保留原文条数', '旧结果 折叠 保留') +
    limitRow('tool-result-keep-recent', intVal(cfg.tool_result_keep_recent, 2)) +
    labelHtml('旧结果折叠上限（字）', '旧结果 折叠 摘要 字数') +
    limitRow('tool-result-fold-chars', intVal(cfg.tool_result_fold_chars, 240)) +
    hintHtml('较早的工具结果会折叠成「工具名 + 目标 + 首尾 + 关键行」，不再只留一句「已清理」。',
             '旧结果 折叠 摘要 关键行') +
    setGroupHtml('写入审批 · 自动判定', '自动审批 ai 风险 高 低 放行 审批') +
    hintHtml('按动作风险判定，不按执行次数：新建文件、建目录、普通编辑、只读命令、' +
             '跑测试与脚本都直接放行；删除 / 移动 / 语义重命名、覆盖已有文件、' +
             '安装依赖 / 联网 / 破坏性 Git（push、reset、clean）才转人工确认。' +
             '需要更细的规则可改用「自定义」档写 .gm/agent-rules.json。',
             '自动审批 风险 删除 覆盖 安装 转人工') +
    setGroupHtml('写入审批 · 全局规则', '全局 规则 预设 权限 agent-rules 标准 谨慎 只读') +
    '<div class="agent-approval">' +
    '<button class="approve-opt agent-preset" type="button" data-preset="standard">' +
    '<span class="approve-opt-ico"><svg class="ic"><use href="#i-check"/></svg></span>' +
    '<span class="approve-opt-text"><b>标准开发</b><i>测试 / 构建 / 本地提交放行</i></span></button>' +
    '<button class="approve-opt agent-preset" type="button" data-preset="cautious">' +
    '<span class="approve-opt-ico"><svg class="ic"><use href="#i-question"/></svg></span>' +
    '<span class="approve-opt-text"><b>谨慎</b><i>写入与命令都问</i></span></button>' +
    '<button class="approve-opt agent-preset" type="button" data-preset="readonly">' +
    '<span class="approve-opt-ico"><svg class="ic"><use href="#i-alert"/></svg></span>' +
    '<span class="approve-opt-text"><b>只读审查</b><i>禁止写入与命令</i></span></button>' +
    '<button class="approve-opt agent-preset" type="button" data-preset="clear">' +
    '<span class="approve-opt-ico"><svg class="ic"><use href="#i-trash"/></svg></span>' +
    '<span class="approve-opt-text"><b>清空</b><i>删除全局规则</i></span></button>' +
    '</div>' +
    '<div class="hint" id="agent-preset-state" style="margin:6px 0 10px;color:var(--text-dim)"></div>' +
    hintHtml('预设写入 <code>data/agent-rules.json</code>，对所有项目生效；' +
             '项目里的 <code>.gm/agent-rules.json</code> 可以追加或收紧。' +
             '应用前会自动备份旧文件；只影响「自定义」档与「自动审批」的规则判定，' +
             '不会改动命令白名单与秘密文件护栏。', '全局 规则 预设 权限 agent-rules') +
    setGroupHtml('命令执行与审计', '命令 超时 run_command 秒 不限 外联 网络 审计 连接') +
    labelHtml('命令默认超时（秒）', '命令 超时 秒 不限') +
    limitRow('agent-cmd-timeout', intVal(cfg.agent_cmd_timeout, 60)) +
    hintHtml('模型没自己指定超时就用这个值（默认 60 秒）。' +
             '装依赖、跑测试这类慢命令可以调大；填 0 = 不限，直到命令自己结束或你点停止——' +
             '卡死的命令也会一直占住这一轮。', '命令 超时 秒 不限') +
    checkRowHtml('agent-net-audit',
      '记录命令访问的外部地址（只记录，不拦截）', '外联 网络 审计 连接') +
    hintHtml('命令跑完后，结果末尾会列出它和子进程连过的外部地址与端口。' +
             '只记录、不拦截，所以 pip / npm 安装不受影响；关掉后不再采集。',
             '外联 网络 审计 连接 上传') +
    setGroupHtml('并行探索（子代理）', '并行 探索 子代理 explore 侦察 交叉验证 证据 验收') +
    checkRowHtml('agent-explore-enabled',
      '允许主代理派出子代理并行调查', '并行 探索 子代理 explore') +
    hintHtml('入口不明、跨多个模块、需要交叉验证时，主代理可以一次派出 1–5 个侦察兵：' +
             '各自独立上下文，只回传 600 字结论与「文件:行号」，原文不进主对话。',
             '并行 探索 子代理 交叉验证') +
    checkRowHtml('agent-explore-verify',
      '重新核对结论里的「文件:行号」', '证据 核验 引用 行号 校验') +
    hintHtml('引用不存在会让子代理带着错误重试一次；仍对不上就判失败，避免把编造的引用当事实。',
             '证据 核验 引用 重试 失败') +
    checkRowHtml('agent-explore-fork',
      '分叉父会话上下文（真 fork）', 'fork 分叉 上下文 继承 摘要') +
    hintHtml('开启后子代理能看到父会话已完成的真实对话（按预算裁剪），不必从头猜背景；' +
             '关掉退回 2000 字摘要模式，更省 token。', 'fork 分叉 上下文 摘要 关闭') +
    labelHtml('fork 前缀预算（token）', 'fork 前缀 预算 token 上下文') +
    limitRow('agent-explore-fork-tokens', intVal(cfg.agent_explore_fork_tokens, 12000)) +
    hintHtml('填 0 = 不限；实际会与下面的「整批 token 预算」联动，取较小值。',
             'fork 预算 联动 不限') +
    checkRowHtml('agent-explore-write',
      '允许子代理在沙箱内写文件（仍走审批）', '子代理 写入 审批 修改 文件') +
    hintHtml('子代理需要模型显式要求写入才会动手；每次写都会弹审批，' +
             '同一批写入串行执行，避免并发改坏同一棵树。', '子代理 写入 审批 串行') +
    labelHtml('一批最多写几次', '子代理 写入 次数 上限') +
    limitRow('agent-explore-write-max', intVal(cfg.agent_explore_write_max, 16)) +
    hintHtml('防止审批轰炸；填 0 = 不限。', '子代理 写入 上限 不限') +
    labelHtml('一次最多几个子代理', '并行 子代理 数量 上限') +
    limitRow('agent-explore-max', intVal(cfg.agent_explore_max, 8)) +
    labelHtml('整批 token 预算', '探索 token 预算 上限') +
    limitRow('agent-explore-budget', intVal(cfg.agent_explore_budget, 160000)) +
    hintHtml('预算用完会带着已有结论提前返回；填 0 = 不限。' +
             '子代理默认用主聊天模型，可在 config.json 里用 agent_explore_model 单独指定。',
             '探索 token 预算 模型') +
    labelHtml('子会话保留秒数（追问用）', '子会话 追问 followup 保留 秒 过期') +
    limitRow('agent-explore-ttl', intVal(cfg.agent_explore_ttl, 600)) +
    hintHtml('探索结束后，这批子代理的会话（含已读片段）会保留这么久，' +
             '期间可以用 explore_followup 追问细节，不必重新探索；填 0 = 关闭追问。',
             '子会话 追问 followup 保留 过期 0') +
    setGroupHtml('子代理 · 嵌套与后台', '嵌套 深度 后台 background 并发 取消') +
    labelHtml('子代理嵌套深度（0–5，0 = 关闭）', '嵌套 深度 子代理') +
    limitRow('agent-explore-max-depth', intVal(cfg.agent_explore_max_depth, 5)) +
    hintHtml('根层 depth=0，子代理最多再往下这么多层；嵌套层默认只读。',
             '嵌套 深度 只读') +
    labelHtml('一棵子代理树的总数上限（0 = 不限）', '子代理 总数 上限') +
    limitRow('agent-explore-max-total', intVal(cfg.agent_explore_max_total, 60)) +
    checkRowHtml('agent-explore-nested-write',
      '允许嵌套层写文件（仍走审批）', '嵌套 写入 审批') +
    hintHtml('默认关闭：嵌套层只做检索与阅读，写入留给根子代理，避免审批链随递归变复杂。',
             '嵌套 写入 关闭 审批') +
    checkRowHtml('agent-explore-background',
      '允许子代理后台运行', '后台 background 不阻塞') +
    hintHtml('后台批次启动后立即返回批次号，主代理继续做别的；' +
             '用 explore_status / explore_result / explore_cancel 取用。后台批次只读。',
             '后台 批次 不阻塞 状态 取消') +
    labelHtml('后台并发上限', '后台 并发 上限') +
    limitRow('agent-explore-background-max', intVal(cfg.agent_explore_background_max, 12)) +
    labelHtml('后台结果保留秒数', '后台 结果 保留 秒') +
    limitRow('agent-explore-background-ttl', intVal(cfg.agent_explore_background_ttl, 86400)) +
    setGroupHtml('研究闸门（复刻 / 还原类任务）', '研究 检索 缺口 台账 还原 复刻 复现') +
    checkRowHtml('agent-research-gate',
      '动笔前先要求检索（复刻 / 还原类任务）', '研究 闸门 检索 还原 复刻') +
    hintHtml('任务像「还原 / 复刻某个已有系统」时，写第一个文件前必须先用 web_search 调研；' +
             '不满足会拦一次并说明先查什么，连拦两次后放行（避免没配搜索时卡死）。',
             '研究 闸门 检索 拦截 还原 复刻') +
    labelHtml('动笔前最少检索次数', '研究 检索 次数 下限') +
    limitRow('agent-research-min-searches', intVal(cfg.agent_research_min_searches, 2)) +
    hintHtml('填 0 = 不设下限。检索结果会记进研究台账（跨轮保留、编号全局连续），' +
             '模型可以用 research_note 登记缺口与结论。', '研究 检索 次数 台账 缺口 关闭') +
    setGroupHtml('工作流（多阶段编排）', '工作流 workflow 多阶段 编排 DAG 断点 恢复') +
    checkRowHtml('workflow-enabled', '允许主代理执行多阶段工作流',
                 '工作流 workflow 多阶段 编排 DAG') +
    hintHtml('工作流把「探索 → 改文件 → 跑测试 → 校验」写成可执行节点，每步落盘，' +
             '中断后可续跑；写文件 / 命令仍然按审批档位询问。',
             '工作流 断点 续跑 审批 编排') +
    labelHtml('最多几个节点', '工作流 节点 上限') +
    limitRow('workflow-max-steps', intVal(cfg.workflow_max_steps, 48)) +
    labelHtml('最多并发几个只读节点', '工作流 并发 节点 并行') +
    limitRow('workflow-max-parallel', intVal(cfg.workflow_max_parallel, 10)) +
    labelHtml('保留多少个历史工作流', '工作流 保留 清理 历史') +
    limitRow('workflow-keep', intVal(cfg.workflow_keep, 200)) +
    hintHtml('并发只对只读节点生效；写 / 命令 / 脚本节点始终串行。填 0 = 不限。',
             '工作流 并发 串行 清理 不限') +
    setGroupHtml('变更审阅（撤销快照）', '变更 审阅 撤销 快照 保留 条数 回滚 diff 不限') +
    labelHtml('每个会话保留多少条撤销快照', '撤销 快照 保留 条数 回滚 diff 不限') +
    limitRow('agent-max-undo', intVal(cfg.agent_max_undo, 60)) +
    hintHtml('「查看变更 / 撤销」都靠这些快照。每个会话保留最近多少条<b>往期</b>写入（默认 60）。' +
             '本轮写入的快照始终保留——同一轮里写过几十个文件时，前几个的 diff 也不会打不开。' +
             '填 0 = 不限：一直留到进程退出（更占内存）。', '撤销 快照 保留 条数 diff 内存 不限') +
    setGroupHtml('电脑操作 · 浏览器（Computer Use）', 'computer 浏览器 操作 网页 白名单 站点 点击') +
    checkRowHtml('computer-enabled',
      '允许 AI 操作受控浏览器（会另开一个独立 Edge 窗口）', '浏览器 操作 开启 computer') +
    hintHtml('开启后模型多出 5 个浏览器工具：打开网页 / 看页面元素 / 点击输入 / 读 console / 截图。' +
             '这是**独立窗口**，看不到你主浏览器里已登录的站点；需要登录态时在这个窗口里登录一次，' +
             'cookie 会留在它自己的配置目录里。银行 / 支付 / 邮箱 / 密码管理器内置禁止，无法解除。',
             '浏览器 独立窗口 登录 禁止') +
    labelHtml('允许操作的站点（每行一个，支持 *.example.com）', '白名单 站点 域名') +
    '<textarea id="computer-sites" class="field-input" rows="4" spellcheck="false"></textarea>' +
    hintHtml('留空表示：每次打开新站点都先问你。把常用站点写进来后不再逐次确认；' +
             '本程序的端口与 web/ 目录永远不允许操作。', '白名单 站点 授权') +
    labelHtml('浏览器后端', '后端 扩展 登录态 独立窗口') +
    '<select id="computer-backend" class="field-input">' +
    '<option value="local">本地受控窗口（独立 profile，看不到登录态）</option>' +
    '<option value="extension">浏览器扩展（复用你已登录的浏览器）</option>' +
    '</select>' +
    hintHtml('扩展模式：先在浏览器里加载 <code>browser_ext/</code> 目录（chrome://extensions → 开发者模式 → 加载已解压），' +
             '点「获取连接码」把它填进扩展设置。扩展会新开一个标签页执行任务，不动你正在看的页面。',
             '扩展 连接码 复用登录态') +
    '<div class="balance-row">' +
    '<input id="computer-pair" class="field-input" readonly placeholder="点右侧按钮获取连接码">' +
    '<button type="button" class="ghost" id="computer-pair-btn">获取连接码</button></div>' +
    checkRowHtml('computer-auto-shot',
      '结构不足时自动补一张截图（canvas / 自绘界面）', '截图 自动 canvas 视觉') +
    hintHtml('页面全是画布、或桌面窗口没有可读控件时，自动截一张图交给视觉模型描述；' +
             '同一目标 30 秒内最多一次。关掉可以省 token，但自绘界面就只能靠猜了。',
             '截图 自动 canvas 视觉 token') +
    labelHtml('页面快照字数上限', '快照 长度 token') +
    numInput('computer-snapshot-chars', intVal(cfg.computer_snapshot_chars, 4000), 800, 12000) +
    hintHtml('模型「看」页面时回灌的文字上限（默认 4000）。调大看得更全，但每步都更费 token。',
             '快照 长度 token') +
    setGroupHtml('电脑操作 · 桌面（Windows UIA）', '桌面 程序 窗口 uia 急停 记事本') +
    checkRowHtml('computer-desktop-enabled',
      '允许 AI 操作桌面程序（仅白名单内的窗口）', '桌面 程序 开启 窗口') +
    hintHtml('与浏览器分开开关。开启后模型能读窗口控件树并点击 / 输入；' +
             '提权（管理员）窗口直接拒绝，密码框不读不填。' +
             '<b>急停：Ctrl+Alt+Shift+Esc</b>（按一次冻结，再按恢复）。',
             '桌面 提权 密码 急停') +
    labelHtml('允许操作的桌面程序（每行一个 exe 名，支持 *.exe）', '白名单 程序 exe') +
    '<textarea id="computer-apps" class="field-input" rows="3" spellcheck="false" ' +
    'placeholder="notepad.exe&#10;Code.exe"></textarea>' +
    hintHtml('不在这个列表里的窗口连「列出」都不会出现 —— 这是桌面的第一道闸门。留空 = 桌面功能不可用。',
             '白名单 程序 闸门') +
    setGroupHtml('代码工具（pyright / Git）', 'pyright lsp 类型 跳转 定义 引用 重命名 git 提交 仓库') +
    checkRowHtml('lsp-enabled',
      '启用 pyright 语言服务（类型检查 / 跳转定义 / 查找引用 / 重命名）',
      'pyright lsp 类型 检查 跳转 定义 引用 重命名') +
    hintHtml('首次调用会用 npx 拉取 pyright（约 50MB，走国内镜像），之后复用缓存。' +
             '关掉后模型退回正则版 find_symbol：只能按名字扫文本，拿不到类型检查。',
             'pyright 下载 镜像 缓存') +
    '<div class="agent-approval-cap" data-search="git 提交 commit 权限 自动">AI 提交 Git 的权限</div>' +
    '<div class="agent-approval">' +
    gitModeBtn('off', 'alert', '不开放', '模型只能看状态和 diff') +
    gitModeBtn('ask', 'check', '每次确认', '推荐：提交前问你一次') +
    gitModeBtn('auto', 'refresh', '完全自动', '改完就提交，不留确认') +
    '</div>' +
    hintHtml('控制模型能不能调用 git_commit。提交不会改文件内容，改的是 Git 历史，' +
             '所以单独一档、不跟写入审批混用。右侧源代码管理面板里的提交不受此限制。',
             'git 提交 commit 历史 审批') +
    setGroupHtml('外部工具（MCP）', 'mcp 外部 工具 服务 协议 stdio http') +
    checkRowHtml('mcp-enabled',
      '启用 MCP（接入外部工具服务）', 'mcp 外部 工具 开启') +
    hintHtml('<b>默认关闭。</b>stdio server 是独立进程，权限与本程序相同；远程 server 意味着数据出境。' +
             '工具默认每次执行前询问；在每行的「只读工具」里写 glob（如 <code>read_*</code>）才会免问。' +
             '工具的 description 与返回值都来自外部，可能夹带指令 —— 不要接不可信的 server。',
             'mcp 安全 进程 数据 审批 注入') +
    labelHtml('工具下发方式', 'mcp 懒注入 上下文 token') +
    '<select id="mcp-tool-mode" class="field-input">' +
    '<option value="lazy">懒注入（推荐：模型要用哪个 server 才挂它的工具）</option>' +
    '<option value="always">全量常驻（每轮都挂所有工具，更费 token）</option>' +
    '</select>' +
    labelHtml('单次调用超时（秒）', 'mcp 超时') +
    numInput('mcp-timeout', intVal(cfg.mcp_timeout, 60), 5, 600) +
    labelHtml('MCP server 列表', 'mcp server 列表 命令 url 环境变量') +
    '<div id="mcp-list" class="mcp-list"></div>' +
    '<button type="button" class="ghost" id="mcp-add">添加 server</button>' +
    '<div id="mcp-test-out" class="hint"></div>' +
    setGroupHtml('生命周期钩子（Hooks）', 'hooks 钩子 工具 前后 审计 上下文') +
    checkRowHtml('hooks-enabled',
      '启用 Hooks（工具前后 / 回合边界可插自定义逻辑）', 'hooks 钩子 开启') +
    hintHtml('默认关闭：command 钩子会执行任意命令，python 钩子会加载 <code>data/plugins</code> 里的代码。' +
             'PreToolUse 返回 <code>{"decision":"block","reason":"..."}</code> 可阻止工具执行；' +
             '返回 <code>{"context":"..."}</code> 会注入模型上下文。钩子写坏了默认放行（fail-open）。',
             'hooks 阻止 上下文 审计 放行') +
    '<textarea id="hooks-json" class="field-input" rows="6" spellcheck="false" ' +
    'placeholder=\'{"PreToolUse":[{"type":"command","command":"python hooks/audit.py","timeout":10}]}\'></textarea>' +
    '<div id="hooks-err" class="hint"></div>';

  // ---- MCP server 列表：结构化行（动态增删，保存在 config.json 的 mcp_servers）----
  const mcpList = qIn(pane, 'mcp-list');
  if (mcpList) {
    (cfg.mcp_servers || []).forEach((sp) => {
      if (sp && typeof sp === 'object') mcpList.appendChild(mcpServerRow(sp));
    });
    mcpList.addEventListener('click', (ev) => {
      const del = ev.target.closest && ev.target.closest('[data-mcp-del]');
      if (del) {
        const row = del.closest('.mcp-row');
        if (row) row.remove();
        return;
      }
      const tst = ev.target.closest && ev.target.closest('[data-mcp-test]');
      if (tst) runMcpTest(pane, tst.closest('.mcp-row'));
    });
  }
  const mcpAdd = qIn(pane, 'mcp-add');
  if (mcpAdd && mcpList) {
    mcpAdd.addEventListener('click', () => mcpList.appendChild(mcpServerRow({})));
  }
  const mcpMode = qIn(pane, 'mcp-tool-mode');
  if (mcpMode) mcpMode.value = cfg.mcp_tool_mode === 'always' ? 'always' : 'lazy';
  const mcpOn = qIn(pane, 'mcp-enabled');
  if (mcpOn) mcpOn.checked = !!cfg.mcp_enabled;
  const hooksJson = qIn(pane, 'hooks-json');
  if (hooksJson) {
    const hk = cfg.hooks && typeof cfg.hooks === 'object' ? cfg.hooks : {};
    hooksJson.value = Object.keys(hk).length ? JSON.stringify(hk, null, 2) : '';
    hooksJson.addEventListener('input', () => syncHooksError(pane));
  }
  const hooksOn = qIn(pane, 'hooks-enabled');
  if (hooksOn) hooksOn.checked = !!cfg.hooks_enabled;

  const sitesEl = qIn(pane, 'computer-sites');
  if (sitesEl) sitesEl.value = (cfg.computer_sites || []).join('\n');
  const compOnEl = qIn(pane, 'computer-enabled');
  if (compOnEl) compOnEl.checked = !!cfg.computer_enabled;
  const appsEl = qIn(pane, 'computer-apps');
  if (appsEl) appsEl.value = (cfg.computer_apps || []).join('\n');
  const deskOnEl = qIn(pane, 'computer-desktop-enabled');
  if (deskOnEl) deskOnEl.checked = !!cfg.computer_desktop_enabled;
  const beEl = qIn(pane, 'computer-backend');
  if (beEl) beEl.value = cfg.computer_backend || 'local';
  const autoShotEl = qIn(pane, 'computer-auto-shot');
  if (autoShotEl) autoShotEl.checked = cfg.computer_auto_shot !== false;
  const lspOnEl = qIn(pane, 'lsp-enabled');
  if (lspOnEl) lspOnEl.checked = cfg.lsp_enabled !== false;
  // 这两项以前漏了回填：界面永远显示未勾选，保存时又按未勾选写回，
  // 表现为「并行探索开了也存不住」，还会把外联审计悄悄关掉。
  const netAuditEl = qIn(pane, 'agent-net-audit');
  if (netAuditEl) netAuditEl.checked = cfg.agent_net_audit !== false;
  const exploreEl = qIn(pane, 'agent-explore-enabled');
  if (exploreEl) exploreEl.checked = cfg.agent_explore_enabled !== false;
  const forkEl = qIn(pane, 'agent-explore-fork');
  if (forkEl) forkEl.checked = cfg.agent_explore_fork !== false;
  const metaStopEl = qIn(pane, 'agent-meta-turn-stop');
  if (metaStopEl) metaStopEl.checked = cfg.agent_meta_turn_stop !== false;
  const qpEl = qIn(pane, 'autonomy-question-policy');
  if (qpEl) qpEl.value = String(cfg.autonomy_question_policy || 'smart');
  const apVerifyEl = qIn(pane, 'autopilot-verify');
  if (apVerifyEl) apVerifyEl.checked = cfg.autopilot_verify !== false;
  const apResumeEl = qIn(pane, 'autopilot-resume-on-start');
  if (apResumeEl) apResumeEl.checked = cfg.autopilot_resume_on_start !== false;
  const apBudgetEl = qIn(pane, 'autopilot-use-budget');
  if (apBudgetEl) apBudgetEl.checked = !!cfg.autopilot_use_budget;
  const exVerEl = qIn(pane, 'agent-explore-verify');
  if (exVerEl) exVerEl.checked = cfg.agent_explore_verify !== false;
  const exWriteEl = qIn(pane, 'agent-explore-write');
  if (exWriteEl) exWriteEl.checked = cfg.agent_explore_write !== false;
  const exNestWriteEl = qIn(pane, 'agent-explore-nested-write');
  if (exNestWriteEl) exNestWriteEl.checked = !!cfg.agent_explore_nested_write;
  const exBgEl = qIn(pane, 'agent-explore-background');
  if (exBgEl) exBgEl.checked = cfg.agent_explore_background !== false;
  const shEl = qIn(pane, 'agent-shell-mode');
  if (shEl) shEl.value = cfg.agent_shell_mode || 'standard';
  const tpEl = qIn(pane, 'agent-tool-parallel');
  if (tpEl) tpEl.checked = cfg.agent_tool_parallel !== false;
  const rrEl = qIn(pane, 'agent-require-read');
  if (rrEl) rrEl.checked = cfg.agent_require_read !== false;
  const hpEl = qIn(pane, 'agent-hall-prompt');
  if (hpEl) hpEl.checked = cfg.agent_hall_prompt !== false;
  const rgEl = qIn(pane, 'agent-research-gate');
  if (rgEl) rgEl.checked = cfg.agent_research_gate !== false;
  const wfEl = qIn(pane, 'workflow-enabled');
  if (wfEl) wfEl.checked = cfg.workflow_enabled !== false;
  const pairEl = qIn(pane, 'computer-pair');
  if (pairEl) pairEl.onclick = () => pairEl.select();
  const pairBtn = qIn(pane, 'computer-pair-btn');
  if (pairBtn) {
    pairBtn.onclick = async () => {
      pairBtn.textContent = '读取中…';
      try {
        const r = await api('/api/computer/bridge/status');
        if (pairEl) pairEl.value = r.pairing || '';
        pairBtn.textContent = r.connected ? '扩展已连接' : '扩展未连接';
      } catch (e) {
        pairBtn.textContent = '获取失败';
      }
    };
  }

  // 续跑三档只改草稿，点「保存」才写回配置（与其它设置项一致）
  let acVal = acNow;
  pane.querySelectorAll('.continue-opt').forEach((b) => {
    b.onclick = () => {
      acVal = parseInt(b.dataset.continue, 10) || 0;
      pane.querySelectorAll('.continue-opt').forEach((x) => {
        x.classList.toggle('active', (parseInt(x.dataset.continue, 10) || 0) === acVal);
      });
      queueDirtyCheck();
    };
  });

  const clamp = (id, d, lo, hi) => {
    const n = parseInt((qIn(pane, id) || {}).value, 10);
    return Math.max(lo, Math.min(hi, Number.isFinite(n) ? n : d));
  };
  // Agent 五项上限：0 = 不限，不能被「上限」钳掉；负数视为填错，回落默认
  const clampLimit = (id, d) => {
    const n = parseInt((qIn(pane, id) || {}).value, 10);
    if (!Number.isFinite(n) || n < 0) return d;
    return n === 0 ? 0 : Math.max(1, n);
  };
  // 「不限」按钮：点一下填 0；输入框里是 0 时高亮，一眼能看出这项没设上限
  const syncUnli = () => {
    pane.querySelectorAll('[data-unli]').forEach((b) => {
      const inp = qIn(pane, b.dataset.unli);
      b.classList.toggle('active', !!inp && String(inp.value).trim() === '0');
    });
  };
  pane.querySelectorAll('[data-unli]').forEach((b) => {
    b.onclick = () => {
      const inp = qIn(pane, b.dataset.unli);
      if (!inp) return;
      inp.value = '0';
      syncUnli();
      queueDirtyCheck();
    };
  });
  pane.addEventListener('input', syncUnli);
  syncUnli();
  let gitMode = gitModeNow;
  pane.querySelectorAll('.gitmode-opt').forEach((b) => {
    b.onclick = () => {
      gitMode = b.dataset.gitmode || 'ask';
      pane.querySelectorAll('.gitmode-opt').forEach((x) => {
        x.classList.toggle('active', x.dataset.gitmode === gitMode);
      });
      queueDirtyCheck();
    };
  });
  // 全局权限预设：立即写 data/agent-rules.json（不走「保存」草稿，覆盖前自动备份）
  const refreshPresetState = async () => {
    const el = pane.querySelector('#agent-preset-state');
    if (!el) return;
    try {
      const r = await api('/api/agent/rules', {});
      if (!r || !r.ok) { el.textContent = ''; return; }
      const g = r.global_rules || {};
      const cmds = g.commands || {};
      const n = ['allow', 'ask', 'deny'].reduce((a, k) => a
        + (g[k] || []).length + (cmds[k] || []).length, 0);
      el.textContent = r.global_exists
        ? ('当前全局规则：' + n + ' 条 · ' + (r.global_path || ''))
        : '当前没有全局规则（应用预设后对所有项目生效）';
    } catch (e) { el.textContent = ''; }
  };
  pane.querySelectorAll('.agent-preset').forEach((b) => {
    b.onclick = async () => {
      const preset = b.dataset.preset;
      if (preset === 'clear' && !confirm('清空全局权限规则？旧文件会先备份。')) return;
      try {
        const r = await api('/api/agent/rules/preset', { preset });
        if (!r || !r.ok) {
          toast('应用失败：' + ((r && r.error) || '未知错误'), 'error');
          return;
        }
        const label = (b.querySelector('b') || {}).textContent || preset;
        toast(preset === 'clear' ? '已清空全局规则' : ('已应用预设：' + label));
        refreshPresetState();
      } catch (e) { toast('应用失败：' + e, 'error'); }
    };
  });
  refreshPresetState();
  pane._collect = () => ({
    agent_max_rounds: clampLimit('agent-max-rounds', 0),
    agent_auto_continue: acVal,
    agent_summary_stop_chars: clampLimit('agent-summary-stop', 180),
    agent_meta_turn_stop: !!(qIn(pane, 'agent-meta-turn-stop') || {}).checked,
    autonomy_question_policy: (qIn(pane, 'autonomy-question-policy') || {}).value || 'smart',
    autopilot_verify: !!(qIn(pane, 'autopilot-verify') || {}).checked,
    autopilot_resume_on_start: !!(qIn(pane, 'autopilot-resume-on-start') || {}).checked,
    autopilot_use_budget: !!(qIn(pane, 'autopilot-use-budget') || {}).checked,
    autopilot_watchdog_turns: clampLimit('autopilot-watchdog-turns', 4),
    autopilot_max_turns: clampLimit('autopilot-max-turns', 30),
    autopilot_max_minutes: clampLimit('autopilot-max-minutes', 90),
    agent_cmd_timeout: clampLimit('agent-cmd-timeout', 60),
    agent_net_audit: !!(qIn(pane, 'agent-net-audit') || {}).checked,
    agent_explore_enabled: !!(qIn(pane, 'agent-explore-enabled') || {}).checked,
    agent_explore_fork: !!(qIn(pane, 'agent-explore-fork') || {}).checked,
    agent_explore_fork_tokens: clampLimit('agent-explore-fork-tokens', 12000),
    agent_explore_verify: !!(qIn(pane, 'agent-explore-verify') || {}).checked,
    agent_explore_write: !!(qIn(pane, 'agent-explore-write') || {}).checked,
    agent_explore_write_max: clampLimit('agent-explore-write-max', 16),
    agent_explore_max_depth: clampLimit('agent-explore-max-depth', 5),
    agent_explore_max_total: clampLimit('agent-explore-max-total', 60),
    agent_explore_nested_write: !!(qIn(pane, 'agent-explore-nested-write') || {}).checked,
    agent_explore_background: !!(qIn(pane, 'agent-explore-background') || {}).checked,
    agent_explore_background_max: clampLimit('agent-explore-background-max', 12),
    agent_explore_background_ttl: clampLimit('agent-explore-background-ttl', 86400),
    agent_research_gate: !!(qIn(pane, 'agent-research-gate') || {}).checked,
    agent_research_min_searches: clampLimit('agent-research-min-searches', 2),
    agent_explore_max: clampLimit('agent-explore-max', 8),
    agent_explore_budget: clampLimit('agent-explore-budget', 160000),
    agent_explore_ttl: clampLimit('agent-explore-ttl', 600),
    workflow_enabled: !!(qIn(pane, 'workflow-enabled') || {}).checked,
    workflow_max_steps: clampLimit('workflow-max-steps', 48),
    workflow_max_parallel: clampLimit('workflow-max-parallel', 10),
    workflow_keep: clampLimit('workflow-keep', 200),
    agent_repeat_limit: clampLimit('agent-repeat-limit', 5),
    agent_shell_mode: (qIn(pane, 'agent-shell-mode') || {}).value || 'standard',
    agent_prefetch_workers: clampLimit('agent-prefetch-workers', 10),
    agent_prefetch_max: clampLimit('agent-prefetch-max', 24),
    agent_tool_parallel: !!(qIn(pane, 'agent-tool-parallel') || {}).checked,
    agent_tool_parallel_max: clampLimit('agent-tool-parallel-max', 10),
    agent_require_read: !!(qIn(pane, 'agent-require-read') || {}).checked,
    agent_hall_prompt: !!(qIn(pane, 'agent-hall-prompt') || {}).checked,
    tool_result_max: clampLimit('tool-result-max', 8192),
    tool_result_head: clampLimit('tool-result-head', 4096),
    tool_result_tail: clampLimit('tool-result-tail', 1024),
    tool_result_keep_recent: clampLimit('tool-result-keep-recent', 2),
    tool_result_fold_chars: clampLimit('tool-result-fold-chars', 240),
    agent_max_undo: clampLimit('agent-max-undo', 60),
    lsp_enabled: !!(qIn(pane, 'lsp-enabled') || {}).checked,
    git_commit_mode: gitMode,
    computer_enabled: !!(qIn(pane, 'computer-enabled') || {}).checked,
    computer_sites: String((qIn(pane, 'computer-sites') || {}).value || '')
      .split(/\r?\n/).map((x) => x.trim()).filter(Boolean),
    computer_snapshot_chars: clamp('computer-snapshot-chars', 4000, 800, 12000),
    computer_desktop_enabled: !!(qIn(pane, 'computer-desktop-enabled') || {}).checked,
    computer_apps: String((qIn(pane, 'computer-apps') || {}).value || '')
      .split(/\r?\n/).map((x) => x.trim()).filter(Boolean),
    computer_backend: (qIn(pane, 'computer-backend') || {}).value || 'local',
    computer_auto_shot: !!(qIn(pane, 'computer-auto-shot') || {}).checked,
    mcp_enabled: !!(qIn(pane, 'mcp-enabled') || {}).checked,
    mcp_tool_mode: (qIn(pane, 'mcp-tool-mode') || {}).value || 'lazy',
    mcp_timeout: clamp('mcp-timeout', 60, 5, 600),
    mcp_servers: mcpCollectRows(pane),
    hooks_enabled: !!(qIn(pane, 'hooks-enabled') || {}).checked,
    hooks: parseHooksJson(qIn(pane, 'hooks-json') || { value: '' }),
  });
  return pane;
}

/* ---------------- MCP / Hooks：设置页辅助 ---------------- */
function mcpServerRow(sp) {
  const row = document.createElement('div');
  row.className = 'mcp-row';

  const mkInput = (cls, ph, value, type) => {
    const el = document.createElement('input');
    el.className = 'field-input ' + cls;
    el.type = type || 'text';
    el.placeholder = ph;
    el.value = value == null ? '' : String(value);
    if (type === 'number') { el.min = '5'; el.max = '600'; }
    return el;
  };
  const mkArea = (cls, ph, value) => {
    const el = document.createElement('textarea');
    el.className = 'field-input ' + cls;
    el.rows = 2;
    el.spellcheck = false;
    el.placeholder = ph;
    el.value = value == null ? '' : String(value);
    return el;
  };
  const cell = (text, el, wide) => {
    const d = document.createElement('div');
    d.className = 'mcp-cell' + (wide ? ' wide' : '');
    const l = document.createElement('label');
    l.className = 'field-label';
    l.textContent = text;
    d.appendChild(l);
    d.appendChild(el);
    return d;
  };

  const sel = document.createElement('select');
  sel.className = 'field-input mcp-transport';
  sel.innerHTML = '<option value="stdio">stdio</option><option value="http">HTTP</option>';
  sel.value = sp.transport === 'http' ? 'http' : 'stdio';

  row.appendChild(cell('名称', mkInput('mcp-name', 'fs', sp.name)));
  row.appendChild(cell('传输', sel));
  row.appendChild(cell('命令（stdio）', mkInput('mcp-command', 'npx', sp.command), true));
  row.appendChild(cell('URL（HTTP）', mkInput('mcp-url', 'https://example.com/mcp', sp.url), true));
  row.appendChild(cell('参数（每行一个）',
    mkArea('mcp-args', '-y\n@modelcontextprotocol/server-filesystem\nD:/work',
      Array.isArray(sp.args) ? sp.args.join('\n') : ''), true));
  row.appendChild(cell('只读工具（glob，空格分隔）',
    mkInput('mcp-readonly', 'read_*  list_*',
      Array.isArray(sp.readonly_tools) ? sp.readonly_tools.join(' ')
        : String(sp.readonly_tools || '')), true));
  row.appendChild(cell('超时（秒）', mkInput('mcp-timeout-row', '60', sp.timeout, 'number')));

  const ops = document.createElement('div');
  ops.className = 'mcp-cell mcp-ops';
  const test = document.createElement('button');
  test.type = 'button';
  test.className = 'ghost';
  test.setAttribute('data-mcp-test', '1');
  test.textContent = '测试连接';
  const del = document.createElement('button');
  del.type = 'button';
  del.className = 'ghost';
  del.setAttribute('data-mcp-del', '1');
  del.textContent = '删除';
  ops.appendChild(test);
  ops.appendChild(del);
  row.appendChild(ops);
  return row;
}

function mcpRowToSpec(row) {
  if (!row) return null;
  const val = (cls) => {
    const el = row.querySelector('.' + cls);
    return el ? String(el.value || '').trim() : '';
  };
  const sp = { name: val('mcp-name'),
               transport: val('mcp-transport') || 'stdio', enabled: true };
  if (sp.transport === 'http') {
    sp.url = val('mcp-url');
  } else {
    sp.command = val('mcp-command');
    const args = val('mcp-args');
    sp.args = args ? args.split(/\r?\n/).map((x) => x.trim()).filter(Boolean) : [];
  }
  const ro = val('mcp-readonly').split(/[,，\s]+/).filter(Boolean);
  if (ro.length) sp.readonly_tools = ro;
  const to = parseInt(val('mcp-timeout-row'), 10);
  if (Number.isFinite(to) && to > 0) sp.timeout = to;
  return sp;
}

function mcpCollectRows(pane) {
  const list = qIn(pane, 'mcp-list');
  if (!list) return [];
  const out = [];
  list.querySelectorAll('.mcp-row').forEach((row) => {
    const sp = mcpRowToSpec(row);
    if (sp && sp.name) out.push(sp);
  });
  return out;
}

async function runMcpTest(pane, row) {
  const out = qIn(pane, 'mcp-test-out');
  const sp = mcpRowToSpec(row);
  if (!sp || !sp.name) {
    if (out) out.textContent = '先填 server 名称';
    return;
  }
  if (out) out.textContent = '连接中…';
  try {
    const r = await api('/api/mcp/test', { server: sp });
    if (out) out.textContent = (r.ok ? '✅ ' : '❌ ') + (r.text || r.error || '');
  } catch (e) {
    if (out) out.textContent = '❌ ' + (e && e.message ? e.message : '请求失败');
  }
}

function parseHooksJson(el) {
  const raw = String((el || {}).value || '').trim();
  if (!raw) return {};
  try {
    const v = JSON.parse(raw);
    if (v && typeof v === 'object' && !Array.isArray(v)) return v;
    throw new Error('必须是 JSON 对象');
  } catch (e) {
    return undefined;          // 解析失败：不提交该键，保存时保留原配置
  }
}

function syncHooksError(pane) {
  const el = qIn(pane, 'hooks-json');
  const err = qIn(pane, 'hooks-err');
  if (!el || !err) return;
  const raw = String(el.value || '').trim();
  if (!raw) { err.textContent = ''; return; }
  try {
    const v = JSON.parse(raw);
    err.textContent = (v && typeof v === 'object' && !Array.isArray(v))
      ? '' : '必须是 JSON 对象';
  } catch (e) {
    err.textContent = 'JSON 解析失败：' + e.message;
  }
}

/* ---------------- 余额 / 视觉开关辅助 ---------------- */
async function refreshBalance(provider, pane) {
  const el = pane.querySelector('#' + provider + '-balance');
  if (!el) return;
  el.textContent = '查询中…';
  try {
    const r = await api('/api/balance', { provider });
    el.textContent = r.ok ? formatBalance(r) : (r.error || '查询失败');
  } catch (e) {
    el.textContent = '查询失败';
  }
}

function formatBalance(r) {
  if (!r.items || !r.items.length) {
    return '已返回数据（未识别字段）：' + JSON.stringify(r.raw || {}).slice(0, 120);
  }
  const parts = r.items.map((it) => {
    const bits = [];
    if (it.granted) bits.push('赠金 ' + it.granted);
    if (it.topped) bits.push('充值 ' + it.topped);
    return (it.label ? it.label + ' ' : '') + (it.total || '') + (bits.length ? '（' + bits.join(' / ') + '）' : '');
  });
  let txt = parts.join('；');
  if (r.is_available === false) txt += ' · 余额不足';
  return txt;
}

function setVisionFieldsDisabled(pane, disabled) {
  ['vision-preset', 'vision-preset-save', 'vision-preset-del', 'vision-url',
   'vision-model', 'vision-key', 'vision-show-key'].forEach((id) => {
    const el = qIn(pane, id);
    if (el) el.disabled = disabled;
  });
  pane.querySelectorAll('input[name="vision-proto"]').forEach((el) => { el.disabled = disabled; });
}

function buildSearchPane() {
  const cfg = settingsDraft();
  const providers = state.searchProviders || {};
  const pane = mkPane('search');

  let opts = '';
  Object.keys(providers).forEach((k) => {
    opts += '<option value="' + k + '">' + providers[k].name + '</option>';
  });

  pane.innerHTML =
    setGroupHtml('联网搜索', '联网 搜索 检索 provider 密钥') +
    hintHtml('开启输入框里的「联网搜索」后，会先把问题改写成多个检索式并行搜索，必要时抓取网页正文，再把结果作为参考资料交给模型，并展示引用来源。完整说明见「功能说明 → 联网搜索」。', '联网 搜索 检索 引用 来源') +
    setGroupHtml('服务商', '服务商 搜索 provider') +
    '<select id="search-provider" class="field-input">' + opts + '</select>' +
    '<p class="hint" id="search-provider-hint"></p>' +
    checkRowHtml('search-prefer-api', '优先使用独立搜索 API（即使聊天模型自带搜索）', '优先 搜索 api 聊天模型 自带') +
    hintHtml('默认：聊天模型自带搜索时优先用它（目前是 DeepSeek，复用聊天 API 的密钥）；'
      + '勾选上面的选项后，始终使用这里配置的服务商（博查 / 豆包 / Tavily）。',
      '优先 搜索 api 聊天模型') +
    setGroupHtml('接入', '接口 地址 密钥 api') +
    labelHtml('接口地址', '接口 地址 url endpoint baseurl') +
    '<input id="search-url" class="field-input" type="text">' +
    labelHtml('API 密钥', '密钥 key api token 认证') +
    '<input id="search-key" class="field-input" type="password">' +
    '<label class="check-row"><input type="checkbox" id="search-show-key"> 显示密钥</label>' +
    setGroupHtml('检索行为', '检索 条数 抓取') +
    labelHtml('结果条数（1–20，每个检索式）', '条数 结果 数量 count') +
    '<input id="search-count" class="field-input" type="number" min="1" max="20">' +
    checkRowHtml('search-fetch', '抓取正文（对命中网页取正文，而不是只有摘要）', '抓取 正文 网页') +
    labelHtml('抓取正文条数（1–10）', '抓取 条数 count') +
    '<input id="search-fetch-count" class="field-input" type="number" min="1" max="10">' +
    setGroupHtml('信息量上限', '合并 条数 字数 上限') +
    labelHtml('单次合并条数上限（1–50）', '合并 条数 上限') +
    '<input id="search-merged-max" class="field-input" type="number" min="1" max="50">' +
    labelHtml('每条正文保留字数（500–20000）', '正文 字数 长度') +
    '<input id="search-fetch-chars" class="field-input" type="number" min="500" max="20000">' +
    setGroupHtml('研究类任务（复刻 / 还原 / 从零实现）', '研究 复刻 还原 条数 字数') +
    labelHtml('研究类合并条数（1–50，0 = 用上面的值）', '研究 合并 条数') +
    '<input id="search-research-merged" class="field-input" type="number" min="0" max="50">' +
    labelHtml('研究类每条正文字数（500–20000，0 = 用上面的值）', '研究 正文 字数') +
    '<input id="search-research-chars" class="field-input" type="number" min="0" max="20000">' +
    setGroupHtml('账户余额', '余额 用量 额度 充值') +
    '<div class="balance-row"><span class="balance-text" id="search-balance">未查询</span>' +
    '<button type="button" class="ghost" id="search-balance-refresh">刷新</button></div>' +
    labelHtml('余额查询地址（可选；留空时按服务商自动识别 bocha / tavily）', '余额 地址') +
    '<input id="search-balance-url" class="field-input" type="text" ' +
    'placeholder="https://api.bochaai.com/v1/fund/remaining">';

  const q = (s) => qIn(pane, 'search-' + s);
  q('provider').value = cfg.search_provider || 'bocha';
  q('prefer-api').checked = !!cfg.search_prefer_api;
  q('url').value = cfg.search_url || '';
  q('key').value = cfg.search_api_key || '';
  q('count').value = cfg.search_count || 8;
  q('fetch').checked = cfg.search_fetch !== false;
  q('fetch-count').value = cfg.search_fetch_count || 4;
  q('merged-max').value = cfg.search_merged_max || 12;
  q('fetch-chars').value = cfg.search_fetch_chars || 2500;
  q('research-merged').value = cfg.search_research_merged_max || 20;
  q('research-chars').value = cfg.search_research_fetch_chars || 4000;
  q('balance-url').value = cfg.search_balance_url || '';
  q('balance-refresh').onclick = () => refreshBalance('search', pane);
  refreshBalance('search', pane);        // 打开设置时查一次；volc 会显示「去控制台查看」

  const syncHint = () => {
    const p = providers[q('provider').value];
    q('provider-hint').textContent = p ? p.hint : '';
    q('url').placeholder = p ? ('留空则使用 ' + p.url) : '留空则使用默认地址';
  };
  syncHint();
  q('provider').onchange = syncHint;
  q('show-key').onchange = (e) => { q('key').type = e.target.checked ? 'text' : 'password'; };

  const syncFetch = () => { q('fetch-count').disabled = !q('fetch').checked; };
  q('fetch').onchange = syncFetch;
  syncFetch();

  const num = (v, d) => {
    const n = parseInt(v, 10);
    return Number.isNaN(n) ? d : n;
  };
  pane._collect = () => ({
    search_provider: q('provider').value,
    search_prefer_api: q('prefer-api').checked,
    search_url: q('url').value.trim(),
    search_api_key: q('key').value.trim(),
    search_count: num(q('count').value, 8),
    search_fetch: q('fetch').checked,
    search_fetch_count: num(q('fetch-count').value, 4),
    search_merged_max: Math.max(1, num(q('merged-max').value, 12)),
    search_fetch_chars: Math.max(500, num(q('fetch-chars').value, 2500)),
    search_research_merged_max: Math.max(0, num(q('research-merged').value, 20)),
    search_research_fetch_chars: Math.max(0, num(q('research-chars').value, 4000)),
    search_balance_url: q('balance-url').value.trim(),
  });
  return pane;
}

/* ============================================================
   B 站数据读取
   ============================================================ */
function buildBiliPane(host) {
  const cfg = settingsDraft();
  // host 为空 = 单独一页；传进来 = 并入「联网与数据源」页
  const pane = host || mkPane('bili');
  const prevCollect = pane._collect;
  pane.insertAdjacentHTML('beforeend',
    setGroupHtml('B 站数据（可选）', 'b站 bilibili 视频 评论 字幕 ai总结 sessdata cookie') +
    hintHtml('让 AI 读取 B 站视频信息、热门评论、字幕与官方 AI 总结。SESSDATA 是本机登录凭证，只保存在本机配置里，不会发送给模型。', 'b站 bilibili 视频 评论 字幕 ai总结 sessdata cookie') +
    setGroupHtml('登录凭证（可选）', 'sessdata cookie 登录 凭证') +
    labelHtml('SESSDATA', 'sessdata cookie 登录') +
    '<input id="bili-sessdata" class="field-input" type="password" autocomplete="off">' +
    '<label class="check-row"><input type="checkbox" id="bili-show-key"> 显示</label>' +
    hintHtml('获取：浏览器登录 bilibili.com → F12 → Application / 存储 → Cookies → https://www.bilibili.com → 复制 SESSDATA 的值。不填也能读取视频信息与评论；字幕和 B 站 AI 总结需要它。', '获取 sessdata 教程 cookie') +
    '<div class="balance-row"><button type="button" class="ghost" id="bili-test">测试登录</button>' +
    '<span class="hint" id="bili-test-result"></span></div>' +
    setGroupHtml('评论', '评论 页数 条数') +
    labelHtml('最多页数（1–10，每页 20 条）', '评论 页数') +
    '<input id="bili-pages" class="field-input" type="number" min="1" max="10">');

  const q = (s) => qIn(pane, 'bili-' + s);
  q('sessdata').value = cfg.bili_sessdata || '';
  q('pages').value = cfg.bili_comment_pages || 3;
  q('show-key').onchange = (e) => { q('sessdata').type = e.target.checked ? 'text' : 'password'; };
  q('test').onclick = async () => {
    const out = q('test-result');
    out.textContent = '检查中…';
    try {
      const r = await api('/api/bili/status', { sessdata: q('sessdata').value.trim() });
      out.textContent = r.logged_in
        ? ('已登录：' + (r.uname || '（未返回昵称）'))
        : '未登录：Cookie 为空或已失效';
    } catch (e) {
      out.textContent = '检查失败：' + (e.message || e);
    }
  };
  const collect = () => ({
    bili_sessdata: q('sessdata').value.trim(),
    bili_comment_pages: Math.max(1, Math.min(10, parseInt(q('pages').value, 10) || 3)),
  });
  pane._collect = () => Object.assign({}, prevCollect ? prevCollect() : {}, collect());
  return pane;
}

/* ============================================================
   Token 用量统计
   ============================================================ */
let usageChart = null;

const USAGE_RANGES = {
  day7: { mode: 'day', span: 7 },
  day30: { mode: 'day', span: 30 },
  month12: { mode: 'month', span: 12 },
};

const fmtNum = (n) => (n || 0).toLocaleString('en-US');

// 调用明细里的时间戳（秒）按本地时区展示
const fmtTime = (ts) => {
  const d = new Date((Number(ts) || 0) * 1000);
  return isNaN(d.getTime()) ? '' : d.toLocaleString('zh-CN', { hour12: false });
};

// 通用加载骨架（占位文案统一观感）
const skeletonHtml = (rows) => {
  let s = '<div class="skeleton">';
  for (let i = 0; i < (rows || 4); i++) s += '<div class="sk-line"></div>';
  return s + '</div>';
};

async function openUsage(rangeKey) {
  const key = USAGE_RANGES[rangeKey] ? rangeKey : 'day7';
  const r = USAGE_RANGES[key];
  showWithAnim($('#usage-modal'));
  $$('#usage-modal .tab').forEach((t) => t.classList.toggle('active', t.dataset.usageRange === key));
  $('#usage-cards').innerHTML = '';
  $('#usage-tables').innerHTML = skeletonHtml(5);
  const rb = $('#usage-balance-refresh');
  if (rb) rb.onclick = () => loadUsageBalance();
  loadUsageBalance();                  // 余额单独拉，不阻塞下面的用量主体
  try {
    const res = await fetch('/api/usage?mode=' + r.mode + '&span=' + r.span,
      { headers: { 'X-Auth-Token': TOKEN } });
    renderUsage(await res.json());
  } catch (e) {
    $('#usage-tables').innerHTML = '<p class="usage-empty">加载失败：' + escapeHtml(String(e)) + '</p>';
  }
}

/* 用量弹窗顶部的账户余额：复用设置页那套 /api/balance + formatBalance。
   失败时给「去哪儿检查」的引导，而不是只丢一句技术错误。 */
async function loadUsageBalance() {
  const el = $('#usage-balance-text');
  if (!el) return;
  el.classList.remove('warn');
  el.textContent = '查询中…';
  try {
    const r = await api('/api/balance', { provider: 'chat' });
    if (r.ok) { el.textContent = formatBalance(r); return; }
    el.textContent = r.error || '未配置余额查询（可在「设置 → 接入与密钥」里填写）';
    el.classList.add('warn');
  } catch (e) {
    el.textContent = '查询失败：接口地址不可达或密钥无效 —— 可在「设置 → 接入与密钥 → 账户余额」里检查';
    el.classList.add('warn');
  }
}

function closeUsage() {
  hideWithAnim($('#usage-modal'), () => {
    if (usageChart) { usageChart.dispose(); usageChart = null; }
  });
}

function renderUsage(data) {
  const s = data.summary || {};
  const cards = [
    ['总 tokens（账户）', fmtNum(s.total)],
    ['输入', fmtNum(s.prompt)],
    ['输出', fmtNum(s.completion)],
    ['缓存命中', fmtNum(s.cached)],
    ['调用次数', fmtNum(s.calls)],
  ];
  $('#usage-cards').innerHTML = cards.map(([l, v]) =>
    '<div class="usage-card"><div class="uc-label">' + l + '</div>' +
    '<div class="uc-value">' + v + '</div></div>').join('');

  const byModel = data.by_model || [];
  const rows = data.rows || [];
  const MAX_ROWS = 200;
  const head = (cols) => '<tr>' + cols.map((c) => '<th>' + c + '</th>').join('') + '</tr>';
  const modelCols = ['模型', '输入', '输出', '合计', '缓存命中', '次数'];
  const rowCols = [data.mode === 'month' ? '月份' : '日期'].concat(modelCols);
  const tr = (cells) => '<tr>' + cells.map((c) => '<td>' + c + '</td>').join('') + '</tr>';

  // 按 API 端点（服务商）：把同一域名下的消耗合并，对应「账户下所有 API」
  const byEndpoint = data.by_endpoint || [];
  let html = '<h3>按 API 端点（服务商）</h3>';
  html += byEndpoint.length
    ? '<table><thead>' + head(['端点', '输入', '输出', '合计', '缓存命中', '次数']) + '</thead><tbody>' +
      byEndpoint.map((r) => tr([escapeHtml(r.endpoint), fmtNum(r.prompt), fmtNum(r.completion),
        fmtNum(r.total), fmtNum(r.cached), fmtNum(r.calls)])).join('') + '</tbody></table>'
    : '<p class="usage-empty">该时间段暂无用量记录</p>';

  html += '<h3>按模型汇总</h3>';
  html += byModel.length
    ? '<table><thead>' + head(modelCols) + '</thead><tbody>' +
      byModel.map((r) => tr([escapeHtml(r.model), fmtNum(r.prompt), fmtNum(r.completion),
        fmtNum(r.total), fmtNum(r.cached), fmtNum(r.calls)])).join('') + '</tbody></table>'
    : '<p class="usage-empty">该时间段暂无用量记录</p>';

  // 按来源：区分「对话」与剧本生成 / 节点判定等内部轻量调用
  const SRC_LABEL = { chat: '对话', vision: '图像识别', char_profile: '角色设定生成',
                      gal_script: '剧本生成', gal_act: '续写下一幕', gal_node: '剧情节点判定' };
  const bySource = Object.keys(data.by_source || {})
    .map((k) => Object.assign({ source: k }, data.by_source[k]))
    .sort((a, b) => b.total - a.total);
  html += '<h3>按来源</h3>';
  html += bySource.length
    ? '<table><thead>' + head(['来源', '输入', '输出', '合计', '缓存命中', '次数']) + '</thead><tbody>' +
      bySource.map((r) => tr([escapeHtml(SRC_LABEL[r.source] || r.source), fmtNum(r.prompt),
        fmtNum(r.completion), fmtNum(r.total), fmtNum(r.cached), fmtNum(r.calls)])).join('') +
      '</tbody></table>'
    : '<p class="usage-empty">该时间段暂无用量记录</p>';

  html += '<h3>按' + (data.mode === 'month' ? '月份' : '日期') + '明细</h3>';
  html += rows.length
    ? '<table><thead>' + head(rowCols) + '</thead><tbody>' +
      rows.slice(0, MAX_ROWS).map((r) => tr([r.bucket, escapeHtml(r.model), fmtNum(r.prompt),
        fmtNum(r.completion), fmtNum(r.total), fmtNum(r.cached), fmtNum(r.calls)])).join('') +
      '</tbody></table>' +
      (rows.length > MAX_ROWS
        ? '<p class="usage-empty">仅显示前 ' + MAX_ROWS + ' 行（共 ' + rows.length + ' 行）</p>' : '')
    : '<p class="usage-empty">该时间段暂无用量记录</p>';

  // 逐次调用明细：用于定位「哪一次、哪个接口」花掉了 token
  const calls = data.calls || [];
  const callCols = ['时间', 'API', '端点', '模型', '来源', '输入', '输出', '合计', '缓存命中'];
  html += '<h3>最近调用明细（溯源）</h3>';
  html += calls.length
    ? '<table><thead>' + head(callCols) + '</thead><tbody>' +
      calls.map((r) => tr([fmtTime(r.ts), escapeHtml(r.api), escapeHtml(r.endpoint),
        escapeHtml(r.model), escapeHtml(SRC_LABEL[r.source] || r.source), fmtNum(r.prompt),
        fmtNum(r.completion), fmtNum(r.total), fmtNum(r.cached)])).join('') + '</tbody></table>' +
      ((data.calls_total || 0) > calls.length
        ? '<p class="usage-empty">仅显示最近 ' + calls.length + ' 条（共 ' + data.calls_total + ' 条）</p>' : '')
    : '<p class="usage-empty">该时间段暂无调用记录</p>';

  $('#usage-tables').innerHTML = html;

  // 图表放在最后渲染：表格插入后 .modal-body 才可能出现滚动条，
  // 滚动条会压缩容器宽度；若先画图，canvas 会比可视区宽，右侧标签被裁掉。
  drawUsageChart(data);
}

function drawUsageChart(data) {
  const el = $('#usage-chart');
  const labels = data.labels || [];
  const models = data.models || [];
  if (!window.echarts) {
    el.innerHTML = '<p class="usage-empty">图表库未加载（需联网加载 ECharts），下方表格仍可查看明细。</p>';
    return;
  }
  el.innerHTML = '';
  if (usageChart) { usageChart.dispose(); usageChart = null; }
  const dark = isDarkTheme();
  const axisColor = dark ? '#a3a3ad' : '#6b6b76';
  const lineColor = dark ? '#333336' : '#e6e6ea';
  usageChart = echarts.init(el);
  usageChart.setOption({
    tooltip: { trigger: 'axis', valueFormatter: (v) => Number(v).toLocaleString('en-US') },
    legend: models.length ? { data: models, bottom: 0, textStyle: { color: axisColor } } : undefined,
    // containLabel: 让 ECharts 为轴标签预留空间；
    // right 再留出余量，避免最右侧标签（如 2026-10-09）贴边被裁
    grid: {
      left: 8, right: 40, top: 18, bottom: models.length ? 46 : 26,
      containLabel: true,
    },
    xAxis: {
      type: 'category', data: labels, boundaryGap: false,
      axisLabel: { color: axisColor, hideOverlap: true },
      axisLine: { lineStyle: { color: lineColor } },
    },
    yAxis: { type: 'value', axisLabel: { color: axisColor }, splitLine: { lineStyle: { color: lineColor } } },
    series: models.map((m) => ({
      name: m, type: 'line', smooth: true, symbol: 'circle', symbolSize: 5,
      data: labels.map((k) => ((data.series || {})[m] || {})[k] || 0),
      areaStyle: { opacity: 0.08 },
    })),
  });
  // 容器尺寸可能在 init 之后才最终确定（弹窗刚显示、出现滚动条等），
  // 再同步一次，避免 canvas 比容器宽导致右侧标签跑出可视区
  requestAnimationFrame(() => { if (usageChart) usageChart.resize(); });
}

function buildLearningPresetSelect(cfg) {
  cfg = cfg || settingsDraft();
  const builtin = state.builtinPresets || {};
  if (!cfg.learning_presets) cfg.learning_presets = {};
  const userPresets = cfg.learning_presets;
  const sel = $('#learning-preset');
  sel.innerHTML = '';

  const add = (value, label) => {
    const o = document.createElement('option');
    o.value = value;
    o.textContent = label;
    sel.appendChild(o);
    return o;
  };
  add('', state.builtinPresetLabel || '默认（内置）');
  Object.keys(builtin).forEach((n) => add('builtin:' + n, '内置 · ' + n));
  Object.keys(userPresets).forEach((n) => add('user:' + n, n));

  // 自动识别当前文本对应哪个预设
  const cur = $('#learning-prompt').value.trim();
  let match = '';
  if (cur) {
    Object.keys(builtin).forEach((n) => { if (!match && builtin[n].trim() === cur) match = 'builtin:' + n; });
    Object.keys(userPresets).forEach((n) => { if (!match && userPresets[n].trim() === cur) match = 'user:' + n; });
    if (!match) {
      add('__custom__', '当前内容（未保存为预设）');
      match = '__custom__';
    }
  }
  sel.value = match;

  sel.onchange = () => {
    const v = sel.value;
    if (v === '__custom__') return;
    if (!v) { $('#learning-prompt').value = ''; return; }
    const kind = v.split(':')[0];
    const name = v.slice(kind.length + 1);
    $('#learning-prompt').value = (kind === 'builtin' ? builtin[name] : userPresets[name]) || '';
  };

  $('#learning-preset-save').onclick = async () => {
    const text = $('#learning-prompt').value.trim();
    if (!text) { toast('学习指令为空，无法保存为预设'); return; }
    const name = await appPrompt('预设名称');
    if (!name || !name.trim()) return;
    const n = name.trim();
    userPresets[n] = text;
    const existing = Array.from(sel.options).find((o) => o.value === 'user:' + n);
    if (existing) existing.textContent = n;
    else add('user:' + n, n);
    sel.value = 'user:' + n;
    toast('预设「' + n + '」已加入，点「保存」生效');
    queueDirtyCheck();
  };

  $('#learning-preset-del').onclick = () => {
    const v = sel.value;
    if (!v || v.indexOf('user:') !== 0) { toast('内置预设不可删除'); return; }
    const n = v.slice(5);
    delete userPresets[n];
    const opt = Array.from(sel.options).find((o) => o.value === v);
    if (opt) opt.remove();
    sel.value = '';
    toast('已删除，点「保存」生效');
    queueDirtyCheck();
  };
}

/* 浮层显隐：退场先播动画，animationend（或兜底超时）后再真正 display:none，
   避免关闭时「啪」地消失 */
function hideWithAnim(el, done) {
  if (!el || el.classList.contains('hidden')) { if (done) done(); return; }
  let fired = false;
  const finish = () => {
    if (fired) return;
    fired = true;
    el.removeEventListener('animationend', finish);
    el.classList.remove('closing');
    el.classList.add('hidden');
    restoreFocusAfterModal();
    if (done) done();
  };
  el.classList.add('closing');
  el.addEventListener('animationend', finish);
  setTimeout(finish, 300);   // 兜底：动画被禁用或被中断时也能收起
}
function showWithAnim(el) {
  if (!el) return;
  const wasHidden = el.classList.contains('hidden');   // 只有「真的从关到开」才记触发元素
  // 复位上一轮拖动留下的位置（窗口重新打开时回到居中）
  const card = el.querySelector ? el.querySelector('.modal-card') : null;
  if (card) { card.style.transform = ''; card.style.animation = ''; }
  el.classList.remove('closing', 'hidden');
  if (!wasHidden) return;
  if (document.activeElement && document.activeElement !== document.body) _modalOpener = document.activeElement;
  // 把焦点交给弹窗容器（而不是输入框）：Tab 就会走进弹窗内部而不是背景页面，
  // 同时不会在移动端一打开就弹出软键盘。卡片补个 tabindex="-1" 才能接收焦点。
  if (card) {
    if (!card.hasAttribute('tabindex')) card.setAttribute('tabindex', '-1');
    try { card.focus({ preventScroll: true }); } catch (e) { /* 忽略 */ }
  }
}
/* 弹窗焦点管理（7 个弹窗共用这两个函数，一处生效）：
   打开时焦点进弹窗 → 关闭时还给原来的触发按钮，避免键盘用户「焦点丢失」。 */
let _modalOpener = null;
function restoreFocusAfterModal() {
  const el = _modalOpener;
  _modalOpener = null;
  if (!el || !document.contains(el) || typeof el.focus !== 'function') return;
  try { el.focus({ preventScroll: true }); } catch (e) { /* 忽略 */ }
}
// 是否处于「打开」状态（退场动画进行中也算已关闭）
function isOpen(el) {
  return !!el && !el.classList.contains('hidden') && !el.classList.contains('closing');
}
// 内容区重新淡入（遮住 vis / ECharts 初始化时的空白）
function replayFadeIn(el) {
  if (!el) return;
  el.classList.remove('fade-in');
  void el.offsetWidth;
  el.classList.add('fade-in');
}

// IDE 主题总开关关闭时，把下方主题卡片整体置灰并禁用
function syncIdeThemeGrid() {
  const on = !!($('#ide-theme-enabled') || {}).checked;
  const grid = $('#modal .ide-theme-grid');
  if (grid) grid.classList.toggle('disabled', !on);
  $$('#modal input[name="ide-theme"]').forEach((r) => { r.disabled = !on; });
}

function deepCopy(obj) {
  return obj == null ? obj : JSON.parse(JSON.stringify(obj));
}

/* 动态面板（依赖配置的页）统一在这里重建，打开设置与「恢复默认」共用 */
function buildDynamicPanes() {
  const content = $('#modal .settings-content');
  if (!content) return [];
  $$('#modal [data-pane="chat"],#modal [data-pane="search"],#modal [data-pane="context"],#modal [data-pane="agent"]')
    .forEach((el) => el.remove());
  const anchor = content.querySelector('[data-pane="profile"]');
  // 模型接入 = 聊天 + 视觉；联网与数据源 = 搜索 + B 站
  const modelPane = buildApiPane('chat', mkPane('chat'));
  buildApiPane('vision', modelPane);
  const netPane = buildSearchPane();
  buildBiliPane(netPane);
  const panes = [modelPane, netPane, buildContextPane(), buildAgentPane()];
  panes.forEach((p) => content.insertBefore(p, anchor));
  return panes;
}

/* 静态面板的值回填：打开设置与「恢复默认」共用 */
function fillStaticSettings(cfg, profile) {
  const p = profile || {};
  const c = cfg || {};
  $('#profile-manual').value = p.manual || '';
  $('#profile-auto-learn').checked = p.auto_learn_enabled !== false;
  $('#learning-prompt').value = p.learning_prompt_custom || '';
  buildLearningPresetSelect(c);

  const theme = c.theme || 'light';
  const radio = document.querySelector('input[name="theme"][value="' + theme + '"]');
  if (radio) radio.checked = true;

  // GAL 演出
  const gTheme = c.gal_theme || 'sakura';
  const gtr = document.querySelector('input[name="gal-theme"][value="' + gTheme + '"]');
  if (gtr) gtr.checked = true;

  // IDE 编辑器主题：与界面主题是两套独立配置
  const iTheme = c.ide_theme || '';
  const itr = document.querySelector('input[name="ide-theme"][value="' + iTheme + '"]');
  if (itr) itr.checked = true;
  const ideToggle = $('#ide-theme-enabled');
  if (ideToggle) {
    ideToggle.checked = !!iTheme;
    ideToggle.onchange = syncIdeThemeGrid;
  }
  syncIdeThemeGrid();
  if ($('#ide-whitespace')) $('#ide-whitespace').value = c.ide_whitespace || 'all';

  const gMode = c.gal_seg_mode || 'single';
  const gmr = document.querySelector('input[name="gal-seg-mode"][value="' + gMode + '"]');
  if (gmr) gmr.checked = true;
  if ($('#gal-auto')) $('#gal-auto').checked = !!c.gal_auto;
  if ($('#gal-min-show')) $('#gal-min-show').value = String(c.gal_min_show != null ? c.gal_min_show : 0.6);
  if ($('#gal-typewriter')) $('#gal-typewriter').checked = c.gal_typewriter !== false;
  if ($('#gal-ui-sfx')) $('#gal-ui-sfx').checked = c.gal_ui_sfx !== false;
  if ($('#gal-nudge-on')) $('#gal-nudge-on').checked = c.gal_nudge !== false;
  if ($('#gal-cps')) $('#gal-cps').value = String(c.gal_cps || 12);
  if ($('#gal-fx')) $('#gal-fx').checked = c.gal_fx !== false;
  if ($('#gal-show-think')) $('#gal-show-think').checked = !!c.gal_show_think;
  if ($('#set-gal-think')) $('#set-gal-think').checked = !!c.gal_think;
  if ($('#gal-show-exec')) $('#gal-show-exec').checked = c.gal_show_exec !== false;
  if ($('#gal-show-stats')) $('#gal-show-stats').checked = c.gal_show_stats !== false;
  if ($('#gal-search')) $('#gal-search').checked = !!c.gal_search;
  if ($('#gal-hide-cursor')) $('#gal-hide-cursor').checked = c.gal_hide_cursor !== false;
  if ($('#gal-dialog-max')) $('#gal-dialog-max').value = String(c.gal_dialog_max || 46);
  if ($('#gal-bgm-volume')) $('#gal-bgm-volume').value = String(c.gal_bgm_volume != null ? c.gal_bgm_volume : 0.5);
  if ($('#gal-sprite-fx')) $('#gal-sprite-fx').checked = c.gal_sprite_fx !== false;
  if ($('#gal-bar-auto')) $('#gal-bar-auto').checked = c.gal_bar_auto !== false;
  if ($('#gal-auto-script')) $('#gal-auto-script').checked = !!c.gal_auto_script;
  if ($('#gal-auto-acts')) $('#gal-auto-acts').value = String(c.gal_auto_script_acts || 5);
  if ($('#gal-auto-rounds')) $('#gal-auto-rounds').value = String(c.gal_auto_script_rounds || 4);

  const sp = $('#speech-polish');
  if (sp) sp.checked = !!c.speech_polish;
  const sd = $('#speech-denoise');
  if (sd) { sd.checked = !!c.speech_denoise; sd.onchange = () => toggleDenoise(sd.checked); }
  const te = $('#speech-tts-enabled'); if (te) te.checked = !!c.tts_enabled;
  const tp = $('#speech-tts-provider'); if (tp) tp.value = c.tts_provider || 'openai';
  const tu = $('#speech-tts-url'); if (tu) tu.value = c.tts_url || '';
  const tk = $('#speech-tts-key'); if (tk) tk.value = c.tts_api_key || '';
  const tm = $('#speech-tts-model'); if (tm) tm.value = c.tts_model || '';
  const tv = $('#speech-tts-voice'); if (tv) tv.value = c.tts_voice || '';
}

/* 分组卡片：每个 h4.set-group 与其后内容包成一张卡（静态面板只包一次） */
function decorateSettingsPanes() {
  $$('#modal .pane').forEach((pane) => {
    if (pane.dataset.carded === '1') return;
    const nodes = Array.from(pane.children);
    let card = null;
    nodes.forEach((node) => {
      if (node.matches && node.matches('h4.set-group')) {
        card = document.createElement('section');
        card.className = 'set-card';
        pane.insertBefore(card, node);
        const head = document.createElement('div');
        head.className = 'set-card-head';
        card.appendChild(head);
        head.appendChild(node);
      } else if (card) {
        card.appendChild(node);
      }
    });
    pane.dataset.carded = '1';
  });
}

/* 长说明默认折叠，首屏只留结论（有 id 的提示会被 JS 动态更新，不折叠） */
function foldLongHints() {
  $$('#modal .pane p.hint').forEach((p) => {
    if (p.id || p.dataset.folded === '1' || p.closest('details')) return;
    if ((p.textContent || '').trim().length < 120) return;
    const det = document.createElement('details');
    det.className = 'hint-fold';
    const sum = document.createElement('summary');
    sum.textContent = '说明';
    const body = document.createElement('div');
    body.className = 'hint-fold-body';
    p.parentNode.insertBefore(det, p);
    det.appendChild(sum);
    det.appendChild(body);
    body.appendChild(p);
    p.dataset.folded = '1';
  });
}

/* 范围型数值：滑块 + 数字输入联动 */
function addRangeCompanions() {
  RANGE_FIELDS.forEach(([id, min, max, step]) => {
    const num = document.querySelector('#modal #' + id);
    if (!num) return;
    let wrap = num.closest('.range-row');
    let range = wrap ? wrap.querySelector('input[type="range"]') : null;
    if (!range) {
      wrap = document.createElement('div');
      wrap.className = 'range-row';
      range = document.createElement('input');
      range.type = 'range';
      range.className = 'range';
      range.min = String(min);
      range.max = String(max);
      range.step = String(step);
      num.parentNode.insertBefore(wrap, num);
      wrap.appendChild(range);
      wrap.appendChild(num);
      num.addEventListener('input', () => { range.value = num.value; });
    }
    range.value = num.value;
    range.oninput = () => {
      num.value = range.value;
      num.dispatchEvent(new Event('input', { bubbles: true }));
    };
  });
}

/* 页签顺序以 DOM 为准，避免再维护一份数组 */
function settingsTabs() {
  return $$('#modal .settings-nav .snav-btn').map((b) => b.dataset.tab);
}

function settingsLastTab() {
  try {
    const t = localStorage.getItem(SETTINGS_LAST_TAB_KEY);
    if (t && document.querySelector('#modal .snav-btn[data-tab="' + t + '"]')) return t;
  } catch (e) { /* 忽略 */ }
  return 'chat';
}

function readSettingsScroll() {
  try { return JSON.parse(localStorage.getItem(SETTINGS_SCROLL_KEY) || '{}') || {}; }
  catch (e) { return {}; }
}

function saveSettingsScroll(tab) {
  const content = $('#modal .settings-content');
  if (!content || !tab) return;
  const map = readSettingsScroll();
  map[tab] = content.scrollTop;
  try { localStorage.setItem(SETTINGS_SCROLL_KEY, JSON.stringify(map)); } catch (e) { /* 忽略 */ }
}

function restoreSettingsScroll(tab) {
  const content = $('#modal .settings-content');
  if (!content) return;
  const map = readSettingsScroll();
  content.scrollTop = Number(map[tab] || 0) || 0;
}

function updateSettingsPageHead(name) {
  const meta = SETTINGS_PAGE_META[name] || [name || '', ''];
  const title = $('#settings-page-title');
  const desc = $('#settings-page-desc');
  if (title) title.textContent = meta[0];
  if (desc) desc.textContent = meta[1];
  const reset = $('#settings-page-reset');
  if (reset) reset.classList.toggle('hidden', !pageResetSpec(name));
  if (name === 'data') loadAppInfo();
}

function openModal(opts) {
  opts = opts || {};
  const modal = $('#modal');
  if (isOpen(modal)) { if (opts.tab) setSettingsTab(opts.tab); return; }

  draftCfg = deepCopy(state.config || {});
  draftProfile = deepCopy(state.profile || {});
  settingsDefaults = null;
  settingsCurrentTab = '';

  const panes = buildDynamicPanes();
  fillStaticSettings(draftCfg, draftProfile);

  // 打开设置时自动查一次余额（不阻塞界面）
  refreshBalance('chat', panes[0]);
  refreshBalance('vision', panes[1]);

  if (typeof loadSkills === 'function') loadSkills();    // Skill 列表（静态面板，只刷数据）
  if (typeof loadTasks === 'function') loadTasks();      // 提醒 / 主动聊天面板

  setSettingsTab(opts.tab || settingsLastTab());
  showWithAnim(modal);
  // 先让弹窗出来，再做全量装饰 / 搜索索引 / 快照：这些都要遍历 13 个分页，
  // 同步做会让「点设置」到「看到界面」多等上百毫秒。
  requestAnimationFrame(() => {
    decorateSettingsPanes();
    foldLongHints();
    addRangeCompanions();
    buildSettingsSearchIndex();        // 动态面板刚重建，索引必须在这之后重建
    resetSettingsSearch();
    restoreSettingsScroll(settingsCurrentTab);
    settingsSnapshot = JSON.stringify(collectSettings());
    updateSettingsDirty();
  });
}

// 设置弹窗：切换左侧导航对应的面板
function setSettingsTab(name) {
  if (!name || !document.querySelector('#modal .snav-btn[data-tab="' + name + '"]')) name = 'chat';
  if (settingsCurrentTab && settingsCurrentTab !== name) saveSettingsScroll(settingsCurrentTab);
  $$('#modal .settings-nav .snav-btn').forEach((b) => b.classList.toggle('active', b.dataset.tab === name));
  $$('#modal .pane').forEach((pn) => pn.classList.toggle('hidden', pn.dataset.pane !== name));
  settingsCurrentTab = name;
  try { localStorage.setItem(SETTINGS_LAST_TAB_KEY, name); } catch (e) { /* 忽略 */ }
  const content = $('#modal .settings-content');
  if (content) content.scrollTop = 0;
  updateSettingsPageHead(name);
  if (isOpen($('#modal'))) requestAnimationFrame(() => restoreSettingsScroll(name));
}

/* ---- 设置面板：搜索 + 键盘操作（与「快捷键」面板的说明一一对应）---- */
let settingsSearchIndex = [];
let settingsSearchHits = [];
let settingsSearchSel = 0;

/* 搜索时把没有命中的分类按钮藏起来，清空后恢复 */
function filterSettingsNav(paneSet) {
  $$('#modal .settings-nav .snav-btn').forEach((b) => {
    const on = !paneSet || paneSet.has(b.dataset.tab);
    b.classList.toggle('hidden', !on);
  });
  $$('#modal .settings-nav .snav-group').forEach((g) => {
    const any = Array.from(g.querySelectorAll('.snav-btn')).some((b) => !b.classList.contains('hidden'));
    g.classList.toggle('hidden', !any);
  });
}

function markSearchText(text, kws) {
  let html = escapeHtml(text || '');
  kws.forEach((k) => {
    if (!k) return;
    const re = new RegExp('(' + k.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ')', 'ig');
    html = html.replace(re, '<mark>$1</mark>');
  });
  return html;
}

function buildSettingsSearchIndex() {
  settingsSearchIndex = [];
  $$('#modal .settings-nav .snav-btn').forEach((b) => {
    settingsSearchIndex.push({ pane: b.dataset.tab, el: b, text: b.textContent.trim(), search: b.dataset.search || '' });
  });
  $$('#modal .pane [data-search]').forEach((el) => {
    const pane = el.closest('.pane');
    settingsSearchIndex.push({
      pane: pane ? pane.dataset.pane : '', el: el,
      text: el.textContent.trim().slice(0, 40), search: el.dataset.search || '',
    });
  });
}

function settingsPaneName(pane) {
  const b = $('#modal .settings-nav .snav-btn[data-tab="' + pane + '"]');
  return b ? b.textContent.trim() : (pane || '');
}

function resetSettingsSearch() {
  const inp = $('#settings-search-input');
  const box = $('#settings-search-list');
  if (inp) inp.value = '';
  if (box) { box.classList.add('hidden'); box.innerHTML = ''; }
  settingsSearchHits = [];
  settingsSearchSel = 0;
  filterSettingsNav(null);
}

function runSettingsSearch(q) {
  const box = $('#settings-search-list');
  if (!box) return;
  const kws = String(q || '').trim().toLowerCase().split(/\s+/).filter(Boolean);
  if (!kws.length) {
    box.classList.add('hidden'); box.innerHTML = '';
    settingsSearchHits = [];
    filterSettingsNav(null);
    return;
  }
  const hits = [];
  settingsSearchIndex.forEach((it) => {
    const hay = (it.text + ' ' + it.search).toLowerCase();
    if (!kws.every((k) => hay.includes(k))) return;
    const inText = kws.every((k) => it.text.toLowerCase().includes(k));
    hits.push({ it: it, score: inText ? 0 : 1 });          // 标签正文命中优先于仅同义词命中
  });
  const order = settingsTabs();
  hits.sort((a, b) => {
    const pa = order.indexOf(a.it.pane);
    const pb = order.indexOf(b.it.pane);
    return (pa - pb) || (a.score - b.score);
  });
  settingsSearchHits = hits.slice(0, 20).map((h) => h.it);
  settingsSearchSel = 0;
  filterSettingsNav(new Set(hits.map((h) => h.it.pane)));
  if (!settingsSearchHits.length) {
    box.innerHTML = '<div class="settings-search-empty">无匹配项，试试「密钥」「压缩」「主题」或「B 站」</div>';
    box.classList.remove('hidden');
    return;
  }
  box.innerHTML = '<div class="settings-search-count">找到 ' + hits.length + ' 项</div>' +
    settingsSearchHits.map((it, i) =>
      '<div class="settings-search-item' + (i === 0 ? ' sel' : '') + '" data-i="' + i + '">' +
      '<span class="settings-search-pane">' + escapeHtml(settingsPaneName(it.pane)) + '</span>' +
      '<span class="settings-search-text">' + markSearchText(it.text || '', kws) + '</span></div>').join('');
  box.classList.remove('hidden');
}

function settingsSearchMove(delta) {
  if (!settingsSearchHits.length) return;
  settingsSearchSel = Math.max(0, Math.min(settingsSearchHits.length - 1, settingsSearchSel + delta));
  $$('#settings-search-list .settings-search-item').forEach((el, i) => el.classList.toggle('sel', i === settingsSearchSel));
}

function flashSettingsTarget(el) {
  if (!el) return;
  const t = el.closest('.check-row, .field-label, .set-group, .snav-btn') || el;
  t.classList.add('settings-flash');
  setTimeout(() => t.classList.remove('settings-flash'), 900);
}

function settingsJumpTo(pane, el) {
  if (!pane) return;
  setSettingsTab(pane);
  resetSettingsSearch();
  requestAnimationFrame(() => {
    const det = el && el.closest ? el.closest('details') : null;
    if (det) det.open = true;
    if (el && el.scrollIntoView) el.scrollIntoView({ block: 'center' });
    flashSettingsTarget(el);
  });
}

// 设置内的键盘操作：Esc 关闭 / Ctrl+S 保存 / ←→ 切分类 / 搜索框内 ↑↓·Enter·Esc
// 用捕获相（true），先于 document 冒泡相上的 GAL、IDE 监听执行，避免互相打架。
function onSettingsKeydown(e) {
  if (_hkRecording) return;                        // 正在录制快捷键 → 键盘交给录制，别切分类 / 关弹窗
  if (isImeComposing(e)) return;                   // 输入法组词时，Enter/Esc 先交给候选框
  const modal = $('#modal');
  if (!modal || modal.classList.contains('hidden')) return;
  const inp = $('#settings-search-input');
  const inSearch = !!inp && document.activeElement === inp;
  if (e.key === 'Escape') {
    e.preventDefault(); e.stopPropagation();
    if (inSearch && inp.value) { inp.value = ''; runSettingsSearch(''); }
    else closeModal();
    return;
  }
  const mod = e.ctrlKey || e.metaKey;
  const k = (e.key || '').toLowerCase();
  if (mod && (k === 's' || k === 'p')) {          // 顺带吞掉 Ctrl+P / Ctrl+Shift+P，防 IDE 面板叠在弹窗上
    e.preventDefault(); e.stopPropagation();
    if (k === 's') saveModal();
    return;
  }
  if (inSearch) {
    if (e.key === 'ArrowDown') { e.preventDefault(); settingsSearchMove(1); return; }
    if (e.key === 'ArrowUp') { e.preventDefault(); settingsSearchMove(-1); return; }
    if (e.key === 'Enter') {
      e.preventDefault();
      const it = settingsSearchHits[settingsSearchSel];
      if (it) settingsJumpTo(it.pane, it.el);
      return;
    }
  }
  if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
    const t = e.target;
    if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable)) return;
    const cur = $$('#modal .settings-nav .snav-btn').find((b) => b.classList.contains('active'));
    const tabs = settingsTabs();
    const i = cur ? tabs.indexOf(cur.dataset.tab) : -1;
    if (i < 0) return;
    const next = e.key === 'ArrowRight'
      ? Math.min(tabs.length - 1, i + 1)
      : Math.max(0, i - 1);
    e.preventDefault();
    setSettingsTab(tabs[next]);
  }
}

function setupSettingsSearch() {
  const inp = $('#settings-search-input');
  const box = $('#settings-search-list');
  if (!inp || !box) return;
  inp.addEventListener('input', () => runSettingsSearch(inp.value));
  inp.addEventListener('focus', () => { if (inp.value) runSettingsSearch(inp.value); });
  box.addEventListener('click', (e) => {
    const item = e.target.closest('.settings-search-item');
    if (!item) return;
    const it = settingsSearchHits[Number(item.dataset.i)];
    if (it) settingsJumpTo(it.pane, it.el);
  });

  // 「数据与高级」页
  const openData = $('#app-open-data');
  if (openData) openData.onclick = openDataDir;
  const backup = $('#app-backup');
  if (backup) backup.onclick = () => {
    if (typeof workOpenBackupDialog === 'function') workOpenBackupDialog();
    else toast('备份与恢复在工作台里');
  };
  const resetAll = $('#app-reset-all');
  if (resetAll) resetAll.onclick = resetAllSettings;
  const ctxMenu = $('#app-context-menu');
  if (ctxMenu) ctxMenu.onclick = toggleContextMenu;
  const resetPage = $('#settings-page-reset');
  if (resetPage) resetPage.onclick = resetCurrentSettingsPage;

  // 任何输入 / 选择都重算「未保存的改动」（弹窗内的搜索框不算）
  const modal = $('#modal');
  if (modal) {
    const onEdit = (e) => { if (e.target.closest && e.target.closest('.pane')) queueDirtyCheck(); };
    modal.addEventListener('input', onEdit);
    modal.addEventListener('change', onEdit);
  }
}

/* ---- 脏状态：每次改动都与打开时的快照对比 ---- */
function countSettingDiff(a, b) {
  if (a === b) return 0;
  if (Array.isArray(a) || Array.isArray(b)) {
    return JSON.stringify(a) === JSON.stringify(b) ? 0 : 1;
  }
  if (a && b && typeof a === 'object' && typeof b === 'object') {
    const keys = new Set(Object.keys(a).concat(Object.keys(b)));
    let n = 0;
    keys.forEach((k) => { n += countSettingDiff(a[k], b[k]); });
    return n;
  }
  return 1;
}

function updateSettingsDirty() {
  if (!settingsSnapshot) return;
  const cur = JSON.stringify(collectSettings());
  settingsDirty = cur !== settingsSnapshot;
  let n = 0;
  try { n = countSettingDiff(JSON.parse(settingsSnapshot), JSON.parse(cur)); }
  catch (e) { n = settingsDirty ? 1 : 0; }
  const save = $('#btn-save');
  if (save) {
    save.disabled = !settingsDirty;
    save.textContent = settingsDirty && n ? ('保存（' + n + '）') : '保存';
  }
  const tip = $('#settings-dirty');
  if (tip) {
    tip.classList.toggle('hidden', !settingsDirty);
    tip.textContent = settingsDirty ? '未保存的改动' : '';
  }
}

function queueDirtyCheck() {
  clearTimeout(_dirtyTimer);
  _dirtyTimer = setTimeout(updateSettingsDirty, 80);
}

/* 同步判断（不依赖 80ms 的防抖），供保存 / 关闭前使用 */
function settingsChangedNow() {
  if (!settingsSnapshot) return false;
  try { return JSON.stringify(collectSettings()) !== settingsSnapshot; }
  catch (e) { return true; }
}

/* 把界面上所有设置收集成 {config, profile}（保存与脏状态共用） */
function collectSettings() {
  const cfg = Object.assign({}, draftCfg || state.config || {});
  $$('#modal .pane').forEach((pane) => {
    if (pane._collect) Object.assign(cfg, pane._collect());
  });

  const tr = document.querySelector('input[name="theme"]:checked');
  if (tr) cfg.theme = tr.value;
  // IDE 编辑器主题：总开关关闭 = 跟随界面主题（存空串）
  // 开关打开时也允许选「跟随界面主题」（value 为空）—— 此时同样存空串，
  // 绝不能兜底成 vscode-dark，否则「调浅色」会被静默改回深色。
  const ideOn = !!($('#ide-theme-enabled') || {}).checked;
  if (!ideOn) {
    cfg.ide_theme = '';
  } else {
    const itr = document.querySelector('input[name="ide-theme"]:checked');
    cfg.ide_theme = (itr && itr.value) || '';
  }
  if ($('#ide-whitespace')) cfg.ide_whitespace = $('#ide-whitespace').value;

  // GAL 演出
  const gtr = document.querySelector('input[name="gal-theme"]:checked');
  if (gtr) cfg.gal_theme = gtr.value;
  const gmr = document.querySelector('input[name="gal-seg-mode"]:checked');
  if (gmr) cfg.gal_seg_mode = gmr.value;
  if ($('#gal-auto')) cfg.gal_auto = !!$('#gal-auto').checked;
  if ($('#gal-min-show')) cfg.gal_min_show = Math.max(0, Math.min(5, parseFloat($('#gal-min-show').value) || 0));
  if ($('#gal-typewriter')) cfg.gal_typewriter = !!$('#gal-typewriter').checked;
  if ($('#gal-ui-sfx')) cfg.gal_ui_sfx = !!$('#gal-ui-sfx').checked;
  if ($('#gal-nudge-on')) cfg.gal_nudge = !!$('#gal-nudge-on').checked;
  if ($('#gal-cps')) cfg.gal_cps = Math.max(8, Math.min(120, parseInt($('#gal-cps').value, 10) || 12));
  if ($('#gal-fx')) cfg.gal_fx = !!$('#gal-fx').checked;
  if ($('#gal-show-think')) cfg.gal_show_think = !!$('#gal-show-think').checked;
  if ($('#set-gal-think')) cfg.gal_think = !!$('#set-gal-think').checked;
  if ($('#gal-show-exec')) cfg.gal_show_exec = !!$('#gal-show-exec').checked;
  if ($('#gal-show-stats')) cfg.gal_show_stats = !!$('#gal-show-stats').checked;
  if ($('#gal-search')) cfg.gal_search = !!$('#gal-search').checked;
  if ($('#gal-hide-cursor')) cfg.gal_hide_cursor = !!$('#gal-hide-cursor').checked;
  if ($('#gal-dialog-max')) cfg.gal_dialog_max = Math.max(30, Math.min(70, parseInt($('#gal-dialog-max').value, 10) || 46));
  if ($('#gal-bgm-volume')) {
    const bv = parseFloat($('#gal-bgm-volume').value);
    cfg.gal_bgm_volume = Math.max(0, Math.min(1, bv >= 0 ? bv : 0.5));
  }
  if ($('#gal-sprite-fx')) cfg.gal_sprite_fx = !!$('#gal-sprite-fx').checked;
  if ($('#gal-bar-auto')) cfg.gal_bar_auto = !!$('#gal-bar-auto').checked;
  if ($('#gal-auto-script')) cfg.gal_auto_script = !!$('#gal-auto-script').checked;
  if ($('#gal-auto-acts')) cfg.gal_auto_script_acts = Math.max(1, Math.min(12, parseInt($('#gal-auto-acts').value, 10) || 5));
  if ($('#gal-auto-rounds')) cfg.gal_auto_script_rounds = Math.max(1, Math.min(12, parseInt($('#gal-auto-rounds').value, 10) || 4));

  cfg.speech_polish = !!$('#speech-polish').checked;
  cfg.tts_enabled = !!($('#speech-tts-enabled') || {}).checked;
  if ($('#speech-tts-provider')) cfg.tts_provider = $('#speech-tts-provider').value;
  if ($('#speech-tts-url')) cfg.tts_url = $('#speech-tts-url').value.trim();
  if ($('#speech-tts-key')) cfg.tts_api_key = $('#speech-tts-key').value.trim();
  if ($('#speech-tts-model')) cfg.tts_model = $('#speech-tts-model').value.trim();
  if ($('#speech-tts-voice')) cfg.tts_voice = $('#speech-tts-voice').value.trim();

  const profile = {
    manual: $('#profile-manual').value,
    auto_learn_enabled: $('#profile-auto-learn').checked,
    learning_prompt_custom: $('#learning-prompt').value,
  };
  return { config: cfg, profile: profile };
}

/* ---- 恢复默认 ---- */
async function ensureSettingsDefaults() {
  if (settingsDefaults) return settingsDefaults;
  const r = await api('/api/settings/defaults');
  if (!r || !r.ok) throw new Error((r && r.error) || '无法读取默认值');
  settingsDefaults = { config: r.config || {}, profile: r.profile || {} };
  return settingsDefaults;
}

function pageResetSpec(name) {
  const pane = document.querySelector('#modal .pane[data-pane="' + name + '"]');
  if (pane && typeof pane._collect === 'function') {
    const keys = Object.keys(pane._collect())
      .filter((k) => !SECRET_SETTING_KEY.test(k) && !PRESET_SETTING_KEY.test(k));
    return keys.length ? { config: keys, profile: [] } : null;
  }
  const spec = STATIC_RESET_KEYS[name];
  if (!spec) return null;
  return { config: (spec.config || []).slice(), profile: (spec.profile || []).slice() };
}

function rebuildSettingsFromDraft() {
  buildDynamicPanes();
  fillStaticSettings(draftCfg, draftProfile);
  decorateSettingsPanes();
  foldLongHints();
  addRangeCompanions();
  buildSettingsSearchIndex();
  setSettingsTab(settingsCurrentTab || 'chat');
  updateSettingsDirty();
}

async function resetCurrentSettingsPage() {
  const name = settingsCurrentTab;
  const spec = pageResetSpec(name);
  if (!spec) { toast('本页没有可恢复的默认项'); return; }
  let defs;
  try { defs = await ensureSettingsDefaults(); }
  catch (e) { toastErr('读取默认值', e); return; }
  const cur = collectSettings();
  spec.config.forEach((k) => { if (k in defs.config) cur.config[k] = deepCopy(defs.config[k]); });
  spec.profile.forEach((k) => { if (k in defs.profile) cur.profile[k] = deepCopy(defs.profile[k]); });
  draftCfg = cur.config;
  draftProfile = cur.profile;
  rebuildSettingsFromDraft();
  toast('已恢复本页默认值，点「保存」生效');
}

async function resetAllSettings() {
  if (!window.confirm('把设置页里的可调项恢复为默认值？\nAPI 密钥、B 站 SESSDATA 与自定义预设会保留。恢复后仍需点「保存」生效。')) return;
  let defs;
  try { defs = await ensureSettingsDefaults(); }
  catch (e) { toastErr('读取默认值', e); return; }
  const cur = collectSettings();
  const keys = new Set();
  settingsTabs().forEach((t) => {
    const spec = pageResetSpec(t);
    if (spec) spec.config.forEach((k) => keys.add(k));
  });
  keys.forEach((k) => { if (k in defs.config) cur.config[k] = deepCopy(defs.config[k]); });
  Object.keys(defs.profile || {}).forEach((k) => { cur.profile[k] = deepCopy(defs.profile[k]); });
  draftCfg = cur.config;
  draftProfile = cur.profile;
  rebuildSettingsFromDraft();
  toast('已恢复全部默认值，点「保存」生效');
}

/* ---- 数据与高级 ---- */
async function loadAppInfo() {
  const el = $('#appinfo-body');
  if (!el) return;
  el.textContent = '加载中…';
  try {
    const r = await api('/api/app/info');
    if (!r || !r.ok) throw new Error((r && r.error) || '读取失败');
    el.innerHTML =
      '<div class="appinfo-row"><span>版本</span><b>' + escapeHtml(r.version || '') + '</b></div>' +
      '<div class="appinfo-row"><span>数据目录</span><code>' + escapeHtml(r.data_dir || '') + '</code></div>' +
      '<div class="appinfo-row"><span>Python</span><code>' + escapeHtml(r.python || '') + '</code></div>' +
      '<div class="appinfo-row"><span>系统</span><code>' + escapeHtml(r.platform || '') + '</code></div>';
  } catch (e) {
    el.textContent = '读取失败：' + (e.message || e);
  }
}

async function openDataDir() {
  try {
    const r = await api('/api/app/open-data-dir');
    if (!r || !r.ok) throw new Error((r && r.error) || '打开失败');
  } catch (e) {
    toastErr('打开数据目录', e);
  }
}

/* 资源管理器右键菜单：按钮是开关，注册状态存在 dataset.installed 上 */
async function toggleContextMenu() {
  const btn = $('#app-context-menu');
  const installed = !!(btn && btn.dataset.installed === '1');
  if (btn) btn.disabled = true;
  try {
    const r = await api('/api/app/context-menu', { action: installed ? 'remove' : 'install' });
    const st = (r && r.status) || {};
    if (btn) {
      btn.dataset.installed = st.installed ? '1' : '0';
      btn.textContent = st.installed ? '移除右键菜单' : '注册右键菜单';
    }
    if (!r || !r.ok) throw new Error((r && (r.message || r.error)) || '操作失败');
    toast(r.message || (st.installed ? '已注册右键菜单' : '已移除右键菜单'), 'success');
  } catch (e) {
    toastErr('资源管理器右键菜单', e);
  } finally {
    if (btn) btn.disabled = false;
  }
}

function closeModal(force) {
  if (!force && settingsChangedNow()) {
    if (!window.confirm('有未保存的改动，关闭后将丢弃。确定关闭？')) return false;
  }
  settingsDirty = false;
  saveSettingsScroll(settingsCurrentTab);
  hideWithAnim($('#modal'));
  return true;
}

async function saveModal() {
  if (!settingsChangedNow()) { toast('没有需要保存的改动'); return; }
  settingsDirty = true;
  // 保存是最高频操作之一：先给出「进行中」反馈，失败也要说清楚（原实现没有 try/catch，失败时界面毫无反应）
  const saveBtn = $('#btn-save');
  const saveTxt = saveBtn ? saveBtn.textContent : '';
  const restoreSaveBtn = () => { if (saveBtn) { saveBtn.disabled = false; saveBtn.textContent = saveTxt; } };
  if (saveBtn) { saveBtn.disabled = true; saveBtn.textContent = '保存中…'; }
  const collected = collectSettings();
  const cfg = collected.config;
  const profile = collected.profile;
  const wasAutoScript = !!(state.config && state.config.gal_auto_script);   // 保存前的开关状态

  try {
    await api('/api/config', cfg);
    await api('/api/profile', profile);
  } catch (e) {
    restoreSaveBtn();
    toastErr('保存', e);          // 失败 → 不改内存里的 config，保证界面与磁盘一致
    return;
  }
  restoreSaveBtn();
  state.config = cfg;
  state.profile = profile;
  draftCfg = deepCopy(cfg);
  draftProfile = deepCopy(profile);
  settingsSnapshot = JSON.stringify(collectSettings());
  // 密钥被清空时，顺带关掉「联网搜索」开关
  if (state.searchEnabled && !hasSearchKey()) {
    state.searchEnabled = false;
    api('/api/config', { search_enabled: false }).then(() => { state.config.search_enabled = false; });
  }
  syncSearchPill();
  applyTheme(cfg.theme);
  galApplyTheme();
  applyIdeTheme();
  ideApplyWhitespace();
  galApplyViewPrefs();
  // 刚开启「AI 自动写剧本」→ 立即为当前会话生成（用户可看到进度）
  // 刚关闭 → 清掉该会话的自动剧本标记（保留已生成的内容，便于再次开启）
  if (state.uiMode === 'gal' && cfg.gal_auto_script !== wasAutoScript) {
    if (cfg.gal_auto_script) setTimeout(() => { galGenerateScript(); }, 260);
    else api('/api/gal/session', { id: state.sessions.current, gal_auto_on: false }).catch(() => {});
  }
  if (state.uiMode === 'gal') { galState.ttsOn = !!cfg.tts_enabled; galSyncTtsBtn(); galRenderStats(galState.stats || []); galApplyBgmVolume(); }
  updateStatusLine();
  updateSettingsDirty();
  closeModal(true);
  toast('已保存');
}

/* ============================================================
   主题
   ============================================================ */
/* ---------------- 应用窗口标题栏（Windows） ----------------
   标题栏由系统绘制，网页改不动；交给后端用 DWM 上色。
   颜色总取「当前实际渲染」的侧栏背景 —— 界面主题 / IDE 主题切换后都能跟上，
   不再只认 applyTheme 那一处。 */
let _winChromeTimer = null;
let _winChromeLast = '';

function windowChromeColor() {
  const el = document.querySelector('#sidebar');
  if (!el) return { dark: false, bg: '' };
  const cs = getComputedStyle(el);
  const scheme = String(cs.colorScheme || '') ||
                 String(getComputedStyle(document.documentElement).colorScheme || '');
  const dark = scheme.indexOf('dark') >= 0;
  const rgb = /^rgba?\(([^)]+)\)/.exec(String(cs.backgroundColor || ''));
  let bg = '';
  if (rgb) {
    const p = rgb[1].split(',').map((x) => parseFloat(x));
    if (p.length < 4 || p[3] !== 0) {         // 透明就交给后端用默认色
      const h = (n) => Math.max(0, Math.min(255, Math.round(n))).toString(16).padStart(2, '0');
      bg = '#' + h(p[0]) + h(p[1]) + h(p[2]);
    }
  }
  return { dark: dark, bg: bg };
}

function syncWindowChrome() {
  clearTimeout(_winChromeTimer);
  _winChromeTimer = setTimeout(() => {
    const c = windowChromeColor();
    const key = (c.dark ? 'dark' : 'light') + ':' + c.bg;
    if (key === _winChromeLast) return;       // 颜色没变就不重复请求
    _winChromeLast = key;
    try {
      fetch('/api/window/theme', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Auth-Token': TOKEN },
        body: JSON.stringify({ theme: c.dark ? 'dark' : 'light', bg: c.bg }),
      }).catch(() => {});
    } catch (e) { /* 忽略 */ }
  }, 200);
}

function applyTheme(theme) {
  const t = THEME_META[theme] ? theme : 'light';
  const meta = THEME_META[t];
  const root = document.documentElement;
  // 只在切换瞬间挂一次过渡类，避免长期挂着全局 transition 影响性能
  root.classList.add('theme-anim');
  clearTimeout(root._themeTimer);
  root._themeTimer = setTimeout(() => root.classList.remove('theme-anim'), 260);
  root.setAttribute('data-theme', t);
  root.setAttribute('data-theme-mode', meta.dark ? 'dark' : 'light');
  // 记到本地：下次开屏前就能恢复主题，避免闪一下反色
  try { localStorage.setItem('gm.theme', t); } catch (e) { /* 忽略 */ }
  // 原生 UI 跟随主题：color-scheme 影响控件/滚动条，theme-color 影响应用窗口的标题栏
  root.style.colorScheme = meta.dark ? 'dark' : 'light';
  const tc = document.getElementById('theme-color');
  if (tc) tc.setAttribute('content', meta.title);
  // 应用窗口标题栏：按实际渲染背景同步（与 IDE 主题共用同一套逻辑）
  syncWindowChrome();
  const light = document.getElementById('hljs-light');
  const dark = document.getElementById('hljs-dark');
  if (light) { light.disabled = meta.dark; light.media = 'all'; }
  if (dark) { dark.disabled = !meta.dark; dark.media = 'all'; }
  // 代码编辑器（Monaco）也跟随明暗
  if (typeof ideSyncMonacoTheme === 'function') { try { ideSyncMonacoTheme(); } catch (e) { /* 忽略 */ } }
  // 图谱配色取自 CSS token：打开着的图谱要重绘一次，否则节点还停在旧主题的颜色
  const kbModal = document.getElementById('kb-modal');
  if (kbModal && !kbModal.classList.contains('hidden')
      && state.kbData && typeof drawKbGraph === 'function') {
    drawKbGraph();
  }
  const notesModal = document.getElementById('notes-modal');
  if (notesModal && !notesModal.classList.contains('hidden')
      && typeof drawNotesGraph === 'function') {
    drawNotesGraph();
  }
}

/* ---- IDE 主题：与上面那套界面主题是**两套独立配置** ----
   实现方式与 GAL 主题同源：只在 IDE 模式给 <html> 挂 data-ide-theme，
   退出 IDE 立刻摘掉 —— 所以 Solo / GAL 在任何情况下都看不到这套色板。
   颜色本身写在 web/css/ide.css，Monaco 侧写在 gal-ide.js。
   取值留空 = 跟随界面主题（不挂属性，直接走全局色板）。

   注：这份清单要与 gal-ide.js 里 IDE_MONACO_THEMES 的键保持一致
   （vscode-dark / vscode-light 不需要自定义 Monaco 主题，用内置 vs-dark / vs）。 */
const IDE_VSCODE_CURRENT = 'vscode-current';   // 特殊值：跟随本机 VS Code 正在用的主题
const IDE_THEME_IDS = ['vscode-modern-dark', 'vscode-modern-light',
  'vscode-dark', 'vscode-light', 'trae-dark', 'trae-light', 'trae-blue'];

function applyIdeTheme() {
  const root = document.documentElement;
  const t = String((state.config && state.config.ide_theme) || '');
  const inIde = state.uiMode === 'ide';
  let attr = '';
  if (inIde) {
    if (t === IDE_VSCODE_CURRENT) {
      // 先用上一次解析出的预设，避免异步取回之前闪一下默认色；
      // 取不到就跟随界面（不再兜底成深色预设）
      attr = (typeof ideVscodePreset === 'function' ? ideVscodePreset() : '') || '';
    } else if (IDE_THEME_IDS.indexOf(t) >= 0) {
      attr = t;
    }
  }
  if (attr) root.setAttribute('data-ide-theme', attr);
  else root.removeAttribute('data-ide-theme');
  if (typeof ideApplyVscodeVars === 'function') ideApplyVscodeVars();
  // 「跟随 VS Code」需要异步取一次并开始轮询；其他取值则确保轮询已停
  if (typeof ideVscodeCurrent === 'function' && ideVscodeCurrent()) {
    ideRefreshVscodeCurrent();
    ideStartVscodeThemePoll();
  } else if (typeof ideStopVscodeThemePoll === 'function') {
    ideStopVscodeThemePoll();
  }
  if (typeof ideSyncMonacoTheme === 'function') { try { ideSyncMonacoTheme(); } catch (e) { /* 忽略 */ } }
  syncWindowChrome();     // IDE 主题切换后，标题栏也跟着变
}

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
/* Agent 设置浮层：沙箱目录 / 路径 / 沙箱外只读收在开关右侧的小箭头里，
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

function syncAgentUI() {
  const s = curSession();
  const on = !!(s && s.agent_enabled);
  const btn = $('#btn-agent');
  if (btn) btn.classList.toggle('active', on);
  // 设置入口只在 Agent 开启时出现；关掉 Agent 时顺手收起浮层
  const more = $('#btn-agent-more');
  if (more) more.classList.toggle('hidden', !on);
  if (!on) closeAgentMenu();
  const sb = $('#btn-sandbox');
  if (sb) {
    sb.classList.toggle('hidden', !on);
    const p = (s && s.sandbox) || '';
    const nm = $('#sandbox-name');
    if (nm) nm.textContent = p ? (p.split(/[\\/]/).filter(Boolean).pop() || p) : '目录';
    sb.title = p ? ('沙箱：' + p + '（点击更换）') : '选择 Agent 沙箱目录';
  }
  const or = $('#btn-outside-read');
  if (or) {
    or.classList.toggle('hidden', !on);
    or.classList.toggle('active', !!(on && s && s.allow_outside_read));
  }
  const pathBtn = $('#btn-sandbox-path');
  if (pathBtn) pathBtn.classList.toggle('hidden', !on);
  // 写入审批档位：勾选当前生效的那一项
  const ap = normApproval(s && s.approval_mode);
  document.querySelectorAll('#agent-menu .approve-opt').forEach((b) => {
    b.classList.toggle('active', b.dataset.mode === ap);
  });
  // 自动续跑：全局配置（不属于会话），默认 3 次
  const acRaw = parseInt((state.config || {}).agent_auto_continue, 10);
  const acv = Number.isFinite(acRaw) ? acRaw : 3;
  document.querySelectorAll('#agent-menu .continue-opt').forEach((b) => {
    b.classList.toggle('active', parseInt(b.dataset.continue, 10) === acv);
    b.onclick = () => setAutoContinue(parseInt(b.dataset.continue, 10) || 0);
  });
}

/* 自动续跑：模型想收尾但任务没做完时，由后端自动推它继续，最多 N 次。
   0 = 关闭（每轮结束就停，需要用户手动说「继续」）；-1 = 无上限。 */
async function setAutoContinue(n) {
  const raw = parseInt(n, 10);
  const v = raw < 0 ? -1 : Math.max(0, Math.min(10, raw || 0));
  const cfg = Object.assign({}, state.config || {}, { agent_auto_continue: v });
  try {
    await api('/api/config', cfg);
  } catch (e) { toast('设置失败：' + e); return; }
  state.config = cfg;
  syncAgentUI();
  toast(v < 0 ? '自动续跑：无上限'
              : (v ? ('自动续跑：最多 ' + v + ' 次') : '已关闭自动续跑'));
}

/* 写入审批档位：ask 手动 / ai 自动 / full 完全访问 / custom 自定义。
   旧会话里可能存着 auto/once/always。旧「自动」曾是默认值、不代表用户明确选择，
   一律收紧为「手动审批」，避免升级后仍然不问就写。 */
const APPROVAL_ALIASES = { auto: 'ask', once: 'ask', always: 'ask' };
const APPROVAL_MODES = ['ask', 'ai', 'full', 'custom'];
function normApproval(mode) {
  const k = String(mode || '').trim().toLowerCase();
  const v = APPROVAL_ALIASES[k] || k;
  return APPROVAL_MODES.indexOf(v) >= 0 ? v : 'ask';
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

// 允许 / 禁止 Agent 读取沙箱外的文件（只读，按会话保存）
async function toggleOutsideRead() {
  const s = curSession();
  if (!s) return;
  const on = !s.allow_outside_read;
  try {
    const r = await api('/api/session/agent', { id: s.id, allow_outside_read: on });
    applySessions(r);
  } catch (e) { toastErr('切换', e); return; }
  syncAgentUI();
  toast(on ? '已允许读取沙箱外文件（只读，写入仍限沙箱内）' : '已关闭沙箱外读取');
}

async function toggleAgent() {
  const s = curSession();
  if (!s) return;
  const on = !s.agent_enabled;
  try {
    const r = await api('/api/session/agent', { id: s.id, agent_enabled: on });
    applySessions(r);
  } catch (e) { toast('切换 Agent 失败：' + e); return; }
  if (on) {
    toast('已开启 Agent');
    // 不在这里自动弹目录选择框：那会与用户手动点 📁 叠加成两个窗口，也会让原本
    // 只想"先开着、稍后再选目录"的用户被强行弹窗打断。改成一句话指路。
    if (!(s.sandbox || '')) toast('请点工具栏的「目录」按钮选择沙箱目录');
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
    const r = await api('/api/sandbox/pick', { initial: (curSession() || {}).sandbox || '' });
    if (r && r.ok && r.path) {
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

// 手动输入 / 粘贴沙箱路径：不想在旧版系统目录框里一层层点时用
async function pickSandboxPath() {
  const s = curSession();
  if (!s) return;
  const raw = await appPrompt('输入或粘贴沙箱目录的绝对路径：', s.sandbox || '');
  if (raw === null) return;
  const path = raw.trim().replace(/^"(.*)"$/, '$1');
  if (!path) { toast('路径不能为空', 'error'); return; }
  try {
    const chk = await api('/api/sandbox/check', { path });
    if (!chk || !chk.ok || !chk.path) {
      toast('路径不存在或不是目录：' + path, 'error');
      return;
    }
    const res = await api('/api/session/agent', { id: s.id, sandbox: chk.path });
    applySessions(res);
    syncAgentUI();
    toast('沙箱已设为：' + chk.path);
  } catch (e) { toastErr('设置沙箱失败', e); }
}

/* ============================================================
   IDE / Solo 模式：以「当前会话沙箱」为根的工作区
   ============================================================ */
