"""设置页快捷键录入框：按下组合键自动录入、退格清空、无效按键被忽略。

回归背景：旧实现是普通 QLineEdit，只能手打 "cmd+shift+space"，既没有
「按一下按键自动录入」的能力，也容易把无效组合写进配置，导致系统热键
整体注册失败、新旧快捷键全部失效。
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import QEvent, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.ui.settings_window import HotkeyEdit, qt_combo  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _press(key, mods) -> QKeyEvent:
    return QKeyEvent(QEvent.KeyPress, key, mods)


def test_qt_combo_maps_modifiers_in_stable_order():
    m = Qt.KeyboardModifier
    assert qt_combo(m.MetaModifier | m.ShiftModifier, Qt.Key_Space) == "cmd+shift+space"
    assert qt_combo(m.MetaModifier | m.ShiftModifier, Qt.Key_E) == "cmd+shift+e"
    assert qt_combo(m.ControlModifier | m.AltModifier, Qt.Key_D) == "ctrl+alt+d"
    assert qt_combo(m.MetaModifier | m.ControlModifier | m.AltModifier | m.ShiftModifier,
                    Qt.Key_P) == "cmd+ctrl+alt+shift+p"


def test_qt_combo_rejects_bare_modifier_only_or_unknown_keys():
    m = Qt.KeyboardModifier
    assert qt_combo(m.ShiftModifier, Qt.Key_A) == "shift+a"  # 有修饰键即合法
    assert qt_combo(0, Qt.Key_A) is None                # 裸键不能抢全局按键
    assert qt_combo(m.MetaModifier, Qt.Key_Meta) is None  # 纯修饰键：等待普通键
    assert qt_combo(m.MetaModifier, Qt.Key_F13) is None   # 无法映射到系统热键的键


def test_hotkey_edit_captures_press_and_clears(app):
    m = Qt.KeyboardModifier
    w = HotkeyEdit("cmd+shift+space")
    # 模拟用户清空后按下 ⌘+Shift+空格 → 自动录入
    w.clear()
    w.keyPressEvent(_press(Qt.Key_Space, m.MetaModifier | m.ShiftModifier))
    assert w.text() == "cmd+shift+space"
    # 退格清空后改按别的键
    w.keyPressEvent(_press(Qt.Key_Backspace, m.NoModifier))
    assert w.text() == ""
    w.keyPressEvent(_press(Qt.Key_E, m.MetaModifier | m.ShiftModifier))
    assert w.text() == "cmd+shift+e"
    # 纯修饰键按下不改变内容
    w.keyPressEvent(_press(Qt.Key_Meta, m.MetaModifier))
    assert w.text() == "cmd+shift+e"


def test_hotkey_edit_capture_vs_manual_typing(app):
    """组合键自动录入取代旧值；无修饰键时回退父类允许手打（保存时再校验）。"""
    m = Qt.KeyboardModifier
    w = HotkeyEdit("cmd+shift+space")
    # 纯修饰键不改内容
    w.keyPressEvent(_press(Qt.Key_Meta, m.MetaModifier))
    assert w.text() == "cmd+shift+space"
    # 有效组合直接录入并取代旧值
    w.keyPressEvent(_press(Qt.Key_D, m.ControlModifier | m.AltModifier))
    assert w.text() == "ctrl+alt+d"
    # 无修饰键的普通按键交给父类（可手打文本兜底）
    w.clear()
    from PySide6.QtCore import QEvent
    typed = QKeyEvent(QEvent.KeyPress, Qt.Key_A, m.NoModifier, "a")
    w.keyPressEvent(typed)
    assert w.text() == "a"
