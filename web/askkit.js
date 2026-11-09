/* ============================================================
   划词问答内核（AskKit）
   ------------------------------------------------------------
   对话旁注 / 笔记 / PDF 三处原来各写一套问答卡片：三种 DOM、三条请求链路、
   三份定位与关闭逻辑，能力也不一致（只有对话流式、只有对话有多轮）。

   这里统一成一张卡片，适配器只声明：
     scope/source/selection  —— 线程分组与上下文（后端据此拼 prompt）
     anchor                  —— 同一段选区复用同一个线程
     persist                 —— 「存为批注」落到 annotations 表
     extras                  —— PDF 的翻译 / 高亮 / 批注等扩展按钮
     onQuote                 —— 「引用到对话」

   内核负责：面板渲染、定位 / 拖动、线程（多轮 + 服务端持久化）、SSE 流式、
   停止、错误态、markdown、复制 / 引用 / 存批注、Esc / 外部点击关闭。
   ============================================================ */
'use strict';

(function () {
  var MAX_HISTORY = 12;      // 与服务端 aside_store.MAX_HISTORY 对齐
  var WIDTH = 400;

  var S = {
    panel: null,
    opts: null,
    scope: '',
    source: 'chat',
    threads: [],
    threadId: '',
    history: [],             // [{ q, a }]
    busy: false,
    aborter: null,
    openSeq: 0,
    pending: null,
    drag: null,
    loadedFor: '',
    ro: null,
  };

  function $(sel, root) { return (root || S.panel || document).querySelector(sel); }

  function esc(text) {
    if (typeof window.escapeHtml === 'function') return window.escapeHtml(text);
    return String(text == null ? '' : text)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function md(text) {
    return (typeof window.safeRenderMarkdown === 'function')
      ? window.safeRenderMarkdown(String(text || ''))
      : esc(text).replace(/\n/g, '<br>');
  }

  function toast(msg, type) {
    if (typeof window.toast === 'function') window.toast(msg, type);
  }

  function copyText(text) {
    var t = String(text || '');
    if (!t) return;
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(t).then(function () { toast('已复制', 'ok'); },
        function () { window.prompt('复制这段内容：', t); });
      return;
    }
    window.prompt('复制这段内容：', t);
  }

  function authToken() {
    // core.js 里是顶层 const TOKEN（不是 window 属性）；跨脚本能按名字读到。
    try { if (typeof TOKEN !== 'undefined' && TOKEN) return TOKEN; } catch (e) { /* 忽略 */ }
    return window.__TOKEN__ || '';
  }

  function apiJson(url, body) {
    if (typeof window.api === 'function') return window.api(url, body);
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Auth-Token': authToken() },
      body: JSON.stringify(body || {}),
    }).then(function (r) { return r.json(); });
  }

  /* ---------------- 面板 ---------------- */

  function buildPanel() {
    var panel = document.createElement('div');
    panel.className = 'askkit hidden';
    panel.setAttribute('role', 'dialog');
    panel.innerHTML =
      '<div class="askkit-head">' +
        '<span class="askkit-title">划词问答</span>' +
        '<div class="askkit-tabs" role="tablist"></div>' +
        '<button type="button" class="askkit-new" title="在当前选区上新建一个问答">＋</button>' +
        '<button type="button" class="askkit-x" title="关闭（Esc）">✕</button>' +
      '</div>' +
      '<div class="askkit-quote" title="点击展开 / 收起"></div>' +
      '<div class="askkit-tools"></div>' +
      '<div class="askkit-toolout hidden"></div>' +
      '<div class="askkit-thread"></div>' +
      '<div class="askkit-chips"></div>' +
      '<div class="askkit-input-row">' +
        '<textarea class="askkit-input" rows="2" placeholder="就这段内容提问…（Enter 发送，Shift+Enter 换行）"></textarea>' +
        '<button type="button" class="askkit-send">发送</button>' +
      '</div>' +
      '<div class="askkit-foot">' +
        '<button type="button" class="askkit-link" data-act="copy">复制回答</button>' +
        '<button type="button" class="askkit-link" data-act="quote">引用到对话</button>' +
        '<button type="button" class="askkit-link" data-act="save" hidden>存为批注</button>' +
        '<span class="askkit-hint">Enter 发送 · Shift+Enter 换行</span>' +
      '</div>';
    panel.addEventListener('click', onPanelClick);
    panel.addEventListener('click', onToolClick);
    panel.addEventListener('dblclick', function (e) {
      var tab = e.target.closest('.askkit-tab');
      if (tab) renameThread(tab.dataset.tid);
    });
    panel.addEventListener('mousedown', function (e) { e.stopPropagation(); });
    panel.querySelector('.askkit-send').addEventListener('click', function (e) {
      e.preventDefault();
      e.stopPropagation();
      askInput();
    });
    $('.askkit-input', panel).addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        askInput();
      } else if (e.key === 'Escape') {
        e.preventDefault();
        close();
      }
    });
    bindDrag(panel);
    document.body.appendChild(panel);
    return panel;
  }

  function ensurePanel() {
    if (!S.panel) {
      S.panel = buildPanel();
      S.panel.addEventListener('pointerdown', function (e) { e.stopPropagation(); });
      if (typeof window.ResizeObserver === 'function') {
        S.ro = new window.ResizeObserver(function () {
          if (S.drag) return;      // 用户正在拖动时不要抢位置
          clampIntoView();
        });
        S.ro.observe(S.panel);
      }
    }
    return S.panel;
  }

  function onPanelClick(e) {
    var closeX = e.target.closest('.askkit-tab-x');
    if (closeX) { e.stopPropagation(); return closeTab(closeX.dataset.close); }
    var tab = e.target.closest('.askkit-tab');
    if (tab) return switchThread(tab.dataset.tid);
    if (e.target.closest('.askkit-new')) return newThread();
    if (e.target.closest('.askkit-x')) return close();
    var act = e.target.closest('[data-act]');
    if (act) {
      var id = act.dataset.act;
      if (id === 'copy') return copyAnswer();
      if (id === 'quote') return quoteToChat();
      if (id === 'save') return saveAsAnno();
    }
    var chip = e.target.closest('.askkit-chip');
    if (chip && !S.busy) {
      var input = $('.askkit-input');
      if (input) input.value = chip.dataset.q || '';
      askInput();
      return;
    }
    var quote = e.target.closest('.askkit-quote');
    if (quote) quote.classList.toggle('open');
  }

  function place() {
    var panel = S.panel;
    if (!panel || panel.classList.contains('hidden') || !S.opts) return;
    var rect = S.opts.rect || { left: 80, top: 120, bottom: 140 };
    var w = panel.offsetWidth || WIDTH;
    var h = panel.offsetHeight || 320;
    var left = Math.max(12, Math.min(window.innerWidth - w - 12, rect.left));
    var top = (rect.bottom || 140) + 10;
    if (top + h > window.innerHeight - 12) top = Math.max(12, (rect.top || 120) - h - 10);
    if (top + h > window.innerHeight - 12) top = Math.max(12, window.innerHeight - h - 12);
    panel.style.left = left + 'px';
    panel.style.top = top + 'px';
  }

  /* 面板高度不是常量：回答在流式增长、切换线程、补历史、扩展工具输出都会变高。
     只在「打开」时定位一次的话，一旦长过窗口下沿，面板最底部的输入框就会落到
     窗口之外——看得见卡片却点不进去、也打不出字。
     所以尺寸一变就把面板夹回视口内：只夹边界、不重新锚定到选区，避免回答
     增长时面板来回跳。 */
  function clampIntoView() {
    var panel = S.panel;
    if (!panel || panel.classList.contains('hidden')) return;
    var w = panel.offsetWidth || WIDTH;
    var h = panel.offsetHeight || 320;
    var r = panel.getBoundingClientRect();
    var left = Math.max(12, Math.min(window.innerWidth - w - 12, r.left));
    var top = Math.max(12, Math.min(window.innerHeight - h - 12, r.top));
    if (Math.abs(left - r.left) > 0.5) panel.style.left = left + 'px';
    if (Math.abs(top - r.top) > 0.5) panel.style.top = top + 'px';
  }

  function bindDrag(panel) {
    var head = $('.askkit-head', panel);
    head.addEventListener('pointerdown', function (e) {
      if (e.target.closest('button')) return;
      var r = panel.getBoundingClientRect();
      S.drag = { x: e.clientX - r.left, y: e.clientY - r.top };
      head.setPointerCapture(e.pointerId);
    });
    head.addEventListener('pointermove', function (e) {
      if (!S.drag) return;
      var left = Math.max(6, Math.min(window.innerWidth - panel.offsetWidth - 6, e.clientX - S.drag.x));
      var top = Math.max(6, Math.min(window.innerHeight - panel.offsetHeight - 6, e.clientY - S.drag.y));
      panel.style.left = left + 'px';
      panel.style.top = top + 'px';
    });
    var end = function () { S.drag = null; };
    head.addEventListener('pointerup', end);
    head.addEventListener('pointercancel', end);
  }

  function renderHeader() {
    var opts = S.opts || {};
    $('.askkit-title').textContent = opts.title || '划词问答';
    var quote = $('.askkit-quote');
    quote.textContent = opts.quote || '';
    quote.setAttribute('data-ref', opts.label || '');
    quote.classList.remove('open');
    renderTabs();
    renderTools();
    renderChips();
    var save = $('[data-act="save"]');
    if (save) save.hidden = !opts.persist;
    var quoteBtn = $('[data-act="quote"]');
    if (quoteBtn) quoteBtn.hidden = !opts.onQuote;
  }

  function renderTabs() {
    var box = $('.askkit-tabs');
    if (!box) return;
    box.innerHTML = '';
    S.threads.forEach(function (t) {
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'askkit-tab' + (t.id === S.threadId ? ' active' : '');
      b.dataset.tid = t.id;
      b.title = '双击改名';
      var label = document.createElement('span');
      label.className = 'askkit-tab-t';
      label.textContent = t.title || '问答';
      b.appendChild(label);
      var x = document.createElement('span');
      x.className = 'askkit-tab-x';
      x.dataset.close = t.id;
      x.textContent = '✕';
      x.title = '删除这个问答';
      b.appendChild(x);
      box.appendChild(b);
    });
  }

  function renderTools() {
    var box = $('.askkit-tools');
    var extras = (S.opts && S.opts.extras) || [];
    box.innerHTML = '';
    if (!extras.length) { box.classList.add('hidden'); return; }
    box.classList.remove('hidden');
    extras.forEach(function (ex) {
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'askkit-tool';
      b.dataset.extra = ex.id;
      b.textContent = ex.label || ex.id;
      box.appendChild(b);
    });
  }

  function renderChips() {
    var box = $('.askkit-chips');
    var quick = (S.opts && S.opts.quick) || [];
    box.innerHTML = '';
    if (!quick.length || S.history.length) { box.classList.add('hidden'); return; }
    box.classList.remove('hidden');
    quick.forEach(function (item) {
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'askkit-chip';
      b.dataset.q = item[1];
      b.textContent = item[0];
      box.appendChild(b);
    });
  }

  function renderHistory() {
    var box = $('.askkit-thread');
    box.innerHTML = '';
    S.history.forEach(function (turn) {
      appendTurn(turn.q, turn.a, false);
    });
    box.scrollTop = box.scrollHeight;
    renderChips();
  }

  function appendTurn(q, a, streaming) {
    var box = $('.askkit-thread');
    var wrap = document.createElement('div');
    wrap.className = 'askkit-turn';
    var qEl = document.createElement('div');
    qEl.className = 'askkit-q';
    qEl.textContent = q;
    var aEl = document.createElement('div');
    aEl.className = 'askkit-a';
    if (streaming) aEl.textContent = a || '';
    else aEl.innerHTML = md(a || '');
    wrap.appendChild(qEl);
    wrap.appendChild(aEl);
    box.appendChild(wrap);
    box.scrollTop = box.scrollHeight;
    return aEl;
  }

  function showTool(html) {
    var out = $('.askkit-toolout');
    if (!out) return;
    out.classList.remove('hidden');
    if (typeof html === 'string') out.innerHTML = html;
    else { out.innerHTML = ''; out.appendChild(html); }
    out.scrollTop = 0;
  }

  function hideTool() {
    var out = $('.askkit-toolout');
    if (!out) return;
    out.classList.add('hidden');
    out.innerHTML = '';
  }

  function setBusy(on) {
    S.busy = !!on;
    var send = $('.askkit-send');
    var input = $('.askkit-input');
    if (send) { send.textContent = on ? '停止' : '发送'; send.classList.toggle('busy', on); }
    if (input) input.disabled = on;
    var chips = S.panel ? S.panel.querySelectorAll('.askkit-chip') : [];
    Array.prototype.forEach.call(chips, function (b) { b.disabled = on; });
  }

  /* ---------------- 线程 ---------------- */

  function threadsOp(body) {
    return apiJson('/api/ask/threads', body).catch(function () { return null; });
  }

  function threadPayload(op) {
    var opts = S.opts || {};
    return {
      op: op,
      scope: S.scope,
      source: S.source,
      thread_id: S.threadId,
      selection: opts.selection || {},
      include_message: !!opts.includeMessage,
      message_text: opts.messageText || '',
      anchor: opts.anchor || '',
      doc_key: opts.docKey || '',
      title: String(opts.quote || '').replace(/\s+/g, ' ').slice(0, 40),
    };
  }

  function sameAnchor(t) {
    var opts = S.opts || {};
    return String(t.anchor || '') === String(opts.anchor || '')
      && String(t.source || '') === String(S.source || '')
      && String((t.selection || {}).text || '') === String((opts.selection || {}).text || '');
  }

  function ensureThreads() {
    var key = S.scope + '|' + S.source;
    if (S.loadedFor === key) return Promise.resolve();
    S.loadedFor = key;
    return threadsOp({ op: 'list', scope: S.scope, source: S.source }).then(function (j) {
      S.threads = (j && j.threads) || [];
      renderTabs();
    });
  }

  function ensureThread() {
    if (S.pending) return S.pending;
    var seq = S.openSeq;
    S.pending = ensureThreads().then(function () {
      var hit = S.threads.find(sameAnchor);
      if (hit) return hit;
      return threadsOp(threadPayload('create')).then(function (j) {
        if (!j || !j.thread) return null;
        S.threads.push(j.thread);
        return j.thread;
      });
    }).then(function (t) {
      if (t && seq === S.openSeq && !S.threadId) {
        S.threadId = t.id;
        renderTabs();
      }
      return t;
    }).finally(function () { S.pending = null; });
    return S.pending;
  }

  function applyThread(t) {
    if (!t) return;
    S.threadId = t.id;
    S.history = (t.history || []).map(function (h) { return { q: h.q, a: h.a }; }).slice(-MAX_HISTORY);
    renderTabs();
    renderHistory();
    var save = $('[data-act="save"]');
    if (save) save.disabled = !lastAnswer();
  }

  function switchThread(tid) {
    if (!tid || tid === S.threadId || S.busy) return;
    var t = S.threads.find(function (x) { return x.id === tid; });
    if (t) applyThread(t);
  }

  function newThread() {
    if (S.busy) return toast('等这条回答结束再新建');
    threadsOp(threadPayload('create')).then(function (j) {
      if (!j || !j.thread) return toast('新建失败', 'error');
      S.threads.push(j.thread);
      applyThread(j.thread);
      var input = $('.askkit-input');
      if (input) { input.value = ''; input.focus(); }
    });
  }

  function renameThread(tid) {
    var t = S.threads.find(function (x) { return x.id === tid; });
    if (!t) return;
    var name = window.prompt('问答名称（留空保留原名）', t.title || '');
    if (name == null) return;
    threadsOp({ op: 'rename', scope: S.scope, thread_id: tid, title: name }).then(function (j) {
      if (!j || !j.thread) return toast('改名失败', 'error');
      t.title = j.thread.title;
      renderTabs();
    });
  }

  function closeTab(tid) {
    if (S.busy && tid === S.threadId) return toast('等这条回答结束再关闭');
    threadsOp({ op: 'delete', scope: S.scope, thread_id: tid }).then(function (j) {
      if (!j || !j.ok) return toast('删除失败', 'error');
      S.threads = S.threads.filter(function (x) { return x.id !== tid; });
      if (S.threadId !== tid) return renderTabs();
      S.threadId = '';
      S.history = [];
      var next = S.threads[S.threads.length - 1];
      if (next) return applyThread(next);
      close();
    });
  }

  /* ---------------- 提问 ---------------- */

  function askInput() {
    if (S.busy) return stop();
    var input = $('.askkit-input');
    var q = String((input && input.value) || '').trim();
    if (!q) { if (input) input.focus(); return; }
    if (input) input.value = '';
    ask(q);
  }

  function ask(question) {
    if (!S.opts || S.busy) return;
    var opts = S.opts;
    var seq = S.openSeq;
    var aEl = appendTurn(question, '…', true);
    setBusy(true);
    renderChips();
    var answer = '';
    var ctrl = new AbortController();
    S.aborter = ctrl;
    var payload = {
      scope: S.scope,
      source: S.source,
      thread_id: S.threadId,
      session_id: (typeof state !== 'undefined' && state.sessions && state.sessions.current) || '',
      selection: opts.selection || {},
      include_message: !!opts.includeMessage,
      message_text: opts.messageText || '',
      question: question,
      history: S.history.slice(-MAX_HISTORY),
    };
    var failed = '';
    fetch('/api/ask/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Auth-Token': authToken() },
      body: JSON.stringify(payload),
      signal: ctrl.signal,
    }).then(function (res) {
      if (!res.ok) {
        return res.json().catch(function () { return {}; }).then(function (j) {
          throw new Error((j && j.error) || ('HTTP ' + res.status));
        });
      }
      var reader = res.body.getReader();
      var dec = new TextDecoder();
      var buf = '';
      function pump() {
        return reader.read().then(function (r) {
          if (r.done) return;
          buf += dec.decode(r.value, { stream: true });
          var pos;
          while ((pos = buf.indexOf('\n\n')) >= 0) {
            var chunk = buf.slice(0, pos);
            buf = buf.slice(pos + 2);
            var line = chunk.replace(/^data:\s*/, '').trim();
            if (!line) continue;
            var obj = null;
            try { obj = JSON.parse(line); } catch (e) { continue; }
            if (obj.type === 'delta') {
              answer += obj.text || '';
              aEl.textContent = answer;
              aEl.parentElement.parentElement.scrollTop = aEl.parentElement.parentElement.scrollHeight;
            } else if (obj.type === 'error') {
              failed = obj.message || '问答失败';
            } else if (obj.type === 'reasoning' && !answer) {
              // 思考阶段的正文还没到：给一个比「…」更明确的占位
              aEl.textContent = '思考中…';
            }
          }
          return pump();
        });
      }
      return pump();
    }).then(function () {
      if (seq !== S.openSeq) return;
      if (failed) throw new Error(failed);
      if (answer) {
        aEl.innerHTML = md(answer);
        S.history.push({ q: question, a: answer });
        if (S.history.length > MAX_HISTORY) S.history.shift();
        renderChips();
      } else {
        aEl.textContent = '（没有返回内容）';
      }
    }).catch(function (e) {
      if (seq !== S.openSeq) return;
      if (e && e.name === 'AbortError') {
        if (answer) {
          aEl.innerHTML = md(answer) + '<div class="askkit-stopped">（已停止）</div>';
          S.history.push({ q: question, a: answer });
        } else {
          aEl.textContent = '（已停止）';
        }
        return;
      }
      aEl.textContent = '问答失败：' + ((e && e.message) || e);
      aEl.classList.add('err');
    }).finally(function () {
      if (seq !== S.openSeq) return;
      S.aborter = null;
      setBusy(false);
      var input = $('.askkit-input');
      if (input && !S.panel.classList.contains('hidden')) input.focus();
      var save = $('[data-act="save"]');
      if (save) save.disabled = !lastAnswer();
    });
  }

  function stop() {
    if (S.aborter) {
      try { S.aborter.abort(); } catch (e) { /* 已结束 */ }
    }
  }

  function lastAnswer() {
    var last = S.history[S.history.length - 1];
    return (last && last.a) || '';
  }

  function copyAnswer() {
    var text = lastAnswer();
    if (!text) return toast('还没有可复制的回答');
    copyText(text);
  }

  function quoteToChat() {
    var last = S.history[S.history.length - 1];
    if (!last || !last.a) return toast('先提问再引用');
    var text = '问：' + (last.q || '') + '\n\n答：' + last.a;
    if (typeof S.opts.onQuote === 'function') S.opts.onQuote(text, last);
    else if (typeof window.addQuote === 'function') window.addQuote(text, S.opts.label || '');
    else return toast('当前界面不支持引用', 'error');
    toast('已引用到对话');
  }

  function saveAsAnno() {
    var last = S.history[S.history.length - 1];
    if (!last || !last.a) return toast('先提问再保存');
    if (!S.opts || typeof S.opts.persist !== 'function') return;
    var btn = $('[data-act="save"]');
    if (btn) { btn.disabled = true; btn.textContent = '保存中…'; }
    Promise.resolve(S.opts.persist({ question: last.q, answer: last.a }))
      .then(function (ok) {
        if (ok === false) return;
        toast('已存为批注', 'ok');
      })
      .catch(function (e) { toast('保存失败：' + ((e && e.message) || e), 'error'); })
      .finally(function () {
        if (btn) { btn.disabled = false; btn.textContent = '存为批注'; }
      });
  }

  /* ---------------- 扩展按钮（PDF 翻译 / 高亮 / 批注） ---------------- */

  function runExtra(id) {
    var extras = (S.opts && S.opts.extras) || [];
    var ex = extras.find(function (x) { return x.id === id; });
    if (!ex) return;
    var kit = {
      showTool: showTool,
      hideTool: hideTool,
      toast: toast,
      close: close,
      quote: (S.opts && S.opts.quote) || '',
      selection: (S.opts && S.opts.selection) || {},
      ask: function (q) { ask(q); },
    };
    try {
      Promise.resolve(ex.run(kit)).catch(function (e) {
        showTool('<div class="askkit-tool-err">' + esc((e && e.message) || e) + '</div>');
      });
    } catch (e) {
      showTool('<div class="askkit-tool-err">' + esc((e && e.message) || e) + '</div>');
    }
  }

  // 扩展按钮点击（事件委托，避免每次重渲染重复绑定）
  function onToolClick(e) {
    var btn = e.target.closest('[data-extra]');
    if (!btn) return;
    runExtra(btn.dataset.extra);
  }

  /* ---------------- 打开 / 关闭 ---------------- */

  function open(opts) {
    opts = opts || {};
    if (S.busy) stop();
    S.openSeq += 1;
    S.opts = opts;
    S.scope = String(opts.scope || ('session:' + ((typeof state !== 'undefined'
      && state.sessions && state.sessions.current) || '_')));
    S.source = String(opts.source || 'chat');
    S.threads = [];
    S.threadId = '';
    S.history = [];
    S.loadedFor = '';
    S.pending = null;
    var panel = ensurePanel();
    panel.classList.remove('hidden');
    panel.querySelector('.askkit-thread').innerHTML = '';
    hideTool();
    renderHeader();
    var input = $('.askkit-input');
    if (input) { input.value = ''; input.disabled = false; }
    setBusy(false);
    place();
    requestAnimationFrame(place);
    var save = $('[data-act="save"]');
    if (save) save.disabled = true;
    setTimeout(function () { if (input) input.focus(); }, 0);
    var seq = S.openSeq;
    ensureThread().then(function (t) {
      if (!t || seq !== S.openSeq) return;
      applyThread(t);
      place();
    });
    return {
      close: close,
      runExtra: runExtra,
      focus: function () { if (input) input.focus(); },
    };
  }

  function close() {
    stop();
    S.openSeq += 1;
    S.busy = false;
    S.opts = null;
    S.threads = [];
    S.threadId = '';
    S.history = [];
    S.loadedFor = '';
    if (S.panel) {
      S.panel.classList.add('hidden');
      hideTool();
    }
  }

  // 点击卡片外部关闭（回答进行中不关，避免误触丢结果）
  document.addEventListener('pointerdown', function (e) {
    if (!S.panel || S.panel.classList.contains('hidden')) return;
    if (S.panel.contains(e.target)) return;
    if (e.target.closest && (e.target.closest('.selbar') || e.target.closest('.selbar-more-menu'))) return;
    if (S.busy) return;
    close();
  }, true);

  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    if (!S.panel || S.panel.classList.contains('hidden')) return;
    e.preventDefault();
    close();
  }, true);

  window.addEventListener('resize', function () {
    if (S.panel && !S.panel.classList.contains('hidden')) place();
  });

  // 扩展按钮事件委托（面板只建一次）
  window.AskKit = {
    open: open,
    close: close,
    get current() { return S.opts; },
  };
})();
