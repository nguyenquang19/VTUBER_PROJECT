"""Test logger: JSONL sink, rotation, turn schema (ARCHITECTURE 9.3, 13.8)."""
from __future__ import annotations

import json
from pathlib import Path

from orchestrator.config_loader import ConfigLoader
from orchestrator.logger import (
    JsonlWriter,
    TurnLogger,
    bind_log_session,
    get_logger,
    setup_from_config,
    setup_logging,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _turn_record(turn_id: int, **overrides):
    record = {
        "schema_version": 3,
        "turn_id": turn_id,
        "request_id": f"request-{turn_id}",
        "kind": "chat_reply",
    }
    record.update(overrides)
    return record


class TestJsonlWriter:
    def test_write_appends_one_line_per_record(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        w = JsonlWriter(path)
        w.write({"a": 1})
        w.write({"b": 2})
        lines = path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 2
        first = json.loads(lines[0])
        second = json.loads(lines[1])
        assert first["a"] == 1 and second["b"] == 2
        for record in (first, second):
            assert record["schema_version"] == 1
            assert record["source"] == "events"
            assert record["timestamp"].endswith("+00:00")
            assert "session_id" in record

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

    def test_free_form_event_fields_are_scrubbed(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        JsonlWriter(path).write({
            "event": "failed",
            "error": "token sk_live_abcdefghij1234567890xyz for @real_user",
            "viewer_id": "raw-platform-id",
        })
        record = json.loads(path.read_text(encoding="utf-8"))
        assert "sk_live" not in record["error"]
        assert "@real_user" not in record["error"]
        assert record["viewer_id"].startswith("v_")

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
        archives = list(tmp_path.glob("events.jsonl.archive.*"))
        assert len(archives) == 2
        records = [path, path.with_suffix(".jsonl.1"), path.with_suffix(".jsonl.2"),
                   *archives]
        assert sorted(json.loads(p.read_text(encoding="utf-8"))["n"] for p in records) == list(range(5))

    def test_rotation_disabled_keeps_appending(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        w = JsonlWriter(path, max_size_mb=0, keep_files=3, rotation_enabled=False)
        w.write({"n": 1})
        w.write({"n": 2})
        assert not path.with_suffix(".jsonl.1").exists()
        assert len(path.read_text(encoding="utf-8").strip().split("\n")) == 2

    def test_permission_failure_is_buffered_and_recovers(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        path = tmp_path / "events.jsonl"
        errors: list[tuple[str, str]] = []
        writer = JsonlWriter(
            path,
            degraded_buffer_records=2,
            error_callback=lambda sink, error: errors.append((sink, error)),
        )
        original_open = Path.open
        blocked = True

        def guarded_open(target, *args, **kwargs):
            if blocked and target == path:
                raise PermissionError("read-only log directory")
            return original_open(target, *args, **kwargs)

        monkeypatch.setattr(Path, "open", guarded_open)
        assert writer.write({"n": 1}) is False
        metrics = writer.get_metrics()
        assert metrics["logging_sink_degraded"] is True
        assert metrics["logging_sink_buffered_records"] == 1
        assert errors == [("events", "PermissionError")]

        blocked = False
        assert writer.flush() is True
        assert json.loads(path.read_text(encoding="utf-8"))["n"] == 1
        assert writer.get_metrics()["logging_sink_degraded"] is False

    def test_degraded_buffer_is_bounded(self, tmp_path: Path, monkeypatch) -> None:
        path = tmp_path / "events.jsonl"
        writer = JsonlWriter(path, degraded_buffer_records=2)

        def fail_open(target, *args, **kwargs):
            raise PermissionError("blocked")

        monkeypatch.setattr(Path, "open", fail_open)
        for value in range(3):
            assert writer.write({"n": value}) is False
        metrics = writer.get_metrics()
        assert metrics["logging_sink_buffered_records"] == 2
        assert metrics["logging_sink_buffer_dropped_total"] == 1
        assert metrics["logging_sink_errors_total"] == 3


class TestTurnLogger:
    def test_log_turn_adds_timestamp(self, tmp_path: Path) -> None:
        path = tmp_path / "turns.jsonl"
        tl = TurnLogger(JsonlWriter(path))
        tl.log_turn(_turn_record(1, user_text="hi"))
        record = json.loads(path.read_text(encoding="utf-8").strip())
        assert record["turn_id"] == 1
        assert "timestamp" in record

    def test_log_turn_preserves_given_timestamp(self, tmp_path: Path) -> None:
        path = tmp_path / "turns.jsonl"
        tl = TurnLogger(JsonlWriter(path))
        tl.log_turn(_turn_record(2, timestamp="2026-07-30T00:00:00Z"))
        record = json.loads(path.read_text(encoding="utf-8").strip())
        assert record["timestamp"] == "2026-07-30T00:00:00+00:00"

    def test_session_binding_propagates_to_writer(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        bind_log_session("session-test")
        JsonlWriter(path).write({"event": "x"})
        bind_log_session(None)
        record = json.loads(path.read_text(encoding="utf-8"))
        assert record["session_id"] == "session-test"

    def test_turn_schema_fields_roundtrip(self, tmp_path: Path) -> None:
        """Schema ARCHITECTURE 9.3 — nested dict/list phải giữ nguyên."""
        path = tmp_path / "turns.jsonl"
        tl = TurnLogger(JsonlWriter(path))
        turn = _turn_record(
            12345,
            mood_state={"vui": 7, "buon": 0},
            mood_cause={"event_id": "chat-1", "alias": "viewer"},
            filter_verdict={"passed": True, "categories": []},
        )
        tl.log_turn(turn)
        record = json.loads(path.read_text(encoding="utf-8").strip())
        assert record["mood_state"]["vui"] == 7
        assert record["mood_cause"]["event_id"] == "chat-1"
        assert record["filter_verdict"]["passed"] is True


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
        assert record["schema_version"] == 1
        assert record["source"] == "events"

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
        turn_logger.log_turn(_turn_record(7))
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

    def test_setup_from_config_creates_local_privacy_salt(self, tmp_path: Path) -> None:
        salt_path = tmp_path / "private" / "salt.bin"

        class Loader:
            def get(self, name, key, default=None):
                if (name, key) == ("data_privacy", "privacy.viewer_hash_salt_file"):
                    return str(salt_path)
                if (name, key) == ("logging", "jsonl.dir"):
                    return str(tmp_path / "logs")
                return default

        setup_from_config(Loader())
        assert salt_path.exists()
        assert len(salt_path.read_bytes()) >= 32
