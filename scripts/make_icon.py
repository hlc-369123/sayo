#!/usr/bin/env python3
"""生成应用图标（纯代码绘制，不依赖外部素材）。macOS 同时产出 .icns。

品牌「轻述 Saylo」视觉规范：
  · 深蓝黑渐变圆角底（squircle），左上偏蓝、右下偏黑蓝
  · 中央蓝→紫渐变 S 标（Brand Blue #4F8BFF → Accent Purple #8B6BFF）
  色值来源见 docs/使用手册.md 与品牌展示板。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
if not ASSETS.exists():
    ASSETS.mkdir(parents=True)

BRAND_BLUE = "#4F8BFF"    # Brand Blue
BRAND_PURPLE = "#8B6BFF"  # Accent Purple
BG_TOP = "#232B4A"        # 深蓝（渐变起点）
BG_BOTTOM = "#0B1020"     # 深黑蓝（渐变终点）

_APP = None


def _app():
    """QFontDatabase 需要 QGuiApplication 实例；离屏渲染复用同一个单例。"""
    global _APP
    if _APP is None:
        from PySide6.QtGui import QGuiApplication
        _APP = QGuiApplication.instance() or QGuiApplication([])
    return _APP


def _grad(x1: float, y1: float, x2: float, y2: float, c0: str, c1: str):
    from PySide6.QtGui import QColor, QLinearGradient
    g = QLinearGradient(x1, y1, x2, y2)
    g.setColorAt(0.0, QColor(c0))
    g.setColorAt(1.0, QColor(c1))
    return g


def _s_font(size_px: float):
    """选一款本机可用的圆润几何无衬线字体来渲染 S 标。"""
    from PySide6.QtGui import QFont, QFontDatabase
    _app()
    fams = set(QFontDatabase.families())
    for fam in ("Avenir Next", "SF Pro Rounded", "Arial Rounded MT Bold",
                "Segoe UI", "Helvetica Neue", "DejaVu Sans", "Arial"):
        if fam in fams:
            f = QFont(fam)
            break
    else:
        f = QFont()
    f.setPixelSize(int(size_px))
    f.setWeight(QFont.Bold)
    return f


def _draw_s(p, size: float, font_ratio: float = 0.52):
    """在已开启抗锯齿的 QPainter 上按字形真实外接框居中绘制渐变 S 标。"""
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QBrush, QFontMetricsF, QPen
    font = _s_font(size * font_ratio)
    p.setFont(font)
    fm = QFontMetricsF(font)
    br = fm.tightBoundingRect("S")          # 字形实际外接框
    x = (size - br.width()) / 2.0 - br.left()
    y = (size - br.height()) / 2.0 - br.top()
    p.setPen(QPen(QBrush(_grad(size * 0.30, size * 0.24, size * 0.70, size * 0.78,
                               BRAND_BLUE, BRAND_PURPLE)), 2))
    p.drawText(QPointF(x, y), "S")


def make_png(size: int = 1024) -> Path:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage, QPainter
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    # 深蓝黑渐变圆角底（满幅，四角透明，呈现 macOS squircle）
    p.setBrush(_grad(0, 0, size, size, BG_TOP, BG_BOTTOM))
    p.drawRoundedRect(0, 0, size, size, size * 0.225, size * 0.225)
    _draw_s(p, size)
    p.end()
    out = ASSETS / "icon.png"
    img.save(str(out))
    img.save(str(ASSETS / "icon.ico"))  # Qt 自带 ICO 插件，Windows 安装包使用
    return out


def make_logo_mark(size: int = 1024) -> Path:
    """透明底、仅 S 标的独立标识（用于 README / 托盘 / 内嵌展示）。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage, QPainter
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    _draw_s(p, size, font_ratio=0.78)
    p.end()
    out = ASSETS / "logo.png"
    img.save(str(out))
    return out


def make_icns(png: Path) -> Path | None:
    if sys.platform != "darwin" or not shutil.which("iconutil"):
        return None
    iconset = ASSETS / "icon.iconset"
    if iconset.exists():
        shutil.rmtree(iconset)
    iconset.mkdir(parents=True)
    for s in (16, 32, 64, 128, 256, 512, 1024):
        subprocess.run(["sips", "-z", str(s), str(s), str(png),
                        "--out", str(iconset / f"icon_{s}x{s}.png")],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if s <= 512:
            subprocess.run(["sips", "-z", str(s * 2), str(s * 2), str(png),
                            "--out", str(iconset / f"icon_{s}x{s}@2x.png")],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    out = ASSETS / "icon.icns"
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(out)], check=True)
    shutil.rmtree(iconset, ignore_errors=True)
    return out


if __name__ == "__main__":
    png = make_png()
    print("PNG:", png)
    logo = make_logo_mark()
    print("LOGO:", logo)
    icns = make_icns(png)
    print("ICNS:", icns or "非 macOS，跳过")
