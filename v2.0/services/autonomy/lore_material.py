"""Bounded, delivery-aware character-lore material for self-talk."""
from __future__ import annotations

import hashlib
import re
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$")
_BULLET_RE = re.compile(r"^-\s+(.+?)\s*$")


@dataclass(frozen=True)
class LoreMaterial:
    """One vetted, bounded fact anchor from the character lore."""

    material_id: str
    section: str
    anchor: str

    @property
    def evidence_ref(self) -> str:
        return f"lore:{self.material_id}"


def parse_lore_material(
    text: str,
    *,
    section_allowlist: tuple[str, ...],
    max_anchor_chars: int,
) -> tuple[LoreMaterial, ...]:
    """Extract only bullet facts under explicitly allowed level-two sections."""
    allowed = {item.strip() for item in section_allowlist if item.strip()}
    if not allowed:
        return ()
    cap = max(1, int(max_anchor_chars))
    section: str | None = None
    items: list[LoreMaterial] = []
    seen: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        heading = _SECTION_RE.match(line)
        if heading is not None:
            section = heading.group(1).strip()
            continue
        bullet = _BULLET_RE.match(line)
        if bullet is None or section not in allowed:
            continue
        fact = " ".join(bullet.group(1).split())
        if not fact:
            continue
        source = f"{section}\0{fact}"
        material_id = hashlib.sha256(source.encode("utf-8")).hexdigest()[:12]
        if material_id in seen:
            continue
        seen.add(material_id)
        prefix = f"Lore đã xác thực về Mai ({section}): "
        anchor = (prefix + fact)[:cap].rstrip()
        items.append(LoreMaterial(material_id, section, anchor))
    return tuple(items)


class LoreMaterialProvider:
    """Reserve lore deterministically and rotate only after delivered commit."""

    def __init__(
        self,
        materials: tuple[LoreMaterial, ...],
        *,
        enabled: bool = True,
        no_repeat_last_n: int = 6,
    ) -> None:
        self._materials = tuple(materials)
        self._enabled = bool(enabled)
        self._cursor = 0
        window = min(max(0, int(no_repeat_last_n)), max(0, len(materials) - 1))
        self._recent: deque[str] = deque(maxlen=window)
        self._pending: tuple[int, LoreMaterial] | None = None
        self._metrics = {
            "reservations_total": 0,
            "commits_total": 0,
            "releases_total": 0,
            "unavailable_total": 0,
        }

    @classmethod
    def from_loader(
        cls,
        loader: Any,
        *,
        enabled: bool = True,
    ) -> "LoreMaterialProvider":
        raw = loader.get("self_talk", "self_talk.lore_material", {}) or {}
        path_value = loader.get("models", "llm_main.lore_prompt_path", None)
        text = ""
        if path_value:
            path = Path(path_value)
            resolved = path if path.is_absolute() else _REPO_ROOT / path
            if resolved.is_file():
                text = resolved.read_text(encoding="utf-8")
        materials = parse_lore_material(
            text,
            section_allowlist=tuple(raw.get("section_allowlist", []) or ()),
            max_anchor_chars=int(raw.get("max_anchor_chars", 280)),
        )
        return cls(
            materials,
            enabled=enabled,
            no_repeat_last_n=int(raw.get("no_repeat_last_n", 6)),
        )

    def reserve(self) -> LoreMaterial | None:
        if not self._enabled or not self._materials:
            self._metrics["unavailable_total"] += 1
            return None
        if self._pending is not None:
            return self._pending[1]
        recent = set(self._recent)
        for offset in range(len(self._materials)):
            index = (self._cursor + offset) % len(self._materials)
            material = self._materials[index]
            if material.material_id in recent:
                continue
            self._pending = (index, material)
            self._metrics["reservations_total"] += 1
            return material
        self._metrics["unavailable_total"] += 1
        return None

    def commit(self, material_id: str) -> bool:
        if self._pending is None or self._pending[1].material_id != material_id:
            return False
        index, material = self._pending
        self._pending = None
        self._recent.append(material.material_id)
        self._cursor = (index + 1) % len(self._materials)
        self._metrics["commits_total"] += 1
        return True

    def release(self, material_id: str) -> bool:
        if self._pending is None or self._pending[1].material_id != material_id:
            return False
        self._pending = None
        self._metrics["releases_total"] += 1
        return True

    def has_reservation(self, material_id: str) -> bool:
        return self._pending is not None and self._pending[1].material_id == material_id

    def has_available_material(self) -> bool:
        """Read-only reserve precondition used by scheduling readiness."""
        if not self._enabled or not self._materials:
            return False
        if self._pending is not None:
            return True
        recent = set(self._recent)
        return any(item.material_id not in recent for item in self._materials)

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        if not self._enabled and self._pending is not None:
            self.release(self._pending[1].material_id)

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def material_count(self) -> int:
        return len(self._materials)

    def get_metrics(self) -> dict[str, Any]:
        return {
            "self_talk_lore_enabled": self._enabled,
            "self_talk_lore_materials_available": len(self._materials),
            **{
                f"self_talk_lore_{key}": value
                for key, value in self._metrics.items()
            },
        }
