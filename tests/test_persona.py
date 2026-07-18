"""
test_persona.py — Test đọc persona, build system prompt, và emotion parser.
Chuẩn pytest (có assert).
"""
from core.persona import Persona, build_monologue_inputs
from core.emotion import parse_emotion


def _load():
    return Persona.load("config/persona_core.yaml", "config/persona_evolving.yaml")


def test_persona_loads():
    p = _load()
    assert p.core["identity"]["name"]           # có tên
    assert p.core["personality_core"]["traits"] # có tính cách


def test_system_prompt_has_key_parts():
    p = _load()
    sp = p.build_system_prompt()
    name = p.core["identity"]["name"]
    assert name in sp                     # tên nhân vật xuất hiện
    assert "RANH GIỚI" in sp              # có ranh giới an toàn
    assert "emotion" in sp.lower()        # có hướng dẫn emotion tag


def test_monologue_inputs_not_empty():
    p = _load()
    interests, seeds = build_monologue_inputs(p)
    assert len(interests) > 0             # có sở thích để tự nói
    assert len(seeds) > 0                 # có topic seed


def test_emotion_parser_valid_tag():
    emo, clean = parse_emotion("[emotion:happy] Chao moi nguoi!")
    assert emo == "happy"
    assert "[emotion" not in clean        # thẻ đã bị gỡ


def test_emotion_parser_with_spaces():
    emo, clean = parse_emotion("[emotion: laugh ] hehe")
    assert emo == "laugh"
    assert "[emotion" not in clean


def test_emotion_parser_no_tag():
    emo, clean = parse_emotion("Khong co the gi ca.")
    assert emo is None
    assert clean == "Khong co the gi ca."


def test_emotion_parser_unknown_tag_stripped():
    """Thẻ lạ: emotion=None nhưng vẫn phải gỡ khỏi text (TTS không đọc)."""
    emo, clean = parse_emotion("[emotion:xyz] noi dung")
    assert emo is None
    assert "[emotion" not in clean


def test_emotion_parser_midsentence():
    emo, clean = parse_emotion("Giua cau [emotion:pout] cung phai go")
    assert emo == "pout"
    assert "[emotion" not in clean


if __name__ == "__main__":
    p = _load()
    print(p.build_system_prompt())
    print("\nInterests:", build_monologue_inputs(p)[0])
