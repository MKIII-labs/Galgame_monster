/* 分享面板：本地应用不做「托管链接」，而是产出自包含文件。
   流程：选范围/内容 → 内联图片 → 后端脱敏+预览 → 导出 HTML / 包 / 长图。 */
(function () {
  let _panel = null;
  let _state = { scope: 'one', index: null, reasoning: false, tools: false, redact: true, format: 'html' };
  let _preview = null;
  let _payload = null;
  let _previewSeq = 0;        // 连续切换选项时只认最后一次预览
  let _previewing = null;     // 正在跑的预览：导出前先等它结束，避免导到旧内容
  let _remoteImagesLeft = 0;  // 本轮预览里没能内联的外链图片数
  // 整段会话导出缓存：sid + 已拉到的最早位置，避免每切一次选项都重新拉全部历史
  let _histCache = { sid: '', total: 0, full: false, msgs: [] };

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function blobToDataUrl(blob) {
    return new Promise((resolve, reject) => {
      const fr = new FileReader();
      fr.onload = () => resolve(String(fr.result || ''));
      fr.onerror = () => reject(fr.error || new Error('读取图片失败'));
      fr.readAsDataURL(blob);
    });
  }

  async function fetchAsDataUrl(url) {
    try {
      const headers = (typeof TOKEN !== 'undefined' && TOKEN) ? { 'X-Auth-Token': TOKEN } : {};
      const res = await fetch(url, { headers });
      if (!res.ok) return '';
      const blob = await res.blob();
      if (blob.size > 3 * 1024 * 1024) return '';     // 单图 > 3MB 不内联
      return await blobToDataUrl(blob);
    } catch (e) { return ''; }
  }

  /* 把正文里的图片全部换成 data URL：否则产物在别人机器上是裂图 */
  async function inlineImages(root, sid) {
    _remoteImagesLeft = 0;
    const imgs = Array.from(root.querySelectorAll('img'));
    for (const img of imgs) {
      const src = img.getAttribute('src') || '';
      if (src.startsWith('data:')) continue;
      if (img.classList.contains('search-favicon-img')) continue;   // 小图标不值得逐张代理
      let data = '';
      if (src.startsWith('blob:')) data = await fetchAsDataUrl(src);
      else if (src.startsWith('/') || src.startsWith(location.origin)) data = await fetchAsDataUrl(src);
      else if (/^https?:/i.test(src)) {
        // 外链图片走本机代理再内联：分享页离线打开也不会裂图，且带 SSRF 校验
        data = await fetchAsDataUrl('/api/share/image?url=' + encodeURIComponent(src));
        if (!data) _remoteImagesLeft += 1;
      }
      if (data) img.setAttribute('src', data);
      else if (!/^https?:/i.test(src)) img.remove();
    }
    // 还没被 rich-media 换成 <img> 的沙箱占位：主动拉一次原文
    const poses = Array.from(root.querySelectorAll('.md-img-sandbox[data-src]'));
    for (const p of poses) {
      let data = '';
      try {
        const res = await fetch('/api/sandbox/raw', {
          method: 'POST',
          headers: Object.assign({ 'Content-Type': 'application/json' },
            (typeof TOKEN !== 'undefined' && TOKEN) ? { 'X-Auth-Token': TOKEN } : {}),
          body: JSON.stringify({ session_id: sid, path: p.getAttribute('data-src') }),
        });
        if (res.ok) {
          const blob = await res.blob();
          if (blob.size <= 3 * 1024 * 1024) data = await blobToDataUrl(blob);
        }
      } catch (e) { data = ''; }
      if (data) {
        const img = document.createElement('img');
        img.src = data;
        img.alt = p.getAttribute('data-alt') || '';
        p.replaceWith(img);
      } else {
        p.textContent = '（图片未包含）';
      }
    }
  }

  function stripChrome(root) {
    const kill = '.msg-actions,.usage-line,.live-bar,.end-note .end-act,.tool-undo,' +
      '.files-changed,.md-toc,.queue-ghost,.queue-ghosts,.typing,.live-text,.ctx-ring,#btn-bottom';
    root.querySelectorAll(kill).forEach((n) => n.remove());
    root.querySelectorAll('button').forEach((n) => {
      // 来源编号在静态分享里没有交互，但文字要保留
      if (n.classList.contains('cite-ref')) {
        n.replaceWith(el('span', 'cite-ref', n.textContent || ''));
        return;
      }
      if (!n.closest('.tool-card')) n.remove();
    });
    // 搜索 trace 的头行只是静态展示：去掉交互语义
    root.querySelectorAll('.search-trace .think-head').forEach((n) => {
      n.removeAttribute('tabindex');
      n.removeAttribute('role');
      n.removeAttribute('aria-expanded');
      n.removeAttribute('aria-haspopup');
      n.removeAttribute('aria-controls');
      n.style.cursor = 'default';
    });
    root.querySelectorAll('a[href^="javascript:"]').forEach((a) => a.removeAttribute('href'));
    // 图卡（iframe）不内联：改成一句可读说明
    root.querySelectorAll('.diagram-card').forEach((n) => {
      const t = el('p', 'share-note', '（此处原有一张可交互图卡，静态分享中未包含）');
      n.replaceWith(t);
    });
  }

  function cloneSearchTrace(row) {
    const trace = row ? row.querySelector('.search-trace') : null;
    return trace ? trace.cloneNode(true) : null;
  }

  /* 整段会话导出：把还没渲染到 DOM 的旧消息也按需拉下来。
     full=false 时请求精简字段（正文 + 来源），分享默认只要这些。
     返回 false 表示中途失败，导出当前已加载部分。 */
  async function loadAllMessages(s, msgs, total, full) {
    const reuse = _histCache.sid === s.id && _histCache.total <= total
      && (_histCache.full || !full);
    if (reuse) {
      for (let i = 0; i < total; i += 1) {
        if (!msgs[i] && _histCache.msgs[i]) msgs[i] = _histCache.msgs[i];
      }
    } else {
      _histCache = { sid: s.id, total: total, full: !!full, msgs: [] };
    }
    // 缓存可能已经把更早的页面补齐：从「当前最早的一条」继续往前拉，
    // 而不是固定从 msg_loaded_start 重拉一遍。
    let before = -1;
    for (let i = 0; i < total; i += 1) {
      if (msgs[i]) { before = i; break; }
    }
    if (before < 0) before = Number(s.msg_loaded_start) || 0;
    while (before > 0) {
      let r;
      try {
        r = await api('/api/session/messages', {
          session_id: s.id, before: before, limit: 200, slim: !full,
        });
      } catch (e) {
        return false;
      }
      if (!r || !r.ok || !Array.isArray(r.messages) || !r.messages.length) return false;
      const start = Number(r.from) || 0;
      r.messages.forEach((m, i) => {
        msgs[start + i] = m;
        _histCache.msgs[start + i] = m;
      });
      if (full) _histCache.full = true;
      _histCache.total = total;
      if (start >= before) return false;       // 后端没有继续往前，避免死循环
      before = start;
      const loaded = msgs.filter(Boolean).length;
      setStatus('正在读取完整会话… ' + loaded + '/' + total);
    }
    return true;
  }

  function appendAiExtras(sec, row, opts) {
    if (opts.reasoning || opts.tools) {
      const wrap = row.querySelector('.content-wrap');
      if (!wrap) return;
      const c = wrap.cloneNode(true);
      c.querySelectorAll('.content').forEach((n) => n.remove());
      if (!opts.reasoning) c.querySelectorAll('.think:not(.search-trace)').forEach((n) => n.remove());
      if (!opts.tools) c.querySelectorAll('.process-fold:not(.search-trace),.tool-group,.tool-card').forEach((n) => n.remove());
      sec.appendChild(c);
      return;
    }
    const trace = cloneSearchTrace(row);
    if (trace) sec.appendChild(trace);
  }

  function collectOne(index, opts) {
    const s = (typeof curSession === 'function') ? curSession() : null;
    const msgs = (s && s.messages) || [];
    const ai = msgs[index];
    const row = document.querySelector('.msg.ai[data-idx="' + index + '"]');
    if (!row) return null;
    let question = '';
    for (let i = index - 1; i >= 0; i -= 1) {
      if (msgs[i] && msgs[i].role === 'user') { question = String(msgs[i].content || ''); break; }
    }
    const holder = document.createElement('div');
    if (opts.reasoning || opts.tools) {
      const wrap = row.querySelector('.content-wrap');
      const clone = wrap ? wrap.cloneNode(true) : document.createElement('div');
      if (!opts.reasoning) clone.querySelectorAll('.think:not(.search-trace)').forEach((n) => n.remove());
      if (!opts.tools) clone.querySelectorAll('.process-fold:not(.search-trace),.tool-group,.tool-card').forEach((n) => n.remove());
      holder.appendChild(clone);
    } else {
      // 一条回答可能被过程组切成多个正文段：全部带上，不能只取第一段
      const boxes = row.querySelectorAll('.content-wrap > .content');
      if (boxes.length) {
        boxes.forEach((n) => holder.appendChild(n.cloneNode(true)));
      } else {
        const box = row.querySelector('.content');
        holder.appendChild(box ? box.cloneNode(true) : el('div', 'content'));
      }
      // 搜索来源属于正文的一部分：即使不导出思考 / 工具，也要带上，
      // 否则正文里的 [n] 会失去对应来源。
      const trace = cloneSearchTrace(row);
      if (trace) holder.insertBefore(trace, holder.firstChild);
    }
    stripChrome(holder);
    return { title: (msgs[index] && msgs[index].title) || '', question: question, html: holder.innerHTML };
  }

  async function collectSession(opts) {
    const s = (typeof curSession === 'function') ? curSession() : null;
    if (!s) return null;
    const total = Number(s.msg_total) || (s.messages || []).length;
    const msgs = (s.messages || []).slice();
    const full = !!(opts.reasoning || opts.tools);
    const complete = await loadAllMessages(s, msgs, total, full);
    const holder = document.createElement('div');
    for (let idx = 0; idx < total; idx += 1) {
      const m = msgs[idx];
      if (!m) continue;
      const isUser = m.role === 'user';
      const sec = document.createElement('section');
      sec.className = 'share-msg ' + (isUser ? 'share-u' : 'share-a');
      sec.appendChild(el('div', 'share-role', isUser ? '我' : 'AI'));

      const row = document.querySelector('.msg[data-idx="' + idx + '"]');
      if (row) {
        if (isUser) {
          // 用户气泡是 .user-wrap（含图片 / 文档附件），不是 .content
          const uw = row.querySelector('.user-wrap');
          if (uw) sec.appendChild(uw.cloneNode(true));
        } else {
          const boxes = row.querySelectorAll('.content-wrap > .content');
          if (boxes.length) {
            boxes.forEach((n) => sec.appendChild(n.cloneNode(true)));
          } else {
            const box = row.querySelector('.content');
            if (box) sec.appendChild(box.cloneNode(true));
          }
          appendAiExtras(sec, row, opts);
        }
      } else if (isUser) {
        const d = el('div', 'content bubble');
        d.textContent = String(m.content || '');
        sec.appendChild(d);
      } else {
        // 未渲染的旧回答：默认只导正文；勾了思考 / 工具时再完整重建一次。
        let built = null;
        if (opts.reasoning || opts.tools) {
          try { built = rowForMsgSafe(msgs, idx); } catch (e) { built = null; }
        }
        if (built) {
          const box = built.querySelector('.content');
          if (box) sec.appendChild(box.cloneNode(true));
          appendAiExtras(sec, built, opts);
        } else {
          const d = el('div', 'content');
          d.innerHTML = (typeof safeRenderMarkdown === 'function')
            ? safeRenderMarkdown(String(m.content || '')) : '';
          sec.appendChild(d);
          const trace = (typeof buildSearchTraceFromMessage === 'function')
            ? buildSearchTraceFromMessage(m) : null;
          if (trace) sec.appendChild(trace);
        }
      }
      holder.appendChild(sec);
    }
    stripChrome(holder);
    return { title: '', question: '', html: holder.innerHTML, partial: !complete };
  }

  async function buildPayload() {
    const sid = (typeof state !== 'undefined' && state.sessions) ? state.sessions.current : '';
    const opts = { reasoning: _state.reasoning, tools: _state.tools };
    const got = (_state.scope === 'session')
      ? await collectSession(opts) : collectOne(_state.index, opts);
    if (!got) throw new Error('找不到可分享的内容');
    const holder = document.createElement('div');
    holder.innerHTML = got.html;
    await inlineImages(holder, sid);
    return {
      title: got.title || '分享',
      question: got.question || '',
      answer_html: holder.innerHTML,
      options: { reasoning: _state.reasoning, tools: _state.tools, scope: _state.scope,
                 partial: !!got.partial, remote_images: _remoteImagesLeft },
    };
  }

  function setStatus(t) {
    const s = _panel && _panel.querySelector('.share-status');
    if (s) s.textContent = t || '';
  }

  function renderReport(rep) {
    const box = _panel && _panel.querySelector('.share-report');
    if (!box) return;
    const total = (rep.secrets || 0) + (rep.paths || 0);
    box.innerHTML = '';
    box.classList.toggle('hidden', !(total || rep.lan));
    if (!total && !rep.lan) return;
    const line = el('div', 'share-report-line');
    line.textContent = '已处理：密钥 ' + (rep.secrets || 0) + ' 处 · 路径 ' + (rep.paths || 0) + ' 处'
      + (rep.lan ? ' · 内网地址 ' + rep.lan + ' 处（仅提示）' : '');
    box.appendChild(line);
    (rep.samples || []).slice(0, 4).forEach((s) => {
      const row = el('div', 'share-report-sample');
      row.textContent = '[' + (s.kind === 'path' ? '路径' : '密钥') + '] ' + s.text;
      box.appendChild(row);
    });
  }

  async function doPreview() {
    const seq = ++_previewSeq;
    _payload = null;                 // 当前选项还没有可用产物，避免导出到旧内容
    setStatus('正在生成预览…');
    const run = (async () => {
      try {
        const payload = await buildPayload();
        if (seq !== _previewSeq) return;
        const r = await api('/api/share/preview',
          Object.assign({}, payload, { redact: _state.redact }));
        if (seq !== _previewSeq) return;
        if (!r || !r.ok) throw new Error((r && r.error) || '预览失败');
        _payload = payload;
        _preview = r;
        const frame = _panel.querySelector('.share-preview');
        if (frame) frame.srcdoc = r.html || '';
        renderReport(r.report || {});
        const size = Math.round((r.bytes || 0) / 1024);
        const partial = !!(payload.options && payload.options.partial);
        const remote = Number(payload.options && payload.options.remote_images) || 0;
        setStatus('预览就绪 · 约 ' + size + ' KB' + (r.sections ? ' · ' + r.sections + ' 节目录' : '')
          + (partial ? ' · 部分历史未加载' : '')
          + (remote ? ' · ' + remote + ' 张外链图片未内联' : ''));
      } catch (e) {
        if (seq === _previewSeq) setStatus('预览失败：' + (e && e.message ? e.message : e));
      }
    })();
    _previewing = run;
    try { await run; } finally { if (_previewing === run) _previewing = null; }
  }

  async function doExport() {
    if (_previewing) { try { await _previewing; } catch (e) { /* 预览失败由状态栏提示 */ } }
    if (!_payload) { await doPreview(); }
    if (!_payload) return;
    setStatus('请选择保存位置…');
    const btn = _panel.querySelector('.share-go');
    if (btn) { btn.disabled = true; btn.classList.add('busy'); }
    try {
      const r = await api('/api/share/export', Object.assign({}, _payload,
        { format: _state.format, redact: _state.redact, save_as: true }));
      if (r && r.canceled) { setStatus('已取消保存'); return; }
      if (!r || !r.ok) throw new Error((r && r.error) || '导出失败');
      showResult(r);
      setStatus(r.save_error || '');
    } catch (e) {
      setStatus('导出失败：' + (e && e.message ? e.message : e));
    } finally {
      if (btn) { btn.disabled = false; btn.classList.remove('busy'); }
    }
  }

  function fmtSize(n) {
    n = Number(n) || 0;
    if (n < 1024) return n + ' B';
    if (n < 1048576) return Math.round(n / 1024) + ' KB';
    return (n / 1048576).toFixed(1) + ' MB';
  }

  function showResult(r) {
    const box = _panel.querySelector('.share-result');
    if (!box) return;
    box.classList.remove('hidden');
    box.innerHTML = '';
    const title = el('div', 'share-result-title',
      ({ html: '单文件 HTML', png: '长图 PNG', package: '可再导入的包' }[_state.format] || '产物')
      + ' · ' + fmtSize(r.size));
    box.appendChild(title);
    if (_state.format === 'png') {
      const img = document.createElement('img');
      img.className = 'share-result-img';
      img.src = '/share/export/' + r.name;
      img.alt = '分享长图';
      box.appendChild(img);
    }
    const acts = el('div', 'share-result-acts');
    const open = el('a', 'share-btn', '打开');
    open.href = '/share/export/' + r.name;
    open.target = '_blank';
    open.rel = 'noreferrer';
    acts.appendChild(open);
    if (_state.format === 'package') {
      const dl = el('a', 'share-btn', '下载 .gmshare');
      dl.href = '/share/export/' + r.name;
      dl.download = r.name;
      acts.appendChild(dl);
    }
    const del = el('button', 'share-btn ghost', '删除');
    del.type = 'button';
    del.onclick = async () => {
      await api('/api/share/delete', { name: r.name });
      box.classList.add('hidden');
      toast('已删除分享文件');
    };
    acts.appendChild(del);
    // 用户「另存为」的位置优先；没有就在导出目录里定位
    const savedTo = r.saved_to || '';
    const reveal = el('button', 'share-btn ghost', '打开文件夹');
    reveal.type = 'button';
    reveal.onclick = async () => {
      try {
        const res = await api('/api/share/reveal',
          savedTo ? { path: savedTo } : { name: r.name });
        if (!res || !res.ok) throw new Error((res && res.error) || '打开失败');
      } catch (e) {
        toast(e && e.message ? e.message : '打开失败');
      }
    };
    acts.appendChild(reveal);
    box.appendChild(acts);
    box.appendChild(el('div', 'share-result-path',
      savedTo ? '已保存到：' + savedTo : (r.path || '')));
  }

  function close() {
    if (!_panel) return;
    const el = _panel;
    _panel = null;
    _payload = null;
    _preview = null;
    _previewSeq += 1;          // 让还在飞的预览结果作废
    _previewing = null;
    if (typeof hideWithAnim === 'function') hideWithAnim(el, () => el.remove());
    else el.remove();
  }

  function row(label, node) {
    const r = el('div', 'share-row');
    r.appendChild(el('div', 'share-label', label));
    const v = el('div', 'share-value');
    v.appendChild(node);
    r.appendChild(v);
    return r;
  }

  function checkbox(label, checked, onChange) {
    const wrap = el('label', 'share-check');
    const inp = document.createElement('input');
    inp.type = 'checkbox';
    inp.checked = !!checked;
    inp.onchange = () => onChange(inp.checked);
    wrap.appendChild(inp);
    wrap.appendChild(document.createTextNode(label));
    return wrap;
  }

  function radioGroup(name, items, value, onChange) {
    const box = el('div', 'share-radios');
    items.forEach((it) => {
      const lab = el('label', 'share-check');
      const inp = document.createElement('input');
      inp.type = 'radio';
      inp.name = name;
      inp.value = it.v;
      inp.checked = it.v === value;
      inp.onchange = () => { if (inp.checked) onChange(it.v); };
      lab.appendChild(inp);
      lab.appendChild(document.createTextNode(it.t));
      box.appendChild(lab);
    });
    return box;
  }

  function buildPanel() {
    const mask = el('div', 'modal hidden');
    mask.setAttribute('role', 'dialog');
    mask.setAttribute('aria-modal', 'true');
    mask.setAttribute('aria-label', '分享');
    mask.onclick = (e) => { if (e.target === mask) close(); };
    const card = el('div', 'modal-card share-card');
    card.onclick = (e) => e.stopPropagation();

    const head = el('div', 'share-head');
    const titleWrap = el('div', 'share-head-left');
    titleWrap.appendChild(el('div', 'share-title', '分享'));
    const imp = el('button', 'share-btn ghost', '导入 .gmshare');
    imp.type = 'button';
    const file = document.createElement('input');
    file.type = 'file';
    file.accept = '.gmshare';
    file.style.display = 'none';
    imp.onclick = () => file.click();
    file.onchange = async () => {
      const f = file.files && file.files[0];
      if (!f) return;
      setStatus('正在读取包…');
      try {
        const dataUrl = await blobToDataUrl(f);
        const b64 = String(dataUrl).split(',')[1] || '';
        const r = await api('/api/share/import', { data: b64 });
        if (!r || !r.ok) throw new Error((r && r.error) || '导入失败');
        const frame = _panel.querySelector('.share-preview');
        if (frame) frame.srcdoc = r.html || '';
        const meta = r.manifest || {};
        setStatus('已导入：' + (meta.title || '未命名') + ' · 仅供查看');
      } catch (e) {
        setStatus('导入失败：' + (e && e.message ? e.message : e));
      } finally {
        file.value = '';
      }
    };
    titleWrap.appendChild(imp);
    head.appendChild(titleWrap);
    head.appendChild(file);
    const x = el('button', 'share-close', '✕');
    x.type = 'button';
    x.onclick = close;
    head.appendChild(x);
    card.appendChild(head);

    const body = el('div', 'share-body');
    const left = el('div', 'share-left');
    const right = el('div', 'share-right');

    // 范围
    left.appendChild(row('范围', radioGroup('sh-scope',
      [{ v: 'one', t: '这条回答' }, { v: 'session', t: '整段会话' }],
      _state.scope, (v) => { _state.scope = v; doPreview(); })));

    // 内容
    const content = el('div', 'share-checks');
    content.appendChild(checkbox('包含思考过程', _state.reasoning, (v) => { _state.reasoning = v; doPreview(); }));
    content.appendChild(checkbox('包含工具调用与结果', _state.tools, (v) => { _state.tools = v; doPreview(); }));
    content.appendChild(checkbox('自动脱敏（密钥 / 本机路径）', _state.redact, (v) => { _state.redact = v; doPreview(); }));
    left.appendChild(row('内容', content));

    // 格式
    left.appendChild(row('产物', radioGroup('sh-fmt',
      [{ v: 'html', t: '单文件 HTML' }, { v: 'png', t: '长图 PNG' }, { v: 'package', t: '可再导入的包' }],
      _state.format, (v) => { _state.format = v; })));

    const report = el('div', 'share-report hidden');
    left.appendChild(report);

    const acts = el('div', 'share-acts');
    const go = el('button', 'share-btn primary share-go', '生成文件');
    go.type = 'button';
    go.onclick = doExport;
    acts.appendChild(go);
    const copy = el('button', 'share-btn ghost', '复制 Markdown');
    copy.type = 'button';
    copy.onclick = () => {
      const s = (typeof curSession === 'function') ? curSession() : null;
      const msgs = (s && s.messages) || [];
      const m = msgs[_state.index];
      const q = (() => {
        for (let i = _state.index - 1; i >= 0; i -= 1) {
          if (msgs[i] && msgs[i].role === 'user') return String(msgs[i].content || '');
        }
        return '';
      })();
      const text = (q ? '> ' + q.replace(/\n/g, '\n> ') + '\n\n' : '')
        + ((m && m.content) || '');
      if (navigator.clipboard) navigator.clipboard.writeText(text).then(() => toast('已复制 Markdown'));
    };
    acts.appendChild(copy);
    left.appendChild(acts);

    const status = el('div', 'share-status');
    left.appendChild(status);

    const result = el('div', 'share-result hidden');
    left.appendChild(result);

    // 预览
    const frame = document.createElement('iframe');
    frame.className = 'share-preview';
    frame.setAttribute('sandbox', '');          // 预览页无脚本，也不允许执行
    frame.setAttribute('title', '分享预览');
    right.appendChild(frame);

    body.appendChild(left);
    body.appendChild(right);
    card.appendChild(body);
    mask.appendChild(card);
    document.body.appendChild(mask);
    // 接入统一的弹窗焦点栈：Tab 不会跑到背后的聊天区，关闭后焦点回触发按钮
    if (typeof showWithAnim === 'function') showWithAnim(mask);
    else mask.classList.remove('hidden');
    return mask;
  }

  async function openSharePanel(index) {
    close();
    _state.index = (typeof index === 'number') ? index : null;
    if (_state.index == null) _state.scope = 'session';
    _panel = buildPanel();
    await doPreview();
  }

  // 分享是模态面板：Esc 优先关闭它，不再触发背后的笔记 / 搜索快捷键
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape' || !_panel) return;
    e.preventDefault();
    e.stopPropagation();
    close();
  }, true);

  window.openSharePanel = openSharePanel;
})();
