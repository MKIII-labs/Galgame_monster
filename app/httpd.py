# -*- coding: utf-8 -*-
"""HTTP 处理：静态资源、REST API 与聊天流式(SSE)"""
import base64
import json
import mimetypes
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import requests

from app.store import WEB_DIR, ACTIVE_STREAMS, _lock
from app.config import (load_config, save_config, get_api_conf, get_api_protocol,
                        resolve_endpoint, normalize_content, thinking_payload,
                        effective_think_mode)
from app.profile import load_profile, save_profile
from app.sessions import (load_sessions, save_sessions, find_session, slim_sessions,
                          ensure_session_kb_fields, new_id)
from app.context import (context_limits, session_context, current_context,
                         build_context_messages, compress_history, COMPRESS_KEEP)
from app.usage import record_usage, aggregate_usage
from app.search import (get_search_conf, build_search_context, SEARCH_PROVIDERS,
                        rewrite_queries, search_multi, fetch_pages, slim_items)
from app.learning import load_builtin_learning_presets, BUILTIN_PRESET_LABEL
from app.imaging import recognize_image
from app.balance import query_balance
from app.knowledge import (load_kb_index, load_kb, save_kb, new_kb, delete_kb, set_kb_write,
                           is_profile_kb, retrieve_memory, migrate_profile_to_kb,
                           relations_payload)
from app import mem_tool
from app import notes as noteslib

TOKEN = secrets.token_urlsafe(24)

# ============================================================
# HTTP 处理
# ============================================================

class Handler(BaseHTTPRequestHandler):
    server_version = "Galgame_monster/2.0"

    def log_message(self, *args):
        pass  # 静音访问日志

    # ---------- 基础工具 ----------
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    def _authed(self):
        return self.headers.get("X-Auth-Token") == TOKEN

    def _host_ok(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    # ---------- 路由 ----------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/" or path == "/index.html":
            return self._static("index.html")
        if path.startswith("/static/"):
            return self._static(path[len("/static/"):])
        if path.startswith("/api/"):
            if not self._host_ok() or not self._authed():
                return self._json({"error": "unauthorized"}, 403)
            if path == "/api/state":
                return self._api_state()
            if path == "/api/usage":
                return self._api_usage(parse_qs(parsed.query))
            return self._json({"error": "not found"}, 404)
        self._send(404, "not found", "text/plain; charset=utf-8")

    def _api_usage(self, qs):
        mode = (qs.get("mode", ["day"])[0] or "day").strip()
        if mode not in ("day", "month"):
            mode = "day"
        try:
            span = int(qs.get("span", ["30"])[0])
        except (TypeError, ValueError):
            span = 30
        span = max(1, min(span, 366))
        self._json(aggregate_usage(mode, span))

    def do_POST(self):
        path = urlparse(self.path).path
        if not path.startswith("/api/"):
            return self._send(404, "not found", "text/plain; charset=utf-8")
        if not self._host_ok() or not self._authed():
            return self._json({"error": "unauthorized"}, 403)
        routes = {
            "/api/chat": self._api_chat,
            "/api/stop": self._api_stop,
            "/api/recognize": self._api_recognize,
            "/api/session/new": self._api_session_new,
            "/api/session/rename": self._api_session_rename,
            "/api/session/delete": self._api_session_delete,
            "/api/session/select": self._api_session_select,
            "/api/session/clear": self._api_session_clear,
            "/api/config": self._api_save_config,
            "/api/profile": self._api_save_profile,
            "/api/message/withdraw": self._api_message_withdraw,
            "/api/kb/list": self._api_kb_list,
            "/api/kb/get": self._api_kb_get,
            "/api/kb/create": self._api_kb_create,
            "/api/kb/delete": self._api_kb_delete,
            "/api/kb/save": self._api_kb_save,
            "/api/kb/write": self._api_kb_write,
            "/api/memory/get": self._api_memory_get,
            "/api/memory/save": self._api_memory_save,
            "/api/memory/undo": self._api_memory_undo,
            "/api/context/compress": self._api_context_compress,
            "/api/session/bind": self._api_session_bind,
            "/api/balance": self._api_balance,
            "/api/notes/tree": self._api_notes_tree,
            "/api/notes/get": self._api_notes_get,
            "/api/notes/save": self._api_notes_save,
            "/api/notes/new": self._api_notes_new,
            "/api/notes/delete": self._api_notes_delete,
            "/api/notes/rename": self._api_notes_rename,
            "/api/notes/folder/new": self._api_notes_folder_new,
            "/api/notes/folder/delete": self._api_notes_folder_delete,
            "/api/notes/graph": self._api_notes_graph,
            "/api/notes/backlinks": self._api_notes_backlinks,
            "/api/notes/search": self._api_notes_search,
        }
        fn = routes.get(path)
        if not fn:
            return self._json({"error": "not found"}, 404)
        try:
            fn()
        except Exception as e:
            print(f"[服务] {path} 出错：{e}")
            self._json({"error": str(e)}, 500)

    # ---------- 静态资源 ----------
    def _static(self, rel):
        if not (self._host_ok()):
            return self._send(403, "forbidden", "text/plain; charset=utf-8")
        path = os.path.normpath(os.path.join(WEB_DIR, rel))
        if not path.startswith(WEB_DIR) or not os.path.isfile(path):
            return self._send(404, "not found", "text/plain; charset=utf-8")
        with open(path, "rb") as f:
            data = f.read()
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if path.endswith(".html"):
            data = data.replace(b"%%TOKEN%%", TOKEN.encode())
            ctype = "text/html; charset=utf-8"
        elif path.endswith(".css"):
            ctype = "text/css; charset=utf-8"
        elif path.endswith(".js"):
            ctype = "application/javascript; charset=utf-8"
        self._send(200, data, ctype)

    # ---------- API ----------
    def _api_state(self):
        data = load_sessions()
        self._json({
            "config": load_config(),
            "profile": load_profile(),
            "sessions": slim_sessions(data),
            "context": current_context(data),
            "kbs": load_kb_index()["kbs"],
            "builtin_presets": load_builtin_learning_presets(),
            "builtin_preset_label": BUILTIN_PRESET_LABEL,
            "search_providers": SEARCH_PROVIDERS,
            "relations": relations_payload(),
        })

    def _ok_sessions(self, data):
        """统一返回精简后的会话数据（只带当前会话的消息）与上下文占用"""
        self._json({"ok": True, "sessions": slim_sessions(data),
                    "context": current_context(data)})

    def _api_save_config(self):
        incoming = self._body()
        cfg = load_config()
        cfg.update(incoming)
        with _lock:
            save_config(cfg)
        self._json({"ok": True})

    def _api_save_profile(self):
        incoming = self._body()
        profile = load_profile()
        for k in ("manual", "auto", "auto_learn_enabled", "learning_prompt_custom"):
            if k in incoming:
                profile[k] = incoming[k]
        with _lock:
            save_profile(profile)
        self._json({"ok": True})

    def _api_session_new(self):
        data = load_sessions()
        s = {"id": new_id(), "title": "新对话", "updated_at": time.time(), "messages": []}
        data["sessions"].insert(0, s)
        data["current"] = s["id"]
        with _lock:
            save_sessions(data)
        self._ok_sessions(data)

    def _api_session_rename(self):
        body = self._body()
        data = load_sessions()
        s = find_session(data, body.get("id"))
        if s and (body.get("title") or "").strip():
            s["title"] = body["title"].strip()
            with _lock:
                save_sessions(data)
        self._ok_sessions(data)

    def _api_session_delete(self):
        body = self._body()
        data = load_sessions()
        if len(data["sessions"]) <= 1:
            return self._json({"ok": False, "sessions": data})
        data["sessions"] = [s for s in data["sessions"] if s["id"] != body.get("id")]
        data["current"] = data["sessions"][0]["id"]
        with _lock:
            save_sessions(data)
        self._ok_sessions(data)

    def _api_session_select(self):
        body = self._body()
        data = load_sessions()
        if find_session(data, body.get("id")):
            data["current"] = body["id"]
            with _lock:
                save_sessions(data)
        self._ok_sessions(data)

    def _api_session_clear(self):
        body = self._body()
        data = load_sessions()
        s = find_session(data, body.get("id"))
        if s:
            s["messages"] = []
            s["summary"] = ""
            s["summary_upto"] = 0
            with _lock:
                save_sessions(data)
        self._ok_sessions(data)

    def _api_message_withdraw(self):
        """撤回：从指定下标起删除该用户消息及其后的全部消息"""
        body = self._body()
        data = load_sessions()
        s = find_session(data, body.get("session_id"))
        try:
            idx = int(body.get("index"))
        except (TypeError, ValueError):
            return self._json({"ok": False, "error": "bad index"}, 400)
        if s and 0 <= idx < len(s["messages"]):
            del s["messages"][idx:]
            s["summary_upto"] = max(0, min(int(s.get("summary_upto") or 0), len(s["messages"])))
            s["updated_at"] = time.time()
            with _lock:
                save_sessions(data)
        self._ok_sessions(data)

    # ---------- 知识库 / 记忆 ----------
    def _api_kb_list(self):
        self._json({"ok": True, "kbs": load_kb_index()["kbs"]})

    def _api_kb_get(self):
        kb = load_kb(self._body().get("id"))
        if not kb:
            return self._json({"ok": False, "error": "not found"}, 404)
        self._json({"ok": True, "kb": kb})

    def _api_kb_create(self):
        kb = new_kb(self._body().get("name"))
        self._json({"ok": True, "kb": kb, "kbs": load_kb_index()["kbs"]})

    def _api_kb_delete(self):
        ok = delete_kb(self._body().get("id"))
        self._json({"ok": ok, "error": "" if ok else "系统库不可删除",
                    "kbs": load_kb_index()["kbs"]})

    def _api_kb_save(self):
        body = self._body()
        kb = load_kb(body.get("id"))
        if not kb:
            return self._json({"ok": False, "error": "not found"}, 404)
        name = body.get("name")
        if isinstance(name, str) and name.strip() and name.strip() != kb.get("name"):
            if is_profile_kb(body.get("id")):
                return self._json({"ok": False, "error": "系统库不可重命名"}, 400)
            kb["name"] = name.strip()
        if isinstance(body.get("nodes"), list):
            kb["nodes"] = body["nodes"]
        if isinstance(body.get("edges"), list):
            kb["edges"] = body["edges"]
        save_kb(kb)
        self._json({"ok": True, "kb": kb, "kbs": load_kb_index()["kbs"]})

    def _api_kb_write(self):
        body = self._body()
        idx = set_kb_write(body.get("id"), body.get("ai_write"))
        self._json({"ok": True, "kbs": idx["kbs"]})

    # ---------- 内置笔记库（Obsidian 核心版） ----------

    def _notes_state(self):
        """树 + 图谱一起回，前端一次刷新到位"""
        return {"ok": True, "tree": noteslib.list_tree(), "graph": noteslib.graph()}

    def _api_notes_tree(self):
        self._json(self._notes_state())

    def _api_notes_get(self):
        rel = self._body().get("path")
        content = noteslib.read_note(rel)
        if content is None:
            return self._json({"ok": False, "error": "笔记不存在"}, 404)
        self._json({"ok": True, "path": rel, "content": content,
                    "backlinks": noteslib.backlinks(rel)})

    def _api_notes_save(self):
        body = self._body()
        rel = body.get("path")
        if not noteslib.write_note(rel, body.get("content")):
            return self._json({"ok": False, "error": "保存失败（路径非法）"}, 400)
        self._json({"ok": True, "backlinks": noteslib.backlinks(rel),
                    "graph": noteslib.graph()})

    def _api_notes_new(self):
        ok, info = noteslib.create_note(self._body().get("path"))
        if not ok:
            return self._json({"ok": False, "error": "已存在", "path": info}, 400)
        self._json({"ok": True, "path": info, "content": "", "backlinks": [],
                    "tree": noteslib.list_tree(), "graph": noteslib.graph()})

    def _api_notes_delete(self):
        if not noteslib.delete_note(self._body().get("path")):
            return self._json({"ok": False, "error": "删除失败"}, 400)
        self._json(self._notes_state())

    def _api_notes_rename(self):
        body = self._body()
        ok, info = noteslib.rename_path(body.get("path"), body.get("name"))
        if not ok:
            return self._json({"ok": False, "error": info}, 400)
        self._json({"ok": True, "path": info, "tree": noteslib.list_tree(),
                    "graph": noteslib.graph()})

    def _api_notes_folder_new(self):
        ok, info = noteslib.create_folder(self._body().get("path"))
        if not ok:
            return self._json({"ok": False, "error": info}, 400)
        self._json(self._notes_state())

    def _api_notes_folder_delete(self):
        if not noteslib.delete_folder(self._body().get("path")):
            return self._json({"ok": False, "error": "删除失败"}, 400)
        self._json(self._notes_state())

    def _api_notes_graph(self):
        self._json({"ok": True, "graph": noteslib.graph()})

    def _api_notes_backlinks(self):
        self._json({"ok": True, "backlinks": noteslib.backlinks(self._body().get("path"))})

    def _api_notes_search(self):
        self._json({"ok": True, "hits": noteslib.search(self._body().get("q"))})

    def _api_balance(self):
        self._json(query_balance(self._body().get("provider") or "chat"))

    def _api_memory_get(self):
        body = self._body()
        data = load_sessions()
        s = find_session(data, body.get("session_id"))
        if not s:
            return self._json({"ok": False, "error": "no session"}, 404)
        ensure_session_kb_fields(s)
        self._json({"ok": True, "memory": s["memory"], "kb_ids": s["kb_ids"],
                    "ai_write": s["ai_write"]})

    def _api_memory_undo(self):
        body = self._body()
        ok = mem_tool.undo(body.get("token"), body.get("session_id"), body.get("index"))
        data = load_sessions()
        self._json({"ok": ok, "sessions": slim_sessions(data),
                    "context": current_context(data)})

    def _api_context_compress(self):
        """手动压缩：把较早的对话总结成摘要（与自动压缩同一套逻辑）"""
        body = self._body()
        data = load_sessions()
        s = find_session(data, body.get("session_id") or data.get("current"))
        if not s:
            return self._json({"ok": False, "message": "会话不存在"}, 404)

        def reply(ok, message):
            self._json({"ok": ok, "message": message,
                        "sessions": slim_sessions(data), "context": current_context(data)})

        msgs = s.get("messages") or []
        cut = int(s.get("summary_upto") or 0)
        if len(msgs) - COMPRESS_KEEP <= cut:
            return reply(False, "可压缩的历史太少（压缩点之后至少需要 %d 条消息，当前 %d 条）"
                               % (COMPRESS_KEEP + 1, len(msgs) - cut))
        if not get_api_conf("chat")[0]:
            return reply(False, "尚未配置聊天 API 密钥，无法生成摘要")
        if compress_history(s):
            s["updated_at"] = time.time()
            with _lock:
                save_sessions(data)
            return reply(True, "已压缩较早的历史对话")
        return reply(False, "压缩失败，请稍后重试")

    def _api_memory_save(self):
        body = self._body()
        data = load_sessions()
        s = find_session(data, body.get("session_id"))
        if not s:
            return self._json({"ok": False, "error": "no session"}, 404)
        ensure_session_kb_fields(s)
        mem = body.get("memory")
        if isinstance(mem, dict):
            if isinstance(mem.get("nodes"), list):
                s["memory"]["nodes"] = mem["nodes"]
            if isinstance(mem.get("edges"), list):
                s["memory"]["edges"] = mem["edges"]
        s["updated_at"] = time.time()
        with _lock:
            save_sessions(data)
        self._json({"ok": True, "memory": s["memory"]})

    def _api_session_bind(self):
        body = self._body()
        data = load_sessions()
        s = find_session(data, body.get("id"))
        if not s:
            return self._json({"ok": False, "error": "no session"}, 404)
        ensure_session_kb_fields(s)
        if isinstance(body.get("kb_ids"), list):
            s["kb_ids"] = [k for k in body["kb_ids"] if isinstance(k, str)]
        if "ai_write" in body:
            s["ai_write"] = bool(body.get("ai_write"))
        s["updated_at"] = time.time()
        with _lock:
            save_sessions(data)
        self._ok_sessions(data)

    def _api_stop(self):
        body = self._body()
        ev = ACTIVE_STREAMS.get(body.get("session_id"))
        if ev:
            ev.set()
        self._json({"ok": True})

    def _api_recognize(self):
        body = self._body()
        b64 = body.get("data") or ""
        mime = body.get("mime") or "image/png"
        if not b64:
            return self._json({"ok": False, "text": "⚠️ 未收到图片数据"})
        text = recognize_image(mime, b64)
        data = load_sessions()
        s = find_session(data, body.get("session_id"))
        if s and text and not text.startswith(("❌", "⏰", "⚠️")):
            s["messages"].append({"role": "assistant",
                                  "content": f"用户上传了一张图片，图片内容为：\n{text}"})
            s["updated_at"] = time.time()
            with _lock:
                save_sessions(data)
        self._json({"ok": True, "text": text, "sessions": slim_sessions(data),
                    "context": current_context(data)})

    def _api_chat(self):
        body = self._body()
        sid = body.get("session_id")
        content = (body.get("content") or "").strip()
        resend = body.get("resend") if isinstance(body.get("resend"), dict) else None
        if not content and not resend:
            return self._json({"ok": False, "error": "empty"}, 400)

        data = load_sessions()
        s = find_session(data, sid)
        if not s:
            return self._json({"ok": False, "error": "no session"}, 404)

        if resend:
            mode = resend.get("mode")
            try:
                idx = int(resend.get("index"))
            except (TypeError, ValueError):
                return self._json({"ok": False, "error": "bad index"}, 400)
            msgs = s["messages"]
            if mode == "edit":
                # 修改用户消息：新文本作为一个新「版本」记下；界面与 AI 都只使用当前版本
                if not (0 <= idx < len(msgs)) or msgs[idx].get("role") != "user" or not content:
                    return self._json({"ok": False, "error": "bad edit target"}, 400)
                m = msgs[idx]
                vers = m.get("versions") or [m.get("content") or ""]
                if vers[-1] != content:
                    vers.append(content)
                m["versions"] = vers
                m["vidx"] = len(vers) - 1
                m["content"] = content
                del msgs[idx + 1:]
            elif mode == "version":
                # 切换该用户消息的历史版本（前端 ‹ n/N › 箭头）→ 用所选版本重新生成
                if not (0 <= idx < len(msgs)) or msgs[idx].get("role") != "user":
                    return self._json({"ok": False, "error": "bad version target"}, 400)
                m = msgs[idx]
                vers = m.get("versions")
                try:
                    v = int(resend.get("vidx"))
                except (TypeError, ValueError):
                    return self._json({"ok": False, "error": "bad vidx"}, 400)
                if not isinstance(vers, list) or not vers or not (0 <= v < len(vers)):
                    return self._json({"ok": False, "error": "bad vidx"}, 400)
                m["vidx"] = v
                m["content"] = vers[v]
                content = vers[v]
                del msgs[idx + 1:]
            elif mode == "regenerate":
                # 重新发言：丢弃该条 AI 回复及其后消息，用上一条用户消息重发
                if not (0 <= idx < len(msgs)) or msgs[idx].get("role") != "assistant":
                    return self._json({"ok": False, "error": "bad regenerate target"}, 400)
                del msgs[idx:]
                if idx - 1 < 0 or msgs[idx - 1].get("role") != "user":
                    return self._json({"ok": False, "error": "no user message before"}, 400)
                content = msgs[idx - 1].get("content") or ""
            else:
                return self._json({"ok": False, "error": "bad mode"}, 400)
            # 截断后收敛压缩点，避免摘要指向已删除的消息
            s["summary_upto"] = max(0, min(int(s.get("summary_upto") or 0), len(msgs)))
        else:
            s["messages"].append({"role": "user", "content": content})
            if (s.get("title") or "新对话") in ("", "新对话"):
                s["title"] = content.replace("\n", " ")[:18] or "新对话"
        s["updated_at"] = time.time()
        with _lock:
            save_sessions(data)

        api_key, api_url, api_model = get_api_conf("chat")

        # 建立 SSE 响应
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        def sse(obj):
            try:
                self.wfile.write(("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8"))
                self.wfile.flush()
            except Exception:
                pass

        sse({"type": "session", "sessions": slim_sessions(data)})

        profile = load_profile()
        # 用户画像已迁入知识库，这里只保留「手动偏好」；画像条目改由回忆检索统一注入
        system_prompt = (profile.get("manual", "") or "").strip()

        # 记忆检索：从用户画像 + 会话记忆 + 绑定的知识库召回，注入系统提示
        sse({"type": "recalling"})
        mem_block, mem_count = retrieve_memory(s, content)
        if mem_block:
            system_prompt = (system_prompt + "\n\n" + mem_block) if system_prompt else mem_block
            sse({"type": "memory", "count": mem_count})

        # 记忆工具：把「你有读写权限 + 可用作用域 + 现有条目」写进系统提示，
        # 模型便能直接在回复里给出写入指令（而不是嘴上说"我无法记录"）
        try:
            tool_prompt = mem_tool.build_tool_prompt(s)
        except Exception as e:
            tool_prompt = ""
            print(f"[记忆工具] 提示词生成失败：{e}")
        if tool_prompt:
            system_prompt = (system_prompt + "\n\n" + tool_prompt) if system_prompt else tool_prompt

        # 上下文占用（估算 + 校准），先推给前端更新进度条
        ctx = session_context(s, system_prompt)
        sse({"type": "context", "context": ctx})

        if not api_key:
            sse({"type": "error", "message": "⚠️ 尚未配置聊天 API 密钥，请在「设置 → 聊天 API」中填写。"})
            sse({"type": "done", "stopped": False, "context": ctx,
                 "sessions": slim_sessions(load_sessions())})
            return

        # 超限预判：上下文 + 为回复预留 超出上限时，先把较早的历史压缩成摘要
        limit, reserve = context_limits()
        if ctx["used"] + reserve > limit:
            sse({"type": "compressing"})
            if compress_history(s):
                with _lock:
                    save_sessions(data)
                ctx = session_context(s, system_prompt)
                sse({"type": "context", "context": ctx})

        messages = build_context_messages(s, system_prompt)
        ctx_cut = int(s.get("summary_upto") or 0)   # 记录本轮使用的压缩点，供后续校准对齐

        # 联网搜索：开关开启时先搜再答，把结果作为参考资料注入系统提示
        sources = []
        want_search = body.get("search_enabled")
        if want_search is None:
            want_search = bool(load_config().get("search_enabled", False))
        if want_search:
            sse({"type": "searching"})
            try:
                sp, surl, skey, scount = get_search_conf()
                if not skey:
                    sse({"type": "search_error",
                         "message": "尚未配置搜索 API 密钥，请在「设置 → 联网搜索」中填写。"})
                else:
                    cfg_s = load_config()
                    # 1) 查询改写：把口语化问题转成多个检索式（失败／未开则用原问题）
                    if cfg_s.get("search_rewrite", True):
                        queries = rewrite_queries(content, s.get("messages", [])[:-1][-6:])
                    else:
                        queries = [content]
                    if len(queries) > 1:
                        sse({"type": "search_queries", "queries": queries})
                    # 2) 多查询并行检索 + 按 URL 去重
                    sources = search_multi(queries, sp, surl, skey, scount)
                    # 3) 抓正文（只用于注入提示；入库时会被 slim_items 裁掉正文）
                    if sources and cfg_s.get("search_fetch", True):
                        try:
                            sources = fetch_pages(sources,
                                                  cfg_s.get("search_fetch_count") or 4,
                                                  cfg_s.get("search_fetch_reader") or "jina")
                        except Exception as fe:
                            print(f"[搜索] 抓正文失败：{fe}")
                    if sources:
                        ctx = build_search_context(content, sources)
                        if messages and messages[0].get("role") == "system":
                            messages[0]["content"] += "\n\n" + ctx
                        else:
                            messages.insert(0, {"role": "system", "content": ctx})
                        sse({"type": "sources", "items": slim_items(sources)})
                    else:
                        sse({"type": "search_error", "message": "未搜索到相关结果。"})
            except Exception as e:
                sse({"type": "search_error", "message": f"联网搜索失败：{e}"})

        # 思考参数：优先用前端传来的开关状态，否则回落到配置
        cfg_now = load_config()
        think_enabled = body.get("think_enabled")
        if think_enabled is None:
            think_enabled = bool(cfg_now.get("chat_think_enabled", False))
        think_level = body.get("think_level") or cfg_now.get("chat_think_level") or "high"
        think_mode = effective_think_mode(api_url)

        req = {"model": api_model, "messages": messages, "stream": True,
               "stream_options": {"include_usage": True}}
        req.update(thinking_payload(think_mode, bool(think_enabled), think_level))

        ev = threading.Event()
        ACTIVE_STREAMS[sid] = ev

        payload = json.dumps(req, ensure_ascii=False).encode("utf-8")
        reasoning = []
        usage_info = {}
        mx = mem_tool.MemoryExtractor()   # 把 ```memory 块从流里摘掉，用户看不到
        try:
            with requests.post(api_url, headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json; charset=utf-8"},
                    data=payload, stream=True, timeout=(10, 300)) as resp:
                resp.raise_for_status()
                for raw in resp.iter_lines():
                    if ev.is_set():
                        break
                    if not raw:
                        continue
                    line = raw.decode("utf-8", errors="ignore").strip()
                    if not line.startswith("data:"):
                        continue
                    chunk = line[5:].strip()
                    if chunk == "[DONE]":
                        break
                    try:
                        obj = json.loads(chunk)
                    except Exception:
                        continue
                    if obj.get("usage"):
                        usage_info = obj["usage"]
                    choices = obj.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    r_piece = delta.get("reasoning_content")
                    if r_piece:
                        reasoning.append(r_piece)
                        sse({"type": "reasoning", "text": r_piece})
                    piece = delta.get("content")
                    if piece:
                        vis = mx.feed(piece)
                        if vis:
                            sse({"type": "delta", "text": vis})
        except requests.exceptions.Timeout:
            sse({"type": "error", "message": "⏰ 请求超时，请检查网络连接"})
        except Exception as e:
            sse({"type": "error", "message": f"❌ 调用出错：{e}"})
        finally:
            ACTIVE_STREAMS.pop(sid, None)

        # 收尾：吐出被按住的内容；未闭合又解析不出 ops 的块当普通文本展示，避免内容丢失
        leftover = mx.finish()
        if leftover and not mx.ops():
            mx.visible.append(leftover)
            sse({"type": "delta", "text": leftover})
        text = mx.visible_text()
        think_text = "".join(reasoning)

        # AI 自主写入：执行模型给出的记忆操作（代码块已在流式过程中被摘除）
        mem_note = None
        try:
            mem_note = mem_tool.apply_chat_ops(sid, mx.ops())
        except Exception as e:
            print(f"[记忆工具] 执行失败：{e}")
        if mem_note:
            sse({"type": "mem_write", "memory": mem_note})

        # 用量（来自最后一个 SSE 块的 usage 字段）
        usage = None
        if usage_info:
            p = int(usage_info.get("prompt_tokens") or 0)
            c = int(usage_info.get("completion_tokens") or 0)
            details = usage_info.get("prompt_tokens_details") or {}
            usage = {
                "prompt": p,
                "completion": c,
                "total": int(usage_info.get("total_tokens") or (p + c)),
                "cached": int(usage_info.get("prompt_cache_hit_tokens")
                              or details.get("cached_tokens") or 0),
                "model": api_model,
            }

        if text.strip() or think_text.strip():
            data2 = load_sessions()
            s2 = find_session(data2, sid)
            if s2:
                msg = {"role": "assistant", "content": text}
                if think_text.strip():
                    msg["reasoning"] = think_text
                if sources:
                    msg["sources"] = slim_items(sources)   # 正文不入库，避免 sessions.json 膨胀
                if usage:
                    msg["usage"] = usage
                if mem_note:
                    msg["memory"] = mem_note
                msg["ctx_cut"] = ctx_cut
                s2["messages"].append(msg)
                s2["updated_at"] = time.time()
                with _lock:
                    save_sessions(data2)

        if usage:
            record_usage({"ts": time.time(), "source": "chat", **usage})
            sse({"type": "usage", "usage": usage})

        final_data = load_sessions()
        final_s = find_session(final_data, sid)
        sse({"type": "done", "stopped": ev.is_set(),
             "context": session_context(final_s, system_prompt) if final_s else None,
             "sessions": slim_sessions(final_data)})


def create_server(host="127.0.0.1", port=0):
    # 启动时把旧的 user_profile.auto 迁移进「用户画像」知识库（只做一次）
    try:
        migrate_profile_to_kb()
    except Exception as e:
        print(f"[知识库] 迁移失败：{e}")
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    return httpd, httpd.server_address[1]
