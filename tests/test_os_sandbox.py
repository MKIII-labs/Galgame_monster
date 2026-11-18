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


def test_os_sandbox_elevate_default_off():
    """默认不在命令链路里弹 UAC：授权失败就退回普通执行。"""
    from app.config import default_config
    assert default_config()["agent_sandbox_elevate"] is False


def _no_elevate_env(monkeypatch):
    import app.config as cfg
    monkeypatch.setattr(cfg, "load_config", lambda: {"agent_sandbox_elevate": False})
    monkeypatch.setattr(sandbox_win, "sid_string", lambda: "S-1-15-2-1")
    monkeypatch.setattr(sandbox_win, "_grant_now", lambda *a, **k: False)
    monkeypatch.setattr(sandbox_win, "_fix_electron_install", lambda *a, **k: None)
    monkeypatch.setattr(sandbox_win, "_elevate_allowed", lambda: False)
    elevated = []
    monkeypatch.setattr(sandbox_win, "_grant_elevated",
                        lambda *a, **k: elevated.append(a) or True)
    sandbox_win._acl_state.update({"failed": False, "reason": "", "ok_roots": set()})
    return elevated


def _reset_acl_state():
    sandbox_win._acl_state.update({"failed": False, "reason": "", "ok_roots": set()})


def test_ensure_acls_does_not_elevate_by_default(tmp_path, monkeypatch):
    elevated = _no_elevate_env(monkeypatch)
    try:
        ok, why = sandbox_win.ensure_acls(str(tmp_path))
        assert ok is False and "UAC" in why
        assert elevated == []                  # 不提权 → 不弹 Windows 安全提示
    finally:
        _reset_acl_state()


def test_grant_extra_does_not_elevate_by_default(tmp_path, monkeypatch):
    elevated = _no_elevate_env(monkeypatch)
    try:
        assert sandbox_win.grant_extra([str(tmp_path)]) is False
        assert elevated == []
    finally:
        _reset_acl_state()


def test_elevate_can_be_enabled_explicitly(tmp_path, monkeypatch):
    elevated = _no_elevate_env(monkeypatch)
    try:
        sandbox_win.ensure_acls(str(tmp_path), elevate=True)
        assert elevated                       # 显式允许时才走提权
    finally:
        _reset_acl_state()


def test_warn_survives_missing_stderr(monkeypatch):
    """pythonw 下 sys.stderr 为 None：沙箱告警不能把整条命令变成工具失败。"""
    import sys
    from app.agent import tools_exec
    monkeypatch.setattr(sys, "stderr", None)
    tools_exec._warn("sandbox fallback\n")     # 不应抛 AttributeError
