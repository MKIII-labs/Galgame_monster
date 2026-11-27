# -*- coding: utf-8 -*-

import re



def _url_public_error(url):
    """fetch_url 的 SSRF 护栏：只允许公网 http(s)。

    实现放在 app/search.py（抓正文时每跳重定向也要复用同一套判断），这里只委托。
    """
    from app.search import public_url_error
    return public_url_error(url)


def _t_fetch_url(root, args, stop_event=None):
    from .base import (FETCH_URL_CHARS, FETCH_URL_MAX_CHARS)
    from .net_policy import check_url
    url = str((args or {}).get("url") or "").strip()
    err = _url_public_error(url)
    if err:
        return "❌ " + err, None
    why = check_url(url)
    if why:
        return "❌ " + why, None
    if stop_event is not None and stop_event.is_set():
        return "⏹ 已停止", None
    try:
        chars = int((args or {}).get("chars") or FETCH_URL_CHARS)
    except (TypeError, ValueError):
        chars = FETCH_URL_CHARS
    chars = max(800, min(chars, FETCH_URL_MAX_CHARS))
    try:
        from app.search import fetch_text_full
        # 工具场景要的是「原文」：不能用搜索那套「最多 6 段证据」的摘要逻辑，
        # 否则 15KB 的文件只会回来约 1KB（2026-11-26 下载 skill 卡住的根因）。
        body = fetch_text_full(url, timeout=15,
                               chars=chars)
    except Exception as e:
        return "❌ 抓取失败：%s" % e, None
    if not body:
        return ("❌ 抓不到正文（页面可能需要登录、是纯 JS 渲染，或站点拒绝抓取）：%s"
                % url), None
    return "URL: %s\n\n%s" % (url, body), None


DOWNLOAD_MAX_BYTES = 32 * 1024 * 1024      # 单文件上限：够放模型 / 素材，防误拉大包
DOWNLOAD_CHUNK = 64 * 1024


def _t_download_file(root, args):
    """把 URL 直接下载到沙箱，内容不经过模型上下文。

    与 fetch_url 的分工：fetch_url 是「读进上下文」（受上下文预算限制），
    download_file 是「落盘」（按字节写，只回大小 + sha1）。
    默认不覆盖已有文件，必须显式 overwrite=true。
    """
    import hashlib
    import os
    import uuid

    from app.search import UA, _public_get, public_url_error
    from .net_policy import check_url
    from .sandbox import _rel, resolve_path
    from .undo import _snap

    args = args if isinstance(args, dict) else {}
    url = str(args.get("url") or "").strip()
    dest = str(args.get("path") or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return "❌ url 必须是 http(s) 地址", None
    if not dest:
        return "❌ 缺少 path（下载到沙箱内的相对路径）", None
    err = public_url_error(url)
    if err:
        return "❌ " + err, None
    why = check_url(url)
    if why:
        return "❌ " + why, None
    p = resolve_path(root, dest)
    if os.path.isdir(p):
        return "❌ 目标是目录，不能写入：" + _rel(root, p), None
    if os.path.exists(p) and not bool(args.get("overwrite")):
        return ("❌ 目标已存在：%s。确认覆盖请传 overwrite=true"
                % _rel(root, p)), None

    try:
        r = _public_get(url, timeout=30,
                        headers={"User-Agent": UA, "Accept": "*/*"}, stream=True)
    except Exception as e:                    # noqa: BLE001
        return "❌ 下载失败：%s" % e, None
    if r is None:
        return "❌ 下载失败：无法连接 %s" % url, None
    if r.status_code < 200 or r.status_code >= 300:
        try:
            r.close()
        except Exception:                     # noqa: BLE001
            pass
        return "❌ 下载失败：HTTP %s" % r.status_code, None
    # _public_get 手动跟 30x，每一跳都校验；这里再核一次最终地址
    final = str(getattr(r, "url", "") or url)
    err = public_url_error(final)
    if err:
        try:
            r.close()
        except Exception:                     # noqa: BLE001
            pass
        return "❌ 重定向被拒绝：%s" % err, None
    why = check_url(final)
    if why:
        try:
            r.close()
        except Exception:                     # noqa: BLE001
            pass
        return "❌ 重定向后的域名不在白名单：%s" % why, None
    headers = getattr(r, "headers", {}) or {}
    try:
        total = int(headers.get("content-length") or 0)
    except (TypeError, ValueError):
        total = 0
    if total > DOWNLOAD_MAX_BYTES:
        try:
            r.close()
        except Exception:                     # noqa: BLE001
            pass
        return ("❌ 文件 %d 字节，超过 %d MB 上限"
                % (total, DOWNLOAD_MAX_BYTES // (1024 * 1024))), None

    parent = os.path.dirname(p)
    undo_op = _snap(p)
    if parent:
        os.makedirs(parent, exist_ok=True)
    # 先写同目录临时文件再 os.replace：中途失败不留半截文件
    tmp = p + ".part-" + uuid.uuid4().hex[:8]
    h = hashlib.sha1()
    size = 0
    try:
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(DOWNLOAD_CHUNK):
                if not chunk:
                    continue
                size += len(chunk)
                if size > DOWNLOAD_MAX_BYTES:
                    raise ValueError("超过 %d MB 上限"
                                     % (DOWNLOAD_MAX_BYTES // (1024 * 1024)))
                h.update(chunk)
                f.write(chunk)
        os.replace(tmp, p)
    except Exception as e:                    # noqa: BLE001
        try:
            os.remove(tmp)
        except OSError:
            pass
        return "❌ 下载中断：%s" % e, None
    finally:
        try:
            r.close()
        except Exception:                     # noqa: BLE001
            pass

    existed = bool(undo_op.get("existed"))
    return ("已%s %s（%d 字节，sha1=%s）\n来源：%s"
            % ("覆盖" if existed else "下载", _rel(root, p), size,
               h.hexdigest(), final)), [undo_op]


_URL_RE = re.compile(r'https?://[^\s<>"\'，。；;)\]】]+', re.I)


_BV_RE = re.compile(r'\b(?:BV[0-9A-Za-z]{10}|av\d+)\b', re.I)


_ABS_TIME_RE = re.compile(r'\d{4}-\d{1,2}-\d{1,2}[ T]\d{1,2}:\d{2}')


def _extract_abs_time(text):
    m = _ABS_TIME_RE.search(str(text or ""))
    return m.group(0).replace("T", " ") if m else ""


def _extract_url(text):
    m = _URL_RE.search(str(text or ""))
    if m:
        return m.group(0).rstrip(".,;，。；:：")
    m = _BV_RE.search(str(text or ""))
    return m.group(0) if m else ""
