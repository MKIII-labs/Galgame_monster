# -*- coding: utf-8 -*-
"""PDF 沉浸式阅读：把 PDF 的文字层重建成「阅读流」，按段落做双语翻译。

为什么重排而不是在原页上盖译文：PDF 是固定版式，页面高度不可变，
覆盖层没有地方放译文，双栏和公式最容易错位。这里走「左原页 + 右阅读流」，
和 OnlyTranslate（只译）同一形态：原页保持 100% 真实，阅读流负责可读性。

版面重建设计（Phase 1，纯规则，不依赖模型）：
1. 逐字符取 bbox（pypdfium2），按基线聚成「行段」；水平空隙过大就断开，
   这样双栏页面的两栏不会粘成一行。
2. 用「中间零覆盖带 + 不允许行段跨越」判定双栏；整宽的行段（标题/摘要/跨栏图）
   单独成带（band），带内先左栏后右栏，避免左右栏交错。
3. 行段按行距/字号合并成段；页尾到页首的无标点续段跨页合并。
4. 公式：整段符号密度高 → 独立公式（前端裁图）；行内公式用 ⟦F1⟧ 占位符
   保护，翻译后原样还原。图表：取 PDF 里的图片对象 bbox，前端裁图。

缓存：
  data/reader/<doc_id>/blocks.json              版面（随文件 size/mtime 失效）
  data/reader/<doc_id>/t_<lang>_<model>.json    译文（逐段累积写入）
  data/reader/<doc_id>/crops/                   裁图（按 rect 哈希）
"""
import hashlib
import json
import os
import re
import threading
import time

from app import agent, docparse
from app.store import DATA_DIR, read_json, write_json

READER_DIR = os.path.join(DATA_DIR, "reader")

SCHEMA_VERSION = 26             # 版面算法有变就 +1，旧缓存自动失效
PROMPT_VERSION = "rd5"          # 提示词改了要 +1，否则旧译文会被当成新译文复用
MAX_PARSE_PAGES = 1000          # 解析上限（大文件基线 1000 页；再大会明显拖慢首次打开）
MAX_FILE_MB = 200
CROP_SCALE = 3.0                # 裁图渲染倍率（3x ≈ 216dpi，公式够清楚）
CROP_VERSION = 3                # 裁图算法变了就 +1，旧缓存失效
FORMULA_VERSION = 3             # 公式识别提示词/清洗规则变了就 +1
PAGE_SCALE = 2.0

# 公式/数学符号：用来算「这一段像不像公式」，以及保护行内公式
MATH_CHARS = set("=+-−×÷±∓≤≥≠≈≡∝∞∑∏∫∬∮√∂∇∈∉⊂⊃⊆⊇∪∩∀∃¬∧∨⇒⇔→←↔"
                 "αβγδεζηθικλμνξοπρστυφχψωΓΔΘΛΞΠΣΦΨΩ·^_{}[]()<>|/\\%")
# 「强符号」：出现就基本可以确定是数学（括号 / 斜杠 / 百分号太常见，不算）
MATH_STRONG = MATH_CHARS - set("()[]{}<>|/\\%^_")
# 常见数学函数名：单独出现（没有括号）也算公式
MATH_FUNCS = {"sin", "cos", "tan", "cot", "sec", "csc", "log", "ln", "lg",
              "exp", "lim", "max", "min", "sup", "inf", "det", "dim", "gcd",
              "mod", "arg", "Pr", "Var", "Cov", "tr", "rank", "deg"}
SENT_END = ".!?。！？；;:：”』」"
CJK_RANGE = re.compile(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]")
GARBAGE = re.compile(r"[\ufffd\ue000-\uf8ff\x00-\x08\x0b\x0c\x0e-\x1f]")


class ReaderError(Exception):
    """阅读器可预期的失败（文件不存在 / 不是 PDF / 加密等），调用方直接展示文案。"""


# ---------------------------------------------------------------- 缓存与文档

def _doc_id(path):
    """doc_id 取「绝对路径 + size + mtime」：比内容哈希快，文件一变就自动失效。"""
    st = os.stat(path)
    raw = "%s|%d|%d" % (os.path.abspath(path), st.st_size, int(st.st_mtime))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _doc_dir(doc_id):
    p = os.path.join(READER_DIR, doc_id)
    os.makedirs(p, exist_ok=True)
    return p


def _blocks_path(doc_id):
    return os.path.join(_doc_dir(doc_id), "blocks.json")


def _trans_path(doc_id, lang, model):
    safe = re.sub(r"[^0-9A-Za-z._-]+", "_", "%s_%s_%s" % (lang, model, PROMPT_VERSION))[:80]
    return os.path.join(_doc_dir(doc_id), "t_%s.json" % safe)


RICH_DOC_EXT = {".pdf", ".docx", ".pptx"}


def resolve(sandbox, rel):
    """沙箱内可阅读文档的绝对路径。只允许读，且必须真的是文件。"""
    try:
        p = agent.resolve_path(sandbox, rel, for_read=True)
    except agent.SandboxError as e:
        raise ReaderError(str(e))
    if os.path.isdir(p):
        raise ReaderError("这是目录，不是文件")
    if not os.path.isfile(p):
        raise ReaderError("文件不存在：" + str(rel))
    if os.path.splitext(p)[1].lower() not in RICH_DOC_EXT:
        raise ReaderError("只能打开 PDF / Word / PPT 文件")
    size = os.path.getsize(p)
    if size > MAX_FILE_MB * 1024 * 1024:
        raise ReaderError("文件过大（%.1f MB），暂不支持阅读" % (size / 1048576.0))
    return p


def read_pdf_bytes(sandbox, rel):
    p = resolve(sandbox, rel)
    with open(p, "rb") as f:
        return f.read()


# ---------------------------------------------------------------- 版面重建

def _extract_chars(page):
    """逐字符取 bbox，统一转成「左上角原点」（PDF 原生是左下角）。"""
    h = page.get_height()
    tp = page.get_textpage()
    kept, hidden, spaces = [], [], []
    try:
        n = tp.count_chars()
        for i in range(n):
            ch = tp.get_text_range(i, 1)
            if not ch or ch in ("\r", "\n"):
                continue
            try:
                x0, y0, x1, y1 = tp.get_charbox(i)
            except Exception:
                continue
            if ch in (" ", "\u00a0"):
                # 零面积空格不能丢：PDF 里词间空格大多没有宽度，
                # 只靠 x 间距补空格会把 "of Computer" 粘成 "ofComputer"。
                # 这里保留位置，_segments 再按行挂回去。
                spaces.append({"ch": " ", "x0": x0, "x1": x0,
                               "y0": h - y1, "y1": h - y1,
                               "size": 0.0, "space": True})
                continue
            if not ch.strip():
                continue
            w, hh = x1 - x0, y1 - y0
            if w <= 0.1 and hh <= 0.1:
                continue
            item = {"ch": ch, "x0": x0, "x1": x1,
                    "y0": h - y1, "y1": h - y0,
                    "size": max(1.0, float(hh))}
            # CVPR/ICCV 的 PDF 常带一层不可见的 LaTeX 源码（ActualText）：
            # 字符宽度只有 0.05pt，文本却是 \label{...}\begin{pmatrix}...
            # 留着会被当成正文/标题显示并送进翻译，必须在入口过滤。
            if hh >= 2.0 and w <= max(0.10, 0.07 * hh):
                hidden.append(item)
            else:
                kept.append(item)
    finally:
        try:
            tp.close()
        except Exception:
            pass
    # 整页都是不可见层（扫描 OCR 等）时不能全丢，否则读不到任何文字
    if hidden and len(hidden) > len(kept):
        kept.extend(hidden)
    kept.extend(spaces)
    return kept


def _segments(chars):
    """字符 → 行段。

    先按「字符纵向中心」分桶，再在桶内按 x 切开：
    - 用上沿分桶会把同一行拆碎：W/h/t 这类升部的上沿比 e/i 高 2–3pt，
      字号小的字符容差又刚好卡在临界值，一个单词会被拆成好几段，阅读顺序跟着乱；
    - 容差用全页字号中位数算（而不是每个字符自己的高度），避免同一行内
      容差忽大忽小；
    - 桶内水平空隙超过阈值才断开，双栏页面的两栏因此不会粘成一行。
    """
    spaces = [c for c in chars if c.get("space")]
    chars = [c for c in chars if not c.get("space")]
    if not chars:
        return []
    heights = sorted(c["size"] for c in chars) or [10.0]
    tol = max(2.0, 0.45 * heights[len(heights) // 2])
    buckets = []
    for c in sorted(chars, key=lambda c: ((c["y0"] + c["y1"]) / 2.0, c["x0"])):
        cy = (c["y0"] + c["y1"]) / 2.0
        if buckets and abs(cy - buckets[-1]["cy"]) <= tol:
            buckets[-1]["chars"].append(c)
        else:
            buckets.append({"cy": cy, "chars": [c]})
    lines = []
    for b in buckets:
        row = sorted(b["chars"], key=lambda c: c["x0"])
        cur = None
        for c in row:
            if cur is None:
                cur = [c]
                continue
            gap = c["x0"] - cur[-1]["x1"]
            # 阈值不能太大：双栏页面的栏间距只有 ~1.5em，切不开两栏就会在
            # _visual_lines 里被当成同一行，左右栏交错。宁可多切，后面会按 y 缝合。
            if gap > max(1.2 * max(c["size"], cur[-1]["size"]), 8.0):
                lines.append(_make_segment(cur))
                cur = [c]
            else:
                cur.append(c)
        if cur:
            lines.append(_make_segment(cur))
    _merge_descenders(lines)
    _attach_spaces(lines, spaces)
    lines.sort(key=lambda ln: (ln["y0"], ln["x0"]))
    return lines


_DESCENDER_ONLY = re.compile(r"^[gjpqy,.;:]+$")


def _merge_descenders(lines):
    """把 g/p/q/y 这类降部碎片并回同一视觉行。

    按字符纵向中心分桶时，降部字符的中心比正文低 3pt 左右，会被切成
    「Len」「g」「th」三段，标题就变成 "Len th"。这里把只含降部/标点的
    短碎片按纵向重叠 + 水平相邻并回主行。
    """
    if not lines:
        return
    for _ in range(3):
        merged = False
        lines.sort(key=lambda ln: (ln["y0"], ln["x0"]))
        for i in range(len(lines) - 1, -1, -1):
            s = lines[i]
            text = _line_text(s["chars"]).strip()
            if not text or len(text) > 3 or not _DESCENDER_ONLY.match(text):
                continue
            best, best_d = None, 1e9
            for j, o in enumerate(lines):
                if j == i:
                    continue
                overlap = min(s["y1"], o["y1"]) - max(s["y0"], o["y0"])
                h = min(s["y1"] - s["y0"], o["y1"] - o["y0"])
                if h <= 0 or overlap < 0.5 * h:
                    continue
                inside = o["x0"] - 0.5 <= s["x0"] <= o["x1"] + 0.5
                gap = max(o["x0"] - s["x1"], s["x0"] - o["x1"])
                if not inside and gap > max(3.0, 0.8 * max(o.get("size") or 10, s.get("size") or 10)):
                    continue
                d = 0.0 if inside else max(0.0, gap)
                if d < best_d:
                    best, best_d = o, d
            if best is None:
                continue
            best["chars"].extend(s["chars"])
            best["x0"] = min(best["x0"], s["x0"])
            best["x1"] = max(best["x1"], s["x1"])
            best["y0"] = min(best["y0"], s["y0"])
            best["y1"] = max(best["y1"], s["y1"])
            best["cy"] = (best["y0"] + best["y1"]) / 2.0
            best["size"] = max(best.get("size") or 0, s.get("size") or 0)
            lines.pop(i)
            merged = True
        if not merged:
            break


def _attach_spaces(lines, spaces):
    """把零面积空格按位置挂回行段；没有显式空格的间隙再靠 x 间距推断。"""
    if not lines or not spaces:
        return
    for sp in spaces:
        best, best_d = None, 1e9
        for ln in lines:
            if not (ln["y0"] - 2.0 <= sp["y0"] <= ln["y1"] + 2.0):
                continue
            inside = ln["x0"] - 1.5 <= sp["x0"] <= ln["x1"] + 1.5
            gap = max(ln["x0"] - sp["x0"], sp["x0"] - ln["x1"])
            limit = max(4.0, 0.7 * (ln.get("size") or 10.0))
            if not inside and gap > limit:
                continue
            d = 0.0 if inside else max(0.0, gap)
            if d < best_d:
                best, best_d = ln, d
        if best is not None:
            best["chars"].append({"ch": " ", "x0": sp["x0"], "x1": sp["x0"],
                                  "y0": sp["y0"], "y1": sp["y1"],
                                  "size": best.get("size") or 0.0, "space": True})


def _make_segment(chars):
    sizes = sorted(c["size"] for c in chars) or [10.0]
    # 用 30 分位而不是最大字号：公式里的 ∑/( ) 会把整行字号抬高，
    # 正文短行随后被误判成标题，章节上下文跟着乱。
    return {"cy": (min(c["y0"] for c in chars) + max(c["y1"] for c in chars)) / 2.0,
            "x0": min(c["x0"] for c in chars), "x1": max(c["x1"] for c in chars),
            "y0": min(c["y0"] for c in chars), "y1": max(c["y1"] for c in chars),
            "size": sizes[max(0, len(sizes) * 3 // 10)], "chars": list(chars)}


def _line_text(chars):
    """行段 → 文本。CJK 直接相连，拉丁文按空隙补空格，行尾连字符回接。"""
    chars = sorted(chars, key=lambda c: c["x0"])
    parts = []
    prev = None
    size = max([c["size"] for c in chars] or [10.0])
    for c in chars:
        ch = c["ch"]
        if c.get("space"):
            if parts and parts[-1] != " ":
                parts.append(" ")
            prev = None
            continue
        if prev is not None and ch.strip() and prev["ch"].strip():
            gap = c["x0"] - prev["x1"]
            if gap > 0.28 * max(size, c["size"]):
                if not (CJK_RANGE.match(ch) or CJK_RANGE.match(prev["ch"])):
                    parts.append(" ")
        parts.append(ch)
        prev = c
    # pdfium 对「行尾软连字符」返回 U+FFFE：换成 U+00AD 保留「这是断词」的信息，
    # 供 _join_lines 决定是去掉还是保留连字符（普通 "-" 多半是复合词）。
    return "".join(parts).strip().replace("\ufffe", "\u00ad")


def _column_boundary(segs, page_w):
    """返回双栏分界 x；判定不成立时返回 None。

    把页面按 6pt 切成横向条带，统计「这条竖线上有多少条带是空的」。真正的
    栏间空带在绝大多数条带里都没有字；单栏正文则几乎每条带都压过页面中段。
    只数行段是不够的：一行常被切成多个词段，任何位置都会有一堆段落在两侧。
    """
    body = [s for s in segs if not s.get("figure")]
    if len(body) < 10:
        return None
    top = min(s["y0"] for s in body)
    ybin = 6.0
    bins = {}
    for s in body:
        a = int((s["y0"] - top) / ybin)
        b = int((s["y1"] - top) / ybin)
        for i in range(a, b + 1):
            bins.setdefault(i, []).append((s["x0"], s["x1"]))
    total = max(1, len(bins))
    lo, hi = int(page_w * 0.30), int(page_w * 0.70)
    best_x, best_gap = None, 0.0
    for x in range(lo, hi + 1):
        gap = left = right = 0
        for items in bins.values():
            covered = False
            has_l = has_r = False
            for a, b in items:
                if a < x + 2 and b > x - 2:      # 留 4pt 容差：词间小空隙不算空带
                    covered = True
                if b <= x + 2:
                    has_l = True
                if a >= x - 2:
                    has_r = True
            if has_l:
                left += 1
            if has_r:
                right += 1
            if not covered:
                gap += 1
        if left < 0.50 * total or right < 0.50 * total:
            continue
        ratio = gap / float(total)
        if ratio > best_gap:
            best_x, best_gap = x, ratio
    return float(best_x) if best_x is not None and best_gap >= 0.65 else None


def _rows(segs, boundary=None, page_w=None):
    """把纵向重叠的行段缝成「视觉行」。

    同一行里上标、斜体、字体切换会让字顶（y0）高低错开，排序后形成 A/B/A/B
    交错，逐个比较会把一行越切越碎（阅读流里出现「jective enables…」这种从
    半个单词开始的段落）。用并查集把所有纵向重叠的碎片连起来。

    有分栏时**不能跨过分界缝合**：左右两栏的行经常在同一高度，跨栏缝合会把
    两个句子拼成一行。跨栏的整宽行段（标题/跨栏图）各自单独成行。
    """
    body = [s for s in segs if not s.get("figure")]
    figs = [s for s in segs if s.get("figure")]
    body.sort(key=lambda s: (s["y0"], s["x0"]))
    parent = list(range(len(body)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        a, b = find(i), find(j)
        if a != b:
            parent[b] = a

    def wide(s):
        return bool(boundary) and s["x0"] < boundary - 2 and s["x1"] > boundary + 2

    for i, a in enumerate(body):
        if wide(a):
            continue
        # 只看前面最近的一段：完全在 a 上方（b.y1 < a.y0）就可以停
        for j in range(i - 1, max(-1, i - 30), -1):
            b = body[j]
            if b["y1"] < a["y0"] - 4:
                break
            if wide(b):
                continue
            if boundary and ((a["x0"] + a["x1"]) / 2.0 < boundary) != \
                    ((b["x0"] + b["x1"]) / 2.0 < boundary):
                continue
            overlap = min(a["y1"], b["y1"]) - max(a["y0"], b["y0"])
            h = min(a["y1"] - a["y0"], b["y1"] - b["y0"])
            tall = max(a["y1"] - a["y0"], b["y1"] - b["y0"])
            # 高度差太大说明是「跨界」的大符号（∑/∏/大括号/分式）：它能同时压到
            # 上下两行，按重叠率缝会把两行粘成一行、文字交错。只缝高度相近的碎片。
            if h > 0 and overlap > 0.5 * h and tall <= 1.8 * h:
                union(i, j)
                continue
            # 同一行的碎片：上标/斜体让两个碎片的中心高度差一点点，
            # 纵向重叠可能不到一半，但水平紧挨着——也要缝起来。
            cy = abs((a["y0"] + a["y1"]) / 2.0 - (b["y0"] + b["y1"]) / 2.0)
            hgap = max(a["x0"] - b["x1"], b["x0"] - a["x1"])
            size = max(a.get("size") or 0, b.get("size") or 0)
            if size > 0 and cy < 0.6 * size and hgap < 2.5 * size:
                union(i, j)

    groups = {}
    for i, s in enumerate(body):
        groups.setdefault(find(i), []).append(s)

    rows = []
    for g in groups.values():
        chars = []
        for s in g:
            chars.extend(s["chars"])
        heights = sorted(c["size"] for c in chars) or [10.0]
        rows.append({"segs": g,
                     "rect": [min(s["x0"] for s in g), min(s["y0"] for s in g),
                              max(s["x1"] for s in g), max(s["y1"] for s in g)],
                     # 取 30 分位而不是中位数：数学符号/大写字母会把中位数抬高，
                     # 让正文行看起来「像标题」，段落被误断。
                     "size": heights[max(0, len(heights) * 3 // 10)],
                     "text": _line_text(chars)})
    for s in figs:
        rect = [round(s["x0"], 2), round(s["y0"], 2), round(s["x1"], 2), round(s["y1"], 2)]
        rows.append({"figure": True, "segs": [s], "rect": rect, "size": 0, "text": ""})
    rows.sort(key=lambda r: (r["rect"][1], r["rect"][0]))
    if boundary and page_w:
        rows = _stitch_centered(rows, boundary, page_w)
    return rows


def _stitch_centered(rows, boundary, page_w):
    """把被分栏切开的居中标题/作者行缝回一整行。

    标题和作者行横跨两栏，但字距大，会被切成左右两段；两段各自归栏后，
    作者名就会出现在左栏末尾和右栏开头。这里只合并「正文开始前、两段拼起来
    居中且足够宽」的行，正文行不参与。
    """
    body = [r for r in rows if not r.get("figure")]
    if not body:
        return rows
    # 只缝「正文开始之前」的行（标题/作者/邮箱）。用「第一条占满栏宽的行」
    # 当正文起点：作者/邮箱通常比正文窄，标题则是整宽（不算窄行）。
    col_rows = [r for r in body if (r["rect"][2] - r["rect"][0]) < page_w * 0.45]
    if len(col_rows) < 6:
        return rows
    colw = max(r["rect"][2] - r["rect"][0] for r in col_rows)
    full = [r for r in col_rows if (r["rect"][2] - r["rect"][0]) >= colw * 0.92]
    if not full:
        return rows
    body_top = min(r["rect"][1] for r in full)
    used = set()
    out = []
    for i, a in enumerate(rows):
        if i in used:
            continue
        if a.get("figure") or a["rect"][1] >= body_top - 2:
            out.append(a)
            continue
        hit = None
        for j in range(i + 1, min(i + 6, len(rows))):
            b = rows[j]
            if j in used or b.get("figure"):
                continue
            ac = (a["rect"][0] + a["rect"][2]) / 2.0
            bc = (b["rect"][0] + b["rect"][2]) / 2.0
            if (ac < boundary) == (bc < boundary):
                continue
            overlap = min(a["rect"][3], b["rect"][3]) - max(a["rect"][1], b["rect"][1])
            h = min(a["rect"][3] - a["rect"][1], b["rect"][3] - b["rect"][1])
            if h <= 0 or overlap < 0.5 * h:
                continue
            rect = [min(a["rect"][0], b["rect"][0]), min(a["rect"][1], b["rect"][1]),
                    max(a["rect"][2], b["rect"][2]), max(a["rect"][3], b["rect"][3])]
            if rect[2] - rect[0] < page_w * 0.45:
                continue
            if abs((rect[0] + rect[2]) / 2.0 - page_w / 2.0) > page_w * 0.08:
                continue
            hit = j
            break
        if hit is None:
            out.append(a)
            used.add(i)
            continue
        b = rows[hit]
        segs = a["segs"] + b["segs"]
        chars = []
        for s in segs:
            chars.extend(s["chars"])
        out.append({"segs": segs,
                    "rect": [min(a["rect"][0], b["rect"][0]), min(a["rect"][1], b["rect"][1]),
                             max(a["rect"][2], b["rect"][2]), max(a["rect"][3], b["rect"][3])],
                    "size": max(a["size"], b["size"]),
                    "text": _line_text(chars)})
        used.add(i)
        used.add(hit)
    out.sort(key=lambda r: (r["rect"][1], r["rect"][0]))
    return out


def _band_rows(rows, boundary, page_w):
    """按「跨栏/整宽」视觉行把页面切成横向带；带内再按栏排序。

    单栏页不做拆分：每行本来就是整宽，拆开只会得到一行一块。
    """
    if boundary is None:
        return [("cols", rows)]
    wide, narrow = [], []
    for r in rows:
        x0, _y0, x1, _y1 = r["rect"]
        crossing = x0 < boundary - 2 and x1 > boundary + 2
        if crossing or (x1 - x0) > page_w * 0.72:
            wide.append(r)
        else:
            narrow.append(r)
    wide.sort(key=lambda r: r["rect"][1])
    bands, cur = [], 0.0
    for w in wide:
        bands.append(("cols", [r for r in narrow if cur - 0.5 <= r["rect"][1] < w["rect"][1] - 0.5]))
        bands.append(("wide", [w]))
        cur = w["rect"][3]
    bands.append(("cols", [r for r in narrow if r["rect"][1] >= cur - 0.5]))
    return [b for b in bands if b[1]]


def _blocks_from_segments(segs, boundary, page_w, page_no):
    """视觉行 → 段落块。带内先左栏后右栏，再按行距/字号合并。"""
    out = []
    bands = _band_rows(_rows(segs, boundary, page_w), boundary, page_w)
    # 标题级别要用「整页正文基准字号」判断：按单个横向带算的话，
    # 标题自己成带时 base 等于标题字号，永远判不出 heading。
    all_sizes = sorted(r["size"] for _, items in bands for r in items
                       if not r.get("figure")) or [10.0]
    page_base = all_sizes[max(0, len(all_sizes) * 3 // 10)]
    for kind, items in bands:
        if kind == "wide":
            groups = [(items, -1)]
        elif boundary:
            left = [r for r in items if (r["rect"][0] + r["rect"][2]) / 2.0 < boundary]
            right = [r for r in items if (r["rect"][0] + r["rect"][2]) / 2.0 >= boundary]
            groups = [(g, i) for i, g in enumerate((left, right)) if g]
        else:
            groups = [(items, 0)]
        for g, col in groups:
            body = [r for r in g if not r.get("figure")]
            left = min([r["rect"][0] for r in body] or [0.0])
            right = max([r["rect"][2] for r in body] or [0.0])
            # 用整栏的字号中位数做基准：单行字高会被大写/斜体拉高，
            # 拿两行的 max 比会随机判成「字号不同」，段落被切碎。
            hs = sorted(r["size"] for r in body) or [10.0]
            base = hs[max(0, len(hs) * 3 // 10)]
            cur = None
            for r in g:
                if r.get("figure"):
                    if cur:
                        out.append(_make_block(cur, page_no, col, page_base))
                        cur = None
                    rect = r["rect"]
                    out.append({"id": "p%df%s" % (page_no, hashlib.sha1(str(rect).encode()).hexdigest()[:8]),
                                "page": page_no, "kind": "figure", "rect": rect,
                                "text": "", "size": 0, "math": {}, "ttext": "", "col": col,
                                "parts": [{"page": page_no, "rect": rect}]})
                    continue
                text = r["text"]
                if not text:
                    continue
                if cur is None:
                    cur = {"lines": [text], "rect": list(r["rect"]), "last_x1": r["rect"][2],
                           "size": r["size"]}
                    continue
                gap = r["rect"][1] - cur["rect"][3]
                # 缩进要相对「当前段落」的左边界判断：整段都在缩进时（如摘要块），
                # 每一行都比页边距靠右，用全局 left 会把每一行都当成新段。
                indent = r["rect"][0] > cur["rect"][0] + max(2.5, 0.8 * base)
                # 上一行短于栏宽的 60% 才算「段末短行」
                short_prev = cur["last_x1"] < right - 0.40 * max(1.0, right - left)
                # 上一行以连字符/软连字符结尾：一定是同一个词的断行，不能断段
                hyphen = cur["lines"] and (cur["lines"][-1].endswith("-")
                                           or cur["lines"][-1].endswith("\u00ad"))
                # 列表项的第一行（• / 1. / (a)）后面常接悬挂缩进，不能当成新段
                bullet = bool(re.match(r"^\s*([•·\-–—*]|\(?\d+[.)]|\(?[a-zA-Z][.)])\s",
                                       cur["lines"][0] if cur["lines"] else ""))
                same_para = hyphen or (gap <= max(3.5, 0.80 * base)
                                       and r["size"] < base * 1.35
                                       and cur["size"] < base * 1.35
                                       and not (indent and gap > 0.25 * base and not bullet)
                                       and not (short_prev and gap > 0.5 * base and not bullet))
                # 二维公式（分式 / 多重求和）：分子、分母、编号在垂直方向上紧挨着，
                # 字号还可能不同（分母小、编号大）。两行都是纯公式就并进同一块，
                # 让后面的 formula 判定把整块裁成一张图，避免出现半截公式 + 重叠块。
                # 不看缩进：公式编号通常右对齐，缩进判断会把编号挡在外面。
                if (not same_para and gap <= max(6.0, 0.9 * base)
                        and _line_is_mathy(cur["lines"][-1] if cur["lines"] else "")
                        and _line_is_mathy(text)):
                    same_para = True
                if same_para:
                    cur["lines"].append(text)
                    cur["rect"] = [min(cur["rect"][0], r["rect"][0]), min(cur["rect"][1], r["rect"][1]),
                                   max(cur["rect"][2], r["rect"][2]),
                                   max(cur["rect"][3], r["rect"][3])]
                    cur["last_x1"] = r["rect"][2]
                    cur["size"] = max(cur["size"], r["size"])
                else:
                    out.append(_make_block(cur, page_no, col, page_base))
                    cur = {"lines": [text], "rect": list(r["rect"]), "last_x1": r["rect"][2],
                           "size": r["size"]}
            if cur:
                out.append(_make_block(cur, page_no, col, page_base))
    return out


def _join_lines(lines):
    text = ""
    for ln in lines:
        if not text:
            text = ln
            continue
        if text.endswith("\u00ad") or text.endswith("-"):
            # 软连字符是断词（ob-/jective）：一般去掉；但 left-to-/right 这种
            # 前面的词本身带连字符，说明是复合词，要保留。普通 "-" 一律保留。
            head = re.search(r"(\S+)[-\u00ad]$", text)
            token = head.group(1) if head else ""
            keep = text.endswith("-") and not text.endswith("\u00ad") or "-" in token
            if ln[:1].islower() or CJK_RANGE.search(ln[:1]):
                text = text + ln if keep else text[:-1] + ln
            else:
                text += " " + ln
        elif CJK_RANGE.search(text[-1:]) or CJK_RANGE.search(ln[:1]):
            text += ln
        else:
            text += " " + ln
    text = re.sub(r"\s{2,}", " ", text).strip()
    # PDF 里常见「教 育 背 景」这种字间空格（排版工具按字定位）：中文之间不保留空格
    text = re.sub(r"(?<=[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af])\s+"
                  r"(?=[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af])", "", text)
    # 标点前多余空格 / 被拆开的 URL（https : / / github . com）
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    if "http" in text:
        text = re.sub(r"(https?)\s*:\s*/\s*/\s*", r"\1://", text)
        text = re.sub(r"\s*/\s*", "/", text)
    # 去掉 PDF 里无意义的控制字符（数学字体偶尔带 \x01）
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    return text.replace("\u00ad", "").strip()


def _math_ratio(text):
    """符号密度（不算数字）：数字在日期/编号里太多，算进去会把整行误判成公式。"""
    body = [c for c in text if not c.isspace()]
    if not body:
        return 0.0
    return sum(1 for c in body if c in MATH_CHARS) / float(len(body))


_NUM_RE = re.compile(r"\d+(?:\.\d+)?")
_LATEX_SRC_RE = re.compile(r"\\label|\\begin\s*\{|\\frac|\\text\s*\{")
# 标题不应带公式符号 / 句内句号 / 虚词结尾；这些是正文短行被误判的主要来源
_HEAD_MATH_RE = re.compile(r"[=∈∉⊂⊆∪∩→←↔⇒⇐∼⊤∂√∑∏∫]")
_HEAD_PROSE_RE = re.compile(r"[A-Za-z]\.\s+[A-Z]")
_HEAD_END_FUNC_RE = re.compile(
    r"\b(?:by|with|the|on|in|that|this|gives|is|are|we|our|its"
    r"|which|when|where|how|than|as|from|into|at|and|or|image|methods)\W*$", re.I)


_TABLE_MATH_RE = re.compile(r"[=≤≥≠≈∈∉⊂⊆∪∩→←↔·√∑∏∫]")
_TABLE_STOP_RE = re.compile(
    r"\b(?:the|and|of|to|is|are|we|with|for|from|that|this|then|where|let|be|by|on|in)\b", re.I)


def _looks_table_row(text):
    """表格行：短、数字多、词少，且不像公式/正常句子。

    只看「数字 ≥4」会把「Qi(x)=… for 0≤i≤3」和「Lemma 2.1…」也当成表格，
    这些段落被跳过翻译后阅读流会缺内容；这里再排除数学符号与常见虚词。
    """
    t = text or ""
    if len(t) > 240 or _CAPTION_RE.match(t):
        return False
    nums = _NUM_RE.findall(t)
    words = re.findall(r"[A-Za-z]{2,}", t)
    if len(nums) < 5 or len(words) > 8:
        return False
    if re.match(r"^(?:Theorem|Lemma|Definition|Corollary|Proposition|Remark|Example|Problem)\s+\d",
                t, re.I):
        return False
    if _TABLE_MATH_RE.search(t) or len(_TABLE_STOP_RE.findall(t)) >= 2:
        return False
    if len(words) >= 8 and t.rstrip().endswith((".", "。")):
        return False
    return True


def _make_block(cur, page_no, col, base=None):
    text = _join_lines(cur["lines"])
    rect = [round(v, 2) for v in cur["rect"]]
    kind = "text"
    # 「几乎没有自然语言单词 + 符号密度高」才算独立公式块。带英文词的行
    # （比如「aS ⊆ D with ... we get」）是正文，误判成公式会被裁成图片、
    # 既不翻译，还会在阅读流里插一大块英文截图。
    words = re.findall(r"[A-Za-z]{2,}", text)
    # 「1. Introduction」「3.2 Setup」这类编号章节：字号常与正文相同，
    # 不能只靠字号判断；先把它们从 bullet 里排除，再单独认成 heading。
    numbered = bool(re.match(r"^\d+(?:\.\d+)*\.?\s+[A-Z]", text)) and len(text) <= 70
    bullet = bool(re.match(r"^\s*([•·\-–—*]|\(?\d+[.)])\s", text)) and not numbered
    lines_n = len(cur["lines"])
    height = (float(rect[3]) - float(rect[1])) if len(rect) == 4 else 0.0
    # 标题：相对正文字号判断。PDF 里的字号单位各不相同（这篇正文 4.9、
    # 标题 6.6），旧的绝对阈值 15 会让所有标题都漏掉。
    # 结尾只看句号/问号/叹号：标题常以冒号结尾（「...{1,...,n}:」）。
    nums = _NUM_RE.findall(text)
    letters = sum(1 for c in text if c.isalpha())
    letter_density = letters / max(1.0, len(re.sub(r"\s+", "", text)))
    size_ok = (cur["size"] >= float(base) * 1.18 if base else cur["size"] >= 15)
    title_big = bool(base) and cur["size"] >= float(base) * 1.35
    upper_words = sum(1 for w in words if w.isupper() and len(w) >= 2)
    plain_ok = (len(text) <= 80 and len(words) >= 1 and lines_n <= 2
                and _math_ratio(text) < 0.15
                and len(nums) < 3 and letter_density >= 0.45
                and "@" not in text and not _LATEX_SRC_RE.search(text)
                and not _HEAD_MATH_RE.search(text)
                and not re.search(r"\[\d+\]", text)
                and not _HEAD_PROSE_RE.search(text)
                and not _HEAD_END_FUNC_RE.search(text)
                and not (len(text) < 8 and len(words) <= 2)
                and not (upper_words >= 3 and len(words) <= 6)
                and not (re.findall(r"\b[A-Z]{2,}\b", text) and len(words) <= 6)
                and " / " not in text
                and not text.rstrip().endswith((".", "!", "?", "。", "！", "？", ";", "；", ",")))
    heading = bool(plain_ok and not bullet and (
        (size_ok and not (re.match(r"^[a-z]", text) and not title_big))
        # 编号章节允许字号等于正文；但要是纯叙述/公式就不算
        or (numbered and not _HEAD_MATH_RE.search(text))))
    # 摘要正文常被粘连成一行 heading：长度明显超过标题就退回正文
    if heading and re.match(r"^abstract\b", text, re.I) and len(text) > 40:
        heading = False
    # 大括号字形（cases / 分段函数）在 PDF 里是私用区字符：整块裁原图最保真，
    # 交给翻译只会得到「c(n)= 32,, mn=≥92,,」这种乱码
    if not bullet and any(ch in text for ch in "\uf8f1\uf8f2\uf8f3\uf8f4"):
        kind = "formula"
    # 单行公式：一行、几乎没有英文词、符号密度高
    elif (not bullet and lines_n <= 1 and len(words) <= 1
            and len(text) <= 120 and _math_ratio(text) >= 0.18):
        kind = "formula"
    # 多行公式组（align / cases / 矩阵）：同样当不可翻译块，裁原图最保真。
    # 限高 + 限行，避免把「定理 / 证明」这类高密度正文整块裁成图。
    elif (not bullet and 2 <= lines_n <= 8 and len(words) <= 1
          and len(text) <= 400 and height <= 300
          and _math_ratio(text) >= 0.32):
        kind = "formula"
    elif _looks_table_row(text):
        # 矩阵 / 多列数字网格：裁原页比显示打散的文本更保真
        kind = "formula" if _math_ratio(text) >= 0.10 else "table"
    elif heading:
        kind = "heading"
    elif bullet:
        kind = "list"
    # 少数 PDF 的不可见层字符宽度正常，单靠宽度过滤不掉；整块是 LaTeX
    # 命令的（\label/\begin/\frac…）直接标 hidden，由 _clean_meta_blocks 丢弃。
    if text.count("\\") >= 2 and _LATEX_SRC_RE.search(text):
        kind = "hidden"
    role = ""
    if kind in ("text", "list"):
        if _CAPTION_RE.match(text):
            role = "caption"           # Figure/Table 图注：阅读流里弱化，导出时用小字
        elif _THEOREM_RE.match(text):
            role = "theorem"           # 定理/引理/证明：阅读流加左侧竖线，和正文区分
    ttext, math = (_protect_inline(text) if kind in ("text", "list") else (text, {}))
    key = hashlib.sha1(("%d|%s|%s" % (page_no, rect, text[:40])).encode("utf-8")).hexdigest()[:10]
    out = {"id": "p%db%s" % (page_no, key), "page": page_no, "kind": kind, "col": col,
           "role": role, "rect": rect, "text": text, "ttext": ttext, "size": round(cur["size"], 2),
           "lines": len(cur["lines"]),
           "math": math, "parts": [{"page": page_no, "rect": rect}]}
    if kind == "heading":
        # 1 = 论文大标题，2 = 章节标题（前端按级别给字号）
        out["level"] = 1 if (base and cur["size"] >= float(base) * 1.45) else 2
    return out


def _line_is_mathy(t):
    """这一行是不是纯公式行：用来把分式的分子 / 分母 / 编号缝成同一个块。

    判据是「没有正常英文单词，并且有数学符号」，比单看符号密度更稳：
    分式的分母行（`,, m − j m − j`）符号密度其实很低。
    """
    t = str(t or "").strip()
    if not t:
        return False
    if re.findall(r"[A-Za-z]{2,}", t):
        return False
    return bool(re.search(r"[=≤≥≠<>+−×÷^_√∑∏∫∈∉⊆⊂∪∩·()\[\]{},;]", t))


_REF_HEAD_RE = re.compile(r"^(?:references|bibliography|参考文献|參考文獻)\b", re.I)
_REF_HEAD_ONLY_RE = re.compile(r"^(?:references|bibliography|参考文献|參考文獻)\s*[:：]?$", re.I)
_REF_ENTRY_RE = re.compile(r"^\[\d+\]")
# 图注 / 表注：阅读流里弱化字号，导出时用注解样式，不再和正文一样抢视线
_CAPTION_RE = re.compile(r"^(?:figure|fig\.|table|tab\.|图|表)\s*\d+", re.I)
# 定理类段落：只加左侧竖线做视觉分组，文本仍原样送翻译
_THEOREM_RE = re.compile(
    r"^(?:theorem|lemma|corollary|proposition|definition|conjecture|claim"
    r"|remark|example|proof|assumption)\b|^problem\s+(?:statement|\d)"
    r"|^(?:定理|引理|推论|定义|命题|证明|注|例)\s*\d*", re.I)


def _mark_reference_blocks(blocks):
    """References / Bibliography 之后的条目标成 ref：只显示原文，不送翻译。

    参考文献翻译成中文没有意义，还会把「19 pp.」这种续行也翻一遍。
    标题本身若单独成行则保留 heading；被粘上首条文献的（如
    「References InEuropean Conference…」）整块标成 ref，避免翻文献。
    """
    blocks = blocks or []
    start = None
    for i, b in enumerate(blocks):
        if not isinstance(b, dict) or b.get("kind") in ("figure", "formula"):
            continue
        text = str(b.get("text") or "").strip()
        if not _REF_HEAD_RE.match(text):
            continue
        if _REF_HEAD_ONLY_RE.match(text) or len(text) <= 24:
            start = i + 1                    # 标题单独一行：保留 heading
        else:
            b["kind"] = "ref"                # 标题与首条文献粘在一起
            b["role"] = ""
            start = i + 1
        break
    if start is None:
        # 没有 References 标题：连续两条以 [n] 开头的段落视为文献区起点
        for i in range(len(blocks) - 1):
            a = str(blocks[i].get("text") or "").strip()
            c = str(blocks[i + 1].get("text") or "").strip()
            if _REF_ENTRY_RE.match(a) and _REF_ENTRY_RE.match(c):
                start = i
                break
    if start is None:
        return blocks
    for b in blocks[start:]:
        if not isinstance(b, dict) or b.get("kind") in ("figure", "formula"):
            continue
        b["kind"] = "ref"
        b["role"] = ""
    return blocks


_ABSTRACT_RE = re.compile(r"^(?:abstract|摘要)\b", re.I)
_AFFIL_RE = re.compile(
    r"@|https?://|doi\.org|\b(?:University|Universit[äaé]t|Department|Institute|Faculty"
    r"|School|College|Laboratory|Laboratoire|CNRS|Politecnico|Academy)\b",
    re.I)
_META_SKIP_RE = re.compile(
    r"^(?:This (?:CVPR|ICCV|ECCV|WACV|BMVC|ACCV) paper is the Open Access version"
    r"|Except for this watermark"
    r"|the final published version"
    r"|International Journal of\s*Computer\s*Vision\s*\(\d{4}\)"
    r"|https?://doi\.org"
    r"|Received:|Accepted:|Published online|© The Author|arXiv:\d)", re.I)


def _norm_block_text(text):
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def _mark_front_matter(blocks):
    """摘要之前的作者 / 单位 / 邮箱 / 收稿日期标成 meta：不翻译、不进章节。

    标题按「front matter 里最大字号的 heading」保留（可能被拆成两行），
    其余 front 块一律 meta。这样翻译上下文不会变成「当前章节：Petr Hruby」。
    """
    blocks = blocks or []
    abs_i = None
    for i, b in enumerate(blocks):
        if not isinstance(b, dict) or b.get("kind") in ("figure", "formula"):
            continue
        if _ABSTRACT_RE.match(str(b.get("text") or "").strip()):
            abs_i = i
            break
    if abs_i is None or abs_i == 0:
        return blocks
    front = [b for b in blocks[:abs_i] if isinstance(b, dict)]
    if not front:
        return blocks
    # 标题可能被拆成 2–3 行：以第一个非水印块为基准，只保留紧贴它、
    # 字号相近的连续块；作者/单位与标题之间通常有明显行距。
    ref = None
    for b in front:
        if b.get("kind") != "hidden" and not _META_SKIP_RE.match(str(b.get("text") or "").strip()):
            ref = b
            break
    if ref is None:
        return blocks
    ref_size = float(ref.get("size") or 0)
    prev_bottom = None
    rect = ref.get("rect") or []
    if len(rect) == 4:
        prev_bottom = float(rect[3])
    for b in front:
        if b is ref:
            continue
        if b.get("kind") == "hidden":
            continue
        text = str(b.get("text") or "").strip()
        r = b.get("rect") or []
        gap = (float(r[1]) - prev_bottom) if (len(r) == 4 and prev_bottom is not None) else None
        size = float(b.get("size") or 0)
        title_like = (b.get("kind") in ("heading", "text") and ref_size and gap is not None
                      # 有些模板把标题两行放在同一个 y 上（上下行矩形重叠），gap 会是负数
                      and -2.0 * ref_size <= gap <= 1.8 * max(ref_size, size)
                      and ref_size * 0.7 <= size <= ref_size * 1.6
                      and "@" not in text)
        if title_like:
            # 标题被拆行时第二行常以 "a / of / for" 开头，字号又与正文接近，
            # 会被 _make_block 判成 text；这里按「紧贴标题 + 字号相近」升回 heading。
            if b.get("kind") == "text":
                b["kind"] = "heading"
                b["level"] = 1
            if len(r) == 4:
                prev_bottom = float(r[3])
            continue
        b["kind"] = "meta"
        b["role"] = ""
    # 有些期刊（IJCV）把作者/单位排在 Abstract 之后、编号章节之前，
    # 这一小段里的 heading 一律当 meta，避免进目录和章节上下文。
    body_start = None
    for i in range(abs_i + 1, min(len(blocks), abs_i + 60)):
        b = blocks[i]
        if not isinstance(b, dict) or b.get("kind") != "heading":
            continue
        t = str(b.get("text") or "").strip()
        if re.match(r"^\d+(?:\.\d+)*\.?\s+[A-Z]", t) and not _AFFIL_RE.search(t):
            body_start = i
            break
    if body_start:
        for b in blocks[abs_i + 1:body_start]:
            if isinstance(b, dict) and b.get("kind") == "heading":
                b["kind"] = "meta"
                b["role"] = ""
    return blocks


def _clean_meta_blocks(blocks, sizes):
    """丢掉水印 / 页眉页脚 / 跨页重复短文本；保留图表与公式块。"""
    blocks = blocks or []
    repeated = {}
    for b in blocks:
        if not isinstance(b, dict):
            continue
        t = _norm_block_text(b.get("text"))
        if t and len(t) <= 100:
            repeated.setdefault(t, set()).add(b.get("page"))
    out = []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        kind = b.get("kind")
        if kind == "hidden":
            continue
        if kind in ("figure", "formula"):
            out.append(b)
            continue
        text = str(b.get("text") or "").strip()
        if _META_SKIP_RE.match(text):
            continue
        # 单位 / 邮箱行即使漏到正文区也不翻译、不占阅读流
        if (kind in ("heading", "text", "list") and len(text) <= 140
                and _AFFIL_RE.search(text)
                and not text.rstrip().endswith((".", "。", "!", "?", "！", "？"))):
            continue
        rect = b.get("rect") or []
        page = b.get("page") or 0
        if (len(rect) == 4 and 0 < page <= len(sizes) and len(text) <= 90
                and kind in ("text", "heading", "list", "table", "ref")):
            ph = float(sizes[page - 1][1] or 0)
            if ph and (rect[3] < ph * 0.055 or rect[1] > ph * 0.925):
                continue
        nt = _norm_block_text(text)
        if nt and len(nt) <= 100 and len(repeated.get(nt, ())) >= 3:
            continue
        out.append(b)
    return out


_NUM_EXPR = re.compile(r"^[0-9.,()\[\]{}^_/+\-*<>|]+$")
_DECIMAL = re.compile(r"^[0-9]+(?:[.,][0-9]+)?$")
_PERCENT = re.compile(r"^[0-9.,]+%$")
_SINGLE_LETTER = re.compile(r"^[A-Za-z]$")
_FUNC_CALL = re.compile(r"^[A-Za-z]\(.*\)$")
# 连字符复合词（product-free / well-defined / Sub-case）：连字符不是减号，不能当公式
_HYPHEN_WORD = re.compile(r"^[A-Za-z]+(?:[-'][A-Za-z]+)+[\.,;:\)\]\}]*$")


def _tok_kind(tok):
    """单个 token 的数学程度：def=确定是公式，maybe=可能是变量，word=自然语言。"""
    if not tok:
        return "word"
    if _HYPHEN_WORD.match(tok):
        return "word"
    if any(c in MATH_STRONG for c in tok):
        return "def"
    if "^" in tok or "_" in tok or "\\" in tok:
        return "def"
    if _NUM_EXPR.match(tok) and any(c.isdigit() for c in tok):
        return "def"
    if _DECIMAL.match(tok) or _PERCENT.match(tok):
        return "def"
    if _FUNC_CALL.match(tok) or tok.lower() in MATH_FUNCS:
        return "def"
    # 形如 aS / pH 的短变量：首字母之外的字母也是大写，基本不可能是英文词
    if len(tok) <= 3 and tok.isalpha() and any(c.isupper() for c in tok[1:]):
        return "maybe"
    if _SINGLE_LETTER.match(tok):
        return "maybe"
    return "word"


def _math_token(tok):
    """这个 token 本身是否足够确定是公式（保留给旧调用 / 测试）。"""
    return _tok_kind(tok) == "def"


def _protect_inline(text):
    """把行内公式换成 ⟦F1⟧ 占位符，翻译后原样还原。

    一段连续的「数学 token」（变量 / 数字 / 运算符 / 函数名）只要里面有一个确定
    信号，就整段保护，避免只保护到孤立的「=」或「1」。占位符外的空白原样保留，
    否则还原后会粘成「6and」「∈R」这种排版。
    返回 (保护后的文本, {"F1": 原文})；原文仍保留在 block["text"] 里给「原文」视图用。
    """
    tokens = re.split(r"(\s+)", text)
    math, out = {}, []

    def flush(run):
        """run: [(token, kind)]，token 之间可能夹着空白。"""
        if not run:
            return
        if not any(kind == "def" for _, kind in run):
            out.extend(tok for tok, _ in run)
            return
        raw = "".join(tok for tok, _ in run)
        lead = raw[:len(raw) - len(raw.lstrip())]
        body = raw.strip()
        trail = raw[len(raw.rstrip()):]
        if not body:
            out.append(raw)
            return
        key = "F%d" % (len(math) + 1)
        math[key] = body
        out.append(lead + "⟦%s⟧" % key + trail)

    run = []
    for tok in tokens:
        if not tok:
            continue
        if tok.isspace():
            if run:
                run.append((tok, "space"))
            else:
                out.append(tok)
            continue
        kind = _tok_kind(tok)
        if kind == "word":
            flush(run)
            run = []
            out.append(tok)
        else:
            run.append((tok, kind))
    flush(run)
    return "".join(out), math


def _figure_segments(page, page_no):
    """PDF 里的图片对象 → 伪行段，交给同一套分栏/分带逻辑排进阅读顺序。

    只认真正的 image 对象（type 3）；矢量图（type 2）不拆，靠左原页保真。
    """
    out = []
    h = page.get_height()
    try:
        from pypdfium2 import raw as _raw
    except Exception:
        return out
    try:
        for obj in page.get_objects():
            try:
                if _raw.FPDFPageObj_GetType(obj.raw) != 3:
                    continue
                x0, y0, x1, y1 = obj.get_bounds()
            except Exception:
                continue
            if abs(x1 - x0) < 28 or abs(y1 - y0) < 28:
                continue
            out.append({"figure": True, "x0": x0, "x1": x1,
                        "y0": h - y1, "y1": h - y0, "size": 0, "chars": []})
    except Exception:
        pass
    return out


def _page_blocks(page, page_no):
    chars = _extract_chars(page)
    if not chars:
        return []
    page_w = page.get_width()
    # 左侧页边距里的竖排 arXiv 编号（x≈18）会被缝进正文行，插出「representa 9 tion」
    # 这种乱码；按页宽 7% 切掉页边碎片。正文左边界通常 ≥ 8% 页宽。
    chars = [c for c in chars if c["x0"] > page_w * 0.07]
    if not chars:
        return []
    text = "".join(c["ch"] for c in chars)
    bad = len(GARBAGE.findall(text)) / float(max(1, len(text)))
    if bad > 0.3:
        return []                       # 乱码/无文本层：不硬造阅读流
    segs = _segments(chars)
    if not segs:
        return []
    segs.extend(_figure_segments(page, page_no))
    boundary = _column_boundary(segs, page_w)
    blocks = _blocks_from_segments(segs, boundary, page_w, page_no)
    # 丢掉纯装饰块（分隔线「----」、孤立标点）和贴边页码：它们对阅读流只有噪音
    page_h = page.get_height()
    keep = []
    for b in blocks:
        if b["kind"] == "figure":
            keep.append(b)
            continue
        t = (b.get("text") or "").strip()
        if not re.search(r"[0-9A-Za-z\u3400-\u9fff]", t):
            continue
        if len(t) <= 3 and (b["rect"][3] < 40 or b["rect"][1] > page_h - 40):
            continue
        if re.fullmatch(r"\d{1,2}", t):
            continue                    # 上标编号 / 页码碎片，进阅读流只会干扰
        keep.append(b)
    # 列表缩进：以本页正文最常见左边界为基准（不是绝对最小值，避免页边公式
    # 把基准拉偏），按每级 ≈1.5 个字号换算层级。只给 kind=list 记录 level。
    left, xs = None, []
    for b in keep:
        rect = b.get("rect") or []
        if len(rect) != 4:
            continue
        x = float(rect[0])
        left = x if left is None else min(left, x)
        if b.get("kind") in ("text", "heading"):
            xs.append(int(round(x)))
    if xs:
        left = max(set(xs), key=xs.count)     # 正文出现最多的左边界
    if left is not None:
        unit = max(8.0, page_w * 0.03)   # 一级缩进 ≈ 3% 页宽，不跟字号走（PDF 字号单位差别很大）
        for b in keep:
            rect = b.get("rect") or []
            if b.get("kind") != "list" or len(rect) != 4:
                continue
            b["indent"] = max(0, min(3, int(round((float(rect[0]) - left) / unit))))
    return keep


def parse_doc(path):
    """把 docx / pptx 抽成阅读流块（没有原页图像，纯文本重排）。

    复用 PDF 阅读流的那套块结构，翻译 / 划词提问 / 批注都不用改。
    页号是「每 10 段一页」的伪分页，只用于目录跳转与进度显示。
    """
    name = os.path.basename(path)
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as e:
        raise ReaderError("读取失败：%s" % e)
    text, warn = docparse.extract(name, raw)
    if not text:
        raise ReaderError("这个文档没提取到文本：%s" % (warn or "可能全是图片"))
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if len(paras) < 3:
        paras = [p.strip() for p in text.splitlines() if p.strip()]
    blocks = []
    for i, para in enumerate(paras):
        page = i // 10 + 1
        head = len(para) <= 70 and not para.endswith(("。", ".", ":", "：", ";", "；", ",", "，"))
        bid = "d%s" % hashlib.sha1(("%d|%s" % (i, para[:60])).encode("utf-8")).hexdigest()[:10]
        blocks.append({
            "id": bid, "page": page, "kind": "heading" if head else "text",
            "col": 0, "rect": [], "text": para, "ttext": para,
            "size": 15 if head else 12, "math": {}, "parts": [],
        })
    pages = blocks[-1]["page"] if blocks else 1
    return {
        "version": SCHEMA_VERSION, "name": name, "pages": pages, "parsed_pages": pages,
        "sizes": [[595, 842] for _ in range(pages)], "scan_pages": [], "blocks": blocks,
        "file_type": os.path.splitext(path)[1].lower().lstrip("."),
    }


def parse_pdf(path, force=False):
    """解析整份 PDF，返回 {version, key, name, pages, blocks, scan_pages}。"""
    doc_id = _doc_id(path)
    if not force:
        cached = read_json(_blocks_path(doc_id), None)
        if cached and cached.get("version") == SCHEMA_VERSION:
            return cached
    try:
        import pypdfium2 as pdfium
    except ImportError:
        raise ReaderError("缺少 pypdfium2，无法解析 PDF")
    try:
        pdf = pdfium.PdfDocument(path)
    except Exception as e:
        raise ReaderError("打不开这个 PDF：%s" % e)
    blocks, page_sizes, scan_pages = [], [], []
    try:
        total = len(pdf)
        n = min(total, MAX_PARSE_PAGES)
        for i in range(n):
            page = pdf[i]
            try:
                page_sizes.append([round(page.get_width(), 1), round(page.get_height(), 1)])
                got = _page_blocks(page, i + 1)
                if not got:
                    scan_pages.append(i + 1)
                blocks.extend(got)
            finally:
                try:
                    page.close()
                except Exception:
                    pass
        blocks = _mark_reference_blocks(blocks)
        blocks = _mark_front_matter(blocks)
        raw_n = len(blocks)
        blocks = _clean_meta_blocks(blocks, page_sizes)
        meta = {"version": SCHEMA_VERSION, "key": doc_id,
                "name": os.path.basename(path), "pages": total,
                "parsed_pages": n, "sizes": page_sizes,
                "scan_pages": scan_pages, "filtered": raw_n - len(blocks),
                "blocks": blocks, "file_type": "pdf"}
        write_json(_blocks_path(doc_id), meta, indent=None)
        return meta
    finally:
        try:
            pdf.close()
        except Exception:
            pass


# ---------------------------------------------------------------- 全文查找

# 归一化后的块文本缓存：doc_key -> (块数, [(page, id, text, norm)])
# 首次搜索建一次；1000 页 / 200 万字符的文档再搜只是内存里的 str.find。
_SEARCH_CACHE = {}
_SEARCH_CACHE_MAX = 2
_SEARCH_SKIP_RE = re.compile(r"[\s\u200b-\u200d\ufeff\u00ad]+")


def _search_norm(s):
    return _SEARCH_SKIP_RE.sub("", str(s or "")).lower()


def _span_in_text(text, needle):
    """归一化后命中了，再把位置映射回原文（只对命中的块执行，开销可忽略）。

    idx 必须与归一化串**逐字符**对齐：个别字符（如 'İ'）lower() 后是两个码位，
    直接按原文下标 append 会让后续位置整体错位，命中靠尾部时还会越界。
    """
    chars, idx = [], []
    for i, ch in enumerate(text):
        if _SEARCH_SKIP_RE.match(ch):
            continue
        low = ch.lower()
        for c in low:                 # 小写可能变长，每个输出字符都映射回同一个原文下标
            chars.append(c)
            idx.append(i)
    norm = "".join(chars)
    pos = norm.find(needle)
    if pos < 0:
        return None
    return idx[pos], idx[pos + len(needle) - 1] + 1


def search_blocks(meta, q, limit=200, per_page=20):
    """在已解析的 blocks 里查子串；返回 {items, total, truncated}。

    归一化规则与前端一致（去空白 / 零宽 / 软连字符 + 小写），
    所以「machine learning」能命中跨行的「machine\\nlearning」。
    """
    blocks = meta.get("blocks") or []
    key = meta.get("key") or meta.get("name") or ""
    entry = _SEARCH_CACHE.get(key)
    if not entry or entry[0] != len(blocks):
        rows = []
        for b in blocks:
            if b.get("kind") == "figure":
                continue
            text = b.get("text") or ""
            if not text:
                continue
            rows.append((b.get("page") or 0, b.get("id") or "", text, _search_norm(text)))
        entry = (len(blocks), rows)
        _SEARCH_CACHE[key] = entry
        while len(_SEARCH_CACHE) > _SEARCH_CACHE_MAX:
            _SEARCH_CACHE.pop(next(iter(_SEARCH_CACHE)), None)

    needle = _search_norm(q)
    if not needle:
        return {"items": [], "total": 0, "truncated": False}

    items, total, per = [], 0, {}
    for page, bid, text, norm in entry[1]:
        if needle not in norm:
            continue
        total += 1
        if len(items) >= limit or per.get(page, 0) >= per_page:
            continue
        span = _span_in_text(text, needle)
        if not span:
            continue
        s, e = span
        left, right = max(0, s - 30), min(len(text), e + 30)
        snippet = ("…" if left > 0 else "") + text[left:right].replace("\n", " ") + \
                  ("…" if right < len(text) else "")
        per[page] = per.get(page, 0) + 1
        items.append({"page": page, "block_id": bid, "quote": text[s:e], "snippet": snippet})
    return {"items": items, "total": total, "truncated": total > len(items)}


# ---------------------------------------------------------------- 批注

def _anno_shape(it):
    """统一批注 → 阅读器前端用的扁平结构（保留 block_id / page 等常用锚点）。"""
    return {
        "id": it.get("id"),
        "block_id": it.get("block_id") or "",
        "page": int(it.get("page") or 0),
        "side": it.get("side") or "src",
        "quote": it.get("quote") or "",
        "question": it.get("question") or "",
        "answer": it.get("answer") or "",
        "comment": it.get("comment") or "",
        "kind": it.get("kind") or "note",
        "color": it.get("color") or "yellow",
        "tags": it.get("tags") or [],
        "created": it.get("created_at"),
        "updated": it.get("updated_at"),
    }


def list_annotations(doc_key):
    """阅读器批注：按沙箱相对路径存在统一 annotations 表里。"""
    from app import anno
    return [_anno_shape(it) for it in anno.list_annotations(doc_key)]


def save_annotation(doc_key, info):
    info = info or {}
    block_id = str(info.get("block_id") or "").strip()[:80]
    answer = str(info.get("answer") or "").strip()[:12000]
    comment = str(info.get("comment") or "").strip()[:8000]
    quote = str(info.get("quote") or "").strip()[:4000]
    if not block_id and not answer and not comment:
        return None
    kind = str(info.get("kind") or "")
    if kind not in ("highlight", "note", "excerpt", "qa"):
        kind = "qa" if answer else "highlight"
    from app import anno
    try:
        page = int(info.get("page") or 0)
    except (TypeError, ValueError):
        page = 0
    return anno.save_annotation({
        "id": info.get("id"),
        "doc_key": doc_key,
        "kind": kind,
        "color": info.get("color") or "yellow",
        "anchor": {"block_id": block_id, "page": page,
                   "side": "dst" if info.get("side") == "dst" else "src"},
        "quote": quote,
        "comment": comment,
        "question": str(info.get("question") or "").strip()[:1000],
        "answer": answer,
        "tags": info.get("tags"),
    })


def delete_annotation(doc_key, aid):
    from app import anno
    anno.delete_annotation(str(aid or ""))
    return True


# ---------------------------------------------------------------- 裁图

def crop_png(path, page_no, rect, scale=CROP_SCALE):
    """按 PDF 坐标裁一块图（图表/独立公式用）。坐标是左上角原点的点单位。"""
    doc_id = _doc_id(path)
    cache_dir = os.path.join(_doc_dir(doc_id), "crops")
    os.makedirs(cache_dir, exist_ok=True)
    key = hashlib.sha1(("%d|%s|%s|%s" % (CROP_VERSION, page_no, rect, scale))
                       .encode("utf-8")).hexdigest()[:20]
    fp = os.path.join(cache_dir, key + ".png")
    if os.path.isfile(fp):
        with open(fp, "rb") as f:
            return f.read()
    try:
        import pypdfium2 as pdfium
    except ImportError:
        raise ReaderError("缺少 pypdfium2，无法裁图")
    pdf = pdfium.PdfDocument(path)
    try:
        page = pdf[page_no - 1]
        try:
            # 先在整页上渲染再裁剪：不同 pypdfium2 版本对 render(crop=…) 的
            # 坐标单位理解不一致（device px / PDF pt），曾因此留下错位重叠的
            # 损坏裁图；整页渲染 + 本地裁剪不受版本影响。
            pil = page.render(scale=scale).to_pil()
            pw, ph = pil.size
            x0, y0, x1, y1 = [float(v) for v in rect]
            pad = 4.0
            left = max(0, int(round((x0 - pad) * scale)))
            top = max(0, int(round((y0 - pad) * scale)))
            right = min(pw, int(round((x1 + pad) * scale)))
            bottom = min(ph, int(round((y1 + pad) * scale)))
            if right - left < 8 or bottom - top < 8:
                raise ReaderError("裁图区域太小")
            pil = pil.crop((left, top, right, bottom))
            import io
            buf = io.BytesIO()
            pil.save(buf, format="PNG", optimize=True)
        finally:
            try:
                page.close()
            except Exception:
                pass
    finally:
        try:
            pdf.close()
        except Exception:
            pass
    raw = buf.getvalue()
    tmp = fp + ".%d.tmp" % os.getpid()
    with open(tmp, "wb") as f:
        f.write(raw)
    os.replace(tmp, fp)
    return raw


FORMULA_PROMPT = (
    "这是一篇学术论文里的一块数学公式截图（可能是一行，也可能是多行公式组）。"
    "请只输出它的 LaTeX 源码，不要 $、不要代码块、不要解释。"
    "多行公式用 \\begin{aligned} … \\end{aligned} 保持原来的换行与对齐；"
    "保留上下标、分式、根号、集合、求和、箭头等符号；公式末尾的编号如 (2) 可以省略。"
    "注意：向下取整是 \\lfloor … \\rfloor，不要写成 b√nc 之类的近似；"
    "不要臆造命令或符号，看不清的地方宁可保守输出也不要用占位符；"
    "如果图中不是数学公式，只输出 NOT_FORMULA。"
)


def _latex_sane(t):
    """公式识别结果的硬校验：括号 / 环境必须成对，且不能混入中文说明。

    模型偶尔会返回「解释 + 半截公式」，KaTeX 渲染失败后会显示成红色报错；
    这样的结果宁可不缓存，让前端继续用截图。
    """
    t = str(t or "").strip()
    if not t or len(t) > 800:
        return False
    if _CJK_RE.search(t):
        return False
    depth = 0
    for i, ch in enumerate(t):
        if i and t[i - 1] == "\\":
            continue                     # \{ \} 是转义字符，不参与配对
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0:
                return False
    if depth:
        return False
    begins = re.findall(r"\\begin\s*\{([^{}]+)\}", t)
    ends = re.findall(r"\\end\s*\{([^{}]+)\}", t)
    return sorted(begins) == sorted(ends)


def formula_ai_available():
    """视觉/聊天模型是否可用于公式识别（只看配置，不发请求）。"""
    try:
        from app import imaging
        return bool(imaging.vision_available())
    except Exception:                    # noqa: BLE001
        return False


def _clean_latex(text):
    t = (text or "").strip()
    t = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", t).strip()
    t = t.strip("$").strip()
    t = re.sub(r"^\s*(LaTeX|latex|公式)\s*[:：]\s*", "", t)
    t = re.sub(r"^\\\[|\\\]$", "", t).strip()
    t = t.replace("\n", " ").strip()
    return t


def cached_formula(path, page_no, rect):
    """只读公式识别缓存；不触发视觉模型。

    导出时用来优先把公式排成数学（mathtext），没有缓存就回退裁图。
    """
    if not page_no or not rect or len(rect) != 4:
        return ""
    if not path or not os.path.isfile(path):
        return ""                        # 没有原文件（docx/pptx / 单测）：没有裁图也没有 LaTeX
    doc_id = _doc_id(path)
    cache_dir = os.path.join(_doc_dir(doc_id), "formulas")
    # 注意：公式缓存的 key 不含 CROP_VERSION —— 裁图修复后旧识别结果仍可用，
    # 是否可信由导出侧 _math_trusted() 再筛一次（错得离谱就回退裁图）。
    key = hashlib.sha1(("%s|%d|%s" % (FORMULA_VERSION, page_no, rect))
                       .encode("utf-8")).hexdigest()[:20]
    fp = os.path.join(cache_dir, key + ".json")
    if not os.path.isfile(fp):
        return ""
    try:
        data = read_json(fp) or {}
        return str(data.get("latex") or "")
    except Exception:                    # noqa: BLE001  缓存坏了就当没有
        return ""


def recognize_formula(path, page_no, rect):
    """把一行公式的裁图交给视觉模型转成 LaTeX（结果按文档+坐标缓存）。"""
    doc_id = _doc_id(path)
    cache_dir = os.path.join(_doc_dir(doc_id), "formulas")
    os.makedirs(cache_dir, exist_ok=True)
    key = hashlib.sha1(("%s|%d|%s" % (FORMULA_VERSION, page_no, rect))
                       .encode("utf-8")).hexdigest()[:20]
    fp = os.path.join(cache_dir, key + ".json")
    if os.path.isfile(fp):
        try:
            data = read_json(fp) or {}
            if data.get("latex"):
                return data["latex"]
        except Exception:                # noqa: BLE001  缓存坏了就重识别
            pass
    if not formula_ai_available():
        raise ReaderError("没有配置可用的视觉/聊天模型，无法识别公式")
    import base64
    from app import imaging
    png = crop_png(path, page_no, rect)
    b64 = base64.b64encode(png).decode("ascii")
    raw = imaging.recognize_image_fallback("image/png", b64, FORMULA_PROMPT)
    latex = _clean_latex(raw)
    if not latex or latex.upper().startswith(("NOT_FORMULA", "❌", "⚠️")):
        raise ReaderError("这行不是可识别的公式")
    if len(latex) > 800:
        raise ReaderError("公式识别结果过长")
    if not _latex_sane(latex):
        raise ReaderError("公式识别结果不完整（LaTeX 括号/环境不配对）")
    write_json(fp, {"latex": latex})
    return latex


# ---------------------------------------------------------------- 翻译任务

TRANSLATE_SYS = """你是学术论文的专业翻译引擎。把用户给的英文段落翻译成简体中文。

硬性要求：
1. 只输出 JSON 数组，形如 [{"i":1,"t":"译文"}]，不要解释、不要 markdown 代码块。
2. 原样保留 ⟦F1⟧ 这类占位符（数量和位置都要对应），不要翻译或展开它们。
3. 数字、公式编号、引用编号（[12]、(3.2)、(Smith et al., 2020)）、图表编号保持原样。
4. 学术语气，忠实原文，不增删内容，不合并或拆分条目。
5. 译文中不要出现"根据原文""这句话""以下是"之类的解释性语言。
6. 行内数学表达式用 $...$ 包裹，并按原字符照抄，不要翻译或改写；
   ⟦F1⟧ 这类占位符原样保留，不要加 $、不要展开。
7. URL、DOI、邮箱、代码标识符、变量名、希腊字母、单位保持原样，不要在中间插空格。
8. 作者名、单位、基金编号、致谢中的人名保持原文，不要音译。
9. 章节标题保留原有编号（如 3.2）；译文要短，不要改写成句子。
10. 如果原文已经是中文，原样返回。"""

_JOBS = {}
_JOBS_LOCK = threading.Lock()
_JOB_TTL = 1800
_JOB_MAX_EVENTS = 4000
_ACTIVE = {}                       # doc_key -> job_id（同一文档同时只跑一个翻译任务）
_ACTIVE_LOCK = threading.Lock()


def _gc_jobs():
    now = time.time()
    with _JOBS_LOCK:
        for k, j in list(_JOBS.items()):
            if j.get("done") and now - j.get("ts", 0) > _JOB_TTL:
                _JOBS.pop(k, None)


def _emit(job, obj):
    with _JOBS_LOCK:
        job["seq"] += 1
        item = dict(obj)
        item["_seq"] = job["seq"]
        job["events"].append(item)
        if len(job["events"]) > _JOB_MAX_EVENTS:
            del job["events"][: len(job["events"]) - _JOB_MAX_EVENTS]
        job["ts"] = time.time()
        if obj.get("type") == "done":
            job["done"] = True


def job_snapshot(job_id):
    return _JOBS.get(job_id)


def cancel_job(job_id):
    job = _JOBS.get(job_id)
    if not job:
        return False
    job["cancel"] = True
    return True


def _model_of(cfg):
    """翻译缓存 / 术语表文件名里的模型标识：必须和实际调用一致。

    翻译走 get_api_conf("chat")，所以这里只能用 chat_model；
    以前用 light_model 优先，换主模型不会让旧译文失效，只配轻量模型反而会误失效。
    """
    return (cfg.get("chat_model") or "").strip() or "chat"


def _parse_translations(raw, n):
    """从模型输出里抠出 [{i,t}]。模型偶尔会带代码块或前后废话。"""
    if not raw:
        return {}
    text = raw.strip()
    text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    a, b = text.find("["), text.rfind("]")
    if a >= 0 and b > a:
        text = text[a:b + 1]
    try:
        arr = json.loads(text)
    except Exception:
        # 兜底：逐个对象抠
        arr = []
        for m in re.finditer(r'\{\s*"i"\s*:\s*(\d+)\s*,\s*"t"\s*:\s*"((?:[^"\\]|\\.)*)"\s*\}', raw, re.S):
            try:
                arr.append({"i": int(m.group(1)), "t": json.loads('"%s"' % m.group(2))})
            except Exception:
                continue
    out = {}
    for it in arr if isinstance(arr, list) else []:
        try:
            i = int(it.get("i"))
            t = str(it.get("t") or "").strip()
        except Exception:
            continue
        if 1 <= i <= n and t:
            out[i] = t
    return out


def _placeholders(text):
    return sorted(re.findall(r"⟦F\d+⟧", text or ""))


_NUM_HEAD_RE = re.compile(r"^(\d+(?:\.\d+)*\.?)\s+")


def normalize_translation(b, text):
    """修译文里的两类脏数据（旧缓存也要能就地修，不用整篇重译）：

    1. 模型凭空造的占位符：math 里没有这个键，前端就原样显示「⟦F1⟧ 引言」；
    2. 标题编号被占位符吃掉：从原文开头的「1.2.」补回来。
    """
    s = str(text or "")
    if not s:
        return s
    math = (b or {}).get("math") or {}
    if "⟦" in s:
        s = re.sub(r"⟦(F\d+)⟧", lambda m: m.group(0) if m.group(1) in math else "", s)
        s = re.sub(r"[ \t]{2,}", " ", s).strip()
        if not s:
            return ""
    # 旧缓存里批翻译序号错位：1 个字符的源文被贴上了整句标题译文。
    # 与 _validate_translation 同一口径：短标签的译文不能明显长于源文，宁可不翻。
    src = str((b or {}).get("text") or "").strip()
    if len(src) <= 8 and len(s) > max(8, 3 * len(src)):
        return ""
    if (b or {}).get("kind") == "heading":
        m = _NUM_HEAD_RE.match(str((b or {}).get("text") or ""))
        if m:
            num = m.group(1)
            if not re.match(r"^%s(?:\s|$)" % re.escape(num), s):
                s = (num + " " + s.lstrip()).strip()
    return s


def normalize_translations(data, meta):
    """整份译文缓存过一遍 normalize_translation。"""
    blocks = {b.get("id"): b for b in ((meta or {}).get("blocks") or [])}
    out = {}
    for bid, txt in (data or {}).items():
        b = blocks.get(bid)
        out[bid] = normalize_translation(b, txt) if b else txt
    return out


def _src_of(b):
    """送模型的是保护过行内公式的 ttext；没有就用原文。"""
    return b.get("ttext") or b.get("text") or ""


"""翻译内核（v2）：章节上下文 + 术语表 + 结构化校验 + 三级重试。

与 v1 的差别：
- 每批只翻同一章节的段落，并把章节名 / 上一段原文 / 术语表一起给模型；
- 校验不再只看占位符：漏译、数字/引用编号丢失、长度异常都会被识别；
- 失败走「整批 → 逐段 → 未保护原文」三级回退，不再整段静默丢弃。
"""
GLOSSARY_VERSION = 1
_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_CITE_RE = re.compile(r"\[\d+(?:\s*[-,–]\s*\d+)*\]|\(\d+(?:\.\d+)+\)"
                      r"|(?:Theorem|Lemma|Corollary|Remark|Proposition)\s+\d+(?:\.\d+)*", re.I)
_STOPWORDS = set("""
a an the and or but if then else for while of in on at to from by with without into over under
is are was were be been being do does did done have has had having can could may might must shall
should will would this that these those it its their there here which who whom whose what when
where why how as such than too very more most less least also only just not no nor so both each
other another any all some many few several much own same different using used use make makes made
give gives given show shows shown see sees seen let lets get gets got take takes taken
we our you your they them he she his her i me my
""".split())


def _section_map(blocks):
    """块 → 所属章节标题；返回 (章节列表, {block_id: 章节标题})。"""
    sections, mapping, cur = [], {}, ""
    for b in blocks or []:
        if b.get("kind") == "heading":
            title = (b.get("text") or "").strip()
            # 过长的「标题」多半是把摘要/正文段落误判成 heading，
            # 拿它当章节名会让整批翻译收到错误上下文。
            if len(title) <= 70:
                cur = title
                sections.append({"page": b.get("page"), "title": cur,
                                 "level": b.get("level") or 2})
        if b.get("id"):
            mapping[b["id"]] = cur
    return sections, mapping


def _doc_title(blocks):
    for b in blocks or []:
        if b.get("kind") == "heading" and (b.get("text") or "").strip():
            return (b.get("text") or "").strip()
    return ""


def _abstract_text(blocks, limit=700):
    """摘要正文（最多 limit 字），给每批翻译提供术语与语气参考。"""
    out, hit = [], False
    for b in blocks or []:
        if not isinstance(b, dict):
            continue
        text = (b.get("text") or "").strip()
        if not hit:
            if _ABSTRACT_RE.match(text):
                hit = True
            continue
        if b.get("kind") == "heading":
            break
        if (len(out) and len(text) < 60
                and re.match(r"^\d+(?:\.\d+)*\.?\s+[A-Z]", text)):
            break
        if b.get("kind") in ("text", "list") and text:
            out.append(text)
            if sum(len(x) for x in out) >= limit:
                break
    return re.sub(r"\s+", " ", " ".join(out))[:limit]


def _term_candidates(blocks, limit=40):
    """从全文抽取候选术语：频次 × 词数加权，过滤常见虚词。"""
    from collections import Counter
    cnt = Counter()
    for b in blocks or []:
        if b.get("kind") not in ("text", "heading"):
            continue
        text = b.get("text") or ""
        for m in re.finditer(r"[A-Za-z][A-Za-z\-]{2,}(?:\s+[A-Za-z][A-Za-z\-]{2,}){0,2}",
                             text):
            word = m.group(0).strip()
            parts = word.split()
            if len(word) < 4 or all(p.lower() in _STOPWORDS for p in parts):
                continue
            cnt[word] += 1
    ranked = sorted(cnt.items(),
                    key=lambda kv: (-(kv[1] * (1 + 0.6 * (len(kv[0].split()) - 1))), kv[0]))
    out, seen = [], set()
    for word, n in ranked:
        if n < 2:
            continue
        key = word.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(word)
        if len(out) >= limit:
            break
    return out


def _glossary_path(doc_id, lang, model):
    safe = re.sub(r"[^0-9A-Za-z._-]+", "_", "%s_%s" % (lang, model))[:60]
    return os.path.join(_doc_dir(doc_id), "glossary_%s.json" % safe)


def _parse_glossary(raw):
    text = (raw or "").strip()
    text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text)
    a, b = text.find("["), text.rfind("]")
    if a >= 0 and b > a:
        text = text[a:b + 1]
    try:
        arr = json.loads(text)
    except Exception:
        return []
    out, seen = [], set()
    for it in arr if isinstance(arr, list) else []:
        if not isinstance(it, dict):
            continue
        src, dst = str(it.get("src") or "").strip(), str(it.get("dst") or "").strip()
        if not src or not dst or src.lower() in seen:
            continue
        seen.add(src.lower())
        out.append({"src": src[:80], "dst": dst[:80]})
        if len(out) >= 40:
            break
    return out


def ensure_glossary(path, blocks, lang, lang_name="简体中文", force=False):
    """文档级术语表：抽取候选 → 一次模型调用生成统一译名 → 按文档缓存。"""
    from app import llm
    from app.config import get_api_conf, load_config
    doc_id = _doc_id(path)
    cfg = load_config()
    model = _model_of(cfg)
    fp = _glossary_path(doc_id, lang, model)
    if not force and os.path.isfile(fp):
        cached = read_json(fp, None) or {}
        if cached.get("terms"):
            return cached
    candidates = _term_candidates(blocks)
    sections, _m = _section_map(blocks)
    data = {"version": GLOSSARY_VERSION, "lang": lang, "model": model,
            "title": _doc_title(blocks), "terms": [], "at": time.time()}
    if candidates:
        api_key, api_url, api_model = get_api_conf("chat", cfg)
        if api_key:
            sysp = ("你是学术论文的术语抽取器。根据标题、章节与候选词，输出这篇论文的中文术语表。"
                    "只输出 JSON 数组 [{\"src\":\"...\",\"dst\":\"...\"}]，不要解释。"
                    "只收录真正的领域术语（方法名 / 数学对象 / 专有名词 / 固定搭配），"
                    "不要收普通动词、形容词与泛泛的词；数学符号、缩写、人名保持原样；最多 40 条。")
            user = ("目标语言：%s\n标题：%s\n章节：%s\n候选：%s"
                    % (lang_name, data["title"] or "（未知）",
                       "；".join(s["title"] for s in sections[:12]) or "（无）",
                       json.dumps(candidates, ensure_ascii=False)))
            try:
                raw = llm._quick_chat(
                    api_key, api_url, api_model,
                    [{"role": "system", "content": sysp},
                     {"role": "user", "content": user}],
                    max_tokens=1800, source="reader", use_light=False, timeout=(10, 90))
                data["terms"] = _parse_glossary(raw)
            except Exception as e:                 # noqa: BLE001  术语表失败不能拖垮翻译
                print("[阅读器] 术语表生成失败：%s" % e)
    try:
        write_json(fp, data, indent=None)
    except Exception:
        pass
    return data


def load_glossary(path, lang):
    from app.config import load_config
    fp = _glossary_path(_doc_id(path), lang, _model_of(load_config()))
    return read_json(fp, None) or {"terms": [], "title": ""}


def save_glossary(path, lang, terms):
    """保存人工编辑后的术语表。"""
    from app.config import load_config
    doc_id = _doc_id(path)
    model = _model_of(load_config())
    fp = _glossary_path(doc_id, lang, model)
    cur = read_json(fp, None) or {}
    clean, seen = [], set()
    for it in terms or []:
        if not isinstance(it, dict):
            continue
        src = str(it.get("src") or "").strip()
        dst = str(it.get("dst") or "").strip()
        if not src or not dst or src.lower() in seen:
            continue
        seen.add(src.lower())
        clean.append({"src": src[:80], "dst": dst[:80]})
        if len(clean) >= 80:
            break
    cur.update({"version": GLOSSARY_VERSION, "lang": lang, "model": model,
                "terms": clean, "at": time.time()})
    write_json(fp, cur, indent=None)
    return cur


def _already_target(text, lang):
    """原文已经是目标语言（例如中文论文译成中文）就不必再翻。"""
    t = text or ""
    if not t.strip():
        return True
    if lang.startswith("zh"):
        cjk = len(_CJK_RE.findall(t))
        return cjk / max(1, len(t)) > 0.5
    return False


_META_DST_RE = re.compile(r"根据原文|这句话|以下是|本段译文|译文[:：]\s*$")


def _validate_translation(src, dst, lang):
    """硬校验：空译文 / 占位符不一致 / 回显英文 / 解释性废话 / 长度异常。"""
    if not (dst or "").strip():
        return "empty"
    if _placeholders(src) != _placeholders(dst):
        return "placeholder"
    if _META_DST_RE.search(dst):
        return "meta"
    if lang.startswith("zh") and re.search(r"[A-Za-z]", src):
        if not _CJK_RE.search(dst):
            return "untranslated"
        latin = len(re.findall(r"[A-Za-z]", dst)) / max(1.0, len(dst))
        if latin > 0.70 and len(src) > 60:
            return "untranslated"
    # 短标签（单个字母 / 编号 / 缩写）被换成了别处的整句译文：批翻译按序号回填，
    # 模型少给一项就会整体错位；宁可不翻，也不能把标题贴到图注上。
    _s = (src or "").strip()
    if len(_s) <= 8 and len(dst) > max(8, 3 * len(_s)):
        return "length"
    # 短标题（Introduction → 引言）本来就远短于原文，长度比只对长段落生效
    ratio = len(dst) / max(1, len(src))
    if len(src) >= 40 and (ratio < 0.30 or ratio > 3.5):
        return "length"
    return ""


def _soft_issues(src, dst):
    """软校验：数字 / 引用编号丢失。只记录，不重试。"""
    bad = []
    for n in re.findall(r"\d+(?:\.\d+)?", src or ""):
        if n not in (dst or ""):
            bad.append(n)
    for c in _CITE_RE.findall(src or ""):
        if c not in (dst or ""):
            bad.append(c)
    return bad[:4]


def _translate_one(b, lang_name, cfg, ctx):
    """单段重试：带章节 / 术语表，不要求占位符（内容少，模型更稳）。"""
    from app import llm
    from app.config import get_api_conf
    api_key, api_url, api_model = get_api_conf("chat", cfg)
    sysp = "你是学术论文翻译引擎。只输出译文，不要解释、不要 markdown。"
    gloss = (ctx or {}).get("glossary") or []
    if gloss:
        sysp += "\n术语表（必须遵守）：" + "；".join(
            "%s→%s" % (t["src"], t["dst"]) for t in gloss[:40])
    if (ctx or {}).get("title"):
        sysp += "\n论文标题：%s" % ctx["title"][:200]
    if (ctx or {}).get("abstract"):
        sysp += "\n摘要参考（不要翻译）：%s" % ctx["abstract"][:400]
    if (ctx or {}).get("section"):
        sysp += "\n当前章节：%s" % ctx["section"]
    src = _src_of(b)
    raw = llm._quick_chat(
        api_key, api_url, api_model,
        [{"role": "system", "content": sysp},
         {"role": "user", "content": "翻译成%s（保留 ⟦F1⟧ 占位符）：\n%s" % (lang_name, src)}],
        max_tokens=min(3000, max(400, len(src) * 2)), source="reader",
        use_light=False, timeout=(10, 90))
    t = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", (raw or "")).strip()
    if not t or t.startswith(('{"', '[{"')):
        return ""
    return t


def _translate_batch(blocks, lang_name, cfg, context=None):
    """翻译一批段落，返回 {block_id: 译文}；失败走三级回退。"""
    from app import llm
    from app.config import get_api_conf
    ctx = context if isinstance(context, dict) else {"title": str(context or "")}
    api_key, api_url, api_model = get_api_conf("chat", cfg)
    items = [{"i": i + 1, "t": _src_of(b)} for i, b in enumerate(blocks)]
    sysp = TRANSLATE_SYS
    gloss = ctx.get("glossary") or []
    if gloss:
        sysp += "\n\n术语表（必须逐条遵守，译名要一致）：\n" + "\n".join(
            "- %s → %s" % (t["src"], t["dst"]) for t in gloss[:40])
    if ctx.get("title"):
        sysp += "\n\n论文标题：%s" % ctx["title"][:200]
    if ctx.get("abstract"):
        sysp += "\n\n摘要（只用于统一术语与语气，不要翻译）：\n" + ctx["abstract"][:600]
    if ctx.get("section"):
        sysp += "\n\n当前章节：%s" % ctx["section"]
    if ctx.get("neighbor"):
        sysp += "\n\n上一段原文（只用于理解上下文，不要翻译）：\n" + ctx["neighbor"][:800]
    prompt = ("目标语言：%s\n\n段落（JSON）：\n%s"
              % (lang_name, json.dumps(items, ensure_ascii=False)))
    need = min(8000, max(800, sum(len(_src_of(b)) for b in blocks) * 2 + 300))
    try:
        raw = llm._quick_chat(
            api_key, api_url, api_model,
            [{"role": "system", "content": sysp},
             {"role": "user", "content": prompt}],
            max_tokens=need, source="reader", use_light=False, timeout=(10, 120))
    except Exception as e:                       # noqa: BLE001
        # 网络 / 限流 / 超时：整批直接抛会让 4 段一起失败，
        # 这里退化成逐段调用，能救回多少算多少。
        print("[阅读器] 批翻译失败，转逐段重试：%s" % e)
        raw = ""
    got = _parse_translations(raw, len(blocks))
    out, retry = {}, []
    for i, b in enumerate(blocks, 1):
        t = got.get(i)
        if t and not _validate_translation(_src_of(b), t, ctx.get("lang") or "zh"):
            out[b["id"]] = t
        else:
            retry.append(b)
    # 二级：逐段重试
    for b in retry:
        try:
            t = _translate_one(b, lang_name, cfg, ctx)
        except Exception:
            t = ""
        if t and not _validate_translation(_src_of(b), t, ctx.get("lang") or "zh"):
            out[b["id"]] = t
    # 三级：未保护原文兜底（公式可能被改写，但总比整段丢失好）
    for b in retry:
        if b["id"] in out:
            continue
        src = (b.get("text") or "").strip()
        if not src or len(src) > 1500:
            continue
        try:
            t = llm._quick_chat(
                api_key, api_url, api_model,
                [{"role": "system", "content": "你是学术论文翻译引擎。只输出译文，不要解释。"},
                 {"role": "user", "content": "翻译成%s：\n%s" % (lang_name, src)}],
                max_tokens=min(3000, max(400, len(src) * 2)), source="reader",
                use_light=False, timeout=(10, 90))
        except Exception:
            continue
        t = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", (t or "")).strip()
        if t and not t.startswith(('{"', '[{"')):
            out[b["id"]] = t
    return out


def _batch_ctx(batch, corpus, pos, sec_map, terms, lang):
    """一批段落的上下文：章节名、上一段原文、术语表。"""
    i0 = pos.get(batch[0].get("id"), -1)
    prev = ""
    for j in range(i0 - 1, -1, -1):
        if corpus[j].get("kind") in ("text", "heading"):
            prev = (corpus[j].get("text") or "").strip()
            if prev:
                break
    return {"section": sec_map.get(batch[0].get("id"), ""), "neighbor": prev,
            "glossary": terms, "lang": lang,
            "title": _doc_title(corpus), "abstract": _abstract_text(corpus)}


def start_translate(path, blocks, lang, lang_name, force=False, context="",
                    all_blocks=None, retry_failed=False):
    """启动（或复用）翻译任务，返回 (job_id, 待翻译段数)。

    blocks = 本次范围（可按页过滤）；all_blocks = 全文，用于章节结构与术语表。
    """
    _gc_jobs()
    doc_id = _doc_id(path)
    with _ACTIVE_LOCK:
        jid = _ACTIVE.get(doc_id)
        if jid:
            job = _JOBS.get(jid)
            if job and not job.get("done"):
                return jid, job.get("total", 0)
    from app.config import load_config
    cfg = load_config()
    model = _model_of(cfg)
    cache = {} if force else (read_json(_trans_path(doc_id, lang, model), {}) or {})
    corpus = all_blocks or blocks
    _sections, sec_map = _section_map(corpus)
    try:
        terms = (ensure_glossary(path, corpus, lang, lang_name) or {}).get("terms") or []
    except Exception as e:                       # noqa: BLE001  术语表失败不影响翻译
        print("[阅读器] 读取术语表失败：%s" % e)
        terms = []
    skip_kinds = ("figure", "formula", "ref", "meta", "table", "hidden")
    todo = [b for b in blocks if _src_of(b).strip()
            and b.get("kind") not in skip_kinds
            and not _already_target(_src_of(b), lang)
            and (force or not (cache.get(b["id"]) or "").strip())]
    skipped = sum(1 for b in blocks if b.get("kind") in skip_kinds)
    jid = "rd_%d_%s" % (int(time.time() * 1000), os.urandom(3).hex())
    job = {"id": jid, "doc": doc_id, "done": False, "cancel": False, "ts": time.time(),
           "seq": 0, "events": [], "total": len(todo),
           "cache_path": _trans_path(doc_id, lang, model)}
    with _JOBS_LOCK:
        _JOBS[jid] = job
    with _ACTIVE_LOCK:
        _ACTIVE[doc_id] = jid
    _emit(job, {"type": "glossary", "count": len(terms)})
    if not todo:
        _emit(job, {"type": "done", "ok": 0, "failed": 0, "cached": True,
                    "skipped": skipped})
        with _ACTIVE_LOCK:
            _ACTIVE.pop(doc_id, None)
        return jid, 0
    _emit(job, {"type": "queued", "total": len(todo), "skipped": skipped})

    # 同章节连续段落成一批；跨章节一律切开，保证模型知道自己在翻哪一节
    batches, cur, size, cur_sec = [], [], 0, object()
    for b in todo:
        sec = sec_map.get(b["id"], "")
        n = len(_src_of(b))
        if cur and (sec != cur_sec or len(cur) >= 4 or size + n > 1600):
            batches.append(cur)
            cur, size = [], 0
        cur.append(b)
        size += n
        cur_sec = sec
    if cur:
        batches.append(cur)

    def run():
        ok = failed = 0
        store = dict(cache)
        lock = threading.Lock()
        pos = {b.get("id"): i for i, b in enumerate(corpus)}
        try:
            workers = int(cfg.get("reader_translate_parallel") or 3)
        except (TypeError, ValueError):
            workers = 3
        workers = max(1, min(4, workers))
        from concurrent.futures import ThreadPoolExecutor, as_completed
        try:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = {}
                for batch in batches:
                    if job.get("cancel"):
                        break
                    ctx = _batch_ctx(batch, corpus, pos, sec_map, terms, lang)
                    futs[ex.submit(_translate_batch, batch, lang_name, cfg, ctx)] = batch
                for fut in as_completed(futs):
                    batch = futs[fut]
                    if job.get("cancel"):
                        continue
                    try:
                        got = fut.result()
                    except Exception as e:           # noqa: BLE001
                        failed += len(batch)
                        _emit(job, {"type": "error", "message": str(e)[:300],
                                    "ids": [b["id"] for b in batch]})
                        continue
                    for b in batch:
                        t = normalize_translation(b, got.get(b["id"]) or "")
                        if not t:
                            failed += 1
                            _emit(job, {"type": "block", "id": b["id"], "page": b["page"],
                                        "text": "", "failed": True})
                            continue
                        ok += 1
                        warn = _soft_issues(_src_of(b), t)
                        with lock:
                            store[b["id"]] = t
                        ev = {"type": "block", "id": b["id"], "page": b["page"], "text": t}
                        if warn:
                            ev["warn"] = warn
                        _emit(job, ev)
                    with lock:
                        try:
                            write_json(job["cache_path"], store, indent=None)
                        except Exception:
                            pass
            _emit(job, {"type": "done", "ok": ok, "failed": failed, "skipped": skipped})
        finally:
            with _ACTIVE_LOCK:
                if _ACTIVE.get(doc_id) == jid:
                    _ACTIVE.pop(doc_id, None)

    threading.Thread(target=run, daemon=True).start()
    return jid, len(todo)


def load_translations(path, lang):
    from app.config import load_config
    cfg = load_config()
    doc_id = _doc_id(path)
    data = read_json(_trans_path(doc_id, lang, _model_of(cfg)), {}) or {}
    # 旧缓存（rd4 及更早）里可能有模型造的占位符 / 丢编号的标题：读的时候统一修，
    # 这样不重译也能让译页正常显示。
    try:
        meta = read_json(_blocks_path(doc_id), None)
        if meta:
            data = normalize_translations(data, meta)
    except Exception as e:              # noqa: BLE001  缓存坏了不能拦住打开
        print("[阅读器] 译文缓存规范化失败：%s" % e)
    return data
