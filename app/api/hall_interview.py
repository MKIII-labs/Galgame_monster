# -*- coding: utf-8 -*-
"""功能大厅：模拟面试 API（自注册路由，不改 httpd 的路由表）。

路由统一为 /api/hall/interview/<动作>，全部 POST：
- resume/parse     上传简历文件 → 纯文本（docparse 支持 txt/md/docx/pdf）
- resume/analyze   简历分析
- resume/build     按真实信息生成简历
- resume/polish    逐条润色
- jd/match         岗位 JD 匹配
- questions        本地题库
- drill            单题训练点评
- start / turn / report  语音面试会话
"""
import base64
import os

from app import docparse, interview
from app.registry import route

MAX_FILE_BYTES = 8 * 1024 * 1024


def _body(h):
    try:
        return h._body() or {}
    except Exception:
        return {}


def _fail(h, e, code=400):
    return h._json({"ok": False, "error": str(e)}, code)


@route("/api/hall/interview/resume/parse")
def _api_interview_resume_parse(h):
    """简历文件解析：只抽文本，不落盘。"""
    b = _body(h)
    name = os.path.basename(str(b.get("name") or "resume.txt"))
    data = b.get("data") or ""
    if not data:
        return _fail(h, "没有收到文件内容")
    try:
        raw = base64.b64decode(data)
    except Exception:
        return _fail(h, "文件内容不是合法的 base64")
    if not raw:
        return _fail(h, "文件为空")
    if len(raw) > MAX_FILE_BYTES:
        return _fail(h, "文件超过 8 MB，请压缩后再试")
    try:
        text, warn = docparse.extract(name, raw)
    except Exception as e:                                  # noqa: BLE001
        return _fail(h, "解析失败：%s" % e, 500)
    text = (text or "").strip()
    if not text:
        return _fail(h, warn or "没有解析出文本；如果是扫描版 PDF，请直接粘贴简历文字")
    return h._json({"ok": True, "name": name, "text": text,
                    "chars": len(text), "warn": warn or ""})


@route("/api/hall/interview/resume/analyze")
def _api_interview_resume_analyze(h):
    b = _body(h)
    try:
        out = interview.analyze_resume(b.get("text"), b.get("role"), b.get("jd"))
    except interview.InterviewError as e:
        return _fail(h, e)
    except Exception as e:                                  # noqa: BLE001
        return _fail(h, "简历分析失败：%s" % e, 500)
    return h._json({"ok": True, **out})


@route("/api/hall/interview/resume/build")
def _api_interview_resume_build(h):
    b = _body(h)
    try:
        out = interview.build_resume(b.get("profile"), b.get("role"), b.get("jd"),
                                     b.get("qa"), b.get("facts"))
    except interview.InterviewError as e:
        return _fail(h, e)
    except Exception as e:                                  # noqa: BLE001
        return _fail(h, "简历生成失败：%s" % e, 500)
    return h._json({"ok": True, **out})


@route("/api/hall/interview/resume/import")
def _api_interview_resume_import(h):
    """把一份已有简历拆进六个栏目，接着用快速填写 / AI 访谈补全。"""
    b = _body(h)
    try:
        out = interview.extract_profile(b.get("text"))
    except interview.InterviewError as e:
        return _fail(h, e)
    except Exception as e:                                  # noqa: BLE001
        return _fail(h, "整理失败：%s" % e, 500)
    return h._json({"ok": True, **out})


@route("/api/hall/interview/resume/guide")
def _api_interview_resume_guide(h):
    """AI 访谈式简历引导：一次返回一个问题。"""
    try:
        out = interview.guide_turn("resume", _body(h))
    except Exception as e:                                  # noqa: BLE001
        return _fail(h, "访谈失败：%s" % e, 500)
    return h._json({"ok": True, **out})


@route("/api/hall/interview/career/guide")
def _api_interview_career_guide(h):
    """职业规划盘点：一次返回一个问题。"""
    try:
        out = interview.guide_turn("career", _body(h))
    except Exception as e:                                  # noqa: BLE001
        return _fail(h, "盘点失败：%s" % e, 500)
    return h._json({"ok": True, **out})


@route("/api/hall/interview/career/plan")
def _api_interview_career_plan(h):
    """生成职业方向、对比与阶段行动计划。"""
    try:
        out = interview.career_plan(_body(h))
    except interview.InterviewError as e:
        return _fail(h, e)
    except Exception as e:                                  # noqa: BLE001
        return _fail(h, "生成职业规划失败：%s" % e, 500)
    return h._json({"ok": True, **out})


@route("/api/hall/interview/resume/polish")
def _api_interview_resume_polish(h):
    b = _body(h)
    try:
        out = interview.polish_resume(b.get("text"), b.get("role"), b.get("mode"))
    except interview.InterviewError as e:
        return _fail(h, e)
    except Exception as e:                                  # noqa: BLE001
        return _fail(h, "简历润色失败：%s" % e, 500)
    return h._json({"ok": True, **out})


@route("/api/hall/interview/jd/match")
def _api_interview_jd_match(h):
    b = _body(h)
    try:
        out = interview.match_jd(b.get("text"), b.get("jd"), b.get("role"))
    except interview.InterviewError as e:
        return _fail(h, e)
    except Exception as e:                                  # noqa: BLE001
        return _fail(h, "JD 匹配失败：%s" % e, 500)
    return h._json({"ok": True, **out})


@route("/api/hall/interview/questions")
def _api_interview_questions(h):
    b = _body(h)
    try:
        n = int(b.get("n") or 12)
    except (TypeError, ValueError):
        n = 12
    items = interview.preset_questions(b.get("type") or "mixed",
                                       b.get("lang") or "zh", n,
                                       shuffle=bool(b.get("shuffle")))
    return h._json({"ok": True, "items": items,
                    "types": interview.INTERVIEW_TYPES})


@route("/api/hall/interview/drill")
def _api_interview_drill(h):
    b = _body(h)
    try:
        out = interview.evaluate_answer(b.get("question"), b.get("answer"),
                                        b.get("lang") or "zh", b.get("tip"))
    except interview.InterviewError as e:
        return _fail(h, e)
    except Exception as e:                                  # noqa: BLE001
        return _fail(h, "点评失败：%s" % e, 500)
    return h._json({"ok": True, **out})


@route("/api/hall/interview/start")
def _api_interview_start(h):
    b = _body(h)
    try:
        out = interview.start_session(b.get("config") if isinstance(b.get("config"), dict) else b)
    except Exception as e:                                  # noqa: BLE001
        return _fail(h, "开始面试失败：%s" % e, 500)
    return h._json({"ok": True, **out})


@route("/api/hall/interview/turn")
def _api_interview_turn(h):
    b = _body(h)
    try:
        out = interview.interview_turn(b.get("session_id"), b.get("answer"),
                                       b.get("duration_ms"), skip=bool(b.get("skip")))
    except interview.InterviewError as e:
        return _fail(h, e)
    except Exception as e:                                  # noqa: BLE001
        return _fail(h, "提交回答失败：%s" % e, 500)
    return h._json({"ok": True, **out})


@route("/api/hall/interview/report")
def _api_interview_report(h):
    b = _body(h)
    try:
        out = interview.finish_session(b.get("session_id"))
    except interview.InterviewError as e:
        return _fail(h, e)
    except Exception as e:                                  # noqa: BLE001
        return _fail(h, "生成复盘报告失败：%s" % e, 500)
    return h._json({"ok": True, **out})
