"""MCB-4 paired producer uses real adapters while remaining read-only."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from interfaces.base import HealthStatus
from interfaces.llm import LLMRequest, LLMToken, LLMWorkloadClass
from orchestrator.config_loader import ConfigLoader
from services.evaluation.cognitive_ab import (
    CognitiveABConfig,
    CognitiveABCorpus,
    CognitiveABEvaluation,
)
from services.evaluation.cognitive_ab_source import (
    CognitiveABIdentity,
    CognitiveABSourceProducer,
)


ROOT = Path(__file__).resolve().parents[2]


class _FakeLLM:
    service_id = "fake_cognitive_ab_llm"

    def __init__(self, *, fail_brain_index: int | None = None) -> None:
        self.requests: list[LLMRequest] = []
        self.cancelled: list[str] = []
        self._brain_calls = 0
        self._fail_brain_index = fail_brain_index

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def health_check(self) -> HealthStatus:
        return HealthStatus.healthy(self.service_id)

    async def cancel(self, request_id: str) -> None:
        self.cancelled.append(request_id)

    async def generate_stream(self, request: LLMRequest):
        self.requests.append(request)
        if request.workload_class is LLMWorkloadClass.SHADOW:
            self._brain_calls += 1
            if self._brain_calls == self._fail_brain_index:
                output = "not-json"
            else:
                context = json.loads(request.messages[-1].content)
                chat = context.get("chat_digest")
                wait = context["available_modes"] == ["WAIT"]
                ref = None if chat is None else chat["evidence_id"]
                output = json.dumps({
                    "mode": "WAIT" if wait else "SPEAK",
                    "attention_target_id": None if wait else ref,
                    "intent": None if wait else "phản ứng trực tiếp",
                    "speech_text": None if wait else "Nghe thấy rồi, để tớ nói thẳng nhé.",
                    "evidence_refs": [] if ref is None else [ref],
                    "uncertainty": "LOW",
                    "reason_codes": ["hard_hold" if wait else "propose_speech"],
                }, ensure_ascii=False, separators=(",", ":"))
        else:
            output = "Được rồi, tớ trả lời thẳng đây."
        yield LLMToken(request_id=request.request_id, token=output, is_final=False)
        yield LLMToken(
            request_id=request.request_id,
            token="",
            is_final=True,
            metadata={"input_tokens": 100, "tokens_predicted": 12},
        )


def _producer(fake: _FakeLLM) -> tuple[CognitiveABSourceProducer, CognitiveABConfig]:
    loader = ConfigLoader(ROOT / "config")
    loader.load_all()
    config = CognitiveABConfig.from_loader(loader)
    corpus = CognitiveABCorpus.load(ROOT / config.corpus_file, config)
    identity = CognitiveABIdentity(
        config_digest="1" * 64,
        corpus_digest=corpus.digest,
        model_digest="2" * 64,
        persona_digest="3" * 64,
        compatibility_prompt_digest="4" * 64,
        brain_prompt_digest="5" * 64,
    )
    return CognitiveABSourceProducer(
        loader=loader,
        service=fake,
        config=config,
        corpus=corpus,
        identity=identity,
        source_revision="a" * 40,
        source_clean=True,
        product_version="1.4.3",
    ), config


@pytest.mark.asyncio
async def test_source_producer_pairs_same_context_sampling_and_never_delivers() -> None:
    fake = _FakeLLM()
    producer, config = _producer(fake)
    artifact = await producer.collect()
    assert artifact["marker"] == "mai_cognitive_ab_source"
    assert len(artifact["rows"]) == 40
    assert {row["case_id"] for row in artifact["rows"]} == {
        case.case_id for case in producer._corpus.cases
    }
    assert all(row["same_input_context"] is True for row in artifact["rows"])
    assert all(row["context_id"].startswith("ctx:") for row in artifact["rows"])
    assert all(request.seed is not None for request in fake.requests)
    assert all(request.max_tokens == config.generation_max_tokens for request in fake.requests)
    assert all(request.temperature == config.generation_temperature for request in fake.requests)
    shadow_contexts = [
        json.loads(request.messages[-1].content)
        for request in fake.requests
        if request.workload_class is LLMWorkloadClass.SHADOW
    ]
    assert any(len(context["recent_delivered_speech"]) == 3 for context in shadow_contexts)
    assert any(context["conversation_state"]["thread_ref"] for context in shadow_contexts)
    assert any(
        "Canonical prior transcript:" in message.content
        for request in fake.requests
        if request.workload_class is LLMWorkloadClass.LIVE
        for message in request.messages
        if message.role == "system"
    )
    assert not any(hasattr(producer, name) for name in (
        "tts", "delivery_boundary", "transaction_manager", "memory_writer",
    ))
    operator = next(row for row in artifact["rows"] if row["case_id"] == "quiet-03")
    assert operator["hard_flags"] == ["operator_hold"]
    assert operator["compatibility"]["mode"] == "WAIT"
    assert operator["brain"]["mode"] == "WAIT"
    assert operator["brain"]["outcome"] == "COMPLETED"
    assert operator["brain"]["input_tokens"] is None
    loader = ConfigLoader(ROOT / "config")
    loader.load_all()
    private, review, manifest = CognitiveABEvaluation.from_loader(
        loader, repo_root=ROOT,
    ).build(artifact)
    assert private["summary"]["selected_pairs"] == 30
    assert min(private["summary"]["selected_per_arc"].values()) >= 2
    assert all("Tập:" in row["context_summary"] for row in review["rows"])
    assert review["status"] == "pending_human_review"
    assert review["commitment"] == manifest["commitment"]


@pytest.mark.asyncio
async def test_source_producer_preserves_model_failure_without_canned_padding() -> None:
    fake = _FakeLLM(fail_brain_index=3)
    producer, _config = _producer(fake)
    artifact = await producer.collect()
    failures = [row for row in artifact["rows"] if row["brain"]["outcome"] != "COMPLETED"]
    parse_failures = [
        row for row in failures if row["brain"]["outcome"] == "PARSE_REJECTED"
    ]
    assert len(parse_failures) == 1
    assert {row["brain"]["outcome"] for row in failures} == {
        "PARSE_REJECTED", "PREFLIGHT_REJECTED", "STALE",
    }
    assert parse_failures[0]["brain"]["mode"] == "WAIT"
    assert parse_failures[0]["brain"]["output"] is None
