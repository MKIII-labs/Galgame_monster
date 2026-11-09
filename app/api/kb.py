# -*- coding: utf-8 -*-
import json
import re
import time
from app.context import (begin_compress, compress_busy, compress_history,
                         current_context, end_compress,
                         flush_summary_memory, keep_from_by_tokens, last_input_detail,
                         manual_keep_budget, restore_summary, save_summary, summary_info)
from app.store import _lock
from app.knowledge import (MAX_KB_PROMPT, delete_kb, edge_is_valid, is_profile_kb, load_kb,
                           load_kb_index, new_kb, save_kb, set_kb_write)
from app.sessions import (ensure_session_kb_fields, find_session, load_sessions, save_sessions, slim_sessions)
from app.config import get_api_conf
from app import mem_tool

def _api_kb_list(h):
        h._json({"ok": True, "kbs": load_kb_index()["kbs"]})


def _api_kb_nodes(h):
        """扁平列出所有知识库条目（输入框 #doc 菜单用），只回定位信息。"""
        body = h._body()
        q = str(body.get("q") or "").strip().lower()
        out = []
        for item in load_kb_index().get("kbs") or []:
            kb = load_kb(item.get("id"))
            if not kb:
                continue
            for n in kb.get("nodes") or []:
                title = str(n.get("title") or "").strip()
                if not title:
                    continue
                cat = str(n.get("category") or "")
                if q and q not in title.lower() and q not in cat.lower():
                    continue
                out.append({"kb": item.get("id"), "node": n.get("id"),
                            "title": title, "category": cat})
                if len(out) >= 200:
                    return h._json({"ok": True, "items": out})
        return h._json({"ok": True, "items": out})


def _api_kb_get(h):
        kb = load_kb(h._body().get("id"))
        if not kb:
            return h._json({"ok": False, "error": "not found"}, 404)
        h._json({"ok": True, "kb": kb})


def _api_kb_create(h):
        kb = new_kb(h._body().get("name"))
        h._json({"ok": True, "kb": kb, "kbs": load_kb_index()["kbs"]})


def _api_kb_delete(h):
        ok = delete_kb(h._body().get("id"))
        h._json({"ok": ok, "error": "" if ok else "系统库不可删除",
                    "kbs": load_kb_index()["kbs"]})


def _api_kb_save(h):
        body = h._body()
        kb = load_kb(body.get("id"))
        if not kb:
            return h._json({"ok": False, "error": "not found"}, 404)
        name = body.get("name")
        if isinstance(name, str) and name.strip() and name.strip() != kb.get("name"):
            if is_profile_kb(body.get("id")):
                return h._json({"ok": False, "error": "系统库不可重命名"}, 400)
            kb["name"] = name.strip()
        if isinstance(body.get("nodes"), list):
            kb["nodes"] = body["nodes"]
        if isinstance(body.get("edges"), list):
            kb["edges"] = body["edges"]
        if isinstance(body.get("graph"), dict):
            kb["graph"] = body["graph"]
        if isinstance(body.get("prompt"), str):
            kb["prompt"] = body["prompt"].strip()[:MAX_KB_PROMPT]
        if isinstance(body.get("families"), list):
            kb["families"] = body["families"]
        if body.get("layout_mode") in ("stable", "physics"):
            kb["layout_mode"] = body["layout_mode"]
        lv = body.get("layout_version")
        if isinstance(lv, int) and not isinstance(lv, bool):
            kb["layout_version"] = max(1, min(lv, 99))
        save_kb(kb)
        h._json({"ok": True, "kb": kb, "kbs": load_kb_index()["kbs"]})


def _api_kb_write(h):
        body = h._body()
        idx = set_kb_write(body.get("id"), body.get("ai_write"))
        h._json({"ok": True, "kbs": idx["kbs"]})


def _yaml_str(raw):
        """YAML 双引号标量：用 JSON 转义即可（合法 YAML，且不怕标题里有冒号 / 引号）"""
        return json.dumps(str(raw or ""), ensure_ascii=False)


def _md_title(node):
        return str(node.get("title") or "").strip() or "(无标题)"


def _kb_markdown(kb):
        """节点 + 关系拼成 Markdown：YAML frontmatter + 节点正文 + 链接 + 事实清单。

        - 节点内只放「链接」（`[[标题]]`），不重复陈述；
        - 末尾「事实」区把每条**有效**边写一遍：`[[A]] --因果--> [[B]]：一句话依据`，
          这样导出的 md 本身就是一份可检索的事实列表（失效边只留在图谱历史里）。
        """
        nodes = [n for n in (kb.get("nodes") or []) if isinstance(n, dict)]
        by_id = {n.get("id"): n for n in nodes}
        adj, facts = {}, []
        for e in (kb.get("edges") or []):
            if not isinstance(e, dict) or not edge_is_valid(e):
                continue          # 失效边只留作图谱历史，不进导出
            a, b = e.get("from"), e.get("to")
            label = e.get("label") or e.get("relation") or "相关"
            fact = str(e.get("fact") or "").strip()
            if a in by_id and b in by_id:
                directed = bool(e.get("directed"))
                adj.setdefault(a, []).append((label, by_id[b], directed, True))
                if not directed:
                    adj.setdefault(b, []).append((label, by_id[a], directed, False))
                facts.append((by_id[a], label, by_id[b], directed, fact))
        name = kb.get("name") or "知识库"
        lines = ["---",
                 "title: " + _yaml_str(name)]
        if kb.get("id"):
            lines.append("id: " + _yaml_str(kb.get("id")))
        lines += ["nodes: %d" % len(nodes),
                  "relations: %d" % len(facts),
                  "exported: " + time.strftime("%Y-%m-%d %H:%M:%S"),
                  "generator: Galgame_monster"]
        tags = sorted({str(t).strip() for n in nodes for t in (n.get("tags") or [])
                       if str(t).strip()})
        if tags:
            lines.append("tags: [" + ", ".join(_yaml_str(t) for t in tags) + "]")
        lines += ["---", "", "# " + name, ""]
        for n in nodes:
            lines.append("## " + _md_title(n))
            meta = []
            if n.get("category"):
                meta.append("分类：" + str(n["category"]))
            if n.get("tags"):
                meta.append("标签：" + "、".join(str(t) for t in n["tags"]))
            if n.get("aliases"):
                meta.append("别名：" + "、".join(str(t) for t in n["aliases"]))
            if n.get("id"):
                meta.append("id：" + str(n["id"]))
            if meta:
                lines += ["", "> " + " · ".join(meta)]
            body = str(n.get("content") or "").strip()
            if body:
                lines += ["", body]
            notes = [x for x in (n.get("notes") or []) if isinstance(x, dict)]
            if notes:
                lines += ["", "**补充**"]
                for note in notes:
                    ts = note.get("created_at")
                    when = time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else ""
                    text = str(note.get("content") or "").strip()
                    if text:
                        lines.append("- " + (("[" + when + "] ") if when else "") + text)
            rels = adj.get(n.get("id")) or []
            if rels:
                lines += ["", "**关联**"]
                for label, other, directed, out in rels:
                    arrow = ("→" if out else "←") if directed else "⇄"
                    lines.append("- %s %s [[%s]]" % (label, arrow, _md_title(other)))
            lines.append("")
        if facts:
            lines += ["## 事实", ""]
            for a, label, b, directed, fact in facts:
                head = ("[[%s]] --%s--> [[%s]]" % (_md_title(a), label, _md_title(b))
                        if directed else
                        "[[%s]] ⇄ [[%s]]（%s）" % (_md_title(a), _md_title(b), label))
                lines.append("- " + head + (("：" + fact) if fact else ""))
            lines.append("")
        return "\n".join(lines)


def _api_kb_export(h):
        """导出当前库（或本会话记忆）为 Markdown / JSON，前端用 Blob 下载。"""
        body = h._body()
        fmt = str(body.get("format") or "markdown").lower()
        kb_id = body.get("id")
        if kb_id == "session":
            data = load_sessions()
            s = find_session(data, body.get("session_id") or data.get("current"))
            if not s:
                return h._json({"ok": False, "error": "no session"}, 404)
            mem = s.get("memory") or {}
            kb = {"name": "会话记忆", "nodes": mem.get("nodes") or [],
                  "edges": mem.get("edges") or []}
        else:
            kb = load_kb(kb_id)
        if not kb:
            return h._json({"ok": False, "error": "not found"}, 404)
        safe = re.sub(r'[\\/:*?"<>|]+', "_", kb.get("name") or "知识库")[:40]
        if fmt == "json":
            return h._json({"ok": True, "filename": safe + ".json",
                            "content": json.dumps(kb, ensure_ascii=False, indent=2),
                            "mime": "application/json"})
        return h._json({"ok": True, "filename": safe + ".md",
                        "content": _kb_markdown(kb), "mime": "text/markdown"})


def _api_memory_get(h):
        body = h._body()
        data = load_sessions()
        s = find_session(data, body.get("session_id"))
        if not s:
            return h._json({"ok": False, "error": "no session"}, 404)
        ensure_session_kb_fields(s)
        h._json({"ok": True, "memory": s["memory"], "kb_ids": s["kb_ids"],
                    "ai_write": s["ai_write"]})


def _api_memory_save(h):
        body = h._body()
        data = load_sessions()
        s = find_session(data, body.get("session_id"))
        if not s:
            return h._json({"ok": False, "error": "no session"}, 404)
        ensure_session_kb_fields(s)
        mem = body.get("memory")
        if isinstance(mem, dict):
            if isinstance(mem.get("nodes"), list):
                s["memory"]["nodes"] = mem["nodes"]
            if isinstance(mem.get("edges"), list):
                s["memory"]["edges"] = mem["edges"]
            if isinstance(mem.get("graph"), dict):
                s["memory"]["graph"] = mem["graph"]
            if isinstance(mem.get("families"), list):
                s["memory"]["families"] = mem["families"]
            if mem.get("layout_mode") in ("stable", "physics"):
                s["memory"]["layout_mode"] = mem["layout_mode"]
            lv = mem.get("layout_version")
            if isinstance(lv, int) and not isinstance(lv, bool):
                s["memory"]["layout_version"] = max(1, min(lv, 99))
        s["updated_at"] = time.time()
        with _lock:
            save_sessions(data)
        h._json({"ok": True, "memory": s["memory"]})


def _api_memory_undo(h):
        body = h._body()
        ok = mem_tool.undo(body.get("token"), body.get("session_id"), body.get("index"))
        data = load_sessions()
        h._json({"ok": ok, "sessions": slim_sessions(data),
                    "context": current_context(data)})


def _api_context_compress(h):
        """手动压缩：把较早的对话总结成摘要（与自动压缩同一套逻辑）"""
        body = h._body()
        data = load_sessions()
        s = find_session(data, body.get("session_id") or data.get("current"))
        if not s:
            return h._json({"ok": False, "message": "会话不存在"}, 404)
        if compress_busy(s["id"]):
            return h._json({"ok": False, "message": "正在后台压缩，请稍后再试"})

        def reply(ok, message):
            h._json({"ok": ok, "message": message,
                        "sessions": slim_sessions(data), "context": current_context(data)})

        # 生成中的会话不压缩：压缩用开始时的快照，生成结束落库会把它覆盖掉。
        # 用 stream_alive（看最近一次 SSE 事件）而不是只看 ACTIVE_STREAMS，
        # 否则崩溃 / 断线留下的残条目会让按钮永远停在「正在生成」。
        try:
            from app.api.chat import stream_alive
            if stream_alive(s["id"]):
                return reply(False, "该会话正在生成回复，请等这一轮结束后再压缩")
        except Exception:                    # noqa: BLE001  状态探测失败不阻断压缩
            pass

        msgs = s.get("messages") or []
        cut = int(s.get("summary_upto") or 0)
        _input_window = manual_keep_budget(msgs, cut)
        keep_from = keep_from_by_tokens(msgs, cut, _input_window)
        if keep_from <= cut:
            return reply(False, "可压缩的历史太少（需要更多较早的消息才能生成摘要，当前保留最近的部分）")
        if not get_api_conf("chat")[0]:
            return reply(False, "尚未配置聊天 API 密钥，无法生成摘要")
        # 与后台预压缩共用登记表：手动压缩期间到达的聊天请求会被转成排队
        if not begin_compress(s["id"]):
            return reply(False, "正在压缩，请稍后再试")
        try:
            if compress_history(s):
                if save_summary(s["id"], s):
                    flush_summary_memory(s["id"])
                    return reply(True, "已压缩较早的历史对话")
                return reply(False, "压缩期间历史发生了变化，本次摘要已放弃，请重试")
            return reply(False, "压缩失败，请稍后重试")
        finally:
            end_compress(s["id"])


def _api_context_summary(h):
        """压缩详情：摘要正文、压缩点、条数、时间，以及本次压缩写入的记忆条数"""
        body = h._body()
        data = load_sessions()
        sid = body.get("session_id") or data.get("current")
        s = find_session(data, sid)
        if not s:
            return h._json({"ok": False, "message": "会话不存在"}, 404)
        info = summary_info(s)
        info.update({"busy": compress_busy(s["id"]),
                     "context": current_context(data),
                     "input": last_input_detail(s)})
        h._json(dict({"ok": True}, **info))


def _api_context_restore(h):
        """撤销压缩：清空摘要与压缩点，让原文重新进入上下文"""
        body = h._body()
        data = load_sessions()
        sid = body.get("session_id") or data.get("current")
        s = find_session(data, sid)
        if not s:
            return h._json({"ok": False, "message": "会话不存在"}, 404)
        n = restore_summary(s)
        if n:
            s["updated_at"] = time.time()
            with _lock:
                save_sessions(data)
        h._json({"ok": bool(n), "restored": n,
                 "message": ("已恢复 %d 条原文" % n) if n else "当前没有可恢复的压缩记录",
                 "sessions": slim_sessions(data), "context": current_context(data)})


def _api_session_bind(h):
        body = h._body()
        data = load_sessions()
        s = find_session(data, body.get("id"))
        if not s:
            return h._json({"ok": False, "error": "no session"}, 404)
        ensure_session_kb_fields(s)
        if isinstance(body.get("kb_ids"), list):
            s["kb_ids"] = [k for k in body["kb_ids"] if isinstance(k, str)]
        if "ai_write" in body:
            s["ai_write"] = bool(body.get("ai_write"))
        if "use_profile" in body:        # 本会话是否读取用户画像（默认开）
            s["use_profile"] = bool(body.get("use_profile"))
        if "use_memory" in body:         # 本会话是否读取会话记忆 / 绑定知识库（默认开）
            s["use_memory"] = bool(body.get("use_memory"))
        s["updated_at"] = time.time()
        with _lock:
            save_sessions(data)
        h._ok_sessions(data)
