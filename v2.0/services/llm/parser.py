"""Parser output LLM (ARCHITECTURE 7.4/8.2, config/prompts/persona_system.txt Phần B, milestone 1.D).

**A1 (docs/03_COMPONENT_REFERENCE.md §PHASE A):** persona đã BỎ yêu cầu mood block —
Mai chỉ nói thoại. Parser giữ khả năng strip mood block DEFENSIVE (nếu LLM lỡ
vẫn sinh do prompt cũ trong ngữ cảnh) nhưng KHÔNG còn dùng nó làm control flow.
`ok` = True miễn có text non-empty. `mood` = MoodState() default nếu không có
block. `continuation` = suy từ dấu câu cuối (roadmap A1: bỏ auto-parse "còn nữa").

Nguyên tắc **fail-safe** (N7): sai format thì vẫn trả text để nói được. Không raise.
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
    ok: bool = False  # A1: True khi text non-empty (mood block không còn bắt buộc).
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


def _infer_continuation(text: str) -> bool:
    """A1: suy 'còn nữa' từ dấu câu cuối text (roadmap khuyến, bỏ auto-parse).

    True khi kết thúc bằng dấu bỏ lửng (', ', '...', '…') → LLM ngụ ý còn ý.
    False cho câu kết bằng '.', '!', '?' hoặc không dấu.
    """
    if not text:
        return False
    stripped = text.rstrip()
    return stripped.endswith((",", "...", "…"))


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
        return ParsedResponse(
            text=text,
            mood=MoodState(),
            continuation=_infer_continuation(text),
            ok=bool(text.strip()),  # A1: ok = text non-empty
            raw=raw,
        )

    # DEFENSIVE (A1): LLM lỡ vẫn xuất mood block → strip khỏi text, KHÔNG dùng
    # làm control flow. mood field vẫn giữ để backward compat với caller cũ đọc.
    text = cleaned[: best_block.start()].strip()
    tail = cleaned[best_block.end():]
    return ParsedResponse(
        text=text,
        mood=MoodState(**best_mood),
        reason=_parse_reason(tail),
        continuation=_parse_continuation(tail) or _infer_continuation(text),
        ok=bool(text),  # A1: ok = text non-empty (không còn phụ thuộc mood block đủ 5)
        raw=raw,
    )
