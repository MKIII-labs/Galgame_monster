# -*- coding: utf-8 -*-
"""端到端 HTTP 冒烟：真起服务、真发请求，验证路由/派发/鉴权/静态资源。

    python scripts/http_smoke.py

比 dev_check 更接近真实使用（dev_check 只 import + create_server，不发请求）。
只打只读/无害接口，不产生真实模型调用。
"""
import json
import os
import re
import sys
import threading
import time

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.httpd import TOKEN, create_server          # noqa: E402

FAIL = []


def _ok(cond, label):
    print("  [%s] %s" % ("OK  " if cond else "FAIL", label))
    if not cond:
        FAIL.append(label)


def main():
    srv, port = create_server(port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.4)
    base = "http://127.0.0.1:%d" % port
    H = {"X-Auth-Token": TOKEN, "Content-Type": "application/json; charset=utf-8"}
    print("服务端口：%d" % port)

    def get(path, expect=200):
        r = requests.get(base + path, headers=H, timeout=15)
        _ok(r.status_code == expect, "GET  %-26s -> %d" % (path, r.status_code))
        return r

    def post(path, body, expect=200):
        r = requests.post(base + path, headers=H,
                          data=json.dumps(body or {}).encode("utf-8"), timeout=25)
        _ok(r.status_code == expect, "POST %-26s -> %d" % (path, r.status_code))
        return r

    print("\n== GET（do_GET 内联派发） ==")
    r = get("/api/state")
    if r.status_code == 200:
        d = r.json()
        _ok(all(k in d for k in ("config", "profile", "sessions", "kbs", "relations")),
            "state 返回字段齐全")
    get("/api/stt/status")
    get("/api/usage?scope=today")

    print("\n== 静态资源与鉴权 ==")
    r = requests.get(base + "/", timeout=15)
    _ok(r.status_code == 200, "GET  / -> %d" % r.status_code)
    # index.html 里引用的每个 /static/ 资源都必须能取到（防前端拆分后路径漏改）
    with open(os.path.join(ROOT, "web", "index.html"), "r", encoding="utf-8") as f:
        html = f.read()
    refs = sorted(set(re.findall(r"/static/[^\s\"'<>]+", html)))
    _ok(bool(refs), "index.html 至少引用一个静态资源")
    bad = []
    for ref in refs:
        rr = requests.get(base + ref, timeout=15)
        if rr.status_code != 200:
            bad.append("%s -> %d" % (ref, rr.status_code))
    _ok(not bad, "index.html 引用的 %d 个静态资源全部 200%s"
        % (len(refs), ("；失败：" + "、".join(bad)) if bad else ""))
    r = requests.get(base + "/api/state", timeout=15)          # 不带 token
    _ok(r.status_code == 403, "GET  /api/state 无 token -> %d（应 403）" % r.status_code)

    print("\n== POST（路由字典派发） ==")
    post("/api/kb/list", {})
    post("/api/gal/characters", {})
    post("/api/gal/backgrounds", {})
    post("/api/gal/bgms", {})
    post("/api/gal/tts/voices", {})
    post("/api/notes/tree", {})
    post("/api/balance", {})
    post("/api/save/list", {"id": "s_not_exist"})
    # 不存在的会话：应用返回 404 + 业务错误体（证明 handler 正常执行过）
    r = post("/api/gal/script", {"action": "stat", "id": "s_not_exist"}, expect=404)
    try:
        _ok(r.json().get("ok") is False, "gal/script 返回业务错误体而非崩溃")
    except Exception:
        _ok(False, "gal/script 返回体不是 JSON")
    post("/api/no-such-route", {}, expect=404)

    srv.shutdown()
    print("\n" + "=" * 50)
    if FAIL:
        print("✗ 失败 %d 项：" % len(FAIL))
        for x in FAIL:
            print("   -", x)
        return 1
    print("✓ HTTP 冒烟全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
