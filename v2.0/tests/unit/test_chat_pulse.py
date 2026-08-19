"""Test C0.2 — ChatPulse (docs/MAI_V2_SYSTEM_SPEC.md).

DoD:
- burst emote (nhiều tin, ít người) → HYPE_SPAM
- chat nguội 90s → COLD (Director tự nói/đổi segment)
"""
from __future__ import annotations

from pathlib import Path

from services.director.chat_pulse import ChatPulse, PulseState

REPO_ROOT = Path(__file__).resolve().parents[2]


def _pulse(**over) -> ChatPulse:
    kw = dict(
        window_seconds=60.0, tempo_low_per_min=2.0, tempo_high_per_min=15.0,
        diversity_threshold=0.4, cold_silence_seconds=90.0,
    )
    kw.update(over)
    return ChatPulse(**kw)


class TestState:
    def test_burst_emote_is_hype_spam(self) -> None:
        # DoD: 30 tin từ 2 user trong 5s → tempo cao, diversity thấp → HYPE_SPAM
        p = _pulse()
        for i in range(30):
            p.record(now=100.0 + i * 0.1, user_id=f"u{i % 2}")
        st = p.state(now=105.0)
        assert st == PulseState.HYPE_SPAM
        assert p.diversity(now=105.0) < 0.4

    def test_many_users_is_lively(self) -> None:
        # tempo cao + diversity cao → LIVELY (bàn luận thật)
        p = _pulse()
        for i in range(30):
            p.record(now=100.0 + i * 0.1, user_id=f"user_{i}")  # 30 người khác nhau
        st = p.state(now=105.0)
        assert st == PulseState.LIVELY
        assert p.diversity(now=105.0) > 0.4

    def test_cold_when_no_recent_messages(self) -> None:
        # DoD: nguội 90s → COLD
        p = _pulse()
        p.record(now=0.0, user_id="a")
        assert p.state(now=95.0) == PulseState.COLD
        assert p.is_cold(now=95.0)

    def test_cold_when_low_tempo(self) -> None:
        p = _pulse()
        # 1 tin trong window → tempo 1/phút < low 2 → COLD
        p.record(now=100.0, user_id="a")
        assert p.state(now=110.0) == PulseState.COLD

    def test_normal_mid_tempo(self) -> None:
        p = _pulse(tempo_low_per_min=2.0, tempo_high_per_min=15.0)
        # 8 tin/phút, nằm giữa → NORMAL
        for i in range(8):
            p.record(now=100.0 + i * 1.0, user_id=f"u{i}")
        assert p.state(now=108.0) == PulseState.NORMAL


class TestMetrics:
    def test_tempo_rolling_window_prunes(self) -> None:
        p = _pulse(window_seconds=60.0)
        for i in range(10):
            p.record(now=0.0 + i, user_id="a")
        # sau 200s, mọi tin đã ra khỏi window 60s
        assert p.tempo(now=200.0) == 0.0

    def test_diversity_all_same_user_low(self) -> None:
        p = _pulse()
        for i in range(10):
            p.record(now=100.0 + i * 0.1, user_id="same")
        assert p.diversity(now=101.0) == 0.1  # 1 unique / 10

    def test_diversity_empty_is_one(self) -> None:
        p = _pulse()
        assert p.diversity(now=0.0) == 1.0

    def test_anonymous_msgs_not_treated_as_spam(self) -> None:
        # tin user None (ẩn danh) → mỗi tin coi 1 người, diversity không tụt giả
        p = _pulse()
        for i in range(10):
            p.record(now=100.0 + i * 0.1, user_id=None)
        assert p.diversity(now=101.0) == 1.0

    def test_seconds_since_last_inf_when_empty(self) -> None:
        p = _pulse()
        assert p.seconds_since_last(now=0.0) == float("inf")

    def test_accel_default_one_without_baseline(self) -> None:
        p = _pulse()
        p.record(now=100.0, user_id="a")
        assert p.accel(now=100.0) == 1.0

    def test_snapshot_has_state(self) -> None:
        p = _pulse()
        for i in range(30):
            p.record(now=100.0 + i * 0.1, user_id=f"u{i % 2}")
        snap = p.snapshot(now=105.0)
        assert snap["pulse_state"] == "hype_spam"
        assert snap["pulse_tempo_per_min"] > 0


class TestFromLoader:
    def test_from_loader(self) -> None:
        from orchestrator.config_loader import ConfigLoader
        loader = ConfigLoader(REPO_ROOT / "config")
        loader.load_all()
        p = ChatPulse.from_loader(loader)
        for i in range(40):
            p.record(now=100.0 + i * 0.1, user_id=f"u{i % 2}")
        assert p.state(now=104.0) == PulseState.HYPE_SPAM
