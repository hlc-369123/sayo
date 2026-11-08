"""可选本地大模型（llama.cpp / GGUF）适配层 + 无模型时的确定性文本编辑。

说明（离线边界）：
  · 「改写选中文字 / 边说边翻译 / 就选中文字提问」在打包了本地 GGUF 模型时可用，
    推理完全在本机 CPU/GPU 上完成；
  · 未打包模型时，仍提供无需模型的确定性命令（缩短、扩写、要点化、正式化、大小写等）；
  · 「联网实时问答 / 搜索网页 / 云同步」在纯离线约束下不可实现，界面会明确提示，
    不做任何伪装成在线能力的假响应。
"""
from __future__ import annotations

import re
from pathlib import Path


class LocalTextEngine:
    def __init__(self, settings: dict):
        self.settings = settings
        self._llm = None
        self._load_error = ""

    @property
    def enabled(self) -> bool:
        return bool(self.settings.get("llm_enabled", True))

    def _candidate_model_paths(self) -> list[Path]:
        """模型文件的三级搜索：设置路径 → 用户数据目录 → 开发目录。

        GGUF 体积大，不随应用包分发；安装版把模型放到
        ~/Library/Application Support/<APP>/llm/*.gguf 即生效。
        """
        paths: list[Path] = []
        custom = self.settings.get("llm_model_path")
        if custom:
            paths.append(Path(str(custom)).expanduser())
        try:
            from ..config import user_data_dir
            d = user_data_dir() / "llm"
            if d.is_dir():
                paths += sorted(d.glob("*.gguf"))
        except Exception:
            pass
        try:
            from ..config import resource_root
            d = resource_root().parent / "llm-models"
            if d.is_dir():
                paths += sorted(d.glob("*.gguf"))
        except Exception:
            pass
        return paths

    @property
    def model_path(self) -> Path:
        custom = Path(str(self.settings.get("llm_model_path") or "")).expanduser()
        if custom.is_file():
            return custom
        found = self._candidate_model_paths()
        return found[0] if found else custom

    def available(self) -> bool:
        return self.enabled and self.model_path.is_file()

    def status(self) -> str:
        if not self.enabled:
            return "未启用本地模型（仅确定性文本命令可用）"
        if not self.model_path.is_file():
            try:
                from ..config import user_data_dir
                llm_dir = user_data_dir() / "llm"
            except Exception:
                llm_dir = "~/Library/Application Support/sayo/llm"
            return ("本地模型文件不存在：把任意 Qwen2.5 等 GGUF 模型放进 "
                    f"{llm_dir} 目录，或在「设置 → 润色」里指定模型路径后保存")
        if self._llm is None:
            try:
                self._get_llm()
            except Exception as exc:
                return f"本地模型加载失败：{exc}"
        return f"本地模型已就绪：{self.model_path.name}" if self._llm else self._load_error

    def _get_llm(self):
        if self._llm is not None:
            return self._llm
        try:
            from llama_cpp import Llama  # 可选依赖，构建时才打包
        except ImportError as exc:
            raise RuntimeError("未打包 llama-cpp-python，无法使用本地 LLM") from exc
        kwargs = dict(
            model_path=str(self.model_path),
            n_ctx=4096,
            n_threads=min(8, __import__("os").cpu_count() or 4),
            verbose=False,
        )
        # Apple Metal 构建默认全量上 GPU；CPU 构建该参数只产生一次警告，无害
        try:
            self._llm = Llama(n_gpu_layers=-1, **kwargs)
        except Exception:
            kwargs.pop("n_gpu_layers", None)
            self._llm = Llama(**kwargs)
        return self._llm

    def _generate(self, system: str, user: str, max_tokens: int = 512) -> str:
        llm = self._get_llm()
        out = llm.create_chat_completion(
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            temperature=0.2,
            max_tokens=max_tokens,
        )
        return (out["choices"][0]["message"]["content"] or "").strip()

    # ------------------------------------------------------------ 对外能力
    def rewrite(self, selected: str, instruction: str) -> tuple[str, str]:
        """返回 (结果, 来源标记)。"""
        if not selected.strip():
            return "", "empty"
        rule = self._rule_command(selected, instruction)
        if rule is not None:
            return rule, "rule"
        if self.available():
            try:
                text = self._generate(
                    "你是文字编辑助手。只输出编辑后的正文，不要解释、不要加引号或前后缀。"
                    "保持原文语言，除非指令明确要求翻译。",
                    f"【指令】{instruction}\n【原文】\n{selected}",
                )
                return text, "local-llm"
            except Exception as exc:
                return f"[本地模型推理失败：{exc}]", "error"
        return "", "unsupported"

    def translate(self, text: str, target: str) -> tuple[str, str]:
        if self.available():
            try:
                out = self._generate(
                    "你是翻译助手。只输出译文，不要解释，不要加引号。译文要符合目标语言的自然书写习惯。",
                    f"把下面的内容翻译成{target}：\n{text}",
                )
                return out, "local-llm"
            except Exception as exc:
                return f"[本地模型推理失败：{exc}]", "error"
        return "", "unsupported"

    def ask(self, selection: str, question: str) -> tuple[str, str]:
        if self.available():
            try:
                out = self._generate(
                    "你是离线助手。只能基于用户提供的文字作答；资料不足时直接说明无法从给定文字得出结论。"
                    "不要编造事实，不要声称可以联网。",
                    f"【问题】{question}\n【给定文字】\n{selection or '（无）'}",
                )
                return out, "local-llm"
            except Exception as exc:
                return f"[本地模型推理失败：{exc}]", "error"
        return "", "unsupported"

    # ------------------------------------------------------------ 口述稿整理
    # 核心约束：整理 ≠ 总结。用户要的是「把所有内容都留下来」，
    # 只纠错、通顺、分点；大模型最常见的失败模式是擅自概括删减，用第 3/7 条硬性禁止。
    _ORGANIZE_SYSTEM = (
        "你是中文口述稿整理助手。任务是「整理」而不是「总结」：把语音转写的原始口述稿"
        "逐句转成通顺的书面文字，信息量必须与原稿完全一致。\n"
        "规则：\n"
        "1) 修正错别字、同音字与口误，按上下文恢复说话人本意；专有名词严格按《专有名词表》的写法输出；\n"
        "2) 只删除语气词（嗯/呃/那个）、口头禅和字面重复，不删除任何实质内容；\n"
        "3) 逐句保留全部信息：每一个要点、数字、日期、人名、项目名、结论都必须出现在输出里，"
        "禁止省略、禁止合并概括、禁止提炼缩写；\n"
        "4) 说话人改口重说时（如「不对，我是说……」「应该是……」），只保留最后确认的说法；\n"
        "5) 原稿出现「第一/第二/首先/接着/最后」等顺序词时，重组为「1. 2. 3.」开头的编号段落；"
        "内容含步骤、清单或并列要点时自动分点；\n"
        "6) 表达不顺的句子可调整语序、替换啰嗦用词，使语句通顺，但必须保持原意，"
        "不改变事实、数字和专业术语，不做过度改写；\n"
        "7) 输出长度必须与原稿相当（只少了口头禅），明显短于原稿就是错误行为；\n"
        "8) 保持原文语言，中文稿输出中文；\n"
        "9) 只输出整理后的正文：不要解释、不要加引号、不要标题和前后缀。"
    )

    _RETRY_SUFFIX = (
        "你上一次的输出删减了原稿的实质内容，这是不允许的。"
        "请逐句对照原稿重新整理：所有要点、数字、名称全部保留，只修正错字、"
        "去掉语气词和重复、理顺句子，绝不能概括或省略任何信息。"
    )

    @staticmethod
    def _effective_chars(text: str) -> int:
        """有效内容长度：去掉空白与标点后统计，避免「编号/标点变多」虚增长度。"""
        return len(re.sub(r"[\s，。！？；：、,.!?;:…—\-()（）「」『』\"'""*\n]", "", text or ""))

    @classmethod
    def _over_compressed(cls, src: str, out: str, floor: float) -> bool:
        a, b = cls._effective_chars(src), cls._effective_chars(out)
        return a >= 12 and b < a * floor

    @classmethod
    def _floors(cls, src: str) -> tuple[float, float]:
        """(首次判定阈值, 重试判定阈值)。

        短文本丢一个词占比就很大（14 字丢「下午」只剩 64%），用更严的 0.75；
        长文本口头禅占比可达两三成，0.60 既能拦住摘要化又不误伤正常整理。
        """
        short = cls._effective_chars(src) < 30
        return (0.75, 0.65) if short else (0.60, 0.50)

    def organize(self, text: str, hotwords: list[str] | None = None) -> tuple[str, str]:
        """整理口述稿：纠错 + 通顺 + 分点，信息量与原稿一致。

        内置长度守卫：输出有效内容 < 原文 60% 视为「被总结过头」，自动重试一次；
        重试后仍 < 50% 则返回 ("", "fallback")，由上层保留润色原文，绝不静默丢内容。
        返回 (结果, 来源标记)：local-llm / unavailable / error / fallback。
        """
        if not text.strip():
            return "", "empty"
        if not self.available():
            return "", "unavailable"
        prompt = f"【口述原稿】\n{text.strip()}"
        if hotwords:
            words = "、".join(dict.fromkeys(hotwords))[:300]
            prompt += f"\n\n【专有名词表】（出现时必须按此写法输出）：{words}"
        try:
            out = self._generate(self._ORGANIZE_SYSTEM, prompt, max_tokens=1536)
            first_floor, retry_floor = self._floors(text)
            if self._over_compressed(text, out, floor=first_floor):
                retry = self._generate(
                    self._ORGANIZE_SYSTEM + "\n重要：" + self._RETRY_SUFFIX,
                    prompt, max_tokens=1536)
                if self._over_compressed(text, retry, floor=retry_floor):
                    return "", "fallback"  # 两次都过度删减：宁可不整理，也不丢内容
                return retry, "local-llm"
            return out, "local-llm"
        except Exception as exc:
            return f"[本地模型整理失败：{exc}]", "error"

    # ------------------------------------------------------------ 一键总结
    _SUMMARY_MODES = {
        "points": (
            "你是离线总结助手。把给定文字提炼成要点列表：5 条以内，每条一行、以「- 」开头，"
            "每条只保留一个关键信息；不新增内容、不编造，只用原文字词。只输出要点，不要解释。"
        ),
        "brief": (
            "你是离线总结助手。把给定文字提炼成一段不超过 100 字的短摘要，"
            "覆盖核心结论与关键信息；不新增内容、不编造。只输出摘要正文，不要解释。"
        ),
    }

    def summarize(self, text: str, style: str = "points") -> tuple[str, str]:
        """一键总结：返回 (结果, 来源标记)。"""
        if not text.strip():
            return "", "empty"
        if not self.available():
            return "", "unavailable"
        system = self._SUMMARY_MODES.get(style, self._SUMMARY_MODES["points"])
        try:
            out = self._generate(system, f"【待总结文字】\n{text.strip()}", max_tokens=512)
            return out, "local-llm"
        except Exception as exc:
            return f"[本地模型总结失败：{exc}]", "error"

    # ------------------------------------------------------------ 确定性命令
    @staticmethod
    def _rule_command(selected: str, instruction: str) -> str | None:
        s = selected.strip()
        i = instruction.strip().lower()

        def has(*keys: str) -> bool:
            return any(k in i for k in keys)

        if has("缩短", "精简", "简写", "shorten", "shorter", "concise", "condense"):
            parts = re.split(r"(?<=[。！？!?.;；\n])", s)
            parts = [p for p in parts if p.strip()]
            return (parts[0] if parts else s).strip()
        if has("扩写", "展开", "longer", "expand", "elaborate"):
            return f"{s}\n\n补充说明：以上内容的背景与细节可进一步展开。"
        if has("要点", "列表", " bullet", "bullet", "列成", "分点", "list"):
            parts = [p.strip() for p in re.split(r"[。！？!?；;\n，,]", s) if p.strip()]
            return "\n".join(f"- {p}" for p in parts)
        if has("更正式", "正式", "专业", "formal", "professional", "商务"):
            from .postprocess import FORMAL_REPLACEMENTS
            for a, b in FORMAL_REPLACEMENTS:
                s = s.replace(a, b)
            return s
        if has("大写", "uppercase", "upper case"):
            return s.upper()
        if has("小写", "lowercase", "lower case"):
            return s.lower()
        if has("去掉空格", "删除空格", "remove spaces"):
            return re.sub(r"\s+", "", s)
        if has("加粗", "bold"):
            return f"**{s}**"
        if has("加引号", "quote"):
            return f"「{s}」"
        if has("原文", "保持不变", "不改动", "keep"):
            return s
        return None

    @staticmethod
    def supported_rule_commands() -> list[str]:
        return ["缩短 / 精简", "扩写", "改成要点列表", "更正式 / 专业化",
                "全部大写 / 小写", "去掉空格", "加粗 / 加引号"]
