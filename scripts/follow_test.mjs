/**
 * 自动跟随（web/follow.js）极端场景测试（只读，零 npm 依赖）。
 *
 * 在真实页面里创建一个离屏滚动容器，用 GmFollow.bind 绑定后模拟：
 * 流式追加、瞬间跨屏跳转、上方内容突变、内容变矮、用户上翻 / 回底、
 * 选区暂停、反复增减、外部程序滚动等场景。
 * 不调用任何会写数据的接口，也不碰真实对话 DOM。
 *
 * 用法：
 *   node scripts/follow_test.mjs --url http://127.0.0.1:52665/
 *
 * 退出码：0 全过；1 有失败项。
 */
import { spawn } from 'node:child_process';
import { existsSync, mkdtempSync, rmSync } from 'node:fs';
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
const PORT = 9375;
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
    ws.addEventListener('message', (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        if (msg.error) reject(new Error(msg.error.message));
        else resolve(msg.result);
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
}

const browser = findBrowser();
const profile = mkdtempSync(path.join(tmpdir(), 'gm-follow-test-'));
const child = spawn(browser, [
  '--headless=new', '--disable-gpu', '--no-first-run', '--hide-scrollbars',
  '--window-size=1280,900', `--user-data-dir=${profile}`,
  `--remote-debugging-port=${PORT}`, 'about:blank',
], { stdio: 'ignore' });

let ready = null;
for (let i = 0; i < 60; i++) {
  try { ready = await (await fetch(`http://127.0.0.1:${PORT}/json/version`)).json(); break; }
  catch (e) { await sleep(250); }
}
if (!ready) { child.kill(); throw new Error('CDP 未就绪'); }

const target = await (await fetch(
  `http://127.0.0.1:${PORT}/json/new?${encodeURIComponent(BASE)}`,
  { method: 'PUT' },
)).json();
const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((res) => ws.addEventListener('open', res));
const cdp = new CDP(ws);
await cdp.send('Runtime.enable');
await cdp.send('Page.enable');
for (let i = 0; i < 100; i++) {
  try {
    const r = await cdp.send('Runtime.evaluate', { expression: 'document.readyState', returnByValue: true });
    if (r.result.value === 'complete') break;
  } catch (e) { /* 加载中 */ }
  await sleep(100);
}
await sleep(1200);

const script = `(async () => {
  const raf = () => new Promise((r) => requestAnimationFrame(() => r()));
  const frames = async (n) => { for (let i = 0; i < n; i++) await raf(); };
  const results = [];
  const check = (name, ok, detail) => results.push({ name, ok: !!ok, detail: detail || '' });

  const host = document.createElement('div');
  host.id = '__gm_follow_test';
  host.style.cssText = 'position:fixed;left:-9999px;top:0;width:360px;height:300px;'
    + 'overflow:auto;overflow-anchor:none;contain:layout paint;';
  const content = document.createElement('div');
  content.style.cssText = 'height:0px;';
  content.textContent = 'x';
  host.appendChild(content);
  document.body.appendChild(host);
  await frames(2);

  const ctl = GmFollow.bind({ scrollEl: host, pinned: true });
  const dist = () => host.scrollHeight - host.clientHeight - host.scrollTop;

  // 1) 流式追加：每一帧都在长高，不能被判成「用户离开」
  let maxGap = 0;
  for (let i = 0; i < 40; i++) {
    content.style.height = ((i + 1) * 40) + 'px';
    await raf();
    maxGap = Math.max(maxGap, dist());
  }
  await frames(24);
  check('流式追加：始终贴底', dist() <= 1 && ctl.isPinned(), 'dist=' + dist().toFixed(1));
  check('流式追加：单帧最大偏差受控', maxGap <= 240, 'maxGap=' + maxGap.toFixed(0));

  // 2) 瞬间跨屏跳转：两帧内到位（走瞬移分支，不做长动画）
  content.style.height = '5000px';
  await frames(3);
  check('跨屏跳转：3 帧内贴底', dist() <= 1 && ctl.isPinned(), 'dist=' + dist().toFixed(1));

  // 3) 上方插入内容（等价于「历史消息里的图片突然加载完」）
  const top = document.createElement('div');
  top.style.height = '600px';
  host.insertBefore(top, content);
  await frames(5);
  check('上方高度突变：保持贴底', dist() <= 1 && ctl.isPinned(), 'dist=' + dist().toFixed(1));

  // 4) 内容变矮：位置被夹到底部，跟随不丢
  content.style.height = '3000px';
  await frames(5);
  check('内容变矮：夹到底部且不丢跟随', dist() <= 1 && ctl.isPinned(), 'dist=' + dist().toFixed(1));

  // 5) 用户上翻：立刻解除跟随，后续增长不把窗口拽走
  host.dispatchEvent(new WheelEvent('wheel', { deltaY: -120, bubbles: true }));
  check('滚轮上翻：立即解除跟随', !ctl.isPinned());
  const before = host.scrollTop;
  content.style.height = '3800px';
  await frames(6);
  check('上翻后新增内容：位置不被拽走', Math.abs(host.scrollTop - before) <= 2,
        'moved=' + (host.scrollTop - before).toFixed(1));

  // 6) 用户回到底部：自动恢复跟随
  host.scrollTop = host.scrollHeight;
  await frames(4);
  check('回到底部：自动恢复跟随', ctl.isPinned(), 'dist=' + dist().toFixed(1));

  // 7) 外部程序滚动到历史：视为用户意图（不抢滚动条）
  host.scrollTop -= 150;
  await frames(4);
  check('外部上滚：解除跟随', !ctl.isPinned(), 'dist=' + dist().toFixed(1));

  // 8) 选区暂停：选中文字期间内容增长不滚动，取消选区后恢复
  ctl.snap();
  await frames(2);
  const range = document.createRange();
  range.selectNodeContents(content);
  const sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(range);
  await frames(2);
  const selTop = host.scrollTop;
  content.style.height = '4200px';
  await frames(5);
  check('选区中：不自动滚动', Math.abs(host.scrollTop - selTop) <= 2,
        'moved=' + (host.scrollTop - selTop).toFixed(1));
  sel.removeAllRanges();
  await frames(6);
  check('取消选区：恢复跟随', dist() <= 1, 'dist=' + dist().toFixed(1));

  // 9) 反复增减（工具卡展开 / 收起）：最终稳定贴底、不抖动
  let flips = 0;
  let prevDist = dist();
  for (let i = 0; i < 10; i++) {
    content.style.height = (i % 2 ? 3400 : 4200) + 'px';
    await raf();
    const d = dist();
    if (Math.abs(d - prevDist) > 2) flips += 1;
    prevDist = d;
  }
  await frames(10);
  check('反复增减：最终贴底', dist() <= 1 && ctl.isPinned(), 'dist=' + dist().toFixed(1));
  check('反复增减：滚动方向不来回抽搐', flips <= 4, '变化次数=' + flips);

  // 10) 解除跟随后的高度突变不能反向吸附
  host.dispatchEvent(new WheelEvent('wheel', { deltaY: -120, bubbles: true }));
  content.style.height = '6000px';
  await frames(6);
  check('未跟随时高度突变：不重新吸附', !ctl.isPinned(), 'dist=' + dist().toFixed(1));

  ctl.destroy();
  host.remove();

  // 11) 真实对话区（只做 DOM 冒烟，不写数据）：只有当前正在 Solo 且已有消息时才跑
  const chat = document.querySelector('#chat');
  if (!chat || !chat.clientHeight || !chat.children.length || document.querySelector('#empty')) {
    check('真实对话区冒烟', true, '跳过（当前没有可见的对话内容）');
    return results;
  }
  window.scrollToBottom(true);
  await frames(8);
  check('对话区：初始贴底', chat.scrollHeight - chat.clientHeight - chat.scrollTop <= 1
        && chat.classList.contains('following'),
        'dist=' + (chat.scrollHeight - chat.clientHeight - chat.scrollTop).toFixed(1));

  // 内容高度突变（等价于图片 / 代码高亮后高度跳变）
  const probe = document.createElement('div');
  probe.style.height = '900px';
  chat.appendChild(probe);
  await frames(8);
  check('对话区：高度突变后仍贴底', chat.scrollHeight - chat.clientHeight - chat.scrollTop <= 1
        && chat.classList.contains('following'),
        'dist=' + (chat.scrollHeight - chat.clientHeight - chat.scrollTop).toFixed(1));
  probe.remove();
  await frames(4);

  // 用户上翻 -> 按钮出现 / 跟随解除；点回到底部 -> 恢复并再次隐藏
  const btn = document.querySelector('#btn-bottom');
  check('对话区：贴底时按钮隐藏', !!btn && btn.classList.contains('off'));
  chat.scrollTop = Math.max(0, chat.scrollTop - 300);
  await frames(4);
  check('对话区：上翻显示回到底部按钮',
        !!btn && !btn.classList.contains('off') && !chat.classList.contains('following'));
  if (btn) btn.click();
  await frames(8);
  check('对话区：点回到底部恢复跟随', chat.classList.contains('following')
        && chat.scrollHeight - chat.clientHeight - chat.scrollTop <= 1
        && !!btn && btn.classList.contains('off'),
        'dist=' + (chat.scrollHeight - chat.clientHeight - chat.scrollTop).toFixed(1));

  return results;
})()`;

let results = [];
try {
  const res = await cdp.send('Runtime.evaluate', {
    expression: script, returnByValue: true, awaitPromise: true,
  });
  if (res.exceptionDetails) {
    const d = res.exceptionDetails;
    throw new Error((d.exception && d.exception.description) || d.text || '测试脚本异常');
  }
  results = res.result.value || [];
} catch (e) {
  console.error('测试失败：' + e.message);
  results = [{ name: '测试脚本执行', ok: false, detail: e.message }];
}

let failed = 0;
for (const r of results) {
  if (!r.ok) failed += 1;
  console.log(`${r.ok ? 'PASS' : 'FAIL'}  ${r.name}${r.detail ? '  — ' + r.detail : ''}`);
}
console.log(`\n${failed ? '失败' : '通过'}：${results.length - failed}/${results.length}`);

try { await cdp.send('Browser.close'); } catch (e) { child.kill(); }
ws.close();
setTimeout(() => {
  try { child.kill(); } catch (e) { /* 已退出 */ }
  try { rmSync(profile, { recursive: true, force: true }); } catch (e) { /* 忽略 */ }
}, 600);

process.exitCode = failed ? 1 : 0;
