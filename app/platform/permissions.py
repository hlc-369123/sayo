"""本机权限自检与【主动弹出系统授权弹窗】（macOS / Windows / Linux）。

只调用本机系统 API，不向任何服务器校验权限、不读取任何远程地址。
macOS 权限可由应用主动触发系统弹窗：
  · 麦克风      AVFoundation AVCaptureDevice requestAccess
  · 辅助功能    ApplicationServices AXIsProcessTrustedWithOptions(prompt=True)
  · 输入监控    IOKit IOHIDRequestAccess（只有回退到键盘监听时才需要）
后两者可能同步等待用户点击，调用方应使用 request_all_async() 放到后台线程。
全局快捷键走 Carbon RegisterEventHotKey，不需要「输入监控」，因此常规启动
只申请麦克风与辅助功能，避免系统弹窗抢走用户正在输入的焦点。
"""
from __future__ import annotations

import ctypes
import subprocess
import sys
import threading
from dataclasses import dataclass

IOKIT_LISTEN_EVENT = 1        # kIOHIDRequestTypeListenEvent
IOHID_ACCESS_GRANTED = 0      # kIOHIDAccessTypeGranted
APP_SERVICES = "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
IOKIT = "/System/Library/Frameworks/IOKit.framework/IOKit"


@dataclass
class PermissionState:
    key: str
    label: str
    granted: bool | None  # None = 该平台无此权限概念 / 无法查询
    hint: str


def _mac_accessibility() -> bool:
    try:
        lib = ctypes.cdll.LoadLibrary(APP_SERVICES)
        lib.AXIsProcessTrusted.restype = ctypes.c_bool
        return bool(lib.AXIsProcessTrusted())
    except Exception:
        return False


def _mac_microphone() -> bool | None:
    try:
        from AVFoundation import AVCaptureDevice, AVMediaTypeAudio  # type: ignore
        return int(AVCaptureDevice.authorizationStatusForMediaType_(AVMediaTypeAudio)) == 3
    except Exception:
        return None


def _mac_hid_access() -> int | None:
    """IOHIDCheckAccess: 0=已授权 1=被拒绝 2=未决定/无法查询。"""
    try:
        lib = ctypes.cdll.LoadLibrary(IOKIT)
        fn = lib.IOHIDCheckAccess
        fn.argtypes = [ctypes.c_uint32]
        fn.restype = ctypes.c_uint32
        return int(fn(IOKIT_LISTEN_EVENT))
    except Exception:
        return None


def accessibility_granted() -> bool | None:
    """供输出链路查询：辅助功能未授权时，文字只能放进剪贴板。"""
    if sys.platform != "darwin":
        return None  # 非 macOS 无此概念
    return _mac_accessibility()


def check() -> list[PermissionState]:
    if sys.platform == "darwin":
        mic = _mac_microphone()
        hid = _mac_hid_access()
        return [
            PermissionState("microphone", "麦克风", mic,
                            "首次启动时系统会弹出授权框；也可在此一键重新申请"),
            PermissionState("accessibility", "辅助功能", _mac_accessibility(),
                            "用于把文字写入当前输入框；系统弹窗会引导到隐私与安全性"),
            PermissionState("input_monitoring", "输入监控（仅回退后端需要）",
                            None if hid is None else hid == IOHID_ACCESS_GRANTED,
                            "系统级 Carbon 热键不需要此项；只有回退到键盘监听时才需要勾选"),
        ]
    if sys.platform == "win32":
        return [
            PermissionState("microphone", "麦克风", None,
                            "设置 → 隐私和安全性 → 麦克风，允许桌面应用访问"),
            PermissionState("accessibility", "辅助功能", None,
                            "无需额外授权；若被安全软件拦截，请允许本应用模拟按键"),
        ]
    return [
        PermissionState("microphone", "麦克风", None, "确保当前用户在 audio 组，且 PulseAudio/PipeWire 正常运行"),
        PermissionState("accessibility", "辅助功能", None, "X11 需 xdotool，Wayland 需开启相应门户权限"),
    ]


def open_settings(which: str) -> None:
    """调起本机系统设置面板（不联网）。"""
    if sys.platform == "darwin":
        panel = {
            "microphone": "Privacy_Microphone",
            "accessibility": "Privacy_Accessibility",
            "input_monitoring": "Privacy_ListenEvent",
        }.get(which, "Privacy")
        subprocess.Popen(
            ["open", f"x-apple.systempreferences:com.apple.preference.security?{panel}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    elif sys.platform == "win32":
        subprocess.Popen(["start", "ms-settings:privacy-microphone"], shell=True)


# ------------------------------------------------------------------ 主动弹窗
def _prompt_microphone() -> str:
    try:
        from AVFoundation import AVCaptureDevice, AVMediaTypeAudio  # type: ignore
        AVCaptureDevice.requestAccessForMediaType_completionHandler_(AVMediaTypeAudio, lambda ok: None)
        return "已弹出系统麦克风授权框"
    except Exception:
        try:
            import sounddevice as sd  # type: ignore
            with sd.InputStream(samplerate=16000, channels=1, dtype="float32", blocksize=1024):
                pass
            return "已通过打开录音流触发系统授权框"
        except Exception as exc:
            return f"弹窗失败（{type(exc).__name__}），请手动打开本机设置"


def _prompt_accessibility() -> str:
    try:
        from ApplicationServices import (  # type: ignore
            AXIsProcessTrustedWithOptions, kAXTrustedCheckOptionPrompt,
        )
        AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True})
        return "已弹出系统辅助功能授权框"
    except Exception:
        try:
            import objc  # type: ignore
            from Foundation import NSDictionary  # type: ignore
            opts = NSDictionary.dictionaryWithObject_forKey_(True, "AXTrustedCheckOptionPrompt")
            lib = ctypes.cdll.LoadLibrary(APP_SERVICES)
            fn = lib.AXIsProcessTrustedWithOptions
            fn.argtypes = [ctypes.c_void_p]
            fn.restype = ctypes.c_bool
            fn(ctypes.c_void_p(objc.pyobjc_id(opts)))
            return "已弹出系统辅助功能授权框"
        except Exception as exc:
            return f"弹窗失败（{type(exc).__name__}），请手动打开本机设置"


def _prompt_input_monitoring() -> str:
    try:
        lib = ctypes.cdll.LoadLibrary(IOKIT)
        fn = lib.IOHIDRequestAccess
        fn.argtypes = [ctypes.c_uint32]
        fn.restype = ctypes.c_bool
        granted = bool(fn(IOKIT_LISTEN_EVENT))
        if granted:
            return "输入监控已授权"
        # 用户曾拒绝时系统不会再弹窗，直接把本机设置面板带到前台
        open_settings("input_monitoring")
        return "系统未再弹窗（可能曾被拒绝），已打开输入监控设置面板"
    except Exception as exc:
        open_settings("input_monitoring")
        return f"弹窗失败（{type(exc).__name__}），已打开输入监控设置面板"


def request_all(force: bool = False, include_input_monitoring: bool = True) -> dict[str, str]:
    """同步申请所有缺失权限；可能阻塞，调用方应放到后台线程。

    force=True 时对已授权项也重新发起（用于「一键重新申请」按钮）。
    include_input_monitoring=False 用于常规启动：新的 Carbon 系统热键不需要
    输入监控，跳过它就不会弹出会抢走输入框光标的系统授权框。
    """
    out: dict[str, str] = {}
    if sys.platform != "darwin":
        out["microphone"] = "桌面系统会在首次录音时由系统自行弹窗"
        return out
    if force or _mac_microphone() is not True:
        out["microphone"] = _prompt_microphone()
    else:
        out["microphone"] = "已授权，无需弹窗"
    if force or not _mac_accessibility():
        out["accessibility"] = _prompt_accessibility()
    else:
        out["accessibility"] = "已授权，无需弹窗"
    if include_input_monitoring:
        hid = _mac_hid_access()
        if force or hid != IOHID_ACCESS_GRANTED:
            out["input_monitoring"] = _prompt_input_monitoring()
        else:
            out["input_monitoring"] = "已授权，无需弹窗"
    else:
        out["input_monitoring"] = "系统热键无需此权限，暂不申请"
    return out


def request_all_async(force: bool = False, callback=None,
                      include_input_monitoring: bool = True) -> None:
    """后台申请，避免系统弹窗阻塞界面线程。"""
    def worker():
        try:
            result = request_all(force=force,
                                 include_input_monitoring=include_input_monitoring)
        except Exception as exc:
            result = {"error": str(exc)}
        if callback:
            try:
                callback(result)
            except Exception:
                pass
    threading.Thread(target=worker, daemon=True, name="permission-prompt").start()


def request_microphone() -> None:
    """兼容旧入口：只触发麦克风弹窗。"""
    _prompt_microphone()
