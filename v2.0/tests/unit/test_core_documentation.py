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


def test_current_feature_inventory_matches_feature_yaml() -> None:
    features = _yaml("config/features.yaml")["features"]
    enabled = {name for name, config in features.items() if config["enabled"] is True}
    disabled = set(features) - enabled
    spec = (ROOT / "docs" / "MAI_V2_SYSTEM_SPEC.md").read_text(encoding="utf-8")
    enabled_text = spec.split("- **Đang bật:**", 1)[1]
    enabled_text, disabled_text = enabled_text.split("- **Đang tắt/tùy chọn:**", 1)
    disabled_text = disabled_text.split("Trong đó `speech_action_adapter`", 1)[0]
    assert _backtick_names(enabled_text) == enabled
    assert _backtick_names(disabled_text) == disabled


def test_documented_config_inventory_matches_loader_registry() -> None:
    spec = (ROOT / "docs" / "MAI_V2_SYSTEM_SPEC.md").read_text(encoding="utf-8")
    for filename in CONFIG_FILES.values():
        assert f"`{filename}`" in spec, f"undocumented config: {filename}"


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
    assert docs_markdown == {"README.md", *CANONICAL_DOCS}
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


def test_canonical_docs_keep_current_phase_and_release_limits_explicit() -> None:
    spec = (ROOT / "docs" / "MAI_V2_SYSTEM_SPEC.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    required = (
        "production flag vẫn tắt, chưa có live rollout/canary",
        "Feature `obs_scene_executor` vẫn mặc định `enabled=false`",
        "speech_action_adapter",
        "avatar_action_adapter",
        "commit rồi mới project Mô hình Thế giới",
        "Vòng tự chủ khép kín | Chưa đạt",
        "`v2.0\\venv` hiện dùng CPython `3.11.15`",
        "Phase 10 đã đóng canonical perception ingress",
        "Phase 13 trở đi chưa đóng gate",
        "full offline `pytest tests -q`: 2.207 đạt, 0 lỗi",
        "Dashboard toggle thành công phải persist",
        "RuntimeCriticalConfig` không nhận chuỗi thay cho",
        "Khởi động là một giao dịch hai tầng",
        "Repository không tự nạp `.env`",
        "MAI_DASHBOARD_CONTROL_TOKEN",
        "X-Mai-Operator-Token",
    )
    for statement in required:
        assert statement in spec, f"critical implementation limit disappeared: {statement}"

    assert "Phase 1–12 đã đóng gate kỹ thuật" in readme
    forbidden = (
        "Phase 1–11 đã đóng gate kỹ thuật",
        "Phase 1–10 đã đóng gate kỹ thuật",
        "Phase 1–9 đã đóng gate kỹ thuật",
        "Perception expansion và các release gate sau vẫn chưa hoàn tất",
        "WIP chưa commit",
        "Working tree còn thay đổi Phase 14",
        "full offline `pytest -m \"not slow and not llm\"`: 1.999 đạt",
        "chưa thực sự thay thế quyết định cũ",
        "chưa được lắp hoàn chỉnh vào điểm ghép chính",
        "trạng thái Thế giới có thể được cập nhật trước",
        "Mức 2 — sửa tính đúng của giao dịch",
    )
    combined = readme + "\n" + spec
    for statement in forbidden:
        assert statement not in combined, f"stale implementation claim returned: {statement}"
