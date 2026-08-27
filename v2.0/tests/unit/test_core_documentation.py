"""Guards for the minimal canonical Mai documentation set."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from orchestrator.config_loader import CONFIG_FILES


ROOT = Path(__file__).resolve().parents[2]
PRODUCT_VERSION = "1.4.3"
CANONICAL_DOCS = ("V1_BASELINE.md", "MAI_V2_SYSTEM_SPEC.md")
ROOT_MARKDOWN_ALLOWLIST = {
    "AGENTS.md",
    "CHANGELOG.md",
    "MAI_V2_MASTER_IMPLEMENTATION_BLUEPRINT_v2.0.md",
    "README.md",
}
# Bộ markdown canonical trong docs/ sau rewrite e510a2a (thêm ROADMAP.md tách
# scope/thứ tự tương lai khỏi spec "current behavior").
DOCS_MARKDOWN_ALLOWLIST = {"README.md", "ROADMAP.md", *CANONICAL_DOCS}
# Spec mới tóm tắt "31 YAML" thay vì liệt kê từng file; guard chỉ còn yêu cầu bộ
# config lõi được nêu tên trong spec.
CORE_DOCUMENTED_CONFIGS = (
    "system.yaml",
    "features.yaml",
    "state.yaml",
    "kernel.yaml",
    "cognition.yaml",
)
IMPLEMENTATION_ROOTS = (
    "config",
    "dashboard",
    "interfaces",
    "orchestrator",
    "scripts",
    "services",
)
COMMENT_SUFFIXES = {".ps1", ".py", ".yaml", ".yml"}
STALE_WORK_COMMENT = re.compile(
    r"(?i:\bTASK\s*\d+\b|CHƯA làm\s*\(Phase|"
    r"Phase\s+\d+(?:\.\d+)?\s+(?:sẽ|mới|sau)\b|"
    r"để Phase\s+\d+|khi Phase\s+[\d.]+.*xong|"
    r"Wire STT sau|Alpine\.js để Phase|auto-recovery.*để Phase|"
    r"AUTONOMY_ENGINE_REDESIGN\.md)|\bARCHITECTURE\b|"
    r"\bspec Mục\b|\bmilestone\s*\d+"
)
FROZEN_V1_ENABLED = {
    "filter_rule",
    "tts_streaming",
    "animation_smooth",
    "data_collector",
    "director_goal_arbiter",
    "director_chat_gate",
    "conversation_continuity",
    "mood_behavior_policy",
    "mood_v2_shadow",
    "mood_v2_prompt",
    "action_transactions",
    "decision_records",
    "operator_dashboard_v2",
    "proactive_hosting",
    "self_talk_planner",
    "behavior_library",
    "natural_timing",
    "self_talk_lore",
    "relationship_memory",
    "evaluation_harness",
    "evaluation_acceptance",
    "live_operations",
    "kv_cache_q8",
    "ambient_talk",
}
FROZEN_V1_DISABLED = {
    "input_voice",
    "input_emotion_voice",
    "filter_ai",
    "tts_emotion_aware",
    "animation_micro",
    "memory_semantic",
    "memory_hierarchical",
    "qc_persona",
    "agent_context",
    "goal_proposals",
    "thread_extraction",
    "speculative_decoding",
    "turn_taking_predictor",
}
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


def test_product_version_is_pinned_across_canonical_documents() -> None:
    system = _yaml("config/system.yaml")
    assert system["app"]["version"] == PRODUCT_VERSION
    files = [
        ROOT / "README.md",
        ROOT / "AGENTS.md",
        ROOT / "MAI_V2_MASTER_IMPLEMENTATION_BLUEPRINT_v2.0.md",
        *(ROOT / "docs" / relative for relative in CANONICAL_DOCS),
    ]
    for document in files:
        assert PRODUCT_VERSION in document.read_text(encoding="utf-8"), (
            f"missing product version in {document.relative_to(ROOT)}"
        )
    assert f"[{PRODUCT_VERSION}]" in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


def test_frozen_v1_feature_inventory_is_immutable() -> None:
    baseline = (ROOT / "docs" / "V1_BASELINE.md").read_text(encoding="utf-8")
    enabled_text = baseline.split("Enabled toggle ở baseline:", 1)[1]
    enabled_text, disabled_text = enabled_text.split(
        "Disabled/optional toggle ở baseline:",
        1,
    )
    disabled_text = disabled_text.split("Danh sách trên là ảnh chụp lịch sử", 1)[0]
    assert _backtick_names(enabled_text) == FROZEN_V1_ENABLED
    assert _backtick_names(disabled_text) == FROZEN_V1_DISABLED


# Spec rewrite (e510a2a) bỏ section liệt kê feature-flag "Đang bật/Đang tắt";
# trạng thái giờ là bảng theo module (LIVE/SHADOW) chứ không map 1-1 flag name.
# Guard inventory-per-flag không còn target → gỡ (features.yaml vẫn được
# FeatureManager validate ở runtime + test riêng).


def test_core_config_files_documented_in_spec() -> None:
    registry = set(CONFIG_FILES.values())
    spec = (ROOT / "docs" / "MAI_V2_SYSTEM_SPEC.md").read_text(encoding="utf-8")
    for filename in CORE_DOCUMENTED_CONFIGS:
        assert filename in registry, f"core config not in loader registry: {filename}"
        assert f"`{filename}`" in spec, f"undocumented core config: {filename}"


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


def test_relative_markdown_links_in_canonical_docs_resolve() -> None:
    files = [
        ROOT / "README.md",
        ROOT / "docs" / "README.md",
        ROOT / "MAI_V2_MASTER_IMPLEMENTATION_BLUEPRINT_v2.0.md",
        *(ROOT / "docs" / relative for relative in CANONICAL_DOCS),
    ]
    pattern = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
    for document in files:
        text = document.read_text(encoding="utf-8")
        for target in pattern.findall(text):
            if "://" in target or target.startswith("#"):
                continue
            resolved = (document.parent / target.split("#", 1)[0]).resolve()
            assert resolved.exists(), f"broken link in {document.name}: {target}"


def test_documented_critical_entrypoints_exist() -> None:
    for relative in CRITICAL_PATHS:
        assert (ROOT / relative).exists(), f"documented critical path missing: {relative}"


def test_document_index_links_only_canonical_documents() -> None:
    index = (ROOT / "docs" / "README.md").read_text(encoding="utf-8")
    for relative in CANONICAL_DOCS:
        assert f"({relative})" in index, f"canonical document missing: {relative}"
    numbered = sorted((ROOT / "docs").glob("[0-9][0-9]_*.md"))
    assert numbered == [], f"duplicate numbered docs remain: {numbered}"


def test_no_auxiliary_markdown_or_root_lore_draft_remains() -> None:
    root_markdown = {path.name for path in ROOT.glob("*.md")}
    assert root_markdown == ROOT_MARKDOWN_ALLOWLIST
    docs_markdown = {path.name for path in (ROOT / "docs").glob("*.md")}
    assert docs_markdown == DOCS_MARKDOWN_ALLOWLIST
    assert not (ROOT / "MAI_LORE_DRAFT.txt").exists()


def test_implementation_comments_do_not_restore_stale_work_promises() -> None:
    stale: list[str] = []
    for root_name in IMPLEMENTATION_ROOTS:
        for path in (ROOT / root_name).rglob("*"):
            if not path.is_file() or path.suffix.lower() not in COMMENT_SUFFIXES:
                continue
            for line_number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(),
                start=1,
            ):
                if STALE_WORK_COMMENT.search(line):
                    stale.append(f"{path.relative_to(ROOT)}:{line_number}: {line.strip()}")
    assert stale == [], "stale implementation work comments returned:\n" + "\n".join(stale)


# Guard cũ pin trạng thái theo phase/commit-hash/số test (Phase 1–15, 2.304 đạt,
# d02c84e, "S8 chưa commit"...). Rewrite e510a2a chuyển spec sang "current
# behavior" và tách tiến trình sang ROADMAP.md, cố ý không track các mốc đó nữa
# → gỡ các assert snapshot. Các invariant cấu trúc repo vẫn giữ ở test dưới.


def test_canonical_boundary_structure_is_current() -> None:
    index = (ROOT / "docs" / "README.md").read_text(encoding="utf-8")
    blueprint = (
        ROOT / "MAI_V2_MASTER_IMPLEMENTATION_BLUEPRINT_v2.0.md"
    ).read_text(encoding="utf-8")
    spec = (ROOT / "docs" / "MAI_V2_SYSTEM_SPEC.md").read_text(encoding="utf-8")
    combined = "\n".join(
        (
            (ROOT / "README.md").read_text(encoding="utf-8"),
            index,
            blueprint,
            spec,
        )
    )

    assert "config/state.yaml" in combined
    assert not (ROOT / "config" / "agent_state.yaml").exists()
    assert not (ROOT / "config" / "relationships.yaml").exists()
    assert "AuthoritativeStateReducer" in spec
    assert "│   ├── models.yaml" in blueprint
    assert "│   ├── model.yaml" not in blueprint
    assert "s2_implemented_source_dirty" not in combined
    assert "S2 sau đó được triển khai trong working tree nhưng chưa commit" not in combined


def test_dead_config_keys_and_repo_hygiene_are_current() -> None:
    # Head-of-doc status strings (commit hash 723ca33, mốc "S8 chưa commit"...)
    # do rewrite e510a2a cố ý bỏ; guard giữ lại phần config/repo invariant.
    assert (ROOT / "docs" / "V1_BASELINE.md").is_file()

    system = _yaml("config/system.yaml")
    for dead in ("ambient_talk", "health", "event_bus"):
        assert dead not in system
    assert system["emergency_stop"]["enabled"] is True
    assert "trigger_manager" not in system["features"]["core"]
    assert "state_machine" not in system["features"]["core"]

    features = _yaml("config/features.yaml")["features"]
    for dead in ("filter_ai", "memory_hierarchical", "qc_persona", "ambient_talk"):
        assert dead not in features

    ignore = (ROOT.parent / ".gitignore").read_text(encoding="utf-8")
    for proposal in ("MAI_DO_LUONG.md", "MAI_KIEN_TRUC_MOI.md", "MAI_UPGRADE_PLAN.md"):
        assert proposal in ignore
