# -*- coding: utf-8 -*-
"""联网搜索的 Query Planner（P0：确定性规则，不额外调模型）。

输入：用户原话 + 主模型给的 1–3 个检索式。
输出：intent / freshness / 2–5 个去重后的检索式。

规则只在「明显不够或明显需要时间锚点」时补查询，不硬造同义词：
- 模型只给 1 条时，补一条去口语词的关键词式；
- 时效类查询补当前年份；
- 对比类只给一条时，补 A / B 两个实体；
- 保留模型给的原始检索式，不替换。
"""
import re
from datetime import datetime

MAX_QUERIES = 5

_STOP_ZH = re.compile(
    r"(请问|帮我|麻烦|一下|怎么样|怎么|如何|为什么|是什么|有哪些|能不能|"
    r"可不可以|可以|需要|想要|我想|的|了|吗|呢|吧|啊|呀|和|与|及|或)")
_SPLIT_RE = re.compile(r"[\s,，。;；、/|()（）\[\]【】]+")
_FRESH_RE = re.compile(
    r"(最新|今天|今日|昨天|本周|这周|最近|目前|现在|实时|新闻|股价|汇率|"
    r"价格|天气|比分|赛况|版本|发布|更新|release|changelog|latest|today|"
    r"news|price|weather)", re.I)
_DOCS_RE = re.compile(
    r"(api|文档|doc|docs|documentation|报错|error|异常|安装|install|配置|"
    r"config|github|版本|version|sdk|接口)", re.I)
_CMP_RE = re.compile(r"(对比|区别|差异|哪个好|vs\.?|versus|compare)", re.I)
_HOWTO_RE = re.compile(r"(怎么|如何|教程|步骤|how\s*to|tutorial|guide|指南)", re.I)
_ACADEMIC_RE = re.compile(r"(论文|paper|arxiv|学术|研究|实验|journal|conference)", re.I)
_CMP_SPLIT_RE = re.compile(r"\s*(?:vs\.?|versus|对比|和|与)\s*", re.I)
_YEAR_RE = re.compile(r"(19|20)\d{2}")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_LATIN_RE = re.compile(r"[a-z0-9][a-z0-9_+.#-]{1,}")


def _clean(q):
    s = str(q or "").strip().strip("\"'“”‘’")
    s = re.sub(r"\s+", " ", s)
    return s.strip(" ，。；;、/|")[:120]


def _compact(q):
    """去掉口语 / 疑问词，留下关键词式。"""
    s = _STOP_ZH.sub(" ", str(q or ""))
    s = _SPLIT_RE.sub(" ", s).strip()
    return s or str(q or "").strip()


def _tokens(q):
    """去重用的轻量 token：拉丁词 + 中文二字片段。"""
    s = str(q or "").lower()
    out = set(_LATIN_RE.findall(s))
    for seg in re.findall(r"[\u4e00-\u9fff]{2,}", s):
        for i in range(len(seg) - 1):
            out.add(seg[i:i + 2])
    return out


def _similar(a, b):
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False
    return len(ta & tb) / max(1, len(ta | tb)) >= 0.8


def detect_intent(text, queries):
    s = " ".join([str(text or "")] + [str(q or "") for q in (queries or [])])
    if _CMP_RE.search(s):
        return "compare"
    if _FRESH_RE.search(s):
        return "fresh"
    if _ACADEMIC_RE.search(s):
        return "academic"
    if _DOCS_RE.search(s):
        return "docs"
    if _HOWTO_RE.search(s):
        return "howto"
    return "general"


def freshness_of(intent, text):
    if intent not in ("fresh", "news"):
        return ""
    s = str(text or "")
    if re.search(r"(今天|今日|实时|刚刚|now|breaking)", s, re.I):
        return "day"
    if re.search(r"(昨天|本周|这周|最近|最新|latest|week)", s, re.I):
        return "week"
    if re.search(r"(本月|这个月|month)", s, re.I):
        return "month"
    return "year"


def _language(text):
    s = str(text or "")
    if _CJK_RE.search(s):
        return "zh"
    if _LATIN_RE.search(s.lower()):
        return "en"
    return ""


def plan(user_text, model_queries, cfg=None, now=None):
    """整理检索式；cfg 预留给后续接「检索偏好」，P0 不读取。"""
    raw = model_queries if isinstance(model_queries, (list, tuple)) else [model_queries]
    qs = []
    for q in raw:
        q = _clean(q)
        if not q:
            continue
        if any(_similar(q, x) for x in qs):
            continue
        qs.append(q)
        if len(qs) >= MAX_QUERIES:
            break

    text = str(user_text or "")
    intent = detect_intent(text, qs)
    fresh = freshness_of(intent, text + " " + " ".join(qs))

    # 只有一条：补一条关键词式（不动原检索式）
    if 0 < len(qs) < 2:
        compact = _compact(qs[0])
        if compact and compact != qs[0] and len(compact) >= 2 and not _similar(compact, qs[0]):
            qs.append(compact)

    # 时效类：补当前年份，避免模型给「最新价格」却不带时间锚点。
    # 最多补前两条，控制检索次数与额度消耗。
    if fresh:
        year = str((now or datetime.now()).year)
        for q in list(qs)[:2]:
            if len(qs) >= MAX_QUERIES:
                break
            if _YEAR_RE.search(q):
                continue
            cand = (q + " " + year).strip()
            if cand not in qs:
                qs.append(cand)

    # 对比类只有一条：补 A / B 两个实体
    if intent == "compare" and len(qs) == 1:
        parts = [p.strip() for p in _CMP_SPLIT_RE.split(qs[0]) if p.strip()]
        if 2 <= len(parts) <= 3:
            for p in parts[:3]:
                if len(qs) >= MAX_QUERIES:
                    break
                if len(p) >= 2 and p not in qs:
                    qs.append(p)

    if not qs and text.strip():
        qs = [_clean(text)[:80]]
    return {
        "intent": intent,
        "freshness": fresh,
        "language": _language(text or " ".join(qs)),
        "queries": qs[:MAX_QUERIES],
    }


def fallback_query(plan, user_text=""):
    """首轮结果很弱时的补搜式：只加一个定向查询，返回空串表示不补。"""
    plan = plan if isinstance(plan, dict) else {}
    qs = plan.get("queries") or []
    intent = plan.get("intent") or "general"
    base = _clean(qs[0]) if qs else _clean(user_text)[:80]
    if not base:
        return ""
    low = base.lower()
    if intent == "docs" and not re.search(r"(文档|docs|documentation|api|github)", low):
        cand = base + " 官方文档"
    elif intent in ("fresh", "news") and not re.search(r"(最新|latest|20\d\d)", low):
        cand = base + " 最新"
    else:
        cand = _compact(base)
    cand = _clean(cand)
    if not cand or cand == base or cand in qs:
        return ""
    return cand
