# -*- coding: utf-8 -*-
"""提示词模板：长文本放在 resources/prompts/*.md，Python 只做装配。

模板格式
--------
一个 .md 文件由若干「段落」组成，用一行 `@@ 段名` 分隔；段内正文按原样保留
（行间用 \\n 连接，段末空行会被去掉）。正文里的 `{{key}}` 由 render() 用传进来的
值替换。

刻意不引入复杂模板语法：**条件分支留在 Python 里做**（按条件挑选段落），这样每一段
都是可以被逐字审阅 / 编辑的纯文本，重构前后也容易用快照逐字节对拍。
"""
import os
import re
import threading

from app.store import RESOURCES_DIR

_DIR = os.path.join(RESOURCES_DIR, "prompts")
_cache = {}                     # name -> (mtime, {section: text})
_lock = threading.Lock()
_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


def _rstrip_empty(lines):
    end = len(lines)
    while end > 0 and lines[end - 1].strip() == "":
        end -= 1
    return lines[:end]


def _parse(text):
    out = {}
    cur = None
    buf = []
    for line in text.split("\n"):
        if line.startswith("@@ "):
            if cur is not None:
                out[cur] = "\n".join(_rstrip_empty(buf))
            cur = line[3:].strip()
            buf = []
        elif cur is not None:
            buf.append(line)
    if cur is not None:
        out[cur] = "\n".join(_rstrip_empty(buf))
    return out


def load(name, /):
    """读取并解析模板，返回 {段名: 正文}。按 mtime 缓存，改 .md 即时生效。"""
    path = os.path.join(_DIR, str(name) + ".md")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    with _lock:
        hit = _cache.get(name)
        if hit and hit[0] == mtime:
            return hit[1]
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return {}
    data = _parse(text.replace("\r\n", "\n").replace("\r", "\n"))
    with _lock:
        _cache[name] = (mtime, data)
    return data


def render(text, /, **kw):
    """把 {{key}} 换成 kw[key]（字符串化）；未知 key 原样保留，便于发现拼写错误。"""
    def _sub(m):
        k = m.group(1)
        return str(kw[k]) if k in kw else m.group(0)
    return _PLACEHOLDER.sub(_sub, str(text or ""))


def part(name, section, /, **kw):
    """取模板的某一段并渲染。"""
    return render(load(name).get(section, ""), **kw)
