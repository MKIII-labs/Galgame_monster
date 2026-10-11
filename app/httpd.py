# -*- coding: utf-8 -*-
"""HTTP 处理：静态资源、REST API 与聊天流式(SSE)"""
import base64
import json
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote

import requests

from app.store import WEB_DIR, ACTIVE_STREAMS, DATA_DIR, _lock
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
from app.imaging import recognize_image, build_vision_content, vision_mode
from app.balance import query_balance
from app.knowledge import (load_kb_index, load_kb, save_kb, new_kb, delete_kb, set_kb_write,
                           is_profile_kb, retrieve_memory, migrate_profile_to_kb,
                           relations_payload, get_profile_kb)
from app import mem_tool
from app import notes as noteslib
from app import speech
from app import agent
from app import gal
from app import tts
from app import winframe

TOKEN = secrets.token_urlsafe(24)

# ============================================================
# 聊天图片附件：落盘到 data/uploads/<会话>/，消息里只存引用
# ============================================================
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")

_UPLOAD_EXT = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg",
    "image/gif": ".gif", "image/webp": ".webp", "image/bmp": ".bmp",
}
_MIME_BY_EXT = {v: k for k, v in _UPLOAD_EXT.items()}
_MIME_BY_EXT[".jpeg"] = "image/jpeg"


def _upload_dir(sid):
    return os.path.join(UPLOAD_DIR, re.sub(r"[^A-Za-z0-9_.-]", "_", str(sid or "_")))


def save_upload(sid, mime, b64):
    """把 base64 图片存盘，返回 {name, mime, size}；失败返回 None"""
    try:
        raw = base64.b64decode(b64)
    except Exception:
        return None
    if not raw:
        return None
    ext = _UPLOAD_EXT.get((mime or "").lower(), ".png")
    name = uuid.uuid4().hex + ext
    d = _upload_dir(sid)
    try:
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, name), "wb") as f:
            f.write(raw)
    except OSError:
        return None
    return {"name": name, "mime": mime or "image/png", "size": len(raw)}


def _upload_path(sid, name):
    name = os.path.basename(str(name or ""))       # 防目录穿越
    if not name:
        return None
    p = os.path.join(_upload_dir(sid), name)
    return p if os.path.isfile(p) else None


def drop_upload_files(sid, msgs):
    """删除这些消息引用的图片文件（用于撤回 / 删除消息）"""
    d = _upload_dir(sid)
    for m in msgs or []:
        im = m.get("image") if isinstance(m, dict) else None
        if not im:
            continue
        name = os.path.basename(str(im.get("name") or ""))
        if name:
            try:
                os.remove(os.path.join(d, name))
            except OSError:
                pass


def delete_session_uploads(sid):
    shutil.rmtree(_upload_dir(sid), ignore_errors=True)


def _quick_chat(api_key, api_url, api_model, messages, max_tokens=96):
    """一次非流式聊天调用（用于 GAL 剧情节点判定等轻量请求）"""
    resp = requests.post(
        api_url,
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json; charset=utf-8"},
        json={"model": api_model, "messages": messages, "stream": False,
              "max_tokens": max_tokens, "temperature": 0.2},
        timeout=(10, 60))
    resp.raise_for_status()
    data = resp.json()
    msg = ((data.get("choices") or [{}])[0].get("message") or {})
    return normalize_content(msg.get("content"))


def user_profile_text(limit=30):
    """用户画像文本（手动偏好 + 「用户画像」知识库），供 AI 编剧贴合用户"""
    parts = []
    try:
        p = load_profile() or {}
        manual = str(p.get("manual") or "").strip()
        if manual:
            parts.append("【用户手动偏好】\n" + manual)
    except Exception:
        pass
    try:
        kb = load_kb(get_profile_kb())
        nodes = (kb or {}).get("nodes") or []
        if nodes:
            parts.append("【用户画像】")
            for n in nodes[:limit]:
                title = str(n.get("title") or "").strip()
                if not title:
                    continue
                cat = str(n.get("category") or "其他").strip()
                content = str(n.get("content") or "").strip().replace("\n", " ")
                parts.append("- （%s）%s%s" % (cat, title, ("：" + content[:60]) if content else ""))
    except Exception:
        pass
    return "\n".join(parts)


def recent_dialog(s, limit=6, each=160):
    """把最近的对话压成简要文本（供编剧参考当前走向）"""
    msgs = (s or {}).get("messages") or []
    picked = []
    for m in msgs[-limit * 2:]:
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        text = str(m.get("content") or "").replace("\n", " ").strip()
        if not text:
            continue
        if len(text) > each:
            text = text[:each] + "…"
        picked.append(("玩家" if role == "user" else "角色") + "：" + text)
    return "\n".join(picked[-limit:])

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

    def _body_raw(self):
        """按 Content-Length 读取原始字节（用于音频等非 JSON 请求体）"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return b""
        return self.rfile.read(length)

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
            if path == "/api/stt/status":
                return self._json({"ok": True, "speech": speech.status()})
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
            "/api/stt": self._api_stt,
            "/api/stt/start": self._api_stt_start,
            "/api/stt/chunk": self._api_stt_chunk,
            "/api/stt/finish": self._api_stt_finish,
            "/api/stt/polish": self._api_stt_polish,
            "/api/session/new": self._api_session_new,
            "/api/session/rename": self._api_session_rename,
            "/api/session/delete": self._api_session_delete,
            "/api/session/select": self._api_session_select,
            "/api/session/clear": self._api_session_clear,
            "/api/config": self._api_save_config,
            "/api/profile": self._api_save_profile,
            "/api/message/withdraw": self._api_message_withdraw,
            "/api/attach/get": self._api_attach_get,
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
            "/api/session/agent": self._api_session_agent,
            "/api/agent/answer": self._api_agent_answer,
            "/api/agent/undo": self._api_agent_undo,
            "/api/sandbox/pick": self._api_sandbox_pick,
            "/api/sandbox/tree": self._api_sandbox_tree,
            "/api/sandbox/file": self._api_sandbox_file,
            "/api/sandbox/raw": self._api_sandbox_raw,
            "/api/sandbox/save": self._api_sandbox_save,
            "/api/sandbox/new": self._api_sandbox_new,
            "/api/sandbox/rename": self._api_sandbox_rename,
            "/api/sandbox/delete": self._api_sandbox_delete,
            "/api/sandbox/search": self._api_sandbox_search,
            "/api/sandbox/open_vscode": self._api_sandbox_open_vscode,
            "/api/gal/characters": self._api_gal_characters,
            "/api/gal/character/get": self._api_gal_character_get,
            "/api/gal/character/save": self._api_gal_character_save,
            "/api/gal/character/delete": self._api_gal_character_delete,
            "/api/gal/asset": self._api_gal_asset,
            "/api/gal/character/files": self._api_gal_character_files,
            "/api/gal/character/upload": self._api_gal_character_upload,
            "/api/gal/character/export": self._api_gal_character_export,
            "/api/gal/character/import": self._api_gal_character_import,
            "/api/gal/character/import_dir": self._api_gal_character_import_dir,
            "/api/gal/packs": self._api_gal_packs,
            "/api/window/theme": self._api_window_theme,
            "/api/gal/character/draft": self._api_gal_character_draft,
            "/api/gal/script": self._api_gal_script,
            "/api/gal/story/get": self._api_gal_story_get,
            "/api/gal/story/save": self._api_gal_story_save,
            "/api/gal/media": self._api_gal_media,
            "/api/gal/effects": self._api_gal_effects,
            "/api/gal/effect/audio": self._api_gal_effect_audio,
            "/api/gal/tts": self._api_gal_tts,
            "/api/gal/tts/active": self._api_gal_tts_active,
            "/api/gal/tts/voices": self._api_gal_tts_voices,
            "/api/gal/session": self._api_gal_session,
            "/api/vscode/import": self._api_vscode_import,
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
        drop_id = body.get("id")
        data["sessions"] = [s for s in data["sessions"] if s["id"] != drop_id]
        data["current"] = data["sessions"][0]["id"]
        with _lock:
            save_sessions(data)
        delete_session_uploads(drop_id)      # 连带清理该会话的图片附件
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
            drop_upload_files(s.get("id"), s.get("messages"))   # 清理该会话的图片附件
            s["messages"] = []
            s["summary"] = ""
            s["summary_upto"] = 0
            with _lock:
                save_sessions(data)
        self._ok_sessions(data)

    def _api_message_withdraw(self):
        """撤回：默认从指定下标起删除该消息及其后的全部消息；only_this=true 时仅删除这一条"""
        body = self._body()
        data = load_sessions()
        s = find_session(data, body.get("session_id"))
        try:
            idx = int(body.get("index"))
        except (TypeError, ValueError):
            return self._json({"ok": False, "error": "bad index"}, 400)
        if s and 0 <= idx < len(s["messages"]):
            removed = [s["messages"][idx]] if body.get("only_this") else s["messages"][idx:]
            drop_upload_files(s.get("id"), removed)      # 连带删除这些消息的图片附件
            if body.get("only_this"):
                del s["messages"][idx]
            else:
                del s["messages"][idx:]
            s["summary_upto"] = max(0, min(int(s.get("summary_upto") or 0), len(s["messages"])))
            s["updated_at"] = time.time()
            with _lock:
                save_sessions(data)
        self._ok_sessions(data)

    def _api_attach_get(self):
        """按需返回聊天图片附件的原始字节"""
        body = self._body()
        p = _upload_path(body.get("session_id"), body.get("name"))
        if not p:
            return self._json({"ok": False, "error": "not found"}, 404)
        try:
            with open(p, "rb") as f:
                data = f.read()
        except OSError:
            return self._json({"ok": False, "error": "read failed"}, 500)
        return self._send(200, data, _MIME_BY_EXT.get(os.path.splitext(p)[1].lower(),
                                                      "application/octet-stream"))

    # ---------- GAL 演出模式（见 docs/GAL_MODE_DESIGN.md） ----------
    def _api_gal_characters(self):
        self._json({"ok": True, "characters": gal.list_characters()})

    def _api_gal_character_get(self):
        c = gal.load_character(self._body().get("id"))
        if not c:
            return self._json({"ok": False, "error": "角色不存在"}, 404)
        self._json({"ok": True, "character": c})

    def _api_gal_character_save(self):
        body = self._body()
        try:
            c = gal.save_character(body.get("id"), body.get("character") or {})
        except ValueError as e:
            return self._json({"ok": False, "error": str(e)}, 400)
        self._json({"ok": True, "character": c, "characters": gal.list_characters()})

    def _api_gal_character_delete(self):
        try:
            ok = gal.delete_character(self._body().get("id"))
        except ValueError as e:
            return self._json({"ok": False, "error": str(e)}, 400)
        self._json({"ok": ok, "characters": gal.list_characters()})

    def _api_gal_asset(self):
        """按需返回角色立绘图片字节"""
        body = self._body()
        try:
            p, mime = gal.resolve_asset(body.get("id"), body.get("path"))
        except ValueError as e:
            return self._json({"ok": False, "error": str(e)}, 400)
        try:
            with open(p, "rb") as f:
                data = f.read()
        except OSError:
            return self._json({"ok": False, "error": "读取失败"}, 500)
        return self._send(200, data, mime)

    def _api_gal_character_files(self):
        try:
            files = gal.list_sprite_files(self._body().get("id"))
        except ValueError as e:
            return self._json({"ok": False, "error": str(e)}, 400)
        self._json({"ok": True, "files": files})

    def _api_gal_character_upload(self):
        body = self._body()
        try:
            rel = gal.save_upload(body.get("id"), body.get("filename"), body.get("data"))
        except ValueError as e:
            return self._json({"ok": False, "error": str(e)}, 400)
        self._json({"ok": True, "path": rel, "files": gal.list_sprite_files(body.get("id"))})

    def _api_gal_character_export(self):
        """把角色导出为 .char（zip）下载"""
        try:
            fname, data = gal.export_character(self._body().get("id"))
        except ValueError as e:
            return self._json({"ok": False, "error": str(e)}, 400)
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition",
                         "attachment; filename=\"character.char\"; filename*=UTF-8''" + quote(fname))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _api_gal_character_import(self):
        """导入 .char / zip 角色包（兼容 Shinsekai 的 character.yaml）"""
        body = self._body()
        try:
            raw = base64.b64decode(body.get("data") or "")
        except Exception:
            return self._json({"ok": False, "error": "文件内容解析失败"}, 400)
        try:
            r = gal.import_character_pack(raw, body.get("filename") or "")
        except ValueError as e:
            return self._json({"ok": False, "error": str(e)}, 400)
        r["ok"] = True
        r["characters"] = gal.list_characters()
        self._json(r)

    def _api_window_theme(self):
        """把明暗主题同步到应用窗口标题栏（Windows DWM；其它平台静默失败）"""
        body = self._body()
        dark = str(body.get("theme") or "") == "dark"
        bg = str(body.get("bg") or "").strip()
        self._json({"ok": bool(winframe.apply(dark, bg))})

    def _api_gal_packs(self):
        """列出 data/gal/packs/ 下待导入的角色包"""
        self._json({"ok": True, "packs": gal.list_packs()})

    def _api_gal_character_import_dir(self):
        """批量导入 data/gal/packs/ 下的角色包"""
        r = gal.import_packs_dir()
        r["ok"] = True
        r["packs"] = gal.list_packs()
        r["characters"] = gal.list_characters()
        self._json(r)

    def _api_gal_character_draft(self):
        """AI 帮写角色设定（复用「设置 → 聊天模型」的配置）"""
        body = self._body()
        name = str(body.get("name") or "").strip()
        if not name:
            return self._json({"ok": False, "error": "请先填写角色名"}, 400)
        api_key, api_url, api_model = get_api_conf("chat")
        if not api_key:
            return self._json({"ok": False, "error": "未配置聊天模型（设置 → 聊天模型）"}, 400)
        sys_prompt = ("你在为 Galgame 角色撰写「角色设定」。用中文输出，300 字以内，"
                      "涵盖：性格、说话方式（语气/口头禅）、背景经历、与主角的关系与态度。"
                      "直接给出设定正文，不要标题、不要分点、不要复述角色名。")
        user = "角色名：%s" % name
        brief = str(body.get("brief") or "").strip()
        if brief:
            user += "\n已有线索：%s" % brief
        profile = str(body.get("profile") or "").strip()
        if profile:
            user += "\n在以下现有设定基础上完善：\n%s" % profile[:800]
        try:
            text = _quick_chat(api_key, api_url, api_model,
                               [{"role": "system", "content": sys_prompt},
                                {"role": "user", "content": user}], max_tokens=700)
        except Exception as e:
            return self._json({"ok": False, "error": "生成失败：%s" % e}, 502)
        self._json({"ok": True, "profile": str(text or "").strip()})

    def _api_gal_script(self):
        """AI 自动剧本：action = get 读取 | generate 生成/重新生成 | clear 清空

        生成时会把「角色设定 + 用户画像 + 已有对话」交给模型，产出与 story.json
        同构的分幕剧本，存进当前会话（每会话独立）。
        """
        body = self._body()
        action = str(body.get("action") or "get").strip()
        data = load_sessions()
        s = find_session(data, body.get("id") or body.get("session_id"))
        if not s:
            return self._json({"ok": False, "error": "no session"}, 404)
        ensure_session_kb_fields(s)

        if action == "clear":
            s["gal_auto_story"] = {}
            s["gal_auto_outline"] = ""
            s["gal_auto_acts_done"] = 0
            s["gal_node_id"] = ""
            s["gal_round"] = 0
            s["updated_at"] = time.time()
            with _lock:
                save_sessions(data)
            return self._json({"ok": True, "story": {}, "outline": "", "node_id": "", "round": 0})

        if action == "generate":
            cfg = load_config() or {}
            char = gal.load_character(body.get("character_id") or s.get("gal_character_id"))
            if not char:
                return self._json({"ok": False, "error": "请先选择角色"}, 400)
            api_key, api_url, api_model = get_api_conf("chat")
            if not api_key:
                return self._json({"ok": False, "error": "未配置聊天模型（设置 → 聊天模型）"}, 400)
            acts = gal._clamp(cfg.get("gal_auto_script_acts"), 5, 1, 12)
            rounds = gal._clamp(cfg.get("gal_auto_script_rounds"), 4, 1, 12)
            prompt = gal.build_script_prompt(char, user_profile_text(), acts, rounds,
                                            recent=recent_dialog(s))
            try:
                reply = _quick_chat(api_key, api_url, api_model, [
                    {"role": "system", "content": "你是 Galgame 编剧。只输出一个 JSON 对象，不要解释。"},
                    {"role": "user", "content": prompt},
                ], max_tokens=1600)
            except Exception as e:
                return self._json({"ok": False, "error": "生成失败：%s" % e}, 502)
            story = gal.parse_plan(reply, acts)
            if not story:
                return self._json({"ok": False, "error": "模型未返回可用大纲，请重试"}, 502)
            start = gal.start_story(story, rounds)
            if not start:
                return self._json({"ok": False, "error": "大纲内容为空，请重试"}, 502)
            s["gal_auto_story"] = start
            s["gal_auto_outline"] = gal.plan_outline_text(story)
            s["gal_auto_acts_done"] = 0
            s["gal_auto_on"] = True
            s["gal_node_id"] = start.get("start") or ""
            s["gal_round"] = 0
            s["updated_at"] = time.time()
            with _lock:
                save_sessions(data)
            return self._json({"ok": True, "story": start, "plan": story,
                               "outline": s["gal_auto_outline"],
                               "node_id": s["gal_node_id"], "round": 0})

        self._json({"ok": True, "on": bool(s.get("gal_auto_on")),
                    "story": s.get("gal_auto_story") or {},
                    "outline": s.get("gal_auto_outline") or "",
                    "node_id": s.get("gal_node_id") or "",
                    "round": int(s.get("gal_round") or 0)})

    def _api_gal_effects(self):
        """列出关键词特效表（供设置界面提示）"""
        self._json({"ok": True, "effects": gal.load_effects()})

    def _api_gal_effect_audio(self):
        """返回特效音频字节（data/gal/effects/ 下）"""
        try:
            p, mime = gal.resolve_effect_audio(self._body().get("path"))
        except ValueError as e:
            return self._json({"ok": False, "error": str(e)}, 400)
        try:
            with open(p, "rb") as f:
                data = f.read()
        except OSError:
            return self._json({"ok": False, "error": "读取失败"}, 500)
        return self._send(200, data, mime)

    def _api_gal_story_get(self):
        self._json({"ok": True, "story": gal.load_story(self._body().get("id"))})

    def _api_gal_story_save(self):
        body = self._body()
        try:
            story = gal.save_story(body.get("id"), body.get("story") or {})
        except ValueError as e:
            return self._json({"ok": False, "error": str(e)}, 400)
        self._json({"ok": True, "story": story})

    def _api_gal_media(self):
        """返回背景（kind=bg）或 BGM（kind=bgm）资源字节"""
        body = self._body()
        try:
            p, mime = gal.resolve_media(body.get("kind") or "bg", body.get("path"))
        except ValueError as e:
            return self._json({"ok": False, "error": str(e)}, 400)
        try:
            with open(p, "rb") as f:
                data = f.read()
        except OSError:
            return self._json({"ok": False, "error": "读取失败"}, 500)
        return self._send(200, data, mime)

    def _api_gal_tts(self):
        """台词朗读：文本 → 音频字节（force=true 时可绕过「未开启」限制，供设置里试听）

        character_id / preset 可选：命中则该角色用它的配音预设覆盖全局 TTS 配置。
        """
        body = self._body()
        char = gal.load_character(body.get("character_id")) if body.get("character_id") else None
        override = gal.tts_override(char, body.get("preset"))
        if not override and isinstance(body.get("override"), dict):
            override = body.get("override")          # 编辑器里的「试听」：用未保存的表单值
        # 参考音频 / 微调权重：预设里存的是角色目录内相对路径 → 解析成 GPT-SoVITS 服务端可用的绝对路径
        if isinstance(override, dict) and body.get("character_id"):
            extra = {}
            if override.get("ref_audio"):
                try:
                    extra["ref_audio_path"], _m = gal.resolve_asset(
                        body.get("character_id"), override["ref_audio"])
                except ValueError:
                    pass
            for k in ("gpt_weights", "sovits_weights"):
                if override.get(k):
                    try:
                        extra[k + "_path"] = gal.resolve_weight(
                            body.get("character_id"), override[k])
                    except ValueError:
                        pass
            if extra:
                override = dict(override, **extra)
        cfg = tts.config(override=override)
        if not cfg["enabled"] and not body.get("force"):
            return self._json({"ok": False, "error": "未开启台词朗读（TTS）"}, 400)
        text = (body.get("text") or "").strip()
        if not text:
            return self._json({"ok": False, "error": "empty"}, 400)
        try:
            data, mime = tts.synthesize(cfg, text, body.get("voice") or "")
        except Exception as e:
            return self._json({"ok": False, "error": "合成失败：%s" % e}, 502)
        return self._send(200, data, mime)

    def _api_gal_tts_active(self):
        """设置某角色当前使用的配音预设（空串表示跟随全局）"""
        body = self._body()
        cid = body.get("id")
        char = gal.load_character(cid)
        if not char:
            return self._json({"ok": False, "error": "角色不存在"}, 404)
        name = str(body.get("preset") or "").strip()
        t = char.get("tts") or {"active": "", "presets": {}}
        if name and name not in (t.get("presets") or {}):
            return self._json({"ok": False, "error": "预设不存在"}, 400)
        t["active"] = name
        char["tts"] = t
        gal.save_character(cid, char)
        self._json({"ok": True, "character": gal.load_character(cid)})

    def _api_gal_tts_voices(self):
        """列出 Edge 中文音色（设置界面提示用）"""
        voices = tts.edge_voices()
        self._json({"ok": True, "voices": voices, "default": tts.EDGE_DEFAULT_VOICE})

    def _api_gal_session(self):
        """设置会话的 GAL 字段（角色 / 是否 GAL 模式）"""
        body = self._body()
        data = load_sessions()
        s = find_session(data, body.get("id") or body.get("session_id"))
        if not s:
            return self._json({"ok": False, "error": "no session"}, 404)
        ensure_session_kb_fields(s)
        if "gal_enabled" in body:
            s["gal_enabled"] = bool(body.get("gal_enabled"))
        if "gal_character_id" in body:
            s["gal_character_id"] = str(body.get("gal_character_id") or "")
        if "gal_node_id" in body:
            s["gal_node_id"] = str(body.get("gal_node_id") or "")
            s["gal_round"] = 0
        if "gal_story_on" in body:      # 启用/停用该角色的剧本
            story = gal.load_story(s.get("gal_character_id"))
            if body.get("gal_story_on") and story and story.get("start"):
                s["gal_node_id"] = story["start"]
            elif not body.get("gal_story_on"):
                s["gal_node_id"] = ""
            s["gal_round"] = 0
        if "gal_auto_on" in body:       # 启用/停用 AI 自动剧本（剧本本身由 /api/gal/script 生成）
            on = bool(body.get("gal_auto_on"))
            s["gal_auto_on"] = on
            if on:
                st = gal.normalize_story(s.get("gal_auto_story"))
                s["gal_node_id"] = st.get("start") or ""
            else:
                s["gal_node_id"] = ""
            s["gal_round"] = 0
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
        if isinstance(body.get("graph"), dict):
            kb["graph"] = body["graph"]
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
            if isinstance(mem.get("graph"), dict):
                s["memory"]["graph"] = mem["graph"]
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

    def _api_session_agent(self):
        """设置会话的 Agent 开关与沙箱目录"""
        body = self._body()
        data = load_sessions()
        s = find_session(data, body.get("id"))
        if not s:
            return self._json({"ok": False, "error": "no session"}, 404)
        ensure_session_kb_fields(s)
        if "agent_enabled" in body:
            s["agent_enabled"] = bool(body.get("agent_enabled"))
        if "sandbox" in body:
            s["sandbox"] = agent.clean_sandbox(body.get("sandbox"))
        if "allow_outside_read" in body:
            s["allow_outside_read"] = bool(body.get("allow_outside_read"))
        s["updated_at"] = time.time()
        with _lock:
            save_sessions(data)
        self._ok_sessions(data)

    def _api_agent_answer(self):
        """回答 AI 中途提出的问题（唤醒正在等待的对话线程）"""
        body = self._body()
        ans = body.get("answer")
        ok = agent.submit_answer(body.get("qid"), "" if ans is None else str(ans))
        self._json({"ok": ok})

    def _api_agent_undo(self):
        """撤回一次 Agent 的写操作"""
        body = self._body()
        token = body.get("token")
        ok = agent.undo(token) if token else False
        self._json({"ok": ok})

    def _api_sandbox_pick(self):
        """弹出系统文件夹选择框，返回所选沙箱目录"""
        body = self._body()
        path = agent.pick_directory((body.get("initial") or "").strip())
        self._json({"ok": bool(path), "path": path})

    # ---------- IDE / 工作区（沙箱内文件操作） ----------

    def _sb(self, body):
        s = find_session(load_sessions(), body.get("session_id"))
        return (s, agent.clean_sandbox(s.get("sandbox"))) if s else (None, None)

    def _sb_guard(self, body):
        """返回 (session, sandbox, None) 或 (None, None, 错误响应)"""
        s, sb = self._sb(body)
        if not s:
            return None, None, self._json({"ok": False, "error": "会话不存在"}, 404)
        return s, sb, None

    def _api_sandbox_tree(self):
        s, sb, err = self._sb_guard(self._body())
        if err:
            return err
        try:
            return self._json({"ok": True, "tree": agent.tree(sb)})
        except agent.SandboxError as e:
            return self._json({"ok": False, "error": str(e)}, 400)

    def _api_sandbox_file(self):
        body = self._body()
        s, sb, err = self._sb_guard(body)
        if err:
            return err
        try:
            data = agent.read_text(sb, body.get("path"))
            data["ok"] = True
            return self._json(data)
        except agent.SandboxError as e:
            return self._json({"ok": False, "error": str(e)}, 400)

    def _api_sandbox_raw(self):
        """返回沙箱内图片的原始字节（用于 IDE 预览）"""
        body = self._body()
        s, sb, err = self._sb_guard(body)
        if err:
            return err
        rel = body.get("path")
        if not agent.image_mime(rel):
            return self._json({"ok": False, "error": "不是可预览的图片类型"}, 400)
        try:
            data, _size = agent.read_bytes(sb, rel)
        except agent.SandboxError as e:
            return self._json({"ok": False, "error": str(e)}, 400)
        return self._send(200, data, agent.image_mime(rel))

    def _api_sandbox_save(self):
        body = self._body()
        s, sb, err = self._sb_guard(body)
        if err:
            return err
        try:
            r = agent.write_text(sb, body.get("path"), body.get("content"))
            r["ok"] = True
            return self._json(r)
        except agent.SandboxError as e:
            return self._json({"ok": False, "error": str(e)}, 400)

    def _api_sandbox_new(self):
        body = self._body()
        s, sb, err = self._sb_guard(body)
        if err:
            return err
        try:
            agent.ensure_entry(sb, body.get("path"), bool(body.get("dir")))
            return self._json({"ok": True})
        except agent.SandboxError as e:
            return self._json({"ok": False, "error": str(e)}, 400)

    def _api_sandbox_rename(self):
        body = self._body()
        s, sb, err = self._sb_guard(body)
        if err:
            return err
        try:
            agent.rename_entry(sb, body.get("path"), body.get("name"))
            return self._json({"ok": True})
        except agent.SandboxError as e:
            return self._json({"ok": False, "error": str(e)}, 400)

    def _api_sandbox_delete(self):
        body = self._body()
        s, sb, err = self._sb_guard(body)
        if err:
            return err
        try:
            agent.delete_entry(sb, body.get("path"))
            return self._json({"ok": True})
        except agent.SandboxError as e:
            return self._json({"ok": False, "error": str(e)}, 400)

    def _api_sandbox_search(self):
        body = self._body()
        s, sb, err = self._sb_guard(body)
        if err:
            return err
        pat = (body.get("pattern") or "").strip()
        globpat = (body.get("glob") or "").strip()
        if not pat and not globpat:
            return self._json({"ok": False, "error": "请输入要搜索的内容"}, 400)
        try:
            text, _ = agent._t_search_files(sb, {"pattern": pat, "glob": globpat})
        except agent.SandboxError as e:
            return self._json({"ok": False, "error": str(e)}, 400)
        except Exception as e:
            return self._json({"ok": False, "error": str(e)}, 400)
        return self._json({"ok": True, "text": text})

    def _api_sandbox_open_vscode(self):
        body = self._body()
        s, sb, err = self._sb_guard(body)
        if err:
            return err
        rel = (body.get("path") or "").strip()
        target = sb
        if rel:
            try:
                target = agent.resolve_path(sb, rel)
            except agent.SandboxError as e:
                return self._json({"ok": False, "error": str(e)}, 400)
        cli = find_vscode_cli()
        if not cli:
            return self._json({"ok": False, "error": "未找到 VS Code 的 code 命令（请确认已安装 VS Code）"}, 400)
        try:
            subprocess.Popen([cli, target], cwd=sb,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            try:                             # .cmd/.bat 在个别环境需借道 cmd /c
                subprocess.Popen(["cmd", "/c", cli, target], cwd=sb,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as e:
                return self._json({"ok": False, "error": "启动 VS Code 失败：%s" % e}, 400)
        except Exception as e:
            return self._json({"ok": False, "error": "启动 VS Code 失败：%s" % e}, 400)
        return self._json({"ok": True})

    def _api_vscode_import(self):
        try:
            return self._json(vscode_import_payload())
        except Exception as e:
            return self._json({"ok": False, "error": str(e)}, 400)

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

    def _api_stt_start(self):
        """开始一条流式识别会话（可选开启流式降噪）"""
        if not speech.find_stream_model_files():
            return self._json({"ok": False,
                               "error": "流式语音模型未就绪，请运行 scripts/fetch_speech_model.py streaming"}, 400)
        denoise = bool(self._body().get("denoise"))
        if denoise and not speech.find_denoise_model():
            return self._json({"ok": False,
                               "error": "降噪模型未就绪，请运行 scripts/fetch_speech_model.py denoise"}, 400)
        try:
            sid = speech.stream_start(denoise=denoise)
        except Exception as e:
            return self._json({"ok": False, "error": "加载模型失败：%s" % e}, 500)
        self._json({"ok": True, "sid": sid, "denoise": denoise})

    def _api_stt_chunk(self):
        """喂一段 16-bit 单声道 PCM，返回当前完整识别文本；sid 见 X-Stt-Sid 头"""
        sid = self.headers.get("X-Stt-Sid") or ""
        if not sid:
            return self._json({"ok": False, "error": "缺少 sid"}, 400)
        raw = self._body_raw()
        if len(raw) % 2:
            raw = raw[:-1]
        import numpy as np
        samples = np.frombuffer(raw, dtype="<i2").astype("float32") / 32768.0 if raw else None
        try:
            text = speech.stream_feed(sid, samples)
        except Exception as e:
            return self._json({"ok": False, "error": "识别失败：%s" % e}, 500)
        self._json({"ok": True, "text": text})

    def _api_stt_finish(self):
        """结束流式会话，返回最终文本"""
        sid = self._body().get("sid")
        if not sid:
            return self._json({"ok": False, "error": "缺少 sid"}, 400)
        try:
            text = speech.stream_finish(sid)
        except Exception as e:
            return self._json({"ok": False, "error": "识别失败：%s" % e}, 500)
        self._json({"ok": True, "text": text})

    def _api_stt_polish(self):
        """用聊天模型对转写文本做 AI 矫正"""
        text = self._body().get("text") or ""
        if not text.strip():
            return self._json({"ok": True, "text": ""})
        self._json({"ok": True, "text": speech.polish(text)})

    def _api_stt(self):
        """本地语音识别：请求体为 16-bit 单声道 PCM 原始字节，采样率见 X-Sample-Rate 头"""
        if not speech.status().get("ready"):
            return self._json({"ok": False, "error": "语音模型未就绪，请先运行 scripts/fetch_speech_model.py 下载",
                               "speech": speech.status()}, 400)
        raw = self._body_raw()
        if len(raw) < 2:
            return self._json({"ok": False, "error": "没有收到音频数据"}, 400)
        if len(raw) % 2:
            raw = raw[:-1]                      # int16，丢弃可能的多余半字节
        try:
            sr = int(self.headers.get("X-Sample-Rate") or 16000)
        except ValueError:
            sr = 16000
        import numpy as np
        samples = np.frombuffer(raw, dtype="<i2").astype("float32") / 32768.0
        try:
            text = speech.transcribe(samples, sr)
        except Exception as e:
            return self._json({"ok": False, "error": "识别失败：%s" % e}, 500)
        self._json({"ok": True, "text": text})

    def _api_chat(self):
        body = self._body()
        sid = body.get("session_id")
        content = (body.get("content") or "").strip()
        resend = body.get("resend") if isinstance(body.get("resend"), dict) else None
        img = body.get("image") if isinstance(body.get("image"), dict) else None
        img_b64 = (img or {}).get("data") or ""
        img_mime = (img or {}).get("mime") or "image/png"
        has_img = bool(img_b64) and not resend      # 图片仅在「发新消息」时支持
        if not content and not resend and not has_img:
            return self._json({"ok": False, "error": "empty"}, 400)

        data = load_sessions()
        s = find_session(data, sid)
        if not s:
            return self._json({"ok": False, "error": "no session"}, 404)

        # GAL 演出模式（见 docs/GAL_MODE_DESIGN.md）：改写系统提示并要求 JSON 输出
        gal_mode = str(body.get("mode") or "") == "gal"
        gal_char = (gal.load_character(body.get("gal_character_id") or s.get("gal_character_id"))
                    if gal_mode else None)
        # GAL 剧情节点：优先用「AI 自动剧本」（存在会话里），否则用角色目录的 story.json
        gal_auto = bool(s.get("gal_auto_on")) and gal.acts_done(s.get("gal_auto_story")) > 0
        if gal_auto:
            gal_story = gal.normalize_story(s.get("gal_auto_story"))
        else:
            gal_story = gal.load_story(gal_char["id"]) if gal_char else None
        gal_node = (gal.story_node(gal_story, str(s.get("gal_node_id") or ""))
                    if gal_story else None)
        if gal_auto and gal_node is None and gal_story:      # 自动剧本：节点缺失时回到首幕
            gal_node = gal.story_node(gal_story, gal_story.get("start") or "")

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
            stored = content or ("【图片】" if has_img else "")
            umsg = {"role": "user", "content": stored}
            if has_img:
                up = save_upload(s.get("id"), img_mime, img_b64)   # 图片落盘，消息里只存引用
                if up:
                    umsg["image"] = up
            s["messages"].append(umsg)
            if (s.get("title") or "新对话") in ("", "新对话"):
                s["title"] = (content or "图片").replace("\n", " ")[:18] or "新对话"
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

        # 图片：'vision' 模式先识别成文字，记进该条 user 消息的 vision 字段；
        #       'chat' 模式不识别，留到构建 messages 时转成多模态直发聊天模型
        if has_img:
            mode = vision_mode()
            sse({"type": "recognizing", "mode": mode})
            if mode == "vision":
                note = recognize_image(img_mime, img_b64, question=content)
                if note and not note.startswith(("❌", "⏰", "⚠️")):
                    s["messages"][-1]["vision"] = note
                    s["updated_at"] = time.time()
                    with _lock:
                        save_sessions(data)
                else:
                    sse({"type": "error", "message": note or "图片识别失败"})

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

        # 'chat' 模式：把最后一条 user 的内容换成「图片 + 文字」多模态数组
        if has_img and vision_mode() == "chat":
            for m in reversed(messages):
                if m.get("role") == "user":
                    m["content"] = build_vision_content(m.get("content"), img_mime, img_b64)
                    break

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

        ev = threading.Event()
        ACTIVE_STREAMS[sid] = ev

        # ---------- 单次模型调用（流式，摘除 ```memory / ```tool 代码块） ----------
        # GAL：按「完整 JSON 对象」逐段解析对白，边流边推给前端（逐段追加 + 逐段朗读）
        gal_name = (gal_char or {}).get("name", "")
        _cfg = load_config() or {}
        gal_fx = gal.load_effects() if (gal_mode and _cfg.get("gal_fx", True)) else []
        gal_dp = gal.DialogStreamParser(gal_name, gal_fx) if gal_mode else None
        gal_sprites = ((gal_char or {}).get("sprites") or []) if gal_mode else []

        def gal_seg_event(seg):
            """给一段对白算好立绘后组事件"""
            idx = gal.match_sprite(gal_char, seg.get("emotion"), seg.get("sprite"))
            path = ""
            if isinstance(idx, int) and 0 <= idx < len(gal_sprites):
                path = gal_sprites[idx]["path"]
            return {"type": "gal_speech", "index": seg.get("index", 0),
                    "speaker": seg.get("speaker", ""), "text": seg.get("speech", ""),
                    "narration": seg.get("narration", ""), "name": gal_name,
                    "emotion": seg.get("emotion", ""), "sprite": seg.get("sprite"),
                    "sprite_index": idx, "path": path, "fx": seg.get("fx") or []}

        # GAL 兜底：会话里若混有普通文本回复，模型可能不再输出 JSON 契约；
        # 此时照样把纯文本按行切段推给前端，保证「逐段 + 逐字」演出不失效。
        gal_plain = {"mode": None, "buf": ""}

        def gal_plain_event(text):
            t = (text or "").strip()
            if not t:
                return
            narr = ""
            if (t.startswith("（") and t.endswith("）")) or (t.startswith("(") and t.endswith(")")):
                narr = t[1:-1].strip()
            sse({"type": "gal_speech", "speaker": "", "text": ("" if narr else t),
                 "narration": narr, "name": gal_name, "emotion": "", "sprite": None,
                 "sprite_index": None, "path": "", "fx": []})

        def gal_plain_feed(chunk, flush=False):
            gal_plain["buf"] += chunk or ""
            pieces, rest = gal.split_plain_chunks(gal_plain["buf"], flush)
            gal_plain["buf"] = rest
            for p in pieces:
                gal_plain_event(p)

        def gal_feed(vis):
            if gal_plain["mode"] is None:
                gal_plain["buf"] += vis
                m = gal.detect_stream_mode(gal_plain["buf"])
                if m is None:
                    return                       # 开头还判断不出来，先攒着
                gal_plain["mode"] = m
                held, gal_plain["buf"] = gal_plain["buf"], ""
                if m == "json":
                    for seg in gal_dp.feed(held):
                        sse(gal_seg_event(seg))
                else:
                    gal_plain_feed(held)
            elif gal_plain["mode"] == "json":
                for seg in gal_dp.feed(vis):
                    sse(gal_seg_event(seg))
            else:
                gal_plain_feed(vis)

        def gal_flush():
            """一次模型调用收尾：把兜底缓冲里剩下的内容吐出来"""
            if gal_dp is None:
                return
            if gal_plain["mode"] == "json":
                for seg in gal_dp.feed(""):
                    sse(gal_seg_event(seg))
                return
            tail, gal_plain["buf"] = gal_plain["buf"], ""
            if gal_plain["mode"] is None:
                if not tail.strip():
                    return
                gal_plain["mode"] = "text"
            gal_plain_feed(tail, flush=True)

        def emit_visible(vis):
            """把一段「模型可见文本」推给前端：GAL 逐段推 gal_speech，普通模式推 delta"""
            if not vis:
                return
            if gal_dp is not None:
                gal_feed(vis)
            else:
                sse({"type": "delta", "text": vis})

        def call_model(msgs, allow_tool):
            req = {"model": api_model, "messages": msgs, "stream": True,
                   "stream_options": {"include_usage": True}}
            req.update(thinking_payload(think_mode, bool(think_enabled), think_level))
            payload = json.dumps(req, ensure_ascii=False).encode("utf-8")
            reasoning, usage_info = [], {}
            mem_mx = mem_tool.MemoryExtractor()
            tool_mx = agent.ToolExtractor() if allow_tool else None
            sent = 0
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
                            vis1 = tool_mx.feed(piece) if tool_mx is not None else piece
                            if vis1:
                                sent += len(vis1)
                                vis = mem_mx.feed(vis1)
                                if vis:
                                    emit_visible(vis)
            except requests.exceptions.Timeout:
                sse({"type": "error", "message": "⏰ 请求超时，请检查网络连接"})
            except Exception as e:
                sse({"type": "error", "message": f"❌ 调用出错：{e}"})

            tool_block, tool_calls = "", []
            if tool_mx is not None:
                tool_mx.finish()
                rest = tool_mx.visible_text()[sent:]   # finish 时可能吐出的残余
                if rest:
                    vis = mem_mx.feed(rest)
                    if vis:
                        emit_visible(vis)
                tool_block = tool_mx.block_text
                tool_calls = agent.parse_calls(tool_block)
            leftover = mem_mx.finish()                 # 未闭合又解析不出的块当普通文本
            if leftover and not mem_mx.ops():
                mem_mx.visible.append(leftover)
                emit_visible(leftover)
            gal_flush()                                # GAL：吐出兜底分段里剩下的内容
            return {"visible": mem_mx.visible_text(), "mem_ops": mem_mx.ops(),
                    "tool_block": tool_block, "tool_calls": tool_calls,
                    "reasoning": "".join(reasoning), "usage": usage_info,
                    "stopped": ev.is_set()}

        # ---------- 工具：提问随时可用；文件工具需「Agent + 沙箱」 ----------
        sandbox = agent.clean_sandbox(s.get("sandbox"))
        agent_on = bool(s.get("agent_enabled")) and bool(sandbox)
        ap = agent.build_agent_prompt(s)
        if ap:
            if messages and messages[0].get("role") == "system":
                messages[0]["content"] += "\n\n" + ap
            else:
                messages.insert(0, {"role": "system", "content": ap})

        # GAL：追加「演出模式」提示（要求 JSON 输出；情绪单/多值随配置；特效清单随开关）
        if gal_mode:
            gp = gal.build_prompt(gal_char, bool(_cfg.get("gal_emotion_multi")), agent_on, gal_fx)
            if gal_node:
                gp += "\n\n" + gal.build_node_prompt(gal_node, int(s.get("gal_round") or 0))
            if gal_auto:
                # AI 自动剧本：额外给出全剧走向，让每轮演出对齐整条故事线
                gp += ("\n\n### 本段故事由你（AI）自行编剧\n"
                       "上面「剧情节点」就是当前这一幕的演出要求，请在不脱离角色设定的前提下主动推动它；"
                       "不要凭空跳过尚未发生的情节。")
                if s.get("gal_auto_outline"):
                    gp += "\n\n### 全剧走向（供你把控节奏，不要直接说出来）\n" + str(s["gal_auto_outline"])
            if messages and messages[0].get("role") == "system":
                messages[0]["content"] += "\n\n" + gp
            else:
                messages.insert(0, {"role": "system", "content": gp})
            # 会话历史里若已有普通文本回复，模型会模仿它而丢掉 JSON 契约：
            # 在「当前这一轮」最末尾再压一次格式要求，优先级压过历史示例。
            for _i in range(len(messages) - 1, -1, -1):
                if messages[_i].get("role") == "user":
                    messages[_i] = dict(messages[_i])
                    messages[_i]["content"] = ((messages[_i].get("content") or "")
                        + "\n\n【格式提醒·必须遵守】上面的 GAL JSON 契约是硬性要求，"
                          "历史里任何普通文本回复都只是旧格式，本轮一律不要模仿；"
                          "本轮只输出一个 JSON 对象（以 { 开头、以 } 结尾），不要代码围栏、不要任何解释文字。")
                    break

        tool_log, mem_ops_all, text_parts, think_parts = [], [], [], []
        usage_total = {"prompt": 0, "completion": 0, "total": 0, "cached": 0, "model": api_model}
        rounds = 0
        while True:
            rr = call_model(messages, True)
            mem_ops_all.extend(rr["mem_ops"])
            if rr["reasoning"]:
                think_parts.append(rr["reasoning"])
            if rr["visible"]:
                text_parts.append(rr["visible"])
            u = rr["usage"]
            if u:
                p = int(u.get("prompt_tokens") or 0)
                c = int(u.get("completion_tokens") or 0)
                d = u.get("prompt_tokens_details") or {}
                usage_total["prompt"] += p
                usage_total["completion"] += c
                usage_total["total"] += int(u.get("total_tokens") or (p + c))
                usage_total["cached"] += int(u.get("prompt_cache_hit_tokens")
                                             or d.get("cached_tokens") or 0)
            if rr["stopped"] or not rr["tool_calls"] or rounds >= agent.MAX_ROUNDS:
                break
            # 把这一轮的说明 + 工具调用记进「本地上下文」（不进会话历史，避免刷屏）
            a_content = rr["visible"] or ""
            if rr["tool_block"]:
                a_content = (a_content + "\n\n```tool\n" + rr["tool_block"] + "\n```").strip()
            messages.append({"role": "assistant", "content": a_content})
            results = []
            for call in rr["tool_calls"]:
                name = call.get("tool")
                cargs = call.get("args") or {}
                if name == "ask_user":
                    q = (cargs.get("question") or "").strip()
                    if not q:
                        results.append("【ask_user】缺少 question 参数")
                        continue
                    qid, opts = agent.new_ask(sid, q, cargs.get("options"))
                    sse({"type": "ask", "qid": qid, "question": q, "options": opts})
                    ans = agent.wait_answer(qid, stop_event=ev)
                    if ans is None:
                        ans = "（用户未回答，已跳过）"
                    tool_log.append({"tool": "ask_user", "question": q, "options": opts, "answer": ans})
                    results.append("【ask_user】用户回答：%s" % ans)
                    continue
                if not agent_on:            # 文件工具需先开启 Agent 并设置沙箱
                    results.append("【%s】该工具需要先开启「Agent」并设置沙箱目录，当前不可用。" % name)
                    continue
                sse({"type": "tool", "name": name, "args": cargs})
                allow_out = bool(s.get("allow_outside_read"))
                t_text, undo_ops, is_write = agent.run_tool(sandbox, name, cargs,
                                                            allow_outside=allow_out)
                token = agent.register_undo(sid, undo_ops, name) if (is_write and undo_ops) else None
                tool_log.append({"tool": name, "args": cargs, "result": t_text[:500],
                                 "undo": token, "write": bool(is_write)})
                sse({"type": "tool_done", "name": name, "result": t_text[:500],
                     "undo": token, "write": bool(is_write)})
                results.append("【%s】\n%s" % (name, t_text))
            messages.append({"role": "user",
                             "content": "工具执行结果如下，请据此继续：\n\n" + "\n\n".join(results)})
            rounds += 1

        ACTIVE_STREAMS.pop(sid, None)
        text = "".join(text_parts)
        think_text = "".join(think_parts)
        usage = dict(usage_total) if usage_total["total"] else None

        # AI 自主写入：执行本轮累计的记忆操作（代码块已在流式过程中被摘除）
        mem_note = None
        try:
            mem_note = mem_tool.apply_chat_ops(sid, mem_ops_all)
        except Exception as e:
            print(f"[记忆工具] 执行失败：{e}")
        if mem_note:
            sse({"type": "mem_write", "memory": mem_note})

        # GAL：整体解析（失败则按普通文本处理，不丢内容）
        gal_data = None
        if gal_mode:
            gal_data = gal.parse_reply(text, gal_name, gal_fx)
            if gal_data:
                for seg in gal_data["dialog"]:
                    i2 = gal.match_sprite(gal_char, seg.get("emotion"), seg.get("sprite"))
                    seg["sprite_index"] = i2
                    seg["path"] = (gal_sprites[i2]["path"]
                                   if isinstance(i2, int) and 0 <= i2 < len(gal_sprites) else "")

        if text.strip() or think_text.strip():
            data2 = load_sessions()
            s2 = find_session(data2, sid)
            if s2:
                content = gal.joined_speech(gal_data["dialog"]) if gal_data else text
                msg = {"role": "assistant", "content": content}
                if gal_data:
                    first_idx = next((s.get("sprite_index") for s in gal_data["dialog"]
                                      if isinstance(s.get("sprite_index"), int)), None)
                    msg["gal"] = {
                        "dialog": gal_data["dialog"],
                        "options": gal_data["options"],
                        "bg": gal_data.get("bg", ""),
                        "bgm": gal_data.get("bgm", ""),
                        "sprite_index": first_idx,
                    }
                if think_text.strip():
                    msg["reasoning"] = think_text
                if sources:
                    msg["sources"] = slim_items(sources)   # 正文不入库，避免 sessions.json 膨胀
                if usage:
                    msg["usage"] = usage
                if mem_note:
                    msg["memory"] = mem_note
                if tool_log:
                    msg["tools"] = tool_log        # Agent 工具时间线（供刷新后回显）
                msg["ctx_cut"] = ctx_cut
                s2["messages"].append(msg)
                s2["updated_at"] = time.time()
                with _lock:
                    save_sessions(data2)

        # GAL：舞台（背景 / BGM）与选项；对白段已在流式过程中逐段推送过
        if gal_data:
            if gal_data.get("bg") or gal_data.get("bgm"):
                sse({"type": "gal_stage", "bg": gal_data.get("bg", ""), "bgm": gal_data.get("bgm", "")})
            if gal_data.get("options"):
                sse({"type": "gal_options", "options": gal_data.get("options")})

        # GAL：剧情推进
        # - 人工剧本：limited_turn 到达回合上限 → 轻量判定在 transitions 里选下一节点
        # - AI 自动剧本：到期后由模型「续写下一幕」，追加进会话剧本再切过去
        if gal_mode and gal_node and gal_story and not ev.is_set():
            try:
                round_no = int(s.get("gal_round") or 0) + 1
                nxt = gal_node["id"]
                new_node = None
                at_limit = (gal_node["type"] == "limited_turn"
                            and round_no >= max(1, int(gal_node["maxRounds"])))
                if at_limit and gal_auto:
                    d_now = load_sessions()
                    s_now = find_session(d_now, sid) or {}
                    story_now = gal.normalize_story(s_now.get("gal_auto_story"))
                    total = gal._clamp(_cfg.get("gal_auto_script_acts"), 5, 1, 12)
                    done = gal.acts_done(story_now)
                    must_end = (done + 1) >= total      # 正要展开的就是最后一幕 → 写成结局
                    reply = _quick_chat(api_key, api_url, api_model, [
                        {"role": "system", "content": "你是 Galgame 编剧，只输出一个 JSON 对象，不要解释。"},
                        {"role": "user", "content": gal.build_next_act_prompt(
                            gal_char, s_now.get("gal_auto_outline") or "", done, total,
                            recent=recent_dialog(s_now), must_end=must_end)},
                    ], max_tokens=800)
                    new_node = gal.parse_next_act(
                        reply, "a%d" % (done + 1),
                        gal._clamp(_cfg.get("gal_auto_script_rounds"), 4, 1, 12))
                    if new_node:
                        story_new = gal.append_node(story_now, new_node)
                        nxt = new_node["id"]
                        d_now2 = load_sessions()
                        s_now2 = find_session(d_now2, sid)
                        if s_now2:
                            s_now2["gal_auto_story"] = story_new
                            s_now2["gal_auto_acts_done"] = done
                            with _lock:
                                save_sessions(d_now2)
                elif at_limit:
                    reply = _quick_chat(api_key, api_url, api_model, [
                        {"role": "system", "content": "你是剧情调度器，只输出一个 JSON，不要解释。"},
                        {"role": "user", "content": gal.build_choice_prompt(gal_node)},
                    ])
                    nxt = gal.parse_node_choice(reply, gal_node) or gal_node["id"]
                d3 = load_sessions()
                s3 = find_session(d3, sid)
                if s3:
                    if nxt and nxt != gal_node["id"]:
                        s3["gal_node_id"] = nxt
                        s3["gal_round"] = 0
                    else:
                        s3["gal_round"] = round_no
                    s3["updated_at"] = time.time()
                    with _lock:
                        save_sessions(d3)
                    # 自动剧本：把新一幕的标题一起下发，前端可提示「进入下一幕」
                    cur_node = (gal.story_node(gal.normalize_story(s3.get("gal_auto_story")), nxt)
                                if (gal_auto and new_node) else None)
                    sse({"type": "gal_node", "node_id": s3.get("gal_node_id"),
                         "round": s3.get("gal_round"),
                         "title": ((cur_node or {}).get("prompt") or "")[:40],
                         "scene": (cur_node or {}).get("prompt") or "",
                         "auto": bool(gal_auto and new_node),
                         "max_rounds": (int(gal_node["maxRounds"])
                                        if gal_node["type"] == "limited_turn" else 0)})
            except Exception as e:
                print(f"[GAL] 剧情推进失败：{e}")

        if usage:
            record_usage({"ts": time.time(), "source": "chat", **usage})
            sse({"type": "usage", "usage": usage})

        final_data = load_sessions()
        final_s = find_session(final_data, sid)
        sse({"type": "done", "stopped": ev.is_set(),
             "context": session_context(final_s, system_prompt) if final_s else None,
             "sessions": slim_sessions(final_data)})


# ---------- VS Code 联动（阶段 3：外部打开 + 只读导入配置） ----------

def find_vscode_cli():
    """定位 Windows 上的 code 命令（PATH 优先，其次常见安装目录）"""
    for name in ("code", "code.cmd"):
        p = shutil.which(name)
        if p:
            return p
    la = os.environ.get("LOCALAPPDATA") or ""
    pf = os.environ.get("ProgramFiles") or ""
    pf86 = os.environ.get("ProgramFiles(x86)") or ""
    for base in (os.path.join(la, "Programs", "Microsoft VS Code"),
                 os.path.join(pf, "Microsoft VS Code"),
                 os.path.join(pf86, "Microsoft VS Code")):
        p = os.path.join(base, "bin", "code.cmd")
        if os.path.isfile(p):
            return p
    return ""


def _vscode_user_dirs():
    """VS Code 用户配置目录候选（含 Insiders）"""
    ap = os.environ.get("APPDATA") or ""
    return [os.path.join(ap, n, "User") for n in ("Code", "Code - Insiders")]


def _strip_jsonc(text):
    """去掉 JSONC 的 // 与 /* */ 注释和尾逗号，好让 json.loads 能吃"""
    out = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] not in "\r\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def vscode_import_payload():
    """只读读取本机 VS Code 的设置 / 快捷键路径 / 扩展清单（不修改任何文件）"""
    settings_path = ""
    user_dir = ""
    for d in _vscode_user_dirs():
        sp = os.path.join(d, "settings.json")
        if os.path.isfile(sp):
            settings_path = sp
            user_dir = d
            break
    if not user_dir:
        for d in _vscode_user_dirs():
            if os.path.isdir(d):
                user_dir = d
                settings_path = os.path.join(d, "settings.json")
                break
    keybindings_path = ""
    if user_dir:
        kb = os.path.join(user_dir, "keybindings.json")
        if os.path.isfile(kb):
            keybindings_path = kb

    raw = {}
    if settings_path and os.path.isfile(settings_path):
        try:
            with open(settings_path, "r", encoding="utf-8", errors="replace") as f:
                raw = json.loads(_strip_jsonc(f.read())) or {}
        except Exception:
            raw = {}

    def pick(dotted):
        # VS Code 的 settings.json 用的是「扁平点号键」（如 "editor.fontSize"），
        # 优先按字面键取；取不到再按嵌套对象兜底。
        if dotted in raw:
            return raw[dotted]
        cur = raw
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return None
            cur = cur[part]
        return cur

    settings = {}
    fs = pick("editor.fontSize")
    ts = pick("editor.tabSize")
    if isinstance(fs, (int, float)):
        settings["fontSize"] = int(fs)
    if isinstance(ts, (int, float)):
        settings["tabSize"] = int(ts)
    ff = pick("editor.fontFamily")
    if isinstance(ff, str) and ff.strip():
        settings["fontFamily"] = ff.strip()
    ww = pick("editor.wordWrap")
    if isinstance(ww, str) and ww.strip():
        settings["wordWrap"] = ww.strip()
    rw = pick("editor.renderWhitespace")
    if isinstance(rw, str) and rw.strip():
        settings["renderWhitespace"] = rw.strip()
    mz = pick("editor.mouseWheelZoom")
    if isinstance(mz, bool):
        settings["mouseWheelZoom"] = mz
    settings["_count"] = len(raw)

    ext_dir = os.path.join(os.path.expanduser("~"), ".vscode", "extensions")
    exts = []
    if os.path.isdir(ext_dir):
        try:
            for name in sorted(os.listdir(ext_dir)):
                if name.startswith(".") or name == "extensions.json":
                    continue
                if not os.path.isdir(os.path.join(ext_dir, name)):
                    continue
                parts = name.rsplit("-", 1)
                exts.append(parts[0] if len(parts) == 2 else name)
        except Exception:
            exts = []

    return {
        "ok": True,
        "settings": settings,
        "extensions": exts,
        "paths": {
            "settings": settings_path or "",
            "keybindings": keybindings_path or "",
            "extensions": ext_dir if os.path.isdir(ext_dir) else "",
        },
    }


def create_server(host="127.0.0.1", port=0):
    # 启动时把旧的 user_profile.auto 迁移进「用户画像」知识库（只做一次）
    try:
        migrate_profile_to_kb()
    except Exception as e:
        print(f"[知识库] 迁移失败：{e}")
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    # 后台预热语音识别模型：首次点「语音输入」即可直接用，不必干等几秒
    threading.Thread(target=speech.warmup, daemon=True).start()
    return httpd, httpd.server_address[1]
