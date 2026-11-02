# -*- coding: utf-8 -*-
"""子代理管理 API（由 app.registry 自注册）。

v1 只读团队：定义保存时会拒绝写 / 执行类工具。
"""
from app import agent_defs
from app.registry import route


def _bad(h, msg, code=400):
    return h._json({"ok": False, "error": msg}, code)


@route("/api/agents")
def _api_agents(h):
    body = h._body()
    op = str(body.get("op") or "list").strip()

    if op == "list":
        return h._json({"ok": True, "agents": agent_defs.list_agents(),
                        "tools": sorted(agent_defs.ALLOWED_TOOLS),
                        "write_tools": sorted(agent_defs.WRITE_TOOLS)})

    if op == "get":
        aid = str(body.get("id") or "").strip()
        try:
            spec = agent_defs.get_agent(aid)
        except agent_defs.AgentDefError as e:
            return _bad(h, str(e))
        if not spec:
            return _bad(h, "找不到子代理：%s" % aid, 404)
        return h._json({"ok": True, "agent": spec})

    if op == "save":
        aid = str(body.get("id") or "").strip()
        text = str(body.get("text") or "")
        try:
            spec = agent_defs.save_agent(
                aid, text, create=bool(body.get("create")),
                old_id=str(body.get("old_id") or ""))
        except agent_defs.AgentDefError as e:
            return _bad(h, str(e))
        except OSError as e:
            return _bad(h, "保存失败：%s" % e, 500)
        return h._json({"ok": True, "agent": spec,
                        "agents": agent_defs.list_agents()})

    if op == "toggle":
        aid = str(body.get("id") or "").strip()
        try:
            spec = agent_defs.set_enabled(aid, bool(body.get("enabled")))
        except agent_defs.AgentDefError as e:
            return _bad(h, str(e))
        except OSError as e:
            return _bad(h, "保存失败：%s" % e, 500)
        return h._json({"ok": True, "agent": spec,
                        "agents": agent_defs.list_agents()})

    if op == "delete":
        aid = str(body.get("id") or "").strip()
        removed = agent_defs.delete_agent(aid)
        return h._json({"ok": True, "removed": bool(removed),
                        "agents": agent_defs.list_agents()})

    return _bad(h, "未知操作：%s" % op)
