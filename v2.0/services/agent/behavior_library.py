"""Structured host behavior library with applicability and hard safety guards (M5.3)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from interfaces.agent import BehaviorLibraryService
from interfaces.base import HealthStatus
from interfaces.state import BehaviorDecision, BehaviorKind


@dataclass(frozen=True)
class BehaviorSpec:
    kind: BehaviorKind
    directive: str
    actions: tuple[str, ...]
    forbidden_flags: tuple[str, ...]


@dataclass(frozen=True)
class BehaviorLibraryConfig:
    directive_max_chars: int
    tease_mood_floor: int
    safety_overrides: dict[str, BehaviorKind]
    action_defaults: dict[str, BehaviorKind]
    behaviors: dict[BehaviorKind, BehaviorSpec]

    @classmethod
    def from_loader(cls, loader: Any) -> "BehaviorLibraryConfig":
        raw = loader.get("hosting", "behavior_library", {}) or {}
        specs: dict[BehaviorKind, BehaviorSpec] = {}
        for name, item in dict(raw.get("behaviors", {}) or {}).items():
            kind = BehaviorKind(str(name))
            applicability = dict(item.get("applicability", {}) or {})
            guard = dict(item.get("safety_guard", {}) or {})
            specs[kind] = BehaviorSpec(
                kind,
                " ".join(str(item.get("directive") or "").split()),
                tuple(str(value) for value in applicability.get("actions", [])),
                tuple(str(value) for value in guard.get("forbidden_flags", [])),
            )
        missing = set(BehaviorKind) - set(specs)
        if missing:
            raise ValueError(f"behavior library missing: {sorted(item.value for item in missing)}")
        value = cls(
            directive_max_chars=int(raw.get("directive_max_chars", 240)),
            tease_mood_floor=int(raw.get("tease_mood_floor", 7)),
            safety_overrides={
                str(flag): BehaviorKind(str(kind))
                for flag, kind in dict(raw.get("safety_overrides", {}) or {}).items()
            },
            action_defaults={
                str(action): BehaviorKind(str(kind))
                for action, kind in dict(raw.get("action_defaults", {}) or {}).items()
            },
            behaviors=specs,
        )
        if value.directive_max_chars <= 0 or not 0 <= value.tease_mood_floor <= 10:
            raise ValueError("invalid behavior library limits")
        return value


class BehaviorLibrary(BehaviorLibraryService):
    service_id = "behavior_library"

    def __init__(
        self, config: BehaviorLibraryConfig, *, metrics: Any = None, enabled: bool = True,
    ) -> None:
        self.config = config
        self._metrics = metrics
        self.enabled = bool(enabled)
        self._running = False
        self._counts: dict[str, int] = {}

    @classmethod
    def from_loader(
        cls, loader: Any, *, metrics: Any = None, enabled: bool | None = None,
    ) -> "BehaviorLibrary":
        configured = bool(loader.get("hosting", "behavior_library.enabled", True))
        return cls(
            BehaviorLibraryConfig.from_loader(loader), metrics=metrics,
            enabled=configured if enabled is None else enabled,
        )

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        return HealthStatus.healthy(self.service_id, selections=sum(self._counts.values()))

    def get_metrics(self) -> dict[str, Any]:
        return {f"host_behavior_{key}_total": value for key, value in sorted(self._counts.items())}

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)

    def select(
        self,
        action: str,
        mood: object,
        tone_flags: set[str] | tuple[str, ...] = (),
        *,
        proactive_source: str | None = None,
        repair_kind: str | None = None,
    ) -> BehaviorDecision:
        if not self.enabled:
            return BehaviorDecision(BehaviorKind.CURIOUS, "", "disabled", False)
        flags = set(tone_flags)
        for flag in sorted(flags):
            override = self.config.safety_overrides.get(flag)
            if override is not None:
                return self._decision(override, action, flags, f"safety_override:{flag}")
        if repair_kind:
            return self._decision(BehaviorKind.REPAIR, action, flags, f"repair:{repair_kind}")
        if action == "read_chat" and int(getattr(mood, "buc", 0)) >= self.config.tease_mood_floor:
            tease = self._decision(BehaviorKind.TEASE, action, flags, "strong_buc")
            if tease.applicable:
                return tease
        selected = self.config.action_defaults.get(action, BehaviorKind.CURIOUS)
        reason = f"action_default:{action}"
        if proactive_source:
            reason = f"proactive:{proactive_source}"
        return self._decision(selected, action, flags, reason)

    def _decision(
        self, kind: BehaviorKind, action: str, flags: set[str], reason: str,
    ) -> BehaviorDecision:
        spec = self.config.behaviors[kind]
        applicable = ("*" in spec.actions or action in spec.actions) and not (
            flags & set(spec.forbidden_flags)
        )
        if not applicable and kind is not BehaviorKind.ACKNOWLEDGE:
            return self._decision(BehaviorKind.ACKNOWLEDGE, action, flags, "guard_fallback")
        directive = spec.directive[: self.config.directive_max_chars] if applicable else ""
        decision = BehaviorDecision(kind, directive, reason, applicable)
        self._counts[kind.value] = self._counts.get(kind.value, 0) + 1
        if self._metrics is not None and hasattr(self._metrics, "record_host_behavior"):
            try:
                self._metrics.record_host_behavior(kind.value, reason)
            except Exception:
                pass
        return decision
