# -*- coding: utf-8 -*-
"""证明训练：AI 按步骤点评（不是形式化验证）。"""
from app.math.constants import COURSE_NAMES
from app.math.errors import MathError

# ------------------------------------------------------------
# 证明题：AI 步骤评分（不是形式化验证）
# ------------------------------------------------------------

PROOFS = [
    {"id": "pf_analysis_monotone", "course": "analysis1", "skill": "数列极限", "difficulty": 3,
     "prompt": "证明：若数列 {a_n} 单调递增且有上界，则 {a_n} 收敛。",
     "hints": ["用确界原理", "极限就是上确界"],
     "outline": ["由非空有界数集有上确界，设 M=sup{a_n}",
                 "对任意 ε>0，存在 N 使 a_N>M-ε",
                 "由单调递增，n≥N 时 M-ε<a_n≤M",
                 "按极限定义得到 a_n→M"]},
    {"id": "pf_analysis_sqrt2", "course": "analysis1", "skill": "无理数", "difficulty": 2,
     "prompt": "证明：√2 不是有理数。",
     "hints": ["反证法", "设 √2=p/q 且 p、q 互素"],
     "outline": ["反设 √2=p/q，p、q 为互素整数且 q≠0",
                 "平方得 p²=2q²，所以 p² 为偶数，p 为偶数",
                 "设 p=2k，代入得 4k²=2q²，即 q²=2k²",
                 "q 也为偶数，与 p、q 互素矛盾"]},
    {"id": "pf_analysis_series", "course": "analysis2", "skill": "级数", "difficulty": 3,
     "prompt": "证明：级数 ∑_{n=1}^∞ 1/n² 收敛。",
     "hints": ["比较判别法", "把 1/n² 与望远镜级数比较"],
     "outline": ["n≥2 时 1/n² ≤ 1/[n(n-1)]",
                 "而 1/[n(n-1)] = 1/(n-1) - 1/n",
                 "∑_{n=2}^N 1/[n(n-1)] = 1 - 1/N 有界",
                 "由比较判别法，原级数收敛"]},
    {"id": "pf_algebra_det", "course": "algebra1", "skill": "矩阵运算", "difficulty": 2,
     "prompt": "证明：若 n 阶方阵 A 可逆，则 det(A)≠0。",
     "hints": ["利用 AA⁻¹=I", "行列式乘法性质"],
     "outline": ["A 可逆，存在 A⁻¹ 使 AA⁻¹=I",
                 "两边取行列式：det(A)det(A⁻¹)=det(I)=1",
                 "所以 det(A)≠0"]},
    {"id": "pf_algebra_dependent", "course": "algebra1", "skill": "线性方程组", "difficulty": 3,
     "prompt": "证明：n 维向量空间中的任意 n+1 个向量必线性相关。",
     "hints": ["把向量作为列组成矩阵", "齐次方程组 Unknown 多于方程"],
     "outline": ["把 n+1 个向量作为列组成 n×(n+1) 矩阵 A",
                 "齐次方程组 Ax=0 有 n 个方程、n+1 个未知量",
                 "未知量多于方程，存在非零解",
                 "非零解给出不全为零的线性组合为 0，故线性相关"]},
    {"id": "pf_algebra_symmetric", "course": "algebra2", "skill": "特征值", "difficulty": 4,
     "prompt": "证明：实对称矩阵的特征值都是实数。",
     "hints": ["在复数域中取特征向量", "利用共轭转置与内积"],
     "outline": ["设 Ax=λx，x≠0，λ 可能为复数",
                 "两边左乘 x* 得 x*Ax=λx*x",
                 "由 A 实对称，x*Ax 为实数，x*x 为正实数",
                 "所以 λ 为实数"]},
    {"id": "pf_algebra_eigenvectors", "course": "algebra2", "skill": "特征值", "difficulty": 3,
     "prompt": "证明：不同特征值对应的特征向量线性无关。",
     "hints": ["对向量个数归纳", "用 A-λI 作用"],
     "outline": ["设 λ1,…,λk 互不相同，对应特征向量 x1,…,xk",
                 "假设有线性关系 c1x1+…+ckxk=0",
                 "用 A-λkI 作用，消去 xk",
                 "归纳得到各系数为 0"]},
    {"id": "pf_probability_complement", "course": "probability", "skill": "独立性", "difficulty": 2,
     "prompt": "证明：若事件 A 与 B 独立，则 A 与 B 的补事件也独立。",
     "hints": ["P(A∩B^c)=P(A)-P(A∩B)"],
     "outline": ["A = (A∩B) ∪ (A∩B^c)，且两部分互斥",
                 "P(A∩B^c)=P(A)-P(A∩B)",
                 "由独立性 P(A∩B)=P(A)P(B)",
                 "得 P(A∩B^c)=P(A)(1-P(B))=P(A)P(B^c)"]},
    {"id": "pf_number_primes", "course": "number", "skill": "素数", "difficulty": 2,
     "prompt": "证明：素数有无穷多个。",
     "hints": ["反证法", "构造 N=p1p2…pn+1"],
     "outline": ["假设只有有限个素数 p1,…,pn",
                 "令 N=p1p2…pn+1",
                 "N 有素因子 p，且 p 必为某个 pi",
                 "但 N 除以 pi 余 1，矛盾"]},
    {"id": "pf_ode_linear", "course": "ode", "skill": "一阶线性方程", "difficulty": 3,
     "prompt": "证明：一阶线性方程 y'+P(x)y=Q(x) 的通解为 y=e^{-∫Pdx}(∫Qe^{∫Pdx}dx+C)。",
     "hints": ["找积分因子", "把左边写成乘积的导数"],
     "outline": ["取积分因子 μ(x)=e^{∫Pdx}",
                 "方程两边乘 μ，左边化为 (μy)'",
                 "积分得 μy=∫μQ dx+C",
                 "两边除以 μ 得通解"]},
    {"id": "pf_complex_square", "course": "complex", "skill": "解析函数", "difficulty": 2,
     "prompt": "证明：f(z)=z² 在复平面上解析，并求 f'(z)。",
     "hints": ["写成 u+iv", "验证 Cauchy–Riemann 方程"],
     "outline": ["令 z=x+iy，f=z²=(x²-y²)+2xyi",
                 "u=x²-y²，v=2xy",
                 "u_x=2x=v_y，u_y=-2y=-v_x",
                 "偏导连续且满足 C-R 方程，故解析，f'(z)=2z"]},
    {"id": "pf_numerical_bisection", "course": "numerical", "skill": "二分法", "difficulty": 3,
     "prompt": "证明：若 f 在 [a,b] 连续且 f(a)f(b)<0，则二分法产生的区间长度趋于 0，并收敛到根。",
     "hints": ["每次区间长度减半", "用区间套定理"],
     "outline": ["每次迭代保留含根区间，长度变为原来的一半",
                 "第 n 次区间长度 (b-a)/2^n→0",
                 "区间套定理给出唯一公共点 x*",
                 "由连续性 f(x*)=0，故二分法收敛到根"]},
]


def proof_meta(course=None):
    items = []
    for p in PROOFS:
        if course and p.get("course") != course:
            continue
        items.append({
            "id": p["id"], "course": p["course"],
            "course_name": COURSE_NAMES.get(p["course"], ""),
            "skill": p.get("skill", ""), "difficulty": p.get("difficulty", 2),
            "prompt": p["prompt"], "hints": list(p.get("hints") or []),
        })
    return {"proofs": items}


def _as_list(value):
    return value if isinstance(value, (list, tuple)) else []


def _text_list(value, limit, each):
    out = []
    for item in _as_list(value)[:limit]:
        s = str(item or "").strip()
        if s:
            out.append(s[:each])
    return out


_PROOF_SYSTEM = (
    "你是大学数学阅卷助教。请对照给定的标准步骤检查学生证明。"
    "硬性规则：只评价学生实际写出的内容，不要假设学生写了未出现的步骤；"
    "不要替学生补证明；不要编造教材结论。"
    "输出 JSON：{\"score\":0-100,\"verdict\":\"通过|基本通过|需要补充|未通过\","
    "\"steps\":[{\"point\":\"标准步骤\",\"status\":\"有|部分|缺失\",\"comment\":\"具体评价\"}],"
    "\"missing\":[\"缺失的关键点\"],\"suggestions\":[\"下一步怎么改\"],"
    "\"confidence\":\"高|中|低\"}。不要输出 Markdown，不要代码围栏。"
)


def grade_proof(proof_id, text):
    """AI 步骤评分；无模型时返回 degraded，并给出标准步骤供自评。"""
    proof = next((p for p in PROOFS if p["id"] == proof_id), None)
    if not proof:
        raise MathError("没有这道证明题")
    text = str(text or "").strip()
    if len(text) < 20:
        raise MathError("证明太短，先写下关键步骤")
    if len(text) > 8000:
        text = text[:8000]
    user = ("题目：%s\n\n标准步骤：\n%s\n\n学生证明：\n%s"
            % (proof["prompt"], "\n".join("- " + x for x in proof["outline"]), text))
    obj = None
    try:
        from app.interview import _llm_json
        obj = _llm_json(_PROOF_SYSTEM, user, max_tokens=1600, source="math_proof")
    except Exception as e:                                  # noqa: BLE001
        print("[数学] 证明评分失败：%s" % e)
    if isinstance(obj, dict):
        try:
            score = max(0, min(100, int(obj.get("score") or 0)))
        except (TypeError, ValueError):
            score = 0
        steps = []
        for it in _as_list(obj.get("steps"))[:12]:
            if isinstance(it, dict):
                steps.append({
                    "point": str(it.get("point") or "")[:300],
                    "status": str(it.get("status") or "部分")[:10],
                    "comment": str(it.get("comment") or "")[:500],
                })
        out = {
            "score": score,
            "verdict": str(obj.get("verdict") or "需要补充")[:20],
            "steps": steps,
            "missing": _text_list(obj.get("missing"), 8, 300),
            "suggestions": _text_list(obj.get("suggestions"), 8, 400),
            "confidence": str(obj.get("confidence") or "中")[:10],
            "outline": list(proof["outline"]),
            "degraded": False,
            "disclaimer": "AI 步骤点评仅供参考，不是形式化验证；以人工解析为准。",
        }
    else:
        out = {
            "score": None, "verdict": "未评分", "steps": [],
            "missing": [], "suggestions": ["当前没有连接聊天模型，请对照标准步骤自评。"],
            "confidence": "低", "outline": list(proof["outline"]),
            "degraded": True,
            "disclaimer": "未连接模型，未生成评分；标准步骤仅供自评。",
        }
    # 证明评分也是一次「尝试」：记下分数与缺失点数量，供之后归因
    try:
        from app import observe
        observe.record(
            source="math_proof",
            subject=COURSE_NAMES.get(proof.get("course"), ""),
            topic=proof.get("skill", ""),
            value={"score": out.get("score"),
                   "verdict": out.get("verdict", ""),
                   "missing": len(out.get("missing") or []),
                   "degraded": bool(out.get("degraded"))},
            evidence="题目：%s\n学生证明：%s" % (proof.get("prompt", ""), text))
    except Exception as e:                       # noqa: BLE001
        print("[数学] 记录观测失败：%s" % e)
    return out
