"""Export the M9 post-stream operations checklist."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.config_loader import ConfigLoader  # noqa: E402
from services.operations.post_stream_review import PostStreamReviewer, ReviewConfig  # noqa: E402


async def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    loader = ConfigLoader(Path("config"))
    loader.load_all()
    report = await PostStreamReviewer(ReviewConfig.from_loader(loader)).review(args.output)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["ready"] else 1


def main() -> None:
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()
