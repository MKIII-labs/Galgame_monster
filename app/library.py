# -*- coding: utf-8 -*-
"""文献元数据提取（轻量，不联网）。

目标：打开 PDF 时先把「标题 / 作者 / 年份 / DOI / 摘要」填好，用户可再编辑。
信息来源按可靠性排序：PDF 元数据 → 首页文本启发式 → 文件名兜底。
刻意不调用模型：打开文档要快，识别不准时用户改两个字段的成本更低。
"""
import os
import re
import time

from app import anno

_DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+\b")
_YEAR_RE = re.compile(r"\b((?:19|20)\d{2})\b")
_ARXIV_RE = re.compile(r"arXiv:\s*([\w.\-]+)(?:v\d+)?", re.I)
_ABSTRACT_RE = re.compile(r"^\s*(abstract|摘要)\b", re.I)


def _first_page_text(abs_path):
    try:
        from pypdf import PdfReader
        reader = PdfReader(abs_path)
        md = reader.metadata or {}
        text = ""
        if reader.pages:
            text = reader.pages[0].extract_text() or ""
        return text, md
    except Exception:                    # noqa: BLE001
        return "", {}


def _clean_lines(text):
    out = []
    for raw in str(text or "").splitlines():
        line = " ".join(raw.split())
        if not line:
            continue
        if re.match(r"^arxiv:\S+", line, re.I):
            continue
        if re.fullmatch(r"\d{1,4}", line):          # 页码
            continue
        out.append(line)
    return out


def extract_meta(abs_path):
    """返回 {title, authors, year, venue, doi, abstract}（空串表示没识别出来）。"""
    name = os.path.splitext(os.path.basename(abs_path))[0]
    text, md = _first_page_text(abs_path)
    lines = _clean_lines(text)
    meta = {"title": "", "authors": [], "year": 0, "venue": "", "doi": "", "abstract": ""}

    title = str(md.get("/Title") or "").strip() if hasattr(md, "get") else ""
    author = str(md.get("/Author") or "").strip() if hasattr(md, "get") else ""
    if title and title.lower() not in ("untitled", "microsoft word"):
        meta["title"] = title
    if author:
        meta["authors"] = [a.strip() for a in re.split(r"[;,]", author) if a.strip()]

    # 标题：取摘要之前最靠前的 1–3 行（跳过 arXiv 编号页眉）
    if not meta["title"] and lines:
        picked = []
        for line in lines[:6]:
            if _ABSTRACT_RE.match(line):
                break
            if len(line) < 4:
                continue
            picked.append(line)
            if len(picked) >= 3 or line.endswith("."):
                break
        if picked:
            meta["title"] = " ".join(picked)[:300]

    # 作者：标题之后、摘要之前的那几行；匹配不到再扫前 8 行里像人名的行
    if not meta["authors"]:
        try:
            start = lines.index(meta["title"]) + 1 if meta["title"] in lines else 1
        except ValueError:
            start = 1
        cand = list(lines[start:start + 4]) + list(lines[:8])
        for line in cand:
            if _ABSTRACT_RE.match(line) or line == meta["title"]:
                continue
            if len(line) > 160:
                continue
            if "@" in line or (
                    re.search(r"[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+", line)
                    and not line.endswith(".")):
                meta["authors"] = [x.strip() for x in re.split(r",| and |;", line)
                                   if x.strip()][:12]
                break

    joined = "\n".join(lines[:80])
    m = _DOI_RE.search(joined)
    if m:
        meta["doi"] = m.group(0).rstrip(".,;)")
    m = _ARXIV_RE.search(joined)
    if m:
        meta["venue"] = "arXiv:" + m.group(1)
    own_year = time.localtime().tm_year
    years = [int(y) for y in _YEAR_RE.findall(joined) if 1970 <= int(y) <= own_year]
    if years:
        meta["year"] = min(years)
    m = re.search(r"(?im)^abstract[\s:—-]*(.+?)(?:\n\s*\n|$)", text or "")
    if m:
        meta["abstract"] = " ".join(m.group(1).split())[:1500]
    if not meta["title"]:
        meta["title"] = name[:300]
    return meta


def ensure_document(abs_path, doc_key):
    """已有条目直接返回；没有则提取一次并落库。"""
    old = anno.get_document(doc_key)
    if old:
        return old
    meta = extract_meta(abs_path)
    return anno.save_document(doc_key, meta)
