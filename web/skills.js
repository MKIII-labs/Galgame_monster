/* 设置 → Skill：两栏工作台（左侧列表 / 右侧编辑器）。
   数据本体在 data/skills/<id>/SKILL.md；这里只把 frontmatter 的常见字段表单化，
   name / description / keywords 以外的键按原顺序保留，不会把用户手写的扩展字段吃掉。 */
const SKILL_STATE = {
  items: [],
  current: '',        // 正在编辑的 id；空 = 新建中
  dir: '',
  dirty: false,
  q: '',
  extra: [],          // 当前 skill 的其余 frontmatter 键值（保序）
  refs: [],           // 当前 skill 的参考文件 [{name,size}]
  refCur: '',         // 正在编辑的参考文件名
  refDirty: false,
  lintTimer: 0,
};

// 中文名转不出 ASCII 时退回时间戳；服务端 unique 会再加 -2/-3 避免覆盖
function skillSlug(name) {
  const s = String(name || '').toLowerCase()
    .replace(/[^a-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 36);
  return s || ('skill-' + Date.now().toString(36));
}

function skillById(id) {
  return SKILL_STATE.items.find((x) => x.id === id) || null;
}

function skillRelPath(id) {
  return 'data/skills/' + id + '/SKILL.md';
}

function skillOneLine(v) {
  return String(v == null ? '' : v).replace(/\s+/g, ' ').trim();
}

/* 解析 SKILL.md：返回 {name, description, keywords, when_not, body, extra} */
function skillParse(text) {
  const raw = String(text || '').replace(/^\uFEFF/, '');
  const out = { name: '', description: '', keywords: '', when_not: '', body: raw.trim(), extra: [] };
  const lines = raw.split(/\r?\n/);
  if (!lines.length || lines[0].trim() !== '---') return out;
  let end = -1;
  for (let i = 1; i < lines.length; i++) {
    if (lines[i].trim() === '---') { end = i; break; }
  }
  if (end < 0) return out;
  const meta = [];
  for (let i = 1; i < end; i++) {
    const k = lines[i].indexOf(':');
    if (k <= 0) continue;
    meta.push([lines[i].slice(0, k).trim(), lines[i].slice(k + 1).trim()]);
  }
  const pick = (key) => {
    const hit = meta.find(([k]) => k.toLowerCase() === key);
    return hit ? hit[1] : '';
  };
  const known = ['name', 'description', 'keywords', 'when_not'];
  out.name = pick('name');
  out.description = pick('description');
  out.keywords = pick('keywords');
  out.when_not = pick('when_not');
  out.extra = meta.filter(([k]) => known.indexOf(k.toLowerCase()) < 0);
  out.body = lines.slice(end + 1).join('\n').trim();
  return out;
}

/* 把表单重新拼回 SKILL.md（未知字段原样写回） */
function skillCompose(d) {
  const lines = ['---',
    'name: ' + skillOneLine(d.name),
    'description: ' + skillOneLine(d.desc)];
  const keys = skillOneLine(d.keys);
  if (keys) lines.push('keywords: ' + keys);
  const whenNot = skillOneLine(d.whenNot);
  if (whenNot) lines.push('when_not: ' + whenNot);
  (d.extra || []).forEach(([k, v]) => {
    if (!k) return;
    lines.push(k + ':' + (v ? ' ' + v : ''));
  });
  lines.push('---', '');
  const body = String(d.body || '').replace(/\s+$/, '');
  return lines.join('\n') + (body ? '\n' + body : '') + '\n';
}

function skillVisibleItems() {
  const q = SKILL_STATE.q.trim().toLowerCase();
  if (!q) return SKILL_STATE.items;
  return SKILL_STATE.items.filter((it) => {
    const hay = [it.name, it.description, it.keywords, it.id]
      .map((x) => String(x || '')).join(' ').toLowerCase();
    return hay.indexOf(q) >= 0;
  });
}

function skillRender() {
  const box = $('#skill-list');
  if (!box) return;
  const list = skillVisibleItems();
  if (!SKILL_STATE.items.length) {
    box.innerHTML = '<p class="skill-empty-tip">还没有 Skill。点上面的「新建」写一个。</p>';
  } else if (!list.length) {
    box.innerHTML = '<p class="skill-empty-tip">没有匹配的 Skill。</p>';
  } else {
    box.innerHTML = list.map((it) => (
      '<div class="skill-row' + (it.id === SKILL_STATE.current ? ' active' : '') +
        '" data-id="' + escapeHtml(it.id) + '" title="' + escapeHtml(skillRelPath(it.id)) + '">' +
        '<div class="skill-row-main">' +
          '<div class="skill-row-name">' + escapeHtml(it.name) +
            (it.enabled ? '' : '<span class="skill-badge">已停用</span>') +
            (it.uses ? '<span class="skill-badge">用过 ' + it.uses + '</span>' : '') + '</div>' +
          '<div class="skill-row-desc">' + escapeHtml(it.description || '无描述') + '</div>' +
        '</div>' +
        '<label class="ui-switch" title="启用 / 停用">' +
          '<input type="checkbox" data-act="toggle"' + (it.enabled ? ' checked' : '') + '>' +
          '<span class="ui-switch-track"></span>' +
        '</label>' +
      '</div>'
    )).join('');
  }
  const cnt = $('#skill-count');
  if (cnt) {
    const on = SKILL_STATE.items.filter((x) => x.enabled).length;
    cnt.textContent = '共 ' + SKILL_STATE.items.length + ' 个 · 启用 ' + on;
  }
}

function skillShowEditor(show) {
  const ed = $('#skill-editor');
  const empty = $('#skill-empty');
  if (ed) ed.classList.toggle('hidden', !show);
  if (empty) empty.classList.toggle('hidden', show);
}

function skillFmtTime(ts) {
  const n = Number(ts || 0);
  if (!n) return '';
  const d = new Date(n * 1000);
  const p = (x) => String(x).padStart(2, '0');
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate());
}

function skillMetaLine() {
  const box = $('#skill-current');
  if (!box) return;
  const it = skillById(SKILL_STATE.current);
  if (!it) {
    box.innerHTML = '新建中：保存后自动分配 id，不会覆盖已有 Skill';
    return;
  }
  const bits = [
    'id: <code>' + escapeHtml(it.id) + '</code>',
    '文件：<code title="' + escapeHtml(it.path || '') + '">' +
      escapeHtml(skillRelPath(it.id)) + '</code>',
  ];
  if (it.uses) {
    bits.push('用过 ' + it.uses + ' 次' +
      (it.last_used ? '（最近 ' + skillFmtTime(it.last_used) + '）' : ''));
  }
  box.innerHTML = bits.join(' · ');
}

function skillFillEditor(name, desc, keys, whenNot, body, extra) {
  $('#skill-name').value = name || '';
  $('#skill-desc').value = desc || '';
  $('#skill-keys').value = keys || '';
  const wn = $('#skill-when-not');
  if (wn) wn.value = whenNot || '';
  $('#skill-body').value = body || '';
  SKILL_STATE.extra = extra || [];
  const edName = $('#skill-ed-name');
  if (edName) edName.textContent = (name || '新 Skill').trim() || '新 Skill';
  skillMetaLine();
  skillShowEditor(true);
  skillLintNow();
}

function skillSetDirty(on) {
  SKILL_STATE.dirty = !!on;
  const st = $('#skill-ed-state');
  if (st) {
    st.textContent = SKILL_STATE.dirty ? '未保存' : '已保存';
    st.classList.toggle('dirty', SKILL_STATE.dirty);
  }
  if (on) skillLintNow();
}

/* 有未保存改动时，切走 / 新建 / 删除前问一句 */
function skillLeaveGuard(what) {
  if (!SKILL_STATE.dirty) return true;
  return confirm('当前 Skill 有未保存的改动，放弃并' + (what || '继续') + '？');
}

/* ---------- 校验：规则在服务端（和提示词同一份实现），这里只负责展示 ---------- */
function skillLintNow() {
  clearTimeout(SKILL_STATE.lintTimer);
  const creating = !SKILL_STATE.current;
  if (creating && !$('#skill-name').value.trim()) {
    skillRenderLint(null);           // 还没开始写，不摆一堆错误
    return;
  }
  SKILL_STATE.lintTimer = setTimeout(skillLint, 450);
}

function skillRenderLint(items) {
  const box = $('#skill-lint');
  if (!box) return;
  if (!items || !items.length) { box.innerHTML = ''; return; }
  const rows = items.filter((x) => x.level !== 'ok');
  const label = { error: '错误', warn: '建议', info: '提示' };
  box.innerHTML = rows.map((x) => (
    '<span class="slint-item ' + x.level + '">' + label[x.level] + ' · ' +
      escapeHtml(x.message) + '</span>'
  )).join('');
}

async function skillLint() {
  const box = $('#skill-lint');
  if (!box) return;
  const text = skillCompose({
    name: $('#skill-name').value,
    desc: $('#skill-desc').value,
    keys: $('#skill-keys').value,
    whenNot: ($('#skill-when-not') || {}).value || '',
    body: $('#skill-body').value,
    extra: SKILL_STATE.extra,
  });
  try {
    const r = await api('/api/skills', {
      op: 'lint', id: SKILL_STATE.current, text: text,
    });
    skillRenderLint(r.items || []);
  } catch (e) {
    box.innerHTML = '<span class="slint-item error">校验失败：' +
      escapeHtml(e.message) + '</span>';
  }
}

/* ---------- 试触发：用服务端同一套匹配规则预览会命中哪些 Skill ---------- */
async function skillTriggerRun() {
  const input = $('#skill-trigger-input');
  const out = $('#skill-trigger-out');
  if (!input || !out) return;
  const text = (input.value || '').trim();
  if (!text) { out.textContent = '先输入一句用户可能说的话。'; return; }
  try {
    const r = await api('/api/skills', { op: 'trigger', text: text });
    const hits = r.matches || [];
    if (!hits.length) {
      out.innerHTML = '<span class="strig-none">没有命中：这句话不会把任何 Skill 带进上下文。</span>';
      return;
    }
    out.innerHTML = hits.map((h) => (
      '<span class="strig-hit">' + escapeHtml(h.name) +
        ' <code>' + escapeHtml(h.id) + '</code> · 分 ' + h.score + '</span>' +
        (h.id === SKILL_STATE.current ? '<span class="strig-cur">当前</span>' : '')
    )).join('');
  } catch (e) {
    out.textContent = '试触发失败：' + e.message;
  }
}

/* ---------- 参考文件（references/） ---------- */
function skillRefRender() {
  const list = $('#skill-ref-list');
  if (!list) return;
  const items = SKILL_STATE.refs || [];
  if (!items.length) {
    list.innerHTML = '<span class="skill-ref-empty">还没有参考文件</span>';
  } else {
    list.innerHTML = items.map((f) => (
      '<button type="button" class="skill-ref-item' +
        (f.name === SKILL_STATE.refCur ? ' active' : '') +
        '" data-name="' + escapeHtml(f.name) + '">' + escapeHtml(f.name) +
        '<span class="skill-ref-size">' +
          Math.max(1, Math.round((f.size || 0) / 1024)) + ' KB</span></button>'
    )).join('');
  }
  const edit = $('#skill-ref-edit');
  if (edit) edit.classList.toggle('hidden', !SKILL_STATE.refCur);
}

function skillRefSet(name, text) {
  SKILL_STATE.refCur = name || '';
  SKILL_STATE.refDirty = false;
  const n = $('#skill-ref-name');
  const b = $('#skill-ref-body');
  if (n) n.value = name || '';
  if (b) b.value = text || '';
  skillRefRender();
}

function skillRefGuard() {
  if (!SKILL_STATE.refDirty) return true;
  return confirm('参考文件有未保存的改动，放弃并继续？');
}

async function skillRefOpen(name) {
  if (!SKILL_STATE.current || !skillRefGuard()) return;
  try {
    const r = await api('/api/skills', {
      op: 'read_file', id: SKILL_STATE.current, file: name,
    });
    skillRefSet(name, r.text || '');
  } catch (e) {
    toast(e.message, 'err');
  }
}

function skillRefNew() {
  if (!SKILL_STATE.current) { toast('先保存 Skill，再添加参考文件', 'err'); return; }
  if (!skillRefGuard()) return;
  skillRefSet('notes.md', '');
  $('#skill-ref-name').focus();
  $('#skill-ref-name').select();
}

async function skillRefSave() {
  const id = SKILL_STATE.current;
  if (!id) { toast('先保存 Skill，再保存参考文件', 'err'); return; }
  const name = ($('#skill-ref-name').value || '').trim();
  const text = $('#skill-ref-body').value || '';
  if (!name) { toast('先给参考文件起个名字', 'err'); return; }
  try {
    const r = await api('/api/skills', {
      op: 'save_file', id: id, file: name, text: text,
    });
    SKILL_STATE.refs = r.files || [];
    const it = skillById(id);
    if (it) it.files = SKILL_STATE.refs;
    SKILL_STATE.refCur = name;
    SKILL_STATE.refDirty = false;
    skillRefRender();
    toast('已保存参考文件：' + name, 'ok');
  } catch (e) {
    toast(e.message, 'err');
  }
}

async function skillRefDel() {
  const id = SKILL_STATE.current;
  const name = ($('#skill-ref-name').value || '').trim() || SKILL_STATE.refCur;
  if (!id || !name) return;
  if (!confirm('删除参考文件「' + name + '」？')) return;
  try {
    const r = await api('/api/skills', { op: 'delete_file', id: id, file: name });
    SKILL_STATE.refs = r.files || [];
    const it = skillById(id);
    if (it) it.files = SKILL_STATE.refs;
    skillRefSet('', '');
    toast('已删除参考文件', 'ok');
  } catch (e) {
    toast(e.message, 'err');
  }
}

/* ---------- 导入 / 导出 ---------- */
async function skillExport() {
  const id = SKILL_STATE.current;
  if (!id) { toast('先保存 Skill，再导出', 'err'); return; }
  try {
    const r = await api('/api/skills', { op: 'export', id: id });
    const bin = atob(r.data || '');
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    const url = URL.createObjectURL(new Blob([bytes], { type: 'application/zip' }));
    const a = document.createElement('a');
    a.href = url;
    a.download = r.filename || (id + '.zip');
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    toast('已导出 ' + a.download, 'ok');
  } catch (e) {
    toast(e.message, 'err');
  }
}

function skillImportPick() {
  const el = $('#skill-import-file');
  if (el) { el.value = ''; el.click(); }
}

async function skillImportFile(file) {
  if (!file) return;
  if (file.size > 4 * 1024 * 1024) { toast('zip 太大（上限 4MB）', 'err'); return; }
  const bytes = new Uint8Array(await file.arrayBuffer());
  let bin = '';
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  await skillImportSend({ op: 'import', filename: file.name, data: btoa(bin) });
}

async function skillImportDir() {
  const path = (typeof appPrompt === 'function')
    ? await appPrompt('输入 Skill 文件夹的完整路径（文件夹里要有 SKILL.md）', '')
    : window.prompt('输入 Skill 文件夹的完整路径（文件夹里要有 SKILL.md）', '');
  if (!path) return;
  await skillImportSend({ op: 'import_path', path: path });
}

async function skillImportSend(body) {
  try {
    const r = await api('/api/skills', body);
    SKILL_STATE.items = r.items || [];
    if (r.id) skillSelect(r.id);
    skillRender();
    toast('已导入：' + (r.id || ''), 'ok');
  } catch (e) {
    toast(e.message, 'err');
  }
}

function skillSelect(id) {
  const it = skillById(id);
  if (!it) return;
  if (id === SKILL_STATE.current) return;      // 已选中：不要用磁盘内容冲掉未保存的编辑
  if (!skillLeaveGuard('切换')) return;
  if (!skillRefGuard()) return;
  const parsed = skillParse(it.text);
  SKILL_STATE.current = id;
  SKILL_STATE.refs = it.files || [];
  SKILL_STATE.refCur = '';
  SKILL_STATE.refDirty = false;
  skillRender();
  skillFillEditor(parsed.name, parsed.description, parsed.keywords,
                  parsed.when_not, parsed.body, parsed.extra);
  skillRefSet('', '');
  skillSetDirty(false);
}

function skillClearSearch() {
  SKILL_STATE.q = '';
  const se = $('#skill-search');
  if (se) se.value = '';
}

function skillNew() {
  if (!skillLeaveGuard('新建') || !skillRefGuard()) return;
  skillClearSearch();
  SKILL_STATE.current = '';
  SKILL_STATE.refs = [];
  SKILL_STATE.refCur = '';
  skillRender();
  skillFillEditor('新 Skill',
                  '当用户……时使用',
                  '',
                  '',
                  '# 新 Skill\n\n## 何时用\n\n- \n\n## 不适用\n\n- \n\n## 核心步骤\n\n1. \n\n## 输出格式\n\n- \n\n## 失败处理\n\n- \n\n## 常见错误\n\n- ',
                  []);
  skillRefSet('', '');
  skillSetDirty(true);
  clearTimeout(SKILL_STATE.lintTimer);
  skillRenderLint(null);          // 新建时先不摆一堆「缺 description」
  $('#skill-name').focus();
  $('#skill-name').select();
}

async function loadSkills() {
  const box = $('#skill-list');
  if (!box) return;
  try {
    const r = await api('/api/skills', { op: 'list' });
    SKILL_STATE.items = r.items || [];
    SKILL_STATE.dir = r.dir || '';
    if (SKILL_STATE.current && !skillById(SKILL_STATE.current)) {
      SKILL_STATE.current = '';
      SKILL_STATE.dirty = false;
      skillShowEditor(false);
    }
    const cur = skillById(SKILL_STATE.current);
    if (cur) {
      SKILL_STATE.refs = cur.files || [];
      skillRefRender();
    }
    skillRender();
    skillMetaLine();
  } catch (e) {
    box.innerHTML = '<p class="skill-empty-tip">读取失败：' + escapeHtml(e.message) + '</p>';
  }
}

async function skillSave() {
  const name = $('#skill-name').value.trim();
  if (!name) { toast('先给这个 Skill 起个名字', 'err'); $('#skill-name').focus(); return; }
  const text = skillCompose({
    name: name,
    desc: $('#skill-desc').value,
    keys: $('#skill-keys').value,
    whenNot: ($('#skill-when-not') || {}).value || '',
    body: $('#skill-body').value,
    extra: SKILL_STATE.extra,
  });
  const creating = !SKILL_STATE.current;
  const id = creating ? skillSlug(name) : SKILL_STATE.current;
  try {
    const r = await api('/api/skills', { op: 'save', id: id, text: text, unique: creating });
    SKILL_STATE.items = r.items || [];
    SKILL_STATE.current = r.id || id;
    skillClearSearch();          // 过滤条件可能把刚保存的条目藏起来
    skillRender();
    const saved = skillById(SKILL_STATE.current);
    if (saved) {
      const parsed = skillParse(saved.text);
      SKILL_STATE.extra = parsed.extra;
      SKILL_STATE.refs = saved.files || [];
      const edName = $('#skill-ed-name');
      if (edName) edName.textContent = saved.name || parsed.name || name;
    }
    skillMetaLine();
    skillRefRender();
    skillSetDirty(false);
    skillLintNow();
    toast((creating ? '已新建：' : '已保存：') + skillRelPath(SKILL_STATE.current), 'ok');
  } catch (e) {
    toast(e.message, 'err');
  }
}

async function skillDelete() {
  const id = SKILL_STATE.current;
  if (!id) { toast('这是还没保存的新 Skill，直接改或者离开即可', 'err'); return; }
  if (!confirm('删除 Skill「' + id + '」？目录会整个删掉，不可恢复。')) return;
  try {
    const r = await api('/api/skills', { op: 'delete', id: id });
    SKILL_STATE.items = r.items || [];
    SKILL_STATE.current = '';
    SKILL_STATE.refs = [];
    SKILL_STATE.refCur = '';
    SKILL_STATE.dirty = false;
    skillRender();
    skillShowEditor(false);
    skillRenderLint(null);
    toast('已删除：' + id, 'ok');
  } catch (e) {
    toast(e.message, 'err');
  }
}

async function skillOpenDir() {
  try {
    await api('/api/skills', { op: 'open' });
    toast('已打开 Skill 目录', 'ok');
  } catch (e) {
    toast(e.message, 'err');
  }
}

function skillBind() {
  const box = $('#skill-list');
  if (!box) return;

  // 启停开关：勾选态来自服务端；失败回滚
  box.addEventListener('change', async (e) => {
    const cb = e.target;
    if (!cb || cb.dataset.act !== 'toggle') return;
    const row = cb.closest('.skill-row');
    if (!row) return;
    try {
      const r = await api('/api/skills', {
        op: 'toggle', id: row.dataset.id, enabled: cb.checked,
      });
      SKILL_STATE.items = r.items || [];
      skillRender();
      toast(cb.checked ? '已启用' : '已停用', 'ok');
    } catch (err) {
      cb.checked = !cb.checked;
      toast(err.message, 'err');
    }
  });

  // 点行切换（开关自己会 stopPropagation，不会误触）
  box.addEventListener('click', (e) => {
    if (e.target.closest && e.target.closest('.ui-switch')) return;
    const row = e.target.closest ? e.target.closest('.skill-row') : null;
    if (row) skillSelect(row.dataset.id);
  });

  const search = $('#skill-search');
  if (search) {
    search.addEventListener('input', () => {
      SKILL_STATE.q = search.value || '';
      skillRender();
    });
  }

  // 表单改动 → 未保存；Ctrl/Cmd+S 直接保存
  ['#skill-name', '#skill-desc', '#skill-keys', '#skill-when-not', '#skill-body'].forEach((sel) => {
    const el = $(sel);
    if (!el) return;
    el.addEventListener('input', () => {
      skillSetDirty(true);
      if (sel === '#skill-name') {
        const edName = $('#skill-ed-name');
        if (edName) edName.textContent = el.value.trim() || '新 Skill';
      }
    });
    el.addEventListener('keydown', (e) => {
      if ((e.ctrlKey || e.metaKey) && (e.key === 's' || e.key === 'S')) {
        e.preventDefault();
        skillSave();
      }
    });
  });

  // 参考文件：点列表打开；输入内容标记未保存
  const refList = $('#skill-ref-list');
  if (refList) {
    refList.addEventListener('click', (e) => {
      const row = e.target.closest ? e.target.closest('.skill-ref-item') : null;
      if (row) skillRefOpen(row.dataset.name);
    });
  }
  ['#skill-ref-name', '#skill-ref-body'].forEach((sel) => {
    const el = $(sel);
    if (!el) return;
    el.addEventListener('input', () => { SKILL_STATE.refDirty = true; });
  });

  // 试触发：按钮或回车
  const triggerBtn = $('#skill-trigger-btn');
  if (triggerBtn) triggerBtn.addEventListener('click', skillTriggerRun);
  const triggerInput = $('#skill-trigger-input');
  if (triggerInput) {
    triggerInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); skillTriggerRun(); }
    });
  }

  const on = (sel, fn) => {
    const el = $(sel);
    if (el) el.addEventListener('click', fn);
  };
  on('#skill-new', skillNew);
  on('#skill-save', skillSave);
  on('#skill-del', skillDelete);
  on('#skill-export', skillExport);
  on('#skill-import', skillImportPick);
  on('#skill-import-dir', skillImportDir);
  on('#skill-open-dir', skillOpenDir);
  on('#skill-ref-add', skillRefNew);
  on('#skill-ref-save', skillRefSave);
  on('#skill-ref-del', skillRefDel);

  const importFile = $('#skill-import-file');
  if (importFile) {
    importFile.addEventListener('change', () => {
      const f = importFile.files && importFile.files[0];
      if (f) skillImportFile(f);
    });
  }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', skillBind);
} else {
  skillBind();
}
