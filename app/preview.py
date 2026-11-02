# -*- coding: utf-8 -*-
"""IDE 内置网页预览：把沙箱目录临时挂到 127.0.0.1:<随机端口>。

为什么不用 iframe 直接读文件：iframe 请求带不了 X-Auth-Token，
而页面里的相对路径 css/js 又必须能正常解析。起一个只监听本机、
只读沙箱目录的临时静态服务最省事；关掉预览面板就停掉，端口不常驻。
"""
import os
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from app import agent

_LOCK = threading.Lock()
_SERVER = None
_THREAD = None
_ROOT = ""


def _handler(root):
    class H(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=root, **kw)

        def log_message(self, *a):       # 预览页的资源请求不要刷日志
            pass

        def end_headers(self):
            # 改完文件点「刷新」要立刻看到，禁止缓存
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

        def send_head(self):
            # 预览页与静态资源同源：页面脚本能 fetch 任意路径，
            # 所以 .env / 私钥 / .git 这些不能用「工具读不了」当挡箭牌——
            # 这里按 agent 的路径规则再拦一次（越界、禁止目录、密钥文件一律 403）。
            try:
                rel = os.path.relpath(self.translate_path(self.path), root)
                if not rel.startswith(".."):
                    agent.resolve_path(root, rel, for_read=True)
                    if agent.is_secret_path(rel):
                        raise agent.SandboxError("secret")
            except (agent.SandboxError, OSError):
                self.send_error(403, "Forbidden")
                return None
            return super().send_head()

    return H



def start(root):
    """启动（或复用）预览服务，返回 base URL。"""
    global _SERVER, _THREAD, _ROOT
    root = os.path.abspath(root)
    with _LOCK:
        if _SERVER and _ROOT == root and _THREAD and _THREAD.is_alive():
            return "http://127.0.0.1:%d/" % _SERVER.server_address[1]
        _stop_locked()
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _handler(root))
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        _SERVER, _THREAD, _ROOT = srv, th, root
        return "http://127.0.0.1:%d/" % srv.server_address[1]


def _stop_locked():
    global _SERVER, _THREAD, _ROOT
    if _SERVER:
        try:
            _SERVER.shutdown()
        except Exception:                   # noqa: BLE001
            pass
        try:
            _SERVER.server_close()
        except Exception:                   # noqa: BLE001
            pass
    _SERVER, _THREAD, _ROOT = None, None, ""


def stop():
    with _LOCK:
        _stop_locked()
