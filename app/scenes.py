# -*- coding: utf-8 -*-
"""场景包：办公 / 学习 / 生活。

场景只影响两件事：
1. 系统提示里的一段「场景纪律」；
2. 能力清单的**展示优先级**（不硬性禁用，避免把用户卡死）。

场景是会话级设置（`session.scene`），默认 general。
"""
SCENES = {
    "general": {
        "name": "通用",
        "desc": "不做额外约束",
        "prompt": "",
        "caps": [],
    },
    "office": {
        "name": "办公",
        "desc": "结论先行、清单化、主动建日程/任务",
        "prompt": (
            "【场景：办公】\n"
            "- 先给结论，再给依据；能用清单/表格就不要写长段散文。\n"
            "- 涉及时间安排时，主动用 calendar.* 建日程；涉及多步执行时，用 task.create 拆解。\n"
            "- 产出尽量落成可交付物（文件、清单、邮件草稿），而不是只给建议。"
        ),
        "caps": ["calendar.create", "calendar.list", "calendar.update",
                 "task.create", "task.list", "task.get", "memory.search",
                 "note.search", "note.read", "search.web", "doc.read"],
    },
    "study": {
        "name": "学习",
        "desc": "讲原理、分步骤、用提问确认理解",
        "prompt": (
            "【场景：学习】\n"
            "- 先讲原理与直觉，再给例子；复杂概念拆成 2–4 步。\n"
            "- 不要直接代写作业或给出完整答案；先给思路，必要时再展开。\n"
            "- 适当用一个小问题确认用户是否理解；涉及复习计划时用 task.create 拆成每日步骤。"
        ),
        "caps": ["task.create", "task.list", "memory.remember", "memory.search",
                 "note.search", "note.read", "search.web", "doc.read"],
    },
    "life": {
        "name": "生活",
        "desc": "轻松简短，关注时间、地点与提醒",
        "prompt": (
            "【场景：生活】\n"
            "- 语气轻松、简短，不要写成工作汇报。\n"
            "- 涉及约会/出行/缴费等时间点时，主动确认并用 calendar.* 记下来。\n"
            "- 不主动给医疗、法律、投资等高风险建议；必要时提醒用户咨询专业人士。"
        ),
        "caps": ["calendar.create", "calendar.list", "calendar.update",
                 "memory.search", "memory.remember"],
    },
}

_KEYWORDS = {
    "office": ("会议", "周报", "日报", "汇报", "PPT", "方案", "客户", "项目",
               "邮件", "排期", "复盘", "KPI", "需求", "文档"),
    "study": ("学习", "复习", "考试", "课程", "作业", "题", "讲解", "知识点",
              "背单词", "论文", "做题", "理解"),
    "life": ("吃", "睡", "健身", "旅行", "买", "缴费", "约会", "医院",
             "提醒我", "周末", "家里", "生活"),
}


def list_scenes():
    return [{"id": k, "name": v["name"], "desc": v["desc"]} for k, v in SCENES.items()]


def normalize(scene):
    s = str(scene or "").strip().lower()
    return s if s in SCENES else "general"


def get(scene):
    return SCENES[normalize(scene)]


def prompt_for(scene):
    return get(scene).get("prompt") or ""


def allowed_caps(scene):
    return list(get(scene).get("caps") or [])


def filter_caps(scene, cap_ids):
    """按场景排序：场景相关的排前面，其余保留（不隐藏能力）。"""
    allow = allowed_caps(scene)
    if not allow:
        return list(cap_ids)
    preferred = [x for x in cap_ids if x in allow]
    rest = [x for x in cap_ids if x not in allow]
    return preferred + rest


def detect(text):
    """粗略意图路由：返回得分最高的场景；没有命中返回 general。"""
    t = str(text or "")
    best, score = "general", 0
    for scene, words in _KEYWORDS.items():
        n = sum(1 for w in words if w in t)
        if n > score:
            best, score = scene, n
    return best
