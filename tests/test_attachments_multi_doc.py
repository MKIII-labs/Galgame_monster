# -*- coding: utf-8 -*-
"""多文档附件：上下文拼装 / 附件登记 / 清理引用 / 重试与文本预览。"""
from app import attachments, context, uploads
from app.api import sessions as sessions_api


class _H:
    """最小 handler 替身（只实现附件接口用到的几个方法）"""

    def __init__(self, body):
        self._incoming = body
        self.calls = []

    def _body(self):
        return self._incoming

    def _json(self, obj, code=200):
        self.calls.append((code, obj))
        return obj

    def last(self):
        return self.calls[-1]


def _msg():
    return {
        "role": "user",
        "content": "看看这两份文件",
        "docs": [
            {"id": "att_a", "name": "a.pdf", "file": "u1.pdf", "size": 10,
             "mime": "application/pdf", "chars": 5, "text": "第一份正文"},
            {"id": "att_b", "name": "b.docx", "file": "u2.docx", "size": 20,
             "mime": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
             "chars": 0, "error": "docx 解析失败：坏文件"},
        ],
        "doc": {"id": "att_a", "name": "a.pdf", "file": "u1.pdf", "size": 10},
        "doc_text": "第一份正文",
    }


def test_msg_text_includes_every_doc():
    text = context._msg_text(_msg())
    assert "【附件：a.pdf】" in text and "第一份正文" in text
    assert "【附件：b.docx】" in text and "未能读取" in text


def test_msg_text_legacy_single_doc_still_works():
    legacy = {"role": "user", "content": "",
              "doc": {"name": "old.txt"}, "doc_text": "旧正文"}
    text = context._msg_text(legacy)
    assert "旧正文" in text and "old.txt" in text


def test_attachments_lists_all_docs():
    items = attachments.from_message(_msg())
    docs = [it for it in items if it["kind"] == attachments.DOC]
    assert [d["name"] for d in docs] == ["a.pdf", "b.docx"]
    assert docs[0]["text"] == "第一份正文"
    assert docs[1]["error"]


def test_referenced_files_include_all_docs():
    names = uploads._referenced_files(_msg())
    assert "u1.pdf" in names and "u2.docx" in names


def test_attach_text_returns_extracted(monkeypatch, tmp_path):
    p = tmp_path / "u1.txt"
    p.write_text("hello 世界", encoding="utf-8")
    monkeypatch.setattr(sessions_api, "_upload_path", lambda sid, name: str(p))
    h = _H({"session_id": "s1", "name": "u1.txt"})
    sessions_api._api_attach_text(h)
    code, obj = h.last()
    assert code == 200 and obj["ok"]
    assert "hello 世界" in obj["text"]


def test_attach_text_reports_missing_file(monkeypatch):
    monkeypatch.setattr(sessions_api, "_upload_path", lambda sid, name: None)
    h = _H({"session_id": "s1", "name": "nope.txt"})
    sessions_api._api_attach_text(h)
    assert h.last()[0] == 404


def test_attach_reparse_updates_doc_meta(monkeypatch, tmp_path):
    p = tmp_path / "u1.pdf"
    p.write_bytes(b"%PDF-1.4 fake")
    meta = {"name": "a.pdf", "file": "u1.pdf", "error": "之前失败"}
    msg = {"role": "user", "content": "", "docs": [meta], "doc": meta}
    data = {"sessions": [{"id": "s1", "messages": [msg]}]}
    monkeypatch.setattr(sessions_api, "load_sessions", lambda: data)
    monkeypatch.setattr(sessions_api, "find_session", lambda d, sid: d["sessions"][0])
    monkeypatch.setattr(sessions_api, "save_sessions", lambda d: None)
    monkeypatch.setattr(sessions_api, "_upload_path", lambda sid, name: str(p))
    monkeypatch.setattr(sessions_api.docparse, "extract", lambda name, raw, **kw: ("重试后的正文", ""))

    h = _H({"session_id": "s1", "index": 0, "doc_index": 0})
    sessions_api._api_attach_reparse(h)
    code, obj = h.last()
    assert code == 200 and obj["ok"] and obj["chars"] == 6
    assert meta["chars"] == 6 and meta["text"] == "重试后的正文"
    assert "error" not in meta
    assert msg["doc_text"] == "重试后的正文"
