"""主题系统：8 套内置配色、非法 key 回退默认、浮层与界面样式随主题切换。"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtWidgets import QApplication  # noqa: E402

import pytest as _pytest  # noqa: E402

from app.ui.themes import (  # noqa: E402
    DEFAULT_THEME, THEMES, Theme, get_theme, theme_items, ui_stylesheet,
)


@_pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_builtin_theme_catalog():
    assert len(THEMES) >= 8
    keys = [k for k, _ in theme_items()]
    assert len(keys) == len(set(keys))
    assert DEFAULT_THEME in keys
    for key, name in theme_items():
        t = THEMES[key]
        assert name and t.name == name
        assert len(t.waves) == 4 and len(t.glow) == 4
        for c in (*t.waves, *t.glow, t.text_live, t.text_final, t.text_stroke):
            assert c.isValid()
            assert c.alpha() > 0


def test_get_theme_falls_back_to_default():
    assert get_theme(None) is THEMES[DEFAULT_THEME]
    assert get_theme("不存在的主题") is THEMES[DEFAULT_THEME]
    assert get_theme(" MIST ") is THEMES["mist"]  # 大小写与空格容错


def test_light_and_dark_text_pairings_differ():
    """浅色主题用深色文字 + 白描边，深色主题相反：保证任意桌面都可读。"""
    mist = THEMES["mist"]
    aurora = THEMES["aurora"]
    assert mist.text_live.lightness() < mist.text_stroke.lightness()
    assert aurora.text_live.lightness() > aurora.text_stroke.lightness()


def test_stylesheet_uses_theme_colors():
    th: Theme = THEMES["ink_gold"]
    qss = ui_stylesheet(th)
    assert th.ui_accent in qss and th.ui_bg in qss
    assert "{" in qss and "}" in qss


def test_overlay_applies_theme(app):
    from app.ui.overlay import RecordingOverlay

    o = RecordingOverlay("ocean")
    assert o.theme is THEMES["ocean"]
    assert o.text_window.theme is THEMES["ocean"]
    assert o.wave_window.theme is THEMES["ocean"]
    o.apply_theme("sakura")
    assert o.text_window.theme is THEMES["sakura"]
    assert o.wave_window.theme.waves[0] == THEMES["sakura"].waves[0]
    o.set_state("recording")
    o.set_live_text("主题切换不影响文字")
    o.set_state("idle")
