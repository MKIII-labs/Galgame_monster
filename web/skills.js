/* 设置 → Skill：列表、启停、编辑。
   数据本体全在 data/skills/<id>/SKILL.md，这里只是一个文本编辑器。 */
const SKILL_STATE = { items: [], current: '' };

// 只读 frontmatter 里的 name / description，用来生成 id 与列表标题
function skillMeta(text) {
  const raw = String(text || '').replace(/^\uFEFF/, '');
  const m = raw.match(/^---\r?\n([\s\S]*?)\r?\n---/);
  const meta = {};
  if (!m) return meta;
  m[1].split(/\r?\n/).forEach((line) => {
    const i = line.indexOf(':');
    if (i > 0) meta[line.slice(0, i).trim().toLowerCase()] = line.slice(i + 1).trim();
  });
  return meta;
}

// 中文名转不出 ASCII 时退回时间戳，避免多个新 Skill 撞同一个 id
function skillSlug(name) {
  const s = String(name || '').toLowerCase()
    .replace(/[^a-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 40);
  return s || ('skill-' + Date.now().toString(36));
}

function skillRender() {
  const box = $('#skill-list');
  if (!box) return;
  if (!SKILL_STATE.items.length) {
    box.innerHTML = '<p class="hint">还没有 Skill。点下面的「新建」写一个。</p>';
    return;
  }
  box.innerHTML = SKILL_STATE.items.map((it) => (
    '<div class="skill-row' + (it.id === SKILL_STATE.current ? ' active' : '') +
      '" data-id="' + escapeHtml(it.id) + '">' +
      '<input type="checkbox" data-act="toggle"' + (it.enabled ? ' checked' : '') + '>' +
      '<span class="skill-name">' + escapeHtml(it.name) + '</span>' +
      '<span class="skill-desc">' + escapeHtml(it.description || '无描述') + '</span>' +
      '<span class="skill-id">' + escapeHtml(it.id) + '</span>' +
      '<button class="ghost" data-act="edit">编辑</button>' +
    '</div>'
  )).join('');
}

async function loadSkills() {
  const box = $('#skill-list');
  if (!box) return;
  try {
    const r = await api('/api/skills', { op: 'list' });
    SKILL_STATE.items = r.items || [];
    if (SKILL_STATE.current && !SKILL_STATE.items.some((x) => x.id === SKILL_STATE.current)) {
      SKILL_STATE.current = '';
    }
    skillRender();
    const dir = $('#skill-dir');
    if (dir) dir.textContent = '目录：' + (r.dir || 'data/skills');
  } catch (e) {
    box.innerHTML = '<p class="hint">读取失败：' + escapeHtml(e.message) + '</p>';
  }
}

function skillEdit(id) {
  const it = SKILL_STATE.items.find((x) => x.id === id);
  if (!it) return;
  SKILL_STATE.current = id;
  $('#skill-text').value = it.text || '';
  $('#skill-current').textContent = '当前：' + it.name + '（id: ' + it.id + '）';
  skillRender();
}

function skillNew() {
  SKILL_STATE.current = '';
  $('#skill-text').value =
    '---\nname: 新 Skill\ndescription: 一句话说明它解决什么问题、什么时候该用\n---\n\n' +
    '## 目标\n\n## 每轮必做\n\n1. \n\n## 禁止\n\n- \n';
  $('#skill-current').textContent = '当前：新 Skill（保存时按 name 生成 id）';
  skillRender();
  $('#skill-text').focus();
}

async function skillSave() {
  const text = $('#skill-text').value;
  const meta = skillMeta(text);
  if (!meta.name) { toast('frontmatter 里要有 name', 'err'); return; }
  const id = SKILL_STATE.current || skillSlug(meta.name);
  try {
    const r = await api('/api/skills', { op: 'save', id: id, text: text });
    SKILL_STATE.items = r.items || [];
    SKILL_STATE.current = id;
    skillRender();
    $('#skill-current').textContent = '当前：' + (meta.name || id) + '（id: ' + id + '）';
    toast('已保存：' + (meta.name || id), 'ok');
  } catch (e) {
    toast(e.message, 'err');
  }
}

async function skillDelete() {
  const id = SKILL_STATE.current;
  if (!id) { toast('先在列表里选一个 Skill', 'err'); return; }
  if (!confirm('删除 Skill「' + id + '」？目录会整个删掉，不可恢复。')) return;
  try {
    const r = await api('/api/skills', { op: 'delete', id: id });
    SKILL_STATE.items = r.items || [];
    SKILL_STATE.current = '';
    $('#skill-text').value = '';
    $('#skill-current').textContent = '当前：新 Skill';
    skillRender();
    toast('已删除：' + id, 'ok');
  } catch (e) {
    toast(e.message, 'err');
  }
}

function skillBind() {
  const box = $('#skill-list');
  if (!box) return;

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
      cb.checked = !cb.checked;      // 失败就还原勾选状态
      toast(err.message, 'err');
    }
  });

  box.addEventListener('click', (e) => {
    const btn = e.target.closest ? e.target.closest('button[data-act="edit"]') : null;
    if (!btn) return;
    const row = btn.closest('.skill-row');
    if (row) skillEdit(row.dataset.id);
  });

  $('#skill-new').addEventListener('click', skillNew);
  $('#skill-save').addEventListener('click', skillSave);
  $('#skill-del').addEventListener('click', skillDelete);
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', skillBind);
} else {
  skillBind();
}
