# -*- coding: utf-8 -*-
"""生命周期钩子：在固定节点插入用户自己的逻辑。

设计目标只有两个：
1. 不侵入主流程 —— 钩子默认关闭，坏了不能把对话带崩（fail-open 默认）；
2. 覆盖所有执行路径 —— PreToolUse / PostToolUse 挂在 `agent.run_tool`，
   所以对话、工作流、子代理、Code Mode 都走同一套钩子。

事件：
- UserPromptSubmit  用户消息提交后
- PreToolUse        工具执行前（可 block，理由会回灌给模型）
- PostToolUse       工具执行后（审计 / 追加提示）
- SessionStart      新会话创建
- Stop              一轮输出结束
- PreCompact        历史压缩前

处理器两种：
- {"type":"command","command":"python hooks/audit.py","timeout":10,"fail_open":true}
- {"type":"python","handler":"gm_plugin_name:func"}   # 从 data/plugins 加载的函数

处理器返回一个 JSON 对象（command 读 stdout；python 直接返回 dict）：
  {"decision":"block","reason":"...","context":"要注入模型的上下文"}
"""
import importlib
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time

CONFIG_TTL = 3.0          # 配置缓存：钩子在一轮里会被调很多次，没必要每次都读盘
DEFAULT_TIMEOUT = 10
MAX_OUTPUT = 6000

_CFG = {"at": 0.0, "val": {}}
_CFG_LOCK = threading.Lock()
_PLUGIN_DIR = None


def _cfg():
    now = time.time()
    with _CFG_LOCK:
        if now - float(_CFG["at"] or 0) < CONFIG_TTL:
            return _CFG["val"]
    try:
        from app.config import load_config
        val = load_config() or {}
    except Exception:                        # noqa: BLE001
        val = {}
    with _CFG_LOCK:
        _CFG["at"] = now
        _CFG["val"] = val
    return val


def clear_cache():
    with _CFG_LOCK:
        _CFG["at"] = 0.0
        _CFG["val"] = {}


def enabled():
    return bool(_cfg().get("hooks_enabled"))


def _items(event):
    raw = _cfg().get("hooks") or {}
    if not isinstance(raw, dict):
        return []
    items = raw.get(event) or []
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        return []
    return [h for h in items if isinstance(h, dict)]


def _plugin_dir():
    global _PLUGIN_DIR
    if _PLUGIN_DIR is None:
        try:
            from app.store import DATA_DIR
            _PLUGIN_DIR = os.path.join(DATA_DIR, "plugins")
        except Exception:                    # noqa: BLE001
            _PLUGIN_DIR = ""
    return _PLUGIN_DIR


def _load_module(name):
    try:
        return importlib.import_module(name)
    except Exception:                        # noqa: BLE001
        pass
    path = os.path.join(_plugin_dir(), name if name.endswith(".py") else name + ".py")
    if not os.path.isfile(path):
        return None
    mod_name = "gm_hook_" + os.path.splitext(os.path.basename(path))[0].replace("-", "_")
    try:
        spec = importlib.util.spec_from_file_location(mod_name, path)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
        return mod
    except Exception:                        # noqa: BLE001
        sys.modules.pop(mod_name, None)
        return None


def _handler(spec):
    spec = str(spec or "").strip()
    if not spec:
        return None
    mod_name, _, fn_name = spec.partition(":")
    if not fn_name:
        fn_name, mod_name = spec, ""
    if mod_name:
        mod = _load_module(mod_name)
        return getattr(mod, fn_name, None) if mod is not None else None
    # 只给函数名：在已加载的 data/plugins 模块里找
    for name, mod in list(sys.modules.items()):
        if name.startswith(("gm_plugin_", "gm_hook_")) and hasattr(mod, fn_name):
            return getattr(mod, fn_name)
    return None


def _parse_output(text):
    """从钩子 stdout 里取 JSON 对象。

    Windows 的 cmd AutoRun / chcp 可能往 stdout 前面塞提示行（如
    "Active code page: 65001"），所以不能要求整段都是 JSON：
    先整段解析，失败再从第一个 `{` 到最后一个 `}` 截出来解析。
    """
    t = str(text or "").strip()
    if not t:
        return None
    try:
        val = json.loads(t)
        return val if isinstance(val, dict) else None
    except ValueError:
        pass
    start, end = t.find("{"), t.rfind("}")
    if 0 <= start < end:
        try:
            val = json.loads(t[start:end + 1])
            return val if isinstance(val, dict) else None
        except ValueError:
            return None
    return None


def _run_one(h, event, payload, timeout):
    kind = str(h.get("type") or "command").strip().lower()
    if kind == "python":
        fn = _handler(h.get("handler") or h.get("name"))
        if fn is None:
            return {"error": "找不到 hook 函数：%s" % h.get("handler")}
        res = fn(payload)
        if isinstance(res, dict):
            return res
        if isinstance(res, str) and res.strip():
            return {"context": res}
        return {}
    cmd = str(h.get("command") or "").strip()
    if not cmd:
        return {"error": "command 钩子缺少 command"}
    cwd = str(payload.get("cwd") or "") or None
    env = dict(os.environ)
    env["GM_HOOK_EVENT"] = event
    try:
        p = subprocess.run(cmd, shell=True, cwd=cwd, env=env, timeout=timeout,
                           input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.TimeoutExpired:
        return {"error": "hook 超时（%ss）：%s" % (timeout, cmd)}
    except OSError as e:
        return {"error": "hook 启动失败：%s" % e}
    text = (p.stdout or b"").decode("utf-8", "replace").strip()[:MAX_OUTPUT]
    data = _parse_output(text)
    if data is not None:
        return data
    if p.returncode != 0:
        err = (p.stderr or b"").decode("utf-8", "replace").strip()[:500]
        return {"error": "hook 退出码 %s：%s" % (p.returncode, err or cmd)}
    return {"context": text} if text else {"exit": 0}


def run(event, payload=None, timeout=None):
    """按顺序执行某事件的钩子。

    返回 {"decision": ""|"block", "reason": str, "contexts": [str]}。
    fail_open=False 的 command 钩子失败时，PreToolUse 会转成 block（宁可停下也不放行）。
    """
    payload = dict(payload or {})
    payload.setdefault("event", event)
    out = {"decision": "", "reason": "", "contexts": []}
    if not enabled():
        return out
    for h in _items(event):
        try:
            t = max(1, min(300, int(h.get("timeout") or timeout or DEFAULT_TIMEOUT)))
            res = _run_one(h, event, payload, t)
        except Exception as e:                # noqa: BLE001  钩子坏了不能带崩工具
            res = {"error": str(e)}
        if not isinstance(res, dict):
            continue
        if res.get("error"):
            if h.get("fail_open") is False and event == "PreToolUse":
                return {"decision": "block",
                        "reason": "hook 失败（fail-closed）：%s" % res["error"],
                        "contexts": out["contexts"]}
            continue
        if str(res.get("decision") or "").strip().lower() == "block":
            out["decision"] = "block"
            out["reason"] = str(res.get("reason") or "被 hook 阻止").strip()
            return out
        ctx = str(res.get("context") or "").strip()
        if ctx:
            out["contexts"].append(ctx[:MAX_OUTPUT])
    return out


def before_tool(name, args, sandbox="", sid=""):
    return run("PreToolUse", {"tool": name, "args": args,
                              "cwd": sandbox or "", "session": sid})


def after_tool(name, args, ok, result, sandbox="", sid=""):
    return run("PostToolUse", {"tool": name, "args": args, "ok": bool(ok),
                               "result": str(result or "")[:MAX_OUTPUT],
                               "cwd": sandbox or "", "session": sid})


def prompt_context(text, sid=""):
    """UserPromptSubmit：返回要追加到本轮上下文里的文本（没有就空串）。"""
    res = run("UserPromptSubmit", {"prompt": str(text or "")[:MAX_OUTPUT],
                                   "session": sid})
    if res.get("decision") == "block":
        return "【hook 拒绝了本轮】%s" % (res.get("reason") or "")
    return "\n".join(res.get("contexts") or [])


def session_start(sid="", cwd=""):
    res = run("SessionStart", {"session": sid, "cwd": cwd or ""})
    return "\n".join(res.get("contexts") or [])


def stop(sid="", cwd=""):
    return run("Stop", {"session": sid, "cwd": cwd or ""})


def pre_compact(sid="", cwd=""):
    return run("PreCompact", {"session": sid, "cwd": cwd or ""})
