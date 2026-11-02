# -*- coding: utf-8 -*-
"""原生视觉附图：缩图、上限、以及多模态消息的 token 估算。"""
import base64
import io

from PIL import Image

from app import agent, context
from app.config import DEFAULT_CONFIG


def _big_png(path, size=(2000, 1500), color=(30, 60, 120)):
    Image.new("RGB", size, color).save(path)


def test_vision_attachments_shrinks_to_1024(tmp_path):
    p = tmp_path / "big.png"
    _big_png(p)
    imgs, err = agent.vision_attachments(str(tmp_path), {"path": "big.png"})
    assert err == "" and len(imgs) == 1
    it = imgs[0]
    assert it["path"] == "big.png" and it["mime"] == "image/jpeg"
    raw = base64.b64decode(it["b64"])
    with Image.open(io.BytesIO(raw)) as im:
        assert max(im.size) <= 1024


def test_vision_attachments_limits_and_errors(tmp_path):
    for i in range(5):
        _big_png(tmp_path / ("p%d.png" % i), size=(64, 64))
    imgs, err = agent.vision_attachments(
        str(tmp_path), {"paths": ["p0.png", "p1.png", "p2.png", "p3.png", "p4.png"]})
    assert imgs == [] and "最多" in err

    (tmp_path / "a.txt").write_text("hi", encoding="utf-8")
    imgs, err = agent.vision_attachments(str(tmp_path), {"path": "a.txt"})
    assert imgs == [] and "只支持图片" in err

    imgs, err = agent.vision_attachments(str(tmp_path), {"path": "nope.png"})
    assert imgs == [] and "不存在" in err

    imgs, err = agent.vision_attachments(str(tmp_path), {})
    assert imgs == [] and "path" in err


def test_multimodal_message_tokens_ignore_base64():
    blob = "A" * 200000
    msg = {"role": "user", "content": [
        {"type": "text", "text": "看这张图"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + blob}},
    ]}
    text = context._msg_text(msg)
    assert "看这张图" in text and "[图片]" in text and blob[:20] not in text
    n = context.estimate_messages_tokens([msg])
    assert n < 5000                       # 一张图按 IMAGE_TOKENS≈1600 计，不按 base64 长度
    # 裁剪函数遇到多模态消息不能崩
    assert context.trim_tool_results([msg], 100, keep_recent=2) == 0


def test_native_vision_default_and_schema():
    assert DEFAULT_CONFIG.get("agent_native_vision") is True
    schemas = agent.native_tool_schemas(agent_on=True, search_ready=False,
                                        cap_domains=set(),
                                        allow_groups={"image"})
    schema = next(t for t in schemas if t["function"]["name"] == "image")
    props = schema["function"]["parameters"]["properties"]
    assert "native" in props and "paths" in props
