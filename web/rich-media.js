'use strict';

/* ============================================================
   AI 可视内容渲染器：SVG / ECharts / Mermaid / 沙箱图片

   - markdown.js 只产出占位块（.rich-media[data-rich] / .md-img-sandbox）
   - 这里负责把占位块变成真的图；渲染失败退回源码，不吞内容
   - SVG 一律用 <img src="data:..."> 渲染：SVG 里的脚本不会执行（隔离）
   - 图表 / 流程图库走懒加载（CDN），离线时退回源码
   ============================================================ */
(function () {
  if (window.enhanceRichMedia) return;

  const charts = new Set();          // { chart, ro }
  const imgCache = new Map();        // "sid|path" -> objectURL
  const imgBusy = new Map();         // "sid|path" -> Promise<url>
  const libLoading = new Set();
  let theming = false;

  const isDark = () => (typeof isDarkTheme === 'function' ? isDarkTheme() : false);

  function whenLib(name, cb, tries) {
    const n = tries || 0;
    if (window[name]) { cb(true); return; }
    if (n > 120) { cb(false); return; }          // 12 秒还拿不到就放弃
    if (!libLoading.has(name)) {
      libLoading.add(name);
      if (window.loadLazyLib) window.loadLazyLib(name);
    }
    setTimeout(() => whenLib(name, cb, n + 1), 100);
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function fallback(body, msg, code) {
    body.className = 'rich-media-body';
    body.style.height = '';
    body.innerHTML = '<div class="rich-media-error">' + esc(msg) + '</div>' +
      (code ? '<pre class="rich-media-fallback">' + esc(code) + '</pre>' : '');
  }

  function downloadHref(href, name) {
    const a = document.createElement('a');
    a.href = href;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  function downloadText(text, name, mime) {
    const blob = new Blob([text], { type: mime || 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    downloadHref(url, name);
    setTimeout(() => URL.revokeObjectURL(url), 5000);
  }

  // ---------------- SVG ----------------
  function renderSvg(box, body, code) {
    const raw = String(code || '').trim();
    if (!/<svg[\s>]/i.test(raw)) {
      fallback(body, '这段 SVG 里找不到 <svg> 根元素', raw);
      return;
    }
    let svg = raw;
    if (!/xmlns\s*=/.test(svg.slice(0, 400))) {
      svg = svg.replace(/<svg/i, '<svg xmlns="http://www.w3.org/2000/svg"');
    }
    const url = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg);
    const img = document.createElement('img');
    img.className = 'rich-img';
    img.alt = 'AI 生成的图形';
    img.onerror = () => fallback(body, 'SVG 解析失败，请检查标签是否闭合', raw);
    img.src = url;
    body.innerHTML = '';
    body.appendChild(img);
    const dl = box.querySelector('[data-rich-download]');
    if (dl) dl.onclick = () => downloadHref(url, 'figure.svg');
  }

  // ---------------- ECharts ----------------
  function renderChart(box, body, code) {
    let opt;
    try {
      opt = JSON.parse(code);
    } catch (e) {
      fallback(body, '图表 JSON 解析失败：' + e.message, code);
      return;
    }
    if (!opt || typeof opt !== 'object' || Array.isArray(opt)) {
      fallback(body, '图表需要是一个 ECharts option 对象', code);
      return;
    }
    let height = 340;
    if (typeof opt.__height === 'number' && opt.__height > 80) {
      height = Math.min(1200, Math.round(opt.__height));
      delete opt.__height;
    }
    whenLib('echarts', (ok) => {
      if (!ok || !window.echarts) {
        fallback(body, 'ECharts 未加载（离线环境不可用），下面是图表定义', code);
        return;
      }
      body.className = 'rich-media-body rich-chart';
      body.style.height = height + 'px';
      body.innerHTML = '';
      let chart;
      try {
        chart = window.echarts.init(body, isDark() ? 'dark' : null, { renderer: 'canvas' });
        chart.setOption(opt, true);
      } catch (e) {
        if (chart) { try { chart.dispose(); } catch (err) { /* 忽略 */ } }
        fallback(body, '图表渲染失败：' + e.message, code);
        return;
      }
      let ro = null;
      if (window.ResizeObserver) {
        ro = new ResizeObserver(() => { try { chart.resize(); } catch (e) { /* 忽略 */ } });
        ro.observe(body);
      }
      charts.add({ chart, ro });
      const dl = box.querySelector('[data-rich-download]');
      if (dl) {
        dl.onclick = () => {
          const bg = getComputedStyle(document.body).backgroundColor || '#ffffff';
          downloadHref(chart.getDataURL({ pixelRatio: 2, backgroundColor: bg }), 'chart.png');
        };
      }
    });
  }

  // ---------------- Mermaid ----------------
  function renderMermaid(box, body, code) {
    whenLib('mermaid', (ok) => {
      if (!ok || !window.mermaid) {
        fallback(body, 'Mermaid 未加载（离线环境不可用），下面是流程图源码', code);
        return;
      }
      const m = window.mermaid;
      try {
        m.initialize({
          startOnLoad: false,
          securityLevel: 'strict',
          theme: isDark() ? 'dark' : 'default',
        });
      } catch (e) { /* 重复 initialize 不影响渲染 */ }
      const id = 'gm-mmd-' + Math.random().toString(36).slice(2);
      Promise.resolve()
        .then(() => m.render(id, code))
        .then((out) => {
          const svg = (out && out.svg) || '';
          if (!svg) throw new Error('没有生成 SVG');
          body.innerHTML = svg;
          const dl = box.querySelector('[data-rich-download]');
          if (dl) dl.onclick = () => downloadText(svg, 'diagram.svg', 'image/svg+xml');
        })
        .catch((e) => fallback(body, '流程图渲染失败：' + (e && e.message ? e.message : e), code));
    });
  }

  // ---------------- 沙箱图片 ----------------
  function currentSid() {
    if (typeof curSession !== 'function') return '';
    const s = curSession();
    return s && s.id ? s.id : '';
  }

  async function sandboxImageUrl(sid, path) {
    const key = sid + '|' + path;
    if (imgCache.has(key)) return imgCache.get(key);
    if (imgBusy.has(key)) return imgBusy.get(key);
    const p = fetch('/api/sandbox/raw', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Auth-Token': TOKEN },
      body: JSON.stringify({ session_id: sid, path }),
    }).then(async (res) => {
      if (!res.ok) {
        let msg = 'HTTP ' + res.status;
        try {
          const j = await res.json();
          if (j && (j.error || j.message)) msg = j.error || j.message;
        } catch (e) { /* 非 JSON */ }
        throw new Error(msg);
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      imgCache.set(key, url);
      return url;
    }).finally(() => imgBusy.delete(key));
    imgBusy.set(key, p);
    return p;
  }

  function fillSandboxImages(root) {
    const scope = root || document;
    scope.querySelectorAll('.md-img-sandbox[data-src]:not([data-img-done])').forEach((el) => {
      el.setAttribute('data-img-done', '1');
      const sid = currentSid();
      if (!sid) { el.textContent = '（图片需要先选中会话）'; return; }
      sandboxImageUrl(sid, el.getAttribute('data-src')).then((url) => {
        const img = document.createElement('img');
        img.className = 'md-img';
        img.alt = el.getAttribute('data-alt') || '';
        img.src = url;
        if (el.parentNode) el.parentNode.replaceChild(img, el);
      }).catch((e) => {
        el.textContent = '图片加载失败：' + (e && e.message ? e.message : e);
        el.classList.add('md-img-error');
      });
    });
  }

  // ---------------- 入口 ----------------
  function enhance(root) {
    const scope = root || document;
    fillSandboxImages(scope);
    scope.querySelectorAll('.rich-media[data-rich]:not([data-rich-done])').forEach((box) => {
      box.setAttribute('data-rich-done', '1');
      const kind = box.getAttribute('data-rich');
      const body = box.querySelector('.rich-media-body');
      const srcEl = box.querySelector('.rich-media-src');
      const code = srcEl ? srcEl.textContent : '';
      if (!body) return;
      if (kind === 'svg') renderSvg(box, body, code);
      else if (kind === 'chart') renderChart(box, body, code);
      else if (kind === 'mermaid') renderMermaid(box, body, code);
    });
    scope.querySelectorAll('.rich-media [data-rich-source]').forEach((btn) => {
      if (btn.dataset.bound) return;
      btn.dataset.bound = '1';
      btn.onclick = () => {
        const box = btn.closest('.rich-media');
        if (!box) return;
        const on = box.classList.toggle('show-src');
        btn.textContent = on ? '隐藏源码' : '源码';
      };
    });
  }

  function dispose() {
    charts.forEach((it) => {
      if (it.ro) { try { it.ro.disconnect(); } catch (e) { /* 忽略 */ } }
      try { it.chart.dispose(); } catch (e) { /* 忽略 */ }
    });
    charts.clear();
  }

  /* 主题切换：图表 / 流程图要按新配色重画（SVG 与图片不需要） */
  function reTheme() {
    if (theming) return;
    theming = true;
    dispose();
    document.querySelectorAll('.rich-media[data-rich="chart"], .rich-media[data-rich="mermaid"]')
      .forEach((box) => {
        box.removeAttribute('data-rich-done');
        const body = box.querySelector('.rich-media-body');
        if (body) {
          body.className = 'rich-media-body';
          body.style.height = '';
          body.innerHTML = '<div class="rich-media-loading">渲染中…</div>';
        }
        const srcBtn = box.querySelector('[data-rich-source]');
        if (srcBtn) srcBtn.textContent = '源码';
        box.classList.remove('show-src');
      });
    setTimeout(() => {
      theming = false;
      enhance(document);
    }, 80);
  }

  window.enhanceRichMedia = enhance;
  window.disposeRichMedia = dispose;

  try {
    new MutationObserver(reTheme).observe(document.documentElement, {
      attributes: true, attributeFilter: ['data-theme'],
    });
  } catch (e) { /* 浏览器不支持就算了 */ }
})();
