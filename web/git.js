/* 源代码管理（Git）侧栏 + 只读 diff。
 *
 * 只做 IDE 面板里的事：拉状态、列改动、暂存/取消暂存、提交、看某个文件的 diff。
 * 与「AI 变更审阅」刻意分开：审阅队列基于 undo token，这里基于 Git 索引，
 * 两套状态混在一起会互相覆盖。
 */
let gitState = { files: [], repo: false, branch: '', reason: '',
                 history: [], ops: [], tab: 'changes',
                 remotes: [], upstream: '', graph: false };
let gitDiffEditor = null;
let gitDiffModel = null;
let gitDiffDecos = [];
const GIT_GRAPH_KEY = 'ide_git_graph_v1';

function gitGraphPref() {
  try { return localStorage.getItem(GIT_GRAPH_KEY) === '1'; } catch (e) { return false; }
}

function gitEl(id) { return document.getElementById(id); }

function gitSplitPath(rel) {
  const i = rel.lastIndexOf('/');
  return i < 0 ? ['', rel] : [rel.slice(0, i + 1), rel.slice(i + 1)];
}

function gitStatusClass(f) {
  if (f.untracked) return 'u';
  const s = String(f.status || '');
  if (s === 'D') return 'd';
  if (s === 'A') return 'a';
  if (s === 'U') return 'd';
  return 'm';
}

function gitStatusText(f) {
  if (f.untracked) return 'U';
  return String(f.status || 'M');
}

function gitRow(f, staged) {
  const [dir, name] = gitSplitPath(f.path);
  const act = staged ? 'unstage' : 'stage';
  const title = staged ? '取消暂存' : '暂存';
  const extra = staged
    ? '<button class="ide-git-act" data-act="discard" title="丢弃改动（暂存区 + 工作区）">↺</button>'
    : (f.untracked
      ? '<button class="ide-git-act" data-act="delete" title="删除未跟踪文件">✕</button>'
      : '<button class="ide-git-act" data-act="discard" title="丢弃工作区改动">↺</button>' +
        '<button class="ide-git-act" data-act="delete" title="删除文件">✕</button>');
  return '<div class="ide-git-row" data-path="' + escapeHtml(f.path) +
    '" data-staged="' + (staged ? '1' : '0') + '">' +
    '<input type="checkbox" class="ide-git-check" data-path="' + escapeHtml(f.path) + '">' +
    '<span class="st ' + gitStatusClass(f) + '">' + gitStatusText(f) + '</span>' +
    '<span class="nm"><span class="dir">' + escapeHtml(dir) + '</span>' + escapeHtml(name) + '</span>' +
    '<button class="ide-git-act" data-act="' + act + '" title="' + title + '">' +
    (staged ? '－' : '＋') + '</button>' + extra + '</div>';
}

function gitRender() {
  const box = gitEl('ide-git-files');
  const br = gitEl('ide-git-branch');
  const badge = gitEl('ide-git-badge');
  if (!box) return;
  const files = gitState.files || [];
  if (badge) {
    badge.textContent = String(files.length);
    badge.classList.toggle('hidden', !files.length);
  }
  if (br) {
    if (!gitState.repo) {
      br.textContent = gitState.reason || '当前沙箱不是 Git 仓库';
    } else {
      let s = '⑂ ' + (gitState.branch || '—');
      if (gitState.ahead) s += '  ↑' + gitState.ahead;
      if (gitState.behind) s += '  ↓' + gitState.behind;
      br.textContent = s;
    }
  }
  if (!gitState.repo) {
    box.innerHTML = '<div class="ide-git-empty">在终端里执行 <code>git init</code> 后点右上角刷新。</div>';
    return;
  }
  const staged = files.filter((f) => f.staged && !f.untracked);
  const changed = files.filter((f) => f.untracked || f.unstaged);
  let html = '';
  if (files.length) {
    html += '<div class="ide-git-bulk">' +
      '<label><input type="checkbox" id="ide-git-check-all"> 全选</label>' +
      '<button data-bulk="stage" title="暂存选中的文件">暂存</button>' +
      '<button data-bulk="unstage" title="取消暂存选中的文件">取消暂存</button>' +
      '<button data-bulk="discard" title="丢弃选中文件的改动">丢弃改动</button>' +
      '<button data-bulk="delete" title="删除选中的已跟踪文件">删除文件</button>' +
      '<button data-bulk="clean" title="清理所有未跟踪文件（不可恢复）">清理未跟踪</button>' +
      '</div>';
  }
  if (staged.length) {
    html += '<div class="ide-git-group"><label><input type="checkbox" ' +
      'class="ide-git-gcheck" data-group="staged"> 已暂存（' + staged.length + '）</label></div>' +
      staged.map((f) => gitRow(f, true)).join('');
  }
  if (changed.length) {
    html += '<div class="ide-git-group"><label><input type="checkbox" ' +
      'class="ide-git-gcheck" data-group="changed"> 更改（' + changed.length + '）</label></div>' +
      changed.map((f) => gitRow(f, false)).join('');
  }
  if (!staged.length && !changed.length) {
    html += '<div class="ide-git-empty">工作区干净，没有待提交的改动。</div>';
  }
  box.innerHTML = html;

  box.querySelectorAll('.ide-git-row').forEach((row) => {
    row.onclick = (e) => {
      if (e.target.closest('.ide-git-act') || e.target.closest('.ide-git-check')) return;
      gitOpenDiff(row.dataset.path, row.dataset.staged === '1');
    };
  });
  box.querySelectorAll('.ide-git-act').forEach((btn) => {
    btn.onclick = (e) => {
      e.stopPropagation();
      const row = btn.closest('.ide-git-row');
      if (!row) return;
      const act = btn.dataset.act;
      if (act === 'stage' || act === 'unstage') {
        return gitSetStaged(row.dataset.path, act === 'stage');
      }
      if (act === 'discard') return gitDiscard([row.dataset.path], row.dataset.staged === '1');
      if (act === 'delete') return gitDelete([row.dataset.path]);
      return null;
    };
  });
  box.querySelectorAll('.ide-git-check').forEach((el) => {
    el.onclick = (e) => e.stopPropagation();
  });
  const all = gitEl('ide-git-check-all');
  if (all) {
    all.onclick = () => {
      box.querySelectorAll('.ide-git-check').forEach((el) => { el.checked = all.checked; });
    };
  }
  box.querySelectorAll('.ide-git-gcheck').forEach((el) => {
    el.onclick = () => {
      const group = el.dataset.group;
      box.querySelectorAll('.ide-git-row').forEach((row) => {
        const isStaged = row.dataset.staged === '1';
        if ((group === 'staged') === isStaged) {
          const c = row.querySelector('.ide-git-check');
          if (c) c.checked = el.checked;
        }
      });
    };
  });
  box.querySelectorAll('[data-bulk]').forEach((btn) => {
    btn.onclick = () => gitBulk(btn.dataset.bulk);
  });
}

async function gitRefresh() {
  if (state.uiMode !== 'ide') return;
  let r;
  try {
    r = await api('/api/git/status', { session_id: state.sessions.current });
  } catch (e) {
    gitState = { files: [], repo: false, branch: '', reason: '读取 Git 状态失败：' + e.message };
    gitRender();
    gitRenderSyncState();
    return;
  }
  gitState = {
    files: (r && r.files) || [],
    repo: !!(r && r.repo),
    branch: (r && r.branch) || '',
    ahead: (r && r.ahead) || 0,
    behind: (r && r.behind) || 0,
    reason: (r && r.reason) || '',
    remotes: (r && r.remotes) || [],
    upstream: (r && r.upstream) || '',
    history: gitState.history || [],
    ops: gitState.ops || [],
    tab: gitState.tab || 'changes',
    graph: gitGraphPref(),
  };
  gitRender();
  gitRenderSyncState();
  await gitLoadHistory();
  await gitLoadOps();
  // 文件树的装饰与面板同源：暂存 / 丢弃 / 提交之后树里的字母与颜色也一起变
  if (typeof ideRefreshGitMarks === 'function') ideRefreshGitMarks();
}

/* 远端同步按钮：没有远程时置灰并给出原因（点一下也能看到提示） */
function gitRenderSyncState() {
  const has = !!(gitState.repo && (gitState.remotes || []).length);
  [['ide-git-fetch', '获取远端更新（fetch）'], ['ide-git-pull', '拉取并合并（pull）'],
    ['ide-git-push', '推送（push）']].forEach(([id, title]) => {
    const b = gitEl(id);
    if (!b) return;
    b.disabled = !has;
    b.title = has ? title : '当前仓库没有配置远程（bash: git remote add origin …）';
  });
}

async function gitSync(action) {
  if (!gitState.repo) return toast('当前沙箱不是 Git 仓库');
  if (!(gitState.remotes || []).length) {
    return toast('仓库还没有配置远程：git remote add origin <url>');
  }
  if (action === 'pull'
      && !window.confirm('拉取并合并远端改动？有冲突时需要手动解决。')) return;
  const ids = { fetch: 'ide-git-fetch', pull: 'ide-git-pull', push: 'ide-git-push' };
  const btn = gitEl(ids[action]);
  if (btn) btn.disabled = true;
  const body = { session_id: state.sessions.current, action: action };
  if (action === 'pull') body.confirm = 'pull';
  try {
    const r = await api('/api/git/sync', body);
    const out = String((r && r.output) || '').split('\n').filter(Boolean).pop() || '';
    toast((action === 'fetch' ? '已获取远端信息' : action === 'pull' ? '已拉取' : '已推送')
      + (out ? '：' + out.slice(0, 80) : ''), 'success');
  } catch (e) {
    toast((action === 'fetch' ? 'fetch 失败：' : action === 'pull' ? 'pull 失败：'
      : 'push 失败：') + e.message);
  } finally {
    await gitRefresh();
  }
}

/* ---------- 历史 / AI 操作 ---------- */
async function gitLoadHistory() {
  const graph = gitGraphPref();
  try {
    const r = await api(graph ? '/api/git/graph' : '/api/git/log', {
      session_id: state.sessions.current, limit: graph ? 60 : 30,
    });
    gitState.history = (r && r.items) || [];
    gitState.historyError = (r && !r.ok && r.error) || '';
  } catch (e) {
    gitState.history = [];
    gitState.historyError = e.message || '';
  }
  const hint = gitEl('ide-git-histhint');
  if (hint) {
    hint.textContent = graph ? '全部分支 · 最近 60 条'
      : (gitState.historyError ? '读取失败' : '当前分支 · 最近 30 条');
  }
  gitRenderHistory();
}

async function gitLoadOps() {
  try {
    const r = await api('/api/git/ops', { session_id: state.sessions.current });
    gitState.ops = (r && r.items) || [];
  } catch (e) {
    gitState.ops = [];
  }
  gitRenderOps();
}

function gitRenderHistory() {
  const box = gitEl('ide-git-history');
  if (!box) return;
  box.classList.toggle('graph', gitGraphPref());
  const list = gitState.history || [];
  if (!list.length) {
    box.innerHTML = '<div class="ide-git-empty">没有提交历史（沙箱不在 Git 仓库里时会为空）。</div>';
    return;
  }
  box.innerHTML = list.map((c) => {
    const graph = (c.graph != null)
      ? '<span class="ide-git-graph">' + escapeHtml(c.graph || '') + '</span>' : '';
    const refs = c.refs
      ? '<span class="ide-git-refs" title="' + escapeHtml(c.refs) + '">' +
        escapeHtml(c.refs) + '</span>' : '';
    return '<div class="ide-git-commit-row" data-hash="' + escapeHtml(c.hash) + '">' +
      graph +
      '<span class="h">' + escapeHtml(c.hash) + '</span>' +
      '<span class="s" title="' + escapeHtml((c.subject || '') + ' · ' + (c.author || '')) + '">' +
        refs + escapeHtml(c.subject || '') + '</span>' +
      '<span class="d">' + escapeHtml(c.date || '') + '</span>' +
      '<span class="acts">' +
        '<button class="ide-git-hact" data-hact="review" data-hash="' +
          escapeHtml(c.hash) + '" title="AI 审查这个提交">审查</button>' +
        '<button class="ide-git-hact" data-hact="revert" data-hash="' +
          escapeHtml(c.hash) + '" title="撤回此提交（生成反向提交，不改写历史）">撤回</button>' +
        '<button class="ide-git-hact" data-hact="reset" data-hash="' +
          escapeHtml(c.hash) + '" title="回退到此提交（之后的改动保留在工作区）">回退</button>' +
      '</span>' +
    '</div>';
  }).join('');
  box.querySelectorAll('.ide-git-commit-row').forEach((row) => {
    row.addEventListener('click', () => gitOpenCommit(row.dataset.hash));
  });
  box.querySelectorAll('.ide-git-hact').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      if (btn.dataset.hact === 'review') {
        gitReview({ mode: 'commit', rev: btn.dataset.hash }, '提交 ' + btn.dataset.hash);
      } else if (btn.dataset.hact === 'revert') gitRevert(btn.dataset.hash);
      else if (btn.dataset.hact === 'reset') gitResetTo(btn.dataset.hash);
    });
  });
}

async function gitRevert(hash) {
  if (!hash) return;
  if (!window.confirm('撤回提交 ' + hash + '？会生成一个新的反向提交，不改写历史。')) return;
  try {
    await api('/api/git/revert', { session_id: state.sessions.current, rev: hash });
  } catch (e) {
    return toast('撤回失败：' + e.message);
  }
  toast('已撤回 ' + hash, 'success');
  await gitRefresh();
}

async function gitResetTo(hash) {
  if (!hash) return;
  if (!window.confirm('回退到 ' + hash + '？之后的提交会从当前分支移除，改动保留在工作区（soft）。')) return;
  try {
    await api('/api/git/reset', {
      session_id: state.sessions.current, rev: hash, mode: 'soft',
    });
  } catch (e) {
    return toast('回退失败：' + e.message);
  }
  toast('已回退到 ' + hash, 'success');
  await gitRefresh();
}

function gitRenderOps() {
  const box = gitEl('ide-git-ops');
  if (!box) return;
  const list = gitState.ops || [];
  if (!list.length) {
    box.innerHTML = '<div class="ide-git-empty">本会话里 AI 还没有执行过 Git 命令。</div>';
    return;
  }
  box.innerHTML = list.map((o) => {
    const cmd = 'git ' + (o.sub || '');
    const res = (o.ok ? '' : '失败 · ') + (o.result || '');
    return '<div class="ide-git-op" title="' + escapeHtml((o.argv || []).join(' ')) + '">' +
      '<span class="sub">' + escapeHtml(cmd) + '</span>' +
      (o.risk ? '<span class="risk">高危</span>' : '') +
      '<span class="res">' + escapeHtml(res) + '</span></div>';
  }).join('');
}

function gitSetTab(tab) {
  const t = (tab === 'history' || tab === 'ops') ? tab : 'changes';
  gitState.tab = t;
  document.querySelectorAll('.ide-git-tab').forEach((b) => {
    b.classList.toggle('active', b.dataset.gitTab === t);
  });
  ['changes', 'history', 'ops'].forEach((k) => {
    const pane = gitEl('ide-git-pane-' + k);
    if (pane) pane.classList.toggle('hidden', k !== t);
  });
}

function gitEnsureEditor(M, host) {
  if (!gitDiffEditor) {
    gitDiffEditor = M.editor.create(host, {
      automaticLayout: true,
      readOnly: true,
      renderLineHighlight: 'none',
      lineNumbersMinChars: 5,
      glyphMargin: false,
      folding: false,
      minimap: { enabled: false },
      scrollBeyondLastLine: false,
      renderOverviewRuler: false,
      wordWrap: 'off',
      lineNumbers: () => '',
    });
  }
  return gitDiffEditor;
}

function gitPatchRows(patch) {
  const rows = [];
  String(patch || '').split('\n').forEach((ln) => {
    let cls = 'dv-ctx-new';
    if (ln.startsWith('+++') || ln.startsWith('---') || ln.startsWith('diff --git') ||
        ln.startsWith('index ') || ln.startsWith('new file') || ln.startsWith('deleted file') ||
        ln.startsWith('old mode') || ln.startsWith('new mode') ||
        ln.startsWith('@@') || ln.startsWith('Binary files')) {
      cls = 'dv-gap';
    } else if (ln.startsWith('+')) {
      cls = 'dv-ins';
    } else if (ln.startsWith('-')) {
      cls = 'dv-del';
    }
    rows.push({ text: ln, cls: cls });
  });
  return rows;
}

async function gitShowPatch(title, patch, statText) {
  const host = gitEl('ide-git-diff-editor');
  if (!host) return;
  const M = state.ideMonaco || await loadMonaco();
  if (!M) return toast('编辑器未加载，无法显示 diff');
  const ed = gitEnsureEditor(M, host);
  M.editor.setTheme(ideMonacoThemeName());
  const rows = gitPatchRows(patch);
  if (!rows.length) rows.push({ text: '这个提交没有可显示的文本改动', cls: 'dv-gap' });
  if (gitDiffModel) { gitDiffModel.dispose(); gitDiffModel = null; }
  gitDiffDecos = [];
  const model = M.editor.createModel(rows.map((r) => r.text).join('\n'), 'plaintext',
    M.Uri.from({ scheme: 'gitcommit', path: '/' + String(title || 'commit').slice(0, 60) }));
  gitDiffModel = model;
  ed.setModel(model);
  ed.updateOptions({ lineNumbers: () => '' });
  const decos = [];
  rows.forEach((row, i) => {
    if (!row.cls) return;
    decos.push({
      range: { startLineNumber: i + 1, startColumn: 1, endLineNumber: i + 1, endColumn: 1 },
      options: { isWholeLine: true, className: row.cls },
    });
  });
  gitDiffDecos = ed.deltaDecorations(gitDiffDecos, decos);
  const fileEl = gitEl('ide-git-diff-file');
  if (fileEl) fileEl.textContent = title || '';
  const statEl = gitEl('ide-git-diff-stat');
  if (statEl) statEl.textContent = statText || '';
  ideShowView('gitdiff');
  ed.setScrollTop(0);
}

async function gitOpenCommit(rev) {
  let r;
  try {
    r = await api('/api/git/show', { session_id: state.sessions.current, rev: rev });
  } catch (e) {
    return toast('读取提交失败：' + e.message);
  }
  if (!r || !r.ok) return toast((r && r.error) || '读取提交失败');
  await gitShowPatch((r.short || rev) + ' · ' + (r.subject || ''), r.patch || '',
                     '+' + (r.adds || 0) + '  −' + (r.dels || 0));
}

async function gitSetStaged(path, on) {
  try {
    await api(on ? '/api/git/stage' : '/api/git/unstage', {
      session_id: state.sessions.current, path: path,
    });
  } catch (e) {
    return toast('操作失败：' + e.message);
  }
  await gitRefresh();
}

function gitSelected() {
  return Array.from(document.querySelectorAll('#ide-git-files .ide-git-check:checked'))
    .map((el) => el.dataset.path);
}

async function gitBulk(action) {
  if (action === 'clean') return gitClean();
  const paths = gitSelected();
  if (!paths.length) return toast('先勾选要操作的文件');
  if (action === 'stage' || action === 'unstage') {
    try {
      await api(action === 'stage' ? '/api/git/stage' : '/api/git/unstage', {
        session_id: state.sessions.current, paths: paths,
      });
    } catch (e) {
      return toast((action === 'stage' ? '暂存失败：' : '取消暂存失败：') + e.message);
    }
    return gitRefresh();
  }
  if (action === 'discard') {
    const staged = Array.from(document.querySelectorAll('#ide-git-files .ide-git-row'))
      .some((row) => paths.includes(row.dataset.path) && row.dataset.staged === '1');
    return gitDiscard(paths, staged);
  }
  if (action === 'delete') return gitDelete(paths);
  return null;
}

async function gitDiscard(paths, staged) {
  if (!paths || !paths.length) return;
  const what = staged ? '丢弃这些文件的暂存与工作区改动' : '丢弃这些文件的工作区改动';
  if (!window.confirm(what + '（共 ' + paths.length + ' 个）？此操作不可恢复。')) return;
  try {
    if (staged) {
      await api('/api/git/unstage', { session_id: state.sessions.current, paths: paths });
    }
    await api('/api/git/discard', { session_id: state.sessions.current, paths: paths });
  } catch (e) {
    return toast('丢弃失败：' + e.message);
  }
  toast('已丢弃 ' + paths.length + ' 个文件的改动', 'success');
  await gitRefresh();
}

async function gitDelete(paths) {
  if (!paths || !paths.length) return;
  // 未跟踪文件 git rm 会报「pathspec did not match」，只能走 clean 分支
  const rows = Array.from(document.querySelectorAll('#ide-git-files .ide-git-row'));
  const untracked = paths.filter((p) => {
    const row = rows.find((r) => r.dataset.path === p);
    return row && !!row.querySelector('.st.u');
  });
  const tracked = paths.filter((p) => !untracked.includes(p));
  if (!window.confirm('删除 ' + paths.length + ' 个文件？'
    + '（未跟踪文件直接删除，已跟踪文件进入暂存区）')) return;
  try {
    if (untracked.length) {
      await api('/api/git/discard', {
        session_id: state.sessions.current, paths: untracked,
      });
    }
    if (tracked.length) {
      await api('/api/git/rm', { session_id: state.sessions.current, paths: tracked });
    }
  } catch (e) {
    return toast('删除失败：' + e.message);
  }
  toast('已删除 ' + paths.length + ' 个文件', 'success');
  await gitRefresh();
}

async function gitClean() {
  if (!window.confirm('清理所有未跟踪文件 / 目录？此操作不可恢复。')) return;
  try {
    const r = await api('/api/git/clean', {
      session_id: state.sessions.current, confirm: true,
    });
    const out = (r && r.output) ? r.output.split('\n')[0].slice(0, 80) : '';
    toast('已清理未跟踪文件' + (out ? '：' + out : ''), 'success');
  } catch (e) {
    return toast('清理失败：' + e.message);
  }
  await gitRefresh();
}

async function gitStageAll() {
  const paths = (gitState.files || []).filter((f) => f.untracked || f.unstaged).map((f) => f.path);
  if (!paths.length) return toast('没有需要暂存的改动');
  try {
    await api('/api/git/stage', { session_id: state.sessions.current, paths: paths });
  } catch (e) {
    return toast('暂存失败：' + e.message);
  }
  await gitRefresh();
}

async function gitCommit() {
  const box = gitEl('ide-git-msg');
  const msg = (box && box.value || '').trim();
  if (!msg) return toast('先写提交信息');
  const amend = !!(gitEl('ide-git-amend') || {}).checked;
  const staged = (gitState.files || []).filter((f) => f.staged && !f.untracked);
  if (!amend && !staged.length) {
    return toast('没有已暂存的改动（点文件右边的 ＋，或「全部暂存」）');
  }
  const btn = gitEl('ide-git-commit-btn');
  if (btn) btn.disabled = true;
  try {
    const r = await api(amend ? '/api/git/amend' : '/api/git/commit', {
      session_id: state.sessions.current, message: msg,
    });
    const hash = (r && r.hash) || '';
    toast(amend ? '已修改上次提交 ' + hash : '已提交 ' + hash, 'success');
    if (box) box.value = '';
    const cb = gitEl('ide-git-amend');
    if (cb) cb.checked = false;
  } catch (e) {
    toast((amend ? '修改提交失败：' : '提交失败：') + e.message);
  } finally {
    if (btn) btn.disabled = false;
    await gitRefresh();
  }
}

/* ---------- AI 生成提交信息 ----------
   和 Trae 一致：读暂存区（没有就工作区）的 diff + 最近提交风格，
   只填进输入框，提交与否仍然由用户决定。 */
async function gitGenMessage() {
  const btn = gitEl('ide-git-ai-msg');
  const box = gitEl('ide-git-msg');
  if (!btn || btn.disabled) return;
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = '生成中…';
  try {
    const r = await api('/api/git/message', { session_id: state.sessions.current });
    if (!r || !r.ok) throw new Error((r && r.error) || '生成失败');
    if (box) {
      box.value = r.message || '';
      box.focus();
    }
    toast(r.staged ? '已根据暂存区生成提交信息' : '已根据工作区生成提交信息', 'success');
  } catch (e) {
    toast('生成提交信息失败：' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = label;
  }
}

/* ---------- AI 审查（Git 范围） ----------
   未提交改动 / 单个提交 / 当前分支与某分支的差异；结果复用 AI 摘要的渲染。 */
let gitReviewLast = null;

async function gitReview(extra, title) {
  const box = gitEl('ide-git-review-body');
  if (!box) return;
  gitReviewLast = { extra: extra || {}, title: title || '' };
  const scopeEl = gitEl('ide-git-review-scope');
  if (scopeEl) scopeEl.textContent = title || '';
  ideShowView('gitreview');
  box.innerHTML = '<div class="ide-review-ai-load">正在生成 AI 审查…</div>';
  const body = Object.assign({ session_id: state.sessions.current, scope: 'git' },
    extra || {});
  try {
    const r = await api('/api/ide/review', body);
    if (!r || !r.ok) throw new Error((r && r.error) || '生成失败');
    const m = (typeof ideReviewAiMarkdown === 'function')
      ? ideReviewAiMarkdown(r) : { html: escapeHtml(r.summary || ''), meta: '' };
    if (scopeEl) scopeEl.textContent = title ? title + (m.meta ? ' · ' + m.meta : '') : m.meta;
    box.innerHTML = '<div class="work-preview-md">' + m.html + '</div>';
    if (window.enhanceRichMedia) window.enhanceRichMedia(box);
  } catch (e) {
    box.innerHTML = '<div class="ide-review-ai-err">' + escapeHtml(e.message || String(e)) + '</div>';
  }
}

/* 只读 diff：复用 ideDiffDoc 的「合成文档 + 整行装饰」方案，
   和 AI 变更审阅长得一样，但不动 undo / 保留逻辑。 */
async function gitOpenDiff(rel, staged) {
  let r;
  try {
    r = await api('/api/git/diff', {
      session_id: state.sessions.current, path: rel, staged: !!staged,
    });
  } catch (e) {
    return toast('读取改动失败：' + e.message);
  }
  if (!r || !r.ok) return toast((r && r.error) || '读取改动失败');
  if (r.binary) return toast('二进制文件不支持文本对比');

  const host = gitEl('ide-git-diff-editor');
  if (!host) return;
  const M = state.ideMonaco || await loadMonaco();
  if (!M) return toast('编辑器未加载，无法显示 diff');
  gitEnsureEditor(M, host);
  M.editor.setTheme(ideMonacoThemeName());

  const doc = ideDiffDoc(r);
  if (gitDiffModel) { gitDiffModel.dispose(); gitDiffModel = null; }
  gitDiffDecos = [];                    // 旧装饰属于旧 model，换模型后 ID 已失效
  gitDiffModel = M.editor.createModel(doc.text, ideLangOf(rel),
    M.Uri.from({ scheme: 'gitdiff', path: '/' + rel }));
  gitDiffEditor.setModel(gitDiffModel);
  gitDiffEditor.updateOptions({ lineNumbers: (n) => (doc.rows[n - 1] || {}).no || '' });
  const decos = [];
  doc.rows.forEach((row, i) => {
    if (!row.cls) return;
    decos.push({
      range: { startLineNumber: i + 1, startColumn: 1, endLineNumber: i + 1, endColumn: 1 },
      options: { isWholeLine: true, className: row.cls },
    });
  });
  gitDiffDecos = gitDiffEditor.deltaDecorations(gitDiffDecos, decos);

  const fileEl = gitEl('ide-git-diff-file');
  if (fileEl) fileEl.textContent = rel + (staged ? '（已暂存的改动）' : '');
  const statEl = gitEl('ide-git-diff-stat');
  if (statEl) statEl.textContent = '+' + (r.adds || 0) + '  −' + (r.dels || 0);
  ideShowView('gitdiff');
  gitDiffEditor.setScrollTop(0);
}

async function gitToggleBranchMenu() {
  const menu = gitEl('ide-git-branch-menu');
  if (!menu) return;
  if (!menu.classList.contains('hidden')) {
    menu.classList.add('hidden');
    return;
  }
  let r;
  try {
    r = await api('/api/git/branches', { session_id: state.sessions.current });
  } catch (e) {
    return toast('读取分支失败：' + e.message);
  }
  if (!r || !r.ok) return toast((r && r.error) || '读取分支失败');
  let tr = null;
  try {
    tr = await api('/api/git/tag', { session_id: state.sessions.current, action: 'list' });
  } catch (e) { tr = null; }
  const tags = (tr && tr.items) || [];
  const items = r.items || [];
  menu.innerHTML = items.map((b) =>
    '<div class="ide-git-branch-row' + (b.current ? ' cur' : '') + '" data-name="' +
      escapeHtml(b.name) + '">' +
      '<span class="n">' + (b.current ? '✓ ' : '') + escapeHtml(b.name) + '</span>' +
      '<span class="acts">' +
        (!b.current ? '<button data-bact="switch" title="切换到此分支">切换</button>' : '') +
        (!b.current ? '<button data-bact="review" title="AI 审查当前分支与此分支的差异">审查</button>' : '') +
        (!b.current ? '<button data-bact="delete" title="删除此分支">删除</button>' : '') +
        '<button data-bact="rename" title="重命名此分支">改名</button>' +
      '</span></div>').join('');
  menu.innerHTML += '<div class="ide-git-branch-row new">' +
    '<button data-bact="create">＋ 新建分支</button></div>';
  menu.innerHTML += '<div class="ide-git-branch-head">标签</div>' +
    tags.map((t) => '<div class="ide-git-branch-row tag" data-tag="' +
      escapeHtml(t.name) + '">' +
      '<span class="n">' + escapeHtml(t.name) + '</span>' +
      '<span class="acts"><button data-bact="tag-delete" title="删除标签">删除</button></span>' +
      '</div>').join('') +
    '<div class="ide-git-branch-row new">' +
    '<button data-bact="tag-create">＋ 新建标签（当前 HEAD）</button></div>';
  menu.classList.remove('hidden');
  menu.querySelectorAll('[data-bact]').forEach((btn) => {
    btn.onclick = async (e) => {
      e.stopPropagation();
      const row = btn.closest('.ide-git-branch-row');
      const name = row && row.dataset.name;
      const act = btn.dataset.bact;
      if (act === 'create') {
        const n = await appPrompt('新分支名（从当前 HEAD 开始）');
        if (!n) return;
        await gitBranchAction('create', { name: n });
      } else if (act === 'switch') {
        await gitCheckout(name);
      } else if (act === 'review') {
        menu.classList.add('hidden');
        await gitReview({ mode: 'branch', base: name, head: 'HEAD' },
                        '当前分支 vs ' + name);
      } else if (act === 'delete') {
        if (!window.confirm('删除分支 ' + name + '？')) return;
        await gitBranchAction('delete', { name: name });
      } else if (act === 'rename') {
        const n = await appPrompt('新的分支名', name);
        if (!n || n === name) return;
        await gitBranchAction('rename', { old: name, new: n });
      } else if (act === 'tag-create') {
        const n = await appPrompt('新标签名（指向当前 HEAD）');
        if (!n) return;
        await gitTagAction('create', { name: n });
      } else if (act === 'tag-delete') {
        const t = row && row.dataset.tag;
        if (!t || !window.confirm('删除标签 ' + t + '？')) return;
        await gitTagAction('delete', { name: t });
      }
    };
  });
}

async function gitTagAction(action, extra) {
  const body = Object.assign({ session_id: state.sessions.current, action: action }, extra);
  try {
    await api('/api/git/tag', body);
  } catch (e) {
    return toast('标签操作失败：' + e.message);
  }
  toast(action === 'create' ? '已创建标签' : '已删除标签', 'success');
  const menu = gitEl('ide-git-branch-menu');
  if (menu) menu.classList.add('hidden');
  await gitToggleBranchMenu();      // 重新打开，刷新标签列表
  await gitLoadHistory();
}

async function gitBranchAction(action, extra) {
  const body = Object.assign({ session_id: state.sessions.current, action: action }, extra);
  try {
    await api('/api/git/branch', body);
  } catch (e) {
    // 未合并的分支：后端会拒绝；再问一次是否强制删除
    if (action === 'delete' && /没合并|not fully merged/i.test(e.message || '')) {
      if (!window.confirm('分支还没合并，确定强制删除？')) return;
      try {
        await api('/api/git/branch', Object.assign({}, body, { force: true }));
      } catch (e2) {
        return toast('删除失败：' + e2.message);
      }
    } else {
      return toast('操作失败：' + e.message);
    }
  }
  const menu = gitEl('ide-git-branch-menu');
  if (menu) menu.classList.add('hidden');
  toast('已更新分支', 'success');
  await gitRefresh();
}

async function gitCheckout(name) {
  if (!name) return;
  try {
    await api('/api/git/checkout', { session_id: state.sessions.current, branch: name });
  } catch (e) {
    return toast('切换分支失败：' + e.message);
  }
  const menu = gitEl('ide-git-branch-menu');
  if (menu) menu.classList.add('hidden');
  toast('已切换到 ' + name, 'success');
  await gitRefresh();
}

(function gitBindUI() {
  document.querySelectorAll('.ide-git-tab').forEach((b) => {
    b.addEventListener('click', () => {
      gitSetTab(b.dataset.gitTab);
      if (b.dataset.gitTab === 'history') gitLoadHistory();
      if (b.dataset.gitTab === 'ops') gitLoadOps();
    });
  });
  gitSetTab('changes');
  const refresh = gitEl('ide-git-refresh');
  if (refresh) refresh.onclick = () => gitRefresh();
  [['ide-git-fetch', 'fetch'], ['ide-git-pull', 'pull'],
    ['ide-git-push', 'push']].forEach(([id, act]) => {
    const b = gitEl(id);
    if (b) b.onclick = () => gitSync(act);
  });
  const graphToggle = gitEl('ide-git-graph-toggle');
  if (graphToggle) {
    graphToggle.checked = gitGraphPref();
    graphToggle.onchange = () => {
      try {
        localStorage.setItem(GIT_GRAPH_KEY, graphToggle.checked ? '1' : '0');
      } catch (e) { /* 忽略：存不了就只影响下次默认值 */ }
      gitLoadHistory();
    };
  }
  const branch = gitEl('ide-git-branch');
  if (branch) branch.onclick = () => gitToggleBranchMenu();
  document.addEventListener('click', (e) => {
    const menu = gitEl('ide-git-branch-menu');
    if (!menu || menu.classList.contains('hidden')) return;
    if (e.target.closest('#ide-git-branch-menu') || e.target.closest('#ide-git-branch')) return;
    menu.classList.add('hidden');
  });
  const stageAll = gitEl('ide-git-stage-all');
  if (stageAll) stageAll.onclick = () => gitStageAll();
  const commitBtn = gitEl('ide-git-commit-btn');
  if (commitBtn) commitBtn.onclick = () => gitCommit();
  const aiMsg = gitEl('ide-git-ai-msg');
  if (aiMsg) aiMsg.onclick = () => gitGenMessage();
  const reviewBtn = gitEl('ide-git-review-btn');
  if (reviewBtn) reviewBtn.onclick = () => gitReview({ mode: 'work' }, '未提交的改动');
  const reviewClose = gitEl('ide-git-review-close');
  if (reviewClose) reviewClose.onclick = () => ideShowView('editor');
  const reviewRefresh = gitEl('ide-git-review-refresh');
  if (reviewRefresh) {
    reviewRefresh.onclick = () => {
      if (gitReviewLast) gitReview(gitReviewLast.extra, gitReviewLast.title);
    };
  }
  const close = gitEl('ide-git-diff-close');
  if (close) close.onclick = () => ideShowView('editor');
  const msg = gitEl('ide-git-msg');
  if (msg) {
    msg.addEventListener('keydown', (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
        e.preventDefault();
        gitCommit();
      }
    });
  }
  // 切回窗口时刷新：AI 刚改完文件、或在终端里动了仓库，回来就能看到
  window.addEventListener('focus', () => {
    if (state.uiMode === 'ide' && state.ideSide === 'git' && !state.ideSideHidden) gitRefresh();
  });
})();
