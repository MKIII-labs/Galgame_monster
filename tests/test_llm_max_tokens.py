# -*- coding: utf-8 -*-
"""输出上限：显式 max_tokens 是否真的进了请求体"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app import llm


class _Capture(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    body = {}

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        type(self).body = json.loads(raw.decode("utf-8"))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        data = b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n'
        self.wfile.write(("%X\r\n" % len(data)).encode() + data + b"\r\n")
        d = b"data: [DONE]\n\n"
        self.wfile.write(("%X\r\n" % len(d)).encode() + d + b"\r\n")
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def log_message(self, *a):
        pass


def _run(**kw):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Capture)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        llm.stream_chat("k", "http://127.0.0.1:%d/v1/chat/completions" % srv.server_address[1],
                        "m", [{"role": "user", "content": "hi"}], **kw)
    finally:
        srv.shutdown()
        srv.server_close()
    return _Capture.body


def test_max_tokens_sent_when_positive():
    body = _run(max_tokens=8192)
    assert body.get("max_tokens") == 8192


def test_max_tokens_omitted_when_zero():
    body = _run(max_tokens=0)
    assert "max_tokens" not in body


def test_max_tokens_omitted_by_default():
    body = _run()
    assert "max_tokens" not in body
