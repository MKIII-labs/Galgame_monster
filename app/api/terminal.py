# -*- coding: utf-8 -*-
"""IDE 终端 HTTP 接口：start / input / resize / interrupt / clear / kill / stream"""
import json
import os
import queue
from urllib.parse import urlparse, parse_qs

from app import agent
from app import terminal
from app.registry import route
from app.sessions import find_session, load_sessions


def _fail(h, msg, code=400):
    h._json({"ok": False, "error": msg}, code)


def _sandbox_of(sid):
    s = find_session(load_sessions(), sid)
    if not s:
        return "", "会话不存在"
    box = agent.clean_sandbox(s.get("sandbox"))
    if not box or not os.path.isdir(box):
        return "", "请先在输入框上方选择沙箱目录"
    return box, ""


def _int(v, default, lo, hi):
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


@route("/api/ide/term/start")
def _api_term_start(h):
    body = h._body()
    sid = str(body.get("session_id") or "")
    box, err = _sandbox_of(sid)
    if err:
        return _fail(h, err)
    try:
        t = terminal.start(sid, box,
                           cols=_int(body.get("cols"), 120, 20, 500),
                           rows=_int(body.get("rows"), 30, 5, 200),
                           force=bool(body.get("force")))
    except terminal.TerminalError as e:
        return _fail(h, str(e))
    except Exception as e:                       # noqa: BLE001
        return _fail(h, "终端启动失败：%s" % e, 500)
    h._json({"ok": True, **t.snapshot(0)})


@route("/api/ide/term/input")
def _api_term_input(h):
    body = h._body()
    t = terminal.get(body.get("tid"))
    if not t:
        return _fail(h, "终端已关闭", 404)
    data = str(body.get("data") or "")
    h._json({"ok": t.write(data) if data else True})


@route("/api/ide/term/resize")
def _api_term_resize(h):
    body = h._body()
    t = terminal.get(body.get("tid"))
    if not t:
        return _fail(h, "终端已关闭", 404)
    h._json({"ok": t.resize(_int(body.get("cols"), 120, 20, 500),
                            _int(body.get("rows"), 30, 5, 200))})


@route("/api/ide/term/interrupt")
def _api_term_interrupt(h):
    t = terminal.get(h._body().get("tid"))
    if not t:
        return _fail(h, "终端已关闭", 404)
    h._json({"ok": t.interrupt()})


@route("/api/ide/term/clear")
def _api_term_clear(h):
    t = terminal.get(h._body().get("tid"))
    if not t:
        return _fail(h, "终端已关闭", 404)
    h._json({"ok": t.clear()})


@route("/api/ide/term/kill")
def _api_term_kill(h):
    h._json({"ok": terminal.drop(h._body().get("tid"))})


@route("/api/ide/term/stream", methods=("GET",))
def _api_term_stream(h):
    """SSE：先补发 off 之后的存量输出，再持续推送新输出；进程退出时发 exit 并关连接。"""
    qs = parse_qs(urlparse(h.path).query)
    t = terminal.get((qs.get("tid") or [""])[0])
    if not t:
        return _fail(h, "终端已关闭", 404)
    try:
        off = int((qs.get("off") or ["0"])[0])
    except ValueError:
        off = 0
    sub = t.subscribe()
    try:
        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream; charset=utf-8")
        h.send_header("Cache-Control", "no-cache")
        h.send_header("X-Accel-Buffering", "no")
        h.send_header("Connection", "close")
        h.end_headers()

        def emit(obj):
            h.wfile.write(("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n")
                          .encode("utf-8"))
            h.wfile.flush()

        snap = t.snapshot(off)
        emit({"type": "init", "text": snap["text"], "off": snap["off"],
              "alive": snap["alive"], "cwd": snap["cwd"], "reset": snap["truncated"]})
        if not snap["alive"]:
            emit({"type": "exit"})
            return
        while True:
            try:
                chunk = sub.get(timeout=15)
            except queue.Empty:
                h.wfile.write(b": ping\n\n")
                h.wfile.flush()
                continue
            if chunk is None:
                emit({"type": "exit"})
                break
            snap = t.snapshot(snap["off"])
            emit({"type": "out", "text": snap["text"], "off": snap["off"],
                  "cwd": snap["cwd"], "reset": snap["truncated"]})
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    finally:
        t.unsubscribe(sub)
