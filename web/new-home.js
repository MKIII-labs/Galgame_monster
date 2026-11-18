'use strict';

/* ============================================================
   新会话首屏交互（#empty.nh）
   ------------------------------------------------------------
   index.html 只放结构；这里负责：
   · 时段问候 / 状态 chips（模型、会话数、沙箱）
   · 「灵感 / 继续 / 工作区」分段切换：滑动指示器 + 面板高度过渡
   · 最近会话列表（运行时填充）
   · 卡片指针跟随高光
   · 响应式重排 / 减少动态适配
   chat.js 的 bindChips() 会在首屏插入后调用 window.gmHomeBind(root)。
   ============================================================ */
(function () {
  const NH = { root: null, hTimer: 0, greetTimer: 0 };

  function q(sel, root) { return (root || document).querySelector(sel); }
  function qa(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  function greeting() {
    const h = new Date().getHours();
    if (h < 5) return '夜深了';
    if (h < 11) return '早上好';
    if (h < 13) return '中午好';
    if (h < 18) return '下午好';
    if (h < 23) return '晚上好';
    return '夜深了';
  }

  function relTime(sec) {
    const t = Number(sec) || 0;
    if (!t) return '';
    const diff = Date.now() / 1000 - t;
    if (diff < 60) return '刚刚';
    if (diff < 3600) return Math.floor(diff / 60) + ' 分钟前';
    if (diff < 86400) return Math.floor(diff / 3600) + ' 小时前';
    if (diff < 172800) return '昨天';
    const d = new Date(t * 1000);
    return (d.getMonth() + 1) + '-' + d.getDate();
  }

  function renderMeta(root) {
    const box = q('#nh-meta', root);
    if (!box) return;
    box.textContent = '';
    const cfg = (typeof state !== 'undefined' && state.config) || {};
    const model = String(cfg.chat_model || cfg.model || '').trim();
    const count = ((state.sessions && state.sessions.sessions) || []).length;
    const items = [];
    if (model) items.push(model);
    items.push(count + ' 条会话');
    items.push(cfg.agent_os_sandbox === false ? '直接执行' : '沙箱开启');
    items.forEach(function (txt, i) {
      const s = document.createElement('span');
      s.className = 'nh-chip' + (i === 0 ? ' is-accent' : '');
      s.textContent = txt;
      box.appendChild(s);
    });
  }

  function spot(el) {
    el.addEventListener('pointermove', function (e) {
      const r = el.getBoundingClientRect();
      el.style.setProperty('--mx', (e.clientX - r.left) + 'px');
      el.style.setProperty('--my', (e.clientY - r.top) + 'px');
    });
  }

  function renderRecent(root) {
    const box = q('#nh-recent', root);
    if (!box) return;
    const blank = q('#nh-recent-blank', root);
    box.textContent = '';
    const sessions = (state.sessions && state.sessions.sessions) || [];
    const current = (state.sessions && state.sessions.current) || '';
    const list = sessions
      .filter(function (s) { return s && s.id && s.id !== current; })
      .sort(function (a, b) { return (Number(b.updated_at) || 0) - (Number(a.updated_at) || 0); })
      .slice(0, 4);
    if (!list.length) {
      if (blank) blank.hidden = false;
      return;
    }
    if (blank) blank.hidden = true;
    list.forEach(function (s) {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'nh-recent-item';

      const dot = document.createElement('span');
      dot.className = 'nh-recent-dot';

      const body = document.createElement('span');
      body.className = 'nh-recent-body';
      const title = document.createElement('span');
      title.className = 'nh-recent-title';
      title.textContent = s.title || '新对话';
      const sub = document.createElement('span');
      sub.className = 'nh-recent-sub';
      const bits = [];
      const rel = relTime(s.updated_at);
      if (rel) bits.push(rel);
      const n = Number(s.msg_total || (Array.isArray(s.messages) ? s.messages.length : 0)) || 0;
      if (n) bits.push(n + ' 条消息');
      if (s.sandbox_name) bits.push(String(s.sandbox_name));
      sub.textContent = bits.join(' · ');
      body.appendChild(title);
      body.appendChild(sub);

      const go = document.createElement('span');
      go.className = 'nh-recent-go';
      go.textContent = '→';
      go.setAttribute('aria-hidden', 'true');

      b.appendChild(dot);
      b.appendChild(body);
      b.appendChild(go);
      b.addEventListener('click', function () {
        if (typeof selectSession === 'function') selectSession(s.id);
      });
      spot(b);
      box.appendChild(b);
    });
  }

  function setIndicator(root, btn) {
    const wrap = q('.nh-switch', root);
    const ind = q('.nh-switch-ind', root);
    if (!wrap || !ind || !btn) return;
    const wr = wrap.getBoundingClientRect();
    const br = btn.getBoundingClientRect();
    ind.style.width = br.width + 'px';
    ind.style.height = br.height + 'px';
    ind.style.transform = 'translate(' + (br.left - wr.left) + 'px,' + (br.top - wr.top) + 'px)';
  }

  function activate(root, name) {
    const wrap = q('.nh-panels', root);
    const target = q('#nh-panel-' + name, root);
    if (!wrap || !target) return;
    const current = q('.nh-panel.is-on', root);
    if (current === target) return;

    // 先把容器高度锁在当前值，切完再过渡到新面板高度，避免整块跳变
    const h0 = wrap.getBoundingClientRect().height;
    wrap.style.height = h0 + 'px';
    qa('.nh-panel', root).forEach(function (p) {
      const on = p === target;
      p.classList.toggle('is-on', on);
      p.hidden = !on;
    });
    const h1 = target.getBoundingClientRect().height;
    void wrap.offsetHeight;
    wrap.style.height = h1 + 'px';
    clearTimeout(NH.hTimer);
    NH.hTimer = setTimeout(function () { wrap.style.height = ''; }, 380);

    target.classList.remove('nh-enter');
    void target.offsetWidth;
    target.classList.add('nh-enter');

    qa('.nh-switch-btn', root).forEach(function (t) {
      const on = t.dataset.tab === name;
      t.classList.toggle('is-on', on);
      t.setAttribute('aria-selected', on ? 'true' : 'false');
      t.tabIndex = on ? 0 : -1;
    });
    setIndicator(root, q('.nh-switch-btn.is-on', root));
  }

  function bind(root) {
    if (!root || root._nhBound) return;
    root._nhBound = true;
    NH.root = root;

    const greet = q('#nh-greet', root);
    if (greet) greet.textContent = greeting();
    // 窗口常开时跨过时段边界也能自动更新（每分钟一次，开销可忽略）
    if (!NH.greetTimer) {
      NH.greetTimer = setInterval(function () {
        const g = q('#nh-greet', NH.root);
        const t = greeting();
        if (g && g.textContent !== t) g.textContent = t;
      }, 60000);
    }
    renderMeta(root);
    renderRecent(root);

    // 分段切换
    const tabs = qa('.nh-switch-btn', root);
    tabs.forEach(function (btn, i) {
      btn.addEventListener('click', function () { activate(root, btn.dataset.tab); });
      btn.addEventListener('keydown', function (e) {
        if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
        e.preventDefault();
        const next = tabs[(i + (e.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length];
        if (!next) return;
        activate(root, next.dataset.tab);
        next.focus();
      });
    });

    // 灵感卡片：模板里带 {{占位}} 的只填入输入框并选中占位词，等用户补完再回车；
    // 只有 data-send="1" 的自足卡片才直接发送。
    // 以前一律「填入并立刻发送」，模型收到「解释一个概念」却不知道要讲什么。
    qa('.nh-card[data-prompt]', root).forEach(function (card) {
      spot(card);
      card.addEventListener('click', function () {
        const input = q('#input');
        if (!input) return;
        const tpl = card.dataset.prompt || '';
        const m = /\{\{(.+?)\}\}/.exec(tpl);

        // 输入框被收起时先展开，否则填了字也看不见
        const composer = document.querySelector('.composer');
        if (composer && composer.classList.contains('collapsed')) {
          const handle = q('#composer-handle');
          if (handle) handle.click();
        }

        if (m) {
          const head = tpl.slice(0, m.index);
          const word = m[1];
          input.value = head + word + tpl.slice(m.index + m[0].length);
          input.focus();
          try { input.setSelectionRange(head.length, head.length + word.length); } catch (e) { /* 不支持选中就不选 */ }
          if (typeof autoGrow === 'function') autoGrow();
          return;                       // 等用户把占位替换成真实主题
        }

        input.value = tpl;
        if (typeof autoGrow === 'function') autoGrow();
        if (card.dataset.send === '1' && typeof sendMessage === 'function') sendMessage();
      });
    });

    // 工作区卡片
    qa('.nh-space[data-mode]', root).forEach(function (card) {
      spot(card);
      card.addEventListener('click', function () {
        if (typeof setUiMode === 'function') setUiMode(card.dataset.mode);
      });
    });

    // 首次指示器位置（等布局稳定）
    requestAnimationFrame(function () {
      setIndicator(root, q('.nh-switch-btn.is-on', root));
    });
  }

  window.gmHomeBind = bind;

  window.addEventListener('resize', function () {
    const root = NH.root;
    if (!root || !root.isConnected) return;
    const wrap = q('.nh-panels', root);
    if (wrap) wrap.style.height = '';
    setIndicator(root, q('.nh-switch-btn.is-on', root));
  });
})();
