# -*- coding: utf-8 -*-
"""HTTP 处理：静态资源、REST API 与聊天流式(SSE)"""
import json
import mimetypes
import os
import queue
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs


from app.store import APP_VERSION, WEB_DIR
from app.sessions import load_sessions, find_session, slim_sessions
from app.context import current_context
from app.knowledge import migrate_profile_to_kb
from app import notes as noteslib
from app import speech
from app import agent
from app import events
from app import scheduler


# ---- 以下模块由 P1 重构从本文件抽出（原为同文件的模块级函数） ----
from app.uploads import sweep_uploads

from app import calendar as calendar_lib, db, memory, task_engine
from app.registry import get as route_get

from app.api import misc
from app.api import sessions
from app.api import savepoints
from app.api import kb
from app.api import notes
from app.api import gal_character
from app.api import gal_script
from app.api import gal_media
from app.api import gal_tts
from app.api import sandbox
from app.api import media_in
from app.api import terminal as term_api        # noqa: F401  自注册 /api/ide/term/* 路由
from app.api import ide_intel as ide_intel_api  # noqa: F401  自注册 /api/ide/lsp/* 路由（编辑器诊断 / 大纲 / 跳转 / 重命名）
from app.api import ide_edit as ide_edit_api    # noqa: F401  自注册 /api/ide/replace 路由（全局搜索替换）
from app.api import ide_tex as ide_tex_api      # noqa: F401  自注册 /api/ide/tex/* 路由（LaTeX 编译）
from app.api import ide_tools as ide_tools_api  # noqa: F401  自注册 /api/ide/{py,c,format} 路由（ruff / gcc）
from app.api import codeindex as codeindex_api  # noqa: F401  自注册 /api/codeindex/* 路由（全仓语义索引）
from app.api import debugger as debugger_api    # noqa: F401  自注册 /api/debug/* 路由（IDE 调试器）
from app.api import kb_stats as kb_stats_api    # noqa: F401  自注册 /api/kb/stats 路由（知识库 token 预估）
from app.api import git as git_api              # noqa: F401  自注册 /api/git/* 路由
from app.api import hall
from app import orchestrator

from app.api import chat
from app.api import autopilot as autopilot_api
from app.api import tasks as tasks_api
# 以下两个模块通过 @route 自注册（新功能按这个模式加，不用改本文件的路由表）
from app.api import work as work_api          # noqa: F401
from app.api import trajectory as trajectory_api  # noqa: F401  轨迹（模型视角的事件账本）
from app.api import calendar as calendar_api  # noqa: F401
from app.api import workflow as workflow_api  # noqa: F401
from app.api import memory as memory_api      # noqa: F401
from app.api import backup as backup_api  # noqa: F401
from app.api import bili as bili_api  # noqa: F401
from app.api import hall_stock as hall_stock_api  # noqa: F401
from app.api import hall_interview as hall_interview_api  # noqa: F401
from app.api import dojo as dojo_api  # noqa: F401
from app.api import skills as skills_api  # noqa: F401
from app.api import market as market_api  # noqa: F401  插件市场：DSH 生态只读索引
from app.api import plugins as plugins_api  # noqa: F401  插件：工具插件 / MCP / 内置发现源
from app.api import explore as explore_api  # noqa: F401  后台探索批次：前端轮询进度
from app.api import image as image_api  # noqa: F401  自注册 /api/image/* 路由（本地 ComfyUI 出图）
from app.api import agents as agents_api  # noqa: F401  自注册 /api/agents 路由（子代理定义）
from app.api import computer_bridge

TOKEN = secrets.token_urlsafe(24)

# 每个响应都带的安全头：禁止被外部页面 iframe 套住（点击劫持审批卡）、禁止 MIME 嗅探。
SECURITY_HEADERS = (
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "SAMEORIGIN"),
    ("Content-Security-Policy", "frame-ancestors 'self'"),
    ("Referrer-Policy", "no-referrer"),
)

_BRIDGE_PREFIX = "/api/computer/bridge/"


def same_origin_ok(origin, host):
    """Origin 必须是本页面自身；没有 Origin（同源 GET / 非浏览器客户端）放行。

    浏览器发起的跨源请求一定带 Origin（扩展是 chrome-extension://），
    这里直接拒绝，避免「Token 被外部页面 / 扩展读到后调本地 API」。
    """
    o = str(origin or "").strip()
    if not o:
        return True
    try:
        p = urlparse(o)
    except ValueError:
        return False
    if p.scheme not in ("http", "https") or not p.netloc:
        return False
    return p.netloc.lower() == str(host or "").strip().lower()


def static_file_path(rel):
    """/static/<rel> → web 目录内的真实文件；越界返回空串。

    旧实现用 startswith(WEB_DIR) 判断，`../webXXX` 这类同级目录能混进来；
    这里用 realpath + commonpath 做真正的包含判断。
    """
    root = os.path.realpath(WEB_DIR)
    full = os.path.realpath(os.path.join(WEB_DIR, rel))
    try:
        if os.path.commonpath([full, root]) != root:
            return ""
    except ValueError:
        return ""
    return full if os.path.isfile(full) else ""

# 静态资源内存缓存：路径 -> (mtime, size, bytes)；mtime/size 变了才重读。
# 原先每个 /static/* 请求都 open+read 一次，而首屏有 20+ 个脚本/样式，纯属重复读盘。
_static_cache = {}
_static_lock = threading.Lock()

# 当前连着 SSE（/api/events）的界面数。窗口重开时会新建一条 SSE，
# 主程序用它判断「窗口还开着吗」：>0 就不能在保活宽限期结束后退出。
_clients_lock = threading.Lock()
_client_count = 0


def active_clients():
    with _clients_lock:
        return _client_count


def _client_enter():
    global _client_count
    with _clients_lock:
        _client_count += 1


def _client_leave():
    global _client_count
    with _clients_lock:
        _client_count = max(0, _client_count - 1)


def _load_static_cached(path, mtime, size):
    with _static_lock:
        hit = _static_cache.get(path)
        if hit and hit[0] == mtime and hit[1] == size:
            return hit[2]
    with open(path, "rb") as f:
        data = f.read()
    with _static_lock:
        _static_cache[path] = (mtime, size, data)
    return data

# ============================================================
# HTTP 处理
# ============================================================

class Handler(BaseHTTPRequestHandler):
    server_version = "Galgame_monster/" + APP_VERSION
    # HTTP/1.1：允许 keep-alive 复用连接。注意：无 Content-Length 的流式响应
    #（/api/chat、/api/events）必须显式带 `Connection: close`，否则客户端会一直等。
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass  # 静音访问日志

    # ---------- 基础工具 ----------
    def _send(self, code, body, ctype="application/json; charset=utf-8",
              cache="no-store", extra_headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        for k, v in SECURITY_HEADERS:
            self.send_header(k, v)
        # 浏览器扩展桥：只对扩展来源、且只对 bridge 端点放行 CORS。
        # 其它 API 一律不回显 ACAO，避免任意扩展跨源读响应。
        origin = self.headers.get("Origin") or ""
        if (origin.startswith(("chrome-extension://", "moz-extension://"))
                and urlparse(self.path).path.startswith(_BRIDGE_PREFIX)):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
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
            raw = self.rfile.read(length)
            self._body_read = True
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _body_raw(self):
        """按 Content-Length 读取原始字节（用于音频等非 JSON 请求体）"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            self._body_read = True
            return b""
        raw = self.rfile.read(length)
        self._body_read = True
        return raw

    def _authed(self):
        return self.headers.get("X-Auth-Token") == TOKEN

    def _host_ok(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    # ---------- 路由 ----------
    def do_OPTIONS(self):
        """扩展桥的 CORS 预检（正常走 ?k= 简单请求用不到，留着兜底）。"""
        origin = self.headers.get("Origin") or ""
        if not origin.startswith(("chrome-extension://", "moz-extension://")):
            return self._send(403, "forbidden", "text/plain; charset=utf-8")
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Bridge-Key")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/" or path == "/index.html":
            return self._static("index.html")
        if path.startswith("/static/"):
            return self._static(path[len("/static/"):])
        if path.startswith("/api/"):
            # 扩展桥用自己的 X-Bridge-Key 鉴权（扩展拿不到每进程随机的 X-Auth-Token）
            if path == "/api/computer/bridge/poll":
                return computer_bridge.api_poll(self)
            if not self._host_ok() or not self._authed():
                return self._json({"error": "unauthorized"}, 403)
            if not same_origin_ok(self.headers.get("Origin"), self.headers.get("Host")):
                return self._json({"error": "forbidden origin"}, 403)
            if path == "/api/computer/bridge/status":
                return computer_bridge.api_status(self)
            fn = route_get("GET", path)
            if fn:
                return fn(self)
            if path == "/api/state":
                return misc._api_state(self)
            if path == "/api/stt/status":
                return self._json({"ok": True, "speech": speech.status()})
            if path == "/api/usage":
                return misc._api_usage(self, parse_qs(parsed.query))
            if path == "/api/events":
                return self._api_events()
            return self._json({"error": "not found"}, 404)
        self._send(404, "not found", "text/plain; charset=utf-8")

    def _api_events(self):
        """常驻 SSE：服务端主动推送（定时提醒 / AI 主动消息）。

        与 /api/chat 不同，这个连接**不随请求结束而关闭** —— 它是「没人提问时也能推」
        的唯一通道。15 秒无事件就发一次心跳注释，防止空闲连接被中间设备掐断。
        """
        q = events.subscribe()
        _client_enter()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")   # 无 Content-Length 的流：靠关连接结束
            self.end_headers()
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    obj = q.get(timeout=15)
                    chunk = "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"
                except queue.Empty:
                    chunk = ": ping\n\n"
                self.wfile.write(chunk.encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            events.unsubscribe(q)
            _client_leave()

    def do_POST(self):
        """POST 入口：保证 keep-alive 不因未读请求体而串包。

        如果处理函数没有读取 body（鉴权失败 / 404 / 提前返回），剩余字节会被
        HTTP/1.1 当成下一个请求行，出现类似 ``{"op":"list"}GET`` 的 501。
        这里统一在 finally 里把这种连接关掉，避免污染后续请求。
        """
        self._body_read = False
        try:
            self._do_post()
        finally:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length > 0 and not getattr(self, "_body_read", False):
                self.close_connection = True

    def _do_post(self):
        path = urlparse(self.path).path
        if not path.startswith("/api/"):
            return self._send(404, "not found", "text/plain; charset=utf-8")
        if path in ("/api/computer/bridge/result", "/api/computer/bridge/hello",
                    "/api/computer/bridge/event"):
            fn = {"/api/computer/bridge/result": computer_bridge.api_result,
                  "/api/computer/bridge/hello": computer_bridge.api_hello,
                  "/api/computer/bridge/event": computer_bridge.api_event}[path]
            return fn(self)
        if not self._host_ok() or not self._authed():
            return self._json({"error": "unauthorized"}, 403)
        if not same_origin_ok(self.headers.get("Origin"), self.headers.get("Host")):
            return self._json({"error": "forbidden origin"}, 403)
        routes = {
            "/api/chat": chat._api_chat,
            "/api/chat/attach": chat._api_chat_attach,
            "/api/tasks": tasks_api._api_tasks,
            "/api/autopilot": autopilot_api._api_autopilot,
            "/api/stop": misc._api_stop,
            "/api/workflow/status": orchestrator.api_status,
            "/api/workflow/pause": orchestrator.api_pause,
            "/api/workflow/resume": orchestrator.api_resume,
            "/api/workflow/cancel": orchestrator.api_cancel,
            "/api/recognize": media_in._api_recognize,
            "/api/stt": media_in._api_stt,
            "/api/stt/start": media_in._api_stt_start,
            "/api/stt/chunk": media_in._api_stt_chunk,
            "/api/stt/finish": media_in._api_stt_finish,
            "/api/stt/polish": media_in._api_stt_polish,
            "/api/session/new": sessions._api_session_new,
            "/api/open/path": misc._api_open_path,
            "/api/app/context-menu": misc._api_app_context_menu,
            "/api/session/rename": sessions._api_session_rename,
            "/api/session/pin": sessions._api_session_pin,
            "/api/session/reorder": sessions._api_session_reorder,
            "/api/session/group": sessions._api_session_group,
            "/api/session/delete": sessions._api_session_delete,
            "/api/session/select": sessions._api_session_select,
            "/api/session/messages": sessions._api_session_messages,
            "/api/session/search": sessions._api_session_search,
            "/api/session/clear": sessions._api_session_clear,
            "/api/session/export": sessions._api_session_export,
            "/api/config": misc._api_save_config,
            "/api/compose": misc._api_compose,
            "/api/profile": misc._api_save_profile,
            "/api/message/withdraw": sessions._api_message_withdraw,
            "/api/attach/get": sessions._api_attach_get,
            "/api/kb/list": kb._api_kb_list,
            "/api/kb/get": kb._api_kb_get,
            "/api/kb/create": kb._api_kb_create,
            "/api/kb/delete": kb._api_kb_delete,
            "/api/kb/save": kb._api_kb_save,
            "/api/kb/write": kb._api_kb_write,
            "/api/kb/export": kb._api_kb_export,
            "/api/memory/get": kb._api_memory_get,
            "/api/memory/save": kb._api_memory_save,
            "/api/save/list": savepoints._api_save_list,
            "/api/save/make": savepoints._api_save_make,
            "/api/save/restore": savepoints._api_save_restore,
            "/api/save/branch": savepoints._api_save_branch,
            "/api/save/delete": savepoints._api_save_delete,
            "/api/memory/undo": kb._api_memory_undo,
            "/api/context/compress": kb._api_context_compress,
            "/api/context/summary": kb._api_context_summary,
            "/api/context/restore": kb._api_context_restore,
            "/api/session/bind": kb._api_session_bind,
            "/api/session/agent": sandbox._api_session_agent,
            "/api/agent/answer": sandbox._api_agent_answer,
            "/api/agent/undo": sandbox._api_agent_undo,
            "/api/agent/turn_undo": sandbox._api_agent_turn_undo,
            "/api/agent/approve": sandbox._api_agent_approve,
            "/api/plan/review": sandbox._api_plan_review,
            "/api/agent/explain": sandbox._api_agent_explain,
            "/api/agent/rules": sandbox._api_agent_rules,
            "/api/agent/rules/preset": sandbox._api_agent_rules_preset,
            "/api/sandbox/pick": sandbox._api_sandbox_pick,
            "/api/sandbox/tree": sandbox._api_sandbox_tree,
            "/api/sandbox/file": sandbox._api_sandbox_file,
            "/api/sandbox/raw": sandbox._api_sandbox_raw,
            "/api/sandbox/save": sandbox._api_sandbox_save,
            "/api/sandbox/new": sandbox._api_sandbox_new,
            "/api/sandbox/rename": sandbox._api_sandbox_rename,
            "/api/sandbox/delete": sandbox._api_sandbox_delete,
            "/api/sandbox/search": sandbox._api_sandbox_search,
            "/api/sandbox/open_vscode": sandbox._api_sandbox_open_vscode,
            "/api/gal/characters": gal_character._api_gal_characters,
            "/api/gal/character/get": gal_character._api_gal_character_get,
            "/api/gal/character/save": gal_character._api_gal_character_save,
            "/api/gal/character/delete": gal_character._api_gal_character_delete,
            "/api/gal/asset": gal_character._api_gal_asset,
            "/api/gal/character/files": gal_character._api_gal_character_files,
            "/api/gal/character/upload": gal_character._api_gal_character_upload,
            "/api/gal/character/export": gal_character._api_gal_character_export,
            "/api/gal/character/import": gal_character._api_gal_character_import,
            "/api/gal/character/import_dir": gal_character._api_gal_character_import_dir,
            "/api/gal/packs": gal_character._api_gal_packs,
            "/api/window/theme": misc._api_window_theme,
            "/api/gal/character/draft": gal_character._api_gal_character_draft,
            "/api/gal/script": gal_script._api_gal_script,
            "/api/gal/story/get": gal_script._api_gal_story_get,
            "/api/gal/story/save": gal_script._api_gal_story_save,
            "/api/gal/media": gal_script._api_gal_media,
            "/api/gal/backgrounds": gal_media._api_gal_backgrounds,
            "/api/gal/background/save": gal_media._api_gal_background_save,
            "/api/gal/background/upload": gal_media._api_gal_background_upload,
            "/api/gal/background/delete": gal_media._api_gal_background_delete,
            "/api/gal/bgms": gal_media._api_gal_bgms,
            "/api/gal/bgm/save": gal_media._api_gal_bgm_save,
            "/api/gal/bgm/upload": gal_media._api_gal_bgm_upload,
            "/api/gal/bgm/delete": gal_media._api_gal_bgm_delete,
            "/api/gal/stats/reset": gal_tts._api_gal_stats_reset,
            "/api/gal/effects": gal_script._api_gal_effects,
            "/api/gal/journal": gal_script._api_gal_journal,
            "/api/gal/effect/audio": gal_script._api_gal_effect_audio,
            "/api/gal/tts": gal_tts._api_gal_tts,
            "/api/gal/tts/active": gal_tts._api_gal_tts_active,
            "/api/gal/tts/voices": gal_tts._api_gal_tts_voices,
            "/api/gal/session": gal_tts._api_gal_session,
            "/api/vscode/import": sandbox._api_vscode_import,
            "/api/vscode/theme": sandbox._api_vscode_theme,
            "/api/balance": misc._api_balance,
            "/api/notes/tree": notes._api_notes_tree,
            "/api/notes/get": notes._api_notes_get,
            "/api/notes/save": notes._api_notes_save,
            "/api/notes/new": notes._api_notes_new,
            "/api/notes/delete": notes._api_notes_delete,
            "/api/notes/rename": notes._api_notes_rename,
            "/api/notes/folder/new": notes._api_notes_folder_new,
            "/api/notes/folder/delete": notes._api_notes_folder_delete,
            "/api/notes/graph": notes._api_notes_graph,
            "/api/notes/backlinks": notes._api_notes_backlinks,
            "/api/notes/search": notes._api_notes_search,
            # 功能大厅（新玩法按 /api/hall/<功能id>/… 分组挂在这里）
            "/api/hall/geo/new": hall._api_hall_geo_new,
            "/api/hall/geo/guess": hall._api_hall_geo_guess,
            "/api/hall/geo/ai": hall._api_hall_geo_ai,
            "/api/hall/geo/analyze": hall._api_hall_geo_analyze,
            "/api/hall/geo/reveal": hall._api_hall_geo_reveal,
            "/api/hall/math/new": hall._api_hall_math_new,
            "/api/hall/math/answer": hall._api_hall_math_answer,
            "/api/hall/math/abilities": hall._api_hall_math_abilities,
            "/api/hall/math/finish": hall._api_hall_math_finish,
            "/api/hall/math/paper/meta": hall._api_hall_math_paper_meta,
            "/api/hall/math/paper/new": hall._api_hall_math_paper_new,
            "/api/hall/math/paper/grade": hall._api_hall_math_paper_grade,
            "/api/hall/math/proofs": hall._api_hall_math_proofs,
            "/api/hall/math/proof/grade": hall._api_hall_math_proof_grade,
            "/api/hall/dojo/packs": dojo_api._api_dojo_packs,
            "/api/hall/dojo/start": dojo_api._api_dojo_start,
            "/api/hall/dojo/judge": dojo_api._api_dojo_judge,
            "/api/hall/dojo/import/parse": dojo_api._api_dojo_import_parse,
            "/api/hall/dojo/import/draft": dojo_api._api_dojo_import_draft,
            "/api/hall/dojo/import/commit": dojo_api._api_dojo_import_commit,
            "/api/hall/dojo/packs/delete": dojo_api._api_dojo_packs_delete,
            "/api/hall/dojo/packs/rename": dojo_api._api_dojo_packs_rename,
            "/api/hall/dojo/packs/export": dojo_api._api_dojo_packs_export,
            "/api/hall/dojo/packs/import": dojo_api._api_dojo_packs_import,
            "/api/hall/dojo/assess/start": dojo_api._api_dojo_assess_start,
            "/api/hall/dojo/assess/answer": dojo_api._api_dojo_assess_answer,
            "/api/hall/dojo/assess/finish": dojo_api._api_dojo_assess_finish,
            "/api/hall/dojo/repro/list": dojo_api._api_dojo_repro_list,
            "/api/hall/dojo/repro/get": dojo_api._api_dojo_repro_get,
            "/api/hall/dojo/repro/run": dojo_api._api_dojo_repro_run,
            "/api/hall/dojo/repro/create": dojo_api._api_dojo_repro_create,
            "/api/hall/dojo/case/list": dojo_api._api_dojo_case_list,
            "/api/hall/dojo/case/get": dojo_api._api_dojo_case_get,
            "/api/hall/dojo/case/start": dojo_api._api_dojo_case_start,
            "/api/hall/dojo/case/answer": dojo_api._api_dojo_case_answer,
            "/api/hall/dojo/case/finish": dojo_api._api_dojo_case_finish,
            "/api/hall/dojo/research/list": dojo_api._api_dojo_research_list,
            "/api/hall/dojo/paper/index": dojo_api._api_dojo_paper_index,
            "/api/hall/dojo/research/get": dojo_api._api_dojo_research_get,
            "/api/hall/dojo/research/start": dojo_api._api_dojo_research_start,
            "/api/hall/dojo/research/state": dojo_api._api_dojo_research_state,
            "/api/hall/dojo/research/answer": dojo_api._api_dojo_research_answer,
            "/api/hall/dojo/research/artifact": dojo_api._api_dojo_research_artifact,
            "/api/hall/dojo/research/revise": dojo_api._api_dojo_research_revise,
            "/api/hall/dojo/research/finish": dojo_api._api_dojo_research_finish,
            "/api/hall/dojo/research/records/list": dojo_api._api_dojo_research_records_list,
            "/api/hall/dojo/research/records/get": dojo_api._api_dojo_research_records_get,
            "/api/hall/dojo/research/records/delete": dojo_api._api_dojo_research_records_delete,
            "/api/hall/dojo/authoring/syllabi": dojo_api._api_dojo_authoring_syllabi,
            "/api/hall/dojo/authoring/syllabus": dojo_api._api_dojo_authoring_syllabus,
            "/api/hall/dojo/authoring/drafts": dojo_api._api_dojo_authoring_drafts,
            "/api/hall/dojo/authoring/generate": dojo_api._api_dojo_authoring_generate,
            "/api/hall/dojo/authoring/draft": dojo_api._api_dojo_authoring_draft_get,
            "/api/hall/dojo/authoring/update": dojo_api._api_dojo_authoring_update,
            "/api/hall/dojo/authoring/reject": dojo_api._api_dojo_authoring_reject,
            "/api/hall/dojo/authoring/commit": dojo_api._api_dojo_authoring_commit,
        }
        fn = routes.get(path) or route_get("POST", path)
        if not fn:
            return self._json({"error": "not found"}, 404)
        try:
            fn(self)
        except Exception as e:
            print(f"[服务] {path} 出错：{e}")
            self._json({"error": str(e)}, 500)

    # ---------- 静态资源 ----------
    def _static(self, rel):
        if not (self._host_ok()):
            return self._send(403, "forbidden", "text/plain; charset=utf-8")
        path = static_file_path(rel)
        if not path:
            return self._send(404, "not found", "text/plain; charset=utf-8")

        # index.html 要注入 CSRF Token：不可缓存，每次现读
        if path.endswith(".html"):
            with open(path, "rb") as f:
                data = f.read().replace(b"%%TOKEN%%", TOKEN.encode())
            return self._send(200, data, "text/html; charset=utf-8")

        # 其余静态资源：ETag（mtime+size）+ 内存缓存 + 304。
        # 原先固定 no-store，导致每次刷新/重开窗口都全量重下 20+ 个脚本与样式。
        st = os.stat(path)
        etag = '"%x-%x"' % (int(st.st_mtime), st.st_size)
        if etag in (self.headers.get("If-None-Match") or ""):
            return self._send(304, b"", "text/plain", cache="no-cache",
                              extra_headers={"ETag": etag})
        data = _load_static_cached(path, st.st_mtime, st.st_size)
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if path.endswith(".css"):
            ctype = "text/css; charset=utf-8"
        elif path.endswith(".js"):
            ctype = "application/javascript; charset=utf-8"
        elif path.endswith(".woff2"):
            ctype = "font/woff2"
        elif path.endswith(".woff"):
            ctype = "font/woff"
        elif path.endswith(".ttf"):
            ctype = "font/ttf"
        self._send(200, data, ctype, cache="no-cache", extra_headers={"ETag": etag})

    # ---------- API ----------

    def _ok_sessions(self, data):
        """统一返回精简后的会话数据（只带当前会话的消息）与上下文占用"""
        self._json({"ok": True, "sessions": slim_sessions(data),
                    "context": current_context(data)})


    # ---------- 会话存档 / 倒带 ----------


    # ---------- GAL 演出模式（见 docs/GAL_MODE_DESIGN.md） ----------


    # ---------- 知识库 / 记忆 ----------


    # ---------- 内置笔记库（Obsidian 核心版） ----------

    def _notes_state(self):
        """树 + 图谱一起回，前端一次刷新到位"""
        return {"ok": True, "tree": noteslib.list_tree(), "graph": noteslib.graph()}


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


def create_server(host="127.0.0.1", port=0):
    # 新域数据库：任务 / 日程 / 产物。建表幂等；旧数据（sessions.json）不动。
    try:
        db.init_db()
        task_engine.recover()          # 上次没跑完的任务统一转 paused，不自动重跑
        calendar_lib.migrate_legacy_reminders()   # 旧提醒并入 events（幂等）
    except Exception as e:
        print(f"[工作台] 数据库初始化失败：{e}")
    # 功能大厅模拟股市：独立 SQLite（data/stock.db），建表幂等
    try:
        from app import stock as stock_lib
        stock_lib.init_db()
    except Exception as e:
        print(f"[模拟股市] 数据库初始化失败：{e}")
    # 启动时把旧的 user_profile.auto 迁移进「用户画像」知识库（只做一次）
    try:
        migrate_profile_to_kb()
    except Exception as e:
        print(f"[知识库] 迁移失败：{e}")
    # 启动时清理孤儿附件（已删除会话的目录 / 不再被引用的文件）
    try:
        sweep_uploads()
    except Exception as e:
        print(f"[附件清理] 失败：{e}")
    # 启动时把随项目发布的示例素材（resources/presets/）播种到 data/（缺失才复制，不覆盖用户数据）
    try:
        from app import presets
        n = presets.seed_presets()
        if n:
            print(f"[示例素材] 已播种 {n} 个文件到 data/")
    except Exception as e:
        print(f"[示例素材] 播种失败：{e}")
    # 预设 Skill：每次启动补齐「新增且没被删除」的预设，用户改过 / 删过的都不动
    try:
        from app import skills as skills_lib
        n = skills_lib.seed_defaults()
        if n:
            print(f"[Skill] 已播种 {n} 个预设")
    except Exception as e:
        print(f"[Skill] 预设播种失败：{e}")
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    # 后台预热语音识别模型：首次点「语音输入」即可直接用，不必干等几秒
    threading.Thread(target=speech.warmup, daemon=True).start()
    # 资源管理器右键菜单：把待打开目录推给已连接的界面（无人连接时留在文件里等下次）
    try:
        from app import open_request
        open_request.start()
    except Exception as e:
        print(f"[右键菜单] 投递线程启动失败：{e}")
    # 定时提醒 / AI 主动聊天的调度线程
    try:
        scheduler.start()
    except Exception as e:
        print(f"[调度] 启动失败：{e}")
    # 自动化目标：进程重启后把未完成的目标接回线程，并起一个看门狗兜住线程意外退出。
    # 这一步必须在 HTTP 服务可用之前完成：自动化不走前端请求，没人点也会自己继续。
    try:
        from app import autopilot as autopilot_lib
        n = autopilot_lib.recover()
        autopilot_lib.start_watchdog()
        if n:
            print(f"[自动化] 已自动恢复 {n} 个未完成目标")
    except Exception as e:
        print(f"[自动化] 恢复失败：{e}")
    # 记忆索引放在「画像迁移 / 示例素材播种」之后：首次启动也能索引到画像库
    try:
        memory.reindex()
    except Exception as e:
        print(f"[记忆] 索引重建失败：{e}")
    return httpd, httpd.server_address[1]
