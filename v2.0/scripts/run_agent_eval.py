"""Run M8 scenario checks and emit a sanitized live-evaluation marker."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.config_loader import ConfigLoader  # noqa: E402
from services.evaluation.acceptance import TextAcceptanceRunner  # noqa: E402
from services.evaluation.harness import ScenarioEvaluationHarness  # noqa: E402
from services.evaluation.review import build_live_artifact, finalize_human_review  # noqa: E402
from interfaces.evaluation import ObservedOutcome  # noqa: E402


def _read_observed(path: Path) -> tuple[ObservedOutcome, ...]:
    value = json.loads(path.read_text(encoding="utf-8"))
    rows = value.get("observations") if isinstance(value, dict) else value
    if not isinstance(rows, list):
        raise ValueError("observed file must contain an observations list")
    return tuple(
        ObservedOutcome(
            scenario_id=str(row.get("scenario_id") or ""),
            action=_optional_text(row.get("action")),
            state=_optional_text(row.get("state")),
            invariants=dict(row.get("invariants") or {}),
            source_refs=tuple(str(item) for item in row.get("source_refs") or ()),
        )
        for row in rows
        if isinstance(row, dict)
    )


def _optional_text(value: Any) -> str | None:
    return str(value) if value is not None else None


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observed", type=Path)
    parser.add_argument("--review", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--run-id", default="manual-live-eval")
    parser.add_argument("--validate-suite", action="store_true")
    parser.add_argument("--acceptance", action="store_true")
    parser.add_argument("--seed", type=int)
    args = parser.parse_args(argv)
    try:
        if args.review:
            reviewed = finalize_human_review(json.loads(args.review.read_text(encoding="utf-8")))
            if args.output:
                _write_json(args.output, reviewed)
            print(json.dumps(reviewed, ensure_ascii=False, indent=2))
            return 0 if reviewed["status"] == "passed" else 1

        loader = ConfigLoader(Path("config"))
        loader.load_all()
        enabled = bool(loader.get(
            "features", "features.evaluation_harness.enabled", False,
        ))
        harness = ScenarioEvaluationHarness.from_loader(loader, enabled=enabled)
        if args.acceptance:
            acceptance_enabled = enabled and bool(loader.get(
                "features", "features.evaluation_acceptance.enabled", False,
            ))
            runner = TextAcceptanceRunner.from_loader(
                loader, enabled=acceptance_enabled,
            )
            artifact = runner.run(seed=args.seed)
            target = args.output or runner.artifact_file
            _write_json(target, artifact)
            print(json.dumps({
                "output": str(target),
                "status": artifact["status"],
                "passed": artifact["passed"],
                "seed": artifact["seed"],
                "scenario_count": artifact["scenario_count"],
                "sanitized": artifact["sanitized"],
            }, ensure_ascii=False))
            return 0 if artifact["passed"] else 1
        if args.validate_suite:
            summary = {
                "contract_id": harness.suite().contract_id,
                "scenario_count": len(harness.suite().scenarios),
                "groups": sorted({item.group.value for item in harness.suite().scenarios}),
                "feature_enabled": enabled,
            }
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 0
        if not args.observed or not args.output:
            raise ValueError("--observed and --output are required for a live evaluation")
        observations = _read_observed(args.observed)
        artifact = build_live_artifact(
            harness.suite(), harness.evaluate_many(observations), run_id=args.run_id,
        )
        _write_json(args.output, artifact)
        print(json.dumps({
            "output": str(args.output),
            "status": artifact["status"],
            "sanitized": True,
        }, ensure_ascii=False))
        return 0 if artifact["status"] == "passed" else 1
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
