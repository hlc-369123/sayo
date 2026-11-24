"""回归：macOS 上 PortAudio stop 与 CoreAudio IO 回调互锁时，GUI 不能被拖死。

现象（2026-11-24）：一次听写结束后 stream.stop() 永久阻塞，界面冻结、
系统静音无法恢复。根因是 GUI 线程同步调 stream.stop()，与音频 IO 线程
的 startStopCallback 互持锁。修复后 stop() 只标记 + 取数据，阻塞的关闭
交给守护线程。
"""
from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from app.core.recorder import Recorder, RecorderError


def _recording_recorder():
    """造一个处于录音中、带一段已知音频的 Recorder（不碰真实硬件）。"""
    rec = Recorder({})
    rec._recording = True
    rec._chunks = [np.full(100, 0.5, dtype=np.float32),
                   np.full(50, 0.25, dtype=np.float32)]
    return rec


def test_stop_returns_immediately_when_stream_close_blocks():
    """流的 stop() 永久阻塞时，recorder.stop() 必须在极短时间内返回。"""
    rec = _recording_recorder()
    gate = threading.Event()   # 模拟底层死锁：stop() 永远等不到

    class BlockingStream:
        def stop(self):
            gate.wait(timeout=5.0)

        def close(self):
            gate.wait(timeout=5.0)

    rec._stream = BlockingStream()
    try:
        started = time.monotonic()
        audio = rec.stop()
        elapsed = time.monotonic() - started
        # 若仍是同步 stop，这里要等满 5 秒；修复后应在毫秒级返回
        assert elapsed < 0.5, f"stop() 阻塞了 {elapsed:.2f}s，GUI 会被冻结"
        assert audio.shape == (150,), "数据必须在关闭流之前就交给调用方"
        assert np.isclose(audio[0], 0.5) and np.isclose(audio[-1], 0.25)
        assert rec._stream is None
        assert not rec._recording
    finally:
        gate.set()   # 放行守护线程，避免测试进程残留


def test_worker_thread_is_tracked_and_cleaned():
    """关闭任务进守护线程，正常结束后要从待关列表移除。"""
    rec = _recording_recorder()

    class FastStream:
        def stop(self):
            pass

        def close(self):
            pass

    rec._stream = FastStream()
    rec.stop()
    # 等守护线程跑完
    deadline = time.monotonic() + 2.0
    while rec._closing and time.monotonic() < deadline:
        time.sleep(0.01)
    assert rec._closing == [], "已结束的关闭线程不应残留在列表里"


def test_stalled_close_blocks_next_start_without_hanging():
    """上一次关闭卡死时，下一次开流要快速报错，而不是在 GUI 线程陪着卡。"""
    rec = Recorder({})
    gate = threading.Event()

    class BlockingStream:
        def stop(self):
            gate.wait(timeout=5.0)

        def close(self):
            pass

    rec._recording = True
    rec._stream = BlockingStream()
    try:
        rec.stop()
        # 伪造一个「已经卡了很久」的时间戳，避免测试真的 sleep 1.5 秒
        thread, started = rec._closing[0]
        rec._closing[0] = (thread, started - Recorder._CLOSE_STALL_SEC - 1)

        started_at = time.monotonic()
        with pytest.raises(RecorderError, match="重启"):
            rec.start(device=0)
        assert time.monotonic() - started_at < 0.5
    finally:
        gate.set()


def test_normal_stop_then_start_does_not_false_positive():
    """正常关闭后紧接着重开（静音流重试路径），不能被误判成卡死。"""
    rec = _recording_recorder()

    class FastStream:
        def stop(self):
            pass

        def close(self):
            pass

    rec._stream = FastStream()
    rec.stop()
    # 只验证检查本身不抛错；不真的开流（那要 sounddevice/设备）
    rec._check_stalled_close()
