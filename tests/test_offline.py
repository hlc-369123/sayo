"""离线约束测试：运行时不允许任何 IP 连接，源码不得出现联网调用。"""
import socket
import sys
from pathlib import Path

import pytest

from app.core.offline import (
    NetworkBlocked, audit_source, enforce_offline_lock, network_allowed,
    offline_lock_active,
)

no_af_unix = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows 无 socket.AF_UNIX（属系统限制，非代码缺陷）")


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


@no_af_unix
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


def test_network_window_nested_relocks():
    """正常嵌套：内层退出不提前重锁，最外层退出必须重锁。"""
    enforce_offline_lock()
    with network_allowed():
        assert not offline_lock_active()
        with network_allowed():
            assert not offline_lock_active()
        assert not offline_lock_active()
    assert offline_lock_active()


def test_network_window_relocks_when_windows_overlap():
    """两个窗口重叠且外层先退出（多线程同时下载的情形）：最后退出时必须重锁。

    旧实现把「进来前是否上锁」记在各自实例上，内层记到的是 False，
    于是最后退出时谁都不重锁，网络被永久放行。
    """
    enforce_offline_lock()
    outer = network_allowed()
    inner = network_allowed()
    outer.__enter__()
    inner.__enter__()
    assert not offline_lock_active()
    outer.__exit__(None, None, None)
    assert not offline_lock_active()      # 内层还没退出，不能提前锁
    inner.__exit__(None, None, None)
    assert offline_lock_active()          # 全部退出后必须恢复离线锁
