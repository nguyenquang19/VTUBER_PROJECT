"""Pytest-wide safety setup.

Test cases intentionally emit failures such as ``boom`` and ``timeout``. Route
those records away from the live runtime logs before test modules are imported.
"""
from __future__ import annotations

from pathlib import Path

from orchestrator.logger import setup_logging


def pytest_configure(config) -> None:
    log_dir = Path(config.rootpath) / "test-results" / "pytest-logs"
    setup_logging(
        level="ERROR",
        console_enabled=False,
        jsonl_enabled=False,
        log_dir=log_dir,
    )

