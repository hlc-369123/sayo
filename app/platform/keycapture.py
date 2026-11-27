"""快捷键录入用的系统层按键捕获（macOS / Windows）。

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

Windows 用 pynput 的低层键盘监听器实现同样的「按键期间临时捕获」：
    支持 Ctrl / Alt / Shift / Win 单独作为热键（例如只按 Alt），也支持任意
    组合键。Qt 的 QLineEdit 在 Windows 上收不到单独的 Alt/Ctrl/Shift 按键，
    因此必须走系统层监听；全局热键在设置窗口打开时已暂停，两者不会打架。

所有操作都是本机事件监听，不联网、不写盘、不读取按键以外的信息。
"""
from __future__ import annotations

import sys
import threading
import time
from datetime import datetime

def _kc_log(msg: str) -> None:
    """KeyCapture 诊断日志（仅用于定位，生产可移除）。"""
    try:
        import pathlib
        logdir = pathlib.Path.home() / "Library/Application Support/sayo/logs"
        logdir.mkdir(parents=True, exist_ok=True)
        with (logdir / "kc.log").open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now().strftime('%H:%M:%S')} {msg}\n")
    except Exception:
        pass

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
    # 左右独立名：共享对应的通用修饰键 flag（左右 Option 都看 ALT 位，
    # 左右 Shift 都看 SHIFT 位…）。缺这些键名会导致 down 永远判 False。
    "cmd_l": 1 << 20, "cmd_r": 1 << 20,
    "ctrl_l": 1 << 18, "ctrl_r": 1 << 18,
    "alt_l": 1 << 19, "alt_r": 1 << 19,
    "shift_l": 1 << 17, "shift_r": 1 << 17,
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
        self._mod_candidates: list = []
        self._saw_plain = False

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
        # 关键修复：每次 start 都【彻底重建】，不复用旧 tap。
        # 旧实现 `if self._tap is not None: return True` 会复用旧 tap，但其
        # on_event 闭包里的状态机（_fired/_mod_candidates）可能残留 _fired=True
        # （上次录入完 stop 时没清），导致 `if self._fired: return` 吞掉后续
        # 所有 Option 事件 → 表现为「按一次成功、删掉重按失败、切焦点再回来才行」。
        # 每次重建 = 全新干净状态，根治。
        with self._lock:
            if self._tap is not None:
                # 已有活跃 tap：复用（录入后不 stop，tap 持续到失焦）。
                # 只更新回调与状态，避免每次 start 都重建导致 TCC/tap 抖动。
                self._callback = callback
                self._fired = False
                self._mod_candidates = []
                self._saw_plain = False
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
                _kc_log(f"事件 etype={etype} keycode={keycode} flags={flags:#x}")

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
                            # 不 stop：保持监听，用户可退格清空后重按重录；
                            # tap 在输入框失焦（focusOut）时才 stop。
                            self._mod_candidates = []
                        self._mod_candidates.clear()
                    return event

                if etype != Quartz.kCGEventKeyDown:
                    return event

                if keycode == _ESC_CODE or keycode in _CLEAR_CODES:
                    # 退格/删除/Esc：清空并复位状态机，继续监听等待重录。
                    # _saw_plain 也必须复位：松开修饰键判定单键的条件是
                    # `not self._saw_plain`，若清空时误置 True，则同一录入框内
                    # 删掉后再按单个 Option 永远录不上（表现为必须切焦点重开）。
                    self._fired = False
                    self._saw_plain = False
                    self._mod_candidates = []
                    if cb:
                        cb("")
                    return event

                # 普通键 keyDown：用当前所有修饰键 + 这个普通键组成串
                self._saw_plain = True
                combo = combo_from_event(keycode, flags)
                if combo and cb:
                    self._fired = True
                    cb(combo)
                    # 不 stop：保持监听，退格清空后可重按重录（失焦时 stop）
                return event

            # 关键修复（2026-11-22 对照实验实证）：
            # 1) listen-only tap 收不到 Option(58/61) 等敏感修饰键的
            #    flagsChanged（macOS 安全分级），必须用 active 模式（opts=0，需辅助功能）。
            # 2) kCGEventMaskForAllEvents 在此 PyObjC/系统下实测收 0 事件，
            #    必须用精确掩码 keyDown|keyUp|flagsChanged。
            # active 模式 handler 必须返回原 event 放行（on_event 已 return event）。
            mask = (Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown)
                    | Quartz.CGEventMaskBit(Quartz.kCGEventKeyUp)
                    | Quartz.CGEventMaskBit(Quartz.kCGEventFlagsChanged))
            tap = Quartz.CGEventTapCreate(
                Quartz.kCGSessionEventTap,
                Quartz.kCGHeadInsertEventTap,
                0,  # active 模式（非 ListenOnly）
                mask,
                on_event,
                None,
            )
            _kc_log(f"KeyCapture tap 创建: {'OK' if tap else 'FAIL'} active模式 掩码={mask:#x}")
            if not tap:
                self._callback = None
                return False
            source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
            # 关键修复（2026-11-22 实测）：tap 在专用线程泵自己的 runloop，
            # 否则主线程 Qt 忙时收不到事件（退格清空/单键录入全部失效）。
            self._stop_pump = threading.Event()
            self._pump = None
            if source is not None:
                self._pump = threading.Thread(
                    target=self._pump_loop, args=(tap, source,),
                    daemon=True, name="keycapture-pump")
                self._pump.start()
            Quartz.CGEventTapEnable(tap, True)
            self._tap = tap
            self._runloop_source = source
            self._runloop = None
            return True

    def _pump_loop(self, tap, source) -> None:
        """专用线程泵 runloop，让 KeyCapture 的 tap 持续接收事件。

        与 SingleKeyHotkeys 相同模式（ChatGPT 诊断器实证可行）。
        额外：定期自检 tap 是否仍 enabled（TCC 辅助功能未就绪时 tap 会被系统
        静默禁用 → 表现为「第一次录不上、重开才行」）。被禁用就重新 enable，
        让首次聚焦也能成功，无需重开界面。
        """
        try:
            import Quartz
            cur = Quartz.CFRunLoopGetCurrent()
            if cur is not None and source is not None:
                Quartz.CFRunLoopAddSource(cur, source, _CF_RUNLOOP_COMMON)
        except Exception:
            pass
        last_check = 0.0
        last_enabled = True
        while not self._stop_pump.is_set():
            try:
                import Quartz
                Quartz.CFRunLoopRunInMode(_CF_RUNLOOP_COMMON, 0.2, True)
                # 每 1.5s 自检一次 tap 启用状态；被禁用就重新 enable
                now = time.monotonic()
                if now - last_check > 1.5:
                    last_check = now
                    try:
                        enabled = bool(Quartz.CGEventTapIsEnabled(tap))
                        if not enabled and last_enabled:
                            _kc_log("tap 被禁用，重新 enable（TCC 未就绪常见）")
                            Quartz.CGEventTapEnable(tap, True)
                            _kc_log(f"重新 enable 结果: {Quartz.CGEventTapIsEnabled(tap)}")
                        last_enabled = bool(Quartz.CGEventTapIsEnabled(tap))
                    except Exception:
                        pass
            except Exception:
                pass
            time.sleep(0.01)
        try:
            import Quartz
            cur = Quartz.CFRunLoopGetCurrent()
            if cur is not None and source is not None:
                Quartz.CFRunLoopRemoveSource(cur, source, _CF_RUNLOOP_COMMON)
        except Exception:
            pass

    def stop(self) -> None:
        with self._lock:
            tap = self._tap
            self._tap = None
            self._runloop_source = None
            self._callback = None
            # 关键：彻底复位状态机，避免脏状态（尤其 _fired=True）残留到下次 start
            self._fired = False
            self._mod_candidates = []
            self._saw_plain = False
            pump = getattr(self, "_pump", None)
            stop_pump = getattr(self, "_stop_pump", None)
        if stop_pump is not None:
            stop_pump.set()
        if pump is not None and pump.is_alive():
            pump.join(timeout=1.0)
        self._pump = None
        if tap is None:
            return
        try:
            import Quartz
            Quartz.CGEventTapEnable(tap, False)
        except Exception:
            pass
        try:
            import Quartz
            Quartz.CFMachPortInvalidate(tap)
        except Exception:
            pass

    def is_active(self) -> bool:
        return self._tap is not None

    def reset_fired(self) -> None:
        """复位「已录入」标志。退格清空后调用，让重按能立即录入。

        否则 _fired=True 残留会吞掉后续所有事件（表现为「录一次删掉重按
        失效、必须切焦点重建 tap 才行」）。线程安全。
        """
        with self._lock:
            self._fired = False
            self._saw_plain = False
            self._mod_candidates = []


# --------------------------------------------------------------- Windows 捕获
_WIN_MODS = [
    ("cmd", "cmd"),      # Windows 键（pynput 的 Key.cmd）
    ("ctrl", "ctrl"),
    ("alt", "alt"),
    ("shift", "shift"),
]
_WIN_MOD_NAMES = {m for _token, m in _WIN_MODS}


def _win_raw_token(key) -> str:
    """物理键标识：左右 Alt 必须分开记，否则同时按住再松开其一会被当成「只按了一下」。

    pynput 的 Key.alt_l / Key.alt_r 都映射到配置名 "alt"，但物理上是两个键。
    """
    name = getattr(key, "name", None)
    if name:
        return str(name).lower()
    char = getattr(key, "char", None)
    if char is not None:
        return "char:" + str(char)
    return "vk:" + str(getattr(key, "vk", id(key)))


def _win_key_name(key) -> str | None:
    """pynput 按键 → 热键配置名；未知键返回 None。"""
    name = getattr(key, "name", None)
    if name:
        n = str(name).lower()
        if n in ("cmd", "cmd_l", "cmd_r", "windows", "win"):
            return "cmd"
        if n in ("ctrl", "ctrl_l", "ctrl_r", "control"):
            return "ctrl"
        if n in ("alt", "alt_l", "alt_r", "alt_gr"):
            return "alt"
        if n in ("shift", "shift_l", "shift_r"):
            return "shift"
        if n in ("backspace", "delete"):
            return n
        if n == "esc":
            return "esc"
        if n in ("space", "enter", "tab", "caps_lock", "num_lock",
                 "insert", "home", "end", "page_up", "page_down",
                 "up", "down", "left", "right"):
            return {
                "caps_lock": "capslock", "num_lock": "numlock",
                "page_up": "pageup", "page_down": "pagedown",
            }.get(n, n)
        if n.startswith("f") and n[1:].isdigit():
            return n
        return None
    char = getattr(key, "char", None)
    if char is not None:
        c = str(char)
        if c == " ":
            return "space"
        if c.isalnum():
            return c.lower()
        return c if c in "-=[]\\;',./`" else None
    return None


class WinKeyCapture:
    """Windows 快捷键录入器：按键期间临时监听，支持单独 Alt / Ctrl / Shift / Win。

    - 修饰键按下后不立即录入，等待普通键；若一直没按普通键，则在修饰键松开时
      录成单键热键（例如只按一次 Alt → "alt"）。
    - 普通键按下时录入当前所有修饰键 + 该键，例如 Ctrl+Shift+空格。
    - 退格 / Delete / Esc 清空，并把状态机复位以便立即重录。
    """

    def __init__(self):
        self._listener = None
        self._callback = None
        self._lock = threading.Lock()
        # 物理键 token → 配置名。用 dict 而不是 set：左右 Alt/Ctrl 各记一条，
        # 同时按住再松开其中一个时，不会误判成「只按过一个修饰键」。
        self._pressed: dict[str, str] = {}
        self._fired = False

    @staticmethod
    def available() -> bool:
        return sys.platform == "win32"

    # ---- 状态机（拆出来是为了能在没有真实键盘的环境里做回归测试）
    def _mods_in_order(self) -> list[str]:
        """当前按住的修饰键，按固定顺序（cmd / ctrl / alt / shift）去重返回。"""
        held = set(self._pressed.values())
        return [m for _token, m in _WIN_MODS if m in held]

    def _handle_press(self, name: str, raw: str) -> str:
        """返回要录入的组合串；空串表示继续等待。"""
        with self._lock:
            if self._fired:
                return ""
            self._pressed[raw] = name
            if name in _WIN_MOD_NAMES:
                return ""       # 等普通键；若一直不来，松开时按单键处理
            combo = "+".join(self._mods_in_order() + [name])
            self._fired = True
            return combo

    def _handle_release(self, name: str, raw: str) -> str:
        """修饰键松开且此时没有任何修饰键还按着 → 录成单键（Alt / Ctrl / Shift / Win）。"""
        with self._lock:
            was_held = self._pressed.pop(raw, None) is not None
            if self._fired or not was_held or name not in _WIN_MOD_NAMES:
                return ""
            if self._mods_in_order():      # 还有别的修饰键按着（例如左右 Alt 同时按）
                return ""
            self._fired = True
            return name

    def reset(self) -> None:
        """清空状态机（退格 / Esc 清空、重新开始录入时用）。"""
        with self._lock:
            self._pressed.clear()
            self._fired = False

    def start(self, callback) -> bool:
        self.stop()
        try:
            from pynput.keyboard import Listener  # type: ignore
        except Exception:
            return False
        with self._lock:
            self._callback = callback
            self._pressed.clear()
            self._fired = False

        def fire(combo: str) -> None:
            cb = self._callback
            if cb:
                try:
                    cb(combo)
                except Exception:
                    pass

        def on_press(key) -> None:
            name = _win_key_name(key)
            if name is None:
                return
            if name in ("backspace", "delete", "esc"):
                self.reset()
                fire("")
                return
            combo = self._handle_press(name, _win_raw_token(key))
            if combo:
                fire(combo)

        def on_release(key) -> None:
            name = _win_key_name(key)
            if name is None:
                return
            combo = self._handle_release(name, _win_raw_token(key))
            if combo:
                fire(combo)

        try:
            listener = Listener(on_press=on_press, on_release=on_release)
            listener.daemon = True
            listener.start()
            self._listener = listener
            return True
        except Exception:
            with self._lock:
                self._callback = None
            return False

    def stop(self) -> None:
        listener = self._listener
        self._listener = None
        with self._lock:
            self._callback = None
            self._pressed.clear()
            self._fired = False
        if listener is not None:
            try:
                listener.stop()
            except Exception:
                pass

    def is_active(self) -> bool:
        return self._listener is not None

    def reset_fired(self) -> None:
        with self._lock:
            self._fired = False
            self._pressed.clear()


def create_key_capture():
    """按当前平台返回对应的录入器；非 Windows 仍是原来的 macOS 实现。"""
    return WinKeyCapture() if sys.platform == "win32" else KeyCapture()
