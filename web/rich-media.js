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
  const IMG_CACHE_MAX = 24;          // 只缓存最近查看的图片，避免 objectURL 常驻

  function rememberImgUrl(key, url) {
    const old = imgCache.get(key);
    if (old) {
      imgCache.delete(key);
      try { URL.revokeObjectURL(old); } catch (e) { /* 忽略 */ }
    }
    imgCache.set(key, url);
    while (imgCache.size > IMG_CACHE_MAX) {
      const k = imgCache.keys().next().value;
      const u = imgCache.get(k);
      imgCache.delete(k);
      try { URL.revokeObjectURL(u); } catch (e) { /* 忽略 */ }
    }
  }
  const imgBusy = new Map();         // "sid|path" -> Promise<url>
  const libLoading = new Set();
  let theming = false;

  /* 图表 / 流程图跟应用主题走：直接从 tokens.css 的语义变量取值，
     这样明暗切换后不用再维护第二份配色。 */
  function cssVar(name, fallback) {
    try {
      const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
      return v || fallback;
    } catch (e) { return fallback; }
  }

  function themedAxis() {
    const line = cssVar('--border', '#e1e4e9');
    const dim = cssVar('--text-dim', '#5b6169');
    return {
      axisLine: { lineStyle: { color: line } },
      axisTick: { lineStyle: { color: line } },
      axisLabel: { color: dim },
      splitLine: { lineStyle: { color: cssVar('--border-subtle', '#eff1f4') } },
      nameTextStyle: { color: dim },
    };
  }

  function chartTheme() {
    return {
      color: [
        cssVar('--cat-1', '#6b8afd'), cssVar('--cat-4', '#14b8a6'),
        cssVar('--cat-2', '#8b5cf6'), cssVar('--cat-6', '#0ea5e9'),
      ],
      backgroundColor: 'transparent',
      textStyle: { color: cssVar('--text', '#1b1e23'),
                   fontFamily: cssVar('--font', 'system-ui') },
      title: { textStyle: { color: cssVar('--text', '#1b1e23') },
               subtextStyle: { color: cssVar('--text-dim', '#5b6169') } },
      legend: { textStyle: { color: cssVar('--text-dim', '#5b6169') } },
      categoryAxis: themedAxis(),
      valueAxis: themedAxis(),
      tooltip: {
        backgroundColor: cssVar('--bg-elevated', '#ffffff'),
        borderColor: cssVar('--border', '#e1e4e9'),
        textStyle: { color: cssVar('--text', '#1b1e23') },
      },
    };
  }

  function mermaidVars() {
    const text = cssVar('--text', '#1b1e23');
    return {
      fontFamily: cssVar('--font', 'system-ui'),
      fontSize: '13px',
      background: cssVar('--bg-elevated', '#ffffff'),
      mainBkg: cssVar('--bg-elevated', '#ffffff'),
      primaryColor: cssVar('--accent-soft', 'rgba(77, 107, 254, .12)'),
      primaryTextColor: text,
      primaryBorderColor: cssVar('--accent', '#4d6bfe'),
      secondaryColor: cssVar('--bg-hover', '#eef0f3'),
      tertiaryColor: cssVar('--bg-input', '#f7f8fa'),
      lineColor: cssVar('--border-strong', '#c3c8cf'),
      textColor: text,
      nodeTextColor: text,
      clusterBkg: cssVar('--bg-side', '#f6f7f9'),
      clusterBorder: cssVar('--border', '#e1e4e9'),
      edgeLabelBackground: cssVar('--bg-elevated', '#ffffff'),
      titleColor: text,
    };
  }

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
        window.echarts.registerTheme('gm', chartTheme());   // 每次重画都取当前主题变量
        chart = window.echarts.init(body, 'gm', { renderer: 'canvas' });
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
      // 图表从 90px 占位撑到 340px：若仍贴底，补一次对齐，别停在对话中间
      if (box.closest('#chat') && typeof window.settleChatBottom === 'function') {
        window.settleChatBottom([0, 160, 500]);
      }
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
          // base 主题 + 应用设计令牌：明暗切换后重画就是同一套配色
          theme: 'base',
          themeVariables: mermaidVars(),
        });
      } catch (e) { /* 重复 initialize 不影响渲染 */ }
      const id = 'gm-mmd-' + Math.random().toString(36).slice(2);
      Promise.resolve()
        .then(() => m.render(id, code))
        .then((out) => {
          const svg = (out && out.svg) || '';
          if (!svg) throw new Error('没有生成 SVG');
          body.innerHTML = svg;
          // SVG 高度和 90px 占位不一致：仍贴底时补一次对齐
          if (box.closest('#chat') && typeof window.settleChatBottom === 'function') {
            window.settleChatBottom([0, 160, 500]);
          }
          const dl = box.querySelector('[data-rich-download]');
          if (dl) dl.onclick = () => downloadText(svg, 'diagram.svg', 'image/svg+xml');
        })
        .catch((e) => fallback(body, '流程图渲染失败：' + (e && e.message ? e.message : e), code));
    });
  }

  // ---------------- 交互页面（html 围栏） ----------------
  /* 对齐 Trae 的交互可视化：把一段完整 HTML 放进沙箱 iframe 真跑起来。
     sandbox 只给 allow-scripts、不给 allow-same-origin —— 页面是独立不透明源，
     读不到本应用的同源存储 / Cookie / 父窗口 DOM，脚本再怎么写也越不出去。
     高度由页面自己 postMessage 上报，随内容自适应。 */
  const htmlFrames = new Map();      // id -> iframe

  function installHtmlHeightListener() {
    if (window.__gmRichHtmlBound) return;
    window.__gmRichHtmlBound = true;
    window.addEventListener('message', (e) => {
      const d = e && e.data;
      if (!d || typeof d !== 'object' || !d.__gmRichHtml) return;
      const frame = htmlFrames.get(String(d.__gmRichHtml));
      if (!frame || !frame.isConnected) return;
      const h = Number(d.h) || 0;
      if (h > 0) frame.style.height = Math.max(120, Math.min(1600, Math.round(h))) + 'px';
    });
  }

  function renderHtml(box, body, code) {
    const raw = String(code || '').trim();
    if (!raw) { fallback(body, '这段 html 是空的', code); return; }
    installHtmlHeightListener();
    const id = 'gmh-' + Math.random().toString(36).slice(2);
    const probe = '<scr' + 'ipt>(function(){' +
      'function r(){try{var d=document.documentElement,b=document.body;' +
      'var h=Math.max(d?d.scrollHeight:0,b?b.scrollHeight:0);' +
      "parent.postMessage({__gmRichHtml:'" + id + "',h:h},'*');}catch(e){}}" +
      "window.addEventListener('load',r);document.addEventListener('click',r);" +
      "document.addEventListener('input',r);setTimeout(r,120);setInterval(r,1000);" +
      '})();</scr' + 'ipt>';
    const frame = document.createElement('iframe');
    frame.className = 'rich-html-frame';
    frame.setAttribute('sandbox', 'allow-scripts allow-popups allow-forms allow-modals');
    frame.setAttribute('referrerpolicy', 'no-referrer');
    frame.setAttribute('title', 'AI 生成的交互页面');
    frame.srcdoc = raw + probe;
    htmlFrames.set(id, frame);
    body.className = 'rich-media-body rich-html';
    body.style.height = '';
    body.innerHTML = '';
    body.appendChild(frame);
    if (box.closest('#chat') && typeof window.settleChatBottom === 'function') {
      window.settleChatBottom([0, 200, 600]);
    }
    const dl = box.querySelector('[data-rich-download]');
    if (dl) dl.onclick = () => downloadText(raw, 'page.html', 'text/html;charset=utf-8');
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
      rememberImgUrl(key, url);
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
  /* 视口增强：只处理真正会看到的节点。
     - 代码块同步高亮十几个会产生上万 span，首屏 Layout 与切会话都被它拖慢；
     - mermaid / echarts 整窗渲染会让首屏外的消息也去拉 3MB 库（实测启动时白占 145ms）。
     两者都等进入视口再做：代码高亮只改颜色、不改行数，不会引起位移；
     富媒体提前 400px 渲染，滚到眼前时已经就绪。 */
  let codeOb = null;
  let richOb = null;
  const hlPending = new Set();
  let hlTimer = 0;
  let hlGiveUp = 0;

  function hlRender(el) {
    if (!el.isConnected || el.dataset.hl !== 'pending') return;
    const code = el.textContent;
    const lang = el.getAttribute('data-lang') || '';
    let html = '';
    try {
      html = (lang && window.hljs.getLanguage(lang))
        ? window.hljs.highlight(code, { language: lang }).value
        : window.hljs.highlightAuto(code).value;
    } catch (e) { html = ''; }
    if (!html) { el.dataset.hl = 'plain'; return; }      // 高亮失败：保留纯文本
    el.innerHTML = html;
    el.dataset.hl = 'done';
  }

  function hlPump() {
    hlTimer = 0;
    if (!window.hljs) {
      if (Date.now() > hlGiveUp) return;                 // 库一直没到：保持纯文本
      hlTimer = setTimeout(hlPump, 300);
      return;
    }
    const list = Array.from(hlPending);
    hlPending.clear();
    list.forEach(hlRender);
  }

  function hlQueue(el) {
    if (!el.isConnected || el.dataset.hl !== 'pending') return;
    if (window.hljs) { hlRender(el); return; }
    if (!hlPending.size) hlGiveUp = Date.now() + 20000;
    hlPending.add(el);
    if (!hlTimer) hlTimer = setTimeout(hlPump, 200);
  }

  function hlObserve(list) {
    if (!list.length) return;
    if (!window.IntersectionObserver) { list.forEach(hlQueue); return; }
    if (!codeOb) {
      codeOb = new IntersectionObserver((entries) => {
        entries.forEach((en) => {
          if (!en.isIntersecting) return;
          codeOb.unobserve(en.target);
          hlQueue(en.target);
        });
      }, { rootMargin: '300px 0px' });
    }
    list.forEach((el) => codeOb.observe(el));
  }

  function renderRichBox(box) {
    if (!box || box.getAttribute('data-rich-done')) return;
    box.setAttribute('data-rich-done', '1');
    const kind = box.getAttribute('data-rich');
    const body = box.querySelector('.rich-media-body');
    const srcEl = box.querySelector('.rich-media-src');
    const code = srcEl ? srcEl.textContent : '';
    if (!body) return;
    if (kind === 'svg') renderSvg(box, body, code);
    else if (kind === 'chart') renderChart(box, body, code);
    else if (kind === 'mermaid') renderMermaid(box, body, code);
    else if (kind === 'html') renderHtml(box, body, code);
  }

  function richObserve(list, immediate) {
    if (!list.length) return;
    if (immediate || !window.IntersectionObserver) { list.forEach(renderRichBox); return; }
    if (!richOb) {
      richOb = new IntersectionObserver((entries) => {
        entries.forEach((en) => {
          if (!en.isIntersecting) return;
          richOb.unobserve(en.target);
          renderRichBox(en.target);
        });
      }, { rootMargin: '400px 0px' });
    }
    list.forEach((box) => richOb.observe(box));
  }

  function enhance(root, immediate) {
    const scope = root || document;
    fillSandboxImages(scope);
    hlObserve(scope.querySelectorAll('code.hljs[data-hl="pending"]'));
    richObserve(scope.querySelectorAll('.rich-media[data-rich]:not([data-rich-done])'), immediate);
    scope.querySelectorAll('.rich-media [data-rich-source]').forEach((btn) => {
      if (btn.dataset.bound) return;
      btn.dataset.bound = '1';
      btn.onclick = () => {
        const box = btn.closest('.rich-media');
        if (!box) return;
        const on = box.classList.toggle('show-src');
        // 源码块初始带 .hidden；全局 .hidden { display:none !important } 会压过
        // .show-src 的 display:block，所以必须同步摘掉这个类，否则按钮看起来失灵。
        const src = box.querySelector('.rich-media-src');
        if (src) src.classList.toggle('hidden', !on);
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
        const srcEl = box.querySelector('.rich-media-src');
        if (srcEl) srcEl.classList.add('hidden');
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
