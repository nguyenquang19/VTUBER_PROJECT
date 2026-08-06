"""Sanitize PII cho data log (T4, Phase 8 data pipeline).

Data đem train KHÔNG được lẫn PII người xem:
- viewer_id (channel id gốc) → hash sha1 8 ký tự, KHÔNG lưu id gốc.
- email / số điện thoại / token dài trong text → mask `[PII]`.

Làm từ ĐẦU (lúc ghi log), không sửa sau — data lỡ ghi PII coi như hỏng.
"""
from __future__ import annotations

import hashlib
import re

# email
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
# số điện thoại VN/quốc tế (8-15 chữ số, cho phép +, space, -, dấu chấm)
_PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d .\-]{7,14}\d)(?!\w)")
# token/khoá dài (≥20 ký tự chữ+số liền, kiểu api key)
_TOKEN_RE = re.compile(r"\b[A-Za-z0-9_\-]{20,}\b")

_MASK = "[PII]"


def hash_viewer_id(viewer_id: str | None) -> str | None:
    """sha1 8 ký tự. None → None. Không lưu id gốc (không reverse được từ log)."""
    if not viewer_id:
        return None
    return hashlib.sha1(str(viewer_id).encode("utf-8")).hexdigest()[:8]


def mask_pii(text: str | None) -> str | None:
    """Mask email/phone/token dài → [PII]. None/rỗng → giữ nguyên. Không raise."""
    if not text:
        return text
    try:
        t = _EMAIL_RE.sub(_MASK, text)
        t = _PHONE_RE.sub(_MASK, t)
        t = _TOKEN_RE.sub(_MASK, t)
        return t
    except Exception:
        return text
