// 只把「窗口控制」这几件事暴露给页面；页面拿不到 Node 能力。
const { contextBridge, ipcRenderer, webUtils } = require('electron');

contextBridge.exposeInMainWorld('gmWin', {
  minimize: () => ipcRenderer.send('win:minimize'),
  toggleMaximize: () => ipcRenderer.send('win:toggle-maximize'),
  close: () => ipcRenderer.send('win:close'),
  focus: () => ipcRenderer.send('win:focus'),
  // 阅读器沉浸模式：真正进入系统全屏（隐藏任务栏），并回推状态
  setFullScreen: (v) => ipcRenderer.send('win:set-fullscreen', !!v),
  onFullScreen: (fn) => {
    if (typeof fn !== 'function') return;
    ipcRenderer.on('win:fullscreen', (_e, v) => {
      try { fn(!!v); } catch (err) { /* 页面异常不影响壳 */ }
    });
  },
  // 系统通知走主进程创建：点击事件可靠，且能在点击时还原 / 聚焦窗口
  notify: (payload) => ipcRenderer.send('notify:show', payload || {}),
  onNotifyClick: (fn) => {
    if (typeof fn !== 'function') return;
    ipcRenderer.on('notify:click', (_e, payload) => {
      try { fn(payload || {}); } catch (err) { /* 页面异常不影响壳 */ }
    });
  },
  onMaximized: (fn) => {
    if (typeof fn !== 'function') return;
    ipcRenderer.on('win:maximized', (_e, v) => {
      try { fn(!!v); } catch (err) { /* 页面自己的异常不影响壳 */ }
    });
  },
  // 从文件树拖出真实文件：必须在 dragstart 里同步调用（页面先 preventDefault 再发这条 IPC）
  startDrag: (payload) => ipcRenderer.send('file:start-drag', payload || {}),
  // drop 事件里 Files 对象拿不到磁盘路径；用 webUtils 换成绝对路径（主进程不参与）
  pathForFile: (file) => {
    try { return webUtils.getPathForFile(file) || ''; } catch (err) { return ''; }
  },
  // 导出文件：主进程弹保存框并直接写盘（不把大文件先读进渲染进程内存）
  saveUrl: (payload) => ipcRenderer.invoke('file:save-url', payload || {}),
  onSaveProgress: (fn) => {
    if (typeof fn !== 'function') return () => {};
    const h = (_e, p) => {
      try { fn(p || {}); } catch (err) { /* 页面自己的异常不影响壳 */ }
    };
    ipcRenderer.on('file:save-progress', h);
    return () => ipcRenderer.removeListener('file:save-progress', h);
  },
});
