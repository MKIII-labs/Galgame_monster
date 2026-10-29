# -*- coding: utf-8 -*-
"""代码库语义检索：分块、排序、签名失效、模型缺失时的关键词兜底。

不加载真实模型：把 `codeindex._embed_texts` 换成按关键词计数的确定性向量。
"""
import numpy as np
import pytest

from app import agent, codeindex

DIMS = ["折叠", "工具", "沙箱", "路径", "计划", "股票", "语音", "文件"]


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
    monkeypatch.setattr(codeindex, "_EMBED_CACHE",
                        {"model": None, "checked": 0.0, "ok": False})
    monkeypatch.setattr(codeindex, "_embed_texts", fake_embed)
    yield


def _sandbox(tmp_path):
    (tmp_path / "a.js").write_text(
        "function collapseToolGroup() {\n  // 折叠工具组\n}\n" * 3, encoding="utf-8")
    (tmp_path / "b.py").write_text(
        "def stock_price():\n    return 1  # 股票\n" * 2, encoding="utf-8")
    return str(tmp_path)


def test_search_ranks_relevant_chunk(tmp_path):
    res = codeindex.search(_sandbox(tmp_path), "工具组怎么折叠")
    assert "a.js" in res
    assert "代码检索" in res and ("相似度" in res or "关键词命中" in res)


def test_identifier_query_uses_sparse(tmp_path):
    """向量完全不懂的标识符，靠 BM25 也能命中。"""
    (tmp_path / "x.js").write_text(
        "function setToolGroupOpen(g, open) {\n  return open;\n}\n", encoding="utf-8")
    res = codeindex.search(str(tmp_path), "setToolGroupOpen")
    assert "x.js" in res
    assert "关键词命中" in res


def test_search_handles_empty_query(tmp_path):
    assert "请提供 query" in codeindex.search(_sandbox(tmp_path), "  ")


def test_index_cache_and_signature_invalidation(tmp_path):
    root = _sandbox(tmp_path)
    idx1, total1, _ = codeindex.get_index(root)
    assert total1 == 2 and idx1["meta"]
    sig1 = idx1["sig"]
    assert len(codeindex._CACHE) == 1
    # 第二次直接用缓存（同一对象）
    idx2, _t, _ = codeindex.get_index(root)
    assert idx2 is idx1

    # 改文件 → 签名变化 → 重建
    (tmp_path / "a.js").write_text("// 新增一行\n", encoding="utf-8")
    idx3, _t, _ = codeindex.get_index(root)
    assert idx3["sig"] != sig1


def test_index_persisted_to_disk(tmp_path):
    root = _sandbox(tmp_path)
    codeindex.get_index(root)
    files = list((tmp_path / "idx").glob("*.npz"))
    assert len(files) == 1

    # 清掉内存缓存，应能从磁盘恢复
    codeindex._CACHE.clear()
    idx, _t, _ = codeindex.get_index(root)
    assert idx["meta"]


def test_fallback_when_model_unavailable(tmp_path, monkeypatch):
    root = _sandbox(tmp_path)

    def boom(_texts, batch=32):
        raise codeindex.Unavailable("模型未下载")

    monkeypatch.setattr(codeindex, "_embed_texts", boom)
    res = codeindex.search(root, "折叠工具组")
    assert "已退回关键词检索" in res
    assert "a.js" in res


def test_binary_and_oversized_files_skipped(tmp_path):
    (tmp_path / "bin.dat").write_bytes(b"\x00\x01\x02")
    (tmp_path / "big.py").write_text("x = 1\n" * 40000, encoding="utf-8")
    (tmp_path / "ok.py").write_text("print('ok')\n", encoding="utf-8")
    files = codeindex._collect_files(str(tmp_path))
    rels = [rel for _full, rel, _size in files]
    assert "ok.py" in rels
    assert "big.py" not in rels
