"""流水线编排测试：用桩 ASR 验证润色、语气、历史落盘与离线模式边界。"""
import numpy as np
import pytest

from app.config import DEFAULT_SETTINGS
from app.core.asr import ASRResult
from app.core.pipeline import AppContext, DictationPipeline
from app.core.storage import Storage


class FakeASR:
    model_name = "fake"

    def __init__(self, text="嗯，我我我想说，第一，确认需求。第二，写方案。"):
        self.text = text

    def transcribe(self, audio, hotwords=None, use_vad=True):
        return ASRResult(text=self.text, language="zh", segments=1,
                         elapsed_ms=1, model="fake")


@pytest.fixture(autouse=True)
def _no_llm_model(monkeypatch):
    """屏蔽真机 GGUF 模型搜索：流水线单测不应随开发机是否放置模型而变。"""
    monkeypatch.setattr("app.core.llm.LocalTextEngine._candidate_model_paths",
                        lambda self: [])


def make_pipeline(tmp_path, settings=None):
    s = dict(DEFAULT_SETTINGS)
    s.update(settings or {})
    storage = Storage(tmp_path / "t.db")
    p = DictationPipeline(s, storage, FakeASR(), None)
    return s, storage, p


def test_polish_and_local_history(tmp_path):
    s, storage, p = make_pipeline(tmp_path, {"save_audio": False})
    r = p.process(np.zeros(1600, dtype=np.float32),
                  context=AppContext(app_name="邮件", bundle_id="com.apple.mail"),
                  audio_seconds=0.1)
    assert "嗯" not in r.text
    assert "我我我" not in r.text
    assert "1." in r.text
    assert r.applied
    assert r.tone == "formal"  # auto 模式下按应用映射为正式语气
    rows = storage.search_history()
    assert len(rows) == 1 and rows[0]["polished_text"] == r.text
    storage.close()


def test_code_tone_keeps_text(tmp_path):
    s, storage, p = make_pipeline(tmp_path)
    r = p.process(np.zeros(100, dtype=np.float32),
                  context=AppContext(bundle_id="com.microsoft.VSCode"))
    assert r.tone == "code"
    storage.close()


def test_translate_without_local_llm_falls_back_to_raw(tmp_path):
    s, storage, p = make_pipeline(tmp_path)
    r = p.process(np.zeros(100, dtype=np.float32), mode="translate")
    assert r.text  # 输出原文而不是报错
    assert any("本地 GGUF" in w for w in r.warnings)
    storage.close()


def test_empty_audio_returns_warning(tmp_path):
    s, storage, p = make_pipeline(tmp_path, {})
    p.asr = FakeASR("")
    r = p.process(np.zeros(100, dtype=np.float32))
    assert r.text == "" and r.warnings
    storage.close()


# ---------------------------------------------------------------- 主路径不含 LLM（评审 P0）
def test_default_dictation_never_calls_llm(tmp_path, monkeypatch):
    """默认听写主路径不再同步等待本机大模型，也不会被模型改写。"""
    s, storage, p = make_pipeline(tmp_path)
    called = {"n": 0}

    def spy(text, hotwords=None):
        called["n"] += 1
        return "被模型改坏的文本", "local-llm"

    monkeypatch.setattr(p.llm, "organize", spy)
    r = p.process(np.zeros(1600, dtype=np.float32),
                  context=AppContext(app_name="邮件", bundle_id="com.apple.mail"))
    assert called["n"] == 0, "默认主路径不应调用大模型整理"
    assert "被模型改坏的文本" not in r.text
    storage.close()


def test_explicit_organize_llm_still_available(tmp_path, monkeypatch):
    """用户显式开启「每次听写都整理」时，能力仍完整保留。"""
    s, storage, p = make_pipeline(tmp_path, {"organize_llm": True})
    monkeypatch.setattr(
        p.llm, "organize",
        lambda text, hotwords=None: (text + "（已整理）", "local-llm"))
    r = p.process(np.zeros(1600, dtype=np.float32),
                  context=AppContext(app_name="邮件", bundle_id="com.apple.mail"))
    assert "（已整理）" in r.text
    assert "本机大模型整理" in r.applied
    storage.close()
