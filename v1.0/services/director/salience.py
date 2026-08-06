"""SaliencePool — chấm điểm + decay + cluster chat (C0.1, ROADMAP §C0.1).

Thay lock FIFO "đáp mọi tin" bằng POOL có điểm: chat tới → score → vào pool
(KHÔNG tự thành turn). Director khi chọn read_chat mới nhặt top từ pool.

Công thức (config chat_salience.yaml):
    base   = base_tier[kind] + superchat_coef * log1p(amount / divisor)
    score  = (base + cluster_coef * log1p(cluster_count - 1)) * exp(-age / tau)

Tin near-duplicate (Jaccard token > threshold) gom vào 1 đại diện (cluster_count++)
— 20 người hỏi cùng = 1 turn gộp, không đáp lẻ 20 lần. Pool cap pool_max, tin
score < floor bị evict (staleness + backpressure tự giải quyết).

MVP: base_tier + amount + decay + cluster. rel_bonus (regular/troll) để C1.
KHÔNG ML ranker, KHÔNG LLM chấm mỗi tin (chậm + tốn).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from services.autonomy.dedup import _tokenize  # tái dùng Jaccard token (N1)


@dataclass
class PooledMessage:
    msg_id: str
    viewer_id: str | None
    text: str
    kind: str                    # chat | question | mention
    base_score: float            # base_tier + superchat (KHÔNG gồm decay/cluster)
    created_at: float            # epoch seconds (giữ để tính age)
    amount_vnd: int = 0
    is_super: bool = False
    cluster_count: int = 1
    _tokens: set[str] = field(default_factory=set, repr=False)


class SaliencePool:
    def __init__(
        self,
        base_tier: dict[str, float],
        superchat_coef: float = 40.0,
        superchat_divisor: float = 1000.0,
        tau_seconds: float = 50.0,
        dedup_threshold: float = 0.6,
        cluster_coef: float = 5.0,
        pool_max: int = 50,
        floor: float = 3.0,
    ) -> None:
        self._base_tier = {k: float(v) for k, v in (base_tier or {}).items()}
        self._sc_coef = float(superchat_coef)
        self._sc_div = max(1.0, float(superchat_divisor))
        self._tau = max(1e-6, float(tau_seconds))
        self._dedup_thr = float(dedup_threshold)
        self._cluster_coef = float(cluster_coef)
        self._pool_max = max(1, int(pool_max))
        self._floor = float(floor)

        self._items: dict[str, PooledMessage] = {}
        self._added = 0
        self._clustered = 0
        self._evicted = 0

    @classmethod
    def from_loader(cls, loader) -> "SaliencePool":
        s = loader.get("chat_salience", "salience", {}) or {}
        return cls(
            base_tier=s.get("base_tier", {"chat": 10, "question": 25, "mention": 35}),
            superchat_coef=float(s.get("superchat_coef", 40.0)),
            superchat_divisor=float(s.get("superchat_divisor", 1000.0)),
            tau_seconds=float(s.get("tau_seconds", 50.0)),
            dedup_threshold=float(s.get("dedup_threshold", 0.6)),
            cluster_coef=float(s.get("cluster_coef", 5.0)),
            pool_max=int(s.get("pool_max", 50)),
            floor=float(s.get("floor", 3.0)),
        )

    # ---------- scoring ----------

    def _base_for(self, kind: str, amount_vnd: int, is_super: bool) -> float:
        base = self._base_tier.get(kind, self._base_tier.get("chat", 10.0))
        if is_super and amount_vnd > 0:
            base += self._sc_coef * math.log1p(amount_vnd / self._sc_div)
        return base

    def current_score(self, m: PooledMessage, now: float) -> float:
        """Điểm hiện tại = (base + cluster_bonus) * decay."""
        cluster_bonus = self._cluster_coef * math.log1p(max(0, m.cluster_count - 1))
        age = max(0.0, now - m.created_at)
        return (m.base_score + cluster_bonus) * math.exp(-age / self._tau)

    # ---------- mutation ----------

    def add(
        self,
        msg_id: str,
        text: str,
        now: float,
        kind: str = "chat",
        viewer_id: str | None = None,
        amount_vnd: int = 0,
        is_super: bool = False,
    ) -> PooledMessage:
        """Thêm 1 tin vào pool. Nếu near-duplicate tin có sẵn → gom cụm (đại diện
        giữ, cluster_count++). Trả entry đại diện (mới hoặc đã gom)."""
        self._added += 1
        tokens = _tokenize(text)

        # Cluster: gom vào đại diện gần nhất (Jaccard > threshold)
        if tokens:
            for rep in self._items.values():
                if rep._tokens and _jaccard(tokens, rep._tokens) > self._dedup_thr:
                    rep.cluster_count += 1
                    self._clustered += 1
                    # đại diện giữ nguyên text/created_at (tin đầu), chỉ tăng count.
                    # Nếu tin mới là superchat mà đại diện không → nâng base (ưu tiên tiền).
                    if is_super and not rep.is_super:
                        rep.is_super = True
                        rep.amount_vnd = amount_vnd
                        rep.base_score = self._base_for(rep.kind, amount_vnd, True)
                    return rep

        m = PooledMessage(
            msg_id=msg_id, viewer_id=viewer_id, text=text, kind=kind,
            base_score=self._base_for(kind, amount_vnd, is_super),
            created_at=now, amount_vnd=amount_vnd, is_super=is_super,
            _tokens=tokens,
        )
        self._items[msg_id] = m
        self._enforce_cap(now)
        return m

    def _enforce_cap(self, now: float) -> None:
        """Quá pool_max → evict tin current_score thấp nhất."""
        while len(self._items) > self._pool_max:
            worst_id = min(self._items, key=lambda k: self.current_score(self._items[k], now))
            del self._items[worst_id]
            self._evicted += 1

    def evict_stale(self, now: float) -> int:
        """Bỏ tin đã decay dưới floor. Trả số tin evict."""
        stale = [k for k, m in self._items.items() if self.current_score(m, now) < self._floor]
        for k in stale:
            del self._items[k]
        self._evicted += len(stale)
        return len(stale)

    # ---------- read ----------

    def peek_top(self, now: float) -> PooledMessage | None:
        """Tin điểm cao nhất còn trên floor. None nếu pool rỗng/toàn dưới floor."""
        best: PooledMessage | None = None
        best_score = self._floor
        for m in self._items.values():
            s = self.current_score(m, now)
            if s >= best_score:
                best, best_score = m, s
        return best

    def pop_top(self, now: float) -> PooledMessage | None:
        """peek_top + gỡ khỏi pool (Director đã quyết đáp tin này)."""
        top = self.peek_top(now)
        if top is not None:
            self._items.pop(top.msg_id, None)
        return top

    def top_cluster(self, now: float, max_refs: int = 3) -> list[PooledMessage]:
        """Tin top + các tin điểm cao kế (để gộp ref ≤ max_refs). C0.2 dùng."""
        ranked = sorted(
            (m for m in self._items.values() if self.current_score(m, now) >= self._floor),
            key=lambda m: self.current_score(m, now), reverse=True,
        )
        return ranked[:max(1, max_refs)]

    def remove(self, msg_id: str) -> bool:
        """Gỡ 1 tin khỏi pool (Director đã đáp). Trả True nếu có gỡ."""
        return self._items.pop(msg_id, None) is not None

    def size(self) -> int:
        return len(self._items)

    def get_metrics(self) -> dict[str, Any]:
        return {
            "salience_pool_size": len(self._items),
            "salience_added": self._added,
            "salience_clustered": self._clustered,
            "salience_evicted": self._evicted,
        }


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    union = a | b
    return len(a & b) / len(union) if union else 0.0
