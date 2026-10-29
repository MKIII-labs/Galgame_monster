# -*- coding: utf-8 -*-
"""音频自检的单元测试：全部用合成信号，不依赖任何外部音频文件。

思路：先用「已知答案」的信号验证每一项测量（正弦 → 质心与基频；
脉冲 → 起音；指数衰减 → RT60；削波 → 削波比例），再验证 A/B 对比
确实先抵消了响度差（否则「只是变响」会被误读成「变好」）。
"""
import re

import numpy as np
import pytest

sf = pytest.importorskip("soundfile")

from app import audio  # noqa: E402

SR = 48000


def _write(tmp_path, x, name="a.wav", sr=SR, subtype="FLOAT"):
    p = tmp_path / name
    sf.write(str(p), np.asarray(x, dtype="float32"), sr, subtype=subtype)
    return str(p)


def _sine(freq=440.0, seconds=1.0, amp=0.5, sr=SR):
    t = np.arange(int(sr * seconds)) / float(sr)
    return amp * np.sin(2 * np.pi * freq * t)


def test_sine_centroid_and_tonal(tmp_path):
    f, err = audio._features(_write(tmp_path, _sine()))
    assert err == ""
    assert abs(f["centroid"] - 440.0) < 40.0
    assert f["tonal"]["f0"] == pytest.approx(440.0, rel=0.03)
    assert f["tonal"]["strength"] > 0.5
    assert f["clip_ratio"] == 0.0
    assert f["silent"] is False


def test_click_has_short_attack(tmp_path):
    x = np.zeros(SR)
    x[100] = 0.9
    x[101] = -0.7
    f, err = audio._features(_write(tmp_path, x))
    assert err == ""
    assert f["attack_ms"] < 10.0
    assert f["proxies"]["冲击"] > 0.3


def test_rt60_on_exponential_decay(tmp_path):
    """包络按 exp(-6.9t/T) 衰减时，理论 RT60 就是 T。"""
    t = np.arange(int(SR * 2.0)) / float(SR)
    rng = np.random.default_rng(0)
    x = rng.standard_normal(len(t)) * 0.5 * np.exp(-6.908 * t / 0.8)
    f, err = audio._features(_write(tmp_path, x, subtype="FLOAT"))
    assert err == ""
    assert f["rt60"] is not None
    assert 0.4 < f["rt60"] < 1.4


def test_clipping_detected(tmp_path):
    x = np.clip(1.6 * _sine(amp=1.0), -1.0, 1.0)
    f, _ = audio._features(_write(tmp_path, x))
    assert f["clip_ratio"] > 0.01


def test_silence_is_flagged(tmp_path):
    f, err = audio._features(_write(tmp_path, np.zeros(SR)))
    assert err == ""
    assert f["silent"] is True
    text = audio.probe(_write(tmp_path, np.zeros(SR), "b.wav"))
    assert "全程静音" in text


def test_probe_mentions_limits(tmp_path):
    text = audio.probe(_write(tmp_path, _sine()))
    assert "音频自检" in text
    assert "需人耳确认" in text
    assert "代理" in text


def test_missing_file_and_bad_format(tmp_path):
    assert "文件不存在" in audio.probe(str(tmp_path / "nope.wav"))
    bad = tmp_path / "x.wav"
    bad.write_bytes(b"not audio at all")
    assert audio.probe(str(bad)).startswith("❌")


def test_compare_cancels_loudness(tmp_path):
    a = _sine()
    pa = _write(tmp_path, a, "a.wav")
    pb = _write(tmp_path, a * 1.8, "b.wav")
    text = audio.compare(pa, pb)
    assert "已从形状比较中剔除" in text
    # 波形完全相同 → 包络形状相关应为 1.00，质心不变
    assert "包络形状相关：1.00" in text
    m = re.search(r"频谱质心：(\d+)Hz → (\d+)Hz", text)
    assert m and abs(int(m.group(1)) - int(m.group(2))) <= 2   # 只是变响，质心不动
    assert "没有可测的结构性变化" in text   # 只有响度变了，不该谎报结构变化


def test_compare_reports_structure_change(tmp_path):
    rng = np.random.default_rng(1)
    t = np.arange(SR) / float(SR)
    clean = np.sin(2 * np.pi * 440 * t) * 0.5
    noisy = clean + rng.standard_normal(len(t)) * 0.3
    pa = _write(tmp_path, clean, "clean.wav")
    pb = _write(tmp_path, noisy, "noisy.wav")
    text = audio.compare(pa, pb)
    assert "低频占比" in text
    assert "变化最大的是" in text


def test_compare_rejects_silence(tmp_path):
    pa = _write(tmp_path, _sine(), "a.wav")
    pb = _write(tmp_path, np.zeros(SR), "b.wav")
    assert "静音文件" in audio.compare(pa, pb)
