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

from PySide6.QtCore import Qt, QTimer, Signal, QRect, QRectF, QPointF, QObject
from PySide6.QtGui import (QColor, QCursor, QFont, QFontMetrics, QLinearGradient,
                           QBrush, QPainter, QPainterPath, QPen)
from PySide6.QtWidgets import QApplication, QWidget

from .themes import DEFAULT_THEME, Theme, get_theme

WIDTH = 820
TEXT_HEIGHT = 52
WAVE_HEIGHT = 74
BAND_COUNT = 28

# ---- 胶囊浮层（pill 样式，单条）常量：小巧、清晰、配色跟随主题 ----
PILL_W = 360          # 默认宽度（可由用户拖拽右边缘调整）
PILL_W_MIN = 240      # 最短：仍放得下 × / 波形 / ✓
PILL_W_MAX = 1000     # 最长：可显示更多识别文字
PILL_H = 40
PILL_BTN_R = 9
PILL_BANDS = 12
PILL_RESIZE_ZONE = 16  # 右边缘拉伸热区宽度（鼠标移上去变左右箭头）
TIP_H = 30              # 窗口顶部预留的透明带高度，专门绘制悬停提示气泡
NOISE_FLOOR = 0.16     # 波形底噪门限：静音时归 0，波形静止


def _mix(a: QColor, b: QColor, t: float) -> QColor:
    """两色线性混合，用于从主题色派生按钮/描边色（不引入主题外的杂色）。"""
    return QColor(int(a.red() + (b.red() - a.red()) * t),
                  int(a.green() + (b.green() - a.green()) * t),
                  int(a.blue() + (b.blue() - a.blue()) * t),
                  int(a.alpha() + (b.alpha() - a.alpha()) * t))


def _with_alpha(c: QColor, alpha: int) -> QColor:
    out = QColor(c)
    out.setAlpha(alpha)
    return out

# NSWindowCollectionBehavior：所有 Space + 不参与窗口循环 + 固定 + 可作为全屏辅助窗口
_NS_CAN_JOIN_ALL_SPACES = 1 << 0
_NS_STATIONARY = 1 << 4
_NS_IGNORES_CYCLE = 1 << 6
_NS_FULL_SCREEN_AUXILIARY = 1 << 8
_COLLECTION_BEHAVIOR = (_NS_CAN_JOIN_ALL_SPACES | _NS_STATIONARY |
                        _NS_IGNORES_CYCLE | _NS_FULL_SCREEN_AUXILIARY)
_NS_SCREEN_SAVER_LEVEL = 1000  # 高于普通窗口、浮动窗口、状态栏，保证始终在最上层
# NSWindowStyleMaskNonactivatingPanel：点击/拖动本窗口时**不激活所属应用**。
# 这是「点 ✓ 或拖胶囊就切回桌面/设置窗口、输入框失焦」的根治开关：
# accessory 应用的普通 NSWindow 被点按时 AppKit 仍会 activate 本应用，
# 于是 macOS 把本应用所在的 Space / 已打开的设置窗口推到前台。
_NS_NONACTIVATING_PANEL = 1 << 7


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

    def __init__(self, title: str, width: int, height: int,
                 mouse_transparent: bool = True, nonactivating: bool = False):
        super().__init__()
        self.setWindowTitle(title)
        flags = (Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint |
                 Qt.WindowDoesNotAcceptFocus)
        # 只有「需要接收鼠标」的胶囊窗用 Qt.Tool。macOS 上 Qt.Tool 生成的是
        # QNSPanel；实测普通 QNSWindow 会直接忽略
        # NSWindowStyleMaskNonactivatingPanel 风格位（styleMask 14 → 14），
        # QNSPanel 才能设上（14 → 142）。经典两窗是鼠标穿透的，永远不会被点，
        # 不需要这层，也就不必承担 NSPanel 的额外行为。
        if nonactivating:
            flags |= Qt.Tool
        self.setWindowFlags(flags)
        self._nonactivating = nonactivating
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        # 鼠标穿透：经典两窗要「点穿」到下方输入框；胶囊窗要接收 ×/✓/拖动
        self._mouse_transparent = mouse_transparent
        self.setAttribute(Qt.WA_TransparentForMouseEvents, mouse_transparent)
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

    def set_mouse_transparent(self, transparent: bool) -> None:
        """运行时切换鼠标穿透：胶囊可见且可点击时关闭穿透，否则开启穿透。

        胶囊窗口平时「透明 + 穿透」避免挡住下方内容；只有录音中（pill 样式）
        才接收鼠标，让 ×/✓ 可点击、整条可拖动。
        """
        if self._mouse_transparent == transparent:
            return
        self._mouse_transparent = transparent
        self.setAttribute(Qt.WA_TransparentForMouseEvents, transparent)
        self.apply_native()

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
            # 关键：非激活面板风格位。没有它，点胶囊上的 ×/✓ 或按住胶囊拖动，
            # AppKit 都会先 activate 本应用 → macOS 切到本应用所在 Space，
            # 把设置窗口/桌面推到前台，用户原来的输入框随即失焦，
            # 识别结果就无处可粘贴了。
            if self._nonactivating:
                try:
                    window.setStyleMask_(int(window.styleMask()) |
                                         _NS_NONACTIVATING_PANEL)
                except Exception as exc:
                    _native_log(f"{self.windowTitle()}：非激活风格位设置失败 "
                                f"{type(exc).__name__}: {exc}")
                try:
                    # NSPanel 专用方法：只在必要时才成为 key 窗口
                    window.setBecomesKeyOnlyIfNeeded_(True)
                except Exception:
                    pass
                try:
                    # 面板默认会在应用失活时自动隐藏，必须显式关掉，
                    # 否则又回到「点别的地方浮层就没了」的老问题。
                    window.setHidesOnDeactivate_(False)
                    window.setCanHide_(False)
                except Exception:
                    pass
            # 强制置前：设置 level 后必须 orderFront，否则可能留在已有窗口之下。
            # orderFrontRegardless 不抢焦点（本窗口 DoesNotAcceptFocus + 鼠标穿透）。
            try:
                window.orderFrontRegardless()
            except Exception:
                pass
            window.setHidesOnDeactivate_(False)   # 关键：切换应用时不隐藏
            window.setCanHide_(False)             # 关键：⌘H / 隐藏其他应用时不被收起
            # 点击穿透：经典两窗穿透到下方输入框；胶囊窗要接收 ×/✓/拖动
            window.setIgnoresMouseEvents_(self._mouse_transparent)
            window.setOpaque_(False)
            window.setHasShadow_(False)
            window.setBackgroundColor_(NSColor.clearColor())
            try:
                window.setExcludedFromWindowsMenu_(True)
            except Exception:
                pass
            try:
                style = int(window.styleMask())
            except Exception:
                style = -1
            nonact = ("不适用" if not self._nonactivating else
                      ("是" if style & _NS_NONACTIVATING_PANEL else "否！点击会切界面"))
            key = (f"ok:{how}:{window.level()}:{window.collectionBehavior()}:{style}")
            if self._native_logged != key:
                self._native_logged = key
                _native_log(f"{self.windowTitle()}：原生属性已设置（{how}）"
                            f" level={window.level()} behavior={window.collectionBehavior()}"
                            f" styleMask={style} 非激活面板={nonact}")
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
        # 毛玻璃底板：浅白圆角胶囊（仅当主题定义了底板色）
        if self.theme.panel_bg:
            p.setPen(QPen(QColor(self.theme.panel_border), 1) if self.theme.panel_border else Qt.NoPen)
            p.setBrush(QColor(self.theme.panel_bg))
            r = self.height() * 0.5
            p.drawRoundedRect(0, 0, self.width(), self.height(), r, r)
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
        # 毛玻璃底板：浅白圆角胶囊
        if self.theme.panel_bg:
            p.setPen(QPen(QColor(self.theme.panel_border), 1) if self.theme.panel_border else Qt.NoPen)
            p.setBrush(QColor(self.theme.panel_bg))
            r = self.height() * 0.5
            p.drawRoundedRect(0, 0, self.width(), self.height(), r, r)
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


class _PillButton(QWidget):
    """胶囊内的可点击圆形按钮（× 取消 / ✓ 确认）。

    父窗口（胶囊）整体鼠标穿透，只有这两个子控件接收点击，
    其余区域点击直达下方输入框。
    """
    clicked = Signal()

    def __init__(self, parent, kind: str):
        super().__init__(parent)
        self.kind = kind
        self.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        self.setCursor(Qt.PointingHandCursor)

    def mousePressEvent(self, event):  # noqa: N802
        # 任何胶囊上的鼠标交互都先通知宿主把前台交还用户原来的应用。
        # 即使非激活风格位在个别系统版本上失效，也不会切走用户界面。
        parent = self.parent()
        if parent is not None and hasattr(parent, "interaction_started"):
            parent.interaction_started.emit()
        self.clicked.emit()
        event.accept()


class _PillWindow(_FloatingWindow):
    """单条胶囊浮层：左 × / 实时文字 / 分隔 / 波形 / 右 ✓。

    配色完全取自当前主题（ui_bg / ui_panel / ui_fg / ui_accent / waves），
    跟随主题切换；×（取消）、✓（确认）圆心与胶囊两端半圆严格同心。
    整条可拖动，右边缘可横向拉伸调宽（拉长显示更多识别文字），
    两者都随配置持久化。状态文案一律不显示，中间只留实时识别文字。
    """
    cancel_requested = Signal()
    confirm_requested = Signal()
    interaction_started = Signal()  # 胶囊上任何鼠标按下（点按钮/拖动/拉伸）
    moved = Signal()          # 拖动结束
    resized = Signal(int)     # 拉伸结束，携带新宽度

    def __init__(self, theme: Theme, width: int = PILL_W):
        width = max(PILL_W_MIN, min(PILL_W_MAX, int(width)))
        super().__init__("sayo Pill", width, PILL_H,
                         mouse_transparent=False, nonactivating=True)
        self.theme = theme
        self._w = width
        self._text = ""
        self._status = ""
        self._is_live = False
        self._state = "idle"
        self._level = 0.0
        self._bands = [0.0] * PILL_BANDS
        self._phase = 0
        self._visible = False
        self._drag_offset = None   # 非 None = 正在拖动整条
        self._resize_start = None  # (起点全局 x, 起始宽度) = 正在拉伸
        # 悬停提示气泡状态（macOS 非激活面板原生 tooltip 不可靠，改为自绘）
        self._oy = TIP_H
        self._tip_text = ""
        self._tip_rect = None
        self._tip_x = width / 2.0
        self.setMouseTracking(True)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(40)

        self._btn_cancel = _PillButton(self, "cancel")
        self._btn_confirm = _PillButton(self, "confirm")
        self._btn_cancel.clicked.connect(self.cancel_requested)
        self._btn_confirm.clicked.connect(self.confirm_requested)
        # 注：macOS 非激活浮层的原生 tooltip 不可靠，按钮含义由自绘提示气泡统一给出
        self._relayout()

    # ---------------------------------------------------------- 布局
    def _relayout(self) -> None:
        """按当前宽度重排所有元素。按钮圆心 = 胶囊两端半圆圆心，保证同心。

        胶囊整体绘制下移 TIP_H：窗口上方留出一条透明带，专门画悬停提示气泡。
        """
        cy_local = PILL_H / 2.0
        d = PILL_BTN_R
        # 胶囊两端是半径 PILL_H/2 的半圆，圆心分别落在这两个点上
        self._cx_cancel = cy_local
        self._cx_confirm = self._w - cy_local
        self._cy = cy_local           # 绘制用的局部圆心（paintEvent 会整体下移 TIP_H）
        self._btn_r = d
        cy_widget = TIP_H + cy_local  # 真实按钮控件在窗口坐标系里的 y
        self._cancel_rect = QRect(round(self._cx_cancel - d), round(cy_widget - d),
                                  2 * d, 2 * d)
        self._confirm_rect = QRect(round(self._cx_confirm - d), round(cy_widget - d),
                                   2 * d, 2 * d)
        self._btn_cancel.setGeometry(self._cancel_rect)
        self._btn_confirm.setGeometry(self._confirm_rect)

        self._wave_right = self._cx_confirm - d - 12
        self._wave_left = self._wave_right - 62
        self._sep_x = self._wave_left - 12
        self._text_left = self._cx_cancel + d + 12
        self._text_right = self._sep_x - 8
        if self._text_right - self._text_left < 36:
            # 极窄宽度：缩短波形，优先保留文字区
            self._wave_left += 36 - (self._text_right - self._text_left)
            self._sep_x = self._wave_left - 8
            self._text_right = self._sep_x - 6
        self.setFixedSize(self._w, TIP_H + PILL_H)
        self.update()

    def resize_to(self, width: int) -> None:
        """拖拽右边缘时实时改宽；宽度变化让文字区自然增减可显示字数。"""
        w = max(PILL_W_MIN, min(PILL_W_MAX, int(width)))
        if w == self._w:
            return
        self._w = w
        self._relayout()
        # 改宽会重建窗口 frame，macOS 可能借此把「非激活风格位」清掉，
        # 导致拖拽拉长时浮层突然变成普通窗口、把界面切回本应用（回到桌面）。
        # 立刻重新套用原生属性，确保整个拉长过程浮层始终保持非激活。
        self.apply_native()

    # ---------------------------------------------------------- 拖动 / 拉伸
    @property
    def is_dragging(self) -> bool:
        """拖动或拉伸中为 True，宿主据此在周期性复位时跳过胶囊定位。"""
        return self._drag_offset is not None or self._resize_start is not None

    def _in_resize_zone(self, x) -> bool:
        return x >= self._w - PILL_RESIZE_ZONE

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.LeftButton:
            # 拖动/拉伸绝不能把用户正在输入的应用切到后台
            self.interaction_started.emit()
            self._clear_tip()  # 拖拽/拉伸期间不显示提示气泡
            if self._in_resize_zone(event.position().x()):
                self._resize_start = (event.globalPosition().toPoint().x(), self._w)
            else:
                self._drag_offset = (event.globalPosition().toPoint()
                                     - self.frameGeometry().topLeft())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):  # noqa: N802
        if self._resize_start is not None and (event.buttons() & Qt.LeftButton):
            gx = event.globalPosition().toPoint().x()
            start_gx, start_w = self._resize_start
            self.resize_to(start_w + gx - start_gx)
            event.accept()
            return
        if self._drag_offset is not None and (event.buttons() & Qt.LeftButton):
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()
            return
        x = event.position().x()
        self.setCursor(Qt.SizeHorCursor if self._in_resize_zone(x)
                       else Qt.OpenHandCursor)
        if self._in_resize_zone(x):
            self._set_tip("拖动右边缘：向左缩短 · 向右拉长", x)
        else:
            self._set_tip("拖动可移动位置 · 点 × 取消 · 点 ✓ 写入", x)
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):  # noqa: N802
        self._clear_tip()
        super().leaveEvent(event)

    # ---------------------------------------------------------- 悬停提示气泡
    def _set_tip(self, text: str, x: float) -> None:
        text = (text or "").strip()
        if text == self._tip_text:
            self._tip_x = x
            return
        self._tip_text = text
        self._tip_x = x
        self._tip_rect = self._compute_tip_rect(text, x) if text else None
        self.update()

    def _clear_tip(self) -> None:
        if self._tip_text or self._tip_rect is not None:
            self._tip_text = ""
            self._tip_rect = None
            self.update()

    def _compute_tip_rect(self, text: str, x: float):
        f = QFont()
        f.setPointSize(11)
        fm = QFontMetrics(f)
        tw = fm.horizontalAdvance(text)
        bw = tw + 18
        bh = TIP_H - 8
        bx = max(2.0, min(self._w - bw - 2.0, x - bw / 2.0))
        return QRectF(bx, 4.0, bw, bh)

    def _draw_tip(self, p: QPainter) -> None:
        t = self.theme
        c_panel = QColor(t.ui_panel)
        c_border = QColor(t.ui_border)
        c_fg = QColor(t.ui_fg)
        r = self._tip_rect
        rr = r.height() / 2.0
        p.setBrush(QBrush(_with_alpha(c_panel, 240)))
        p.setPen(QPen(c_border, 1))
        p.drawRoundedRect(r, rr, rr)
        f = QFont()
        f.setPointSize(11)
        p.setFont(f)
        p.setPen(c_fg)
        p.setBrush(Qt.NoBrush)
        p.drawText(r, Qt.AlignCenter, self._tip_text)

    def mouseReleaseEvent(self, event):  # noqa: N802
        if self._resize_start is not None:
            self._resize_start = None
            self.resized.emit(self._w)
            event.accept()
            return
        if self._drag_offset is not None:
            self._drag_offset = None
            self.moved.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    # ---------------------------------------------------------- 数据入口
    def set_theme(self, theme: Theme) -> None:
        self.theme = theme
        self.update()

    def set_state(self, state: str) -> None:
        self._state = state
        self.update()

    def set_content(self, text: str, status: str, is_live: bool) -> None:
        self._text = (text or "").strip()
        self._status = (status or "").strip()
        self._is_live = is_live
        self.update()

    def set_visible(self, visible: bool) -> None:
        self._visible = visible
        self.update()

    def set_interactive(self, on: bool) -> None:
        """胶囊可点击时（pill 样式 + 录音中）接收鼠标；否则整体穿透。"""
        self._btn_cancel.setAttribute(Qt.WA_TransparentForMouseEvents, not on)
        self._btn_confirm.setAttribute(Qt.WA_TransparentForMouseEvents, not on)
        self.set_mouse_transparent(not on)

    def set_level(self, level: float) -> None:
        try:
            lvl = float(level)
        except (TypeError, ValueError):
            lvl = 0.0
        if lvl < NOISE_FLOOR:  # 底噪门限：静音时波形保持静止
            lvl = 0.0
        self._level = min(1.0, max(self._level * 0.92, lvl))
        if self._visible:
            self.update()

    def set_audio(self, payload) -> None:
        try:
            bands, level, rms = payload
        except Exception:
            return
        try:
            boosted = min(1.0, max(float(level), float(rms) * 40.0))
        except (TypeError, ValueError):
            boosted = 0.0
        if boosted < NOISE_FLOOR:  # 底噪门限：静音时波形保持静止
            boosted = 0.0
        self._level = min(1.0, max(self._level * 0.90, boosted))
        if bands:
            for i in range(min(PILL_BANDS, len(bands))):
                try:
                    v = min(1.0, max(0.0, float(bands[i])))
                except (TypeError, ValueError):
                    continue
                self._bands[i] = max(v, self._bands[i] * 0.62)
        if self._visible:
            self.update()

    def reset(self) -> None:
        self._level = 0.0
        self._bands = [0.0] * PILL_BANDS
        self.update()

    def _tick(self) -> None:
        if not self._visible:
            return
        self._phase += 1
        self._bands = [v * 0.90 for v in self._bands]
        self._level *= 0.93
        self.update()

    # ---------------------------------------------------------- 绘制
    def _band_at(self, t: float) -> float:
        pos = t * (PILL_BANDS - 1)
        i = int(pos)
        frac = pos - i
        a = self._bands[i]
        b = self._bands[min(PILL_BANDS - 1, i + 1)]
        return a + (b - a) * frac

    def _draw_btn(self, p, cx, cy, r, bg, glyph, gcolor, border=None):
        """圆底与符号共用同一个浮点圆心 (cx, cy)，严格同心。

        不再用 QRect.center()：整数 QRect 的中心有 0.5px 截断，叠加
        drawEllipse(QRect) 的端点语义，会让符号看起来偏出圆心。
        """
        p.setPen(QPen(border, 1) if border else Qt.NoPen)
        p.setBrush(QBrush(bg))
        p.drawEllipse(QRectF(cx - r, cy - r, 2 * r, 2 * r))
        if glyph is None or gcolor is None:
            return
        s = r * 0.56  # 符号半径
        p.setPen(QPen(gcolor, 2.4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(Qt.NoBrush)
        if glyph == "x":
            # 两条完全对称的斜线，交点恰好是圆心
            p.drawLine(QPointF(cx - s, cy - s), QPointF(cx + s, cy + s))
            p.drawLine(QPointF(cx - s, cy + s), QPointF(cx + s, cy - s))
        elif glyph == "check":
            # 包围盒上下/左右都以圆心对称（视觉重心也居中）
            path = QPainterPath()
            path.moveTo(cx - 0.95 * s, cy + 0.02 * s)
            path.lineTo(cx - 0.18 * s, cy + 0.58 * s)
            path.lineTo(cx + 0.95 * s, cy - 0.58 * s)
            p.drawPath(path)

    def paintEvent(self, event):  # noqa: N802
        if not self._visible:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        # 悬停提示气泡画在窗口顶部预留的透明带上（不随胶囊下移）
        if self._tip_text and self._tip_rect is not None:
            self._draw_tip(p)
        p.translate(0, self._oy)
        w = self._w
        t = self.theme

        # 所有颜色都取自当前主题，不引入主题外的杂色（切换主题自动跟随）
        c_panel = QColor(t.ui_panel)
        c_bg = QColor(t.ui_bg)
        c_fg = QColor(t.ui_fg)
        c_muted = QColor(t.ui_muted)
        c_accent = QColor(t.ui_accent)
        c_border = QColor(t.ui_border)
        c_wave = QColor(t.waves[0]) if t.waves else c_fg
        dark = (c_bg.red() + c_bg.green() + c_bg.blue()) < 330

        # 单一连续 stadium 路径：一次填充 + 一次描边，没有任何分层投影。
        # 旧实现用三层「向下偏移的圆角矩形」当阴影，偏移的阴影在左右两个
        # 半圆端会露出灰色月牙，加上端弧与直线的分别描边，看起来就像
        # 两个半圆贴片被单独抠出来贴到长条上。现在端弧与上下直线属于
        # 同一条路径，从几何上不可能出现接缝或双层边。
        r = (PILL_H - 1) / 2.0
        shape = QPainterPath()
        shape.addRoundedRect(QRectF(0.5, 0.5, w - 1, PILL_H - 1), r, r)
        grad = QLinearGradient(0, 0, w, 0)
        grad.setColorAt(0, _mix(c_panel, c_accent, 0.05 if dark else 0.025))
        grad.setColorAt(1, c_bg)
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(grad))
        p.drawPath(shape)
        # 只描一圈细边：浅底上也能把胶囊和白色桌面分开
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(_mix(c_border, c_muted, 0.45 if dark else 0.30), 1))
        p.drawPath(shape)

        # × 取消：主题弱色圆 + 主题前景色叉；无描边环，避免「贴纸感」
        self._draw_btn(p, self._cx_cancel, self._cy, self._btn_r,
                       _mix(c_panel, c_muted, 0.16), "x", c_fg)

        # 实时识别文字：只画 _text，绝不画状态文案；
        # 按可用宽度截断——把胶囊拉长就显示更多字，拉短就更少。
        text = self._text
        if text:
            f = QFont()
            f.setPointSize(12)
            f.setWeight(QFont.Medium)
            p.setFont(f)
            fm = p.fontMetrics()
            maxw = max(20, self._text_right - self._text_left)
            tw = fm.horizontalAdvance(text)
            if tw > maxw:
                while tw > maxw and len(text) > 1:
                    text = text[:-1]
                    tw = fm.horizontalAdvance(text + "…")
                text += "…"
            p.setPen(c_fg)
            p.setBrush(Qt.NoBrush)
            p.drawText(QRectF(self._text_left, 0, maxw, PILL_H),
                       Qt.AlignVCenter | Qt.AlignLeft, text)

        # 分隔线：主题弱化色
        p.setPen(QPen(_with_alpha(c_muted, 110), 1.2))
        p.drawLine(QPointF(self._sep_x, 14), QPointF(self._sep_x, PILL_H - 14))

        # 波形：主题曲线色。静音时电平归 0 → 一根平线；说话越多线越多。
        level = self._level
        count = 1 + min(3, int(level * 4.0))
        cy = PILL_H / 2.0
        base_amp = 3.0 + 11.0 * level
        span = self._wave_right - self._wave_left
        points = 40
        for i in range(count):
            amp = base_amp * (1.0 - i * 0.12)
            path = QPainterPath()
            for j in range(points):
                u = j / (points - 1)
                v = self._band_at(u)
                phase = self._phase * 0.085 + i * 0.88
                wave = (math.sin(2 * math.pi * (u * 2.1 + phase)) * 0.60 +
                        math.sin(2 * math.pi * (u * 4.3 - phase * 0.62)) * 0.28 +
                        math.sin(2 * math.pi * (u * 7.9 + phase * 1.3)) * 0.12)
                y = (cy + wave * (0.30 + 1.0 * v) * amp +
                     (i - (count - 1) / 2.0) * 2.0)
                x = self._wave_left + u * span
                if j == 0:
                    path.moveTo(x, y)
                else:
                    path.lineTo(x, y)
            p.setPen(QPen(_with_alpha(c_accent, 60), 5.0, Qt.SolidLine,
                          Qt.RoundCap, Qt.RoundJoin))
            p.drawPath(path)
            p.setPen(QPen(c_wave, 1.8, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.drawPath(path)

        # ✓ 确认：主题强调色圆 + 主题背景色勾；同样无描边环
        self._draw_btn(p, self._cx_confirm, self._cy, self._btn_r,
                       c_accent, "check", c_bg)
        p.end()


class RecordingOverlay(QObject):
    """组合两种浮层样式，保持 main.py 使用的 set_state / set_* 接口不变。

      · classic —— 原样式：文字窗口 + 声音曲线窗口，两窗彼此独立、无底板
      · pill    —— 新样式：单条胶囊（配色跟随主题），× / 实时文字 / 波形 / ✓

    两个样式都从启动就常驻（show 一次后永不 hide），保证 macOS 跨 Space 注册
    永不丢失；由 overlay_style 设置决定当前显示哪一个，另一组保持透明+穿透。
    ×/✓ 只在 pill 样式且录音中可点击：× 取消（丢弃），✓ 确认（提交）；
    胶囊整条可拖动、右边缘可拉伸调宽，位置与宽度都记入配置，重启后保持。
    """

    cancel_requested = Signal()
    confirm_requested = Signal()
    interaction_started = Signal()  # 胶囊被点/被拖：宿主据此恢复原前台应用

    def __init__(self, theme_key: str = DEFAULT_THEME, overlay_style: str = "classic",
                 settings=None):
        super().__init__()
        self.theme = get_theme(theme_key)
        self._style = overlay_style
        self._settings = settings
        # 实时识别文字在两种样式下都必须显示（胶囊中间 / 经典上窗）。
        # 旧 live_preview 开关会连带关掉增量解码引擎，让经典模式一片空白，已废弃。
        # 跨重启记忆胶囊位置与宽度
        self._pill_pos = None
        width = PILL_W
        if settings is not None:
            saved = settings.get("pill_pos")
            if isinstance(saved, (list, tuple)) and len(saved) == 2:
                try:
                    self._pill_pos = (int(saved[0]), int(saved[1]))
                except (TypeError, ValueError):
                    self._pill_pos = None
            try:
                width = int(settings.get("pill_width") or PILL_W)
            except (TypeError, ValueError):
                width = PILL_W
        self.text_window = _TextWindow(self.theme)
        self.wave_window = _WaveWindow(self.theme)
        self.pill_window = _PillWindow(self.theme, width)
        self.pill_window.cancel_requested.connect(self.cancel_requested)
        self.pill_window.confirm_requested.connect(self.confirm_requested)
        self.pill_window.interaction_started.connect(self.interaction_started)
        self.pill_window.moved.connect(self._on_pill_moved)
        self.pill_window.resized.connect(self._on_pill_resized)
        self._state = "idle"
        self._status = ""
        self._live_text = ""
        self._done_at = 0.0
        self._maintain_timer = QTimer(self)
        self._maintain_timer.timeout.connect(self._maintain)
        self._place()
        # 关键：三个窗口从启动就常驻（show 一次后永不 hide）。
        # macOS 上窗口一旦 hide()/orderOut 再 show，window server 会丢弃
        # 「跨所有 Space」注册，下次显示被挂回上次所在的 Space（桌面），
        # 于是浮层永远只在桌面出现、切到别的应用就消失。常驻 + 清空内容
        # 就能在视觉上隐藏，同时让跨 Space 注册永不丢失。
        self.text_window.show_overlay()
        self.wave_window.show_overlay()
        self.pill_window.show_overlay()
        self._apply_style_visibility()
        self.text_window.set_content("", "", False)
        self.wave_window.set_visible(False)
        self.pill_window.set_visible(False)

    def apply_theme(self, theme_key: str) -> None:
        """切换主题：立即重绘所有浮层窗口。"""
        self.theme = get_theme(theme_key)
        self.text_window.set_theme(self.theme)
        self.wave_window.set_theme(self.theme)
        self.pill_window.set_theme(self.theme)

    def apply_style(self, style: str) -> None:
        """切换浮层样式（classic / pill）：只改可见分组，不重建窗口。"""
        if style == self._style:
            return
        self._style = style
        self._apply_style_visibility()
        self._sync_text()  # 切换样式后立刻按新样式重刷文字，避免旧的描边大字残留

    def set_live_preview(self, enabled: bool) -> None:
        """已废弃：实时文字在两种样式下始终开启，保留仅为兼容旧调用。"""
        self._sync_text()

    def _apply_style_visibility(self) -> None:
        """依据当前样式开启对应分组，清空另一组内容；绝不主动弹出窗口。

        三个窗口从启动就常驻显示，靠「内容为空 / _visible=False」在视觉上隐藏。
        旧实现只要 _state != idle 就 show 目标样式，而结束听写后 _state 会停在
        done / transcribing，于是用户在设置里切一下样式，屏幕上就凭空多出一个
        空胶囊或一根静止曲线（没有声音、也不消失）。这里只在真正录音中保持显示。
        """
        active = self._state == "recording"
        if self._style == "pill":
            # 经典两窗：清空内容（透明+穿透，视觉上消失）
            self.text_window.set_content("", "", False)
            self.wave_window.set_visible(False)
            # 胶囊：仅当正在录音时才可见/可点击；否则整体穿透
            self.pill_window.set_interactive(active)
            self.pill_window.set_visible(active)
        else:
            # 胶囊：彻底隐藏且点击穿透，避免透明空胶囊抢点击
            self.pill_window.set_visible(False)
            self.pill_window.set_interactive(False)
            # 曲线随录音显隐；文字窗口的内容由 _sync_text() 统一写入
            self.wave_window.set_visible(active)

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
        # 胶囊：优先用上次拖动记忆的位置；没有记忆则居中（拖动中不打断用户）
        if not self.pill_window.is_dragging:
            pw = self.pill_window.width()  # 用户可调宽度
            if self._pill_pos is not None:
                px, py = self._pill_pos
                # 夹到当前屏幕可视范围，防止换显示器/分辨率后跑到屏外
                px = max(g.left(), min(g.right() - pw, px))
                py = max(g.top(), min(g.bottom() - PILL_H, py))
                self.pill_window.move(px, py)
            else:
                self.pill_window.move(g.center().x() - pw // 2, g.top() + 12)

    def _on_pill_moved(self) -> None:
        pos = self.pill_window.pos()
        self._pill_pos = (pos.x(), pos.y())
        if self._settings is not None:
            self._settings.set("pill_pos", [pos.x(), pos.y()])

    def _on_pill_resized(self, width: int) -> None:
        if self._settings is None:
            return
        self._settings.set("pill_width", int(width))
        # 同时固定当前位置：否则拉伸后下一次 _place() 会按新宽度重新居中，
        # 胶囊会突然整体平移一下。
        pos = self.pill_window.pos()
        self._pill_pos = (pos.x(), pos.y())
        self._settings.set("pill_pos", [pos.x(), pos.y()])

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
            self.pill_window.reset()

        # 只有「录音中」才显示浮层；结束后（识别/完成）不再在屏幕上显示文字，
        # 识别结果直接写入输入框。「error」仍显示，方便用户立刻看到错误。
        active = state in ("recording", "error")
        if active:
            self._place()
            if self._style == "pill":
                self.pill_window.set_interactive(True)
                self.pill_window.set_visible(True)
                self.pill_window.show_overlay()
            else:
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
            self.pill_window.set_visible(False)
            self.pill_window.set_interactive(False)
            # 归一为 idle：transcribing / done 都不显示任何东西。否则之后在设置里
            # 切换样式时，apply_style 会误以为「会话仍在进行」而把浮层弹出来。
            self._state = "idle"
        self._sync_text()

    def _sync_text(self) -> None:
        is_live = self._state == "recording" and bool(self._live_text)
        if self._style == "pill":
            # 关键：胶囊样式下经典文字窗口必须始终为空。它从启动就常驻显示，
            # 一旦被写入状态文案，就会以描边大字出现在屏幕别处
            # （「正在聆听…（WorkBuddy）· MacBook Air 麦克风」）。
            self.text_window.set_content("", "", False)
            self.wave_window.set_visible(False)
            # 胶囊中间只放实时识别文字；状态文案一律不显示
            self.pill_window.set_content(self._live_text, "", is_live)
        else:
            # 经典模式：录音时上窗显示实时识别文字（未出字时显示状态），
            # 结束后由 set_state() 清空，屏幕上不留字。不再受已废弃的
            # live_preview 开关影响，避免「说了话却什么都不显示」。
            self.text_window.set_content(self._live_text or self._status,
                                         self._status, is_live)
            self.pill_window.set_content("", "", False)
        self.pill_window.set_state(self._state)

    def _maintain(self) -> None:
        """周期性重申原生层级与位置：应用切换、Space 切换都不会让浮层消失。"""
        if self._state == "idle":
            return
        self._place()
        self.text_window.apply_native()
        self.wave_window.apply_native()
        if self._style == "pill":
            self.pill_window.apply_native()
            # NSPanel 被系统收起时（换 Space / 全屏切换）立刻恢复显示，
            # 避免「点别的地方浮层就没了」在 Tool 窗口上重演。
            if self._state == "recording" and not self.pill_window.isVisible():
                self.pill_window.show_overlay()

    # ------------------------------------------------------------ 数据入口
    def set_live_text(self, text: str) -> None:
        """实时文字：后续音频重解码整句时会修正前面已显示的字。"""
        self._live_text = (text or "").strip()
        self._sync_text()

    def set_level(self, level: float) -> None:
        self.wave_window.set_level(level)
        self.pill_window.set_level(level)

    def set_audio(self, payload) -> None:
        self.wave_window.set_audio(payload)
        self.pill_window.set_audio(payload)
