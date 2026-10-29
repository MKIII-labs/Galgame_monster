/* 弹窗：只显示配置与接管状态 */

async function refresh() {
  const s = await chrome.storage.local.get(["base", "key"]);
  document.getElementById("cfg").textContent =
    s.base && s.key ? (s.base + " ✓") : "未配置";
  try {
    const r = await chrome.runtime.sendMessage({ type: "status" });
    document.getElementById("tab").textContent = r && r.tabId ? ("#" + r.tabId) : "—";
    document.getElementById("st").textContent = r && r.tabId ? "已接管" : "待命";
    document.getElementById("st").className = r && r.tabId ? "ok" : "bad";
  } catch (e) {
    document.getElementById("st").textContent = "后台未就绪";
    document.getElementById("st").className = "bad";
  }
}

document.getElementById("opt").addEventListener("click", () => {
  chrome.runtime.openOptionsPage();
});

refresh();
