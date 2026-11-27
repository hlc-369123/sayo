"""语音模型下载器（仅首次需要，之后永久离线）。

安装包不携带 240 MB 的 SenseVoice 模型。程序装好后第一次启动就会在后台
静默下载，失败自动重试 2 次；仍失败则下次启动继续，成功后写入状态文件，
以后不再下载、不再校验哈希（只做毫秒级的大小比对），不浪费磁盘与网络。

下载全程只访问模型镜像，不上传任何本机数据；每个文件都做大小 + SHA256
校验，支持断点续传。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

_HF_REPO = "csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
_HF_MIRRORS = ("https://hf-mirror.com", "https://huggingface.co")
_GH_MIRRORS = ("https://gh-proxy.com/https://github.com", "https://github.com")

_STATE_NAME = ".model-state.json"
# 同一时刻只允许一个下载任务（启动后台任务与设置页手动重试可能同时发生）
_LOCK = threading.Lock()


@dataclass(frozen=True)
class ModelFile:
    relative: str
    sha256: str
    size: int
    urls: tuple[str, ...]


def _files() -> tuple[ModelFile, ...]:
    hf = [f"{m}/{_HF_REPO}/resolve/main" for m in _HF_MIRRORS]
    gh = [f"{m}/k2-fsa/sherpa-onnx/releases/download/asr-models" for m in _GH_MIRRORS]
    return (
        ModelFile("sensevoice/model.int8.onnx",
                  "c71f0ce00bec95b07744e116345e33d8cbbe08cef896382cf907bf4b51a2cd51",
                  239233841, tuple(f"{b}/model.int8.onnx" for b in hf)),
        ModelFile("sensevoice/tokens.txt",
                  "f449eb28dc567533d7fa59be34e2abca8784f771850c78a47fb731a31429a1dc",
                  315894, tuple(f"{b}/tokens.txt" for b in hf)),
        ModelFile("silero_vad.onnx",
                  "9e2449e1087496d8d4caba907f23e0bd3f78d91fa552479bb9c23ac09cbb1fd6",
                  643854, tuple(f"{b}/silero_vad.onnx" for b in gh)),
    )


FILES = _files()
TOTAL_BYTES = sum(f.size for f in FILES)


# ---------------------------------------------------------------- 状态文件
def state_path(models_dir: Path) -> Path:
    return Path(models_dir) / _STATE_NAME


def read_state(models_dir: Path) -> dict:
    try:
        data = json.loads(state_path(models_dir).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_state(models_dir: Path, **fields) -> dict:
    """只更新传入的字段，保留其余（并发写不会互相清空）。"""
    path = state_path(models_dir)
    state = read_state(models_dir)
    state.update(fields)
    state["version"] = 1
    state["updated_at"] = time.time()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        pass
    return state


def _fingerprint(path: Path) -> str:
    try:
        st = path.stat()
        return f"{st.st_size}:{int(st.st_mtime)}"
    except OSError:
        return ""


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- 完整性检查
def light_missing(models_dir: Path) -> list[ModelFile]:
    """只比对文件大小：毫秒级，用于每次启动 / 打开设置页。

    正常情况（文件都在且大小正确）不会读 240 MB 内容，避免拖慢启动、磨损磁盘。
    """
    out: list[ModelFile] = []
    for item in FILES:
        p = Path(models_dir) / item.relative
        try:
            if p.stat().st_size != item.size:
                out.append(item)
        except OSError:
            out.append(item)
    return out


def missing_files(models_dir: Path, verify: bool = False) -> list[ModelFile]:
    """缺失文件列表。verify=True 时做完整 SHA256（较慢，仅在需要时调用）。"""
    models_dir = Path(models_dir)
    if not verify:
        return light_missing(models_dir)
    return [item for item in FILES if not _valid(models_dir / item.relative, item)]


def _valid(path: Path, item: ModelFile) -> bool:
    try:
        return path.stat().st_size == item.size and _sha256(path) == item.sha256
    except OSError:
        return False


def verify_cached(models_dir: Path, force: bool = False) -> list[ModelFile]:
    """带指纹缓存的全量校验：文件没变过就只做 stat，不重复哈希。

    用于启动自检——首次装好/刚下载完会算一次哈希并记下 (size, mtime)，
    之后每次启动都是毫秒级；只有文件被改动过才重新计算。
    """
    models_dir = Path(models_dir)
    cached = (read_state(models_dir).get("verified") or {})
    verified: dict[str, str] = {}
    bad: list[ModelFile] = []
    for item in FILES:
        p = models_dir / item.relative
        fp = _fingerprint(p)
        if not force and fp and cached.get(item.relative) == fp:
            verified[item.relative] = fp
            continue
        if _valid(p, item):
            verified[item.relative] = fp
        else:
            bad.append(item)
    if verified != cached:
        _write_state(models_dir, verified=verified)
    return bad


def is_complete(models_dir: Path) -> bool:
    return not light_missing(models_dir)


# ---------------------------------------------------------------- 下载
def _download_one(item: ModelFile, dest: Path, progress, should_stop=None) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    last_error = ""
    for url in item.urls:
        if should_stop is not None and should_stop():
            raise OSError("已取消")
        try:
            existing = part.stat().st_size if part.exists() else 0
            if existing > item.size:
                part.unlink(missing_ok=True)
                existing = 0
            req = urllib.request.Request(url, headers={
                "User-Agent": "sayo-model-installer/1.0",
                "Range": f"bytes={existing}-",
            })
            with urllib.request.urlopen(req, timeout=30) as resp:
                code = getattr(resp, "status", 200)
                if existing and code != 206:
                    existing = 0
                    part.unlink(missing_ok=True)
                mode = "ab" if existing and code == 206 else "wb"
                done = existing
                with part.open(mode) as out:
                    while True:
                        if should_stop is not None and should_stop():
                            raise OSError("已取消")
                        chunk = resp.read(1024 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)
                        done += len(chunk)
                        if progress:
                            progress(item.relative, done, item.size)
                        time.sleep(0)  # 让出 GIL，界面线程保持响应
            if part.stat().st_size != item.size:
                raise OSError(f"下载大小不符（{part.stat().st_size} / {item.size}）")
            if _sha256(part) != item.sha256:
                part.unlink(missing_ok=True)
                raise OSError("SHA256 校验失败")
            os.replace(part, dest)
            return
        except Exception as exc:  # 换镜像重试
            last_error = f"{type(exc).__name__}: {exc}"
            continue
    raise OSError(f"{item.relative} 下载失败：{last_error}")


def download_all(models_dir: Path, progress=None, attempts: int = 1,
                 backoff: tuple[float, ...] = (6.0, 15.0), should_stop=None) -> str:
    """下载全部缺失文件。

    attempts：总尝试次数（1 表示不重试；启动后台任务传 3 = 首次 + 重试 2 次）。
    progress(relative, done, total) 在下载线程回调。
    """
    models_dir = Path(models_dir)
    if not light_missing(models_dir):
        _write_state(models_dir, complete=True, failed_at=0, fail_count=0)
        return "语音模型已完整，无需下载"

    last_error = ""
    for i in range(max(1, attempts)):
        if i:
            wait = backoff[min(i - 1, len(backoff) - 1)] if backoff else 6.0
            end = time.time() + wait
            while time.time() < end:
                if should_stop is not None and should_stop():
                    raise OSError("已取消")
                time.sleep(0.5)
        try:
            with _LOCK:
                items = light_missing(models_dir)
                if not items:
                    _write_state(models_dir, complete=True, failed_at=0, fail_count=0)
                    return "语音模型已完整，无需下载"
                # 只在需要下载时临时放行网络，结束立即恢复离线锁。
                from .offline import network_allowed
                with network_allowed():
                    for item in items:
                        _download_one(item, models_dir / item.relative, progress,
                                      should_stop=should_stop)
            # 每个文件在 _download_one 里已做过 SHA256；这里只复核大小并记录
            # 指纹，避免刚下完 240 MB 又整份重读一遍（省一次磁盘 IO）。
            still = light_missing(models_dir)
            if still:
                raise OSError("下载完成后仍有文件不完整：" + ", ".join(b.relative for b in still))
            verified = {item.relative: _fingerprint(models_dir / item.relative) for item in FILES}
            _write_state(models_dir, complete=True, verified=verified,
                         failed_at=0, fail_count=0)
            return "语音模型安装完成"
        except Exception as exc:
            if isinstance(exc, OSError) and str(exc) == "已取消":
                raise
            last_error = f"{type(exc).__name__}: {exc}"
            _write_state(models_dir, complete=False,
                         failed_at=time.time(),
                         fail_count=int(read_state(models_dir).get("fail_count") or 0) + 1)
    raise OSError(last_error or "下载失败")


def can_reach_any_mirror(timeout: float = 3.0) -> bool:
    """快速探测镜像可达性（几秒内），避免离线时反复等待 30 秒超时。"""
    import socket
    hosts = ("hf-mirror.com", "huggingface.co", "gh-proxy.com")
    from .offline import network_allowed
    for host in hosts:
        try:
            with network_allowed():
                socket.create_connection((host, 443), timeout=timeout).close()
            return True
        except Exception:
            continue
    return False
