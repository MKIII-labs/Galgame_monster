# -*- coding: utf-8 -*-
"""本地混合模式：开关、思考参数、轻量模型选择、压缩质量闸门。"""
from app import config, context, llm


def test_local_url_and_hybrid_flag(monkeypatch):
    assert config.is_local_url("http://127.0.0.1:11434/v1/chat/completions")
    assert not config.is_local_url("https://api.deepseek.com/v1/chat/completions")
    monkeypatch.setattr(config, "load_config", lambda: {"hybrid_mode": "on"})
    assert config.hybrid_on() is True
    assert config.light_chat_mode() is False
    monkeypatch.setattr(config, "load_config", lambda: {"hybrid_mode": "off"})
    assert config.hybrid_on() is False
    monkeypatch.setattr(config, "load_config", lambda: {"hybrid_mode": "light"})
    assert config.hybrid_on() is True and config.light_chat_mode() is True
    # 仅轻量模型不要求 light 在本地；是否本地只影响工具与窗口预算
    assert config.light_chat_is_local() is False


def test_ollama_think_mode_disables_with_reasoning_effort(monkeypatch):
    monkeypatch.setattr(config, "load_config", lambda: {})
    url = "http://127.0.0.1:11434/v1/chat/completions"
    assert config.effective_think_mode(url) == "ollama"
    assert config.thinking_payload("ollama", False, "high") == {"reasoning_effort": "none"}
    assert config.thinking_payload("ollama", True, "low") == {"reasoning_effort": "low"}


def test_light_model_is_ignored_when_hybrid_off():
    local = {"hybrid_mode": "off", "light_model": "qwen35-9b:iq4nl",
             "light_url": "http://127.0.0.1:11434/v1/chat/completions"}
    assert llm._pick_light_model(local) == ""
    cloud = {"hybrid_mode": "off", "light_model": "small-cloud",
             "light_url": "https://api.example.com/v1/chat/completions"}
    assert llm._pick_light_model(cloud) == "small-cloud"
    assert llm._pick_light_model(dict(local, hybrid_mode="on")) == "qwen35-9b:iq4nl"
    assert llm._pick_light_model(dict(local, hybrid_mode="on"), use_light=False) == ""
    # IDE 内联补全默认不切本地（延迟敏感），开了 hybrid_ide_complete 才切
    on = dict(local, hybrid_mode="on")
    assert llm._pick_light_model(on, source="ide-complete") == ""
    assert llm._pick_light_model(dict(on, hybrid_ide_complete=True),
                                 source="ide-complete") == "qwen35-9b:iq4nl"


def test_hybrid_context_limit_overrides_base(monkeypatch):
    monkeypatch.setattr(context, "load_config", lambda: {
        "hybrid_mode": "on", "hybrid_context_limit": 64000,
        "chat_reserve_output": 16384,
    })
    monkeypatch.setattr(context, "model_context_limit", lambda model="", cfg=None: 1_000_000)
    limit, reserve = context.context_limits("m")
    assert limit == 64000 and reserve == 16384

    monkeypatch.setattr(context, "load_config", lambda: {
        "hybrid_mode": "off", "chat_reserve_output": 16384,
    })
    limit, _ = context.context_limits("m")
    assert limit == 1_000_000


def test_anchor_extraction_and_quality_gate():
    text = ("修改 web/reader.js 的 captureFlowSelection 与 linkToFlow；"
            "同时检查 selkit.js 的 liveFromBlock 与 paintLiveFlow；"
            "app/api/chat.py 的 _pending_plan_end_reason 返回 HTTP 400；"
            "报错 Error: ctx is not defined；待办：补 tests/test_reader.py 与 tests/test_hybrid.py。")
    anchors = context._extract_anchors(text)
    assert "web/reader.js" in anchors
    assert "captureFlowSelection" in anchors
    assert any("Error" in a for a in anchors)
    assert len(anchors) >= 8

    summary = "已改 web/reader.js 的 captureFlowSelection。"   # 漏了其余事实
    assert context._summary_quality_ok(text, summary, "", {"hybrid_quality_gate": 0.55}) is False
    good = (summary + " 同时改 linkToFlow、selkit.js 的 liveFromBlock / paintLiveFlow；"
            "chat.py 的 _pending_plan_end_reason 返回 HTTP 400；"
            "报错 Error: ctx is not defined；待办补 tests/test_reader.py、tests/test_hybrid.py。")
    assert context._summary_quality_ok(text, good, "", {"hybrid_quality_gate": 0.55}) is True


def test_split_chunks_respect_token_limit():
    replay = [{"role": "assistant", "content": "内容" * 3000} for _ in range(5)]
    chunks = context._split_replay_chunks(replay, 3500)
    assert len(chunks) >= 5
    assert all(len(c) > 0 for c in chunks)


def test_augment_summary_adds_missing_critical_facts():
    text = "改 app/context.py 的 compress_history；报错 Error: boom，退出码 1。"
    out = context._augment_summary("## 摘要\n- 改了压缩逻辑", text)
    assert "app/context.py" in out
    assert "Error: boom" in out
    # 已经包含路径时不重复追加整段
    same = context._augment_summary(out, text)
    assert same == out


def test_light_mode_overrides_chat_conf_and_chain(monkeypatch):
    cfg = {
        "hybrid_mode": "light",
        "light_url": "http://127.0.0.1:11434/v1/chat/completions",
        "light_model": "qwen35-9b:iq4nl", "light_api_key": "ollama",
        "chat_url": "https://api.deepseek.com/v1/chat/completions",
        "chat_model": "deepseek-flash", "chat_api_key": "sk-x",
        "chat_fallback": ["backup"],
    }
    monkeypatch.setattr(config, "load_config", lambda: cfg)
    key, url, model = config.get_api_conf("chat")
    assert model == "qwen35-9b:iq4nl" and "11434" in url and key == "ollama"
    chain = config.provider_chain()
    assert len(chain) == 1 and chain[0][4] == "qwen35-9b:iq4nl"
    # 本地轻量模型是文本模型：识图改走视觉 API
    assert config.use_chat_for_vision() is False
    # 缺 light 地址 / 模型：返回空，避免悄悄回落到主 API
    assert config.get_api_conf("chat", dict(cfg, light_url="", light_model="")) == ("", "", "")
    # 只走 light：云端 light 同样允许（不再限定本地）
    cloud = dict(cfg, light_url="https://api.example.com/v1/chat/completions")
    assert llm._pick_light_model(cloud) == "qwen35-9b:iq4nl"
    ckey, curl, cmodel = config.get_api_conf("chat", cloud)
    assert cmodel == "qwen35-9b:iq4nl" and "example.com" in curl and ckey == "ollama"
    # 云端 light 不是文本限制场景：识图仍跟随原设置
    monkeypatch.setattr(config, "load_config", lambda: cloud)
    assert config.use_chat_for_vision() is True


def test_light_context_limits(monkeypatch):
    # 本地 light：窗口按 local_context_limit
    monkeypatch.setattr(context, "load_config", lambda: {
        "hybrid_mode": "light",
        "light_url": "http://127.0.0.1:11434/v1/chat/completions",
        "local_context_limit": 12000, "local_reserve_output": 2048,
    })
    monkeypatch.setattr(context, "model_context_limit", lambda model="", cfg=None: 1_000_000)
    limit, reserve = context.context_limits("m")
    assert limit == 12000 and reserve == 2048
    # 云端 light：沿用用户填的窗口，不吃 local_context_limit
    monkeypatch.setattr(context, "load_config", lambda: {
        "hybrid_mode": "light",
        "light_url": "https://api.example.com/v1/chat/completions",
        "chat_reserve_output": 4096,
    })
    limit, reserve = context.context_limits("m")
    assert limit == 1_000_000 and reserve == 4096


def test_ollama_reasoning_delta_is_captured():
    """Ollama 的 OpenAI 兼容端点把思考发在 delta.reasoning（不是 reasoning_content）。"""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class _H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(n)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for payload in ('{"choices":[{"delta":{"reasoning":"先想"}}]}',
                            '{"choices":[{"delta":{"content":"再答"},"finish_reason":"stop"}]}'):
                data = ("data: " + payload + "\n\n").encode()
                self.wfile.write(("%X\r\n" % len(data)).encode() + data + b"\r\n")
            done = b"data: [DONE]\n\n"
            self.wfile.write(("%X\r\n" % len(done)).encode() + done + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    events = []
    try:
        res = llm.stream_chat(
            "k", "http://127.0.0.1:%d/v1/chat/completions" % srv.server_address[1], "m",
            [{"role": "user", "content": "hi"}], allow_tool=False,
            emit=events.append)
    finally:
        srv.shutdown()
        srv.server_close()
    assert res["reasoning"] == "先想"
    assert res["visible"] == "再答"
    assert any(e.get("type") == "reasoning" and e.get("text") == "先想" for e in events)


def test_light_mode_uses_non_agent_prompt(monkeypatch):
    from app import agent
    s = {"agent_enabled": True, "sandbox": "D:/tmp"}
    monkeypatch.setattr(agent, "light_chat_is_local", lambda: False)
    full = agent.build_agent_prompt(s)
    monkeypatch.setattr(agent, "light_chat_is_local", lambda: True)
    lite = agent.build_agent_prompt(s)
    assert lite == "" and len(lite) < len(full) * 0.6
