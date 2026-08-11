"""Generate deterministic Mood v1/v2 pairs and build a sanitized blind review sheet."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from interfaces.animation import MoodState  # noqa: E402
from interfaces.llm import LLMRequest, LLMService  # noqa: E402
from orchestrator.config_loader import ConfigLoader  # noqa: E402
from services.data.sanitize import mask_pii  # noqa: E402
from services.emotion.affect_style import AffectStyleRenderer  # noqa: E402
from services.emotion.affect_v2 import AffectV2  # noqa: E402
from services.emotion.hybrid_affect import HybridAffectComposer  # noqa: E402
from services.emotion.mood_style import MoodStyleTable  # noqa: E402
from services.evaluation.mood_ab import MoodABReview  # noqa: E402
from services.llm.llama_cpp_llm import LlamaCppLLMService  # noqa: E402
from services.llm.prompt_manager import PromptManager  # noqa: E402


@dataclass(frozen=True)
class MoodABCase:
    case_id: str
    category: str
    context: str
    input_text: str


def load_cases(loader: ConfigLoader) -> tuple[MoodABCase, ...]:
    policy = loader.get("mood_ab_cases", "policy", {}) or {}
    target_count = int(policy.get("target_count", 25))
    categories = loader.get("mood_ab_cases", "categories", {}) or {}
    if target_count < 1 or not categories:
        raise ValueError("mood A/B corpus requires a positive target and at least one category")
    base_per_category, remainder = divmod(target_count, len(categories))
    appraisal = loader.get("emotion_appraisal", "appraisal", {}) or {}
    affect_mappings = loader.get("affect_v2", "mappings", {}) or {}
    cases: list[MoodABCase] = []
    for category_index, (category, raw) in enumerate(categories.items()):
        if category not in appraisal or category not in affect_mappings:
            raise ValueError(f"mood A/B category is not mapped by both versions: {category}")
        spec = dict(raw or {})
        context = _clean(spec.get("context"), 800)
        inputs = list(spec.get("inputs") or [])
        per_category = base_per_category + int(category_index < remainder)
        if len(inputs) < per_category:
            raise ValueError(
                f"mood A/B category {category} requires at least {per_category} inputs, got {len(inputs)}"
            )
        for offset, input_text in enumerate(inputs[:per_category], start=1):
            cases.append(MoodABCase(
                case_id=f"{category}:{offset:02d}",
                category=str(category),
                context=context,
                input_text=_clean(input_text, 500),
            ))
    if len(cases) != target_count:
        raise ValueError(f"mood A/B corpus requires {target_count} cases, got {len(cases)}")
    return tuple(cases)


def build_pair_requests(
    loader: ConfigLoader,
    prompt_manager: PromptManager,
    affect: AffectV2,
    renderer: AffectStyleRenderer,
    case: MoodABCase,
    *,
    index: int,
    composer: HybridAffectComposer | None = None,
) -> tuple[LLMRequest, LLMRequest]:
    policy = loader.get("affect_v2", "policy", {}) or {}
    seed = int(policy.get("ab_seed", 20260809)) + index
    max_tokens = int(policy.get("ab_generation_max_tokens", 128))
    temperature = float(policy.get("ab_generation_temperature", 0.75))
    targets = dict(loader.get("emotion_appraisal", f"appraisal.{case.category}", {}) or {})
    tone_flag = loader.get("emotion_appraisal", f"tone_flags.{case.category}", None)
    tone_flags = {str(tone_flag)} if tone_flag else set()
    mood = MoodState(**{key: int(value) for key, value in targets.items()})

    v1 = prompt_manager.build_request_with_mood(
        request_id=f"m10-ab-{index:03d}-v1",
        user_text=case.input_text,
        current_mood=mood,
        event_category=case.category,
        tone_flags=tone_flags,
        max_tokens=max_tokens,
        temperature=temperature,
        grounded_context=case.context,
    ).model_copy(update={"seed": seed})

    affect.reset_session()
    turn_affect = affect.observe(
        case.category,
        targets=targets,
        tone_flag=str(tone_flag) if tone_flag else None,
        cause_ref=case.case_id,
    )
    hybrid = composer or HybridAffectComposer.from_loader(
        loader, mood_style=MoodStyleTable.from_loader(loader),
    )
    plan = hybrid.compose(case.category, turn_affect, mood, tone_flags)
    directive = renderer.directive_for_plan(plan, affect.current_session_mood())
    if not directive:
        raise ValueError(f"hybrid mood produced no directive for {case.case_id}")
    v2 = prompt_manager.build_request_with_mood(
        request_id=f"m10-ab-{index:03d}-v2",
        user_text=case.input_text,
        current_mood=mood,
        event_category=case.category,
        tone_flags=tone_flags,
        affect_directive=directive,
        max_tokens=max_tokens,
        temperature=temperature,
        grounded_context=case.context,
    ).model_copy(update={"seed": seed})
    return v1, v2


async def generate_text(service: LLMService, request: LLMRequest) -> str:
    parts: list[str] = []
    async for token in service.generate_stream(request):
        if token.token:
            parts.append(token.token)
    return "".join(parts).strip()


async def run_replay(
    loader: ConfigLoader,
    service: LLMService,
    *,
    cases: tuple[MoodABCase, ...] | None = None,
    existing: tuple[dict[str, Any], ...] = (),
    progress: Any = None,
) -> tuple[dict[str, Any], ...]:
    corpus = cases or load_cases(loader)
    prompt_manager = PromptManager.from_loader(loader)
    affect = AffectV2.from_loader(loader)
    renderer = AffectStyleRenderer.from_loader(loader)
    composer = HybridAffectComposer.from_loader(
        loader, mood_style=MoodStyleTable.from_loader(loader),
    )
    completed = {str(row.get("turn_ref")): dict(row) for row in existing}
    output: list[dict[str, Any]] = []
    output_max = int(loader.get("affect_v2", "policy.ab_output_max_chars", 400))
    base_seed = int(loader.get("affect_v2", "policy.ab_seed", 20260809))
    for index, case in enumerate(corpus, start=1):
        if case.case_id in completed:
            output.append(completed[case.case_id])
            continue
        v1_request, v2_request = build_pair_requests(
            loader, prompt_manager, affect, renderer, case, index=index,
            composer=composer,
        )
        swap_order = hashlib.sha256(f"{base_seed}:{case.case_id}".encode()).digest()[0] & 1
        requests = (("v2", v2_request), ("v1", v1_request)) if swap_order else (
            ("v1", v1_request), ("v2", v2_request),
        )
        generated: dict[str, str] = {}
        for version, request in requests:
            generated[version] = _clean(await generate_text(service, request), output_max)
            if not generated[version]:
                raise RuntimeError(f"empty {version} output for {case.case_id}")
        row = {
            "turn_ref": case.case_id,
            "event_category": case.category,
            "input": case.input_text,
            "context": case.context,
            "generation_seed": v1_request.seed,
            "same_input_context": True,
            "v1_output": generated["v1"],
            "v2_output": generated["v2"],
        }
        output.append(row)
        if progress is not None:
            progress(index, len(corpus), tuple(output))
    return tuple(output)


def _clean(value: Any, max_chars: int) -> str:
    return " ".join(str(mask_pii(str(value or "")) or "").split())[:max_chars]


def _read_comparisons(path: Path) -> tuple[dict[str, Any], ...]:
    if not path.is_file():
        return ()
    raw = json.loads(path.read_text(encoding="utf-8"))
    rows = raw.get("comparisons", []) if isinstance(raw, dict) else []
    return tuple(dict(row) for row in rows if isinstance(row, dict))


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


async def _run(args: argparse.Namespace) -> int:
    loader = ConfigLoader(Path("config"))
    loader.load_all()
    all_cases = load_cases(loader)
    cases = all_cases[: args.limit] if args.limit else all_cases
    existing = _read_comparisons(args.checkpoint) if args.resume else ()
    service = LlamaCppLLMService.from_loader(loader)
    await service.start()
    health = await service.health_check()
    if not health.is_ok:
        await service.stop()
        raise RuntimeError("llama-server is not healthy on the configured endpoint")

    def checkpoint(done: int, total: int, rows: tuple[dict[str, Any], ...]) -> None:
        _write_json(args.checkpoint, {"schema_version": 1, "comparisons": rows})
        print(json.dumps({"generated": done, "total": total}, ensure_ascii=False), flush=True)

    try:
        comparisons = await run_replay(
            loader, service, cases=cases, existing=existing, progress=checkpoint,
        )
    finally:
        await service.stop()
    _write_json(args.checkpoint, {"schema_version": 1, "comparisons": comparisons})
    artifact = MoodABReview.from_loader(loader).build(comparisons)
    _write_json(args.output, artifact)
    print(json.dumps({
        "output": str(args.output),
        "checkpoint": str(args.checkpoint),
        "turn_count": len(comparisons),
        "status": artifact["status"],
    }, ensure_ascii=False), flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--checkpoint", type=Path, default=Path("logs/m10_mood_ab_comparisons.json"),
    )
    parser.add_argument("--limit", type=int)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be positive")
    try:
        return asyncio.run(_run(args))
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
