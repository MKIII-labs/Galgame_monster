# -*- coding: utf-8 -*-
from app import notes as noteslib

def _api_notes_tree(h):
        h._json(h._notes_state())


def _api_notes_get(h):
        rel = h._body().get("path")
        content = noteslib.read_note(rel)
        if content is None:
            return h._json({"ok": False, "error": "笔记不存在"}, 404)
        h._json({"ok": True, "path": rel, "content": content,
                    "backlinks": noteslib.backlinks(rel)})


def _api_notes_save(h):
        body = h._body()
        rel = body.get("path")
        if not noteslib.write_note(rel, body.get("content")):
            return h._json({"ok": False, "error": "保存失败（路径非法）"}, 400)
        h._json({"ok": True, "backlinks": noteslib.backlinks(rel),
                    "graph": noteslib.graph()})


def _api_notes_new(h):
        ok, info = noteslib.create_note(h._body().get("path"))
        if not ok:
            return h._json({"ok": False, "error": "已存在", "path": info}, 400)
        h._json({"ok": True, "path": info, "content": "", "backlinks": [],
                    "tree": noteslib.list_tree(), "graph": noteslib.graph()})


def _api_notes_delete(h):
        if not noteslib.delete_note(h._body().get("path")):
            return h._json({"ok": False, "error": "删除失败"}, 400)
        h._json(h._notes_state())


def _api_notes_rename(h):
        body = h._body()
        ok, info = noteslib.rename_path(body.get("path"), body.get("name"))
        if not ok:
            return h._json({"ok": False, "error": info}, 400)
        h._json({"ok": True, "path": info, "tree": noteslib.list_tree(),
                    "graph": noteslib.graph()})


def _api_notes_folder_new(h):
        ok, info = noteslib.create_folder(h._body().get("path"))
        if not ok:
            return h._json({"ok": False, "error": info}, 400)
        h._json(h._notes_state())


def _api_notes_folder_delete(h):
        if not noteslib.delete_folder(h._body().get("path")):
            return h._json({"ok": False, "error": "删除失败"}, 400)
        h._json(h._notes_state())


def _api_notes_graph(h):
        h._json({"ok": True, "graph": noteslib.graph()})


def _api_notes_backlinks(h):
        h._json({"ok": True, "backlinks": noteslib.backlinks(h._body().get("path"))})


def _api_notes_search(h):
        h._json({"ok": True, "hits": noteslib.search(h._body().get("q"))})
