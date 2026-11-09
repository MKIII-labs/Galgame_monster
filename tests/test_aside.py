# -*- coding: utf-8 -*-
"""Solo 旁注：消息裁剪、历史保留与并发限制。"""
from app import aside, aside_store
from app.api import aside as aside_api


def test_route_registered():
    from app.registry import snapshot
    import app.api.aside  # noqa: F401  导入即注册
    assert ("POST", "/api/aside/stream") in snapshot()
    assert ("POST", "/api/aside/threads") in snapshot()


def test_empty_payload_returns_empty():
    assert aside.build_messages({}) == []
    assert aside.build_messages({"selection": {"text": "  "}, "question": "?"}) == []
    assert aside.build_messages({"selection": {"text": "x"}, "question": ""}) == []


def test_build_keeps_selection_and_question():
    msgs = aside.build_messages({
        "selection": {"text": "Cache-Control: no-store", "role": "ai",
                      "msg_index": 12, "before": "上文", "after": "下文"},
        "question": "这句是什么意思？",
    })
    assert msgs[0]["role"] == "system"
    assert msgs[1]["role"] == "user"
    body = msgs[1]["content"]
    assert "Cache-Control: no-store" in body
    assert "这句是什么意思？" in body
    assert "#12" in body and "上文" in body and "下文" in body


def test_build_replays_history_then_question():
    msgs = aside.build_messages({
        "selection": {"text": "abc"},
        "question": "第二个问题",
        "history": [{"q": "第一个问题", "a": "第一个回答"}],
    })
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]
    assert "第一个问题" in msgs[1]["content"]
    assert msgs[3]["content"] == "第二个问题"


def test_history_is_capped():
    total = aside.MAX_HISTORY + 3
    history = [{"q": "q%d" % i, "a": "a%d" % i} for i in range(total)]
    msgs = aside.build_messages({"selection": {"text": "abc"},
                                 "question": "现在", "history": history})
    joined = "\n".join(m["content"] for m in msgs)
    assert ("q%d" % (total - 1)) in joined and ("q3" in joined)
    assert "q0" not in joined and "q2" not in joined


def test_code_selection_builds_code_context():
    """编辑器选区：带上文件、行号、语言与前后代码，不混入对话字段。"""
    msgs = aside.build_messages({
        "selection": {
            "kind": "code", "path": "app/demo.py", "language": "python",
            "start_line": 10, "end_line": 12,
            "text": "def f():\n    return 1",
            "before": "import os", "after": "f()",
        },
        "question": "这个函数做什么？",
    })
    body = msgs[1]["content"]
    assert "app/demo.py" in body and "第 10-12 行" in body and "python" in body
    assert "import os" in body and "f()" in body
    assert "【选中的代码】" in body
    assert "【选中片段】" not in body


def test_total_budget_drops_old_history():
    """上下文 + 多轮历史超预算时，从最老的追问开始丢，当前问题必须保留。"""
    history = [{"q": "问题" * 400, "a": "回答" * 1000} for _ in range(4)]
    msgs = aside.build_messages({
        "selection": {"text": "片段" * 1000, "before": "前" * 1200, "after": "后" * 1200},
        "question": "最后的问题",
        "history": history,
    })
    total = sum(len(m["content"]) for m in msgs)
    assert total <= aside.MAX_TOTAL
    assert msgs[-1]["content"] == "最后的问题"


def test_selection_and_context_are_clipped():
    msgs = aside.build_messages({
        "selection": {"text": "x" * 9000, "before": "b" * 9000, "after": "a" * 9000},
        "question": "q",
    })
    body = msgs[1]["content"]
    assert "已截断" in body
    assert len(body) < 9000 + 4000


def test_concurrency_limits():
    aside_api._ACTIVE.clear()
    try:
        assert aside_api._enter("s1") == ""
        assert "已经有一条划词问答" in aside_api._enter("s1")
        aside_api._leave("s1")
        assert aside_api._enter("s1") == ""
    finally:
        aside_api._ACTIVE.clear()


class _FakeHandler:
    """只实现旁注路由用到的 HTTP 接口，便于在单测里跑完整 SSE 分支。"""

    def __init__(self, body):
        import io
        self._b = body
        self.wfile = io.BytesIO()
        self.connection = None
        self.code = 0
        self.json = None

    def _body(self):
        return self._b

    def _json(self, obj, code=200):
        self.json = obj
        self.code = code
        return obj

    def send_response(self, code):
        self.code = code

    def send_header(self, *args):
        pass

    def end_headers(self):
        pass


def test_stream_endpoint_emits_delta_and_done(monkeypatch):
    monkeypatch.setattr(aside_api, "load_config",
                        lambda: {"aside_enabled": True, "aside_max_tokens": 800})
    monkeypatch.setattr(aside_api, "get_api_conf",
                        lambda provider, cfg=None: ("key", "http://x", "m"))

    def fake_stream(api_key, api_url, model, messages, **kw):
        kw["on_visible"]("旁注回答")
        return {"usage": {"total_tokens": 7}}

    monkeypatch.setattr(aside_api.llm, "stream_chat", fake_stream)
    h = _FakeHandler({
        "session_id": "s1", "question": "这是什么",
        "selection": {"text": "Cache-Control", "role": "ai", "msg_index": 1},
    })
    aside_api._api_aside_stream(h)
    body = h.wfile.getvalue().decode("utf-8")
    assert h.code == 200
    assert '"type": "delta"' in body and "旁注回答" in body
    assert '"type": "done"' in body
    assert aside_api._ACTIVE == {}


def test_thread_crud_and_grouping():
    """旁注线程：建 / 读 / 追加问答 / 改名 / 删，按主会话分组。"""
    sel = {"text": "这段代码在做什么", "msg_index": 3, "role": "ai"}
    t = aside_store.create_thread("s1", sel, include_message=True,
                                  message_text="所在消息全文")
    assert t["title"] == "这段代码在做什么"
    assert t["selection"]["msg_index"] == 3
    assert aside_store.get_thread("s1", t["id"])["message_text"] == "所在消息全文"

    aside_store.append_qa("s1", t["id"], "问题一", "回答一")
    got = aside_store.get_thread("s1", t["id"])
    assert got["history"][-1]["q"] == "问题一"
    assert got["history"][-1]["a"] == "回答一"
    assert aside_store.rename_thread("s1", t["id"], "改名后")["title"] == "改名后"

    # 线程只在所属主会话里可见
    assert aside_store.list_threads("s2") == []
    assert aside_store.get_thread("s2", t["id"]) is None

    assert aside_store.delete_thread("s1", t["id"]) is True
    assert aside_store.delete_thread("s1", t["id"]) is False
    assert aside_store.list_threads("s1") == []


def test_thread_history_and_count_are_capped():
    """每个线程只留最近 MAX_HISTORY 轮；每个会话最多 MAX_THREADS 个标签。"""
    t = aside_store.create_thread("s3", {"text": "x"}, title="上限")
    total = aside_store.MAX_HISTORY + 5
    for i in range(total):
        aside_store.append_qa("s3", t["id"], "q%d" % i, "a%d" % i)
    hist = aside_store.get_thread("s3", t["id"])["history"]
    assert len(hist) == aside_store.MAX_HISTORY
    assert hist[-1]["q"] == "q%d" % (total - 1)

    for i in range(aside_store.MAX_THREADS + 3):
        aside_store.create_thread("s4", {"text": "t%d" % i})
    assert len(aside_store.list_threads("s4")) == aside_store.MAX_THREADS


def test_stream_uses_thread_context_and_saves_qa(monkeypatch):
    """带 thread_id 时以服务端线程为准：选区 / 历史从线程读，回答写回线程。"""
    thread = aside_store.create_thread(
        "s5", {"text": "线程里的片段", "role": "ai", "msg_index": 2},
        include_message=True, message_text="线程里的整条消息")
    aside_store.append_qa("s5", thread["id"], "旧问题", "旧回答")

    seen = {}

    def fake_stream(api_key, api_url, model, messages, **kw):
        seen["msgs"] = messages
        kw["on_visible"]("新回答")
        return {"usage": {}}

    monkeypatch.setattr(aside_api, "load_config", lambda: {"aside_enabled": True})
    monkeypatch.setattr(aside_api, "get_api_conf",
                        lambda provider, cfg=None: ("key", "http://x", "m"))
    monkeypatch.setattr(aside_api.llm, "stream_chat", fake_stream)

    h = _FakeHandler({
        "session_id": "s5", "thread_id": thread["id"], "question": "新问题",
        "selection": {"text": "前端传的（应被线程覆盖）"},
    })
    aside_api._api_aside_stream(h)

    first = seen["msgs"][1]["content"]
    assert "线程里的片段" in first
    assert "前端传的" not in first
    assert "线程里的整条消息" in first
    assert "旧问题" in "\n".join(m["content"] for m in seen["msgs"])
    assert seen["msgs"][-1]["content"] == "新问题"

    hist = aside_store.get_thread("s5", thread["id"])["history"]
    assert hist[-1]["q"] == "新问题" and hist[-1]["a"] == "新回答"
    assert h.code == 200


def test_ask_routes_registered():
    from app.registry import snapshot
    import app.api.aside  # noqa: F401  导入即注册
    assert ("POST", "/api/ask/stream") in snapshot()
    assert ("POST", "/api/ask/threads") in snapshot()


def test_build_messages_notes_source():
    msgs = aside.build_messages({
        "source": "notes",
        "title": "笔记A",
        "selection": {"text": "引用片段", "prefix": "前文", "suffix": "后文"},
        "question": "这段什么意思？",
    })
    assert "笔记助手" in msgs[0]["content"]
    body = msgs[1]["content"]
    assert "笔记A" in body and "前文" in body and "后文" in body and "引用片段" in body
    assert "这段什么意思？" in body


def test_build_messages_reader_source():
    msgs = aside.build_messages({
        "source": "reader",
        "title": "论文B",
        "selection": {"text": "选中公式", "context": "段落原文", "page": 3},
        "question": "解释一下",
    })
    assert "论文阅读助手" in msgs[0]["content"]
    body = msgs[1]["content"]
    assert "论文B" in body and "第 3 页" in body and "段落原文" in body and "选中公式" in body


def test_doc_scope_threads_are_isolated():
    t = aside_store.create_thread("doc:notes:a.md", {"text": "片段"},
                                  source="notes", anchor="note:a:1")
    assert [x["id"] for x in aside_store.list_threads("doc:notes:a.md")] == [t["id"]]
    assert aside_store.list_threads("session:s1") == []

    # 旧调用（裸 sid）按 session 作用域
    aside_store.create_thread("s1", {"text": "x"})
    assert len(aside_store.list_threads("session:s1")) == 1
    assert len(aside_store.list_threads("s1")) == 1
    assert aside_store.get_thread("doc:notes:a.md", t["id"])["source"] == "notes"


