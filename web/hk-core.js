'use strict';

/* ============================================================
   快捷键「纯逻辑层」
   ------------------------------------------------------------
   这一层**不碰 DOM、不碰 localStorage**，只做判定与换算，因此可以被
   Node 直接 require 做单元测试（见 scripts/hotkeys_test.js）。
   浏览器侧由 web/hk-core.js 作为普通脚本先于 hotkeys.js 加载，
   顶层 const / function 落在同一个全局作用域里，hotkeys.js 直接引用即可。

   为什么要拆出来：改键的判定规则（冲突检测、用户自定义优先、e.code → Monaco
   键位换算）是最容易出错、也最值得被测试覆盖的部分，混在带 DOM 的大文件里没法测。
   ============================================================ */

// e.code → 展示名（未列出的在 hkLabel 里按 KeyA / Digit1 的前缀规则推导）
const HK_KEY_LABEL = {
  Space: '空格', Enter: '回车', NumpadEnter: '小键盘回车', Escape: 'Esc', Tab: 'Tab',
  ArrowUp: '↑', ArrowDown: '↓', ArrowLeft: '←', ArrowRight: '→',
  Backspace: '退格', Delete: 'Del', Insert: 'Ins', Home: 'Home', End: 'End',
  PageUp: 'PgUp', PageDown: 'PgDn',
  Slash: '/', Comma: ',', Period: '.', Semicolon: ';', Quote: '\'', Backquote: '`',
  Minus: '-', Equal: '=', BracketLeft: '[', BracketRight: ']', Backslash: '\\',
  ControlLeft: 'Ctrl', ControlRight: 'Ctrl', ShiftLeft: 'Shift', ShiftRight: 'Shift',
  AltLeft: 'Alt', AltRight: 'Alt', MetaLeft: 'Win', MetaRight: 'Win',
};

// 这些键单独按下只算"修饰中"，不构成一次完整按键
const HK_MOD_KEYS = ['ControlLeft', 'ControlRight', 'ShiftLeft', 'ShiftRight',
                     'AltLeft', 'AltRight', 'MetaLeft', 'MetaRight'];

/* e.code 组合串：修饰键固定顺序，便于字符串直接比较。
   只按住修饰键（没有主键）时返回空串 —— 那不是一次完整按键。 */
function hkKeyOf(e) {
  if (!e || !e.code) return '';
  const parts = [];
  if (e.ctrlKey) parts.push('Ctrl');
  if (e.metaKey) parts.push('Meta');
  if (e.altKey) parts.push('Alt');
  if (e.shiftKey) parts.push('Shift');
  parts.push(e.code);
  return parts.join('+');
}

// 单个按键串 → 展示名（'Ctrl+KeyS' → 'Ctrl+S'）
function hkLabel(spec) {
  return String(spec || '').split('+').map((p) => {
    if (HK_KEY_LABEL[p]) return HK_KEY_LABEL[p];
    if (/^Key[A-Z]$/.test(p)) return p.slice(3);
    if (/^Digit\d$/.test(p)) return p.slice(5);
    if (/^Numpad\d$/.test(p)) return '小键盘' + p.slice(6);
    return p || '?';
  }).join('+');
}

/* 录制时校验：这个键能不能用在当前作用域。
   关键约束 —— 除 GAL（它有「焦点不在输入框」的保护）以外，**不接受裸的可打印字符**，
   否则用户把「发送」改成 E，以后每次打 e 都会发消息。只允许组合键或功能键。 */
function hkSpecProblem(scope, spec) {
  if (scope === 'gal') return '';
  if (spec === 'Shift+Enter') return 'Shift+Enter 是「换行」，请换一个';
  const parts = String(spec).split('+');
  const main = parts[parts.length - 1];
  const hasCmdMod = parts.indexOf('Ctrl') >= 0 || parts.indexOf('Meta') >= 0 || parts.indexOf('Alt') >= 0;
  if (hasCmdMod) return '';
  const FN = /^(Enter|NumpadEnter|Tab|Escape|Backspace|Delete|Insert|ArrowUp|ArrowDown|ArrowLeft|ArrowRight|Home|End|PageUp|PageDown|F\d{1,2})$/;
  if (FN.test(main)) return '';
  return '这个键会在打字时误触发，请改用组合键（Ctrl/⌘ + …）或功能键（Enter / F1… 等）';
}

// 某条规则当前生效的按键：有覆盖只保留新键，否则用默认列表
// 覆盖为空串 = 用户主动「解除绑定」，返回空数组（不再参与命中）
function hkKeysOfDef(def, overrides) {
  if (!def) return [];
  const ov = overrides || {};
  if (Object.prototype.hasOwnProperty.call(ov, def.id)) {
    return ov[def.id] ? [ov[def.id]] : [];
  }
  return (def.keys || []).slice();
}

/* 找出本次按键命中的规则（scopes 为空 = 不限作用域）。

   优先级（对应需求「冲突时优先遵循用户自定义配置」）：
     1. 若这个键被**用户显式改给某条规则**，则只有那些规则参与竞争 ——
        改键是显式意图，应当压过所有默认键；
     2. 否则按注册表顺序，在指定作用域里取第一个命中的。
   注意第 1 条会让「别的作用域的默认规则」让路（返回 null 而不是吞掉按键），
   这样改键后不会出现「两个模块同时抢一个键」或「键被吞掉但什么也没发生」。 */
function hkHitIn(defs, spec, scopes, overrides) {
  if (!spec) return null;
  const ov = overrides || {};
  const live = (defs || []).filter((d) => !d.dyn && !d.hold && !d.displayOnly &&
    hkKeysOfDef(d, ov).indexOf(spec) >= 0);
  if (!live.length) return null;
  const anyCustom = live.some((d) => ov[d.id]);
  const pool = anyCustom ? live.filter((d) => ov[d.id]) : live;
  const inScope = pool.filter((d) => !scopes || scopes.indexOf(d.scope) >= 0);
  return inScope[0] || null;
}

// 同作用域重键（改键时要拒绝：同一作用域里两个动作抢同一个键必然自相矛盾）
function hkFindDup(defs, overrides, id, spec) {
  const d = (defs || []).find((x) => x.id === id);
  if (!d) return null;
  return (defs || []).find((x) => x.scope === d.scope && x.id !== id && !x.fixed &&
    hkKeysOfDef(x, overrides).indexOf(spec) >= 0) || null;
}

/* 跨作用域重键（允许，但要在提示里说明"你的自定义优先"）。
   不用拒绝：跨作用域本来就靠"谁先响应 + 焦点判断"分流，拒绝反而挡住合理用法。 */
function hkFindCross(defs, overrides, id, spec) {
  const d = (defs || []).find((x) => x.id === id);
  if (!d) return null;
  return (defs || []).filter((x) => x.scope !== d.scope && x.id !== id &&
    hkKeysOfDef(x, overrides).indexOf(spec) >= 0);
}

/* ============================================================
   编辑器快捷键 → Monaco 键位
   ------------------------------------------------------------
   下面两张表是**从项目内置的 Monaco 实物里抄出来的**（web/vendor/monaco/vs/editor/
   editor.main.js 的 KeyCode / KeyMod 枚举），不是凭记忆写的：
     · KeyCode：Tab=2, Enter=3, Escape=9, Space=10, PageUp=11, PageDown=12, End=13,
       Home=14, LeftArrow=15, UpArrow=16, RightArrow=17, DownArrow=18, Insert=19,
       Delete=20, Digit0=21…Digit9=30, KeyA=31…KeyZ=56, F1=59…F24=82,
       Semicolon=85, Equal=86, Comma=87, Minus=88, Period=89, Slash=90, Backquote=91,
       BracketLeft=92, Backslash=93, BracketRight=94, Quote=95
       —— 注意：**没有 NumpadEnter**（实测不存在），所以它无法映射到编辑器。
     · KeyMod：CtrlCmd=2048, Shift=1024, Alt=512, WinCtrl=256
   这些数值只用于单元测试断言"名字真的存在"；运行时仍走 monaco.KeyCode[name] /
   monaco.KeyMod[name] 取值，避免把数值写死。 */
const HK_MONACO_KEYCODE = {
  Unknown: 0, Backspace: 1, Tab: 2, Enter: 3, Shift: 4, Ctrl: 5, Alt: 6,
  PauseBreak: 7, CapsLock: 8, Escape: 9, Space: 10, PageUp: 11, PageDown: 12,
  End: 13, Home: 14, LeftArrow: 15, UpArrow: 16, RightArrow: 17, DownArrow: 18,
  Insert: 19, Delete: 20,
  Digit0: 21, Digit1: 22, Digit2: 23, Digit3: 24, Digit4: 25, Digit5: 26,
  Digit6: 27, Digit7: 28, Digit8: 29, Digit9: 30,
  KeyA: 31, KeyB: 32, KeyC: 33, KeyD: 34, KeyE: 35, KeyF: 36, KeyG: 37,
  KeyH: 38, KeyI: 39, KeyJ: 40, KeyK: 41, KeyL: 42, KeyM: 43, KeyN: 44,
  KeyO: 45, KeyP: 46, KeyQ: 47, KeyR: 48, KeyS: 49, KeyT: 50, KeyU: 51,
  KeyV: 52, KeyW: 53, KeyX: 54, KeyY: 55, KeyZ: 56,
  Meta: 57, ContextMenu: 58,
  F1: 59, F2: 60, F3: 61, F4: 62, F5: 63, F6: 64, F7: 65, F8: 66, F9: 67,
  F10: 68, F11: 69, F12: 70,
  NumLock: 83, ScrollLock: 84,
  Semicolon: 85, Equal: 86, Comma: 87, Minus: 88, Period: 89, Slash: 90,
  Backquote: 91, BracketLeft: 92, Backslash: 93, BracketRight: 94, Quote: 95,
};

const HK_MONACO_KEYMOD = { CtrlCmd: 2048, Shift: 1024, Alt: 512, WinCtrl: 256 };

// e.code → Monaco KeyCode 名字：只有方向键不同名，其余同名
const HK_MONACO_KEY_ALIAS = {
  ArrowUp: 'UpArrow', ArrowDown: 'DownArrow', ArrowLeft: 'LeftArrow', ArrowRight: 'RightArrow',
};

/* 我们的修饰键 → Monaco KeyMod 名字。
   Ctrl 与 Meta 都映射到 CtrlCmd（= 平台主修饰键：Windows 是 Ctrl、macOS 是 Cmd），
   与 VS Code 自己的 keybinding 字符串语义一致；这样 Ctrl+Z / ⌘Z 在各自平台上都对。 */
const HK_MONACO_MOD = { Ctrl: 'CtrlCmd', Meta: 'CtrlCmd', Alt: 'Alt', Shift: 'Shift' };

/* 'Alt+Shift+ArrowUp' → { mods: ['Alt','Shift'], key: 'UpArrow' }
   名字不在内置 Monaco 的 KeyCode 里就返回 null（**不猜**）——调用方会跳过这条，
   而不是硬塞一个不存在的键位把编辑器搞坏。 */
function hkMonacoParts(spec) {
  const parts = String(spec || '').split('+').filter(Boolean);
  if (!parts.length) return null;
  const code = parts.pop();
  const key = HK_MONACO_KEY_ALIAS[code] || code;
  if (!(key in HK_MONACO_KEYCODE)) return null;
  const mods = [];
  parts.forEach((p) => {
    const mp = HK_MONACO_MOD[p];
    if (mp && mods.indexOf(mp) < 0) mods.push(mp);
  });
  return { mods, key };
}

// Node 单元测试用（浏览器里 typeof module 是 undefined，这段不会执行）
if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    HK_KEY_LABEL, HK_MOD_KEYS, HK_MONACO_KEYCODE, HK_MONACO_KEYMOD,
    HK_MONACO_KEY_ALIAS, HK_MONACO_MOD,
    hkKeyOf, hkLabel, hkSpecProblem, hkKeysOfDef,
    hkHitIn, hkFindDup, hkFindCross, hkMonacoParts,
  };
}
