from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

import pytest

from scripts.run_release_verification import (
    VerificationCommandResult,
    default_verification_output,
    run_release_verification,
)
from services.evaluation.release_gate import SourceState


REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 20, 8, 0, tzinfo=timezone.utc)
REVISION = "a" * 40


class FixedRunner:
    def __init__(self, *, failing_call: int | None = None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.failing_call = failing_call

    def __call__(
        self, command: Sequence[str], cwd: Path,
    ) -> VerificationCommandResult:
        assert cwd == REPO_ROOT
        self.calls.append(tuple(command))
        call = len(self.calls)
        if call == 5:
            return VerificationCommandResult(
                0, json.dumps({"summary": {"pass": 3, "fail": 0, "skip": 1}}),
            )
        if call == self.failing_call:
            return VerificationCommandResult(1, "1 failed, 2 passed in 0.02s")
        return VerificationCommandResult(0, "3 passed, 1 skipped in 0.01s")


def test_fixed_runner_emits_source_bound_verification_matrix() -> None:
    runner = FixedRunner()
    report = run_release_verification(
        REPO_ROOT, runner=runner, source_state=SourceState(REVISION, True),
        now_utc=NOW, python_exe="python311.exe",
    )
    assert report["status"] == "passed"
    assert report["source_revision"] == REVISION
    assert report["clean_worktree"] is True
    assert set(report["test_groups"]) == {"targeted", "offline", "llm", "slow", "smoke"}
    assert all(value == 0 for value in report["correctness"].values())
    assert len(runner.calls) == 5
    assert all(command[0] == "python311.exe" for command in runner.calls)
    assert all("shell" not in command for command in runner.calls)


def test_default_verification_output_does_not_dirty_source_tree() -> None:
    assert default_verification_output(REPO_ROOT, "2.0.0") == (
        REPO_ROOT / "logs" / "operations" / "release_verification_2_0_0.json"
    )


def test_any_fixed_group_failure_fails_correctness_evidence() -> None:
    report = run_release_verification(
        REPO_ROOT, runner=FixedRunner(failing_call=2),
        source_state=SourceState(REVISION, True), now_utc=NOW,
    )
    assert report["status"] == "failed"
    assert report["test_groups"]["offline"]["failures"] == 1
    assert all(value == 1 for value in report["correctness"].values())


def test_release_verification_refuses_dirty_worktree() -> None:
    with pytest.raises(RuntimeError, match="clean worktree"):
        run_release_verification(
            REPO_ROOT, runner=FixedRunner(),
            source_state=SourceState(REVISION, False), now_utc=NOW,
        )


def test_release_verification_requires_timezone_aware_clock() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        run_release_verification(
            REPO_ROOT, runner=FixedRunner(),
            source_state=SourceState(REVISION, True), now_utc=datetime(2026, 8, 20),
        )
