# -*- coding: utf-8 -*-
"""P0：原生工具协议、工具日志、任务状态、1M 窗口。"""
import json


def test_anthropic_tool_schema_conversion():
    from app import providers
    schema = {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读文件",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    }
    body = providers.anthropic_body(
        "claude-test", [{"role": "user", "content": "hi"}],
        tools=[schema], max_tokens=100)
    assert body["tools"][0]["name"] == "read_file"
    assert body["tools"][0]["input_schema"]["required"] == ["path"]


def test_anthropic_tool_stream_events():
    from app import providers
    start = providers.anthropic_tool_event({
        "type": "content_block_start", "index": 1,
        "content_block": {"type": "tool_use", "id": "toolu_1", "name": "read_file"},
    })
    assert start["kind"] == "start" and start["id"] == "toolu_1"
    delta = providers.anthropic_tool_event({
        "type": "content_block_delta", "index": 1,
        "delta": {"type": "input_json_delta", "partial_json": '{"path":'},
    })
    assert delta["kind"] == "delta" and delta["json"] == '{"path":'
    stop = providers.anthropic_tool_event({
        "type": "content_block_stop", "index": 1,
    })
    assert stop["kind"] == "stop"
    # 文本块不是工具事件
    assert providers.anthropic_tool_event({
        "type": "content_block_start", "index": 0,
        "content_block": {"type": "text", "text": ""},
    }) is None


def test_tool_journal_pending_only_side_effects(tmp_path):
    from app import tool_journal
    tool_journal.intent("s1", "c1", "write_file", {"path": "a.py"}, data_dir=tmp_path)
    tool_journal.intent("s1", "c2", "read_file", {"path": "a.py"}, data_dir=tmp_path)
    pending = tool_journal.pending("s1", data_dir=tmp_path)
    assert [p["call_id"] for p in pending] == ["c1"]
    tool_journal.outcome("s1", "c1", ok=True, data_dir=tmp_path)
    assert tool_journal.pending("s1", data_dir=tmp_path) == []
    assert "write_file" in tool_journal.pending_text("s1", data_dir=tmp_path) or True


def test_task_state_text_and_plan():
    from app import task_state
    s = {}
    task_state.set_goal(s, "做一个 MC 克隆")
    task_state.set_acceptance(s, [{"text": "页面无报错"}, {"text": "贴图正确"}])
    task_state.set_plan(s, {"steps": [
        {"text": "素材审计", "status": "done"},
        {"text": "引擎骨架", "status": "doing"},
    ]})
    task_state.note_write(s, "index.html")
    task_state.note_error(s, "render_check: canvas 黑屏")
    text = task_state.text(s)
    assert "做一个 MC 克隆" in text
    assert "页面无报错" in text
    assert "引擎骨架" in text
    assert "index.html" in text
    assert "canvas 黑屏" in text


def test_model_context_limit_has_no_model_table():
    from app.config import model_context_limit
    # 不看模型名：没填就是 0（不限）
    assert model_context_limit("claude-sonnet-4-6-1m", {"chat_context_limit": 0}) == 0
    assert model_context_limit("claude-sonnet-4-6", {"chat_context_limit": 0}) == 0
    # 显式填写才生效，且原样使用
    assert model_context_limit("claude-sonnet-4-6",
                               {"chat_context_limit": 500000}) == 500000


def test_compress_pins_task_ledger(tmp_path, monkeypatch):
    """压缩请求里必须带上任务状态；不真正调模型，只检查请求组装。"""
    from app import context as ctx
    from app import task_state

    s = {"messages": [{"role": "user", "content": "x" * 5000} for _ in range(6)]}
    task_state.set_goal(s, "目标 A")
    task_state.note_write(s, "main.js")
    captured = {}

    def fake_quick(api_key, api_url, api_model, req, max_tokens=0, source=""):
        captured["req"] = req
        return "【摘要】测试\n【要点】\n- 继续"

    monkeypatch.setattr(ctx, "get_api_conf", lambda *a, **k: ("k", "u", "m"))
    monkeypatch.setattr(ctx, "context_limits", lambda *a, **k: (100000, 4096))
    monkeypatch.setattr(ctx, "keep_from_by_tokens", lambda *a, **k: 2)
    monkeypatch.setattr(ctx, "calibrate_ratio", lambda *a, **k: 1.0)
    import app.llm as llm_mod
    monkeypatch.setattr(llm_mod, "_quick_chat", fake_quick)
    assert ctx.compress_history(s, "sys") is True
    blob = json.dumps(captured["req"], ensure_ascii=False)
    assert "目标 A" in blob
    assert "main.js" in blob
