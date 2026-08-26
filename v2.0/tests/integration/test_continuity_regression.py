from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.operations.metrics import MetricsCollector
from services.agent.conversation_context import (
    ConversationContextComposer, ConversationContextConfig,
)
from services.agent.repair_policy import (
    ConversationRepairPolicy, RepairKind, RepairPolicyConfig,
)
from interfaces.state import (
    AgentEventKind, AgentEventSource, AgentStateSnapshot, EventProvenance,
    GroundedEvent, OpenThread, ThreadEvidence, ThreadKind,
)
from services.data.sanitize import mask_pii

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "continuity_sanitized.json"
BASELINE = ROOT / "docs" / "baselines" / "m4_continuity_eval.json"
NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


def _load() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _events(data: dict) -> tuple[GroundedEvent, ...]:
    result = []
    for index, item in enumerate(data["events"]):
        result.append(GroundedEvent(
            item["event_id"], AgentEventKind(item["kind"]),
            AgentEventSource.CHAT, NOW + timedelta(seconds=index), 1.0,
            {"text": item["text"]},
            EventProvenance("sanitized_fixture", source_event_id=item["event_id"]),
        ))
    return tuple(result)


def _state(data: dict) -> AgentStateSnapshot:
    events = _events(data)
    thread = OpenThread(
        "fixture-story-thread", "Harry Potter", "viewer requested a Harry Potter story",
        NOW, NOW, NOW + timedelta(minutes=10), kind=ThreadKind.STORY,
        evidence=(ThreadEvidence(
            "sanitized-turn-10-user", "kể harry potter đi", "sanitized_fixture",
        ),),
        origin_event_id="sanitized-turn-10-user",
    )
    return AgentStateSnapshot(open_threads=(thread,), recent_events=events)


def test_fixture_contains_only_bounded_sanitized_transcript_material() -> None:
    data = _load()
    assert data["source"]["kind"] == "sanitized_local_transcript"
    assert data["source"]["path_committed"] is False
    assert data["source"]["raw_turn_count"] == 114
    assert len(data["events"]) == 4
    rendered = json.dumps(data, ensure_ascii=False)
    assert "viewer_id" not in rendered
    assert "session_id" not in rendered
    assert "@" not in rendered
    for item in data["events"]:
        assert mask_pii(item["text"]) == item["text"]
        assert len(item["text"]) <= 80


def test_continuity_fixture_meets_grounded_recall_and_repair_baseline() -> None:
    data = _load()
    state = _state(data)
    metrics = MetricsCollector()
    policy = ConversationRepairPolicy(
        RepairPolicyConfig(), clock=lambda: NOW + timedelta(minutes=1), metrics=metrics,
    )
    composer = ConversationContextComposer(
        ConversationContextConfig(), metrics=metrics, repair_policy=policy,
    )
    event_ids = {event.event_id for event in state.recent_events}
    grounded_total = 0
    grounded_matched = 0
    repair_counts: dict[str, int] = {}

    for check in data["checks"]:
        decision = policy.decide(state, check["query"])
        context = composer.render(state, check["query"])
        expected = check["expected"]
        if expected.startswith("grounded"):
            grounded_total += 1
            assert decision is None
            evidence_id = check["expected_evidence_id"]
            if evidence_id in context:
                grounded_matched += 1
            assert evidence_id in event_ids
        else:
            assert decision is not None
            assert decision.kind is RepairKind(expected)
            repair_counts[expected] = repair_counts.get(expected, 0) + 1

        referenced_source_ids = set(re.findall(r"source_id=([^;\]]+)", context))
        assert referenced_source_ids <= event_ids
        assert len(context) <= 1400

    metrics.set_grounded_recall_rate(grounded_matched, grounded_total)
    snapshot = metrics.continuity_snapshot()
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert grounded_total == baseline["grounded_checks"]
    assert grounded_matched == baseline["grounded_checks_matched"]
    assert snapshot["grounded_recall_rate"] >= baseline["grounded_recall_rate"]
    assert repair_counts == baseline["repair_checks"]
    assert b"mai_grounded_recall_rate 1.0" in metrics.prometheus_text()
