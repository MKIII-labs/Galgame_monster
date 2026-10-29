# -*- coding: utf-8 -*-
"""完成声明核对：只有「全都没成功」或「写操作全失败」才判定不成立。"""

from app.api.chat import _done_claim_verdict

TEXT = "4 处都已写入。现在验证语法。"


def _ok(name, write=False):
    return {"tool": name, "result": "已修改 index.html（−6 行）", "write": write, "ok": True}


def _bad(name, write=False, msg="❌ 工作目录不存在：."):
    return {"tool": name, "result": msg, "write": write, "ok": False}


def test_partial_failure_does_not_void_claim():
    """40 个成功 + 1 个 run_command 参数写坏 → 不该报「没有成功执行」"""
    log = [_ok("edit_file", write=True) for _ in range(19)] + [_bad("run_command")]
    assert _done_claim_verdict(TEXT, log, []) == ""


def test_no_tool_at_all_warns():
    assert "没有成功执行任何工具" in _done_claim_verdict(TEXT, [], [])


def test_memory_ops_alone_is_enough():
    assert _done_claim_verdict(TEXT, [], [{"op": "add"}]) == ""


def test_all_writes_failed_warns():
    """读成功了、写全失败 → 完成声明不成立（这条才是 `failed_tools` 想抓的情况）"""
    log = [_ok("read_file"), _bad("write_file", write=True), _bad("edit_file", write=True)]
    assert "写操作全部失败" in _done_claim_verdict(TEXT, log, [])


def test_one_successful_write_is_enough():
    log = [_bad("write_file", write=True), _ok("edit_file", write=True)]
    assert _done_claim_verdict(TEXT, log, []) == ""


def test_no_completion_claim_is_silent():
    assert _done_claim_verdict("我先看看结构。", [], []) == ""


def test_non_agent_only_checks_knowledge_base():
    """非 Agent 会话没有文件 / 命令能力：只核对「声称写入知识库」。

    现场 s_2eed2dd969cf idx672：讲课时正文出现「写完了」，被当成完成声明，
    末尾被追加「本轮没有成功执行任何工具」。
    """
    assert _done_claim_verdict("这道题我把推导写完了。", [], [], agent_mode=False) == ""
    # 声称改了记忆、却没有 mem ops → 仍然要警告
    warn = _done_claim_verdict("已经把这条记忆改短了。", [], [], agent_mode=False)
    assert "知识库" in warn
    assert _done_claim_verdict("已经把这条记忆改短了。", [], [{"op": "update"}],
                               agent_mode=False) == ""
