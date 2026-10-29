# -*- coding: utf-8 -*-
"""子代理真 fork：父会话前缀裁剪、预算、边界声明、追问沿用。"""
import pytest

from app import explore
from app.api import chat


@pytest.fixture(autouse=True)
def _no_evidence(monkeypatch, tmp_path):
    """fork 用例聚焦前缀拼接；证据核验在 test_explore.py 单独覆盖。"""
    monkeypatch.setattr(explore, "VERIFY_EVIDENCE", False)
    monkeypatch.setattr(explore, "DATA_DIR", str(tmp_path))   # 子会话落盘隔离


def _tool_msg(text):
    return {"role": "user", "content": chat.TOOL_MSG_PREFIX + "\n" + text}


def _parent_messages():
    return [
        {"role": "system", "content": "SYS-不应进入前缀"},
        {"role": "user", "content": "实现鉴权"},
        {"role": "assistant", "content": "先看代码"},
        _tool_msg("旧结果"),
        {"role": "assistant", "content": "继续看"},
        _tool_msg("结果2"),
        _tool_msg("结果3"),
        {"role": "user", "content": "现在问你"},
    ]


def test_fork_prefix_keeps_conversation_and_recent_tools():
    msgs, used = chat._fork_prefix(_parent_messages(), 12000)
    joined = "\n".join(m["content"] for m in msgs)
    assert "SYS-不应进入前缀" not in joined          # system 换成子代理自己的
    assert all(m["role"] in ("user", "assistant") for m in msgs)
    assert "实现鉴权" in joined and "现在问你" in joined
    assert "结果2" in joined and "结果3" in joined    # 最近 2 条工具结果保留
    assert "旧结果" not in joined                     # 更早的被省略
    assert "1 条工具结果已省略" in joined
    assert msgs[-1]["content"] == "现在问你"
    assert used > 0


def test_fork_prefix_clips_long_tool_result():
    long_text = "代" * 3000
    msgs, _ = chat._fork_prefix([_tool_msg(long_text)], 12000)
    text = msgs[-1]["content"]
    assert len(text) <= chat.FORK_TOOL_CLIP + 30
    assert text.endswith("（工具结果已截断）")


def test_fork_prefix_respects_budget():
    msgs, used = chat._fork_prefix(_parent_messages(), 1)
    assert msgs == [] and used == 0

    msgs2, used2 = chat._fork_prefix(_parent_messages(), 12)
    assert len(msgs2) <= 1                           # 预算只够最后一条
    assert msgs2[0]["content"] == "现在问你"
    assert used2 <= 12


def test_fork_prefix_strips_image_base64():
    """多模态 content 只留文本，绝不把 base64 带进前缀。"""
    msgs = [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        {"type": "text", "text": "看这张图里的报错"},
    ]}]
    out, _ = chat._fork_prefix(msgs, 12000)
    text = out[-1]["content"]
    assert "看这张图里的报错" in text and "【图片】" in text
    assert "base64" not in text and "AAAA" not in text


def _fake_chat(seen):
    def fake(ctx, messages, schemas, stop_event):
        seen["msgs"] = list(messages)
        return {"visible": "结论 app/a.py:1", "usage": {"total_tokens": 10},
                "tool_calls": [], "native_fallback": False}
    return fake


def test_explore_injects_fork_prefix(tmp_path):
    seen, marks = {}, []
    prefix = [{"role": "user", "content": "父：帮我查鉴权"},
              {"role": "assistant", "content": "父：我准备探索"}]
    explore.run(
        {"goal": "查鉴权", "questions": [{"ask": "入口在哪"}]},
        {"sandbox": str(tmp_path), "api_key": "k", "api_url": "http://x", "model": "m",
         "budget": 0, "fork_prefix": prefix, "fork_tokens": 88,
         "emit": marks.append, "chat_fn": _fake_chat(seen)})
    msgs = seen["msgs"]
    assert msgs[0]["role"] == "system"
    assert msgs[1]["content"] == explore.FORK_NOTE
    assert msgs[2]["content"] == "父：帮我查鉴权"
    assert msgs[3]["content"] == "父：我准备探索"
    assert "入口在哪" in msgs[-1]["content"]
    start = next(m for m in marks if m["type"] == "subagent_start")
    assert start["fork"] is True and start["prefix_tokens"] == 88


def test_explore_without_fork_keeps_two_messages(tmp_path):
    seen = {}
    explore.run(
        {"goal": "g", "questions": [{"ask": "入口"}]},
        {"sandbox": str(tmp_path), "api_key": "k", "api_url": "http://x", "model": "m",
         "budget": 0, "chat_fn": _fake_chat(seen)})
    assert len(seen["msgs"]) == 2                   # system + 任务
    assert "父对话最近进展" not in seen["msgs"][-1]["content"]


def test_followup_keeps_fork_history(monkeypatch, tmp_path):
    from app import agent
    monkeypatch.setattr(agent, "run_tool",
                        lambda s, n, a: ("已读内容", None, False))
    with explore._BATCH_LOCK:
        explore._BATCHES.clear()
    seen, marks = {}, []
    ctx = {"sandbox": str(tmp_path), "api_key": "k", "api_url": "http://x", "model": "m",
           "budget": 0, "ttl": 60, "fork_tokens": 88, "emit": marks.append,
           "fork_prefix": [{"role": "user", "content": "父：帮我查鉴权"},
                           {"role": "assistant", "content": "父：我准备探索"}],
           "chat_fn": _fake_chat(seen)}
    res = explore.run({"goal": "查鉴权", "questions": [{"ask": "入口在哪"}]}, ctx)
    assert res["batch"]

    seen.clear()
    out = explore.followup({"batch": res["batch"], "questions": ["那个函数返回什么"]}, ctx)
    assert out["ok"] is True
    joined = "\n".join(m["content"] for m in seen["msgs"])
    assert "父：帮我查鉴权" in joined                 # 追问沿用同一份 fork 前缀
    assert joined.count("父：帮我查鉴权") == 1        # 不会重复拼接
    assert "追问" in joined
    assert len(seen["msgs"]) >= 4                    # system + fork + 历史 + 追问
