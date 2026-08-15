from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.restore_data import restore_data


def _backup(tmp_path: Path, relative: str = "events.jsonl") -> Path:
    root = tmp_path / "backup"
    source = root / relative
    source.parent.mkdir(parents=True)
    source.write_text('{"ok": true}\n', encoding="utf-8")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    (root / "manifest.json").write_text(json.dumps({
        "schema_version": 1,
        "files": [{"path": relative, "sha256": digest}],
    }), encoding="utf-8")
    return root


def test_restore_defaults_to_verify_only(tmp_path: Path) -> None:
    destination = tmp_path / "restored"
    report = restore_data(_backup(tmp_path), destination)
    assert report["verified"] is True
    assert report["applied"] is False
    assert not destination.exists()


def test_restore_applies_verified_files_without_deleting_data(tmp_path: Path) -> None:
    destination = tmp_path / "restored"
    destination.mkdir()
    keep = destination / "keep.txt"
    keep.write_text("keep", encoding="utf-8")
    report = restore_data(_backup(tmp_path), destination, apply=True)
    assert report["file_count"] == 1
    assert (destination / "events.jsonl").is_file()
    assert keep.read_text(encoding="utf-8") == "keep"


def test_restore_rejects_checksum_mismatch(tmp_path: Path) -> None:
    backup = _backup(tmp_path)
    (backup / "events.jsonl").write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="checksum mismatch"):
        restore_data(backup, tmp_path / "restored", apply=True)


def test_restore_rejects_path_traversal(tmp_path: Path) -> None:
    backup = _backup(tmp_path)
    manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    manifest["files"][0]["path"] = "../escape.jsonl"
    (backup / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="unsafe manifest path"):
        restore_data(backup, tmp_path / "restored")


def test_restore_refuses_overwrite_without_explicit_flag(tmp_path: Path) -> None:
    destination = tmp_path / "restored"
    destination.mkdir()
    (destination / "events.jsonl").write_text("existing", encoding="utf-8")
    with pytest.raises(FileExistsError, match="target exists"):
        restore_data(_backup(tmp_path), destination, apply=True)
