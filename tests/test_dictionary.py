from app.core.asr import build_hotwords
from app.core.dictionary import PersonalDictionary
from app.core.storage import Storage


def test_alias_correction(tmp_path):
    s = Storage(tmp_path / "t.db")
    s.add_term("张伟", ["张卫", "章伟"], "")
    d = PersonalDictionary(s.terms())
    assert d.correct("张卫今天来开会") == "张伟今天来开会"
    # 同音纠错（拼音一致、字面不同）
    assert "章伟今天来开会" != d.correct("章伟今天来开会")
    s.close()


def test_replacement_and_english_typo(tmp_path):
    s = Storage(tmp_path / "t.db")
    s.add_term("sayo", [], "轻述")
    s.add_term("sherpa-onnx", ["sherpa onx"], "")
    d = PersonalDictionary(s.terms())
    assert "轻述" in d.correct("打开 sayo 设置")
    assert "sherpa-onnx" in d.correct("使用 sherpa onx 推理")
    s.close()


def test_hotwords():
    d = PersonalDictionary([{"term": "张伟", "aliases": "", "replacement": ""},
                            {"term": "sayo", "aliases": "", "replacement": ""}])
    assert "张伟" in d.hotwords()
    hotwords = build_hotwords(d.hotwords())
    assert hotwords is not None
    assert "张 伟" in hotwords and "SAYO" in hotwords
