'use strict';

/* ============================================================
   笔记工作区（全屏）
   ------------------------------------------------------------
   左栏：树 / 置顶 / 最近 / 搜索       中栏：编辑 · 源码 · 阅读 · 分栏（左右各自选）
   右栏：大纲 / 反向链接 / 批注 / 图谱
   存储仍是 data/notes 下的 Markdown；本文件只负责界面与交互。
   ============================================================ */
(function () {
  const VIEWS = ['edit', 'source', 'reading'];
  function normalizeView(v) {
    if (v === 'block') return 'edit';
    if (v === 'preview') return 'reading';
    return VIEWS.indexOf(v) >= 0 ? v : 'edit';
  }
  const WS = {
    tree: [], graph: { nodes: [], edges: [] }, current: '',
    view: normalizeView(lsGet('gm.notes.view', 'edit')),
    split: !!lsGet('gm.notes.split', false),
    splitLeft: normalizeView(lsGet('gm.notes.splitLeft', 'edit')),
    splitRight: normalizeView(lsGet('gm.notes.splitRight', 'reading')),
    tab: 'outline', blocks: [],
    annos: [], openDirs: new Set(), dirty: false,
    sel: new Set(), lastSel: '', rows: [], info: {},
    dragPaths: [], expandTimer: 0, sort: 'name',
    saveTimer: 0, previewTimer: 0, srcTimer: 0, lintTimer: 0, searchTimer: 0,
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
  const OPEN_KEY = 'gm.notes.openDirs';
  const SORT_KEY = 'gm.notes.sort';
  const DOCK_KEY = 'gm.notes.dock';
  const DOCKW_KEY = 'gm.notes.dockW';
  const DOCK_MIN = 340;
  WS.openDirs = loadOpenDirs();
  WS.sort = lsGet(SORT_KEY, 'name') === 'mtime' ? 'mtime' : 'name';
  WS.captures = {};

  function loadOpenDirs() {
    const v = lsGet(OPEN_KEY, []);
    return new Set(Array.isArray(v) ? v.filter((x) => typeof x === 'string') : []);
  }

  const $0 = (s, r) => (r || document).querySelector(s);
  const $$0 = (s, r) => Array.prototype.slice.call((r || document).querySelectorAll(s));
  let editorEl = null;
  let sourceEl = null;
  let srcHost = null;          // #nt-src-editor：Monaco 宿主（源码模式）
  let srcMonacoLib = null;     // monaco 命名空间
  let srcEditor = null;        // 源码编辑器实例
  let srcLoading = null;       // 加载中的 Promise（避免并发重复加载）
  let srcApplying = false;     // 程序写入期间屏蔽 change 回调
  function edNode() {
    if (!editorEl) editorEl = document.getElementById('nb-editor');
    return editorEl;
  }
  function taNode() {
    if (!sourceEl) sourceEl = document.getElementById('nt-textarea');
    return sourceEl;
  }
  function srcHostEl() {
    if (!srcHost) srcHost = document.getElementById('nt-src-editor');
    return srcHost;
  }
  function srcValue() {
    if (srcEditor) return srcEditor.getValue();
    const ta = taNode();
    return ta ? ta.value : '';
  }
  function srcSetValue(md) {
    const v = String(md == null ? '' : md);
    if (srcEditor && srcEditor.getValue() !== v) {
      srcApplying = true;
      try { srcEditor.setValue(v); } finally { srcApplying = false; }
    }
    const ta = taNode();
    if (ta && ta.value !== v) ta.value = v;
  }

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
  function renderPath(path) {
    const el = $0('#nt-path');
    if (!el) return;
    if (!path) { el.textContent = '未打开笔记'; el.title = ''; return; }
    const parts = String(path).split('/');
    const file = parts.pop() || '';
    el.innerHTML = parts.map((p) =>
      '<span class="nt-crumb">' + escapeHtml(p) + '</span>' +
      '<span class="nt-crumb-sep">/</span>').join('') +
      '<b class="nt-crumb-file">' + escapeHtml(file.replace(/\.md$/i, '')) + '</b>';
    el.title = path;
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
  function readBoxes() {
    return $$0('#nt-edit-wrap .nt-read-box');
  }
  function readBoxOf(node) {
    const el = node && (node.nodeType === 1 ? node : node.parentElement);
    return el && el.closest ? el.closest('.nt-read-box') : null;
  }
  function sourceVisible() {
    const host = srcHostEl();
    if (srcEditor && host && !host.classList.contains('hidden')) {
      const pane = host.closest('.nt-pane');
      return !!pane && !pane.classList.contains('hidden');
    }
    const ta = taNode();
    if (!ta || ta.classList.contains('hidden')) return false;
    const pane = ta.closest('.nt-pane');
    return !!pane && !pane.classList.contains('hidden');
  }
  function currentMarkdown() {
    if (sourceVisible()) return srcValue();
    return editor ? editor.getMarkdown() : srcValue();
  }
  function setMarkdownBoth(md) {
    srcSetValue(md);
    if (editor) {
      editor.setMarkdown(md);
      WS.blocks = editor.getBlocks();
    }
  }
  function syncSourceIfVisible() {
    if (!sourceVisible() || !editor) return;
    const md = srcValue();
    if (md !== editor.getMarkdown()) {
      editor.setMarkdown(md);
      WS.blocks = editor.getBlocks();
    }
  }
  function paneModes() {
    if (!WS.split) return [normalizeView(WS.view)];
    let l = normalizeView(WS.splitLeft);
    let r = normalizeView(WS.splitRight);
    if (l === r && (l === 'edit' || l === 'source')) r = 'reading';
    WS.splitLeft = l;
    WS.splitRight = r;
    return [l, r];
  }
  function saveViewPrefs() {
    lsSet('gm.notes.view', WS.view);
    lsSet('gm.notes.split', !!WS.split);
    lsSet('gm.notes.splitLeft', WS.splitLeft);
    lsSet('gm.notes.splitRight', WS.splitRight);
  }
  function saveOpenDirs() { lsSet(OPEN_KEY, Array.from(WS.openDirs)); }
  function parentOf(path) {
    const p = String(path || '');
    const i = p.lastIndexOf('/');
    return i >= 0 ? p.slice(0, i) : '';
  }
  function sortNodes(nodes) {
    const arr = (nodes || []).slice();
    const byName = (a, b) => String(a.name || '').localeCompare(
      String(b.name || ''), 'zh-Hans-CN', { numeric: true, sensitivity: 'base' });
    arr.sort((a, b) => {
      const ad = a.type === 'dir';
      const bd = b.type === 'dir';
      if (ad !== bd) return ad ? -1 : 1;
      if (!ad && WS.sort === 'mtime') {
        const d = (Number(b.mtime) || 0) - (Number(a.mtime) || 0);
        if (d) return d;
      }
      return byName(a, b);
    });
    return arr;
  }
  function remapPath(p, moved) {
    if (!p) return p;
    if (moved[p] != null) return moved[p];
    const keys = Object.keys(moved);
    for (let i = 0; i < keys.length; i += 1) {
      const from = keys[i];
      if (p.indexOf(from + '/') === 0) return moved[from] + p.slice(from.length);
    }
    return p;
  }
  function remapStored(moved) {
    WS.sel = new Set(Array.from(WS.sel).map((p) => remapPath(p, moved)));
    WS.lastSel = remapPath(WS.lastSel, moved);
    WS.dragPaths = WS.dragPaths.map((p) => remapPath(p, moved));
    WS.openDirs = new Set(Array.from(WS.openDirs).map((p) => remapPath(p, moved)));
    saveOpenDirs();
    lsSet(PIN_KEY, Array.from(new Set(pins().map((p) => remapPath(p, moved)))));
    lsSet(RECENT_KEY, Array.from(new Set(recents().map((p) => remapPath(p, moved)))));
    const cur = remapPath(WS.current, moved);
    if (cur !== WS.current) {
      WS.current = cur;
      state.noteCurrent = cur;
      renderPath(cur);
    }
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

  function fileRow(f, extraClass, depth) {
    const active = f.path === WS.current ? ' active' : '';
    const pin = pins().indexOf(f.path) >= 0;
    const sel = WS.sel.has(f.path) ? ' sel' : '';
    const deep = depth == null ? '' : ' style="--depth:' + depth + '"';
    return '<div class="nt-row nt-file' + active + sel + (extraClass ? ' ' + extraClass : '') +
      '" data-path="' + escapeHtml(f.path) + '" data-kind="file" draggable="true"' + deep +
      ' tabindex="0">' +
      '<span class="nt-caret"></span>' +
      '<span class="nt-ico">' + icon('note') + '</span>' +
      '<span class="nt-name">' + escapeHtml(f.name) + '</span>' +
      (pin ? '<span class="nt-pin" title="已置顶">★</span>' : '') +
      '</div>';
  }

  function dirRow(n, depth) {
    const open = WS.openDirs.has(n.path);
    const count = flatten(n.children || []).length;
    const sel = WS.sel.has(n.path) ? ' sel' : '';
    return '<div class="nt-row nt-dir' + sel + '" data-path="' + escapeHtml(n.path) +
      '" data-kind="dir" draggable="true" style="--depth:' + depth + '" tabindex="0">' +
      '<span class="nt-caret">' + (open ? '▾' : '▸') + '</span>' +
      '<span class="nt-ico">' + icon('folder') + '</span>' +
      '<span class="nt-name">' + escapeHtml(n.name) + '</span>' +
      '<span class="nt-num">' + count + '</span>' +
      '<button type="button" class="nt-add" title="在此文件夹新建笔记">＋</button></div>';
  }

  function renderVaultMeta() {
    const el = $0('#nt-vault-meta');
    if (!el) return;
    const n = flatten(WS.tree).length;
    el.textContent = n ? n + ' 篇' : '';
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

    WS.rows = [];
    WS.info = {};
    let html = '';
    const pushRow = (path, kind, parent, inRange) => {
      if (inRange !== false) WS.rows.push({ path: path, kind: kind });
      WS.info[path] = { kind: kind, parent: parent };
    };
    if (pinList.length) {
      html += '<div class="nt-group">置顶</div>';
      pinList.forEach((f) => {
        pushRow(f.path, 'file', parentOf(f.path), false);
        html += fileRow(f, 'nt-pinned');
      });
    }
    if (recentList.length) {
      html += '<div class="nt-group">最近</div>';
      recentList.forEach((f) => {
        if (!WS.info[f.path]) pushRow(f.path, 'file', parentOf(f.path), false);
        html += fileRow(f, 'nt-recent');
      });
    }
    html += '<div class="nt-group nt-root">全部笔记</div>';

    const walk = (nodes, depth, parent) => {
      sortNodes(nodes).forEach((n) => {
        const kind = n.type === 'dir' ? 'dir' : 'file';
        WS.info[n.path] = { kind: kind, parent: parent };
        WS.rows.push({ path: n.path, kind: kind });
        if (n.type === 'dir') {
          html += dirRow(n, depth);
          if (WS.openDirs.has(n.path)) walk(n.children || [], depth + 1, n.path);
        } else {
          html += fileRow(n, '', depth);
        }
      });
    };
    walk(WS.tree, 0, '');
    box.innerHTML = html || '<div class="hint nt-blank">还没有笔记，点右上角「笔记」新建。</div>';
    renderVaultMeta();
    renderDockFiles();
    paintSelection();
  }

  function findRows(path) {
    return $$0('#nt-tree .nt-row[data-path]').filter((r) => r.dataset.path === path);
  }
  function findRow(path) { return findRows(path)[0] || null; }
  function paintSelection() {
    $$0('#nt-tree .nt-row[data-path]').forEach((r) => {
      r.classList.toggle('sel', WS.sel.has(r.dataset.path));
    });
  }
  function selectOnly(path) {
    WS.sel = new Set(path ? [path] : []);
    WS.lastSel = path || '';
    paintSelection();
  }
  function toggleSel(path) {
    if (!path) return;
    if (WS.sel.has(path)) WS.sel.delete(path); else WS.sel.add(path);
    WS.lastSel = path;
    paintSelection();
  }
  function selectRange(path) {
    const idx = WS.rows.findIndex((r) => r.path === path);
    if (idx < 0) return;
    let anchor = WS.rows.findIndex((r) => r.path === WS.lastSel);
    if (anchor < 0) anchor = idx;
    const a = Math.min(idx, anchor);
    const b = Math.max(idx, anchor);
    const set = new Set();
    WS.rows.slice(a, b + 1).forEach((r) => set.add(r.path));
    WS.sel = set;
    paintSelection();
  }
  function clearSelection() { WS.sel = new Set(); WS.lastSel = ''; paintSelection(); }
  function kindOfPath(path) { return (WS.info[path] || {}).kind || ''; }
  function toggleDir(path) {
    if (WS.openDirs.has(path)) WS.openDirs.delete(path); else WS.openDirs.add(path);
    saveOpenDirs();
    renderTree();
  }
  function revealDir(path) {
    let p = String(path || '');
    while (p) {
      WS.openDirs.add(p);
      p = parentOf(p);
    }
    saveOpenDirs();
  }

  /* 右键菜单：定位在鼠标附近 */
  function ensureCtx() {
    let box = $0('#nt-ctx');
    if (box) return box;
    box = document.createElement('div');
    box.id = 'nt-ctx';
    box.className = 'nt-ctx hidden';
    ($0('#notes-modal') || document.body).appendChild(box);
    return box;
  }
  function closeCtx() { const b = $0('#nt-ctx'); if (b) b.classList.add('hidden'); }

  function allDirPaths(nodes, out) {
    const acc = out || [];
    (nodes || []).forEach((n) => {
      if (n.type === 'dir') { acc.push(n.path); allDirPaths(n.children, acc); }
    });
    return acc;
  }

  function showTreeMenu(e, target) {
    const box = ensureCtx();
    const paths = (target && target.paths) ? target.paths.slice()
      : target && target.dir ? [target.dir]
        : target && target.note ? [target.note] : [];
    let items = [];
    if (paths.length > 1) {
      items = [
        { k: 'move', label: '移动 ' + paths.length + ' 项到…' },
        { k: 'delete', label: '删除 ' + paths.length + ' 项', danger: true },
        { k: 'clear', label: '取消选择' },
      ];
    } else if (paths.length === 1) {
      const path = paths[0];
      if (kindOfPath(path) === 'dir') {
        items = [
          { k: 'new-note', label: '新建笔记' },
          { k: 'new-dir', label: '新建子文件夹' },
          { k: 'rename', label: '重命名' },
          { k: 'move', label: '移动到…' },
          { k: 'delete', label: '删除文件夹', danger: true },
        ];
      } else {
        items = [
          { k: 'open', label: '打开' },
          { k: 'pin', label: pins().indexOf(path) >= 0 ? '取消置顶' : '置顶' },
          { k: 'rename', label: '重命名' },
          { k: 'move', label: '移动到…' },
          { k: 'copy-link', label: '复制双链' },
          { k: 'delete', label: '删除', danger: true },
        ];
      }
    } else {
      items = [
        { k: 'new-note', label: '新建笔记' },
        { k: 'new-dir', label: '新建文件夹' },
        { k: 'expand-all', label: '展开全部' },
        { k: 'collapse-all', label: '全部折叠' },
        { k: 'sort-name', label: '按名称排序' },
        { k: 'sort-mtime', label: '按修改时间排序' },
      ];
    }
    box.innerHTML = items.map((it) =>
      '<button type="button" class="nt-ctx-item' + (it.danger ? ' danger' : '') +
      '" data-k="' + it.k + '">' + escapeHtml(it.label) + '</button>').join('');
    box.style.left = Math.min(e.clientX, window.innerWidth - 210) + 'px';
    box.style.top = Math.min(e.clientY, window.innerHeight - 10 - items.length * 32) + 'px';
    box.classList.remove('hidden');
    box.querySelectorAll('.nt-ctx-item').forEach((btn) => {
      btn.onclick = async () => {
        closeCtx();
        const k = btn.dataset.k;
        const path = paths[0] || '';
        if (k === 'move') { movePaths(paths); return; }
        if (k === 'delete') { deletePaths(paths); return; }
        if (k === 'clear') { clearSelection(); return; }
        if (k === 'new-note') { newNote('', path); return; }
        if (k === 'new-dir') { newFolder(path); return; }
        if (k === 'rename') { renamePath(path); return; }
        if (k === 'open') { openNote(path); return; }
        if (k === 'copy-link') {
          const link = '[[' + noteTitleOf(path) + ']]';
          if (navigator.clipboard) navigator.clipboard.writeText(link).then(() => toast('已复制 ' + link), () => toast('复制失败', 'error'));
        } else if (k === 'pin') {
          const list = pins();
          const i = list.indexOf(path);
          if (i >= 0) list.splice(i, 1); else list.unshift(path);
          lsSet(PIN_KEY, list);
          renderTree();
        } else if (k === 'expand-all') {
          allDirPaths(WS.tree).forEach((p) => WS.openDirs.add(p));
          saveOpenDirs();
          renderTree();
        } else if (k === 'collapse-all') {
          WS.openDirs.clear();
          saveOpenDirs();
          renderTree();
        } else if (k === 'sort-name' || k === 'sort-mtime') {
          WS.sort = k === 'sort-name' ? 'name' : 'mtime';
          lsSet(SORT_KEY, WS.sort);
          renderTree();
        }
      };
    });
  }
  document.addEventListener('mousedown', (e) => {
    if (!e.target.closest('#nt-ctx')) closeCtx();
  }, true);

  /* ---------------- 文件树：多选 / 拖拽 / 内联重命名 ---------------- */

  function clearDropMarks() {
    $$0('#nt-tree .nt-row.drop-on').forEach((r) => r.classList.remove('drop-on'));
    const box = $0('#nt-tree');
    if (box) box.classList.remove('drop-root');
  }
  function markDrop(target) {
    clearDropMarks();
    if (target && target.classList) target.classList.add('drop-on');
    else {
      const box = $0('#nt-tree');
      if (box) box.classList.add('drop-root');
    }
  }
  function canDropTo(target, paths) {
    const list = paths || WS.dragPaths;
    return !list.some((p) => target === p || target.indexOf(p + '/') === 0);
  }

  function wireTreeEvents() {
    const tree = $0('#nt-tree');
    if (!tree || tree._wired) return;
    tree._wired = true;

    tree.addEventListener('click', (e) => {
      if (e.target.closest('.nt-rename-input')) return;
      const add = e.target.closest('.nt-add');
      if (add) {
        e.stopPropagation();
        const row = add.closest('.nt-row');
        if (row) newNote('', row.dataset.path);
        return;
      }
      const row = e.target.closest('.nt-row[data-path]');
      if (!row) { clearSelection(); return; }
      const path = row.dataset.path;
      if (e.ctrlKey || e.metaKey) { toggleSel(path); return; }
      if (e.shiftKey) { selectRange(path); return; }
      selectOnly(path);
      if (row.dataset.kind === 'dir') toggleDir(path);
      else openNote(path);
    });

    tree.addEventListener('dblclick', (e) => {
      const name = e.target.closest('.nt-name');
      const row = name && name.closest('.nt-row[data-path]');
      if (row) { e.preventDefault(); beginRename(row.dataset.path, row); }
    });

    tree.addEventListener('contextmenu', (e) => {
      const row = e.target.closest('.nt-row[data-path]');
      e.preventDefault();
      if (!row) { clearSelection(); showTreeMenu(e, { paths: [] }); return; }
      if (!WS.sel.has(row.dataset.path)) selectOnly(row.dataset.path);
      showTreeMenu(e, { paths: Array.from(WS.sel) });
    });

    tree.addEventListener('keydown', (e) => {
      if (e.target.closest('.nt-rename-input')) return;
      const row = e.target.closest('.nt-row[data-path]');
      if (!row) return;
      const path = row.dataset.path;
      if (e.key === 'F2') { e.preventDefault(); beginRename(path, row); }
      else if (e.key === 'Enter') {
        e.preventDefault();
        if (row.dataset.kind === 'dir') toggleDir(path); else openNote(path);
      } else if (e.key === 'Delete' || e.key === 'Backspace') {
        e.preventDefault();
        deletePaths(WS.sel.size ? Array.from(WS.sel) : [path]);
      } else if (e.key === 'Escape') {
        clearSelection();
      }
    });

    tree.addEventListener('dragstart', (e) => {
      const row = e.target.closest('.nt-row[data-path]');
      if (!row) return;
      if (!WS.sel.has(row.dataset.path)) selectOnly(row.dataset.path);
      WS.dragPaths = Array.from(WS.sel);
      try {
        e.dataTransfer.effectAllowed = 'move';
        e.dataTransfer.setData('text/plain', WS.dragPaths.join('\n'));
      } catch (err) { /* 忽略 */ }
      tree.classList.add('dragging');
    });

    tree.addEventListener('dragend', () => {
      WS.dragPaths = [];
      clearTimeout(WS.expandTimer);
      clearDropMarks();
      tree.classList.remove('dragging');
    });

    tree.addEventListener('dragover', (e) => {
      if (!WS.dragPaths.length) return;
      const row = e.target.closest('.nt-row[data-path]');
      let target = '';
      if (row) target = row.dataset.kind === 'dir' ? row.dataset.path : parentOf(row.dataset.path);
      if (!canDropTo(target)) { e.dataTransfer.dropEffect = 'none'; clearDropMarks(); return; }
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      markDrop(row && row.dataset.kind === 'dir' && row.dataset.path === target
        ? row : (target ? findRow(target) : null));
      if (row && row.dataset.kind === 'dir' && !WS.openDirs.has(target)) {
        clearTimeout(WS.expandTimer);
        WS.expandTimer = setTimeout(() => {
          if (WS.openDirs.has(target)) return;
          WS.openDirs.add(target);
          saveOpenDirs();
          renderTree();
        }, 650);
      }
    });

    tree.addEventListener('dragleave', (e) => {
      if (!tree.contains(e.relatedTarget)) clearDropMarks();
    });

    tree.addEventListener('drop', (e) => {
      if (!WS.dragPaths.length) return;
      const row = e.target.closest('.nt-row[data-path]');
      const target = row
        ? (row.dataset.kind === 'dir' ? row.dataset.path : parentOf(row.dataset.path))
        : '';
      const paths = WS.dragPaths.slice();
      WS.dragPaths = [];
      clearTimeout(WS.expandTimer);
      clearDropMarks();
      tree.classList.remove('dragging');
      if (!canDropTo(target, paths)) return;
      e.preventDefault();
      movePathsTo(paths, target);
    });
  }

  let pickResolve = null;
  function ensurePicker() {
    let box = $0('#nt-pick');
    if (box) return box;
    box = document.createElement('div');
    box.id = 'nt-pick';
    box.className = 'nt-pick hidden';
    box.innerHTML = '<div class="nt-pick-card">' +
      '<div class="nt-pick-head"><b></b><button type="button" class="nt-pick-x" title="关闭">✕</button></div>' +
      '<input id="nt-pick-q" class="field-input" placeholder="筛选文件夹…">' +
      '<div id="nt-pick-list" class="nt-pick-list"></div></div>';
    ($0('#notes-modal') || document.body).appendChild(box);
    box.addEventListener('mousedown', (e) => { if (e.target === box) closePicker(null); });
    box.querySelector('.nt-pick-x').onclick = () => closePicker(null);
    box.querySelector('#nt-pick-q').addEventListener('input', (e) => renderPickList(e.target.value));
    return box;
  }
  function folderList() {
    const out = [{ path: '', name: '根目录', depth: 0 }];
    const walk = (nodes, depth) => {
      sortNodes(nodes).forEach((n) => {
        if (n.type !== 'dir') return;
        out.push({ path: n.path, name: n.name, depth: depth });
        walk(n.children || [], depth + 1);
      });
    };
    walk(WS.tree, 0);
    return out;
  }
  function renderPickList(q) {
    const box = $0('#nt-pick-list');
    if (!box) return;
    const low = String(q || '').trim().toLowerCase();
    const items = folderList().filter((it) => !low ||
      it.name.toLowerCase().indexOf(low) >= 0 || it.path.toLowerCase().indexOf(low) >= 0);
    box.innerHTML = items.length ? items.map((it) =>
      '<button type="button" class="nt-pick-row" data-dir="' + escapeHtml(it.path) +
      '" style="--depth:' + it.depth + '">' +
      '<span class="nt-pick-ico">' + icon('folder') + '</span>' +
      '<span class="nt-pick-name">' + escapeHtml(it.name) + '</span>' +
      (it.path ? '<span class="nt-pick-path">' + escapeHtml(it.path) + '</span>' : '') +
      '</button>').join('') : '<div class="hint">没有匹配的文件夹</div>';
    box.querySelectorAll('.nt-pick-row').forEach((btn) => {
      btn.onclick = () => closePicker(btn.dataset.dir);
    });
  }
  function pickFolder(title) {
    const box = ensurePicker();
    box.querySelector('.nt-pick-head b').textContent = title || '选择文件夹';
    const q = box.querySelector('#nt-pick-q');
    q.value = '';
    renderPickList('');
    box.classList.remove('hidden');
    setTimeout(() => q.focus(), 20);
    return new Promise((resolve) => { pickResolve = resolve; });
  }
  function closePicker(value) {
    const box = $0('#nt-pick');
    if (box) box.classList.add('hidden');
    const r = pickResolve;
    pickResolve = null;
    if (r) r(value);
  }

  function beginRename(path, row) {
    const r = row || findRow(path);
    if (!r) return;
    const nameEl = r.querySelector('.nt-name');
    if (!nameEl || r.querySelector('.nt-rename-input')) return;
    const old = nameEl.textContent;
    const input = document.createElement('input');
    input.className = 'nt-rename-input';
    input.value = old;
    input.dataset.old = old;
    nameEl.style.display = 'none';
    nameEl.after(input);
    input.focus();
    input.select();
    let done = false;
    const cancel = () => {
      if (done) return;
      done = true;
      input.remove();
      nameEl.style.display = '';
    };
    const commit = async () => {
      if (done) return;
      const val = input.value.trim();
      if (!val || val === old) { cancel(); return; }
      done = true;
      input.remove();
      nameEl.style.display = '';
      await applyRename(path, val);
    };
    input.addEventListener('keydown', (e) => {
      e.stopPropagation();
      if (e.key === 'Enter') { e.preventDefault(); commit(); }
      else if (e.key === 'Escape') { e.preventDefault(); cancel(); }
    });
    input.addEventListener('blur', () => { if (!done) commit(); });
  }

  async function applyRename(path, name) {
    if (path === WS.current) await saveNoteNow();
    const r = await api('/api/notes/rename', { path: path, name: name });
    if (!r || !r.ok) {
      toast('重命名失败：' + ((r && r.error) || ''));
      renderTree();
      return;
    }
    WS.tree = r.tree;
    WS.graph = r.graph;
    remapStored({ [path]: r.path });
    renderTree();
    toast('已重命名');
  }

  function movePaths(paths) {
    const list = (paths || []).filter(Boolean);
    if (!list.length) return;
    const title = list.length > 1
      ? '移动 ' + list.length + ' 项到…'
      : '移动「' + noteTitleOf(list[0]) + '」到…';
    pickFolder(title).then((to) => {
      if (to == null) return;
      movePathsTo(list, to);
    });
  }

  async function movePathsTo(paths, to) {
    const list = (paths || []).filter(Boolean);
    if (!list.length) return;
    const r = await api('/api/notes/move', { paths: list, to: to || '' });
    if (!r || !r.ok) { toast('移动失败：' + ((r && r.error) || '')); return; }
    const moved = {};
    (r.moved || []).forEach((m) => { moved[m.from] = m.to; });
    if (r.tree) WS.tree = r.tree;
    if (r.graph) WS.graph = r.graph;
    const failed = (r.errors || []).length;
    if (failed) {
      toast((r.moved || []).length + ' 项已移动，' + failed + ' 项失败：' +
        ((r.errors[0] || {}).error || ''), 'error');
    } else {
      toast('已移动 ' + (r.moved || []).length + ' 项');
    }
    if (Object.keys(moved).length) {
      remapStored(moved);
      revealDir(to || '');
    }
    if (r.tree) renderTree();
    clearSelection();
  }

  async function deletePaths(paths) {
    let list = (paths || []).filter(Boolean);
    if (!list.length) return;
    list = list.filter((p) => !list.some((q) => q !== p && p.indexOf(q + '/') === 0));
    const isCurrent = !!WS.current && list.some((p) =>
      p === WS.current || WS.current.indexOf(p + '/') === 0);
    const msg = list.length > 1
      ? '删除选中的 ' + list.length + ' 项？文件夹及其中笔记不可恢复'
      : (kindOfPath(list[0]) === 'dir'
        ? '删除文件夹「' + list[0] + '」及里面全部笔记？不可撤销'
        : '删除笔记「' + noteTitleOf(list[0]) + '」？该操作不可撤销');
    const ok = typeof appConfirm === 'function'
      ? await appConfirm(msg, { danger: true, okText: '删除' })
      : confirm(msg);
    if (!ok) return;
    if (isCurrent) { clearTimeout(WS.saveTimer); WS.dirty = false; }
    let tree = WS.tree;
    let graph = WS.graph;
    let done = 0;
    let lastErr = '';
    for (const p of list) {
      const url = kindOfPath(p) === 'dir' ? '/api/notes/folder/delete' : '/api/notes/delete';
      const r = await api(url, { path: p });
      if (r && r.ok) {
        done += 1;
        if (r.tree) tree = r.tree;
        if (r.graph) graph = r.graph;
      } else {
        lastErr = (r && r.error) || '';
      }
    }
    WS.tree = tree;
    WS.graph = graph;
    WS.sel = new Set(Array.from(WS.sel).filter((p) =>
      !list.some((q) => p === q || p.indexOf(q + '/') === 0)));
    renderTree();
    if (isCurrent) showNoNote();
    toast(done ? ('已删除 ' + done + ' 项' +
      (list.length > done ? '，' + (list.length - done) + ' 项失败' : ''))
      : ('删除失败：' + lastErr));
  }

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
    renderPath(path);
    $0('#nt-empty').classList.toggle('hidden', true);
    $0('#nt-edit-wrap').classList.remove('hidden');
    setSaveState('saved');
    renderBacklinks(r.backlinks || []);
    renderAnnos();
    renderOutline();
    renderTree();
    mountPanes();
    lintRun();
    pushRecent(path);
    setSaveState('saved');
    renderDockFiles();
    updateRefNoteBtn();
    if (WS.graphNet) highlightGraphNode(path);
  }

  function applyContent(md) {
    srcSetValue(md);
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
    const md = editor ? editor.getMarkdown() : '';
    // 源码编辑器正在输入时不要回写它（否则会打断输入 / 撤销栈）
    let srcFocused = false;
    try { srcFocused = !!(srcEditor && srcEditor.hasTextFocus && srcEditor.hasTextFocus()); } catch (e) { srcFocused = false; }
    if (!srcFocused) srcSetValue(md);
    clearTimeout(WS.previewTimer);
    WS.previewTimer = setTimeout(() => {
      if (readBoxes().length) renderPreview();
      renderOutline();
      updateStats();
      lintSchedule();
    }, 320);
    updateStats();
  }

  /* 源码模式的输入处理：textarea 与 Monaco 共用同一套（自动保存 / 同步块编辑器 / 预览 / 纠错） */
  function onSourceInput() {
    WS.dirty = true;
    setSaveState('dirty');
    clearTimeout(WS.saveTimer);
    WS.saveTimer = setTimeout(saveNoteNow, 900);
    updateStats();
    lintSchedule();
    clearTimeout(WS.srcTimer);
    WS.srcTimer = setTimeout(() => {
      if (!editor) return;
      const md = srcValue();
      if (md !== editor.getMarkdown()) {
        editor.setMarkdown(md);
        WS.blocks = editor.getBlocks();
      }
      renderOutline();
      updateStats();
      lintSchedule();
    }, 300);
    clearTimeout(WS.previewTimer);
    WS.previewTimer = setTimeout(renderPreview, 320);
  }

  /* ---------------- 视图切换 / 分栏 ---------------- */

  function paneEls() { return [$0('#nt-pane-left'), $0('#nt-pane-right')]; }

  function makeReadBox() {
    const box = document.createElement('div');
    box.className = 'notes-preview content nt-read-box';
    return box;
  }

  /* 源码模式用本地 Monaco（与 IDE 同一份）：懒加载，加载失败退回 <textarea>。
     宿主节点在 pane 之间移动即可，编辑器实例不销毁，避免每次切视图重建。 */
  function ensureSrcEditor() {
    if (srcEditor) return Promise.resolve(srcEditor);
    if (srcLoading) return srcLoading;
    if (typeof window.loadMonaco !== 'function') return Promise.resolve(null);
    srcLoading = window.loadMonaco().then((M) => {
      srcLoading = null;
      if (!M) return null;
      const host = srcHostEl();
      if (!host) return null;
      const theme = (typeof ideMonacoThemeName === 'function') ? ideMonacoThemeName() : 'vs';
      srcMonacoLib = M;
      try {
        srcEditor = M.editor.create(host, {
          value: '',
          language: 'markdown',
          theme: theme,
          ariaLabel: 'Markdown 源码',
          automaticLayout: true,
          minimap: { enabled: false },
          lineNumbers: 'on',
          glyphMargin: false,
          folding: true,
          wordWrap: 'on',
          fontSize: 13,
          tabSize: 2,
          renderLineHighlight: 'line',
          scrollBeyondLastLine: false,
          scrollbar: { verticalScrollbarSize: 10, horizontalScrollbarSize: 10 },
          padding: { top: 10, bottom: 60 },
          fixedOverflowWidgets: true,
        });
      } catch (e) {
        srcEditor = null;
        if (window.console) console.error('[notes] Monaco 创建失败，退回 textarea', e);
        return null;
      }
      const ed = srcEditor;
      ed.onDidChangeModelContent(() => {
        if (srcApplying) return;
        onSourceInput();
        lintApplyMarkers();
      });
      ed.onDidBlurEditorText(() => {
        if (sourceVisible() && WS.dirty) saveNoteNow();
      });
      // 用 textarea 里已有的当前内容初始化（openNote → applyContent 先写的是它）
      const seed = taNode() ? taNode().value : '';
      if (seed) {
        srcApplying = true;
        try { ed.setValue(seed); } finally { srcApplying = false; }
      }
      mountPanes();
      lintRun();
      if (WS.current && WS.view === 'source' && !WS.split) {
        setTimeout(() => { try { ed.focus(); } catch (e) { /* 忽略 */ } }, 30);
      }
      return ed;
    });
    return srcLoading;
  }

  // 主题切换时由 settings/theme.js → ideSyncMonacoTheme → 这里同步
  window.notesSourceSyncTheme = function () {
    if (!srcEditor || !srcMonacoLib) return;
    const theme = (typeof ideMonacoThemeName === 'function') ? ideMonacoThemeName() : 'vs';
    try { srcMonacoLib.editor.setTheme(theme); } catch (e) { /* 忽略 */ }
  };

  function mountPanes() {
    const panes = paneEls();
    const modes = paneModes();
    const tops = panes.map((p) => {
      const b = p && p.querySelector('.nt-read-box');
      return b ? b.scrollTop : 0;
    });
    const ed = edNode();
    const ta = taNode();
    const host = srcHostEl();
    if (host && host.parentNode) host.parentNode.removeChild(host);
    if (ed && ed.parentNode) ed.parentNode.removeChild(ed);
    if (ta && ta.parentNode) ta.parentNode.removeChild(ta);
    // 需要源码视图：懒加载 Monaco，加载完会重新 mount 一次
    if (modes.indexOf('source') >= 0 && !srcEditor && !srcLoading) ensureSrcEditor();
    panes.forEach((p, i) => {
      if (!p) return;
      p.innerHTML = '';
      p.classList.toggle('hidden', i > 0 && !WS.split);
      const mode = modes[i];
      if (!mode) return;
      if (mode === 'edit' && ed) p.appendChild(ed);
      else if (mode === 'source') {
        if (srcEditor && host) p.appendChild(host);
        else if (ta) p.appendChild(ta);
      } else p.appendChild(makeReadBox());
    });
    if (ed) ed.classList.toggle('hidden', modes.indexOf('edit') < 0);
    if (host) host.classList.toggle('hidden', !srcEditor || modes.indexOf('source') < 0);
    if (ta) ta.classList.toggle('hidden', !!srcEditor || modes.indexOf('source') < 0);
    applyViewChrome();
    if (srcEditor) { try { srcEditor.layout(); } catch (e) { /* 忽略 */ } }
    if (readBoxes().length) renderPreview();
    readBoxes().forEach((box, i) => { box.scrollTop = tops[i] || 0; });
    updateStats();
    lintSchedule();
  }

  function applyViewChrome() {
    const modes = paneModes();
    $$0('.nt-view-switch button').forEach((b) => {
      const m = b.id.replace('nt-view-', '');
      if (m === 'split') b.classList.toggle('active', !!WS.split);
      else b.classList.toggle('active', !WS.split && normalizeView(m) === normalizeView(WS.view));
    });
    $0('#nt-split-bar').classList.toggle('hidden', !WS.split);
    const sl = $0('#nt-split-left');
    const sr = $0('#nt-split-right');
    if (sl) sl.value = normalizeView(WS.splitLeft);
    if (sr) sr.value = normalizeView(WS.splitRight);
    const tb = $0('#nb-toolbar');
    if (tb) tb.classList.toggle('hidden', !WS.current || modes.indexOf('edit') < 0);
    $0('#nt-edit-wrap').classList.toggle('split', !!WS.split);
    state.noteMode = (!WS.split && modes[0] === 'reading') ? 'preview' : 'edit';
  }

  function setView(mode) {
    const next = normalizeView(mode);
    syncSourceIfVisible();
    WS.split = false;
    WS.view = next;
    saveViewPrefs();
    mountPanes();
    if (next === 'source') {
      srcSetValue(editor ? editor.getMarkdown() : srcValue());
      if (srcEditor) setTimeout(() => { try { srcEditor.focus(); } catch (e) { /* 忽略 */ } }, 20);
      else {
        const ta = taNode();
        if (ta) ta.focus();
      }
    } else if (next === 'edit' && editor && WS.current) {
      setTimeout(() => {
        const b = editor.blocks[0];
        if (b) editor.focusBlock(0, true);
      }, 30);
    }
  }

  function toggleSplit() {
    syncSourceIfVisible();
    WS.split = !WS.split;
    if (!WS.split) WS.view = normalizeView(WS.splitLeft);
    saveViewPrefs();
    mountPanes();
    if (WS.split) {
      const modes = paneModes();
      const idx = modes.indexOf('edit') >= 0 ? modes.indexOf('edit') : modes.indexOf('source');
      if (idx >= 0) {
        setTimeout(() => {
          if (modes[idx] === 'edit' && editor && editor.blocks[0]) editor.focusBlock(0, true);
          else if (modes[idx] === 'source') {
            if (srcEditor) { try { srcEditor.focus(); } catch (e) { /* 忽略 */ } }
            else { const ta = taNode(); if (ta) ta.focus(); }
          }
        }, 30);
      }
    }
  }

  function setSplitSide(side, mode) {
    syncSourceIfVisible();
    const next = normalizeView(mode);
    if (side === 'left') WS.splitLeft = next;
    else WS.splitRight = next;
    if (WS.splitLeft === WS.splitRight &&
        (WS.splitLeft === 'edit' || WS.splitLeft === 'source')) {
      if (side === 'left') WS.splitRight = 'reading';
      else WS.splitLeft = 'reading';
    }
    saveViewPrefs();
    mountPanes();
  }

  function renderPreview() {
    const boxes = readBoxes();
    if (!boxes.length) return;
    hideAnnoTip();                       // 高亮会被重建，先把悬停提示收掉
    const md = currentMarkdown();
    boxes.forEach((box) => {
      const top = box.scrollTop;
      box.innerHTML = safeRenderMarkdown(md);
      box.querySelectorAll('a.wikilink').forEach((a) => {
        if (!resolveNotePath(a.dataset.note)) a.classList.add('missing');
      });
      applyAnnotations(box);
      if (window.enhanceRichMedia) window.enhanceRichMedia(box);
      box.scrollTop = top;
    });
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
    const primary = paneModes()[0];
    if (primary === 'source' && sourceVisible()) {
      const line = Math.max(1, (h.line || 0) + 1);
      if (srcEditor) {
        const model = srcEditor.getModel();
        const len = model ? model.getLineContent(line).length : 0;
        srcEditor.revealLineInCenter(line);
        srcEditor.setSelection({
          startLineNumber: line, startColumn: 1,
          endLineNumber: line, endColumn: Math.max(2, len + 1),
        });
        srcEditor.focus();
        return;
      }
      const ta = taNode();
      if (!ta) return;
      const lines = ta.value.split('\n');
      let off = 0;
      for (let i = 0; i < h.line; i++) off += (lines[i] || '').length + 1;
      ta.focus();
      try { ta.setSelectionRange(off, off + (lines[h.line] || '').length); } catch (e) { /* 忽略 */ }
      const lh = parseFloat(getComputedStyle(ta).lineHeight) || 24;
      ta.scrollTop = Math.max(0, h.line * lh - ta.clientHeight / 2);
      return;
    }
    if (primary === 'edit' && editor) {
      const idx = editor.getBlocks().findIndex((b) =>
        /^h[1-3]$/.test(b.type) && String(b.text || '').trim() === h.text);
      if (idx >= 0) { editor.scrollToBlock(idx); return; }
    }
    const box = readBoxes()[0];
    if (!box) return;
    const target = $$0('h1,h2,h3', box).find((el) => (el.textContent || '').trim() === h.text);
    if (target) target.scrollIntoView({ block: 'center', behavior: GM_SMOOTH });
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
      : '<div class="hint nt-pane-hint">在阅读或分栏里选中一段文字，点「批注」写下想法。</div>';
    box.querySelectorAll('.nt-anno-item').forEach((el) => {
      el.onclick = () => {
        if (!readBoxes().length) setView('reading');
        const a = items.find((x) => x.id === el.dataset.id);
        let mark = null;
        readBoxes().some((b) => {
          mark = b.querySelector('mark.nt-anno[data-id="' + el.dataset.id + '"]');
          return !!mark;
        });
        if (mark) {
          mark.scrollIntoView({ block: 'center', behavior: GM_SMOOTH });
          mark.classList.add('flash');
          setTimeout(() => mark.classList.remove('flash'), 1000);
          if (a) showAnnoPop(a, mark.getBoundingClientRect());
        } else if (a) {
          showAnnoPop(a, { left: 200, top: 200, bottom: 220 });
        }
      };
    });
  }

  function draftFromRange(range, pv) {
    pv = pv || readBoxOf(range.commonAncestorContainer) || readBoxes()[0];
    if (!pv) return null;
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
      roots: () => readBoxes(),
      maxText: 500,
      capture: (range) => {
        const pv = readBoxOf(range.commonAncestorContainer);
        if (!pv || !WS.current || !pv.contains(range.commonAncestorContainer)) return null;
        const raw = String(range.toString() || '').trim();
        if (!raw || raw.length > 500) return null;
        const draft = draftFromRange(range, pv);
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
    if (!isDockedOpen()) {
      try { await closeNotesModal(); } catch (e) { /* 保存失败也让引用生效 */ }
    }
    const input = $0('#input');
    if (input) input.focus();
    toast(isDockedOpen() ? '已引用到对话（笔记保持打开）' : '已引用到对话，直接提问即可');
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
        if (isDockedOpen()) {
          const input = $0('#input');
          if (input) input.focus();
        } else {
          closeNotesModal().then(() => { const input = $0('#input'); if (input) input.focus(); });
        }
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
      if (readBoxes().length) renderPreview();
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
    el.addEventListener('mousedown', (e) => e.stopPropagation());
    el.querySelector('.nt-anno-cancel').onclick = hideAnnoPop;
    el.querySelector('.nt-anno-save').onclick = saveAnnoPop;
    el.querySelector('.nt-anno-del').onclick = deleteAnnoPop;
    ($0('#notes-modal') || document.body).appendChild(el);
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
    ($0('#notes-modal') || document.body).appendChild(noteAnnoTip);
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
    if (readBoxes().length) renderPreview();
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
    if (readBoxes().length) renderPreview();
    renderAnnos();
    toast('批注已删除');
  }

  function wireAnnotations() {
    const wrap = $0('#nt-edit-wrap');
    if (!wrap) return;
    registerNoteSelection();
    wrap.addEventListener('mouseover', (e) => {
      const mark = e.target.closest && e.target.closest('mark.nt-anno');
      if (!mark) return;
      const a = (WS.annos || []).find((x) => x.id === mark.dataset.id);
      if (a) showAnnoTip(a, mark);
    });
    wrap.addEventListener('click', (e) => {
      const link = e.target.closest('a.wikilink');
      if (link && link.closest('.nt-read-box')) {
        e.preventDefault();
        const name = link.dataset.note || '';
        const path = resolveNotePath(name);
        if (path) { openNote(path); return; }
        const clean = name.replace(/[\\/:*?"<>|]/g, '').trim();
        if (clean) newNote(clean + '.md');
        return;
      }
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
      lintApplyMarkers();
      return;
    }
    WS.lintTimer = setTimeout(lintRun, 420);
  }
  function lintRun() {
    if (!lintEnabled() || !WS.current || !window.MDLint) {
      lintItems = [];
      renderLint();
      lintApplyMarkers();
      return;
    }
    try {
      lintItems = MDLint.lint(currentMarkdown(), { noteNames: Object.keys(notesNameIndex()) });
    } catch (e) {
      lintItems = [];
      if (window.console) console.error('[mdlint] 检查失败', e);
    }
    renderLint();
    lintApplyMarkers();
  }
  /* 把同一份诊断写进 Monaco：源码模式下的波浪线 + 悬停说明（与 IDE 一致） */
  function lintApplyMarkers() {
    if (!srcEditor || !srcMonacoLib) return;
    const model = srcEditor.getModel();
    if (!model) return;
    const on = lintEnabled();
    const markers = (on ? lintItems : []).map((it) => {
      const col = Math.max(1, it.col || 1);
      return {
        severity: it.severity === 'error' ? 8 : (it.severity === 'info' ? 1 : 4),
        message: it.message + (it.rule ? '  [' + it.rule + ']' : ''),
        source: 'markdown',
        code: it.rule || '',
        startLineNumber: it.line + 1,
        startColumn: col,
        endLineNumber: (it.endLine != null ? it.endLine : it.line) + 1,
        endColumn: Math.max(col + 1, it.endCol || (col + 1)),
      };
    });
    try { srcMonacoLib.editor.setModelMarkers(model, 'markdown', markers); } catch (e) { /* 忽略 */ }
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
    const editable = paneModes().some((m) => m === 'edit' || m === 'source');
    const show = on && !!WS.current && editable && lintItems.length > 0;
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
    if (!sourceVisible()) setView('source');
    const line = Math.max(1, (it.line || 0) + 1);
    const col = Math.max(1, it.col || 1);
    if (srcEditor) {
      const endCol = Math.max(col + 1, it.endCol || (col + 1));
      srcEditor.revealLineInCenter(line);
      srcEditor.setSelection({
        startLineNumber: line, startColumn: col,
        endLineNumber: (it.endLine != null ? it.endLine : it.line) + 1,
        endColumn: endCol,
      });
      srcEditor.focus();
      return;
    }
    const ta = taNode();
    if (!ta) return;
    const lines = ta.value.split('\n');
    let off = 0;
    for (let i = 0; i < it.line; i++) off += (lines[i] || '').length + 1;
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
    setMarkdownBoth(r.text);
    scheduleSave();
    lintRun();
    if (readBoxes().length) renderPreview();
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
    if (dir) revealDir(dir);
    renderTree();
    await openNote(r.path);
    if (paneModes().indexOf('edit') < 0) setView('edit');
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
    const clean = path.replace(/\\/g, '/');
    WS.openDirs.add(clean);
    if (parent) revealDir(parent);
    saveOpenDirs();
    renderTree();
    toast('已创建文件夹');
  }

  async function renamePath(path) {
    if (!path) return;
    if (findRow(path)) { beginRename(path); return; }
    const cur = noteTitleOf(path);
    const name = await appPrompt('重命名为（会自动更新其它笔记里的 [[旧名]] 链接）', cur);
    if (!name || !name.trim() || name.trim() === cur) return;
    await applyRename(path, name.trim());
  }

  function moveNote(path) {
    movePaths([path]);
  }

  function deleteNote(path) {
    deletePaths([path]);
  }

  function deleteFolder(path) {
    deletePaths([path]);
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
    renderPath('');
    $0('#nt-empty').classList.remove('hidden');
    const ed = edNode();
    const ta = taNode();
    const tb = $0('#nb-toolbar');
    if (ed) ed.classList.add('hidden');
    if (ta) ta.classList.add('hidden');
    if (tb) tb.classList.add('hidden');
    $0('#nt-edit-wrap').classList.add('hidden');
    $0('#nt-split-bar').classList.add('hidden');
    $0('#nt-lint-box').classList.add('hidden');
    setSaveState('');
    renderBacklinks([]);
    renderAnnos();
    renderOutline();
    renderDockFiles();
    updateRefNoteBtn();
  }

  /* ---------------- 停靠 / 全屏 ----------------
     默认停靠右侧：对话留在左边，笔记不挡路；全屏只作为「专注」。
     窄屏直接回退全屏，保证编辑区可用。 */
  function notesVisible() {
    const m = $0('#notes-modal');
    return !!m && !m.classList.contains('hidden');
  }

  function isDockedOpen() {
    const m = $0('#notes-modal');
    return !!m && !m.classList.contains('hidden') && m.classList.contains('notes-dock');
  }

  function dockCapable() {
    return !(window.matchMedia && window.matchMedia('(max-width: 900px)').matches);
  }

  function wantDock() {
    return dockCapable() && lsGet(DOCK_KEY, true) !== false;
  }

  function clampDockW(w) {
    const max = Math.max(DOCK_MIN, Math.min(760, window.innerWidth - 360));
    return Math.round(Math.max(DOCK_MIN, Math.min(Number(w) || 420, max)));
  }

  function applyDockWidth() {
    const w = clampDockW(lsGet(DOCKW_KEY, 420));
    document.documentElement.style.setProperty('--note-dock-w', w + 'px');
    return w;
  }

  function syncDockRoot() {
    const m = $0('#notes-modal');
    const on = !!m && !m.classList.contains('hidden') && m.classList.contains('notes-dock');
    document.documentElement.classList.toggle('notes-docked', on);
  }

  function renderDockFiles() {
    const sel = $0('#nt-dock-files');
    if (!sel) return;
    const files = flatten(WS.tree);
    sel.innerHTML = files.length
      ? files.map((f) => '<option value="' + escapeHtml(f.path) + '">' +
          escapeHtml(f.path.replace(/\.md$/i, '')) + '</option>').join('')
      : '<option value="">（暂无笔记）</option>';
    if (WS.current) sel.value = WS.current;
  }

  function applyDock(docked) {
    const m = $0('#notes-modal');
    if (!m) return;
    const on = !!docked && dockCapable();
    m.classList.toggle('notes-dock', on);
    // 停靠态不是模态：否则 focusin 约束会把焦点从聊天输入框拉回笔记
    m.setAttribute('aria-modal', on ? 'false' : 'true');
    $0('#nt-dock-files').classList.toggle('hidden', !on);
    $0('#nt-dock-side').classList.toggle('hidden', !on);
    const body = $0('#notes-modal .notes-body');
    if (body) body.classList.remove('side-open');
    const btn = $0('#nt-dock');
    if (btn) {
      btn.title = on ? '展开全屏' : '停靠到右侧';
      btn.setAttribute('aria-label', btn.title);
    }
    if (on) {
      // 右侧只留一个主面板：笔记停靠会盖住搜索侧栏 / 检查器，先收起来
      if (typeof closeSearchPanel === 'function') closeSearchPanel();
      if (window.uiShell && typeof window.uiShell.closeInspector === 'function') {
        window.uiShell.closeInspector();
      }
    }
    if (on) applyDockWidth();
    else document.documentElement.style.removeProperty('--note-dock-w');
    renderDockFiles();
    syncDockRoot();
  }

  function wireDockResize() {
    const h = $0('#nt-dock-resize');
    if (!h || h._wired) return;
    h._wired = true;
    h.addEventListener('pointerdown', (e) => {
      if (!dockCapable() || !$0('#notes-modal').classList.contains('notes-dock')) return;
      e.preventDefault();
      const move = (ev) => {
        const w = clampDockW(window.innerWidth - ev.clientX);
        document.documentElement.style.setProperty('--note-dock-w', w + 'px');
      };
      const up = () => {
        document.removeEventListener('pointermove', move);
        document.removeEventListener('pointerup', up);
        const cur = parseInt(getComputedStyle(document.documentElement)
          .getPropertyValue('--note-dock-w'), 10);
        if (cur) lsSet(DOCKW_KEY, cur);
      };
      document.addEventListener('pointermove', move);
      document.addEventListener('pointerup', up);
    });
  }

  async function openNotesModal() {
    if (!editor) {
      editor = window.NoteBlocks.mount(edNode(), {
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
        askLink: () => appPrompt('链接地址（https://…）'),
        wikiSearch: (q) => {
          const low = String(q || '').toLowerCase();
          return flatten(WS.tree).map((f) => ({ name: noteTitleOf(f.path), path: f.path }))
            .filter((it) => !low || it.name.toLowerCase().indexOf(low) >= 0)
            .slice(0, 8);
        },
      });
    }
    restoreWidths();
    if (WS.split && window.matchMedia && window.matchMedia('(max-width: 1160px)').matches) {
      WS.split = false;                 // 窄屏只保留单栏，分栏按钮也已隐藏
      saveViewPrefs();
    }
    showWithAnim($0('#notes-modal'));
    // 弹窗从 display:none 恢复后 Monaco 需要一次 layout（automaticLayout 有时要等下一帧）
    if (srcEditor) setTimeout(() => { try { srcEditor.layout(); } catch (e) { /* 忽略 */ } }, 60);
    applyDock(wantDock());
    syncDockRoot();
    await loadNotes();
    if (!WS.current) renderDockFiles();
    switchTab(WS.tab || 'outline');
    const files = flatten(WS.tree);
    if (WS.current) await openNote(WS.current);
    else if (files.length) await openNote(files[0].path);
    else showNoNote();
    updateRefNoteBtn();
    if (paneModes().indexOf('edit') >= 0 && editor) {
      setTimeout(() => editor.focusBlock(0, true), 60);
    }
  }

  async function closeNotesModal() {
    closeQuick();
    closeCtx();
    closePicker(null);
    const sp = $0('#nt-sort-pop');
    if (sp) sp.classList.add('hidden');
    hideAnnoPop();
    document.documentElement.classList.remove('notes-docked');
    await saveNoteNow();
    const refBtn = $0('#btn-ref-note');
    if (refBtn) refBtn.classList.add('hidden');
    hideWithAnim($0('#notes-modal'), () => {
      if (WS.graphNet) { try { WS.graphNet.destroy(); } catch (e) { /* 忽略 */ } WS.graphNet = null; }
    });
  }

  /* 兼容旧调用：edit → 编辑，其它 → 阅读 */
  function setNoteMode(mode) {
    setView(mode === 'edit' ? 'edit' : 'reading');
  }

  /* ---------------- 对话 ⇄ 笔记：显式捕获 / 来源回链 ----------------
     只有用户点「记入笔记 / 参考整篇」才发生数据流动：
     对话内容被显式追加到笔记；笔记内容被显式加入本轮引用。
     两边都不是实时同步，删除任何一边也不会级联删除另一边。 */
  let capOutside = null;
  let capSyncTimer = 0;

  function closeCapturePop() {
    const el = document.getElementById('nt-cap-pop');
    if (el) el.remove();
    if (capOutside) {
      document.removeEventListener('pointerdown', capOutside, true);
      capOutside = null;
    }
  }

  function capturePop(text, meta) {
    text = String(text || '').trim();
    if (!text) return toast('先选中一段内容', 'error');
    if (text.length > 12000) text = text.slice(0, 12000);
    meta = meta || {};
    closeCapturePop();

    const el = document.createElement('div');
    el.id = 'nt-cap-pop';
    el.className = 'nt-cap';
    const current = WS.current;
    el.innerHTML =
      '<div class="nt-cap-head">' + icon('note') + '记入笔记</div>' +
      '<textarea class="nt-cap-thought" placeholder="我的想法（可选）"></textarea>' +
      '<div class="nt-cap-acts">' +
        (current ? '<button type="button" data-t="current">' + icon('note') +
          '当前笔记《' + escapeHtml(noteTitle()) + '》</button>' : '') +
        '<button type="button" data-t="inbox">' + icon('folder') + '今日学习收件箱</button>' +
        '<button type="button" data-t="new">' + icon('file-plus') + '新建笔记…</button>' +
      '</div>' +
      '<div class="nt-cap-hint">只在你点选后写入一次；笔记与对话各自独立。</div>';
    document.body.appendChild(el);

    const r = meta.rect;
    const w = el.offsetWidth || 300;
    const h = el.offsetHeight || 220;
    let left = r ? r.right - w : window.innerWidth - w - 20;
    let top = r ? r.bottom + 8 : window.innerHeight - h - 80;
    left = Math.max(8, Math.min(left, window.innerWidth - w - 8));
    top = Math.max(8, Math.min(top, window.innerHeight - h - 8));
    el.style.left = left + 'px';
    el.style.top = top + 'px';

    capOutside = (ev) => {
      if (el.contains(ev.target)) return;
      // 从旁注卡片点「记入笔记」打开的浮层：回到卡片里看回答时不要顺手关掉它
      if (ev.target.closest && ev.target.closest('.askkit')) return;
      closeCapturePop();
    };
    document.addEventListener('pointerdown', capOutside, true);

    el.querySelectorAll('[data-t]').forEach((b) => {
      b.onclick = () => {
        const target = b.dataset.t;
        const thought = el.querySelector('.nt-cap-thought').value.trim();
        if (target === 'new') {
          closeCapturePop();
          appPrompt('新笔记标题').then((t) => {
            if (t && t.trim()) doCapture(text, meta, 'new', '', t.trim(), thought);
          });
          return;
        }
        doCapture(text, meta, target, target === 'current' ? current : '', '', thought);
      };
    });
  }

  async function doCapture(text, meta, target, path, title, thought) {
    if (target === 'current' && WS.current) {
      const saved = await saveNoteNow();
      if (!saved) return;          // 保存失败就别再往文件里追加，免得覆盖未保存内容
    }
    const r = await api('/api/notes/capture', {
      target: target, path: path || '', title: title || '',
      text: text, note: thought || '',
      session_id: meta.session_id || '', session_title: meta.session_title || '',
      msg_index: meta.msg_index, role: meta.role || '',
    });
    if (!r || !r.ok) {
      toast('写入失败：' + ((r && r.error) || ''), 'error');
      return;
    }
    closeCapturePop();
    if (notesVisible()) {
      await loadNotes();
      if (target === 'current' && WS.current === r.path) await openNote(r.path);
    }
    WS.captures = {};
    if (meta.session_id) syncCaptures(meta.session_id, true);
    toast(r.deduped ? '这条已经记过了' : '已记入《' + (r.title || '笔记') + '》');
  }

  function paintCaptures(sid, items) {
    const chat = document.getElementById('chat');
    if (!chat) return;
    chat.querySelectorAll('.msg-note-chip').forEach((x) => x.remove());
    const byMsg = {};
    (items || []).forEach((it) => {
      const i = Number(it.msg_index);
      if (!Number.isFinite(i)) return;
      (byMsg[i] = byMsg[i] || []).push(it);
    });
    Object.keys(byMsg).forEach((k) => {
      const row = chat.querySelector('.msg[data-idx="' + k + '"]');
      const acts = row && row.querySelector('.msg-actions');
      if (!acts) return;
      const list = byMsg[k];
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'msg-note-chip';
      chip.innerHTML = icon('note') + '<span>已记入笔记' +
        (list.length > 1 ? ' · ' + list.length : '') + '</span>';
      chip.title = list.map((x) => '《' + (x.note_title || x.note_path) + '》').join('\n');
      chip.onclick = () => {
        if (list[0] && window.notesOpen) window.notesOpen(list[0].note_path);
      };
      acts.insertBefore(chip, acts.firstChild);
    });
  }

  async function syncCaptures(sid, force) {
    sid = String(sid || '');
    if (!sid) return;
    const cached = WS.captures[sid];
    if (!force && cached && Date.now() - cached.at < 15000) {
      paintCaptures(sid, cached.items);
      return;
    }
    const r = await api('/api/notes/captures', { session_id: sid });
    if (!r || !r.ok) return;
    WS.captures[sid] = { at: Date.now(), items: r.items || [] };
    const now = String((state.sessions && state.sessions.current) || '');
    if (now === sid) paintCaptures(sid, r.items || []);
  }

  function scheduleCaptureSync() {
    clearTimeout(capSyncTimer);
    capSyncTimer = setTimeout(() => {
      const sid = String((state.sessions && state.sessions.current) || '');
      if (sid) syncCaptures(sid);
    }, 300);
  }

  function updateRefNoteBtn() {
    const btn = $0('#btn-ref-note');
    if (!btn) return;
    const show = notesVisible() && !!WS.current;
    btn.classList.toggle('hidden', !show);
    if (show) btn.title = '把《' + noteTitle() + '》整篇作为参考，随下一条消息一起发送';
  }

  function quoteWholeNote() {
    if (!WS.current) return toast('还没有打开笔记', 'error');
    if (typeof addQuote !== 'function') return toast('当前界面不支持引用', 'error');
    const md = String(currentMarkdown() || '').trim();
    if (!md) return toast('当前笔记是空的', 'error');
    addQuote(md, '笔记《' + noteTitle() + '》');
    toast(md.length > 4000 ? '已加入整篇笔记（超出 4000 字的部分已截断）' : '已加入整篇笔记');
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
    $0('#nt-dock').onclick = () => {
      const on = $0('#notes-modal').classList.contains('notes-dock');
      lsSet(DOCK_KEY, !on);
      applyDock(!on);
      syncDockRoot();
    };
    $0('#nt-dock-side').onclick = (e) => {
      e.stopPropagation();
      const body = $0('#notes-modal .notes-body');
      if (body) body.classList.toggle('side-open');
    };
    $0('#nt-dock-files').onchange = (e) => {
      if (e.target.value) openNote(e.target.value);
    };
    wireDockResize();
    window.addEventListener('resize', () => {
      const m = $0('#notes-modal');
      if (!m || m.classList.contains('hidden')) return;
      if (!dockCapable()) applyDock(false);
      else if (m.classList.contains('notes-dock')) applyDockWidth();
      else if (wantDock()) applyDock(true);
    });
    // 来源块回链：/__gm/chat?session=…&msg=… → 定位到对应对话消息
    $0('#notes-modal').addEventListener('click', (e) => {
      const a = e.target.closest && e.target.closest('a[href^="/__gm/chat"]');
      if (!a) return;
      e.preventDefault();
      e.stopPropagation();
      const u = new URL(a.getAttribute('href'), location.origin);
      window.openChatAtMessage(u.searchParams.get('session') || '',
        Number(u.searchParams.get('msg')));
    });
    const refBtn = $0('#btn-ref-note');
    if (refBtn) refBtn.onclick = () => quoteWholeNote();

    $$0('.nt-view-switch button').forEach((btn) => {
      btn.onclick = () => {
        const mode = btn.id.replace('nt-view-', '');
        if (mode === 'split') toggleSplit();
        else setView(mode);
      };
    });
    wireTreeEvents();
    $0('#nt-sort').onclick = (e) => {
      e.stopPropagation();
      $0('#nt-sort-pop').classList.toggle('hidden');
    };
    $0('#nt-sort-pop').onclick = (e) => {
      const b = e.target.closest('button[data-sort]');
      if (!b) return;
      WS.sort = b.dataset.sort === 'mtime' ? 'mtime' : 'name';
      lsSet(SORT_KEY, WS.sort);
      $0('#nt-sort-pop').classList.add('hidden');
      renderTree();
    };
    document.addEventListener('click', (e) => {
      if (!e.target.closest('#nt-sort') && !e.target.closest('#nt-sort-pop')) {
        $0('#nt-sort-pop').classList.add('hidden');
      }
    });
    $0('#nt-collapse-all').onclick = () => {
      WS.openDirs.clear();
      saveOpenDirs();
      renderTree();
    };
    const sl = $0('#nt-split-left');
    const sr = $0('#nt-split-right');
    if (sl) sl.onchange = () => setSplitSide('left', sl.value);
    if (sr) sr.onchange = () => setSplitSide('right', sr.value);
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

    const ta = taNode();
    if (ta) {
      ta.addEventListener('input', onSourceInput);
      ta.addEventListener('blur', () => { if (sourceVisible() && WS.dirty) saveNoteNow(); });
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

    // 预览里的双链由 wireAnnotations 在 #nt-edit-wrap 上统一代理

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
        setView(paneModes().indexOf('source') >= 0 ? 'edit' : 'source');
        return;
      }
      if (e.key === 'Escape') {
        const pick = $0('#nt-pick');
        if (pick && !pick.classList.contains('hidden')) { closePicker(null); return; }
        const sortPop = $0('#nt-sort-pop');
        if (sortPop && !sortPop.classList.contains('hidden')) { sortPop.classList.add('hidden'); return; }
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
      { k: 'new-note', label: '新建笔记' },
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
        if (k === 'new-note') newNote();
        else if (k === 'new-folder') newFolder('');
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
  window.notesCapture = capturePop;
  window.notesSyncCaptures = syncCaptures;
  window.notesScheduleCaptures = scheduleCaptureSync;

  /* 来源块回链：停靠时留在笔记里，全屏时先收起笔记再定位消息。 */
  window.openChatAtMessage = async function (sid, msgIndex) {
    if (!sid) return;
    if (!isDockedOpen()) {
      try { await closeNotesModal(); } catch (e) { /* 忽略 */ }
    }
    if (typeof window.selectSession === 'function') await window.selectSession(sid);
    const n = Number(msgIndex);
    if (Number.isFinite(n) && typeof window.ssFocusMessage === 'function') {
      await window.ssFocusMessage(n, '');
    }
  };
  window.notesToggleView = () => {
    const hasSource = paneModes().indexOf('source') >= 0;
    setView(hasSource ? 'edit' : 'source');
    return WS.view;
  };
})();
