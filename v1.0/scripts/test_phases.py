"""Bộ test gom theo phase cho Mai — chạy + báo cáo pass/fail từng phase.

Chạy:
    .\\venv\\Scripts\\python.exe scripts\\test_phases.py           # P0+P1+P2 (bỏ live)
    .\\venv\\Scripts\\python.exe scripts\\test_phases.py --live    # + test LLM thật (cần server)
    .\\venv\\Scripts\\python.exe scripts\\test_phases.py --phase 2 # chỉ 1 phase

Live cần llama-server chạy sẵn (--reasoning off, port 8080).
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# phase → (mô tả, list file test offline)
PHASES: dict[str, tuple[str, list[str]]] = {
    "0": ("Foundation (config/interfaces/bus/state/trigger/migration/metrics/dashboard/health)", [
        "tests/unit/test_config_loader.py",
        "tests/unit/test_logger.py",
        "tests/unit/test_interfaces.py",
        "tests/unit/test_features.py",
        "tests/unit/test_event_bus.py",
        "tests/unit/test_state_machine.py",
        "tests/unit/test_trigger_manager.py",
        "tests/unit/test_fallback_manager.py",
        "tests/unit/test_migration_runner.py",
        "tests/unit/test_metrics_collector.py",
        "tests/unit/test_emergency_stop.py",
        "tests/unit/test_health_monitor.py",
        "tests/integration/test_dashboard.py",
        "tests/integration/test_memory_leak.py",
    ]),
    "1": ("Core LLM (process/llm/prompt/parser/canned/turn/metrics)", [
        "tests/unit/test_process_manager.py",
        "tests/unit/test_llama_cpp_llm.py",
        "tests/unit/test_prompt_manager.py",
        "tests/unit/test_parser.py",
        "tests/unit/test_canned_response.py",
        "tests/unit/test_llm_turn.py",
        "tests/unit/test_metrics_llm.py",
        "tests/integration/test_phase1_turns.py",
    ]),
    "2": ("Trigger + State Machine (interrupt/watchdog/ambient/dashboard/integration)", [
        "tests/unit/test_interrupt_policy.py",
        "tests/unit/test_state_watchdog.py",
        "tests/unit/test_ambient_prompt.py",
        "tests/unit/test_dashboard_phase2.py",
        "tests/integration/test_trigger_state_interaction.py",
    ]),
    "3": ("Filter (rule + regenerate + dashboard + DoD catch/FP)", [
        "tests/unit/test_rule_filter.py",
        "tests/unit/test_regenerator.py",
        "tests/unit/test_metrics_filter.py",
        "tests/integration/test_filter_dod.py",
    ]),
    "4": ("TTS (VieNeu/splitter/subtitle/player/pipeline)", [
        "tests/unit/test_vieneu_service.py",
        "tests/unit/test_sentence_splitter.py",
        "tests/unit/test_subtitle_fallback.py",
        "tests/unit/test_audio_player.py",
        "tests/unit/test_metrics_tts.py",
        "tests/integration/test_tts_pipeline.py",
    ]),
}

# test cần server thật (marker llm)
LIVE_FILES = [
    "tests/integration/test_llama_server_live.py",
    "tests/integration/test_llm_live.py",
]

BAR = "=" * 72


def run_group(title: str, files: list[str], marker: str) -> bool:
    print(f"\n{BAR}\n  {title}\n{BAR}")
    cmd = [sys.executable, "-m", "pytest", *files, "-q", "-p", "no:cacheprovider", "-m", marker]
    rc = subprocess.run(cmd, cwd=REPO_ROOT).returncode
    print(f"  → {'✅ PASS' if rc == 0 else '❌ FAIL'}")
    return rc == 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="chạy thêm test LLM thật (cần server)")
    ap.add_argument("--phase", choices=list(PHASES), help="chỉ chạy 1 phase")
    args = ap.parse_args()

    results: dict[str, bool] = {}
    phases = {args.phase: PHASES[args.phase]} if args.phase else PHASES

    for pid, (desc, files) in phases.items():
        results[f"Phase {pid} — {desc}"] = run_group(f"PHASE {pid}: {desc}", files, "not llm and not slow")

    if args.live:
        results["Live LLM (cần server)"] = run_group("LIVE: LLM thật qua llama-server", LIVE_FILES, "llm")

    print(f"\n{BAR}\n  TỔNG KẾT\n{BAR}")
    for name, ok in results.items():
        print(f"  {'✅' if ok else '❌'}  {name[:66]}")
    passed = sum(results.values())
    total = len(results)
    print(f"{BAR}\n  {passed}/{total} nhóm PASS")
    if passed < total:
        print("  ⚠️ Nếu chỉ Phase 0 fail ở test_migration_runner → flaky theo mốc giây, chạy lại.")
    print(BAR)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
