"""配置默认值与一次性迁移测试。

评审指出的关键风险：升级用户的 settings.json 里早已存了 organize_llm=true，
仅把 DEFAULT_SETTINGS 改成 False 对他们完全不生效，必须由带标记的一次性迁移翻转，
且只翻一次——之后用户在设置里的显式选择必须被尊重。
"""
import json
from pathlib import Path

from app.config import DEFAULT_SETTINGS, Settings


def _write(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def test_default_organize_llm_is_off():
    assert DEFAULT_SETTINGS["organize_llm"] is False
    assert DEFAULT_SETTINGS["organize_default_migrated_v2"] is False


def test_upgrade_user_is_migrated_exactly_once(tmp_path):
    """旧配置显式写着 true：首次加载应翻成 false 并写入标记，重启不再重复翻转。"""
    path = tmp_path / "settings.json"
    _write(path, {"organize_llm": True, "llm_enabled": True, "organize_min_chars": 10})

    s = Settings(path)
    assert s.get("organize_llm") is False
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["organize_llm"] is False
    assert data["organize_default_migrated_v2"] is True

    assert Settings(path).get("organize_llm") is False


def test_user_choice_after_migration_is_respected(tmp_path):
    path = tmp_path / "settings.json"
    _write(path, {"organize_llm": True})
    s = Settings(path)
    assert s.get("organize_llm") is False

    s.set("organize_llm", True)  # 用户主动在设置里重新开启
    again = Settings(path)
    assert again.get("organize_llm") is True
    assert again.get("organize_default_migrated_v2") is True


def test_fresh_install_defaults_off(tmp_path):
    path = tmp_path / "settings.json"
    s = Settings(path)
    assert s.get("organize_llm") is False


def test_migration_keeps_unrelated_settings(tmp_path):
    path = tmp_path / "settings.json"
    _write(path, {"organize_llm": True, "inject_method": "type",
                  "hotkey_dictate": "Ctrl+Space"})
    s = Settings(path)
    assert s.get("organize_llm") is False
    assert s.get("inject_method") == "type"
    assert s.get("hotkey_dictate") == "Ctrl+Space"
