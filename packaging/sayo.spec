# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包规格：onedir。语音模型不随包，首次使用时联网下载一次。"""
import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

ROOT = Path(os.path.abspath(SPECPATH)).parent
IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"
DEBUG_CONSOLE = os.environ.get("SAYO_DEBUG_CONSOLE", "0") == "1"

ICON = ROOT / "assets" / ("icon.icns" if IS_MAC else "icon.ico" if IS_WIN else "icon.png")
if not ICON.exists():
    ICON = ROOT / "assets" / "icon.png"

# 图标、内置词表随包。语音模型体积 0.24–1.1 GB，不随安装包分发：
# 由 scripts/install_models.command 在首次使用时下载到用户数据目录的 models/。
# 应用运行时仍然完全离线（离线锁禁止一切 IP 连接），下载只由这个独立脚本完成。
datas = [
    (str(ROOT / "assets"), "assets"),
]
binaries = []
hiddenimports = [
    "PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets",
    "app.core.asr", "app.core.pipeline", "app.core.recorder", "app.core.live",
    "app.core.accel", "app.core.audio_gate", "app.core.postprocess", "app.core.dictionary",
    "app.core.storage", "app.core.offline", "app.core.llm",
    "app.platform.context", "app.platform.hotkey", "app.platform.inject",
    "app.platform.permissions", "app.platform.focus",
    "app.ui.overlay", "app.ui.settings_window", "app.ui.history_window",
    "app.ui.quickstart",
]

# sherpa-onnx 带 .so/.dylib/.dll 推理库；sounddevice 带 PortAudio 二进制
for pkg in ("sherpa_onnx", "sounddevice", "pypinyin", "pynput", "pyperclip"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# 可选依赖：本地大模型推理（llama.cpp Metal 库 + metallib 资源），装了才打包
try:
    d, b, h = collect_all("llama_cpp")
    datas += d
    binaries += b
    hiddenimports += h
except Exception:
    pass

if IS_MAC:
    for pkg in ("objc", "Foundation", "AppKit", "Quartz", "AVFoundation",
                "ApplicationServices", "HIServices"):
        try:
            d, b, h = collect_all(pkg)
            datas += d
            binaries += b
            hiddenimports += h
        except Exception:
            pass

a = Analysis(
    [str(ROOT / "entry.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "torch", "tensorflow", "IPython", "jupyter"],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=None)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="sayo",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=DEBUG_CONSOLE,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON) if ICON.exists() else None,
)

coll = COLLECT(
    exe, a.binaries, a.zipfiles, a.datas,
    strip=False, upx=False, upx_exclude=[], name="sayo",
)

if IS_MAC:
    app = BUNDLE(
        coll,
        name="轻述 sayo.app",
        icon=str(ICON),
        bundle_identifier="local.sayo.desktop",
        info_plist={
            "CFBundleName": "轻述 sayo",
            "CFBundleDisplayName": "轻述 sayo",
            "CFBundleShortVersionString": "1.0.0",
            "CFBundleVersion": "1.0.0",
            "LSMinimumSystemVersion": "11.0",
            # 菜单栏常驻：永不成为前台 App，避免显示浮层时抢走输入框光标
            "LSUIElement": True,
            "NSMicrophoneUsageDescription": "仅在本机采集语音用于离线听写，音频不会离开此设备。",
            "NSAppleEventsUsageDescription": "用于把识别结果写入您正在使用的应用。",
            "NSSupportsAutomaticTermination": False,
            "NSHighResolutionCapable": True,
        },
    )
