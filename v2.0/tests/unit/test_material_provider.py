"""Test MaterialProvider — Aut.B (5 category material builder)."""
from __future__ import annotations

import random
from pathlib import Path

import pytest

from services.autonomy.material_provider import MaterialProvider, RuntimeContext
from services.autonomy.pools import RoundRobinPool

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def mp() -> MaterialProvider:
    return MaterialProvider(
        share_thought_pool=RoundRobinPool(
            ["chuyện a", "chuyện b", "chuyện c"], no_repeat_last_n=1,
            rng=random.Random(0),
        ),
        question_pools={
            "opinion": RoundRobinPool(["q1", "q2"], no_repeat_last_n=1,
                                      rng=random.Random(0)),
        },
        follow_up_min_memory=1,
    )


def ctx(**over) -> RuntimeContext:
    kw = dict(
        silence_seconds=45.0, chat_count_last_10min=3,
        operator_online=True, consecutive_ignored=0,
        working_memory_recent=[],
    )
    kw.update(over)
    return RuntimeContext(**kw)


class TestComplainSilence:
    def test_returns_natural_phrases_not_raw_numbers(self, mp: MaterialProvider) -> None:
        # A2: trả LỜI TỰ NHIÊN, không số thô.
        m = mp.get("complain_silence", ctx(silence_seconds=90.5, chat_count_last_10min=2))
        assert set(m.keys()) == {"silence_phrase", "chat_phrase"}
        # phrase non-empty, không chứa chữ số
        assert m["silence_phrase"] and not any(c.isdigit() for c in m["silence_phrase"])
        assert m["chat_phrase"] and not any(c.isdigit() for c in m["chat_phrase"])
        # 90s im lặng → band cuối "hơi lâu"; 2 tin → band "lác đác"
        assert "lâu" in m["silence_phrase"]
        assert "lác đác" in m["chat_phrase"]

    def test_never_none(self, mp: MaterialProvider) -> None:
        """Silence + chat count luôn có (từ ctx), không trả None."""
        m = mp.get("complain_silence", ctx())
        assert m is not None


class TestShareThought:
    def test_returns_topic_seed(self, mp: MaterialProvider) -> None:
        m = mp.get("share_thought", ctx())
        assert m is not None
        assert m["topic_seed"] in {"chuyện a", "chuyện b", "chuyện c"}

    def test_returns_none_when_pool_exhausted_no_reshuffle(self) -> None:
        mp = MaterialProvider(
            share_thought_pool=RoundRobinPool(
                ["only"], no_repeat_last_n=1,
                reshuffle_when_exhausted=False,
            ),
            question_pools={},
        )
        first = mp.get("share_thought", ctx())
        assert first == {"topic_seed": "only"}
        # Lần 2: "only" trong recent + không reshuffle → None
        assert mp.get("share_thought", ctx()) is None


class TestAskChat:
    def test_returns_question_seed(self, mp: MaterialProvider) -> None:
        m = mp.get("ask_chat", ctx())
        assert m is not None
        assert m["question_seed"] in {"q1", "q2"}
        assert m["question_kind"] == "opinion"

    def test_no_pools_returns_none(self) -> None:
        mp = MaterialProvider(
            share_thought_pool=RoundRobinPool(["x"]),
            question_pools={},
        )
        assert mp.get("ask_chat", ctx()) is None


class TestCallOperator:
    def test_returns_operator_state_with_phrase(self, mp: MaterialProvider) -> None:
        # A2: ignored → phrase, không số thô.
        m = mp.get("call_operator",
                   ctx(operator_online=False, consecutive_ignored=3))
        assert m["operator_online"] is False
        assert "ignored_phrase" in m and "ignored_streak" not in m
        assert not any(c.isdigit() for c in m["ignored_phrase"])
        assert "lơ" in m["ignored_phrase"]  # 3 > 2 → band cuối "gọi hoài mà ông lơ luôn"


class TestFollowUpTopic:
    def test_returns_snippet_when_memory_present(self, mp: MaterialProvider) -> None:
        m = mp.get("follow_up_topic",
                   ctx(working_memory_recent=["turn1", "turn2", "turn3"]))
        assert m is not None
        # Lấy 2 entry mới nhất
        assert "turn2" in m["memory_snippet"]
        assert "turn3" in m["memory_snippet"]

    def test_returns_none_when_no_memory(self, mp: MaterialProvider) -> None:
        assert mp.get("follow_up_topic", ctx(working_memory_recent=[])) is None

    def test_returns_none_when_below_min(self) -> None:
        mp = MaterialProvider(
            share_thought_pool=RoundRobinPool(["x"]),
            question_pools={},
            follow_up_min_memory=3,
        )
        assert mp.get("follow_up_topic", ctx(working_memory_recent=["a"])) is None


class TestRoastChat:
    def test_returns_target_chat(self, mp: MaterialProvider) -> None:
        m = mp.get("roast_chat",
                   ctx(working_memory_recent=["chat viewer nói gì đó vu vơ"]))
        assert m is not None
        assert m["target_chat"] == "chat viewer nói gì đó vu vơ"

    def test_returns_none_when_no_recent(self, mp: MaterialProvider) -> None:
        assert mp.get("roast_chat", ctx(working_memory_recent=[])) is None

    def test_caps_target_length(self, mp: MaterialProvider) -> None:
        long_text = "x" * 500
        m = mp.get("roast_chat", ctx(working_memory_recent=[long_text]))
        assert len(m["target_chat"]) <= 200


class TestUnknownCategory:
    def test_returns_none(self, mp: MaterialProvider) -> None:
        assert mp.get("random_cat_not_exist", ctx()) is None


class TestFromLoader:
    def test_wires_real_pools(self) -> None:
        from orchestrator.config_loader import ConfigLoader
        loader = ConfigLoader(REPO_ROOT / "config")
        loader.load_all()
        mp = MaterialProvider.from_loader(loader)

        # share_thought_pool phải có seed
        m = mp.get("share_thought", ctx())
        assert m is not None
        assert m["topic_seed"]  # non-empty

        # ask_chat có pool opinion
        m = mp.get("ask_chat", ctx())
        assert m is not None


class TestA2DoD:
    """DoD A2 (docs/03_COMPONENT_REFERENCE.md §PHASE A): no seed lặp trong 20; không đọc số liệu thô."""

    def _real_mp(self) -> MaterialProvider:
        from orchestrator.config_loader import ConfigLoader
        loader = ConfigLoader(REPO_ROOT / "config")
        loader.load_all()
        return MaterialProvider.from_loader(loader)

    def test_share_thought_no_repeat_within_20(self) -> None:
        # DoD (a): 100 lần tự nói, không seed nào lặp trong window 20.
        mp = self._real_mp()
        seeds: list[str] = []
        for _ in range(100):
            m = mp.get("share_thought", ctx())
            assert m is not None
            seeds.append(m["topic_seed"])
        # kiểm mọi cửa sổ trượt 20 phần tử không có trùng
        window = 20
        for i in range(len(seeds)):
            chunk = seeds[max(0, i - window + 1): i + 1]
            assert len(chunk) == len(set(chunk)), f"seed lặp trong window tại idx {i}"

    def test_no_raw_numbers_in_any_material(self) -> None:
        # DoD (b): material cho prompt không chứa số thô.
        mp = self._real_mp()
        for c in ("complain_silence", "call_operator"):
            m = mp.get(c, ctx(silence_seconds=200, chat_count_last_10min=50,
                              consecutive_ignored=9))
            assert m is not None
            for v in m.values():
                if isinstance(v, str):
                    assert not any(ch.isdigit() for ch in v), f"{c} còn số thô: {v}"
