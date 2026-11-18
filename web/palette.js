/* ============================================================
   全局命令面板（Ctrl / ⌘ K）
   ------------------------------------------------------------
   只做「跳转 / 触发已有动作」：工作区、设置页、最近会话、常用操作。
   不新增业务能力，不改变任何现有流程。
   ============================================================ */
'use strict';

(function () {
  const $ = (sel) => document.querySelector(sel);
  const palette = $('#palette');
  const input = $('#palette-input');
  const list = $('#palette-list');
  const trigger = $('#btn-palette');
  if (!palette || !input || !list) return;

  const SETTINGS = [
    ['chat', '模型接入'], ['search', '联网与数据源'], ['context', '上下文与压缩'],
    ['remind', '主动与提醒'], ['agent', 'Agent 执行'], ['agents', '子代理'],
    ['skills', 'Skill'], ['image', '图片生成'], ['profile', '用户画像与学习'],
    ['appearance', '主题与外观'], ['speech', '语音与朗读'], ['keys', '快捷键'],
    ['data', '数据与高级'], ['help', '功能说明'],
  ];

  const MODES = [
    ['solo', '对话', 'Solo 工作区'],
    ['ide', '工作区', 'IDE'],
    ['hall', '功能大厅', '6 个互动模块'],
    ['work', '工作台', '今日 / 任务 / 产物'],
  ];

  let items = [];
  let current = [];
  let active = 0;
  let lastFocus = null;

  const getState = () => (typeof state !== 'undefined' ? state : null);

  function buildItems() {
    const out = [];
    MODES.forEach(([mode, label, sub]) => {
      out.push({
        group: '工作区', label, sub,
        run: () => { if (typeof setUiMode === 'function') setUiMode(mode); },
      });
    });
    out.push({
      group: '工作区', label: '插件市场', sub: 'Python 插件 / MCP / 发现',
      run: () => { const b = $('#btn-plugins'); if (b) b.click(); },
    });
    out.push({
      group: '操作', label: '新建对话', sub: '立即开始一条新会话',
      run: () => { if (typeof newSession === 'function') newSession(); },
    });
    out.push({
      group: '操作', label: '搜索会话', sub: '按标题 / 内容搜索历史会话',
      run: () => { if (typeof ssOpen === 'function') ssOpen(); },
    });
    out.push({
      group: '操作', label: '打开设置', sub: 'Ctrl+, / 设置',
      run: () => { const b = $('#btn-settings'); if (b) b.click(); },
    });
    out.push({
      group: '操作', label: '切换界面风格 / 氛围', sub: '雾光氛围 · 扁平化深浅',
      run: () => { const b = $('#btn-theme'); if (b) b.click(); },
    });
    // 侧栏底部工具：窄屏（底部导航）下这些入口可能收进命令面板，保证全部可达
    [['知识库 / 记忆图谱', 'btn-kb'],
      ['笔记库', 'btn-notes'], ['刷新页面', 'btn-reload']].forEach(([label, id]) => {
      out.push({
        group: '工具', label, sub: '打开对应工具',
        run: () => { const b = document.getElementById(id); if (b) b.click(); },
      });
    });
    out.push({
      group: '操作', label: '打开检查器', sub: '上下文 / 工具 / 用量 / 轨迹',
      run: () => { if (window.uiShell) window.uiShell.openInspector(); },
    });

    SETTINGS.forEach(([tab, label]) => {
      out.push({
        group: '设置', label: '设置：' + label, sub: '打开对应设置页',
        run: () => {
          if (window.gmOpenSettings) window.gmOpenSettings({ tab });
          else if (typeof openModal === 'function') openModal({ tab });
        },
      });
    });

    const st = getState();
    const sessions = (st && st.sessions && st.sessions.sessions) || [];
    sessions.slice(0, 8).forEach((s) => {
      out.push({
        group: '最近会话', label: s.title || '新对话', sub: s.id === (st.sessions.current || '') ? '当前会话' : '切换到该会话',
        run: () => { if (typeof selectSession === 'function') selectSession(s.id); },
      });
    });
    return out;
  }

  function close() {
    if (palette.classList.contains('hidden')) return;
    palette.classList.add('hidden');
    if (lastFocus && typeof lastFocus.focus === 'function') lastFocus.focus();
  }

  function open() {
    lastFocus = document.activeElement;
    items = buildItems();
    active = 0;
    input.value = '';
    filter('');
    palette.classList.remove('hidden');
    input.focus();
  }

  function filter(q) {
    const key = String(q || '').trim().toLowerCase();
    current = key
      ? items.filter((it) => (it.label + ' ' + it.sub + ' ' + it.group).toLowerCase().includes(key))
      : items;
    if (active >= current.length) active = Math.max(0, current.length - 1);
    render();
  }

  function render() {
    list.innerHTML = '';
    if (!current.length) {
      const p = document.createElement('p');
      p.className = 'palette-empty';
      p.textContent = '没有匹配的结果';
      list.appendChild(p);
      return;
    }
    current.forEach((it, i) => {
      const row = document.createElement('button');
      row.type = 'button';
      row.className = 'palette-item' + (i === active ? ' active' : '');
      row.setAttribute('role', 'option');
      row.setAttribute('aria-selected', i === active ? 'true' : 'false');

      const body = document.createElement('span');
      body.className = 'pi-body';
      const b = document.createElement('b');
      b.textContent = it.label;
      const s = document.createElement('span');
      s.textContent = it.sub || '';
      body.appendChild(b);
      body.appendChild(s);

      const key = document.createElement('span');
      key.className = 'pi-key';
      key.textContent = it.group;
      row.appendChild(body);
      row.appendChild(key);

      row.onmousemove = () => {
        if (active === i) return;
        active = i;
        render();
      };
      row.onclick = () => run(it);
      list.appendChild(row);
    });
  }

  function run(it) {
    if (!it) return;
    close();
    setTimeout(() => {
      try { it.run(); } catch (e) { if (typeof toast === 'function') toast('执行失败：' + e, 'error'); }
    }, 0);
  }

  input.addEventListener('input', () => { active = 0; filter(input.value); });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      if (current.length) { active = (active + 1) % current.length; render(); }
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      if (current.length) { active = (active - 1 + current.length) % current.length; render(); }
    } else if (e.key === 'Enter') {
      e.preventDefault();
      run(current[active]);
    } else if (e.key === 'Escape') {
      e.preventDefault();
      close();
    }
  });

  palette.addEventListener('mousedown', (e) => {
    if (e.target === palette) close();
  });
  if (trigger) trigger.onclick = open;
  // 窄屏底部导航的「工具」项：打开同一命令面板（不新增业务能力）
  const railMore = $('#btn-rail-more');
  if (railMore) railMore.onclick = open;

  document.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === 'k') {
      e.preventDefault();
      if (palette.classList.contains('hidden')) open();
      else close();
      return;
    }
    if (e.key === 'Escape' && !palette.classList.contains('hidden')) {
      e.preventDefault();
      close();
    }
  });

  window.uiPalette = { open, close };
})();
