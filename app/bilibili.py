# -*- coding: utf-8 -*-
"""B 站数据读取：视频信息 / 热门评论 / 字幕 / 官方 AI 总结。

边界（有意写死，别扩大）：
- 只处理用户**主动提供**的单个视频链接或 BV/av 号；不做批量、不做账号池；
- 只读公开内容；不下载音视频、不绕过付费 / 大会员 / 充电专属；
- SESSDATA 只用于请求 B 站，不写日志、不进入模型上下文。

接口来源：SocialSisterYi/bilibili-API-collect（社区逆向文档）。
B 站会改接口，失败时返回可读中文原因，不抛裸异常。
"""
import hashlib
import html
import json
import re
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
import zlib
from collections import Counter

import requests

from app import capabilities
from app.config import load_config

API = "https://api.bilibili.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
REFERER = "https://www.bilibili.com/"
TIMEOUT = (5, 20)
MIN_INTERVAL = 1.5          # 同一进程内两次请求的最小间隔（秒）；实测 0.6s 容易触发 412
MAX_TEXT = 7000             # 单次返回给模型的正文上限（对话工具结果上限是 8000）
MAX_COMMENT_PAGES = 10
DEFAULT_COMMENT_PAGES = 3
COMMENT_PAGE_SIZE = 20

SHORT_HOSTS = ("b23.tv", "bili2233.cn")
ALLOW_HOSTS = ("bilibili.com",) + SHORT_HOSTS

_NEED_COOKIE = ("需要 SESSDATA 才能读取字幕 / B 站 AI 总结："
                "到「设置 → B 站」填入（浏览器登录 bilibili.com 后，"
                "从 Cookie 里复制 SESSDATA）")

# WBI 打乱表（官方前端同款）
_MIXIN_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]

_WBI = {"keys": None, "ts": 0.0}
_WBI_LOCK = threading.Lock()
_RATE = {"ts": 0.0}
_RATE_LOCK = threading.Lock()
# B 站风控会看 buvid3/buvid4；匿名也要带，否则部分接口直接 412
_FINGER = {"cookie": "", "ts": 0.0}
_FINGER_LOCK = threading.Lock()
_UP_CACHE = {}
_UP_TTL = 300               # UP 主投稿列表缓存 5 分钟，避免同一轮反复请求
_UP_LOCK = threading.Lock()
# 通用缓存：视频详情 / 评论 / 弹幕 / 时间轴
_DETAIL_CACHE, _DETAIL_TTL = {}, 600
_COMMENT_CACHE, _COMMENT_TTL = {}, 300
_DM_CACHE, _DM_TTL = {}, 600
_TIMELINE_CACHE, _TIMELINE_TTL = {}, 600
_CACHE_LOCK = threading.Lock()


def _cache_get(store, key, ttl):
    with _CACHE_LOCK:
        hit = store.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    return None


def _cache_set(store, key, value, limit=80):
    with _CACHE_LOCK:
        if len(store) > limit:
            for k in list(store)[:10]:
                store.pop(k, None)
        store[key] = (time.time(), value)


class BiliError(Exception):
    """可直接展示给用户的中文错误"""


# ============================================================
# HTTP / WBI
# ============================================================

def _sessdata():
    return str(load_config().get("bili_sessdata") or "").strip()


def _finger_cookie():
    """取 buvid3/buvid4（匿名可用），缓存 12 小时。失败不影响主流程。"""
    with _FINGER_LOCK:
        if _FINGER["cookie"] and time.time() - _FINGER["ts"] < 12 * 3600:
            return _FINGER["cookie"]
        try:
            r = requests.get(API + "/x/frontend/finger/spi",
                             headers={"User-Agent": UA, "Referer": REFERER},
                             timeout=8)
            d = (r.json().get("data") or {}) if r.ok else {}
            b3, b4 = str(d.get("b_3") or ""), str(d.get("b_4") or "")
            if b3:
                _FINGER["cookie"] = "buvid3=%s; buvid4=%s" % (b3, b4)
                _FINGER["ts"] = time.time()
        except (requests.RequestException, ValueError):
            return ""
        return _FINGER["cookie"]


def _headers(sessdata=None, auth=True):
    h = {"User-Agent": UA, "Referer": REFERER, "Origin": "https://www.bilibili.com",
         "Accept": "application/json, text/plain, */*",
         "Accept-Language": "zh-CN,zh;q=0.9"}
    parts = []
    # 公开接口不发送登录 Cookie：只带 SESSDATA 的「残缺登录态」更容易触发风控
    cookie = (sessdata if sessdata is not None else _sessdata()).strip() if auth else ""
    if cookie:
        # SESSDATA 本身含 %2C 等转义，原样放入 Cookie，不要再编码
        parts.append("SESSDATA=" + cookie)
    finger = _finger_cookie()
    if finger:
        parts.append(finger)
    if parts:
        h["Cookie"] = "; ".join(parts)
    return h


def _throttle():
    with _RATE_LOCK:
        wait = MIN_INTERVAL - (time.time() - _RATE["ts"])
        if wait > 0:
            time.sleep(wait)
        _RATE["ts"] = time.time()


def _http_error(e):
    status = getattr(getattr(e, "response", None), "status_code", None)
    if status == 412:
        return BiliError("触发 B 站风控（412），请稍后再试，不要连续快速请求")
    if status == 429:
        return BiliError("B 站请求过于频繁（429），请稍后再试")
    if status == 403:
        return BiliError("B 站拒绝访问（403），可能触发了风控")
    return BiliError("请求 B 站失败：%s" % e)


def _get(url, params=None, sessdata=None, timeout=TIMEOUT, auth=True):
    """GET 并解析 JSON；网络/解析错误统一转成 BiliError。

    auth=False：公开接口，只带 buvid，不发 SESSDATA。
    """
    _throttle()
    try:
        r = requests.get(url, params=params or {}, headers=_headers(sessdata, auth=auth),
                         timeout=timeout)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        raise _http_error(e)
    except ValueError:
        raise BiliError("B 站返回的不是 JSON（可能被风控或接口已变更）")


def _get_raw(url, params=None, auth=False, timeout=TIMEOUT):
    """GET 原始字节（弹幕 XML 等非 JSON 接口）。"""
    _throttle()
    try:
        r = requests.get(url, params=params or {}, headers=_headers(auth=auth),
                         timeout=timeout)
        r.raise_for_status()
        return r.content
    except requests.RequestException as e:
        raise _http_error(e)


def _check(data):
    """检查 api 根对象的 code；成功返回 data 字段。"""
    code = int((data or {}).get("code") or 0)
    if code == 0:
        return data.get("data")
    msg = str((data or {}).get("message") or "")
    if code == -101:
        raise BiliError(_NEED_COOKIE)
    if code == -403:
        raise BiliError("访问权限不足（可能需要登录，或该视频不支持此功能）")
    if code == -352:
        raise BiliError("触发 B 站风控，请稍后再试（不要连续快速请求）")
    if code == 12002:
        raise BiliError("该视频的评论区已关闭")
    raise BiliError("B 站返回错误 %s：%s" % (code, msg or "未知错误"))


def _mixin_key(orig):
    return "".join(orig[i] for i in _MIXIN_TAB)[:32]


def wbi_keys(force=False):
    """取 img_key / sub_key；全站统一、每日轮换，缓存 6 小时。"""
    with _WBI_LOCK:
        now = time.time()
        if not force and _WBI["keys"] and now - _WBI["ts"] < 6 * 3600:
            return _WBI["keys"]
        raw = _get(API + "/x/web-interface/nav", auth=False)
        img = ((raw.get("data") or {}).get("wbi_img") or {})
        def _key(u):
            return str(u or "").rsplit("/", 1)[-1].split(".")[0]
        ik, sk = _key(img.get("img_url")), _key(img.get("sub_url"))
        if not ik or not sk:
            raise BiliError("无法获取 B 站签名密钥（nav 接口未返回 wbi_img）")
        _WBI["keys"] = (ik, sk)
        _WBI["ts"] = now
        return ik, sk


def sign(params, wts=None):
    """WBI 签名：返回带 wts / w_rid 的新参数表。"""
    ik, sk = wbi_keys()
    p = {str(k): str(v) for k, v in (params or {}).items() if v is not None}
    p["wts"] = str(int(wts if wts is not None else time.time()))
    p = {k: "".join(ch for ch in v if ch not in "!'()*")
         for k, v in sorted(p.items())}
    query = urllib.parse.urlencode(p)
    p["w_rid"] = hashlib.md5((query + _mixin_key(ik + sk)).encode("utf-8")).hexdigest()
    return p


# ============================================================
# 链接解析
# ============================================================

def _host_ok(url):
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in ALLOW_HOSTS)


def _expand_short(url):
    """只跟随短链跳转；拒绝跳到 B 站以外的域名。"""
    for _ in range(3):
        host = (urllib.parse.urlparse(url).hostname or "").lower()
        if not any(host == h or host.endswith("." + h) for h in SHORT_HOSTS):
            return url
        _throttle()
        try:
            r = requests.get(url, headers=_headers(auth=False), allow_redirects=False, timeout=8)
        except requests.RequestException as e:
            raise BiliError("短链解析失败：%s" % e)
        loc = r.headers.get("Location") or ""
        if not loc:
            break
        url = urllib.parse.urljoin(url, loc)
    if not _host_ok(url):
        raise BiliError("短链跳转到了非 B 站地址，已停止")
    return url


def parse_target(text):
    """从链接 / BV 号 / av 号里解析出 (bvid | avid, 分P)。

    返回 {"bvid": str, "avid": int|None, "page": int}。
    """
    raw = str(text or "").strip()
    if not raw:
        raise BiliError("缺少 B 站链接或 BV 号")
    if raw.lower().startswith(("http://", "https://")):
        if not _host_ok(raw):
            raise BiliError("只支持 bilibili.com / b23.tv 的链接")
        raw = _expand_short(raw)

    page = 1
    try:
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(raw).query)
        if qs.get("p"):
            page = max(1, int(qs["p"][0]))
    except (ValueError, TypeError):
        page = 1

    m = re.search(r"(BV[0-9A-Za-z]{10})", raw)
    if m:
        return {"bvid": m.group(1), "avid": None, "page": page}
    m = re.search(r"(?:^|[/=])av(\d+)", raw, re.I)
    if m:
        return {"bvid": "", "avid": int(m.group(1)), "page": page}
    raise BiliError("没有识别到 B 站视频链接或 BV 号")


# ============================================================
# 数据读取
# ============================================================

def get_video(target, page=None):
    t = parse_target(target)
    params = {"bvid": t["bvid"]} if t["bvid"] else {"aid": t["avid"]}
    d = _check(_get(API + "/x/web-interface/view", params, auth=False)) or {}
    pages = d.get("pages") or []
    pno = max(1, int(page or t["page"] or 1))
    if pages:
        pno = min(pno, len(pages))
    pg = pages[pno - 1] if pages else {}
    return {
        "bvid": d.get("bvid") or t["bvid"],
        "aid": d.get("aid") or t["avid"],
        "title": d.get("title") or "",
        "desc": d.get("desc") or "",
        "owner": (d.get("owner") or {}).get("name") or "",
        "owner_mid": (d.get("owner") or {}).get("mid"),
        "duration": d.get("duration") or 0,
        "pubdate": d.get("pubdate") or 0,
        "tname": d.get("tname") or "",
        "stat": d.get("stat") or {},
        "page": pno,
        "cid": pg.get("cid") or d.get("cid"),
        "page_title": pg.get("part") or "",
        "pages": [{"page": x.get("page"), "cid": x.get("cid"),
                   "part": x.get("part") or "", "duration": x.get("duration") or 0}
                  for x in pages],
        "subtitle_allowed": bool((d.get("subtitle") or {}).get("allow_submit")),
    }


def video_detail(target, page=None):
    """视频详情 + 标签 + 相关视频 + 合集信息（公开接口，缓存 10 分钟）。"""
    video = get_video(target, page=page)
    key = ("detail", video.get("bvid"))
    cached = _cache_get(_DETAIL_CACHE, key, _DETAIL_TTL)
    if cached:
        return cached
    tags, related, collection = [], [], ""
    try:
        d = _check(_get(API + "/x/web-interface/view/detail",
                        {"bvid": video.get("bvid")}, auth=False)) or {}
        for t in d.get("Tags") or []:
            name = str(t.get("tag_name") or "").strip()
            if name:
                tags.append(name)
        for r in d.get("Related") or []:
            related.append({"bvid": r.get("bvid") or "",
                            "title": str(r.get("title") or ""),
                            "owner": str((r.get("owner") or {}).get("name") or ""),
                            "play": int((r.get("stat") or {}).get("view") or 0)})
        season = (d.get("View") or {}).get("ugc_season") or {}
        if season:
            collection = "%s（共 %s 集）" % (season.get("title") or "",
                                            season.get("ep_count") or 0)
    except BiliError:
        pass
    out = dict(video)
    out.update({"tags": tags[:15], "related": related[:8], "collection": collection})
    _cache_set(_DETAIL_CACHE, key, out)
    return out


def ai_summary(video):
    """B 站官方 AI 总结：summary / outline / AI 字幕。需要 SESSDATA + WBI。"""
    key = ("summary", video.get("bvid"), video.get("cid"))
    cached = _cache_get(_DETAIL_CACHE, key, _DETAIL_TTL)
    if cached is not None:
        return cached
    if not _sessdata():
        raise BiliError(_NEED_COOKIE)
    params = sign({"bvid": video.get("bvid") or "",
                   "cid": video.get("cid") or 0,
                   "up_mid": video.get("owner_mid") or 0})
    d = _check(_get(API + "/x/web-interface/view/conclusion/get", params)) or {}
    model = d.get("model_result") or {}

    outline = []
    for o in model.get("outline") or []:
        outline.append({
            "title": o.get("title") or "",
            "timestamp": o.get("timestamp") or 0,
            "points": [{"timestamp": p.get("timestamp") or 0,
                        "content": p.get("content") or ""}
                       for p in (o.get("part_outline") or [])],
        })

    segments = []
    for part in model.get("subtitle") or []:
        for s in part.get("part_subtitle") or []:
            text = str(s.get("content") or "").strip()
            if text:
                segments.append({"start": s.get("start_timestamp") or 0,
                                 "end": s.get("end_timestamp") or 0,
                                 "content": text})

    out = {"code": d.get("code"), "summary": str(model.get("summary") or "").strip(),
           "outline": outline, "subtitles": segments}
    _cache_set(_DETAIL_CACHE, key, out)
    return out


def _pick_subtitle(subs):
    """优先中文；其次 ai 状态正常的；最后取第一条。"""
    def score(s):
        lan = str(s.get("lan") or "").lower()
        ai = 1 if lan.startswith("ai-") else 0
        zh = 2 if (lan.startswith("zh") or "中文" in str(s.get("lan_doc") or "")) else 0
        return (zh, ai)
    return sorted(subs, key=score, reverse=True)[0]


def player_info(video):
    """播放器信息：章节 view_points + 字幕列表（需要 SESSDATA，缓存 10 分钟）。"""
    key = ("player", video.get("cid"))
    cached = _cache_get(_DETAIL_CACHE, key, _DETAIL_TTL)
    if cached:
        return cached
    params = sign({"bvid": video.get("bvid") or "", "cid": video.get("cid") or 0})
    d = _check(_get(API + "/x/player/wbi/v2", params)) or {}
    out = {"view_points": d.get("view_points") or [],
           "subtitles": ((d.get("subtitle") or {}).get("subtitles") or [])}
    _cache_set(_DETAIL_CACHE, key, out)
    return out


def subtitles(video):
    """作者 / AI 字幕；未登录时 B 站返回空列表。"""
    key = ("subs", video.get("bvid"), video.get("cid"))
    cached = _cache_get(_DETAIL_CACHE, key, _DETAIL_TTL)
    if cached is not None:
        return cached
    if not _sessdata():
        raise BiliError(_NEED_COOKIE)
    subs = player_info(video).get("subtitles") or []
    if not subs:
        raise BiliError("该视频没有可读字幕（需要登录，且视频带作者字幕或 AI 字幕）")
    url = str(_pick_subtitle(subs).get("subtitle_url") or "").strip()
    if not url:
        raise BiliError("字幕地址为空")
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("/"):
        url = API + url
    raw = _get(url, auth=False)
    out = []
    for x in raw.get("body") or []:
        text = str(x.get("content") or "").strip()
        if text:
            out.append({"start": x.get("from") or 0, "end": x.get("to") or 0,
                        "content": text})
    if not out:
        raise BiliError("字幕内容为空")
    _cache_set(_DETAIL_CACHE, key, out)
    return out


def _transcript_segments(video):
    """返回 (official_summary, segments, source)：官方 AI 字幕优先，其次作者字幕。"""
    summary = None
    try:
        summary = ai_summary(video)
    except BiliError:
        summary = None
    segments = (summary or {}).get("subtitles") or []
    source = "B站 AI 字幕"
    if not segments:
        try:
            segments = subtitles(video)
            source = "作者字幕"
        except BiliError:
            segments = []
    return summary, segments, source


# ------------------------------------------------------------
# UP 主：昵称搜索 / 主页投稿列表
# ------------------------------------------------------------

def _space_mid(text):
    raw = str(text or "").strip()
    m = re.search(r"(?:space|m)\.bilibili\.com/(?:space/)?(\d+)", raw)
    if m:
        return int(m.group(1))
    if re.fullmatch(r"\d{3,}", raw):
        return int(raw)
    return None


def search_user(keyword):
    """按昵称搜 UP 主；返回 [{mid, uname, fans, videos, sign}]。"""
    params = sign({"search_type": "bili_user", "keyword": keyword, "page": 1})
    d = _check(_get(API + "/x/web-interface/wbi/search/type", params, auth=False)) or {}
    out = []
    for x in d.get("result") or []:
        out.append({"mid": x.get("mid"), "uname": str(x.get("uname") or ""),
                    "fans": int(x.get("fans") or 0), "videos": int(x.get("videos") or 0),
                    "sign": str(x.get("usign") or "")})
    return out


def user_card(mid):
    """公开用户名片（不触发空间页风控）：昵称 / 粉丝 / 投稿数。"""
    d = _check(_get(API + "/x/web-interface/card", {"mid": mid}, auth=False)) or {}
    card = d.get("card") or {}
    return {"mid": int(mid), "uname": str(card.get("name") or ""),
            "fans": int(card.get("fans") or 0),
            "videos": int(d.get("archive_count") or card.get("archive_count") or 0)}


def _clean_title(raw):
    """搜索结果标题带 <em class="keyword"> 高亮，去掉标签并反转义。"""
    return html.unescape(re.sub(r"<[^>]+>", "", str(raw or ""))).strip()


def resolve_user(query):
    """昵称 / space 链接 / mid → 用户信息；同名时优先完全匹配。"""
    raw = str(query or "").strip()
    if not raw:
        raise BiliError("缺少 UP 主昵称或主页链接")
    mid = _space_mid(raw)
    if mid:
        try:
            info = user_card(mid)
        except BiliError:
            info = {"mid": mid, "uname": "", "fans": 0, "videos": 0}
        info["candidates"] = []
        return info
    if re.search(r"BV[0-9A-Za-z]{10}", raw):
        raise BiliError("这是视频链接；查 UP 主投稿请给昵称或 space.bilibili.com 主页链接")
    users = search_user(raw)
    if not users:
        raise BiliError("B站搜索接口没有返回结果（可能是临时风控或接口波动），"
                        "这不等于账号不存在；稍后再试，或直接给 space 链接 / UID")
    exact = next((u for u in users if u["uname"].strip().lower() == raw.lower()), None)
    best = dict(exact or users[0])
    best["candidates"] = [] if exact else users[:3]
    return best


def _space_item(x):
    return {"bvid": x.get("bvid") or "", "title": str(x.get("title") or ""),
            "created": x.get("created") or 0, "length": x.get("length") or "",
            "play": int(x.get("play") or 0), "comment": int(x.get("comment") or 0)}


def search_up_videos(mid, uname, pages=3, keyword=""):
    """投稿列表兜底：用搜索接口按昵称找视频，再按 mid 过滤。

    空间页的 arc/search 在部分网络下持续 412，但 search/type 仍可用；
    搜索结果不保证完整，但足以回答「有哪些视频」。
    """
    try:
        n = max(1, min(int(pages or 3), 10))
    except (TypeError, ValueError):
        n = 3
    out, seen = [], set()
    for pn in range(1, n + 1):
        params = sign({"search_type": "video", "keyword": uname, "page": pn})
        d = _check(_get(API + "/x/web-interface/wbi/search/type",
                        params, auth=False)) or {}
        rows = d.get("result") or []
        if not rows:
            break
        for x in rows:
            try:
                if int(x.get("mid") or 0) != int(mid):
                    continue
            except (TypeError, ValueError):
                continue
            bvid = str(x.get("bvid") or "")
            if not bvid or bvid in seen:
                continue
            title = _clean_title(x.get("title"))
            if keyword and keyword.lower() not in title.lower():
                continue
            seen.add(bvid)
            out.append({"bvid": bvid, "title": title,
                        "created": int(x.get("pubdate") or 0),
                        "length": str(x.get("duration") or ""),
                        "play": int(x.get("play") or 0), "comment": 0})
    return out


def list_up_videos(mid, pages=1, order="pubdate", keyword=None, uname="", known_total=0):
    """UP 主投稿列表；order: pubdate（最新）/ click（播放）/ stow（收藏）。

    空间页接口持续 412 时，自动退回搜索接口按昵称找视频再按 mid 过滤。
    """
    try:
        n = max(1, min(int(pages or 1), 5))
    except (TypeError, ValueError):
        n = 1
    order = order if order in ("pubdate", "click", "stow") else "pubdate"
    keyword = str(keyword or "").strip()
    key = (str(mid), n, order, keyword, uname)
    with _UP_LOCK:
        hit = _UP_CACHE.get(key)
        if hit and time.time() - hit[0] < _UP_TTL:
            return hit[1]

    raw_rows, total = [], 0
    source, note = "space", ""
    try:
        for pn in range(1, n + 1):
            params = sign({"mid": mid, "ps": 30, "pn": pn, "order": order,
                           "platform": "web", "web_location": 1550101})
            d = _check(_get(API + "/x/space/wbi/arc/search", params, auth=False)) or {}
            total = (d.get("page") or {}).get("count") or total
            rows = (d.get("list") or {}).get("vlist") or []
            if not rows:
                break
            raw_rows.extend(rows)
            if len(rows) < 30:
                break
        out = [_space_item(x) for x in raw_rows]
    except BiliError as e:
        if not uname:
            raise
        fallback = search_up_videos(mid, uname, pages=min(max(n * 2, 2), 10),
                                    keyword=keyword)
        if not fallback:
            raise BiliError("读取投稿列表失败（%s），搜索兜底也没有取到；"
                            "这不代表该账号没有投稿" % e)
        out = fallback
        total = known_total or len(fallback)
        source = "search"
        note = "完整投稿接口被风控（%s），以下为搜索接口能读到的投稿" % e

    scanned = len(out)
    if keyword:
        out = [x for x in out if keyword.lower() in x["title"].lower()]
    data = {"total": total, "scanned": scanned, "keyword": keyword,
            "order": order, "items": out, "source": source, "note": note}
    with _UP_LOCK:
        if len(_UP_CACHE) > 100:
            for k in list(_UP_CACHE)[:20]:
                _UP_CACHE.pop(k, None)
        _UP_CACHE[key] = (time.time(), data)
    return data


def format_up(user, data):
    name = user.get("uname") or ("mid %s" % user.get("mid"))
    head = ["【UP主】%s（mid=%s）" % (name, user.get("mid")),
            "主页：https://space.bilibili.com/%s" % user.get("mid")]
    if user.get("fans") or user.get("videos"):
        head.append("粉丝 %s ｜ 投稿 %s" % (user.get("fans") or 0, user.get("videos") or 0))
    order_name = {"pubdate": "按发布时间", "click": "按播放量", "stow": "按收藏"}.get(
        data.get("order"), "按发布时间")
    head.append("排序：%s ｜ 共 %s 条 ｜ 本次显示 %d 条"
                % (order_name, data.get("total") or 0, len(data.get("items") or [])))
    if data.get("keyword"):
        head.append("关键词：%s ｜ 匹配 %d 条 / 扫描 %d 条"
                    % (data["keyword"], len(data.get("items") or []),
                       data.get("scanned") or 0))
    if data.get("source") == "search":
        head.append("来源：搜索接口（空间投稿接口被风控）")
    if data.get("note"):
        head.append("说明：%s" % data["note"])
    lines, size = [], len("\n".join(head))
    for it in data.get("items") or []:
        line = "- %s ｜ %s ｜ 播放 %d ｜ %s ｜ %s" % (
            _fmt_date(it.get("created")) or "-", it.get("length") or "-",
            it.get("play") or 0, it.get("bvid") or "-", it.get("title") or "")
        if size + len(line) + 1 > MAX_TEXT:
            lines.append("…（投稿较多，已截断）")
            break
        lines.append(line)
        size += len(line) + 1
    if lines:
        body = lines
    elif user.get("videos"):
        body = ["（本次接口没有返回列表；该账号共有 %s 个投稿，"
                "这不代表没有投稿，稍后再试）" % user["videos"]]
    else:
        body = ["（没有取到投稿）"]
    text = "\n".join(head + body)
    if user.get("candidates"):
        text += "\n\n相近账号：%s。如果找的不是这位，请给完整昵称或主页链接。" % "、".join(
            "%s（mid=%s）" % (u.get("uname"), u.get("mid")) for u in user["candidates"])
    text += "\n（想看某条视频的内容或评论，把它的 BV 号 / 链接发我即可。）"
    return text[:MAX_TEXT]


def _comment_item(it, limit=300):
    content = re.sub(r"\s+", " ", str((it.get("content") or {}).get("message") or "")).strip()
    if not content:
        return None
    name = str((it.get("member") or {}).get("uname") or "").strip() or "匿名"
    return {"like": int(it.get("like") or 0), "user": name, "content": content[:limit],
            "rpid": int(it.get("rpid") or 0), "rcount": int(it.get("rcount") or 0)}


def comments(target, pages=None, sort=None, offset=0, rpid=None):
    """根评论分页；传 rpid 时读该条根评论的楼中楼。"""

    video = get_video(target)
    cfg = load_config()

    if rpid:
        try:
            n = max(1, min(int(pages or 3), 5))
        except (TypeError, ValueError):
            n = 3
        key = ("replies", video.get("aid"), str(rpid), n)
        cached = _cache_get(_COMMENT_CACHE, key, _COMMENT_TTL)
        if cached:
            return cached
        rows = []
        for pn in range(1, n + 1):
            d = _check(_get(API + "/x/v2/reply/reply", {
                "oid": video.get("aid"), "type": 1, "root": rpid,
                "ps": COMMENT_PAGE_SIZE, "pn": pn}, auth=False)) or {}
            part = d.get("replies") or []
            rows.extend(part)
            if len(part) < COMMENT_PAGE_SIZE:
                break
        items = [x for x in (_comment_item(it, 200) for it in rows) if x]
        out = {"kind": "replies", "video": video, "rpid": int(rpid),
               "items": items}
        _cache_set(_COMMENT_CACHE, key, out)
        return out

    try:
        n = int(pages if pages is not None else (cfg.get("bili_comment_pages") or DEFAULT_COMMENT_PAGES))
    except (TypeError, ValueError):
        n = DEFAULT_COMMENT_PAGES
    n = max(1, min(n, MAX_COMMENT_PAGES))
    try:
        s = 1 if sort is None else int(sort)
    except (TypeError, ValueError):
        s = 1
    if s not in (0, 1, 2):
        s = 1

    key = ("roots", video.get("aid"), n, s)
    cached = _cache_get(_COMMENT_CACHE, key, _COMMENT_TTL)
    if cached:
        return cached

    items, total, acount = [], 0, 0
    for pn in range(1, n + 1):
        d = _check(_get(API + "/x/v2/reply", {
            "type": 1, "oid": video.get("aid"), "sort": s,
            "ps": COMMENT_PAGE_SIZE, "pn": pn, "nohot": 1}, auth=False)) or {}
        page = d.get("page") or {}
        total = page.get("count") or total
        acount = page.get("acount") or acount
        rows = d.get("replies") or []
        if not rows:
            break
        items.extend(rows)
        if len(rows) < COMMENT_PAGE_SIZE:
            break

    out_items = [x for x in (_comment_item(it) for it in items) if x]
    data = {"kind": "roots", "video": video, "sort": s, "total": total,
            "acount": acount, "items": out_items}
    _cache_set(_COMMENT_CACHE, key, data)
    return data


# ------------------------------------------------------------
# 弹幕：全量 XML → 聚合 / 时间段原文
# ------------------------------------------------------------

def _danmaku_xml(cid):
    key = ("dm", str(cid))
    cached = _cache_get(_DM_CACHE, key, _DM_TTL)
    if cached is not None:
        return cached
    raw = _get_raw(API + "/x/v1/dm/list.so", {"oid": cid}, auth=False)
    if not raw.startswith(b"<"):
        try:
            raw = zlib.decompress(raw, 47)
        except zlib.error:
            pass
    _cache_set(_DM_CACHE, key, raw)
    return raw


def _parse_danmaku(raw):
    """解析 XML 弹幕；返回 [{t: 秒, text}]。"""
    out = []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return out
    for d in root.findall("d"):
        p = (d.get("p") or "").split(",")
        if not p:
            continue
        try:
            t = float(p[0])
        except (TypeError, ValueError):
            continue
        text = re.sub(r"\s+", " ", d.text or "").strip()
        if text:
            out.append({"t": t, "text": text[:120]})
    return out


def _num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def danmaku(target, mode="digest", start=None, end=None, limit=200):
    """弹幕聚合（默认）或指定时间段的原文。"""
    video = get_video(target)
    items = _parse_danmaku(_danmaku_xml(video.get("cid")))
    if not items:
        return "【弹幕】%s\n该视频没有可解析的弹幕（可能已关闭或未开放）。" % video.get("title")

    if str(mode or "").lower() == "raw":
        lo, hi = _num(start, 0.0), _num(end, 1e9)
        sel = [x for x in items if lo <= x["t"] <= hi]
        try:
            n = max(1, min(int(limit or 200), 500))
        except (TypeError, ValueError):
            n = 200
        lines = ["[%s] %s" % (_fmt_duration(x["t"]), x["text"]) for x in sel[:n]]
        head = ("【弹幕原文】%s\n区间：%s–%s ｜ 命中 %d 条 ｜ 显示 %d 条"
                % (video.get("title"), _fmt_duration(lo),
                   _fmt_duration(hi) if hi < 1e8 else "结尾", len(sel), len(lines)))
        return (head + "\n" + "\n".join(lines))[:MAX_TEXT]

    buckets = Counter(int(x["t"] // 60) for x in items)
    by_min = {}
    for x in items:
        by_min.setdefault(int(x["t"] // 60), []).append(x)
    top = buckets.most_common(12)
    freq = Counter(x["text"] for x in items if len(x["text"]) >= 2)
    repeats = [(k, c) for k, c in freq.most_common(20) if c >= 3][:12]

    lines = ["【弹幕】%s" % video.get("title"),
             "可解析弹幕：%d 条 ｜ 视频总弹幕：%s 条 ｜ 时长：%s ｜ 覆盖：%d 分钟"
             % (len(items), (video.get("stat") or {}).get("danmaku") or "-",
                _fmt_duration(video.get("duration")), len(buckets)),
             "密度峰值（每分钟条数）："]
    for m, count in top:
        samples = []
        for x in by_min.get(m, []):
            if len(x["text"]) >= 3 and x["text"] not in samples:
                samples.append(x["text"])
            if len(samples) >= 4:
                break
        lines.append("- %s–%s ｜ %d 条 ｜ 样本：%s" % (
            _fmt_duration(m * 60), _fmt_duration((m + 1) * 60), count,
            " / ".join(samples) or "（无）"))
    if repeats:
        lines.append("高频重复：")
        for k, c in repeats:
            lines.append("- 「%s」× %d" % (k[:30], c))
    lines.append("（要看某段原始弹幕：mode=raw&start=秒&end=秒）")
    return "\n".join(lines)[:MAX_TEXT]


# ============================================================
# 格式化（给模型看的纯文本）
# ============================================================

def _fmt_duration(sec):
    sec = int(sec or 0)
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return ("%d:%02d:%02d" % (h, m, s)) if h else ("%d:%02d" % (m, s))


def _fmt_date(ts):
    try:
        return time.strftime("%Y-%m-%d", time.localtime(int(ts)))
    except (TypeError, ValueError, OSError):
        return ""


def format_info(v):
    stat = v.get("stat") or {}
    meta = "UP：%s" % (v.get("owner") or "-")
    if v.get("tname"):
        meta += " ｜ 分区：%s" % v.get("tname")
    meta += " ｜ 时长：%s ｜ 发布：%s" % (
        _fmt_duration(v.get("duration")), _fmt_date(v.get("pubdate")) or "-")
    lines = [
        "【视频】%s" % v.get("title"),
        meta,
        "数据：播放 %s ｜ 点赞 %s ｜ 评论 %s ｜ 弹幕 %s" % (
            stat.get("view") or 0, stat.get("like") or 0,
            stat.get("reply") or 0, stat.get("danmaku") or 0),
        "链接：https://www.bilibili.com/video/%s" % v.get("bvid"),
    ]
    if v.get("page_title") and len(v.get("pages") or []) > 1:
        lines.append("当前分P：P%d %s" % (v.get("page"), v.get("page_title")))
    pages = v.get("pages") or []
    if len(pages) > 1:
        show = pages[:10]
        lines.append("分P：共 %d P ｜ %s%s" % (
            len(pages),
            "；".join("P%d %s（%s）" % (p.get("page"), p.get("part"),
                                      _fmt_duration(p.get("duration"))) for p in show),
            "；…" if len(pages) > len(show) else ""))
    desc = re.sub(r"\s+", " ", str(v.get("desc") or "")).strip()
    if desc:
        lines.append("简介：%s" % desc[:600])
    if v.get("collection"):
        lines.append("合集：%s" % v["collection"])
    if v.get("tags"):
        lines.append("标签：%s" % "、".join(v["tags"][:15]))
    related = v.get("related") or []
    if related:
        lines.append("相关视频：")
        for r in related[:6]:
            lines.append("- %s ｜ %s ｜ %s" % (
                r.get("bvid") or "-", (r.get("title") or "")[:40],
                r.get("owner") or "-"))
    lines.append("字幕：%s" % ("已配置 SESSDATA，可尝试读取" if _sessdata()
                              else "未配置 SESSDATA（只能读信息和评论）"))
    return "\n".join(lines)


def format_summary(summary):
    lines = []
    if summary.get("summary"):
        lines.append("一句话总结：%s" % summary["summary"])
    if summary.get("outline"):
        lines.append("分段提纲：")
        for o in summary["outline"]:
            lines.append("- [%s] %s" % (_fmt_duration(o.get("timestamp")), o.get("title") or ""))
            for p in o.get("points") or []:
                lines.append("  - [%s] %s" % (_fmt_duration(p.get("timestamp")),
                                              p.get("content") or ""))
    return "\n".join(lines)


def format_transcript(video, segments, source, offset=0):
    lines = ["[%s] %s" % (_fmt_duration(s.get("start")), s.get("content")) for s in segments]
    full = "\n".join(lines)
    total = len(full)
    off = max(0, int(offset or 0))
    chunk = full[off:off + MAX_TEXT]
    head = "【逐字稿】%s\n来源：%s ｜ 全文字数：%d" % (video.get("title"), source, total)
    if off:
        head += " ｜ 续取 offset=%d" % off
    tail = ""
    if off + len(chunk) < total:
        tail = "\n…（未取完，下一段传 offset=%d）" % (off + len(chunk))
    return head + "\n" + chunk + tail


def format_comments(data, offset=0):
    v = data["video"]
    lines = []
    if data.get("kind") == "replies":
        head = "【楼中楼】%s\n根评论 rpid=%s ｜ 本次取 %d 条" % (
            v.get("title"), data.get("rpid"), len(data.get("items") or []))
        for it in data.get("items") or []:
            lines.append("↳ 👍%d %s：%s" % (it["like"], it["user"], it["content"]))
    else:
        sort_name = {0: "按时间", 1: "按点赞", 2: "按回复"}.get(data.get("sort"), "按点赞")
        head = ("【评论】%s\n排序：%s ｜ 根评论 %s 条 ｜ 总回复 %s 条 ｜ 本次取 %d 条\n"
                "（每条带 rpid；要读楼中楼，把 rpid 传给 bili.comments）"
                % (v.get("title"), sort_name, data.get("total") or 0,
                   data.get("acount") or 0, len(data.get("items") or [])))
        for it in data.get("items") or []:
            lines.append("- [rpid=%s] 👍%d 回复%d %s：%s" % (
                it.get("rpid"), it["like"], it.get("rcount") or 0,
                it["user"], it["content"]))
    full = head + "\n" + ("\n".join(lines) if lines else "（没有可显示的评论）")
    total = len(full)
    off = max(0, int(offset or 0))
    chunk = full[off:off + MAX_TEXT]
    tail = ""
    if off + len(chunk) < total:
        tail = "\n…（未取完，下一段传 offset=%d；需要更多根评论可增大 pages，最多 10）" % (
            off + len(chunk))
    return chunk + tail


def analyze(target):
    """视频内容分析：信息 + B 站官方 AI 总结/提纲 + 逐字稿节选。"""
    video = video_detail(target)
    summary, segments, source = _transcript_segments(video)
    parts = [format_info(video)]
    if summary and (summary.get("summary") or summary.get("outline")):
        parts.append("【B站官方 AI 总结】\n" + format_summary(summary))
    if segments:
        parts.append(format_transcript(video, segments, source)[:4000])
    elif not (summary and (summary.get("summary") or summary.get("outline"))):
        parts.append("（该视频没有可用的官方总结或字幕；当前版本不下载音频做本地转写）")
    return "\n\n".join(parts)[:MAX_TEXT]


def timeline(target):
    """把章节 + 字幕 + 弹幕峰值对齐成一条可读时间轴。"""
    video = video_detail(target)
    key = ("timeline", video.get("bvid"))
    cached = _cache_get(_TIMELINE_CACHE, key, _TIMELINE_TTL)
    if cached:
        return cached

    chapters = []
    try:
        for x in player_info(video).get("view_points") or []:
            chapters.append({"start": _num(x.get("from")), "end": _num(x.get("to")),
                             "title": str(x.get("content") or "")})
    except BiliError:
        pass

    summary = None
    if not chapters:
        try:
            summary = ai_summary(video)
            outline = summary.get("outline") or []
            for i, o in enumerate(outline):
                end = outline[i + 1]["timestamp"] if i + 1 < len(outline) else (
                    video.get("duration") or 0)
                chapters.append({"start": _num(o.get("timestamp")), "end": _num(end),
                                 "title": str(o.get("title") or "")})
        except BiliError:
            pass

    _, segments, source = _transcript_segments(video)
    try:
        dm_items = _parse_danmaku(_danmaku_xml(video.get("cid")))
    except BiliError:
        dm_items = []
    by_min = {}
    for x in dm_items:
        by_min.setdefault(int(x["t"] // 60), []).append(x)

    if not chapters:
        step, total = 120, int(video.get("duration") or 0)
        if total <= 0:
            chapters = [{"start": 0, "end": 0, "title": ""}]
        else:
            for s in range(0, total, step):
                chapters.append({"start": s, "end": min(total, s + step), "title": ""})

    lines = ["【时间轴】%s" % video.get("title"),
             "章节：%d 段 ｜ 字幕：%s ｜ 可解析弹幕：%d 条"
             % (len(chapters), source if segments else "无", len(dm_items))]
    for ch in chapters[:40]:
        s, e = _num(ch.get("start")), _num(ch.get("end"))
        hi = max(e, s + 1)
        lines.append("")
        lines.append("【%s–%s】%s" % (_fmt_duration(s), _fmt_duration(e),
                                     ch.get("title") or "（无标题）"))
        for x in [y for y in segments if s <= y.get("start", 0) < hi][:2]:
            lines.append("  字幕：[%s] %s" % (_fmt_duration(x.get("start")),
                                              str(x.get("content") or "")[:120]))
        if dm_items:
            count, samples = 0, []
            for m in range(int(s // 60), int(max(hi - 1, s) // 60) + 1):
                for x in by_min.get(m, []):
                    if s <= x["t"] < hi:
                        count += 1
                        if (len(x["text"]) >= 3 and x["text"] not in samples
                                and len(samples) < 3):
                            samples.append(x["text"])
            if count:
                lines.append("  弹幕：%d 条 ｜ 样本：%s" % (count, " / ".join(samples) or "（无）"))
        if len("\n".join(lines)) > MAX_TEXT:
            lines.append("…（已截断）")
            break
    text = "\n".join(lines)[:MAX_TEXT]
    _cache_set(_TIMELINE_CACHE, key, text)
    return text


# ============================================================
# 能力：主对话 / 任务引擎共用
# ============================================================

def _target(args):
    return (args.get("url") or args.get("bvid") or args.get("aid") or "").strip()


def _cap_info(args, ctx):
    try:
        return format_info(video_detail(_target(args)))
    except BiliError as e:
        return "❌ " + str(e)


def _cap_analyze(args, ctx):
    try:
        return analyze(_target(args))
    except BiliError as e:
        return "❌ " + str(e)


def _cap_transcript(args, ctx):
    try:
        video = get_video(_target(args), page=args.get("page"))
        summary, segments, source = _transcript_segments(video)
        if segments:
            return format_transcript(video, segments, source, args.get("offset") or 0)
        if summary and (summary.get("summary") or summary.get("outline")):
            text = "【B站官方 AI 总结】%s\n%s\n（该视频没有可读字幕，以上为官方总结）" % (
                video.get("title"), format_summary(summary))
            return text[:MAX_TEXT]
        raise BiliError("该视频没有可用字幕或 AI 总结；当前版本不下载音频做本地转写")
    except BiliError as e:
        return "❌ " + str(e)


def _cap_comments(args, ctx):
    try:
        return format_comments(comments(_target(args),
                                        pages=args.get("pages"),
                                        sort=args.get("sort"),
                                        rpid=args.get("rpid")),
                               args.get("offset") or 0)
    except BiliError as e:
        return "❌ " + str(e)


def _cap_danmaku(args, ctx):
    try:
        return danmaku(_target(args),
                       mode=str(args.get("mode") or "digest"),
                       start=args.get("start"), end=args.get("end"),
                       limit=args.get("limit"))
    except BiliError as e:
        return "❌ " + str(e)


def _cap_timeline(args, ctx):
    try:
        return timeline(_target(args))
    except BiliError as e:
        return "❌ " + str(e)


def _cap_up(args, ctx):
    try:
        query = (args.get("name") or args.get("url") or args.get("mid") or "").strip()
        user = resolve_user(query)
        data = list_up_videos(user.get("mid"), pages=args.get("pages"),
                              order=str(args.get("order") or "pubdate"),
                              keyword=str(args.get("keyword") or "").strip(),
                              uname=user.get("uname") or "",
                              known_total=user.get("videos") or 0)
        return format_up(user, data)
    except BiliError as e:
        return "❌ " + str(e)


def login_status(sessdata=None):
    """设置页「测试登录」：只返回是否登录与昵称，不回传 Cookie。"""
    raw = _get(API + "/x/web-interface/nav", sessdata=sessdata)
    d = raw.get("data") or {}
    return {"ok": True, "logged_in": bool(d.get("isLogin")),
            "uname": str(d.get("uname") or ""),
            "has_cookie": bool(str(sessdata if sessdata is not None else _sessdata()).strip())}


capabilities.capability(
    "bili.info", "读取 B 站视频信息（标题 / UP / 简介 / 分P / 标签 / 相关视频 / 合集）",
    {"url": "str"}, permission="read")(_cap_info)
capabilities.capability(
    "bili.analyze", "分析 B 站视频内容：信息 + B站官方 AI 总结/提纲 + 逐字稿节选",
    {"url": "str"}, permission="read")(_cap_analyze)
capabilities.capability(
    "bili.transcript", "读取 B 站视频逐字稿（B站 AI 字幕 / 作者字幕，带时间戳；长视频按返回的 offset 续取）",
    {"url": "str", "page": "int?", "offset": "int?"}, permission="read")(_cap_transcript)
capabilities.capability(
    "bili.comments", "读取 B 站评论：根评论分页/offset 续取；传 rpid 时读楼中楼",
    {"url": "str", "pages": "int?", "sort": "int?", "offset": "int?", "rpid": "int?"},
    permission="read")(_cap_comments)
capabilities.capability(
    "bili.danmaku", "读取 B 站弹幕：默认按分钟聚合找密度峰值；mode=raw 可取指定时间段原文",
    {"url": "str", "mode": "str?", "start": "num?", "end": "num?", "limit": "int?"},
    permission="read")(_cap_danmaku)
capabilities.capability(
    "bili.timeline", "把 B 站视频的章节 / 字幕 / 弹幕峰值对齐成时间轴",
    {"url": "str"}, permission="read")(_cap_timeline)
capabilities.capability(
    "bili.up", "查询 B 站 UP 主与投稿（昵称 / space.bilibili.com 主页 / mid；可用 keyword 过滤标题）",
    {"name": "str", "pages": "int?", "order": "str?", "keyword": "str?"},
    permission="read")(_cap_up)
