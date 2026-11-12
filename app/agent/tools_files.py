# -*- coding: utf-8 -*-

import base64
import os
import re
import shutil
import time
from app import docparse
from app import langs
from app import tool_meta



def _t_tool_search(args, caps):
    """tool_search 的实现：匹配工具组并登记解锁，返回参数速查。"""
    from .groups import (TOOL_GROUPS, TOOL_GROUP_KEYS, tool_group_catalog)
    args = args if isinstance(args, dict) else {}
    q = str(args.get("query") or "").strip().lower()
    want = str(args.get("group") or "").strip().lower()
    hit = []
    for group, names in TOOL_GROUPS.items():
        keys = TOOL_GROUP_KEYS.get(group, ())
        meta_name = tool_meta.GROUP_META.get(group, "")
        if want and want in (group, meta_name):
            hit.append(group)
        elif q and (group in q or (meta_name and meta_name in q)
                    or any(str(k).lower() in q for k in keys)
                    or any(n in q for n in names)):
            hit.append(group)
    if not hit and q:
        # 关键词没命中：按工具名的子串再找一遍（模型可能直接写了工具名）
        for group, names in TOOL_GROUPS.items():
            if any(n in q for n in names):
                hit.append(group)
    if not hit:
        return ("没有匹配的工具组。可用：\n" + tool_group_catalog())
    unlock = caps.get("tool_unlock") if isinstance(caps, dict) else None
    if unlock is None:
        unlock = set()
        if isinstance(caps, dict):
            caps["tool_unlock"] = unlock
    unlock.update(hit)
    lines = ["已解锁（下一轮生效）："]
    for group in hit:
        label, _shown = tool_meta.group_catalog(group, TOOL_GROUPS[group])
        lines.append("- %s：%s" % (group, label))
    return "\n".join(lines)


def _t_list_dir(root, args, allow_outside=False):
    from .base import MAX_LIST_ENTRIES
    from .groups import (BLOCKED_SEGMENTS, READ_ONLY_SEGMENTS)
    from .io_utils import _fmt_size
    from .sandbox import (SandboxError, _real, _rel, resolve_path)
    p = resolve_path(root, args.get("path") or "", allow_outside, for_read=True)
    if not os.path.isdir(p):
        return "不是目录：" + _rel(root, p), None
    # 沙箱外目录：列表里直接给绝对路径。否则模型拿到 ../xxx 再去 read_file，
    # 相对路径会被 resolve_path 拒掉（只有绝对路径能跨沙箱），看起来就像「时好时坏」。
    root_real = _real(root)
    outside = bool(allow_outside) and not (
        p == root_real or p.startswith(root_real + os.sep))
    rows = []
    for name in sorted(os.listdir(p)):
        if name in BLOCKED_SEGMENTS and name not in READ_ONLY_SEGMENTS:
            continue
        full = os.path.join(p, name)
        try:
            resolve_path(root, full, allow_outside, for_read=True)
        except SandboxError:
            continue
        try:
            is_dir = os.path.isdir(full)
            size = 0 if is_dir else os.path.getsize(full)
        except OSError:
            continue
        label = full.replace("\\", "/") if outside else name
        rows.append("%s  %s%s" % (_fmt_size(size) if not is_dir else "<目录>",
                                  label, "/" if is_dir else ""))
        if len(rows) >= MAX_LIST_ENTRIES:
            rows.append("…（已达 %d 条上限）" % MAX_LIST_ENTRIES)
            break
    head = "目录：%s（共 %d 项%s）" % (
        p.replace("\\", "/") if outside else (_rel(root, p) or "."),
        len(rows),
        "，沙箱外：以下为绝对路径" if outside else "")
    return head + "\n" + ("\n".join(rows) if rows else "（空）"), None


def _t_read_file(root, args, allow_outside=False):
    from .approval import (_secret_allowed, _secret_deny_text, is_secret_path)
    from .base import MAX_READ_FILE_BYTES
    from .io_utils import (_fmt_size, _looks_binary, _read_doc_for_agent, _read_text_file)
    from .sandbox import (_rel, resolve_path)
    p = resolve_path(root, args.get("path"), allow_outside, for_read=True)
    if not os.path.isfile(p):
        return "文件不存在：" + _rel(root, p), None
    _relp = _rel(root, p)
    _sec = is_secret_path(_relp if not allow_outside else p)
    if _sec and not _secret_allowed(root, [_relp]):
        return _secret_deny_text(_sec), None
    ext = os.path.splitext(p)[1].lower()
    # 富文档（PDF / Word / Excel / PPT）不能按文本读：统一交给 docparse 抽正文，
    # 与「附件」通道共用同一套解析，避免两处行为不一致。
    if ext in (".pdf", ".docx", ".xlsx", ".xlsm", ".pptx"):
        return _read_doc_for_agent(root, p, args)
    if ext in (".doc", ".xls", ".ppt"):
        return ("❌ 不支持旧版 Office 格式（%s）。请让用户另存为 .docx / .xlsx / .pdf，"
                "或在输入框用「附件」上传。" % ext), None
    if ext not in docparse.TEXT_EXT and _looks_binary(p):
        return ("❌ 这是二进制文件（%s），不能按文本读。图片请用 vision 相关工具，"
                "文档请让用户在输入框用「附件」上传。" % (ext or "无扩展名")), None
    text, enc, size, truncated = _read_text_file(p, MAX_READ_FILE_BYTES)
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
    raw = bool(args.get("raw"))
    # 大文件默认给大纲（read_outline_mode，默认关）：模型先看结构再指定区间，
    # 避免一上来就把整文件塞进上下文、并在后续每一轮里重发。
    if (args.get("_outline") and not limit and start == 1 and not raw
            and len(lines) >= int(args.get("_outline_min") or 0)):
        try:
            from app import repomap
            _ext = os.path.splitext(p)[1].lower()
            if _ext in repomap.PY_EXT or _ext in repomap.JS_EXT:
                _ol = repomap.outline(p)
                if _ol:
                    return ("文件大纲：%s（共 %d 行，%s）。下面是符号与起始行号；"
                            "看某一节请用 read_file(path=..., start=行号, limit=行数)。\n%s"
                            % (_relp, len(lines), _fmt_size(size), _ol)), None
        except Exception:                 # noqa: BLE001  大纲失败就退回普通读取
            pass
    head = "文件：%s（共 %d 行，%s%s，编码 %s），下面是第 %d 行起%s：" % (
        _rel(root, p), len(lines), _fmt_size(size),
        "，已截断" if truncated else "", enc, start,
        "（%d 行）" % len(chunk) if limit else "")
    if raw:
        # 原文模式：不加行号。模型需要精确匹配 / 自己统计时要的就是这份原始文本，
        # 以前只能写脚本 open() 去拿，现在直接给。
        return head + "（原文，无行号）\n" + "\n".join(chunk), None
    width = len(str(start + max(len(chunk), 1) - 1))
    body = "\n".join("%*d | %s" % (width, start + i, s) for i, s in enumerate(chunk))
    tip = "\n注意：行号只是为了方便你定位，edit_file 的 old 参数必须用不含行号的原文。"
    return head + "\n" + body + tip, None


def _t_read_files(root, args, allow_outside=False):
    """批量读取：一次返回多个文件，减少「一轮只读一个」的往返。"""
    from .base import MAX_READ_FILES
    paths = args.get("paths")
    if isinstance(paths, str):
        paths = [paths]
    if not isinstance(paths, (list, tuple)) or not paths:
        return "❌ read_files 需要 paths 数组", None
    out = []
    for p in list(paths)[:MAX_READ_FILES]:
        one = {"path": p}
        for k in ("start", "limit", "raw"):
            if args.get(k) is not None:
                one[k] = args[k]
        try:
            text, _undo = _t_read_file(root, one, allow_outside)
        except Exception as e:              # noqa: BLE001  单个文件失败不影响其它
            text = "❌ 读取 %s 失败：%s" % (p, e)
        out.append(str(text))
    if len(paths) > MAX_READ_FILES:
        out.append("…（一次最多读 %d 个，其余请分批）" % MAX_READ_FILES)
    return "\n\n".join(out), None


def _t_search_files(root, args):
    """按内容正则 / 文件名 glob 搜索。

    与旧版相比：同一个文件不再「命中第一条就停」（旧版每文件只报一处，
    想看「所有出现位置」只能去写脚本），默认每文件列 5 处；可带上下文；
    也可以用 count_only 只统计每个文件命中几处。
    """
    from .approval import (_match_rules, is_secret_path)
    from .base import (MAX_SEARCH_CONTEXT, MAX_SEARCH_FILES, MAX_SEARCH_HITS, MAX_SEARCH_PER_FILE, _glob_to_regex)
    from .io_utils import (_looks_binary, _read_text_file)
    from .rules import load_effective_rules
    from .sandbox import (_rel, _walk)
    pattern = (args.get("pattern") or "").strip()
    globpat = (args.get("glob") or "").strip()
    if not pattern and not globpat:
        return "请至少提供 pattern（内容关键字）或 glob（文件名匹配）", None
    try:
        rx = re.compile(pattern) if pattern else None
    except re.error:
        rx = re.compile(re.escape(pattern)) if pattern else None
    glob_rx = _glob_to_regex(globpat) if globpat else None
    try:
        context = max(0, min(int(args.get("context") or 0), MAX_SEARCH_CONTEXT))
    except (TypeError, ValueError):
        context = 0
    try:
        per_file = max(1, min(int(args.get("max_per_file") or MAX_SEARCH_PER_FILE), 50))
    except (TypeError, ValueError):
        per_file = MAX_SEARCH_PER_FILE
    count_only = bool(args.get("count_only"))
    # 规则只读一次：遍历几千个文件时不能每个都去碰一次磁盘
    _rules, _rerr = load_effective_rules(root)
    blocks = []             # 实际展示的命中块
    per_file_counts = []    # (相对路径, 命中数)
    total = 0
    shown = 0
    for dirpath, _dirnames, filenames in _walk(root):
        if not count_only and shown >= MAX_SEARCH_HITS:
            break
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rp = _rel(root, full)
            # 秘密文件不参与内容检索：否则读不到也能从搜索结果里把密钥带出来
            _sec = is_secret_path(rp)
            if _sec and not (_rerr == "" and _match_rules(_rules, [rp])[0] == "allow"):
                continue
            # glob 同时匹配「相对路径」与「文件名」（*.py 也能命中深层文件；**/*.py 亦可）
            if glob_rx and not (glob_rx.match(rp) or glob_rx.match(fn)):
                continue
            if rx is None:
                total += 1
                per_file_counts.append((rp, 1))
                if not count_only and shown < MAX_SEARCH_HITS:
                    blocks.append("%s  （文件名匹配 %s）" % (rp, globpat))
                    shown += 1
                continue
            file_hits = 0
            if rx.search(fn):
                file_hits += 1
                total += 1
                if not count_only and shown < MAX_SEARCH_HITS:
                    blocks.append("%s  （文件名命中）" % rp)
                    shown += 1
            if not _looks_binary(full):          # 只搜文本，跳过图片等二进制
                try:
                    text, _enc, _size, _tr = _read_text_file(full)
                except OSError:
                    text = ""
                lines = text.splitlines()
                printed_here = 0
                for i, line in enumerate(lines, 1):
                    if not rx.search(line):
                        continue
                    file_hits += 1
                    total += 1
                    if count_only or printed_here >= per_file or shown >= MAX_SEARCH_HITS:
                        continue
                    block = ["%s:%d: %s" % (rp, i, line.strip()[:160])]
                    if context:
                        lo = max(1, i - context)
                        hi = min(len(lines), i + context)
                        for j in range(lo, hi + 1):
                            if j != i:
                                block.append("    | %d: %s" % (j, lines[j - 1][:160]))
                    blocks.append("\n".join(block))
                    printed_here += 1
                    shown += 1
            if file_hits:
                per_file_counts.append((rp, file_hits))
    if count_only:
        if not total:
            # 零命中是正常结果，不是错误：文案里不能出现「未找到」或「沙箱」，
            # 否则 tool_protocol 会把它升级成错误（历史上被误判成 SANDBOX_DENIED）
            return "没有匹配（搜索范围：%s）" % root, None
        rows = ["%s: %d" % (rp, n) for rp, n in per_file_counts[:MAX_SEARCH_FILES]]
        more = ""
        if len(per_file_counts) > MAX_SEARCH_FILES:
            more = "\n…（还有 %d 个文件未列出，可用 glob 缩小范围）" % (
                len(per_file_counts) - MAX_SEARCH_FILES)
        return "共 %d 处命中，涉及 %d 个文件：\n%s%s" % (
            total, len(per_file_counts), "\n".join(rows), more), None
    if not blocks:
        # 零命中是正常结果，不是错误：文案里不能出现「未找到」或「沙箱」，
        # 否则 tool_protocol 会把它升级成错误（历史上被误判成 SANDBOX_DENIED）
        return "没有匹配（搜索范围：%s）" % root, None
    return "共 %d 处命中，涉及 %d 个文件；下面最多列 %d 处：\n%s" % (
        total, len(per_file_counts), MAX_SEARCH_HITS, "\n\n".join(blocks)), None


def _t_search_code(root, args):
    """语义检索（本地 embedding）。模型未就绪时 codeindex 会自动退回关键词检索。"""
    from app import codeindex
    q = str(args.get("query") or "").strip()
    if not q:
        return "请提供 query：用一句自然语言描述你要找的代码或逻辑。", None
    return codeindex.search(root, q, args.get("limit") or 6), None


def _t_repo_map(root, args):
    """仓库结构图：不花 token，帮模型少读文件。"""
    from app import repomap
    return repomap.build(root, args.get("path") or "", args.get("max_files") or repomap.MAX_FILES), None


# ---- 轻量「LSP」：按符号找定义 / 引用，按需做语法体检 ----


def _t_find_symbol(root, args):
    """按符号名找定义与引用（沙箱内）。

    这是「轻量 LSP」的 goto-definition / find-references：不需要 pyright，
    用正则 + 行号给出位置，模型再按行号精读。
    """
    from .base import (SYMBOL_EXTS, SYMBOL_MAX_BYTES, SYMBOL_MAX_FILES, SYMBOL_PER_FILE_REFS, SYMBOL_REF_LIMIT, _symbol_def_re)
    from .sandbox import (_real, _rel, _walk, resolve_path)
    name = str(args.get("name") or "").strip()
    if not name or len(name) > 80:
        return "请提供要查找的符号名（函数 / 类 / 变量）", None
    sub = str(args.get("path") or "").strip()
    base = resolve_path(root, sub) if sub else _real(root)
    if not os.path.isdir(base) and not os.path.isfile(base):
        return "❌ 路径不存在：%s" % (sub or "."), None

    def_re = _symbol_def_re(name)
    ref_re = re.compile(r"\b%s\b" % re.escape(name))
    files = 0
    defs, refs = [], []
    targets = [base] if os.path.isfile(base) else [
        os.path.join(dp, fn) for dp, _dns, fns in _walk(base) for fn in fns]
    for full in targets:
        if files >= SYMBOL_MAX_FILES:
            break
        if os.path.splitext(full)[1].lower() not in SYMBOL_EXTS:
            continue
        try:
            if os.path.getsize(full) > SYMBOL_MAX_BYTES:
                continue
        except OSError:
            continue
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        files += 1
        rel = _rel(root, full)
        per_file_refs = 0
        for i, line in enumerate(text.splitlines(), 1):
            if def_re.search(line):
                defs.append((rel, i, line.strip()[:120]))
            elif ref_re.search(line):
                if len(refs) < SYMBOL_REF_LIMIT and per_file_refs < SYMBOL_PER_FILE_REFS:
                    refs.append((rel, i, line.strip()[:100]))
                    per_file_refs += 1

    if not defs and not refs:
        return ("在 %d 个文件里没有找到符号「%s」。可以换用 search_files 按正则搜，"
                "或先用 search_code 做语义检索。" % (files, name)), None
    lines = ["符号「%s」：扫描 %d 个文件，定义 %d 处，引用 %d 条%s"
             % (name, files, len(defs), len(refs),
                "（引用只列前 %d 条）" % SYMBOL_REF_LIMIT if len(refs) >= SYMBOL_REF_LIMIT else "")]
    if defs:
        lines.append("\n【定义】")
        for rel, i, txt in defs[:40]:
            lines.append("  %s:%d  %s" % (rel, i, txt))
    if refs:
        lines.append("\n【引用】")
        for rel, i, txt in refs:
            lines.append("  %s:%d  %s" % (rel, i, txt))
        lines.append("\n（需要看上下文时用 read_file(path, start=行号-5, limit=20)）")
    return "\n".join(lines), None


# ---- 代码上下文包 / 变更影响：符号图 + 语义检索的聚合 ----


def _t_code_context(root, args):
    """一次调用给全上下文：定义 / 引用 / 测试 / 语义片段。

    目的不是「比 grep 更快」，而是**减少往返**：模型原本要 search_code → read_file
    → 再 grep 才能确认「实现在哪、谁在用」，这里一轮给齐。
    """
    from .base import (CODE_CONTEXT_CHARS, CODE_CONTEXT_REFS, CODE_IDENT_RE)
    from app import codeindex, codesym
    q = str(args.get("query") or "").strip()
    sym = str(args.get("symbol") or "").strip()
    sub = str(args.get("path") or "").strip()
    if not q and not sym:
        return "请提供 query（要找什么）或 symbol（确切符号名）", None
    if not sym and CODE_IDENT_RE.match(q):
        sym = q                      # 纯标识符（load_config / Class.method）直接查符号图

    parts = []
    if sym:
        got = codesym.lookup(root, sym, sub)
        rows = ["【符号 %s】定义 %d 处 / 引用 %d 处 / 调用 %d 处（已解析 %d 个文件）"
                % (sym, len(got["defs"]), len(got["refs"]), len(got["calls"]), got["total"])]
        if got["defs"]:
            for rel, d in got["defs"][:8]:
                rows.append("  %s:%d  %s %s%s" % (rel, d["line"], d["kind"],
                                                  d["name"], d.get("sig") or ""))
                if d.get("doc"):
                    rows.append("      # " + d["doc"])
        else:
            rows.append("  （没有找到定义；可能是动态生成 / 拼写不同 / 不在索引范围，"
                        "不要据此断定它不存在）")
        if got["refs"]:
            per, shown = {}, []
            for rel, r in got["refs"]:
                if per.get(rel, 0) >= 2:
                    continue
                per[rel] = per.get(rel, 0) + 1
                shown.append("  %s:%d  (%s)" % (rel, r["line"], r.get("qual") or "模块级"))
                if len(shown) >= CODE_CONTEXT_REFS:
                    break
            rows.append("【直接引用】")
            rows.extend(shown)
        parts.append("\n".join(rows))

    if q:
        boost = codesym.boost_map(root, [sym]) if sym else None
        try:
            frag = codeindex.search(root, q, 4, boost=boost)
        except Exception as e:                       # noqa: BLE001
            frag = "（语义检索不可用：%s）" % e
        parts.append("【语义片段】\n" + frag)

    tests = codesym.tests_for(root, sym or sub) if (sym or sub) else []
    if tests:
        parts.append("【相关测试】\n  " + "\n  ".join(tests))

    text = "\n\n".join(parts)
    if len(text) > CODE_CONTEXT_CHARS:
        text = (text[:CODE_CONTEXT_CHARS]
                + "\n…（上下文包共 %d 字，已截断；可对具体文件用 read_file 精读）" % len(text))
    return text, None


def _t_impact(root, args):
    """变更影响分析：改它会影响谁 + 该重跑哪些测试。"""
    from app import codesym
    target = str(args.get("target") or "").strip()
    if not target:
        return "请提供 target：符号名或文件相对路径", None
    sub = str(args.get("path") or "").strip()
    try:
        text = codesym.impact(root, target, sub)
    except Exception as e:                           # noqa: BLE001
        text = "❌ 影响分析失败：%s" % e
    if text and not str(text).lstrip().startswith("❌"):
        return text, None
    # 非 Python / JS 项目没有符号图：降级为正则检索，不把语言挡在门外
    if re.match(r"^[A-Za-z_$][\w$.:]{2,80}$", target):
        fb, _ = _t_find_symbol(root, {"name": target, "path": sub})
        return "（该语言暂无符号图，已降级为正则检索）\n" + str(fb), None
    return ("该语言暂无影响分析；可用 read(action=search, pattern=...) "
            "搜符号名，或 code(action=find) 找定义。"), None


def _t_check_code(root, args):
    """对文件或目录做语法体检（.py / .js / .json），返回有问题的地方。

    复用写后自检的同一套规则（post_write_check），所以「平时写文件会不会报错」
    与「主动体检」结论一致。不做类型检查 —— 那需要 pyright / mypy。
    """
    from .base import (CHECK_MAX_FILES, post_write_check)
    from .sandbox import (_real, _rel, _walk, resolve_path)
    sub = str(args.get("path") or "").strip()
    base = resolve_path(root, sub) if sub else _real(root)
    if not os.path.exists(base):
        return "❌ 路径不存在：%s" % (sub or "."), None
    targets = []
    if os.path.isfile(base):
        targets = [base]
    else:
        for dp, _dns, fns in _walk(base):
            for fn in fns:
                if os.path.splitext(fn)[1].lower() in langs.CHECK_EXTS:
                    targets.append(os.path.join(dp, fn))
                if len(targets) >= CHECK_MAX_FILES:
                    break
            if len(targets) >= CHECK_MAX_FILES:
                break
    if not targets:
        return ("没有可检查的文件（当前可快速校验：%s）"
                % " / ".join(sorted(langs.CHECK_EXTS))), None
    problems, checked = [], 0
    for full in targets:
        err = post_write_check(full)
        checked += 1
        if err:
            problems.append("%s：%s" % (_rel(root, full), err))
    if not problems:
        return "语法体检通过：检查了 %d 个文件，没有发现语法错误。" % checked, None
    head = "语法体检：检查 %d 个文件，%d 个有问题：\n" % (checked, len(problems))
    return head + "\n".join("- " + p for p in problems[:30]), None


# ---- pyright（真 LSP）：类型诊断 / 定义 / 引用 / 重命名 ----
# 与 find_symbol 的分工：find_symbol 是「不记得在哪，先扫一遍」，
# 这里是「已经知道在哪，要准」。LSP 首次启动可能要下载 pyright，所以统一
# 把 Unavailable 变成一句人话，绝不让它把整轮对话拖垮。


def _recover_candidates(sid, token=None):
    """收集本会话的写后快照，按路径取最新一份。"""
    from .state import _ensure_loaded
    from .undo import (_UNDO, _undo_lock)
    _ensure_loaded(sid)
    with _undo_lock:
        items = [(t, v) for t, v in _UNDO.items() if v.get("sid") == sid]
    items.sort(key=lambda kv: kv[1].get("ts") or 0)
    if token:
        items = [(t, v) for t, v in items if t == token]
    latest = {}
    for t, item in items:
        for op in item.get("ops") or []:
            if op.get("kind") != "restore" or not op.get("after_existed"):
                continue
            p = op.get("path")
            data = op.get("after")
            if not p or data is None:
                continue
            latest[p] = {"token": t, "ts": item.get("ts") or 0,
                         "label": item.get("label") or "", "data": data}
    return latest


def _t_recover_file(root, args, caps=None):
    """从写后快照恢复丢失文件；只允许写回当前沙箱。"""
    from .sandbox import (SandboxError, _real, _recover_rel, resolve_path)
    from .undo import SNAP_AFTER_MAX
    sid = str((caps or {}).get("sid") or "")
    if not sid:
        return "需要会话 id 才能定位写后快照。", None
    token = str(args.get("token") or "").strip()
    rel = str(args.get("path") or "").strip()
    force = bool(args.get("force"))
    dry = bool(args.get("dry_run"))
    latest = _recover_candidates(sid, token or None)
    if not latest:
        return ("没有找到写后快照。先用 list_steps 查看本次会话的写操作；"
                "如果步骤已被清除，无法从这里恢复。"), None

    if rel:
        try:
            want = os.path.normcase(_real(resolve_path(root, rel)))
        except SandboxError as e:
            return "❌ 恢复路径不在沙箱内：%s" % e, None
        want_dir = want + os.sep
        picked = {}
        for p, info in latest.items():
            try:
                rp = os.path.normcase(_real(p))
            except OSError:
                continue
            # 文件精确匹配；目录按前缀匹配，便于一次恢复整个 webcraft/src
            if rp == want or rp.startswith(want_dir):
                picked[p] = info
        if not picked:
            return ("没有找到 %s 的写后快照。先用 list_steps 确认它是否被本会话写过。"
                    % rel), None
        latest = picked

    # 不带 path/token：只列清单，避免一次误恢复整棵树
    if not rel and not token:
        rows = []
        for p, info in sorted(latest.items(), key=lambda kv: -(kv[1].get("ts") or 0)):
            r = _recover_rel(root, p)
            if not r:
                continue
            rows.append("  %s  %d B  %s" % (
                r, len(info.get("data") or b""),
                time.strftime("%Y-%m-%d %H:%M:%S",
                              time.localtime(info.get("ts") or 0))))
            if len(rows) >= 40:
                rows.append("  …（只列最近 40 个）")
                break
        if not rows:
            return "写后快照都在沙箱外，无法恢复。", None
        return ("可恢复的写后快照（%d 个文件，最多列 40 个）：\n%s\n\n"
                "用 recover_file(path=...) 恢复单个文件；token=... 恢复某一步全部文件。"
                % (len(latest), "\n".join(rows))), None

    done, skipped, dry_rows = [], [], []
    for p, info in sorted(latest.items(), key=lambda kv: (kv[1].get("ts") or 0)):
        r = _recover_rel(root, p)
        if not r:
            skipped.append("%s（沙箱外）" % p)
            continue
        data = info.get("data")
        if isinstance(data, str):
            data = data.encode("utf-8")
        if not isinstance(data, (bytes, bytearray)):
            skipped.append("%s（快照内容损坏）" % r)
            continue
        if len(data) >= SNAP_AFTER_MAX:
            skipped.append("%s（快照达到 1MB 上限，可能被截断）" % r)
            continue
        target = resolve_path(root, r)
        if os.path.exists(target):
            try:
                with open(target, "rb") as f:
                    cur = f.read()
            except OSError as e:
                skipped.append("%s（读现有文件失败：%s）" % (r, e))
                continue
            if cur != data and not force:
                skipped.append("%s（已存在且不同，加 force=true）" % r)
                continue
        if dry:
            dry_rows.append(r)
            continue
        try:
            parent = os.path.dirname(target)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(target, "wb") as f:
                f.write(data)
            done.append(r)
        except OSError as e:
            skipped.append("%s（写入失败：%s）" % (r, e))

    if dry:
        return ("将恢复 %d 个文件：\n%s"
                % (len(dry_rows), "\n".join("  " + x for x in dry_rows))), None
    lines = []
    if done:
        lines.append("已从写后快照恢复 %d 个文件：\n%s"
                     % (len(done), "\n".join("  " + x for x in done)))
    if skipped:
        lines.append("跳过 %d 个：\n%s"
                     % (len(skipped), "\n".join("  " + x for x in skipped)))
    return ("\n\n".join(lines) if lines else "没有可恢复的文件。"), None


def missing_written_files(sid, root, limit=50):
    """本会话写过、但现在不在磁盘上的文件（工具调用之外消失）。"""
    from .sandbox import (_real, _recover_rel)
    from .state import _ensure_loaded
    from .undo import (_UNDO, _undo_lock)
    _ensure_loaded(sid)
    with _undo_lock:
        items = [v for v in _UNDO.values() if v.get("sid") == sid]
    items.sort(key=lambda v: v.get("ts") or 0, reverse=True)
    out, seen = [], set()
    for item in items:
        for op in item.get("ops") or []:
            if op.get("kind") != "restore" or not op.get("after_existed"):
                continue
            p = op.get("path")
            if not p:
                continue
            try:
                key = os.path.normcase(_real(p))
            except OSError:
                continue
            if key in seen:
                continue
            seen.add(key)
            rel = _recover_rel(root, p)
            if not rel:
                continue
            if not os.path.exists(p):
                out.append(rel)
                if len(out) >= limit:
                    return out
    return out


def _t_list_steps(root, args, caps=None):
    """列出本次会话的写操作步骤（步级时间线 / 撤销重做的入口）。"""
    from .sandbox import _rel
    from .state import _ensure_loaded
    from .undo import (MAX_UNDO, _UNDO, _undo_lock)
    sid = str((caps or {}).get("sid") or "")
    _ensure_loaded(sid)
    with _undo_lock:
        items = [(t, v) for t, v in _UNDO.items()
                 if sid and v.get("sid") == sid]
    if not items:
        return "本次会话还没有可撤销的写操作。", None
    items.sort(key=lambda kv: kv[1].get("ts") or 0)
    lines = ["本次会话的写操作步骤（%d 个，最多保留 %d）：" % (len(items), MAX_UNDO)]
    for i, (token, item) in enumerate(items, 1):
        files = []
        missing = []
        for op in item.get("ops") or []:
            p = op.get("path")
            if p:
                try:
                    files.append(_rel(root, p))
                except Exception:            # noqa: BLE001
                    pass
                if op.get("after_existed") and not os.path.exists(p):
                    missing.append(_rel(root, p))
        suffix = ""
        if missing:
            suffix = "  ⚠ 文件已丢失（用 recover_file 恢复）：%s" % "、".join(missing[:3])
        lines.append("  %d. %s  %-12s  %s  %s%s" % (
            i, token, item.get("label") or "写操作",
            "已撤销" if item.get("undone") else "生效中",
            "、".join(files[:5]) or "（无文件）", suffix))
    lines.append("\n撤销：undo_step(token)；重做：redo_step(token)；"
                 "文件丢失：recover_file(path=...)。")
    return "\n".join(lines), None


def _t_undo_step(root, args, caps=None):
    from .sandbox import _rel
    from .state import _ensure_loaded
    from .undo import (_UNDO, _undo_lock, undo)
    token = str(args.get("token") or "").strip()
    if not token:
        return "请提供 token（先用 list_steps 查看）", None
    sid = str((caps or {}).get("sid") or "")
    _ensure_loaded(sid)
    with _undo_lock:
        item = _UNDO.get(token)
    if item:
        missing = []
        for op in item.get("ops") or []:
            p = op.get("path")
            if p and op.get("after_existed") and not os.path.exists(p):
                missing.append(_rel(root, p))
        if missing:
            return ("⚠ 这一步涉及的文件已经不在磁盘上：%s。\n"
                    "undo_step 只能撤销、不能恢复写后内容；"
                    "请改用 recover_file(path=...) 从写后快照恢复。"
                    % "、".join(missing[:5])), None
    if undo(token):
        return "已撤销 %s。" % token, None
    return "撤销失败：%s 不存在、已经撤销过，或超出保留条数。" % token, None


def _t_redo_step(root, args, caps=None):
    from .undo import redo
    token = str(args.get("token") or "").strip()
    if not token:
        return "请提供 token（先用 list_steps 查看）", None
    if redo(token):
        return "已重做 %s。" % token, None
    return ("重做失败：%s 不存在，或它没有被撤销过。"
            "（删除 / 移动类操作没有写后快照，无法重做）" % token), None


def _t_write_file(root, args):
    from .sandbox import (_rel, resolve_path)
    from .undo import _snap
    p = resolve_path(root, args.get("path"))
    if os.path.isdir(p):
        return "目标是目录，不能写入：" + _rel(root, p), None
    content = args.get("content")
    if content is None:
        return "缺少 content 参数", None
    content = str(content)
    undo_op = _snap(p)
    parent = os.path.dirname(p)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(content)
    existed = undo_op.get("existed")
    tail = "\n".join(("%d | %s" % (i, s)) for i, s in
                     enumerate(content.splitlines()[:40], 1))
    return ("已%s %s（%d 行，%d 字节）\n%s" % (
        "覆盖" if existed else "创建", _rel(root, p),
        len(content.splitlines()), len(content.encode("utf-8")),
        tail + ("\n…（略）" if len(content.splitlines()) > 40 else ""))), [undo_op]


def _t_edit_file(root, args):
    from .io_utils import _read_text_file
    from .sandbox import (_rel, resolve_path)
    from .undo import _snap
    p = resolve_path(root, args.get("path"))
    if not os.path.isfile(p):
        return "文件不存在：" + _rel(root, p), None
    old = args.get("old")
    new = args.get("new")
    if old is None or new is None:
        return "缺少 old / new 参数", None
    old, new = str(old), str(new)
    text, enc, _size, _tr = _read_text_file(p, limit=0)
    cnt = text.count(old)
    if cnt == 0:
        return ("未找到要替换的内容（注意 old 必须是原文，且不含行号前缀）：\n"
                + old[:200]), None
    if cnt > 1:
        return ("old 在多处出现（%d 处），请给出更长的唯一片段" % cnt), None
    undo_op = _snap(p)
    write_enc = enc if enc in ("utf-8", "utf-8-sig", "gb18030") else "utf-8"
    with open(p, "w", encoding=write_enc, newline="") as f:
        f.write(text.replace(old, new, 1))
    add = len(new.splitlines())
    dele = len(old.splitlines())
    return ("已修改 %s（−%d 行 / +%d 行）" % (_rel(root, p), dele, add)), [undo_op]


def _t_make_dir(root, args):
    from .sandbox import (_rel, resolve_path)
    p = resolve_path(root, args.get("path"))
    if os.path.isdir(p):
        return "目录已存在：" + _rel(root, p), None
    if os.path.isfile(p):
        return "同名文件已存在：" + _rel(root, p), None
    try:
        os.makedirs(p, exist_ok=False)
    except OSError as e:
        return "创建目录失败：" + str(e), None
    return "已创建目录：" + _rel(root, p), [{"kind": "rmdir", "path": p}]


def _t_move_file(root, args):
    from .sandbox import (_rel, resolve_path)
    src = resolve_path(root, args.get("from"))
    dst = resolve_path(root, args.get("to"))
    if not os.path.exists(src):
        return "源不存在：" + _rel(root, src), None
    if os.path.exists(dst):
        return "目标已存在，拒绝覆盖：" + _rel(root, dst), None
    parent = os.path.dirname(dst)
    if parent:
        os.makedirs(parent, exist_ok=True)
    try:
        os.replace(src, dst)
    except OSError as e:
        return "移动失败：" + str(e), None
    return "已移动：%s → %s" % (_rel(root, src), _rel(root, dst)), \
        [{"kind": "move_back", "src": src, "dst": dst}]


def _t_delete_file(root, args):
    from .sandbox import (_real, _rel, resolve_path)
    from .undo import _snap
    p = resolve_path(root, args.get("path"))
    if p == _real(root):
        return "不能删除沙箱根目录", None
    if os.path.isdir(p):
        if os.listdir(p):
            return "目录非空，拒绝删除（请先逐个删除其中的文件）", None
        try:
            os.rmdir(p)
        except OSError as e:
            return "删除目录失败：" + str(e), None
        return "已删除空目录：" + _rel(root, p), [{"kind": "mkdir", "path": p}]
    if not os.path.isfile(p):
        return "文件不存在：" + _rel(root, p), None
    undo_op = _snap(p)
    try:
        os.remove(p)
    except OSError as e:
        return "删除失败：" + str(e), None
    return "已删除文件：" + _rel(root, p), [undo_op]


def _parse_page_ranges(spec, total, max_pages=20):
    """解析 pages="1-5,7" / "3" 这类范围；1-based，最多 max_pages 页。"""
    out = []
    for part in str(spec or "").replace("，", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if "-" in part:
                a, b = part.split("-", 1)
                lo, hi = int(a), int(b)
            else:
                lo = hi = int(part)
        except (TypeError, ValueError):
            continue
        if lo > hi:
            lo, hi = hi, lo
        for n in range(max(1, lo), min(int(total), hi) + 1):
            if n not in out:
                out.append(n)
            if len(out) >= max_pages:
                return out
    return out


def _t_attachment_read(root, args, caps=None):
    """回看本会话里已留存的附件（图片 / 文档）。

    对话附件不发路径：只按会话登记的 id / 文件名取，避免借这个工具把 data/ 变成
    任意路径入口。图片重新走视觉链路；文档返回发送时抽取的正文，可分段读。
    """
    from .io_utils import IMG_MAX_BYTES
    from app import attachments, imaging
    caps = caps if isinstance(caps, dict) else {}
    sid = str(caps.get("sid") or "")
    if not sid:
        return "❌ attachment_read 只能用在对话会话里（当前没有会话上下文）。", None
    args = args if isinstance(args, dict) else {}
    key = str(args.get("id") or args.get("name") or args.get("ref") or "").strip()

    if not key:
        items = attachments.list_for_session(sid)
        if not items:
            return "本会话还没有可回看的附件（用户还没发过图片或文档）。", None
        lines = ["本会话已留存的附件（回看时把 id 传给我）："]
        for it in items[:30]:
            tag = "图片" if it.get("kind") == attachments.IMAGE else "文档"
            more = ""
            if it.get("kind") == attachments.DOC:
                n = len(it.get("text") or "")
                more = ("，正文 %d 字" % n) if n else "（无正文）"
            lines.append("- %s｜%s｜%s%s" % (it["id"], tag, it["name"], more))
        if len(items) > 30:
            lines.append("…（共 %d 个，只列前 30 个；可用 name 精确指定）" % len(items))
        return "\n".join(lines), None

    item = attachments.find(sid, key)
    if not item:
        return ("❌ 本会话里没有这个附件：%s。不带参数调用 attachment_read 可列出全部。"
                % key), None

    if item["kind"] == attachments.DOC:
        pages_spec = str(args.get("pages") or "").strip()
        if pages_spec:
            page_texts = item.get("pages") or []
            if not page_texts:
                return ("该附件没有页级数据（不是 PDF，或旧会话没存页）。"
                        "请改用 offset / limit 分段读。"), None
            nums = _parse_page_ranges(pages_spec, len(page_texts), 20)
            if not nums:
                return ("页码范围无效：本附件共 %d 页，示例 pages=\"1-5\"（每次最多 20 页）。"
                        % len(page_texts)), None
            parts = []
            for n in nums:
                t = str(page_texts[n - 1] or "").strip()
                parts.append("【第 %d 页】\n%s"
                             % (n, t or "（本页没有可提取的文本）"))
            head = "【附件 %s｜%s｜共 %d 页】" % (
                item["id"], item["name"], len(page_texts))
            if len(nums) < len(page_texts):
                head += "（本次第 %d–%d 页）" % (nums[0], nums[-1])
            return head + "\n\n" + "\n\n".join(parts), None
        text = item.get("text") or ""
        if not text:
            return ("❌ 附件「%s」没有留存正文（发送时解析失败：%s）。"
                    "请让用户重新发送这个文件。" % (item["name"],
                                                item.get("error") or "未知原因")), None
        try:
            off = max(0, int(args.get("offset") or 0))
        except (TypeError, ValueError):
            off = 0
        try:
            lim = int(args.get("limit") or 6000)
        except (TypeError, ValueError):
            lim = 6000
        lim = max(500, min(20000, lim))
        part = text[off:off + lim]
        head = "【附件 %s｜%s｜全文 %d 字】" % (item["id"], item["name"], len(text))
        if off or len(part) < len(text):
            head += "（本次 %d–%d 字）" % (off + 1, off + len(part))
        tail = ""
        if off + len(part) < len(text):
            tail = "\n…（还有 %d 字；用 offset=%d 继续读）" % (
                len(text) - off - len(part), off + len(part))
        return head + "\n" + part + tail, None

    raw = attachments.read_bytes(sid, item)
    if not raw:
        return "❌ 附件文件已不存在：%s" % item["name"], None
    if len(raw) > IMG_MAX_BYTES:
        return ("❌ 图片太大（%.1f MB，单张上限 %d MB）。"
                % (len(raw) / 1048576.0, IMG_MAX_BYTES // 1048576)), None
    b64 = base64.b64encode(raw).decode("ascii")
    q = str(args.get("question") or "").strip()
    meta = {}
    note = imaging.recognize_image_fallback(item["mime"] or "image/png", b64,
                                            question=q, meta=meta)
    if meta.get("fallback") and not str(note).startswith(("❌", "⚠️")):
        note += "\n\n（识图通道：主聊天模型未接受图片，已回落到视觉 API）"
    return "【附件 %s｜%s】\n%s" % (item["id"], item["name"], note), None


def _t_read_tool_result(root, args, allow_outside=False):
    """句柄只在主对话的请求内有效；这里给明确提示，避免模型反复重试。"""
    return ("❌ read_tool_result 只能在主对话里使用："
            "工具结果句柄按请求隔离，其他场景请直接重新读一次文件。"), None


# ---- Computer Use：五个入口都转发到 app/computer，沙箱根用于 file:// 校验 ----


def _t_copy_file(root, args):
    from .sandbox import (_rel, resolve_path)
    src = resolve_path(root, (args or {}).get("from"))
    dst = resolve_path(root, (args or {}).get("to"))
    if not os.path.isfile(src):
        return "❌ 源文件不存在：%s" % _rel(root, src), None
    if os.path.isdir(dst):
        dst = os.path.join(dst, os.path.basename(src))
    if os.path.exists(dst):
        _ow = str((args or {}).get("overwrite") or "").strip().lower()
        if _ow not in ("1", "true", "yes", "on"):
            return ("❌ 目标已存在：%s；需要覆盖请传 overwrite=true"
                    % _rel(root, dst)), None
        if os.path.isdir(dst):
            return "❌ 目标是目录：%s" % _rel(root, dst), None
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)
    return ("已复制：%s → %s（%d 字节）"
            % (_rel(root, src), _rel(root, dst), os.path.getsize(dst))), None


def _t_sleep(root, args, stop_event=None):
    from .base import SLEEP_MAX
    try:
        sec = float((args or {}).get("seconds") or 0)
    except (TypeError, ValueError):
        return "❌ seconds 必须是数字", None
    sec = max(0.5, min(sec, SLEEP_MAX))
    end = time.time() + sec
    while time.time() < end:
        if stop_event is not None and stop_event.is_set():
            return "⏹ 已停止等待", None
        time.sleep(min(0.2, max(0.05, end - time.time())))
    return "已等待 %.1f 秒" % sec, None
