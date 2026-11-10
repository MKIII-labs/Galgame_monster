# -*- coding: utf-8 -*-
"""把一段「界面里已经渲染好的 HTML」组装成**自包含**的分享页。

自包含的含义：断网、换一台没装本应用的电脑，双击打开也能看到同样的公式、
代码块、图片与版式。为此这里做三件事：

1. KaTeX 的 CSS 内联，并把 `url(fonts/*.woff2)` 换成 base64（只留 woff2）；
2. 给长回答自动生成目录（h2/h3 ≥ 3 时）；
3. 去掉脚本与 `javascript:` 链接，避免分享页变成可执行文件。
"""
import base64
import html as html_lib
import os
import re

from app.store import ROOT

KATEX_DIR = os.path.join(ROOT, "web", "vendor", "katex")
KATEX_CSS_PATH = os.path.join(KATEX_DIR, "katex.min.css")
KATEX_FONTS = os.path.join(KATEX_DIR, "fonts")

_FACE_RE = re.compile(r"@font-face\s*\{[^}]*\}")
_WOFF2_RE = re.compile(r"url\(\s*(?:\.\./)?fonts/([A-Za-z0-9_\-]+\.woff2)\s*\)")
_FAMILY_RE = re.compile(r"font-family:\s*([^;}]+)")
_WEIGHT_RE = re.compile(r"font-weight:\s*([^;}]+)")
_STYLE_RE = re.compile(r"font-style:\s*([^;}]+)")
_SCRIPT_RE = re.compile(r"<script\b[^>]*>[\s\S]*?</script\s*>", re.I)
_SCRIPT_OPEN_RE = re.compile(r"<script\b[^>]*/?>", re.I)
_JS_HREF_RE = re.compile(r"""(href|src)\s*=\s*(["'])\s*javascript:[^"']*\2""", re.I)
_ON_ATTR_RE = re.compile(r"""\s+on[a-z]+\s*=\s*(["'])[\s\S]*?\1""", re.I)
_TOC_RE = re.compile(r"<h([23])(\s[^>]*)?>([\s\S]*?)</h\1>", re.I)

BASE_CSS = """
:root{color-scheme:light dark;--fg:#1f2328;--dim:#6a737d;--bg:#fff;--line:#e6e8eb;
--code:#f6f8fa;--accent:#3b82f6}
@media (prefers-color-scheme:dark){:root{--fg:#e6e6e6;--dim:#9aa0a6;--bg:#1b1c1f;
--line:#33363b;--code:#26282c}}
*{box-sizing:border-box}
body{margin:0;padding:32px 16px;background:var(--bg);color:var(--fg);
font:15px/1.75 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.sh{max-width:760px;margin:0 auto}
.sh-brand{font-size:12px;letter-spacing:.14em;text-transform:uppercase;color:var(--dim);margin-bottom:18px}
.sh-q{position:relative;padding:14px 16px 14px 44px;border-radius:12px;background:var(--code);
white-space:pre-wrap;word-break:break-word;margin-bottom:20px}
.sh-q::before{content:"Q";position:absolute;left:16px;top:13px;font-weight:700;color:var(--accent)}
.sh-a h1,.sh-a h2,.sh-a h3{line-height:1.35;margin:22px 0 10px}
.sh-a h1{font-size:22px}.sh-a h2{font-size:19px}.sh-a h3{font-size:17px}
.sh-a p,.sh-a ul,.sh-a ol,.sh-a blockquote{margin:10px 0}
.sh-a ul,.sh-a ol{padding-left:22px}
.sh-a a{color:var(--accent)}
.sh-a img{max-width:100%;height:auto;border-radius:8px}
.sh-a pre{background:var(--code);border:1px solid var(--line);border-radius:8px;
padding:12px 14px;overflow:auto;font-size:13px;line-height:1.6}
.sh-a code{font-family:Consolas,Menlo,monospace;font-size:13px}
.sh-a :not(pre)>code{background:var(--code);padding:1px 5px;border-radius:4px}
.sh-a blockquote{border-left:3px solid var(--line);padding-left:12px;color:var(--dim)}
.sh-a table{border-collapse:collapse;width:100%;margin:12px 0;font-size:13.5px}
.sh-a th,.sh-a td{border:1px solid var(--line);padding:6px 10px;text-align:left}
.sh-a hr{border:0;border-top:1px solid var(--line);margin:20px 0}
.sh-toc{margin:4px 0 16px;border:1px solid var(--line);border-radius:8px;background:var(--code);
font-size:12.5px}
.sh-toc>summary{padding:6px 10px;color:var(--dim);cursor:pointer;list-style:none}
.sh-toc>summary::-webkit-details-marker{display:none}
.sh-toc[open]>summary{border-bottom:1px solid var(--line);color:var(--fg)}
.sh-toc-list{display:flex;flex-direction:column;gap:3px;padding:6px 10px 8px}
.sh-toc-item{color:var(--dim);text-decoration:none}
.sh-toc-item.lvl3{padding-left:14px;font-size:12px}
.sh-foot{margin-top:32px;padding-top:14px;border-top:1px solid var(--line);
font-size:12px;color:var(--dim)}
"""


def katex_css():
    """内联 KaTeX 样式；字体只保留 woff2 并转成 data URL。失败时返回空串。"""
    try:
        css = open(KATEX_CSS_PATH, encoding="utf-8").read()
    except OSError:
        return ""

    def repl(m):
        block = m.group(0)
        fam = _FAMILY_RE.search(block)
        src = _WOFF2_RE.search(block)
        if not fam or not src:
            return ""
        path = os.path.join(KATEX_FONTS, src.group(1))
        try:
            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
        except OSError:
            return ""
        weight = _WEIGHT_RE.search(block)
        style = _STYLE_RE.search(block)
        return ("@font-face{font-family:%s;font-style:%s;font-weight:%s;"
                "src:url(data:font/woff2;base64,%s) format('woff2');font-display:block}"
                % (fam.group(1).strip(), (style.group(1).strip() if style else "normal"),
                   (weight.group(1).strip() if weight else "normal"), b64))

    return _FACE_RE.sub(repl, css)


def sanitize(html):
    """分享页不允许有任何可执行内容。"""
    s = _SCRIPT_RE.sub("", str(html or ""))
    s = _SCRIPT_OPEN_RE.sub("", s)
    s = _JS_HREF_RE.sub(r'\1="#"', s)
    s = _ON_ATTR_RE.sub("", s)
    return s


def with_toc(answer_html):
    """h2/h3 ≥ 3 时在正文最上方插一个折叠目录，返回 (html, 节数)。"""
    count = [0]

    def repl(m):
        count[0] += 1
        level, attrs, inner = m.group(1), m.group(2) or "", m.group(3)
        aid = None
        am = re.search(r'id\s*=\s*"([^"]+)"', attrs)
        if am:
            aid = am.group(1)
        else:
            aid = "sh-h%d" % count[0]
            attrs = attrs + ' id="%s"' % aid
        return '<h%s%s>%s</h%s>' % (level, attrs, inner, level)

    body = _TOC_RE.sub(repl, str(answer_html or ""))
    if count[0] < 3:
        return body, count[0]
    items = []
    for i, m in enumerate(_TOC_RE.finditer(body), 1):
        level = m.group(1)
        am = re.search(r'id\s*=\s*"([^"]+)"', m.group(2) or "")
        aid = am.group(1) if am else ("sh-h%d" % i)
        text = re.sub(r"<[^>]+>", "", m.group(3)).strip()
        cls = " lvl3" if level == "3" else ""
        items.append('<a class="sh-toc-item%s" href="#%s">%s</a>'
                     % (cls, aid, html_lib.escape(text[:120])))
    toc = ('<details class="sh-toc"><summary>目录 · %d 节</summary>'
           '<div class="sh-toc-list">%s</div></details>' % (count[0], "".join(items)))
    return toc + body, count[0]


def build_html(title, question, answer_html, footer=None, inline_katex=True):
    """返回完整的自包含 HTML 字符串。"""
    title = str(title or "分享")
    body, sections = with_toc(sanitize(answer_html))
    css = BASE_CSS + ("\n" + katex_css() if inline_katex else "")
    q = ""
    if str(question or "").strip():
        q = '<div class="sh-q">%s</div>' % html_lib.escape(str(question))
    foot = footer or ""
    return ("<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>%s</title><style>%s</style></head><body><main class=\"sh\">"
            "<div class=\"sh-brand\">Galgame Monster</div>%s"
            "<div class=\"sh-a\">%s</div><div class=\"sh-foot\">%s</div>"
            "</main></body></html>" % (html_lib.escape(title), css, q, body, foot)), sections
