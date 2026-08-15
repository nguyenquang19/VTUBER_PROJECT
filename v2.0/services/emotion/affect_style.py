"""Render one bounded delivery directive from Mood v2 state."""
from __future__ import annotations

from typing import Any

from interfaces.affect import ResponsePlan, SessionMood, TurnAffect


class AffectStyleRenderer:
    def __init__(
        self,
        templates: dict[str, str],
        *,
        response_templates: dict[str, str] | None = None,
        max_chars: int,
    ) -> None:
        if max_chars <= 0:
            raise ValueError("affect directive max_chars must be positive")
        self._templates = {str(k): " ".join(str(v).split()) for k, v in templates.items()}
        self._response_templates = {
            str(k): " ".join(str(v).split())
            for k, v in (response_templates or {}).items()
        }
        self.max_chars = int(max_chars)

    @classmethod
    def from_loader(cls, loader: Any) -> "AffectStyleRenderer":
        return cls(
            loader.get("affect_v2", "style_templates", {}) or {},
            response_templates=loader.get("affect_v2", "response_templates", {}) or {},
            max_chars=int(loader.get("affect_v2", "policy.directive_max_chars", 220)),
        )

    def directive_for(
        self, affect: TurnAffect | None, session: SessionMood | None = None,
    ) -> str | None:
        try:
            if affect is None:
                return None
            return self.directive_for_plan(ResponsePlan(
                style=affect.style,
                response_mode=affect.response_mode,
                energy=affect.energy,
                warmth=affect.warmth,
                urgency=affect.urgency,
            ), session)
        except Exception:
            return None

    def directive_for_plan(
        self, plan: ResponsePlan | None, session: SessionMood | None = None,
    ) -> str | None:
        try:
            if plan is None:
                return None
            style_template = self._templates.get(plan.style.value)
            response_template = self._response_templates.get(plan.response_mode.value)
            if not style_template and not response_template:
                return None
            values = {
                "energy": _band(plan.energy), "warmth": _band(plan.warmth),
                "urgency": _band(plan.urgency),
                "valence": _signed_band(session.valence if session else 0.0),
                "arousal": _signed_band(session.arousal if session else 0.0),
                "irritation": _signed_band(session.irritation if session else 0.0),
            }
            tone_template = plan.tone_directive or style_template
            parts = [
                template.format(**values)
                for template in (
                    response_template,
                    f"Tối đa {plan.max_sentences} câu." if plan.max_sentences else None,
                    tone_template,
                )
                if template
            ]
            directive = " ".join(part.removeprefix("- ") for part in parts)
            return "- " + " ".join(directive.split())[: self.max_chars]
        except Exception:
            return None


def _band(value: float) -> str:
    if value >= 0.75:
        return "cao"
    if value >= 0.4:
        return "vừa"
    return "thấp"


def _signed_band(value: float) -> str:
    if value >= 0.35:
        return "cao"
    if value <= -0.35:
        return "thấp"
    return "ổn định"
