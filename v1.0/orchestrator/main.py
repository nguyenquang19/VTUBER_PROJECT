"""Compatibility shim for the retired dashboard-only bootstrap command.

Mai's production composition root is :mod:`orchestrator.stream_runtime` and is
started by ``scripts\\start_live.ps1`` through a platform entrypoint.  Keeping a
fail-fast module here prevents old operator notes from starting a second,
incomplete runtime that appears healthy while lacking LLM, TTS, and Director.
"""
from __future__ import annotations

import sys
from typing import NoReturn

LEGACY_ENTRYPOINT_EXIT_CODE = 2
LEGACY_ENTRYPOINT_MESSAGE = (
    "orchestrator.main is not a production runtime. "
    "Use .\\scripts\\start_live.ps1 -Platform youtube -VideoId \"VIDEO_ID\" "
    "or .\\scripts\\start_live.ps1 -Platform discord."
)


def main() -> NoReturn:
    """Exit without composing services, binding ports, or mutating runtime data."""
    print(LEGACY_ENTRYPOINT_MESSAGE, file=sys.stderr)
    raise SystemExit(LEGACY_ENTRYPOINT_EXIT_CODE)


if __name__ == "__main__":
    main()
