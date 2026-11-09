// -*- coding: utf-8 -*-
/* Markdown 语法检查（笔记库 + IDE 共用）
 *
 * 纯函数、零依赖，浏览器与 Node 都能跑：
 *   MDLint.lint(text, { noteNames }) -> [{ rule, severity, line, col, endLine, endCol, message, fix }]
 *   MDLint.fixAll(text, diags)       -> { text, fixed }
 *
 * 只报「规则能确定」的问题；强调/公式这类误报率高的降为 info。
 * 代码块（围栏）与行内代码里的内容不参与其它规则。
 * fix 只做行级、幂等的安全替换；结构性问题（围栏未闭合）只提示，不猜着补。
 */
(function (root) {
  'use strict';

  var FENCE_RE = /^ {0,3}(`{3,}|~{3,})(.*)$/;
  var HEADING_RE = /^( {0,3})(#{1,6})([^#\s].*)$/;
  var FULLWIDTH_HASH_RE = /^( {0,3})(＃+)(.*)$/;
  var LIST_RE = /^(\s*)([-*+])(\s+)(.*)$/;
  var TABLE_SEP_RE = /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/;

  function splitLines(text) {
    return String(text == null ? '' : text).replace(/\r\n?/g, '\n').split('\n');
  }

  /* 行内代码替换成等长空白：后面数 * $ 时不受代码里的符号干扰 */
  function stripInlineCode(line) {
    return String(line).replace(/`[^`]*`/g, function (m) {
      return new Array(m.length + 1).join(' ');
    });
  }

  function countChar(s, ch) {
    var n = 0;
    for (var i = 0; i < s.length; i++) {
      if (s[i] === '\\') { i++; continue; }
      if (s[i] === ch) n++;
    }
    return n;
  }

  /* 表格按未转义的 | 切单元格（忽略首尾竖线） */
  function splitCells(line) {
    var s = String(line == null ? '' : line).trim();
    if (s.charAt(0) === '|') s = s.slice(1);
    if (s.charAt(s.length - 1) === '|' && s.charAt(s.length - 2) !== '\\') s = s.slice(0, -1);
    var out = [], cur = '';
    for (var i = 0; i < s.length; i++) {
      var c = s[i];
      if (c === '\\' && s[i + 1] === '|') { cur += '|'; i++; continue; }
      if (c === '|') { out.push(cur); cur = ''; continue; }
      cur += c;
    }
    out.push(cur);
    return out;
  }

  function nameSetOf(v) {
    if (!v) return null;
    var set = Object.create(null);
    if (typeof v.forEach === 'function') {
      v.forEach(function (x) { set[String(x).toLowerCase()] = 1; });
    }
    return set;
  }

  function lint(text, opts) {
    opts = opts || {};
    var src = String(text == null ? '' : text).replace(/\r\n?/g, '\n');
    var lines = src.split('\n');
    // 末尾换行会split出最后一个空串：那不算一行内容
    if (lines.length && lines[lines.length - 1] === '') lines.pop();
    var names = nameSetOf(opts.noteNames);
    var out = [];

    function add(o) { out.push(o); }
    function at(i, col, rule, severity, message, fix) {
      add({
        rule: rule, severity: severity, line: i, col: col || 1,
        endLine: i, endCol: (col || 1) + 1, message: message, fix: fix || null,
      });
    }

    var fence = null;             // { line, ch, len }
    var blankRun = 0;
    var prevHeading = 0;
    var listMarker = Object.create(null);   // 缩进 -> 首个符号
    var tableCols = 0;

    for (var i = 0; i < lines.length; i++) {
      var raw = lines[i];

      if (fence) {
        var close = raw.match(FENCE_RE);
        if (close && close[1].charAt(0) === fence.ch && close[1].length >= fence.len &&
            /^\s*$/.test(close[2])) {
          fence = null;
        }
        continue;                 // 代码块内部不跑其它规则
      }

      var open = raw.match(FENCE_RE);
      if (open) {
        var info = String(open[2] || '').trim();
        fence = { line: i, ch: open[1].charAt(0), len: open[1].length };
        if (!info) at(i, 1, 'fence-lang', 'info', '代码块没有标注语言（如 ```js / ```text）');
        tableCols = 0;
        continue;
      }

      if (!raw.trim()) {
        blankRun++;
        if (blankRun >= 2) at(i, 1, 'blank-lines', 'warning', '连续空行（最多保留 1 行）', { kind: 'drop' });
        tableCols = 0;
        continue;
      }
      blankRun = 0;

      // 全角＃当标题
      var fw = raw.match(FULLWIDTH_HASH_RE);
      if (fw) at(i, fw[1].length + 1, 'fullwidth-hash', 'warning', '标题用了全角「＃」，应为半角 #', { kind: 'fullwidth-hash' });

      // 标题：# 后缺空格
      var hm = raw.match(HEADING_RE);
      if (hm) {
        at(i, hm[1].length + hm[2].length + 1, 'heading-space', 'warning', '标题的 # 后面缺少空格', { kind: 'heading-space' });
      }
      // 标题级别跳跃（含「# 后没空格」的标题，按行首 1–6 个 # 计）
      var lvMatch = raw.match(/^ {0,3}(#{1,6})(?!#)/);
      if (lvMatch) {
        var lv = lvMatch[1].length;
        if (prevHeading && lv > prevHeading + 1) {
          at(i, 1, 'heading-jump', 'warning', '标题级别从 H' + prevHeading + ' 跳到 H' + lv + '（建议逐级）');
        }
        prevHeading = lv;
      }

      // 行尾空白：2 个空格是 Markdown 的强制换行，不报
      var trail = raw.match(/[ \t]+$/);
      if (trail && (trail[0].length === 1 || trail[0].indexOf('\t') >= 0)) {
        at(i, raw.length - trail[0].length + 1, 'trailing-space', 'warning', '行尾空白（2 个空格表示强制换行，如非本意请删除）',
          { kind: 'trailing-space' });
      }

      // 列表符号混用：同一缩进统一用一个符号。
      // 列表之间夹着段落 / 标题时算新的一组，否则整篇里两个独立列表用不同符号会互相误报。
      var lm = raw.match(LIST_RE);
      if (lm) {
        var indent = lm[1].replace(/\t/g, '    ').length;
        var marker = lm[2];
        if (!(indent in listMarker)) listMarker[indent] = marker;
        else if (listMarker[indent] !== marker) {
          at(i, lm[1].length + 1, 'list-marker', 'warning',
            '列表符号与同级其它项不一致（统一用 ' + listMarker[indent] + '）',
            { kind: 'list-marker', marker: listMarker[indent] });
        }
      } else {
        listMarker = Object.create(null);
      }

      // 表格：表头 + 分隔行之后，每行列数应与表头一致
      if (tableCols) {
        if (raw.indexOf('|') < 0) tableCols = 0;
        else {
          var cc = splitCells(raw).length;
          if (cc !== tableCols) {
            at(i, 1, 'table-cols', 'warning', '表格列数不一致（表头 ' + tableCols + ' 列，这行 ' + cc + ' 列）');
          }
          continue;
        }
      } else if (raw.indexOf('|') >= 0 && i + 1 < lines.length && TABLE_SEP_RE.test(lines[i + 1])) {
        tableCols = splitCells(raw).length;
        continue;
      }

      var plain = stripInlineCode(raw);

      // 双链：只在笔记库检查（有 noteNames 才跑）
      if (names) {
        var wiki = /\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]/g;
        var wm;
        while ((wm = wiki.exec(plain))) {
          var nm = String(wm[1] || '').trim();
          if (!nm) continue;
          var key = nm.toLowerCase();
          if (!names[key] && !names[key.split('/').pop()]) {
            at(i, wm.index + 1, 'wikilink', 'warning', '双链指向不存在的笔记：' + nm);
          }
        }
      }

      // 行内强调 / 行内代码 / 公式：容易误报，只给 info。
      // 列表行的行首符号本身就是一个 *，先跳过，避免「* b」被当成没闭合的强调。
      if (!lm) {
        if (countChar(plain, '*') % 2 === 1) {
          at(i, 1, 'emphasis', 'info', '「*」数量不成对，加粗/斜体可能没闭合');
        }
        if (countChar(plain, '`') % 2 === 1) {
          at(i, 1, 'inline-code', 'info', '反引号不成对，行内代码可能没闭合');
        }
        var mathLine = plain.replace(/\$\$/g, '');
        if (countChar(mathLine, '$') % 2 === 1) {
          at(i, 1, 'math', 'info', '「$」数量不成对，公式可能没闭合');
        }
      }
    }

    if (fence) {
      add({
        rule: 'fence-open', severity: 'error', line: fence.line, col: 1,
        endLine: fence.line, endCol: 2, message: '代码块没有闭合（缺少同长度的 ``` 或 ~~~）', fix: null,
      });
    }

    if (src && !/\n$/.test(src)) {
      add({
        rule: 'eof-newline', severity: 'warning', line: Math.max(0, lines.length - 1), col: 1,
        endLine: Math.max(0, lines.length - 1), endCol: 2, message: '文件末尾缺少换行',
        fix: { kind: 'eof-newline' },
      });
    } else if (/\n\n$/.test(src)) {
      add({
        rule: 'eof-blank', severity: 'warning', line: Math.max(0, lines.length - 1), col: 1,
        endLine: Math.max(0, lines.length - 1), endCol: 2, message: '文件末尾有多余空行',
        fix: { kind: 'eof-blank' },
      });
    }

    out.sort(function (a, b) { return a.line - b.line || (a.col || 1) - (b.col || 1); });
    return out;
  }

  function fixAll(text, diags) {
    var src = String(text == null ? '' : text).replace(/\r\n?/g, '\n');
    var lines = src.split('\n');
    var drop = Object.create(null);
    var fixed = 0;
    var eof = false;

    (diags || []).forEach(function (d) {
      var f = d && d.fix;
      if (!f) return;
      var i = d.line;
      if (f.kind === 'eof-newline' || f.kind === 'eof-blank') { eof = true; fixed++; return; }
      if (i == null || i < 0 || i >= lines.length) return;

      if (f.kind === 'drop') {
        if (lines[i].trim() !== '' || drop[i]) return;
        drop[i] = 1;
        fixed++;
      } else if (f.kind === 'heading-space') {
        var m = lines[i].match(HEADING_RE);
        if (!m) return;
        lines[i] = m[1] + m[2] + ' ' + m[3];
        fixed++;
      } else if (f.kind === 'fullwidth-hash') {
        var m2 = lines[i].match(FULLWIDTH_HASH_RE);
        if (!m2) return;
        var rest = m2[3] || '';
        lines[i] = m2[1] + new Array(m2[2].length + 1).join('#') + (rest && !/^\s/.test(rest) ? ' ' : '') + rest;
        fixed++;
      } else if (f.kind === 'trailing-space') {
        var t = lines[i].replace(/[ \t]+$/, '');
        if (t === lines[i]) return;
        lines[i] = t;
        fixed++;
      } else if (f.kind === 'list-marker') {
        var m3 = lines[i].match(LIST_RE);
        if (!m3) return;
        lines[i] = m3[1] + f.marker + m3[3] + m3[4];
        fixed++;
      }
    });

    var out = lines.filter(function (_, i) { return !drop[i]; }).join('\n');
    if (eof) out = out.replace(/\n+$/, '') + '\n';
    return { text: out, fixed: fixed };
  }

  var API = { lint: lint, fixAll: fixAll };
  if (typeof module === 'object' && module.exports) module.exports = API;
  if (root) root.MDLint = API;
})(typeof window !== 'undefined' ? window : (typeof globalThis !== 'undefined' ? globalThis : this));
