# -*- coding: utf-8 -*-
"""API JSON 响应压缩：大响应 gzip，小响应保持明文。"""
import gzip

from app import httpd


def test_small_json_stays_plain():
    body, extra = httpd.gzip_json_body('{"a":1}', 'gzip, deflate')
    assert extra is None and body == b'{"a":1}'


def test_large_json_is_gzipped_and_roundtrips():
    text = '{"text":"' + '中文内容' * 4000 + '"}'
    body, extra = httpd.gzip_json_body(text, 'gzip, deflate, br')
    assert extra and extra.get("Content-Encoding") == "gzip"
    assert extra.get("Vary") == "Accept-Encoding"
    assert gzip.decompress(body).decode("utf-8") == text
    assert len(body) < len(text) / 3


def test_client_refuses_gzip():
    text = '{"text":"' + '中文内容' * 4000 + '"}'
    body, extra = httpd.gzip_json_body(text, 'gzip;q=0, identity')
    assert extra is None
    assert body.decode("utf-8") == text
    body2, extra2 = httpd.gzip_json_body(text, '')
    assert extra2 is None
