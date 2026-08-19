"""Test A3 — ResponsePacer + FillerManager (docs/MAI_V2_SYSTEM_SPEC.md).

DoD A3:
- phân bố delay có σ>0 (không constant)
- filler không quá X lần/phút (cap) + cooldown + no-repeat
- pool rỗng → không filler (no-op)
"""
from __future__ import annotations

import random
import statistics
from pathlib import Path

from services.tts.pacing import FillerManager, ResponsePacer

REPO_ROOT = Path(__file__).resolve().parents[2]


class TestResponsePacer:
    def test_distribution_has_variance(self) -> None:
        # DoD: σ>0 — 100 mẫu cùng câu phải khác nhau.
        p = ResponsePacer(sigma_seconds=0.15, rng=random.Random(0))
        samples = [p.delay("một câu bình thường") for _ in range(100)]
        assert statistics.pstdev(samples) > 0.0

    def test_zero_sigma_is_deterministic(self) -> None:
        p = ResponsePacer(sigma_seconds=0.0)
        a = p.delay("câu")
        b = p.delay("câu")
        assert a == b

    def test_longer_text_longer_delay(self) -> None:
        p = ResponsePacer(sigma_seconds=0.0)
        short = p.delay("ừ")
        long = p.delay("x" * 200)
        assert long > short

    def test_question_gets_bonus(self) -> None:
        p = ResponsePacer(sigma_seconds=0.0, question_bonus_seconds=0.3)
        assert p.delay("thế à?") > p.delay("thế à")

    def test_clamped_within_bounds(self) -> None:
        p = ResponsePacer(
            sigma_seconds=0.5, min_seconds=0.15, max_seconds=1.4,
            rng=random.Random(1),
        )
        for _ in range(200):
            d = p.delay("x" * 500)  # dài + noise mạnh → phải bị clamp
            assert 0.15 <= d <= 1.4

    def test_disabled_returns_zero(self) -> None:
        p = ResponsePacer(enabled=False)
        assert p.delay("bất kỳ") == 0.0

    def test_from_loader(self) -> None:
        from orchestrator.config_loader import ConfigLoader
        loader = ConfigLoader(REPO_ROOT / "config")
        loader.load_all()
        p = ResponsePacer.from_loader(loader)
        assert p.enabled is True
        assert 0.0 < p.delay("câu test") <= p.max_s


class TestFillerManager:
    def _fm(self, **over) -> FillerManager:
        kw = dict(
            clips=["a.wav", "b.wav", "c.wav"],
            probability=1.0,          # luôn qua prob gate để test cap/cooldown
            frequency_cap_per_min=4,
            cooldown_seconds=6.0,
            no_repeat_last_n=2,
            rng=random.Random(0),
        )
        kw.update(over)
        return FillerManager(**kw)

    def test_empty_pool_never_fires(self) -> None:
        fm = self._fm(clips=[])
        assert all(fm.maybe_pick(t) is None for t in range(0, 100, 1))

    def test_disabled_never_fires(self) -> None:
        fm = self._fm(enabled=False)
        assert fm.maybe_pick(0.0) is None

    def test_cooldown_blocks_close_calls(self) -> None:
        fm = self._fm(cooldown_seconds=6.0)
        first = fm.maybe_pick(100.0)
        assert first is not None
        # ngay sau đó (chưa hết cooldown) → None
        assert fm.maybe_pick(103.0) is None
        # sau cooldown → được
        assert fm.maybe_pick(106.5) is not None

    def test_frequency_cap_per_minute(self) -> None:
        # cap=4, cooldown nhỏ để không chặn. Trong 60s chỉ được 4.
        fm = self._fm(frequency_cap_per_min=4, cooldown_seconds=0.0)
        fired = 0
        # rải 20 lần cách nhau 2s → 40s < 60s window
        for i in range(20):
            if fm.maybe_pick(i * 2.0) is not None:
                fired += 1
        assert fired == 4
        assert fm.suppressed_cap > 0

    def test_no_repeat_within_window(self) -> None:
        # cooldown/cap không chặn → kiểm 2 lần liên tiếp không cùng clip.
        fm = self._fm(cooldown_seconds=0.0, frequency_cap_per_min=0,
                      no_repeat_last_n=2)
        picks = []
        for i in range(30):
            p = fm.maybe_pick(i * 100.0)  # cách xa để không dính cap/cooldown
            if p:
                picks.append(p)
        # không có 2 lần liên tiếp trùng
        for i in range(1, len(picks)):
            assert picks[i] != picks[i - 1]

    def test_probability_gate_suppresses(self) -> None:
        fm = self._fm(probability=0.0)  # không bao giờ qua
        assert fm.maybe_pick(0.0) is None
        assert fm.suppressed_prob > 0

    def test_from_loader_empty_clips_noop(self) -> None:
        # config mặc định clips=[] → no-op (chưa có asset).
        from orchestrator.config_loader import ConfigLoader
        loader = ConfigLoader(REPO_ROOT / "config")
        loader.load_all()
        fm = FillerManager.from_loader(loader)
        assert fm.maybe_pick(0.0) is None  # pool rỗng
