"""Evaluate sanitized aggregate evidence against the Mai 2.0.0 release gates."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from orchestrator.config_loader import ConfigLoader  # noqa: E402
from services.evaluation.release_gates import ProductReleaseGateEvaluator  # noqa: E402


def _without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _read_evidence(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_without_duplicate_keys)
    if not isinstance(value, dict):
        raise ValueError("release evidence must be a JSON object")
    return value


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    rendered = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(rendered, encoding="utf-8")
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def evaluate_release_evidence(
    evidence_path: Path,
    *,
    config_dir: Path = REPO_ROOT / "config",
) -> dict[str, Any]:
    loader = ConfigLoader(config_dir)
    loader.load_all()
    evaluator = ProductReleaseGateEvaluator.from_loader(loader, enabled=True)
    try:
        evidence = _read_evidence(evidence_path)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        evidence = {}
    return evaluator.evaluate(evidence)


def main() -> int:
    parser = argparse.ArgumentParser(description="Fail-closed Mai 2.0.0 release gate evaluator")
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--output")
    parser.add_argument("--config-dir", default=str(REPO_ROOT / "config"))
    args = parser.parse_args()

    report = evaluate_release_evidence(
        Path(args.evidence), config_dir=Path(args.config_dir),
    )
    if args.output:
        _write_json_atomic(Path(args.output), report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["release_eligible"] else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())