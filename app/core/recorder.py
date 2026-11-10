"""本地麦克风采集：16 kHz / 单声道 / float32，内存处理，不落盘（除非用户开启留存）。

灵敏度设计：
  · 自适应噪声底 + 可配置 VAD 阈值（在 ASR 侧）
  · 温和增益由识别前端统一处理（先判断是否有语音，避免放大底噪）
  · 实时电平回调驱动悬浮指示条
  · 可配置「说完静音 N 秒自动结束」
"""
from __future__ import annotations

import threading
import time
import wave
from pathlib import Path

import numpy as np

from ..config import AUDIO_DIR, CHANNELS, SAMPLE_RATE

# ---- 输入设备健康探测 ----------------------------------------------------
# 真实麦克风即使环境安静，每个音频块也有连续底噪（rms≈0.001~0.02）；
# 断流的蓝牙链路 / 失效的虚拟声卡则是「大部分块精确为 0 + 零星爆音」。
# 后者 peak 很高、max_rms 也不小，单看电平完全测不出来，必须看有效块占比。
_PROBE_SECONDS = 0.35
_PROBE_FLOOR = 0.0005      # 录音中判定「这一块有声音」的最低电平
_PROBE_ALIVE = 0.0003      # 设备探测：真实麦克风底噪的下限
_HEALTHY_RATIO = 0.6       # 有效块占比 ≥ 此值才算可用输入设备
_CACHE_TTL = 120.0         # 探测结果缓存秒数；设备列表变化会立即失效
_PROBE_LOCK = threading.Lock()


def probe_input_device(index: int, seconds: float = _PROBE_SECONDS) -> float | None:
    """短录一段，返回输入设备的「有效音频块占比」；打不开返回 None。

    断流设备不是「全零」这么简单：实测蓝牙默认输入是「大部分块精确 0 +
    一个 0.13 的爆音」。只看最大电平会误判成有声音，所以这里看有效块占比，
    并对「几乎全静音 + 零星爆音」直接判 0。前两块是流启动瞬态，丢弃不统计，
    否则会把健康的内置麦克风误判为坏设备。

    只用于设备自检，不产生任何输出、不写盘、不联网。
    """
    try:
        import sounddevice as sd
        blocks: list[float] = []

        def cb(indata, frames, time_info, status):
            a = np.asarray(indata[:, 0], dtype=np.float32)
            blocks.append(float(np.sqrt(np.mean(a ** 2))) if a.size else 0.0)

        stream = sd.InputStream(
            samplerate=SAMPLE_RATE, channels=CHANNELS, dtype="float32",
            blocksize=int(SAMPLE_RATE * 0.05), device=index, callback=cb)
        stream.start()
        time.sleep(seconds)
        stream.stop()
        stream.close()
    except Exception:
        return None
    body = blocks[2:]  # 丢掉开流瞬态
    if len(body) < 3:
        return 0.0
    ratio = sum(1 for v in body if v > _PROBE_ALIVE) / len(body)
    if ratio < 0.35 and max(body) > 0.02:
        return 0.0  # 零星爆音，不是人声
    return ratio


class RecorderError(RuntimeError):
    pass


class Recorder:
    # 类级缓存：signature -> (探测时间, 应选设备索引或 None, 说明文案)
    _device_cache: dict[str, tuple[float, int | None, str]] = {}

    def __init__(self, settings: dict, on_level=None, on_auto_stop=None, on_audio=None,
                 on_silent_stream=None):
        self.settings = settings
        self.on_level = on_level
        self.on_auto_stop = on_auto_stop
        self.on_audio = on_audio  # (频谱频段, 电平, rms) -> 浮层第二行
        # 输入流假死报警：开始录音约 1.5s 后仍全是静音（强杀重启后 CoreAudio
        # 输入流挂起 / 蓝牙麦克风断开 / 系统输入音量为 0），必须主动告诉用户，
        # 否则表现为「收不到我说话」，无从排查。
        self.on_silent_stream = on_silent_stream
        self.device_name = "未知设备"
        self._chunks: list[np.ndarray] = []
        self._lock = threading.Lock()
        self._stream = None
        self._recording = False
        self._started_at = 0.0
        self._last_voice_at = 0.0
        self._noise_floor = 0.003
        self._calibrated = 0
        self._timer: threading.Timer | None = None
        self._block_count = 0
        self._input_max_rms = 0.0
        self._block_rms: list[float] = []
        self._silent_checked = False
        self.auto_device_note = ""  # 自动绕开坏设备时的说明，供状态栏/日志使用

    @property
    def recording(self) -> bool:
        return self._recording

    @property
    def duration(self) -> float:
        return time.time() - self._started_at if self._recording else 0.0

    @staticmethod
    def devices() -> list[dict]:
        try:
            import sounddevice as sd
            return [
                {"index": i, "name": d["name"], "channels": d["max_input_channels"]}
                for i, d in enumerate(sd.query_devices())
                if d.get("max_input_channels", 0) > 0
            ]
        except Exception as exc:
            raise RecorderError(f"无法枚举音频输入设备：{exc}") from exc

    @staticmethod
    def default_input_name() -> str:
        try:
            import sounddevice as sd
            info = sd.query_devices(kind="input")
            return str(info.get("name", "未知设备")) if info else "未知设备"
        except Exception:
            return "未知设备"

    @staticmethod
    def _looks_silent(block_count: int, max_rms: float,
                      block_rms: list[float] | None = None) -> bool:
        """输入流假死判定：没收到块、全零，或「断续流」。

        真实麦克风即使安静也有连续底噪（rms ≈ 0.001~0.02），静音流是精确
        0.0。但断流的蓝牙设备会偶发一个爆音把 max_rms 抬到 0.1 以上，单看
        最大电平会漏判：实测某蓝牙默认输入 35 块里 9 块精确为 0、其余近零、
        只有 1 块 0.13。因此同时看「有效块占比」，低于 25% 即判为假死。
        """
        if block_count < 5 or max_rms < 0.0002:
            return True
        if block_rms:
            live = sum(1 for v in block_rms if v > _PROBE_FLOOR)
            if live / max(1, len(block_rms)) < 0.25:
                return True
        return False

    # ---- 自动绕开断流输入设备 -------------------------------------------
    @staticmethod
    def _device_signature() -> str:
        """设备列表指纹：插拔/切换默认设备后缓存立即失效。"""
        try:
            import sounddevice as sd
            devs = [(i, str(d.get("name", "")), int(d.get("max_input_channels", 0)),
                     round(float(d.get("default_samplerate", 0))))
                    for i, d in enumerate(sd.query_devices())
                    if d.get("max_input_channels", 0) > 0]
            default_name = str(sd.query_devices(kind="input").get("name", ""))
            return repr((default_name, devs))
        except Exception:
            return "unknown"

    @staticmethod
    def _input_candidates(default_idx: int | None) -> list[tuple[int, str]]:
        """候选输入设备，内置麦克风优先（探测到健康设备即可提前结束）。"""
        try:
            import sounddevice as sd
        except Exception:
            return []
        out = []
        for i, d in enumerate(sd.query_devices()):
            if d.get("max_input_channels", 0) <= 0 or i == default_idx:
                continue
            name = str(d.get("name", ""))
            low = name.lower()
            builtin = any(k in low for k in
                          ("built-in", "内建", "内置", "麦克风", "microphone", "macbook"))
            out.append((0 if builtin else 1, i, name))
        out.sort(key=lambda t: (t[0], t[1]))
        return [(i, name) for _, i, name in out]

    @classmethod
    def invalidate_device_cache(cls) -> None:
        cls._device_cache.clear()

    @classmethod
    def shares_output_device(cls) -> bool:
        """输入与输出是同一个设备（蓝牙耳机典型特征）。

        对同一蓝牙设备设置输出 mute 会让它的输入流一起断掉，因此这种
        情况下不要为了「静音其他音频」去动输出设备。
        """
        try:
            import sounddevice as sd
            return (str(sd.query_devices(kind="input").get("name", "")) ==
                    str(sd.query_devices(kind="output").get("name", "")))
        except Exception:
            return False

    def auto_input_device(self) -> tuple[int | None, str]:
        """系统默认输入断流时，自动挑一个健康的输入设备。

        返回 (设备索引或 None=跟随系统默认, 说明文案)。探测结果按设备列表
        指纹缓存；预热线程正在探测时不阻塞录音，直接沿用旧结果。
        """
        sig = self._device_signature()
        cached = Recorder._device_cache.get(sig)
        if cached and time.time() - cached[0] < _CACHE_TTL:
            return cached[1], cached[2]
        if not _PROBE_LOCK.acquire(blocking=False):
            return (cached[1] if cached else None), (cached[2] if cached else "")
        try:
            import sounddevice as sd
            try:
                default_idx = int(sd.default.device[0])
                if default_idx < 0:
                    default_idx = None  # type: ignore[assignment]
            except Exception:
                default_idx = None
            default_name = str(sd.query_devices(kind="input").get("name", "默认输入"))

            score = (probe_input_device(default_idx)
                     if default_idx is not None else None)
            chosen: int | None = None
            note = ""
            if score is not None and score >= _HEALTHY_RATIO:
                pass  # 默认设备健康，继续跟随系统
            else:
                for idx, name in self._input_candidates(default_idx):
                    s = probe_input_device(idx)
                    if s is not None and s >= _HEALTHY_RATIO:
                        chosen, note = idx, (
                            f"系统默认输入「{default_name}」收不到声音，"
                            f"已自动切换到「{name}」")
                        break
            Recorder._device_cache[sig] = (time.time(), chosen, note)
            return chosen, note
        except Exception:
            Recorder._device_cache[sig] = (time.time(), None, "")
            return None, ""
        finally:
            _PROBE_LOCK.release()

    def start(self, device=None) -> None:
        if self._recording:
            return
        try:
            import sounddevice as sd
        except ImportError as exc:
            raise RecorderError("缺少 PortAudio 运行库，请重新安装完整安装包") from exc
        # 设置里显式选的麦克风优先（此前该配置从未被读取，是真实 bug）；
        # 未指定时先自检系统默认输入，断流就自动绕开，不让用户对着坏设备
        # 反复说话却一句都出不来。
        if device is None:
            device = self.settings.get("input_device")
        self.auto_device_note = ""
        if device is None:
            device, self.auto_device_note = self.auto_input_device()
        self._chunks = []
        self._started_at = time.time()
        self._last_voice_at = self._started_at
        self._noise_floor = 0.003
        self._calibrated = 0
        self._block_count = 0
        self._input_max_rms = 0.0
        self._block_rms = []
        self._silent_checked = False
        self.device_name = self.default_input_name()
        self._recording = True
        try:
            self._stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype="float32",
                blocksize=int(SAMPLE_RATE * 0.05),  # 50ms
                device=device,
                callback=self._callback,
            )
            self._stream.start()
        except Exception as exc:
            if device is not None:
                # 指定的输入设备不可用（被拔出/被占用）：自动回退系统默认，不中断听写
                try:
                    self._stream = sd.InputStream(
                        samplerate=SAMPLE_RATE, channels=CHANNELS, dtype="float32",
                        blocksize=int(SAMPLE_RATE * 0.05), device=None,
                        callback=self._callback)
                    self._stream.start()
                    device = None
                except Exception as exc2:
                    self._recording = False
                    raise RecorderError(
                        f"麦克风打开失败：{exc2}（请检查麦克风权限与输入设备）") from exc2
            else:
                self._recording = False
                raise RecorderError(
                    f"麦克风打开失败：{exc}（请检查麦克风权限与输入设备）") from exc
        # 显示实际打开的设备名（指定设备/回退后都可能不同于系统默认）
        try:
            info = sd.query_devices(device=self._stream.device)
            if info and info.get("name"):
                self.device_name = str(info["name"])
        except Exception:
            pass
        self._start_guard()

    def _callback(self, indata, frames, time_info, status) -> None:
        if not self._recording:
            return
        audio = np.asarray(indata[:, 0], dtype=np.float32).copy()
        rms = float(np.sqrt(np.mean(audio ** 2))) if audio.size else 0.0
        self._block_count += 1
        self._input_max_rms = max(self._input_max_rms, rms)
        self._block_rms.append(rms)
        # 前 300ms 估计环境噪声底，用于低音量/高灵敏度场景
        if self._calibrated < 6:
            self._noise_floor = 0.8 * self._noise_floor + 0.2 * max(rms, 0.0005)
            self._calibrated += 1
        with self._lock:
            self._chunks.append(audio)
        voice_level = max(self._noise_floor * 2.2, 0.004)
        if rms > voice_level:
            self._last_voice_at = time.time()
        level = min(1.0, rms * 12.0)
        if self.on_level:
            try:
                self.on_level(level, rms)
            except Exception:
                pass
        if self.on_audio:
            try:
                from .live import spectrum_bands
                self.on_audio((spectrum_bands(audio), level, rms))
            except Exception:
                pass

    def _start_guard(self) -> None:
        max_seconds = float(self.settings.get("max_record_seconds", 300))
        silence_limit = float(self.settings.get("auto_stop_silence_s", 0) or 0)

        def guard():
            if not self._recording:
                return
            elapsed = time.time() - self._started_at
            silent = time.time() - self._last_voice_at
            # 静音流自检：开始约 1.5 秒后仍无任何非零样本，只报一次
            if not self._silent_checked and elapsed > 1.5:
                self._silent_checked = True
                if self._looks_silent(self._block_count, self._input_max_rms,
                                      list(self._block_rms)):
                    if self.on_silent_stream:
                        try:
                            self.on_silent_stream(self.device_name)
                        except Exception:
                            pass
                    return
            if elapsed >= max_seconds or (silence_limit > 0 and elapsed > 1.0 and silent >= silence_limit):
                if self.on_auto_stop:
                    self.on_auto_stop()
                return
            self._timer = threading.Timer(0.1, guard)
            self._timer.daemon = True
            self._timer.start()

        self._timer = threading.Timer(0.1, guard)
        self._timer.daemon = True
        self._timer.start()

    def snapshot(self) -> np.ndarray:
        """录音期间返回当前累积音频的拷贝，供实时预览重解码（不打断采集）。"""
        with self._lock:
            if not self._chunks:
                return np.zeros(0, dtype=np.float32)
            return np.concatenate(self._chunks).astype(np.float32, copy=False)

    def stop(self) -> np.ndarray:
        """停止录音并返回本机内存中的音频；返回后不再持有音频引用。"""
        self._recording = False
        if self._timer:
            self._timer.cancel()
            self._timer = None
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        with self._lock:
            audio = np.concatenate(self._chunks) if self._chunks else np.zeros(0, dtype=np.float32)
            self._chunks = []
        audio = np.ascontiguousarray(audio, dtype=np.float32)
        # 增益统一交给识别前端 audio_gate.preprocess：那里会先判断是否真有语音，
        # 且把增益限制在 3 倍以内。旧版在这里无条件把峰值拉到 0.85，
        # 纯底噪曾被放大 17 倍，直接导致模型编出乱码句子。
        # 保持原始波形也让实时预览与最终识别用的是同一份数据。
        return audio

    def save_wav(self, audio: np.ndarray) -> str:
        """可选：把本次录音留在用户本机（默认关闭）。"""
        AUDIO_DIR.mkdir(parents=True, exist_ok=True)
        path = AUDIO_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}.wav"
        pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
        with wave.open(str(path), "wb") as w:
            w.setnchannels(CHANNELS)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(pcm.tobytes())
        return str(path)


def read_wav(path: str | Path) -> np.ndarray:
    """命令行/测试用：读取 16k 单声道 wav。"""
    with wave.open(str(path), "rb") as w:
        rate = w.getframerate()
        raw = w.readframes(w.getnframes())
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if rate != SAMPLE_RATE:
        # 线性重采样，避免引入额外依赖
        idx = np.arange(0, len(audio), rate / SAMPLE_RATE)
        audio = np.interp(idx, np.arange(len(audio)), audio).astype(np.float32)
    return audio
