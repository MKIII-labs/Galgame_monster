#!/usr/bin/env node
'use strict';

/* ============================================================
   译页版式单元测试（零依赖：node scripts/transpage_test.js）
   ------------------------------------------------------------
   覆盖：
     · 译文清洗：非法占位符丢弃、合法占位符保留、标题编号补回
     · 折行：中文按字 / 英文按词，行宽不超框
     · 自适应：目标行数约束、行距撑满原框、放不下时降字号
     · 遮盖差集：盖原文但不盖公式 / 表格
   ============================================================ */

const path = require('path');
const T = require(path.join(__dirname, '..', 'web', 'transpage.js'));

let pass = 0;
const fails = [];

function ok() { pass += 1; }
function fail(name, got, want) {
  fails.push(name + '\n      实际: ' + JSON.stringify(got) + '\n      期望: ' + JSON.stringify(want));
}
function eq(name, got, want) {
  if (JSON.stringify(got) === JSON.stringify(want)) ok();
  else fail(name, got, want);
}
function truthy(name, cond, detail) {
  if (cond) ok();
  else fail(name, detail === undefined ? cond : detail, true);
}

/* 量宽替身：中文 1em，西文 0.55em（与浏览器 canvas 的近似比例） */
function measure(text, px) {
  let w = 0;
  for (const ch of text) {
    const c = ch.charCodeAt(0);
    w += (c >= 0x2e80 && c <= 0x9fff) ? px : px * 0.55;
  }
  return w;
}

/* ---------------- 1. 译文清洗 ---------------- */

eq('标题：模型造的占位符被丢掉，编号从原文补回',
  T.normalize('⟦F1⟧ 引言', { kind: 'heading', text: '1. Introduction', math: {} }),
  '1. 引言');

eq('标题：已有编号不重复添加',
  T.normalize('1.2. 贡献', { kind: 'heading', text: '1.2. Contribution', math: {} }),
  '1.2. 贡献');

eq('正文：合法的公式占位符必须保留（交给 KaTeX 渲染）',
  T.normalize('见 ⟦F1⟧ 与 ⟦F2⟧', { kind: 'text', text: 'see [1] and [2]', math: { F1: '[1]', F2: '[2]' } }),
  '见 ⟦F1⟧ 与 ⟦F2⟧');

eq('正文：不存在的占位符清掉，不留空洞',
  T.normalize('见⟦F9⟧结果', { kind: 'text', text: 'see result', math: { F1: 'x' } }),
  '见结果');

eq('只剩非法占位符 → 视为没有译文',
  T.normalize('⟦F1⟧', { kind: 'text', text: 'x', math: {} }),
  '');

/* ---------------- 2. 折行 ---------------- */

const wrapped = T.wrap('这是一段中文和 English words 混排的测试文本', 60, 10, measure);
truthy('折行：至少两行', wrapped.length >= 2, wrapped);
truthy('折行：每行都不超框',
  wrapped.every((ln) => measure(ln, 10) <= 60 + 0.01),
  wrapped.map((ln) => measure(ln, 10)));

const words = T.wrap('alpha beta gamma delta epsilon', 40, 10, measure);
truthy('折行：英文不把单词拆开',
  words.every((ln) => ln.trim() === '' || !/^\s/.test(ln) || true) &&
  words.join(' ').replace(/\s+/g, ' ').trim() === 'alpha beta gamma delta epsilon',
  words);

/* ---------------- 3. 自适应字号 ---------------- */

const lay = T.fit('中'.repeat(40), 200, 60, 6, 10, 5, 11.6, measure);
truthy('自适应：行数不超过原段行数', lay.lines <= 6, lay);
truthy('自适应：行高 × 行数不超出框高', lay.lines * lay.lineHeight <= 61, lay);
truthy('自适应：字号贴近原字号（为保持原段行数最多让 5%）', lay.fontSize >= 9.4, lay);

const short = T.fit('短短一行', 200, 60, 6, 10, 5, 11.6, measure);
truthy('短译文：仍按原段行距撑满（行高 = 框高 / 原段行数）',
  Math.abs(short.lineHeight - 10) < 0.6, short);

const crowded = T.fit('中'.repeat(400), 100, 40, 2, 10, 5, 11.6, measure);
truthy('长译文：放不下时给出 overflow 标记', crowded.overflow === true, crowded);
truthy('长译文：字号不低于下限', crowded.fontSize >= 5 - 0.01, crowded);

/* ---------------- 4. 遮盖差集 ---------------- */

const rect = [0, 0, 100, 50];
const pieces = T.coverPieces(rect, [[0, 20, 40, 30]]);
const area = pieces.reduce((s, p) => s + (p[2] - p[0]) * (p[3] - p[1]), 0);
eq('差集：面积 = 原矩形 - 障碍物交集', Math.round(area), 100 * 50 - 40 * 10);
truthy('差集：每一块都不再与障碍物相交',
  pieces.every((p) => Math.min(p[2], 40) - Math.max(p[0], 0) <= 0.01 ||
                      Math.min(p[3], 30) - Math.max(p[1], 20) <= 0.01),
  pieces);

eq('差集：没有障碍物时原样返回', T.coverPieces(rect, []), [rect]);
eq('差集：障碍物在外侧时原样返回', T.coverPieces(rect, [[200, 200, 300, 300]]), [rect]);

/* ---------------- 汇总 ---------------- */

if (fails.length) {
  console.log('译页版式单元测试：' + pass + ' 通过，' + fails.length + ' 失败');
  fails.forEach((f) => console.log('  ✗ ' + f));
  process.exit(1);
}
console.log('译页版式单元测试：' + pass + '/' + pass + ' 全部通过');
