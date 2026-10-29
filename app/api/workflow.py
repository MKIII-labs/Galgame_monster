# -*- coding: utf-8 -*-
"""工作流 API（自注册）"""
from app import workflow
from app.registry import route


def _bad(h, msg, code=400):
    return h._json({"ok": False, "error": msg}, code)


@route("/api/work/workflows")
def _api_workflows(h):
    body = h._body()
    op = str(body.get("op") or "list").strip()

    if op == "list":
        return h._json({"ok": True, "workflows": workflow.list_workflows(),
                        "templates": workflow.TEMPLATES,
                        "runs": workflow.list_runs("", 10),
                        "events": list(workflow.EVENT_NAMES)})

    if op == "get":
        wf = workflow.get_workflow(body.get("id"))
        return h._json({"ok": bool(wf), "workflow": wf} if wf else
                       {"ok": False, "error": "找不到工作流"})

    if op == "create":
        try:
            wf = workflow.create_workflow(
                body.get("name"), body.get("trigger_type") or "manual",
                body.get("trigger_at") or "", body.get("trigger_weekdays") or "",
                body.get("trigger_event") or "", body.get("actions") or [],
                enabled=bool(body.get("enabled", True)))
        except ValueError as e:
            return _bad(h, str(e))
        return h._json({"ok": True, "workflow": wf})

    if op == "update":
        wid = str(body.get("id") or "").strip()
        if not wid:
            return _bad(h, "缺少 id")
        fields = {k: v for k, v in body.items() if k not in ("op", "id")}
        wf = workflow.update_workflow(wid, **fields)
        return h._json({"ok": bool(wf), "workflow": wf} if wf else
                       {"ok": False, "error": "找不到工作流"})

    if op == "delete":
        return h._json({"ok": workflow.delete_workflow(body.get("id"))})

    if op == "toggle":
        wf = workflow.set_enabled(body.get("id"), bool(body.get("enabled")))
        return h._json({"ok": bool(wf), "workflow": wf})

    if op == "run":
        rid = workflow.run_workflow(body.get("id"))
        return h._json({"ok": bool(rid), "run_id": rid})

    if op == "runs":
        return h._json({"ok": True, "runs": workflow.list_runs(
            body.get("id") or "", body.get("limit") or 20)})

    return _bad(h, "未知操作：%s" % op)
