#!/usr/bin/env python3
"""生成应用图标（纯代码绘制，不依赖外部素材）。macOS 同时产出 .icns。"""
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


def make_png(size: int = 1024) -> Path:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QBrush, QColor, QFont, QImage, QPainter, QPen
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    # 白色圆角底 + 深色声波环
    p.setBrush(QColor("#FAFAFC"))
    p.setPen(QPen(QColor("#D6DAE2"), 6))
    p.drawRoundedRect(24, 24, size - 48, size - 48, size * 0.22, size * 0.22)
    cx, cy = size / 2, size / 2
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#1F2937"))
    p.drawEllipse(int(cx - size * 0.30), int(cy - size * 0.30),
                  int(size * 0.60), int(size * 0.60))
    p.setBrush(QColor("#FAFAFC"))
    p.drawEllipse(int(cx - size * 0.17), int(cy - size * 0.17),
                  int(size * 0.34), int(size * 0.34))
    # 右侧声波弧
    p.setBrush(QColor("#EF4444"))
    p.drawRoundedRect(int(cx + size * 0.05), int(cy - size * 0.22),
                      int(size * 0.075), int(size * 0.44), 40, 40)
    p.setBrush(QColor("#60A5FA"))
    p.drawRoundedRect(int(cx + size * 0.17), int(cy - size * 0.14),
                      int(size * 0.065), int(size * 0.28), 40, 40)
    p.end()
    out = ASSETS / "icon.png"
    img.save(str(out))
    img.save(str(ASSETS / "icon.ico"))  # Qt 自带 ICO 插件，Windows 安装包使用
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
    icns = make_icns(png)
    print("ICNS:", icns or "非 macOS，跳过")
