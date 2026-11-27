"""进程级离线硬约束：开启后禁止任何 AF_INET/AF_INET6 出站连接。

正常代码路径本来就没有任何网络调用；这里是纵深防御，便于审计与断网验收：
  · 允许 AF_UNIX 本地套接字（Qt/系统 IPC 需要），禁止一切 IP 连接（含 127.0.0.1 以外的 TCP/UDP）
  · 禁止 urllib / http.client 等标准库出站请求
  · 无账号体系、无许可证服务器、无更新检查、无遥测、无崩溃上报
"""
from __future__ import annotations

import socket
import threading

_ORIGINALS: dict[str, object] = {}
_INSTALLED = False


class NetworkBlocked(RuntimeError):
    pass


# Windows 没有 socket.AF_UNIX（老系统 / 精简运行库都可能缺），取不到就退化为 None，
# 避免离线守卫在 Windows 上直接抛 AttributeError 把主流程带崩。
_AF_UNIX = getattr(socket, "AF_UNIX", None)


def enforce_offline_lock(allow_local_unix: bool = True) -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    def _is_local_unix(sock) -> bool:
        return (allow_local_unix and _AF_UNIX is not None
                and getattr(sock, "family", None) == _AF_UNIX)

    def blocked_connect(self, address, *args, **kwargs):
        if _is_local_unix(self):
            return _ORIGINALS["connect"](self, address, *args, **kwargs)
        raise NetworkBlocked(f"离线模式：已阻止网络连接到 {address!r}")

    def blocked_connect_ex(self, address, *args, **kwargs):
        if _is_local_unix(self):
            return _ORIGINALS["connect_ex"](self, address, *args, **kwargs)
        return 111  # ECONNREFUSED

    def blocked_create_connection(address, *args, **kwargs):
        raise NetworkBlocked(f"离线模式：已阻止网络连接到 {address!r}")

    _ORIGINALS.update({
        "connect": socket.socket.connect,
        "connect_ex": socket.socket.connect_ex,
        "create_connection": socket.create_connection,
    })
    socket.socket.connect = blocked_connect          # type: ignore[assignment]
    socket.socket.connect_ex = blocked_connect_ex    # type: ignore[assignment]
    socket.create_connection = blocked_create_connection  # type: ignore[assignment]

    try:
        import urllib.request
        _ORIGINALS["urlopen"] = urllib.request.urlopen
        urllib.request.urlopen = blocked_create_connection  # type: ignore[assignment]
    except Exception:
        pass
    _INSTALLED = True


def offline_lock_active() -> bool:
    return _INSTALLED


_WINDOW_DEPTH = 0
_WINDOW_LOCK = threading.Lock()
_RELOCK_ON_EXIT = False     # 进入第一个放行窗口之前，离线锁是否本来是开启的


class _NetworkWindow:
    """仅在准备语音模型时临时放行网络，退出后立即恢复离线锁。

    支持嵌套，也支持多个线程同时各开一个窗口（后进先出不一定成立）：
    重锁判断只用「打开第一个窗口时」的记录，且 depth 归零才真正重锁。
    这样无论哪个窗口最后退出，都不会把网络永久留在放行状态。
    """

    def __enter__(self):
        global _WINDOW_DEPTH, _RELOCK_ON_EXIT
        with _WINDOW_LOCK:
            if _WINDOW_DEPTH == 0:
                _RELOCK_ON_EXIT = _INSTALLED
                if _INSTALLED:
                    _restore_network()
            _WINDOW_DEPTH += 1
        return self

    def __exit__(self, exc_type, exc, tb):
        global _WINDOW_DEPTH
        with _WINDOW_LOCK:
            _WINDOW_DEPTH = max(0, _WINDOW_DEPTH - 1)
            if _WINDOW_DEPTH == 0 and _RELOCK_ON_EXIT:
                enforce_offline_lock()
        return False


def network_allowed() -> _NetworkWindow:
    """with network_allowed(): 期间允许出站；离开后自动恢复离线锁。"""
    return _NetworkWindow()


def _restore_network() -> None:
    global _INSTALLED
    if not _INSTALLED:
        return
    if "connect" in _ORIGINALS:
        socket.socket.connect = _ORIGINALS["connect"]  # type: ignore[assignment]
    if "connect_ex" in _ORIGINALS:
        socket.socket.connect_ex = _ORIGINALS["connect_ex"]  # type: ignore[assignment]
    if "create_connection" in _ORIGINALS:
        socket.create_connection = _ORIGINALS["create_connection"]  # type: ignore[assignment]
    if "urlopen" in _ORIGINALS:
        import urllib.request
        urllib.request.urlopen = _ORIGINALS["urlopen"]  # type: ignore[assignment]
    _INSTALLED = False


def audit_source(root: str) -> list[str]:
    """静态审计：列出源码中出现的联网关键字（应为空）。仅用于构建期自检。"""
    import re
    from pathlib import Path
    # 只列通用联网特征与库名，不引用任何第三方产品 / 品牌名称
    pattern = re.compile(
        r"(requests\.(get|post)|urllib\.request|http\.client|httpx|aiohttp|"
        r"socket\.create_connection|https?://|telemetry|analytics)",
        re.I,
    )
    hits: list[str] = []
    for p in Path(root).rglob("*.py"):
        if p.name in {"offline.py", "model_download.py"}:
            # 守卫本体 / 用户主动触发的模型下载器含被审计关键字，跳过
            continue
        if any(part in {"build", "dist", ".venv", "scripts", "tests"} for part in p.parts):
            continue
        for n, line in enumerate(p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            if pattern.search(line) and not line.lstrip().startswith("#") and "audit_source" not in line:
                hits.append(f"{p}:{n}: {line.strip()[:110]}")
    return hits
