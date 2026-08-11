"""Deterministic lexical topic matching for bounded conversation threads."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from interfaces.agent import TopicMatcherService
from interfaces.base import HealthStatus
from services.agent.types import OpenThread, TopicMatch

_WORD_RE = re.compile(r"\w+", re.UNICODE)
_DEFAULT_STOPWORDS = frozenset({
    "a", "ai", "and", "anh", "ban", "bi", "cai", "cho", "co", "con", "cua",
    "da", "dau", "de", "di", "do", "duoc", "em", "gi", "ha", "hay", "ho",
    "khong", "la", "lai", "lam", "mai", "mot", "nao", "nay", "ne", "nhe",
    "nhung", "noi", "oi", "roi", "sao", "se", "the", "thi", "tiep", "toi",
    "tro", "va", "vay", "ve", "voi", "you", "the", "is", "it", "to", "of",
})


@dataclass(frozen=True)
class TopicMatcherConfig:
    min_score: float = 0.34
    min_shared_terms: int = 1
    topic_weight: float = 0.65
    summary_weight: float = 0.35

    @classmethod
    def from_loader(cls, loader: Any) -> "TopicMatcherConfig":
        prefix = "topic_matcher."
        value = cls(
            min_score=float(loader.get("conversation", prefix + "min_score", 0.34)),
            min_shared_terms=int(
                loader.get("conversation", prefix + "min_shared_terms", 1)
            ),
            topic_weight=float(
                loader.get("conversation", prefix + "topic_weight", 0.65)
            ),
            summary_weight=float(
                loader.get("conversation", prefix + "summary_weight", 0.35)
            ),
        )
        if not 0.0 < value.min_score <= 1.0 or value.min_shared_terms <= 0:
            raise ValueError("topic matcher threshold must be positive")
        if abs(value.topic_weight + value.summary_weight - 1.0) > 0.0001:
            raise ValueError("topic matcher weights must sum to 1")
        return value


class LexicalTopicMatcher(TopicMatcherService):
    service_id = "topic_matcher"

    def __init__(self, config: TopicMatcherConfig, *, metrics: Any = None) -> None:
        self.config = config
        self._metrics = metrics
        self._running = False
        self._matched = 0
        self._rejected = 0

    @classmethod
    def from_loader(cls, loader: Any, *, metrics: Any = None) -> "LexicalTopicMatcher":
        return cls(TopicMatcherConfig.from_loader(loader), metrics=metrics)

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        return HealthStatus.healthy(self.service_id, matched=self._matched)

    def get_metrics(self) -> dict[str, Any]:
        return {
            "topic_matcher_matched_total": self._matched,
            "topic_matcher_rejected_total": self._rejected,
        }

    def match(self, text: str, open_threads: tuple[OpenThread, ...]) -> TopicMatch | None:
        query = _terms(text)
        if not query or not open_threads:
            self._rejected += 1
            return None
        ranked: list[tuple[float, int, OpenThread, tuple[str, ...]]] = []
        for thread in open_threads:
            topic_terms = _terms(thread.topic)
            summary_terms = _terms(thread.summary)
            shared = query & (topic_terms | summary_terms)
            if len(shared) < self.config.min_shared_terms:
                continue
            topic_score = _overlap(query, topic_terms)
            summary_score = _overlap(query, summary_terms)
            score = min(
                1.0,
                topic_score * self.config.topic_weight
                + summary_score * self.config.summary_weight,
            )
            ranked.append((score, len(shared), thread, tuple(sorted(shared))))
        if not ranked:
            self._rejected += 1
            return None
        score, _, thread, shared = max(
            ranked, key=lambda item: (item[0], item[1], item[2].updated_at, item[2].thread_id),
        )
        if score < self.config.min_score:
            self._rejected += 1
            return None
        self._matched += 1
        if self._metrics is not None and hasattr(self._metrics, "record_thread_event"):
            self._metrics.record_thread_event("topic_matched", thread.kind.value)
        return TopicMatch(thread.thread_id, score, shared)


def _terms(value: str) -> set[str]:
    normalized = unicodedata.normalize("NFKD", str(value).casefold())
    plain = "".join(char for char in normalized if not unicodedata.combining(char))
    return {
        token for token in _WORD_RE.findall(plain)
        if len(token) > 1 and token not in _DEFAULT_STOPWORDS
    }


def _overlap(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    shared = len(left & right)
    return shared / max(1, min(len(left), len(right)))
