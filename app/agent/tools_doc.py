# -*- coding: utf-8 -*-
"""doc_create：把结构化内容写成 .docx / .xlsx / .pdf / .pptx。

对齐 Trae 的「文档生成」：模型给 blocks（段落 / 标题 / 列表 / 表格）或 sheets（表格数据），
工具直接落盘成真正的 Office / PDF 文件，用户在沙箱里打开即可。
docx / xlsx / pptx 用标准库 zipfile 写 OOXML，不引入 python-docx / openpyxl / python-pptx；
PDF 用 reportlab（阅读器导出已经依赖它），没有 reportlab 时给出安装提示。
"""

import io
import os
import re
import uuid
import zipfile
from xml.sax.saxutils import escape

MAX_BLOCKS = 4000
MAX_CELL_CHARS = 20000

_ILLEGAL_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _esc(s):
    return escape(_ILLEGAL_XML.sub("", str("" if s is None else s)), {'"': "&quot;"})


def _as_list(v):
    return list(v) if isinstance(v, (list, tuple)) else []


def _norm_blocks(blocks):
    out = []
    for b in _as_list(blocks):
        if isinstance(b, str):
            out.append({"type": "paragraph", "text": b})
        elif isinstance(b, dict):
            out.append(b)
    if len(out) > MAX_BLOCKS:
        raise ValueError("blocks 太多（%d 条，上限 %d 条）" % (len(out), MAX_BLOCKS))
    return out


# ============================================================
# docx（WordprocessingML）
# ============================================================

_DOCX_CT = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>"""

_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""

_DOC_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""


def _heading_style(level, size, before):
    return (
        '<w:style w:type="paragraph" w:styleId="Heading%d">'
        '<w:name w:val="heading %d"/><w:basedOn w:val="Normal"/>'
        '<w:next w:val="Normal"/><w:qFormat/>'
        '<w:pPr><w:keepNext/><w:spacing w:before="%d" w:after="120"/>'
        '<w:outlineLvl w:val="%d"/></w:pPr>'
        '<w:rPr><w:b/><w:sz w:val="%d"/><w:szCs w:val="%d"/></w:rPr>'
        "</w:style>" % (level, level, before, level - 1, size, size))


_DOCX_STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    "<w:docDefaults><w:rPrDefault><w:rPr>"
    '<w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:eastAsia="Microsoft YaHei"/>'
    '<w:sz w:val="22"/><w:szCs w:val="22"/></w:rPr></w:rPrDefault>'
    '<w:pPrDefault><w:pPr><w:spacing w:after="120" w:line="300" w:lineRule="auto"/>'
    "</w:pPr></w:pPrDefault></w:docDefaults>"
    '<w:style w:type="paragraph" w:default="1" w:styleId="Normal">'
    '<w:name w:val="Normal"/><w:qFormat/></w:style>'
    + _heading_style(1, 40, 240)
    + _heading_style(2, 32, 200)
    + _heading_style(3, 28, 160)
    + _heading_style(4, 26, 140)
    + '<w:style w:type="table" w:styleId="TableGrid">'
    '<w:name w:val="Table Grid"/><w:tblPr><w:tblBorders>'
    '<w:top w:val="single" w:sz="4" w:color="auto"/>'
    '<w:left w:val="single" w:sz="4" w:color="auto"/>'
    '<w:bottom w:val="single" w:sz="4" w:color="auto"/>'
    '<w:right w:val="single" w:sz="4" w:color="auto"/>'
    '<w:insideH w:val="single" w:sz="4" w:color="auto"/>'
    '<w:insideV w:val="single" w:sz="4" w:color="auto"/>'
    "</w:tblBorders></w:tblPr></w:style>"
    "</w:styles>"
)


def _w_text_runs(text, bold=False):
    """一段文本 -> 若干 <w:r>；\n 用 <w:br/> 表示。"""
    rpr = "<w:rPr><w:b/></w:rPr>" if bold else ""
    parts = []
    for i, line in enumerate(str(text).split("\n")):
        if i:
            parts.append("<w:r>%s<w:br/></w:r>" % rpr)
        parts.append('<w:r>%s<w:t xml:space="preserve">%s</w:t></w:r>' % (rpr, _esc(line)))
    return "".join(parts)


def _w_p(text, style=None, indent=0, bold=False):
    ppr = ""
    if style:
        ppr += '<w:pStyle w:val="%s"/>' % style
    if indent:
        ppr += '<w:ind w:left="%d"/>' % int(indent)
    head = "<w:pPr>%s</w:pPr>" % ppr if ppr else ""
    return "<w:p>%s%s</w:p>" % (head, _w_text_runs(text, bold=bold))


def _w_table(rows):
    rows = [_as_list(r) for r in _as_list(rows) if _as_list(r)]
    if not rows:
        return ""
    out = ['<w:tbl><w:tblPr><w:tblStyle w:val="TableGrid"/>'
           '<w:tblW w:w="0" w:type="auto"/></w:tblPr>']
    for ri, row in enumerate(rows):
        out.append("<w:tr>")
        for cell in row:
            out.append("<w:tc><w:tcPr><w:tcW w:w=\"0\" w:type=\"auto\"/></w:tcPr>%s</w:tc>"
                       % _w_p(str(cell)[:MAX_CELL_CHARS], bold=(ri == 0)))
        out.append("</w:tr>")
    out.append("</w:tbl>")
    # 表格后面补一个空段，避免两张表贴在一起（Word 会把它们合并）
    out.append("<w:p/>")
    return "".join(out)


def _docx_bytes(title, blocks):
    body = []
    if title:
        body.append(_w_p(title, style="Heading1"))
    for b in _norm_blocks(blocks):
        kind = str(b.get("type") or b.get("kind") or "paragraph").strip().lower()
        text = b.get("text")
        if kind in ("title", "h1", "heading", "heading1"):
            body.append(_w_p(text, style="Heading1"))
        elif kind in ("h2", "heading2"):
            body.append(_w_p(text, style="Heading2"))
        elif kind in ("h3", "heading3"):
            body.append(_w_p(text, style="Heading3"))
        elif kind in ("h4", "heading4"):
            body.append(_w_p(text, style="Heading4"))
        elif kind in ("bullet", "list", "li", "item"):
            for line in str(text or "").split("\n"):
                if line.strip():
                    body.append(_w_p("• " + line.strip(), indent=360))
        elif kind in ("number", "ordered"):
            for i, line in enumerate(str(text or "").split("\n")):
                if line.strip():
                    body.append(_w_p("%d. %s" % (i + 1, line.strip()), indent=360))
        elif kind in ("table", "grid"):
            body.append(_w_table(b.get("rows")))
        elif kind in ("pagebreak", "page_break", "hr"):
            body.append('<w:p><w:r><w:br w:type="page"/></w:r></w:p>')
        else:
            body.append(_w_p(text if text is not None else ""))
    sect = ('<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
            '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"'
            ' w:header="851" w:footer="992" w:gutter="0"/></w:sectPr>')
    doc = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           "<w:body>%s%s</w:body></w:document>" % ("".join(body), sect))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _DOCX_CT)
        z.writestr("_rels/.rels", _ROOT_RELS)
        z.writestr("word/_rels/document.xml.rels", _DOC_RELS)
        z.writestr("word/document.xml", doc)
        z.writestr("word/styles.xml", _DOCX_STYLES)
    return buf.getvalue()


# ============================================================
# xlsx（SpreadsheetML）
# ============================================================

_XLSX_CT_HEAD = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>"""


def _col_name(idx):
    """0 -> A, 25 -> Z, 26 -> AA"""
    s = ""
    n = int(idx)
    while True:
        s = chr(65 + n % 26) + s
        n = n // 26 - 1
        if n < 0:
            return s


def _cell_xml(ref, value):
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return '<c r="%s" t="b"><v>%d</v></c>' % (ref, 1 if value else 0)
    if isinstance(value, (int, float)):
        return '<c r="%s"><v>%s</v></c>' % (ref, value)
    text = _esc(str(value)[:MAX_CELL_CHARS])
    return '<c r="%s" t="inlineStr"><is><t xml:space="preserve">%s</t></is></c>' % (ref, text)


def _xlsx_bytes(_title, sheets, blocks=None):
    if not sheets and blocks:
        # 也接受 blocks 里的表格：每个 table 一张 sheet
        sheets = [{"name": "Sheet%d" % (i + 1), "rows": b.get("rows")}
                  for i, b in enumerate(_norm_blocks(blocks))
                  if str(b.get("type") or "").lower() in ("table", "grid")]
    sheets = [s for s in _as_list(sheets) if isinstance(s, dict)]
    if not sheets:
        raise ValueError("xlsx 需要 sheets（[{name, rows:[[...]]}]）或 blocks 里的 table")
    if len(sheets) > 50:
        raise ValueError("sheet 太多（上限 50）")

    names, sheet_xml, rels, ct = [], [], [], []
    used = set()
    for i, sh in enumerate(sheets):
        raw_name = re.sub(r"[\[\]:*?/\\]", "_", str(sh.get("name") or "Sheet%d" % (i + 1)))
        name = (raw_name.strip() or "Sheet%d" % (i + 1))[:31]
        while name.lower() in used:
            name = (name[:28] + "_%d" % (i + 1))[:31]
        used.add(name.lower())
        names.append(name)
        rows = _as_list(sh.get("rows"))
        body = []
        for ri, row in enumerate(rows):
            cells = []
            for ci, v in enumerate(_as_list(row)):
                cells.append(_cell_xml("%s%d" % (_col_name(ci), ri + 1), v))
            if cells:
                body.append('<row r="%d">%s</row>' % (ri + 1, "".join(cells)))
        sheet_xml.append(
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            "<sheetData>%s</sheetData></worksheet>" % "".join(body))
        rels.append('<Relationship Id="rId%d" '
                    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                    'Target="worksheets/sheet%d.xml"/>' % (i + 1, i + 1))
        ct.append('<Override PartName="/xl/worksheets/sheet%d.xml" '
                  'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                  % (i + 1))

    workbook = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                "<sheets>%s</sheets></workbook>"
                % "".join('<sheet name="%s" sheetId="%d" r:id="rId%d"/>'
                          % (_esc(n), i + 1, i + 1) for i, n in enumerate(names)))
    wb_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
               '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
               "%s</Relationships>" % "".join(rels))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _XLSX_CT_HEAD + "".join(ct) + "</Types>")
        z.writestr("_rels/.rels", _ROOT_RELS.replace("word/document.xml", "xl/workbook.xml"))
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        for i, xml in enumerate(sheet_xml):
            z.writestr("xl/worksheets/sheet%d.xml" % (i + 1), xml)
    return buf.getvalue()


# ============================================================
# pdf（reportlab）
# ============================================================


def _pdf_bytes(title, blocks):
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.platypus import (ListFlowable, ListItem, PageBreak, Paragraph,
                                        SimpleDocTemplate, Spacer, Table, TableStyle)
    except Exception:                       # noqa: BLE001
        raise ValueError("缺少 reportlab，无法生成 PDF：请先 pip install reportlab")
    from app import reader_export as rx
    if not rx._ensure_fonts():
        raise ValueError("找不到可用的中文字体（需要 Noto / 宋体 / 黑体之一）")
    body_font, head_font = rx.BODY_FONT, rx.HEAD_FONT
    st_normal = ParagraphStyle("gmBody", fontName=body_font, fontSize=11, leading=17,
                               spaceAfter=6)
    st_heads = {
        1: ParagraphStyle("gmH1", fontName=head_font, fontSize=20, leading=28,
                          spaceBefore=6, spaceAfter=10),
        2: ParagraphStyle("gmH2", fontName=head_font, fontSize=16, leading=23,
                          spaceBefore=10, spaceAfter=8),
        3: ParagraphStyle("gmH3", fontName=head_font, fontSize=13.5, leading=20,
                          spaceBefore=8, spaceAfter=6),
        4: ParagraphStyle("gmH4", fontName=head_font, fontSize=12, leading=18,
                          spaceBefore=6, spaceAfter=4),
    }

    def para(text, style=None):
        safe = escape(_ILLEGAL_XML.sub("", str(text or "")))
        return Paragraph(safe.replace("\n", "<br/>"), style or st_normal)

    flow = []
    if title:
        flow.append(para(title, st_heads[1]))
    for b in _norm_blocks(blocks):
        kind = str(b.get("type") or b.get("kind") or "paragraph").strip().lower()
        text = b.get("text")
        if kind in ("title", "h1", "heading", "heading1"):
            flow.append(para(text, st_heads[1]))
        elif kind in ("h2", "heading2"):
            flow.append(para(text, st_heads[2]))
        elif kind in ("h3", "heading3"):
            flow.append(para(text, st_heads[3]))
        elif kind in ("h4", "heading4"):
            flow.append(para(text, st_heads[4]))
        elif kind in ("bullet", "list", "li", "item"):
            items = [ListItem(para(x.strip()), leftIndent=12)
                     for x in str(text or "").split("\n") if x.strip()]
            if items:
                flow.append(ListFlowable(items, bulletType="bullet", start="•",
                                         leftIndent=16))
        elif kind in ("number", "ordered"):
            items = [ListItem(para(x.strip()), leftIndent=12)
                     for x in str(text or "").split("\n") if x.strip()]
            if items:
                flow.append(ListFlowable(items, bulletType="1", leftIndent=16))
        elif kind in ("table", "grid"):
            rows = [r for r in (_as_list(x) for x in _as_list(b.get("rows"))) if r]
            if rows:
                data = [[para(str(c)[:MAX_CELL_CHARS], st_normal) for c in r] for r in rows]
                width = (A4[0] - 40 * mm) / max(1, max(len(r) for r in rows))
                table = Table(data, colWidths=[width] * max(len(r) for r in rows))
                table.setStyle(TableStyle([
                    ("GRID", (0, 0), (-1, -1), 0.5, "#999999"),
                    ("BACKGROUND", (0, 0), (-1, 0), "#f2f4f7"),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ]))
                flow.append(table)
                flow.append(Spacer(1, 8))
        elif kind in ("pagebreak", "page_break"):
            flow.append(PageBreak())
        elif kind in ("hr",):
            flow.append(Spacer(1, 10))
        else:
            flow.append(para(text if text is not None else ""))
    if not flow:
        raise ValueError("没有内容：请给 title 或 blocks")

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm,
                            topMargin=18 * mm, bottomMargin=18 * mm,
                            title=title or "document")
    doc.build(flow)
    return buf.getvalue()


# ============================================================
# pptx（PresentationML）
# ============================================================

_PPTX_NS = ('xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
            'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"')

_PPTX_CT_HEAD = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>
<Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/>
<Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>
<Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>"""

_PPTX_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/>
</Relationships>"""

_PPTX_THEME = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="Office">
<a:themeElements>
<a:clrScheme name="Office">
<a:dk1><a:sysClr val="windowText" lastClr="000000"/></a:dk1><a:lt1><a:sysClr val="window" lastClr="FFFFFF"/></a:lt1>
<a:dk2><a:srgbClr val="44546A"/></a:dk2><a:lt2><a:srgbClr val="E7E6E6"/></a:lt2>
<a:accent1><a:srgbClr val="4472C4"/></a:accent1><a:accent2><a:srgbClr val="ED7D31"/></a:accent2>
<a:accent3><a:srgbClr val="A5A5A5"/></a:accent3><a:accent4><a:srgbClr val="FFC000"/></a:accent4>
<a:accent5><a:srgbClr val="5B9BD5"/></a:accent5><a:accent6><a:srgbClr val="70AD47"/></a:accent6>
<a:hlink><a:srgbClr val="0563C1"/></a:hlink><a:folHlink><a:srgbClr val="954F72"/></a:folHlink>
</a:clrScheme>
<a:fontScheme name="Office">
<a:majorFont><a:latin typeface="Calibri Light"/><a:ea typeface="Microsoft YaHei"/><a:cs typeface=""/></a:majorFont>
<a:minorFont><a:latin typeface="Calibri"/><a:ea typeface="Microsoft YaHei"/><a:cs typeface=""/></a:minorFont>
</a:fontScheme>
<a:fmtScheme name="Office">
<a:fillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:fillStyleLst>
<a:lnStyleLst><a:ln w="6350" cap="flat" cmpd="sng" algn="ctr"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:prstDash val="solid"/></a:ln><a:ln w="12700" cap="flat" cmpd="sng" algn="ctr"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:prstDash val="solid"/></a:ln><a:ln w="19050" cap="flat" cmpd="sng" algn="ctr"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:prstDash val="solid"/></a:ln></a:lnStyleLst>
<a:effectStyleLst><a:effectStyle><a:effectLst/></a:effectStyle><a:effectStyle><a:effectLst/></a:effectStyle><a:effectStyle><a:effectLst/></a:effectStyle></a:effectStyleLst>
<a:bgFillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:bgFillStyleLst>
</a:fmtScheme>
</a:themeElements>
</a:theme>"""

_PPTX_MASTER = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sldMaster %s>
<p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>
</p:spTree></p:cSld>
<p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/>
<p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst>
<p:txStyles>
<p:titleStyle><a:lvl1pPr algn="l"><a:defRPr sz="3600" b="1"><a:solidFill><a:schemeClr val="tx1"/></a:solidFill><a:latin typeface="+mj-lt"/></a:defRPr></a:lvl1pPr></p:titleStyle>
<p:bodyStyle><a:lvl1pPr marL="342900" indent="-342900"><a:buFont typeface="Arial"/><a:buChar char="&#8226;"/><a:defRPr sz="2000"/></a:lvl1pPr></p:bodyStyle>
<p:otherStyle><a:lvl1pPr><a:defRPr sz="1800"/></a:lvl1pPr></p:otherStyle>
</p:txStyles>
</p:sldMaster>""" % _PPTX_NS

_PPTX_LAYOUT = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sldLayout %s type="blank" preserve="1">
<p:cSld name="Blank"><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>
</p:spTree></p:cSld>
<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>
</p:sldLayout>""" % _PPTX_NS

_PPTX_MASTER_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="../theme/theme1.xml"/>
</Relationships>"""

_PPTX_LAYOUT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="../slideMasters/slideMaster1.xml"/>
</Relationships>"""

_PPTX_SLIDE_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>
</Relationships>"""

_PPTX_MAX_SLIDES = 60
_PPTX_MAX_BULLETS = 30


def _pptx_slides(title, blocks):
    """把 blocks 变成 [{title, bullets}]：h1/heading/title 开新页，其余进当前页要点。"""
    slides = []
    if title:
        slides.append({"title": title, "bullets": []})
    for b in _norm_blocks(blocks):
        kind = str(b.get("type") or b.get("kind") or "paragraph").strip().lower()
        if kind == "slide":
            slides.append({"title": str(b.get("title") or ""),
                           "bullets": [str(x) for x in _as_list(b.get("bullets"))]})
        elif kind in ("title", "h1", "heading", "heading1", "h2", "heading2", "section"):
            slides.append({"title": str(b.get("text") or ""), "bullets": []})
        elif kind in ("table", "grid"):
            if not slides:
                slides.append({"title": "", "bullets": []})
            rows = [r for r in (_as_list(x) for x in _as_list(b.get("rows"))) if r]
            slides[-1]["bullets"].extend(" | ".join(str(c) for c in r) for r in rows)
        else:
            text = str(b.get("text") or "")
            if not slides:
                slides.append({"title": "", "bullets": []})
            lines = [x.strip() for x in text.split("\n") if x.strip()] or [""]
            slides[-1]["bullets"].extend(lines)
    slides = [s for s in slides if str(s["title"]).strip() or s["bullets"]]
    if not slides:
        raise ValueError("pptx 没有内容：给 title，或 blocks 里用 heading / slide 分页")
    if len(slides) > _PPTX_MAX_SLIDES:
        raise ValueError("页数太多（%d，上限 %d）" % (len(slides), _PPTX_MAX_SLIDES))
    return slides


def _p_text_shape(sid, name, x, y, cx, cy, text, size, bold=False):
    runs = ""
    for i, line in enumerate(str(text).split("\n")):
        if i:
            runs += "<a:br/>"
        runs += ('<a:r><a:rPr lang="zh-CN" sz="%d"%s/><a:t>%s</a:t></a:r>'
                 % (size, ' b="1"' if bold else "", _esc(line)))
    # 标题用真正的 title 占位符：PowerPoint 的大纲视图 / 辅助功能才认得出
    ph = '<p:ph type="title"/>' if bold else "<p:ph/>"
    return ('<p:sp><p:nvSpPr><p:cNvPr id="%d" name="%s"/><p:cNvSpPr><a:spLocks noGrp="1"/>'
            '</p:cNvSpPr><p:nvPr>%s</p:nvPr></p:nvSpPr>'
            '<p:spPr><a:xfrm><a:off x="%d" y="%d"/><a:ext cx="%d" cy="%d"/></a:xfrm></p:spPr>'
            '<p:txBody><a:bodyPr%s/><a:lstStyle/><a:p>%s</a:p></p:txBody></p:sp>'
            % (sid, name, ph, x, y, cx, cy, ' anchor="b"' if bold else "", runs))


def _p_bullets_shape(sid, bullets, x, y, cx, cy):
    paras = []
    for line in bullets[:_PPTX_MAX_BULLETS]:
        paras.append('<a:p><a:pPr lvl="0"><a:buFont typeface="Arial"/><a:buChar char="&#8226;"/>'
                     '</a:pPr><a:r><a:rPr lang="zh-CN" sz="2000"/><a:t>%s</a:t></a:r></a:p>'
                     % _esc(str(line)[:MAX_CELL_CHARS]))
    return ('<p:sp><p:nvSpPr><p:cNvPr id="%d" name="Content"/><p:cNvSpPr><a:spLocks noGrp="1"/>'
            '</p:cNvSpPr><p:nvPr><p:ph type="body" idx="1"/></p:nvPr></p:nvSpPr>'
            '<p:spPr><a:xfrm><a:off x="%d" y="%d"/><a:ext cx="%d" cy="%d"/></a:xfrm></p:spPr>'
            '<p:txBody><a:bodyPr/><a:lstStyle/>%s</p:txBody></p:sp>'
            % (sid, x, y, cx, cy, "".join(paras)))


def _pptx_bytes(title, blocks):
    slides = _pptx_slides(title, blocks)
    buf = io.BytesIO()
    slide_entries, ct, rels = [], [], []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for i, s in enumerate(slides):
            n = i + 1
            shapes = []
            if str(s["title"]).strip():
                shapes.append(_p_text_shape(2, "Title", 838200, 365125,
                                            10515600, 1325563, s["title"], 3600, bold=True))
            if s["bullets"]:
                top = 1825625 if str(s["title"]).strip() else 838200
                shapes.append(_p_bullets_shape(3, s["bullets"], 838200, top,
                                               10515600, 6858000 - top - 457200))
            xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<p:sld %s><p:cSld><p:spTree>'
                   '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
                   '<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/>'
                   '<a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>'
                   "%s</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>"
                   % (_PPTX_NS, "".join(shapes)))
            z.writestr("ppt/slides/slide%d.xml" % n, xml)
            z.writestr("ppt/slides/_rels/slide%d.xml.rels" % n, _PPTX_SLIDE_RELS)
            slide_entries.append('<p:sldId id="%d" r:id="rId%d"/>' % (255 + n, n + 1))
            ct.append('<Override PartName="/ppt/slides/slide%d.xml" '
                      'ContentType="application/vnd.openxmlformats-officedocument.'
                      'presentationml.slide+xml"/>' % n)
            rels.append('<Relationship Id="rId%d" '
                        'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                        'relationships/slide" Target="slides/slide%d.xml"/>' % (n + 1, n))
        presentation = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                        '<p:presentation %s><p:sldMasterIdLst>'
                        '<p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>'
                        '<p:sldIdLst>%s</p:sldIdLst>'
                        '<p:sldSz cx="12192000" cy="6858000"/><p:notesSz cx="6858000" cy="9144000"/>'
                        '</p:presentation>' % (_PPTX_NS, "".join(slide_entries)))
        pres_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                     '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                     '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/'
                     '2006/relationships/slideMaster" Target="slideMasters/slideMaster1.xml"/>'
                     "%s</Relationships>" % "".join(rels))
        z.writestr("[Content_Types].xml", _PPTX_CT_HEAD + "".join(ct) + "</Types>")
        z.writestr("_rels/.rels", _PPTX_ROOT_RELS)
        z.writestr("ppt/presentation.xml", presentation)
        z.writestr("ppt/_rels/presentation.xml.rels", pres_rels)
        z.writestr("ppt/slideMasters/slideMaster1.xml", _PPTX_MASTER)
        z.writestr("ppt/slideMasters/_rels/slideMaster1.xml.rels", _PPTX_MASTER_RELS)
        z.writestr("ppt/slideLayouts/slideLayout1.xml", _PPTX_LAYOUT)
        z.writestr("ppt/slideLayouts/_rels/slideLayout1.xml.rels", _PPTX_LAYOUT_RELS)
        z.writestr("ppt/theme/theme1.xml", _PPTX_THEME)
    return buf.getvalue()


# ============================================================
# 工具入口
# ============================================================


def _t_doc_create(root, args):
    from .sandbox import _rel, resolve_path
    from .undo import _snap

    args = args if isinstance(args, dict) else {}
    dest = str(args.get("path") or "").strip()
    if not dest:
        return "❌ 缺少 path（沙箱内目标文件，扩展名 .docx / .xlsx / .pdf）", None
    ext = os.path.splitext(dest)[1].lower()
    if ext not in (".docx", ".xlsx", ".pdf", ".pptx"):
        return ("❌ 不支持的扩展名 %s：doc_create 只写 .docx / .xlsx / .pdf / .pptx；"
                "纯文本 / Markdown 请用 write_file" % (ext or "（无）")), None
    p = resolve_path(root, dest)
    if os.path.isdir(p):
        return "❌ 目标是目录，不能写入：" + _rel(root, p), None
    if os.path.exists(p) and not bool(args.get("overwrite")):
        return ("❌ 目标已存在：%s。确认覆盖请传 overwrite=true" % _rel(root, p)), None

    title = str(args.get("title") or "").strip()
    blocks = args.get("blocks")
    try:
        if ext == ".docx":
            data = _docx_bytes(title, blocks)
        elif ext == ".xlsx":
            data = _xlsx_bytes(title, args.get("sheets"), blocks)
        elif ext == ".pptx":
            data = _pptx_bytes(title, blocks)
        else:
            data = _pdf_bytes(title, blocks)
    except ValueError as e:
        return "❌ " + str(e), None
    except Exception as e:                  # noqa: BLE001
        return "❌ 生成失败：%s" % e, None

    parent = os.path.dirname(p)
    undo_op = _snap(p)
    if parent:
        os.makedirs(parent, exist_ok=True)
    # 先写同目录临时文件再 os.replace：中途失败不留半截文件
    tmp = p + ".part-" + uuid.uuid4().hex[:8]
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, p)
    except Exception as e:                  # noqa: BLE001
        try:
            os.remove(tmp)
        except OSError:
            pass
        return "❌ 写入失败：%s" % e, None

    kind = {".docx": "Word 文档", ".xlsx": "Excel 表格", ".pdf": "PDF",
            ".pptx": "PPT 演示文稿"}[ext]
    return ("已%s%s：%s（%d KB）\n用户在沙箱里直接打开即可。"
            % ("覆盖" if undo_op.get("existed") else "生成", kind, _rel(root, p),
               max(1, len(data) // 1024))), [undo_op]
