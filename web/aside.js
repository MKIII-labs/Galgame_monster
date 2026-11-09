/* ============================================================
   旁注提问（侧边对话）
   ------------------------------------------------------------
   在主对话流式输出时，划选一段文字就地提问：
   - 独立接口 /api/aside/stream，不写会话、不调工具、不打断主流；
   - 选区在点击「问问」的瞬间冻结（主流还在追加 token，DOM 会变）；
   - 线程按主会话分组持久化在服务端（data/aside.json），卡片顶部的
     标签页可切换 / 新建 / 改名 / 删除，刷新或切会话都不丢；
   - Solo 与 IDE 模式都能用（IDE 右侧仍保留对话栏）。
   ============================================================ */
'use strict';

(function () {
  const MAX_SELECTION = 4000;
  const QUICK = [
    ['解释这段', '用中文解释这段内容在说什么，必要时先直译关键句。'],
    ['翻译成中文', '把这段完整翻译成自然的中文，保留代码 / 命令 / 专有名词。'],
    ['这步在做什么', '解释这一步在做什么、为什么要这么做，以及可能的坑。'],
  ];

  // Solo / IDE 都有 #chat（IDE 是右侧对话栏），两种模式都能旁注
  function canAsk() {
    if (typeof state === 'undefined' || !state) return false;
    const mode = state.uiMode || 'solo';
    if (mode !== 'solo' && mode !== 'ide') return false;
    const box = document.getElementById('chat');
    return !!(box && box.offsetParent !== null);
  }

  function nodeEl(node) {
    if (!node) return null;
    return node.nodeType === 1 ? node : node.parentElement;
  }

  /* ---------------- 选区工具条 ----------------
     浮层由 web/selkit.js 统一渲染；这里只注册「对话」适配器。 */

  function copyText(text) {
    const t = String(text || '');
    if (!t) return;
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(t).then(
        () => toast('已复制'),
        () => toast('复制失败', 'error'));
      return;
    }
    const ta = document.createElement('textarea');
    ta.value = t;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); toast('已复制'); }
    catch (e) { toast('复制失败', 'error'); }
    ta.remove();
  }

  function registerSelection() {
    if (!window.SelKit) return;
    window.SelKit.register({
      id: 'chat',
      roots: () => [document.getElementById('chat')],
      maxText: MAX_SELECTION,
      capture: (range) => {
        if (!canAsk()) return null;
        const row = nodeEl(range.startContainer);
        if (!row || !row.closest('.msg[data-idx]')) return null;
        const frozen = freezeSelection(range);
        if (!frozen) return null;
        return {
          text: frozen.text,
          truncated: !!frozen.truncated,
          rect: range.getBoundingClientRect(),
          label: '对话 · #' + frozen.msg_index,
          raw: frozen,
        };
      },
      actions: [
        {
          id: 'ask', label: '提问', icon: 'chat', primary: true,
          run: (ctx) => {
            const raw = ctx.raw;
            if (!window.AskKit) return toast('问答面板未加载，请刷新页面', 'error');
            window.AskKit.open({
              scope: 'session:' + sidNow(),
              source: 'chat',
              selection: {
                kind: 'chat', text: raw.text, before: raw.before, after: raw.after,
                msg_index: raw.msg_index, role: raw.role, segments: raw.segments,
                truncated: raw.truncated, message_text: raw.message_text,
              },
              includeMessage: !!raw.truncated,
              messageText: raw.message_text || '',
              anchor: 'msg:' + raw.msg_index + '|' + String(raw.text || '').slice(0, 60),
              label: '对话 · #' + raw.msg_index,
              quote: raw.text,
              title: '旁注',
              rect: ctx.rect,
              quick: QUICK,
              onQuote: (text) => addQuote(text, '对话 · #' + raw.msg_index),
            });
          },
        },
        {
          id: 'quote', label: '引用', icon: 'note', primary: true,
          run: (ctx) => {
            if (typeof addQuote !== 'function') return toast('这段内容无法引用', 'error');
            addQuote(ctx.raw.text, '对话 · #' + ctx.raw.msg_index);
            toast('已添加到对话');
          },
        },
        {
          id: 'copy', label: '复制', icon: 'copy',
          run: (ctx) => copyText(ctx.raw.copy_text || ctx.raw.text),
        },
      ],
    });
  }

  /* ---------------- 冻结选区（主流还在追加 token） ---------------- */
  /* 一条消息里可选的正文节点：AI 回复被提问卡切开后会有多段（row._secs），
     只看 row._content 会把前面几段误判成「不在选区里」→ 点了「问问」却没反应。 */
  function rowContents(row) {
    if (!row) return [];
    if (row.classList.contains('user')) return [row];
    const list = [];
    (row._secs || []).forEach((el) => {
      if (el && el.isConnected) list.push(el);
    });
    if (row._content && list.indexOf(row._content) < 0) list.push(row._content);
    if (!list.length) list.push(row);
    return list;
  }

  // 把 range 夹到某个节点内；返回夹好的 Range（失败返回 null）
  function rangePieceIn(node, range) {
    const piece = document.createRange();
    try {
      piece.selectNodeContents(node);
      if (node.contains(range.startContainer)) piece.setStart(range.startContainer, range.startOffset);
      if (node.contains(range.endContainer)) piece.setEnd(range.endContainer, range.endOffset);
    } catch (e) {
      return null;
    }
    return piece;
  }

  // 给模型看的提问文本：压成一行（便于检索 / 定位锚点）
  function rangeTextIn(node, range) {
    const piece = rangePieceIn(node, range);
    return piece ? String(piece.toString() || '').replace(/\s+/g, ' ').trim() : '';
  }

  // 复制用：保留换行与连续空格
  function rangeTextExact(node, range) {
    const piece = rangePieceIn(node, range);
    if (!piece) return '';
    if (window.SelKit && typeof window.SelKit.plainText === 'function') {
      return window.SelKit.plainText(piece);
    }
    return String(piece.toString() || '');
  }

  function freezeSelection(range) {
    const box = document.getElementById('chat');
    if (!box) return null;
    const segments = [];
    box.querySelectorAll('.msg[data-idx]').forEach((row) => {
      let picked = '';
      let copied = '';
      rowContents(row).forEach((node) => {
        try {
          if (!range.intersectsNode(node)) return;
        } catch (e) {
          return;
        }
        const text = rangeTextIn(node, range);
        if (text) picked = picked ? (picked + ' ' + text) : text;
        const exact = rangeTextExact(node, range);
        if (exact) copied = copied ? (copied + '\n' + exact) : exact;
      });
      if (!picked) return;
      segments.push({
        idx: Number(row.dataset.idx),
        role: row.classList.contains('user') ? 'user' : 'ai',
        text: picked,
        copy_text: copied || picked,
      });
    });
    // 兜底：选区确实落在某条消息里（validRange 已校验），但选的是思考过程 /
    // 工具输出等非正文节点。此时直接取选区本身，别把「无法提问」做成死路。
    if (!segments.length) {
      const el = nodeEl(range.startContainer);
      const row = el && el.closest ? el.closest('.msg[data-idx]') : null;
      const text = String(range.toString() || '').replace(/\s+/g, ' ').trim();
      const exact = (window.SelKit && typeof window.SelKit.plainText === 'function')
        ? window.SelKit.plainText(range)
        : String(range.toString() || '');
      if (row && text) {
        segments.push({
          idx: Number(row.dataset.idx),
          role: row.classList.contains('user') ? 'user' : 'ai',
          text: text,
          copy_text: exact || text,
        });
      }
    }
    if (!segments.length) return null;
    if (segments.length > 3) segments.length = 3;

    let text = segments.map((s) => s.text).join('\n');
    // 复制单独一份原文：不受提问文本的 4000 字截断影响
    const copyText = segments.map((s) => s.copy_text || s.text).join('\n');
    let truncated = false;
    if (text.length > MAX_SELECTION) {
      text = text.slice(0, MAX_SELECTION);
      truncated = true;
    }
    const firstRow = box.querySelector('.msg[data-idx="' + segments[0].idx + '"]');
    const lastRow = box.querySelector('.msg[data-idx="' + segments[segments.length - 1].idx + '"]');
    const before = rowAround(firstRow, segments[0].text, true);
    const after = rowAround(lastRow, segments[segments.length - 1].text, false);
    return {
      text: text,
      copy_text: copyText,
      before: before,
      after: after,
      truncated: truncated,
      msg_index: segments[0].idx,
      role: segments[0].role,
      message_text: String((firstRow && firstRow._raw) || '').slice(0, MAX_SELECTION * 2),
      segments: segments,
    };
  }

  // 取同一行里片段前后的文字，帮模型消歧；找不到就留空（不影响提问）
  function rowAround(row, segmentText, isBefore) {
    if (!row) return '';
    // 流式行的 _raw 可能还没更新，优先用已经渲染出来的可见文本
    const raw = String((row._content && row._content.innerText) || row._raw || '');
    const flat = raw.replace(/\s+/g, ' ');
    const probe = segmentText.slice(0, 60);
    const at = probe ? flat.indexOf(probe) : -1;
    if (at < 0) return '';
    return isBefore
      ? flat.slice(Math.max(0, at - 600), at).trim()
      : flat.slice(at + segmentText.length, at + segmentText.length + 600).trim();
  }

  function sidNow() {
    return String((typeof state !== 'undefined' && state.sessions
      && state.sessions.current) || '');
  }

  /* ---------------- 问答卡片（AskKit 统一渲染） ---------------- */

  function init() {
    registerSelection();
  }

  /* 编辑器里选中代码 → 同一张卡片多轮追问（IDE 内联问答）。
     code: {path, language, text, before, after, startLine, endLine} */
  window.asideOpenCode = function (code, rect) {
    code = code || {};
    const raw = String(code.text || '');
    if (!raw.trim()) return toast('先选中要问的代码', 'error');
    if (!window.AskKit) return toast('问答面板未加载，请刷新页面', 'error');
    const text = raw.slice(0, MAX_SELECTION);
    const path = String(code.path || '');
    const startLine = code.startLine || 0;
    const endLine = code.endLine || 0;
    window.AskKit.open({
      scope: 'session:' + sidNow(),
      source: 'chat',
      selection: {
        kind: 'code', text: text,
        before: String(code.before || '').slice(-4000),
        after: String(code.after || '').slice(0, 2000),
        path: path, language: String(code.language || ''),
        start_line: startLine, end_line: endLine,
      },
      anchor: 'code:' + path + ':' + startLine + '-' + endLine + '|' + text.slice(0, 40),
      label: '代码 · ' + (path || '编辑器'),
      quote: text,
      title: '旁注',
      rect: rect || null,
      onQuote: (t) => addQuote(t, '代码 · ' + (path || '编辑器')),
    });
  };

  // 切会话：收起卡片（线程按会话分组，下次打开时重新拉取）
  window.asideOnSessionChange = function (sid) {   // eslint-disable-line no-unused-vars
    if (window.AskKit) window.AskKit.close();
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
