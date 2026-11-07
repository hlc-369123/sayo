"""把文字写入当前光标位置：剪贴板 + 本机按键事件；可选恢复原剪贴板。

macOS 使用 Quartz CGEvent（不依赖第三方键盘库）；Windows/Linux 使用 pynput。
所有操作均在系统本地完成。
"""
from __future__ import annotations

import sys
import time

MAC_KEYCODE = {"cmd": 0x37, "ctrl": 0x3B, "alt": 0x3A, "shift": 0x38, "v": 0x09, "c": 0x08, "a": 0x00}


class Clipboard:
    @staticmethod
    def read() -> str:
        try:
            import pyperclip
            return pyperclip.paste() or ""
        except Exception:
            return ""

    @staticmethod
    def write(text: str) -> None:
        try:
            import pyperclip
            pyperclip.copy(text)
        except Exception as exc:
            raise RuntimeError(f"剪贴板写入失败：{exc}") from exc


def _mac_key(code: int, down: bool, flags: int = 0) -> None:
    import Quartz  # type: ignore
    ev = Quartz.CGEventCreateKeyboardEvent(None, code, down)
    if flags:
        Quartz.CGEventSetFlags(ev, flags)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)


def _mac_paste() -> None:
    import Quartz  # type: ignore
    cmd_flag = Quartz.kCGEventFlagMaskCommand
    _mac_key(MAC_KEYCODE["cmd"], True, cmd_flag)
    _mac_key(MAC_KEYCODE["v"], True, cmd_flag)
    _mac_key(MAC_KEYCODE["v"], False, cmd_flag)
    _mac_key(MAC_KEYCODE["cmd"], False, 0)


def _mac_copy() -> None:
    import Quartz  # type: ignore
    cmd_flag = Quartz.kCGEventFlagMaskCommand
    _mac_key(MAC_KEYCODE["cmd"], True, cmd_flag)
    _mac_key(MAC_KEYCODE["c"], True, cmd_flag)
    _mac_key(MAC_KEYCODE["c"], False, cmd_flag)
    _mac_key(MAC_KEYCODE["cmd"], False, 0)


def _pynput_paste_key(char: str) -> None:
    from pynput.keyboard import Controller, Key  # type: ignore
    kb = Controller()
    mod = Key.cmd if sys.platform == "darwin" else Key.ctrl
    with kb.pressed(mod):
        kb.press(char)
        kb.release(char)


def send_paste() -> None:
    if sys.platform == "darwin":
        try:
            _mac_paste()
            return
        except Exception:
            pass
    _pynput_paste_key("v")


def send_copy() -> None:
    if sys.platform == "darwin":
        try:
            _mac_copy()
            return
        except Exception:
            pass
    _pynput_paste_key("c")


def type_text(text: str) -> None:
    """逐字输入（不支持中文，仅在纯 ASCII 且用户显式选择时使用）。"""
    from pynput.keyboard import Controller  # type: ignore
    Controller().type(text)


def insert_text(text: str, *, method: str = "paste", restore_clipboard: bool = True,
                delay: float = 0.35) -> None:
    if not text:
        return
    if method == "clipboard_only":
        Clipboard.write(text)
        return
    clip = Clipboard()
    previous = clip.read()
    clip.write(text)
    if method == "type" and text.isascii():
        type_text(text)
    else:
        send_paste()
    time.sleep(delay)  # 等待目标应用完成粘贴，再恢复剪贴板
    if restore_clipboard:
        clip.write(previous)
