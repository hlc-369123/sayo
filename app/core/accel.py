"""本机推理加速后端（GPU / Apple 神经引擎）探测与选择。

技术边界（务必如实理解）：GPU 只改变算力，不会改变同一份权重的识别结果。
它真正的价值是让下面这些「重方案」保持实时，从而间接提升准确率与灵敏度：
  · fp32 全精度模型（比 int8 量化保留更多声学细节，长句 / 噪声 / 轻声更稳）
  · 更激进的 VAD 切分与更低的触发阈值（轻声、远场不丢字）
  · 低电平片段的二次复核解码（额外算力换召回率）
全部在本机完成：只做本地硬件探测，不访问任何远程地址。
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

# sherpa-onnx 1.13.x 的 onnxruntime execution provider 只接受 cpu / cuda / coreml
CPU = "cpu"
CUDA = "cuda"
COREML = "coreml"


@dataclass(frozen=True)
class Backend:
    key: str                 # 传给 sherpa-onnx 的 provider 值
    label: str               # UI 展示名
    detail: str              # 本机硬件说明
    is_gpu: bool = False


BACKEND_CPU = Backend(CPU, "CPU 多线程", "纯 CPU 推理，冷启动最快，短句延迟最低")
BACKEND_COREML = Backend(COREML, "Apple GPU / 神经引擎 (Core ML)",
                         "本机 Core ML 执行 provider，可用 GPU 与 ANE 加速，不联网", True)
BACKEND_CUDA = Backend(CUDA, "NVIDIA CUDA GPU",
                       "本机 CUDA 执行 provider（需带 CUDA EP 的推理库与 NVIDIA 驱动）", True)


@lru_cache(maxsize=1)
def probe_hardware() -> tuple[bool, str, Backend | None]:
    """探测本机是否存在可用的 GPU 推理通道。结果进程内缓存。

    返回 (是否有 GPU, 硬件描述, 建议的 GPU Backend)。
    只调用本机系统命令 / 读取本机文件，绝不产生网络流量。
    """
    if sys.platform == "darwin":
        arch = platform.machine() or ""
        if arch == "arm64":
            detail = f"Apple Silicon ({arch}) · Core ML 可调用 GPU / ANE"
        else:
            detail = "macOS 独立/集成显卡 · Core ML 可调用 Metal"
        return True, detail, BACKEND_COREML

    if os.name == "nt":
        if shutil.which("nvidia-smi"):
            try:
                out = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                                     capture_output=True, text=True, timeout=3, check=False)
                name = (out.stdout or "").strip().splitlines()[0:1]
                detail = "NVIDIA " + (name[0].strip() if name else "GPU")
            except (OSError, subprocess.SubprocessError):
                detail = "NVIDIA GPU（驱动探测超时）"
            return True, detail, BACKEND_CUDA
        return False, "未检测到 NVIDIA GPU；官方 PyPI 推理库仅提供 CPU 执行 provider", None

    # Linux / 其它
    if Path("/dev/nvidiactl").exists() or Path("/dev/nvidia0").exists():
        return True, "检测到 NVIDIA 设备节点", BACKEND_CUDA
    return False, "未检测到 NVIDIA 设备节点", None


def choose_backend(mode: str, precision: str, sensitivity: str,
                   allow_fallback: bool = True) -> tuple[Backend, str]:
    """按设置选择推理后端。

    mode: auto / cpu / gpu
    auto 的策略来自本机实测：int8 短句在 CPU 上更快（GPU 有小批量搬运与编译开销），
    而 fp32 全精度和高灵敏复核需要额外算力，才交给 GPU。
    """
    has_gpu, detail, gpu_backend = probe_hardware()
    mode = (mode or "auto").lower()

    if mode == "cpu":
        return BACKEND_CPU, "已强制使用 CPU"
    if mode == "gpu":
        if has_gpu and gpu_backend:
            return gpu_backend, detail
        return BACKEND_CPU, "请求 GPU 但本机不可用，已回退 CPU：" + detail

    # auto：Apple Core ML 在本机实测对 SenseVoice（batch=1 小模型）没有速度优势，
    # GPU 搬运 / 模型编译开销抵消算力收益，因此自动档保留 CPU；
    # CUDA 平台在 fp32 全精度或高灵敏复核档启用 GPU（大模型 / 长语音收益明显）。
    if has_gpu and gpu_backend and gpu_backend.key == CUDA \
            and (precision == "fp32" or sensitivity in ("high", "max")):
        return gpu_backend, "自动：高精度/高灵敏档启用 CUDA GPU · " + detail
    if has_gpu and gpu_backend:
        return BACKEND_CPU, "自动：该小模型短句实测 CPU 更快，GPU 搬运/编译开销大于收益；可强制 GPU 对比" \
                            + " · " + detail
    return BACKEND_CPU, "自动：本机无可用 GPU 推理通道，使用 CPU · " + detail


def status_lines() -> list[str]:
    has_gpu, detail, backend = probe_hardware()
    if has_gpu and backend:
        return [f"GPU 推理：可用 · {backend.label}", f"硬件：{detail}"]
    return ["GPU 推理：不可用", f"原因：{detail}"]


def capability_summary(settings: dict) -> str:
    """给设置界面 / 自检用的一行状态。"""
    mode = settings.get("accelerator", "auto")
    precision = settings.get("precision", "auto")
    sensitivity = settings.get("sensitivity", "standard")
    backend, note = choose_backend(mode, precision, sensitivity)
    return f"{backend.label} · {note}"
