# -*- coding: utf-8 -*-
"""本机语音朗读：工作流 tts.speak 步骤使用

GAL 台词朗读已下线，这里只保留不依赖网络与 TTS 配置的 Windows 本机朗读。
"""
import os


def speak_local(text, timeout=180):
    """用 Windows 自带语音在本机朗读（不依赖网络与 TTS 配置）。

    PowerShell 5.1 的命令行编码不可靠，中文正文改为写 UTF-8 临时文件再读取。
    返回 (ok, 信息)；失败不抛异常，由调用方决定是否忽略。
    """
    import subprocess
    import tempfile

    body = str(text or "").strip()
    if not body:
        return False, "文本为空"
    fd, path = tempfile.mkstemp(prefix="gm_tts_", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        safe = path.replace("'", "''")
        ps = (
            "$ErrorActionPreference='Stop';"
            "Add-Type -AssemblyName System.Speech;"
            "$t=Get-Content -Raw -Encoding UTF8 '%s';"
            "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
            "$s.Speak($t);" % safe)
        r = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-Command", ps],
            capture_output=True, text=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        return r.returncode == 0, out
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
