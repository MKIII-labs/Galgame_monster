/* Computer Bridge · 后台
 *
 * 角色：把控制端的 CDP 命令转给 chrome.debugger，再把事件与结果回传。
 * 通信：HTTP 长轮询（不引 native messaging，省掉注册表与 native host）。
 *
 * 安全边界：
 *   · 只连 127.0.0.1；
 *   · 每条请求都带 X-Bridge-Key（用户从设置里复制过来的配对码）；
 *   · 第一次打开网页时**新建标签页**，不动用户正在看的那一页；
 *   · 站点能否操作由控制端的 policy 决定，扩展只执行。
 */

const POLL_WAIT = 20;
const RETRY_MS = 2000;

let cfg = { base: "", key: "" };
let attachedTabId = null;
let looping = false;

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

async function loadCfg() {
  const s = await chrome.storage.local.get(["base", "key"]);
  cfg = { base: String(s.base || "").trim(), key: String(s.key || "").trim() };
}

function headers() {
  // 用 text/plain：这是「简单请求」，不会触发 CORS 预检（密钥走 ?k=）
  return { "Content-Type": "text/plain;charset=UTF-8" };
}

function api(path) {
  const base = cfg.base.replace(/\/+$/, "");
  const sep = path.indexOf("?") >= 0 ? "&" : "?";
  return base + path + sep + "k=" + encodeURIComponent(cfg.key);
}

async function hello(tab, attached) {
  if (!cfg.base || !cfg.key) return;
  try {
    await fetch(api("/api/computer/bridge/hello"), {
      method: "POST",
      headers: headers(),
      body: JSON.stringify({
        url: tab && tab.url ? tab.url : "",
        title: tab && tab.title ? tab.title : "",
        attached: attached !== false,
      }),
    });
  } catch (e) {
    /* 控制端没开：等下一轮 */
  }
}

async function postResult(id, ok, data, error) {
  try {
    await fetch(api("/api/computer/bridge/result"), {
      method: "POST",
      headers: headers(),
      body: JSON.stringify({ id, ok: !!ok, data: data || null, error: error || "" }),
    });
  } catch (e) {
    /* 控制端断开：结果丢失，下一次会超时报错 */
  }
}

async function postEvent(method, params) {
  try {
    await fetch(api("/api/computer/bridge/event"), {
      method: "POST",
      headers: headers(),
      body: JSON.stringify({ method, params: params || {} }),
    });
  } catch (e) {
    /* ignore */
  }
}

async function aliveTab(id) {
  try {
    return await chrome.tabs.get(id);
  } catch (e) {
    return null;
  }
}

async function attach(tabId) {
  try {
    await chrome.debugger.attach({ tabId }, "1.3");
  } catch (e) {
    // 已经在调试就是正常情况（用户可能重连）
    if (!/already attached/i.test(String(e && e.message || e))) throw e;
  }
}

async function ensureAttached() {
  if (attachedTabId != null) {
    const t = await aliveTab(attachedTabId);
    if (t) return attachedTabId;
    attachedTabId = null;
  }
  const tabs = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  const tab = tabs && tabs[0];
  if (!tab || tab.id == null) throw new Error("没有可用的活动标签页");
  if (!/^https?:/i.test(tab.url || "")) {
    throw new Error("当前标签页不是 http(s) 页面，无法附加调试器");
  }
  await attach(tab.id);
  attachedTabId = tab.id;
  await hello(tab, true);
  return attachedTabId;
}

async function handle(cmd) {
  const args = cmd.args || {};
  if (cmd.op === "open") {
    const url = String(args.url || "");
    if (!/^https?:/i.test(url)) return { ok: false, error: "只支持 http(s) 地址" };
    const tab = await chrome.tabs.create({ url, active: true });
    if (attachedTabId != null && attachedTabId !== tab.id) {
      try {
        await chrome.debugger.detach({ tabId: attachedTabId });
      } catch (e) {
        /* ignore */
      }
    }
    await attach(tab.id);
    attachedTabId = tab.id;
    await hello(tab, true);
    return { ok: true, data: { tabId: tab.id } };
  }
  if (cmd.op === "cdp") {
    const tabId = await ensureAttached();
    try {
      const res = await chrome.debugger.sendCommand({ tabId },
        String(args.method || ""), args.params || {});
      return { ok: true, data: { result: res || {} } };
    } catch (e) {
      return { ok: false, error: String((e && e.message) || e) };
    }
  }
  return { ok: false, error: "未知命令：" + cmd.op };
}

async function loop() {
  if (looping) return;
  looping = true;
  for (;;) {
    await loadCfg();
    if (!cfg.base || !cfg.key) {
      await sleep(3000);
      continue;
    }
    try {
      const r = await fetch(api("/api/computer/bridge/poll?wait=" + POLL_WAIT), {
        headers: headers(),
        cache: "no-store",
      });
      if (!r.ok) {
        await sleep(RETRY_MS);
        continue;
      }
      const cmd = await r.json();
      if (!cmd || !cmd.op) continue;
      let out;
      try {
        out = await handle(cmd);
      } catch (e) {
        out = { ok: false, error: String((e && e.message) || e) };
      }
      await postResult(cmd.id, out.ok, out.data, out.error);
    } catch (e) {
      await sleep(RETRY_MS);
    }
  }
}

chrome.debugger.onEvent.addListener(async (source, method, params) => {
  if (attachedTabId == null || source.tabId !== attachedTabId) return;
  await loadCfg();
  if (!cfg.base || !cfg.key) return;
  await postEvent(method, params);
});

chrome.debugger.onDetach.addListener((source) => {
  if (attachedTabId != null && source.tabId === attachedTabId) {
    attachedTabId = null;
    hello({ url: "", title: "" }, false);
  }
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  if (tabId === attachedTabId && changeInfo.url) {
    chrome.tabs.get(tabId).then((t) => hello(t, true)).catch(() => {});
  }
});

chrome.tabs.onRemoved.addListener((tabId) => {
  if (tabId === attachedTabId) {
    attachedTabId = null;
    hello({ url: "", title: "" }, false);
  }
});

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg && msg.type === "status") {
    sendResponse({ tabId: attachedTabId, base: cfg.base, hasKey: !!cfg.key });
    return true;
  }
  return false;
});

chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create("bridge-keep", { periodInMinutes: 0.5 });
  loop();
});
chrome.runtime.onStartup.addListener(() => loop());
chrome.alarms.onAlarm.addListener(() => loop());
chrome.storage.onChanged.addListener(() => loop());

loop();
