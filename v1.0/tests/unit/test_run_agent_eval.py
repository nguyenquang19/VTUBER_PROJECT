from __future__ import annotations

import json
from pathlib import Path

from scripts.run_agent_eval import main


def test_cli_validates_versioned_suite(capsys) -> None:
    assert main(["--validate-suite"]) == 0
    value = json.loads(capsys.readouterr().out)
    assert value["contract_id"] == "mai-agent-v1"
    assert value["scenario_count"] == 12
    assert value["feature_enabled"] is True


def test_cli_writes_sanitized_pending_marker(tmp_path: Path) -> None:
    observed = tmp_path / "observed.json"
    output = tmp_path / "artifact.json"
    observed.write_text(json.dumps({"observations": [{
        "scenario_id": "continuity.promise_evidence",
        "action": "continue_thread",
        "invariants": {"invented_event_ids": 0},
        "source_refs": ["private@example.com"],
    }]}), encoding="utf-8")
    assert main([
        "--observed", str(observed), "--output", str(output), "--run-id", "test",
    ]) == 1
    artifact = json.loads(output.read_text(encoding="utf-8"))
    assert artifact["status"] == "pending_human_review"
    assert "private@example.com" not in output.read_text(encoding="utf-8")
