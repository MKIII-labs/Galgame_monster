# -*- coding: utf-8 -*-
"""附件清理的安全边界。

背景：live 用例会把 SESSION_FILE 指向临时文件再 create_server；启动清理读到临时
会话后，曾把真实 data/uploads 下的目录整批删掉（2026-11-09 实际发生）。
这里锁住三条防线：显式开关、pytest 自动跳过、未知目录只隔离不删除。
"""
from app import uploads


def _prepare(tmp_path, monkeypatch, sessions):
    up = tmp_path / "uploads"
    up.mkdir()
    monkeypatch.setattr(uploads, "UPLOAD_DIR", str(up))
    monkeypatch.setattr(uploads, "load_sessions", lambda: sessions)
    monkeypatch.delenv("GM_NO_SWEEP", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    return up


def test_sweep_skipped_by_env(tmp_path, monkeypatch):
    up = tmp_path / "uploads"
    d = up / "s_old"
    d.mkdir(parents=True)
    (d / "a.bin").write_bytes(b"x")

    monkeypatch.setattr(uploads, "UPLOAD_DIR", str(up))
    monkeypatch.setattr(uploads, "load_sessions", lambda: {"sessions": []})
    monkeypatch.setenv("GM_NO_SWEEP", "1")

    uploads.sweep_uploads()
    assert d.exists()
    assert (d / "a.bin").exists()


def test_sweep_skipped_under_pytest(tmp_path, monkeypatch):
    up = _prepare(tmp_path, monkeypatch, {"sessions": []})
    d = up / "s_old"
    d.mkdir()
    (d / "a.bin").write_bytes(b"x")
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "x")

    uploads.sweep_uploads()
    assert (d / "a.bin").exists()


def test_sweep_skips_empty_session_list(tmp_path, monkeypatch):
    """读不到会话时宁可留着，也不要把附件全清掉。"""
    up = _prepare(tmp_path, monkeypatch, {"sessions": []})
    d = up / "s_old"
    d.mkdir()
    (d / "a.bin").write_bytes(b"x")

    uploads.sweep_uploads()
    assert (d / "a.bin").exists()


def test_sweep_skips_when_session_file_redirected(tmp_path, monkeypatch):
    """live 用例会改 SESSION_FILE；只要不是默认 data/sessions.json 就不清理。"""
    up = _prepare(tmp_path, monkeypatch, {
        "sessions": [{"id": "s_keep", "messages": []}],
    })
    import app.sessions as session_mod
    monkeypatch.setattr(session_mod, "SESSION_FILE",
                        str(tmp_path / "elsewhere" / "sessions.json"))
    d = up / "s_old"
    d.mkdir()
    (d / "a.bin").write_bytes(b"x")

    uploads.sweep_uploads()
    assert (d / "a.bin").exists()


def test_sweep_quarantines_unknown_dir(tmp_path, monkeypatch):
    """会话列表里没有的目录移进 .orphan，而不是 shutil.rmtree。"""
    up = _prepare(tmp_path, monkeypatch, {
        "sessions": [{"id": "s_keep", "messages": [
            {"role": "user", "content": "hi",
             "images": [{"name": "b.bin", "mime": "image/png", "size": 1}]},
        ]}],
    })
    old = up / "s_old"
    old.mkdir()
    (old / "a.bin").write_bytes(b"x")
    keep = up / "s_keep"
    keep.mkdir()
    (keep / "b.bin").write_bytes(b"y")

    uploads.sweep_uploads()

    assert not old.exists()
    assert (keep / "b.bin").exists()
    batches = [p for p in (up / ".orphan").iterdir() if p.is_dir()]
    assert len(batches) == 1
    assert (batches[0] / "s_old" / "a.bin").exists()


def test_sweep_keeps_referenced_files(tmp_path, monkeypatch):
    """会话目录里被消息引用的文件必须保留，只删没引用的。"""
    up = _prepare(tmp_path, monkeypatch, {
        "sessions": [{"id": "s_keep", "messages": [
            {"role": "user", "content": "hi",
             "images": [{"name": "keep.png", "mime": "image/png", "size": 1}]},
        ]}],
    })
    d = up / "s_keep"
    d.mkdir()
    (d / "keep.png").write_bytes(b"k")
    (d / "stale.png").write_bytes(b"s")

    uploads.sweep_uploads()

    assert (d / "keep.png").exists()
    assert not (d / "stale.png").exists()
