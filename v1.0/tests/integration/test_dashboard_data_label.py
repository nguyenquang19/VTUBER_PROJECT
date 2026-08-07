"""Integration T3+T7 — operator rating + correction qua dashboard (Phase 8)."""
from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from dashboard.dashboard_server import DashboardServer


class FakeRunner:
    def __init__(self, last=0):
        self.last_turn_id = last


def _read(path: Path):
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def _server(tmp_path, last_turn_id=5):
    return DashboardServer(runner=FakeRunner(last_turn_id), data_dir=str(tmp_path))


class TestRating:
    def test_rate_good_writes_ratings(self, tmp_path: Path) -> None:
        c = TestClient(_server(tmp_path).app)
        r = c.post("/api/rate", json={"rating": "good"})
        assert r.status_code == 200 and r.json()["turn_id"] == 5
        recs = _read(tmp_path / "ratings.jsonl")
        assert recs[0]["turn_id"] == 5 and recs[0]["rating"] == "good"

    def test_invalid_rating_400(self, tmp_path: Path) -> None:
        c = TestClient(_server(tmp_path).app)
        assert c.post("/api/rate", json={"rating": "meh"}).status_code == 400

    def test_no_turn_400(self, tmp_path: Path) -> None:
        c = TestClient(_server(tmp_path, last_turn_id=0).app)
        assert c.post("/api/rate", json={"rating": "good"}).status_code == 400

    def test_rate_specific_turn_id(self, tmp_path: Path) -> None:
        # bấm 👍 trên 1 item Review → rate đúng turn_id đó, không phải turn cuối
        c = TestClient(_server(tmp_path, last_turn_id=99).app)
        r = c.post("/api/rate", json={"rating": "bad", "turn_id": 3})
        assert r.status_code == 200 and r.json()["turn_id"] == 3
        recs = _read(tmp_path / "ratings.jsonl")
        assert recs[0]["turn_id"] == 3 and recs[0]["rating"] == "bad"


class TestCorrection:
    def test_correct_writes_with_original(self, tmp_path: Path) -> None:
        # có turns.jsonl để lấy original
        turns = tmp_path / "turns.jsonl"
        turns.write_text(json.dumps({"turn_id": 5, "kind": "chat_reply",
                                     "user_text": "hi", "mai_text": "câu gốc dở"}) + "\n",
                         encoding="utf-8")
        c = TestClient(_server(tmp_path).app)
        r = c.post("/api/correct", json={"turn_id": 5, "corrected_text": "câu sửa hay"})
        assert r.status_code == 200
        recs = _read(tmp_path / "corrections.jsonl")
        assert recs[0]["turn_id"] == 5
        assert recs[0]["original"] == "câu gốc dở"
        assert recs[0]["corrected"] == "câu sửa hay"

    def test_empty_correction_400(self, tmp_path: Path) -> None:
        c = TestClient(_server(tmp_path).app)
        assert c.post("/api/correct", json={"turn_id": 5, "corrected_text": ""}).status_code == 400

    def test_recent_turns_tail(self, tmp_path: Path) -> None:
        turns = tmp_path / "turns.jsonl"
        with turns.open("w", encoding="utf-8") as f:
            for i in range(30):
                f.write(json.dumps({"turn_id": i, "kind": "chat_reply",
                                    "user_text": f"u{i}", "mai_text": f"m{i}"}) + "\n")
        c = TestClient(_server(tmp_path).app)
        data = c.get("/api/recent_turns?n=5").json()["turns"]
        assert len(data) == 5
        assert data[-1]["turn_id"] == 29   # mới nhất cuối
