from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts.evaluate_product_release_gates import evaluate_release_evidence


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_release_gate_script_fails_closed_for_malformed_evidence(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.json"
    evidence.write_text('{"prompt": "do not expose this secret"}', encoding="utf-8")

    report = evaluate_release_evidence(evidence)

    assert report["release_eligible"] is False
    assert report["failed_gates"] == ["invalid_evidence"]
    assert "do not expose" not in str(report)


def test_release_gate_cli_writes_sanitized_blocked_report(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.json"
    evidence.write_text("not valid JSON", encoding="utf-8")
    output = tmp_path / "report.json"

    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "evaluate_product_release_gates.py"),
            "--evidence", str(evidence), "--output", str(output),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert result.returncode == 1
    assert report["sanitized"] is True
    assert report["release_authorized"] is False
    assert report["release_eligible"] is False