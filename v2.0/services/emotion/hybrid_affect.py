"""Compose legacy Mood v1 tone with Mood v2 per-turn response policy."""
from __future__ import annotations

from typing import Any

from interfaces.affect import (
    AffectComposer,
    AffectResponseMode,
    AffectStyle,
    ResponsePlan,
    TurnAffect,
)


class HybridAffectComposer(AffectComposer):
    """Pure, config-driven composer that emits one inspectable response plan."""

    def __init__(
        self,
        *,
        routes: dict[str, dict[str, Any]],
        default_tone_source: str,
        default_max_sentences: int,
        mood_style: Any = None,
    ) -> None:
        if default_tone_source not in {"turn", "legacy"}:
            raise ValueError("hybrid default tone source must be turn or legacy")
        if not 1 <= int(default_max_sentences) <= 3:
            raise ValueError("hybrid default max sentences must be within [1, 3]")
        self._routes = {str(key): dict(value or {}) for key, value in routes.items()}
        self._default_tone_source = default_tone_source
        self._default_max_sentences = int(default_max_sentences)
        self._mood_style = mood_style
        self._compositions = 0
        self._legacy_tone_uses = 0
        self._legacy_tone_fallbacks = 0
        self._last_plan: ResponsePlan | None = None
        self._validate_routes()

    @classmethod
    def from_loader(cls, loader: Any, *, mood_style: Any = None) -> "HybridAffectComposer":
        config = loader.get("affect_v2", "hybrid", {}) or {}
        return cls(
            routes=config.get("routes", {}) or {},
            default_tone_source=str(config.get("default_tone_source", "turn")),
            default_max_sentences=int(config.get("default_max_sentences", 2)),
            mood_style=mood_style,
        )

    def compose(
        self,
        category: str,
        affect: TurnAffect | None,
        legacy_mood: Any,
        tone_flags: set[str] | tuple[str, ...] = (),
    ) -> ResponsePlan | None:
        if affect is None:
            return None
        route = self._routes.get(str(category), {})
        mode = AffectResponseMode(str(route.get("mode", affect.response_mode.value)))
        tone_source = str(route.get("tone_source", self._default_tone_source))
        max_sentences = int(route.get("max_sentences", self._default_max_sentences))
        tone_directive: str | None = None
        if tone_source == "legacy":
            if self._mood_style is not None:
                try:
                    tone_directive = self._mood_style.directive_for(
                        legacy_mood, set(tone_flags),
                    )
                except Exception:
                    tone_directive = None
            if tone_directive:
                self._legacy_tone_uses += 1
            else:
                self._legacy_tone_fallbacks += 1

        style = affect.style
        if tone_source == "legacy" and not tone_directive and "fallback_style" in route:
            style = AffectStyle(str(route["fallback_style"]))

        plan = ResponsePlan(
            category=str(category or "default"),
            style=style,
            response_mode=mode,
            energy=affect.energy,
            warmth=affect.warmth,
            urgency=affect.urgency,
            tone_source="legacy" if tone_source == "legacy" else "turn",
            tone_directive=tone_directive,
            max_sentences=max_sentences,
        )
        self._compositions += 1
        self._last_plan = plan
        return plan

    def get_metrics(self) -> dict[str, Any]:
        return {
            "hybrid_affect_compositions_total": self._compositions,
            "hybrid_affect_legacy_tone_uses_total": self._legacy_tone_uses,
            "hybrid_affect_legacy_tone_fallbacks_total": self._legacy_tone_fallbacks,
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            **self.get_metrics(),
            "last_plan": self._last_plan.model_dump(mode="json") if self._last_plan else None,
        }

    def _validate_routes(self) -> None:
        for category, route in self._routes.items():
            tone_source = str(route.get("tone_source", self._default_tone_source))
            if tone_source not in {"turn", "legacy"}:
                raise ValueError(f"hybrid route {category} has invalid tone source")
            max_sentences = int(route.get("max_sentences", self._default_max_sentences))
            if not 1 <= max_sentences <= 3:
                raise ValueError(f"hybrid route {category} max sentences must be within [1, 3]")
            if "mode" in route:
                AffectResponseMode(str(route["mode"]))
            if "fallback_style" in route:
                AffectStyle(str(route["fallback_style"]))
