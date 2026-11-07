"""GPU 后端选择、精度档位与二次复核的单元测试（不依赖真实 GPU 或模型文件）。"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from app.config import sensitivity_preset
from app.core import accel
from app.core.asr import ASREngine


def _patch_hw(monkeypatch, backend):
    monkeypatch.setattr(accel, "probe_hardware", lambda: (True, "测试 GPU", backend))


def test_force_cpu(monkeypatch):
    _patch_hw(monkeypatch, accel.BACKEND_COREML)
    b, _ = accel.choose_backend("cpu", "fp32", "high")
    assert b.key == accel.CPU


def test_force_gpu(monkeypatch):
    _patch_hw(monkeypatch, accel.BACKEND_COREML)
    b, _ = accel.choose_backend("gpu", "fp32", "high")
    assert b.key == accel.COREML and b.is_gpu


def test_gpu_unavailable_falls_back(monkeypatch):
    monkeypatch.setattr(accel, "probe_hardware", lambda: (False, "无设备", None))
    b, note = accel.choose_backend("gpu", "fp32", "high")
    assert b.key == accel.CPU and "回退" in note


def test_auto_keeps_cpu_for_coreml_small_model(monkeypatch):
    """本机实测：Core ML 对 batch=1 的 SenseVoice 无速度优势。"""
    _patch_hw(monkeypatch, accel.BACKEND_COREML)
    b, note = accel.choose_backend("auto", "fp32", "high")
    assert b.key == accel.CPU and "实测" in note


def test_auto_enables_cuda_for_fp32(monkeypatch):
    """CUDA 平台的高精度档应启用 GPU；标准 int8 档仍用 CPU。"""
    _patch_hw(monkeypatch, accel.BACKEND_CUDA)
    assert accel.choose_backend("auto", "fp32", "high")[0].key == accel.CUDA
    assert accel.choose_backend("auto", "int8", "standard")[0].key == accel.CPU


def test_sensitivity_presets_are_monotonic():
    std = sensitivity_preset("standard")
    high = sensitivity_preset("high")
    maximum = sensitivity_preset("max")
    assert std["vad_threshold"] > high["vad_threshold"] > maximum["vad_threshold"]
    assert std["vad_min_speech"] > high["vad_min_speech"] > maximum["vad_min_speech"]
    assert std["agc_target_peak"] < maximum["agc_target_peak"]


def test_precision_selection(tmp_path):
    sv = tmp_path / "sensevoice"
    sv.mkdir()
    (sv / "model.onnx").write_bytes(b"fp32")
    (sv / "model.int8.onnx").write_bytes(b"int8")
    (sv / "tokens.txt").write_text("x 0\n")

    e = ASREngine(tmp_path, {"model": "sensevoice", "precision": "fp32"})
    assert e.model_paths()["sensevoice_model"].name == "model.onnx"
    assert e.precision_actual == "fp32"

    e = ASREngine(tmp_path, {"model": "sensevoice", "precision": "int8"})
    assert e.model_paths()["sensevoice_model"].name == "model.int8.onnx"
    assert e.precision_actual == "int8"


def test_missing_fp32_falls_back_to_int8(tmp_path):
    sv = tmp_path / "sensevoice"
    sv.mkdir()
    (sv / "model.int8.onnx").write_bytes(b"int8")
    (sv / "tokens.txt").write_text("x 0\n")
    e = ASREngine(tmp_path, {"model": "sensevoice", "precision": "fp32"})
    assert e.model_paths()["sensevoice_model"].name == "model.int8.onnx"
    ok, _ = e.check_models()
    assert ok


def test_gpu_load_failure_falls_back_to_cpu(monkeypatch, tmp_path):
    sv = tmp_path / "sensevoice"
    sv.mkdir()
    (sv / "model.onnx").write_bytes(b"fp32")
    (sv / "tokens.txt").write_text("x 0\n")
    _patch_hw(monkeypatch, accel.BACKEND_CUDA)
    e = ASREngine(tmp_path, {"model": "sensevoice", "precision": "fp32",
                             "accelerator": "gpu"})
    calls = []

    def fake_create(provider):
        calls.append(provider)
        if provider == accel.CUDA:
            raise RuntimeError("CUDA EP not compiled")
        return object()

    monkeypatch.setattr(e, "_create_recognizer", fake_create)
    e.ensure_loaded()
    assert calls == [accel.CUDA, accel.CPU]
    assert e.gpu_fallback and e.backend.key == accel.CPU
    assert e._recognizer is not None


def _force_speech_gate(monkeypatch):
    """这两个用例只验证二次复核逻辑，音频门控另有 test_audio_gate 覆盖。"""
    from app.core import asr as asr_mod
    from app.core.audio_gate import AudioAnalysis

    def fake(samples, *, sensitivity="high", target_peak=0.85, max_gain=3.0,
             force_speech=False):
        info = AudioAnalysis(True, 0.003, 0.1, 0.03, 0.05, 6.0, 0.5, "测试夹具：确认有语音")
        return np.asarray(samples, dtype=np.float32), info

    monkeypatch.setattr(asr_mod.audio_gate, "preprocess", fake)
    monkeypatch.setattr(
        asr_mod.ASREngine, "_speech_gate",
        lambda self, samples, min_vad_s: (False, AudioAnalysis(
            True, 0.003, 0.1, 0.03, 0.05, 6.0, 0.5, "测试夹具：确认有语音")))


def test_quiet_segment_recheck_recovers_lost_text(monkeypatch, tmp_path):
    """高灵敏档下，低电平片段首次解码为空时，二次解码结果应被采用。"""
    e = ASREngine(Path(tmp_path), {
        "model": "sensevoice", "precision": "int8", "sensitivity": "high",
        "gpu_recheck": True, "vad_enabled": False,
    })
    _force_speech_gate(monkeypatch)
    monkeypatch.setattr(e, "ensure_loaded", lambda: None)
    e.precision_actual = "int8"
    e._recognizer = object()
    quiet = (np.random.default_rng(1).uniform(-0.01, 0.01, 16000 * 2)).astype(np.float32)
    monkeypatch.setattr(e, "split", lambda samples, use_vad=True, noise_floor=0.0: [samples])
    responses = iter(["", "今天下午三点开会"])

    def fake_decode(chunk, hotwords):
        return next(responses)

    monkeypatch.setattr(e, "_decode", fake_decode)
    r = e.transcribe(quiet, use_vad=False)
    assert r.rechecked == 1
    assert r.text == "今天下午三点开会"


def test_standard_mode_never_rechecks(monkeypatch, tmp_path):
    e = ASREngine(Path(tmp_path), {
        "model": "sensevoice", "precision": "int8", "sensitivity": "standard",
        "gpu_recheck": True, "vad_enabled": False,
    })
    _force_speech_gate(monkeypatch)
    monkeypatch.setattr(e, "ensure_loaded", lambda: None)
    e._recognizer = object()
    monkeypatch.setattr(e, "split", lambda samples, use_vad=True, noise_floor=0.0: [samples])
    calls = []

    def fake_decode(chunk, hotwords):
        calls.append(1)
        return "正常的一句话"

    monkeypatch.setattr(e, "_decode", fake_decode)
    quiet = np.zeros(16000 * 2, dtype=np.float32)
    r = e.transcribe(quiet, use_vad=False)
    assert r.rechecked == 0 and len(calls) == 1
