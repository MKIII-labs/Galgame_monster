# -*- coding: utf-8 -*-
"""
Galgame_monster —— 启动器
在本地启动后端服务，并用 Edge/Chrome 的「应用模式」窗口打开界面
（应用模式窗口没有地址栏和标签页，看起来就是一个独立桌面程序）。

用法： python main.py
退出： 关闭应用窗口即可；或在控制台按 Ctrl+C
"""
import faulthandler
import json
import os
import socket
import subprocess
import sys
import threading
import time
import traceback
import webbrowser

from server import create_server

ROOT = os.path.dirname(os.path.abspath(__file__))
PROFILE_DIR = os.path.join(ROOT, "data", ".browser_profile")
LOG_FILE = os.path.join(ROOT, "data", "app.log")
STATE_FILE = os.path.join(ROOT, "data", "server.json")
PORT_FILE = os.path.join(ROOT, "data", "app_port.txt")
LOG_MAX_BYTES = 8 * 1024 * 1024        # 启动时超过这个大小就把旧日志挪成 app.log.1
_LOG_FH = None
_OUT_FH = None
_CODE_FILES = ("main.py", "app/httpd.py", "app/api/chat.py", "app/llm.py",
               "app/bilibili.py", "app/agent.py", "app/stock.py", "app/config.py",
               "app/api/hall_stock.py", "web/hall.js", "web/hall-stock.js",
               "web/markdown.js", "web/chat.js", "web/stream.js")
_APP_AUMID = ""                        # 传给 Electron 壳的 AppUserModelID（任务栏图标靠它）

WINDOW_W, WINDOW_H = 1280, 860


def _log(msg):
    """最小启动/退出日志：pythonw 没有控制台，出问题时只能靠它。"""
    global _LOG_FH
    try:
        if _LOG_FH is None:
            os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
            _LOG_FH = open(LOG_FILE, "a", encoding="utf-8")
        _LOG_FH.write(time.strftime("[%Y-%m-%d %H:%M:%S] ") + str(msg) + "\n")
        _LOG_FH.flush()
    except Exception:
        pass


try:
    os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
    faulthandler.enable(open(LOG_FILE, "a", encoding="utf-8"))
except Exception:
    pass


def _pipe_output():
    """把 print / 未捕获异常的栈接到 app.log。

    只在「没有控制台」时接管（pythonw 下 sys.stdout 是 None）：所有诊断
    （[服务] xxx 出错、[压缩] 失败、[收尾] …）以前都静默丢掉，排障时等于瞎跑。
    用 `python main.py` 或在测试里导入本模块时不接管，输出行为不变。
    """
    global _OUT_FH
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
        try:
            if os.path.getsize(LOG_FILE) > LOG_MAX_BYTES:
                os.replace(LOG_FILE, LOG_FILE + ".1")   # 只在启动时轮转，运行中不搬
        except OSError:
            pass
        _OUT_FH = open(LOG_FILE, "a", encoding="utf-8", buffering=1)   # 按行 flush
        sys.stdout = _OUT_FH
        sys.stderr = _OUT_FH
        # 崩溃栈也要跟着走：上面若刚轮转过，faulthandler 还握着旧文件的句柄
        try:
            faulthandler.enable(_OUT_FH)
        except Exception:
            pass
    except Exception:
        pass


_pipe_output()


# ============================================================
# 单实例：避免两个服务共用同一个浏览器配置目录，互相顶掉
# ============================================================

def _code_stamp():
    """关键代码的最新修改时间，用来判断旧实例是不是在跑旧代码。"""
    latest = 0.0
    for rel in _CODE_FILES:
        try:
            latest = max(latest, os.path.getmtime(os.path.join(ROOT, rel)))
        except OSError:
            pass
    return latest


def _read_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


# ============================================================
# 固定端口：localStorage 按「协议+主机+端口」隔离，端口每次变会导致
# 题库筛选、段位、错题本等本地数据读不到。记录上次端口并优先复用。
# ============================================================

def _read_preferred_port():
    try:
        with open(PORT_FILE, "r", encoding="utf-8") as f:
            port = int((f.read() or "").strip())
        return port if 1024 <= port <= 65535 else 0
    except Exception:
        return 0


def _port_free(port):
    """能连上说明已有服务在监听；连接被拒/超时则视为可用（避开 TIME_WAIT 误判）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.3)
    try:
        s.connect(("127.0.0.1", port))
        return False
    except OSError:
        return True
    finally:
        s.close()


def _save_preferred_port(port):
    try:
        os.makedirs(os.path.dirname(PORT_FILE), exist_ok=True)
        with open(PORT_FILE, "w", encoding="utf-8") as f:
            f.write(str(int(port)))
    except Exception:
        pass


def _write_state(port):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "port": int(port),
                       "started": time.time(), "code_stamp": _code_stamp()},
                      f, ensure_ascii=False)
    except Exception as e:
        _log("write state failed: %s" % e)


def _clear_state(force=False):
    """只清理自己的状态文件；force=True 用于清理已死实例的残留。"""
    st = _read_state()
    if not force and st and int(st.get("pid") or 0) != os.getpid():
        return
    try:
        os.remove(STATE_FILE)
    except OSError:
        pass


def _pid_alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if h:
                ctypes.windll.kernel32.CloseHandle(h)
                return True
            return False
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _pid_is_python(pid):
    """Windows：确认该 PID 的镜像名是 python，避免 PID 被系统复用后误判成旧实例。"""
    if os.name != "nt":
        return True
    import ctypes
    from ctypes import wintypes
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return False
    try:
        buf = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buf))
        if not ctypes.windll.kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return False
        return os.path.basename(buf.value).lower() in ("python.exe", "pythonw.exe", "python3.exe")
    except Exception:
        return False
    finally:
        ctypes.windll.kernel32.CloseHandle(h)


def _port_alive(port):
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=1):
            return True
    except Exception:
        return False


def _kill_stale_tree(pid):
    """结束「进程还活着、但服务已不在监听」的旧实例（连同子进程）。

    以前这里只弹框让用户去任务管理器杀：venv 的 pythonw 会再拉起一个子 pythonw，
    杀掉父进程后子进程还在，于是「怎么关都关不掉、也起不来新实例」。
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return
    if pid <= 0 or pid == os.getpid() or not _pid_is_python(pid):
        return
    try:
        kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], timeout=10,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
    except Exception as e:                       # noqa: BLE001
        _log("kill stale %s failed: %s" % (pid, e))
    for _ in range(30):                          # 最多等 3 秒确认它消失
        if not _pid_alive(pid):
            break
        time.sleep(0.1)


def _window_reconnected(seconds):
    """最多等 seconds 秒；期间出现 SSE 客户端就认为窗口重新打开了。"""
    from app import httpd as _httpd
    end = time.monotonic() + max(0.0, float(seconds or 0))
    while True:
        if _httpd.active_clients() > 0:
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(0.2)


def _wait_clients_gone():
    """等到所有界面都断开（连续 1.5 秒没有 SSE 客户端）。"""
    from app import httpd as _httpd
    idle = 0
    while True:
        if _httpd.active_clients() <= 0:
            idle += 1
            if idle >= 3:
                return
        else:
            idle = 0
        time.sleep(0.5)


def _active_stream_count():
    """当前还有几个会话在生成（app.store.ACTIVE_STREAMS）。"""
    try:
        from app.store import ACTIVE_STREAMS
        return len(ACTIVE_STREAMS)
    except Exception:
        return 0


def _background_job_count():
    """还有几个后台长任务在跑：子代理批次 + 自动化目标。

    它们不在 ACTIVE_STREAMS 里（不是对话回合），但同样是用户交给程序的任务；
    关窗后如果只等对话流，回合一结束进程就退出，后台线程会被一起杀掉。
    任何一个模块读失败都按 0 计——保活逻辑不能反过来把进程卡死。
    """
    n = 0
    try:
        from app import explore
        n += int(explore.running_count() or 0)
    except Exception:
        pass
    try:
        from app import autopilot
        n += int(autopilot.running_count() or 0)
    except Exception:
        pass
    return n


def _pending_job_count():
    """对话回合 + 后台长任务的合计，退出判断统一用它。"""
    return _active_stream_count() + _background_job_count()


def _wait_streams_grace(minutes, keep_alive=False):
    """窗口退出后给正在生成的任务留一段保活时间。

    浏览器窗口没了不等于任务结束：断线期间任务仍在跑，跑完会正常落库。
    以前窗口一关就 shutdown，正在生成的回合被杀掉，下次启动只能从旧快照
    「继续任务」，表现为同一段内容反复重来（NOTE 43.168）。

    keep_alive=True（默认，对齐 dsh 的「任务与 UI 解耦」）时一直等到
    最后一个任务结束，不再用分钟数硬砍；False 时退回旧行为。
    """
    try:
        minutes = float(minutes or 0)
    except (TypeError, ValueError):
        minutes = 0
    if not keep_alive and minutes <= 0:
        return
    deadline = None if keep_alive else time.monotonic() + minutes * 60
    while True:
        n = _pending_job_count()
        if n <= 0:
            _log("grace: streams/background jobs finished")
            return
        if deadline is not None and time.monotonic() >= deadline:
            _log("grace: timeout with %d pending job(s)" % n)
            return
        time.sleep(2)


def _message_box(text):
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, text, "Galgame_monster", 0x40)
    except Exception:
        print(text)


def find_browser():
    """优先 Edge，其次 Chrome（都用 Chromium 的 --app 应用模式）"""
    candidates = [
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


def _electron_shell():
    """Electron 窗口壳是否就绪（desktop/node_modules 里的 electron + main.js）。

    壳负责「无系统标题栏 + 自绘顶栏」；没装依赖或不是 Windows 时返回 None，
    启动照旧走 Edge 的 --app 应用窗口。
    """
    if os.name != "nt":
        return None
    exe = os.path.join(ROOT, "desktop", "node_modules", "electron", "dist", "electron.exe")
    js = os.path.join(ROOT, "desktop", "main.js")
    if os.path.isfile(exe) and os.path.isfile(js):
        return exe, js
    return None


def open_app_window(url):
    """打开应用窗口；返回 (Popen, 是否用了 Electron 壳)。

    优先 Electron 壳（无系统标题栏）；壳启动失败再回退 Edge --app。
    两者都不行时返回 (None, False)，由调用方退回系统默认浏览器。
    """
    shell = _electron_shell()
    if shell:
        exe, js = shell
        try:
            env = dict(os.environ)
            if _APP_AUMID:
                env["GM_AUMID"] = _APP_AUMID     # 见 _ensure_app_shortcut
            return subprocess.Popen([exe, js, url], env=env), True
        except Exception as e:
            print(f"[启动] Electron 壳启动失败，改用 Edge 应用窗口：{e}")

    exe = find_browser()
    if not exe:
        return None, False
    try:
        os.makedirs(PROFILE_DIR, exist_ok=True)
    except Exception:
        pass
    args = [
        exe,
        "--app=" + url,
        "--window-size=%d,%d" % (WINDOW_W, WINDOW_H),
        "--user-data-dir=" + PROFILE_DIR,
        "--no-first-run",
        "--no-default-browser-check",
        # 应用窗口被遮挡/最小化时，别把渲染进程与定时器降频；
        # 否则流式输出看起来会“停住”，工作台轮询也会变慢。
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-features=CalculateNativeWinOcclusion,Translate",
    ]
    try:
        return subprocess.Popen(args), False
    except Exception as e:
        print(f"[启动] 应用窗口打开失败：{e}")
        return None, False


def _ensure_app_shortcut():
    """保持快捷方式与窗口的 AppUserModelID 一致；返回窗口该用的 AUMID。

    Windows 10/11 的任务栏按钮图标不认 Electron 的 BrowserWindow.icon，而是按
    AppUserModelID 去找「带同一个 System.AppUserModel.ID 的快捷方式」并取它的图标；
    找不到就回落到 electron.exe 自带的原子图标。详见 app/win_shortcut.py。

    已经「固定到任务栏」时，资源管理器会给那条快捷方式写一个 AutoGenerated 形式的 ID，
    窗口必须沿用同一个 ID 才能并进那个按钮，否则任务栏会多出一个按钮。
    """
    if os.name != "nt":
        return ""
    try:
        from app import win_shortcut
        py = sys.executable or ""
        if os.path.basename(py).lower() == "python.exe":
            cand = os.path.join(os.path.dirname(py), "pythonw.exe")
            if os.path.isfile(cand):
                py = cand

        aumid = ""
        for lnk in (win_shortcut.pinned_lnk(), win_shortcut.start_menu_lnk()):
            aumid = (win_shortcut.read(lnk) or {}).get("aumid") or ""
            if aumid:
                break

        ok, note = win_shortcut.ensure(
            win_shortcut.start_menu_lnk(),
            py,
            args='"%s"' % os.path.join(ROOT, "main.py"),
            icon=os.path.join(ROOT, "app.ico"),
            workdir=ROOT,
            aumid=aumid or win_shortcut.AUMID,
        )
        if not aumid:
            aumid = (win_shortcut.read(win_shortcut.start_menu_lnk()) or {}).get("aumid") or ""
        _log("start menu shortcut: %s (ok=%s); aumid=%s" % (note, ok, aumid))
        return aumid or win_shortcut.AUMID
    except Exception as e:
        _log("start menu shortcut failed: %s" % e)
        return ""


def _open_arg(argv):
    """解析右键菜单传入的 `--open <目录>`（也接受 `--open=<目录>`）。"""
    for i, a in enumerate(argv):
        if a == "--open" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--open="):
            return a.split("=", 1)[1]
    return ""


def _set_open_request(path):
    """登记「用 Galgame Monster 打开这个目录」；界面连上后由服务端推给它。"""
    if not path:
        return
    try:
        from app import open_request
        ok = open_request.set_pending(path)
        _log("pending open: %s (ok=%s)" % (path, ok))
    except Exception as e:                 # noqa: BLE001  投递失败不该阻断启动
        _log("pending open failed: %s" % e)


def main():
    global _APP_AUMID
    _log("start main.py; python=%s" % sys.executable)
    _APP_AUMID = _ensure_app_shortcut()
    open_path = _open_arg(sys.argv[1:])
    # 单实例：已有实例在跑就不再起第二个服务（否则两个 Edge 窗口会共用
    # 同一个 data/.browser_profile，互相顶掉，表现为输出中途终止）
    st = _read_state()
    old_pid = int(st.get("pid") or 0) if st else 0
    # 跳过自身 PID：Windows 会把刚释放的 PID 再分配给新进程，否则上一次运行残留的
    # 记录会指到「自己」，表现为误报「旧实例仍在运行，但端口没有响应」。
    if st and old_pid != os.getpid() and _pid_alive(old_pid) and _pid_is_python(old_pid):
        port = st.get("port")
        url = "http://127.0.0.1:%s/" % port
        if _port_alive(port):
            current, old = _code_stamp(), float(st.get("code_stamp") or 0)
            if old and current > old + 0.5:
                _log("code updated (%.0f > %.0f); ask user to close old instance"
                     % (current, old))
                _message_box("检测到代码已更新，但旧实例仍在运行。\n\n"
                             "请先关闭旧的应用窗口，再重新启动，"
                             "否则新功能不会生效。")
                return
            _log("already running pid=%s port=%s; opening existing window"
                 % (st.get("pid"), port))
            _set_open_request(open_path)
            open_app_window(url)
            return
        # 进程还在、但端口已经没人监听：典型是退出流程被残留线程卡住留下的僵尸。
        # 直接清掉进程树并继续启动，不再让用户去任务管理器里猜该杀哪一个。
        _log("existing pid=%s port=%s not responding; killing stale tree"
             % (st.get("pid"), port))
        _kill_stale_tree(old_pid)
        _clear_state(force=True)
    if st:
        _log("stale state pid=%s; removing" % st.get("pid"))
        _clear_state(force=True)

    for rel in ("app/api/chat.py", "app/llm.py", "app/bilibili.py", "app/agent.py"):
        p = os.path.join(ROOT, rel)
        try:
            _log("module %s mtime=%s size=%s"
                 % (rel, time.strftime("%Y-%m-%d %H:%M:%S",
                                       time.localtime(os.path.getmtime(p))),
                    os.path.getsize(p)))
        except OSError:
            pass
    preferred = _read_preferred_port()
    bind_port = preferred if preferred and _port_free(preferred) else 0
    _log("preferred port=%s bind=%s" % (preferred, bind_port))
    _set_open_request(open_path)
    httpd, port = create_server(port=bind_port)
    _save_preferred_port(port)
    _write_state(port)
    _log("server listening on 127.0.0.1:%d" % port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/"

    print("=" * 56)
    print("  Galgame_monster 已启动")
    print(f"  地址：{url}")
    print("  关闭应用窗口即可退出（或在控制台按 Ctrl+C）")
    print("=" * 56)

    proc, used_shell = open_app_window(url)
    _window_closed = False        # 只有「窗口自己退出了」才走保活宽限（Ctrl+C 立即退出）
    _log("app window pid=%s shell=%s" % (getattr(proc, "pid", None), used_shell))

    # Electron 壳的顶栏是网页自绘的，不需要 DWM 上色；
    # 只有回退到 Edge --app 时才尝试给系统标题栏配色。
    if proc is not None and not used_shell:
        try:
            from app import config as _cfg
            from app import winframe
            _theme = str((_cfg.load_config() or {}).get("theme") or "light")
            winframe.apply_async(_theme == "dark", "#1e1e1e" if _theme == "dark" else "#ffffff")
        except Exception as e:
            print(f"[启动] 标题栏配色跳过：{e}")

    if proc is None:
        # 没有找到 Chromium 系浏览器，退回系统默认浏览器
        webbrowser.open(url)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    else:
        # 稍等片刻：若浏览器进程立刻退出，说明它是把 URL 交给了已有实例，
        # 此时不能立即关服务，改为保持运行直到 Ctrl+C
        time.sleep(2.5)
        if proc.poll() is not None:
            print("[提示] 窗口已交由已有浏览器实例打开，服务保持运行；按 Ctrl+C 退出。")
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass
        else:
            try:
                proc.wait()
                _window_closed = True
            except KeyboardInterrupt:
                pass

    if _window_closed:
        try:
            from app.config import load_config
            _cfg = load_config()
            _grace = _cfg.get("chat_window_grace_min", 10)
            _keep = bool(_cfg.get("chat_window_keep_alive", True))
        except Exception:
            _grace, _keep = 10, True
        while True:
            _n = _pending_job_count()
            if _n > 0:
                _log("window closed with %d pending job(s) (streams+background); "
                     "keep_alive=%s grace=%s min" % (_n, _keep, _grace))
                _wait_streams_grace(_grace, keep_alive=_keep)
                continue          # 任务结束后再看一眼：这段时间可能已经重开了窗口
            # 没有任务：给 5 秒重连窗口（关窗后立刻重开 / 刷新都算）。
            # 以前这里直接 shutdown，于是「重开的新窗口指向一个刚停掉的服务」——
            # 界面停在「后台运行中」，看不到输出、也终止不了。
            if _window_reconnected(5.0):
                _log("client reconnected; keep serving")
                _wait_clients_gone()
                continue
            break

    _log("browser window exited (code=%s); shutting down"
         % (getattr(proc, "returncode", None) if proc is not None else "n/a"))
    try:
        httpd.shutdown()
        httpd.server_close()
    except Exception as e:                       # noqa: BLE001
        _log("shutdown error: %s" % e)
    # 故意不删 state 文件：如果退出时还有残留线程把进程卡住，下一次启动
    # 会看到「pid 活着但端口没响应」，从而把这只僵尸连同子进程一起收掉。
    # 正常退出的情况，pid 已不存在，下次启动按 stale 处理即可。
    _log("server stopped")
    print("已退出。", flush=True)
    # 还有一些残留的非守护线程（线程池 / 第三方库）会让解释器在退出时一直 join，
    # 表现成「日志已经写了 server stopped，但 pythonw 进程还在」的僵尸。
    # 这里强制收尾，保证窗口一关进程就真的结束。
    os._exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        _clear_state()
        _log("FATAL: %s\n%s" % (e, traceback.format_exc()))
        print(f"启动失败：{e}")
        sys.exit(1)
