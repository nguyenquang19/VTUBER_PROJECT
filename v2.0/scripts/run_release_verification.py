"""Run the fixed Phase 15 verification matrix and emit current-revision evidence."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from orchestrator.config_loader import ConfigLoader  # noqa: E402
from scripts.build_release_evidence import _write_json_atomic  # noqa: E402
from services.evaluation.release_gate import (  # noqa: E402
    ReleaseReadinessConfig,
    SourceState,
    inspect_source_state,
)


@dataclass(frozen=True)
class VerificationCommandResult:
    returncode: int
    stdout: str
    stderr: str = ""


CommandRunner = Callable[[Sequence[str], Path], VerificationCommandResult]


def default_verification_output(repo_root: Path, target_version: str) -> Path:
    version = target_version.replace(".", "_")
    return repo_root / "logs" / "operations" / f"release_verification_{version}.json"


def _fixed_commands(python_exe: str) -> dict[str, tuple[str, ...]]:
    return {
        "targeted": (
            python_exe, "-m", "pytest",
            "tests/unit/test_build_release_evidence.py",
            "tests/unit/test_release_verification.py",
            "tests/unit/test_closed_loop_canary.py",
            "tests/unit/test_operations_rehearsal.py",
            "tests/unit/test_live_preflight.py",
            "tests/unit/test_capability_registry.py",
            "tests/unit/test_action_transaction.py",
            "tests/unit/test_general_action_mock_loop.py",
            "tests/integration/test_action_transaction.py",
            "tests/integration/test_external_action_transaction.py",
            "tests/integration/test_closed_loop_canary_workflow.py", "-q",
        ),
        "offline": (
            python_exe, "-m", "pytest", "tests", "-m", "not llm and not slow", "-q",
        ),
        "llm": (python_exe, "-m", "pytest", "tests", "-m", "llm", "-q"),
        "slow": (python_exe, "-m", "pytest", "tests", "-m", "slow", "-q"),
        "smoke": (
            python_exe, "scripts/smoke_offline.py", "--output-format", "Json",
        ),
    }


def _subprocess_runner(command: Sequence[str], cwd: Path) -> VerificationCommandResult:
    completed = subprocess.run(
        list(command), cwd=cwd, capture_output=True, text=True, encoding="utf-8",
        errors="replace", check=False,
    )
    return VerificationCommandResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _pytest_counts(output: str, returncode: int) -> tuple[int, int, int, int]:
    def count(label: str) -> int:
        match = re.search(rf"(\d+)\s+{label}\b", output)
        return int(match.group(1)) if match else 0

    passed = count("passed")
    failed = count("failed")
    skipped = count("skipped")
    deselected = count("deselected")
    if returncode != 0 and failed == 0:
        failed = 1
    return passed, failed, skipped, deselected


def _smoke_counts(output: str, returncode: int) -> tuple[int, int, int, int]:
    try:
        value = json.loads(output)
        summary = value["summary"]
        passed = int(summary["pass"])
        failed = int(summary["fail"])
        skipped = int(summary["skip"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return 0, 1, 0, 0
    if returncode != 0 and failed == 0:
        failed = 1
    return passed, failed, skipped, 0


def run_release_verification(
    repo_root: Path = REPO_ROOT,
    *,
    runner: CommandRunner | None = None,
    source_state: SourceState | None = None,
    now_utc: datetime | None = None,
    python_exe: str | None = None,
) -> dict[str, object]:
    root = repo_root.resolve()
    loader = ConfigLoader(root / "config")
    loader.load_all()
    config = ReleaseReadinessConfig.from_loader(loader)
    source = source_state or inspect_source_state(root)
    if not source.clean:
        raise RuntimeError("release verification requires a clean worktree")
    generated = now_utc or datetime.now(timezone.utc)
    if generated.tzinfo is None or generated.utcoffset() is None:
        raise ValueError("now_utc must be timezone-aware")
    commands = _fixed_commands(python_exe or sys.executable)
    if tuple(commands) != config.required_test_groups:
        raise ValueError("fixed verification groups disagree with release config")
    execute = runner or _subprocess_runner
    groups: dict[str, dict[str, object]] = {}
    for name in config.required_test_groups:
        started = time.perf_counter()
        result = execute(commands[name], root)
        duration = max(0.0, time.perf_counter() - started)
        combined = f"{result.stdout}\n{result.stderr}"
        counts = (
            _smoke_counts(result.stdout, result.returncode)
            if name == "smoke"
            else _pytest_counts(combined, result.returncode)
        )
        groups[name] = {
            "command_id": name,
            "passed": counts[0],
            "failures": counts[1],
            "skipped": counts[2],
            "deselected": counts[3],
            "duration_seconds": round(duration, 6),
        }
    matrix_passed = all(
        row["passed"] > 0 and row["failures"] == 0 for row in groups.values()
    )
    correctness = {
        name: 0 if matrix_passed else 1 for name in config.correctness_zero_counters
    }
    return {
        "schema_version": config.schema_version,
        "marker": "mai_release_verification",
        "sanitized": True,
        "source_revision": source.revision,
        "current_product_version": str(loader.get("system", "app.version", "")),
        "target_product_version": config.target_version,
        "generated_at_utc": generated.astimezone(timezone.utc).isoformat(),
        "runner_id": "mai-fixed-verification-v1",
        "clean_worktree": True,
        "test_groups": groups,
        "correctness": correctness,
        "bounded_state_passed": (
            groups["slow"]["passed"] > 0 and groups["slow"]["failures"] == 0
        ),
        "status": "passed" if matrix_passed else "failed",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = run_release_verification()
    output = args.output or default_verification_output(
        REPO_ROOT, str(report["target_product_version"]),
    )
    _write_json_atomic(output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
