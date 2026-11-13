/* ============================================================
   按需加载：大厅模块 / 工作台 / 插件市场
   ------------------------------------------------------------
   这三组脚本只在首次进入对应工作区时才下载与执行，避免首屏
   解析约 800 KB 的业务代码。分组内的加载顺序固定，且只加载一次。
   ============================================================ */
'use strict';

window.gmLoadGroup = (function () {
  const GROUPS = {
    hall: [
      '/static/hall-geo.js',
      '/static/hall-stock.js',
      '/static/hall-math.js',
      '/static/hall-math-archive.js',
      '/static/hall-math-exam.js',
      '/static/hall-dojo.js',
      '/static/hall-observe.js',
      '/static/hall-interview.js',
    ],
    work: [
      '/static/work/core.js',
      '/static/work/events.js',
      '/static/work/tasks.js',
      '/static/work/artifacts.js',
      '/static/work/workflows.js',
      '/static/work/backup.js',
      '/static/work/views.js',
      '/static/work/stats.js',
      '/static/work/focus.js',
      '/static/work/templates.js',
      '/static/work/misc.js',
    ],
    plugins: ['/static/hall-plugins.js'],
    xterm: [
      '/static/vendor/xterm/xterm.js',
      '/static/vendor/xterm/addon-fit.js',
    ],
    reader: [
      '/static/transpage.js',
      '/static/reader.js',
    ],
  };

  const cache = {};

  function loadOne(src) {
    return new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.src = src;
      s.async = false;                 // 动态插入时保持组内顺序
      s.onload = () => resolve();
      s.onerror = () => reject(new Error('脚本加载失败：' + src));
      document.body.appendChild(s);
    });
  }

  return function loadGroup(kind) {
    if (cache[kind]) return cache[kind];
    const list = GROUPS[kind] || [];
    // 并发下载、按注册顺序执行（async=false 保证组内顺序），避免一串串行往返
    cache[kind] = Promise.all(list.map(loadOne)).then(() => undefined);
    return cache[kind];
  };
})();
