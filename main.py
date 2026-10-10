# -*- coding: utf-8 -*-
"""
Galgame_monster —— 启动器
在本地启动后端服务，并用 Edge/Chrome 的「应用模式」窗口打开界面
（应用模式窗口没有地址栏和标签页，看起来就是一个独立桌面程序）。

用法： python main.py
退出： 关闭应用窗口即可；或在控制台按 Ctrl+C
"""
import os
import subprocess
import sys
import threading
import time
import webbrowser

from server import create_server

ROOT = os.path.dirname(os.path.abspath(__file__))
PROFILE_DIR = os.path.join(ROOT, "data", ".browser_profile")

WINDOW_W, WINDOW_H = 1280, 860


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


def open_app_window(url):
    """用应用模式打开窗口；成功返回 Popen，失败返回 None"""
    exe = find_browser()
    if not exe:
        return None
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
        "--disable-features=Translate",
    ]
    try:
        return subprocess.Popen(args)
    except Exception as e:
        print(f"[启动] 应用窗口打开失败：{e}")
        return None


def main():
    httpd, port = create_server()
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/"

    print("=" * 56)
    print("  Galgame_monster 已启动")
    print(f"  地址：{url}")
    print("  关闭应用窗口即可退出（或在控制台按 Ctrl+C）")
    print("=" * 56)

    proc = open_app_window(url)

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
            except KeyboardInterrupt:
                pass

    httpd.shutdown()
    print("已退出。")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"启动失败：{e}")
        sys.exit(1)
