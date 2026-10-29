# -*- coding: utf-8 -*-
"""resume：断点摘要内容 + 启动时把 _progress 快照转成「可继续任务」。"""
from app import sessions
from app.api.chat import _resume_brief


def test_resume_brief_lists_tools_and_plan():
    msg = {"role": "assistant", "content": "写到一半",
           "tools": [{"tool": "read_file", "args": {"path": "src/main.js"},
                      "result": "123 行", "ok": True},
                     {"tool": "edit_file", "args": {"path": "src/main.js"},
                      "result": "失败", "ok": False}],
           "plan": {"steps": [{"text": "修渲染", "status": "doing"},
                              {"text": "跑测试", "status": "pending"}]}}
    note = _resume_brief(msg)
    assert "read_file" in note and "src/main.js" in note
    assert "（失败）" in note
    assert "[doing]" in note and "跑测试" in note
    assert "不要重复" in note


def test_resume_brief_empty_tools():
    note = _resume_brief({"role": "assistant", "content": "x"})
    assert "没有记录到工具操作" in note


def test_mark_resume_pending():
    data = {"sessions": [{"id": "s1", "messages": [
        {"role": "user", "content": "做任务"},
        {"role": "assistant", "content": "半截", "_progress": True,
         "tools": [{"tool": "read_file"}]}]}]}
    assert sessions._mark_resume_pending(data) is True
    m = data["sessions"][0]["messages"][-1]
    assert "_progress" not in m
    assert m["interrupted"] is True
    assert m["end_reason"]["action"] == "resume"
    assert data["sessions"][0]["resume_pending"] is True
    # 已经转过一次：不应再判为需要写盘
    assert sessions._mark_resume_pending(data) is False


def test_save_progress_does_not_mark_interrupted(monkeypatch):
    """运行中的进度快照不能标 interrupted。

    现场：后台任务还在跑，刷新/重开窗口后最后一条是 _progress 快照；
    旧代码把它标成 interrupted，前端就显示「继续输出」，看起来像输出停了。
    真正的中断只由进程重启后的 _mark_resume_pending 标记。
    """
    from app.api import chat

    data = {"sessions": [{"id": "s1", "messages": [
        {"role": "user", "content": "做任务"},
        {"role": "assistant", "content": "半截", "_progress": True,
         "interrupted": True, "end_reason": {"kind": "resume"}}]}]}
    monkeypatch.setattr(chat, "load_sessions", lambda: data)
    monkeypatch.setattr(chat, "find_session",
                        lambda d, sid: d["sessions"][0] if sid == "s1" else None)
    monkeypatch.setattr(chat, "_persist_session", lambda s: None)

    chat._save_progress("s1", "半截更多", [{"tool": "read_file"}], {"steps": []})
    m = data["sessions"][0]["messages"][-1]
    assert m["_progress"] is True
    assert "interrupted" not in m
    assert "end_reason" not in m

    # 进程重启时才转成中断
    assert sessions._mark_resume_pending(data) is True
    assert m["interrupted"] is True
    assert m["end_reason"]["kind"] == "resume"
