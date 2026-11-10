# -*- coding: utf-8 -*-

import os
import re
import shutil
import time
from app.store import DATA_DIR



class SandboxError(Exception):
    """沙箱路径校验失败"""
    pass


# ============================================================
# 0. 流式摘除与调用解析
# ============================================================


def _real(path):
    return os.path.realpath(os.path.abspath(path))


def _norm_rel(rel):
    return (rel or "").strip().replace("\\", "/")


def clean_cwd(raw):
    """清洗模型给的 cwd 伪影（多余引号 / 尾随 ] } , ;），再交给沙箱解析。"""
    s = str(raw or ".").strip()
    for _ in range(2):                      # 处理 ."] 这种「标点 + 引号」交替结尾
        s = s.strip().strip('"\'`').rstrip(" ]},;")
    return s or "."


def sandbox_warnings(root):
    """沙箱里已有 Git 仓库且工作树不干净时，给模型一条通用提醒。"""
    try:
        root = clean_sandbox(root)
    except Exception:
        return []
    if not root or not os.path.exists(os.path.join(root, ".git")):
        return []
    try:
        from app import git as gitlib
        st = gitlib.status(root)
    except Exception:
        return ["沙箱里有 .git 目录，但无法读取 Git 状态；"
                "如果任务要求从零开始，请确认是否换一个空目录。"]
    files = st.get("files") or []
    if not files:
        return []
    deleted = sum(1 for f in files if str(f.get("status") or "") == "D")
    untracked = sum(1 for f in files if f.get("untracked"))
    changed = max(0, len(files) - deleted - untracked)
    return ["沙箱里已有 Git 仓库（分支 %s，%d 个改动 / %d 个删除 / %d 个未跟踪）。"
            "如果任务要求从零开始，请确认是否换空目录，或先核对现状再动手。"
            % (st.get("branch") or "?", changed, deleted, untracked)]


def clean_sandbox(raw):
    """把沙箱字符串收敛成一个真实存在的目录路径。

    历史遗留或某些软件注入 stdout，会让沙箱值变成「路径 + 一堆日志」的多行文本；
    这里逐行尝试取第一个真实存在的目录；都不存在时返回首行，供报错展示。
    """
    s = str(raw or "").strip().strip('"').strip("'")
    if not s:
        return ""
    lines = [ln.strip().strip('"').strip("'") for ln in s.splitlines()]
    for ln in lines:
        if ln and os.path.isdir(_real(ln)):
            return ln
    return next((ln for ln in lines if ln), s)


# 读类工具额外放行的目录：编辑器（Trae / VS Code）本来就能直接读这些目录，
# 这里也放开「读」，但写入 / 删除仍然禁止。`.git`（可能含带令牌的 remote）与 data/ 不放开。


def _assert_not_blocked(target, for_read=False):
    """绝对路径层面的硬性拦截：禁止目录段 + 本项目 data/"""
    from .groups import (BLOCKED_SEGMENTS, READ_ONLY_SEGMENTS)
    for seg in str(target).replace("\\", "/").split("/"):
        if seg in BLOCKED_SEGMENTS and not (for_read and seg in READ_ONLY_SEGMENTS):
            raise SandboxError("该目录被禁止访问：" + seg)
    data = _real(DATA_DIR)
    if target == data or target.startswith(data + os.sep):
        raise SandboxError("data 目录被禁止访问（含密钥等运行数据）")


def resolve_path(sandbox, rel, allow_outside=False, for_read=False):
    """把沙箱内路径解析成绝对路径。

    只接受**沙箱内**的路径：相对路径（推荐）与沙箱内的绝对路径都行（模型常直接引用
    提示里给出的沙箱绝对路径）；越界、命中禁止目录、或落在本项目 data/ 下时抛 SandboxError。

    allow_outside=True（用户已明确授权「读取沙箱外文件」）时：**绝对路径**可越过沙箱边界，
    但仍禁止 BLOCKED_SEGMENTS 与本项目 data/；相对路径依旧只在沙箱内解析。
    for_read=True（读类工具）：node_modules / .venv 等目录可读，写入类工具仍按 BLOCKED_SEGMENTS 拒绝。
    """
    from .groups import (BLOCKED_SEGMENTS, READ_ONLY_SEGMENTS)
    sandbox = clean_sandbox(sandbox)
    root = _real(sandbox or "")
    if not sandbox or not os.path.isdir(root):
        raise SandboxError("沙箱目录不存在或未设置：" + (sandbox or "（空）"))
    raw = str(rel or "").strip().strip('"').strip("'")
    if raw.lower().startswith("file://"):
        raw = raw[7:]
    if os.path.isabs(raw) or (len(raw) > 1 and raw[1] == ":"):
        target = _real(raw)                      # 绝对路径
        if target != root and not target.startswith(root + os.sep):
            if not allow_outside:
                raise SandboxError("路径越出沙箱范围：" + raw)
            _assert_not_blocked(target, for_read)   # 沙箱外：仅放行「读」，硬性拦截不变
            return target
    else:
        norm = _norm_rel(raw)
        base = os.path.basename(root)
        if base and (norm == base or norm.startswith(base + "/")):
            norm = norm[len(base):].lstrip("/")   # 容忍模型带上沙箱目录名
        target = _real(os.path.join(root, norm))
        if target != root and not target.startswith(root + os.sep):
            if allow_outside:
                raise SandboxError(
                    "路径越出沙箱范围：%s（沙箱外读取需要用绝对路径，"
                    "相对路径（含 ../）只在沙箱内解析）" % raw)
            raise SandboxError("路径越出沙箱范围：" + raw)
    parts = os.path.relpath(target, root).replace("\\", "/").split("/")
    for seg in parts:
        if seg in BLOCKED_SEGMENTS and not (for_read and seg in READ_ONLY_SEGMENTS):
            raise SandboxError("该目录被禁止访问：" + seg)
    data = _real(DATA_DIR)
    if target == data or target.startswith(data + os.sep):
        raise SandboxError("data 目录被禁止访问（含密钥等运行数据）")
    return target


def _rel(root, path):
    try:
        return os.path.relpath(path, _real(root)).replace("\\", "/")
    except ValueError:
        return path


def _walk(root):
    """遍历沙箱，跳过禁止目录与本项目 data 目录"""
    from .groups import BLOCKED_SEGMENTS
    data = _real(DATA_DIR)
    for dirpath, dirnames, filenames in os.walk(_real(root)):
        here = _real(dirpath)
        if here == data or here.startswith(data + os.sep):
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if d not in BLOCKED_SEGMENTS]
        yield dirpath, dirnames, filenames


def _recover_rel(root, path):
    """写后快照里的绝对路径 → 沙箱相对路径；沙箱外返回空串。"""
    try:
        rel = os.path.relpath(_real(path), _real(root))
    except (OSError, ValueError):
        return ""
    if rel == ".." or rel.startswith(".." + os.sep):
        return ""
    return rel.replace("\\", "/")


def _write_rel_paths(sandbox, name, args):
    """写入类工具会碰到的沙箱内相对路径（给规则匹配用）；非法路径直接忽略。"""
    if name == "move_file":
        keys = ("from", "to")
    elif name == "archify":
        keys = ("output", "candidate_path")
    else:
        keys = ("path",)
    root = _real(clean_sandbox(sandbox))
    out = []
    for key in keys:
        raw = (args or {}).get(key)
        if not raw:
            continue
        try:
            p = resolve_path(sandbox, raw)
        except SandboxError:
            continue
        out.append(os.path.relpath(p, root).replace("\\", "/"))
    return out


def _tree(sandbox, max_lines=60):
    """生成浅层目录树（最多两层），让模型知道沙箱里大致有什么"""
    lines = []
    root = _real(sandbox)
    for dirpath, dirnames, filenames in _walk(root):
        depth = os.path.relpath(dirpath, root).replace("\\", "/").count("/")
        if os.path.relpath(dirpath, root) == ".":
            depth = -1
        if depth >= 2:
            dirnames[:] = []
            continue
        prefix = "  " * (depth + 1)
        rel = _rel(root, dirpath)
        if rel != ".":
            lines.append(prefix + rel + "/")
        for fn in sorted(filenames)[:30]:
            lines.append(prefix + ("  " if rel != "." else "") + fn)
            if len(lines) >= max_lines:
                lines.append("…（目录树已截断）")
                return "\n".join(lines)
    return "\n".join(lines) or "（沙箱为空）"


# 浏览器操作（Computer Use）：独立于文件 Agent，只要设置里开启就注入。


def _looks_like_path(s):
    from .base import _PATH_EXT
    s = str(s or "").strip().strip('"\'')
    if not s or len(s) > 260:
        return False
    if "://" in s or s.startswith(("http://", "https://")):
        return False
    if re.search(r"[\\/]", s):
        return True
    return bool(re.search(r"\.(?:" + _PATH_EXT + r")$", s, re.I))


def _extract_paths(text, limit=4):
    from .base import _PATH_RE
    out = []
    for m in _PATH_RE.finditer(str(text or "")):
        val = next((g for g in m.groups() if g), "")
        val = val.strip().strip(".,;，。；:：")
        if _looks_like_path(val) and val not in out:
            out.append(val)
        if len(out) >= limit:
            break
    return out


def _extract_path(text):
    paths = _extract_paths(text, 1)
    return paths[0] if paths else ""


def _extract_dir(text):
    m = re.search(r'(?:看看|查看|列出|列一下|打开|ls|list|dir)\s+["\']?'
                  r'([A-Za-z0-9_./\\\u4e00-\u9fff-]{1,80})', str(text or ""), re.I)
    if m:
        cand = m.group(1).strip().strip(".,;，。；:：")
        stop = ("一下", "这个", "那个", "目录", "文件夹", "文件", "项目",
                "里面", "里", "内容", "有什么", "当前", "所有", "全部")
        if (cand in stop or cand.endswith(("文件", "目录", "文件夹", "项目"))
                or "这个" in cand or "那个" in cand):
            return "."
        return cand
    if re.search(r"(目录|文件夹|项目|沙箱)", str(text or "")):
        return "."
    return ""


def _extract_pattern(text):
    s = str(text or "")
    m = re.search(r'(?:grep|搜索|查找|找一下|search)\s*(?:一下)?\s*'
                  r'["\']([^"\']{1,80})["\']', s, re.I)
    if m:
        return m.group(1).strip()
    m = re.search(r'(?:有没有|是否包含|包含|里有|中的|里的|内容为|匹配)\s*'
                  r'["\']?([^\n，。；;]{1,60})', s, re.I)
    if m:
        return m.group(1).strip()
    m = re.search(r'(?:grep|搜索|查找|找一下|search)\s*(?:一下)?\s*'
                  r'([^\s，。；;]{1,60})', s, re.I)
    if m:
        return m.group(1).strip()
    return ""


def _sandbox_root(sandbox):
    root = _real(clean_sandbox(sandbox))
    if not sandbox or not os.path.isdir(root):
        raise SandboxError("沙箱目录不存在或未设置：" + (clean_sandbox(sandbox) or "（空）"))
    return root


TREE_TTL = 3.0                 # 目录树缓存（秒）：既要避免每轮重扫，也要尽快反映改动


def invalidate_tree(root=None):
    """写 / 删 / 移动之后清掉目录树缓存（root=None 时全清）。"""
    from .base import (_TREE_CACHE, _TREE_LOCK)
    with _TREE_LOCK:
        if not root:
            _TREE_CACHE.clear()
            return
        target = _real(root)
        for key in list(_TREE_CACHE):
            if key[0] == target:
                _TREE_CACHE.pop(key, None)


def _build_tree(root, max_entries, max_depth):
    from .groups import BLOCKED_SEGMENTS
    data_dir = os.path.normcase(_real(DATA_DIR))
    count = [0]

    def walk(dirpath, depth):
        try:
            names = sorted(os.listdir(dirpath))
        except OSError:
            return []
        dirs, files = [], []
        for name in names:
            if name in BLOCKED_SEGMENTS or count[0] >= max_entries:
                continue
            full = os.path.join(dirpath, name)
            # 不跟符号链接：既能防止越界，也省掉逐条 realpath（原来 963 个节点要 2.3s）
            if os.path.islink(full):
                continue
            if os.path.normcase(os.path.abspath(full)) == data_dir:
                continue
            count[0] += 1
            (dirs if os.path.isdir(full) else files).append(name)
        out = []
        for name in dirs:
            node = {"name": name, "dir": True, "children": []}
            if depth < max_depth:
                node["children"] = walk(os.path.join(dirpath, name), depth + 1)
            out.append(node)
        for name in files:
            try:
                size = os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                size = 0
            out.append({"name": name, "dir": False, "size": size})
        return out

    return {"root": root, "children": walk(root, 0),
            "truncated": count[0] >= max_entries}


def tree(sandbox, max_entries=2000, max_depth=6):
    """沙箱目录树（嵌套结构），跳过禁止目录与本项目 data/。

    带 3 秒缓存：前端启动、每轮 Agent 提示词、工具调用后刷新都会读它，
    而一次完整遍历在 Windows 上可能要 2 秒以上。
    """
    from .base import (_TREE_CACHE, _TREE_LOCK)
    root = _sandbox_root(sandbox)
    key = (root, int(max_entries), int(max_depth))
    now = time.time()
    with _TREE_LOCK:
        hit = _TREE_CACHE.get(key)
    if hit and (now - hit[0]) < TREE_TTL:
        return hit[1]
    data = _build_tree(root, max_entries, max_depth)
    with _TREE_LOCK:
        _TREE_CACHE[key] = (now, data)
        while len(_TREE_CACHE) > 8:
            _TREE_CACHE.pop(next(iter(_TREE_CACHE)))
    return data


# 可在 IDE 里预览的图片类型 → MIME


def ensure_entry(sandbox, rel, is_dir=False):
    """新建空文件或空目录（已存在则报错）"""
    p = resolve_path(sandbox, rel)
    if os.path.exists(p):
        raise SandboxError("已存在：" + str(rel))
    if is_dir:
        os.makedirs(p, exist_ok=False)
    else:
        parent = os.path.dirname(p)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(p, "w", encoding="utf-8"):
            pass
    return True


def delete_entry(sandbox, rel, recursive=False):
    """删除文件或目录；recursive=True 时连同目录内容一起删（IDE 右键删除用）"""
    p = resolve_path(sandbox, rel)
    if p == _sandbox_root(sandbox):
        raise SandboxError("不能删除沙箱根目录")
    if os.path.isdir(p):
        if recursive:
            shutil.rmtree(p)
            return True
        if os.listdir(p):
            raise SandboxError("目录非空，请先删除里面的内容")
        os.rmdir(p)
        return True
    if not os.path.isfile(p):
        raise SandboxError("不存在：" + str(rel))
    os.remove(p)
    return True
