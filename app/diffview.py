# -*- coding: utf-8 -*-
"""行级 diff：把两份文本切成前端可直接渲染的「块对」。

前端展示的不是标准左右 / 单栏 diff，而是「变更前整块在上、变更后整块在下」：
未改动的上下文行也属于块（浅色底），只有真正删掉 / 新增的行才是深色。
因此这里除了行内容，还要给出每行的种类（ctx / del / ins）。

只做纯计算，不读文件、不碰状态，方便单测。
"""

import difflib

# 超大文件不做逐行对齐：纯 Python 的 SequenceMatcher 在这种规模下会把界面拖住，
# 退化成「整篇一块」至少还能看，不会卡死。
MAX_LINES = 20000


def _lines(text):
    # 先统一换行再切分；空文本算 0 行，末尾换行会多出一个空行（与编辑器一致）
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not t:
        return []
    return t.split("\n")


def _int_or(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _whole(a, b):
    """退化模式：整篇旧文一块、整篇新文一块。"""
    return {
        "hunks": [{
            "old_start": 1, "new_start": 1,
            "old": a, "new": b,
            "old_kinds": ["del"] * len(a),
            "new_kinds": ["ins"] * len(b),
            "gap_before": 0,
        }],
        "adds": len(b), "dels": len(a),
        "old_total": len(a), "new_total": len(b),
        "gap_after": 0,
    }


def build(old_text, new_text, context=3, stat_only=False):
    """返回 {hunks, adds, dels, old_total, new_total, gap_after}。

    hunk 的字段：
      old_start / new_start  块首行号（1 起）
      old / new              行文本（不含换行）
      old_kinds / new_kinds  与行一一对应，'ctx' 未改动 / 'del' 删除 / 'ins' 新增
      gap_before             本块之前被折叠的行数
    """
    a, b = _lines(old_text), _lines(new_text)
    ctx = max(0, _int_or(context, 3))
    if len(a) + len(b) > MAX_LINES:
        if stat_only:
            return {"hunks": [], "adds": len(b), "dels": len(a),
                    "old_total": len(a), "new_total": len(b), "gap_after": 0}
        return _whole(a, b)

    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    ops = [op for op in sm.get_opcodes() if op[0] != "equal"]
    adds = sum(op[4] - op[3] for op in ops)
    dels = sum(op[2] - op[1] for op in ops)
    if not ops:
        return {"hunks": [], "adds": 0, "dels": 0,
                "old_total": len(a), "new_total": len(b), "gap_after": 0}
    if stat_only:
        # 只要增删数时不必把每行都复制进 hunk
        return {"hunks": [], "adds": adds, "dels": dels,
                "old_total": len(a), "new_total": len(b), "gap_after": 0}

    # 相邻改动之间未改动的行不多于 2*context 就并成一块，避免碎成一堆小块
    groups = []
    for op in ops:
        last = groups[-1][-1] if groups else None
        if last and op[1] - last[2] <= ctx * 2 and op[3] - last[4] <= ctx * 2:
            groups[-1].append(op)
        else:
            groups.append([op])

    out = []
    prev_old_end = 0
    for g in groups:
        i1 = max(0, g[0][1] - ctx)
        i2 = min(len(a), g[-1][2] + ctx)
        j1 = max(0, g[0][3] - ctx)
        j2 = min(len(b), g[-1][4] + ctx)
        ok = ["ctx"] * (i2 - i1)
        nk = ["ctx"] * (j2 - j1)
        for _tag, oi1, oi2, nj1, nj2 in g:
            for k in range(oi1, oi2):
                ok[k - i1] = "del"
            for k in range(nj1, nj2):
                nk[k - j1] = "ins"
        out.append({
            "old_start": i1 + 1,
            "new_start": j1 + 1,
            "old": a[i1:i2],
            "new": b[j1:j2],
            "old_kinds": ok,
            "new_kinds": nk,
            "gap_before": i1 - prev_old_end,
        })
        prev_old_end = i2

    return {
        "hunks": out, "adds": adds, "dels": dels,
        "old_total": len(a), "new_total": len(b),
        "gap_after": len(a) - prev_old_end,
    }
