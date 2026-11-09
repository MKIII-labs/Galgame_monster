# -*- coding: utf-8 -*-
"""未命中构成观测（批 1 补测：只统计，不参与任何装配决策）。

为什么需要：账单里最贵的是「未命中输入」（约为命中价的 50 倍）。但
`prompt - cached` 这个数只告诉你「付了多少钱」，不告诉你「钱花在哪」——
可能是本轮真的新增了工具结果，也可能是上一轮的内容被折叠/压缩改写，
导致从改写点开始整段重算，还可能只是缓存没接上。

这里按轮把每一轮的 miss 归到四类：

- cold   ：本回合第一轮、且该会话之前没有任何请求（无前缀可比）。
- new    ：缓存正常命中时「必然要付」的部分，即相对上一次请求新增/变化的 token。
- rebuild：miss 里超出 new 的部分，且本轮发送前发生过工具结果折叠或历史压缩。
- drift  ：剩下的部分——既不是新增，也不是本地能解释的改写（缓存没按预期命中）。

字段设计成「拿不到就降级」：旧记录缺 est / prefix_tokens 时按 0 处理，
不会抛异常。
"""


def _i(v):
    try:
        return max(0, int(v or 0))
    except (TypeError, ValueError):
        return 0


def _scaled(tokens, prompt, est):
    """把本地估算的 token 换算到「真实 prompt」的尺度。

    估算器与实际 tokenizer 有系统性偏差（中文、工具结果、多模态都不同），
    用本轮的 prompt/est 比例校准一下，避免把估算误差算成「漂移」。
    比例离谱（<0.2 或 >8）时放弃校准，按原值算。
    """
    tokens = max(0, int(tokens or 0))
    if est > 0 and prompt > 0:
        ratio = float(prompt) / float(est)
        if 0.2 <= ratio <= 8.0:
            return int(round(tokens * ratio))
    return tokens


def _split_first(r, miss, out):
    """本回合第一轮：用 prefixtrace 的「公共前缀」判断新增/非新增。"""
    if r.get("cold"):
        out["cold"] += miss
        return
    cur = _i(r.get("cur_tokens"))
    prefix = _i(r.get("prefix_tokens"))
    prompt = _i(r.get("prompt"))
    changed = _scaled(max(0, cur - prefix), prompt, cur)
    new = min(miss, changed)
    out["new"] += new
    rest = miss - new
    if rest <= 0:
        return
    if r.get("fold"):
        out["rebuild"] += rest
    else:
        out["drift"] += rest


def _split_next(r, miss, prev_est, out):
    """本回合第 2 轮起：用「相邻两轮的上下文估算增量」判断新增。"""
    est = _i(r.get("est"))
    prompt = _i(r.get("prompt"))
    base = est if est > 0 else prompt
    growth = max(0, base - prev_est) if prev_est is not None else 0
    new = min(miss, _scaled(growth, prompt, base))
    out["new"] += new
    rest = miss - new
    if rest <= 0:
        return
    if r.get("fold"):
        out["rebuild"] += rest
    else:
        out["drift"] += rest


def split(rounds):
    """把一轮回合的 miss 拆成 {cold, new, rebuild, drift, total, rounds}。

    rounds: [{"prompt", "cached", "est", "fold", "cold", "prefix_tokens", "cur_tokens"}, ...]
    按发送顺序排列；首轮可用 cold/prefix_tokens/cur_tokens，其余轮用 est。
    """
    out = {"cold": 0, "new": 0, "rebuild": 0, "drift": 0, "total": 0, "rounds": 0}
    prev_est = None
    seen = 0                 # 只数「拿到真实用量」的轮次，避免首轮缺数据时错位
    for r in rounds or []:
        if not isinstance(r, dict):
            continue
        prompt = _i(r.get("prompt"))
        if prompt <= 0:
            continue
        miss = max(0, prompt - _i(r.get("cached")))
        out["total"] += miss
        out["rounds"] += 1
        if seen == 0:
            _split_first(r, miss, out)
        else:
            _split_next(r, miss, prev_est, out)
        est = _i(r.get("est"))
        prev_est = est if est > 0 else prompt
        seen += 1
    return out
