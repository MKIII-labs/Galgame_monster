# -*- coding: utf-8 -*-
"""写后自检 / 项目规则文件 / 整轮回滚 / 写入审批。"""
import threading
import time

from app import agent
from app.api import chat
from app.api import sandbox as sandbox_api


# ---------------------------------------------------------------- 审批通知
def test_approval_notice_uses_call_summary(monkeypatch):
    """审批事件要带「先看一眼」摘要：通知里直接说清楚要执行什么。"""
    from app import events
    seen = []
    monkeypatch.setattr(events, "publish", lambda obj: seen.append(obj))

    aid = agent.new_approval("s_notice", "run_command", {"argv": ["git", "status"]})
    ev = [o for o in seen if o.get("type") == "session_pending"][-1]
    assert ev["kind"] == "approval"
    assert "执行 git status" in ev["text"]
    agent.resolve_approval(aid, False)
    agent.wait_approval(aid, timeout=0.2)

    # 子代理回写：用文件数 / 增删行摘要，而不是工具名 apply_worktree
    aid2 = agent.new_approval("s_notice", "apply_worktree",
                              {"agent": "writer", "summary": "3 个文件（+10 / −2）"})
    ev = [o for o in seen if o.get("type") == "session_pending"][-1]
    assert ev["text"] == "回写子代理改动：3 个文件（+10 / −2）"
    agent.resolve_approval(aid2, False)
    agent.wait_approval(aid2, timeout=0.2)


# ---------------------------------------------------------------- 写后自检
def test_post_write_check_python(tmp_path):
    p = tmp_path / "a.py"
    p.write_text("def f(:\n    pass\n", encoding="utf-8")
    err = agent.post_write_check(str(p))
    assert "行" in err and "第" in err

    p.write_text("def f():\n    return 1\n", encoding="utf-8")
    assert agent.post_write_check(str(p)) == ""


def test_post_write_check_json(tmp_path):
    p = tmp_path / "a.json"
    p.write_text("{bad json", encoding="utf-8")
    assert agent.post_write_check(str(p))
    p.write_text('{"ok": true}', encoding="utf-8")
    assert agent.post_write_check(str(p)) == ""


def test_run_tool_appends_selfcheck_error(tmp_path):
    text, undo, is_write = agent.run_tool(
        str(tmp_path), "write_file",
        {"path": "bad.py", "content": "def f(:\n"})
    assert is_write and undo
    assert "【自检】" in text


def test_run_tool_clean_write_has_no_selfcheck(tmp_path):
    text, undo, is_write = agent.run_tool(
        str(tmp_path), "write_file",
        {"path": "ok.py", "content": "x = 1\n"})
    assert is_write and undo
    assert "【自检】" not in text


# ---------------------------------------------------------------- 项目规则
def test_project_rules_reads_both_files(tmp_path):
    (tmp_path / "AGENTS.md").write_text("规则A：改完更新文档", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("规则B：注释用中文", encoding="utf-8")
    txt = agent.project_rules(str(tmp_path))
    assert "规则A" in txt and "规则B" in txt
    assert "AGENTS.md" in txt and "CLAUDE.md" in txt


def test_project_rules_truncated(tmp_path):
    (tmp_path / "AGENTS.md").write_text("x" * (agent.RULES_MAX_CHARS + 500),
                                        encoding="utf-8")
    txt = agent.project_rules(str(tmp_path))
    assert "已截断" in txt
    assert len(txt) < agent.RULES_MAX_CHARS + 200


def test_build_agent_prompt_injects_rules(tmp_path):
    (tmp_path / "AGENTS.md").write_text("必须为改动补测试", encoding="utf-8")
    s = {"agent_enabled": True, "sandbox": str(tmp_path)}
    prompt = agent.build_agent_prompt(s, search_ready=False)
    assert "必须为改动补测试" in prompt
    # 动态内容（规则 / 目录树）放在最末尾，前面才是稳定的缓存前缀
    assert "沙箱目录树" in prompt[-1500:]
    assert prompt.index("必须为改动补测试") > prompt.index("## 文件 Agent")
    assert prompt.index("必须为改动补测试") < prompt.index("沙箱目录树")


def test_compact_prompt_is_shorter_but_has_tools(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    s = {"agent_enabled": True, "sandbox": str(tmp_path)}
    verbose = agent.build_agent_prompt(s, search_ready=True, native_tools=False)
    compact = agent.build_agent_prompt(s, search_ready=True, native_tools=True)
    assert len(compact) < len(verbose)
    for name in ("read(action=", "search", "map", "edit(action="):
        assert name in compact


def test_tree_cache_and_invalidate(tmp_path):
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    first = agent.tree(str(tmp_path))
    again = agent.tree(str(tmp_path))
    assert again is first                      # 命中 3 秒缓存
    (tmp_path / "b.txt").write_text("y", encoding="utf-8")
    agent.invalidate_tree(str(tmp_path))
    names = [n["name"] for n in agent.tree(str(tmp_path))["children"]]
    assert "b.txt" in names


# ---------------------------------------------------------------- 整轮回滚
def test_turn_undo_restores_all_writes(tmp_path):
    sid = "s_turn_undo"
    agent.turn_begin(sid)
    p1 = tmp_path / "a.txt"
    p1.write_text("old-a", encoding="utf-8")
    p2 = tmp_path / "b.txt"          # 本轮新建
    ops1 = [agent._snap(str(p1))]
    p1.write_text("new-a", encoding="utf-8")
    agent.register_undo(sid, ops1, "edit_file")
    ops2 = [agent._snap(str(p2))]
    p2.write_text("new-b", encoding="utf-8")
    agent.register_undo(sid, ops2, "write_file")

    done, failed = agent.turn_undo(sid)
    assert (done, failed) == (2, 0)
    assert p1.read_text(encoding="utf-8") == "old-a"
    assert not p2.exists()
    assert agent.turn_undo(sid) == (0, 0)      # 同一轮只能回滚一次


# ---------------------------------------------------------------- 审批
def test_approval_remember_flag():
    aid = agent.new_approval("s", "write_file", {"path": "a.py"})
    threading.Timer(0.05, lambda: agent.resolve_approval(aid, True, remember=True)).start()
    assert agent.wait_approval(aid, timeout=2) is True
    assert agent.approval_remember(aid) is True
    assert agent.approval_remember(aid) is False          # 取过一次就清掉


def test_normalize_approval_aliases():
    assert agent.normalize_approval("") == "ai"
    assert agent.normalize_approval("auto") == "ai"      # 旧默认值 = 自动审批
    assert agent.normalize_approval("once") == "ask"
    assert agent.normalize_approval("always") == "ai"
    assert agent.normalize_approval("AI") == "ai"
    assert agent.normalize_approval("nonsense") == "ai"


def test_judge_write_modes(tmp_path):
    root = str(tmp_path)
    existing = tmp_path / "a.txt"
    existing.write_text("old", encoding="utf-8")
    new_file = {"path": "b.txt", "content": "x"}
    assert agent.judge_write(root, "write_file", new_file, "ask")[0] == "ask"
    assert agent.judge_write(root, "write_file", new_file, "full")[0] == "allow"

    # 自动审批：低 / 中风险直接放行（不再按次数、也不按替换行数）
    assert agent.judge_write(root, "write_file", new_file, "ai")[0] == "allow"
    assert agent.judge_write(root, "make_dir", {"path": "sub"}, "ai")[0] == "allow"
    assert agent.judge_write(root, "edit_file",
                             {"path": "a.txt", "old": "x\n" * 60, "new": "y"}, "ai")[0] == "allow"
    # 高风险才问
    assert agent.judge_write(root, "write_file", {"path": "a.txt", "content": "y"}, "ai")[0] == "ask"
    assert agent.judge_write(root, "delete_file", {"path": "a.txt"}, "ai")[0] == "ask"
    assert agent.judge_write(root, "move_file",
                             {"from": "a.txt", "to": "b.txt"}, "ai")[0] == "ask"
    assert agent.judge_write(root, "lsp_rename", {"path": "a.txt", "new": "b"}, "ai")[0] == "ask"


def test_judge_write_no_turn_count_limit(tmp_path):
    """连续写很多次也不再转人工：判定只看动作风险。"""
    root = str(tmp_path)
    for i in range(12):
        assert agent.judge_write(root, "write_file",
                                 {"path": "f%d.txt" % i, "content": "x"}, "ai")[0] == "allow"


def test_judge_write_rules(tmp_path):
    root = str(tmp_path)
    gm = tmp_path / ".gm"
    gm.mkdir()
    rules = gm / "agent-rules.json"
    rules.write_text('{"allow": ["docs/**"], "ask": ["*.md"], "deny": ["secret/**"]}',
                     encoding="utf-8")
    assert agent.judge_write(root, "write_file", {"path": "docs/a.txt"}, "custom")[0] == "allow"
    assert agent.judge_write(root, "write_file", {"path": "notes/a.md"}, "custom")[0] == "ask"
    assert agent.judge_write(root, "write_file", {"path": "secret/a.txt"}, "custom") == (
        "deny", "规则禁止：secret/**")
    # 未命中规则：自定义档按「必须询问」兜底
    assert agent.judge_write(root, "write_file", {"path": "src/a.py"}, "custom")[0] == "ask"
    # 规则在自动审批档同样生效
    assert agent.judge_write(root, "write_file", {"path": "secret/a.txt"}, "ai")[0] == "deny"

    rules.write_text("{bad json", encoding="utf-8")
    decision, why = agent.judge_write(root, "write_file", {"path": "docs/a.txt"}, "custom")
    assert decision == "ask" and "无法解析" in why      # 规则写坏了不能变成放行


# ---------------------------------------------------------------- 失败熔断 / cwd
def test_failure_signature_ignores_volatile_args():
    a = agent.failure_signature(
        "render_check",
        {"path": "x.html", "viewport": "1280x800", "screenshot": True, "timeout": 90},
        "❌ TIMEOUT")
    b = agent.failure_signature(
        "render_check",
        {"path": "x.html", "viewport": "720x450", "screenshot": False, "timeout": 30},
        "❌ TIMEOUT")
    assert a == b
    c = agent.failure_signature(
        "render_check",
        {"path": "y.html", "viewport": "1280x800"}, "❌ TIMEOUT")
    assert a != c


def test_failure_streak_resets_on_different_target():
    state = {"key": "", "n": 0}
    assert agent.failure_streak(state, "a") == 1
    assert agent.failure_streak(state, "a") == 2
    assert agent.failure_streak(state, "b") == 1
    assert agent.failure_streak(state, "b", limit=0) == 0


def test_clean_cwd_strips_json_artifacts():
    assert agent.clean_cwd('."]') == "."
    assert agent.clean_cwd('"webcraft"') == "webcraft"
    assert agent.clean_cwd(" . ") == "."
    assert agent.clean_cwd("") == "."


def test_sandbox_warnings_for_dirty_repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    from app import git
    monkeypatch.setattr(git, "status", lambda root: {
        "branch": "main",
        "files": [{"status": "D"}, {"status": "U", "untracked": True}],
    })
    warns = agent.sandbox_warnings(str(tmp_path))
    assert warns and "Git 仓库" in warns[0]
    assert "1 个删除" in warns[0] and "1 个未跟踪" in warns[0]

    monkeypatch.setattr(git, "status", lambda root: {"branch": "main", "files": []})
    assert agent.sandbox_warnings(str(tmp_path)) == []


def test_sandbox_warnings_without_repo(tmp_path):
    assert agent.sandbox_warnings(str(tmp_path)) == []


def test_approval_allow_and_timeout():
    aid = agent.new_approval("s", "write_file", {"path": "a.py"})
    threading.Timer(0.05, lambda: agent.resolve_approval(aid, True)).start()
    assert agent.wait_approval(aid, timeout=2) is True

    aid2 = agent.new_approval("s", "delete_file", {})
    threading.Timer(0.05, lambda: agent.resolve_approval(aid2, False)).start()
    assert agent.wait_approval(aid2, timeout=2) is False

    t0 = time.time()
    assert agent.wait_approval("p_missing", timeout=0.2) is None
    assert time.time() - t0 < 1.0


def test_judge_command_modes(tmp_path):
    """命令审批四档：自动审批按风险判定，不再按本轮次数。"""
    root = str(tmp_path)
    assert agent.judge_command(root, {"argv": ["git", "status"]}, "ask")[0] == "ask"
    assert agent.judge_command(root, {"argv": ["git", "status"]}, "full")[0] == "allow"
    assert agent.judge_command(root, {"argv": ["git", "status"]}, "ai")[0] == "allow"
    assert agent.judge_command(root, {"argv": ["pip", "install", "x"]}, "ai")[0] == "ask"
    assert agent.judge_command(root, {"argv": ["git", "push"]}, "ai")[0] == "ask"

    # 完全放权：白名单外的 exe / 串联符号都不再拦（沙箱 cwd 仍生效）
    assert agent.judge_command(root, {"argv": ["cargo", "build"]}, "full")[0] == "allow"
    assert agent.judge_command(root, {"cmd": "git status | more"}, "full")[0] == "allow"
    assert agent.judge_command(root, {"cmd": 'python -c "print(1)"'}, "full")[0] == "allow"
    # 空命令仍按非法参数拒绝
    assert agent.judge_command(root, {"argv": []}, "full")[0] == "deny"


def test_unrestricted_flag_skips_even_ask_mode(tmp_path):
    """unrestricted 是执行侧的开关：档位即使不是 full 也不再校验。"""
    root = str(tmp_path)
    assert agent.judge_command(root, {"argv": ["pip", "install", "x"]}, "ask")[0] == "ask"
    assert agent.judge_command(root, {"argv": ["curl", "http://x"]}, "ask")[0] == "deny"
    assert agent.judge_command(root, {"argv": ["cargo", "build"]}, "ask")[0] == "ask"
    assert agent.judge_command(root, {"argv": ["cargo", "build"]}, "ask",
                               unrestricted=True)[0] == "allow"


def test_unrestricted_git_commit_ignores_setting():
    """完全放权下 Git 提交不再看设置里的 off / ask。"""
    assert agent.judge_git_commit("off")[0] == "deny"
    assert agent.judge_git_commit("ask")[0] == "ask"
    assert agent.judge_git_commit("off", unrestricted=True)[0] == "allow"
    assert agent.judge_git_commit("ask", unrestricted=True)[0] == "allow"


def test_unrestricted_computer_skips_allowlist_but_keeps_blocks(monkeypatch):
    """完全放权不再要求站点白名单，但金融 / 密码类内置禁区不可解除。"""
    from app.computer import policy
    # 不读真实 data/config.json：本机若把站点档位设成 allow，首个断言会失真
    monkeypatch.setattr(policy, "_cfg", lambda: {})
    policy._session_granted.clear()
    assert policy.check("https://example.com/a")[0] == "ask"
    assert policy.check("https://example.com/a", skip_allowlist=True)[0] == "allow"
    assert policy.check("https://www.paypal.com/", skip_allowlist=True)[0] == "deny"
    assert policy.check("https://mail.google.com/", skip_allowlist=True)[0] == "deny"


# ---------------------------------------------------------------- 工具结果去重
def test_dedupe_tool_result():
    seen = {}
    long_text = "x" * 300
    a, dup1 = chat._dedupe_tool_result(seen, 1, "read_file", long_text)
    assert a == long_text and dup1 is False

    b, dup2 = chat._dedupe_tool_result(seen, 2, "read_file", long_text)
    assert dup2 is True
    assert "第 1 次" in b and "已省略 300 字" in b

    tiny, dup3 = chat._dedupe_tool_result(seen, 3, "read_file", "tiny")
    assert tiny == "tiny" and dup3 is False      # 短结果不去重


# ---------------------------------------------------------------- 审批前提问
class _FakeH:
    """最小 handler：只提供 _body / _json，够 explain 端点用。"""

    def __init__(self, body):
        self.body, self.status, self.payload = body, 200, None

    def _body(self):
        return self.body

    def _json(self, obj, status=200):
        self.payload, self.status = obj, status
        return obj


def test_get_approval_is_readonly():
    aid = agent.new_approval("s1", "run_command", {"argv": ["python", "-c", "print(1)"]})
    a = agent.get_approval(aid)
    b = agent.get_approval(aid)
    assert a and a["tool"] == "run_command" and a["sid"] == "s1"
    assert b is not None                  # 取多次不会把审批记录 pop 掉
    threading.Timer(0.05, lambda: agent.resolve_approval(aid, False)).start()
    assert agent.wait_approval(aid, timeout=2) is False
    assert agent.get_approval(aid) is None


def test_describe_call():
    d = agent.describe_call("run_command", {"argv": ["git", "status"], "cwd": "web"})
    assert d["risk"] == "只读" and "web" in d["text"]
    d = agent.describe_call("run_command", {"argv": ["pip", "install", "x"]})
    assert d["risk"] == "高风险"
    d = agent.describe_call("run_command", {"cmd": "python -c 1 | more"})
    assert d["risk"] == "高风险" and "无法解析" in d["text"]
    assert agent.describe_call("delete_file", {"path": "a.py"})["risk"] == "高风险"
    assert "a.py" in agent.describe_call("edit_file", {"path": "a.py"})["text"]


def test_explain_endpoint(monkeypatch):
    aid = agent.new_approval("s", "run_command", {"argv": ["git", "status"]})
    monkeypatch.setattr(sandbox_api, "get_api_conf", lambda _: ("k", "http://x", "m"))
    monkeypatch.setattr(sandbox_api, "_quick_chat", lambda *a, **k: "这是查看仓库状态，只读。")

    h = _FakeH({"aid": aid, "question": ""})
    sandbox_api._api_agent_explain(h)
    assert h.payload["ok"] is True and "只读" in h.payload["answer"]
    assert h.payload["summary"].startswith("执行 git status")
    assert agent.get_approval(aid) is not None          # 提问不改变审批状态

    monkeypatch.setattr(sandbox_api, "get_api_conf", lambda _: ("", "", ""))
    h2 = _FakeH({"aid": aid})
    sandbox_api._api_agent_explain(h2)
    assert h2.status == 400 and "未配置模型" in h2.payload["error"]

    h3 = _FakeH({"aid": "p_missing"})
    sandbox_api._api_agent_explain(h3)
    assert h3.status == 400 and h3.payload["ok"] is False


# ---------------------------------------------------------------- 提示词与工具能力一致
def test_visual_prompt_follows_tool_availability(tmp_path):
    # 没下发 run_command 时不能暗示「有命令行」
    no_cmd = agent._visual_prompt(False)
    assert "没有命令执行能力" in no_cmd
    assert "用 run_command 生成" not in no_cmd
    assert "用 run_command 生成" in agent._visual_prompt(True)

    # 未开 Agent：整份系统提示里也不能出现「用 run_command 生成 PNG」
    p = agent.build_agent_prompt({"agent_enabled": False, "sandbox": ""})
    assert "没有命令执行能力" in p
    assert "用 run_command 生成" not in p

    # 开了 Agent 但没设沙箱：同样不能暗示
    p2 = agent.build_agent_prompt({"agent_enabled": True, "sandbox": ""})
    assert "用 run_command 生成" not in p2

    # 开了 Agent 且有沙箱：允许提位图生成
    p3 = agent.build_agent_prompt({"agent_enabled": True, "sandbox": str(tmp_path)})
    assert "用 run_command 生成" in p3


def test_visual_prompt_layers_archify_by_agent_mode():
    # Agent + 沙箱：告诉模型「复杂的用 archify，没挂载就 tool_search 解锁」
    ready = agent._visual_prompt(True)
    assert "archify" in ready
    assert 'tool_search("diagram")' in ready
    # 普通聊天：archify 不可用，只能内联围栏；要可交互页面就提示用户开 Agent
    off = agent._visual_prompt(False)
    assert "非 Agent 模式下不可用" in off
    assert "开启 Agent 模式" in off
    assert "tool_search" not in off
    assert "不需要开启 Agent" not in off


def test_capability_boundary_is_injected():
    p = agent.build_agent_prompt({"agent_enabled": False, "sandbox": ""})
    assert "以本轮下发的工具清单" in p
    assert "不要假装调用过" in p


# ---------------------------------------------------------------- 提问卡兜底
def test_ask_nudge_fires_on_decision_question():
    """末尾是决策问句、又没调 ask_user → 该提醒改用提问卡"""
    assert chat._needs_ask_nudge("改完了。\n\n要不要我现在就把这两处重构掉？", False, False)


def test_ask_nudge_ignores_continue_question():
    """「要不要继续」本来就该自动续跑，不要变成一张卡片"""
    assert not chat._needs_ask_nudge("这部分先放着。\n\n要不要我继续？", False, False)
    assert not chat._needs_ask_nudge("要不要我做下一步？", False, False)


def test_ask_nudge_ignores_rhetorical_question():
    """解说里的反问句不是交互提问"""
    assert not chat._needs_ask_nudge("读完了。\n\n那为什么还是慢？", False, False)


def test_ask_nudge_accepts_alternative_question():
    """「A 还是 B」是真正的二选一，该走卡片"""
    assert chat._needs_ask_nudge("两种都行。\n\n用方案 A 还是方案 B？", False, False)


def test_ask_nudge_only_once_and_not_after_ask_user():
    assert not chat._needs_ask_nudge("要选哪一套？", True, False)
    assert not chat._needs_ask_nudge("要选哪一套？", False, True)


def test_ask_nudge_requires_question_at_tail():
    """问句在中间、结尾是结论 → 不打扰"""
    assert not chat._needs_ask_nudge("要选哪一套？\n\n我按方案 A 做了。", False, False)


def test_looks_like_question_on_ask_answer():
    """用户在提问卡里反问，不是选择；不能当成默认同意。"""
    assert chat._looks_like_question("xxx 是怎么做的？")
    assert chat._looks_like_question("我想知道这个方案有什么风险")
    assert chat._looks_like_question("这样做行不行")
    assert not chat._looks_like_question("没什么问题")
    assert not chat._looks_like_question("用方案 A")
    assert not chat._looks_like_question("（未回答）")


def test_ask_back_authorization_phrase():
    """反问里明确说「按那个方式改」= 已授权，不再强制重新确认。"""
    assert chat._AUTHORIZE_RE.search("按那个方式改一下")
    assert chat._AUTHORIZE_RE.search("直接做")
    assert not chat._AUTHORIZE_RE.search("用方案 A 做吗？")


def test_ask_nudge_fires_on_plan_choice():
    """「要不要按方案 A 做」必须走提问卡，而不是自行决定。"""
    assert chat._needs_ask_nudge("可以按方案 A 实现。\n\n要不要按方案 A 做？", False, False)
    assert not chat._needs_ask_nudge("第一步做完了。\n\n要不要继续？", False, False)


def test_new_asks_marks_multiple_question():
    """multiple=true → 题目自动补「（多选）」，前端据此允许选多个气泡"""
    qid, clean = agent.new_asks("s_test", [
        {"question": "想改哪几块？", "options": ["渲染", "音效", "输入"], "multiple": True},
        {"question": "要不要现在开始？", "options": ["要", "不要"]},
    ])
    assert clean[0]["multiple"] is True
    assert clean[0]["question"] == "想改哪几块？（多选）"
    assert clean[1]["multiple"] is False
    assert clean[1]["question"] == "要不要现在开始？"
    assert agent.submit_answers(qid, ["渲染、音效", "要"])
    assert agent.wait_answers(qid, timeout=1) == ["渲染、音效", "要"]


def test_new_asks_keeps_existing_multiple_mark():
    """题目里已经写了「多选」就不再重复补"""
    qid, clean = agent.new_asks("s_test", [
        {"question": "选哪些（多选）", "options": ["A", "B"], "multiple": "true"}])
    assert clean[0]["multiple"] is True
    assert clean[0]["question"] == "选哪些（多选）"
    assert agent.submit_answers(qid, ["A、B"])


# ---------------------------------------------------------------- 通用工具
def test_copy_file_and_overwrite_guard(tmp_path):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    out = agent.run_tool(str(tmp_path), "copy_file",
                         {"from": "a.txt", "to": "b.txt"})[0]
    assert "已复制" in out
    assert (tmp_path / "b.txt").read_text(encoding="utf-8") == "hello"

    out2 = agent.run_tool(str(tmp_path), "copy_file",
                          {"from": "a.txt", "to": "b.txt"})[0]
    assert "目标已存在" in out2

    out3 = agent.run_tool(str(tmp_path), "copy_file",
                          {"from": "a.txt", "to": "b.txt", "overwrite": True})[0]
    assert "已复制" in out3


def test_fetch_url_blocks_private_hosts():
    """fetch_url 的 SSRF 护栏：本机 / 内网 / 非 http(s) 一律拒绝"""
    assert "内网" in agent._url_public_error("http://127.0.0.1:8080/")
    assert "内网" in agent._url_public_error("http://localhost/")
    assert "内网" in agent._url_public_error("http://[::1]/")
    assert agent._url_public_error("ftp://example.com") == "只支持 http(s) URL"
    # 公网 IP 字面量不需要 DNS，离线也能判断
    assert agent._url_public_error("https://8.8.8.8/") == ""


def test_shell_idle_detects_prompt():
    assert agent._shell_idle("\r\nPS D:\\work>")
    assert agent._shell_idle("PS C:\\x>\x1b[0K\x1b[43G\x1b[?25h")
    assert not agent._shell_idle("PS D:\\work> echo hi\r\n")


def test_sleep_returns_without_blocking_long(tmp_path):
    out = agent.run_tool(str(tmp_path), "sleep", {"seconds": 0.01})[0]
    assert "已等待" in out


def test_shell_runs_and_keeps_state(tmp_path):
    """持久 shell：同一会话里 cd 会保留（没装 pywinpty 时跳过）"""
    from app import terminal
    if not terminal.available():
        return
    sid = "pytest_shell_" + str(int(time.time() * 1000))
    caps = {"sid": sid}

    def run(command):
        # ConPTY 偶尔会吞掉紧跟提示符的输入（无回显、无输出）；重试一次即可
        for _ in range(2):
            out = agent.run_tool(str(tmp_path), "shell",
                                 {"action": "run", "command": command, "wait": 20},
                                 caps=caps)[0]
            if "秒内没有输出" not in out:
                return out
            time.sleep(0.3)
        return out

    try:
        out = run("echo shell-ok")
        assert "shell-ok" in out
        run("cd ..")
        pwd = run("(Get-Location).Path")
        assert str(tmp_path.parent) in pwd
    finally:
        agent.run_tool(str(tmp_path), "shell", {"action": "close"}, caps=caps)


# ---------------------------------------------------------------- 表达复盘
def test_expression_tip_records_and_dedupes(tmp_path, monkeypatch):
    """同一句纠正重复出现 → 只留一条并累加次数（供输入预检复用）"""
    from app import expression
    monkeypatch.setattr(expression, "PATH", str(tmp_path / "tips.json"))
    s = {"messages": [{"role": "user", "content": "把首页改得好看一点"},
                      {"role": "assistant", "content": "改完了"}]}
    assert expression.note_correction("不是这个意思，我只想改配色", s) is True
    assert expression.note_correction("不是这个意思，我只想改配色", s) is True
    tips = expression.recent(5)
    assert len(tips) == 1 and tips[0]["count"] == 2
    assert tips[0]["prev"].startswith("把首页")


def test_expression_tip_ignores_chitchat(tmp_path, monkeypatch):
    from app import expression
    monkeypatch.setattr(expression, "PATH", str(tmp_path / "tips.json"))
    s = {"messages": [{"role": "user", "content": "今天天气怎么样"}]}
    assert expression.note_correction("哈哈好的", s) is False
    assert expression.recent(3) == []


# ---------------------------------------------------------------- 输出截断归因
def test_length_classifies_output_vs_window():
    """finish_reason=length 要区分「输出额度用满」和「上下文窗口用满」"""
    # completion 贴着 max_tokens → 输出额度
    assert chat._length_is_window(1000, 4090, 4096, 128000) is False
    # 没设输出上限 → 只可能是窗口
    assert chat._length_is_window(0, 3000, 0, 128000) is True
    # prompt + completion 贴着窗口 → 窗口
    assert chat._length_is_window(125000, 2000, 64000, 128000) is True
    # 拿不到 usage 也没有输出上限 → 保守按窗口处理
    assert chat._length_is_window(0, 0, 0, 128000) is True
    # 没有窗口信息 → 不臆测
    assert chat._length_is_window(1000, 500, 4096, 0) is False


def test_read_files_batches_and_keeps_errors(tmp_path):
    """B档 B2：一次读多个文件，单个失败不影响其它。"""
    (tmp_path / "a.txt").write_text("AAA\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("BBB\n", encoding="utf-8")
    text, undo = agent._t_read_files(
        str(tmp_path), {"paths": ["a.txt", "b.txt", "nope.txt"]})
    assert undo is None
    assert "AAA" in text and "BBB" in text
    assert "文件不存在" in text
    assert text.index("a.txt") < text.index("b.txt") < text.index("nope.txt")
    names = [t["function"]["name"] for t in agent.native_tool_schemas(agent_on=True)]
    assert "read" in names
    assert agent.normalize_tool_name("read_files") == "read_files"


def test_read_files_counts_as_read_before_write(tmp_path):
    """批量读过的文件，随后 edit_file 不应再被「写前必读」拦下。"""
    (tmp_path / "a.txt").write_text("x\n", encoding="utf-8")
    agent.run_tool(str(tmp_path), "read_files", {"paths": ["a.txt"]})
    out, _undo, _w = agent.run_tool(
        str(tmp_path), "edit_file", {"path": "a.txt", "old": "x", "new": "y"})
    assert "READ_REQUIRED" not in out
    assert (tmp_path / "a.txt").read_text(encoding="utf-8").startswith("y")


# ---------------------------------------------------------------- 读类工具表达力
def test_read_file_raw_skips_line_numbers(tmp_path):
    """raw=true 返回原文：模型要精确匹配 / 自己统计时不必再写脚本。"""
    (tmp_path / "a.txt").write_text("hello\nworld\n", encoding="utf-8")
    plain, _ = agent._t_read_file(str(tmp_path), {"path": "a.txt"})
    assert "1 | hello" in plain
    raw, _ = agent._t_read_file(str(tmp_path), {"path": "a.txt", "raw": True})
    assert "1 | hello" not in raw
    assert "hello\nworld" in raw
    assert "原文" in raw


def test_read_files_batch_limit_is_16(tmp_path):
    for i in range(20):
        (tmp_path / ("f%02d.txt" % i)).write_text("x\n", encoding="utf-8")
    text, _ = agent._t_read_files(str(tmp_path), {"paths": ["f%02d.txt" % i for i in range(20)]})
    assert "f15.txt" in text
    assert "f16.txt" not in text
    assert "最多读 16 个" in text


def test_search_files_lists_all_hits_with_context(tmp_path):
    """同一个文件的多处命中都要列出来（旧版每文件只报第一处）。"""
    (tmp_path / "a.txt").write_text("TODO one\nmid\nTODO two\n", encoding="utf-8")
    out, _ = agent._t_search_files(str(tmp_path), {"pattern": "TODO"})
    assert "共 2 处命中" in out
    assert "a.txt:1" in out and "a.txt:3" in out
    out2, _ = agent._t_search_files(str(tmp_path), {"pattern": "TODO", "context": 1})
    assert "| 2: mid" in out2


def test_search_files_count_only(tmp_path):
    (tmp_path / "a.txt").write_text("hit\nhit\nhit\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("hit\n", encoding="utf-8")
    out, _ = agent._t_search_files(
        str(tmp_path), {"pattern": "hit", "count_only": True})
    assert "共 4 处命中" in out
    assert "a.txt: 3" in out and "b.txt: 1" in out
    assert "a.txt:1:" not in out          # 只统计，不返回原文


def test_read_tools_allow_node_modules_but_not_git(tmp_path):
    """像编辑器一样能直接读 node_modules；.git 与 data/ 仍然拒绝。"""
    nm = tmp_path / "node_modules" / "pkg"
    nm.mkdir(parents=True)
    (nm / "a.js").write_text("module.exports = 1;\n", encoding="utf-8")
    out, _u, _w = agent.run_tool(str(tmp_path), "read_file",
                                 {"path": "node_modules/pkg/a.js"})
    assert "module.exports" in out
    gitdir = tmp_path / ".git"
    gitdir.mkdir()
    (gitdir / "config").write_text("[remote]\n", encoding="utf-8")
    blocked, _u, _w = agent.run_tool(str(tmp_path), "read_file", {"path": ".git/config"})
    assert "禁止访问" in blocked
    # 写类工具仍然不允许进 node_modules
    try:
        agent.resolve_path(str(tmp_path), "node_modules/pkg/a.js")
        raise AssertionError("写路径不应放行 node_modules")
    except agent.SandboxError:
        pass


# ---------------- 命令的只读分类（决定能否与其他读并发） ----------------

def test_cmd_read_only_accepts_git_read_commands():
    assert chat._cmd_read_only({"argv": ["git", "status"]})
    assert chat._cmd_read_only({"cmd": "git log --oneline -5"})
    assert chat._cmd_read_only({"cmd": "git show HEAD:app/a.py"})


def test_cmd_read_only_rejects_writes_and_composition():
    # 会写文件
    assert not chat._cmd_read_only({"cmd": "git diff --output=x.patch"})
    # 非只读子命令
    assert not chat._cmd_read_only({"cmd": "git commit -m x"})
    # 管道 / 串联 / 重定向：不猜
    assert not chat._cmd_read_only({"cmd": "git status | head"})
    assert not chat._cmd_read_only({"cmd": "git status; rm -rf ."})
    # 前置参数（-C）会让子命令位置不确定：保守判否
    assert not chat._cmd_read_only({"argv": ["git", "-C", "sub", "status"]})
    # 非 git 一律不算
    assert not chat._cmd_read_only({"cmd": "python x.py"})
    assert not chat._cmd_read_only({})


# ---------------- 多个只读 explore 并发（对齐 CC 的 AgentTool 并发安全） ----------------

def test_explore_batchable_only_read_only_and_foreground():
    assert chat._explore_batchable({"goal": "g"})
    assert not chat._explore_batchable({"goal": "g", "allow_write": True})
    assert not chat._explore_batchable({"goal": "g", "background": True})


def test_run_explore_batch_is_concurrent_and_keeps_indexes():
    """用 Barrier 证明是并发跑的：顺序执行会超时。"""
    barrier = threading.Barrier(2, timeout=3)

    def fake(args, ctx):
        barrier.wait()
        return {"ok": True, "goal": args.get("goal")}

    calls = [{"tool": "explore", "args": {"goal": "a"}},
             {"tool": "explore", "args": {"goal": "b"}}]
    out = chat._run_explore_batch([0, 1], calls, {}, fake)
    assert out[0][0]["goal"] == "a"
    assert out[1][0]["goal"] == "b"


def test_run_explore_batch_turns_exception_into_result():
    """单个子代理批次炸了也要有结果，不能让主循环丢配对。"""
    def boom(args, ctx):
        raise RuntimeError("炸了")

    calls = [{"tool": "explore", "args": {}}, {"tool": "explore", "args": {}}]
    out = chat._run_explore_batch([0, 1], calls, {}, boom)
    assert out[0][0]["ok"] is False and "炸了" in out[0][0]["error"]
    assert out[1][0]["ok"] is False


# ---------------- 命令增量输出（前端执行中就能看到日志） ----------------

def test_run_command_streams_output_via_sink(tmp_path):
    import sys

    got = []
    prev = agent.set_output_sink(got.append)
    try:
        code = ("import time\n"
                "for i in range(3):\n"
                "    print('L%d' % i, flush=True)\n"
                "    time.sleep(0.2)\n")
        text, _err = agent._t_run_command(
            str(tmp_path), {"argv": [sys.executable, "-c", code], "timeout": 20},
            unrestricted=True)
    finally:
        agent.set_output_sink(prev)
    assert "结论：成功" in text
    # 读线程按行回调：执行中就能拿到首行与末行，而不是等进程结束
    assert any("L0" in c for c in got)
    assert any("L2" in c for c in got)
    # 最终结果仍是完整输出（增量推流不影响回灌给模型的内容）
    assert "L0" in text and "L2" in text

