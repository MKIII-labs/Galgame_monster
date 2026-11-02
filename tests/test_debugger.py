# -*- coding: utf-8 -*-
"""调试会话：路径护栏、API 注册、Node 真实调试（有 Node 时）。"""
import shutil

import pytest

from app import debugger


def test_rejects_program_outside_sandbox(tmp_path):
    with pytest.raises(debugger.DebugError):
        debugger.start("t1", str(tmp_path), "../outside.js")


def test_api_routes_registered():
    from app.registry import get
    from app.api import debugger as api
    assert get("POST", "/api/debug/start") is api._api_debug_start
    assert get("POST", "/api/debug/state") is api._api_debug_state
    assert get("POST", "/api/debug/action") is api._api_debug_action
    assert get("POST", "/api/debug/stop") is api._api_debug_stop


@pytest.mark.skipif(not shutil.which("node"), reason="需要 Node")
def test_node_debug_session(tmp_path):
    (tmp_path / "a.js").write_text(
        "const x = 1;\nfunction add(a, b) { const s = a + b; return s; }\n"
        "console.log(add(1, 2));\n", encoding="utf-8")
    sid = "debug-node-test"
    try:
        res = debugger.start(sid, str(tmp_path), "a.js", stop_on_entry=True)
        assert res["language"] == "node"
        st = debugger.state(sid)
        assert st["running"] is True and st["paused"] is True
        assert st["frames"]
        ev = debugger.evaluate(sid, "1 + 2")
        assert ev["value"] == "3"
        debugger.action(sid, "continue")
    finally:
        debugger.stop(sid)
    assert debugger.state(sid)["running"] is False


@pytest.mark.skipif(not shutil.which("node"), reason="需要 Node")
def test_node_breakpoint_and_step(tmp_path):
    (tmp_path / "b.js").write_text(
        "let total = 0;\nfor (let i = 0; i < 2; i++) {\n  total += i;\n}\n"
        "console.log(total);\n", encoding="utf-8")
    sid = "debug-node-bp"
    try:
        debugger.start(sid, str(tmp_path), "b.js",
                       breakpoints={"b.js": [3]}, stop_on_entry=True)
        assert debugger.state(sid)["paused"] is True
        debugger.action(sid, "next")
        assert debugger.state(sid)["paused"] is True
    finally:
        debugger.stop(sid)
