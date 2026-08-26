"""Strict and non-disclosing credential/environment contract tests."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from orchestrator.credential_contract import (
    CredentialContractError,
    CredentialState,
    RuntimeCredentialReferences,
    inspect_environment_secret,
    read_optional_secret_file,
    require_dashboard_control_token,
    require_environment_secret,
    validate_environment_reference,
    write_secret_file_atomic,
)


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "value",
    ("discord_token", " DISCORD_TOKEN", "DISCORD-TOKEN", "1_DISCORD_TOKEN", ""),
)
def test_environment_reference_requires_upper_snake_case(value: str) -> None:
    with pytest.raises(ValueError, match="UPPER_SNAKE_CASE"):
        validate_environment_reference(value, "credential")


def test_runtime_references_must_be_distinct() -> None:
    with pytest.raises(ValueError, match="distinct"):
        RuntimeCredentialReferences("SHARED_SECRET", "SHARED_SECRET")


def test_dashboard_control_token_is_required_and_bounded() -> None:
    loader = _Loader({
        ("system", "dashboard.control_token_env"): "MAI_DASHBOARD_CONTROL_TOKEN",
    })
    with pytest.raises(CredentialContractError, match="credential_missing"):
        require_dashboard_control_token(loader, {})
    with pytest.raises(CredentialContractError, match="credential_invalid"):
        require_dashboard_control_token(loader, {"MAI_DASHBOARD_CONTROL_TOKEN": "short"})
    assert require_dashboard_control_token(
        loader,
        {"MAI_DASHBOARD_CONTROL_TOKEN": "dashboard-control-token-123456"},
    ) == "dashboard-control-token-123456"


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({}, CredentialState.MISSING),
        ({"DISCORD_BOT_TOKEN": ""}, CredentialState.MISSING),
        ({"DISCORD_BOT_TOKEN": " token"}, CredentialState.INVALID),
        ({"DISCORD_BOT_TOKEN": "token\n"}, CredentialState.INVALID),
        ({"DISCORD_BOT_TOKEN": "exact-token"}, CredentialState.PRESENT),
    ],
)
def test_environment_secret_state_is_deterministic(
    environ: dict[str, str], expected: CredentialState,
) -> None:
    inspected = inspect_environment_secret(environ, "DISCORD_BOT_TOKEN")
    assert inspected.state is expected
    assert inspected.present is (expected is CredentialState.PRESENT)


def test_environment_secret_error_never_retains_raw_value() -> None:
    secret = " raw-secret "
    with pytest.raises(CredentialContractError) as raised:
        require_environment_secret({"DISCORD_BOT_TOKEN": secret}, "DISCORD_BOT_TOKEN")
    assert raised.value.reason_code == "credential_invalid"
    assert secret not in str(raised.value)
    assert secret not in repr(raised.value)


def test_secret_file_read_is_strict_and_does_not_trim(tmp_path: Path) -> None:
    target = tmp_path / "token.txt"
    target.write_text("token-with-newline\n", encoding="utf-8")

    with pytest.raises(CredentialContractError, match="credential_invalid") as raised:
        read_optional_secret_file(target)

    assert "token-with-newline" not in str(raised.value)


def test_secret_file_write_is_atomic_and_preserves_existing_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "token.txt"
    target.write_text("old-token", encoding="utf-8")
    secret = "new-secret"

    def fail_replace(self: Path, destination: Path) -> Path:
        raise OSError("replace failed")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(CredentialContractError, match="credential_file_write_failed") as raised:
        write_secret_file_atomic(target, secret)

    assert target.read_text(encoding="utf-8") == "old-token"
    assert not target.with_suffix(".txt.tmp").exists()
    assert secret not in str(raised.value)


def test_environment_example_is_exact_documentation_only_inventory() -> None:
    lines = {
        line.strip()
        for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert lines == {
        "DISCORD_BOT_TOKEN=",
        "MAI_DASHBOARD_CONTROL_TOKEN=",
        "OBS_WEBSOCKET_PASSWORD=",
    }
    assert "/vts_token.txt" in (ROOT / ".gitignore").read_text(encoding="utf-8")


class _Loader:
    def __init__(self, values: dict[tuple[str, str], Any]) -> None:
        self.values = values

    def get(self, name: str, key: str, default: Any = None) -> Any:
        return self.values.get((name, key), default)


def test_runtime_references_load_without_string_coercion() -> None:
    loader = _Loader({
        ("chat_sources", "discord.token_env_var"): 123,
        ("execution", "external.obs.password_env"): "OBS_PASSWORD",
    })
    with pytest.raises(ValueError, match="discord.token_env_var"):
        RuntimeCredentialReferences.from_loader(loader)
