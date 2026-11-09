# -*- coding: utf-8 -*-
"""笔记划词问答落库：问答也进统一 annotations 表（kind=qa）。"""
from app import anno
from app import notes as noteslib


def test_notes_qa_save_maps_to_qa_annotation(monkeypatch):
    captured = {}

    def fake_save(info):
        captured.update(info)
        return "a_test"

    monkeypatch.setattr(anno, "save_annotation", fake_save)
    aid = noteslib.save_annotation("demo.md", {
        "quote": "选中片段",
        "prefix": "前文",
        "suffix": "后文",
        "nth": 2,
        "question": "这段在讲什么？",
        "answer": "在讲定义。",
    })
    assert aid == "a_test"
    assert captured["doc_key"] == "notes:demo.md"
    assert captured["kind"] == "qa"
    assert captured["question"] == "这段在讲什么？"
    assert captured["answer"] == "在讲定义。"
    assert "问：" in captured["comment"] and "答：" in captured["comment"]
    assert captured["anchor"] == {
        "quote": "选中片段", "nth": 2, "prefix": "前文", "suffix": "后文"}


def test_notes_plain_note_still_note_kind(monkeypatch):
    captured = {}
    monkeypatch.setattr(anno, "save_annotation", lambda info: captured.update(info) or "a2")
    noteslib.save_annotation("demo.md", {"quote": "片段", "comment": "自己的想法"})
    assert captured["kind"] == "note"
    assert captured["question"] == "" and captured["answer"] == ""
