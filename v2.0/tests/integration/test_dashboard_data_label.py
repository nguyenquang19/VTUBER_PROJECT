"""Integration T3+T7 — operator rating + correction qua dashboard (Phase 8)."""
from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from dashboard.dashboard_server import DashboardServer
from services.operations.surface import OperationsSurface, OperationsSurfaceConfig

CONTROL_TOKEN = "test-dashboard-control-token-123456"
CONTROL_HEADERS = {"X-Mai-Operator-Token": CONTROL_TOKEN}


def _read(path: Path):
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def _server(tmp_path, last_turn_id=5):
    surface = OperationsSurface(OperationsSurfaceConfig(8, 8, 80, 1024))
    surface.register_snapshot_provider("data_label", lambda: {
        "latest_turn": (
            {"session_id": "session-live", "turn_id": last_turn_id}
            if last_turn_id else None
        ),
    })
    return DashboardServer(
        operations_surface=surface, data_dir=str(tmp_path),
        control_token=CONTROL_TOKEN,
    )


class TestRating:
    def test_rate_good_writes_ratings(self, tmp_path: Path) -> None:
        c = TestClient(_server(tmp_path).app, headers=CONTROL_HEADERS)
        r = c.post("/api/rate", json={"rating": "good"})
        assert r.status_code == 200 and r.json()["turn_id"] == 5
        recs = _read(tmp_path / "ratings.jsonl")
        assert recs[0]["turn_id"] == 5 and recs[0]["rating"] == "good"
        assert recs[0]["session_id"] == "session-live"
        assert recs[0]["schema_version"] == 1
        assert recs[0]["source"] == "ratings"
        assert recs[0]["timestamp"].endswith("+00:00")

    def test_invalid_rating_400(self, tmp_path: Path) -> None:
        c = TestClient(_server(tmp_path).app, headers=CONTROL_HEADERS)
        assert c.post("/api/rate", json={"rating": "meh"}).status_code == 400

    def test_no_turn_400(self, tmp_path: Path) -> None:
        c = TestClient(_server(tmp_path, last_turn_id=0).app, headers=CONTROL_HEADERS)
        assert c.post("/api/rate", json={"rating": "good"}).status_code == 400

    def test_rate_specific_turn_id(self, tmp_path: Path) -> None:
        # bấm 👍 trên 1 item Review → rate đúng turn_id đó, không phải turn cuối
        c = TestClient(_server(tmp_path, last_turn_id=99).app, headers=CONTROL_HEADERS)
        r = c.post("/api/rate", json={
            "rating": "bad", "session_id": "session-review", "turn_id": 3,
        })
        assert r.status_code == 200 and r.json()["turn_id"] == 3
        recs = _read(tmp_path / "ratings.jsonl")
        assert recs[0]["session_id"] == "session-review"
        assert recs[0]["turn_id"] == 3 and recs[0]["rating"] == "bad"

    def test_specific_turn_requires_session_id(self, tmp_path: Path) -> None:
        c = TestClient(_server(tmp_path).app, headers=CONTROL_HEADERS)
        assert c.post(
            "/api/rate", json={"rating": "good", "turn_id": 5},
        ).status_code == 400


class TestCorrection:
    def test_correct_writes_with_original(self, tmp_path: Path) -> None:
        # có turns.jsonl để lấy original
        turns = tmp_path / "turns.jsonl"
        turns.write_text(json.dumps({"session_id": "session-a", "turn_id": 5,
                                     "kind": "chat_reply",
                                     "user_text": "hi", "mai_text": "câu gốc dở"}) + "\n",
                         encoding="utf-8")
        c = TestClient(_server(tmp_path).app, headers=CONTROL_HEADERS)
        r = c.post("/api/correct", json={"session_id": "session-a", "turn_id": 5,
                                         "corrected_text": "câu sửa hay"})
        assert r.status_code == 200
        recs = _read(tmp_path / "corrections.jsonl")
        assert recs[0]["turn_id"] == 5
        assert recs[0]["session_id"] == "session-a"
        assert recs[0]["schema_version"] == 1
        assert recs[0]["source"] == "corrections"
        assert recs[0]["timestamp"].endswith("+00:00")
        assert recs[0]["original"] == "câu gốc dở"
        assert recs[0]["corrected"] == "câu sửa hay"

    def test_empty_correction_400(self, tmp_path: Path) -> None:
        c = TestClient(_server(tmp_path).app, headers=CONTROL_HEADERS)
        assert c.post("/api/correct", json={"session_id": "session-a", "turn_id": 5,
                                             "corrected_text": ""}).status_code == 400

    def test_same_turn_id_uses_matching_session(self, tmp_path: Path) -> None:
        turns = tmp_path / "turns.jsonl"
        records = [
            {"session_id": "session-a", "turn_id": 1, "kind": "chat_reply",
             "mai_text": "câu phiên A"},
            {"session_id": "session-b", "turn_id": 1, "kind": "chat_reply",
             "mai_text": "câu phiên B"},
        ]
        turns.write_text(
            "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
            encoding="utf-8",
        )
        c = TestClient(_server(tmp_path).app, headers=CONTROL_HEADERS)
        r = c.post("/api/correct", json={
            "session_id": "session-a", "turn_id": 1, "corrected_text": "câu sửa",
        })
        assert r.status_code == 200
        assert _read(tmp_path / "corrections.jsonl")[0]["original"] == "câu phiên A"

    def test_recent_turns_tail(self, tmp_path: Path) -> None:
        turns = tmp_path / "turns.jsonl"
        with turns.open("w", encoding="utf-8") as f:
            for i in range(30):
                f.write(json.dumps({"session_id": "session-tail", "turn_id": i,
                                    "kind": "chat_reply",
                                    "user_text": f"u{i}", "mai_text": f"m{i}"}) + "\n")
        c = TestClient(_server(tmp_path).app)
        data = c.get("/api/recent_turns?n=5").json()["turns"]
        assert len(data) == 5
        assert data[-1]["turn_id"] == 29   # mới nhất cuối
        assert data[-1]["session_id"] == "session-tail"
