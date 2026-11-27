# -*- coding: utf-8 -*-
"""清理会话事件日志：把已被快照覆盖的历史事件压成一条基线。

用法（请先退出应用）：
    python scripts/prune_session_events.py              # 只读，打印能回收多少
    python scripts/prune_session_events.py --apply      # 先备份，再写回

为什么安全：清理后每个会话保留一条 `session/baseline`（内含完整快照）+ 之后的
增量事件，`session_events.rebuild()` 仍能还原出完整会话。
"""
import argparse
import json
import os
import socket
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app import session_event_prune as sep  # noqa: E402
from app import store  # noqa: E402


def _server_port(data_dir):
    path = os.path.join(data_dir, "server.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return int((json.load(f) or {}).get("port") or 0)
    except (OSError, ValueError, TypeError):
        return 0


def _app_running(data_dir):
    port = _server_port(data_dir)
    if not port:
        return False
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=0.5)
        s.close()
        return True
    except OSError:
        return False


def _mb(n):
    return "%.1f MB" % (n / 1024.0 / 1024.0)


def _index_saved_at(data_dir):
    """新版存储的「最后落盘时间」：index.json 里的 saved_at。"""
    try:
        with open(os.path.join(data_dir, "sessions", "index.json"), "r", encoding="utf-8") as f:
            return float((json.load(f) or {}).get("saved_at") or 0)
    except (OSError, ValueError, TypeError):
        return 0.0


def _load_sessions(data_dir):
    """会话内容：老版本读 sessions.json；新版走 sessions.load_sessions()。"""
    path = os.path.abspath(store.SESSION_FILE)
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f), path
    from app import sessions as sessions_mod
    data = sessions_mod.load_sessions()
    data["saved_at"] = _index_saved_at(data_dir)
    return data, ""


def main():
    ap = argparse.ArgumentParser(description="清理会话事件日志（默认只读预览）")
    ap.add_argument("--data-dir", default="", help="数据目录（默认取 sessions.json 所在目录）")
    ap.add_argument("--apply", action="store_true", help="备份后写回（默认只读）")
    ap.add_argument("--force", action="store_true", help="应用运行中也强制写回（不推荐）")
    args = ap.parse_args()

    data_dir = (os.path.abspath(args.data_dir) if args.data_dir
                else os.path.dirname(os.path.abspath(store.SESSION_FILE)))
    running = _app_running(data_dir)
    if running:
        print("检测到应用正在运行。写回请先退出应用，否则内存里的事件基线会与清理后的文件对不上。")
    if args.apply and running and not args.force:
        print("已停止：请退出应用后重试，或确认风险后加 --force。")
        return 2

    data, legacy_path = _load_sessions(data_dir)
    if not isinstance(data, dict) or not isinstance(data.get("sessions"), list):
        print("读不到有效的会话列表（%s）" % (legacy_path or "事件存储"))
        return 2

    todo, skipped, stats = sep.plan(data, data_dir)
    orphans, keep = sep.scan_orphans(data, data_dir)
    orphan_bytes = sum(r["before"] for r in orphans)
    print("扫描完成：%d 个会话，%d 个可清理（另有 %d 个跳过）"
          % (stats["sessions"], stats["candidates"], stats["skipped"]))
    if todo:
        print("事件日志体积：%s → %s（可回收 %s，共 %d 条历史事件，保留 %d 条增量）"
              % (_mb(stats["before"]), _mb(stats["after"]),
                 _mb(max(0, stats["before"] - stats["after"])),
                 stats["events"], stats["tail"]))
        print("体积最大的会话：")
        for row in todo[:10]:
            print("  · %-28s %8s → %7s  事件 %d 条  %s"
                  % (row["sid"], _mb(row["before"]), _mb(row["after"]),
                     row["events"], (row["title"] or "")[:24]))
    if orphans:
        print("已删除会话的残留目录：%d 个，%s（最后事件早于最后一次落盘）"
              % (len(orphans), _mb(orphan_bytes)))
        for row in orphans[:10]:
            print("  · %-28s %8s" % (row["sid"], _mb(row["before"])))
    if keep:
        print("保留的残留目录：%d 个（事件比最后一次落盘新，可能是内存里未落库的会话）"
              % len(keep))
    for row in skipped[:5]:
        print("  ! 跳过 %s：%s" % (row["sid"], row["reason"]))
    if not args.apply:
        print("\n这是只读预览。确认无误后：先退出应用，再加 --apply 执行。")
        return 0
    if not todo and not orphans:
        print("没有需要清理的内容。")
        return 0

    backup_dir = os.path.join(data_dir, "event_prune_backup", time.strftime("%Y%m%d-%H%M%S"))
    done = 0
    for row in todo:
        try:
            if sep.apply_row(row, backup_dir):
                done += 1
        except OSError as e:
            print("  ! 写回失败 %s：%s" % (row["sid"], e))
    moved = 0
    for row in orphans:
        try:
            if sep.apply_orphan(row, backup_dir):
                moved += 1
        except OSError as e:
            print("  ! 移动失败 %s：%s" % (row["sid"], e))
    print("\n已清理 %d/%d 个会话，移除 %d/%d 个残留目录，原文件备份在：%s"
          % (done, len(todo), moved, len(orphans), backup_dir))
    return 0 if (done == len(todo) and moved == len(orphans)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
