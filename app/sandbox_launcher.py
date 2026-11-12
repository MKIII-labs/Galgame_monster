# -*- coding: utf-8 -*-
"""AppContainer 启动器。

由主进程用「最小环境」启动，本进程再把命令放进 AppContainer 执行：
环境继承自本进程（已被主进程清理），因此 API key 不会进入沙箱子进程。
输出直接写到本进程的 stdout（主进程会读这条管道）。
"""
import base64
import json
import sys


def main():
    if len(sys.argv) < 2:
        return 2
    from app import sandbox_win
    try:
        payload = json.loads(base64.b64decode(sys.argv[1]).decode("utf-8"))
        return sandbox_win.run_direct(
            payload.get("argv") or [],
            payload.get("shell_cmd"),
            payload.get("cwd") or "",
            bool(payload.get("network", True)))
    except Exception as e:                 # noqa: BLE001  启动失败也要让主进程看到原因
        sys.stderr.write("[sandbox-launcher] %s\n" % e)
        return 3


if __name__ == "__main__":
    sys.exit(main())
