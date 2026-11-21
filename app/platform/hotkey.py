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

# 左/右修饰键在 macOS 上是不同的虚拟键码（如左 Option=58、右 Option=61）。
# 组合键场景：Carbon RegisterEventHotKey 的 modifier mask 不区分左右（只有一个
# optionKey 位），所以组合键里 alt_l/alt_r 归一到 alt（见 _GENERIC_SIDE）。
# 单键场景（Typeless 式「按单个左 Option 开始听写」）：必须区分左右，否则
# 用户设了左 Option、按右 Option 也会误触发。因此 alt_l/alt_r 各自独立保留。
MOD_ALIASES = {
    "cmd": "cmd", "command": "cmd", "meta": "cmd", "super": "cmd", "win": "cmd",
    "cmd_l": "cmd_l", "command_l": "cmd_l", "meta_l": "cmd_l",
    "cmd_r": "cmd_r", "command_r": "cmd_r", "meta_r": "cmd_r",
    "ctrl": "ctrl", "control": "ctrl", "ctl": "ctrl",
    "ctrl_l": "ctrl_l", "control_l": "ctrl_l",
    "ctrl_r": "ctrl_r", "control_r": "ctrl_r",
    "alt": "alt", "option": "alt", "opt": "alt", "alt_gr": "alt",
    "alt_l": "alt_l", "option_l": "alt_l", "opt_l": "alt_l",
    "alt_r": "alt_r", "option_r": "alt_r", "opt_r": "alt_r",
    "shift": "shift", "shift_l": "shift_l", "shift_r": "shift_r",
    "space": "space", "spacebar": "space", "空格": "space", " ": "space",
    "esc": "esc", "escape": "esc", "return": "enter", "enter": "enter",
}

# 通用修饰键名（左右归一）；组合键 / Carbon 注册用这些。
_GENERIC_SIDE = {
    "cmd_l": "cmd", "cmd_r": "cmd",
    "ctrl_l": "ctrl", "ctrl_r": "ctrl",
    "alt_l": "alt", "alt_r": "alt",
    "shift_l": "shift", "shift_r": "shift",
}

MODIFIER_KEYS = frozenset({
    "cmd", "ctrl", "alt", "shift", "fn",
    "cmd_l", "cmd_r", "ctrl_l", "ctrl_r",
    "alt_l", "alt_r", "shift_l", "shift_r",
})

# macOS 虚拟键码：单个修饰键在按下时只产生 flagsChanged 事件、没有对应的
# keyDown keycode。要让「按一个 option/fn 就开始听写」生效，必须按虚拟键码
# 逐个监听 flagsChanged 的边缘（松开→按下）。
# 通用的 "alt"/"cmd" 等同时覆盖左右键码（组合键场景不区分）；左右独立的
# "alt_l"/"alt_r" 只覆盖各自那一个键码（单键场景精确匹配）。
MODIFIER_KEYCODES: dict[str, tuple[int, ...]] = {
    "cmd": (54, 55), "cmd_l": (55,), "cmd_r": (54,),       # 55=左 ⌘ 54=右 ⌘
    "ctrl": (59, 62), "ctrl_l": (59,), "ctrl_r": (62,),    # 59=左 ⌃ 62=右 ⌃
    "alt": (58, 61), "alt_l": (58,), "alt_r": (61,),       # 58=左 ⌥ 61=右 ⌥（Typeless 默认右 Option）
    "shift": (56, 60), "shift_l": (56,), "shift_r": (60,), # 56=左 ⇧ 60=右 ⇧
    "fn": (63,),                                           # 63=fn（无左右之分）
}


def is_single_modifier(combo: str) -> bool:
    """combo 是否是「恰好一个修饰键」（option / fn / cmd 等单键热键）。

    这类热键 Carbon RegisterEventHotKey 注册不了（修饰键无 keyDown keycode），
    必须走 flagsChanged 监听；Typeless 的默认口述键就是单个右 Option。
    """
    keys = parse_combo(combo)
    return len(keys) == 1 and next(iter(keys)) in MODIFIER_KEYS


def is_single_plain_key(combo: str) -> bool:
    """combo 是否是「恰好一个普通键」（单个字母/数字/功能键，无修饰键）。"""
    keys = parse_combo(combo)
    if len(keys) != 1:
        return False
    key = next(iter(keys))
    return key not in MODIFIER_KEYS and key in KEYCODES


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


def valid_hotkey(combo: str) -> bool:
    """combo 能否作为热键使用：可被 Carbon 注册的组合键，或合法单键。

    参考 Typeless「press the key or key combination」：单个修饰键（option/fn）
    与单个普通键都合法。单键走 CGEventTap 触发，组合键走 Carbon。
    """
    return (carbon_combo(combo) is not None
            or is_single_modifier(combo)
            or is_single_plain_key(combo))


def carbon_combo(combo: str) -> tuple[int, int] | None:
    """把 'cmd+shift+space' 翻译成 (虚拟键码, Carbon 修饰键掩码)。

    返回 None 表示这个组合无法用系统热键注册（缺普通键、多个普通键、
    键名未知或没有修饰键），调用方应回退到 pynput。

    组合键里的左右修饰键（alt_l/alt_r 等）归一到通用名（alt）——Carbon
    RegisterEventHotKey 的 modifier mask 不区分左右，只有一个 optionKey 位。
    单键场景（alt_l 作为独立热键）不走 carbon_combo，走 SingleKeyHotkeys。
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
        k = _GENERIC_SIDE.get(k, k)  # 组合键里左右归一（alt_l → alt）
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

    def install(self, bindings, dispatch, indices=None) -> bool:
        """注册组合键热键。indices 给出每个 binding 在全局列表里的下标，
        用于 dispatch 对位（混合后端时只把组合键子集交给 Carbon）。"""
        self.error = ""
        if indices is None:
            indices = list(range(len(bindings)))
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

        registered_any = False
        for pos, binding in enumerate(bindings):
            global_index = indices[pos] if pos < len(indices) else pos
            parsed = carbon_combo(binding.combo)
            if parsed is None:
                # 单键 binding 由 SingleKeyHotkeys 处理，不算 Carbon 的失败
                if is_single_modifier(binding.combo) or is_single_plain_key(binding.combo):
                    continue
                # 旧行为：任何一个组合无法解析就 uninstall 全部并失败——
                # 用户改坏一个键，另一个本来可用的键也跟着失效（「新旧键都不管用」）。
                # 现在只跳过无效组合，其余热键照常注册。
                self.error = f"组合键 {binding.combo} 无法注册为系统热键，已跳过"
                continue
            code, mods = parsed
            ref = ctypes.c_void_p()
            status = lib.RegisterEventHotKey(
                code, mods, _EventHotKeyID(_SIGNATURE, global_index + 1),
                target, 0, ctypes.byref(ref))
            if status != 0:
                self.error = (f"系统拒绝注册 {binding.combo}（OSStatus {status}）；"
                              "该组合键可能已被其他应用或系统占用，已跳过")
                continue
            self._hotkey_refs.append(ref)
            registered_any = True
        # 热键 id 仍用原始 index，跳过若干组合不影响其余组合与 dispatch 的对位
        if bindings and not registered_any:
            self.uninstall()
            if not self.error:
                self.error = "没有任何组合键注册成功"
            return False
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


# ------------------------------------------------------------ 单键热键（CGEventTap）
# 修饰键标志位（CGEventFlags）——flagsChanged 事件里用它们判断按下/松开
_CG_FLAG_CMD = 1 << 20
_CG_FLAG_CTRL = 1 << 18
_CG_FLAG_ALT = 1 << 19
_CG_FLAG_SHIFT = 1 << 17
_CG_FLAG_FN = 1 << 23
_MOD_FLAG = {
    "cmd": _CG_FLAG_CMD, "cmd_l": _CG_FLAG_CMD, "cmd_r": _CG_FLAG_CMD,
    "ctrl": _CG_FLAG_CTRL, "ctrl_l": _CG_FLAG_CTRL, "ctrl_r": _CG_FLAG_CTRL,
    "alt": _CG_FLAG_ALT, "alt_l": _CG_FLAG_ALT, "alt_r": _CG_FLAG_ALT,
    "shift": _CG_FLAG_SHIFT, "shift_l": _CG_FLAG_SHIFT, "shift_r": _CG_FLAG_SHIFT,
    "fn": _CG_FLAG_FN,
}


class SingleKeyHotkeys:
    """监听「单个按键」热键（单个修饰键 or 单个普通键）。

    Carbon RegisterEventHotKey 要求「修饰键+普通键」，注册不了单个修饰键；
    而单个普通键注册成全局热键会吞掉正常打字，也不合适。这类热键改用
    CGEventTap 在事件流里按虚拟键码/修饰键标志判断按下与松开：
      - 单个修饰键（option/fn/cmd/…）：监听 flagsChanged，检测 松开→按下 边缘；
      - 单个普通键：监听 keyDown/keyUp，仅当无修饰键同时按下时才派发。
    回调里只做状态判断与派发，真正的动作由 HotkeyManager 在新线程执行。

    必须在主线程 install（CFRunLoop 绑定主线程，与 Qt 主循环共存）。
    """

    def __init__(self):
        self._tap = None
        self._source = None
        self._dispatch = None
        self._bindings: list = []       # [(index, combo)] 只含单键 binding
        self._mod_pressed: dict[str, bool] = {}
        self._plain_pressed: set[int] = set()
        self.error = ""
        self.count = 0

    def install(self, single_bindings, dispatch) -> bool:
        self.error = ""
        if sys.platform != "darwin" or not single_bindings:
            return False
        try:
            import Quartz  # type: ignore
        except ImportError:
            self.error = "缺少 Quartz，无法监听单键热键"
            return False

        self._bindings = list(single_bindings)  # [(index, combo)]
        self._dispatch = dispatch
        self._mod_pressed = {}
        self._plain_pressed = set()

        # 预置每个修饰键为「未按下」，避免第一次 flagsChanged 误判为松开
        for _idx, combo in self._bindings:
            if is_single_modifier(combo):
                mod = next(iter(parse_combo(combo)))
                self._mod_pressed[mod] = False

        # 需要监听的事件掩码：修饰键状态变化 + 普通键按下/抬起
        mask = (Quartz.CGEventMaskBit(Quartz.kCGEventFlagsChanged)
                | Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown)
                | Quartz.CGEventMaskBit(Quartz.kCGEventKeyUp))

        def on_event(proxy, etype, event, refcon):
            try:
                keycode = Quartz.CGEventGetIntegerValueField(
                    event, Quartz.kCGKeyboardEventKeycode)
                flags = int(Quartz.CGEventGetFlags(event))
                if etype == Quartz.kCGEventFlagsChanged:
                    for index, combo in self._bindings:
                        if not is_single_modifier(combo):
                            continue
                        mod = next(iter(parse_combo(combo)))
                        flag = _MOD_FLAG.get(mod, 0)
                        codes = MODIFIER_KEYCODES.get(mod, ())
                        # 只关心这个事件对应的修饰键（左右键码都算）
                        if keycode not in codes or not flag:
                            continue
                        down = bool(flags & flag)
                        was = self._mod_pressed.get(mod, False)
                        if down and not was:
                            self._mod_pressed[mod] = True
                            self._fire(index, True)
                        elif not down and was:
                            self._mod_pressed[mod] = False
                            self._fire(index, False)
                elif etype in (Quartz.kCGEventKeyDown, Quartz.kCGEventKeyUp):
                    modifiers_down = bool(flags & (_CG_FLAG_CMD | _CG_FLAG_CTRL
                                                   | _CG_FLAG_ALT | _CG_FLAG_SHIFT
                                                   | _CG_FLAG_FN))
                    if modifiers_down:  # 组合键交给 Carbon，不重复派发
                        return event
                    for index, combo in self._bindings:
                        if not is_single_plain_key(combo):
                            continue
                        code = KEYCODES.get(next(iter(parse_combo(combo))))
                        if code != keycode:
                            continue
                        if etype == Quartz.kCGEventKeyDown:
                            if keycode not in self._plain_pressed:
                                self._plain_pressed.add(keycode)
                                self._fire(index, True)
                        else:
                            if keycode in self._plain_pressed:
                                self._plain_pressed.discard(keycode)
                                self._fire(index, False)
            except Exception:
                pass  # C 回调绝不能抛异常
            return event

        tap = Quartz.CGEventTapCreate(
            Quartz.kCGSessionEventTap,
            Quartz.kCGHeadInsertEventTap,
            Quartz.kCGEventTapOptionListenOnly,
            mask,
            on_event,
            None,
        )
        if not tap:
            self.error = "无法创建单键监听（输入监控未授权）"
            return False
        source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
        runloop = Quartz.CFRunLoopGetCurrent()
        Quartz.CFRunLoopAddSource(runloop, source, Quartz.kCFRunLoopDefaultMode)
        Quartz.CGEventTapEnable(tap, True)
        self._tap = tap
        self._source = source
        self._runloop = runloop
        self.count = len(self._bindings)
        return True

    def _fire(self, index: int, pressed: bool) -> None:
        if self._dispatch:
            self._dispatch(index, pressed)

    def uninstall(self) -> None:
        tap, source, runloop = self._tap, self._source, getattr(self, "_runloop", None)
        self._tap = None
        self._source = None
        self._dispatch = None
        self._bindings = []
        self.count = 0
        if tap is None:
            return
        try:
            import Quartz  # type: ignore
            Quartz.CGEventTapEnable(tap, False)
            if source is not None and runloop is not None:
                Quartz.CFRunLoopRemoveSource(runloop, source, Quartz.kCFRunLoopDefaultMode)
        except Exception:
            pass


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
        self._single: SingleKeyHotkeys | None = None
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
            name = normalize_key(getattr(key, "name", None) or str(key))
            return _GENERIC_SIDE.get(name, name)  # pynput 回退只处理组合键，左右归一
        char = getattr(key, "char", None)
        if char:
            return "space" if char == " " else char.lower()
        name = normalize_key(str(key).strip("'"))
        return _GENERIC_SIDE.get(name, name)

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
        """组合键走免授权的 Carbon；单键（option/fn/单个按键）走 CGEventTap。

        两类可并存：例如听写用单个 option、Ask 用 cmd+shift+e。全部失败才
        回退到 pynput 键盘监听。dispatch 统一按全局 binding 下标对位。
        """
        self.stop()
        combo_bindings: list[tuple[int, object]] = []
        single_bindings: list[tuple[int, object]] = []
        for i, b in enumerate(self._bindings):
            if is_single_modifier(b.combo) or is_single_plain_key(b.combo):
                single_bindings.append((i, b))
            else:
                combo_bindings.append((i, b))

        carbon_ok = False
        single_ok = False
        backend_parts = []

        if sys.platform == "darwin":
            # 组合键 → Carbon（免授权、系统直接分发）
            if combo_bindings:
                carbon = CarbonHotkeys()
                cb = [b for _i, b in combo_bindings]
                cidx = [i for i, _b in combo_bindings]
                if carbon.install(cb, self._carbon_dispatch, indices=cidx):
                    self._carbon = carbon
                    carbon_ok = True
                    backend_parts.append("carbon")
                    self._carbon_error = ""
                else:
                    self._carbon_error = carbon.error
            # 单键 → CGEventTap（flagsChanged / keyDown）
            if single_bindings:
                single = SingleKeyHotkeys()
                # 单键后端只认 combo 字符串（不做 Binding 对象解析）
                single_pairs = [(i, b.combo) for i, b in single_bindings]
                if single.install(single_pairs, self._carbon_dispatch):
                    self._single = single
                    single_ok = True
                    backend_parts.append("single-key")
                else:
                    self._error = single.error

            if carbon_ok or single_ok:
                self._backend = "+".join(backend_parts)
                self._error = "" if not (single_bindings and not single_ok) else self._error
                return True

        # 回退：pynput 键盘监听（需要输入监控授权）
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

    def suspend(self) -> None:
        """临时停用全局热键（打开设置窗口录快捷键时用）。

        Carbon 系统热键是全局的：设置窗口开着时按 cmd+shift+... 仍会触发
        听写并抢走焦点，导致录入框收不到按键、看起来「按键录入没生效」。
        停用后按键只发给当前焦点控件（录入框），关闭窗口再 resume 恢复。
        """
        self.stop()

    def resume(self) -> None:
        """恢复被 suspend 停用的全局热键。"""
        if self._bindings and not self._backend:
            self.start()

    def stop(self) -> None:
        if self._carbon is not None:
            try:
                self._carbon.uninstall()
            except Exception:
                pass
            self._carbon = None
        if self._single is not None:
            try:
                self._single.uninstall()
            except Exception:
                pass
            self._single = None
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
        if self._backend and "pynput" not in self._backend:
            # 组合键走 Carbon（免授权），单键走 CGEventTap（需输入监控）
            has_single = self._single is not None
            detail = "Carbon 系统热键：系统直接分发，无需输入监控权限"
            tap_ok = True
            if has_single:
                detail += "；单键热键走系统事件监听（需输入监控授权）"
                tap_ok, tap_detail = event_tap_available()
                if not tap_ok:
                    detail += f"：{tap_detail}"
            return {
                "listener_alive": True,
                "tap_ok": tap_ok,
                "tap_detail": detail,
                "error": self._error,
                "last_hit": self._last_hit,
                "bindings": self.describe(),
                "healthy": tap_ok and not self._error,
                "backend": self._backend,
                "carbon_error": self._carbon_error,
                "registered": (self._carbon.count if self._carbon else 0)
                              + (self._single.count if self._single else 0),
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
    return "option_l" if platform == "darwin" else "ctrl+shift+space"
