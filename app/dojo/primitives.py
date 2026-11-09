# -*- coding: utf-8 -*-
"""研究道场的训练原语与判分器。

设计约束（不要为了「多支持一种题型」而放松）：
- L1/L2 才产生分数；L3 只给诊断。LLM 文本评分存在系统性偏差
  （弱作业被抬高、强作业被压低），所以开放文本永远不进提分统计。
- 判分只读 unit["expert"]，答复里由 engine 决定是否回传；
  题面接口一律走 packs.public_unit，不泄露 expert。
- 宁可判错报错，也不要「猜一个分」：输入不合法时抛 JudgeError。
"""
import ast
import math

PRIMITIVES = (
    "single_choice", "multi_select", "order", "pair",
    "numeric", "expr", "annotate", "short_text",
)
JUDGE_LEVELS = ("L1", "L2", "L3")


class JudgeError(Exception):
    """答案格式不对，或该题无法按声明的方式判定。"""


def _result(correct, score, **detail):
    return {"correct": bool(correct), "score": max(0.0, min(1.0, float(score))), "detail": detail}


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [str(value)]


def _option_keys(payload):
    """选项 key：显式给了就用，否则按 A/B/C/D 顺序生成。"""
    keys = []
    for i, opt in enumerate(payload.get("options") or []):
        if isinstance(opt, dict) and opt.get("key"):
            keys.append(str(opt["key"]))
        else:
            keys.append(chr(65 + i))
    return keys


def _judge_single_choice(unit, answer):
    payload, expert = unit["payload"], unit["expert"]
    keys = _option_keys(payload)
    if not keys:
        raise JudgeError("题目没有选项")
    got = str(answer or "").strip().upper()
    if got not in keys:
        raise JudgeError("选项不在范围内")
    want = str(expert.get("choice") or "").strip().upper()
    if want not in keys:
        raise JudgeError("题目的标准答案配置有误")
    return _result(got == want, 1.0 if got == want else 0.0, choice=got)


def _judge_multi_select(unit, answer):
    payload, expert = unit["payload"], unit["expert"]
    keys = set(_option_keys(payload))
    if not keys:
        raise JudgeError("题目没有选项")
    got = {k.upper() for k in _as_list(answer)}
    if not got <= keys:
        raise JudgeError("选项不在范围内")
    want = {str(k).upper() for k in (expert.get("choice") or [])}
    if not want <= keys:
        raise JudgeError("题目的标准答案配置有误")
    hit = len(got & want)
    miss = len(got - want)
    # 误选要扣分：竞赛里「多选了错的」和「少选了对的」都不算掌握
    score = max(0.0, (hit - 0.5 * miss) / max(1, len(want)))
    return _result(got == want, score, hit=sorted(got & want), missed=sorted(want - got),
                   wrong=sorted(got - want))


def _judge_order(unit, answer):
    payload, expert = unit["payload"], unit["expert"]
    steps = {str(s["id"]) for s in (payload.get("steps") or [])}
    want = [str(x) for x in (expert.get("order") or [])]
    got = _as_list(answer)
    if not want or set(want) != steps:
        raise JudgeError("题目的标准顺序配置有误")
    if len(got) != len(want) or set(got) != set(want):
        raise JudgeError("请把所有步骤都排进来")
    total = len(want) * (len(want) - 1) // 2
    if total <= 0:
        return _result(got == want, 1.0 if got == want else 0.0, order=got)
    pos = {v: i for i, v in enumerate(got)}
    concord = sum(1 for i in range(len(want))
                  for j in range(i + 1, len(want))
                  if pos[want[i]] < pos[want[j]])
    return _result(got == want, concord / total, order=got, concordant=concord, pairs=total)


def _judge_pair(unit, answer):
    expert = unit["expert"]
    want = {str(k): str(v) for k, v in (expert.get("pairs") or {}).items()}
    if not want:
        raise JudgeError("题目的配对答案配置有误")
    if isinstance(answer, dict):
        got = {str(k): str(v) for k, v in answer.items()}
    else:
        raise JudgeError("配对答案要传对象")
    if not set(got) <= set(want):
        raise JudgeError("配对的左侧不在题目里")
    hit = sum(1 for k, v in got.items() if want.get(k) == v)
    return _result(hit == len(want), hit / len(want), hit=hit, total=len(want),
                   wrong=[k for k, v in got.items() if want.get(k) != v])


def _to_float(value):
    try:
        return float(str(value).strip().replace(",", ""))
    except (TypeError, ValueError):
        raise JudgeError("请填一个数值") from None


def _judge_numeric(unit, answer):
    expert = unit["expert"]
    want = _to_float(expert.get("value"))
    got = _to_float(answer)
    rtol = float(expert.get("tolerance", 0.01))
    atol = float(expert.get("abs_tolerance", 1e-9))
    err = abs(got - want)
    ok = err <= atol or err <= rtol * max(abs(want), 1e-12)
    rel = err / max(abs(want), 1e-12)
    # 误差越大给越少的部分分，但误差 ≥4 倍容差直接 0：避免「差很远也拿分」
    score = 0.0
    if ok:
        score = 1.0
    elif rel < rtol * 4:
        score = max(0.0, 1.0 - rel / (rtol * 4))
    return _result(ok, score, value=got, error=err, relative=rel)


_FUNCS = {
    "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "exp": math.exp, "log": math.log, "ln": math.log,
    "sqrt": math.sqrt, "abs": abs,
}
_CONSTS = {"pi": math.pi, "e": math.e}

_ALLOWED_NODES = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.Name, ast.Load,
    ast.Call, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.Mod,
    ast.USub, ast.UAdd,
)


def _normalize_expr(text):
    s = str(text or "").strip()
    s = s.replace("π", "pi").replace("×", "*").replace("÷", "/")
    s = s.replace("^", "**")
    # ln 与 log 在数学书写里常混用；这里统一成自然对数
    s = s.replace("ln(", "log(")
    return s


def _compile_expr(text, var):
    src = _normalize_expr(text)
    if not src:
        raise JudgeError("请填一个表达式")
    try:
        tree = ast.parse(src, mode="eval")
    except SyntaxError:
        raise JudgeError("表达式语法不对") from None
    allowed_names = set(_FUNCS) | set(_CONSTS) | {var}
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise JudgeError("表达式里有不支持的写法")
        if isinstance(node, ast.Name) and node.id not in allowed_names:
            raise JudgeError("表达式里有不认识的符号：%s" % node.id)
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCS:
                raise JudgeError("只支持常见数学函数")
    return compile(tree, "<dojo-expr>", "eval")


def _eval_expr(code, var, x):
    env = {"__builtins__": {}}
    env.update(_FUNCS)
    env.update(_CONSTS)
    env[var] = x
    val = eval(code, env, {})                       # noqa: S307 —— 节点已按白名单校验
    if not isinstance(val, (int, float)) or not math.isfinite(val):
        raise ValueError("非有限值")
    return float(val)


def _judge_expr(unit, answer):
    expert = unit["expert"]
    var = str(unit["payload"].get("var") or "x")
    want_code = _compile_expr(expert.get("expr"), var)
    got_code = _compile_expr(answer, var)
    # 固定采样点：同一份答案每次判分结果一致，便于复盘与测试
    points = [-2.7, -1.9, -1.1, -0.37, 0.43, 1.23, 2.05, 2.9]
    ok_points, total, first_bad = 0, 0, None
    for x in points:
        try:
            a = _eval_expr(want_code, var, x)
            b = _eval_expr(got_code, var, x)
        except (ValueError, ZeroDivisionError, OverflowError):
            continue
        total += 1
        if abs(a - b) <= 1e-6 * max(1.0, abs(a), abs(b)):
            ok_points += 1
        elif first_bad is None:
            first_bad = x
    if total < 4:
        raise JudgeError("这个表达式在采样点上没有定义，换个写法")
    return _result(ok_points == total, ok_points / total, points=total,
                   matched=ok_points, first_mismatch=first_bad)


def _judge_annotate(unit, answer):
    expert = unit["expert"]
    segments = {str(s["id"]) for s in (unit["payload"].get("segments") or [])}
    want = {str(x) for x in (expert.get("segments") or [])}
    if not want or not want <= segments:
        raise JudgeError("题目的关键句配置有误")
    got = {str(x) for x in _as_list(answer)}
    if not got <= segments:
        raise JudgeError("标出了不存在的句子")
    if not got:
        return _result(False, 0.0, hit=[], missed=sorted(want))
    hit = len(got & want)
    if not hit:
        return _result(False, 0.0, hit=[], missed=sorted(want), extra=sorted(got))
    precision = hit / len(got)
    recall = hit / len(want)
    return _result(got == want, 2 * precision * recall / (precision + recall),
                   hit=sorted(got & want), missed=sorted(want - got),
                   extra=sorted(got - want))


def _judge_short_text(unit, answer):
    """L3：只做要点覆盖诊断，不产生分数。

    关键词命中是一个粗糙代理，所以这里刻意不叫「评分」；
    engine 会把 scored 置为 False，前端只能展示「覆盖/缺失」。
    """
    rubric = unit["expert"].get("rubric") or []
    text = str(answer or "").strip().lower()
    if not text:
        raise JudgeError("先写一句理由再提交")
    hits, missing, total_w, hit_w = [], [], 0.0, 0.0
    for item in rubric:
        weight = float(item.get("weight", 1))
        total_w += weight
        keywords = [str(k).lower() for k in (item.get("keywords") or []) if str(k).strip()]
        if any(k in text for k in keywords):
            hits.append(item["id"])
            hit_w += weight
        else:
            missing.append(item["id"])
    coverage = (hit_w / total_w) if total_w else 0.0
    return _result(False, 0.0, hits=hits, missing=missing, coverage=coverage)


_JUDGES = {
    "single_choice": _judge_single_choice,
    "multi_select": _judge_multi_select,
    "order": _judge_order,
    "pair": _judge_pair,
    "numeric": _judge_numeric,
    "expr": _judge_expr,
    "annotate": _judge_annotate,
    "short_text": _judge_short_text,
}


def judge(unit, answer):
    """按单元声明判分。

    返回 {scored, correct, score, judge, detail}：
    - scored=False 表示这一题不计分（L3，或题型本身只做诊断）
    - score ∈ [0,1]，供前端折算掌握度；不计分时恒为 0
    """
    primitive = unit.get("primitive")
    fn = _JUDGES.get(primitive)
    if not fn:
        raise JudgeError("不支持的题型：%s" % primitive)
    level = str(unit.get("judge") or "")
    if level not in JUDGE_LEVELS:
        raise JudgeError("题目的判定等级配置有误")
    out = fn(unit, answer)
    scored = level in ("L1", "L2")
    return {
        "scored": scored,
        "correct": bool(out["correct"]) if scored else False,
        "score": out["score"] if scored else 0.0,
        "judge": level,
        "detail": out["detail"],
    }
