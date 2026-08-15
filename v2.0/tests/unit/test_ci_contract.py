from __future__ import annotations

from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_ROOT = PROJECT_ROOT.parent
WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml"


def test_ci_workflow_is_offline_windows_python311_contract() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    workflow = yaml.safe_load(text)
    job = workflow["jobs"]["test"]

    assert job["runs-on"] == "windows-latest"
    assert workflow["defaults"]["run"]["working-directory"] == "v2.0"
    assert 'python-version: "3.11"' in text
    assert "requirements-ci.txt" in text
    assert 'not llm and not slow' in text
    assert "--junitxml=test-results/pytest.xml" in text
    assert "requirements.lock.txt" not in text


def test_ci_requirements_exclude_live_and_gpu_stacks() -> None:
    text = (PROJECT_ROOT / "requirements-ci.txt").read_text(encoding="utf-8").lower()
    for forbidden in (
        "llama-cpp", "torch", "vieneu", "transformers", "pytchat", "discord.py",
        "sentence-transformers", "sounddevice", "pyaudio",
    ):
        assert forbidden not in text
