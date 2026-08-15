"""M0.4 — manual data backup, checksum manifest and dry-run safety."""
from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "backup_data", REPO / "scripts" / "backup_data.py",
)
backup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backup)


def test_dry_run_has_checksums_and_writes_nothing(tmp_path: Path) -> None:
    source = tmp_path / "logs"
    source.mkdir()
    (source / "turns.jsonl").write_text('{"turn_id":1}\n', encoding="utf-8")
    destination = tmp_path / "backups"

    manifest = backup.backup_data(
        source, destination, ["*.jsonl"], dry_run=True,
        now=datetime(2026, 8, 8, tzinfo=timezone.utc),
    )

    assert manifest["dry_run"] is True
    assert manifest["file_count"] == 1
    assert len(manifest["files"][0]["sha256"]) == 64
    assert not destination.exists()
    assert (source / "turns.jsonl").exists()


def test_backup_copies_without_mutating_source_and_writes_manifest(tmp_path: Path) -> None:
    source = tmp_path / "logs"
    source.mkdir()
    original = '{"turn_id":1}\n'
    (source / "turns.jsonl").write_text(original, encoding="utf-8")
    (source / "turns.jsonl.1").write_text('{"turn_id":0}\n', encoding="utf-8")
    destination = tmp_path / "backups"

    result = backup.backup_data(
        source, destination, ["*.jsonl", "*.jsonl.*"], dry_run=False,
        now=datetime(2026, 8, 8, tzinfo=timezone.utc),
    )
    backup_dir = Path(result["backup_dir"])
    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["file_count"] == 2
    assert (backup_dir / "turns.jsonl").exists()
    assert (backup_dir / "turns.jsonl.1").exists()
    assert (source / "turns.jsonl").read_text(encoding="utf-8") == original
