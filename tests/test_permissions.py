"""权限自检与异步申请流程（不触发真实系统弹窗）。"""
from __future__ import annotations

import threading

from app.platform import permissions


def test_check_returns_expected_keys():
    states = permissions.check()
    keys = {s.key for s in states}
    assert "microphone" in keys
    assert "accessibility" in keys
    assert all(isinstance(s.label, str) and s.label for s in states)
    assert all(isinstance(s.hint, str) for s in states)


def test_non_mac_request_does_not_prompt(monkeypatch):
    monkeypatch.setattr(permissions.sys, "platform", "linux")
    out = permissions.request_all()
    assert "microphone" in out
    assert "桌面系统" in out["microphone"]


def test_async_runs_in_background_and_calls_back(monkeypatch):
    seen = []
    done = threading.Event()

    def fake_request(force=False, include_input_monitoring=True):
        return {"microphone": "fake", "accessibility": "fake", "input_monitoring": "fake"}

    monkeypatch.setattr(permissions, "request_all", fake_request)

    def cb(result):
        seen.append(result)
        done.set()

    permissions.request_all_async(force=True, callback=cb)
    assert done.wait(2.0)
    assert seen and seen[0]["microphone"] == "fake"


def test_skip_input_monitoring_prompt(monkeypatch):
    """常规启动只申请麦克风与辅助功能，避免系统弹窗抢走输入框焦点。"""
    monkeypatch.setattr(permissions.sys, "platform", "darwin")
    monkeypatch.setattr(permissions, "_mac_microphone", lambda: True)
    monkeypatch.setattr(permissions, "_mac_accessibility", lambda: True)
    called = []
    monkeypatch.setattr(permissions, "_prompt_input_monitoring",
                        lambda: called.append("prompt") or "不该调用")
    out = permissions.request_all(include_input_monitoring=False)
    assert not called
    assert "暂不申请" in out["input_monitoring"]
