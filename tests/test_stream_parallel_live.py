# -*- coding: utf-8 -*-
"""多会话并行 · 真机端到端（会启动 Edge + 假模型）

默认跳过：
    set GM_LIVE_UI=1 && python -m pytest tests/test_stream_parallel_live.py -q

验证四件事：
  1. A 生成中切到 B：A 的流不中断（state.streams[A] 仍在）；
  2. 在 B 里继续发消息：两个会话真并发，各自完成；
  3. 切回 A：live DOM 重新挂上、继续收流；
  4. A 在后台完成 → 未读标记 + 两条消息完整落盘。
"""
import copy
import http.server
import json
import os
import socketserver
import subprocess
import tempfile
import threading
import time

import pytest
import requests

from app import config as C
from app import sessions as S
from app import webprobe

pytestmark = pytest.mark.skipif(
    not os.environ.get("GM_LIVE_UI"),
    reason="会启动 Edge 与假模型；设 GM_LIVE_UI=1 才跑")

CHUNKS = list("这是一段慢速回复，用来验证并发。")


class _FakeModel(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            req = json.loads(raw.decode("utf-8"))
        except Exception:
            req = {}
        model = req.get("model") or "fake"
        if req.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                for t in CHUNKS:
                    body = {"id": "x", "model": model,
                            "choices": [{"index": 0, "delta": {"content": t},
                                         "finish_reason": None}]}
                    self.wfile.write(("data: " + json.dumps(body, ensure_ascii=False) + "\n\n")
                                     .encode())
                    self.wfile.flush()
                    time.sleep(0.2)
                end = {"id": "x", "model": model,
                       "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                       "usage": {"prompt_tokens": 10, "completion_tokens": len(CHUNKS),
                                 "total_tokens": 10 + len(CHUNKS)}}
                self.wfile.write(("data: " + json.dumps(end, ensure_ascii=False) + "\n\n").encode())
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        data = {"id": "x", "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6}}
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_parallel_sessions_keep_streaming():
    edge = webprobe.edge_path()
    if not edge:
        pytest.skip("本机没有 Edge")

    import app.usage as U
    import app.saves as SA
    from app import httpd
    from app.computer.cdp import CDP

    mp = pytest.MonkeyPatch()
    fake = srv = None
    proc = None
    try:
        # 假模型
        fake = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _FakeModel)
        fake.daemon_threads = True
        fake_port = fake.server_address[1]
        threading.Thread(target=fake.serve_forever, daemon=True).start()

        # 会话文件重定向到临时目录
        S.SESSION_FILE = os.path.join(tempfile.mkdtemp(prefix="cu_sess_"), "sessions.json")
        S.save_sessions({"current": "s1", "sessions": [
            {"id": "s1", "title": "A", "messages": [], "updated_at": 0},
            {"id": "s2", "title": "B", "messages": [], "updated_at": 0},
        ]})

        cfg = copy.deepcopy(C.DEFAULT_CONFIG)
        cfg.update({
            "chat_api_key": "fake-key",
            "chat_url": "http://127.0.0.1:%d/v1/chat/completions" % fake_port,
            "chat_model": "fake", "chat_protocol": "openai",
            "chat_native_tools": False, "chat_think_enabled": False,
            "chat_think_mode": "", "search_enabled": False, "memory_llm_pick": False,
        })
        patch = lambda: dict(cfg)          # noqa: E731
        mp.setattr(C, "load_config", patch)
        mp.setattr(C, "save_config", lambda c: None)
        # 已被其它测试导入过的模块持有自己的引用，逐个替换
        import app.api.chat as chat_mod
        import app.api.misc as misc_mod
        for mod in (chat_mod, misc_mod):
            if hasattr(mod, "load_config"):
                mp.setattr(mod, "load_config", patch)
        mp.setattr(U, "record_usage", lambda *a, **k: None)
        mp.setattr(SA, "add_save", lambda *a, **k: None)
        mp.setattr(chat_mod, "record_usage", lambda *a, **k: None)

        srv, app_port = httpd.create_server("127.0.0.1", 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        prof = tempfile.mkdtemp(prefix="ui_prof_")
        edge_port = 9466
        proc = subprocess.Popen([edge, "--remote-debugging-port=%d" % edge_port,
                                 "--user-data-dir=" + prof, "--no-first-run",
                                 "--no-default-browser-check",
                                 "--disable-background-timer-throttling",
                                 "--disable-renderer-backgrounding",
                                 "--disable-backgrounding-occluded-windows",
                                 "http://127.0.0.1:%d/" % app_port],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        page = None
        for _ in range(80):
            try:
                lst = requests.get("http://127.0.0.1:%d/json/list" % edge_port, timeout=1).json()
                hit = [t for t in lst if (t.get("url") or "").startswith(
                    "http://127.0.0.1:%d" % app_port)]
                if hit:
                    page = hit[0]
                    break
            except Exception:
                pass
            time.sleep(0.25)
        assert page, "UI 没打开"
        cdp = CDP(page["webSocketDebuggerUrl"])
        cdp.send("Runtime.enable")
        cdp.send("Page.enable")
        try:
            cdp.send("Page.bringToFront")
        except Exception:
            pass

        def ev(expr, wait=True, timeout=20):
            r = cdp.send("Runtime.evaluate", {"expression": expr, "returnByValue": True,
                                              "awaitPromise": wait}, timeout=timeout)
            if r.get("exceptionDetails"):
                raise AssertionError((r["exceptionDetails"].get("exception") or {})
                                     .get("description"))
            return r.get("result", {}).get("value")

        for _ in range(80):
            if ev("typeof state !== 'undefined' && !!state.config", wait=False):
                break
            time.sleep(0.25)
        assert ev("!!(state.config && state.config.chat_api_key)", wait=False), "UI 没拿到密钥"

        step1 = ev(r"""
        (async () => {
          await newSession(); const A = state.sessions.current;
          await newSession(); const B = state.sessions.current;
          await selectSession(A);
          document.querySelector('#input').value = 'A 慢慢回答';
          sendMessage();
          await new Promise(r => setTimeout(r, 800));
          return { A, B, liveA: !!state.streams[A] };
        })()
        """)
        A, B = step1["A"], step1["B"]
        assert step1["liveA"] is True

        mid = ev(r"""
        (async () => {
          await selectSession(%s);
          await new Promise(r => setTimeout(r, 300));
          const inB = { stillA: !!state.streams[%s], curStreaming: state.streaming,
                        inDom: !!document.querySelector('.stream-host') };
          document.querySelector('#input').value = 'B 也慢慢回答';
          sendMessage();
          await new Promise(r => setTimeout(r, 500));
          const liveB = { liveB: !!state.streams[%s], aStill: !!state.streams[%s] };
          await selectSession(%s);
          await new Promise(r => setTimeout(r, 300));
          const backA = { backInDom: !!document.querySelector('.stream-host'),
                          backStreaming: state.streaming };
          await selectSession(%s);
          await new Promise(r => setTimeout(r, 200));
          return Object.assign({}, inB, liveB, backA);
        })()
        """ % (json.dumps(B), json.dumps(A), json.dumps(B), json.dumps(A),
               json.dumps(A), json.dumps(B)))
        assert mid["stillA"] is True and mid["curStreaming"] is False
        assert mid["inDom"] is False                       # B 里不该出现 A 的 live host
        assert mid["liveB"] is True and mid["aStill"] is True
        assert mid["backInDom"] is True and mid["backStreaming"] is True

        deadline = time.time() + 40
        while time.time() < deadline:
            if not ev("Object.keys(state.streams).length", wait=False):
                break
            time.sleep(0.3)
        assert ev("Object.keys(state.streams).length", wait=False) == 0

        assert ev("!!state.unread[%s]" % json.dumps(A), wait=False) is True
        assert ev("state.sessions.current === %s" % json.dumps(B), wait=False) is True

        disk = S.load_sessions()
        sa = S.find_session(disk, A) or {"messages": []}
        sb = S.find_session(disk, B) or {"messages": []}
        assert len(sa.get("messages") or []) == 2
        assert len(sb.get("messages") or []) == 2
        assert "慢速回复" in (sa["messages"][-1].get("content") or "")
        assert "慢速回复" in (sb["messages"][-1].get("content") or "")
    finally:
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
            time.sleep(0.4)
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/F"], capture_output=True)
        for s in (srv, fake):
            try:
                s.shutdown()
            except Exception:
                pass
        mp.undo()
