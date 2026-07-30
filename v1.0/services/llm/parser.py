"""Parser output LLM (ARCHITECTURE 7.4/8.2, persona.md Phần B, milestone 1.D).

Tách output thô của Mai thành:
- text: câu Mai NÓI (đã bỏ mood block + meta + reasoning nếu rò)
- mood: MoodState (interfaces/animation.py — key KHÔNG dấu)
- reason: nội dung dòng "lý do:"
- continuation: cờ "còn nữa: có/không" (Phase 2 CONTINUATION dùng sau; giờ chỉ parse)

Nguyên tắc **fail-safe** (N7 cho phần non-filter): model sai format thì VẪN trả text
để còn nói được, chỉ đánh `ok=False` + mood neutral. Không raise.

Chấp nhận key mood có dấu / không dấu / space / underscore (persona.md ghi chú):
  buồn≡buon, bực≡buc, bồn_chồn≡"bồn chồn"≡bon_chon, ngượng≡nguong.
"""
from __future__ import annotations

import re
import unicodedata

from pydantic import BaseModel

from interfaces.animation import MoodState

_MOOD_FIELDS = {"vui", "buon", "buc", "bon_chon", "nguong"}

# Bỏ reasoning nếu rò (Gemma 4 --reasoning off thì không có, nhưng thủ sẵn).
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_SPECIAL_TOKEN_RE = re.compile(r"<\|[^|>]*\|>")

# 1 block trong ngoặc vuông (không lồng nhau).
_BLOCK_RE = re.compile(r"\[([^\[\]]+)\]")
# Cặp key:number — key CHỈ khớp đúng 5 mood word (có/không dấu, space/underscore).
# Giới hạn key như vậy để value hỏng (vd "bực:abc") không nuốt nhầm key kế tiếp.
_PAIR_RE = re.compile(
    r"(vui|bu[ồo]n|b[ựu]c|b[ồo]n[ _]ch[ồo]n|ng[ưu][ợơo]ng)\s*:\s*(\d+)",
    re.IGNORECASE,
)

# "lý do:" / "ly do:" — lấy phần còn lại của DÒNG.
_REASON_RE = re.compile(r"l[ýy]\s*do\s*:\s*(.+)", re.IGNORECASE)
# "còn nữa:" / "con nua:" ...
_CONT_RE = re.compile(r"c[òo]n\s*n[ữu]a\s*:\s*(.+)", re.IGNORECASE)


class ParsedResponse(BaseModel):
    text: str
    mood: MoodState
    reason: str = ""
    continuation: bool = False
    ok: bool = False  # True khi mood block đủ 5 chiều parse được
    raw: str = ""


def _strip_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn"
    )


def _norm_key(k: str) -> str:
    return _strip_accents(k.strip().lower()).replace(" ", "_")


def _strip_reasoning(s: str) -> str:
    s = _THINK_RE.sub("", s)
    s = _SPECIAL_TOKEN_RE.sub("", s)
    return s


def _parse_continuation(tail: str) -> bool:
    m = _CONT_RE.search(tail)
    if not m:
        return False
    val = _strip_accents(m.group(1).strip().lower())
    if val.startswith("khong"):
        return False
    return val.startswith("co")


def _parse_reason(tail: str) -> str:
    m = _REASON_RE.search(tail)
    return m.group(1).strip() if m else ""


def _strip_meta_lines(text: str) -> str:
    """Bỏ dòng lý do/còn nữa lỡ dính vào text (khi không có block)."""
    keep = [
        ln for ln in text.splitlines()
        if not _REASON_RE.match(ln.strip()) and not _CONT_RE.match(ln.strip())
    ]
    return "\n".join(keep).strip()


def parse_response(raw: str) -> ParsedResponse:
    """Parse output thô → ParsedResponse. Không bao giờ raise (fail-safe)."""
    cleaned = _strip_reasoning(raw or "")

    # Tìm block có nhiều mood key nhận diện được nhất (né ngoặc vuông ngẫu nhiên trong text).
    best_block: re.Match | None = None
    best_mood: dict[str, int] = {}
    for m in _BLOCK_RE.finditer(cleaned):
        mood: dict[str, int] = {}
        for key, val in _PAIR_RE.findall(m.group(1)):
            nk = _norm_key(key)
            if nk in _MOOD_FIELDS:
                mood[nk] = max(0, min(10, int(val)))  # clamp 0-10 (fail-safe)
        if mood and len(mood) > len(best_mood):
            best_block, best_mood = m, mood

    if best_block is None:
        text = _strip_meta_lines(cleaned)
        return ParsedResponse(text=text, mood=MoodState(), raw=raw)

    text = cleaned[: best_block.start()].strip()
    tail = cleaned[best_block.end():]
    return ParsedResponse(
        text=text,
        mood=MoodState(**best_mood),
        reason=_parse_reason(tail),
        continuation=_parse_continuation(tail),
        ok=len(best_mood) == 5,
        raw=raw,
    )
