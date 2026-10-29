# -*- coding: utf-8 -*-
import re
import time

def _md_name(name, fallback):
    """把会话标题变成安全的文件名（去掉路径分隔符与非法字符）"""
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", str(name or "")).strip(" .")
    return (s or fallback)[:60]


def session_markdown(s):
    """把一条会话导出成 Markdown（普通对话与 GAL 演出都支持）"""
    out = ["# " + str(s.get("title") or "新对话"), ""]
    msgs = s.get("messages") or []
    out.append("- 导出时间：" + time.strftime("%Y-%m-%d %H:%M"))
    out.append("- 消息数：%d" % len(msgs))
    out.append("")
    for m in msgs:
        if not isinstance(m, dict) or m.get("role") == "system":
            continue
        who = "我" if m.get("role") == "user" else "AI"
        g = m.get("gal") if isinstance(m.get("gal"), dict) else None
        if g and (g.get("dialog") or []):          # GAL 会话：按对白导出
            out.append("### " + who)
            out.append("")
            for seg in g.get("dialog") or []:
                if not isinstance(seg, dict):
                    continue
                if seg.get("narration"):
                    out.append("> （" + str(seg["narration"]) + "）")
                else:
                    out.append("**" + str(seg.get("name") or "角色") + "**：" + str(seg.get("text") or ""))
                out.append("")
            for opt in g.get("options") or []:
                if isinstance(opt, dict) and opt.get("text"):
                    out.append("- 选项：" + str(opt["text"]))
            if g.get("options"):
                out.append("")
            continue
        out.append("### " + who)
        out.append("")
        body = str(m.get("content") or "").strip()
        if body:
            out.append(body)
            out.append("")
        doc = m.get("doc")
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
