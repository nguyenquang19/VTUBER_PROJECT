"""Stress a complete YouTube replay through Director with the real llama.cpp backend."""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from orchestrator.config_loader import ConfigLoader  # noqa: E402
from orchestrator.fallback_manager import FallbackManager  # noqa: E402
from scripts.simulate_youtube_replay import simulate_replay  # noqa: E402
from services.agent.conversation_context import ConversationContextComposer  # noqa: E402
from services.llm.canned_response import CannedResponder  # noqa: E402
from services.llm.llama_cpp_llm import LlamaCppLLMService  # noqa: E402
from services.llm.llm_turn import LLMTurnRunner  # noqa: E402
from services.llm.prompt_manager import PromptManager  # noqa: E402
from services.filter.regenerator import FilterRegenerator  # noqa: E402
from services.filter.rule_filter import RuleFilter  # noqa: E402


class _ReadOnlyAgentState:
    """Expose live snapshots to prompt rendering without duplicating speech events."""

    def __init__(self, state: Any) -> None:
        self._state = state

    def snapshot(self) -> Any:
        return self._state.snapshot()

    def record(self, _event: Any) -> bool:
        return False


class InstrumentedLLMRunner:
    """Record every real generation while preserving LLMTurnRunner semantics."""

    delivery_mode = "real_llama_cpp_subtitle_simulation"

    def __init__(
        self,
        delegate: LLMTurnRunner,
        service: LlamaCppLLMService,
        *,
        input_max_chars: int,
        checkpoint: Path | None = None,
        checkpoint_every: int = 10,
    ) -> None:
        self._delegate = delegate
        self._service = service
        self._input_max_chars = max(200, int(input_max_chars))
        self._checkpoint = checkpoint
        self._checkpoint_every = max(1, int(checkpoint_every))
        self.calls: list[dict[str, Any]] = []
        self.committed_self_talk: list[str] = []

    async def run_turn(self, request_id: str, user_text: str, **kwargs: Any) -> Any:
        started = time.perf_counter()
        parsed, level = await self._delegate.run_turn(
            request_id=request_id, user_text=user_text, **kwargs,
        )
        self._append(
            request_id=request_id,
            kind="chat",
            input_text=user_text,
            parsed=parsed,
            level_used=level,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
            viewer_id=kwargs.get("viewer_id"),
        )
        return parsed, level

    async def run_ambient_turn(
        self, request_id: str, prompt_text: str, **kwargs: Any,
    ) -> Any:
        started = time.perf_counter()
        parsed = await self._delegate.run_ambient_turn(
            request_id, prompt_text, **kwargs,
        )
        self._append(
            request_id=request_id,
            kind="ambient",
            input_text=prompt_text,
            parsed=parsed,
            level_used=0 if parsed.ok else 1,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
        )
        return parsed

    async def run_directed_turn(
        self, request_id: str, system_context: str, **kwargs: Any,
    ) -> Any:
        started = time.perf_counter()
        parsed = await self._delegate.run_directed_turn(
            request_id, system_context, **kwargs,
        )
        self._append(
            request_id=request_id,
            kind="directed",
            input_text=system_context,
            parsed=parsed,
            level_used=0 if parsed.ok else 1,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
        )
        return parsed

    def finalize_delivery(self, request_id: str, success: bool) -> bool:
        return self._delegate.finalize_delivery(request_id, success)

    def commit_self_talk(self, text: str) -> None:
        self.committed_self_talk.append(text)
        self._delegate.commit_self_talk(text)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    def _append(
        self,
        *,
        request_id: str,
        kind: str,
        input_text: str,
        parsed: Any,
        level_used: int,
        elapsed_ms: float,
        viewer_id: str | None = None,
    ) -> None:
        metrics = self._service.get_metrics()
        self.calls.append({
            "request_id": request_id,
            "kind": kind,
            "input": _compact(input_text, self._input_max_chars),
            "response": str(parsed.text or "").strip(),
            "parse_ok": bool(parsed.ok),
            "level_used": int(level_used),
            "viewer_id": viewer_id,
            "latency_ms": round(elapsed_ms, 3),
            "ttft_ms": _round_optional(metrics.get("llm_last_ttft_ms")),
            "decode_tps": _round_optional(metrics.get("llm_last_decode_tps")),
            "tokens_out": int(metrics.get("llm_last_tokens_out") or 0),
        })
        if self._checkpoint is not None and len(self.calls) % self._checkpoint_every == 0:
            _write_json(self._checkpoint, {
                "schema_version": 1,
                "status": "running",
                "generated": len(self.calls),
                "calls": self.calls,
            })


def build_quality_report(
    replay: dict[str, Any],
    calls: Sequence[dict[str, Any]],
    policy: dict[str, Any],
) -> dict[str, Any]:
    patterns = dict(policy.get("forbidden_patterns") or {})
    delivery_summary = dict(replay.get("delivery") or {})
    delivery_items = list(delivery_summary.get("items") or ())
    if not delivery_items:
        delivery_items = [
            delivery
            for trace in replay.get("trace") or ()
            for delivery in trace.get("deliveries") or ()
        ]
    delivered_ids = {
        str(delivery.get("request_id") or "")
        for delivery in delivery_items
        if str(delivery.get("request_id") or "")
    }
    candidate_flagged: dict[str, list[dict[str, str]]] = {
        category: [] for category in patterns
    }
    empty_ids: list[str] = []
    normalized: Counter[str] = Counter()
    fallback_ids: list[str] = []
    latencies: list[float] = []
    ttfts: list[float] = []
    decode_rates: list[float] = []
    for call in calls:
        request_id = str(call.get("request_id") or "")
        response = str(call.get("response") or "").strip()
        if not response:
            empty_ids.append(request_id)
        else:
            normalized[_normalize(response)] += 1
        if int(call.get("level_used") or 0) > 0 or not bool(call.get("parse_ok")):
            fallback_ids.append(request_id)
        _append_number(latencies, call.get("latency_ms"))
        _append_number(ttfts, call.get("ttft_ms"))
        _append_number(decode_rates, call.get("decode_tps"))
        folded = response.casefold()
        for category, values in patterns.items():
            hit = next(
                (str(value) for value in values or () if str(value).casefold() in folded),
                None,
            )
            if hit is not None:
                candidate_flagged[category].append({
                    "request_id": request_id, "pattern": hit,
                })

    identity_policy = dict(policy.get("foreign_identity_guard") or {})
    foreign_names = tuple(
        str(value).casefold() for value in identity_policy.get("names", ())
        if str(value).strip()
    )
    first_person_markers = tuple(
        str(value).casefold()
        for value in identity_policy.get("first_person_markers", ())
        if str(value).strip()
    )
    knowledge_request_markers = tuple(
        str(value).casefold()
        for value in identity_policy.get("knowledge_request_markers", ())
        if str(value).strip()
    )
    uncertainty_markers = tuple(
        str(value).casefold()
        for value in identity_policy.get("uncertainty_markers", ())
        if str(value).strip()
    )
    require_name = bool(identity_policy.get("require_name_in_response", False))
    identity_flags: list[dict[str, str]] = []
    for call in calls:
        input_text = str(call.get("input") or "").casefold()
        response = str(call.get("response") or "").casefold()
        name = next((value for value in foreign_names if value in input_text), None)
        marker = next(
            (value for value in first_person_markers if value in response), None,
        )
        asks_unknown = any(value in input_text for value in knowledge_request_markers)
        states_uncertainty = any(value in response for value in uncertainty_markers)
        missing_name = name is not None and require_name and name not in response
        ungrounded_answer = (
            name is not None
            and str(call.get("kind") or "") != "directed"
            and asks_unknown
            and not states_uncertainty
        )
        if name is not None and (marker is not None or missing_name or ungrounded_answer):
            identity_flags.append({
                "request_id": str(call.get("request_id") or ""),
                "pattern": f"{name}:{marker or 'ungrounded_third_party_answer'}",
            })
    candidate_flagged["foreign_identity_confusion"] = identity_flags
    flagged = {
        category: [
            item for item in values if item["request_id"] in delivered_ids
        ]
        for category, values in candidate_flagged.items()
    }

    output_count = len(calls)
    fallback_ratio = len(fallback_ids) / output_count if output_count else 1.0
    delivered_texts = [
        str(delivery.get("text") or "").strip()
        for delivery in delivery_items
        if str(delivery.get("text") or "").strip()
    ]
    delivered_normalized = Counter(_normalize(text) for text in delivered_texts)
    duplicate_outputs = sum(
        count - 1 for count in delivered_normalized.values() if count > 1
    )
    repetition_ratio = (
        duplicate_outputs / len(delivered_texts) if delivered_texts else 1.0
    )
    # distinct-N: đo đa dạng từ vựng (chống nhạt/lặp cụm — mạnh hơn exact_repetition).
    # distinct thấp = nói đi nói lại vài cụm = "sặc AI". Cao = phong phú.
    diversity = _distinct_ngrams(delivered_texts)
    reasons = dict((replay.get("director") or {}).get("reason_counts") or {})
    cadence = dict((replay.get("director") or {}).get("self_talk_cadence") or {})
    threads = dict(replay.get("conversation_threads") or {})
    delivery = delivery_summary
    gates = dict(policy.get("gates") or {})
    checks = {
        "minimum_generated_responses": output_count >= int(
            gates.get("minimum_generated_responses", 1)
        ),
        "no_empty_output": len(empty_ids) <= int(gates.get("max_empty_outputs", 0)),
        "fallback_ratio": fallback_ratio <= float(gates.get("max_fallback_ratio", 0.0)),
        "exact_repetition_ratio": repetition_ratio <= float(
            gates.get("max_exact_repetition_ratio", 1.0)
        ),
        "meta_leak": len(flagged.get("meta_leak", ())) <= int(
            gates.get("max_meta_leaks", 0)
        ),
        "assistant_register": len(flagged.get("assistant_register", ())) <= int(
            gates.get("max_assistant_register", 0)
        ),
        "hostility": len(flagged.get("hostility", ())) <= int(
            gates.get("max_hostility", 0)
        ),
        "identity_conflict": len(flagged.get("identity_conflict", ())) <= int(
            gates.get("max_identity_conflicts", 0)
        ),
        "foreign_identity_confusion": len(
            flagged.get("foreign_identity_confusion", ())
        ) <= int(
            gates.get("max_foreign_identity_confusions", 0)
        ),
        "ttft_p95": bool(ttfts) and _percentile(ttfts, 95) <= float(
            gates.get("ttft_p95_ms", math.inf)
        ),
        "turn_latency_p95": bool(latencies) and _percentile(latencies, 95) <= float(
            gates.get("turn_latency_p95_ms", math.inf)
        ),
        "decode_tps_p50": bool(decode_rates) and _percentile(
            decode_rates, 50,
        ) >= float(
            gates.get("decode_tps_p50_min", 0.0)
        ),
        "no_stale_thread_wait": int(reasons.get("thread_missing", 0)) == 0,
        "no_false_thread_commit": int(threads.get("false_commits") or 0) == 0,
        "all_committed_delivered": int(
            (delivery.get("transactions") or {}).get("committed") or 0
        ) == int(delivery.get("delivered_responses") or 0),
        "no_self_talk_cooldown_violation": int(
            cadence.get("gaps_below_configured_cooldown") or 0
        ) == 0,
    }
    review_count = min(int(policy.get("operator_review_turns", 30)), output_count)
    return {
        "technical_live_ready": all(checks.values()),
        "human_content_review_required": True,
        "checks": checks,
        "counts": {
            "outputs": output_count,
            "delivered_outputs": len(delivered_texts),
            "fallbacks": len(fallback_ids),
            "empty": len(empty_ids),
            "duplicate_outputs": duplicate_outputs,
            "flagged": {key: len(value) for key, value in flagged.items()},
            "candidate_flagged": {
                key: len(value) for key, value in candidate_flagged.items()
            },
        },
        "ratios": {
            "fallback": round(fallback_ratio, 4),
            "exact_repetition": round(repetition_ratio, 4),
            "distinct_1": diversity["distinct_1"],
            "distinct_2": diversity["distinct_2"],
            "avg_words": diversity["avg_words"],
        },
        "latency": {
            "turn_ms": _distribution(latencies),
            "ttft_ms": _distribution(ttfts),
            "decode_tps": _distribution(decode_rates),
        },
        "empty_request_ids": empty_ids,
        "fallback_request_ids": fallback_ids,
        "flags": flagged,
        "candidate_flags": candidate_flagged,
        "operator_review_sample": _even_sample(calls, review_count),
    }


def _even_sample(
    calls: Sequence[dict[str, Any]], count: int,
) -> list[dict[str, Any]]:
    if count <= 0 or not calls:
        return []
    if count >= len(calls):
        return [dict(call) for call in calls]
    indices = {
        round(index * (len(calls) - 1) / (count - 1))
        for index in range(count)
    } if count > 1 else {0}
    return [dict(calls[index]) for index in sorted(indices)]


def _distribution(values: Sequence[float]) -> dict[str, float | None]:
    if not values:
        return {"min": None, "p50": None, "p95": None, "max": None}
    return {
        "min": round(min(values), 3),
        "p50": round(_percentile(values, 50), 3),
        "p95": round(_percentile(values, 95), 3),
        "max": round(max(values), 3),
    }


def _percentile(values: Sequence[float], percentile: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        raise ValueError("percentile requires at least one value")
    position = (len(ordered) - 1) * max(0.0, min(100.0, percentile)) / 100.0
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _normalize(text: str) -> str:
    return " ".join(re.findall(r"\w+", text.casefold(), flags=re.UNICODE))


def _distinct_ngrams(texts: Sequence[str]) -> dict[str, float]:
    """distinct-1/2 = unique n-gram / tổng n-gram (gộp toàn corpus output).

    Cao = từ vựng phong phú; thấp = lặp cụm, nhạt. avg_words = độ dài trung bình.
    """
    uni_total = uni_seen = bi_total = 0
    bi_seen: set[tuple[str, str]] = set()
    uni_set: set[str] = set()
    word_counts: list[int] = []
    for text in texts:
        toks = re.findall(r"\w+", str(text).casefold(), flags=re.UNICODE)
        word_counts.append(len(toks))
        for w in toks:
            uni_total += 1
            if w not in uni_set:
                uni_set.add(w); uni_seen += 1
        for a, b in zip(toks, toks[1:]):
            bi_total += 1
            bi_seen.add((a, b))
    return {
        "distinct_1": round(uni_seen / uni_total, 4) if uni_total else 0.0,
        "distinct_2": round(len(bi_seen) / bi_total, 4) if bi_total else 0.0,
        "avg_words": round(sum(word_counts) / len(word_counts), 1) if word_counts else 0.0,
    }


def _append_number(target: list[float], value: Any) -> None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return
    if math.isfinite(number):
        target.append(number)


def _round_optional(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, 3) if math.isfinite(number) else None


def _compact(value: Any, max_chars: int) -> str:
    return " ".join(str(value or "").split())[:max_chars]


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, nargs="?")
    parser.add_argument("--config-dir", type=str, default=None,
                        help="thư mục config thay thế (sweep sampling patch models.yaml ở đây)")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--burst-window-ms", type=int)
    parser.add_argument("--max-trace-items", type=int)
    parser.add_argument(
        "--reanalyze-report", type=Path,
        help="recompute quality gates from an existing stress report without LLM",
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    config_dir = Path(args.config_dir) if getattr(args, "config_dir", None) else REPO_ROOT / "config"
    loader = ConfigLoader(config_dir)
    loader.load_all()
    policy = dict(loader.get(
        "evaluation", "evaluation.youtube_llm_stress", {},
    ) or {})
    replay_policy = dict(loader.get(
        "evaluation", "evaluation.youtube_replay", {},
    ) or {})
    if args.reanalyze_report is not None:
        source = args.reanalyze_report
        if not source.is_absolute():
            source = REPO_ROOT / source
        stored = json.loads(source.read_text(encoding="utf-8"))
        quality = build_quality_report(
            dict(stored.get("replay") or {}),
            list((stored.get("llm") or {}).get("calls") or ()),
            policy,
        )
        output = args.output or source.with_name(source.stem + "_assessment.json")
        if not output.is_absolute():
            output = REPO_ROOT / output
        assessment = {
            "schema_version": 1,
            "mode": "youtube_llm_stress_reanalysis",
            "source_report": str(source.resolve()),
            "quality": quality,
        }
        _write_json(output, assessment)
        print(json.dumps({
            "output": str(output.resolve()),
            "technical_live_ready": quality["technical_live_ready"],
            "checks": quality["checks"],
            "counts": quality["counts"],
        }, ensure_ascii=False, indent=2))
        return 0 if quality["technical_live_ready"] else 1
    if args.input is None:
        raise ValueError("input dataset is required unless --reanalyze-report is used")
    output = args.output or Path(str(policy.get(
        "output_file", "logs/evaluation/youtube_llm_stress.json",
    )))
    checkpoint = args.checkpoint or Path(str(policy.get(
        "checkpoint_file", "logs/evaluation/youtube_llm_stress.checkpoint.json",
    )))
    if not output.is_absolute():
        output = REPO_ROOT / output
    if not checkpoint.is_absolute():
        checkpoint = REPO_ROOT / checkpoint

    service = LlamaCppLLMService.from_loader(loader)
    await service.start()
    health = await service.health_check()
    if not health.is_ok:
        await service.stop()
        raise RuntimeError("llama-server is not healthy on the configured endpoint")
    filter_service: RuleFilter | None = None
    regenerator: FilterRegenerator | None = None
    if bool(loader.get("features", "features.filter_rule.enabled", False)):
        filter_service = RuleFilter.from_config(loader)
        regenerator = FilterRegenerator.from_loader(
            loader, filter_service, service,
        )
        await filter_service.start()

    instrumented: InstrumentedLLMRunner | None = None

    def runner_factory(agent_state: Any, goal_manager: Any) -> InstrumentedLLMRunner:
        nonlocal instrumented
        prompt_manager = PromptManager.from_loader(loader)
        context = ConversationContextComposer.from_loader(
            loader, goal_provider=goal_manager.snapshot,
        )
        delegate = LLMTurnRunner.from_loader(
            loader,
            service,
            prompt_manager,
            FallbackManager(),
            CannedResponder.from_loader(loader),
            regenerator=regenerator,
            session_id="youtube-llm-stress",
            agent_state=_ReadOnlyAgentState(agent_state),
            conversation_context_renderer=context,
        )
        instrumented = InstrumentedLLMRunner(
            delegate,
            service,
            input_max_chars=int(policy.get("input_max_chars", 2400)),
            checkpoint=checkpoint,
            checkpoint_every=int(policy.get("checkpoint_every", 10)),
        )
        return instrumented

    started = time.perf_counter()
    try:
        replay = await simulate_replay(
            args.input,
            loader=loader,
            tick_window_ms=args.burst_window_ms or int(
                replay_policy.get("burst_window_ms", 1500)
            ),
            max_trace_items=args.max_trace_items or int(
                replay_policy.get("max_trace_items", 2000)
            ),
            runner_factory=runner_factory,
        )
    finally:
        if filter_service is not None:
            await filter_service.stop()
        await service.stop()
    if instrumented is None:
        raise RuntimeError("real LLM runner was not initialized")
    quality = build_quality_report(replay, instrumented.calls, policy)
    report = {
        "schema_version": 1,
        "mode": "youtube_director_real_llama_cpp_stress",
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "replay": replay,
        "llm": {
            "backend": "llama.cpp",
            "health_gate_passed": True,
            "metrics": service.get_metrics(),
            "filter_metrics": (
                filter_service.get_metrics() if filter_service is not None else None
            ),
            "calls": instrumented.calls,
        },
        "quality": quality,
    }
    _write_json(output, report)
    _write_json(checkpoint, {
        "schema_version": 1,
        "status": "complete",
        "output": str(output.resolve()),
        "generated": len(instrumented.calls),
    })
    print(json.dumps({
        "output": str(output.resolve()),
        "generated": len(instrumented.calls),
        "elapsed_seconds": report["elapsed_seconds"],
        "technical_live_ready": quality["technical_live_ready"],
        "checks": quality["checks"],
        "latency": quality["latency"],
    }, ensure_ascii=False, indent=2))
    return 0 if quality["technical_live_ready"] else 1


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
