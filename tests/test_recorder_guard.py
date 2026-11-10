"""录音器输入流自检与设备自动回退：断流设备不能让听写静默失效。"""
from __future__ import annotations

import sys
import types

import pytest

from app.core import recorder as recorder_mod
from app.core.recorder import Recorder


def test_looks_silent_thresholds():
    # 没收到任何音频块（流没跑起来）
    assert Recorder._looks_silent(0, 0.0)
    # 块太少（流刚起或挂起）
    assert Recorder._looks_silent(3, 0.01)
    # 收到块但全零：强杀重启后 CoreAudio 流假死的典型表现
    assert Recorder._looks_silent(40, 0.0)
    # 有底噪 = 流是活的（真实麦克风安静时 rms ≈ 0.001~0.02）
    assert not Recorder._looks_silent(40, 0.0005)
    assert not Recorder._looks_silent(40, 0.02)


def test_looks_silent_detects_intermittent_stream():
    """蓝牙断流：零星爆音把 max_rms 抬到 0.13，旧逻辑会误判成「有声音」。

    实测 Ear Joy（蓝牙默认输入）35 块里 9 块精确为 0、25 块近零、
    只有 1 块 0.1379 —— 峰值 0.86、p90≈0.0002，人声根本没进流。
    """
    broken = [0.0] * 9 + [0.0001] * 25 + [0.1379]
    assert Recorder._looks_silent(35, max(broken), broken)
    healthy = [0.002, 0.006, 0.011, 0.0038, 0.005] * 8
    assert not Recorder._looks_silent(40, max(healthy), healthy)


def test_default_input_name_returns_nonempty_string():
    pytest.importorskip("sounddevice")
    name = Recorder.default_input_name()
    assert isinstance(name, str) and name


def _fake_stream_with_amps(monkeypatch, amps):
    """让 probe_input_device 收到构造好的波形，不碰真实硬件。"""
    import numpy as np

    fake = _install_fake_sounddevice(monkeypatch)

    class FakeStream:
        device = 0

        def __init__(self, **kwargs):
            self.callback = kwargs["callback"]
            self.blocksize = kwargs["blocksize"]

        def start(self):
            for amp in amps:
                block = np.full((self.blocksize, 1), amp, dtype=np.float32)
                self.callback(block, self.blocksize, None, None)

        def stop(self):
            pass

        def close(self):
            pass

    fake.InputStream = FakeStream
    monkeypatch.setattr(recorder_mod.time, "sleep", lambda _s: None)


def test_probe_rejects_intermittent_bluetooth_stream(monkeypatch):
    _fake_stream_with_amps(monkeypatch, [0.0] * 9 + [0.0001] * 25 + [0.1379])
    assert recorder_mod.probe_input_device(0) == 0.0


def test_probe_accepts_real_mic_with_startup_transient(monkeypatch):
    # 开流瞬间有两块全零是正常现象，不能因此把内置麦克风判成坏设备
    _fake_stream_with_amps(monkeypatch,
                           [0.0094, 0.0, 0.0, 0.0016, 0.006, 0.0044, 0.0046, 0.0114])
    score = recorder_mod.probe_input_device(0)
    assert score is not None and score >= 0.6


# ------------------------------------------------------------- 设备自检 / 回退
DEVICES = [
    {"name": "Ear Joy", "max_input_channels": 1, "max_output_channels": 2,
     "default_samplerate": 16000.0},
    {"name": "MacBook Air扬声器", "max_input_channels": 0, "max_output_channels": 2,
     "default_samplerate": 44100.0},
    {"name": "MacBook Air麦克风", "max_input_channels": 1, "max_output_channels": 0,
     "default_samplerate": 44100.0},
    {"name": "Omi Recorder Audio Driver", "max_input_channels": 2,
     "max_output_channels": 2, "default_samplerate": 44100.0},
]


def _install_fake_sounddevice(monkeypatch, devices=DEVICES):
    """注入假的 sounddevice，测试不依赖本机音频硬件。"""

    def query_devices(*args, **kwargs):
        kind = kwargs.get("kind")
        if args and isinstance(args[0], str):
            kind = args[0]
        if "device" in kwargs:
            return devices[int(kwargs["device"])]
        if kind == "input":
            return next(d for d in devices if d["max_input_channels"] > 0)
        if kind == "output":
            return next(d for d in devices if d["max_output_channels"] > 0)
        return devices

    class FakeStream:
        device = 2

        def __init__(self, **kwargs):
            pass

        def start(self):
            pass

        def stop(self):
            pass

        def close(self):
            pass

    fake = types.SimpleNamespace(
        query_devices=query_devices,
        default=types.SimpleNamespace(device=(0, 1)),
        InputStream=FakeStream,
    )
    monkeypatch.setitem(sys.modules, "sounddevice", fake)
    return fake


def test_auto_input_device_skips_broken_default(monkeypatch):
    Recorder.invalidate_device_cache()
    _install_fake_sounddevice(monkeypatch)
    scores = {0: 0.03, 2: 1.0, 3: 0.0}
    monkeypatch.setattr(recorder_mod, "probe_input_device",
                        lambda idx, seconds=0.18: scores.get(idx, 0.0))
    idx, note = Recorder({}).auto_input_device()
    assert idx == 2
    assert "已自动切换" in note and "MacBook Air麦克风" in note


def test_auto_input_device_keeps_healthy_default(monkeypatch):
    Recorder.invalidate_device_cache()
    _install_fake_sounddevice(monkeypatch)
    monkeypatch.setattr(recorder_mod, "probe_input_device",
                        lambda idx, seconds=0.18: 1.0)
    idx, note = Recorder({}).auto_input_device()
    assert idx is None and note == ""


def test_auto_input_device_result_is_cached(monkeypatch):
    Recorder.invalidate_device_cache()
    _install_fake_sounddevice(monkeypatch)
    calls = []

    def probe(idx, seconds=0.18):
        calls.append(idx)
        return 1.0

    monkeypatch.setattr(recorder_mod, "probe_input_device", probe)
    rec = Recorder({})
    rec.auto_input_device()
    rec.auto_input_device()
    assert calls == [0]  # 第二次直接命中缓存，不会给录音引入探测延迟


def test_shares_output_device_true_for_bluetooth(monkeypatch):
    _install_fake_sounddevice(monkeypatch)
    assert Recorder.shares_output_device() is True


def test_start_honours_configured_input_device(monkeypatch):
    """设置里选的麦克风必须真正传给输入流（此前该配置从未被读取）。"""
    seen = []
    fake = _install_fake_sounddevice(monkeypatch)

    class FakeStream:
        device = 2

        def __init__(self, **kwargs):
            seen.append(kwargs.get("device"))

        def start(self):
            pass

        def stop(self):
            pass

        def close(self):
            pass

    fake.InputStream = FakeStream
    monkeypatch.setattr(Recorder, "_start_guard", lambda self: None)
    rec = Recorder({"input_device": 2})
    rec.start()
    assert seen == [2]
    rec.stop()
