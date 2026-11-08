#!/usr/bin/env python3
"""把品牌图标源图转换为 app 所需格式（PNG / ICO / ICNS）。

macOS 26+（Tahoe）图标规范：系统对 icns 画布边缘做 alpha 检测——
只要边缘像素 alpha ≤ 252（例如透明圆角、半透明抗锯齿边），就被判定为
旧式图标，强制缩小并垫上系统底板，看起来像「图标外面一圈白的」。

因此本脚本在打包前做一次「满幅方角化」：
  · 画布 1024×1024 全部不透明（alpha=255）
  · 图形背景延伸到画布边缘（方角，不自己画圆角）
  · 圆角由 macOS 显示时用系统 squircle 蒙版自动裁切
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
# 优先用当前黑底成品图；其次退回 icon-source.png
SOURCE = ASSETS / "icon-app-1024.png"
if not SOURCE.exists():
    SOURCE = ASSETS / "icon-source.png"
if not SOURCE.exists():
    SOURCE = ASSETS / "icon.png"

SIZE = 1024


def make_full_bleed(img) -> "Image.Image":
    """把任意源图变成满幅方角不透明方形（macOS 26+ 免「监狱」的必要条件）。"""
    from PIL import Image

    img = img.convert("RGBA")
    if img.size != (SIZE, SIZE):
        img = img.resize((SIZE, SIZE), Image.LANCZOS)
    # 取画布四边中点像素的平均色作为背景延伸色（图形自身的背景色）
    w, h = img.size
    samples = [img.getpixel((w // 2, 2)), img.getpixel((w // 2, h - 3)),
               img.getpixel((2, h // 2)), img.getpixel((w - 3, h // 2))]
    opaque = [p for p in samples if p[3] > 200] or [(10, 10, 12, 255)]
    bg = tuple(sum(p[i] for p in opaque) // len(opaque) for i in range(3))

    base = Image.new("RGBA", (SIZE, SIZE), bg + (255,))
    base.alpha_composite(img)
    # 强制所有像素 alpha=255（边缘任何 ≤252 的半透明都会触发系统底板）
    px = base.load()
    for y in range(SIZE):
        for x in range(SIZE):
            r, g, b, _ = px[x, y]
            if _ != 255:
                px[x, y] = (r, g, b, 255)
    return base


def make_png_ico() -> Path:
    from PIL import Image

    img = Image.open(SOURCE)
    if img.format is None:
        raise SystemExit(f"图标源图无效：{SOURCE}")
    out_img = make_full_bleed(img)
    out = ASSETS / "icon.png"
    out_img.save(str(out))
    out_img.save(str(ASSETS / "icon.ico"),
                 sizes=[(16, 16), (32, 32), (64, 64), (128, 128), (256, 256)])
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
