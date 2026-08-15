"""Build a metadata-only post-stream review from durable operations artifacts."""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ReviewConfig:
    shutdown_snapshot: Path
    incident_log: Path
    operator_audit: Path
    soak_report: Path
    export_dir: Path

    @classmethod
    def from_loader(cls, loader: Any) -> "ReviewConfig":
        return cls(
            shutdown_snapshot=Path(loader.get(
                "operations", "shutdown.snapshot_file",
                "logs/operations/last_runtime_snapshot.json",
            )),
            incident_log=Path(loader.get(
                "operations", "incident_log.file", "logs/operations/incidents.jsonl",
            )),
            operator_audit=Path(loader.get(
                "operations", "dashboard_standalone.operator_audit_file",
                "logs/operations/operator_audit.jsonl",
            )),
            soak_report=Path(loader.get(
                "operations", "soak.report_file", "docs/baselines/m9_live_operations.json",
            )),
            export_dir=Path(loader.get(
                "operations", "post_stream_review.export_dir", "logs/operations/reviews",
            )),
        )


class PostStreamReviewer:
    def __init__(self, config: ReviewConfig) -> None:
        self.config = config

    async def review(self, output: Path | None = None) -> dict[str, Any]:
        return await asyncio.to_thread(self._review_sync, output)

    def _review_sync(self, output: Path | None) -> dict[str, Any]:
        shutdown, shutdown_error = _read_json(self.config.shutdown_snapshot)
        soak, soak_error = _read_json(self.config.soak_report)
        incidents = _read_jsonl(self.config.incident_log)
        audit = _read_jsonl(self.config.operator_audit)
        latest_incidents: dict[str, dict[str, Any]] = {}
        for item in incidents["records"]:
            incident_id = str(item.get("incident_id") or "")
            if incident_id:
                latest_incidents[incident_id] = item
        unresolved_ids = sorted(
            incident_id for incident_id, item in latest_incidents.items()
            if item.get("status") != "resolved"
        )
        shutdown_ok = bool(shutdown) and not shutdown_error and not (
            shutdown or {}
        ).get("shutdown_errors_before_snapshot")
        soak_ok = bool(soak) and not soak_error and bool((soak or {}).get("passed"))
        checklist = {
            "shutdown_snapshot": {
                "ok": shutdown_ok, "exists": self.config.shutdown_snapshot.exists(),
                "schema_version": (shutdown or {}).get("schema_version"),
                "error": shutdown_error,
            },
            "incident_log": {
                "ok": incidents["parse_errors"] == 0 and not unresolved_ids,
                "events": incidents["count"], "parse_errors": incidents["parse_errors"],
                "unresolved_count": len(unresolved_ids),
                "unresolved_ids": unresolved_ids,
            },
            "operator_audit": {
                "ok": audit["parse_errors"] == 0,
                "events": audit["count"], "parse_errors": audit["parse_errors"],
            },
            "soak_acceptance": {
                "ok": soak_ok, "exists": self.config.soak_report.exists(),
                "configured_duration_s": (soak or {}).get("configured_duration_s"),
                "error": soak_error,
            },
        }
        report = {
            "schema_version": 1,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "kind": "post_stream_review",
            "ready": all(item["ok"] for item in checklist.values()),
            "checklist": checklist,
            "privacy": "metadata_only_no_chat_or_prompt_content",
        }
        target = output or self._default_output()
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        os.replace(temporary, target)
        report["output"] = str(target)
        return report

    def _default_output(self) -> Path:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return self.config.export_dir / f"post_stream_review_{stamp}.json"


def _read_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not path.exists():
        return None, "missing"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, type(exc).__name__
    return (value, None) if isinstance(value, dict) else (None, "invalid_root")


def _read_jsonl(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"records": [], "count": 0, "parse_errors": 0}
    records: list[dict[str, Any]] = []
    parse_errors = 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {"records": [], "count": 0, "parse_errors": 1}
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            parse_errors += 1
            continue
        if isinstance(value, dict):
            records.append(value)
        else:
            parse_errors += 1
    return {"records": records, "count": len(records), "parse_errors": parse_errors}
