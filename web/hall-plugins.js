/* ============================================================
   插件 —— 侧栏入口（#btn-plugins）打开的整屏页面
   ------------------------------------------------------------
   两个 tab：
     已安装：Python 工具插件（启停 / 详情 / 卸载）+ MCP 服务器开关
     发现：内置官方插件（可安装）+ DSH 生态索引（只读）
   支持安装：内置源 / 本地目录 / zip / https Git 仓库。
   插件是任意 Python 代码：安装与启用都要确认，且重启应用后才加载。
   后端：/api/plugins（app/api/plugins.py）、/api/market（DSH 索引）
   ============================================================ */
'use strict';

let marketRefreshFn = null;

function marketMount(root) {
  const S = {
    tab: 'installed',
    plugins: { enabled: false, plugins: [], dir: '' },
    catalog: [],
    mcp: { enabled: false, tool_mode: 'lazy', servers: [] },
    dsh: { items: [], page: 1, total: 0, categories: {}, updated: '', stale: false, error: '' },
    source: 'builtin',          // builtin | dsh
    q: '',
    cat: '',
    busy: false,
  };

  root.innerHTML = '';
  const wrap = document.createElement('div');
  wrap.className = 'mk-wrap';

  /* ---------- 头部 ---------- */
  const hero = document.createElement('div');
  hero.className = 'gal-hall-hero';
  const title = document.createElement('div');
  title.className = 'gal-hall-title';
  title.textContent = '插件';
  const desc = document.createElement('p');
  desc.className = 'hint';
  desc.textContent = '管理 Python 工具插件与 MCP 服务器；也可以从内置源、本地目录、zip、Git 或单文件 .py 安装。'
    + '插件以你的权限运行，安装/启用前请确认来源；装完要重启应用才加载。';
  hero.appendChild(title);
  hero.appendChild(desc);

  const tabs = document.createElement('div');
  tabs.className = 'mk-tabs';
  const tabInstalled = document.createElement('button');
  tabInstalled.type = 'button';
  tabInstalled.className = 'mk-tab active';
  tabInstalled.textContent = '已安装';
  const tabDiscover = document.createElement('button');
  tabDiscover.type = 'button';
  tabDiscover.className = 'mk-tab';
  tabDiscover.textContent = '发现';
  tabs.appendChild(tabInstalled);
  tabs.appendChild(tabDiscover);

  /* ---------- 已安装 ---------- */
  const installedPanel = document.createElement('div');
  installedPanel.className = 'mk-panel';

  const masterBox = document.createElement('div');
  masterBox.className = 'mk-master';
  masterBox.innerHTML = '<label class="check-row">'
    + '<input type="checkbox" id="plugins-master"> 启用工具插件（总开关）</label>'
    + '<p class="hint">关闭时任何工具插件都不会加载。启停与安装都需要重启应用才生效。</p>';

  const pluginTitle = document.createElement('h4');
  pluginTitle.className = 'mk-section';
  pluginTitle.textContent = '工具插件';
  const pluginList = document.createElement('div');
  pluginList.className = 'mk-list';

  const mcpTitle = document.createElement('h4');
  mcpTitle.className = 'mk-section';
  mcpTitle.textContent = 'MCP 服务器';
  const mcpBox = document.createElement('div');
  mcpBox.className = 'mk-list';

  installedPanel.appendChild(masterBox);
  installedPanel.appendChild(pluginTitle);
  installedPanel.appendChild(pluginList);
  installedPanel.appendChild(mcpTitle);
  installedPanel.appendChild(mcpBox);

  /* ---------- 发现 ---------- */
  const discoverPanel = document.createElement('div');
  discoverPanel.className = 'mk-panel hidden';

  const sources = document.createElement('div');
  sources.className = 'mk-sources';
  const srcBuiltin = document.createElement('button');
  srcBuiltin.type = 'button';
  srcBuiltin.className = 'mk-tab active';
  srcBuiltin.textContent = '官方插件';
  const srcDsh = document.createElement('button');
  srcDsh.type = 'button';
  srcDsh.className = 'mk-tab';
  srcDsh.textContent = 'DSH 生态（只读）';
  sources.appendChild(srcBuiltin);
  sources.appendChild(srcDsh);

  const installBar = document.createElement('div');
  installBar.className = 'mk-toolbar';
  const localBtn = document.createElement('button');
  localBtn.type = 'button';
  localBtn.className = 'mk-btn';
  localBtn.textContent = '从本地目录安装';
  const remoteBtn = document.createElement('button');
  remoteBtn.type = 'button';
  remoteBtn.className = 'mk-btn';
  remoteBtn.textContent = '从 zip / Git 安装';
  const dirBtn = document.createElement('button');
  dirBtn.type = 'button';
  dirBtn.className = 'mk-btn';
  dirBtn.textContent = '打开插件目录';
  installBar.appendChild(localBtn);
  installBar.appendChild(remoteBtn);
  installBar.appendChild(dirBtn);

  const builtinBox = document.createElement('div');
  builtinBox.className = 'mk-grid';

  const dshBar = document.createElement('div');
  dshBar.className = 'mk-toolbar hidden';
  const dshSearch = document.createElement('input');
  dshSearch.type = 'search';
  dshSearch.className = 'mk-search';
  dshSearch.placeholder = '搜索插件 / 作者 / 包名（支持中英文描述）';
  const dshCat = document.createElement('select');
  dshCat.className = 'mk-select';
  const dshRef = document.createElement('button');
  dshRef.type = 'button';
  dshRef.className = 'mk-btn';
  dshRef.textContent = '刷新索引';
  dshBar.appendChild(dshSearch);
  dshBar.appendChild(dshCat);
  dshBar.appendChild(dshRef);

  const dshNotice = document.createElement('p');
  dshNotice.className = 'hint mk-hint hidden';
  dshNotice.textContent = '数据来自 awesome-dsh-plugin.com 公开索引，只读。DSH 插件是 Cordis/TypeScript 模块，'
    + '不能安装到本项目；要装请先装 DSH，再执行对应命令。';

  const dshMeta = document.createElement('div');
  dshMeta.className = 'mk-meta hidden';
  const dshGrid = document.createElement('div');
  dshGrid.className = 'mk-grid hidden';
  const dshMore = document.createElement('button');
  dshMore.type = 'button';
  dshMore.className = 'mk-btn mk-more hidden';
  dshMore.textContent = '加载更多';

  /* ---------- 安装教程（发现页顶部折叠） ---------- */
  const guide = document.createElement('details');
  guide.className = 'mk-guide';
  guide.innerHTML = '<summary><span class="mk-guide-arrow">›</span>'
    + '<span class="mk-guide-title">安装教程（3 分钟）</span>'
    + '<span class="mk-guide-sub">总开关 → 安装 → 重启</span></summary>'
    + '<div class="mk-guide-body">'
    + '<p class="mk-guide-step"><b>最快 3 步</b>：① 到「已安装」打开总开关（默认关闭）→ '
    + '② 选一种方式安装 → ③ 重启应用；状态显示「已加载」才算成功。</p>'
    + '<table class="mk-guide-table"><thead><tr><th>方式</th><th>怎么装</th><th>要求</th></tr></thead><tbody>'
    + '<tr><td>内置官方</td><td>「官方插件」卡片点安装</td><td>随项目发布；当前内置源为空</td></tr>'
    + '<tr><td>本地目录</td><td>「从本地目录安装」选目录</td><td>目录里有 plugin.json + 入口 .py</td></tr>'
    + '<tr><td>zip</td><td>「从 zip / Git 安装」填 .zip 路径</td><td>根目录（或只多套一层）有 plugin.json</td></tr>'
    + '<tr><td>Git</td><td>同上，填 https:// 仓库地址</td><td>公开仓库；根目录（或一层子目录）有 plugin.json</td></tr>'
    + '<tr><td>单文件 .py</td><td>同上填 .py 路径</td><td>文件名就是插件 id</td></tr>'
    + '<tr><td>手动</td><td>「打开插件目录」后放进去再重启</td><td>data/plugins/&lt;id&gt; 或 &lt;id&gt;.py</td></tr>'
    + '</tbody></table>'
    + '<p class="mk-guide-h">插件包最小结构</p>'
    + '<pre class="mk-guide-pre">plugin.json\n{\n  "id": "my_tool",\n  "name": "我的工具",\n'
    + '  "version": "1.0.0",\n  "description": "一句话说明",\n  "entry": "plugin.py",\n'
    + '  "permissions": ["read"]\n}\n\nplugin.py\nfrom app.agent import agent_tool\n\n@agent_tool(\n'
    + '    "hello", "打个招呼",\n'
    + '    {"type": "object", "properties": {"name": {"type": "string"}}},\n'
    + '    permission="read")\ndef hello(root, args):\n'
    + '    return "hello, " + str((args or {}).get("name") or "world")</pre>'
    + '<p class="mk-guide-h">兼容性三问</p>'
    + '<p class="mk-guide-step">① 是 Python 吗？② 入口用的是我们的 <code>@agent_tool</code> 吗？'
    + '③ 依赖在当前环境里吗？任一为否就不能直接装，按下面的流程改造。</p>'
    + '<p class="mk-guide-h">让 Agent 帮你改造 / 移植</p>'
    + '<ol class="mk-guide-list">'
    + '<li>把插件仓库所在目录设为 Agent 沙箱（不用把本项目设成沙箱）。</li>'
    + '<li>把仓库地址 + 目标发给 Agent，让它读 README 和入口文件。</li>'
    + '<li>Agent 给出结论：能直接装 / 需要适配 / 只能重写，并说明原因。</li>'
    + '<li>你决定方案和范围。</li>'
    + '<li>Agent 在<b>沙箱内</b>产出兼容包（plugin.json + plugin.py）；它写不进 data/plugins，也不能重启应用。</li>'
    + '<li>你在「插件」页用「从本地目录安装」选这个包 → 开总开关 → 重启验证。</li>'
    + '</ol>'
    + '<p class="mk-guide-step">插件本来就是我们的格式时，直接把 Git 地址填进「从 zip / Git 安装」即可，'
    + '不用经过 Agent。DSH 生态是 Cordis/TypeScript 模块，只能参考，装不进本项目。</p>'
    + '<p class="mk-guide-h">安全与限制</p>'
    + '<ul class="mk-guide-list">'
    + '<li>插件是任意 Python 代码，以你的权限运行；安装和启用都会二次确认。</li>'
    + '<li>总开关默认关闭；安装 / 卸载 / 启停都要重启应用才生效。</li>'
    + '<li>上限 300 个文件 / 20 MB；入口必须是包内 .py；不执行安装脚本，也不跟随软链。</li>'
    + '</ul></div>';

  discoverPanel.appendChild(guide);
  discoverPanel.appendChild(sources);
  discoverPanel.appendChild(installBar);
  discoverPanel.appendChild(builtinBox);
  discoverPanel.appendChild(dshBar);
  discoverPanel.appendChild(dshNotice);
  discoverPanel.appendChild(dshMeta);
  discoverPanel.appendChild(dshGrid);
  discoverPanel.appendChild(dshMore);

  wrap.appendChild(hero);
  wrap.appendChild(tabs);
  wrap.appendChild(installedPanel);
  wrap.appendChild(discoverPanel);
  root.appendChild(wrap);

  /* ---------- 详情弹层 ---------- */
  const modal = document.createElement('div');
  modal.className = 'mk-modal hidden';
  const card = document.createElement('div');
  card.className = 'mk-modal-card';
  const mHead = document.createElement('div');
  mHead.className = 'mk-modal-head';
  const mTitle = document.createElement('div');
  mTitle.className = 'mk-modal-title';
  const mClose = document.createElement('button');
  mClose.type = 'button';
  mClose.className = 'mk-btn';
  mClose.textContent = '关闭';
  mHead.appendChild(mTitle);
  mHead.appendChild(mClose);
  const mBody = document.createElement('div');
  mBody.className = 'mk-modal-body';
  card.appendChild(mHead);
  card.appendChild(mBody);
  modal.appendChild(card);
  wrap.appendChild(modal);
  mClose.onclick = () => modal.classList.add('hidden');
  modal.onclick = (e) => { if (e.target === modal) modal.classList.add('hidden'); };

  /* ---------- 已安装渲染 ---------- */
  function statusBadge(p) {
    const cls = p.status === 'loaded' ? 'on' : (p.status === 'error' ? 'off' : '');
    return '<span class="mk-badge ' + cls + '">' + escapeHtml(p.status_text || '') + '</span>';
  }

  function renderInstalled() {
    const master = $('#plugins-master');
    if (master) master.checked = !!S.plugins.enabled;
    pluginList.innerHTML = S.plugins.plugins.length
      ? S.plugins.plugins.map((p) => ('<div class="mk-plugin" data-id="' + escapeHtml(p.id) + '">'
        + '<div class="mk-card-head"><span class="mk-name">' + escapeHtml(p.name) + '</span>'
        + (p.version ? '<span class="mk-ver">v' + escapeHtml(p.version) + '</span>' : '')
        + statusBadge(p) + '</div>'
        + '<p class="mk-desc">' + escapeHtml(p.description || '无描述') + '</p>'
        + (p.permissions.length ? '<div class="mk-tags">'
          + p.permissions.map((x) => '<span class="mk-tag warn">' + escapeHtml(x) + '</span>').join('')
          + '</div>' : '')
        + '<div class="mk-card-foot"><span class="mk-cat">' + escapeHtml(p.id) + '</span>'
        + '<span class="mk-actions">'
        + '<label class="ui-switch" title="启用 / 停用"><input type="checkbox" data-act="toggle"'
        + (p.enabled ? ' checked' : '') + '><span class="ui-switch-track"></span></label>'
        + '<button type="button" class="mk-btn" data-act="detail">详情</button>'
        + '<button type="button" class="mk-btn danger" data-act="uninstall">卸载</button>'
        + '</span></div></div>')).join('')
      : '<p class="mk-empty">还没有安装工具插件。去「发现」安装，或把插件目录放进 data/plugins。'
        + ' <button type="button" class="mk-link" data-act="guide">看安装教程</button></p>';

    const mcpMaster = S.mcp.servers.length
      ? ('<div class="mk-master"><label class="check-row"><input type="checkbox" id="mcp-master"'
        + (S.mcp.enabled ? ' checked' : '') + '> 启用 MCP（总开关）</label>'
        + '<p class="hint">新增 / 编辑服务器请在「设置 → MCP」。启停后下一轮对话重新连接。</p></div>')
      : '';
    const rows = S.mcp.servers.length
      ? S.mcp.servers.map((s) => ('<div class="mk-plugin" data-mcp="' + escapeHtml(s.name) + '">'
        + '<div class="mk-card-head"><span class="mk-name">' + escapeHtml(s.name) + '</span>'
        + '<span class="mk-badge">' + escapeHtml(s.transport) + '</span></div>'
        + '<div class="mk-sub">' + escapeHtml(s.command || s.url || '') + '</div>'
        + '<div class="mk-card-foot"><span class="mk-cat">MCP</span><span class="mk-actions">'
        + '<label class="ui-switch" title="启用 / 停用"><input type="checkbox" data-act="mcp-toggle"'
        + (s.enabled ? ' checked' : '') + '><span class="ui-switch-track"></span></label>'
        + '</span></div></div>')).join('')
      : '<p class="mk-empty">还没有配置 MCP 服务器。去「设置 → MCP」添加。</p>';
    mcpBox.innerHTML = mcpMaster + rows;
  }

  /* ---------- 发现渲染 ---------- */
  function renderBuiltin() {
    builtinBox.innerHTML = S.catalog.length
      ? S.catalog.map((it) => ('<div class="mk-card">'
        + '<div class="mk-card-head"><span class="mk-name">' + escapeHtml(it.name) + '</span>'
        + (it.version ? '<span class="mk-ver">v' + escapeHtml(it.version) + '</span>' : '')
        + '<span class="mk-badge">' + (it.installed ? '已安装' : '官方') + '</span></div>'
        + '<p class="mk-desc">' + escapeHtml(it.description || '无描述') + '</p>'
        + (it.permissions.length ? '<div class="mk-tags">'
          + it.permissions.map((x) => '<span class="mk-tag warn">' + escapeHtml(x) + '</span>').join('')
          + '</div>' : '')
        + '<div class="mk-card-foot"><span class="mk-cat">' + escapeHtml(it.id) + '</span>'
        + '<span class="mk-actions">'
        + (it.installed
          ? '<button type="button" class="mk-btn" disabled>已安装</button>'
          : '<button type="button" class="mk-btn primary" data-act="install-builtin" data-id="'
            + escapeHtml(it.id) + '">安装</button>')
        + '</span></div></div>')).join('')
      : '<p class="mk-empty">内置源为空。可以用「从本地目录安装」或「从 zip / Git 安装」。</p>';
  }

  function fillDshCats() {
    const keep = dshCat.value;
    dshCat.innerHTML = '<option value="">全部分类</option>' +
      Object.keys(S.dsh.categories || {}).sort().map((k) =>
        '<option value="' + escapeHtml(k) + '">'
        + escapeHtml(S.dsh.categories[k] || k) + '</option>').join('');
    if (keep) dshCat.value = keep;
  }

  function renderDsh(append) {
    if (!S.dsh.items.length) {
      dshGrid.innerHTML = '<p class="mk-empty">'
        + escapeHtml(S.dsh.error || '没有匹配的插件。') + '</p>';
      dshMeta.textContent = '';
      dshMore.classList.add('hidden');
      return;
    }
    const cards = S.dsh.items.map((p) => {
      const zh = (p.description || {}).zh || '';
      const en = (p.description || {}).en || '';
      const caps = (p.capabilities || []).length
        ? '<div class="mk-tags"><span class="mk-tag warn">权限：'
          + escapeHtml((p.capabilities || []).join(' / ')) + '</span></div>'
        : '';
      const stats = [];
      if (p.stars != null) stats.push('★ ' + p.stars);
      if (p.downloads != null) stats.push('↓ ' + p.downloads);
      if (p.version) stats.push('v' + p.version);
      return '<div class="mk-card">'
        + '<div class="mk-card-head"><span class="mk-name">' + escapeHtml(p.name) + '</span>'
        + '<span class="mk-badge">'
        + escapeHtml(S.dsh.categories[p.category] || p.category || '未分类') + '</span></div>'
        + '<p class="mk-desc">' + escapeHtml(zh || en || '（无描述）') + '</p>'
        + '<div class="mk-sub">' + escapeHtml(p.owner || '')
        + (stats.length ? ' · ' + escapeHtml(stats.join(' · ')) : '') + '</div>'
        + caps
        + '<div class="mk-card-foot"><span class="mk-cat">只读</span><span class="mk-actions">'
        + (p.page ? '<a class="mk-btn" href="' + escapeHtml(p.page)
          + '" target="_blank" rel="noopener">主页</a>' : '')
        + (p.url ? '<a class="mk-btn" href="' + escapeHtml(p.url)
          + '" target="_blank" rel="noopener">仓库</a>' : '')
        + (p.install ? '<button type="button" class="mk-btn primary" data-act="copy" data-cmd="'
          + escapeHtml(p.install) + '">复制安装命令</button>' : '')
        + '</span></div></div>';
    }).join('');
    if (append) dshGrid.insertAdjacentHTML('beforeend', cards);
    else dshGrid.innerHTML = cards;
    const shown = dshGrid.querySelectorAll('.mk-card').length;
    dshMeta.textContent = '共 ' + S.dsh.total + ' 条 · 已显示 ' + shown
      + (S.dsh.updated ? ' · 索引更新于 ' + S.dsh.updated : '')
      + (S.dsh.stale ? ' · 离线缓存' : '');
    dshMore.classList.toggle('hidden', shown >= S.dsh.total);
  }

  function showSource(src) {
    S.source = src;
    const builtin = src === 'builtin';
    srcBuiltin.classList.toggle('active', builtin);
    srcDsh.classList.toggle('active', !builtin);
    builtinBox.classList.toggle('hidden', !builtin);
    dshBar.classList.toggle('hidden', builtin);
    dshNotice.classList.toggle('hidden', builtin);
    dshMeta.classList.toggle('hidden', builtin);
    dshGrid.classList.toggle('hidden', builtin);
    dshMore.classList.toggle('hidden', builtin);
    if (!builtin && !S.dsh.items.length) loadDsh(1, false);
  }

  /* ---------- 数据 ---------- */
  function applyPayload(r) {
    S.plugins = r.plugins || S.plugins;
    S.catalog = r.catalog || S.catalog;
    S.mcp = r.mcp || S.mcp;
    renderInstalled();
    renderBuiltin();
  }

  async function loadPlugins() {
    try {
      applyPayload(await api('/api/plugins', { op: 'list' }));
    } catch (e) {
      pluginList.innerHTML = '<p class="mk-empty">读取失败：' + escapeHtml(e.message) + '</p>';
    }
  }

  async function loadDsh(page, refresh) {
    if (S.busy) return;
    S.busy = true;
    if (!page) page = 1;
    const first = page <= 1;
    try {
      const r = await api('/api/market', {
        op: 'dsh', q: S.q, category: S.cat, page: page, size: 24, refresh: !!refresh,
      });
      S.dsh.items = first ? (r.items || []) : S.dsh.items.concat(r.items || []);
      S.dsh.total = r.total || 0;
      S.dsh.page = r.page || page;
      S.dsh.categories = r.categories || {};
      S.dsh.updated = r.updated || '';
      S.dsh.stale = !!r.stale;
      S.dsh.error = r.error || '';
      fillDshCats();
      renderDsh(!first);
    } catch (e) {
      if (first) {
        S.dsh.items = [];
        S.dsh.total = 0;
        S.dsh.error = e.message;
        renderDsh(false);
      } else {
        toastErr('加载更多', e);
      }
    } finally {
      S.busy = false;
    }
  }

  async function showDetail(id) {
    try {
      const r = await api('/api/plugins', { op: 'detail', id: id });
      const it = r.item || {};
      mTitle.textContent = it.name || id;
      const meta = [
        it.version ? '版本 ' + it.version : '',
        it.author ? '作者 ' + it.author : '',
        it.id ? 'id ' + it.id : '',
        it.path ? it.path : '',
      ].filter(Boolean).join(' · ');
      mBody.innerHTML = '<p class="hint">' + escapeHtml(meta) + '</p>'
        + (it.permissions && it.permissions.length
          ? '<p class="mk-tags">' + it.permissions.map((x) =>
            '<span class="mk-tag warn">' + escapeHtml(x) + '</span>').join('') + '</p>' : '')
        + '<pre class="mk-readme">' + escapeHtml(it.readme || '（没有 README）') + '</pre>';
      modal.classList.remove('hidden');
    } catch (e) { toastErr('读取插件详情', e); }
  }

  async function install(source, payload, label) {
    if (!confirm('安装「' + label + '」？\n插件会以你的权限运行 Python 代码，只安装可信来源。')) return;
    try {
      const r = await api('/api/plugins', Object.assign({ op: 'install', source: source }, payload));
      applyPayload(r);
      toast(r.note || ('已安装：' + (r.id || label)), 'ok');
    } catch (e) { toastErr('安装', e); }
  }

  /* ---------- 事件 ---------- */
  masterBox.addEventListener('change', async (e) => {
    const cb = e.target.closest('#plugins-master');
    if (!cb) return;
    if (cb.checked && !confirm('启用工具插件？\n所有已启用的插件脚本都会以你的权限运行。')) {
      cb.checked = false;
      return;
    }
    try {
      const r = await api('/api/plugins', { op: 'master', enabled: cb.checked });
      applyPayload(r);
      toast(cb.checked ? '已开启（重启后生效）' : '已关闭（重启后生效）', 'ok');
    } catch (err) { cb.checked = !cb.checked; toastErr('保存总开关', err); }
  });

  pluginList.addEventListener('click', async (e) => {
    const btn = e.target.closest('[data-act]');
    if (!btn) return;
    if (btn.dataset.act === 'guide') {
      switchTab('discover');
      guide.open = true;
      guide.scrollIntoView({ block: 'nearest' });
      return;
    }
    const card = btn.closest('.mk-plugin');
    const id = card && card.dataset.id;
    if (!id) return;
    if (btn.dataset.act === 'detail') return showDetail(id);
    if (btn.dataset.act === 'uninstall') {
      if (!confirm('卸载插件「' + id + '」？目录会被删除，重启后生效。')) return;
      try {
        applyPayload(await api('/api/plugins', { op: 'uninstall', id: id }));
        toast('已卸载：' + id, 'ok');
      } catch (err) { toastErr('卸载', err); }
    }
  });

  pluginList.addEventListener('change', async (e) => {
    const cb = e.target.closest('[data-act="toggle"]');
    if (!cb) return;
    const card = cb.closest('.mk-plugin');
    const id = card && card.dataset.id;
    if (!id) return;
    if (cb.checked && !confirm('启用插件「' + id + '」？\n它会在重启后以你的权限运行。')) {
      cb.checked = false;
      return;
    }
    try {
      applyPayload(await api('/api/plugins', { op: 'toggle', id: id, enabled: cb.checked }));
      toast('已保存（重启后生效）', 'ok');
    } catch (err) { cb.checked = !cb.checked; toastErr('切换插件', err); }
  });

  mcpBox.addEventListener('change', async (e) => {
    if (e.target.id === 'mcp-master') {
      try {
        applyPayload(await api('/api/plugins', { op: 'mcp_master', enabled: e.target.checked }));
        toast(e.target.checked ? 'MCP 已开启' : 'MCP 已关闭', 'ok');
      } catch (err) { e.target.checked = !e.target.checked; toastErr('保存 MCP 总开关', err); }
      return;
    }
    const cb = e.target.closest('[data-act="mcp-toggle"]');
    if (!cb) return;
    const row = cb.closest('[data-mcp]');
    const name = row && row.dataset.mcp;
    if (!name) return;
    try {
      applyPayload(await api('/api/plugins', { op: 'mcp_toggle', name: name, enabled: cb.checked }));
      toast('已保存：' + name, 'ok');
    } catch (err) { cb.checked = !cb.checked; toastErr('切换 MCP', err); }
  });

  builtinBox.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-act="install-builtin"]');
    if (!btn) return;
    const it = S.catalog.find((x) => x.id === btn.dataset.id) || { name: btn.dataset.id };
    install('builtin', { id: btn.dataset.id }, it.name);
  });

  localBtn.onclick = async () => {
    try {
      const pick = await api('/api/sandbox/pick', {});
      if (!pick || !pick.ok || !pick.path) return;
      install('path', { path: pick.path }, pick.path);
    } catch (e) { toastErr('选择目录', e); }
  };
  remoteBtn.onclick = async () => {
    const raw = await appPrompt('输入 .zip 路径、单文件 .py 路径，或 https:// 开头的 Git 仓库地址'
      + '（安装后重启应用才会加载）', '');
    if (raw === null) return;
    const v = String(raw).trim();
    if (!v) return;
    if (/^https:\/\//i.test(v)) install('git', { url: v }, v);
    else install('path', { path: v }, v);
  };
  dirBtn.onclick = () => {
    api('/api/plugins', { op: 'open_dir' }).catch((e) => toastErr('打开插件目录', e));
  };

  dshGrid.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-act="copy"]');
    if (!btn) return;
    const text = btn.dataset.cmd || '';
    const done = () => {
      btn.textContent = '已复制';
      setTimeout(() => { btn.textContent = '复制安装命令'; }, 1200);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done).catch(() => window.prompt('复制这条命令：', text));
    } else {
      window.prompt('复制这条命令：', text);
    }
  });

  let timer = null;
  dshSearch.addEventListener('input', () => {
    S.q = dshSearch.value || '';
    clearTimeout(timer);
    timer = setTimeout(() => loadDsh(1, false), 260);
  });
  dshCat.addEventListener('change', () => { S.cat = dshCat.value || ''; loadDsh(1, false); });
  dshRef.onclick = () => loadDsh(1, true);
  dshMore.onclick = () => loadDsh(S.dsh.page + 1, false);

  function switchTab(id) {
    S.tab = id;
    tabInstalled.classList.toggle('active', id === 'installed');
    tabDiscover.classList.toggle('active', id === 'discover');
    installedPanel.classList.toggle('hidden', id !== 'installed');
    discoverPanel.classList.toggle('hidden', id !== 'discover');
    if (id === 'discover' && S.source === 'dsh' && !S.dsh.items.length) loadDsh(1, false);
  }
  tabInstalled.onclick = () => switchTab('installed');
  tabDiscover.onclick = () => switchTab('discover');
  srcBuiltin.onclick = () => showSource('builtin');
  srcDsh.onclick = () => showSource('dsh');

  marketRefreshFn = loadPlugins;
  loadPlugins();
}

/* 进入插件页：首次挂载；之后只刷新已安装列表 */
function marketEnter() {
  const body = document.getElementById('plugins-body');
  if (!body) return;
  if (!body.dataset.mounted) {
    body.dataset.mounted = '1';
    marketMount(body);
  } else if (typeof marketRefreshFn === 'function') {
    marketRefreshFn();
  }
}

window.marketMount = marketMount;
window.marketEnter = marketEnter;

/* 侧栏入口：走 setUiMode，和 Solo / IDE / 大厅 / 工作台 共用一套模式切换与持久化 */
function marketBindEntry() {
  const btn = document.getElementById('btn-plugins');
  if (!btn) return;
  btn.onclick = () => {
    if (typeof setUiMode === 'function') setUiMode('plugins');
    else if (typeof applyUiMode === 'function') applyUiMode('plugins');
    else marketEnter();
  };
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', marketBindEntry);
} else {
  marketBindEntry();
}
