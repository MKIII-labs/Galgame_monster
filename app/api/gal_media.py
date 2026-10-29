# -*- coding: utf-8 -*-
from app.store import _lock
from app import gal
from app.sessions import (load_sessions, save_sessions)

def _api_gal_backgrounds(h):
        """背景库列表（含名称与分类标签）"""
        data = gal.load_backgrounds()
        h._json({"ok": True, "items": data.get("items") or [], "dims": list(gal.BG_TAG_DIMS)})


def _api_gal_background_save(h):
        """更新某张背景的名称 / 标签"""
        body = h._body()
        try:
            item = gal.update_background(body.get("file"), body.get("name"), body.get("tags"))
        except ValueError as e:
            return h._json({"ok": False, "error": str(e)}, 400)
        h._json({"ok": True, "item": item})


def _api_gal_background_upload(h):
        """上传一张背景图（可同时带名称 / 标签）"""
        body = h._body()
        try:
            item = gal.save_background_upload(body.get("filename"), body.get("data"),
                                               body.get("name"), body.get("tags"))
        except ValueError as e:
            return h._json({"ok": False, "error": str(e)}, 400)
        h._json({"ok": True, "item": item})


def _api_gal_background_delete(h):
        body = h._body()
        rel = gal._safe_bg_rel(body.get("file"))
        try:
            gal.delete_background(rel)
        except ValueError as e:
            return h._json({"ok": False, "error": str(e)}, 400)
        # 连带清掉引用：会话记住的背景 + 历史消息里的 bg
        # （否则重渲染时会去取一个已删除的文件，静默失败）
        data = load_sessions()
        changed = False
        for s in data.get("sessions") or []:
            if s.get("gal_bg") == rel:
                s["gal_bg"] = ""
                changed = True
            for m in s.get("messages") or []:
                g = m.get("gal") if isinstance(m, dict) else None
                if isinstance(g, dict) and g.get("bg") == rel:
                    g["bg"] = ""
                    changed = True
        if changed:
            with _lock:
                save_sessions(data)
        h._json({"ok": True})


def _api_gal_bgms(h):
        """BGM 库列表（含名称与分类标签）"""
        data = gal.load_bgms()
        h._json({"ok": True, "items": data.get("items") or [], "dims": list(gal.BGM_TAG_DIMS)})


def _api_gal_bgm_save(h):
        """更新某首 BGM 的名称 / 标签"""
        body = h._body()
        try:
            item = gal.update_bgm(body.get("file"), body.get("name"), body.get("tags"))
        except ValueError as e:
            return h._json({"ok": False, "error": str(e)}, 400)
        h._json({"ok": True, "item": item})


def _api_gal_bgm_upload(h):
        """上传一首 BGM（可同时带名称 / 标签）"""
        body = h._body()
        try:
            item = gal.save_bgm_upload(body.get("filename"), body.get("data"),
                                       body.get("name"), body.get("tags"))
        except ValueError as e:
            return h._json({"ok": False, "error": str(e)}, 400)
        h._json({"ok": True, "item": item})


def _api_gal_bgm_delete(h):
        body = h._body()
        rel = gal._safe_bgm_rel(body.get("file"))
        try:
            gal.delete_bgm(rel)
        except ValueError as e:
            return h._json({"ok": False, "error": str(e)}, 400)
        # 连带清掉引用：会话记住的 BGM + 历史消息里的 bgm
        data = load_sessions()
        changed = False
        for s in data.get("sessions") or []:
            if s.get("gal_bgm") == rel:
                s["gal_bgm"] = ""
                changed = True
            for m in s.get("messages") or []:
                g = m.get("gal") if isinstance(m, dict) else None
                if isinstance(g, dict) and g.get("bgm") == rel:
                    g["bgm"] = ""
                    changed = True
        if changed:
            with _lock:
                save_sessions(data)
        h._json({"ok": True})
