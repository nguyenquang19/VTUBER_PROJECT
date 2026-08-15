from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import AsyncIterator

from interfaces.base import HealthStatus
from interfaces.llm import LLMRequest, LLMService, LLMToken
from orchestrator.config_loader import ConfigLoader
from scripts.run_mood_ab_replay import build_pair_requests, load_cases, run_replay
from services.emotion.affect_style import AffectStyleRenderer
from services.emotion.affect_v2 import AffectV2
from services.llm.prompt_manager import PromptManager


ROOT = Path(__file__).resolve().parents[2]


def _loader() -> ConfigLoader:
    loader = ConfigLoader(ROOT / "config")
    loader.load_all()
    return loader


class FakeLLM(LLMService):
    service_id = "fake_mood_ab_llm"

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def health_check(self) -> HealthStatus:
        return HealthStatus.healthy(self.service_id)

    def get_metrics(self) -> dict:
        return {}

    async def cancel(self, request_id: str) -> None:
        return None

    async def generate_stream(self, request: LLMRequest) -> AsyncIterator[LLMToken]:
        version = request.request_id.rsplit("-", 1)[-1]
        yield LLMToken(
            request_id=request.request_id,
            token=f"generated {version}",
            is_final=False,
        )
        yield LLMToken(request_id=request.request_id, token="", is_final=True)


def test_stratified_corpus_has_exactly_twenty_five_cases() -> None:
    cases = load_cases(_loader())
    counts = Counter(case.category for case in cases)
    assert len(cases) == 25
    assert len(counts) == 10
    assert set(counts.values()) == {2, 3}
    assert sum(value == 3 for value in counts.values()) == 5
    assert len({case.case_id for case in cases}) == 25


def test_pair_uses_same_input_context_and_sampling_seed() -> None:
    loader = _loader()
    case = load_cases(loader)[0]
    v1, v2 = build_pair_requests(
        loader,
        PromptManager.from_loader(loader),
        AffectV2.from_loader(loader),
        AffectStyleRenderer.from_loader(loader),
        case,
        index=1,
    )
    assert v1.seed == v2.seed == 20260810
    assert v1.max_tokens == v2.max_tokens == 128
    assert v1.temperature == v2.temperature == 0.75
    assert v1.messages[0] == v2.messages[0]
    assert v1.messages[2:] == v2.messages[2:]
    assert v1.messages[1].content != v2.messages[1].content
    assert "Nhận lời khen ngắn" in v2.messages[1].content
    assert v1.messages[-1].content == case.input_text
    assert v1.messages[-2].content == case.context


async def test_replay_preserves_version_labels_before_blinding() -> None:
    loader = _loader()
    rows = await run_replay(loader, FakeLLM(), cases=load_cases(loader)[:2])
    assert len(rows) == 2
    assert all(row["same_input_context"] is True for row in rows)
    assert all(row["v1_output"] == "generated v1" for row in rows)
    assert all(row["v2_output"] == "generated v2" for row in rows)
    assert rows[0]["generation_seed"] == 20260810
