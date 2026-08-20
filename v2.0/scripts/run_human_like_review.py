"""Build or finalize a strict MAI-HLC blind comparison artifact."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.config_loader import ConfigLoader  # noqa: E402
from services.evaluation.human_like import HumanLikeCalibration  # noqa: E402
from services.evaluation.release_gate import (  # noqa: E402
    ReleaseReadinessConfig,
    SourceState,
    inspect_source_state,
)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    temporary.replace(path)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_human_release_evidence(
    final: dict[str, Any], *, review_digest: str, loader: Any,
    source_state: SourceState, now_utc: datetime,
) -> dict[str, Any]:
    """Bind a finalized blind review to one source revision without re-scoring it."""
    config = ReleaseReadinessConfig.from_loader(loader)
    if not source_state.clean:
        raise RuntimeError("human release evidence requires a clean worktree")
    if now_utc.tzinfo is None or now_utc.utcoffset() is None:
        raise ValueError("human release evidence clock must be timezone-aware")
    summaries = final.get("summaries")
    if final.get("marker") != "mai_hlc_finalized_review" or not isinstance(summaries, dict):
        raise ValueError("finalized MAI-HLC artifact is invalid")
    previous = summaries.get("previous")
    candidate = summaries.get("candidate")
    if not isinstance(previous, dict) or not isinstance(candidate, dict):
        raise ValueError("finalized MAI-HLC summaries are invalid")

    def projection(value: dict[str, Any]) -> dict[str, float]:
        dimensions = value.get("dimension_averages")
        if not isinstance(dimensions, dict):
            raise ValueError("finalized MAI-HLC dimensions are invalid")
        return {
            "weighted_average": float(value["weighted_average"]),
            "ai_smell_rate": float(value["ai_smell_rate"]),
            "character_average": float(dimensions["character"]),
        }

    previous_projection = projection(previous)
    candidate_projection = projection(candidate)
    previous_pairs = previous.get("reviewed_candidates")
    candidate_pairs = candidate.get("reviewed_candidates")
    if type(previous_pairs) is not int or previous_pairs != candidate_pairs:
        raise ValueError("finalized MAI-HLC reviewed pair counts disagree")
    delta = candidate_projection["weighted_average"] - previous_projection["weighted_average"]
    passed = (
        previous_pairs >= config.human_min_pairs
        and delta >= config.minimum_previous_build_delta
        and candidate_projection["ai_smell_rate"] - previous_projection["ai_smell_rate"]
        <= config.max_ai_smell_rate_increase
        and candidate_projection["character_average"] - previous_projection["character_average"]
        >= config.minimum_character_delta
        and final.get("status") == "review_complete"
        and final.get("human_review", {}).get("complete") is True
    )
    return {
        "schema_version": config.schema_version,
        "marker": "mai_human_quality_evidence",
        "sanitized": True,
        "source_revision": source_state.revision,
        "current_product_version": str(loader.get("system", "app.version", "")),
        "target_product_version": config.target_version,
        "generated_at_utc": now_utc.astimezone(timezone.utc).isoformat(),
        "review_digest": review_digest,
        "reviewed_pairs": previous_pairs,
        "previous": previous_projection,
        "candidate": candidate_projection,
        "previous_build_delta": round(delta, 6),
        "operator_approved": final.get("human_review", {}).get("complete") is True,
        "status": "passed" if passed else "failed",
    }


async def _run(args: argparse.Namespace) -> int:
    loader = ConfigLoader(Path("config"))
    loader.load_all()
    service = HumanLikeCalibration.from_loader(loader, enabled=True)
    await service.start()
    try:
        if args.finalize:
            if args.manifest is None or args.output is None:
                raise ValueError("--manifest and --output are required with --finalize")
            manifest = _read_json(args.manifest)
            final = await service.finalize(args.finalize, manifest)
            _write_json(args.output, final)
            release_status = None
            if args.release_evidence is not None:
                source = inspect_source_state(Path.cwd())
                evidence = build_human_release_evidence(
                    final, review_digest=_sha256(args.output), loader=loader,
                    source_state=source, now_utc=datetime.now(timezone.utc),
                )
                _write_json(args.release_evidence, evidence)
                release_status = evidence["status"]
            print(json.dumps({
                "output": str(args.output),
                "status": final["status"],
                "previous_build_delta": final["previous_build_delta"],
                "automatic_release_decision": False,
                "release_evidence_status": release_status,
            }, ensure_ascii=False))
            return 0
        if args.input is None or args.review is None or args.manifest is None:
            raise ValueError("--input, --review and --manifest are required to build")
        raw = _read_json(args.input)
        comparisons = raw.get("comparisons") if isinstance(raw, dict) else raw
        if not isinstance(comparisons, list):
            raise ValueError("input must contain a comparisons list")
        artifact, manifest = service.build(tuple(comparisons))
        _write_json(args.review, artifact)
        _write_json(args.manifest, manifest)
        print(json.dumps({
            "review": str(args.review),
            "manifest": str(args.manifest),
            "status": artifact["status"],
            "build_identity_hidden": True,
        }, ensure_ascii=False))
        return 0
    finally:
        await service.stop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--review", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--finalize", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--release-evidence", type=Path)
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
