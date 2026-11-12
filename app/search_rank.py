# -*- coding: utf-8 -*-
"""联网搜索的结果层：canonical 去重 + 页面级排序。

P0 不引入向量模型，先用可解释的轻量特征：
- 词法覆盖：标题 / 摘要 / 域名命中查询词（中文按二字片段）
- 域名权威先验：官方文档 / GitHub / 学术 / 政府等加权，内容农场降权
- 时效：按意图（新闻 / 文档 / 通用）做不同衰减
- 结构：摘要长度、数字、BLUF（前 100 字命中查询词）
- 来源类型：文档类优先官方文档 / 代码站，新闻类优先新闻站
- 域名多样性：同一域名最多 2 条，避免一屏全是同一个站

排序只负责「把更可能被引用的页面排前面」，不丢弃结果；
最终条数仍由调用方（search_multi）按配置截断。
"""
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from app.config import load_config

_TRACK_PARAMS = {
    "fbclid", "gclid", "yclid", "msclkid", "igshid", "mc_cid", "mc_eid",
    "ref_src", "ref_url", "spm", "scm", "from_source", "share_token",
}

# 命中即给较高权威分（官方文档 / 规范 / 代码站）
_AUTH_HIGH = (
    "docs.python.org", "developer.mozilla.org", "nodejs.org", "react.dev",
    "vuejs.org", "rust-lang.org", "go.dev", "learn.microsoft.com",
    "docs.github.com", "kubernetes.io", "postgresql.org", "sqlite.org",
    "w3.org", "ietf.org", "rfc-editor.org", "docs.oracle.com", "dev.mysql.com",
    "help.aliyun.com", "docs.pingcap.com", "support.huaweicloud.com",
    "docs.aws.amazon.com",
)
_AUTH_MED = (
    "github.com", "stackoverflow.com", "arxiv.org", "wikipedia.org",
    "python.org", "mozilla.org", "openai.com", "anthropic.com", "deepseek.com",
    "readthedocs.io", "npmjs.com", "pypi.org", "crates.io",
    "cloud.tencent.com", "developer.aliyun.com", "segmentfault.com",
)
_AUTH_LOW = (
    "baijiahao.baidu.com", "hao123.com", "zhidao.baidu.com", "51cto.com",
)

_NEWS_HOSTS = (
    "reuters.com", "apnews.com", "bbc.com", "bbc.co.uk", "nytimes.com",
    "theguardian.com", "wsj.com", "bloomberg.com", "cnbc.com", "finance.sina.com.cn",
    "caixin.com", "thepaper.cn", "36kr.com", "jiemian.com", "ithome.com",
)

_CJK_RE = re.compile(r"[\u4e00-\u9fff]{2,}")
_LATIN_RE = re.compile(r"[a-z0-9][a-z0-9_+.#-]{1,}")


def _host(url):
    try:
        return (urlparse(str(url or "")).hostname or "").lower()
    except ValueError:
        return ""


def canonical_url(url):
    """去掉 tracking 参数 / 片段 / www / 末尾斜杠 / AMP 后缀，做去重键。"""
    s = str(url or "").strip()
    if not s:
        return ""
    try:
        p = urlparse(s)
    except ValueError:
        return s
    if not p.netloc:
        return s
    host = p.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if host.endswith(":80"):
        host = host[:-3]
    if host.endswith(":443"):
        host = host[:-4]
    path = re.sub(r"/+$", "", p.path) or "/"
    path = re.sub(r"/amp/?$", "", path, flags=re.I) or "/"
    q = []
    for k, v in parse_qsl(p.query, keep_blank_values=False):
        lk = k.lower()
        if lk.startswith("utm_") or lk in _TRACK_PARAMS:
            continue
        q.append((k, v))
    q.sort()
    return urlunparse((p.scheme or "https", host, path, "", urlencode(q), ""))


def _dedupe_key(url, title=""):
    c = canonical_url(url)
    if c:
        return re.sub(r"^https?://", "", c, flags=re.I)
    t = re.sub(r"\s+", " ", str(title or "").strip().lower())
    return ("title:" + t[:120]) if t else ""


def query_terms(query):
    """查询词集合：拉丁词 + 中文二字片段（不依赖分词器）。"""
    s = str(query or "").lower()
    terms = set(_LATIN_RE.findall(s))
    for seg in _CJK_RE.findall(s):
        for i in range(len(seg) - 1):
            terms.add(seg[i:i + 2])
    return terms


def _authority(url, intent=""):
    h = _host(url)
    if not h:
        return 0.0
    if any(h == x or h.endswith("." + x) for x in _AUTH_HIGH):
        return 1.0
    if h.endswith(".gov") or h.endswith(".edu"):
        return 0.85
    if any(h == x or h.endswith("." + x) for x in _AUTH_MED):
        return 0.75
    if any(h == x or h.startswith(x) for x in _AUTH_LOW):
        return -0.45
    if intent == "docs" and (h.endswith(".io") or h.endswith(".dev")):
        return 0.2
    if h.endswith(".org"):
        return 0.15
    return 0.0


def _coverage(item, terms):
    """标题命中权重最高，摘要次之，域名再次；返回 0–1。"""
    if not terms:
        return 0.5
    title = str(item.get("title") or "").lower()
    snippet = str(item.get("snippet") or "").lower()
    host = _host(item.get("url"))
    hit = 0.0
    for t in terms:
        if t in title:
            hit += 1.0
        elif t in snippet:
            hit += 0.6
        elif t and t in host:
            hit += 0.4
    denom = max(1, min(len(terms), 12))
    return min(1.0, hit / denom)


def _parse_date(value):
    s = str(value or "").strip()
    if not s:
        return None
    now = datetime.now(timezone.utc)
    if s.isdigit():                       # You.com 的 page_age 可能是「天数」
        try:
            return now - timedelta(days=int(s))
        except ValueError:
            return None
    m = re.search(r"(\d+)\s*(hour|hours|day|days|week|weeks|month|months|year|years)",
                  s, re.I)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        days = {"hour": 1 / 24, "hours": 1 / 24, "day": 1, "days": 1,
                "week": 7, "weeks": 7, "month": 30, "months": 30,
                "year": 365, "years": 365}[unit]
        return now - timedelta(days=n * days)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _freshness(item, intent, freshness):
    dt = _parse_date(item.get("published_at") or item.get("last_updated"))
    if dt is None:
        return 0.45 if intent in ("fresh", "news") else 0.5
    try:
        age = max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400)
    except (TypeError, ValueError):
        return 0.5
    if intent in ("fresh", "news") or freshness:
        window = {"day": 1, "week": 7, "month": 30, "year": 365}.get(freshness or "week", 7)
        if age <= window:
            return 1.0
        if age <= window * 4:
            return 0.6
        if age <= window * 12:
            return 0.3
        return 0.1
    if age <= 30:
        return 0.9
    if age <= 365:
        return 0.7
    return 0.4


def _structure(item, terms, intent):
    title = str(item.get("title") or "")
    snippet = str(item.get("snippet") or "")
    score = 0.0
    if re.search(r"(官方|文档|documentation|docs|api|reference|guide|教程|手册)", title, re.I):
        score += 0.4
    if re.search(r"\d", snippet):
        score += 0.2
    if len(snippet) >= 80:
        score += 0.2
    if terms and any(t in snippet[:120].lower() for t in terms):
        score += 0.2              # BLUF：答案在前 100 字左右
    return min(1.0, score)


def _source_type(item, intent):
    h = _host(item.get("url"))
    if not h:
        return 0.0
    if intent == "docs":
        if any(h == x or h.endswith("." + x) for x in _AUTH_HIGH + _AUTH_MED):
            return 1.0
        return 0.3 if h.endswith((".io", ".dev", ".org")) else 0.0
    if intent in ("fresh", "news"):
        return 0.8 if any(h == x or h.endswith("." + x) for x in _NEWS_HOSTS) else 0.0
    if intent == "academic":
        return 1.0 if h.endswith((".edu", ".org")) or h in ("arxiv.org",) else 0.2
    return 0.0


def dedupe(items):
    """按 canonical URL（没有 URL 时按标题）去重，保留先出现的一条。"""
    out, seen = [], set()
    for it in (items or []):
        if not isinstance(it, dict):
            continue
        url = str(it.get("url") or "").strip()
        key = _dedupe_key(url, it.get("title"))
        if not key or key in seen:
            continue
        seen.add(key)
        if url:
            it = dict(it)
            it["canonical_url"] = canonical_url(url)
        out.append(it)
    return out


def _semantic_scores(queries, rows):
    """本地向量相似度；模型未就绪 / 依赖缺失返回 None，排序回退纯词法。"""
    try:
        if not bool(load_config().get("search_semantic_rank", True)):
            return None
        from app import codeindex
        if not codeindex.model_ready():
            return None
        qtext = " ".join(str(q or "") for q in (queries or []))[:600]
        texts = [(str(it.get("title") or "") + " " + str(it.get("snippet") or ""))[:800]
                 for it in rows]
        if not qtext.strip() or not texts:
            return None
        vecs = codeindex.embed_texts([qtext] + texts)
        if vecs is None or len(vecs) != len(texts) + 1:
            return None
        import numpy as np
        qv = np.asarray(vecs[0], dtype="float32")
        mat = np.asarray(vecs[1:], dtype="float32")
        qn = float(np.linalg.norm(qv))
        if qn <= 0:
            return None
        norms = np.linalg.norm(mat, axis=1)
        sims = (mat @ qv) / np.maximum(norms * qn, 1e-6)
        return [float(x) for x in sims]
    except Exception:                        # noqa: BLE001  向量不可用不能影响搜索
        return None


def rank_hits(items, queries, intent="general", freshness="", max_items=None, semantic=None):
    """按相关性 / 权威 / 时效 / 结构 / 多样性排序；不丢结果。

    本地向量模型可用时混入语义相似度（P2），否则仍走 P0 的纯词法权重。
    semantic=False 可强制纯词法（离线评测 / 可复现实验用）。
    """
    terms = set()
    for q in (queries or []):
        terms |= query_terms(q)
    rows = dedupe(items)
    sem = None if semantic is False else _semantic_scores(queries, rows)
    scored = []
    for idx, it in enumerate(rows):
        cov = _coverage(it, terms)
        auth = _authority(it.get("url"), intent)
        fresh = _freshness(it, intent, freshness)
        struct = _structure(it, terms, intent)
        stype = _source_type(it, intent)
        try:
            qrank = max(0, int(it.get("_qrank") or 0))
        except (TypeError, ValueError):
            qrank = 0
        prank = 1.0 / (1.0 + qrank)
        if sem is not None:
            sim = max(0.0, float(sem[idx] if idx < len(sem) else 0.0))
            score = (0.28 * cov + 0.22 * sim + 0.20 * max(0.0, auth)
                     + 0.12 * fresh + 0.08 * struct + 0.06 * stype + 0.04 * prank)
            it["rank_semantic"] = round(sim, 4)
        else:
            score = (0.35 * cov + 0.20 * max(0.0, auth) + 0.15 * fresh
                     + 0.10 * struct + 0.10 * stype + 0.10 * prank)
        if auth < 0:
            score += auth * 0.3
        it["rank_score"] = round(score, 4)
        reasons = []
        if cov >= 0.5:
            reasons.append("query")
        if sem is not None and float(it.get("rank_semantic") or 0) >= 0.55:
            reasons.append("semantic")
        if auth >= 0.75:
            reasons.append("authority")
        if auth < 0:
            reasons.append("low-authority")
        if fresh >= 0.9 and intent in ("fresh", "news"):
            reasons.append("fresh")
        if struct >= 0.6:
            reasons.append("structure")
        it["rank_reason"] = "+".join(reasons)
        scored.append((score, idx, it))
    scored.sort(key=lambda x: (-x[0], x[1]))
    picked, host_n = [], {}
    for _score, _idx, it in scored:
        h = _host(it.get("url"))
        if h and host_n.get(h, 0) >= 2:
            continue
        host_n[h] = host_n.get(h, 0) + 1
        picked.append(it)
    # 域名上限导致的空位用剩余结果补齐，保证条数不打折
    if max_items and len(picked) < max_items:
        chosen = {id(x) for x in picked}
        for _score, _idx, it in scored:
            if id(it) in chosen:
                continue
            picked.append(it)
            chosen.add(id(it))
            if len(picked) >= max_items:
                break
    return picked[:max_items] if max_items else picked


def provider_quality(items, query):
    """给「引擎链换源」用的廉价质量分：0 = 明显跑题，1 = 高度相关。

    查询词太少（如单字 / 纯符号）时无法判断，直接给 1，避免误换源。
    """
    rows = [it for it in (items or []) if isinstance(it, dict)]
    if not rows:
        return 0.0
    terms = query_terms(query)
    if not terms:
        return 1.0
    cover = max(_coverage(it, terms) for it in rows[:10])
    auth = max(max(0.0, _authority(it.get("url"))) for it in rows[:10])
    return min(1.0, cover * 0.8 + auth * 0.2)
