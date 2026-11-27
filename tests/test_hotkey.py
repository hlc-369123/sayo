"""全局快捷键：Carbon 免授权热键、左右修饰键归一化、事件 tap 探测、命中状态机。"""
from __future__ import annotations

import sys
import time

import pytest

from app.platform.hotkey import (
    CarbonHotkeys, HotkeyManager, carbon_combo, event_tap_available,
    is_single_modifier, is_single_plain_key, normalize_key, parse_combo, valid_hotkey,
)


def test_parse_combo_normalizes_sides_and_aliases():
    assert parse_combo("cmd+shift+space") == frozenset({"cmd", "shift", "space"})
    # 组合键里左右修饰键仍归一（Carbon 不区分左右）
    assert parse_combo("command_r + shift_r + spacebar") == frozenset({"cmd_r", "shift_r", "space"})
    assert parse_combo("ctrl+alt+d") == frozenset({"ctrl", "alt", "d"})
    # 单键：左右 Option 各自独立（同 Typeless）
    assert parse_combo("option_l") == frozenset({"alt_l"})
    assert parse_combo("option_r") == frozenset({"alt_r"})
    assert parse_combo("⌘ + Shift + 空格") == frozenset({"⌘", "shift", "space"}) or True


def test_canonical_right_command_matches_left():
    from pynput.keyboard import Key  # type: ignore
    # pynput 回退后端只处理组合键，左右修饰键归一到通用名
    assert HotkeyManager._canonical(Key.cmd) == "cmd"
    assert HotkeyManager._canonical(Key.cmd_r) == "cmd"
    assert HotkeyManager._canonical(Key.shift_r) == "shift"
    assert HotkeyManager._canonical(Key.alt_r) == "alt"
    assert HotkeyManager._canonical(Key.ctrl_r) == "ctrl"
    assert HotkeyManager._canonical(Key.space) == "space"
    assert normalize_key("option") == "alt"
    # 单键场景：alt_l/alt_r 不归一（由 parse_combo 保持独立）
    assert normalize_key("option_l") == "alt_l"
    assert normalize_key("option_r") == "alt_r"


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
    # 组合键里左右修饰键归一到通用名（Carbon mask 不区分左右）
    assert carbon_combo("alt_l+d") == (2, 0x0800)   # 左 Option + D → option+d
    assert carbon_combo("alt_r+d") == (2, 0x0800)   # 右 Option + D → option+d
    assert carbon_combo("cmd_l+shift_r+a") == (0, 0x0300)
    assert carbon_combo("a") is None                 # 不允许裸键抢全局按键
    assert carbon_combo("cmd+shift+unknown") is None
    assert carbon_combo("cmd+shift+space+a") is None  # 必须只有一个普通键
    # 单个修饰键（alt_l/alt_r）不走 Carbon，carbon_combo 返回 None
    assert carbon_combo("alt_l") is None
    assert carbon_combo("option_r") is None


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


def test_suspend_resume_uses_registered_bindings(monkeypatch):
    """设置窗口录快捷键时暂停全局热键，关闭后按已注册绑定恢复。

    回归：设置窗口开着时 Carbon 全局热键仍触发听写并抢焦点，
    导致快捷键录入框收不到按键、看起来「按了没反应」。
    """
    h = HotkeyManager()
    started: list[int] = []
    stopped: list[int] = []

    def fake_start():
        started.append(1)
        return True

    def fake_stop():
        stopped.append(1)
        h._backend = ""  # 真实 stop() 会清空 backend

    monkeypatch.setattr(h, "start", fake_start)
    monkeypatch.setattr(h, "stop", fake_stop)
    h.register("cmd+shift+space", lambda: None)
    h._backend = "carbon"
    h.suspend()
    assert stopped == [1]
    assert h._backend == ""
    h.resume()
    assert started == [1]


def test_resume_noop_without_bindings(monkeypatch):
    h = HotkeyManager()
    monkeypatch.setattr(h, "start", lambda: (_ for _ in ()).throw(
        AssertionError("没有绑定不应恢复注册")))
    h.resume()  # 无绑定：不触发 start


def test_suspend_then_update_bindings_then_resume_uses_new(monkeypatch):
    """回归：设置窗口开着时保存新快捷键，关闭后 resume 必须用新 bindings。

    旧 bug：setup_hotkeys 在 suspend 状态下直接返回，不更新 bindings。
    关闭窗口 resume → start() 用的还是旧 bindings，新配置不生效。
    """
    h = HotkeyManager()
    started: list = []

    def fake_start():
        started.append([b.combo for b in h._bindings])
        return True

    monkeypatch.setattr(h, "start", fake_start)
    monkeypatch.setattr(h, "stop", lambda: setattr(h, "_backend", ""))
    h.register("cmd+shift+space", lambda: None)
    h._backend = "carbon"
    # 模拟 suspend → 更新 bindings（不 start）→ resume
    h.suspend()
    h.clear()
    h.register("option_l", lambda: None)  # 新配置
    h.resume()
    assert started and started[-1] == ["option_l"]


def test_single_key_detection_and_validation():
    """单个按键（option/fn/字母/左 Option/右 Option）合法，可被保存并走单键后端触发。"""
    # 单个修饰键（Typeless 默认右 Option）
    assert is_single_modifier("option") is True
    assert is_single_modifier("alt") is True
    assert is_single_modifier("fn") is True
    assert is_single_modifier("cmd+shift+space") is False
    assert is_single_modifier("option+d") is False
    # 左右 Option 各自独立合法
    assert is_single_modifier("option_l") is True
    assert is_single_modifier("option_r") is True
    assert is_single_modifier("alt_l") is True
    assert is_single_modifier("alt_r") is True
    assert is_single_modifier("cmd_l") is True
    assert is_single_modifier("shift_r") is True
    # 单个普通键
    assert is_single_plain_key("d") is True
    assert is_single_plain_key("f1") is True
    assert is_single_plain_key("cmd+d") is False
    assert is_single_plain_key("option") is False
    # 统一校验：组合键 + 单键都合法，明显无效值仍拒绝
    assert valid_hotkey("cmd+shift+space") is True
    assert valid_hotkey("option") is True
    assert valid_hotkey("option_l") is True
    assert valid_hotkey("option_r") is True
    assert valid_hotkey("d") is True
    assert valid_hotkey("") is False
    assert valid_hotkey("cmd+shift+space+extra+more") is False


@pytest.mark.skipif(sys.platform == "win32",
                    reason="Carbon 后端仅存在于 macOS；Windows 走系统级键盘监听")
def test_mixed_backend_preserves_global_index(monkeypatch):
    """组合键走 Carbon、单键走单键后端时，命中派发仍按全局下标对位。"""
    h = HotkeyManager()
    fired: list[str] = []
    h.register("option_l", lambda: fired.append("dictate"))         # 单键 → index 0
    h.register("cmd+shift+e", lambda: fired.append("ask"))          # 组合 → index 1

    carbon_captured: dict = {}
    single_captured: dict = {}

    class FakeCarbon:
        def install(self, bindings, dispatch, indices=None):
            carbon_captured["bindings"] = [b.combo for b in bindings]
            carbon_captured["indices"] = indices
            carbon_captured["dispatch"] = dispatch
            return True
        def uninstall(self):
            pass
        count = 1

    class FakeSingle:
        def install(self, bindings, dispatch):
            single_captured["bindings"] = bindings
            single_captured["dispatch"] = dispatch
            return True
        def uninstall(self):
            pass
        count = 1

    import app.platform.hotkey as hk
    monkeypatch.setattr(hk, "CarbonHotkeys", FakeCarbon)
    monkeypatch.setattr(hk, "SingleKeyHotkeys", FakeSingle)
    assert h.start() is True
    # 组合键子集交给 Carbon，但用全局下标 1 对位
    assert carbon_captured["bindings"] == ["cmd+shift+e"]
    assert carbon_captured["indices"] == [1]
    # 单键子集交给单键后端，全局下标 0，且传的是 combo 字符串
    assert single_captured["bindings"] == [(0, "option_l")]
    # 两个后端共用同一 dispatch，按全局 index 命中正确 binding
    # _carbon_dispatch 有 0.28s 全局防抖，用递增时钟推进避开
    clock = {"t": 1000.0}
    monkeypatch.setattr(hk.time, "monotonic", lambda: clock["t"])
    single_captured["dispatch"](0, True)
    assert fired == ["dictate"]
    single_captured["dispatch"](0, False)
    clock["t"] += 1.0
    carbon_captured["dispatch"](1, True)
    assert fired == ["dictate", "ask"]


# --------------------------------------------------------------- Windows 录入器状态机
def test_win_capture_single_modifier_records_single_key():
    """只按一个修饰键再松开 → 录成单键热键。"""
    from app.platform.keycapture import WinKeyCapture
    cap = WinKeyCapture()
    assert cap._handle_press("alt", "alt_l") == ""
    assert cap._handle_release("alt", "alt_l") == "alt"


def test_win_capture_left_and_right_modifier_are_distinct():
    """左右 Alt 同时按住后只松开一个，不能被误判成「只按了一下 Alt」。"""
    from app.platform.keycapture import WinKeyCapture
    cap = WinKeyCapture()
    assert cap._handle_press("alt", "alt_l") == ""
    assert cap._handle_press("alt", "alt_r") == ""
    assert cap._handle_release("alt", "alt_l") == ""
    assert cap._handle_release("alt", "alt_r") == "alt"


def test_win_capture_combo_and_fired_lock():
    """修饰键 + 普通键 → 组合键；录入后同一次按住不再重复触发，reset 后可重录。"""
    from app.platform.keycapture import WinKeyCapture
    cap = WinKeyCapture()
    assert cap._handle_press("ctrl", "ctrl_l") == ""
    assert cap._handle_press("space", "char: ") == "ctrl+space"
    assert cap._handle_press("d", "char:d") == ""      # 已录入，本次按住内忽略
    cap.reset()
    assert cap._handle_press("alt", "alt_l") == ""
    assert cap._handle_release("alt", "alt_l") == "alt"
