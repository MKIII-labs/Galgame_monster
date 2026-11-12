# -*- coding: utf-8 -*-
"""OS 命令沙箱：越界路径识别与配置开关。"""
import os

from app import sandbox_win
from app.agent.tools_exec import _path_token, outside_paths


def _box(tmp_path):
    box = tmp_path / "box"
    box.mkdir()
    out = tmp_path / "outside"
    out.mkdir()
    return str(box), str(out)


def test_available_is_off_in_tests():
    assert sandbox_win.available() is False


def test_path_token_forms():
    assert _path_token("C:\\x\\y.txt") == "C:\\x\\y.txt"
    assert _path_token("--out=../x.txt") == "../x.txt"
    assert _path_token("--name=abc") == ""
    assert _path_token("https://example.com") == ""


def test_outside_paths_detects_absolute(tmp_path):
    box, out = _box(tmp_path)
    target = os.path.join(out, "a.txt")
    paths = outside_paths(box, {"argv": ["python", target]})
    assert target in paths
    inside = os.path.join(box, "a.txt")
    assert outside_paths(box, {"argv": ["python", inside]}) == []


def test_outside_paths_detects_dotdot(tmp_path):
    box, out = _box(tmp_path)
    target = os.path.join(out, "a.txt")
    paths = outside_paths(box, {"argv": ["python", "../outside/a.txt"]})
    assert any(os.path.normcase(p) == os.path.normcase(target) for p in paths)


def test_outside_paths_from_shell_text(tmp_path):
    box, _out = _box(tmp_path)
    paths = outside_paths(box, {}, text='type "C:\\Windows\\win.ini"')
    assert any("win.ini" in p for p in paths)


def test_os_sandbox_switch_default_on():
    from app.config import default_config
    assert default_config()["agent_os_sandbox"] is True
