"""实时浮层相关：频谱换算、增量修正与当前句提交。"""
from __future__ import annotations

import time
from types import SimpleNamespace

import numpy as np

from app.core.live import LiveTranscriber, spectrum_bands


class FakeASR:
    """模拟「后续音频让模型改判整句」：第一次只出前半，第二次出完整句。"""

    def __init__(self, texts):
        self.texts = texts
        self.calls = 0

    def transcribe_utterance(self, seg, hotwords=None):
        i = min(self.calls, len(self.texts) - 1)
        self.calls += 1
        return SimpleNamespace(text=self.texts[i])


def test_spectrum_bands_shape_and_range():
    rng = np.random.default_rng(0)
    bands = spectrum_bands(rng.uniform(-0.2, 0.2, 800).astype(np.float32))
    assert len(bands) == 28
    assert all(0.0 <= v <= 1.0 for v in bands)
    assert max(bands) > 0.1
    assert max(spectrum_bands(np.zeros(0, dtype=np.float32))) == 0.0


def test_following_speech_revises_earlier_text():
    texts: list[str] = []
    asr = FakeASR(["今天", "今天下午三点开会"])
    live = LiveTranscriber(asr, on_text=texts.append, interval_s=0.02, silence_s=3.0)
    audio = np.random.default_rng(1).uniform(-0.05, 0.05, 16000).astype(np.float32)
    live.start(lambda: audio)
    time.sleep(0.12)
    live.stop()
    assert "今天" in texts
    assert "今天下午三点开会" in texts, "后续音频应修正前面已显示的文字"
    assert texts.index("今天") < texts.index("今天下午三点开会")


def test_silence_commits_current_sentence():
    texts: list[str] = []
    committed: list[str] = []
    asr = FakeASR(["今天天气很好"])
    live = LiveTranscriber(asr, on_text=texts.append, on_sentence=committed.append,
                           interval_s=0.02, silence_s=0.5)
    speech = np.random.default_rng(2).uniform(-0.05, 0.05, 8000).astype(np.float32)
    audio = np.concatenate([speech, np.zeros(8000, dtype=np.float32)])
    live.start(lambda: audio)
    time.sleep(0.1)
    live.stop()
    assert committed, "尾部静音应提交当前句"
    assert texts[-1] == "", "提交后浮层清空，只显示下一句"


def test_stop_is_idempotent():
    live = LiveTranscriber(FakeASR(["x"]), on_text=lambda t: None)
    live.stop()
    assert not live.running


def test_live_engine_keeps_user_sensitivity():
    """实时预览必须与主引擎同样的灵敏度，否则轻声说话时预览窗口一直空白。"""
    from app.main import build_live_engine

    class FakeSettings:
        def __init__(self, data):
            self.data = data

        def get(self, key, default=None):
            return self.data.get(key, default)

    for level in ("standard", "high", "max"):
        eng = build_live_engine(FakeSettings({"sensitivity": level,
                                              "model": "sensevoice",
                                              "language": "auto"}))
        assert eng.sensitivity == level
        assert eng.settings["precision"] == "int8"
        assert eng.settings["vad_enabled"] is True
