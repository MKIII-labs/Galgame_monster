# -*- coding: utf-8 -*-
"""功能大厅 API。

路由按功能分组：`/api/hall/<功能id>/<动作>`。新增一个 AI 互动功能时，
在这里加一组 handler、在 app/httpd.py 的 routes 里挂上即可 —— 大厅前端
（web/hall.js 的注册表）与后端各功能之间没有耦合。
"""
from app import hall, math_arena


def _api_hall_geo_new(h):
    """开一局图寻：随机取一张真实照片 + 真实拍摄点，返回题面（**不含**真值）。"""
    body = h._body()
    try:
        count = int(body.get("ai_count", 2))
    except (TypeError, ValueError):
        count = 2
    # ai_off：按人关闭的 AI 名单；没传时仍按 ai_count 取前 N 位（兼容旧调用）
    ai_off = body.get("ai_off")
    if ai_off is not None and not isinstance(ai_off, list):
        ai_off = None
    try:
        round_ = hall.new_round(ai_count=count, ai_off=ai_off)
    except Exception as e:                                  # 取题失败：明确告知可重试
        return h._json({"ok": False, "error": str(e)}, 502)
    payload = hall.public_round(round_)
    payload["ai_vision"] = hall.ai_capability()
    payload["ai_roster"] = hall.ai_catalog()
    h._json({"ok": True, "round": payload})


def _api_hall_geo_guess(h):
    """提交一次标注（真人）。"""
    body = h._body()
    guess, err = hall.put_guess(body.get("round_id"), body.get("player_id"),
                                body.get("lat"), body.get("lng"), mode="human")
    if err:
        return h._json({"ok": False, "error": err}, 400)
    h._json({"ok": True, "guess": guess})


def _api_hall_geo_ai(h):
    """让一个 AI 参与者标注。

    前端逐人发起（可并行），这样界面上能一个个人地「思考中 → 已标注」，
    而不是等最慢的那个把所有人一起卡住。
    同一 AI 已经标注过就直接返回；正在请求时重复点击也不会重复扣模型。
    """
    body = h._body()
    rid, pid = body.get("round_id"), body.get("player_id")
    ctx, err = hall.ai_begin(rid, pid)
    if err:
        return h._json({"ok": False, "error": err}, 400)
    if ctx.get("guess"):
        return h._json({"ok": True, "guess": ctx["guess"], "reused": True})
    try:
        loc = hall.ai_locate_round(rid, ctx.get("style", ""), ctx.get("template", ""))
        guess, err2 = hall.put_guess(rid, pid, loc["lat"], loc["lng"],
                                     mode=loc["mode"], note=loc["note"],
                                     reason=loc.get("reason") or [])
    finally:
        hall.ai_end(rid, pid)
    if err2:
        return h._json({"ok": False, "error": err2}, 400)
    h._json({"ok": True, "guess": guess})


def _api_hall_geo_analyze(h):
    """玩家识图辅助：只返回画面特征，不判断国家/地区。"""
    body = h._body()
    out, err = hall.geo_describe(body.get("round_id"))
    if err:
        return h._json({"ok": False, "error": err}, 400)
    h._json({"ok": True, **out})


def _api_hall_math_new(h):
    """开一局数学竞技场：按本地段位与所选课程生成 5 道题（题面不含答案）。"""
    body = h._body()
    try:
        level = int(body.get("level", 3))
    except (TypeError, ValueError):
        level = 3
    try:
        round_ = math_arena.new_round(level=level,
                                      courses=body.get("courses"),
                                      subjects=body.get("subjects"))
    except Exception as e:                                  # noqa: BLE001
        return h._json({"ok": False, "error": str(e)}, 502)
    h._json({"ok": True, "round": math_arena.public_round(round_)})


def _api_hall_math_abilities(h):
    """8 维能力名称 + 知识点映射；前端据此在本地聚合历史数据。"""
    h._json({"ok": True, **math_arena.ability_meta()})


def _api_hall_math_answer(h):
    """提交一道题的答案，立即返回对错、解析与三位 AI 的作答。"""
    body = h._body()
    try:
        out = math_arena.answer(body.get("round_id"), body.get("qid"),
                                body.get("value"), body.get("elapsed_ms"))
        return h._json({"ok": True, **out})
    except math_arena.MathError as e:
        return h._json({"ok": False, "error": str(e)}, 400)


def _api_hall_math_finish(h):
    """结算一局：总分、答对数、名次。"""
    body = h._body()
    try:
        out = math_arena.finish(body.get("round_id"))
        return h._json({"ok": True, **out})
    except math_arena.MathError as e:
        return h._json({"ok": False, "error": str(e)}, 400)


def _api_hall_math_paper_meta(h):
    """期末模拟卷的课程 / 知识点选项。"""
    h._json({"ok": True, **math_arena.paper_meta()})


def _api_hall_math_paper_new(h):
    """生成一套单课程期末模拟卷（题面不含答案与提示）。"""
    body = h._body()
    try:
        out = math_arena.new_paper(
            body.get("course"), body.get("count", 15),
            body.get("duration", 90), body.get("level", 3),
            body.get("skill", ""), seed=body.get("seed"))
        return h._json({"ok": True, **out})
    except math_arena.MathError as e:
        return h._json({"ok": False, "error": str(e)}, 400)
    except Exception as e:                                  # noqa: BLE001
        return h._json({"ok": False, "error": "生成试卷失败：%s" % e}, 500)


def _api_hall_math_paper_grade(h):
    """交卷后批量判分，返回每题解析与知识点统计。"""
    body = h._body()
    try:
        out = math_arena.grade_paper(body.get("paper_id"), body.get("answers"),
                                     body.get("time_ms", 0))
        return h._json({"ok": True, **out})
    except math_arena.MathError as e:
        return h._json({"ok": False, "error": str(e)}, 400)


def _api_hall_math_proofs(h):
    """证明题列表。"""
    body = h._body()
    h._json({"ok": True, **math_arena.proof_meta(body.get("course"))})


def _api_hall_math_proof_grade(h):
    """AI 步骤评分（不是形式化验证）。"""
    body = h._body()
    try:
        out = math_arena.grade_proof(body.get("proof_id"), body.get("text"))
        return h._json({"ok": True, **out})
    except math_arena.MathError as e:
        return h._json({"ok": False, "error": str(e)}, 400)


def _api_hall_geo_reveal(h):
    """结算：算出每位参与者与真实拍摄点的距离并排名。幂等。"""
    result, err = hall.reveal(h._body().get("round_id"))
    if err:
        return h._json({"ok": False, "error": err}, 400)
    h._json({"ok": True, "result": result})
