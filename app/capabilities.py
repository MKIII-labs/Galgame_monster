# -*- coding: utf-8 -*-
"""能力契约：让「对话 Agent」与「任务引擎」共用同一套工具。

一个能力由四部分组成：
- id          模型看到的工具名，如 calendar.create
- description 给模型看的一句话说明
- schema      参数说明（用于拼提示词，不做严格校验）
- permission  read | write；write 在任务自动执行时必须人工确认

为什么不复用 agent.run_tool：文件工具依赖沙箱路径，而日程/任务不需要沙箱，
权限模型也不同。这里做一层薄注册表，两边各自调用，互不干扰。
"""
import importlib.util
import os
import re
import sys
import threading
from dataclasses import dataclass, field

from app.store import DATA_DIR

PLUGIN_DIR = os.path.join(DATA_DIR, "plugins")
_PLUGIN_NAME = re.compile(r"^[A-Za-z0-9_-]+\.py$")
_PLUGINS = {"loaded": False}


@dataclass
class Capability:
    id: str
    description: str
    schema: dict = field(default_factory=dict)
    permission: str = "read"
    handler: object = None
    chat: bool = True          # 是否允许在主对话里直接调用


_CAPS = {}
_lock = threading.Lock()


def capability(cid, description, schema=None, permission="read", chat=True):
    """注册一个能力。handler 签名：fn(args: dict, ctx: dict) -> str

    chat=False 的能力只给任务引擎用（例如文件工具，它仍走 Agent 的沙箱开关，
    不能在对话里绕过「Agent 是否开启」。
    """
    if permission not in ("read", "write"):
        raise ValueError("permission 只能是 read / write：%s" % permission)

    def deco(fn):
        with _lock:
            if cid in _CAPS:
                raise RuntimeError("能力重复注册：%s" % cid)
            _CAPS[cid] = Capability(id=cid, description=description,
                                    schema=dict(schema or {}),
                                    permission=permission, handler=fn,
                                    chat=bool(chat))
        return fn

    return deco


def _ensure_plugins():
    """第一次访问能力表时加载 `data/plugins/*.py`。

    插件是**任意 Python 代码**，所以默认关闭：只有 `config.plugins_enabled=true` 才加载，
    且只认目录下形如 `name.py` 的文件（不递归、不加载 `_` 开头的）。
    加载失败只打印，不影响启动与其他插件。

    插件约定：模块里提供 `register()`，在里面用 `@capability(...)` 注册。
    不热加载 —— 新增 / 修改插件后需要重启进程。
    """
    if _PLUGINS["loaded"]:
        return
    _PLUGINS["loaded"] = True          # 先置位，避免导入期间递归进来
    try:
        from app.config import load_config
        if not bool(load_config().get("plugins_enabled", False)):
            return
    except Exception as e:
        print("[插件] 读取开关失败，已跳过：%s" % e)
        return
    if not os.path.isdir(PLUGIN_DIR):
        return
    for fn in sorted(os.listdir(PLUGIN_DIR)):
        if not _PLUGIN_NAME.match(fn) or fn.startswith("_"):
            continue
        path = os.path.join(PLUGIN_DIR, fn)
        mod_name = "gm_plugin_" + os.path.splitext(fn)[0].replace("-", "_")
        try:
            spec = importlib.util.spec_from_file_location(mod_name, path)
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = mod
            spec.loader.exec_module(mod)
            reg = getattr(mod, "register", None)
            if callable(reg):
                reg()
                print("[插件] 已加载：%s" % fn)
        except Exception as e:
            sys.modules.pop(mod_name, None)
            print("[插件] %s 加载失败：%s" % (fn, e))


def get(cid):
    _ensure_plugins()
    return _CAPS.get((cid or "").strip())


def invoke(cid, args, ctx=None):
    """执行能力。返回 (结果文本, 是否写操作)。

    失败不抛异常：统一返回以「❌」开头的文本，调用方据此判断成败
    （与 agent.run_tool 的约定保持一致）。
    """
    _ensure_plugins()
    cap = get(cid)
    if cap is None:
        return "❌ 未知能力：%s" % cid, False
    try:
        text = cap.handler(args if isinstance(args, dict) else {}, ctx or {})
    except Exception as e:
        return "❌ %s 执行失败：%s" % (cid, e), cap.permission == "write"
    return str(text if text is not None else ""), cap.permission == "write"


def describe_lines(chat_only=True):
    """给系统提示词用的能力清单"""
    _ensure_plugins()
    with _lock:
        caps = sorted(_CAPS.values(), key=lambda c: c.id)
    if chat_only:
        caps = [c for c in caps if c.chat]
    lines = []
    for c in caps:
        args = ", ".join(c.schema.keys()) if c.schema else ""
        lines.append("- %-16s {%s}  %s" % (c.id, args, c.description))
    return lines


def ids():
    _ensure_plugins()
    with _lock:
        return sorted(_CAPS.keys())


def chat_items():
    """主对话可见的能力对象（供原生 function calling 生成 schema）。"""
    _ensure_plugins()
    with _lock:
        return [c for c in sorted(_CAPS.values(), key=lambda c: c.id) if c.chat]
