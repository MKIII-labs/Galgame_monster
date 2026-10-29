# -*- coding: utf-8 -*-
"""资源管理器右键菜单：「用 Galgame Monster 打开」的注册 / 移除 / 查询。

只写 **HKEY_CURRENT_USER\\Software\\Classes**（当前用户），不需要管理员权限；
移除就是把对应子键删掉。

挂两处：
  * ``Directory\\shell``              —— 右键文件夹本身，参数 ``%1``
  * ``Directory\\Background\\shell``  —— 在文件夹空白处右键，参数 ``%V``（当前目录）

注意：这是「传统谓词」，Windows 11 把它折叠在右键菜单的「显示更多选项」里；
新式（一级）菜单需要 IExplorerCommand COM 组件 / MSIX 打包，不在本模块范围。
"""
import os
import sys

try:
    import winreg
except ImportError:                     # 非 Windows：让调用方拿到「不支持」
    winreg = None

VERB = "GalgameMonster"
LABEL = "用 Galgame Monster 打开"
ROOT = r"Software\Classes"

# (挂载位置, 传给 --open 的参数)
_TARGETS = (
    (r"Directory\shell", "%1"),
    (r"Directory\Background\shell", "%V"),
)


def _key_paths(root=ROOT):
    return [(root + "\\" + sub + "\\" + VERB, arg) for sub, arg in _TARGETS]


def command_for(python, main_py, arg):
    """右键菜单实际执行的命令行（三段都加引号：路径可能带空格）。"""
    return '"%s" "%s" --open "%s"' % (python, main_py, arg)


def _default_python():
    """与开始菜单快捷方式保持一致的解释器选择：python.exe 旁边有 pythonw 就用它（不弹黑框）。"""
    exe = sys.executable or ""
    if os.path.basename(exe).lower() == "python.exe":
        cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.isfile(cand):
            return cand
    return exe


def _supported():
    return winreg is not None and sys.platform.startswith("win")


def install(python="", main_py="", icon="", root=ROOT):
    """写入（或覆盖）右键菜单；返回 (ok, 说明)。"""
    if not _supported():
        return False, "仅支持 Windows"
    python = python or _default_python()
    if not python:
        return False, "找不到 Python 解释器"
    main_py = main_py or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")
    if not os.path.isfile(main_py):
        return False, "找不到 main.py：%s" % main_py
    try:
        for key_path, arg in _key_paths(root):
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0,
                                    winreg.KEY_WRITE) as k:
                winreg.SetValueEx(k, "MUIVerb", 0, winreg.REG_SZ, LABEL)
                if icon:
                    winreg.SetValueEx(k, "Icon", 0, winreg.REG_SZ, icon)
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path + r"\command",
                                    0, winreg.KEY_WRITE) as k:
                winreg.SetValueEx(k, "", 0, winreg.REG_SZ,
                                  command_for(python, main_py, arg))
        return True, "已注册"
    except OSError as e:
        return False, "写入注册表失败：%s" % e


def uninstall(root=ROOT):
    """删掉两个挂载点的子键；返回 (ok, 说明)。"""
    if not _supported():
        return False, "仅支持 Windows"
    removed = 0
    for key_path, _arg in _key_paths(root):
        for path in (key_path + r"\command", key_path):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
                removed += 1
            except FileNotFoundError:
                pass
            except OSError as e:
                return False, "删除注册表失败：%s" % e
    return True, ("已移除" if removed else "本来就没注册")


def status(root=ROOT):
    """{'installed': 两处都已写入, 'command': 第一条命令行}。"""
    out = {"installed": False, "command": "", "supported": _supported()}
    if not _supported():
        return out
    commands = []
    for key_path, _arg in _key_paths(root):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path + r"\command") as k:
                commands.append(str(winreg.QueryValueEx(k, "")[0] or ""))
        except OSError:
            commands.append("")
    out["installed"] = bool(commands) and all(commands)
    out["command"] = commands[0] if commands else ""
    return out
