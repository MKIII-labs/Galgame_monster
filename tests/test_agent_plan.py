# -*- coding: utf-8 -*-
"""执行计划参数规范化（update_plan）。"""
from app import agent


def test_plan_valid():
    plan, err = agent.normalize_plan({"steps": [
        {"text": "读 chat.js", "status": "done"},
        {"text": "改折叠逻辑", "status": "doing"},
        {"text": "跑测试", "status": "pending"},
    ]})
    assert err == ""
    assert [s["status"] for s in plan["steps"]] == ["done", "doing", "pending"]


def test_plan_accepts_strings_and_bad_status():
    plan, err = agent.normalize_plan({"steps": ["第一步", "第二步"]})
    assert err == ""
    assert [s["status"] for s in plan["steps"]] == ["pending", "pending"]

    plan2, _ = agent.normalize_plan({"steps": [{"text": "x", "status": "running"}]})
    assert plan2["steps"][0]["status"] == "pending"


def test_plan_dict_step_and_empty():
    plan, err = agent.normalize_plan({"steps": {"text": "单步", "status": "done"}})
    assert err == "" and plan["steps"][0]["text"] == "单步"

    bad, err2 = agent.normalize_plan({"steps": []})
    assert bad is None and "缺少 steps" in err2


def test_plan_caps_steps_and_text():
    many = [{"text": "x%d" % i, "status": "pending"} for i in range(40)]
    plan, _ = agent.normalize_plan({"steps": many})
    assert len(plan["steps"]) == agent.PLAN_MAX_STEPS

    long_text = "很长" * 200
    plan2, _ = agent.normalize_plan({"steps": [{"text": long_text}]})
    assert len(plan2["steps"][0]["text"]) <= agent.PLAN_MAX_TEXT + 1   # 末尾省略号


def test_plan_v2_milestones_and_ids():
    plan, err = agent.normalize_plan({
        "goal": "还原 Minecraft 核心玩法",
        "done_when": ["能移动", "能破坏方块"],
        "milestones": [
            {"title": "可运行的行走骨架", "done_when": "WASD 可动",
             "steps": [
                 {"text": "搭渲染循环", "status": "done", "verify": "控制台无报错"},
                 {"text": "接键盘输入", "status": "doing", "verify": "WASD 移动"},
             ]},
            {"title": "体素世界", "steps": [
                {"text": "区块生成", "verify": "bench p95 < 20ms"},
            ]},
        ],
    })
    assert err == ""
    assert plan["goal"].startswith("还原")
    assert plan["done_when"] == ["能移动", "能破坏方块"]
    assert plan["revision"] == 1
    assert [m["id"] for m in plan["milestones"]] == ["M1", "M2"]
    assert [s["id"] for s in plan["steps"]] == ["M1S1", "M1S2", "M2S1"]
    assert plan["steps"][0]["milestone"] == "M1"
    assert plan["steps"][0]["status"] == "done"
    assert plan["steps"][2]["verify"] == "bench p95 < 20ms"


def test_plan_patch_updates_and_logs():
    plan, _ = agent.normalize_plan({
        "milestones": [{"title": "骨架", "steps": [
            {"text": "渲染循环", "status": "doing"},
            {"text": "键盘输入", "status": "pending"},
        ]}],
    })
    newp, err, summary = agent.apply_plan_patch(plan, {
        "reason": "先补主循环，键盘输入依赖它",
        "update_steps": [{"id": "M1S2", "status": "blocked"}],
        "add_steps": [{"milestone": "M1", "text": "补 requestAnimationFrame", "verify": "60fps"}],
        "log": [{"kind": "discovery", "text": "浏览器需要 rAF 才能稳定 60fps"}],
    })
    assert err == "" and newp["revision"] == 2
    assert "v2" in summary
    by = {s["id"]: s for s in newp["steps"]}
    assert by["M1S2"]["status"] == "blocked"
    assert len(newp["steps"]) == 3
    assert any("先补主循环" in x["text"] for x in newp["log"]["decisions"])
    assert any("rAF" in x["text"] for x in newp["log"]["discoveries"])


def test_plan_patch_requires_existing_plan():
    newp, err, summary = agent.apply_plan_patch(None, {"reason": "x"})
    assert newp is None and "还没有计划" in err and summary == ""


def test_plan_log_accepts_dict_of_strings():
    """log 用字典形式（键=kind、值=文本）时不能崩。

    历史崩溃点：dict(v, kind=k) 把字符串按字符拆键值对，
    抛 "dictionary update sequence element #0 has length 1"。
    """
    plan, err = agent.normalize_plan({
        "steps": [{"text": "收尾复盘", "status": "doing"}],
        "log": {"decisions": "先写复盘再收尾",
                "discoveries": "股票子系统 5 个测试文件全绿"},
    })
    assert err == ""
    assert [x["text"] for x in plan["log"]["decisions"]] == ["先写复盘再收尾"]
    assert [x["text"] for x in plan["log"]["discoveries"]] == ["股票子系统 5 个测试文件全绿"]


def test_plan_log_ignores_non_string_values():
    """字典里非字符串的值保持忽略，而不是让整轮崩掉。"""
    plan, err = agent.normalize_plan({
        "steps": [{"text": "x"}],
        "log": {"decisions": ["不是字符串"],
                "discoveries": {"text": "对象形式不在这里支持"}},
    })
    assert err == ""
    assert plan["log"] == {"decisions": [], "discoveries": []}


def test_plan_schema_exposed():
    names = [t["function"]["name"]
             for t in agent.native_tool_schemas(agent_on=True, search_ready=True)]
    assert "plan" in names
    assert "read" in names and "code" in names

    # /plan 只读：写 / 命令 / 工作流 schema 都不下发
    ro = [t["function"]["name"]
          for t in agent.native_tool_schemas(agent_on=True, search_ready=True,
                                             plan_mode=True)]
    assert "plan" in ro and "read" in ro
    assert "edit" not in ro and "run" not in ro
    assert "workflow" not in ro


def test_plan_step_ref_roundtrip():
    """ref 是「这一步的证据引用」，update_plan / plan_patch 都要能读写。"""
    plan, err = agent.normalize_plan({"milestones": [{"title": "骨架", "steps": [
        {"text": "改渲染", "ref": "web/chat.js:1523"},
    ]}]})
    assert err == "" and plan["steps"][0]["ref"] == "web/chat.js:1523"

    newp, perr, _ = agent.apply_plan_patch(plan, {
        "update_steps": [{"id": "M1S1", "ref": "acceptance:ui-1"}],
        "add_steps": [{"text": "补测试", "ref": "tests/test_stepctx.py"}],
    })
    assert perr == ""
    by = {s["id"]: s for s in newp["steps"]}
    assert by["M1S1"]["ref"] == "acceptance:ui-1"
    assert by["S2"]["ref"] == "tests/test_stepctx.py"


def test_plan_accept_and_retro_roundtrip():
    """步骤挂验收项 id、整份计划写复盘：两个字段都要能被 update_plan / plan_patch 读写。"""
    plan, err = agent.normalize_plan({
        "steps": [{"text": "改渲染", "accept": ["a1", "a2"]}],
        "retro": "结果：页面正常；偏差：没做移动端；经验：先跑冒烟再改样式。",
    })
    assert err == "" and plan["steps"][0]["accept"] == ["a1", "a2"]
    assert plan["retro"].startswith("结果：")

    newp, perr, _ = agent.apply_plan_patch(plan, {
        "update_steps": [{"id": "S1", "accept": "a3"}],
        "retro": "结果：通过；偏差：无；经验：验收项挂到步骤上更省事。",
    })
    assert perr == ""
    assert newp["steps"][0]["accept"] == ["a3"]
    assert "验收项挂到步骤上" in newp["retro"]
    # 只写 retro 也算有效改动（不能报「没有可执行的改动」）
    only_retro, rerr, _ = agent.apply_plan_patch(newp, {"retro": "补充一条经验"})
    assert rerr == "" and only_retro["retro"] == "补充一条经验"


def test_plan_brief_points_to_current_step():
    plan = {"steps": [
        {"text": "读 chat.js", "status": "done"},
        {"text": "改折叠", "status": "doing"},
        {"text": "跑测试", "status": "pending"},
    ]}
    brief = agent.plan_brief(plan)
    assert "1/3" in brief and "正在：改折叠" in brief and "还剩 2 步" in brief

    done = {"steps": [{"text": "a", "status": "done"}]}
    assert "已全部完成" in agent.plan_brief(done)
    assert agent.plan_brief({}) == ""
