"""
独立单键热键测试器
验证双 tap + 专用线程方案能否捕获 Option。
GUI 版：.venv/bin/python /tmp/singletap_test.py
"""
import os
import sys
import threading
import time
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from PySide6 import QtCore, QtWidgets
import Quartz
from Quartz import (
    CGEventTapCreate, kCGSessionEventTap, kCGAnnotatedSessionEventTap,
    kCGHeadInsertEventTap, kCGEventTapOptionListenOnly,
    kCGEventKeyDown, kCGEventKeyUp, kCGEventFlagsChanged,
)
# CoreFoundation 的 runloop 函数（pyobjc-core 提供）
import objc
try:
    from CoreFoundation import (
        CFRunLoopRunInMode, CFRunLoopGetMain, CFRunLoopAddSource,
        CFRunLoopSourceCreate,
    )
except ImportError:
    import CoreFoundation as _CF
    CFRunLoopRunInMode = _CF.CFRunLoopRunInMode
    CFRunLoopGetMain = _CF.CFRunLoopGetMain
    CFRunLoopAddSource = _CF.CFRunLoopAddSource
    CFRunLoopSourceCreate = _CF.CFRunLoopSourceCreate

CF_KEYCODES = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8,
    "v": 9, "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17,
    "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "9": 25, "7": 26,
    "8": 28, "0": 29, "i": 30, "j": 31, "k": 32, "l": 33, "o": 34, "u": 35,
    "m": 36, "n": 37, "comma": 43, "period": 47, "slash": 44,
    "return": 36, "tab": 48, "space": 49, "delete": 51, "escape": 53,
    "command": 55, "shift": 56, "capslock": 57, "option_l": 58, "control": 59,
    "option_r": 61, "fn": 63,
}
KEY_NAMES = {
    0: "a", 1: "s", 2: "d", 3: "f", 4: "h", 5: "g", 6: "z", 7: "x", 8: "c",
    9: "v", 11: "b", 12: "q", 13: "w", 14: "e", 15: "r", 16: "y", 17: "t",
    18: "1", 19: "2", 20: "3", 21: "4", 22: "6", 23: "5", 24: "=", 25: "9",
    26: "7", 27: "-", 28: "8", 29: "0", 30: "i", 31: "j", 32: "k", 33: "l",
    34: "o", 35: "u", 36: "enter/m", 37: "n", 38: "enter", 39: "delete",
    42: "\\", 43: ",", 44: "/", 45: ".", 47: ";", 48: "tab", 49: "space",
    51: "quote", 53: "]", 55: "command", 56: "shift", 57: "capslock",
    58: "option(左)", 59: "control", 61: "option(右)", 63: "fn",
}

CAPTURED = []


def log(msg):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)


def _handler(proxy, event_type, event, refcon, capture):
    try:
        if event_type in (kCGEventKeyDown, kCGEventKeyUp, kCGEventFlagsChanged):
            keycode = int(event.getIntegerValueField(0x10000000))
            flags = int(event.getIntegerValueField(0xFF000000))
            entry = {"type": event_type, "keycode": keycode, "flags": flags,
                     "ts": datetime.now().strftime("%H:%M:%S")}
            capture.append(entry)
            if len(capture) > 2000:
                del capture[:1000]
            if event_type == kCGEventFlagsChanged:
                names = []
                if flags & (1 << 19): names.append("ALT")
                if flags & (1 << 24): names.append("CTRL")
                if flags & (1 << 25): names.append("SHIFT")
                if flags & (1 << 26): names.append("CMD")
                log(f"【flagsChanged】keycode={keycode} ({KEY_NAMES.get(keycode, '?')}) flags={flags:#x} -> {names or '全部释放'}")
            else:
                log(f"【{kCGEventKeyDown if event_type == kCGEventKeyDown else 'keyUp'}】keycode={keycode} ({KEY_NAMES.get(keycode, '?')})")
    except Exception as exc:
        log(f"handler 异常: {exc}")
    return 1


class Listener:
    def __init__(self):
        self._thread = None
        self._active = False
        self._created = False
        self._fail_reason = ""
        self._session = None
        self._annotated = None
        self._source = None

    def start(self):
        self._active = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="singletap-listener")
        self._thread.start()
        return True

    def _make_tap(self, tap_point):
        def handler(proxy, event_type, event, refcon):
            return _handler(proxy, event_type, event, refcon, CAPTURED)
        mask = (1 << kCGEventKeyDown) | (1 << kCGEventKeyUp) | (1 << kCGEventFlagsChanged)
        try:
            return CGEventTapCreate(
                tap_point, kCGHeadInsertEventTap,
                kCGEventTapOptionListenOnly, mask,
                handler, None,
            )
        except Exception:
            # 部分 PyObjC 版本需要显式类型包裹
            from Quartz import CFEventTapCallBackType
            return CGEventTapCreate(
                tap_point, kCGHeadInsertEventTap,
                kCGEventTapOptionListenOnly, mask,
                CFEventTapCallBackType(handler), None,
            )

    def _run(self):
        log(f"专用线程启动 pid={os.getpid()}")
        try:
            self._session = self._make_tap(kCGSessionEventTap)
            self._annotated = self._make_tap(kCGAnnotatedSessionEventTap)
            if not self._session:
                log("✗ session tap 创建失败")
                self._fail_reason = "session tap 创建失败"
                return
            self._created = True
            log("✓ 双 tap 创建成功，监听中...")
        except Exception as exc:
            log(f"✗ 创建 tap 异常: {exc}")
            self._fail_reason = f"创建异常: {exc}"
            return
        if self._session:
            # tap 本身可作为 RunLoop source；直接加入 main runloop 的 common modes
            CFRunLoopAddSource(CFRunLoopGetMain(), self._session, 0)  # 0 = kCFRunLoopCommonModes
            log("✓ tap 源已加入 main runloop (CommonModes)")
        while self._active:
            CFRunLoopRunInMode(0, 0.1, False)
            time.sleep(0.01)
        log("线程退出")

    def status(self):
        return {
            "created": self._created,
            "fail_reason": self._fail_reason,
            "thread_alive": bool(self._thread and self._thread.is_alive()),
            "n_captured": len(CAPTURED),
        }


class TestWindow(QtWidgets.QWidget):
    def __init__(self, listener):
        super().__init__()
        self.listener = listener
        self.setWindowTitle("轻述 sayo · 单键热键测试器")
        self.setMinimumWidth(660)
        self._build()
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(200)

    def _build(self):
        lay = QtWidgets.QVBoxLayout(self)
        self.status_label = QtWidgets.QLabel("正在启动 tap...")
        self.status_label.setStyleSheet("font-size: 14px; padding: 8px;")
        lay.addWidget(self.status_label)
        self.count_label = QtWidgets.QLabel("已捕获事件：0")
        self.count_label.setStyleSheet("font-size: 12px; color: #888;")
        lay.addWidget(self.count_label)
        self.last_label = QtWidgets.QLabel("最近事件：—\n\n下方会实时滚动显示捕获到的按键。")
        self.last_label.setWordWrap(True)
        self.last_label.setStyleSheet(
            "font-family: monospace; font-size: 13px; padding: 6px; "
            "min-height: 240px; background: rgba(0,0,0,40); border-radius: 6px;"
        )
        lay.addWidget(self.last_label)
        hint = QtWidgets.QLabel(
            "现在【反复按 左右 Option / fn / 普通字母】，\n"
            "能看到 'flagsChanged keycode=58/61' → 方案成功！\n"
            "按 q 或点退出结束。"
        )
        hint.setStyleSheet("color: #aaa; font-size: 12px; padding: 4px;")
        lay.addWidget(hint)
        btn = QtWidgets.QPushButton("退出测试")
        btn.clicked.connect(self.close)
        lay.addWidget(btn)

    def _refresh(self):
        st = self.listener.status()
        if st["created"]:
            self.status_label.setText(f"✓ 双 tap 已创建 · 线程存活 · 已捕获 {st['n_captured']} 事件")
            self.status_label.setStyleSheet("font-size: 14px; color: #4CAF50; padding: 8px;")
        elif st["fail_reason"]:
            self.status_label.setText(f"✗ {st['fail_reason']}")
            self.status_label.setStyleSheet("font-size: 14px; color: #f44336; padding: 8px;")
        self.count_label.setText(f"已捕获事件：{st['n_captured']}")
        if CAPTURED:
            lines = [f"{e['ts']} type={e['type']} keycode={e['keycode']} ({KEY_NAMES.get(e['keycode'],'?')}) flags={e['flags']:#x}"
                     for e in CAPTURED[-12:]]
            self.last_label.setText("\n".join(lines))

    def closeEvent(self, e):
        self._timer.stop()
        self.listener._active = False
        e.accept()


def main():
    listener = Listener()
    listener.start()
    app = QtWidgets.QApplication(sys.argv)
    win = TestWindow(listener)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
