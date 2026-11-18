# -*- coding: utf-8 -*-
"""压缩 sessions.json：去掉重复 / 二进制的附件正文。

用法：
    python scripts/compact_sessions.py            # 只读，打印能省多少
    python scripts/compact_sessions.py --apply    # 先备份，再写回（请先退出应用）
    python scripts/compact_sessions.py --file D:\\tmp\\sessions.json --apply

只处理附件正文，不改消息顺序、角色、文本内容和附件元信息。
"""
import argparse
import json
import os
import shutil
import socket
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app import store  # noqa: E402
from app.session_compact import compact_data  # noqa: E402


def _server_port():
    path = os.path.join(store.DATA_DIR, "server.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return int((json.load(f) or {}).get("port") or 0)
    except (OSError, ValueError, TypeError):
        return 0


def _app_running():
    port = _server_port()
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


def _top_messages(data, limit=5):
    rows = []
    for s in data.get("sessions") or []:
        for i, m in enumerate(s.get("messages") or []):
            try:
                n = len(json.dumps(m, ensure_ascii=False))
            except (TypeError, ValueError):
                continue
            rows.append((n, s.get("id") or "", s.get("title") or "", i, m.get("role") or ""))
    rows.sort(reverse=True)
    return rows[:limit]


def main():
    ap = argparse.ArgumentParser(description="压缩 sessions.json 里的附件正文")
    ap.add_argument("--file", default=store.SESSION_FILE, help="sessions.json 路径")
    ap.add_argument("--apply", action="store_true", help="写回（默认只读）")
    ap.add_argument("--force", action="store_true", help="应用运行中也强制写回（不推荐）")
    args = ap.parse_args()

    path = os.path.abspath(args.file)
    if not os.path.isfile(path):
        print("找不到文件：%s" % path)
        return 2
    running = _app_running()
    if running:
        print("检测到应用正在运行。写回请先退出应用，否则内存里的旧数据会覆盖结果。")
    if args.apply and running and not args.force:
        print("已停止：请退出应用后重试，或确认风险后加 --force。")
        return 2

    before = os.path.getsize(path)
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or not isinstance(data.get("sessions"), list):
        print("不是有效的 sessions.json（缺少 sessions 数组）")
        return 2

    stats = compact_data(data)
    after = len(json.dumps(data, ensure_ascii=False).encode("utf-8"))
    print("扫描完成：%d 个会话 / %d 条消息需要压缩" % (stats["sessions"], stats["messages"]))
    print("释放正文：约 %s（按 UTF-8 估算）" % _mb(stats["freed_chars"]))
    print("文件体积：%s → %s（预计节省 %s）" % (_mb(before), _mb(after), _mb(max(0, before - after))))
    if stats["notes"]:
        print("处理项：")
        for note in stats["notes"]:
            print("  · " + note)

    top = _top_messages(data)
    if top:
        print("压缩后仍然最大的消息：")
        for n, sid, title, idx, role in top:
            print("  · %s  %s#%d %s  %s" % (_mb(n), sid, idx, role, title[:30]))

    if not args.apply:
        print("\n这是只读预览。确认无误后：先退出应用，再加 --apply 执行。")
        return 0

    bak = "%s.bak-%s" % (path, time.strftime("%Y%m%d-%H%M%S"))
    try:
        shutil.copy2(path, bak)
        print("已备份：%s（%s）" % (bak, _mb(os.path.getsize(bak))))
    except OSError as e:
        print("备份失败，已停止：%s" % e)
        return 2

    data["saved_at"] = time.time()
    store.write_json(path, data, indent=None, remember=False)
    print("已写回：%s（%s）" % (path, _mb(os.path.getsize(path))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
