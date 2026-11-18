# -*- coding: utf-8 -*-
"""手机访问管理接口（仅本机可调：设置页与网关内部使用）。"""
import time

from app.registry import route
from app.mobile import (DEFAULT_PORT, audit, ca, devices, gateway, identity, lan,
                        pairing)


def _cfg():
    from app.config import load_config
    return load_config()


def _save(patch):
    from app.config import load_config, save_config
    cfg = load_config()
    cfg.update(patch)
    save_config(cfg)


def _port():
    raw = _cfg().get("mobile_port")
    try:
        port = int(raw) if raw is not None else DEFAULT_PORT
    except (TypeError, ValueError):
        return DEFAULT_PORT
    return port if 0 <= port <= 65535 else DEFAULT_PORT


def _main_port(h):
    try:
        return int(h.server.server_address[1])
    except Exception:
        return 0


def _status_payload():
    st = gateway.status()
    fw = lan.firewall_status()
    return {
        "ok": True,
        "enabled": bool(_cfg().get("mobile_enabled")),
        "port": _port(),
        "gateway": st,
        "lan_ip": lan.lan_ip(),
        "candidates": lan.candidate_ips(),
        "firewall": fw,
        "admin": lan.is_admin(),
        "fingerprint": identity.get_identity().fingerprint,
        "ca_fingerprint": ca.existing_fingerprint(),
        "devices": devices.list_all(),
        "pending": pairing.list_pending(),
        "accepts_https": st.get("running", False),
    }


@route("/api/mobile/status", methods=("GET", "POST"))
def _api_status(h):
    return h._json(_status_payload())


@route("/api/mobile/enable")
def _api_enable(h):
    from app import httpd
    port = _port()
    st = gateway.start(port, _main_port(h), httpd.TOKEN)
    if not st.get("running"):
        return h._json({"ok": False, "error": st.get("error") or "网关启动失败",
                        "gateway": st, "firewall": {}}, 500)
    _save({"mobile_enabled": True})
    fw = lan.ensure_firewall(st.get("port") or port)
    audit.record("gateway_start", port=st.get("port"), lan_ip=lan.lan_ip())
    return h._json({**_status_payload(), "firewall": {**lan.firewall_status(),
                                                       "message": fw.get("message", "")}})


@route("/api/mobile/disable")
def _api_disable(h):
    st = gateway.stop()
    pairing.clear_pending()
    _save({"mobile_enabled": False})
    audit.record("gateway_stop")
    return h._json({**_status_payload(), "gateway": st})


@route("/api/mobile/firewall")
def _api_firewall(h):
    st = gateway.status()
    if not st.get("running"):
        return h._json({"ok": False, "error": "网关未启动"}, 409)
    res = lan.ensure_firewall(st.get("port") or _port())
    return h._json({"ok": bool(res.get("ok")), "result": res,
                    "firewall": lan.firewall_status()})


@route("/api/mobile/pair/start")
def _api_pair_start(h):
    st = gateway.status()
    if not st.get("running"):
        return h._json({"ok": False, "error": "请先开启手机访问"}, 409)
    try:
        ticket = pairing.create_ticket()
    except pairing.PairingError as exc:
        return h._json({"ok": False, "error": str(exc)}, 429)
    ident = identity.get_identity()
    base = st.get("url") or ("https://%s:%d" % (lan.lan_ip(), st.get("port") or _port()))
    name = str(h._body().get("name") or "").strip()
    payload = pairing.pair_url(base, ident.fingerprint, ticket, name)
    try:
        svg = pairing.qr_svg(payload)
    except pairing.PairingError as exc:
        return h._json({"ok": False, "error": str(exc)}, 500)
    audit.record("pair_ticket", pair_id=ticket["pair_id"])
    return h._json({"ok": True, "payload": payload, "qr_svg": svg,
                    "expires": ticket["expires"], "ttl": int(ticket["expires"] - time.time()),
                    "fingerprint": ident.fingerprint, "url": base,
                    "ca_fingerprint": ca.ca_fingerprint()})


@route("/api/mobile/pair/pending")
def _api_pair_pending(h):
    return h._json({"ok": True, "pending": pairing.list_pending()})


@route("/api/mobile/pair/confirm")
def _api_pair_confirm(h):
    body = h._body()
    pair_id = str(body.get("pair_id") or "")
    ok = pairing.resolve(pair_id, bool(body.get("ok")))
    audit.record("pair_confirm", pair_id=pair_id, allow=bool(body.get("ok")))
    return h._json({"ok": ok})


@route("/api/mobile/devices")
def _api_devices(h):
    return h._json({"ok": True, "devices": devices.list_all()})


@route("/api/mobile/revoke")
def _api_revoke(h):
    body = h._body()
    did = str(body.get("device_id") or "")
    ok = devices.revoke(did)
    if ok:
        audit.record("device_revoked", device=did)
    return h._json({"ok": ok, "devices": devices.list_all()})


@route("/api/mobile/revoke-all")
def _api_revoke_all(h):
    devices.revoke_all()
    audit.record("device_revoked_all")
    return h._json({"ok": True, "devices": devices.list_all()})


@route("/api/mobile/audit")
def _api_audit(h):
    try:
        limit = int(h._body().get("limit") or 200)
    except (TypeError, ValueError):
        limit = 200
    return h._json({"ok": True, "items": audit.tail(max(1, min(1000, limit)))})


@route("/api/mobile/reset")
def _api_reset(h):
    """重置桌面身份并吊销全部设备（所有手机都要重新配对）。"""
    gateway.stop()
    pairing.clear_pending()
    devices.reset()
    identity.reset_identity()
    _save({"mobile_enabled": False})
    audit.record("identity_reset")
    return h._json(_status_payload())


@route("/api/mobile/state")
def _api_state(h):
    """给网关用的脱敏状态：不含 config / API Key / 用户画像。"""
    from app.api.misc import _live_streams
    from app.sessions import load_sessions, slim_sessions
    from app.context import current_context
    from app import session_status
    data = load_sessions()
    return h._json({
        "ok": True,
        "sessions": slim_sessions(data),
        "context": current_context(data),
        "streaming": _live_streams(),
        "pending": session_status.snapshot(),
        "mobile": {"fingerprint": identity.get_identity().fingerprint,
                   "lan_ip": lan.lan_ip()},
    })
