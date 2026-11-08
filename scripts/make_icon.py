#!/usr/bin/env python3
"""把品牌图标源图（assets/icon-source.png）转换为 app 所需格式。

图标本体来自品牌视觉（用户提供的截图），本脚本只做格式转换与尺寸打包，
不重绘、不改动图标内容。macOS 额外产出 .icns。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
SOURCE = ASSETS / "icon-source.png"
if not SOURCE.exists():
    # 兼容：若只有 icon.png 也当作源图
    SOURCE = ASSETS / "icon.png"


def make_png_ico() -> Path:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtGui import QImage
    img = QImage(str(SOURCE))
    if img.isNull():
        raise SystemExit(f"图标源图无效：{SOURCE}")
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
    png = make_png_ico()
    print("PNG/ICO:", png)
    icns = make_icns(png)
    print("ICNS:", icns or "非 macOS，跳过")
