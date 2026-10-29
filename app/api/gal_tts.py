# -*- coding: utf-8 -*-
import time
from app.store import _lock
from app.sessions import (ensure_session_kb_fields, find_session, load_sessions, save_sessions)
from app import (gal, tts)

def _api_gal_stats_reset(h):
        """清空当前会话的舞台状态参数（stats）"""
        data = load_sessions()
        s = find_session(data, h._body().get("id"))
        if not s:
            return h._json({"ok": False, "error": "no session"}, 404)
        s["gal_stats"] = []
        s["updated_at"] = time.time()
        with _lock:
            save_sessions(data)
        h._ok_sessions(data)


def _api_gal_tts(h):
        """台词朗读：文本 → 音频字节（force=true 时可绕过「未开启」限制，供设置里试听）

        character_id / preset 可选：命中则该角色用它的配音预设覆盖全局 TTS 配置。
        """
        body = h._body()
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
            return h._json({"ok": False, "error": "未开启台词朗读（TTS）"}, 400)
        text = (body.get("text") or "").strip()
        if not text:
            return h._json({"ok": False, "error": "empty"}, 400)
        try:
            data, mime = tts.synthesize(cfg, text, body.get("voice") or "")
        except Exception as e:
            return h._json({"ok": False, "error": "合成失败：%s" % e}, 502)
        return h._send(200, data, mime)


def _api_gal_tts_active(h):
        """设置某角色当前使用的配音预设（空串表示跟随全局）"""
        body = h._body()
        cid = body.get("id")
        char = gal.load_character(cid)
        if not char:
            return h._json({"ok": False, "error": "角色不存在"}, 404)
        name = str(body.get("preset") or "").strip()
        t = char.get("tts") or {"active": "", "presets": {}}
        if name and name not in (t.get("presets") or {}):
            return h._json({"ok": False, "error": "预设不存在"}, 400)
        t["active"] = name
        char["tts"] = t
        gal.save_character(cid, char)
        h._json({"ok": True, "character": gal.load_character(cid)})


def _api_gal_tts_voices(h):
        """列出 Edge 中文音色（设置界面提示用）"""
        voices = tts.edge_voices()
        h._json({"ok": True, "voices": voices, "default": tts.EDGE_DEFAULT_VOICE})


def _api_gal_session(h):
        """设置会话的 GAL 字段（角色 / 是否 GAL 模式）"""
        body = h._body()
        data = load_sessions()
        s = find_session(data, body.get("id") or body.get("session_id"))
        if not s:
            return h._json({"ok": False, "error": "no session"}, 404)
        ensure_session_kb_fields(s)
        if "gal_enabled" in body:
            s["gal_enabled"] = bool(body.get("gal_enabled"))
        for key in ("gal_use_profile", "gal_use_kb", "gal_use_rules"):
            if key in body:
                s[key] = bool(body.get(key))
        if "gal_character_id" in body:
            s["gal_character_id"] = str(body.get("gal_character_id") or "")
        if "gal_bg" in body:            # 手动选定的舞台背景（相对路径，空串=清除）
            s["gal_bg"] = str(body.get("gal_bg") or "")
        if "gal_bgm" in body:           # 手动选定的舞台 BGM（相对路径，空串=停止）
            s["gal_bgm"] = str(body.get("gal_bgm") or "")
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
        h._ok_sessions(data)
