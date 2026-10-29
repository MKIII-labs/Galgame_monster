# -*- coding: utf-8 -*-
"""see_image：让模型真的看一眼沙箱里的图片。"""
import base64

from app import agent

# 1×1 透明 PNG，用于构造真实可读的图片文件
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def test_see_image_registered():
    assert "see_image" in agent.TOOLS
    assert agent.normalize_tool_name("look") == "see_image"
    assert agent.normalize_tool_name("view_image") == "see_image"
    names = [t["function"]["name"] for t in agent.native_tool_schemas(
        agent_on=True, search_ready=False, cap_domains=set())]
    assert "see_image" in names


def test_see_image_rejects_non_image(tmp_path):
    (tmp_path / "a.txt").write_text("hi", encoding="utf-8")
    text, undo = agent._t_see_image(str(tmp_path), {"path": "a.txt"})
    assert text.startswith("❌")
    assert "只支持图片" in text
    assert undo is None


def test_see_image_rejects_missing_file(tmp_path):
    text, _ = agent._t_see_image(str(tmp_path), {"path": "nope.png"})
    assert text.startswith("❌")
    assert "不存在" in text


def test_see_image_passes_base64_and_question(tmp_path, monkeypatch):
    (tmp_path / "a.png").write_bytes(PNG_1PX)
    seen = {}

    def fake(mime, b64, question="", prefer=None):
        seen.update({"mime": mime, "b64": b64, "q": question})
        return "一只白猫"

    monkeypatch.setattr("app.imaging.recognize_image_fallback", fake)
    text, undo = agent._t_see_image(str(tmp_path), {"path": "a.png", "question": "是什么"})
    assert text == "一只白猫"
    assert seen["mime"] == "image/png"
    assert seen["q"] == "是什么"
    assert len(seen["b64"]) > 0
    assert undo is None
