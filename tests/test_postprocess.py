"""本地文本润色规则测试（不需要模型、不需要网络）。"""
from app.core.postprocess import (
    TextPolisher, format_lists, normalize_spacing, remove_fillers,
    remove_repetitions, remove_self_corrections, restore_spoken_punctuation,
    restore_sentence_punctuation,
)


def test_fillers_zh_en():
    text = "嗯，那个，我想说一下这个方案，you know, it's basically fine."
    out, changed = remove_fillers(text)
    assert changed
    assert "嗯" not in out
    assert "you know" not in out.lower()
    assert "basically" not in out.lower()
    assert "方案" in out


def test_leading_filler_keeps_demonstratives():
    """回归：句首的「这个 / 那个」是指示代词（这个东西 / 那个地方），不能当填充词删掉。"""
    out, _ = remove_fillers("这个东西它能移动，然后它就能用了")
    assert out.startswith("这个东西"), out
    out2, _ = remove_fillers("那个地方我记得改过了。其实没改。")
    assert out2.startswith("那个地方"), out2
    # 「就是 / 然后 / 其实」等纯犹豫词在句首仍应被清理
    out3, _ = remove_fillers("就是，我觉得还行")
    assert out3 == "我觉得还行", out3


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


def test_vad_segments_no_spurious_periods():
    """回归：VAD 把一句话切成多段、每段都带句号时，不应通篇都是「。」。

    模拟引擎已去掉切片间虚假句号后的拼接结果（流水句），语义断句应只保留
    真正的句末句号（语气词后 / 整段末尾），其余用逗号分隔。
    """
    joined = ("主要现在做的都是这种图片的视频后面如果有其他的形式的话"
              "再往上迭代吧计划是准备给它做成至少自动化率达到 95%，"
              "不需要人怎么操作但现在的话还有几个模块还要再补一补")
    out, _ = restore_sentence_punctuation(joined)
    # 通篇句号的反例：每两三个字就一个「。」——不允许出现「X。Y」把短语当句
    assert "主要现在做的都是" in out, out
    assert "再往上迭代吧。" in out, out
    assert out.endswith("。"), out
    # 句中真实句号只能出现在句末语气词之后或结尾，数量应远小于「每段一个」
    assert out.count("。") <= 2, out  # 仅「吧」后一处 + 结尾一处


def test_semantic_punctuation_connectives_and_particles():
    """连接词前补逗号；「的话」后补逗号；句末语气词后补句号/问号。"""
    # 连接词 + 语气词
    out, _ = restore_sentence_punctuation("今天天气很好所以我们去公园吧明天再写代码")
    assert "，所以" in out, out
    assert "去公园吧。" in out, out
    # 问号语气词
    out2, _ = restore_sentence_punctuation("你知道这个方法吗我觉得不太行")
    assert "你知道这个方法吗？" in out2, out2
    # 「的话」后逗号
    out3, _ = restore_sentence_punctuation("如果下雨的话我们就改期")
    assert "如果下雨的话，" in out3, out3

