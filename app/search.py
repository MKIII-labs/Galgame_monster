# -*- coding: utf-8 -*-
"""联网搜索：服务商配置、查询改写、多查询并行检索与正文抓取"""
import json
import re
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser

import requests

from app.config import get_api_conf, load_config, normalize_content

# 联网搜索服务商（可在「设置 → 联网搜索」里改地址/密钥）
SEARCH_PROVIDERS = {
    "bocha": {"name": "博查 Bocha", "url": "https://api.bocha.cn/v1/web-search",
              "hint": "申请地址：open.bochaai.com（国内直连，中文结果较好）"},
    "tavily": {"name": "Tavily", "url": "https://api.tavily.com/search",
               "hint": "申请地址：app.tavily.com（面向 AI，每月 1000 次免费额度）"},
}

MAX_QUERIES = 3        # 单次最多改写几个检索式
MAX_MERGED = 12        # 合并去重后的结果上限
FETCH_CHARS = 2500     # 每条正文最多截取多少字
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

REWRITE_PROMPT = (
    "你是搜索查询改写器。把用户的问题改写成 2-3 个适合搜索引擎的检索式。\n"
    "要求：\n"
    "1. 只给关键词式检索式，不要整句问句，不要引号。\n"
    "2. 结合最近对话消解指代：「它 / 这个 / 那个」要换成具体对象。\n"
    "3. 覆盖不同角度：核心概念、同义或英文说法、必要限定词（时间/地点/版本）。\n"
    "4. 只输出 JSON，不要解释、不要代码块围栏，格式：" + '{"queries": ["检索式1", "检索式2"]}'
)


def get_search_conf():
    cfg = load_config()
    provider = (cfg.get("search_provider") or "bocha").strip() or "bocha"
    p = SEARCH_PROVIDERS.get(provider) or SEARCH_PROVIDERS["bocha"]
    url = (cfg.get("search_url") or "").strip() or p["url"]
    key = (cfg.get("search_api_key") or "").strip()
    try:
        count = int(cfg.get("search_count") or 5)
    except (TypeError, ValueError):
        count = 5
    return provider, url, key, max(1, min(count, 20))

def search_web(query, provider, url, key, count):
    """调用搜索服务，返回 [{title, url, site, snippet}]；失败时抛出异常"""
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if provider == "tavily":
        payload = {"query": query, "search_depth": "basic",
                   "max_results": count, "include_answer": False}
    else:  # bocha
        payload = {"query": query, "freshness": "noLimit",
                   "summary": True, "count": count}

    resp = requests.post(url, headers=headers,
                         data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), timeout=25)
    resp.raise_for_status()
    d = resp.json()

    items = []
    if provider == "tavily":
        for it in (d.get("results") or [])[:count]:
            items.append({
                "title": it.get("title") or "",
                "url": it.get("url") or "",
                "site": "",
                "snippet": (it.get("content") or "").strip()[:600],
            })
    else:
        code = d.get("code")
        if code is not None and code != 200:
            raise RuntimeError(d.get("msg") or f"搜索服务返回 code={code}")
        value = (((d.get("data") or {}).get("webPages") or {}).get("value")) or []
        for it in value[:count]:
            items.append({
                "title": it.get("name") or "",
                "url": it.get("url") or "",
                "site": it.get("siteName") or "",
                "snippet": (it.get("summary") or it.get("snippet") or "").strip()[:600],
            })
    return items


# ============================================================
# 查询改写（用聊天模型把口语化问题转成多个检索式）
# ============================================================

def _loads_json(raw):
    """尽量把模型输出的 JSON 解出来（容忍围栏与杂质）"""
    text = (raw or "").strip()
    if not text:
        return None
    text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    i, j = text.find("{"), text.rfind("}")
    if i >= 0 and j > i:
        frag = text[i:j + 1]
        for cand in (frag, re.sub(r",\s*([}\]])", r"\1", frag)):
            try:
                return json.loads(cand)
            except Exception:
                continue
    return None

def rewrite_queries(question, history=None):
    """把用户问题改写成多个检索式；没配聊天密钥或失败时回落 [question]"""
    question = (question or "").strip()
    if not question:
        return []
    key, url, model = get_api_conf("chat")
    if not key:
        return [question]

    msgs = [{"role": "system", "content": REWRITE_PROMPT}]
    for h in (history or [])[-6:]:
        role = h.get("role")
        text = str(h.get("content") or "").strip()
        if role in ("user", "assistant") and text:
            msgs.append({"role": role, "content": text[:400]})
    msgs.append({"role": "user", "content": "用户问题：" + question})

    req = {"model": model, "messages": msgs, "stream": False, "temperature": 0.2}
    try:
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json; charset=utf-8"},
            data=json.dumps(req, ensure_ascii=False).encode("utf-8"), timeout=30)
        resp.raise_for_status()
        d = resp.json()
        choices = d.get("choices") or [{}]
        text = normalize_content((choices[0].get("message") or {}).get("content"))
        data = _loads_json(text)
        raw = data.get("queries") if isinstance(data, dict) else None
        out = []
        for q in (raw or []):
            q = str(q).strip()
            if q and q not in out:
                out.append(q)
        return out[:MAX_QUERIES] or [question]
    except Exception as e:
        print(f"[搜索] 查询改写失败，回落到原问题：{e}")
        return [question]


# ============================================================
# 多查询并行检索（合并 + 按 URL 去重）
# ============================================================

def search_multi(queries, provider, url, key, count):
    """并行执行多个检索式，按 URL 去重后返回；单个查询失败不影响其它"""
    queries = [q for q in (queries or []) if str(q).strip()][:MAX_QUERIES]
    if not queries:
        return []
    per = max(3, count)
    results, seen = [], set()
    with ThreadPoolExecutor(max_workers=min(4, len(queries))) as ex:
        futs = [(ex.submit(search_web, q, provider, url, key, per), q) for q in queries]
        for fut, q in futs:                 # 按检索式顺序收集，保证结果稳定
            try:
                items = fut.result()
            except Exception as e:
                print(f"[搜索] 检索式「{q}」失败：{e}")
                continue
            for it in items:
                u = (it.get("url") or "").strip()
                mark = u or (it.get("title") or "").strip()
                if not mark or mark in seen:
                    continue
                seen.add(mark)
                it["query"] = q
                results.append(it)
    return results[:MAX_MERGED]


# ============================================================
# 正文抓取（Jina Reader 优先，失败回落直连 + 去标签）
# ============================================================

class _TextExtractor(HTMLParser):
    """极简 HTML 取文本：跳过 script/style 等，块级标签补换行"""
    _SKIP = ("script", "style", "noscript", "svg", "head")
    _BLOCK = ("p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "section", "article")

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self.skip += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self.skip:
            self.skip -= 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            d = (data or "").strip()
            if d:
                self.parts.append(d + " ")

def _strip_html(html):
    p = _TextExtractor()
    try:
        p.feed(html or "")
    except Exception:
        pass
    return "".join(p.parts)

def _tidy(text):
    text = re.sub(r"[ \t]+", " ", text or "")
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    text = re.sub(r"^[ \t]+", "", text, flags=re.M)
    return text.strip()

def _fetch_one(url, reader="jina", timeout=20):
    """抓单个网页正文；失败返回空串"""
    url = (url or "").strip()
    if not url.lower().startswith("http"):
        return ""
    if reader != "direct":
        try:
            r = requests.get("https://r.jina.ai/" + url, timeout=timeout,
                             headers={"Accept": "text/plain",
                                      "User-Agent": UA})
            if r.status_code == 200 and r.text.strip():
                return _tidy(r.text)[:FETCH_CHARS]
        except Exception:
            pass
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": UA})
        r.raise_for_status()
        return _tidy(_strip_html(r.text))[:FETCH_CHARS]
    except Exception:
        return ""

def fetch_pages(items, limit=4, reader="jina"):
    """为前 limit 条结果并行抓正文，写入 it['content']；失败则保持原样（用 snippet）"""
    limit = max(0, min(int(limit or 0), len(items)))
    if limit <= 0:
        return items
    with ThreadPoolExecutor(max_workers=min(4, limit)) as ex:
        futs = [(ex.submit(_fetch_one, it.get("url"), reader), it) for it in items[:limit]]
        for fut, it in futs:
            try:
                text = fut.result()
            except Exception:
                text = ""
            if text:
                it["content"] = text
    return items


def slim_items(items):
    """只保留可入库/可展示的字段（正文体积大，不入库）"""
    keys = ("title", "url", "site", "snippet")
    return [{k: (it.get(k) or "") for k in keys} for it in (items or [])]

def build_search_context(query, items):
    """把搜索结果拼成给模型看的参考资料（有正文就用正文，否则用摘要）"""
    lines = [f"以下是根据用户问题「{query}」联网搜索到的参考资料。",
             "这些资料可能不完整或过时，请结合它们回答，并在引用处用 [1] [2] 标注来源编号。",
             "如果资料与问题无关，请忽略并如实说明。", ""]
    for i, it in enumerate(items, 1):
        lines.append(f"[{i}] {it['title']}" + (f" —— {it['site']}" if it.get("site") else ""))
        body = (it.get("content") or it.get("snippet") or "").strip()
        if body:
            lines.append(body)
        lines.append(f"URL: {it['url']}")
        lines.append("")
    return "\n".join(lines)
