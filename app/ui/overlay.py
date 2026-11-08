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

from PySide6.QtCore import Qt, QTimer, Signal, QRect, QObject
from PySide6.QtGui import (QColor, QCursor, QFont, QLinearGradient, QBrush,
                           QPainter, QPainterPath, QPen)
from PySide6.QtWidgets import QApplication, QWidget

from .themes import DEFAULT_THEME, Theme, get_theme

WIDTH = 820
TEXT_HEIGHT = 52
WAVE_HEIGHT = 74
BAND_COUNT = 28

# ---- 胶囊浮层（pill 样式，简洁风单条）常量：小巧、清晰 ----
PILL_W = 430
PILL_H = 46
PILL_PAD = 15
PILL_BTN_R = 11
PILL_LOGO = 20
PILL_BANDS = 7

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
        self.clicked.emit()
        event.accept()


class _PillWindow(_FloatingWindow):
    """单条胶囊浮层：左 × / logo / 文字 / 分隔 / 麦克风 / 波形 / 右 ✓。

    形状、底色、布局完全按参考图重设计；与经典两窗样式互不干扰。
    """
    cancel_requested = Signal()
    confirm_requested = Signal()

    def __init__(self, theme: Theme):
        super().__init__("sayo Pill", PILL_W, PILL_H)
        self.theme = theme
        self._text = ""
        self._status = ""
        self._is_live = False
        self._state = "idle"
        self._level = 0.0
        self._bands = [0.0] * PILL_BANDS
        self._phase = 0
        self._visible = False
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(40)

        # 布局坐标（基于常量）
        self._cancel_rect = QRect(PILL_PAD, (PILL_H - 2 * PILL_BTN_R) // 2,
                                  2 * PILL_BTN_R, 2 * PILL_BTN_R)
        self._logo_rect = QRect(PILL_PAD + 2 * PILL_BTN_R + 8,
                                 (PILL_H - PILL_LOGO) // 2, PILL_LOGO, PILL_LOGO)
        cx_conf = PILL_W - PILL_PAD - PILL_BTN_R
        self._confirm_rect = QRect(cx_conf - PILL_BTN_R,
                                   (PILL_H - 2 * PILL_BTN_R) // 2,
                                   2 * PILL_BTN_R, 2 * PILL_BTN_R)
        wave_w = PILL_BANDS * 5 + (PILL_BANDS - 1) * 4
        wave_right = cx_conf - PILL_BTN_R - 16
        self._wave_left = wave_right - wave_w
        cx_mic = self._wave_left - 16 - PILL_BTN_R
        self._mic_rect = QRect(cx_mic - PILL_BTN_R,
                               (PILL_H - 2 * PILL_BTN_R) // 2,
                               2 * PILL_BTN_R, 2 * PILL_BTN_R)
        self._sep_x = cx_mic - PILL_BTN_R - 14
        self._text_left = PILL_PAD + 2 * PILL_BTN_R + PILL_LOGO + 16
        self._text_right = self._sep_x - 8

        self._btn_cancel = _PillButton(self, "cancel")
        self._btn_confirm = _PillButton(self, "confirm")
        self._btn_cancel.setGeometry(self._cancel_rect)
        self._btn_confirm.setGeometry(self._confirm_rect)
        self._btn_cancel.clicked.connect(self.cancel_requested)
        self._btn_confirm.clicked.connect(self.confirm_requested)

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
        """样式切回经典时关闭 ×/✓ 的点击捕获，避免透明空胶囊抢点击。"""
        self._btn_cancel.setAttribute(Qt.WA_TransparentForMouseEvents, not on)
        self._btn_confirm.setAttribute(Qt.WA_TransparentForMouseEvents, not on)

    def set_level(self, level: float) -> None:
        try:
            self._level = min(1.0, max(self._level * 0.92, float(level)))
        except (TypeError, ValueError):
            pass
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

    def _draw_btn(self, p, rect, bg, glyph, gcolor):
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(bg))
        p.drawEllipse(rect)
        cx, cy = rect.center().x(), rect.center().y()
        r = rect.width() * 0.30
        p.setPen(QPen(gcolor, 2.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(Qt.NoBrush)
        if glyph == "x":
            p.drawLine(cx - r, cy - r, cx + r, cy + r)
            p.drawLine(cx - r, cy + r, cx + r, cy - r)
        else:  # check
            path = QPainterPath()
            path.moveTo(cx - r * 0.9, cy)
            path.lineTo(cx - r * 0.1, cy + r * 0.62)
            path.lineTo(cx + r * 1.05, cy - r * 0.72)
            p.drawPath(path)

    def _draw_mic(self, p, rect, active: bool):
        bg = QColor("#4F8BFF") if active else QColor("#B9B6C8")
        self._draw_btn(p, rect, bg, None, None)
        cx, cy = rect.center().x(), rect.center().y()
        p.setPen(QPen(QColor("#FFFFFF"), 2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(int(cx - 4), int(cy - 9), 8, 12, 4, 4)
        p.drawArc(int(cx - 6), int(cy - 3), 12, 11, 25 * 16, 130 * 16)

    def paintEvent(self, event):  # noqa: N802
        if not self._visible:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        # 柔和投影：三层偏移圆角矩形，保证在任何背景上都清晰可见
        for off, alpha in ((3, 38), (2, 26), (1, 16)):
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(0, 0, 0, alpha))
            p.drawRoundedRect(0, off, PILL_W, PILL_H,
                              PILL_H / 2, PILL_H / 2)

        # 胶囊底：清晰可辨的薰衣草紫灰渐变（非浅白，避免看不清）
        grad = QLinearGradient(0, 0, PILL_W, PILL_H)
        grad.setColorAt(0, QColor("#D9D4F0"))
        grad.setColorAt(1, QColor("#C3BCE2"))
        p.setPen(QPen(QColor("#A89FCF"), 1.5))
        p.setBrush(QBrush(grad))
        p.drawRoundedRect(0, 0, PILL_W, PILL_H, PILL_H / 2, PILL_H / 2)

        # × 取消
        self._draw_btn(p, self._cancel_rect, QColor("#FFFFFF"),
                       "x", QColor("#3A3A45"))
        # logo（品牌 S 渐变）
        lx, ly = self._logo_rect.x(), self._logo_rect.y()
        lg = QLinearGradient(lx, ly, lx, ly + PILL_LOGO)
        lg.setColorAt(0, QColor("#4F8BFF"))
        lg.setColorAt(1, QColor("#8B6BFF"))
        f = QFont()
        f.setPointSize(15)
        f.setWeight(QFont.Bold)
        p.setFont(f)
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(lg))
        p.drawText(self._logo_rect, Qt.AlignCenter, "S")

        # 文字
        text = self._text or self._status
        if text:
            if len(text) > 30:
                text = "…" + text[-29:]
            f = QFont()
            f.setPointSize(13)
            f.setWeight(QFont.Medium)
            p.setFont(f)
            fm = p.fontMetrics()
            tw = fm.horizontalAdvance(text)
            maxw = self._text_right - self._text_left
            while tw > maxw and len(text) > 4:
                text = text[:-2] + "…"
                tw = fm.horizontalAdvance(text)
            tx = self._text_left + max(0, (maxw - tw) // 2)
            p.setPen(QColor("#26222E"))
            p.setBrush(Qt.NoBrush)
            p.drawText(int(tx), 0, int(maxw), PILL_H,
                       Qt.AlignVCenter | Qt.AlignLeft, text)

        # 分隔线
        p.setPen(QPen(QColor("#C6C3D8"), 1.5))
        p.drawLine(self._sep_x, 20, self._sep_x, PILL_H - 20)

        # 麦克风
        self._draw_mic(p, self._mic_rect,
                        self._state in ("recording", "transcribing"))

        # 波形
        n = PILL_BANDS
        bw = 5
        gap = (self._wave_left + (PILL_W - PILL_PAD - PILL_BTN_R - 16)
               - self._wave_left - bw * n) / (n - 1) if n > 1 else 0
        cy = PILL_H / 2.0
        for i in range(n):
            v = self._band_at(i / (n - 1)) if n > 1 else self._bands[0]
            h = 5 + 32 * v
            x = self._wave_left + i * (bw + gap)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#3B7BFF"))
            p.drawRoundedRect(int(x), int(cy - h / 2), bw, int(h), bw // 2, bw // 2)

        # ✓ 确认
        done = self._state == "done"
        self._draw_btn(p, self._confirm_rect, QColor("#FFFFFF"),
                       "check", QColor("#2E9E6B") if done else QColor("#9AA0AE"))
        p.end()


class RecordingOverlay(QObject):
    """组合两种浮层样式，保持 main.py 使用的 set_state / set_* 接口不变。

      · classic —— 原样式：文字窗口 + 声音曲线窗口，两窗彼此独立、无底板
      · pill    —— 新样式：单条胶囊（简洁风），所有元素都在一个椭圆框内

    两个样式都从启动就常驻（show 一次后永不 hide），保证 macOS 跨 Space 注册
    永不丢失；由 overlay_style 设置决定当前显示哪一个，另一组保持透明+穿透。
    ×/✓ 按钮只在 pill 样式下可点击，分别请求「取消（丢弃）」与「确认（提交）」。
    """

    cancel_requested = Signal()
    confirm_requested = Signal()

    def __init__(self, theme_key: str = DEFAULT_THEME, overlay_style: str = "classic"):
        super().__init__()
        self.theme = get_theme(theme_key)
        self._style = overlay_style
        self.text_window = _TextWindow(self.theme)
        self.wave_window = _WaveWindow(self.theme)
        self.pill_window = _PillWindow(self.theme)
        self.pill_window.cancel_requested.connect(self.cancel_requested)
        self.pill_window.confirm_requested.connect(self.confirm_requested)
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

    def _apply_style_visibility(self) -> None:
        """依据当前样式开启对应分组、并清空另一组的可见内容。

        若正在听写/识别/完成（非 idle），同时把目标样式的窗口显示出来，
        保证「设置里中途切换样式」时当前会话不会突然失去浮层。
        """
        if self._style == "pill":
            # 经典两窗：清空内容（透明+穿透，视觉上消失）
            self.text_window.set_content("", "", False)
            self.wave_window.set_visible(False)
            # 胶囊：仅当正在听写/识别/完成时才画出来；×/✓ 可点击
            self.pill_window.set_interactive(True)
            if self._state != "idle":
                self.pill_window.set_visible(True)
                self.pill_window.show_overlay()
        else:
            # 胶囊：彻底隐藏且点击穿透，避免透明空胶囊抢点击
            self.pill_window.set_visible(False)
            self.pill_window.set_interactive(False)
            if self._state != "idle":
                self.wave_window.set_visible(True)
                self.text_window.show_overlay()
                self.wave_window.show_overlay()

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
        px = g.center().x() - PILL_W // 2
        self.pill_window.move(px, g.top() + 12)

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

        active = state in ("recording", "transcribing", "done", "error")
        if active:
            self._place()
            if self._style == "pill":
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
        if state in ("done", "error"):
            self._done_at = time.monotonic()
        self._sync_text()

    def _sync_text(self) -> None:
        is_live = self._state == "recording" and bool(self._live_text)
        shown = self._live_text or self._status
        self.text_window.set_content(shown, self._status, is_live)
        self.pill_window.set_content(shown, self._status, is_live)
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
        self.pill_window.set_level(level)

    def set_audio(self, payload) -> None:
        self.wave_window.set_audio(payload)
        self.pill_window.set_audio(payload)
