# -*- coding: utf-8 -*-
"""deploy_site：把沙箱里的静态网页发布到内置静态服务器（本机 + 局域网）。"""
import os


def _t_deploy_site(root, args):
    from app import deploy
    from .sandbox import SandboxError, _rel, resolve_path

    a = args if isinstance(args, dict) else {}
    action = str(a.get("action") or "publish").strip().lower()

    if action in ("list", "ls", "status"):
        rows = deploy.list_sites()
        if not rows:
            return "还没有发布过站点。用 deploy_site(path=...) 发布一个静态目录。", None
        lines = ["已发布站点："]
        for s in rows:
            lines.append("- %s（id=%s）%s%s%s"
                         % (s.get("name") or s["id"], s["id"],
                            "运行中 " if s.get("running") else "已停止 ",
                            s.get("url") or "",
                            ("  局域网：%s" % s["lan_url"]) if s.get("lan_url") else ""))
        return "\n".join(lines), None

    if action in ("stop", "close", "cancel"):
        sid = str(a.get("site_id") or a.get("name") or "").strip()
        if not sid:
            return "❌ stop 需要 site_id（可先用 action=list 查看）。", None
        r = deploy.stop(sid)
        return (("已停止站点：%s" % r["id"]) if r.get("ok")
                else "❌ " + str(r.get("error") or "停止失败")), None

    path = str(a.get("path") or ".").strip() or "."
    try:
        src = resolve_path(root, path, for_read=True)
    except SandboxError as e:
        return "❌ " + str(e), None
    if not os.path.isdir(src):
        return "❌ path 必须是目录（静态站点的根，里面要有 index.html）：" + path, None
    try:
        site = deploy.publish(src, name=str(a.get("name") or ""),
                              port=int(a.get("port") or 0),
                              source_label=_rel(root, src) if root else src)
    except ValueError as e:
        return "❌ " + str(e), None
    except Exception as e:                  # noqa: BLE001
        return "❌ 发布失败：%s" % e, None

    lines = ["已发布「%s」到本机静态服务器（只含发布文件，隐藏文件 / .git / node_modules 已跳过）："
             % (site.get("name") or site["id"]),
             "- 本机：%s" % site["url"]]
    if site.get("lan_url"):
        lines.append("- 局域网（手机连同一个 Wi-Fi 可访问）：%s" % site["lan_url"])
    if not site.get("has_index"):
        lines.append("提示：目录里没有 index.html，根路径会 404，需要直接访问具体文件名。")
    lines.append("站点 id：%s；文件 %d 个 / %.1f KB；重新发布会原地更新这个站点。"
                 % (site["id"], int(site.get("files") or 0),
                    (int(site.get("bytes") or 0) / 1024.0)))
    lines.append("仅本机与局域网可见，没有上传到任何外部平台。")
    # 前端识别这个标记后自动打开预览面板（与 open_preview 同一机制）
    lines.append("GM_PREVIEW_URL:%s" % site["url"])
    return "\n".join(lines), None
