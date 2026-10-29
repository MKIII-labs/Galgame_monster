// Galgame_monster 的 Electron 窗口壳。
//
// 它只做一件事：给现有的 Web 界面套一个「没有系统标题栏」的原生窗口。
// 顶栏（应用名 + 最小化 / 最大化 / 关闭）由网页里的 #titlebar 自绘，
// 通过 preload 暴露的 window.gmWin 调用这里的 IPC。
// 业务逻辑、页面、后端服务都不在这里改动。
const { app, BrowserWindow, Menu, dialog, ipcMain, session, shell } = require('electron');
const fs = require('fs');
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
ipcMain.on('win:toggle-maximize', () => {
  if (!win) return;
  if (win.isMaximized()) win.unmaximize();
  else win.maximize();
});
ipcMain.on('win:close', () => { dlog('ipc win:close'); if (win) win.close(); });
