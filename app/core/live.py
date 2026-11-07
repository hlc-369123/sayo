"""录音期间的「当前句」实时预览与增量修正（全部本机推理）。

工作方式：每隔约 0.55 s 取一次已录音频快照，把「当前句起点 → 现在」的整段
重新解码一次。因为每次都重解码整句，后面说出来的字会改变模型对前面字的判定，
于是浮层上已经显示的文字会被实时修正——这正是参考工具的行为。

静音超过阈值时把当前句提交（浮层清空，只显示新的一句）；
录音结束时仍由主引擎用 fp32 全精度重新识别整段，预览不参与最终输出。
"""
from __future__ import annotations

import threading
from typing import Callable

import numpy as np

from ..config import SAMPLE_RATE


def spectrum_bands(audio: np.ndarray, count: int = 28) -> list[float]:
    """把一段波形换算成对数频段的能量（用于浮层下方的频率柱）。"""
    n = 1024
    x = np.asarray(audio, dtype=np.float32).reshape(-1)
    if x.size < n:
        x = np.pad(x, (n - x.size, 0))
    else:
        x = x[-n:]
    spec = np.abs(np.fft.rfft(x * np.hanning(n).astype(np.float32)))
    freqs = np.fft.rfftfreq(n, 1.0 / SAMPLE_RATE)
    edges = np.logspace(np.log10(80), np.log10(7600), count + 1)
    out: list[float] = []
    for i in range(count):
        mask = (freqs >= edges[i]) & (freqs < edges[i + 1])
        val = float(np.mean(spec[mask])) if mask.any() else 0.0
        out.append(val)
    arr = np.log1p(np.asarray(out) * 40.0)
    peak = float(arr.max()) if arr.size else 0.0
    if peak < 0.35:
        peak = 0.35
    return [round(min(1.0, float(v / peak)), 3) for v in arr]


class LiveTranscriber:
    def __init__(self, asr, on_text: Callable[[str], None],
                 on_sentence: Callable[[str], None] | None = None,
                 on_error: Callable[[str], None] | None = None,
                 *, interval_s: float = 0.55, min_speech_s: float = 0.35,
                 silence_s: float = 0.7, silence_rms: float = 0.009):
        self.asr = asr
        self.on_text = on_text
        self.on_sentence = on_sentence
        self.on_error = on_error
        self.interval = max(0.05, interval_s)
        self.min_samples = int(SAMPLE_RATE * min_speech_s)
        self.tail_samples = int(SAMPLE_RATE * silence_s)
        self.silence_rms = silence_rms
        self._snapshot: Callable[[], np.ndarray] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_text = ""

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, snapshot: Callable[[], np.ndarray]) -> None:
        if self.running:
            return
        self._snapshot = snapshot
        self._stop.clear()
        self.last_text = ""
        self._error_reported = False
        self._thread = threading.Thread(target=self._loop, daemon=True, name="live-asr")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.2)
            self._thread = None

    def _emit(self, text: str) -> None:
        if text == self.last_text:
            return
        self.last_text = text
        try:
            self.on_text(text)
        except Exception:
            pass

    def _loop(self) -> None:
        sentence_start = 0
        while not self._stop.wait(self.interval):
            if self._snapshot is None:
                continue
            try:
                audio = self._snapshot()
            except Exception:
                continue
            if audio is None or audio.size < self.min_samples:
                continue
            seg = audio[sentence_start:]
            if seg.size < self.min_samples:
                continue
            try:
                result = self.asr.transcribe_utterance(seg)
            except Exception as exc:
                # 旧实现在这里静默 continue：模型加载失败时实时文字永远为空，
                # 用户只看到「不显示识别的文字」却没有任何线索。现在上报一次。
                if self.on_error and not self._error_reported:
                    self._error_reported = True
                    try:
                        self.on_error(f"实时预览不可用：{type(exc).__name__}: {exc}")
                    except Exception:
                        pass
                continue
            self._emit(result.text.strip())
            # 尾部静音 → 当前句说完，清空浮层准备下一句
            tail = seg[-self.tail_samples:] if seg.size >= self.tail_samples else seg
            rms = float(np.sqrt(np.mean(tail ** 2))) if tail.size else 0.0
            if seg.size >= self.tail_samples and rms < self.silence_rms and result.text.strip():
                if self.on_sentence:
                    try:
                        self.on_sentence(result.text.strip())
                    except Exception:
                        pass
                sentence_start = audio.size
                self._emit("")
