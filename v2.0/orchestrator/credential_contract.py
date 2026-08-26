"""Strict, sanitized credential boundary for environment and local secret files."""
from __future__ import annotations

import contextlib
import os
import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping


_ENVIRONMENT_REFERENCE = re.compile(r"^[A-Z][A-Z0-9_]*$")


class CredentialState(str, Enum):
    PRESENT = "present"
    MISSING = "missing"
    INVALID = "invalid"


class CredentialContractError(RuntimeError):
    """Sanitized failure that never retains a credential value."""

    def __init__(self, reason_code: str, *, source: str) -> None:
        self.reason_code = reason_code
        self.source = source
        super().__init__(f"{reason_code}: {source}")


@dataclass(frozen=True)
class CredentialInspection:
    reference: str
    state: CredentialState

    @property
    def present(self) -> bool:
        return self.state is CredentialState.PRESENT


@dataclass(frozen=True)
class RuntimeCredentialReferences:
    discord_token: str
    obs_password: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "discord_token",
            validate_environment_reference(
                self.discord_token, "discord.token_env_var",
            ),
        )
        object.__setattr__(
            self,
            "obs_password",
            validate_environment_reference(
                self.obs_password, "execution.external.obs.password_env",
            ),
        )
        if self.discord_token == self.obs_password:
            raise ValueError("credential environment references must be distinct")

    @classmethod
    def from_loader(cls, loader: Any) -> "RuntimeCredentialReferences":
        return cls(
            discord_token=loader.get(
                "chat_sources", "discord.token_env_var", "DISCORD_BOT_TOKEN",
            ),
            obs_password=loader.get(
                "execution",
                "external.obs.password_env",
                "OBS_WEBSOCKET_PASSWORD",
            ),
        )


def validate_runtime_credential_contract(loader: Any) -> RuntimeCredentialReferences:
    references = RuntimeCredentialReferences.from_loader(loader)
    dashboard_reference = dashboard_control_token_reference(loader)
    if dashboard_reference in {references.discord_token, references.obs_password}:
        raise ValueError("credential environment references must be distinct")
    validate_secret_file_reference(
        loader.get("animation", "animation.token_file", "vts_token.txt"),
        "animation.token_file",
    )
    return references


def dashboard_control_token_reference(loader: Any) -> str:
    return validate_environment_reference(
        loader.get(
            "system", "dashboard.control_token_env", "MAI_DASHBOARD_CONTROL_TOKEN",
        ),
        "dashboard.control_token_env",
    )


def require_dashboard_control_token(
    loader: Any,
    environ: Mapping[str, str] | None = None,
) -> str:
    reference = dashboard_control_token_reference(loader)
    value = require_environment_secret(os.environ if environ is None else environ, reference)
    return validate_dashboard_control_token(value, source=reference)


def validate_dashboard_control_token(value: object, *, source: str) -> str:
    secret = validate_secret_value(value, source=source)
    if len(secret) < 24:
        raise CredentialContractError("credential_invalid", source=source)
    return secret


def validate_environment_reference(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not _ENVIRONMENT_REFERENCE.fullmatch(value):
        raise ValueError(f"{field_name} must be canonical UPPER_SNAKE_CASE")
    return value


def validate_secret_file_reference(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field_name} must be a canonical non-empty string")
    return value


def validate_secret_value(value: object, *, source: str) -> str:
    if not isinstance(value, str) or not value:
        raise CredentialContractError("credential_invalid", source=source)
    if value != value.strip() or any(
        unicodedata.category(character).startswith("C") for character in value
    ):
        raise CredentialContractError("credential_invalid", source=source)
    return value


def inspect_environment_secret(
    environ: Mapping[str, str],
    reference: str,
) -> CredentialInspection:
    reference = validate_environment_reference(reference, "credential reference")
    if reference not in environ or environ.get(reference) == "":
        return CredentialInspection(reference, CredentialState.MISSING)
    try:
        validate_secret_value(environ.get(reference), source=reference)
    except CredentialContractError:
        return CredentialInspection(reference, CredentialState.INVALID)
    return CredentialInspection(reference, CredentialState.PRESENT)


def require_environment_secret(
    environ: Mapping[str, str],
    reference: str,
) -> str:
    reference = validate_environment_reference(reference, "credential reference")
    if reference not in environ:
        raise CredentialContractError("credential_missing", source=reference)
    value = environ.get(reference)
    if value == "":
        raise CredentialContractError("credential_missing", source=reference)
    return validate_secret_value(value, source=reference)


def read_optional_secret_file(path: str | Path) -> str | None:
    target = Path(path)
    if not target.exists():
        return None
    try:
        value = target.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise CredentialContractError(
            "credential_file_unreadable", source=str(target),
        ) from exc
    return validate_secret_value(value, source=str(target))


def write_secret_file_atomic(path: str | Path, value: object) -> None:
    target = Path(path)
    secret = validate_secret_value(value, source=str(target))
    temporary = target.with_suffix(target.suffix + ".tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(secret, encoding="utf-8")
        temporary.replace(target)
    except OSError as exc:
        raise CredentialContractError(
            "credential_file_write_failed", source=str(target),
        ) from exc
    finally:
        if temporary.exists():
            with contextlib.suppress(OSError):
                temporary.unlink()
