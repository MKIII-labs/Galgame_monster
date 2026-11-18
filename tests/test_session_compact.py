# -*- coding: utf-8 -*-
"""会话存储压缩 / 二进制附件拒绝。"""
from app import docparse
from app import session_compact as sc


def _doc_msg(text, name="a.txt", with_doc=True, with_doc_text=True):
    meta = {"id": "att_1", "name": name, "file": name, "text": text, "chars": len(text)}
    msg = {"role": "user", "content": "看看这个", "docs": [meta]}
    if with_doc:
        msg["doc"] = dict(meta)
    if with_doc_text:
        msg["doc_text"] = text
    return msg


def test_compact_dedupes_three_copies():
    text = "正文" * 2000
    msg = _doc_msg(text)
    changed, freed, notes = sc.compact_message(msg)
    assert changed
    assert msg["docs"][0]["text"] == text          # 唯一保留的一份
    assert "text" not in msg["doc"]                # 只留元信息
    assert "doc_text" not in msg
    assert freed >= len(text) * 2
    assert any("重复" in n for n in notes)

    # 幂等：再跑一次不应该再变
    assert sc.compact_message(msg)[0] is False


def test_compact_removes_binary_attachment_text():
    blob = "\x00\x00\x00\x18ftypmp42" + "\x00\x01\x02" * 4000
    msg = _doc_msg(blob, name="recording.mp4")
    changed, freed, notes = sc.compact_message(msg)
    assert changed
    assert "text" not in msg["docs"][0]
    assert "text" not in msg["doc"]
    assert "doc_text" not in msg
    assert msg["docs"][0]["warn"]
    assert freed >= len(blob)
    assert any("二进制" in n for n in notes)


def test_compact_caps_old_doc_text(monkeypatch):
    monkeypatch.setattr(sc, "MAX_INLINE", 100)
    text = "长" * 250
    msg = {"role": "user", "content": "旧消息", "doc_text": text}
    changed, freed, _ = sc.compact_message(msg)
    assert changed
    assert len(msg["doc_text"]) == 100
    assert freed == 150


def test_compact_keeps_small_legacy_doc_text():
    msg = {"role": "user", "content": "旧消息", "doc_text": "短文"}
    assert sc.compact_message(msg)[0] is False
    assert msg["doc_text"] == "短文"


def test_compact_data_counts_sessions():
    data = {"sessions": [
        {"id": "s1", "messages": [_doc_msg("正文" * 1000)]},
        {"id": "s2", "messages": [{"role": "assistant", "content": "ok"}]},
    ]}
    stats = sc.compact_data(data)
    assert stats["sessions"] == 1
    assert stats["messages"] == 1
    assert stats["freed_chars"] > 0


def test_docparse_rejects_media_and_binary():
    text, warn = docparse.extract("recording.mp4", b"\x00\x00\x00\x18ftypmp42")
    assert text is None
    assert "视频" in warn or "音频" in warn

    text, warn = docparse.extract("unknown.bin", b"\x00\x01\x02\x03" * 200)
    assert text is None
    assert "二进制" in warn

    text, warn = docparse.extract("a.txt", "你好，世界".encode("utf-8"))
    assert text == "你好，世界"
