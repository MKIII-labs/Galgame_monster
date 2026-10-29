# -*- coding: utf-8 -*-
"""附件清理的安全开关：测试隔离 sessions 时必须能跳过真实 uploads 清理。"""
import os

from app import uploads


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
