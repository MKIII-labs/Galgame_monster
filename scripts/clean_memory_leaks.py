# -*- coding: utf-8 -*-
"""清理会话里残留的 <memory> 控制块。

用法：
    python scripts/clean_memory_leaks.py            # 只读：打印会改多少条
    python scripts/clean_memory_leaks.py --apply    # 先备份，再写回（请先退出应用）
    python scripts/clean_memory_leaks.py --file D:\\tmp\\sessions.json --apply

默认同时扫描 data/sessions.json 与 data/session_store/*.json（应用热路径的单会话快照）；
给了 --file 就只处理那一个文件。只删「块内像 ops JSON」的 <memory>…</memory>
（含未闭合的 JSON 尾巴），正常正文、代码围栏、消息顺序与其它字段都不动。
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

from app import sessions as sm  # noqa: E402
from app import store  # noqa: E402
from app.memory_leak import clean_sessions_data  # noqa: E402


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


def _targets(explicit):
    if explicit:
        return [os.path.abspath(explicit)]
    out = [store.SESSION_FILE]
    store_dir = os.path.join(os.path.dirname(store.SESSION_FILE), sm.STORE_DIRNAME)
    try:
        for name in sorted(os.listdir(store_dir)):
            if name.endswith(".json"):
                out.append(os.path.join(store_dir, name))
    except OSError:
        pass
    return out


def main():
    ap = argparse.ArgumentParser(description="清理会话里残留的 <memory> 控制块")
    ap.add_argument("--file", default="", help="只处理指定文件（默认扫描 sessions.json + 快照）")
    ap.add_argument("--apply", action="store_true", help="写回（默认只读）")
    ap.add_argument("--force", action="store_true", help="应用运行中也强制写回（不推荐）")
    args = ap.parse_args()

    running = _app_running()
    if running:
        print("检测到应用正在运行。写回请先退出应用，否则内存里的旧数据会覆盖结果。")
    if args.apply and running and not args.force:
        print("已停止：请退出应用后重试，或确认风险后加 --force。")
        return 2

    total = {"files": 0, "sessions": 0, "messages": 0, "chars": 0}
    pending = []
    for path in _targets(args.file):
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            print("跳过（无法解析）：%s（%s）" % (path, e))
            continue
        stats = clean_sessions_data(data)
        if not stats["messages"]:
            continue
        pending.append((path, data, stats))
        total["files"] += 1
        total["sessions"] += stats["sessions"]
        total["messages"] += stats["messages"]
        total["chars"] += stats["chars"]
        print("%s：%d 个会话 / %d 条消息 / 可删 %d 字符"
              % (path, stats["sessions"], stats["messages"], stats["chars"]))

    if not total["messages"]:
        print("没有发现残留的 <memory> 块。")
        return 0

    print("\n合计：%d 个文件 / %d 个会话 / %d 条消息，可删 %d 字符（约 %.1f KB）"
          % (total["files"], total["sessions"], total["messages"], total["chars"],
             total["chars"] / 1024.0))
    if not args.apply:
        print("这是只读预览。确认无误后：先退出应用，再加 --apply 执行。")
        return 0

    stamp = time.strftime("%Y%m%d-%H%M%S")
    for path, data, _stats in pending:
        bak = "%s.bak-%s" % (path, stamp)
        try:
            shutil.copy2(path, bak)
        except OSError as e:
            print("备份失败，已停止：%s（%s）" % (path, e))
            return 2
        if isinstance(data.get("sessions"), list):
            data["saved_at"] = time.time()
        store.write_json(path, data, indent=None, remember=False)
        print("已写回：%s（备份 %s）" % (path, bak))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
