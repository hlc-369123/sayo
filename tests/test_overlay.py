"""浮层回归：文字与曲线是两个独立窗口，最终文字一定显示，曲线随音量明显变化。"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.ui.overlay import (  # noqa: E402
    _COLLECTION_BEHAVIOR, _NS_NONACTIVATING_PANEL, _NS_SCREEN_SAVER_LEVEL,
    RecordingOverlay,
)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_text_and_wave_are_separate_windows(app):
    o = RecordingOverlay()
    assert o.text_window is not o.wave_window
    assert o.text_window.windowTitle() != o.wave_window.windowTitle()
    assert o.text_window.height() != o.wave_window.height()
    # 两个窗口都不能接受焦点、都鼠标穿透
    for window in (o.text_window, o.wave_window):
        assert window.testAttribute(Qt.WA_TransparentForMouseEvents)
        assert window.testAttribute(Qt.WA_ShowWithoutActivating)
        assert window.testAttribute(Qt.WA_TranslucentBackground)
    o.set_state("idle")


def test_always_on_top_native_constants():
    """跨 Space + 全屏辅助 + 屏幕保护程序层级，保证切换应用不消失。"""
    assert _NS_SCREEN_SAVER_LEVEL >= 1000
    assert _COLLECTION_BEHAVIOR & (1 << 0)   # CanJoinAllSpaces
    assert _COLLECTION_BEHAVIOR & (1 << 8)   # FullScreenAuxiliary
    assert _COLLECTION_BEHAVIOR & (1 << 6)   # IgnoresCycle


def test_wave_reacts_to_loud_and_quiet_input(app):
    o = RecordingOverlay()
    o.set_state("recording")
    quiet = o.wave_window._level
    o.set_audio(([0.05] * 28, 0.02, 0.001))
    low = o.wave_window._level
    o.set_audio(([0.95] * 28, 0.9, 0.08))
    loud = o.wave_window._level
    assert loud > low >= quiet
    assert max(o.wave_window._bands) > 0.5
    o.set_state("idle")


def test_quiet_speech_still_moves_the_wave(app):
    """轻声说话（rms≈0.012）也要明显起伏，但不能把底噪放大成满屏曲线。"""
    o = RecordingOverlay()
    o.set_state("recording")
    o.set_audio(([0.05] * 28, 0.0, 0.003))   # 环境底噪
    noise = o.wave_window._level
    o.set_audio(([0.35] * 28, 0.14, 0.012))  # 轻声说话
    speech = o.wave_window._level
    assert noise < 0.2, "底噪应接近一条直线"
    assert speech > noise * 3, "轻声也应让曲线明显动起来"
    o.set_state("idle")


def test_no_text_after_done(app):
    """结束后（识别/完成）不再在屏幕上显示文字，识别结果直接写入输入框。"""
    o = RecordingOverlay()
    o.set_state("recording", "正在聆听…")
    o.set_live_text("今天下午三点开会")
    assert o.text_window._text == "今天下午三点开会"
    o.set_state("done", "已写入输入框")
    assert o.text_window._text == ""      # 结束后屏幕不留字
    assert o.text_window._status == ""
    o.set_state("idle")
    # 常驻隐藏：窗口不 hide（避免丢失跨 Space 注册），改为清空内容 + 曲线不绘制
    assert o.text_window._text == "" and o.text_window._status == ""
    assert o.wave_window._visible is False
    assert o.text_window.isVisible() and o.wave_window.isVisible()  # 常驻，永不 orderOut


def test_pill_interactive_only_while_recording(app):
    """胶囊只在录音中接收鼠标（×/✓ 可点、整条可拖动），空闲时整体穿透。"""
    o = RecordingOverlay(overlay_style="pill")
    assert o.pill_window._mouse_transparent is True   # 初始（idle）穿透
    o.set_state("recording")
    assert o.pill_window._mouse_transparent is False  # 录音中可交互
    assert not o.pill_window.testAttribute(Qt.WA_TransparentForMouseEvents)
    o.set_state("idle")
    assert o.pill_window._mouse_transparent is True   # 恢复穿透


def test_pill_wave_still_when_silent(app):
    """静音（低于底噪门限）时胶囊波形保持静止，不出现「不说话也一直动」。"""
    o = RecordingOverlay(overlay_style="pill")
    o.set_state("recording")
    o.set_audio(([0.05] * 7, 0.02, 0.001))  # 环境底噪
    assert o.pill_window._level == 0.0
    o.set_audio(([0.95] * 7, 0.9, 0.08))    # 正常说话
    assert o.pill_window._level > 0.5
    o.set_state("idle")


class _FakeSettings:
    def __init__(self, data=None):
        self.data = data or {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


def test_pill_never_leaks_status_text_to_classic_window(app):
    """回归：胶囊模式下经典文字窗口必须始终为空。

    该窗口从启动就常驻显示，一旦被写入「正在聆听…（WorkBuddy）· MacBook Air
    麦克风」，就会以描边大字出现在屏幕别处，正是用户反馈的「别处还有文字」。
    """
    o = RecordingOverlay(overlay_style="pill")
    o.set_state("recording", "正在聆听…（WorkBuddy）· MacBook Air麦克风")
    assert o.text_window._text == "" and o.text_window._status == ""
    assert o.wave_window._visible is False
    o.set_live_text("今天下午三点开会")
    assert o.text_window._text == "" and o.text_window._status == ""
    assert o.pill_window._text == "今天下午三点开会"
    assert o.pill_window._status == ""
    o.set_state("transcribing", "本地识别与润色中…")
    assert o.text_window._text == ""
    o.set_state("idle")


def test_pill_resize_persists_and_relayouts(app):
    """右边缘拉伸：宽度变化、文字区变宽、宽度写入配置。"""
    s = _FakeSettings({"pill_width": 360})
    o = RecordingOverlay(overlay_style="pill", settings=s)
    old_right = o.pill_window._text_right
    o.pill_window.resize_to(640)       # 鼠标拖动时实时改宽
    o.pill_window.resized.emit(640)    # 松开鼠标时落盘
    assert o.pill_window.width() == 640
    assert o.pill_window._text_right > old_right + 200
    assert s.get("pill_width") == 640
    o.pill_window.resize_to(100)      # 下限钳制
    assert o.pill_window.width() == 240
    o.set_state("idle")


def test_pill_position_and_width_restored(app):
    """重启后位置与宽度都从配置恢复。"""
    s = _FakeSettings({"pill_pos": [123, 456], "pill_width": 520})
    o = RecordingOverlay(overlay_style="pill", settings=s)
    assert o.pill_window.width() == 520
    assert o._pill_pos == (123, 456)


def test_pill_is_nonactivating_and_notifies_host(app):
    """回归：点 ✓ / 拖胶囊都不许激活本应用、切走用户正在输入的界面。

    原生窗口必须带 NSWindowStyleMaskNonactivatingPanel；同时任何鼠标按下
    都要把 interaction_started 传给宿主，由宿主把前台交还原应用。
    """
    assert _NS_NONACTIVATING_PANEL == (1 << 7)
    o = RecordingOverlay(overlay_style="pill")
    seen = []
    o.interaction_started.connect(lambda: seen.append(1))
    o.pill_window.interaction_started.emit()
    assert seen == [1]
    # ×/✓ 按钮的 mousePressEvent 也走同一条通知通道
    assert hasattr(o.pill_window._btn_confirm.parent(), "interaction_started")
    assert hasattr(o.pill_window._btn_cancel.parent(), "interaction_started")
    o.set_state("idle")
