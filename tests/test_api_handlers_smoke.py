# -*- coding: utf-8 -*-
"""被搬走的 API handler：**真调用一次**（只挑只读的），
防止「搬移漏 import / 名字撞名 → 调用时才出错」。dev_check 只验 import，抓不到这类问题。

（api 模块里的函数名保留 `_api_` 前缀，避免与 import 进来的同名函数撞名，
 例如 misc 里既 import 了 `save_config`，又有同名的路由函数。）"""
import pytest

from app.api import gal_character, gal_media, hall, kb, misc, notes, savepoints, sandbox


class FakeHandler:
    """最小 handler 替身：只实现被调用到的几个方法"""

    def __init__(self, body=None, qs=None):
        self._incoming = body or {}
        self.qs = qs or {}
        self.headers = {}
        self.calls = []

    def _json(self, obj, code=200):
        self.calls.append((code, obj))
        return obj

    def _body(self):
        return self._incoming

    def _body_raw(self):
        return b""

    def _send(self, code, body, ctype=""):
        self.calls.append((code, body))

    # Handler 上的私有辅助方法（搬走的代码会直接调用它们，替身要给上）
    def _notes_state(self):
        return {"ok": True, "tree": [], "items": []}

    def _upload_path(self, sid, name):
        return None

    def last(self):
        return self.calls[-1]


def test_misc_state_runs():
    h = FakeHandler()
    misc._api_state(h)
    code, obj = h.last()
    assert code == 200
    for k in ("config", "profile", "sessions", "kbs", "relations", "streaming"):
        assert k in obj, "state 返回缺字段 %s" % k


def test_misc_state_reports_streaming_sessions():
    """断线重连要靠 streaming 判断任务结束没，不能只凭 attach 失败就收尾。"""
    import threading

    from app.store import ACTIVE_STREAMS
    h = FakeHandler()
    ACTIVE_STREAMS["s_test"] = threading.Event()
    try:
        misc._api_state(h)
        assert "s_test" in h.last()[1]["streaming"]
    finally:
        ACTIVE_STREAMS.pop("s_test", None)


def test_misc_usage_runs():
    h = FakeHandler()
    misc._api_usage(h, {})
    assert h.last()[0] == 200


def test_kb_list_and_missing_get():
    h = FakeHandler()
    kb._api_kb_list(h)
    assert h.last()[0] == 200 and "kbs" in h.last()[1]
    h2 = FakeHandler({"id": "kb_not_exist"})
    kb._api_kb_get(h2)
    assert h2.last()[0] in (200, 404)          # 不存在也不能崩


def test_savepoints_list_runs():
    h = FakeHandler({"id": "s_not_exist"})
    savepoints._api_save_list(h)
    assert h.last()[0] == 200


def test_session_search_matches_title_and_body(monkeypatch):
    """会话搜索：标题与对话正文都能命中；GAL 会话不混进主列表。"""
    from app.api import sessions as sessions_api

    fake = {"sessions": [
        {"id": "a", "title": "群论笔记", "updated_at": 2,
         "messages": [{"role": "user", "content": "无关内容"}]},
        {"id": "b", "title": "别的", "updated_at": 1,
         "messages": [{"role": "assistant", "content": "这里讲到群的定义"}]},
        {"id": "c", "title": "剧场", "gal_scope": "gal", "updated_at": 5, "messages": []},
        {"id": "d", "title": "不匹配", "updated_at": 3, "messages": []},
    ]}
    monkeypatch.setattr(sessions_api, "load_sessions", lambda: fake)
    h = FakeHandler({"q": "群"})
    sessions_api._api_session_search(h)
    code, obj = h.last()
    assert code == 200 and obj["ok"] is True
    assert [i["id"] for i in obj["items"]] == ["a", "b"]
    assert obj["items"][0]["title_hit"] is True
    assert "群" in obj["items"][1]["hits"][0]["snippet"]


def test_notes_tree_runs():
    h = FakeHandler()
    notes._api_notes_tree(h)
    assert h.last()[0] == 200


def test_sandbox_check_validates_directory(tmp_path):
    h = FakeHandler({"path": str(tmp_path)})
    sandbox._api_sandbox_check(h)
    code, obj = h.last()
    assert code == 200 and obj["ok"] is True and obj["path"] == str(tmp_path)

    h2 = FakeHandler({"path": str(tmp_path / "not-exist")})
    sandbox._api_sandbox_check(h2)
    code2, obj2 = h2.last()
    assert code2 == 200 and obj2["ok"] is False and obj2["path"] == ""


def test_kb_save_stores_custom_prompt(monkeypatch):
    fake = {"id": "kb_1", "name": "教材", "nodes": [], "edges": [], "graph": {}}
    saved = {}
    monkeypatch.setattr(kb, "load_kb", lambda kid: fake)
    monkeypatch.setattr(kb, "save_kb", lambda obj: saved.update(obj))
    monkeypatch.setattr(kb, "load_kb_index", lambda: {"kbs": []})
    monkeypatch.setattr(kb, "is_profile_kb", lambda kid: False)
    h = FakeHandler({"id": "kb_1", "prompt": " 只写教材内容 "})
    kb._api_kb_save(h)
    assert saved["prompt"] == "只写教材内容"


def test_kb_save_stores_families_and_layout(monkeypatch):
    fake = {"id": "kb_1", "name": "教材", "nodes": [], "edges": [], "graph": {}}
    saved = {}
    monkeypatch.setattr(kb, "load_kb", lambda kid: fake)
    monkeypatch.setattr(kb, "save_kb", lambda obj: saved.update(obj))
    monkeypatch.setattr(kb, "load_kb_index", lambda: {"kbs": []})
    monkeypatch.setattr(kb, "is_profile_kb", lambda kid: False)
    fam = [{"id": "f_1", "title": "抽象代数", "members": ["n_1"]}]
    h = FakeHandler({"id": "kb_1", "families": fam, "layout_mode": "physics"})
    kb._api_kb_save(h)
    assert saved["families"] == fam
    assert saved["layout_mode"] == "physics"


def test_gal_characters_runs():
    h = FakeHandler()
    gal_character._api_gal_characters(h)
    code, obj = h.last()
    assert code == 200
    assert isinstance(obj, (list, dict))


def test_gal_backgrounds_runs():
    h = FakeHandler()
    gal_media._api_gal_backgrounds(h)
    assert h.last()[0] == 200


def test_misc_save_config_does_not_recurse(monkeypatch):
    """防回归：misc 里 import 的 save_config(cfg) 必须指向 app.config，
    不能被同名的路由函数遮蔽（否则把自己当参数传进去 → AttributeError）。"""
    from app import config as cfg_mod
    from app.api import misc as misc_mod

    seen = {}

    def fake_save_config(cfg):
        seen["cfg"] = cfg

    monkeypatch.setattr(misc_mod, "save_config", fake_save_config)
    h = FakeHandler({"chat_model": "x"})
    misc_mod._api_save_config(h)
    assert seen.get("cfg", {}).get("chat_model") == "x"
    assert h.last()[0] == 200


# ---------------- 功能大厅：图寻游戏 ----------------

PHOTO = {"photo_id": "p1", "image_url": "https://example.org/a.jpg",
         "thumb_url": "https://example.org/a_t.jpg", "lat": 48.8501, "lng": 2.3004,
         "license": "CC-BY-SA-4.0", "taken_at": "2024-01-01T00:00:00Z",
         "credit": "Panoramax · OSM-FR"}


@pytest.fixture
def _hall_store():
    from app import hall as hall_mod
    hall_mod.reset_store()
    yield hall_mod
    hall_mod.reset_store()


def test_hall_geo_new_reports_fetch_failure(monkeypatch, _hall_store):
    monkeypatch.setattr(_hall_store, "fetch_photo",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("取不到图")))
    h = FakeHandler({"ai_count": 1})
    hall._api_hall_geo_new(h)
    code, obj = h.last()
    assert code == 502 and obj["ok"] is False
    assert "取不到图" in obj["error"]


def test_hall_geo_new_accepts_ai_off(monkeypatch, _hall_store):
    monkeypatch.setattr(_hall_store, "fetch_photo", lambda *a, **k: dict(PHOTO))
    h = FakeHandler({"ai_off": ["ai_scout", "ai_arch", "ai_geo"]})
    hall._api_hall_geo_new(h)
    code, obj = h.last()
    assert code == 200 and obj["ok"] is True
    rnd = obj["round"]
    assert [p["id"] for p in rnd["players"]] == ["you", "ai_yuke"]
    # 名册随题面下发，前端才能显示每个 AI 的风格介绍
    assert len(rnd["ai_roster"]) == 4
    assert all(p.get("desc") for p in rnd["ai_roster"])


def test_hall_geo_full_flow(monkeypatch, _hall_store):
    monkeypatch.setattr(_hall_store, "fetch_photo", lambda *a, **k: dict(PHOTO))
    h = FakeHandler({"ai_count": 1})
    hall._api_hall_geo_new(h)
    code, obj = h.last()
    assert code == 200 and obj["ok"] is True
    rnd = obj["round"]
    rid = rnd["id"]
    # 题面里绝不能出现真值，否则前端一抓包就知道答案了
    assert "lat" not in rnd and "lng" not in rnd
    assert [p["kind"] for p in rnd["players"]] == ["human", "ai"]
    assert rnd["guesses"] == {} and "ai_vision" in rnd

    human = next(p["id"] for p in rnd["players"] if p["kind"] == "human")
    h2 = FakeHandler({"round_id": rid, "player_id": human, "lat": 48.9, "lng": 2.3})
    hall._api_hall_geo_guess(h2)
    assert h2.last()[0] == 200 and h2.last()[1]["guess"]["mode"] == "human"

    h3 = FakeHandler({"round_id": rid})
    hall._api_hall_geo_reveal(h3)
    code3, obj3 = h3.last()
    assert code3 == 200 and obj3["result"]["truth"] == {"lat": 48.8501, "lng": 2.3004}
    assert obj3["result"]["winner"]["kind"] == "human"


def test_hall_geo_rejects_bad_round_and_human_as_ai(_hall_store):
    h = FakeHandler({"round_id": "nope", "player_id": "you", "lat": 1, "lng": 1})
    hall._api_hall_geo_guess(h)
    assert h.last()[0] == 400

    monkeypatch_h = FakeHandler({"round_id": "nope"})
    hall._api_hall_geo_reveal(monkeypatch_h)
    assert monkeypatch_h.last()[0] == 400

    a = FakeHandler({"round_id": "nope", "player_id": "ai_scout"})
    hall._api_hall_geo_ai(a)
    assert a.last()[0] == 400

