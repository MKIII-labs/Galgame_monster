# -*- coding: utf-8 -*-
"""功能大厅：AI 互动功能的公共后端支撑。

「功能大厅」是各 AI 互动玩法的统一入口（卡片清单在前端注册表 web/hall.js，
单一来源，不在这里再维护一份）。本模块只放**功能无关**的三类设施，
新功能可以直接复用，不必从零写起：

  1. 地理计算   haversine_km / clamp_latlng      —— 纯函数，可单测
  2. 题面数据源 fetch_photo / normalize_photo    —— 只负责「取一道题」
  3. 对局仓库   new_round / put_guess / reveal   —— 线程安全的进程内状态

「图寻游戏」自身的规则（谁离得近谁赢）落在 score_round；
AI 参与者的定位能力落在 ai_locate / ai_context —— 两者都与对局仓库解耦。

数据来源：Panoramax（开放街景图库，免密钥、免注册）。它的 /api/search 按
bbox 返回真实照片与**拍摄点真实经纬度**，正好满足「不在本地维护图片与坐标数据集」。
"""
import base64
import json
import math
import random
import re
import threading
import time
import uuid

import requests

from app import imaging
from app.config import get_api_conf, use_chat_for_vision

# ---------------- 1. 地理计算（纯函数） ----------------

EARTH_R_KM = 6371.0088          # IUGG 平均地球半径


def haversine_km(lat1, lng1, lat2, lng2):
    """两点大圆距离（公里）。

    这里必须用球面公式而不是平面近似：图寻的标注经常跨洲，
    经度方向的平面换算会随纬度剧烈失真（在 60°N 误差可达一倍以上）。
    """
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R_KM * math.asin(min(1.0, math.sqrt(a)))


def clamp_latlng(lat, lng):
    """校验并归一化经纬度，返回 (lat, lng)；任一非法则返回 (None, None)。

    经度允许绕圈（Leaflet 在跨 180° 平移时会给出 ±180 之外的值），
    纬度越界一律视为非法 —— 那一定是解析或点击出了问题，不该被悄悄修正。
    """
    try:
        la, ln = float(lat), float(lng)
    except (TypeError, ValueError):
        return None, None
    if not (math.isfinite(la) and math.isfinite(ln)):
        return None, None
    if not -90.0 <= la <= 90.0:
        return None, None
    if ln < -180.0 or ln > 180.0:           # 只在越界时才绕圈，避免正常值被取模掉精度
        ln = (ln + 180.0) % 360.0 - 180.0
    return la, ln


# ---------------- 2. 题面数据源（Panoramax） ----------------

PANORAMAX_SEARCH = "https://api.panoramax.xyz/api/search"
_USER_AGENT = "Galgame_monster/1.0 (local geo game)"
_IMAGE_LIMIT = 12 * 1024 * 1024         # 单张图下载上限，防止把巨型全景塞进模型请求

# 题面区域池：城市量级的 bbox，已逐个实测有照片（见 docs/NOTES.md）。
# 用 bbox 而不是全网随机点，是因为 Panoramax 覆盖并不均匀 —— 随机撒点会大量落到空区域，
# 每题都要多花几次请求才发现「这儿没图」。宁可「题库以欧洲为主」，也不要每局空转。
GEO_REGIONS = [
    {"name": "巴黎", "bbox": [2.25, 48.80, 2.45, 48.92]},
    {"name": "里昂", "bbox": [4.78, 45.70, 4.92, 45.80]},
    {"name": "马赛", "bbox": [5.30, 43.25, 5.42, 43.33]},
    {"name": "图卢兹", "bbox": [1.40, 43.56, 1.49, 43.64]},
    {"name": "波尔多", "bbox": [-0.62, 44.80, -0.53, 44.87]},
    {"name": "里尔", "bbox": [3.02, 50.60, 3.11, 50.66]},
    {"name": "斯特拉斯堡", "bbox": [7.70, 48.55, 7.79, 48.61]},
    {"name": "南特", "bbox": [-1.60, 47.19, -1.51, 47.25]},
    {"name": "阿尔萨斯", "bbox": [7.30, 48.30, 7.60, 48.60]},
    {"name": "布鲁塞尔", "bbox": [4.32, 50.82, 4.42, 50.88]},
    {"name": "阿姆斯特丹", "bbox": [4.85, 52.34, 4.95, 52.40]},
    {"name": "乌得勒支", "bbox": [5.08, 52.07, 5.14, 52.11]},
    {"name": "米兰", "bbox": [9.14, 45.44, 9.24, 45.50]},
    {"name": "都灵", "bbox": [7.63, 45.03, 7.72, 45.10]},
    {"name": "罗马", "bbox": [12.44, 41.86, 12.54, 41.93]},
    {"name": "那不勒斯", "bbox": [14.24, 40.83, 14.29, 40.87]},
    {"name": "巴塞罗那", "bbox": [2.12, 41.36, 2.21, 41.43]},
    {"name": "马德里", "bbox": [-3.74, 40.39, -3.65, 40.45]},
    {"name": "里斯本", "bbox": [-9.18, 38.70, -9.10, 38.75]},
    {"name": "波尔图", "bbox": [-8.63, 41.14, -8.58, 41.18]},
    {"name": "塞维利亚", "bbox": [-6.00, 37.38, -5.95, 37.41]},
    {"name": "维也纳", "bbox": [16.32, 48.17, 16.42, 48.24]},
    {"name": "苏黎世", "bbox": [8.50, 47.35, 8.58, 47.40]},
    {"name": "柏林", "bbox": [13.35, 52.48, 13.48, 52.55]},
    {"name": "哥本哈根", "bbox": [12.53, 55.65, 12.62, 55.70]},
    {"name": "斯德哥尔摩", "bbox": [18.02, 59.30, 18.12, 59.35]},
    {"name": "奥斯陆", "bbox": [10.72, 59.90, 10.78, 59.94]},
    {"name": "赫尔辛基", "bbox": [24.92, 60.16, 24.98, 60.19]},
    {"name": "都柏林", "bbox": [-6.30, 53.32, -6.22, 53.36]},
    {"name": "爱尔兰西部", "bbox": [-9.60, 53.20, -9.00, 53.40]},
    {"name": "布拉格", "bbox": [14.38, 50.05, 14.48, 50.11]},
    {"name": "布达佩斯", "bbox": [19.02, 47.47, 19.10, 47.53]},
    {"name": "华沙", "bbox": [21.00, 52.22, 21.05, 52.26]},
    {"name": "卢森堡", "bbox": [6.10, 49.59, 6.16, 49.63]},
    {"name": "达喀尔", "bbox": [-17.48, 14.66, -17.40, 14.74], "continent": "非洲"},
    {"name": "塔那那利佛", "bbox": [47.48, -18.94, 47.56, -18.87], "continent": "非洲"},
    {"name": "墨西哥城", "bbox": [-99.16, 19.40, -99.10, 19.45], "continent": "北美洲"},
    {"name": "哥斯达黎加", "bbox": [-84.10, 9.92, -84.05, 9.96], "continent": "北美洲"},
    # 非欧洲区域：2026-10-20 用 Panoramax /api/search 实测有照片
    # （limit=5 时返回 ≥4 条；只剩 1 条的区域没放进来，避免长期重复同一张图）
    {"name": "布宜诺斯艾利斯", "bbox": [-58.45, -34.65, -58.35, -34.55], "continent": "南美洲"},
    {"name": "圣地亚哥", "bbox": [-70.68, -33.47, -70.60, -33.41], "continent": "南美洲"},
    {"name": "里约热内卢", "bbox": [-43.22, -22.93, -43.14, -22.87], "continent": "南美洲"},
    {"name": "波哥大", "bbox": [-74.10, 4.62, -74.03, 4.68], "continent": "南美洲"},
    {"name": "基多", "bbox": [-78.53, -0.23, -78.47, -0.17], "continent": "南美洲"},
    {"name": "内罗毕", "bbox": [36.79, -1.31, 36.85, -1.25], "continent": "非洲"},
    {"name": "突尼斯", "bbox": [10.15, 36.78, 10.22, 36.83], "continent": "非洲"},
    {"name": "东京", "bbox": [139.68, 35.65, 139.78, 35.72], "continent": "亚洲"},
    {"name": "大阪", "bbox": [135.47, 34.65, 135.53, 34.71], "continent": "亚洲"},
    {"name": "首尔", "bbox": [126.95, 37.53, 127.02, 37.58], "continent": "亚洲"},
    {"name": "曼谷", "bbox": [100.47, 13.72, 100.55, 13.77], "continent": "亚洲"},
    {"name": "马尼拉", "bbox": [120.96, 14.57, 121.02, 14.62], "continent": "亚洲"},
    {"name": "伊斯坦布尔", "bbox": [28.94, 41.00, 29.02, 41.06], "continent": "亚洲"},
    {"name": "悉尼", "bbox": [151.18, -33.89, 151.24, -33.84], "continent": "大洋洲"},
    {"name": "奥克兰", "bbox": [174.74, -36.86, 174.80, -36.83], "continent": "大洋洲"},
    {"name": "纽约", "bbox": [-74.02, 40.70, -73.95, 40.76], "continent": "北美洲"},
    {"name": "旧金山", "bbox": [-122.45, 37.76, -122.39, 37.80], "continent": "北美洲"},
    {"name": "多伦多", "bbox": [-79.40, 43.64, -79.34, 43.68], "continent": "北美洲"},
    {"name": "温哥华", "bbox": [-123.14, 49.26, -123.08, 49.30], "continent": "北美洲"},
]
for _region in GEO_REGIONS:
    _region.setdefault("continent", "欧洲")


def _sample_regions(tries):
    """默认抽题时约一半探测名额给非欧洲，避免总被法国 / 西欧刷屏。"""
    europe = [r for r in GEO_REGIONS if r.get("continent") == "欧洲"]
    others = [r for r in GEO_REGIONS if r.get("continent") != "欧洲"]
    random.shuffle(europe)
    random.shuffle(others)
    n_other = max(1, tries // 2)
    picked = others[:n_other] + europe[:max(0, tries - n_other)]
    if len(picked) < tries:
        rest = [r for r in europe + others if r not in picked]
        random.shuffle(rest)
        picked += rest[:tries - len(picked)]
    random.shuffle(picked)
    return picked


def _is_equirectangular(props):
    """360° 全景照片判据：传感器阵列宽高比接近 2:1。

    这类图直接按平面展示会严重拉伸变形，作为题面不合理，直接判为不合格。
    """
    ori = (props or {}).get("pers:interior_orientation") or {}
    dim = ori.get("sensor_array_dimensions") or []
    try:
        w, h = float(dim[0]), float(dim[1])
    except (IndexError, TypeError, ValueError):
        return False
    return h > 0 and (w / h) > 1.9


def normalize_photo(feat):
    """把 Panoramax 的一条 GeoJSON feature 归一化成一道题的题面。

    不合格（非点几何 / 坐标非法 / 缺图 / 全景变形）一律返回 None：
    宁可换一张，也不要发一道连答案都不成立的题出去。
    """
    if not isinstance(feat, dict):
        return None
    geo = feat.get("geometry") or {}
    coords = geo.get("coordinates") or []
    if geo.get("type") != "Point" or len(coords) < 2:
        return None
    lat, lng = clamp_latlng(coords[1], coords[0])
    if lat is None:
        return None
    props = feat.get("properties") or {}
    if _is_equirectangular(props):
        return None
    assets = feat.get("assets") or {}
    img = ((assets.get("sd") or {}).get("href")
           or (assets.get("hd") or {}).get("href") or "")
    thumb = (assets.get("thumb") or {}).get("href") or img
    if not str(img).startswith("http"):
        return None
    producer = str(props.get("geovisio:producer") or "开放街景")
    license_ = props.get("license")
    return {
        "photo_id": str(feat.get("id") or ""),
        "image_url": str(img),
        "thumb_url": str(thumb),
        "lat": lat,
        "lng": lng,
        "license": license_ if isinstance(license_, str) else "",
        "taken_at": str(props.get("datetime") or ""),
        "credit": "Panoramax · " + producer,
    }


def fetch_photo(timeout=15, tries=6, regions=None):
    """随机取一道合格题面。

    从区域池里随机挑若干个逐个请求，返回第一个有合格照片的。
    全部落空才抛 RuntimeError —— 让上层给出明确提示，
    而不是发一道没有真值的题出去（那样结算必然是错的）。
    """
    pool = _sample_regions(tries) if regions is None else list(regions)
    if regions is not None:
        random.shuffle(pool)
    last_err = ""
    for region in pool[:max(1, tries)]:
        try:
            resp = requests.get(
                PANORAMAX_SEARCH,
                params={"bbox": ",".join(str(v) for v in region["bbox"]), "limit": 60},
                headers={"User-Agent": _USER_AGENT},
                timeout=timeout)
            resp.raise_for_status()
            feats = (resp.json() or {}).get("features") or []
        except Exception as e:                       # noqa: BLE001 - 换下一个区域继续试
            last_err = "%s: %s" % (type(e).__name__, e)
            continue
        cands = [p for p in (normalize_photo(f) for f in feats) if p]
        if cands:
            return random.choice(cands)
    raise RuntimeError("取题失败（%s）" % (last_err or "所选区域都没有可用照片"))


# ---------------- 3. 参与者 ----------------

HUMAN_ID = "you"

# AI 参与者的差异不在「算力」，在**看图的切入点**：同一张照片，
# 按不同线索入手会给出不同的坐标，这比给同一份提示词再随机加噪声更自然。
AI_PLAYERS = [
    {"id": "ai_scout", "name": "背包客阿岚",
     "desc": "从植被、土壤和地形切入，先判断气候带与纬度，再落到具体国家。",
     "style": "先看植被与地貌（植被带、土壤颜色、山形、水体），据此判断气候带与大致纬度"},
    {"id": "ai_arch", "name": "建筑迷小柯",
     "desc": "盯建筑风格、招牌文字与道路标线，用人文细节锁定国家与城市。",
     "style": "先看建筑风格、路牌与招牌上的文字和字体、路面标线与交通设施，据此锁定国家与城市"},
    {"id": "ai_geo", "name": "地理老师老周",
     "desc": "用太阳方位、影子、左右舵和市政设施做硬性排除，风格最像教科书。",
     "style": "先看太阳高度与影子方向、车辆行驶方向（左舵还是右舵）、电线杆与市政设施规格，据此推断地区"},
    {"id": "ai_yuke", "name": "宇科君", "tag": "B站", "prompt": "yuke",
     "desc": "按车牌→道路→太阳→植被→建筑→选点的固定顺序，先列候选国再逐个排除。",
     "style": "按车牌、道路方向、太阳方位、植被、建筑设施、选点的固定顺序逐步排除"},
]
MAX_AI_PLAYERS = len(AI_PLAYERS)


def ai_roster(count):
    """取前 count 个 AI 参与者（含 kind 标记），越界自动夹紧。"""
    try:
        n = int(count)
    except (TypeError, ValueError):
        n = 0
    n = max(0, min(n, MAX_AI_PLAYERS))
    return [dict(p, kind="ai") for p in AI_PLAYERS[:n]]


def ai_roster_off(off_ids):
    """按「关闭名单」组建 AI：列表里的选手不参战，其余全部上场。

    与 ai_roster(count) 的区别：这里是**按人**开关，不再受人数上限影响，
    传空列表等于四位全上。
    """
    off = {str(x) for x in (off_ids or [])}
    return [dict(p, kind="ai") for p in AI_PLAYERS if p["id"] not in off]


def ai_catalog():
    """给前端的完整 AI 名册（含风格介绍），不随本局开关变化。"""
    out = []
    for p in AI_PLAYERS:
        item = {"id": p["id"], "name": p["name"], "desc": p.get("desc", "")}
        if p.get("tag"):
            item["tag"] = p["tag"]
        out.append(item)
    return out


def _locate_order():
    """AI 看图的通道尝试顺序。

    跟随设置里的「聊天模型自带视觉能力」开关，与「上传图片识别」用的是同一条通道 ——
    否则会出现「同一张图，识图功能走 chat、图寻 AI 走 vision」这种自相矛盾的行为。
    """
    if use_chat_for_vision():
        return (("chat", "chat"), ("vision", "vision"))
    return (("vision", "vision"), ("chat", "chat"))


def ai_capability():
    """AI 当前「能不能看图」，供前端给提示：vision | chat | none。

    只反映密钥配置与通道顺序，不代表这次一定能成功 ——
    真实情况以每个 AI 落点上标注的 mode 为准（碰壁会自动退到盲猜）。
    """
    for provider, tag in _locate_order():
        if (get_api_conf(provider)[0] or "").strip():
            return tag
    return "none"


# ---------------- 4. 结算规则 ----------------

def score_round(guesses, truth):
    """按「与真实拍摄点的直线距离」排名。

    guesses: [{"player_id": ..., "lat": ..., "lng": ...}]
    truth:   (lat, lng)
    返回 [{player_id, distance_km, rank}]，按距离升序；同距离并列同名次（下一名跳号）。

    距离先四舍五入到 0.01 km 再比较并列，否则浮点尾差会让「看起来一样近」的两个人分出先后。
    """
    tlat, tlng = truth
    rows = []
    for g in guesses or []:
        la, ln = clamp_latlng(g.get("lat"), g.get("lng"))
        if la is None:
            continue
        rows.append({"player_id": g.get("player_id"),
                     "distance_km": round(haversine_km(la, ln, tlat, tlng), 2)})
    rows.sort(key=lambda r: r["distance_km"])
    for i, row in enumerate(rows):
        if i and row["distance_km"] == rows[i - 1]["distance_km"]:
            row["rank"] = rows[i - 1]["rank"]
        else:
            row["rank"] = i + 1
    return rows


# ---------------- 5. AI 定位（视觉优先，对话模型兜底） ----------------

GEO_AI_ASK = (
    "这是一张真实拍摄的户外照片，请你判断它的拍摄地点。\n"
    "你的分析角度：{style}\n"
    "请按这个顺序观察：文字与招牌（语言 / 字体）→ 建筑与道路 → 植被与地貌 → "
    "车辆 / 车牌 → 日照与影子方向。\n"
    "只输出一个 JSON 对象，不要任何多余文字："
    '{{"lat": 纬度数字, "lng": 经度数字, "confidence": 0.0到1.0的小数, '
    '"clue": "一句话结论", "reason": ["依据1：画面里看到了什么，说明什么", '
    '"依据2：……", "依据3：为什么排除其他地区"]}}\n'
    "reason 写 2–4 条，每条一句话，必须具体到画面里真实看到的细节，"
    "不要写「可能 / 大概」这类空话；看不到图或无法判断时写"
    "「没有可用的画面线索」。"
)

GEO_AI_ASK_YUKE = (
    "这是一张真实拍摄的户外照片，你要像图寻高手一样先缩小国家范围，再给坐标。\n"
    "固定顺序：车牌（颜色 / 前后）→ 道路（中心线颜色、路标底色、左行 / 右行）→ "
    "太阳方位 → 植被 → 建筑 / 设施 → 选点。\n"
    "先列出 2–4 个候选国，再用能排除其他候选的硬特征逐个排除；"
    "不要把草垛、沙漠化这类软特征当成排他证据。\n"
    "只输出一个 JSON 对象，不要多余文字："
    '{{"lat": 纬度数字, "lng": 经度数字, "confidence": 0.0到1.0的小数, '
    '"clue": "一句话结论", "candidates": ["候选国"], '
    '"decided_by": "起决定作用的硬特征", '
    '"reason": ["看到什么 → 排除/锁定谁", "……"]}}\n'
    "reason 写 3–4 条，每条一句话，按「观察 → 排除/锁定」的顺序；"
    "不要复述整段推理，不要写「可能 / 大概」这类空话。\n"
    "看不到图或无法判断时写「没有可用的画面线索」。"
)

_NO_KEY_MARKS = ("⚠️", "❌")            # imaging 的失败/未配置前缀

_DIR_AXIS = {"N": ("lat", 1), "S": ("lat", -1), "E": ("lng", 1), "W": ("lng", -1)}

# 后缀方位（数字在前）：48.85N / 48°51'30"N。**要求数字与方位字母紧邻**，
# 不能允许中间有空格 —— 否则 "35.68  E 139.76" 这种前缀写法会被错读成两次度数。
_SUFFIX_RE = re.compile(
    r"(\d{1,3}(?:\.\d+)?)"
    r"(?:[°度](?:(\d{1,2}(?:\.\d+)?)['′分])?(?:(\d{1,2}(?:\.\d+)?)[\"″秒])?)?"
    r"([NSEWnsew])(?![A-Za-z])")
# 前缀方位（字母在前）：N48.85 / 东经 139.76（中文方位词先被 _norm_dirs 换成字母）。
# 前置断言 (?<![A-Za-z]) 是必须的：否则英文句子里的 "like 48.85" 会被当成 E 轴，整条读反。
_PREFIX_RE = re.compile(r"(?<![A-Za-z])([NSEWnsew])\s*(\d{1,3}(?:\.\d+)?)")

_LAT_LABEL_RE = re.compile(r"(?:纬度|lat(?:itude)?)\D{0,6}(-?\d{1,3}(?:\.\d+)?)", re.I)
_LNG_LABEL_RE = re.compile(r"(?:经度|lng|lon(?:gitude)?)\D{0,6}(-?\d{1,3}(?:\.\d+)?)", re.I)
# 兜底用的裸数字对：必须带小数点，或用逗号/分号这类明确的分隔符 ——
# 只靠空格的「3 到 4」这类句子不能被当成坐标。
_PAIR_RE = [re.compile(r"(-?\d{1,3}\.\d+)\s*[,，;；/\s]\s*(-?\d{1,3}\.\d+)"),
            re.compile(r"(-?\d{1,3}(?:\.\d+)?)\s*[,，;；/]\s*(-?\d{1,3}(?:\.\d+)?)")]


def _norm_dirs(text):
    """把中文方位词换成字母方位，让后面的解析只有一套写法要处理。"""
    for zh, en in (("北纬", " N"), ("南纬", " S"), ("东经", " E"), ("西经", " W")):
        text = text.replace(zh, en)
    return text


def _from_json(text):
    for m in re.finditer(r"\{[^{}]*\}", text):
        try:
            obj = json.loads(m.group(0))
        except Exception:                            # noqa: BLE001 - 不是 JSON 就继续找下一个
            continue
        if not isinstance(obj, dict):
            continue
        low = {str(k).lower(): v for k, v in obj.items()}
        la = next((low[k] for k in ("lat", "latitude", "纬度") if k in low), None)
        ln = next((low[k] for k in ("lng", "lon", "long", "longitude", "经度") if k in low), None)
        got = clamp_latlng(la, ln)
        if got[0] is not None:
            return got
    return None


def _from_direction(text):
    """按方位字母解析：需要正好一个纬度轴 + 一个经度轴，否则视为不可信。

    前缀写法（N 35.68 / 东经 139.76）优先 —— 它在中文里更常见，
    而后缀正则对「数字 空格 方位」是模糊的，先被前缀认领就不会互相打架。
    """
    pre = {}
    for m in _PREFIX_RE.finditer(text):
        axis, sign = _DIR_AXIS[m.group(1).upper()]
        pre.setdefault(axis, sign * float(m.group(2)))
    got = clamp_latlng(pre.get("lat"), pre.get("lng"))
    if got[0] is not None:
        return got

    suf = {}
    for m in _SUFFIX_RE.finditer(text):
        axis, sign = _DIR_AXIS[m.group(4).upper()]
        val = float(m.group(1))
        if m.group(2):
            val += float(m.group(2)) / 60.0
        if m.group(3):
            val += float(m.group(3)) / 3600.0
        suf.setdefault(axis, sign * val)
    got = clamp_latlng(suf.get("lat"), suf.get("lng"))
    return got if got[0] is not None else None


def _from_label(text):
    a = _LAT_LABEL_RE.search(text)
    b = _LNG_LABEL_RE.search(text)
    if not (a and b):
        return None
    got = clamp_latlng(a.group(1), b.group(1))
    return got if got[0] is not None else None


def _from_pair(text):
    """兜底：整段里只剩「两个数」时按 (纬度, 经度) 读；纬度越界则交换。"""
    for rx in _PAIR_RE:
        m = rx.search(text)
        if not m:
            continue
        a, b = float(m.group(1)), float(m.group(2))
        if abs(a) <= 90 and abs(b) <= 180:
            return (a, b)
        if abs(b) <= 90 and abs(a) <= 180:
            return (b, a)
    return None


def parse_latlng(text):
    """从模型回复里解析出 (lat, lng)，解析不出来返回 None。

    模型不会乖乖只输出 JSON，所以按「最具体 → 最宽松」依次尝试：
    JSON → 方位字母/度分秒 → 带标签的数字 → 裸的两个数字。
    全部失败就返回 None —— 上层会退化成盲猜，并**如实标注**，而不是假装 AI 认出了地方。
    """
    if not text:
        return None
    s = _norm_dirs(str(text))
    for fn in (_from_json, _from_direction, _from_label, _from_pair):
        got = fn(s)
        if got:
            return got
    return None


def _clue_of(text):
    """取模型给的依据字段（没有就退化成开头的短句）。"""
    if not text:
        return ""
    m = re.search(r'"clue"\s*:\s*"([^"]{0,120})"', text)
    if m:
        return m.group(1).strip()
    return re.sub(r"\s+", " ", str(text)).strip()[:60]


def _reason_of(text):
    """取模型给的分步依据，返回 2–4 条短句。

    兼容三种常见输出：整段 JSON、```json 围栏、以及 JSON 坏掉时的数组兜底。
    宁可少显示，也不把整段解释塞进界面。
    """
    if not text:
        return []
    raw = str(text)
    m = re.search(r"\{.*\}", raw, re.S)
    if m:
        try:
            obj = json.loads(m.group(0))
            items = obj.get("reason") or obj.get("reasons") or []
            if isinstance(items, str):
                items = [items]
            if isinstance(items, list):
                out = [re.sub(r"\s+", " ", str(x)).strip()[:180] for x in items]
                out = [x for x in out if x]
                if out:
                    return out[:4]
        except (ValueError, TypeError, AttributeError):
            pass
    # JSON 解析失败：直接从 reason 数组里抓字符串
    m = re.search(r'"reason"\s*:\s*\[(.*?)\]', raw, re.S)
    if m:
        items = re.findall(r'"([^"]{2,180})"', m.group(1))
        if items:
            return [re.sub(r"\s+", " ", x).strip() for x in items][:4]
    return []


def _read_remote_image(url, timeout=25, limit=_IMAGE_LIMIT):
    """下载远程图片，返回 (mime, base64)。超限直接放弃，避免巨型请求打满上下文。"""
    resp = requests.get(url, headers={"User-Agent": _USER_AGENT}, timeout=timeout, stream=True)
    # stream=True 的连接必须显式释放：超限 / 空图都是常见失败路径，
    # 不关就会把连接留在 keep-alive 池里（requests 只在读到底时才自动归还）。
    try:
        resp.raise_for_status()
        mime = (resp.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip()
        buf = b""
        for chunk in resp.iter_content(65536):
            buf += chunk
            if len(buf) > limit:
                raise RuntimeError("图片超过 %d MB" % (limit // 1024 // 1024))
        if not buf:
            raise RuntimeError("图片为空")
        return mime, base64.b64encode(buf).decode("ascii")
    finally:
        resp.close()


def ai_locate(image_url, style=""):
    """让一个 AI 参与者给出坐标，返回 {lat, lng, mode, note}。

    mode: vision = 视觉接口真看图 / chat = 聊天模型（多模态）看图 /
          blind  = 没看成或坐标解析不出来 → 在题库区域内随机落点（并如实标注）
    任何一步失败都不抛异常 —— 一个 AI 掉线不该把整局弄崩。
    """
    mime = b64 = ""
    try:
        mime, b64 = _read_remote_image(image_url)
    except Exception:                                # noqa: BLE001 - 取不到图就走盲猜
        mime = b64 = ""
    return _ai_locate_bytes(mime, b64, style)


def _ai_locate_bytes(mime, b64, style="", template=""):
    """用已经拿到的图片字节做定位；同一局的多个 AI 共用一份图片。"""
    mode, text = "blind", ""
    if b64:
        ask_tpl = GEO_AI_ASK_YUKE if template == "yuke" else GEO_AI_ASK
        ask = ask_tpl.format(style=style or "综合判断")
        for provider, tag in _locate_order():
            if not (get_api_conf(provider)[0] or "").strip():
                continue
            try:
                out = (imaging._recognize_with(provider, mime, b64, question=ask,
                                               max_tokens=600) or "").strip()
            except Exception:                        # noqa: BLE001 - 换下一条通道
                continue
            if out and not out.startswith(_NO_KEY_MARKS):
                text, mode = out, tag
                break
    got = parse_latlng(text) if text else None
    if not got:
        # 盲猜也落在题库区域内，至少不会把 AI 扔到南太平洋，观感上更像「人在猜」
        w, s, e, n = random.choice(GEO_REGIONS)["bbox"]
        got = (round(random.uniform(s, n), 4), round(random.uniform(w, e), 4))
        mode = "blind"
    note = _clue_of(text)
    if not note and mode == "blind":
        note = "没有可用的图像判断，只能凭运气挑了个地方"
    reason = _reason_of(text)
    if not reason and note:
        reason = [note]          # 模型只给了一句话时，至少让理由区有内容
    return {"lat": got[0], "lng": got[1], "mode": mode, "note": note,
            "reason": reason}


# ---------------- 6. 对局仓库（进程内、线程安全） ----------------

_ROUNDS = {}
_LOCK = threading.RLock()
ROUND_TTL_SEC = 6 * 3600                # 超时的对局自然淘汰，避免长跑进程攒内存


def _gc_locked(now=None):
    now = now or time.time()
    for rid in [r for r, v in _ROUNDS.items() if now - v["created"] > ROUND_TTL_SEC]:
        _ROUNDS.pop(rid, None)


def _player_of(round_, pid):
    return next((p for p in round_["players"] if p["id"] == pid), None)


def public_round(round_):
    """下发给前端的题面：**不含真实坐标**（真值只在 reveal 时给出）。"""
    return {
        "id": round_["id"],
        "image_url": round_["photo"]["image_url"],
        "thumb_url": round_["photo"]["thumb_url"],
        "credit": round_["photo"]["credit"],
        "license": round_["photo"]["license"],
        "taken_at": round_["photo"]["taken_at"],
        "players": [{"id": p["id"], "name": p["name"], "kind": p["kind"],
                     **({"tag": p["tag"]} if p.get("tag") else {}),
                     **({"desc": p["desc"]} if p.get("desc") else {})}
                    for p in round_["players"]],
        "guesses": {pid: {"lat": g["lat"], "lng": g["lng"],
                          "mode": g["mode"], "note": g["note"],
                          "reason": g.get("reason") or []}
                    for pid, g in round_["guesses"].items()},
        "revealed": round_["revealed"],
    }


def new_round(ai_count=2, photo=None, ai_off=None):
    """开一局：取一道题 + 建参与者名单。photo 可注入（测试用，跳过网络）。

    ai_off 为「关闭名单」：传列表时按人开关（空列表=全员上场，忽略 ai_count）；
    传 None 时沿用 ai_count 取前 N 位，保证旧调用与测试不变。
    """
    photo = photo or fetch_photo()
    players = [{"id": HUMAN_ID, "name": "你", "kind": "human"}]
    players += ai_roster(ai_count) if ai_off is None else ai_roster_off(ai_off)
    rid = uuid.uuid4().hex[:12]
    with _LOCK:
        _gc_locked()
        _ROUNDS[rid] = {"id": rid, "created": time.time(), "photo": photo,
                        "players": players, "guesses": {}, "revealed": False,
                        "_img": {"lock": threading.Lock(), "mime": "", "b64": ""}}
        return _ROUNDS[rid]


def _live_round_locked(rid):
    """取一局可继续操作的对局；返回 (round, error)。"""
    _gc_locked()
    r = _ROUNDS.get(rid)
    if not r:
        return None, "对局已过期，请开新的一局"
    if r["revealed"]:
        return None, "这一局已经结算了"
    return r, ""


def put_guess(rid, pid, lat, lng, mode="human", note="", reason=None):
    """记录一次标注，返回 (guess, error)。同一人重复提交按最后一次算。

    同一人重标是**允许**的（AI 重试 / 玩家改主意），规则里「标注完成」看的是
    「每人是否至少标过一次」，所以覆盖旧值不会把对局卡死。
    """
    la, ln = clamp_latlng(lat, lng)
    if la is None:
        return None, "坐标不合法"
    with _LOCK:
        r, err = _live_round_locked(rid)
        if err:
            return None, err
        if not _player_of(r, pid):
            return None, "对局里没有这个参与者"
        r["guesses"][pid] = {"lat": la, "lng": ln, "mode": mode,
                             "note": note or "",
                             "reason": [str(x)[:180] for x in (reason or [])][:4],
                             "at": time.time()}
        return dict(r["guesses"][pid]), ""


def ai_context(rid, pid):
    """给 AI 用的一次性上下文（在锁内拷出来，慢速的模型调用放到锁外做）。"""
    with _LOCK:
        r, err = _live_round_locked(rid)
        if err:
            return None, err
        p = _player_of(r, pid)
        if not p or p.get("kind") != "ai":
            return None, "对局里没有这个 AI 参与者"
        return {"image_url": r["photo"]["image_url"], "style": p.get("style", "")}, ""


# 正在调用模型的 (round, player)：同一 AI 并发请求只算一次
_AI_BUSY = set()


def ai_begin(rid, pid):
    """校验参与者并占位；已有标注直接返回，避免重复调用模型。

    返回 (ctx, error)。ctx = {"style": ..., "guess": 已有标注或 None}。
    """
    with _LOCK:
        r, err = _live_round_locked(rid)
        if err:
            return None, err
        p = _player_of(r, pid)
        if not p or p.get("kind") != "ai":
            return None, "对局里没有这个 AI 参与者"
        guess = r["guesses"].get(pid)
        if guess:
            return {"style": p.get("style", ""), "template": p.get("prompt", ""),
                    "guess": dict(guess)}, ""
        key = "%s:%s" % (rid, pid)
        if key in _AI_BUSY:
            return None, "这个 AI 正在定位，请稍候"
        _AI_BUSY.add(key)
        return {"style": p.get("style", ""), "template": p.get("prompt", ""),
                "guess": None}, ""


def ai_end(rid, pid):
    with _LOCK:
        _AI_BUSY.discard("%s:%s" % (rid, pid))


def ai_image(rid):
    """每局只下载/编码一次图片，供 3 个 AI 共用；优先缩略图，失败回退原图。"""
    with _LOCK:
        r = _ROUNDS.get(rid)
        if not r:
            return "", ""
        cache = r.get("_img")
        if not cache:
            cache = {"lock": threading.Lock(), "mime": "", "b64": ""}
            r["_img"] = cache
    with cache["lock"]:
        if cache["b64"]:
            return cache["mime"], cache["b64"]
        photo = r.get("photo") or {}
        thumb = str(photo.get("thumb_url") or "")
        full = str(photo.get("image_url") or "")
        sources = []
        if thumb:
            sources.append((thumb, 3 * 1024 * 1024))
        if full and full != thumb:
            sources.append((full, _IMAGE_LIMIT))
        for url, limit in sources:
            try:
                mime, b64 = _read_remote_image(url, limit=limit)
            except Exception:                        # noqa: BLE001 - 缩略图失败就试原图
                continue
            if b64:
                cache["mime"], cache["b64"] = mime, b64
                return mime, b64
    return "", ""


def ai_locate_round(rid, style="", template=""):
    """用该局缓存的图片做一次定位（取不到图时自动盲猜）。"""
    mime, b64 = ai_image(rid)
    return _ai_locate_bytes(mime, b64, style, template)


# ---------------- 玩家识图辅助（只描述特征，不判断地点） ----------------

GEO_DESCRIBE_ASK = (
    "只描述这张照片里能直接看到的特征，帮助玩家自己判断地点。"
    "不要推断国家、城市、地区或坐标，不要给结论。\n"
    "按下面 6 类各写 1 句，没有就写「不明显」：\n"
    "1. 文字语言：招牌 / 路牌上的文字、字母体系\n"
    "2. 道路：中心线颜色、路标形状与底色、车辆靠哪一侧行驶\n"
    "3. 建筑：材质、屋顶、层数、风格\n"
    "4. 植被地貌：树型、草、土壤、地形\n"
    "5. 气候光线：干湿、日照方向、影子长短\n"
    "6. 车辆设施：车牌颜色、电线杆、卫星锅、护栏\n"
    "每句不超过 30 字，只写观察，不写推理；不要 Markdown。"
)

_DESC_CACHE = {}
_DESC_CACHE_LOCK = threading.Lock()
_DESC_TTL = 3600
_DESC_MAX = 64


def _desc_cache_get(photo_id):
    if not photo_id:
        return ""
    now = time.time()
    with _DESC_CACHE_LOCK:
        item = _DESC_CACHE.get(photo_id)
        if not item:
            return ""
        ts, text = item
        if now - ts > _DESC_TTL:
            _DESC_CACHE.pop(photo_id, None)
            return ""
        return text


def _desc_cache_put(photo_id, text):
    if not photo_id or not text:
        return
    with _DESC_CACHE_LOCK:
        _DESC_CACHE[photo_id] = (time.time(), text)
        while len(_DESC_CACHE) > _DESC_MAX:
            _DESC_CACHE.pop(next(iter(_DESC_CACHE)), None)


def _desc_lines(text):
    lines = []
    for raw in re.split(r"[\r\n]+", text or ""):
        s = re.sub(r"^\s*(?:[-*•]|\d+[.、)])\s*", "", raw).strip()
        if s:
            lines.append(s[:120])
        if len(lines) >= 8:
            break
    return lines or [str(text or "")[:300]]


def geo_describe(rid):
    """玩家点「识图辅助」时才调用：只描述画面特征，结果按照片缓存。"""
    with _LOCK:
        r = _ROUNDS.get(rid)
        if not r:
            return None, "对局已过期，请开新的一局"
        if r.get("_desc"):
            return {"text": r["_desc"], "cached": True}, ""
        photo_id = str(r["photo"].get("photo_id") or "")
        cached = _desc_cache_get(photo_id)
        if cached:
            r["_desc"] = cached
            return {"text": cached, "cached": True}, ""
    mime, b64 = ai_image(rid)
    if not b64:
        return None, "取不到本局图片，暂时无法识图"
    text = ""
    for provider, tag in _locate_order():
        if not (get_api_conf(provider)[0] or "").strip():
            continue
        try:
            out = (imaging._recognize_with(provider, mime, b64, question=GEO_DESCRIBE_ASK,
                                           max_tokens=350) or "").strip()
        except Exception:                            # noqa: BLE001 - 换下一条通道
            continue
        if out and not out.startswith(_NO_KEY_MARKS):
            text = out
            break
    if not text:
        return None, "当前没有可用的识图模型，或图片识别失败"
    text = "\n".join(_desc_lines(text))
    with _LOCK:
        if rid in _ROUNDS:
            _ROUNDS[rid]["_desc"] = text
    _desc_cache_put(photo_id, text)
    return {"text": text, "cached": False}, ""


def reveal(rid):
    """结算：算距离、排名、找冠军。已结算则幂等返回同一份结果。"""
    with _LOCK:
        _gc_locked()
        r = _ROUNDS.get(rid)
        if not r:
            return None, "对局已过期，请开新的一局"
        if not r["revealed"]:
            r["revealed"] = True
            r["result"] = _settle(r)
        return r["result"], ""


def _settle(round_):
    photo = round_["photo"]
    truth = {"lat": photo["lat"], "lng": photo["lng"]}
    rows = score_round([dict(g, player_id=pid) for pid, g in round_["guesses"].items()],
                       (truth["lat"], truth["lng"]))
    results = []
    for row in rows:
        p = _player_of(round_, row["player_id"]) or {}
        g = round_["guesses"].get(row["player_id"]) or {}
        results.append({"player_id": row["player_id"], "name": p.get("name", ""),
                        "kind": p.get("kind", ""), "lat": g.get("lat"), "lng": g.get("lng"),
                        "mode": g.get("mode", ""), "note": g.get("note", ""),
                        "reason": g.get("reason") or [],
                        "distance_km": row["distance_km"], "rank": row["rank"]})
    # 没标注的人单列（无距离、无名次）：前端要能把「没赶上」和「标了但很远」分开显示
    results += [{"player_id": p["id"], "name": p["name"], "kind": p["kind"],
                 "lat": None, "lng": None, "mode": "", "note": "",
                 "reason": [], "distance_km": None, "rank": None}
                for p in round_["players"] if p["id"] not in round_["guesses"]]
    return {
        "truth": truth,
        "results": results,
        "winner": next((x for x in results if x["rank"] == 1), None),
        "image_url": photo["image_url"],
        "credit": photo["credit"],
        "license": photo["license"],
        "taken_at": photo["taken_at"],
    }


def reset_store():
    """清空所有对局（仅供测试与调试）。"""
    with _LOCK:
        _ROUNDS.clear()
        _AI_BUSY.clear()
