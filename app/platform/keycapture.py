"""快捷键录入用的系统层按键捕获（macOS）。

为什么不用 Qt 的 keyPressEvent：
    cmd+shift+space、cmd+space 等组合被 macOS 当作系统级快捷键（Spotlight /
    输入法切换），在事件分发到应用之前就被系统消费，Qt 控件永远收不到，
    表现就是「清空输入框后按组合键录不进去」。实测（本模块开发时的铁证）：
    同一个合成按键，CGEventTap（HID 层）能看到 keycode=49 + cmd/shift 标志，
    而 QLineEdit.keyPressEvent 一次都没触发。

方案：
    仅在录入框进入「录入中」状态时安装一个 listen-only 的 CGEventTap，
    捕获 keyDown 的虚拟键码 + 修饰键标志，翻译成配置串（如 cmd+shift+space），
    通过回调交给调用方。捕获到第一个「修饰键 + 普通键」组合后立即卸载，
    绝不在后台长期监听键盘。非 macOS / 无 Quartz 时静默不可用，调用方回退
    到普通键盘事件录入。

所有操作都是本机事件监听，不联网、不写盘、不读取按键以外的信息。
"""
from __future__ import annotations

import sys
import threading

# 虚拟键码 → 键名，复用热键模块的权威映射（只取每个键码的第一个名字）
from .hotkey import KEYCODES, MODIFIER_KEYCODES

_CODE_TO_NAME: dict[int, str] = {}
for _name, _code in KEYCODES.items():
    _CODE_TO_NAME.setdefault(_code, _name)

# 修饰键虚拟键码 → 配置名：按左 Option 录入 "alt_l"、按右 Option 录入 "alt_r"，
# 这样用户可以精确设定只响应左 Option 或只响应右 Option（同 Typeless）。
# 注意：必须用左右具体名覆盖通用名——如果 58 同时映射到 "alt" 和 "alt_l"，
# 录入时就显示不了左右。这里直接硬编码，不依赖 MODIFIER_KEYCODES 的遍历顺序。
_MODCODE_TO_NAME: dict[int, str] = {
    55: "cmd_l", 54: "cmd_r",
    59: "ctrl_l", 62: "ctrl_r",
    58: "alt_l", 61: "alt_r",
    56: "shift_l", 60: "shift_r",
    63: "fn",
}

# 配置名 → CGEventFlags 位，用于判断修饰键是按下还是松开
_FLAG_BY_MOD = {
    "cmd": 1 << 20, "ctrl": 1 << 18, "alt": 1 << 19,
    "shift": 1 << 17, "fn": 1 << 23,
}

# CGEventFlags 修饰键位
_FLAG_CMD = 1 << 20
_FLAG_CTRL = 1 << 18
_FLAG_ALT = 1 << 19
_FLAG_SHIFT = 1 << 17
_MODS = [
    (_FLAG_CMD, "cmd"),
    (_FLAG_CTRL, "ctrl"),
    (_FLAG_ALT, "alt"),
    (_FLAG_SHIFT, "shift"),
]

# 退格 / 删除 / Esc：清空
_CLEAR_CODES = {51, 117}  # 51=delete/backspace, 117=forward delete
_ESC_CODE = 53


def combo_from_event(keycode: int, flags: int) -> str | None:
    """把 (虚拟键码, CGEventFlags) 翻译成配置串（如 'cmd+shift+space' 或单键 'd'）。

    修饰键顺序固定为 cmd/ctrl/alt/shift，与 qt_combo / carbon_combo 一致。
    参考 Typeless「press the key or key combination」：单个普通键也合法，
    所以无修饰键时返回裸键名（如 'd' / 'f1'），而不是拒绝。无法映射返回 None。
    """
    plain = _CODE_TO_NAME.get(keycode)
    if plain is None:
        return None
    mods = [name for flag, name in _MODS if flags & flag]
    return "+".join(mods + [plain]) if mods else plain


# kCFRunLoopCommonCategories（CoreFoundation 常量 = 0），PyObjC Quartz 未暴露
_CF_RUNLOOP_COMMON = 0


class KeyCapture:
    """一次性的系统层组合键捕获器。

    start(callback) 安装 tap；任意 keyDown 到达时：
      - Esc / 退格 → callback("") 表示清空；
      - 修饰键+普通键 → callback(combo) 并自动 stop()（一次录入完成）；
      - 纯修饰键 / 无意义键 → 忽略，继续等待。
    stop() 幂等，随时可调用；回调可能来自 tap 的 runloop 线程。
    """

    def __init__(self):
        self._callback = None
        self._tap = None
        self._runloop_source = None
        self._lock = threading.Lock()
        self._fired = False  # 已录入到一个完整组合，防止按键 auto-repeat 重复触发

    @staticmethod
    def available() -> bool:
        return sys.platform == "darwin"

    def start(self, callback) -> bool:
        if sys.platform != "darwin":
            return False
        try:
            import Quartz
        except ImportError:
            return False
        with self._lock:
            if self._tap is not None:
                return True
            self._callback = callback
            self._fired = False
            self._mod_candidates: list[str] = []  # 已按下、尚未确认的修饰键
            self._saw_plain = False               # 本次按键序列是否出现过普通键

            def on_event(proxy, etype, event, refcon):
                if self._fired:
                    return event
                keycode = Quartz.CGEventGetIntegerValueField(
                    event, Quartz.kCGKeyboardEventKeycode)
                flags = int(Quartz.CGEventGetFlags(event))
                cb = self._callback

                if etype == Quartz.kCGEventFlagsChanged:
                    # 单个修饰键（option/fn/cmd/…）：keycode 直接标识被改变的修饰键
                    mname = _MODCODE_TO_NAME.get(keycode)
                    if mname is None or cb is None:
                        return event
                    flag = _FLAG_BY_MOD.get(mname, 0)
                    down = bool(flags & flag)
                    if down:
                        # 修饰键按下：记录为候选，等后续判断是否有普通键跟随
                        if mname not in self._mod_candidates:
                            self._mod_candidates.append(mname)
                    else:
                        # 修饰键松开：若期间没有普通键 keyDown，说明用户只按了
                        # 这一个（或这几个）修饰键 → 录入单键/多修饰键串
                        if self._mod_candidates and not self._saw_plain:
                            combo = "+".join(self._mod_candidates)
                            self._fired = True
                            cb(combo)
                            self.stop()
                        self._mod_candidates.clear()
                    return event

                if etype != Quartz.kCGEventKeyDown:
                    return event

                if keycode == _ESC_CODE or keycode in _CLEAR_CODES:
                    # 退格/删除/Esc：清空后继续等待录入，不停止捕获
                    self._saw_plain = True
                    if cb:
                        cb("")
                    return event

                # 普通键 keyDown：用当前所有修饰键 + 这个普通键组成串
                self._saw_plain = True
                combo = combo_from_event(keycode, flags)
                if combo and cb:
                    self._fired = True
                    cb(combo)
                    self.stop()  # 录入到一个完整组合/单键，立即停止监听
                return event

            tap = Quartz.CGEventTapCreate(
                Quartz.kCGSessionEventTap,
                Quartz.kCGHeadInsertEventTap,
                Quartz.kCGEventTapOptionListenOnly,
                Quartz.kCGEventMaskForAllEvents,
                on_event,
                None,
            )
            if not tap:
                self._callback = None
                return False
            source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
            runloop = Quartz.CFRunLoopGetCurrent()
            Quartz.CFRunLoopAddSource(runloop, source, _CF_RUNLOOP_COMMON)
            Quartz.CGEventTapEnable(tap, True)
            self._tap = tap
            self._runloop_source = source
            self._runloop = runloop
            return True

    def stop(self) -> None:
        with self._lock:
            tap, source, runloop = self._tap, self._runloop_source, getattr(self, "_runloop", None)
            self._tap = None
            self._runloop_source = None
            self._callback = None
        if tap is None:
            return
        try:
            import Quartz
            Quartz.CGEventTapEnable(tap, False)
            if source is not None and runloop is not None:
                Quartz.CFRunLoopRemoveSource(runloop, source, _CF_RUNLOOP_COMMON)
        except Exception:
            pass

    def is_active(self) -> bool:
        return self._tap is not None
