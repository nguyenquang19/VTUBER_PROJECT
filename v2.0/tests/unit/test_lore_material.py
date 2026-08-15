"""Contract tests for bounded, delivery-aware lore material."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from orchestrator.config_loader import ConfigLoader
from services.autonomy.lore_material import (
    LoreMaterial,
    LoreMaterialProvider,
    parse_lore_material,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _material(index: int) -> LoreMaterial:
    return LoreMaterial(
        material_id=f"item-{index}",
        section="Thích",
        anchor=f"Lore đã xác thực về Mai: fact {index}",
    )


def test_parser_accepts_only_allowlisted_section_bullets_and_caps_anchor() -> None:
    text = """# Lore

## Thích
- Sưu tầm thú bông và đặt tên cho từng con.

Đoạn prose này không phải material.

## Không cho phép
- Đây không được đi vào self-talk.
"""
    materials = parse_lore_material(
        text,
        section_allowlist=("Thích",),
        max_anchor_chars=64,
    )

    assert len(materials) == 1
    assert materials[0].section == "Thích"
    assert "thú bông" in materials[0].anchor
    assert len(materials[0].anchor) <= 64
    assert "Không cho phép" not in materials[0].anchor
    assert "Đoạn prose" not in materials[0].anchor


def test_release_retries_same_candidate_and_commit_advances() -> None:
    provider = LoreMaterialProvider(
        (_material(1), _material(2), _material(3)), no_repeat_last_n=2,
    )

    first = provider.reserve()
    assert first == _material(1)
    assert provider.release(first.material_id)
    assert provider.reserve() == first
    assert provider.commit(first.material_id)
    assert provider.reserve() == _material(2)

    metrics = provider.get_metrics()
    assert metrics["self_talk_lore_reservations_total"] == 3
    assert metrics["self_talk_lore_releases_total"] == 1
    assert metrics["self_talk_lore_commits_total"] == 1


def test_rotation_is_bounded_and_avoids_recent_material() -> None:
    provider = LoreMaterialProvider(
        (_material(1), _material(2), _material(3)), no_repeat_last_n=20,
    )
    delivered: list[str] = []
    for _ in range(4):
        material = provider.reserve()
        assert material is not None
        delivered.append(material.material_id)
        assert provider.commit(material.material_id)

    assert delivered == ["item-1", "item-2", "item-3", "item-1"]


def test_missing_lore_file_is_safe_and_observable(tmp_path: Path) -> None:
    class Loader:
        def get(self, name: str, key: str, default: Any = None) -> Any:
            if (name, key) == ("models", "llm_main.lore_prompt_path"):
                return str(tmp_path / "missing.txt")
            if (name, key) == ("self_talk", "self_talk.lore_material"):
                return {
                    "section_allowlist": ["Thích"],
                    "max_anchor_chars": 80,
                    "no_repeat_last_n": 2,
                }
            return default

    provider = LoreMaterialProvider.from_loader(Loader())

    assert provider.material_count == 0
    assert provider.reserve() is None
    assert provider.get_metrics()["self_talk_lore_unavailable_total"] == 1


def test_repository_lore_config_produces_vetted_material() -> None:
    loader = ConfigLoader(REPO_ROOT / "config")
    loader.load_all()
    provider = LoreMaterialProvider.from_loader(loader)

    assert provider.material_count >= 6
    material = provider.reserve()
    assert material is not None
    assert material.section in loader.get(
        "self_talk", "self_talk.lore_material.section_allowlist", [],
    )
    assert material.anchor.startswith("Lore đã xác thực về Mai")


def test_disabling_provider_releases_pending_reservation() -> None:
    provider = LoreMaterialProvider((_material(1),))
    material = provider.reserve()
    assert material is not None

    provider.set_enabled(False)

    assert provider.has_reservation(material.material_id) is False
    assert provider.reserve() is None
    assert provider.get_metrics()["self_talk_lore_releases_total"] == 1
