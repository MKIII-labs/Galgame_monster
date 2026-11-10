# -*- coding: utf-8 -*-

import os
import threading
from app import docparse



MAX_READ_BYTES = 256 * 1024  # 搜索 / 索引时的单文件读取上限


MAX_RESULT_CHARS = 20000    # 单个工具结果回灌给模型时的截断长度


IMG_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")


IMG_MAX_BYTES = 12 * 1024 * 1024


IMG_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp"}


WRITE_LOCK = threading.RLock()
# 编译锁：LaTeX / gcc 这类构建只产出「中间文件 / 可执行文件」，
# 不需要和 agent 的源码写操作互斥（latex 一次 15 秒，拿 WRITE_LOCK 会把整个 agent 堵住）。
# 构建之间仍然串行：同一个目录并发跑 latexmk 会互相覆盖 .aux。


def _read_text_file(path, limit=MAX_READ_BYTES):
    """读取文本文件，容忍常见编码（UTF-8 / GBK 等）。

    返回 (text, encoding, size, truncated)；截断可能切在多字节字符中间，最多回退 3 字节再解码。
    """
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        raw = f.read(limit) if limit else f.read()
    truncated = bool(limit) and size > len(raw)
    first = ("utf-8-sig",) if raw[:3] == b"\xef\xbb\xbf" else ("utf-8",)   # 有 BOM 才用 sig
    for back in range(0, 4 if truncated else 1):
        data = raw[:len(raw) - back] if back else raw
        for enc in first + ("gb18030",):
            try:
                return data.decode(enc), enc, size, truncated
            except UnicodeDecodeError:
                continue
    return raw.decode("utf-8", errors="replace"), "utf-8(replace)", size, truncated


def _fmt_size(n):
    for unit in ("B", "KB", "MB"):
        if n < 1024 or unit == "MB":
            return "%.0f%s" % (n, unit) if unit == "B" else "%.1f%s" % (n, unit)
        n /= 1024.0


# ============================================================
# 2. 工具实现（每个返回 (结果文本, 撤销操作列表或 None)）
# ============================================================


def _read_doc_for_agent(root, p, args):
    """PDF / docx / xlsx / pptx 走文档解析器抽正文（支持 start/limit 分段读，便于逐段啃长文档）。

    扫描件没有文本层时这里**没有**视觉回调（工具调用是同步的，逐页识图太慢），
    所以只给出「让用户用附件上传」的提示。
    """
    from .sandbox import _rel
    ext = os.path.splitext(p)[1].lower()
    label = {".pdf": "PDF", ".docx": "Word", ".xlsx": "Excel",
             ".xlsm": "Excel", ".pptx": "PPT"}.get(ext, ext.lstrip(".").upper())
    try:
        with open(p, "rb") as f:
            raw = f.read()
    except OSError as e:
        return "读取失败：" + str(e), None
    text, warn = docparse.extract(os.path.basename(p), raw)
    if not text:
        return ("❌ 无法从这个 %s 提取文本：%s\n"
                "提示：若它是**扫描件 / 纯图片文档**，"
                "请让用户在输入框用「附件」上传该文件——那样程序会用视觉模型逐页识别。"
                % (label, warn or "未知原因")), None
    lines = text.splitlines()
    try:
        start = max(1, int(args.get("start") or 1))
    except (TypeError, ValueError):
        start = 1
    try:
        limit = int(args.get("limit") or 0)
    except (TypeError, ValueError):
        limit = 0
    chunk = lines[start - 1:start - 1 + limit] if limit > 0 else lines[start - 1:]
    width = len(str(start + max(len(chunk), 1) - 1))
    body = "\n".join("%*d | %s" % (width, start + i, s) for i, s in enumerate(chunk))
    head = "文件：%s（%s，共 %d 行，%s%s），下面是第 %d 行起%s：" % (
        _rel(root, p), label, len(lines), _fmt_size(os.path.getsize(p)),
        ("；" + warn) if warn else "", start,
        "（%d 行）" % len(chunk) if limit else "")
    tip = "\n注意：行号只是为了方便你定位；文档很长时可再用 start / limit 分段读。"
    return head + "\n" + body + tip, None


def _looks_binary(path, probe=4096):
    try:
        with open(path, "rb") as f:
            return b"\x00" in f.read(probe)
    except OSError:
        return True


def _preview_paths(op):
    """一条 undo op 实际会动到的文件：[(path, action, kind)]。

    action 是**回滚后**的净效果：restore + existed 表示改回旧内容（modify），
    existed=False 表示这个文件是 AI 新建的、回滚会删掉（delete）。
    """
    kind = op.get("kind")
    if kind == "restore":
        p = op.get("path")
        if not p:
            return []
        return [(p, "modify" if op.get("existed") else "delete", "file")]
    if kind == "move_back":
        out = []
        dst = op.get("dst")
        if dst:
            out.append((dst, "delete", "file"))
        if op.get("src"):
            out.append((op["src"], "create", "file"))
        return out
    if kind in ("mkdir", "restore_dir"):
        p = op.get("path")
        return [(p, "create", "dir")] if p else []
    if kind == "rmdir":
        p = op.get("path")
        return [(p, "delete", "dir")] if p else []
    return []


def _preview_display(path, root):
    """展示路径：沙箱内给相对路径，沙箱外给绝对路径。"""
    if not path:
        return ""
    p = path
    if root:
        try:
            base = os.path.normcase(os.path.abspath(root).rstrip("\\/")) + os.sep
            full = os.path.abspath(p)
            if os.path.normcase(full).startswith(base):
                p = os.path.relpath(full, os.path.abspath(root))
        except (OSError, ValueError):
            p = path
    return p.replace("\\", "/")


IMAGE_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".ico": "image/x-icon", ".avif": "image/avif",
    ".svg": "image/svg+xml",          # 只用于 <img> 展示；SVG 里的脚本不会执行
}


MAX_IMAGE_BYTES = 12 * 1024 * 1024


def image_mime(rel):
    """按扩展名返回图片 MIME；非图片返回 None"""
    return IMAGE_MIME.get(os.path.splitext(str(rel or ""))[1].lower())


def read_bytes(sandbox, rel, limit=MAX_IMAGE_BYTES):
    """读取图片等二进制文件的原始字节（用于预览），带沙箱校验与大小上限"""
    from .sandbox import (SandboxError, resolve_path)
    p = resolve_path(sandbox, rel)
    if os.path.isdir(p):
        raise SandboxError("这是目录，不是文件")
    if not os.path.isfile(p):
        raise SandboxError("文件不存在：" + str(rel))
    size = os.path.getsize(p)
    if size > limit:
        raise SandboxError("文件过大（%.1f MB），暂不支持预览" % (size / 1048576.0))
    with open(p, "rb") as f:
        return f.read(), size


def read_text(sandbox, rel):
    """读一个文本文件（用于编辑器），容忍 UTF-8 / GBK 等编码"""
    from .sandbox import (SandboxError, resolve_path)
    p = resolve_path(sandbox, rel)
    if os.path.isdir(p):
        raise SandboxError("这是目录，不是文件")
    if not os.path.isfile(p):
        raise SandboxError("文件不存在：" + str(rel))
    if _looks_binary(p):
        raise SandboxError("这是二进制文件，无法在编辑器里打开")
    text, enc, size, truncated = _read_text_file(p, limit=2 * 1024 * 1024)
    return {"text": text, "encoding": enc, "size": size, "truncated": truncated}


def _write_enc(p):
    """沿用文件原编码写回；新文件用 utf-8"""
    if os.path.isfile(p):
        try:
            _t, e, _s, _r = _read_text_file(p, limit=0)
            if e in ("utf-8", "utf-8-sig", "gb18030"):
                return e
        except OSError:
            pass
    return "utf-8"


def write_text(sandbox, rel, content):
    """保存文本到文件（保持原编码）"""
    from .sandbox import (SandboxError, _rel, _sandbox_root, resolve_path)
    p = resolve_path(sandbox, rel)
    if os.path.isdir(p):
        raise SandboxError("目标是目录，不能写入")
    enc = _write_enc(p)
    parent = os.path.dirname(p)
    if parent:
        os.makedirs(parent, exist_ok=True)
    # 编辑器保存也走全局写锁：避免和 agent / 子代理的写操作交错
    with WRITE_LOCK:
        with open(p, "w", encoding=enc, newline="") as f:
            f.write(content if content is not None else "")
    return {"encoding": enc, "path": _rel(_sandbox_root(sandbox), p)}
