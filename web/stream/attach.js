'use strict';

/* 流式对话 · attach.js
   附件与引用：图片 / 文档 / 文本块 / 引文条
   原 web/stream.js 拆分而来：按 index.html 中的顺序加载，共享全局作用域。 */

/* ============================================================
   图片：先暂存为「待发送附件」，可继续输入文字后一起发送
   ============================================================ */
// 文档标签后缀：优先显示字数，其次文件体积
function docChipSuffix(dc) {
  if (!dc) return '';
  if (dc.chars) return '（' + dc.chars + ' 字）';
  if (dc.size) return '（' + Math.max(1, Math.round(dc.size / 1024)) + ' KB）';
  return '';
}

/* 把解析状态写回「最后一条用户消息」里的文档标签（多附件按 index 定位）。
   后端事件只带 index，不依赖 DOM 顺序重排；找不到对应标签时静默跳过。 */
function docChipRender(obj, phase) {
  const chips = (typeof lastUserDocChips === 'function') ? lastUserDocChips() : [];
  const idx = Math.max(0, Number(obj && obj.index) || 0);
  const chip = chips[idx] || (chips.length === 1 ? chips[0] : null);
  if (!chip) return;
  const name = (obj && obj.name) || chip.dataset.name || '附件';
  chip.dataset.name = name;
  chip.classList.remove('parsing', 'warn', 'err');
  chip.classList.remove('hidden');
  let suffix = '';
  if (phase === 'parsing') {
    suffix = '（正在解析…）';
    chip.classList.add('parsing');
    chip.title = '正在解析，请稍候';
  } else if (phase === 'progress') {
    const stageLabel = ({ text: '读取', render: '渲染', ocr: '识别' })[obj.stage] || '解析';
    suffix = obj.total
      ? ('（' + stageLabel + ' ' + obj.done + '/' + obj.total + ' 页）')
      : '（正在解析…）';
    chip.classList.add('parsing');
    chip.title = '正在解析，请稍候';
  } else if (phase === 'error') {
    suffix = '（解析失败）';
    chip.classList.add('err');
    chip.title = ((obj && obj.error) || '解析失败') + ' · 点击重试';
  } else {
    suffix = '（' + ((obj && obj.chars) || 0) + ' 字）';
    if (obj && obj.warn) chip.classList.add('warn');
    chip.title = (obj && obj.warn) || '点击预览';
  }
  chip.innerHTML = icon('attach') + ' ' + escapeHtml(name) + suffix;
}

const DOC_MAX_BYTES = 2 * 1024 * 1024 * 1024; // 单个附件宽松上限 2GB（防误拖超大文件；实际仍受浏览器内存约束）
const MAX_PENDING_DOCS = 10;                  // 一次最多带 10 个文档（再多会让请求体和上下文一起失控）
const MAX_PENDING_IMAGES = 4;                 // 一次最多带 4 张图，避免请求体过大
// 粘贴长文本：只在输入框上方生成一个轻量文本块（不生成文件、不上传、不走文档解析）。
// 发送时按顺序并入消息正文；短文本仍走原生粘贴。
const PASTE_BLOCK_MIN = 800;                  // 超过这个字数才收成文本块
const MAX_PENDING_PASTES = 6;                 // 一次最多带 6 段，避免输入框上方堆满
let pasteSeq = 0;                             // 文本块自增 id（删除 / 预览用）
let attachSeq = 0;                            // 连续粘贴多张时保持顺序
let attachEpoch = 0;                          // 清空 / 发送后作废还在读取的附件
let imageLoads = 0;                           // 正在读取、还没 onload 的图片数

function attachImageBlob(blob) {
  if (!blob) return;
  const loaded = (state.pendingImages || []).length;
  if (loaded + imageLoads >= MAX_PENDING_IMAGES) {
    toast('一次最多发送 ' + MAX_PENDING_IMAGES + ' 张图片', 'error');
    return;
  }
  const seq = ++attachSeq;
  const epoch = attachEpoch;
  imageLoads++;
  const reader = new FileReader();
  const done = () => { imageLoads = Math.max(0, imageLoads - 1); };
  reader.onload = () => {
    done();
    if (epoch !== attachEpoch) return;        // 读取期间已清空 / 已发送
    const url = String(reader.result || '');
    const mime = (url.match(/^data:([^;]+);/) || [])[1] || 'image/png';
    const item = {
      mime,
      b64: url.split(',')[1] || '',
      url,
      seq,
    };
    state.pendingImages = (state.pendingImages || []).concat(item)
      .sort((a, b) => (a.seq || 0) - (b.seq || 0));
    state.pendingImage = state.pendingImages[0];
    renderAttach();
  };
  reader.onerror = () => { done(); toast('图片读取失败', 'error'); };
  reader.readAsDataURL(blob);
}

// 文档附件：读成 base64 暂存，正文由后端在发送时抽取（界面只显示文件名）
// 允许多个：连续选择 / 拖拽 / 粘贴都追加到列表，不再互相覆盖。
// 读取期间先放一个 loading 占位项，让用户立刻看到附件并看到读取百分比。
function findPendingDoc(seq) {
  return (state.pendingDocs || []).find((d) => d.seq === seq) || null;
}

function attachDocBlob(file) {
  if (!file) return;
  if (file.size > DOC_MAX_BYTES) { toast('附件太大（上限 2GB）', 'error'); return; }
  if (!file.size) { toast('附件是空文件：' + (file.name || '未命名'), 'error'); return; }
  if ((state.pendingDocs || []).length >= MAX_PENDING_DOCS) {
    toast('一次最多发送 ' + MAX_PENDING_DOCS + ' 个文档', 'error');
    return;
  }
  const name = file.name || '附件';
  const seq = ++attachSeq;
  const epoch = attachEpoch;
  const item = {
    name,
    mime: file.type || '',
    b64: '',
    size: file.size || 0,
    seq,
    loading: true,
    progress: 0,
  };
  state.pendingDocs = (state.pendingDocs || []).concat(item)
    .sort((a, b) => (a.seq || 0) - (b.seq || 0));
  renderAttach();
  const reader = new FileReader();
  reader.onprogress = (ev) => {
    if (epoch !== attachEpoch) return;
    if (!ev.lengthComputable || !ev.total) return;
    const cur = findPendingDoc(seq);
    if (!cur || !cur.loading) return;
    cur.progress = Math.min(99, Math.round(ev.loaded * 100 / ev.total));
    renderAttach();
  };
  reader.onload = () => {
    if (epoch !== attachEpoch) return;        // 读取期间已清空 / 已发送
    const cur = findPendingDoc(seq);
    if (!cur) return;                         // 读取期间被用户移除了
    const s = String(reader.result || '');
    cur.b64 = s.split(',')[1] || '';
    cur.mime = cur.mime || (s.match(/^data:([^;]+);/) || [])[1] || 'application/octet-stream';
    cur.loading = false;
    cur.progress = 100;
    renderAttach();
    const list = state.pendingDocs.filter((d) => d.b64);
    const n = list.length;
    toast(n > 1 ? ('已附带 ' + name + '（共 ' + n + ' 个文档）')
                : ('已附带 ' + name + '，可继续输入文字后一起发送'));
  };
  reader.onerror = () => {
    removePendingDoc(seq);
    toast('附件读取失败', 'error');
  };
  reader.readAsDataURL(file);
}

function removePendingDoc(seq) {
  state.pendingDocs = (state.pendingDocs || []).filter((d) => d.seq !== seq);
  renderAttach();
}

/* 粘贴长文本 → 轻量文本块：只存原文，发送时并入消息正文。
   不生成 File、不上传、不占附件通道，也不用「还原」。 */
function shouldPasteAsBlock(text) {
  return String(text || '').length >= PASTE_BLOCK_MIN;
}

function attachPastedBlock(text) {
  const s = String(text || '');
  if (!s) return;
  if ((state.pendingPastes || []).length >= MAX_PENDING_PASTES) {
    toast('一次最多添加 ' + MAX_PENDING_PASTES + ' 段粘贴文本', 'error');
    return;
  }
  state.pendingPastes = (state.pendingPastes || []).concat({ id: ++pasteSeq, text: s });
  renderAttach();
}

function removePendingPaste(id) {
  state.pendingPastes = (state.pendingPastes || []).filter((p) => p.id !== id);
  renderAttach();
}

function previewPendingPaste(p) {
  if (!p) return;
  const label = '粘贴文本 · ' + p.text.length + ' 字.txt';
  docPreviewOpen(new Blob([p.text], { type: 'text/plain;charset=utf-8' }), label, 'text/plain');
}

function removePendingImage(index) {
  state.pendingImages = (state.pendingImages || []).filter((_, i) => i !== index);
  state.pendingImage = state.pendingImages[0] || null;
  renderAttach();
}

function renderAttach() {
  const imgs = (state.pendingImages && state.pendingImages.length)
    ? state.pendingImages
    : (state.pendingImage ? [state.pendingImage] : []);
  const docs = state.pendingDocs || [];
  const pastes = state.pendingPastes || [];
  const bar = $('#attach-bar');
  if (!bar) return;
  bar.classList.toggle('hidden', !imgs.length && !docs.length && !pastes.length);
  const box = $('#attach-thumbs');
  if (box) {
    box.classList.toggle('hidden', !imgs.length);
    box.innerHTML = '';
    imgs.forEach((im, i) => {
      const wrap = document.createElement('span');
      wrap.className = 'attach-thumb-wrap';
      if (im.url) {
        const thumb = document.createElement('img');
        thumb.className = 'attach-thumb';
        thumb.src = im.url;
        thumb.alt = '待发送图片 ' + (i + 1);
        thumb.title = '点击查看大图';
        thumb.style.cursor = 'zoom-in';
        thumb.onclick = () => imgZoomOpen(im.url, '待发送图片');
        wrap.appendChild(thumb);
      } else {
        // 沿用原消息的图片：缩略图还在从本地读，先占位，避免出现破图
        const ph = document.createElement('span');
        ph.className = 'attach-thumb';
        ph.title = '原消息里的图片（沿用）';
        wrap.appendChild(ph);
      }
      const x = document.createElement('button');
      x.type = 'button';
      x.className = 'attach-thumb-x';
      x.title = '移除这张图片';
      x.textContent = '✕';
      x.onclick = (e) => { e.stopPropagation(); removePendingImage(i); };
      wrap.appendChild(x);
      box.appendChild(wrap);
    });
  }
  const dbox = $('#attach-docs');
  if (dbox) {
    dbox.classList.toggle('hidden', !docs.length);
    dbox.innerHTML = '';
    docs.forEach((d) => {
      const chip = document.createElement('span');
      const loading = !!d.loading;
      chip.className = 'attach-doc' + (loading ? ' loading' : '');
      chip.title = loading
        ? ('正在读取附件… ' + (d.progress || 0) + '%')
        : ('点击预览：' + d.name);
      const suffix = loading ? ('（读取 ' + (d.progress || 0) + '%）') : docChipSuffix(d);
      chip.innerHTML = icon('attach')
        + '<span class="attach-doc-name">' + escapeHtml(d.name) + suffix + '</span>';
      const x = document.createElement('button');
      x.type = 'button';
      x.className = 'attach-doc-x';
      x.title = '移除这个附件';
      x.textContent = '✕';
      x.onclick = (e) => { e.stopPropagation(); removePendingDoc(d.seq); };
      chip.appendChild(x);
      if (!loading) chip.onclick = () => (d.reuse
        ? docPreviewStored(d, String((state.sessions && state.sessions.current) || ''))
        : docPreviewPending(d));
      dbox.appendChild(chip);
    });
  }
  const pbox = $('#attach-pastes');
  if (pbox) {
    pbox.classList.toggle('hidden', !pastes.length);
    pbox.innerHTML = '';
    pastes.forEach((p, i) => {
      const item = document.createElement('span');
      item.className = 'attach-paste';
      item.title = '点击预览这段文本';
      item.innerHTML = icon('attach')
        + '<span class="attach-paste-name">粘贴文本' + (pastes.length > 1 ? ' ' + (i + 1) : '')
        + ' · ' + p.text.length + ' 字</span>';
      const x = document.createElement('button');
      x.type = 'button';
      x.className = 'attach-paste-x';
      x.title = '移除这段文本';
      x.textContent = '✕';
      x.onclick = (e) => { e.stopPropagation(); removePendingPaste(p.id); };
      item.appendChild(x);
      item.onclick = () => previewPendingPaste(p);
      pbox.appendChild(item);
    });
  }
  const txt = $('#attach-text');
  if (txt) {
    const parts = [];
    if (imgs.length) parts.push(imgs.length > 1 ? (imgs.length + ' 张图片') : '图片');
    const readyDocs = docs.filter((d) => !d.loading);
    if (readyDocs.length) {
      const reused = readyDocs.filter((d) => d.reuse).length;
      const fresh = readyDocs.length - reused;
      if (fresh) {
        parts.push(fresh > 1
          ? (fresh + ' 个文档（发送时自动抽取正文）')
          : '文档（发送时自动抽取正文）');
      }
      if (reused) parts.push(reused > 1 ? ('沿用原附件 ' + reused + ' 个') : '沿用原附件');
    }
    const loadingDocs = docs.length - readyDocs.length;
    if (loadingDocs) parts.push(loadingDocs + ' 个附件正在读取');
    if (pastes.length) parts.push(pastes.length + ' 段粘贴文本');
    txt.textContent = parts.length
      ? ('已附带 ' + parts.join(' + ') + ' · 可继续输入文字后一起发送')
      : '';
  }
  syncSendBtn();
}

function clearAttach() {
  attachEpoch++;
  state.pendingImage = null;
  state.pendingImages = [];
  state.pendingDocs = [];
  state.pendingPastes = [];
  const box = $('#attach-thumbs');
  if (box) box.innerHTML = '';
  const dbox = $('#attach-docs');
  if (dbox) dbox.innerHTML = '';
  const pbox = $('#attach-pastes');
  if (pbox) pbox.innerHTML = '';
  const thumb = $('#attach-thumb');
  if (thumb) thumb.removeAttribute('src');   // 兼容旧 DOM
  renderAttach();
}

/* 修改历史消息：把原消息里的附件带回输入框。
   只引用已落盘的文件（不重新读取 / 上传 / 解析），发送时按 reuse 列表沿用；
   用户可以直接点 × 移除，也能再追加新附件。 */
function restoreEditAttachments(msg) {
  if (!msg) return;
  const sid = String((state.sessions && state.sessions.current) || '');
  const imgs = (Array.isArray(msg.images) && msg.images.length)
    ? msg.images : (msg.image ? [msg.image] : []);
  imgs.forEach((im) => {
    const file = String((im && (im.file || im.name)) || '');
    if (!file) return;
    const item = { mime: im.mime || '', url: '', seq: ++attachSeq,
                   reuse: file, name: im.name || file };
    state.pendingImages = (state.pendingImages || []).concat(item);
    if (typeof attachBlobUrl === 'function') {
      attachBlobUrl(sid, file).then((url) => {
        if (!(state.pendingImages || []).includes(item)) return;   // 期间已被移除
        item.url = url;
        renderAttach();
      }).catch(() => { /* 文件不在了：保留占位，发送时服务端会忽略 */ });
    }
  });
  const docs = (Array.isArray(msg.docs) && msg.docs.length)
    ? msg.docs : (msg.doc ? [msg.doc] : []);
  docs.forEach((d) => {
    const file = String((d && (d.file || d.name)) || '');
    if (!file) return;
    state.pendingDocs = (state.pendingDocs || []).concat({
      name: d.name || file, file: file, mime: d.mime || '', size: d.size || 0,
      chars: d.chars || 0, b64: '', seq: ++attachSeq, reuse: file,
    });
  });
  if (imgs.length || docs.length) {
    state.pendingImage = (state.pendingImages || [])[0] || null;
    renderAttach();
  }
}

/* 退出「修改」：只清掉从原消息带回来的附件，用户自己新加的保留 */
function dropReusedAttachments() {
  const before = (state.pendingImages || []).length + (state.pendingDocs || []).length;
  state.pendingImages = (state.pendingImages || []).filter((it) => !it.reuse);
  state.pendingDocs = (state.pendingDocs || []).filter((it) => !it.reuse);
  state.pendingImage = state.pendingImages[0] || null;
  if ((state.pendingImages || []).length + (state.pendingDocs || []).length !== before) {
    renderAttach();
  }
}

/* ---------- 引用块（「添加到对话」）----------
   只在输入框上方显示一个「选中文本 N」小块；发送时作为 quotes 随请求带走，
   由后端拼进本轮 prompt，不写入消息正文。 */
const QUOTE_MAX_ONE = 4000;      // 与后端 _quotes_block 的上限保持一致

function addQuote(text, label) {
  const t = String(text || '').trim();
  if (!t) return false;
  const clipped = t.length > QUOTE_MAX_ONE ? t.slice(0, QUOTE_MAX_ONE) + '…（已截断）' : t;
  const name = String(label || '').trim().slice(0, 24);
  state.pendingQuotes = (state.pendingQuotes || []).concat({ text: clipped, label: name });
  renderQuoteBar();
  return true;
}

function removeQuote(index) {
  state.pendingQuotes = (state.pendingQuotes || []).filter((_, i) => i !== index);
  renderQuoteBar();
}

function clearQuotes() {
  if (!(state.pendingQuotes || []).length) return;
  state.pendingQuotes = [];
  renderQuoteBar();
}

function renderQuoteBar() {
  const bar = $('#quote-bar');
  if (!bar) return;
  const list = state.pendingQuotes || [];
  bar.classList.toggle('hidden', !list.length);
  bar.innerHTML = '';
  list.forEach((q, i) => {
    const chip = document.createElement('span');
    chip.className = 'quote-chip';
    chip.title = q.text.slice(0, 200) + (q.text.length > 200 ? '…' : '');
    const label = q.label || ('选中文本 ' + (i + 1));
    chip.innerHTML = icon('chat') + '<span class="quote-label">' + escapeHtml(label) + '</span>';
    const x = document.createElement('button');
    x.type = 'button';
    x.className = 'quote-x';
    x.title = '移除这条引用';
    x.textContent = '✕';
    x.onclick = () => removeQuote(i);
    chip.appendChild(x);
    bar.appendChild(chip);
  });
}
