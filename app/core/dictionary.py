"""本地个人词典：优先识别人名、品牌、专业术语。

SenseVoice 不支持热词注入，因此采用「识别后纠错」路线：
1) 精确/别名匹配；
2) 同音词纠错（中文拼音，pypinyin 可选依赖）；
3) 英文近似拼写纠错（编辑距离）。
全过程只用本地词表，不调用任何服务。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

try:
    from pypinyin import lazy_pinyin  # type: ignore
except ImportError:  # 打包失败或裁剪依赖时优雅降级
    lazy_pinyin = None


@dataclass(frozen=True)
class Entry:
    term: str
    aliases: tuple[str, ...]
    replacement: str

    @property
    def output(self) -> str:
        return self.replacement or self.term


def _pinyin(word: str) -> str:
    if lazy_pinyin is None:
        return ""
    return "".join(lazy_pinyin(word)).lower()


def _edit_distance(a: str, b: str) -> int:
    if abs(len(a) - len(b)) > 2:
        return 99
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


class PersonalDictionary:
    def __init__(self, rows: list[dict] | None = None):
        self.entries: list[Entry] = []
        self.load(rows or [])

    def load(self, rows: list[dict]) -> None:
        self.entries = [
            Entry(
                term=r["term"],
                aliases=tuple(x for x in (r.get("aliases") or "").split(",") if x),
                replacement=r.get("replacement") or "",
            )
            for r in rows
            if r.get("term")
        ]

    def correct(self, text: str) -> str:
        if not text or not self.entries:
            return text

        # 1) 标准词 -> 自定义替换；以及别名/错写 -> 标准写法（长词优先，避免子串误伤）
        for e in sorted(self.entries,
                        key=lambda x: -max([len(x.term), 1] + [len(a) for a in x.aliases])):
            if e.replacement and e.term in text:
                text = text.replace(e.term, e.output)
            for alias in e.aliases:
                if alias and alias != e.output and alias in text:
                    text = text.replace(alias, e.output)

        # 2) 中文同音纠错：按词长滑动窗口，拼音相同且字面不同才替换
        if lazy_pinyin is not None:
            cn_entries = [e for e in self.entries if re.fullmatch(r"[\u4e00-\u9fff]{2,6}", e.term)]
            for e in cn_entries:
                target_py = _pinyin(e.term)
                if not target_py:
                    continue
                n = len(e.term)
                out, i = [], 0
                changed = False
                while i < len(text):
                    if i + n <= len(text):
                        win = text[i:i + n]
                        if (re.fullmatch(r"[\u4e00-\u9fff]+", win)
                                and win != e.output
                                and _pinyin(win) == target_py):
                            out.append(e.output)
                            i += n
                            changed = True
                            continue
                    out.append(text[i])
                    i += 1
                if changed:
                    text = "".join(out)

        # 3) 英文拼写纠错：仅对长度 >= 5 的词做编辑距离 <= 1 的保守替换
        en_entries = [e for e in self.entries if re.fullmatch(r"[A-Za-z][A-Za-z0-9\-]{4,}", e.term)]
        if en_entries:
            def repl(m: re.Match) -> str:
                w = m.group(0)
                if w in {e.output for e in en_entries}:
                    return w
                for e in en_entries:
                    if _edit_distance(w.lower(), e.term.lower()) <= 1:
                        return e.output
                return w
            text = re.sub(r"[A-Za-z][A-Za-z0-9\-]{4,}", repl, text)

        return text

    def hotwords(self) -> list[str]:
        """供支持热词的引擎（如 Zipformer/Paraformer）使用。"""
        return [e.output for e in self.entries]
