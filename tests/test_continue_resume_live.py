# -*- coding: utf-8 -*-
"""继续输出 / 回到实时输出 · 真机端到端（启动 Edge + 假模型）

默认跳过：
    $env:GM_LIVE_UI=1; python -m pytest tests/test_continue_resume_live.py -q

验证四件事：
  1. 点「继续任务」（按钮挂在旧的那条上）时，前端续写起点必须是**最后一条 assistant**
     —— 与后端 cont_base = messages[-1].content 对齐，否则界面会从旧正文重长；
  2. 续写落库走原地更新：最后一条变长，旧那条不动，不新增第三条 assistant；
  3. 有实时数据时 renderMessages 不会把落库的 _progress 快照弄丢，
     也不会让同一条回答出现两份投影；
  4. 批 1：一次真实回合会把前缀/折叠/压缩/预算观测写进用量记录（只增字段）。
"""
import copy
import http.server
import json
import os
import socketserver
import subprocess
import tempfile
import threading
import time

import pytest
import requests

from app import config as C
from app import sessions as S
from app import webprobe

pytestmark = pytest.mark.skipif(
    not os.environ.get("GM_LIVE_UI"),
    reason="会启动 Edge 与假模型；设 GM_LIVE_UI=1 才跑")

CHUNKS = list("续写追加的内容，慢一点输出才好观察实时行绑定在哪一条消息上")


class _FakeModel(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            req = json.loads(raw.decode("utf-8"))
        except Exception:
            req = {}
        model = req.get("model") or "fake"
        if req.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                for t in CHUNKS:
                    body = {"id": "x", "model": model,
                            "choices": [{"index": 0, "delta": {"content": t},
                                         "finish_reason": None}]}
                    self.wfile.write(("data: " + json.dumps(body, ensure_ascii=False) + "\n\n")
                                     .encode())
                    self.wfile.flush()
                    time.sleep(0.08)
                end = {"id": "x", "model": model,
                       "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                       "usage": {"prompt_tokens": 10, "completion_tokens": len(CHUNKS),
                                 "total_tokens": 10 + len(CHUNKS)}}
                self.wfile.write(("data: " + json.dumps(end, ensure_ascii=False) + "\n\n").encode())
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        data = {"id": "x", "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6}}
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


OLD_TEXT = "旧的一轮正文（215 字那种）"
NEW_TEXT = "最新一轮正文，继续输出必须从这里接"

# 批 1 观测：把后端写出的用量记录捕获下来，断言字段确实落盘（不写真实 data/）
RECORDS = []


def _boot(monkeypatch, messages):
    """起假模型 + 重定向会话文件 + 起 httpd + 开 Edge，返回 (ev, sid, close)。"""
    import app.usage as U
    import app.saves as SA
    from app import httpd
    from app.computer.cdp import CDP

    fake = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _FakeModel)
    fake.daemon_threads = True
    threading.Thread(target=fake.serve_forever, daemon=True).start()

    S.SESSION_FILE = os.path.join(tempfile.mkdtemp(prefix="cu_sess_"), "sessions.json")
    S.save_sessions({"current": "s1", "sessions": [
        {"id": "s1", "title": "续写验证", "messages": messages, "updated_at": 0},
    ]})

    cfg = copy.deepcopy(C.DEFAULT_CONFIG)
    cfg.update({
        "chat_api_key": "fake-key",
        "chat_url": "http://127.0.0.1:%d/v1/chat/completions" % fake.server_address[1],
        "chat_model": "fake", "chat_protocol": "openai",
        "chat_native_tools": False, "chat_think_enabled": False,
        "chat_think_mode": "", "search_enabled": False, "memory_llm_pick": False,
    })
    patch = lambda: dict(cfg)          # noqa: E731
    import app.api.chat as chat_mod
    import app.api.misc as misc_mod
    for mod in (C, chat_mod, misc_mod):
        if hasattr(mod, "load_config"):
            monkeypatch.setattr(mod, "load_config", patch)
    monkeypatch.setattr(C, "save_config", lambda c: None)
    monkeypatch.setattr(U, "record_usage", lambda *a, **k: None)
    monkeypatch.setattr(SA, "add_save", lambda *a, **k: None)
    RECORDS.clear()
    monkeypatch.setattr(chat_mod, "record_usage",
                        lambda rec, *a, **k: RECORDS.append(rec))
    # 前缀观测是进程内状态：每个用例从「本会话第一次请求」开始，才不会互相污染
    from app import prefixtrace
    prefixtrace.reset()

    srv, port = httpd.create_server("127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    edge = webprobe.edge_path()
    if not edge:
        pytest.skip("本机没有 Edge")
    edge_port = 9469
    proc = subprocess.Popen([edge, "--remote-debugging-port=%d" % edge_port,
                             "--user-data-dir=" + tempfile.mkdtemp(prefix="ui_prof_"),
                             "--no-first-run", "--no-default-browser-check",
                             "--disable-background-timer-throttling",
                             "--disable-renderer-backgrounding",
                             "http://127.0.0.1:%d/" % port],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    page = None
    for _ in range(80):
        try:
            lst = requests.get("http://127.0.0.1:%d/json/list" % edge_port, timeout=1).json()
            hit = [t for t in lst if (t.get("url") or "").startswith("http://127.0.0.1:%d" % port)]
            if hit:
                page = hit[0]
                break
        except Exception:
            pass
        time.sleep(0.25)
    assert page, "UI 没打开"
    cdp = CDP(page["webSocketDebuggerUrl"])
    cdp.send("Runtime.enable")
    cdp.send("Page.enable")

    def ev(expr, wait=True, timeout=25):
        r = cdp.send("Runtime.evaluate", {"expression": expr, "returnByValue": True,
                                          "awaitPromise": wait}, timeout=timeout)
        if r.get("exceptionDetails"):
            raise AssertionError((r["exceptionDetails"].get("exception") or {})
                                 .get("description"))
        return r.get("result", {}).get("value")

    for _ in range(80):
        if ev("typeof state !== 'undefined' && !!state.config", wait=False):
            break
        time.sleep(0.25)
    assert ev("!!(state.config && state.config.chat_api_key)", wait=False), "UI 没拿到密钥"

    def close():
        try:
            proc.kill()
        except Exception:
            pass
        try:
            srv.shutdown()
        except Exception:
            pass
        try:
            fake.shutdown()
        except Exception:
            pass

    return ev, close


# ---------------------------------------------------------------- 继续输出
def test_continue_uses_last_assistant(monkeypatch):
    """按钮挂在旧消息上时，前端起点也要取最后一条 assistant。"""
    messages = [
        {"role": "user", "content": "继续吧"},
        {"role": "assistant", "content": OLD_TEXT, "interrupted": True,
         "end_reason": {"kind": "resume", "text": "上次中断", "action": "resume"}},
        {"role": "assistant", "content": NEW_TEXT},
    ]
    ev, close = _boot(monkeypatch, messages)
    try:
        out = ev(r"""
        (async () => {
          window.__bodies = [];
          const of = window.fetch;
          window.fetch = function (u, o) {
            if (String(u).indexOf('/api/chat') === 0 && o && o.body && !o.__seen) {
              try { window.__bodies.push(JSON.parse(o.body)); } catch (e) {}
            }
            return of.apply(this, arguments);
          };
          const btn = document.querySelector('#chat .msg[data-idx="1"] .end-act');
          const hasBtn = !!btn;
          if (btn) btn.click();
          await new Promise(r => setTimeout(r, 300));
          const sid = state.sessions.current;
          const st = state.streams[sid];
          return {
            hasBtn,
            rowIdx: st && st.row ? (st.row.dataset.idx || null) : null,
            body: window.__bodies[0] || null
          };
        })()
        """)
        assert out["hasBtn"] is True, "旧消息上应该有「继续任务」按钮"
        assert out["body"] and out["body"].get("continue") is True, out["body"]
        assert out["body"].get("resume") is True, out["body"]
        # 起点必须是最后一条（idx=2），不是被点击的 idx=1
        assert out["rowIdx"] == "2", "续写行应该绑定最后一条 assistant，实际 %r" % (out["rowIdx"],)

        # 等这一轮跑完，检查落库：最后一条变长、旧那条不动、不新增
        for _ in range(80):
            data = S.load_sessions()
            msgs = data["sessions"][0]["messages"]
            if len(msgs) >= 3 and msgs[2].get("content") != NEW_TEXT:
                break
            time.sleep(0.25)
        data = S.load_sessions()
        msgs = data["sessions"][0]["messages"]
        assert len(msgs) == 3, "不该新增第四条消息，实际 %d 条" % len(msgs)
        assert msgs[1]["content"] == OLD_TEXT, "旧那条不应被改动"
        assert msgs[2]["content"].startswith(NEW_TEXT), msgs[2]["content"][:40]
        assert msgs[2]["content"] != NEW_TEXT, "续写内容没有落库"
    finally:
        close()


# ---------------------------------------------------------------- 回到实时输出
def test_live_projection_keeps_progress_message(monkeypatch):
    """有实时数据时，落库的 _progress 快照必须由 live 投影承担，且只出现一份。"""
    messages = [
        {"role": "user", "content": "继续吧"},
        {"role": "assistant", "content": NEW_TEXT, "_progress": True},
    ]
    ev, close = _boot(monkeypatch, messages)
    try:
        out = ev(r"""
        (async () => {
          const sid = state.sessions.current;
          // 直接在前端构造「最后一条是进度快照」：落库那份会被 reap 转成 interrupted，
          // 而这里要验证的是渲染不变量本身
          const s = curSession();
          s.messages = [
            { role: 'user', content: '继续吧' },
            { role: 'assistant', content: __PROG__, _progress: true }
          ];
          state.streamingSids = state.streamingSids || {};
          state.streamingSids[sid] = true;
          renderMessages();
          const before = document.querySelector('#chat').innerText;
          const beforeRows = document.querySelectorAll('#chat .msg.ai').length;

          // 实时数据接入（相当于 attach 回放写进来的内容）
          state.live = state.live || {};
          state.live[sid] = {
            blocks: [{ kind: 'text', text: '实时正文' }],
            tools: [], plan: null, timing: null, text: '实时正文'
          };
          renderMessages();
          const after = document.querySelector('#chat').innerText;
          const afterRows = document.querySelectorAll('#chat .msg.ai').length;

          delete state.live[sid];
          delete state.streamingSids[sid];
          return {
            beforeKept: before.indexOf('最新一轮正文') >= 0,
            afterKept: after.indexOf('实时正文') >= 0,
            afterHasSnapshot: after.indexOf('最新一轮正文') >= 0,
            beforeRows, afterRows,
            liveRows: document.querySelectorAll('#chat .msg.stream-live').length
          };
        })()
        """.replace("__PROG__", json.dumps(NEW_TEXT)))
        assert out["beforeKept"] is True, "没有实时数据时，进度快照必须正常显示"
        assert out["afterKept"] is True, "有实时数据后，实时正文必须在"
        assert out["afterHasSnapshot"] is False, "同一条回答不能同时出现快照与实时行"
        assert out["beforeRows"] == 1 and out["afterRows"] == 1, out
        assert out["liveRows"] == 1, out
    finally:
        close()


# ---------------------------------------------------------------- 已在运行时的点击
def test_continue_takes_over_when_already_running(monkeypatch):
    """后端那轮还在跑时，点「继续任务」应当接管实时输出，而不是只弹 toast。"""
    messages = [
        {"role": "user", "content": "继续吧"},
        {"role": "assistant", "content": OLD_TEXT, "interrupted": True,
         "end_reason": {"kind": "resume", "text": "上次中断", "action": "resume"}},
    ]
    ev, close = _boot(monkeypatch, messages)
    try:
        out = ev(r"""
        (async () => {
          const sid = state.sessions.current;
          window.__hit = [];
          const of = window.fetch;
          window.fetch = function (u) {
            const s = String(u);
            if (s.indexOf('/api/chat/attach') === 0) window.__hit.push('attach');
            if (s === '/api/chat') window.__hit.push('chat');
            return of.apply(this, arguments);
          };
          // 后端在跑、本地没有流：这就是截图里「任务仍在后台运行」的状态
          state.streamingSids = state.streamingSids || {};
          state.streamingSids[sid] = true;
          renderMessages();
          const btn = document.querySelector('#chat .msg[data-idx="1"] .end-act');
          const hasBtn = !!btn;
          if (btn) btn.click();
          await new Promise(r => setTimeout(r, 900));
          return { hasBtn, hit: window.__hit.slice(0, 4) };
        })()
        """)
        assert out["hasBtn"] is True, "中断消息上应该有「继续任务」按钮"
        assert "attach" in out["hit"], "已在运行时应当接管实时输出，实际请求 %r" % (out["hit"],)
        assert "chat" not in out["hit"], "不该再发一条新的续写请求"
    finally:
        close()


# ---------------------------------------------------------------- 断线回放
def test_replay_delta_does_not_throw(monkeypatch):
    """回放分支的局部变量曾与全局 liveText() 重名，delta 会抛 TypeError 打断回放。"""
    messages = [
        {"role": "user", "content": "继续吧"},
        {"role": "assistant", "content": OLD_TEXT},
    ]
    ev, close = _boot(monkeypatch, messages)
    try:
        out = ev(r"""
        (async () => {
          const sid = state.sessions.current;
          const h = (typeof startJobReplay === 'function') ? startJobReplay(sid) : null;
          if (!h) return { made: false };
          let err = null;
          try {
            h.event({ type: 'base', text: '起点' });
            h.event({ type: 'delta', text: '增量' });
            h.event({ type: 'tool', name: 'read_file' });
            h.event({ type: 'tool_done', name: 'read_file', ok: true });
          } catch (e) { err = String((e && e.message) || e); }
          await new Promise(r => setTimeout(r, 150));   // paint 是 80ms 节流
          const st = state.streams[sid];
          const rowText = (st && st.row) ? st.row.innerText : '';
          const replay = !!(st && st.replay);
          h.finish();
          return { made: true, err, replay,
                   text: rowText.indexOf('增量') >= 0,
                   gone: !(state.streams[sid]) };
        })()
        """)
        assert out["made"] is True, "startJobReplay 没有建行"
        assert out["err"] is None, out["err"]
        assert out["replay"] is True, "回放行应当登记成 replay 流"
        assert out["text"] is True, "回放的 delta 正文没有写进实时行"
        assert out["gone"] is True, "finish 之后应当注销这条回放流"
    finally:
        close()


# ---------------------------------------------------------------- 批 1 观测
def test_turn_writes_token_obs(monkeypatch):
    """一次真实回合必须把「前缀 / 折叠 / 压缩 / 预算」写进用量记录。

    批 1 只做观测：这里断言字段存在且数值合理，不校验任何行为变化。
    """
    messages = [{"role": "user", "content": "你好"}]
    ev, close = _boot(monkeypatch, messages)
    try:
        ev(r"""
        (async () => {
          const res = await fetch('/api/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Auth-Token': TOKEN },
            body: JSON.stringify({
              session_id: state.sessions.current,
              content: '你好',
              think_enabled: false,
            }),
          });
          await res.text();
          return true;
        })()
        """)
        for _ in range(60):
            if RECORDS:
                break
            time.sleep(0.25)
        assert RECORDS, "没有写出任何用量记录"
        rec = RECORDS[-1]
        obs = (rec.get("timing") or {}).get("obs")
        assert isinstance(obs, dict) and obs, "timing.obs 缺失：%r" % (rec.get("timing"),)
        for key in ("fold", "fold_removed", "compress", "overflow", "prefix", "budget", "miss"):
            assert key in obs, "obs 缺字段 %s：%r" % (key, obs)
        assert obs["budget"]["prompt_cap"] > 0, obs["budget"]
        assert obs["budget"]["level"] in (0, 1, 2, 3), obs["budget"]
        assert obs["prefix"]["cold"] is True, "本会话第一次请求应当是 cold"
        assert obs["prefix"]["cur_msgs"] >= 2, obs["prefix"]
        # 未命中构成：四类之和必须等于真实 miss，否则这份统计不可信
        mp = obs["miss"]
        assert (mp["cold"] + mp["new"] + mp["rebuild"] + mp["drift"]) == mp["total"]
        assert mp["total"] == (int(rec.get("prompt") or 0) - int(rec.get("cached") or 0))
        assert rec.get("turn_id"), "turn_id 缺失"
    finally:
        close()
