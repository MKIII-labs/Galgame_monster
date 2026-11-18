'use strict';

/* ============================================================
   笔记工作区（全屏）
   ------------------------------------------------------------
   左栏：树 / 置顶 / 最近 / 搜索       中栏：块编辑 · 分栏 · 预览 · 源码
   右栏：大纲 / 反向链接 / 批注 / 图谱
   存储仍是 data/notes 下的 Markdown；本文件只负责界面与交互。
   ============================================================ */
(function () {
  const WS = {
    tree: [], graph: { nodes: [], edges: [] }, current: '',
    view: 'block', tab: 'outline', blocks: [],
    annos: [], openDirs: new Set(), dirty: false,
    saveTimer: 0, previewTimer: 0, lintTimer: 0, searchTimer: 0,
    lastSavedAt: 0, graphNet: null, positions: {}, quickIndex: 0,
  };
  window.notesWS = WS;
  let editor = null;
  let noteAnnoPop = null;
  let noteAnnoTip = null;
  let noteAnnoDraft = null;
  let noteSelRegistered = false;
  let lintItems = [];
  let lintCollapsed = false;

  const PIN_KEY = 'gm.notes.pin';
  const RECENT_KEY = 'gm.notes.recent';
  const LINT_KEY = 'gm.notes_lint';

  const $0 = (s, r) => (r || document).querySelector(s);
  const $$0 = (s, r) => Array.prototype.slice.call((r || document).querySelectorAll(s));

  /* ---------------- 小工具 ---------------- */
  function lsGet(key, fallback) {
    try { const v = JSON.parse(localStorage.getItem(key) || 'null'); return v == null ? fallback : v; }
    catch (e) { return fallback; }
  }
  function lsSet(key, val) {
    try { localStorage.setItem(key, JSON.stringify(val)); } catch (e) { /* 隐私模式 */ }
  }
  function flatten(tree, out) {
    const acc = out || [];
    (tree || []).forEach((n) => {
      if (n.type === 'file') acc.push(n);
      else flatten(n.children, acc);
    });
    return acc;
  }
  function noteTitleOf(path) {
    const base = String(path || '').split('/').pop() || '';
    return base.replace(/\.md$/i, '') || '未命名笔记';
  }
  function notesNameIndex() {
    const idx = {};
    flatten(WS.tree).forEach((f) => {
      const stem = f.path.replace(/\.md$/i, '');
      idx[stem.toLowerCase()] = f.path;
      idx[stem.split('/').pop().toLowerCase()] = f.path;
    });
    return idx;
  }
  function resolveNotePath(name) {
    const idx = notesNameIndex();
    const key = String(name || '').trim().toLowerCase();
    if (!key) return null;
    return idx[key] || idx[key.split('/').pop()] || null;
  }
  function noteTitle() {
    return noteTitleOf(WS.current);
  }
  function pins() { return lsGet(PIN_KEY, []); }
  function recents() { return lsGet(RECENT_KEY, []); }
  function pushRecent(path) {
    if (!path) return;
    const list = recents().filter((x) => x !== path);
    list.unshift(path);
    lsSet(RECENT_KEY, list.slice(0, 8));
  }
  function setSaveState(kind) {
    const el = $0('#nt-save');
    if (!el) return;
    el.classList.toggle('saving', kind === 'saving');
    el.classList.toggle('error', kind === 'error');
    if (kind === 'saving') el.textContent = '保存中…';
    else if (kind === 'dirty') el.textContent = '未保存';
    else if (kind === 'error') el.textContent = '保存失败';
    else if (kind === 'saved') {
      const d = new Date((WS.lastSavedAt || Date.now() / 1000) * 1000);
      const hh = String(d.getHours()).padStart(2, '0');
      const mm = String(d.getMinutes()).padStart(2, '0');
      el.textContent = '已保存 ' + hh + ':' + mm;
    } else el.textContent = '—';
  }
  function currentMarkdown() {
    if (WS.view === 'source') {
      const ta = $0('#nt-textarea');
      return ta ? ta.value : '';
    }
    return editor ? editor.getMarkdown() : '';
  }
  function updateStats() {
    const md = currentMarkdown();
    const el = $0('#nt-stat-text');
    if (el) el.textContent = (md.replace(/\s/g, '').length) + ' 字 · ' +
      (md ? md.split('\n').length : 0) + ' 行';
  }

  /* ---------------- 树 / 搜索 / 最近 / 置顶 ---------------- */
  async function loadNotes() {
    const r = await api('/api/notes/tree', {});
    if (!r || !r.ok) { toast('笔记库读取失败'); return; }
    WS.tree = r.tree || [];
    WS.graph = r.graph || { nodes: [], edges: [] };
    renderTree();
  }

  function noteRow(f, extraClass) {
    const active = f.path === WS.current ? ' active' : '';
    const pin = pins().indexOf(f.path) >= 0;
    return '<div class="nt-row' + active + (extraClass ? ' ' + extraClass : '') +
      '" data-note="' + escapeHtml(f.path) + '" tabindex="0">' +
      '<span class="nt-ico">' + icon('note') + '</span>' +
      '<span class="nt-name">' + escapeHtml(f.name) + '</span>' +
      (pin ? '<span class="nt-pin" title="已置顶">★</span>' : '') +
      '</div>';
  }

  function renderTree() {
    const box = $0('#nt-tree');
    if (!box) return;
    const files = flatten(WS.tree);
    const byPath = {};
    files.forEach((f) => { byPath[f.path] = f; });
    const pinList = pins().filter((p) => byPath[p]).map((p) => byPath[p]);
    const recentList = recents().filter((p) => byPath[p] && pins().indexOf(p) < 0)
      .map((p) => byPath[p]).slice(0, 5);

    let html = '';
    if (pinList.length) {
      html += '<div class="nt-group">置顶</div>' + pinList.map((f) => noteRow(f)).join('');
    }
    if (recentList.length) {
      html += '<div class="nt-group">最近</div>' + recentList.map((f) => noteRow(f)).join('');
    }
    html += '<div class="nt-group">全部笔记</div>';

    const walk = (nodes, depth) => {
      (nodes || []).forEach((n) => {
        const pad = 8 + depth * 12;
        if (n.type === 'dir') {
          const open = WS.openDirs.has(n.path);
          const count = flatten(n.children || []).length;
          html += '<div class="nt-row nt-dir" data-dir="' + escapeHtml(n.path) +
            '" style="padding-left:' + pad + 'px" tabindex="0">' +
            '<span class="nt-caret">' + (open ? '▾' : '▸') + '</span>' +
            '<span class="nt-ico">' + icon('folder') + '</span>' +
            '<span class="nt-name">' + escapeHtml(n.name) + '</span>' +
            '<span class="nt-num">' + count + '</span>' +
            '<button type="button" class="nt-add" title="在此文件夹新建笔记">＋</button></div>';
          if (open) walk(n.children, depth + 1);
        } else {
          const active = n.path === WS.current ? ' active' : '';
          html += '<div class="nt-row' + active + '" data-note="' + escapeHtml(n.path) +
            '" style="padding-left:' + pad + 'px" tabindex="0">' +
            '<span class="nt-caret"></span>' +
            '<span class="nt-ico">' + icon('note') + '</span>' +
            '<span class="nt-name">' + escapeHtml(n.name) + '</span></div>';
        }
      });
    };
    walk(WS.tree, 0);
    box.innerHTML = html || '<div class="hint nt-blank">还没有笔记，点右上角「笔记」新建。</div>';

    $$0('[data-dir]', box).forEach((el) => {
      const toggle = () => {
        const p = el.dataset.dir;
        if (WS.openDirs.has(p)) WS.openDirs.delete(p); else WS.openDirs.add(p);
        renderTree();
      };
      el.onclick = (e) => {
        if (e.target.closest('.nt-add')) return;
        toggle();
      };
      el.oncontextmenu = (e) => { e.preventDefault(); showTreeMenu(e, { dir: el.dataset.dir }); };
      const add = el.querySelector('.nt-add');
      if (add) add.onclick = (e) => {
        e.stopPropagation();
        newNote('', el.dataset.dir);
      };
    });
    $$0('[data-note]', box).forEach((el) => {
      const open = () => openNote(el.dataset.note);
      el.onclick = open;
      el.oncontextmenu = (e) => { e.preventDefault(); showTreeMenu(e, { note: el.dataset.note }); };
      el.addEventListener('keydown', (e) => { if (e.key === 'Enter') open(); });
    });
  }

  /* 右键菜单：定位在鼠标附近 */
  function ensureCtx() {
    let box = $0('#nt-ctx');
    if (box) return box;
    box = document.createElement('div');
    box.id = 'nt-ctx';
    box.className = 'nt-ctx hidden';
    document.body.appendChild(box);
    return box;
  }
  function closeCtx() { const b = $0('#nt-ctx'); if (b) b.classList.add('hidden'); }

  function showTreeMenu(e, target) {
    const box = ensureCtx();
    const isDir = !!target.dir;
    const path = target.dir || target.note || '';
    const items = isDir
      ? [{ k: 'new-note', label: '新建笔记' }, { k: 'new-dir', label: '新建子文件夹' },
         { k: 'rename', label: '重命名' }, { k: 'delete-dir', label: '删除文件夹', danger: true }]
      : [{ k: 'pin', label: pins().indexOf(path) >= 0 ? '取消置顶' : '置顶' },
         { k: 'rename', label: '重命名' }, { k: 'move', label: '移动到…' },
         { k: 'copy-link', label: '复制双链' }, { k: 'delete', label: '删除', danger: true }];
    box.innerHTML = items.map((it) =>
      '<button type="button" class="nt-ctx-item' + (it.danger ? ' danger' : '') +
      '" data-k="' + it.k + '">' + escapeHtml(it.label) + '</button>').join('');
    box.style.left = Math.min(e.clientX, window.innerWidth - 190) + 'px';
    box.style.top = Math.min(e.clientY, window.innerHeight - 10 - items.length * 32) + 'px';
    box.classList.remove('hidden');
    box.querySelectorAll('.nt-ctx-item').forEach((btn) => {
      btn.onclick = async () => {
        closeCtx();
        const k = btn.dataset.k;
        if (k === 'new-note') newNote('', path);
        else if (k === 'new-dir') newFolder(path);
        else if (k === 'rename') renamePath(path);
        else if (k === 'delete') deleteNote(path);
        else if (k === 'delete-dir') deleteFolder(path);
        else if (k === 'move') moveNote(path);
        else if (k === 'copy-link') {
          const link = '[[' + noteTitleOf(path) + ']]';
          if (navigator.clipboard) navigator.clipboard.writeText(link).then(() => toast('已复制 ' + link), () => toast('复制失败', 'error'));
        } else if (k === 'pin') {
          const list = pins();
          const i = list.indexOf(path);
          if (i >= 0) list.splice(i, 1); else list.unshift(path);
          lsSet(PIN_KEY, list);
          renderTree();
        }
        if (k !== 'pin') { /* 其余操作自己刷新 */ }
      };
    });
  }
  document.addEventListener('mousedown', (e) => {
    if (!e.target.closest('#nt-ctx')) closeCtx();
  }, true);

  async function searchNotes(q) {
    const box = $0('#nt-hits');
    if (!box) return;
    if (!q) { box.classList.add('hidden'); box.innerHTML = ''; return; }
    const r = await api('/api/notes/search', { q: q });
    const hits = (r && r.hits) || [];
    box.classList.remove('hidden');
    box.innerHTML = hits.length
      ? hits.map((h) => '<div class="hit" data-note="' + escapeHtml(h.path) + '">' +
          '<b>' + escapeHtml(h.name) + '</b>' +
          (h.snippet ? '<span>' + escapeHtml(h.snippet) + '</span>' : '') + '</div>').join('')
      : '<div class="hint">没有匹配的笔记。</div>';
    box.querySelectorAll('.hit').forEach((el) => {
      el.onclick = () => { openNote(el.dataset.note); box.classList.add('hidden'); };
    });
  }

  /* ---------------- 打开 / 保存 ---------------- */
  async function openNote(path) {
    if (!path) return;
    if (WS.current && WS.current !== path) await saveNoteNow();
    const [r, ar] = await Promise.all([
      api('/api/notes/get', { path: path }),
      api('/api/notes/anno/list', { path: path }),
    ]);
    if (!r || !r.ok) { toast('打开失败：' + ((r && r.error) || '笔记不存在')); return; }
    WS.current = path;
    state.noteCurrent = path;                 // 兼容仍在读旧状态的外部代码
    WS.annos = (ar && ar.items) || [];
    WS.dirty = false;
    applyContent(r.content || '');
    $0('#nt-path').textContent = path;
    $0('#nt-path').title = path;
    $0('#nt-empty').classList.toggle('hidden', true);
    $0('#nt-edit-wrap').classList.remove('hidden');
    setSaveState('saved');
    renderBacklinks(r.backlinks || []);
    renderAnnos();
    renderOutline();
    renderTree();
    setView(WS.view);
    lintRun();
    pushRecent(path);
    setSaveState('saved');
    if (WS.graphNet) highlightGraphNode(path);
  }

  function applyContent(md) {
    const ta = $0('#nt-textarea');
    if (ta) ta.value = md;
    if (editor) editor.setMarkdown(md);
    WS.blocks = editor ? editor.getBlocks() : NoteBlocks.parse(md);
    updateStats();
  }

  function scheduleSave() {
    WS.dirty = true;
    setSaveState('dirty');
    clearTimeout(WS.saveTimer);
    WS.saveTimer = setTimeout(saveNoteNow, 900);
  }

  async function saveNoteNow() {
    clearTimeout(WS.saveTimer);
    if (!WS.current || !WS.dirty) return true;
    const md = currentMarkdown();
    setSaveState('saving');
    const r = await api('/api/notes/save', { path: WS.current, content: md });
    if (!r || !r.ok) {
      setSaveState('error');
      toast('保存失败：' + ((r && r.error) || ''));
      return false;
    }
    WS.dirty = false;
    WS.lastSavedAt = Number(r.saved_at) || (Date.now() / 1000);
    setSaveState('saved');
    if (r.graph) WS.graph = r.graph;
    if (r.backlinks) renderBacklinks(r.backlinks);
    if (WS.tab === 'outline') renderOutline();
    return true;
  }

  function onEditorChange() {
    scheduleSave();
    clearTimeout(WS.previewTimer);
    WS.previewTimer = setTimeout(() => {
      if (WS.view === 'split' || WS.view === 'preview') renderPreview();
      renderOutline();
      updateStats();
      lintSchedule();
    }, 320);
    updateStats();
  }

  /* ---------------- 视图切换 ---------------- */
  function setView(mode) {
    const next = ['block', 'split', 'preview', 'source'].indexOf(mode) >= 0 ? mode : 'block';
    const ta = $0('#nt-textarea');
    const ed = $0('#nb-editor');
    const pv = $0('#nt-preview-pane');
    const tb = $0('#nb-toolbar');
    if (WS.view === 'source' && next !== 'source' && ta && editor) {
      editor.setMarkdown(ta.value);
      WS.blocks = editor.getBlocks();
    }
    WS.view = next;
    state.noteMode = next === 'source' ? 'edit' : (next === 'preview' ? 'preview' : 'edit');
    const has = !!WS.current;
    if (ed) ed.classList.toggle('hidden', !has || next === 'source' || next === 'preview');
    if (ta) ta.classList.toggle('hidden', !has || next !== 'source');
    if (pv) pv.classList.toggle('hidden', !has || next === 'block' || next === 'source');
    if (tb) tb.classList.toggle('hidden', !has || next === 'preview' || next === 'source');
    $$0('.nt-view-switch button').forEach((b) => {
      b.classList.toggle('active', b.id === 'nt-view-' + next);
    });
    $0('#nt-edit-wrap').classList.toggle('split', next === 'split');
    if (next === 'preview' || next === 'split') renderPreview();
    if (next === 'source' && ta) {
      ta.value = editor ? editor.getMarkdown() : ta.value;
      ta.focus();
    }
    if (next === 'block' || next === 'split') {
      setTimeout(() => {
        const b = editor && editor.blocks[0];
        if (b) editor.focusBlock(0, true);
      }, 30);
    }
    updateStats();
    lintSchedule();
  }

  function renderPreview() {
    const box = $0('#nt-preview-box');
    if (!box) return;
    hideAnnoTip();                       // 高亮会被重建，先把悬停提示收掉
    const md = currentMarkdown();
    const top = box.scrollTop;
    box.innerHTML = safeRenderMarkdown(md);
    box.querySelectorAll('a.wikilink').forEach((a) => {
      if (!resolveNotePath(a.dataset.note)) a.classList.add('missing');
    });
    applyAnnotations(box);
    if (window.enhanceRichMedia) window.enhanceRichMedia(box);
    box.scrollTop = top;
  }

  /* ---------------- 大纲 ---------------- */
  function headingList() {
    const md = currentMarkdown();
    const out = [];
    md.split('\n').forEach((line, i) => {
      const m = /^(#{1,3})\s+(.+)$/.exec(line);
      if (m) out.push({ level: m[1].length, text: m[2].trim(), line: i });
    });
    return out;
  }
  function renderOutline() {
    const box = $0('#nt-outline');
    if (!box) return;
    const hs = headingList();
    box.innerHTML = hs.length
      ? hs.map((h, i) => '<button type="button" class="nt-out-item lv' + h.level +
          '" data-i="' + i + '">' + escapeHtml(h.text) + '</button>').join('')
      : '<div class="hint nt-pane-hint">还没有标题。用 H1 / H2 写小节，这里会生成大纲。</div>';
    box.querySelectorAll('.nt-out-item').forEach((btn) => {
      btn.onclick = () => jumpToHeading(hs[Number(btn.dataset.i)]);
    });
  }
  function jumpToHeading(h) {
    if (!h) return;
    if (WS.view === 'block' || WS.view === 'source') {
      if (WS.view === 'source') {
        const ta = $0('#nt-textarea');
        if (ta) {
          const lines = ta.value.split('\n');
          let off = 0;
          for (let i = 0; i < h.line; i++) off += (lines[i] || '').length + 1;
          ta.focus();
          try { ta.setSelectionRange(off, off + (lines[h.line] || '').length); } catch (e) { /* 忽略 */ }
          const lh = parseFloat(getComputedStyle(ta).lineHeight) || 24;
          ta.scrollTop = Math.max(0, h.line * lh - ta.clientHeight / 2);
        }
        return;
      }
      const idx = (editor ? editor.getBlocks() : []).findIndex((b) =>
        /^h[1-3]$/.test(b.type) && String(b.text || '').trim() === h.text);
      if (idx >= 0) editor.scrollToBlock(idx);
      return;
    }
    const box = $0('#nt-preview-box');
    if (!box) return;
    const target = $$0('h1,h2,h3', box).find((el) => (el.textContent || '').trim() === h.text);
    if (target) target.scrollIntoView({ block: 'center', behavior: 'smooth' });
  }

  /* ---------------- 右栏 ---------------- */
  function switchTab(name) {
    WS.tab = name;
    $$0('#notes-modal .ntab').forEach((t) => t.classList.toggle('active', t.dataset.ntab === name));
    ['outline', 'backlinks', 'annos', 'graph'].forEach((k) => {
      const el = $0('#nt-' + (k === 'outline' ? 'outline' : k));
      if (el) el.classList.toggle('hidden', k !== name);
    });
    if (name === 'outline') renderOutline();
    if (name === 'annos') renderAnnos();
    if (name === 'graph') drawGraph();
  }

  function renderBacklinks(list) {
    const box = $0('#nt-backlinks');
    if (!box) return;
    const items = list || [];
    box.innerHTML = items.length
      ? items.map((b) => '<div class="bl" data-note="' + escapeHtml(b.path) + '">' +
          '<b>' + escapeHtml(b.name) + '</b>' +
          (b.snippet ? '<div>' + escapeHtml(b.snippet) + '</div>' : '') + '</div>').join('')
      : '<div class="hint nt-pane-hint">还没有其它笔记链接到这一篇。在别的笔记里写 <code>[[' +
        escapeHtml(noteTitle()) + ']]</code> 就会出现在这里。</div>';
    box.querySelectorAll('.bl').forEach((el) => { el.onclick = () => openNote(el.dataset.note); });
  }

  /* ---------------- 批注 ---------------- */
  function textIndex(root) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
    const nodes = [];
    let text = '';
    let n;
    while ((n = walker.nextNode())) {
      nodes.push({ node: n, start: text.length });
      text += n.nodeValue || '';
    }
    return { nodes, text };
  }

  /* 只在文本节点内部切分，跨块时逐块高亮，避免拆坏列表 / 表格结构 */
  function applyAnnotations(root) {
    (WS.annos || []).forEach((a) => {
      const quote = a && a.quote;
      if (!quote) return;
      const { nodes, text } = textIndex(root);
      const want = Math.max(0, Number(a.nth) || 0);
      let idx = -1;
      let from = 0;
      for (let i = 0; i <= want; i++) {
        idx = text.indexOf(quote, from);
        if (idx < 0) break;
        from = idx + 1;
      }
      if (idx < 0) idx = text.indexOf(quote);
      if (idx < 0) return;
      const end = idx + quote.length;
      nodes.forEach((it) => {
        const node = it.node;
        const v = node.nodeValue || '';
        if (!v) return;
        const s = it.start;
        const e = s + v.length;
        if (e <= idx || s >= end) return;
        if (node.parentElement
            && node.parentElement.closest('.katex, .rich-media-src, script, style')) return;
        const a0 = Math.max(0, idx - s);
        const b0 = Math.min(v.length, end - s);
        let target = node;
        if (a0 > 0) target = target.splitText(a0);
        if (b0 - a0 < target.nodeValue.length) target.splitText(b0 - a0);
        const mark = document.createElement('mark');
        mark.className = 'nt-anno';
        mark.dataset.id = a.id;
        target.parentNode.replaceChild(mark, target);
        mark.appendChild(target);
      });
    });
  }

  function renderAnnos() {
    const box = $0('#nt-annos');
    const cnt = $0('#nt-anno-count');
    const items = WS.annos || [];
    if (cnt) cnt.textContent = items.length ? String(items.length) : '';
    if (!box) return;
    box.innerHTML = items.length
      ? items.map((a) => '<div class="nt-anno-item" data-id="' + escapeHtml(a.id) + '">' +
          '<div class="q">“' + escapeHtml(a.quote) + '”</div>' +
          '<div class="c">' +
          ((typeof safeRenderMarkdown === 'function')
            ? safeRenderMarkdown(a.comment || '') : escapeHtml(a.comment || '')) +
          '</div></div>').join('')
      : '<div class="hint nt-pane-hint">在预览或分栏里选中一段文字，点「批注」写下想法。</div>';
    box.querySelectorAll('.nt-anno-item').forEach((el) => {
      el.onclick = () => {
        if (WS.view !== 'preview' && WS.view !== 'split') setView('preview');
        const a = items.find((x) => x.id === el.dataset.id);
        const mark = $0('#nt-preview-box mark.nt-anno[data-id="' + el.dataset.id + '"]');
        if (mark) {
          mark.scrollIntoView({ block: 'center', behavior: 'smooth' });
          mark.classList.add('flash');
          setTimeout(() => mark.classList.remove('flash'), 1000);
          if (a) showAnnoPop(a, mark.getBoundingClientRect());
        } else if (a) {
          showAnnoPop(a, { left: 200, top: 200, bottom: 220 });
        }
      };
    });
  }

  function draftFromRange(range) {
    const pv = $0('#nt-preview-box');
    const { text } = textIndex(pv);
    const quote = range.toString();
    if (!quote) return null;
    const pre = document.createRange();
    pre.selectNodeContents(pv);
    try { pre.setEnd(range.startContainer, range.startOffset); } catch (e) { return null; }
    const start = pre.toString().length;
    let nth = 0;
    let from = 0;
    let hit;
    while ((hit = text.indexOf(quote, from)) >= 0 && hit < start) { nth++; from = hit + 1; }
    const copyText = (window.SelKit && typeof window.SelKit.plainText === 'function')
      ? window.SelKit.plainText(range) : quote;
    return {
      quote: quote.slice(0, 500), copy_text: copyText,
      prefix: text.slice(Math.max(0, start - 30), start),
      suffix: text.slice(start + quote.length, start + quote.length + 30),
      nth: nth, comment: '', rect: range.getBoundingClientRect(),
    };
  }

  function registerNoteSelection() {
    if (noteSelRegistered || !window.SelKit) return;
    noteSelRegistered = true;
    window.SelKit.register({
      id: 'notes',
      roots: () => [$0('#nt-preview-box')],
      maxText: 500,
      capture: (range) => {
        const pv = $0('#nt-preview-box');
        if (!pv || !WS.current || !pv.contains(range.commonAncestorContainer)) return null;
        const raw = String(range.toString() || '').trim();
        if (!raw || raw.length > 500) return null;
        const draft = draftFromRange(range);
        if (!draft || !String(draft.quote || '').trim()) return null;
        return { text: draft.quote, rect: draft.rect || range.getBoundingClientRect(),
                 label: '笔记《' + noteTitle() + '》', raw: draft };
      },
      actions: [
        { id: 'ask', label: '提问', icon: 'chat', primary: true, run: (ctx) => noteAskOpen(ctx.raw, ctx.rect) },
        { id: 'quote', label: '引用', icon: 'note', primary: true, run: (ctx) => quoteNoteDraft(ctx.raw) },
        { id: 'anno', label: '批注', icon: 'pencil', run: (ctx) => showAnnoPop(ctx.raw, ctx.rect) },
        { id: 'copy', label: '复制', icon: 'copy', run: (ctx) => copyPlainText(ctx.raw.copy_text || ctx.raw.quote) },
      ],
    });
  }

  function copyPlainText(text) {
    const t = String(text || '');
    if (!t) return;
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(t).then(() => toast('已复制'), () => toast('复制失败', 'error'));
      return;
    }
    const ta = document.createElement('textarea');
    ta.value = t;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); toast('已复制'); } catch (e) { toast('复制失败', 'error'); }
    ta.remove();
  }

  async function quoteNoteDraft(draft) {
    if (!draft || !draft.quote) return;
    noteAnnoDraft = draft;
    if (typeof addQuote !== 'function') { toast('当前界面不支持引用', 'error'); return; }
    addQuote(draft.quote, '笔记《' + noteTitle() + '》');
    try { await closeNotesModal(); } catch (e) { /* 保存失败也让引用生效 */ }
    const input = $0('#input');
    if (input) input.focus();
    toast('已引用到对话，直接提问即可');
  }

  function noteAskOpen(draft, rect) {
    if (!draft || !String(draft.quote || '').trim()) return;
    if (!window.AskKit) { toast('问答面板未加载，请刷新页面', 'error'); return; }
    const path = WS.current || '';
    if (!path) return;
    window.AskKit.open({
      scope: 'doc:notes:' + path, source: 'notes',
      selection: { kind: 'note', text: draft.quote, prefix: draft.prefix, suffix: draft.suffix,
                   nth: draft.nth, title: noteTitle(), path: path },
      anchor: 'note:' + path + ':' + draft.nth + '|' + String(draft.quote || '').slice(0, 40),
      label: '笔记《' + noteTitle() + '》', quote: draft.quote, title: '笔记问答',
      rect: rect || draft.rect,
      persist: (qa) => saveAskAsAnno(draft, path, qa),
      onQuote: (text) => {
        if (typeof addQuote !== 'function') { toast('当前界面不支持引用', 'error'); return; }
        addQuote(text, '笔记问答《' + noteTitle() + '》');
        window.AskKit.close();
        closeNotesModal().then(() => { const input = $0('#input'); if (input) input.focus(); });
        toast('已引用到对话');
      },
    });
  }

  function saveAskAsAnno(draft, path, qa) {
    return api('/api/notes/anno/save', {
      path: path, id: '', quote: draft.quote, prefix: draft.prefix, suffix: draft.suffix,
      nth: draft.nth, question: (qa && qa.question) || '', answer: (qa && qa.answer) || '',
    }).then((r) => {
      if (!r || !r.ok) throw new Error((r && r.error) || '保存失败');
      WS.annos = r.items || [];
      renderAnnos();
      if (WS.view === 'preview' || WS.view === 'split') renderPreview();
      return true;
    });
  }

  function ensureAnnoPop() {
    if (noteAnnoPop) return noteAnnoPop;
    const el = document.createElement('div');
    el.id = 'nt-anno-pop';
    el.className = 'nt-anno-pop hidden';
    el.innerHTML = '<div class="nt-anno-quote"></div>' +
      '<textarea class="nt-anno-text" placeholder="写下你的批注…"></textarea>' +
      '<div class="nt-anno-acts">' +
        '<button class="ghost nt-anno-del hidden">删除</button>' +
        '<span class="nt-anno-gap"></span>' +
        '<button class="ghost nt-anno-cancel">取消</button>' +
        '<button class="primary nt-anno-save">保存</button>' +
      '</div>';
    document.body.appendChild(el);
    el.querySelector('.nt-anno-cancel').onclick = hideAnnoPop;
    el.querySelector('.nt-anno-save').onclick = saveAnnoPop;
    el.querySelector('.nt-anno-del').onclick = deleteAnnoPop;
    el.addEventListener('mousedown', (e) => e.stopPropagation());
    noteAnnoPop = el;
    return el;
  }

  function hideAnnoPop() {
    if (noteAnnoPop) noteAnnoPop.classList.add('hidden');
    noteAnnoDraft = null;
  }

  /* 批注悬停提示：按 Markdown 渲染，公式不再露出 $...$ 源码。
     原生 title 只能纯文本，所以换成自绘浮层（pointer-events: none，不挡点击）。 */
  function ensureAnnoTip() {
    if (noteAnnoTip) return noteAnnoTip;
    noteAnnoTip = document.createElement('div');
    noteAnnoTip.className = 'nt-anno-tip hidden';
    document.body.appendChild(noteAnnoTip);
    document.addEventListener('mouseover', (e) => {
      if (!(e.target && e.target.closest && e.target.closest('mark.nt-anno'))) hideAnnoTip();
    });
    document.addEventListener('scroll', hideAnnoTip, true);
    return noteAnnoTip;
  }

  function hideAnnoTip() {
    if (noteAnnoTip) noteAnnoTip.classList.add('hidden');
  }

  function showAnnoTip(a, anchor) {
    const tip = ensureAnnoTip();
    const src = String((a && a.comment) || '');
    tip.innerHTML = '<div class="nt-anno-tip-head">批注</div>' +
      '<div class="nt-anno-tip-body">' +
      ((typeof safeRenderMarkdown === 'function') ? safeRenderMarkdown(src) : escapeHtml(src)) +
      '</div>';
    tip.classList.remove('hidden');
    const w = tip.offsetWidth;
    const h = tip.offsetHeight;
    const r = anchor.getBoundingClientRect();
    const left = Math.max(8, Math.min(window.innerWidth - w - 8, r.left + r.width / 2 - w / 2));
    let top = r.top - h - 8;
    if (top < 8) top = Math.min(window.innerHeight - h - 8, r.bottom + 8);
    tip.style.left = left + 'px';
    tip.style.top = Math.max(8, top) + 'px';
  }

  function showAnnoPop(info, rect) {
    if (!info) return;
    hideAnnoTip();                       // 打开编辑弹层就收起悬停提示
    noteAnnoDraft = Object.assign({}, info);
    const pop = ensureAnnoPop();
    pop.querySelector('.nt-anno-quote').textContent =
      info.quote.length > 80 ? info.quote.slice(0, 80) + '…' : info.quote;
    pop.querySelector('.nt-anno-text').value = info.comment || '';
    pop.querySelector('.nt-anno-del').classList.toggle('hidden', !info.id);
    pop.classList.remove('hidden');
    const w = Math.min(360, window.innerWidth - 24);
    pop.style.width = w + 'px';
    const h = pop.offsetHeight || 150;
    const left = Math.max(8, Math.min(window.innerWidth - w - 8, rect.left));
    let top = rect.bottom + 8;
    if (top + h > window.innerHeight - 8) top = Math.max(8, rect.top - h - 8);
    pop.style.left = left + 'px';
    pop.style.top = top + 'px';
    setTimeout(() => pop.querySelector('.nt-anno-text').focus(), 0);
  }

  async function saveAnnoPop() {
    const pop = noteAnnoPop;
    if (!pop || !noteAnnoDraft || !WS.current) return;
    const comment = pop.querySelector('.nt-anno-text').value.trim();
    if (!comment) { toast('批注内容不能为空'); return; }
    const r = await api('/api/notes/anno/save', {
      path: WS.current, id: noteAnnoDraft.id || '',
      quote: noteAnnoDraft.quote, prefix: noteAnnoDraft.prefix,
      suffix: noteAnnoDraft.suffix, nth: noteAnnoDraft.nth, comment: comment,
    });
    if (!r || !r.ok) { toast('保存失败：' + ((r && r.error) || '')); return; }
    WS.annos = r.items || [];
    hideAnnoPop();
    if (WS.view === 'preview' || WS.view === 'split') renderPreview();
    renderAnnos();
    toast('批注已保存');
  }

  async function deleteAnnoPop() {
    if (!noteAnnoDraft || !noteAnnoDraft.id || !WS.current) return;
    if (typeof appConfirm === 'function'
        ? !(await appConfirm('删除这条批注？', { danger: true }))
        : !confirm('删除这条批注？')) return;
    const r = await api('/api/notes/anno/delete', { path: WS.current, id: noteAnnoDraft.id });
    if (!r || !r.ok) { toast('删除失败'); return; }
    WS.annos = r.items || [];
    hideAnnoPop();
    if (WS.view === 'preview' || WS.view === 'split') renderPreview();
    renderAnnos();
    toast('批注已删除');
  }

  function wireAnnotations() {
    const pv = $0('#nt-preview-box');
    if (!pv) return;
    registerNoteSelection();
    pv.addEventListener('mouseover', (e) => {
      const mark = e.target.closest && e.target.closest('mark.nt-anno');
      if (!mark) return;
      const a = (WS.annos || []).find((x) => x.id === mark.dataset.id);
      if (a) showAnnoTip(a, mark);
    });
    pv.addEventListener('click', (e) => {
      const mark = e.target.closest('mark.nt-anno');
      if (!mark) return;
      const a = (WS.annos || []).find((x) => x.id === mark.dataset.id);
      if (a) showAnnoPop(a, mark.getBoundingClientRect());
    });
    document.addEventListener('mousedown', (e) => {
      if (e.target.closest('.selbar') || e.target.closest('.selbar-more-menu') ||
          e.target.closest('#nt-anno-pop') || e.target.closest('.askkit')) return;
      if (noteAnnoPop) noteAnnoPop.classList.add('hidden');
    });
  }

  /* ---------------- 图谱 ---------------- */
  function drawGraph() {
    const el = $0('#nt-graph');
    if (!el || el.classList.contains('hidden')) return;
    const view = WS.graphNet ? (() => {
      try { return { pos: WS.graphNet.getViewPosition(), scale: WS.graphNet.getScale() }; }
      catch (e) { return null; }
    })() : null;
    if (WS.graphNet) { try { WS.graphNet.destroy(); } catch (e) { /* 忽略 */ } WS.graphNet = null; }
    const g = WS.graph || { nodes: [], edges: [] };
    if (!window.vis || !window.vis.Network || !window.vis.DataSet) {
      el.innerHTML = '<p class="hint nt-pane-hint">图谱库 vis-network 未加载。</p>';
      return;
    }
    el.innerHTML = '';
    if (!g.nodes.length) {
      el.innerHTML = '<p class="hint nt-pane-hint">还没有笔记。用 [[双链]] 互相关联后，这里会长出关系网络。</p>';
      return;
    }
    const th = graphTheme();
    const cur = WS.current;
    const activeColor = { background: th.accentSoft, border: th.accent,
                          highlight: { background: th.accentSoft, border: th.accent },
                          hover: { background: th.accentSoft, border: th.accent } };
    const nodes = g.nodes.map((n) => {
      const missing = !!n.missing;
      const active = n.id === cur;
      const o = {
        id: n.id, label: missing ? (n.label + ' ?') : n.label, shape: 'dot',
        size: active ? 22 : (missing ? 12 : 16), borderWidth: 2,
        font: { color: th.font, size: 13, strokeWidth: 3, strokeColor: th.labelBg },
        color: missing
          ? { background: th.warnSoft, border: th.warn, highlight: { background: th.warnSoft, border: th.warn },
              hover: { background: th.warnSoft, border: th.warn } }
          : (active ? activeColor : graphNodeColor(null, th)),
      };
      if (missing) o.shapeProperties = { borderDashes: [4, 4] };
      const p = WS.positions[n.id];
      if (p && typeof p.x === 'number') { o.x = p.x; o.y = p.y; }
      return o;
    });
    const edges = g.edges.map((e) => ({
      id: e.id, from: e.from, to: e.to,
      color: { color: th.edge, highlight: th.edge, hover: th.edge },
      arrows: { to: { enabled: true, scaleFactor: 0.6 } }, width: 1.5,
    }));
    try {
      const ds = { nodes: new vis.DataSet(nodes), edges: new vis.DataSet(edges) };
      WS.graphNet = new vis.Network(el, ds, {
        physics: { stabilization: { enabled: true, iterations: 150 },
                   barnesHut: { gravitationalConstant: -6000, springLength: 120 } },
        interaction: { hover: true }, locale: 'cn', locales: VIS_LOCALES,
      });
      WS.graphNet._ds = ds;
      WS.graphNet.on('stabilized', () => captureGraphPositions());
      WS.graphNet.on('dragEnd', (p) => captureGraphPositions(p.nodes));
      WS.graphNet.on('click', (p) => {
        if (!p.nodes || !p.nodes.length) return;
        const id = p.nodes[0];
        if (String(id).indexOf('missing:') === 0) {
          const nd = (WS.graph.nodes || []).find((x) => x.id === id);
          newNote(((nd ? nd.label : '') + '.md').replace(/[\\/:*?"<>|]/g, ''));
        } else openNote(id);
      });
      if (view) { try { WS.graphNet.moveTo({ position: view.pos, scale: view.scale }); } catch (e) { /* 忽略 */ } }
    } catch (err) {
      WS.graphNet = null;
      el.innerHTML = '<p class="hint nt-pane-hint">图谱渲染失败：' +
        escapeHtml(err && err.message ? err.message : String(err)) + '</p>';
    }
  }
  function captureGraphPositions(ids) {
    if (!WS.graphNet) return;
    let pos = {};
    try { pos = WS.graphNet.getPositions(ids || undefined); } catch (e) { return; }
    Object.keys(pos).forEach((k) => { WS.positions[k] = pos[k]; });
  }
  function highlightGraphNode(path) {
    if (!WS.graphNet || !WS.graphNet._ds) return;
    const th = graphTheme();
    const activeColor = { background: th.accentSoft, border: th.accent,
                          highlight: { background: th.accentSoft, border: th.accent },
                          hover: { background: th.accentSoft, border: th.accent } };
    (WS.graph.nodes || []).forEach((n) => {
      if (n.missing) return;
      try {
        WS.graphNet._ds.nodes.update({
          id: n.id, size: n.id === path ? 22 : 16,
          color: n.id === path ? activeColor : graphNodeColor(null, th),
        });
      } catch (e) { /* 忽略 */ }
    });
  }

  /* ---------------- Markdown 纠错（复用 mdlint） ---------------- */
  function lintEnabled() {
    try { return localStorage.getItem(LINT_KEY) !== '0'; } catch (e) { return true; }
  }
  function lintSchedule() {
    clearTimeout(WS.lintTimer);
    if (!lintEnabled() || !WS.current || !window.MDLint) {
      lintItems = [];
      renderLint();
      return;
    }
    WS.lintTimer = setTimeout(lintRun, 420);
  }
  function lintRun() {
    if (!lintEnabled() || !WS.current || !window.MDLint) { lintItems = []; renderLint(); return; }
    try {
      lintItems = MDLint.lint(currentMarkdown(), { noteNames: Object.keys(notesNameIndex()) });
    } catch (e) {
      lintItems = [];
      if (window.console) console.error('[mdlint] 检查失败', e);
    }
    renderLint();
  }
  function renderLint() {
    const btn = $0('#nt-lint');
    const on = lintEnabled();
    if (btn) {
      btn.classList.toggle('on', on);
      btn.setAttribute('aria-pressed', on ? 'true' : 'false');
      btn.title = on ? 'Markdown 纠错：已开启（点击关闭）' : 'Markdown 纠错：已关闭（点击开启）';
    }
    const n = on ? lintItems.length : 0;
    const b = $0('#nt-lint-n');
    if (b) b.textContent = n ? String(n) : '';
    const box = $0('#nt-lint-box');
    if (!box) return;
    const show = on && !!WS.current && WS.view !== 'preview' && lintItems.length > 0;
    box.classList.toggle('hidden', !show);
    if (!show) { box.innerHTML = ''; return; }
    const fixable = lintItems.filter((x) => x.fix).length;
    const head = '<div class="nt-lint-head"><b>⚠ ' + lintItems.length + '</b> 处问题' +
      (fixable ? '<button class="nt-lint-fix" data-lint-fix-all>全部修正（' + fixable + '）</button>' : '') +
      '<button class="nt-lint-collapse" data-lint-collapse>' +
      (lintCollapsed ? '展开' : '收起') + '</button></div>';
    if (lintCollapsed) { box.innerHTML = head; return; }
    box.innerHTML = head + '<div class="nt-lint-list">' + lintItems.map((it, i) => {
      const ico = it.severity === 'error' ? '✖' : (it.severity === 'warning' ? '⚠' : 'ⓘ');
      return '<div class="nt-lint-row ' + it.severity + '" data-lint-row="' + i + '">' +
        '<span class="nt-lint-ico">' + ico + '</span>' +
        '<span class="nt-lint-msg">' + escapeHtml(it.message) + '</span>' +
        '<span class="nt-lint-line">L' + (it.line + 1) + '</span>' +
        (it.fix ? '<button class="nt-lint-fix-one" data-lint-fix="' + i + '" title="只修正这一条">修复</button>' : '') +
        '</div>';
    }).join('') + '</div>';
  }
  function lintGoto(it) {
    if (!it) return;
    if (WS.view !== 'source') setView('source');
    const ta = $0('#nt-textarea');
    if (!ta) return;
    const lines = ta.value.split('\n');
    let off = 0;
    for (let i = 0; i < it.line; i++) off += (lines[i] || '').length + 1;
    const col = Math.max(1, it.col || 1);
    const start = off + col - 1;
    const end = start + Math.max(1, (it.endCol || col + 1) - col);
    ta.focus();
    try { ta.setSelectionRange(start, Math.min(end, ta.value.length)); } catch (e) { /* 忽略 */ }
    const lh = parseFloat(getComputedStyle(ta).lineHeight) || 24;
    ta.scrollTop = Math.max(0, (it.line + 0.5) * lh - ta.clientHeight / 2);
  }
  function lintApply(items) {
    if (!window.MDLint) return;
    const r = MDLint.fixAll(currentMarkdown(), items || []);
    if (!r || r.fixed <= 0 || r.text === currentMarkdown()) { toast('没有可自动修正的问题'); return; }
    if (WS.view === 'source') {
      const ta = $0('#nt-textarea');
      if (ta) ta.value = r.text;
    } else if (editor) {
      editor.setMarkdown(r.text);
      WS.blocks = editor.getBlocks();
    }
    scheduleSave();
    lintRun();
    if (WS.view === 'split' || WS.view === 'preview') renderPreview();
    renderOutline();
    updateStats();
    toast('已修正 ' + r.fixed + ' 处');
  }

  /* ---------------- 新建 / 重命名 / 移动 / 删除 ---------------- */
  async function newNote(pathArg, dir) {
    let path = pathArg;
    if (!path) {
      const name = await appPrompt('新笔记标题（可写 文件夹/子标题 建到子目录）');
      if (!name || !name.trim()) return;
      path = name.trim();
    }
    if (dir && path.indexOf('/') < 0) path = dir.replace(/\\/g, '/') + '/' + path;
    if (!/\.md$/i.test(path)) path += '.md';
    const r = await api('/api/notes/new', { path: path });
    if (!r || !r.ok) {
      if (r && r.path) { await loadNotes(); await openNote(r.path); toast('该笔记已存在，已为你打开'); return; }
      toast('新建失败：' + ((r && r.error) || ''));
      return;
    }
    WS.tree = r.tree;
    WS.graph = r.graph;
    renderTree();
    await openNote(r.path);
    if (WS.view === 'preview' || WS.view === 'source') setView('block');
    if (editor) editor.focusBlock(0, false);
  }

  async function newFolder(parent) {
    const name = await appPrompt('新文件夹名称（可写 一级/二级）');
    if (!name || !name.trim()) return;
    const path = parent ? parent + '/' + name.trim() : name.trim();
    const r = await api('/api/notes/folder/new', { path: path });
    if (!r || !r.ok) { toast('新建文件夹失败：' + ((r && r.error) || '')); return; }
    WS.tree = r.tree;
    WS.graph = r.graph;
    WS.openDirs.add(path.replace(/\\/g, '/'));
    renderTree();
    toast('已创建文件夹');
  }

  async function renamePath(path) {
    if (!path) return;
    const cur = noteTitleOf(path);
    const name = await appPrompt('重命名为（会自动更新其它笔记里的 [[旧名]] 链接）', cur);
    if (!name || !name.trim() || name.trim() === cur) return;
    if (path === WS.current) await saveNoteNow();
    const r = await api('/api/notes/rename', { path: path, name: name.trim() });
    if (!r || !r.ok) { toast('重命名失败：' + ((r && r.error) || '')); return; }
    WS.tree = r.tree;
    WS.graph = r.graph;
    renderTree();
    if (path === WS.current) { WS.current = ''; await openNote(r.path); }
    toast('已重命名');
  }

  async function moveNote(path) {
    if (!path) return;
    const to = await appPrompt('移动到哪个文件夹？（留空 = 根目录，可写 一级/二级）');
    if (to == null) return;
    const r = await api('/api/notes/move', { path: path, to: String(to || '').trim() });
    if (!r || !r.ok) { toast('移动失败：' + ((r && r.error) || '')); return; }
    WS.tree = r.tree;
    WS.graph = r.graph;
    renderTree();
    if (path === WS.current) await openNote(r.path);
    toast('已移动');
  }

  async function deleteNote(path) {
    if (!path) return;
    const ok = typeof appConfirm === 'function'
      ? await appConfirm('删除笔记「' + noteTitleOf(path) + '」？该操作不可撤销', { danger: true, okText: '删除' })
      : confirm('删除笔记「' + path + '」？该操作不可撤销');
    if (!ok) return;
    clearTimeout(WS.saveTimer);
    WS.dirty = false;
    const r = await api('/api/notes/delete', { path: path });
    if (!r || !r.ok) { toast('删除失败'); return; }
    WS.tree = r.tree || WS.tree;
    WS.graph = r.graph || WS.graph;
    if (path === WS.current) {
      showNoNote();
    }
    renderTree();
    toast('已删除');
  }

  async function deleteFolder(path) {
    if (!path) return;
    const ok = typeof appConfirm === 'function'
      ? await appConfirm('删除文件夹「' + path + '」及里面全部笔记？不可撤销', { danger: true, okText: '删除' })
      : confirm('删除文件夹「' + path + '」及里面全部笔记？');
    if (!ok) return;
    const r = await api('/api/notes/folder/delete', { path: path });
    if (!r || !r.ok) { toast('删除失败'); return; }
    WS.tree = r.tree || WS.tree;
    WS.graph = r.graph || WS.graph;
    renderTree();
    toast('已删除文件夹');
  }

  function exportNote() {
    if (!WS.current) { toast('先打开一篇笔记'); return; }
    const name = noteTitle() + '.md';
    const blob = new Blob([currentMarkdown()], { type: 'text/markdown;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
    toast('已导出 ' + name);
  }

  /* ---------------- 快速打开 ---------------- */
  function quickOpen() {
    const pop = $0('#nt-quick');
    const input = $0('#nt-quick-input');
    if (!pop || !input) return;
    pop.classList.remove('hidden');
    input.value = '';
    WS.quickIndex = 0;
    renderQuick('');
    setTimeout(() => input.focus(), 0);
  }
  function closeQuick() {
    const pop = $0('#nt-quick');
    if (pop) pop.classList.add('hidden');
  }
  function quickMatches(q) {
    const list = flatten(WS.tree);
    const low = String(q || '').toLowerCase();
    const scored = list.map((f) => {
      const name = f.name.toLowerCase();
      const path = f.path.toLowerCase();
      let score = -1;
      if (!low) score = 0;
      else if (name === low) score = 100;
      else if (name.indexOf(low) === 0) score = 80;
      else if (name.indexOf(low) >= 0) score = 60;
      else if (path.indexOf(low) >= 0) score = 40;
      return { f: f, score: score };
    }).filter((x) => x.score >= 0);
    scored.sort((a, b) => b.score - a.score || a.f.name.localeCompare(b.f.name));
    return scored.slice(0, 12).map((x) => x.f);
  }
  function renderQuick(q) {
    const box = $0('#nt-quick-list');
    if (!box) return;
    const list = quickMatches(q);
    if (!list.length) { box.innerHTML = '<div class="hint">没有匹配的笔记。</div>'; return; }
    if (WS.quickIndex >= list.length) WS.quickIndex = 0;
    box.innerHTML = list.map((f, i) =>
      '<div class="nt-quick-row' + (i === WS.quickIndex ? ' on' : '') +
      '" data-note="' + escapeHtml(f.path) + '"><b>' + escapeHtml(f.name) +
      '</b><span>' + escapeHtml(f.path) + '</span></div>').join('');
    box.querySelectorAll('.nt-quick-row').forEach((row) => {
      row.onclick = () => { closeQuick(); openNote(row.dataset.note); };
    });
  }

  /* ---------------- 分栏拖拽 ---------------- */
  function wireSplitter(id, side) {
    const el = $0('#' + id);
    const shell = $0('.notes-shell');
    if (!el || !shell) return;
    el.addEventListener('mousedown', (e) => {
      e.preventDefault();
      const startX = e.clientX;
      const left = $0('#nt-left');
      const right = $0('#nt-right');
      const startW = side === 'left' ? left.getBoundingClientRect().width
                                     : right.getBoundingClientRect().width;
      const move = (ev) => {
        const delta = ev.clientX - startX;
        const w = Math.max(180, Math.min(420, startW + (side === 'left' ? delta : -delta)));
        shell.style.setProperty(side === 'left' ? '--nt-left-w' : '--nt-right-w', w + 'px');
      };
      const up = () => {
        document.removeEventListener('mousemove', move);
        document.removeEventListener('mouseup', up);
        try {
          localStorage.setItem('gm.notes.' + side + 'w',
            (side === 'left' ? left : right).getBoundingClientRect().width);
        } catch (err) { /* 隐私模式 */ }
      };
      document.addEventListener('mousemove', move);
      document.addEventListener('mouseup', up);
    });
  }
  function restoreWidths() {
    const shell = $0('.notes-shell');
    if (!shell) return;
    try {
      const lw = Number(localStorage.getItem('gm.notes.leftw')) || 0;
      const rw = Number(localStorage.getItem('gm.notes.rightw')) || 0;
      if (lw) shell.style.setProperty('--nt-left-w', lw + 'px');
      if (rw) shell.style.setProperty('--nt-right-w', rw + 'px');
    } catch (e) { /* 忽略 */ }
  }

  /* ---------------- 打开 / 关闭工作区 ---------------- */
  function showNoNote() {
    WS.current = '';
    state.noteCurrent = '';
    WS.dirty = false;
    applyContent('');
    $0('#nt-path').textContent = '未打开笔记';
    $0('#nt-empty').classList.remove('hidden');
    const ed = $0('#nb-editor');
    const ta = $0('#nt-textarea');
    const pv = $0('#nt-preview-pane');
    const tb = $0('#nb-toolbar');
    if (ed) ed.classList.add('hidden');
    if (ta) ta.classList.add('hidden');
    if (pv) pv.classList.add('hidden');
    if (tb) tb.classList.add('hidden');
    $0('#nt-edit-wrap').classList.add('hidden');
    $0('#nt-lint-box').classList.add('hidden');
    setSaveState('');
    renderBacklinks([]);
    renderAnnos();
    renderOutline();
  }

  async function openNotesModal() {
    if (!editor) {
      editor = window.NoteBlocks.mount($0('#nb-editor'), {
        onChange: onEditorChange,
        onWikiLink: (name) => {
          const p = resolveNotePath(name);
          if (p) openNote(p);
          else {
            const clean = String(name || '').replace(/[\\/:*?"<>|]/g, '').trim();
            if (clean) newNote(clean + '.md');
          }
        },
        onSaveNow: () => saveNoteNow(),
        askWikiLink: () => appPrompt('双链到哪篇笔记？（输入标题）'),
      });
    }
    restoreWidths();
    showWithAnim($0('#notes-modal'));
    await loadNotes();
    switchTab(WS.tab || 'outline');
    const files = flatten(WS.tree);
    if (WS.current) await openNote(WS.current);
    else if (files.length) await openNote(files[0].path);
    else showNoNote();
    if (WS.view === 'block' && editor) setTimeout(() => editor.focusBlock(0, true), 60);
  }

  async function closeNotesModal() {
    closeQuick();
    closeCtx();
    hideAnnoPop();
    await saveNoteNow();
    hideWithAnim($0('#notes-modal'), () => {
      if (WS.graphNet) { try { WS.graphNet.destroy(); } catch (e) { /* 忽略 */ } WS.graphNet = null; }
    });
  }

  /* 兼容旧调用：edit → 块编辑，其它 → 预览 */
  function setNoteMode(mode) {
    setView(mode === 'edit' ? 'block' : 'preview');
  }

  /* ---------------- 事件绑定 ---------------- */
  function bindNotesEvents() {
    if (bindNotesEvents._done) return;
    bindNotesEvents._done = true;

    $0('#btn-notes').onclick = () => openNotesModal();
    $0('#btn-close-notes').onclick = () => closeNotesModal();
    $0('#nt-new').onclick = () => newNote();
    $0('#nt-new-folder').onclick = () => newFolder('');
    $0('#nt-quickbtn').onclick = () => quickOpen();
    $0('#nt-more').onclick = (e) => {
      e.stopPropagation();
      showMoreMenu(e.currentTarget);
    };

    $$0('.nt-view-switch button').forEach((btn) => {
      btn.onclick = () => setView(btn.id.replace('nt-view-', ''));
    });
    $$0('#notes-modal .ntab').forEach((t) => { t.onclick = () => switchTab(t.dataset.ntab); });

    $0('#nt-lint').onclick = () => {
      const next = !lintEnabled();
      try { localStorage.setItem(LINT_KEY, next ? '1' : '0'); } catch (e) { /* 忽略 */ }
      if (!next) lintItems = [];
      renderLint();
      if (next) lintSchedule();
      toast(next ? '已开启 Markdown 纠错' : '已关闭 Markdown 纠错');
    };
    $0('#nt-lint-box').addEventListener('click', (e) => {
      if (e.target.closest('[data-lint-collapse]')) {
        lintCollapsed = !lintCollapsed;
        renderLint();
        return;
      }
      if (e.target.closest('[data-lint-fix-all]')) {
        lintApply(lintItems.filter((x) => x.fix));
        return;
      }
      const one = e.target.closest('[data-lint-fix]');
      if (one) {
        e.stopPropagation();
        const it = lintItems[Number(one.dataset.lintFix)];
        if (it) lintApply([it]);
        return;
      }
      const row = e.target.closest('[data-lint-row]');
      if (row) lintGoto(lintItems[Number(row.dataset.lintRow)]);
    });

    $0('#nt-search').addEventListener('input', (e) => {
      clearTimeout(WS.searchTimer);
      const q = e.target.value.trim();
      WS.searchTimer = setTimeout(() => searchNotes(q), 250);
      const box = $0('#nt-tree');
      if (!q) { renderTree(); return; }
      // 本地标题过滤：命中项直接高亮，不等全文搜索
      const low = q.toLowerCase();
      $$0('[data-note]', box).forEach((row) => {
        const hit = (row.textContent || '').toLowerCase().indexOf(low) >= 0;
        row.classList.toggle('filtered', !hit);
      });
    });

    const ta = $0('#nt-textarea');
    if (ta) {
      ta.addEventListener('input', () => {
        WS.dirty = true;
        setSaveState('dirty');
        clearTimeout(WS.saveTimer);
        WS.saveTimer = setTimeout(saveNoteNow, 900);
        updateStats();
        lintSchedule();
        if (WS.view === 'split') {
          clearTimeout(WS.previewTimer);
          WS.previewTimer = setTimeout(renderPreview, 320);
        }
      });
      ta.addEventListener('blur', () => { if (WS.view === 'source' && WS.dirty) saveNoteNow(); });
    }

    const tool = $0('#nb-toolbar');
    if (tool) {
      tool.addEventListener('mousedown', (e) => e.preventDefault());  // 不抢编辑器焦点
      tool.addEventListener('click', (e) => {
        const btn = e.target.closest('button');
        if (!btn || !editor) return;
        if (btn.dataset.nbcmd) editor.applyInline(btn.dataset.nbcmd);
        else if (btn.dataset.nbtype) editor.setType(btn.dataset.nbtype);
      });
    }

    // 预览里的双链
    $0('#nt-preview-box').addEventListener('click', (e) => {
      const a = e.target.closest('a.wikilink');
      if (!a) return;
      e.preventDefault();
      const name = a.dataset.note || '';
      const path = resolveNotePath(name);
      if (path) { openNote(path); return; }
      const clean = name.replace(/[\\/:*?"<>|]/g, '').trim();
      if (clean) newNote(clean + '.md');
    });

    // 快速打开：输入 / 方向键 / 回车
    const qi = $0('#nt-quick-input');
    if (qi) {
      qi.addEventListener('input', () => { WS.quickIndex = 0; renderQuick(qi.value.trim()); });
      qi.addEventListener('keydown', (e) => {
        const list = quickMatches(qi.value.trim());
        if (e.key === 'ArrowDown') {
          e.preventDefault();
          WS.quickIndex = Math.min(list.length - 1, WS.quickIndex + 1);
          renderQuick(qi.value.trim());
        } else if (e.key === 'ArrowUp') {
          e.preventDefault();
          WS.quickIndex = Math.max(0, WS.quickIndex - 1);
          renderQuick(qi.value.trim());
        } else if (e.key === 'Enter') {
          e.preventDefault();
          if (list[WS.quickIndex]) { closeQuick(); openNote(list[WS.quickIndex].path); }
        } else if (e.key === 'Escape') {
          e.preventDefault();
          closeQuick();
        }
      });
    }
    $0('#nt-quick').addEventListener('mousedown', (e) => {
      if (e.target === $0('#nt-quick')) closeQuick();
    });

    wireSplitter('nt-splitter-left', 'left');
    wireSplitter('nt-splitter-right', 'right');
    wireAnnotations();

    // 快捷键（只在笔记工作区打开时生效）
    document.addEventListener('keydown', (e) => {
      const modal = $0('#notes-modal');
      if (!modal || modal.classList.contains('hidden')) return;
      const mod = (e.ctrlKey || e.metaKey) && !e.altKey;
      if (mod && !e.shiftKey && e.key.toLowerCase() === 'p') {
        e.preventDefault(); quickOpen(); return;
      }
      if (mod && !e.shiftKey && e.key.toLowerCase() === 's') {
        e.preventDefault(); saveNoteNow(); return;
      }
      if (mod && !e.shiftKey && e.key.toLowerCase() === 'n') {
        e.preventDefault(); newNote(); return;
      }
      if (mod && !e.shiftKey && e.key.toLowerCase() === 'e') {
        e.preventDefault();
        setView(WS.view === 'source' ? 'block' : 'source');
        return;
      }
      if (e.key === 'Escape') {
        const quick = $0('#nt-quick');
        if (quick && !quick.classList.contains('hidden')) { closeQuick(); return; }
        const ctx = $0('#nt-ctx');
        if (ctx && !ctx.classList.contains('hidden')) { closeCtx(); return; }
        if (noteAnnoPop && !noteAnnoPop.classList.contains('hidden')) { hideAnnoPop(); return; }
        const sel = document.querySelector('.selbar');
        if (sel) return;
        closeNotesModal();
      }
    });
  }

  function showMoreMenu(anchor) {
    const box = ensureCtx();
    const has = !!WS.current;
    const items = [
      { k: 'new-folder', label: '新建文件夹' },
      { k: 'rename', label: '重命名当前笔记', disabled: !has },
      { k: 'move', label: '移动当前笔记…', disabled: !has },
      { k: 'export', label: '导出 Markdown', disabled: !has },
      { k: 'delete', label: '删除当前笔记', danger: true, disabled: !has },
    ];
    box.innerHTML = items.map((it) =>
      '<button type="button" class="nt-ctx-item' + (it.danger ? ' danger' : '') +
      (it.disabled ? ' disabled' : '') + '" data-k="' + it.k + '">' +
      escapeHtml(it.label) + '</button>').join('');
    const r = anchor.getBoundingClientRect();
    box.style.left = Math.min(r.right - 190, window.innerWidth - 200) + 'px';
    box.style.top = Math.round(r.bottom + 6) + 'px';
    box.classList.remove('hidden');
    box.querySelectorAll('.nt-ctx-item').forEach((btn) => {
      btn.onclick = () => {
        closeCtx();
        const k = btn.dataset.k;
        if (k === 'new-folder') newFolder('');
        else if (k === 'rename') renamePath(WS.current);
        else if (k === 'move') moveNote(WS.current);
        else if (k === 'export') exportNote();
        else if (k === 'delete') deleteNote(WS.current);
      };
    });
  }

  /* ---------------- 对外接口（保持旧名字） ---------------- */
  window.notesOpen = async function (rel) {
    await openNotesModal();
    if (rel) await openNote(rel);
  };
  window.openNotesModal = openNotesModal;
  window.closeNotesModal = closeNotesModal;
  window.openNote = openNote;
  window.setNoteMode = setNoteMode;
  window.bindNotesEvents = bindNotesEvents;
  window.notesToggleView = () => {
    setView(WS.view === 'source' ? 'block' : 'source');
    return WS.view;
  };
})();
