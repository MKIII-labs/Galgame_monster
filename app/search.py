# -*- coding: utf-8 -*-
"""联网搜索：服务商配置、查询改写、多查询并行检索与正文抓取"""
import ipaddress
import json
import re
import socket
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import requests

from app.config import load_config

# 联网搜索服务商（可在「设置 → 联网搜索」里改地址/密钥）
SEARCH_PROVIDERS = {
    "auto": {"name": "自动（免注册引擎链）",
             "url": "",
             "hint": "Firecrawl → Keenable → You.com 依次尝试，都不需要注册或密钥，"
                     "失败自动换下一个；额度耗尽的引擎会冷却一段时间。"
                     "可在下方填各自的 Key 换用个人额度，或取消勾选把某个引擎移出链。"},
    "deepseek": {"name": "DeepSeek 搜索（复用聊天密钥）",
                 "url": "https://api.deepseek.com/v1/tools/search",
                 "hint": "与聊天 API 同一个 Key，按次计费（5 元 / 1000 次成功请求）；"
                         "默认在聊天模型支持时自动启用"},
    "bocha": {"name": "博查 Bocha", "url": "https://api.bocha.cn/v1/web-search",
              "hint": "申请地址：open.bochaai.com（国内直连，中文结果较好）"},
    "volc": {"name": "豆包搜索（火山）",
             "url": "https://open.feedcoopapi.com/search_api/web_search",
             "hint": "申请地址：console.volcengine.com/search-infinity（0.02元/次，每月免费 500 次）"},
    "tavily": {"name": "Tavily", "url": "https://api.tavily.com/search",
               "hint": "申请地址：app.tavily.com（面向 AI，每月 1000 次免费额度）"},
}

MAX_QUERIES = 3        # 单次最多改写几个检索式
MAX_MERGED = 12        # 合并去重后的结果上限
FETCH_CHARS = 2500     # 每条正文最多截取多少字
SCRAPE_TIMEOUT = 30    # 云端抓页（Firecrawl）比本地抓取慢，单独给更长预算
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def chat_search_available(cfg=None):
    """聊天模型/服务商是否自带可用搜索。

    目前只接入了 DeepSeek 的 Search API：它复用聊天 API 的地址与密钥，
    所以只要聊天地址是 api.deepseek.com 且模型声明了 web_search 能力即可。
    """
    cfg = cfg or load_config()
    try:
        from app.config import get_api_conf
        from app.model_caps import caps_for
        key, url, model = get_api_conf("chat", cfg)
    except Exception:                    # noqa: BLE001  能力判断失败时回落独立搜索 API
        return False
    if not key or "deepseek" not in str(url or "").lower():
        return False
    return bool(caps_for(model, cfg).get("web_search"))


def _deepseek_search_url(chat_url):
    """从聊天接口地址推导 Search API：https://api.deepseek.com/v1/tools/search"""
    try:
        from urllib.parse import urlparse
        p = urlparse(str(chat_url or ""))
        if p.scheme and p.netloc:
            return "%s://%s/v1/tools/search" % (p.scheme, p.netloc)
    except Exception:                    # noqa: BLE001  地址异常就用官方默认值
        pass
    return SEARCH_PROVIDERS["deepseek"]["url"]


def effective_search_provider(cfg=None):
    """本轮实际使用的服务商（供设置页 / 诊断显示）。"""
    cfg = cfg or load_config()
    provider = (cfg.get("search_provider") or "auto").strip() or "auto"
    if provider == "auto":
        # 总开关关掉后，旧配置里的 auto 按原来的默认服务商处理
        if bool(cfg.get("search_modsearch_enabled", True)):
            return "auto"
        provider = "bocha"
    if not bool(cfg.get("search_prefer_api")) and chat_search_available(cfg):
        return "deepseek"
    return provider


def get_search_conf():
    cfg = load_config()
    provider = effective_search_provider(cfg)
    try:
        count = int(cfg.get("search_count") or 5)
    except (TypeError, ValueError):
        count = 5
    count = max(1, min(count, 20))
    if provider == "auto":               # 引擎链：Key 由 search_engines 按引擎分别读
        return provider, "", "", count
    if provider == "deepseek":
        # 复用聊天 API 的密钥；用户把 search_url 指到 DeepSeek 代理时尊重它
        from app.config import get_api_conf
        key, chat_url, _model = get_api_conf("chat", cfg)
        _su = (cfg.get("search_url") or "").strip()
        url = _su if "deepseek" in _su.lower() else _deepseek_search_url(chat_url)
        return provider, url, key, count
    p = SEARCH_PROVIDERS.get(provider) or SEARCH_PROVIDERS["bocha"]
    provider = provider if provider in SEARCH_PROVIDERS else "bocha"
    url = (cfg.get("search_url") or "").strip() or p["url"]
    key = (cfg.get("search_api_key") or "").strip()
    return provider, url, key, count

def search_web(query, provider, url, key, count):
    """调用搜索服务，返回 [{title, url, site, snippet}]；失败时抛出异常"""
    if provider == "auto":               # 免注册引擎链：逐个尝试，全失败才抛
        from app import search_engines
        return search_engines.auto_search(query, count)
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if provider == "deepseek":
        # DeepSeek Search API：正文由下面的抓取链路单独取（need_content=false）
        payload = {"query": query, "count": count, "need_content": False}
    elif provider == "tavily":
        payload = {"query": query, "search_depth": "basic",
                   "max_results": count, "include_answer": False}
    elif provider == "volc":          # 豆包搜索（火山）：字段名首字母大写
        payload = {"Query": query, "SearchType": "web", "Count": count,
                   "Filter": {"NeedContent": False, "NeedUrl": True},
                   "QueryControl": {"QueryRewrite": False}}   # 改写由本程序自己做，避免重复
    else:  # bocha
        payload = {"query": query, "freshness": "noLimit",
                   "summary": True, "count": count}

    resp = requests.post(url, headers=headers,
                         data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), timeout=25)
    resp.raise_for_status()
    d = resp.json()

    items = []
    if provider == "deepseek":
        for it in (d.get("results") or [])[:count]:
            items.append({
                "title": it.get("title") or "",
                "url": it.get("url") or "",
                "site": it.get("site_name") or "",
                # summary 是为模型准备的长摘要（600–800 字），优先于 snippet
                "snippet": (it.get("summary") or it.get("snippet") or "").strip()[:600],
            })
    elif provider == "tavily":
        for it in (d.get("results") or [])[:count]:
            items.append({
                "title": it.get("title") or "",
                "url": it.get("url") or "",
                "site": "",
                "snippet": (it.get("content") or "").strip()[:600],
            })
    elif provider == "volc":
        err = (d.get("ResponseMetadata") or {}).get("Error")
        if err:
            raise RuntimeError(err.get("Message") or err.get("Code") or "豆包搜索返回错误")
        web = ((d.get("Result") or {}).get("WebResults")) or []
        for it in web[:count]:
            items.append({
                "title": it.get("Title") or "",
                "url": it.get("Url") or "",
                "site": it.get("SiteName") or "",
                # Snippet 只有约 200 字，官方不建议喂模型；优先用为 LLM 设计的 Summary
                "snippet": (it.get("Summary") or it.get("Content")
                            or it.get("Snippet") or "").strip()[:600],
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
# 多查询并行检索（合并 + 按 URL 去重）
# ============================================================

def search_multi(queries, provider, url, key, count, should_stop=None, merged_max=None):
    """并行执行多个检索式，按 URL 去重后返回；单个查询失败不影响其它。

    should_stop()：返回 True 时立即放弃剩余检索（用户点了「停止」）。
    在飞的 HTTP 请求杀不掉，但**不再等它们**，所以停止是即时响应的。
    merged_max：合并后的条数上限；None = MAX_MERGED（研究类任务可放宽）。
    """
    queries = [q for q in (queries or []) if str(q).strip()][:MAX_QUERIES]
    if not queries or (should_stop and should_stop()):
        return []
    per = max(3, count)
    results, seen, errors = [], set(), []
    ex = ThreadPoolExecutor(max_workers=min(4, len(queries)))
    stopped = False
    futs = []
    try:
        futs = [(ex.submit(search_web, q, provider, url, key, per), q) for q in queries]
        for fut, q in futs:                 # 按检索式顺序收集，保证结果稳定
            items = None
            while True:                     # 轮询等待，保证点「停止」立刻生效
                if should_stop and should_stop():
                    stopped = True
                    break
                try:
                    items = fut.result(timeout=0.1)
                    break
                except FuturesTimeout:
                    continue
                except Exception as e:
                    print(f"[搜索] 检索式「{q}」失败：{e}")
                    errors.append(str(e))
                    break
            if stopped:
                break
            for it in (items or []):
                u = (it.get("url") or "").strip()
                mark = u or (it.get("title") or "").strip()
                if not mark or mark in seen:
                    continue
                seen.add(mark)
                it["query"] = q
                results.append(it)
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    # 一条都没搜到、并且每个检索式都报错：把真实原因抛给上层，
    # 否则界面会显示成「没有搜到相关结果」，看不出是引擎/密钥的问题。
    if not results and errors and futs and len(errors) >= len(futs):
        raise RuntimeError("；".join(dict.fromkeys(errors))[:400])
    try:
        cap = int(merged_max or 0) or MAX_MERGED
    except (TypeError, ValueError):
        cap = MAX_MERGED
    return results[:max(1, cap)]


# ============================================================
# 正文抓取（云端浏览器优先，失败回落 Jina Reader / 直连去标签）
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


def _excerpt(text, query, limit=FETCH_CHARS):
    """按检索词挑相关段落，而不是一律取前 N 字。

    网页正文往往前 2500 字全是导航 / 版权声明；按段落命中数从高到低挑，
    再按原文顺序拼回去，同样的字数里信息密度高得多。
    """
    text = text or ""
    if len(text) <= limit:
        return text
    terms = [t for t in re.split(r"[\s,，。;；、/|]+", str(query or ""))
             if len(t) >= 2][:8]
    paras = [p.strip() for p in text.split("\n") if p.strip()]
    if not terms or not paras:
        return text[:limit]
    scored = []
    for i, p in enumerate(paras):
        s = sum(p.count(t) for t in terms)
        scored.append((s, i, p))
    picked, total = [], 0
    for s, i, p in sorted(scored, key=lambda x: (-x[0], x[1])):
        if total >= limit:
            break
        if s <= 0 and picked:            # 已经挑到相关段落，就不再拿无关段落凑数
            break
        picked.append((i, p))
        total += len(p) + 1
    if not picked:
        return text[:limit]
    out, used = [], 0
    for _i, p in sorted(picked):
        room = limit - used
        if room <= 0:
            break
        if len(p) > room:
            p = p[:max(0, room - 1)] + "…"
        out.append(p)
        used += len(p) + 1
    return "\n".join(out)


def public_url_error(url):
    """这个 URL 能不能安全抓取？返回 "" 表示通过，否则是拒绝原因。

    只允许公网 http/https：本机、内网、链路本地、保留地址一律拒绝。
    域名会先解析再逐个 IP 判断（防「localhost 换个域名就进来」）。
    """
    try:
        u = urlparse(str(url or "").strip())
    except ValueError:
        return "URL 解析失败"
    if u.scheme not in ("http", "https"):
        return "只支持 http(s) URL"
    host = (u.hostname or "").strip().lower()
    if not host:
        return "URL 缺少主机名"
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        return "不允许访问本机 / 内网地址：%s" % host
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception as e:                       # noqa: BLE001
        return "域名解析失败：%s" % e
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_unspecified):
            return "不允许访问内网地址：%s" % host
    return ""


def _public_get(url, timeout=20, headers=None, max_redirects=3):
    """不自动跟随重定向的 GET：每一跳都重新做公网校验。

    requests 默认会自动跟 30x，公网页只要 302 到 http://127.0.0.1:端口
    就能把抓正文变成打本机服务（SSRF）。这里手动跟，最多 3 跳。
    """
    cur = str(url or "").strip()
    for _ in range(max_redirects + 1):
        if public_url_error(cur):
            return None
        try:
            r = requests.get(cur, timeout=timeout, allow_redirects=False,
                             headers=headers or {})
        except Exception:                        # noqa: BLE001
            return None
        if r is None:
            return None
        if r.status_code in (301, 302, 303, 307, 308):
            loc = r.headers.get("Location") or ""
            if not loc:
                return None
            cur = urljoin(cur, loc)
            continue
        return r
    return None


def _fetch_one(url, reader="jina", timeout=20, query="", chars=FETCH_CHARS, cloud=None):
    """抓单个网页正文；失败返回空串。给了 query 就按相关段落截取。

    顺序：云端浏览器（Firecrawl，能渲染 JS 页面）→ r.jina.ai → 直连去标签。
    云端那步会把 URL 发给 Firecrawl，所以私网目标直接跳过，只走本地。
    """
    url = (url or "").strip()
    if not url.lower().startswith("http"):
        return ""
    if cloud is None:
        cloud = bool(load_config().get("search_fetch_cloud", True))
    if cloud and reader != "direct" and not public_url_error(url):
        from app import search_engines
        md = search_engines.cloud_fetch(url, timeout=max(SCRAPE_TIMEOUT, timeout))
        if md:
            return _excerpt(_tidy(md), query, chars)
    if reader != "direct":
        r = _public_get("https://r.jina.ai/" + url, timeout=timeout,
                        headers={"Accept": "text/plain", "User-Agent": UA})
        if r is not None and r.status_code == 200 and r.text.strip():
            return _excerpt(_tidy(r.text), query, chars)
    r = _public_get(url, timeout=timeout, headers={"User-Agent": UA})
    if r is None:
        return ""
    try:
        r.raise_for_status()
        return _excerpt(_tidy(_strip_html(r.text)), query, chars)
    except Exception:                            # noqa: BLE001
        return ""

def fetch_text(url, timeout=20, query="", chars=FETCH_CHARS):
    """抓单个网页正文（供 fetch_url 工具用）；失败返回空串。

    复用搜索链路的 reader：优先 r.jina.ai（正文干净），失败再直连 + 去标签。
    """
    return _fetch_one(url, reader="jina", timeout=timeout, query=query, chars=chars)


def fetch_pages(items, limit=4, reader="jina", on_item=None, should_stop=None,
                query="", chars=FETCH_CHARS):
    """为前 limit 条结果并行抓正文，写入 it['content']；失败则保持原样（用 snippet）。

    on_item(it, text)：每读完一条回调一次（按完成先后，谁快谁先回调），
    供上层实时汇报「正在读取网页」的进度。
    should_stop()：返回 True 时立即放弃剩余抓取（用户点了「停止」），
    不再等在飞的抓取（单个网页最长可能卡到几十秒）。
    query：用于挑选正文里的相关段落（见 _excerpt）。
    chars：每条正文最多保留多少字；研究类任务可放宽。
    """
    limit = max(0, min(int(limit or 0), len(items)))
    if limit <= 0:
        return items
    try:
        chars = int(chars or 0) or FETCH_CHARS
    except (TypeError, ValueError):
        chars = FETCH_CHARS
    ex = ThreadPoolExecutor(max_workers=min(4, limit))
    try:
        futs = {ex.submit(_fetch_one, it.get("url"), reader, 20, query, chars): it
                for it in items[:limit]}
        pending = list(futs)
        while pending:
            if should_stop and should_stop():
                break
            ready = [f for f in pending if f.done()]
            if not ready:
                time.sleep(0.05)
                continue
            for fut in ready:
                pending.remove(fut)
                it = futs[fut]
                try:
                    text = fut.result()
                except Exception:
                    text = ""
                if text:
                    it["content"] = text
                if on_item:
                    try:
                        on_item(it, text)
                    except Exception:
                        pass
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    return items


def slim_items(items):
    """只保留可入库/可展示的字段（正文体积大，不入库）"""
    keys = ("title", "url", "site", "snippet")
    return [{k: (it.get(k) or "") for k in keys} for it in (items or [])]

def build_search_context(query, items, start=1):
    """把搜索结果拼成给模型看的参考资料（有正文就用正文，否则用摘要）。

    条目带 ``n`` 时用它的编号（研究台账的全局编号，跨调用连续且不重复）；
    否则从 start 开始顺排。
    """
    lines = [f"以下是根据用户问题「{query}」联网搜索到的参考资料。",
             "这些资料可能不完整或过时，请结合它们回答，并在引用处用 [1] [2] 标注来源编号。",
             "如果资料与问题无关，请忽略并如实说明。", ""]
    for i, it in enumerate(items):
        try:
            n = int(it.get("n") or 0) or (start + i)
        except (TypeError, ValueError):
            n = start + i
        lines.append(f"[{n}] {it['title']}" + (f" —— {it['site']}" if it.get("site") else ""))
        body = (it.get("content") or it.get("snippet") or "").strip()
        if body:
            lines.append(body)
        lines.append(f"URL: {it['url']}")
        lines.append("")
    return "\n".join(lines)
