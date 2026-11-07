"""进程级离线硬约束：开启后禁止任何 AF_INET/AF_INET6 出站连接。

正常代码路径本来就没有任何网络调用；这里是纵深防御，便于审计与断网验收：
  · 允许 AF_UNIX 本地套接字（Qt/系统 IPC 需要），禁止一切 IP 连接（含 127.0.0.1 以外的 TCP/UDP）
  · 禁止 urllib / http.client 等标准库出站请求
  · 无账号体系、无许可证服务器、无更新检查、无遥测、无崩溃上报
"""
from __future__ import annotations

import socket

_ORIGINALS: dict[str, object] = {}
_INSTALLED = False


class NetworkBlocked(RuntimeError):
    pass


def enforce_offline_lock(allow_local_unix: bool = True) -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    def blocked_connect(self, address, *args, **kwargs):
        family = getattr(self, "family", None)
        if allow_local_unix and family == socket.AF_UNIX:
            return _ORIGINALS["connect"](self, address, *args, **kwargs)
        raise NetworkBlocked(f"离线模式：已阻止网络连接到 {address!r}")

    def blocked_connect_ex(self, address, *args, **kwargs):
        family = getattr(self, "family", None)
        if allow_local_unix and family == socket.AF_UNIX:
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


def audit_source(root: str) -> list[str]:
    """静态审计：列出源码中出现的联网关键字（应为空）。仅用于构建期自检。"""
    import re
    from pathlib import Path
    pattern = re.compile(
        r"(requests\.(get|post)|urllib\.request|http\.client|httpx|aiohttp|"
        r"socket\.create_connection|openai|anthropic|api\.|telemetry|analytics)",
        re.I,
    )
    hits: list[str] = []
    for p in Path(root).rglob("*.py"):
        if p.name == "offline.py":  # 守卫本体含被审计的关键字，跳过
            continue
        if any(part in {"build", "dist", ".venv", "scripts", "tests"} for part in p.parts):
            continue
        for n, line in enumerate(p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            if pattern.search(line) and not line.lstrip().startswith("#") and "audit_source" not in line:
                hits.append(f"{p}:{n}: {line.strip()[:110]}")
    return hits
