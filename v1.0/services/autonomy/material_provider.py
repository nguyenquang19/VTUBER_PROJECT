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
    ) -> None:
        self._share_thought = share_thought_pool
        self._question_pools = dict(question_pools)
        self._follow_up_min = max(1, follow_up_min_memory)

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

        return cls(
            share_thought_pool=share_pool,
            question_pools=question_pools,
        )

    def get(self, category: str, ctx: RuntimeContext) -> dict[str, Any] | None:
        """Dispatch per category. Trả None nếu category không có material (composer skip)."""
        if category == "complain_silence":
            return {
                "silence_seconds": int(ctx.silence_seconds),
                "chat_count_10min": int(ctx.chat_count_last_10min),
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
            return {
                "operator_online": bool(ctx.operator_online),
                "ignored_streak": int(ctx.consecutive_ignored),
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
