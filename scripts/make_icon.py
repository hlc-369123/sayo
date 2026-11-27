#!/usr/bin/env python3
"""把品牌图标源图转换为 app 所需格式（PNG / ICO / ICNS）。

平台差异（必须区分，否则 Windows 图标会变回难看的直角黑方块）：

  · macOS 26+（Tahoe）图标规范：系统对 icns 画布边缘做 alpha 检测——
    只要边缘像素 alpha ≤ 252（例如透明圆角、半透明抗锯齿边），就被判定为
    旧式图标，强制缩小并垫上系统底板，看起来像「图标外面一圈白的」。
    因此 macOS 打包前做一次「满幅方角化」：画布全不透明、背景延伸到边缘、
    圆角交给系统 squircle 蒙版裁切。

  · Windows 不做方角化：桌面/任务栏快捷方式直接显示 ICO 自身的 alpha，
    必须保留源图的透明圆角，才能与 WorkBuddy 等应用的圆角图标风格一致。
    若也做满幅方角，桌面图标就会显示成直角黑方块（历史 bug）。
    无 Pillow 环境时用仓库里的 .workbuddy/build_icons.py（标准库实现）兜底。
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



def make_rounded(out_img) -> Path:
    """生成 README 用的圆角透明图标：与桌面图标同源，圆角由系统 squircle 比例模拟。"""
    from PIL import Image, ImageDraw

    w, h = out_img.size
    # macOS app icon 圆角比例约 22.4%；4× 超采样让边缘平滑
    ss = 4
    radius = int(round(w * 0.2237))
    mask = Image.new("L", (w * ss, h * ss), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, w * ss - 1, h * ss - 1), radius=radius * ss, fill=255)
    mask = mask.resize((w, h), Image.LANCZOS)
    rounded = out_img.convert("RGBA")
    rounded.putalpha(mask)
    out = ASSETS / "icon-rounded.png"
    rounded.save(str(out))
    return out


def make_png_ico() -> Path:
    """按平台生成各自专属的图标文件，两边互不覆盖。

    macOS  → assets/icon.png   （满幅方角化，供 iconutil 生成 icon.icns）
    Windows→ assets/icon-windows.ico（保留源图透明圆角，绝不铺满方角）
    Linux  → assets/icon.png   （保留透明圆角）

    关键点：Windows 构建不写 icon.png、macOS 构建不写 icon-windows.ico。
    这样仓库里两套图标永远各归各的，不会出现「Windows 构建把 Mac 图标改掉」
    或「Mac 构建把 Windows 桌面图标改成直角方块」。
    """
    from PIL import Image

    img = Image.open(SOURCE)
    if img.format is None:
        raise SystemExit(f"图标源图无效：{SOURCE}")

    if sys.platform == "win32":
        out = ASSETS / "icon-windows.ico"
        img.convert("RGBA").save(
            str(out),
            sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                   (64, 64), (128, 128), (256, 256)])
        return out

    if sys.platform == "darwin":
        out_img = make_full_bleed(img)
    else:
        # 桌面图标：保留透明圆角，绝不铺满方角
        out_img = img.convert("RGBA")
        if out_img.size != (SIZE, SIZE):
            out_img = out_img.resize((SIZE, SIZE), Image.LANCZOS)
    out = ASSETS / "icon.png"
    out_img.save(str(out))
    make_rounded(out_img)          # README 用：与桌面同源的圆角透明图标
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
    out = make_png_ico()
    if out.suffix == ".ico":
        print("ICO(Windows):", out)
    else:
        print("PNG/ICO:", out)
        icns = make_icns(out)
        print("ICNS:", icns or "非 macOS，跳过")
