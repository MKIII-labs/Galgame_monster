'use strict';

/* 设置弹窗 · panes-model.js
   模型接入页：聊天 / 视觉 / 上下文
   原 web/settings.js 拆分而来：按 index.html 中的顺序加载，共享全局作用域。 */

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
    html += hintHtml('摘要 / 记忆挑选 / 语音矫正这类轻量调用，可以改用更小更快的模型；'
                     + '三项都留空 = 沿用上面的主模型（默认，行为与从前一致）。');
    html += labelHtml('轻量模型名', '轻量 小模型 model') +
      '<input id="light-model" class="field-input" type="text" placeholder="留空则沿用主模型">';
    html += labelHtml('轻量接口地址（可选）', '轻量 小模型 地址 url') +
      '<input id="light-url" class="field-input" type="text" placeholder="留空则沿用上面的接口地址">';
    html += labelHtml('轻量 API 密钥（可选）', '轻量 小模型 密钥 key') +
      '<input id="light-key" class="field-input" type="password" placeholder="留空则沿用上面的密钥">';

    html += setGroupHtml('本地混合模式', '本地 混合 hybrid 压缩 token 省钱');
    html += hintHtml('开启：轻量任务走上面的轻量模型，并按「压缩触发上下文」提前压缩历史；'
                     + '仅轻量模型：主对话也只走轻量模型（本地 / 云端都行）；'
                     + '关闭：完全回到纯 API（忽略本地轻量模型，也不启用混合压缩）。');
    html += labelHtml('混合模式', '混合 hybrid 本地') +
      '<select id="hybrid-mode" class="field-input">' +
      '<option value="off">关闭（纯 API）</option>' +
      '<option value="on">开启（本地摘要 + 轻量任务）</option>' +
      '<option value="light">仅轻量模型（主对话也走轻量模型）</option>' +
      '</select>';
    html += hintHtml('「仅轻量模型」不限定本地还是云端：主对话只使用上面的轻量模型，'
                     + '不产生主 API 费用。轻量模型在本地（如 Ollama）时会自动停用'
                     + '文件 / 命令工具与联网搜索（本地上下文只有 8–12k），只保留普通问答；'
                     + '云端轻量模型按正常工具运行。');
    html += labelHtml('压缩触发上下文（tokens）', '压缩 hybrid 上下文') +
      '<input id="hybrid-ctx" class="field-input" type="number" min="8000" step="1000" placeholder="64000">';
    html += labelHtml('压缩保留比例（%）', '压缩 保留 hybrid') +
      '<input id="hybrid-keep" class="field-input" type="number" min="10" max="90" step="5" placeholder="45">';
    html += labelHtml('本地窗口（tokens）', '本地 ollama 上下文 window num_ctx') +
      '<input id="local-ctx" class="field-input" type="number" min="4000" step="1000" placeholder="12000">';
    html += hintHtml('必须与 Ollama 模型实际分配的 num_ctx 一致（默认模型是 8192，'
                     + '派生 32k 版本后填 32000）。填大了请求会超窗报错，填小了浪费可用上下文。',
                     '本地 窗口 ollama num_ctx');

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
    const hm = pane.querySelector('#hybrid-mode');
    if (hm) hm.value = cfg.hybrid_mode === 'light' ? 'light'
      : (cfg.hybrid_mode === 'on' ? 'on' : 'off');
    const hc = pane.querySelector('#hybrid-ctx');
    if (hc) hc.value = cfg.hybrid_context_limit || 64000;
    const hk = pane.querySelector('#hybrid-keep');
    if (hk) hk.value = Math.round((Number(cfg.hybrid_keep_ratio) || 0.45) * 100);
    const lc = pane.querySelector('#local-ctx');
    if (lc) lc.value = cfg.local_context_limit || 12000;
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
      const hm = pane.querySelector('#hybrid-mode');
      out.hybrid_mode = hm && hm.value === 'light' ? 'light'
        : (hm && hm.value === 'on' ? 'on' : 'off');
      const hc = pane.querySelector('#hybrid-ctx');
      out.hybrid_context_limit = hc ? Math.max(8000, parseInt(hc.value, 10) || 64000) : 64000;
      const hk = pane.querySelector('#hybrid-keep');
      out.hybrid_keep_ratio = hk
        ? Math.min(0.9, Math.max(0.1, (parseInt(hk.value, 10) || 45) / 100)) : 0.45;
      const lc = pane.querySelector('#local-ctx');
      out.local_context_limit = lc
        ? Math.max(4000, parseInt(lc.value, 10) || 12000) : 12000;
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
    labelHtml('模型上下文窗口（tokens，0 = 不限）', '上限 token 上下文 窗口 快捷 1m 128k') +
    '<input id="context-limit" class="field-input" type="number" min="0" step="1000">' +
    '<div class="ctx-presets" data-search="快捷 1M 200K 128K 64K 不限">' +
    '<button type="button" data-tok="0">不限</button>' +
    '<button type="button" data-tok="64000">64K</button>' +
    '<button type="button" data-tok="128000">128K</button>' +
    '<button type="button" data-tok="200000">200K</button>' +
    '<button type="button" data-tok="1000000">1M</button>' +
    '</div>' +
    hintHtml('模型一次能「看到」的总量（输入 + 输出都算）。点上面的快捷按钮直接填入：' +
             '64K / 128K / 200K / 1M。<strong>0 = 不限</strong>：不做模型窗口猜测，' +
             '也不按窗口触发压缩（只受请求体字符上限约束），进度圆环会隐藏；' +
             '填了具体值才按它算压缩时机，填多少就是多少。' +
             '手填的话：128K 填 <code>128000</code>，1M 填 <code>1000000</code>（1000 token ≈ 1700 汉字）。',
             '上限 token 窗口 快捷 自动 不限') +
    checkRowHtml('chat-auto-context',
      '提问时自动检索沙箱代码库（对齐 Trae 的 #Workspace）',
      '自动 上下文 代码库 检索 workspace 引用') +
    hintHtml('打开（默认）：每次提问按问题从本地代码索引召回最多 5 段相关代码，'
             + '回答底部给出「文件:行号」引用。索引没建好时会先在后台建一次，本轮不等待。'
             + '关闭后只使用你手动 @ 的文件。',
             '自动 上下文 代码库 检索 workspace 引用') +
    labelHtml('其中留给 AI 回答（tokens）', '预留 token 回复 输出') +
    '<input id="context-reserve" class="field-input" type="number" min="0" step="256">' +
    hintHtml('先从窗口里划出这一块，作为「同步压缩」的硬预留；默认 16384，常用 8192–16384。' +
             '填 0 表示不预留。实际单次输出还会被 max_tokens 和窗口 25% 夹紧' +
             '（窗口填 0 时不按窗口夹），所以这里不需要按回答长度去精确填。调大 → 更早触发同步压缩。',
             '预留 token 回复 默认 16384') +
    labelHtml('单次输出上限 max_tokens（0 = 用服务商默认）', '输出 上限 max_tokens 截断 长度') +
    '<input id="chat-max-tokens" class="field-input" type="number" min="0" max="65536" step="1024">' +
    hintHtml('单次模型调用最多生成多少 token，**硬截断**（思考模式的 reasoning 也计入）。' +
             '设得太小，长回复会在写工具块之前被截断，表现为「话说到一半停了、没有执行工具」；' +
             '默认 8192。填 0 表示不传，由服务商决定。实际还会被夹到窗口的 25%，' +
             '避免「输入 + 输出」越过窗口（窗口填 0 时不受这条限制）。',
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
    '<p><strong>窗口填 0（不限）时</strong>：不显示进度圆环、不按窗口做 25% 输出夹紧，' +
    '也不会触发预压缩；只有请求体字符上限（默认 50 万字符）兜底。' +
    '想要「快满了自动压缩」就填模型真实窗口。</p>' +
    '<p><strong>估算口径</strong>：中文约 0.6 token/字、英文约 0.3 token/字，再用接口返回的真实 usage 校准，' +
    '所以百分比是近似值。</p>' +
    '<p><strong>填错的后果</strong>：窗口填得比真实值大 → 进度条偏低、压缩太晚，可能真的报超限错误；' +
    '填得比真实值小 → 压缩过早，浪费窗口。</p>' +
    '</div></details>' +
    setGroupHtml('省费模式', '省费 token 引用 句柄 前缀 缓存') +
    checkRowHtml('token-profile-balanced',
      '省费模式：历史引用只留句柄、动态块贴到请求末尾',
      '省费 token 引用 句柄 前缀 缓存') +
    hintHtml('关闭（quality）时装配结果与旧版逐字节一致。打开后：@文件 / @目录 的正文只在当轮内联，'
             + '更早轮次只留「路径:行号」由模型按需读取；界面 / 意图提示贴到请求末尾，'
             + '避免每轮让整段历史失去前缀缓存。默认开启。',
             '省费 token 引用 句柄 前缀 缓存') +
    labelHtml('历史原文预算（tokens，0 = 关闭）', '省费 工作集 历史 原文 预算 token') +
    '<input id="history-working-set" class="field-input" type="number" min="0" step="1000">' +
    hintHtml('每轮最多重发多少历史原文；超出部分压进结构化摘要（当前回合始终保留原文）。'
             + '默认 40000。调大更保守也更费钱；调小更省，但更依赖模型按需重读文件。',
             '省费 工作集 历史 原文 预算 token') +
    labelHtml('单回合输入预算（tokens，0 = 不限）', '省费 单回合 预算 护栏 token') +
    '<input id="agent-turn-token-budget" class="field-input" type="number" min="0" step="1000000">' +
    hintHtml('一个回合累计输入超过它就先收尾并保留「继续任务」。默认 3000 万，只拦病态长跑。',
             '省费 单回合 预算 护栏 token') +
    labelHtml('历史附图保留条数（0 = 不重发）', '历史 图片 附图 多模态 回看 附件') +
    '<input id="image-history-keep" class="field-input" type="number" min="0" max="6" step="1">' +
    hintHtml('把最近几条带图消息重新作为图片附上（每条最多 2 张）。默认 2。'
             + '设为 0 时历史里只留「可回看附件」一行，模型可能要求你重新发图。',
             '历史 图片 附图 多模态 回看 附件') +
    labelHtml('自动召回内联预算（字符，0 = 不限）', '自动 召回 代码 上下文 预算 省费') +
    '<input id="auto-context-budget" class="field-input" type="number" min="0" step="1000">' +
    hintHtml('按问题自动检索代码时，只有相关度最高的前几段内联正文，其余只给「路径:行号」，'
             + '模型需要时自己 read_file。默认 8000 字符（约 2~3k token）。',
             '自动 召回 代码 上下文 预算 省费') +
    labelHtml('自动召回内联条数（0 = 不内联）', '自动 召回 代码 上下文 条数') +
    '<input id="auto-context-inline-max" class="field-input" type="number" min="0" step="1">' +
    checkRowHtml('agent-read-dedupe',
      '同回合重复读去重（文件没变就只回一行）',
      '重复 读 去重 read_file 省费') +
    hintHtml('同一回合里再次读同一个「版本未变」的文件时，不再重发全文，只回一行提示；'
             + '带 start / limit / raw 或文件已改动时照常读。默认开启。',
             '重复 读 去重 read_file 省费') +
    checkRowHtml('read-outline-mode',
      '大文件默认给符号大纲（需再按行号精读）',
      'read_file 大纲 符号 按需 省费') +
    hintHtml('对 600 行以上的 .py / .js / .ts，未指定 start / limit 时先返回符号表而不是全文。'
             + '会改变 read_file 的返回形态，默认关闭，建议先用评测确认模型适应。',
             'read_file 大纲 符号 按需 省费') +
    checkRowHtml('repo-summary-inject',
      '会话首次提问注入仓库结构',
      '仓库结构 目录 repomap 省费') +
    hintHtml('只在该会话第一次提问时注入一眼目录结构（约 1.2k 字符），减少盲目 list_dir。'
             + '默认关闭：会多一次目录遍历。',
             '仓库结构 目录 repomap 省费') +
    checkRowHtml('anthropic-cache',
      'Anthropic 通道启用提示缓存（cache_control）',
      'anthropic cache_control 缓存 省费') +
    hintHtml('只对主对话生效：稳定段与历史前缀各打一个缓存断点。'
             + '命中约按 0.1 倍计费、写入约 1.25 倍；OpenAI 兼容通道不受影响。默认开启。',
             'anthropic cache_control 缓存 省费') +
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
    checkRowHtml('session-memory-enabled',
      '会话工作记忆（后台轻量摘要，压缩时零 API 复用）',
      '会话 工作记忆 摘要 零 API 压缩') +
    hintHtml('开启（默认）：每累计约 6000 token，且工具调用达标或本轮自然停顿时，'
             + '后台用轻量模型把新增对话合并成一份结构化工作笔记。压缩时若它已覆盖待压区间、'
             + '没过期且区间没被编辑，就直接当摘要用，省掉一次摘要调用。'
             + '关闭后只走原有摘要流程；已有笔记仍会自然过期，不再更新。',
             '会话 工作记忆 摘要 零 API 压缩') +
    checkRowHtml('microcompact-enabled',
      '微压缩（60% 预算后摘要单条过大的工具结果）',
      '微压缩 microcompact 工具结果 摘要') +
    hintHtml('开启（默认）：上下文到预算 60% 后，对单条 ≥3000 token 的工具结果做一次轻量摘要，'
             + '只保留错误 / 数值 / 路径 / 命令 / 结论；每次最多 3 条，最近 2 条原文不动。'
             + '只影响本次请求，会话里的原文和落盘句柄都还在。',
             '微压缩 microcompact 工具结果 摘要') +
    setGroupHtml('记忆检索', '记忆 检索 挑选 模型 延迟 首字节') +
    checkRowHtml('memory-llm-pick',
      '用模型挑选记忆（更准，但会拖慢首字节）', '记忆 挑选 模型 检索 延迟 首字节') +
    hintHtml('关闭（默认）：按关键词 + IDF 排序直接取最相关的 12 条。' +
             '打开后会在「首字节之前」多一次轻量模型往返，实测装配耗时 p90 1.9 秒、最坏 7 秒；' +
             '记忆条目很多、或常问「上次那个」这类模糊指代时再打开。',
             '记忆 挑选 延迟 首字节') +
    labelHtml('每轮记忆注入预算（tokens，默认 3000）', '记忆 注入 预算 token') +
    '<input id="memory-inject-tokens" class="field-input" type="number" min="300" max="50000" step="500">' +
    hintHtml('每轮「相关记忆」整块的 token 上限；单条正文仍截 400 字，命中与邻居合并最多 12 条。'
             + '调大能多塞几条，但也更费 token；默认 3000。',
             '记忆 注入 预算 token') +
    checkRowHtml('memory-index-enabled',
      '常驻记忆索引（MEMORY.md 式标题清单）',
      '记忆 索引 MEMORY.md 常驻 标题 清单') +
    hintHtml('开启（默认）：把可访问的知识库 / 会话记忆渲染成 ≤200 行、≤25KB 的'
             + '「作用域·分类·标题 — 一行钩子」清单，随系统提示常驻；正文仍按需检索 / memory.search。'
             + '本地轻量小窗口模型不注入。',
             '记忆 索引 MEMORY.md 常驻 标题 清单') +
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
  q('session-memory-enabled').checked = cfg.session_memory_enabled !== false;
  q('microcompact-enabled').checked = cfg.microcompact_enabled !== false;
  q('memory-inject-tokens').value = cfg.memory_inject_tokens != null
    ? cfg.memory_inject_tokens : 3000;
  q('memory-index-enabled').checked = cfg.memory_index_enabled !== false;
  q('token-profile-balanced').checked = (cfg.token_profile || 'balanced') !== 'quality';
  q('history-working-set').value = cfg.history_working_set_tokens != null
    ? cfg.history_working_set_tokens : 40000;
  q('agent-turn-token-budget').value = cfg.agent_turn_token_budget != null
    ? cfg.agent_turn_token_budget : 30000000;
  q('image-history-keep').value = cfg.image_history_keep != null
    ? cfg.image_history_keep : 2;
  q('auto-context-budget').value = cfg.auto_context_budget_chars != null
    ? cfg.auto_context_budget_chars : 8000;
  q('auto-context-inline-max').value = cfg.auto_context_inline_max != null
    ? cfg.auto_context_inline_max : 2;
  q('agent-read-dedupe').checked = cfg.agent_read_dedupe !== false;
  q('read-outline-mode').checked = cfg.read_outline_mode === true;
  q('repo-summary-inject').checked = cfg.repo_summary_inject === true;
  q('anthropic-cache').checked = cfg.anthropic_cache !== false;
  q('chat-auto-context').checked = cfg.chat_auto_context !== false;
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
      calcEl.textContent = '已设为「不限」：不按窗口触发压缩、不显示进度圆环，'
        + '只受请求体字符上限（默认 50 万字符）约束。';
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
    session_memory_enabled: !!q('session-memory-enabled').checked,
    microcompact_enabled: !!q('microcompact-enabled').checked,
    memory_inject_tokens: Math.max(300,
      Math.min(50000, intOr(q('memory-inject-tokens').value, 3000))),
    memory_index_enabled: !!q('memory-index-enabled').checked,
    chat_auto_context: !!q('chat-auto-context').checked,
    // 0 是合法值（= 不限 / 关闭），不能兜底成默认值
    memory_soft_limit: Math.max(0, intOr(q('memory-soft-limit').value, 60)),
    memory_organize_every: Math.max(0, intOr(q('memory-organize-every').value, 20)),
    memory_list_recent: Math.max(12, Math.min(80, intOr(q('memory-list-recent').value, 40))),
    compress_ratio: Math.max(0.3, Math.min(0.95, intOr(q('compress-ratio-pct').value, 80) / 100)),
    token_profile: q('token-profile-balanced').checked ? 'balanced' : 'quality',
    history_working_set_tokens: Math.max(0, intOr(q('history-working-set').value, 40000)),
    agent_turn_token_budget: Math.max(0,
      intOr(q('agent-turn-token-budget').value, 30000000)),
    image_history_keep: Math.max(0, Math.min(6, intOr(q('image-history-keep').value, 2))),
    auto_context_budget_chars: Math.max(0, intOr(q('auto-context-budget').value, 8000)),
    auto_context_inline_max: Math.max(0,
      Math.min(8, intOr(q('auto-context-inline-max').value, 2))),
    agent_read_dedupe: !!q('agent-read-dedupe').checked,
    read_outline_mode: !!q('read-outline-mode').checked,
    repo_summary_inject: !!q('repo-summary-inject').checked,
    anthropic_cache: q('anthropic-cache').checked,
    compress_keep_ratio: Math.max(0.05, Math.min(0.6,
      intOr(q('compress-keep-pct').value, 25) / 100)),
    compress_summary_tokens: Math.max(0, intOr(q('compress-summary-tokens').value, 8192)),
  });
  return pane;
}
