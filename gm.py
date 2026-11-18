#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""gm —— 在终端里和 Galgame_monster 对话的 CLI 客户端。

它不是另一个后端：只是连上正在运行的应用（同一个 data/、同一套会话与沙箱），
把 SSE 事件渲染成终端文本。所以 GUI 里聊到一半，切到终端继续是同一个会话。

用法：
    gm                          交互式（接着最近一次会话）
    gm -p "帮我跑一下测试"       单次问答后退出
    gm -n                       新建会话后进入交互
    gm -l                       列出会话（编号可直接给 -s）
    gm -s 3                     指定第 3 个会话
    gm --start                  应用没在跑时先把它拉起来
    gm -v                       显示思考过程与工具输出

交互式里的命令：/list /use N /new /stop /verbose /help /quit
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(ROOT, "data", "server.json")

# Windows 终端默认代码页不是 UTF-8；先把自己的输出通道掰成 UTF-8，避免中文乱码。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _enable_ansi():
    """老版控制台（conhost）默认不解析 ANSI；打开 VT 处理。"""
    if os.name != "nt":
        return True
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not k32.GetConsoleMode(h, ctypes.byref(mode)):
            return False
        return bool(k32.SetConsoleMode(h, mode.value | 0x0004))
    except Exception:
        return False


USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
if USE_COLOR:
    USE_COLOR = _enable_ansi()


def paint(code, text):
    return ("\033[%sm%s\033[0m" % (code, text)) if USE_COLOR else text


def dim(t):
    return paint("2", t)


def bold(t):
    return paint("1", t)


def cyan(t):
    return paint("36", t)


def green(t):
    return paint("32", t)


def red(t):
    return paint("31", t)


def yellow(t):
    return paint("33", t)


class CliError(Exception):
    pass


# ---------------------------------------------------------------- 连接


def read_runtime():
    """读服务端写的 runtime 文件，返回 (base_url, token)。"""
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        raise CliError("应用没有在运行（找不到 %s）" % os.path.relpath(STATE_FILE, ROOT))
    port = int(st.get("port") or 0)
    token = str(st.get("token") or "")
    if not port:
        raise CliError("runtime 文件里没有端口，请重启应用")
    if not token:
        raise CliError("runtime 文件里没有令牌：这个实例是旧版本，重启应用后再试")
    return "http://127.0.0.1:%d" % port, token


def start_app():
    """用和快捷方式相同的方式把应用拉起来（不打开控制台窗口）。"""
    exe = os.path.join(ROOT, ".venv", "Scripts", "pythonw.exe")
    if not os.path.isfile(exe):
        exe = sys.executable
    flags = 0
    if os.name == "nt":
        flags = 0x08000000          # CREATE_NO_WINDOW
    subprocess.Popen([exe, os.path.join(ROOT, "main.py")],
                     cwd=ROOT, creationflags=flags,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 45
    while time.time() < deadline:
        time.sleep(0.5)
        try:
            return read_runtime()
        except CliError:
            continue
    raise CliError("启动超时：应用没能在 45 秒内就绪")


class Client:
    def __init__(self, base, token):
        self.base = base
        self.token = token

    def _request(self, path, body=None, method=None, timeout=60):
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.base + path, data=data,
            method=method or ("POST" if data is not None else "GET"))
        req.add_header("X-Auth-Token", self.token)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read().decode("utf-8", "replace")).get("message")
            except Exception:
                msg = ""
            raise CliError(msg or ("HTTP %s" % e.code))
        except Exception as e:
            raise CliError(str(e))
        try:
            return json.loads(raw or "{}")
        except Exception:
            return {}

    def state(self):
        return self._request("/api/state", timeout=20)

    def sessions(self):
        payload = self.state().get("sessions") or {}
        return payload.get("sessions") or [], payload.get("current") or ""

    def new_session(self):
        r = self._request("/api/session/new", {})
        cur = (r.get("sessions") or {}).get("current") or ""
        return cur

    def stop(self, sid):
        return self._request("/api/stop", {"session_id": sid})

    def stream(self, sid, text, verbose=False, auto_yes=False):
        """发一条消息，边收边打印。返回 done 事件（可能为空 dict）。"""
        body = json.dumps({
            "session_id": sid,
            "content": text,
            "think_enabled": True,
            "think_level": "high",
            "search_enabled": False,
        }, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.base + "/api/chat", data=body, method="POST")
        req.add_header("X-Auth-Token", self.token)
        req.add_header("Content-Type", "application/json")

        think_open = False
        wrote = False
        done_obj = {}

        def close_think():
            nonlocal think_open, wrote
            if think_open:
                sys.stdout.write("\n")
                think_open = False

        try:
            resp = urllib.request.urlopen(req, timeout=None)
        except urllib.error.HTTPError as e:
            try:
                j = json.loads(e.read().decode("utf-8", "replace"))
                msg = j.get("message") or j.get("error") or ("HTTP %s" % e.code)
            except Exception:
                msg = "HTTP %s" % e.code
            raise CliError(msg)

        with resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload:
                    continue
                try:
                    ev = json.loads(payload)
                except Exception:
                    continue
                kind = ev.get("type")
                if kind == "reasoning":
                    if verbose:
                        if not think_open and not wrote:
                            sys.stdout.write(dim("思考 … "))
                            think_open = True
                        sys.stdout.write(dim(ev.get("text") or ""))
                        sys.stdout.flush()
                elif kind == "delta":
                    close_think()
                    sys.stdout.write(ev.get("text") or "")
                    sys.stdout.flush()
                    wrote = True
                elif kind == "tool":
                    close_think()
                    name = ev.get("name") or "tool"
                    sys.stdout.write("\n" + dim("  ⚙ " + name + _arg_text(ev.get("args"))))
                    sys.stdout.flush()
                elif kind == "tool_done":
                    if ev.get("ok") is False:
                        sys.stdout.write("\n" + red("  ✘ " + str(ev.get("name") or "")) + "\n")
                    elif verbose:
                        ms = ev.get("ms")
                        sys.stdout.write("\n" + dim("  ✔ %s%s" % (
                            ev.get("name") or "", (" · %dms" % int(ms)) if ms else "")))
                    sys.stdout.flush()
                elif kind == "approval":
                    close_think()
                    self._approval(ev, sid, auto_yes)
                elif kind == "ask":
                    close_think()
                    self._answer(ev)
                elif kind == "error":
                    close_think()
                    sys.stdout.write("\n" + red(str(ev.get("message") or "出错了")) + "\n")
                    sys.stdout.flush()
                elif kind == "end_reason":
                    if verbose and ev.get("reason"):
                        sys.stdout.write("\n" + dim("结束原因：" + str(ev.get("reason"))))
                elif kind == "done":
                    done_obj = ev
                    break
        close_think()
        if not wrote and not done_obj:
            sys.stdout.write(dim("（没有输出）"))
        sys.stdout.write("\n")
        sys.stdout.flush()
        return done_obj

    def _approval(self, ev, sid, auto_yes):
        name = str(ev.get("name") or "工具")
        arg = _arg_text(ev.get("args"))
        reason = str(ev.get("reason") or "")
        sys.stdout.write("\n" + yellow("需要确认：%s%s" % (name, arg)) + "\n")
        if reason:
            sys.stdout.write(dim("  " + reason) + "\n")
        ok = auto_yes
        remember = False
        if sys.stdin.isatty() and not auto_yes:
            try:
                ans = input("允许？[y]允许 / [n]拒绝 / [a]本轮都允许：").strip().lower()
            except (EOFError, KeyboardInterrupt):
                ans = "n"
            ok = ans in ("y", "a", "yes")
            remember = ans in ("a", "always")
        self._request("/api/agent/approve", {
            "session_id": sid, "aid": ev.get("aid"),
            "ok": ok, "remember": remember, "always": False,
        }, timeout=20)
        sys.stdout.write(dim("  → " + ("已允许" if ok else "已拒绝")) + "\n")

    def _answer(self, ev):
        qs = ev.get("questions") or []
        qid = ev.get("qid")
        if not qs or not qid:
            return
        for q in qs:
            sys.stdout.write("\n" + cyan("AI 问：") + str(q.get("question") or "") + "\n")
        if not sys.stdin.isatty():
            self._request("/api/agent/ask/dismiss", {"qid": qid}, timeout=20)
            sys.stdout.write(dim("  → 非交互模式，交给 AI 自行决定") + "\n")
            return
        answers = []
        for q in qs:
            try:
                answers.append(input("回答：").strip())
            except (EOFError, KeyboardInterrupt):
                answers.append("")
        self._request("/api/agent/answer", {"qid": qid, "answers": answers}, timeout=20)


def _arg_text(args):
    """把工具参数压成一行预览。"""
    if not args:
        return ""
    try:
        s = json.dumps(args, ensure_ascii=False)
    except Exception:
        s = str(args)
    s = " ".join(s.split())
    return " · " + (s[:80] + "…" if len(s) > 80 else s)


# ---------------------------------------------------------------- 会话


def print_sessions(sessions, current):
    if not sessions:
        print(dim("（还没有会话）"))
        return
    for i, s in enumerate(sessions, 1):
        mark = green("●") if s.get("id") == current else " "
        title = (s.get("title") or "新对话").replace("\n", " ")
        ts = time.strftime("%m-%d %H:%M",
                           time.localtime(float(s.get("updated_at") or s.get("updated") or 0)))
        print("%s %2d. %s  %s" % (mark, i, title[:52], dim(ts)))


def pick_session(sessions, current, arg):
    if not arg:
        if current:
            return current
        return sessions[0].get("id") if sessions else ""
    try:
        idx = int(arg)
        if 1 <= idx <= len(sessions):
            return sessions[idx - 1].get("id")
    except ValueError:
        pass
    for s in sessions:
        if s.get("id") == arg:
            return arg
    raise CliError("找不到会话：%s（用 -l 看编号）")


HELP = """命令：
  /list        列出会话
  /use N       切到第 N 个会话
  /new         新建会话
  /stop        停止正在生成的回复
  /verbose     切换「显示思考与工具输出」
  /help        这份帮助
  /quit        退出（Ctrl+D 也行）"""


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gm", description="在终端里和 Galgame_monster 对话")
    ap.add_argument("-p", "--prompt", help="单次问答：发完这条就退出")
    ap.add_argument("-n", "--new", action="store_true", help="新建会话")
    ap.add_argument("-l", "--list", action="store_true", help="列出会话")
    ap.add_argument("-s", "--session", help="指定会话（编号或 id）")
    ap.add_argument("--start", action="store_true", help="应用没在运行时先启动它")
    ap.add_argument("-v", "--verbose", action="store_true", help="显示思考过程与工具输出")
    ap.add_argument("--yes", action="store_true", help="自动允许需要确认的工具（谨慎）")
    ap.add_argument("--url", help="直接指定服务地址（默认读 data/server.json）")
    ap.add_argument("--token", help="直接指定令牌（配合 --url）")
    args = ap.parse_args(argv)

    try:
        base, token = ((args.url, args.token) if (args.url and args.token)
                       else read_runtime())
    except CliError:
        if not args.start:
            raise
        base, token = start_app()

    cli = Client(base, token)
    sessions, current = cli.sessions()

    if args.list:
        print_sessions(sessions, current)
        return 0
    if args.new:
        current = cli.new_session()
        sessions, current = cli.sessions()
    if args.session:
        current = pick_session(sessions, current, args.session)
    if not current:
        current = cli.new_session()
        sessions, current = cli.sessions()

    verbose = bool(args.verbose)

    if args.prompt:
        cli.stream(current, args.prompt, verbose=verbose, auto_yes=args.yes)
        return 0

    # 交互式
    title = next((s.get("title") for s in sessions if s.get("id") == current), "新对话")
    print(dim("已连接 %s · 会话：%s" % (base, title or "新对话")))
    print(dim("输入 /help 看命令，Ctrl+D 退出"))
    while True:
        try:
            line = input(cyan("› "))
        except (EOFError, KeyboardInterrupt):
            print()
            break
        text = line.strip()
        if not text:
            continue
        if text in ("/quit", "/exit", "/q"):
            break
        if text == "/help":
            print(HELP)
            continue
        if text == "/verbose":
            verbose = not verbose
            print(dim("思考/工具输出：" + ("开" if verbose else "关")))
            continue
        if text == "/list":
            sessions, current = cli.sessions()
            print_sessions(sessions, current)
            continue
        if text.startswith("/use"):
            arg = text[4:].strip()
            sessions, current = cli.sessions()
            try:
                current = pick_session(sessions, current, arg)
                t = next((s.get("title") for s in sessions if s.get("id") == current), "")
                print(dim("已切到：" + (t or "新对话")))
            except CliError as e:
                print(red(str(e)))
            continue
        if text == "/new":
            current = cli.new_session()
            print(dim("已新建会话"))
            continue
        if text == "/stop":
            try:
                cli.stop(current)
                print(dim("已发送停止"))
            except CliError as e:
                print(red(str(e)))
            continue
        try:
            cli.stream(current, text, verbose=verbose, auto_yes=args.yes)
        except KeyboardInterrupt:
            print()
            try:
                cli.stop(current)
                print(dim("已中断"))
            except CliError:
                pass
        except CliError as e:
            print(red("失败：" + str(e)))
        # 标题可能在首轮后由服务端生成，刷新一次用于提示
        try:
            sessions, current = cli.sessions()
        except CliError:
            pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except CliError as e:
        print(red(str(e)), file=sys.stderr)
        sys.exit(1)
