# -*- coding: utf-8 -*-
"""轨迹：事件账本投影、请求头去重、工具配对、分页、子代理展开与回退。"""
import json

import pytest

from app import store, trajectory


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATA_DIR", str(tmp_path))
    trajectory._TURN_LAST.clear()
    trajectory._HEADER_LAST.clear()
    trajectory._CFG_CACHE.update({"at": 0.0, "enabled": True,
                                  "cap": trajectory.DEFAULT_FIELD_CAP})
    return tmp_path


def _cells(view):
    out = []
    for t in view["turns"]:
        for g in t["groups"]:
            out.extend(g["cells"])
    return out


def test_build_turns_and_cell_fields(tmp_path):
    sid = "s1"
    turn = trajectory.new_turn(sid)
    trajectory.user(sid, turn, "帮我改一下代码", {"kind": "human"})
    trajectory.context(sid, turn, "memory", "记忆条目 A", "记忆")
    trajectory.step_start(sid, turn, 1)
    trajectory.header(sid, turn, 1, model="m1", provider="p",
                      system="SYSTEM", tools=[{"type": "function",
                                               "function": {"name": "read_file"}}])
    trajectory.assistant(sid, turn, 1, content="好的", reasoning="先看看",
                         usage={"prompt_tokens": 10, "completion_tokens": 4},
                         timing={"start": 1.0, "first": 1.2, "end": 2.0,
                                 "first_ms": 200})
    trajectory.tool(sid, turn, 1, {
        "tool": "read_file", "args": {"path": "a.py"}, "result": "内容",
        "ok": True, "ms": 12, "trace_id": "tr1"})
    trajectory.todo(sid, turn, {"revision": 2, "steps": [
        {"title": "A", "status": "done"}, {"title": "B", "status": "pending"}]})
    trajectory.turn_end(sid, turn, "completed")

    view = trajectory.build(sid)
    assert view["source"] == "events"
    assert len(view["turns"]) == 1
    kinds = [c["kind"] for c in _cells(view)]
    assert kinds[:6] == ["user", "context", "system", "context", "reasoning", "message"]
    assert "tool" in kinds and "context" in kinds
    part = next(c for c in _cells(view)
                if (c.get("source") or {}).get("kind") == "prompt_part")
    assert part["inputDetail"] == "SYSTEM"
    cells = _cells(view)
    msg = next(c for c in cells if c["kind"] == "message")
    assert msg["input"] == 10 and msg["output"] == 4
    assert abs(msg["timeSeconds"] - 1.0) < 1e-6
    assert msg["ttftMs"] == 200
    tool = next(c for c in cells if c["kind"] == "tool")
    assert tool["traceId"] == "tr1" and tool["timeSeconds"] == 0.012
    assert tool["schemaDetail"]  # 从 request/header 的工具 schema 取到
    assert tool["resultChars"] == 2 and tool["errorText"] == ""
    sys_cell = next(c for c in cells if c["kind"] == "system")
    assert sys_cell["model"] == "m1" and sys_cell["provider"] == "p"
    assert sys_cell["toolCount"] == 1
    # 系统提示分段能指回账本里对应的那一行（前端据此跳转）
    part_rec = next(p["recordId"] for p in sys_cell["parts"] if p["recordId"])
    assert part_rec == part["recordId"]
    plan = next(c for c in cells if c["kind"] == "context" and c.get("source", {}).get("kind") == "plan")
    assert "2 步" in plan["text"] and "1 完成" in plan["text"]


def test_sections_split_markdown_and_bracket_headings():
    text = ("【全局规则（必须遵守）】\n# 规则\n1. 甲\n\n"
            "## 可用 Skill（专业流程包）\n- a\n- b\n\n"
            "## 可用工具\n- read_file\n")
    secs = trajectory.sections(text)
    labels = [s[0] for s in secs]
    assert labels[0] == "全局规则（必须遵守）"
    assert "可用 Skill（专业流程包）" in labels
    assert "可用工具" in labels
    # 偏移能切回原文（投影靠它取分段全文）
    for label, start, end in secs:
        assert text[start:end].strip()
    assert trajectory.sections("") == []


def test_header_parts_change_detection_and_diff():
    sid = "sp"
    turn = trajectory.new_turn(sid)
    first = "【全局规则】\n甲\n\n## 可用 Skill\n- 写代码\n"
    trajectory.header(sid, turn, 1, model="m", system=first,
                      tools=[{"function": {"name": "x"}}])
    second = "【全局规则】\n甲\n\n## 可用 Skill\n- 写代码\n- 画图\n"
    trajectory.header(sid, turn, 2, model="m", system=second,
                      tools=[{"function": {"name": "x"}}])
    events = [e for e in trajectory._iter_events(sid)
              if e["type"] == "request/header"]
    assert len(events) == 2
    assert events[0]["data"]["reason"] == "initial"
    assert events[1]["data"]["reason"] == "change"
    changed = [p["label"] for p in events[1]["data"]["parts"] if p["changed"]]
    assert changed == ["可用 Skill"]

    cells = _cells(trajectory.build(sid))
    systems = [c for c in cells if c["kind"] == "system"]
    assert systems[0]["text"].startswith("初始系统提示")
    assert "可用 Skill" in systems[1]["text"]          # 只有这一段变了
    assert "画图" in (systems[1].get("promptDiff") or "")
    parts = [c for c in cells if (c.get("source") or {}).get("kind") == "prompt_part"]
    assert any("画图" in c["inputDetail"] for c in parts)
    assert any("（变化）" in c["text"] for c in parts)


def test_header_accepts_multi_system_messages():
    sid = "sm"
    turn = trajectory.new_turn(sid)
    trajectory.header(sid, turn, 1, model="m", systems=[
        {"text": "【当前时间】\n2026-11-01"}, {"text": "## 可用工具\n- a"}])
    header = next(e for e in trajectory._iter_events(sid)
                  if e["type"] == "request/header")["data"]["header"]
    assert "当前时间" in header["system"] and "可用工具" in header["system"]


def test_header_dedupes_identical_system_and_tools():
    sid = "s2"
    turn = trajectory.new_turn(sid)
    for step in (1, 2):
        trajectory.step_start(sid, turn, step)
        trajectory.header(sid, turn, step, model="m", provider="p",
                          system="SAME", tools=[{"name": "x"}])
    cells = _cells(trajectory.build(sid))
    assert sum(1 for c in cells if c["kind"] == "system") == 1


def test_tool_detail_uses_trace(monkeypatch):
    sid = "s3"
    turn = trajectory.new_turn(sid)
    trajectory.tool(sid, turn, 1, {
        "tool": "read_file", "args": {"path": "a"}, "result": "短",
        "ok": True, "ms": 1, "trace_id": "tid"})
    monkeypatch.setattr("app.tool_trace.load",
                        lambda sid, tid: {"result": "完整内容", "tool": "read_file"})
    res = trajectory.tool_detail(sid, trace_id="tid")
    assert res["ok"] and res["result"] == "完整内容" and res["source"] == "trace"


def test_pagination_by_turns():
    sid = "s4"
    for i in range(3):
        turn = trajectory.new_turn(sid)
        trajectory.user(sid, turn, "q%d" % i, {"kind": "human"})
    view = trajectory.build(sid, limit_turns=2)
    assert [t["turn"] for t in view["turns"]] == [2, 3]
    assert view["has_more"] is True
    view2 = trajectory.build(sid, before=3, limit_turns=2)
    assert [t["turn"] for t in view2["turns"]] == [1, 2]
    assert view2["has_more"] is False


def test_subagent_expands_from_batch_file(tmp_path):
    sid = "s5"
    batch = "ex_test"
    d = tmp_path / "subagents"
    d.mkdir()
    rec = {"items": {"a1": {
        "role": "reviewer", "ask": "看看 a.py",
        "messages": [{"role": "user", "content": "任务"},
                     {"role": "assistant", "content": "结论 OK"}]}}}
    (d / (batch + ".jsonl")).write_text(
        json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8")
    turn = trajectory.new_turn(sid)
    trajectory.tool(sid, turn, 1, {
        "tool": "delegate", "args": {"batch": batch,
                                     "agents": [{"agent": "reviewer", "task": "看看"}]},
        "result": "批次号 %s" % batch, "ok": True, "ms": 5})
    cells = _cells(trajectory.build(sid))
    subs = [c for c in cells if c["kind"] == "subtool"]
    assert len(subs) == 1
    assert "reviewer" in subs[0]["text"]
    assert "结论 OK" in subs[0]["outputDetail"]


def test_fallback_from_session(monkeypatch):
    sid = "s6"
    session = {"id": sid, "messages": [
        {"role": "user", "content": "问题"},
        {"role": "assistant", "content": "答案",
         "blocks": [{"kind": "reasoning", "text": "想一想"},
                    {"kind": "text"},
                    {"kind": "tool", "i": 0}],
         "tools": [{"tool": "read_file", "args": {"path": "a.py"},
                    "result": "内容", "ok": True, "ms": 3, "trace_id": "t1"}],
         "usage": {"prompt_tokens": 5, "completion_tokens": 2},
         "timing": {"total_ms": 1000}},
    ]}
    import app.sessions as sessions
    monkeypatch.setattr(sessions, "load_sessions",
                        lambda: {"sessions": [session], "current": sid})
    monkeypatch.setattr(sessions, "find_session",
                        lambda data, wanted: session if wanted == sid else None)
    view = trajectory.build(sid)
    assert view["source"] == "session"
    kinds = [c["kind"] for c in _cells(view)]
    assert kinds == ["user", "reasoning", "message", "tool"]


def test_export_contains_events_and_view():
    sid = "s7"
    turn = trajectory.new_turn(sid)
    trajectory.user(sid, turn, "hi", {"kind": "human"})
    data = trajectory.export(sid)
    assert data["sid"] == sid and data["version"] == 1
    assert data["events"] and data["view"]["turns"]


def test_revision_tracks_appends():
    sid = "s9"
    before = trajectory.revision(sid)
    turn = trajectory.new_turn(sid)
    trajectory.user(sid, turn, "hi", {"kind": "human"})
    after = trajectory.revision(sid)
    assert before["cur"][0] == 0
    assert after["cur"][0] > 0 and after["cur"] != before["cur"]


def test_api_routes_registered_and_respond(monkeypatch):
    from app.registry import get
    from app.api import trajectory as api_mod

    assert get("POST", "/api/trajectory") is api_mod._api_trajectory
    assert get("POST", "/api/trajectory/rev") is api_mod._api_trajectory_rev
    assert get("POST", "/api/trajectory/tool") is api_mod._api_trajectory_tool
    assert get("POST", "/api/trajectory/export") is api_mod._api_trajectory_export

    sid = "s8"
    turn = trajectory.new_turn(sid)
    trajectory.user(sid, turn, "你好", {"kind": "human"})

    class H:
        def _body(self):
            return {"session_id": sid, "limit": 10}

        def _json(self, obj, code=200):
            self.obj = obj
            self.code = code
            return obj

    h = H()
    api_mod._api_trajectory(h)
    assert h.obj["ok"] is True
    assert h.obj["view"]["turns"][0]["groups"][0]["cells"][0]["kind"] == "user"
    assert h.obj["rev"]["cur"][0] > 0

