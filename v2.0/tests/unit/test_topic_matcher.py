from __future__ import annotations

from datetime import datetime, timedelta, timezone

from services.agent.topic_matcher import LexicalTopicMatcher, TopicMatcherConfig
from interfaces.state import OpenThread

NOW = datetime(2026, 8, 12, tzinfo=timezone.utc)


def _thread(thread_id: str, topic: str, summary: str) -> OpenThread:
    return OpenThread(
        thread_id, topic, summary, NOW, NOW, NOW + timedelta(minutes=5),
    )


def test_matches_same_topic_and_exposes_shared_grounded_terms() -> None:
    matcher = LexicalTopicMatcher(TopicMatcherConfig(min_score=0.3))
    threads = (
        _thread("coffee", "cà phê rang", "đang bàn về quán cà phê"),
        _thread("game", "game kinh dị", "đang bàn về game mới"),
    )
    match = matcher.match("Quán cà phê rang đó ở đâu?", threads)
    assert match is not None
    assert match.thread_id == "coffee"
    assert {"ca", "phe", "rang"}.issubset(set(match.shared_terms))


def test_rejects_cross_topic_and_stopword_only_text() -> None:
    matcher = LexicalTopicMatcher(TopicMatcherConfig(min_score=0.3))
    threads = (_thread("coffee", "cà phê rang", "quán cà phê cũ"),)
    assert matcher.match("Game kinh dị mới nhìn sợ thật", threads) is None
    assert matcher.match("Mai ơi kể tiếp đi", threads) is None

