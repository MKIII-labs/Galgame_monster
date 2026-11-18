# -*- coding: utf-8 -*-
"""文档抽取缓存：同内容文件命中缓存，手动重解析可旁路。"""
import pytest

from app import docparse


@pytest.fixture(autouse=True)
def _tmp_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(docparse, "DOC_CACHE_DIR", str(tmp_path / "doc_cache"))


@pytest.fixture
def calls(monkeypatch):
    """统计真正执行抽取的次数。"""
    box = {"n": 0}
    real = docparse._extract_pages_impl

    def fake(name, raw, ocr=None, progress=None):
        box["n"] += 1
        return real(name, raw, ocr=ocr, progress=progress)

    monkeypatch.setattr(docparse, "_extract_pages_impl", fake)
    return box


def _doc():
    return ("# 标题\n\n" + "正文内容。" * 400).encode("utf-8")


def test_cache_hit_skips_extraction(calls):
    raw = _doc()
    t1, _w1, p1 = docparse.extract_pages("笔记.md", raw)
    assert "正文内容" in t1 and p1 == []
    assert calls["n"] == 1
    t2, _w2, _p2 = docparse.extract_pages("笔记.md", raw)
    assert t2 == t1
    assert calls["n"] == 1                 # 第二次直接命中缓存


def test_cache_isolated_by_content(calls):
    docparse.extract_pages("笔记.md", _doc())
    docparse.extract_pages("笔记.md", _doc() + "\n\n结尾".encode("utf-8"))
    assert calls["n"] == 2


def test_reparse_bypasses_cache(calls):
    raw = _doc()
    docparse.extract_pages("笔记.md", raw)
    docparse.extract_pages("笔记.md", raw, use_cache=False)
    assert calls["n"] == 2


def test_empty_file_not_cached(calls):
    assert docparse.extract_pages("空.md", b"") == (None, "文件是空的", [])
    assert calls["n"] == 0


def test_ocr_result_is_cached(monkeypatch):
    """扫描件的视觉识别结果也缓存：同一份扫描 PDF 不该反复烧识图额度。"""
    monkeypatch.setattr(docparse, "_pdf_text",
                        lambda raw, progress=None: (None, "", True, []))
    box = {"n": 0}

    def fake_ocr(raw, ocr, progress=None):
        box["n"] += 1
        return "识别出的正文", "", True, ["识别出的正文"]

    monkeypatch.setattr(docparse, "_pdf_ocr", fake_ocr)
    raw = b"%PDF-1.4 fake scanned"
    t1, _w1, p1 = docparse.extract_pages("扫描件.pdf", raw, ocr=lambda jpg: "x")
    t2, _w2, p2 = docparse.extract_pages("扫描件.pdf", raw, ocr=lambda jpg: "x")
    assert t1 == t2 == "识别出的正文"
    assert p1 == p2 == ["识别出的正文"]
    assert box["n"] == 1
