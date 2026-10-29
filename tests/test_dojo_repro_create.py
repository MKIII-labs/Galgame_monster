# -*- coding: utf-8 -*-
"""研究道场：复现包编辑器（保存前用参考解自检）。"""
import pytest

from app.dojo import repro

DATA = "x,y\n1,2\n2,4\n3,6\n"
STARTER = "import json\nprint(json.dumps({'slope': 0}))\n"
REFERENCE = "import json\nprint(json.dumps({'slope': 2.0}))\n"


@pytest.fixture
def user_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(repro, "USER_REPRO_DIR", str(tmp_path))
    monkeypatch.setattr(repro, "REPRO_DIRS", (str(tmp_path),))
    yield tmp_path


def _payload(**over):
    base = {
        "title": "我的复现",
        "source": "测试数据",
        "question": "用 data.csv 算出斜率，打印 {\"slope\": ...}",
        "starter": STARTER,
        "reference": REFERENCE,
        "time_limit": 5,
        "data": [{"name": "data.csv", "content": DATA}],
        "expected": {"slope": {"value": 2.0, "tolerance": 1e-6}},
    }
    base.update(over)
    return base


def test_create_pack_and_public_view(user_dir):
    out = repro.create_pack(_payload())
    assert out["check"]["passed"] is True
    pack = repro.get_pack(out["id"])
    pub = repro.public_pack(pack)
    assert pub["title"] == "我的复现"
    assert pub["expected"]["slope"]["value"] == 2.0
    assert "reference" not in pub
    # 参考解确实落盘了，但不在题面接口里
    assert (user_dir / out["id"] / "reference.py").is_file()


def test_reference_must_match_expected(user_dir):
    with pytest.raises(repro.ReproError) as e:
        repro.create_pack(_payload(expected={"slope": {"value": 3.0, "tolerance": 0.01}}))
    assert "自检没通过" in str(e.value)
    assert not any(user_dir.iterdir())


def test_reject_bad_data_extension(user_dir):
    with pytest.raises(repro.ReproError):
        repro.create_pack(_payload(data=[{"name": "run.exe", "content": "x"}]))


def test_reject_non_numeric_target(user_dir):
    with pytest.raises(repro.ReproError):
        repro.create_pack(_payload(expected={"slope": {"value": "不是数", "tolerance": 0.1}}))


def test_reject_empty_reference(user_dir):
    with pytest.raises(repro.ReproError):
        repro.create_pack(_payload(reference=""))


def test_same_title_gets_distinct_ids(user_dir):
    a = repro.create_pack(_payload())
    b = repro.create_pack(_payload())
    assert a["id"] != b["id"]
    assert len(repro.load_packs()) == 2
