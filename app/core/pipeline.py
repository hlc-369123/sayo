"""听写流水线编排：音频 → VAD/ASR → 文本润色 → 本地存储 → 待注入文本。

全流程在调用线程内同步执行（由 UI 放入 worker 线程），不创建任何网络连接。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np

from .asr import ASREngine, build_hotwords
from .dictionary import PersonalDictionary
from .llm import LocalTextEngine
from .postprocess import TextPolisher
from .recorder import Recorder
from .storage import Storage


@dataclass
class AppContext:
    app_name: str = ""
    bundle_id: str = ""
    selected_text: str = ""


@dataclass
class DictationResult:
    raw: str = ""
    text: str = ""
    language: str = ""
    model: str = ""
    mode: str = "dictate"
    applied: list[str] = field(default_factory=list)
    duration_ms: int = 0
    audio_seconds: float = 0.0
    audio_path: str = ""
    tone: str = ""
    context: AppContext = field(default_factory=AppContext)
    warnings: list[str] = field(default_factory=list)


def resolve_tone(settings: dict, bundle_id: str) -> str:
    mode = settings.get("tone_mode", "auto")
    if mode != "auto":
        return mode
    return (settings.get("app_tone_map") or {}).get(bundle_id, "none")


class DictationPipeline:
    def __init__(self, settings, storage: Storage, asr: ASREngine,
                 recorder: Recorder | None = None):
        self.settings = settings
        self.storage = storage
        self.asr = asr
        self.recorder = recorder
        self.dictionary = PersonalDictionary(storage.terms())
        self.polisher = TextPolisher(settings.get("postprocess", {}), self.dictionary)
        self.llm = LocalTextEngine(settings)
        self.refresh()

    def refresh(self) -> None:
        """设置或词典变更后刷新内存态。"""
        self.dictionary.load(self.storage.terms())
        self.polisher = TextPolisher(self.settings.get("postprocess", {}), self.dictionary)
        self.llm = LocalTextEngine(self.settings)

    @staticmethod
    def _log_empty(audio, asr_result) -> None:
        """识别为空时落日志：记录音频电平与时长，用于定位「收不到声音」。

        区分两种根因：录音全零（音频流假死/设备断开）还是「有声音但模型没出字」
        （门控误杀/音质差）。只写本机日志，不联网。
        """
        try:
            import numpy as np
            from ..config import logs_dir
            import time
            a = np.asarray(audio, dtype=np.float32)
            rms = float(np.sqrt(np.mean(a ** 2))) if a.size else 0.0
            peak = float(np.abs(a).max()) if a.size else 0.0
            with (logs_dir() / "empty.log").open("a", encoding="utf-8") as fh:
                fh.write(
                    f"{time.strftime('%H:%M:%S')} 识别为空：时长={a.size / 16000:.2f}s "
                    f"RMS={rms:.5f} 峰值={peak:.4f} 样本数={a.size} "
                    f"reason={getattr(asr_result, 'warnings', '')}\n")
        except Exception:
            pass

    def process(self, audio: np.ndarray, *, mode: str = "dictate",
                context: AppContext | None = None,
                audio_seconds: float = 0.0, audio_path: str = "") -> DictationResult:
        t0 = time.time()
        ctx = context or AppContext()
        settings = self.settings
        tone = resolve_tone(settings, ctx.bundle_id)

        hotwords = None
        if settings.get("postprocess", {}).get("personal_dictionary", True):
            hotwords = build_hotwords(self.dictionary.hotwords())

        asr_result = self.asr.transcribe(audio, hotwords=hotwords)
        raw = asr_result.text
        result = DictationResult(
            raw=raw, text=raw, language=asr_result.language, model=asr_result.model,
            mode=mode, duration_ms=asr_result.elapsed_ms, audio_seconds=audio_seconds,
            audio_path=audio_path, tone=tone, context=ctx,
            warnings=list(asr_result.warnings),
        )
        if not raw:
            result.warnings.append("未检测到有效语音，可在设置中降低 VAD 阈值或提高麦克风音量")
            self._log_empty(audio, asr_result)
            return result

        polished = self.polisher.polish(raw, tone=tone)
        result.applied = polished.applied
        result.text = polished.text

        # 智能整理（听写模式可选）：本机大模型纠错、理顺语句、按顺序词重组结构。
        # 整理 ≠ 总结：LLM 输出被长度守卫判定为「过度删减」时（fallback），
        # 保留规则润色文本并明确提示，绝不静默丢内容。
        if mode == "dictate" and bool(settings.get("organize_llm", True)) \
                and len(result.text.strip()) >= int(settings.get("organize_min_chars", 10)):
            organized, source = self.llm.organize(
                result.text, hotwords=self.dictionary.hotwords())
            if source == "local-llm" and organized:
                result.text = organized
                result.applied.append("本机大模型整理")
            elif source == "fallback":
                result.warnings.append(
                    "智能整理两次输出均删减过多，已保留信息完整的润色原文")
            elif source == "unavailable":
                result.warnings.append(
                    "智能整理需要本机 GGUF 模型（放置路径见 设置 → 常规 → 整理状态）；"
                    "已输出规则润色结果，未联网")
            elif source == "error":
                result.warnings.append(organized)

        if mode == "translate":
            translated, source = self.llm.translate(result.text, settings.get("target_language", "en"))
            if source == "local-llm":
                result.text = translated
                result.applied.append("本地翻译")
            else:
                result.warnings.append("翻译需要随包安装的本地 GGUF 模型；当前未启用，已输出原文")
        elif mode == "ask":
            edited, source = self.llm.rewrite(ctx.selected_text, raw)
            if source == "rule":
                result.text = edited
                result.applied.append("本地文本命令")
            elif source == "local-llm":
                result.text = edited
                result.applied.append("本地 LLM 改写")
            else:
                answer, src = self.llm.ask(ctx.selected_text, raw)
                if src == "local-llm":
                    result.text = answer
                    result.applied.append("本地 LLM 问答")
                else:
                    result.warnings.append(
                        "该指令需要随包安装的本地 GGUF 模型；离线环境不提供联网问答。"
                        "可用确定性命令：" + "、".join(LocalTextEngine.supported_rule_commands()[:4])
                    )
                    result.text = edited or result.text

        result.duration_ms = int((time.time() - t0) * 1000)
        if settings.get("save_audio", False) and audio_path:
            result.audio_path = audio_path

        try:
            self.storage.add_history(
                raw=result.raw, polished=result.text, app_name=ctx.app_name,
                bundle_id=ctx.bundle_id, language=result.language, mode=mode,
                duration_ms=result.duration_ms, audio_path=result.audio_path,
            )
            self.storage.bump_stat("dictations")
            self.storage.bump_stat(f"chars_{result.language}", len(result.text))
        except Exception as exc:
            result.warnings.append(f"本地历史写入失败：{exc}")
        return result
