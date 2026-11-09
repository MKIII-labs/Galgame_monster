# -*- coding: utf-8 -*-
"""译文导出：仅译文 PDF / 双语对照 PDF / Markdown。

为什么不再沿用「原页铺图 + 白块盖译文」：
- 页面高度固定，译文比原文长时只能把字号一路压到 3~4pt，纸上/屏幕上都看不清；
- 白块盖住的是原页像素，双栏页经常压到栏间空白，还会顺手盖掉图注和公式编号。

这里改成真正的重排文档：
- 统一字号与行距，按阅读流顺序输出；图表 / 独立公式按原页坐标裁图后插回原位；
- 字体嵌入（Noto Serif SC / 黑体），中文可复制、可搜索；
- 标题生成 PDF 书签（目录），页脚带页码；
- 双语模式一段原文 + 一段译文，原文弱化成灰色小字，阅读时以译文为主。

只使用已缓存译文：缺译的段落保留原文，并在返回值里报告缺译段数。
"""
import copy
import io
import os
import re
import threading
import time

try:                                     # 延迟依赖：没装 reportlab 时阅读器仍能正常用
    from reportlab.lib.colors import HexColor
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas as rl_canvas
    _HAS_RL = True
except ImportError:                      # pragma: no cover - 取决于运行环境
    _HAS_RL = False

from app import reader

PAGE_W = 595.28                       # A4：和 parse_doc 的伪分页尺寸保持一致
PAGE_H = 841.89
MX = 62.0                             # 左右页边距
MT = 58.0                             # 上边距
MB = 60.0                             # 下边距（页脚画在这里）
CONTENT_W = PAGE_W - MX * 2

BODY_FONT = "RdSerif"
HEAD_FONT = "RdSans"
LATIN_FONT = "RdLatin"          # 西文扩展（ő / ł / š 等）：中文字体通常缺这些字形
SYMBOL_FONT = "RdSymbol"        # 数学符号（↦ / ∉ / ⌊⌋ 等）：Segoe UI 也缺
_FONTS_OK = False
_REGISTERED = set()
_FONT_LOCK = threading.Lock()

# 字重：正文 400 / 标题 700。Noto 的 *-VF.ttf 是**变量字体**，reportlab 不认 wght 轴，
# 只会读默认实例（Noto Serif SC = ExtraLight 200、Noto Sans SC = Thin 100），
# 导出结果比阅读器细一大截。注册前先用 fontTools 实例化成静态字重。
BODY_WEIGHT = 400
HEAD_WEIGHT = 700
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FONT_CACHE = os.path.join(_ROOT, "data", "cache", "fonts")

# 中文字体候选：Noto Serif SC 最像论文正文；不在了退回宋体 / 黑体。
_SERIF_CANDIDATES = [
    "C:/Windows/Fonts/NotoSerifSC-VF.ttf",
    "C:/Windows/Fonts/simsun.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
]
_SANS_CANDIDATES = [
    "C:/Windows/Fonts/NotoSansSC-VF.ttf",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/msyh.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]
_LATIN_CANDIDATES = [
    "C:/Windows/Fonts/segoeui.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
]
_SYMBOL_CANDIDATES = [
    "C:/Windows/Fonts/seguisym.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
]


def _is_variable(path):
    """是不是带 fvar 轴的变量字体。读不到就当成静态（宁可不实例化）。"""
    try:
        from fontTools.ttLib import TTFont as _FTFont
        f = _FTFont(path, lazy=True)
        try:
            return "fvar" in f
        finally:
            f.close()
    except Exception:                    # noqa: BLE001  没装 fontTools / 文件损坏
        return False


def _instance_vf(src, weight):
    """把变量字体实例化到指定字重，返回缓存文件；没 fontTools 或失败返回 ""。

    缓存到 data/cache/fonts，源文件更新后自动重建（比较 mtime）。
    """
    try:
        from fontTools.ttLib import TTFont as _FTFont
        from fontTools.varLib.instancer import instantiateVariableFont
    except Exception:                    # noqa: BLE001
        return ""
    try:
        stem = re.sub(r"[^A-Za-z0-9_.-]", "_", os.path.splitext(os.path.basename(src))[0])
        dst = os.path.join(_FONT_CACHE, "%s-w%d.ttf" % (stem, weight))
        if os.path.isfile(dst) and os.path.getmtime(dst) >= os.path.getmtime(src):
            return dst
        os.makedirs(_FONT_CACHE, exist_ok=True)
        font = _FTFont(src)
        inst = instantiateVariableFont(font, {"wght": weight}, inplace=True,
                                       updateFontNames=True)
        tmp = dst + ".%d.tmp" % os.getpid()
        inst.save(tmp)
        os.replace(tmp, dst)
        return dst
    except Exception:                    # noqa: BLE001  实例化失败就退回静态候选
        return ""


def _register(name, candidates, weight=None):
    for fp in candidates:
        if not os.path.isfile(fp):
            continue
        use = fp
        if weight and _is_variable(fp):
            use = _instance_vf(fp, weight)
            if not use:
                continue                 # 没 fontTools：跳过变量字体，用后面的静态候选
        try:
            # TTC 需要指定子字体；传关键字对 .ttf 无害
            kw = {"subfontIndex": 0} if use.lower().endswith(".ttc") else {}
            pdfmetrics.registerFont(TTFont(name, use, **kw))
            _REGISTERED.add(name)
            return True
        except Exception:                # noqa: BLE001  TTC 子字体参数不对等
            continue
    return False


def _ensure_fonts():
    global _FONTS_OK
    if _FONTS_OK:
        return True
    with _FONT_LOCK:
        if _FONTS_OK:
            return True
        _FONTS_OK = _register(BODY_FONT, _SERIF_CANDIDATES, BODY_WEIGHT)
        head_ok = _register(HEAD_FONT, _SANS_CANDIDATES, HEAD_WEIGHT)
        # 回退字体尽力而为：缺了只在遇到对应字符时退回主字体（可能画成方块）
        _register(LATIN_FONT, _LATIN_CANDIDATES)
        _register(SYMBOL_FONT, _SYMBOL_CANDIDATES)
        if not head_ok or not _FONTS_OK:
            # 退回内置字体：中文会缺字，但至少不崩（Windows 上基本不会走到）
            _FONTS_OK = False
        return _FONTS_OK


class ExportError(Exception):
    """导出可预期的失败，API 直接把文案返回给前端。"""


STYLES = {}
if _HAS_RL:
    STYLES = {
        "h1": {"font": HEAD_FONT, "size": 19.0, "leading": 28.0, "before": 0.0,
               "after": 16.0, "color": HexColor("#101418")},
        "h2": {"font": HEAD_FONT, "size": 13.5, "leading": 21.0, "before": 15.0,
               "after": 8.0, "color": HexColor("#15191d")},
        "body": {"font": BODY_FONT, "size": 10.5, "leading": 17.6, "before": 0.0,
                 "after": 9.0, "color": HexColor("#1b1f23")},
        "src": {"font": BODY_FONT, "size": 8.9, "leading": 14.2, "before": 0.0,
                "after": 4.0, "color": HexColor("#6d7680")},
        "ref": {"font": BODY_FONT, "size": 9.2, "leading": 13.8, "before": 0.0,
                "after": 3.0, "color": HexColor("#3a4148")},
        "note": {"font": BODY_FONT, "size": 9.0, "leading": 13.8, "before": 0.0,
                 "after": 3.0, "color": HexColor("#8a929b")},
    }

_CJK = re.compile(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]")
_CJK_INLINE = re.compile(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]"
                         r"|[^\s\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]+")
_FORMULA_REF = re.compile(r"⟦(F\d+)⟧")
# 这些标点后面不补空格：「Transformer，它」不能被拆成「Transformer， 它」
_NO_BREAK_HEAD = "。，、；：！？）》」』】〕…—%"


def _is_cjk(ch):
    return bool(ch and _CJK.match(ch))


def _glue_space(prev, nxt):
    """中文与西文之间补一个空格（排版习惯），中文标点 / 中文之间不补。"""
    if not prev or not nxt:
        return False
    if prev in _NO_BREAK_HEAD or nxt in _NO_BREAK_HEAD:
        return False
    return not (_is_cjk(prev) and _is_cjk(nxt))


def _plain(text):
    """去掉控制字符 / 私用区字形并压平换行：PDF 里逐字定位会带 (\x01) 之类噪声。"""
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\u200b-\u200d\ufeff\ue000-\uf8ff]",
                  "", str(text or ""))
    return re.sub(r"\s+", " ", text).strip()


def restore_inline(text, math):
    """把译文里的 ⟦F1⟧ 还原成原文的行内公式，导出后才不会出现占位符。"""
    return _FORMULA_REF.sub(lambda m: (math or {}).get(m.group(1)) or "", str(text or ""))


def _translation_of(block, translations):
    return (translations or {}).get(block.get("id")) or ""


_CMAP_CACHE = {}
_WIDTH_CACHE = {}


def _cmap(font):
    """已注册字体能画的码位集合；拿不到就当空（只用主字体）。"""
    if font in _CMAP_CACHE:
        return _CMAP_CACHE[font]
    try:
        face = pdfmetrics.getFont(font).face
        cm = getattr(face, "charToGlyph", {}) or {}
    except Exception:                    # noqa: BLE001
        cm = {}
    _CMAP_CACHE[font] = cm
    return cm


def _font_for(ch, primary):
    """按字符挑字体：主字体缺字时依次用西文扩展 / 数学符号 / 另一套中文字体。"""
    cp = ord(ch)
    if cp < 128:
        return primary
    if cp in _cmap(primary):
        return primary
    for name in (LATIN_FONT, SYMBOL_FONT, BODY_FONT, HEAD_FONT):
        if name in _REGISTERED and cp in _cmap(name):
            return name
    return primary


def _char_width(ch, font):
    key = (ch, font)
    w = _WIDTH_CACHE.get(key)
    if w is None:
        try:
            w = float(stringWidth(ch or " ", font, 1.0))
        except Exception:                # noqa: BLE001
            w = 0.5
        _WIDTH_CACHE[key] = w
    return w


def _measure(text, font, size):
    """逐字符量宽：字体回退后每个字符可能来自不同字体。"""
    return sum(_char_width(ch, _font_for(ch, font)) for ch in text) * size


def _draw_text(c, x, baseline, text, font, size):
    """按字体回退把一行拆成若干 run 画出去（drawString 共享同一基线）。"""
    cur = x
    run = []
    run_font = None
    for ch in text:
        f = _font_for(ch, font)
        if f != run_font:
            if run:
                c.setFont(run_font, size)
                c.drawString(cur, baseline, "".join(run))
                cur += _measure("".join(run), run_font, size)
            run = []
            run_font = f
        run.append(ch)
    if run:
        c.setFont(run_font, size)
        c.drawString(cur, baseline, "".join(run))


# 行首禁则 / 行尾禁则：中文标点不能出现在行首，开括号不能留在行尾
_NO_LINE_START = "。，、；：！？）》」』】〕…—%”’"
_NO_LINE_END = "（《「『【〔“‘"


def _fix_breaks(lines, font, size, first_w, rest_w):
    def limit(i):
        return first_w if i == 0 else rest_w

    for i in range(1, len(lines)):
        guard = 0
        while lines[i] and lines[i][0] in _NO_LINE_START and guard < 4:
            ch = lines[i][0]
            if _measure(lines[i - 1] + ch, font, size) <= limit(i - 1) + 0.6:
                lines[i - 1] += ch
                lines[i] = lines[i][1:]
            elif len(lines[i - 1]) > 1:
                lines[i] = lines[i - 1][-1] + lines[i]
                lines[i - 1] = lines[i - 1][:-1]
            else:
                break
            guard += 1
    for i in range(len(lines) - 1):
        guard = 0
        while lines[i] and lines[i][-1] in _NO_LINE_END and guard < 3:
            lines[i + 1] = lines[i][-1] + lines[i + 1]
            lines[i] = lines[i][:-1]
            guard += 1
    return lines


def _wrap(text, font, size, width, rest_width=None):
    """中文按字断行、西文按词断行；超长单词再按字符硬断。

    rest_width 用于悬挂缩进（列表续行比首行窄）；默认与 width 相同。
    宽度逐字符累加（字体回退后同一行可能混用多个字体），不是整串重算，
    长文档不会退化成 O(n²)。
    """
    first_w = max(24.0, float(width))
    rest_w = max(24.0, float(rest_width if rest_width is not None else width))

    def limit(i):
        return first_w if i == 0 else rest_w

    out, cur, cur_w = [], "", 0.0
    for tok in _CJK_INLINE.findall(text or ""):
        sep = " " if (cur and _glue_space(cur[-1], tok[:1])) else ""
        sep_w = _char_width(" ", _font_for(" ", font)) * size if sep else 0.0
        tok_w = _measure(tok, font, size)
        lim = limit(len(out))
        if cur and cur_w + sep_w + tok_w <= lim:
            cur += sep + tok
            cur_w += sep_w + tok_w
            continue
        if cur:
            out.append(cur)
            cur, cur_w = "", 0.0
        lim = limit(len(out))
        if tok_w <= lim:
            cur = tok
            cur_w = tok_w
            continue
        for ch in tok:                   # 单个词比整行还宽：逐字符断
            ch_w = _measure(ch, font, size)
            if not cur:
                cur, cur_w = ch, ch_w
            elif cur_w + ch_w <= lim:
                cur += ch
                cur_w += ch_w
            else:
                out.append(cur)
                cur, cur_w = ch, ch_w
                lim = limit(len(out))
    if cur:
        out.append(cur)
    return _fix_breaks(out or [""], font, size, first_w, rest_w)


def _ascent(font, size):
    try:
        return pdfmetrics.getAscent(font, size)
    except Exception:                    # noqa: BLE001
        return size * 0.88


class _Doc:
    """一页一页往下画的极简排版器：比 platypus 少一层抽象，字号/分页完全可控。"""

    def __init__(self, buf, title):
        self.c = rl_canvas.Canvas(buf, pagesize=(PAGE_W, PAGE_H))
        self.c.setTitle(title or "译文")
        self.c.setCreator("Galgame Monster · PDF 阅读器")
        self.page = 1
        self.y = PAGE_H - MT
        self.head_count = 0
        self.title = _plain(title)[:48]
        self._open_page(first=True)

    # -- 页面骨架 ------------------------------------------------------
    def _open_page(self, first=False):
        c = self.c
        if not first:
            c.setFillColor(HexColor("#8b949e"))
            c.setFont(HEAD_FONT, 8)
            c.drawString(MX, PAGE_H - 34, self.title)
        self.y = PAGE_H - MT

    def _footer(self):
        c = self.c
        c.setFillColor(HexColor("#8b949e"))
        c.setFont(HEAD_FONT, 8)
        c.drawRightString(PAGE_W - MX, 32, str(self.page))
        c.setStrokeColor(HexColor("#e3e7eb"))
        c.setLineWidth(0.5)
        c.line(MX, 46, PAGE_W - MX, 46)

    def new_page(self):
        self._footer()
        self.c.showPage()
        self.page += 1
        self._open_page()

    def _need(self, height):
        if self.y - height < MB:
            self.new_page()

    # -- 文本 ----------------------------------------------------------
    def block(self, text, style_name, indent=0.0, hanging=0.0, role=""):
        """画一段文本；返回实际行数。

        indent / hanging：列表的首行缩进与续行悬挂缩进（续行比首行多缩 hanging）。
        role=theorem：加左侧竖线与浅底，和阅读器里的定理分组一致。
        """
        st = STYLES[style_name]
        text = _plain(text)
        if not text:
            return 0
        first_w = CONTENT_W - indent
        rest_w = CONTENT_W - indent - hanging
        lines = _wrap(text, st["font"], st["size"], first_w, rest_w)
        self.y -= st["before"]
        for i, line in enumerate(lines):
            if self.y - st["leading"] < MB:
                self.new_page()
            x = MX + indent + (hanging if i else 0.0)
            top = self.y - st["leading"]
            if role == "theorem":
                c = self.c
                c.setFillColor(HexColor("#f4f7ff"))
                c.rect(MX - 10, top + 2, CONTENT_W + 20, st["leading"] - 2, stroke=0, fill=1)
                c.setFillColor(HexColor("#4d6bfe"))
                c.rect(MX - 10, top + 2, 2, st["leading"] - 2, stroke=0, fill=1)
            self.c.setFillColor(st["color"])
            _draw_text(self.c, x, self.y - _ascent(st["font"], st["size"]),
                       line, st["font"], st["size"])
            self.y -= st["leading"]
        self.y -= st["after"]
        return len(lines)

    def heading(self, text, level, original="", dual=False):
        text = _plain(text) or _plain(original)
        if not text:
            return
        st = STYLES["h1"] if level <= 1 else STYLES["h2"]
        self.y -= st["before"]
        lines = _wrap(text, st["font"], st["size"], CONTENT_W)
        # 预留两行正文：标题不能孤零零留在页底
        self._need(len(lines) * st["leading"] + st["after"]
                   + STYLES["body"]["leading"] * 2)
        self.head_count += 1
        key = "h%d" % self.head_count
        try:
            self.c.bookmarkPage(key)
            self.c.addOutlineEntry(text[:80], key, 0 if level <= 1 else 1, False)
        except Exception:                # noqa: BLE001  书签失败不影响正文
            pass
        for line in lines:
            self.c.setFillColor(st["color"])
            _draw_text(self.c, MX, self.y - _ascent(st["font"], st["size"]),
                       line, st["font"], st["size"])
            self.y -= st["leading"]
        self.y -= st["after"]
        if dual and original:
            self.block(original, "src", indent=0.0)

    def image(self, png, max_ratio=0.46, px_per_pt=None, min_w=96.0, keep_next=0.0):
        """插图；px_per_pt 用来把数学排版图（600dpi）换算回点尺寸。"""
        if not png:
            return False
        try:
            img = ImageReader(io.BytesIO(png))
            iw, ih = img.getSize()
        except Exception:                # noqa: BLE001
            return False
        if not iw or not ih:
            return False
        # 裁图按 3x 渲染，pixels / CROP_SCALE 就是它在原文里的真实尺寸。
        # 不再强行铺满版心：半栏宽的小公式被放大到整栏，会变成一大块空白+巨字；
        # 这里按原尺寸排，只有超大图才缩到版心内。
        scale = float(px_per_pt or reader.CROP_SCALE)
        nat_w = iw / scale
        nat_h = ih / scale
        w = min(CONTENT_W, max(nat_w, min_w))   # 极小的裁图允许轻微放大，保证可读
        h = w * nat_h / max(1.0, nat_w)
        max_h = (PAGE_H - MT - MB) * max_ratio
        if h > max_h:                          # 公式 / 高图各自有上限，避免占掉半页
            h = max_h
            w = h * nat_w / max(1.0, nat_h)
        self._need(h + 10 + keep_next)
        self.c.drawImage(img, MX + (CONTENT_W - w) / 2.0, self.y - h, w, h,
                         mask="auto", preserveAspectRatio=True)
        self.y -= h + 10
        return True

    def finish(self):
        self._footer()
        self.c.save()


def _crop(path, block):
    """图表 / 公式块按原页坐标裁图；失败返回 None，导出时跳过而不是报错。"""
    rect = block.get("rect") or []
    page = block.get("page") or 0
    if len(rect) != 4 or not page:
        return None
    try:
        return reader.crop_png(path, int(page), rect)
    except Exception:                    # noqa: BLE001
        return None


def _title_of(meta, translations):
    for b in meta.get("blocks") or []:
        if b.get("kind") == "heading" and (b.get("text") or "").strip():
            return _translation_of(b, translations) or b.get("text") or ""
    return str(meta.get("name") or "译文").rsplit(".", 1)[0]


def _iter_blocks(meta):
    return [b for b in (meta.get("blocks") or []) if isinstance(b, dict)]


_MATH_SUPPORT = None
_MATH_LOCK = threading.Lock()
_EQNUM_RE = re.compile(r"\(\s*(\d+[A-Za-z]?)\s*\)\s*$")


_SYM_MAP = [
    ("\\longrightarrow", "→"), ("\\rightarrow", "→"), ("\\to", "→"),
    ("\\Longleftrightarrow", "⟺"), ("\\Leftrightarrow", "⟺"), ("\\iff", "⟺"),
    ("\\leq", "≤"), ("\\le", "≤"), ("\\geq", "≥"), ("\\ge", "≥"),
    ("\\neq", "≠"), ("\\ne", "≠"), ("\\notin", "∉"), ("\\in", "∈"),
    ("\\subseteq", "⊆"), ("\\subset", "⊂"), ("\\cup", "∪"), ("\\cap", "∩"),
    ("⟶", "→"), ("⇔", "⟺"),
]
_REL_SYMBOLS = "≤≥≠∈∉⊆⊂∪∩→⟺"


def _symbol_text(text):
    """把 LaTeX 关系命令换成统一符号，便于和原文文字层比对。"""
    s = str(text or "")
    for cmd, sym in _SYM_MAP:
        if cmd.startswith("\\"):
            s = re.sub(re.escape(cmd) + r"(?![A-Za-z])", sym, s)
        else:
            s = s.replace(cmd, sym)
    return s


def _math_trusted(latex, src):
    """粗筛识别结果：字母基本都能在原文里找到，关系符号也不能凭空消失。

    识别模型偶尔会把相邻两行 / 栏目拼成一条公式（字母凭空多出来、≤ 变没）。
    这种结果排成数学比截图更糟，直接回退截图。
    """
    body = str(latex or "")
    body = re.sub(r"\\begin\{[^{}]*\}|\\end\{[^{}]*\}", "", body)
    body = re.sub(r"\\text\{[^{}]*\}", "", body)
    body = re.sub(r"\\[A-Za-z]+", "", body)
    letters = set(re.findall(r"[A-Za-z]", body))
    if not letters:
        return True
    known = set(re.findall(r"[A-Za-z]", str(src or "")))
    if not known:
        return False
    if len(letters - known) > int(len(letters) * 0.2):
        return False
    sym_src = _symbol_text(src)
    sym_tex = _symbol_text(latex)
    return not any(sym in sym_src and sym not in sym_tex for sym in _REL_SYMBOLS)


def _with_eqnum(latex, src):
    """公式编号在识别时容易丢；原文末尾附近的 (n) 补回排版里。"""
    s = str(src or "")
    hits = list(_EQNUM_RE.finditer(s))
    if not hits:
        # 文本层里编号可能被塞在别名 / 残留字符前面：只认靠近结尾的那个
        hits = [m for m in re.finditer(r"\(\s*(\d+[A-Za-z]?)\s*\)", s)
                if m.end() >= len(s) - 24]
    if not hits:
        return latex
    num = hits[-1].group(1)
    if re.search(r"\(\s*%s\s*\)" % re.escape(num), str(latex or "")):
        return latex
    return str(latex).rstrip() + r"\qquad (%s)" % num


def _formula_latex(path, block, src):
    """能安全用来排版的 LaTeX；不可信 / 没有缓存返回空串。"""
    latex = reader.cached_formula(path, block.get("page") or 0, block.get("rect") or [])
    if not latex or not _math_trusted(latex, src):
        return ""
    return _with_eqnum(latex, src)


def _prep_math(latex):
    """把模型给的 LaTeX 规整成 mathtext 能吃的子集。"""
    t = str(latex or "").strip()
    t = re.sub(r"^\$+|\$+$", "", t).strip()
    t = re.sub(r"\\(?:label|tag)\{[^{}]*\}", "", t)
    t = re.sub(r"\\begin\{(?:equation|align|gather|displaymath)\*?\}", "", t)
    t = re.sub(r"\\end\{(?:equation|align|gather|displaymath)\*?\}", "", t)
    t = t.replace("\\setminus", "\\backslash")
    t = t.replace("\\ ", "\\;")
    t = t.replace("\\text{", "\\mathrm{")
    return t.strip()


def _math_png(latex):
    """用 matplotlib mathtext 把 LaTeX 画成 600dpi 透明 PNG；不可用返回 None。

    这是「导出公式和阅读器一样是数学排版」的关键：阅读器用 KaTeX，
    reportlab 画不了 LaTeX，mathtext 是最省依赖的替代；不支持的结构回退裁图。
    """
    global _MATH_SUPPORT
    if _MATH_SUPPORT is False:
        return None
    t = _prep_math(latex)
    if not t:
        return None
    try:
        import matplotlib
        matplotlib.use("Agg")
        from matplotlib import mathtext
        from matplotlib.font_manager import FontProperties
    except Exception:                    # noqa: BLE001  没装 matplotlib：回退裁图
        _MATH_SUPPORT = False
        return None
    buf = io.BytesIO()
    try:
        # matplotlib 不是完全线程安全的：导出接口可能并发调用，串行渲染
        with _MATH_LOCK:
            mathtext.math_to_image("$" + t + "$", buf,
                                   prop=FontProperties(size=STYLES["body"]["size"]),
                                   dpi=600, format="png")
    except Exception:                    # noqa: BLE001  mathtext 不支持的结构：回退裁图
        return None
    _MATH_SUPPORT = True
    return buf.getvalue()


def _export_pdf(path, meta, translations, mode):
    _ensure_fonts()
    dual = mode == "dual"
    title = _plain(_title_of(meta, translations))
    buf = io.BytesIO()
    doc = _Doc(buf, title)
    missing = 0
    blocks = _iter_blocks(meta)
    for i, b in enumerate(blocks):
        kind = b.get("kind") or "text"
        src = _plain(b.get("text") or "")
        dst = _plain(restore_inline(_translation_of(b, translations), b.get("math")))
        if kind == "formula":
            # 优先用阅读器已经识别好的 LaTeX 数学排版，失败再回退裁图
            png = _math_png(_formula_latex(path, b, src))
            if png:
                doc.image(png, max_ratio=0.32, px_per_pt=600.0 / 72.0, min_w=0.0)
            else:
                png = _crop(path, b)
                if png:
                    doc.image(png, max_ratio=0.32)
                elif src:
                    doc.block(src, "note")
            continue
        if kind == "figure":
            png = _crop(path, b)
            if png:
                # 图注紧跟图片：放不下就整体挪到下一页，别让图注孤零零留在页首
                nxt = blocks[i + 1] if i + 1 < len(blocks) else None
                keep = (STYLES["ref"]["leading"] * 2
                        if nxt and nxt.get("role") == "caption" else 0.0)
                doc.image(png, max_ratio=0.46, keep_next=keep)
            elif src:
                # 没有坐标（docx/pptx）时退化成文本，至少不丢内容
                doc.block(src, "note")
            continue
        if kind == "heading":
            doc.heading(dst or src, b.get("level") or 2, original=src, dual=dual and bool(dst))
            continue
        if kind in ("ref", "table", "meta"):
            if src:
                doc.block(src, "note" if kind == "meta" else "ref")
            continue
        if not src:
            continue
        role = b.get("role") or ""
        if not dst:
            missing += 1
            doc.block(src, "body", role=role)     # 未翻译：保留原文，避免导出后出现空白
            continue
        if dual:
            doc.block(src, "note" if role == "caption" else "src")
        if kind == "list":
            # 列表按原页缩进层级 + 悬挂缩进，续行与文字对齐（阅读器同款）
            level = max(0, min(3, int(b.get("indent") or 0)))
            doc.block(dst, "body", indent=14.0 * level + 14.0, hanging=14.0, role=role)
        else:
            # 图注用小一号；定理 / 证明加左侧竖线
            doc.block(dst, "ref" if role == "caption" else "body", role=role)
    doc.finish()
    return buf.getvalue(), missing


def _md_escape_img(png):
    import base64
    return base64.b64encode(png).decode("ascii")


def _export_md(path, meta, translations):
    title = _plain(_title_of(meta, translations))
    lines = ["# " + title, ""]
    missing = 0
    for b in _iter_blocks(meta):
        kind = b.get("kind") or "text"
        src = _plain(b.get("text") or "")
        dst = _plain(restore_inline(_translation_of(b, translations), b.get("math")))
        if kind == "formula":
            # Markdown 里直接写 $$LaTeX$$，比贴图更适合二次编辑 / 喂给其他工具
            latex = _formula_latex(path, b, src)
            if latex:
                lines.extend(["$$", latex.strip(), "$$", ""])
                continue
            png = _crop(path, b)
            if png:
                lines.append('<img src="data:image/png;base64,%s" alt="公式" />'
                             % _md_escape_img(png))
                lines.append("")
            elif src:
                lines.extend(["```", src, "```", ""])
            continue
        if kind == "figure":
            png = _crop(path, b)
            if png:
                lines.append('<img src="data:image/png;base64,%s" alt="图" />'
                             % _md_escape_img(png))
                lines.append("")
            elif src:
                lines.extend(["```", src, "```", ""])
            continue
        if kind == "heading":
            level = 1 if (b.get("level") or 2) <= 1 else 2
            lines.extend(["#" * (level + 1) + " " + (dst or src), ""])
            continue
        if kind in ("ref", "table", "meta"):
            if src:
                if kind == "table":
                    lines.extend(["```", src, "```", ""])
                elif kind == "meta":
                    lines.extend(["> " + src, ""])
                else:
                    lines.extend([src, ""])
            continue
        if not src:
            continue
        if not dst:
            missing += 1
            lines.extend(["> " + src, ""])
            continue
        lines.extend([dst, ""])
        if dst != src:
            lines.extend(["> " + src, ""])
    return ("\n".join(lines).rstrip() + "\n").encode("utf-8"), missing


def _layout_blocks(meta):
    by_page = {}
    for b in _iter_blocks(meta):
        page = int(b.get("page") or 0)
        if page <= 0:
            continue
        if (b.get("kind") or "text") not in ("text", "list", "heading"):
            continue
        if not str(b.get("text") or "").strip():
            continue
        by_page.setdefault(page, []).append(b)
    return by_page


def _layout_rect(b, page):
    for p in b.get("parts") or []:
        try:
            if int(p.get("page")) == page and len(p.get("rect") or []) == 4:
                return [float(v) for v in p["rect"]]
        except (TypeError, ValueError):
            continue
    r = b.get("rect") or []
    if len(r) == 4:
        try:
            return [float(v) for v in r]
        except (TypeError, ValueError):
            return None
    return None


def _layout_fit(text, font, width, height, max_size, min_size=4.6, target=1):
    """在固定矩形里找最大可用字号；行距按「原段行数」撑满原框。

    与阅读器译页（web/transpage.js）同一套口径：字号从原段行距反推，
    行距 = 框高 / max(译文行数, 原段行数)，放不下才降字号。
    放不下返回 None（调用方决定是否退回原文）。
    """
    target = max(1, int(target or 1))
    lo, hi = float(min_size), float(max(min_size, max_size))
    # 字号不能大到把原段行数挤掉（行距至少 1.05 倍字号）
    cap = height / (target * 1.05)
    if cap >= lo:
        hi = min(hi, cap)
    best = None
    for _ in range(12):
        size = (lo + hi) / 2.0
        lines = _fix_breaks(_wrap(text, font, size, width), font, size, width, width)
        lh = height / max(len(lines), target)
        lh = max(size * 1.05, min(size * 1.9, lh))
        if lines and len(lines) * lh <= height + 0.5:
            best = (lines, size, lh)
            lo = size
        else:
            hi = size
    if best:
        return best
    # 最小字号再试一次：短行/大框的边界情况
    lines = _fix_breaks(_wrap(text, font, min_size, width), font, min_size, width, width)
    if lines:
        lh = max(min_size * 1.05, min(min_size * 1.9,
                                     height / max(len(lines), target)))
        if len(lines) * lh <= height + 0.5:
            return (lines, min_size, lh)
    return None


def _layout_obstacles(blocks):
    """同一页不可覆盖的区域（公式 / 表格 / 图片）。"""
    out = []
    for b in blocks or []:
        if (b.get("kind") or "") not in ("figure", "formula", "table"):
            continue
        r = b.get("rect") or []
        if len(r) != 4:
            continue
        try:
            out.append([float(v) for v in r])
        except (TypeError, ValueError):
            continue
    return out


def _subtract_rect(rect, obstacles):
    """矩形差集：盖原文时避开图表 / 公式，返回若干个小矩形。"""
    pieces = [list(rect)]
    for o in obstacles or []:
        nxt = []
        for p in pieces:
            if not (min(p[2], o[2]) - max(p[0], o[0]) > 0.5 and
                    min(p[3], o[3]) - max(p[1], o[1]) > 0.5):
                nxt.append(p)
                continue
            if o[1] > p[1]:
                nxt.append([p[0], p[1], p[2], min(o[1], p[3])])
            if o[3] < p[3]:
                nxt.append([p[0], max(o[3], p[1]), p[2], p[3]])
            top, bot = max(p[1], o[1]), min(p[3], o[3])
            if o[0] > p[0] and bot > top:
                nxt.append([p[0], top, min(o[0], p[2]), bot])
            if o[2] < p[2] and bot > top:
                nxt.append([max(o[2], p[0]), top, p[2], bot])
        pieces = [p for p in nxt if p[2] - p[0] > 0.5 and p[3] - p[1] > 0.5]
    return pieces


def _layout_bottom_limit(rect, rects):
    """向下扩展的边界：同页、横向有重叠、起点在当前块下方的最近一个块。"""
    limit = None
    for r in rects or []:
        if min(rect[2], r[2]) - max(rect[0], r[0]) <= 1:
            continue
        if r[1] >= rect[3] - 0.5 and (limit is None or r[1] < limit):
            limit = r[1]
    return limit


def _overlap_area(a, b):
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return w * h if w > 0 and h > 0 else 0.0


def _layout_overlay(page_w, page_h, blocks, translations, missing):
    """给一页生成透明覆盖层（白块 + 译文），返回 reportlab PDF bytes。

    排版与阅读器译页一致：按原段行数 / 行距铺满原框；放不下先向下借空隙，
    仍然放不下才退回原文（missing 计数）。
    """
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=(page_w, page_h))
    page_rects = []
    for b in blocks or []:
        r = _layout_rect(b, int(b.get("page") or 0))
        if r:
            page_rects.append(r)
    obstacles = _layout_obstacles(blocks)
    for b in blocks:
        dst = _plain(restore_inline(_translation_of(b, translations), b.get("math")))
        if not dst:
            missing[0] += 1
            continue
        rect = _layout_rect(b, int(b.get("page") or 0))
        if not rect:
            missing[0] += 1
            continue
        x0, y0, x1, y1 = rect
        w, h = max(1.0, x1 - x0), max(1.0, y1 - y0)
        # 与阅读器译页一致：边角碎片不翻、原样回显不翻、大面积压图表不翻
        if w < 12 or h < 5:
            continue
        if sum(_overlap_area(rect, o) for o in obstacles) > w * h * 0.5:
            continue
        if dst.strip() == str(b.get("text") or "").strip():
            continue
        kind = b.get("kind") or "text"
        font = HEAD_FONT if kind == "heading" else BODY_FONT
        level = 1 if kind == "heading" and (b.get("level") or 2) <= 1 else 2
        # 原段行数：schema 26 起由解析写入；旧缓存按「高度 / 字形高度」估
        try:
            target = int(b.get("lines") or 0)
        except (TypeError, ValueError):
            target = 0
        base_px = float(b.get("size") or 10.0)
        if target <= 0:
            target = max(1, int(round(h / max(1.0, base_px) / 1.18)))
        lead = h / target
        mult = 2.2 if kind == "heading" else 1.85
        base = max(lead / 1.15, base_px * mult)
        max_size = min(18.0, base * 1.16) if kind == "heading" else min(12.5, base * 1.16)
        min_size = max(4.6, base * 0.62)
        indent = 0.0
        if kind == "list":
            indent = 7.0 * (min(3, max(0, int(b.get("indent") or 0))) + 1)
        text_w = max(8.0, w - 2.0 - indent)
        # 单行块（标题 / 图注）的矩形只是字形墨迹高度，装不下一个正常行框：
        # 与阅读器译页一致，向下借一点空隙，否则所有标题都会判成「放不下」。
        fit_h = max(3.0, h - 1.0)
        draw_h = h
        if target <= 1:
            want = base * 1.22
            if fit_h < want:
                lim0 = _layout_bottom_limit(rect, page_rects)
                avail = (lim0 - y0) if lim0 is not None else want
                fit_h = max(fit_h, min(want, max(fit_h, avail)))
                draw_h = max(h, fit_h + 1.0)
        fit = _layout_fit(dst, font, text_w, fit_h,
                          max_size, min_size, target)
        if not fit:
            # 向下借空隙（到下一个块为止），按借到的高度重算目标行数
            limit = _layout_bottom_limit(rect, page_rects)
            if limit is not None and limit > y1 + 1:
                draw_h = max(draw_h, max(1.0, limit - y0))
                t2 = max(target, int(round(draw_h / max(0.1, lead))))
                fit = _layout_fit(dst, font, text_w, max(3.0, draw_h - 1.0),
                                  max_size, min_size, t2)
        if not fit:
            missing[0] += 1
            continue
        lines, size, lh = fit
        # 覆盖原文字块；图表 / 公式 / 表格区域用差集避让
        c.setFillColor(HexColor("#ffffff"))
        for p in _subtract_rect([x0, y0, x1, y1], obstacles):
            c.rect(p[0] - 0.6, page_h - p[3] - 0.6,
                   (p[2] - p[0]) + 1.2, (p[3] - p[1]) + 1.2, stroke=0, fill=1)
        c.setFillColor(HexColor("#111111"))
        center = bool(kind == "heading" and level == 1)
        for i, line in enumerate(lines):
            baseline = page_h - y0 - size * 0.95 - i * lh
            if baseline < page_h - (y0 + draw_h) + 1.0:
                break
            if center:
                lx = x0 + max(0.0, (w - _measure(line, font, size)) / 2.0)
            else:
                lx = x0 + 1.0 + indent
            _draw_text(c, lx, baseline, line, font, size)
    c.showPage()
    c.save()
    return buf.getvalue()


def _export_layout(path, meta, translations, dual=False):
    """版式还原 PDF：原页为底，按 block.rect 覆盖译文；图表 / 公式 / 表格原位保留。"""
    from pypdf import PageObject, PdfReader, PdfWriter, Transformation

    _ensure_fonts()
    if not os.path.isfile(path):
        raise ExportError("原文件不存在，无法生成版式还原 PDF")
    try:
        src = PdfReader(path)
    except Exception as e:                      # noqa: BLE001
        raise ExportError("读取原 PDF 失败：%s" % e)
    by_page = _layout_blocks(meta)
    writer = PdfWriter()
    missing = [0]
    total = min(len(src.pages), int(meta.get("pages") or len(src.pages)))
    for i in range(total):
        base = src.pages[i]
        page_w = float(base.mediabox.width)
        page_h = float(base.mediabox.height)
        raw = _layout_overlay(page_w, page_h, by_page.get(i + 1, []),
                              translations, missing)
        overlay = PdfReader(io.BytesIO(raw)).pages[0] if raw else None
        if not dual:
            merged = writer.add_page(copy.deepcopy(base))
            if overlay is not None:
                merged.merge_page(overlay)
            continue
        # 同页左右并排：左半原页，右半译页（覆盖层直接平移过去，避免二次合并）
        wide = writer.add_page(PageObject.create_blank_page(
            width=page_w * 2.0, height=page_h))
        wide.merge_page(base)
        if overlay is not None:
            wide.merge_transformed_page(overlay, Transformation().translate(page_w, 0))
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue(), missing[0]


def export(path, meta, translations, mode, lang="zh"):
    """返回 (bytes, 扩展名, mime, 缺译段数)。mode: mono / dual / md / layout-mono / layout-dual。"""
    if mode not in ("mono", "dual", "md", "layout-mono", "layout-dual"):
        raise ExportError("未知的导出格式：%s" % mode)
    if not meta or not (meta.get("blocks") or []):
        raise ExportError("这份文档还没有可导出的内容")
    if mode == "md":
        raw, missing = _export_md(path, meta, translations)
        return raw, "md", "text/markdown; charset=utf-8", missing
    if not _HAS_RL:
        raise ExportError("缺少 reportlab，无法生成 PDF：请先 pip install reportlab")
    try:
        if mode in ("layout-mono", "layout-dual"):
            raw, missing = _export_layout(path, meta, translations, dual=(mode == "layout-dual"))
        else:
            raw, missing = _export_pdf(path, meta, translations, mode)
    except ExportError:
        raise
    except Exception as e:                # noqa: BLE001
        raise ExportError("生成 PDF 失败：%s" % e)
    return raw, "pdf", "application/pdf", missing


SUFFIX = {"mono": "译文", "dual": "双语对照", "md": "译文",
          "layout-mono": "版式译文", "layout-dual": "版式双语"}


def export_name(meta, mode, ext):
    stem = os.path.splitext(str(meta.get("name") or "document"))[0] or "document"
    return "%s-%s.%s" % (stem, SUFFIX.get(mode, "译文"), ext)


def save(path, raw, name):
    """把导出文件落到 data/reader/<doc_id>/exports/，返回 (doc_id, 文件名)。"""
    doc_id = reader._doc_id(path)        # noqa: SLF001  同目录缓存，避免重复算哈希
    d = os.path.join(reader._doc_dir(doc_id), "exports")  # noqa: SLF001
    os.makedirs(d, exist_ok=True)
    clean = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", str(name or "export.bin")).strip()[:120]
    if not clean:
        clean = "export.bin"
    fp = os.path.join(d, clean)
    tmp = fp + ".%d.tmp" % os.getpid()
    with open(tmp, "wb") as f:
        f.write(raw)
    os.replace(tmp, fp)
    _purge(d)
    return doc_id, clean


def _purge(d, keep_sec=7 * 86400):
    """旧导出清掉：每次导出重新生成，留着只占磁盘。"""
    now = time.time()
    try:
        for fn in os.listdir(d):
            fp = os.path.join(d, fn)
            try:
                if os.path.isfile(fp) and now - os.path.getmtime(fp) > keep_sec:
                    os.remove(fp)
            except OSError:
                continue
    except OSError:
        pass

