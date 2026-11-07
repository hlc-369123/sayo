"""录音器静音输入流自检：录到全零时必须能被发现并报警。"""
from __future__ import annotations

import pytest

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


def test_default_input_name_returns_nonempty_string():
    pytest.importorskip("sounddevice")
    name = Recorder.default_input_name()
    assert isinstance(name, str) and name
