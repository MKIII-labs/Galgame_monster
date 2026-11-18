# -*- coding: utf-8 -*-
"""子进程窗口控制：别让后台命令弹黑框。

本应用通常以 pythonw 启动（没有控制台）。此时每个**控制台子进程**
（python / node / taskkill / cmd）都会被 Windows 新建一个控制台窗口，
表现为「AI 一干活就闪一下黑框」，正在用电脑的人会被打断。

统一给这类子进程加 CREATE_NO_WINDOW；GUI 程序（Edge、VS Code）不受影响，
加不加都一样。非 Windows 返回 0，subprocess 会忽略这个参数。
"""
import os
import subprocess

# CREATE_NO_WINDOW 在 Python 3.7+ 的 subprocess 里有，但只在 Windows 上定义；
# 这里写死值，避免在别的平台 getattr 拿到 None 再去传给 subprocess。
CREATE_NO_WINDOW = 0x08000000

# install_default() 是否已安装过（入口可能被调用多次）
_INSTALLED = False


def no_window():
    """Windows 下隐藏子进程控制台窗口；其它平台返回 0。"""
    return CREATE_NO_WINDOW if os.name == "nt" else 0


def install_default():
    """给进程内所有没显式声明的子进程默认加上 CREATE_NO_WINDOW。

    为什么要兜底而不是逐处传：
    - 本应用用 pythonw 启动，自身没有控制台。Windows 的规则是「父进程没有控制台、
      创建子进程时又不带 CREATE_NO_WINDOW」，就给子进程分配一个新控制台 → 黑框。
    - 逐处传参依赖「每个新链路都记得加」，实测就是加漏了（评测、脚本、插件各漏过）。
      这里在入口统一兜住，等价于 Node child_process 的 windowsHide 语义。

    显式传了 creationflags 的调用原样尊重（例如需要 CREATE_NEW_PROCESS_GROUP 的）。
    subprocess.run / call / check_output 内部都走 Popen，所以一并覆盖。

    返回 True 表示本次安装；非 Windows、或已经装过，返回 False。
    """
    global _INSTALLED
    if os.name != "nt" or _INSTALLED:
        return False
    _orig_init = subprocess.Popen.__init__
    # Popen(args, bufsize, executable, stdin, stdout, stderr, preexec_fn, close_fds,
    #       shell, cwd, env, universal_newlines, startupinfo, creationflags, ...)
    # creationflags 是 self 之后的第 14 个位置参数；少于此数说明没按位置传。
    positional_limit = 14

    def _init(self, *args, **kwargs):
        if "creationflags" not in kwargs and len(args) < positional_limit:
            kwargs["creationflags"] = CREATE_NO_WINDOW
        return _orig_init(self, *args, **kwargs)

    _init.__wrapped__ = _orig_init
    subprocess.Popen.__init__ = _init
    _INSTALLED = True
    return True
