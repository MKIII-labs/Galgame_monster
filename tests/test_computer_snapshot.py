# -*- coding: utf-8 -*-
"""Computer Use · 快照排版与 CDP 编解码（纯函数）"""
from app.computer import snapshot
from app.computer import cdp


def _data(**kw):
    d = {"url": "https://example.com/", "title": "示例", "vw": 1280, "vh": 800,
         "scrollY": 0, "scrollMax": 1200, "count": 3, "start": 0,
         "items": [
             {"ref": 1, "tag": "button", "name": "开始", "type": "", "x": 10, "y": 20},
             {"ref": 2, "tag": "input#email", "name": "邮箱", "type": "text", "x": 10, "y": 60},
             {"ref": 3, "tag": "input#pwd", "name": "", "type": "password", "pass": True,
              "x": 10, "y": 90},
         ],
         "canvases": [{"id": "game", "w": 960, "h": 600}],
         "text": "正文第一行"}
    d.update(kw)
    return d


def test_render_contains_refs_and_marks_password():
    text = snapshot.render(_data())
    assert "[1] button 「开始」" in text
    assert "[2] input#email" in text and "邮箱" in text
    assert "密码框，禁止读取/填写" in text
    assert "[canvas#game] 960x600" in text
    assert "正文第一行" in text
    assert "示例" in text and "https://example.com/" in text


def test_render_pagination_hint():
    text = snapshot.render(_data(count=42, start=0, items=_data()["items"]))
    assert "共 42 个可交互元素" in text
    assert "start=3" in text


def test_render_truncates():
    items = [{"ref": i, "tag": "button", "name": "按钮" * 10, "x": 0, "y": 0}
             for i in range(1, 60)]
    text = snapshot.render(_data(count=60, items=items), max_chars=400)
    assert len(text) < 520
    assert "快照过长已截断" in text


def test_render_empty_page():
    text = snapshot.render({"url": "about:blank", "title": "", "vw": 800, "vh": 600,
                            "count": 0, "items": []})
    assert "(无标题)" in text and "可交互元素 0 个" in text


# ---------------------------------------------------------------- CDP 编解码
def test_parse_ws_url():
    host, port, path = cdp._parse_ws_url("ws://127.0.0.1:9222/devtools/page/ABC")
    assert host == "127.0.0.1" and port == 9222 and path == "/devtools/page/ABC"


def test_encode_frame_lengths():
    small = cdp.encode_frame(b"hi", mask=False)
    assert small[0] == 0x81 and small[1] == 2 and small[2:] == b"hi"
    mid = cdp.encode_frame(b"x" * 200, mask=False)
    assert mid[1] == 126 and int.from_bytes(mid[2:4], "big") == 200
    big = cdp.encode_frame(b"x" * 70000, mask=False)
    assert big[1] == 127 and int.from_bytes(big[2:10], "big") == 70000


def test_encode_frame_masked_roundtrip():
    payload = b'{"id":1,"method":"Page.enable"}'
    frame = cdp.encode_frame(payload, mask=True)
    assert frame[1] & 0x80                       # mask 位
    key = frame[2:6]
    assert cdp._unmask(frame[6:], key) == payload


def test_key_table_has_enter():
    from app.computer.actions import KEYS
    assert KEYS["Enter"][0] == 13
    assert "Escape" in KEYS and "Tab" in KEYS
