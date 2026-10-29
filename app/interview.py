# -*- coding: utf-8 -*-
"""模拟面试：简历分析 / AI 简历工坊 / 语音面试 / 复盘报告。

设计原则
--------
1. **只使用用户提供的事实**：简历生成与润色都反复强调「不得编造公司、数字、经历」；
2. **模型失败可降级**：所有 AI 能力都有本地兜底（规则评分 / 本地题库），
   接口始终返回可用结果，不会因为没配密钥就整块不可用；
3. **面试题不提前下发**：start 只返回第一题，后续题目存在服务端会话里，
   避免前端一次拿到整套题面；
4. **会话是进程内状态**：与数学竞技场的 round 一样，重启即清空，前端负责持久化记录。

对外主要函数
------------
- analyze_resume / build_resume / polish_resume / match_jd
- start_session / interview_turn / finish_session
- preset_questions / evaluate_answer / expression_stats
"""
import json
import random
import re
import threading
import time
import uuid

from app.config import get_api_conf
from app.llm import _quick_chat

# ---------------- 常量 ----------------

INTERVIEW_TYPES = {
    "hr": "HR 行为面",
    "technical": "专业技术面",
    "stress": "压力面",
    "mixed": "综合面",
}

INTERVIEW_DIMENSIONS = ["表达清晰", "逻辑结构", "专业能力", "岗位匹配", "应变抗压"]
RESUME_DIMENSIONS = ["完整度", "结构清晰", "成果量化", "岗位匹配", "表达专业"]

MAX_RESUME = 20000
MAX_JD = 8000
MAX_ANSWER = 6000
MAX_PROFILE = 12000
MAX_SUMMARY = 2500
MAX_QUESTIONS = 10
SESSION_TTL = 2 * 3600
MAX_SESSIONS = 40


class InterviewError(Exception):
    """给前端看的业务错误（文案可直接展示）"""


# ---------------- 通用小工具 ----------------

def _s(v):
    return str(v or "").strip()


def _clip(text, limit):
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[:limit]


def _score100(v, default=60):
    """把模型可能给出的 "85" / "85分" / 0.85 统一成 0-100 整数"""
    if isinstance(v, bool):
        return default
    try:
        if isinstance(v, str):
            m = re.search(r"-?\d+(?:\.\d+)?", v)
            if not m:
                return default
            v = m.group(0)
        n = float(v)
    except (TypeError, ValueError):
        return default
    if 0 <= n <= 1:
        n *= 100
    return int(max(0, min(100, round(n))))


def _as_list(v):
    return v if isinstance(v, list) else []


def _text_list(v, limit=8, each=300):
    out = []
    for item in _as_list(v)[:limit]:
        if isinstance(item, dict):
            item = item.get("text") or item.get("content") or item.get("name") or ""
        s = _s(item)
        if s:
            out.append(s[:each])
    return out


def _lang(v):
    return "en" if str(v or "").lower().startswith("en") else "zh"


def _type(v):
    return v if v in INTERVIEW_TYPES else "mixed"


# ---------------- JSON 解析 ----------------

def extract_json(text):
    """从模型输出里尽力提取 JSON。

    模型常见的三种写法都要吃下：纯 JSON、```json 围栏、正文夹一段 JSON。
    解析失败返回 None，由调用方决定重试或走本地兜底。
    """
    s = str(text or "").strip()
    if not s:
        return None
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s).strip()
    try:
        return json.loads(s)
    except Exception:
        pass
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", s, re.I)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except Exception:
            pass
    dec = json.JSONDecoder()
    for i, ch in enumerate(s):
        if ch not in "{[":
            continue
        try:
            obj, _end = dec.raw_decode(s[i:])
            return obj
        except Exception:
            continue
    return None


def _llm_json(system, user, max_tokens=1800, source="interview"):
    """调用聊天模型并解析 JSON；无密钥 / 网络失败 / 两次解析失败都返回 None"""
    api_key, api_url, api_model = get_api_conf("chat")
    if not api_key:
        return None
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    try:
        text = _quick_chat(api_key, api_url, api_model, messages,
                           max_tokens=max_tokens, source=source)
    except Exception as e:                                  # noqa: BLE001
        print("[面试] 模型调用失败：%s" % e)
        return None
    obj = extract_json(text)
    if obj is not None:
        return obj
    # 格式纠正重试一次：这次把上一次的输出也带上，明确要求只回 JSON
    try:
        text2 = _quick_chat(api_key, api_url, api_model, messages + [
            {"role": "assistant", "content": _clip(text, 4000)},
            {"role": "user", "content": "上面的输出不是合法 JSON。请只输出一个合法 JSON 对象，"
                                        "不要代码围栏、不要解释、不要前后缀。"},
        ], max_tokens=max_tokens, source=source)
    except Exception as e:                                  # noqa: BLE001
        print("[面试] 模型纠正重试失败：%s" % e)
        return None
    return extract_json(text2)


# ---------------- 简历：文本统计与本地兜底 ----------------

_SEC_KEYS = [
    ("教育", ["教育", "学历", "学校", "专业", "GPA", "education"]),
    ("实习/工作", ["实习", "工作经历", "任职", "experience", "intern"]),
    ("项目", ["项目", "project"]),
    ("技能", ["技能", "skill", "技术栈"]),
    ("奖项/证书", ["奖", "证书", "荣誉", "award", "certificate"]),
]

_ACTION_WORDS = ["负责", "主导", "搭建", "设计", "优化", "实现", "推动", "落地", "提升",
                 "降低", "增长", "解决", "独立", "协调", "带领",
                 "led", "built", "designed", "optimized", "improved", "launched", "owned"]

_SKILL_WORDS = [
    "Python", "Java", "JavaScript", "TypeScript", "Go", "C++", "C#", "SQL", "Linux",
    "React", "Vue", "Angular", "Node", "Spring", "Django", "Flask", "FastAPI",
    "MySQL", "PostgreSQL", "Redis", "MongoDB", "Kafka", "Docker", "Kubernetes",
    "Git", "AWS", "Azure", "Excel", "Tableau", "PowerBI",
    "数据分析", "机器学习", "深度学习", "算法", "前端", "后端", "测试", "运维",
    "产品", "运营", "项目管理", "沟通", "团队协作", "英语",
]


def resume_stats(text):
    """简历的可计算指标：用于本地兜底评分，也让前端能展示字数等"""
    text = str(text or "")
    plain = re.sub(r"\s+", "", text)
    chars = len(plain)
    numbers = len(re.findall(r"\d+(?:\.\d+)?\s*%?", text))
    actions = sum(1 for w in _ACTION_WORDS if w.lower() in text.lower())
    sections = [name for name, keys in _SEC_KEYS
                if any(k.lower() in text.lower() for k in keys)]
    bullets = len(re.findall(r"(?m)^\s*[-*•·]\s+", text))
    skills = [w for w in _SKILL_WORDS if w.lower() in text.lower()]
    return {"chars": chars, "numbers": numbers, "action_words": actions,
            "sections": sections, "bullets": bullets, "skills": skills}


def _local_resume_report(text, target_role=""):
    """无模型时的规则版简历报告：不编造，只指出可验证的问题"""
    st = resume_stats(text)
    issues = []

    completeness = min(100, 30 + len(st["sections"]) * 14)
    if len(st["sections"]) < 3:
        issues.append({"severity": "high", "section": "整体结构",
                       "quote": "",
                       "problem": "简历缺少明确的板块（教育 / 经历 / 项目 / 技能），招聘方很难快速定位信息。",
                       "fix": "按「基本信息 → 教育 → 实习/项目 → 技能 → 奖项」的顺序分块，每块加小标题。"})
    structure = min(100, 35 + st["bullets"] * 5)
    if st["bullets"] < 4:
        issues.append({"severity": "medium", "section": "排版",
                       "quote": "",
                       "problem": "要点句偏少，经历容易写成大段叙述。",
                       "fix": "每段经历拆成 2-4 条以动词开头的要点，一条只讲一件事。"})
    impact = min(100, 25 + st["numbers"] * 8 + st["action_words"] * 3)
    if st["numbers"] < 3:
        issues.append({"severity": "high", "section": "成果量化",
                       "quote": "",
                       "problem": "几乎看不到数字，成果难以判断。",
                       "fix": "给每条经历补一个可核实的数字：规模、时长、效率提升、用户量、准确率等。"})
    match = 55
    if target_role:
        hits = [w for w in _SKILL_WORDS if w.lower() in target_role.lower()
                and w.lower() in text.lower()]
        match = min(95, 45 + len(hits) * 12)
    expression = min(100, 40 + st["action_words"] * 5)
    if st["chars"] < 300:
        issues.append({"severity": "high", "section": "篇幅",
                       "quote": "",
                       "problem": "内容过短，无法支撑一次完整筛选。",
                       "fix": "至少写满一页：教育、2 段经历/项目、技能与成果。"})
    elif st["chars"] > 4000:
        issues.append({"severity": "medium", "section": "篇幅",
                       "quote": "",
                       "problem": "内容偏长，重点不够集中。",
                       "fix": "压缩到 1-2 页，删掉与目标岗位无关的经历细节。"})

    dims = [
        {"name": "完整度", "score": completeness, "comment": "按可识别的板块数量估算。"},
        {"name": "结构清晰", "score": structure, "comment": "按要点句数量估算。"},
        {"name": "成果量化", "score": impact, "comment": "按数字与动词密度估算。"},
        {"name": "岗位匹配", "score": match, "comment": "按目标岗位关键词命中估算。"},
        {"name": "表达专业", "score": expression, "comment": "按动作词使用情况估算。"},
    ]
    overall = int(round(sum(d["score"] for d in dims) / len(dims)))
    highlights = []
    if st["sections"]:
        highlights.append("已包含板块：" + "、".join(st["sections"]))
    if st["numbers"]:
        highlights.append("检测到 %d 处数字/量化表达" % st["numbers"])
    if st["skills"]:
        highlights.append("可识别的技能关键词：" + "、".join(st["skills"][:10]))
    return {
        "overall": overall,
        "dimensions": dims,
        "summary": "这是本地规则版分析（未连接聊天模型）：先按结构与量化补齐，再让 AI 做逐条润色。",
        "highlights": highlights,
        "issues": issues[:10],
        "missing": [],
        "keywords": st["skills"][:12],
        "stats": st,
        "degraded": True,
    }


def normalize_resume_report(obj):
    """把模型输出收敛成前端固定结构"""
    dims_raw = obj.get("dimensions") if isinstance(obj, dict) else None
    dims = []
    pool = _as_list(dims_raw)
    for i, name in enumerate(RESUME_DIMENSIONS):
        item = pool[i] if i < len(pool) and isinstance(pool[i], dict) else {}
        named = next((d for d in pool if isinstance(d, dict)
                      and _s(d.get("name") or d.get("dimension")) == name), None)
        if isinstance(named, dict):
            item = named
        dims.append({
            "name": name,
            "score": _score100(item.get("score"), 60),
            "comment": _s(item.get("comment") or item.get("advice") or item.get("reason"))[:300],
        })
    issues = []
    for it in _as_list(obj.get("issues"))[:12]:
        if not isinstance(it, dict):
            continue
        sev = _s(it.get("severity") or it.get("level")).lower()
        issues.append({
            "severity": sev if sev in ("high", "medium", "low") else "medium",
            "section": _s(it.get("section") or it.get("where"))[:60],
            "quote": _s(it.get("quote") or it.get("original"))[:300],
            "problem": _s(it.get("problem") or it.get("issue"))[:400],
            "fix": _s(it.get("fix") or it.get("suggestion") or it.get("action"))[:500],
        })
    overall = _score100(obj.get("overall") or obj.get("score"),
                        int(round(sum(d["score"] for d in dims) / len(dims))))
    return {
        "overall": overall,
        "dimensions": dims,
        "summary": _s(obj.get("summary") or obj.get("comment"))[:800],
        "highlights": _text_list(obj.get("highlights"), 8, 300),
        "issues": issues,
        "missing": _text_list(obj.get("missing"), 8, 200),
        "keywords": _text_list(obj.get("keywords"), 16, 40),
        "stats": resume_stats(""),
        "degraded": False,
    }


# ---------------- 简历：三个 AI 能力 ----------------

_ANALYZE_SYSTEM = (
    "你是一位有 10 年招聘经验的简历顾问，服务对象是职场新人。"
    "请严格依据用户给出的简历原文分析，不要臆测或编造简历里没有的信息。"
    "评分维度固定为：完整度、结构清晰、成果量化、岗位匹配、表达专业（各 0-100）。"
    "问题必须可定位：能引用原文就引用；quote 找不到对应原文时留空字符串。"
    "只输出 JSON 对象，不要 Markdown 代码围栏，不要任何解释。JSON 结构："
    '{"overall":0-100,"dimensions":[{"name":"完整度","score":0-100,"comment":""},'
    '{"name":"结构清晰","score":0-100,"comment":""},{"name":"成果量化","score":0-100,"comment":""},'
    '{"name":"岗位匹配","score":0-100,"comment":""},{"name":"表达专业","score":0-100,"comment":""}],'
    '"summary":"两三句话总评","highlights":["亮点"],'
    '"issues":[{"severity":"high|medium|low","section":"板块","quote":"原文",'
    '"problem":"为什么是问题","fix":"具体怎么改"}],'
    '"missing":["缺失的关键信息"],"keywords":["岗位相关关键词"]}'
)


def analyze_resume(resume_text, target_role="", jd=""):
    text = _clip(resume_text, MAX_RESUME).strip()
    if len(text) < 50:
        raise InterviewError("简历内容太短，至少粘贴 50 个字符")
    role = _clip(target_role, 120)
    jd_text = _clip(jd, MAX_JD)
    user = ("目标岗位：%s\n\n岗位 JD（可能为空）：\n%s\n\n简历原文：\n%s"
            % (role or "未填写", jd_text or "（无）", text))
    obj = _llm_json(_ANALYZE_SYSTEM, user, max_tokens=2200, source="interview_resume")
    if isinstance(obj, dict):
        out = normalize_resume_report(obj)
        out["stats"] = resume_stats(text)
        return out
    return _local_resume_report(text, role)


_BUILD_SYSTEM = (
    "你是简历撰写教练，帮助职场新人把真实经历整理成一份可投递的简历。"
    "硬性规则："
    "1) 只能使用用户明确提供的事实；缺失的公司名、时间、数字一律不要编造，"
    "数字缺失时在该条末尾写「（量级待补）」，不要编；"
    "2) 只写一页，按相关度排序：最相关的经历 → 项目 → 教育 → 技能；"
    "3) 每条经历 1-2 行，统一写成「动作 + 方法/工具 + 结果」，"
    "去掉「认真负责、学习能力强」这类空泛描述；"
    "4) 有目标岗位 / JD 时，优先保留与之相关的证据，按岗位关键词排序；"
    "5) 结构化事实优先于访谈原文，两者冲突时以事实为准。"
    "输出 Markdown 简历（不要代码围栏），并给出最多 5 个补齐关键信息的追问。"
    "只输出 JSON 对象，不要解释。JSON 结构："
    '{"resume_markdown":"完整简历 Markdown","followups":["具体问题"],'
    '"gaps":[{"section":"projects","need":"缺结果数字"}],'
    '"used_facts":["用到的用户事实"],"warnings":["需要用户确认或补充的地方"]}'
    "gaps 的 section 取值：basic / education / experience / projects / skills / extra。"
)


def _facts_text(facts):
    """把结构化事实按板块拼成给模型看的清单"""
    facts = _normalize_facts(facts)
    lines = []
    for key in GUIDE_SECTIONS:
        vals = facts.get(key) or []
        if vals:
            lines.append("[" + _GUIDE_TITLES[key] + "]")
            lines.extend("- " + v for v in vals)
    return "\n".join(lines)


def _normalize_gaps(v, followups=None):
    """统一追问结构：gaps 为空时用 followups 兜底，保证前端总有可定位的缺口"""
    out = []
    for it in _as_list(v)[:6]:
        if isinstance(it, dict):
            need = _s(it.get("need") or it.get("text") or it.get("question"))[:200]
            if not need:
                continue
            sec = _s(it.get("section"))
            out.append({"section": sec if sec in GUIDE_SECTIONS else "extra", "need": need})
        else:
            s = _s(it)[:200]
            if s:
                out.append({"section": "extra", "need": s})
    if not out:
        for s in _text_list(followups, 5, 200):
            out.append({"section": "extra", "need": s})
    return out[:6]


def _local_gaps(profile, facts, skipped, qa=None):
    cov = _guide_coverage(profile, _merge_facts(facts, _qa_facts(qa)))
    out = []
    if cov.get("basic", 0) < 60:
        out.append({"section": "basic", "need": _GUIDE_NEEDS["basic"]})
    if cov.get("education", 0) < 40:
        out.append({"section": "education", "need": _GUIDE_NEEDS["education"]})
    if max(cov.get("experience", 0), cov.get("projects", 0)) < 70:
        out.append({"section": "projects", "need": _GUIDE_NEEDS["projects"]})
    if cov.get("skills", 0) < 50:
        out.append({"section": "skills", "need": _GUIDE_NEEDS["skills"]})
    for q in skipped:
        out.append({"section": "extra", "need": "你跳过了这题，可以补充：" + q})
    return out[:6]


def _qa_text(qa, limit=30, each=1200):
    """把访谈问答压成可读文本（给模型和本地兜底共用）"""
    lines = []
    for it in _as_list(qa)[:limit]:
        if not isinstance(it, dict):
            continue
        q = _s(it.get("q") or it.get("question"))[:400]
        a = _s(it.get("a") or it.get("answer"))[:each]
        if q or a:
            lines.append("问：" + q + "\n答：" + (a or "（未回答）"))
    return "\n\n".join(lines)


_SKIP_ANSWER = "（跳过这题）"


def _skipped_questions(qa):
    """从问答记录里挑出被跳过的问题：前端跳过时答案是固定标记。"""
    out = []
    for it in _as_list(qa):
        if not isinstance(it, dict):
            continue
        a = _s(it.get("a") or it.get("answer")).strip()
        q = _s(it.get("q") or it.get("question"))[:400]
        if q and _SKIP_ANSWER in a:
            out.append(q)
    return out


def _with_skipped(followups, skipped):
    """把跳过的主题并进 followups，保证结果页一定看得到需要补什么。"""
    out = list(followups or [])
    for q in skipped:
        item = "你跳过了这题，可以补充：" + q
        if item not in out:
            out.append(item)
    return out[:8]


def build_resume(profile, target_role="", jd="", qa=None, facts=None):
    facts = _normalize_facts(facts)
    facts_text = _facts_text(facts)
    if isinstance(profile, str):
        profile_text = _clip(profile, MAX_PROFILE)
    else:
        try:
            profile_text = json.dumps(profile or {}, ensure_ascii=False, indent=2)
        except Exception:
            profile_text = _clip(str(profile or ""), MAX_PROFILE)
    qa_text = _qa_text(qa, 20, 900)
    skipped = _skipped_questions(qa)
    if len((profile_text + "\n" + qa_text + "\n" + facts_text).strip()) < 20:
        raise InterviewError("请先填写一些真实信息（教育、经历、项目、技能）")
    role = _clip(target_role, 120)
    user = ("目标岗位：%s\n\n岗位 JD（可能为空）：\n%s\n\n用户提供的事实（JSON 或自由文本）：\n%s"
            % (role or "未填写", _clip(jd, 5000) or "（无）",
               _clip(profile_text, 8000)))
    if facts_text:
        user += ("\n\n结构化事实（优先使用；这里已经是用户逐条确认过的内容，"
                 "与访谈原文冲突时以此为准）：\n" + _clip(facts_text, 8000))
    if qa_text:
        user += "\n\n访谈原文（只作补充，不要照抄口语）：\n" + qa_text
    if skipped:
        user += ("\n\n用户跳过了这些问题，没有提供任何信息，绝对不要替对方编造。"
                 "请在 followups 里列出这些需要补充的主题：\n"
                 + "\n".join("- " + q for q in skipped))
    obj = _llm_json(_BUILD_SYSTEM, user, max_tokens=3000, source="interview_build")
    md = ""
    if isinstance(obj, dict):
        md = _s(obj.get("resume_markdown") or obj.get("markdown"))
        if md:
            followups = _with_skipped(_text_list(obj.get("followups"), 5, 240), skipped)
            return {
                "resume_markdown": md,
                "followups": followups,
                "gaps": _normalize_gaps(obj.get("gaps"), followups),
                "used_facts": _text_list(obj.get("used_facts"), 20, 200),
                "warnings": _text_list(obj.get("warnings"), 8, 240),
                "degraded": False,
            }
    # 本地兜底：按六个栏目拼结构化简历，只搬用户写下的原话，不新增任何事实
    source = profile if isinstance(profile, dict) else profile_text
    out = {
        "resume_markdown": _local_resume_markdown(source, role, qa, facts),
        "followups": _with_skipped([
            "每段经历分别做出了什么可量化的结果（数字 / 比例 / 规模）？",
            "你在每段经历里具体负责哪一部分，团队还有谁？",
            "目标岗位最看重的 2-3 项技能，你有对应的项目证据吗？",
        ], skipped),
        "gaps": _local_gaps(profile if isinstance(profile, dict) else {},
                            facts, skipped, qa),
        "used_facts": [],
        "warnings": ["未连接聊天模型：这一版只是把栏目内容整理成简历结构，"
                     "数字、时间与措辞请自行核对。"],
        "degraded": True,
    }
    if isinstance(obj, dict):
        # 模型返回了对象却没给正文：别把空简历交给前端
        out["warnings"].insert(0, "模型没有返回简历正文，已按你填写的栏目整理了一版。")
    return out


_RESUME_SECTIONS = [
    ("education", "教育背景"),
    ("experience", "实习 / 工作经历"),
    ("projects", "项目经历"),
    ("skills", "技能与证书"),
]


def _local_resume_markdown(profile, role="", qa=None, facts=None):
    """无模型时的简历：六个栏目拼成结构化 Markdown。

    profile 可以是六个栏目的 dict，也可以是自由文本（旧调用 / 直接粘贴）；
    自由文本统一放进「补充信息」，不做任何改写。facts 会按板块并入对应栏目。
    """
    if isinstance(profile, dict):
        data = {k: _s(profile.get(k)) for k in
                ("basic", "education", "experience", "projects", "skills", "extra")}
    else:
        data = {"basic": "", "education": "", "experience": "",
                "projects": "", "skills": "", "extra": _s(profile)}

    facts = _normalize_facts(facts)
    for key in data:
        items = facts.get(key) or []
        if not items:
            continue
        extra_lines = "\n".join("- " + x for x in items)
        data[key] = (data[key].rstrip() + "\n" + extra_lines).strip() if data[key] else extra_lines

    lines = ["# 简历" + ("｜" + role if role else ""), ""]
    if role:
        lines += ["## 求职意向", "", role, ""]
    if data["basic"]:
        lines += ["## 基本信息", ""] + data["basic"].splitlines() + [""]
    for key, title in _RESUME_SECTIONS:
        body = data[key]
        if not body:
            continue
        lines += ["## " + title, ""]
        for line in body.splitlines():
            s = line.strip()
            if not s:
                continue
            lines.append(s if s[:1] in "-*•·" else "- " + s)
        lines.append("")

    qa_lines = []
    for it in _as_list(qa):
        if not isinstance(it, dict):
            continue
        a = _s(it.get("a") or it.get("answer"))
        q = _s(it.get("q") or it.get("question"))[:200]
        if not a or _SKIP_ANSWER in a:
            continue
        if q:
            qa_lines += ["**" + q + "**", ""]
        qa_lines += [a, ""]

    extra = data["extra"]
    if extra or qa_lines:
        lines += ["## 补充信息", ""]
        if extra:
            lines += [extra, ""]
        lines += qa_lines

    if not any(data[k] for k in ("basic", "education", "experience", "projects", "skills")):
        lines += ["> 目前只有零散信息。先把教育、项目、技能写成要点句，再生成一版。", ""]
    return "\n".join(lines).rstrip() + "\n"


# ---------------- 简历原文 → 六个栏目 ----------------

_IMPORT_SYSTEM = (
    "你是简历信息整理助手。把用户粘贴的简历原文拆进六个固定栏目，"
    "只搬运原文里已有的事实：不要改写、不要补全、不要编造；原文没有的栏目留空字符串。"
    "栏目说明：basic=姓名/城市/联系方式/求职意向；education=学校/专业/时间/GPA/课程；"
    "experience=实习或工作；projects=项目；skills=技能/证书/奖项；extra=其他。"
    "每栏保留原文的换行与要点（一行一条，保留 - 前缀）。"
    "只输出 JSON 对象，不要解释。JSON 结构："
    '{"basic":"","education":"","experience":"","projects":"","skills":"","extra":"",'
    '"warnings":["需要用户自己核对或补充的地方"]}'
)

_PROFILE_KEYS = ("basic", "education", "experience", "projects", "skills", "extra")

# 小标题 → 栏目。只做精确匹配，避免把「获校级一等奖学金」这类正文当成标题吃掉。
_IMPORT_TITLES = {
    "基本信息": "basic", "个人信息": "basic", "联系方式": "basic", "个人资料": "basic",
    "教育": "education", "教育背景": "education", "教育经历": "education",
    "学历": "education", "学习经历": "education",
    "实习": "experience", "实习经历": "experience", "工作经历": "experience",
    "工作履历": "experience", "职业经历": "experience", "实践经历": "experience",
    "项目": "projects", "项目经历": "projects", "项目经验": "projects", "科研经历": "projects",
    "技能": "skills", "专业技能": "skills", "技能证书": "skills", "技能与证书": "skills",
    "证书": "skills", "奖项": "skills", "荣誉": "skills", "获奖情况": "skills",
    "荣誉奖项": "skills", "获奖": "skills",
}


def _section_key(line):
    """判断这一行是不是板块小标题：去装饰后精确命中才认。"""
    probe = re.sub(r"^[#\s>*\-•·]+|[:：\s]+$", "", str(line or ""))
    probe = re.sub(r"^[一二三四五六七八九十\d]+[、.．)]\s*", "", probe)
    probe = re.sub(r"[（(][^）)]{0,12}[）)]$", "", probe).strip()
    return _IMPORT_TITLES.get(probe)


def _local_import_profile(text):
    """无模型时按小标题把简历原文切进栏目；切不进去的放 extra。"""
    data = {k: [] for k in _PROFILE_KEYS}
    current = "extra"
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        key = _section_key(line)
        if key:
            current = key
            continue
        # 「技能：Python / Vue」这种一行式：标题 + 冒号 + 内容，直接归到对应栏目
        m = re.match(r"^([^：:]{2,10})[：:]\s*(.+)$", line)
        if m:
            k2 = _IMPORT_TITLES.get(
                re.sub(r"^[#\s>*\-•·]+", "", m.group(1)).strip())
            if k2:
                data[k2].append(m.group(2).strip())
                current = k2
                continue
        data[current].append(line)
    return {k: "\n".join(v).strip() for k, v in data.items()}


def _profile_text(v):
    """栏目值可能是字符串，也可能是模型给的字符串数组。"""
    if isinstance(v, list):
        out = []
        for item in v[:30]:
            if isinstance(item, dict):
                item = item.get("text") or item.get("content") or ""
            s = _s(item)
            if s:
                out.append(s)
        return "\n".join(out)
    return _s(v)


def extract_profile(resume_text):
    """把一份已有简历拆进六个栏目，供「快速填写 / AI 访谈」继续补全。"""
    text = _clip(resume_text, MAX_RESUME).strip()
    if len(text) < 50:
        raise InterviewError("简历内容太短，至少粘贴 50 个字符")
    obj = _llm_json(_IMPORT_SYSTEM, "简历原文：\n" + text,
                    max_tokens=2200, source="interview_import")
    if isinstance(obj, dict):
        profile = {k: _profile_text(obj.get(k))[:3000] for k in _PROFILE_KEYS}
        if any(profile.values()):
            return {"profile": profile,
                    "warnings": _text_list(obj.get("warnings"), 6, 120),
                    "degraded": False}
    return {"profile": _local_import_profile(text),
            "warnings": ["未连接聊天模型：按小标题拆分，结果需要你核对一遍。"],
            "degraded": True}


_POLISH_SYSTEM = (
    "你是简历润色师。只改措辞与结构，不新增事实、不虚构数字。"
    "逐条处理用户给出的简历要点：original 保留原文，rewritten 给出改写，"
    "reason 用一句话说明改了什么（动词、量化、结构、去冗余）。"
    "若原文没有数字，不要替用户编造，可在 warnings 里提醒补充。"
    "只输出 JSON 对象，不要解释。JSON 结构："
    '{"summary":"整体改写说明","bullets":[{"original":"","rewritten":"","reason":""}],'
    '"resume_markdown":"应用改写后的完整简历 Markdown","warnings":[""]}'
)


def polish_resume(resume_text, target_role="", mode="impact"):
    text = _clip(resume_text, MAX_RESUME).strip()
    if len(text) < 30:
        raise InterviewError("简历内容太短，至少粘贴 30 个字符")
    mode_name = {
        "impact": "强化成果量化（补数字、补结果，但不许编造）",
        "concise": "精简去冗余，一条只讲一件事",
        "professional": "换成更专业的书面表达",
        "star": "按 STAR（情境-任务-行动-结果）展开",
    }.get(str(mode or ""), "强化成果量化")
    user = ("目标岗位：%s\n润色模式：%s\n\n简历原文：\n%s"
            % (_clip(target_role, 120) or "未填写", mode_name, text))
    obj = _llm_json(_POLISH_SYSTEM, user, max_tokens=3000, source="interview_polish")
    if isinstance(obj, dict):
        bullets = []
        for it in _as_list(obj.get("bullets"))[:30]:
            if not isinstance(it, dict):
                continue
            bullets.append({
                "original": _s(it.get("original"))[:500],
                "rewritten": _s(it.get("rewritten") or it.get("after"))[:800],
                "reason": _s(it.get("reason"))[:300],
            })
        return {
            "summary": _s(obj.get("summary"))[:600],
            "bullets": bullets,
            "resume_markdown": _s(obj.get("resume_markdown") or obj.get("markdown")),
            "warnings": _text_list(obj.get("warnings"), 8, 240),
            "degraded": False,
        }
    return {
        "summary": "未连接聊天模型：下面按本地规则给出可执行的润色方向。",
        "bullets": _local_polish_bullets(text),
        "resume_markdown": "",
        "warnings": ["AI 润色不可用时不会改写原文，请参考每条建议手动调整。"],
        "degraded": True,
    }


def _local_polish_bullets(text):
    """从原文里挑出要点句，给出不改变事实的修改建议"""
    out = []
    for line in str(text or "").splitlines():
        s = line.strip().lstrip("-*•· ").strip()
        if len(s) < 8:
            continue
        reason = []
        if not re.search(r"\d", s):
            reason.append("补一个可核实的数字（规模 / 比例 / 时长 / 结果）")
        if not any(w in s for w in _ACTION_WORDS):
            reason.append("用动词开头（负责 / 搭建 / 优化 / 推动）")
        if len(s) > 120:
            reason.append("一句话只讲一件事，超过 120 字就拆分")
        if not reason:
            reason.append("保留事实，可再压缩修饰语")
        out.append({"original": s[:500], "rewritten": "", "reason": "；".join(reason)})
        if len(out) >= 12:
            break
    return out


# ---------------- JD 匹配 ----------------

_JD_SYSTEM = (
    "你是招聘顾问。对照岗位 JD 审核简历，判断关键词与能力的覆盖情况。"
    "只能依据简历原文举证，不要编造经历。"
    "只输出 JSON 对象，不要解释。JSON 结构："
    '{"match_score":0-100,"summary":"","covered":[{"keyword":"","evidence":"简历原文"}],'
    '"missing":[{"keyword":"","suggestion":"怎么补"}],"questions":["针对该岗位可能被问到的问题"]}'
)


def match_jd(resume_text, jd, target_role=""):
    text = _clip(resume_text, MAX_RESUME).strip()
    jd_text = _clip(jd, MAX_JD).strip()
    if len(text) < 50:
        raise InterviewError("请先提供简历内容")
    if len(jd_text) < 20:
        raise InterviewError("请粘贴完整的岗位 JD")
    user = "目标岗位：%s\n\n岗位 JD：\n%s\n\n简历原文：\n%s" % (
        _clip(target_role, 120) or "未填写", jd_text, text)
    obj = _llm_json(_JD_SYSTEM, user, max_tokens=1800, source="interview_jd")
    if isinstance(obj, dict):
        covered = []
        for it in _as_list(obj.get("covered"))[:20]:
            if isinstance(it, dict):
                covered.append({"keyword": _s(it.get("keyword"))[:60],
                                "evidence": _s(it.get("evidence"))[:300]})
        missing = []
        for it in _as_list(obj.get("missing"))[:20]:
            if isinstance(it, dict):
                missing.append({"keyword": _s(it.get("keyword"))[:60],
                                "suggestion": _s(it.get("suggestion"))[:300]})
        return {"match_score": _score100(obj.get("match_score") or obj.get("score"), 60),
                "summary": _s(obj.get("summary"))[:600],
                "covered": covered, "missing": missing,
                "questions": _text_list(obj.get("questions"), 10, 240),
                "degraded": False}
    return _local_jd_match(text, jd_text)


def _local_jd_match(resume_text, jd_text):
    jd_skills = [w for w in _SKILL_WORDS if w.lower() in jd_text.lower()]
    if not jd_skills:
        jd_skills = list(dict.fromkeys(re.findall(r"[A-Za-z][A-Za-z0-9+#.]{1,20}", jd_text)))[:12]
    covered, missing = [], []
    low = resume_text.lower()
    for w in jd_skills:
        if w.lower() in low:
            covered.append({"keyword": w, "evidence": "简历中出现该关键词"})
        else:
            missing.append({"keyword": w,
                            "suggestion": "如果确有相关经历，补一条项目证据；没有就不要硬写。"})
    total = max(1, len(jd_skills))
    score = int(round(100 * len(covered) / total)) if jd_skills else 50
    return {"match_score": max(20, min(95, score)),
            "summary": "本地关键词比对结果（未连接聊天模型）。",
            "covered": covered, "missing": missing,
            "questions": [], "degraded": True}


# ---------------- AI 访谈：简历引导 / 职业盘点 ----------------

GUIDE_SECTIONS = ["basic", "education", "experience", "projects", "skills", "extra"]
MAX_GUIDE_STEPS = 10

_GUIDE_TITLES = {
    "basic": "基本信息",
    "education": "教育背景",
    "experience": "实习 / 工作经历",
    "projects": "项目经历",
    "skills": "技能与证书",
    "extra": "其他信息",
}

_GUIDE_NEEDS = {
    "basic": "补姓名、城市、联系方式和求职意向",
    "education": "补学校、专业、就读时间；有 GPA 或排名更好",
    "experience": "补一段实习 / 兼职：你负责什么 → 用什么方法 → 做出什么结果",
    "projects": "补一段项目：你负责什么 → 用什么方法 → 做出什么结果",
    "skills": "补技能，并各配一句「用它做过什么」",
}

# 题库按「整块经历」设计：一次问负责什么 → 怎么做 → 结果如何，
# 避免把一段经历拆成三轮，问得又多又碎。
_RESUME_GUIDE_BANK = [
    {"field": "role",
     "question": "这份简历准备投什么岗位或方向（比如数据分析、后端开发、产品运营）？",
     "why": "岗位决定简历筛什么、按什么顺序写。",
     "examples": ["数据分析", "后端开发", "产品运营", "还没定"]},
    {"field": "basic",
     "question": "先补抬头：你的姓名、所在城市、联系方式，以及求职意向是什么？",
     "why": "抬头不完整，HR 没法联系你。",
     "examples": ["张三，深圳，138xxxx，求职方向：数据分析"]},
    {"field": "education",
     "question": "教育背景说一下：学校、专业、就读时间，有 GPA、排名或奖学金吗？",
     "why": "应届生的教育背景是重要证据。",
     "examples": ["深圳大学 数学与应用数学 2023.09-2027.06，GPA 3.6/4.0",
                  "专业前 10%，拿过校级奖学金"]},
    {"field": "experience",
     "question": "挑一段最拿得出手的经历（实习、课程项目、竞赛、社团都算），"
                 "一句话说清：你负责什么 → 用什么方法 → 做出什么结果？",
     "why": "一段完整经历胜过十句自我评价。",
     "examples": ["我负责清洗数据，用 Python 处理 3 万条记录，把整理时间从 2 小时降到 20 分钟",
                  "我在竞赛里做建模，负责模型和论文，拿了省三"]},
    {"field": "experience",
     "question": "这段经历有什么数字或规模吗（人数、时长、准确率、排名都行，"
                 "记不清就说个大概量级）？",
     "why": "数字决定这条经历可信不可信。",
     "examples": ["大约几十人", "准确率六成左右", "一周左右", "确实没有数字"]},
    {"field": "projects",
     "question": "除了刚才那段，还有没有课程设计、毕设或自己做的作品？"
                 "一句话说清做了什么、你负责哪部分。",
     "why": "项目能证明你会做什么，课程名本身没有说服力。",
     "examples": ["做过成绩表处理脚本，用 pandas 按学号排序并导出 Excel",
                  "毕设做了一个推荐系统，负责特征和评估"]},
    {"field": "skills",
     "question": "你实际用过哪些工具或技能？每个配一句「用它做过什么」。",
     "why": "技能栏要真实，面试会追问。",
     "examples": ["Python：写过数据清洗脚本", "Excel：做过成绩统计", "SQL：课程里查过数据"]},
    {"field": "extra",
     "question": "还有没有奖项、证书、志愿活动、班级工作或自学作品？",
     "why": "这些能补足经历空白。",
     "examples": ["数学建模省三", "CET-6", "班级学习委员"]},
    {"field": "education",
     "question": "教育里还有能加分的吗：核心课程、课程成绩、毕业设计或论文题目？",
     "why": "课程和成绩能证明你和岗位相关。",
     "examples": ["主修数学分析、高等代数、Python 程序设计", "专业课平均 85"]},
    {"field": "skills",
     "question": "目标岗位最看重的 2-3 项能力，你有对应的例子吗？没有就说说你最强的一项。",
     "why": "把能力对上岗位要求，简历才有针对性。",
     "examples": ["Python 数据分析", "沟通协作", "自学能力"]},
]

_CAREER_GUIDE_BANK = [
    {"question": "先介绍一下：你的专业、年级、学校，以及目前最困惑的是什么？",
     "why": "先确定你站在哪里。",
     "examples": ["数学专业大四，不知道投什么", "计算机应届，想做技术但怕卷", "不知道自己能做什么"]},
    {"question": "过去一年里，做什么事时你会比较投入，甚至忘记时间？",
     "why": "兴趣往往藏在注意力里。",
     "examples": ["写代码 / 做数据分析", "写文章 / 做视频", "和人聊天、组织活动", "解数学题"]},
    {"question": "有没有什么事是你明显不喜欢、做起来很消耗的？",
     "why": "排除项和喜欢同样重要。",
     "examples": ["不喜欢频繁应酬", "不喜欢长时间对着表格", "不喜欢纯销售"]},
    {"question": "别人常找你帮什么忙？这通常是你相对擅长的地方。",
     "why": "外部反馈比自我评价更可靠。",
     "examples": ["帮同学讲题", "帮忙修电脑 / 做表格", "组织活动", "写文案"]},
    {"question": "你更看重工作的哪一面：成长、稳定、薪资、兴趣、城市、工作强度？可以排序。",
     "why": "价值观决定方向取舍。",
     "examples": ["成长 > 薪资 > 稳定", "稳定 > 城市 > 兴趣", "先赚钱再谈兴趣"]},
    {"question": "你能接受的城市、行业或岗位类型有哪些？有没有明确不能接受的？",
     "why": "现实约束会直接排除一些方向。",
     "examples": ["只想留深圳", "可以接受出差", "不考虑销售岗"]},
    {"question": "你目前已有的技能、证书、经历有哪些？包括课程、项目、兼职。",
     "why": "方向要建立在已有积累上。",
     "examples": ["Python、SQL", "教师资格证", "社团运营", "家教经历"]},
    {"question": "如果三年后回头看，你希望自己已经拥有了什么？",
     "why": "把模糊的愿望变成可验证的目标。",
     "examples": ["能独立负责一个项目", "有一份稳定的工作", "攒到第一桶金", "成为某个领域的高手"]},
    {"question": "有没有现实约束，比如经济压力、家庭期待、时间限制？",
     "why": "规划必须能执行。",
     "examples": ["需要尽快赚钱", "家里希望考编", "只能边工作边准备"]},
]


def _normalize_qa(qa):
    out = []
    for it in _as_list(qa)[:30]:
        if not isinstance(it, dict):
            continue
        q = _s(it.get("q") or it.get("question"))[:500]
        a = _s(it.get("a") or it.get("answer"))[:2000]
        f = _s(it.get("f") or it.get("field"))[:20]
        if q or a:
            out.append({"q": q, "a": a, "f": f})
    return out


def _qa_facts(qa):
    """每题问的是哪个板块，就把它归到哪个板块：没接模型时进度也能动"""
    out = {}
    for it in _as_list(qa):
        if not isinstance(it, dict):
            continue
        field = _s(it.get("f"))
        a = _s(it.get("a"))
        if field in GUIDE_SECTIONS and a and _SKIP_ANSWER not in a:
            out.setdefault(field, []).append(a[:300])
    return out


def _normalize_facts(v):
    """把模型抽出的结构化事实统一成 {板块: [要点句]}"""
    out = {}
    if not isinstance(v, dict):
        return out
    for key in GUIDE_SECTIONS:
        raw = v.get(key)
        if isinstance(raw, str):
            items = [raw]
        elif isinstance(raw, (list, tuple)):
            items = list(raw)
        else:
            continue
        vals = []
        for it in items[:8]:
            if isinstance(it, dict):
                it = it.get("text") or it.get("content") or ""
            s = _s(it)[:300]
            if s and s not in vals:
                vals.append(s)
        if vals:
            out[key] = vals
    return out


def _merge_facts(a, b):
    """合并两批事实，按板块去重，每块最多 8 条"""
    out = {}
    for key in GUIDE_SECTIONS:
        vals = []
        for it in list((a or {}).get(key) or []) + list((b or {}).get(key) or []):
            s = _s(it)
            if s and s not in vals:
                vals.append(s)
        if vals:
            out[key] = vals[:8]
    return out


def _section_text(profile, facts, key):
    """表单内容 + 访谈抽出的事实，合成这一栏的文本"""
    parts = []
    val = (profile or {}).get(key)
    if isinstance(val, str):
        parts.append(val)
    elif isinstance(val, (list, tuple)):
        parts.extend(_s(x) for x in val)
    parts.extend((facts or {}).get(key) or [])
    return "\n".join(x for x in parts if x).strip()


def _guide_coverage(profile, facts=None):
    """按最小证据要求估算每个板块的覆盖度（0-100），用于进度条与收敛判断"""
    out = {}
    for key in GUIDE_SECTIONS:
        text = _section_text(profile, facts, key)
        n = len(text)
        if key == "basic":
            score = 100 if n >= 12 else (60 if n >= 6 else 0)
        elif key == "education":
            score = 40 if n >= 6 else 0
            if n >= 15:
                score += 30
            if re.search(r"GPA|绩点|排名|奖学金|课程", text, re.I):
                score += 30
        elif key in ("experience", "projects"):
            score = 40 if n >= 10 else 0
            if n >= 30:
                score += 30
            if re.search(r"\d", text):
                score += 30
        elif key == "skills":
            score = 100 if n >= 6 else (50 if n >= 3 else 0)
        else:
            score = 60 if n >= 4 else 0
        out[key] = min(100, score)
    return out


def _guide_missing(cov):
    out = []
    if cov.get("basic", 0) < 60:
        out.append(_GUIDE_NEEDS["basic"])
    if cov.get("education", 0) < 40:
        out.append(_GUIDE_NEEDS["education"])
    if max(cov.get("experience", 0), cov.get("projects", 0)) < 70:
        out.append(_GUIDE_NEEDS["projects"])
    if cov.get("skills", 0) < 50:
        out.append(_GUIDE_NEEDS["skills"])
    return out


def _guide_enough(cov):
    return (cov.get("basic", 0) >= 60
            and cov.get("education", 0) >= 40
            and max(cov.get("experience", 0), cov.get("projects", 0)) >= 70
            and cov.get("skills", 0) >= 50)


def _role_set(role):
    r = _s(role)
    if not r:
        return False
    return not (r in ("未填写", "不知道", "不确定", "未定", "无")
                or r.startswith("还没") or r.startswith("暂不"))


def _looks_like_question(q):
    q = _s(q)
    return len(q) >= 4 and (q.endswith("？") or q.endswith("?"))


def _local_guide(domain, step, profile, hint, facts=None, role=""):
    bank = _CAREER_GUIDE_BANK if domain == "career" else _RESUME_GUIDE_BANK
    index = max(0, min(step, len(bank)))
    if domain == "resume" and not hint:
        # 表单里已经填过的板块、已经问过的岗位不用重复问。游标要跟着一起前移，
        # 否则把跳过后的 index 当成已问轮数返回，下一轮会又问到同一题。
        while index < len(bank):
            field = bank[index].get("field")
            if field == "role" and _role_set(role):
                index += 1
            elif field and field != "role" and _section_text(profile, facts, field):
                index += 1
            else:
                break
    cov = _guide_coverage(profile, facts)
    missing = _guide_missing(cov)
    if index >= len(bank):
        return {"question": "", "why": "", "examples": [], "hint": "", "field": "",
                "done": True, "summary": "信息已经比较完整，可以生成简历了。",
                "step": index, "facts": {}, "coverage": cov, "missing": missing,
                "enough": _guide_enough(cov), "degraded": True}
    item = bank[index]
    examples = list(item.get("examples") or [])[:4]
    hint_text = ""
    if hint:
        hint_text = "可以从这些里挑一个，或者只说你记得的部分：" + "；".join(examples)
    return {"question": item["question"], "why": item["why"], "examples": examples,
            "hint": hint_text, "field": item.get("field") or "", "done": False, "summary": "",
            "step": step if hint else index + 1, "facts": {},
            "coverage": cov, "missing": missing, "enough": _guide_enough(cov),
            "degraded": True}


_GUIDE_SYSTEM = (
    "你是简历访谈教练。目标：用尽量少的问题，把用户真实经历变成可直接写进简历的素材。"
    "硬性规则："
    "1) 一次只问一个问题，必须是疑问句，20-60 字，以「？」结尾；"
    "2) 问题要围绕目标岗位需要什么证据，不要跟着上一句闲聊跑偏；"
    "3) 优先一次问一整段经历：负责什么 → 用什么方法 → 做出什么结果（数字/规模），"
    "不要把同一段经历拆成多轮；"
    "4) 不要复述用户原话，不要问「记不清也没关系」这类空话，绝对不要替用户编造事实；"
    "5) 用户说不知道 / 没有 / 想不起来时，给 2-3 条具体回忆线索，然后换板块，不要纠缠；"
    "6) 已覆盖的板块不要再问，优先补 missing 里最缺的；"
    "7) 用户已答 3 题以上且核心板块都有内容时，可以 done=true 结束。"
    "同时把用户最新回答里的事实抽成要点句，按板块放进 facts（只写用户说过的内容）。"
    "只输出 JSON 对象，不要解释。JSON 结构："
    '{"question":"下一个问题","section":"板块","examples":["可点的句式模板或量级选项"],'
    '"why":"为什么问","facts":{"projects":["从回答里抽出的要点句"]},'
    '"hint":"用户求助时的提示","done":false,"summary":""}'
    "section 取值：basic / education / experience / projects / skills / extra / role。"
)


def guide_turn(domain, payload):
    """AI 访谈的一步：返回下一个问题；hint=True 时只给提示，不换题。"""
    domain = "career" if domain == "career" else "resume"
    profile = payload.get("profile") if isinstance(payload.get("profile"), dict) else {}
    qa = _normalize_qa(payload.get("qa"))
    facts = _normalize_facts(payload.get("facts"))
    qa_facts = _qa_facts(qa)
    known = _merge_facts(facts, qa_facts)
    hint = bool(payload.get("hint"))
    skip = bool(payload.get("skip"))
    try:
        step = max(0, int(payload.get("step") or len(qa)))
    except (TypeError, ValueError):
        step = len(qa)
    # qa 里每条都是「已经问过一轮」：用它兜底，避免没接模型时本地题库反复问同一题
    step = max(step, len(qa))
    answered = len(qa)
    current = _clip(_s(payload.get("current_question")), 800)
    role = _clip(_s(payload.get("role")), 120)
    jd = _clip(_s(payload.get("jd")), 4000)
    focus = _clip(_s(payload.get("focus")), 800)

    local = _local_guide(domain, step, profile, hint, known, role)
    if hint and current:
        local["question"] = current          # 提示只给回忆线索，不换题
        local["field"] = _s(payload.get("current_field")) or local.get("field", "")

    # 第一题先把目标岗位定下来：岗位为空时不让模型自由发挥，否则后面全是无目标闲聊
    if domain == "resume" and not hint and not skip and answered == 0 and not _role_set(role):
        return local

    label = "职业规划" if domain == "career" else "简历"
    system = _GUIDE_SYSTEM + (" 当前领域：%s。" % label)
    if domain == "career":
        system += (" 用户可能是没有工作经历的应届生：不要问「最近这份工作」，"
                   "先从专业、年级、学校、课程项目和目前困惑入手。")
    else:
        system += (" 用户可能是没有实习经历的应届生：先从教育、课程项目、竞赛、"
                   "社团、兼职等入手，不要假设对方有工作经历。")
    user = ("访谈领域：%s\n目标岗位：%s\n岗位 JD：\n%s\n\n已有表单信息：\n%s\n\n"
            "已抽出的结构化事实：\n%s\n\n已问答记录：\n%s\n\n当前问题：%s\n"
            % (label, (role if _role_set(role) else "还没确定，按应届通用模板") if role else "未填写",
               jd or "（无）",
               json.dumps(profile, ensure_ascii=False)[:3000],
               _facts_text(facts) or "（暂无）",
               _qa_text(qa, 10, 600) or "（暂无）", current or "（尚未开始）"))
    if hint:
        user += "\n用户表示想不起来，请只给 2-3 条回忆线索或例子；question 必须保持当前问题不变。"
    elif skip:
        user += "\n用户跳过了当前问题。请换一个不同的问题（换方向或换角度），不要重复当前问题。"
    else:
        user += "\n请决定下一个问题，同时抽取这一轮回答里的事实。"
    if focus:
        user += ("\n这一轮访谈要优先补齐这个缺口（仍要一次只问一个问题）：\n" + focus)
    obj = _llm_json(system, user, max_tokens=800, source="interview_guide")
    if isinstance(obj, dict):
        new_facts = _normalize_facts(obj.get("facts"))
        merged = _merge_facts(_merge_facts(facts, new_facts), qa_facts)
        cov = _guide_coverage(profile, merged)
        missing = _guide_missing(cov)
        enough = _guide_enough(cov)
        done = bool(obj.get("done")) and not hint
        if answered + 1 >= MAX_GUIDE_STEPS:
            done = True
        if enough and answered + 1 >= 3:
            done = True
        raw_q = _s(obj.get("question"))[:400]
        if skip and raw_q and raw_q == current:
            # 模型仍然重复了被跳过的问题：用本地题库往前走一题，保证「跳过」有效
            return dict(local, facts=new_facts, coverage=cov,
                        missing=missing, enough=enough)
        q = raw_q
        if hint and current:
            q = current
        field = _s(obj.get("section") or obj.get("field"))
        if field not in GUIDE_SECTIONS and field != "role":
            field = ""
        if not _looks_like_question(q):
            # 模型给的是陈述句 / 空题：直接用本地题库，保证每题都能回答
            q = local["question"]
            field = local.get("field", "")
        if done:
            q = ""
        summary = _s(obj.get("summary"))[:400]
        if done and not summary:
            summary = "核心信息已经够了，可以先生成一版；剩下的缺口在结果页继续补。"
        return {
            "question": q,
            "why": _s(obj.get("why"))[:300],
            "examples": _text_list(obj.get("examples") or obj.get("options"), 4, 120),
            "field": field,
            "hint": _s(obj.get("hint"))[:800],
            "done": done,
            "summary": summary,
            "step": step if hint else step + 1,
            "facts": new_facts,
            "coverage": cov,
            "missing": missing,
            "enough": enough,
            "degraded": False,
        }
    return dict(local, facts={})


# ---------------- 职业生涯规划 ----------------

_CAREER_TEMPLATES = [
    {"name": "数据分析 / 商业分析", "one_liner": "用数据回答业务问题，做决策支持。",
     "keywords": ["数据", "统计", "sql", "excel", "python", "数学", "分析", "报表", "指标"],
     "entry_roles": ["数据分析师", "商业分析", "数据运营"],
     "skills": ["SQL", "Excel", "Python / pandas", "统计基础", "可视化"],
     "path": [("0-1 年", "独立完成取数、清洗、报表"),
              ("1-3 年", "能定义指标并推动业务决策"),
              ("3-5 年", "负责分析体系或带小团队")],
     "risks": ["容易停留在取数，需要主动理解业务", "对沟通和业务表达要求高于想象"],
     "verify": ["在招聘平台搜 10 个「数据分析师」JD，记录高频要求",
                "用公开数据做一份 1 页分析报告", "找 1 位从业者聊 30 分钟"]},
    {"name": "前端开发", "one_liner": "把产品需求变成可用的网页和交互。",
     "keywords": ["前端", "vue", "react", "javascript", "typescript", "页面", "ui", "css"],
     "entry_roles": ["前端开发工程师", "Web 开发", "小程序开发"],
     "skills": ["HTML / CSS / JavaScript", "Vue 或 React", "TypeScript", "构建工具", "Git"],
     "path": [("0-1 年", "独立完成页面与组件"),
              ("1-3 年", "负责模块设计、性能与工程化"),
              ("3-5 年", "负责前端架构或团队")],
     "risks": ["技术更新快，需要持续学习", "初级岗位竞争激烈，作品集很重要"],
     "verify": ["做 1 个可部署的小作品", "对照 10 个前端 JD 查缺口", "复刻一个常用组件"]},
    {"name": "后端 / 服务端开发", "one_liner": "设计接口、数据和系统，让业务稳定运行。",
     "keywords": ["后端", "java", "python", "go", "数据库", "接口", "服务", "linux", "并发"],
     "entry_roles": ["后端开发工程师", "服务端开发", "Java / Python 开发"],
     "skills": ["一门后端语言", "数据库", "接口设计", "Linux", "调试与排错"],
     "path": [("0-1 年", "完成业务接口与排错"),
              ("1-3 年", "负责模块设计与性能"),
              ("3-5 年", "负责系统架构与稳定性")],
     "risks": ["需要理解业务而非只写代码", "线上问题排查压力较大"],
     "verify": ["做一个带数据库的完整小项目", "读 5 个后端 JD", "练习一次线上问题排查"]},
    {"name": "产品 / 运营", "one_liner": "理解用户，把需求变成方案并推动落地。",
     "keywords": ["产品", "需求", "用户", "沟通", "项目", "运营", "活动", "内容", "社群"],
     "entry_roles": ["产品经理", "产品运营", "用户运营", "内容运营"],
     "skills": ["需求分析", "用户调研", "数据分析", "沟通协作", "文档表达"],
     "path": [("0-1 年", "独立负责一个小功能或活动"),
              ("1-3 年", "负责一条业务线"),
              ("3-5 年", "负责产品方向或团队")],
     "risks": ["入门门槛看似低，实际很看项目证据", "需要频繁沟通和推动他人"],
     "verify": ["拆解 1 个常用产品的功能", "做一次小范围用户访谈", "写一份活动方案"]},
    {"name": "测试 / 运维 / 技术支持", "one_liner": "保障系统质量和稳定，问题定位能力强。",
     "keywords": ["测试", "运维", "linux", "docker", "部署", "监控", "质量", "排错"],
     "entry_roles": ["测试工程师", "运维工程师", "技术支持"],
     "skills": ["测试方法", "Linux", "脚本", "监控与排错", "沟通"],
     "path": [("0-1 年", "独立完成测试或值班"),
              ("1-3 年", "负责质量体系或自动化"),
              ("3-5 年", "负责稳定性与团队")],
     "risks": ["部分岗位成长天花板需要主动突破", "值班和线上问题可能带来压力"],
     "verify": ["学一个测试/监控工具并实践", "找 5 个 JD 对照", "做一次故障演练"]},
    {"name": "教育 / 教研", "one_liner": "把知识讲清楚，帮助别人学会。",
     "keywords": ["教育", "老师", "教学", "课程", "辅导", "讲题", "教研"],
     "entry_roles": ["学科教师", "教研员", "课程设计", "教育产品"],
     "skills": ["学科知识", "表达与讲课", "课程设计", "学生沟通"],
     "path": [("0-1 年", "独立带课或教研支持"),
              ("1-3 年", "负责课程体系或班级"),
              ("3-5 年", "负责教研方向或团队")],
     "risks": ["收入与机构/地区差异大，需要核实", "需要长期面对学生和家长"],
     "verify": ["试讲 1 次并收集反馈", "看 5 个教师/教研 JD", "设计 1 节 30 分钟课程"]},
    {"name": "稳定型岗位（考编 / 国企 / 公共部门）", "one_liner": "以稳定和长期确定性为优先。",
     "keywords": ["稳定", "体制", "公务员", "编制", "国企", "家乡", "家庭"],
     "entry_roles": ["公务员", "事业单位", "国企职能岗"],
     "skills": ["行测 / 申论或专业考试", "公文写作", "流程执行", "沟通"],
     "path": [("0-1 年", "完成考试准备与报名"),
              ("1-3 年", "适应岗位并积累资历"),
              ("3-5 年", "争取核心岗位或职级")],
     "risks": ["考试周期和竞争强度需要提前了解", "岗位与城市选择受招录限制"],
     "verify": ["查最近一次招录公告与报录比", "做一套真题", "问 1 位在职人员真实体验"]},
]


def _career_text(profile, qa, resume):
    parts = []
    if isinstance(profile, dict):
        parts.append(json.dumps(profile, ensure_ascii=False))
    elif profile:
        parts.append(str(profile))
    parts.append(_qa_text(qa))
    parts.append(str(resume or ""))
    return "\n".join(parts)


def _local_career_plan(text, selected=""):
    low = text.lower()
    scored = []
    for t in _CAREER_TEMPLATES:
        hits = [k for k in t["keywords"] if k.lower() in low]
        if hits:
            scored.append((t, hits))
    scored.sort(key=lambda x: len(x[1]), reverse=True)
    chosen = scored[:4]
    # 信息不足时补充通用方向
    generic = [
        {"name": "专业相关岗位", "one_liner": "先用专业背景换一张入场券。",
         "keywords": [], "entry_roles": ["专业对口岗位"], "skills": ["专业核心课", "基础工具"],
         "path": [("0-1 年", "完成从学生到职场人的过渡"),
                  ("1-3 年", "找到擅长的细分方向"),
                  ("3-5 年", "形成可迁移的核心能力")],
         "risks": ["专业对口不等于适合，需要用实习验证"],
         "verify": ["找 5 个专业相关 JD", "问 1 位同专业前辈"]},
        {"name": "技能型岗位", "one_liner": "用一项可展示的技能切入职场。",
         "keywords": [], "entry_roles": ["专员 / 助理 / 初级工程师"], "skills": ["一项硬技能", "作品集"],
         "path": [("0-1 年", "做出可展示的作品"), ("1-3 年", "独立负责模块"), ("3-5 年", "形成专长")],
         "risks": ["技能需要持续练习，不能只停留在了解"],
         "verify": ["做一个 1-2 周的小作品", "收集 5 个岗位要求"]},
        {"name": "稳定型岗位", "one_liner": "以稳定和确定性为优先。",
         "keywords": [], "entry_roles": ["国企 / 事业单位 / 职能岗"], "skills": ["考试准备", "流程执行", "沟通"],
         "path": [("0-1 年", "准备考试与报名"), ("1-3 年", "适应岗位"), ("3-5 年", "争取核心岗位")],
         "risks": ["招录周期与竞争强度需自行核实"],
         "verify": ["查最近一次招录公告", "做一套真题"]},
    ]
    for g in generic:
        if len(chosen) >= 3:
            break
        if not any(c[0]["name"] == g["name"] for c in chosen):
            chosen.append((g, []))

    directions = []
    for t, hits in chosen[:4]:
        fit = 72 if hits else 55
        fit = min(92, fit + min(18, len(hits) * 4))
        why = ("你提供的信息里出现了：" + "、".join(hits[:5])) if hits else \
              "目前信息不足，建议先用一个低成本项目验证兴趣。"
        directions.append({
            "name": t["name"], "one_liner": t["one_liner"], "fit": fit, "why": [why],
            "entry_roles": t["entry_roles"], "skills": t["skills"],
            "path": [{"stage": a, "focus": b, "milestones": [b]} for a, b in t["path"]],
            "risks": t["risks"], "verify_actions": t["verify"],
        })
    picked = next((d for d in directions if d["name"] == selected), directions[0])
    comparison = [{
        "name": d["name"], "fit": d["fit"],
        "entry_difficulty": "中" if d["fit"] >= 70 else "低",
        "growth": d["path"][-1]["focus"] if d["path"] else "",
        "gap": "、".join(d["skills"][:3]),
        "verify_cost": "1-2 周",
    } for d in directions]
    gaps = [{"have": "目前信息里能看到的证据有限", "need": s,
             "action": "用一个课程 / 个人项目补一个可展示证据"}
            for s in picked["skills"][:4]]
    plan = [
        {"period": "3 个月", "goals": ["补齐一项核心技能", "做一个小作品"],
         "outputs": ["可展示的项目或作品集"], "checks": ["能讲清项目目标、做法和结果"]},
        {"period": "6 个月", "goals": ["投递并复盘 10 次", "补齐岗位高频要求"],
         "outputs": ["一份针对方向的简历", "面试复盘记录"], "checks": ["拿到至少 3 次面试"]},
        {"period": "12 个月", "goals": ["进入目标岗位并站稳", "形成可迁移能力"],
         "outputs": ["工作成果或作品集更新"], "checks": ["能独立负责一个模块"]},
    ]
    return {
        "summary": "这是本地规则版规划：先按你提到的关键词给出方向，再补通用行动计划。",
        "directions": directions,
        "comparison": comparison,
        "selected": {"name": picked["name"], "gaps": gaps, "plan": plan,
                     "search_keywords": [picked["name"]] + picked["skills"][:3]},
        "questions_for_user": ["你更看重成长还是稳定？", "可接受的城市和行业有哪些？"],
        "degraded": True,
    }


_CAREER_SYSTEM = (
    "你是职业生涯规划教练，服务对象是迷茫的毕业生。"
    "请基于用户提供的真实信息给出 3-5 个可验证的职业方向，不要编造薪资、招聘量或市场数据；"
    "涉及外部信息时在文字里提醒用户去招聘平台核实。"
    "每个方向都要说明为什么适合、入门岗位、要补的能力、1-3 年路径、风险和本周可做的验证动作。"
    "再给出方向对比表，以及选定方向的差距分析和 3/6/12 个月行动计划。"
    "只输出 JSON 对象，不要解释。JSON 结构："
    '{"summary":"","directions":[{"name":"","one_liner":"","fit":0-100,"why":[""],'
    '"entry_roles":[""],"skills":[""],"path":[{"stage":"","focus":"","milestones":[""]}],'
    '"risks":[""],"verify_actions":[""]}],'
    '"comparison":[{"name":"","fit":0-100,"entry_difficulty":"低|中|高","growth":"","gap":"","verify_cost":""}],'
    '"selected":{"name":"","gaps":[{"have":"","need":"","action":""}],'
    '"plan":[{"period":"3个月|6个月|12个月","goals":[""],"outputs":[""],"checks":[""]}],'
    '"search_keywords":[""]},"questions_for_user":[""]}'
)


def _normalize_career_plan(obj):
    directions = []
    for it in _as_list(obj.get("directions"))[:5]:
        if not isinstance(it, dict):
            continue
        path = []
        for p in _as_list(it.get("path"))[:5]:
            if isinstance(p, dict):
                path.append({"stage": _s(p.get("stage"))[:40],
                             "focus": _s(p.get("focus"))[:200],
                             "milestones": _text_list(p.get("milestones"), 5, 200)})
        directions.append({
            "name": _s(it.get("name"))[:80],
            "one_liner": _s(it.get("one_liner"))[:200],
            "fit": _score100(it.get("fit"), 60),
            "why": _text_list(it.get("why"), 5, 300),
            "entry_roles": _text_list(it.get("entry_roles"), 6, 80),
            "skills": _text_list(it.get("skills"), 8, 80),
            "path": path,
            "risks": _text_list(it.get("risks"), 5, 240),
            "verify_actions": _text_list(it.get("verify_actions"), 6, 240),
        })
    if not directions:
        return None
    comparison = []
    for it in _as_list(obj.get("comparison"))[:5]:
        if isinstance(it, dict):
            comparison.append({
                "name": _s(it.get("name"))[:80],
                "fit": _score100(it.get("fit"), 60),
                "entry_difficulty": _s(it.get("entry_difficulty"))[:20] or "中",
                "growth": _s(it.get("growth"))[:200],
                "gap": _s(it.get("gap"))[:200],
                "verify_cost": _s(it.get("verify_cost"))[:60],
            })
    sel = obj.get("selected") if isinstance(obj.get("selected"), dict) else {}
    gaps = []
    for it in _as_list(sel.get("gaps"))[:10]:
        if isinstance(it, dict):
            gaps.append({"have": _s(it.get("have"))[:300],
                         "need": _s(it.get("need"))[:300],
                         "action": _s(it.get("action"))[:400]})
    plan = []
    for it in _as_list(sel.get("plan"))[:6]:
        if isinstance(it, dict):
            plan.append({"period": _s(it.get("period"))[:30],
                         "goals": _text_list(it.get("goals"), 6, 200),
                         "outputs": _text_list(it.get("outputs"), 6, 200),
                         "checks": _text_list(it.get("checks"), 6, 200)})
    return {
        "summary": _s(obj.get("summary"))[:800],
        "directions": directions,
        "comparison": comparison,
        "selected": {
            "name": _s(sel.get("name"))[:80] or directions[0]["name"],
            "gaps": gaps, "plan": plan,
            "search_keywords": _text_list(sel.get("search_keywords"), 10, 80),
        },
        "questions_for_user": _text_list(obj.get("questions_for_user"), 8, 240),
        "degraded": False,
    }


def career_plan(payload):
    profile = payload.get("profile") if isinstance(payload.get("profile"), dict) else {}
    qa = _normalize_qa(payload.get("qa"))
    resume = _clip(_s(payload.get("resume")), 12000)
    selected = _clip(_s(payload.get("selected")), 80)
    text = _career_text(profile, qa, resume)
    if len(text.strip()) < 20:
        raise InterviewError("先完成几道盘点问题，或粘贴简历，再生成规划")
    user = ("用户自述 / 盘点问答：\n%s\n\n简历：\n%s\n\n用户选定的方向：%s"
            % (_qa_text(qa, 20, 900) or "（无）", resume or "（无）", selected or "（未选，请给出推荐）"))
    obj = _llm_json(_CAREER_SYSTEM, user, max_tokens=4200, source="interview_career")
    out = _normalize_career_plan(obj) if isinstance(obj, dict) else None
    if out:
        return out
    return _local_career_plan(text, selected)


# ---------------- 面试题库 ----------------

_BANK_ZH = {
    "hr": [
        ("请用 1 分钟做一个自我介绍，重点讲和这个岗位相关的经历。", "按「我是谁 → 我做过什么 → 为什么适合」三段，别复述简历全文。"),
        ("你为什么选择这个岗位 / 这个行业？", "把个人动机与岗位要求连起来，避免只说「喜欢」。"),
        ("讲一次你和同事意见不一致的经历，最后怎么解决的？", "用 STAR，重点讲你如何对齐目标，而不是证明谁对。"),
        ("你觉得自己最大的缺点是什么？", "选一个真实但不致命的点，并说明你正在怎么改。"),
        ("未来三年你的职业规划是什么？", "给出可验证的阶段目标，与岗位成长路径对齐。"),
        ("讲一次你在压力下完成任务的经历。", "说清压力来源、你的取舍、结果与复盘。"),
        ("你为什么离开上一份实习 / 为什么现在找工作？", "保持客观，不抱怨前团队，聚焦下一步发展。"),
        ("你还有什么问题想问我？", "问团队、业务、成长路径，不问薪资福利等终面再谈的事。"),
        ("介绍一下你最近一段实习 / 项目里最日常的工作内容。", "说清频率、协作对象和产出，别只讲感受。"),
        ("你为什么选择这个专业？现在还会选它吗？", "讲真实原因和变化，不要背招生简章。"),
        ("讲一次你说服别人的经历。", "说清对方顾虑、你的论据、最后结果。"),
        ("你在团队里通常扮演什么角色？", "给具体场景，比如推进、补位、记录。"),
        ("讲一次你没完成承诺的经历，后来怎么处理？", "承认问题 → 补救 → 之后怎么避免。"),
        ("你如何安排一周的学习 / 工作时间？", "给出真实方法：清单、优先级、固定时段。"),
        ("你最近关注这个行业的什么变化？", "只讲你真的看过的东西，并说明对你的判断。"),
        ("如果同时拿到两个 offer，你会怎么选？", "给出你的排序标准，别只说「看情况」。"),
        ("你怎么看待加班？", "先问清节奏，再给真实边界和底线。"),
        ("讲一次你主动做了分外之事的经历。", "说清动机、额外投入和结果。"),
        ("你和老师 / 上级意见不一致时怎么办？", "先对齐目标，再摆证据，最后执行决定。"),
        ("你希望第一份工作带给你什么？", "具体到能力、项目和反馈机制。"),
    ],
    "technical": [
        ("请介绍一个你做过的最有挑战的项目，重点讲你的技术决策。", "讲清背景、约束、方案对比、你为何选它、结果。"),
        ("如果让你从零搭建这个岗位相关的一个系统，你会怎么拆？", "先讲需求与边界，再讲模块、数据、异常与验证。"),
        ("讲一次你排查线上 / 复杂问题的过程。", "按「现象 → 假设 → 验证 → 根因 → 修复 → 预防」讲。"),
        ("你如何保证自己交付的质量？", "说具体手段：测试、评审、监控、灰度、回滚。"),
        ("最近学过什么新技术？为什么学、怎么验证学会了？", "给出来源、练习方式与产出，不要只报名词。"),
        ("讲一次你做过的技术权衡（性能 vs 成本 / 速度 vs 质量）。", "把约束和代价说清楚，结论要能落地。"),
        ("如果需求频繁变更，你会怎么处理？", "讲沟通、优先级、可逆设计与记录。"),
        ("你如何和产品 / 设计 / 测试协作？", "用具体冲突场景说明你的协作方式。"),
        ("讲一个你最熟悉的工具或库，以及你用它做过什么。", "来源 → 练习 → 产出，别只报名词。"),
        ("你的项目里最薄弱的一环是什么？如果重做会怎么改？", "承认取舍，给出具体改法。"),
        ("如何定位一个「偶发」的线上问题？", "日志、指标、复现条件、灰度对比。"),
        ("你写过测试吗？怎么判断测够了？", "覆盖关键路径、边界和失败场景。"),
        ("介绍一次你做过的性能优化。", "先量基线，再定位瓶颈，最后验证收益。"),
        ("你怎么做技术选型？", "约束、备选、成本、退出成本。"),
        ("讲一次你读源码 / 文档解决问题的经历。", "问题 → 查证路径 → 结论。"),
        ("团队代码规范不统一，你会怎么办？", "先对齐规则，再用工具约束。"),
        ("如何保证交付时间？延期了怎么办？", "拆任务、暴露风险、提前沟通。"),
        ("说一个你熟悉的数据结构，以及你用它解决过的问题。", "讲清复杂度和适用场景。"),
        ("你如何学习一个陌生领域的知识？", "给出你的路径和验证方式。"),
        ("如果让你给一个新人讲你的项目，你会怎么讲？", "背景 → 架构 → 关键决策 → 坑。"),
    ],
    "stress": [
        ("你的经历看起来并不突出，我为什么要录用你？", "不硬顶，先承认差距，再用一两个具体证据证明匹配。"),
        ("如果这次面试你没通过，你觉得原因会是什么？", "展示自我认知，不要过度自我否定。"),
        ("你刚才的回答很空，能给出具体例子吗？", "立刻切到 STAR，给时间、动作、结果。"),
        ("这个岗位经常加班，你能接受吗？", "先问清节奏与周期，再给出真实边界。"),
        ("你觉得自己比同届毕业生强在哪里？", "给可验证的差异点，不贬低他人。"),
        ("如果我告诉你，我们更想要有 3 年经验的人呢？", "强调可迁移能力与上手速度，给出证据。"),
        ("你的简历里这段经历看起来和你投的岗位没关系，怎么解释？", "找可迁移能力，给证据。"),
        ("你刚才说的数字是自己算的还是听来的？", "诚实说明口径，不要硬撑。"),
        ("如果我今天只能记住你一句话，你希望是哪句？", "给一个与岗位强相关的差异点。"),
        ("你觉得自己这次面试表现怎么样？", "客观评价，指出一处可改进。"),
        ("你为什么到现在还没有相关实习？", "说明客观原因，再讲你做了哪些替代准备。"),
        ("你的同学都比你强，你怎么看？", "不贬低他人，聚焦自己的进展。"),
        ("这个岗位可能需要做很多琐事，你能接受吗？", "问清比例，表达真实态度。"),
        ("你说的「团队合作」听起来很空，能举个例子吗？", "立刻切 STAR，给时间、动作、结果。"),
        ("如果入职后发现工作内容和你预期不符，你会怎么办？", "先沟通，再评估去留。"),
        ("你最大的不足是什么？我们为什么还要你？", "承认 + 正在改 + 其他优势。"),
        ("你刚才的回答前后矛盾，你怎么解释？", "先澄清定义，再补充信息。"),
        ("你觉得自己值这个薪资吗？", "用能力和市场信息支撑，不硬顶。"),
        ("给你一周时间，你能学会这个工具吗？", "给出计划与验收标准。"),
        ("如果这次没通过，你下一步打算做什么？", "展示复盘与持续行动。"),
    ],
    "mixed": [
        ("请做一个 1 分钟自我介绍。", "三段式：背景 → 相关经历 → 匹配点。"),
        ("讲一个你最有成就感的项目，以及你在其中的具体贡献。", "用 STAR，数字优先。"),
        ("你为什么适合这个岗位？", "把岗位要求逐条对应到你的证据。"),
        ("讲一次失败经历，你从中学到了什么？", "重点在复盘与之后的改变。"),
        ("你如何处理多任务冲突？", "讲优先级判断与沟通机制。"),
        ("你希望加入什么样的团队？", "说协作方式与成长诉求，避免空泛。"),
        ("你最近一次主动学习是什么？", "给出学习动机、方法与产出。"),
        ("你还有什么问题？", "问业务与成长，不问敏感福利。"),
        ("用一个具体例子说明你的学习能力。", "新领域 → 方法 → 结果。"),
        ("讲一次你拒绝别人的经历。", "说清边界和替代方案。"),
        ("你怎么判断一件事值不值得做？", "给判断标准，不只讲感觉。"),
        ("讲一次你犯了错但及时止损的经历。", "发现 → 上报 → 补救。"),
        ("你更擅长独立工作还是协作？", "给场景，别二选一。"),
        ("讲一个你坚持很久的习惯。", "时长、频率、带来的变化。"),
        ("你如何面对重复性的工作？", "讲提效或质量提升的做法。"),
        ("讲一次你帮助他人的经历。", "对方的问题、你的动作、结果。"),
        ("你怎么处理负面情绪？", "给真实方法，别喊口号。"),
        ("你对这份工作最担心的是什么？", "说真实顾虑，并说明怎么应对。"),
        ("如果让你改进我们产品的某个功能，你会改什么？", "先讲用户问题，再讲方案。"),
        ("用三句话介绍你自己。", "身份 → 证据 → 求职意向。"),
    ],
}

_BANK_EN = {
    "hr": [
        ("Tell me about yourself in one minute.", "Background → relevant experience → why this role."),
        ("Why do you want this role and this industry?", "Connect motivation with the job requirements."),
        ("Tell me about a time you disagreed with a teammate.", "Use STAR; focus on alignment, not winning."),
        ("What is your greatest weakness?", "Pick a real but manageable one and show how you improve."),
        ("Where do you see yourself in three years?", "Give concrete, verifiable milestones."),
        ("Describe a time you worked under pressure.", "Explain the pressure, your trade-offs, the result."),
        ("Why are you leaving your current internship?", "Stay objective and focus on growth."),
        ("Do you have any questions for me?", "Ask about the team, product, and growth path."),
        ("Walk me through a typical day in your most recent internship or project.", "Cover frequency, collaborators, and output."),
        ("Why did you choose your major? Would you choose it again?", "Give your real reason, not a brochure answer."),
        ("Tell me about a time you persuaded someone.", "Explain their concern, your argument, and the result."),
        ("What role do you usually play in a team?", "Use a concrete example: driver, filler, recorder."),
        ("Tell me about a commitment you failed to keep. What did you do next?", "Own it → fix it → prevent it."),
        ("How do you plan your week?", "Share a real method: list, priority, fixed slots."),
        ("What recent change in this industry caught your attention?", "Only cite what you actually read, and say what it means to you."),
        ("How would you choose between two offers?", "State your ranking criteria instead of saying it depends."),
        ("How do you feel about overtime?", "Ask about the actual cadence, then state real boundaries."),
        ("Tell me about a time you did more than you were asked.", "Motivation, extra effort, and result."),
        ("What do you do when you disagree with a teacher or manager?", "Align on the goal, present evidence, then commit to the decision."),
        ("What do you want from your first job?", "Be specific: skills, projects, and feedback."),
    ],
    "technical": [
        ("Walk me through the most challenging project you have built.", "Context, constraints, options, decision, result."),
        ("How would you design a system related to this role from scratch?", "Clarify requirements, then modules, data, failure modes."),
        ("Tell me about a difficult bug you debugged.", "Symptom → hypothesis → verification → root cause → fix."),
        ("How do you ensure the quality of your work?", "Tests, reviews, monitoring, rollout, rollback."),
        ("What have you learned recently, and how did you verify it?", "Show sources, practice, and output."),
        ("Describe a technical trade-off you made.", "State constraints and costs clearly."),
        ("How do you handle frequently changing requirements?", "Communication, prioritization, reversible design."),
        ("How do you collaborate with product and QA?", "Use a concrete conflict example."),
        ("Which tool or library do you know best, and what have you shipped with it?", "Source → practice → output."),
        ("What is the weakest part of your project, and how would you rebuild it?", "Admit the trade-off and give a concrete fix."),
        ("How do you debug an intermittent production issue?", "Logs, metrics, reproduction conditions, canary comparison."),
        ("How do you decide you have enough tests?", "Critical paths, boundaries, and failure cases."),
        ("Walk me through a performance optimization you made.", "Baseline first, then bottleneck, then verified gain."),
        ("How do you make a technology choice?", "Constraints, alternatives, cost, exit cost."),
        ("Tell me about a time you solved a problem by reading source code or docs.", "Problem → evidence path → conclusion."),
        ("What would you do if the team's coding standards are inconsistent?", "Agree on rules first, then enforce with tooling."),
        ("How do you protect a delivery deadline, and what if you slip?", "Break down tasks, surface risk early, communicate."),
        ("Describe a data structure you know well and a problem you solved with it.", "Complexity and fit for the scenario."),
        ("How do you learn an unfamiliar domain?", "Your path and how you verify understanding."),
        ("How would you explain your project to a new teammate?", "Background → architecture → key decisions → pitfalls."),
    ],
    "stress": [
        ("Your experience does not look impressive. Why should we hire you?", "Acknowledge the gap, then give concrete evidence."),
        ("If you fail this interview, what would be the reason?", "Show self-awareness without over-apologizing."),
        ("Your last answer was vague. Can you give a specific example?", "Immediately switch to STAR with facts."),
        ("This role requires frequent overtime. Is that acceptable?", "Ask about the actual cadence, then state real boundaries."),
        ("What makes you better than other graduates?", "Give verifiable differentiators without putting others down."),
        ("We prefer candidates with three years of experience. What do you say?", "Emphasize transferable skills and ramp-up evidence."),
        ("This experience on your resume seems unrelated to the role. Explain.", "Find transferable skills and give evidence."),
        ("Are those numbers your own calculation or something you heard?", "Be honest about how you measured them."),
        ("If I remember only one sentence from this interview, what should it be?", "Give a differentiator relevant to the role."),
        ("How do you think you did today?", "Be objective and name one thing to improve."),
        ("Why do you still have no relevant internship?", "Explain the situation and what you did instead."),
        ("Your classmates seem stronger. What do you think?", "Do not put others down; focus on your progress."),
        ("This role involves a lot of repetitive work. Is that acceptable?", "Ask about the proportion and answer honestly."),
        ("Your example about teamwork sounds vague. Can you be specific?", "Switch to STAR immediately."),
        ("What if the job turns out different from what you expected?", "Talk first, then decide whether to stay."),
        ("What is your biggest weakness, and why should we hire you anyway?", "Admit it, show progress, add other strengths."),
        ("Your answers seem contradictory. How do you explain that?", "Clarify definitions, then add information."),
        ("Do you think you deserve this salary?", "Support it with skills and market information."),
        ("Could you learn this tool in one week?", "Give a plan and an acceptance standard."),
        ("If you fail this interview, what will you do next?", "Show review and consistent action."),
    ],
    "mixed": [
        ("Tell me about yourself in one minute.", "Background → relevant experience → fit."),
        ("Describe your most proud project and your contribution.", "STAR, numbers first."),
        ("Why are you a good fit for this role?", "Map each requirement to evidence."),
        ("Tell me about a failure and what you learned.", "Focus on the review and what changed afterwards."),
        ("How do you handle competing priorities?", "Explain prioritization and communication."),
        ("What kind of team do you want to join?", "Talk about collaboration and growth."),
        ("What did you learn proactively recently?", "Motivation, method, output."),
        ("Any questions for me?", "Ask about business and growth, not perks."),
        ("Give a concrete example of your learning ability.", "New field → method → result."),
        ("Tell me about a time you said no.", "Explain the boundary and the alternative you offered."),
        ("How do you decide whether something is worth doing?", "Give criteria, not just a feeling."),
        ("Tell me about a mistake you caught early.", "Spot it → report it → fix it."),
        ("Are you better working alone or in a team?", "Use scenarios instead of picking one."),
        ("Tell me about a habit you have kept for a long time.", "Duration, frequency, and the change it brought."),
        ("How do you handle repetitive work?", "Talk about efficiency or quality improvements."),
        ("Tell me about a time you helped someone.", "Their problem, your action, the result."),
        ("How do you deal with negative emotions?", "Share a real method, not a slogan."),
        ("What worries you most about this job?", "Name the concern and how you would handle it."),
        ("If you could improve one feature of our product, what would it be?", "Start from the user problem, then the solution."),
        ("Introduce yourself in three sentences.", "Identity → evidence → intent."),
    ],
}


def preset_questions(kind="mixed", lang="zh", n=8, shuffle=False):
    bank = _BANK_EN if _lang(lang) == "en" else _BANK_ZH
    items = list(bank.get(_type(kind)) or bank["mixed"])
    if shuffle:
        random.shuffle(items)
    out = []
    for i, (q, tip) in enumerate(items[:max(1, min(int(n or 8), 30))]):
        out.append({"id": "%s-%s-%d" % (_type(kind), _lang(lang), i + 1),
                    "category": INTERVIEW_TYPES[_type(kind)],
                    "question": q, "tip": tip})
    return out


# ---------------- 表达力统计 ----------------

_FILLERS_ZH = ["嗯", "呃", "额", "那个", "然后", "就是说", "其实", "这个", "反正"]
_FILLERS_EN = ["um", "uh", "like", "you know", "basically", "actually", "so yeah"]


def expression_stats(items, lang="zh"):
    """根据作答文本与时长统计语速、口头禅。

    items: [{"text": str, "duration_ms": int}]；duration_ms 缺失时按字数估算
    （中文约 4 字/秒，英文约 2.5 词/秒），结果里用 estimated 标记。
    """
    lang = _lang(lang)
    rows, total_units, total_sec, estimated = [], 0, 0.0, False
    filler_total = 0
    for it in _as_list(items):
        if not isinstance(it, dict):
            continue
        text = _s(it.get("text"))
        if not text:
            continue
        if lang == "en":
            units = len(re.findall(r"[A-Za-z0-9']+", text))
            fillers = {w: len(re.findall(r"\b%s\b" % re.escape(w), text, re.I))
                       for w in _FILLERS_EN}
        else:
            units = len(re.sub(r"[\s\W_]+", "", text, flags=re.U))
            fillers = {w: text.count(w) for w in _FILLERS_ZH}
        try:
            sec = max(0.0, float(it.get("duration_ms") or 0) / 1000.0)
        except (TypeError, ValueError):
            sec = 0.0
        if sec <= 0:
            sec = units / (4.0 if lang != "en" else 2.5)
            estimated = True
        fc = sum(v for v in fillers.values())
        filler_total += fc
        total_units += units
        total_sec += sec
        rows.append({"units": units, "seconds": round(sec, 1),
                     "speed": int(round(units / sec * 60)) if sec > 0 else 0,
                     "fillers": {k: v for k, v in fillers.items() if v},
                     "filler_count": fc})
    speed = int(round(total_units / total_sec * 60)) if total_sec > 0 else 0
    ideal = (110, 180) if lang == "en" else (180, 300)
    if not rows:
        level = "无数据"
    elif speed < ideal[0]:
        level = "偏慢"
    elif speed > ideal[1]:
        level = "偏快"
    else:
        level = "适中"
    return {
        "lang": lang,
        "unit": "words/min" if lang == "en" else "字/分",
        "answers": rows,
        "chars": total_units,
        "seconds": round(total_sec, 1),
        "speed": speed,
        "ideal": {"min": ideal[0], "max": ideal[1]},
        "speed_level": level,
        "fillers": filler_total,
        "filler_rate": round(filler_total / total_units * 100, 1) if total_units else 0,
        "estimated": estimated,
    }


# ---------------- 面试会话 ----------------

_SESSION_LOCK = threading.Lock()
_SESSIONS = {}


def reset_sessions():
    """测试用：清空所有面试会话"""
    with _SESSION_LOCK:
        _SESSIONS.clear()


def _sweep():
    now = time.time()
    dead = [k for k, v in _SESSIONS.items() if now - v.get("updated_at", 0) > SESSION_TTL]
    for k in dead:
        _SESSIONS.pop(k, None)
    if len(_SESSIONS) > MAX_SESSIONS:
        for k, _v in sorted(_SESSIONS.items(), key=lambda kv: kv[1].get("updated_at", 0))[
                :len(_SESSIONS) - MAX_SESSIONS]:
            _SESSIONS.pop(k, None)


def _sanitize_config(config):
    c = config if isinstance(config, dict) else {}
    try:
        count = int(c.get("count") or 5)
    except (TypeError, ValueError):
        count = 5
    try:
        difficulty = int(c.get("difficulty") or 3)
    except (TypeError, ValueError):
        difficulty = 3
    return {
        "role": _clip(_s(c.get("role")) or "目标岗位", 120),
        "company": _clip(_s(c.get("company")), 120),
        "type": _type(_s(c.get("type"))),
        "lang": _lang(c.get("lang")),
        "difficulty": max(1, min(5, difficulty)),
        "count": max(3, min(MAX_QUESTIONS, count)),
        "resume_summary": _clip(_s(c.get("resume_summary")), MAX_SUMMARY),
        "jd": _clip(_s(c.get("jd")), MAX_JD),
    }


_PLAN_SYSTEM = (
    "你是一名资深面试官。请根据目标岗位、面试类型、难度和候选人简历摘要，"
    "设计一组主问题。问题要具体、可追问，覆盖不同维度；不要给答案。"
    "只输出 JSON 对象，不要解释。JSON 结构："
    '{"questions":[{"question":"","focus":"考察点","tip":"给候选人的一句话提示"}]}'
)


def _generate_plan(cfg, n):
    lang_note = "请用英文提问。" if cfg["lang"] == "en" else "请用中文提问。"
    user = ("岗位：%s\n公司：%s\n面试类型：%s\n难度：%d/5\n"
            "简历摘要：\n%s\n\n岗位 JD：\n%s\n\n请设计 %d 道主问题。%s"
            % (cfg["role"], cfg["company"] or "未填写",
               INTERVIEW_TYPES[cfg["type"]], cfg["difficulty"],
               cfg["resume_summary"] or "（无）", cfg["jd"] or "（无）", n, lang_note))
    obj = _llm_json(_PLAN_SYSTEM, user, max_tokens=1600, source="interview_plan")
    out = []
    if isinstance(obj, dict):
        for it in _as_list(obj.get("questions")):
            if isinstance(it, dict):
                q = _s(it.get("question"))
                if q:
                    out.append({"question": q[:400],
                                "focus": _s(it.get("focus"))[:80],
                                "tip": _s(it.get("tip"))[:300]})
            elif isinstance(it, str) and it.strip():
                out.append({"question": it.strip()[:400], "focus": "", "tip": ""})
    ai_used = bool(out)
    if len(out) < n:
        bank = preset_questions(cfg["type"], cfg["lang"], n)
        have = {x["question"] for x in out}
        for b in bank:
            if len(out) >= n:
                break
            if b["question"] not in have:
                out.append({"question": b["question"], "focus": "", "tip": b["tip"]})
    return out[:n], ai_used


def start_session(config):
    cfg = _sanitize_config(config)
    plan, ai_used = _generate_plan(cfg, cfg["count"])
    sid = uuid.uuid4().hex[:12]
    now = time.time()
    first = plan[0]["question"] if plan else "请先做一个自我介绍。"
    session = {
        "id": sid,
        "config": cfg,
        "plan": plan,
        "index": 0,                       # 已问过的主问题数量
        "pending": [],                    # 追加的追问
        "followups": 0,                   # 已使用的追问次数（防止模型无限追问）
        "current": first,
        "transcript": [{"role": "interviewer", "text": first, "at": now}],
        "durations": [],
        "report": None,
        "started_at": now,
        "updated_at": now,
    }
    with _SESSION_LOCK:
        _sweep()
        _SESSIONS[sid] = session
    return {"session_id": sid, "question": first, "tip": plan[0]["tip"] if plan else "",
            "index": 0, "total": len(plan), "config": cfg,
            "degraded": not ai_used}


def _get_session(sid):
    with _SESSION_LOCK:
        s = _SESSIONS.get(str(sid or ""))
    if not s:
        raise InterviewError("面试会话不存在或已过期，请重新开始")
    return s


_TURN_SYSTEM = (
    "你是正在面试候选人的面试官。请根据岗位与问答记录，对候选人刚才的回答给出："
    "1) 一句现场反馈（指出优点或问题，但不要给出标准答案）；"
    "2) 0-100 的本题得分；3) 如果需要澄清，给一个追问（否则留空）。"
    "只输出 JSON 对象，不要解释。JSON 结构："
    '{"score":0-100,"feedback":"","followup":""}'
)


def _local_turn_eval(answer, lang):
    n = len(answer)
    star = ["情境", "任务", "行动", "结果", "situation", "task", "action", "result"]
    hits = sum(1 for w in star if w.lower() in answer.lower())
    score = 45
    if n >= 60:
        score += 15
    if n >= 150:
        score += 10
    if hits >= 2:
        score += 15
    if re.search(r"\d", answer):
        score += 10
    score = min(95, score)
    if lang == "en":
        feedback = ("Answer recorded (%d characters). " % n
                    + ("Good: it includes concrete details. " if n >= 60 else
                       "Try to add a specific example. ")
                    + ("Add structure like Situation-Task-Action-Result."
                       if hits < 2 else "The structure is clear."))
    else:
        feedback = ("回答已记录（%d 字）。" % n
                    + ("有具体细节。" if n >= 60 else "建议补一个具体例子。")
                    + ("可以按 STAR 把结构讲清楚。" if hits < 2 else "结构比较清楚。"))
    return {"score": score, "feedback": feedback, "followup": ""}


def _advance(s):
    """推进到下一题：优先消费追问，其次主问题。返回 (question, done, tip)。"""
    now = time.time()
    if s["pending"]:
        nxt = s["pending"].pop(0)
    else:
        s["index"] += 1
        nxt = s["plan"][s["index"]]["question"] if s["index"] < len(s["plan"]) else ""
    done = not nxt
    if nxt:
        s["transcript"].append({"role": "interviewer", "text": nxt, "at": now})
    s["current"] = nxt
    s["updated_at"] = now
    tip = ""
    if nxt and s["index"] < len(s["plan"]):
        tip = s["plan"][s["index"]].get("tip") or ""
    return nxt, done, tip


def interview_turn(sid, answer, duration_ms=0, skip=False):
    s = _get_session(sid)
    text = _clip(answer, MAX_ANSWER).strip()
    if skip:
        # 跳过：不评分、不计语速、不追问，直接进下一题
        s["transcript"].append({"role": "candidate", "text": "（跳过）",
                                "score": None, "skipped": True, "at": time.time()})
        nxt, done, tip = _advance(s)
        return {"score": None, "feedback": "这题先跳过，之后可以在专项训练里补。",
                "question": nxt, "tip": tip, "index": s["index"],
                "total": len(s["plan"]), "done": done, "skipped": True,
                "degraded": False}
    if not text:
        raise InterviewError("请先作答再提交")
    cfg = s["config"]
    history = []
    for item in s["transcript"][-8:]:
        who = "面试官" if item.get("role") == "interviewer" else "候选人"
        history.append("%s：%s" % (who, _clip(item.get("text"), 1200)))
    user = ("岗位：%s\n面试类型：%s\n当前问题：%s\n\n最近问答：\n%s\n\n候选人刚才的回答：\n%s"
            % (cfg["role"], INTERVIEW_TYPES[cfg["type"]], s["current"],
               "\n".join(history), text))
    if cfg["lang"] == "en":
        obj = _llm_json(_TURN_SYSTEM + " Reply in English.", user,
                        max_tokens=600, source="interview_turn")
    else:
        obj = _llm_json(_TURN_SYSTEM, user, max_tokens=600, source="interview_turn")
    out = None
    if isinstance(obj, dict):
        out = {"score": _score100(obj.get("score"), 60),
               "feedback": _s(obj.get("feedback"))[:500],
               "followup": _s(obj.get("followup"))[:300]}
    used_ai = isinstance(obj, dict)
    if not out:
        out = _local_turn_eval(text, cfg["lang"])
    try:
        ms = max(0, int(duration_ms or 0))
    except (TypeError, ValueError):
        ms = 0

    now = time.time()
    s["transcript"].append({"role": "candidate", "text": text,
                            "score": out["score"], "feedback": out["feedback"], "at": now})
    s["durations"].append({"text": text, "duration_ms": ms})
    if out["followup"] and s.get("followups", 0) < 3:
        s["pending"].append(out["followup"])
        s["followups"] = s.get("followups", 0) + 1

    nxt, done, tip = _advance(s)
    return {"score": out["score"], "feedback": out["feedback"],
            "question": nxt, "tip": tip, "index": s["index"],
            "total": len(s["plan"]), "done": done,
            "degraded": not used_ai, "skipped": False}


_REPORT_SYSTEM = (
    "你是面试教练。请根据完整面试记录与表达力统计，生成复盘报告。"
    "评分维度固定为：表达清晰、逻辑结构、专业能力、岗位匹配、应变抗压（各 0-100）。"
    "证据必须来自候选人原话；参考回答可以示范更好的结构，但不得编造候选人的经历。"
    "只输出 JSON 对象，不要解释。JSON 结构："
    '{"overall":0-100,"summary":"","dimensions":[{"name":"表达清晰","score":0-100,"comment":""},'
    '{"name":"逻辑结构","score":0-100,"comment":""},{"name":"专业能力","score":0-100,"comment":""},'
    '{"name":"岗位匹配","score":0-100,"comment":""},{"name":"应变抗压","score":0-100,"comment":""}],'
    '"strengths":[""],"improvements":[{"issue":"","evidence":"","action":""}],'
    '"per_question":[{"question":"","score":0-100,"comment":"","better_answer":""}],'
    '"next_steps":[""]}'
)


def _local_report(s, expr):
    # 跳过题不参与评分：没有分数、也没说话
    answers = [t for t in s["transcript"]
               if t.get("role") == "candidate" and not t.get("skipped")]
    scores = [int(t.get("score") or 0) for t in answers] or [0]
    avg = int(round(sum(scores) / len(scores)))
    speed_level = expr.get("speed_level")
    clear = avg
    if speed_level == "偏快":
        clear -= 8
    elif speed_level == "偏慢":
        clear -= 5
    if expr.get("filler_rate", 0) > 4:
        clear -= 8
    logic = min(95, avg + (5 if answers and all(
        len(t.get("text") or "") >= 80 for t in answers) else -5))
    dims = [
        {"name": "表达清晰", "score": max(20, clear), "comment": "语速%s，口头禅 %d 次。" % (
            speed_level, expr.get("fillers", 0))},
        {"name": "逻辑结构", "score": max(20, logic), "comment": "按回答完整度与结构词估算。"},
        {"name": "专业能力", "score": max(20, avg), "comment": "按逐题得分估算。"},
        {"name": "岗位匹配", "score": max(20, avg), "comment": "按回答与岗位的相关度估算。"},
        {"name": "应变抗压", "score": max(20, avg), "comment": "按追问与短回答的应对估算。"},
    ]
    per = []
    for t in answers:
        per.append({"question": _prev_question(s, t),
                    "score": int(t.get("score") or 0),
                    "comment": _s(t.get("feedback")),
                    "better_answer": ""})
    return {
        "overall": avg,
        "summary": "本地规则版复盘（未连接聊天模型）：重点看语速、口头禅与回答完整度。",
        "dimensions": dims,
        "strengths": ["完成了 %d 道题的作答。" % len(answers)] if answers else [],
        "improvements": [
            {"issue": "回答结构", "evidence": "部分回答缺少情境与结果。",
             "action": "每题先用一句话给结论，再按 STAR 展开。"},
            {"issue": "表达流畅度",
             "evidence": "语速%s，口头禅 %d 次。" % (speed_level, expr.get("fillers", 0)),
             "action": "回答前停 1 秒想结构，用短句替代「嗯 / 然后」。"},
        ],
        "per_question": per,
        "next_steps": ["挑 2 道低分题，按 STAR 重写并录音复听。"],
        "degraded": True,
    }


def _prev_question(s, answer_item):
    idx = None
    for i, t in enumerate(s["transcript"]):
        if t is answer_item:
            idx = i
            break
    if idx is None:
        return ""
    for j in range(idx - 1, -1, -1):
        if s["transcript"][j].get("role") == "interviewer":
            return s["transcript"][j].get("text") or ""
    return ""


def normalize_report(obj):
    pool = _as_list(obj.get("dimensions")) if isinstance(obj, dict) else []
    dims = []
    for i, name in enumerate(INTERVIEW_DIMENSIONS):
        item = pool[i] if i < len(pool) and isinstance(pool[i], dict) else {}
        named = next((d for d in pool if isinstance(d, dict)
                      and _s(d.get("name") or d.get("dimension")) == name), None)
        if isinstance(named, dict):
            item = named
        dims.append({"name": name, "score": _score100(item.get("score"), 60),
                     "comment": _s(item.get("comment") or item.get("advice"))[:300]})
    improvements = []
    for it in _as_list(obj.get("improvements"))[:10]:
        if isinstance(it, dict):
            improvements.append({"issue": _s(it.get("issue"))[:120],
                                 "evidence": _s(it.get("evidence"))[:300],
                                 "action": _s(it.get("action"))[:400]})
        elif _s(it):
            improvements.append({"issue": _s(it)[:120], "evidence": "", "action": ""})
    per = []
    for it in _as_list(obj.get("per_question"))[:20]:
        if isinstance(it, dict):
            per.append({"question": _s(it.get("question"))[:300],
                        "score": _score100(it.get("score"), 60),
                        "comment": _s(it.get("comment"))[:400],
                        "better_answer": _s(it.get("better_answer"))[:1200]})
    return {
        "overall": _score100(obj.get("overall") or obj.get("score"),
                             int(round(sum(d["score"] for d in dims) / len(dims)))),
        "summary": _s(obj.get("summary"))[:800],
        "dimensions": dims,
        "strengths": _text_list(obj.get("strengths"), 8, 300),
        "improvements": improvements,
        "per_question": per,
        "next_steps": _text_list(obj.get("next_steps"), 8, 300),
        "degraded": False,
    }


def finish_session(sid):
    s = _get_session(sid)
    if s.get("report"):
        return {"report": s["report"], "expression": s.get("expression") or {},
                "transcript": s["transcript"]}
    cfg = s["config"]
    expr = expression_stats(s["durations"], cfg["lang"])
    lines = []
    for t in s["transcript"]:
        who = "面试官" if t.get("role") == "interviewer" else "候选人"
        lines.append("%s：%s" % (who, t.get("text")))
    user = ("岗位：%s\n公司：%s\n面试类型：%s\n难度：%d/5\n\n"
            "岗位 JD：\n%s\n\n简历摘要：\n%s\n\n表达力统计：%s\n\n完整记录：\n%s"
            % (cfg["role"], cfg["company"] or "未填写", INTERVIEW_TYPES[cfg["type"]],
               cfg["difficulty"], _clip(cfg.get("jd") or "", 3000) or "（无）",
               _clip(cfg.get("resume_summary") or "", 2000) or "（无）",
               json.dumps(expr, ensure_ascii=False), "\n".join(lines)[-12000:]))
    obj = _llm_json(_REPORT_SYSTEM, user, max_tokens=3200, source="interview_report")
    if isinstance(obj, dict):
        report = normalize_report(obj)
        if expr.get("fillers"):
            report.setdefault("strengths", [])
    else:
        report = _local_report(s, expr)
    s["report"] = report
    s["expression"] = expr
    s["updated_at"] = time.time()
    return {"report": report, "expression": expr, "transcript": s["transcript"]}


# ---------------- 单题训练 ----------------

_DRILL_SYSTEM = (
    "你是面试教练。请点评候选人对这道面试题的回答，给出 0-100 分、"
    "具体改进点和一份参考回答（可以示范结构，但不得编造候选人的经历）。"
    "只输出 JSON 对象，不要解释。JSON 结构："
    '{"score":0-100,"comment":"","strengths":[""],"improvements":[""],"better_answer":""}'
)


def evaluate_answer(question, answer, lang="zh", tip=""):
    q = _clip(_s(question), 800)
    a = _clip(_s(answer), MAX_ANSWER)
    if not q:
        raise InterviewError("缺少题目")
    if len(a) < 10:
        raise InterviewError("回答太短，至少写 10 个字符")
    lang = _lang(lang)
    user = ("面试题：%s\n提示：%s\n候选人回答：%s\n\n请用%s点评。"
            % (q, _clip(tip, 300) or "（无）", a,
               "英文" if lang == "en" else "中文"))
    obj = _llm_json(_DRILL_SYSTEM + (" Reply in English." if lang == "en" else ""),
                    user, max_tokens=1600, source="interview_drill")
    if isinstance(obj, dict):
        return {
            "score": _score100(obj.get("score"), 60),
            "comment": _s(obj.get("comment"))[:600],
            "strengths": _text_list(obj.get("strengths"), 6, 240),
            "improvements": _text_list(obj.get("improvements"), 8, 300),
            "better_answer": _s(obj.get("better_answer"))[:1600],
            "degraded": False,
        }
    local = _local_turn_eval(a, lang)
    return {
        "score": local["score"],
        "comment": local["feedback"],
        "strengths": ["回答完整度 %d 字。" % len(a)] if len(a) >= 60 else [],
        "improvements": ["按 STAR 补全情境、任务、行动、结果。",
                         "给一个可核实的数字结果。"],
        "better_answer": "",
        "degraded": True,
    }
