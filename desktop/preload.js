// 只把「窗口控制」这几件事暴露给页面；页面拿不到 Node 能力。
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('gmWin', {
  minimize: () => ipcRenderer.send('win:minimize'),
  toggleMaximize: () => ipcRenderer.send('win:toggle-maximize'),
  close: () => ipcRenderer.send('win:close'),
  onMaximized: (fn) => {
    if (typeof fn !== 'function') return;
    ipcRenderer.on('win:maximized', (_e, v) => {
      try { fn(!!v); } catch (err) { /* 页面自己的异常不影响壳 */ }
    });
  },
});
