"""离线约束测试：运行时不允许任何 IP 连接，源码不得出现联网调用。"""
import socket
from pathlib import Path

import pytest

from app.core.offline import (
    NetworkBlocked, audit_source, enforce_offline_lock, offline_lock_active,
)


def test_offline_lock_blocks_ip_connections():
    enforce_offline_lock()
    assert offline_lock_active()
    with pytest.raises(NetworkBlocked):
        socket.create_connection(("1.1.1.1", 443), timeout=0.1)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.1)
    with pytest.raises(NetworkBlocked):
        s.connect(("8.8.8.8", 53))
    s.close()


def test_local_unix_socket_still_allowed():
    enforce_offline_lock()
    import os
    import socket as s
    path = "/tmp/qt-%d.sock" % os.getpid()
    server = s.socket(s.AF_UNIX, s.SOCK_STREAM)
    client = s.socket(s.AF_UNIX, s.SOCK_STREAM)
    try:
        server.bind(path)  # AF_UNIX 路径必须短，避免 macOS 的长度限制
        server.listen(1)
        client.connect(path)  # 本地 IPC 不应被拦截
        conn, _ = server.accept()
        conn.close()
    finally:
        client.close()
        server.close()
        if os.path.exists(path):
            os.unlink(path)


def test_no_network_calls_in_source():
    root = Path(__file__).resolve().parents[1] / "app"
    assert audit_source(str(root)) == []
