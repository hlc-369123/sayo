"""语音输入时临时静音系统输出（macOS CoreAudio，纯本机、零新依赖）。

「语音输入时静音其他活动音频」：
  · 开始听写 → 把默认输出设备静音（记住原状态）
  · 结束/取消听写 → 恢复原状态

设计原则：静音是锦上添花，任何失败都只写本机日志，绝不影响听写主流程。
不监听设备变更、不引入 pyobjc 依赖：直接 ctypes 调 CoreAudio C API。
"""
from __future__ import annotations

import ctypes
import ctypes.util
import datetime as _dt
import struct
import threading

from ..config import logs_dir

_LIB = None
_LOCK = threading.Lock()
_SAVED: dict[int, bool] = {}  # device_id -> 静音前的原状态

_kSystemObject = 1
_kDefaultOutput = struct.unpack(">I", b"dOut")[0]  # kAudioHardwarePropertyDefaultOutputDevice
_kMute = struct.unpack(">I", b"mute")[0]           # kAudioDevicePropertyMute
_kScopeOutput = struct.unpack(">I", b"outp")[0]    # kAudioObjectPropertyScopeOutput


class _PropAddress(ctypes.Structure):
    _fields_ = [("selector", ctypes.c_uint32),
                ("scope", ctypes.c_uint32),
                ("element", ctypes.c_uint32)]


def _coreaudio():
    global _LIB
    if _LIB is None:
        path = (ctypes.util.find_library("CoreAudio")
                or "/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
        lib = ctypes.CDLL(path)
        lib.AudioObjectGetPropertyData.argtypes = [
            ctypes.c_uint32, ctypes.POINTER(_PropAddress),
            ctypes.c_uint32, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
        lib.AudioObjectGetPropertyData.restype = ctypes.c_int32
        lib.AudioObjectSetPropertyData.argtypes = [
            ctypes.c_uint32, ctypes.POINTER(_PropAddress),
            ctypes.c_uint32, ctypes.c_void_p,
            ctypes.c_uint32, ctypes.c_void_p]
        lib.AudioObjectSetPropertyData.restype = ctypes.c_int32
        _LIB = lib
    return _LIB


def _log(msg: str) -> None:
    try:
        with (logs_dir() / "mute.log").open("a", encoding="utf-8") as fh:
            fh.write(f"{_dt.datetime.now().isoformat(timespec='seconds')} {msg}\n")
    except Exception:
        pass


def _default_output_device() -> int:
    lib = _coreaudio()
    addr = _PropAddress(_kDefaultOutput, 0, 0)
    dev = ctypes.c_uint32(0)
    size = ctypes.c_uint32(ctypes.sizeof(dev))
    status = lib.AudioObjectGetPropertyData(
        _kSystemObject, ctypes.byref(addr), 0, None, ctypes.byref(size), ctypes.byref(dev))
    if status != 0 or not dev.value:
        raise OSError(f"读取默认输出设备失败 status={status}")
    return dev.value


def _get_mute(lib, dev: int) -> bool:
    addr = _PropAddress(_kMute, _kScopeOutput, 0)
    val = ctypes.c_uint32(0)
    size = ctypes.c_uint32(ctypes.sizeof(val))
    status = lib.AudioObjectGetPropertyData(
        dev, ctypes.byref(addr), 0, None, ctypes.byref(size), ctypes.byref(val))
    if status != 0:
        raise OSError(f"读取静音状态失败 status={status}")
    return bool(val.value)


def _set_mute(lib, dev: int, on: bool) -> None:
    addr = _PropAddress(_kMute, _kScopeOutput, 0)
    val = ctypes.c_uint32(1 if on else 0)
    status = lib.AudioObjectSetPropertyData(
        dev, ctypes.byref(addr), 0, None, ctypes.sizeof(val), ctypes.byref(val))
    if status != 0:
        raise OSError(f"设置静音失败 status={status}")


def set_muted(mute: bool) -> None:
    """mute=True：静音默认输出（记住原状态）；mute=False：恢复原状态。

    幂等：重复 begin 不重复记存；未 begin 的 end 不做任何事。
    """
    with _LOCK:
        try:
            lib = _coreaudio()
            dev = _default_output_device()
            if mute:
                if dev in _SAVED:
                    return  # 已在静音会话中
                try:
                    _SAVED[dev] = _get_mute(lib, dev)
                except Exception:
                    _SAVED[dev] = False
                try:
                    _set_mute(lib, dev, True)
                    _log(f"已静音默认输出 dev={dev}（原状态={_SAVED[dev]}）")
                except Exception as exc:
                    _SAVED.pop(dev, None)
                    _log(f"静音失败（设备可能不支持 mute 属性）：{exc}")
            else:
                if not _SAVED:
                    return
                for d, was_muted in _SAVED.items():
                    try:
                        if not was_muted:
                            _set_mute(lib, d, False)
                        _log(f"恢复输出 dev={d} -> 原状态 {was_muted}")
                    except Exception as exc:
                        _log(f"恢复静音状态失败 dev={d}: {exc}")
                _SAVED.clear()
        except Exception as exc:
            _log(f"set_muted({mute}) 异常：{exc}")


class MuteSession:
    """一轮录音的延时静音会话，解决两个真实故障：

    1. 开始提示音约 0.44s，静音必须等它播完，否则设备级静音会把本应用
       自己的提示音一起掐掉（用户只听到结束音，误以为提示音反了）。
    2. 若用户在延时内就结束录音（误触、极短录音），延时回调不能再静音，
       否则系统输出永久保持静音——这正是旧的 `QTimer.singleShot(320, ...)`
       无复查导致的「按一下快捷键后电脑一直没声音」。

    用 generation token 而不是持有 QTimer 句柄：PySide 的 singleShot 不返回
    可 stop 的句柄，因此让每个延时回调带上发起时的 token，会话结束/重开后
    token 失效，回调自然变成空操作。线程安全。
    """

    def __init__(self, apply_fn=None):
        self._apply = apply_fn or set_muted
        self._lock = threading.Lock()
        self._generation = 0
        self._active = False

    def begin(self) -> int:
        """开启一轮录音，返回本轮 token，交给延时回调。"""
        with self._lock:
            self._generation += 1
            self._active = True
            return self._generation

    def end(self) -> None:
        """结束录音：立刻使未触发的延时失效并恢复输出（幂等）。"""
        with self._lock:
            self._generation += 1
            self._active = False
            apply_fn = self._apply
        try:
            apply_fn(False)
        except Exception as exc:
            _log(f"MuteSession.end 恢复失败：{exc}")

    def apply(self, token: int) -> None:
        """延时回调：只有 token 仍属于当前活跃会话才真正静音。"""
        with self._lock:
            if not self._active or token != self._generation:
                return
            apply_fn = self._apply
        try:
            apply_fn(True)
        except Exception as exc:
            _log(f"MuteSession.apply 静音失败：{exc}")
