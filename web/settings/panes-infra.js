'use strict';

/* 设置弹窗 · panes-infra.js
   MCP / Hooks / 余额 / 视觉开关 / 搜索页
   原 web/settings.js 拆分而来：按 index.html 中的顺序加载，共享全局作用域。 */

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
    checkRowHtml('search-modsearch', '启用 modsearch（免注册引擎链 + 云端抓页）', 'modsearch 开关 启用 免注册') +
    hintHtml('关闭后不调用任何免注册引擎，也不会把网页发给 Firecrawl 云端抓取；'
      + '已保存的「自动」会回落到聊天自带搜索 / 博查。', 'modsearch 关闭 开关') +
    setGroupHtml('服务商', '服务商 搜索 provider') +
    '<select id="search-provider" class="field-input">' + opts + '</select>' +
    '<p class="hint" id="search-provider-hint"></p>' +
    '<div id="search-auto-box">' +
      setGroupHtml('免注册引擎', '免注册 keyless firecrawl keenable youcom 引擎链') +
      hintHtml('三个引擎都不填 Key 也能用：Firecrawl 每月 1000 credits（搜索 + 云端抓页）、'
        + 'Keenable 每小时 1000 次 / IP、You.com 每天 100 次 / IP，都是共享额度。'
        + '填 Key 换自己的额度；取消勾选可把该引擎移出链。', '免注册 额度 keyless 引擎链') +
      labelHtml('Firecrawl Key（可空）', 'firecrawl key 密钥') +
      '<input id="search-ek-firecrawl" class="field-input" type="password" autocomplete="off">' +
      checkRowHtml('search-eon-firecrawl', '参与自动链：Firecrawl（搜索 + 云端抓页）', 'firecrawl 引擎') +
      labelHtml('Keenable Key（可空）', 'keenable key 密钥') +
      '<input id="search-ek-keenable" class="field-input" type="password" autocomplete="off">' +
      checkRowHtml('search-eon-keenable', '参与自动链：Keenable（仅搜索）', 'keenable 引擎') +
      labelHtml('You.com Key（可空，目前只有免注册端点）', 'youcom key 密钥') +
      '<input id="search-ek-youcom" class="field-input" type="password" autocomplete="off">' +
      checkRowHtml('search-eon-youcom', '参与自动链：You.com（仅搜索）', 'youcom 引擎') +
    '</div>' +
    '<div id="search-manual-box">' +
    checkRowHtml('search-prefer-api', '优先使用独立搜索 API（即使聊天模型自带搜索）', '优先 搜索 api 聊天模型 自带') +
    hintHtml('默认：聊天模型自带搜索时优先用它；'
      + '勾选上面的选项后，始终使用这里配置的服务商（博查 / 豆包 / Tavily）。',
      '优先 搜索 api 聊天模型') +
    setGroupHtml('接入', '接口 地址 密钥 api') +
    labelHtml('接口地址', '接口 地址 url endpoint baseurl') +
    '<input id="search-url" class="field-input" type="text">' +
    labelHtml('API 密钥', '密钥 key api token 认证') +
    '<input id="search-key" class="field-input" type="password">' +
    '<label class="check-row"><input type="checkbox" id="search-show-key"> 显示密钥</label>' +
    '</div>' +
    setGroupHtml('检索行为', '检索 条数 抓取') +
    checkRowHtml('search-research', '深度检索（默认开启：放宽来源条数 / 正文长度，弱结果自动补搜一次）', '深度 研究 检索 补搜') +
    checkRowHtml('search-cache', '复用检索缓存（同一 provider + 检索式在 TTL 内直接复用）', '缓存 cache 检索') +
    checkRowHtml('search-cite-nli', '引用语义校验（小模型判断结论是否被原文支持；会增加 5–20s）', '引用 语义 nli 校验 小模型') +
    checkRowHtml('search-fetch', '抓取正文（对命中网页取正文，而不是只有摘要）', '抓取 正文 网页') +
    checkRowHtml('search-fetch-cloud', '抓正文优先用云端浏览器（Firecrawl，可读 JS 渲染页面；URL 会发给 Firecrawl）', '抓取 云端 firecrawl js') +
    labelHtml('抓取正文条数（1–10）', '抓取 条数 count') +
    '<input id="search-fetch-count" class="field-input" type="number" min="1" max="10">' +
    setGroupHtml('信息量上限', '合并 条数 字数 上限') +
    labelHtml('每个检索式取多少条（1–20）', '条数 结果 数量 count 检索式') +
    '<input id="search-count" class="field-input" type="number" min="1" max="20">' +
    labelHtml('合并后给模型的条数上限（1–50）', '合并 条数 上限 最终') +
    '<input id="search-merged-max" class="field-input" type="number" min="1" max="50">' +
    hintHtml('多个检索式会先各取 N 条，再去重排序，最终最多保留「合并上限」条给模型；' +
             '只发一个检索式时通常就是 N 条。', '条数 合并 检索式 上限') +
    labelHtml('每条正文保留字数（500–20000）', '正文 字数 长度') +
    '<input id="search-fetch-chars" class="field-input" type="number" min="500" max="20000">' +
    setGroupHtml('深度检索上限（默认启用）', '深度 研究 复刻 还原 条数 字数') +
    labelHtml('深度检索合并条数（1–50，0 = 用上面的值）', '研究 合并 条数') +
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
  q('modsearch').checked = cfg.search_modsearch_enabled !== false;
  q('provider').value = cfg.search_provider || 'auto';
  q('prefer-api').checked = !!cfg.search_prefer_api;
  q('url').value = cfg.search_url || '';
  q('key').value = cfg.search_api_key || '';
  const ek = cfg.search_engine_keys || {};
  const ee = cfg.search_engine_enabled || {};
  ['firecrawl', 'keenable', 'youcom'].forEach((n) => {
    q('ek-' + n).value = ek[n] || '';
    q('eon-' + n).checked = ee[n] !== false;
  });
  q('count').value = cfg.search_count || 8;
  q('research').checked = cfg.search_research_enabled !== false;
  q('cache').checked = cfg.search_cache_enabled !== false;
  q('cite-nli').checked = !!cfg.search_cite_nli;
  q('fetch').checked = cfg.search_fetch !== false;
  q('fetch-cloud').checked = cfg.search_fetch_cloud !== false;
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
    // 自动模式用自己的每引擎 Key，单服务商模式才需要地址 / 密钥
    const auto = q('provider').value === 'auto';
    q('auto-box').classList.toggle('hidden', !auto);
    q('manual-box').classList.toggle('hidden', auto);
  };
  // modsearch 总开关：关掉后「自动」不可选，正选着就回落到博查；云端抓页也一起停
  const syncModsearch = () => {
    const on = q('modsearch').checked;
    const opt = q('provider').querySelector('option[value="auto"]');
    if (opt) opt.disabled = !on;
    if (!on && q('provider').value === 'auto') q('provider').value = 'bocha';
    q('fetch-cloud').disabled = !on;
    syncHint();
  };
  syncModsearch();
  q('provider').onchange = syncHint;
  q('modsearch').onchange = syncModsearch;
  q('show-key').onchange = (e) => { q('key').type = e.target.checked ? 'text' : 'password'; };

  const syncFetch = () => { q('fetch-count').disabled = !q('fetch').checked; };
  q('fetch').onchange = syncFetch;
  syncFetch();

  const num = (v, d) => {
    const n = parseInt(v, 10);
    return Number.isNaN(n) ? d : n;
  };
  pane._collect = () => ({
    search_modsearch_enabled: q('modsearch').checked,
    search_provider: q('provider').value,
    search_prefer_api: q('prefer-api').checked,
    search_url: q('url').value.trim(),
    search_api_key: q('key').value.trim(),
    search_engine_keys: {
      firecrawl: q('ek-firecrawl').value.trim(),
      keenable: q('ek-keenable').value.trim(),
      youcom: q('ek-youcom').value.trim(),
    },
    search_engine_enabled: {
      firecrawl: q('eon-firecrawl').checked,
      keenable: q('eon-keenable').checked,
      youcom: q('eon-youcom').checked,
    },
    search_count: num(q('count').value, 8),
    search_research_enabled: q('research').checked,
    search_cache_enabled: q('cache').checked,
    search_cite_nli: q('cite-nli').checked,
    search_fetch: q('fetch').checked,
    search_fetch_cloud: q('fetch-cloud').checked,
    search_fetch_count: num(q('fetch-count').value, 4),
    search_merged_max: Math.max(1, num(q('merged-max').value, 12)),
    search_fetch_chars: Math.max(500, num(q('fetch-chars').value, 2500)),
    search_research_merged_max: Math.max(0, num(q('research-merged').value, 20)),
    search_research_fetch_chars: Math.max(0, num(q('research-chars').value, 4000)),
    search_balance_url: q('balance-url').value.trim(),
  });
  return pane;
}
