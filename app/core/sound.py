"""交互声音：开始 / 结束听写的提示音。

设计为离线、零网络依赖。macOS 用系统 `afplay`（零依赖、低延迟、不阻塞主线程）；
其它平台回退到 QtMultimedia 的 QSound。

提示音文件放在 assets/sounds/：
  · start.wav —— 开始听写（上行短音）
  · end.wav   —— 结束听写（下行短音）
两段都是随包内置的本地音频素材，播放不依赖网络；如需换成自己的音色，同名覆盖即可。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_enabled = True


def _sounds_dir() -> Path:
    """定位 assets/sounds 目录，兼容开发环境与被 PyInstaller 打包后的运行时。

    开发环境：app/core/sound.py → 项目根/assets/sounds
    打包环境：sys._MEIPASS/assets/sounds
    """
    # 1) PyInstaller 打包后，资源随包放在 _MEIPASS 下
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        cand = Path(sys._MEIPASS) / "assets" / "sounds"
        if cand.exists():
            return cand
    # 2) 开发环境：从本文件向上三级到项目根
    here = Path(__file__).resolve().parent          # app/core
    for depth in (2, 3, 1):                         # app/core -> app -> 根
        cand = here.parents[depth - 1] / "assets" / "sounds"
        if cand.exists():
            return cand
    # 3) 兜底：仍返回最可能的位置（便于报错时定位）
    return Path(__file__).resolve().parent.parent.parent / "assets" / "sounds"


def set_enabled(value: bool) -> None:
    """设置是否播放交互声音（来自「设置 → 常规 → 交互声音」）。"""
    global _enabled
    _enabled = bool(value)


def is_enabled() -> bool:
    return _enabled


def _play(name: str) -> None:
    if not _enabled:
        return
    path = _sounds_dir() / name
    if not path.exists():
        return
    try:
        if sys.platform == "darwin":
            subprocess.Popen(
                ["afplay", str(path)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        else:
            from PySide6.QtMultimedia import QSound  # 非 macOS 的回退
            QSound.play(str(path))
    except Exception:
        pass


def play_start() -> None:
    """听写开始：上行提示音。"""
    _play("start.wav")


def play_end() -> None:
    """听写结束：下行提示音。"""
    _play("end.wav")
