# -*- coding: utf-8 -*-
"""输入框 `#` 引用的服务端展开（app/fileref.py）。"""
from app import fileref


def test_from_client_file_and_code(tmp_path):
    (tmp_path / "a.py").write_text("line1\nline2\nline3\n", encoding="utf-8")
    refs, ws = fileref.from_client(
        [{"type": "file", "path": "a.py"},
         {"type": "code", "path": "a.py", "start": 2, "end": 3}],
        str(tmp_path))
    assert ws is False and len(refs) == 2
    assert refs[0]["path"] == "a.py" and "line1" in refs[0]["text"]
    assert refs[1]["start"] == 2 and refs[1]["end"] == 3
    assert refs[1]["text"] == "line2\nline3"


def test_from_client_code_falls_back_to_snapshot(tmp_path):
    """文件读不到（或已被删除）时用前端选区快照，不能把绝对行号再切一遍。"""
    refs, _ws = fileref.from_client(
        [{"type": "code", "path": "missing.py", "start": 3, "end": 4,
          "text": "snapshot"}], str(tmp_path))
    assert refs and refs[0]["text"] == "snapshot"
    assert refs[0]["start"] == 3


def test_from_client_folder(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("A\n", encoding="utf-8")
    (tmp_path / "src" / "b.txt").write_text("B\n", encoding="utf-8")
    (tmp_path / "src" / "bin.dat").write_bytes(b"\x00\x01")
    refs, _ws = fileref.from_client([{"type": "folder", "path": "src"}],
                                    str(tmp_path))
    assert {r["path"] for r in refs} == {"src/a.py", "src/b.txt"}


def test_from_client_terminal_problems_and_workspace():
    refs, ws = fileref.from_client(
        [{"type": "workspace"},
         {"type": "terminal", "text": "npm ERR!\n"},
         {"type": "problems"}],
        "C:/not-real",
        problems=[{"path": "a.py", "line": 3, "severity": "error", "message": "boom"}])
    assert ws is True
    assert any(r["path"] == "终端输出" and "npm ERR!" in r["text"] for r in refs)
    assert any(r["path"] == "IDE 问题" and "a.py:3" in r["text"] for r in refs)


def test_from_client_memory():
    refs, _ws = fileref.from_client(
        [{"type": "memory", "id": "m1"}], "",
        memory_nodes=[{"id": "m1", "title": "偏好", "content": "喜欢简短回答"}])
    assert refs and refs[0]["path"] == "记忆：偏好"
    assert "简短" in refs[0]["text"]


def test_from_client_doc(monkeypatch):
    from app import knowledge
    monkeypatch.setattr(knowledge, "load_kb", lambda kid: {
        "nodes": [{"id": "n1", "title": "术语", "content": "GAL=视觉小说"}]})
    refs, _ws = fileref.from_client([{"type": "doc", "kb": "k1", "node": "n1"}], "")
    assert refs and refs[0]["path"] == "知识库：术语"
    assert "视觉小说" in refs[0]["text"]


def test_from_client_ignores_unknown_types():
    refs, ws = fileref.from_client(
        [{"type": "nope"}, None, {"type": "file", "path": ""}], "")
    assert refs == [] and ws is False


def test_kb_nodes_filters_by_title(monkeypatch):
    import app.api.kb as kb_api
    monkeypatch.setattr(kb_api, "load_kb_index", lambda: {"kbs": [{"id": "k1"}]})
    monkeypatch.setattr(kb_api, "load_kb", lambda kid: {"nodes": [
        {"id": "n1", "title": "术语", "category": "词汇"},
        {"id": "n2", "title": "人物"},
    ]})

    class _H:
        def __init__(self):
            self.out = None

        def _body(self):
            return {"q": "术"}

        def _json(self, obj, code=200):
            self.out = obj
            return obj

    out = kb_api._api_kb_nodes(_H())
    assert [x["title"] for x in out["items"]] == ["术语"]
    assert out["items"][0]["kb"] == "k1" and out["items"][0]["node"] == "n1"
