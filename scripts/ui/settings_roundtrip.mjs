// 设置页回环基线：打开设置 → 逐页采集 collectSettings / pane / card / field 清单。
// 重构前后各跑一次，diff 结果必须一致（字段不丢、收集值不变）。
// 用法：node scripts/ui/settings_roundtrip.mjs --out .gm-tmp/settings-baseline.json
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const EDGE = process.env.CHROME_PATH
  || 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const PORT = 9345;
const _srv = (() => { try { return JSON.parse(fs.readFileSync('data/server.json', 'utf8')); }
                      catch (e) { return {}; } })();
const APP = process.env.GM_UI_URL || ('http://127.0.0.1:' + (_srv.port || 52665) + '/');
const outIdx = process.argv.indexOf('--out');
const OUT = path.resolve(outIdx > 0 ? process.argv[outIdx + 1] : '.gm-tmp/settings-baseline.json');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  const profile = path.join(os.tmpdir(), 'gm-settings-' + Date.now());
  const child = spawn(EDGE, ['--headless=new', `--remote-debugging-port=${PORT}`,
    '--remote-allow-origins=*', `--user-data-dir=${profile}`, '--no-first-run',
    '--disable-gpu', '--window-size=1440,900', 'about:blank'], { stdio: 'ignore' });
  let target = null;
  for (let i = 0; i < 40 && !target; i++) {
    await sleep(250);
    try {
      const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
      target = list.find((t) => t.type === 'page');
    } catch (e) { /* 等 */ }
  }
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
  let seq = 0;
  const pending = new Map();
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) {
      const p = pending.get(m.id); pending.delete(m.id);
      m.error ? p.rej(new Error(JSON.stringify(m.error))) : p.res(m.result);
    }
  };
  const send = (method, params = {}) => new Promise((res, rej) => {
    const id = ++seq; pending.set(id, { res, rej });
    ws.send(JSON.stringify({ id, method, params }));
  });
  const evaluate = async (expression) => {
    const r = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
    if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails));
    return r.result.value;
  };

  await send('Page.enable');
  await send('Runtime.enable');
  await send('Network.enable');
  await send('Network.setCacheDisabled', { cacheDisabled: true });
  await send('Page.navigate', { url: APP });
  await sleep(2200);

  const data = await evaluate(`(async () => {
    for (let i = 0; i < 120; i++) {
      if (typeof openModal === 'function' && typeof collectSettings === 'function'
          && typeof SETTINGS_PAGES !== 'undefined' && state.sessions) break;
      await new Promise(r => setTimeout(r, 250));
    }
    const sleep = (ms) => new Promise(r => setTimeout(r, ms));
    openModal({ tab: 'model' });
    await sleep(900);
    const pages = SETTINGS_PAGES.map(p => p.id);
    const panes = allSettingsPanes();
    const out = {
      pages,
      settings: JSON.parse(JSON.stringify(collectSettings())),
      paneOrder: panes.map(p => p.dataset.pane),
      cards: [],
      fields: [],
      visibleByPage: {},
    };
    panes.forEach((pane) => {
      pane.querySelectorAll('.set-card').forEach((c) => {
        out.cards.push({ pane: pane.dataset.pane, title: c.dataset.card || '' });
      });
      pane.querySelectorAll('[id]').forEach((el) => {
        out.fields.push({ id: el.id, tag: el.tagName, type: el.type || '' });
      });
    });
    for (const id of pages) {
      setSettingsTab(id);
      await sleep(180);
      out.visibleByPage[id] = {
        panes: Array.from(document.querySelectorAll('#modal .pane:not(.hidden)')).map(p => p.dataset.pane),
        cards: document.querySelectorAll('#modal .pane:not(.hidden) .set-card').length,
      };
    }
    const close = document.querySelector('#btn-close-modal');
    if (close) close.click();
    await sleep(200);
    return out;
  })()`);

  fs.mkdirSync(path.dirname(OUT), { recursive: true });
  fs.writeFileSync(OUT, JSON.stringify(data, null, 1), 'utf8');
  console.log('wrote', OUT);
  console.log('pages', data.pages.length, 'panes', data.paneOrder.length,
              'cards', data.cards.length, 'fields', data.fields.length);
  await send('Browser.close').catch(() => {});
  child.kill();
  ws.close();
}

main().then(() => process.exit(0)).catch((e) => { console.error('FAIL:', e.message); process.exit(1); });
