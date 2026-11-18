# -*- coding: utf-8 -*-
"""移动端 API 白名单与限流。

原则：手机只能触达「聊天 / 审批 / 截图 / 语音 / 会话读取」，
配置、终端、IDE、插件、密钥等一律不在名单里（网关直接 403，不转发）。
"""
import threading
import time

# 本机内部接口：已做脱敏（不含 config / 密钥），供网关调用
GET_PATHS = {
    "/api/mobile/state",
}

POST_PATHS = {
    "/api/mobile/state",
    "/api/chat",
    "/api/chat/attach",
    "/api/stop",
    "/api/session/new",
    "/api/session/messages",
    "/api/session/search",
    "/api/agent/approve",
    "/api/agent/answer",
    "/api/agent/ask/dismiss",
    "/api/sandbox/raw",
    "/api/attach/get",
    "/api/stt",
    "/api/stt/polish",
}

# 需要设备私钥签名（网关校验）+ 手机端生物识别的敏感动作
SENSITIVE_PATHS = {
    "/api/agent/approve",
    "/api/agent/answer",
    "/api/session/new",
    "/api/chat",
}

MAX_BODY = 8 * 1024 * 1024


def allowed(method, path):
    m = str(method or "").upper()
    p = str(path or "")
    if m == "GET":
        return p in GET_PATHS
    if m == "POST":
        return p in POST_PATHS
    return False


def is_sensitive(path):
    return str(path or "") in SENSITIVE_PATHS


class TokenBucket:
    """简单令牌桶：rate 个/秒，最多 burst 个。"""

    def __init__(self, rate=20.0, burst=40):
        self.rate = float(rate)
        self.burst = float(burst)
        self.tokens = float(burst)
        self.stamp = time.time()
        self._lock = threading.Lock()

    def take(self, cost=1.0):
        with self._lock:
            now = time.time()
            self.tokens = min(self.burst, self.tokens + (now - self.stamp) * self.rate)
            self.stamp = now
            if self.tokens < cost:
                return False
            self.tokens -= cost
            return True
