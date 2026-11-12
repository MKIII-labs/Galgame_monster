/* 分享面板：本地应用不做「托管链接」，而是产出自包含文件。
   流程：选范围/内容 → 内联图片 → 后端脱敏+预览 → 导出 HTML / 包 / 长图。 */
(function () {
  let _panel = null;
  let _state = { scope: 'one', index: null, reasoning: false, tools: false, redact: true, format: 'html' };
  let _preview = null;
  let _payload = null;

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
    const imgs = Array.from(root.querySelectorAll('img'));
    for (const img of imgs) {
      const src = img.getAttribute('src') || '';
      if (src.startsWith('data:')) continue;
      let data = '';
      if (src.startsWith('blob:')) data = await fetchAsDataUrl(src);
      else if (src.startsWith('/') || src.startsWith(location.origin)) data = await fetchAsDataUrl(src);
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
      if (!n.closest('.tool-card')) n.remove();
    });
    root.querySelectorAll('a[href^="javascript:"]').forEach((a) => a.removeAttribute('href'));
    // 图卡（iframe）不内联：改成一句可读说明
    root.querySelectorAll('.diagram-card').forEach((n) => {
      const t = el('p', 'share-note', '（此处原有一张可交互图卡，静态分享中未包含）');
      n.replaceWith(t);
    });
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
      if (!opts.reasoning) clone.querySelectorAll('.think').forEach((n) => n.remove());
      if (!opts.tools) clone.querySelectorAll('.process-fold,.tool-group,.tool-card').forEach((n) => n.remove());
      holder.appendChild(clone);
    } else {
      const box = row.querySelector('.content');
      holder.appendChild(box ? box.cloneNode(true) : el('div', 'content'));
    }
    stripChrome(holder);
    return { title: (msgs[index] && msgs[index].title) || '', question: question, html: holder.innerHTML };
  }

  function collectSession(opts) {
    const chat = document.querySelector('#chat');
    if (!chat) return null;
    const holder = document.createElement('div');
    const rows = Array.from(chat.querySelectorAll('.msg'));
    rows.forEach((row) => {
      const isUser = row.classList.contains('user');
      const box = isUser ? row.querySelector('.content') : row.querySelector('.content');
      if (!box) return;
      const sec = document.createElement('section');
      sec.className = 'share-msg ' + (isUser ? 'share-u' : 'share-a');
      const label = el('div', 'share-role', isUser ? '我' : 'AI');
      sec.appendChild(label);
      const body = box.cloneNode(true);
      sec.appendChild(body);
      if (!isUser && (opts.reasoning || opts.tools)) {
        const wrap = row.querySelector('.content-wrap');
        if (wrap) {
          const c = wrap.cloneNode(true);
          c.querySelectorAll('.content').forEach((n) => n.remove());
          if (!opts.reasoning) c.querySelectorAll('.think').forEach((n) => n.remove());
          if (!opts.tools) c.querySelectorAll('.process-fold,.tool-group,.tool-card').forEach((n) => n.remove());
          sec.appendChild(c);
        }
      }
      holder.appendChild(sec);
    });
    stripChrome(holder);
    return { title: '', question: '', html: holder.innerHTML };
  }

  async function buildPayload() {
    const sid = (typeof state !== 'undefined' && state.sessions) ? state.sessions.current : '';
    const opts = { reasoning: _state.reasoning, tools: _state.tools };
    const got = (_state.scope === 'session') ? collectSession(opts) : collectOne(_state.index, opts);
    if (!got) throw new Error('找不到可分享的内容');
    const holder = document.createElement('div');
    holder.innerHTML = got.html;
    await inlineImages(holder, sid);
    return {
      title: got.title || '分享',
      question: got.question || '',
      answer_html: holder.innerHTML,
      options: { reasoning: _state.reasoning, tools: _state.tools, scope: _state.scope },
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
    setStatus('正在生成预览…');
    try {
      _payload = await buildPayload();
      const r = await api('/api/share/preview', Object.assign({}, _payload, { redact: _state.redact }));
      if (!r || !r.ok) throw new Error((r && r.error) || '预览失败');
      _preview = r;
      const frame = _panel.querySelector('.share-preview');
      if (frame) frame.srcdoc = r.html || '';
      renderReport(r.report || {});
      const size = Math.round((r.bytes || 0) / 1024);
      setStatus('预览就绪 · 约 ' + size + ' KB' + (r.sections ? ' · ' + r.sections + ' 节目录' : ''));
    } catch (e) {
      setStatus('预览失败：' + (e && e.message ? e.message : e));
    }
  }

  async function doExport() {
    if (!_payload) { await doPreview(); }
    if (!_payload) return;
    setStatus('正在生成文件…');
    const btn = _panel.querySelector('.share-go');
    if (btn) { btn.disabled = true; btn.classList.add('busy'); }
    try {
      const r = await api('/api/share/export', Object.assign({}, _payload,
        { format: _state.format, redact: _state.redact }));
      if (!r || !r.ok) throw new Error((r && r.error) || '导出失败');
      showResult(r);
      setStatus('');
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
    box.appendChild(acts);
    box.appendChild(el('div', 'share-result-path', r.path || ''));
  }

  function close() {
    if (!_panel) return;
    _panel.remove();
    _panel = null;
    _payload = null;
    _preview = null;
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
    const mask = el('div', 'modal');
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
    return mask;
  }

  async function openSharePanel(index) {
    close();
    _state.index = (typeof index === 'number') ? index : null;
    if (_state.index == null) _state.scope = 'session';
    _panel = buildPanel();
    await doPreview();
  }

  window.openSharePanel = openSharePanel;
})();
