# -*- coding: utf-8 -*-
"""研究台账：把「来源 / 缺口 / 结论」从上下文搬到磁盘。

为什么需要它
------------
1. 主循环会把较早的工具结果压成 100 字占位，只保留最近 2 条 ——
   多轮检索的证据第二轮就看不见了，等于白搜；
2. 没有「还缺什么」的账本，模型缺少继续检索的硬信号，容易一轮就收尾；
3. 每次 web_search 的编号都从 [1] 开始，跨调用冲突，引用无法核验。

设计
----
- 每个会话一份 ``data/research/<sid>.json``，跨轮、跨重启保留；
- 主上下文只放「未解决缺口 + 最近来源」的短摘要（``context_block``），
  详细内容留在磁盘，需要时由模型用 research_note / web_search 再取；
- 模型通过 ``research_note`` 工具显式登记缺口与结论；
  ``web_search`` 的结果由主循环自动登记来源（编号全局递增、按 URL 去重）。

参数为 0 一律表示「不限 / 关闭」，与项目其它配置保持一致。
"""
import json
import os
import re
import threading

from app.store import DATA_DIR

MAX_SOURCES = 200        # 超过后不再登记新来源（编号必须保持稳定，不能回退覆盖）
MAX_GAPS = 80
MAX_CLAIMS = 80

# 任务是不是「复刻 / 还原 / 从零实现」类：这类任务的知识缺口在外部，必须先研究。
_RESEARCH_RE = re.compile(
    r"还原|复刻|复现|重制|重建|从零(?:实现|写|做|搭)|仿照|仿制|照搬|一模一样|"
    r"复刻版|clone|replicate|reimplement",
    re.I)

_CITE_RE = re.compile(r"\[(\d{1,3})\]")

_CACHE = {}
_CACHE_LOCK = threading.Lock()


def is_research_task(text):
    """用户这次要做的，是不是「照着某个已有系统重建」类任务。"""
    return bool(_RESEARCH_RE.search(str(text or "")))


def _safe_sid(sid):
    s = re.sub(r"[^A-Za-z0-9_-]", "", str(sid or ""))
    return s or "default"


class Ledger:
    """单个会话的研究台账。所有写操作都会立刻落盘（文件很小）。"""

    def __init__(self, sid):
        self.sid = _safe_sid(sid)
        self.sources = []      # [{n, url, title, site, snippet, query}]
        self.gaps = []         # [{id, q, open}]
        self.claims = []       # [{id, text, sources:[n]}]
        self.searches = 0      # web_search 调用次数
        self._lock = threading.Lock()
        self._load()

    # ------------------------------------------------------------ 存储

    def _path(self):
        return os.path.join(DATA_DIR, "research", self.sid + ".json")

    def _load(self):
        try:
            with open(self._path(), "r", encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            return
        if not isinstance(d, dict):
            return
        self.sources = [s for s in (d.get("sources") or []) if isinstance(s, dict)]
        self.gaps = [g for g in (d.get("gaps") or []) if isinstance(g, dict)]
        self.claims = [c for c in (d.get("claims") or []) if isinstance(c, dict)]
        try:
            self.searches = max(0, int(d.get("searches") or 0))
        except (TypeError, ValueError):
            self.searches = 0

    def _save(self):
        try:
            d = os.path.dirname(self._path())
            os.makedirs(d, exist_ok=True)
            tmp = self._path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"sources": self.sources, "gaps": self.gaps,
                           "claims": self.claims, "searches": self.searches},
                          f, ensure_ascii=False)
            os.replace(tmp, self._path())
        except Exception:
            pass

    # ------------------------------------------------------------ 写入

    def add_sources(self, hits, query=""):
        """登记一批检索结果，返回 [(编号, 结果)]；同一 URL 只登记一次。

        返回的是**全部**命中（含之前登记过的），这样同一批结果里的编号始终全局一致；
        重复 URL 复用旧编号，不会再从 1 开始。
        """
        out = []
        with self._lock:
            by_url = {(s.get("url") or "").strip(): int(s.get("n") or 0)
                      for s in self.sources}
            for it in (hits or []):
                u = str((it or {}).get("url") or "").strip()
                if not u:
                    continue
                n = by_url.get(u)
                if n is None:
                    if len(self.sources) >= MAX_SOURCES:
                        continue
                    n = len(self.sources) + 1
                    self.sources.append({
                        "n": n, "url": u,
                        "title": str(it.get("title") or ""),
                        "site": str(it.get("site") or ""),
                        "snippet": str(it.get("snippet") or "")[:300],
                        "query": str(query or "")[:120]})
                    by_url[u] = n
                out.append((n, it))
            if out:
                self.searches += 1
            self._save()
        return out

    def add_gaps(self, items):
        added = []
        with self._lock:
            for q in (items or []):
                q = str(q or "").strip()
                if not q or len(self.gaps) >= MAX_GAPS:
                    continue
                if any(g.get("q") == q for g in self.gaps):
                    continue
                gid = "g%d" % (len(self.gaps) + 1)
                self.gaps.append({"id": gid, "q": q[:300], "open": True})
                added.append(gid)
            if added:
                self._save()
        return added

    def close_gaps(self, ids):
        done = []
        want = {str(x or "").strip() for x in (ids or []) if str(x or "").strip()}
        with self._lock:
            for g in self.gaps:
                if g.get("id") in want and g.get("open", True):
                    g["open"] = False
                    done.append(g.get("id"))
            if done:
                self._save()
        return done

    def add_claims(self, items):
        added = []
        with self._lock:
            for c in (items or []):
                if isinstance(c, dict):
                    text = str(c.get("text") or "").strip()
                    srcs = c.get("sources") or []
                else:
                    text, srcs = str(c or "").strip(), []
                if not text or len(self.claims) >= MAX_CLAIMS:
                    continue
                nums = []
                for s in (srcs if isinstance(srcs, list) else [srcs]):
                    try:
                        nums.append(int(s))
                    except (TypeError, ValueError):
                        continue
                cid = "c%d" % (len(self.claims) + 1)
                self.claims.append({"id": cid, "text": text[:400], "sources": nums[:8]})
                added.append(cid)
            if added:
                self._save()
        return added

    def clear(self):
        with self._lock:
            self.sources, self.gaps, self.claims, self.searches = [], [], [], 0
            self._save()

    # ------------------------------------------------------------ 读取

    def open_gaps(self):
        return [g for g in self.gaps if g.get("open", True)]

    def stats(self):
        return {"searches": self.searches,
                "sources": len(self.sources),
                "gaps": len(self.open_gaps()),
                "claims": len(self.claims)}

    def bad_citations(self, text):
        """正文里引用了不存在的来源编号 → 返回这些编号（升序）。"""
        if not self.sources:
            return []
        have = {int(s.get("n") or 0) for s in self.sources}
        bad = set()
        for m in _CITE_RE.finditer(str(text or "")):
            try:
                n = int(m.group(1))
            except ValueError:
                continue
            if n >= 1 and n not in have:
                bad.add(n)
        return sorted(bad)

    def context_block(self, max_sources=6, max_gaps=6):
        """给系统提示的短摘要；没有内容时返回空串（不污染上下文）。"""
        if not self.sources and not self.gaps:
            return ""
        st = self.stats()
        lines = ["## 研究台账（跨轮保留，不随工具结果被裁掉）",
                 "已检索 %d 次 · 来源 %d 条 · 未解决缺口 %d 条"
                 % (st["searches"], st["sources"], st["gaps"])]
        gaps = self.open_gaps()[:max_gaps]
        if gaps:
            lines.append("未解决缺口：" + "；".join(
                "%s %s" % (g.get("id"), g.get("q")) for g in gaps))
        recent = self.sources[-max_sources:]
        if recent:
            lines.append("最近来源：")
            for s in recent:
                _snip = " ".join(str(s.get("snippet") or "").split())[:80]
                lines.append("[%d] %s%s —— %s%s" % (
                    int(s.get("n") or 0), s.get("title") or "(无标题)",
                    (" —— " + s["site"]) if s.get("site") else "", s.get("url"),
                    ("\n    摘要：" + _snip) if _snip else ""))
        lines.append("缺口补上后用 research_note(action=\"gap_done\") 关掉；"
                     "引用只能写台账里已有的编号。")
        return "\n".join(lines)

    def summary(self):
        st = self.stats()
        return ("研究台账：检索 %d 次 · 来源 %d 条 · 未解决缺口 %d 条 · 结论 %d 条"
                % (st["searches"], st["sources"], st["gaps"], st["claims"]))


def clear_ledger(sid):
    """清空某个会话的研究台账：新任务 / 清空会话时调用，避免旧来源冒充本轮检索。"""
    try:
        get(sid).clear()          # 走缓存对象，避免同进程里旧台账还在内存里
    except Exception:
        pass


def get(sid):
    """取（或创建）某会话的台账；同一进程内复用同一个对象。"""
    key = _safe_sid(sid)
    with _CACHE_LOCK:
        obj = _CACHE.get(key)
        if obj is None:
            obj = Ledger(key)
            _CACHE[key] = obj
        return obj


def note(args, sid):
    """处理 research_note 工具调用，返回 (ok, 给模型看的文本)。"""
    args = args if isinstance(args, dict) else {}
    action = str(args.get("action") or "").strip().lower()
    ledger = get(sid)
    if action in ("", "status"):
        block = ledger.context_block()
        return True, block or "研究台账还是空的：先用 web_search 检索，或登记缺口。"
    if action == "gap_add":
        ids = ledger.add_gaps(args.get("gaps") or args.get("items") or [])
        if not ids:
            return False, "没有新增缺口（内容为空或已存在）。"
        return True, "已登记缺口：%s\n%s" % ("、".join(ids), ledger.summary())
    if action == "gap_done":
        done = ledger.close_gaps(args.get("ids") or [])
        if not done:
            return False, "没有匹配的未解决缺口：%s" % (args.get("ids") or [])
        return True, "已关闭缺口：%s\n%s" % ("、".join(done), ledger.summary())
    if action == "claim_add":
        ids = ledger.add_claims(args.get("claims") or args.get("items") or [])
        if not ids:
            return False, "没有新增结论（内容为空）。"
        return True, "已登记结论：%s\n%s" % ("、".join(ids), ledger.summary())
    if action == "clear":
        ledger.clear()
        return True, "研究台账已清空。"
    return False, ("未知 action：%s（可用 gap_add / gap_done / claim_add / status / clear）"
                   % (action or "（空）"))
