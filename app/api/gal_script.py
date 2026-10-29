# -*- coding: utf-8 -*-
import time
from app.store import _lock
from app.llm import (_quick_chat, recent_dialog, user_profile_text)
from app.sessions import (ensure_session_kb_fields, find_session, load_sessions, save_sessions)
from app import gal
from app.config import (get_api_conf, load_config)
from app.knowledge import world_text

def _api_gal_script(h):
        """AI 自动剧本：action = get 读取 | generate 生成/重新生成 | clear 清空

        生成时会把「角色设定 + 用户画像 + 已有对话」交给模型，产出与 story.json
        同构的分幕剧本，存进当前会话（每会话独立）。
        """
        body = h._body()
        action = str(body.get("action") or "get").strip()
        data = load_sessions()
        s = find_session(data, body.get("id") or body.get("session_id"))
        if not s:
            return h._json({"ok": False, "error": "no session"}, 404)
        ensure_session_kb_fields(s)

        if action == "clear":
            s["gal_auto_story"] = {}
            s["gal_auto_outline"] = ""
            s["gal_auto_acts_done"] = 0
            s["gal_recap"] = ""
            s["gal_node_id"] = ""
            s["gal_round"] = 0
            s["updated_at"] = time.time()
            with _lock:
                save_sessions(data)
            return h._json({"ok": True, "story": {}, "outline": "", "node_id": "", "round": 0})

        if action == "generate":
            cfg = load_config() or {}
            char = gal.load_character(body.get("character_id") or s.get("gal_character_id"))
            if not char:
                return h._json({"ok": False, "error": "请先选择角色"}, 400)
            api_key, api_url, api_model = get_api_conf("chat")
            if not api_key:
                return h._json({"ok": False, "error": "未配置聊天模型（设置 → 聊天模型）"}, 400)
            acts = gal._clamp(cfg.get("gal_auto_script_acts"), 5, 1, 12)
            rounds = gal._clamp(cfg.get("gal_auto_script_rounds"), 4, 1, 12)
            prompt = gal.build_script_prompt(char, user_profile_text(), acts, rounds,
                                            recent=recent_dialog(s), world=world_text(s))
            try:
                reply = _quick_chat(api_key, api_url, api_model, [
                    {"role": "system", "content": "你是 Galgame 编剧。只输出一个 JSON 对象，不要解释。"},
                    {"role": "user", "content": prompt},
                ], max_tokens=4000, source="gal_script")   # 分幕大纲（5 幕 × 100–180 字）本身就长，额度要给足
            except Exception as e:
                return h._json({"ok": False, "error": "生成失败：%s" % e}, 502)
            story = gal.parse_plan(reply, acts)
            if not story:
                return h._json({"ok": False, "error": "模型未返回可用大纲，请重试"}, 502)
            start = gal.start_story(story, rounds)
            if not start:
                return h._json({"ok": False, "error": "大纲内容为空，请重试"}, 502)
            s["gal_auto_story"] = start
            s["gal_auto_outline"] = gal.plan_outline_text(story)
            s["gal_auto_acts_done"] = 0
            s["gal_auto_on"] = True
            s["gal_node_id"] = start.get("start") or ""
            s["gal_round"] = 0
            s["updated_at"] = time.time()
            with _lock:
                save_sessions(data)
            return h._json({"ok": True, "story": start, "plan": story,
                               "outline": s["gal_auto_outline"],
                               "node_id": s["gal_node_id"], "round": 0})

        h._json({"ok": True, "on": bool(s.get("gal_auto_on")),
                    "story": s.get("gal_auto_story") or {},
                    "outline": s.get("gal_auto_outline") or "",
                    "node_id": s.get("gal_node_id") or "",
                    "round": int(s.get("gal_round") or 0)})


def _api_gal_story_get(h):
        h._json({"ok": True, "story": gal.load_story(h._body().get("id"))})


def _api_gal_story_save(h):
        body = h._body()
        try:
            story = gal.save_story(body.get("id"), body.get("story") or {})
        except ValueError as e:
            return h._json({"ok": False, "error": str(e)}, 400)
        h._json({"ok": True, "story": story})


def _api_gal_effects(h):
        """列出关键词特效表（供设置界面提示）"""
        h._json({"ok": True, "effects": gal.load_effects()})


def _api_gal_journal(h):
        """剧情档案：本会话 AI 记录下来的记忆（主角已知的信息）+ 当前舞台方位。

        只返回会话记忆，且**排除 `source=="preset"` 的预设节点**——那是世界圣经，
        含剧透，不该在玩家的「档案」里露出。"""
        body = h._body()
        data = load_sessions()
        s = find_session(data, body.get("session_id") or body.get("id"))
        if not s:
            return h._json({"ok": False, "error": "no session"}, 404)
        ensure_session_kb_fields(s)
        nodes = []
        ids = set()
        for n in (s.get("memory") or {}).get("nodes", []):
            if not isinstance(n, dict) or n.get("source") == "preset":
                continue
            title = str(n.get("title") or "").strip()
            if not title:
                continue
            nid = n.get("id") or ""
            if nid:
                ids.add(nid)
            nodes.append({
                "id": nid,
                "title": title,
                "content": str(n.get("content") or "").strip(),
                "category": str(n.get("category") or "其他").strip(),
                "tags": n.get("tags") or [],
                "x": n.get("x"), "y": n.get("y"),
                "size": n.get("size"), "color": n.get("color"),
            })
        edges = []
        for e in (s.get("memory") or {}).get("edges", []):
            if not isinstance(e, dict):
                continue
            a, b = e.get("from"), e.get("to")
            if a in ids and b in ids:       # 端点被过滤（预设/无效）的边不返回，避免图谱出现悬空边
                edges.append({"id": e.get("id") or "", "from": a, "to": b,
                              "label": e.get("label") or "",
                              "relation": e.get("relation") or "related",
                              "directed": e.get("directed")})
        h._json({"ok": True, "nodes": nodes, "edges": edges,
                    "map": gal._norm_map(s.get("gal_map")) or {}})


def _api_gal_effect_audio(h):
        """返回特效音频字节（data/gal/effects/ 下）"""
        try:
            p, mime = gal.resolve_effect_audio(h._body().get("path"))
        except ValueError as e:
            return h._json({"ok": False, "error": str(e)}, 400)
        try:
            with open(p, "rb") as f:
                data = f.read()
        except OSError:
            return h._json({"ok": False, "error": "读取失败"}, 500)
        return h._send(200, data, mime)


def _api_gal_media(h):
        """返回背景（kind=bg）或 BGM（kind=bgm）资源字节"""
        body = h._body()
        try:
            p, mime = gal.resolve_media(body.get("kind") or "bg", body.get("path"))
        except ValueError as e:
            return h._json({"ok": False, "error": str(e)}, 400)
        try:
            with open(p, "rb") as f:
                data = f.read()
        except OSError:
            return h._json({"ok": False, "error": "读取失败"}, 500)
        return h._send(200, data, mime)
