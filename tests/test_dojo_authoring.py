# -*- coding: utf-8 -*-
"""教材大纲 → 章级草稿 → 入库。"""
import copy

import pytest

from app.dojo import authoring, research


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setattr(authoring, "DRAFTS_FILE", str(tmp_path / "authoring_drafts.json"))
    authoring.reset_store()
    authoring.reset_cache()
    research.reset_cache()
    yield
    authoring.reset_store()
    authoring.reset_cache()


def _base_doc():
    q = research.get_inquiry("ma-uniform-convergence")
    doc = copy.deepcopy(q)
    for key in ("_path", "collection", "collection_name"):
        doc.pop(key, None)
    doc.update({"id": "ma5-ch01", "subject": "数学分析", "track": "教材"})
    return doc


def test_syllabus_is_chapter_level():
    rows = authoring.list_syllabi()
    assert rows and rows[0]["id"] == "math-analysis"
    assert rows[0]["level"] == "chapter"
    assert rows[0]["chapters"] == 23
    detail = authoring.syllabus_detail("math-analysis")
    assert len(detail["chapters"]) == 23
    assert detail["chapters"][0]["id"] == "ch01"
    assert detail["chapters"][0]["sections"]


def test_all_courses_have_syllabus():
    rows = {r["id"]: r for r in authoring.list_syllabi()}
    expected = {
        "math-analysis", "advanced-algebra", "analytic-geometry",
        "number-theory", "abstract-algebra", "ode", "discrete-math", "math-modeling",
    }
    assert expected <= set(rows)
    assert len(rows) == len(expected)
    prefixes = [s.get("item_prefix") for s in authoring.load_syllabi()]
    assert all(prefixes)
    assert len(prefixes) == len(set(prefixes))
    total = sum(r["chapters"] for r in rows.values())
    assert authoring.MAX_DRAFTS >= total


def test_item_id_shrinks_to_chapter():
    syl = {"id": "math-analysis", "item_prefix": "ma5"}
    assert authoring._item_id(syl, "ch01") == "ma5-ch01"
    assert authoring._item_id(syl, "ch21") == "ma5-ch21"
    assert authoring._item_id({"id": "ode"}, "ch03") == "ode-ch03"


def test_draft_must_have_six_acts():
    doc = _base_doc()
    doc["acts"] = doc["acts"][:5]
    with pytest.raises(authoring.AuthoringError):
        authoring._normalize(doc, "数学分析", {"id": "ch01", "title": "实数集与函数"},
                             "ma5-ch01")


def test_commit_writes_chapter_item(tmp_path, monkeypatch):
    monkeypatch.setattr(authoring, "INQUIRY_ROOT", str(tmp_path / "inquiries"))
    doc = authoring._normalize(_base_doc(), "数学分析",
                               {"id": "ch01", "title": "实数集与函数"}, "ma5-ch01")
    authoring._DRAFTS["dr_test"] = {
        "id": "dr_test", "syllabus_id": "math-analysis", "chapter_id": "ch01",
        "chapter_title": "实数集与函数", "doc": doc, "created": 0,
    }
    out = authoring.commit("dr_test")
    assert out["item_id"] == "ma5-ch01"
    assert out["collection"] == "course-math-analysis"
    item = tmp_path / "inquiries" / "course-math-analysis" / "items" / "ma5-ch01.json"
    assert item.is_file()
    assert '"id": "ma5-ch01"' in item.read_text(encoding="utf-8")
    assert "dr_test" not in authoring._DRAFTS
