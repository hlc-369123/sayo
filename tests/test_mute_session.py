"""语音输入静音时序：开始提示音播完后才静音，短录音不得残留静音。

回归背景：旧实现进入录音后无条件 `QTimer.singleShot(320, mute(True))`，
两个故障：① 用户误触在 320ms 内结束 → leaving 时 mute(False) 因未静音而
直接返回，随后定时器触发 mute(True) → 系统输出永久静音；② 开始提示音
实测 0.44s，320ms 静音会掐掉尾部约 120ms。

MuteSession 用 generation token 让延时回调在会话结束后失效，无需持有
PySide 不返回的 QTimer 句柄。
"""
from __future__ import annotations

from app.core.mute import MuteSession


class _Recorder:
    def __init__(self):
        self.calls: list[bool] = []

    def __call__(self, mute: bool):
        self.calls.append(mute)


def test_short_recording_timer_callback_is_cancelled():
    rec = _Recorder()
    session = MuteSession(rec)
    token = session.begin()          # 进入录音，排定延时静音
    session.end()                    # 320ms 内就结束（误触）
    session.apply(token)             # 延时到期才触发
    assert rec.calls == [False], "短录音后延时回调不得静音，否则系统永久没声音"


def test_normal_recording_mutes_after_delay():
    rec = _Recorder()
    session = MuteSession(rec)
    token = session.begin()
    session.apply(token)             # 提示音播完后触发
    session.end()                    # 结束录音
    assert rec.calls == [True, False]


def test_restart_invalidates_previous_timer():
    rec = _Recorder()
    session = MuteSession(rec)
    t1 = session.begin()
    session.end()
    t2 = session.begin()             # 快速重开一轮
    session.apply(t1)                # 上一轮的延时回调迟到
    session.apply(t2)                # 本轮的延时回调
    session.end()
    assert rec.calls == [False, True, False]


def test_end_is_idempotent():
    rec = _Recorder()
    session = MuteSession(rec)
    session.end()                    # 没 begin 就 end（静音功能关闭时）
    session.end()
    assert rec.calls == [False, False]  # 恢复是幂等且安全的
