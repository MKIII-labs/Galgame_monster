'use strict';

/* 设置弹窗 · help.js
   功能说明页：目录 / 筛选 / 去设置
   原 web/settings.js 拆分而来：按 index.html 中的顺序加载，共享全局作用域。 */

/* ============================================================
   功能说明页：目录 / 筛选 / 展开收起 / 去设置
   ============================================================ */

// 条目标题 → [设置分页, 定位关键词]。没有对应设置页的条目不列（不给假的入口）。
const HELP_SET_TARGET = {
  '图像识别：聊天模型直发 vs 独立视觉 API': ['chat', 'vision'],
  '账户余额': ['chat', 'balance'],
  '深度思考：开关、档位与参数模式': ['chat', 'reasoning'],
  '上下文上限（回复末尾的圆环）': ['context', '压缩'],
  '省费模式：历史预算 / 引用句柄 / 提示缓存': ['context', '省费'],
  '联网搜索（判据 / 抓取 / 来源）': ['search', '搜索'],
  '用户画像：手动偏好 vs AI 自主记录': ['profile', '画像'],
  '学习指令（预设与自定义）': ['profile', '学习指令'],
  '主题与外观（界面主题 / IDE 主题）': ['appearance', '主题'],
  '语音输入（实时转写 + AI 矫正）': ['speech', '语音'],
  '台词朗读（TTS）：三种后端与音色克隆': ['speech', '朗读'],
  'IDE / Solo 模式（VS Code 风格工作区）': ['appearance', 'ide'],
  'Agent 沙箱（文件读写与命令）与审批': ['agent', 'agent'],
  '执行计划与分步思考（过程里的「步骤 2/5」）': ['agent', '计划'],
  '自动续跑：什么时候自己继续、什么时候会停': ['agent', '续跑'],
  '变更审阅：块对 diff、标签页统计与提示栏': ['agent', '审阅'],
  '快捷键总览': ['keys', '快捷键'],
};

let helpGroups = [];
let helpOpenSnap = null;      // 筛选前的展开状态，清空筛选时还原
let helpSpyBound = false;

function helpPaneEl() {
  return settingsPaneEl('help');
}

function initHelpPane() {
  const pane = helpPaneEl();
  const head = pane && pane.querySelector('#help-head');
  if (!pane || !head || pane.dataset.helpReady === '1') return;
  pane.dataset.helpReady = '1';

  // 按文档顺序切组：h4 开头，后面到下一个 h4 之间的 details 属于该组
  const nodes = Array.from(pane.querySelectorAll('h4.set-group, details.help-sec'));
  helpGroups = [];
  let cur = null;
  nodes.forEach((el, i) => {
    if (el.tagName === 'H4') {
      cur = { h4: el, title: el.textContent.trim(), secs: [] };
      helpGroups.push(cur);
    } else if (cur) {
      cur.secs.push(el);
      if (!el.id) el.id = 'help-' + (i + 1);    // 稳定锚点，便于深链
    }
  });

  head.innerHTML =
    '<div class="help-toolbar">' +
    '<input id="help-filter" class="help-filter" type="search" ' +
    'placeholder="筛选本页说明（如 压缩 / 通知 / 计划）" aria-label="筛选功能说明">' +
    '<button class="help-act" type="button" data-act="toggle">全部展开</button>' +
    '</div>' +
    '<div class="help-toc-row"><div class="help-toc" id="help-toc"></div>' +
    '<span class="help-count" id="help-count"></span></div>';

  const toc = head.querySelector('#help-toc');
  toc.innerHTML = helpGroups.map((g, i) =>
    '<button type="button" data-g="' + i + '">' + escapeHtml(g.title) +
    '<span class="help-toc-n">' + g.secs.length + '</span></button>').join('');
  toc.querySelectorAll('button').forEach((b) => {
    const g = helpGroups[Number(b.dataset.g)];
    g.chip = b;
    b.onclick = () => helpJumpGroup(g);
  });

  helpGroups.forEach((g) => {
    const note = document.createElement('span');
    note.className = 'help-group-note';
    note.textContent = g.secs.length + ' 条';
    g.h4.appendChild(note);

    g.secs.forEach((sec) => {
      const sum = sec.querySelector('summary');
      if (!sum) return;
      const title = sum.textContent.trim();
      sum.textContent = '';
      const span = document.createElement('span');
      span.className = 'help-sec-title';
      span.textContent = title;
      span.dataset.text = title;
      sum.appendChild(span);
      const t = HELP_SET_TARGET[title];
      if (t) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'help-set';
        btn.textContent = '去设置';
        btn.title = '跳到「' + settingsPaneName(t[0]) + '」中对应的设置项';
        btn.onclick = (e) => {
          e.preventDefault();
          e.stopPropagation();     // 别让 summary 顺手把条目收起
          helpGoSetting(t[0], t[1]);
        };
        sum.appendChild(btn);
      }
    });
  });

  const filter = head.querySelector('#help-filter');
  filter.oninput = () => helpApplyFilter(filter.value);
  head.querySelector('[data-act="toggle"]').onclick = () => {
    const btn = head.querySelector('[data-act="toggle"]');
    // 还有没展开的（且没被筛掉）就全开，否则全收
    const anyClosed = helpGroups.some((g) => g.secs.some(
      (s) => !s.open && !s.classList.contains('help-hidden')));
    helpGroups.forEach((g) => g.secs.forEach((s) => {
      s.open = anyClosed && !s.classList.contains('help-hidden');
    }));
    btn.textContent = anyClosed ? '全部收起' : '全部展开';
  };

  helpBindSpy(pane);
  helpCountUpdate();
  helpSyncHeadHeight(pane);

  // 深链：#help-12 / openModal({ tab:'help', anchor:'help-12' })
  const m = /^#(help-\d+)$/.exec(location.hash || '');
  if (m) openHelpAnchor(m[1]);
}

function resetHelpFilter() {
  const f = document.querySelector('#modal #help-filter');
  if (f) f.value = '';
  if (helpGroups.length) helpApplyFilter('');
}

function helpCountUpdate(hit) {
  const el = document.querySelector('#modal #help-count');
  if (!el) return;
  const total = helpGroups.reduce((n, g) => n + g.secs.length, 0);
  el.textContent = (hit == null || hit === total) ? ('共 ' + total + ' 条') : ('命中 ' + hit + ' / ' + total);
}

function helpApplyFilter(q) {
  const pane = helpPaneEl();
  if (!pane || !helpGroups.length) return;
  const kws = String(q || '').trim().toLowerCase().split(/\s+/).filter(Boolean);
  if (kws.length && !helpOpenSnap) {
    helpOpenSnap = helpGroups.map((g) => g.secs.map((s) => s.open));
  } else if (!kws.length && helpOpenSnap) {
    // 清空筛选：回到筛选前看到的样子
    helpGroups.forEach((g, gi) => g.secs.forEach((s, si) => {
      s.open = !!(helpOpenSnap[gi] && helpOpenSnap[gi][si]);
    }));
    helpOpenSnap = null;
  }

  let shown = 0;
  helpGroups.forEach((g) => {
    let visible = 0;
    g.secs.forEach((sec) => {
      const title = sec.querySelector('.help-sec-title');
      const text = (title && title.dataset.text) || (title ? title.textContent : '');
      // 正文也参与匹配：用户想找「通知」时，正文提到的那条也该命中
      if (sec.dataset.plain == null) sec.dataset.plain = (sec.textContent || '').toLowerCase();
      const hay = ((sec.dataset.search || '') + ' ' + text + ' ' + sec.dataset.plain).toLowerCase();
      const hit = kws.every((k) => hay.includes(k));
      sec.classList.toggle('help-hidden', !hit);
      if (hit) {
        visible += 1;
        if (kws.length) sec.open = true;      // 筛选时直接展开，省一次点击
      }
      if (title) title.innerHTML = kws.length ? markSearchText(text, kws) : escapeHtml(text);
    });
    // 整组没命中就一起藏（h4、它所在的 set-card 与目录 chip）
    const card = g.h4.closest('.set-card');
    g.h4.classList.toggle('help-hidden', !visible);
    if (card) card.classList.toggle('help-hidden', !visible);
    if (g.chip) {
      g.chip.classList.toggle('help-hidden', !visible);
      const n = g.chip.querySelector('.help-toc-n');
      if (n) n.textContent = kws.length ? (visible + '/' + g.secs.length) : String(g.secs.length);
    }
    shown += visible;
  });
  helpCountUpdate(kws.length ? shown : null);
  const box = pane.parentElement;
  if (kws.length && box) box.scrollTop = 0;
}

function helpJumpGroup(g) {
  if (!g) return;
  g.h4.scrollIntoView({ block: 'start', behavior: 'smooth' });
}

function helpGoSetting(tab, kw) {
  const pane = settingsPaneEl(tab);
  if (!pane) return;
  setSettingsTab(settingsPageOfPane(tab));
  let target = null;
  if (kw) {
    const k = String(kw).toLowerCase();
    target = Array.from(pane.querySelectorAll('[data-search]')).find(
      (el) => (el.dataset.search || '').toLowerCase().includes(k));
  }
  requestAnimationFrame(() => {
    (target || pane).scrollIntoView({ block: 'center', behavior: 'smooth' });
    if (!target) return;
    target.classList.add('help-flash');
    setTimeout(() => target.classList.remove('help-flash'), 1200);
  });
}

function helpBindSpy(pane) {
  if (helpSpyBound) return;
  const box = pane.parentElement;
  if (!box) return;
  helpSpyBound = true;
  box.addEventListener('scroll', () => {
    if (!isOpen($('#modal')) || pane.classList.contains('hidden')) return;
    helpUpdateSpy();
  }, { passive: true });
  window.addEventListener('resize', () => helpSyncHeadHeight(pane));
}

/* 「功能说明」内部的吸附区贴在滚动区顶部。设置页页头现在已经不吸顶，
   只有页头仍是 sticky 时才需要为它让位（保留兼容，方便以后改回去）。
   高度同时写进 CSS 变量，供目录高亮与 scroll-margin 使用 */
function helpSyncHeadHeight(pane) {
  const head = pane && pane.querySelector('#help-head');
  if (!head) return;
  const box = pane.parentElement;
  const pageHead = box && box.querySelector('.set-page-head');
  let ph = 0;
  if (pageHead && getComputedStyle(pageHead).position === 'sticky') {
    ph = Math.round(pageHead.getBoundingClientRect().height);
  }
  head.style.top = ph + 'px';
  try {
    pane.style.setProperty('--help-head-h',
      (ph + Math.round(head.getBoundingClientRect().height) + 28) + 'px');
  } catch (e) { /* 忽略 */ }
}

function helpUpdateSpy() {
  const pane = helpPaneEl();
  const toc = pane && pane.querySelector('#help-toc');
  if (!pane || !toc || !helpGroups.length || pane.classList.contains('hidden')) return;
  const box = pane.parentElement;
  // 用同步好的「页头 + 吸附区」高度：否则分组标题刚滚到它下方时，高亮还停在上一个组
  const hh = parseFloat(getComputedStyle(pane).getPropertyValue('--help-head-h')) || 120;
  const top = box.getBoundingClientRect().top + hh;
  let idx = 0;
  helpGroups.forEach((g, i) => {
    if (g.h4.classList.contains('help-hidden')) return;
    if (g.h4.getBoundingClientRect().top <= top) idx = i;
  });
  toc.querySelectorAll('button').forEach((b) => {
    b.classList.toggle('active', Number(b.dataset.g) === idx);
  });
}

function openHelpAnchor(id) {
  const sec = document.getElementById(id);
  if (!sec) return;
  sec.open = true;
  requestAnimationFrame(() => sec.scrollIntoView({ block: 'center', behavior: 'smooth' }));
}
