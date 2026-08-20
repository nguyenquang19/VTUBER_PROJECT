"""Build or finalize a strict MAI-HLC blind comparison artifact."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.config_loader import ConfigLoader  # noqa: E402
from services.evaluation.human_like import HumanLikeCalibration  # noqa: E402


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    temporary.replace(path)


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
            print(json.dumps({
                "output": str(args.output),
                "status": final["status"],
                "previous_build_delta": final["previous_build_delta"],
                "automatic_release_decision": False,
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
