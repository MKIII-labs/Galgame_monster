# -*- coding: utf-8 -*-
"""Windows 系统级提醒：注册/删除计划任务，让**程序没开时**也能到点弹通知。

两条实测结论（已验证，别改回去）：
1. 创建计划任务**不需要管理员**，用 PowerShell 的 Register-ScheduledTask 即可；
2. **必须**带 -AllowStartIfOnBatteries / -DontStopIfGoingOnBatteries —— 否则笔记本在电池
   供电时 Windows 会**静默跳过**任务（实测 Last Run Time 停在 1999，看起来像"没反应"）。

中文标题/正文不通过命令行传递（PS 5.1 的命令行编码不可靠），改用 UTF-8 JSON 载荷文件。
"""
import json
import os
import subprocess

from app.store import ROOT, NOTIFY_DIR, write_json

PS1 = os.path.join(ROOT, "scripts", "notify.ps1")
TASK_PREFIX = "Galgame_monster_Reminder_"   # 纯 ASCII：任务名要经命令行传给 PowerShell


def _run_ps(script):
    """执行一段 PowerShell，返回 (ok, 输出)。失败不抛异常，调用方决定怎么降级。"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True, text=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    return r.returncode == 0, out


def write_payload(task_id, title, text):
    """把通知内容写成 UTF-8 JSON，供 notify.ps1 读取；返回绝对路径"""
    os.makedirs(NOTIFY_DIR, exist_ok=True)
    path = os.path.join(NOTIFY_DIR, "%s.json" % task_id)
    write_json(path, {"title": title, "text": text}, indent=None)
    return path


def payload_path(task_id):
    return os.path.join(NOTIFY_DIR, "%s.json" % task_id)


def task_name(task_id):
    return TASK_PREFIX + str(task_id)


def register(task_id, at, title, text):
    """注册一次性计划任务（at = datetime）。返回 (ok, 信息)。"""
    payload = write_payload(task_id, title, text)
    at_str = at.strftime("%Y-%m-%dT%H:%M:%S")
    name = task_name(task_id)
    ps = (
        "$ErrorActionPreference='Stop';"
        "$act=New-ScheduledTaskAction -Execute 'powershell.exe' "
        "-Argument '-NoProfile -ExecutionPolicy Bypass -File \"{ps1}\" -Payload \"{pl}\"';"
        "$trg=New-ScheduledTaskTrigger -Once -At ([datetime]'{at}');"
        "$set=New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries "
        "-DontStopIfGoingOnBatteries -StartWhenAvailable;"
        "Register-ScheduledTask -TaskName '{name}' -Action $act -Trigger $trg "
        "-Settings $set -Force | Out-Null;"
        "Write-Output 'OK'"
    ).format(ps1=PS1, pl=payload, at=at_str, name=name)
    ok, out = _run_ps(ps)
    return (ok and "OK" in out), out


def unregister(task_id):
    """删除计划任务（不存在也算成功）"""
    ps = (
        "$ErrorActionPreference='SilentlyContinue';"
        "Unregister-ScheduledTask -TaskName '{name}' -Confirm:$false;"
        "Write-Output 'OK'"
    ).format(name=task_name(task_id))
    ok, out = _run_ps(ps)
    return (ok and "OK" in out), out


def cleanup_payload(task_id):
    try:
        os.remove(payload_path(task_id))
    except OSError:
        pass


def available():
    """本机是否具备「系统级提醒」的条件（Windows + 有 notify.ps1）"""
    return os.name == "nt" and os.path.isfile(PS1)


def supports_json_payload_ok():
    # 保留：便于单测打桩，不必真的调 PowerShell
    return available()
