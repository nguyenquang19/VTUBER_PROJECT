"""Build or finalize a sanitized blind Mood v1/v2 human review artifact."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.config_loader import ConfigLoader  # noqa: E402
from services.evaluation.mood_ab import MoodABReview  # noqa: E402


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--finalize", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        loader = ConfigLoader(Path("config"))
        loader.load_all()
        service = MoodABReview.from_loader(loader)
        if args.finalize:
            artifact = service.finalize(dict(_read(args.finalize)))
        else:
            if not args.input:
                raise ValueError("--input is required when not finalizing")
            raw = _read(args.input)
            comparisons = raw.get("comparisons") if isinstance(raw, dict) else raw
            if not isinstance(comparisons, list):
                raise ValueError("mood A/B input must be a comparisons list")
            artifact = service.build(tuple(dict(item) for item in comparisons if isinstance(item, dict)))
        _write(args.output, artifact)
        print(json.dumps({
            "output": str(args.output), "status": artifact["status"],
            "turn_count": artifact["turn_count"], "sanitized": artifact["sanitized"],
        }, ensure_ascii=False))
        return 0 if artifact.get("passed") is True else 1
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
