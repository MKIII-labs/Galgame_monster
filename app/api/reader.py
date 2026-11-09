# -*- coding: utf-8 -*-
"""沉浸式阅读 API：打开 PDF、原始字节、裁图、翻译任务与事件流。

前端打开 PDF 走 /api/reader/open（解析结果带缓存），pdf.js 渲染原页；
右栏阅读流用 open 返回的 blocks；图表/独立公式按 rect 调 /api/reader/crop。
"""
import json
import os
import re
import time
from urllib.parse import parse_qs, quote, urlparse

from app import anno, library, reader, reader_export
from app.registry import route

LANG_NAMES = {"zh": "简体中文", "zh-TW": "繁體中文", "en": "English", "ja": "日本語"}
_DOC_ID_RE = re.compile(r"^[0-9a-f]{16}$")


def _guard(h):
    body = h._body()
    s, sb = h._sb(body)
    if not s:
        return body, None, h._json({"ok": False, "error": "会话不存在"}, 404)
    return body, sb, None


def _parse_any(path, force=False):
    """按扩展名选择解析器：PDF 走版面重建，docx/pptx 走纯文本阅读流。"""
    if os.path.splitext(path)[1].lower() == ".pdf":
        return reader.parse_pdf(path, force=force)
    return reader.parse_doc(path)


@route("/api/reader/open")
def _api_reader_open(h):
    body, sb, err = _guard(h)
    if err:
        return err
    lang = str(body.get("lang") or "zh")
    try:
        path = reader.resolve(sb, body.get("path"))
        meta = _parse_any(path, force=bool(body.get("force")))
        meta["translations"] = reader.load_translations(path, lang)
        meta["formula_ai"] = reader.formula_ai_available()
        meta["annotations"] = reader.list_annotations(body.get("path"))
        # 文献条目：首次打开时按 PDF 元数据 / 首页文本自动补全，之后可手动编辑
        try:
            if str(body.get("path") or "").lower().endswith(".pdf"):
                library.ensure_document(path, body.get("path"))
        except Exception:                    # noqa: BLE001  元数据识别失败不影响阅读
            pass
        meta["document"] = anno.get_document(body.get("path"))
        meta["ok"] = True
        return h._json(meta)
    except reader.ReaderError as e:
        return h._json({"ok": False, "error": str(e)}, 400)
    except Exception as e:                       # noqa: BLE001  解析失败要可读
        return h._json({"ok": False, "error": "解析失败：%s" % e}, 500)


@route("/api/reader/search")
def _api_reader_search(h):
    """全文查找：复用 open 时的解析缓存，只做归一化子串匹配。

    前端 200ms 防抖后才请求，1000 页文档首次建索引、之后每次搜索都在内存里。
    """
    body, sb, err = _guard(h)
    if err:
        return err
    q = str(body.get("q") or "").strip()
    if not q:
        return h._json({"ok": True, "items": [], "total": 0, "truncated": False})
    try:
        path = reader.resolve(sb, body.get("path"))
        meta = _parse_any(path)
    except reader.ReaderError as e:
        return h._json({"ok": False, "error": str(e)}, 400)
    except Exception as e:                       # noqa: BLE001
        return h._json({"ok": False, "error": "解析失败：%s" % e}, 500)
    try:
        res = reader.search_blocks(meta, q)
    except Exception as e:                       # noqa: BLE001
        return h._json({"ok": False, "error": "查找失败：%s" % e}, 500)
    res["ok"] = True
    return h._json(res)


@route("/api/reader/file")
def _api_reader_file(h):
    body, sb, err = _guard(h)
    if err:
        return err
    try:
        data = reader.read_pdf_bytes(sb, body.get("path"))
    except reader.ReaderError as e:
        return h._json({"ok": False, "error": str(e)}, 400)
    return h._send(200, data, "application/pdf", cache="private, max-age=60")


@route("/api/reader/crop")
def _api_reader_crop(h):
    body, sb, err = _guard(h)
    if err:
        return err
    try:
        page = int(body.get("page") or 0)
        rect = [float(v) for v in (body.get("rect") or [])]
    except (TypeError, ValueError):
        return h._json({"ok": False, "error": "参数不对"}, 400)
    if page <= 0 or len(rect) != 4:
        return h._json({"ok": False, "error": "参数不对"}, 400)
    try:
        path = reader.resolve(sb, body.get("path"))
        png = reader.crop_png(path, page, rect)
    except reader.ReaderError as e:
        return h._json({"ok": False, "error": str(e)}, 400)
    except Exception as e:                       # noqa: BLE001
        return h._json({"ok": False, "error": "裁图失败：%s" % e}, 500)
    return h._send(200, png, "image/png", cache="private, max-age=86400")


@route("/api/reader/translate")
def _api_reader_translate(h):
    body, sb, err = _guard(h)
    if err:
        return err
    lang = str(body.get("lang") or "zh")
    try:
        path = reader.resolve(sb, body.get("path"))
        meta = _parse_any(path)
    except reader.ReaderError as e:
        return h._json({"ok": False, "error": str(e)}, 400)
    blocks = meta.get("blocks") or []
    pages = body.get("pages")
    if isinstance(pages, list) and pages:
        want = set()
        for p in pages:
            try:
                want.add(int(p))
            except (TypeError, ValueError):
                continue
        if want:
            blocks = [b for b in blocks
                      if b.get("page") in want
                      or any(pt.get("page") in want for pt in (b.get("parts") or []))]
    try:
        jid, total = reader.start_translate(path, blocks, lang,
                                            LANG_NAMES.get(lang, "简体中文"),
                                            force=bool(body.get("force")),
                                            context=_doc_context(meta),
                                            all_blocks=meta.get("blocks"),
                                            retry_failed=bool(body.get("retry")))
    except Exception as e:                       # noqa: BLE001
        return h._json({"ok": False, "error": "启动翻译失败：%s" % e}, 500)
    return h._json({"ok": True, "job_id": jid, "total": total})


@route("/api/reader/glossary")
def _api_reader_glossary(h):
    """术语表：GET 读取当前文档的术语表，POST 保存人工编辑 / 重新生成。"""
    body, sb, err = _guard(h)
    if err:
        return err
    lang = str(body.get("lang") or "zh")
    try:
        path = reader.resolve(sb, body.get("path"))
    except reader.ReaderError as e:
        return h._json({"ok": False, "error": str(e)}, 400)
    try:
        if str(body.get("action") or "") == "save":
            data = reader.save_glossary(path, lang, body.get("terms") or [])
        elif str(body.get("action") or "") == "regenerate":
            meta = _parse_any(path)
            data = reader.ensure_glossary(path, meta.get("blocks") or [], lang,
                                          LANG_NAMES.get(lang, "简体中文"), force=True)
        else:
            data = reader.load_glossary(path, lang)
    except Exception as e:                       # noqa: BLE001
        return h._json({"ok": False, "error": str(e)[:200]}, 500)
    return h._json({"ok": True, "terms": data.get("terms") or [],
                    "title": data.get("title") or ""})


@route("/api/reader/export")
def _api_reader_export(h):
    """生成译文导出文件：mono（仅译文）/ dual（双语对照）/ md（Markdown）。

    生成结果落到 data/reader/<doc_id>/exports/，返回同源下载地址；
    前端用 Electron 的 saveUrl 流式写盘（浏览器则直接 fetch 成 blob）。
    """
    body, sb, err = _guard(h)
    if err:
        return err
    mode = str(body.get("mode") or "mono")
    lang = str(body.get("lang") or "zh")
    try:
        path = reader.resolve(sb, body.get("path"))
        meta = _parse_any(path)
    except reader.ReaderError as e:
        return h._json({"ok": False, "error": str(e)}, 400)
    try:
        translations = reader.load_translations(path, lang)
        raw, ext, mime, missing = reader_export.export(path, meta, translations, mode, lang)
        name = reader_export.export_name(meta, mode, ext)
        doc_id, name = reader_export.save(path, raw, name)
    except reader_export.ExportError as e:
        return h._json({"ok": False, "error": str(e)}, 400)
    except Exception as e:                       # noqa: BLE001
        return h._json({"ok": False, "error": "导出失败：%s" % e}, 500)
    url = "/api/reader/export/file?doc=%s&name=%s" % (doc_id, quote(name))
    return h._json({"ok": True, "url": url, "name": name, "bytes": len(raw),
                    "mime": mime, "missing": missing, "mode": mode})


@route("/api/reader/export/file", methods=("GET",))
def _api_reader_export_file(h):
    qs = parse_qs(urlparse(h.path).query)
    doc = str((qs.get("doc") or [""])[0])
    name = str((qs.get("name") or [""])[0])
    if not _DOC_ID_RE.match(doc) or not name or os.path.basename(name) != name:
        return h._json({"ok": False, "error": "参数不合法"}, 400)
    fp = os.path.join(reader.READER_DIR, doc, "exports", name)
    if not os.path.isfile(fp):
        return h._json({"ok": False, "error": "文件不存在或已过期，请重新导出"}, 404)
    try:
        with open(fp, "rb") as f:
            raw = f.read()
    except OSError as e:
        return h._json({"ok": False, "error": "读取失败：%s" % e}, 500)
    ext = os.path.splitext(name)[1].lower()
    ctype = "application/pdf" if ext == ".pdf" else "text/markdown; charset=utf-8"
    return h._send(200, raw, ctype, cache="private, max-age=60",
                   extra_headers={"Content-Disposition":
                                  "attachment; filename*=UTF-8''%s" % quote(name)})


def _doc_context(meta):
    """给翻译模型的全文上下文：首页最像标题的一段，兜底文件名。"""
    for b in meta.get("blocks") or []:
        if b.get("page") != 1:
            continue
        t = (b.get("text") or "").strip()
        if t and (b.get("kind") == "heading" or len(t) > 12):
            return t[:80]
    return (meta.get("name") or "").rsplit(".", 1)[0][:80]


@route("/api/reader/cancel")
def _api_reader_cancel(h):
    body, _sb, err = _guard(h)
    if err:
        return err
    return h._json({"ok": reader.cancel_job(str(body.get("job_id") or ""))})


@route("/api/reader/formula")
def _api_reader_formula(h):
    """把一行公式的裁图识别成 LaTeX（视觉模型，结果落缓存）。"""
    body, _sb, err = _guard(h)
    if err:
        return err
    try:
        page_no = int(body.get("page") or 0)
        rect = [float(v) for v in (body.get("rect") or [])]
    except (TypeError, ValueError):
        return h._json({"ok": False, "error": "参数不合法"}, 400)
    if page_no < 1 or len(rect) != 4:
        return h._json({"ok": False, "error": "参数不合法"}, 400)
    try:
        path = reader.resolve(_sb, body.get("path"))
        latex = reader.recognize_formula(path, page_no, rect)
    except Exception as e:                       # noqa: BLE001
        return h._json({"ok": False, "error": str(e)[:200]}, 400)
    return h._json({"ok": True, "latex": latex})


@route("/api/reader/selection")
def _api_reader_selection(h):
    """选中片段的精准翻译（提问统一走 /api/ask/stream）。"""
    body, _sb, err = _guard(h)
    if err:
        return err
    text = str(body.get("text") or "").strip()[:1200]
    if not text:
        return h._json({"ok": False, "error": "没有选中文字"}, 400)
    ctx = str(body.get("context") or "").strip()[:600]
    from app import llm
    from app.config import get_api_conf, load_config
    cfg = load_config()
    api_key, api_url, api_model = get_api_conf("chat", cfg)
    sys = ("你是学术论文翻译引擎。只输出中文译文，不要解释、不要引号、不要重复原文。"
           "术语与给出的上下文保持一致，公式和编号原样保留。")
    user = "文档上下文：%s\n\n选中片段：\n%s" % (ctx or "（无）", text)
    need = min(1200, max(200, len(text) * 2))
    try:
        ans = llm._quick_chat(api_key, api_url, api_model,
                              [{"role": "system", "content": sys},
                               {"role": "user", "content": user}],
                              max_tokens=need, source="reader")
    except Exception as e:                       # noqa: BLE001
        return h._json({"ok": False, "error": "模型调用失败：%s" % e}, 500)
    ans = (ans or "").strip()
    if not ans:
        return h._json({"ok": False, "error": "模型没有返回内容"}, 500)
    return h._json({"ok": True, "text": ans})


@route("/api/reader/anno/save")
def _api_reader_anno_save(h):
    """把一次问答保存成批注，挂在对应段落块上。"""
    body, sb, err = _guard(h)
    if err:
        return err
    try:
        reader.resolve(sb, body.get("path"))
    except Exception:                            # noqa: BLE001
        return h._json({"ok": False, "error": "文件不存在"}, 400)
    aid = reader.save_annotation(body.get("path"), body)
    if not aid:
        return h._json({"ok": False, "error": "批注内容不能为空"}, 400)
    return h._json({"ok": True, "id": aid, "items": reader.list_annotations(body.get("path"))})


@route("/api/reader/anno/delete")
def _api_reader_anno_delete(h):
    body, sb, err = _guard(h)
    if err:
        return err
    try:
        reader.resolve(sb, body.get("path"))
    except Exception:                            # noqa: BLE001
        return h._json({"ok": False, "error": "文件不存在"}, 400)
    reader.delete_annotation(body.get("path"), str(body.get("id") or ""))
    return h._json({"ok": True, "items": reader.list_annotations(body.get("path"))})


@route("/api/reader/events")
def _api_reader_events(h):
    body = h._body()
    job = reader.job_snapshot(str(body.get("job_id") or ""))
    if not job:
        return h._json({"ok": False, "error": "任务不存在或已过期"}, 404)
    try:
        since = int(body.get("since") or 0)
    except (TypeError, ValueError):
        since = 0

    h.send_response(200)
    h.send_header("Content-Type", "text/event-stream; charset=utf-8")
    h.send_header("Cache-Control", "no-cache")
    h.send_header("X-Accel-Buffering", "no")
    h.send_header("Connection", "close")
    h.end_headers()
    try:
        h.connection.settimeout(20)
    except Exception:
        pass

    sent = max(0, since)
    last_tick = time.time()
    while True:
        with reader._JOBS_LOCK:                  # noqa: SLF001  与任务线程共用同一把锁
            batch = [e for e in job["events"] if int(e.get("_seq") or 0) > sent]
            done = bool(job.get("done"))
        try:
            for e in batch:
                h.wfile.write(("data: " + json.dumps(e, ensure_ascii=False) + "\n\n")
                              .encode("utf-8"))
                sent = max(sent, int(e.get("_seq") or 0))
            h.wfile.flush()
        except Exception:
            return                               # 客户端断开：任务照跑
        if done and not batch:
            return
        if not batch:
            if time.time() - last_tick >= 5:
                try:
                    h.wfile.write(("data: " + json.dumps({"type": "tick"}) + "\n\n")
                                  .encode("utf-8"))
                    h.wfile.flush()
                except Exception:
                    return
                last_tick = time.time()
            time.sleep(0.2)
