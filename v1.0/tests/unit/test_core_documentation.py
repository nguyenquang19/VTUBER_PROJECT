"""Guards that keep the frozen Mai v1.0.0 documentation aligned with config."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from orchestrator.config_loader import CONFIG_FILES


ROOT = Path(__file__).resolve().parents[2]
PRODUCT_VERSION = "1.4.0"
CORE_DOCS = (
    "00_V1_0_BASELINE.md",
    "01_SYSTEM_OVERVIEW.md",
    "02_DATA_PIPELINE.md",
    "03_COMPONENT_REFERENCE.md",
    "04_DATA_AND_STORAGE.md",
    "05_CONFIGURATION.md",
    "06_OPERATIONS_AND_TROUBLESHOOTING.md",
    "07_TESTING_AND_EXTENSION.md",
    "08_SECURITY_RECOVERY.md",
)
CRITICAL_PATHS = (
    "config/system.yaml",
    "config/features.yaml",
    "config/evaluation.yaml",
    "eval/contracts/mai_agent_v1.yaml",
    "eval/scenarios/mai_agent_v1.yaml",
    "orchestrator/stream_runtime.py",
    "orchestrator/logger.py",
    "interfaces/tts.py",
    "interfaces/evaluation.py",
    "services/director/director_loop.py",
    "services/autonomy/self_talk_planner.py",
    "services/autonomy/lore_material.py",
    "services/llm/llm_turn.py",
    "services/tts/tts_pipeline.py",
    "services/evaluation/data_quality.py",
    "scripts/start_live.ps1",
    "scripts/cli.py",
    "scripts/live_preflight.py",
    "scripts/simulate_youtube_replay.py",
    "scripts/stress_youtube_llm.py",
    "scripts/export_dataset.py",
    "scripts/backup_data.py",
    "scripts/restore_data.py",
)


def _yaml(relative: str) -> dict:
    value = yaml.safe_load((ROOT / relative).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _backtick_names(value: str) -> set[str]:
    return set(re.findall(r"`([a-z][a-z0-9_]*)`", value))


def test_product_version_is_pinned_across_baseline_documents() -> None:
    system = _yaml("config/system.yaml")
    assert system["app"]["version"] == PRODUCT_VERSION
    for relative in CORE_DOCS:
        text = (ROOT / "docs" / relative).read_text(encoding="utf-8")
        assert PRODUCT_VERSION in text, f"missing product version in {relative}"
    assert PRODUCT_VERSION in (ROOT / "README.md").read_text(encoding="utf-8")
    assert PRODUCT_VERSION in (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert f"[{PRODUCT_VERSION}]" in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


def test_documented_feature_inventory_matches_feature_yaml() -> None:
    features = _yaml("config/features.yaml")["features"]
    enabled = {name for name, config in features.items() if config["enabled"] is True}
    disabled = set(features) - enabled
    baseline = (ROOT / "docs" / CORE_DOCS[0]).read_text(encoding="utf-8")
    enabled_text, remainder = baseline.split("Disabled/optional toggle ở baseline:", 1)
    enabled_text = enabled_text.split("Enabled toggle ở baseline:", 1)[1]
    disabled_text = remainder.split("`animation_smooth=true`", 1)[0]
    assert _backtick_names(enabled_text) == enabled
    assert _backtick_names(disabled_text) == disabled


def test_documented_config_inventory_matches_loader_registry() -> None:
    configuration_doc = (ROOT / "docs" / "05_CONFIGURATION.md").read_text(
        encoding="utf-8",
    )
    for filename in CONFIG_FILES.values():
        assert f"`{filename}`" in configuration_doc, f"undocumented config: {filename}"


def test_runtime_data_schema_view_matches_frozen_contract() -> None:
    runtime = _yaml("config/evaluation.yaml")["data_contract"]
    contract = _yaml("eval/contracts/mai_agent_v1.yaml")
    for field in (
        "architecture_version",
        "persona_version",
        "context_schema_version",
        "agenda_policy_version",
        "turn_schema_version",
        "sft_schema_version",
        "dpo_schema_version",
    ):
        assert runtime[field] == contract[field], f"data contract mismatch: {field}"


def test_relative_markdown_links_in_core_docs_resolve() -> None:
    files = [ROOT / "README.md", ROOT / "docs" / "README.md"] + [
        ROOT / "docs" / relative for relative in CORE_DOCS
    ]
    pattern = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
    for document in files:
        text = document.read_text(encoding="utf-8")
        for target in pattern.findall(text):
            if "://" in target or target.startswith("#"):
                continue
            path_text = target.split("#", 1)[0]
            resolved = (document.parent / path_text).resolve()
            assert resolved.exists(), f"broken link in {document.name}: {target}"


def test_documented_critical_entrypoints_exist() -> None:
    for relative in CRITICAL_PATHS:
        assert (ROOT / relative).exists(), f"documented critical path missing: {relative}"


def test_core_document_index_links_every_versioned_document() -> None:
    index = (ROOT / "docs" / "README.md").read_text(encoding="utf-8")
    for relative in CORE_DOCS:
        assert f"({relative})" in index, f"core document missing from index: {relative}"
