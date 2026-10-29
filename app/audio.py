# -*- coding: utf-8 -*-
"""声音自检（audio_probe / audio_compare）：把「我听不见」变成可判定的数字。

为什么需要它（NOTE 43.83）：模型能写出 / 改出音效，却听不到结果，
只能保证「频谱结构对」，答不了「混响够不够厚」「能量紧不紧」。
本模块**不猜听感**，只做两件事：

1. 确定性声学测量（响度包络、起音、RT60、频谱形状、事件密度…），
   每一项都能用公式复算，换个文件也不会飘；
2. 把「冲击 / 压迫 / 空间 / 清晰」四个主观词拆成**有公式的代理分数**，
   只用于同一族音效之间的横向比较 —— 报告里会明确写「不等于听感」。

「好不好听」仍然只有人耳能判：音频大模型当裁判有已被证实的响度偏好、
长度偏好与位置偏好（见 NOTE 43.83 引用的审计论文），所以这里不接任何
音频大模型，也不引入新的 API key。
"""
import os

import numpy as np

MAX_SECONDS = 120.0            # 只分析前 120 秒：一个音效 / 一小段 BGM 足够
MAX_BYTES = 80 * 1024 * 1024   # 再大就不是「听一下」的场景了
ENV_WIN = 0.02                 # 包络帧长 20ms：能看清起音，又不会被噪声抖动带偏
NFFT = 2048
MAX_SPEC_FRAMES = 6000         # 频谱分析最多取这么多帧（长音频等间隔抽样）
CURVE_WIN = 0.5                # 压力曲线的时间分辨率（秒）
# 不使用空格当最低档：空格在文本里看不见，会让曲线看起来凭空少几格
BARS = "▁▂▃▄▅▆▇█"
TONAL_MIN = 0.35               # 自相关峰低于它就不算「有明确音高」
EPS = 1e-12

PROXY_FORMULAS = (
    "代理公式（0–1，只用于同族音效之间的比较，不代表听感）：\n"
    "  冲击 = 0.45·低频占比 + 0.35·起音陡度 + 0.20·峰值因数\n"
    "  压迫 = 0.40·事件密度 + 0.35·(1−动态范围) + 0.25·低频占比\n"
    "  空间 = 0.50·RT60 + 0.30·250ms 后能量占比 + 0.20·立体声宽度\n"
    "  清晰 = 0.55·频谱质心 + 0.45·(1−频谱平坦度)"
)


# ============================================================
# 1. 读取
# ============================================================

def _load(path):
    """读音频 → (data[帧, 声道] float32, sr, meta dict, 错误文本)。

    soundfile 延迟导入：没装时给一句能照做的提示，而不是让整个工具炸掉。
    """
    if not path:
        return None, 0, None, "❌ 未指定音频路径"
    if not os.path.isfile(path):
        return None, 0, None, "❌ 文件不存在：%s" % path
    size = os.path.getsize(path)
    if size <= 0:
        return None, 0, None, "❌ 文件是空的：%s" % os.path.basename(path)
    if size > MAX_BYTES:
        return None, 0, None, ("❌ 文件太大（%.1fMB），音频自检只处理 %dMB 以内"
                               % (size / 1e6, MAX_BYTES // 1024 // 1024))
    try:
        import soundfile as sf
    except ImportError:
        return None, 0, None, "⚠️ 未安装 soundfile，无法读取音频：pip install soundfile"
    try:
        info = sf.info(path)
        frames = int(min(info.frames, MAX_SECONDS * info.samplerate))
        data, sr = sf.read(path, frames=frames, dtype="float32", always_2d=True)
    except Exception as e:
        return None, 0, None, "❌ 读取音频失败：%s" % e
    if data.size == 0 or sr <= 0:
        return None, 0, None, "❌ 音频没有样本"
    meta = {
        "path": path,
        "name": os.path.basename(path),
        "format": (info.format or "").upper(),
        "subtype": info.subtype or "",
        "sr": int(sr),
        "channels": int(data.shape[1]),
        "seconds": round(float(len(data) / float(sr)), 3),
        "truncated": bool(info.frames > frames),
    }
    return data, int(sr), meta, ""


def _mono(data):
    """多声道下混成单声道（取平均，避免左右相消时把能量算丢）"""
    if data.shape[1] == 1:
        return data[:, 0].astype(np.float64)
    return data.mean(axis=1).astype(np.float64)


# ============================================================
# 2. 基础测量
# ============================================================

def _frames(x, win, hop):
    """切成重叠帧；样本不足一帧时补零到一帧。"""
    if len(x) < win:
        pad = np.zeros(win - len(x), dtype=x.dtype)
        return np.concatenate([x, pad])[None, :]
    n = 1 + (len(x) - win) // hop
    idx = np.arange(win)[None, :] + hop * np.arange(n)[:, None]
    return x[idx]


def _db(v):
    """线性幅度 → dBFS；0 给 -180（不返回 -inf，后面还要算差值）"""
    return 20.0 * np.log10(np.maximum(v, 1e-9))


def _envelope(x, sr):
    """20ms 短时 RMS 包络 + 每帧中心时间。"""
    win = max(8, int(sr * ENV_WIN))
    hop = max(1, win // 2)
    fr = _frames(x, win, hop)
    env = np.sqrt(np.mean(fr ** 2, axis=1))
    times = (np.arange(len(env)) * hop + win / 2.0) / float(sr)
    return env, times, hop


def _first_below(arr, thr):
    """第一个 <= thr 的下标；没有就返回 -1。"""
    hit = np.nonzero(arr <= thr)[0]
    return int(hit[0]) if len(hit) else -1


def _first_above(arr, thr):
    hit = np.nonzero(arr >= thr)[0]
    return int(hit[0]) if len(hit) else -1


def _rt60(x, sr, peak_idx):
    """Schroeder 反向积分估 RT60（秒）。

    只在「衰减到 −30dB 之后仍有样本」且 −5→−25dB 段近似线性时给值，
    否则返回 None —— 硬编一个数字比不给更糟（短促点击本来就没有混响）。
    """
    seg = x[peak_idx:]
    if len(seg) < int(sr * 0.08):
        return None
    e = seg ** 2
    rev = np.cumsum(e[::-1])[::-1]
    if rev[0] <= 0:
        return None
    db = 10.0 * np.log10(rev / rev[0] + 1e-20)
    if db[-1] > -30.0:
        return None                    # 文件在衰减完成前就结束了
    i5 = _first_below(db, -5.0)
    i25 = _first_below(db, -25.0)
    if i5 < 0 or i25 <= i5:
        return None
    slope = (db[i25] - db[i5]) / ((i25 - i5) / float(sr))
    if slope >= -1e-6:
        return None
    val = -60.0 / slope
    return float(val) if 0.01 <= val <= 20.0 else None


def _spectrum(x, sr):
    """频谱形状：质心、85% 滚降、平坦度、通量、四段频带能量占比。"""
    nfft = NFFT
    while nfft > len(x) and nfft > 256:
        nfft //= 2
    if nfft < 64:
        return {}
    fr = _frames(x, nfft, nfft // 2)
    if fr.shape[1] < nfft:
        fr = np.hstack([fr, np.zeros((fr.shape[0], nfft - fr.shape[1]), dtype=fr.dtype)])
    if fr.shape[0] > MAX_SPEC_FRAMES:
        pick = np.linspace(0, fr.shape[0] - 1, MAX_SPEC_FRAMES).astype(int)
        fr = fr[pick]
    win = np.hanning(nfft)
    mag = np.abs(np.fft.rfft(fr * win, axis=1)) + 1e-9
    freqs = np.fft.rfftfreq(nfft, 1.0 / float(sr))
    power = mag ** 2

    # 质心按**能量**加权：按幅度加权会被宽带底噪拉高（1.2kHz 的 ping 会算出 5kHz）
    total_p0 = float(power.sum())
    centroid = float((freqs[None, :] * power).sum() / total_p0) if total_p0 > 0 else 0.0

    # 85% 滚降：逐帧累积能量，再对帧取平均
    cum = np.cumsum(power, axis=1)
    last = cum[:, -1:]
    ratio = cum / np.maximum(last, EPS)
    roll_idx = np.argmax(ratio >= 0.85, axis=1)
    rolloff = float(np.mean(freqs[roll_idx]))

    flat = np.exp(np.mean(np.log(power + 1e-20), axis=1)) / np.maximum(
        np.mean(power, axis=1), 1e-20)
    flatness = float(np.mean(flat))

    norm = mag / np.maximum(mag.sum(axis=1, keepdims=True), EPS)
    flux = float(np.mean(np.sum(np.maximum(np.diff(norm, axis=0), 0.0), axis=1))) \
        if norm.shape[0] > 1 else 0.0

    band = 0.0
    edges = [(0, 200), (200, 1000), (1000, 4000), (4000, sr / 2.0 + 1)]
    total_p = float(power.sum())
    for lo, hi in edges:
        sel = (freqs >= lo) & (freqs < hi)
        band += float(power[:, sel].sum()) if sel.any() else 0.0
    ratios = []
    for lo, hi in edges:
        sel = (freqs >= lo) & (freqs < hi)
        v = float(power[:, sel].sum()) if sel.any() else 0.0
        ratios.append(v / total_p if total_p > 0 else 0.0)

    return {"centroid": centroid, "rolloff": rolloff, "flatness": flatness,
            "flux": flux, "bands": ratios, "total_power": band}


def _tonal(x, sr):
    """自相关找基频：判断「音调性」还是「噪声性」。"""
    n = min(len(x), int(sr * 1.0))
    if n < 256:
        return None
    # 取能量最大的 1 秒，而不是固定取开头（音效开头常有静音）
    if len(x) > n:
        step = max(1, n // 4)
        best_i, best_e = 0, -1.0
        for i in range(0, len(x) - n + 1, step):
            e = float(np.mean(x[i:i + n] ** 2))
            if e > best_e:
                best_i, best_e = i, e
        seg = x[best_i:best_i + n]
    else:
        seg = x
    seg = seg - float(seg.mean())
    if float(np.mean(seg ** 2)) <= 0:
        return None
    size = 1 << (2 * len(seg) - 1).bit_length()
    spec = np.fft.rfft(seg, size)
    ac = np.fft.irfft(spec * np.conj(spec), size)[:len(seg)]
    if ac[0] <= 0:
        return None
    ac = ac / ac[0]
    lag_min = max(2, int(sr / 2000.0))
    lag_max = min(len(seg) - 1, int(sr / 50.0))
    if lag_max <= lag_min:
        return None
    k = lag_min + int(np.argmax(ac[lag_min:lag_max]))
    strength = float(ac[k])
    if strength < TONAL_MIN:
        return {"f0": 0.0, "strength": strength}
    return {"f0": float(sr) / k, "strength": strength}


def _events(env_db, hop, sr, peak_db):
    """过阈值上升沿计数 → 事件数 / 秒。30ms 内的抖动算同一个事件。"""
    if len(env_db) < 3:
        return 0
    thr = max(peak_db - 25.0, float(np.percentile(env_db, 70)))
    above = env_db > thr
    # 从第 0 帧就超过阈值也算一次事件：音效常常是「一上来就是峰值」，
    # 只数 False→True 的跳变会把开头那一下漏掉（否则 1 个 ping 会报 0 次/秒）
    rises, last_t = (1 if above[0] else 0), (0.0 if above[0] else -1e9)
    for i in range(1, len(above)):
        if above[i] and not above[i - 1]:
            t = i * hop / float(sr)
            if t - last_t >= 0.03:
                rises += 1
                last_t = t
    return rises


def _late_ratio(x, sr, peak_idx, start=0.25):
    """峰值之后 250ms 起的能量占整段尾部的比例 —— 比 RT60 更稳的「混响厚度」信号。

    RT60 是外推值，离散回声（延迟 60/120/240ms）几乎不改变它的斜率；
    而尾能量占比会直接随回声变厚，正好对应「混响加厚」这种改动。
    """
    seg = x[peak_idx:]
    n0 = int(start * sr)
    total = float(np.sum(seg ** 2))
    if total <= 0 or len(seg) <= n0:
        return 0.0
    return float(np.sum(seg[n0:] ** 2) / total)


def _stereo(data):
    """左右声道相关度与宽度；单声道返回 None。"""
    if data.shape[1] < 2:
        return None
    left = data[:, 0].astype(np.float64)
    right = data[:, 1].astype(np.float64)
    if left.std() < 1e-9 or right.std() < 1e-9:
        return {"corr": 1.0, "width": 0.0}
    corr = float(np.corrcoef(left, right)[0, 1])
    denom = float(np.sqrt(np.mean(left ** 2)) + np.sqrt(np.mean(right ** 2)) + EPS)
    width = float(np.sqrt(np.mean((left - right) ** 2)) / denom)
    return {"corr": corr, "width": width}


# ============================================================
# 3. 汇总
# ============================================================

def _norm(v, lo, hi):
    """线性映射到 0–1 并夹住（代理分数用的唯一缩放方式）"""
    if hi == lo:
        return 0.0
    return float(min(1.0, max(0.0, (float(v) - lo) / (hi - lo))))


def _proxies(f):
    """四类感受的代理分数。公式见 PROXY_FORMULAS，改动必须同步改文案。"""
    low = f["bands"][0] + f["bands"][1] * 0.3       # 低频占比（200Hz 以下权重最高）
    attack = _norm(f["attack_ms"], 120.0, 5.0)      # 越短越陡
    crest = _norm(f["crest_db"], 3.0, 18.0)
    impact = 0.45 * _norm(low, 0.05, 0.6) + 0.35 * attack + 0.20 * crest

    density = _norm(f["events_per_sec"], 0.5, 8.0)
    tight = 1.0 - _norm(f["dyn_range_db"], 3.0, 30.0)
    pressure = 0.40 * density + 0.35 * tight + 0.25 * _norm(low, 0.05, 0.5)

    tail_ratio = _norm(f["late_ratio"], 0.02, 0.6)
    width = _norm((f["stereo"] or {}).get("width", 0.0), 0.0, 0.6)
    space = 0.50 * _norm(f["rt60"] or 0.0, 0.05, 1.5) + 0.30 * tail_ratio + 0.20 * width

    clarity = 0.55 * _norm(f["centroid"], 500.0, 6000.0) + 0.45 * (1.0 - _norm(f["flatness"], 0.0, 0.5))
    return {"冲击": impact, "压迫": pressure, "空间": space, "清晰": clarity}


def _pressure_curve(env, sr, hop, low_ratio):
    """每 0.5 秒一个「压力」值 → 字符条。

    只用已算出的量：能量（相对峰值）、事件密度、低频占比。
    不引入新的主观判断，避免「曲线看起来紧张」被当成结论。
    """
    if len(env) == 0:
        return ""
    step = max(1, int(CURVE_WIN * sr / hop))
    peak = float(env.max()) or 1.0
    vals = []
    for i in range(0, len(env), step):
        seg = env[i:i + step]
        if len(seg) == 0:
            continue
        energy = float(np.sqrt(np.mean(seg ** 2))) / peak
        vals.append(0.65 * energy + 0.35 * low_ratio)
    if not vals:
        return ""
    hi = max(vals) or 1.0
    out = []
    for v in vals:
        k = int(round((v / hi) * (len(BARS) - 1)))
        out.append(BARS[max(0, min(len(BARS) - 1, k))])
    return "".join(out)


def _features(path):
    """完整分析。返回 (features dict, 错误文本)。"""
    data, sr, meta, err = _load(path)
    if err:
        return None, err
    x = _mono(data)
    seconds = float(len(x)) / float(sr)
    env, times, hop = _envelope(x, sr)
    env_db = _db(env)
    peak = float(np.max(np.abs(x)))
    peak_db = float(_db(peak))
    rms = float(np.sqrt(np.mean(x ** 2)))
    rms_db = float(_db(rms))

    silent = peak < 1e-6
    if silent:
        env_db = np.full_like(env_db, -180.0)

    p95, p50, p10 = (float(np.percentile(env_db, q)) for q in (95, 50, 10))
    crest_db = peak_db - rms_db

    idx_peak = int(np.argmax(env)) if len(env) else 0
    peak_time = float(times[idx_peak]) if len(times) else 0.0
    pk_env = float(env[idx_peak]) if len(env) else 0.0
    if pk_env > 0:
        head = env[:idx_peak + 1]
        i10 = _first_above(head, 0.1 * pk_env)
        i90 = _first_above(head, 0.9 * pk_env)
        attack_ms = max(0.0, (i90 - i10) * hop / float(sr) * 1000.0) if 0 <= i10 <= i90 else 0.0
    else:
        attack_ms = 0.0

    tail_s = 0.0
    tail_truncated = False
    if pk_env > 0:
        thr = float(_db(pk_env * 1e-3))         # 比峰值低 60dB
        above = np.nonzero(env_db > thr)[0]
        if len(above):
            tail_s = max(0.0, float(times[above[-1]] - times[idx_peak]))
            tail_truncated = bool(above[-1] >= len(env_db) - 1)

    peak_idx = int(peak_time * sr)
    rt60 = _rt60(x, sr, min(peak_idx, len(x) - 1)) if pk_env > 0 else None
    late = _late_ratio(x, sr, min(peak_idx, len(x) - 1)) if pk_env > 0 else 0.0
    spec = _spectrum(x, sr)
    ton = _tonal(x, sr)
    events = _events(env_db, hop, sr, peak_db) if not silent else 0

    clip_ratio = float(np.mean(np.abs(x) >= 0.999))
    silence_ratio = float(np.mean(env_db < -50.0)) if not silent else 1.0

    f = {
        "meta": meta,
        "seconds": round(seconds, 3),
        "peak_db": peak_db,
        "rms_db": rms_db,
        "crest_db": crest_db,
        "clip_ratio": clip_ratio,
        "dc": float(np.mean(x)),
        "p95": p95, "p50": p50, "p10": p10,
        "dyn_range_db": p95 - p10,
        "attack_ms": attack_ms,
        "peak_time": peak_time,
        "tail_s": tail_s,
        "tail_truncated": tail_truncated,
        "rt60": rt60,
        "late_ratio": late,
        "centroid": spec.get("centroid", 0.0),
        "rolloff": spec.get("rolloff", 0.0),
        "flatness": spec.get("flatness", 0.0),
        "flux": spec.get("flux", 0.0),
        "bands": spec.get("bands", [0.0, 0.0, 0.0, 0.0]),
        "events_per_sec": (events / seconds) if seconds > 0 else 0.0,
        "silence_ratio": silence_ratio,
        "tonal": ton or {},
        "stereo": _stereo(data),
        "silent": silent,
        "env_db": env_db,
        "hop": hop,
        "sr": sr,
    }
    f["proxies"] = _proxies(f)
    f["curve"] = _pressure_curve(env, sr, hop, f["bands"][0] if f["bands"] else 0.0)
    return f, ""


# ============================================================
# 4. 文本报告
# ============================================================

def _fmt(v, unit="", nd=2):
    if v is None:
        return "不适用"
    return ("%." + str(nd) + "f%s") % (v, unit)


def _render(f):
    m = f["meta"]
    ch = "单声道" if m["channels"] == 1 else ("立体声" if m["channels"] == 2 else "%d 声道" % m["channels"])
    lines = [
        "音频自检：%s" % m["name"],
        "格式：%s%s / %dHz / %s / %.2fs%s"
        % (m["format"] or "未知", (" " + m["subtype"]) if m["subtype"] else "",
           m["sr"], ch, f["seconds"], "（已截断到 %.0fs）" % MAX_SECONDS if m["truncated"] else ""),
    ]
    if f["silent"]:
        lines.append("电平：全程静音（峰值低于 −120dBFS）——没有任何可分析的内容")
        lines += ["", PROXY_FORMULAS, "",
                  "结论：这是静音文件。以上为确定性测量；听感仍需人耳确认。"]
        return "\n".join(lines)

    lines += [
        "电平：峰值 %.1f dBFS | RMS %.1f dBFS | 峰值因数 %.1f dB | 削波 %.3f%% | 直流 %+.4f"
        % (f["peak_db"], f["rms_db"], f["crest_db"], f["clip_ratio"] * 100, f["dc"]),
        "响度：p95 %.1f / p50 %.1f / p10 %.1f dBFS | 动态范围 %.1f dB"
        % (f["p95"], f["p50"], f["p10"], f["dyn_range_db"]),
        "包络：起音 %s | 峰值 @%.0fms | 尾部 %.2fs%s"
        % ("<%.0fms" % (ENV_WIN * 500) if f["attack_ms"] < 1 else "%.1fms" % f["attack_ms"],
           f["peak_time"] * 1000, f["tail_s"],
           "（结束时仍未衰减到 −60dB）" if f["tail_truncated"] else ""),
        "混响：RT60 %s（外推） | 250ms 后能量 %.0f%%"
        % (_fmt(f["rt60"], "s"), f["late_ratio"] * 100),
        "频谱：质心 %.0fHz | 85%%滚降 %.0fHz | 平坦度 %.2f | 通量 %.2f"
        % (f["centroid"], f["rolloff"], f["flatness"], f["flux"]),
        "频带：<200Hz %.0f%% | 200–1k %.0f%% | 1k–4k %.0f%% | >4k %.0f%%"
        % tuple(v * 100 for v in f["bands"]),
        "事件：%.1f 次/秒 | 静默占比 %.0f%%" % (f["events_per_sec"], f["silence_ratio"] * 100),
    ]
    ton = f["tonal"] or {}
    if ton:
        if ton.get("f0"):
            lines.append("音调性：有明确基频 ≈%.0fHz（自相关 %.2f）" % (ton["f0"], ton["strength"]))
        else:
            lines.append("音调性：无明确基频（噪声性，自相关 %.2f）" % ton.get("strength", 0.0))
    st = f["stereo"]
    lines.append("立体声：%s" % ("单声道" if not st else
                                "左右相关 %.2f | 宽度 %.2f" % (st["corr"], st["width"])))
    lines += [
        "代理分数：" + " | ".join("%s %.2f" % (k, v) for k, v in f["proxies"].items()),
        "压力曲线（每 %.1fs）：%s" % (CURVE_WIN, f["curve"] or "—"),
        "",
        PROXY_FORMULAS,
        "",
        "结论：以上是确定性测量；「好不好听 / 紧不紧张」需人耳确认，不要把代理分数当结论。",
    ]
    return "\n".join(lines)


def probe(path):
    """单文件体检，返回可直接回灌模型的文本。"""
    f, err = _features(path)
    return err if err else _render(f)


# ============================================================
# 5. A/B 对比
# ============================================================

def _shape_corr(a, b):
    """两条包络的相关系数（各自先减均值，抵消整体响度差）。"""
    n = 480
    xa = np.interp(np.linspace(0, 1, n), np.linspace(0, 1, len(a)), a)
    xb = np.interp(np.linspace(0, 1, n), np.linspace(0, 1, len(b)), b)
    xa = xa - xa.mean()
    xb = xb - xb.mean()
    den = float(np.linalg.norm(xa) * np.linalg.norm(xb))
    return float(xa @ xb / den) if den > 0 else 0.0


def _delta(label, va, vb, unit="", nd=2, typical=1.0):
    """一行差值；(文本, 归一化后的变化幅度)；任一侧缺失时返回 (None, 0)。"""
    if va is None or vb is None:
        return None, 0.0
    d = vb - va
    txt = "%s：%s → %s（%+.2f%s）" % (label, _fmt(va, unit, nd), _fmt(vb, unit, nd), d, unit)
    return txt, abs(d) / typical


def compare(path_a, path_b):
    """A/B 对比：先剔除整体响度差，再比形状与结构。"""
    fa, err = _features(path_a)
    if err:
        return err
    fb, err = _features(path_b)
    if err:
        return err
    if fa["silent"] or fb["silent"]:
        return "⚠️ 其中一个是静音文件，无法比较：%s / %s" % (
            "A 静音" if fa["silent"] else "A 正常", "B 静音" if fb["silent"] else "B 正常")

    d_rms = fb["rms_db"] - fa["rms_db"]
    corr = _shape_corr(fa["env_db"], fb["env_db"])

    # 每项的 typical 是「同类音效里算明显的改变量」，只用来挑出变化最大的几项
    rows = []
    for row in (
        _delta("时长", fa["seconds"], fb["seconds"], "s", typical=1.0),
        _delta("起音", fa["attack_ms"], fb["attack_ms"], "ms", 1, typical=50.0),
        _delta("动态范围", fa["dyn_range_db"], fb["dyn_range_db"], "dB", 1, typical=10.0),
        _delta("RT60", fa["rt60"], fb["rt60"], "s", typical=0.3),
        _delta("尾能量占比", fa["late_ratio"], fb["late_ratio"], "", typical=0.2),
        _delta("尾部", fa["tail_s"], fb["tail_s"], "s", typical=0.3),
        _delta("频谱质心", fa["centroid"], fb["centroid"], "Hz", 0, typical=1000.0),
        _delta("低频占比", fa["bands"][0], fb["bands"][0], "", 3, typical=0.2),
        _delta("事件密度", fa["events_per_sec"], fb["events_per_sec"], "/s", 1, typical=2.0),
        _delta("峰值因数", fa["crest_db"], fb["crest_db"], "dB", 1, typical=4.0),
    ):
        if row[0]:
            rows.append(row)

    ranked = sorted(rows, key=lambda r: r[1], reverse=True)
    # 阈值 0.02：浮点误差级别的差异不算「结构性变化」，否则纯变响也会被说成改了频谱
    top = [r[0].split("：")[0] for r in ranked[:2] if r[1] >= 0.02]

    px = " | ".join("%s %+.2f" % (k, fb["proxies"][k] - fa["proxies"][k])
                    for k in fa["proxies"])
    if top:
        concl = ("结论：变化最大的是 %s。以上是确定性测量；是否更好听仍需人耳确认。"
                 % "、".join(top))
    else:
        concl = ("结论：除整体响度外没有可测的结构性变化（形状相关 %.2f）。"
                 "以上是确定性测量；是否更好听仍需人耳确认。" % corr)
    out = [
        "A/B 对比（B 相对 A；形状比较前已抵消整体响度差）",
        "A：%s  %.2fs / %dHz" % (fa["meta"]["name"], fa["seconds"], fa["meta"]["sr"]),
        "B：%s  %.2fs / %dHz" % (fb["meta"]["name"], fb["seconds"], fb["meta"]["sr"]),
        "",
        "响度：A %.1f dBFS / B %.1f dBFS（B 响 %+.1f dB，已从形状比较中剔除）"
        % (fa["rms_db"], fb["rms_db"], d_rms),
        "包络形状相关：%.2f（1 = 完全一致；低于 0.8 说明节奏/包络本身变了）" % corr,
        "",
        "逐项差异：",
        *["- " + r[0] for r in rows],
        "",
        "代理差值：" + px,
        "",
        PROXY_FORMULAS,
        "",
        concl,
    ]
    return "\n".join(out)
