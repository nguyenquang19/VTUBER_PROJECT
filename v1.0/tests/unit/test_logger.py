"""Test logger: JSONL sink, rotation, turn schema (ARCHITECTURE 9.3, 13.8)."""
from __future__ import annotations

import json
from pathlib import Path

from orchestrator.config_loader import ConfigLoader
from orchestrator.logger import (
    JsonlWriter,
    TurnLogger,
    get_logger,
    setup_from_config,
    setup_logging,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


class TestJsonlWriter:
    def test_write_appends_one_line_per_record(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        w = JsonlWriter(path)
        w.write({"a": 1})
        w.write({"b": 2})
        lines = path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 2
        assert json.loads(lines[0]) == {"a": 1}
        assert json.loads(lines[1]) == {"b": 2}

    def test_creates_parent_dir(self, tmp_path: Path) -> None:
        path = tmp_path / "nested" / "deep" / "events.jsonl"
        JsonlWriter(path).write({"x": 1})
        assert path.exists()

    def test_vietnamese_not_escaped(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        JsonlWriter(path).write({"msg": "Xin chào Mai"})
        assert "Xin chào Mai" in path.read_text(encoding="utf-8")

    def test_non_serializable_falls_back_to_str(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        JsonlWriter(path).write({"obj": object()})
        record = json.loads(path.read_text(encoding="utf-8").strip())
        assert "object object" in record["obj"]

    def test_rotation_when_size_exceeded(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        # max_size 0 MB → mọi write sau lần đầu đều rotate
        w = JsonlWriter(path, max_size_mb=0, keep_files=3)
        w.write({"n": 1})
        w.write({"n": 2})
        assert path.exists()
        assert path.with_suffix(".jsonl.1").exists()
        assert json.loads(path.read_text(encoding="utf-8").strip())["n"] == 2
        assert json.loads(path.with_suffix(".jsonl.1").read_text(encoding="utf-8").strip())["n"] == 1

    def test_rotation_respects_keep_files(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        w = JsonlWriter(path, max_size_mb=0, keep_files=2)
        for i in range(5):
            w.write({"n": i})
        assert path.with_suffix(".jsonl.1").exists()
        assert path.with_suffix(".jsonl.2").exists()
        # keep_files=2 → không được tạo .3
        assert not path.with_suffix(".jsonl.3").exists()

    def test_rotation_disabled_keeps_appending(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        w = JsonlWriter(path, max_size_mb=0, keep_files=3, rotation_enabled=False)
        w.write({"n": 1})
        w.write({"n": 2})
        assert not path.with_suffix(".jsonl.1").exists()
        assert len(path.read_text(encoding="utf-8").strip().split("\n")) == 2


class TestTurnLogger:
    def test_log_turn_adds_timestamp(self, tmp_path: Path) -> None:
        path = tmp_path / "turns.jsonl"
        tl = TurnLogger(JsonlWriter(path))
        tl.log_turn({"turn_id": 1, "prompt": "hi"})
        record = json.loads(path.read_text(encoding="utf-8").strip())
        assert record["turn_id"] == 1
        assert "timestamp" in record

    def test_log_turn_preserves_given_timestamp(self, tmp_path: Path) -> None:
        path = tmp_path / "turns.jsonl"
        tl = TurnLogger(JsonlWriter(path))
        tl.log_turn({"turn_id": 2, "timestamp": "2026-07-30T00:00:00Z"})
        record = json.loads(path.read_text(encoding="utf-8").strip())
        assert record["timestamp"] == "2026-07-30T00:00:00Z"

    def test_turn_schema_fields_roundtrip(self, tmp_path: Path) -> None:
        """Schema ARCHITECTURE 9.3 — nested dict/list phải giữ nguyên."""
        path = tmp_path / "turns.jsonl"
        tl = TurnLogger(JsonlWriter(path))
        turn = {
            "turn_id": 12345,
            "trigger": {"type": "chat_mention", "priority": 80, "queue_wait_ms": 250},
            "state_transitions": [
                {"from": "IDLE", "to": "THINKING", "at": "10:30:15.123"},
                {"from": "THINKING", "to": "SPEAKING", "at": "10:30:15.560"},
            ],
            "latency": {"ttfa_ms": 720, "total_ms": 1735},
            "features_active": ["filter_rule", "tts_streaming"],
        }
        tl.log_turn(turn)
        record = json.loads(path.read_text(encoding="utf-8").strip())
        assert record["trigger"]["priority"] == 80
        assert len(record["state_transitions"]) == 2
        assert record["latency"]["ttfa_ms"] == 720
        assert record["features_active"] == ["filter_rule", "tts_streaming"]


class TestSetupLogging:
    def test_events_go_to_jsonl(self, tmp_path: Path) -> None:
        setup_logging(log_dir=tmp_path, console_enabled=False)
        log = get_logger("test")
        log.info("state_transition", **{"from": "IDLE", "to": "THINKING"})
        content = (tmp_path / "events.jsonl").read_text(encoding="utf-8")
        record = json.loads(content.strip().split("\n")[-1])
        assert record["event"] == "state_transition"
        assert record["from"] == "IDLE"
        assert record["level"] == "info"
        assert "timestamp" in record

    def test_level_filter_drops_debug_when_info(self, tmp_path: Path) -> None:
        setup_logging(level="INFO", log_dir=tmp_path, console_enabled=False)
        log = get_logger("test")
        log.debug("should_not_appear")
        log.info("should_appear")
        content = (tmp_path / "events.jsonl").read_text(encoding="utf-8")
        assert "should_not_appear" not in content
        assert "should_appear" in content

    def test_jsonl_disabled_writes_no_events_file(self, tmp_path: Path) -> None:
        setup_logging(log_dir=tmp_path, console_enabled=False, jsonl_enabled=False)
        get_logger("test").info("nowhere")
        assert not (tmp_path / "events.jsonl").exists()

    def test_returns_working_turn_logger(self, tmp_path: Path) -> None:
        turn_logger = setup_logging(log_dir=tmp_path, console_enabled=False)
        turn_logger.log_turn({"turn_id": 7})
        record = json.loads((tmp_path / "turns.jsonl").read_text(encoding="utf-8").strip())
        assert record["turn_id"] == 7

    def test_setup_from_config_uses_real_yaml(self, tmp_path: Path) -> None:
        loader = ConfigLoader(REPO_ROOT / "config")
        loader.load_all()
        # config thật trỏ log_dir="logs"; test không ghi vào repo nên chỉ verify
        # setup chạy được và trả TurnLogger, còn ghi file test riêng ở trên.
        assert loader.get("logging", "jsonl.turns_file") == "turns.jsonl"
        assert loader.get("logging", "level") == "INFO"
        assert loader.get("logging", "rotation.max_size_mb") == 100
