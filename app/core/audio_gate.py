"""录音前端：噪声底估计、语音有效性门控、温和增益（纯 numpy，本机运行）。

为什么需要这一层：SenseVoice 这类端到端模型在「只有底噪 / 静音」的输入上
不会安静地返回空串，而会自信地编出一句完整的话（常夹杂日文假名等乱 token）。
此外把底噪按峰值归一化放大十几倍，会让 VAD 把噪声误判成语音，进一步放大幻觉。

因此所有音频在送进模型之前，先在这里判断「是否真的有人在说话」。
判定同时看三条，避免误杀轻声/远场：
  1. 绝对电平：帧 RMS 的 90 分位要高于噪声底
  2. 动态范围：语音有明显的音节起伏，稳态噪声没有
  3. 有效时长：超过阈值的帧累计要够长
"""
from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np

from ..config import SAMPLE_RATE

FRAME = int(SAMPLE_RATE * 0.02)  # 20 ms = 320 samples


@dataclass
class AudioAnalysis:
    has_speech: bool
    noise_floor: float
    peak: float
    rms: float
    p90: float
    dynamic_range: float
    active_ratio: float
    reason: str


def frame_rms(samples: np.ndarray) -> np.ndarray:
    x = np.asarray(samples, dtype=np.float32).reshape(-1)
    if x.size == 0:
        return np.zeros(0, dtype=np.float32)
    n = (x.size // FRAME) * FRAME
    if n == 0:
        return np.array([float(np.sqrt(np.mean(x ** 2)))], dtype=np.float32)
    f = x[:n].reshape(-1, FRAME)
    return np.sqrt(np.mean(f ** 2, axis=1)).astype(np.float32)


def estimate_noise_floor(samples: np.ndarray) -> float:
    """用开头 300 ms 与全局低分位共同估计环境噪声 RMS。"""
    rms = frame_rms(samples)
    if rms.size == 0:
        return 0.002
    head = rms[:max(1, int(0.3 / 0.02))]
    return float(max(0.0008, min(np.percentile(head, 40), np.percentile(rms, 20))))


def analyze(samples: np.ndarray, *, sensitivity: str = "high") -> AudioAnalysis:
    """判断一段音频里是否有语音。sensitivity 越高，门槛越宽。"""
    x = np.asarray(samples, dtype=np.float32).reshape(-1)
    rms_all = float(np.sqrt(np.mean(x ** 2))) if x.size else 0.0
    peak = float(np.max(np.abs(x))) if x.size else 0.0
    frames = frame_rms(x)
    if frames.size == 0:
        return AudioAnalysis(False, 0.002, peak, 0, 0, 0, 0, "无音频数据")

    noise = estimate_noise_floor(x)
    p20 = float(np.percentile(frames, 20))
    p90 = float(np.percentile(frames, 90))
    dynamic = p90 / max(p20, 1e-6)

    # 高灵敏档降低绝对门槛与动态范围要求，但不会低到把稳态底噪当成语音。
    # 参数来自本机实测：MacBook Air 环境底噪 p90≈0.0064、动态范围≈2.3，
    # 而极轻声语音动态范围≈10 —— 靠「动态范围 + 相对噪声底电平」区分二者。
    level_factor = {"standard": 2.4, "high": 2.2, "max": 1.9}.get(sensitivity, 2.2)
    dyn_min = {"standard": 3.0, "high": 2.8, "max": 2.5}.get(sensitivity, 2.8)
    min_ratio = {"standard": 0.09, "high": 0.06, "max": 0.05}.get(sensitivity, 0.06)
    min_active_s = {"standard": 0.25, "high": 0.20, "max": 0.15}.get(sensitivity, 0.20)

    gate = max(noise * level_factor, p20 * 1.6, 0.004)
    active = frames > gate
    active_ratio = float(np.mean(active))
    active_s = active_ratio * frames.size * 0.02

    level_ok = p90 >= gate
    dyn_ok = dynamic >= dyn_min
    ratio_ok = active_ratio >= min_ratio and active_s >= min_active_s
    has = bool(level_ok and dyn_ok and ratio_ok)

    if not level_ok:
        reason = f"电平过低（p90={p90:.5f} < 门槛 {gate:.5f}）"
    elif not dyn_ok:
        reason = f"稳态噪声（动态范围 {dynamic:.2f} < {dyn_min}）"
    elif not ratio_ok:
        reason = f"有效语音过短（{active_s:.2f}s / 占比 {active_ratio:.2f}）"
    else:
        reason = "检测到语音"
    return AudioAnalysis(has, noise, peak, rms_all, p90, dynamic, active_ratio, reason)


def highpass(samples: np.ndarray, cutoff_hz: float = 100.0) -> np.ndarray:
    """向量化高通：减去因果移动平均，去掉风扇/空调等低频隆隆声。

    用累积和把复杂度压到 O(n)，5 分钟长录音也不会卡住识别线程。
    """
    x = np.asarray(samples, dtype=np.float32).reshape(-1)
    if x.size < 8:
        return x.copy()
    k = max(2, min(int(SAMPLE_RATE / cutoff_hz), x.size // 4))
    pad = np.pad(x, (k, 0), mode="edge").astype(np.float64)
    cs = np.cumsum(np.concatenate(([0.0], pad)))
    low = (cs[k:k + x.size] - cs[:x.size]) / k
    return (x.astype(np.float64) - low).astype(np.float32)


def soft_noise_gate(samples: np.ndarray, noise_floor: float,
                    floor_gain: float = 0.16) -> np.ndarray:
    """软噪声门：安静帧只衰减不抹掉，避免语音起止处出现硬切造成吞字。"""
    x = np.asarray(samples, dtype=np.float32).reshape(-1)
    if x.size < FRAME:
        return x.copy()
    rms = frame_rms(x)
    gate = max(noise_floor * 1.5, 0.003)
    env = np.clip((rms - gate * 0.6) / max(gate * 1.4, 1e-6), 0.0, 1.0)
    gain = floor_gain + (1.0 - floor_gain) * env
    # 3 帧平滑，防止增益跳变引入咔哒声
    kernel = np.ones(3, dtype=np.float32) / 3.0
    gain = np.convolve(np.pad(gain, (1, 1), mode="edge"), kernel, mode="valid")
    repeated = np.repeat(gain, FRAME)[:x.size]
    if repeated.size < x.size:
        repeated = np.pad(repeated, (0, x.size - repeated.size), mode="edge")
    return (x * repeated).astype(np.float32)


def apply_agc(samples: np.ndarray, *, has_speech: bool,
              target_peak: float = 0.85, max_gain: float = 3.0) -> np.ndarray:
    """温和增益：只在确认有语音时放大，且限制最大倍数。

    旧实现无条件把 peak 拉到 0.85，纯底噪曾被放大 17 倍，
    是模型产生幻觉文字的直接原因。
    """
    x = np.asarray(samples, dtype=np.float32).reshape(-1)
    if x.size == 0 or not has_speech:
        return x.copy()
    peak = float(np.max(np.abs(x)))
    if peak <= 0:
        return x.copy()
    gain = min(max_gain, max(1.0, target_peak / peak))
    return np.clip(x * gain, -1.0, 1.0).astype(np.float32)


def preprocess(samples: np.ndarray, *, sensitivity: str = "high",
               target_peak: float = 0.85, max_gain: float = 3.0,
               force_speech: bool = False) -> tuple[np.ndarray, AudioAnalysis]:
    """识别前统一预处理：高通 → 语音判定 → 软噪声门 → 温和增益。

    force_speech 由 ASR 侧的 Silero VAD 兜底置位：低信噪比的轻声说话
    能量特征接近稳态噪声，但 VAD 能听到连续语音，此时允许放大增益。
    """
    x = np.asarray(samples, dtype=np.float32).reshape(-1)
    info = analyze(x, sensitivity=sensitivity)
    effective = bool(info.has_speech or force_speech)
    reason = ("VAD 确认连续语音（低信噪比轻声）" if force_speech and not info.has_speech
              else info.reason)
    y = highpass(x)
    y = soft_noise_gate(y, info.noise_floor)
    y = apply_agc(y, has_speech=effective,
                  target_peak=target_peak, max_gain=max_gain)
    return y, replace(info, has_speech=effective, reason=reason)
