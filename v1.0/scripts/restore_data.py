"""Verify and safely restore a Mai data backup without deleting existing data."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative_path(value: str) -> Path:
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError(f"unsafe manifest path: {value!r}")
    return relative


def restore_data(
    backup_dir: Path,
    destination_dir: Path,
    *,
    apply: bool = False,
    overwrite: bool = False,
) -> dict[str, Any]:
    backup_root = backup_dir.resolve()
    destination_root = destination_dir.resolve()
    manifest_path = backup_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1 or not isinstance(manifest.get("files"), list):
        raise ValueError("unsupported or malformed backup manifest")

    entries: list[dict[str, Any]] = []
    for item in manifest["files"]:
        relative = _safe_relative_path(str(item.get("path", "")))
        source = (backup_root / relative).resolve()
        if backup_root not in source.parents:
            raise ValueError(f"backup file escapes backup directory: {relative}")
        expected = str(item.get("sha256", ""))
        actual = _sha256(source) if source.is_file() else ""
        if not expected or actual != expected:
            raise ValueError(f"checksum mismatch: {relative.as_posix()}")
        target = destination_root / relative
        if apply and target.exists() and not overwrite:
            raise FileExistsError(f"restore target exists: {target}")
        entries.append({"path": relative.as_posix(), "sha256": actual})

    if apply:
        for entry in entries:
            relative = Path(entry["path"])
            source = backup_root / relative
            target = destination_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

    return {
        "schema_version": 1,
        "verified": True,
        "applied": apply,
        "overwrite": overwrite,
        "backup_dir": str(backup_root),
        "destination_dir": str(destination_root),
        "file_count": len(entries),
        "files": entries,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify or restore a Mai data backup")
    parser.add_argument("backup_dir")
    parser.add_argument("--destination", default=str(REPO_ROOT / "logs"))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args()
    report = restore_data(
        Path(args.backup_dir), Path(args.destination),
        apply=args.apply, overwrite=args.overwrite,
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
