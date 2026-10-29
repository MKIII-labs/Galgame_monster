# -*- coding: utf-8 -*-
"""模拟面试后端单测：JSON 解析 / 简历兜底 / 面试会话 / 表达力统计 / API 处理。

不联网：所有模型调用都通过 monkeypatch 注入返回值。
"""
import base64
import json

import pytest

from app import interview
from app.api import hall_interview
from app.registry import get as route_get


SAMPLE_RESUME = (
    "教育背景\n"
    "XX 大学 计算机科学与技术 2021.09-2025.06，GPA 3.7/4.0\n"
    "项目经历\n"
    "- 负责校园二手交易平台前端开发，使用 Vue 与 TypeScript\n"
    "- 优化首屏加载，把 3.2s 降到 1.1s，日活 800+\n"
    "技能\n"
    "Python / Java / MySQL / Git\n"
)


@pytest.fixture(autouse=True)
def _clean_sessions():
    interview.reset_sessions()
    yield
    interview.reset_sessions()


# ---------------- JSON 解析 ----------------

def test_extract_json_variants():
    assert interview.extract_json('{"a": 1}') == {"a": 1}
    assert interview.extract_json('```json\n{"a": 2}\n```') == {"a": 2}
    assert interview.extract_json('说明如下：\n{"a": 3}\n以上。') == {"a": 3}
    assert interview.extract_json('[{"a": 4}]') == [{"a": 4}]
    assert interview.extract_json('不是 JSON') is None
    assert interview.extract_json('') is None


def test_score100_accepts_common_shapes():
    assert interview._score100(85) == 85
    assert interview._score100("85分") == 85
    assert interview._score100(0.8) == 80
    assert interview._score100(120) == 100
    assert interview._score100(-3) == 0
    assert interview._score100(None, 60) == 60
    assert interview._score100("没有数字", 55) == 55


# ---------------- 简历 ----------------

def test_resume_stats_detects_sections_and_numbers():
    st = interview.resume_stats(SAMPLE_RESUME)
    assert "教育" in st["sections"]
    assert "项目" in st["sections"]
    assert st["numbers"] >= 2
    assert "Python" in st["skills"]
    assert st["bullets"] >= 2


def test_analyze_resume_falls_back_without_model(monkeypatch):
    monkeypatch.setattr(interview, "_llm_json", lambda *a, **k: None)
    out = interview.analyze_resume(SAMPLE_RESUME, "前端开发工程师")
    assert out["degraded"] is True
    assert 0 <= out["overall"] <= 100
    assert len(out["dimensions"]) == 5
    assert [d["name"] for d in out["dimensions"]] == interview.RESUME_DIMENSIONS
    assert isinstance(out["issues"], list)
    assert out["stats"]["chars"] > 0


def test_analyze_resume_rejects_too_short():
    with pytest.raises(interview.InterviewError):
        interview.analyze_resume("太短")


def test_normalize_resume_report_cleans_model_output():
    obj = {
        "overall": "88",
        "dimensions": [
            {"name": "完整度", "score": "90分", "comment": "ok"},
            {"name": "结构清晰", "score": 0.7},
        ],
        "issues": [{"severity": "HIGH", "section": "项目", "quote": "负责",
                    "problem": "没有结果", "fix": "补数字"}],
        "highlights": ["有数字"], "missing": ["奖项"], "keywords": ["Vue"],
    }
    out = interview.normalize_resume_report(obj)
    assert out["overall"] == 88
    assert out["dimensions"][0]["score"] == 90
    assert out["dimensions"][1]["score"] == 70
    assert out["issues"][0]["severity"] == "high"
    assert out["degraded"] is False


def test_build_resume_fallback_keeps_facts(monkeypatch):
    monkeypatch.setattr(interview, "_llm_json", lambda *a, **k: None)
    profile = {"basic": "张三，上海", "education": "XX 大学 计算机", "skills": "Python"}
    out = interview.build_resume(profile, "后端开发")
    assert out["degraded"] is True
    assert "张三" in out["resume_markdown"]
    assert "XX 大学" in out["resume_markdown"]
    assert out["followups"]
    assert out["warnings"]


def test_polish_resume_fallback_suggests_without_rewriting(monkeypatch):
    monkeypatch.setattr(interview, "_llm_json", lambda *a, **k: None)
    out = interview.polish_resume("- 负责校园二手交易平台前端开发，使用 Vue 与 TypeScript 完成核心页面", "前端")
    assert out["degraded"] is True
    assert out["bullets"]
    assert out["bullets"][0]["rewritten"] == ""
    assert out["bullets"][0]["reason"]


def test_match_jd_fallback_reports_covered_and_missing(monkeypatch):
    monkeypatch.setattr(interview, "_llm_json", lambda *a, **k: None)
    jd = "岗位要求：熟悉 Python、MySQL，了解 Redis 与 Docker。"
    out = interview.match_jd(SAMPLE_RESUME, jd, "后端开发")
    assert out["degraded"] is True
    assert 0 <= out["match_score"] <= 100
    covered = {x["keyword"] for x in out["covered"]}
    missing = {x["keyword"] for x in out["missing"]}
    assert "Python" in covered
    assert "Redis" in missing


# ---------------- 题库与表达力 ----------------

def test_preset_questions_shape_and_language():
    zh = interview.preset_questions("hr", "zh", 5)
    assert len(zh) == 5
    assert all("question" in x and "tip" in x for x in zh)
    en = interview.preset_questions("technical", "en", 5)
    assert len(en) == 5
    assert all(ord(x["question"][0]) < 128 for x in en)


def test_expression_stats_zh_counts_fillers_and_speed():
    text = "嗯，我觉得这个方案可以，然后我们先做用户调研。"
    st = interview.expression_stats([{"text": text, "duration_ms": 4000}], "zh")
    assert st["lang"] == "zh"
    assert st["chars"] > 0
    assert st["seconds"] == 4.0
    assert st["speed"] > 0
    assert st["fillers"] >= 3
    assert st["estimated"] is False


def test_expression_stats_en_and_estimated_duration():
    st = interview.expression_stats(
        [{"text": "Um, I think, you know, this works."}], "en")
    assert st["lang"] == "en"
    assert st["unit"] == "words/min"
    assert st["fillers"] >= 2
    assert st["estimated"] is True


def test_expression_stats_empty():
    st = interview.expression_stats([], "zh")
    assert st["answers"] == []
    assert st["speed"] == 0
    assert st["speed_level"] == "无数据"


# ---------------- 面试会话 ----------------

def _fake_llm(plan_n=4):
    def fake(system, user, max_tokens=1800, source="interview"):
        if source == "interview_plan":
            return {"questions": [{"question": "Q%d" % (i + 1), "focus": "f", "tip": "t%d" % (i + 1)}
                                  for i in range(plan_n)]}
        if source == "interview_turn":
            return {"score": 77, "feedback": "回答有细节", "followup": ""}
        if source == "interview_report":
            return {
                "overall": 78,
                "dimensions": [{"name": n, "score": 75, "comment": "c"}
                               for n in interview.INTERVIEW_DIMENSIONS],
                "summary": "整体不错",
                "strengths": ["结构清楚"],
                "improvements": [{"issue": "数字少", "evidence": "原话", "action": "补数字"}],
                "per_question": [{"question": "Q1", "score": 77,
                                  "comment": "ok", "better_answer": "STAR"}],
                "next_steps": ["重录一遍"],
            }
        return None
    return fake


def test_session_lifecycle_and_report(monkeypatch):
    monkeypatch.setattr(interview, "_llm_json", _fake_llm(4))
    start = interview.start_session({"role": "后端开发", "type": "technical",
                                     "lang": "zh", "count": 4, "difficulty": 3})
    assert start["total"] == 4
    assert start["question"] == "Q1"
    assert "Q2" not in json.dumps(start, ensure_ascii=False)      # 后续题目不下发
    sid = start["session_id"]

    last = None
    for i in range(4):
        last = interview.interview_turn(sid, "这是我的第 %d 个回答，包含具体行动与结果。" % (i + 1), 3000)
        assert last["score"] == 77
    assert last["done"] is True
    assert last["question"] == ""

    out = interview.finish_session(sid)
    assert out["report"]["overall"] == 78
    assert len(out["report"]["dimensions"]) == 5
    assert out["expression"]["answers"]
    assert len(out["expression"]["answers"]) == 4

    again = interview.finish_session(sid)
    assert again["report"] == out["report"]                     # 报告缓存，不重复生成


def test_turn_rejects_empty_answer(monkeypatch):
    monkeypatch.setattr(interview, "_llm_json", _fake_llm(3))
    start = interview.start_session({"role": "产品", "count": 3})
    with pytest.raises(interview.InterviewError):
        interview.interview_turn(start["session_id"], "   ")


def test_turn_skip_advances_without_score(monkeypatch):
    monkeypatch.setattr(interview, "_llm_json", _fake_llm(3))
    start = interview.start_session({"role": "产品", "count": 3})
    out = interview.interview_turn(start["session_id"], "", skip=True)
    assert out["skipped"] is True
    assert out["score"] is None
    assert out["question"] == "Q2"
    assert out["index"] == 1


def test_local_report_ignores_skipped(monkeypatch):
    monkeypatch.setattr(interview, "_llm_json", lambda *a, **k: None)
    start = interview.start_session({"role": "运营", "count": 3})
    sid = start["session_id"]
    interview.interview_turn(sid, "我负责了一次活动策划，最后参与人数 200。", 5000)
    interview.interview_turn(sid, "", skip=True)
    out = interview.finish_session(sid)
    assert len(out["expression"]["answers"]) == 1
    assert len(out["report"]["per_question"]) == 1


def test_report_prompt_includes_jd_and_resume(monkeypatch):
    """复盘要带上 JD 与简历，否则「岗位匹配」维度只能空口评分。"""
    seen = {}

    def fake(system, user, max_tokens=1800, source="interview"):
        if source == "interview_plan":
            return {"questions": [{"question": "Q1", "focus": "", "tip": ""}]}
        if source == "interview_turn":
            return {"score": 70, "feedback": "ok", "followup": ""}
        if source == "interview_report":
            seen["user"] = user
            return {"overall": 70, "dimensions": [], "summary": ""}
        return None

    monkeypatch.setattr(interview, "_llm_json", fake)
    start = interview.start_session({"role": "后端", "count": 3,
                                     "jd": "熟悉 Redis 与 Docker",
                                     "resume_summary": "做过校园二手平台"})
    interview.interview_turn(start["session_id"], "我做过缓存优化。", 3000)
    interview.finish_session(start["session_id"])
    assert "Redis" in seen["user"]
    assert "校园二手平台" in seen["user"]


def test_preset_questions_are_expanded_and_shuffleable():
    assert len(interview.preset_questions("hr", "zh", 20)) == 20
    picks = interview.preset_questions("mixed", "zh", 12, shuffle=True)
    assert len(picks) == 12
    assert len({x["question"] for x in picks}) == 12


def test_unknown_session_raises():
    with pytest.raises(interview.InterviewError):
        interview.interview_turn("nope", "回答")


def test_start_without_model_uses_local_bank(monkeypatch):
    monkeypatch.setattr(interview, "_llm_json", lambda *a, **k: None)
    start = interview.start_session({"role": "运营", "type": "hr", "count": 3})
    assert start["total"] == 3
    assert start["question"]
    assert start["degraded"] is True


def test_evaluate_answer_fallback(monkeypatch):
    monkeypatch.setattr(interview, "_llm_json", lambda *a, **k: None)
    out = interview.evaluate_answer("讲一次失败经历", "情境是……我负责……最后结果是……", "zh")
    assert out["degraded"] is True
    assert 0 <= out["score"] <= 100
    assert out["improvements"]


def test_normalize_report_defaults_missing_dimensions():
    out = interview.normalize_report({"overall": "82", "dimensions": [{"score": 70}]})
    assert out["overall"] == 82
    assert len(out["dimensions"]) == 5
    assert out["dimensions"][0]["score"] == 70


# ---------------- API 层 ----------------

class FakeHandler:
    def __init__(self, body=None):
        self.body = body or {}
        self.payload = None
        self.code = None

    def _body(self):
        return self.body

    def _json(self, payload, code=200):
        self.payload = payload
        self.code = code
        return payload


def test_routes_registered():
    for path in ("/api/hall/interview/resume/analyze", "/api/hall/interview/start",
                 "/api/hall/interview/turn", "/api/hall/interview/report"):
        assert route_get("POST", path) is not None


def test_api_resume_parse_txt():
    h = FakeHandler({"name": "resume.txt",
                     "data": base64.b64encode(SAMPLE_RESUME.encode("utf-8")).decode("ascii")})
    hall_interview._api_interview_resume_parse(h)
    assert h.payload["ok"] is True
    assert "教育背景" in h.payload["text"]


def test_api_questions():
    h = FakeHandler({"type": "stress", "lang": "zh", "n": 4})
    hall_interview._api_interview_questions(h)
    assert h.payload["ok"] is True
    assert len(h.payload["items"]) == 4


def test_api_session_flow(monkeypatch):
    monkeypatch.setattr(interview, "_llm_json", _fake_llm(3))
    h = FakeHandler({"config": {"role": "数据分析", "count": 3}})
    hall_interview._api_interview_start(h)
    assert h.payload["ok"] is True
    sid = h.payload["session_id"]

    for _ in range(3):
        h2 = FakeHandler({"session_id": sid, "answer": "我负责了一个完整的分析项目。", "duration_ms": 2000})
        hall_interview._api_interview_turn(h2)
        assert h2.payload["ok"] is True

    h3 = FakeHandler({"session_id": sid})
    hall_interview._api_interview_report(h3)
    assert h3.payload["ok"] is True
    assert h3.payload["report"]["overall"] == 78


def test_api_rejects_bad_file():
    h = FakeHandler({"name": "x.txt", "data": "!!!not-base64!!!"})
    hall_interview._api_interview_resume_parse(h)
    assert h.payload["ok"] is False
    assert h.code == 400


def test_api_resume_import(monkeypatch):
    monkeypatch.setattr(interview, "_llm_json", lambda *a, **k: None)
    h = FakeHandler({"text": SAMPLE_RESUME})
    hall_interview._api_interview_resume_import(h)
    assert h.payload["ok"] is True
    assert "Python" in h.payload["profile"]["skills"]
    assert route_get("POST", "/api/hall/interview/resume/import") is not None


def test_api_turn_skip(monkeypatch):
    monkeypatch.setattr(interview, "_llm_json", _fake_llm(3))
    h = FakeHandler({"config": {"role": "数据分析", "count": 3}})
    hall_interview._api_interview_start(h)
    sid = h.payload["session_id"]
    h2 = FakeHandler({"session_id": sid, "skip": True})
    hall_interview._api_interview_turn(h2)
    assert h2.payload["ok"] is True
    assert h2.payload["skipped"] is True


# ---------------- AI 访谈 / 职业规划 ----------------

def test_resume_guide_local_asks_role_first(monkeypatch):
    """岗位为空时第一题必须先定岗位，否则后面全是无目标闲聊。"""
    monkeypatch.setattr(interview, "_llm_json", lambda *a, **k: None)
    out = interview.guide_turn("resume", {"profile": {}, "qa": [], "step": 0})
    assert out["degraded"] is True
    assert out["field"] == "role"
    assert "岗位" in out["question"]


def test_resume_guide_local_skips_known_fields(monkeypatch):
    monkeypatch.setattr(interview, "_llm_json", lambda *a, **k: None)
    out = interview.guide_turn("resume", {
        "profile": {"basic": "张三，深圳大学，大四"}, "qa": [], "step": 0,
        "role": "数据分析",
    })
    assert out["degraded"] is True
    assert out["step"] == 3
    assert "教育背景" in out["question"]


def test_resume_guide_hint_keeps_question(monkeypatch):
    monkeypatch.setattr(interview, "_llm_json", lambda *a, **k: None)
    out = interview.guide_turn("resume", {
        "profile": {}, "qa": [], "step": 0, "hint": True,
        "current_question": "有没有实习经历？",
    })
    assert out["step"] == 0
    assert out["hint"]
    assert out["question"] == "有没有实习经历？"     # 提示不能换成另一道题


def test_career_guide_local_starts_with_background(monkeypatch):
    monkeypatch.setattr(interview, "_llm_json", lambda *a, **k: None)
    out = interview.guide_turn("career", {"qa": [], "step": 0})
    assert out["degraded"] is True
    assert "专业" in out["question"]


def test_resume_guide_local_advances_with_qa(monkeypatch):
    """本地兜底也要往前走：qa 的长度就是已经问过的轮数。"""
    monkeypatch.setattr(interview, "_llm_json", lambda *a, **k: None)
    first = interview._RESUME_GUIDE_BANK[0]["question"]
    out = interview.guide_turn("resume", {
        "profile": {}, "step": 0, "qa": [{"q": first, "a": "张三，深圳大学"}],
    })
    assert out["question"] != first
    assert out["step"] == 2


def test_resume_guide_local_does_not_repeat_with_prefilled_profile(monkeypatch):
    """回归：预填过基本信息后，本地题库曾经每轮都返回同一道教育题。"""
    monkeypatch.setattr(interview, "_llm_json", lambda *a, **k: None)
    profile = {"basic": "张三，深圳大学，大四"}
    first = interview.guide_turn("resume", {"profile": profile, "qa": [], "step": 0})
    second = interview.guide_turn("resume", {
        "profile": profile, "step": first["step"], "current_question": first["question"],
        "qa": [{"q": first["question"], "a": "拿过奖学金"}],
    })
    assert first["question"] != second["question"]
    assert second["step"] > first["step"]


def test_resume_guide_skip_moves_on_in_local_mode(monkeypatch):
    monkeypatch.setattr(interview, "_llm_json", lambda *a, **k: None)
    first = interview._RESUME_GUIDE_BANK[0]["question"]
    out = interview.guide_turn("resume", {
        "profile": {}, "step": 0, "skip": True,
        "qa": [{"q": first, "a": "（跳过这题）"}], "current_question": first,
    })
    assert out["degraded"] is True
    assert out["question"] != first
    assert out["step"] == 2


def test_resume_guide_skip_falls_back_when_model_repeats(monkeypatch):
    """模型无视「跳过」又问了同一题时，用本地题库兜底换题。"""
    first = interview._RESUME_GUIDE_BANK[0]["question"]
    monkeypatch.setattr(interview, "_llm_json", lambda *a, **k: {
        "question": first, "why": "背景确认", "examples": [], "hint": "",
        "done": False, "summary": "",
    })
    out = interview.guide_turn("resume", {
        "profile": {}, "step": 0, "skip": True,
        "qa": [{"q": first, "a": "（跳过这题）"}], "current_question": first,
    })
    assert out["question"] != first
    assert out["degraded"] is True


def test_guide_coverage_rewards_numbers():
    cov = interview._guide_coverage({
        "education": "深圳大学 数学与应用数学 2023-2027，GPA 3.6/4.0",
        "projects": "用 Python 清洗 3 万条数据，把处理时间从 2 小时降到 20 分钟",
    })
    assert cov["education"] == 100
    assert cov["projects"] >= 70
    assert cov["skills"] == 0


def test_guide_turn_returns_facts_and_coverage(monkeypatch):
    monkeypatch.setattr(interview, "_llm_json", lambda *a, **k: {
        "question": "这段经历的结果数字是多少，记不清就说个大概量级？",
        "section": "projects", "examples": ["大约几十人"], "why": "数字更可信",
        "facts": {"projects": ["用 Python 清洗 3 万条数据"]},
        "done": False, "summary": "",
    })
    out = interview.guide_turn("resume", {
        "profile": {}, "role": "数据分析",
        "qa": [{"q": "介绍一段经历", "a": "做过数据清洗"}], "step": 1,
    })
    assert out["facts"]["projects"]
    assert out["coverage"]["projects"] > 0
    assert out["field"] == "projects"
    assert out["degraded"] is False


def test_guide_turn_rejects_non_question(monkeypatch):
    """模型输出陈述句时换成本地问题，但这一轮抽出的事实不能丢。"""
    monkeypatch.setattr(interview, "_llm_json", lambda *a, **k: {
        "question": "你记不清也没关系。", "section": "projects",
        "facts": {"projects": ["某项目"]}, "done": False,
    })
    out = interview.guide_turn("resume", {
        "profile": {}, "role": "数据分析",
        "qa": [{"q": "q", "a": "a"}], "step": 1,
    })
    assert out["question"].endswith("？")
    assert out["facts"]["projects"]


def test_guide_turn_stops_at_max_rounds(monkeypatch):
    monkeypatch.setattr(interview, "_llm_json", lambda *a, **k: {
        "question": "下一题？", "done": False, "summary": "", "facts": {},
    })
    qa = [{"q": "q%d" % i, "a": "a"} for i in range(interview.MAX_GUIDE_STEPS)]
    out = interview.guide_turn("resume", {
        "profile": {}, "role": "数据分析", "qa": qa,
        "step": interview.MAX_GUIDE_STEPS,
    })
    assert out["done"] is True
    assert out["question"] == ""


def test_build_resume_uses_facts_in_fallback(monkeypatch):
    monkeypatch.setattr(interview, "_llm_json", lambda *a, **k: None)
    out = interview.build_resume({}, "数据分析", "", [], {
        "projects": ["用 Python 清洗 3 万条数据"],
    })
    assert "3 万条" in out["resume_markdown"]
    assert out["gaps"]


def test_build_resume_returns_gaps(monkeypatch):
    monkeypatch.setattr(interview, "_llm_json", lambda *a, **k: {
        "resume_markdown": "# 简历", "followups": [],
        "gaps": [{"section": "projects", "need": "缺结果数字"}],
        "warnings": [], "used_facts": [],
    })
    out = interview.build_resume({"basic": "张三，深圳大学"}, "数据分析")
    assert out["gaps"][0]["section"] == "projects"


def test_build_resume_includes_qa_fallback(monkeypatch):
    monkeypatch.setattr(interview, "_llm_json", lambda *a, **k: None)
    qa = [{"q": "你做过什么项目？", "a": "我做过校园二手交易平台，负责前端并优化首屏。"}]
    out = interview.build_resume({}, "前端开发", "", qa)
    assert out["degraded"] is True
    assert "校园二手交易平台" in out["resume_markdown"]


def test_build_resume_lists_skipped_topics(monkeypatch):
    """跳过的题不能悄悄消失：要作为待补项出现在 followups 里。"""
    qa = [{"q": "有没有实习经历？", "a": "（跳过这题）"},
          {"q": "你做过什么项目？", "a": "做过校园二手交易平台"}]
    monkeypatch.setattr(interview, "_llm_json", lambda *a, **k: {
        "resume_markdown": "# 简历", "followups": ["补充 GPA"], "warnings": [], "used_facts": [],
    })
    out = interview.build_resume({"basic": "张三，深圳大学"}, "前端开发", "", qa)
    assert any("GPA" in x for x in out["followups"])
    assert any("有没有实习经历" in x for x in out["followups"])


def test_build_resume_local_lists_skipped(monkeypatch):
    monkeypatch.setattr(interview, "_llm_json", lambda *a, **k: None)
    out = interview.build_resume({"basic": "张三，深圳大学"}, "前端开发", "",
                                 [{"q": "项目经历？", "a": "（跳过这题）"}])
    assert out["degraded"] is True
    assert any("项目经历" in x for x in out["followups"])


def test_build_resume_fallback_is_structured(monkeypatch):
    """降级版也得像简历：按栏目分块，不能把 JSON 原文直接当正文。"""
    monkeypatch.setattr(interview, "_llm_json", lambda *a, **k: None)
    out = interview.build_resume(
        {"basic": "张三，上海", "projects": "校园二手平台，独立完成前端"}, "后端开发")
    md = out["resume_markdown"]
    assert "## 项目经历" in md
    assert "## 基本信息" in md
    assert '"projects"' not in md
    assert "（跳过这题）" not in md


def test_build_resume_blank_markdown_falls_back(monkeypatch):
    """模型只给追问不给正文时，不能让前端拿到空简历。"""
    monkeypatch.setattr(interview, "_llm_json", lambda *a, **k: {
        "resume_markdown": "", "followups": ["补充 GPA"], "warnings": [], "used_facts": [],
    })
    out = interview.build_resume({"education": "XX 大学 计算机"}, "前端开发")
    assert "XX 大学" in out["resume_markdown"]
    assert any("正文" in w for w in out["warnings"])


def test_extract_profile_local_splits_sections(monkeypatch):
    monkeypatch.setattr(interview, "_llm_json", lambda *a, **k: None)
    text = ("个人信息\n张三 / 13800000000\n教育背景\nXX 大学 计算机 2021-2025\n"
            "实习经历\n某公司 前端实习，负责页面开发\n项目经历\n校园二手平台，Vue 开发\n"
            "技能\nPython / Vue / Git\n")
    out = interview.extract_profile(text)
    assert out["degraded"] is True
    p = out["profile"]
    assert "13800000000" in p["basic"]
    assert "XX 大学" in p["education"]
    assert "某公司" in p["experience"]
    assert "校园二手平台" in p["projects"]
    assert "Python" in p["skills"]


def test_extract_profile_rejects_short():
    with pytest.raises(interview.InterviewError):
        interview.extract_profile("太短了")


def test_career_plan_fallback_returns_directions_and_plan(monkeypatch):
    monkeypatch.setattr(interview, "_llm_json", lambda *a, **k: None)
    qa = [{"q": "你会什么？", "a": "我会 Python、SQL 和数据分析，做过统计项目。"}]
    out = interview.career_plan({"qa": qa, "resume": "", "selected": ""})
    assert out["degraded"] is True
    assert out["directions"]
    assert out["comparison"]
    assert out["selected"]["name"]
    assert len(out["selected"]["plan"]) >= 3
    assert any("数据" in d["name"] for d in out["directions"])


def test_career_plan_requires_some_info():
    with pytest.raises(interview.InterviewError):
        interview.career_plan({"qa": [], "resume": ""})


def test_career_plan_selected_direction(monkeypatch):
    monkeypatch.setattr(interview, "_llm_json", lambda *a, **k: None)
    qa = [{"q": "你会什么？", "a": "我会 Python、SQL 和数据分析。"}]
    first = interview.career_plan({"qa": qa, "selected": ""})
    target = first["directions"][0]["name"]
    out = interview.career_plan({"qa": qa, "selected": target})
    assert out["selected"]["name"] == target


def test_new_routes_registered():
    for path in ("/api/hall/interview/resume/guide",
                 "/api/hall/interview/career/guide",
                 "/api/hall/interview/career/plan"):
        assert route_get("POST", path) is not None


def test_api_career_plan(monkeypatch):
    monkeypatch.setattr(interview, "_llm_json", lambda *a, **k: None)
    h = FakeHandler({"qa": [{"q": "专业", "a": "数学专业，想做数据分析"}], "resume": ""})
    hall_interview._api_interview_career_plan(h)
    assert h.payload["ok"] is True
    assert h.payload["directions"]
