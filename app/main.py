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
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from PySide6.QtCore import QObject, QTimer, Signal

from .config import APP_NAME, APP_VERSION, Settings, logs_dir, models_dir, resource_root
from .core.asr import ASREngine
from .core.live import LiveTranscriber
from .core.offline import enforce_offline_lock, offline_lock_active
from .core.pipeline import AppContext, DictationPipeline
from .core.recorder import Recorder, RecorderError, read_wav
from .core.storage import Storage
from .platform import permissions
from .platform.context import current_context
from .platform.focus import choose_output, text_input_focused
from .platform.hotkey import HotkeyManager, event_tap_available
from .platform.inject import Clipboard, insert_text

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
        f"系统热键注册：{h.get('carbon_error') or '成功（免输入监控授权）'}",
        f"监听线程存活：{h['listener_alive']}",
        f"全局事件监听：{'可用' if h['tap_ok'] else '不可用'} · {h['tap_detail']}",
        f"监听错误：{h['error'] or '无'}",
        f"最近命中：{h['last_hit'] or '（本次启动尚未收到快捷键）'}",
        "权限：麦克风=%s 辅助功能=%s 输入监控=%s（新后端不需要最后一项）" % (
            perms.get("microphone"), perms.get("accessibility"), perms.get("input_monitoring")),
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


def build_live_engine(settings: Settings) -> ASREngine:
    """实时预览用 int8 轻量引擎，与主 fp32 引擎隔离，避免并发改模型。

    灵敏度必须与主引擎一致：旧实现写死 standard，轻声说话时能量门控会把
    整句判成静音，预览窗口就一直空白（用户看到「不显示识别的文字」）。
    """
    cfg = dict(settings.data)
    cfg.update({"precision": "int8", "accelerator": "cpu",
                "sensitivity": settings.get("sensitivity", "high"),
                "gpu_recheck": False, "vad_enabled": True, "num_threads": 2})
    return ASREngine(models_dir(), cfg)


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
    from .platform.hotkey import carbon_combo
    for label, key in (("听写快捷键", "hotkey_dictate"), ("编辑快捷键", "hotkey_ask")):
        combo = settings.get(key)
        mapped = carbon_combo(combo)
        print(f"{label}：{combo} · 系统热键映射：",
              "可注册（免授权）" if mapped else "不可用系统热键，将回退到键盘监听")
    tap_ok, tap_detail = event_tap_available()
    print("全局键盘监听（仅回退后端使用）：", "可用" if tap_ok else "不可用", "-", tap_detail)
    print("说明：macOS 上 space = 空格键，⌘ + Shift + 空格 三键同按；"
          "系统热键由 Carbon 注册，不需要输入监控权限")
    try:
        from pynput.keyboard import Listener  # noqa: F401
        print("键盘监听模块 pynput：随包可用")
    except Exception as exc:
        print("键盘监听模块 pynput：不可用 -", exc)
    for mod in ("ApplicationServices", "AVFoundation", "AppKit"):
        try:
            __import__(mod)
            print(f"本机系统模块 {mod}：随包可用")
        except Exception as exc:
            print(f"本机系统模块 {mod}：不可用 - {exc}")
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
        self.state = "idle"
        self.begin()

    def _handle_mic_error(self, msg: str) -> None:
        """麦克风打不开时区分根因：权限未授权就主动弹授权框 + 打开设置，别只报错。"""
        log_note(f"麦克风打开失败：{msg}")
        try:
            from .platform import permissions
            mic_ok = permissions._mac_microphone()  # True / False / None
        except Exception:
            mic_ok = None
        if mic_ok is False:  # 明确未授权：覆盖安装后签名变更最常见的后果
            try:
                permissions._prompt_microphone()
                permissions.open_settings("microphone")
            except Exception:
                pass
            self.session.sig_error.emit(
                "麦克风权限未授权：已为您弹出系统授权框并打开「隐私与安全性 → 麦克风」。"
                "请勾选「轻述 sayo」的开关（若列表里有多个同名条目，先把旧的删掉），"
                "然后回到输入框重新按 ⌘ + Shift + 空格 听写。")
            return
        # 其他原因：设备被占用 / 蓝牙断开 / 无输入设备
        self.session.sig_error.emit(
            f"麦克风打开失败：{msg}\n"
            "若刚更换过蓝牙耳机或系统默认输入设备，请到 系统设置 → 声音 → 输入 确认后重试。")

    def begin(self, mode: str | None = None):
        if self.state != "idle":
            return
        self._force_paste = False
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
            self.recorder.start()
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
                # 根因提示：辅助功能未授权时 macOS 只允许写剪贴板（焦点检测与 ⌘V 注入都依赖它）
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
        self.executor.shutdown(wait=False, cancel_futures=True)
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
    from .core.sound import set_enabled as set_sound_enabled, play_start, play_end
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
    settings_win = SettingsWindow(settings, ctrl.storage, ctrl.pipeline)
    history_win = HistoryWindow(ctrl.storage, ctrl.pipeline)

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
        """免麦克风试识别：用安装包内置的语音样本跑一遍完整离线流程。"""
        sample = resource_root() / "assets" / "sample_zh.wav"
        audio = read_wav(str(sample))
        r = ctrl.pipeline.process(audio, audio_seconds=len(audio) / 16000)
        return r.text or "（未识别到文字）"

    quickstart = QuickStartWindow(settings, probe=quick_probe)
    guide_win = PermissionGuideWindow(settings)
    guide_win.open_settings_requested.connect(lambda key: permissions.open_settings(key))

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
        if healthy and carbon:
            summary = (
                "系统级全局热键已生效，不需要「输入监控」授权，覆盖安装后也不会失效。\n\n"
                "⌘ + Shift + 空格（键盘最下方那个空格键）：开始 / 结束听写\n"
                "⌘ + Shift + E：语音编辑选中文字\n\n"
                "听写期间屏幕顶部会一直显示两样彼此独立的东西："
                "上面一行实时文字，下面几条随音量变化的曲线；切换应用或桌面也不会消失。")
        elif healthy:
            summary = ("快捷键可用（回退到键盘监听后端）。\n\n"
                       "⌘ + Shift + 空格 开始 / 结束听写，再按一次结束。")
        else:
            summary = (
                "系统热键未能注册，所以按了没有反应。\n\n"
                f"原因：{info.get('carbon_error') or info.get('tap_detail')}\n\n"
                "常见情况是组合键被别的应用占用；可以在「设置 → 常规」换成别的组合，\n"
                "或点「重新注册系统热键」重试。若回退到键盘监听，"
                "则还需要在「输入监控」里勾选本应用。")
        activate_for_dialog()  # 用户主动打开：让诊断窗口正常置于前台
        box = QMessageBox()
        box.setWindowTitle("快捷键诊断 / 本机授权")
        box.setIcon(QMessageBox.Information if healthy else QMessageBox.Warning)
        box.setText(summary)
        box.setDetailedText("\n".join(info["lines"]) +
                           (f"\n日志：{info['log_path']}" if info["log_path"] else ""))
        b_fix = box.addButton("重新注册系统热键", QMessageBox.ActionRole)
        b_set = box.addButton("输入监控设置（仅回退后端）", QMessageBox.ActionRole)
        b_mic = box.addButton("麦克风设置", QMessageBox.ActionRole)
        b_ax = box.addButton("辅助功能设置", QMessageBox.ActionRole)
        b_close = box.addButton("关闭", QMessageBox.RejectRole)
        box.setDefaultButton(b_fix if not healthy else b_close)
        box.exec()
        clicked = box.clickedButton()
        if clicked is b_mic:
            permissions.open_settings("microphone")
        elif clicked is b_ax:
            permissions.open_settings("accessibility")
        elif clicked is b_fix:
            after = rebuild_hotkeys("诊断窗口点击重新启用")
            tray.showMessage(APP_NAME,
                             "系统热键已重新注册，现在可以按 ⌘ + Shift + 空格" if after["healthy"]
                             else "系统热键仍未注册，请更换组合键或在输入监控里授权回退监听",
                             QSystemTrayIcon.Information if after["healthy"] else QSystemTrayIcon.Warning,
                             6000)
        elif clicked is b_set:
            permissions.open_settings("input_monitoring")

    prev_sig = [asr_signature(settings.data)]

    def on_settings_saved(_data):
        ctrl.pipeline.refresh()
        ctrl.asr.settings = settings.data
        ctrl.live.interval = max(0.05, min(0.5,
            float(settings.get("live_interval_ms", 550)) / 1000.0))
        apply_app_theme()
        overlay.apply_theme(settings.get("theme", DEFAULT_THEME))
        overlay.apply_style(settings.get("overlay_style", "classic"))
        set_sound_enabled(bool(settings.get("interaction_sound", True)))
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
            ctrl.shutdown()
            app.quit()

    _prev_state = ["idle"]

    def on_state(state, text):
        overlay.set_state(state, text)
        # 语音输入时静音其他活动音频（可在设置关闭）：进入录音静音，结束/取消恢复
        if bool(settings.get("mute_others_during_input", False)):
            if state == "recording" and _prev_state[0] != "recording":
                # 蓝牙耳机的输入/输出是同一个设备：给它设 output mute 会把
                # 输入流一起掐断（实测从断续爆音变成全零）。这种情况跳过静音。
                if not Recorder.shares_output_device():
                    mute_system_output(True)
            elif _prev_state[0] == "recording" and state != "recording":
                mute_system_output(False)
        # 交互声音：进入 recording 播放开始音，离开 recording 播放结束音
        if state == "recording" and _prev_state[0] != "recording":
            play_start()
        elif _prev_state[0] == "recording" and state != "recording":
            play_end()
        _prev_state[0] = state

    def on_result(result):
        # 激活应用是异步的：先恢复用户原来的应用，稍等再检测焦点并粘贴。
        # 少了这一步，⌘V 会发给被切到前台的本应用，输入框里什么都不会出现。
        # 识别结果直接写入输入框，屏幕上不留任何文字。
        t_emit = time.perf_counter()
        ctrl._restore_front()

        def _paste_and_measure():
            t_paste = time.perf_counter()
            try:
                ctrl.on_result(result)
            finally:
                # T1/T2/T3 分口径埋点（评审 §7）：三种耗时分开记，避免把
                # 「模型处理」与「等待目标应用焦点 / 粘贴」混成一个数字。
                # 注：这里测到的是「粘贴指令返回」，并非目标应用真正渲染出文字，
                # 后者无跨应用可靠回执，需人工实测补充。
                t_done = time.perf_counter()
                log_note(
                    f"T1_asr={result.duration_ms}ms "
                    f"T2_emit_to_paste={(t_paste - t_emit) * 1000:.0f}ms "
                    f"T3_paste_call={(t_done - t_paste) * 1000:.0f}ms "
                    f"total_from_emit={(t_done - t_emit) * 1000:.0f}ms "
                    f"chars={len(result.text)} "
                    f"llm_in_path={'本机大模型整理' in result.applied}",
                    "latency.log")

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
    ok, msg = ctrl.asr.check_models()
    if not ok:
        tray.showMessage(
            APP_NAME,
            "模型未安装：" + msg + "\n首次使用请运行 scripts/install_models.command 下载（仅这一步需联网）",
            QSystemTrayIcon.Critical, 10000)
    else:
        def _warmup_all():
            try:
                ctrl.asr.warmup()
            except Exception as exc:
                log_note(f"主引擎预热失败：{type(exc).__name__}: {exc}")
            # 实时预览也必须预热：旧实现第一次说话时才加载 int8 模型（约 0.5 s），
            # 叠加 0.35 s 最小语音长度，首字出现太晚，看起来像「不显示识别的文字」。
            try:
                ctrl.live.asr.warmup()
            except Exception as exc:
                log_note(f"实时预览预热失败：{type(exc).__name__}: {exc}")
            return None
        ctrl.executor.submit(_warmup_all)
    hotkey_warning = ctrl.setup_hotkeys()
    hotkey_diagnostics(ctrl, "应用启动")
    if hotkey_warning:
        tray.showMessage(APP_NAME, hotkey_warning, QSystemTrayIcon.Warning, 6000)

    def _post_start_check():
        # 只做本机状态复查并写日志。绝不自动打开系统设置、绝不弹模态窗口：
        # 那会把用户正在使用的输入框光标抢走（旧版「开始使用后光标就没了」的根因）。
        hotkey_diagnostics(ctrl, "启动后自检（无弹窗）")
        perms = {st.key: st.granted for st in permissions.check()}
        if perms.get("accessibility") is not True:
            tray.showMessage(
                APP_NAME,
                "辅助功能未授权：文字只放入剪贴板（⌘V 粘贴）。"
                "已弹出「授权引导」窗口，按步骤勾选即可自动输入",
                QSystemTrayIcon.Information, 7000)
            guide_win.maybe_auto_show()

    # 系统热键由 Carbon 注册，不需要输入监控。启动时只申请麦克风与辅助功能；
    # 不再弹「输入监控」系统框、不打开系统设置、不在授权后重建监听。
    QTimer.singleShot(1500, lambda: permissions.request_all_async(
        force=False, include_input_monitoring=False))
    QTimer.singleShot(8000, _post_start_check)
    if not settings.get("first_run_done", False):
        QTimer.singleShot(900, lambda: (activate_for_dialog(),
                                        quickstart.show(), quickstart.raise_()))

    tray.showMessage(APP_NAME, "已就绪 · ⌘ + Shift + 空格 开始听写 · 完全离线",
                     QSystemTrayIcon.Information, 4000)
    return app.exec()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=f"{APP_NAME} · 全离线 AI 语音键盘")
    parser.add_argument("--cli", action="store_true", help="命令行模式")
    parser.add_argument("--file", help="离线转写本机 16k wav 文件")
    parser.add_argument("--self-check", action="store_true", help="环境与离线约束自检")
    parser.add_argument("--benchmark", action="store_true", help="CPU/GPU × fp32/int8 本机实测")
    parser.add_argument("--bench-file", help="基准测试使用的本机 wav")
    args = parser.parse_args(argv)
    settings = Settings()
    if args.self_check:
        return self_check(settings)
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
        QMessageBox.warning(
            None, f"{APP_NAME} 已在运行",
            "检测到轻述 sayo 已经在运行，本次启动已取消。\n\n"
            "请只使用 /Applications 中的正式版；若刚覆盖安装过，"
            "请先从菜单栏退出旧实例再启动。\n"
            "（dist/ 目录下的构建副本请勿直接打开）")
        return 0
    return run_gui(settings)


if __name__ == "__main__":
    raise SystemExit(main())
