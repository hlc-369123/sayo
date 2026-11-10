"""本地文本润色引擎：把「口语转写」变成「可以直接发出去的文字」。

智能润色全部由本机规则实现，零云端、零 LLM 也可工作：
  · 去除填充词（中/英）           · 去除重复与口吃
  · 识别中途改口，只保留最终意图  · 口述列表/步骤自动排版
  · 口述标点还原                  · 中英混排空格与全半角规范化
  · 按当前应用切换语气（正式/随意/代码）
  · 个人词典纠错                  · 可选本地 LLM 深度改写/翻译/问答

每个步骤均可开关，并返回「应用了哪些改写」，界面上可逐条审计。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from .dictionary import PersonalDictionary

CJK = r"\u3400-\u9fff"

# ------------------------------------------------------------------ 词表
CN_FILLERS = [
    "嗯嗯", "嗯", "呃", "额", "唔", "噢", "哦", "啊", "呀",
    "那个那个", "这个这个", "你知道的", "怎么说呢", "就是说", "对吧", "是吧",
    "然后呢", "好吧", "其实吧", "怎么说", "我意思是", "额这个",
]
# 句首填充词：仅删「没实义、只是开口犹豫」的词。
# 注意： deliberately 不含「这个 / 那个」——它们是中文实义指示代词
# （这个东西 / 那个地方），若在句首被删会吞掉真实内容（见 test_leading_filler_keeps_demonstratives）。
# 真正的「那个，那个…」口吃式犹豫由 remove_repetitions 的「那个那个」规则处理。
CN_LEADING_FILLERS = ["就是", "然后", "所以说", "反正", "要不", "那么", "其实", "基本上"]
EN_FILLERS = [
    "um", "uh", "erm", "er", "hmm", "mm", "ah", "you know", "kind of", "sort of",
    "basically", "actually", "literally", "I guess", "right", "okay so", "well",
]
STRONG_CORRECTION_MARKERS = [
    "不对应该是", "说错了", "搞错了", "记错了", "哦不", "算了", "重新说", "改一下",
    "不对", "or rather", "I mean", "sorry", "wait", "no,",
]
WEAK_CORRECTION_MARKERS = ["不是", "应该是", "改成", "actually"]
SPOKEN_PUNCT = [
    ("新段落", "\n\n"), ("另起一段", "\n\n"), ("新的一段", "\n\n"), ("new paragraph", "\n\n"),
    ("换行", "\n"), ("另起一行", "\n"), ("下一行", "\n"), ("new line", "\n"),
    ("句号", "。"), ("逗号", "，"), ("问号", "？"), ("感叹号", "！"), ("叹号", "！"),
    ("冒号", "："), ("分号", "；"), ("顿号", "、"), ("省略号", "……"),
    ("period", "."), ("full stop", "."), ("comma", ","), ("question mark", "?"),
    ("exclamation mark", "!"), ("colon", ":"), ("semicolon", ";"),
]
ORDINALS = [
    ("第一", "1"), ("第二", "2"), ("第三", "3"), ("第四", "4"), ("第五", "5"),
    ("第六", "6"), ("第七", "7"), ("第八", "8"), ("第九", "9"), ("第十", "10"),
    ("首先", "1"), ("其次", "2"), ("再次", "3"), ("接着", "4"),
    ("first", "1"), ("second", "2"), ("third", "3"), ("fourth", "4"), ("fifth", "5"),
]
FORMAL_REPLACEMENTS = [
    ("搞定", "完成"), ("弄好", "完成"), ("弄", "处理"), ("挺", "较为"), ("比较好", "更优"),
    ("我觉得", "我们认为"), ("我想", "拟"), ("马上", "立即"), ("赶紧", "尽快"),
    ("但是", "然而"), ("所以", "因此"), ("大家", "各位"), ("这个", "该"), ("那个", "该"),
    ("有点儿", "略有"), ("有点", "略有"), ("差不多", "基本一致"), ("一下子", "一次性"),
    ("gonna", "going to"), ("wanna", "want to"), ("a lot of", "a number of"),
    (" kinda ", " "), (" gonna ", " going to "),
]


@dataclass
class PolishResult:
    text: str
    applied: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ 各步骤
def remove_self_corrections(text: str) -> tuple[str, bool]:
    """发现改口标记时，丢弃同一小句里标记之前的原话，只留最终意图。

    强标记（不对/说错了/I mean…）在句中即可生效；
    弱标记（不是/应该是…）只在小句开头生效，避免误删「我不是说了吗」这类正常表达。
    """
    changed = False
    clause = r"[^，,。！？!?；;\n]{1,45}"
    # 强标记：删掉同一小句里标记之前的原话，保留后面的最终意图
    for marker in sorted(STRONG_CORRECTION_MARKERS, key=len, reverse=True):
        pattern = re.compile("(" + clause + ")" + re.escape(marker) + r"[，,:\s]*",
                             re.I if marker.isascii() else 0)
        for _ in range(8):  # 有界循环，永不死循环
            m = pattern.search(text)
            if not m:
                break
            tail = text[m.end():]
            if not tail.strip("，,。！？!?；;\n "):
                break
            text = text[:m.start()] + tail
            changed = True
    # 弱标记：只删小句开头的标记本身，不动相邻文字，避免误删「我不是说了吗」
    for marker in sorted(WEAK_CORRECTION_MARKERS, key=len, reverse=True):
        pattern = re.compile(r"(?:^|(?<=[，,。！？!?；;\n]))\s*" + re.escape(marker) + r"[，,:\s]*",
                             re.I if marker.isascii() else 0)
        new, n = pattern.subn("", text)
        if n:
            text, changed = new, True
    text = re.sub(r"\s{2,}", " ", text)
    return text, changed


def remove_fillers(text: str) -> tuple[str, bool]:
    original = text
    # 英文填充词（词边界，防误伤 like / right 的实义用法）
    for w in EN_FILLERS:
        text = re.sub(r"(?i)(?<![A-Za-z])" + re.escape(w) + r"(?![A-Za-z])[,\s]*", " ", text)
    # 中文独立语气词
    for w in CN_FILLERS:
        text = re.sub(r"(?<![A-Za-z0-9])" + re.escape(w) + r"[，,\s]*", "", text)
    # 仅在小句开头出现的填充词（句中「就是」「然后」有实义，不删）
    for w in CN_LEADING_FILLERS:
        text = re.sub(r"(?:(?<=^)|(?<=[。！？!?；;\n]))\s*" + re.escape(w) + r"[，,\s]*", "", text)
    # 句首 well / so
    text = re.sub(r"(?i)(?:(?<=^)|(?<=[.!?\n]))\s*(?:well|so|okay)[,\s]+", "", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"(?<=[，,])[ \t]+", "", text)
    return text, text != original


def remove_repetitions(text: str) -> tuple[str, bool]:
    original = text
    # 英文连续重复词： the the / I I
    text = re.sub(r"(?i)\b([A-Za-z']{1,20})(\s+\1\b)+", r"\1", text)
    # 中文短语连续重复：我们我们 / 这个这个
    text = re.sub(r"([" + CJK + r"]{2,8})\1+", r"\1", text)
    # 口吃：同一汉字连续 3 次以上 -> 1 次；常见口吃字 2 次也合并
    text = re.sub(r"([" + CJK + r"])\1{2,}", r"\1", text)
    text = re.sub(r"([我你他这那是就的])\1", r"\1", text)
    # 「呃，呃」「嗯，嗯」残留
    text = re.sub(r"(?:嗯|呃|啊)[，,]\s*(?:嗯|呃|啊)[，,]?", "", text)
    return text, text != original


def restore_spoken_punctuation(text: str) -> tuple[str, bool]:
    original = text
    for spoken, symbol in SPOKEN_PUNCT:
        text = re.sub(r"(?i)\s*" + re.escape(spoken) + r"\s*", symbol, text)
    return text, text != original


def format_lists(text: str) -> tuple[str, bool]:
    original = text
    for word, num in ORDINALS:
        # 仅当序数词后面紧跟停顿/冒号时才视为列表项
        text = re.sub(
            r"(?:(?<=^)|(?<=[\n。；;，,]))\s*" + re.escape(word) + r"(?:点|项|个)?\s*[，,、:：]?\s*",
            ("" if text.startswith(word) else "\n") + num + ". ",
            text,
        )
    text = re.sub(r"(?:(?<=^)|(?<=[\n。；;，,]))\s*(?:最后|最后一点|最后一点是|finally|lastly)[，,、:：]?\s*", "\n- ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip("\n")
    return text, text != original


def normalize_spacing(text: str) -> tuple[str, bool]:
    original = text
    # 中文之间的多余空格
    text = re.sub(r"([" + CJK + r"])[ \t]+(?=[" + CJK + r"])", r"\1", text)
    # 中英/中数之间补空格
    text = re.sub(r"([" + CJK + r"])([A-Za-z0-9])", r"\1 \2", text)
    text = re.sub(r"([A-Za-z0-9])([" + CJK + r"])", r"\1 \2", text)
    # 标点前后空白
    text = re.sub(r"[ \t]+([，。！？；：、,.!?;:])", r"\1", text)
    text = re.sub(r"([，。！？；：、])[ \t]+", r"\1", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"([。！？!?])\1+", r"\1", text)
    return text, text != original


def normalize_punctuation(text: str, chinese: bool) -> tuple[str, bool]:
    """全/半角标点规范化；只在中文语境且紧邻汉字时转换，保护小数点、英文缩写和代码。"""
    original = text
    if chinese:
        pairs = {",": "，", "?": "？", "!": "！", ";": "；", ":": "："}
        # 逗号等：紧邻汉字才转全角
        text = re.sub(
            r"(?:(?<=[" + CJK + r"])|(?=[" + CJK + r"]))[,?!;:]",
            lambda m: pairs.get(m.group(0), m.group(0)), text,
        )
        # 句点：仅句末或后接汉字、且不是数字间的小数点
        text = re.sub(r"(?<!\d)\.(?=\s*(?:[" + CJK + r"]|$))", "。", text)
        text = re.sub(r"([，。！？；：、])\1+", r"\1", text)
        if text and text[-1] not in "。！？；：、,.!?;:…\n":
            text += "。"
    else:
        text = re.sub(r"\s+([,.!?;:])", r"\1", text)
        if text and text[-1] not in ".!?;:,\n":
            text += "."
    return text, text != original


# 话语连接词：连接分句，不构成新句子，其前补逗号
_CN_CONNECTIVES = [
    "但是", "但", "所以", "因此", "因而", "而且", "并且", "另外", "此外",
    "最后", "首先", "其次", "然后", "接着", "同时", "否则", "不过", "然而",
    "其实", "总之", "比如", "例如", "如果", "因为", "虽然", "即使", "只要",
    "一旦", "假如", "换句话说", "也就是说", "与此同时",
]
# 句末语气词：其后还有内容 → 视为一句结束，补句号
_CN_SENT_END = "吧啦咯嘛哦耶哈嘞呗哟"
# 句末语气词：其后还有内容 → 补问号
_CN_QUEST_END = "吗呢"


def restore_sentence_punctuation(text: str) -> tuple[str, bool]:
    """按中文语义重新断句与标点。

    背景：即便引擎已去掉 VAD 切片间的虚假句号，拼接后的文本仍是「流水句」，
    缺少符合语义的逗号与句号。本步骤依据话语连接词与句末语气词补回自然停顿：
      · 连接词（但是 / 所以 / 如果 …）前补逗号 —— 它们连接分句，而非另起一句
      · 「的话 / 的时候 / 的情况下 / 的前提下」后补逗号
      · 句末语气词（吧 / 啦 / 吗 / 呢 …）后若还有内容，补句号 / 问号，标志一句结束
      · 末尾统一补一个句号
    不做「每小句都加句号」式处理，避免通篇都是「。」。
    """
    original = text
    s = text.strip()
    if not s:
        return text, False

    # 1) 连接词前补逗号（不在句首、且前面不是标点）
    for c in sorted(_CN_CONNECTIVES, key=len, reverse=True):
        s = re.sub(r"(?<!^)(?<![，。！？；：、,.!?;:！？])\s*" + re.escape(c), "，" + c, s)

    # 2) 「的话 / 的时候 / 的情况下 / 的前提下」后补逗号
    s = re.sub(r"(的话|的时候|的情况下|的前提下)(?=[^\s，。！？；：、,.!?;:])", r"\1，", s)

    # 3) 句末语气词后若还有内容，补句号 / 问号
    s = re.sub(r"([" + _CN_SENT_END + r"])(?=[^\s，。！？；：、,.!?;:])", r"\1。", s)
    s = re.sub(r"([" + _CN_QUEST_END + r"])(?=[^\s，。！？；：、,.!?;:])", r"\1？", s)

    # 4) 末尾补句号
    if s and s[-1] not in "。！？；：、,.!?;:…\n":
        s += "。"

    # 5) 清理：去掉「，。」之类、合并重复标点、清掉标点前空格
    s = re.sub(r"，\s*[。！？]", lambda m: m.group(0)[-1], s)
    s = re.sub(r"([，。！？；：])\1+", r"\1", s)
    s = re.sub(r"[ \t]+([，。！？；：、])", r"\1", s)
    return s, s != original


def apply_tone(text: str, tone: str) -> tuple[str, bool]:
    if tone in ("", "none", "casual", "code", "auto"):
        return text, False
    original = text
    if tone == "formal":
        for a, b in FORMAL_REPLACEMENTS:
            text = text.replace(a, b)
    return text, text != original


class TextPolisher:
    """按顺序执行可配置的本地润色步骤。"""

    def __init__(self, options: dict | None = None, dictionary: PersonalDictionary | None = None):
        self.options = options or {}
        self.dictionary = dictionary

    def _enabled(self, key: str) -> bool:
        return bool(self.options.get(key, True))

    def polish(self, raw: str, *, tone: str = "auto",
               llm_edit: Callable[[str], str] | None = None) -> PolishResult:
        text = (raw or "").strip()
        if not text:
            return PolishResult(text="")
        applied: list[str] = []
        chinese = bool(re.search(r"[" + CJK + r"]", text))

        steps: list[tuple[str, str, Callable[[str], tuple[str, bool]]]] = [
            ("self_correction", "识别改口", remove_self_corrections),
            ("punctuation", "口述标点还原", restore_spoken_punctuation),
            ("remove_fillers", "去填充词", remove_fillers),
            ("remove_repetitions", "去重复口吃", remove_repetitions),
            ("auto_format", "列表自动排版", format_lists),
        ]
        for key, label, fn in steps:
            if self._enabled(key):
                text, changed = fn(text)
                if changed:
                    applied.append(label)

        if self._enabled("personal_dictionary") and self.dictionary is not None:
            new = self.dictionary.correct(text)
            if new != text:
                text, _ = new, applied.append("个人词典纠错")

        if self._enabled("normalize_spacing"):
            text, changed = normalize_spacing(text)
            if changed:
                applied.append("中英混排规范化")

        if tone not in ("code",):
            if self._enabled("punctuation"):
                text, changed = restore_sentence_punctuation(text)
                if changed:
                    applied.append("语义断句")
                text, changed = normalize_punctuation(text, chinese)
                if changed:
                    applied.append("标点规范化")

        text, tone_changed = apply_tone(text, tone)
        if tone_changed:
            applied.append(f"语气适配({tone})")

        if llm_edit is not None:
            new = llm_edit(text)
            if new and new != text:
                text = new
                applied.append("本地 LLM 深度改写")

        return PolishResult(text=text.strip(), applied=applied)
