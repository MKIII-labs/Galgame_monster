/**
 * 界面重构基线 / 回归探针（开发用，零 npm 依赖）。
 *
 * 用本机 Edge/Chrome 的 headless 模式 + CDP：
 * - 逐个工作区截图（solo / ide / hall / work / settings）；
 * - 采集 FCP、LCP、CLS、长任务、DOM 节点数、控制台错误；
 * - 支持桌面与手机两档视口。
 *
 * 用法：
 *   node scripts/ui_probe.mjs --url http://127.0.0.1:8791/ --out data/ui-baseline
 *   node scripts/ui_probe.mjs --url ... --out ... --modes solo,settings --mobile
 */
import { spawn } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
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
const URL_ = arg('url', 'http://127.0.0.1:8791/').replace(/\/$/, '') + '/';
const OUT = path.resolve(arg('out', 'data/ui-baseline'));
const MODES = arg('modes', 'solo,ide,hall,work,settings').split(',').map((s) => s.trim()).filter(Boolean);
const MOBILE = process.argv.includes('--mobile');
const WIDTH = MOBILE ? 390 : 1440;
const HEIGHT = MOBILE ? 844 : 900;
const PORT = 9371;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function findBrowser() {
  for (const p of CANDIDATES) if (existsSync(p)) return p;
  throw new Error('未找到 Edge/Chrome，可用 CHROME_PATH 指定');
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
const profile = mkdtempSync(path.join(tmpdir(), 'gm-ui-probe-'));
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
    const res = await fetch(`http://127.0.0.1:${PORT}/json/version`);
    ready = await res.json();
    break;
  } catch (e) { await sleep(250); }
}
if (!ready) { child.kill(); throw new Error('CDP 未就绪'); }

const target = await (await fetch(
  `http://127.0.0.1:${PORT}/json/new?${encodeURIComponent(URL_)}`,
  { method: 'PUT' },
)).json();
const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((res) => ws.addEventListener('open', res));
const cdp = new CDP(ws);

const errors = [];
cdp.on((msg) => {
  if (msg.method === 'Runtime.exceptionThrown') {
    errors.push(msg.params.exceptionDetails.text || 'exception');
  }
  if (msg.method === 'Runtime.consoleAPICalled' && msg.params.type === 'error') {
    errors.push(`console.error: ${(msg.params.args || []).map((a) => a.value || '').join(' ')}`);
  }
});

await cdp.send('Runtime.enable');
await cdp.send('Page.enable');
await cdp.send('Log.enable');
await cdp.send('Emulation.setDeviceMetricsOverride', {
  width: WIDTH, height: HEIGHT, deviceScaleFactor: 1, mobile: MOBILE,
});
await cdp.send('Page.addScriptToEvaluateOnNewDocument', {
  source: `
    window.__uiProbe = { lcp: 0, cls: 0, long: [] };
    try {
      new PerformanceObserver((list) => {
        for (const e of list.getEntries()) window.__uiProbe.lcp = Math.max(window.__uiProbe.lcp, e.startTime);
      }).observe({ type: 'largest-contentful-paint', buffered: true });
      new PerformanceObserver((list) => {
        for (const e of list.getEntries()) {
          if (!e.hadRecentInput) window.__uiProbe.cls += e.value;
        }
      }).observe({ type: 'layout-shift', buffered: true });
      new PerformanceObserver((list) => {
        for (const e of list.getEntries()) window.__uiProbe.long.push(e.duration);
      }).observe({ type: 'longtask', buffered: true });
    } catch (e) {}
  `,
});
await cdp.send('Page.reload', { ignoreCache: true });
await sleep(4500);

mkdirSync(OUT, { recursive: true });
const result = { url: URL_, viewport: `${WIDTH}x${HEIGHT}`, at: new Date().toISOString(), modes: {} };

async function evaluate(expr) {
  const r = await cdp.send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true });
  return r.result.value;
}

async function shot(name) {
  const r = await cdp.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false });
  writeFileSync(path.join(OUT, `${MOBILE ? 'mobile' : 'desktop'}-${name}.png`), Buffer.from(r.data, 'base64'));
}

const metricsJs = `(() => {
  const nav = performance.getEntriesByType('navigation')[0] || {};
  const p = window.__uiProbe || { lcp: 0, cls: 0, long: [] };
  return {
    fcp: Math.round((performance.getEntriesByName('first-contentful-paint')[0] || {}).startTime || 0),
    lcp: Math.round(p.lcp || 0),
    cls: Number((p.cls || 0).toFixed(4)),
    tbt: Math.round((p.long || []).reduce((s, d) => s + Math.max(0, d - 50), 0)),
    dom_nodes: document.querySelectorAll('*').length,
    scripts: document.querySelectorAll('script[src]').length,
    styles: document.querySelectorAll('link[rel=stylesheet]').length,
    doc_height: document.documentElement.scrollHeight,
    overflow_x: document.documentElement.scrollWidth - window.innerWidth,
    dcl: Math.round(nav.domContentLoadedEventEnd || 0),
    load: Math.round(nav.loadEventEnd || 0),
  };
})()`;

for (const mode of MODES) {
  if (mode === 'settings') {
    // 设置是在 Solo 上打开的浮层：先回到 Solo，再打开
    await evaluate(`typeof setUiMode === 'function' && setUiMode('solo')`);
    await sleep(700);
    await cdp.send('Page.reload', { ignoreCache: true });
    await sleep(3500);
    await evaluate(`document.querySelector('#btn-settings') && document.querySelector('#btn-settings').click()`);
    await sleep(1200);
    await shot('settings');
    result.modes.settings = await evaluate(metricsJs);
    await evaluate(`document.querySelector('#btn-close-modal') && document.querySelector('#btn-close-modal').click()`);
    await sleep(400);
    continue;
  }
  // 每个模式从一次全新加载开始测量：否则 LCP / CLS 会把切模式引起的布局位移累计进来
  await evaluate(`typeof setUiMode === 'function' && setUiMode('${mode}')`);
  await sleep(700);
  await cdp.send('Page.reload', { ignoreCache: true });
  await sleep(mode === 'ide' ? 5000 : 3500);
  await shot(mode);
  result.modes[mode] = await evaluate(metricsJs);
}

result.console_errors = errors.slice(0, 20);
writeFileSync(path.join(OUT, 'metrics.json'), JSON.stringify(result, null, 2));
console.log(JSON.stringify(result, null, 2));

try { await cdp.send('Browser.close'); } catch (e) { child.kill(); }
ws.close();
setTimeout(() => {
  try { child.kill(); } catch (e) { /* 已退出 */ }
  try { rmSync(profile, { recursive: true, force: true }); } catch (e) { /* 忽略 */ }
}, 600);
