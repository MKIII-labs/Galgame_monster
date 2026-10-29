/* 设置页：解析连接码、保存、测试连接 */

function parsePair(raw) {
  const s = String(raw || "").trim();
  const i = s.indexOf("#");
  if (i < 0) return null;
  const base = s.slice(0, i).replace(/\/+$/, "");
  const key = s.slice(i + 1).trim();
  if (!/^https?:\/\/127\.0\.0\.1(:\d+)?$/.test(base) || !key) return null;
  return { base, key };
}

function setMsg(text) {
  document.getElementById("msg").textContent = text;
}

async function load() {
  const s = await chrome.storage.local.get(["base", "key"]);
  if (s.base && s.key) {
    document.getElementById("pair").value = s.base + "#" + s.key;
  }
}

document.getElementById("save").addEventListener("click", async () => {
  const p = parsePair(document.getElementById("pair").value);
  if (!p) {
    setMsg("连接码格式不对：应为 http://127.0.0.1:<端口>#<密钥>");
    return;
  }
  await chrome.storage.local.set(p);
  setMsg("已保存。扩展会自动开始轮询。");
});

document.getElementById("test").addEventListener("click", async () => {
  const p = parsePair(document.getElementById("pair").value);
  if (!p) {
    setMsg("连接码格式不对。");
    return;
  }
  setMsg("正在测试…");
  try {
    const r = await fetch(p.base + "/api/computer/bridge/hello?k=" + encodeURIComponent(p.key), {
      method: "POST",
      headers: { "Content-Type": "text/plain;charset=UTF-8" },
      body: JSON.stringify({ url: "", title: "", attached: false }),
    });
    if (r.ok) {
      await chrome.storage.local.set(p);
      setMsg("连接成功，已保存。");
    } else if (r.status === 403) {
      setMsg("连接被拒绝：密钥不对，或应用里重新生成过连接码。");
    } else {
      setMsg("应用返回 HTTP " + r.status);
    }
  } catch (e) {
    setMsg("连不上：" + ((e && e.message) || e) + "\n请确认应用在运行，且端口与连接码一致。");
  }
});

load();
