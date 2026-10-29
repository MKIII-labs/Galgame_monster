# -*- coding: utf-8 -*-
"""研究道场：材料导入（抽取 → 草稿 → 确认落盘）。"""
import base64
import json

import pytest

from app.dojo import importer, packs

MATERIAL = (
    "在建立回归模型之前，先检查解释变量之间是否存在严重共线性。"
    "如果方差膨胀因子大于 10，说明共线性已经影响系数估计的稳定性。"
    "此时应优先考虑剔除冗余变量或改用岭回归，而不是直接增加样本量。"
    "此外，残差图出现明显的漏斗形状，提示异方差，需要做稳健标准误或对数变换。"
)


def _b64(text):
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _draft(**over):
    base = {
        "ability": "assumption",
        "primitive": "single_choice",
        "minutes": 4,
        "payload": {
            "situation": "建模前发现两个解释变量的方差膨胀因子都大于 10，下一步最合理的做法是？",
            "options": [
                {"key": "A", "text": "直接增加样本量"},
                {"key": "B", "text": "剔除冗余变量或改用岭回归"},
                {"key": "C", "text": "忽略共线性继续建模"},
            ],
        },
        "expert": {
            "suggested": "B",
            "why": "材料明确说 VIF 大于 10 时优先剔除冗余变量或改用岭回归。",
            "signals": ["VIF > 10", "系数估计不稳定"],
            "traps": {"A": "增加样本量不解决共线性", "C": "会放大系数方差"},
            "source_quote": "如果方差膨胀因子大于 10，说明共线性已经影响系数估计的稳定性",
        },
        "card": {"title": "先查共线性", "trigger": "多变量回归", "first_step": "算 VIF", "pitfall": "直接加样本"},
        "confidence": "high",
    }
    base.update(over)
    return base


@pytest.fixture(autouse=True)
def _clean():
    importer.reset_materials()
    yield
    importer.reset_materials()


def test_parse_material_returns_preview_and_segments():
    out = importer.parse_material("note.md", _b64(MATERIAL))
    assert out["chars"] >= 80
    assert out["preview"].startswith("在建立回归模型之前")
    assert out["segments"]
    assert len(out["material_id"]) == 32


def test_parse_material_rejects_short_text():
    with pytest.raises(importer.DojoImportError):
        importer.parse_material("x.md", _b64("太短了"))


def test_validate_draft_accepts_quoted_evidence():
    unit, reason = importer._validate_draft(_draft(), MATERIAL)
    assert reason is None
    assert unit["expert"]["suggested"] == "B"
    assert "choice" not in unit["expert"]          # 草稿阶段不能有正式答案
    assert unit["judge"] == "L2"


def test_validate_draft_rejects_fabricated_quote():
    bad = _draft()
    bad["expert"] = dict(bad["expert"], source_quote="这段原文根本不存在于材料里")
    unit, reason = importer._validate_draft(bad, MATERIAL)
    assert unit is None
    assert "原文依据" in reason


def test_validate_draft_requires_traps_for_every_wrong_option():
    bad = _draft()
    bad["expert"] = dict(bad["expert"], traps={"A": "只写了一个"})
    unit, reason = importer._validate_draft(bad, MATERIAL)
    assert unit is None
    assert "错误选项" in reason


def test_validate_draft_rejects_unsupported_primitive():
    bad = _draft(primitive="numeric")
    unit, reason = importer._validate_draft(bad, MATERIAL)
    assert unit is None
    assert "题型" in reason


def test_draft_uses_model_output(monkeypatch):
    out = importer.parse_material("note.md", _b64(MATERIAL))
    monkeypatch.setattr(importer, "get_api_conf", lambda p: ("k", "u", "m"))
    monkeypatch.setattr(importer.llm, "stream_chat",
                        lambda *a, **k: {"visible": json.dumps([_draft()], ensure_ascii=False),
                                         "usage": {"total_tokens": 1}})
    got = importer.draft(out["material_id"], count=3)
    assert len(got["drafts"]) == 1
    assert got["drafts"][0]["draft_id"] == "d1"
    assert got["rejected"] == []


def test_draft_reports_rejected_items(monkeypatch):
    out = importer.parse_material("note.md", _b64(MATERIAL))
    bad = _draft()
    bad["expert"] = dict(bad["expert"], source_quote="不存在的原文片段")
    monkeypatch.setattr(importer, "get_api_conf", lambda p: ("k", "u", "m"))
    monkeypatch.setattr(importer.llm, "stream_chat",
                        lambda *a, **k: {"visible": json.dumps([bad], ensure_ascii=False)})
    got = importer.draft(out["material_id"], count=3)
    assert got["drafts"] == []
    assert got["rejected"][0]["reason"].startswith("原文依据")


def test_commit_writes_personal_pack(tmp_path, monkeypatch):
    monkeypatch.setattr(packs, "USER_PACKS_DIR", str(tmp_path))
    packs.reset_cache()
    unit = _draft()
    unit["confirmed"] = True
    out = importer.commit("我的统计课", [unit], standard="论文方法复盘",
                          material_name="note.md")
    assert out["unit_count"] == 1
    assert (tmp_path / out["pack_id"] / "units" / "imp-01.json").is_file()
    loaded = packs.get_pack(out["pack_id"])
    assert loaded["manifest"]["grade"] == "personal"
    assert loaded["manifest"]["name"] == "我的统计课"
    assert loaded["units"][0]["expert"]["choice"] == "B"
    assert "suggested" not in loaded["units"][0]["expert"]
    assert loaded["units"][0]["source_ref"]["quote"]
    packs.reset_cache()


def test_commit_requires_confirmation(tmp_path, monkeypatch):
    monkeypatch.setattr(packs, "USER_PACKS_DIR", str(tmp_path))
    packs.reset_cache()
    with pytest.raises(importer.DojoImportError):
        importer.commit("没有确认", [_draft()], material_name="note.md")
    packs.reset_cache()
