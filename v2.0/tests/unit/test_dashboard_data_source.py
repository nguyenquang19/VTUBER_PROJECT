from __future__ import annotations

import json
from pathlib import Path

from services.operations.dashboard_data_source import DashboardDataSource, _UpstreamHttpError
from services.operations.standalone_snapshot import StandaloneSnapshotProvider


def _source(tmp_path: Path, **overrides) -> DashboardDataSource:
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps({"runtime": {"session_id": "offline-session"}}), encoding="utf-8")
    values = {
        "offline_provider": StandaloneSnapshotProvider(
            snapshot_path=snapshot, audit_path=tmp_path / "audit.jsonl",
        ),
        "live_base_url": "http://127.0.0.1:65534",
        "turns_path": tmp_path / "turns.jsonl",
        "delivery_path": tmp_path / "delivery_outcomes.jsonl",
        "request_timeout_s": 0.05,
        "max_files": 3,
        "max_records": 20,
        "default_limit": 10,
        "max_limit": 20,
    }
    values.update(overrides)
    return DashboardDataSource(**values)


async def test_auto_falls_back_to_read_only_history_and_live_does_not(tmp_path: Path) -> None:
    source = _source(tmp_path)
    await source.start()

    auto = await source.snapshot_for("auto")
    live = await source.snapshot_for("live")

    assert auto["dashboard_source"]["actual"] == "history"
    assert auto["runtime"]["session_id"] == "offline-session"
    assert auto["runtime"]["controls_available"] is False
    assert live["dashboard_source"] == {
        **live["dashboard_source"],
        "requested": "live",
        "actual": "live",
        "available": False,
        "read_only": True,
        "error": "live_upstream_unavailable",
    }
    assert "session_id" not in live["runtime"]
    await source.stop()


async def test_live_snapshot_is_used_when_upstream_returns_object(tmp_path: Path, monkeypatch) -> None:
    source = _source(tmp_path)
    monkeypatch.setattr(source, "_request_json", lambda *_args: {
        "runtime": {"online": True, "controls_available": True},
        "agent": {"last_spoken_summary": "Xin chào"},
    })

    value = await source.snapshot_for("auto")

    assert value["dashboard_source"]["actual"] == "live"
    assert value["dashboard_source"]["read_only"] is False
    assert value["agent"]["last_spoken_summary"] == "Xin chào"


async def test_history_joins_rotated_turns_and_filters_delivery(tmp_path: Path) -> None:
    turns = tmp_path / "turns.jsonl"
    rotated = tmp_path / "turns.jsonl.1"
    delivery = tmp_path / "delivery_outcomes.jsonl"
    rotated.write_text("\n".join((
        json.dumps({
            "session_id": "s1", "request_id": "r1", "turn_id": 1,
            "kind": "chat", "timestamp": "2026-08-13T10:00:00+00:00",
            "mai_text": "một",
        }),
        "not-json",
    )), encoding="utf-8")
    turns.write_text(json.dumps({
        "session_id": "s2", "request_id": "r2", "turn_id": 2,
        "kind": "self_talk", "timestamp": "2026-08-13T11:00:00+00:00",
        "mai_text": "hai",
    }), encoding="utf-8")
    delivery.write_text("\n".join((
        json.dumps({
            "session_id": "s1", "request_id": "r1", "turn_id": 1,
            "delivered": True, "mode": "audio",
        }),
        json.dumps({
            "session_id": "s2", "request_id": "r2", "turn_id": 2,
            "delivered": False, "mode": "failed",
        }),
    )), encoding="utf-8")
    source = _source(tmp_path, turns_path=turns, delivery_path=delivery)

    value = await source.query_history(delivered=True)

    assert value["total_matched"] == 1
    assert value["records"][0]["mai_text"] == "một"
    assert value["records"][0]["delivery_mode"] == "audio"
    assert value["malformed_skipped"] == 1
    assert source.get_metrics()["dashboard_history_records_scanned"] == 4


async def test_history_applies_session_time_kind_and_limit_bounds(tmp_path: Path) -> None:
    turns = tmp_path / "turns.jsonl"
    turns.write_text("\n".join(json.dumps({
        "session_id": "s1", "request_id": f"r{index}", "turn_id": index,
        "kind": "chat", "timestamp": f"2026-08-13T10:0{index}:00+00:00",
    }) for index in range(3)), encoding="utf-8")
    source = _source(tmp_path, turns_path=turns, max_limit=2, default_limit=1)

    value = await source.query_history(
        session_id="s1", started_at="2026-08-13T10:01:00+00:00",
        kind="chat", limit=99,
    )

    assert value["total_matched"] == 2
    assert value["limit"] == 2
    assert [record["turn_id"] for record in value["records"]] == [2, 1]


async def test_command_proxy_is_allowlisted(tmp_path: Path, monkeypatch) -> None:
    source = _source(tmp_path)
    calls = []
    monkeypatch.setattr(source, "_request_json", lambda method, path, payload: (
        calls.append((method, path, payload)) or {"ok": True}
    ))

    denied = await source.forward_command("/api/relationships/viewer/delete", {})
    allowed = await source.forward_command("/api/agent/pause", {"reason": "test"})

    assert denied == (403, {"ok": False, "reason": "command_not_allowlisted"})
    assert allowed == (200, {"ok": True})
    assert calls == [("POST", "/api/agent/pause", {"reason": "test"})]


async def test_command_proxy_preserves_allowlisted_upstream_error(tmp_path: Path, monkeypatch) -> None:
    source = _source(tmp_path)

    def fail(*_args):
        raise _UpstreamHttpError(409, {"ok": False, "reason": "already_paused"})

    monkeypatch.setattr(source, "_request_json", fail)

    status, value = await source.forward_command("/api/agent/pause", {})

    assert status == 409
    assert value == {"ok": False, "reason": "already_paused"}


def test_upstream_must_be_loopback(tmp_path: Path) -> None:
    try:
        _source(tmp_path, live_base_url="https://example.com")
    except ValueError as exc:
        assert "loopback" in str(exc)
    else:
        raise AssertionError("non-loopback upstream should be rejected")
