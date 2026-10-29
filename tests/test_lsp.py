# -*- coding: utf-8 -*-
"""LSP 客户端：帧格式、诊断过滤、定义/引用、重命名方案。

用「假语言服务器」验证协议层，不依赖 pyright / Node / 网络。
"""
import io
import json
import sys

import pytest

from app import lsp

FAKE = r'''
import json
import sys


def read_frame(stream):
    headers = {}
    while True:
        line = stream.readline()
        if not line:
            return None
        text = line.decode("utf-8", "replace").strip()
        if not text:
            break
        k, v = text.split(":", 1)
        headers[k.strip().lower()] = v.strip()
    n = int(headers.get("content-length") or 0)
    if n <= 0:
        return None
    return json.loads(stream.read(n).decode("utf-8"))


def send(obj):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(("Content-Length: %d\r\n\r\n" % len(body)).encode("ascii"))
    sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()


while True:
    msg = read_frame(sys.stdin.buffer)
    if msg is None:
        break
    method = msg.get("method")
    rid = msg.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": rid, "result": {"capabilities": {}}})
    elif method == "textDocument/didOpen":
        uri = msg["params"]["textDocument"]["uri"]
        send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics",
              "params": {"uri": uri, "version": 1, "diagnostics": [
                  {"range": {"start": {"line": 1, "character": 0},
                             "end": {"line": 1, "character": 5}},
                   "severity": 1, "message": "类型不匹配", "code": "reportAssignmentType"},
                  {"range": {"start": {"line": 2, "character": 0},
                             "end": {"line": 2, "character": 3}},
                   "severity": 1, "message": "缺库噪音", "code": "reportMissingImports"}]}})
    elif method == "textDocument/definition":
        uri = msg["params"]["textDocument"]["uri"]
        send({"jsonrpc": "2.0", "id": rid, "result": [
            {"uri": uri, "range": {"start": {"line": 0, "character": 4},
                                   "end": {"line": 0, "character": 7}}}]})
    elif method == "textDocument/references":
        uri = msg["params"]["textDocument"]["uri"]
        send({"jsonrpc": "2.0", "id": rid, "result": [
            {"uri": uri, "range": {"start": {"line": 0, "character": 4},
                                   "end": {"line": 0, "character": 7}}},
            {"uri": uri, "range": {"start": {"line": 3, "character": 0},
                                   "end": {"line": 3, "character": 3}}}]})
    elif method == "textDocument/rename":
        uri = msg["params"]["textDocument"]["uri"]
        send({"jsonrpc": "2.0", "id": rid, "result": {"changes": {uri: [
            {"range": {"start": {"line": 0, "character": 4},
                       "end": {"line": 0, "character": 7}},
             "newText": "bar"}]}}})
    elif rid is not None:
        send({"jsonrpc": "2.0", "id": rid, "result": None})
'''


@pytest.fixture()
def fake(monkeypatch, tmp_path):
    server = tmp_path / "fake_ls.py"
    server.write_text(FAKE, encoding="utf-8")
    monkeypatch.setattr(lsp, "_launcher", lambda: [sys.executable, str(server)])
    yield tmp_path
    lsp.shutdown()


def test_read_frame_and_uri():
    buf = io.BytesIO(b"Content-Length: 7\r\n\r\n{\"a\":1}")
    assert lsp._read_frame(buf) == {"a": 1}
    assert lsp._path_of("file:///D%3A/a/b.py") == "D:\\a\\b.py"
    assert lsp._path_of("https://x/y.py") == ""


def test_diagnostics_filters_noise(fake):
    f = fake / "a.py"
    f.write_text("def foo():\n    x = 1\n    return x\n", encoding="utf-8")
    text, undo = lsp.diagnostics(str(fake), str(f))
    assert undo is None
    assert "1 个 error" in text
    assert "类型不匹配" in text and "【a.py】" in text and "2:1 错误" in text
    assert "缺库噪音" not in text                 # reportMissingImports 被过滤
    assert "忽略 1 条缺库提示" in text


def test_definition(fake):
    f = fake / "a.py"
    f.write_text("def foo():\n    return 1\n", encoding="utf-8")
    text, _ = lsp.definition(str(fake), str(f), 1, "foo")
    assert "定义" in text
    assert "a.py:1:5" in text


def test_references(fake):
    f = fake / "a.py"
    f.write_text("def foo():\n    pass\n\nfoo()\n", encoding="utf-8")
    text, _ = lsp.references(str(fake), str(f), 1, "foo")
    assert "引用 2 处" in text
    assert "a.py:1:5" in text and "a.py:4:1" in text


def test_rename_plan(fake):
    f = fake / "a.py"
    f.write_text("def foo():\n    return 1\n", encoding="utf-8")
    files, note = lsp.rename_plan(str(fake), str(f), 1, "foo", "bar")
    assert files and len(files) == 1
    assert files[0]["old"].startswith("def foo")
    assert files[0]["new"].startswith("def bar")
    assert "重命名「foo」→「bar」" in note


def test_session_reuses_process(fake):
    f = fake / "a.py"
    f.write_text("def foo():\n    return 1\n", encoding="utf-8")
    s1 = lsp.session(str(fake))
    s2 = lsp.session(str(fake))
    assert s1 is s2
    assert s1.alive()


def test_unavailable_when_no_launcher(monkeypatch, tmp_path):
    monkeypatch.setattr(lsp, "_launcher", lambda: [])
    with pytest.raises(lsp.Unavailable):
        lsp.session(str(tmp_path))
    assert str(tmp_path) not in lsp._SESSIONS
