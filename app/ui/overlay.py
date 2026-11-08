"""听写浮层：**文字**与**声音曲线**是两个各自独立的透明窗口，不共用一个色块。

按用户要求重做：
  · 文字窗口：只有一行文字（描边保证在任意应用上都看得清），没有背景块
  · 曲线窗口：只有 1~4 条曲线，没有背景块；声音小一条近直线，声音越大条数越多、振幅越大
  · 两个窗口水平居中、固定在屏幕最上方；切换应用、切换桌面 Space、进入全屏应用都保持可见
  · 完全鼠标穿透、不接受焦点、不激活本应用：点回输入框不会结束听写、光标不会消失
  · 曲线取自实时频谱并按 25 fps 平滑刷新，音量映射更灵敏，安静时也保持轻微流动
  · 配色来自内置主题（app/ui/themes.py），可随时切换

所有绘制都在本机完成，不涉及任何网络或远程渲染。
"""
from __future__ import annotations

import math
import sys
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QCursor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QApplication, QWidget

from .themes import DEFAULT_THEME, Theme, get_theme

WIDTH = 820
TEXT_HEIGHT = 52
WAVE_HEIGHT = 74
BAND_COUNT = 28

# NSWindowCollectionBehavior：所有 Space + 不参与窗口循环 + 固定 + 可作为全屏辅助窗口
_NS_CAN_JOIN_ALL_SPACES = 1 << 0
_NS_STATIONARY = 1 << 4
_NS_IGNORES_CYCLE = 1 << 6
_NS_FULL_SCREEN_AUXILIARY = 1 << 8
_COLLECTION_BEHAVIOR = (_NS_CAN_JOIN_ALL_SPACES | _NS_STATIONARY |
                        _NS_IGNORES_CYCLE | _NS_FULL_SCREEN_AUXILIARY)
_NS_SCREEN_SAVER_LEVEL = 1000  # 高于普通窗口、浮动窗口、状态栏，保证始终在最上层


def _native_log(message: str) -> None:
    """把浮层原生窗口的设置结果写入本机日志，失败不再静默（便于排查「不跨 Space」）。"""
    try:
        from ..config import logs_dir
        import time as _t
        with (logs_dir() / "overlay.log").open("a", encoding="utf-8") as fh:
            fh.write(f"{_t.strftime('%H:%M:%S')} {message}\n")
    except Exception:
        pass


class _FloatingWindow(QWidget):
    """透明、鼠标穿透、不抢焦点、跨 Space 常驻置顶的独立窗口基类。"""

    def __init__(self, title: str, width: int, height: int):
        super().__init__()
        self.setWindowTitle(title)
        # 注意：不能用 Qt.Tool —— macOS 上 Tool 窗口等价于 NSPanel，
        # 应用一失活就会被系统隐藏，正是「点别的地方浮层就没了」的根因。
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint |
                            Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setFixedSize(width, height)
        self._native_logged = ""  # 去重：同样的诊断消息只写一次

    def show_overlay(self) -> None:
        if not self.isVisible():
            self.show()
        # NSWindow 在 show() 之后才真正创建，需要延迟设置原生属性；
        # 再重复几次，防止 Qt 重建窗口或系统改变窗口层级后设置丢失。
        self.apply_native()
        QTimer.singleShot(0, self.apply_native)
        QTimer.singleShot(160, self.apply_native)

    def apply_native(self) -> None:
        """让窗口跨 Space / 全屏应用常驻，并彻底不参与焦点与鼠标。

        定位 NSWindow 用「winId → NSView → NSWindow」直取：Qt 的 winId() 在
        macOS 上就是 NSView 指针，这条路不依赖窗口标题，也不依赖
        pyobjc 包装对象的 id 比较（后者在打包环境中不可靠，曾导致
        「浮层只在桌面所在的 Space 显示，切到其他应用/Space 就消失」）。
        """
        if sys.platform != "darwin" or not self.isVisible():
            return
        try:
            from PySide6.QtGui import QGuiApplication
            if QGuiApplication.platformName() != "cocoa":
                return  # offscreen 等非原生平台没有真实 NSWindow；winId 是虚拟地址，
                        # 直接包装成 NSView 会解引用假指针（offscreen 测试段错误的根因）
        except Exception:
            pass
        window = None
        how = ""
        try:
            import objc  # type: ignore
            view = objc.objc_object(c_void_p=int(self.winId()))
            window = view.window()
            how = "winId直取"
        except Exception as exc:
            _native_log(f"{self.windowTitle()}：winId 直取失败 {type(exc).__name__}: {exc}")
        if window is None:
            try:  # 兜底：按标题在应用窗口里找
                from AppKit import NSApp  # type: ignore
                title = self.windowTitle()
                for w in (NSApp.windows() if NSApp is not None else []):
                    try:
                        if str(w.title() or "") == title:
                            window = w
                            how = "标题匹配"
                            break
                    except Exception:
                        continue
            except Exception as exc:
                _native_log(f"{self.windowTitle()}：标题匹配失败 {type(exc).__name__}: {exc}")
        if window is None:
            if self._native_logged != "notfound":
                self._native_logged = "notfound"
                _native_log(f"{self.windowTitle()}：未定位到 NSWindow，"
                            "跨 Space 属性未设置（这是浮层不跟随窗口切换的直接原因）")
            return
        try:
            from AppKit import NSColor  # type: ignore
            window.setLevel_(_NS_SCREEN_SAVER_LEVEL)
            window.setCollectionBehavior_(_COLLECTION_BEHAVIOR)
            # 强制置前：设置 level 后必须 orderFront，否则可能留在已有窗口之下。
            # orderFrontRegardless 不抢焦点（本窗口 DoesNotAcceptFocus + 鼠标穿透）。
            try:
                window.orderFrontRegardless()
            except Exception:
                pass
            window.setHidesOnDeactivate_(False)   # 关键：切换应用时不隐藏
            window.setCanHide_(False)             # 关键：⌘H / 隐藏其他应用时不被收起
            window.setIgnoresMouseEvents_(True)   # 点击穿透到下面的输入框
            window.setOpaque_(False)
            window.setHasShadow_(False)
            window.setBackgroundColor_(NSColor.clearColor())
            try:
                window.setExcludedFromWindowsMenu_(True)
            except Exception:
                pass
            key = f"ok:{how}:{window.level()}:{window.collectionBehavior()}"
            if self._native_logged != key:
                self._native_logged = key
                _native_log(f"{self.windowTitle()}：原生属性已设置（{how}）"
                            f" level={window.level()} behavior={window.collectionBehavior()}")
        except Exception as exc:
            if "seterr" not in self._native_logged:
                self._native_logged = "seterr"
                _native_log(f"{self.windowTitle()}：原生属性设置失败 {type(exc).__name__}: {exc}")


class _TextWindow(_FloatingWindow):
    """第一行：只有文字，用描边代替背景块，避免组成一个大面板。"""

    def __init__(self, theme: Theme):
        super().__init__("sayo Live Text", WIDTH, TEXT_HEIGHT)
        self.theme = theme
        self._text = ""
        self._status = ""
        self._is_live = False

    def set_theme(self, theme: Theme) -> None:
        self.theme = theme
        self.update()

    def set_content(self, text: str, status: str, is_live: bool) -> None:
        self._text = (text or "").strip()
        self._status = (status or "").strip()
        self._is_live = is_live
        self.update()

    def paintEvent(self, event):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        text = self._text or self._status
        if not text:
            p.end()
            return
        if len(text) > 48:
            text = "…" + text[-47:]

        font = QFont()
        font.setPointSize(19)
        font.setWeight(QFont.Medium)
        p.setFont(font)
        fm = p.fontMetrics()
        tw = min(self.width() - 28, fm.horizontalAdvance(text))
        x = max(14, (self.width() - tw) // 2)
        baseline = int(self.height() * 0.68)

        path = QPainterPath()
        path.addText(x, baseline, font, text)
        t = self.theme
        # 描边 + 填充：颜色成对来自主题，在浅色/深色应用背景上都看得清，且没有底板
        p.setPen(QPen(t.text_stroke, 5.5, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)
        p.setPen(Qt.NoPen)
        p.setBrush(t.text_live if self._is_live else t.text_final)
        p.drawPath(path)
        p.end()


class _WaveWindow(_FloatingWindow):
    """第二行：只有曲线，无背景块。条数与振幅都由实时音量驱动。"""

    def __init__(self, theme: Theme):
        super().__init__("sayo Live Wave", WIDTH, WAVE_HEIGHT)
        self.theme = theme
        self._level = 0.0
        self._bands = [0.0] * BAND_COUNT
        self._phase = 0
        self._visible = False  # 常驻窗口：idle 时清空内容、不绘制，视觉上"看不见"
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(40)  # 25 fps，曲线连续流动

    def set_theme(self, theme: Theme) -> None:
        self.theme = theme
        if self._visible:
            self.update()

    def set_visible(self, visible: bool) -> None:
        self._visible = visible
        self.update()

    def set_level(self, level: float) -> None:
        try:
            self._level = min(1.0, max(self._level * 0.92, float(level)))
        except (TypeError, ValueError):
            pass
        if self._visible:
            self.update()

    def set_audio(self, payload) -> None:
        """payload = (频段能量, 电平, rms)。rms 直接驱动振幅，轻声也能看出变化。"""
        try:
            bands, level, rms = payload
        except Exception:
            return
        try:
            # rms 系数放大到 40：轻声说话（rms≈0.01~0.02）也能明显看到曲线起伏；
            # 底噪（rms≈0.003）仍然只得到 ~0.12，保持一条近乎平直的线。
            boosted = min(1.0, max(float(level), float(rms) * 40.0))
        except (TypeError, ValueError):
            boosted = 0.0
        self._level = min(1.0, max(self._level * 0.90, boosted))
        if bands:
            for i in range(min(BAND_COUNT, len(bands))):
                try:
                    v = min(1.0, max(0.0, float(bands[i])))
                except (TypeError, ValueError):
                    continue
                self._bands[i] = max(v, self._bands[i] * 0.62)
        if self._visible:
            self.update()

    def reset(self) -> None:
        self._level = 0.0
        self._bands = [0.0] * BAND_COUNT
        if self._visible:
            self.update()

    def _tick(self) -> None:
        if not self._visible:
            return
        self._phase += 1
        self._bands = [v * 0.90 for v in self._bands]
        self._level *= 0.93
        self.update()

    def _band_at(self, t: float) -> float:
        pos = t * (BAND_COUNT - 1)
        i = int(pos)
        frac = pos - i
        a = self._bands[i]
        b = self._bands[min(BAND_COUNT - 1, i + 1)]
        return a + (b - a) * frac

    def paintEvent(self, event):  # noqa: N802
        if not self._visible:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        level = self._level
        count = 1 + min(3, int(level * 4.0))
        cy = self.height() / 2.0
        base_amp = 6.0 + 34.0 * level
        points = 128
        x0, span = 16.0, self.width() - 32.0
        waves, glow = self.theme.waves, self.theme.glow

        for i in range(count):
            amp = base_amp * (1.0 - i * 0.15)
            path = QPainterPath()
            for j in range(points):
                t = j / (points - 1)
                v = self._band_at(t)
                phase = self._phase * 0.085 + i * 0.88
                wave = (math.sin(2 * math.pi * (t * 2.1 + phase)) * 0.60 +
                        math.sin(2 * math.pi * (t * 4.3 - phase * 0.62)) * 0.28 +
                        math.sin(2 * math.pi * (t * 7.9 + phase * 1.3)) * 0.12)
                y = (cy + wave * (0.30 + 1.0 * v) * amp +
                     (i - (count - 1) / 2.0) * 2.4)
                x = x0 + t * span
                if j == 0:
                    path.moveTo(x, y)
                else:
                    path.lineTo(x, y)
            # 先画一层柔光，再画细亮线，保证在浅色桌面也看得清
            p.setPen(QPen(glow[min(i, 3)], 8.0, Qt.SolidLine,
                          Qt.RoundCap, Qt.RoundJoin))
            p.drawPath(path)
            p.setPen(QPen(waves[min(i, 3)], 2.0, Qt.SolidLine,
                          Qt.RoundCap, Qt.RoundJoin))
            p.drawPath(path)
        p.end()


class RecordingOverlay:
    """组合两个独立窗口，保持 main.py 使用的 set_state / set_* 接口不变。"""

    def __init__(self, theme_key: str = DEFAULT_THEME):
        self.theme = get_theme(theme_key)
        self.text_window = _TextWindow(self.theme)
        self.wave_window = _WaveWindow(self.theme)
        self._state = "idle"
        self._status = ""
        self._live_text = ""
        self._done_at = 0.0
        self._maintain_timer = QTimer(self.text_window)
        self._maintain_timer.timeout.connect(self._maintain)
        self._place()
        # 关键：两个窗口从启动就常驻（show 一次后永不 hide）。
        # macOS 上窗口一旦 hide()/orderOut 再 show，window server 会丢弃
        # 「跨所有 Space」注册，下次显示被挂回上次所在的 Space（桌面），
        # 于是浮层永远只在桌面出现、切到别的应用就消失。常驻 + 清空内容
        # 就能在视觉上隐藏，同时让跨 Space 注册永不丢失。
        self.text_window.show_overlay()
        self.wave_window.show_overlay()
        self.text_window.set_content("", "", False)
        self.wave_window.set_visible(False)

    def apply_theme(self, theme_key: str) -> None:
        """切换主题：立即重绘两个浮层窗口。"""
        self.theme = get_theme(theme_key)
        self.text_window.set_theme(self.theme)
        self.wave_window.set_theme(self.theme)

    # ------------------------------------------------------------ 位置
    def _target_screen(self):
        """跟随鼠标所在的显示器；取不到时退回主屏。"""
        try:
            screen = QApplication.screenAt(QCursor.pos())
        except Exception:
            screen = None
        return screen or QApplication.primaryScreen()

    def _place(self) -> None:
        screen = self._target_screen()
        if not screen:
            return
        g = screen.availableGeometry()  # 已排除菜单栏与 Dock
        x = g.center().x() - WIDTH // 2
        y = g.top() + 12
        self.text_window.move(x, y)
        self.wave_window.move(x, y + TEXT_HEIGHT + 4)

    # ------------------------------------------------------------ 状态
    def set_state(self, state: str, text: str = "") -> None:
        self._state = state
        self._status = text or {
            "recording": "正在聆听…",
            "transcribing": "正在识别…",
            "done": "已写入",
            "error": "请查看设置",
        }.get(state, "")
        if state == "recording":
            self._live_text = ""
            self._done_at = 0.0
            self.wave_window.reset()

        active = state in ("recording", "transcribing", "done", "error")
        if active:
            self._place()
            self.wave_window.set_visible(True)
            self.text_window.show_overlay()
            self.wave_window.show_overlay()
            if not self._maintain_timer.isActive():
                self._maintain_timer.start(1000)
        else:
            # 常驻隐藏：不 hide()，只清空内容（窗口透明+穿透，视觉上不可见）
            self._maintain_timer.stop()
            self._live_text = ""
            self._status = ""
            self.wave_window.set_visible(False)
            self.text_window.set_content("", "", False)
        if state in ("done", "error"):
            self._done_at = time.monotonic()
        self._sync_text()

    def _sync_text(self) -> None:
        is_live = self._state == "recording" and bool(self._live_text)
        shown = self._live_text or self._status
        self.text_window.set_content(shown, self._status, is_live)

    def _maintain(self) -> None:
        """周期性重申原生层级与位置：应用切换、Space 切换都不会让浮层消失。"""
        if self._state == "idle":
            return
        self._place()
        self.text_window.apply_native()
        self.wave_window.apply_native()
        if self._state in ("done", "error") and self._done_at and \
                time.monotonic() - self._done_at > 3.5:
            self.set_state("idle")

    # ------------------------------------------------------------ 数据入口
    def set_live_text(self, text: str) -> None:
        """实时文字：后续音频重解码整句时会修正前面已显示的字。"""
        self._live_text = (text or "").strip()
        self._sync_text()

    def set_final_text(self, text: str) -> None:
        """识别结束后把最终文字留在浮层，即使实时预览没出字也能看到结果。"""
        if text and text.strip():
            self._live_text = text.strip()
        self._sync_text()

    def set_level(self, level: float) -> None:
        self.wave_window.set_level(level)

    def set_audio(self, payload) -> None:
        self.wave_window.set_audio(payload)
