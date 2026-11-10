"""本地语音识别引擎（sherpa-onnx / ONNX Runtime，本机 CPU 或 GPU 推理）。

主模型：SenseVoice-Small，随包提供 fp32 全精度与 int8 量化两份权重
        （中/英/粤/日/韩自动语种检测、自带标点与 ITN）
辅助模型：Silero VAD（端点检测，剔除静音、切分长语音）
可选模型：Whisper ONNX（构建时另行打包，用于 100+ 语种）

加速说明：GPU（macOS Core ML / NVIDIA CUDA）只提供算力，不改变同一权重的输出。
真正提升准确率的是 fp32 全精度 + 低电平片段二次复核，提升灵敏度的是 VAD 阈值/分段档位；
GPU 让这些更重的组合在长语音和大模型上保持实时。GPU 加载失败自动回退 CPU。

模型文件来自外部模型目录（首次使用时由独立脚本下载）或包内 models/ 兼容目录；
推理过程无任何网络 IO。
"""
from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np

from ..config import SAMPLE_RATE
from . import accel, audio_gate

TAG_RE = re.compile(r"<\|[^|]*?\|>")
CJK_RE = re.compile(r"[\u3400-\u9fff]")
# 噪声幻觉常表现为孤立的一两个日文假名 / 韩文字符（例如「ん，知道」）。
# 成串的假名（≥3 字）保留，那是用户真的在说日语/韩语。
KANA_RE = re.compile(r"[\u3040-\u30ff\uac00-\ud7af]+")

LANG_TAG_MAP = {
    "zh": "zh", "en": "en", "yue": "yue", "ja": "ja", "ko": "ko",
}


@dataclass
class ASRResult:
    text: str = ""
    language: str = "auto"
    segments: int = 0
    elapsed_ms: int = 0
    model: str = ""
    backend: str = "cpu"
    precision: str = "int8"
    rechecked: int = 0
    warnings: list[str] = field(default_factory=list)


def strip_tags(text: str) -> str:
    """去掉 SenseVoice 的标签，并清理噪声造成的孤立假名/韩文幻觉字符。"""
    out = TAG_RE.sub("", text or "").strip()
    out = KANA_RE.sub(lambda m: m.group(0) if len(m.group(0)) >= 3 else "", out)
    return re.sub(r"\s{2,}", " ", out).strip()


def detect_language(text: str) -> str:
    if not text:
        return "unknown"
    cjk = len(CJK_RE.findall(text))
    latin = len(re.findall(r"[A-Za-z]", text))
    if cjk == 0 and latin == 0:
        return "unknown"
    return "zh" if cjk / max(1, cjk + latin) > 0.12 else "en"


# 句末标点（含中英文），用于识别 SenseVoice 的「散点句号」
_PUNCT_RUN = re.compile(r"([。！？!?]+)")


def _strip_spurious_periods(text: str, min_periods: int = 4,
                            max_avg_frag: int = 10) -> str:
    """剥离 SenseVoice 在单切片内部撒的「散点句号」。

    SenseVoice 偶尔会对每个短短语单独加句号（「主要现在。做的。都是…」），
    早先的修复只处理了「多 VAD 切片边界」的句号，单切片内部的漏掉了。
    这里仅在「明显过度断句」时介入：内部句末标点 ≥ min_periods 个，且内部片段
    平均有效字数 < max_avg_frag —— 判定为模型过度断句，删掉这些内部标点，
    仅保留最后一次句末标点，交给后续规则 / 本地 LLM 重新断句。
    正常长句（标点少、片段长）完全不动，避免误并真实句子。
    """
    matches = list(_PUNCT_RUN.finditer(text))
    if len(matches) < min_periods:
        return text
    internal_frags: list[int] = []
    last = 0
    for m in matches[:-1]:
        frag = text[last:m.start()]
        internal_frags.append(len(re.sub(r"\s", "", frag)))
        last = m.end()
    if not internal_frags:
        return text
    avg = sum(internal_frags) / len(internal_frags)
    if avg > max_avg_frag:
        return text  # 长片段为主，视为正常断句，不动
    out: list[str] = []
    last = 0
    for i, m in enumerate(matches):
        out.append(text[last:m.start()])
        if i < len(matches) - 1:
            pass  # 删掉内部句末标点
        else:
            out.append(m.group(1))  # 仅保留末次句末标点
        last = m.end()
    out.append(text[last:])
    return "".join(out)


def build_hotwords(terms: list[str], limit: int = 60) -> str | None:
    """把个人词典转成 sherpa-onnx 的热词偏置串（create_stream(hotwords=...)）。"""
    parts: list[str] = []
    for t in terms[:limit]:
        t = t.strip()
        if not t:
            continue
        if CJK_RE.search(t):
            parts.append(" ".join(t))  # 中文按字切分，与模型 token 对齐
        else:
            parts.append(" ".join(t.upper().split()))
    return "/".join(parts) if parts else None


def energy_segments(samples: np.ndarray, *, threshold: float = 0.012,
                    min_speech_s: float = 0.18, min_silence_s: float = 0.55,
                    max_segment_s: float = 18.0) -> list[np.ndarray]:
    """无 VAD 模型时的能量端点检测回退方案（自适应噪声底）。"""
    if samples.size < SAMPLE_RATE // 2:
        return [samples] if samples.size else []
    hop = int(SAMPLE_RATE * 0.02)
    frames = [samples[i:i + hop] for i in range(0, len(samples), hop)]
    rms = np.array([float(np.sqrt(np.mean(f ** 2))) if f.size else 0.0 for f in frames])
    noise = float(np.percentile(rms[:25], 20)) if rms.size >= 25 else 0.0
    level = max(threshold, noise * 2.2, 0.003)
    active = rms > level
    min_speech = max(1, int(min_speech_s / 0.02))
    min_silence = max(1, int(min_silence_s / 0.02))
    max_len = int(max_segment_s / 0.02)

    out: list[np.ndarray] = []
    start: int | None = None
    silence = 0
    for i, a in enumerate(active):
        if a:
            if start is None:
                start = max(0, i - 5)  # 前置 100ms，避免吞掉字头
            silence = 0
            if i - start >= max_len:
                out.append(samples[start * hop:(i + 1) * hop])
                start = None
                silence = 0
        elif start is not None:
            silence += 1
            if silence >= min_silence:
                if (i - silence - start) >= min_speech:
                    end = min(len(samples), (i - silence + 8) * hop)
                    out.append(samples[start * hop:end])
                start = None
                silence = 0
    if start is not None and (len(active) - start) >= min_speech:
        out.append(samples[start * hop:])
    return [s for s in out if s.size > SAMPLE_RATE * 0.12]


class ASREngine:
    """线程安全（单实例，识别在 worker 线程串行调用）的离线识别器。"""

    def __init__(self, models_dir: Path, settings: dict, provider: str | None = None):
        self.models_dir = Path(models_dir)
        self.settings = settings
        # provider 显式传入时用于基准测试；正常运行时按设置自动选择
        self.provider_override = provider
        self.backend = accel.BACKEND_CPU
        self.backend_note = ""
        self.gpu_fallback = False
        self.precision_actual = "int8"
        self._recognizer = None
        self._vad = None
        self._load_key: tuple | None = None
        self._load_error = ""
        # 启动预热与首次实时预览可能并发进入 ensure_loaded，加锁避免重复创建
        # recognizer（两份 900 MB 模型同时加载会显著拖慢首字出现的时间）。
        self._load_lock = threading.Lock()

    # ---------------------------------------------------------------- 模型状态
    @property
    def model_name(self) -> str:
        return self.settings.get("model", "sensevoice")

    @property
    def sensitivity(self) -> str:
        return self.settings.get("sensitivity", "standard")

    def model_paths(self) -> dict[str, Path]:
        sv = self.models_dir / "sensevoice"
        fp32 = sv / "model.onnx"
        int8 = sv / "model.int8.onnx"
        want = self.settings.get("precision", "auto")
        # auto：优先 fp32（实测在 CPU 上也只多约 10 ms），文件缺失则回退 int8
        if want == "int8":
            model = int8 if int8.exists() else fp32
        else:
            model = fp32 if fp32.exists() else int8
        self.precision_actual = "fp32" if model.name == "model.onnx" else "int8"
        return {
            "sensevoice_model": model,
            "sensevoice_tokens": sv / "tokens.txt",
            "vad": self.models_dir / "silero_vad.onnx",
            "whisper_dir": self.models_dir / "whisper",
        }

    def check_models(self) -> tuple[bool, str]:
        p = self.model_paths()
        if self.model_name == "whisper":
            d = p["whisper_dir"]
            required = [d / "encoder.onnx", d / "decoder.onnx", d / "tokens.txt"]
        else:
            required = [p["sensevoice_model"], p["sensevoice_tokens"]]
        missing = [str(x) for x in required if not x.exists()]
        if missing:
            return False, "缺少模型文件：" + ", ".join(missing)
        return True, ""

    def _select_backend(self) -> accel.Backend:
        if self.provider_override:
            key = self.provider_override
            if key == accel.COREML:
                return accel.BACKEND_COREML
            if key == accel.CUDA:
                return accel.BACKEND_CUDA
            return accel.BACKEND_CPU
        mode = self.settings.get("accelerator", "auto")
        precision = self.settings.get("precision", "auto")
        backend, note = accel.choose_backend(mode, precision, self.sensitivity)
        self.backend_note = note
        return backend

    def _create_recognizer(self, provider: str):
        import sherpa_onnx  # 延迟导入，加快冷启动

        p = self.model_paths()
        threads = int(self.settings.get("num_threads") or 0) or min(4, os.cpu_count() or 2)
        language = self.settings.get("language", "auto")
        if self.model_name == "whisper":
            d = p["whisper_dir"]
            return sherpa_onnx.OfflineRecognizer.from_whisper(
                encoder=str(d / "encoder.onnx"),
                decoder=str(d / "decoder.onnx"),
                tokens=str(d / "tokens.txt"),
                language="" if language == "auto" else language,
                num_threads=threads, provider=provider, debug=False,
            )
        kwargs = dict(
            model=str(p["sensevoice_model"]),
            tokens=str(p["sensevoice_tokens"]),
            num_threads=threads,
            sample_rate=SAMPLE_RATE,
            feature_dim=80,
            use_itn=True,
            provider=provider,
            debug=False,
        )
        if language in ("auto", "zh", "en", "yue", "ja", "ko"):
            kwargs["language"] = language
        return sherpa_onnx.OfflineRecognizer.from_sense_voice(**kwargs)

    def ensure_loaded(self) -> None:
        with self._load_lock:
            self.model_paths()  # 刷新 precision_actual
            backend = self._select_backend()
            key = (self.model_name, self.precision_actual, backend.key)
            if self._recognizer is not None and self._load_key == key:
                return
            ok, msg = self.check_models()
            if not ok:
                raise FileNotFoundError(msg)

            try:
                recognizer = self._create_recognizer(backend.key)
            except Exception as exc:
                if not backend.is_gpu:
                    raise
                # GPU EP 不可用（缺驱动 / 未编译进推理库）时自动回退，听写不中断
                self.gpu_fallback = True
                self._load_error = f"{type(exc).__name__}: {str(exc)[:160]}"
                backend = accel.BACKEND_CPU
                self.backend_note = "GPU 执行 provider 加载失败，已自动回退 CPU"
                recognizer = self._create_recognizer(accel.CPU)

            self._recognizer = recognizer
            self.backend = backend
            self._load_key = key
            if not self.gpu_fallback:
                self._load_error = ""

    def reload(self) -> None:
        self._recognizer = None
        self._vad = None
        self._load_key = None
        self.gpu_fallback = False
        self.ensure_loaded()

    def warmup(self) -> None:
        """直接跑一次解码预热 ONNX Runtime（不能走 transcribe，静音会被门控拦下）。"""
        self.ensure_loaded()
        self._decode(np.zeros(int(SAMPLE_RATE * 0.3), dtype=np.float32), None)

    def status(self) -> str:
        s = f"{self.backend.label} · {self.precision_actual} 全精度" \
            if self.precision_actual == "fp32" else f"{self.backend.label} · int8 量化"
        if self.gpu_fallback:
            s += " · GPU 不可用已回退 CPU"
        return s

    # ---------------------------------------------------------------- VAD
    def _new_vad(self):
        """每次识别新建 VAD 实例，避免有状态检测器跨次残留。"""
        vad_path = self.model_paths()["vad"]
        if not vad_path.exists():
            return None, 512
        import sherpa_onnx
        cfg = sherpa_onnx.VadModelConfig()
        cfg.silero_vad.model = str(vad_path)
        cfg.silero_vad.threshold = float(self.settings.get("vad_threshold", 0.42))
        cfg.silero_vad.min_silence_duration = float(self.settings.get("vad_min_silence", 0.55))
        cfg.silero_vad.min_speech_duration = float(self.settings.get("vad_min_speech", 0.18))
        cfg.silero_vad.max_speech_duration = 18.0
        cfg.sample_rate = SAMPLE_RATE
        cfg.num_threads = 1
        return sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=300), cfg.silero_vad.window_size

    def _silero_segments(self, samples: np.ndarray,
                         noise_floor: float = 0.0) -> list[np.ndarray]:
        vad, w = self._new_vad()
        if vad is None:
            return []
        segs: list[np.ndarray] = []
        for i in range(0, len(samples), w):
            vad.accept_waveform(samples[i:i + w])
            while not vad.empty():
                segs.append(np.asarray(vad.front.samples, dtype=np.float32).copy())
                vad.pop()
        vad.flush()
        while not vad.empty():
            segs.append(np.asarray(vad.front.samples, dtype=np.float32).copy())
            vad.pop()
        # 段级二次门控：VAD 在低阈值档偶尔会把一小段底噪当作语音，
        # 这种碎片送进模型最容易编出乱码，直接丢弃。
        min_rms = max(noise_floor * 1.1, 0.0025)
        return [s for s in segs
                if s.size > SAMPLE_RATE * 0.22
                and float(np.sqrt(np.mean(s ** 2))) >= min_rms]

    def split(self, samples: np.ndarray, use_vad: bool = True,
              noise_floor: float = 0.0) -> list[np.ndarray]:
        if samples.size == 0:
            return []
        # 注意：旧版对「短于 1.2 秒」的音频直接整段送模型、完全跳过 VAD，
        # 这是静音/底噪被识别成乱句的主要原因之一。现在任何长度都先做端点检测。
        if use_vad and self.settings.get("vad_enabled", True):
            try:
                segs = self._silero_segments(samples, noise_floor)
                if segs:
                    return segs
            except Exception:
                pass  # VAD 异常时自动回退能量检测，不中断听写
        return energy_segments(
            samples,
            min_silence_s=float(self.settings.get("vad_min_silence", 0.55)),
            min_speech_s=float(self.settings.get("vad_min_speech", 0.18)),
            threshold=float(self.settings.get("vad_threshold", 0.42)) * 0.03,
        )

    def _vad_confirms_speech(self, samples: np.ndarray, min_seconds: float) -> bool:
        """低信噪比轻声的兜底：能量门控会误杀，但 VAD 能听到连续语音。

        本机实测：纯底噪 VAD 时长 0.44s（放大 17 倍后 0.76s），
        而「底噪 + 轻声说话」为 6.2s，用 1.5s 阈值可以干净分开。
        """
        if samples.size < int(SAMPLE_RATE * min_seconds):
            return False
        try:
            segs = self._silero_segments(samples, 0.0)
            return sum(len(s) for s in segs) >= int(SAMPLE_RATE * min_seconds)
        except Exception:
            return False

    def _speech_gate(self, samples: np.ndarray, min_vad_s: float) -> tuple[bool, object]:
        info = audio_gate.analyze(samples, sensitivity=self.sensitivity)
        force = False
        if not info.has_speech and self._vad_confirms_speech(samples, min_vad_s):
            force = True
            info = replace(info, has_speech=True,
                           reason="VAD 确认连续语音（低信噪比轻声）")
        return force, info

    # ---------------------------------------------------------------- 识别
    def _decode(self, chunk: np.ndarray, hotwords: str | None) -> str:
        stream = self._recognizer.create_stream(hotwords=hotwords) if hotwords \
            else self._recognizer.create_stream()
        stream.accept_waveform(SAMPLE_RATE, chunk)
        self._recognizer.decode_stream(stream)
        return strip_tags(getattr(stream.result, "text", "") or "")

    @staticmethod
    def _score(text: str) -> int:
        """更完整的结果得分更高：优先实体字符，抑制纯标点与单字碎片。"""
        return len(CJK_RE.findall(text)) * 2 + len(re.findall(r"[A-Za-z0-9]", text))

    def _needs_recheck(self, seg: np.ndarray, text: str,
                       speech_ok: bool = True) -> bool:
        if not speech_ok or not self.settings.get("gpu_recheck", True):
            return False
        if self.sensitivity not in ("high", "max"):
            return False
        rms = float(np.sqrt(np.mean(seg ** 2))) if seg.size else 0.0
        quiet = rms < 0.05 or seg.size < SAMPLE_RATE * 0.55
        # 只在「没识别出来」或「明显丢字」时追加一次解码，不改动正常结果
        return (not text) or (self._score(text) <= 3 and quiet)

    def transcribe_utterance(self, samples: np.ndarray,
                             hotwords: str | None = None) -> ASRResult:
        """整句一次性重解码，不做 VAD 切分。

        实时预览专用：每次对「当前句起点到此刻」的整段重新解码，
        后续语音因此能修正前面已显示的识别结果。
        """
        t0 = time.time()
        samples = np.asarray(samples, dtype=np.float32).reshape(-1)
        if samples.size == 0:
            return ASRResult(model=self.model_name)
        # 实时预览也必须过语音门控：句子开头的静音若送进模型会先冒出乱码，
        # 再被后续内容「修正」，看起来就像识别乱跳。
        force, info = self._speech_gate(samples, 0.9)
        if not info.has_speech:
            return ASRResult(model=self.model_name)
        self.ensure_loaded()
        clean, _ = audio_gate.preprocess(
            samples, sensitivity=self.sensitivity,
            target_peak=float(self.settings.get("agc_target_peak", 0.85)),
            max_gain=(3.0 if self.settings.get("agc_enabled", True) else 1.0),
            force_speech=force)
        tail = np.zeros(int(SAMPLE_RATE * 0.45), dtype=np.float32)
        text = self._decode(np.concatenate([clean, tail]), hotwords)
        text = _strip_spurious_periods(text)
        return ASRResult(
            text=text, language=detect_language(text), segments=1,
            elapsed_ms=int((time.time() - t0) * 1000), model=self.model_name,
            backend=self.backend.key, precision=self.precision_actual,
        )

    def transcribe(self, samples: np.ndarray, *, hotwords: str | None = None,
                   use_vad: bool = True) -> ASRResult:
        t0 = time.time()
        samples = np.asarray(samples, dtype=np.float32).reshape(-1)
        if samples.size == 0:
            return ASRResult(model=self.model_name)

        # 关键修复：识别前先做本机音频前端处理并判断「是否真的有人在说话」。
        # 旧实现把底噪按峰值放大 17 倍、静音片段直接送模型，SenseVoice 会
        # 自信地编出带日文假名的乱码句子 —— 那不是准确率问题，是输入被污染。
        force, _info_pre = self._speech_gate(samples, 1.5)
        clean, info = audio_gate.preprocess(
            samples, sensitivity=self.sensitivity,
            target_peak=float(self.settings.get("agc_target_peak", 0.85)),
            max_gain=(3.0 if self.settings.get("agc_enabled", True) else 1.0),
            force_speech=force)
        if not info.has_speech:
            return ASRResult(
                model=self.model_name, backend=self.backend.key,
                precision=self.precision_actual, elapsed_ms=int((time.time() - t0) * 1000),
                warnings=[f"未检测到有效语音（{info.reason}），本次不输出文字"])
        self.ensure_loaded()

        segments = self.split(clean, use_vad=use_vad, noise_floor=info.noise_floor)
        if not segments:
            return ASRResult(
                model=self.model_name, backend=self.backend.key,
                precision=self.precision_actual, elapsed_ms=int((time.time() - t0) * 1000),
                warnings=["语音过短或过于嘈杂，未形成可识别片段"])

        self.ensure_loaded()

        head = np.zeros(int(SAMPLE_RATE * 0.20), dtype=np.float32)  # 段前补静音，减句首吞字
        tail = np.zeros(int(SAMPLE_RATE * 0.60), dtype=np.float32)  # 尾部补零，防末字丢失
        lead = np.zeros(int(SAMPLE_RATE * 0.30), dtype=np.float32)  # 复核用更长静音上下文
        long_tail = np.zeros(int(SAMPLE_RATE * 1.0), dtype=np.float32)
        rechecked = 0

        # 关键修复：逐段解码会让 SenseVoice 在每个 VAD 切片尾部各自加句号、且长句 /
        # 短停顿处丢失句内标点，最终拼出来要么「通篇句号」要么「一整段没标点」
        # （实时预览是整段解码所以有标点，终稿反而没有，前后不一致）。
        # 改成「把整段语音拼成一个整体一次性解码」：SenseVoice 拿到完整上下文，
        # 标点与断句明显更稳。仅超长语音（>30s）退回逐段解码 + 低电平片段二次复核。
        MAX_FULL = int(SAMPLE_RATE * 30)
        speech = np.concatenate([np.asarray(s, dtype=np.float32) for s in segments])
        if speech.size <= MAX_FULL:
            raw = self._decode(np.concatenate([head, speech, tail]), hotwords)
            # 单句解码极少出现散点句号；仍做兜底，避免个别碎片被切出多余标点
            raw = _strip_spurious_periods(raw)
            return ASRResult(
                text=raw,
                language=detect_language(raw),
                segments=len(segments),
                elapsed_ms=int((time.time() - t0) * 1000),
                model=self.model_name,
                backend=self.backend.key,
                precision=self.precision_actual,
                rechecked=0,
            )

        # 超长语音（>30s）：保留旧行为（逐段解码 + 低电平片段二次复核）
        texts: list[str] = []
        for seg in segments:
            text = self._decode(np.concatenate([head, seg, tail]), hotwords)
            if self._needs_recheck(seg, text):
                rechecked += 1
                alt = self._decode(np.concatenate([lead, seg, long_tail]), hotwords)
                if self._score(alt) > self._score(text):
                    text = alt
            if text:
                texts.append(text)

        # 去掉「非最后一段」切片尾部的句末标点，避免拼接后通篇都是「。」
        if len(texts) > 1:
            _sf = "。！？!?"
            for i in range(len(texts) - 1):
                texts[i] = re.sub(r"[" + re.escape(_sf) + r"]+$", "", texts[i]).rstrip()

        joiner = " " if self.model_name == "whisper" else ""
        raw = joiner.join(texts)
        raw = re.sub(r"\s+", " ", raw).strip()
        # 止血：剥离 SenseVoice 在单切片内部撒的散点句号（「短语。短语。」），
        # 仅保留末次句末标点，避免规则 / LLM 拿到一堆被句号切碎的碎片。
        raw = _strip_spurious_periods(raw)
        return ASRResult(
            text=raw,
            language=detect_language(raw),
            segments=len(segments),
            elapsed_ms=int((time.time() - t0) * 1000),
            model=self.model_name,
            backend=self.backend.key,
            precision=self.precision_actual,
            rechecked=rechecked,
        )
