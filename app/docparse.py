"""文档附件解析：把上传的文档抽成纯文本，喂给聊天模型。

依赖策略：只用标准库，PDF 才需要可选依赖。
- txt / md / json / 代码等纯文本：探测编码后直接解码
- csv / tsv：解析成 Markdown 表格（限行限列），避免原始分隔符导致列错位
- docx / xlsx / pptx：Office 文件本身就是 zip，用 zipfile + 正则取 XML 文本
- pdf：pypdf 抽文本；抽不到（扫描件）或抽出来明显是乱码（中文 CID 字体）时，
  交给视觉模型**逐页识图**（渲染用 pypdfium2，自带 PDFium，无需额外系统组件）

PDF 三个坑（都已在实现里处理）：
1. **扫描件没有文本层** —— extract_text() 返回空，必须渲染成图片走视觉识别；
2. **中文 CID 字体抽取乱码/丢字** —— 用 _sane_ratio 判定，同样转视觉识别；
3. **超长扫描件烧额度** —— 逐页识别有页数上限（PDF_MAX_OCR_PAGES）。
"""
import csv
import io
import os
import re
import zipfile

MAX_CHARS = 0              # 0 = 不截断（已放开抽取上限；超长文档可能触发模型上下文报错）

# ---- CSV / TSV 结构化上限 ----
MAX_CSV_ROWS = 200         # 最多转多少行为 Markdown 表格（超出只给前 N 行 + 提示）
MAX_CSV_COLS = 20          # 最多保留多少列

# ---- PDF 相关参数 ----
PDF_TEXT_MIN_PER_PAGE = 5    # 每页平均正常字符少于这个数 → 认定「没有文本层」
PDF_SANE_MIN = 0.60          # 抽取结果的「正常字符」占比低于此 → 认定乱码，转视觉
PDF_MAX_OCR_PAGES = 60       # 走视觉识别时最多处理多少页（控制耗时与 token 花费）
PDF_RENDER_SCALE = 2.0       # 渲染倍率（2.0 ≈ 144dpi，够 OCR 用且体积可控）
# 扫描件逐页识图时的提示词：逐字、保顺序、表格/公式可读，禁止总结
PDF_PAGE_OCR_PROMPT = (
    "这是 PDF 的一页。请把这一页的全部文字**逐字**识别出来，"
    "保持原有顺序与分段；表格用 Markdown 表格、公式用 LaTeX。"
    "不要总结、不要补充、不要遗漏。")
PDF_JPEG_QUALITY = 80

# 明确按纯文本处理的扩展名
TEXT_EXT = {
    ".txt", ".md", ".markdown", ".tex", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml",
    ".log", ".ini", ".conf", ".env", ".html", ".htm", ".xml", ".css", ".js", ".mjs",
    ".ts", ".tsx", ".jsx", ".py", ".java", ".c", ".h", ".cpp", ".hpp", ".cs", ".go",
    ".rs", ".rb", ".php", ".sh", ".bat", ".ps1", ".sql", ".lua", ".kt", ".swift",
}


def _decode(raw):
    """尽力猜编码。顺序有讲究（实测 charset_normalizer 对短样本很容易猜错，
    会把 GBK 的「中文测试」猜成 cp949、把 Latin-1 猜成 cp1006），所以：
    utf-8 严格 → gb18030 → big5 → charset_normalizer → utf-8 容错。"""
    try:
        return raw.decode("utf-8")           # 现代文件绝大多数是 utf-8，且严格解码自带校验
    except UnicodeDecodeError:
        pass
    for enc in ("gb18030", "big5"):          # 中文旧文件基本是这两套（gb18030 是 GBK/GB2312 的超集）
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    try:
        from charset_normalizer import from_bytes
        best = from_bytes(raw[:400000]).best()
        if best is not None:
            return str(best)
    except Exception:
        pass
    return raw.decode("utf-8", errors="replace")


# 「正常文字」判定：ASCII 可见字符 / 中日韩汉字与标点 / 假名 / 谚文
def _is_sane(ch):
    o = ord(ch)
    return (o >= 0x20 and o < 0x7F) or o in (9, 10, 13) or \
           0x4E00 <= o <= 0x9FFF or 0x3000 <= o <= 0x303F or \
           0xFF00 <= o <= 0xFFEF or 0x3040 <= o <= 0x30FF or 0xAC00 <= o <= 0xD7AF


def _sane_ratio(text, n=1500):
    head = text[:n]
    if not head:
        return 1.0
    return sum(1 for ch in head if _is_sane(ch)) / len(head)



def _unescape(s):
    s = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), s)          # 数字实体 &#38;
    s = re.sub(r"&#x([0-9a-fA-F]+);", lambda m: chr(int(m.group(1), 16)), s)
    return (s.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
             .replace("&apos;", "'").replace("&amp;", "&"))             # &amp; 必须最后处理


def _docx_text(raw):
    """docx：正文在 word/document.xml，段落 </w:p> 当换行"""
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        names = z.namelist()
        parts = [n for n in ("word/document.xml", "word/header1.xml", "word/footer1.xml") if n in names]
        out = []
        for name in parts:
            xml = z.read(name).decode("utf-8", errors="replace")
            xml = re.sub(r"</w:p>", "\n", xml)
            xml = re.sub(r"<w:tab[^>]*/>", "\t", xml)
            xml = re.sub(r"<w:br[^>]*/>", "\n", xml)
            xml = re.sub(r"<[^>]+>", "", xml)
            out.append(xml)
        return "\n".join(out)


def _xlsx_text(raw):
    """xlsx：共享字符串表 + 各工作表，按行输出制表符分隔的文本"""
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        names = z.namelist()
        shared = []
        if "xl/sharedStrings.xml" in names:
            sx = z.read("xl/sharedStrings.xml").decode("utf-8", errors="replace")
            for si in re.findall(r"<si>(.*?)</si>", sx, re.S):
                shared.append("".join(re.findall(r"<t[^>]*>(.*?)</t>", si, re.S)))
        sheets = sorted(n for n in names if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
        out = []
        for i, sn in enumerate(sheets, 1):
            sx = z.read(sn).decode("utf-8", errors="replace")
            out.append("### 工作表 %d" % i)
            for row in re.findall(r"<row[^>]*>(.*?)</row>", sx, re.S):
                cells = []
                for c in re.findall(r"<c\b[^>]*?(?:/>|>.*?</c>)", row, re.S):
                    t = re.search(r't="([^"]+)"', c)
                    v = re.search(r"<v>(.*?)</v>", c, re.S)
                    if not v:
                        it = re.search(r"<is>.*?<t[^>]*>(.*?)</t>", c, re.S)
                        cells.append(it.group(1) if it else "")
                        continue
                    val = v.group(1)
                    if t and t.group(1) == "s":            # t="s" 表示值是共享字符串的下标
                        try:
                            val = shared[int(val)]
                        except (ValueError, IndexError):
                            pass
                    cells.append(val)
                out.append("\t".join(cells))
        return "\n".join(out)


def _pptx_text(raw):
    """pptx：每页是 ppt/slides/slideN.xml，正文在 <a:t> 里。
    只用 zipfile + 正则，不依赖 python-pptx（与 docx / xlsx 的做法一致）。"""
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        slides = []
        for n in z.namelist():
            m = re.match(r"ppt/slides/slide(\d+)\.xml$", n)
            if m:
                slides.append((int(m.group(1)), n))
        slides.sort()
        if not slides:
            return ""
        out = []
        has_text = False
        for idx, name in slides:
            xml = z.read(name).decode("utf-8", errors="replace")
            texts = re.findall(r"<a:t[^>]*>(.*?)</a:t>", xml, re.S)
            body = "\n".join(_unescape(t).strip() for t in texts if t.strip())
            if body:
                has_text = True
            out.append("### 幻灯片 %d\n%s" % (idx, body or "（本页无文本）"))
        # 整份都是图片 / 空页时返回空串，让 extract 给出「没有可提取的文本」，
        # 而不是给模型一堆只有标题的空壳
        return "\n\n".join(out) if has_text else ""


def _csv_table(raw, ext):
    """csv / tsv：解析成 Markdown 表格，限制行列避免一次塞满上下文。

    返回 (text, warn)。纯文本解码出来的原始表格读起来很容易错位，
    转成表格后模型才能正确对齐「哪一列是什么」。
    """
    text = _decode(raw)
    delim = "\t" if ext == ".tsv" else ","
    try:
        rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    except Exception as e:
        return None, "表格解析失败：%s" % e
    rows = [r for r in rows if any(str(c).strip() for c in r)]
    if not rows:
        return None, "表格是空的"
    total_rows, total_cols = len(rows), max(len(r) for r in rows)
    head = rows[0][:MAX_CSV_COLS]
    body = rows[1:MAX_CSV_ROWS + 1]
    warn = ""
    if total_rows > MAX_CSV_ROWS + 1 or total_cols > MAX_CSV_COLS:
        warn = ("表格共 %d 行 × %d 列，只显示了前 %d 行、前 %d 列。"
                % (total_rows, total_cols, min(len(body), MAX_CSV_ROWS), len(head)))

    def cell(x):
        return str(x or "").replace("|", "\\|").replace("\n", " ").strip()

    lines = ["| " + " | ".join(cell(c) for c in head) + " |",
             "| " + " | ".join("---" for _ in head) + " |"]
    for r in body:
        cells = [cell(c) for c in r[:len(head)]]
        cells += [""] * (len(head) - len(cells))
        lines.append("| " + " | ".join(cells) + " |")
    if warn:
        lines.insert(0, "（" + warn + "）\n")
    return "\n".join(lines), warn


def _pdfium_text_pages(raw, progress=None):
    """pypdfium2 兜底抽文本（pypdf 抽不到时用）。返回每页文本列表。"""
    try:
        import pypdfium2 as pdfium
    except Exception:
        return []
    try:
        doc = pdfium.PdfDocument(raw)
        total = len(doc)
        out = []
        for i in range(total):
            if progress:
                try:
                    progress(i + 1, total, "text")
                except Exception:            # noqa: BLE001  进度回调不该影响解析
                    pass
            try:
                out.append(doc[i].get_textpage().get_text_range() or "")
            except Exception:
                out.append("")
        return out
    except Exception:
        return []


def _pdf_text(raw, progress=None):
    """抽取 PDF 文本。返回 (text, warn, needs_ocr)。

    needs_ocr=True 表示「没有文本层」或「抽出来是乱码」—— 调用方应改走视觉识别。
    progress(done, total, stage)：逐页读取 / 识别进度，供前端显示（可选）。
    """
    pages, warn = [], ""
    try:
        from pypdf import PdfReader
    except Exception:
        warn = "未安装 pypdf"
    else:
        try:
            reader = PdfReader(io.BytesIO(raw))
            total = len(reader.pages)
            for i, pg in enumerate(reader.pages):
                if progress:
                    try:
                        progress(i + 1, total, "text")
                    except Exception:        # noqa: BLE001
                        pass
                try:
                    pages.append(pg.extract_text() or "")
                except Exception:
                    pages.append("")          # 单页失败不影响整体
        except Exception as e:
            warn = "pypdf 解析失败（%s）" % e
            pages = []

    if not any(p.strip() for p in pages):      # 一个字都没抽到 → 换 pypdfium2 再试
        alt = _pdfium_text_pages(raw, progress=progress)
        if any(p.strip() for p in alt):
            pages = alt
            warn = ""

    if not pages:
        return None, (warn or "无法解析该 PDF") + \
            "。请执行：.venv\\Scripts\\pip install pypdf pypdfium2", False

    joined = "\n\n".join(p.strip() for p in pages if p.strip())
    # 两种情况都要转视觉：① 几乎没有文字（扫描件）② 抽出来不是正常文字（CID 乱码）
    # 阈值取「每页 5 字」——只为抓真正没文本层的页，别把短文档（封面 / 票据）也误判
    if len(joined) < PDF_TEXT_MIN_PER_PAGE * len(pages) or _sane_ratio(joined) < PDF_SANE_MIN:
        return (joined or None), warn, True
    return joined, warn, False


def _pdf_render_pages(raw, max_pages=PDF_MAX_OCR_PAGES, progress=None):
    """把 PDF 逐页渲染成 JPEG 字节，供视觉模型识别。返回 ([(页码, jpeg), …], 总页数)。"""
    try:
        import pypdfium2 as pdfium
    except Exception:
        return [], 0
    try:
        doc = pdfium.PdfDocument(raw)
        total = len(doc)
    except Exception:
        return [], 0
    out = []
    limit = min(total, max_pages)
    for i in range(limit):
        if progress:
            try:
                progress(i + 1, limit, "render")
            except Exception:            # noqa: BLE001  进度回调不该影响解析
                pass
        try:
            pil = doc[i].render(scale=PDF_RENDER_SCALE).to_pil().convert("RGB")
            buf = io.BytesIO()
            pil.save(buf, "JPEG", quality=PDF_JPEG_QUALITY)
            out.append((i + 1, buf.getvalue()))
        except Exception:
            continue
    return out, total


def _pdf_ocr(raw, ocr, max_pages=PDF_MAX_OCR_PAGES, progress=None):
    """扫描件 / 乱码 PDF：逐页交给视觉模型。ocr(jpeg_bytes) -> 文本。

    progress(done, total, "ocr")：每页识别前回调一次，用于前端显示进度。
    返回 (text, warn, 成功页数)。
    """
    shots, total = _pdf_render_pages(raw, max_pages, progress=progress)
    if not shots:
        return "", "PDF 逐页渲染失败（缺少 pypdfium2，或文件已损坏）。", 0
    parts, ok, fail = [], 0, 0
    for pno, jpg in shots:
        if progress:
            try:
                progress(pno, total or len(shots), "ocr")
            except Exception:            # noqa: BLE001  进度回调不该影响解析
                pass
        try:
            t = (ocr(jpg) or "").strip()
        except Exception as e:
            print("[PDF] 第 %d 页识别异常：%s" % (pno, e))
            t = ""
        if t:
            ok += 1
            parts.append("【第 %d 页】\n%s" % (pno, t))
        else:
            fail += 1
            parts.append("【第 %d 页】\n（本页未识别出内容，可能是空白页或图片太模糊）" % pno)
    if not ok:
        return "", "该 PDF 没有文本层，逐页识别也没得到内容（可能是空白页或扫描质量太差）。", 0
    warn = "该 PDF 没有文本层（扫描件），已用视觉模型逐页识别：成功 %d 页%s。" % (
        ok, ("，失败 %d 页" % fail) if fail else "")
    if total > len(shots):
        warn += "文件共 %d 页，只识别了前 %d 页（可用页数上限 %d）。" % (
            total, len(shots), max_pages)
    return "\n\n".join(parts), warn, ok


def extract(name, raw, ocr=None, progress=None):
    """返回 (text, warn)。text 为 None 表示不支持或失败，warn 是给用户看的说明。

    ocr：可选回调，`ocr(jpeg_bytes) -> str`。传入后，PDF 若是扫描件或乱码，
    会自动逐页渲染并调用它做视觉识别（不传则只返回提示）。
    progress：可选回调，`progress(done, total, stage)`，stage 为 "text" / "ocr"。
    """
    if not raw:
        return None, "文件是空的"
    ext = os.path.splitext(str(name or ""))[1].lower()
    warn = ""
    if ext == ".docx":
        try:
            text = _unescape(_docx_text(raw))
        except Exception as e:
            return None, "docx 解析失败：" + str(e)
    elif ext in (".xlsx", ".xlsm"):
        try:
            text = _unescape(_xlsx_text(raw))
        except Exception as e:
            return None, "xlsx 解析失败：" + str(e)
    elif ext == ".pptx":
        try:
            text = _pptx_text(raw)
        except Exception as e:
            return None, "pptx 解析失败：" + str(e)
        if not text:
            return None, "这个 pptx 里没有可提取的文本（可能全是图片）"
    elif ext in (".csv", ".tsv"):
        text, cwarn = _csv_table(raw, ext)
        if text is None:
            return None, cwarn
        warn = cwarn
    elif ext == ".pdf":
        text, warn, needs_ocr = _pdf_text(raw, progress=progress)
        if needs_ocr:
            if ocr is not None:
                otext, owarn, okn = _pdf_ocr(raw, ocr, progress=progress)
                if okn:
                    return otext, owarn
                warn = owarn or warn
            if not text:
                return None, ("%s；" % warn if warn else "") + \
                    "该 PDF 没有可提取的文本层（可能是扫描件）。" \
                    "在「设置 → 视觉模型」或「聊天模型」里配好密钥后重试，即可自动逐页识图。"
        if text is None:
            return None, warn
    elif ext in (".doc", ".xls", ".ppt"):
        return None, "不支持旧版 Office 格式（%s），请先另存为 .docx / .xlsx / .pdf" % ext
    else:
        text = _decode(raw)
        if ext not in TEXT_EXT:
            # 扩展名不认识：看一眼二进制控制字符占比，太多就判定为不支持的类型
            head = text[:2000]
            bad = sum(1 for ch in head if ord(ch) < 9 or (13 < ord(ch) < 32))
            if bad > 40:
                return None, "无法识别的文件类型：%s" % (ext or "无扩展名")
        # 解码后「正常字符」占比过低 → 多半是编码猜错了，明确提醒（别让模型拿到乱码还一本正经回答）
        if _sane_ratio(text) < 0.80:
            warn = "这个文件的编码没有识别准，内容可能乱码；建议用 UTF-8 重新保存后再上传"
    text = (text or "").strip()
    if not text:
        return None, "没有提取到文本（可能是扫描件 / 空文件 / 纯图片文档）"
    if MAX_CHARS and len(text) > MAX_CHARS:
        return text[:MAX_CHARS], (warn + "；" if warn else "") + "文档较长，只取了前 %d 字" % MAX_CHARS
    return text, warn
