"""回归：macOS 上 PortAudio stop 与 CoreAudio IO 回调互锁时，GUI 不能被拖死。

现象（2026-11-24）：一次听写结束后 stream.stop() 永久阻塞，界面冻结、
系统静音无法恢复。根因是 GUI 线程同步调 stream.stop()，与音频 IO 线程
的 startStopCallback 互持锁（PortAudio #1174）。

根治方案（Mixxx PR #14208 已验证）：不直接 stop active 流，而是让 audio
callback 抛 CallbackAbort 使流 inactive，等 finished_callback 后再 stop。
本文件验证这条路径的顺序与非阻塞特性。
"""
from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from app.core import recorder as recorder_mod
from app.core.recorder import Recorder


def _recording_recorder():
    """造一个处于录音中、带一段已知音频的 Recorder（不碰真实硬件）。"""
    rec = Recorder({})
    rec._recording = True
    rec._chunks = [np.full(100, 0.5, dtype=np.float32),
                   np.full(50, 0.25, dtype=np.float32)]
    rec._start_event_pair_for_test()
    return rec


class _FakeStream:
    """可控的假流：stop 阻塞由 gate 决定；finished 由 fire_finished 触发。"""

    def __init__(self, gate: threading.Event | None = None):
        self.gate = gate
        self.stop_calls = 0
        self.close_calls = 0
        self.abort_calls = 0
        self.stopped_before_inactive = False
        self._inactive = False

    def fire_finished(self):
        self._inactive = True

    def abort(self):
        self.abort_calls += 1

    def stop(self):
        self.stop_calls += 1
        if not self._inactive:
            self.stopped_before_inactive = True
        if self.gate is not None:
            self.gate.wait(timeout=5.0)

    def close(self):
        self.close_calls += 1


def test_start_creates_fresh_events_per_session():
    """每轮录音用全新 Event，避免上一轮的后台关闭线程被本轮 clear 干扰。"""
    rec = Recorder({})
    rec._start_event_pair_for_test()
    first_stop, first_inactive = rec._stop_event, rec._inactive_event
    rec._start_event_pair_for_test()
    assert rec._stop_event is not first_stop
    assert rec._inactive_event is not first_inactive
    assert not rec._stop_event.is_set()


def test_stop_signals_callback_and_returns_immediately():
    """stop 只设标志+取数据，不等流关闭（GUI 不冻结）。"""
    rec = _recording_recorder()
    gate = threading.Event()
    rec._stream = _FakeStream(gate)
    try:
        started = time.monotonic()
        audio = rec.stop()
        elapsed = time.monotonic() - started
        assert elapsed < 0.5, f"stop() 阻塞了 {elapsed:.2f}s，GUI 会被冻结"
        assert audio.shape == (150,), "数据必须在关闭流之前就交给调用方"
        assert np.isclose(audio[0], 0.5) and np.isclose(audio[-1], 0.25)
        assert rec._stream is None
        assert not rec._recording
        assert rec._stop_event.is_set(), "必须通知 callback 抛 CallbackAbort"
    finally:
        gate.set()


def test_callback_raises_abort_when_stop_requested():
    """callback 见到停止标志后抛 CallbackAbort，促使流 inactive。"""
    rec = _recording_recorder()
    rec._stop_event.set()
    if recorder_mod._CALLBACK_ABORT is None:
        pytest.skip("本环境无 sounddevice")
    with pytest.raises(recorder_mod._CALLBACK_ABORT):
        rec._callback(np.zeros((10, 1), dtype=np.float32), 10, None, None)


def test_background_thread_waits_for_inactive_before_stopping():
    """守护线程必须等 finished_callback 之后才 stop（顺序正确=不死锁）。"""
    rec = _recording_recorder()
    stream = _FakeStream()
    rec._stream = stream
    rec._INACTIVE_WAIT_SEC = 5.0

    rec.stop()
    # finished 未触发前，后台线程应仍在等待，不应调用 stop
    time.sleep(0.15)
    assert stream.stop_calls == 0, "finished 未触发前不应调用 stop"

    # 模拟 finished_callback 触发
    stream.fire_finished()
    rec._inactive_event.set()

    deadline = time.monotonic() + 2.0
    while (stream.stop_calls == 0 or stream.close_calls == 0) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert stream.stop_calls == 1 and stream.close_calls == 1
    assert not stream.stopped_before_inactive, "stop 发生在流 inactive 之前，会死锁"
    assert stream.abort_calls == 0


def test_fallback_aborts_if_finished_never_comes():
    """finished 迟迟不来（设备被拔）时走 abort 兜底，且不影响 GUI。"""
    rec = _recording_recorder()
    stream = _FakeStream()
    rec._stream = stream
    rec._INACTIVE_WAIT_SEC = 0.1

    rec.stop()
    deadline = time.monotonic() + 2.0
    while stream.abort_calls == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert stream.abort_calls == 1, "finished 超时后应 abort 兜底"
    assert stream.stop_calls == 1 and stream.close_calls == 1


def test_no_user_facing_restart_error_path():
    """根治版不应再有旧版开流前的「请重启」兜底。"""
    rec = _recording_recorder()
    assert not hasattr(rec, "_check_stalled_close")


def test_session_callback_uses_captured_stop_event_not_instance():
    """新录音开始后，旧流的迟到回调必须看自己那轮的 stop_event。

    回归：callback 若动态读 self._stop_event，则 stop→start 后旧流回调
    看到的是新一轮未 set 的 Event，不会 abort，反而把旧流音频写进新一轮
    chunks（新旧混音），且旧的 inactive 永不置位（后台线程空等 2 秒）。
    """
    rec = Recorder({})
    old_stop, old_inactive = rec._start_event_pair_for_test()
    old_stop.set()                       # 上一轮已请求停止
    new_stop, new_inactive = rec._start_event_pair_for_test()
    assert not new_stop.is_set()         # 新一轮的 Event 是干净的

    def callback_for_session(indata, frames, time_info, status):
        return rec._callback(indata, frames, time_info, status, stop_event=old_stop)

    if recorder_mod._CALLBACK_ABORT is None:
        pytest.skip("本环境无 sounddevice，无法验证 CallbackAbort")
    with pytest.raises(recorder_mod._CALLBACK_ABORT):
        callback_for_session(np.zeros((10, 1), dtype=np.float32), 10, None, None)
    assert len(rec._chunks) == 0, "旧流回调不得写入新一轮的音频缓冲"

    # 本轮的 callback 用本轮 Event：未停止时正常收音频
    rec._recording = True
    callback_for_session = lambda indata, frames, t, s: rec._callback(
        indata, frames, t, s, stop_event=new_stop)
    callback_for_session(np.zeros((10, 1), dtype=np.float32), 10, None, None)
    assert len(rec._chunks) == 1


def test_session_finished_sets_captured_inactive_event():
    """finished 回调必须置本轮的 inactive Event，不能置新一轮的。"""
    rec = Recorder({})
    old_stop, old_inactive = rec._start_event_pair_for_test()
    new_stop, new_inactive = rec._start_event_pair_for_test()

    rec._on_stream_finished(inactive_event=old_inactive)
    assert old_inactive.is_set()
    assert not new_inactive.is_set(), "旧流 finished 不得置新一轮的 inactive"
