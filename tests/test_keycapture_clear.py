"""快捷键录入状态机：退格清空后必须能在同一录入框内重录单修饰键。

回归背景：旧实现在两处漏复位 `_saw_plain`——① on_event 的退格分支误置 True；
② 设置页调用的 `reset_fired()` 没复位它。而单修饰键（单个 Option/Cmd/Shift）
的录入条件是 `not self._saw_plain`，结果用户「删掉热键 → 重新按单个 Option」
永远录不上，必须切走焦点再回来才行。这条路径是真实 CGEventTap 回调状态机，
Qt 层的 keyPressEvent 测试覆盖不到，因此注入 fake Quartz 直接驱动 on_event。

注意：`_fired=True` 时 on_event 会吞掉一切事件，实际退格清空由 UI 层
（HotkeyEdit）调用 `reset_fired()` 完成，测试按真实交互顺序模拟。
"""
from __future__ import annotations

import sys
import types

import pytest

from app.platform import keycapture as kc


_KDOWN, _KUP, _FLAGS = 10, 11, 12
_KEYCODE_FIELD = 100
_ALT_FLAG = 1 << 19          # alt_l / alt_r 共用的 CGEventFlags 位
_CMD_FLAG = 1 << 20
_OPT_L_KEYCODE = 58          # 左 Option
_OPT_R_KEYCODE = 61          # 右 Option
_SPACE_KEYCODE = 49
_BACKSPACE_KEYCODE = 51


class _FakeEvent:
    def __init__(self, etype: int, keycode: int, flags: int):
        self.etype = etype
        self.keycode = keycode
        self.flags = flags


def _install_fake_quartz(monkeypatch):
    holder: dict = {}
    quartz = types.ModuleType("Quartz")
    quartz.kCGSessionEventTap = 0
    quartz.kCGHeadInsertEventTap = 0
    quartz.kCGEventKeyDown = _KDOWN
    quartz.kCGEventKeyUp = _KUP
    quartz.kCGEventFlagsChanged = _FLAGS
    quartz.kCGKeyboardEventKeycode = _KEYCODE_FIELD
    quartz.CGEventMaskBit = lambda mask: mask
    quartz.CGEventGetIntegerValueField = lambda event, field: event.keycode
    quartz.CGEventGetFlags = lambda event: event.flags

    def _tap_create(location, place, opts, mask, callback, refcon):
        holder["callback"] = callback
        return object()

    quartz.CGEventTapCreate = _tap_create
    quartz.CFMachPortCreateRunLoopSource = lambda alloc, tap, order: object()
    quartz.CGEventTapEnable = lambda tap, enabled: None
    quartz.CGEventTapIsEnabled = lambda tap: True
    monkeypatch.setitem(sys.modules, "Quartz", quartz)
    return holder


@pytest.fixture
def capture(monkeypatch):
    holder = _install_fake_quartz(monkeypatch)
    cap = kc.KeyCapture()
    got: list[str] = []
    assert cap.start(lambda combo: got.append(combo)) is True
    try:
        yield cap, holder["callback"], got
    finally:
        # 必须停掉 pump 线程再让 monkeypatch 恢复真实 Quartz，否则线程里
        # import Quartz 拿到真模块、却持有 fake tap(object)，C 层会崩溃。
        cap.stop()


def _fire(callback, etype, keycode, flags=0):
    return callback(None, etype, _FakeEvent(etype, keycode, flags), None)


def _single_option(cb, keycode=_OPT_L_KEYCODE):
    _fire(cb, _FLAGS, keycode, _ALT_FLAG)   # 按下
    _fire(cb, _FLAGS, keycode, 0)           # 松开 → 录入单键


def test_on_event_backspace_resets_saw_plain(capture):
    """直接走 on_event 退格分支：清空后 _saw_plain 必须为 False。"""
    cap, cb, got = capture
    _fire(cb, _KDOWN, _BACKSPACE_KEYCODE)   # 用户在已有热键框里按退格清空
    assert got == [""]
    assert cap._saw_plain is False
    _single_option(cb)
    assert got == ["", "alt_l"]


def test_clear_after_combo_then_single_option(capture):
    """核心回归：录组合键 → UI 退格清空 → 同框内按单 Option 能重录。"""
    cap, cb, got = capture
    _fire(cb, _FLAGS, 55, _CMD_FLAG)                    # cmd 按下
    _fire(cb, _KDOWN, _SPACE_KEYCODE, _CMD_FLAG)        # cmd+space
    assert got == ["cmd+space"]
    assert cap._saw_plain is True
    cap.reset_fired()                                   # UI 层退格清空
    assert cap._saw_plain is False, "reset_fired 必须复位 _saw_plain"
    _single_option(cb)
    assert got == ["cmd+space", "alt_l"]


def test_reset_fired_clears_saw_plain(capture):
    cap, cb, got = capture
    _fire(cb, _KDOWN, _SPACE_KEYCODE)      # 普通键 → _saw_plain=True
    assert cap._saw_plain is True
    cap.reset_fired()
    assert cap._saw_plain is False
    _single_option(cb)
    assert "alt_l" in got


def test_left_and_right_option_after_each_clear(capture):
    cap, cb, got = capture
    _single_option(cb, _OPT_L_KEYCODE)
    cap.reset_fired()
    _single_option(cb, _OPT_R_KEYCODE)
    assert got == ["alt_l", "alt_r"]
