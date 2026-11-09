# -*- coding: utf-8 -*-
"""PDF 解析单测：文本层直读 / 扫描件转视觉 / 无密钥提示 / 渲染与页数上限。"""
import io

import pytest

from app import docparse


def _make_text_pdf(text="Hello PDF 12345"):
    """手搓一个带文本层的最小 PDF（ASCII / 未压缩），不依赖 PDF 生成库。"""
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
    ]
    stream = ("BT /F1 18 Tf 40 120 Td (%s) Tj ET" % text).encode("latin-1")
    objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += (b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (len(objs) + 1, xref))
    return bytes(out)


def _make_scanned_pdf(pages=1):
    """用 PIL 生成「纯图片、无文本层」的 PDF，模拟扫描件。"""
    pytest.importorskip("PIL", reason="需要 pillow（仅测试用，不在运行依赖里）")
    from PIL import Image, ImageDraw
    imgs = []
    for i in range(pages):
        im = Image.new("RGB", (400, 200), "white")
        ImageDraw.Draw(im).text((30, 90), "SCANNED PAGE %d" % (i + 1), fill="black")
        imgs.append(im)
    buf = io.BytesIO()
    imgs[0].save(buf, "PDF", save_all=True, append_images=imgs[1:])
    return buf.getvalue()


def test_text_layer_pdf_read_without_vision():
    called = []
    text, warn = docparse.extract("a.pdf", _make_text_pdf(),
                                  ocr=lambda jpg: called.append(1) or "x")
    assert text and "Hello PDF 12345" in text
    assert called == []          # 有文本层就不该走视觉，省时间也省额度
    assert warn == ""


def test_scanned_pdf_routes_to_vision():
    seen = []

    def fake_ocr(jpg):
        seen.append(jpg[:3])
        return "识别出来的正文"

    text, warn = docparse.extract("scan.pdf", _make_scanned_pdf(), ocr=fake_ocr)
    assert text and "识别出来的正文" in text
    assert "【第 1 页】" in text
    assert seen and seen[0] == b"\xff\xd8\xff"     # 交给视觉模型的是 JPEG
    assert "扫描件" in warn


def test_scanned_pdf_without_ocr_gives_actionable_hint():
    text, warn = docparse.extract("scan.pdf", _make_scanned_pdf())
    assert text is None
    assert ("文本层" in warn) or ("扫描件" in warn)
    assert ("视觉模型" in warn) or ("聊天模型" in warn)   # 提示要能照做


def test_ocr_returning_nothing_is_reported():
    text, warn = docparse.extract("scan.pdf", _make_scanned_pdf(), ocr=lambda jpg: "")
    assert text is None
    assert "识别" in warn


def test_render_pages_returns_jpeg():
    shots, total = docparse._pdf_render_pages(_make_scanned_pdf())
    assert total == 1
    assert len(shots) == 1
    assert shots[0][0] == 1
    assert shots[0][1][:2] == b"\xff\xd8"


def test_render_pages_respects_max_pages():
    shots, total = docparse._pdf_render_pages(_make_scanned_pdf(pages=3), max_pages=2)
    assert total == 3
    assert [p for p, _ in shots] == [1, 2]


def test_broken_pdf_does_not_raise():
    text, warn = docparse.extract("broken.pdf", b"%PDF-1.4 not really a pdf",
                                  ocr=lambda jpg: "x")
    assert text is None
    assert warn


def test_progress_reports_text_pages():
    seen = []
    text, _ = docparse.extract("a.pdf", _make_text_pdf(),
                               progress=lambda done, total, stage: seen.append((done, total, stage)))
    assert text
    assert seen == [(1, 1, "text")]


def test_progress_reports_render_and_ocr_pages():
    seen = []

    def on_prog(done, total, stage):
        seen.append((done, total, stage))

    text, _ = docparse.extract("scan.pdf", _make_scanned_pdf(pages=2),
                               ocr=lambda jpg: "正文", progress=on_prog)
    assert text
    stages = [s for _, _, s in seen]
    assert stages.count("render") == 2
    assert stages.count("ocr") == 2
    assert all(total == 2 for _, total, _ in seen)


def test_progress_callback_error_does_not_break_parsing():
    def boom(done, total, stage):
        raise RuntimeError("进度回调炸了")

    text, _ = docparse.extract("a.pdf", _make_text_pdf(), progress=boom)
    assert text and "Hello PDF 12345" in text
