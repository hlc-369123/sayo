"""音频前端门控：纯底噪不得送进模型，轻声/带噪语音必须保留。"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from app.core import audio_gate
from app.core.asr import strip_tags
from app.core.recorder import read_wav

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "zh_sample.wav"
SAMPLE_RATE = 16000


def _noise(seed: int = 3, seconds: float = 3.0) -> np.ndarray:
    """模拟带缓慢起伏的环境底噪（比白噪声更容易骗过简单能量门控）。"""
    rng = np.random.default_rng(seed)
    n = int(SAMPLE_RATE * seconds)
    x = rng.normal(0, 0.005, n).astype(np.float32)
    env = 0.7 + 0.3 * np.sin(np.linspace(0, 9, n))
    return (x * env).astype(np.float32)


def test_silence_and_steady_noise_are_rejected():
    assert not audio_gate.analyze(np.zeros(SAMPLE_RATE * 2)).has_speech
    assert not audio_gate.analyze(_noise()).has_speech
    assert not audio_gate.analyze(_noise() * 17).has_speech  # 放大不改变动态范围


def test_speech_is_accepted_even_when_quiet_or_noisy():
    voice = read_wav(FIXTURE)
    assert audio_gate.analyze(voice).has_speech
    assert audio_gate.analyze(voice * 0.1).has_speech
    assert audio_gate.analyze(voice * 0.03).has_speech  # 极轻声
    noisy = (voice * 0.4 + np.resize(_noise(), voice.size)).astype(np.float32)
    assert audio_gate.analyze(noisy).has_speech


def test_agc_limited_and_never_amplifies_noise():
    noise = _noise()
    assert np.allclose(audio_gate.apply_agc(noise, has_speech=False), noise)
    quiet = read_wav(FIXTURE) * 0.03
    out = audio_gate.apply_agc(quiet, has_speech=True, target_peak=0.85, max_gain=3.0)
    assert float(np.max(np.abs(out))) <= float(np.max(np.abs(quiet))) * 3.0 + 1e-6
    assert float(np.max(np.abs(out))) <= 1.0


def test_preprocess_keeps_shape_and_finite():
    voice = read_wav(FIXTURE)
    out, info = audio_gate.preprocess(voice)
    assert out.shape == voice.shape
    assert np.isfinite(out).all()
    assert info.has_speech
    # 高通是向量化的，长录音也不应明显变慢（这里只验证不报错、长度正确）
    long = np.resize(voice, SAMPLE_RATE * 20)
    out2, _ = audio_gate.preprocess(long)
    assert out2.shape == long.shape


def test_stray_kana_hallucination_is_stripped():
    assert strip_tags("<|zh|>嗯，ん，知道") == "嗯，，知道"
    assert strip_tags("<|ja|>こんにちは") == "こんにちは"  # 成串假名保留
    assert strip_tags("<|zh|><|NEUTRAL|><|Speech|>今天天气很好") == "今天天气很好"


def test_asr_returns_empty_for_noise_not_hallucination():
    """回归用户实测问题：底噪曾被识别成「又中八…ん，知道」这类乱码。"""
    from app.config import DEFAULT_SETTINGS, sensitivity_preset
    from app.core.asr import ASREngine

    s = dict(DEFAULT_SETTINGS)
    s.update(sensitivity_preset("high"))
    engine = ASREngine(Path(__file__).resolve().parents[1] / "models", s)
    r = engine.transcribe(_noise(seed=7))
    assert r.text == ""
    assert r.warnings


def test_asr_still_transcribes_real_speech():
    from app.config import DEFAULT_SETTINGS, sensitivity_preset
    from app.core.asr import ASREngine

    s = dict(DEFAULT_SETTINGS)
    s.update(sensitivity_preset("high"))
    engine = ASREngine(Path(__file__).resolve().parents[1] / "models", s)
    r = engine.transcribe(read_wav(FIXTURE))
    assert r.text
    assert "天气" in r.text or "开会" in r.text
