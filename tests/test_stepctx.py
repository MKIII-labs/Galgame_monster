# -*- coding: utf-8 -*-
"""步骤上下文：思考摘要、失败诊断、预算提示。"""

from app import stepctx


def test_summary_keeps_last_two_sentences():
    text = ("先看目录。\n发现入口在 gal-ide.js。\n所以需要改渲染层。\n"
            "下一步：确认容器挂载点。")
    out = stepctx.summarize_reasoning(text, limit=240)
    assert "所以需要改渲染层" in out
    assert "下一步：确认容器挂载点" in out


def test_summary_is_capped_and_marks_ellipsis():
    text = "甲" * 400
    out = stepctx.summarize_reasoning(text, limit=240)
    assert out.startswith("…")
    assert len(out) <= 241


def test_summary_prefers_keyword_over_middle_noise():
    text = ("闲聊一句。\n所以方案换成合成文档。\n"
            "再多说几句没有结论的话。\n最后一句只是补充。")
    out = stepctx.summarize_reasoning(text, limit=240)
    assert "所以方案换成合成文档" in out


def test_summary_empty():
    assert stepctx.summarize_reasoning("") == ""
    assert stepctx.summarize_reasoning(None) == ""


def test_fail_diagnose_edit_file_points_to_read_first():
    out = stepctx.fail_diagnose("edit_file", "❌ 未找到匹配的 old 片段")
    assert "read_file" in out
    assert "不要用完全相同的参数重试" in out


def test_fail_diagnose_unknown_falls_back_to_triage():
    out = stepctx.fail_diagnose("list_dir", "❌ 权限不足")
    assert "参数写错" in out


def test_join_turns_separates_rounds():
    out = stepctx.join_turns(["第一步确认了文件存在。", "", "  第二步搜到了结果。"])
    assert out == "第一步确认了文件存在。\n\n第二步搜到了结果。"


def test_budget_hint_only_near_limit():
    assert stepctx.budget_hint(5, 24) == ""
    assert stepctx.budget_hint(15, 24) == ""
    assert "还剩 3 轮" in stepctx.budget_hint(21, 24)
    assert stepctx.budget_hint(24, 24) == ""
    assert stepctx.budget_hint(0, 24) == ""


def test_progress_hint_every_twenty_rounds():
    """不限轮次时唯一的时间感：第 20 / 40 轮各提醒一次自查，其余轮次不说话。"""
    assert stepctx.progress_hint(0) == ""
    assert stepctx.progress_hint(19) == ""
    assert "第 20 轮" in stepctx.progress_hint(20)
    assert stepctx.progress_hint(21) == ""
    assert "第 40 轮" in stepctx.progress_hint(40)
    # 有轮次上限时也照常提示，两者可以同时出现（一个催收尾、一个让自查）
    assert stepctx.progress_hint(60) != ""


def test_auto_continue_on_pending_plan():
    plan = {"steps": [{"text": "读文件", "status": "done"},
                      {"text": "改渲染", "status": "pending"}]}
    r = stepctx.auto_continue_reason(plan, "先这样。", 3, True, "实现功能",
                                     agent_mode=True)
    assert r and "改渲染" in r["msg"]
    assert r["why"] == "计划还有 1 步未完成"
    # 非 Agent 会话没有执行能力：计划没做完也不催
    assert stepctx.auto_continue_reason(plan, "先这样。", 3, True, "实现功能",
                                        agent_mode=False) is None


def test_auto_continue_when_asking_next():
    r = stepctx.auto_continue_reason(None, "改好了第一步，要不要继续？", 1, True, "实现")
    assert r and r["why"] == "结尾在征询用户"


def test_auto_continue_does_not_decide_for_user():
    """结尾在问「要不要按方案 A 做」→ 不能续跑，交给提问卡等用户拍板。"""
    assert stepctx.auto_continue_reason(
        None, "可以按方案 A 实现。\n\n要不要按方案 A 做？", 3, True, "改",
        agent_mode=True) is None
    assert stepctx.auto_continue_reason(
        None, "两种都行。\n\n用方案 A 还是方案 B？", 3, True, "改",
        agent_mode=True) is None


def test_pending_decision_ignores_continue_and_narration():
    assert stepctx._pending_decision("第一步完成了，要不要继续？") is False
    assert stepctx._pending_decision("读完了。\n\n那为什么还是慢？") is False
    assert stepctx._pending_decision("接下来处理音效。") is False


def test_auto_continue_released_when_complete():
    plan = {"steps": [{"text": "a", "status": "done"}]}
    assert stepctx.auto_continue_reason(plan, "全部完成了。", 2, True, "实现") is None


def test_auto_continue_skips_blocked():
    plan = {"steps": [{"text": "a", "status": "done"},
                      {"text": "b", "status": "blocked"}]}
    assert stepctx.auto_continue_reason(plan, "已全部完成。", 2, True, "实现") is None


def test_auto_continue_kickstarts_dev_task_only_once():
    r = stepctx.auto_continue_reason(None, "我先看看结构。", 0, False,
                                     "实现一个导出功能", first=True, agent_mode=True)
    assert r and r["why"] == "多步任务还没有执行计划"
    assert stepctx.auto_continue_reason(None, "我先看看结构。", 0, False,
                                        "实现一个导出功能", first=False,
                                        agent_mode=True) is None
    # 非 Agent 会话：没有文件能力，不催它建计划
    assert stepctx.auto_continue_reason(None, "我先看看结构。", 0, False,
                                        "实现一个导出功能", first=True,
                                        agent_mode=False) is None


def test_auto_continue_when_promising_next_but_not_acting():
    """「说要做但没调用工具」：本轮动过手、结尾在描述下一步 → 续跑"""
    r = stepctx.auto_continue_reason(None, "先读那段确认原文再改。", 3, True, "改游戏",
                                     agent_mode=True)
    assert r and r["why"] == "结尾说了要做但没调用工具"


def test_auto_continue_ignores_promise_without_any_tool():
    """教学 / 闲聊也会说「接下来」，本轮一个工具都没调 → 不催"""
    assert stepctx.auto_continue_reason(None, "接下来我们看第二块。", 0, False, "讲解",
                                        agent_mode=True) is None


def test_auto_continue_ignores_promise_when_not_agent():
    assert stepctx.auto_continue_reason(None, "接下来我会读一下这个文件。", 2, False, "改",
                                        agent_mode=False) is None


def test_auto_continue_complete_wins_over_promise():
    assert stepctx.auto_continue_reason(None, "全部完成，接下来你可以自己试。", 2, True,
                                        "改", agent_mode=True) is None


def test_auto_continue_on_trailing_colon():
    """正文以冒号结尾：后面本该跟列表 / 代码 / 工具块，却停了"""
    tail = "说明 `toast` 生命值递减那处没写进去。现在一次读清 `achPoll` 里实际写的递减语句："
    r = stepctx.auto_continue_reason(None, tail, 35, True, "改游戏", agent_mode=True)
    assert r and r["why"] == "结尾说了要做但没调用工具"


def test_auto_continue_colon_needs_agent_mode():
    assert stepctx.auto_continue_reason(None, "先说三点：", 2, False, "聊天",
                                        agent_mode=False) is None


def test_auto_continue_on_rewrite_phrasing():
    """43.85 现场原句：词表只收「现在改 / 现在读」，模型换成了「现在重写」"""
    tail = ("现在重写一版：加入高通噪声声部，参数按 `pingCrack` 设置"
            "（7ms、700Hz 高通、0.8ms 起音），然后重新测量。")
    r = stepctx.auto_continue_reason(None, tail, 9, True, "改音效", agent_mode=True)
    assert r and r["why"] == "结尾说了要做但没调用工具"


def test_auto_continue_requires_explicit_promise_after_tools():
    """动过手但什么都没承诺 → 不再无条件续跑。

    旧实现是「动过手 + 没命中完成词」就续跑，长任务尾部会一路催到空转刹车
    （用户看到的「连续多轮只输出占位文字或没有调用工具」）。
    """
    assert stepctx.auto_continue_reason(None, "这一版看起来可以了。", 9, True,
                                        "改音效", agent_mode=True) is None
    r = stepctx.auto_continue_reason(None, "我会插入 `ORE_DROPS` 定义，放在之前。",
                                     9, True, "改音效", agent_mode=True)
    assert r and r["why"] == "结尾说了要做但没调用工具"


def test_auto_continue_stops_when_blocked_by_missing_material():
    """现场 s_2eed idx630：计划挂着 pending，正文写「第 5 题题面始终没拿到」。

    这不是「没做完」而是「推不动」：续跑 3 轮无工具，最后撞上空转刹车。
    """
    plan = {"steps": [{"text": "补第 3 题解答", "status": "pending"}]}
    text = ("这一步确实无法继续：第 5 题的题面我始终没有拿到。\n\n"
            "其余 4 项都已完成。\n\n你拍来题干时，我随时补上第 3 题。")
    assert stepctx.auto_continue_reason(plan, text, 2, True, "做题",
                                        agent_mode=True) is None


def test_auto_continue_release_long_delivery_without_complete_words():
    """成篇交付 + 结尾没有动作承诺 → 放行（即使计划没回标 done）。

    现场 s_2809 idx678：2 万字说明，结尾是「双击 index.html，Ctrl+F5 强刷」，
    因为计划 5 步没回标 done 被连推 3 轮，正文最后被判成「没有结论」。
    """
    plan = {"steps": [{"text": "修手柄", "status": "doing"},
                      {"text": "补 e2e", "status": "pending"}]}
    text = "改动说明。" * 120 + "\n双击 `index.html`，**Ctrl+F5** 强刷。"
    # 计划还没推进时仍会催（避免把中途总结当成完成）
    assert stepctx.auto_continue_reason(plan, text, 144, True, "修手柄",
                                        agent_mode=True) is not None
    # 连续两轮计划一个字没变 → 不再拿「计划未完成」当理由
    assert stepctx.auto_continue_reason(plan, text, 144, True, "修手柄",
                                        agent_mode=True, plan_stale=2) is None


def test_promise_words_drop_ambiguous_closings():
    """「补上 / 看一下 / 确认一下」不再单独算承诺，带引导词的动作仍算"""
    assert stepctx._tail_promises_work("你确认一下页面效果。") is False
    assert stepctx._tail_promises_work("你拍来题干时，我随时补上第 3 题。") is False
    assert stepctx._tail_promises_work("接下来处理音效。") is True


def test_pending_decision_recognizes_decision_section():
    """最后一句不是问句，但整段是「需要你决定」的清单 → 也算待拍板。

    现场 s_2809 idx758：结尾是「你直接说按你的来，我就开写」，
    旧逻辑只盯最后一行，于是既没续跑、也没弹提问卡。
    """
    from app.api.chat import _needs_ask_nudge

    text = (
        "### 需要你决定\n"
        "三处都能自己定，但会影响手感，我列出来等你确认：\n"
        "1. **电磁线圈强度**：2.5 秒瘫痪 + 6 秒充能，会不会太强？（备选：2 秒 / 8 秒）\n"
        "2. **护盾挡成功后回复多少**：我按 60% 充能（备选：直接回满）\n"
        "3. **3 秒等待期**：要不要允许玩家继续拾取修复胶囊和合金？\n"
        "你直接说「按你的来」，我就照上面这套开写；也可以只改其中几条。"
    )
    assert stepctx._pending_decision(text) is True
    plan = {"steps": [{"text": "给方案", "status": "doing"},
                      {"text": "实现", "status": "pending"}]}
    # 待拍板时不能自动续跑
    assert stepctx.auto_continue_reason(plan, text, 21, True, "开发",
                                        agent_mode=True) is None
    # 且必须提醒模型改用提问卡
    assert _needs_ask_nudge(text, False, False) is True
    assert _needs_ask_nudge(text, True, False) is False

    # 普通交付说明不误判
    assert stepctx._pending_decision("改动完成。需要你确认一下效果。") is False
    assert stepctx._pending_decision("全部改好了，双击 index.html 即可。") is False
    assert stepctx._pending_decision("方案 A 和方案 B 都可以。") is False


def test_tool_progress_resets_plan_stale():
    """刚执行过工具就不能算「计划停滞」。

    现场 s_2809 最新一轮：模型连续读/写文件但没回标计划，auto_continues=4
    后被 plan_stale 误判成停滞，正文停在「运行该核对脚本。」不再续跑。
    """
    from app.api.chat import _next_plan_stale

    plan = {"steps": [{"text": "改 index.html", "status": "doing"},
                      {"text": "跑回归", "status": "pending"}]}
    text = "运行该核对脚本。"
    sig = (("doing", "改 index.html"), ("pending", "跑回归"))

    # 没有动作承诺的短句 + plan_stale >= 2：确实不再续跑
    assert stepctx.auto_continue_reason(plan, "说明。", 26, True, "开发",
                                        agent_mode=True, plan_stale=2) is None
    # 「运行该核对脚本。」现在命中句首动词表 → stale=2 也要按「说而不做」续跑
    assert stepctx.auto_continue_reason(plan, text, 26, True, "开发",
                                        agent_mode=True, plan_stale=2) is not None
    # 新行为：有工具进展 → 停滞计数归零 → 继续推计划
    assert _next_plan_stale(2, sig, sig, True) == 0
    r = stepctx.auto_continue_reason(plan, text, 26, True, "开发",
                                     agent_mode=True, plan_stale=0)
    assert r is not None and "计划" in r["why"]
    # 计划变了也算推进；两者都没有才累加
    assert _next_plan_stale(2, (("doing", "新描述"),), sig, False) == 0
    assert _next_plan_stale(2, sig, sig, False) == 3


def test_bare_action_promise_is_detected():
    """句首直接一个动作也算承诺：现场「查 applyTech 的调用点。」"""
    assert stepctx._tail_promises_work("查 `applyTech` 的调用点。") is True
    assert stepctx._tail_promises_work("读 `index.html` 的 2600 行。") is True
    # idx828 的漏判：「运行扫描脚本…」「扫描文档里实际存在的规模数字。」
    assert stepctx._tail_promises_work("运行扫描脚本，取得旧数字的确切位置。") is True
    assert stepctx._tail_promises_work("扫描文档里实际存在的规模数字。") is True
    assert stepctx._tail_promises_work("统计一下各页面的溢出量。") is True
    assert stepctx._tail_promises_work("你可以查看 docs。") is False
    assert stepctx._tail_promises_work("你可以扫描二维码。") is False
    assert stepctx._tail_promises_work("看效果。") is False

    plan = {"steps": [{"text": "写测试", "status": "doing"},
                      {"text": "回归", "status": "pending"}]}
    r = stepctx.auto_continue_reason(
        plan, "查 `applyTech` 的调用点。", 34, True, "开发",
        agent_mode=True, plan_stale=2)
    assert r is not None and "没调用工具" in r["why"]


def test_pending_plan_end_reason_fallback():
    """正常 stop + 计划还有步骤 + 最后一段没工具 → 收尾必须给可继续原因。"""
    from app.api.chat import _pending_plan_end_reason

    plan = {"steps": [{"text": "跑验证", "status": "doing"},
                      {"text": "同步文档", "status": "pending"}]}
    r = _pending_plan_end_reason(plan, "扫描文档里实际存在的规模数字。", False, False)
    assert r is not None and r["kind"] == "idle"
    assert "2 步" in r["text"] and r["action"] == "continue"
    # 最后一个工具轮（有调用）不能再报「没调工具」
    assert _pending_plan_end_reason(plan, "扫描文档。", True, False) is None
    # GAL / 明确完成 / 受阻 / 等拍板 / 成篇总结 都不误报
    assert _pending_plan_end_reason(plan, "随便", False, True) is None
    assert _pending_plan_end_reason(plan, "已经全部完成。", False, False) is None
    assert _pending_plan_end_reason(plan, "无法继续，缺少文件。", False, False) is None
    assert _pending_plan_end_reason(plan, "要不要按 A 做？", False, False) is None
    assert _pending_plan_end_reason(plan, "说明" * 200, False, False) is None
    # 计划已全部完成 → 不提示
    done = {"steps": [{"text": "a", "status": "done"}]}
    assert _pending_plan_end_reason(done, "短句。", False, False) is None


def test_done_claim_verdict_catches_virtual_tool_work():
    """正文说「已写好 / 现在运行」但本轮零工具 → 必须点明只是文字描述。"""
    from app.api.chat import _done_claim_verdict

    text = ("补丁脚本已写好。现在运行它，每处替换带「恰好命中 1 次」断言。"
            "运行补丁脚本。用 search_code 定位 applySyn。")
    warn = _done_claim_verdict(text, [], [])
    assert "没有成功执行任何工具" in warn and "文字描述" in warn
    # 有成功工具时不算虚报
    assert _done_claim_verdict(text, [{"tool": "run_command", "result": "ok"}], []) == ""
    # 有成功工具、但写操作全失败 → 另一条警告
    failed = [{"tool": "run_command", "write": True, "result": "❌ 失败"}]
    ok_read = {"tool": "read_file", "result": "ok"}
    assert "写操作全部失败" in _done_claim_verdict(text, [ok_read] + failed, [])


def test_pending_plan_end_reason_without_plan():
    """零工具 + 无计划 + 长正文：收尾必须给出原因，不能只留「完成时间」。"""
    from app.api.chat import _pending_plan_end_reason

    long_text = "补丁脚本已写好，现在运行它，然后读取结果。" * 20
    r = _pending_plan_end_reason({}, long_text, False, False, tool_count=0)
    assert r is not None and "没有执行任何工具" in r["text"]
    assert r["action"] == "continue"

    assert _pending_plan_end_reason({}, long_text, False, False, tool_count=3) is None
    assert _pending_plan_end_reason({}, "短句。", False, False, tool_count=0) is None
    done = {"steps": [{"text": "a", "status": "done"}]}
    assert _pending_plan_end_reason(done, long_text, False, False, tool_count=0) is None
    # 非 Agent 会话（无文件 / 命令能力）：纯文字回答不该被催「继续输出让它真正动手」
    assert _pending_plan_end_reason({}, long_text, False, False, tool_count=0,
                                    agent_mode=False) is None


def test_latest_plan_only_reads_last_assistant_message():
    """计划继承只看最后一条 assistant：旧任务的计划不能跨话题被「接着」带回来。"""
    from app.api.chat import _latest_plan

    old = {"steps": [{"text": "写 resume.html", "status": "pending"}]}
    s = {"messages": [
        {"role": "assistant", "content": "旧任务", "plan": old},
        {"role": "user", "content": "接着补基础"},
        {"role": "assistant", "content": "讲课"},
        {"role": "user", "content": "再接着"},
    ]}
    assert _latest_plan(s) is None
    # 最后一条 assistant 自己有计划 → 正常继承
    s["messages"][-2]["plan"] = old
    assert _latest_plan(s) is old


def test_meta_turn_detection():
    """用户在问「为什么 / 进度 / 是否完成」时，不能当成任务继续推。"""
    assert stepctx.is_meta_turn("为什么又中断了？任务到底完美完成") is True
    assert stepctx.is_meta_turn("现在做到哪了？") is True
    assert stepctx.is_meta_turn("总结一下这轮的进展") is True
    assert stepctx.is_meta_turn("为什么失败？帮我修一下") is False
    assert stepctx.is_meta_turn("继续做 S2") is False
    assert stepctx.is_meta_turn("") is False
    assert stepctx.is_meta_turn("为什么" + "x" * 300) is False   # 太长，按任务处理


def test_checkpoint_summary_allows_stop():
    """成段总结（默认 180 字）允许先收尾；一句「继续 S1」仍然续跑。"""
    plan = {"steps": [{"text": "S1", "status": "done"},
                      {"text": "S2", "status": "doing"},
                      {"text": "S3", "status": "pending"}]}
    summary = ("这一轮已完成 S1 与 S2：渲染循环已通过，键盘输入已接上，实测稳定 60fps。"
               "目前剩下 S3 碰撞与 S4 方块交互；全量回归 36 套 / 943 PASS / 0 FAIL。"
               "已经把探针测量值同步到日志，验证通过。") * 2
    assert stepctx.is_checkpoint_summary(summary, 180) is True
    assert stepctx.is_checkpoint_summary("继续。S1：核对 index.html。", 180) is False
    assert stepctx.is_checkpoint_summary(summary, 0) is False        # 0 = 关闭

    # 成段总结 + 计划还有未完成步骤 → 不续跑（允许先收尾）
    assert stepctx.auto_continue_reason(
        plan, summary, 10, True, "开发", agent_mode=True, plan_stale=0,
        summary_stop_chars=180) is None
    # 关闭阈值后回到旧行为：计划还有步骤 → 继续推
    assert stepctx.auto_continue_reason(
        plan, summary, 10, True, "开发", agent_mode=True, plan_stale=0,
        summary_stop_chars=0) is not None
    # 一句「继续 S1」不算总结，仍然续跑
    assert stepctx.auto_continue_reason(
        plan, "继续。S1：核对 index.html。", 10, True, "开发",
        agent_mode=True, plan_stale=0, summary_stop_chars=180) is not None


def test_plan_ready_steps_and_hint():
    """ready queue：doing 优先，deps 未完成的 pending 不能提前做。"""
    plan = {
        "milestones": [{"id": "M1", "title": "骨架"}, {"id": "M2", "title": "体素世界"}],
        "steps": [
            {"id": "M1S1", "text": "渲染循环", "status": "done", "milestone": "M1"},
            {"id": "M1S2", "text": "键盘输入", "status": "done", "milestone": "M1"},
            {"id": "M2S1", "text": "区块生成", "status": "pending", "milestone": "M2",
             "deps": ["M1S2"], "verify": "bench p95<20"},
            {"id": "M2S2", "text": "材质图集", "status": "pending", "milestone": "M2",
             "deps": ["M1S2"], "verify": "截图正常"},
            {"id": "M2S3", "text": "方块交互", "status": "pending", "milestone": "M2",
             "deps": ["M2S1"]},
        ],
    }
    ready = stepctx.ready_steps(plan, 3)
    assert [s["id"] for s in ready] == ["M2S1", "M2S2"]
    # 依赖没完成的那一步不能提前放进 ready
    assert "M2S3" not in [s["id"] for s in ready]

    hint = stepctx.plan_ready_hint(plan, 3)
    assert "M2S1" in hint and "bench p95<20" in hint
    assert "M2S2" in hint and "可同一轮并行" in hint
    assert "M2S3" not in hint

    # doing 优先
    plan["steps"][4]["status"] = "doing"
    assert stepctx.ready_steps(plan, 3)[0]["id"] == "M2S3"


def test_auto_continue_names_ready_step():
    plan = {"steps": [
        {"id": "S1", "text": "读 A", "status": "done"},
        {"id": "S2", "text": "改 B", "status": "pending", "verify": "跑测试"},
    ]}
    r = stepctx.auto_continue_reason(plan, "继续。S2：改 B。", 3, True, "开发",
                                     agent_mode=True, plan_stale=0)
    assert r is not None
    assert "S2" in r["msg"] and "跑测试" in r["msg"]
    assert "可同一轮并行" not in r["msg"]       # 只有一条 ready 时不该标并行


def test_plan_ready_hint_prefers_same_milestone_slice():
    """一轮做一个切片：同一里程碑的步骤优先凑满，不跨里程碑来回跳。"""
    plan = {
        "milestones": [{"id": "M1", "title": "骨架"}, {"id": "M2", "title": "体素"}],
        "steps": [
            {"id": "M1S1", "text": "渲染循环", "status": "pending", "milestone": "M1"},
            {"id": "M2S1", "text": "区块生成", "status": "pending", "milestone": "M2"},
            {"id": "M1S2", "text": "键盘输入", "status": "pending", "milestone": "M1"},
        ],
    }
    assert [s["id"] for s in stepctx.ready_steps(plan, 2)] == ["M1S1", "M1S2"]
    hint = stepctx.plan_ready_hint(plan, 2)
    assert "一个切片" in hint or "一起做完" in hint
    assert "M1" in hint

    # 只有一条 ready 时不催「并行 / 切片」
    one = {"steps": [{"id": "S1", "text": "读 A", "status": "pending"}]}
    h1 = stepctx.plan_ready_hint(one, 3)
    assert "切片" not in h1 and "不要一步一轮" not in h1


def test_plan_retro_hint_only_after_all_done():
    """收尾复盘：只有整份计划全部 done、且还没写 retro 时才催。"""
    pending = {"steps": [{"id": "S1", "text": "a", "status": "done"},
                         {"id": "S2", "text": "b", "status": "pending"}]}
    assert stepctx.plan_retro_hint(pending) == ""

    done = {"steps": [{"id": "S1", "text": "a", "status": "done"}]}
    hint = stepctx.plan_retro_hint(done)
    assert "retro" in hint and "经验" in hint

    blocked = {"steps": [{"id": "S1", "text": "a", "status": "blocked"}]}
    assert stepctx.plan_retro_hint(blocked) == ""

    assert stepctx.plan_retro_hint(dict(done, retro="结果：OK")) == ""
    assert stepctx.plan_retro_hint({}) == ""


def test_plan_replan_hint_needs_two_stale_rounds():
    """停滞 >=2 轮才强制重写计划；1 轮仍走软提示（plan_health_hint）。"""
    assert stepctx.plan_replan_hint(0) == ""
    assert stepctx.plan_replan_hint(1) == ""
    hint = stepctx.plan_replan_hint(2)
    assert "update_plan" in hint and "连续 2 轮" in hint
    assert "只补 log" in hint


def test_plan_accept_gaps_and_hint():
    """计划 ↔ 验收联动：只报未通过 / 不存在的验收项。"""
    from app.api.chat import _plan_accept_gaps, _plan_accept_hint

    plan = {"steps": [
        {"id": "S1", "text": "改渲染", "status": "done", "accept": ["a1", "a2"]},
        {"id": "S2", "text": "补测试", "status": "done", "accept": ["a9"]},
        {"id": "S3", "text": "无验收", "status": "done"},
        {"id": "S4", "text": "还没开始", "status": "pending", "accept": ["a2"]},
    ]}
    criteria = [{"id": "a1", "status": "passed"}, {"id": "a2", "status": "pending"}]
    gaps = _plan_accept_gaps(plan, criteria)
    assert ("S1", "a2", "pending") in gaps
    assert ("S2", "a9", "missing") in gaps
    assert len(gaps) == 2                     # a1 通过，不算差距

    txt = _plan_accept_hint(gaps)
    assert "acceptance" in txt and "a2" in txt and "不存在" in txt
    assert _plan_accept_hint([]) == ""


def test_plan_sync_gaps_marks_unreconciled_steps():
    """计划回标：正文说某步完成、计划里还没标 done 才报。"""
    plan = {
        "steps": [
            {"id": "S1", "text": "改渲染", "status": "done"},
            {"id": "S4", "text": "全量回归", "status": "pending"},
            {"id": "S5", "text": "同步文档", "status": "pending"},
            {"id": "M1S2", "text": "补测试", "status": "doing"},
        ]
    }
    # 现场 s_2809 idx904 原话：说完 S4 完成，计划里还是 pending
    text = "S4 已完成。我更新计划，并进入 S5。"
    assert stepctx.plan_sync_gaps(plan, text) == ["S4"]

    assert stepctx.plan_sync_gaps(plan, "S1 已通过，接着做 S4。") == []
    assert stepctx.plan_sync_gaps(plan, "S4 还没完成，需要重跑。") == []
    assert stepctx.plan_sync_gaps(plan, "M1S2 已通过（0 FAIL）。") == ["M1S2"]
    # 变量名里的 S 后面没有数字，不能命中
    assert stepctx.plan_sync_gaps(plan, "S.dmgDealt 已经加好。") == []

    hint = stepctx.plan_sync_hint(plan, text)
    assert "计划回标" in hint and "S4" in hint
    assert stepctx.plan_sync_hint(plan, "没有提到步骤。") == ""
    assert stepctx.plan_sync_hint({}, text) == ""


def test_plan_pending_line_names_left_steps():
    """收尾文案点名没做完的步骤，而不是只说「模型没有继续调用工具」。"""
    plan = {"steps": [
        {"id": "S1", "text": "改渲染", "status": "done"},
        {"id": "S2", "text": "跑全量回归确认零失败", "status": "pending"},
        {"id": "S3", "text": "同步 README 与 NOTES", "status": "pending"},
        {"id": "S4", "text": "补测试", "status": "pending"},
        {"id": "S5", "text": "等用户决定", "status": "blocked"},
    ]}
    line = stepctx.plan_pending_line(plan)
    assert line.startswith("未完成：")
    assert "跑全量回归" in line and "同步 README" in line
    assert "另有 1 步" in line              # S4 被省略
    assert "等用户决定" not in line          # blocked 不算未完成
    assert stepctx.plan_pending_line({"steps": []}) == ""


def test_tool_fail_brief_picks_fail_line():
    """续跑 / 收尾要带失败信息时，优先取 FAIL 行，其次退出码。"""
    from app.api.chat import _tool_fail_brief

    entry = {"tool": "run_command", "ok": False, "result": (
        "命令：node _dev/events_e2e.js\n工作目录：.\n退出码：1（用时 0.1s）\n输出：\n"
        "=== A. 表结构 ===\n  PASS A1 条目数 = 8\n"
        "=== E. scoreMul ===\n  FAIL E1 scoreMul 未生效\n")}
    txt = _tool_fail_brief(entry)
    assert txt.startswith("run_command：") and "FAIL E1" in txt
    assert "FAIL E1" in _tool_fail_brief(entry, 90)

    only_code = {"tool": "run_command", "ok": False,
                 "result": "命令：node x.js\n工作目录：.\n退出码：2（用时 0.1s）\n输出：\n"}
    assert "退出码：2" in _tool_fail_brief(only_code)
    assert _tool_fail_brief(None) == ""


def test_plan_can_inherit_only_on_continuation():
    assert stepctx.plan_can_inherit("继续") is True
    assert stepctx.plan_can_inherit("接着改 S2") is True
    assert stepctx.plan_can_inherit("好") is True
    assert stepctx.plan_can_inherit("开始") is True
    assert stepctx.plan_can_inherit("为什么又中断了？", meta=True) is True
    assert stepctx.plan_can_inherit("继续", cont=True) is True
    assert stepctx.plan_can_inherit("把颜色改成蓝色") is False
    assert stepctx.plan_can_inherit("再做一个新游戏") is False
    assert stepctx.plan_can_inherit("换个需求") is False
    assert stepctx.plan_can_inherit("") is False


def test_plan_health_hint_triggers():
    """计划体检：里程碑完成 > 连续失败 > 计划停滞 > 6 轮没更新。"""
    plan = {"steps": [{"text": "a", "status": "doing", "milestone": "M1"},
                      {"text": "b", "status": "pending", "milestone": "M2"}],
            "milestones": [{"id": "M1", "title": "骨架"}, {"id": "M2", "title": "世界"}]}
    # 里程碑完成：要求展开下一个
    txt = stepctx.plan_health_hint(plan, milestone_done="可运行的行走骨架")
    assert "里程碑" in txt and "plan_patch" in txt
    # 连续失败：要求拆步 / blocked
    txt = stepctx.plan_health_hint(plan, recent_failures=2)
    assert "失败" in txt and "blocked" in txt
    # 计划停滞：要求记录 discovery 或说明计划不变
    txt = stepctx.plan_health_hint(plan, plan_stale=1)
    assert "没有变化" in txt and "discovery" in txt
    # 6 轮没更新
    txt = stepctx.plan_health_hint(plan, rounds_since_update=6)
    assert "6 轮" in txt
    # 没有计划 / 没触发 → 空串
    assert stepctx.plan_health_hint({}) == ""
    assert stepctx.plan_health_hint(plan) == ""
    # 脏参数不炸
    assert stepctx.plan_health_hint(plan, recent_failures="x") == ""


def test_plan_stagnant_needs_visible_reason():
    """计划停滞且没有完成/受阻/待拍板说明 → 收尾必须给可见原因。"""
    from app.api.chat import _plan_stagnant

    plan = {"steps": [{"text": "写测试", "status": "doing"},
                      {"text": "回归", "status": "pending"}]}
    assert _plan_stagnant(plan, "查 `applyTech` 的调用点。", 2, None) is True
    assert _plan_stagnant(plan, "已完成。", 2, None) is False
    assert _plan_stagnant(plan, "无法继续，缺少文件。", 2, None) is False
    assert _plan_stagnant(plan, "要不要按 A 做？", 2, None) is False
    assert _plan_stagnant(plan, "随便", 1, None) is False
    assert _plan_stagnant({"steps": [{"text": "a", "status": "done"}]},
                          "随便", 2, None) is False


def test_auto_continue_release_complete_with_long_summary():
    """计划没回标 done、但写了成篇的完成结论 → 放行，别再推成空转。

    现场：160 个工具之后模型写 5155 字正文，计划里还挂着 doing，
    续跑两次都不动手，最后撞上「空转」刹车，任务反而断在半路。
    """
    plan = {"steps": [{"text": "改渲染", "status": "doing"}]}
    long_text = "细节说明。" * 60 + "已完成：渲染管线修好了，截图正常。"
    assert stepctx.auto_continue_reason(plan, long_text, 5, True, "实现",
                                        agent_mode=True) is None
    # 只有一句「已完成」不放行：可能只是「第一步完成了」
    assert stepctx.auto_continue_reason(plan, "已完成。", 5, True, "实现",
                                        agent_mode=True) is not None
    # 结尾还在承诺下一步时也不放行
    tail = "已完成渲染修复。" + "细节说明。" * 60 + "接下来处理音效。"
    assert stepctx.auto_continue_reason(plan, tail, 5, True, "实现",
                                        agent_mode=True) is not None


def test_promise_regex_ignores_plain_narration():
    """正则只认句首动作引导词，不把普通叙述当承诺"""
    assert stepctx._tail_promises_work("然后我们就得到了一个三角形。") is False
    assert stepctx._tail_promises_work("然后重新测量。") is True


def test_memory_organize_hint_triggers_on_limit_and_rounds():
    """每 N 轮提醒一次整理；只有超软上限 / 关系为 0 时才真的注入。"""
    # 到 20 的倍数 + 超软上限 → 提醒
    text = stepctx.memory_organize_hint(20, 60, 5, 60, 20)
    assert "【记忆整理】" in text and "60 条" in text and "5 条关系" in text
    assert "purpose" in text and "sequence" in text
    # 没到倍数 → 不提醒
    assert stepctx.memory_organize_hint(19, 60, 5, 60, 20) == ""
    # 到倍数但没超上限、也有关系 → 不提醒
    assert stepctx.memory_organize_hint(20, 10, 3, 60, 20) == ""
    # 一条关系都没有、条目超过 20 → 起个头
    assert "【记忆整理】" in stepctx.memory_organize_hint(40, 21, 0, 60, 20)
    # 关掉（every=0）或软上限 0（= 不限）时不提醒
    assert stepctx.memory_organize_hint(20, 999, 5, 60, 0) == ""
    assert stepctx.memory_organize_hint(20, 999, 5, 0, 20) == ""
    # 脏参数不炸
    assert stepctx.memory_organize_hint(None, "x", [], 60, 20) == ""
