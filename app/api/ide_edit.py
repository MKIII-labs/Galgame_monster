# -*- coding: utf-8 -*-
"""IDE 全局替换（跨文件）。

搜索走 /api/sandbox/search（只读）；这里补「替换」：

- `dry_run=true`（默认）只算方案：每个文件改几处 + 前后对照，不写盘
- `dry_run=false` 才写：写前用 `agent._snap` 留快照，写后登记 undo，
  可以在「AI 变更」里撤销（和 agent 的写操作共用一套撤销栈）
- 只处理 UTF-8 文本：非 UTF-8 / 二进制 / 超大文件一律跳过并计数，
  宁可少改，也不把编码写坏
- 密钥文件按规则过滤（与搜索、读取保持一致）

上限：一次最多 200 个文件 / 2000 处，避免一条正则把整个仓库刷掉。
"""
import os
import re

from app import agent
from app.registry import route

MAX_FILES = 200
MAX_MATCHES = 2000
MAX_BYTES = 2 * 1024 * 1024
PREVIEW_PER_FILE = 3


def _rx_of(query, regex, case):
    flags = 0 if case else re.I
    return re.compile(query if regex else re.escape(query), flags)


def _preview(text, rx, replacement, limit=PREVIEW_PER_FILE):
    """每个文件给前几处「改前 / 改后」，按行去重；只做展示。"""
    out, seen = [], set()
    for m in rx.finditer(text):
        line_no = text.count("\n", 0, m.start()) + 1
        if line_no in seen:
            continue
        seen.add(line_no)
        start = text.rfind("\n", 0, m.start()) + 1
        end = text.find("\n", m.end())
        if end < 0:
            end = len(text)
        before = text[start:end]
        after = rx.sub(lambda _m: replacement, before)
        out.append({"line": line_no, "before": before.strip()[:200],
                    "after": after.strip()[:200]})
        if len(out) >= limit:
            break
    return out


def _rollback(undo_ops):
    """写失败：把已经写过的文件还原（只处理文件快照）。"""
    for op in reversed(undo_ops or []):
        if op.get("kind") != "restore":
            continue
        try:
            if op.get("existed"):
                with open(op["path"], "wb") as f:
                    f.write(op.get("data") or b"")
            elif os.path.exists(op["path"]):
                os.remove(op["path"])
        except OSError:
            pass


@route("/api/ide/replace")
def _api_ide_replace(h):
    body = h._body()
    s, sb, err = h._sb_guard(body)
    if err:
        return err
    query = str(body.get("query") or "")
    if not query:
        return h._json({"ok": False, "error": "请输入要查找的内容"}, 400)
    # 默认区分大小写（与搜索一致）；勾「正则」时才按正则解释
    try:
        rx = _rx_of(query, bool(body.get("regex")), body.get("case", True) is not False)
    except re.error as e:
        return h._json({"ok": False, "error": "正则写错了：%s" % e}, 400)
    replacement = str(body.get("replacement") or "")
    globpat = str(body.get("glob") or "").strip()
    glob_rx = agent._glob_to_regex(globpat) if globpat else None
    dry = body.get("dry_run", True) is not False

    rules, rerr = agent.load_effective_rules(sb)
    hits = []            # (abs, rel, new_text, count, preview)
    skipped = 0
    total = 0
    for dirpath, _dirs, filenames in agent._walk(sb):
        if len(hits) >= MAX_FILES or total >= MAX_MATCHES:
            break
        for fn in filenames:
            if len(hits) >= MAX_FILES or total >= MAX_MATCHES:
                break
            full = os.path.join(dirpath, fn)
            rp = agent._rel(sb, full)
            if glob_rx and not (glob_rx.match(rp) or glob_rx.match(fn)):
                continue
            # 密钥文件不参与：否则「搜索 + 替换」能把密钥摸出来
            if agent.is_secret_path(rp) and not (
                    rerr == "" and agent._match_rules(rules, [rp])[0] == "allow"):
                continue
            try:
                if os.path.getsize(full) > MAX_BYTES:
                    skipped += 1
                    continue
                with open(full, "rb") as f:
                    raw = f.read()
            except OSError:
                continue
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                skipped += 1
                continue
            if "\x00" in text:
                skipped += 1
                continue
            new, n = rx.subn(lambda _m: replacement, text)
            if not n:
                continue
            total += n
            hits.append((full, rp, new, n, _preview(text, rx, replacement)))

    plan = [{"path": rp, "count": n, "preview": pv}
            for _full, rp, _new, n, pv in hits]
    truncated = len(hits) >= MAX_FILES or total >= MAX_MATCHES
    if dry:
        return h._json({"ok": True, "dry_run": True, "total": total,
                        "files": plan, "skipped": skipped, "truncated": truncated})

    undo_ops, written = [], []
    try:
        with agent.WRITE_LOCK:          # 和 agent / 子代理的写操作互斥
            for full, rp, new, n, _pv in hits:
                undo_ops.append(agent._snap(full))
                with open(full, "w", encoding="utf-8", newline="") as f:
                    f.write(new)
                written.append({"path": rp, "count": n})
    except OSError as e:
        _rollback(undo_ops)
        return h._json({"ok": False, "error": "写入失败：%s" % e}, 400)
    agent.invalidate_tree(sb)
    token = agent.register_undo((s or {}).get("id") or "", undo_ops, "全局替换")
    return h._json({"ok": True, "total": total, "files": written, "token": token,
                    "skipped": skipped, "truncated": truncated})
