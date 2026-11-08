"""交互声音：开始 / 结束听写的提示音。

设计为离线、零网络依赖。macOS 用系统 `afplay`（零依赖、低延迟、不阻塞主线程）；
其它平台回退到 QtMultimedia 的 QSound。

提示音文件放在 assets/sounds/：
  · start.wav —— 开始听写（上行提示音），取自  的 record-start 原声
  · end.wav   —— 结束听写（下行提示音），取自  的 record-end 原声
（从 /Applications/.app 的 app.asar 中提取，已确认语义：
 {'record-start': 7ZwPubNT.wav（升调）, 'record-end': BGK9atS4.wav（降调））。
如需替换为其它音色，同名覆盖即可。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_SOUNDS_DIR = Path(__file__).resolve().parent.parent / "assets" / "sounds"
_enabled = True


def set_enabled(value: bool) -> None:
    """设置是否播放交互声音（来自「设置 → 常规 → 交互声音」）。"""
    global _enabled
    _enabled = bool(value)


def is_enabled() -> bool:
    return _enabled


def _play(name: str) -> None:
    if not _enabled:
        return
    path = _SOUNDS_DIR / name
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
