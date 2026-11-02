# -*- coding: utf-8 -*-
"""轻量调试会话：Node 走 CDP Inspector，Python 走 debugpy 的 DAP。

设计约束：
- 每个会话最多一个调试进程；启动前把 program 限制在沙箱内。
- 调试器是「可选能力」：debugpy 未安装时给出明确安装提示，不影响其它功能。
- 只暴露 IDE 需要的动作：断点、继续、单步、调用栈、作用域变量、求值、停止。
"""
import importlib.util
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.parse

from app import winproc
from app.computer.cdp import _WSClient


class DebugError(Exception):
    pass


def _uri(path):
    return "file:///" + urllib.parse.quote(str(path).replace("\\", "/").lstrip("/"))


def _value(v):
    if not isinstance(v, dict):
        return str(v)
    if "value" in v:
        return str(v.get("value"))
    return str(v.get("description") or v.get("type") or "")


class _NodeSession:
    language = "node"

    def __init__(self, root, program, args=None, breakpoints=None,
                 stop_on_entry=False):
        self.root = root
        self.program = program
        self.args = list(args or [])
        self.breakpoints = breakpoints or {}
        self.stop_on_entry = stop_on_entry
        self.proc = None
        self.ws = None
        self._seq = 0
        self._pending = {}
        self._events = queue.Queue()
        self._scripts = {}
        self._paused = None
        self._lock = threading.Lock()
        self._stderr = []
        self._stdout = []

    def start(self):
        port = _free_port()
        cmd = ["node", "--inspect-brk=127.0.0.1:%d" % port, self.program] + self.args
        self.proc = subprocess.Popen(
            cmd, cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            creationflags=winproc.no_window())
        threading.Thread(target=self._read_stdout, daemon=True).start()
        url = ""
        deadline = time.time() + 10
        while time.time() < deadline:
            line = self.proc.stderr.readline()
            if not line:
                if self.proc.poll() is not None:
                    break
                time.sleep(0.05)
                continue
            self._stderr.append(line.rstrip())
            m = re.search(r"(ws://\S+)", line)
            if m:
                url = m.group(1)
                break
        if not url:
            self.stop()
            raise DebugError("Node 调试端口没有启动：%s"
                             % (" / ".join(self._stderr[-3:]) or "超时"))
        threading.Thread(target=self._read_stderr, daemon=True).start()
        self.ws = _WSClient(url, timeout=20)
        threading.Thread(target=self._reader, daemon=True).start()
        self.request("Runtime.enable")
        self.request("Debugger.enable")
        self.set_breakpoints(self.breakpoints)
        # --inspect-brk 会停在「等待调试器」；必须显式放行才会到第一行断点
        self.request("Runtime.runIfWaitingForDebugger")
        deadline = time.time() + 3
        while time.time() < deadline and not self._paused:
            time.sleep(0.05)
        if not self.stop_on_entry:
            self.request("Debugger.resume")

    def _read_stderr(self):
        try:
            for line in self.proc.stderr:
                self._stderr.append(line.rstrip())
                if len(self._stderr) > 50:
                    self._stderr = self._stderr[-50:]
        except Exception:                     # noqa: BLE001
            pass

    def _read_stdout(self):
        try:
            for line in self.proc.stdout:
                self._stdout.append(line.rstrip())
                if len(self._stdout) > 100:
                    self._stdout = self._stdout[-100:]
        except Exception:                     # noqa: BLE001
            pass

    def _reader(self):
        while True:
            try:
                raw = self.ws.recv_text()
            except Exception:                 # noqa: BLE001
                self._events.put({"method": "__closed__", "params": {}})
                return
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            rid = msg.get("id")
            if rid is not None:
                with self._lock:
                    hit = self._pending.pop(rid, None)
                if hit:
                    hit["result"] = msg.get("result")
                    hit["error"] = msg.get("error")
                    hit["event"].set()
                continue
            method = str(msg.get("method") or "")
            if method == "Debugger.scriptParsed":
                p = msg.get("params") or {}
                self._scripts[str(p.get("scriptId"))] = p.get("url") or ""
            elif method == "Debugger.paused":
                self._paused = msg.get("params") or {}
            elif method == "Debugger.resumed":
                self._paused = None
            if method:
                self._events.put(msg)

    def request(self, method, params=None, timeout=15):
        if not self.ws:
            raise DebugError("调试连接未建立")
        with self._lock:
            self._seq += 1
            rid = self._seq
            hit = {"event": threading.Event(), "result": None, "error": None}
            self._pending[rid] = hit
        self.ws.send_text(json.dumps({"id": rid, "method": method,
                                      "params": params or {}}, ensure_ascii=False))
        if not hit["event"].wait(timeout):
            raise DebugError("调试请求超时：%s" % method)
        if hit.get("error"):
            raise DebugError("调试请求失败：%s"
                             % (hit["error"].get("message") or hit["error"]))
        return hit.get("result") or {}

    def set_breakpoints(self, mapping):
        for rel, lines in (mapping or {}).items():
            path = os.path.join(self.root, rel)
            self.request("Debugger.setBreakpointByUrl",
                         {"url": _uri(path),
                          "lineNumber": max(0, int(lines[0]) - 1) if lines else 0})
            # CDP 没有「一次设置多行」的接口，逐行设置
            for line in list(lines or [])[1:]:
                self.request("Debugger.setBreakpointByUrl",
                             {"url": _uri(path), "lineNumber": max(0, int(line) - 1)})

    def _wait_paused(self, timeout=4):
        deadline = time.time() + timeout
        while time.time() < deadline and not self._paused:
            time.sleep(0.05)
        return bool(self._paused)

    def action(self, act):
        if act == "pause":
            self.request("Debugger.pause")
            self._wait_paused()
            return
        method = {"continue": "Debugger.resume", "next": "Debugger.stepOver",
                  "step_in": "Debugger.stepInto", "step_out": "Debugger.stepOut"}.get(act)
        if not method:
            raise DebugError("未知调试动作：%s" % act)
        if act == "continue":
            self.request(method)
            self._paused = None
            return
        if not self._paused:
            return
        self._paused = None
        self.request(method)
        self._wait_paused()

    def _frames(self):
        out = []
        for f in (self._paused or {}).get("callFrames") or []:
            loc = f.get("location") or {}
            url = f.get("url") or self._scripts.get(str(loc.get("scriptId")), "")
            rel = ""
            if url.startswith("file:///"):
                try:
                    rel = os.path.relpath(urllib.parse.unquote(url[8:]), self.root)
                except ValueError:
                    rel = ""
                rel = rel.replace("\\", "/")
            out.append({"id": f.get("callFrameId"), "name": f.get("functionName") or "(anonymous)",
                        "path": rel, "line": int(loc.get("lineNumber") or 0) + 1,
                        "_scopes": f.get("scopeChain") or []})
        return out

    def _scopes(self, frame):
        out = []
        for sc in frame.get("_scopes") or []:
            obj = sc.get("object") or {}
            oid = obj.get("objectId")
            if not oid:
                continue
            try:
                res = self.request("Runtime.getProperties",
                                   {"objectId": oid, "ownProperties": True})
            except DebugError:
                continue
            vals = []
            for prop in (res.get("result") or [])[:60]:
                name = str(prop.get("name") or "")
                if not name or name == "__proto__":
                    continue
                val = prop.get("value") or {}
                vals.append({"name": name, "value": _value(val),
                             "type": val.get("type") or ""})
            out.append({"name": sc.get("type") or "scope", "variables": vals})
        return out

    def state(self):
        frames = self._frames()
        scopes = self._scopes(frames[0]) if frames else []
        running = bool(self.proc and self.proc.poll() is None)
        return {"ok": True, "running": running, "paused": bool(self._paused),
                "language": "node", "frames": frames, "scopes": scopes,
                "output": (self._stdout + self._stderr)[-40:]}

    def evaluate(self, expression, frame_id=""):
        frames = self._frames()
        if not frames:
            raise DebugError("当前没有暂停的栈帧")
        fid = frame_id or frames[0]["id"]
        res = self.request("Debugger.evaluateOnCallFrame",
                           {"callFrameId": fid, "expression": expression,
                            "returnByValue": True})
        return {"ok": True, "value": _value(res.get("result") or {})}

    def stop(self):
        try:
            if self.ws:
                self.ws.close()
        except Exception:                     # noqa: BLE001
            pass
        self.ws = None
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=3)
            except Exception:                 # noqa: BLE001
                try:
                    self.proc.kill()
                except Exception:             # noqa: BLE001
                    pass


class _PySession:
    language = "python"

    def __init__(self, root, program, args=None, breakpoints=None,
                 stop_on_entry=False):
        self.root = root
        self.program = program
        self.args = list(args or [])
        self.breakpoints = breakpoints or {}
        self.stop_on_entry = stop_on_entry
        self.proc = None
        self.sock = None
        self._file = None
        self._seq = 0
        self._lock = threading.Lock()
        self._pending = {}
        self._stopped = None
        self._thread_id = None
        self._output = []
        self._stderr = []

    def start(self):
        if not importlib.util.find_spec("debugpy"):
            raise DebugError("未安装 debugpy：请先运行 `python -m pip install debugpy`")
        port = _free_port()
        cmd = [sys.executable, "-m", "debugpy", "--listen", "127.0.0.1:%d" % port,
               "--wait-for-client", self.program] + self.args
        self.proc = subprocess.Popen(
            cmd, cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            creationflags=winproc.no_window())
        deadline = time.time() + 12
        while time.time() < deadline:
            try:
                self.sock = socket.create_connection(("127.0.0.1", port), timeout=1)
                break
            except OSError:
                if self.proc.poll() is not None:
                    break
                time.sleep(0.1)
        if not self.sock:
            self.stop()
            raise DebugError("debugpy 没有监听调试端口（可能启动失败）")
        self._file = self.sock.makefile("rb")
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._read_output, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        self._request("initialize", {
            "clientID": "galgame-monster", "adapterID": "python",
            "pathFormat": "path", "linesStartAt1": True, "columnsStartAt1": True,
            "supportsRunInTerminalRequest": False})
        self._request("launch", {
            "program": self.program, "cwd": self.root, "args": self.args,
            "justMyCode": False, "console": "internalConsole"})
        self.set_breakpoints(self.breakpoints)
        self._request("configurationDone")
        if not self.stop_on_entry:
            self.action("continue")

    def _read_output(self):
        try:
            for line in self.proc.stdout:
                self._output.append(line.rstrip())
                if len(self._output) > 100:
                    self._output = self._output[-100:]
        except Exception:                     # noqa: BLE001
            pass

    def _read_stderr(self):
        try:
            for line in self.proc.stderr:
                self._stderr.append(line.rstrip())
                if len(self._stderr) > 100:
                    self._stderr = self._stderr[-100:]
        except Exception:                     # noqa: BLE001
            pass

    def _recv(self):
        headers = {}
        while True:
            line = self._file.readline()
            if not line:
                raise DebugError("调试连接已关闭")
            line = line.strip()
            if not line:
                break
            k, _, v = line.decode("latin-1").partition(":")
            headers[k.strip().lower()] = v.strip()
        n = int(headers.get("content-length") or 0)
        if n <= 0:
            return {}
        return json.loads(self._file.read(n).decode("utf-8", "replace"))

    def _reader(self):
        while True:
            try:
                msg = self._recv()
            except Exception:                 # noqa: BLE001
                break
            if msg.get("type") == "response":
                with self._lock:
                    hit = self._pending.pop(msg.get("request_seq"), None)
                if hit:
                    hit["result"] = msg
                    hit["event"].set()
            elif msg.get("type") == "event":
                ev = msg.get("event")
                body = msg.get("body") or {}
                if ev == "stopped":
                    self._stopped = body
                    self._thread_id = body.get("threadId")
                elif ev == "continued":
                    self._stopped = None
                elif ev == "output":
                    text = str(body.get("output") or "").rstrip()
                    if text:
                        self._output.append(text)

    def _request(self, command, args=None, timeout=15):
        if not self.sock:
            raise DebugError("调试连接未建立")
        with self._lock:
            self._seq += 1
            seq = self._seq
            hit = {"event": threading.Event(), "result": None}
            self._pending[seq] = hit
        payload = json.dumps({"seq": seq, "type": "request", "command": command,
                              "arguments": args or {}}, ensure_ascii=False).encode("utf-8")
        self.sock.sendall(b"Content-Length: %d\r\n\r\n" % len(payload) + payload)
        if not hit["event"].wait(timeout):
            raise DebugError("调试请求超时：%s" % command)
        res = hit.get("result") or {}
        if not res.get("success", True):
            raise DebugError(str(res.get("message") or "调试请求失败：%s" % command))
        return res.get("body") or {}

    def set_breakpoints(self, mapping):
        for rel, lines in (mapping or {}).items():
            path = os.path.join(self.root, rel)
            self._request("setBreakpoints", {
                "source": {"path": path},
                "breakpoints": [{"line": int(x)} for x in lines]})

    def _wait_stopped(self, timeout=4):
        deadline = time.time() + timeout
        while time.time() < deadline and not self._stopped:
            time.sleep(0.05)
        return bool(self._stopped)

    def action(self, act):
        if act == "pause":
            self._request("pause", {"threadId": self._thread_id or 1})
            self._wait_stopped()
            return
        cmd = {"continue": "continue", "next": "next", "step_in": "stepIn",
               "step_out": "stepOut"}.get(act)
        if not cmd:
            raise DebugError("未知调试动作：%s" % act)
        tid = (self._stopped or {}).get("threadId") or self._thread_id or 1
        if act == "continue":
            self._request(cmd, {"threadId": tid})
            self._stopped = None
            return
        if not self._stopped:
            return
        self._stopped = None
        self._request(cmd, {"threadId": tid})
        self._wait_stopped()

    def _frames(self):
        if not self._stopped:
            return []
        body = self._request("stackTrace", {
            "threadId": self._stopped.get("threadId") or 1,
            "startFrame": 0, "levels": 20})
        out = []
        for f in body.get("stackFrames") or []:
            src = f.get("source") or {}
            path = src.get("path") or ""
            rel = os.path.relpath(path, self.root).replace("\\", "/") if path else ""
            out.append({"id": f.get("id"), "name": f.get("name") or "(module)",
                        "path": rel, "line": int(f.get("line") or 1)})
        return out

    def _scopes(self, frame):
        if not frame:
            return []
        body = self._request("scopes", {"frameId": frame["id"]})
        out = []
        for sc in body.get("scopes") or []:
            ref = sc.get("variablesReference")
            if not ref:
                continue
            vals = []
            try:
                vb = self._request("variables", {"variablesReference": ref})
                for v in (vb.get("variables") or [])[:80]:
                    vals.append({"name": v.get("name") or "",
                                 "value": str(v.get("value") or ""),
                                 "type": str(v.get("type") or "")})
            except DebugError:
                pass
            out.append({"name": sc.get("name") or "scope", "variables": vals})
        return out

    def state(self):
        frames = self._frames()
        running = bool(self.proc and self.proc.poll() is None)
        return {"ok": True, "running": running, "paused": bool(self._stopped),
                "language": "python", "frames": frames,
                "scopes": self._scopes(frames[0]) if frames else [],
                "output": (self._output + self._stderr)[-40:]}

    def evaluate(self, expression, frame_id=""):
        frames = self._frames()
        args = {"expression": expression, "context": "watch"}
        if frame_id:
            args["frameId"] = int(frame_id)
        elif frames:
            args["frameId"] = frames[0]["id"]
        body = self._request("evaluate", args)
        return {"ok": True, "value": str(body.get("result") or "")}

    def stop(self):
        try:
            if self.sock:
                self._request("disconnect", {"terminateDebuggee": True}, timeout=3)
        except Exception:                     # noqa: BLE001
            pass
        try:
            if self._file:
                self._file.close()
        except Exception:                     # noqa: BLE001
            pass
        try:
            if self.sock:
                self.sock.close()
        except Exception:                     # noqa: BLE001
            pass
        self.sock = None
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=3)
            except Exception:                 # noqa: BLE001
                try:
                    self.proc.kill()
                except Exception:             # noqa: BLE001
                    pass


_SESSIONS = {}
_LOCK = threading.Lock()


def start(sid, root, program, args=None, language=None, breakpoints=None,
          stop_on_entry=False):
    """启动一个调试会话；同会话已有实例时先停掉。"""
    root = os.path.realpath(str(root or ""))
    if not root or not os.path.isdir(root):
        raise DebugError("沙箱目录不可用")
    program = str(program or "").strip()
    full = program if os.path.isabs(program) else os.path.join(root, program)
    full = os.path.realpath(full)
    if not full.startswith(root + os.sep) or not os.path.isfile(full):
        raise DebugError("程序必须位于沙箱内：%s" % program)
    lang = str(language or "").lower()
    if not lang:
        ext = os.path.splitext(full)[1].lower()
        lang = "python" if ext in (".py", ".pyw") else "node"
    stop(sid)
    sess = (_PySession if lang == "python" else _NodeSession)(
        root, full, args, breakpoints, stop_on_entry)
    sess.start()
    with _LOCK:
        _SESSIONS[sid] = sess
    return {"ok": True, "language": lang, "program": os.path.relpath(full, root)}


def get(sid):
    with _LOCK:
        return _SESSIONS.get(sid)


def state(sid):
    sess = get(sid)
    if not sess:
        return {"ok": True, "running": False, "paused": False, "frames": [],
                "scopes": [], "language": "", "output": []}
    return sess.state()


def set_breakpoints(sid, mapping):
    sess = get(sid)
    if not sess:
        return {"ok": False, "error": "没有进行中的调试会话"}
    sess.breakpoints = mapping or {}
    sess.set_breakpoints(sess.breakpoints)
    return {"ok": True}


def action(sid, act):
    sess = get(sid)
    if not sess:
        return {"ok": False, "error": "没有进行中的调试会话"}
    sess.action(act)
    return {"ok": True}


def evaluate(sid, expression, frame_id=""):
    sess = get(sid)
    if not sess:
        return {"ok": False, "error": "没有进行中的调试会话"}
    return sess.evaluate(expression, frame_id)


def stop(sid):
    with _LOCK:
        sess = _SESSIONS.pop(sid, None)
    if sess:
        sess.stop()
    return {"ok": True}


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port
