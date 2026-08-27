"""Production entrypoint ownership and legacy-command regression."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator.main import (
    LEGACY_ENTRYPOINT_EXIT_CODE,
    LEGACY_ENTRYPOINT_MESSAGE,
    main,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_legacy_main_fails_fast_without_composing_services(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        main()

    assert raised.value.code == LEGACY_ENTRYPOINT_EXIT_CODE
    assert LEGACY_ENTRYPOINT_MESSAGE in capsys.readouterr().err


def test_module_command_exits_with_operator_guidance() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "orchestrator.main"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == LEGACY_ENTRYPOINT_EXIT_CODE
    assert "start_live.ps1" in result.stderr
    assert result.stdout == ""


def test_legacy_main_has_no_parallel_runtime_dependencies() -> None:
    source = (REPO_ROOT / "orchestrator" / "main.py").read_text(encoding="utf-8")
    forbidden = (
        "DashboardServer",
        "ConversationStateMachine",
        "TriggerManager",
        "MigrationRunner",
        "FeatureManager",
        "uvicorn",
        "asyncio.run",
    )
    assert [name for name in forbidden if name in source] == []


def test_canonical_launcher_dispatches_only_platform_stream_entrypoints() -> None:
    launcher = (REPO_ROOT / "scripts" / "start_live.ps1").read_text(encoding="utf-8")
    assert '"stream_youtube.py"' in launcher
    assert '"stream_discord.py"' in launcher
    assert "orchestrator.main" not in launcher

    for name in ("stream_youtube.py", "stream_discord.py"):
        source = (REPO_ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert "from orchestrator.stream_runtime import" in source
        assert "build_stream_runtime(" in source
        assert "run_stream_runtime(rt)" in source
        assert "argparse.BooleanOptionalAction" in source
        assert "default=True" in source

    assert 'if ($NoMemory) { $RuntimeArgs += "--no-memory" }' in launcher
