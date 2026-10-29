# -*- coding: utf-8 -*-
"""研究道场 API：/api/hall/dojo/…（与其它大厅玩法同一分组）。

除了训练本身，还包含 P1 的导入三段：
parse（材料 → 正文）→ draft（模型 → 待确认草稿）→ commit（确认 → 个人场景包）。
"""
from app.dojo import assess, authoring, cases, engine, importer, packs, records, repro, research


def _api_dojo_packs(h):
    """场景包清单。前端据此决定显示哪些场景，不需要知道任何具体学科。"""
    h._json({"ok": True, "packs": packs.public_packs()})


def _api_dojo_start(h):
    """开始一个训练单元：返回题面（不含答案与理由）。"""
    body = h._body()
    try:
        out = engine.start(body.get("pack_id") or "", body.get("unit_id"))
        return h._json({"ok": True, **out})
    except engine.DojoError as e:
        return h._json({"ok": False, "error": str(e)}, 400)


def _api_dojo_judge(h):
    """提交一个判断：返回对错、专家理由、陷阱与方法卡。"""
    body = h._body()
    try:
        out = engine.answer(body.get("session_id"), body.get("unit_id"),
                            body.get("value"), body.get("elapsed_ms"))
        return h._json({"ok": True, **out})
    except engine.DojoError as e:
        return h._json({"ok": False, "error": str(e)}, 400)


def _api_dojo_import_parse(h):
    """上传材料或粘贴文本并抽取正文；不读任意本地路径。"""
    body = h._body()
    try:
        out = importer.parse_material(body.get("name"), body.get("data"), body.get("text") or "")
        return h._json({"ok": True, **out})
    except importer.DojoImportError as e:
        return h._json({"ok": False, "error": str(e)}, 400)


def _api_dojo_import_draft(h):
    """用当前聊天模型生成草稿；出处对不上的条目会被丢弃并回传原因。"""
    body = h._body()
    try:
        out = importer.draft(body.get("material_id"), body.get("count"),
                             body.get("standard") or "", body.get("dimensions"))
        return h._json({"ok": True, **out})
    except importer.DojoImportError as e:
        return h._json({"ok": False, "error": str(e)}, 400)


def _api_dojo_import_commit(h):
    """把用户确认过的草稿写成一个 personal 场景包。"""
    body = h._body()
    try:
        out = importer.commit(body.get("name"), body.get("units"),
                              body.get("standard") or "", body.get("dimensions"),
                              body.get("material_name") or "", body.get("desc") or "")
        return h._json({"ok": True, **out})
    except importer.DojoImportError as e:
        return h._json({"ok": False, "error": str(e)}, 400)


def _api_dojo_packs_delete(h):
    """删除个人场景；内置包一律拒绝。"""
    body = h._body()
    try:
        packs.delete_user_pack(body.get("pack_id"))
        return h._json({"ok": True, "packs": packs.public_packs()})
    except packs.PackError as e:
        return h._json({"ok": False, "error": str(e)}, 400)


def _api_dojo_packs_rename(h):
    body = h._body()
    try:
        out = packs.rename_user_pack(body.get("pack_id"), body.get("name"))
        return h._json({"ok": True, "pack": out, "packs": packs.public_packs()})
    except packs.PackError as e:
        return h._json({"ok": False, "error": str(e)}, 400)


def _api_dojo_packs_export(h):
    """导出成 JSON；返回内容由前端写成 .json 文件。"""
    body = h._body()
    try:
        return h._json({"ok": True, "pack": packs.export_pack(body.get("pack_id"))})
    except packs.PackError as e:
        return h._json({"ok": False, "error": str(e)}, 400)


def _api_dojo_packs_import(h):
    """导入场景包 JSON；id 冲突时自动改名，不覆盖现有包。"""
    body = h._body()
    try:
        out = packs.import_pack(body.get("pack"))
        return h._json({"ok": True, "pack": out, "packs": packs.public_packs()})
    except packs.PackError as e:
        return h._json({"ok": False, "error": str(e)}, 400)


def _api_dojo_assess_start(h):
    """开始前测 / 后测：一次返回全部题面（不含答案）。"""
    body = h._body()
    try:
        out = assess.start(body.get("pack_id"), body.get("phase"))
        return h._json({"ok": True, **out})
    except assess.AssessError as e:
        return h._json({"ok": False, "error": str(e)}, 400)


def _api_dojo_assess_answer(h):
    body = h._body()
    try:
        out = assess.answer(body.get("session_id"), body.get("unit_id"),
                            body.get("value"), body.get("elapsed_ms"))
        return h._json({"ok": True, **out})
    except assess.AssessError as e:
        return h._json({"ok": False, "error": str(e)}, 400)


def _api_dojo_assess_finish(h):
    """结算测评：正确率、单位时间正确率、按能力维度分解。"""
    body = h._body()
    try:
        return h._json({"ok": True, **assess.finish(body.get("session_id"))})
    except assess.AssessError as e:
        return h._json({"ok": False, "error": str(e)}, 400)


def _api_dojo_repro_list(h):
    """可用的复现实验（内置 + 用户放的包）。"""
    h._json({"ok": True, "packs": [
        {"id": p["id"], "title": p.get("title") or p["id"]}
        for p in repro.load_packs()
    ]})


def _api_dojo_repro_get(h):
    body = h._body()
    try:
        return h._json({"ok": True, "pack": repro.public_pack(repro.get_pack(body.get("id")))})
    except repro.ReproError as e:
        return h._json({"ok": False, "error": str(e)}, 400)


def _api_dojo_repro_run(h):
    """在临时目录执行用户代码，把 stdout 的 JSON 与论文值比对。"""
    body = h._body()
    try:
        return h._json({"ok": True, **repro.run(body.get("id"), body.get("code"))})
    except repro.ReproError as e:
        return h._json({"ok": False, "error": str(e)}, 400)


def _api_dojo_repro_create(h):
    """从编辑器保存个人复现包；先跑参考解自检，目标值对不上就整包拒绝。"""
    body = h._body()
    try:
        return h._json({"ok": True, **repro.create_pack(body)})
    except repro.ReproError as e:
        return h._json({"ok": False, "error": str(e)}, 400)


def _api_dojo_case_list(h):
    """完整建模案例清单：只给标题、来源与步骤数。"""
    h._json({"ok": True, "cases": cases.list_cases()})


def _api_dojo_case_get(h):
    body = h._body()
    try:
        return h._json({"ok": True, "case": cases.public_case(cases.get_case(body.get("id")))})
    except cases.CaseError as e:
        return h._json({"ok": False, "error": str(e)}, 400)


def _api_dojo_case_start(h):
    """开始一个案例：返回第一步题面，不含答案。"""
    body = h._body()
    try:
        return h._json({"ok": True, **cases.start(body.get("id"))})
    except cases.CaseError as e:
        return h._json({"ok": False, "error": str(e)}, 400)


def _api_dojo_case_answer(h):
    """提交当前步骤：返回判分、专家理由、方法卡与下一步题面。"""
    body = h._body()
    try:
        return h._json({"ok": True, **cases.answer(
            body.get("session_id"), body.get("step_id"), body.get("value"),
            body.get("elapsed_ms"))})
    except cases.CaseError as e:
        return h._json({"ok": False, "error": str(e)}, 400)


def _api_dojo_case_finish(h):
    """结算案例：每步得分、最弱能力与第一个断点（不做因果结论）。"""
    body = h._body()
    try:
        return h._json({"ok": True, **cases.finish(body.get("session_id"))})
    except cases.CaseError as e:
        return h._json({"ok": False, "error": str(e)}, 400)


def _api_dojo_research_list(h):
    """研究任务清单：按 collection 分组，只给主题与幕数。"""
    h._json({"ok": True, "collections": research.list_collections(),
             "inquiries": research.list_inquiries()})


def _api_dojo_paper_index(h):
    """真题索引：按赛事 / 年份 / 卷别 / 题号列出全部题位，并标出是否已改编。"""
    h._json({"ok": True, "contests": research.list_paper_index()})


def _api_dojo_research_get(h):
    body = h._body()
    try:
        q = research.get_inquiry(body.get("id"))
        return h._json({"ok": True, "inquiry": research.public_inquiry(q)})
    except research.ResearchError as e:
        return h._json({"ok": False, "error": str(e)}, 400)


def _api_dojo_research_start(h):
    body = h._body()
    try:
        return h._json({"ok": True, **research.start(body.get("id"))})
    except research.ResearchError as e:
        return h._json({"ok": False, "error": str(e)}, 400)


def _api_dojo_research_state(h):
    """断点续做：返回当前幕、已完成幕与研究结论状态。"""
    body = h._body()
    try:
        return h._json({"ok": True, **research.state(body.get("session_id"))})
    except research.ResearchError as e:
        return h._json({"ok": False, "error": str(e)}, 400)


def _api_dojo_research_answer(h):
    body = h._body()
    try:
        return h._json({"ok": True, **research.answer(
            body.get("session_id"), body.get("act_id"), body.get("value"),
            body.get("elapsed_ms"))})
    except research.ResearchError as e:
        return h._json({"ok": False, "error": str(e)}, 400)


def _api_dojo_research_artifact(h):
    """提交研究结论：只做要素覆盖诊断，不计分。"""
    body = h._body()
    try:
        return h._json({"ok": True, **research.submit_artifact(
            body.get("session_id"), body.get("text"))})
    except research.ResearchError as e:
        return h._json({"ok": False, "error": str(e)}, 400)


def _api_dojo_research_revise(h):
    body = h._body()
    try:
        return h._json({"ok": True, **research.revise_artifact(
            body.get("session_id"), body.get("text"))})
    except research.ResearchError as e:
        return h._json({"ok": False, "error": str(e)}, 400)


def _api_dojo_research_finish(h):
    body = h._body()
    try:
        report = research.finish(body.get("session_id"))
    except research.ResearchError as e:
        return h._json({"ok": False, "error": str(e)}, 400)
    try:
        records.save(report)
    except Exception as e:   # 记录失败不能影响本次结算
        print("[研究] 记录保存失败：%s" % e)
    return h._json({"ok": True, **report})


def _api_dojo_research_records_list(h):
    return h._json({"ok": True, "records": records.list_records()})


def _api_dojo_research_records_get(h):
    body = h._body()
    try:
        return h._json({"ok": True, "record": records.get_record(body.get("id"))})
    except ValueError as e:
        return h._json({"ok": False, "error": str(e)}, 400)


def _api_dojo_research_records_delete(h):
    body = h._body()
    try:
        return h._json({"ok": True, **records.delete_record(body.get("id"))})
    except ValueError as e:
        return h._json({"ok": False, "error": str(e)}, 400)


# ---------------- 教材大纲 / 生成审核 ----------------

def _api_dojo_authoring_syllabi(h):
    return h._json({"ok": True, "syllabi": authoring.list_syllabi()})


def _api_dojo_authoring_syllabus(h):
    body = h._body()
    try:
        return h._json({"ok": True, "syllabus": authoring.syllabus_detail(body.get("id"))})
    except authoring.AuthoringError as e:
        return h._json({"ok": False, "error": str(e)}, 400)


def _api_dojo_authoring_drafts(h):
    return h._json({"ok": True, "drafts": authoring.list_drafts()})


def _api_dojo_authoring_generate(h):
    body = h._body()
    try:
        return h._json({"ok": True, "draft": authoring.generate(
            body.get("syllabus_id"), body.get("chapter_id") or body.get("section_id"))})
    except authoring.AuthoringError as e:
        return h._json({"ok": False, "error": str(e)}, 400)


def _api_dojo_authoring_draft_get(h):
    body = h._body()
    try:
        return h._json({"ok": True, "draft": authoring.public_draft(body.get("id"))})
    except authoring.AuthoringError as e:
        return h._json({"ok": False, "error": str(e)}, 400)


def _api_dojo_authoring_update(h):
    body = h._body()
    try:
        return h._json({"ok": True, "draft": authoring.update(
            body.get("id"), body.get("doc"))})
    except authoring.AuthoringError as e:
        return h._json({"ok": False, "error": str(e)}, 400)


def _api_dojo_authoring_reject(h):
    body = h._body()
    try:
        return h._json({"ok": True, **authoring.reject(body.get("id"))})
    except authoring.AuthoringError as e:
        return h._json({"ok": False, "error": str(e)}, 400)


def _api_dojo_authoring_commit(h):
    body = h._body()
    try:
        return h._json({"ok": True, **authoring.commit(body.get("id"))})
    except authoring.AuthoringError as e:
        return h._json({"ok": False, "error": str(e)}, 400)
