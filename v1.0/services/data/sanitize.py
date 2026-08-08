"""Sanitize identifiers for local logs and shareable training datasets.

Data đem train KHÔNG được lẫn PII người xem:
- viewer_id → HMAC-SHA256 với salt local, không lưu ID gốc.
- email, phone, handle, secret URL, address, IP and common IDs → `[PII]`.

Làm từ ĐẦU (lúc ghi log), không sửa sau — data lỡ ghi PII coi như hỏng.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

# email
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
# số điện thoại VN/quốc tế (8-15 chữ số, cho phép +, space, -, dấu chấm)
_PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d .\-]{7,14}\d)(?!\w)")
# token/khoá dài (≥20 ký tự chữ+số liền, kiểu api key)
_TOKEN_RE = re.compile(r"\b[A-Za-z0-9_\-]{20,}\b")
_HANDLE_RE = re.compile(r"(?<![\w@])@[A-Za-z0-9_][A-Za-z0-9_.-]{1,31}\b")
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_IP_RE = re.compile(
    r"(?<!\d)(?:25[0-5]|2[0-4]\d|1?\d?\d)"
    r"(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}(?!\d)"
)
_LABELED_ID_RE = re.compile(
    r"(?i)\b(CCCD|CMND|passport|hộ\s*chiếu|ID)\s*[:#-]?\s*[A-Z0-9][A-Z0-9-]{5,}\b"
)
_LABELED_NAME_RE = re.compile(
    r"(?i)\b(tên\s+(?:tôi|mình|em)\s+là|họ\s*và\s*tên|full\s*name|name)"
    r"\s*[:=-]?\s*[\wÀ-ỹ]+(?:\s+[\wÀ-ỹ]+){0,3}"
)
_ADDRESS_RE = re.compile(
    r"(?i)\b(địa\s*chỉ|address)\s*[:=-]?\s*[^,;\n]{5,100}"
)
_SECRET_QUERY_KEYS = {
    "access_token", "api_key", "apikey", "auth", "code", "key", "password",
    "secret", "session", "signature", "sig", "token",
}

_MASK = "[PII]"
_PROCESS_SALT = secrets.token_bytes(32)
_LOCAL_SALT = _PROCESS_SALT


def configure_hash_salt(path: str | Path) -> Path:
    """Load or create the local HMAC salt. The path must stay ignored by git."""
    global _LOCAL_SALT
    salt_path = Path(path)
    salt_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with salt_path.open("xb") as stream:
            stream.write(secrets.token_bytes(32))
    except FileExistsError:
        pass
    salt = salt_path.read_bytes()
    if len(salt) < 32:
        raise ValueError("privacy hash salt must contain at least 32 bytes")
    _LOCAL_SALT = salt
    return salt_path


def hash_viewer_id(viewer_id: str | None, *, salt: bytes | None = None) -> str | None:
    """Return a non-reversible, locally salted viewer pseudonym."""
    if not viewer_id:
        return None
    key = salt or _LOCAL_SALT
    digest = hmac.new(key, str(viewer_id).encode("utf-8"), hashlib.sha256).hexdigest()[:16]
    return f"v_{digest}"


def mask_pii(text: str | None) -> str | None:
    """Mask common direct identifiers. None/empty remains unchanged; never raises."""
    return mask_pii_with_count(text)[0]


def mask_known_identifier(text: str | None, identifier: str | None) -> str | None:
    """Mask an identifier known from structured metadata, such as a display name."""
    if not text or not identifier or len(identifier.strip()) < 2:
        return text
    try:
        return re.sub(re.escape(identifier.strip()), _MASK, text, flags=re.IGNORECASE)
    except Exception:
        return text


def mask_pii_with_count(text: str | None) -> tuple[str | None, int]:
    """Return sanitized text and number of substitutions for dry-run reporting."""
    if not text:
        return text, 0
    try:
        count = 0
        t, n = _EMAIL_RE.subn(_MASK, text)
        count += n
        t, n = _mask_sensitive_urls(t)
        count += n
        t, n = _IP_RE.subn(_MASK, t)
        count += n
        t, n = _PHONE_RE.subn(_MASK, t)
        count += n
        t, n = _LABELED_ID_RE.subn(lambda m: f"{m.group(1)} {_MASK}", t)
        count += n
        t, n = _LABELED_NAME_RE.subn(lambda m: f"{m.group(1)} {_MASK}", t)
        count += n
        t, n = _ADDRESS_RE.subn(lambda m: f"{m.group(1)} {_MASK}", t)
        count += n
        t, n = _HANDLE_RE.subn(_MASK, t)
        count += n
        t, n = _TOKEN_RE.subn(_MASK, t)
        count += n
        return t, count
    except Exception:
        return text, 0


def _mask_sensitive_urls(text: str) -> tuple[str, int]:
    count = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal count
        url = match.group(0)
        try:
            query_keys = {key.lower() for key, _value in parse_qsl(urlsplit(url).query)}
            if not query_keys & _SECRET_QUERY_KEYS:
                return url
        except Exception:
            pass
        count += 1
        return _MASK

    return _URL_RE.sub(replace, text), count
