# -*- coding: utf-8 -*-
"""AI 改动审阅：JSON 宽松解析、diff 收集、提交信息生成。"""
from app import ide_review as ir


def test_parse_review_json_with_fence():
    raw = ('```json\n{"summary": "改了 A", "risks": ["风险1"], '
           '"flow": "flowchart TD\\n  A[入口] --> B[出口]"}\n```')
    r = ir.parse_review(raw)
    assert r["summary"] == "改了 A"
    assert r["risks"] == ["风险1"]
    assert r["flow"].startswith("flowchart TD")


def test_parse_review_tolerates_plain_text():
    r = ir.parse_review("模型直接说了几句")
    assert "模型" in r["summary"]
    assert r["risks"] == [] and r["flow"] == ""


def test_parse_review_string_risks_and_flow_fence():
    r = ir.parse_review('{"summary": "s", "risks": "单个风险", '
                        '"flow": "```mermaid\\ngraph TD\\n  A-->B\\n```"}')
    assert r["risks"] == ["单个风险"]
    assert r["flow"].startswith("graph TD")


def test_review_requires_api_key(monkeypatch):
    monkeypatch.setattr(ir, "get_api_conf", lambda kind: ("", "", ""))
    r = ir.review("lbl", "+ x = 1")
    assert r["ok"] is False and "密钥" in r["error"]


def test_review_returns_parsed_result(monkeypatch):
    monkeypatch.setattr(ir, "get_api_conf", lambda kind: ("k", "u", "m"))
    monkeypatch.setattr(ir, "_quick_chat",
                        lambda *a, **kw: '{"summary": "s", "risks": [], "flow": ""}')
    r = ir.review("lbl", "+ x = 1")
    assert r["ok"] is True and r["summary"] == "s"
    assert r["error"] == ""


def test_commit_message_strips_fence(monkeypatch):
    monkeypatch.setattr(ir, "get_api_conf", lambda kind: ("k", "u", "m"))
    monkeypatch.setattr(ir, "_quick_chat",
                        lambda *a, **kw: "```\nfeat: 新增补全\n```")
    msg, why = ir.commit_message("diff")
    assert why == "" and msg == "feat: 新增补全"


def test_commit_message_empty_diff():
    msg, why = ir.commit_message("   ")
    assert msg == "" and "没有" in why


def test_ai_diff_builds_unified_text(monkeypatch, tmp_path):
    monkeypatch.setattr(ir.agent, "ensure_undo_loaded", lambda sid: None)
    monkeypatch.setattr(ir.agent, "resolve_path",
                        lambda sb, rel: str(tmp_path / rel))
    monkeypatch.setattr(ir.agent, "undo_old_content",
                        lambda token, target: "old line\n")
    (tmp_path / "a.py").write_text("new line\n", encoding="utf-8")
    d = ir.ai_diff(str(tmp_path), "s1", [{"path": "a.py", "undo": "t1"}])
    assert d["ok"] is True
    assert d["files"][0]["path"] == "a.py"
    assert "- old line" in d["text"] and "+ new line" in d["text"]
    assert d["label"].startswith("AI 改动")


def test_ai_diff_skips_expired_snapshot(monkeypatch, tmp_path):
    monkeypatch.setattr(ir.agent, "ensure_undo_loaded", lambda sid: None)
    monkeypatch.setattr(ir.agent, "resolve_path",
                        lambda sb, rel: str(tmp_path / rel))
    monkeypatch.setattr(ir.agent, "undo_old_content", lambda token, target: None)
    d = ir.ai_diff(str(tmp_path), "s1", [{"path": "a.py", "undo": "t1"}])
    assert d["ok"] is False and "没有" in d["error"]
    assert d["skipped"] == 1
