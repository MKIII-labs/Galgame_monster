/**
 * UI 冒烟测试（只读，零 npm 依赖）。
 *
 * 用本机 Edge/Chrome 的 headless + CDP 跑一遍关键界面入口，断言 DOM 不变量、
 * 控制台无报错、CLS 在阈值内；不调用任何会写数据或调用模型的接口
 * （工作台只检查 AI 面板已渲染，不触发 /api/work/assist）。
 *
 * 前置：应用已在运行（端口见 data/app_port.txt）。
 *
 * 用法：
 *   node scripts/ui_smoke.mjs --url http://127.0.0.1:52665/
 *   node scripts/ui_smoke.mjs --url ... --mobile --out data/ui-smoke.json
 *
 * 退出码：0 全过；1 有失败项或控制台报错。
 */
import { spawn } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync, readdirSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

const CANDIDATES = [
  process.env.CHROME_PATH,
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
].filter(Boolean);

function arg(name, fallback) {
  const i = process.argv.indexOf(`--${name}`);
  return i > 0 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}

const BASE = arg('url', 'http://127.0.0.1:8791/').replace(/\/$/, '') + '/';
const MOBILE = process.argv.includes('--mobile');
const OUT = arg('out', '');
const WIDTH = MOBILE ? 390 : 1440;
const HEIGHT = MOBILE ? 844 : 900;
const PORT = 9373;
const CLS_LIMIT = 0.1;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function findBrowser() {
  for (const p of CANDIDATES) if (existsSync(p)) return p;
  throw new Error('未找到 Edge/Chrome，可用 CHROME_PATH 指定');
}

/* 找几个不在 data/ 下的小 PDF 供阅读器冒烟（沙箱禁止读 data/） */
function findSamplePdfs(limit = 4) {
  const override = process.env.GM_SMOKE_PDF || arg('reader', '');
  if (override) return [path.resolve(override)];
  const roots = ['.tmp_audit', 'docs', 'eval'];
  const out = [];
  const walk = (dir, depth) => {
    if (out.length >= limit || depth > 3) return;
    let names = [];
    try { names = readdirSync(dir); } catch (e) { return; }
    for (const n of names) {
      if (out.length >= limit) return;
      const p = path.join(dir, n);
      let st = null;
      try { st = statSync(p); } catch (e) { continue; }
      if (st.isDirectory()) walk(p, depth + 1);
      else if (n.toLowerCase().endsWith('.pdf') && st.size > 1024 && st.size < 8 * 1024 * 1024) {
        out.push(path.resolve(p));
      }
    }
  };
  for (const r of roots) walk(path.resolve(r), 0);
  return out;
}

class CDP {
  constructor(ws) {
    this.ws = ws;
    this.id = 0;
    this.pending = new Map();
    this.listeners = new Set();
    ws.addEventListener('message', (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        if (msg.error) reject(new Error(msg.error.message));
        else resolve(msg.result);
      } else if (msg.method) {
        for (const fn of this.listeners) fn(msg);
      }
    });
  }
  send(method, params = {}) {
    const id = ++this.id;
    this.ws.send(JSON.stringify({ id, method, params }));
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      setTimeout(() => {
        if (this.pending.has(id)) {
          this.pending.delete(id);
          reject(new Error(`CDP 超时: ${method}`));
        }
      }, 30000);
    });
  }
  on(fn) { this.listeners.add(fn); return () => this.listeners.delete(fn); }
}

const browser = findBrowser();
const profile = mkdtempSync(path.join(tmpdir(), 'gm-ui-smoke-'));
const child = spawn(browser, [
  '--headless=new', '--disable-gpu', '--no-first-run', '--hide-scrollbars',
  `--window-size=${WIDTH},${HEIGHT}`,
  `--user-data-dir=${profile}`,
  `--remote-debugging-port=${PORT}`,
  'about:blank',
], { stdio: 'ignore' });

let ready = null;
for (let i = 0; i < 60; i++) {
  try {
    ready = await (await fetch(`http://127.0.0.1:${PORT}/json/version`)).json();
    break;
  } catch (e) { await sleep(250); }
}
if (!ready) { child.kill(); throw new Error('CDP 未就绪'); }

const target = await (await fetch(
  `http://127.0.0.1:${PORT}/json/new?${encodeURIComponent(BASE)}`,
  { method: 'PUT' },
)).json();
const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((res) => ws.addEventListener('open', res));
const cdp = new CDP(ws);

let errors = [];
// 阅读器的公式识别 / 裁图 / 翻译是「可选增强」：视觉模型未配置或单页无可识别内容时
// 后端返回 400，前端只静默降级。这类请求记为警告，不算冒烟失败（避免误报）。
const soft = [];
const SOFT_API = /\/api\/reader\/(formula|crop|trans|selection)/;
cdp.on((msg) => {
  if (msg.method === 'Runtime.exceptionThrown') {
    const d = msg.params.exceptionDetails || {};
    const desc = d.exception && (d.exception.description || d.exception.value);
    errors.push(desc || d.text || 'exception');
  } else if (msg.method === 'Runtime.consoleAPICalled' && msg.params.type === 'error') {
    errors.push('console.error: ' + (msg.params.args || []).map((a) => a.value || '').join(' '));
  } else if (msg.method === 'Log.entryAdded') {
    const e = msg.params.entry || {};
    if (e.level !== 'error' || /favicon/i.test(e.url || e.text || '')) return;
    const line = `${e.source || 'log'}: ${e.text || ''}${e.url ? ' <' + e.url + '>' : ''}`;
    if (SOFT_API.test(e.url || '') && /40[034]/.test(e.text || '')) soft.push(line);
    else errors.push(line);
  }
});

await cdp.send('Runtime.enable');
await cdp.send('Page.enable');
await cdp.send('Log.enable');
await cdp.send('Emulation.setDeviceMetricsOverride', {
  width: WIDTH, height: HEIGHT, deviceScaleFactor: 1, mobile: MOBILE,
});
// 每次导航都重新初始化指标收集器
await cdp.send('Page.addScriptToEvaluateOnNewDocument', {
  source: `
    window.__smoke = { lcp: 0, cls: 0 };
    try {
      new PerformanceObserver((list) => {
        for (const e of list.getEntries()) window.__smoke.lcp = Math.max(window.__smoke.lcp, e.startTime);
      }).observe({ type: 'largest-contentful-paint', buffered: true });
      new PerformanceObserver((list) => {
        for (const e of list.getEntries()) if (!e.hadRecentInput) window.__smoke.cls += e.value;
      }).observe({ type: 'layout-shift', buffered: true });
    } catch (e) {}
  `,
});

async function evaluate(expr) {
  const r = await cdp.send('Runtime.evaluate', {
    expression: expr, returnByValue: true, awaitPromise: true,
  });
  if (r.exceptionDetails) {
    const d = r.exceptionDetails;
    throw new Error((d.exception && d.exception.description) || d.text || 'evaluate 失败');
  }
  return r.result.value;
}

async function goto(mode, waitMs) {
  errors = [];
  // 首屏模式由 state.config.ui_mode（服务端）决定，用 ?mode= 覆盖；
  // 同时写 localStorage，让首帧的侧栏预判逻辑也走同一模式（避免首帧闪动）。
  await evaluate(`try { localStorage.setItem('gm.uiMode', ${JSON.stringify(mode)}); } catch (e) {}`);
  await cdp.send('Page.navigate', { url: BASE + '?mode=' + encodeURIComponent(mode) });
  for (let i = 0; i < 120; i++) {
    try { if (await evaluate('document.readyState') === 'complete') break; } catch (e) { /* 加载中 */ }
    await sleep(100);
  }
  await sleep(waitMs);
}

const checks = [];
function check(name, ok, detail) {
  checks.push({ name, ok: !!ok, detail: detail || '' });
  const mark = ok ? 'PASS' : 'FAIL';
  console.log(`${mark}  ${name}${detail ? '  — ' + detail : ''}`);
}

async function visible(sel) {
  return evaluate(`(() => {
    const el = document.querySelector(${JSON.stringify(sel)});
    if (!el) return false;
    if (el.classList.contains('hidden')) return false;
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    return r.width > 2 && r.height > 2 && st.visibility !== 'hidden' && st.display !== 'none';
  })()`);
}

async function metrics() {
  return evaluate(`(() => {
    const p = window.__smoke || { lcp: 0, cls: 0 };
    return { cls: Number((p.cls || 0).toFixed(4)),
             lcp: Math.round(p.lcp || 0),
             nodes: document.querySelectorAll('*').length,
             overflow_x: document.documentElement.scrollWidth - window.innerWidth };
  })()`);
}

/* ---------------- 1. Solo 首屏 ---------------- */
await goto('solo', 3500);
const solo = await metrics();
check('solo 输入框可见', await visible('#input'));
check('solo CLS < ' + CLS_LIMIT, solo.cls < CLS_LIMIT, `cls=${solo.cls} lcp=${solo.lcp}ms`);
check('solo 无横向溢出', solo.overflow_x <= 1, `overflow=${solo.overflow_x}px`);
check('solo 无控制台报错', errors.length === 0, errors.slice(0, 3).join(' | '));

// 计划：消息卡进度 + 右侧检查器计划面板
await evaluate(`document.querySelector('#btn-inspector').click()`);
await sleep(300);
const planUi = await evaluate(`(async () => {
  const tab = document.querySelector('.insp-tab[data-itab="plan"]');
  if (!tab) return { ok: false, why: 'no plan tab' };
  tab.click();
  const sample = {
    goal: '把自动跟随重构稳', revision: 3,
    milestones: [{ id: 'M1', title: '核心修复' }],
    steps: [
      { id: 'S1', text: '定位 scroll 误判', status: 'done', milestone: 'M1' },
      { id: 'S2', text: '重构跟随状态机', status: 'doing', milestone: 'M1', verify: '极端场景测试通过' },
      { id: 'S3', text: '接入检查器', status: 'pending', milestone: 'M1' },
      { id: 'S4', text: '补文档', status: 'blocked', milestone: 'M1' },
    ],
  };
  window.planPanelSet(sample);
  await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
  const box = document.querySelector('#insp-plan');
  const bar = box.querySelector('.insp-plan-bar i');
  const rows = box.querySelectorAll('.insp-plan-step');
  const cur = box.querySelector('.insp-plan-current-text');
  // 待审批横幅
  state.sessionPlanReview = 'pr_test';
  window.uiInspectorSyncPlan();
  const review = !!box.querySelector('.insp-plan-review');
  state.sessionPlanReview = '';
  window.uiInspectorSyncPlan();
  // 消息卡：进度条 / 副标题 / 收尾信息折叠
  const wrap = document.createElement('div');
  wrap.appendChild(buildPlanCard(sample, false));
  const card = wrap.querySelector('.plan-card');
  const out = {
    pct: bar && bar.style.width,
    metrics: box.querySelector('.insp-plan-metrics').textContent.replace(/\\s+/g, ' ').trim(),
    current: cur ? cur.textContent : '',
    steps: rows.length,
    locate: !!box.querySelector('#insp-plan-locate'),
    review,
    cardTitle: card.querySelector('.plan-title').textContent,
    cardSub: card.querySelector('.plan-sub').textContent,
    cardPct: card.querySelector('.plan-progress i').style.width,
    cardExtra: !!card.querySelector('.plan-extra'),
  };
  card.remove();
  box.innerHTML = '';
  out.ok = out.pct === '25%' && out.current.indexOf('重构跟随状态机') >= 0
    && out.steps >= 4 && out.locate && out.review
    && out.cardTitle.indexOf('1/4') >= 0 && out.cardSub.indexOf('正在') >= 0
    && out.cardPct === '25%' && !out.cardExtra;
  return out;
})()`);
check('计划卡与检查器计划面板', planUi.ok, JSON.stringify(planUi));
await evaluate(`document.querySelector('#insp-close').click()`);
await sleep(200);

// Skill 卡片：落库记录能还原成常驻卡，且不与通用工具卡重复
const skillCard = await evaluate(`(() => {
  const wrap = document.createElement('div');
  appendSkillCards(wrap, [
    { tool: 'skill.read', args: { name: 'code-review' },
      result: '【Skill：代码审查】\\n\\n## 目标\\n逐条检查', ok: true, ms: 12 },
    { tool: 'read_file', args: { path: 'a.txt' }, result: 'x', ok: true },
  ]);
  const card = wrap.querySelector('.skcard');
  if (!card) return { ok: false, why: 'no card' };
  const name = card.querySelector('.skcard-name').textContent;
  const state = card.querySelector('.skcard-state').textContent;
  card.querySelector('.skcard-head').click();
  const out = {
    name, state,
    open: card.classList.contains('open'),
    body: card.querySelector('.skcard-text').textContent,
    generic: !!wrap.querySelector('.tool-card'),
    icons: !!card.querySelector('.skcard-ico svg'),
  };
  out.ok = name === '代码审查' && state === '已加载' && out.open
    && out.body.indexOf('逐条检查') >= 0 && !out.generic && out.icons;
  // 实时路径用的两个函数：建卡是 running，失败收尾是 failed
  const live = buildSkillCard('skill.read', { name: 'demo' });
  out.running = live.classList.contains('running')
    && live.querySelector('.skcard-state').textContent === '读取中…';
  finishSkillCard(live, { ok: false, result: '❌ 没有这个 Skill：demo', ms: 3 });
  out.failed = live.classList.contains('failed')
    && live.querySelector('.skcard-state').textContent === '未找到';
  out.ok = out.ok && out.running && out.failed;
  card.remove();
  return out;
})()`);
check('Skill 卡片可还原并可展开', skillCard.ok, JSON.stringify(skillCard));

/* ---------------- 2. IDE 工作区 ---------------- */
await goto('ide', 4500);
check('ide 模式已生效', await evaluate(`document.querySelector('#main').classList.contains('ide-mode')`));
check('ide 面板可见', await visible('#ide-panel'));
check('ide 编辑器容器存在', await evaluate(`!!document.querySelector('#ide-editor')`));
check('ide 无控制台报错', errors.length === 0, errors.slice(0, 3).join(' | '));

/* ---------------- 3. 工作台 ---------------- */
await goto('work', 4000);
check('work 面板可见', await visible('#work-panel'));
check('work 三栏存在', await evaluate(`document.querySelectorAll('#work-panel .work-col').length >= 2`));
check('work AI 面板已渲染', await evaluate(`(() => {
  const p = document.querySelector('#work-ai-panel');
  const b = document.querySelector('#work-ai-body');
  return !!p && !p.classList.contains('hidden') && !!b && b.childElementCount > 0;
})()`));
check('work 未触发 AI 请求', await evaluate(`performance.getEntriesByType('resource')
  .filter((e) => /\\/api\\/work\\/assist/.test(e.name)).length === 0`));
check('work 无控制台报错', errors.length === 0, errors.slice(0, 3).join(' | '));

/* ---------------- 4. 设置（分页渲染 + 关闭） ---------------- */
await goto('solo', 2500);
await evaluate(`document.querySelector('#btn-settings').click()`);
await sleep(1000);
check('设置弹窗已打开', await visible('#modal'));
for (const tab of ['agent', 'data', 'help', 'appearance']) {
  const ok = await evaluate(`(() => {
    const btn = document.querySelector('#modal .snav-btn[data-tab="${tab}"]');
    if (!btn) return false;
    btn.click();
    const pane = document.querySelector('#modal .pane[data-pane="${tab}"]');
    return !!pane && !pane.classList.contains('hidden') && pane.textContent.trim().length > 20;
  })()`);
  check(`设置页「${tab}」渲染`, ok);
}
check('设置显示级别与标签栏已渲染', await evaluate(`(() => {
  const lv = document.querySelectorAll('#settings-level-bar .settings-level-btn');
  const tags = document.querySelectorAll('#settings-tagbar .settings-tag-btn');
  return lv.length === 3 && !!document.querySelector('#settings-level-bar .settings-level-btn.active')
    && tags.length > 0;
})()`));
check('设置级别可隐藏并恢复高级项', await evaluate(`(() => {
  const pick = (l) => document.querySelector('#settings-level-bar .settings-level-btn[data-level="' + l + '"]');
  const active = document.querySelector('#settings-level-bar .settings-level-btn.active');
  const orig = active ? active.dataset.level : 'advanced';
  if (pick('expert')) pick('expert').click();
  const base = document.querySelectorAll('#modal .set-card.meta-hidden').length;
  if (pick('basic')) pick('basic').click();
  const hidden = document.querySelectorAll('#modal .set-card.meta-hidden').length;
  if (pick(orig)) pick(orig).click();
  const restored = document.querySelectorAll('#modal .set-card.meta-hidden').length;
  return hidden > base && restored === base;
})()`));
await evaluate(`document.querySelector('#modal .snav-btn[data-tab="data"]').click()`);
await sleep(900);
check('配置历史区已渲染', await evaluate(`(() => {
  const box = document.querySelector('#settings-history-list');
  return !!box && box.textContent.trim().length > 0 && box.textContent.indexOf('加载中') < 0;
})()`));
await evaluate(`document.querySelector('#btn-close-modal').click()`);
await sleep(500);
check('设置弹窗可关闭', !(await visible('#modal')));
check('设置流程无控制台报错', errors.length === 0, errors.slice(0, 3).join(' | '));

/* ---------------- 5. 知识库（打开 / 骨架 / 关闭） ---------------- */
await evaluate(`document.querySelector('#btn-kb').click()`);
await sleep(1800);
check('知识库已打开', await visible('#kb-modal'));
check('知识库内容已渲染', await evaluate(`(() => {
  const m = document.querySelector('#kb-modal');
  return !!m && m.textContent.trim().length > 40 &&
    !!m.querySelector('.kb-side, .kb-graph, .kb-list');
})()`));
await evaluate(`document.querySelector('#btn-close-kb').click()`);
await sleep(600);
check('知识库可关闭', !(await visible('#kb-modal')));
check('知识库流程无控制台报错', errors.length === 0, errors.slice(0, 3).join(' | '));

/* ---------------- 6. 阅读器（打开 / 切换视图 / 关闭） ---------------- */
const pdfs = findSamplePdfs();
if (!pdfs.length) {
  check('阅读器冒烟', false, '未找到样例 PDF（可用 --reader <路径> 或 GM_SMOKE_PDF 指定）');
} else {
  // reader 要求会话有沙箱目录：挑一个已选目录的会话（只改进程内变量，不落库）
  const sids = await evaluate(`(() => {
    const list = (state.sessions && state.sessions.sessions) || [];
    return list.filter((s) => s && s.id && s.sandbox).map((s) => s.id);
  })()`);
  let opened = '';
  let readerErrors = [];
  if (!sids.length) {
    check('阅读器冒烟', false, '没有已设置目录的会话，无法解析文档路径');
  } else {
    for (const sid of sids.slice(0, 5)) {
      await evaluate(`state.sessions.current = ${JSON.stringify(sid)}`);
      for (const pdf of pdfs) {
        errors = [];
        // reader.js 是按需加载的（feature-loader 的 reader 组）
        await evaluate(`(async () => {
          if (!window.Reader && typeof window.gmLoadGroup === 'function') await window.gmLoadGroup('reader');
          return !!(window.Reader && window.Reader.open);
        })()`).catch(() => {});
        await evaluate(`window.Reader && window.Reader.open(${JSON.stringify(pdf)})`).catch(() => {});
        await sleep(2500);
        readerErrors = errors.slice(0, 3);
        if (await visible('#rd-root')) { opened = `${sid} · ${path.basename(pdf)}`; break; }
      }
      if (opened) break;
    }
    check('阅读器已打开', !!opened, opened || `尝试 ${sids.length} 个会话均失败`);
    if (opened) {
      check('阅读器加载出文档', await evaluate(`(() => {
        const root = document.querySelector('#rd-root');
        const n = root && root.querySelector('.rd-name');
        return !!n && n.textContent.trim().length > 0 && !root.classList.contains('hidden');
      })()`));
      await evaluate(`(() => {
        const b = document.querySelector('#rd-root .rd-modes [data-mode="trans"]');
        if (b) b.click();
        return true;
      })()`);
      await sleep(800);
      check('阅读器可切换视图', await evaluate(`(() => {
        const root = document.querySelector('#rd-root');
        return !!root && root.classList.contains('rd-mode-trans');
      })()`));
      await evaluate(`window.Reader && window.Reader.close()`);
      await sleep(800);
      check('阅读器可关闭', !(await evaluate(`(() => {
        const root = document.querySelector('#rd-root');
        return !!root && !root.classList.contains('hidden');
      })()`)));
      // 只统计成功那次打开期间的报错（失败尝试的 400 / 异常不计）
      check('阅读器流程无控制台报错', readerErrors.length === 0, readerErrors.join(' | '));
    }
  }
}

/* ---------------- 汇总 ---------------- */
const failed = checks.filter((c) => !c.ok);
const report = {
  url: BASE, viewport: `${WIDTH}x${HEIGHT}`, at: new Date().toISOString(),
  ok: failed.length === 0, passed: checks.length - failed.length, total: checks.length,
  checks, warnings: soft,
};
console.log(`\n${report.ok ? '通过' : '失败'}：${report.passed}/${report.total}`);
if (soft.length) {
  console.log(`警告（可选增强接口失败，不计入失败）：${soft.length} 条`);
  for (const line of soft.slice(0, 5)) console.log('  · ' + line);
}
if (OUT) {
  mkdirSync(path.dirname(path.resolve(OUT)), { recursive: true });
  writeFileSync(path.resolve(OUT), JSON.stringify(report, null, 2));
  console.log('报告已写入：' + path.resolve(OUT));
}

try { await cdp.send('Browser.close'); } catch (e) { child.kill(); }
ws.close();
setTimeout(() => {
  try { child.kill(); } catch (e) { /* 已退出 */ }
  try { rmSync(profile, { recursive: true, force: true }); } catch (e) { /* 忽略 */ }
}, 600);

process.exitCode = report.ok ? 0 : 1;
