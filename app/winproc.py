# -*- coding: utf-8 -*-
"""子进程窗口控制：别让后台命令弹黑框。

本应用通常以 pythonw 启动（没有控制台）。此时每个**控制台子进程**
（python / node / taskkill / cmd）都会被 Windows 新建一个控制台窗口，
表现为「AI 一干活就闪一下黑框」，正在用电脑的人会被打断。

统一给这类子进程加 CREATE_NO_WINDOW；GUI 程序（Edge、VS Code）不受影响，
加不加都一样。非 Windows 返回 0，subprocess 会忽略这个参数。
"""
import os

# CREATE_NO_WINDOW 在 Python 3.7+ 的 subprocess 里有，但只在 Windows 上定义；
# 这里写死值，避免在别的平台 getattr 拿到 None 再去传给 subprocess。
CREATE_NO_WINDOW = 0x08000000


def no_window():
    """Windows 下隐藏子进程控制台窗口；其它平台返回 0。"""
    return CREATE_NO_WINDOW if os.name == "nt" else 0
