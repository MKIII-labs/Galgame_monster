# -*- coding: utf-8 -*-
"""常驻推送总线：服务端在**没有用户请求**时也能把消息推给前端。

为什么需要它：现有 SSE 只在 /api/chat 的请求期间存在，请求一结束连接就断了。
「定时提醒」和「AI 主动发消息」都发生在用户没提问的时刻，必须有一条常驻通道。

实现：每个已连接的客户端一个 queue.Queue；publish() 时往所有队列投递。
httpd.py 的 /api/events 负责阻塞读取并把内容写成 SSE 帧。
"""
import queue
import threading

_subscribers = set()
_lock = threading.Lock()


def subscribe():
    """登记一个连接，返回它专属的消息队列"""
    q = queue.Queue()
    with _lock:
        _subscribers.add(q)
    return q


def unsubscribe(q):
    """连接断开时注销"""
    with _lock:
        _subscribers.discard(q)


def count():
    with _lock:
        return len(_subscribers)


def publish(obj):
    """向所有已连接的前端投递一条事件（没人连接时静默丢弃）"""
    with _lock:
        subs = list(_subscribers)
    for q in subs:
        try:
            q.put_nowait(obj)
        except Exception:
            pass
