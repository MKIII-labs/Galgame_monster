# -*- coding: utf-8 -*-
"""插件目录：只有显式开启才加载，失败不影响其他能力。"""
import sys

import pytest

from app import capabilities as caps

PLUGIN_SRC = '''# -*- coding: utf-8 -*-
from app.capabilities import capability


def register():
    @capability("plug.hello", "插件测试能力", {"name": "str"}, chat=True)
    def hello(args, ctx=None):
        return "hi " + str(args.get("name") or "")
'''


@pytest.fixture(autouse=True)
def _reset():
    caps._PLUGINS["loaded"] = False
    caps._CAPS.pop("plug.hello", None)
    sys.modules.pop("gm_plugin_hello", None)
    yield
    caps._PLUGINS["loaded"] = False
    caps._CAPS.pop("plug.hello", None)
    sys.modules.pop("gm_plugin_hello", None)


def _write_plugin(tmp_path):
    (tmp_path / "hello.py").write_text(PLUGIN_SRC, encoding="utf-8")


def test_disabled_by_default(tmp_path, monkeypatch):
    _write_plugin(tmp_path)
    monkeypatch.setattr(caps, "PLUGIN_DIR", str(tmp_path))
    monkeypatch.setattr("app.config.load_config", lambda: {})
    assert "plug.hello" not in caps.ids()


def test_enabled_loads_register(tmp_path, monkeypatch):
    _write_plugin(tmp_path)
    monkeypatch.setattr(caps, "PLUGIN_DIR", str(tmp_path))
    monkeypatch.setattr("app.config.load_config", lambda: {"plugins_enabled": True})
    assert "plug.hello" in caps.ids()
    text, is_write = caps.invoke("plug.hello", {"name": "世界"})
    assert text == "hi 世界"
    assert is_write is False


def test_broken_plugin_does_not_break_others(tmp_path, monkeypatch):
    (tmp_path / "bad.py").write_text("def register(:\n", encoding="utf-8")
    _write_plugin(tmp_path)
    monkeypatch.setattr(caps, "PLUGIN_DIR", str(tmp_path))
    monkeypatch.setattr("app.config.load_config", lambda: {"plugins_enabled": True})
    assert "plug.hello" in caps.ids()          # 坏插件不影响好插件


def test_underscore_file_ignored(tmp_path, monkeypatch):
    (tmp_path / "_private.py").write_text(PLUGIN_SRC, encoding="utf-8")
    monkeypatch.setattr(caps, "PLUGIN_DIR", str(tmp_path))
    monkeypatch.setattr("app.config.load_config", lambda: {"plugins_enabled": True})
    assert "plug.hello" not in caps.ids()
