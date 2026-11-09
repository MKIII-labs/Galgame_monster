# -*- coding: utf-8 -*-
"""PDF 阅读器的版面启发式与翻译结果解析（不依赖真实 PDF）。"""
from app import reader


def _seg(x0, x1, y0, y1, size=10.0, text="x"):
    return {"x0": x0, "x1": x1, "y0": y0, "y1": y1, "size": size,
            "chars": [{"ch": c, "x0": x0, "x1": x1, "y0": y0, "y1": y1,
                       "size": size} for c in text]}


def test_column_boundary_detects_two_columns():
    segs = []
    for i in range(5):
        segs.append(_seg(50, 280, 100 + i * 20, 110 + i * 20))
        segs.append(_seg(320, 550, 100 + i * 20, 110 + i * 20))
    segs.append(_seg(50, 550, 40, 55, size=16, text="Title"))
    b = reader._column_boundary(segs, 600)
    assert b is not None and 280 <= b <= 320


def test_column_boundary_rejects_single_column():
    segs = [_seg(50, 550, 40 + i * 20, 52 + i * 20) for i in range(12)]
    assert reader._column_boundary(segs, 600) is None


def test_rows_do_not_stitch_across_columns():
    left = [_seg(50, 280, 100, 110, text="left line"),
            _seg(50, 280, 120, 130, text="left two")]
    right = [_seg(320, 550, 100, 110, text="right line"),
             _seg(320, 550, 120, 130, text="right two")]
    rows = reader._rows(left + right, boundary=300)
    texts = sorted(r["text"] for r in rows if not r.get("figure"))
    assert texts == ["left line", "left two", "right line", "right two"]


def test_rows_stitch_fragments_on_same_line():
    a = _seg(50, 120, 100, 110, text="half")
    b = _seg(124, 200, 100, 110, text="line")
    rows = reader._rows([a, b])
    assert len(rows) == 1 and rows[0]["text"] == "half line"


def test_protect_inline_keeps_math_tokens():
    ttext, math = reader._protect_inline("We use n = 6 layers and 41.8 BLEU.")
    assert "⟦F1⟧" in ttext and "⟦F2⟧" in ttext
    assert math["F1"] == "n = 6"
    assert math["F2"] == "41.8"
    assert "layers" in ttext and "BLEU" in ttext


def test_protect_inline_plain_text_untouched():
    ttext, math = reader._protect_inline("Attention is all you need.")
    assert math == {}
    assert ttext == "Attention is all you need."


def test_parse_translations_from_fenced_json():
    raw = '```json\n[{"i":1,"t":"第一段"},{"i":2,"t":"第二段"}]\n```'
    out = reader._parse_translations(raw, 2)
    assert out == {1: "第一段", 2: "第二段"}


def test_latex_sane_accepts_valid_formulas():
    assert reader._latex_sane(r"\frac{a}{b}")
    assert reader._latex_sane(r"\left\{ x \right\} = 1")
    assert reader._latex_sane(r"\begin{aligned} x &= y \\ z &= w \end{aligned}")


def test_latex_sane_rejects_broken_results():
    assert not reader._latex_sane(r"\frac{a}{b")                    # 括号不配对
    assert not reader._latex_sane(r"\begin{aligned} x \end{matrix}")  # 环境不配对
    assert not reader._latex_sane("公式：x = 1")                     # 混入中文说明
    assert not reader._latex_sane("")


def test_parse_translations_tolerates_noise():
    raw = '好的：\n[{"i": 1, "t": "含 \\"引号\\" 的译文"}]'
    out = reader._parse_translations(raw, 3)
    assert out[1] == '含 "引号" 的译文'


def test_join_lines_removes_cjk_spacing():
    assert reader._join_lines(["教 育 背 景"]) == "教育背景"
    assert reader._join_lines(["hello", "world"]) == "hello world"


def test_join_lines_rejoins_hyphenation():
    # 软连字符 = 断词，去掉；普通连字符 = 复合词，保留
    assert reader._join_lines(["transduc\u00ad", "tion models"]) == "transduction models"
    assert reader._join_lines(["left-to-", "right"]) == "left-to-right"


def _mk(text, size=10.0, x0=74.0, base=10.0):
    return reader._make_block(
        {"lines": [text], "rect": [x0, 100, x0 + 400, 112], "size": size}, 1, 0, base=base)


def test_make_block_marks_list_items():
    b = _mk("• first item in a list")
    assert b["kind"] == "list"
    b = _mk("1. enumerate all subsets")
    assert b["kind"] == "list"


def test_make_block_marks_theorem_and_caption_roles():
    assert _mk("Theorem 2.1. For all n we have ...")["role"] == "theorem"
    assert _mk("Proof. Since 1 = 1 · 1 ...")["role"] == "theorem"
    assert _mk("Figure 3: Architecture of the model.")["role"] == "caption"
    assert _mk("Table 2 lists the results.")["role"] == "caption"
    assert _mk("Ordinary paragraph text.")["role"] == ""


def test_make_block_records_line_count():
    """译页要按原段行数排版（行距撑满原框），所以解析时必须记下行数。"""
    b = reader._make_block(
        {"lines": ["a", "b", "c"], "rect": [74, 100, 474, 130], "size": 10.0}, 1, 0, base=10.0)
    assert b["lines"] == 3


def test_normalize_translation_strips_unknown_placeholder_and_restores_number():
    head = {"kind": "heading", "text": "1.2. Contribution", "math": {}}
    # 旧译文把编号当成公式 ⇒ 占位符非法（math 里没有），编号从原文补回
    assert reader.normalize_translation(head, "⟦F1⟧ 贡献") == "1.2. 贡献"
    body = {"kind": "text", "text": "see [1]", "math": {"F1": "[1]"}}
    # 合法占位符必须保留（前端交给 KaTeX）
    assert reader.normalize_translation(body, "见 ⟦F1⟧") == "见 ⟦F1⟧"
    # 不存在的占位符直接丢掉
    assert reader.normalize_translation(body, "见 ⟦F9⟧ 结果") == "见 结果"
    # 只有非法占位符 = 没有译文（保留原文，比显示 ⟦F1⟧ 好）
    assert reader.normalize_translation(body, "⟦F9⟧") == ""


def test_normalize_translations_keeps_unknown_block_ids():
    meta = {"blocks": [{"id": "a", "kind": "text", "text": "x", "math": {}}]}
    out = reader.normalize_translations({"a": "⟦F1⟧x", "gone": "⟦F9⟧"}, meta)
    assert out["a"] == "x"
    assert out["gone"] == "⟦F9⟧"      # 找不到块定义：原样保留，不猜


def test_validate_translation_rejects_shifted_short_label():
    """批翻译按序号回填，模型少给一项就会错位：短标签不能收下整句译文。"""
    assert reader._validate_translation("C", "1.1. 研究动机", "zh") == "length"
    assert reader._validate_translation("Figure 1.", "图 1。", "zh") == ""


def test_normalize_translation_drops_shifted_short_label():
    """旧缓存（rd4）里的错位译文要在读取时清掉，而不是重译整篇。"""
    b = {"kind": "text", "text": "C", "math": {}}
    assert reader.normalize_translation(b, "1.1. 研究动机") == ""
    assert reader.normalize_translation(b, "碳") == "碳"


def test_layout_fit_respects_original_line_count():
    """版式导出：行距按原段行数撑满，行数不超过原段（与阅读器译页同口径）。"""
    from app import reader_export

    reader_export._ensure_fonts()
    lines, size, lh = reader_export._layout_fit(
        "中文" * 40, reader_export.BODY_FONT, 200, 60, 12, 5, 6)
    assert len(lines) <= 6
    assert len(lines) * lh <= 60.5
    # 短译文：仍按原段行距（60 / 6 = 10）铺开，而不是贴顶
    lines2, _s2, lh2 = reader_export._layout_fit(
        "短句", reader_export.BODY_FONT, 200, 60, 12, 5, 6)
    assert len(lines2) == 1 and abs(lh2 - 10) < 0.6


def test_layout_cover_avoids_figure_rect():
    """版式导出的白块要用差集避开公式/表格，不能整块盖住图。"""
    from app import reader_export

    pieces = reader_export._subtract_rect([0, 0, 100, 50], [[0, 20, 40, 30]])
    area = sum((p[2] - p[0]) * (p[3] - p[1]) for p in pieces)
    assert round(area) == 100 * 50 - 40 * 10
    assert reader_export._subtract_rect([0, 0, 100, 50], []) == [[0, 0, 100, 50]]


# ---------------------------------------------------------------- 新版抽取/清洗

def _char(ch, x0, x1, y0=100.0, y1=110.0, size=None, space=False):
    return {"ch": ch, "x0": x0, "x1": x1, "y0": y0, "y1": y1,
            "size": (size if size is not None else (y1 - y0)), "space": space}


def test_line_text_uses_explicit_spaces():
    """零面积空格必须参与拼行，否则 of Computer 会粘成 ofComputer。"""
    chars = [
        _char("o", 0, 5), _char("f", 5, 9),
        _char(" ", 10, 10, space=True),
        _char("C", 12, 20), _char("o", 20, 25), _char("m", 25, 35),
    ]
    assert reader._line_text(chars) == "of Com"


def test_merge_descenders_reattaches_letters():
    """g/p/q/y 被按中心分桶切成独立段时要并回主行。"""
    a = _seg(0, 30, 100, 110, text="Len")
    b = _seg(31, 39, 104, 114, text="g")
    reader._merge_descenders([a, b])
    assert "Leng" in reader._line_text(a["chars"])


def test_clean_meta_blocks_drops_watermark_and_repeats():
    blocks = [
        {"kind": "text", "page": 1, "rect": [0, 0, 200, 10],
         "text": "This CVPR paper is the Open Access version provided by IEEE"},
        {"kind": "text", "page": 1, "rect": [500, 735, 540, 747], "text": "123"},
        {"kind": "text", "page": 2, "rect": [500, 735, 540, 747], "text": "123"},
        {"kind": "text", "page": 3, "rect": [500, 735, 540, 747], "text": "123"},
        {"kind": "text", "page": 2, "rect": [50, 100, 500, 120],
         "text": "A real paragraph that should survive."},
    ]
    out = reader._clean_meta_blocks(blocks, [[595, 790]] * 3)
    assert [b["text"] for b in out] == ["A real paragraph that should survive."]


def test_make_block_rejects_table_row_heading():
    b = _mk("Dino 319 36 0.63 4.38 4.89", size=12)
    assert b["kind"] == "table"


def test_make_block_drops_latex_source_block():
    b = _mk(r"\label {eq:C} C = \begin {pmatrix} 0 & 0 & -1 \end {pmatrix}^T, (3)")
    assert b["kind"] == "hidden"


def test_mark_reference_blocks_matches_merged_heading():
    blocks = [
        {"kind": "heading", "text": "9 Conclusion"},
        {"kind": "heading", "text": "References InEuropean Conference on Computer Vision"},
        {"kind": "text", "text": "[1] First reference"},
        {"kind": "text", "text": "[2] Second reference"},
    ]
    reader._mark_reference_blocks(blocks)
    assert blocks[1]["kind"] == "ref"
    assert blocks[2]["kind"] == "ref"


def test_front_matter_marks_authors_meta():
    blocks = [
        {"kind": "heading", "size": 20, "rect": [100, 80, 500, 96], "text": "A Paper Title"},
        {"kind": "heading", "size": 12, "rect": [100, 120, 400, 132], "text": "Alice Smith"},
        {"kind": "heading", "size": 12, "rect": [100, 160, 500, 172], "text": "Abstract"},
        {"kind": "text", "size": 10, "rect": [100, 180, 500, 200], "text": "Body."},
    ]
    reader._mark_front_matter(blocks)
    assert blocks[0]["kind"] == "heading"
    assert blocks[1]["kind"] == "meta"
    assert blocks[3]["kind"] == "text"


def test_validate_translation_rejects_echo_and_meta():
    src = "We present a fast and accurate solution for the perspective four points problem."
    assert reader._validate_translation(src, src, "zh") == "untranslated"
    assert reader._validate_translation(src, "以下是译文", "zh") == "meta"
    assert reader._validate_translation(
        src, "我们提出一种快速而精确的方法来求解透视四点问题，并通过实验验证了它的性能与稳定性。", "zh") == ""
    # 短标题本来就远短于英文，不能按长段落的长度比判失败
    assert reader._validate_translation("Introduction", "引言", "zh") == ""


def test_model_of_uses_chat_model():
    assert reader._model_of({"light_model": "lite", "chat_model": "main"}) == "main"


def test_start_translate_skips_non_translatable(monkeypatch, tmp_path):
    """表格 / 作者 / 文献 / 公式不进翻译任务；标题与摘要进上下文。"""
    import shutil
    import time
    from app import llm

    pdf = tmp_path / "demo.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    blocks = [
        {"id": "h1", "page": 1, "kind": "heading", "text": "A Paper Title", "ttext": "A Paper Title"},
        {"id": "p1", "page": 1, "kind": "text", "text": "Body text.",
         "ttext": "Body text."},
        {"id": "m1", "page": 1, "kind": "meta", "text": "Alice Smith", "ttext": "Alice Smith"},
        {"id": "t1", "page": 1, "kind": "table", "text": "1 2 3 4 5", "ttext": "1 2 3 4 5"},
        {"id": "r1", "page": 1, "kind": "ref", "text": "[1] Ref", "ttext": "[1] Ref"},
        {"id": "f1", "page": 1, "kind": "formula", "text": "x = 1", "ttext": "x = 1"},
    ]
    prompts = []

    monkeypatch.setattr(reader, "ensure_glossary", lambda *a, **k: {"terms": []})

    def fake_chat(api_key, api_url, api_model, messages, **kw):
        prompts.append(messages)
        return '[{"i":1,"t":"论文标题"},{"i":2,"t":"正文。"}]'

    monkeypatch.setattr(llm, "_quick_chat", fake_chat)
    doc_id = reader._doc_id(str(pdf))
    try:
        jid, total = reader.start_translate(str(pdf), blocks, "zh", "简体中文",
                                            all_blocks=blocks)
        assert total == 2                      # 只有 heading + text
        for _ in range(150):
            job = reader.job_snapshot(jid)
            if job and job.get("done"):
                break
            time.sleep(0.02)
        job = reader.job_snapshot(jid)
        assert job and job.get("done")
        done = [e for e in job["events"] if e.get("type") == "done"][-1]
        assert done["skipped"] == 4
        assert prompts and "论文标题" in prompts[0][0]["content"]
    finally:
        shutil.rmtree(reader._doc_dir(doc_id), ignore_errors=True)


# ---------------------------------------------------------------- 译文导出

def _export_meta():
    return {
        "name": "demo.pdf", "sizes": [[595, 842]], "blocks": [
            {"id": "h1", "page": 1, "kind": "heading", "level": 1,
             "text": "Attention Is All You Need"},
            {"id": "h2", "page": 1, "kind": "heading", "level": 2, "text": "1 Introduction"},
            {"id": "p1", "page": 1, "kind": "text", "text": "We use n = 6 layers.",
             "math": {"F1": "n = 6"}},
            {"id": "f1", "page": 1, "kind": "formula", "text": "y = x^2", "rect": []},
            {"id": "r1", "page": 1, "kind": "ref",
             "text": "[1] Vaswani et al. 2017."},
        ],
    }


def test_export_restores_inline_formula_placeholder():
    from app import reader_export
    assert reader_export.restore_inline("使用 ⟦F1⟧ 层。", {"F1": "n = 6"}) == "使用 n = 6 层。"
    assert reader_export.restore_inline("没有占位符", {}) == "没有占位符"


def test_export_markdown_uses_translation_and_keeps_source():
    from app import reader_export
    tr = {"h1": "注意力就是你所需要的一切", "h2": "1 引言",
          "p1": "我们使用 ⟦F1⟧ 层。", "r1": ""}
    raw, ext, mime, missing = reader_export.export("", _export_meta(), tr, "md")
    text = raw.decode("utf-8")
    assert ext == "md" and "markdown" in mime
    assert "# 注意力就是你所需要的一切" in text
    assert "我们使用 n = 6 层。" in text
    assert "We use n = 6 layers." in text          # 原文以引用块保留
    assert "[1] Vaswani et al. 2017." in text
    assert missing == 0


def test_export_pdf_mono_returns_real_pdf_and_counts_missing():
    from app import reader_export
    tr = {"h1": "注意力就是你所需要的一切", "h2": "1 引言"}   # p1 故意不给
    raw, ext, mime, missing = reader_export.export("", _export_meta(), tr, "mono")
    assert ext == "pdf" and mime == "application/pdf"
    assert raw.startswith(b"%PDF")
    assert missing == 1
    assert len(raw) > 1000


def test_export_pdf_dual_contains_both_languages():
    from app import reader_export
    tr = {"h1": "注意力就是你所需要的一切", "h2": "1 引言", "p1": "我们使用 n = 6 层。"}
    raw, ext, _, missing = reader_export.export("", _export_meta(), tr, "dual")
    assert ext == "pdf" and raw.startswith(b"%PDF")
    assert missing == 0
    # 字体子集 + 两段正文都在：体积应比纯短文本大
    assert len(raw) > 2000


def test_export_name_keeps_readable_suffix():
    from app import reader_export
    meta = _export_meta()
    assert reader_export.export_name(meta, "mono", "pdf") == "demo-译文.pdf"
    assert reader_export.export_name(meta, "dual", "pdf") == "demo-双语对照.pdf"
    assert reader_export.export_name(meta, "md", "md") == "demo-译文.md"


def test_export_keeps_table_and_meta_without_translation():
    from app import reader_export
    meta = {"name": "demo.pdf", "sizes": [[595, 842]], "blocks": [
        {"id": "t1", "page": 1, "kind": "table", "text": "Dino 319 36 0.63 4.38 4.89"},
        {"id": "m1", "page": 1, "kind": "meta", "text": "Alice Smith, Example University"},
        {"id": "p1", "page": 1, "kind": "text", "text": "Body."},
    ]}
    raw, ext, _, missing = reader_export.export("", meta, {"p1": "正文。"}, "md")
    text = raw.decode("utf-8")
    assert ext == "md" and missing == 0
    assert "Dino 319 36 0.63" in text and "```" in text
    assert "> Alice Smith" in text


def test_export_layout_keeps_page_size_and_overlays_translation(tmp_path):
    import io
    from reportlab.pdfgen import canvas as rl_canvas
    from pypdf import PdfReader
    from app import reader_export

    pdf = tmp_path / "layout.pdf"
    c = rl_canvas.Canvas(str(pdf), pagesize=(595, 842))
    c.drawString(72, 730, "Hello world")
    c.showPage()
    c.save()
    meta = {"name": "layout.pdf", "pages": 1, "sizes": [[595, 842]], "blocks": [
        {"id": "p1", "page": 1, "kind": "text", "text": "Hello world",
         "rect": [72, 100, 300, 120], "math": {}},
    ]}
    raw, ext, mime, missing = reader_export.export(
        str(pdf), meta, {"p1": "你好，世界。"}, "layout-mono")
    assert ext == "pdf" and mime == "application/pdf" and missing == 0
    got = PdfReader(io.BytesIO(raw))
    assert len(got.pages) == 1
    assert round(float(got.pages[0].mediabox.width)) == 595
    assert "你好" in (got.pages[0].extract_text() or "")

    raw2, _, _, _ = reader_export.export(
        str(pdf), meta, {"p1": "你好，世界。"}, "layout-dual")
    got2 = PdfReader(io.BytesIO(raw2))
    assert round(float(got2.pages[0].mediabox.width)) == 1190
    assert reader_export.export_name(meta, "layout-dual", "pdf") == "layout-版式双语.pdf"


def test_export_rejects_unknown_mode():
    from app import reader_export
    import pytest
    with pytest.raises(reader_export.ExportError):
        reader_export.export("", _export_meta(), {}, "pdf")


def test_export_fix_breaks_avoids_leading_punctuation():
    """中文禁则：行首的「，」要并回上一行，不能让它单独起行。"""
    from app import reader_export
    lines = reader_export._fix_breaks(["这是一行文字", "，继续"], "RdNone", 10, 200, 200)
    assert lines[0].endswith("，")
    assert lines[1].startswith("继续")


def test_export_prep_math_normalizes_known_commands():
    from app import reader_export
    t = reader_export._prep_math(
        r"$\begin{equation} A \setminus B \text{ and } C \end{equation}$")
    assert t.startswith("A")
    assert "\\backslash" in t
    assert "\\mathrm{" in t
    assert "\\begin" not in t and "\\text{" not in t


def test_export_math_png_renders_when_matplotlib_available():
    from app import reader_export
    png = reader_export._math_png(r"\frac{a}{b}")
    if png is None:
        import pytest
        pytest.skip("matplotlib 不可用")
    assert png.startswith(b"\x89PNG")


def test_cached_formula_without_file_returns_empty():
    from app import reader
    assert reader.cached_formula("", 1, [0, 0, 10, 10]) == ""
    assert reader.cached_formula("not-a-file.pdf", 1, [0, 0, 10, 10]) == ""
