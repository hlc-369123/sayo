"""轻述 sayo 入口：全离线 AI 语音键盘（菜单栏常驻应用）。

用法：
    python -m app.main                 启动桌面应用
    python -m app.main --cli           命令行听写（无 GUI，便于断网验收）
    python -m app.main --file a.wav    离线转写本机 wav 文件
    python -m app.main --self-check    检查模型/依赖/离线约束
    python -m app.main --benchmark     本机 CPU/GPU × fp32/int8 实测对比
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from PySide6.QtCore import QEvent, QObject, QTimer, Signal

from .config import (
    APP_NAME, APP_VERSION, Settings, logs_dir, models_dir, resource_root, user_models_dir,
)
from .core.asr import ASREngine
from .core.live import LiveTranscriber
from .core.model_bootstrap import ModelBootstrap
from .core import model_download
from .core.offline import enforce_offline_lock, offline_lock_active
from .core.pipeline import AppContext, DictationPipeline
from .core.recorder import Recorder, RecorderError, read_wav
from .core.storage import Storage
from .platform import permissions
from .platform.context import current_context
from .platform.focus import choose_output, text_input_focused
from .platform.hotkey import HotkeyManager, event_tap_available
from .platform.inject import Clipboard, insert_text
from .platform.platform_text import (
    IS_WIN, combo_text, default_hotkey_hint, need_accessibility_prompt, paste_text,
)

# 进程级离线锁在导入后立即生效（在 Qt / ONNX Runtime 初始化之前）
_BOOT_SETTINGS = Settings()
if _BOOT_SETTINGS.get("offline_lock", True):
    enforce_offline_lock()


def _log_output_event(focused, action: str, status: str) -> None:
    """把输出决策写入本机 logs/output.log，用于排查「为何没自动进输入框」。"""
    try:
        stamp = datetime.now().isoformat(timespec="seconds")
        line = (f"{stamp} focused={focused} action={action} "
                f"ax={permissions.accessibility_granted()} | {status}\n")
        with (logs_dir() / "output.log").open("a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:
        pass


def build_components(settings: Settings):
    storage = Storage()
    asr = ASREngine(models_dir(), settings.data)
    recorder = Recorder(
        settings.data,
        on_level=None,  # GUI 模式下由 Controller 注入电平/频谱回调
        on_auto_stop=None,
    )
    pipeline = DictationPipeline(settings.data, storage, asr, recorder)
    return storage, asr, recorder, pipeline


def hotkey_diagnostics(ctrl, reason: str = "") -> dict:
    """把快捷键健康状态写进本机日志，用于排查「按了没反应」。

    纯本地：只查本机权限与事件 tap，不联网、不上传。
    """
    h = ctrl.hotkeys.health()
    perms = {st.key: st.granted for st in permissions.check()}
    lines = [
        f"时间：{datetime.now().isoformat(timespec='seconds')}",
        f"平台：{sys.platform} · 版本：{APP_VERSION}",
        f"快捷键：{h['bindings']}",
        f"后端：{h.get('backend', '?')} · 已注册 {h.get('registered', 0)} 个组合",
        f"系统热键注册：{h.get('carbon_error') or ('成功' if IS_WIN else '成功（免输入监控授权）')}",
        f"监听线程存活：{h['listener_alive']}",
        f"全局事件监听：{'可用' if h['tap_ok'] else '不可用'} · {h['tap_detail']}",
        f"监听错误：{h['error'] or '无'}",
        f"最近命中：{h['last_hit'] or '（本次启动尚未收到快捷键）'}",
        ("权限：麦克风=%s" % perms.get("microphone")) +
        ("" if IS_WIN else
         " 辅助功能=%s 输入监控=%s（新后端不需要最后一项）" % (
             perms.get("accessibility"), perms.get("input_monitoring"))),
        f"触发原因：{reason or '-'}",
    ]
    path = ""
    try:
        p = logs_dir() / "hotkey.log"
        with p.open("a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n" + "-" * 60 + "\n")
        path = str(p)
    except OSError:
        pass
    h["log_path"] = path
    h["lines"] = lines
    return h


def log_note(message: str, filename: str = "runtime.log") -> None:
    """把运行期异常写到本机日志（纯本地，不联网、不上传）。"""
    try:
        p = logs_dir() / filename
        with p.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} {message}\n")
    except OSError:
        pass


# ------------------------------------------------------------------ 单实例锁
# 保活文件描述符：一旦被 GC 关闭，flock 立刻释放，锁就形同虚设。
_INSTANCE_LOCK_FD: int | None = None


def _acquire_gui_instance_lock() -> bool:
    """确保同一时间只有一个 GUI 实例在运行。

    背景：/Applications 的正式版与 dist/ 里的构建副本曾同时运行。两个进程
    会同时注册同一组全局热键、抢占麦克风、互相静音/恢复系统输出，录到的
    音频只剩零星尖峰（峰值 0.86 但 p90=0.00126），被判为「未检测到有效
    语音」→ 不输出文字、不写历史记录，看起来像整个应用坏掉了。

    用 flock 而非 pidfile：锁由内核持有，进程退出（含崩溃）自动释放，
    不会留下过期锁文件把后续正常启动挡在门外。
    --self-check / --file / --cli 不走 GUI 分支，不受此锁影响。

    任何异常一律 fail-open（放行）：宁可退化成多实例，也绝不让应用起不来。
    """
    global _INSTANCE_LOCK_FD
    import os
    try:
        import fcntl  # macOS / Linux；Windows 无此模块
    except ImportError:
        return True
    try:
        fd = os.open(str(logs_dir() / "sayo.instance.lock"),
                     os.O_CREAT | os.O_RDWR, 0o600)
    except Exception as exc:
        log_note(f"单实例锁初始化失败（已放行）：{type(exc).__name__}: {exc}")
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return False
    except Exception as exc:
        log_note(f"单实例锁获取失败（已放行）：{type(exc).__name__}: {exc}")
        os.close(fd)
        return True
    _INSTANCE_LOCK_FD = fd
    return True


def live_engine_config(settings_data: dict) -> dict:
    """实时预览引擎的配置：在主引擎设置上覆盖预览专用的固定项。

    设置保存后必须用本函数重新生成 live.asr.settings——旧实现只更新了
    主引擎，导致浮层预览一直用旧的语言/灵敏度/VAD，出现「预览空白但终稿
    有字」或相反。抽成单一函数，构造与保存共用，杜绝两处口径漂移。
    """
    cfg = dict(settings_data)
    cfg.update({"precision": "int8", "accelerator": "cpu",
                "sensitivity": settings_data.get("sensitivity", "high"),
                "gpu_recheck": False, "vad_enabled": True, "num_threads": 2})
    return cfg


def build_live_engine(settings: Settings) -> ASREngine:
    """实时预览用 int8 轻量引擎，与主 fp32 引擎隔离，避免并发改模型。

    灵敏度必须与主引擎一致：旧实现写死 standard，轻声说话时能量门控会把
    整句判成静音，预览窗口就一直空白（用户看到「不显示识别的文字」）。
    """
    return ASREngine(models_dir(), live_engine_config(settings.data))


# --------------------------------------------------------------------- CLI
def run_cli(settings: Settings, wav: str | None = None) -> int:
    storage, asr, recorder, pipeline = build_components(settings)
    ok, msg = asr.check_models()
    if not ok:
        print("[错误]", msg)
        return 2
    print(f"{APP_NAME} v{APP_VERSION} · 离线锁={offline_lock_active()} · 模型={asr.model_name}")
    if wav:
        audio = read_wav(wav)
        r = pipeline.process(audio, audio_seconds=len(audio) / 16000)
        print("原文：", r.raw)
        print("输出：", r.text)
        print("改写：", "、".join(r.applied) or "无")
        print(f"耗时：{r.duration_ms} ms · 推理：{asr.status()}")
        return 0
    print("命令行交互：回车开始录音 3 秒，q 退出")
    while True:
        cmd = input("> ").strip().lower()
        if cmd == "q":
            return 0
        try:
            recorder.start()
            time.sleep(3)
            audio = recorder.stop()
            r = pipeline.process(audio, audio_seconds=len(audio) / 16000)
            print("→", r.text or r.warnings)
        except Exception as exc:
            print("[错误]", exc)
            try:
                recorder.stop()
            except Exception:
                pass


def run_probe_json(settings: Settings) -> int:
    """免麦克风试识别（子进程模式）：只输出一行 JSON，避免拖住主界面。"""
    import json

    try:
        _storage, asr, _recorder, pipeline = build_components(settings)
        ok, msg = asr.check_models()
        if not ok:
            print(json.dumps({"ok": False, "error": "语音模型未安装：" + msg},
                             ensure_ascii=False))
            return 2
        sample = resource_root() / "assets" / "sample_zh.wav"
        audio = read_wav(str(sample))
        result = pipeline.process(audio, audio_seconds=len(audio) / 16000)
        print(json.dumps({"ok": True, "text": result.text or "（未识别到文字）"},
                         ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                         ensure_ascii=False))
        return 1


def run_key_diagnosis(settings: Settings) -> int:
    """在真实 sayo app 进程内诊断快捷键链路（权限 / tap / 真实事件）。

    分四步，每步打印原始状态，定位问题到底在哪一环：
      1) 输入监控权限（IOHIDCheckAccess 原始返回值，正确请求类型 = 1）
      2) CGEventTap 能否创建 + 是否启用
      3) 安装 tap 后，监听真实按键事件（请手动按 左/右 Option / a 键）
      4) 汇总诊断
    不修改任何业务逻辑，只读 + 监听，用于把根因锁定到具体环节。
    """
    import ctypes
    import time
    import sys
    import threading

    print("=" * 60)
    print("快捷键链路诊断（真实 app 进程内执行）")
    print("=" * 60)

    # ---- 1) 输入监控权限（正确请求类型 = 1）----
    IOKIT = "/System/Library/Frameworks/IOKit.framework/IOKit"
    print("\n[1] 输入监控权限 IOHIDCheckAccess：")
    try:
        lib = ctypes.cdll.LoadLibrary(IOKIT)
        for rt in (1, 16):
            lib.IOHIDCheckAccess.argtypes = [ctypes.c_uint32]
            lib.IOHIDCheckAccess.restype = ctypes.c_uint32
            status = int(lib.IOHIDCheckAccess(rt))
            meaning = {0: "Granted(已授权)", 1: "Denied(拒绝)", 2: "Unknown(未决定)"}
            print(f"    IOHIDCheckAccess(request_type={rt}) = {status} = {meaning.get(status, '未知值')}")
    except Exception as exc:
        print(f"    查询失败: {exc}")
    print("    → 正确请求类型是 1；若 IOHIDCheckAccess(1)=0 说明输入监控【已授权】")

    # ---- 2) CGEventTap 创建 + 启用 ----
    print("\n[2] CGEventTap 创建与启用：")
    try:
        import Quartz
    except Exception as exc:
        print(f"    无法导入 Quartz: {exc}")
        return 1
    mask = (Quartz.CGEventMaskBit(Quartz.kCGEventFlagsChanged)
            | Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown)
            | Quartz.CGEventMaskBit(Quartz.kCGEventKeyUp))
    received = []

    def on_event(proxy, etype, event, refcon):
        try:
            keycode = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
            flags = int(Quartz.CGEventGetFlags(event))
            tname = {Quartz.kCGEventFlagsChanged: "flagsChanged",
                     Quartz.kCGEventKeyDown: "keyDown",
                     Quartz.kCGEventKeyUp: "keyUp"}.get(etype, str(etype))
            received.append((tname, keycode, flags))
        except Exception:
            pass
        return event

    tap = Quartz.CGEventTapCreate(
        Quartz.kCGSessionEventTap, Quartz.kCGHeadInsertEventTap,
        Quartz.kCGEventTapOptionListenOnly, mask, on_event, None)
    print(f"    CGEventTapCreate = {'成功' if tap else '失败(None)'}")
    if tap:
        Quartz.CGEventTapEnable(tap, True)
        print(f"    CGEventTapIsEnabled(刚启用后) = {Quartz.CGEventTapIsEnabled(tap)}")
    else:
        print("    tap 创建失败 → 输入监控可能未授权；单键监听无法工作。")
        return 1

    # ---- 关键：tap 放独立线程跑该线程自己的 runloop（与主线程 Qt 循环解耦）----
    tap_thread = threading.Thread(target=lambda: _pump_tap(tap, mask, on_event, received),
                                  daemon=True, name="diag-tap-thread")

    import time as _time
    # 让 tap 线程安装 source 并跑 8 秒
    def _pump_tap(t, m, cb, out):
        import Quartz as _Q
        import time as _T
        source = _Q.CFMachPortCreateRunLoopSource(None, t, 0)
        runloop = _Q.CFRunLoopGetCurrent()
        _Q.CFRunLoopAddSource(runloop, source, 0)  # CommonCategories
        print(f"    tap 线程 runloop 已挂载，enabled={_Q.CGEventTapIsEnabled(t)}")
        for _ in range(80):
            _Q.CFRunLoopRunInMode(0, 0.1, True)
        _Q.CGEventTapEnable(t, False)
        _Q.CFRunLoopRemoveSource(runloop, source, 0)

    tap_thread.start()
    _time.sleep(1)

    # ---- 3) 监听真实按键 ----
    print("\n[3] 现在请真实按键（约 8 秒窗口，每次按下→松开都记一次）：")
    print("    - 左 Option 按 2 次")
    print("    - 右 Option 按 2 次")
    print("    - 按 a 键 1 次")
    print("    开始…")
    tap_thread.join(timeout=10)

    print(f"\n[4] 诊断汇总：共收到 {len(received)} 个键盘事件")
    left_opt = [r for r in received if r[0] == "flagsChanged" and r[1] == 58]
    right_opt = [r for r in received if r[0] == "flagsChanged" and r[1] == 61]
    a_key = [r for r in received if r[0] in ("keyDown", "keyUp") and r[1] == 0]
    print(f"    左 Option flagsChanged: {len(left_opt)}")
    print(f"    右 Option flagsChanged: {len(right_opt)}")
    print(f"    a 键 keyDown/Up:      {len(a_key)}")
    print("    原始事件（前 12 条）：")
    for tname, keycode, flags in received[:12]:
        print(f"      [{tname}] keycode={keycode} flags=0x{flags:x}")
    print("\n判定：")
    if a_key and not left_opt:
        print("    ✓ 普通键能收到、✗ 修饰键 flagsChanged 收不到 → 问题在修饰键事件投递")
    elif left_opt:
        print("    ✓ 修饰键 flagsChanged 能收到 → 底层监听正常，问题在上层状态机/注册")
    elif not a_key and not left_opt:
        print("    ✗ 连普通键都收不到 → tap 未真正工作（权限/runloop 问题）")
    print("\n诊断完成。")
    return 0


def self_check(settings: Settings) -> int:
    from .core import accel
    from .core.offline import audit_source
    from pathlib import Path
    storage, asr, _, _ = build_components(settings)
    ok, msg = asr.check_models()
    print(f"版本：{APP_NAME} v{APP_VERSION}")
    print("离线锁：", "已启用" if offline_lock_active() else "未启用")
    print("模型：", "完整" if ok else msg)
    print("模型目录：", models_dir())
    for line in accel.status_lines():
        print("加速：", line)
    print("推理配置：", accel.capability_summary(settings.data))
    print("数据目录：", settings.path.parent)
    print("历史条数：", len(storage.search_history()))
    try:
        print("麦克风设备：", len(Recorder.devices()))
    except Exception as exc:
        print("麦克风设备：无法枚举 -", exc)
    print("权限：")
    for st in permissions.check():
        print(f"  - {st.label}: {'已授权' if st.granted is True else ('未授权' if st.granted is False else '需人工确认')}")
    from .platform.focus import describe as focus_desc
    print("焦点检测：", focus_desc())
    from .platform.hotkey import (
        carbon_combo, is_single_modifier, is_single_plain_key, valid_hotkey,
    )
    for label, key in (("听写快捷键", "hotkey_dictate"), ("编辑快捷键", "hotkey_ask")):
        combo = settings.get(key)
        if IS_WIN:
            mapping = "系统级键盘监听（免授权）"
        elif carbon_combo(combo):
            mapping = "可注册（免授权）"
        elif is_single_modifier(combo) or is_single_plain_key(combo):
            mapping = ("单键热键（Windows 默认能力，无需额外授权）" if IS_WIN
                       else "单键热键（系统事件监听，需输入监控授权）")
        elif not valid_hotkey(combo):
            mapping = "无效组合，需要重新设置"
        else:
            mapping = "将回退到键盘监听"
        print(f"{label}：{combo} · 系统热键映射：{mapping}")
    tap_ok, tap_detail = event_tap_available()
    print("全局键盘监听（单键热键/回退后端使用）：", "可用" if tap_ok else "不可用", "-", tap_detail)
    if IS_WIN:
        print("说明：Windows 下全局热键由系统级键盘监听实现，无需额外授权、覆盖安装后不失效。"
              "麦克风需在「设置 → 隐私和安全性 → 麦克风」里允许桌面应用访问；"
              "文字注入为系统默认能力，无额外授权项。")
    else:
        print("说明：组合键（如 ⌘+Shift+空格）由 Carbon 注册、免授权，可靠且覆盖安装后不失效。"
              "单个按键（如 option/fn）需输入监控授权，但 macOS 系统限制修饰键不产生 "
              "keyDown/keyUp 事件，单键修饰键可能不可靠；组合键最稳定。")
    try:
        from pynput.keyboard import Listener  # noqa: F401
        print("键盘监听模块 pynput：随包可用")
    except Exception as exc:
        print("键盘监听模块 pynput：不可用 -", exc)
    system_modules = ("ApplicationServices", "AVFoundation", "AppKit") if sys.platform == "darwin" else ()
    for mod in system_modules:
        try:
            __import__(mod)
            print(f"本机系统模块 {mod}：随包可用")
        except Exception as exc:
            print(f"本机系统模块 {mod}：不可用 - {exc}")
    if sys.platform == "win32":
        try:
            import winreg  # noqa: F401
            print("Windows 系统模块 winreg：随包可用")
        except Exception as exc:
            print("Windows 系统模块 winreg：不可用 -", exc)
    hits = audit_source(str(Path(__file__).resolve().parent))
    print("源码联网关键字扫描：", "通过（0 处）" if not hits else f"发现 {len(hits)} 处")
    for h in hits:
        print("   ", h)
    return 0 if ok and not hits else 1


def asr_signature(s: dict) -> tuple:
    """用于判断设置保存后是否需要重新加载识别器。"""
    from .core import accel
    backend, _ = accel.choose_backend(s.get("accelerator", "auto"),
                                      s.get("precision", "auto"),
                                      s.get("sensitivity", "standard"))
    return (s.get("model"), s.get("precision"), backend.key,
            s.get("language"), s.get("num_threads"))


def run_benchmark(settings: Settings, wav: str | None = None) -> int:
    """在本机实测 CPU / GPU × fp32 / int8 的加载与推理耗时（纯本地，无联网）。"""
    import numpy as np
    from .config import resource_root
    from .core import accel
    from .core.asr import ASREngine

    if wav:
        audio = read_wav(wav)
    else:
        sample = resource_root() / "assets" / "sample_zh.wav"
        audio = read_wav(str(sample)) if sample.exists() else \
            np.random.default_rng(0).uniform(-0.02, 0.02, 16000 * 3).astype(np.float32)
    audio = np.concatenate([audio, np.zeros(9600, dtype=np.float32)])

    has_gpu, detail, gpu_backend = accel.probe_hardware()
    providers = [("cpu", accel.BACKEND_CPU)]
    if has_gpu and gpu_backend:
        providers.append((gpu_backend.key, gpu_backend))

    print(f"{APP_NAME} · 本机推理基准 · 音频 {len(audio)/16000:.1f}s · {detail}")
    print(f"{'后端':<10}{'精度':<8}{'加载':>8}{'3 次推理 (ms)':>24}  识别结果")
    for provider, backend in providers:
        for precision in ("int8", "fp32"):
            cfg = dict(settings.data)
            cfg.update({"model": "sensevoice", "precision": precision,
                        "vad_enabled": False, "gpu_recheck": False})
            engine = ASREngine(models_dir(), cfg, provider=provider)
            t0 = time.time()
            try:
                engine.ensure_loaded()
                load = time.time() - t0
                times, text = [], ""
                for _ in range(3):
                    t = time.time()
                    r = engine.transcribe(audio, use_vad=False)
                    times.append(int((time.time() - t) * 1000))
                    text = r.text
                print(f"{backend.label[:9]:<10}{precision:<8}{load:>7.2f}s"
                      f"{str(times):>24}  {text[:36]}")
            except Exception as exc:
                print(f"{backend.label[:9]:<10}{precision:<8}{'失败':>8}  {type(exc).__name__}: {str(exc)[:70]}")
    print("说明：GPU 只改变算力；准确率差异来自 fp32 全精度与 VAD/复核策略。")
    return 0


# --------------------------------------------------------------------- GUI
class Controller:
    """非 QObject 的控制器：由 AppSession 持有，所有跨线程调用经 Qt 信号转发。"""

    def __init__(self, session: "AppSession"):
        self.session = session
        self.settings = session.settings
        self.storage, self.asr, self.recorder, self.pipeline = build_components(self.settings)
        self.recorder.on_level = lambda lvl, rms: session.sig_level.emit(lvl)
        self.recorder.on_audio = lambda payload: session.sig_audio.emit(payload)
        self.recorder.on_auto_stop = lambda: session.sig_command.emit("stop")
        self.recorder.on_silent_stream = self._on_silent_stream
        self.live = LiveTranscriber(
            build_live_engine(self.settings),
            on_text=lambda t: session.sig_live_text.emit(t),
            on_sentence=None,
            on_error=lambda m: log_note(m),
            interval_s=float(self.settings.get("live_interval_ms", 550)) / 1000.0,
        )
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="asr")
        self.hotkeys = HotkeyManager()
        self.mode = "dictate"
        self.state = "idle"
        self.context = AppContext()
        self.last_text = ""  # 最近一次听写的整理结果，供一键总结使用
        self.push_to_talk = bool(self.settings.get("push_to_talk"))
        self._front_pid = None
        self._force_paste = False  # 胶囊 ✓ 明确要求写进原输入框
        self._ax_nudged = False  # 每次运行最多自动弹一次授权引导
        self._silent_retries = 0  # 录音流假死自动重试计数（最多一次，防死循环）
        self._force_probe_next = False  # 断流重试时强制同步重探设备
        self._hotkeys_suspended = False  # 设置窗口打开时暂停全局热键

        # 启动后在后台做一次输入设备健康探测：等用户按快捷键时结果已缓存，
        # 不会给「按下 → 开始录音」引入等待。系统默认输入若是断流的蓝牙
        # 链路 / 失效虚拟声卡，这里就能提前发现并准备好备用麦克风。
        try:
            import threading

            def _warmup_input_probe():
                try:
                    _, note = self.recorder.auto_input_device()
                    if note:
                        log_note(f"输入设备自检：{note}")
                except Exception:
                    pass

            threading.Thread(target=_warmup_input_probe, daemon=True,
                             name="input-device-probe").start()
        except Exception:
            pass

    # ---- 焦点保护：开始听写时把前台交还给用户原来的应用，光标不会消失
    def _remember_front(self):
        self._front_pid = None
        if sys.platform != "darwin":
            return
        try:
            import os
            from AppKit import NSWorkspace  # type: ignore
            front = NSWorkspace.sharedWorkspace().frontmostApplication()
            if front and int(front.processIdentifier()) != os.getpid():
                self._front_pid = int(front.processIdentifier())
        except Exception:
            pass

    def _restore_front(self):
        if sys.platform != "darwin" or not self._front_pid:
            return
        try:
            import os
            from AppKit import NSRunningApplication  # type: ignore
            if self._front_pid == os.getpid():
                return  # 本应用是 accessory，永远不该抢前台
            target = NSRunningApplication.runningApplicationWithProcessIdentifier_(
                self._front_pid)
            if not target:
                return
            # 系统设置/授权弹窗不是用户原来正在输入的应用，绝不把前台还给它
            bundle = str(target.bundleIdentifier() or "")
            if bundle.startswith("com.apple.systempreferences") or bundle.startswith("com.apple.Preferences"):
                return
            target.activateWithOptions_(1 << 1)
        except Exception:
            pass

    def suspend_hotkeys(self) -> None:
        """打开设置等需要录按键的窗口时，临时停用全局热键。"""
        self._hotkeys_suspended = True
        self.hotkeys.suspend()

    def resume_hotkeys(self) -> None:
        """关闭设置窗口后恢复全局热键。"""
        self._hotkeys_suspended = False
        self.hotkeys.resume()

    def setup_hotkeys(self) -> str:
        # macOS 把 ⌘+Shift+A 保留给系统，Carbon 注册会成功但永远不派发；
        # 老配置自动迁移到可用的 ⌘+Shift+E，避免「按了没反应」又出现一次。
        legacy = {
            "cmd+shift+a": "cmd+shift+e" if sys.platform == "darwin" else "ctrl+shift+e",
            "ctrl+shift+a": "ctrl+shift+e",
        }
        for key in ("hotkey_dictate", "hotkey_ask"):
            old = str(self.settings.get(key, "")).strip().lower().replace(" ", "")
            if old in legacy:
                self.settings.set(key, legacy[old])
                log_note(f"快捷键 {key} 从 {old} 迁移到 {legacy[old]}（系统保留组合）")
        # 无论是否 suspend 都要更新 bindings：设置窗口开着时保存配置，虽然
        # 此时不能 start()（会抢录入框的按键），但必须把新 combo 写进 bindings，
        # 这样关闭窗口 resume() → start() 时用的就是新配置，而非旧的。
        self.hotkeys.clear()
        dictate = self.settings.get("hotkey_dictate")
        ask = self.settings.get("hotkey_ask")
        if self.push_to_talk:
            self.hotkeys.register(
                dictate,
                lambda: self.session.sig_command.emit("begin"),
                lambda: self.session.sig_command.emit("stop"),
            )
        else:
            self.hotkeys.register(dictate, lambda: self.session.sig_command.emit("toggle"))
        self.hotkeys.register(ask, lambda: self.session.sig_command.emit("ask"))
        if getattr(self, "_hotkeys_suspended", False):
            # 设置窗口开着：只更新 bindings 不注册，关闭时 resume 会 start()
            return ""
        ok = self.hotkeys.start()
        return "" if ok else self.hotkeys.error

    def _on_silent_stream(self, device_name: str) -> None:
        """录音 1.5 秒仍全是静音：先自动重开一次音频流（多数假死一次即愈），
        仍不行再明确告知根因，不再让用户盲猜。"""
        log_note(f"静音输入流报警 device={device_name} retries={self._silent_retries}")
        if self._silent_retries < 1:
            self._silent_retries += 1
            self.session.sig_error.emit(
                f"麦克风输入异常（{device_name}），正在自动切换输入设备并重开音频流，请继续说话…")
            self.session.sig_command.emit("restart_recording")
        else:
            msg = (f"麦克风始终没有声音（当前输入设备：{device_name}）。"
                   "已尝试自动切换输入设备仍未恢复。常见原因：蓝牙耳机已断开、"
                   "系统输入音量为 0、麦克风被别的应用独占。"
                   "请到 系统设置 → 声音 → 输入 选择内置麦克风后重新听写。")
            self.session.sig_error.emit(msg)
            self.session.sig_command.emit("stop")

    def restart_recording(self) -> None:
        """丢弃本次（断流）录音，强制重探设备并重开麦克风流。"""
        self.live.stop()
        try:
            self.recorder.stop()
        except Exception:
            pass
        Recorder.invalidate_device_cache()  # 重听一次，别继续沿用坏设备
        self._force_probe_next = True  # 这次允许同步探测（重试值得等 0.5s，找健康设备）
        self.state = "idle"
        self.begin()

    def _handle_mic_error(self, msg: str) -> None:
        """麦克风打不开时区分根因：权限未授权就主动弹授权框 + 打开设置，别只报错。"""
        log_note(f"麦克风打开失败：{msg}")
        try:
            # 平台通用：macOS 查 TCC，Windows 查注册表 ConsentStore
            mic_ok = (permissions._win_microphone() if IS_WIN
                      else permissions._mac_microphone())  # True / False / None
        except Exception:
            mic_ok = None
        if mic_ok is False:  # 明确未授权
            ok, note = permissions.open_settings_result("microphone")
            if not IS_WIN:  # Windows 无法由应用主动弹授权框，只打开设置页
                try:
                    permissions._prompt_microphone()
                except Exception:
                    pass
            if IS_WIN:
                tip = (
                    "麦克风访问已被系统拒绝。" + note
                    + f" 开启后回到输入框重新按 {default_hotkey_hint()} 听写。")
            else:
                tip = (
                    "麦克风权限未授权：已为您弹出系统授权框并打开「隐私与安全性 → 麦克风」。"
                    "请勾选「轻述 sayo」的开关（若列表里有多个同名条目，先把旧的删掉），"
                    f"然后回到输入框重新按 {default_hotkey_hint()} 听写。")
            self.session.sig_error.emit(tip)
            return
        # 其他原因：设备被占用 / 蓝牙断开 / 无输入设备
        self.session.sig_error.emit(
            f"麦克风打开失败：{msg}\n"
            + ("若刚更换过麦克风或系统默认输入设备，请到系统声音设置 → 输入 确认后重试。"
               if IS_WIN else
               "若刚更换过蓝牙耳机或系统默认输入设备，请到 系统设置 → 声音 → 输入 确认后重试。"))

    def begin(self, mode: str | None = None):
        if self.state != "idle":
            return
        self._force_paste = False
        force_probe = self._force_probe_next
        self._force_probe_next = False  # 本次无论成败都消费掉，不泄漏到下次按键
        self._remember_front()  # 记住用户当前正在输入的应用
        mode = mode or self.mode
        ok, msg = self.asr.check_models()
        if not ok:
            self.session.sig_error.emit(msg)
            return
        try:
            self.context = current_context(with_selection=(mode == "ask"))
        except Exception:
            self.context = AppContext()
        try:
            self.recorder.start(force_probe=force_probe)
        except RecorderError as exc:
            self._handle_mic_error(str(exc))
            return
        except Exception as exc:
            self.session.sig_error.emit(str(exc))
            return
        self._silent_retries = 0  # 流成功打开：重置假死重试计数
        self.state = "recording"
        # 状态里带上真实输入设备名：蓝牙耳机/内置麦切换时用户能立刻发现用错了麦
        device = getattr(self.recorder, "device_name", "")
        note = getattr(self.recorder, "auto_device_note", "")
        if note:
            log_note(f"输入设备自检：{note}")
        suffix = f" · {device}" if device and device != "未知设备" else ""
        if note:
            suffix += " · 已自动切换麦克风"
        self.session.sig_state.emit(
            "recording", f"正在聆听…（{self.context.app_name or '当前应用'}）" + suffix)
        self.session.sig_mode.emit(mode)
        # 浮层显示后把前台交还原应用；用户点回输入框不会中断录音
        self.session.sig_command.emit("restore_front")
        # 两种浮层样式都要显示实时识别文字（胶囊中间 / 经典上窗），
        # 所以增量解码始终启动。旧 live_preview 开关会连带关掉引擎，
        # 导致经典模式一片空白，已废弃。
        try:
            self.live.interval = max(0.05, min(0.5,
                float(self.settings.get("live_interval_ms", 550)) / 1000.0))
            self.live.start(self.recorder.snapshot)
        except Exception as exc:
            log_note(f"实时预览启动失败：{type(exc).__name__}: {exc}")
            # 预览失败不影响正式识别；结果仍会写入输入框

    def stop(self):
        if self.state != "recording":
            return
        self.live.stop()  # 结束增量解码，避免与最终 fp32 识别争用
        audio = self.recorder.stop()
        device_name = str(getattr(self.recorder, "device_name", "") or "")
        seconds = len(audio) / 16000
        audio_path = ""
        if self.settings.get("save_audio", False) and audio.size:
            audio_path = self.recorder.save_wav(audio)
        self.state = "transcribing"
        self.session.sig_state.emit("transcribing", "本地识别与润色中…")
        mode, ctx = self.mode, self.context
        self.executor.submit(self._work, audio, mode, ctx, seconds, audio_path, device_name)

    def cancel(self):
        """取消本次听写：停止录音并丢弃音频，不识别、不写入。"""
        if self.state != "recording":
            return
        self.live.stop()  # 结束增量解码
        try:
            self.recorder.stop()
        except Exception:
            pass
        self.state = "idle"
        self.session.sig_state.emit("idle", "")

    def confirm(self):
        """胶囊 ✓：把结果直接写进用户原来那个输入框，绝不切换界面。

        点 ✓ 已经是明确的「就放这儿」指令，不再走智能焦点探测：点击浮层
        会让 AX 焦点查询短暂失真，旧逻辑因此退化成「只复制进剪贴板」，
        用户看到的就是「点了对勾什么都没进输入框、界面还被切走了」。
        """
        self._force_paste = True
        self._restore_front()
        QTimer.singleShot(60, self.stop)

    def toggle(self):
        if self.state == "idle":
            self.begin()
        elif self.state == "recording":
            self.stop()

    def _work(self, audio, mode, ctx, seconds, audio_path, device_name=""):
        try:
            result = self.pipeline.process(
                audio, mode=mode, context=ctx, audio_seconds=seconds,
                audio_path=audio_path, device_name=device_name
            )
            self.session.sig_result.emit(result)
        except Exception as exc:
            self.session.sig_error.emit(f"识别失败：{exc}")

    def on_result(self, result):
        # 无论本次有没有识别到文字都必须清掉标记，否则「空结果」会把
        # 强制粘贴误留到下一次听写。
        force = self._force_paste
        self._force_paste = False
        status = ""
        if result.text:
            self.last_text = result.text
            # 粘贴目标必须是用户开始听写时所在的那个应用，不是本应用
            self._restore_front()
            # 智能输出：焦点在输入框才模拟粘贴；没有输入框只写剪贴板。
            # 点胶囊 ✓ 时强制按「有输入框」处理，避免 AX 探测不确定导致
            # 结果只进剪贴板。
            smart = bool(self.settings.get("smart_output", True))
            focused = True if force else (text_input_focused() if smart else True)
            action, status = choose_output(
                focused, smart=smart,
                unknown=self.settings.get("smart_output_when_unknown", "clipboard"),
            )
            if action == "clipboard" and smart and \
                    permissions.accessibility_granted() is False:
                # 根因提示：macOS 辅助功能未授权时只允许写剪贴板
                # （焦点检测与粘贴注入都依赖它；Windows 该值恒为 True 不会走到这里）
                status = "辅助功能未授权 · 已复制到剪贴板（菜单栏「授权引导」一键修复）"
                if not self._ax_nudged:
                    self._ax_nudged = True
                    self.session.sig_command.emit("ax_guide")
            if action == "clipboard":
                Clipboard.write(result.text)
            else:
                try:
                    insert_text(
                        result.text,
                        method=self.settings.get("inject_method", "paste"),
                        restore_clipboard=bool(self.settings.get("restore_clipboard", True)),
                    )
                except Exception as exc:
                    Clipboard.write(result.text)
                    self.session.sig_error.emit(f"自动输入不可用，文字已放入剪贴板：{exc}")
                    status = "已复制到剪贴板"
            _log_output_event(focused, action, status)
        for w in result.warnings:
            self.session.sig_error.emit(w)
        applied = "、".join(result.applied[:3]) or "原样输出"
        self.session.sig_state.emit("done", f"{status} · {applied} · {result.duration_ms} ms")
        self.state = "idle"

    def shutdown(self):
        self.live.stop()
        self.hotkeys.stop()
        # 必须等已在运行的识别任务结束再关数据库：cancel_futures 只取消
        # 排队任务，运行中的 _work 随后写历史会撞上已关闭的连接，导致
        # 本次识别结果丢失且无提示。max_workers=1 + 本地推理有限时，
        # wait=True 最坏多等几秒，换来不丢数据。
        self.executor.shutdown(wait=True, cancel_futures=True)
        self.storage.close()


class _Signals(QObject):
    sig_command = Signal(str)
    sig_level = Signal(float)
    sig_audio = Signal(object)
    sig_live_text = Signal(str)
    sig_state = Signal(str, str)
    sig_result = Signal(object)
    sig_error = Signal(str)
    sig_mode = Signal(str)


class AppSession:
    """Qt 信号桥：把热键/音频/识别线程的结果统一派发到 GUI 线程。"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.s = _Signals()
        self.sig_command = self.s.sig_command
        self.sig_level = self.s.sig_level
        self.sig_audio = self.s.sig_audio
        self.sig_live_text = self.s.sig_live_text
        self.sig_state = self.s.sig_state
        self.sig_result = self.s.sig_result
        self.sig_error = self.s.sig_error
        self.sig_mode = self.s.sig_mode


def run_gui(settings: Settings) -> int:
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtGui import QAction, QColor, QIcon, QPixmap
    from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

    from .ui.history_window import HistoryWindow
    from .ui.overlay import RecordingOverlay
    from .ui.permission_guide import PermissionGuideWindow
    from .ui.quickstart import QuickStartWindow
    from .ui.settings_window import SettingsWindow
    from .ui.summary_window import SummaryWindow
    from .ui.themes import DEFAULT_THEME, get_theme, ui_stylesheet
    from .core.sound import (
        set_enabled as set_sound_enabled, play_start, play_end,
        prewarm as sound_prewarm, shutdown as sound_shutdown,
    )
    from .core.mute import set_muted as mute_system_output

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)

    def make_accessory():
        """菜单栏常驻应用：永不成为前台 App，光标不会从输入框里消失。"""
        if sys.platform != "darwin":
            return
        try:
            from AppKit import NSApp  # type: ignore
            if NSApp is not None:
                NSApp.setActivationPolicy_(1)  # NSApplicationActivationPolicyAccessory
        except Exception:
            pass

    def activate_for_dialog():
        """只有用户主动点开的窗口才激活应用；听写浮层绝不调用。"""
        if sys.platform != "darwin":
            return
        try:
            from AppKit import NSApp  # type: ignore
            if NSApp is not None:
                NSApp.activateIgnoringOtherApps_(True)
        except Exception:
            pass

    make_accessory()
    if not QSystemTrayIcon.isSystemTrayAvailable():
        print(f"[{APP_NAME}] 警告：未检测到系统托盘/菜单栏，可通过设置窗口操作。", file=sys.stderr)

    session = AppSession(settings)
    ctrl = Controller(session)

    # 菜单栏（托盘）图标：白色 S（icon-menubar.png）。
    # 注意：PySide6 的 QIcon 没有 setIsTemplate 方法（调用即 AttributeError 崩溃），
    # 白色 PNG 在深色菜单栏上直接显示为白色，效果与系统模板图一致。
    icon_path = resource_root() / "assets" / "icon-menubar.png"
    pix = QPixmap(str(icon_path))
    if pix.isNull():
        pix = QPixmap(64, 64)
        pix.fill(QColor("#FFFFFF"))
    else:
        pix = pix.scaled(64, 64, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    tray_icon = QIcon(pix)

    overlay = RecordingOverlay(settings.get("theme", DEFAULT_THEME),
                               settings.get("overlay_style", "classic"),
                               settings)
    # 胶囊交互绝不切换用户界面：任何鼠标按下都把前台交还用户原来的应用。
    # （NSWindow 已设为非激活面板，这里是系统版本差异下的第二重保险。）
    overlay.interaction_started.connect(
        lambda: QTimer.singleShot(0, ctrl._restore_front))
    # 胶囊样式的 ×/✓：× 取消（丢弃本次听写），✓ 确认（直接写进原输入框）
    overlay.cancel_requested.connect(
        lambda: (ctrl._restore_front(), ctrl.cancel()))
    overlay.confirm_requested.connect(ctrl.confirm)
    # 初始化交互声音开关（来自「设置 → 常规 → 交互声音」）
    set_sound_enabled(bool(settings.get("interaction_sound", True)))
    sound_prewarm()  # 兼容接口：提示音改为按需排队播放，不再预占音频设备
    settings_win = SettingsWindow(settings, ctrl.storage, ctrl.pipeline)
    history_win = HistoryWindow(ctrl.storage, ctrl.pipeline)

    def _use_downloaded_models() -> None:
        """模型就绪后立刻切换引擎目录并预热，无需重启应用、无需等待。"""
        target = user_models_dir()
        engines = [ctrl.asr, getattr(getattr(ctrl, "live", None), "asr", None)]
        for engine in engines:
            if engine is None:
                continue
            engine.models_dir = target
            engine._recognizer = None
            engine._vad = None
            engine._load_key = None

        def _warm_live() -> None:
            try:
                ctrl.live.asr.warmup()
            except Exception as exc:
                log_note(f"实时预览预热失败：{type(exc).__name__}: {exc}")

        try:
            ctrl.executor.submit(ctrl.asr.warmup)
        except Exception:
            pass
        # 实时预览单独一个线程预热，不占用识别线程，保证下一次按键就能立刻出字
        threading.Thread(target=_warm_live, daemon=True, name="live-warmup").start()

    settings_win.models_ready.connect(_use_downloaded_models)

    def latest_text() -> str:
        """总结的默认源文本：最近一次听写的整理结果；没有则取本机历史最近一条。"""
        if ctrl.last_text.strip():
            return ctrl.last_text
        rows = ctrl.storage.search_history(limit=1)
        return rows[0]["polished_text"] if rows else ""

    def generate_summary(text: str, style: str) -> tuple[str, str]:
        return ctrl.pipeline.llm.summarize(text, style)

    summary_win = SummaryWindow(generate_summary, ctrl.executor)

    def quick_probe() -> str:
        """免麦克风试识别：用独立子进程跑，首次加载模型再慢也不会卡住界面。"""
        import json
        import subprocess

        if getattr(sys, "frozen", False):
            cmd = [sys.executable, "--probe-json"]
        else:
            cmd = [sys.executable, "-m", "app.main", "--probe-json"]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=240,
                cwd=str(resource_root()),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired:
            return "测试超时：模型首次加载较慢，请稍等片刻再试一次。"
        lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
        if not lines:
            return "测试失败：识别进程没有返回结果。"
        try:
            data = json.loads(lines[-1])
        except Exception:
            return "测试失败：" + lines[-1][:200]
        if data.get("ok"):
            return str(data.get("text") or "（未识别到文字）")
        return "测试失败：" + str(data.get("error") or "未知错误")

    quickstart = QuickStartWindow(settings, probe=quick_probe)
    guide_win = PermissionGuideWindow(settings)
    def _open_permission_settings_from_guide(key: str) -> None:
        ok, note = permissions.open_settings_result(key)
        guide_win.lb_state.setText(note)
        if not ok:
            tray.showMessage(APP_NAME, note, QSystemTrayIcon.Warning, 8000)

    guide_win.open_settings_requested.connect(_open_permission_settings_from_guide)

    def apply_app_theme():
        """把当前主题套到所有应用窗口（浮层由 overlay.apply_theme 单独处理）。"""
        qss = ui_stylesheet(get_theme(settings.get("theme", DEFAULT_THEME)))
        for win in (settings_win, history_win, quickstart, guide_win, summary_win):
            win.setStyleSheet(qss)

    apply_app_theme()
    settings_win.theme_previewed.connect(overlay.apply_theme)
    settings_win.overlay_style_previewed.connect(overlay.apply_style)

    def rebuild_hotkeys(reason: str) -> dict:
        """重新创建键盘监听：用户在系统设置里授权后必须重建事件 tap。"""
        ctrl.setup_hotkeys()
        return hotkey_diagnostics(ctrl, reason)

    def show_hotkey_diag():
        info = hotkey_diagnostics(ctrl, "用户打开快捷键诊断")
        healthy = info["healthy"]
        carbon = info.get("backend") == "carbon"
        # 从当前配置动态生成快捷键文案，避免把某一平台的写法硬编码进界面
        key_dictate = combo_text(settings.get("hotkey_dictate", default_hotkey_hint()))
        key_ask = combo_text(settings.get("hotkey_ask", ""))
        if healthy and carbon:
            if IS_WIN:
                summary = (
                    "系统级全局热键已生效，覆盖安装后也不会失效。\n\n"
                    f"{key_dictate}：开始 / 结束听写\n"
                    + (f"{key_ask}：语音编辑选中文字\n\n" if key_ask else "\n")
                    + "也可以在「设置 → 常规」改成其他组合键。"
                    "听写期间屏幕顶部会显示胶囊浮层：实时文字与音量波形；"
                    "切换应用或桌面也不会消失。")
            else:
                # macOS 保留原始说明（含「不需要输入监控授权」「option/fn」等字样）
                summary = (
                    "系统级全局热键已生效，不需要「输入监控」授权，覆盖安装后也不会失效。\n\n"
                    f"{key_dictate}（键盘最下方那个空格键）：开始 / 结束听写\n"
                    + (f"{key_ask}：语音编辑选中文字\n\n" if key_ask else "\n")
                    + "也可以在「设置 → 常规」改成其他组合键（免授权）或单个按键"
                    "（需输入监控授权，如 option/fn）。\n"
                    "听写期间屏幕顶部会一直显示两样彼此独立的东西："
                    "上面一行实时文字，下面几条随音量变化的曲线；切换应用或桌面也不会消失。")
        elif healthy:
            summary = (f"快捷键可用。\n\n{key_dictate} 开始 / 结束听写，再按一次结束。")
        else:
            summary = (
                "系统热键未能注册，所以按了没有反应。\n\n"
                f"原因：{info.get('carbon_error') or info.get('tap_detail')}\n\n"
                "常见情况是组合键被别的应用占用；可以在「设置 → 常规」换成别的组合，"
                "或点「重新注册系统热键」重试。"
                + ("" if IS_WIN else "若回退到键盘监听，还需在「输入监控」里勾选本应用。"))
        activate_for_dialog()  # 用户主动打开：让诊断窗口正常置于前台
        box = QMessageBox()
        box.setWindowTitle("快捷键诊断 / 本机授权")
        box.setIcon(QMessageBox.Information if healthy else QMessageBox.Warning)
        box.setText(summary)
        box.setDetailedText("\n".join(info["lines"]) +
                           (f"\n日志：{info['log_path']}" if info["log_path"] else ""))
        b_fix = box.addButton("重新注册系统热键", QMessageBox.ActionRole)
        # Windows 无「输入监控 / 辅助功能」概念，不显示这两个无效按钮
        b_set = b_ax = None
        if not IS_WIN:
            b_set = box.addButton("输入监控设置（仅回退后端）", QMessageBox.ActionRole)
        b_mic = box.addButton("麦克风设置", QMessageBox.ActionRole)
        if not IS_WIN:
            b_ax = box.addButton("辅助功能设置", QMessageBox.ActionRole)
        b_close = box.addButton("关闭", QMessageBox.RejectRole)
        box.setDefaultButton(b_fix if not healthy else b_close)
        box.exec()
        clicked = box.clickedButton()
        if clicked is b_mic:
            permissions.open_settings("microphone")
        elif b_ax is not None and clicked is b_ax:
            permissions.open_settings("accessibility")
        elif clicked is b_fix:
            after = rebuild_hotkeys("诊断窗口点击重新启用")
            tray.showMessage(APP_NAME,
                             f"系统热键已重新注册，现在可以按 {key_dictate}" if after["healthy"]
                             else "系统热键仍未注册，请到「设置 → 常规」更换组合键后重试",
                             QSystemTrayIcon.Information if after["healthy"] else QSystemTrayIcon.Warning,
                             6000)
        elif b_set is not None and clicked is b_set:
            permissions.open_settings("input_monitoring")

    prev_sig = [asr_signature(settings.data)]

    def on_settings_saved(_data):
        ctrl.pipeline.refresh()
        ctrl.asr.settings = settings.data
        # 实时预览引擎用的是独立 cfg 副本，必须同步，否则预览与终稿口径不一致
        ctrl.live.asr.settings = live_engine_config(settings.data)
        ctrl.live.interval = max(0.05, min(0.5,
            float(settings.get("live_interval_ms", 550)) / 1000.0))
        apply_app_theme()
        overlay.apply_theme(settings.get("theme", DEFAULT_THEME))
        overlay.apply_style(settings.get("overlay_style", "classic"))
        set_sound_enabled(bool(settings.get("interaction_sound", True)))
        sound_prewarm()  # 兼容接口：无需预热，下一次提示音直接排队播放
        rebuild_hotkeys("设置保存后重建系统热键")
        new_sig = asr_signature(settings.data)
        if new_sig == prev_sig[0]:
            return
        prev_sig[0] = new_sig

        def _reload():
            try:
                ctrl.asr.reload()
            except Exception as exc:
                session.sig_error.emit(f"推理配置切换失败：{exc}")

        ctrl.executor.submit(_reload)  # 与识别串行，避免替换模型时并发冲突

    settings_win.saved.connect(on_settings_saved)

    class _SettingsHotkeyGuard(QObject):
        """设置窗口显示时停用全局热键、隐藏时恢复。

        系统级 Carbon 热键不区分焦点：设置窗口开着时按 cmd+shift+... 依然
        会触发听写并切走焦点，快捷键录入框因此收不到按键、看起来「没更新、
        按了没反应」。挂起后按键只发给录入框，关闭窗口再恢复热键。
        """

        def eventFilter(self, watched, event):
            if event.type() == QEvent.Show:
                ctrl.suspend_hotkeys()
            elif event.type() == QEvent.Hide:
                ctrl.resume_hotkeys()
            return False

    _settings_guard = _SettingsHotkeyGuard()
    settings_win.installEventFilter(_settings_guard)

    def on_command(cmd: str):
        if cmd == "toggle":
            ctrl.toggle()
        elif cmd == "begin":
            ctrl.begin()
        elif cmd == "stop":
            ctrl.stop()
        elif cmd == "ask":
            ctrl.begin("ask")
        elif cmd == "settings":
            activate_for_dialog()
            settings_win.show()
            settings_win.raise_()
        elif cmd == "history":
            activate_for_dialog()
            history_win.reload()
            history_win.show()
            history_win.raise_()
        elif cmd == "summarize":
            activate_for_dialog()
            summary_win.set_source(latest_text(), auto_run=True)
            summary_win.show()
            summary_win.raise_()
        elif cmd == "help":
            activate_for_dialog()
            quickstart.show()
            quickstart.raise_()
        elif cmd == "hotkey_diag":
            show_hotkey_diag()
        elif cmd == "ax_guide":
            activate_for_dialog()
            guide_win.refresh()
            guide_win.show()
            guide_win.raise_()
        elif cmd == "rebuild_hotkeys":
            rebuild_hotkeys("授权流程结束 / 定时自检后自动重建")
        elif cmd == "restore_front":
            QTimer.singleShot(80, ctrl._restore_front)
        elif cmd == "restart_recording":
            ctrl.restart_recording()
        elif cmd == "quit":
            mute_system_output(False)  # 兜底：退出时若仍处于静音会话则恢复用户音量
            sound_shutdown()           # 允许已排队的提示音播完，不强占设备
            ctrl.shutdown()
            app.quit()

    _prev_state = ["idle"]
    # 延时静音会话：开始提示音实测 0.44s，静音须等它播完（否则把本应用提示音
    # 一起掐掉）；若用户在延时结束前就停录音，token 失效，避免系统永久静音。
    from .core.mute import MuteSession as _MuteSession
    _mute_session = _MuteSession(mute_system_output)
    _START_CUE_MS = 500  # > 0.44s 提示音时长，留一点余量

    def on_state(state, text):
        overlay.set_state(state, text)
        entering = state == "recording" and _prev_state[0] != "recording"
        leaving = _prev_state[0] == "recording" and state != "recording"
        # 语音输入时静音其他活动音频（可在设置关闭）：进入录音静音，结束/取消恢复。
        # 蓝牙耳机输入/输出同一设备时跳过静音，否则会把输入流一起掐断（实测变全零）。
        if entering:
            if (bool(settings.get("mute_others_during_input", False))
                    and not Recorder.shares_output_device()):
                token = _mute_session.begin()
                QTimer.singleShot(_START_CUE_MS, lambda t=token: _mute_session.apply(t))
        elif leaving:
            # 先恢复输出，结束音才能听到；end() 同时让未触发的延时静音失效。
            _mute_session.end()
        # 交互声音：进入 recording 播放开始音，离开 recording 播放结束音。
        if entering:
            play_start()
        elif leaving:
            play_end()
        _prev_state[0] = state

    def on_result(result):
        # 激活应用是异步的：先恢复用户原来的应用，稍等再检测焦点并粘贴。
        # 少了这一步，⌘V 会发给被切到前台的本应用，输入框里什么都不会出现。
        # 识别结果直接写入输入框，屏幕上不留任何文字。
        t_emit = time.perf_counter()
        ctrl._restore_front()

        def _paste_and_measure():
            # 整个粘贴流程放到工作线程：insert_text 内部有 350ms 的
            # 「等目标应用完成粘贴再恢复剪贴板」sleep，放在 GUI 线程会让
            # 界面冻结 350ms。ctrl.on_result 里的 UI 更新都走 Qt Signal
            # （QueuedConnection），工作线程发是安全的。
            t_paste = time.perf_counter()

            def _work():
                try:
                    ctrl.on_result(result)
                finally:
                    # T1/T2/T3 分口径埋点（评审 §7）：三种耗时分开记。
                    # 注：这里测到的是「粘贴指令返回」，并非目标应用真正
                    # 渲染出文字，后者无跨应用可靠回执。
                    t_done = time.perf_counter()
                    log_note(
                        f"T1_asr={getattr(result, 'asr_ms', result.duration_ms)}ms "
                        f"T2_emit_to_paste={(t_paste - t_emit) * 1000:.0f}ms "
                        f"T3_paste_call={(t_done - t_paste) * 1000:.0f}ms "
                        f"total_from_emit={(t_done - t_emit) * 1000:.0f}ms "
                        f"chars={len(result.text)} "
                        f"llm_in_path={'本机大模型整理' in result.applied}",
                        "latency.log")

            threading.Thread(target=_work, daemon=True, name="sayo-paste").start()

        QTimer.singleShot(140, _paste_and_measure)

    def on_error(msg):
        overlay.set_state("error", msg[:42])
        tray.showMessage(APP_NAME, msg[:120], QSystemTrayIcon.Warning, 4000)

    session.sig_command.connect(on_command, Qt.QueuedConnection)
    session.sig_level.connect(overlay.set_level, Qt.QueuedConnection)
    session.sig_audio.connect(overlay.set_audio, Qt.QueuedConnection)
    session.sig_live_text.connect(overlay.set_live_text, Qt.QueuedConnection)
    session.sig_state.connect(on_state, Qt.QueuedConnection)
    session.sig_result.connect(on_result, Qt.QueuedConnection)
    session.sig_error.connect(on_error, Qt.QueuedConnection)

    # ------------------------------------------------------------ 菜单栏
    tray = QSystemTrayIcon(tray_icon)
    tray.setToolTip(f"{APP_NAME} v{APP_VERSION} · 完全离线")
    menu = QMenu()
    act_toggle = QAction("开始 / 结束听写", menu)
    act_toggle.triggered.connect(lambda: session.sig_command.emit("toggle"))
    act_ask = QAction("编辑选中文字（Ask）", menu)
    act_ask.triggered.connect(lambda: session.sig_command.emit("ask"))
    menu.addAction(act_toggle)
    menu.addAction(act_ask)
    menu.addSeparator()
    mode_menu = menu.addMenu("模式")
    mode_actions: dict[str, QAction] = {}

    def set_mode(key: str):
        ctrl.mode = key
        for k, a in mode_actions.items():
            a.setChecked(k == key)
        session.sig_mode.emit(key)

    for key, label in [("dictate", "标准听写"), ("translate", "边说边翻译"), ("ask", "语音编辑 / 问答")]:
        a = QAction(label, menu)
        a.setCheckable(True)
        a.setChecked(key == "dictate")
        a.triggered.connect(lambda checked=False, k=key: set_mode(k))
        mode_actions[key] = a
        mode_menu.addAction(a)
    menu.addSeparator()
    act_diag = QAction("快捷键没反应？诊断 / 修复…", menu)
    act_diag.triggered.connect(lambda: session.sig_command.emit("hotkey_diag"))
    menu.addAction(act_diag)
    act_guide = QAction("授权引导 · 文字自动输入", menu)
    act_guide.triggered.connect(lambda: session.sig_command.emit("ax_guide"))
    menu.addAction(act_guide)
    for label, cmd in [("怎么用…", "help"), ("设置…", "settings"),
                       ("总结最近一次听写…", "summarize"), ("本机历史…", "history")]:
        a = QAction(label, menu)
        a.triggered.connect(lambda checked=False, c=cmd: session.sig_command.emit(c))
        menu.addAction(a)
    menu.addSeparator()
    act_quit = QAction("退出", menu)
    act_quit.triggered.connect(lambda: session.sig_command.emit("quit"))
    menu.addAction(act_quit)
    tray.setContextMenu(menu)
    double_click = getattr(QSystemTrayIcon.ActivationReason, "DoubleClick", QSystemTrayIcon.DoubleClick)
    tray.activated.connect(lambda reason: session.sig_command.emit("settings")
                           if reason == double_click else None)
    tray.show()

    # ------------------------------------------------------------ 启动检查
    # 模型准备完全自动化：装好第一次启动就在后台静默下载（失败重试 2 次，
    # 仍失败则下次启动继续），成功后写入状态文件，以后只做毫秒级比对。
    # 用户不需要点任何按钮，也不需要看安装说明。
    class _ModelBridge(QObject):
        done = Signal(bool, str)
        repair = Signal()

    model_bridge = _ModelBridge()
    bootstrap_holder: dict[str, ModelBootstrap] = {}
    _heal_lock = threading.Lock()

    def _start_model_bootstrap(announce: bool = True) -> None:
        """启动后台模型准备线程（重复调用安全）。"""
        current = bootstrap_holder.get("bootstrap")
        if current is not None and current.is_running():
            return
        bootstrap = ModelBootstrap(
            user_models_dir(),
            on_done=lambda ok, msg: model_bridge.done.emit(ok, msg))
        bootstrap_holder["bootstrap"] = bootstrap
        if not bootstrap.start():
            return
        missing_count = len(model_download.light_missing(user_models_dir()))
        log_note("启动自检：语音模型缺失 %d 个文件，已开始后台下载" % missing_count,
                 "model.log")
        if announce:
            tray.showMessage(
                APP_NAME,
                "首次使用需要准备语音模型（约 240 MB），已在后台自动下载；"
                "完成后会提示你，期间其它功能照常可用。",
                QSystemTrayIcon.Information, 8000)

    def _schedule_model_self_heal() -> None:
        """预热失败时自检模型：文件真的坏了就删掉重新下载，用户什么都不用做。"""
        def work():
            if not _heal_lock.acquire(blocking=False):
                return
            try:
                bad = model_download.verify_cached(user_models_dir(), force=True)
                if not bad:
                    return
                removed: list[str] = []
                for item in bad:
                    try:
                        (user_models_dir() / item.relative).unlink(missing_ok=True)
                        removed.append(item.relative)
                    except Exception:
                        pass
                log_note("自检发现模型文件损坏，已删除并准备重新下载：" + ", ".join(removed),
                         "model.log")
                model_bridge.repair.emit()
            except Exception as exc:
                log_note(f"模型自愈失败：{type(exc).__name__}: {exc}", "model.log")
            finally:
                _heal_lock.release()
        threading.Thread(target=work, daemon=True, name="model-heal").start()

    def _warmup_engines() -> None:
        def _warmup_all():
            try:
                ctrl.asr.warmup()
            except Exception as exc:
                log_note(f"主引擎预热失败：{type(exc).__name__}: {exc}", "model.log")
                _schedule_model_self_heal()
            # 实时预览也必须预热：旧实现第一次说话时才加载 int8 模型（约 0.5 s），
            # 叠加 0.35 s 最小语音长度，首字出现太晚，看起来像「不显示识别的文字」。
            try:
                ctrl.live.asr.warmup()
            except Exception as exc:
                log_note(f"实时预览预热失败：{type(exc).__name__}: {exc}")
            return None
        ctrl.executor.submit(_warmup_all)

    def _on_models_ready(ok: bool, message: str) -> None:
        if not ok:
            # 失败不打扰用户：只写本机日志，下次启动自动再试
            log_note(f"语音模型后台准备未完成：{message}", "model.log")
            return
        log_note(f"语音模型后台准备完成：{message}", "model.log")
        _use_downloaded_models()
        try:
            settings_win.refresh_model_state()
        except Exception:
            pass
        tray.showMessage(APP_NAME, "语音模型已就绪，现在可以直接听写了",
                         QSystemTrayIcon.Information, 5000)

    model_bridge.done.connect(_on_models_ready)
    model_bridge.repair.connect(lambda: _start_model_bootstrap(True))

    missing_models = model_download.light_missing(user_models_dir())
    if missing_models:
        _start_model_bootstrap(True)
    else:
        _warmup_engines()
        ok, msg = ctrl.asr.check_models()
        if not ok:
            tray.showMessage(
                APP_NAME,
                "语音模型未就绪：" + msg + "\n请在「设置 → 识别」中检查。",
                QSystemTrayIcon.Warning, 9000)
    hotkey_warning = ctrl.setup_hotkeys()
    hotkey_diagnostics(ctrl, "应用启动")
    if hotkey_warning:
        tray.showMessage(APP_NAME, hotkey_warning, QSystemTrayIcon.Warning, 6000)

    def _post_start_check():
        # 只做本机状态复查并写日志。绝不自动打开系统设置、绝不弹模态窗口：
        # 那会把用户正在使用的输入框光标抢走（旧版「开始使用后光标就没了」的根因）。
        hotkey_diagnostics(ctrl, "启动后自检（无弹窗）")
        # 每次启动都做一次模型自检：只比对大小 + 指纹，正常情况不读 240 MB 文件。
        missing_now = model_download.light_missing(user_models_dir())
        log_note(
            "启动自检：模型%s（缺失 %d 项）；引擎=%s" % (
                "完整" if not missing_now else "不完整", len(missing_now),
                getattr(ctrl.asr, "status", lambda: "?")()),
            "model.log")
        perms = {st.key: st.granted for st in permissions.check()}
        # Windows 无「辅助功能」概念，绝不能因此弹出一个满是 macOS 文案的窗口。
        # 只有 macOS 才做辅助功能引导；Windows 仅在麦克风被显式拒绝时提醒。
        if need_accessibility_prompt() and perms.get("accessibility") is not True:
            tray.showMessage(
                APP_NAME,
                f"辅助功能未授权：文字只放入剪贴板（{paste_text()} 粘贴）。"
                "已弹出「授权引导」窗口，按步骤勾选即可自动输入",
                QSystemTrayIcon.Information, 7000)
            guide_win.maybe_auto_show()
        elif IS_WIN and perms.get("microphone") is False:
            tray.showMessage(
                APP_NAME,
                "麦克风访问已被系统拒绝：请在 Windows 设置 → 隐私和安全性 → 麦克风 中开启。",
                QSystemTrayIcon.Warning, 7000)
            guide_win.maybe_auto_show()

    # 组合键由 Carbon 注册，免授权。单键（如 option/fn）走 CGEventTap，
    # 需要输入监控授权，但受 macOS 系统限制（修饰键 flagsChanged 不投递），
    # 可能不可靠。启动时按需申请：若用户设了单键热键则申请输入监控。
    from .platform.hotkey import is_single_modifier, is_single_plain_key
    dictate_key = settings.get("hotkey_dictate", "")
    ask_key = settings.get("hotkey_ask", "")
    needs_input_monitoring = (is_single_modifier(dictate_key) or is_single_plain_key(dictate_key)
                              or is_single_modifier(ask_key) or is_single_plain_key(ask_key))
    QTimer.singleShot(1500, lambda: permissions.request_all_async(
        force=False, include_input_monitoring=needs_input_monitoring))
    QTimer.singleShot(8000, _post_start_check)
    if not settings.get("first_run_done", False):
        QTimer.singleShot(900, lambda: (activate_for_dialog(),
                                        quickstart.show(), quickstart.raise_()))

    if missing_models:
        # 模型还在后台下载：这时再说「已就绪 · 完全离线」会和上一条通知自相矛盾
        tray.showMessage(
            APP_NAME,
            f"正在准备语音模型（首次约 240 MB，后台下载中）· 就绪后按 {default_hotkey_hint()} 即可听写",
            QSystemTrayIcon.Information, 6000)
    else:
        tray.showMessage(APP_NAME,
                         f"已就绪 · {default_hotkey_hint()} 开始听写 · 完全离线",
                         QSystemTrayIcon.Information, 4000)
    return app.exec()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=f"{APP_NAME} · 全离线 AI 语音键盘")
    parser.add_argument("--cli", action="store_true", help="命令行模式")
    parser.add_argument("--file", help="离线转写本机 16k wav 文件")
    parser.add_argument("--self-check", action="store_true", help="环境与离线约束自检")
    parser.add_argument("--probe-json", action="store_true",
                        help="内置样本试识别（JSON 输出，供界面子进程调用）")
    parser.add_argument("--benchmark", action="store_true", help="CPU/GPU × fp32/int8 本机实测")
    parser.add_argument("--bench-file", help="基准测试使用的本机 wav")
    parser.add_argument("--diagnose-keys", action="store_true",
                        help="在真实 app 进程内诊断快捷键链路（权限/ tap / 真实事件），需手动按键")
    args = parser.parse_args(argv)
    settings = Settings()
    if args.diagnose_keys:
        return run_key_diagnosis(settings)
    if args.self_check:
        return self_check(settings)
    if args.probe_json:
        return run_probe_json(settings)
    if args.benchmark:
        return run_benchmark(settings, args.bench_file)
    if args.cli or args.file:
        return run_cli(settings, args.file)
    # 只给 GUI 加锁：自检 / 离线转写 / 命令行允许与常驻实例并存。
    if not _acquire_gui_instance_lock():
        # 不能静默退出——用户会以为「点了没反应」。明确告知已有实例，
        # 并提示构建副本（dist/）是常见的重复启动来源。
        log_note("拒绝重复启动：已有 GUI 实例持有单实例锁", "instance.log")
        from PySide6.QtWidgets import QApplication, QMessageBox
        qapp = QApplication.instance() or QApplication(sys.argv)
        _install_hint = ("请只使用 /Applications 中的正式版；若刚覆盖安装过，"
                         "请先从菜单栏退出旧实例再启动。"
                         if sys.platform == "darwin" else
                         "请在托盘图标右键菜单选择「退出」，结束已在运行的实例后再启动。\n"
                         "若刚覆盖安装过，旧进程可能仍在后台运行。")
        QMessageBox.warning(
            None, f"{APP_NAME} 已在运行",
            "检测到轻述 sayo 已经在运行，本次启动已取消。\n\n"
            + _install_hint
            + "\n（dist/ 目录下的构建副本请勿直接打开）")
        return 0
    return run_gui(settings)


if __name__ == "__main__":
    raise SystemExit(main())
