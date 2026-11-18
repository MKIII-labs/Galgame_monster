# -*- coding: utf-8 -*-
"""修改历史消息时的附件变更：沿用 / 删除 / 新增。"""
import base64

import pytest

from app import uploads
from app.api.chat_attach import reconcile_edit

IMG = {"mime": "image/png", "data": base64.b64encode(b"png-bytes").decode("ascii")}
DOC = {"name": "新笔记.md", "mime": "text/markdown",
       "data": base64.b64encode("新正文".encode("utf-8")).decode("ascii")}


@pytest.fixture(autouse=True)
def _tmp_upload_dir(tmp_path, monkeypatch):
    """附件落盘指到临时目录，别写真实 data/uploads。"""
    monkeypatch.setattr(uploads, "UPLOAD_DIR", str(tmp_path))
    return tmp_path


def _msg():
    return {
        "role": "user", "content": "原话",
        "images": [{"name": "a.png"}, {"name": "b.png"}],
        "image": {"name": "a.png"},
        "vision": "旧描述",
        "docs": [{"name": "甲.pdf", "file": "f1.pdf", "text": "甲正文"},
                 {"name": "乙.pdf", "file": "f2.pdf", "text": "乙正文"}],
        "doc": {"name": "甲.pdf", "file": "f1.pdf"},
        "doc_text": "甲正文",
    }


def test_edit_keeps_only_reused_attachments():
    msg = _msg()
    changed, items = reconcile_edit(msg, "s1", {"a.png"}, {"f2.pdf"})
    assert changed is True
    assert [x["name"] for x in msg["images"]] == ["a.png"]
    assert msg["image"]["name"] == "a.png"
    assert "vision" not in msg                     # 图片变了，旧识图作废
    assert [x["file"] for x in msg["docs"]] == ["f2.pdf"]
    assert msg["docs"][0]["text"] == "乙正文"       # 沿用的文档正文不重解析
    assert msg["doc"]["file"] == "f2.pdf" and "text" not in msg["doc"]
    assert "doc_text" not in msg                   # 兼容字段不能残留旧正文
    assert items == []


def test_edit_without_reuse_list_leaves_attachments_alone():
    """旧客户端不传 reuse_*：附件原样保留，行为不变。"""
    msg = _msg()
    changed, items = reconcile_edit(msg, "s1")
    assert changed is False and items == []
    assert len(msg["images"]) == 2 and len(msg["docs"]) == 2
    assert msg["vision"] == "旧描述" and msg["doc_text"] == "甲正文"


def test_edit_can_remove_all_attachments():
    msg = _msg()
    changed, _items = reconcile_edit(msg, "s1", set(), set())
    assert changed is True
    for k in ("images", "image", "docs", "doc", "doc_text", "vision"):
        assert k not in msg


def test_edit_appends_new_attachments():
    msg = _msg()
    changed, items = reconcile_edit(msg, "s1", {"a.png"}, {"f1.pdf"},
                                    new_imgs=[IMG], new_docs=[DOC])
    assert changed is True
    assert [x["name"] for x in msg["images"]][0] == "a.png"
    assert msg["images"][-1]["size"] == len(b"png-bytes")
    assert msg["images"][-1]["mime"] == "image/png"
    # 新文档排在沿用的后面，进度事件要按消息里的真实下标回报
    assert [x["file"] for x in msg["docs"]][:1] == ["f1.pdf"]
    assert msg["docs"][-1]["name"] == "新笔记.md"
    assert items and items[0]["idx"] == 1 and items[0]["name"] == "新笔记.md"
    assert items[0]["meta"]["size"] == len("新正文".encode("utf-8"))
    assert "doc_text" not in msg


def test_edited_message_context_keeps_only_kept_doc_text():
    """修改后真正发给模型的文本：沿用的文档正文还在，被删掉的不再出现。"""
    from app.context import _msg_text

    msg = _msg()
    reconcile_edit(msg, "s1", {"a.png"}, {"f2.pdf"})
    text = _msg_text(msg)
    assert "乙正文" in text and "甲正文" not in text
    assert "乙.pdf" in text

    reconcile_edit(msg, "s1", set(), set())
    text2 = _msg_text(msg)
    assert "乙正文" not in text2 and "甲正文" not in text2
