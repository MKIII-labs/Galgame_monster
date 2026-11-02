// 设置 → 子代理：列表 + Markdown 编辑器（数据本体在 data/agents/<id>.md）
(function () {
  const AG = { list: [], cur: '', oldId: '', isNew: false, tools: [],
    writeTools: [], bound: false };

  function el(id) { return document.getElementById(id); }

  function template() {
    return '---\nname: 新代理\ndescription: 一句话说明它负责什么\n'
      + 'tools: read_file, search_code\nmodel: \nmax_turns: 6\n'
      + 'permission: readonly\n---\n'
      + '你是……（写清任务、边界、输出格式与证据要求）\n';
  }

  function msg(text, bad) {
    const box = el('agents-msg');
    if (!box) return;
    box.textContent = text || '';
    box.style.color = bad ? 'var(--danger)' : '';
  }

  // ---- frontmatter 读写（只处理第一段 --- ... ---）----
  function splitFm(text) {
    const m = /^---\r?\n([\s\S]*?)\r?\n---\r?\n?/.exec(String(text || ''));
    if (!m) return null;
    return { fm: m[1], body: String(text).slice(m[0].length) };
  }

  function getField(text, key) {
    const fm = splitFm(text);
    if (!fm) return '';
    const re = new RegExp('^' + key + ':\\s*(.*)$', 'm');
    const m = re.exec(fm.fm);
    return m ? m[1].trim() : '';
  }

  function setField(text, key, value) {
    const fm = splitFm(text);
    if (!fm) return text;
    const re = new RegExp('^' + key + ':\\s*.*$', 'm');
    let block = fm.fm;
    if (re.test(block)) block = block.replace(re, key + ': ' + value);
    else block = block.replace(/\s*$/, '') + '\n' + key + ': ' + value;
    return '---\n' + block + '\n---\n' + fm.body;
  }

  function toolList(text) {
    return getField(text, 'tools').replace(/[\[\]]/g, '')
      .split(/[\s,，]+/).map((s) => s.trim()).filter(Boolean);
  }

  // ---- 列表 ----
  function render() {
    const box = el('agents-list');
    if (!box) return;
    box.innerHTML = '';
    AG.list.forEach((a) => {
      const row = document.createElement('div');
      row.className = 'rule-item agent-row' + (a.id === AG.cur ? ' sel' : '');
      row.tabIndex = 0;
      row.setAttribute('role', 'button');

      const head = document.createElement('div');
      head.className = 'rule-head';
      const sw = document.createElement('label');
      sw.className = 'ui-switch';
      sw.title = '启用 / 停用';
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.checked = a.enabled !== false;
      cb.onclick = (e) => { e.stopPropagation(); };
      cb.onchange = () => toggle(a.id, cb.checked);
      const track = document.createElement('span');
      track.className = 'ui-switch-track';
      sw.appendChild(cb);
      sw.appendChild(track);
      head.appendChild(sw);

      const name = document.createElement('span');
      name.className = 'rule-index';
      name.textContent = (a.name || a.id) + ' · ' + a.id;
      name.style.color = 'var(--text)';
      head.appendChild(name);
      const tag = document.createElement('span');
      tag.className = 'work-chip';
      tag.textContent = a.enabled === false ? '已停用' : (a.builtin ? '内置' : '自定义');
      head.appendChild(tag);
      row.appendChild(head);

      const meta = document.createElement('div');
      meta.className = 'rule-meta';
      meta.textContent = (a.error ? '✗ ' + a.error
        : (a.description || '') + ' · 工具 ' + (a.tools || []).length
          + (a.model ? ' · 模型 ' + a.model : ''));
      if (a.error) meta.style.color = 'var(--danger)';
      row.appendChild(meta);

      const open = () => openEditor(a);
      row.onclick = open;
      row.onkeydown = (e) => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); }
      };
      box.appendChild(row);
    });
    if (!AG.list.length) {
      const p = document.createElement('p');
      p.className = 'hint';
      p.textContent = '还没有子代理。点「＋ 新建代理」添加。';
      box.appendChild(p);
    }
  }

  // ---- 编辑器 ----
  function buildTools(tools) {
    const box = el('agents-tools');
    if (!box) return;
    box.innerHTML = '';
    const canWrite = getField(el('agents-text').value, 'permission') === 'write';
    (AG.tools || []).forEach((name) => {
      const isWrite = (AG.writeTools || []).indexOf(name) >= 0;
      const label = document.createElement('label');
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.value = name;
      cb.checked = (tools || []).indexOf(name) >= 0;
      cb.disabled = isWrite && !canWrite;
      cb.onchange = () => {
        const picked = Array.from(box.querySelectorAll('input:checked')).map((x) => x.value);
        const ta = el('agents-text');
        if (ta) ta.value = setField(ta.value, 'tools', picked.join(', '));
      };
      label.appendChild(cb);
      label.appendChild(document.createTextNode(name + (isWrite ? '（写）' : '')));
      box.appendChild(label);
    });
  }

  function buildModels() {
    const dl = el('agents-model-list');
    if (!dl) return;
    const cfg = (window.state && window.state.config) || {};
    const names = [cfg.api_model, cfg.chat_model, cfg.light_model, cfg.vision_model]
      .filter(Boolean);
    const seen = {};
    dl.innerHTML = '';
    names.forEach((n) => {
      if (seen[n]) return;
      seen[n] = 1;
      const o = document.createElement('option');
      o.value = n;
      dl.appendChild(o);
    });
  }

  function syncFromText() {
    const ta = el('agents-text');
    if (!ta) return;
    const tools = toolList(ta.value);
    const canWrite = getField(ta.value, 'permission') === 'write';
    const w = el('agents-write');
    if (w) w.checked = canWrite;
    const box = el('agents-tools');
    if (box) {
      Array.prototype.forEach.call(box.querySelectorAll('input'), (cb) => {
        cb.checked = tools.indexOf(cb.value) >= 0;
        cb.disabled = (AG.writeTools || []).indexOf(cb.value) >= 0 && !canWrite;
      });
    }
    const model = el('agents-model');
    if (model && document.activeElement !== model) model.value = getField(ta.value, 'model');
  }

  function openEditor(agent, isNew) {
    AG.cur = agent ? agent.id : '';
    AG.oldId = agent ? agent.id : '';
    AG.isNew = !!isNew;
    const editor = el('agents-editor');
    if (!editor) return;
    editor.classList.remove('hidden');
    el('agents-id').value = AG.cur;
    el('agents-id').readOnly = false;      // 允许改名
    el('agents-text').value = agent && agent.text ? agent.text : template();
    el('agents-reset').classList.toggle('hidden', !!isNew);
    buildTools(toolList(el('agents-text').value));
    buildModels();
    syncFromText();
    msg(isNew ? '新建：保存后不能与其他代理重名' : '');
    render();
  }

  async function load(keep) {
    try {
      const r = await api('/api/agents', { op: 'list' });
      if (!r.ok) throw new Error(r.error || '加载失败');
      AG.list = r.agents || [];
      AG.tools = r.tools || [];
      AG.writeTools = r.write_tools || [];
      if (!AG.tools.length) {
        const seen = {};
        AG.list.forEach((a) => (a.tools || []).forEach((t) => { seen[t] = 1; }));
        AG.tools = Object.keys(seen).sort();
      }
      if (keep && AG.cur && !AG.list.some((a) => a.id === AG.cur)) AG.cur = '';
      render();
    } catch (e) {
      msg('加载子代理失败：' + e.message, true);
    }
  }

  async function toggle(id, enabled) {
    try {
      const r = await api('/api/agents', { op: 'toggle', id, enabled });
      if (!r.ok) throw new Error(r.error || '切换失败');
      AG.list = r.agents || AG.list;
      render();
      toast(enabled ? '已启用 ' + id : '已停用 ' + id, 'success');
    } catch (e) {
      toastErr('切换子代理失败', e);
    }
  }

  async function save() {
    const id = (el('agents-id').value || '').trim();
    const text = el('agents-text').value || '';
    if (!id) { msg('请填写 id', true); return; }
    try {
      const r = await api('/api/agents', {
        op: 'save', id, text, create: AG.isNew, old_id: AG.oldId,
      });
      if (!r.ok) throw new Error(r.error || '保存失败');
      AG.cur = id;
      AG.oldId = id;
      AG.isNew = false;
      el('agents-id').readOnly = false;
      el('agents-reset').classList.remove('hidden');
      toast('子代理已保存', 'success');
      AG.list = r.agents || AG.list;
      render();
      msg('已保存：data/agents/' + id + '.md');
    } catch (e) {
      msg('保存失败：' + e.message, true);
    }
  }

  async function remove() {
    const id = (el('agents-id').value || '').trim();
    if (!id) return;
    if (!window.confirm('删除「' + id + '」的自定义文件？内置代理会恢复默认定义。')) return;
    try {
      const r = await api('/api/agents', { op: 'delete', id });
      if (!r.ok) throw new Error(r.error || '删除失败');
      el('agents-editor').classList.add('hidden');
      AG.cur = '';
      AG.oldId = '';
      AG.isNew = false;
      toast('已删除', 'success');
      AG.list = r.agents || AG.list;
      render();
    } catch (e) {
      msg('删除失败：' + e.message, true);
    }
  }

  function exportMd() {
    const id = (el('agents-id').value || 'agent').trim() || 'agent';
    const text = el('agents-text').value || '';
    const url = URL.createObjectURL(new Blob([text], { type: 'text/markdown' }));
    const a = document.createElement('a');
    a.href = url;
    a.download = id + '.md';
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
  }

  function importMd(file) {
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      const text = String(reader.result || '');
      const id = (file.name || 'agent.md').replace(/\.md$/i, '')
        .replace(/[^A-Za-z0-9_-]/g, '_').slice(0, 32) || 'agent';
      openEditor(null, true);
      el('agents-id').value = id;
      el('agents-text').value = text;
      buildTools(toolList(text));
      syncFromText();
      msg('已导入，确认无误后点保存');
    };
    reader.readAsText(file, 'utf-8');
  }

  function bind() {
    const n = el('agents-new');
    if (n) n.onclick = () => openEditor(null, true);
    const rf = el('agents-refresh');
    if (rf) rf.onclick = () => load(true);
    const sv = el('agents-save');
    if (sv) sv.onclick = save;
    const rm = el('agents-reset');
    if (rm) rm.onclick = remove;
    const ex = el('agents-export');
    if (ex) ex.onclick = exportMd;
    const ib = el('agents-import-btn');
    const inf = el('agents-import');
    if (ib && inf) {
      ib.onclick = () => inf.click();
      inf.onchange = () => {
        if (inf.files && inf.files[0]) importMd(inf.files[0]);
        inf.value = '';
      };
    }
    const ta = el('agents-text');
    if (ta) ta.oninput = syncFromText;
    const model = el('agents-model');
    if (model) model.onchange = () => {
      if (ta) ta.value = setField(ta.value, 'model', model.value.trim());
    };
    const write = el('agents-write');
    if (write) write.onchange = () => {
      if (!ta) return;
      ta.value = setField(ta.value, 'permission', write.checked ? 'write' : 'readonly');
      if (!write.checked) {
        const kept = toolList(ta.value).filter((t) => (AG.writeTools || []).indexOf(t) < 0);
        ta.value = setField(ta.value, 'tools', kept.join(', '));
      }
      syncFromText();
    };
  }

  window.loadAgents = function () {
    if (!el('agents-list')) return;
    if (!AG.bound) { bind(); AG.bound = true; }
    load(true);
  };
})();
