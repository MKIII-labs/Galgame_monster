# -*- coding: utf-8 -*-
"""功能大厅后端单测：地理计算 / 题面归一化 / 结算排名 / 坐标解析 / 对局生命周期。

不联网：所有外部依赖都靠注入或 monkeypatch。
"""
import math

import pytest

from app import hall


# ---------------- 地理计算 ----------------

def test_haversine_known_distance():
    # 巴黎 → 伦敦 实测大圆距离约 343.5 km
    d = hall.haversine_km(48.8566, 2.3522, 51.5074, -0.1278)
    assert d == pytest.approx(344, abs=3)


def test_haversine_same_point_is_zero():
    assert hall.haversine_km(31.23, 121.47, 31.23, 121.47) == pytest.approx(0, abs=1e-9)


def test_haversine_antipodal_is_half_circumference():
    d = hall.haversine_km(0, 0, 0, 180)
    assert d == pytest.approx(math.pi * hall.EARTH_R_KM, rel=1e-6)


def test_haversine_is_symmetric():
    a = hall.haversine_km(10, 20, -35, 145)
    b = hall.haversine_km(-35, 145, 10, 20)
    assert a == pytest.approx(b, rel=1e-12)


def test_clamp_latlng_rejects_bad_input():
    assert hall.clamp_latlng(48.8, 2.3) == (48.8, 2.3)
    assert hall.clamp_latlng("48.8", "2.3") == (48.8, 2.3)
    assert hall.clamp_latlng(91, 0) == (None, None)          # 纬度越界：一定是哪儿错了
    assert hall.clamp_latlng(None, 0) == (None, None)
    assert hall.clamp_latlng(float("nan"), 0) == (None, None)
    assert hall.clamp_latlng(float("inf"), 0) == (None, None)


def test_clamp_latlng_wraps_longitude_only():
    # 经度允许绕圈（Leaflet 跨 180° 平移会给 ±180 之外的值），纬度不允许
    assert hall.clamp_latlng(0, 190)[1] == pytest.approx(-170)
    assert hall.clamp_latlng(0, -190)[1] == pytest.approx(170)


# ---------------- 题面归一化 ----------------

def _feat(**over):
    feat = {
        "id": "abc",
        "geometry": {"type": "Point", "coordinates": [2.3004, 48.8501]},
        "properties": {"license": "CC-BY-SA-4.0", "geovisio:producer": "OSM-FR",
                       "datetime": "2024-01-01T00:00:00+00:00"},
        "assets": {"sd": {"href": "https://example.org/sd.jpg"},
                   "thumb": {"href": "https://example.org/t.jpg"}},
    }
    feat.update(over)
    return feat


def test_normalize_photo_swaps_lng_lat():
    # GeoJSON 是 [经度, 纬度]，写反了整局的距离都会离谱 —— 这条专门钉住它
    p = hall.normalize_photo(_feat())
    assert (p["lat"], p["lng"]) == (48.8501, 2.3004)
    assert p["image_url"] == "https://example.org/sd.jpg"
    assert p["thumb_url"] == "https://example.org/t.jpg"
    assert "Panoramax" in p["credit"] and "OSM-FR" in p["credit"]


def test_normalize_photo_rejects_equirectangular():
    pano = _feat(properties={"pers:interior_orientation":
                             {"sensor_array_dimensions": [5760, 2880]}})
    assert hall.normalize_photo(pano) is None


def test_normalize_photo_rejects_incomplete():
    assert hall.normalize_photo(None) is None
    assert hall.normalize_photo(_feat(geometry={"type": "Point", "coordinates": []})) is None
    assert hall.normalize_photo(_feat(geometry={"type": "Polygon", "coordinates": [[0, 0]]})) is None
    assert hall.normalize_photo(_feat(assets={})) is None
    assert hall.normalize_photo(_feat(geometry={"type": "Point", "coordinates": [0, 99]})) is None


def test_normalize_photo_falls_back_to_hd():
    p = hall.normalize_photo(_feat(assets={"hd": {"href": "https://example.org/hd.jpg"}}))
    assert p["image_url"] == "https://example.org/hd.jpg"


def test_fetch_photo_skips_empty_regions(monkeypatch):
    calls = []

    class _Resp:
        def __init__(self, feats):
            self._feats = feats

        def raise_for_status(self):
            pass

        def json(self):
            return {"features": self._feats}

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(params["bbox"])
        # 只有最后一个区域有照片：要求遍历而不在半路失败
        return _Resp([_feat()] if len(calls) == 3 else [])

    monkeypatch.setattr(hall.requests, "get", fake_get)
    regions = [{"name": "a", "bbox": [0, 0, 1, 1]},
               {"name": "b", "bbox": [1, 1, 2, 2]},
               {"name": "c", "bbox": [2, 2, 3, 3]}]
    p = hall.fetch_photo(regions=regions, tries=3)
    assert p["lat"] == pytest.approx(48.8501)
    assert len(calls) == 3


def test_fetch_photo_raises_when_all_empty(monkeypatch):
    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"features": []}

    monkeypatch.setattr(hall.requests, "get", lambda *a, **k: _Resp())
    with pytest.raises(RuntimeError):
        hall.fetch_photo(regions=[{"name": "a", "bbox": [0, 0, 1, 1]}], tries=2)


# ---------------- 结算排名 ----------------

def test_score_round_orders_by_distance():
    truth = (48.8566, 2.3522)
    rows = hall.score_round([
        {"player_id": "far", "lat": 40.0, "lng": 2.3522},
        {"player_id": "near", "lat": 48.9, "lng": 2.3522},
        {"player_id": "mid", "lat": 47.0, "lng": 2.3522},
    ], truth)
    assert [r["player_id"] for r in rows] == ["near", "mid", "far"]
    assert [r["rank"] for r in rows] == [1, 2, 3]
    assert rows[0]["distance_km"] < rows[1]["distance_km"] < rows[2]["distance_km"]


def test_score_round_shares_rank_on_tie():
    truth = (0.0, 0.0)
    rows = hall.score_round([
        {"player_id": "a", "lat": 0.0, "lng": 1.0},
        {"player_id": "b", "lat": 0.0, "lng": -1.0},
        {"player_id": "c", "lat": 0.0, "lng": 5.0},
    ], truth)
    ranks = {r["player_id"]: r["rank"] for r in rows}
    assert ranks["a"] == ranks["b"] == 1
    assert ranks["c"] == 3                                   # 并列后下一名跳号


def test_score_round_skips_invalid_and_keeps_all_ranks_assigned():
    rows = hall.score_round([{"player_id": "bad", "lat": None, "lng": 0},
                             {"player_id": "ok", "lat": 1.0, "lng": 1.0}], (0.0, 0.0))
    assert [r["player_id"] for r in rows] == ["ok"]


# ---------------- AI 回复解析 ----------------

def test_parse_latlng_json():
    assert hall.parse_latlng('{"lat": 48.85, "lng": 2.35, "confidence": 0.6}') == (48.85, 2.35)
    assert hall.parse_latlng('```json\n{"latitude": -33.87, "longitude": 151.21}\n```') == \
        (-33.87, 151.21)


def test_parse_latlng_directions():
    assert hall.parse_latlng("It looks like 48.85N 2.35E") == (48.85, 2.35)
    assert hall.parse_latlng("约为北纬 35.68，东经 139.76") == (35.68, 139.76)
    assert hall.parse_latlng("南纬 33.87 东经 151.21") == (-33.87, 151.21)
    assert hall.parse_latlng("N48.85 E2.35") == (48.85, 2.35)
    # 英文句子里的 w/e/s/n 不能被当成方位（否则整条会读反）
    assert hall.parse_latlng("This seems like somewhere near 48.85 and 2.35") is None


def test_parse_latlng_dms():
    got = hall.parse_latlng('48°51\'30"N 2°17\'40"E')
    assert got[0] == pytest.approx(48.8583, abs=1e-3)
    assert got[1] == pytest.approx(2.2944, abs=1e-3)


def test_parse_latlng_labelled_and_bare_pair():
    assert hall.parse_latlng("纬度: 48.85 经度: 2.35") == (48.85, 2.35)
    assert hall.parse_latlng("48.85, 2.35") == (48.85, 2.35)
    # 写反（纬度位置放了 >90 的数）时自动交换，而不是直接判废
    assert hall.parse_latlng("151.21, -33.87") == (-33.87, 151.21)


def test_parse_latlng_rejects_junk():
    assert hall.parse_latlng("") is None
    assert hall.parse_latlng("我不确定这是哪里") is None
    assert hall.parse_latlng("根据云量判断有 3 到 4 成的可能性") is None


# ---------------- AI 参与者 ----------------

def test_ai_roster_clamps_count():
    assert hall.ai_roster(0) == []
    assert len(hall.ai_roster(2)) == 2
    assert len(hall.ai_roster(99)) == hall.MAX_AI_PLAYERS
    assert len(hall.ai_roster("x")) == 0
    assert all(p["kind"] == "ai" for p in hall.ai_roster(3))


def test_ai_roster_includes_yuke():
    assert hall.MAX_AI_PLAYERS == 4
    ids = [p["id"] for p in hall.ai_roster(hall.MAX_AI_PLAYERS)]
    assert "ai_yuke" in ids
    yuke = next(p for p in hall.ai_roster(hall.MAX_AI_PLAYERS) if p["id"] == "ai_yuke")
    assert yuke["tag"] == "B站"
    assert yuke["prompt"] == "yuke"


def test_public_round_exposes_ai_tag():
    r = hall.new_round(ai_count=4, photo=_photo())
    pub = hall.public_round(r)
    yuke = next(p for p in pub["players"] if p["id"] == "ai_yuke")
    assert yuke["tag"] == "B站"
    # 真人玩家没有 tag 字段，前端用 kind 区分
    assert "tag" not in pub["players"][0]


def test_ai_roster_off_excludes_selected():
    r = hall.new_round(photo=_photo(), ai_off=["ai_yuke", "ai_geo"])
    assert [p["id"] for p in r["players"]] == ["you", "ai_scout", "ai_arch"]


def test_ai_roster_off_empty_means_all():
    # 空关闭名单 = 全员上场，不再受 ai_count 的默认值限制
    r = hall.new_round(photo=_photo(), ai_off=[])
    assert len(r["players"]) == 1 + hall.MAX_AI_PLAYERS


def test_ai_catalog_has_style_desc():
    cat = hall.ai_catalog()
    assert [p["id"] for p in cat] == [p["id"] for p in hall.AI_PLAYERS]
    assert all(p.get("desc") for p in cat)
    yuke = next(p for p in cat if p["id"] == "ai_yuke")
    assert yuke["tag"] == "B站" and "候选国" in yuke["desc"]


def test_public_round_exposes_ai_desc():
    r = hall.new_round(ai_count=4, photo=_photo())
    pub = hall.public_round(r)
    yuke = next(p for p in pub["players"] if p["id"] == "ai_yuke")
    assert "desc" in yuke and yuke["desc"]


def test_sample_regions_mixes_non_europe():
    picked = hall._sample_regions(6)
    assert len(picked) == 6
    continents = {r["continent"] for r in picked}
    assert "欧洲" in continents and (continents - {"欧洲"})


def test_ai_locate_falls_back_to_blind(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no image")

    monkeypatch.setattr(hall, "_read_remote_image", boom)
    got = hall.ai_locate("http://127.0.0.1:1/x.jpg", "随便看看")
    assert got["mode"] == "blind"
    assert -90 <= got["lat"] <= 90 and -180 <= got["lng"] <= 180
    # 盲猜也必须落在题库区域内，否则观感上是「AI 被扔进海里」
    in_region = any(b[1] <= got["lat"] <= b[3] and b[0] <= got["lng"] <= b[2]
                    for b in (r["bbox"] for r in hall.GEO_REGIONS))
    assert in_region
    assert got["note"]


def test_ai_uses_parsed_coordinates_when_model_answers(monkeypatch):
    monkeypatch.setattr(hall, "_read_remote_image", lambda url, **k: ("image/jpeg", "AAA"))
    monkeypatch.setattr(hall, "get_api_conf",
                        lambda p: ("k", "http://x", "m") if p == "vision" else ("", "", ""))
    monkeypatch.setattr(hall.imaging, "_recognize_with",
                        lambda *a, **k: '{"lat": 35.68, "lng": 139.76, "clue": "日语招牌"}')
    got = hall.ai_locate("http://x/y.jpg", "看建筑")
    assert (got["lat"], got["lng"]) == (35.68, 139.76)
    assert got["mode"] == "vision"
    assert got["note"] == "日语招牌"


def test_imaging_entrypoint_still_exists():
    # hall 依赖 imaging 的识图入口：改名要在这里先炸，而不是等线上没人应答
    assert callable(getattr(hall.imaging, "_recognize_with", None))


def test_reason_parsing_variants():
    """理由既能从标准 JSON 取，也要容忍围栏 / 字符串 / 损坏的 JSON。"""
    assert hall._reason_of(
        '{"lat": 1, "lng": 2, "reason": ["A", "B", "C", "D", "E"]}'
    ) == ["A", "B", "C", "D"]                       # 最多 4 条
    assert hall._reason_of(
        '```json\n{"reason": ["日语招牌", "左行车道"]}\n```'
    ) == ["日语招牌", "左行车道"]
    assert hall._reason_of('{"reason": "只给了一句话"}') == ["只给了一句话"]
    assert hall._reason_of("没有 JSON，也没有 reason") == []
    assert hall._reason_of('{"reason": ["' + "长" * 300 + '"]}')[0][:1] == "长"


def test_ai_locate_returns_reason(monkeypatch):
    monkeypatch.setattr(hall, "_read_remote_image", lambda url, **k: ("image/jpeg", "AAA"))
    monkeypatch.setattr(hall, "get_api_conf",
                        lambda p: ("k", "http://x", "m") if p == "vision" else ("", "", ""))
    monkeypatch.setattr(
        hall.imaging, "_recognize_with",
        lambda *a, **k: ('{"lat": 35.68, "lng": 139.76, "clue": "日语招牌", '
                         '"reason": ["招牌是日文", "车辆靠左行驶"]}'))
    got = hall.ai_locate("http://x/y.jpg", "看建筑")
    assert got["reason"] == ["招牌是日文", "车辆靠左行驶"]


def test_round_stores_and_reveals_reason():
    r = hall.new_round(ai_count=1, photo=_photo())
    hall.put_guess(r["id"], "ai_scout", 40.0, 2.35, mode="vision",
                   note="一句话", reason=["依据一", "依据二", "依据三", "依据四", "依据五"])
    pub = hall.public_round(r)
    assert pub["guesses"]["ai_scout"]["reason"] == ["依据一", "依据二", "依据三", "依据四"]

    res, err = hall.reveal(r["id"])
    assert err == ""
    row = next(x for x in res["results"] if x["player_id"] == "ai_scout")
    assert row["reason"] == ["依据一", "依据二", "依据三", "依据四"]


def test_round_reason_defaults_empty_for_human():
    r = hall.new_round(ai_count=0, photo=_photo())
    hall.put_guess(r["id"], hall.HUMAN_ID, 1.0, 1.0)
    pub = hall.public_round(r)
    assert pub["guesses"][hall.HUMAN_ID]["reason"] == []
    res, _ = hall.reveal(r["id"])
    row = next(x for x in res["results"] if x["player_id"] == hall.HUMAN_ID)
    assert row["reason"] == []


# ---------------- 对局生命周期 ----------------

@pytest.fixture(autouse=True)
def _clean_store():
    hall.reset_store()
    yield
    hall.reset_store()


def _photo():
    return hall.normalize_photo(_feat())


def test_round_lifecycle():
    r = hall.new_round(ai_count=2, photo=_photo())
    assert [p["id"] for p in r["players"]] == [hall.HUMAN_ID, "ai_scout", "ai_arch"]

    pub = hall.public_round(r)
    assert "lat" not in pub and "lng" not in pub               # 题面绝不能带真值
    assert pub["guesses"] == {}

    g, err = hall.put_guess(r["id"], hall.HUMAN_ID, 48.9, 2.35)
    assert err == "" and g["mode"] == "human"
    _, err = hall.put_guess(r["id"], "ai_scout", 40.0, 2.35, mode="blind")
    assert err == ""

    res, err = hall.reveal(r["id"])
    assert err == ""
    assert res["truth"] == {"lat": 48.8501, "lng": 2.3004}
    winner = res["winner"]
    assert winner["player_id"] == hall.HUMAN_ID              # 48.9/2.35 比 40.0/2.35 近
    assert winner["rank"] == 1
    missing = [x for x in res["results"] if x["rank"] is None]
    assert [x["player_id"] for x in missing] == ["ai_arch"]  # 没标的人单列


def test_round_reveal_is_idempotent():
    r = hall.new_round(ai_count=0, photo=_photo())
    hall.put_guess(r["id"], hall.HUMAN_ID, 1.0, 1.0)
    first, _ = hall.reveal(r["id"])
    second, _ = hall.reveal(r["id"])
    assert first == second


def test_round_rejects_guess_after_reveal_and_on_unknown_round():
    r = hall.new_round(ai_count=0, photo=_photo())
    hall.reveal(r["id"])
    assert hall.put_guess(r["id"], hall.HUMAN_ID, 1.0, 1.0)[1] != ""
    assert hall.put_guess("nope", hall.HUMAN_ID, 1.0, 1.0)[1] != ""
    assert hall.reveal("nope")[1] != ""


def test_round_rejects_unknown_player_and_bad_coords():
    r = hall.new_round(ai_count=0, photo=_photo())
    assert hall.put_guess(r["id"], "ghost", 1.0, 1.0)[1] == "对局里没有这个参与者"
    assert hall.put_guess(r["id"], hall.HUMAN_ID, "x", 1.0)[1] == "坐标不合法"


def test_round_guess_can_be_overwritten():
    r = hall.new_round(ai_count=0, photo=_photo())
    hall.put_guess(r["id"], hall.HUMAN_ID, 0.0, 0.0)
    hall.put_guess(r["id"], hall.HUMAN_ID, 10.0, 10.0)
    res, _ = hall.reveal(r["id"])
    assert len([x for x in res["results"] if x["player_id"] == hall.HUMAN_ID]) == 1
    row = next(x for x in res["results"] if x["player_id"] == hall.HUMAN_ID)
    assert row["lat"] == 10.0


def test_ai_context_guards(monkeypatch):
    r = hall.new_round(ai_count=1, photo=_photo())
    ctx, err = hall.ai_context(r["id"], "ai_scout")
    assert err == "" and ctx["image_url"] == "https://example.org/sd.jpg"
    assert hall.ai_context(r["id"], hall.HUMAN_ID)[1] != ""     # 真人不是 AI 参与者
    assert hall.ai_context(r["id"], "ai_arch")[1] != ""         # 不在名单里


def test_ai_capability_reflects_keys(monkeypatch):
    monkeypatch.setattr(hall, "use_chat_for_vision", lambda: False)
    monkeypatch.setattr(hall, "get_api_conf", lambda p: ("", "", ""))
    assert hall.ai_capability() == "none"
    monkeypatch.setattr(hall, "get_api_conf",
                        lambda p: ("k", "", "") if p == "chat" else ("", "", ""))
    assert hall.ai_capability() == "chat"
    monkeypatch.setattr(hall, "get_api_conf",
                        lambda p: ("k", "", "") if p == "vision" else ("", "", ""))
    assert hall.ai_capability() == "vision"


def test_locate_order_follows_vision_setting(monkeypatch):
    """两条通道都有密钥时，先试哪条必须跟随设置 —— 与「上传图片识别」保持一致。"""
    monkeypatch.setattr(hall, "get_api_conf", lambda p: ("k", "", ""))
    monkeypatch.setattr(hall, "use_chat_for_vision", lambda: True)
    assert hall._locate_order()[0][0] == "chat"
    assert hall.ai_capability() == "chat"
    monkeypatch.setattr(hall, "use_chat_for_vision", lambda: False)
    assert hall._locate_order()[0][0] == "vision"
    assert hall.ai_capability() == "vision"


def test_region_pool_is_wellformed():
    assert len(hall.GEO_REGIONS) >= 20
    for r in hall.GEO_REGIONS:
        w, s, e, n = r["bbox"]
        assert w < e and s < n, r
        assert -180 <= w and e <= 180 and -90 <= s and n <= 90, r
        assert r["name"]


# ---------------- AI 调度：图片缓存 / 幂等 ----------------

def test_ai_image_is_cached_per_round(monkeypatch):
    calls = []

    def fake_read(url, **kwargs):
        calls.append(url)
        return "image/jpeg", "AAA"

    monkeypatch.setattr(hall, "_read_remote_image", fake_read)
    photo = _photo()
    photo["thumb_url"] = "https://example.org/thumb.jpg"
    r = hall.new_round(ai_count=2, photo=photo)
    assert hall.ai_image(r["id"]) == ("image/jpeg", "AAA")
    assert hall.ai_image(r["id"]) == ("image/jpeg", "AAA")
    assert calls == ["https://example.org/thumb.jpg"]


def test_ai_image_falls_back_to_full(monkeypatch):
    calls = []

    def fake_read(url, **kwargs):
        calls.append(url)
        if url.endswith("thumb.jpg"):
            raise RuntimeError("boom")
        return "image/jpeg", "FULL"

    monkeypatch.setattr(hall, "_read_remote_image", fake_read)
    photo = _photo()
    photo["thumb_url"] = "https://example.org/thumb.jpg"
    r = hall.new_round(ai_count=1, photo=photo)
    assert hall.ai_image(r["id"]) == ("image/jpeg", "FULL")
    assert calls == ["https://example.org/thumb.jpg", photo["image_url"]]


def test_ai_begin_reuses_guess_and_blocks_duplicate():
    r = hall.new_round(ai_count=1, photo=_photo())
    rid, pid = r["id"], "ai_scout"
    ctx, err = hall.ai_begin(rid, pid)
    assert err == "" and ctx["guess"] is None
    ctx2, err2 = hall.ai_begin(rid, pid)
    assert ctx2 is None and "正在" in err2
    hall.ai_end(rid, pid)
    hall.put_guess(rid, pid, 10.0, 20.0, mode="blind")
    ctx3, err3 = hall.ai_begin(rid, pid)
    assert err3 == "" and ctx3["guess"]["lat"] == 10.0
    hall.ai_end(rid, pid)


def test_ai_begin_returns_yuke_template():
    r = hall.new_round(ai_count=4, photo=_photo())
    ctx, err = hall.ai_begin(r["id"], "ai_yuke")
    assert err == "" and ctx["template"] == "yuke"
    hall.ai_end(r["id"], "ai_yuke")


def test_yuke_prompt_can_format_and_keeps_fixed_order():
    # 模板里有 JSON 花括号，必须转义成 {{ }}，否则 .format() 直接抛错
    ask = hall.GEO_AI_ASK_YUKE.format(style="综合判断")
    for key in ("车牌", "道路", "太阳方位", "植被", "建筑", "选点", "候选国", "硬特征"):
        assert key in ask
    assert '{{' not in ask and '}}' not in ask


def test_yuke_locate_uses_yuke_prompt(monkeypatch):
    seen = {}

    def fake(provider, mime, b64, question="", max_tokens=None):
        seen["q"] = question
        return '{"lat": 1, "lng": 2, "reason": ["车牌颜色 → 排除南美"]}'

    monkeypatch.setattr(hall, "get_api_conf",
                        lambda p: ("k", "http://x", "m") if p == "vision" else ("", "", ""))
    monkeypatch.setattr(hall.imaging, "_recognize_with", fake)
    hall._ai_locate_bytes("image/jpeg", "AAA", "", "yuke")
    assert "先列出 2–4 个候选国" in seen["q"]


def test_geo_describe_caches_per_round_and_photo(monkeypatch):
    calls = []

    def fake_recognize(*a, **k):
        calls.append(1)
        return "1. 招牌是拉丁字母\n2. 车辆靠右侧行驶"

    monkeypatch.setattr(hall, "_read_remote_image", lambda url, **k: ("image/jpeg", "AAA"))
    monkeypatch.setattr(hall, "get_api_conf",
                        lambda p: ("k", "http://x", "m") if p == "vision" else ("", "", ""))
    monkeypatch.setattr(hall.imaging, "_recognize_with", fake_recognize)
    hall._DESC_CACHE.clear()

    r1 = hall.new_round(ai_count=0, photo=_photo())
    out, err = hall.geo_describe(r1["id"])
    assert err == "" and out["cached"] is False
    assert "拉丁字母" in out["text"] and "右侧" in out["text"]

    out2, err2 = hall.geo_describe(r1["id"])
    assert err2 == "" and out2["cached"] is True

    # 同一张照片换一局：按 photo_id 命中全局缓存，不再调模型
    r2 = hall.new_round(ai_count=0, photo=_photo())
    out3, err3 = hall.geo_describe(r2["id"])
    assert err3 == "" and out3["cached"] is True
    assert len(calls) == 1


def test_geo_describe_rejects_unknown_round():
    out, err = hall.geo_describe("nope")
    assert out is None and "过期" in err
