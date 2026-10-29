# -*- coding: utf-8 -*-
"""写后快照恢复：recover_file / list_steps / undo_step 的丢失检测。"""
import pytest

from app import agent


@pytest.fixture(autouse=True)
def _clean_state():
    with agent._undo_lock:
        agent._UNDO.clear()
    agent._LOADED_SIDS.clear()
    yield
    with agent._undo_lock:
        agent._UNDO.clear()
    agent._LOADED_SIDS.clear()


def _add(tmp_path, sid="s_recover", name="a.txt", after=b"hello", ts=1):
    p = tmp_path / name
    token = "a_%s" % name.replace(".", "_")
    agent._UNDO[token] = {
        "sid": sid, "ts": ts, "label": "write_file", "undone": False,
        "ops": [{"kind": "restore", "path": str(p), "existed": False,
                 "data": b"", "after": after, "after_existed": True}],
    }
    return token, p


def test_recover_file_by_path(tmp_path):
    sid = "s_recover"
    _token, p = _add(tmp_path, sid)
    text, undo = agent._t_recover_file(str(tmp_path), {"path": "a.txt"}, {"sid": sid})
    assert "已从写后快照恢复 1 个文件" in text
    assert p.read_bytes() == b"hello"
    assert undo is None


def test_recover_file_refuses_existing_without_force(tmp_path):
    sid = "s_recover"
    _token, p = _add(tmp_path, sid)
    p.write_bytes(b"new")
    text, _ = agent._t_recover_file(str(tmp_path), {"path": "a.txt"}, {"sid": sid})
    assert "已存在且不同" in text
    assert p.read_bytes() == b"new"

    text, _ = agent._t_recover_file(
        str(tmp_path), {"path": "a.txt", "force": True}, {"sid": sid})
    assert "已从写后快照恢复 1 个文件" in text
    assert p.read_bytes() == b"hello"


def test_recover_file_dry_run_and_list(tmp_path):
    sid = "s_recover"
    _add(tmp_path, sid)
    text, _ = agent._t_recover_file(str(tmp_path), {}, {"sid": sid})
    assert "可恢复的写后快照" in text and "a.txt" in text

    text, _ = agent._t_recover_file(
        str(tmp_path), {"path": "a.txt", "dry_run": True}, {"sid": sid})
    assert "将恢复 1 个文件" in text
    assert not (tmp_path / "a.txt").exists()


def test_recover_file_token_restores_all(tmp_path):
    sid = "s_recover"
    token, p1 = _add(tmp_path, sid, "a.txt", b"a")
    p2 = tmp_path / "b.txt"
    agent._UNDO[token]["ops"].append({
        "kind": "restore", "path": str(p2), "existed": False, "data": b"",
        "after": b"b", "after_existed": True})
    text, _ = agent._t_recover_file(str(tmp_path), {"token": token}, {"sid": sid})
    assert "恢复 2 个文件" in text
    assert p1.read_bytes() == b"a" and p2.read_bytes() == b"b"


def test_recover_file_directory_prefix(tmp_path):
    sid = "s_recover"
    _add(tmp_path, sid, "sub/a.txt", b"a")
    _add(tmp_path, sid, "sub/b.txt", b"b", ts=2)
    text, _ = agent._t_recover_file(
        str(tmp_path), {"path": "sub", "dry_run": True}, {"sid": sid})
    assert "将恢复 2 个文件" in text
    assert "sub/a.txt" in text and "sub/b.txt" in text


def test_recover_file_rejects_outside(tmp_path):
    sid = "s_recover"
    outside = tmp_path.parent / "evil.txt"
    agent._UNDO["a_out"] = {
        "sid": sid, "ts": 1, "label": "write_file", "undone": False,
        "ops": [{"kind": "restore", "path": str(outside), "existed": False,
                 "data": b"", "after": b"evil", "after_existed": True}]}
    text, _ = agent._t_recover_file(str(tmp_path), {"path": "../evil.txt"}, {"sid": sid})
    assert "不在沙箱内" in text
    assert not outside.exists()


def test_list_steps_marks_missing(tmp_path):
    sid = "s_recover"
    _add(tmp_path, sid)
    text, _ = agent._t_list_steps(str(tmp_path), {}, {"sid": sid})
    assert "文件已丢失" in text and "recover_file" in text


def test_missing_written_files(tmp_path):
    sid = "s_recover"
    _add(tmp_path, sid, "a.txt", b"a")
    assert agent.missing_written_files(sid, str(tmp_path)) == ["a.txt"]
    (tmp_path / "a.txt").write_bytes(b"a")
    assert agent.missing_written_files(sid, str(tmp_path)) == []


def test_undo_step_refuses_missing(tmp_path):
    sid = "s_recover"
    token, p = _add(tmp_path, sid)
    text, _ = agent._t_undo_step(str(tmp_path), {"token": token}, {"sid": sid})
    assert "recover_file" in text
    assert not p.exists()

    text2, _ = agent._t_recover_file(str(tmp_path), {"path": "a.txt"}, {"sid": sid})
    assert "已从写后快照恢复 1 个文件" in text2
    assert p.read_bytes() == b"hello"
