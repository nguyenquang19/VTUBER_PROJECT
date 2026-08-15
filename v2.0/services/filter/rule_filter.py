"""RuleFilter — lọc output Mai bằng regex (ARCHITECTURE 8.3A, Phase 3 3.A).

Lọc câu Mai SẮP nói theo ranh giới persona Phần C + explicit/harmful. Pattern từ
`config/filters.yaml` (N6). AI filter bỏ (E4B removed) → rule-only.

N7 fail-open: `check()` KHÔNG BAO GIỜ raise — lỗi nội bộ → trả `FilterVerdict.fail_open`
(cho qua + log). Pattern lỗi lúc compile → bỏ pattern đó + log, không chết filter.

PERSONA_BREAK KHÔNG bắt "là AI" trần (persona C.3 bắt buộc Mai thừa nhận là AI) —
chỉ bắt hedge robot + lộ system prompt.
"""
from __future__ import annotations

import re
import time
from typing import Any

from interfaces.base import HealthStatus
from interfaces.filter import FilterCategory, FilterService, FilterVerdict
from orchestrator.logger import get_logger

_SEV_ORDER = {"low": 0, "medium": 1, "high": 2}
_ACTION_ORDER = {"allow": 0, "regenerate": 1, "replace": 2, "block": 3}


class RuleFilter(FilterService):
    service_id = "filter"

    def __init__(
        self,
        patterns: dict[str, list[str]],
        severity: dict[str, str],
        action: dict[str, str],
        identity_guard: dict[str, Any] | None = None,
        event_bus: Any = None,
    ) -> None:
        self._log = get_logger("filter")
        self._severity = dict(severity or {})
        self._action = dict(action or {})
        self._event_bus = event_bus
        guard = dict(identity_guard or {})
        self._foreign_names = tuple(
            str(value).casefold() for value in guard.get("foreign_names", ())
            if str(value).strip()
        )
        self._first_person_patterns = tuple(
            re.compile(str(value), re.IGNORECASE)
            for value in guard.get("first_person_patterns", ())
            if str(value).strip()
        )
        self._knowledge_request_patterns = tuple(
            re.compile(str(value), re.IGNORECASE)
            for value in guard.get("knowledge_request_patterns", ())
            if str(value).strip()
        )
        self._uncertainty_patterns = tuple(
            re.compile(str(value), re.IGNORECASE)
            for value in guard.get("uncertainty_patterns", ())
            if str(value).strip()
        )
        self._require_name_in_response = bool(
            guard.get("require_name_in_response", False)
        )

        # Compile per category — bỏ pattern lỗi + category lạ (fail-safe).
        self._compiled: dict[FilterCategory, list[re.Pattern[str]]] = {}
        for cat_str, pats in (patterns or {}).items():
            try:
                cat = FilterCategory(cat_str)
            except ValueError:
                self._log.warning("filter_unknown_category", category=cat_str)
                continue
            compiled: list[re.Pattern[str]] = []
            for p in pats or []:
                try:
                    compiled.append(re.compile(p, re.IGNORECASE))
                except re.error as e:
                    self._log.warning("filter_bad_pattern", category=cat_str, pattern=p, error=str(e))
            self._compiled[cat] = compiled

        self._checks_total = 0
        self._hits_total = 0
        self._fail_open_total = 0
        self._by_category: dict[str, int] = {}

    @classmethod
    def from_config(cls, loader, event_bus: Any = None) -> "RuleFilter":
        f = loader.section("filters").get("filter", {})
        return cls(
            patterns=f.get("patterns", {}),
            severity=f.get("severity", {}),
            action=f.get("action", {}),
            identity_guard=f.get("identity_guard", {}),
            event_bus=event_bus,
        )

    # ---------- Service ----------

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def health_check(self) -> HealthStatus:
        return HealthStatus.healthy(self.service_id, patterns=sum(len(v) for v in self._compiled.values()))

    def get_metrics(self) -> dict[str, Any]:
        return {
            "filter_checks_total": self._checks_total,
            "filter_hits_total": self._hits_total,
            "filter_fail_open_total": self._fail_open_total,
            "filter_by_category": dict(self._by_category),
        }

    # ---------- FilterService ----------

    async def check(self, text: str, context: dict[str, Any] | None = None) -> FilterVerdict:
        t0 = time.perf_counter()
        self._checks_total += 1
        try:
            hits = [
                cat for cat, pats in self._compiled.items()
                if any(p.search(text) for p in pats)
            ]
            if self._foreign_identity_confusion(text, context):
                hits.append(FilterCategory.PERSONA_BREAK)
            hits = list(dict.fromkeys(hits))
            elapsed = int((time.perf_counter() - t0) * 1000)
            if not hits:
                return FilterVerdict.allow(latency_ms=elapsed)

            self._hits_total += 1
            for c in hits:
                self._by_category[c.value] = self._by_category.get(c.value, 0) + 1

            severity = max(
                (self._severity.get(c.value, "medium") for c in hits),
                key=lambda s: _SEV_ORDER.get(s, 1),
            )
            action = max(
                (self._action.get(c.value, "regenerate") for c in hits),
                key=lambda a: _ACTION_ORDER.get(a, 1),
            )
            verdict = FilterVerdict(
                passed=False,
                categories_hit=hits,
                severity=severity,
                suggested_action=action,
                reason=f"Detected: {[c.value for c in hits]}",
                latency_ms=elapsed,
            )
            self._log.info(
                "filter_hit",
                categories=[c.value for c in hits],
                severity=severity,
                action=action,
            )
            if self._event_bus is not None:
                self._event_bus.publish(
                    "filter_hit", {"categories": [c.value for c in hits], "action": action}
                )
            return verdict
        except Exception as e:  # N7 fail-open — không bao giờ block do lỗi filter
            self._fail_open_total += 1
            self._log.warning("filter_fail_open", error=str(e))
            return FilterVerdict.fail_open(str(e), latency_ms=int((time.perf_counter() - t0) * 1000))

    def _foreign_identity_confusion(
        self, text: str, context: dict[str, Any] | None,
    ) -> bool:
        if not self._foreign_names or not context:
            return False
        messages = context.get("messages") or ()
        direct_user_text = next(
            (
                str(message.get("content") or "")
                for message in reversed(messages)
                if str(message.get("role") or "") == "user"
            ),
            "",
        )
        context_text = direct_user_text or next(
            (
                str(message.get("content") or "")
                for message in reversed(messages)
                if str(message.get("content") or "").strip()
            ),
            "",
        )
        folded_user = context_text.casefold()
        name = next(
            (value for value in self._foreign_names if value in folded_user), None,
        )
        if name is None:
            return False
        folded_response = text.casefold()
        if any(pattern.search(text) for pattern in self._first_person_patterns):
            return True
        if self._require_name_in_response and name not in folded_response:
            return True
        # A system-only directed continuation may quote an old question. It is not
        # itself a direct request, so apply identity-takeover checks but do not force
        # every neutral continuation to repeat an uncertainty disclaimer.
        if not direct_user_text:
            return False
        asks_unknown_fact = any(
            pattern.search(direct_user_text)
            for pattern in self._knowledge_request_patterns
        )
        states_uncertainty = any(
            pattern.search(text) for pattern in self._uncertainty_patterns
        )
        return asks_unknown_fact and not states_uncertainty
