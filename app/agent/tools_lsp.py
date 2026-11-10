# -*- coding: utf-8 -*-




def _lsp_line(args):
    try:
        return int(args.get("line") or 0)
    except (TypeError, ValueError):
        return 0


def _t_lsp_diagnostics(root, args):
    from .sandbox import (_real, resolve_path)
    from app import lsp
    sub = str(args.get("path") or "").strip()
    base = resolve_path(root, sub) if sub else _real(root)
    try:
        return lsp.diagnostics(root, base)
    except lsp.Unavailable as e:
        return "❌ %s" % e, None


def _t_lsp_definition(root, args):
    from .sandbox import resolve_path
    from app import lsp
    line = _lsp_line(args)
    if line <= 0:
        return "请提供 line：符号所在的行号（从 1 开始）", None
    p = resolve_path(root, args.get("path"))
    try:
        return lsp.definition(root, p, line, str(args.get("symbol") or ""))
    except lsp.Unavailable as e:
        return "❌ %s" % e, None


def _t_lsp_references(root, args):
    from .sandbox import resolve_path
    from app import lsp
    line = _lsp_line(args)
    if line <= 0:
        return "请提供 line：符号所在的行号（从 1 开始）", None
    p = resolve_path(root, args.get("path"))
    try:
        return lsp.references(root, p, line, str(args.get("symbol") or ""))
    except lsp.Unavailable as e:
        return "❌ %s" % e, None


def _t_lsp_hover(root, args):
    """悬停信息：签名 / 类型 / 文档，不用读整个文件。"""
    from .sandbox import resolve_path
    from app import lsp
    line = _lsp_line(args)
    if line <= 0:
        return "请提供 line：符号所在的行号（从 1 开始）", None
    p = resolve_path(root, args.get("path"))
    try:
        return lsp.hover(root, p, line, str(args.get("symbol") or ""))
    except lsp.Unavailable as e:
        return "❌ %s" % e, None


def _t_lsp_symbols(root, args):
    """给了 path 就是文件内符号树，否则按名字全项目搜符号。"""
    from .sandbox import resolve_path
    from app import lsp
    sub = str(args.get("path") or "").strip()
    try:
        if sub:
            return lsp.document_symbols(root, resolve_path(root, sub))
        return lsp.workspace_symbols(root, str(args.get("query") or ""))
    except lsp.Unavailable as e:
        return "❌ %s" % e, None


def _t_lsp_implementation(root, args):
    """谁实现了这个接口 / 抽象方法。"""
    from .sandbox import resolve_path
    from app import lsp
    line = _lsp_line(args)
    if line <= 0:
        return "请提供 line：符号所在的行号（从 1 开始）", None
    p = resolve_path(root, args.get("path"))
    try:
        return lsp.implementation(root, p, line, str(args.get("symbol") or ""))
    except lsp.Unavailable as e:
        return "❌ %s" % e, None


def _t_lsp_calls(root, args):
    """调用链：incoming = 谁调用了它，outgoing = 它调用了谁。"""
    from .sandbox import resolve_path
    from app import lsp
    line = _lsp_line(args)
    if line <= 0:
        return "请提供 line：符号所在的行号（从 1 开始）", None
    p = resolve_path(root, args.get("path"))
    direction = str(args.get("direction") or "incoming")
    try:
        return lsp.call_hierarchy(root, p, line,
                                  str(args.get("symbol") or ""), direction)
    except lsp.Unavailable as e:
        return "❌ %s" % e, None


def _t_lsp_rename(root, args):
    from .sandbox import (invalidate_tree, resolve_path)
    from .undo import _snap
    from app import lsp
    line = _lsp_line(args)
    if line <= 0:
        return "请提供 line：符号所在的行号（从 1 开始）", None
    p = resolve_path(root, args.get("path"))
    try:
        files, note = lsp.rename_plan(root, p, line,
                                      str(args.get("symbol") or ""),
                                      str(args.get("new_name") or ""))
    except lsp.Unavailable as e:
        return "❌ %s" % e, None
    if not files:
        return note, None
    undo_ops = []
    for f in files:
        target = f["path"]                  # rename_plan 已保证在沙箱内
        undo_ops.append(_snap(target))
        with open(target, "w", encoding="utf-8", newline="") as fh:
            fh.write(f["new"])
    invalidate_tree(root)
    return (note + "\n\n已写入。建议再跑一次 lsp_diagnostics，确认没有漏掉的引用。",
            undo_ops)


# ---- Git：只读查询 + 提交（提交是否放行由 chat 层按配置判定） ----
