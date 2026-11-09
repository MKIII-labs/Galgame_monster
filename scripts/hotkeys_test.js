#!/usr/bin/env node
'use strict';

/* ============================================================
   快捷键单元测试（零依赖，直接跑 node scripts/hotkeys_test.js）
   ------------------------------------------------------------
   覆盖范围：
     · 按键串解析与展示名（hkKeyOf / hkLabel）
     · 录制校验（hkSpecProblem：裸键拒绝、组合键放行、GAL 例外）
     · 命中判定与**优先级**（hkHitIn：用户自定义优先、跨作用域让路）
     · 重键检测（hkFindDup 同作用域拒绝 / hkFindCross 跨作用域提示）
     · e.code → Monaco 键位换算（hkMonacoParts）+ 内置枚举快照一致性
     · 注册表自校验（HK_DEFS / HK_SCOPES：作用域合法、editor 规则自带 mono、
       默认键能解析、同作用域不重键、需求点名的三条快捷键确实存在）
   为什么用零依赖手写断言：本项目"无构建、无 node_modules"，引测试框架会破坏这一前提。
   ============================================================ */

const path = require('path');
const fs = require('fs');
const core = require(path.join(__dirname, '..', 'web', 'hk-core.js'));
const reg = require(path.join(__dirname, '..', 'web', 'hotkeys.js'));
const doc = require(path.join(__dirname, '..', 'web', 'hk-doc.js'));

const { HK_DEFS, HK_SCOPES } = reg;
const { HK_DOC_GROUPS } = doc;
const {
  hkKeyOf, hkLabel, hkSpecProblem, hkKeysOfDef,
  hkHitIn, hkFindDup, hkFindCross, hkMonacoParts,
  HK_MONACO_KEYCODE, HK_MONACO_KEYMOD,
} = core;

let pass = 0;
const fails = [];

function ok(name) { pass += 1; }
function fail(name, got, want) {
  fails.push(name + '\n      实际: ' + JSON.stringify(got) + '\n      期望: ' + JSON.stringify(want));
}
function eq(name, got, want) {
  if (JSON.stringify(got) === JSON.stringify(want)) ok(name);
  else fail(name, got, want);
}
function truthy(name, got) { got ? ok(name) : fail(name, got, '真值'); }
function falsy(name, got) { !got ? ok(name) : fail(name, got, '假值'); }

/* 造一个只带 hkDefs 需要的字段的键盘事件 */
function ev(code, mods) {
  const m = mods || {};
  return { code, ctrlKey: !!m.ctrl, metaKey: !!m.meta, altKey: !!m.alt, shiftKey: !!m.shift };
}

/* ---------- 1. 按键串解析 ---------- */
eq('hkKeyOf: Ctrl+Shift+Z', hkKeyOf(ev('KeyZ', { ctrl: true, shift: true })), 'Ctrl+Shift+KeyZ');
eq('hkKeyOf: 无修饰键', hkKeyOf(ev('Escape')), 'Escape');
eq('hkKeyOf: 修饰键顺序固定（Alt 在 Shift 前）',
  hkKeyOf(ev('ArrowUp', { alt: true, shift: true })), 'Alt+Shift+ArrowUp');
eq('hkKeyOf: 空 code → 空串', hkKeyOf({ ctrlKey: true }), '');

/* ---------- 2. 展示名 ---------- */
eq('hkLabel: Ctrl+KeyS → Ctrl+S', hkLabel('Ctrl+KeyS'), 'Ctrl+S');
eq('hkLabel: Ctrl+Slash → Ctrl+/', hkLabel('Ctrl+Slash'), 'Ctrl+/');
eq('hkLabel: Alt+ArrowUp → Alt+↑', hkLabel('Alt+ArrowUp'), 'Alt+↑');
eq('hkLabel: Shift+Digit1 → Shift+1', hkLabel('Shift+Digit1'), 'Shift+1');

/* ---------- 3. 录制校验（裸键闸门） ---------- */
truthy('hkSpecProblem: 裸字母键被拒（打字会误触发）', hkSpecProblem('editor', 'KeyE'));
truthy('hkSpecProblem: 裸数字键被拒', hkSpecProblem('editor', 'Digit1'));
eq('hkSpecProblem: 裸方向键放行（GAL 默认键就是 ArrowUp，属既有约定）',
  hkSpecProblem('editor', 'ArrowDown'), '');
eq('hkSpecProblem: 裸 Esc 放行', hkSpecProblem('editor', 'Escape'), '');
eq('hkSpecProblem: 组合键放行', hkSpecProblem('editor', 'Ctrl+KeyE'), '');
eq('hkSpecProblem: 功能键放行', hkSpecProblem('editor', 'F5'), '');
eq('hkSpecProblem: GAL 作用域例外（有焦点保护，允许裸键）', hkSpecProblem('gal', 'KeyE'), '');
truthy('hkSpecProblem: Shift+Enter 明确拒绝（那是换行）', hkSpecProblem('editor', 'Shift+Enter'));

/* ---------- 4. 优先级：用户自定义优先 ---------- */
const ov = { 'b.rule': 'Ctrl+KeyJ' };
const defs = [
  { id: 'a.rule', scope: 'x', name: 'A', keys: ['Ctrl+KeyJ'] },
  { id: 'b.rule', scope: 'y', name: 'B', keys: ['Ctrl+KeyK'] },
];
eq('hkKeysOfDef: 有覆盖只保留新键', hkKeysOfDef(defs[1], ov), ['Ctrl+KeyJ']);
eq('hkKeysOfDef: 无覆盖用默认列表', hkKeysOfDef(defs[0], ov), ['Ctrl+KeyJ']);
eq('hkKeysOfDef: 空串覆盖 = 解除绑定', hkKeysOfDef(defs[1], { 'b.rule': '' }), []);
eq('hkHitIn: 解除绑定后不再命中', hkHitIn(defs, 'Ctrl+KeyK', ['y'], { 'b.rule': '' }), null);
eq('hkHitIn: 自定义优先 —— 原作用域 x 让路（返回 null，不吞键）',
  hkHitIn(defs, 'Ctrl+KeyJ', ['x'], ov), null);
eq('hkHitIn: 自定义优先 —— 改键者 y 胜出',
  (hkHitIn(defs, 'Ctrl+KeyJ', ['y'], ov) || {}).id, 'b.rule');

const noOv = {};
eq('hkHitIn: 无自定义时按注册表顺序（不改变既有行为）',
  (hkHitIn(defs, 'Ctrl+KeyJ', ['x'], noOv) || {}).id, 'a.rule');
eq('hkHitIn: 作用域不匹配 → null', hkHitIn(defs, 'Ctrl+KeyK', ['x'], noOv), null);
eq('hkHitIn: 空串 → null', hkHitIn(defs, '', ['x'], noOv), null);
eq('hkHitIn: dyn/hold/displayOnly 不参与分发',
  hkHitIn([{ id: 'z', scope: 'x', keys: ['Escape'], displayOnly: true }], 'Escape', ['x'], noOv), null);

/* ---------- 5. 重键检测 ---------- */
const dupDefs = [
  { id: 's1', scope: 's', name: 'S1', keys: ['Ctrl+KeyJ'] },
  { id: 's2', scope: 's', name: 'S2', keys: ['Ctrl+KeyK'] },
];
eq('hkFindDup: 同作用域重键 → 命中',
  (hkFindDup(dupDefs, noOv, 's1', 'Ctrl+KeyK') || {}).id, 's2');
eq('hkFindDup: 同作用域不重键 → null', hkFindDup(dupDefs, noOv, 's1', 'Ctrl+KeyQ'), null);

const crossDefs = [
  { id: 'c1', scope: 'x', name: 'C1', keys: ['Ctrl+KeyJ'] },
  { id: 'c2', scope: 'y', name: 'C2', keys: ['Ctrl+KeyJ'] },
];
eq('hkFindDup: 同键不同作用域不算 dup（交给 hkFindCross）',
  hkFindDup(crossDefs, noOv, 'c1', 'Ctrl+KeyJ'), null);
eq('hkFindCross: 同键不同作用域 → 列出对方',
  hkFindCross(crossDefs, noOv, 'c1', 'Ctrl+KeyJ').map((d) => d.id), ['c2']);
eq('hkFindCross: 同作用域不返回', hkFindCross(dupDefs, noOv, 's1', 'Ctrl+KeyK'), []);

/* ---------- 6. e.code → Monaco 键位 ---------- */
eq('hkMonacoParts: Ctrl+Shift+KeyZ', hkMonacoParts('Ctrl+Shift+KeyZ'),
  { mods: ['CtrlCmd', 'Shift'], key: 'KeyZ' });
eq('hkMonacoParts: Alt+ArrowUp（方向键要换成 Monaco 的名字）', hkMonacoParts('Alt+ArrowUp'),
  { mods: ['Alt'], key: 'UpArrow' });
eq('hkMonacoParts: Meta 与 Ctrl 都归到 CtrlCmd（平台主修饰键）',
  hkMonacoParts('Meta+KeyL'), { mods: ['CtrlCmd'], key: 'KeyL' });
eq('hkMonacoParts: 无修饰键', hkMonacoParts('Enter'), { mods: [], key: 'Enter' });
eq('hkMonacoParts: 符号键 Ctrl+Slash', hkMonacoParts('Ctrl+Slash'),
  { mods: ['CtrlCmd'], key: 'Slash' });
eq('hkMonacoParts: 方括号 Ctrl+BracketLeft', hkMonacoParts('Ctrl+BracketLeft'),
  { mods: ['CtrlCmd'], key: 'BracketLeft' });
eq('hkMonacoParts: 重复修饰去重', hkMonacoParts('Ctrl+Meta+KeyA'),
  { mods: ['CtrlCmd'], key: 'KeyA' });
/* 这条是"不猜"的证据：Monaco 的 KeyCode 枚举里**没有 NumpadEnter**（已实测），
   所以解析失败返回 null，调用方会跳过，而不是硬塞一个不存在的键位。 */
eq('hkMonacoParts: NumpadEnter 在 Monaco 里不存在 → null（不猜）',
  hkMonacoParts('NumpadEnter'), null);
eq('hkMonacoParts: 空串 → null', hkMonacoParts(''), null);

/* ---------- 7. 内置快照一致性 ----------
   HK_MONACO_KEYCODE 是从 web/vendor/monaco 的枚举里抄出来的，
   这里抽查若干"关键成员"，防止有人手滑改错名字/数值。 */
eq('快照: KeyL = 42', HK_MONACO_KEYCODE.KeyL, 42);
eq('快照: KeyZ = 56', HK_MONACO_KEYCODE.KeyZ, 56);
eq('快照: UpArrow = 16', HK_MONACO_KEYCODE.UpArrow, 16);
eq('快照: DownArrow = 18', HK_MONACO_KEYCODE.DownArrow, 18);
eq('快照: Slash = 90', HK_MONACO_KEYCODE.Slash, 90);
eq('快照: BracketLeft = 92', HK_MONACO_KEYCODE.BracketLeft, 92);
eq('快照: Enter = 3 / Escape = 9 / Tab = 2',
  [HK_MONACO_KEYCODE.Enter, HK_MONACO_KEYCODE.Escape, HK_MONACO_KEYCODE.Tab], [3, 9, 2]);
eq('快照: 不存在 NumpadEnter', 'NumpadEnter' in HK_MONACO_KEYCODE, false);
eq('快照: KeyMod 数值', HK_MONACO_KEYMOD, { CtrlCmd: 2048, Shift: 1024, Alt: 512, WinCtrl: 256 });

/* ---------- 8. 注册表自校验 ---------- */
const scopeIds = HK_SCOPES.map((s) => s.id);
eq('注册表: 作用域 id 唯一', new Set(scopeIds).size, scopeIds.length);
eq('注册表: 每条规则的 scope 都在 HK_SCOPES 里',
  HK_DEFS.filter((d) => scopeIds.indexOf(d.scope) < 0).map((d) => d.id), []);
eq('注册表: 规则 id 唯一',
  new Set(HK_DEFS.map((d) => d.id)).size, HK_DEFS.length);

const eds = HK_DEFS.filter((d) => d.scope === 'editor');
truthy('注册表: 编辑器作用域有规则（' + eds.length + ' 条）', eds.length >= 13);
eq('注册表: 每条 editor 规则都带 mono（改键时要用它绑回原动作）',
  eds.filter((d) => !d.mono).map((d) => d.id), []);
eq('注册表: 每条 editor 的默认键都能解析成 Monaco 键位（不会静默失效）',
  eds.filter((d) => (d.keys || []).some((k) => hkMonacoParts(k) == null)).map((d) => d.id), []);
eq('注册表: editor 作用域内部不重键',
  (() => {
    const seen = {};
    const bad = [];
    eds.forEach((d) => (d.keys || []).forEach((k) => {
      if (seen[k]) bad.push(k + ':' + seen[k] + '/' + d.id);
      else seen[k] = d.id;
    }));
    return bad;
  })(), []);

/* 需求点名的三条核心快捷键必须在册 */
const byId = (id) => HK_DEFS.find((d) => d.id === id) || {};
truthy('需求 1: Ctrl+L 选中整行在册',
  (byId('ed.selectLine').keys || []).indexOf('Ctrl+KeyL') >= 0);
truthy('需求 1: Ctrl+Z 撤销在册',
  (byId('ed.undo').keys || []).indexOf('Ctrl+KeyZ') >= 0);
truthy('需求 1: Ctrl+Shift+Z 重做在册',
  (byId('ed.redo').keys || []).indexOf('Ctrl+Shift+KeyZ') >= 0);
truthy('需求 1: Ctrl+/ 行注释在册',
  (byId('ed.comment').keys || []).indexOf('Ctrl+Slash') >= 0);
eq('需求 1: mono 指向真实的 Monaco action id',
  [byId('ed.selectLine').mono, byId('ed.undo').mono, byId('ed.redo').mono],
  ['expandLineSelection', 'undo', 'redo']);

/* 同一作用域内不允许两个非 fixed 规则共用同一个默认键（真实注册表） */
eq('注册表: 各作用域内部默认键不冲突',
  (() => {
    const bad = [];
    scopeIds.forEach((sc) => {
      const seen = {};
      HK_DEFS.filter((d) => d.scope === sc).forEach((d) => (d.keys || []).forEach((k) => {
        if (seen[k]) bad.push(sc + ' ' + k + ':' + seen[k] + '/' + d.id);
        else seen[k] = d.id;
      }));
    });
    return bad;
  })(), []);

/* 注册表包装函数能跑通（它们只是把 localStorage 的覆盖项喂给纯逻辑） */
eq('hkKeysOf: 无覆盖时返回默认键', reg.hkKeysOf('ed.undo'), ['Ctrl+KeyZ', 'Meta+KeyZ']);
eq('hkHit: 在 editor 作用域命中 Ctrl+Shift+Z',
  (reg.hkHit(ev('KeyZ', { ctrl: true, shift: true }), ['editor']) || {}).id, 'ed.redo');

/* ---------- 8.5 「完整说明」与注册表同源 ----------
   说明页的键位由 HK_DEFS 现取，但**条目**仍需人工维护。这里挡住两类回归：
   新增了可改键规则却忘了写进说明；说明里引用了不存在的规则 id。 */
const DOC_ROWS = [];
HK_DOC_GROUPS.forEach((g) => (g.rows || []).forEach((r) => DOC_ROWS.push({ g, r })));
const docIds = HK_DOC_GROUPS.map((g) => g.id);

eq('说明: 分组 id 唯一', new Set(docIds).size, docIds.length);
eq('说明: 分组标题不重复',
  new Set(HK_DOC_GROUPS.map((g) => g.title)).size, HK_DOC_GROUPS.length);
eq('说明: 每个分组都有标题与条目',
  HK_DOC_GROUPS.filter((g) => !g.title || !(g.rows || []).length).map((g) => g.id), []);
eq('说明: 没有引用不存在的规则 id',
  DOC_ROWS.filter((x) => x.r.def && !reg.hkDef(x.r.def)).map((x) => x.r.def), []);
eq('说明: 每条可改键规则都写进了说明',
  HK_DEFS.filter((d) => !DOC_ROWS.some((x) => x.r.def === d.id)).map((d) => d.id), []);
eq('说明: 同一条规则不重复文档化',
  (() => {
    const seen = {};
    const bad = [];
    DOC_ROWS.forEach((x) => {
      if (!x.r.def) return;
      if (seen[x.r.def]) bad.push(x.r.def);
      else seen[x.r.def] = true;
    });
    return bad;
  })(), []);
eq('说明: 自写条目都有 keys / name / cond',
  DOC_ROWS.filter((x) => !x.r.def &&
    (!(x.r.keys || []).length || !x.r.name || !x.r.cond)).map((x) => x.r.name || '(无名)'), []);
eq('说明: def 条目都写了生效条件',
  DOC_ROWS.filter((x) => x.r.def && !x.r.cond).map((x) => x.r.def), []);

/* ---------- 9. 单一来源校验 ----------
   纯逻辑只允许声明在 hk-core.js。hotkeys.js 若再声明一份同名函数/常量，浏览器里
   后加载的那个会静默覆盖前一个 —— 两份实现一旦漂移，测试还测的是 hk-core 的版本，
   问题会被完美藏住。这条断言就是为了挡住这类"看起来没事"的重复。 */
const HKTEXT = fs.readFileSync(path.join(__dirname, '..', 'web', 'hotkeys.js'), 'utf8');
['hkKeyOf', 'hkLabel', 'hkSpecProblem', 'hkKeysOfDef', 'hkHitIn',
  'hkFindDup', 'hkFindCross', 'hkMonacoParts',
  'HK_KEY_LABEL', 'HK_MOD_KEYS', 'HK_MONACO_KEYCODE'].forEach((nm) => {
  const re = new RegExp('(?:function\\s+' + nm + '\\s*\\(|(?:const|let|var)\\s+' + nm + '\\s*=)');
  eq('单一来源: hotkeys.js 不再重复声明 ' + nm, re.test(HKTEXT), false);
});

/* ---------- 结果 ---------- */
const total = pass + fails.length;
if (fails.length) {
  console.error('快捷键单元测试：' + pass + '/' + total + ' 通过，' + fails.length + ' 失败\n');
  fails.forEach((f, i) => console.error('  ' + (i + 1) + '. ' + f + '\n'));
  process.exit(1);
}
console.log('快捷键单元测试：' + pass + '/' + total + ' 全部通过');
