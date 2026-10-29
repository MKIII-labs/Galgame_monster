const fs = require('fs');
const vm = require('vm');

const code = fs.readFileSync('web/markdown.js', 'utf8');
const ctx = { window: {}, console };
vm.createContext(ctx);
vm.runInContext(code, ctx);

let failed = 0;
function check(name, input, includes, excludes) {
  const out = ctx.renderMarkdown(input);
  const okInc = (includes || []).every((s) => out.includes(s));
  const okExc = (excludes || []).every((s) => !out.includes(s));
  if (!okInc || !okExc) {
    failed++;
    console.log('[FAIL]', name);
    console.log(out.slice(0, 500));
  } else {
    console.log('[OK]  ', name);
  }
  return out;
}

check('inline math', '公式 $a+b$ 结束', ['math-pending', 'data-tex="a+b"'], []);
check('display math', '$$x^2$$', ['math-pending', 'data-display="1"'], []);
check('paren math', '\\(a+b\\)', ['math-pending', 'data-tex="a+b"'], []);
check('bracket math', '\\[a+b\\]', ['math-pending', 'data-display="1"'], []);
check('math in inline code is code', '`$$\\tag*{$\\blacksquare$}$$`',
  ['<code class="inline">'], ['math-pending']);
check('table with math',
  '| $t$ | 差 $=0.01e^t$ |\n|---|---|\n| 0 | 0.01 |',
  ['<table>', 'math-pending'], []);
check('display math with inner dollars', '$$\\tag*{$\\blacksquare$}$$',
  ['math-pending', 'data-tex="\\tag*{$\\blacksquare$}"'], []);
check('pseudo tool tag before heading',
  '<tool调用>\n\n</tool>## 每日 note 方案\n\n内容',
  ['<h2>每日 note 方案</h2>'], ['<tool调用>', '## 每日 note 方案']);
check('bare tool calls hidden',
  '正文\n\n{"calls":[{"tool":"calendar.create","args":{"title":"x"}}]}',
  ['正文'], ['calls', 'calendar.create']);
check('tagged tool calls hidden in middle',
  '先看格式。\n\n<tool calls>\n{"calls":[{"tool":"list_dir","args":{"path":"."}}]}\n</tool calls>\n\n等待返回。',
  ['先看格式', '等待返回'], ['calls', 'tool calls', 'list_dir']);
check('tagged tool calls without closer keeps body',
  'A<tool calls>{"calls":[{"tool":"read_file","args":{"path":"a.md"}}]}\n\n中间正文',
  ['A', '中间正文'], ['calls', 'read_file', 'tool calls']);
check('tagged tool calls underscore variant',
  '<tool_calls>{"calls":[{"tool":"list_dir","args":{}}]}</tool_calls>正文',
  ['正文'], ['calls', 'tool_calls']);
check('tool tag in prose only',
  '讲解一下 <tool calls> 这个标签是什么。',
  ['讲解一下', '这个标签是什么'], ['<tool calls>']);
check('parameter tag with array hidden',
  '先说明。\n\n<parameter name="calls">[{"tool":"read_file","args":{"path":"a.md"}}]</parameter>\n\n继续。',
  ['先说明', '继续'], ['calls', 'read_file', 'parameter']);
check('self closing tool tag hidden',
  '已经写入。\n\n<tool calls/>\n\n现在读回确认。',
  ['已经写入', '现在读回确认'], ['<tool']);
check('tool tag with stray quote hidden',
  '<tool calls">\n{"calls":[{"tool":"delete_file","args":{"path":"x"}}]}\n</tool calls>\n结束。',
  ['结束'], ['calls', 'delete_file', '<tool']);
check('bare call block at line start hidden',
  '直接写脚本：\n\n{"tool":"write_file","args":{"path":"a.js","content":"1"}}\n\n然后运行。',
  ['直接写脚本', '然后运行'], ['write_file']);
// 第 8 代伪装：占位符标签 + 模型自演工具结果
check('placeholder tool tag in backticks hidden',
  '先确认行号。\n\n`<tool calls here>`\n\n再读原文。',
  ['先确认行号', '再读原文'], ['tool calls', '&lt;tool']);
check('fake tool_result dropped with content',
  '<tool_result>thinking\nI am checking the file.\n</tool_result>\n\n工具位置已确认。',
  ['工具位置已确认'], ['tool_result', 'checking the file']);
check('tool调用 tag without space hidden',
  '<tool调用>\n\n正文结论。', ['正文结论'], ['tool调用']);
check('html whitelist keeps normal tags',
  '这是 <b>加粗</b> 与 <span class="x">行内</span>。',
  ['&lt;b&gt;加粗&lt;/b&gt;', '&lt;span class=&quot;x&quot;&gt;'], []);
check('js fence keeps unknown tag as code',
  '示例：\n\n```js\nconst el = <MyComp />;\n```',
  ['&lt;MyComp /&gt;'], []);
check('unlabeled fence cleaned',
  '示例：\n\n```\n<tool calls here>\n```',
  ['示例'], ['tool calls']);
check('bare tool calls after math hidden',
  '\\(\\frac{G}{K}\\) 完成\n{"calls":[{"tool":"calendar.create"}]}',
  ['math-pending'], ['calls', 'calendar.create']);
check('json code block kept',
  '示例：\n\n```json\n{"calls":[{"tool":"calendar.create"}]}\n```',
  ['<pre>', 'calendar.create'], []);
check('plain json kept', '配置：{"a":1}', ['配置：', '{'], []);

// ---- 流式 / 不完整 Markdown 与安全兜底 ----
check('unclosed code fence', '```python\nprint(1)', ['<pre>', 'print(1)'], []);
check('unclosed bold stays literal', '**bold', ['**bold'], []);
check('unclosed math stays literal', '$x+1', ['$x+1'], []);
check('crlf normalized', 'a\r\nb', ['a<br>b'], []);
check('raw html escaped', '<script>alert(1)</script>', ['&lt;script&gt;'], ['<script>']);
check('wikilink attribute escaped', '[["><img src=x onerror=1>]]',
  ['data-note=', '&lt;img'], ['<img src=x']);
check('javascript link downgraded', '[x](javascript:alert(1))',
  ['href="#"'], ['javascript:']);
check('link query ampersand not double escaped', '[x](https://a.com/?a=1&b=2)',
  ['href="https://a.com/?a=1&amp;b=2"'], ['&amp;amp;']);

let safeFailed = 0;
const safeCases = [
  null, undefined, '', 'x', '```', '$', '[[', '<script>', 'a'.repeat(100000),
];
safeCases.forEach((v, i) => {
  try {
    const out = ctx.safeRenderMarkdown(v);
    if (v && !String(out || '').trim()) safeFailed++;
  } catch (e) {
    safeFailed++;
    console.log('[FAIL] safeRenderMarkdown case', i, e.message);
  }
});
if (safeFailed) {
  failed += safeFailed;
  console.log('[FAIL] safeRenderMarkdown 兜底', safeFailed);
} else {
  console.log('[OK]   safeRenderMarkdown never throws');
}

// AI 可视内容：只有闭合围栏才渲染成富媒体块
check('svg rich block',
  '```svg\n<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><circle cx="5" cy="5" r="4"/></svg>\n```',
  ['data-rich="svg"', 'rich-media-src'], ['<svg xmlns=']);
check('unclosed svg stays code',
  '```svg\n<svg></svg>',
  ['code-block'], ['data-rich']);
check('chart rich block', '```chart\n{"xAxis":{},"series":[]}\n```',
  ['data-rich="chart"'], []);
check('echarts alias', '```echarts\n{"series":[]}\n```', ['data-rich="chart"'], []);
check('mermaid rich block', '```mermaid\nflowchart TD\nA-->B\n```',
  ['data-rich="mermaid"'], []);
check('remote image', '![图](https://example.com/a.png)',
  ['<img class="md-img"', 'src="https://example.com/a.png"'], []);
check('sandbox image placeholder', '![图](figs/a.png)',
  ['md-img-sandbox', 'data-src="figs/a.png"'], ['<img']);
check('javascript image rejected', '![x](javascript:alert(1))', [], ['<img']);
check('data uri image rejected', '![x](data:image/png;base64,AAA)', [], ['<img']);

// 渲染器抛异常时必须降级为转义纯文本
const originalRender = ctx.renderMarkdown;
try {
  ctx.renderMarkdown = () => { throw new Error('boom'); };
  const fallback = ctx.safeRenderMarkdown('<b>x</b>');
  if (!fallback.includes('&lt;b&gt;') || fallback.includes('<b>x</b>')) {
    failed++;
    console.log('[FAIL] safeRenderMarkdown exception fallback', fallback);
  } else {
    console.log('[OK]   safeRenderMarkdown exception fallback');
  }
} finally {
  ctx.renderMarkdown = originalRender;
}

console.log(failed ? ('FAILED ' + failed) : 'ALL OK');
process.exit(failed ? 1 : 0);
