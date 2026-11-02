# -*- coding: utf-8 -*-
"""全量备份：导出、预检、恢复。"""
import json
import os
import sqlite3

import pytest

from app import backup


def _make_data(root):
    (root / "knowledge").mkdir(parents=True)
    (root / "notes").mkdir()
    (root / "uploads" / "s1").mkdir(parents=True)
    (root / "saves").mkdir()
    (root / "sessions.json").write_text(
        json.dumps({"sessions": [{"id": "s1", "title": "测试"}]}), encoding="utf-8")
    (root / "tasks.json").write_text(
        json.dumps({"items": [{"id": "t1"}]}), encoding="utf-8")
    (root / "knowledge" / "index.json").write_text(
        json.dumps({"kbs": [{"id": "kb1"}]}), encoding="utf-8")
    (root / "knowledge" / "kb1.json").write_text(
        json.dumps({"id": "kb1", "nodes": []}), encoding="utf-8")
    (root / "notes" / "a.md").write_text("# A\n", encoding="utf-8")
    (root / "uploads" / "s1" / "a.png").write_bytes(b"PNG")
    (root / "saves" / "s1.json").write_text("{}", encoding="utf-8")
    db_file = root / "assistant.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute("CREATE TABLE sample (x INTEGER)")
    conn.execute("INSERT INTO sample VALUES (7)")
    conn.commit()
    conn.close()
    return db_file


def test_export_inspect_restore_roundtrip(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    db_file = _make_data(root)

    blob, name = backup.export_backup(str(root), str(db_file))
    assert name.endswith(".zip") and blob

    info = backup.inspect_backup(blob)
    assert info["summary"]["sessions"] == 1
    assert info["summary"]["knowledge"] == 1
    assert info["summary"]["notes"] == 1
    assert info["summary"]["uploads"] == 1
    assert info["summary"]["saves"] == 1

    # 改坏当前数据，再恢复
    (root / "sessions.json").write_text("{}", encoding="utf-8")
    (root / "notes" / "a.md").write_text("broken", encoding="utf-8")
    conn = sqlite3.connect(str(db_file))
    conn.execute("DELETE FROM sample")
    conn.commit()
    conn.close()

    result = backup.restore_backup(blob, str(root), str(db_file), make_safety=False)
    assert result["ok"] is True
    data = json.loads((root / "sessions.json").read_text(encoding="utf-8"))
    assert data["sessions"][0]["id"] == "s1"
    assert (root / "notes" / "a.md").read_text(encoding="utf-8") == "# A\n"
    conn = sqlite3.connect(str(db_file))
    assert conn.execute("SELECT x FROM sample").fetchone()[0] == 7
    conn.close()


def test_inspect_rejects_invalid_zip():
    with pytest.raises(ValueError):
        backup.inspect_backup(b"not a zip")


def test_export_skips_missing_data(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    blob, _name = backup.export_backup(str(root), str(root / "assistant.db"))
    info = backup.inspect_backup(blob)
    assert info["summary"]["sessions"] == 0
    assert info["files"] >= 0


def _export_with_file(root, name, data):
    (root / "saves").mkdir(exist_ok=True)
    (root / "saves" / name).write_bytes(data)
    blob, _name = backup.export_backup(str(root), str(root / "assistant.db"))
    return blob


def test_limit_allows_gb_scale_backups():
    # 回归：旧上限 512MB 会把用户自己导出的备份（解压后 1.8GB）判成非法
    assert backup.MAX_BACKUP_BYTES >= 2 * 1024 ** 3


def test_inspect_accepts_large_compressible_backup(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    blob = _export_with_file(root, "save.json", b"0" * (20 * 1024 * 1024))
    info = backup.inspect_backup(blob)
    assert info["summary"]["saves"] == 1


def test_inspect_rejects_over_limit(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    blob = _export_with_file(root, "save.json", b"0" * (2 * 1024 * 1024))
    monkeypatch.setattr(backup, "MAX_BACKUP_BYTES", 1024 * 1024)
    with pytest.raises(ValueError) as err:
        backup.inspect_backup(blob)
    assert "超过上限" in str(err.value)


def test_inspect_rejects_zipbomb_ratio(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    blob = _export_with_file(root, "save.json", b"0" * (2 * 1024 * 1024))
    monkeypatch.setattr(backup, "MAX_BACKUP_RATIO_MIN", 1024)
    monkeypatch.setattr(backup, "MAX_BACKUP_RATIO", 2)
    with pytest.raises(ValueError) as err:
        backup.inspect_backup(blob)
    assert "压缩比" in str(err.value)


def test_inspect_rejects_too_many_entries(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    blob = _export_with_file(root, "save.json", b"{}")
    monkeypatch.setattr(backup, "MAX_BACKUP_ENTRIES", 1)
    with pytest.raises(ValueError) as err:
        backup.inspect_backup(blob)
    assert "条目过多" in str(err.value)
