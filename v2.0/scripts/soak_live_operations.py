"""Run the controlled M9 live-operations soak and write its gate report."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.config_loader import ConfigLoader  # noqa: E402
from services.evaluation.soak import ControlledSoakMonitor, SoakConfig  # noqa: E402


async def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=float)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    loader = ConfigLoader(Path("config"))
    loader.load_all()
    config = SoakConfig.from_loader(loader)
    if args.output is not None:
        config = SoakConfig(**{**config.__dict__, "report_file": args.output})
    monitor = ControlledSoakMonitor(config)
    report = await monitor.run(args.duration)
    print(json.dumps({
        "passed": report["passed"], "elapsed_s": report["elapsed_s"],
        "measurements": report["measurements"], "report": str(config.report_file),
    }, ensure_ascii=False))
    return 0 if report["passed"] else 1


def main() -> None:
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()
