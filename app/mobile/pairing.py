# -*- coding: utf-8 -*-
"""配对票据 / 桌面确认 / 二维码载荷。

票据 5 分钟有效、单次使用；连续失败触发全局冷却（防在线暴力猜票据）。
二维码只含公钥指纹与一次性票据，不含任何长期秘密。
"""
import base64
import secrets
import socket
import threading
import time
import urllib.parse

from app.mobile import crypto

PAIR_TTL = 300
MAX_ATTEMPTS = 5
FAIL_WINDOW = 600
FAIL_LIMIT = 10

_LOCK = threading.RLock()
_TICKETS = {}
_PENDING = {}
_FAILS = []


class PairingError(Exception):
    pass


def _prune_fails(now):
    while _FAILS and now - _FAILS[0] > FAIL_WINDOW:
        _FAILS.pop(0)


def cooldown_left():
    with _LOCK:
        now = time.time()
        _prune_fails(now)
        if len(_FAILS) < FAIL_LIMIT:
            return 0.0
        return max(0.0, FAIL_WINDOW - (now - _FAILS[0]))


def create_ticket(ttl=PAIR_TTL):
    left = cooldown_left()
    if left > 0:
        raise PairingError("配对失败次数过多，请 %.0f 秒后再试" % left)
    now = time.time()
    ticket = {"pair_id": secrets.token_urlsafe(12),
              "secret": secrets.token_urlsafe(32),
              "created": now,
              "expires": now + float(ttl),
              "attempts": 0}
    with _LOCK:
        for pid, item in list(_TICKETS.items()):
            if item.get("expires", 0) < now:
                _TICKETS.pop(pid, None)
        _TICKETS[ticket["pair_id"]] = ticket
    return ticket


def get_ticket(pair_id):
    with _LOCK:
        return _TICKETS.get(str(pair_id or ""))


def verify_proof(pair_id, transcript, proof):
    """校验 HMAC 配对证明；成功即消费票据（单次使用）。"""
    ticket = get_ticket(pair_id)
    if not ticket:
        return False, "票据不存在或已使用"
    if ticket["expires"] < time.time():
        with _LOCK:
            _TICKETS.pop(pair_id, None)
        return False, "票据已过期"
    if ticket["attempts"] >= MAX_ATTEMPTS:
        with _LOCK:
            _TICKETS.pop(pair_id, None)
        return False, "尝试次数过多"
    if not crypto.verify_pair_proof(ticket["secret"], transcript, proof):
        with _LOCK:
            ticket["attempts"] += 1
            _FAILS.append(time.time())
        return False, "配对证明不正确"
    with _LOCK:
        _TICKETS.pop(pair_id, None)
    return True, ""


def ticket_secret(pair_id):
    ticket = get_ticket(pair_id)
    return ticket["secret"] if ticket else ""


# ---------- 桌面确认（配对最后一步由 PC 用户点「允许」） ----------

def add_pending(info):
    """登记一条待桌面确认的配对；返回带 Event 的内部对象。"""
    obj = dict(info)
    obj["event"] = threading.Event()
    with _LOCK:
        _PENDING[obj["pair_id"]] = obj
    return obj


def list_pending():
    now = time.time()
    with _LOCK:
        out = []
        for pid, item in list(_PENDING.items()):
            if now - float(item.get("created") or 0) > 120:
                _PENDING.pop(pid, None)
                item["event"].set()
                continue
            if item.get("resolved"):
                continue
            out.append({"pair_id": pid, "name": item.get("name") or "",
                        "sas": item.get("sas") or "", "created": item.get("created")})
    return out


def resolve(pair_id, ok):
    with _LOCK:
        item = _PENDING.get(str(pair_id or ""))
        if not item:
            return False
        item["ok"] = bool(ok)
        item["resolved"] = True
        item["event"].set()
    return True


def wait_decision(pair_id, timeout=120.0):
    """等待桌面确认；返回 True / False / None（超时或请求不存在）。"""
    with _LOCK:
        item = _PENDING.get(str(pair_id or ""))
    if not item:
        return None
    if not item["event"].wait(max(0.0, float(timeout))):
        return None
    with _LOCK:
        _PENDING.pop(str(pair_id or ""), None)
    return bool(item.get("ok"))


def clear_pending():
    with _LOCK:
        items = list(_PENDING.values())
        _PENDING.clear()
    for item in items:
        item["event"].set()


# ---------- 二维码 ----------

def pair_url(base_url, fingerprint, ticket, name=""):
    """二维码内容：参数放 fragment，不会进服务端日志 / Referer。"""
    base = str(base_url or "").rstrip("/")
    frag = "v=1&fp=%s&t=%s.%s&n=%s" % (
        urllib.parse.quote(fingerprint, safe=""),
        urllib.parse.quote(ticket["pair_id"], safe=""),
        urllib.parse.quote(ticket["secret"], safe=""),
        urllib.parse.quote(str(name or socket.gethostname()), safe=""),
    )
    return "%s/#%s" % (base, frag)


def qr_svg(text, box_size=8):
    """生成 SVG 二维码（返回字符串）。未安装 qrcode 时抛出明确错误。"""
    try:
        import qrcode
        import qrcode.image.svg
    except Exception as exc:                          # noqa: BLE001
        raise PairingError("缺少 qrcode 依赖：%s" % exc)
    img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage,
                      box_size=max(1, int(box_size)), border=2)
    import io
    buf = io.BytesIO()
    img.save(buf)
    svg = buf.getvalue().decode("utf-8")
    # 去掉 XML 声明：这段 SVG 直接塞进设置页的 innerHTML
    if svg.lstrip().startswith("<?xml"):
        svg = svg.split("?>", 1)[-1]
    return svg.strip()


def b64(raw):
    return base64.b64encode(bytes(raw)).decode("ascii")
