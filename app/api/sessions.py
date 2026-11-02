# -*- coding: utf-8 -*-
import os
import time
from app.uploads import (_MIME_BY_EXT, _upload_path, delete_session_uploads, drop_upload_files)
from app.store import _lock
from app.export_md import (_md_name, session_markdown)
from app.config import load_config, save_config
from app.sessions import (ensure_session_kb_fields, find_session, invalidate_compression,
                          load_sessions, new_id, reorder_sessions, sandbox_group,
                          save_sessions, set_current, trim_compress_marks)
from app import (gal, saves)

def _api_session_new(h):
        body = h._body()
        gal_scope = "gal" if str(body.get("gal_scope") or "") == "gal" else "main"
        data = load_sessions()
        s = {"id": new_id(),
             "title": str(body.get("title") or ("GAL 剧场" if gal_scope == "gal" else "新对话")).strip()[:60],
             "updated_at": time.time(), "messages": []}
        ensure_session_kb_fields(s)
        s["gal_scope"] = gal_scope
        # SessionStart：钩子可以给新会话注入一段长期上下文（跟着会话保存）
        try:
            from app import hooks as _hooks
            _sctx = _hooks.session_start(s["id"], "")
        except Exception:                    # noqa: BLE001
            _sctx = ""
        if _sctx:
            s["hook_session_context"] = _sctx[:6000]
        if gal_scope == "gal":
            s["gal_enabled"] = True
            s["kb_ids"] = []
            s["gal_use_profile"] = bool(body.get("gal_use_profile"))
            s["gal_use_kb"] = bool(body.get("gal_use_kb"))
            s["gal_use_rules"] = bool(body.get("gal_use_rules"))
        data["sessions"].insert(0, s)
        data["current"] = s["id"]
        with _lock:
            save_sessions(data)
        try:
            saves.add_save(s["id"], s, label="开局", manual=True)   # 留一个「回到开局」的锚点
        except Exception as e:
            print(f"[存档] 开局档失败：{e}")
        h._ok_sessions(data)


def _session_texts(m):
    """一条消息里可搜索的文本：普通正文 + GAL 对白 / 旁白。"""
    out = []
    t = m.get("content")
    if isinstance(t, str) and t.strip():
        out.append(t)
    for seg in (m.get("gal") or {}).get("dialog") or []:
        if not isinstance(seg, dict):
            continue
        for k in ("speech", "narration"):
            v = seg.get(k)
            if isinstance(v, str) and v.strip():
                out.append(v)
    return out


def _api_session_search(h):
        """会话搜索：匹配标题与对话正文，返回命中的会话与片段（q 为空时返回最近会话）。"""
        body = h._body()
        q = str(body.get("q") or "").strip().lower()
        scope = "gal" if str(body.get("scope") or "") == "gal" else "main"
        data = load_sessions()
        items = []
        for s in data.get("sessions") or []:
            if (s.get("gal_scope") or "main") != scope:
                continue
            title = str(s.get("title") or "新对话")
            title_hit = bool(q) and q in title.lower()
            hits = []
            if q:
                for i, m in enumerate(s.get("messages") or []):
                    for text in _session_texts(m):
                        pos = text.lower().find(q)
                        if pos < 0:
                            continue
                        start = max(0, pos - 24)
                        end = min(len(text), pos + len(q) + 40)
                        snippet = " ".join(text[start:end].split())
                        hits.append({
                            "index": i, "role": m.get("role") or "",
                            "snippet": ("…" if start else "") + snippet +
                                       ("…" if end < len(text) else ""),
                        })
                        break
                    if len(hits) >= 3:
                        break
            if not q or title_hit or hits:
                items.append({"id": s.get("id"), "title": title,
                              "updated_at": s.get("updated_at") or 0,
                              "pinned": bool(s.get("pinned")),
                              "msg_count": len(s.get("messages") or []),
                              "title_hit": title_hit, "hits": hits})
        items.sort(key=lambda x: (0 if x.get("pinned") else 1,
                                  -(x.get("updated_at") or 0)))
        h._json({"ok": True, "q": q, "items": items[:30]})


def _api_session_rename(h):
        body = h._body()
        data = load_sessions()
        s = find_session(data, body.get("id"))
        if s and (body.get("title") or "").strip():
            s["title"] = body["title"].strip()
            # 手动改名后不再让「首轮摘要标题」覆盖
            s["title_source"] = "manual"
            with _lock:
                save_sessions(data)
        h._ok_sessions(data)


def _api_session_pin(h):
        body = h._body()
        data = load_sessions()
        s = find_session(data, body.get("id"))
        if s:
            s["pinned"] = bool(body.get("pinned"))
            with _lock:
                save_sessions(data)
        h._ok_sessions(data)


def _api_session_reorder(h):
        """拖拽排序：前端只提交当前分组里的 id，未列出的会话保持原位置。"""
        body = h._body()
        ids = body.get("ids")
        if not isinstance(ids, list) or not ids:
            return h._json({"ok": False, "error": "缺少 ids"}, 400)
        data = load_sessions()
        with _lock:
            err = reorder_sessions(data, ids)
            if err:
                return h._json({"ok": False, "error": err}, 400)
            save_sessions(data)
        h._ok_sessions(data)


def _api_session_group(h):
        """分组级操作。

        pin / rename 只动 config 里的展示元数据（分组是从 sandbox 派生的，
        改显示名不能去改磁盘目录）；delete 才是真的删除组内全部会话。
        """
        body = h._body()
        op = str(body.get("op") or "").strip()
        key = str(body.get("key") or "").strip()[:500]
        cfg = load_config()

        if op == "pin":
            if not key:
                return h._json({"ok": False, "error": "缺少分组 key"}, 400)
            pins = [x for x in (cfg.get("session_group_pins") or []) if x != key]
            if body.get("pinned"):
                pins.append(key)
            cfg["session_group_pins"] = pins
            save_config(cfg)
            return h._json({"ok": True, "group_pins": pins})

        if op == "rename":
            if not key:
                return h._json({"ok": False, "error": "缺少分组 key"}, 400)
            names = dict(cfg.get("session_group_names") or {})
            name = str(body.get("name") or "").strip()[:40]
            if name:
                names[key] = name
            else:
                names.pop(key, None)
            cfg["session_group_names"] = names
            save_config(cfg)
            return h._json({"ok": True, "group_names": names})

        if op == "delete":
            ids = [str(x).strip() for x in (body.get("ids") or []) if str(x).strip()]
            if not ids:
                return h._json({"ok": False, "error": "缺少要删除的会话"}, 400)
            data = load_sessions()
            rows = data.get("sessions") or []
            drop = set(ids)
            removed = [s for s in rows if s.get("id") in drop]
            if not removed:
                return h._json({"ok": False, "error": "没有匹配的会话"}, 400)
            # key 只是保险：传来的会话必须真的属于这个分组，防止前端算错批量误删
            if key and any(sandbox_group(s.get("sandbox"))[0] != key for s in removed):
                return h._json({"ok": False, "error": "会话不属于该分组"}, 400)
            remain = [s for s in rows if s.get("id") not in drop]
            if not remain:
                return h._json({"ok": False, "error": "至少保留一个会话"}, 400)
            data["sessions"] = remain
            if data.get("current") in drop:
                data["current"] = remain[0]["id"]
            with _lock:
                save_sessions(data)
            for sid in drop:
                delete_session_uploads(sid)      # 附件
                saves.drop_saves(sid)            # 存档
            if key:                              # 分组没了，展示元数据一并清掉
                cfg["session_group_pins"] = [x for x in (cfg.get("session_group_pins") or [])
                                             if x != key]
                names = dict(cfg.get("session_group_names") or {})
                names.pop(key, None)
                cfg["session_group_names"] = names
                save_config(cfg)
            return h._ok_sessions(data)

        return h._json({"ok": False, "error": "未知操作：%s" % op}, 400)


def _api_session_delete(h):
        body = h._body()
        data = load_sessions()
        if len(data["sessions"]) <= 1:
            return h._json({"ok": False, "sessions": data})
        drop_id = body.get("id")
        data["sessions"] = [s for s in data["sessions"] if s["id"] != drop_id]
        data["current"] = data["sessions"][0]["id"]
        with _lock:
            save_sessions(data)
        delete_session_uploads(drop_id)      # 连带清理该会话的图片附件
        saves.drop_saves(drop_id)            # 及其存档文件
        h._ok_sessions(data)


def _api_session_select(h):
        body = h._body()
        data = load_sessions()
        sid = str(body.get("id") or "")
        if find_session(data, sid):
            data["current"] = sid
            # 只改侧车文件；不再为一个 current 全量重写 20MB 的 sessions.json
            # （实测每次 230ms，是切会话延迟的一半）。
            set_current(sid)
        h._ok_sessions(data)


def _api_session_messages(h):
        """按需拉取更早的消息（切会话只回最近 MSG_WINDOW 条）。"""
        body = h._body()
        data = load_sessions()
        s = find_session(data, body.get("session_id"))
        if not s:
            return h._json({"ok": False, "error": "not found"}, 404)
        msgs = s.get("messages") or []
        total = len(msgs)
        try:
            before = int(body.get("before", total))
        except (TypeError, ValueError):
            before = total
        try:
            limit = int(body.get("limit", 60))
        except (TypeError, ValueError):
            limit = 60
        before = max(0, min(before, total))
        limit = max(1, min(limit, 200))
        start = max(0, before - limit)
        h._json({"ok": True, "messages": msgs[start:before],
                 "from": start, "before": before, "total": total})


def _api_session_clear(h):
        body = h._body()
        data = load_sessions()
        s = find_session(data, body.get("id"))
        if s:
            drop_upload_files(s.get("id"), s.get("messages"))   # 清理该会话的图片附件
            s["messages"] = []
            s["summary"] = ""
            s["summary_upto"] = 0
            s["compress_marks"] = []
            s["gal_stats"] = []                                  # 舞台状态参数随对话一起清空
            s["gal_bg"] = ""                                     # 手动选定的背景也清掉
            s["gal_bgm"] = ""                                    # BGM 也停掉
            s.pop("plan", None)                                  # 新任务不能沿用旧计划
            s.pop("acceptance", None)                            # 验收项也随任务清空
            s.pop("task_goal", None)                             # 目标钉住也随任务清空
            s.pop("sandbox_warned", None)                        # 沙箱提示允许下次重新检查
            with _lock:
                save_sessions(data)
            try:
                from app.research import clear_ledger
                clear_ledger(s["id"])                            # 研究台账也按会话清空
            except Exception as e:
                print(f"[研究] 清空台账失败：{e}")
        h._ok_sessions(data)


def _api_message_withdraw(h):
        """撤回：默认从指定下标起删除该消息及其后的全部消息；only_this=true 时仅删除这一条"""
        body = h._body()
        data = load_sessions()
        s = find_session(data, body.get("session_id"))
        try:
            idx = int(body.get("index"))
        except (TypeError, ValueError):
            return h._json({"ok": False, "error": "bad index"}, 400)
        if s and 0 <= idx < len(s["messages"]):
            removed = [s["messages"][idx]] if body.get("only_this") else s["messages"][idx:]
            drop_upload_files(s.get("id"), removed)      # 连带删除这些消息的图片附件
            if body.get("only_this"):
                del s["messages"][idx]
            else:
                del s["messages"][idx:]
            _upto = int(s.get("summary_upto") or 0)
            if _upto > 0 and idx < _upto:
                # 删掉的消息在压缩区内：摘要描述的内容已经不存在，直接作废
                invalidate_compression(s)
            elif not body.get("only_this"):
                s["summary_upto"] = min(_upto, idx)
            if body.get("only_this"):
                trim_compress_marks(s, shift_after=idx)       # 后面的标记整体前移
            else:
                trim_compress_marks(s, drop_after=idx)        # 删除点之后的标记丢弃
            s["gal_stats"] = gal.stats_from_messages(s["messages"])   # 状态参数跟着对话一起回滚
            s["updated_at"] = time.time()
            with _lock:
                save_sessions(data)
        h._ok_sessions(data)


def _api_attach_get(h):
        """按需返回聊天图片附件的原始字节"""
        body = h._body()
        p = _upload_path(body.get("session_id"), body.get("name"))
        if not p:
            return h._json({"ok": False, "error": "not found"}, 404)
        try:
            with open(p, "rb") as f:
                data = f.read()
        except OSError:
            return h._json({"ok": False, "error": "read failed"}, 500)
        return h._send(200, data, _MIME_BY_EXT.get(os.path.splitext(p)[1].lower(),
                                                      "application/octet-stream"))


def _api_session_export(h):
        """把某个会话导出成 Markdown 文本"""
        data = load_sessions()
        s = find_session(data, h._body().get("session_id"))
        if not s:
            return h._json({"ok": False, "error": "会话不存在"}, 404)
        h._json({"ok": True, "filename": _md_name(s.get("title"), "conversation") + ".md",
                    "markdown": session_markdown(s)})
