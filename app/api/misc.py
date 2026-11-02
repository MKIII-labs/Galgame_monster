# -*- coding: utf-8 -*-
import os
import platform
import re
from urllib.parse import parse_qs, urlparse

from app.registry import route
from app.store import (ACTIVE_STREAMS, APP_VERSION, DATA_DIR, ROOT, _lock)
from app import open_request, open_session, session_status, win_context_menu
from app.learning import (BUILTIN_PRESET_LABEL, load_builtin_learning_presets)
from app.search import (SEARCH_PROVIDERS, chat_search_available,
                        effective_search_provider)
from app.imaging import chat_vision_available
from app.usage import aggregate_usage
from app.context import current_context
from app.config import (default_config, get_api_conf, load_config, save_config)
from app.knowledge import (load_kb_index, relations_payload)
from app.profile import (DEFAULT_PROFILE, clean_rules, load_profile, save_profile)
from app.sessions import (load_sessions, slim_sessions)
from app.balance import query_balance
from app import llm, mem_tool, observe as observe_store, winframe
from app import expression

# 提醒开关相关的配置键：保存这些键时要同步 Windows 计划任务
REMINDER_SWITCH_KEYS = {
    "reminders_enabled", "reminder_inapp_enabled", "reminder_system_enabled",
    "reminder_calendar_enabled", "auto_chat_enabled",
}


def _api_state(h):
        data = load_sessions()
        h._json({
            "config": load_config(),
            "profile": load_profile(),
            "sessions": slim_sessions(data),
            "context": current_context(data),
            "kbs": load_kb_index()["kbs"],
            "builtin_presets": load_builtin_learning_presets(),
            "builtin_preset_label": BUILTIN_PRESET_LABEL,
            "search_providers": SEARCH_PROVIDERS,
            # 能力态：默认用聊天模型自带搜索 / 识图，做不到再回落独立 API
            "chat_search_available": chat_search_available(),
            "chat_vision_available": chat_vision_available(),
            "search_effective": effective_search_provider(),
            "relations": relations_payload(),
            # 哪些会话正在生成：断线重连时前端据此判断「任务真的结束了没」，
            # 避免 attach 一失败就提前收尾（表现成回答说到一半没了）。
            "streaming": sorted(ACTIVE_STREAMS.keys()),
            # 会话运行态：sid -> approval / ask；侧栏据此显示「待批准 / 待回答」
            "pending": session_status.snapshot(),
            # 右键菜单冷启动时的待打开目录（SSE 还没连上，走这里兜底）
            "pending_open": str((open_request.pending() or {}).get("path") or ""),
            # 资源管理器右键菜单是否已注册（设置页按钮的初始文案）
            "context_menu": win_context_menu.status(),
        })


@route("/api/app/context-menu")
def _api_app_context_menu(h):
    """注册 / 移除资源管理器右键菜单（只写当前用户 HKCU）。"""
    action = str(h._body().get("action") or "status").strip().lower()
    if action == "install":
        ok, msg = win_context_menu.install(icon=os.path.join(ROOT, "app.ico"))
    elif action in ("remove", "uninstall"):
        ok, msg = win_context_menu.uninstall()
    else:
        ok, msg = True, "当前状态"
    return h._json({"ok": ok, "message": msg, "status": win_context_menu.status()})


@route("/api/open/path")
def _api_open_path(h):
    """右键菜单 / 界面调用：把目录落成会话（同目录优先复用最近的），并消费掉请求文件。"""
    path = open_session.norm_dir(h._body().get("path"))
    if not path:
        return h._json({"ok": False, "error": "目录不存在"}, 400)
    try:
        s, created = open_session.open_dir_session(path)
    except Exception as e:                 # noqa: BLE001  会话文件写失败要能看见原因
        return h._json({"ok": False, "error": "打开目录失败：%s" % e}, 500)
    open_request.clear()
    return h._json({"ok": True, "id": s["id"], "created": created, "path": path,
                    "sessions": slim_sessions(load_sessions()),
                    "context": current_context(load_sessions())})


@route("/api/settings/defaults")
def _api_settings_defaults(h):
    """设置页「恢复默认」用的权威默认值：配置 + 用户画像。"""
    h._json({"ok": True, "config": default_config(), "profile": dict(DEFAULT_PROFILE)})


@route("/api/app/info")
def _api_app_info(h):
    """「数据与高级」页：版本与运行环境。"""
    h._json({
        "ok": True,
        "version": APP_VERSION,
        "data_dir": DATA_DIR,
        "python": platform.python_version(),
        "platform": platform.platform(),
    })


@route("/api/app/open-data-dir")
def _api_app_open_data_dir(h):
    """在资源管理器里打开数据目录（只打开文件夹，不执行任何文件）。"""
    opener = getattr(os, "startfile", None)
    if opener is None:
        return h._json({"ok": False, "error": "当前系统不支持打开文件夹"}, 500)
    try:
        opener(DATA_DIR)
    except OSError as e:
        return h._json({"ok": False, "error": "打开文件夹失败：%s" % e}, 500)
    return h._json({"ok": True, "path": DATA_DIR})


def _api_save_config(h):
        incoming = h._body()
        cfg = load_config()
        cfg.update(incoming)
        with _lock:
            save_config(cfg)
        # 配置变更后让外部工具与钩子重新读：MCP 断开旧连接、hooks 清掉 3 秒缓存
        try:
            from app import mcp, hooks
            mcp.reset()
            hooks.clear_cache()
        except Exception:                    # noqa: BLE001  保存成功不能被清理失败拖累
            pass
        # 提醒开关变更：同步注销 / 恢复 Windows 计划任务（否则程序关了仍会弹）
        if REMINDER_SWITCH_KEYS & set(incoming):
            try:
                from app import calendar as calendar_lib
                from app import scheduler
                scheduler.apply_switches()
                calendar_lib.apply_system_switch()
            except Exception:                # noqa: BLE001
                pass
        h._json({"ok": True})


@route("/api/open/folder")
def _api_open_folder(h):
    """在文件管理器里打开一个目录（只允许当前会话实际用过的沙箱路径）。"""
    path = str(h._body().get("path") or "").strip()
    if not path or len(path) > 1000:
        return h._json({"ok": False, "error": "缺少目录路径"}, 400)
    target = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(target):
        return h._json({"ok": False, "error": "目录不存在"}, 404)
    known = set()
    for s in (load_sessions().get("sessions") or []):
        raw = str(s.get("sandbox") or "").strip()
        if raw:
            known.add(os.path.normcase(os.path.abspath(raw)))
    if os.path.normcase(target) not in known:
        return h._json({"ok": False, "error": "只能打开当前会话用过的目录"}, 403)
    opener = getattr(os, "startfile", None)
    if opener is None:
        return h._json({"ok": False, "error": "当前系统不支持打开文件夹"}, 500)
    try:
        opener(target)
    except OSError as e:
        return h._json({"ok": False, "error": "打开失败：%s" % e}, 500)
    return h._json({"ok": True, "path": target})


@route("/api/mcp/test")
def _api_mcp_test(h):
    """设置页「测试连接」：临时起一个 MCP 连接，拉一次工具清单。"""
    body = h._body()
    sp = body.get("server") if isinstance(body.get("server"), dict) else body
    from app import mcp
    ok, text = mcp.test_server(sp or {})
    h._json({"ok": ok, "text": text})


def _api_save_profile(h):
        incoming = h._body()
        profile = load_profile()
        for k in ("manual", "auto", "auto_learn_enabled", "learning_prompt_custom"):
            if k in incoming:
                profile[k] = incoming[k]
        if "rules" in incoming:
            profile["rules"] = clean_rules(incoming.get("rules"))
        with _lock:
            save_profile(profile)
        h._json({"ok": True, "rules": profile.get("rules") or []})


def _api_window_theme(h):
        """把明暗主题同步到应用窗口标题栏（Windows DWM；其它平台静默失败）"""
        body = h._body()
        dark = str(body.get("theme") or "") == "dark"
        bg = str(body.get("bg") or "").strip()
        h._json({"ok": bool(winframe.apply(dark, bg))})


def _api_balance(h):
        h._json(query_balance(h._body().get("provider") or "chat"))


def _api_usage(h, qs):
        mode = (qs.get("mode", ["day"])[0] or "day").strip()
        if mode not in ("day", "month"):
            mode = "day"
        try:
            span = int(qs.get("span", ["30"])[0])
        except (TypeError, ValueError):
            span = 30
        span = max(1, min(span, 366))
        h._json(aggregate_usage(mode, span))


@route("/api/observe", methods=("GET",))
def _api_observe(h):
    """学习观测：近 N 天按学科 / 主题聚合 + 明细，供侧栏「学习观测」面板读取。"""
    qs = parse_qs(urlparse(h.path).query)
    try:
        days = int(qs.get("days", [observe_store.DEFAULT_DAYS])[0])
    except (TypeError, ValueError):
        days = observe_store.DEFAULT_DAYS
    days = max(1, min(days, 365))
    try:
        limit = int(qs.get("limit", [observe_store.MAX_ROWS])[0])
    except (TypeError, ValueError):
        limit = observe_store.MAX_ROWS
    h._json({"ok": True, **observe_store.summary(
        days=days,
        subject=str(qs.get("subject", [""])[0]).strip(),
        topic=str(qs.get("topic", [""])[0]).strip(),
        limit=limit)})


def _api_stop(h):
        body = h._body()
        ev = ACTIVE_STREAMS.get(body.get("session_id"))
        if ev:
            ev.set()
        h._json({"ok": True})


# 输入优化动作 → 指令措辞（见 web/composer.js 的 improveInput / precheckInput）
_COMPOSE_ACTIONS = {
    "polish": ("优化成一条清楚的指令：保留原文全部事实与意图，不增删事实；"
               "把含糊处补成「要改什么 / 范围 / 不要动什么 / 验收标准」这类可执行表述；"
               "原文没说的需求不要编，缺什么放进 gaps。"),
    "spec": ("改写成规格，纯文本四段（不要额外解释）：\n"
             "目标：一句话\n改动范围：只改哪些文件 / 模块\n不要动：明确边界\n"
             "验收标准：怎么算做完（可验证）\n"
             "原文没说的不要编，缺什么放进 gaps。"),
    "shorten": "精简：压缩到更短，去掉冗余与重复，保留全部关键信息。",
    "expand": "扩写：把要点补全为更完整、更具体的表达；不要编造原文没有的事实。",
}

# 发送前预检：只判断缺什么，不改写正文
_CHECK_RULE = (
    "只判断、不改写。判断这条指令是否缺少「能一次做对」所需的信息："
    "目标、改动范围（哪个文件 / 模块）、边界（不要动什么）、验收标准。"
    "只有缺了会导致做错时才列，最多 3 条，每条不超过 20 字，不要客套话；"
    "如果这条消息里塞了几个互不相关的任务（例如「修 bug + 加交互 + 新增功能」），"
    "把「包含 N 个不相关任务，建议拆开分别发」作为第一条 gaps；"
    "example 给一句「补全后可以怎么说」的示例（不超过 60 字）。")


def _api_compose(h):
        """轻量「输入优化」：润色 / 精简 / 扩写 / 翻译 / 规格化 / 预检，并顺带识别意图。

        走 _quick_chat（配置了 light_model 时自动用它），用量按 source="compose" 记录。
        action="check" 只做发送前预检：返回 gaps（缺什么）+ example，不改写正文。
        """
        body = h._body()
        text = str(body.get("text") or "").strip()
        action = str(body.get("action") or "polish").strip().lower()
        target = str(body.get("target_lang") or "").strip()
        if not text:
            return h._json({"ok": False, "error": "empty", "message": "输入内容为空"})
        if len(text) > 8000:
            text = text[:8000]
        if action == "translate":
            rule = "翻译：把原文翻译成「%s」；只输出译文，不要解释。" % (target or "英文")
        elif action == "check":
            rule = _CHECK_RULE
        else:
            rule = _COMPOSE_ACTIONS.get(action)
            if not rule:
                return h._json({"ok": False, "error": "bad_action", "message": "不支持的动作"})
        api_key, api_url, api_model = get_api_conf("chat")
        if not api_key:
            return h._json({"ok": False, "error": "no_key",
                            "message": "尚未配置聊天 API 密钥（设置 → 接入与密钥）"})
        # 表达复盘：这位用户以前在哪类说法上返工过，预检时优先提示同类问题
        memo = ""
        tips = expression.recent(3)
        if tips:
            memo = ("\n\n这位用户以前因为表述笼统返工过（同类问题优先提示）：\n"
                    + "\n".join("· 原话：%s → 纠正：%s"
                                % (t["prev"][:120].replace("\n", " "),
                                   t["fix"][:80].replace("\n", " ")) for t in tips))
        if action == "check":
            prompt = ("你是输入预检员。只输出一个 JSON 对象：\n"
                      '{"gaps": ["..."], "example": "..."}\n'
                      "要求：" + rule + memo + "\n\n用户的指令：\n" + text)
        else:
            prompt = ("你是输入优化助手。请按要求处理下面的文本，并且**只输出一个 JSON 对象**：\n"
                      '{"intent": "<写作|翻译|代码|提问|闲聊|其他 之一>", "text": "<处理后的文本>",'
                      ' "gaps": ["..."]}\n'
                      "要求：" + rule + memo + "\n\n原文：\n" + text)
        try:
            out = llm._quick_chat(api_key, api_url, api_model,
                                  [{"role": "user", "content": prompt}],
                                  max_tokens=1200, source="compose") or ""
        except Exception as e:
            return h._json({"ok": False, "error": "call_failed", "message": "调用失败：%s" % e})
        data = mem_tool._loads_loose(out)
        new_text, intent, example, gaps = "", "", "", []
        if isinstance(data, dict):
            new_text = str(data.get("text") or "").strip()
            intent = str(data.get("intent") or "").strip()
            example = str(data.get("example") or "").strip()
            g = data.get("gaps")
            if isinstance(g, list):
                gaps = [str(x).strip() for x in g if str(x).strip()][:3]
        if action == "check":
            # 预检失败不该打扰用户：宁可没有提示，也不要弹错误
            return h._json({"ok": True, "text": "", "intent": intent,
                            "gaps": gaps, "example": example, "action": action})
        if not new_text:                     # 模型没按 JSON 输出 → 退回去掉围栏的纯文本
            new_text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", out).strip()
        if not new_text:
            return h._json({"ok": False, "error": "empty_result", "message": "AI 未返回内容，请重试"})
        return h._json({"ok": True, "text": new_text, "intent": intent,
                        "gaps": gaps, "action": action})
