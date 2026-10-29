# -*- coding: utf-8 -*-
"""产物预览：文本读取与路径越界拦截"""
import pytest

from app import db, task_engine
from app.api import work as api_work


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db.configure(tmp_path / "test.db")
    yield


class FakeHandler:
    def __init__(self):
        self.out = None
        self.code = 200

    def _json(self, obj, code=200):
        self.out = obj
        self.code = code


def _artifact(tmp_path, path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir(exist_ok=True)
    # 新逻辑只登记「真实存在的文件」（目录 / 已删除文件不算产物），
    # 所以测试先落一个空文件，再模拟任务写文件成功后的登记。
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    task = task_engine.create_task("产出", ["写文件"], session_id="s1")
    task_engine._add_artifact(task["id"], {"tool": "write_file",
                                           "args": {"path": str(path)}})
    art = task_engine.list_artifacts(task["id"])[0]
    return sandbox, art


def test_text_preview(tmp_path, monkeypatch):
    sandbox, _ = _artifact(tmp_path, tmp_path / "sandbox" / "note.md")
    (sandbox / "note.md").write_text("你好，产物", encoding="utf-8")
    art = task_engine.get_artifact(task_engine.list_artifacts()[0]["id"])
    monkeypatch.setattr(api_work, "load_sessions",
                        lambda: {"sessions": [{"id": "s1", "sandbox": str(sandbox)}]})
    monkeypatch.setattr(api_work, "find_session",
                        lambda data, sid: data["sessions"][0] if sid == "s1" else None)
    h = FakeHandler()
    api_work._preview_artifact(h, art["id"])
    assert h.out["ok"] is True
    assert h.out["preview"]["kind"] == "text"
    assert "你好，产物" in h.out["preview"]["text"]


def test_path_outside_sandbox_rejected(tmp_path, monkeypatch):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    _artifact(tmp_path, outside)          # 绝对路径在沙箱外
    art = task_engine.list_artifacts()[0]
    sandbox = tmp_path / "sandbox"
    monkeypatch.setattr(api_work, "load_sessions",
                        lambda: {"sessions": [{"id": "s1", "sandbox": str(sandbox)}]})
    monkeypatch.setattr(api_work, "find_session",
                        lambda data, sid: data["sessions"][0] if sid == "s1" else None)
    h = FakeHandler()
    api_work._preview_artifact(h, art["id"])
    assert h.code == 403
    assert "允许范围" in h.out["error"]
