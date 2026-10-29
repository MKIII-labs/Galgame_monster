# -*- coding: utf-8 -*-
import time
from app.store import _lock
from app.context import current_context
from app.sessions import (find_session, load_sessions, save_sessions, slim_sessions)
from app.imaging import recognize_image
from app import speech

def _api_recognize(h):
        body = h._body()
        b64 = body.get("data") or ""
        mime = body.get("mime") or "image/png"
        if not b64:
            return h._json({"ok": False, "text": "⚠️ 未收到图片数据"})
        text = recognize_image(mime, b64)
        data = load_sessions()
        s = find_session(data, body.get("session_id"))
        if s and text and not text.startswith(("❌", "⏰", "⚠️")):
            s["messages"].append({"role": "assistant",
                                  "content": f"用户上传了一张图片，图片内容为：\n{text}"})
            s["updated_at"] = time.time()
            with _lock:
                save_sessions(data)
        h._json({"ok": True, "text": text, "sessions": slim_sessions(data),
                    "context": current_context(data)})


def _api_stt_start(h):
        """开始一条流式识别会话（可选开启流式降噪）"""
        if not speech.find_stream_model_files():
            return h._json({"ok": False,
                               "error": "流式语音模型未就绪，请运行 scripts/fetch_speech_model.py streaming"}, 400)
        denoise = bool(h._body().get("denoise"))
        if denoise and not speech.find_denoise_model():
            return h._json({"ok": False,
                               "error": "降噪模型未就绪，请运行 scripts/fetch_speech_model.py denoise"}, 400)
        try:
            sid = speech.stream_start(denoise=denoise)
        except Exception as e:
            return h._json({"ok": False, "error": "加载模型失败：%s" % e}, 500)
        h._json({"ok": True, "sid": sid, "denoise": denoise})


def _api_stt_chunk(h):
        """喂一段 16-bit 单声道 PCM，返回当前完整识别文本；sid 见 X-Stt-Sid 头"""
        sid = h.headers.get("X-Stt-Sid") or ""
        if not sid:
            return h._json({"ok": False, "error": "缺少 sid"}, 400)
        raw = h._body_raw()
        if len(raw) % 2:
            raw = raw[:-1]
        import numpy as np
        samples = np.frombuffer(raw, dtype="<i2").astype("float32") / 32768.0 if raw else None
        try:
            text = speech.stream_feed(sid, samples)
        except Exception as e:
            return h._json({"ok": False, "error": "识别失败：%s" % e}, 500)
        h._json({"ok": True, "text": text})


def _api_stt_finish(h):
        """结束流式会话，返回最终文本"""
        sid = h._body().get("sid")
        if not sid:
            return h._json({"ok": False, "error": "缺少 sid"}, 400)
        try:
            text = speech.stream_finish(sid)
        except Exception as e:
            return h._json({"ok": False, "error": "识别失败：%s" % e}, 500)
        h._json({"ok": True, "text": text})


def _api_stt_polish(h):
        """用聊天模型对转写文本做 AI 矫正"""
        text = h._body().get("text") or ""
        if not text.strip():
            return h._json({"ok": True, "text": ""})
        h._json({"ok": True, "text": speech.polish(text)})


def _api_stt(h):
        """本地语音识别：请求体为 16-bit 单声道 PCM 原始字节，采样率见 X-Sample-Rate 头"""
        if not speech.status().get("ready"):
            return h._json({"ok": False, "error": "语音模型未就绪，请先运行 scripts/fetch_speech_model.py 下载",
                               "speech": speech.status()}, 400)
        raw = h._body_raw()
        if len(raw) < 2:
            return h._json({"ok": False, "error": "没有收到音频数据"}, 400)
        if len(raw) % 2:
            raw = raw[:-1]                      # int16，丢弃可能的多余半字节
        try:
            sr = int(h.headers.get("X-Sample-Rate") or 16000)
        except ValueError:
            sr = 16000
        import numpy as np
        samples = np.frombuffer(raw, dtype="<i2").astype("float32") / 32768.0
        try:
            text = speech.transcribe(samples, sr)
        except Exception as e:
            return h._json({"ok": False, "error": "识别失败：%s" % e}, 500)
        h._json({"ok": True, "text": text})
