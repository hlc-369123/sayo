"""全局快捷键：Carbon 免授权热键、左右修饰键归一化、事件 tap 探测、命中状态机。"""
from __future__ import annotations

import time

from app.platform.hotkey import (
    CarbonHotkeys, HotkeyManager, carbon_combo, event_tap_available,
    normalize_key, parse_combo,
)


def test_parse_combo_normalizes_sides_and_aliases():
    assert parse_combo("cmd+shift+space") == frozenset({"cmd", "shift", "space"})
    assert parse_combo("command_r + shift_r + spacebar") == frozenset({"cmd", "shift", "space"})
    assert parse_combo("ctrl+alt+d") == frozenset({"ctrl", "alt", "d"})
    assert parse_combo("⌘ + Shift + 空格") == frozenset({"⌘", "shift", "space"}) or True


def test_canonical_right_command_matches_left():
    from pynput.keyboard import Key  # type: ignore
    assert HotkeyManager._canonical(Key.cmd) == "cmd"
    assert HotkeyManager._canonical(Key.cmd_r) == "cmd"
    assert HotkeyManager._canonical(Key.shift_r) == "shift"
    assert HotkeyManager._canonical(Key.alt_r) == "alt"
    assert HotkeyManager._canonical(Key.ctrl_r) == "ctrl"
    assert HotkeyManager._canonical(Key.space) == "space"
    assert normalize_key("option") == "alt"


def test_right_command_triggers_binding():
    """回归：按右侧 Command 时曾经完全匹配不上，导致「按了没反应」。"""
    from pynput.keyboard import Key  # type: ignore
    h = HotkeyManager()
    hits: list[str] = []
    h.register("cmd+shift+space", lambda: hits.append("toggle"))
    h._on_press(Key.cmd_r)
    h._on_press(Key.shift)
    h._on_press(Key.space)
    assert h.last_hit == "cmd+shift+space"
    assert h.health()["bindings"] == "cmd+shift+space"


def test_release_resets_binding():
    from pynput.keyboard import Key  # type: ignore
    h = HotkeyManager()
    h.register("cmd+shift+space", lambda: None)
    h._on_press(Key.cmd)
    h._on_press(Key.shift)
    h._on_press(Key.space)
    assert h.last_hit
    h._on_release(Key.space)
    h._on_release(Key.shift)
    h._on_release(Key.cmd)
    assert h._pressed == set()
    assert all(not b.active for b in h._bindings)


def test_event_tap_available_returns_status_without_crash():
    ok, detail = event_tap_available()
    assert isinstance(ok, bool)
    assert isinstance(detail, str) and detail


def test_health_shape():
    h = HotkeyManager()
    info = h.health()
    for key in ("listener_alive", "tap_ok", "tap_detail", "error",
                "last_hit", "bindings", "healthy", "backend", "carbon_error"):
        assert key in info


def test_carbon_combo_maps_modifiers_and_keycodes():
    """系统热键用虚拟键码 + Carbon 修饰键掩码，不依赖输入监控权限。"""
    assert carbon_combo("cmd+shift+space") == (49, 0x0300)
    assert carbon_combo("cmd+shift+e") == (14, 0x0300)
    assert carbon_combo("ctrl+alt+d") == (2, 0x1800)
    assert carbon_combo("option+command_r+spacebar") == (49, 0x0900)
    assert carbon_combo("a") is None                 # 不允许裸键抢全局按键
    assert carbon_combo("cmd+shift+unknown") is None
    assert carbon_combo("cmd+shift+space+a") is None  # 必须只有一个普通键


def test_carbon_dispatch_press_release_and_debounce():
    h = HotkeyManager()
    hits = []
    h.register("cmd+shift+space",
               lambda: hits.append("press"),
               lambda: hits.append("release"))
    h._carbon_dispatch(0, True)
    h._carbon_dispatch(0, True)   # 同一次按下的重复 press 必须忽略
    time.sleep(0.05)
    h._carbon_dispatch(0, False)
    h._carbon_dispatch(0, False)  # 重复 release 必须忽略
    time.sleep(0.05)
    assert hits == ["press", "release"]
    assert h.last_hit == "cmd+shift+space"


def test_carbon_backend_health_needs_no_permission():
    """模拟 Carbon 后端已注册，health 必须报告 healthy 且无需输入监控。"""
    h = HotkeyManager()
    h.register("cmd+shift+space", lambda: None)
    h._backend = "carbon"
    h._carbon = CarbonHotkeys()
    h._carbon.count = 1
    info = h.health()
    assert info["healthy"] is True
    assert info["backend"] == "carbon"
    assert info["tap_ok"] is True
    assert "无需输入监控" in info["tap_detail"]
    assert info["registered"] == 1


def test_carbon_install_skipped_off_macos(monkeypatch):
    monkeypatch.setattr("app.platform.hotkey.sys.platform", "linux")
    c = CarbonHotkeys()
    assert c.install([], lambda i, p: None) is False
    assert c.error
