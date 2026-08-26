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


def test_architecture_inventory_records_s5_owner_review_checkpoint() -> None:
    inventory = _inventory()
    normalization = inventory["normalization"]
    assert isinstance(normalization, dict)

    assert inventory["status"] == "s5_implementation_owner_review_pending"
    assert inventory["checkpoint_revision"] == (
        "361bc44c0375610a89933fb13a001595c2847a53"
    )
    assert inventory["checkpoint_scope_clean"] is True
    assert inventory["structure_gate_eligible"] is True
    assert inventory["release_gate_eligible"] is False
    assert normalization["completed_waves"] == ["S0", "S1", "S2", "S3", "S4"]
    assert normalization["active_wave"] == "S5"
    assert normalization["canonical_config_owner"] == "config/state.yaml"
    assert normalization["canonical_kernel_config_owner"] == "config/kernel.yaml"
    assert normalization["canonical_execution_config_owner"] == "config/execution.yaml"
    assert normalization["compatibility_config_files"] == [
        "config/agent_state.yaml",
        "config/relationships.yaml",
    ]

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
    assert isinstance(overrides, list)
    seen: set[str] = set()
    for item in overrides:
        assert isinstance(item, dict)
        path = str(item["path"])
        assert path not in seen
        seen.add(path)
        assert (ROOT / path).is_file(), path
        assert item["disposition"] in ALLOWED
        assert item.get("target_owner")
        if item["disposition"] == "DELETE":
            assert item.get("deletion_gate")

    policy = inventory["deletion_policy"]
    assert isinstance(policy, dict)
    assert policy["source_deletion_authorized"] is False


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
