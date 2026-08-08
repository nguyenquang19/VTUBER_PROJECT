"""Compare a candidate to a versioned M8 baseline; never promote automatically."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.config_loader import ConfigLoader  # noqa: E402
from services.evaluation.readiness import CandidateMetrics, compare_candidate  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        baseline = CandidateMetrics.from_mapping(json.loads(args.baseline.read_text(encoding="utf-8")))
        candidate = CandidateMetrics.from_mapping(json.loads(args.candidate.read_text(encoding="utf-8")))
        loader = ConfigLoader(Path("config"))
        loader.load_all()
        gate = loader.get("evaluation", "fine_tune_gate", {})
        result = compare_candidate(
            baseline, candidate,
            max_safety_regression=int(gate["max_safety_regression"]),
            max_latency_increase_percent=float(gate["max_latency_increase_percent"]),
        )
        rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered, encoding="utf-8")
        print(rendered, end="")
        return 0 if result["status"] == "passed" else 1
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
