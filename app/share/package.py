# -*- coding: utf-8 -*-
""".gmshare 包：zip(manifest.json + messages.json + preview.html [+ assets/*])。

用途：同一应用之间无损还原（文本、选项、脱敏报告、预览页）。
导入侧只解析 JSON 与受控资源，**不执行**包内 HTML；有大小与条目数上限。
"""
import hashlib
import io
import json
import re
import time
import zipfile

FORMAT = "gmshare"
VERSION = 1
MAX_ZIP = 40 * 1024 * 1024        # 压缩包本体上限
MAX_UNZIP = 120 * 1024 * 1024     # 解压后总量上限（防 zip bomb）
MAX_FILES = 200
_NAME_RE = re.compile(r"^(?:manifest|messages)\.json$|^preview\.html$|^assets/[A-Za-z0-9_.\-]+$")


def _sha(b):
    return "sha256:" + hashlib.sha256(b).hexdigest()


def build(messages, preview_html, title="", app_version="", redacted=None, options=None):
    """返回 zip 字节。messages 是 dict（会被 JSON 序列化）。"""
    body = json.dumps(messages, ensure_ascii=False, indent=1).encode("utf-8")
    preview = str(preview_html or "").encode("utf-8")
    manifest = {
        "format": FORMAT,
        "version": VERSION,
        "created_at": int(time.time()),
        "app": app_version,
        "title": str(title or "")[:120],
        "options": options or {},
        "redacted": redacted or {},
        "checksums": {"messages.json": _sha(body), "preview.html": _sha(preview)},
    }
    man = json.dumps(manifest, ensure_ascii=False, indent=1).encode("utf-8")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", man)
        z.writestr("messages.json", body)
        z.writestr("preview.html", preview)
    return buf.getvalue()


def read(data):
    """解析并校验；返回 {manifest, messages, preview}。异常抛 ValueError（信息可直接给用户）。"""
    if not data:
        raise ValueError("包是空的")
    if len(data) > MAX_ZIP:
        raise ValueError("包超过 40MB")
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError("不是有效的 .gmshare（zip）文件")
    names = [n for n in z.namelist() if not n.endswith("/")]
    if len(names) > MAX_FILES:
        raise ValueError("包内文件过多")
    total = 0
    for n in names:
        if not _NAME_RE.match(n):
            raise ValueError("包内出现不允许的路径：%s" % n[:60])
        total += z.getinfo(n).file_size
    if total > MAX_UNZIP:
        raise ValueError("解压后体积超过上限")
    try:
        raw_m = z.read("messages.json")
        raw_p = z.read("preview.html")
        manifest = json.loads(z.read("manifest.json").decode("utf-8"))
        messages = json.loads(raw_m.decode("utf-8"))
        preview = raw_p.decode("utf-8", "replace")
    except KeyError as e:
        raise ValueError("包缺少必要文件：%s" % e)
    except (ValueError, UnicodeDecodeError):
        raise ValueError("包内 JSON 损坏")
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise ValueError("不是 gmshare 格式")
    if int(manifest.get("version") or 0) > VERSION:
        raise ValueError("包版本比当前应用新，请升级后再导入")
    sums = manifest.get("checksums") or {}
    for name, raw in (("messages.json", raw_m), ("preview.html", raw_p)):
        want = sums.get(name)
        if want and want != _sha(raw):
            raise ValueError("包内容校验失败：%s" % name)
    return {"manifest": manifest, "messages": messages, "preview": preview}
