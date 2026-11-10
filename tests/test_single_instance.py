"""GUI 单实例锁：防止 /Applications 正式版与 dist 构建副本同时运行。

两个实例会同时抢麦克风、注册同一组全局热键、互相静音/恢复系统输出，
录到的音频只剩零星尖峰 → 识别为空、文字不输出、历史无记录。
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app import main as main_mod

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="fcntl.flock 仅 POSIX 可用")

_PROJECT = str(Path(__file__).resolve().parents[1])

_CHILD = """
import sys
sys.path.insert(0, {project!r})
from app import main as m
print("ACQUIRED" if m._acquire_gui_instance_lock() else "BLOCKED")
"""


@pytest.fixture(autouse=True)
def _isolated_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("SAYO_DATA", str(tmp_path))
    yield
    fd = main_mod._INSTANCE_LOCK_FD
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass
        main_mod._INSTANCE_LOCK_FD = None


def _try_in_child(data_dir: Path) -> str:
    env = {**os.environ, "SAYO_DATA": str(data_dir)}
    out = subprocess.run(
        [sys.executable, "-c", _CHILD.format(project=_PROJECT)],
        env=env, capture_output=True, text=True, timeout=90)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip().splitlines()[-1]


def test_first_instance_acquires_and_creates_lock(tmp_path):
    assert main_mod._acquire_gui_instance_lock() is True
    assert (tmp_path / "logs" / "sayo.instance.lock").exists()


def test_second_acquire_in_same_process_is_rejected(tmp_path):
    assert main_mod._acquire_gui_instance_lock() is True
    # 独立 fd 再次尝试：BSD flock 按 open file description 互斥
    assert main_mod._acquire_gui_instance_lock() is False


def test_second_process_rejected_while_first_holds_lock(tmp_path):
    assert main_mod._acquire_gui_instance_lock() is True
    assert _try_in_child(tmp_path) == "BLOCKED"


def test_child_acquires_when_no_instance_running(tmp_path):
    assert _try_in_child(tmp_path) == "ACQUIRED"


def test_lock_released_when_fd_closed(tmp_path):
    """进程退出（fd 关闭）后锁自动释放，不留过期锁文件。"""
    assert main_mod._acquire_gui_instance_lock() is True
    os.close(main_mod._INSTANCE_LOCK_FD)
    main_mod._INSTANCE_LOCK_FD = None
    assert _try_in_child(tmp_path) == "ACQUIRED"


def test_cli_and_self_check_paths_skip_lock(monkeypatch):
    """--self-check / --file / --cli 不进入 GUI 分支，不应获取锁。"""
    called = []
    monkeypatch.setattr(main_mod, "_acquire_gui_instance_lock",
                        lambda: called.append(1) or True)
    monkeypatch.setattr(main_mod, "self_check", lambda s: 0)
    monkeypatch.setattr(main_mod, "run_cli", lambda s, f: 0)
    assert main_mod.main(["--self-check"]) == 0
    assert main_mod.main(["--file", "x.wav"]) == 0
    assert main_mod.main(["--cli"]) == 0
    assert not called
