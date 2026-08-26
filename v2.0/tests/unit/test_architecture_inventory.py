"""Machine-checkable coverage guard for the canonical structure inventory."""
from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
INVENTORY_PATH = ROOT / "eval" / "architecture_inventory_v1.yaml"
ALLOWED = {"KEEP", "MERGE", "DELETE", "OFFLINE", "ARCHIVE"}


def _inventory() -> dict[str, object]:
    value = yaml.safe_load(INVENTORY_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_architecture_inventory_snapshot_matches_current_tree() -> None:
    inventory = _inventory()
    snapshot = inventory["snapshot"]
    assert isinstance(snapshot, dict)

    production = tuple(
        path
        for directory in ("interfaces", "orchestrator", "services")
        for path in (ROOT / directory).rglob("*.py")
    )
    assert len(production) == snapshot["production_python_files"]
    assert len(tuple((ROOT / "config").rglob("*.yaml"))) == snapshot["config_yaml_files"]
    assert len(tuple((ROOT / "config" / "prompts").glob("*.txt"))) == snapshot["prompt_files"]
    assert len(tuple(path for path in (ROOT / "scripts").iterdir() if path.is_file())) == snapshot["script_files"]
    assert len(tuple((ROOT / "tests").rglob("*.py"))) == snapshot["test_python_files"]


def test_architecture_inventory_records_s8_docs_first_checkpoint() -> None:
    inventory = _inventory()
    normalization = inventory["normalization"]
    assert isinstance(normalization, dict)

    assert inventory["status"] == "s8_implementation_complete_owner_approved"
    assert inventory["checkpoint_revision"] == (
        "1f1b48b39c8504096c007498a995d916fd22a58b"
    )
    assert inventory["checkpoint_scope_clean"] is True
    assert inventory["structure_gate_eligible"] is True
    assert inventory["release_gate_eligible"] is False
    assert normalization["completed_waves"] == [
        "S0", "S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8",
    ]
    assert normalization["active_wave"] is None
    assert normalization["canonical_config_owner"] == "config/state.yaml"
    assert normalization["canonical_kernel_config_owner"] == "config/kernel.yaml"
    assert normalization["canonical_execution_config_owner"] == "config/execution.yaml"
    assert normalization["canonical_operations_config_owner"] == "config/operations.yaml"
    assert normalization["canonical_evaluation_config_owner"] == "config/evaluation.yaml"
    assert normalization["compatibility_config_files"] == []

    s5 = inventory["s5_execution_outcome"]
    assert isinstance(s5, dict)
    assert s5["status"] == "implementation_complete_owner_approved"
    s6 = inventory["s6_continuity"]
    assert isinstance(s6, dict)
    assert s6["status"] == "implementation_complete_owner_approved"
    assert s6["target_contract_owner"] == "interfaces/state.py"
    assert s6["target_commit_owner"] == "services/state/continuity.py"
    assert s6["target_config_owner"] == "config/state.yaml"
    assert s6["brain_takeover_authorized"] is False
    s7 = inventory["s7_operations"]
    assert isinstance(s7, dict)
    assert s7["status"] == "implementation_complete_owner_approved_committed"
    assert s7["target_metrics_owner"] == "services/operations/metrics.py"
    assert s7["target_journal_owner"] == "services/operations/turn_journal.py"
    assert s7["target_surface_owner"] == "services/operations/surface.py"
    assert s7["live_evaluation_imports_after"] == 0
    assert s7["live_dashboard_mutable_domain_dependencies_after"] == 0
    assert s7["brain_takeover_authorized"] is False

    s8 = inventory["s8_compaction"]
    assert isinstance(s8, dict)
    assert s8["status"] == "implementation_complete_owner_approved"
    assert s8["source_deletion_authorized"] is True
    assert s8["public_behavior_change_authorized"] is False
    assert len(s8["pure_re_exports_to_delete_after_import_migration"]) == 19
    assert len(s8["implementation_moves"]) == 4
    assert len(s8["dead_runtime_files_to_delete"]) == 9

    canonical_facades = set(normalization["canonical_import_facades"])
    compatibility_re_exports = set(normalization["compatibility_re_exports"])
    assert canonical_facades.isdisjoint(compatibility_re_exports)
    for relative in canonical_facades | compatibility_re_exports:
        assert (ROOT / relative).is_file(), relative


def test_architecture_inventory_rules_cover_every_scoped_file() -> None:
    inventory = _inventory()
    rules = inventory["path_rules"]
    assert isinstance(rules, list)
    covered: set[str] = set()
    for rule in rules:
        assert isinstance(rule, dict)
        assert rule["disposition"] in ALLOWED
        paths = tuple(ROOT.glob(str(rule["glob"])))
        assert paths, rule["glob"]
        covered.update(path.relative_to(ROOT).as_posix() for path in paths if path.is_file())

    expected: set[str] = set()
    for pattern in inventory["scope"]:
        expected.update(
            path.relative_to(ROOT).as_posix()
            for path in ROOT.glob(str(pattern))
            if path.is_file()
        )
    assert expected == covered


def test_architecture_inventory_overrides_are_valid_and_non_destructive() -> None:
    inventory = _inventory()
    overrides = inventory["overrides"]
    s8 = inventory["s8_compaction"]
    assert isinstance(overrides, list)
    assert isinstance(s8, dict)
    seen: set[str] = set()
    for item in overrides:
        assert isinstance(item, dict)
        path = str(item["path"])
        assert path not in seen
        seen.add(path)
        retired = {
            *s8["pure_re_exports_to_delete_after_import_migration"],
            *s8["duplicate_config_files_to_delete"],
            *s8["dead_runtime_files_to_delete"],
            *(move["from"] for move in s8["implementation_moves"]),
            *(path for path in s8["dashboard_compatibility_to_delete"] if not path.startswith("feature:")),
        }
        if path in retired:
            assert not (ROOT / path).exists(), path
        else:
            assert (ROOT / path).is_file(), path
        assert item["disposition"] in ALLOWED
        assert item.get("target_owner")
        if item["disposition"] == "DELETE":
            assert item.get("deletion_gate")

    policy = inventory["deletion_policy"]
    assert isinstance(policy, dict)
    assert policy["source_deletion_authorized"] is True


def test_architecture_inventory_covers_exact_feature_registry() -> None:
    inventory = _inventory()
    feature_inventory = inventory["features"]
    assert isinstance(feature_inventory, dict)
    configured = yaml.safe_load(
        (ROOT / "config" / "features.yaml").read_text(encoding="utf-8")
    )["features"]
    assert set(feature_inventory) == set(configured)
    assert len(feature_inventory) == inventory["snapshot"]["feature_count"]

    for name, item in feature_inventory.items():
        assert isinstance(item, dict), name
        assert item["enabled"] is configured[name]["enabled"], name
        assert item["disposition"] in ALLOWED, name
        assert item.get("target_owner"), name
        if item["disposition"] == "DELETE":
            assert item.get("deletion_gate"), name
