# -*- coding: utf-8 -*-
"""B 站读取：WBI 签名、链接解析、字幕/评论格式化与错误处理（全部离线，不联网）"""
import pytest

from app import bilibili
from app.bilibili import BiliError


# ============================================================
# WBI
# ============================================================

def test_wbi_sign_matches_documented_vector(monkeypatch):
    """用官方文档给出的 img_key/sub_key/wts 复现同一个 w_rid"""
    monkeypatch.setattr(bilibili, "wbi_keys",
                        lambda force=False: ("653657f524a547ac981ded72ea172057",
                                             "6e4909c702f846728e64f6007736a338"))
    signed = bilibili.sign({"foo": "114", "bar": "514", "zab": 1919810},
                           wts=1684746387)
    assert signed["wts"] == "1684746387"
    assert signed["w_rid"] == "90efcab09403023875b8516f07e9f9de"


def test_wbi_filters_forbidden_chars(monkeypatch):
    monkeypatch.setattr(bilibili, "wbi_keys", lambda force=False: ("a" * 32, "b" * 32))
    signed = bilibili.sign({"q": "a!b'c(d)e*f"}, wts=1)
    assert signed["q"] == "abcdef"


# ============================================================
# 链接解析
# ============================================================

def test_parse_bv_and_page():
    t = bilibili.parse_target("https://www.bilibili.com/video/BV117411r7R1/?p=3&spm=1")
    assert t == {"bvid": "BV117411r7R1", "avid": None, "page": 3}


def test_parse_bare_bv_and_av():
    assert bilibili.parse_target("BV117411r7R1")["bvid"] == "BV117411r7R1"
    assert bilibili.parse_target("av85440373")["avid"] == 85440373


def test_parse_short_link(monkeypatch):
    monkeypatch.setattr(bilibili, "_expand_short",
                        lambda url: "https://www.bilibili.com/video/BV117411r7R1")
    t = bilibili.parse_target("https://b23.tv/abcd")
    assert t["bvid"] == "BV117411r7R1"


def test_parse_rejects_other_hosts():
    with pytest.raises(BiliError):
        bilibili.parse_target("https://example.com/video/BV117411r7R1")


def test_parse_rejects_empty():
    with pytest.raises(BiliError):
        bilibili.parse_target("")


# ============================================================
# 登录状态
# ============================================================

def test_login_status_not_logged_in(monkeypatch):
    monkeypatch.setattr(bilibili, "_sessdata", lambda: "")
    monkeypatch.setattr(bilibili, "_get",
                        lambda *a, **k: {"code": -101, "data": {"isLogin": False}})
    st = bilibili.login_status(None)
    assert st["ok"] is True
    assert st["logged_in"] is False
    assert st["has_cookie"] is False


def test_login_status_with_cookie(monkeypatch):
    monkeypatch.setattr(bilibili, "_get",
                        lambda *a, **k: {"code": 0,
                                         "data": {"isLogin": True, "uname": "测试用户"}})
    st = bilibili.login_status("sess")
    assert st["logged_in"] is True
    assert st["uname"] == "测试用户"


# ============================================================
# 字幕 / 总结
# ============================================================

def test_subtitles_parses_json(monkeypatch):
    bilibili._DETAIL_CACHE.clear()
    monkeypatch.setattr(bilibili, "_sessdata", lambda: "sess")
    monkeypatch.setattr(bilibili, "sign", lambda params, wts=None: params)
    calls = {"n": 0}

    def fake_get(url, *a, **k):
        calls["n"] += 1
        if "player" in url:
            return {"code": 0, "data": {"subtitle": {"subtitles": [
                {"lan": "en-US", "subtitle_url": "//example/sub.en.json"},
                {"lan": "zh-Hans", "subtitle_url": "//example/sub.zh.json"},
            ]}}}
        return {"body": [{"from": 1.2, "to": 3.4, "content": "第一句"},
                         {"from": 3.4, "to": 5.0, "content": "  "}]}

    monkeypatch.setattr(bilibili, "_get", fake_get)
    segs = bilibili.subtitles({"bvid": "BV1", "cid": 1})
    assert calls["n"] == 2
    assert segs == [{"start": 1.2, "end": 3.4, "content": "第一句"}]


def test_transcript_falls_back_to_official_summary(monkeypatch):
    video = {"title": "标题", "bvid": "BV1", "cid": 1, "duration": 60}
    monkeypatch.setattr(bilibili, "get_video", lambda target, page=None: video)
    monkeypatch.setattr(bilibili, "ai_summary", lambda v: {
        "summary": "一句话", "outline": [], "subtitles": []})
    monkeypatch.setattr(bilibili, "subtitles",
                        lambda v: (_ for _ in ()).throw(BiliError("没有字幕")))
    out = bilibili._cap_transcript({"url": "BV1"}, {})
    assert "官方 AI 总结" in out
    assert "一句话" in out


def test_transcript_paginates_with_offset(monkeypatch):
    video = {"title": "标题", "bvid": "BV1", "cid": 1, "duration": 60}
    segs = [{"start": i, "end": i + 1, "content": "第%d句" % i} for i in range(2000)]
    monkeypatch.setattr(bilibili, "get_video", lambda target, page=None: video)
    monkeypatch.setattr(bilibili, "ai_summary",
                        lambda v: {"summary": "", "outline": [], "subtitles": segs})
    out = bilibili._cap_transcript({"url": "BV1", "offset": 0}, {})
    assert "offset=" in out                  # 超过上限时提示续取
    assert len(out) < bilibili.MAX_TEXT + 300


# ============================================================
# 评论 / 格式化
# ============================================================

def test_format_comments_keeps_like_and_hides_nothing(monkeypatch):
    data = {
        "kind": "roots",
        "video": {"title": "标题"},
        "sort": 1, "total": 2, "acount": 2,
        "items": [
            {"like": 10, "user": "甲", "content": "不错", "rpid": 111, "rcount": 3},
            {"like": 3, "user": "乙", "content": "有道理", "rpid": 222, "rcount": 1},
        ],
    }
    text = bilibili.format_comments(data)
    assert "按点赞" in text
    assert "rpid=111" in text and "回复3" in text
    assert "👍10" in text and "甲：不错" in text


def test_format_info_contains_key_fields():
    v = {"title": "标题", "owner": "UP", "tname": "知识", "duration": 65,
         "pubdate": 1700000000, "stat": {"view": 1, "like": 2, "reply": 3, "danmaku": 4},
         "bvid": "BV1", "pages": [], "desc": "简介", "page": 1}
    text = bilibili.format_info(v)
    assert "标题" in text and "UP" in text and "1:05" in text
    assert "播放 1" in text and "评论 3" in text


# ============================================================
# 能力错误前缀 / 注册
# ============================================================

def test_capability_errors_are_marked(monkeypatch):
    monkeypatch.setattr(bilibili, "get_video",
                        lambda *a, **k: (_ for _ in ()).throw(BiliError("失败了")))
    assert bilibili._cap_info({"url": "BV1"}, {}).startswith("❌")


def test_capabilities_registered():
    from app import capabilities
    for cid in ("bili.info", "bili.transcript", "bili.comments", "bili.up"):
        cap = capabilities.get(cid)
        assert cap is not None and cap.chat is True


# ============================================================
# UP 主：昵称 / 主页 / 投稿列表
# ============================================================

def test_space_mid_parsing():
    assert bilibili._space_mid("https://space.bilibili.com/198439") == 198439
    assert bilibili._space_mid("https://m.bilibili.com/space/198439") == 198439
    assert bilibili._space_mid("198439") == 198439
    assert bilibili._space_mid("可可味") is None


def test_resolve_user_prefers_exact_match(monkeypatch):
    users = [
        {"mid": 1, "uname": "可可味Mw", "fans": 7414, "videos": 2, "sign": ""},
        {"mid": 198439, "uname": "可可味", "fans": 1224641, "videos": 286, "sign": ""},
    ]
    monkeypatch.setattr(bilibili, "search_user", lambda q: users)
    u = bilibili.resolve_user("可可味")
    assert u["mid"] == 198439
    assert u["candidates"] == []


def test_resolve_user_returns_candidates(monkeypatch):
    users = [
        {"mid": 1, "uname": "可可味Mw", "fans": 1, "videos": 1, "sign": ""},
        {"mid": 2, "uname": "可可味的薄荷喵", "fans": 2, "videos": 2, "sign": ""},
    ]
    monkeypatch.setattr(bilibili, "search_user", lambda q: users)
    u = bilibili.resolve_user("可可")
    assert u["mid"] == 1
    assert len(u["candidates"]) == 2


def test_list_up_videos_parses(monkeypatch):
    monkeypatch.setattr(bilibili, "sign", lambda params, wts=None: params)

    def fake_get(url, *a, **k):
        return {"code": 0, "data": {"page": {"count": 286}, "list": {"vlist": [
            {"bvid": "BV1mbhW6bEBa", "title": "标题", "created": 1790136000,
             "length": "14:00", "play": 38583, "comment": 10},
        ]}}}

    monkeypatch.setattr(bilibili, "_get", fake_get)
    data = bilibili.list_up_videos(198439)
    assert data["total"] == 286
    assert data["items"][0]["bvid"] == "BV1mbhW6bEBa"
    assert data["items"][0]["play"] == 38583


def test_format_up_contains_video_and_candidates():
    user = {"mid": 1, "uname": "可可味Mw", "fans": 10, "videos": 2,
            "candidates": [{"mid": 198439, "uname": "可可味"}]}
    data = {"order": "pubdate", "total": 2, "items": [
        {"bvid": "BV1x", "title": "视频一", "created": 1790136000,
         "length": "1:00", "play": 5, "comment": 1},
    ]}
    text = bilibili.format_up(user, data)
    assert "可可味Mw" in text and "BV1x" in text
    assert "相近账号" in text
    assert "space.bilibili.com/1" in text


# ============================================================
# 全量增强：详情 / 评论线程 / 弹幕 / 时间轴 / UP 关键词
# ============================================================

DM_XML = ('<i>'
          '<d p="12.5,1,25,16777215,0,0,0,0">第一句</d>'
          '<d p="65,1,25,16777215,0,0,0,0">第二句</d>'
          '<d p="70,1,25,16777215,0,0,0,0">第二句</d>'
          '</i>')


def test_video_detail_tags_related_collection(monkeypatch):
    bilibili._DETAIL_CACHE.clear()
    monkeypatch.setattr(bilibili, "get_video", lambda target, page=None: {
        "bvid": "BV1", "aid": 1, "cid": 2, "title": "T", "duration": 60,
        "pages": [], "page": 1, "desc": "", "owner": "U", "tname": "", "stat": {}})
    monkeypatch.setattr(bilibili, "_get", lambda *a, **k: {"code": 0, "data": {
        "Tags": [{"tag_name": "音乐"}, {"tag_name": "AI"}],
        "Related": [{"bvid": "BV2", "title": "相关", "owner": {"name": "UP2"},
                     "stat": {"view": 10}}],
        "View": {"ugc_season": {"title": "合集", "ep_count": 3}},
    }})
    d = bilibili.video_detail("BV1")
    assert d["tags"] == ["音乐", "AI"]
    assert d["related"][0]["bvid"] == "BV2"
    assert "合集" in d["collection"]


def test_comments_rpid_reads_thread(monkeypatch):
    bilibili._COMMENT_CACHE.clear()
    monkeypatch.setattr(bilibili, "get_video", lambda target, page=None: {"aid": 1, "title": "T"})
    monkeypatch.setattr(bilibili, "_get", lambda *a, **k: {"code": 0, "data": {"replies": [
        {"rpid": 9, "like": 1, "member": {"uname": "甲"},
         "content": {"message": "回复内容"}},
    ]}})
    data = bilibili.comments("BV1", rpid=123, pages=1)
    assert data["kind"] == "replies"
    text = bilibili.format_comments(data)
    assert "楼中楼" in text and "回复内容" in text


def test_comments_offset_truncates(monkeypatch):
    data = {"kind": "roots", "video": {"title": "T"}, "sort": 1,
            "total": 200, "acount": 200,
            "items": [{"like": i, "user": "u", "content": "x" * 120,
                       "rpid": i, "rcount": 0} for i in range(80)]}
    text = bilibili.format_comments(data)
    assert "下一段传 offset=" in text
    assert len(text) < bilibili.MAX_TEXT + 200


def test_parse_danmaku():
    items = bilibili._parse_danmaku(DM_XML)
    assert len(items) == 3
    assert items[0]["t"] == 12.5 and items[0]["text"] == "第一句"


def test_danmaku_digest_and_raw(monkeypatch):
    monkeypatch.setattr(bilibili, "get_video",
                        lambda *a, **k: {"cid": 1, "title": "T", "duration": 120})
    monkeypatch.setattr(bilibili, "_danmaku_xml", lambda cid: DM_XML)
    digest = bilibili.danmaku("BV1")
    assert "可解析弹幕：3" in digest
    assert "密度峰值" in digest
    raw = bilibili.danmaku("BV1", mode="raw", start=60, end=80)
    assert "第一句" not in raw and "第二句" in raw


def test_analyze_includes_summary_and_transcript(monkeypatch):
    monkeypatch.setattr(bilibili, "video_detail",
                        lambda target, page=None: {"title": "T", "bvid": "BV1", "duration": 60})
    monkeypatch.setattr(bilibili, "format_info", lambda v: "INFO")
    monkeypatch.setattr(bilibili, "_transcript_segments", lambda v: (
        {"summary": "一句话", "outline": []},
        [{"start": 0, "content": "第一句"}], "作者字幕"))
    out = bilibili.analyze("BV1")
    assert "INFO" in out and "B站官方 AI 总结" in out and "第一句" in out


def test_timeline_merges_chapters_and_danmaku(monkeypatch):
    bilibili._TIMELINE_CACHE.clear()
    monkeypatch.setattr(bilibili, "video_detail",
                        lambda target, page=None: {"title": "T", "bvid": "BV1", "duration": 120})
    monkeypatch.setattr(bilibili, "player_info",
                        lambda v: {"view_points": [{"content": "第一章", "from": 0, "to": 60}]})
    monkeypatch.setattr(bilibili, "_transcript_segments", lambda v: (
        None, [{"start": 1, "content": "字幕一"}], "作者字幕"))
    monkeypatch.setattr(bilibili, "_danmaku_xml", lambda cid: DM_XML)
    out = bilibili.timeline("BV1")
    assert "【时间轴】" in out and "第一章" in out
    assert "字幕一" in out and "弹幕：" in out


def test_up_keyword_filter(monkeypatch):
    bilibili._UP_CACHE.clear()
    monkeypatch.setattr(bilibili, "sign", lambda params, wts=None: params)
    monkeypatch.setattr(bilibili, "_get", lambda *a, **k: {"code": 0, "data": {
        "page": {"count": 2}, "list": {"vlist": [
            {"bvid": "BV1", "title": "AI 教程", "created": 1, "length": "1:00", "play": 1},
            {"bvid": "BV2", "title": "游戏", "created": 2, "length": "1:00", "play": 2},
        ]}}})
    data = bilibili.list_up_videos(1, pages=1, keyword="AI")
    assert data["scanned"] == 2
    assert len(data["items"]) == 1 and data["items"][0]["bvid"] == "BV1"


def test_resolve_user_by_mid_uses_card(monkeypatch):
    monkeypatch.setattr(bilibili, "user_card",
                        lambda mid: {"mid": mid, "uname": "可可味",
                                     "fans": 1, "videos": 286})
    u = bilibili.resolve_user("198439")
    assert u["uname"] == "可可味" and u["videos"] == 286


def test_list_up_falls_back_to_search(monkeypatch):
    """空间接口 412 时，必须用搜索接口兜底，而不是返回「没有投稿」"""
    bilibili._UP_CACHE.clear()
    monkeypatch.setattr(bilibili, "sign", lambda params, wts=None: params)

    def fake_get(url, *a, **k):
        if "arc/search" in url:
            raise bilibili.BiliError("触发 B 站风控（412）")
        return {"code": 0, "data": {"result": [
            {"mid": 198439, "author": "可可味", "bvid": "BV1",
             "title": '<em class="keyword">AI</em> 教程',
             "pubdate": 1, "duration": "1:00", "play": 5},
        ]}}

    monkeypatch.setattr(bilibili, "_get", fake_get)
    data = bilibili.list_up_videos(198439, pages=1, uname="可可味", known_total=286)
    assert data["source"] == "search"
    assert data["total"] == 286
    assert data["items"][0]["title"] == "AI 教程"


def test_format_up_no_items_but_known_videos():
    user = {"mid": 1, "uname": "可可味", "fans": 1, "videos": 286, "candidates": []}
    data = {"order": "pubdate", "total": 286, "scanned": 0, "items": [],
            "source": "search", "note": "风控"}
    text = bilibili.format_up(user, data)
    assert "不代表没有投稿" in text and "286" in text


def test_new_capabilities_registered():
    from app import capabilities
    for cid in ("bili.analyze", "bili.danmaku", "bili.timeline"):
        cap = capabilities.get(cid)
        assert cap is not None and cap.chat is True
