"""本地大模型适配层：整理入口、模型搜索与来源标记（不加载真实模型文件）。"""
from __future__ import annotations

from app.config import DEFAULT_SETTINGS
from app.core.llm import LocalTextEngine


def _engine(**overrides) -> LocalTextEngine:
    data = dict(DEFAULT_SETTINGS)
    data.update(overrides)
    return LocalTextEngine(data)


def test_organize_empty_text(monkeypatch):
    monkeypatch.setattr(LocalTextEngine, "_candidate_model_paths", lambda self: [])
    assert _engine().organize("   ") == ("", "empty")


def test_organize_unavailable_without_model(monkeypatch):
    monkeypatch.setattr(LocalTextEngine, "_candidate_model_paths", lambda self: [])
    out, source = _engine().organize("今天天气很好我们开个会")
    assert out == "" and source == "unavailable"


def test_organize_success_with_fake_llm(monkeypatch):
    eng = _engine()
    monkeypatch.setattr(eng, "available", lambda: True)
    monkeypatch.setattr(eng, "_generate", lambda s, u, max_tokens=512: "1. 甲项内容\n2. 乙项内容")
    out, source = eng.organize("第一点甲项内容第二点乙项内容")
    assert source == "local-llm"
    assert out.startswith("1.")


def test_organize_error_surfaces_message(monkeypatch):
    eng = _engine()
    monkeypatch.setattr(eng, "available", lambda: True)

    def boom(*_a, **_k):
        raise RuntimeError("模型炸了")

    monkeypatch.setattr(eng, "_generate", boom)
    out, source = eng.organize("随便说点什么内容")
    assert source == "error" and "模型炸了" in out


def test_model_path_prefers_existing_custom_file(tmp_path, monkeypatch):
    model = tmp_path / "m.gguf"
    model.write_bytes(b"x")
    monkeypatch.setattr(LocalTextEngine, "_candidate_model_paths", lambda self: [])
    eng = _engine(llm_model_path=str(model))
    assert eng.model_path == model
    assert eng.available() is True


def test_model_path_falls_back_to_search(monkeypatch, tmp_path):
    found = tmp_path / "auto.gguf"
    found.write_bytes(b"x")
    monkeypatch.setattr(LocalTextEngine, "_candidate_model_paths", lambda self: [found])
    eng = _engine(llm_model_path=str(tmp_path / "不存在.gguf"))
    assert eng.model_path == found
    assert eng.available() is True


def test_disabled_engine_is_never_available(monkeypatch, tmp_path):
    model = tmp_path / "m.gguf"
    model.write_bytes(b"x")
    monkeypatch.setattr(LocalTextEngine, "_candidate_model_paths", lambda self: [model])
    eng = _engine(llm_enabled=False)
    assert eng.available() is False
    assert eng.organize("内容超过最小长度了吗") == ("", "unavailable")


def test_config_defaults_exist():
    assert "organize_llm" in DEFAULT_SETTINGS
    assert "organize_min_chars" in DEFAULT_SETTINGS
    assert DEFAULT_SETTINGS["organize_llm"] is True


# ---------------------------------------------------------------- 长度守卫
def test_effective_chars_ignores_punct_and_space():
    from app.core.llm import LocalTextEngine as E
    assert E._effective_chars("1. 甲项内容\n2. 乙项内容。") == 10
    assert E._effective_chars("") == 0


def test_organize_retries_when_over_compressed(monkeypatch):
    eng = _engine()
    monkeypatch.setattr(eng, "available", lambda: True)
    src = "今天下午三点开项目评审会，第一点对齐进度，第二点确认预算，第三点安排下季度计划，请大家都参加。"
    calls = {"n": 0}

    def fake_generate(system, user, max_tokens=512):
        calls["n"] += 1
        if calls["n"] == 1:
            return "开评审会。"  # 模拟被总结过头：有效字数远小于原文 60%
        return "今天下午三点开项目评审会：1. 对齐进度；2. 确认预算；3. 安排下季度计划，请大家都参加。"

    monkeypatch.setattr(eng, "_generate", fake_generate)
    out, source = eng.organize(src)
    assert calls["n"] == 2  # 首次过度压缩后自动重试了一次
    assert source == "local-llm" and "预算" in out


def test_organize_falls_back_after_second_over_compression(monkeypatch):
    eng = _engine()
    monkeypatch.setattr(eng, "available", lambda: True)
    src = "今天下午三点开项目评审会，第一点对齐进度，第二点确认预算，第三点安排下季度计划，请大家都参加。"
    monkeypatch.setattr(eng, "_generate", lambda s, u, max_tokens=512: "开会。")
    out, source = eng.organize(src)
    assert out == "" and source == "fallback"  # 宁可不整理，也不丢内容


def test_organize_short_text_uses_stricter_floor(monkeypatch):
    eng = _engine()
    monkeypatch.setattr(eng, "available", lambda: True)
    src = "今天天气很好，我们下午3点开会。"  # 14 个有效字：短文本阈值 0.75
    calls = {"n": 0}

    def fake_generate(system, user, max_tokens=512):
        calls["n"] += 1
        if calls["n"] == 1:
            return "3 点开会。"  # 丢掉「下午」等词，有效字数 64% < 75%
        return "今天天气很好，我们下午三点开会。"

    monkeypatch.setattr(eng, "_generate", fake_generate)
    out, source = eng.organize(src)
    assert calls["n"] == 2 and source == "local-llm" and "下午" in out


def test_organize_floors_by_length():
    assert LocalTextEngine._floors("短句测试内容十二个字左右") == (0.75, 0.65)
    long = "这是一段超过三十个有效字的较长口述内容，" * 3
    assert LocalTextEngine._floors(long) == (0.60, 0.50)


def test_organize_injects_hotwords(monkeypatch):
    eng = _engine()
    monkeypatch.setattr(eng, "available", lambda: True)
    seen = {}

    def fake_generate(system, user, max_tokens=512):
        seen["user"] = user
        return "张伟负责 sayo 项目。"

    monkeypatch.setattr(eng, "_generate", fake_generate)
    eng.organize("张卫负责快type项目。", hotwords=["张伟", "sayo"])
    assert "专有名词表" in seen["user"] and "sayo" in seen["user"]


def test_organize_prompt_forbids_summarizing():
    system = LocalTextEngine._ORGANIZE_SYSTEM
    assert "整理" in system and "总结" in system  # 明确区分两种任务
    assert "禁止省略" in system and "信息量必须与原稿完全一致" in system


# ---------------------------------------------------------------- 一键总结
def test_summarize_points_with_fake_llm(monkeypatch):
    eng = _engine()
    monkeypatch.setattr(eng, "available", lambda: True)
    monkeypatch.setattr(
        eng, "_generate", lambda s, u, max_tokens=512: "- 要点一\n- 要点二")
    out, source = eng.summarize("很长的原文……")
    assert source == "local-llm" and out.startswith("-")


def test_summarize_brief_mode_uses_brief_prompt(monkeypatch):
    eng = _engine()
    monkeypatch.setattr(eng, "available", lambda: True)
    seen = {}

    def fake_generate(system, user, max_tokens=512):
        seen["system"] = system
        return "短摘要。"

    monkeypatch.setattr(eng, "_generate", fake_generate)
    out, _ = eng.summarize("原文", style="brief")
    assert "100 字" in seen["system"] and out == "短摘要。"


def test_summarize_empty_and_unavailable(monkeypatch):
    monkeypatch.setattr(LocalTextEngine, "_candidate_model_paths", lambda self: [])
    eng = _engine()
    assert eng.summarize("   ") == ("", "empty")
    assert eng.summarize("有内容") == ("", "unavailable")
