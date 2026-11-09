// Galgame_monster 的 Electron 窗口壳。
//
// 它只做一件事：给现有的 Web 界面套一个「没有系统标题栏」的原生窗口。
// 顶栏（应用名 + 最小化 / 最大化 / 关闭）由网页里的 #titlebar 自绘，
// 通过 preload 暴露的 window.gmWin 调用这里的 IPC。
// 业务逻辑、页面、后端服务都不在这里改动。
const { app, BrowserWindow, Menu, Notification, dialog, ipcMain, session, shell } = require('electron');
const fs = require('fs');
const http = require('http');
const https = require('https');
const path = require('path');

const ROOT = path.join(__dirname, '..');
// 窗口 / 任务栏图标：直接用项目根目录的 app.ico（内含 16/32/48/64/128/256 多尺寸帧，
// 系统会挑最合适的一帧，不会被二次缩放到模糊）
const ICON = path.join(ROOT, 'app.ico');

// 用户数据放在项目自己的 data/ 下（和 --app 时代一致，便于整体清理 / 备份）
const PROFILE_DIR = path.join(ROOT, 'data', '.electron_profile');
try {
  fs.mkdirSync(PROFILE_DIR, { recursive: true });
} catch (e) { /* 目录建不出来就让 Electron 用默认路径 */ }
app.setPath('userData', PROFILE_DIR);

// 退出诊断：窗口「自己消失」时只有这份日志能说明原因
// （渲染进程崩溃 / 页面按钮触发 / 系统关闭 / 第二个实例抢锁）。
const DLOG = path.join(ROOT, 'data', 'desktop.log');
function dlog(msg) {
  try {
    fs.appendFileSync(DLOG, '[' + new Date().toISOString() + '] ' + msg + '\n');
  } catch (e) { /* 日志失败不影响窗口 */ }
}

// 任务栏图标（Windows 10/11）不走 BrowserWindow.icon，而是按这个 AppUserModelID
// 去找「同 ID 的快捷方式」并取它的图标。所以这个字符串必须和快捷方式上的
// System.AppUserModel.ID 一致：main.py 会读出来（已固定到任务栏时用资源管理器
// 自动生成的那个 ID）并通过环境变量传进来，这里只留一个兜底值。
app.setAppUserModelId(process.env.GM_AUMID || 'GalgameMonster.Desktop');

// 启动参数里带界面的地址（main.py 传进来）
const APP_URL = (process.argv.find((a) => /^https?:\/\//i.test(a)) || '').trim();

let win = null;

// 同一个窗口壳只允许一个实例：第二次启动只把已有窗口提到前面
if (!app.requestSingleInstanceLock()) {
  dlog('single-instance lock busy; quitting');
  app.quit();
} else {
  app.on('second-instance', () => {
    dlog('second-instance');
    if (!win) return;
    if (win.isMinimized()) win.restore();
    win.show();
    win.focus();
  });
  app.on('window-all-closed', () => { dlog('window-all-closed'); app.quit(); });
  app.on('before-quit', () => dlog('before-quit'));
  app.on('child-process-gone', (_e, d) => {
    dlog('child-process-gone ' + JSON.stringify(d || {}));
  });

  app.whenReady().then(() => {
    if (!APP_URL) {
      dialog.showErrorBox('Galgame_monster', '没有拿到界面地址，无法启动窗口。');
      app.quit();
      return;
    }
    Menu.setApplicationMenu(null);
    setupPermissions();
    createWindow();
  });
}

/** 只放行本应用真正用到的权限：麦克风（语音输入）、通知、剪贴板 */
function setupPermissions() {
  const ALLOW = new Set(['media', 'notifications', 'clipboard-read',
                         'clipboard-sanitized-write', 'fullscreen']);
  const ses = session.defaultSession;
  ses.setPermissionRequestHandler((_wc, permission, cb) => cb(ALLOW.has(permission)));
  ses.setPermissionCheckHandler((_wc, permission) => ALLOW.has(permission));

  // 导出文件（.md / .json / .png）时弹保存框，和浏览器里的体验一致
  ses.on('will-download', (_e, item) => {
    try {
      item.setSaveDialogOptions({
        title: '保存文件',
        defaultPath: path.join(app.getPath('downloads'), item.getFilename()),
      });
    } catch (e) { /* 打不开保存框就让 Electron 走默认路径 */ }
  });
}

function createWindow() {
  win = new BrowserWindow({
    width: 1280,
    height: 860,
    minWidth: 960,
    minHeight: 600,
    icon: ICON,
    frame: false,              // 无系统标题栏：由页面里的 #titlebar 顶上
    show: false,               // ready-to-show 再显示，避免白屏闪一下
    backgroundColor: '#ffffff',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      // 被遮挡 / 最小化时别把定时器降频，否则流式输出看起来会「停住」
      // （等价于原本 Edge 的 --disable-background-timer-throttling 那一组参数）
      backgroundThrottling: false,
      spellcheck: false,
    },
  });

  win.loadURL(APP_URL);
  win.once('ready-to-show', () => win.show());
  win.on('close', () => dlog('window close'));
  win.on('closed', () => { dlog('window closed'); win = null; });
  win.webContents.on('render-process-gone', (_e, d) => {
    dlog('render-process-gone ' + JSON.stringify(d || {}));
  });
  win.on('unresponsive', () => dlog('window unresponsive'));
  win.on('responsive', () => dlog('window responsive'));

  // 顶栏图标要跟着最大化状态切换（最大化 → 显示「还原」）
  const pushMax = (v) => {
    if (win) win.webContents.send('win:maximized', !!v);
  };
  win.on('maximize', () => pushMax(true));
  win.on('unmaximize', () => pushMax(false));

  // 系统全屏状态回推：用户按系统快捷键 / 阅读器点 ⤢ 都走这里同步 UI
  const pushFull = (v) => {
    if (win) win.webContents.send('win:fullscreen', !!v);
  };
  win.on('enter-full-screen', () => pushFull(true));
  win.on('leave-full-screen', () => pushFull(false));

  // 外链交给系统浏览器；页面内 window.open 的 blob:/data:（图片预览）仍开在应用里
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:/i.test(url)) {
      shell.openExternal(url);
      return { action: 'deny' };
    }
    return {
      action: 'allow',
      overrideBrowserWindowOptions: {
        width: 960, height: 720,
        icon: ICON,
        autoHideMenuBar: true,
        webPreferences: { contextIsolation: true, nodeIntegration: false },
      },
    };
  });
  win.webContents.on('will-navigate', (e, url) => {
    if (url.startsWith(APP_URL)) return;
    e.preventDefault();
    // 只把普通网页交给系统浏览器；file: / ms-settings: 等其它 scheme 一律不转交，
    // 避免模型输出里的链接被点一下就触发系统级动作。
    if (/^(https?:|mailto:)/i.test(url)) shell.openExternal(url);
  });

  bindKeys(win.webContents);
}

/** 刷新 / 开发者工具 / 缩放：浏览器里用惯的快捷键别丢 */
function bindKeys(wc) {
  const setZoom = (z) => {
    const v = Math.max(-3, Math.min(4, z));
    wc.setZoomLevel(v);
  };
  wc.on('before-input-event', (event, input) => {
    if (input.type !== 'keyDown') return;
    const k = String(input.key || '').toLowerCase();
    const ctrl = input.control || input.meta;
    if (k === 'f5' || (ctrl && k === 'r')) {
      event.preventDefault();
      wc.reloadIgnoringCache();
    } else if (k === 'f12' || (ctrl && input.shift && k === 'i')) {
      event.preventDefault();
      wc.toggleDevTools();
    } else if (ctrl && (k === '=' || k === '+')) {
      event.preventDefault();
      setZoom(wc.getZoomLevel() + 0.5);
    } else if (ctrl && k === '-') {
      event.preventDefault();
      setZoom(wc.getZoomLevel() - 0.5);
    } else if (ctrl && k === '0') {
      event.preventDefault();
      setZoom(0);
    }
  });
  // Ctrl + 滚轮缩放
  wc.on('zoom-changed', (_e, dir) => {
    setZoom(wc.getZoomLevel() + (dir === 'in' ? 0.5 : -0.5));
  });
}

// 顶栏按钮（页面 → 壳）
ipcMain.on('win:minimize', () => { if (win) win.minimize(); });
ipcMain.on('win:set-fullscreen', (_e, v) => {
  try {
    if (win) win.setFullScreen(!!v);
  } catch (e) {
    dlog('fullscreen failed: ' + (e && e.message));
  }
});
ipcMain.on('win:toggle-maximize', () => {
  if (!win) return;
  if (win.isMaximized()) win.unmaximize();
  else win.maximize();
});
ipcMain.on('win:close', () => { dlog('ipc win:close'); if (win) win.close(); });

/** 把窗口提到前台：最小化就还原、隐藏就显示，再聚焦 */
function focusWindow() {
  if (!win) return;
  try {
    if (win.isMinimized()) win.restore();
    if (!win.isVisible()) win.show();
    win.focus();
  } catch (e) { /* 窗口已销毁时忽略 */ }
}

ipcMain.on('win:focus', () => focusWindow());

// 系统通知由主进程创建：Windows 上渲染进程的 HTML5 Notification 点击事件
// 在部分版本里收不到，点完就等于没反应；主进程的 Notification 有稳定的
// 'click' 事件，能先还原窗口，再通知页面切到对应会话 / 任务。
const liveNotifs = new Set();
ipcMain.on('notify:show', (_e, payload) => {
  const p = payload || {};
  try {
    if (!Notification.isSupported()) return;
    const n = new Notification({
      title: String(p.title || 'Galgame_monster'),
      body: String(p.body || ''),
      // 提示音由渲染进程按类型（完成 / 待回答 / 提醒）现场合成播放；
      // 这里必须静音，否则 Windows 会再加一声系统通知音，听起来就是两声。
      silent: true,
    });
    liveNotifs.add(n);
    n.on('close', () => liveNotifs.delete(n));
    let fired = false;                 // 同一条通知只处理一次 click（避免重复 IPC）
    n.on('click', () => {
      if (fired) return;
      fired = true;
      dlog('notify click ' + String(p.id || ''));
      focusWindow();
      try { if (win) win.webContents.send('notify:click', { id: p.id || '', target: p.target || null }); } catch (e) { /* 页面不在也先把窗口拉起来 */ }
      liveNotifs.delete(n);
    });
    dlog('notify show ' + String(p.id || '') + ' target=' +
         JSON.stringify(p.target || null));   // 用来核对 show / click 是否成对
    n.show();
  } catch (e) {
    dlog('notify failed: ' + (e && e.message));
  }
});

// 从 IDE 文件树拖出真实文件 / 目录到系统（桌面 / 资源管理器 / 其他应用）。
// 页面在 dragstart 里先 preventDefault，再发这条 IPC：此时系统级拖拽会话还没结束，
// 主进程调用 webContents.startDrag 把「磁盘上的这个条目」交给系统，
// 目标应用拿到的就是文件本体（Windows 上是 CF_HDROP），而不是一串路径文字。
ipcMain.on('file:start-drag', (_e, payload) => {
  const p = payload || {};
  const target = String(p.file || '');
  if (!win || !target) return;
  let st = null;
  try { st = fs.statSync(target); } catch (e) {
    dlog('start-drag skipped (missing): ' + target);
    return;
  }
  try {
    // icon 在 Windows 上必须是 .ico；app.ico 是多尺寸帧，系统会挑合适的一帧
    win.webContents.startDrag({ file: target, icon: ICON });
    dlog('start-drag ' + (st.isDirectory() ? 'dir ' : 'file ') + target);
  } catch (e) {
    dlog('start-drag failed: ' + (e && e.message));
  }
});

// 导出文件：主进程弹保存框，并把 HTTP 响应直接流式写盘。
// 之前是页面 fetch 成 blob 再 <a download>：500MB 的备份要在渲染进程内存里多存
// 一份，打包 + 传输期间页面没有任何反馈，看起来就像「点了没反应」。
// 地址只允许指向本应用（APP_URL 同源），避免页面把导出请求引到外部。
ipcMain.handle('file:save-url', async (_e, payload) => {
  const p = payload || {};
  const url = String(p.url || '');
  let target = null;
  try {
    target = new URL(url);
  } catch (e) {
    return { ok: false, error: '地址不合法' };
  }
  let base = '';
  try { base = APP_URL ? new URL(APP_URL).origin : ''; } catch (e) { base = ''; }
  if (!/^https?:$/i.test(target.protocol) || (base && target.origin !== base)) {
    return { ok: false, error: '不允许导出到外部地址' };
  }
  const opts = {
    title: String(p.title || '保存文件'),
    defaultPath: path.join(app.getPath('downloads'), String(p.name || 'download.bin')),
  };
  let picked;
  try {
    picked = win ? await dialog.showSaveDialog(win, opts) : await dialog.showSaveDialog(opts);
  } catch (e) {
    return { ok: false, error: (e && e.message) || '无法打开保存框' };
  }
  if (!picked || picked.canceled || !picked.filePath) return { ok: false, canceled: true };

  return await new Promise((resolve) => {
    let finished = false;
    const done = (v) => { if (!finished) { finished = true; resolve(v); } };
    const mod = target.protocol === 'https:' ? https : http;
    try {
      const req = mod.request(target, {
        method: String(p.method || 'POST').toUpperCase(),
        headers: (p.headers && typeof p.headers === 'object') ? p.headers : {},
      }, (res) => {
        if (res.statusCode < 200 || res.statusCode >= 300) {
          let text = '';
          res.on('data', (c) => { if (text.length < 400) text += c.toString('utf8'); });
          res.on('end', () => done({
            ok: false, error: 'HTTP ' + res.statusCode + (text ? '：' + text : ''),
          }));
          return;
        }
        const total = Number(res.headers['content-length']) || 0;
        const out = fs.createWriteStream(picked.filePath);
        let bytes = 0;
        let lastSent = 0;
        res.on('data', (c) => {
          bytes += c.length;
          const now = Date.now();
          if (now - lastSent > 250) {          // 大文件别把 IPC 刷爆
            lastSent = now;
            try {
              if (win) win.webContents.send('file:save-progress', { bytes, total });
            } catch (e) { /* 窗口已关闭 */ }
          }
        });
        res.on('error', (err) => {
          try { out.destroy(); } catch (e) { /* ignore */ }
          done({ ok: false, error: err.message });
        });
        out.on('error', (err) => {
          try { req.destroy(); } catch (e) { /* ignore */ }
          done({ ok: false, error: err.message });
        });
        out.on('finish', () => out.close(() => done({ ok: true, path: picked.filePath, bytes })));
        res.pipe(out);
      });
      req.on('error', (err) => done({ ok: false, error: err.message }));
      req.end();
    } catch (e) {
      done({ ok: false, error: (e && e.message) || '导出失败' });
    }
  });
});

// 阅读器「译文 PDF」已改为服务端重排（app/reader_export.py），通过 file:save-url
// 直接下载矢量 PDF；这里不再需要「HTML → printToPDF」的隐藏窗口通路。
