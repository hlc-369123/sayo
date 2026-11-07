"""全局快捷键：任意应用中按下组合键开始/结束听写（纯本机事件）。

macOS 主后端：Carbon ``RegisterEventHotKey`` + ``InstallEventHandler``。
    系统直接把热键事件投递给本应用，**不需要「输入监控」也不需要「辅助功能」**。
    以前用 pynput / CGEventTap 监听全局按键，必须用户手动勾选输入监控；
    ad-hoc 签名的应用每次覆盖安装都会让该授权失效，于是「按了没反应」反复出现。
    Carbon 热键是系统级注册（MASShortcut、系统自带快捷键面板同款机制），
    不读取键盘事件流，因此不受该权限约束，重建/升级后依然可用。

回退后端：pynput。只在 Carbon 注册失败（例如组合键被别的应用占用、
键名无法映射到虚拟键码、或非 macOS 平台）时启用；此时才需要输入监控授权。

本模块同时提供 event_tap_available() / HotkeyManager.health()，
用于在应用内自查「为什么按了快捷键没反应」。所有检查都是本机查询，不联网。
"""
from __future__ import annotations

import ctypes
import sys
import threading
import time
from dataclasses import dataclass

# 左/右修饰键在 macOS 上是不同的 Key（cmd_r / shift_r / alt_r / ctrl_r），
# 必须统一归一化，否则用户按右侧 Command 时组合键永远匹配不上。
MOD_ALIASES = {
    "cmd": "cmd", "command": "cmd", "meta": "cmd", "super": "cmd", "win": "cmd",
    "cmd_l": "cmd", "cmd_r": "cmd", "command_l": "cmd", "command_r": "cmd",
    "ctrl": "ctrl", "control": "ctrl", "ctl": "ctrl",
    "ctrl_l": "ctrl", "ctrl_r": "ctrl",
    "alt": "alt", "option": "alt", "opt": "alt",
    "alt_l": "alt", "alt_r": "alt", "alt_gr": "alt",
    "option_l": "alt", "option_r": "alt",
    "shift": "shift", "shift_l": "shift", "shift_r": "shift",
    "space": "space", "spacebar": "space", "空格": "space", " ": "space",
    "esc": "esc", "escape": "esc", "return": "enter", "enter": "enter",
}

MODIFIER_KEYS = frozenset({"cmd", "ctrl", "alt", "shift", "fn"})


def normalize_key(name: str) -> str:
    """把 pynput 的键名与用户输入的键名统一到同一套别名。"""
    n = (name or "").strip().lower()
    return MOD_ALIASES.get(n, n)


def parse_combo(combo: str) -> frozenset[str]:
    parts = [p.strip().lower() for p in str(combo).split("+") if p.strip()]
    return frozenset(normalize_key(p) for p in parts)


# ---------------------------------------------------------------- Carbon 热键
CARBON_PATH = "/System/Library/Frameworks/Carbon.framework/Carbon"
CARBON_MODIFIERS = {
    "cmd": 0x0100,    # cmdKey
    "shift": 0x0200,  # shiftKey
    "alt": 0x0800,    # optionKey
    "ctrl": 0x1000,   # controlKey
}

# macOS 虚拟键码（Carbon HIToolbox kVK_*），只需覆盖组合键常用键。
KEYCODES: dict[str, int] = {
    "space": 49, "enter": 36, "return": 36, "tab": 48, "esc": 53,
    "backspace": 51, "delete": 51, "del": 51, "home": 115, "end": 119,
    "pageup": 116, "pagedown": 121, "up": 126, "down": 125,
    "left": 123, "right": 124,
    "a": 0, "b": 11, "c": 8, "d": 2, "e": 14, "f": 3, "g": 5, "h": 4,
    "i": 34, "j": 38, "k": 40, "l": 37, "m": 46, "n": 45, "o": 31,
    "p": 35, "q": 12, "r": 15, "s": 1, "t": 17, "u": 32, "v": 9,
    "w": 13, "x": 7, "y": 16, "z": 6,
    "0": 29, "1": 18, "2": 19, "3": 20, "4": 21, "5": 23, "6": 22,
    "7": 26, "8": 28, "9": 25,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97,
    "f7": 98, "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111,
    "-": 27, "=": 24, "[": 33, "]": 30, "\\": 42, ";": 41, "'": 39,
    ",": 43, ".": 47, "/": 44, "`": 10,
}


def carbon_combo(combo: str) -> tuple[int, int] | None:
    """把 'cmd+shift+space' 翻译成 (虚拟键码, Carbon 修饰键掩码)。

    返回 None 表示这个组合无法用系统热键注册（缺普通键、多个普通键、
    键名未知或没有修饰键），调用方应回退到 pynput。
    """
    keys = parse_combo(combo)
    plain = [k for k in keys if k not in MODIFIER_KEYS]
    if len(plain) != 1:
        return None
    code = KEYCODES.get(plain[0])
    if code is None:
        return None
    mods = 0
    for k in keys & MODIFIER_KEYS:
        mods |= CARBON_MODIFIERS.get(k, 0)
    if not mods:  # 不允许把单个普通键注册成全局热键
        return None
    return code, mods


def _fourcc(text: str) -> int:
    return int.from_bytes(text.encode("ascii"), "big")


class _EventHotKeyID(ctypes.Structure):
    _fields_ = [("signature", ctypes.c_uint32), ("id", ctypes.c_uint32)]


class _EventTypeSpec(ctypes.Structure):
    _fields_ = [("eventClass", ctypes.c_uint32), ("eventKind", ctypes.c_uint32)]


_EVENT_HANDLER_PROC = ctypes.CFUNCTYPE(
    ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
)

_K_EVENT_CLASS_KEYBOARD = _fourcc("keyb")
_K_EVENT_HOTKEY_PRESSED = 5
_K_EVENT_HOTKEY_RELEASED = 6
_K_EVENT_PARAM_DIRECT_OBJECT = _fourcc("----")
_TYPE_EVENT_HOTKEY_ID = _fourcc("hkid")
_SIGNATURE = _fourcc("QTTY")


class CarbonHotkeys:
    """系统级热键（免授权）。必须在主线程 install（Carbon 事件目标绑定主线程）。"""

    def __init__(self):
        self._lib = None
        self._callback = None           # 必须长期持有，防止被 GC 后野指针回调
        self._handler_ref = None
        self._hotkey_refs: list[ctypes.c_void_p] = []
        self._dispatch = None
        self.error = ""
        self.count = 0

    def install(self, bindings, dispatch) -> bool:
        self.error = ""
        if sys.platform != "darwin":
            self.error = "非 macOS 平台"
            return False
        try:
            lib = ctypes.cdll.LoadLibrary(CARBON_PATH)
        except OSError as exc:
            self.error = f"无法加载 Carbon 框架：{exc}"
            return False

        lib.GetApplicationEventTarget.restype = ctypes.c_void_p
        lib.GetEventParameter.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32), ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p,
        ]
        lib.GetEventParameter.restype = ctypes.c_int32
        lib.GetEventKind.argtypes = [ctypes.c_void_p]
        lib.GetEventKind.restype = ctypes.c_uint32
        lib.InstallEventHandler.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
            ctypes.POINTER(_EventTypeSpec), ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        lib.InstallEventHandler.restype = ctypes.c_int32
        lib.RegisterEventHotKey.argtypes = [
            ctypes.c_uint32, ctypes.c_uint32, _EventHotKeyID,
            ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p),
        ]
        lib.RegisterEventHotKey.restype = ctypes.c_int32
        lib.UnregisterEventHotKey.argtypes = [ctypes.c_void_p]
        lib.UnregisterEventHotKey.restype = ctypes.c_int32
        lib.RemoveEventHandler.argtypes = [ctypes.c_void_p]
        lib.RemoveEventHandler.restype = ctypes.c_int32

        self._lib = lib
        self._dispatch = dispatch
        self._callback = _EVENT_HANDLER_PROC(self._handle)

        target = lib.GetApplicationEventTarget()
        if not target:
            self._callback = None
            self.error = "无法取得应用事件目标（应用尚未连接窗口服务器）"
            return False
        spec = (_EventTypeSpec * 2)(
            _EventTypeSpec(_K_EVENT_CLASS_KEYBOARD, _K_EVENT_HOTKEY_PRESSED),
            _EventTypeSpec(_K_EVENT_CLASS_KEYBOARD, _K_EVENT_HOTKEY_RELEASED),
        )
        href = ctypes.c_void_p()
        status = lib.InstallEventHandler(
            target, ctypes.cast(self._callback, ctypes.c_void_p),
            2, spec, None, ctypes.byref(href))
        if status != 0:
            self._callback = None
            self.error = f"安装系统热键处理器失败（OSStatus {status}）"
            return False
        self._handler_ref = href

        for index, binding in enumerate(bindings):
            parsed = carbon_combo(binding.combo)
            if parsed is None:
                self.uninstall()
                self.error = f"组合键 {binding.combo} 无法注册为系统热键"
                return False
            code, mods = parsed
            ref = ctypes.c_void_p()
            status = lib.RegisterEventHotKey(
                code, mods, _EventHotKeyID(_SIGNATURE, index + 1),
                target, 0, ctypes.byref(ref))
            if status != 0:
                self.uninstall()
                self.error = (f"系统拒绝注册 {binding.combo}（OSStatus {status}）；"
                              "该组合键可能已被其他应用或系统占用")
                return False
            self._hotkey_refs.append(ref)
        self.count = len(self._hotkey_refs)
        return True

    def _handle(self, _call_ref, event, _user_data) -> int:
        try:
            lib = self._lib
            hkid = _EventHotKeyID()
            actual_type = ctypes.c_uint32(0)
            actual_size = ctypes.c_uint32(0)
            status = lib.GetEventParameter(
                event, _K_EVENT_PARAM_DIRECT_OBJECT, _TYPE_EVENT_HOTKEY_ID,
                ctypes.byref(actual_type), ctypes.sizeof(hkid),
                ctypes.byref(actual_size), ctypes.byref(hkid))
            if status != 0:
                return 0
            index = int(hkid.id) - 1
            kind = int(lib.GetEventKind(event))
            if self._dispatch and index >= 0:
                self._dispatch(index, kind == _K_EVENT_HOTKEY_PRESSED)
        except Exception:
            pass  # C 回调里绝不能抛异常
        return 0

    def uninstall(self) -> None:
        lib = self._lib
        if lib:
            for ref in self._hotkey_refs:
                try:
                    lib.UnregisterEventHotKey(ref)
                except Exception:
                    pass
            if self._handler_ref:
                try:
                    lib.RemoveEventHandler(self._handler_ref)
                except Exception:
                    pass
        self._hotkey_refs = []
        self._handler_ref = None
        self._callback = None
        self._dispatch = None
        self.count = 0


# ------------------------------------------------------------ pynput 回退探测
def event_tap_available() -> tuple[bool, str]:
    """真实探测本进程能否收到全局按键事件（pynput 回退后端的能力前提）。

    Carbon 热键不需要这个能力；只有回退到 pynput 时才有意义。
    """
    if sys.platform != "darwin":
        return True, "非 macOS 平台无需输入监控权限"
    try:
        import Quartz  # type: ignore

        mask = Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown)

        def _callback(proxy, event_type, event, refcon):
            return event

        tap = Quartz.CGEventTapCreate(
            Quartz.kCGSessionEventTap,
            Quartz.kCGHeadInsertEventTap,
            Quartz.kCGEventTapOptionListenOnly,
            mask,
            _callback,
            None,
        )
        if tap is None:
            return False, "系统拒绝创建全局键盘监听（输入监控未授权）"
        enabled = bool(Quartz.CGEventTapIsEnabled(tap))
        try:
            Quartz.CFMachPortInvalidate(tap)
        except Exception:
            pass
        return (enabled,
                "可创建并启用全局键盘监听" if enabled
                else "事件监听被系统禁用（输入监控未授权）")
    except Exception as exc:
        return False, f"事件监听检测失败：{type(exc).__name__}: {exc}"


@dataclass
class Binding:
    combo: str
    keys: frozenset[str]
    on_press: object
    on_release: object = None
    active: bool = False


class HotkeyManager:
    def __init__(self):
        self._bindings: list[Binding] = []
        self._pressed: set[str] = set()
        self._lock = threading.Lock()
        self._listener = None
        self._carbon: CarbonHotkeys | None = None
        self._carbon_error = ""
        self._backend = ""
        self._error = ""
        self._last_hit: str = ""
        self._last_fire = 0.0

    @property
    def error(self) -> str:
        return self._error

    @property
    def last_hit(self) -> str:
        return self._last_hit

    @property
    def backend(self) -> str:
        return self._backend

    @property
    def carbon_error(self) -> str:
        return self._carbon_error

    def register(self, combo: str, on_press, on_release=None) -> None:
        self._bindings.append(Binding(combo, parse_combo(combo), on_press, on_release))

    def clear(self) -> None:
        self._bindings.clear()

    @staticmethod
    def _canonical(key) -> str:
        from pynput.keyboard import Key  # type: ignore
        if isinstance(key, Key):
            return normalize_key(getattr(key, "name", None) or str(key))
        char = getattr(key, "char", None)
        if char:
            return "space" if char == " " else char.lower()
        return normalize_key(str(key).strip("'"))

    # ---- Carbon 后端：系统直接分发，按一次触发一次，不读取键盘流
    def _carbon_dispatch(self, index: int, pressed: bool) -> None:
        with self._lock:
            if index >= len(self._bindings):
                return
            binding = self._bindings[index]
            now = time.monotonic()
            if pressed:
                if binding.active or now - self._last_fire < 0.28:
                    return
                binding.active = True
                self._last_fire = now
                self._last_hit = binding.combo
                fn = binding.on_press
            else:
                if not binding.active:
                    return
                binding.active = False
                fn = binding.on_release
            if fn:
                self._invoke(fn)

    # ---- pynput 回退后端
    def _on_press(self, key) -> None:
        name = self._canonical(key)
        with self._lock:
            self._pressed.add(name)
            for b in self._bindings:
                if not b.active and b.keys.issubset(self._pressed):
                    b.active = True
                    self._last_hit = b.combo
                    if b.on_press:
                        self._invoke(b.on_press)

    def _on_release(self, key) -> None:
        name = self._canonical(key)
        with self._lock:
            self._pressed.discard(name)
            for b in self._bindings:
                if b.active and not b.keys.issubset(self._pressed):
                    b.active = False
                    if b.on_release:
                        self._invoke(b.on_release)

    @staticmethod
    def _invoke(fn) -> None:
        try:
            threading.Thread(target=fn, daemon=True).start()
        except Exception:
            pass

    def start(self) -> bool:
        """优先注册免授权的系统热键；只有失败时才回退到需要权限的键盘监听。"""
        self.stop()
        if sys.platform == "darwin" and self._bindings:
            carbon = CarbonHotkeys()
            if carbon.install(list(self._bindings), self._carbon_dispatch):
                self._carbon = carbon
                self._backend = "carbon"
                self._carbon_error = ""
                self._error = ""
                return True
            self._carbon_error = carbon.error
        try:
            from pynput.keyboard import Listener  # type: ignore
            self._listener = Listener(on_press=self._on_press, on_release=self._on_release)
            self._listener.daemon = True
            self._listener.start()
            tap_ok, tap_detail = event_tap_available()
            self._backend = "pynput"
            self._error = "" if tap_ok else (
                f"系统热键注册失败（{self._carbon_error}），"
                f"回退到键盘监听但输入监控未授权：{tap_detail}")
            return True
        except Exception as exc:
            self._backend = ""
            self._error = (f"系统热键注册失败（{self._carbon_error}）；"
                           f"键盘监听也不可用：{exc}；请从菜单栏手动开始听写")
            return False

    def stop(self) -> None:
        if self._carbon is not None:
            try:
                self._carbon.uninstall()
            except Exception:
                pass
            self._carbon = None
        listener, self._listener = self._listener, None
        with self._lock:
            self._pressed.clear()
            for b in self._bindings:
                b.active = False
        if listener:
            try:
                listener.stop()
            except Exception:
                pass
        self._backend = ""

    def health(self) -> dict:
        """给 UI / 日志用的热键健康状态（全部本机查询，不联网）。"""
        if self._backend == "carbon":
            return {
                "listener_alive": True,
                "tap_ok": True,
                "tap_detail": "Carbon 系统热键：系统直接分发，无需输入监控权限",
                "error": "",
                "last_hit": self._last_hit,
                "bindings": self.describe(),
                "healthy": True,
                "backend": "carbon",
                "carbon_error": "",
                "registered": self._carbon.count if self._carbon else 0,
            }
        alive = bool(self._listener is not None and self._listener.is_alive())
        tap_ok, tap_detail = event_tap_available()
        return {
            "listener_alive": alive,
            "tap_ok": tap_ok,
            "tap_detail": tap_detail,
            "error": self._error,
            "last_hit": self._last_hit,
            "bindings": self.describe(),
            "healthy": bool(alive and tap_ok and not self._error),
            "backend": self._backend or "none",
            "carbon_error": self._carbon_error,
            "registered": 0,
        }

    def describe(self) -> str:
        return "；".join(b.combo for b in self._bindings) or "未设置"


def default_combo(platform: str = sys.platform) -> str:
    return "cmd+shift+space" if platform == "darwin" else "ctrl+shift+space"
