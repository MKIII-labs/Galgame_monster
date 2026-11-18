# -*- coding: utf-8 -*-
"""真实对话冒烟（**opt-in**：会联网并消耗 token，因此不放进 pytest / dev_check）

    python scripts/chat_smoke.py            # 普通对话一轮

流程：起临时服务 → 新建临时会话 → 发消息并读完整个 SSE 流 → 校验事件序列与正文
      → 删掉临时会话 → 把「当前会话」还原成原来那条。

⚠️ 跑之前请先关闭正在运行的应用：两个进程同时读写 sessions.json 会互相覆盖。

为什么要它：流式聊天是唯一「自动化测试覆盖不到」的主流程（要真调模型 + 收 SSE）。
抽 call_model 这类改动必须靠它兜底。
"""
import argparse
import json
import os
import sys
import threading
import time

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.httpd import TOKEN, create_server          # noqa: E402

FAIL = []


def _ok(cond, label, extra=""):
    print("  [%s] %s%s" % ("OK  " if cond else "FAIL", label, ("  " + extra) if extra else ""))
    if not cond:
        FAIL.append(label)
    return cond


def read_stream(base, H, body, timeout=300):
    """POST /api/chat 并逐条解析 SSE"""
    out = []
    with requests.post(base + "/api/chat", headers=H, json=body,
                       stream=True, timeout=timeout) as r:
        if r.status_code != 200:
            raise RuntimeError("HTTP %d：%s" % (r.status_code, r.text[:300]))
        for line in r.iter_lines(decode_unicode=True):
            if not line:
                continue
            line = line.strip()
            if not line.startswith("data:"):
                continue
            try:
                out.append(json.loads(line[5:].strip()))
            except ValueError:
                pass
    return out


def summarize(evs):
    """返回 (事件类型序列, 正文长度, 错误信息)

    正文口径：普通模式看 `delta`；GAL 模式的台词与旁白分别在
    gal_speech 的 `text` 与 `narration` 两个字段里。
    """
    types = [e.get("type") for e in evs]
    text = "".join(e.get("text") or "" for e in evs if e.get("type") == "delta")
    gal_text = "".join((e.get("text") or "") + (e.get("narration") or "")
                       for e in evs if e.get("type") == "gal_speech")
    errs = [e.get("message") for e in evs if e.get("type") == "error"]
    return types, (text + gal_text), errs


def check_common(evs, label):
    types, text, errs = summarize(evs)
    _ok(bool(types), "%s：有收到 SSE 事件" % label)
    if not types:
        return types, text
    _ok(types[0] == "session", "%s：首事件是 session" % label, "实际=%s" % types[0])
    _ok(types[-1] == "done", "%s：末事件是 done" % label, "实际=%s" % types[-1])
    _ok("session" in types and "recalling" in types, "%s：含 session / recalling" % label)
    _ok("error" not in types, "%s：无 error 事件" % label, "；".join(filter(None, errs))[:160])
    _ok(len(text) > 0, "%s：正文非空" % label, "%d 字" % len(text))
    return types, text


def main():
    ap = argparse.ArgumentParser()
    ap.parse_args()

    srv, port = create_server(port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.4)
    base = "http://127.0.0.1:%d" % port
    H = {"X-Auth-Token": TOKEN, "Content-Type": "application/json; charset=utf-8"}
    print("临时服务端口：%d" % port)

    def get_state():
        return requests.get(base + "/api/state", headers=H, timeout=20).json()

    def post(path, body):
        r = requests.post(base + path, headers=H,
                          data=json.dumps(body or {}).encode("utf-8"), timeout=60)
        return r.json() if r.status_code == 200 else {"ok": False, "error": r.text[:200]}

    orig = (get_state().get("sessions") or {}).get("current")
    print("原当前会话：%s" % orig)
    sid = None
    try:
        # ---- 建临时会话 ----
        post("/api/session/new", {})
        sid = (get_state().get("sessions") or {}).get("current")
        print("临时会话：%s" % sid)
        if not sid or sid == orig:
            print("  [FAIL] 没能建出临时会话")
            return 1

        # ---- 普通对话 ----
        print("\n== 普通对话（逐字流） ==")
        evs = read_stream(base, H, {"session_id": sid, "content": "用一句话打个招呼就好。"})
        types, text = check_common(evs, "普通")
        print("      事件类型：%s" % "、".join(sorted(set(types))))
        print("      正文：%s" % text[:80].replace("\n", " "))
    finally:
        # ---- 清理：删临时会话 + 还原当前会话 ----
        if sid:
            post("/api/session/delete", {"id": sid})
        if orig:
            post("/api/session/select", {"id": orig})
        now = (get_state().get("sessions") or {}).get("current")
        print("\n清理：当前会话已还原 → %s（%s）" % (now, "OK" if now == orig else "注意：与原值不同"))
        srv.shutdown()

    print("\n" + "=" * 50)
    if FAIL:
        print("✗ 失败 %d 项：" % len(FAIL))
        for x in FAIL:
            print("   -", x)
        return 1
    print("✓ 真实对话冒烟通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
