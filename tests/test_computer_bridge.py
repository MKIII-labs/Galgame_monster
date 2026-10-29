# -*- coding: utf-8 -*-
"""Computer Use · 扩展桥（队列/鉴权）与自动补截图启发式"""
import threading

import app.config as config
from app import computer
from app.computer import bridge, browser


def _fake_cfg(cfg):
    import copy
    base = copy.deepcopy(config.DEFAULT_CONFIG)
    base.update(cfg)
    return base


# ---------------------------------------------------------------- 密钥
def test_key_generated_and_persisted(monkeypatch):
    saved = {}
    monkeypatch.setattr(config, "load_config", lambda: dict(saved))
    monkeypatch.setattr(config, "save_config", lambda c: saved.update(c))
    monkeypatch.setattr(bridge, "_key_cache", {"value": "", "at": 0.0})
    k1 = bridge.key()
    assert k1 and saved.get("computer_bridge_key") == k1
    # 第二次读同一个值（缓存 + 落盘）
    bridge._key_cache.update({"value": "", "at": 0.0})
    assert bridge.key() == k1


def test_check_key_header_and_query(monkeypatch):
    monkeypatch.setattr(bridge, "key", lambda: "secret")
    assert bridge.check_key({"X-Bridge-Key": "secret"})
    assert bridge.check_key({}, "secret")
    assert not bridge.check_key({"X-Bridge-Key": "bad"})
    assert not bridge.check_key({}, "")


# ---------------------------------------------------------------- 队列
def test_call_roundtrip(monkeypatch):
    bridge.reset()
    bridge.heartbeat()
    out = {}

    t = threading.Thread(target=lambda: out.update(
        {"v": bridge.call("cdp", {"method": "Runtime.evaluate"}, timeout=5)}))
    t.start()
    cmd = bridge.poll(3)
    assert cmd and cmd["op"] == "cdp" and cmd["args"]["method"] == "Runtime.evaluate"
    assert bridge.post_result(cmd["id"], True, {"result": {"value": 42}})
    t.join(5)
    assert out["v"]["result"]["value"] == 42


def test_call_without_extension(monkeypatch):
    bridge.reset()
    try:
        bridge.call("cdp", {}, timeout=1)
        assert False, "应当抛 BridgeError"
    except bridge.BridgeError as e:
        assert "未连接" in str(e)


def test_status_disconnected_after_timeout(monkeypatch):
    bridge.reset()
    assert bridge.status()["connected"] is False
    bridge.heartbeat("https://x.test/", "标题", True)
    st = bridge.status()
    assert st["connected"] is True and st["url"].startswith("https://x.test")


# ---------------------------------------------------------------- 后端选择
def test_use_extension_switch(monkeypatch):
    monkeypatch.setattr(config, "load_config",
                        lambda: _fake_cfg({"computer_backend": "extension"}))
    assert browser.use_extension() is True
    monkeypatch.setattr(config, "load_config",
                        lambda: _fake_cfg({"computer_backend": "local"}))
    assert browser.use_extension() is False


# ---------------------------------------------------------------- 自动补截图
def test_poor_browser_structure():
    assert computer.poor_browser_structure({"count": 0}) is True
    assert computer.poor_browser_structure(
        {"count": 5, "vw": 1000, "vh": 800,
         "canvases": [{"w": 900, "h": 700}]}) is True
    assert computer.poor_browser_structure(
        {"count": 5, "vw": 1000, "vh": 800, "canvases": [{"w": 100, "h": 100}]}) is False


def test_poor_desktop_structure():
    assert computer.poor_desktop_structure({"count": 0}) is True
    assert computer.poor_desktop_structure({"count": 3}) is False


def test_auto_shot_cooldown(monkeypatch):
    computer._LAST_SHOT.clear()
    monkeypatch.setattr(computer, "_auto_shot_enabled", lambda: True)
    calls = {"n": 0}

    def shooter():
        calls["n"] += 1
        return "画面描述"

    first = computer.maybe_auto_shot("k", True, shooter)
    second = computer.maybe_auto_shot("k", True, shooter)
    assert "已自动截图" in first and second == ""
    assert calls["n"] == 1


def test_auto_shot_disabled_and_not_poor(monkeypatch):
    computer._LAST_SHOT.clear()
    monkeypatch.setattr(computer, "_auto_shot_enabled", lambda: False)
    assert computer.maybe_auto_shot("k2", True, lambda: "x") == ""
    monkeypatch.setattr(computer, "_auto_shot_enabled", lambda: True)
    assert computer.maybe_auto_shot("k3", False, lambda: "x") == ""


# ---------------------------------------------------------------- HTTP 端点
class _H:
    def __init__(self, path="/", body=None, headers=None):
        self.path = path
        self.body = body or {}
        self.headers = headers or {}
        self.out = None

    def _host_ok(self):
        return True

    def _json(self, obj, code=200):
        self.out = (obj, code)

    def _body(self):
        return self.body


def test_endpoint_requires_key(monkeypatch):
    from app.api import computer_bridge as api
    monkeypatch.setattr(bridge, "key", lambda: "secret")
    h = _H("/api/computer/bridge/poll?wait=0")
    api.api_poll(h)
    assert h.out[1] == 403


def test_endpoint_hello_and_result(monkeypatch):
    from app.api import computer_bridge as api
    monkeypatch.setattr(bridge, "key", lambda: "secret")
    bridge.reset()
    h = _H("/api/computer/bridge/hello?k=secret",
           {"url": "https://a.test/", "title": "A", "attached": True})
    api.api_hello(h)
    assert h.out[0]["ok"] is True
    assert bridge.status()["connected"] is True

    h2 = _H("/api/computer/bridge/result?k=secret",
            {"id": "nope", "ok": True, "data": {}})
    api.api_result(h2)
    assert h2.out[0]["ok"] is False      # 不存在的请求 id


def test_endpoint_poll_empty(monkeypatch):
    from app.api import computer_bridge as api
    monkeypatch.setattr(bridge, "key", lambda: "secret")
    bridge.reset()
    bridge.heartbeat()
    h = _H("/api/computer/bridge/poll?wait=0&k=secret")
    api.api_poll(h)
    assert h.out[0] == {"op": ""}
