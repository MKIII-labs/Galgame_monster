# -*- coding: utf-8 -*-
"""竞技场的过程能力层。

知识点回答“哪个内容不会”，8 维回答“哪个认知动作不会”。
两者都保留：前者用于补课，后者用于换题/换科目时迁移。

原则：
- 一个知识点只归一个主能力，避免同一道题重复计入多个维度；
- 竞技场以计算/判断为主，`framing / assumption / robustness / reflection`
  天然样本少，前端必须显示“样本不足”，不能画满雷达。
"""

ABILITIES = (
    "framing", "assumption", "method", "condition",
    "evidence", "robustness", "argument", "reflection",
)
ABILITY_NAMES = {
    "framing": "定义问题",
    "assumption": "建立假设",
    "method": "选择方法",
    "condition": "判断适用性",
    "evidence": "证据",
    "robustness": "稳健性",
    "argument": "论证",
    "reflection": "反思迁移",
}

# 96 个知识点 → 主能力。按学科分组，便于以后人工校正。
SKILL_ABILITY = {
    # 数学分析
    "数列极限": "method",
    "连续与可导": "condition",
    "导数应用": "method",
    "黎曼积分": "method",
    "反常积分": "condition",
    "级数": "condition",
    "全微分": "condition",
    "多元连续与偏导": "condition",
    "混合偏导": "condition",
    "重积分": "method",
    "曲线积分": "method",
    "Green 公式": "condition",
    # 高等代数
    "行列式": "method",
    "矩阵运算": "method",
    "线性方程组": "method",
    "秩与特征值": "condition",
    "特征值": "method",
    "对角化": "condition",
    "二次型": "method",
    "线性变换": "argument",
    "多项式": "method",
    "Jordan 标准形": "condition",
    # 抽象代数
    "子群": "condition",
    "正规子群": "condition",
    "循环群": "method",
    "同态": "argument",
    "理想": "condition",
    "整环": "condition",
    "置换": "method",
    "群中心": "argument",
    # 解析几何
    "向量点积": "method",
    "向量叉积": "method",
    "向量垂直": "condition",
    "平面方程": "method",
    "点到平面距离": "method",
    "旋转变换": "method",
    "二次曲面": "condition",
    # 初等数论
    "整除": "method",
    "同余": "method",
    "最大公因数": "method",
    "欧拉函数": "method",
    "素数": "method",
    "费马小定理": "condition",
    "二次剩余": "condition",
    "因数分解": "method",
    "奇偶性": "argument",
    "无理数": "argument",
    # 概率论
    "条件概率": "method",
    "条件概率与独立性": "condition",
    "独立性": "assumption",
    "互斥与独立": "assumption",
    "期望与方差": "method",
    "随机变量类型": "condition",
    "相关与独立": "assumption",
    # 数理统计
    "假设检验": "evidence",
    "置信区间": "evidence",
    "无偏估计": "condition",
    "极大似然": "method",
    # 常微分方程
    "方程分类": "condition",
    "一阶方程": "method",
    "一阶线性方程": "method",
    "二阶常系数方程": "method",
    "初值问题": "condition",
    "线性方程": "method",
    "线性相关与无关": "argument",
    "Wronskian": "evidence",
    # 复变函数
    "复数的模": "method",
    "复数的辐角": "method",
    "解析函数": "condition",
    "解析性判定": "condition",
    "Cauchy–Riemann 方程": "condition",
    "初等解析函数": "method",
    "围道积分": "method",
    "奇点": "condition",
    # 数值分析
    "误差": "robustness",
    "二分法": "method",
    "Newton 法": "method",
    "求根方法": "method",
    "数值积分": "method",
    "条件数": "robustness",
    # 数学建模
    "建模流程": "framing",
    "数据分析": "evidence",
    "模型选择": "reflection",
    "线性规划": "method",
    "人口模型": "method",
    "指数模型": "method",
    "冷却模型": "method",
    "复利": "method",
    "排队模型": "method",
    "金融建模": "method",
    "Bayes 与假阳性": "evidence",
    # 离散数学
    "集合与关系": "method",
    "等价关系": "condition",
    "欧拉图": "condition",
    "树": "condition",
    "命题逻辑": "argument",
}


def ability_of(skill):
    return SKILL_ABILITY.get(str(skill or ""), "method")


def _row(ability, name):
    return {"ability": ability, "ability_name": name, "attempts": 0,
            "correct": 0, "score": 0.0, "time_ms": 0}


def aggregate(rows):
    """把逐题记录聚合成 by_skill / by_ability。

    rows: [{skill, correct, score?, elapsed_ms?}, ...]
    返回的 accuracy 是百分比；score 是 0–100 的平均分，便于与竞技场现有展示一致。
    """
    by_skill, by_ability = {}, {}
    for item in rows or []:
        skill = str(item.get("skill") or "未标注")
        correct = bool(item.get("correct"))
        score = float(item.get("score") or 0)
        elapsed = int(item.get("elapsed_ms") or 0)
        s = by_skill.setdefault(skill, {"skill": skill, **_row("", "")})
        s.pop("ability", None)
        s.pop("ability_name", None)
        s["attempts"] += 1
        s["correct"] += 1 if correct else 0
        s["score"] += score
        s["time_ms"] += elapsed

        key = ability_of(skill)
        a = by_ability.setdefault(key, _row(key, ABILITY_NAMES.get(key, key)))
        a["attempts"] += 1
        a["correct"] += 1 if correct else 0
        a["score"] += score
        a["time_ms"] += elapsed

    skills = []
    for s in by_skill.values():
        s["accuracy"] = round(100.0 * s["correct"] / max(1, s["attempts"]), 1)
        s["avg_score"] = round(s["score"] / max(1, s["attempts"]), 1)
        skills.append(s)
    abilities = []
    for a in by_ability.values():
        a["accuracy"] = round(100.0 * a["correct"] / max(1, a["attempts"]), 1)
        a["avg_score"] = round(a["score"] / max(1, a["attempts"]), 1)
        abilities.append(a)
    skills.sort(key=lambda x: (-x["attempts"], x["skill"]))
    abilities.sort(key=lambda x: ABILITIES.index(x["ability"]))
    return {"by_skill": skills, "by_ability": abilities}


def meta():
    return {
        "abilities": [{"id": k, "name": ABILITY_NAMES[k]} for k in ABILITIES],
        "skills": dict(SKILL_ABILITY),
    }
