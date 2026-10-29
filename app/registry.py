# -*- coding: utf-8 -*-
"""路由注册表。

背景：httpd.py 里原先是一张 90+ 项的硬编码路由表，新增功能要同时改
api 模块、路由表、前端三处，容易漏。这里提供「模块自注册」：

    from app.registry import route

    @route("/api/foo")
    def _api_foo(h):
        ...

约束：
- 老路由**保持原样**，不迁移；httpd 先查老表、再查这里。
- 同一 (method, path) 重复注册会直接报错，避免静默覆盖。
"""
import threading

ROUTES = {}
_lock = threading.Lock()


def route(path, methods=("POST",)):
    """把一个处理函数注册到路由表。方法默认只接受 POST。"""
    if not path.startswith("/api/"):
        raise ValueError("只允许注册 /api/ 下的路径：%s" % path)
    normalized = tuple(m.upper() for m in methods)

    def deco(fn):
        with _lock:
            for m in normalized:
                key = (m, path)
                if key in ROUTES:
                    raise RuntimeError("路由重复注册：%s %s" % key)
                ROUTES[key] = fn
        return fn

    return deco


def get(method, path):
    return ROUTES.get((str(method or "").upper(), path))


def snapshot():
    """返回注册表副本（测试 / 自检用）"""
    with _lock:
        return dict(ROUTES)
