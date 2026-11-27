"""启动时的后台模型准备：装好就能用，用户不需要管模型。

策略（尽量少打扰、少占用）：
  · 启动先做毫秒级自检（只比对文件大小 + 指纹缓存），完整文件不读盘、不联网；
  · 缺文件就在后台线程静默下载，失败自动重试 2 次（共 3 次）；
  · 三次都失败只写日志，下次启动再来，期间不影响程序其它功能；
  · 下载成功写状态文件，之后永远不再下载，也只做 stat 级校验；
  · 下载线程降低优先级，避免和听写抢 CPU；
  · 网络完全不可达时几秒内快速失败，不会卡着反复等 30 秒超时。
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

from . import model_download

MAX_ATTEMPTS = 3          # 首次 + 重试 2 次
RETRY_BACKOFF = (8.0, 20.0)


def _lower_thread_priority() -> None:
    """Windows 下把当前线程降到 BELOW_NORMAL，下载不抢听写的 CPU。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.SetThreadPriority(k32.GetCurrentThread(), -1)  # THREAD_PRIORITY_BELOW_NORMAL
    except Exception:
        pass


class ModelBootstrap:
    """一次性后台任务：确保模型就绪。

    on_done(ok, message) 在工作线程回调（调用方负责切回主线程）。
    on_progress(relative, done, total) 可选。
    """

    def __init__(self, models_dir: Path, on_done=None, on_progress=None,
                 max_attempts: int = MAX_ATTEMPTS):
        self.models_dir = Path(models_dir)
        self.on_done = on_done
        self.on_progress = on_progress
        self.max_attempts = max(1, int(max_attempts))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.status = "idle"       # idle | checking | downloading | ready | failed | offline
        self.message = ""

    # ------------------------------------------------------------ 对外接口
    def start(self) -> bool:
        """返回 True 表示确实需要下载（已经完整则不启动线程）。"""
        if self._thread is not None and self._thread.is_alive():
            return True
        if not model_download.light_missing(self.models_dir):
            self.status = "ready"
            # 文件都在，后台补一次带指纹缓存的全量校验（通常只做 stat，毫秒级）
            model_download.verify_cached(self.models_dir)
            return False
        self.status = "checking"
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="model-bootstrap")
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------ 工作线程
    def _run(self) -> None:
        _lower_thread_priority()
        try:
            if not model_download.can_reach_any_mirror():
                # 离线：几秒内给出结论，本次启动不再尝试，下次启动重来
                self.status = "offline"
                self.message = "当前网络不可用，语音模型将在下次联网启动时自动下载"
                self._done(False, self.message)
                return

            self.status = "downloading"
            started = time.time()
            msg = model_download.download_all(
                self.models_dir,
                progress=self.on_progress,
                attempts=self.max_attempts,
                backoff=RETRY_BACKOFF,
                should_stop=self._stop.is_set,
            )
            self.status = "ready"
            self.message = msg
            self._done(True, f"{msg}（耗时 {time.time() - started:.0f} 秒）")
        except Exception as exc:
            self.status = "failed"
            self.message = f"{type(exc).__name__}: {exc}"
            self._done(False, self.message)

    def _done(self, ok: bool, message: str) -> None:
        cb = self.on_done
        if cb is None:
            return
        try:
            cb(ok, message)
        except Exception:
            pass
