"""Evaluate an M8 fine-tune statistics artifact against configured gates."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.config_loader import ConfigLoader  # noqa: E402
from services.evaluation.readiness import (  # noqa: E402
    FineTuneThresholds,
    ReadinessStats,
    assess_finetune_readiness,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stats", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        value = json.loads(args.stats.read_text(encoding="utf-8"))
        stats = ReadinessStats(**value["stats"])
        loader = ConfigLoader(Path("config"))
        loader.load_all()
        thresholds = FineTuneThresholds.from_mapping(
            loader.get("evaluation", "fine_tune_gate", {})
        )
        result = assess_finetune_readiness(
            stats, thresholds,
            contract_id=str(loader.get("evaluation", "evaluation.contract_id")),
        )
        rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered, encoding="utf-8")
        print(rendered, end="")
        return 0 if result["status"] == "ready" else 1
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
