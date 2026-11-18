// 设置页交互回归：导航 / 二级分段 / 搜索跳转 / 分区高级项 / 手机列表→详情。
// 用法：node scripts/ui/settings_interaction.mjs
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const EDGE = process.env.CHROME_PATH
  || 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const PORT = 9346;
const _srv = (() => { try { return JSON.parse(fs.readFileSync('data/server.json', 'utf8')); }
                      catch (e) { return {}; } })();
const APP = process.env.GM_UI_URL || ('http://127.0.0.1:' + (_srv.port || 52665) + '/');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const FAIL = [];

function ok(cond, label, extra) {
  console.log((cond ? '  [OK]   ' : '  [FAIL] ') + label + (extra ? '  ' + extra : ''));
  if (!cond) FAIL.push(label);
}

async function main() {
  const profile = path.join(os.tmpdir(), 'gm-settings-ui-' + Date.now());
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
  const errors = [];
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) {
      const p = pending.get(m.id); pending.delete(m.id);
      m.error ? p.rej(new Error(JSON.stringify(m.error))) : p.res(m.result);
      return;
    }
    if (m.method === 'Runtime.exceptionThrown') {
      const d = m.params.exceptionDetails || {};
      const ex = d.exception || {};
      errors.push((ex.description || d.text || 'exception').split('\n')[0]);
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

  const ready = `(async () => {
    for (let i = 0; i < 120; i++) {
      if (typeof openModal === 'function' && typeof SETTINGS_PAGES !== 'undefined'
          && state.sessions) return true;
      await new Promise(r => setTimeout(r, 250));
    }
    throw new Error('前端未就绪');
  })()`;

  // ---------- 桌面 ----------
  await send('Page.navigate', { url: APP });
  await sleep(2200);
  await evaluate(ready);
  const desk = await evaluate(`(async () => {
    const sleep = (ms) => new Promise(r => setTimeout(r, ms));
    const q = (s) => document.querySelector(s);
    openModal({ tab: 'model' });
    await sleep(900);
    const out = {};
    out.pages = SETTINGS_PAGES.map(p => p.id);
    out.navCount = document.querySelectorAll('#modal .settings-nav .snav-btn').length;
    out.levelBar = !!q('#settings-level-bar');
    out.tocVisible = !!q('#settings-toc') && !q('#settings-toc').classList.contains('hidden');
    out.tocItems = document.querySelectorAll('#settings-anchors .set-anchor').length;
    out.modelPanes = Array.from(document.querySelectorAll('#modal .pane:not(.hidden)')).map(p => p.dataset.pane);
    out.attachedPanes = document.querySelectorAll('#modal .pane').length;
    out.detachedPanes = SETTINGS_PANE_STORE ? SETTINGS_PANE_STORE.size : -1;
    out.modalNodes = document.querySelectorAll('#modal *').length;

    // 能力与工具：二级分段
    q('.snav-btn[data-tab="tools"]').click();
    await sleep(350);
    out.subnav = Array.from(document.querySelectorAll('#settings-subnav [data-sub]'))
      .map(b => ({ id: b.dataset.sub, active: b.classList.contains('active') }));
    out.toolsDefault = Array.from(document.querySelectorAll('#modal .pane:not(.hidden)')).map(p => p.dataset.pane);
    q('#settings-subnav [data-sub="flow"]').click();
    await sleep(250);
    out.toolsFlow = Array.from(document.querySelectorAll('#modal .pane:not(.hidden)')).map(p => p.dataset.pane);
    q('#settings-subnav [data-sub="ext"]').click();
    await sleep(250);
    out.toolsExt = Array.from(document.querySelectorAll('#modal .pane:not(.hidden)')).map(p => p.dataset.pane);
    q('#settings-subnav [data-sub="image"]').click();
    await sleep(250);
    out.toolsImage = Array.from(document.querySelectorAll('#modal .pane:not(.hidden)')).map(p => p.dataset.pane);

    // 分区高级项
    q('.snav-btn[data-tab="model"]').click();
    await sleep(300);
    const sec = Array.from(document.querySelectorAll('#modal .set-section'))
      .find(s => !s.querySelector('[data-act="toggle-section-adv"]').classList.contains('hidden'));
    if (sec) {
      out.advBefore = sec.querySelectorAll('.set-card.adv-hidden').length;
      sec.querySelector('[data-act="toggle-section-adv"]').click();
      await sleep(150);
      out.advAfter = sec.querySelectorAll('.set-card.adv-hidden').length;
      out.advLabel = sec.querySelector('[data-act="toggle-section-adv"]').textContent;
    } else {
      out.advBefore = out.advAfter = -1;
    }

    // 跨页字段保留：外观页 detached 之后，collectSettings 仍读到最新值
    q('.snav-btn[data-tab="ui"]').click();
    await sleep(250);
    const cb = q('#ui-process-follow');
    const before = cb.checked;
    cb.checked = !before;
    q('.snav-btn[data-tab="model"]').click();
    await sleep(250);
    out.detachedCollect = collectSettings().config.ui_process_follow;
    out.detachedCollectExpected = !before;
    out.detachedLookup = typeof window.__gmDetachedEl === 'function'
      && !!window.__gmDetachedEl('#ui-process-follow');
    out.detachedIndexSize = (typeof SETTINGS_FIELD_INDEX !== 'undefined') ? SETTINGS_FIELD_INDEX.size : -1;
    const direct = $('#ui-process-follow');
    out.directHas = !!direct;
    out.directChecked = direct ? direct.checked : null;
    out.collectHasLine = collectSettings.toString().indexOf('ui_process_follow') >= 0;

    // 搜索跳转（压缩在 context 页，且可能是高级项）
    const inp = q('#settings-search-input');
    inp.value = '压缩';
    inp.dispatchEvent(new Event('input', { bubbles: true }));
    await sleep(250);
    out.searchCount = document.querySelectorAll('#settings-search-list .settings-search-item').length;
    // 只取坐标，点击用真实鼠标事件：搜索结果项是 div，曾因标题栏拖动
    // 捕获指针导致真实点击被重定向到标题栏（el.click() 测不出来）
    const item = q('#settings-search-list .settings-search-item');
    const ir = item ? item.getBoundingClientRect() : null;
    out.searchRect = ir ? { x: ir.left + ir.width / 2, y: ir.top + ir.height / 2 } : null;
    return out;
  })()`);
  if (desk.searchRect) {
    const { x, y } = desk.searchRect;
    await send('Input.dispatchMouseEvent', { type: 'mouseMoved', x, y, button: 'none', clickCount: 0 });
    await send('Input.dispatchMouseEvent', { type: 'mousePressed', x, y, button: 'left', clickCount: 1 });
    await send('Input.dispatchMouseEvent', { type: 'mouseReleased', x, y, button: 'left', clickCount: 1 });
    await sleep(600);
  }
  const searchAfter = await evaluate(`(() => {
    const q = (s) => document.querySelector(s);
    const flash = q('#modal .settings-flash');
    const content = q('#modal .settings-content');
    const fr = flash ? flash.getBoundingClientRect() : null;
    const cr = content ? content.getBoundingClientRect() : null;
    return {
      page: q('#settings-page-title').textContent.trim(),
      panes: Array.from(document.querySelectorAll('#modal .pane:not(.hidden)')).map(p => p.dataset.pane),
      listHidden: q('#settings-search-list').classList.contains('hidden'),
      flashVisible: !!flash && flash.offsetParent !== null,
      flashInView: !!(fr && cr && fr.top >= cr.top - 4 && fr.bottom <= cr.bottom + 4),
    };
  })()`);

  ok(desk.pages.length === 6, '6 个一级页面', desk.pages.join(','));
  ok(desk.navCount === 6, '导航按钮 6 个', String(desk.navCount));
  ok(!desk.levelBar, '顶部级别筛选已移除');
  ok(desk.tocVisible && desk.tocItems >= 2, '右侧本页目录可见', 'items=' + desk.tocItems);
  ok(desk.modelPanes.join(',') === 'chat,context', '模型与对话含 chat+context', desk.modelPanes.join(','));
  ok(desk.attachedPanes === 2 && desk.detachedPanes === 15, '按需挂载：只留当前页 pane',
     'attached=' + desk.attachedPanes + ' detached=' + desk.detachedPanes + ' modalNodes=' + desk.modalNodes);
  ok(desk.subnav.length === 4 && desk.subnav[0].active, '能力与工具 4 个二级分段',
     JSON.stringify(desk.subnav));
  ok(desk.toolsDefault.join(',') === 'agent', '默认执行策略只显示 agent', desk.toolsDefault.join(','));
  ok(desk.toolsFlow.join(',') === 'flow,agents', '子代理分段显示 flow+agents', desk.toolsFlow.join(','));
  ok(desk.toolsExt.join(',') === 'ext,skills', '外部工具分段显示 ext+skills', desk.toolsExt.join(','));
  ok(desk.toolsImage.join(',') === 'image', '图像分段显示 image', desk.toolsImage.join(','));
  ok(desk.advBefore > 0 && desk.advAfter === 0, '分区高级项可展开',
     desk.advBefore + ' -> ' + desk.advAfter + ' ' + (desk.advLabel || ''));
  ok(desk.detachedCollect === desk.detachedCollectExpected,
     'detached 页面字段仍参与 collectSettings',
     String(desk.detachedCollect) + ' vs ' + String(desk.detachedCollectExpected)
     + ' lookup=' + desk.detachedLookup + ' index=' + desk.detachedIndexSize
     + ' direct=' + desk.directHas + '/' + desk.directChecked
     + ' hasLine=' + desk.collectHasLine);
  ok(desk.searchCount > 0, '搜索「压缩」有结果', String(desk.searchCount));
  ok(searchAfter.page === '模型与对话' && searchAfter.panes.indexOf('context') >= 0,
     '真实鼠标点击搜索结果可跳转到上下文页', searchAfter.page + ' / ' + searchAfter.panes.join(','));
  ok(searchAfter.listHidden, '搜索跳转后结果列表收起');
  ok(searchAfter.flashVisible && searchAfter.flashInView, '搜索目标可见（高级项已展开）');

  // ---------- 手机 ----------
  await send('Emulation.setDeviceMetricsOverride',
             { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  await send('Page.reload', { ignoreCache: true });
  await sleep(2200);
  await evaluate(ready);
  const mob = await evaluate(`(async () => {
    const sleep = (ms) => new Promise(r => setTimeout(r, ms));
    const q = (s) => document.querySelector(s);
    const shown = (el) => !!el && getComputedStyle(el).display !== 'none';
    openModal();
    await sleep(900);
    const out = {};
    out.homeNav = shown(q('#modal .settings-nav'));
    out.homeContent = shown(q('#modal .settings-content'));
    q('.snav-btn[data-tab="model"]').click();
    await sleep(350);
    out.pageNav = shown(q('#modal .settings-nav'));
    out.pageContent = shown(q('#modal .settings-content'));
    out.back = shown(q('#settings-back'));
    out.mobileClass = q('#modal').classList.contains('mobile-page');
    q('#settings-back').click();
    await sleep(300);
    out.backHomeNav = shown(q('#modal .settings-nav'));
    out.backHomeContent = shown(q('#modal .settings-content'));
    q('#btn-close-modal').click();
    await sleep(200);
    return out;
  })()`);

  ok(mob.homeNav && !mob.homeContent, '手机打开先显示分类列表',
     JSON.stringify({ nav: mob.homeNav, content: mob.homeContent }));
  ok(!mob.pageNav && mob.pageContent && mob.back && mob.mobileClass,
     '手机选中页进入详情并显示返回',
     JSON.stringify({ nav: mob.pageNav, content: mob.pageContent, back: mob.back }));
  ok(mob.backHomeNav && !mob.backHomeContent, '返回键回到分类列表',
     JSON.stringify({ nav: mob.backHomeNav, content: mob.backHomeContent }));

  console.log('runtime exceptions:', errors.length ? JSON.stringify(errors) : 'none');
  await send('Browser.close').catch(() => {});
  child.kill();
  ws.close();

  console.log('\n' + '='.repeat(46));
  if (FAIL.length) {
    console.log('✗ 失败 ' + FAIL.length + ' 项：' + FAIL.join('；'));
    process.exit(1);
  }
  console.log('✓ 设置交互回归通过');
}

main().then(() => process.exit(0)).catch((e) => { console.error('FAIL:', e.message); process.exit(1); });
