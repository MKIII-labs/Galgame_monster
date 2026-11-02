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

/* 解析 SKILL.md：返回 {name, description, keywords, body, extra} */
function skillParse(text) {
  const raw = String(text || '').replace(/^\uFEFF/, '');
  const out = { name: '', description: '', keywords: '', body: raw.trim(), extra: [] };
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
  const known = ['name', 'description', 'keywords'];
  out.name = pick('name');
  out.description = pick('description');
  out.keywords = pick('keywords');
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
            (it.enabled ? '' : '<span class="skill-badge">已停用</span>') + '</div>' +
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

function skillMetaLine() {
  const box = $('#skill-current');
  if (!box) return;
  const it = skillById(SKILL_STATE.current);
  if (!it) {
    box.innerHTML = '新建中：保存后自动分配 id，不会覆盖已有 Skill';
    return;
  }
  box.innerHTML = 'id: <code>' + escapeHtml(it.id) + '</code>' +
    ' · 文件：<code title="' + escapeHtml(it.path || '') + '">' +
    escapeHtml(skillRelPath(it.id)) + '</code>';
}

function skillFillEditor(name, desc, keys, body, extra) {
  $('#skill-name').value = name || '';
  $('#skill-desc').value = desc || '';
  $('#skill-keys').value = keys || '';
  $('#skill-body').value = body || '';
  SKILL_STATE.extra = extra || [];
  const edName = $('#skill-ed-name');
  if (edName) edName.textContent = (name || '新 Skill').trim() || '新 Skill';
  skillMetaLine();
  skillShowEditor(true);
}

function skillSetDirty(on) {
  SKILL_STATE.dirty = !!on;
  const st = $('#skill-ed-state');
  if (st) {
    st.textContent = SKILL_STATE.dirty ? '未保存' : '已保存';
    st.classList.toggle('dirty', SKILL_STATE.dirty);
  }
}

/* 有未保存改动时，切走 / 新建 / 删除前问一句 */
function skillLeaveGuard(what) {
  if (!SKILL_STATE.dirty) return true;
  return confirm('当前 Skill 有未保存的改动，放弃并' + (what || '继续') + '？');
}

function skillSelect(id) {
  const it = skillById(id);
  if (!it) return;
  if (id === SKILL_STATE.current) return;      // 已选中：不要用磁盘内容冲掉未保存的编辑
  if (!skillLeaveGuard('切换')) return;
  const parsed = skillParse(it.text);
  SKILL_STATE.current = id;
  skillRender();
  skillFillEditor(parsed.name, parsed.description, parsed.keywords,
                  parsed.body, parsed.extra);
  skillSetDirty(false);
}

function skillClearSearch() {
  SKILL_STATE.q = '';
  const se = $('#skill-search');
  if (se) se.value = '';
}

function skillNew() {
  if (!skillLeaveGuard('新建')) return;
  skillClearSearch();
  SKILL_STATE.current = '';
  skillRender();
  skillFillEditor('新 Skill',
                  '一句话说明它解决什么问题、什么时候该用',
                  '',
                  '## 目标\n\n## 每轮必做\n\n1. \n\n## 禁止\n\n- ',
                  []);
  skillSetDirty(true);
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
      const edName = $('#skill-ed-name');
      if (edName) edName.textContent = saved.name || parsed.name || name;
    }
    skillMetaLine();
    skillSetDirty(false);
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
    SKILL_STATE.dirty = false;
    skillRender();
    skillShowEditor(false);
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
  ['#skill-name', '#skill-desc', '#skill-keys', '#skill-body'].forEach((sel) => {
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

  $('#skill-new').addEventListener('click', skillNew);
  $('#skill-save').addEventListener('click', skillSave);
  $('#skill-del').addEventListener('click', skillDelete);
  const openBtn = $('#skill-open-dir');
  if (openBtn) openBtn.addEventListener('click', skillOpenDir);
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', skillBind);
} else {
  skillBind();
}
