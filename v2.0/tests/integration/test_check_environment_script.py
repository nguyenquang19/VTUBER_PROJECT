"""Integration tests for the Windows M0.1 environment preflight script."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_environment.ps1"


def _powershell_executable() -> str:
    return "powershell.exe"


@pytest.fixture
def fake_project(tmp_path: Path) -> Path:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    llama_binary = tmp_path / "tools" / "llama-server.exe"
    llama_binary.parent.mkdir()
    llama_binary.touch()
    model_path = tmp_path / "models" / "mai.gguf"
    model_path.parent.mkdir()
    model_path.touch()
    reference_audio = tmp_path / "models" / "mai.wav"
    reference_audio.touch()

    config = {
        "llm_main": {
            "binary": str(llama_binary),
            "model_path": str(model_path),
            "host": "127.0.0.1",
            "port": 8080,
        },
        "tts": {"reference_audio": str(reference_audio)},
    }
    (config_dir / "models.yaml").write_text(
        yaml.safe_dump(config, allow_unicode=True),
        encoding="utf-8",
    )
    return tmp_path


def _run_check(
    project_root: Path,
    python_path: Path | None = None,
    *,
    script_path: Path = SCRIPT_PATH,
    pass_project_root: bool = True,
) -> subprocess.CompletedProcess[str]:
    command = [
        _powershell_executable(),
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script_path),
        "-PythonPath",
        str(python_path or Path(sys.executable)),
        "-OutputFormat",
        "Json",
        "-SkipCudaCheck",
        "-SkipLlamaHealth",
    ]
    if pass_project_root:
        command[6:6] = ["-ProjectRoot", str(project_root)]
    return subprocess.run(command, capture_output=True, text=True, check=False, timeout=30)


def _json_output(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    assert result.stdout, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_preflight_passes_for_complete_fixture(fake_project: Path) -> None:
    result = _run_check(fake_project)
    payload = _json_output(result)

    assert result.returncode == 0, result.stderr
    assert payload["ok"] is True
    checks = {item["name"]: item for item in payload["checks"]}
    assert checks["python"]["status"] == "PASS"
    assert checks["llama_binary"]["status"] == "PASS"
    assert checks["llm_model"]["status"] == "PASS"
    assert checks["cuda"]["status"] == "SKIP"


def test_preflight_defaults_project_root_from_script_location(fake_project: Path) -> None:
    copied_script = fake_project / "scripts" / SCRIPT_PATH.name
    copied_script.parent.mkdir()
    copied_script.write_text(SCRIPT_PATH.read_text(encoding="utf-8"), encoding="utf-8")

    result = _run_check(
        fake_project,
        script_path=copied_script,
        pass_project_root=False,
    )
    payload = _json_output(result)

    assert result.returncode == 0, result.stderr
    checks = {item["name"]: item for item in payload["checks"]}
    assert checks["project_root"]["message"] == str(fake_project)


def test_preflight_reports_missing_llama_binary(fake_project: Path) -> None:
    (fake_project / "tools" / "llama-server.exe").unlink()

    result = _run_check(fake_project)
    payload = _json_output(result)
    checks = {item["name"]: item for item in payload["checks"]}

    assert result.returncode == 1
    assert payload["ok"] is False
    assert checks["llama_binary"]["status"] == "FAIL"
    assert "llama-server.exe" in checks["llama_binary"]["message"]


def test_preflight_reports_missing_llm_model(fake_project: Path) -> None:
    (fake_project / "models" / "mai.gguf").unlink()

    result = _run_check(fake_project)
    payload = _json_output(result)
    checks = {item["name"]: item for item in payload["checks"]}

    assert result.returncode == 1
    assert payload["ok"] is False
    assert checks["llm_model"]["status"] == "FAIL"
    assert "GGUF model" in checks["llm_model"]["message"]


def test_preflight_reports_missing_python(fake_project: Path) -> None:
    result = _run_check(fake_project, fake_project / "missing-python.exe")
    payload = _json_output(result)
    checks = {item["name"]: item for item in payload["checks"]}

    assert result.returncode == 1
    assert checks["python"]["status"] == "FAIL"
    assert checks["models_config"]["status"] == "FAIL"
    assert checks["dependencies"]["status"] == "FAIL"


def test_preflight_reports_invalid_models_config(fake_project: Path) -> None:
    (fake_project / "config" / "models.yaml").write_text(
        yaml.safe_dump({"llm_main": {"port": 8080}}),
        encoding="utf-8",
    )

    result = _run_check(fake_project)
    payload = _json_output(result)
    checks = {item["name"]: item for item in payload["checks"]}

    assert result.returncode == 1
    assert checks["models_config"]["status"] == "FAIL"
    assert "missing keys" in checks["models_config"]["message"]
