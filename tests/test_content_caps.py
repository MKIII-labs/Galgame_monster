# -*- coding: utf-8 -*-
"""内容能力：笔记读写、联网搜索、文档解析"""
import pytest

from app import content_caps, notes as noteslib


@pytest.fixture(autouse=True)
def notes_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(noteslib, "NOTES_DIR", str(tmp_path / "notes"))
    yield


def test_note_create_read_append():
    r = content_caps._note_create({"path": "学习/索引", "content": "第一行"}, {})
    assert "已创建" in r and "学习/索引.md" in r
    r = content_caps._note_append({"path": "学习/索引.md", "content": "第二行"}, {})
    assert "已追加" in r
    text = content_caps._note_read({"path": "学习/索引"}, {})
    assert "第一行" in text and "第二行" in text
    hits = content_caps._note_search({"query": "第二行"}, {})
    assert "学习/索引.md" in hits


def test_note_append_creates_when_missing():
    r = content_caps._note_append({"path": "随手记", "content": "临时内容"}, {})
    assert "已创建" in r
    assert "临时内容" in content_caps._note_read({"path": "随手记.md"}, {})


def test_search_web_formats_results(monkeypatch):
    monkeypatch.setattr(content_caps.search, "get_search_conf",
                        lambda: ("bocha", "http://x", "key", 5))
    monkeypatch.setattr(content_caps.search, "search_web",
                        lambda q, p, u, k, c: [{
                            "title": "标题", "url": "http://a", "snippet": "摘要"}])
    out = content_caps._search_web({"query": "测试"}, {})
    assert "标题" in out and "http://a" in out and "摘要" in out


def test_search_web_requires_key(monkeypatch):
    monkeypatch.setattr(content_caps.search, "get_search_conf",
                        lambda: ("bocha", "http://x", "", 5))
    assert content_caps._search_web({"query": "测试"}, {}).startswith("❌")


def test_doc_read_uses_sandbox(tmp_path, monkeypatch):
    (tmp_path / "a.md").write_text("文档正文", encoding="utf-8")
    monkeypatch.setattr(content_caps, "_sandbox_of", lambda ctx: str(tmp_path))
    out = content_caps._doc_read({"path": "a.md"}, {})
    assert "文档正文" in out


def test_doc_read_rejects_outside_path(tmp_path, monkeypatch):
    monkeypatch.setattr(content_caps, "_sandbox_of", lambda ctx: str(tmp_path))
    out = content_caps._doc_read({"path": "../secret.md"}, {})
    assert out.startswith("❌")
