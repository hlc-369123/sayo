"""交互声音：开始 / 结束听写提示音。

设计要点（都是为了「每次都完整播完，且绝不影响听写主流程」）：
  · 不再自己打开音频输出流。旧实现与录音的输入流同时占用同一个设备，
    在虚拟声卡 / 蓝牙耳机切换设备时会直接把进程带崩（实测整进程退出）。
  · 改成「单线程 + 队列 + 阻塞播放」：一段播完才播下一段，绝不互相打断，
    这就是之前「只听到半截」的根因；队列长度限制为 2，异常刷屏也不堆积。
  · 播放线程常驻但空闲时只阻塞在队列上，待命几乎不占 CPU / 内存，
    也不长期占用音频设备。
  · 任何异常都静默忽略，提示音永远不能成为听写失败的原因。

提示音文件放在 assets/sounds/：start.wav（开始，上行）、end.wav（结束，下行）。
"""
from __future__ import annotations

import queue
import subprocess
import sys
import threading
from pathlib import Path

_enabled = True
_queue: "queue.Queue[str | None]" = queue.Queue(maxsize=2)
_worker: threading.Thread | None = None
_worker_lock = threading.Lock()


def _sounds_dir() -> Path:
    """定位 assets/sounds，兼容开发环境与被 PyInstaller 打包后的运行时。"""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        cand = Path(sys._MEIPASS) / "assets" / "sounds"
        if cand.exists():
            return cand
    here = Path(__file__).resolve().parent          # app/core
    for depth in (2, 3, 1):
        cand = here.parents[depth - 1] / "assets" / "sounds"
        if cand.exists():
            return cand
    return Path(__file__).resolve().parent.parent.parent / "assets" / "sounds"


def set_enabled(value: bool) -> None:
    """设置是否播放交互声音（来自「设置 → 常规 → 交互声音」）。"""
    global _enabled
    _enabled = bool(value)


def is_enabled() -> bool:
    return _enabled


# ------------------------------------------------------------------ 播放线程
def _play_blocking(path: str) -> None:
    """在当前线程把一段提示音播完（阻塞，保证完整）。"""
    if sys.platform == "win32":
        import winsound
        # 不带 SND_ASYNC：阻塞到播完，不会被下一段打断
        winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_NODEFAULT)
    elif sys.platform == "darwin":
        subprocess.run(["afplay", path], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=5)
    else:
        subprocess.run(["paplay", path], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=5)


def _run() -> None:
    while True:
        name = _queue.get()
        try:
            if name is None:
                return
            path = _sounds_dir() / name
            if path.is_file():
                _play_blocking(str(path))
        except Exception:
            pass          # 播放失败绝不影响听写
        finally:
            _queue.task_done()


def _ensure_worker() -> None:
    global _worker
    with _worker_lock:
        if _worker is not None and _worker.is_alive():
            return
        _worker = threading.Thread(target=_run, daemon=True, name="sound-cue")
        _worker.start()


def _enqueue(name: str) -> None:
    if not _enabled:
        return
    _ensure_worker()
    try:
        _queue.put_nowait(name)
    except queue.Full:
        # 队列已满说明提示音在刷屏，丢掉这一段比阻塞调用方更好
        pass


# ------------------------------------------------------------------ 对外接口
def play_start() -> None:
    """听写开始：上行提示音（非阻塞，排队播完整段）。"""
    _enqueue("start.wav")


def play_end() -> None:
    """听写结束：下行提示音（非阻塞，排队播完整段）。"""
    _enqueue("end.wav")


def prewarm() -> None:
    """兼容旧调用：现在不预先占用音频设备，待命更省资源。"""
    return


def shutdown() -> None:
    """退出时不做处理：让已排队的提示音播完，也不占用设备。

    播放线程是守护线程，进程退出时自然结束；这里主动 close 任何音频设备
    反而是之前「声音被截断」的来源，所以保持空实现。
    """
    return
