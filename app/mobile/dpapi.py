# -*- coding: utf-8 -*-
"""Windows DPAPI：用当前用户凭据加密落盘的小工具（非 Windows 直接原样返回）。

只用于桌面身份私钥；解密只在本进程、同一 Windows 用户下可行。
"""
import base64
import ctypes
import os
import sys
from ctypes import wintypes


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data):
    buf = ctypes.create_string_buffer(data, len(data))
    return _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def available():
    return os.name == "nt" and sys.platform.startswith("win")


def protect(data):
    """加密字节串；失败返回 None（调用方退回明文，并自行提示）。"""
    if not available():
        return None
    try:
        blob, _buf = _blob(bytes(data))
        out = _DataBlob()
        ok = ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(blob), "gm-mobile", None, None, None, 0, ctypes.byref(out))
        if not ok:
            return None
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(out.pbData)
    except Exception:
        return None


def unprotect(data):
    """解密字节串；不是 DPAPI 密文 / 失败时返回 None。"""
    if not available():
        return None
    try:
        blob, _buf = _blob(bytes(data))
        out = _DataBlob()
        ok = ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blob), None, None, None, None, 0, ctypes.byref(out))
        if not ok:
            return None
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(out.pbData)
    except Exception:
        return None


def seal_text(text):
    """字符串 → dict（可直接塞进 JSON）。"""
    raw = str(text).encode("utf-8")
    enc = protect(raw)
    if enc is None:
        return {"dpapi": False, "data": base64.b64encode(raw).decode("ascii")}
    return {"dpapi": True, "data": base64.b64encode(enc).decode("ascii")}


def open_text(obj):
    """dict → 字符串；失败返回空串。"""
    if not isinstance(obj, dict):
        return ""
    try:
        raw = base64.b64decode(str(obj.get("data") or ""))
    except Exception:
        return ""
    if obj.get("dpapi"):
        raw = unprotect(raw)
        if raw is None:
            return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return ""
