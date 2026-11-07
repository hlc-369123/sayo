"""浮层回归：文字与曲线是两个独立窗口，最终文字一定显示，曲线随音量明显变化。"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.ui.overlay import (  # noqa: E402
    _COLLECTION_BEHAVIOR, _NS_SCREEN_SAVER_LEVEL, RecordingOverlay,
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


def test_final_text_always_shown_even_without_live_preview(app):
    o = RecordingOverlay()
    o.set_state("recording", "正在聆听…")
    assert o.text_window._status == "正在聆听…"
    o.set_final_text("今天下午三点开会")
    o.set_state("done", "已写入输入框")
    assert o.text_window._text == "今天下午三点开会"
    o.set_state("idle")
    # 常驻隐藏：窗口不 hide（避免丢失跨 Space 注册），改为清空内容 + 曲线不绘制
    assert o.text_window._text == "" and o.text_window._status == ""
    assert o.wave_window._visible is False
    assert o.text_window.isVisible() and o.wave_window.isVisible()  # 常驻，永不 orderOut
