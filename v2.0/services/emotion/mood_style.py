"""MoodStyleTable — mood engine → chỉ dẫn giọng bằng CHỮ (PLAN Mood→Style).

Thay vì bơm số thô "vui=3 buc=8" (LLM dịch không đáng tin + kéo register máy móc),
tra bảng mood_style.yaml theo (chiều trội + band) → 1 chỉ dẫn 2-3 dòng bằng chữ.

Đây là bản ĐÚNG của mood block cũ: mood làm INPUT điều kiện (bảo LLM nói sao),
KHÔNG phải OUTPUT tự khai. Config dày, inject mỏng (1 ô/lượt).

Fail-safe (N7): mọi lỗi tra bảng → return None (không bơm gì), KHÔNG raise.
"""
from __future__ import annotations

from typing import Any

# Thứ tự khai báo → tie-break dominant (vui thắng khi bằng điểm).
_DIMS = ("vui", "buon", "buc", "bon_chon", "nguong")
_OVERRIDE_FLAGS = {"force_gentle_tone", "force_deflect"}


class MoodStyleTable:
    def __init__(
        self,
        policy: dict[str, Any],
        labels: dict[str, str],
        band_prefix: dict[str, str],
        styles: dict[str, dict[str, dict[str, str]]],
    ) -> None:
        self._inject_floor = int(policy.get("inject_floor", 6))
        self._bands = policy.get("bands", {"mid": [6, 7], "high": [8, 9], "peak": [10, 10]})
        self._tone_overrides = bool(policy.get("tone_flag_overrides", True))
        self._labels = dict(labels or {})
        self._band_prefix = dict(band_prefix or {})
        self._styles = styles or {}

    @classmethod
    def from_loader(cls, loader) -> "MoodStyleTable | None":
        """Đọc config/mood_style.yaml. None nếu thiếu/hỏng (fail-safe → không bơm)."""
        try:
            styles = loader.get("mood_style", "styles", {}) or {}
            if not styles:
                return None
            return cls(
                policy=loader.get("mood_style", "policy", {}) or {},
                labels=loader.get("mood_style", "labels", {}) or {},
                band_prefix=loader.get("mood_style", "band_prefix", {}) or {},
                styles=styles,
            )
        except Exception:
            return None

    def _band_for(self, val: int) -> str | None:
        for name, rng in self._bands.items():
            try:
                lo, hi = int(rng[0]), int(rng[1])
            except (TypeError, ValueError, IndexError):
                continue
            if lo <= val <= hi:
                return name
        return None

    def directive_for(self, mood, tone_flags: set[str] | None = None) -> str | None:
        """1 chuỗi chỉ dẫn giọng, hoặc None (không bơm). Không raise."""
        try:
            # 1. Tone flag thắng — case tổn thương thật / gạ gẫm xử ở tầng khác.
            if self._tone_overrides and tone_flags and (_OVERRIDE_FLAGS & set(tone_flags)):
                return None
            # 2. dominant (tie → thứ tự _DIMS: vui trước)
            dominant = max(_DIMS, key=lambda d: getattr(mood, d, 0))
            val = int(getattr(mood, dominant, 0))
            # 3. vùng chết dưới floor
            if val < self._inject_floor:
                return None
            # 4. band
            band = self._band_for(val)
            if band is None:
                return None
            # 5. render
            cell = self._styles.get(dominant, {}).get(band)
            if not cell:
                return None
            label = self._labels.get(dominant, dominant)
            prefix = self._band_prefix.get(band, "")
            return (
                f"- Đang {prefix}{label}: {cell.get('thai_do', '')}. "
                f"{cell.get('nhip', '')}. Câu {cell.get('do_dai', '')}. "
                f"Hay dùng: {cell.get('tu_dem', '')}."
            )
        except Exception:
            return None
