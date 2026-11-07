"""焦点判定与智能输出分支（用假的辅助功能 API，不触碰真实系统权限）。"""
from __future__ import annotations

import sys
import types

from app.platform import focus


def _fake_ax(monkeypatch, role, extra=None):
    fake = types.ModuleType("ApplicationServices")

    def create():
        return object()

    def copy(el, name, _unused):
        if name == "AXFocusedUIElement":
            return (0, object())
        if name == "AXRole":
            return (0, role)
        if extra and name in extra:
            return (0, extra[name])
        return (1, None)

    fake.AXUIElementCreateSystemWide = create
    fake.AXUIElementCopyAttributeValue = copy
    monkeypatch.setitem(sys.modules, "ApplicationServices", fake)
    monkeypatch.setattr(focus.sys, "platform", "darwin")


def test_text_field_is_input(monkeypatch):
    _fake_ax(monkeypatch, "AXTextField")
    assert focus.text_input_focused() is True


def test_secure_and_text_area_are_input(monkeypatch):
    for role in ("AXSecureTextField", "AXTextArea", "AXSearchField", "AXComboBox"):
        _fake_ax(monkeypatch, role)
        assert focus.text_input_focused() is True


def test_window_or_button_is_not_input(monkeypatch):
    for role in ("AXWindow", "AXButton", "AXStaticText", "AXList"):
        _fake_ax(monkeypatch, role)
        assert focus.text_input_focused() is False


def test_browser_editable_area(monkeypatch):
    _fake_ax(monkeypatch, "AXWebArea", {"AXRoleDescription": "text entry area"})
    assert focus.text_input_focused() is True


def test_unknown_role_keeps_default_behavior(monkeypatch):
    _fake_ax(monkeypatch, "AXWebArea")
    assert focus.text_input_focused() is None


def test_non_mac_returns_none(monkeypatch):
    monkeypatch.setattr(focus.sys, "platform", "linux")
    assert focus.text_input_focused() is None
    assert "无法判断" in focus.describe()


def test_choose_output_matrix():
    assert focus.choose_output(True)[0] == "paste"
    assert focus.choose_output(False)[0] == "clipboard"
    assert focus.choose_output(None)[0] == "clipboard", "判断不出来时默认只放剪贴板"
    assert focus.choose_output(None, unknown="paste")[0] == "paste"
    assert focus.choose_output(None, smart=False)[0] == "paste", "关闭智能输出应恢复原行为"
    assert "焦点不明确" in focus.choose_output(None)[1]
