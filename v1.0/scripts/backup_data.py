"""Manual, non-destructive backup for Mai JSONL data.

Usage (Windows PowerShell):
  .\venv\Scripts\python.exe scripts\backup_data.py --dry-run
  .\venv\Scripts\python.exe scripts\backup_data.py
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from orchestrator.config_loader import ConfigLoader  # noqa: E402


def collect_sources(source_dir: Path, patterns: list[str]) -> list[Path]:
    """Return a stable, duplicate-free list of files selected by configured patterns."""
    files: dict[Path, None] = {}
    for pattern in patterns:
        for path in source_dir.glob(pattern):
            if path.is_file():
                files[path.resolve()] = None
    return sorted(files, key=lambda path: str(path).lower())


def backup_data(
    source_dir: Path,
    destination_dir: Path,
    patterns: list[str],
    *,
    dry_run: bool,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Copy selected logs and emit a checksum manifest; dry-run performs no writes."""
    source_root = source_dir.resolve()
    destination_root = destination_dir.resolve()
    if source_root == destination_root or source_root in destination_root.parents:
        raise ValueError("backup destination must not be inside the source directory")

    timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    backup_dir = destination_root / timestamp.strftime("backup_%Y%m%dT%H%M%S%fZ")
    sources = collect_sources(source_root, patterns)
    entries: list[dict[str, Any]] = []
    for source in sources:
        relative = source.relative_to(source_root)
        entries.append({
            "path": relative.as_posix(),
            "size_bytes": source.stat().st_size,
            "sha256": _sha256(source),
        })
        if not dry_run:
            target = backup_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

    manifest: dict[str, Any] = {
        "schema_version": 1,
        "timestamp": timestamp.isoformat(),
        "source": "data_backup",
        "session_id": None,
        "dry_run": dry_run,
        "source_dir": str(source_root),
        "backup_dir": str(backup_dir),
        "file_count": len(entries),
        "files": entries,
    }
    if not dry_run:
        manifest_path = backup_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
    return manifest


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description="Backup Mai JSONL data without deleting sources")
    parser.add_argument("--config-dir", default=str(REPO_ROOT / "config"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    loader = ConfigLoader(Path(args.config_dir))
    loader.load_all()
    manifest = backup_data(
        Path(loader.get("data_privacy", "backup.source_dir", "logs")),
        Path(loader.get("data_privacy", "backup.destination_dir", "backups/data")),
        list(loader.get("data_privacy", "backup.include_patterns", ["*.jsonl"])),
        dry_run=args.dry_run,
    )
    mode = "DRY-RUN" if args.dry_run else "BACKUP"
    print(f"{mode}: {manifest['file_count']} files → {manifest['backup_dir']}")
    for entry in manifest["files"]:
        print(f"  {entry['path']}  {entry['size_bytes']} bytes  sha256={entry['sha256']}")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
