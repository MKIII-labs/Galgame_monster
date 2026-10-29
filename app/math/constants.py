# -*- coding: utf-8 -*-
"""数学竞技场的共享常量（从 math_arena.py 拆出）。"""

ROUND_TTL_SEC = 6 * 3600
MAX_AI = 3
QUESTION_COUNT = 5

SUBJECT_NAMES = {
    "analysis": "数学分析",
    "algebra": "高等代数",
    "number": "初等数论",
    "abstract": "抽象代数",
    "probability": "概率论",
    "statistics": "数理统计",
    "ode": "常微分方程",
    "complex": "复变函数",
    "geometry": "解析几何",
    "numerical": "数值分析",
    "modeling": "数学建模",
    "discrete": "离散数学",
}

# 选课粒度：数学分析拆 3 学期、高等代数拆 2 学期；其余课程与学科一一对应。
# subject 仍保留大学科口径，用于「总计」掌握度；course 用于选课与学期小计。
COURSE_NAMES = {
    "analysis1": "数学分析（一）",
    "analysis2": "数学分析（二）",
    "analysis3": "数学分析（三）",
    "algebra1": "高等代数（一）",
    "algebra2": "高等代数（二）",
    "number": "初等数论",
    "abstract": "抽象代数",
    "probability": "概率论",
    "statistics": "数理统计",
    "ode": "常微分方程",
    "complex": "复变函数",
    "geometry": "解析几何",
    "numerical": "数值分析",
    "modeling": "数学建模",
    "discrete": "离散数学",
}
COURSE_PARENT = {
    "analysis1": "analysis",
    "analysis2": "analysis",
    "analysis3": "analysis",
    "algebra1": "algebra",
    "algebra2": "algebra",
}
COURSE_PARENT.update({k: k for k in COURSE_NAMES if k not in COURSE_PARENT})
COURSE_ORDER = [
    "analysis1", "analysis2", "analysis3", "algebra1", "algebra2",
    "number", "abstract", "discrete", "probability", "statistics",
    "ode", "complex", "geometry", "numerical", "modeling",
]

AI_PLAYERS = [
    {"id": "ai_rigorous", "name": "严谨派", "style": "先证条件，再算结果",
     "base": 0.74, "tf_bonus": 0.08, "calc_bonus": 0.05, "speed": (8, 22)},
    {"id": "ai_intuitive", "name": "直觉派", "style": "先估量级，快速作答",
     "base": 0.52, "tf_bonus": -0.05, "calc_bonus": -0.03, "speed": (2, 10)},
    {"id": "ai_counter", "name": "反例派", "style": "先找反例，再下判断",
     "base": 0.63, "tf_bonus": 0.11, "calc_bonus": -0.02, "speed": (4, 14)},
]

HUMAN_ID = "you"
