'use strict';

/* ============================================================
   Markdown 渲染（自实现，避免额外依赖）
   ============================================================ */
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function mathHtml(tex, display) {
  if (window.katex) {
    try {
      return katex.renderToString(tex, {
        displayMode: display, throwOnError: false, strict: false,
      });
    } catch (e) { /* 落到下面 */ }
  }
  return '<code class="inline math-pending" data-tex="' + escapeHtml(tex) +
    '" data-display="' + (display ? '1' : '0') + '">' + escapeHtml(tex) + '</code>';
}

/* ---------- AI 可视内容：svg / chart(echarts) / mermaid ----------
   只在围栏**闭合后**才转成富媒体块；流式过程中未闭合的围栏仍按普通代码块渲染，
   否则每个 token 都会重建一次图表（又闪又卡）。真正的渲染在 web/rich-media.js。 */
const RICH_LANGS = { svg: 'svg', chart: 'chart', echarts: 'chart', mermaid: 'mermaid' };
const RICH_TITLES = { svg: '图形', chart: '图表', mermaid: '流程图' };

function richBlockHtml(kind, code) {
  return '<div class="rich-media" data-rich="' + kind + '">' +
    '<div class="rich-media-head"><span class="rich-media-title">' + RICH_TITLES[kind] + '</span>' +
    '<span class="rich-media-tools">' +
    '<button class="rich-btn" type="button" data-rich-download>下载</button>' +
    '<button class="rich-btn" type="button" data-rich-source>源码</button>' +
    '</span></div>' +
    '<div class="rich-media-body"><div class="rich-media-loading">渲染中…</div></div>' +
    '<pre class="rich-media-src hidden">' + escapeHtml(code) + '</pre>' +
    '</div>';
}

function codeBlockHtml(lang, code, closed) {
  const l = String(lang || '').trim().toLowerCase();
  const kind = RICH_LANGS[l];
  if (closed !== false && typeof kind === 'string') return richBlockHtml(kind, code);
  let body;
  if (window.hljs && lang && hljs.getLanguage(lang)) {
    try { body = hljs.highlight(code, { language: lang }).value; } catch (e) { body = escapeHtml(code); }
  } else if (window.hljs) {
    try { body = hljs.highlightAuto(code).value; } catch (e) { body = escapeHtml(code); }
  } else {
    body = escapeHtml(code);
  }
  const lines = String(code).split('\n').length;
  const fold = lines > 28;   // 超长代码块默认折叠，避免刷屏
  return '<div class="code-block' + (fold ? ' folded' : '') + '"><div class="code-head"><span>' +
    escapeHtml(lang || 'text') + '</span><span class="code-tools">' +
    (fold ? '<button class="copy" data-fold data-total="' + lines + '">展开全部（' + lines + ' 行）</button>' : '') +
    '<button class="copy" data-copy>复制</button></span></div><pre><code class="hljs" data-lang="' +
    escapeHtml(lang || '') + '">' + body + '</code></pre></div>';
}

function safeHref(href) {
  // 注意：inlineFmt 收到的文本已经过 escapeHtml，这里不能再转义一次，
  // 否则 URL 里的 & 会变成 &amp;amp;。
  const s = String(href || '').trim();
  if (/^(https?:|mailto:)/i.test(s)) return s;
  if (/^(#|\/|\.\/|\.\.\/)/.test(s)) return s;
  return '#';                       // javascript: / data: 等一律降级
}

function inlineFmt(s) {
  // 双链 [[目标]] / [[目标|别名]] / [[目标#锚点]]（放在最前面，避免被其它规则干扰）
  s = s.replace(/\[\[([^\[\]\n]+?)\]\]/g, (m, raw) => {
    const seg = String(raw).split('|');
    const head = seg[0];
    const target = head.split('#')[0].trim();
    const anchor = (head.split('#')[1] || '').trim();
    const label = (seg[1] || head).trim();
    if (!target) return m;
    return '<a class="wikilink" data-note="' + target
      + '" data-anchor="' + anchor + '">' + label + '</a>';
  });
  s = s.replace(/`([^`]+)`/g, (m, code) =>
    '<code class="inline">' + escapeHtml(code) + '</code>');
  s = s.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/(^|[^*\w])\*([^*\n]+)\*/g, '$1<em>$2</em>');
  // 图片：远程地址直接渲染；沙箱相对路径先占位，由 rich-media.js 带鉴权换成 blob
  s = s.replace(/!\[([^\]\n]*)\]\(([^)\s]+)\)/g, (m, alt, src) => {
    if (/^data:/i.test(src)) return m;                 // 不接受 base64 内联
    if (/^(https?:)?\/\//i.test(src) || /^\//.test(src)) {
      const href = safeHref(src);
      return href === '#' ? m
        : '<img class="md-img" src="' + href + '" alt="' + alt
          + '" loading="lazy" decoding="async">';
    }
    return '<span class="md-img md-img-sandbox" data-src="' + src +
      '" data-alt="' + alt + '">图片加载中…</span>';
  });
  s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g,
    (m, label, href) => '<a href="' + safeHref(href)
      + '" target="_blank" rel="noreferrer">' + label + '</a>');
  return s;
}

function renderMarkdown(src) {
  if (!src) return '';
  const key = String(src);
  const hit = _mdCache.get(key);
  if (hit !== undefined) {                    // 命中：挪到队尾，维持 LRU 顺序
    _mdCache.delete(key);
    _mdCache.set(key, hit);
    return hit;
  }
  const out = _renderMarkdown(key);
  _mdCache.set(key, out);
  if (_mdCache.size > MD_CACHE_MAX) {
    _mdCache.delete(_mdCache.keys().next().value);
  }
  return out;
}

/* 带兜底的渲染：任何异常 / 空结果都退回纯文本，保证消息不会因为一条坏 Markdown 整块消失。
   聊天、流式收尾、历史重渲染都走这里；renderMarkdown 保留给纯函数测试与预览。 */
function safeRenderMarkdown(src) {
  const text = String(src == null ? '' : src);
  if (!text) return '';
  try {
    const out = renderMarkdown(text);
    if (out && out.trim()) return out;
  } catch (e) {
    if (window.console && console.error) console.error('[markdown] 渲染失败，已降级为纯文本', e);
  }
  return '<div class="md-fallback">' + escapeHtml(text).replace(/\n/g, '<br>') + '</div>';
}

// 纯函数，可安全缓存：长会话反复切换 / 展开更早时避免重复解析同一段文本
const _mdCache = new Map();
const MD_CACHE_MAX = 300;

// 模型偶尔不写 ```tool 围栏，直接在正文末尾吐 {"calls":[...]}。
// 这类 JSON 不会被当成工具调用，只会把整段源码露给用户 → 从末尾摘掉。
// 只在能识别出 calls[].tool 时才摘；代码块里的 JSON 已在上一步换成占位符，不受影响。
function stripTrailingBareCalls(text) {
  const s = String(text || '');
  for (let i = s.length - 1; i >= 0; i--) {
    if (s[i] !== '{') continue;
    let obj;
    try { obj = JSON.parse(s.slice(i)); } catch (e) { continue; }
    if (!obj || !Array.isArray(obj.calls) || !obj.calls.length) continue;
    const ok = obj.calls.every((c) =>
      c && typeof c === 'object' && typeof c.tool === 'string');
    if (!ok) continue;
    return s.slice(0, i).replace(/\s+$/, '');
  }
  return s;
}

// 从 start 处的 { 或 [ 起做括号配平，返回闭合符号之后的位置；失败返回 -1。
// 只区分「字符串 / 转义」，够用来切出一段嵌在正文里的 JSON。
function _jsonSpanEnd(s, start) {
  if (start >= s.length || (s[start] !== '{' && s[start] !== '[')) return -1;
  const stack = [];
  let inStr = false, esc = false;
  for (let i = start; i < s.length; i++) {
    const ch = s[i];
    if (inStr) {
      if (esc) esc = false;
      else if (ch === '\\') esc = true;
      else if (ch === '"') inStr = false;
      continue;
    }
    if (ch === '"') inStr = true;
    else if (ch === '{') stack.push('}');
    else if (ch === '[') stack.push(']');
    else if (ch === '}' || ch === ']') {
      if (!stack.length || stack[stack.length - 1] !== ch) return -1;
      stack.pop();
      if (!stack.length) return i + 1;
    }
  }
  return -1;
}

function _allCalls(arr) {
  return Array.isArray(arr) && arr.length > 0 && arr.every((c) =>
    c && typeof c === 'object' && typeof c.tool === 'string');
}

// 形状要宽松：{"calls":[...]}、{"tool_calls":[...]}、单个 {"tool":...}、以及顶层数组
function _isCallsJson(raw) {
  let obj;
  try { obj = JSON.parse(raw); } catch (e) { return false; }
  if (Array.isArray(obj)) return _allCalls(obj);
  if (!obj || typeof obj !== 'object') return false;
  if (_allCalls(obj.calls)) return true;
  if (_allCalls(obj.tool_calls)) return true;
  return typeof obj.tool === 'string';
}

// ---------------------------------------------------------------
// 伪工具标签清洗：与后端 llm._strip_unknown_tags 同一套规则
// 判定只看「名字族」：名字里带 tool，或属于少数已知的调用协议名。
// 不枚举写法（<tool calls> / <tool_calls> / <tool calls here> / <tool调用> /
// <tool_result> … 名字里始终带 tool），也不用「未知标签一律删」——
// 真实数据里那样会吃掉 SVG 的 <linearGradient> / <ellipse> 和占位符 <YYYYMMDD>。
// 必须在「抽代码围栏 / 行内代码」之前执行 —— 否则模型把标签写进反引号里就清不到。
// ---------------------------------------------------------------
const _PROTO_NAME_RE = /tool|parameter|invoke|function_calls|thinking|antml/i;

// 名字里允许 CJK 后缀：模型写过 `<tool调用>`（中间没有空格）
const _TAG_RE = /<\s*(\/?)\s*([A-Za-z][\w:.\-\u4e00-\u9fff]*)((?:\s[^<>]*?)?)\s*(\/?)\s*>/g;

// 配对内容超过这个长度就只删标签：模型偶尔把整篇答案包进标签里，不能连正文一起删
const _PROTO_PAIR_MAX = 2000;
// 这些语言标注的围栏是「协议」而不是「用户要看的代码」，照常清洗
const _PROTO_FENCE_LANGS = new Set(['tool', 'tool_call', 'tool_calls', 'calls', 'function_calls']);

// 「带语言标注、且不是工具协议」的围栏 = 用户要看的代码，跳过清洗
function _protectedFences(s) {
  const spans = [];
  const re = /^[ \t]*(```|~~~)[ \t]*([\w+-]*)[ \t]*$/gm;
  let m;
  while ((m = re.exec(s))) {
    const cre = new RegExp('^[ \\t]*' + m[1] + '[ \\t]*$', 'gm');
    cre.lastIndex = m.index + m[0].length;
    const c = cre.exec(s);
    const end = c ? c.index + c[0].length : s.length;
    const lang = (m[2] || '').toLowerCase();
    if (lang && !_PROTO_FENCE_LANGS.has(lang)) spans.push([m.index, end]);
    re.lastIndex = end;
  }
  return spans;
}

function stripUnknownTags(text) {
  const s = String(text || '');
  if (s.indexOf('<') < 0) return s;
  const keep = _protectedFences(s);
  const inside = (i) => keep.some((sp) => i >= sp[0] && i < sp[1]);
  let out = '', pos = 0, m;
  _TAG_RE.lastIndex = 0;
  while ((m = _TAG_RE.exec(s))) {
    if (m.index < pos || inside(m.index)) continue;
    const name = m[2];
    if (!_PROTO_NAME_RE.test(name)) continue;
    if ((m[3] || '').indexOf('://') >= 0) continue;
    out += s.slice(pos, m.index) + '\n';
    pos = m.index + m[0].length;
    if (m[1] || m[4]) continue;                  // 闭合 / 自闭合：只丢标签本身
    const safe = name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const cm = s.slice(pos).match(new RegExp('<\\s*/\\s*' + safe + '\\s*>', 'i'));
    if (cm && cm.index <= _PROTO_PAIR_MAX) pos += cm.index + cm[0].length;
  }
  out += s.slice(pos);
  return out;
}

// 与后端 llm._CALL_TAG_RE 同源：这些是「能定位到调用块」的信号标签
const _TAG_SRC = '<\\s*\\/?\\s*(?:tool|tool_calls?|tool\\s+calls|tool调用|parameter|invoke|function_calls)'
  + '(?:\\s[^<>]{0,60})?\\s*\\/?>';
const _TOOL_TAG_ONE = new RegExp(_TAG_SRC, 'i');
const _TOOL_TAG_ALL = new RegExp(_TAG_SRC, 'gi');

// 模型把工具调用写成 <tool calls>{...}</tool calls> / <parameter name="calls">[...]</parameter>。
// 这类块经常出现在正文**中间**，闭合标签还时有时无 —— 所以边界只能由后面的 JSON 决定：
// 标签后面紧跟可解析的调用 JSON 就整块隐去，否则只丢掉孤立标签、正文保持原样。
function stripToolCallText(text) {
  let s = _stripLabelledCalls(String(text || ''));
  s = _stripBareCallBlocks(s);
  return s.replace(_TOOL_TAG_ALL, '\n');
}

function _stripLabelledCalls(s) {
  const re = new RegExp(_TAG_SRC, 'gi');
  let out = '', pos = 0, m;
  while ((m = re.exec(s))) {
    if (m.index < pos) continue;
    out += s.slice(pos, m.index);
    let p = m.index + m[0].length;
    for (;;) {                          // 允许连续标签（多种写法混用）
      while (p < s.length && /\s/.test(s[p])) p++;
      const t = _TOOL_TAG_ONE.exec(s.slice(p));
      if (!t || t.index !== 0) break;
      p += t[0].length;
    }
    let end = -1;
    if (s[p] === '{' || s[p] === '[') {
      end = _jsonSpanEnd(s, p);
      if (end < 0 || !_isCallsJson(s.slice(p, end))) end = -1;
    }
    if (end > 0) {
      pos = end;
      for (;;) {                        // 顺手吞掉紧随的闭合标签（只认 </）
        let q = pos;
        while (q < s.length && /\s/.test(s[q])) q++;
        if (s.slice(q, q + 2) !== '</') break;
        const t = _TOOL_TAG_ONE.exec(s.slice(q));
        if (!t || t.index !== 0) break;
        pos = q + t[0].length;
      }
      re.lastIndex = pos;
    } else {
      pos = m.index + m[0].length;
      re.lastIndex = pos;
    }
  }
  out += s.slice(pos);
  return out;
}

// 行首独立成段的裸调用 JSON（一个标签都没写）：{"calls":[...]} / [{"tool":...}]
function _stripBareCallBlocks(s) {
  let out = '', pos = 0;
  const re = /(^|\n)([ \t]*)([\{\[])/g;
  let m;
  while ((m = re.exec(s))) {
    const p = m.index + m[1].length + m[2].length;
    if (p < pos) continue;
    const end = _jsonSpanEnd(s, p);
    if (end < 0 || !_isCallsJson(s.slice(p, end))) continue;
    out += s.slice(pos, p);
    pos = end;
    re.lastIndex = end;
  }
  out += s.slice(pos);
  return out;
}

function _renderMarkdown(src) {
  if (!src) return '';
  let text = String(src).replace(/\r\n?/g, '\n');

  // 0) 未知标签清洗必须最先做：抽完代码块 / 行内代码再清，反引号里的标签就再也清不到了。
  //    带语言标注的代码围栏（```js / ```python …）会跳过，保护用户要看的代码。
  text = stripUnknownTags(text);

  // 1) 抽出围栏代码块（closed 决定能不能当富媒体渲染）
  const codes = [];
  text = text.replace(/```([^\n`]*)\n?([\s\S]*?)(```|$)/g, (m, lang, code, close) => {
    codes.push({
      lang: (lang || '').trim(),
      code: code.replace(/\n$/, ''),
      closed: !!close,
    });
    return '\u0000C' + (codes.length - 1) + '\u0000';
  });

  // 1.5) 抽出行内代码：先保护 `...`，否则里面的 $...$ / $$...$$ 会被误当成公式
  const inlines = [];
  text = text.replace(/`([^`\n]+)`/g, (m, code) => {
    inlines.push(code);
    return '\u0000I' + (inlines.length - 1) + '\u0000';
  });

  // 1.6) <tool calls> / <tool_calls> 包裹的调用块：正文中间也会出现，整块隐去
  text = stripToolCallText(text);

  // 1.7) 清掉剩余的伪工具标签；顺手补换行，避免 `</tool>## 标题` 这种标题不生效
  text = text.replace(_TOOL_TAG_ALL, '\n');

  // 1.8) 末尾裸工具 JSON（没有 ```tool 围栏）→ 摘掉，别把源码露给用户
  text = stripTrailingBareCalls(text);

  // 2) 抽出数学公式（$$ / \[ 为块级，$ / \( 为行内）
  const maths = [];
  const pushMath = (tex, display) => {
    maths.push({ tex, display });
    return '\u0000M' + (maths.length - 1) + '\u0000';
  };
  text = text.replace(/\$\$([\s\S]+?)\$\$/g, (m, t) => pushMath(t, true));
  text = text.replace(/\\\[([\s\S]+?)\\\]/g, (m, t) => pushMath(t, true));
  text = text.replace(/\\\(([\s\S]+?)\\\)/g, (m, t) => pushMath(t, false));
  text = text.replace(/\$([^\n$]+?)\$/g, (m, t) => pushMath(t, false));

  // 3) 转义剩余文本
  text = escapeHtml(text);

  // 4) 块级解析
  const lines = text.split('\n');
  let html = '';
  let i = 0;
  const isBlank = (l) => /^\s*$/.test(l);

  while (i < lines.length) {
    let line = lines[i];

    if (isBlank(line)) { i++; continue; }

    // 标题
    let m = line.match(/^(#{1,6})\s+(.*)$/);
    if (m) {
      const lv = Math.min(m[1].length, 6);
      html += '<h' + lv + '>' + inlineFmt(m[2]) + '</h' + lv + '>';
      i++; continue;
    }

    // 分割线
    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) { html += '<hr>'; i++; continue; }

    // 引用
    if (/^\s*&gt;\s?/.test(line)) {
      const buf = [];
      while (i < lines.length && /^\s*&gt;\s?/.test(lines[i])) {
        buf.push(lines[i].replace(/^\s*&gt;\s?/, ''));
        i++;
      }
      html += '<blockquote>' + inlineFmt(buf.join('<br>')) + '</blockquote>';
      continue;
    }

    // 表格
    if (line.indexOf('|') >= 0 && i + 1 < lines.length &&
        /^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(lines[i + 1])) {
      const splitRow = (r) => r.replace(/^\s*\|/, '').replace(/\|\s*$/, '').split('|').map((c) => c.trim());
      const head = splitRow(line);
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].indexOf('|') >= 0 && !isBlank(lines[i])) {
        rows.push(splitRow(lines[i])); i++;
      }
      html += '<table><thead><tr>' + head.map((c) => '<th>' + inlineFmt(c) + '</th>').join('') +
        '</tr></thead><tbody>' +
        rows.map((r) => '<tr>' + head.map((_, k) => '<td>' + inlineFmt(r[k] || '') + '</td>').join('') + '</tr>').join('') +
        '</tbody></table>';
      continue;
    }

    // 列表
    if (/^\s*([-*+]|\d+\.)\s+/.test(line)) {
      const ordered = /^\s*\d+\.\s+/.test(line);
      const items = [];
      while (i < lines.length && /^\s*([-*+]|\d+\.)\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*([-*+]|\d+\.)\s+/, ''));
        i++;
      }
      const tag = ordered ? 'ol' : 'ul';
      html += '<' + tag + '>' + items.map((t) => '<li>' + inlineFmt(t) + '</li>').join('') + '</' + tag + '>';
      continue;
    }

    // 段落
    const para = [];
    while (i < lines.length && !isBlank(lines[i]) &&
           !/^(#{1,6})\s+/.test(lines[i]) &&
           !/^\s*&gt;\s?/.test(lines[i]) &&
           !/^\s*([-*+]|\d+\.)\s+/.test(lines[i]) &&
           !/^\s*([-*_])(\s*\1){2,}\s*$/.test(lines[i])) {
      para.push(lines[i]); i++;
      if (i < lines.length && lines[i].indexOf('|') >= 0 && /^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(lines[i] || '')) break;
    }
    html += '<p>' + inlineFmt(para.join('<br>')) + '</p>';
  }

  // 5) 还原行内代码、代码块与公式
  html = html.replace(/\u0000I(\d+)\u0000/g, (m, idx) => {
    const code = inlines[+idx];
    return code === undefined ? '' : '<code class="inline">' + escapeHtml(code) + '</code>';
  });
  html = html.replace(/\u0000C(\d+)\u0000/g, (m, idx) => {
    const c = codes[+idx];
    return c ? codeBlockHtml(c.lang, c.code, c.closed) : '';
  });
  html = html.replace(/\u0000M(\d+)\u0000/g, (m, idx) => {
    const k = maths[+idx];
    return k ? mathHtml(k.tex, k.display) : '';
  });
  return html;
}

// KaTeX 加载完成后清掉旧缓存：缓存里可能存的是 math-pending 的 HTML
function clearMarkdownCache() {
  _mdCache.clear();
}
