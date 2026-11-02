# -*- coding: utf-8 -*-
"""分组头「在文件管理器中打开」接口：只允许会话用过的沙箱目录。"""
import os

import app.api.misc as misc


class _H:
    def __init__(self, body):
        self._b = body
        self.code = 200
        self.payload = None

    def _body(self):
        return self._b

    def _json(self, obj, code=200):
        self.payload = obj
        self.code = code
        return obj


def test_open_known_folder(tmp_path, monkeypatch):
    d = tmp_path / "proj"
    d.mkdir()
    monkeypatch.setattr(misc, "load_sessions", lambda: {"sessions": [{"sandbox": str(d)}]})
    called = []
    monkeypatch.setattr(os, "startfile", lambda p: called.append(p), raising=False)
    h = _H({"path": str(d)})
    misc._api_open_folder(h)
    assert h.payload["ok"] is True
    assert called and os.path.samefile(called[0], str(d))


def test_open_rejects_unknown_and_missing(tmp_path, monkeypatch):
    known = tmp_path / "proj"
    known.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setattr(misc, "load_sessions", lambda: {"sessions": [{"sandbox": str(known)}]})
    called = []
    monkeypatch.setattr(os, "startfile", lambda p: called.append(p), raising=False)

    h = _H({"path": str(other)})                 # 存在但不是会话用过的目录
    misc._api_open_folder(h)
    assert h.code == 403

    h = _H({"path": str(tmp_path / "nope")})     # 不存在
    misc._api_open_folder(h)
    assert h.code == 404

    h = _H({})                                   # 空路径
    misc._api_open_folder(h)
    assert h.code == 400
    assert called == []
