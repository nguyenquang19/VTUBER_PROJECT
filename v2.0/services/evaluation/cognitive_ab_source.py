"""Offline, zero-side-effect MCB-4 source producer for paired cognitive A/B."""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Mapping

from interfaces.animation import MoodState
from interfaces.cognition import CognitionConfig, CognitiveContextRequest, CognitiveHardState
from interfaces.compatibility import SelfSnapshot, WorldSnapshot
from interfaces.llm import LLMRequest, LLMService, LLMToken
from interfaces.state import GoalSnapshot
from interfaces.state import (
    AgentEventKind,
    AgentEventSource,
    AgentStateSnapshot,
    EventProvenance,
    GroundedEvent,
    OpenThread,
    ThreadContribution,
    ThreadEvidence,
    ThreadKind,
    ThreadSpeaker,
    ThreadStatus,
    TopicState,
)
from services.cognition.brain_shadow import (
    CognitiveBrain,
    CognitiveBrainParseError,
    CognitiveBrainSchemaError,
)
from services.cognition.context_builder import CognitiveContextBuilder
from services.director.action_prompts import (
    join_directives,
    literal_grounding_directive,
    read_user_text,
    stage_direction_for,
)
from services.director.action_types import DirectorChatRef, DirectorInput
from services.director.chat_pulse import ChatPulse
from services.director.director import Director, DirectorAction
from services.director.salience import SaliencePool
from services.data.sanitize import mask_pii
from services.evaluation.cognitive_ab import CognitiveABCase, CognitiveABConfig, CognitiveABCorpus
from services.filter.rule_filter import RuleFilter
from services.llm.llama_cpp_llm import (
    LlamaCppBusyError,
    LlamaCppContextBudgetError,
    LlamaCppPreemptedError,
)
from services.llm.parser import parse_response
from services.llm.prompt_cache import PromptCache
from services.llm.prompt_manager import PromptManager


@dataclass(frozen=True)
class CognitiveABIdentity:
    config_digest: str
    corpus_digest: str
    model_digest: str
    persona_digest: str
    compatibility_prompt_digest: str
    brain_prompt_digest: str

    def to_dict(self) -> dict[str, str]:
        return {
            "config_digest": self.config_digest,
            "corpus_digest": self.corpus_digest,
            "model_digest": self.model_digest,
            "persona_digest": self.persona_digest,
            "compatibility_prompt_digest": self.compatibility_prompt_digest,
            "brain_prompt_digest": self.brain_prompt_digest,
        }


class _BoundedLLM(LLMService):
    """Force the exact A/B sampling tuple without changing production adapters."""

    service_id = "cognitive_ab_bounded_llm"

    def __init__(self, delegate: LLMService, *, seed: int, max_tokens: int, temperature: float) -> None:
        self._delegate = delegate
        self._seed = seed
        self._max_tokens = max_tokens
        self._temperature = temperature
        self.requests: list[LLMRequest] = []

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def health_check(self) -> Any:
        return await self._delegate.health_check()

    def get_metrics(self) -> dict[str, Any]:
        method = getattr(self._delegate, "get_metrics", None)
        return dict(method() or {}) if callable(method) else {}

    async def cancel(self, request_id: str) -> None:
        await self._delegate.cancel(request_id)

    async def generate_stream(self, request: LLMRequest) -> AsyncIterator[LLMToken]:
        bounded = request.model_copy(update={
            "seed": self._seed,
            "max_tokens": self._max_tokens,
            "temperature": self._temperature,
        })
        self.requests.append(bounded)
        async for token in self._delegate.generate_stream(bounded):
            yield token


class _SnapshotProvider:
    def __init__(self, value: Any) -> None:
        self._value = value

    def snapshot(self) -> Any:
        return self._value


class _EmptyMemory:
    async def query(self, _query: str, *, top_k: int, viewer_id: str | None = None) -> list[Any]:
        del top_k, viewer_id
        return []


class CognitiveABSourceProducer:
    """Run kernel compatibility and proposal-only Brain from one immutable case."""

    def __init__(
        self,
        *,
        loader: Any,
        service: LLMService,
        config: CognitiveABConfig,
        corpus: CognitiveABCorpus,
        identity: CognitiveABIdentity,
        source_revision: str,
        source_clean: bool,
        product_version: str,
    ) -> None:
        self._loader = loader
        self._service = service
        self._config = config
        self._corpus = corpus
        self._identity = identity
        self._source_revision = source_revision
        self._source_clean = source_clean
        self._product_version = product_version
        self._cognition = CognitionConfig.from_mapping(loader.section("cognition"))
        self._compatibility_timeout_seconds = float(loader.get(
            "models", "llm_canned.timeout_primary_s", 5.0,
        ))
        self._filter = (
            RuleFilter.from_config(loader)
            if bool(loader.get("features", "features.filter_rule.enabled", False))
            else None
        )

    async def collect(self, *, progress: Any = None) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        for index, case in enumerate(self._corpus.cases, start=1):
            row = await self._case(case, index=index)
            rows.append(row)
            if progress is not None:
                result = progress(index, len(self._corpus.cases), tuple(rows))
                if inspect.isawaitable(result):
                    await result
        return {
            "schema_version": self._config.schema_version,
            "marker": "mai_cognitive_ab_source",
            "source_revision": self._source_revision,
            "source_clean": self._source_clean,
            "product_version": self._product_version,
            "evidence_identity": self._identity.to_dict(),
            "rows": rows,
        }

    async def _case(self, case: CognitiveABCase, *, index: int) -> dict[str, Any]:
        seed = self._config.seed + index
        bounded = _BoundedLLM(
            self._service,
            seed=seed,
            max_tokens=self._config.generation_max_tokens,
            temperature=self._config.generation_temperature,
        )
        now = datetime(2026, 8, 24, tzinfo=timezone.utc) + timedelta(seconds=index)
        director_input, event_ref, agent = self._director_input(case, now)
        director = Director.from_loader(
            SaliencePool.from_loader(self._loader),
            ChatPulse.from_loader(self._loader),
            self._loader,
            clock=lambda: now.timestamp(),
            chat_gate_enabled=True,
        )
        director.start(now.timestamp())
        decision = director.decide(director_input)
        if case.scenario.operator_hold:
            decision = decision.__class__(
                action=DirectorAction.WAIT,
                segment=decision.segment,
                reason="operator_hold",
            )

        builder = CognitiveContextBuilder(
            self._cognition,
            world_model=_SnapshotProvider(WorldSnapshot(
                snapshot_id=f"world:{case.case_id}", created_at=now,
            )),
            self_model=_SnapshotProvider(SelfSnapshot(
                snapshot_id=f"self:{case.case_id}",
                created_at=now,
                speaking=False,
                busy=False,
                degraded=False,
                current_action_id=None,
                current_intention_id=None,
                active_goal_id=None,
                focused_thread_id=f"thread:{case.arc_id}",
                current_topic=case.arc_title,
                attention_target=event_ref,
                avatar_state={},
                recent_action_ids=(),
            )),
            capability_registry=_SnapshotProvider({"enabled": True, "capabilities": []}),
            agent_state=_SnapshotProvider(agent),
            goal_manager=_SnapshotProvider(GoalSnapshot()),
            thread_manager=_SnapshotProvider(agent.open_threads),
            memory_service=_EmptyMemory(),
            clock=lambda: now,
        )
        hard = self._hard_state(case)
        await builder.start()
        try:
            context = await builder.build(CognitiveContextRequest(
                config=self._cognition,
                schema_version=self._cognition.schema_version,
                request_id=f"ab-request:{case.case_id}",
                session_id="cognitive-ab-offline",
                requested_at=now,
                trigger_event_ref=event_ref,
                hard_state=hard,
            ))
        finally:
            await builder.stop()
        prompt_cache = PromptCache.from_loader(self._loader)
        brain_prompt = self._brain_prompt()
        hard_hold = case.scenario.operator_hold or case.scenario.safety_hold
        brain = None if context is None or hard_hold else CognitiveBrain(
            config=self._cognition, llm=bounded,
            persona_prompt=prompt_cache.text, shadow_prompt=brain_prompt,
        )
        if brain is not None:
            await brain.start()
        try:
            compatibility: dict[str, Any] | None = None
            brain_result: dict[str, Any] | None = None
            roles = (
                ("brain", "compatibility")
                if _swap(self._config.seed, case.case_id)
                else ("compatibility", "brain")
            )
            for role in roles:
                if role == "compatibility":
                    compatibility = await self._compatibility(
                        bounded, case, decision,
                    )
                else:
                    brain_result = (
                        await self._brain(brain, context)
                        if brain is not None else _candidate(
                            mode="WAIT",
                            action_label=("brain_wait" if hard_hold else "brain_preflight"),
                            output=None,
                            outcome=(
                                "COMPLETED" if hard_hold
                                else "STALE" if case.scenario.evidence_state == "stale"
                                else "PREFLIGHT_REJECTED"
                            ),
                            prompt_ref=self._identity.brain_prompt_digest,
                        )
                    )
        finally:
            if brain is not None:
                await brain.stop()
        assert compatibility is not None and brain_result is not None
        flags = [
            name for name, active in (
                ("safety_hold", case.scenario.safety_hold),
                ("operator_hold", case.scenario.operator_hold),
                (f"evidence_{case.scenario.evidence_state}", case.scenario.evidence_state != "fresh"),
            ) if active
        ]
        return {
            "case_id": case.case_id,
            "context_id": (
                context.context_id if context is not None
                else "ctx:" + hashlib.sha256(
                    f"{case.case_id}:{case.review_context}:{case.scenario.evidence_state}".encode("utf-8")
                ).hexdigest()
            ),
            "context_summary": case.review_context,
            "same_input_context": True,
            "profile_ref": self._identity.persona_digest,
            "model_ref": self._identity.model_digest,
            "seed": seed,
            "max_tokens": self._config.generation_max_tokens,
            "temperature": self._config.generation_temperature,
            "hard_flags": flags,
            "compatibility": compatibility,
            "brain": brain_result,
        }

    async def _compatibility(self, llm: _BoundedLLM, case: CognitiveABCase, decision: Any) -> dict[str, Any]:
        if decision.action is DirectorAction.WAIT:
            return _candidate(
                mode="WAIT", action_label=decision.action.value, output=None,
                outcome="COMPLETED", prompt_ref=self._identity.compatibility_prompt_digest,
            )
        prompt = PromptManager.from_loader(self._loader)
        stage = join_directives(stage_direction_for(decision), literal_grounding_directive())
        request = prompt.build_request_with_mood(
            request_id=f"ab-compat:{case.case_id}",
            user_text=read_user_text(decision) or case.input_text,
            current_mood=MoodState(),
            tone_flags=set(case.scenario.tone_flags),
            stage_direction=stage,
            grounded_context=(
                "[Offline immutable story context]\n"
                + _story_grounding(case)
            ),
            max_tokens=self._config.generation_max_tokens,
            temperature=self._config.generation_temperature,
        )
        started = time.perf_counter()
        chunks: list[str] = []
        input_tokens: int | None = None
        output_tokens: int | None = None
        try:
            async def consume() -> None:
                nonlocal input_tokens, output_tokens
                async for token in llm.generate_stream(request):
                    if token.token:
                        chunks.append(token.token)
                    if token.is_final:
                        input_tokens = _strict_optional_int(token.metadata.get("input_tokens"))
                        output_tokens = _strict_optional_int(token.metadata.get("tokens_predicted"))

            await asyncio.wait_for(
                consume(), timeout=self._compatibility_timeout_seconds,
            )
            parsed = parse_response("".join(chunks))
            output = _sanitized_output(parsed.text, self._config.max_candidate_output_chars)
            if not parsed.ok or output is None:
                return _candidate(
                    mode="WAIT", action_label=decision.action.value, output=None,
                    outcome="PARSE_REJECTED",
                    prompt_ref=self._identity.compatibility_prompt_digest,
                    latency_ms=_elapsed(started), input_tokens=input_tokens,
                    output_tokens=output_tokens,
                )
            if self._filter is not None:
                verdict = await self._filter.check(
                    output,
                    context={"messages": request.to_messages()},
                )
                if not verdict.passed:
                    return _candidate(
                        mode="WAIT", action_label=decision.action.value, output=None,
                        outcome="FILTER_REJECTED",
                        prompt_ref=self._identity.compatibility_prompt_digest,
                        latency_ms=_elapsed(started), input_tokens=input_tokens,
                        output_tokens=output_tokens,
                    )
            return _candidate(
                mode="SPEAK", action_label=decision.action.value,
                output=output, outcome="COMPLETED",
                prompt_ref=self._identity.compatibility_prompt_digest,
                latency_ms=_elapsed(started), input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return _candidate(
                mode="WAIT", action_label=decision.action.value, output=None,
                outcome=_outcome(exc), prompt_ref=self._identity.compatibility_prompt_digest,
                latency_ms=_elapsed(started), input_tokens=input_tokens,
                output_tokens=output_tokens,
            )

    async def _brain(self, brain: CognitiveBrain, context: Any) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            turn = await brain.propose(context)
            telemetry = brain.last_telemetry
            output = _sanitized_output(
                turn.speech_text, self._config.max_candidate_output_chars,
            )
            if turn.mode.value == "SPEAK" and output is None:
                return _candidate(
                    mode="WAIT", action_label="brain_speak", output=None,
                    outcome="FILTER_REJECTED",
                    prompt_ref=self._identity.brain_prompt_digest,
                    latency_ms=_elapsed(started),
                    input_tokens=None if telemetry is None else telemetry.input_tokens,
                    output_tokens=None if telemetry is None else telemetry.output_tokens,
                )
            return _candidate(
                mode=turn.mode.value,
                action_label=f"brain_{turn.mode.value.casefold()}",
                output=output,
                outcome="COMPLETED",
                prompt_ref=self._identity.brain_prompt_digest,
                latency_ms=_elapsed(started),
                input_tokens=None if telemetry is None else telemetry.input_tokens,
                output_tokens=None if telemetry is None else telemetry.output_tokens,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            telemetry = brain.last_telemetry
            return _candidate(
                mode="WAIT", action_label=(
                    f"brain_failure_{exc.code}"
                    if isinstance(exc, CognitiveBrainSchemaError)
                    else "brain_failure"
                ), output=None,
                outcome=_outcome(exc), prompt_ref=self._identity.brain_prompt_digest,
                latency_ms=_elapsed(started),
                input_tokens=None if telemetry is None else telemetry.input_tokens,
                output_tokens=None if telemetry is None else telemetry.output_tokens,
            )

    def _director_input(
        self, case: CognitiveABCase, now: datetime,
    ) -> tuple[DirectorInput, str, AgentStateSnapshot]:
        event_ref = f"agent:chat:{case.case_id}"
        is_donation = case.scenario.kind == "donation"
        event_timestamp = now
        if case.scenario.evidence_state == "stale":
            event_timestamp = now - timedelta(
                seconds=self._cognition.max_recent_speech_age_seconds + 1,
            )
        payload: Mapping[str, Any] = {
            "text": (
                42 if case.scenario.evidence_state == "malformed" else case.input_text
            ),
            "amount_vnd": case.scenario.amount_vnd,
        }
        event = GroundedEvent(
            event_id=event_ref,
            kind=(AgentEventKind.DONATION_RECEIVED if is_donation else AgentEventKind.CHAT_RECEIVED),
            source=(
                AgentEventSource.OPERATOR
                if case.scenario.sender_role == "operator" else AgentEventSource.YOUTUBE
            ),
            timestamp=event_timestamp,
            confidence=1.0,
            payload=payload,
            provenance=EventProvenance(
                producer="cognitive_ab_source",
                source_event_id=case.case_id,
                session_id="cognitive-ab-offline",
                platform="youtube",
            ),
        )
        topic = TopicState(
            summary=case.context_summary,
            source_event_id=event_ref,
            updated_at=now,
            confidence=1.0,
        )
        prior_events, thread = _story_snapshot(case, now, event_ref)
        chat_events = () if case.scenario.evidence_state == "missing" else (event,)
        agent = AgentStateSnapshot(
            current_topic=topic,
            open_threads=(thread,),
            recent_events=(*prior_events, *chat_events),
            last_spoken_summary=next(
                (
                    turn.text for turn in reversed(case.prior_turns)
                    if turn.role == "mai"
                ),
                None,
            ),
        )
        ref = DirectorChatRef(
            msg_id=event_ref,
            text=case.input_text,
            kind="chat",
            score=case.scenario.chat_score,
            created_at=now.timestamp(),
            viewer_id=f"viewer:{case.case_id}",
            viewer_name="viewer",
            amount_vnd=case.scenario.amount_vnd,
            is_super=is_donation,
            is_owner=case.scenario.sender_role == "operator",
            is_moderator=case.scenario.sender_role == "moderator",
        )
        candidates = (
            (ref,) if case.scenario.evidence_state == "fresh" else ()
        )
        return DirectorInput(
            now=now.timestamp(),
            agent_state=agent,
            goals=GoalSnapshot(),
            chat_candidates=candidates,
            pool_size=len(candidates),
            pulse_state=case.scenario.pulse_state,
            urge_ready=case.scenario.urge_ready,
            safety_hold=(case.scenario.safety_hold or case.scenario.operator_hold),
            tone_flags=case.scenario.tone_flags,
            self_talk_ready=case.scenario.self_talk_ready,
            self_talk_wait_reason=("scenario_not_ready" if not case.scenario.self_talk_ready else "ready"),
        ), event_ref, agent

    def _hard_state(self, case: CognitiveABCase) -> CognitiveHardState:
        return CognitiveHardState(
            config=self._cognition,
            schema_version=self._cognition.schema_version,
            emergency=False,
            operator_hold=case.scenario.operator_hold,
            safety_hold=case.scenario.safety_hold,
            permission_hold=False,
            transaction_conflict=False,
            critical_state=False,
            source_failure_codes=(),
        )

    def _brain_prompt(self) -> str:
        path = Path(self._cognition.brain_prompt_path)
        if not path.is_absolute():
            path = Path(__file__).resolve().parents[2] / path
        return path.read_text(encoding="utf-8")


def build_identity(loader: Any, *, repo_root: Path, corpus: CognitiveABCorpus) -> CognitiveABIdentity:
    cache = PromptCache.from_loader(loader)
    cognition = CognitionConfig.from_mapping(loader.section("cognition"))
    brain_path = Path(cognition.brain_prompt_path)
    if not brain_path.is_absolute():
        brain_path = Path(repo_root) / brain_path
    model_path = Path(str(loader.get("models", "llm_main.model_path", "")))
    if not model_path.is_absolute():
        model_path = Path(repo_root) / model_path
    action_prompt = Path(repo_root) / "services" / "director" / "action_prompts.py"
    prompt_manager = Path(repo_root) / "services" / "llm" / "prompt_manager.py"
    return CognitiveABIdentity(
        config_digest=_digest_tree(Path(repo_root) / "config", suffixes={".yaml", ".yml", ".txt"}),
        corpus_digest=corpus.digest,
        model_digest=_sha256_file(model_path),
        persona_digest=_sha256_text(cache.text),
        compatibility_prompt_digest=_sha256_parts((
            cache.text.encode("utf-8"), action_prompt.read_bytes(), prompt_manager.read_bytes(),
        )),
        brain_prompt_digest=_sha256_parts((cache.text.encode("utf-8"), brain_path.read_bytes())),
    )


def _candidate(
    *, mode: str, action_label: str, output: str | None, outcome: str,
    prompt_ref: str, latency_ms: float | None = None,
    input_tokens: int | None = None, output_tokens: int | None = None,
) -> dict[str, Any]:
    return {
        "mode": mode,
        "action_label": action_label,
        "output": output,
        "outcome": outcome,
        "prompt_ref": prompt_ref,
        "latency_ms": latency_ms,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }


def _story_grounding(case: CognitiveABCase) -> str:
    lines = [
        f"Episode: {case.arc_title}",
        f"Beat: {case.turn_index}/{case.arc_length}",
    ]
    if case.prior_turns:
        lines.append("Canonical prior transcript:")
        for turn in case.prior_turns:
            speaker = {"mai": "Mai", "viewer": "Viewer", "operator": "Operator"}[turn.role]
            lines.append(f"{speaker}: {turn.text}")
    lines.extend((f"Situation: {case.context_summary}", f"Current input: {case.input_text}"))
    return "\n".join(lines)


def _story_snapshot(
    case: CognitiveABCase, now: datetime, current_event_ref: str,
) -> tuple[tuple[GroundedEvent, ...], OpenThread]:
    events: list[GroundedEvent] = []
    evidence: list[ThreadEvidence] = []
    claims: list[ThreadContribution] = []
    viewer: list[ThreadContribution] = []
    for offset, turn in enumerate(case.prior_turns, start=1):
        event_id = f"agent:story:{case.case_id}:{offset}"
        timestamp = now - timedelta(seconds=len(case.prior_turns) - offset + 2)
        is_mai = turn.role == "mai"
        source = (
            AgentEventSource.LLM if is_mai
            else AgentEventSource.OPERATOR if turn.role == "operator"
            else AgentEventSource.YOUTUBE
        )
        events.append(GroundedEvent(
            event_id=event_id,
            kind=AgentEventKind.SPEECH_COMPLETED if is_mai else AgentEventKind.CHAT_RECEIVED,
            source=source,
            timestamp=timestamp,
            confidence=1.0,
            payload={
                "text": turn.text,
                **({"action": "read_chat"} if is_mai else {}),
            },
            provenance=EventProvenance(
                producer="cognitive_ab_story_fixture",
                source_event_id=f"story:{case.arc_id}:{case.turn_index}:{offset}",
                session_id="cognitive-ab-offline",
                platform="youtube",
            ),
        ))
        contribution = ThreadContribution(
            source_event_id=event_id,
            text=turn.text,
            speaker=ThreadSpeaker.MAI if is_mai else ThreadSpeaker.VIEWER,
        )
        if is_mai:
            claims.append(contribution)
        else:
            viewer.append(contribution)
            evidence.append(ThreadEvidence(event_id, turn.text, "story_fixture", 1.0))

    current_contribution = ThreadContribution(
        source_event_id=current_event_ref,
        text=case.input_text,
        speaker=ThreadSpeaker.VIEWER,
    )
    viewer.append(current_contribution)
    evidence.append(ThreadEvidence(
        current_event_ref, case.input_text, "story_fixture", 1.0,
    ))
    created_at = now - timedelta(seconds=max(2, len(case.prior_turns) + 2))
    thread = OpenThread(
        thread_id=f"thread:{case.arc_id}",
        topic=case.arc_title,
        summary=case.context_summary,
        created_at=created_at,
        updated_at=now,
        expires_at=now + timedelta(minutes=10),
        kind=ThreadKind.STORY,
        evidence=tuple(evidence),
        origin_event_id=evidence[0].source_event_id,
        status=ThreadStatus.ACTIVE,
        claims=tuple(claims),
        viewer_contributions=tuple(viewer),
        open_questions=(current_contribution,),
        move_count=case.turn_index - 1,
    )
    return tuple(events), thread


def _outcome(exc: BaseException) -> str:
    if isinstance(exc, asyncio.TimeoutError):
        return "TIMEOUT"
    if isinstance(exc, CognitiveBrainParseError):
        return "PARSE_REJECTED"
    if isinstance(exc, CognitiveBrainSchemaError):
        return "SCHEMA_REJECTED"
    if isinstance(exc, LlamaCppContextBudgetError):
        return "PREFLIGHT_REJECTED"
    if isinstance(exc, LlamaCppPreemptedError):
        return "CANCELLED"
    if isinstance(exc, LlamaCppBusyError):
        return "SERVICE_ERROR"
    return "SERVICE_ERROR"


def _swap(seed: int, case_id: str) -> bool:
    return bool(hashlib.sha256(f"{seed}:{case_id}".encode("utf-8")).digest()[0] & 1)


def _elapsed(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 3)


def _strict_optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _sanitized_output(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    output = " ".join(str(mask_pii(value) or "").split())
    return output if output and len(output) <= limit else None


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    if not path.is_file():
        raise ValueError(f"identity source file does not exist: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_parts(parts: tuple[bytes, ...]) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(len(part).to_bytes(8, "big"))
        digest.update(part)
    return digest.hexdigest()


def _digest_tree(root: Path, *, suffixes: set[str]) -> str:
    files = sorted(
        path for path in root.rglob("*")
        if path.is_file() and path.suffix.casefold() in suffixes
    )
    parts: list[bytes] = []
    for path in files:
        parts.extend((path.relative_to(root).as_posix().encode("utf-8"), path.read_bytes()))
    return _sha256_parts(tuple(parts))
