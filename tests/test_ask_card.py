# -*- coding: utf-8 -*-
"""提问卡：选项结构化、等待截止、让 AI 自己决定、策略分流。"""
import time

from app import agent
from app.api import chat, sandbox
from app.config import default_config


class _H:
    def __init__(self, body):
        self._b = body
        self.json = None
        self.code = 0

    def _body(self):
        return self._b

    def _json(self, obj, code=200):
        self.json = obj
        self.code = code
        return obj

    def _ok_sessions(self, data):
        self.json = {"ok": True, "sessions": data.get("sessions") or []}
        return self.json


def test_reasoning_ask_nudge_only_first_round_without_ask_call():
    """思考里说要问用户风格，却直接开工：第一轮拦一次，逼它走提问卡。"""
    from app.api.chat_nudge import _needs_reasoning_ask_nudge, _reasoning_wants_ask

    en = ("The user hasn't specified a style. I should ask the user which design "
          "style they want before building.")
    assert _reasoning_wants_ask(en) is True
    assert _needs_reasoning_ask_nudge(en, [{"tool": "read_file"}], False, 0) is True

    zh = "需要先和用户确认一下风格，再开始实现。"
    assert _reasoning_wants_ask(zh) is True
    assert _needs_reasoning_ask_nudge(zh, [{"tool": "write_file"}], False, 0) is True

    # 已经调用 ask_user / 不是第一轮 / 已提醒过 / 没有工具调用 → 不拦
    assert _needs_reasoning_ask_nudge(en, [{"tool": "ask_user"}], False, 0) is False
    assert _needs_reasoning_ask_nudge(en, [{"tool": "read_file"}], False, 1) is False
    assert _needs_reasoning_ask_nudge(en, [{"tool": "read_file"}], True, 0) is False
    assert _needs_reasoning_ask_nudge(en, [], False, 0) is False

    # 明确决定不问 / 只是在调整样式 → 不误伤
    assert _reasoning_wants_ask("不需要问用户风格，直接按默认做。") is False
    assert _reasoning_wants_ask("调整 CSS 的 design token 和 style 变量。") is False


def test_new_asks_normalizes_options_and_expiry():
    qid, clean = agent.new_asks("s_ask", [{
        "question": "选方案",
        "options": ["A", {"label": "B", "desc": "改动小、可回滚", "recommended": True}],
    }], timeout=120)
    opts = clean[0]["options"]
    assert opts[0] == {"label": "A", "desc": "", "recommended": False}
    assert opts[1] == {"label": "B", "desc": "改动小、可回滚", "recommended": True}

    meta = agent.ask_meta(qid)
    assert meta and meta["timeout"] == 120
    assert meta["expires_at"] > time.time()


def test_new_asks_clamps_timeout():
    qid, _clean = agent.new_asks("s_ask", [{"question": "q"}], timeout=1)
    assert agent.ask_meta(qid)["timeout"] == 1       # 下限放开：>0 即按秒计
    qid2, _clean2 = agent.new_asks("s_ask", [{"question": "q"}], timeout=99999)
    assert agent.ask_meta(qid2)["timeout"] == 3600   # 上限 1 小时


def test_new_asks_zero_timeout_means_no_expiry():
    """timeout=0：不限时等待（无截止时间、无倒计时）；wait 被 dismiss 唤醒。"""
    qid, _clean = agent.new_asks("s_ask", [{"question": "q"}], timeout=0)
    meta = agent.ask_meta(qid)
    assert meta["timeout"] == 0 and meta["expires_at"] == 0
    agent.dismiss_ask(qid)
    # 不传 timeout：走登记值 0 = 永久等待；若误成立刻过期，这里会返回 None
    assert agent.wait_answers(qid) == ["（由 AI 自行决定）"]


def test_pending_asks_snapshot_and_api():
    """断线后提问卡要能重建：快照含问题原文，回答后立刻消失。"""
    qid, _clean = agent.new_asks("s_ask_pending", [{"question": "选哪个？",
                                                    "options": ["A", "B"]}], timeout=0)
    rows = agent.pending_asks("s_ask_pending")
    assert len(rows) == 1 and rows[0]["qid"] == qid
    assert rows[0]["questions"][0]["question"] == "选哪个？"
    assert agent.pending_asks("other_session") == []

    h = _H({"session_id": "s_ask_pending"})
    sandbox._api_interactions(h)
    assert h.json["ok"] is True
    assert any(it["kind"] == "ask" and it["qid"] == qid for it in h.json["items"])

    assert agent.submit_answers(qid, ["A"]) is True
    agent.wait_answers(qid, timeout=0.1)
    h2 = _H({"session_id": "s_ask_pending"})
    sandbox._api_interactions(h2)
    assert all(it.get("qid") != qid for it in h2.json["items"])


def test_dismiss_ask_wakes_waiter():
    qid, _clean = agent.new_asks("s_ask", [{"question": "q1"}, {"question": "q2"}])
    assert agent.dismiss_ask(qid) is True
    assert agent.wait_answers(qid, timeout=1) == ["（由 AI 自行决定）"] * 2
    assert agent.dismiss_ask(qid) is False           # 已被消费


def test_policy_split_ask_mode_keeps_everything():
    qs = [{"question": "要不要删除旧数据？"},
          {"question": "请登录你的账号", "action": True}]
    auto, ask, logged = chat._ask_policy_split(qs, "ask")
    assert auto == [] and logged == []
    assert len(ask) == 2


def test_policy_split_auto_avoids_irreversible_and_keeps_action():
    """auto 模式也不执行不可逆操作：改成「可回滚最小方案」代答；亲手操作仍然弹卡。"""
    qs = [{"question": "要不要删除旧数据？", "options": [{"label": "删除"}]},
          {"question": "请登录你的账号", "action": True, "detail": "扫码"}]
    auto, ask, logged = chat._ask_policy_split(qs, "auto")
    assert len(auto) == 1 and "可回滚" in auto[0]
    assert len(ask) == 1 and ask[0].get("action") is True
    assert logged and logged[0]["auto"] is True


def test_answer_endpoint_returns_409_when_expired():
    h = _H({"qid": "q_missing", "answers": ["x"]})
    sandbox._api_agent_answer(h)
    assert h.code == 409
    assert h.json["ok"] is False and "超时" in h.json["error"]


def test_dismiss_endpoint_and_route():
    from app.registry import snapshot
    assert ("POST", "/api/agent/ask/dismiss") in snapshot()

    qid, _clean = agent.new_asks("s_ask2", [{"question": "q"}])
    h = _H({"qid": qid})
    sandbox._api_agent_ask_dismiss(h)
    assert h.json["ok"] is True
    agent.wait_answers(qid, timeout=1)      # 消费掉，避免残留


def test_ask_notice_text(monkeypatch):
    """提问登记时把题面带给系统通知（多问题只报条数 + 第一题）。"""
    from app import events
    seen = []
    monkeypatch.setattr(events, "publish", lambda obj: seen.append(obj))

    qid, _c = agent.new_asks("s_ask_notice",
                             [{"question": "用哪个方案？"}, {"question": "要不要备份？"}])
    ev = [o for o in seen if o.get("type") == "session_pending"][-1]
    assert ev["kind"] == "ask" and ev["text"] == "共 2 个问题：用哪个方案？"
    agent.wait_answers(qid, timeout=0.01)          # 消费掉，避免残留在全局表里

    qid2, _c2 = agent.new_asks("s_ask_action", [{"question": "登录账号", "action": True,
                                                 "detail": "在弹出的窗口里扫码"}])
    ev = [o for o in seen if o.get("type") == "session_pending"][-1]
    assert ev["text"] == "需要你在本机操作：在弹出的窗口里扫码"
    agent.wait_answers(qid2, timeout=0.01)


def test_defaults_include_ask_and_notify():
    cfg = default_config()
    assert cfg["autonomy_question_policy"] == "ask"
    assert cfg["notify_banner"] is True
    assert cfg["notify_volume"] == 60
    # 验收子代理默认开启；设置页关掉后 agent 收尾不再跑独立复核
    assert cfg["agent_acceptance_review"] is True


def test_ask_attachment_payload_filters_bad_items():
    """提问卡附件只收结构完整的项，脏数据直接丢掉。"""
    payload = sandbox._ask_attach_payload({
        "images": [{"data": "aGk=", "mime": "image/png", "name": "a.png"}, {"mime": "image/png"}],
        "docs": [{"name": "a.txt", "data": "aGk="}, {"name": "", "data": "aGk="}, "bad"],
    })
    assert len(payload["images"]) == 1 and payload["images"][0]["name"] == "a.png"
    assert len(payload["docs"]) == 1 and payload["docs"][0]["name"] == "a.txt"


def test_register_ask_uploads_saves_and_parses(monkeypatch):
    """图片 / 文档落盘后带可挂到消息上的元信息；文档顺手抽正文。"""
    calls = []

    def _fake_save(sid, mime, b64, name=""):
        calls.append((sid, mime, name))
        return {"id": "att_x", "name": "u_" + str(len(calls)), "mime": mime, "size": 3}

    monkeypatch.setattr(sandbox, "save_upload", _fake_save)
    monkeypatch.setattr(sandbox.docparse, "extract_pages",
                        lambda name, raw: ("正文", "", []))
    out = sandbox._register_ask_uploads("s_att", {
        "images": [{"mime": "image/png", "data": "aGk=", "name": "a.png"}],
        "docs": [{"name": "a.txt", "mime": "text/plain", "data": "aGk="}],
    })
    assert out["images"][0]["file"] == "u_1" and out["images"][0]["name"] == "a.png"
    assert out["docs"][0]["file"] == "u_2" and out["docs"][0]["text"] == "正文"
    assert out["names"] == ["a.png", "a.txt"]
    assert sandbox._register_ask_uploads("", {"images": [], "docs": []}) is None


def test_submit_answers_carries_attachment_summary():
    """附件摘要随回答提交，wait 之后由聊天线程取走一次。"""
    qid, _clean = agent.new_asks("s_ask_att", [{"question": "q"}], timeout=0)
    atts = {"images": 1, "docs": 0, "names": ["a.png"]}
    assert agent.submit_answers(qid, ["A"], attachments=atts) is True
    assert agent.ask_attachments(qid) == atts
    assert agent.ask_attachments(qid) is None
    assert agent.wait_answers(qid, timeout=0.1) == ["A"]


def test_session_agent_toggle_sets_live_flags(monkeypatch):
    """中途开关 Agent：接口写会话后留一份即时开关，回合内下一轮取走一次。"""
    from app.store import peek_live_flags, take_live_flags

    fake = {"sessions": [{"id": "s_live", "agent_enabled": False, "sandbox": ""}]}
    monkeypatch.setattr(sandbox, "load_sessions", lambda: fake)
    monkeypatch.setattr(sandbox, "find_session", lambda d, sid: d["sessions"][0])
    monkeypatch.setattr(sandbox, "ensure_session_kb_fields", lambda s: None)
    monkeypatch.setattr(sandbox, "save_sessions", lambda d: None)
    monkeypatch.setattr(sandbox.agent, "clean_sandbox", lambda p: str(p or ""))
    monkeypatch.setattr(sandbox.os.path, "isdir", lambda p: True)
    take_live_flags("s_live")
    h = _H({"id": "s_live", "agent_enabled": True, "sandbox": "D:/work"})
    sandbox._api_session_agent(h)
    assert peek_live_flags("s_live") == {"agent_enabled": True, "sandbox": "D:/work",
                                         "approval_mode": "ai"}
    assert take_live_flags("s_live") == {"agent_enabled": True, "sandbox": "D:/work",
                                         "approval_mode": "ai"}
    assert take_live_flags("s_live") is None
    # 审批档位也要即时生效：回合内下一次工具调用就用新档位判定
    h2 = _H({"id": "s_live", "approval_mode": "ask"})
    sandbox._api_session_agent(h2)
    assert take_live_flags("s_live") == {"agent_enabled": True, "sandbox": "D:/work",
                                         "approval_mode": "ask"}
    # 清空目录也要即时生效（回到未选目录的只读模式）
    h3 = _H({"id": "s_live", "sandbox": ""})
    sandbox._api_session_agent(h3)
    assert take_live_flags("s_live") == {"agent_enabled": True, "sandbox": "",
                                         "approval_mode": "ask"}


def test_persist_session_keeps_live_agent_flags(monkeypatch):
    """回合内改过 Agent 开关：落库时不能被旧的会话内存快照覆盖回去。"""
    from app.store import set_live_flags, take_live_flags

    s = {"id": "s_persist", "agent_enabled": False, "sandbox": ""}
    monkeypatch.setattr(chat, "load_sessions",
                        lambda copy=True: {"sessions": [], "current": ""})
    monkeypatch.setattr(chat, "save_session", lambda x, **k: None)
    set_live_flags("s_persist", {"agent_enabled": True, "sandbox": "D:/work"})
    chat._persist_session(s)
    assert s["agent_enabled"] is True and s["sandbox"] == "D:/work"
    # peek 不消费：工具循环的下一轮仍要取到它来重建 schema
    assert take_live_flags("s_persist") == {"agent_enabled": True, "sandbox": "D:/work"}


def test_persist_session_keeps_live_approval_mode(monkeypatch):
    """回合内改过审批档位：落库时不能被会话内存快照里的旧值覆盖回去。"""
    from app.store import set_live_flags, take_live_flags

    s = {"id": "s_am", "agent_enabled": True, "sandbox": "D:/work",
         "approval_mode": "ai"}
    monkeypatch.setattr(chat, "load_sessions",
                        lambda copy=True: {"sessions": [], "current": ""})
    monkeypatch.setattr(chat, "save_session", lambda x, **k: None)
    set_live_flags("s_am", {"agent_enabled": True, "sandbox": "D:/work",
                            "approval_mode": "ask"})
    chat._persist_session(s)
    assert s["approval_mode"] == "ask"
    take_live_flags("s_am")


def test_question_policy_falls_back_to_ask():
    """缺字段 / 脏值 / 大小写：一律按 ask 处理，不允许静默变成自决。"""
    from app.api.chat import question_policy_of
    from app.config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["autonomy_question_policy"] == "ask"
    assert question_policy_of({}) == "ask"
    assert question_policy_of(None) == "ask"
    assert question_policy_of({"autonomy_question_policy": ""}) == "ask"
    assert question_policy_of({"autonomy_question_policy": "  "}) == "ask"
    assert question_policy_of({"autonomy_question_policy": "whatever"}) == "ask"
    assert question_policy_of({"autonomy_question_policy": "SMART"}) == "smart"
    assert question_policy_of({"autonomy_question_policy": "auto"}) == "auto"


def test_settings_select_defaults_to_ask():
    """设置页下拉：第一项必须是 ask，回填与收集的兜底也必须是 ask。"""
    import os
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "web", "settings", "panes-agent.js")
    with open(path, encoding="utf-8") as f:
        src = f.read()
    assert src.index('<option value="ask">') < src.index('<option value="smart">')
    assert "cfg.autonomy_question_policy || 'ask'" in src
    assert "value || 'ask'" in src
