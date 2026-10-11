# -*- coding: utf-8 -*-
"""本地语音识别（sherpa-onnx，离线运行，音频不出本机）

- 流式（实时）：data/models/sherpa-onnx-streaming-zipformer-zh-int8-2025-06-30
- 离线（整段）：data/models/sherpa-onnx-paraformer-zh-small-2024-03-09
模型需先下载（见 scripts/fetch_speech_model.py）。识别器懒加载、进程内单例。
"""
import fnmatch
import json
import os
import threading
import time
import uuid

import requests

from app.store import DATA_DIR
from app.config import load_config, get_api_conf, normalize_content

MODEL_NAME = "sherpa-onnx-paraformer-zh-small-2024-03-09"
DEFAULT_MODEL_DIR = os.path.join(DATA_DIR, "models", MODEL_NAME)

STREAM_MODEL_NAME = "sherpa-onnx-streaming-zipformer-zh-int8-2025-06-30"
STREAM_MODEL_DIR = os.path.join(DATA_DIR, "models", STREAM_MODEL_NAME)
STREAM_TTL = 120                    # 空闲多久回收一条流式会话（秒）

DENOISE_MODEL_NAME = "dpdfnet_baseline.onnx"       # 流式降噪（DPDFNet baseline）
DENOISE_MODEL = os.path.join(DATA_DIR, "models", DENOISE_MODEL_NAME)

# 两遍识别的「精修」离线模型（SenseVoice：中英日韩粤）
FINAL_MODEL_NAME = "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17"
FINAL_MODEL_DIR = os.path.join(DATA_DIR, "models", FINAL_MODEL_NAME)
MAX_FINAL_SECONDS = 180            # 两遍识别最多缓存多长音频（秒）

_lock = threading.Lock()
_recognizer = None

_stream_lock = threading.Lock()
_online = None
_STREAMS = {}                       # sid -> {"stream","ts","denoiser","audio","n"}

_denoise_lock = threading.Lock()
_denoiser = None

_final_lock = threading.Lock()
_final = None


def model_dir():
    """模型目录：优先取配置里的 speech_model_dir，未配置则用 data/models 下的默认目录"""
    d = (load_config().get("speech_model_dir") or "").strip()
    return d or DEFAULT_MODEL_DIR


def find_model_files():
    """在模型目录里找 (onnx 文件, tokens 文件)；找不到返回 (None, None)"""
    d = model_dir()
    tokens = os.path.join(d, "tokens.txt")
    if not os.path.isfile(tokens):
        return None, None
    for name in ("model.int8.onnx", "model.onnx"):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p, tokens
    return None, None


def status():
    """给前端用的就绪状态"""
    onnx_path, _tokens = find_model_files()
    stream_ready_ = find_stream_model_files() is not None
    return {"ready": stream_ready_, "offline": bool(onnx_path), "stream": stream_ready_,
            "denoise": find_denoise_model() is not None,
            "final": find_final_model_files() is not None,
            "model_dir": model_dir(), "stream_dir": stream_model_dir()}


def _get_recognizer():
    global _recognizer
    if _recognizer is not None:
        return _recognizer
    with _lock:
        if _recognizer is not None:
            return _recognizer
        onnx_path, tokens = find_model_files()
        if not onnx_path:
            raise RuntimeError("未找到语音模型，请先运行 scripts/fetch_speech_model.py 下载")
        import sherpa_onnx
        _recognizer = sherpa_onnx.OfflineRecognizer.from_paraformer(
            paraformer=onnx_path,
            tokens=tokens,
            num_threads=2,
            sample_rate=16000,
            feature_dim=80,
            decoding_method="greedy_search",
            debug=False,
        )
    return _recognizer


def transcribe(samples, sample_rate=16000):
    """识别一段音频。

    samples: float32 的 numpy 数组，取值约 -1.0 ~ 1.0（单声道）。
    返回识别出的文本（已去首尾空白）；无内容时返回空串。
    """
    import numpy as np
    rec = _get_recognizer()
    samples = np.ascontiguousarray(samples, dtype=np.float32)
    stream = rec.create_stream()
    stream.accept_waveform(int(sample_rate), samples)
    rec.decode_stream(stream)
    return (stream.result.text or "").strip()


# ============================================================
# 流式（实时）识别：边说边出字
# ============================================================

def stream_model_dir():
    """流式模型目录：优先取配置里的 speech_stream_model_dir，否则用默认目录"""
    d = (load_config().get("speech_stream_model_dir") or "").strip()
    return d or STREAM_MODEL_DIR


def _pick(d, *patterns):
    """在目录里按通配符找一个文件（优先 int8），找不到返回 None"""
    if not os.path.isdir(d):
        return None
    names = sorted(os.listdir(d))
    int8 = [n for n in names if "int8" in n.lower()]
    for pool in (int8, names):
        for pat in patterns:
            for n in pool:
                if fnmatch.fnmatch(n, pat):
                    return os.path.join(d, n)
    return None


def find_stream_model_files():
    """定位流式 Zipformer 的 encoder/decoder/joiner/tokens；缺任一返回 None"""
    d = stream_model_dir()
    files = {
        "encoder": _pick(d, "*encoder*.onnx"),
        "decoder": _pick(d, "*decoder*.onnx"),
        "joiner": _pick(d, "*joiner*.onnx"),
        "tokens": _pick(d, "tokens.txt"),
    }
    return files if all(files.values()) else None


def _get_online_recognizer():
    global _online
    if _online is not None:
        return _online
    with _stream_lock:
        if _online is not None:
            return _online
        f = find_stream_model_files()
        if not f:
            raise RuntimeError("未找到流式语音模型，请先运行 scripts/fetch_speech_model.py streaming")
        import sherpa_onnx
        _online = sherpa_onnx.OnlineRecognizer.from_transducer(
            encoder=f["encoder"], decoder=f["decoder"], joiner=f["joiner"], tokens=f["tokens"],
            num_threads=2, sample_rate=16000, feature_dim=80,
            decoding_method="greedy_search", enable_endpoint_detection=False, provider="cpu",
        )
    return _online


def deny_model_path():
    """降噪模型路径：优先取配置里的 speech_denoise_model，否则用默认文件"""
    d = (load_config().get("speech_denoise_model") or "").strip()
    return d or DENOISE_MODEL


def find_denoise_model():
    """返回降噪模型路径；不存在返回 None"""
    p = deny_model_path()
    return p if (p and os.path.isfile(p)) else None


def _get_denoiser():
    """懒加载流式降噪器（OnlineSpeechDenoiser，进程内单例）"""
    global _denoiser
    if _denoiser is not None:
        return _denoiser
    with _denoise_lock:
        if _denoiser is not None:
            return _denoiser
        path = find_denoise_model()
        if not path:
            raise RuntimeError("未找到降噪模型，请先运行 scripts/fetch_speech_model.py denoise")
        import sherpa_onnx
        cfg = sherpa_onnx.OnlineSpeechDenoiserConfig(
            model=sherpa_onnx.OfflineSpeechDenoiserModelConfig(
                dpdfnet=sherpa_onnx.OfflineSpeechDenoiserDpdfNetModelConfig(model=path),
                num_threads=1, debug=False, provider="cpu"))
        assert cfg.validate(), "降噪模型配置无效：" + path
        _denoiser = sherpa_onnx.OnlineSpeechDenoiser(cfg)
    return _denoiser


def _denoise_chunk(denoiser, samples, sample_rate):
    """降噪一段音频，返回 (降噪后 float32 数组, 输出采样率)"""
    import numpy as np
    out = denoiser.run(np.ascontiguousarray(samples, dtype=np.float32), int(sample_rate))
    return np.asarray(out.samples, dtype=np.float32), int(denoiser.sample_rate)


def final_model_dir():
    """精修模型目录：优先取配置里的 speech_final_model_dir，否则用默认目录"""
    d = (load_config().get("speech_final_model_dir") or "").strip()
    return d or FINAL_MODEL_DIR


def find_final_model_files():
    """定位 SenseVoice 精修模型；缺文件返回 None"""
    d = final_model_dir()
    model = _pick(d, "*model*.onnx")
    tokens = _pick(d, "tokens.txt")
    return {"model": model, "tokens": tokens} if (model and tokens) else None


def _get_final_recognizer():
    """懒加载离线「精修」识别器（SenseVoice，进程内单例）"""
    global _final
    if _final is not None:
        return _final
    with _final_lock:
        if _final is not None:
            return _final
        f = find_final_model_files()
        if not f:
            raise RuntimeError("未找到精修模型，请先运行 scripts/fetch_speech_model.py final")
        import sherpa_onnx
        _final = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=f["model"], tokens=f["tokens"], num_threads=2, use_itn=True, debug=False)
    return _final


def _decode_full(audio, sample_rate=16000):
    """用离线精修模型对整段音频解码，返回文本（失败抛出）"""
    import numpy as np
    rec = _get_final_recognizer()
    st = rec.create_stream()
    st.accept_waveform(int(sample_rate), np.ascontiguousarray(audio, dtype=np.float32))
    rec.decode_stream(st)
    return (st.result.text or "").strip()


def _append_audio(item, samples, sample_rate):
    """把音频追加进会话缓存（统一重采样到 16k；超长则不再追加）"""
    import numpy as np
    if samples is None or not len(samples):
        return
    sr = int(sample_rate or 16000)
    if sr != 16000:                      # 线性重采样到 16k，保证两遍识别输入一致
        ratio = sr / 16000.0
        n = max(1, int(len(samples) / ratio))
        idx = np.clip((np.arange(n) * ratio).astype(np.int64), 0, len(samples) - 1)
        samples = samples[idx]
    if item.get("n", 0) + len(samples) > 16000 * MAX_FINAL_SECONDS:
        return
    item["audio"].append(np.ascontiguousarray(samples, dtype=np.float32))
    item["n"] = item.get("n", 0) + len(samples)


def warmup():
    """预热：把流式识别模型提前载入内存，避免首次点麦克风时才开始加载（会有几秒延迟）"""
    try:
        if find_stream_model_files():
            _get_online_recognizer()
            return True
    except Exception as e:
        print("[语音] 预热失败：%s" % e)
    return False


def _sweep_streams():
    now = time.time()
    with _stream_lock:
        for sid in [s for s, it in _STREAMS.items() if now - it["ts"] > STREAM_TTL]:
            _STREAMS.pop(sid, None)


def stream_start(denoise=False):
    """开一条新的流式识别会话，返回 sid；denoise=True 时先做流式降噪再识别"""
    _sweep_streams()
    rec = _get_online_recognizer()
    denoiser = None
    if denoise:
        denoiser = _get_denoiser()          # 未就绪会抛异常，由调用方转成友好提示
    sid = uuid.uuid4().hex[:12]
    with _stream_lock:
        _STREAMS[sid] = {"stream": rec.create_stream(), "ts": time.time(),
                         "denoiser": denoiser, "audio": [], "n": 0}
    return sid


def stream_feed(sid, samples, sample_rate=16000):
    """喂一段音频（float32 numpy），返回当前的完整识别文本"""
    import numpy as np
    rec = _get_online_recognizer()
    with _stream_lock:
        item = _STREAMS.get(sid)
    if not item:
        return ""
    st = item["stream"]
    if samples is not None and len(samples):
        if item.get("denoiser") is not None:
            samples, sample_rate = _denoise_chunk(item["denoiser"], samples, sample_rate)
        if len(samples):
            _append_audio(item, samples, sample_rate)   # 存一份，供两遍识别精修
            st.accept_waveform(int(sample_rate), np.ascontiguousarray(samples, dtype=np.float32))
    while rec.is_ready(st):
        rec.decode_stream(st)
    item["ts"] = time.time()
    return (rec.get_result(st) or "").strip()


def stream_finish(sid, two_pass=True):
    """结束会话并返回最终文本，同时释放该流。

    two_pass=True 且精修模型可用时，用离线模型对**整段音频**重新解码，
    结果覆盖流式的实时文本（两遍识别：实时用流式，最终用离线）。
    """
    import numpy as np
    rec = _get_online_recognizer()
    with _stream_lock:
        item = _STREAMS.pop(sid, None)
    if not item:
        return ""
    st = item["stream"]
    if item.get("denoiser") is not None:
        try:                                # 冲降噪器尾音，一并送进识别与缓存
            tail = item["denoiser"].flush()
            arr = np.asarray(tail.samples, dtype=np.float32)
            if len(arr):
                _append_audio(item, arr, int(item["denoiser"].sample_rate))
                st.accept_waveform(int(item["denoiser"].sample_rate), arr)
        except Exception:
            pass
    try:
        st.input_finished()                 # 冲掉识别器尾部缓冲，让最后一个字也出来
    except Exception:
        pass
    while rec.is_ready(st):
        rec.decode_stream(st)
    text = (rec.get_result(st) or "").strip()
    if two_pass and item.get("audio"):
        try:
            audio = (item["audio"][0] if len(item["audio"]) == 1
                     else np.concatenate(item["audio"]))
            better = _decode_full(audio, 16000)
            if better:
                text = better
        except Exception as e:
            print("[语音] 精修失败，沿用流式结果：%s" % e)
    return text


# ============================================================
# AI 矫正（用「聊天 API」模型做最小必要的校对）
# ============================================================

_POLISH_PROMPT = (
    "你是语音转写的后处理器。请对用户给出的转写文本做**最小必要**的修正："
    "补全标点、修正明显的错别字与同音字、去掉重复口癖（如「嗯」「那个」）。"
    "**必须保持原意与原措辞**，不要改写、扩写、润色、翻译或解释；无法确定的地方保持原样。"
    "只输出修正后的文本本身，不要任何说明、前后缀或引号。"
)


def polish(text):
    """把语音转写文本交给聊天模型校正；失败或未配置密钥时原样返回"""
    text = (text or "").strip()
    if not text:
        return ""
    api_key, api_url, api_model = get_api_conf("chat")
    if not api_key:
        return text
    payload = {"model": api_model, "stream": False, "messages": [
        {"role": "system", "content": _POLISH_PROMPT},
        {"role": "user", "content": text},
    ]}
    try:
        resp = requests.post(api_url, headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json; charset=utf-8"},
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), timeout=60)
        resp.raise_for_status()
        out = normalize_content(resp.json()["choices"][0]["message"]["content"])
        return (out or "").strip() or text
    except Exception:
        return text
