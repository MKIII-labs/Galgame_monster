# -*- coding: utf-8 -*-
"""「停止」是否及时生效：流式读（读线程 + 队列）与命令中断"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app import agent, llm


class _Upstream(BaseHTTPRequestHandler):
    """假的上游 SSE 服务：先发一段，静默 quiet 秒，再发一段。"""
    protocol_version = "HTTP/1.1"
    quiet = 0.0

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def w(obj):
            data = ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode()
            self.wfile.write(("%X\r\n" % len(data)).encode() + data + b"\r\n")
            self.wfile.flush()

        try:
            w({"choices": [{"delta": {"content": "第一段"}}]})
            if self.quiet:
                time.sleep(self.quiet)
            w({"choices": [{"delta": {"content": "第二段"}, "finish_reason": "stop"}]})
            d = b"data: [DONE]\n\n"
            self.wfile.write(("%X\r\n" % len(d)).encode() + d + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except Exception:
            pass

    def log_message(self, *a):
        pass


class _HeartbeatOnly(BaseHTTPRequestHandler):
    """假上游：只发 SSE 注释行（`: ping`），永远不发 data 行。

    这正是「连接活着、正文永远不来」的形态：socket 读超时会被注释行续命，
    只能靠首包看门狗中止。
    """
    protocol_version = "HTTP/1.1"
    quiet = 3.0

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def w(raw):
            self.wfile.write(("%X\r\n" % len(raw)).encode() + raw + b"\r\n")
            self.wfile.flush()

        try:
            deadline = time.time() + self.quiet
            while time.time() < deadline:
                w(b": ping\n\n")
                time.sleep(0.1)
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except Exception:
            pass

    def log_message(self, *a):
        pass


class _EmptyHeartbeat(BaseHTTPRequestHandler):
    """假上游：`data:` 行一直在来，但只有空 delta（没有思考 / 正文）。

    这正是「有数据但界面全静默」的形态：原始数据看门狗会被续命，
    只有空转看门狗能兜住。
    """
    protocol_version = "HTTP/1.1"
    quiet = 3.0

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def w(obj):
            data = ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode()
            self.wfile.write(("%X\r\n" % len(data)).encode() + data + b"\r\n")
            self.wfile.flush()

        try:
            deadline = time.time() + self.quiet
            while time.time() < deadline:
                w({"choices": [{"delta": {}}]})
                time.sleep(0.2)
            w({"choices": [{"delta": {}, "finish_reason": "stop"}]})
            d = b"data: [DONE]\n\n"
            self.wfile.write(("%X\r\n" % len(d)).encode() + d + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except Exception:
            pass

    def log_message(self, *a):
        pass


class _ReasoningUpstream(BaseHTTPRequestHandler):
    """假上游：持续发 reasoning_content，验证「慢思考」不会被误判成空转。"""
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def w(obj):
            data = ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode()
            self.wfile.write(("%X\r\n" % len(data)).encode() + data + b"\r\n")
            self.wfile.flush()

        try:
            for _ in range(8):
                w({"choices": [{"delta": {"reasoning_content": "想"}}]})
                time.sleep(0.12)
            w({"choices": [{"delta": {"content": "答"}, "finish_reason": "stop"}]})
            d = b"data: [DONE]\n\n"
            self.wfile.write(("%X\r\n" % len(d)).encode() + d + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except Exception:
            pass

    def log_message(self, *a):
        pass


class _ToolStreamUpstream(BaseHTTPRequestHandler):
    """假上游：流式发原生 tool_calls 参数，验证 tool_stream 进度事件。"""
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def w(obj):
            data = ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode()
            self.wfile.write(("%X\r\n" % len(data)).encode() + data + b"\r\n")
            self.wfile.flush()

        try:
            w({"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "call_1",
                 "function": {"name": "write_file", "arguments": ""}}]}}]})
            args = json.dumps({"path": "a.txt", "content": "x" * 600})
            for i in range(0, len(args), 100):
                w({"choices": [{"delta": {"tool_calls": [
                    {"index": 0, "function": {"arguments": args[i:i + 100]}}]}}]})
                time.sleep(0.1)
            w({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]})
            d = b"data: [DONE]\n\n"
            self.wfile.write(("%X\r\n" % len(d)).encode() + d + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except Exception:
            pass

    def log_message(self, *a):
        pass


class _AnthropicUpstream(BaseHTTPRequestHandler):
    """假 Anthropic 流：验证 kind 不被队列变量覆盖、message_stop 被识别。"""
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def w(raw):
            self.wfile.write(("%X\r\n" % len(raw)).encode() + raw + b"\r\n")
            self.wfile.flush()

        try:
            w(('event: content_block_delta\n'
               'data: {"type":"content_block_delta","delta":'
               '{"type":"text_delta","text":"你好"}}\n\n'
               'event: message_stop\n'
               'data: {"type":"message_stop"}\n\n').encode("utf-8"))
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except Exception:
            pass

    def log_message(self, *a):
        pass


class _Billing402(BaseHTTPRequestHandler):
    """假上游：直接 402 + Insufficient Balance（DeepSeek 的典型余额不足返回）。"""
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        body = '{"error":{"message":"Insufficient Balance"}}'.encode("utf-8")
        self.send_response(402)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def log_message(self, *a):
        pass


class _BillingSSE(BaseHTTPRequestHandler):
    """假上游：HTTP 200，但 SSE 里发 error（余额不足）。"""
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def w(obj):
            data = ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode()
            self.wfile.write(("%X\r\n" % len(data)).encode() + data + b"\r\n")
            self.wfile.flush()

        try:
            w({"error": {"message": "Insufficient Balance"}})
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except Exception:
            pass

    def log_message(self, *a):
        pass


class _HttpErrorUpstream(BaseHTTPRequestHandler):
    """假上游：固定返回一个 HTTP 错误码，用来验证错误分类。"""
    protocol_version = "HTTP/1.1"
    status = 500
    body = '{"error":{"message":"mock"}}'

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        raw = self.body.encode("utf-8")
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
        self.wfile.flush()

    def log_message(self, *a):
        pass


def _serve(quiet):
    handler = type("H", (_Upstream,), {"quiet": quiet})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def _serve_hb(quiet):
    handler = type("H", (_HeartbeatOnly,), {"quiet": quiet})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def _serve_anthropic():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _AnthropicUpstream)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def _serve_empty(quiet):
    handler = type("H", (_EmptyHeartbeat,), {"quiet": quiet})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def _serve_reasoning():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _ReasoningUpstream)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def _serve_tool_stream():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _ToolStreamUpstream)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def _serve_billing(kind):
    cls = _Billing402 if kind == "http" else _BillingSSE
    srv = ThreadingHTTPServer(("127.0.0.1", 0), cls)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def _serve_http_error(status, body=None):
    handler = type("H", (_HttpErrorUpstream,),
                   {"status": status, "body": body or _HttpErrorUpstream.body})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def test_stop_returns_fast_when_upstream_silent():
    """上游静默时，点停止必须在 1 秒内返回（旧实现要等下一段数据）。"""
    srv, port = _serve(3.0)
    try:
        ev = threading.Event()
        marks = {}

        def stopper():
            time.sleep(0.2)
            marks["t"] = time.time()
            ev.set()

        threading.Thread(target=stopper, daemon=True).start()
        r = llm.stream_chat("k", "http://127.0.0.1:%d/v1/chat/completions" % port, "m",
                            [{"role": "user", "content": "hi"}], stop_event=ev)
        delay = time.time() - marks["t"]
        assert delay < 1.0, "停止延迟 %.2fs，仍然在等上游" % delay
        assert r["stopped"] is True
        assert r["visible"] == "第一段"
    finally:
        srv.shutdown()
        srv.server_close()


def test_normal_stream_returns_all_content():
    """没有停止信号时，流式内容完整、stopped 为 False。"""
    srv, port = _serve(0.0)
    try:
        r = llm.stream_chat("k", "http://127.0.0.1:%d/v1/chat/completions" % port, "m",
                            [{"role": "user", "content": "hi"}])
        assert r["stopped"] is False
        assert r["visible"] == "第一段第二段"
        assert r["finish_reason"] == "stop"
        # 首包延迟：正常流必须能报出来（否则每轮埋点是空的）
        assert isinstance(r["first_ms"], int) and r["first_ms"] >= 0
        # 断流现场：正常流也要有「响应头 / 首 data / 行数」可查
        assert r["diag"]["status"] == 200
        assert r["diag"]["headers_ms"] is not None
        assert r["diag"]["first_data_ms"] is not None
        assert r["diag"]["data_lines"] >= 2
    finally:
        srv.shutdown()
        srv.server_close()


def test_idle_watchdog_aborts_heartbeat_only_upstream():
    """上游只发注释行、永远没有 data：看门狗到点必须中止。

    旧实现只有 socket 读超时（300s），注释行会让它一直续命 ——
    表现就是界面十几分钟一个字都没有，连接却还「活着」。
    """
    srv, port = _serve_hb(3.0)
    try:
        t0 = time.time()
        marks = []
        r = llm.stream_chat("k", "http://127.0.0.1:%d/v1/chat/completions" % port, "m",
                            [{"role": "user", "content": "hi"}],
                            emit=marks.append, idle_timeout=0.6)
        used = time.time() - t0
        assert used < 2.0, "看门狗没生效，等了 %.1fs" % used
        assert r["stream_state"] == "idle"
        assert r["visible"] == ""
        assert any("没有任何响应数据" in (m.get("message") or "") for m in marks)
        # 只收到注释行：说明连接活着、上游不产正文（这是最能区分故障类型的信号）
        assert r["diag"]["status"] == 200
        assert r["diag"]["data_lines"] == 0
        assert r["diag"]["other_lines"] > 0
    finally:
        srv.shutdown()
        srv.server_close()


def test_idle_watchdog_zero_disables():
    """idle_timeout=0 = 不限：不该因为「慢」被误杀（这里用正常流验证不误报）。"""
    srv, port = _serve(0.5)
    try:
        r = llm.stream_chat("k", "http://127.0.0.1:%d/v1/chat/completions" % port, "m",
                            [{"role": "user", "content": "hi"}], idle_timeout=0)
        assert r["stream_state"] == "done"
        assert r["visible"] == "第一段第二段"
    finally:
        srv.shutdown()
        srv.server_close()


def test_stall_watchdog_aborts_empty_heartbeat():
    """data 行一直在来，但只有空 delta：空转看门狗必须中止。"""
    srv, port = _serve_empty(3.0)
    try:
        t0 = time.time()
        marks = []
        r = llm.stream_chat("k", "http://127.0.0.1:%d/v1/chat/completions" % port, "m",
                            [{"role": "user", "content": "hi"}],
                            emit=marks.append, idle_timeout=0, stall_timeout=0.6)
        used = time.time() - t0
        assert used < 2.0, "空转看门狗没生效，等了 %.1fs" % used
        assert r["stream_state"] == "idle"
        assert r["visible"] == ""
        d = r["diag"]
        assert d["meaningful_lines"] == 0
        assert d["empty_lines"] > 0
        assert d["last_progress_age_ms"] >= 500
        assert d["samples"], "应该留下空 chunk 样本，供确认上游发了什么"
        assert any("没有思考或正文" in (m.get("message") or "") for m in marks)
    finally:
        srv.shutdown()
        srv.server_close()


def test_stall_watchdog_zero_disables():
    """stall_timeout=0 = 关：空心跳流正常结束，不误杀。

    空流按 dsh 的 EMPTY_RESPONSE 归类为 empty（不是 done），
    证明它是「正常收尾但零内容」，而不是被看门狗杀掉。
    """
    srv, port = _serve_empty(1.0)
    try:
        r = llm.stream_chat("k", "http://127.0.0.1:%d/v1/chat/completions" % port, "m",
                            [{"role": "user", "content": "hi"}],
                            idle_timeout=0, stall_timeout=0)
        assert r["stream_state"] == "empty"
        assert r["visible"] == ""
        assert r["failed"] is False
    finally:
        srv.shutdown()
        srv.server_close()


def test_native_tool_stream_emits_progress():
    """原生工具调用参数流要发 tool_stream 进度，diag 里要留工具名。"""
    srv, port = _serve_tool_stream()
    try:
        marks = []
        r = llm.stream_chat("k", "http://127.0.0.1:%d/v1/chat/completions" % port, "m",
                            [{"role": "user", "content": "hi"}],
                            emit=marks.append, idle_timeout=0, stall_timeout=0)
        assert r["stream_state"] == "done"
        assert r["tool_calls"] and r["tool_calls"][0]["tool"] == "write_file"
        evs = [m for m in marks if m.get("type") == "tool_stream"]
        assert evs, "应该发出工具调用进度事件"
        assert any(e.get("name") == "write_file" for e in evs)
        assert max(int(e.get("bytes") or 0) for e in evs) > 0
        assert "write_file" in r["diag"]["tool_names"]
        assert r["diag"]["tool_arg_bytes"] >= 600
    finally:
        srv.shutdown()
        srv.server_close()


def test_reasoning_progress_prevents_stall():
    """reasoning 在流 = 有进展，不能把慢思考误判成空转。"""
    srv, port = _serve_reasoning()
    try:
        r = llm.stream_chat("k", "http://127.0.0.1:%d/v1/chat/completions" % port, "m",
                            [{"role": "user", "content": "hi"}],
                            idle_timeout=0, stall_timeout=0.5)
        assert r["stream_state"] == "done"
        assert "想" in r["reasoning"]
        assert r["visible"] == "答"
        assert r["diag"]["meaningful_lines"] >= 8
        assert r["diag"]["reasoning_chars"] >= 8
    finally:
        srv.shutdown()
        srv.server_close()


def test_anthropic_stream_parses_text_and_done():
    """kind 曾被队列元组覆盖：Anthropic 流会解析成空正文，message_stop 也不认。"""
    srv, port = _serve_anthropic()
    try:
        r = llm.stream_chat("k", "http://127.0.0.1:%d" % port, "claude-test",
                            [{"role": "user", "content": "hi"}], kind="anthropic")
        assert r["visible"] == "你好"
        assert r["stream_state"] == "done"
    finally:
        srv.shutdown()
        srv.server_close()


def test_billing_402_is_reported_as_balance():
    """402 / Insufficient Balance 必须单独识别，不能混成普通调用出错。"""
    srv, port = _serve_billing("http")
    try:
        marks = []
        r = llm.stream_chat("k", "http://127.0.0.1:%d/v1/chat/completions" % port, "m",
                            [{"role": "user", "content": "hi"}],
                            emit=marks.append, timeout=(2, 5))
        assert r["stream_state"] == "billing"
        assert r["visible"] == ""
        assert any("余额不足" in (m.get("message") or "") for m in marks)
    finally:
        srv.shutdown()
        srv.server_close()


def test_billing_error_inside_sse_is_reported_as_balance():
    """HTTP 200 + SSE error 也要识别成余额不足，而不是「空转」。"""
    srv, port = _serve_billing("sse")
    try:
        marks = []
        r = llm.stream_chat("k", "http://127.0.0.1:%d/v1/chat/completions" % port, "m",
                            [{"role": "user", "content": "hi"}],
                            emit=marks.append, timeout=(2, 5))
        assert r["stream_state"] == "billing"
        assert any("余额不足" in (m.get("message") or "") for m in marks)
    finally:
        srv.shutdown()
        srv.server_close()


def test_upstream_http_errors_are_classified():
    """401 / 429 / 500 / 400 / 404 要分开提示，不能都写成「调用出错」。"""
    cases = [(401, "auth", "密钥无效"), (429, "rate_limit", "限流"),
             (500, "server", "服务端故障"), (400, "bad_request", "请求被上游拒绝"),
             (404, "not_found", "接口地址不存在")]
    for status, state, word in cases:
        body = '{"error":{"message":"mock-%d"}}' % status
        srv, port = _serve_http_error(status, body)
        try:
            marks = []
            r = llm.stream_chat("k", "http://127.0.0.1:%d/v1/chat/completions" % port,
                                "m", [{"role": "user", "content": "hi"}],
                                emit=marks.append, timeout=(2, 5))
            assert r["stream_state"] == state, (status, r["stream_state"])
            msgs = " ".join(str(m.get("message") or "") for m in marks)
            assert word in msgs, (status, msgs)
            # 上游给的原因要带出来，否则用户只看到状态码
            assert "mock-%d" % status in msgs, (status, msgs)
        finally:
            srv.shutdown()
            srv.server_close()


def test_tools_fallback_second_error_is_also_classified():
    """服务商拒绝 tools 后重发仍失败：二次请求也必须分类，不能退回「调用出错」。"""
    srv, port = _serve_http_error(404, '{"error":{"message":"no such endpoint"}}')
    try:
        marks = []
        r = llm.stream_chat("k", "http://127.0.0.1:%d/v1/chat/completions" % port, "m",
                            [{"role": "user", "content": "hi"}],
                            tools=[{"type": "function",
                                    "function": {"name": "read_file",
                                                 "parameters": {"type": "object"}}}],
                            emit=marks.append, timeout=(2, 5))
        assert r["stream_state"] == "not_found"
        assert any("接口地址不存在" in (m.get("message") or "") for m in marks)
    finally:
        srv.shutdown()
        srv.server_close()


def test_should_retry_transient_failures():
    """瞬时失败可整步重试；用户停止 / 工具调用 / 确定性错误不重试。"""
    from app.api.chat import _should_retry_round

    idle = {"visible": "", "tool_calls": [], "stream_state": "idle"}
    assert _should_retry_round(idle, 0, 2) is True
    assert _should_retry_round(idle, 2, 2) is False          # 次数用尽
    assert _should_retry_round(idle, 0, 0) is False          # 0 = 关闭
    assert _should_retry_round(idle, 0, 2, stopped=True) is False
    # 半截正文默认也允许整步重试（llm 已丢弃失败分片，前端先 rewind）
    assert _should_retry_round({"visible": "半句话", "tool_calls": [],
                                "stream_state": "idle"}, 0, 2) is True
    # GAL 不整步回滚演出：allow_partial=False 时半截正文不重试
    assert _should_retry_round({"visible": "半句话", "tool_calls": [],
                                "stream_state": "idle"}, 0, 2,
                               allow_partial=False) is False
    assert _should_retry_round({"visible": "", "tool_calls": [{"tool": "read_file"}],
                                "stream_state": "idle"}, 0, 2) is False
    assert _should_retry_round({"visible": "", "tool_calls": [],
                                "stream_state": "done"}, 0, 2) is False
    # 余额不足不自动重试：换服务商可以，重发同一条链只会再撞一次 402
    assert _should_retry_round({"visible": "", "tool_calls": [],
                                "stream_state": "billing"}, 0, 2) is False
    assert _should_retry_round({"visible": "", "tool_calls": [],
                                "stream_state": "error"}, 0, 2,
                               billing_hit=True) is False
    assert _should_retry_round({"visible": "", "tool_calls": [],
                                "stream_state": "error"}, 0, 2) is True
    # 确定性错误（重发只会再撞一次同样的错）不自动重试
    for _st in ("auth", "bad_request", "not_found", "context"):
        assert _should_retry_round({"visible": "", "tool_calls": [],
                                    "stream_state": _st}, 0, 2) is False
    # 限流 / 服务端故障 / 网络不通属于「等一会儿可能就好」，允许重试
    for _st in ("rate_limit", "server", "network"):
        assert _should_retry_round({"visible": "", "tool_calls": [],
                                    "stream_state": _st}, 0, 2) is True
    # EMPTY_RESPONSE（正常收尾但零内容）默认重试
    assert _should_retry_round({"visible": "", "tool_calls": [],
                                "stream_state": "empty"}, 0, 2) is True


def test_should_retry_upload_uses_wider_budget():
    """请求体没发完就断：给独立的重试预算；半截正文按整步重试处理。"""
    from app.api.chat import _should_retry_round

    up = {"visible": "", "tool_calls": [], "stream_state": "upload"}
    assert _should_retry_round(up, 2, 2) is False                   # 普通预算用尽
    assert _should_retry_round(up, 2, 2, upload_limit=4) is True    # 上传预算更宽
    assert _should_retry_round(up, 4, 2, upload_limit=4) is False   # 两份都用尽
    assert _should_retry_round(up, 0, 0, upload_limit=4) is False   # 0 = 关，仍是关
    assert _should_retry_round(up, 0, 2, upload_limit=0) is True    # 0 = 跟随普通预算
    assert _should_retry_round(up, 2, 2, upload_limit=0) is False   # 用尽普通预算
    # 半截正文：默认整步重试；GAL 不重试
    assert _should_retry_round({"visible": "半句话", "tool_calls": [],
                                "stream_state": "upload"}, 0, 2,
                               upload_limit=4) is True
    assert _should_retry_round({"visible": "半句话", "tool_calls": [],
                                "stream_state": "upload"}, 0, 2,
                               upload_limit=4, allow_partial=False) is False


def test_auto_continue_break_only_once_and_skips_deterministic():
    """异常中断后自动点一次「继续输出」：每回合一次，确定性错误不点。"""
    from app.api.chat import _should_auto_continue_break

    net = {"visible": "", "tool_calls": [], "stream_state": "network"}
    assert _should_auto_continue_break(net, 0, -1) is True       # 无上限也允许一次
    assert _should_auto_continue_break(net, 0, 2) is True
    assert _should_auto_continue_break(net, 1, -1) is False      # 已经点过
    assert _should_auto_continue_break(net, 0, 0) is False       # 自动续跑关闭
    assert _should_auto_continue_break(net, 0, -1, stopped=True) is False
    assert _should_auto_continue_break(net, 0, -1, billing_hit=True) is False
    for st in ("auth", "bad_request", "not_found", "billing"):
        assert _should_auto_continue_break({"stream_state": st}, 0, -1) is False
    for st in ("network", "idle", "timeout", "eof", "upload", "error",
               "server", "rate_limit", "http", "context", "empty"):
        assert _should_auto_continue_break({"stream_state": st}, 0, -1) is True
    assert _should_auto_continue_break({"stream_state": "done"}, 0, -1) is False


def test_net_alive_detects_closed_port():
    """探活：本地监听端口为 True，关闭后的端口为 False，且不抛异常。"""
    import socket

    from app.api.chat import _net_alive

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert _net_alive("http://127.0.0.1:%d/" % port, timeout=1.0) is True
    finally:
        srv.close()
    assert _net_alive("http://127.0.0.1:%d/" % port, timeout=0.5) is False
    # 地址解析不出来时不应阻断重试
    assert _net_alive("") is True


def test_run_command_aborts_on_stop_event(tmp_path):
    """长命令在收到停止信号后应被终止，而不是等到超时。"""
    ev = threading.Event()
    threading.Timer(0.3, ev.set).start()
    (tmp_path / "slow.py").write_text("import time; time.sleep(10)", encoding="utf-8")
    t0 = time.time()
    text, _, _ = agent.run_tool(str(tmp_path), "run_command",
                                {"argv": ["python", "slow.py"], "timeout": 30},
                                stop_event=ev)
    used = time.time() - t0
    assert used < 5, "命令停止用了 %.1fs" % used
    assert "终止" in text

def test_stream_alive_uses_recent_events(monkeypatch):
    """压缩互斥：ACTIVE_STREAMS 里有残条目、但久无事件的会话要算「已结束」。"""
    from app import store
    from app.api import chat as chat_api

    sid = "pytest_stream_alive"
    monkeypatch.setitem(store.ACTIVE_STREAMS, sid, threading.Event())
    monkeypatch.setitem(chat_api.JOBS, sid, {"sid": sid, "events": [], "seq": 0,
                                             "done": False, "ts": time.time()})
    assert chat_api.stream_alive(sid) is True

    # 超过 idle 没有事件：崩溃 / 断线留下的残条目，不能一直拦着压缩
    chat_api.JOBS[sid]["ts"] = time.time() - 120
    assert chat_api.stream_alive(sid) is False

    # 已经发出 done：明确结束
    chat_api.JOBS[sid]["done"] = True
    chat_api.JOBS[sid]["ts"] = time.time()
    assert chat_api.stream_alive(sid) is False

    monkeypatch.delitem(store.ACTIVE_STREAMS, sid, raising=False)
    assert chat_api.stream_alive(sid) is False


def test_request_stop_falls_back_to_pending(monkeypatch):
    """请求还没登记流时点停止：要留下 pending stop，等登记时消费（长上下文压缩阶段）。"""
    from app import store

    sid = "pytest_pending_stop"
    store.PENDING_STOPS.pop(sid, None)
    monkeypatch.delitem(store.ACTIVE_STREAMS, sid, raising=False)

    r = store.request_stop(sid)
    assert r == {"found": False, "pending": True}
    assert store.consume_stop(sid) is True
    assert store.consume_stop(sid) is False          # 只生效一次

    # 已经有流：直接置事件，不写 pending
    ev = threading.Event()
    monkeypatch.setitem(store.ACTIVE_STREAMS, sid, ev)
    assert store.request_stop(sid) == {"found": True, "pending": False}
    assert ev.is_set()
    assert sid not in store.PENDING_STOPS
    monkeypatch.delitem(store.ACTIVE_STREAMS, sid, raising=False)


def test_pending_stop_expires():
    """pending stop 不能无限期保留：超时后下一次发送不该被误停。"""
    from app import store

    sid = "pytest_pending_expired"
    store.PENDING_STOPS[sid] = time.time() - store.PENDING_STOP_TTL - 1
    try:
        assert store.consume_stop(sid) is False
        assert sid not in store.PENDING_STOPS
    finally:
        store.PENDING_STOPS.pop(sid, None)


def test_abort_chat_stream_releases_lease_and_emits_done(monkeypatch):
    """httpd 异常兜底：释放流租约 + 给 attach 补一条 done，避免永久 busy。"""
    from app import store
    from app.api import chat as chat_api

    sid = "pytest_abort_stream"
    ev = threading.Event()
    monkeypatch.setitem(store.ACTIVE_STREAMS, sid, ev)
    monkeypatch.setitem(chat_api.JOBS, sid, {"sid": sid, "events": [], "seq": 0,
                                             "done": False, "ts": time.time()})
    try:
        chat_api.abort_chat_stream(sid, ev, "boom")
        assert ev.is_set()
        assert sid not in store.ACTIVE_STREAMS
        job = chat_api.JOBS[sid]
        assert job["done"] is True
        last = job["events"][-1]
        assert last["type"] == "done" and last["stopped"] is True
        assert "boom" in last["error"]
    finally:
        monkeypatch.delitem(chat_api.JOBS, sid, raising=False)
        monkeypatch.delitem(store.ACTIVE_STREAMS, sid, raising=False)


def test_live_streams_drops_zombie_entries(monkeypatch):
    """/api/state 不能把「线程早死了」的会话报成生成中（刷新后永久 409 的根源）。"""
    from app import store
    from app.api import chat as chat_api, misc

    live, dead = "pytest_live_stream", "pytest_dead_stream"
    monkeypatch.setitem(store.ACTIVE_STREAMS, live, threading.Event())
    monkeypatch.setitem(store.ACTIVE_STREAMS, dead, threading.Event())
    monkeypatch.setitem(chat_api.JOBS, live, {"sid": live, "events": [], "seq": 0,
                                              "done": False, "ts": time.time()})
    monkeypatch.setitem(chat_api.JOBS, dead, {"sid": dead, "events": [], "seq": 0,
                                              "done": False, "ts": time.time() - 600})
    try:
        got = misc._live_streams()
        assert live in got
        assert dead not in got
        assert dead not in store.ACTIVE_STREAMS       # 顺手清掉僵尸登记
    finally:
        monkeypatch.delitem(chat_api.JOBS, live, raising=False)
        monkeypatch.delitem(chat_api.JOBS, dead, raising=False)
        monkeypatch.delitem(store.ACTIVE_STREAMS, live, raising=False)


def test_reap_orphan_progress_converts_snapshot():
    """没有活跃流却留着 _progress：要转成「可继续」的中断态，而不是假装已完成。"""
    from app.sessions import reap_orphan_progress

    data = {"sessions": [
        {"id": "a", "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "半截", "_progress": True}]},
        {"id": "b", "messages": [
            {"role": "assistant", "content": "跑着", "_progress": True}]},
    ]}
    assert reap_orphan_progress(data, {"b"}) is True
    a = data["sessions"][0]
    assert "_progress" not in a["messages"][-1]
    assert a["messages"][-1]["interrupted"] is True
    assert a["messages"][-1]["end_reason"]["kind"] == "resume"
    assert a["resume_pending"] is True
    # 还在跑的那个会话不能动
    assert data["sessions"][1]["messages"][-1]["_progress"] is True
    assert reap_orphan_progress({"sessions": []}, set()) is False


def test_quick_chat_stop_returns_immediately(monkeypatch):
    """压缩用的非流式调用：停止信号一到就返回，不再等上游。"""
    from app import llm

    ev = threading.Event()
    ev.set()
    t0 = time.time()
    out = llm._quick_chat("k", "http://127.0.0.1:1/v1", "m",
                          [{"role": "user", "content": "hi"}], stop_event=ev)
    assert out == ""
    assert time.time() - t0 < 1.0

    # 请求已发出后点停止：主线程最多等 0.2 秒一次轮询
    ev2 = threading.Event()

    def _slow(*a, **k):
        time.sleep(3)
        raise AssertionError("停止后不该再等这个请求")

    monkeypatch.setattr(llm, "_post_retry", _slow)
    threading.Timer(0.3, ev2.set).start()
    t0 = time.time()
    out2 = llm._quick_chat("k", "http://127.0.0.1:1/v1", "m",
                           [{"role": "user", "content": "hi"}], stop_event=ev2)
    assert out2 == ""
    assert time.time() - t0 < 1.5


def test_compress_history_skips_when_stopped():
    """压缩阶段点停止：不再发摘要请求（长上下文停止慢的主因之一）。"""
    from app import context

    ev = threading.Event()
    ev.set()
    s = {"id": "x", "messages": [{"role": "user", "content": "a"}] * 10}
    assert context.compress_history(s, "", stop_event=ev) is False
