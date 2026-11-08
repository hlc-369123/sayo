"""全局配置、路径解析与默认设置。

设计约束：所有路径都指向本机磁盘；运行时不读取任何网络地址，
不发起账号校验、授权校验、更新检查或遥测。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

APP_NAME = "轻述 sayo"
APP_ID = "sayo"
APP_VERSION = "1.0.0"
SAMPLE_RATE = 16000  # SenseVoice / Silero VAD 均要求 16 kHz 单声道
CHANNELS = 1


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def resource_root() -> Path:
    """安装包内的只读资源根目录（模型、图标、内置词表）。

    PyInstaller 在不同平台/不同打包模式下把数据放到不同位置，
    这里逐一探测，保证开发态与三种平台安装包都能找到 models/。
    """
    if is_frozen():
        exe = Path(sys.executable).resolve()
        base = Path(getattr(sys, "_MEIPASS", exe.parent))
        candidates = [
            base,
            base.parent / "Resources",
            exe.parent / "Resources",
            exe.parent.parent / "Resources",
            exe.parent,
        ]
        for c in candidates:
            if (c / "models").is_dir():
                return c
        return base
    return Path(__file__).resolve().parent.parent


def models_dir() -> Path:
    override = os.environ.get("SAYO_MODELS")
    if override:
        return Path(override).expanduser()
    return resource_root() / "models"


def user_data_dir() -> Path:
    override = os.environ.get("SAYO_DATA")
    if override:
        p = Path(override).expanduser()
    elif sys.platform == "darwin":
        p = Path.home() / "Library" / "Application Support" / APP_ID
    elif os.name == "nt":
        p = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / APP_ID
    else:
        p = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / APP_ID
    p.mkdir(parents=True, exist_ok=True)
    return p


def logs_dir() -> Path:
    p = user_data_dir() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


DB_PATH = user_data_dir() / "sayo.db"
SETTINGS_PATH = user_data_dir() / "settings.json"
AUDIO_DIR = user_data_dir() / "audio"

DEFAULT_SETTINGS: dict = {
    # ---- 快捷键 ----
    "hotkey_dictate": "cmd+shift+space" if sys.platform == "darwin" else "ctrl+shift+space",
    # ⌘+Shift+A 被 macOS 系统保留，Carbon 注册成功但不派发；用 E 代替
    "hotkey_ask": "cmd+shift+e" if sys.platform == "darwin" else "ctrl+shift+e",
    "push_to_talk": False,  # True = 按住说话、松开结束
    # ---- 识别 ----
    "language": "auto",  # auto / zh / en / yue / ja / ko / auto(whisper)
    "model": "sensevoice",  # sensevoice | whisper（whisper 需构建时额外打包）
    # ---- 精度与加速（GPU 只提供算力，准确率来自模型精度与复核策略）----
    "precision": "fp32",  # auto | fp32 | int8；fp32 保留完整声学细节，实测 CPU 上也只多约 10ms
    "accelerator": "auto",  # auto | cpu | gpu（macOS Core ML / NVIDIA CUDA，失败自动回退 CPU）
    "sensitivity": "high",  # standard | high | max，见下方预设表
    "gpu_recheck": True,  # 低电平/过短片段做二次解码复核，用额外算力换轻声召回率
    "num_threads": 0,  # 0 = 自动（min(4, CPU 核数)）
    "vad_enabled": True,
    # 默认 VAD 参数必须与 sensitivity: "high" 档一致——曾经默认档位是 high
    # 而参数是 standard 组，用户在设置窗口保存一次后就得到「high 档 speech/AGC
    # + standard 档阈值」的混合状态，VAD 阈值偏大导致轻声识别不到（灵敏度下降）。
    "vad_threshold": 0.32,  # 越低越灵敏（high 档）
    "vad_min_silence": 0.46,  # 秒，静音多久判定一句话结束（high 档）
    "vad_min_speech": 0.12,  # 秒，最短有效语音（high 档）
    "auto_stop_silence_s": 0.0,  # >0 时说完自动结束；0 = 手动再按快捷键结束
    "agc_enabled": True,  # 麦克风增益归一化，提升灵敏度
    "agc_target_peak": 0.90,  # high 档
    "max_record_seconds": 300,
    # ---- 实时浮层与输出 ----
    "live_preview": True,  # 录音时两个独立置顶窗口：实时文字 + 声音曲线
    "live_interval_ms": 450,  # 每约 0.45s 重解码当前句，后续语音会修正前面已显示的字
    "smart_output": True,  # 有输入框焦点才粘贴；无焦点只写剪贴板
    "smart_output_when_unknown": "paste",  # 焦点无法判断时：paste（默认，保证「说了就出字」）/ clipboard（保守，只复制需手动 ⌘V）
    "first_run_done": False,  # 首次启动弹出「怎么用」引导
    "theme": "aurora",  # 内置主题 key，见 app/ui/themes.py THEMES（默认极光：文字+曲线无底板）
    "overlay_style": "classic",  # 浮层样式：classic（原样式，文字/曲线两窗）/ pill（单条胶囊，简洁风）
    "interaction_sound": True,  # 交互声音：开始/结束听写的提示音（ 原声）
    "ax_guide_auto": True,  # 辅助功能未授权时，启动后自动弹出授权引导窗口
    # ---- 文本润色（全部本地规则，可叠加本地 LLM）----
    "postprocess": {
        "remove_fillers": True,
        "remove_repetitions": True,
        "self_correction": True,
        "auto_format": True,
        "normalize_spacing": True,
        "punctuation": True,
        "personal_dictionary": True,
    },
    "tone_mode": "auto",  # auto / formal / casual / code / none
    "app_tone_map": {
        "com.apple.mail": "formal",
        "com.microsoft.Word": "formal",
        "com.apple.iWork.Pages": "formal",
        "com.tencent.WeWorkMac": "casual",
        "com.tencent.xinWeChat": "casual",
        "com.apple.Terminal": "code",
        "com.microsoft.VSCode": "code",
    },
    "target_language": "en",  # 翻译模式目标语言（需本地 LLM）
    # ---- 输出 ----
    "inject_method": "paste",  # paste | clipboard_only | type
    "restore_clipboard": True,
    # ---- 隐私与存储 ----
    "save_audio": False,
    "history_retention_days": 365,
    "llm_enabled": True,
    "llm_model_path": "",
    # 智能整理（完全离线）：本机 GGUF 模型修正错别字、理顺语句、按「第一点/第二点」重组
    "organize_llm": True,
    "organize_min_chars": 10,  # 短于此长度的结果不做大模型整理，避免过度改写
    "offline_lock": True,  # True = 主动封锁所有出站 socket（进程级硬约束）
    "launch_at_login": False,
}

# 灵敏度档位：越低阈值越能捕捉轻声/远场，但分段更碎，
# 因此高灵敏档配合二次复核（gpu_recheck）与 fp32 全精度一起用。
SENSITIVITY_PRESETS: dict[str, dict] = {
    "standard": {"vad_threshold": 0.42, "vad_min_silence": 0.55,
                 "vad_min_speech": 0.18, "agc_target_peak": 0.85},
    "high": {"vad_threshold": 0.32, "vad_min_silence": 0.46,
             "vad_min_speech": 0.12, "agc_target_peak": 0.90},
    "max": {"vad_threshold": 0.24, "vad_min_silence": 0.38,
            "vad_min_speech": 0.08, "agc_target_peak": 0.95},
}


def sensitivity_preset(level: str) -> dict:
    return dict(SENSITIVITY_PRESETS.get(level, SENSITIVITY_PRESETS["standard"]))


# 供 UI 展示的敏感权限说明
PERMISSION_HELP = {
    "microphone": "麦克风：用于采集语音，音频只在本机内存中处理。",
    "accessibility": "辅助功能：用于模拟 ⌘/Ctrl+V，把文字写入当前输入框；未授权时只写剪贴板。",
    "input_monitoring": "输入监控（macOS）：新的系统级热键不需要；仅在回退到键盘监听时使用。",
}


class Settings:
    """JSON 设置，落盘在用户本机目录；缺项自动用默认值补齐。"""

    def __init__(self, path: Path = SETTINGS_PATH):
        self.path = Path(path)
        self._data = json.loads(json.dumps(DEFAULT_SETTINGS))
        self.load()

    def load(self) -> None:
        if self.path.exists():
            try:
                saved = json.loads(self.path.read_text(encoding="utf-8"))
                self._merge(self._data, saved)
                self._migrate(set(saved))
            except (OSError, ValueError):
                pass  # 损坏时回退默认值，不联网、不崩溃

    def _migrate(self, saved_keys: set) -> None:
        """旧版本升级：新增能力只提升、不回退用户的显式选择。

        判断依据是**用户配置文件里是否写过 organize_llm**（合并默认值后
        新键总是存在，不能用它判断）。旧配置里的 llm_enabled=False 只是
        旧版本的默认值，不视为用户显式关闭，随本次升级一并开启；
        此后用户在设置里的任何修改都会写入文件、不再被迁移覆盖。
        """
        if "organize_llm" not in saved_keys:
            self._data["organize_llm"] = DEFAULT_SETTINGS["organize_llm"]
            self._data["llm_enabled"] = DEFAULT_SETTINGS["llm_enabled"]
            self.save()
        # 旧配置若停留在最早的「极光 aurora」且无 overlay_style 键，补上默认浮层样式，
        # 不再强制改主题（保留用户当时的视觉选择）。
        if "overlay_style" not in self._data:
            self._data["overlay_style"] = DEFAULT_SETTINGS["overlay_style"]
            self.save()

    @staticmethod
    def _merge(base: dict, saved: dict) -> None:
        for k, v in saved.items():
            if k not in base:
                continue
            if isinstance(base[k], dict) and isinstance(v, dict):
                Settings._merge(base[k], v)
            else:
                base[k] = v

    def get(self, key: str, default=None):
        return self._data.get(key, default)

    def set(self, key: str, value) -> None:
        self._data[key] = value
        self.save()

    def update(self, mapping: dict) -> None:
        self._merge(self._data, mapping)
        self.save()

    @property
    def data(self) -> dict:
        return self._data

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)
