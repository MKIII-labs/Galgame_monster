# -*- coding: utf-8 -*-
"""历史附图：过去发送的图片要能重新进入上下文，而不是只剩一行句柄。"""

from app import context


def _session():
    return {"messages": [
        {"role": "user", "content": "第一张图", "images": [{"file": "a.png"}]},
        {"role": "assistant", "content": "看到了"},
        {"role": "user", "content": "第二张图", "images": [{"file": "b.png"}]},
        {"role": "assistant", "content": "好"},
        {"role": "user", "content": "现在问问题"},      # 当前回合
    ]}


def _loader(_m, text):
    return [{"type": "image_url", "image_url": {"url": "data:image/png;base64,FAKE"}},
            {"type": "text", "text": text}]


def test_history_image_msgs_excludes_current_turn():
    msgs = _session()["messages"]
    picked = context._history_image_msgs(msgs, 2)
    assert [m["content"] for m in picked] == ["第一张图", "第二张图"]


def test_history_image_msgs_respects_keep():
    msgs = _session()["messages"]
    picked = context._history_image_msgs(msgs, 1)
    assert [m["content"] for m in picked] == ["第二张图"]


def test_build_context_reattaches_recent_image():
    out = context.build_context_messages(_session(), "SYS",
                                         image_loader=_loader, image_keep=1)
    # 系统 + 4 条历史 + 1 条当前
    assert out[0] == {"role": "system", "content": "SYS"}
    # 第一张图超出保留条数：纯文本 + 可回看句柄
    assert isinstance(out[1]["content"], str)
    assert "可回看附件" in out[1]["content"]
    # 第二张图：重新附图（多模态数组）
    assert isinstance(out[3]["content"], list)
    assert out[3]["content"][0]["type"] == "image_url"
    # 当前回合保持字符串（由调用方另有附图逻辑）
    assert isinstance(out[5]["content"], str)


def test_build_context_without_loader_stays_text():
    out = context.build_context_messages(_session(), "SYS", image_keep=2)
    assert all(isinstance(m["content"], str) for m in out)


def test_history_image_count():
    assert context.history_image_count(_session(), 2) == 2
    assert context.history_image_count(_session(), 1) == 1
    assert context.history_image_count(_session(), 0) == 0


def test_attach_dyn_tail_supports_multimodal_content():
    import app.api.chat as chat

    msgs = [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,FAKE"}}]}]
    assert chat._attach_dyn_tail(msgs, "动态上下文", "") == "动态上下文"
    assert msgs[0]["content"][-1] == {"type": "text", "text": "动态上下文"}
