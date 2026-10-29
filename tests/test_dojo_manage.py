# -*- coding: utf-8 -*-
"""研究道场：个人场景包的删除 / 重命名 / 导出导入。"""
import copy

import pytest

from app.dojo import packs

UNIT = {
    "id": "u1",
    "ability": "method",
    "primitive": "single_choice",
    "judge": "L2",
    "minutes": 4,
    "payload": {
        "situation": "这是一道用于测试的判断题，长度足够。",
        "options": [{"key": "A", "text": "甲"}, {"key": "B", "text": "乙"}, {"key": "C", "text": "丙"}],
    },
    "expert": {"choice": "B", "why": "理由", "signals": ["信号"], "traps": {"A": "坑", "C": "坑"}},
    "card": {"title": "卡", "trigger": "触发", "first_step": "第一步", "pitfall": "坑"},
    "source_ref": {"doc": "test"},
}


@pytest.fixture
def user_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(packs, "USER_PACKS_DIR", str(tmp_path))
    packs.reset_cache()
    yield tmp_path
    packs.reset_cache()


def _make(pid="my-pack", name="我的包"):
    packs.write_pack(pid, {
        "id": pid,
        "name": name,
        "version": 1,
        "grade": "personal",
        "alignment": {"standard": "测试", "dimensions": ["x"]},
        "ability_weights": {"method": 1},
        "entry": {"minutes": 4, "first_unit": "u1"},
        "sequence": ["u1"],
    }, [copy.deepcopy(UNIT)])
    return pid


def test_delete_user_pack(user_dir):
    pid = _make()
    assert any(p["manifest"]["id"] == pid for p in packs.load_packs())
    packs.delete_user_pack(pid)
    assert not any(p["manifest"]["id"] == pid for p in packs.load_packs())


def test_cannot_delete_builtin(user_dir):
    with pytest.raises(packs.PackError):
        packs.delete_user_pack("cmc-calculus")


def test_rename_user_pack(user_dir):
    pid = _make()
    out = packs.rename_user_pack(pid, "新名字")
    assert out["name"] == "新名字"
    assert packs.get_pack(pid)["manifest"]["name"] == "新名字"


def test_export_import_roundtrip(user_dir):
    pid = _make()
    payload = packs.export_pack(pid)
    assert payload["format"] == "dojo-pack"
    out = packs.import_pack(payload)
    assert out["id"] != pid
    assert out["name"] == "我的包"
    assert out["grade"] == "personal"
    assert out["unit_count"] == 1

    again = packs.import_pack(payload)
    assert again["id"] not in (pid, out["id"])


def test_import_forces_personal(user_dir):
    pid = _make()
    payload = packs.export_pack(pid)
    payload["manifest"]["grade"] = "official"
    out = packs.import_pack(payload)
    assert out["grade"] == "personal"


def test_import_requires_units(user_dir):
    with pytest.raises(packs.PackError):
        packs.import_pack({"manifest": {"name": "空包"}, "units": []})
