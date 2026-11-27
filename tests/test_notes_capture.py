# -*- coding: utf-8 -*-
"""笔记：对话捕获 + 来源索引。"""
import pytest

from app import notes


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(notes, "NOTES_DIR", str(tmp_path / "notes"))
    monkeypatch.setattr(notes, "ANNO_DIR", str(tmp_path / "notes" / ".annotations"))
    monkeypatch.setattr(notes, "LINK_FILE", str(tmp_path / "note_links.json"))
    notes.ensure_notes_dir()
    return tmp_path


def _capture(**kw):
    info = {
        "text": "梯度下降沿着负梯度方向更新参数。",
        "session_id": "s1", "session_title": "机器学习入门",
        "msg_index": 3, "role": "ai",
    }
    info.update(kw)
    return notes.capture_from_chat(info)


def test_capture_inbox_creates_note_with_source_link(sandbox):
    ok, info = _capture(target="inbox", note="和动量法的区别？")
    assert ok and info["deduped"] is False
    md = notes.read_note(info["path"])
    assert md.startswith("# ")
    assert "> 梯度下降沿着负梯度方向更新参数。" in md
    assert "[回到对话 #3](/__gm/chat?session=s1&msg=3)" in md
    assert "我的想法：和动量法的区别？" in md
    items = notes.list_chat_captures("s1")
    assert len(items) == 1
    assert items[0]["note_path"] == info["path"]
    assert items[0]["msg_index"] == 3


def test_capture_current_appends_and_dedupes(sandbox):
    notes.create_note("学习/回归.md", "# 回归\n\n已有内容\n")
    ok, info = _capture(target="current", path="学习/回归.md")
    assert ok and info["deduped"] is False
    md = notes.read_note("学习/回归.md")
    assert md.startswith("# 回归\n\n已有内容")
    assert "## 对话摘录" in md
    ok, again = _capture(target="current", path="学习/回归.md")
    assert ok and again["deduped"] is True
    assert notes.read_note("学习/回归.md").count("## 对话摘录") == 1


def test_capture_new_rejects_existing(sandbox):
    ok, err = _capture(target="new", title="我的笔记")
    assert ok
    assert notes.read_note("我的笔记.md") is not None
    ok, err = _capture(target="new", title="我的笔记")
    assert not ok and "已存在" in err


def test_capture_requires_text(sandbox):
    ok, err = notes.capture_from_chat({"target": "inbox", "text": "  "})
    assert not ok


def test_rename_and_delete_keep_index_in_sync(sandbox):
    notes.create_note("旧笔记.md", "# 旧笔记\n")
    ok, info = _capture(target="current", path="旧笔记.md")
    assert ok
    ok, new_rel = notes.rename_path("旧笔记.md", "新笔记")
    assert ok and new_rel == "新笔记.md"
    assert notes.list_chat_captures("s1")[0]["note_path"] == "新笔记.md"
    notes.create_folder("归档")
    ok, moved = notes.move_path("新笔记.md", "归档")
    assert ok and moved == "归档/新笔记.md"
    assert notes.list_chat_captures("s1")[0]["note_path"] == "归档/新笔记.md"
    assert notes.delete_note("归档/新笔记.md")
    assert notes.list_chat_captures("s1") == []


def test_move_folder_remaps_children(sandbox):
    notes.create_note("项目/a.md", "# A\n")
    ok, _ = _capture(target="current", path="项目/a.md")
    assert ok
    notes.create_folder("归档")
    ok, moved = notes.move_path("项目", "归档")
    assert ok and moved == "归档/项目"
    assert notes.list_chat_captures("s1")[0]["note_path"] == "归档/项目/a.md"
    assert notes.delete_folder("归档/项目")
    assert notes.list_chat_captures("s1") == []
