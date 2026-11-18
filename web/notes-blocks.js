'use strict';

/* ============================================================
   笔记块编辑器（无构建链、Markdown 仍是唯一存储格式）
   ------------------------------------------------------------
   · parse(md) → blocks；serialize(blocks) → md，保证「打开再保存」不丢内容
   · 不支持的语法（表格 / HTML / 嵌套列表 / 缩进代码）整段进 raw 块，原样保留
   · 行内支持：**粗** *斜* `代码` ~~删除~~ [链接](url) [[双链]]
   · 交互：Enter 拆块、Backspace 合并、方向键跨块、/ 菜单、拖动手柄排序
   ============================================================ */
(function () {
  if (window.NoteBlocks) return;

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
  }

  /* ---------- 行内 Markdown → HTML ---------- */
  function inlineToHtml(text) {
    let s = esc(text);
    const codes = [];
    // 行内代码先摘出来，避免里面的 ** / [[ 被当成格式
    s = s.replace(/`([^`\n]+)`/g, (m, c) => {
      codes.push(c);
      return '\u0000C' + (codes.length - 1) + '\u0000';
    });
    s = s.replace(/\[\[([^\[\]\n]+?)\]\]/g, (m, raw) => {
      const seg = String(raw).split('|');
      const target = seg[0].trim();
      const label = (seg[1] || target).trim();
      if (!target) return m;
      return '<a class="wikilink" data-note="' + esc(target) + '" contenteditable="false">'
        + esc(label) + '</a>';
    });
    s = s.replace(/\[([^\]\n]+)\]\(([^)\s]+)\)/g, (m, label, url) =>
      (/^(https?:|mailto:|#|\/|\.)/i.test(url)
        ? '<a href="' + url + '" target="_blank" rel="noreferrer">' + label + '</a>'
        : m));
    s = s.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>');
    s = s.replace(/~~([^~\n]+)~~/g, '<s>$1</s>');
    s = s.replace(/\u0000C(\d+)\u0000/g, (m, i) => '<code>' + esc(codes[+i] || '') + '</code>');
    return s;
  }

  function nodeToMd(n) {
    if (n.nodeType === 3) return n.nodeValue || '';
    if (n.nodeType !== 1) return '';
    const tag = n.tagName;
    if (n.classList && n.classList.contains('nb-check')) return '';
    if (tag === 'BR') return '\n';
    const inner = Array.prototype.map.call(n.childNodes, nodeToMd).join('');
    switch (tag) {
      case 'STRONG': case 'B': return inner ? '**' + inner + '**' : '';
      case 'EM': case 'I': return inner ? '*' + inner + '*' : '';
      case 'CODE': return '`' + inner + '`';
      case 'S': case 'DEL': case 'STRIKE': return '~~' + inner + '~~';
      case 'A': {
        const wiki = n.getAttribute && n.getAttribute('data-note');
        if (wiki) {
          const label = inner || wiki;
          return '[[' + wiki + (label && label !== wiki ? '|' + label : '') + ']]';
        }
        const href = n.getAttribute ? (n.getAttribute('href') || '') : '';
        return href ? '[' + (inner || href) + '](' + href + ')' : inner;
      }
      default: return inner;
    }
  }

  function inlineToMd(root) {
    if (!root) return '';
    const tmp = document.createElement('div');
    Array.prototype.forEach.call(root.childNodes, (n) => tmp.appendChild(n.cloneNode(true)));
    return nodeToMd(tmp).replace(/\u00a0/g, ' ').replace(/\n+$/, '');
  }

  /* ---------- Markdown ↔ blocks ---------- */
  let seq = 0;
  function nid() { seq += 1; return 'b' + Date.now().toString(36) + seq.toString(36); }

  const LIST_TYPES = { ul: 1, ol: 1, task: 1 };
  const RAW_START = /^(?:\s*\|[\s:|-]|<\/?[a-zA-Z][^>]*>|\s{4,}|\t)/;

  function parse(md) {
    const lines = String(md == null ? '' : md).replace(/\r\n?/g, '\n').split('\n');
    const out = [];
    const push = (type, text, meta) => out.push({ id: nid(), type, text, meta: meta || {} });
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (!line.trim()) { i += 1; continue; }

      // 围栏代码
      let m = /^\s*```([\w+-]*)\s*$/.exec(line);
      if (m) {
        const lang = m[1] || '';
        const buf = [];
        i += 1;
        while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) { buf.push(lines[i]); i += 1; }
        i += 1;                                   // 跳过结束围栏
        push('code', buf.join('\n'), { lang: lang });
        continue;
      }

      if (/^\s*(?:---|\*\*\*|___)\s*$/.test(line)) { push('hr', ''); i += 1; continue; }

      m = /^(#{1,3})\s+(.*)$/.exec(line);
      if (m) { push('h' + m[1].length, m[2].trim()); i += 1; continue; }

      m = /^\s*[-*+]\s+\[([ xX])\]\s+(.*)$/.exec(line);
      if (m) { push('task', m[2], { checked: m[1].toLowerCase() === 'x' }); i += 1; continue; }

      m = /^\s*[-*+]\s+(.*)$/.exec(line);
      if (m) {
        const buf = [m[1]];
        i += 1;
        while (i < lines.length && /^\s{2,}\S/.test(lines[i])) {
          buf.push(lines[i].trim()); i += 1;
        }
        push('ul', buf.join('\n'));
        continue;
      }

      m = /^\s*(\d+)[.)]\s+(.*)$/.exec(line);
      if (m) {
        const buf = [m[2]];
        i += 1;
        while (i < lines.length && /^\s{2,}\S/.test(lines[i])) {
          buf.push(lines[i].trim()); i += 1;
        }
        push('ol', buf.join('\n'), { num: Number(m[1]) || 1 });
        continue;
      }

      if (/^\s*>\s?/.test(line)) {
        const buf = [];
        while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
          buf.push(lines[i].replace(/^\s*>\s?/, ''));
          i += 1;
        }
        push('quote', buf.join('\n'));
        continue;
      }

      // 表格 / HTML / 缩进代码：整段原样保留，避免保存时被改写
      if (RAW_START.test(line)) {
        const buf = [];
        while (i < lines.length && lines[i].trim()) { buf.push(lines[i]); i += 1; }
        push('raw', buf.join('\n'));
        continue;
      }

      // 普通段落：连续非空、且不是其它块开头的行合成一段
      const buf = [line];
      i += 1;
      while (i < lines.length && lines[i].trim()
             && !/^\s*(?:```|#{1,3}\s|[-*+]\s|>\s?|\d+[.)]\s)/.test(lines[i])
             && !RAW_START.test(lines[i])) {
        buf.push(lines[i]);
        i += 1;
      }
      push('p', buf.join('\n'));
    }
    if (!out.length) out.push({ id: nid(), type: 'p', text: '', meta: {} });
    return out;
  }

  function serialize(blocks) {
    const parts = [];
    (blocks || []).forEach((b) => {
      const text = String(b.text || '');
      switch (b.type) {
        case 'h1': parts.push('# ' + text); break;
        case 'h2': parts.push('## ' + text); break;
        case 'h3': parts.push('### ' + text); break;
        case 'ul': parts.push('- ' + text.replace(/\n/g, '\n  ')); break;
        case 'ol': parts.push(((b.meta && b.meta.num) || 1) + '. '
          + text.replace(/\n/g, '\n   ')); break;
        case 'task': parts.push('- [' + (b.meta && b.meta.checked ? 'x' : ' ') + '] ' + text
          .replace(/\n/g, '\n  ')); break;
        case 'quote': parts.push('> ' + text.replace(/\n/g, '\n> ')); break;
        case 'code':
          parts.push('```' + ((b.meta && b.meta.lang) || '') + '\n' + text + '\n```');
          break;
        case 'hr': parts.push('---'); break;
        case 'raw': parts.push(text); break;
        default: parts.push(text);
      }
    });
    // 连续列表项之间不留空行，其余块之间空一行（保持 Markdown 观感）
    let out = '';
    (blocks || []).forEach((b, i) => {
      if (i === 0) { out = parts[0]; return; }
      const prev = blocks[i - 1];
      const tight = LIST_TYPES[prev.type] && prev.type === b.type;
      out += (tight ? '\n' : '\n\n') + parts[i];
    });
    return out;
  }

  /* ---------- 编辑器 ---------- */
  function fragToMd(frag) {
    const box = document.createElement('div');
    box.appendChild(frag);
    return inlineToMd(box);
  }

  function BlockEditor(el, opts) {
    this.el = el;
    this.opts = opts || {};
    this.blocks = [];
    this.dragIndex = -1;
    this._slash = null;
    this._bind();
  }

  BlockEditor.prototype._bind = function () {
    const self = this;
    this.el.addEventListener('input', (e) => self._onInput(e));
    this.el.addEventListener('keydown', (e) => self._onKey(e));
    this.el.addEventListener('paste', (e) => self._onPaste(e));
    this.el.addEventListener('click', (e) => {
      const chk = e.target.closest('.nb-check');
      if (chk) {
        const b = self._blockOf(chk);
        if (b) {
          b.meta = b.meta || {};
          b.meta.checked = !b.meta.checked;
          chk.classList.toggle('on', b.meta.checked);
          self._emit();
        }
      }
      const plus = e.target.closest('.nb-plus');
      if (plus) {
        const b = self._blockOf(plus);
        const idx = self.blocks.indexOf(b);
        self.insertBlock(idx + 1, { type: 'p', text: '', meta: {} });
        self.focusBlock(idx + 1, false);
      }
      const link = e.target.closest('a.wikilink');
      if (link) {
        e.preventDefault();
        self._openWiki(link.getAttribute('data-note') || '');
      }
    });
    this.el.addEventListener('dragstart', (e) => {
      const blk = e.target.closest('.nb-block');
      if (!blk || !blk.getAttribute('draggable')) { e.preventDefault(); return; }
      self.dragIndex = self.blocks.findIndex((b) => b.id === blk.dataset.id);
      try { e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', ''); } catch (err) { /* 忽略 */ }
    });
    this.el.addEventListener('dragover', (e) => {
      if (self.dragIndex < 0) return;
      e.preventDefault();
      const blk = e.target.closest('.nb-block');
      self.el.querySelectorAll('.nb-block.drop-before,.nb-block.drop-after')
        .forEach((x) => x.classList.remove('drop-before', 'drop-after'));
      if (blk) {
        const r = blk.getBoundingClientRect();
        blk.classList.add(e.clientY < r.top + r.height / 2 ? 'drop-before' : 'drop-after');
      }
    });
    this.el.addEventListener('drop', (e) => {
      if (self.dragIndex < 0) return;
      e.preventDefault();
      const blk = e.target.closest('.nb-block');
      const to = blk ? self.blocks.findIndex((b) => b.id === blk.dataset.id) : self.blocks.length - 1;
      const r = blk ? blk.getBoundingClientRect() : null;
      let target = to;
      if (r && e.clientY > r.top + r.height / 2) target = to + 1;
      self.moveBlock(self.dragIndex, target);
      self.dragIndex = -1;
      self.el.querySelectorAll('.nb-block.drop-before,.nb-block.drop-after')
        .forEach((x) => x.classList.remove('drop-before', 'drop-after'));
    });
    this.el.addEventListener('dragend', () => { self.dragIndex = -1; });
  };

  BlockEditor.prototype._openWiki = function (name) {
    if (this.opts.onWikiLink) this.opts.onWikiLink(name);
  };

  BlockEditor.prototype._blockOf = function (node) {
    const blk = node.closest ? node.closest('.nb-block') : null;
    if (!blk) return null;
    return this.blocks.find((b) => b.id === blk.dataset.id) || null;
  };

  BlockEditor.prototype._emit = function () {
    if (this.opts.onChange) this.opts.onChange(serialize(this.blocks), this.blocks);
  };

  BlockEditor.prototype._body = function (block) {
    return this.el.querySelector('.nb-block[data-id="' + block.id + '"] .nb-body');
  };

  BlockEditor.prototype._sync = function (body) {
    const b = this._blockOf(body);
    if (!b) return;
    if (b.type === 'code' || b.type === 'raw') b.text = String(body.textContent || '').replace(/\n$/, '');
    else b.text = inlineToMd(body);
  };

  BlockEditor.prototype._onInput = function (e) {
    const body = e.target.closest && e.target.closest('.nb-body');
    if (!body) return;
    this._sync(body);
    this._emit();
    this._maybeSlash(body);
  };

  /* 输入 `/` 时弹出块类型菜单（只在空段落里） */
  BlockEditor.prototype._maybeSlash = function (body) {
    const b = this._blockOf(body);
    if (!b) return;
    const q = String(b.text || '');
    if (b.type !== 'p' || !/^\/[^\s]*$/.test(q)) { this._closeSlash(); return; }
    this._slash = { block: b, query: q.slice(1) };
    this._renderSlash(body);
  };

  const SLASH_ITEMS = [
    { type: 'p', label: '文本', hint: '普通段落' },
    { type: 'h1', label: '标题 1', hint: '# 大标题' },
    { type: 'h2', label: '标题 2', hint: '## 小标题' },
    { type: 'h3', label: '标题 3', hint: '### 更小标题' },
    { type: 'ul', label: '无序列表', hint: '- 项目' },
    { type: 'ol', label: '有序列表', hint: '1. 编号' },
    { type: 'task', label: '待办', hint: '- [ ] 勾选' },
    { type: 'quote', label: '引用', hint: '> 引用' },
    { type: 'code', label: '代码块', hint: '```' },
    { type: 'hr', label: '分割线', hint: '---' },
  ];

  BlockEditor.prototype._renderSlash = function (body) {
    let pop = this.el.querySelector('.nb-slash');
    if (!pop) {
      pop = document.createElement('div');
      pop.className = 'nb-slash';
      this.el.appendChild(pop);
    }
    const q = (this._slash.query || '').toLowerCase();
    const items = SLASH_ITEMS.filter((it) => !q
      || it.label.toLowerCase().indexOf(q) >= 0 || it.type.indexOf(q) >= 0);
    if (!items.length) { this._closeSlash(); return; }
    pop.innerHTML = items.map((it, i) =>
      '<div class="nb-slash-item' + (i === 0 ? ' on' : '') + '" data-type="' + it.type + '">'
      + '<b>' + esc(it.label) + '</b><span>' + esc(it.hint) + '</span></div>').join('');
    const rect = body.getBoundingClientRect();
    pop.style.left = Math.round(rect.left) + 'px';
    pop.style.top = Math.round(rect.bottom + 4) + 'px';
    pop.classList.remove('hidden');
    pop.querySelectorAll('.nb-slash-item').forEach((row) => {
      row.onmousedown = (ev) => {
        ev.preventDefault();
        this._applySlash(row.dataset.type);
      };
    });
  };

  BlockEditor.prototype._closeSlash = function () {
    this._slash = null;
    const pop = this.el.querySelector('.nb-slash');
    if (pop) pop.remove();
  };

  BlockEditor.prototype._applySlash = function (type) {
    const ctx = this._slash;
    this._closeSlash();
    if (!ctx) return;
    const b = ctx.block;
    const idx = this.blocks.indexOf(b);
    b.text = String(b.text || '').replace(/^\/[^\s]*/, '');
    if (type === 'hr') {
      b.type = 'hr';
      b.text = '';
      this.blocks.splice(idx + 1, 0, { id: nid(), type: 'p', text: '', meta: {} });
      this.render();
      this.focusBlock(idx + 1, false);
    } else {
      if (type === 'ol' && !(b.meta && b.meta.num)) b.meta = Object.assign({}, b.meta, { num: 1 });
      b.type = type;
      this.render();
      this.focusBlock(idx, false);
    }
    this._emit();
  };

  /* 计算光标前的 Markdown 与之后的 Markdown */
  function caretSplit(body) {
    const sel = document.getSelection();
    if (!sel || !sel.rangeCount) return null;
    const r = sel.getRangeAt(0);
    if (!body.contains(r.startContainer)) return null;
    const pre = r.cloneRange();
    pre.selectNodeContents(body);
    pre.setEnd(r.startContainer, r.startOffset);
    const post = r.cloneRange();
    post.selectNodeContents(body);
    post.setStart(r.endContainer, r.endOffset);
    return { before: fragToMd(pre.cloneContents()), after: fragToMd(post.cloneContents()) };
  }

  function caretAtStart(body) {
    const sel = document.getSelection();
    if (!sel || !sel.rangeCount) return false;
    const r = sel.getRangeAt(0);
    if (!r.collapsed) return false;
    const pre = r.cloneRange();
    pre.selectNodeContents(body);
    pre.setEnd(r.startContainer, r.startOffset);
    return pre.toString().length === 0;
  }

  function caretAtEnd(body) {
    const sel = document.getSelection();
    if (!sel || !sel.rangeCount) return false;
    const r = sel.getRangeAt(0);
    if (!r.collapsed) return false;
    const post = r.cloneRange();
    post.selectNodeContents(body);
    post.setStart(r.endContainer, r.endOffset);
    return post.toString().length === 0;
  }

  BlockEditor.prototype._onKey = function (e) {
    const body = e.target.closest && e.target.closest('.nb-body');
    if (!body) return;
    const b = this._blockOf(body);
    if (!b) return;
    const idx = this.blocks.indexOf(b);
    const mod = e.ctrlKey || e.metaKey;

    if (mod && !e.shiftKey && (e.key === 'b' || e.key === 'i')) {
      e.preventDefault();
      this.applyInline(e.key === 'b' ? 'bold' : 'italic');
      return;
    }
    if (mod && e.key === 's') { e.preventDefault(); if (this.opts.onSaveNow) this.opts.onSaveNow(); return; }

    if (this._slash && this.blocks[idx] === this._slash.block) {
      if (e.key === 'Escape') { e.preventDefault(); this._closeSlash(); return; }
      if (e.key === 'Enter') {
        e.preventDefault();
        const first = this.el.querySelector('.nb-slash-item');
        if (first) this._applySlash(first.dataset.type);
        return;
      }
    }

    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      if (b.type === 'code' || b.type === 'raw') return;      // 代码块内换行
      e.preventDefault();
      const parts = caretSplit(body);
      if (!parts) return;
      const wasList = !!LIST_TYPES[b.type];
      if (wasList && !parts.before.trim() && !parts.after.trim()) {
        b.type = 'p';
        b.text = '';
        this.render();
        this.focusBlock(idx, false);
        this._emit();
        return;
      }
      b.text = parts.before;
      const next = { id: nid(), type: wasList ? b.type : 'p', text: parts.after, meta: {} };
      if (b.type === 'task') next.meta.checked = false;
      if (b.type === 'ol') next.meta.num = Number((b.meta && b.meta.num) || 1) + 1;
      this.blocks.splice(idx + 1, 0, next);
      this.render();
      this.focusBlock(idx + 1, false);
      this._emit();
      return;
    }

    if (e.key === 'Backspace' && !e.isComposing && caretAtStart(body)) {
      if (b.type !== 'p') {
        e.preventDefault();
        b.type = 'p';
        this.render();
        this.focusBlock(idx, false);
        this._emit();
        return;
      }
      if (idx > 0) {
        e.preventDefault();
        const prev = this.blocks[idx - 1];
        if (prev.type === 'hr') {
          this.blocks.splice(idx - 1, 1);
          this.render();
          this.focusBlock(Math.max(0, idx - 1), true);
        } else {
          const carry = b.text;
          prev.text = (prev.text || '') + carry;
          if (b.type === 'code' || b.type === 'raw') prev.type = 'p';
          this.blocks.splice(idx, 1);
          this.render();
          this.focusBlock(idx - 1, true);
        }
        this._emit();
      }
      return;
    }

    if (e.key === 'ArrowUp' && caretAtStart(body) && idx > 0) {
      e.preventDefault();
      this.focusBlock(idx - 1, true);
      return;
    }
    if (e.key === 'ArrowDown' && caretAtEnd(body) && idx < this.blocks.length - 1) {
      e.preventDefault();
      this.focusBlock(idx + 1, false);
      return;
    }
    if (e.key === 'Tab') e.preventDefault();     // 不把焦点甩到页面别处
  };

  BlockEditor.prototype._onPaste = function (e) {
    const dt = e.clipboardData || window.clipboardData;
    if (!dt) return;
    const text = dt.getData('text/plain') || '';
    if (!text || !/[\n#>*\-]/.test(text)) return;      // 单行文本交给浏览器默认处理
    const body = e.target.closest && e.target.closest('.nb-body');
    const b = body ? this._blockOf(body) : null;
    if (!b) return;
    e.preventDefault();
    const blocks = parse(text);
    const idx = this.blocks.indexOf(b);
    const parts = caretSplit(body);
    if (parts) {
      b.text = parts.before + (blocks[0] ? blocks[0].text : '');
      if (blocks[0] && blocks[0].type !== 'p') b.type = blocks[0].type;
    }
    blocks.slice(1).forEach((nb, i) => this.blocks.splice(idx + 1 + i, 0, nb));
    this.render();
    this.focusBlock(idx + (blocks.length - 1), true);
    this._emit();
  };

  BlockEditor.prototype.applyInline = function (cmd) {
    const body = document.activeElement && document.activeElement.closest
      ? document.activeElement.closest('.nb-body') : null;
    if (!body) return;
    if (cmd === 'wikilink') {
      const name = (this.opts.askWikiLink ? this.opts.askWikiLink() : '') || '';
      if (!name) return;
      document.execCommand('insertHTML', false,
        '<a class="wikilink" data-note="' + esc(name) + '" contenteditable="false">' + esc(name) + '</a>');
    } else if (cmd === 'code') {
      const sel = document.getSelection();
      const text = sel && sel.rangeCount ? sel.toString() : '';
      document.execCommand('insertHTML', false, '<code>' + esc(text || '代码') + '</code>');
    } else if (cmd === 'strike') {
      document.execCommand('strikeThrough', false, null);
    } else {
      document.execCommand(cmd, false, null);
    }
    this._sync(body);
    this._emit();
  };

  BlockEditor.prototype.setType = function (type) {
    const body = document.activeElement && document.activeElement.closest
      ? document.activeElement.closest('.nb-body') : null;
    const b = body ? this._blockOf(body) : null;
    if (!b) return;
    const idx = this.blocks.indexOf(b);
    if (type === 'hr') {
      b.type = 'hr';
      b.text = '';
      this.blocks.splice(idx + 1, 0, { id: nid(), type: 'p', text: '', meta: {} });
      this.render();
      this.focusBlock(idx + 1, false);
    } else {
      if (type === 'ol' && !(b.meta && b.meta.num)) b.meta = Object.assign({}, b.meta, { num: 1 });
      b.type = type;
      this.render();
      this.focusBlock(idx, false);
    }
    this._emit();
  };

  BlockEditor.prototype.render = function () {
    const self = this;
    this.el.innerHTML = this.blocks.map((b) => self._html(b)).join('');
  };

  BlockEditor.prototype._html = function (b) {
    const type = b.type || 'p';
    let inner = '';
    if (type === 'hr') {
      return '<div class="nb-block nb-hr" data-id="' + b.id + '" data-type="hr">'
        + this._gutterHtml() + '<div class="nb-hrline"></div></div>';
    }
    if (type === 'code' || type === 'raw') {
      inner = esc(b.text || '');
    } else if (type === 'task') {
      inner = '<span class="nb-check' + (b.meta && b.meta.checked ? ' on' : '')
        + '" contenteditable="false"></span>' + inlineToHtml(b.text || '');
    } else {
      inner = inlineToHtml(b.text || '');
    }
    const lang = type === 'code'
      ? '<span class="nb-lang">' + esc((b.meta && b.meta.lang) || 'text') + '</span>' : '';
    const num = type === 'ol' ? ' data-num="' + Number((b.meta && b.meta.num) || 1) + '"' : '';
    return '<div class="nb-block nb-' + type + '"' + num + ' data-id="' + b.id + '" data-type="' + type + '">'
      + this._gutterHtml() + lang
      + '<div class="nb-body" contenteditable="true" spellcheck="false">' + inner + '</div></div>';
  };

  BlockEditor.prototype._gutterHtml = function () {
    return '<div class="nb-gutter" contenteditable="false">'
      + '<button type="button" class="nb-handle" title="拖动排序">⋮⋮</button>'
      + '<button type="button" class="nb-plus" title="在下方插入">＋</button></div>';
  };

  BlockEditor.prototype.setMarkdown = function (md) {
    this.blocks = parse(md);
    this.render();
  };

  BlockEditor.prototype.getMarkdown = function () {
    return serialize(this.blocks);
  };

  BlockEditor.prototype.getBlocks = function () { return this.blocks; };

  BlockEditor.prototype.insertBlock = function (at, block) {
    const b = { id: block.id || nid(), type: block.type || 'p',
                text: block.text || '', meta: block.meta || {} };
    this.blocks.splice(Math.max(0, Math.min(at, this.blocks.length)), 0, b);
    this.render();
  };

  BlockEditor.prototype.moveBlock = function (from, to) {
    if (from < 0 || from >= this.blocks.length) return;
    let at = to;
    if (at > from) at -= 1;
    const [b] = this.blocks.splice(from, 1);
    this.blocks.splice(Math.max(0, Math.min(at, this.blocks.length)), 0, b);
    this.render();
    this._emit();
  };

  BlockEditor.prototype.focusBlock = function (idx, atEnd) {
    const b = this.blocks[idx];
    if (!b) return;
    const body = this._body(b);
    if (!body) return;
    body.focus();
    const r = document.createRange();
    r.selectNodeContents(body);
    r.collapse(!atEnd);
    const sel = document.getSelection();
    if (!sel) return;
    sel.removeAllRanges();
    sel.addRange(r);
  };

  BlockEditor.prototype.scrollToBlock = function (idx) {
    const b = this.blocks[idx];
    if (!b) return;
    const el = this.el.querySelector('.nb-block[data-id="' + b.id + '"]');
    if (el) el.scrollIntoView({ block: 'center', behavior: 'smooth' });
  };

  BlockEditor.prototype.mount = function () {
    this.render();
    return this;
  };

  window.NoteBlocks = {
    parse: parse,
    serialize: serialize,
    inlineToHtml: inlineToHtml,
    inlineToMd: inlineToMd,
    BlockEditor: BlockEditor,
    mount: function (el, opts) { return new BlockEditor(el, opts).mount(); },
  };
})();
