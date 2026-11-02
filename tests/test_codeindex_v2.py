# -*- coding: utf-8 -*-
"""全仓索引 v2：增量、忽略规则、符号分块、状态与结构化检索。"""
import numpy as np
import pytest

from app import codeindex

DIMS = ["折叠", "工具", "索引", "股票"]


def fake_embed(texts):
    rows = []
    for t in texts or []:
        v = np.zeros(len(DIMS), dtype=np.float32)
        for i, w in enumerate(DIMS):
            v[i] = float(str(t).count(w))
        n = float(np.linalg.norm(v))
        rows.append(v / n if n else v)
    return np.asarray(rows, dtype=np.float32)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(codeindex, "INDEX_DIR", str(tmp_path / "idx"))
    monkeypatch.setattr(codeindex, "_CACHE", {})
    monkeypatch.setattr(codeindex, "_JOBS", {})
    monkeypatch.setattr(codeindex, "_embed_texts", fake_embed)
    yield


def test_incremental_reuses_unchanged_chunks(tmp_path):
    (tmp_path / "a.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("def beta():\n    return 2\n", encoding="utf-8")
    idx1, total1, _ = codeindex.get_index(str(tmp_path))
    assert total1 == 2 and len(idx1["meta"]) == 2
    beta_chunks = [m for m in idx1["meta"] if m["path"] == "b.py"]
    (tmp_path / "a.py").write_text("def alpha():\n    return 3\n", encoding="utf-8")
    idx2, _total, _ = codeindex.get_index(str(tmp_path))
    assert idx2["sig"] != idx1["sig"]
    assert [m for m in idx2["meta"] if m["path"] == "b.py"] == beta_chunks
    assert any("return 3" in m["text"] for m in idx2["meta"] if m["path"] == "a.py")


def test_ignore_dirs_and_gitignore(tmp_path):
    (tmp_path / "keep.py").write_text("x = 1\n", encoding="utf-8")
    sub = tmp_path / "node_modules"
    sub.mkdir()
    (sub / "x.js").write_text("function a(){}\n", encoding="utf-8")
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "b.js").write_text("function b(){}\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("generated/\n*.min.js\n", encoding="utf-8")
    gen = tmp_path / "generated"
    gen.mkdir()
    (gen / "g.py").write_text("x = 2\n", encoding="utf-8")
    (tmp_path / "app.min.js").write_text("var x=1;\n", encoding="utf-8")
    rels = [rel for _f, rel, _s in codeindex._collect_files(str(tmp_path))]
    assert rels == ["keep.py"]


def test_symbol_aware_chunks(tmp_path):
    (tmp_path / "a.py").write_text(
        "def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n" * 20,
        encoding="utf-8")
    chunks = codeindex._chunk_file(str(tmp_path / "a.py"), "a.py")
    symbols = {c.get("symbol") for c in chunks}
    assert "alpha" in symbols and "beta" in symbols


def test_search_items_and_status(tmp_path):
    (tmp_path / "a.py").write_text("def collapse_tool():\n    return 1\n",
                                   encoding="utf-8")
    res = codeindex.search_items(str(tmp_path), "折叠 工具")
    assert res["items"] and res["items"][0]["path"] == "a.py"
    assert res["items"][0]["symbol"] == "collapse_tool"
    assert codeindex.status(str(tmp_path))["state"] in ("idle", "ready")
    codeindex.reindex(str(tmp_path), background=False)
    st = codeindex.status(str(tmp_path))
    assert st["state"] == "ready" and st["files"] >= 1


def test_api_routes_registered():
    from app.registry import get
    from app.api import codeindex as api
    assert get("POST", "/api/codeindex/status") is api._api_codeindex_status
    assert get("POST", "/api/codeindex/reindex") is api._api_codeindex_reindex
    assert get("POST", "/api/codeindex/search") is api._api_codeindex_search
