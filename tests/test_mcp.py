# -*- coding: utf-8 -*-
"""MCP 客户端：握手、工具清单、懒注入、调用、超时、熔断、HTTP 传输"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app import agent, mcp

FAKE = r'''
import json, sys, time

TOOLS = [
    {"name": "echo", "description": "回显", "inputSchema": {
        "type": "object", "properties": {"text": {"type": "string"}}}},
    {"name": "read_file", "description": "读文件", "inputSchema": {
        "type": "object", "properties": {"path": {"type": "string"}}}},
    {"name": "slow", "description": "慢工具", "inputSchema": {
        "type": "object", "properties": {}}},
]

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except ValueError:
        continue
    method = msg.get("method")
    rid = msg.get("id")
    params = msg.get("params") or {}
    if method == "initialize":
        out = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
               "serverInfo": {"name": "fake", "version": "1"}}
    elif method == "tools/list":
        out = {"tools": TOOLS}
    elif method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if name == "slow":
            time.sleep(10)
            out = {"content": [{"type": "text", "text": "done"}]}
        elif name == "echo":
            out = {"content": [{"type": "text", "text": "echo:" + str(args.get("text"))}]}
        elif name == "read_file":
            out = {"content": [{"type": "text", "text": "content"}]}
        else:
            out = {"content": [{"type": "text", "text": "unknown"}], "isError": True}
    else:
        out = {}
    if rid is not None:
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": out}) + "\n")
        sys.stdout.flush()
'''


def _cfg(tmp_path, timeout=5):
    script = tmp_path / "fake_mcp_server.py"
    script.write_text(FAKE, encoding="utf-8")
    return {
        "mcp_enabled": True,
        "mcp_servers": [{"name": "fs", "transport": "stdio",
                         "command": sys.executable, "args": [str(script)],
                         "enabled": True, "timeout": timeout,
                         "readonly_tools": ["read_*"]}],
        "mcp_tool_mode": "lazy",
        "mcp_timeout": timeout,
    }


@pytest.fixture(autouse=True)
def _clean():
    mcp.reset()
    yield
    mcp.reset()


@pytest.fixture(autouse=True)
def _no_hooks(monkeypatch):
    from app import hooks
    monkeypatch.setattr(hooks, "enabled", lambda: False)


def test_disabled_returns_nothing(monkeypatch):
    monkeypatch.setattr(mcp, "_cfg", lambda: {"mcp_enabled": False})
    assert mcp.tool_schemas() == []
    assert mcp.servers() == []


def test_exposed_name_is_safe():
    assert mcp.exposed_name("fs", "read.file") == "mcp__fs__read_file"
    assert len(mcp.exposed_name("s" * 60, "t" * 60)) <= 64


def test_lazy_injection(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp, "_cfg", lambda: _cfg(tmp_path))
    names = [s["function"]["name"] for s in mcp.tool_schemas()]
    assert names == ["mcp_list_servers", "mcp_list_tools"]

    text = mcp.list_tools_text("fs")
    assert "mcp__fs__echo" in text
    names = [s["function"]["name"] for s in mcp.tool_schemas()]
    assert "mcp__fs__echo" in names
    assert "mcp__fs__read_file" in names


def test_schema_reuses_input_schema(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp, "_cfg", lambda: _cfg(tmp_path))
    mcp.list_tools_text("fs")
    by_name = {s["function"]["name"]: s["function"] for s in mcp.tool_schemas()}
    echo = by_name["mcp__fs__echo"]
    assert echo["parameters"]["properties"]["text"]["type"] == "string"


def test_call_and_readonly(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp, "_cfg", lambda: _cfg(tmp_path))
    mcp.list_tools_text("fs")
    assert mcp.call("mcp__fs__echo", {"text": "hi"}) == "echo:hi"
    assert mcp.is_readonly_tool("mcp__fs__read_file") is True
    assert mcp.is_readonly_tool("mcp__fs__echo") is False


def test_unknown_tool_returns_error(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp, "_cfg", lambda: _cfg(tmp_path))
    mcp.list_tools_text("fs")
    assert mcp.call("mcp__fs__nope", {}).startswith("❌")


def test_timeout_and_breaker(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp, "_cfg", lambda: _cfg(tmp_path, timeout=1))
    mcp.list_tools_text("fs")
    for _ in range(3):
        assert "超时" in mcp.call("mcp__fs__slow", {}, timeout=1)
    # 连续 3 次失败后熔断：不再启动调用
    assert "连续失败" in mcp.call("mcp__fs__echo", {"text": "x"})


def test_agent_routes_mcp_tool(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp, "_cfg", lambda: _cfg(tmp_path))
    mcp.list_tools_text("fs")
    text, undo, is_write = agent.run_tool(str(tmp_path), "mcp__fs__echo", {"text": "x"})
    assert "echo:x" in text
    assert undo is None and is_write is False


def test_native_schemas_include_mcp(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp, "_cfg", lambda: _cfg(tmp_path))
    names = [s["function"]["name"] for s in agent.native_tool_schemas(agent_on=True)]
    assert "mcp_list_servers" in names
    assert "mcp_list_tools" in names


class _HttpMcp(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            msg = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            msg = {}
        method, rid = msg.get("method"), msg.get("id")
        if method == "initialize":
            out = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                   "serverInfo": {"name": "web", "version": "1"}}
        elif method == "tools/list":
            out = {"tools": [{"name": "ping", "description": "p",
                              "inputSchema": {"type": "object", "properties": {}}}]}
        elif method == "tools/call":
            out = {"content": [{"type": "text", "text": "pong"}]}
        else:
            out = {}
        body = json.dumps({"jsonrpc": "2.0", "id": rid, "result": out}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Mcp-Session-Id", "sess-1")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def test_http_transport(monkeypatch):
    srv = HTTPServer(("127.0.0.1", 0), _HttpMcp)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d/mcp" % srv.server_port
    cfg = {"mcp_enabled": True, "mcp_tool_mode": "lazy", "mcp_timeout": 5,
           "mcp_servers": [{"name": "web", "transport": "http", "url": url,
                            "enabled": True}]}
    monkeypatch.setattr(mcp, "_cfg", lambda: cfg)
    try:
        assert "mcp__web__ping" in mcp.list_tools_text("web")
        assert mcp.call("mcp__web__ping", {}) == "pong"
    finally:
        srv.shutdown()
