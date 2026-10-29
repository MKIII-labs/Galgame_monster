# -*- coding: utf-8 -*-
"""研究道场：复现实验（受限执行 + 数值比对）。"""
import json
import pathlib

import pytest

from app.dojo import repro

ROOT = pathlib.Path(__file__).resolve().parents[1]
PACK = ROOT / "resources" / "dojo" / "repro" / "ols-slope"


def _read(name):
    return (PACK / name).read_text(encoding="utf-8")


def test_list_and_public_pack_hide_reference():
    packs = repro.load_packs()
    assert any(p["id"] == "ols-slope" for p in packs)
    pub = repro.public_pack(repro.get_pack("ols-slope"))
    assert pub["title"] == "复现最小二乘斜率"
    assert pub["expected"]["slope"]["value"] == 2.003
    assert pub["starter"].strip()
    assert "reference" not in json.dumps(pub, ensure_ascii=False)


def test_reference_solution_passes():
    out = repro.run("ols-slope", _read("reference.py"))
    assert out["passed"] is True
    assert out["fields"][0]["name"] == "slope"
    assert out["fields"][0]["ok"] is True


def test_starter_fails():
    out = repro.run("ols-slope", _read("starter.py"))
    assert out["passed"] is False
    assert out["fields"][0]["actual"] == 0.0


def test_output_without_json():
    out = repro.run("ols-slope", "print('hello')")
    assert out["passed"] is False
    assert "JSON" in out["error"]


def test_empty_code_rejected():
    with pytest.raises(repro.ReproError):
        repro.run("ols-slope", "   ")


def test_timeout(tmp_path, monkeypatch):
    d = tmp_path / "slow"
    d.mkdir()
    (d / "manifest.json").write_text(json.dumps({
        "id": "slow", "title": "慢任务", "question": "等",
        "data": ["data.csv"], "starter": "starter.py", "time_limit": 1,
        "expected": {"v": {"value": 1, "tolerance": 0.01}},
    }, ensure_ascii=False), encoding="utf-8")
    (d / "data.csv").write_text("x\n1\n", encoding="utf-8")
    (d / "starter.py").write_text("print('{}')\n", encoding="utf-8")
    monkeypatch.setattr(repro, "REPRO_DIRS", (str(tmp_path),))
    out = repro.run("slow", "while True:\n    pass")
    assert out["timed_out"] is True
    assert out["passed"] is False


def test_missing_pack():
    with pytest.raises(repro.ReproError):
        repro.get_pack("no-such-repro")
