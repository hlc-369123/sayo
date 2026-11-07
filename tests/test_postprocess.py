"""本地文本润色规则测试（不需要模型、不需要网络）。"""
from app.core.postprocess import (
    TextPolisher, format_lists, normalize_spacing, remove_fillers,
    remove_repetitions, remove_self_corrections, restore_spoken_punctuation,
)


def test_fillers_zh_en():
    text = "嗯，那个，我想说一下这个方案，you know, it's basically fine."
    out, changed = remove_fillers(text)
    assert changed
    assert "嗯" not in out
    assert "you know" not in out.lower()
    assert "basically" not in out.lower()
    assert "方案" in out


def test_repetitions():
    out, _ = remove_repetitions("我我我想去 the the meeting，这个这个方案")
    assert "我我我" not in out
    assert "the the" not in out
    assert "这个这个" not in out


def test_self_correction():
    out, changed = remove_self_corrections("我们明天开会不对应该是后天开会")
    assert changed
    assert "明天开会" not in out
    assert "后天开会" in out


def test_weak_marker_does_not_eat_normal_sentence():
    out, _ = remove_self_corrections("我不是说了吗，明天再交")
    assert "不是说了吗" in out


def test_spoken_punctuation_and_lists():
    out, _ = restore_spoken_punctuation("今天天气很好句号明天再说")
    assert "。" in out
    listed, changed = format_lists("第一，确认需求。第二，写方案。最后，评审。")
    assert changed
    assert "1." in listed and "2." in listed and "-" in listed


def test_mixed_spacing():
    out, _ = normalize_spacing("使用Python开发ASR模型，速度很快")
    assert "使用 Python 开发 ASR 模型" in out


def test_full_pipeline_is_deterministic():
    p = TextPolisher({}, None)
    r = p.polish("嗯，那个，我我我想说的是，第一，确认需求。第二，写方案。", tone="none")
    assert "嗯" not in r.text
    assert "我我我" not in r.text
    assert "1." in r.text
    assert r.applied  # 至少记录了一条改写
