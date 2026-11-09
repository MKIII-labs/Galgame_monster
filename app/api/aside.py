# -*- coding: utf-8 -*-
"""划词问答接口：选中一段文本就地提问的独立 SSE 通道。

覆盖三种来源（`source`）：
- `chat`：对话 / 编辑器旁注（线程按主会话分组）；
- `notes`：笔记预览里的划词提问（线程按笔记文档分组）；
- `reader`：PDF 阅读器里的划词提问（线程按文档分组）。

刻意不走 /api/chat：那条链路有「同会话串行」约束（ACTIVE_STREAMS），
而且会写消息、跑工具、记轨迹。这里只要一次轻量模型调用。
"""
import json
import threading

from app import aside, aside_store, llm
from app.config import get_api_conf, load_config
from app.registry import route

_LOCK = threading.Lock()
_ACTIVE = {}                 # sid -> 进行中的问答数
MAX_PER_SESSION = 1          # 同一会话同时只答一条，避免把 provider 打满
MAX_GLOBAL = 4


def _enter(sid):
    with _LOCK:
        if _ACTIVE.get(sid, 0) >= MAX_PER_SESSION:
            return "这个会话已经有一条划词问答在回答，等它结束或先关掉"
        if sum(_ACTIVE.values()) >= MAX_GLOBAL:
            return "同时进行的划词问答太多，稍后再试"
        _ACTIVE[sid] = _ACTIVE.get(sid, 0) + 1
        return ""


def _leave(sid):
    with _LOCK:
        n = _ACTIVE.get(sid, 1) - 1
        if n > 0:
            _ACTIVE[sid] = n
        else:
            _ACTIVE.pop(sid, None)


def _scope_of(body):
    """作用域：前端显式传 scope；否则按来源回退到会话 / 文档。"""
    scope = str((body or {}).get("scope") or "").strip()
    if scope:
        return aside_store.norm_scope(scope)
    source = str((body or {}).get("source") or "chat")
    doc = str((body or {}).get("doc_key") or "").strip()
    if source in ("notes", "reader") and doc:
        return aside_store.norm_scope("doc:" + doc)
    return aside_store.norm_scope("session:" + str((body or {}).get("session_id") or "_"))


def _threads_op(body):
    body = body if isinstance(body, dict) else {}
    scope = _scope_of(body)
    op = str(body.get("op") or "list")
    if op == "list":
        return {"ok": True, "scope": scope, "threads": aside_store.list_threads(scope)}
    if op == "create":
        t = aside_store.create_thread(
            scope,
            body.get("selection") or {},
            include_message=body.get("include_message") or False,
            message_text=body.get("message_text") or "",
            title=body.get("title") or "",
            source=body.get("source") or "chat",
            anchor=body.get("anchor") or "",
            doc_key=body.get("doc_key") or "",
        )
        return {"ok": True, "scope": scope, "thread": t}
    if op == "rename":
        t = aside_store.rename_thread(scope, body.get("thread_id"), body.get("title"))
        return {"ok": bool(t), "scope": scope, "thread": t}
    if op == "delete":
        return {"ok": aside_store.delete_thread(scope, body.get("thread_id")), "scope": scope}
    return {"ok": False, "error": "未知操作：%s" % op}


def _stream(h, body):
    body = dict(body or {})
    scope = _scope_of(body)
    tid = str(body.get("thread_id") or "")
    thread = aside_store.get_thread(scope, tid) if tid else None
    if thread:
        # 线程的主上下文以服务端为准：选中片段 / 所在消息 / 历史轮次都从线程读，
        # 前端只负责追加当前问题，避免刷新后历史丢失。
        body["selection"] = thread.get("selection") or {}
        body["include_message"] = thread.get("include_message") or False
        body["message_text"] = thread.get("message_text") or ""
        body["history"] = [{"q": x.get("q"), "a": x.get("a")}
                           for x in (thread.get("history") or [])]
        body["source"] = thread.get("source") or body.get("source") or "chat"
    source = str(body.get("source") or "chat")
    cfg = load_config() or {}
    if not bool(cfg.get("aside_enabled", True)):
        return h._json({"ok": False, "error": "划词问答已在设置里关闭"}, 403)
    msgs = aside.build_messages(body)
    if not msgs:
        return h._json({"ok": False, "error": "选中内容或问题为空"}, 400)
    api_key, api_url, api_model = get_api_conf("chat", cfg)
    if not api_key:
        return h._json({"ok": False, "error": "请先在「设置 → 模型接入」填写密钥"}, 400)
    model = str(cfg.get("aside_model") or "").strip() or api_model
    try:
        max_tokens = max(128, min(4000, int(cfg.get("aside_max_tokens") or 800)))
    except (TypeError, ValueError):
        max_tokens = 800
    sid = str(body.get("session_id") or scope)
    why = _enter(sid)
    if why:
        return h._json({"ok": False, "error": why}, 429)

    h.send_response(200)
    h.send_header("Content-Type", "text/event-stream; charset=utf-8")
    h.send_header("Cache-Control", "no-cache")
    h.send_header("X-Accel-Buffering", "no")
    h.send_header("Connection", "close")
    h.end_headers()
    try:
        h.connection.settimeout(20)       # 客户端断网时不把线程永久挂住
    except Exception:                     # noqa: BLE001
        pass

    stop = threading.Event()
    answer = []

    def emit(obj):
        try:
            h.wfile.write(("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n")
                          .encode("utf-8"))
            h.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            stop.set()

    try:
        emit({"type": "start", "model": model, "thread_id": tid, "source": source})

        def on_visible(text):
            answer.append(str(text or ""))
            emit({"type": "delta", "text": text})

        res = llm.stream_chat(
            api_key, api_url, model, msgs,
            emit=emit,
            on_visible=on_visible,
            allow_tool=False,             # 划词问答永远不调工具
            stop_event=stop,
            max_tokens=max_tokens,
        )
        if thread:
            aside_store.append_qa(scope, tid, body.get("question"), "".join(answer))
        emit({"type": "done", "usage": (res or {}).get("usage") or {},
              "model": model, "thread_id": tid})
    except Exception as e:                # noqa: BLE001
        emit({"type": "error", "message": "问答失败：%s" % e})
    finally:
        _leave(sid)


# ---------------- 新路由（AskKit 使用） ----------------

@route("/api/ask/threads")
def _api_ask_threads(h):
    return h._json(_threads_op(h._body()))


@route("/api/ask/stream")
def _api_ask_stream(h):
    return _stream(h, h._body())


# ---------------- 旧路由（对话旁注旧前端 / 测试） ----------------

@route("/api/aside/threads")
def _api_aside_threads(h):
    body = h._body()
    body.setdefault("scope", aside_store.norm_scope("session:" + str(body.get("session_id") or "_")))
    return h._json(_threads_op(body))


@route("/api/aside/stream")
def _api_aside_stream(h):
    body = h._body()
    body.setdefault("source", "chat")
    return _stream(h, body)
