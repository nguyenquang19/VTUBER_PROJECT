"""Test CannedResponder — Level 2 LLM fallback (ARCHITECTURE 8.7.1, 1.E)."""
from __future__ import annotations

import random
from pathlib import Path

from interfaces.animation import MoodState
from services.llm.canned_response import CannedResponder

REPO_ROOT = Path(__file__).resolve().parents[2]

RESPONSES = {
    "default": ["Ừ.", "Hả?"],
    "vui": ["Hehe."],
    "buc": ["Hừ."],
}


def responder(**kw) -> CannedResponder:
    return CannedResponder(RESPONSES, rng=random.Random(0), **kw)


class TestPick:
    def test_default_when_no_mood(self) -> None:
        assert responder().pick() in RESPONSES["default"]

    def test_picks_by_dominant_mood(self) -> None:
        r = responder()
        r.update_mood(MoodState(vui=9))
        assert r.pick() == "Hehe."

    def test_buc_mood(self) -> None:
        r = responder()
        r.update_mood(MoodState(buc=8))
        assert r.pick() == "Hừ."

    def test_neutral_falls_to_default(self) -> None:
        r = responder()
        r.update_mood(MoodState())  # tất cả 0 → dominant "neutral" → không có key → default
        assert r.pick() in RESPONSES["default"]

    def test_mood_without_pool_falls_to_default(self) -> None:
        r = responder()
        r.update_mood(MoodState(nguong=7))  # không có "nguong" trong RESPONSES → default
        assert r.pick() in RESPONSES["default"]

    def test_empty_responses_uses_fallback(self) -> None:
        r = CannedResponder({}, rng=random.Random(0))
        assert r.pick() == "..."


class TestBuild:
    def test_build_parsedresponse(self) -> None:
        r = responder()
        r.update_mood(MoodState(vui=9))
        pr = r.build()
        assert pr.text == "Hehe."
        assert pr.ok is False
        assert pr.raw == "<canned>"
        assert pr.mood.vui == 9

    def test_build_no_mood_neutral(self) -> None:
        pr = responder().build()
        assert pr.mood.dominant() == "neutral"
        assert pr.text in RESPONSES["default"]


class TestFromLoader:
    def test_reads_config(self) -> None:
        from orchestrator.config_loader import ConfigLoader

        loader = ConfigLoader(REPO_ROOT / "config")
        loader.load_all()
        r = CannedResponder.from_loader(loader, rng=random.Random(0))
        r.update_mood(MoodState(buc=9))
        # config thật có key "buc"
        assert isinstance(r.pick(), str) and len(r.pick()) > 0
