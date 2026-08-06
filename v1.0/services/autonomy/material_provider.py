"""MaterialProvider — cấp dữ kiện thật cho mỗi category (Aut.B, spec Mục 2.4 Bước 1).

Vấn đề bản gốc: prompt_hint là mô tả trừu tượng → LLM tự bịa mỗi lần → hội tụ
về vài pattern quen. Fix: mỗi category có nguồn dữ kiện cụ thể (số/pool/memory).

`get()` trả None nếu không có material đủ → composer bỏ category khỏi candidate
(không bao giờ để LLM bịa từ số 0).
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

from services.autonomy.pools import RoundRobinPool

# A2: band mặc định nếu config thiếu (fail-safe). max = ngưỡng trên inclusive.
_DEFAULT_SILENCE_BANDS = [
    {"max": 25, "phrase": "vừa mới lặng đi một chút"},
    {"max": 75, "phrase": "im được một lúc rồi"},
    {"max": 999999, "phrase": "im lặng hơi lâu rồi đấy"},
]
_DEFAULT_CHAT_BANDS = [
    {"max": 0, "phrase": "chat đang vắng tanh"},
    {"max": 8, "phrase": "chat chỉ lác đác vài người"},
    {"max": 30, "phrase": "chat đang rôm rả"},
    {"max": 999999, "phrase": "chat đang trôi nhanh không đọc kịp"},
]
_DEFAULT_IGNORED_BANDS = [
    {"max": 0, "phrase": "vừa mới lên tiếng"},
    {"max": 2, "phrase": "đã gọi mấy lần mà chưa thấy ông đâu"},
    {"max": 999999, "phrase": "gọi hoài mà ông lơ luôn"},
]


def _band_phrase(value: float, bands: list[dict]) -> str:
    """A2: map SỐ → LỜI TỰ NHIÊN theo band (cấm đọc số thô ra prompt).

    Trả phrase của band đầu tiên có value <= max. Fallback phrase band cuối.
    """
    for b in bands:
        if value <= float(b.get("max", 0)):
            return str(b.get("phrase", ""))
    return str(bands[-1].get("phrase", "")) if bands else ""


@dataclass
class RuntimeContext:
    """State thô để MaterialProvider dispatch. Composer (Aut.C) fill từ orchestrator."""
    silence_seconds: float = 0.0
    chat_count_last_10min: int = 0
    operator_online: bool = False
    consecutive_ignored: int = 0
    working_memory_recent: list[str] = field(default_factory=list)


class MaterialProvider:
    """Composer gọi `get(category, ctx)`. Trả dict material hoặc None."""

    def __init__(
        self,
        share_thought_pool: RoundRobinPool,
        question_pools: dict[str, RoundRobinPool],
        # follow_up cần ít nhất N entry memory mới enable (spec 2.4)
        follow_up_min_memory: int = 1,
        # A2: band số→lời tự nhiên (cấm số thô). None → default.
        silence_bands: list[dict] | None = None,
        chat_activity_bands: list[dict] | None = None,
        ignored_bands: list[dict] | None = None,
    ) -> None:
        self._share_thought = share_thought_pool
        self._question_pools = dict(question_pools)
        self._follow_up_min = max(1, follow_up_min_memory)
        self._silence_bands = silence_bands or _DEFAULT_SILENCE_BANDS
        self._chat_bands = chat_activity_bands or _DEFAULT_CHAT_BANDS
        self._ignored_bands = ignored_bands or _DEFAULT_IGNORED_BANDS

    @classmethod
    def from_loader(cls, loader, rng: random.Random | None = None) -> "MaterialProvider":
        pol = loader.get("autonomy_content_pool", "pool_policy", {}) or {}
        no_repeat = int(pol.get("no_repeat_last_n", 8))
        reshuffle = bool(pol.get("reshuffle_when_exhausted", True))

        share_items = loader.get("autonomy_content_pool", "share_thought_pool", []) or []
        share_pool = RoundRobinPool(share_items, no_repeat, reshuffle, rng)

        q_raw = loader.get("autonomy_content_pool", "question_pool", {}) or {}
        question_pools: dict[str, RoundRobinPool] = {}
        for kind, items in q_raw.items():
            if items:
                question_pools[str(kind)] = RoundRobinPool(items, no_repeat, reshuffle, rng)

        sl = loader.get("autonomy_content_pool", "slot_language", {}) or {}
        return cls(
            share_thought_pool=share_pool,
            question_pools=question_pools,
            silence_bands=sl.get("silence_bands"),
            chat_activity_bands=sl.get("chat_activity_bands"),
            ignored_bands=sl.get("ignored_bands"),
        )

    def get(self, category: str, ctx: RuntimeContext) -> dict[str, Any] | None:
        """Dispatch per category. Trả None nếu category không có material (composer skip)."""
        if category == "complain_silence":
            # A2: trả LỜI TỰ NHIÊN, không số thô (cấm "im 90s, chat 2 tin").
            return {
                "silence_phrase": _band_phrase(ctx.silence_seconds, self._silence_bands),
                "chat_phrase": _band_phrase(ctx.chat_count_last_10min, self._chat_bands),
            }

        if category == "share_thought":
            topic = self._share_thought.next()
            if topic is None:
                return None
            return {"topic_seed": topic}

        if category == "ask_chat":
            # Rotate qua các sub-pool (opinion/personal) — pick 1 kind random rồi seed
            if not self._question_pools:
                return None
            kinds = list(self._question_pools.keys())
            # Simple deterministic dispatch: use hash of category name + len(kinds)
            # để không phụ thuộc rng external
            kind = kinds[0]  # đơn giản MVP: dùng kind đầu tiên; muốn round-robin thì thêm state
            q = self._question_pools[kind].next()
            if q is None:
                return None
            return {"question_seed": q, "question_kind": kind}

        if category == "call_operator":
            # A2: ignored_streak → lời tự nhiên, không số thô.
            return {
                "operator_online": bool(ctx.operator_online),
                "ignored_phrase": _band_phrase(ctx.consecutive_ignored, self._ignored_bands),
            }

        if category == "follow_up_topic":
            recent = ctx.working_memory_recent
            if len(recent) < self._follow_up_min:
                return None
            snippet = " | ".join(recent[-2:])
            return {"memory_snippet": snippet}

        if category == "roast_chat":
            # Cần ít nhất 1 chat gần đây để cà khịa
            recent = ctx.working_memory_recent
            if not recent:
                return None
            # Pick chat gần nhất (nếu list dài, dùng element cuối)
            target = recent[-1]
            return {"target_chat": target[:200]}   # cap length tránh prompt bloat

        return None
