# -*- coding: utf-8 -*-
"""兼容入口：实现已拆分到 app/ 包（见 app/httpd.py）。"""
from app.httpd import create_server  # noqa: F401
