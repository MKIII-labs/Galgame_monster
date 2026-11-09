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


def test_defaults_include_ask_and_notify():
    cfg = default_config()
    assert cfg["question_policy_scope"] == "autopilot"
    assert cfg["notify_banner"] is True
    assert cfg["notify_volume"] == 60
