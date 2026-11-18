# -*- coding: utf-8 -*-
import re
import time

def _md_name(name, fallback):
    """把会话标题变成安全的文件名（去掉路径分隔符与非法字符）"""
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", str(name or "")).strip(" .")
    return (s or fallback)[:60]


def session_markdown(s):
    """把一条会话导出成 Markdown"""
    out = ["# " + str(s.get("title") or "新对话"), ""]
    msgs = s.get("messages") or []
    out.append("- 导出时间：" + time.strftime("%Y-%m-%d %H:%M"))
    out.append("- 消息数：%d" % len(msgs))
    out.append("")
    for m in msgs:
        if not isinstance(m, dict) or m.get("role") == "system":
            continue
        who = "我" if m.get("role") == "user" else "AI"
        out.append("### " + who)
        out.append("")
        body = str(m.get("content") or "").strip()
        if body:
            out.append(body)
            out.append("")
        _docs = m.get("docs") if isinstance(m.get("docs"), list) else None
        if not _docs:
            _docs = [m.get("doc")] if isinstance(m.get("doc"), dict) else []
        for doc in _docs:
            if isinstance(doc, dict) and doc.get("name"):
                out.append("> 附件：" + str(doc["name"]))
                out.append("")
        vs = m.get("vision")
        if vs:
            out.append("> 图片识别：" + str(vs).replace("\n", " ").strip())
            out.append("")
        ims = m.get("images")
        if not isinstance(ims, list) or not ims:
            im = m.get("image")
            ims = [im] if isinstance(im, dict) else []
        for im in ims:
            if isinstance(im, dict) and im.get("name"):
                out.append("![图片](uploads/" + str(im["name"]) + ")")
                out.append("")
        if m.get("interrupted"):
            out.append("> ⚠️ 这条回复曾被中断（可在应用里点「继续输出」）")
            out.append("")
    return "\n".join(out)
