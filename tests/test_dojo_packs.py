# -*- coding: utf-8 -*-
"""研究道场：场景包加载与校验。"""
import json

import pytest

from app.dojo import packs


def _unit(uid, primitive="single_choice", **over):
    base = {
        "id": uid,
        "ability": "framing",
        "primitive": primitive,
        "judge": "L2",
        "minutes": 3,
        "payload": {
            "situation": "情境",
            "options": [{"key": "A", "text": "甲"}, {"key": "B", "text": "乙"}],
        },
        "expert": {"choice": "A", "why": "因为", "signals": ["信号"], "traps": {"B": "坑"}},
        "card": {"title": "卡", "trigger": "触发", "first_step": "第一步", "pitfall": "坑"},
        "source_ref": {"doc": "test"},
    }
    base.update(over)
    return base


def _order_unit(uid):
    return _unit(uid, "order",
                 payload={"situation": "情境", "steps": [{"id": "s1"}, {"id": "s2"}]},
                 expert={"order": ["s1", "s2"], "why": "因为", "signals": ["信号"]})


def _pair_unit(uid):
    return _unit(uid, "pair",
                 payload={"situation": "情境",
                          "left": [{"id": "l1"}, {"id": "l2"}],
                          "right": [{"id": "r1"}, {"id": "r2"}]},
                 expert={"pairs": {"l1": "r1", "l2": "r2"}, "why": "因为", "signals": ["信号"]})


def _write_pack(root, units, grade="official", sequence=None):
    d = root / "test-pack"
    (d / "units").mkdir(parents=True)
    manifest = {
        "id": "test-pack", "name": "测试包", "version": 1, "grade": grade,
        "alignment": {"standard": "测试标准", "dimensions": ["极限"]},
        "ability_weights": {"framing": 1}, "entry": {"minutes": 3},
    }
    if sequence:
        manifest["sequence"] = sequence
    (d / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    for u in units:
        (d / "units" / (u["id"] + ".json")).write_text(
            json.dumps(u, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def tmp_packs(tmp_path, monkeypatch):
    monkeypatch.setattr(packs, "PACKS_DIR", str(tmp_path))
    packs.reset_cache()
    yield tmp_path
    packs.reset_cache()


def test_builtin_pack_is_valid():
    packs.reset_cache()
    pack = packs.get_pack("cmc-calculus")
    # 28 = 12 个训练单元 + 8 前测 + 8 后测
    assert len(pack["units"]) == 28
    pub = packs.public_manifest(pack)
    assert pub["unit_count"] == 12
    assert pub["assessment"] == {"pre": 8, "post": 8}
    assert pack["units"][0]["id"] == "cmc-lim-01"      # sequence 生效
    assert pack["manifest"]["entry"]["first_unit"] == "cmc-lim-01"
    prims = {u["primitive"] for u in pack["units"] if not u.get("test")}
    assert len(prims) >= 3


def test_public_unit_never_leaks_answers():
    packs.reset_cache()
    pack = packs.get_pack("cmc-calculus")
    for u in pack["units"]:
        pub = packs.public_unit(u)
        assert "expert" not in pub and "card" not in pub
        assert "why" not in json.dumps(pub, ensure_ascii=False)


def test_bad_pack_is_rejected(tmp_packs):
    broken = _unit("u1")
    broken["card"] = {"title": "只有标题"}
    _write_pack(tmp_packs, [broken, _order_unit("u2"), _pair_unit("u3")])
    assert packs.load_packs() == []


def test_official_pack_needs_three_primitives(tmp_packs):
    _write_pack(tmp_packs, [_unit("u1"), _unit("u2"), _unit("u3")], grade="official")
    assert packs.load_packs() == []


def test_sequence_controls_order(tmp_packs):
    _write_pack(tmp_packs, [_unit("u1"), _order_unit("u2"), _pair_unit("u3")],
                sequence=["u3", "u2", "u1"])
    loaded = packs.load_packs()
    assert len(loaded) == 1
    assert [u["id"] for u in loaded[0]["units"]] == ["u3", "u2", "u1"]


def test_personal_pack_allows_one_primitive(tmp_packs):
    _write_pack(tmp_packs, [_unit("u1")], grade="personal")
    loaded = packs.load_packs()
    assert len(loaded) == 1
    assert loaded[0]["manifest"]["grade"] == "personal"
