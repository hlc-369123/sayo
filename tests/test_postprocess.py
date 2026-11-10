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


def test_cn_digits_never_get_spaced():
    """回归（评审 §3.1）：中文数字混排不插空格，英文技术名词保留空格。"""
    out, _ = normalize_spacing("我们下午 3 点开会，目标达到 95 %")
    assert "下午3点" in out, out
    assert "达到95%" in out, out
    # 中文↔字母仍补空格，英文 token 尾数字后接汉字也补
    en, _ = normalize_spacing("使用Python和PySide6开发")
    assert "使用 Python 和 PySide6 开发" in en, en
    # 「macOS 26 开发」里数字后的合理空格不能被误删
    keep, _ = normalize_spacing("支持 macOS 26 开发")
    assert keep == "支持 macOS 26 开发", keep


def test_modal_particle_keeps_comma_not_period():
    """回归（评审 §3.2）：语气词后的话题延续只能补逗号，不能强行断句。"""
    out, _ = restore_sentence_punctuation("我觉得吧这件事情可以再看看")
    assert "我觉得吧，这件" in out, out
    assert "吧。" not in out, out
    out2, _ = restore_sentence_punctuation("去公园吧然后吃午饭")
    assert "吧，然后" in out2, out2
    # 明确另起一句时仍应断句（用户黄金样例依赖这一点）
    out3, _ = restore_sentence_punctuation("再往上迭代吧计划是准备做成")
    assert "吧。计划" in out3, out3


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


def test_strip_spurious_periods_over_punctuated():
    """回归：SenseVoice 在单切片内部撒的散点句号应被剥离，仅保留末次。"""
    from app.core.asr import _strip_spurious_periods
    bad = ("主要现在。做的。都是这种图片的视频。后面如果有其他的。形式的话再往上迭代吧。"
           "计划是准备给它做成。至少自动化率达到 95%，不需要人怎么操作。但现在的话还有。"
           "几个模块还要再补一补。")
    out = _strip_spurious_periods(bad)
    assert out.count("。") == 1, out
    assert "主要现在做的都是这种图片的视频" in out
    assert out.endswith("几个模块还要再补一补。")


def test_strip_spurious_periods_keeps_normal_sentences():
    """回归：正常长句与少量独立短句不被误并。"""
    from app.core.asr import _strip_spurious_periods
    normal = "今天天气很好。我们去公园吧，然后吃午饭。"
    assert _strip_spurious_periods(normal) == normal
    three = "我来了。他走了。事情办完了。"
    assert _strip_spurious_periods(three) == three


def test_polish_reconstructs_user_golden_punctuation():
    """回归：散点句号剥离后，规则兜底应还原出正确的逗号/分号/句号骨架。

    规则不擅自删词，故原稿中识别插入的『的/的话/还』仍会保留；
    删除冗余字是本地 LLM 整理层（organize）的职责，本测试只校验标点骨架。
    """
    from app.core.asr import _strip_spurious_periods
    bad = ("主要现在。做的。都是这种图片的视频。后面如果有其他的。形式的话再往上迭代吧。"
           "计划是准备给它做成。至少自动化率达到 95%，不需要人怎么操作。但现在的话还有。"
           "几个模块还要再补一补。")
    raw = _strip_spurious_periods(bad)
    out, _ = restore_sentence_punctuation(raw)
    assert "，后面如果有" in out, out
    assert "再往上迭代吧。" in out, out
    assert "；但" in out, out
    assert out.endswith("。"), out
    assert out.count("。") <= 2, out  # 仅「吧」后一处 + 结尾一处

