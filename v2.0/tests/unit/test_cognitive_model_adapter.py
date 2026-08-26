"""Single-generation and backend-neutral Cognitive Model Adapter behavior."""
from __future__ import annotations

from pathlib import Path

import pytest

from interfaces.cognition import (
    CognitiveBrainParseError,
    CognitiveModelBusyError,
    CognitiveModelContextError,
    CognitiveModelPreemptedError,
)
from interfaces.llm import LLMRequest
from services.cognition.model_adapter import CognitiveModelAdapter
from services.llm.llama_cpp_llm import (
    LlamaCppBusyError,
    LlamaCppContextBudgetError,
    LlamaCppPreemptedError,
)
from tests.unit.test_cognitive_brain_shadow import (
    _LLM,
    _config,
    _context,
    _speak_json,
)


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.asyncio
async def test_adapter_owns_exactly_one_strict_shadow_generation() -> None:
    config = _config()
    llm = _LLM(_speak_json())
    adapter = CognitiveModelAdapter(
        config=config,
        llm=llm,
        persona_prompt="Mai persona",
        brain_prompt="Return strict JSON.",
    )
    await adapter.start()
    output = await adapter.generate(_context(config))

    assert output.raw_output == _speak_json()
    assert len(llm.requests) == 1
    request = llm.requests[0]
    assert request.workload_class.value == "shadow"
    assert request.context_overflow_policy.value == "reject"
    assert request.response_format is not None
    assert output.telemetry.request_id == request.request_id
    assert adapter.get_metrics()["cognitive_model_adapter_calls_total"] == 1


class _FailingLLM(_LLM):
    def __init__(self, error: Exception) -> None:
        super().__init__("")
        self.error = error

    async def generate_stream(self, request: LLMRequest):
        self.requests.append(request)
        if False:
            yield
        raise self.error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("backend_error", "contract_error"),
    [
        (LlamaCppBusyError("busy"), CognitiveModelBusyError),
        (LlamaCppContextBudgetError("large"), CognitiveModelContextError),
        (LlamaCppPreemptedError("preempted"), CognitiveModelPreemptedError),
    ],
)
async def test_adapter_maps_llama_failures_without_leaking_backend_types(
    backend_error: Exception,
    contract_error: type[Exception],
) -> None:
    config = _config()
    llm = _FailingLLM(backend_error)
    adapter = CognitiveModelAdapter(
        config=config,
        llm=llm,
        persona_prompt="Mai persona",
        brain_prompt="Return strict JSON.",
    )
    await adapter.start()

    with pytest.raises(contract_error):
        await adapter.generate(_context(config))
    assert len(llm.requests) == 1
    assert len(llm.cancelled) == 1


class _NoFinalLLM(_LLM):
    async def generate_stream(self, request: LLMRequest):
        self.requests.append(request)
        if False:
            yield


@pytest.mark.asyncio
async def test_adapter_rejects_stream_without_final_marker() -> None:
    config = _config()
    llm = _NoFinalLLM("")
    adapter = CognitiveModelAdapter(
        config=config,
        llm=llm,
        persona_prompt="Mai persona",
        brain_prompt="Return strict JSON.",
    )
    await adapter.start()

    with pytest.raises(CognitiveBrainParseError):
        await adapter.generate(_context(config))
    assert len(llm.requests) == 1
    assert len(llm.cancelled) == 1


def test_cognition_has_one_model_call_and_no_legacy_live_context_import() -> None:
    call_sites = []
    for path in (ROOT / "services" / "cognition").glob("*.py"):
        count = path.read_text(encoding="utf-8").count(".generate_stream(")
        call_sites.extend([path.name] * count)
    assert call_sites == ["model_adapter.py"]

    legacy_imports = (
        "services.agent.context_renderer",
        "services.agent.conversation_context",
    )
    for directory in ("orchestrator", "services", "scripts"):
        for path in (ROOT / directory).rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            assert not any(item in source for item in legacy_imports), path

    scheduler = (
        ROOT / "services" / "cognition" / "scheduler.py"
    ).read_text(encoding="utf-8")
    assert "services.llm.llama_cpp_llm" not in scheduler
    assert "services.cognition.brain" not in scheduler
