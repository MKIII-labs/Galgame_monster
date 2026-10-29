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
