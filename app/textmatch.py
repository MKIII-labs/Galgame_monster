# -*- coding: utf-8 -*-
"""轻量文本切分：英文按词、中文按双字 + 整段（不依赖分词库）

memory（统一索引）与 knowledge（图谱检索）共用同一套切分，
避免出现「同一个查询在两套检索里命中不同条目」。
"""

import re

_WORD_RE = re.compile(r"[a-z0-9_\-]{2,}")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")

# 只挡掉高频虚词：真正的语义词（如「规则」「偏好」「项目」）必须保留
STOP = frozenset("""
的 了 是 我 你 他 她 它 我们 你们 他们 这 那 这个 那个 这些 那些 在 有 和 与 就 不 也 都 很
吗 呢 吧 啊 一个 什么 怎么 如何 可以 需要 现在 然后 但是 因为 所以 如果 已经 还是 只是 就是
没有 不是 自己 一下 一些 有点 那个 那么 这样 那样
""".split())


def tokens(text, limit=24):
    """返回去重后的 token 列表（保持出现顺序，便于调试）。"""
    q = (text or "").lower().strip()
    if not q:
        return []
    out, seen = [], set()

    def add(t):
        if t and t not in STOP and t not in seen:
            seen.add(t)
            out.append(t)

    for w in _WORD_RE.findall(q):
        add(w)
    for seg in _CJK_RE.findall(q):
        if len(seg) == 1:
            add(seg)
            continue
        if len(seg) <= 12:          # 整段精确匹配：短查询时价值很高
            add(seg)
        for i in range(len(seg) - 1):
            add(seg[i:i + 2])
    return out[:limit]
