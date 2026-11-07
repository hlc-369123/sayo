"""前台应用上下文与选中文字读取（全部本机 API，无网络）。"""
from __future__ import annotations

import subprocess
import sys
import time

from ..core.pipeline import AppContext
from .inject import Clipboard, send_copy


def _mac_frontmost() -> tuple[str, str]:
    try:
        from AppKit import NSWorkspace  # type: ignore
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        return str(app.localizedName() or ""), str(app.bundleIdentifier() or "")
    except Exception:
        return "", ""


def _win_frontmost() -> tuple[str, str]:
    try:
        import ctypes
        from ctypes import wintypes  # type: ignore
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        buf = ctypes.create_unicode_buffer(512)
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, 512)
        pid = wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        name = buf.value.strip()
        return name, f"pid:{pid.value}"
    except Exception:
        return "", ""


def _linux_frontmost() -> tuple[str, str]:
    try:
        title = subprocess.check_output(
            ["xdotool", "getactivewindow", "getwindowname"], timeout=1, stderr=subprocess.DEVNULL
        ).decode("utf-8", "ignore").strip()
    except Exception:
        title = ""
    try:
        cls = subprocess.check_output(
            ["xprop", "-id", subprocess.check_output(["xdotool", "getactivewindow"], timeout=1).decode().strip(),
             "WM_CLASS"], timeout=1, stderr=subprocess.DEVNULL
        ).decode("utf-8", "ignore")
    except Exception:
        cls = ""
    return title, cls.strip()


def frontmost_app() -> tuple[str, str]:
    if sys.platform == "darwin":
        return _mac_frontmost()
    if sys.platform == "win32":
        return _win_frontmost()
    return _linux_frontmost()


def current_context(with_selection: bool = False) -> AppContext:
    name, bundle = frontmost_app()
    selected = ""
    if with_selection:
        clip = Clipboard()
        previous = clip.read()
        send_copy()
        time.sleep(0.12)
        selected = clip.read()
        if selected == previous:
            selected = ""  # 未选中文字时剪贴板不变
        clip.write(previous)
    return AppContext(app_name=name, bundle_id=bundle, selected_text=selected)
