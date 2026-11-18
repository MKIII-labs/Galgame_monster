# -*- coding: utf-8 -*-
"""局域网地址探测与 Windows 防火墙入站规则。"""
import ctypes
import os
import socket
import subprocess
import time

RULE_NAME = "Galgame Monster Mobile"
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _udp_ip():
    """默认路由网卡地址（最可靠的「本机在局域网里的地址」）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return ""
    finally:
        s.close()


def _host_ips():
    try:
        return socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        return []


def _usable(ip):
    parts = str(ip or "").split(".")
    if len(parts) != 4 or not all(p.isdigit() for p in parts):
        return False
    a, b = int(parts[0]), int(parts[1])
    if a in (0, 127) or a >= 224:
        return False
    if a == 169 and b == 254:            # 链路本地（没拿到 DHCP）
        return False
    if a == 100 and 64 <= b <= 127:      # CGNAT / Tailscale
        return False
    if a == 192 and b == 168:
        return True
    if a == 10:
        return True
    if a == 172 and 16 <= b <= 31:       # 常见于 WSL / Hyper-V 虚拟网卡
        return True
    return False


def _rank(ip):
    if ip.startswith("192.168."):
        return 0
    if ip.startswith("10."):
        return 1
    return 2


def candidate_ips():
    """按可用性排序的局域网地址列表（首选在前）。"""
    seen, out = set(), []
    for ip in [_udp_ip()] + _host_ips():
        ip = str(ip or "").strip()
        if ip and ip not in seen and _usable(ip):
            seen.add(ip)
            out.append(ip)
    return sorted(out, key=_rank)


def lan_ip():
    ips = candidate_ips()
    return ips[0] if ips else ""


def is_admin():
    if os.name != "nt":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _netsh(args, timeout=20):
    try:
        p = subprocess.run(["netsh"] + list(args), capture_output=True, text=True,
                           timeout=timeout, creationflags=_NO_WINDOW,
                           encoding="utf-8", errors="replace")
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)


def firewall_status():
    if os.name != "nt":
        return {"supported": False, "present": False}
    code, _out = _netsh(["advfirewall", "firewall", "show", "rule", "name=" + RULE_NAME])
    return {"supported": True, "present": code == 0}


def _rule_command(port):
    """入站规则命令：只放行本子网（localsubnet），专用/公用网络都生效。

    Windows 常把家里的 Wi-Fi 判成「公用」，只写 private 会导致规则不命中、手机连不上。
    """
    return ('netsh advfirewall firewall add rule name="%s" dir=in action=allow '
            'protocol=TCP localport=%d profile=private,public remoteip=localsubnet'
            % (RULE_NAME, int(port)))


def _elevated(cmd, timeout=180):
    """用户主动点按钮时，用一次 UAC 提权执行命令（拒绝/失败返回 False）。"""
    # _rule_command 返回的是给人看的完整命令（含 netsh 前缀）；
    # Start-Process 已经指定了 -FilePath netsh，参数里必须去掉它，否则变成 netsh netsh ...
    args = cmd[6:] if cmd.lower().startswith("netsh ") else cmd
    ps = ("Start-Process -FilePath 'netsh' -Verb RunAs -WindowStyle Hidden "
          "-ArgumentList '%s' -Wait" % args.replace("'", "''"))
    try:
        proc = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                              capture_output=True, text=True, timeout=timeout,
                              creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def ensure_firewall(port):
    """添加（或更新）TCP 入站规则：只允许同一子网访问该端口。"""
    if os.name != "nt":
        return {"ok": True, "supported": False, "message": "非 Windows，无需防火墙规则"}
    cmd = _rule_command(port)
    if not is_admin():
        # 用户点了「添加防火墙规则」才会走到提权：只弹一次 UAC，不再要求手抄命令
        _elevated(cmd)
        for _ in range(20):
            if firewall_status()["present"]:
                break
            time.sleep(0.25)
        ok = firewall_status()["present"]
        return {"ok": ok, "supported": True, "admin": False, "command": cmd,
                "message": "" if ok else "没有添加上规则（UAC 被取消或失败）。可手动以管理员身份运行：\n" + cmd}
    _netsh(["advfirewall", "firewall", "delete", "rule", "name=" + RULE_NAME])
    code, out = _netsh(["advfirewall", "firewall", "add", "rule", "name=" + RULE_NAME,
                        "dir=in", "action=allow", "protocol=TCP",
                        "localport=%d" % int(port), "profile=private,public",
                        "remoteip=localsubnet"])
    return {"ok": code == 0, "supported": True, "admin": True,
            "message": "" if code == 0 else out.strip()[:300]}


def remove_firewall():
    if os.name != "nt" or not is_admin():
        return False
    code, _out = _netsh(["advfirewall", "firewall", "delete", "rule", "name=" + RULE_NAME])
    return code == 0
