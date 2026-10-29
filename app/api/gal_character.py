# -*- coding: utf-8 -*-
import base64
from app.llm import _quick_chat
from app import gal
from app.config import get_api_conf
from urllib.parse import quote

def _api_gal_characters(h):
        h._json({"ok": True, "characters": gal.list_characters()})


def _api_gal_character_get(h):
        c = gal.load_character(h._body().get("id"))
        if not c:
            return h._json({"ok": False, "error": "角色不存在"}, 404)
        h._json({"ok": True, "character": c})


def _api_gal_character_save(h):
        body = h._body()
        try:
            c = gal.save_character(body.get("id"), body.get("character") or {})
        except ValueError as e:
            return h._json({"ok": False, "error": str(e)}, 400)
        h._json({"ok": True, "character": c, "characters": gal.list_characters()})


def _api_gal_character_delete(h):
        try:
            ok = gal.delete_character(h._body().get("id"))
        except ValueError as e:
            return h._json({"ok": False, "error": str(e)}, 400)
        h._json({"ok": ok, "characters": gal.list_characters()})


def _api_gal_asset(h):
        """按需返回角色立绘图片字节"""
        body = h._body()
        try:
            p, mime = gal.resolve_asset(body.get("id"), body.get("path"))
        except ValueError as e:
            return h._json({"ok": False, "error": str(e)}, 400)
        try:
            with open(p, "rb") as f:
                data = f.read()
        except OSError:
            return h._json({"ok": False, "error": "读取失败"}, 500)
        return h._send(200, data, mime)


def _api_gal_character_files(h):
        try:
            files = gal.list_sprite_files(h._body().get("id"))
        except ValueError as e:
            return h._json({"ok": False, "error": str(e)}, 400)
        h._json({"ok": True, "files": files})


def _api_gal_character_upload(h):
        body = h._body()
        try:
            rel = gal.save_upload(body.get("id"), body.get("filename"), body.get("data"))
        except ValueError as e:
            return h._json({"ok": False, "error": str(e)}, 400)
        h._json({"ok": True, "path": rel, "files": gal.list_sprite_files(body.get("id"))})


def _api_gal_character_export(h):
        """把角色导出为 .char（zip）下载"""
        try:
            fname, data = gal.export_character(h._body().get("id"))
        except ValueError as e:
            return h._json({"ok": False, "error": str(e)}, 400)
        h.send_response(200)
        h.send_header("Content-Type", "application/zip")
        h.send_header("Content-Length", str(len(data)))
        h.send_header("Content-Disposition",
                         "attachment; filename=\"character.char\"; filename*=UTF-8''" + quote(fname))
        h.send_header("Cache-Control", "no-store")
        h.end_headers()
        h.wfile.write(data)


def _api_gal_character_import(h):
        """导入 .char / zip 角色包（兼容 Shinsekai 的 character.yaml）"""
        body = h._body()
        try:
            raw = base64.b64decode(body.get("data") or "")
        except Exception:
            return h._json({"ok": False, "error": "文件内容解析失败"}, 400)
        try:
            r = gal.import_character_pack(raw, body.get("filename") or "")
        except ValueError as e:
            return h._json({"ok": False, "error": str(e)}, 400)
        r["ok"] = True
        r["characters"] = gal.list_characters()
        h._json(r)


def _api_gal_character_import_dir(h):
        """批量导入 data/gal/packs/ 下的角色包"""
        r = gal.import_packs_dir()
        r["ok"] = True
        r["packs"] = gal.list_packs()
        r["characters"] = gal.list_characters()
        h._json(r)


def _api_gal_packs(h):
        """列出 data/gal/packs/ 下待导入的角色包"""
        h._json({"ok": True, "packs": gal.list_packs()})


def _api_gal_character_draft(h):
        """AI 帮写角色设定（复用「设置 → 聊天模型」的配置）"""
        body = h._body()
        name = str(body.get("name") or "").strip()
        if not name:
            return h._json({"ok": False, "error": "请先填写角色名"}, 400)
        api_key, api_url, api_model = get_api_conf("chat")
        if not api_key:
            return h._json({"ok": False, "error": "未配置聊天模型（设置 → 聊天模型）"}, 400)
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
                                {"role": "user", "content": user}], max_tokens=700,
                               source="char_profile")
        except Exception as e:
            return h._json({"ok": False, "error": "生成失败：%s" % e}, 502)
        h._json({"ok": True, "profile": str(text or "").strip()})
